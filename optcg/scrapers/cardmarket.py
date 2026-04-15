"""
CardMarket scraper for One Piece TCG prices.

Cloudflare bypass strategy
──────────────────────────
CardMarket uses Cloudflare Managed Challenge. Headless browsers are reliably
detected and blocked regardless of stealth patches.

Practical solution: we auto-extract the cf_clearance cookie (and companion
cookies) from the Arc browser profile on macOS. curl_cffi presents the same
Chrome TLS fingerprint, so Cloudflare accepts the cookies.

Auto-extraction requires:
  • Arc browser installed (uses its Cookies SQLite DB)
  • macOS Keychain entry "Arc Safe Storage" (created automatically by Arc)
  • `cryptography` package (pip install cryptography)

Fallback — manual cookie entry:
  1. Open cardmarket.com in Chrome/Arc (loads normally for real users)
  2. DevTools → Application → Cookies → https://www.cardmarket.com
  3. Copy the value of `cf_clearance`
  4. Run: optcg config set-cookie <paste-value>

The cookie typically lasts several days. Re-run when you get 403s again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    _HAS_CFFI = False

from bs4 import BeautifulSoup

from optcg.scrapers.slugs import card_url, search_url, box_url, sealed_url
from optcg.config import CONFIG_FILE, CONFIG_DIR

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.cardmarket.com/en/OnePiece",
    "Connection":      "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Cookies we care about from the Arc profile
_CM_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "_cfuvid", "PHPSESSID"}

# Module-level cache so Keychain is only prompted once per process
_arc_key_cache: Optional[bytes] = None

# Persist derived key to disk; only re-ask Keychain after this many days
_ARC_KEY_TTL_DAYS = 7

# AppleScript: open URL in Arc without switching to it
_OPEN_BG_SCRIPT = """\
tell application "Arc"
    open location "https://www.cardmarket.com/en/OnePiece"
end tell
"""

# AppleScript: close any tab whose URL contains cardmarket.com
_CLOSE_CM_SCRIPT = """\
tell application "Arc"
    repeat with w in every window
        set doomed to {}
        repeat with t in (every tab of w)
            try
                if URL of t contains "cardmarket.com" then
                    set end of doomed to t
                end if
            end try
        end repeat
        repeat with t in doomed
            close t
        end repeat
    end repeat
end tell
"""


# ── Config / manual cookie store ─────────────────────────────────────────────

def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def get_cf_cookie() -> Optional[str]:
    """Return the stored cf_clearance cookie value, or None if not set."""
    return _load_config().get("cf_clearance")


def set_cf_cookie(value: str) -> None:
    """Persist the cf_clearance cookie value."""
    data = _load_config()
    data["cf_clearance"] = value.strip()
    data["cf_clearance_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_config(data)


def clear_cf_cookie() -> None:
    data = _load_config()
    data.pop("cf_clearance", None)
    data.pop("cf_clearance_saved", None)
    _save_config(data)


# ── Arc browser cookie auto-extraction ────────────────────────────────────────

def _arc_decrypt_value(enc: bytes, key: bytes) -> Optional[str]:
    """
    Decrypt a Chromium AES-CBC cookie value.

    Format: b'v10' (3 bytes) | IV (16 bytes) | ciphertext
    The decrypted plaintext has an internal 16-byte prefix before the actual
    value (Chromium prepends it for versioning), followed by PKCS#7 padding.
    """
    if not enc.startswith(b"v10"):
        # Unencrypted (plain text) — rare for CF cookies but handle it
        try:
            return enc.decode("utf-8")
        except Exception:
            return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        iv = enc[3:19]
        ct = enc[19:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        raw = decryptor.update(ct) + decryptor.finalize()
        pad_len = raw[-1]
        # Skip Chromium's 16-byte internal prefix, strip PKCS#7 padding
        value = raw[16:-pad_len].decode("utf-8")
        return value
    except Exception as exc:
        logger.debug("Arc cookie decrypt error: %s", exc)
        return None


def _arc_master_key() -> Optional[bytes]:
    """
    Retrieve Arc's AES master key.

    Cache hierarchy:
      1. Module-level (_arc_key_cache)   — free, per-process
      2. Config file (arc_key_b64)       — free, survives for _ARC_KEY_TTL_DAYS
      3. macOS Keychain                  — prompts user, at most once per week
    """
    import base64
    from datetime import datetime, timedelta

    global _arc_key_cache
    if _arc_key_cache is not None:
        return _arc_key_cache

    # ── Level 2: file cache ───────────────────────────────────────────────────
    cfg = _load_config()
    b64 = cfg.get("arc_key_b64")
    saved_at = cfg.get("arc_key_cached_at")
    if b64 and saved_at:
        try:
            age = datetime.now() - datetime.fromisoformat(saved_at)
            if age < timedelta(days=_ARC_KEY_TTL_DAYS):
                _arc_key_cache = base64.b64decode(b64)
                logger.debug("Arc key loaded from file cache (age %s)", age)
                return _arc_key_cache
        except Exception:
            pass  # corrupt entry — fall through to Keychain

    # ── Level 3: Keychain ─────────────────────────────────────────────────────
    try:
        master = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", "Arc Safe Storage"],
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).strip()
        key = hashlib.pbkdf2_hmac("sha1", master, b"saltysalt", 1003, 16)
        _arc_key_cache = key

        # Persist so the next 7 days of runs skip Keychain entirely
        cfg["arc_key_b64"] = base64.b64encode(key).decode()
        cfg["arc_key_cached_at"] = datetime.now().isoformat()
        _save_config(cfg)
        logger.debug("Arc key fetched from Keychain and cached to disk")

        return key
    except Exception as exc:
        logger.debug("Arc Keychain lookup failed: %s", exc)
        return None


def _arc_cookie_db() -> Optional[Path]:
    """Return path to Arc's Cookies SQLite file, or None if not found."""
    candidate = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Arc"
        / "User Data"
        / "Default"
        / "Cookies"
    )
    return candidate if candidate.exists() else None


