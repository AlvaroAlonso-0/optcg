"""
CardMarket scraper for One Piece TCG prices.

Cloudflare bypass strategy
──────────────────────────
CardMarket uses Cloudflare Managed Challenge. curl_cffi presents the same
Chrome TLS fingerprint, so Cloudflare accepts cookies from a real browser.

Cookies are auto-extracted from Arc or Chrome on both macOS and Windows.

macOS (Arc / Chrome):
  • AES-CBC, 16-byte key via PBKDF2(keychain_password, "saltysalt", 1003)
  • Keychain services: "Arc Safe Storage", "Chrome Safe Storage"

Windows (Arc / Chrome):
  • AES-GCM, 32-byte key stored in browser's Local State, DPAPI-wrapped
  • ctypes CryptUnprotectData — no extra packages needed

Fallback — manual cookie entry:
  1. Open cardmarket.com in Chrome/Arc
  2. DevTools → Application → Cookies → https://www.cardmarket.com
  3. Copy value of cf_clearance
  4. Run: optcg config set-cookie <value>
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import platform
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
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

_PLATFORM = platform.system()   # "Darwin" | "Windows" | "Linux"

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

_CM_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "_cfuvid", "PHPSESSID"}
_KEY_TTL_DAYS    = 7

# Per-browser in-process key cache  {cache_key: bytes}
_key_cache: dict[str, bytes] = {}

# Arc refresh throttle — only open browser once per process lifetime
_arc_refresh_done: bool = False

# CF status cache — avoid hammering CM just to check status
_cf_blocked_cache: Optional[bool] = None
_cf_blocked_at: float = 0.0
_CF_CACHE_TTL = 120.0  # seconds

# AppleScript: open CardMarket in Arc without stealing focus (macOS only)
_OPEN_BG_SCRIPT = """\
tell application "Arc"
    open location "https://www.cardmarket.com/en/OnePiece"
end tell
"""
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
    return _load_config().get("cf_clearance")


def set_cf_cookie(value: str) -> None:
    data = _load_config()
    data["cf_clearance"] = value.strip()
    data["cf_clearance_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_config(data)


def clear_cf_cookie() -> None:
    data = _load_config()
    data.pop("cf_clearance", None)
    data.pop("cf_clearance_saved", None)
    _save_config(data)


# ── macOS: AES-CBC cookie decryption ─────────────────────────────────────────

def _macos_decrypt_value(enc: bytes, key: bytes) -> Optional[str]:
    """Decrypt Chromium AES-CBC cookie (macOS).
    Format: b'v10' | IV(16) | ciphertext; plaintext has 16-byte internal prefix.
    """
    if not enc.startswith(b"v10"):
        try:
            return enc.decode("utf-8")
        except Exception:
            return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        iv  = enc[3:19]
        ct  = enc[19:]
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        raw = dec.update(ct) + dec.finalize()
        pad = raw[-1]
        return raw[16:-pad].decode("utf-8")
    except Exception as exc:
        logger.debug("macOS cookie decrypt error: %s", exc)
        return None


def _macos_master_key(keychain_service: str, cache_key: str) -> Optional[bytes]:
    """Retrieve Chromium AES master key from macOS Keychain.
    Cache hierarchy: in-process dict → config file (7 days) → Keychain.
    """
    import base64

    if cache_key in _key_cache:
        return _key_cache[cache_key]

    cfg      = _load_config()
    b64_key  = f"{cache_key}_key_b64"
    b64_at   = f"{cache_key}_key_cached_at"
    b64      = cfg.get(b64_key)
    saved_at = cfg.get(b64_at)
    if b64 and saved_at:
        try:
            age = datetime.now() - datetime.fromisoformat(saved_at)
            if age < timedelta(days=_KEY_TTL_DAYS):
                _key_cache[cache_key] = base64.b64decode(b64)
                return _key_cache[cache_key]
        except Exception:
            pass

    try:
        master = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", keychain_service],
            stderr=subprocess.DEVNULL, timeout=15,
        ).strip()
        key = hashlib.pbkdf2_hmac("sha1", master, b"saltysalt", 1003, 16)
        _key_cache[cache_key] = key
        cfg[b64_key] = base64.b64encode(key).decode()
        cfg[b64_at]  = datetime.now().isoformat()
        _save_config(cfg)
        logger.debug("Key fetched from Keychain: %s", keychain_service)
        return key
    except Exception as exc:
        logger.debug("Keychain lookup failed (%s): %s", keychain_service, exc)
        return None


# ── Windows: DPAPI + AES-GCM cookie decryption ───────────────────────────────

def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt bytes with Windows CryptUnprotectData (no extra packages)."""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf    = ctypes.create_string_buffer(data, len(data))
    blob_in  = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _windows_browser_key(local_state_path: Path, cache_key: str) -> Optional[bytes]:
    """Extract AES-GCM key from Windows browser Local State (DPAPI-wrapped)."""
    import base64

    if cache_key in _key_cache:
        return _key_cache[cache_key]
    try:
        state   = json.loads(local_state_path.read_text(encoding="utf-8"))
        enc_key = base64.b64decode(state["os_crypt"]["encrypted_key"])[5:]  # strip b"DPAPI"
        key     = _dpapi_decrypt(enc_key)
        _key_cache[cache_key] = key
        return key
    except Exception as exc:
        logger.debug("Windows browser key extract failed (%s): %s", cache_key, exc)
        return None


