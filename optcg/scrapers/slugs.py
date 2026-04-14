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
    "ZH":   10,   # Chinese Traditional (Taiwan market)
    "ZH-T": 10,
    "ZH-S": 11,   # Chinese Simplified
    "KR":   9,
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


def box_url(set_code: str, box_name: str) -> str | None:
    box_slug = name_to_slug(box_name)
    return f"{CM_BASE}/Products/Booster-Boxes/{box_slug}"


def sealed_url(product_name: str) -> str:
    slug = name_to_slug(product_name)
    return f"{CM_BASE}/Products/Sealed-Products/{slug}"


def search_url(query: str, language: str = None) -> str:
    params: dict = {"searchString": query}
    if language and language.upper() in LANGUAGE_CM_CODES:
        params["language[0]"] = LANGUAGE_CM_CODES[language.upper()]
    return f"{CM_BASE}/Products/Search?{urlencode(params)}"