def _read_arc_cookies_from_db() -> dict[str, str]:
    """Low-level: read and decrypt CardMarket cookies from Arc's SQLite DB."""
    db_path = _arc_cookie_db()
    if not db_path:
        return {}
    key = _arc_master_key()
    if not key:
        return {}
    result: dict[str, str] = {}
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%cardmarket%'"
        ).fetchall()
        conn.close()
        for row in rows:
            name = row["name"]
            if name not in _CM_COOKIE_NAMES:
                continue
            value = _arc_decrypt_value(bytes(row["encrypted_value"]), key)
            if value:
                result[name] = value
    except Exception as exc:
        logger.debug("Arc cookie DB read error: %s", exc)
    return result


def _arc_refresh_cookies(wait: float = 5.0) -> dict[str, str]:
    """
    Open cardmarket.com silently in Arc (no focus steal), wait for CF to set
    fresh cookies, read them, then close the tab — all without interrupting
    whatever the user is doing.
    """
    if not _arc_cookie_db():
        return {}

    logger.debug("Refreshing CardMarket cookies via Arc (background tab)…")

    # Open the page without bringing Arc to the foreground
    try:
        subprocess.run(
            ["osascript", "-e", _OPEN_BG_SCRIPT],
            check=True,
            timeout=8,
            capture_output=True,
        )
    except Exception as exc:
        logger.debug("Arc AppleScript open failed: %s", exc)
        # Fallback: plain open -g so we at least don't steal focus
        try:
            subprocess.run(
                ["open", "-g", "-a", "Arc",
                 "https://www.cardmarket.com/en/OnePiece"],
                check=True, timeout=5,
            )
        except Exception:
            return {}

    # Wait for page load + CF cookie issuance
    time.sleep(wait)

    cookies = _read_arc_cookies_from_db()

    # Close the CardMarket tab silently
    try:
        subprocess.run(
            ["osascript", "-e", _CLOSE_CM_SCRIPT],
            timeout=8,
            capture_output=True,
        )
        logger.debug("CardMarket tab closed in Arc")
    except Exception as exc:
        logger.debug("Could not close Arc tab: %s", exc)

    return cookies


def auto_import_arc_cookies(refresh_if_stale: bool = False) -> dict[str, str]:
    """
    Extract CardMarket cookies from Arc browser on macOS.

    If refresh_if_stale=True and a cf_clearance cookie exists but __cf_bm is
    missing or stale, trigger a background Arc page-load to refresh cookies.

    Returns a dict of {cookie_name: cookie_value}.
    Returns an empty dict if Arc is not installed or decryption fails.
    """
    result = _read_arc_cookies_from_db()

    if not result:
        logger.debug("Arc Cookies DB not found or unreadable — skipping auto-import")
        return {}

    if result:
        logger.debug(
            "Auto-imported %d CardMarket cookies from Arc: %s",
            len(result),
            list(result.keys()),
        )

    if refresh_if_stale and "cf_clearance" not in result:
        logger.debug("cf_clearance missing — refreshing via Arc")
        result = _arc_refresh_cookies()

    return result


# ── HTTP session ──────────────────────────────────────────────────────────────

