from typing import Optional
from optcg.db import Database


def get_latest_snapshot(item_id: int, db: Database) -> Optional[dict]:
    """Most recent price snapshot for an item (any source/type)."""
    row = db.fetchone(
        "SELECT price, price_type, source, fetched_at, url "
        "FROM price_snapshots WHERE item_id = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (item_id,),
    )
    return dict(row) if row else None


def get_price_history(item_id: int, db: Database, limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        "SELECT price, price_type, source, fetched_at, url "
        "FROM price_snapshots WHERE item_id = ? "
        "ORDER BY fetched_at DESC LIMIT ?",
        (item_id, limit),
    )
    return [dict(r) for r in rows]


def item_pnl(item_row, db: Database) -> dict:
    cost = item_row["purchase_price"]
    snap = get_latest_snapshot(item_row["id"], db)
    current = snap["price"] if snap else None
    if current is None:
        return {
            "cost": cost, "current": None,
            "pnl": None, "pnl_pct": None,
            "price_source": None, "price_date": None,
        }
    pnl = current - cost
    pnl_pct = (pnl / cost * 100) if cost else 0.0
    return {
        "cost": cost,
        "current": current,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "price_source": snap.get("source"),
        "price_date": (snap.get("fetched_at") or "")[:10],
    }


def portfolio_summary(db: Database) -> dict:
    items = db.fetchall("SELECT * FROM items")
    total_cost = 0.0
    total_current = 0.0
    priced = 0
    unpriced = 0
    by_type: dict[str, dict] = {}
    by_set: dict[str, dict] = {}

    for item in items:
        cost = item["purchase_price"]
        total_cost += cost

        snap = get_latest_snapshot(item["id"], db)
        if snap:
            total_current += snap["price"]
            priced += 1
        else:
            total_current += cost   # assume break-even for items without price
            unpriced += 1

        t = item["item_type"]
        s = item["set_code"] or "—"
        by_type.setdefault(t, {"cost": 0.0, "count": 0})
        by_type[t]["cost"] += cost
        by_type[t]["count"] += 1
        by_set.setdefault(s, {"cost": 0.0, "count": 0})
        by_set[s]["cost"] += cost
        by_set[s]["count"] += 1

    pnl = total_current - total_cost
    pnl_pct = (pnl / total_cost * 100) if total_cost else 0.0

    return {
        "total_invested": total_cost,
        "total_current_value": total_current,
        "total_pnl": pnl,
        "total_pnl_pct": pnl_pct,
        "item_count": len(items),
        "items_with_price": priced,
        "items_missing_price": unpriced,
        "by_type": by_type,
        "by_set": by_set,
    }
