import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from optcg.config import EXPORTS_DIR
from optcg.db import Database
from optcg.portfolio import item_pnl


_PORTFOLIO_FIELDS = [
    "id", "type", "name", "set_code", "card_number", "language",
    "condition", "foil", "variant",
    "graded", "grading_company", "grade", "cert_number",
    "purchase_price_eur", "purchase_date", "purchase_source",
    "current_price_eur", "pnl_eur", "pnl_pct",
    "price_source", "price_last_updated",
    "notes",
]

_HISTORY_FIELDS = [
    "item_id", "name", "set_code", "card_number", "language",
    "source", "price_type", "price_eur", "fetched_at", "url",
]


def _date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def export_portfolio_csv(db: Database, path: Optional[Path] = None) -> Path:
    if path is None:
        path = EXPORTS_DIR / f"portfolio_{_date_str()}.csv"

    items = db.fetchall("SELECT * FROM items ORDER BY item_type, set_code, name")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_PORTFOLIO_FIELDS)
        writer.writeheader()
        for item in items:
            pnl = item_pnl(item, db)
            writer.writerow({
                "id":                  item["id"],
                "type":                item["item_type"],
                "name":                item["name"],
                "set_code":            item["set_code"] or "",
                "card_number":         item["card_number"] or "",
                "language":            item["language"] or "EN",
                "condition":           item["condition"] or "",
                "foil":                "Yes" if item["foil"] else "No",
                "variant":             item["variant"] or "",
                "graded":              "Yes" if item["graded"] else "No",
                "grading_company":     item["grading_company"] or "",
                "grade":               item["grade"] or "",
                "cert_number":         item["cert_number"] or "",
                "purchase_price_eur":  f"{item['purchase_price']:.2f}",
                "purchase_date":       item["purchase_date"],
                "purchase_source":     item["purchase_source"] or "",
                "current_price_eur":   f"{pnl['current']:.2f}" if pnl["current"] is not None else "",
                "pnl_eur":             f"{pnl['pnl']:.2f}" if pnl["pnl"] is not None else "",
                "pnl_pct":             f"{pnl['pnl_pct']:.1f}%" if pnl["pnl_pct"] is not None else "",
                "price_source":        pnl.get("price_source") or "",
                "price_last_updated":  pnl.get("price_date") or "",
                "notes":               item["notes"] or "",
            })

    return path


def export_price_history_csv(db: Database, path: Optional[Path] = None) -> Path:
    if path is None:
        path = EXPORTS_DIR / f"price_history_{_date_str()}.csv"

    rows = db.fetchall("""
        SELECT ps.item_id, i.name, i.set_code, i.card_number, i.language,
               ps.source, ps.price_type, ps.price, ps.fetched_at, ps.url
        FROM price_snapshots ps
        JOIN items i ON i.id = ps.item_id
        ORDER BY ps.fetched_at DESC
    """)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_HISTORY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "item_id":    row["item_id"],
                "name":       row["name"],
                "set_code":   row["set_code"] or "",
                "card_number": row["card_number"] or "",
                "language":   row["language"] or "",
                "source":     row["source"],
                "price_type": row["price_type"],
                "price_eur":  f"{row['price']:.2f}",
                "fetched_at": row["fetched_at"],
                "url":        row["url"] or "",
            })

    return path


def auto_export(db: Database) -> tuple[Path, Path]:
    """Export CSVs + regenerate the HTML dashboard to iCloud exports directory."""
    from optcg.export_html import generate_html
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    generate_html(db)
    return export_portfolio_csv(db), export_price_history_csv(db)