def _windows_decrypt_value(enc: bytes, key: bytes) -> Optional[str]:
    """Decrypt Chromium AES-GCM cookie (Windows).
    v10/v20: b'v10'|nonce(12)|ciphertext+tag(16).
    Legacy: raw DPAPI blob.
    """
    if enc[:3] in (b"v10", b"v20"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = enc[3:15]
            ct    = enc[15:]
            return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
        except Exception as exc:
            logger.debug("AES-GCM decrypt failed: %s", exc)
            return None
    else:
        try:
            return _dpapi_decrypt(enc).decode("utf-8")
        except Exception:
            return None


# ── Browser profile discovery ─────────────────────────────────────────────────

def _macos_browsers() -> list[tuple[str, Path, str]]:
    """Return [(browser_name, cookie_db_path, keychain_service)] available on macOS."""
    home = Path.home()
    candidates = [
        ("arc",    home / "Library/Application Support/Arc/User Data/Default/Cookies",
                   "Arc Safe Storage"),
        ("chrome", home / "Library/Application Support/Google/Chrome/Default/Cookies",
                   "Chrome Safe Storage"),
        ("brave",  home / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
                   "Brave Browser"),
        ("edge",   home / "Library/Application Support/Microsoft Edge/Default/Cookies",
                   "Microsoft Edge"),
    ]
    return [(name, p, svc) for name, p, svc in candidates if p.exists()]


def _windows_browsers() -> list[tuple[str, Path, Path]]:
    """Return [(browser_name, cookie_db_path, local_state_path)] available on Windows."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    results: list[tuple[str, Path, Path]] = []

    candidates: list[tuple[str, str, str]] = [
        ("chrome",
         str(local / "Google/Chrome/User Data/Default/Network/Cookies"),
         str(local / "Google/Chrome/User Data/Local State")),
        ("edge",
         str(local / "Microsoft/Edge/User Data/Default/Network/Cookies"),
         str(local / "Microsoft/Edge/User Data/Local State")),
        ("brave",
         str(local / "BraveSoftware/Brave-Browser/User Data/Default/Network/Cookies"),
         str(local / "BraveSoftware/Brave-Browser/User Data/Local State")),
    ]

    # Arc on Windows — Microsoft Store package ID varies, use glob
    arc_cookie_globs = [
        str(local / "Packages/TheBrowser.App_*/LocalCache/Roaming/Arc/User Data/Default/Network/Cookies"),
        str(local / "Arc/User Data/Default/Network/Cookies"),
    ]
    arc_state_globs = [
        str(local / "Packages/TheBrowser.App_*/LocalCache/Roaming/Arc/User Data/Local State"),
        str(local / "Arc/User Data/Local State"),
    ]
    for cg, sg in zip(arc_cookie_globs, arc_state_globs):
        cm = glob.glob(cg)
        sm = glob.glob(sg)
        if cm and sm:
            candidates.insert(0, ("arc", cm[0], sm[0]))
            break

    for name, cookie_path, state_path in candidates:
        cp = Path(cookie_path)
        sp = Path(state_path)
        if cp.exists() and sp.exists():
            results.append((name, cp, sp))

    return results


# ── Generic cookie reader ─────────────────────────────────────────────────────

def _read_cookies_from_db(
    db_path: Path,
    decrypt_fn,
) -> dict[str, str]:
    """Read and decrypt CardMarket cookies from a Chromium SQLite Cookies file."""
    result: dict[str, str] = {}
    try:
        # Open read-only; copy to temp if locked (Windows locks the file)
        uri = f"file:{db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError:
            import shutil, tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            shutil.copy2(db_path, tmp.name)
            conn = sqlite3.connect(tmp.name)
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
            value = decrypt_fn(bytes(row["encrypted_value"]))
            if value:
                result[name] = value
    except Exception as exc:
        logger.debug("Cookie DB read error (%s): %s", db_path, exc)
    return result


# ── Public auto-import ────────────────────────────────────────────────────────

def auto_import_browser_cookies() -> dict[str, str]:
    """
    Extract CardMarket cookies from any available Chromium browser.

    macOS: tries Arc → Chrome → Brave → Edge (AES-CBC via Keychain)
    Windows: tries Arc → Chrome → Brave → Edge (AES-GCM via DPAPI Local State)

    Returns {cookie_name: value}. Empty dict if nothing found.
    """
    if _PLATFORM == "Darwin":
        for name, db_path, keychain_svc in _macos_browsers():
            cache_key = name
            key = _macos_master_key(keychain_svc, cache_key)
            if not key:
                continue
            cookies = _read_cookies_from_db(
                db_path,
                lambda enc, k=key: _macos_decrypt_value(enc, k),
            )
            if cookies:
                logger.debug("Got %d CM cookies from %s (macOS)", len(cookies), name)
                return cookies

    elif _PLATFORM == "Windows":
        for name, db_path, state_path in _windows_browsers():
            cache_key = name
            key = _windows_browser_key(state_path, cache_key)
            if not key:
                continue
            cookies = _read_cookies_from_db(
                db_path,
                lambda enc, k=key: _windows_decrypt_value(enc, k),
            )
            if cookies:
                logger.debug("Got %d CM cookies from %s (Windows)", len(cookies), name)
                return cookies

    logger.debug("No browser cookies found on %s", _PLATFORM)
    return {}


# Backward-compat alias (used internally and in _make_session)
def auto_import_arc_cookies(refresh_if_stale: bool = False) -> dict[str, str]:
    cookies = auto_import_browser_cookies()
    if not cookies and refresh_if_stale and _PLATFORM == "Darwin":
        cookies = _arc_refresh_cookies()
    return cookies


def _arc_cookie_db() -> Optional[Path]:
    """Return Arc's Cookies path if it exists (macOS only). Used for refresh check."""
    p = Path.home() / "Library/Application Support/Arc/User Data/Default/Cookies"
    return p if p.exists() else None


def _arc_refresh_cookies(wait: float = 5.0) -> dict[str, str]:
    """Open cardmarket.com in Arc silently, wait for fresh CF cookies, close tab."""
    global _arc_refresh_done
    if _arc_refresh_done:
        logger.debug("Arc cookie refresh already attempted this session — skipping")
        return {}
    if not _arc_cookie_db():
        return {}
    _arc_refresh_done = True
    logger.debug("Refreshing CardMarket cookies via Arc…")
    try:
        subprocess.run(["osascript", "-e", _OPEN_BG_SCRIPT],
                       check=True, timeout=8, capture_output=True)
    except Exception as exc:
        logger.debug("Arc AppleScript open failed: %s", exc)
        try:
            subprocess.run(["open", "-g", "-a", "Arc",
                            "https://www.cardmarket.com/en/OnePiece"],
                           check=True, timeout=5)
        except Exception:
            return {}
    time.sleep(wait)
    cookies = auto_import_browser_cookies()
    try:
        subprocess.run(["osascript", "-e", _CLOSE_CM_SCRIPT],
                       timeout=8, capture_output=True)
    except Exception:
        pass
    return cookies


# ── CF status probe ───────────────────────────────────────────────────────────

def is_cf_blocked() -> bool:
    """Return True if CardMarket search is currently Cloudflare-blocked.

    Uses a lightweight probe against the search endpoint (which CF challenges
    more aggressively than the homepage). Result cached for 2 minutes so
    repeated calls don't hammer CM.
    """
    global _cf_blocked_cache, _cf_blocked_at
    now = time.monotonic()
    if _cf_blocked_cache is not None and (now - _cf_blocked_at) < _CF_CACHE_TTL:
        return _cf_blocked_cache

    try:
        sess = _make_session()
        resp = sess.get(
            "https://www.cardmarket.com/en/OnePiece/Products/Search"
            "?searchString=test&idGame=17&view=list&site=1",
            headers=_HEADERS,
            timeout=10,
        )
        blocked = _is_cf_block(resp.status_code, resp.text)
    except Exception:
        blocked = True  # network error → treat as blocked

    _cf_blocked_cache = blocked
    _cf_blocked_at    = now
    return blocked


def clear_cf_cache() -> None:
    """Invalidate the CF status cache (call after a successful fetch)."""
    global _cf_blocked_cache
    _cf_blocked_cache = None


# ── HTTP session ──────────────────────────────────────────────────────────────

def _make_session(warmup: bool = False) -> object:
    if _HAS_CFFI:
        sess = cffi_requests.Session(impersonate="chrome136")
    else:
        import requests
        sess = requests.Session()
        logger.warning("curl_cffi not installed; install it for better results")

    # Primary: auto-import from Arc browser
    # __cf_bm and _cfuvid are bound to the TLS session that created them —
    # sending them from a different client causes CF to reject the request.
    _SESSION_BOUND = {"__cf_bm", "_cfuvid"}
    arc_cookies = auto_import_arc_cookies()
    if arc_cookies:
        for name, value in arc_cookies.items():
            if name not in _SESSION_BOUND:
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
        clear_cf_cache()  # successful fetch → CF no longer blocking
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

    # Image — prefer og:image; fall back to first product img on page
    img_src = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = og["content"]
        if "logos/cardmarket" not in src and "cardmarket-logo" not in src:
            img_src = src
    if not img_src:
        # Booster boxes / sealed: og:image returns generic logo; real image is
        # in <img class="is-front"> inside <div class="image">
        prod_img = soup.select_one("div.image img.is-front")
        if prod_img:
            src = prod_img.get("src") or prod_img.get("data-echo") or ""
            if src and "logos/cardmarket" not in src:
                img_src = src
    if img_src:
        prices["img"] = img_src

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
    condition: str = None,
) -> dict:
    """
    Fetch CardMarket prices for any One Piece product.

    known_url:  previously cached CM product URL — tried first, skips slug/search.
    condition:  item condition (M/NM/LP/MP/HP/PL) — injects minCondition into URL
                so prices reflect listings at that condition or better.
    Returns: {trend, low, market, url, error}
    """
    result: dict = {
        "trend": None, "low": None, "market": None,
        "url": None, "img": None, "error": None,
    }
    session = _make_session()

    _is_lang_filtered = language is not None

    # Normalise cached URL: strip stale condition/language params, re-inject correct ones.
    # minCondition=1 returns Mint-only listings → inflated "low" for NM/LP cards.
    if known_url:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        from optcg.scrapers.slugs import LANGUAGE_CM_CODES as _LANG_CODES
        from optcg.scrapers.slugs import CONDITION_CM_CODES as _COND_CODES
        _p  = urlparse(known_url)
        _qs = {k: v for k, v in parse_qs(_p.query).items()
               if k.lower() not in ("mincondition", "maxcondition", "language")}
        if language and language.upper() in _LANG_CODES:
            _qs["language"] = [str(_LANG_CODES[language.upper()])]
        if condition and condition.upper() in _COND_CODES:
            _qs["minCondition"] = [str(_COND_CODES[condition.upper()])]
        known_url = urlunparse(_p._replace(query=urlencode(_qs, doseq=True)))

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
    # Skip when known_url was provided: falling back to a generic search would
    # overwrite the user's pinned product URL with a random (often cheaper) hit.
    if known_url:
        result["error"] = "Cached URL returned no prices — clear it with: optcg price set-url <id> <url>"
        return result

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
