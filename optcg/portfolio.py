from typing import Optional
from optcg.db import Database


def get_latest_snapshot(item_id: int, db: Database) -> Optional[dict]:
    """Most recent price snapshot for an item (any source/type)."""
    row = db.fetchone(
        "SELECT price, price_type, source, fetched_at, url "
        "FROM price_snapshots WHERE item_id = ? "
        "ORDER BY "
        "  CASE source "
        "    WHEN 'manual'      THEN 0 "
        "    WHEN 'cardmarket'  THEN 1 "
        "    ELSE                    2 "   # ebay_sold / anything else
        "  END ASC, "
        "  CASE price_type "
        "    WHEN 'low'    THEN 0 "   # cheapest listing (filtered by language + condition)
        "    WHEN 'trend'  THEN 1 "   # 30-day average fallback
        "    WHEN 'market' THEN 2 "
        "    ELSE               3 "
        "  END ASC, "
        "  fetched_at DESC, id DESC LIMIT 1",
        (item_id,),
    )
    return dict(row) if row else None


def get_price_history(item_id: int, db: Database, limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        "SELECT price, price_type, source, fetched_at, url "
        "FROM price_snapshots WHERE item_id = ? "
        "ORDER BY fetched_at DESC, id DESC LIMIT ?",
        (item_id, limit),
    )
    return [dict(r) for r in rows]


def item_pnl(item_row, db: Database) -> dict:
    cost   = item_row["purchase_price"]
    status = item_row["status"] if "status" in item_row.keys() else "owned"

    # Sold items: use the actual sell price — no snapshot needed
    if status == "sold":
        sell_price = item_row["sell_price"] if "sell_price" in item_row.keys() else None
        if sell_price is not None:
            pnl     = sell_price - cost
            pnl_pct = (pnl / cost * 100) if cost else 0.0
            return {
                "cost": cost, "current": sell_price,
                "pnl": pnl, "pnl_pct": pnl_pct,
                "price_source": "sold",
                "price_date": (item_row["sell_date"] if "sell_date" in item_row.keys() else None) or "",
            }
        # Sold but sell_price not recorded yet — treat as unpriced
        return {"cost": cost, "current": None, "pnl": None, "pnl_pct": None,
                "price_source": None, "price_date": None}

    # Owned / pending: use latest market snapshot
    snap    = get_latest_snapshot(item_row["id"], db)
    current = snap["price"] if snap else None
    if current is None:
        return {"cost": cost, "current": None, "pnl": None, "pnl_pct": None,
                "price_source": None, "price_date": None}
    pnl     = current - cost
    pnl_pct = (pnl / cost * 100) if cost else 0.0
    return {
        "cost": cost, "current": current,
        "pnl": pnl, "pnl_pct": pnl_pct,
        "price_source": snap.get("source"),
        "price_date": (snap.get("fetched_at") or "")[:10],
    }


def portfolio_summary(db: Database) -> dict:
    items = db.fetchall("SELECT * FROM items")

    active_cost    = 0.0   # owned + pending
    active_current = 0.0
    realized_cost  = 0.0   # sold items — capital deployed
    realized_rev   = 0.0   # sell_price received
    priced         = 0
    unpriced       = 0
    sold_count     = 0
    by_type: dict[str, dict] = {}
    by_set:  dict[str, dict] = {}

    for item in items:
        cost   = item["purchase_price"]
        status = item["status"] if "status" in item.keys() else "owned"

        if status == "sold":
            sell_price = item["sell_price"] if "sell_price" in item.keys() else None
            realized_cost += cost
            realized_rev  += sell_price or cost   # fallback to cost if unrecorded
            sold_count    += 1
            # Sold items excluded from allocation (no longer in portfolio)
            continue

        # Active (owned / pending)
        active_cost += cost
        snap = get_latest_snapshot(item["id"], db)
        if snap:
            active_current += snap["price"]
            priced += 1
        else:
            active_current += cost   # break-even assumption
            unpriced += 1

        t = item["item_type"]
        s = item["set_code"] or "—"
        by_type.setdefault(t, {"cost": 0.0, "count": 0})
        by_type[t]["cost"]  += cost
        by_type[t]["count"] += 1
        by_set.setdefault(s, {"cost": 0.0, "count": 0})
        by_set[s]["cost"]  += cost
        by_set[s]["count"] += 1

    unrealized_pnl     = active_current - active_cost
    unrealized_pnl_pct = (unrealized_pnl / active_cost * 100) if active_cost else 0.0
    realized_pnl       = realized_rev - realized_cost
    realized_pnl_pct   = (realized_pnl / realized_cost * 100) if realized_cost else 0.0
    total_cost         = active_cost + realized_cost
    total_current      = active_current + realized_rev
    total_pnl          = unrealized_pnl + realized_pnl
    total_pnl_pct      = (total_pnl / total_cost * 100) if total_cost else 0.0

    return {
        # Active portfolio
        "active_invested":     active_cost,
        "active_current_value": active_current,
        "unrealized_pnl":      unrealized_pnl,
        "unrealized_pnl_pct":  unrealized_pnl_pct,
        # Sold
        "realized_pnl":        realized_pnl,
        "realized_pnl_pct":    realized_pnl_pct,
        "realized_invested":   realized_cost,
        "realized_revenue":    realized_rev,
        "sold_count":          sold_count,
        # Totals (all items ever)
        "total_invested":      total_cost,
        "total_current_value": total_current,
        "total_pnl":           total_pnl,
        "total_pnl_pct":       total_pnl_pct,
        "item_count":          len(items) - sold_count,   # active count
        "total_item_count":    len(items),
        "items_with_price":    priced,
        "items_missing_price": unpriced,
        "by_type":             by_type,
        "by_set":              by_set,
    }