def _make_session(warmup: bool = False) -> object:
    if _HAS_CFFI:
        sess = cffi_requests.Session(impersonate="chrome133a")
    else:
        import requests
        sess = requests.Session()
        logger.warning("curl_cffi not installed; install it for better results")

    # Primary: auto-import from Arc browser
    arc_cookies = auto_import_arc_cookies()
    if arc_cookies:
        for name, value in arc_cookies.items():
            sess.cookies.set(name, value, domain="www.cardmarket.com")
    else:
        # Fallback: manually stored cf_clearance
        cf = get_cf_cookie()
        if cf:
            sess.cookies.set("cf_clearance", cf, domain="www.cardmarket.com")

    if warmup:
        # A single GET to the hub page lets Cloudflare issue a fresh __cf_bm
        # for this TLS session before we hit any product page.
        try:
            sess.get(
                "https://www.cardmarket.com/en/OnePiece",
                headers=_HEADERS,
                timeout=20,
            )
            time.sleep(0.3)
        except Exception:
            pass

    return sess


def _is_cf_block(status: int, html: str) -> bool:
    return status in (403, 503) or (
        status == 200 and (
            "challenge-platform" in html
            or "chl_page" in html
            or "just a moment" in html[:3000].lower()
            or "un momento" in html[:3000].lower()
        )
    )


def _fetch(url: str, session=None, _retry: bool = True) -> Optional[str]:
    if session is None:
        session = _make_session(warmup=True)
    try:
        resp = session.get(url, headers=_HEADERS, timeout=20)
        if _is_cf_block(resp.status_code, resp.text):
            if _retry and _arc_cookie_db():
                # Cookies are stale — open Arc to refresh them and retry once
                logger.info(
                    "CardMarket blocked. Refreshing cookies via Arc…"
                )
                fresh = _arc_refresh_cookies(wait=5.0)
                if fresh:
                    for name, value in fresh.items():
                        session.cookies.set(name, value, domain="www.cardmarket.com")
                    return _fetch(url, session=session, _retry=False)
            # Could not auto-refresh
            arc_ok = bool(_arc_cookie_db())
            if arc_ok:
                logger.warning(
                    "CardMarket blocked — open cardmarket.com in Arc to "
                    "solve the Cloudflare challenge, then retry."
                )
            elif not get_cf_cookie():
                logger.warning(
                    "CardMarket blocked (no cf_clearance cookie set). "
                    "Run: optcg config set-cookie <value>  "
                    "See instructions: optcg config cookie-help"
                )
            else:
                logger.warning(
                    "CardMarket blocked — cf_clearance cookie may be expired. "
                    "Refresh it with: optcg config set-cookie <new-value>"
                )
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        time.sleep(0.5)
        return resp.text
    except Exception as exc:
        logger.error("CardMarket fetch error: %s", exc)
        return None


# ── Price parsing ─────────────────────────────────────────────────────────────

