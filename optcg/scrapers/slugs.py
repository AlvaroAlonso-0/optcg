from __future__ import annotations

import re
from urllib.parse import urlencode

# ── CardMarket set slugs ──────────────────────────────────────────────────────
# Key: internal set code  Value: CardMarket URL slug

SET_SLUGS: dict[str, str] = {
    # Main sets
    "OP-01": "ONE-PIECE-CARD-GAME-Romance-Dawn",
    "OP-02": "ONE-PIECE-CARD-GAME-Paramount-War",
    "OP-03": "ONE-PIECE-CARD-GAME-Pillars-of-Strength",
    "OP-04": "ONE-PIECE-CARD-GAME-Kingdoms-of-Intrigue",
    "OP-05": "ONE-PIECE-CARD-GAME-Awakening-of-the-New-Era",
    "OP-06": "ONE-PIECE-CARD-GAME-Wings-of-the-Captain",
    "OP-07": "ONE-PIECE-CARD-GAME-500-Years-in-the-Future",
    "OP-08": "ONE-PIECE-CARD-GAME-Two-Legends",
    "OP-09": "ONE-PIECE-CARD-GAME-The-Four-Emperors",
    "OP-10": "ONE-PIECE-CARD-GAME-Emperors-in-the-New-World",
    "OP-11": "ONE-PIECE-CARD-GAME-Mighty-Enemies",
    "OP-12": "ONE-PIECE-CARD-GAME-Side-Quest-The-Wings-of-Straw-Hat",
    "OP-13": "ONE-PIECE-CARD-GAME-Hero-of-Justice",
    "OP-14": "ONE-PIECE-CARD-GAME-The-Azure-Seas-Seven",
    "OP-15": "ONE-PIECE-CARD-GAME-Adventure-on-Kamis-Island",
    "OP-16": "ONE-PIECE-CARD-GAME-The-Time-of-Battle",
    "OP-17": "ONE-PIECE-CARD-GAME-The-Worlds-Strongest-Warriors",
    # Extra / Premium boosters
    "EB-01": "ONE-PIECE-CARD-GAME-Extra-Booster-Memorial-Collection",
    "PRB-01": "ONE-PIECE-CARD-GAME-Premium-Booster-THE-BEST",
    # Starter decks
    "ST-01": "ONE-PIECE-CARD-GAME-Starter-Deck-Straw-Hat-Crew",
    "ST-02": "ONE-PIECE-CARD-GAME-Starter-Deck-Worst-Generation",
    "ST-03": "ONE-PIECE-CARD-GAME-Starter-Deck-The-Seven-Warlords-of-the-Sea",
    "ST-04": "ONE-PIECE-CARD-GAME-Starter-Deck-Animal-Kingdom-Pirates",
    "ST-05": "ONE-PIECE-CARD-GAME-Starter-Deck-Film-Edition",
    "ST-06": "ONE-PIECE-CARD-GAME-Starter-Deck-Absolute-Justice",
    "ST-07": "ONE-PIECE-CARD-GAME-Starter-Deck-500-Years-in-the-Future",
    "ST-08": "ONE-PIECE-CARD-GAME-Starter-Deck-Four-Emperors",
    "ST-09": "ONE-PIECE-CARD-GAME-Starter-Deck-Yamato",
    "ST-10": "ONE-PIECE-CARD-GAME-Starter-Deck-UTA",
    "ST-12": "ONE-PIECE-CARD-GAME-Starter-Deck-Zoro-Sanji",
    "ST-13": "ONE-PIECE-CARD-GAME-Ultra-Deck-The-Three-Captains",
    "ST-14": "ONE-PIECE-CARD-GAME-3D2Y",
    "ST-15": "ONE-PIECE-CARD-GAME-Red-Edward-Newgate",
    "ST-16": "ONE-PIECE-CARD-GAME-Blue-Donquixote-Doflamingo",
    "ST-17": "ONE-PIECE-CARD-GAME-Green-Sabo",
    "ST-18": "ONE-PIECE-CARD-GAME-Yellow-Charlotte-Katakuri",
    "ST-19": "ONE-PIECE-CARD-GAME-Purple-Monkey-D-Dragon",
    "ST-20": "ONE-PIECE-CARD-GAME-Black-Gecko-Moria",
}

# ── CardMarket condition codes ────────────────────────────────────────────────
# Used as ?minCondition=N — shows listings at this condition OR BETTER.
# CardMarket: 1=Mint, 2=NM, 3=Excellent, 4=Good, 5=Light Played, 6=Played, 7=Poor

CONDITION_CM_CODES: dict[str, int] = {
    "M":  1,   # Mint
    "NM": 2,   # Near Mint
    "LP": 3,   # Light Played  → CM "Excellent"
    "MP": 4,   # Moderately Played → CM "Good"
    "HP": 5,   # Heavily Played → CM "Light Played"
    "PL": 6,   # Poor / Played → CM "Played"
}

# ── CardMarket language codes ─────────────────────────────────────────────────
# https://www.cardmarket.com/en/Magic/Help/LanguageCodes (same across games)

LANGUAGE_CM_CODES: dict[str, int] = {
    "EN":   1,
    "FR":   2,
    "DE":   3,
    "ES":   4,
    "IT":   5,
    "PT":   8,
    "JP":   7,
    "ZH":   6,    # bare "Chinese" → treat as Simplified (mainland, most common)
    "ZH-S": 6,    # Chinese Simplified
    "ZH-T": 11,   # Chinese Traditional (Taiwan market)
    "KR":   10,
}

CM_BASE = "https://www.cardmarket.com/en/OnePiece"


def name_to_slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s\-]", "", name)
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug


def card_url(set_code: str, card_name: str, language: str = None) -> str | None:
    set_slug = SET_SLUGS.get(set_code.upper())
    if not set_slug:
        return None
    card_slug = name_to_slug(card_name)
    url = f"{CM_BASE}/Products/Singles/{set_slug}/{card_slug}"
    if language and language.upper() in LANGUAGE_CM_CODES:
        url += f"?language={LANGUAGE_CM_CODES[language.upper()]}"
    return url


def box_url(set_code: str, box_name: str, language: str = None) -> str | None:
    # CardMarket uses "OP17-Booster-Box" style slugs (no hyphen between prefix
    # and number) for main-series booster boxes, regardless of item display name.
    import re as _re
    m = _re.match(r'^([A-Z]+)-?(\d+)$', (set_code or "").upper().strip())
    if m:
        box_slug = f"{m.group(1)}{m.group(2)}-Booster-Box"
    else:
        box_slug = name_to_slug(box_name)
    url = f"{CM_BASE}/Products/Booster-Boxes/{box_slug}"
    if language and language.upper() in LANGUAGE_CM_CODES:
        url += f"?language={LANGUAGE_CM_CODES[language.upper()]}"
    return url


def sealed_url(product_name: str, language: str = None) -> str:
    slug = name_to_slug(product_name)
    url = f"{CM_BASE}/Products/Sealed-Products/{slug}"
    if language and language.upper() in LANGUAGE_CM_CODES:
        url += f"?language={LANGUAGE_CM_CODES[language.upper()]}"
    return url


def search_url(query: str, language: str = None) -> str:
    params: dict = {"searchString": query}
    if language and language.upper() in LANGUAGE_CM_CODES:
        params["language[0]"] = LANGUAGE_CM_CODES[language.upper()]
    return f"{CM_BASE}/Products/Search?{urlencode(params)}"
