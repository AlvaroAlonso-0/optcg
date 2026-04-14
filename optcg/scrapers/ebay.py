"""
eBay.com scraper for One Piece TCG price comparison and deal hunting.

Uses eBay.com (international) — best for price discovery since it has the
largest global market. Sold/completed listings give realistic comps;
active listings reveal current buy opportunities.
"""
import logging
import re
import time
from typing import Optional
from urllib.parse import urlencode

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    _HAS_CFFI = False

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_LANG_KEYWORDS: dict[str, str] = {
    "JP":   "Japanese",
    "ZH":   "Chinese",
    "ZH-T": "Chinese",
    "ZH-S": "Chinese Simplified",
    "ES":   "Spanish",
    "FR":   "French",
    "DE":   "German",
    "PT":   "Portuguese",
    "IT":   "Italian",
    "KR":   "Korean",
}


def _session():
    if _HAS_CFFI:
        return cffi_requests.Session(impersonate="chrome120")
    return cffi_requests.Session()


def _parse_price(text: str) -> Optional[float]:
    """Parse eBay price strings: '$45.99', 'EUR 45,99', 'US $1,234.56'."""
    if not text:
        return None
    # Strip currency symbols / codes
    cleaned = re.sub(r"[€$£¥]", "", text)
    cleaned = re.sub(r"\b(EUR|USD|GBP|JPY|CNY|CHF)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", "", cleaned).strip()
    if not cleaned:
        return None
    # Distinguish European vs US formats
    if re.fullmatch(r"\d{1,3}(,\d{3})*\.\d{2}", cleaned):
        # US: 1,234.56
        cleaned = cleaned.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})*,\d{2}", cleaned):
        # EU: 1.234,56
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned and "." not in cleaned:
        # Single comma — assume decimal separator
        cleaned = cleaned.replace(",", ".")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _build_query(
    name: str,
    set_code: str = None,
    card_number: str = None,
    language: str = None,
    graded: bool = False,
    grading_company: str = None,
) -> str:
    parts = ["One Piece"]
    if card_number:
        parts.append(card_number)
    parts.append(name)
    lang_kw = _LANG_KEYWORDS.get((language or "").upper())
    if lang_kw:
        parts.append(lang_kw)
    if graded and grading_company:
        parts.append(grading_company)
    return " ".join(parts)


def _fetch(url: str, session) -> Optional[str]:
    try:
        resp = session.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        time.sleep(0.8)
        return resp.text
    except Exception as exc:
        logger.error("eBay fetch error: %s  url=%s", exc, url)
        return None


def _parse_listing(item) -> Optional[dict]:
    """Parse a single .s-item element."""
    title_el    = item.select_one(".s-item__title")
    price_el    = item.select_one(".s-item__price")
    link_el     = item.select_one(".s-item__link")
    ship_el     = item.select_one(".s-item__shipping, .s-item__logisticsCost")
    date_el     = item.select_one(".s-item__ended-date, .POSITIVE, .s-item__caption--signal")
    loc_el      = item.select_one(".s-item__location")

    if not title_el or not price_el:
        return None
    title = title_el.get_text(strip=True)
    if "Shop on eBay" in title:
        return None

    price_text = price_el.get_text(strip=True)
    # Range prices ("$10.00 to $20.00") — take the lower bound
    if " to " in price_text.lower():
        price_text = price_text.split(" to ")[0]

    price = _parse_price(price_text)
    if not price:
        return None

    shipping_cost = 0.0
    if ship_el:
        ship_text = ship_el.get_text(strip=True)
        if "free" not in ship_text.lower():
            ship_price = _parse_price(ship_text)
            if ship_price:
                shipping_cost = ship_price

    return {
        "title":    title,
        "price":    price,
        "shipping": shipping_cost,
        "total":    price + shipping_cost,
        "url":      link_el.get("href") if link_el else None,
        "date":     date_el.get_text(strip=True) if date_el else None,
        "location": loc_el.get_text(strip=True) if loc_el else None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def search_sold_listings(
    query: str,
    max_results: int = 10,
) -> list[dict]:
    """
    Search eBay completed/sold listings.
    Returns list of {title, price, shipping, total, url, date}.
    """
    params = {
        "_nkw": query,
        "LH_Sold": "1",
        "LH_Complete": "1",
        "_sop": "13",     # sort: most recently ended
        "_ipg": "50",
    }
    url = f"https://www.ebay.com/sch/i.html?{urlencode(params)}"
    session = _session()
    html = _fetch(url, session)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.select(".s-item"):
        parsed = _parse_listing(item)
        if parsed:
            results.append(parsed)
        if len(results) >= max_results:
            break
    return results


def search_active_listings(
    query: str,
    max_results: int = 10,
    max_price: float = None,
) -> list[dict]:
    """
    Search eBay active listings sorted by lowest total price.
    Returns list of {title, price, shipping, total, url, location}.
    """
    params = {
        "_nkw": query,
        "_sop": "15",     # sort: lowest price + shipping first
        "_ipg": "50",
    }
    if max_price:
        params["_udhi"] = f"{max_price:.2f}"

    url = f"https://www.ebay.com/sch/i.html?{urlencode(params)}"
    session = _session()
    html = _fetch(url, session)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.select(".s-item"):
        parsed = _parse_listing(item)
        if parsed:
            results.append(parsed)
        if len(results) >= max_results:
            break
    return results


def find_deals(
    name: str,
    market_price: float,
    set_code: str = None,
    card_number: str = None,
    language: str = None,
    graded: bool = False,
    grading_company: str = None,
    discount_threshold: float = 0.15,
) -> list[dict]:
    """
    Find active eBay listings at least `discount_threshold` below `market_price`.
    Returns listings sorted by discount % descending, each including:
      price, shipping, total, url, discount_pct, market_price
    """
    query = _build_query(name, set_code, card_number, language, graded, grading_company)
    ceiling = market_price * (1.0 - discount_threshold)
    listings = search_active_listings(query, max_results=30, max_price=ceiling)

    deals = []
    for listing in listings:
        total = listing["total"]
        if total <= 0:
            continue
        discount_pct = (market_price - total) / market_price * 100
        if discount_pct >= discount_threshold * 100:
            deals.append({**listing, "discount_pct": discount_pct, "market_price": market_price})

    deals.sort(key=lambda x: x["discount_pct"], reverse=True)
    return deals


def sold_average(
    name: str,
    set_code: str = None,
    card_number: str = None,
    language: str = None,
) -> Optional[float]:
    """Returns the average of recent eBay sold prices (None if no data)."""
    query = _build_query(name, set_code, card_number, language)
    listings = search_sold_listings(query, max_results=10)
    if not listings:
        return None
    prices = [l["price"] for l in listings if l["price"]]
    return sum(prices) / len(prices) if prices else None
