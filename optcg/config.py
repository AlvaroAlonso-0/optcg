from pathlib import Path

# Config dir lives locally (not in iCloud — no need to sync credentials)
CONFIG_DIR  = Path.home() / ".config" / "optcg"
CONFIG_FILE = CONFIG_DIR / "config.json"

ICLOUD_BASE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
APP_DIR     = ICLOUD_BASE / "OnePieceTCG"
DB_PATH     = APP_DIR / "tracker.db"
RECEIPTS_DIR = APP_DIR / "receipts"
EXPORTS_DIR  = APP_DIR / "exports"

CACHE_TTL_HOURS = 24

LANGUAGES = ["EN", "JP", "ZH", "ZH-T", "ZH-S", "ES", "FR", "DE", "PT", "IT", "KR"]
CONDITIONS = ["M", "NM", "LP", "MP", "HP", "PL"]
ITEM_TYPES = ["card", "promo", "blister", "booster_box", "sealed_set"]
GRADING_COMPANIES = ["PSA", "BGS", "CGC", "SGC", "TAG"]