def _parse_eur(text: str) -> Optional[float]:
    """'45,99 €' → 45.99   '1.234,56 €' → 1234.56"""
    if not text or text.strip() in ("N/A", "—", "-", ""):
        return None
    cleaned = text.replace("€", "").replace("\xa0", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if "," in cleaned:
        integer, _, decimal = cleaned.rpartition(",")
        cleaned = f"{integer.replace('.', '')}.{decimal}"
    else:
        cleaned = cleaned.replace(".", "")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _parse_prices(html: str, language_filtered: bool = False) -> dict:
    soup   = BeautifulSoup(html, "lxml")
    prices: dict[str, float] = {}

    # Strategy 1 — <dl> info tables (info-box: trend/from/market)
    for dl in soup.select("dl"):
        for dt, dd in zip(dl.select("dt"), dl.select("dd")):
            key = dt.get_text(strip=True).lower()
            val = dd.get_text(strip=True)
            p   = _parse_eur(val)
            if not p:
                continue
            if "trend" in key and "trend" not in prices:
                prices["trend"] = p
            elif ("low" in key or "from" in key) and "low" not in prices:
                prices["low"] = p
            elif ("market" in key or "avg" in key or "average" in key) and "market" not in prices:
                prices["market"] = p

    # Strategy 2 — JSON-LD
    if "low" not in prices:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data   = json.loads(script.string or "")
                offers = data.get("offers", {})
                if isinstance(offers, dict):
                    p = _parse_eur(str(offers.get("price", "")))
                    if p:
                        prices["low"] = p
            except Exception:
                pass

    # Strategy 3 — price-trend spans
    if "trend" not in prices:
        for span in soup.select(".price-trend span, span.color-primary"):
            p = _parse_eur(span.get_text(strip=True))
            if p:
                prices["trend"] = p
                break

    # Strategy 4 — article listing prices (language-filtered product pages)
    # When ?language=N is set, the listed articles are already filtered.
    # The info-box "trend" is global (all languages) so we scrape the actual
    # offer rows to get the real minimum EN/JP price.
    if language_filtered:
        article_prices: list[float] = []
        # CardMarket uses .article-row or table rows inside .article-list/.table-body
        for selector in (
            ".article-row .col-offer-price span",
            ".article-row span.color-primary",
            "table.article-table td.col-offer-price span",
            ".table-body .col-offer-price span.color-primary",
            "td.col-sellerProductInfo ~ td span.color-primary",
            # Fallback: any span.color-primary inside a row-like container,
            # skipping the first (header trend)
        ):
            candidates = [_parse_eur(s.get_text(strip=True))
                          for s in soup.select(selector)]
            candidates = [p for p in candidates if p]
            if candidates:
                article_prices.extend(candidates)
                break

        if article_prices:
            article_min = min(article_prices)
            # Override info-box "low" with the actual cheapest language-specific listing
            prices["low"] = article_min
            # The info-box trend is cross-language; don't trust it when we have
            # real article data — store as a separate key so callers can choose.
            prices["article_min"] = article_min

    # Image — og:image meta tag; skip the generic CM fallback logo
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = og["content"]
        if "logos/cardmarket" not in src and "cardmarket-logo" not in src:
            prices["img"] = src

    return prices


def _follow_first_product_link(html: str, session) -> tuple[Optional[str], dict]:
    soup = BeautifulSoup(html, "lxml")
    for selector in (
        "a[href*='/Products/Singles/']",
        "a[href*='/Products/Booster']",
        "a[href*='/Products/Sealed']",
        ".product-list a",
        ".table-body .col-title a",
    ):
        links = soup.select(selector)
        if links:
            href = links[0].get("href", "")
            if href.startswith("/"):
                href = f"https://www.cardmarket.com{href}"
            page = _fetch(href, session)
            if page:
                return href, _parse_prices(page)
    return None, {}


# ── Public API ────────────────────────────────────────────────────────────────

def get_card_prices(
    name: str,
    set_code: str = None,
    card_number: str = None,
    language: str = None,
    item_type: str = "card",
    known_url: str = None,
) -> dict:
    """
    Fetch CardMarket prices for any One Piece product.

    known_url: previously cached CM product URL — tried first, skips slug/search.
    Returns: {trend, low, market, url, error}
    """
    result: dict = {
        "trend": None, "low": None, "market": None,
        "url": None, "img": None, "error": None,
    }
    session = _make_session()

    _is_lang_filtered = language is not None and item_type in (
        "booster_box", "blister", "sealed_set"
    )

    # ── Known URL (cached from previous successful fetch) ─────────────────────
    if known_url:
        html = _fetch(known_url, session)
        if html:
            prices = _parse_prices(html, language_filtered=_is_lang_filtered)
            if prices:
                result.update(prices)
                result["url"] = known_url
                return result

    # ── Derived direct URL ────────────────────────────────────────────────────
    direct_url = None
    if set_code and item_type in ("card", "promo"):
        direct_url = card_url(set_code, name, language)
    elif item_type == "booster_box":
        direct_url = box_url(set_code or "", name, language)
    elif item_type in ("blister", "sealed_set"):
        direct_url = sealed_url(name, language)

    if direct_url and direct_url != known_url:
        html = _fetch(direct_url, session)
        if html:
            prices = _parse_prices(html, language_filtered=_is_lang_filtered)
            if prices:
                result.update(prices)
                result["url"] = direct_url
                return result

    # ── Search fallback ───────────────────────────────────────────────────────
    # For sealed products (booster boxes, blisters) CardMarket lists EN and JP
    # as separate product pages — inject language into the search query so the
    # correct edition is returned.  Sealed products are always mint/sealed, so
    # no condition filtering is needed.
    _SEALED_TYPES = ("booster_box", "blister", "sealed_set")
    if item_type in _SEALED_TYPES and language:
        lang_label = {"EN": "English", "JP": "Japanese"}.get(
            language.upper(), language.upper()
        )
        query = f"{name} {lang_label}"
    elif card_number:
        query = f"{card_number} {name}"
    else:
        query = name
    s_url = search_url(query, language)
    html  = _fetch(s_url, session)

    if not html:
        result["error"] = (
            "CardMarket blocked. "
            "Open cardmarket.com in Arc to refresh cookies, "
            "or set manually: optcg config set-cookie <value>  |  optcg config cookie-help"
        )
        return result

    prices = _parse_prices(html)
    if prices:
        result.update(prices)
        result["url"] = s_url
        return result

    product_url, prices = _follow_first_product_link(html, session)
    if prices:
        result.update(prices)
        result["url"] = product_url
        return result

    result["error"] = "No prices found on CardMarket for this query"
    return result
