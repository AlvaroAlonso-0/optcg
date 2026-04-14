"""
CardMarket card search + interactive terminal picker.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

from optcg.scrapers.cardmarket import _make_session, _HEADERS, _is_cf_block
from optcg.scrapers.slugs import LANGUAGE_CM_CODES, CM_BASE


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class CardResult:
    name: str
    set_slug: str          # e.g. "Promos", "ONE-PIECE-CARD-GAME-Romance-Dawn"
    card_number: str       # e.g. "OP01-001", "P-043"
    variant: str           # e.g. "V.1", "V.3", ""
    price_from: str        # e.g. "6,99 €"
    image_url: str
    cm_url: str            # full cardmarket URL
    item_type: str = "card"  # "card" or "sealed" etc.


# ── Scrape search results ─────────────────────────────────────────────────────

_IMG_HEADERS = {
    "User-Agent":      (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept":          "image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer":         "https://www.cardmarket.com/",
    "Sec-Fetch-Dest":  "image",
    "Sec-Fetch-Mode":  "no-cors",
    "Sec-Fetch-Site":  "cross-site",
}


SORT_OPTIONS = {
    "popular":   "popularity_desc",
    "cheap":     "price_asc",
    "expensive": "price_desc",
    "name":      "name_asc",
    "number":    "collectorsnumber_asc",
    "new":       "date_desc",
    "old":       "date_asc",
}


def search_cardmarket(
    query: str,
    language: str = None,
    sort: str = "popular",
    page: int = 1,
) -> list[CardResult]:
    """
    Search CardMarket for One Piece singles matching *query*.

    sort: one of SORT_OPTIONS keys (popular, cheap, expensive, name, number, new, old)
    page: 1-based page number (30 results per page)
    """
    sort_val = SORT_OPTIONS.get(sort, "popularity_desc")
    params = (
        f"searchString={query.replace(' ', '+')}"
        f"&idGame=17&view=list"
        f"&sortBy={sort_val}"
        f"&site={page}"
    )
    if language and language.upper() in LANGUAGE_CM_CODES:
        params += f"&language%5B0%5D={LANGUAGE_CM_CODES[language.upper()]}"
    url = f"{CM_BASE}/Products/Search?{params}"

    sess = _make_session()
    resp = sess.get(url, headers=_HEADERS, timeout=20)
    if _is_cf_block(resp.status_code, resp.text):
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[CardResult] = []

    for a in soup.select("a.galleryBox"):
        href = a.get("href", "")
        href_parts = href.rstrip("/").split("/")
        set_slug = href_parts[-2] if len(href_parts) >= 2 else ""

        img_tag = a.find("img")
        image_url = img_tag.get("data-echo", "") if img_tag else ""

        title_el = a.select_one(".card-title")
        if title_el:
            for span in title_el.select("span"):
                span.decompose()
            raw_name = title_el.get_text(strip=True)
        else:
            raw_name = ""

        num_match = re.search(r"\(([A-Z0-9\-]+)\)", raw_name)
        card_number = num_match.group(1) if num_match else ""
        var_match = re.search(r"\(V\.(\S+)\)", raw_name)
        variant = f"V.{var_match.group(1)}" if var_match else ""

        clean_name = re.sub(r"\s*\([^)]+\)\s*$", "", raw_name).strip()
        clean_name = re.sub(r"\s*\([^)]+\)\s*$", "", clean_name).strip()
        clean_name = clean_name.replace(".", ". ").replace(".  ", ". ").strip()
        clean_name = re.sub(r"\s+", " ", clean_name)

        price_el = a.select_one(".card-text b")
        price_from = price_el.get_text(strip=True) if price_el else ""

        full_url = f"https://www.cardmarket.com{href}" if href.startswith("/") else href

        results.append(CardResult(
            name=clean_name,
            set_slug=set_slug,
            card_number=card_number,
            variant=variant,
            price_from=price_from,
            image_url=image_url,
            cm_url=full_url,
        ))

    return results


# ── Terminal inline images ────────────────────────────────────────────────────

def _supports_inline_images() -> bool:
    """True if the terminal can display inline images."""
    tp = os.environ.get("TERM_PROGRAM", "")
    term = os.environ.get("TERM", "")
    return (
        tp in ("iTerm.app", "WezTerm")
        or "kitty" in term
        or os.environ.get("KITTY_WINDOW_ID") is not None
    )


def _image_url_from_page(cm_url: str) -> Optional[str]:
    """
    Fetch the CardMarket product page and extract the first non-lazy image URL.
    The detail page has real `src=` attributes (not transparent.gif) for the
    primary card image, and those S3 paths are publicly accessible.
    """
    try:
        soup = BeautifulSoup(
            _make_session().get(cm_url, headers=_HEADERS, timeout=15).text,
            "lxml",
        )
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "product-images.s3.cardmarket.com" in src:
                return src
    except Exception:
        pass
    return None


def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Fetch raw image bytes from a publicly accessible S3 URL."""
    if not url:
        return None
    try:
        from curl_cffi import requests as cffi_requests
        sess = cffi_requests.Session(impersonate="chrome133a")
        r = sess.get(url, headers=_IMG_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def fetch_card_image_bytes(result: "CardResult") -> Optional[bytes]:
    """
    Return image bytes for a CardResult.
    First tries the search-result image URL; if blocked, falls back to
    extracting the real URL from the card detail page.
    """
    # Try direct (works for newer sets / Promos that have public S3 access)
    if result.image_url:
        data = _fetch_image_bytes(result.image_url)
        if data:
            return data
    # Fallback: get real image URL from the card detail page
    real_url = _image_url_from_page(result.cm_url)
    if real_url:
        return _fetch_image_bytes(real_url)
    return None


def _write_to_tty(payload: str) -> None:
    """Write raw bytes directly to the terminal, bypassing any stdout capture."""
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(payload)
            tty.flush()
    except Exception:
        sys.stdout.write(payload)
        sys.stdout.flush()


def _iterm2_inline(data: bytes, width_cols: int = 24) -> str:
    b64 = base64.b64encode(data).decode()
    return (
        f"\x1b]1337;File=inline=1;width={width_cols};"
        f"preserveAspectRatio=1:{b64}\x07\n"
    )


def show_card_image(image_url: str, width_cols: int = 24) -> bool:
    """Print a card image inline. Returns True if shown, False if not supported."""
    if not _supports_inline_images():
        return False
    data = _fetch_image_bytes(image_url)
    if not data:
        return False
    _write_to_tty(_iterm2_inline(data, width_cols))
    return True


def show_card_image_result(result: "CardResult", width_cols: int = 28) -> bool:
    """Fetch and display a card image, trying detail page fallback if needed."""
    if not _supports_inline_images():
        return False
    data = fetch_card_image_bytes(result)
    if not data:
        return False
    _write_to_tty(_iterm2_inline(data, width_cols))
    return True


# ── Interactive picker ────────────────────────────────────────────────────────

def _print_results_table(
    console, results: list[CardResult], page: int, sort: str
) -> None:
    from rich.table import Table
    from rich import box as rbox

    t = Table(box=rbox.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False, collapse_padding=True)
    t.add_column("#",       style="dim",        width=3,  no_wrap=True)
    t.add_column("Name",    style="bold white", min_width=24)
    t.add_column("Number",  style="cyan",       width=13, no_wrap=True)
    t.add_column("Set",     style="dim",        min_width=14)
    t.add_column("Var",     style="magenta",    width=5,  no_wrap=True)
    t.add_column("From",    style="green",      width=10, no_wrap=True)

    for i, r in enumerate(results, 1):
        set_display = (
            r.set_slug.replace("-", " ")
            .replace("ONE PIECE CARD GAME ", "")
            [:28]
        )
        t.add_row(str(i), r.name[:36], r.card_number,
                  set_display, r.variant, r.price_from)

    console.print(t)
    console.print(
        f"[dim]Page {page} · sort: {sort} · {len(results)} result(s)"
        f"  |  sort: {', '.join(SORT_OPTIONS.keys())}[/dim]"
    )


def pick_card(
    query: str,
    language: str = None,
    sort: str = "popular",
) -> Optional[CardResult]:
    """
    Search CardMarket for *query* and let the user interactively pick one result.
    Supports pagination (n=next page) and re-sort.
    Returns the selected CardResult, or None if cancelled / no results.
    """
    from rich.console import Console
    import questionary

    console = Console()
    page = 1

    while True:
        with console.status(
            f"[dim]Searching [bold]{query}[/bold]  "
            f"[page {page}, sort: {sort}]…"
        ):
            results = search_cardmarket(query, language, sort=sort, page=page)

        if not results:
            if page > 1:
                console.print("[yellow]No more results.[/yellow]")
                page -= 1
                continue
            console.print("[yellow]No results found.[/yellow]")
            return None

        _print_results_table(console, results, page, sort)

        # ── Questionary select ────────────────────────────────────────────────
        nav_choices = [
            questionary.Choice(title="▶  Next page", value="next"),
            questionary.Choice(title="◀  Prev page", value="prev"),
        ] if page > 1 else [
            questionary.Choice(title="▶  Next page", value="next"),
        ]

        choices = [
            questionary.Choice(
                title=(
                    f"{r.card_number:13s}  {r.name[:32]:32s}"
                    f"  {r.variant:5s}  {r.price_from}"
                ),
                value=i,
            )
            for i, r in enumerate(results, 1)
        ] + nav_choices + [questionary.Choice(title="✕  Cancel", value=0)]

        chosen = questionary.select(
            "Select card (or navigate):",
            choices=choices,
            use_shortcuts=False,
        ).ask()

        if chosen is None or chosen == 0:
            return None
        if chosen == "next":
            page += 1
            continue
        if chosen == "prev" and page > 1:
            page -= 1
            continue

        selected = results[chosen - 1]
        break

    # ── Show image ────────────────────────────────────────────────────────────
    if _supports_inline_images():
        with console.status("[dim]Loading card image…"):
            img_data = fetch_card_image_bytes(selected)
        # Write after status exits to avoid escape seq mangling by rich live display
        if img_data:
            _write_to_tty(_iterm2_inline(img_data, width_cols=28))
        elif selected.image_url:
            console.print(f"[dim]Image: {selected.image_url}[/dim]")

    console.print(
        f"[green]✓[/green] Selected: [bold]{selected.name}[/bold]"
        f"  [cyan]{selected.card_number}[/cyan]"
        f"  [dim]{selected.set_slug}[/dim]"
    )
    return selected
