# optcg — One Piece TCG Investment Tracker

CLI portfolio tracker for One Piece TCG cards, boxes, blisters, and promos.
Prices scraped live from **CardMarket** and **eBay**. Data syncs to **iCloud Drive**
so your portfolio is always on your phone.

---

## Requirements

- macOS (iCloud sync + Arc cookie extraction)
- Python 3.11+ (Homebrew recommended)
- [Arc browser](https://arc.net) (for automatic CardMarket login — optional but strongly recommended)

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/optcg.git
cd optcg
pip install -e . --break-system-packages
```

Verify:

```bash
optcg --version
```

---

## Quick start

```bash
# Add a card interactively (searches CardMarket, shows image)
optcg add card

# Add with full details
optcg add card -n "Monkey D. Luffy" -s OP-01 -l JP -p 120.00

# Add a graded slab
optcg add card -n "Shanks" -p 200.00 --graded --gc PSA --grade 10 --cert 12345678

# Add sealed product
optcg add box -n "Romance Dawn Booster Box" -s OP-01 -p 95.00

# List everything
optcg list
optcg list --sort pnl          # best P&L at top
optcg list --type card --lang JP

# Update prices (scrapes CardMarket + eBay)
optcg price update --all

# Portfolio P&L summary
optcg portfolio
optcg portfolio --by-set --by-type

# Search CardMarket
optcg search "Zoro" --sort cheap --image
optcg search "Luffy" --sort new --page 2

# Find deals (CardMarket vs eBay comparison)
optcg deals search "Monkey D. Luffy" --discount 20
optcg deals portfolio --discount 15

# Watchlist
optcg watchlist add -n "Shanks" -s OP-01 --target 80.00
optcg watchlist check

# Attach a receipt (auto-renamed, stored in iCloud)
optcg receipt add 3 ~/Downloads/factura.pdf
```

---

## CardMarket setup

CardMarket uses Cloudflare protection. The easiest setup is **Arc browser**:

1. Install [Arc](https://arc.net) and open `cardmarket.com` once — it loads normally.
2. That's it. `optcg` reads cookies from Arc automatically. No manual steps.

**No Arc? Manual fallback:**

1. Open cardmarket.com in Chrome.
2. DevTools → Application → Cookies → `cf_clearance` → copy value.
3. `optcg config set-cookie <paste-value>`

Run `optcg config cookie-help` for detailed instructions.

---

## Dashboard (offline HTML file)

Generates a single self-contained `dashboard.html` with all your data baked in.
No server. Open it by double-clicking on Mac or tapping in Files app on iPhone.

```bash
optcg dashboard            # generate + open immediately
optcg dashboard --no-open  # just write the file to iCloud
```

The file is saved to **iCloud Drive → OnePieceTCG → exports/dashboard.html**
and regenerated automatically every time you run `optcg price update --all`.

On iPhone:
1. Open **Files** → iCloud Drive → OnePieceTCG → exports.
2. Tap `dashboard.html` — opens in Safari.

Features:
- **Overview** — invested / current value / P&L stat cards
- **Portfolio over time** — value vs invested line chart
- **Allocation** — donut by type or by set
- **Items** — search + filter by type + sort
- **Charts** — P&L per item bar chart
- **Watchlist** — target prices

---

## Phone features (iPhone / iPad)

Your portfolio lives in **iCloud Drive → OnePieceTCG** and syncs automatically.

### View your portfolio in Numbers

1. On iPhone, open the **Files** app.
2. Navigate to **iCloud Drive → OnePieceTCG → exports**.
3. Tap `portfolio.csv` — it opens in **Numbers** automatically.
4. You'll see all your items with current prices and P&L.
5. `price_history.csv` shows the full price timeline for every item.

> The CSV is regenerated every time you run `optcg price update --all` on your Mac.
> Pull down to refresh in Files after an update.

### View receipts (tax documents)

1. Files app → **iCloud Drive → OnePieceTCG → receipts**.
2. Each item has its own folder: `item_3/`, `item_7/`, etc.
3. Files are named `item_3_2026-01-15.pdf` — date = purchase date.
4. Tap any file to preview in Quick Look.

### Open the database directly (advanced)

The raw database is at `iCloud Drive → OnePieceTCG → tracker.db`.
Open it with any SQLite viewer app (e.g. **SQLiteViewer** on the App Store)
for custom queries and filters.

---

## All commands

```
optcg add card / promo / box / blister / sealed
optcg list [--type] [--set] [--lang] [--graded] [--sort]
optcg show <id>
optcg edit <id> [--price] [--condition] [--grade] ...
optcg remove <id>
optcg search <query> [--sort] [--page] [--image]
optcg price update [--all | -i <id>]
optcg price set <id> <price>
optcg portfolio [--by-set] [--by-type]
optcg deals search <query> [--discount] [--lang]
optcg deals portfolio [--discount] [--top]
optcg watchlist add / list / check / remove
optcg receipt add / list / open
optcg export csv
optcg config show / set-cookie / clear-cookie / cookie-help
optcg stats
optcg dashboard [--out path] [--no-open]
```

Run `optcg <command> -h` on any command for full options and examples.

---

## Data storage

| What | Where |
|------|-------|
| Database | `~/Library/Mobile Documents/com~apple~CloudDocs/OnePieceTCG/tracker.db` |
| Receipts | `…/OnePieceTCG/receipts/` |
| CSV exports | `…/OnePieceTCG/exports/` |
| Config / cookies | `~/.config/optcg/config.json` (local, not iCloud) |

The config file stores only a cached Arc key and an optional manual CF cookie.
It never leaves your machine and is excluded from git.

---

## Supported sets

OP-01 through OP-10 (more in `optcg/scrapers/slugs.py`), EB-01, PRB-01, ST-01 → ST-20.
If a set is missing, add its CardMarket slug to `SET_SLUGS` in `slugs.py`.

---

## License

MIT
