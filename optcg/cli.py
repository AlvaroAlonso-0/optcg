"""
optcg — One Piece TCG Investment Tracker
"""
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from optcg.db import Database, db_conn, init_db
from optcg.config import EXPORTS_DIR, RECEIPTS_DIR, APP_DIR
from optcg.export import auto_export, export_portfolio_csv, export_price_history_csv
from optcg.portfolio import get_price_history, item_pnl, portfolio_summary
from optcg.search import (
    pick_card, search_cardmarket, show_card_image,
    show_card_image_result, _supports_inline_images, SORT_OPTIONS,
)

console = Console()


def _open_path(path: Path) -> None:
    """Open a file or directory with the OS default application (cross-platform)."""
    import platform
    sys = platform.system()
    if sys == "Darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys == "Windows":
        os.startfile(str(path))
    else:
        subprocess.run(["xdg-open", str(path)], check=False)

# ── Shared helpers ─────────────────────────────────────────────────────────────

_LANG_CHOICE   = click.Choice(
    ["EN", "JP", "ZH", "ZH-T", "ZH-S", "ES", "FR", "DE", "PT", "IT", "KR"],
    case_sensitive=False,
)
_COND_CHOICE   = click.Choice(["M", "NM", "LP", "MP", "HP", "PL"], case_sensitive=False)
_GRADE_CHOICE  = click.Choice(["PSA", "BGS", "CGC", "SGC", "TAG"], case_sensitive=False)
_TYPE_CHOICE   = click.Choice(["card", "promo", "blister", "booster_box", "sealed_set"])
_SORT_CHOICE   = click.Choice(["date", "name", "price", "pnl", "pct"])


def _fmt_pnl(pnl: Optional[float], pct: Optional[float] = None) -> str:
    if pnl is None:
        return "[dim]—[/dim]"
    color = "green" if pnl >= 0 else "red"
    sign  = "+" if pnl >= 0 else ""
    s = f"[{color}]{sign}{pnl:.2f}[/{color}]"
    if pct is not None:
        s += f" [dim]({sign}{pct:.1f}%)[/dim]"
    return s


def _fmt_grade(item_row) -> str:
    if item_row["graded"]:
        co = item_row["grading_company"] or ""
        gr = item_row["grade"] or ""
        return f"[bold yellow]{co} {gr}[/bold yellow]".strip()
    return item_row["condition"] or ""


def _insert_item(
    item_type: str, name: str, set_code: Optional[str], card_number: Optional[str],
    language: str, condition: Optional[str], foil: bool, variant: Optional[str],
    graded: bool, grading_company: Optional[str], grade: Optional[str],
    cert_number: Optional[str], price: float, purchase_date: str,
    source: Optional[str], notes: Optional[str],
    status: str = "owned",
) -> int:
    with db_conn() as conn:
        db = Database(conn)
        return db.lastrowid(
            """INSERT INTO items
               (item_type, name, set_code, card_number, language, condition,
                foil, variant, graded, grading_company, grade, cert_number,
                purchase_price, purchase_date, purchase_source, notes, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_type, name, set_code, card_number, language, condition,
             int(foil), variant, int(graded), grading_company, grade, cert_number,
             price, purchase_date, source, notes, status),
        )


def _insert_items(
    qty: int,
    item_type: str, name: str, set_code: Optional[str], card_number: Optional[str],
    language: str, condition: Optional[str], foil: bool, variant: Optional[str],
    graded: bool, grading_company: Optional[str], grade: Optional[str],
    cert_number: Optional[str], price: float, purchase_date: str,
    source: Optional[str], notes: Optional[str],
    status: str = "owned",
    cardmarket_url: Optional[str] = None,
    cardmarket_img: Optional[str] = None,
) -> list[int]:
    item_ids: list[int] = []
    with db_conn() as conn:
        db = Database(conn)
        for _ in range(max(1, qty)):
            iid = db.lastrowid(
                """INSERT INTO items
                   (item_type, name, set_code, card_number, language, condition,
                    foil, variant, graded, grading_company, grade, cert_number,
                    purchase_price, purchase_date, purchase_source, notes, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_type, name, set_code, card_number, language, condition,
                 int(foil), variant, int(graded), grading_company, grade, cert_number,
                 price, purchase_date, source, notes, status),
            )
            item_ids.append(iid)
            if cardmarket_url:
                db.execute("UPDATE items SET cardmarket_url = ? WHERE id = ?", (cardmarket_url, iid))
            if cardmarket_img:
                db.execute("UPDATE items SET cardmarket_img = ? WHERE id = ?", (cardmarket_img, iid))
    return item_ids


def _auto_update(item_id: int) -> None:
    """Fetch price for a single item and regenerate the dashboard. Called after add/edit."""
    from optcg.scrapers.cardmarket import get_card_prices
    from optcg.scrapers.ebay import search_sold_listings
    from optcg.export import auto_export
    try:
        with db_conn() as conn:
            db = Database(conn)
            item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
            if not item:
                return
            saved = False
            known_url = item["cardmarket_url"] if "cardmarket_url" in item.keys() else None
            cm = get_card_prices(item["name"], item["set_code"], item["card_number"],
                                 item["language"], item["item_type"], known_url=known_url,
                                 condition=item["condition"])
            for ptype in ("trend", "low", "market"):
                if cm.get(ptype):
                    db.execute(
                        "INSERT INTO price_snapshots (item_id,source,price_type,price,url) VALUES (?,?,?,?,?)",
                        (item["id"], "cardmarket", ptype, cm[ptype], cm.get("url")),
                    )
                    saved = True
            if cm.get("url"):
                db.execute("UPDATE items SET cardmarket_url = ? WHERE id = ?",
                           (cm["url"], item["id"]))
            if cm.get("img"):
                db.execute("UPDATE items SET cardmarket_img = ? WHERE id = ?",
                           (cm["img"], item["id"]))
            query = f"One Piece {item['card_number'] or ''} {item['name']}".strip()
            sold  = search_sold_listings(query, max_results=5)
            if sold:
                avg = sum(l["price"] for l in sold) / len(sold)
                db.execute(
                    "INSERT INTO price_snapshots (item_id,source,price_type,price) VALUES (?,?,?,?)",
                    (item["id"], "ebay", "sold_avg", avg),
                )
                saved = True
            auto_export(db)
        _SEALED = {"booster_box", "blister", "sealed_set"}
        is_sealed = item["item_type"] in _SEALED
        if saved:
            parts = []
            cm_p = cm.get("low") or cm.get("trend")
            lbl  = "CM low" if cm.get("low") else "CM trend"
            if cm_p:  parts.append(f"{lbl} {cm_p:.2f} €")
            if sold:  parts.append(f"eBay {avg:.2f} €")
            console.print(f"  [dim]Price: {'  ·  '.join(parts)}  · dashboard updated[/dim]")
        else:
            err = cm.get("error") or "no data found"
            console.print(
                f"  [dim yellow]Price fetch failed: {err}\n"
                f"  → optcg price set {item_id} <price>[/dim yellow]"
            )
    except Exception as exc:
        console.print(f"  [dim yellow]Price fetch error: {exc}[/dim yellow]")


def _auto_update_multi(item_ids: list[int]) -> None:
    """Fetch price once for item_ids[0], copy snapshot to all IDs, regen dashboard."""
    from optcg.scrapers.cardmarket import get_card_prices
    from optcg.scrapers.ebay import search_sold_listings
    from optcg.export import auto_export
    if not item_ids:
        return
    try:
        with db_conn() as conn:
            db = Database(conn)
            item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_ids[0],))
            if not item:
                return
            saved = False
            known_url = item["cardmarket_url"] if "cardmarket_url" in item.keys() else None
            cm = get_card_prices(item["name"], item["set_code"], item["card_number"],
                                 item["language"], item["item_type"], known_url=known_url,
                                 condition=item["condition"])
            for ptype in ("trend", "low", "market"):
                if cm.get(ptype):
                    for iid in item_ids:
                        db.execute(
                            "INSERT INTO price_snapshots (item_id, source, price_type, price, currency, url) "
                            "VALUES (?, 'cardmarket', ?, ?, 'EUR', ?)",
                            (iid, ptype, cm[ptype], cm.get("url")),
                        )
                    saved = True
            if cm.get("url"):
                for iid in item_ids:
                    db.execute("UPDATE items SET cardmarket_url = ? WHERE id = ?",
                               (cm["url"], iid))
            if cm.get("img"):
                for iid in item_ids:
                    db.execute("UPDATE items SET cardmarket_img = ? WHERE id = ?",
                               (cm["img"], iid))

            query = f"One Piece {item['card_number'] or ''} {item['name']}".strip()
            sold_listings = search_sold_listings(query, max_results=5)
            avg = None
            if sold_listings:
                avg = sum(x["price"] for x in sold_listings) / len(sold_listings)
                for iid in item_ids:
                    db.execute(
                        "INSERT INTO price_snapshots (item_id, source, price_type, price, currency, url) "
                        "VALUES (?, 'ebay_sold', 'sold_avg', ?, 'EUR', ?)",
                        (iid, avg, sold_listings[0].get("url")),
                    )
                saved = True

            auto_export(db)

        _SEALED = {"booster_box", "blister", "sealed_set"}
        is_sealed = item["item_type"] in _SEALED
        if saved:
            parts = []
            cm_p = cm.get("low") or cm.get("trend")
            lbl  = "CM low" if cm.get("low") else "CM trend"
            if cm_p:  parts.append(f"{lbl} {cm_p:.2f} €")
            if avg:   parts.append(f"eBay {avg:.2f} €")
            console.print(f"  [dim]Price: {'  ·  '.join(parts)}  · dashboard updated[/dim]")
        else:
            err = cm.get("error") or "no data found"
            console.print(
                f"  [dim yellow]Price fetch failed: {err}\n"
                f"  → optcg price set {item_ids[0]} <price>[/dim yellow]"
            )
    except Exception as exc:
        console.print(f"  [dim yellow]Price fetch error: {exc}[/dim yellow]")


def _regen_dashboard() -> None:
    """Regenerate the HTML dashboard (no price fetch). Called after edit/remove."""
    from optcg.export import auto_export
    try:
        with db_conn() as conn:
            db = Database(conn)
            auto_export(db)
    except Exception:
        pass


# ── Root group ─────────────────────────────────────────────────────────────────

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("0.1.0", prog_name="optcg")
def main():
    """One Piece TCG Investment Tracker

    Data stored in iCloud Drive. CSV auto-exported after every price update.

    \b
    Quick start:
      optcg add card                          # interactive search + add
      optcg add card -n "Monkey D. Luffy" -s OP-01 -p 45.00
      optcg list
      optcg portfolio
      optcg price update --all
      optcg search "Zoro" --sort cheap --image
      optcg deals search "Luffy" --discount 20
      optcg watchlist add -n "Shanks" --target 80.00
    """
    init_db()


# ══════════════════════════════════════════════════════════════════════════════
# ADD
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def add():
    """Add items to the portfolio.

    \b
    Examples:
      optcg add card                              # interactive CardMarket search
      optcg add card -n "Monkey D. Luffy" -s OP-01 -l JP -p 120.00
      optcg add card -n "Roronoa Zoro" --card-num OP01-001 -p 45.00 --foil
      optcg add card -n "Shanks" -p 200 --graded --gc PSA --grade 10 --cert 12345678
      optcg add promo                             # interactive search
      optcg add promo -n "Nami" --card-num P-001 -p 15.00
      optcg add box -n "Romance Dawn" -s OP-01 -p 95.00 --source CardMarket
      optcg add blister -n "OP-01 Blister" -s OP-01 -p 4.50
    """


@add.command("card")
@click.option("--name",      "-n", default=None,    help="Card name (omit to search interactively)")
@click.option("--set",       "-s", "set_code",       default=None, help="Set code (OP-01, OP-07...)")
@click.option("--card-num",        "card_number",    default=None, help="Card number (OP01-001)")
@click.option("--lang",      "-l", default="EN",    type=_LANG_CHOICE,  show_default=True)
@click.option("--condition", "-c", default="NM",    type=_COND_CHOICE,  show_default=True)
@click.option("--foil",            is_flag=True,     default=False)
@click.option("--variant",         default=None,     help="Alt Art, Full Art, SP, etc.")
@click.option("--graded",          is_flag=True,     default=False, help="Graded slab?")
@click.option("--gc",              "grading_company",default=None,  type=_GRADE_CHOICE, help="Grading company")
@click.option("--grade",     "-g", default=None,     help="Grade value (10, 9.5, ...)")
@click.option("--cert",            default=None,     help="Cert / serial number")
@click.option("--price",     "-p", default=None,    type=float,   help="Purchase price (EUR)")
@click.option("--date",      "-d", "purchase_date",  default=str(date.today()), show_default=True)
@click.option("--source",          default=None,     help="CardMarket / eBay / LGS / ...")
@click.option("--notes",           default=None)
@click.option("--pending",         is_flag=True, default=False,
              help="Pre-order / paid but not yet arrived")
def add_card(name, set_code, card_number, lang, condition, foil, variant,
             graded, grading_company, grade, cert, price, purchase_date, source, notes, pending):
    """Add a single card. Omit --name to search CardMarket interactively.

    \b
    Examples:
      optcg add card                                  # search & pick interactively
      optcg add card -n "Monkey D. Luffy" -s OP-01 -p 45.00
      optcg add card -n "Zoro" -s OP-01 -l JP -c NM -p 120.00 --foil
      optcg add card -n "Shanks" -p 200.00 --graded --gc PSA --grade 10 --cert 12345678
      optcg add card -n "OP-10 Luffy" -p 45.00 --pending   # pre-order, not arrived yet
    """
    # ── Interactive card search ────────────────────────────────────────────────
    if not name:
        import questionary as _q
        query = click.prompt("Search CardMarket")
        result = pick_card(query, language=lang)
        if not result:
            console.print("[yellow]Cancelled.[/yellow]")
            return
        dest = _q.select(
            "Add to:",
            choices=[
                _q.Choice("Portfolio  (bought it)", value="portfolio"),
                _q.Choice("Wishlist   (want it)",   value="wishlist"),
                _q.Choice("Cancel",                 value=None),
            ],
        ).ask()
        if not dest:
            return
        if dest == "wishlist":
            sc = set_code
            if not sc and result.card_number:
                m = re.match(r"([A-Z]+)(\d+)-", result.card_number)
                if m:
                    sc = f"{m.group(1)}-{m.group(2)}"
            target_raw = click.prompt("Target price € (leave blank to skip)", default="", show_default=False)
            try:    target_price = float(target_raw) if target_raw.strip() else None
            except: target_price = None
            with db_conn() as conn:
                db_obj = Database(conn)
                wid = db_obj.lastrowid(
                    "INSERT INTO watchlist (name, set_code, card_number, variant, language, item_type, target_price, cm_url) VALUES (?,?,?,?,?,?,?,?)",
                    (result.name, sc, result.card_number, result.variant or None,
                     lang or "EN", "card", target_price, result.cm_url),
                )
            vstr = f"  [magenta]{result.variant}[/magenta]" if result.variant else ""
            console.print(f"[green]✓[/green] Added to wishlist [bold]#{wid}[/bold]: {result.name}{vstr}")
            return
        name        = name        or result.name
        card_number = card_number or result.card_number
        variant     = variant     or (result.variant if result.variant else None)
        # Derive set_code from card_number prefix (e.g. OP01-001 → OP-01)
        if not set_code and result.card_number:
            m = re.match(r"([A-Z]+)(\d+)-", result.card_number)
            if m:
                set_code = f"{m.group(1)}-{m.group(2)}"

    if price is None:
        price = click.prompt("Purchase price (EUR)", type=float)

    if graded:
        if not grading_company:
            grading_company = click.prompt("Grading company", type=_GRADE_CHOICE)
        if not grade:
            grade = click.prompt("Grade (e.g. 10, 9.5)")

    status = "pending" if pending else "owned"
    row_id = _insert_item("card", name, set_code, card_number, lang.upper(), condition,
                           foil, variant, graded, grading_company, grade, cert,
                           price, purchase_date, source, notes, status)
    flag = " [yellow][PENDING][/yellow]" if pending else ""
    console.print(f"[green]✓[/green] Added card [bold]#{row_id}[/bold]: {name}{flag}")
    with console.status("[dim]Fetching price…"):
        _auto_update(row_id)


@add.command("promo")
@click.option("--name",      "-n", default=None,   help="Card name (omit to search interactively)")
@click.option("--card-num",        "card_number",   default=None)
@click.option("--set",       "-s", "set_code",     default=None,
              help='Set code or promo category (e.g. P, PROMO-JP, P-OP)')
@click.option("--variant",         default=None,   help="Variant label (e.g. V1, Alt Art)")
@click.option("--lang",      "-l", default="EN",   type=_LANG_CHOICE,  show_default=True)
@click.option("--condition", "-c", default="NM",   type=_COND_CHOICE,  show_default=True)
@click.option("--graded",          is_flag=True,   default=False)
@click.option("--gc",              "grading_company", default=None, type=_GRADE_CHOICE)
@click.option("--grade",     "-g", default=None)
@click.option("--cert",            default=None)
@click.option("--price",     "-p", default=None,   type=float)
@click.option("--date",      "-d", "purchase_date", default=str(date.today()))
@click.option("--source",          default=None)
@click.option("--notes",           default=None)
@click.option("--pending",         is_flag=True, default=False,
              help="Pre-order / paid but not yet arrived")
def add_promo(name, card_number, set_code, variant, lang, condition, graded,
              grading_company, grade, cert, price, purchase_date, source, notes, pending):
    """Add a promo card. Omit --name to search CardMarket interactively.

    \b
    Examples:
      optcg add promo                                           # search & pick interactively
      optcg add promo -n "Monkey D. Luffy" --card-num P-043 -p 8.00
      optcg add promo -n "Monkey D. Luffy" --card-num ST21-014 --set P --variant V1 -l JP -c M -p 82.35
    """
    if not name:
        import questionary as _q
        query = click.prompt("Search CardMarket")
        result = pick_card(query, language=lang)
        if not result:
            console.print("[yellow]Cancelled.[/yellow]")
            return
        dest = _q.select(
            "Add to:",
            choices=[
                _q.Choice("Portfolio  (bought it)", value="portfolio"),
                _q.Choice("Wishlist   (want it)",   value="wishlist"),
                _q.Choice("Cancel",                 value=None),
            ],
        ).ask()
        if not dest:
            return
        if dest == "wishlist":
            sc = set_code or ("P" if (result.card_number or "").startswith("P-") else None)
            target_raw = click.prompt("Target price € (leave blank to skip)", default="", show_default=False)
            try:    target_price = float(target_raw) if target_raw.strip() else None
            except: target_price = None
            with db_conn() as conn:
                db_obj = Database(conn)
                wid = db_obj.lastrowid(
                    "INSERT INTO watchlist (name, set_code, card_number, variant, language, item_type, target_price, cm_url) VALUES (?,?,?,?,?,?,?,?)",
                    (result.name, sc, result.card_number, result.variant or None,
                     lang or "EN", "promo", target_price, result.cm_url),
                )
            vstr = f"  [magenta]{result.variant}[/magenta]" if result.variant else ""
            console.print(f"[green]✓[/green] Added to wishlist [bold]#{wid}[/bold]: {result.name}{vstr}")
            return
        name        = name        or result.name
        card_number = card_number or result.card_number
    if price is None:
        price = click.prompt("Purchase price (EUR)", type=float)
    if graded:
        if not grading_company:
            grading_company = click.prompt("Grading company", type=_GRADE_CHOICE)
        if not grade:
            grade = click.prompt("Grade")
    status = "pending" if pending else "owned"
    row_id = _insert_item("promo", name, set_code, card_number, lang.upper(), condition,
                           False, variant, graded, grading_company, grade, cert,
                           price, purchase_date, source, notes, status)
    flag = " [yellow][PENDING][/yellow]" if pending else ""
    console.print(f"[green]✓[/green] Added promo [bold]#{row_id}[/bold]: {name}{flag}")
    with console.status("[dim]Fetching price…"):
        _auto_update(row_id)


@add.command("blister")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--qty",   "-q", default=1, type=click.IntRange(min=1), show_default=True,
              help="Number of copies to add")
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
@click.option("--pending",     is_flag=True, default=False, help="Pre-order / not yet arrived")
def add_blister(name, set_code, lang, price, qty, purchase_date, source, notes, pending):
    """Add a blister pack. Use --qty to add multiple copies at the same price."""
    status = "pending" if pending else "owned"
    ids = [
        _insert_item("blister", name, set_code, None, lang.upper(), "M",
                     False, None, False, None, None, None,
                     price, purchase_date, source, notes, status)
        for _ in range(qty)
    ]
    flag = " [yellow][PENDING][/yellow]" if pending else ""
    id_s = f"#{ids[0]}" if qty == 1 else f"#{ids[0]}–#{ids[-1]}"
    console.print(f"[green]✓[/green] Added {qty}× blister [bold]{id_s}[/bold]: {name}  {price:.2f} € each{flag}")
    with console.status("[dim]Fetching price…"):
        _auto_update_multi(ids)


@add.command("box")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--qty",   "-q", default=1, type=click.IntRange(min=1), show_default=True,
              help="Number of copies to add")
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
@click.option("--pending",     is_flag=True, default=False, help="Pre-order / not yet arrived")
def add_box(name, set_code, lang, price, qty, purchase_date, source, notes, pending):
    """Add a booster box. Use --qty to add multiple copies at the same price."""
    status = "pending" if pending else "owned"
    ids = [
        _insert_item("booster_box", name, set_code, None, lang.upper(), "M",
                     False, None, False, None, None, None,
                     price, purchase_date, source, notes, status)
        for _ in range(qty)
    ]
    flag = " [yellow][PENDING][/yellow]" if pending else ""
    id_s = f"#{ids[0]}" if qty == 1 else f"#{ids[0]}–#{ids[-1]}"
    console.print(f"[green]✓[/green] Added {qty}× booster box [bold]{id_s}[/bold]: {name}  {price:.2f} € each{flag}")
    with console.status("[dim]Fetching price…"):
        _auto_update_multi(ids)


@add.command("sealed")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--qty",   "-q", default=1, type=click.IntRange(min=1), show_default=True,
              help="Number of copies to add")
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
@click.option("--pending",     is_flag=True, default=False, help="Pre-order / not yet arrived")
def add_sealed(name, set_code, lang, price, qty, purchase_date, source, notes, pending):
    """Add a sealed set / special product. Use --qty to add multiple copies at the same price."""
    status = "pending" if pending else "owned"
    ids = [
        _insert_item("sealed_set", name, set_code, None, lang.upper(), "M",
                     False, None, False, None, None, None,
                     price, purchase_date, source, notes, status)
        for _ in range(qty)
    ]
    flag = " [yellow][PENDING][/yellow]" if pending else ""
    id_s = f"#{ids[0]}" if qty == 1 else f"#{ids[0]}–#{ids[-1]}"
    console.print(f"[green]✓[/green] Added {qty}× sealed [bold]{id_s}[/bold]: {name}  {price:.2f} € each{flag}")
    with console.status("[dim]Fetching price…"):
        _auto_update_multi(ids)


# ══════════════════════════════════════════════════════════════════════════════
# LIST
# ══════════════════════════════════════════════════════════════════════════════

@main.command("list")
@click.option("--type",      "item_type",   default=None, type=_TYPE_CHOICE)
@click.option("--set",       "set_code",    default=None)
@click.option("--lang",                     default=None)
@click.option("--condition", "-c",          default=None, type=_COND_CHOICE,
              help="Filter by condition (M/NM/LP/MP/HP/PL)")
@click.option("--graded",    "graded_only", is_flag=True, default=False)
@click.option("--pending",   "pending_only", is_flag=True, default=False,
              help="Show only pre-orders / items not yet arrived")
@click.option("--sold",      "sold_only",   is_flag=True, default=False,
              help="Show only sold items")
@click.option("--active",    "active_only", is_flag=True, default=False,
              help="Hide sold items (show owned + pending only)")
@click.option("--sort",      default="date", type=_SORT_CHOICE, show_default=True)
def list_items(item_type, set_code, lang, condition, graded_only, pending_only, sold_only, active_only, sort):
    """List portfolio items.

    \b
    Examples:
      optcg list                          # all items, newest first
      optcg list --sort pnl               # best P&L at top
      optcg list --type card --lang JP    # Japanese singles only
      optcg list --set OP-01              # Romance Dawn set only
      optcg list --condition NM           # Near Mint items only
      optcg list --graded                 # graded slabs only
      optcg list --pending                # pre-orders awaiting arrival
    """
    with db_conn() as conn:
        db = Database(conn)

        where, params = [], []
        if item_type:
            where.append("item_type = ?"); params.append(item_type)
        if set_code:
            where.append("set_code = ?");  params.append(set_code.upper())
        if lang:
            where.append("language = ?");  params.append(lang.upper())
        if condition:
            where.append("condition = ?"); params.append(condition.upper())
        if graded_only:
            where.append("graded = 1")
        if pending_only:
            where.append("status = 'pending'")
        if sold_only:
            where.append("status = 'sold'")
        if active_only:
            where.append("status != 'sold'")

        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        items = db.fetchall(sql, tuple(params))

        if not items:
            console.print("[yellow]No items match.[/yellow]")
            return

        rows = [(item, item_pnl(item, db)) for item in items]

        if sort == "name":
            rows.sort(key=lambda x: x[0]["name"].lower())
        elif sort == "price":
            rows.sort(key=lambda x: x[0]["purchase_price"], reverse=True)
        elif sort == "pnl":
            rows.sort(key=lambda x: (x[1]["pnl"] or 0.0), reverse=True)
        elif sort == "pct":
            rows.sort(key=lambda x: (x[1]["pnl_pct"] or 0.0), reverse=True)

        TYPE_COLOR = {
            "card": "white", "promo": "magenta",
            "blister": "cyan", "booster_box": "blue", "sealed_set": "yellow",
        }

        tbl = Table(
            show_header=True, header_style="bold cyan",
            box=box.ROUNDED, expand=False,
        )
        tbl.add_column("#",          style="dim",      width=5)
        tbl.add_column("Type",                         width=11)
        tbl.add_column("Name",       min_width=22)
        tbl.add_column("Set",                          width=7)
        tbl.add_column("Num",                          width=10)
        tbl.add_column("Lang",                         width=5)
        tbl.add_column("Cond/Grade",                   width=12)
        tbl.add_column("Paid €",     justify="right",  width=8)
        tbl.add_column("Now €",      justify="right",  width=8)
        tbl.add_column("P&L",        justify="right",  width=16)
        tbl.add_column("Date",                         width=11)

        for item, pnl in rows:
            c       = TYPE_COLOR.get(item["item_type"], "white")
            status  = item["status"] if "status" in item.keys() else "owned"
            pending = status == "pending"
            sold    = status == "sold"
            if sold:
                cur_str  = f"[dim]{pnl['current']:.2f}[/dim]" if pnl["current"] else "[dim]—[/dim]"
                name_str = f"[dim strike]{item['name']}[/dim strike]"
            elif pending:
                cur_str  = "[yellow]PENDING[/yellow]"
                name_str = f"[yellow]⏳[/yellow] {item['name']}"
            else:
                cur_str  = f"{pnl['current']:.2f}" if pnl["current"] is not None else "[dim]—[/dim]"
                name_str = item["name"]
            pnl_str = "[dim]—[/dim]"
            if sold:
                pnl_str = _fmt_pnl(pnl["pnl"], pnl["pnl_pct"]) + " [dim][SOLD][/dim]"
            elif not pending:
                pnl_str = _fmt_pnl(pnl["pnl"], pnl["pnl_pct"])
            tbl.add_row(
                str(item["id"]),
                f"[{c}]{item['item_type']}[/{c}]",
                name_str,
                item["set_code"] or "—",
                item["card_number"] or "—",
                item["language"] or "EN",
                _fmt_grade(item),
                f"{item['purchase_price']:.2f}",
                cur_str,
                pnl_str,
                item["purchase_date"],
            )

        console.print(tbl)
        console.print(f"[dim]{len(rows)} item(s)[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════════════════════════

_SORT_CHOICE_SEARCH = click.Choice(list(SORT_OPTIONS.keys()), case_sensitive=False)


def _set_code_from_result(card_number: str, set_slug: str) -> str:
    """Derive a set code from card_number (OP12-020 → OP-12) or fall back to 'P'."""
    import re as _re
    m = _re.match(r'^([A-Za-z]+)(\d+)-', card_number or "")
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    if (card_number or "").startswith("P-"):
        return "P"
    return "P"


def _item_type_from_slug(set_slug: str) -> str:
    """Guess item_type from CM set slug."""
    slug_lower = set_slug.lower()
    if "promo" in slug_lower or "tournament" in slug_lower:
        return "promo"
    return "card"


def _add_from_result(r, lang: str) -> None:
    """Prompt for destination (portfolio or wishlist), then insert from a CardResult."""
    import questionary
    from optcg.scrapers.slugs import LANGUAGE_CM_CODES

    item_type = _item_type_from_slug(r.set_slug)
    set_code  = _set_code_from_result(r.card_number, r.set_slug)

    cm_url = r.cm_url
    if lang:
        lcode = LANGUAGE_CM_CODES.get(lang.upper())
        if lcode and "language=" not in cm_url:
            sep = "&" if "?" in cm_url else "?"
            cm_url += f"{sep}language={lcode}"

    console.print(
        f"\n[bold]{r.name}[/bold]  [cyan]{r.card_number}[/cyan]"
        f"  [magenta]{r.variant}[/magenta]  [dim]{r.set_slug}[/dim]"
        f"\n[dim]Type: {item_type}  Set: {set_code}[/dim]\n"
    )

    dest = questionary.select(
        "Add to:",
        choices=[
            questionary.Choice("Portfolio  (bought it)", value="portfolio"),
            questionary.Choice("Wishlist   (want it)",   value="wishlist"),
            questionary.Choice("Cancel",                 value=None),
        ],
    ).ask()

    if not dest:
        return

    # ── Wishlist path ─────────────────────────────────────────────────────────
    if dest == "wishlist":
        target_raw = click.prompt("Target price € (leave blank to skip)", default="", show_default=False)
        try:
            target_price = float(target_raw) if target_raw.strip() else None
        except ValueError:
            target_price = None
        pick_lang = click.prompt("Language", default=lang or "EN").upper()
        with db_conn() as conn:
            db_obj = Database(conn)
            row_id = db_obj.lastrowid(
                "INSERT INTO watchlist (name, set_code, card_number, variant, language, item_type, target_price, cm_url) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r.name, set_code, r.card_number, r.variant or None, pick_lang,
                 item_type, target_price, cm_url),
            )
        vstr = f"  [magenta]{r.variant}[/magenta]" if r.variant else ""
        console.print(f"[green]✓[/green] Added to wishlist [bold]#{row_id}[/bold]: {r.name}{vstr}")
        return

    # ── Portfolio path ────────────────────────────────────────────────────────
    price     = click.prompt("Purchase price per item (EUR)", type=float)
    qty       = click.prompt("Quantity", type=int, default=1)
    pick_lang = click.prompt("Language", default=lang or "EN").upper()
    condition = click.prompt("Condition [M/NM/LP/MP/HP/PL]", default="NM").upper()
    source    = click.prompt("Source", default="CardMarket")

    item_ids = _insert_items(
        qty=max(1, qty),
        item_type=item_type, name=r.name, set_code=set_code,
        card_number=r.card_number, language=pick_lang, condition=condition,
        foil=False, variant=r.variant or None, graded=False,
        grading_company=None, grade=None, cert_number=None,
        price=price, purchase_date=str(date.today()), source=source, notes=None,
        cardmarket_url=cm_url, cardmarket_img=r.image_url,
    )

    qty_str = f" ×{qty}" if qty > 1 else ""
    console.print(f"[green]✓[/green] Added {item_type}{qty_str} [bold]#{item_ids[0]}{'–#'+str(item_ids[-1]) if qty > 1 else ''}[/bold]: {r.name}")
    _auto_update_multi(item_ids)


def _print_search_table(results, page: int, sort: str) -> None:
    from rich.table import Table
    from rich import box as rbox
    t = Table(box=rbox.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("#",      style="dim",        width=3,  no_wrap=True)
    t.add_column("Name",   style="bold white", min_width=26)
    t.add_column("Number", style="cyan",       width=13, no_wrap=True)
    t.add_column("Set",    style="dim",        min_width=16)
    t.add_column("Var",    style="magenta",    width=5,  no_wrap=True)
    t.add_column("From",   style="green",      width=10, no_wrap=True)
    for i, r in enumerate(results, 1):
        set_display = r.set_slug.replace("-", " ").replace("ONE PIECE CARD GAME ", "")[:28]
        t.add_row(str(i), r.name[:36], r.card_number, set_display, r.variant, r.price_from)
    console.print(t)
    console.print(
        f"[dim]{len(results)} result(s) · page {page} · sorted by {sort}"
        f"  |  n next · p prev · # image · add # pick · q quit[/dim]"
    )


@main.command("search")
@click.argument("query")
@click.option("--lang",  "-l", default=None,       type=_LANG_CHOICE, help="Filter by language")
@click.option("--sort",  "-s", default="popular",  type=_SORT_CHOICE_SEARCH, show_default=True,
              help="popular | cheap | expensive | name | number | new | old")
@click.option("--page",  "-p", default=1,          type=int, show_default=True, help="Page number")
@click.option("--image", "-i", is_flag=True,       default=False,
              help="Interactive browse: view images, navigate pages, add to collection")
@click.option("--pick",  "-k", default=None,       type=int,
              help="Pick result #N directly and add it to your collection")
def search_cmd(query: str, lang: str, sort: str, page: int, image: bool, pick: int):
    """Search CardMarket for One Piece singles.

    \b
    Sort options: popular, cheap, expensive, name, number, new, old
    Examples:
      optcg search "Luffy" --sort cheap --page 2
      optcg search "Zoro" --sort expensive --pick 23
      optcg search "Zoro" --image          # browse, view images, add interactively
    """
    # ── Initial fetch ─────────────────────────────────────────────────────────
    from optcg.search import CFBlockedError
    try:
        with console.status(f"[dim]Searching [bold]{query}[/bold]  [page {page}, sort: {sort}]…"):
            results = search_cardmarket(query, language=lang, sort=sort, page=page)
    except CFBlockedError as e:
        console.print(f"[bold red]⚠ {e}[/bold red]")
        return

    if not results:
        console.print("[yellow]No results.[/yellow]")
        return

    _print_search_table(results, page, sort)

    # ── Non-interactive --pick shortcut ───────────────────────────────────────
    if pick is not None:
        if not (1 <= pick <= len(results)):
            console.print(f"[red]--pick {pick} out of range (1–{len(results)})[/red]")
            return
        _add_from_result(results[pick - 1], lang)
        return

    # ── Interactive image-browse mode ─────────────────────────────────────────
    if not image:
        return

    if not _supports_inline_images():
        console.print("[yellow]Inline images not supported in this terminal.[/yellow]")
        return

    from optcg.search import fetch_card_image_bytes, _iterm2_inline, _write_to_tty

    console.print(
        "[dim]Commands:  [bold]#[/bold] view image  "
        "·  [bold]add #[/bold] add to collection  "
        "·  [bold]n[/bold] next page  "
        "·  [bold]p[/bold] prev page  "
        "·  [bold]q[/bold] quit[/dim]"
    )

    while True:
        cmd = click.prompt(f"[page {page}]", default="q", prompt_suffix=" > ").strip().lower()

        if cmd in ("q", ""):
            break

        elif cmd == "n":
            page += 1
            with console.status(f"[dim]Loading page {page}…"):
                new_results = search_cardmarket(query, language=lang, sort=sort, page=page)
            if not new_results:
                console.print("[yellow]No more results.[/yellow]")
                page -= 1
            else:
                results = new_results
                _print_search_table(results, page, sort)

        elif cmd == "p":
            if page <= 1:
                console.print("[dim]Already on page 1.[/dim]")
            else:
                page -= 1
                with console.status(f"[dim]Loading page {page}…"):
                    results = search_cardmarket(query, language=lang, sort=sort, page=page)
                _print_search_table(results, page, sort)

        elif cmd.startswith("add "):
            parts = cmd.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                if 0 <= idx < len(results):
                    _add_from_result(results[idx], lang)
                else:
                    console.print(f"[red]#{idx+1} out of range (1–{len(results)})[/red]")
            else:
                console.print("[dim]Usage: add <number>[/dim]")

        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(results):
                with console.status("[dim]Loading image…"):
                    img_data = fetch_card_image_bytes(results[idx])
                r = results[idx]
                console.print(
                    f"[bold]{r.name}[/bold]  [cyan]{r.card_number}[/cyan]"
                    f"  [magenta]{r.variant}[/magenta]  [green]{r.price_from}[/green]"
                )
                if img_data:
                    _write_to_tty(_iterm2_inline(img_data, width_cols=30))
                else:
                    console.print(f"[dim]No image available.[/dim]")
            else:
                console.print(f"[red]#{int(cmd)} out of range (1–{len(results)})[/red]")

        else:
            console.print("[dim]Unknown command. Try: # · add # · n · p · q[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# SHOW
# ══════════════════════════════════════════════════════════════════════════════

@main.command("show")
@click.argument("item_id", type=int)
def show_item(item_id: int):
    """Show full detail for an item."""
    with db_conn() as conn:
        db = Database(conn)
        item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
        if not item:
            console.print(f"[red]Item #{item_id} not found.[/red]")
            sys.exit(1)

        pnl      = item_pnl(item, db)
        history  = get_price_history(item_id, db)
        receipts = db.fetchall(
            "SELECT * FROM receipts WHERE item_id = ? ORDER BY added_at DESC", (item_id,)
        )

        title = f"#{item['id']} · {item['name']}"
        if item["graded"]:
            title += f"  [{item['grading_company']} {item['grade']}]"

        lines = [
            f"[bold]Type:[/bold]       {item['item_type']}",
            f"[bold]Set:[/bold]        {item['set_code'] or '—'}",
            f"[bold]Card #:[/bold]     {item['card_number'] or '—'}",
            f"[bold]Language:[/bold]   {item['language'] or 'EN'}",
            f"[bold]Condition:[/bold]  {item['condition'] or '—'}",
            f"[bold]Foil:[/bold]       {'Yes' if item['foil'] else 'No'}",
            f"[bold]Variant:[/bold]    {item['variant'] or '—'}",
        ]
        if item["graded"]:
            lines += [
                "",
                f"[bold]Grader:[/bold]     [yellow]{item['grading_company']}[/yellow]",
                f"[bold]Grade:[/bold]      [bold yellow]{item['grade']}[/bold yellow]",
                f"[bold]Cert #:[/bold]     {item['cert_number'] or '—'}",
            ]
        lines += [
            "",
            f"[bold]Paid:[/bold]       [cyan]{item['purchase_price']:.2f} EUR[/cyan]",
            f"[bold]Date:[/bold]       {item['purchase_date']}",
            f"[bold]Source:[/bold]     {item['purchase_source'] or '—'}",
        ]
        if pnl["current"] is not None:
            color = "green" if pnl["pnl"] >= 0 else "red"
            sign  = "+" if pnl["pnl"] >= 0 else ""
            lines += [
                f"[bold]Now:[/bold]        [bold]{pnl['current']:.2f} EUR[/bold]",
                f"[bold]P&L:[/bold]        [{color}]{sign}{pnl['pnl']:.2f} EUR  {sign}{pnl['pnl_pct']:.1f}%[/{color}]",
                f"[bold]Price from:[/bold] {pnl.get('price_source', '—')} · {pnl.get('price_date', '—')}",
            ]
        else:
            lines.append("[bold]Now:[/bold]        [dim]No price — run: optcg price update --item-id "
                         + str(item_id) + "[/dim]")
        if item["notes"]:
            lines += ["", f"[bold]Notes:[/bold]      {item['notes']}"]

        console.print(Panel("\n".join(lines), title=title, border_style="cyan"))

        if history:
            ht = Table("Date", "Source", "Type", "Price €", box=box.SIMPLE, show_header=True)
            for snap in history[:12]:
                ht.add_row(
                    snap["fetched_at"][:10],
                    snap["source"],
                    snap["price_type"],
                    f"{snap['price']:.2f}",
                )
            console.print(Panel(ht, title="Price History", border_style="dim"))

        if receipts:
            rt = Table("#", "File", "Description", "Added", box=box.SIMPLE)
            for r in receipts:
                rt.add_row(str(r["id"]), r["filename"], r["description"] or "—", r["added_at"][:10])
            console.print(Panel(rt, title="Receipts", border_style="dim"))


# ══════════════════════════════════════════════════════════════════════════════
# EDIT / REMOVE
# ══════════════════════════════════════════════════════════════════════════════

@main.command("edit")
@click.argument("item_id", type=int)
@click.option("--name",      default=None)
@click.option("--condition", "-c", default=None, type=_COND_CHOICE)
@click.option("--price",     "-p", type=float,   default=None)
@click.option("--date",      "-d", "purchase_date", default=None)
@click.option("--source",    default=None)
@click.option("--notes",     default=None)
@click.option("--variant",   default=None)
@click.option("--grade",     "-g", default=None)
@click.option("--cert",      default=None)
@click.option("--lang",      "-l", default=None, type=_LANG_CHOICE)
@click.option("--arrived",   is_flag=True, default=False,
              help="Mark a pending pre-order as arrived/owned")
@click.option("--pending",   is_flag=True, default=False,
              help="Mark an item as a pending pre-order")
def edit_item(item_id, name, condition, price, purchase_date, source, notes,
              variant, grade, cert, lang, arrived, pending):
    """Edit fields on an existing item.

    \b
    Examples:
      optcg edit 3 --condition LP
      optcg edit 3 --price 55.00
      optcg edit 3 --grade 9.5 --cert 87654321
      optcg edit 3 --notes "bought at GP Madrid"
      optcg edit 3 --arrived           # pre-order landed, mark as owned
    """
    field_map = [
        ("name", name), ("condition", condition), ("purchase_price", price),
        ("purchase_date", purchase_date), ("purchase_source", source),
        ("notes", notes), ("variant", variant), ("grade", grade),
        ("cert_number", cert), ("language", lang),
    ]
    updates = [(f, v) for f, v in field_map if v is not None]
    if arrived:
        updates.append(("status", "owned"))
    elif pending:
        updates.append(("status", "pending"))
    if not updates:
        console.print("[yellow]Nothing to update. Pass at least one option.[/yellow]")
        return

    with db_conn() as conn:
        db = Database(conn)
        if not db.fetchone("SELECT id FROM items WHERE id = ?", (item_id,)):
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)
        fields_sql = ", ".join(f"{f} = ?" for f, _ in updates)
        values = [v for _, v in updates] + [item_id]
        db.execute(
            f"UPDATE items SET {fields_sql}, updated_at = datetime('now') WHERE id = ?",
            tuple(values),
        )
    status_msg = ("  [green]Marked as arrived ✓[/green]" if arrived else
                  "  [yellow]Marked as pending ⏳[/yellow]" if pending else "")
    console.print(f"[green]✓[/green] Updated #{item_id}{status_msg}")
    _regen_dashboard()


@main.command("sell")
@click.argument("item_id", type=int)
@click.option("--price",  "-p", "sell_price", required=True, type=float, help="Sell price (EUR)")
@click.option("--date",   "-d", "sell_date",  default=str(date.today()), show_default=True)
@click.option("--source",       "sell_source", default=None, help="CardMarket / eBay / LGS / ...")
@click.option("--notes",        default=None,  help="Append to existing notes")
def sell_item(item_id, sell_price, sell_date, sell_source, notes):
    """Mark an item as sold and record the sale price.

    Receipts, price history and all data are kept for tax / metric purposes.

    \b
    Examples:
      optcg sell 7 --price 180.00
      optcg sell 7 --price 180.00 --source CardMarket --date 2026-04-20
    """
    with db_conn() as conn:
        db = Database(conn)
        item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
        if not item:
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)
        if (item["status"] if "status" in item.keys() else "owned") == "sold":
            console.print(f"[yellow]Item #{item_id} is already marked as sold.[/yellow]")
            return

        # Append sell note if provided
        existing_notes = item["notes"] or ""
        merged_notes   = f"{existing_notes}\nSold {sell_date} via {sell_source or '?'}: {sell_price:.2f} €".strip() \
                         if not notes else \
                         f"{existing_notes}\n{notes}".strip()

        db.execute(
            """UPDATE items
               SET status = 'sold', sell_price = ?, sell_date = ?, sell_source = ?,
                   notes = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (sell_price, sell_date, sell_source, merged_notes, item_id),
        )

    cost    = item["purchase_price"]
    pnl     = sell_price - cost
    pnl_pct = (pnl / cost * 100) if cost else 0.0
    sg      = "+" if pnl >= 0 else ""
    color   = "green" if pnl >= 0 else "red"
    console.print(
        f"[green]✓[/green] Sold #{item_id}: {item['name']}\n"
        f"  Bought [cyan]{cost:.2f} €[/cyan]  →  Sold [{color}]{sell_price:.2f} €[/{color}]  "
        f"[{color}]{sg}{pnl:.2f} € ({sg}{pnl_pct:.1f}%)[/{color}]"
    )
    _regen_dashboard()


@main.command("remove")
@click.argument("item_id", type=int)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation")
def remove_item(item_id, yes):
    """Remove an item from the portfolio."""
    with db_conn() as conn:
        db = Database(conn)
        item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
        if not item:
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)
        if not yes:
            click.confirm(f"Remove #{item_id} '{item['name']}'?", abort=True)
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    console.print(f"[red]Removed[/red] #{item_id}: {item['name']}")
    _regen_dashboard()


# ══════════════════════════════════════════════════════════════════════════════
# RECEIPT
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def receipt():
    """Manage purchase receipts (tax documents).

    Files are copied into iCloud and renamed automatically:
    item_<id>_YYYY-MM-DD.<ext>

    \b
    Examples:
      optcg receipt add 3 ~/Downloads/factura.pdf
      optcg receipt add 3 ~/Desktop/invoice.png --desc "CardMarket Jan 2024"
      optcg receipt list 3
      optcg receipt open 3
    """


@receipt.command("add")
@click.argument("item_id", type=int)
@click.argument("filepath", type=click.Path(exists=True, path_type=Path))
@click.option("--desc", default=None, help="Short description (e.g. 'CardMarket invoice Jan 2024')")
def receipt_add(item_id, filepath, desc):
    """
    Attach a receipt / invoice file to an item.

    The file is copied into the iCloud receipts folder and renamed using a
    structured naming convention:  item_<id>_YYYY-MM-DD<ext>
    so all your tax documents are organised automatically.

    \b
    Examples:
      optcg receipt add 3 ~/Downloads/factura.pdf
      optcg receipt add 3 ~/Desktop/screenshot.png --desc "CardMarket Jan 2024"
    """
    import mimetypes, shutil
    from datetime import date as _date

    with db_conn() as conn:
        db = Database(conn)
        item = db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
        if not item:
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)

        dest_dir = RECEIPTS_DIR / f"item_{item_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Structured filename: item_3_2024-01-15.pdf
        # If a file with that name already exists, append _2, _3, etc.
        ext  = filepath.suffix.lower()
        base = f"item_{item_id}_{item['purchase_date']}{ext}"
        dest = dest_dir / base
        counter = 1
        while dest.exists():
            counter += 1
            dest = dest_dir / f"item_{item_id}_{item['purchase_date']}_{counter}{ext}"

        shutil.copy2(filepath, dest)

        mime, _ = mimetypes.guess_type(str(dest))
        rel = str(dest.relative_to(RECEIPTS_DIR))

        # Auto-generate description if not provided
        if not desc:
            desc = f"{item['name']} — {item['purchase_source'] or 'purchase'} {item['purchase_date']}"

        db.execute(
            "INSERT INTO receipts (item_id, filename, file_type, description) VALUES (?,?,?,?)",
            (item_id, rel, mime, desc),
        )

    console.print(f"[green]✓[/green] Receipt saved: [cyan]{dest.name}[/cyan]")
    console.print(f"[dim]  → {dest}[/dim]")


@receipt.command("list")
@click.argument("item_id", type=int)
def receipt_list(item_id):
    """List receipts for an item."""
    with db_conn() as conn:
        db = Database(conn)
        rows = db.fetchall(
            "SELECT * FROM receipts WHERE item_id = ? ORDER BY added_at", (item_id,)
        )
    if not rows:
        console.print("[dim]No receipts for this item.[/dim]")
        return
    for r in rows:
        console.print(
            f"[dim]{r['id']}[/dim]  {r['filename']}  "
            f"[dim]{r['file_type'] or '?'}[/dim]  "
            f"{r['description'] or ''}  {r['added_at'][:10]}"
        )


@receipt.command("open")
@click.argument("item_id", type=int)
@click.argument("receipt_id", type=int, required=False)
def receipt_open(item_id, receipt_id):
    """Open a receipt in the default viewer (macOS Preview / Quick Look)."""
    with db_conn() as conn:
        db = Database(conn)
        if receipt_id:
            row = db.fetchone(
                "SELECT * FROM receipts WHERE id = ? AND item_id = ?", (receipt_id, item_id)
            )
        else:
            row = db.fetchone(
                "SELECT * FROM receipts WHERE item_id = ? ORDER BY added_at DESC LIMIT 1",
                (item_id,),
            )
    if not row:
        console.print("[red]Receipt not found.[/red]"); sys.exit(1)
    path = RECEIPTS_DIR / row["filename"]
    _open_path(path)


# ══════════════════════════════════════════════════════════════════════════════
# PRICE
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def price():
    """Manage prices.

    \b
    Examples:
      optcg price update --all            # scrape CardMarket + eBay for all items
      optcg price update -i 3             # update item #3 only
      optcg price set 3 45.99             # manual override when scraping fails
      optcg price set 3 45.99 --type trend
    """


@price.command("update")
@click.option("--item-id", "-i", type=int,   default=None, help="Update a specific item")
@click.option("--all",    "all_items", is_flag=True, default=False, help="Update every item")
def price_update(item_id, all_items):
    """Scrape current prices from CardMarket and eBay sold comps.

    \b
    Examples:
      optcg price update --all            # refresh every item in the portfolio
      optcg price update -i 3             # update only item #3
    """
    from optcg.scrapers.cardmarket import get_card_prices
    from optcg.scrapers.ebay import search_sold_listings

    if not item_id and not all_items:
        console.print("[yellow]Pass --item-id N or --all[/yellow]")
        return

    with db_conn() as conn:
        db = Database(conn)
        if item_id:
            items = [db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))]
            items = [i for i in items if i]
        else:
            items = db.fetchall("SELECT * FROM items")

        if not items:
            console.print("[yellow]No items to update.[/yellow]")
            return

        updated = failed = 0
        _SEALED_TYPES = {"booster_box", "blister", "sealed_set"}

        # ── Deduplicate: group items with identical market identity ────────────
        # Same name + set + language + condition + type → same price. Fetch once, apply all.
        import time as _time
        from collections import defaultdict
        groups: dict[tuple, list] = defaultdict(list)
        for item in items:
            key = (item["name"], item["set_code"] or "", item["language"] or "EN",
                   item["condition"] or "", item["item_type"])
            groups[key].append(item)

        import random as _random
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as prog:
            _group_idx = 0
            for group_items in groups.values():
                if _group_idx > 0:
                    _time.sleep(_random.uniform(2.0, 3.5))
                _group_idx += 1
                representative = group_items[0]
                ids = [i["id"] for i in group_items]
                qty = len(ids)
                label = representative["name"][:40]
                if qty > 1:
                    label += f" ×{qty}"
                task = prog.add_task(f"[cyan]{label}[/cyan]", total=None)
                saved = False

                # ── CardMarket ────────────────────────────────────────────────
                known_url = (representative["cardmarket_url"]
                             if "cardmarket_url" in representative.keys() else None)
                cm = get_card_prices(
                    representative["name"], representative["set_code"],
                    representative["card_number"], representative["language"],
                    representative["item_type"], known_url=known_url,
                    condition=representative["condition"],
                )
                for ptype in ("trend", "low", "market"):
                    if cm.get(ptype):
                        for iid in ids:
                            db.execute(
                                "INSERT INTO price_snapshots "
                                "(item_id, source, price_type, price, url) VALUES (?,?,?,?,?)",
                                (iid, "cardmarket", ptype, cm[ptype], cm.get("url")),
                            )
                        saved = True
                # Cache the hit URL + image on all items in the group
                if cm.get("url"):
                    for iid in ids:
                        db.execute("UPDATE items SET cardmarket_url = ? WHERE id = ?",
                                   (cm["url"], iid))
                if cm.get("img"):
                    for iid in ids:
                        db.execute("UPDATE items SET cardmarket_img = ? WHERE id = ?",
                                   (cm["img"], iid))

                # ── eBay sold comps ────────────────────────────────────────────
                query = (f"One Piece {representative['card_number'] or ''} "
                         f"{representative['name']}").strip()
                sold = search_sold_listings(query, max_results=5)
                avg  = None
                if sold:
                    avg = sum(l["price"] for l in sold) / len(sold)
                    for iid in ids:
                        db.execute(
                            "INSERT INTO price_snapshots "
                            "(item_id, source, price_type, price) VALUES (?,?,?,?)",
                            (iid, "ebay", "sold_avg", avg),
                        )
                    saved = True

                prog.remove_task(task)

                # For sealed products prefer "low" (language-filtered min listing)
                # over "trend" (global cross-language average)
                is_sealed = representative["item_type"] in _SEALED_TYPES
                cm_price  = cm.get("low") or cm.get("trend")
                cm_label  = "CM low" if cm.get("low") else "CM trend"

                id_range = f"#{ids[0]}" if qty == 1 else f"#{ids[0]}–#{ids[-1]}"
                if saved:
                    updated += qty
                    parts = []
                    if cm_price:  parts.append(f"{cm_label}: {cm_price:.2f} €")
                    if avg:       parts.append(f"eBay avg: {avg:.2f} €")
                    console.print(
                        f"  [green]✓[/green] {id_range} {representative['name'][:35]:<35}"
                        f"  {'  |  '.join(parts)}"
                    )
                else:
                    failed += qty
                    err = cm.get("error") or "No prices found on CardMarket for this query"
                    console.print(
                        f"  [red]✗[/red] {id_range} {representative['name'][:35]:<35}  {err}\n"
                        f"     [dim]→ set manually: optcg price set {ids[0]} <price>[/dim]"
                    )

                # Throttle between groups to avoid CM rate-limiting
                if len(groups) > 1:
                    _time.sleep(1.0)

    # Auto-export OUTSIDE the transaction — if this fails it must not rollback
    # the price inserts that were just committed.
    _regen_dashboard()

    console.print(
        f"\n[green]Updated {updated}[/green]  [red]Failed {failed}[/red]"
        f"  [dim]CSVs → {EXPORTS_DIR}[/dim]"
    )


@price.command("set")
@click.argument("item_id",    type=int)
@click.argument("price_value", type=float)
@click.option("--type", "price_type", default="manual",
              type=click.Choice(["manual", "trend", "low", "market"]), show_default=True)
def price_set(item_id, price_value, price_type):
    """Manually set a price for an item (use when scraping fails).

    \b
    Examples:
      optcg price set 3 45.99
      optcg price set 3 45.99 --type trend
    """
    with db_conn() as conn:
        db = Database(conn)
        if not db.fetchone("SELECT id FROM items WHERE id = ?", (item_id,)):
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)
        db.execute(
            "INSERT INTO price_snapshots (item_id, source, price_type, price) VALUES (?,?,?,?)",
            (item_id, "manual", price_type, price_value),
        )
    console.print(f"[green]✓[/green] #{item_id} price set to {price_value:.2f} EUR [{price_type}]")
    _regen_dashboard()


@price.command("set-url")
@click.argument("item_id", type=int)
@click.argument("url")
def price_set_url(item_id, url):
    """Cache the CardMarket product URL for an item (used as starting point for future updates).

    \b
    Example:
      optcg price set-url 8 "https://www.cardmarket.com/en/OnePiece/Products/Booster-Boxes/The-Azure-Seas-Seven-Booster-Box?language=1"
    """
    with db_conn() as conn:
        db = Database(conn)
        if not db.fetchone("SELECT id FROM items WHERE id = ?", (item_id,)):
            console.print(f"[red]Item #{item_id} not found.[/red]"); sys.exit(1)
        db.execute("UPDATE items SET cardmarket_url = ? WHERE id = ?", (url, item_id))
    console.print(f"[green]✓[/green] #{item_id} CardMarket URL cached.")


# ══════════════════════════════════════════════════════════════════════════════
# PORTFOLIO
# ══════════════════════════════════════════════════════════════════════════════

@main.command("portfolio")
@click.option("--by-set",  is_flag=True, default=False)
@click.option("--by-type", is_flag=True, default=False)
def portfolio_cmd(by_set, by_type):
    """Portfolio P&L summary.

    \b
    Examples:
      optcg portfolio                    # total invested / current value / P&L
      optcg portfolio --by-type          # breakdown by card/box/blister/etc.
      optcg portfolio --by-set           # breakdown by set (OP-01, OP-02, ...)
    """
    with db_conn() as conn:
        db  = Database(conn)
        summary = portfolio_summary(db)

    color = "green" if summary["total_pnl"] >= 0 else "red"
    sign  = "+" if summary["total_pnl"] >= 0 else ""

    body = "\n".join([
        f"[bold]Total Invested:[/bold]  [cyan]{summary['total_invested']:.2f} EUR[/cyan]",
        f"[bold]Current Value:[/bold]   [bold]{summary['total_current_value']:.2f} EUR[/bold]",
        f"[bold]Total P&L:[/bold]       [{color}]{sign}{summary['total_pnl']:.2f} EUR  "
        f"({sign}{summary['total_pnl_pct']:.1f}%)[/{color}]",
        "",
        f"[bold]Items:[/bold]           {summary['item_count']} total  "
        f"[green]{summary['items_with_price']} priced[/green]  "
        f"[dim]{summary['items_missing_price']} without price[/dim]",
    ])
    console.print(Panel(body, title="Portfolio Summary", border_style=color))

    if by_type and summary["by_type"]:
        t = Table("Type", "Items", "Invested €", box=box.SIMPLE, header_style="bold")
        for name, d in sorted(summary["by_type"].items()):
            t.add_row(name, str(d["count"]), f"{d['cost']:.2f}")
        console.print(t)

    if by_set and summary["by_set"]:
        t = Table("Set", "Items", "Invested €", box=box.SIMPLE, header_style="bold")
        for name, d in sorted(summary["by_set"].items()):
            t.add_row(name, str(d["count"]), f"{d['cost']:.2f}")
        console.print(t)


# ══════════════════════════════════════════════════════════════════════════════
# DEALS  (price comparator / steal finder)
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def deals():
    """Find cheap steals by comparing CardMarket vs eBay.

    \b
    Examples:
      optcg deals search "Monkey D. Luffy"
      optcg deals search "Zoro" --set OP-01 --discount 20 --lang JP
      optcg deals portfolio --discount 15 --top 10
    """


@deals.command("search")
@click.argument("query")
@click.option("--set",      "set_code",   default=None)
@click.option("--card-num", "card_number", default=None)
@click.option("--lang",     "-l",          default=None, type=_LANG_CHOICE)
@click.option("--graded",   is_flag=True,  default=False, help="Include grading company in query")
@click.option("--gc",       "grading_company", default=None, type=_GRADE_CHOICE)
@click.option("--discount", default=15,    type=int, show_default=True,
              help="Min % below CardMarket trend to flag as a deal")
@click.option("--results",  default=6,     type=int, show_default=True,
              help="Max eBay results per section")
def deals_search(query, set_code, card_number, lang, graded, grading_company,
                 discount, results):
    """
    Compare CardMarket vs eBay.com prices for any card/product.

    \b
    Examples:
      optcg deals search "Monkey D. Luffy" --set OP-01 --lang JP
      optcg deals search "OP01-001 Luffy" --discount 20
      optcg deals search "Romance Dawn Box" --set OP-01 --lang JP
    """
    from optcg.scrapers.cardmarket import get_card_prices
    from optcg.scrapers.ebay import search_sold_listings, find_deals

    header = f"[bold cyan]{query}[/bold cyan]"
    if set_code: header += f"  [dim]{set_code}[/dim]"
    if lang:     header += f"  [dim]{lang}[/dim]"
    console.print(f"\n{header}\n")

    # ── CardMarket ────────────────────────────────────────────────────────────
    with console.status("[green]CardMarket…[/green]"):
        cm = get_card_prices(query, set_code, card_number, lang)

    cm_trend = cm.get("trend")
    cm_low   = cm.get("low")
    if cm.get("error") and not cm_trend:
        console.print(f"[yellow]CardMarket:[/yellow] {cm['error']}")
    else:
        cm_lines = []
        if cm_trend:   cm_lines.append(f"[bold]Trend:[/bold]  [bold cyan]{cm_trend:.2f} EUR[/bold cyan]")
        if cm_low:     cm_lines.append(f"[bold]Low:[/bold]    [cyan]{cm_low:.2f} EUR[/cyan]")
        if cm.get("market"): cm_lines.append(f"[bold]Market:[/bold] [cyan]{cm['market']:.2f} EUR[/cyan]")
        if cm.get("url"):    cm_lines.append(f"[dim]{cm['url']}[/dim]")
        console.print(Panel("\n".join(cm_lines) if cm_lines else "[dim]No data[/dim]",
                            title="CardMarket", border_style="blue"))

    # ── eBay sold comps ───────────────────────────────────────────────────────
    from optcg.scrapers.ebay import _build_query as _eq
    ebay_q = _eq(query, set_code, card_number, lang, graded, grading_company)

    with console.status("[green]eBay sold listings…[/green]"):
        sold = search_sold_listings(ebay_q, max_results=results)

    if sold:
        prices     = [l["price"] for l in sold]
        sold_avg   = sum(prices) / len(prices)
        sold_min   = min(prices)
        sold_max   = max(prices)

        sold_tbl = Table("Title", "Price €", "Date", box=box.SIMPLE, header_style="bold",
                         show_header=True)
        for l in sold:
            sold_tbl.add_row(l["title"][:62], f"{l['price']:.2f}", l.get("date") or "—")

        spread_str = ""
        if cm_trend:
            spread = sold_avg - cm_trend
            c2 = "green" if spread >= 0 else "red"
            spread_str = f"  vs CM trend: [{c2}]{'+' if spread >= 0 else ''}{spread:.2f} €[/{c2}]"

        console.print(Panel(
            sold_tbl,
            title=f"eBay Sold ({len(sold)})  avg {sold_avg:.2f} €  "
                  f"[dim]({sold_min:.2f}–{sold_max:.2f})[/dim]{spread_str}",
            border_style="yellow",
        ))
    else:
        console.print("[yellow]eBay:[/yellow] No sold listings found")

    # ── Deal hunter ───────────────────────────────────────────────────────────
    market_price = cm_trend or (sold_avg if sold else None)
    if not market_price:
        console.print("[dim]Cannot run deal comparison — no market price available.[/dim]")
        return

    ceiling = market_price * (1 - discount / 100)
    with console.status(f"[green]Hunting eBay deals below {ceiling:.2f} € ({discount}% off {market_price:.2f} €)…[/green]"):
        deal_listings = find_deals(
            query, market_price,
            set_code=set_code, card_number=card_number, language=lang,
            graded=graded, grading_company=grading_company,
            discount_threshold=discount / 100,
        )

    if deal_listings:
        dt = Table("Title", "Price €", "Ship €", "Total €", "Discount", "URL",
                   box=box.SIMPLE, header_style="bold", show_header=True)
        for d in deal_listings[:results]:
            disc_col = "bright_green" if d["discount_pct"] >= 25 else "green"
            dt.add_row(
                d["title"][:55],
                f"{d['price']:.2f}",
                "free" if not d["shipping"] else f"{d['shipping']:.2f}",
                f"{d['total']:.2f}",
                f"[{disc_col}]-{d['discount_pct']:.0f}%[/{disc_col}]",
                (d.get("url") or "")[:60],
            )
        console.print(Panel(
            dt,
            title=f"[bold green] {len(deal_listings)} Deal(s) — {discount}%+ below {market_price:.2f} €[/bold green]",
            border_style="green",
        ))
    else:
        console.print(
            f"[dim]No active eBay listings found {discount}%+ below {market_price:.2f} €.[/dim]"
        )


@deals.command("portfolio")
@click.option("--discount", default=15, type=int, show_default=True,
              help="Min % below market price to flag")
@click.option("--top",      default=10, type=int, show_default=True,
              help="Show top N deals")
def deals_portfolio(discount, top):
    """
    Scan your portfolio for cheap eBay alternatives to items you already hold.

    Useful for identifying cards worth accumulating at below-market prices.

    \b
    Examples:
      optcg deals portfolio                   # 15% discount threshold, top 10
      optcg deals portfolio --discount 20     # only show 20%+ steals
      optcg deals portfolio --top 5           # show top 5 only
    """
    from optcg.scrapers.ebay import find_deals as _find_deals

    with db_conn() as conn:
        db    = Database(conn)
        items = db.fetchall("SELECT * FROM items")
        all_deals: list[dict] = []

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as prog:
            for item in items:
                t = prog.add_task(f"[dim]{item['name'][:45]}[/dim]", total=None)
                snap = db.fetchone(
                    "SELECT price FROM price_snapshots WHERE item_id = ? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (item["id"],),
                )
                market = snap["price"] if snap else item["purchase_price"]
                item_deals = _find_deals(
                    item["name"], market,
                    set_code=item["set_code"],
                    card_number=item["card_number"],
                    language=item["language"],
                    discount_threshold=discount / 100,
                )
                prog.remove_task(t)
                for d in item_deals[:2]:
                    all_deals.append({
                        "item_id":   item["id"],
                        "item_name": item["name"],
                        "set_code":  item["set_code"],
                        "language":  item["language"],
                        **d,
                    })

    if not all_deals:
        console.print(f"[dim]No deals found {discount}%+ below market across your portfolio.[/dim]")
        return

    all_deals.sort(key=lambda x: x["discount_pct"], reverse=True)
    tbl = Table(
        "Item", "Set", "Lang", "Market €", "eBay €", "Discount", "URL",
        box=box.ROUNDED, header_style="bold cyan", show_header=True,
    )
    for d in all_deals[:top]:
        dc = "bright_green" if d["discount_pct"] >= 25 else "green"
        tbl.add_row(
            f"#{d['item_id']} {d['item_name'][:32]}",
            d.get("set_code") or "—",
            d.get("language") or "—",
            f"{d['market_price']:.2f}",
            f"{d['total']:.2f}",
            f"[{dc}]-{d['discount_pct']:.0f}%[/{dc}]",
            (d.get("url") or "")[:55],
        )
    console.print(Panel(tbl,
        title=f"[bold green] Top {min(top, len(all_deals))} Portfolio Deals[/bold green]",
        border_style="green",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def watchlist():
    """Track cards you want to buy when the price is right.

    \b
    Examples:
      optcg watchlist add -n "Shanks" -s OP-01 --target 80.00
      optcg watchlist add -n "Big Mom" --card-num OP01-062 -l JP --target 30.00
      optcg watchlist list
      optcg watchlist check                   # fetch prices + show eBay deals
      optcg watchlist check --discount 20
      optcg watchlist remove 2
    """


@watchlist.command("add")
@click.option("--name",     "-n", required=True)
@click.option("--set",      "-s", "set_code",   default=None)
@click.option("--card-num",       "card_number", default=None)
@click.option("--variant",  "-v", default=None, help="e.g. V.3, Alt Art")
@click.option("--lang",     "-l", default=None,  type=_LANG_CHOICE)
@click.option("--target",         "target_price", type=float, default=None,
              help="Alert if price drops below this")
@click.option("--notes",          default=None)
def watchlist_add(name, set_code, card_number, variant, lang, target_price, notes):
    """Add a card to your watchlist.

    \b
    Examples:
      optcg watchlist add -n "Shanks" -s OP-01 --target 80.00
      optcg watchlist add -n "Kaido" --card-num OP01-060 -l JP
      optcg watchlist add -n "Big Mom" --target 25.00 --notes "want NM or better"
      optcg watchlist add -n "Luffy" --card-num OP01-001 --variant V.3 --target 200
    """
    with db_conn() as conn:
        db = Database(conn)
        row_id = db.lastrowid(
            "INSERT INTO watchlist (name, set_code, card_number, variant, language, target_price, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, set_code, card_number, variant, lang, target_price, notes),
        )
    vstr = f"  {variant}" if variant else ""
    console.print(f"[green]✓[/green] Watchlist #{row_id}: {name}{vstr}")


@watchlist.command("list")
def watchlist_list():
    """Show all watchlist entries."""
    with db_conn() as conn:
        db   = Database(conn)
        rows = db.fetchall("SELECT * FROM watchlist ORDER BY name")
    if not rows:
        console.print("[dim]Watchlist empty. Add with: optcg watchlist add --name '...'[/dim]")
        return
    tbl = Table("#", "Name", "Variant", "Set", "Card #", "Lang", "Target €", "Notes",
                box=box.ROUNDED, header_style="bold cyan")
    for r in rows:
        tbl.add_row(
            str(r["id"]), r["name"], r.get("variant") or "—",
            r["set_code"] or "—", r["card_number"] or "—",
            r["language"] or "—",
            f"{r['target_price']:.2f}" if r["target_price"] else "—",
            r["notes"] or "—",
        )
    console.print(tbl)


@watchlist.command("remove")
@click.argument("watch_id", type=int)
def watchlist_remove(watch_id):
    """Remove a watchlist entry."""
    with db_conn() as conn:
        db = Database(conn)
        db.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))
    console.print(f"[red]Removed[/red] watchlist #{watch_id}")


@watchlist.command("check")
@click.option("--discount", default=10, type=int, show_default=True,
              help="Min % below market to show eBay deals")
def watchlist_check(discount):
    """Fetch current prices for watchlist items and show any deals.

    \b
    Examples:
      optcg watchlist check               # checks all entries, 10% threshold
      optcg watchlist check --discount 20
    """
    from optcg.scrapers.cardmarket import get_card_prices
    from optcg.scrapers.ebay import find_deals as _find_deals

    with db_conn() as conn:
        db   = Database(conn)
        rows = db.fetchall("SELECT * FROM watchlist ORDER BY name")

    if not rows:
        console.print("[dim]Watchlist is empty.[/dim]")
        return

    for row in rows:
        vstr = f"  [magenta]{row['variant']}[/magenta]" if row["variant"] else ""
        console.print(f"\n[bold cyan]{row['name']}[/bold cyan]{vstr}"
                      f"  [dim]{row['set_code'] or ''} {row['card_number'] or ''}[/dim]")
        with console.status("Fetching…"):
            cm = get_card_prices(
                row["name"], row["set_code"], row["card_number"],
                row["language"], row["item_type"] or "card",
                known_url=row["cm_url"],
            )

        market = cm.get("trend")
        target = row["target_price"]

        if market:
            hit = target and market <= target
            col = "bold green" if hit else "white"
            line = f"  CM trend: [{col}]{market:.2f} EUR[/{col}]"
            if cm.get("low"):
                line += f"   low: {cm['low']:.2f} EUR"
            if target:
                flag = "  [bold green] BELOW TARGET![/bold green]" if hit \
                       else f"  [dim](target {target:.2f})[/dim]"
                line += flag
            console.print(line)

            ebay_deals = _find_deals(
                row["name"], market,
                row["set_code"], row["card_number"], row["language"],
                discount_threshold=discount / 100,
            )
            if ebay_deals:
                for d in ebay_deals[:2]:
                    console.print(
                        f"  [green] eBay: {d['total']:.2f} EUR  "
                        f"-{d['discount_pct']:.0f}%[/green]  {(d.get('url') or '')[:70]}"
                    )
            else:
                console.print(f"  [dim]No eBay deals {discount}%+ below market[/dim]")
        else:
            console.print(f"  [yellow]CardMarket failed:[/yellow] {cm.get('error', '')}")


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def export():
    """Export portfolio and price history to CSV.

    Files land in iCloud Drive → OnePieceTCG/exports/ and open directly
    in Numbers or Excel on iPhone/Mac.

    \b
    Examples:
      optcg export csv                    # export to iCloud exports folder
      optcg export csv --out ~/Desktop/portfolio.csv
      optcg export icloud                 # force-sync all CSVs to iCloud
    """


@export.command("csv")
@click.option("--out", default=None, type=click.Path(path_type=Path), help="Custom output path")
def export_csv(out):
    """Export portfolio CSV (and price history) to iCloud."""
    with db_conn() as conn:
        db = Database(conn)
        p = export_portfolio_csv(db, out)
        h = export_price_history_csv(db)
    console.print(f"[green]✓[/green] Portfolio:     {p}")
    console.print(f"[green]✓[/green] Price history: {h}")


@export.command("icloud")
def export_icloud():
    """Force export all CSVs to iCloud Drive."""
    with db_conn() as conn:
        db = Database(conn)
        p, h = auto_export(db)
    console.print(f"[green]✓[/green] Exported to: {p.parent}")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  (CardMarket cookie management)
# ══════════════════════════════════════════════════════════════════════════════

@main.group()
def config():
    """Configure optcg (CardMarket cookie, etc.).

    Cookies are auto-read from Arc browser on macOS.
    Use set-cookie only as a fallback when Arc is not installed.

    \b
    Examples:
      optcg config show                   # show current config
      optcg config cookie-help            # full instructions for manual setup
      optcg config set-cookie <value>     # paste cf_clearance from DevTools
      optcg config clear-cookie           # remove stored cookie
    """


@config.command("set-cookie")
@click.argument("cf_clearance_value")
def config_set_cookie(cf_clearance_value):
    """
    Store your CardMarket cf_clearance cookie so price scraping works.

    \b
    How to get the value:
      1. Open https://www.cardmarket.com in Chrome (it loads normally for you)
      2. Press F12 → Application tab → Cookies → https://www.cardmarket.com
      3. Find the row named  cf_clearance  and copy its Value column
      4. Run:  optcg config set-cookie  <paste-value-here>
    """
    from optcg.scrapers.cardmarket import set_cf_cookie
    set_cf_cookie(cf_clearance_value)
    console.print("[green]✓[/green] cf_clearance cookie saved.")
    console.print("[dim]Test it with: optcg price update --item-id <any-id>[/dim]")


@config.command("clear-cookie")
def config_clear_cookie():
    """Remove the stored cf_clearance cookie."""
    from optcg.scrapers.cardmarket import clear_cf_cookie
    clear_cf_cookie()
    console.print("[yellow]Cookie cleared.[/yellow]")


@config.command("show")
def config_show():
    """Show current configuration."""
    from optcg.scrapers.cardmarket import get_cf_cookie, _load_config
    data = _load_config()
    cf   = data.get("cf_clearance")
    saved = data.get("cf_clearance_saved", "—")
    console.print(Panel(
        f"[bold]cf_clearance:[/bold]  {'[green]set[/green]  (saved ' + saved + ')' if cf else '[red]NOT SET[/red] — CardMarket scraping will fail'}\n\n"
        f"[dim]Run  optcg config cookie-help  for setup instructions[/dim]",
        title="Config", border_style="dim",
    ))


@config.command("cookie-help")
def config_cookie_help():
    """Print step-by-step instructions for getting the CardMarket cookie."""
    console.print(Panel(
        "[bold]Why this is needed[/bold]\n"
        "CardMarket uses Cloudflare which blocks automated access. Your real\n"
        "browser has already passed the challenge — we just borrow its cookie.\n"
        "\n"
        "[bold]Steps (Chrome)[/bold]\n"
        "  1. Open [link=https://www.cardmarket.com]https://www.cardmarket.com[/link] in Chrome\n"
        "  2. Press [bold]F12[/bold] (or Cmd+Option+I on Mac)\n"
        "  3. Click the [bold]Application[/bold] tab\n"
        "  4. In the left panel: Storage → Cookies → https://www.cardmarket.com\n"
        "  5. Find the row named  [bold yellow]cf_clearance[/bold yellow]\n"
        "  6. Double-click the [bold]Value[/bold] column → Ctrl+A → Ctrl+C\n"
        "  7. Run:\n"
        "       [bold cyan]optcg config set-cookie <paste-here>[/bold cyan]\n"
        "\n"
        "[bold]Steps (Safari)[/bold]\n"
        "  1. Enable developer menu: Safari → Settings → Advanced → Show Dev menu\n"
        "  2. Open cardmarket.com → Develop → Show Web Inspector\n"
        "  3. Storage → Cookies → cardmarket.com → cf_clearance → copy Value\n"
        "  4. Same command as above\n"
        "\n"
        "[bold]How long it lasts[/bold]\n"
        "  Usually 1–7 days. Re-run step 7 if you start getting blocked again.\n"
        "  eBay price comps always work as a fallback (no cookie needed).",
        title="CardMarket Cookie Setup",
        border_style="cyan",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════════════

@main.command("stats")
def db_stats():
    """Show database and storage stats."""
    import os
    from optcg.config import DB_PATH
    with db_conn() as conn:
        db = Database(conn)
        n_items    = db.fetchone("SELECT COUNT(*) c FROM items")["c"]
        n_snaps    = db.fetchone("SELECT COUNT(*) c FROM price_snapshots")["c"]
        n_receipts = db.fetchone("SELECT COUNT(*) c FROM receipts")["c"]
        n_watch    = db.fetchone("SELECT COUNT(*) c FROM watchlist")["c"]
    db_size = os.path.getsize(str(DB_PATH)) / 1024 if DB_PATH.exists() else 0
    console.print(Panel(
        f"[bold]DB path:[/bold]        {DB_PATH}\n"
        f"[bold]DB size:[/bold]        {db_size:.1f} KB\n"
        f"[bold]Items:[/bold]          {n_items}\n"
        f"[bold]Price records:[/bold]  {n_snaps}\n"
        f"[bold]Receipts:[/bold]       {n_receipts}\n"
        f"[bold]Watchlist:[/bold]      {n_watch}\n"
        f"[bold]iCloud dir:[/bold]     {APP_DIR}",
        title="Stats", border_style="dim",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD (static HTML export)
# ══════════════════════════════════════════════════════════════════════════════

@main.command("tui")
def tui_cmd():
    """Interactive TUI dashboard (lazygit-style).

    \b
    Keys:
      ↑↓       navigate portfolio
      f        cycle type filter (all / singles / promos / boxes …)
      s        cycle sort (P&L € / P&L % / paid / date / name)
      u        update price for selected item
      r        reload all data
      1        focus portfolio panel
      2        focus detail panel
      3        focus P&L chart
      typing   live search (backspace to delete)
      Escape   clear search
      q        quit
    """
    from optcg.tui import run_tui
    run_tui()


@main.command("dashboard")
@click.option("--out", default=None, type=click.Path(path_type=Path),
              help="Custom output path (default: iCloud exports/dashboard.html)")
def dashboard_cmd(out):
    """Generate the HTML dashboard and open it.

    Also runs automatically after every `optcg price update --all`.

    \b
    Examples:
      optcg dashboard
      optcg dashboard --out ~/Desktop/portfolio.html
    """
    from optcg.export_html import generate_html
    with db_conn() as conn:
        db = Database(conn)
        path = generate_html(db, Path(out) if out else None)
    console.print(f"[green]✓[/green] Dashboard: {path}")
    _open_path(path)


# ══════════════════════════════════════════════════════════════════════════════
# CLI DASHBOARD  (terminal view)
# ══════════════════════════════════════════════════════════════════════════════

def _build_timeline(db: Database) -> list[dict]:
    """Portfolio value + invested per day, using last snapshot per item per day."""
    items_raw = db.fetchall("SELECT * FROM items")
    # Last-inserted snapshot per item per calendar day (trend/low/manual only)
    snaps = db.fetchall(
        "SELECT item_id, price, substr(fetched_at,1,10) AS date "
        "FROM price_snapshots "
        "WHERE id IN ("
        "  SELECT MAX(id) FROM price_snapshots "
        "  WHERE price_type IN ('trend','low','manual') "
        "  GROUP BY item_id, substr(fetched_at,1,10)"
        ") ORDER BY date ASC"
    )
    if not snaps:
        return []

    dates  = sorted({s["date"] for s in snaps})
    by_dt: dict[str, dict] = {}
    for s in snaps:
        by_dt.setdefault(s["date"], {})[s["item_id"]] = s["price"]

    rolling: dict = {}
    timeline: list[dict] = []
    for d in dates:
        rolling.update(by_dt[d])
        tv = ti = 0.0
        for item in items_raw:
            if item["purchase_date"] <= d and \
               (item["status"] if "status" in item.keys() else "owned") != "sold":
                ti += item["purchase_price"]
                tv += rolling.get(item["id"], item["purchase_price"])
        timeline.append({"date": d, "value": round(tv, 2), "invested": round(ti, 2)})
    return timeline


def _render_timeline_chart(timeline: list[dict], width: int = 64, height: int = 8) -> list[str]:
    """Render a multi-row line chart; returns list of Rich-markup strings."""
    if len(timeline) < 2:
        return []

    values   = [t["value"]    for t in timeline]
    invested = [t["invested"] for t in timeline]
    dates    = [t["date"]     for t in timeline]
    n        = len(timeline)

    all_vals = values + invested
    y_min = min(all_vals) * 0.97
    y_max = max(all_vals) * 1.03
    y_rng = y_max - y_min or 1.0

    # Column position for each data point (0 … width-1)
    col_pos = [round(i / (n - 1) * (width - 1)) for i in range(n)]

    def to_row(v: float) -> int:
        return max(0, min(height - 1,
                          height - 1 - round((v - y_min) / y_rng * (height - 1))))

    val_rows = [to_row(v) for v in values]
    inv_rows = [to_row(v) for v in invested]

    # Grid: (char, kind)  kind ∈ 'val','inv','empty'
    grid: list[list[tuple]] = [[(' ', 'empty')] * width for _ in range(height)]

    def _fill_line(rows_list, cols_list, kind):
        for i in range(len(cols_list)):
            r, c = rows_list[i], cols_list[i]
            mark = '●' if kind == 'val' else '─'
            grid[r][c] = (mark, kind)
            if i > 0:
                pr, pc = rows_list[i - 1], cols_list[i - 1]
                for cc in range(pc + 1, c):
                    t  = (cc - pc) / max(1, c - pc)
                    rr = round(pr + t * (r - pr))
                    rr = max(0, min(height - 1, rr))
                    dr = r - pr
                    if kind == 'val':
                        ch = '╱' if dr < 0 else ('╲' if dr > 0 else '─')
                    else:
                        ch = '─'
                    # Only overwrite empty cells for the invested line
                    if kind == 'val' or grid[rr][cc][1] == 'empty':
                        grid[rr][cc] = (ch, kind)

    _fill_line(inv_rows, col_pos, 'inv')
    _fill_line(val_rows, col_pos, 'val')

    LABEL_W = 9  # width of y-axis label + separator

    lines: list[str] = []
    for row in range(height):
        y_val = y_max - (row / max(1, height - 1)) * y_rng
        label = f"{y_val:>7,.0f} │"
        row_str = [f"[dim]{label}[/dim]"]
        for col in range(width):
            ch, kind = grid[row][col]
            if kind == 'val':
                row_str.append(f"[bold yellow]{ch}[/bold yellow]"
                                if ch == '●' else f"[yellow]{ch}[/yellow]")
            elif kind == 'inv':
                row_str.append(f"[dim]{ch}[/dim]")
            else:
                row_str.append(ch)
        lines.append("".join(row_str))

    # X-axis rule
    lines.append(f"[dim]{'':>7} └{'─' * width}[/dim]")

    # X-axis date labels — spread evenly, at most 5
    n_lbls = min(n, max(2, width // 10))
    lbl_idx = [round(i / (n_lbls - 1) * (n - 1)) for i in range(n_lbls)]
    x_chars = [' '] * width
    for li in lbl_idx:
        col   = col_pos[li]
        label = dates[li][5:]          # MM-DD
        start = max(0, min(width - len(label), col - len(label) // 2))
        for k, c in enumerate(label):
            if start + k < width:
                x_chars[start + k] = c
    lines.append(f"[dim]{'':>9}{''.join(x_chars)}[/dim]")

    # Legend
    lines.append(
        f"{'':>9}[yellow]●[/yellow] [dim]Value[/dim]   "
        f"[dim]─ Invested[/dim]"
    )
    return lines


@main.command("dash")
def dash_cmd():
    """Terminal dashboard — portfolio at a glance.

    \b
    Shows portfolio summary, all items with P&L, and a text P&L bar chart.
    """
    from rich.columns import Columns
    from rich.text import Text
    from datetime import datetime as _dt

    with db_conn() as conn:
        db = Database(conn)
        summary = portfolio_summary(db)
        items_raw = db.fetchall("SELECT * FROM items ORDER BY purchase_date DESC, id DESC")
        rows = [(item, item_pnl(item, db)) for item in items_raw]
        receipt_ids = {r["item_id"] for r in db.fetchall("SELECT DISTINCT item_id FROM receipts")}
        timeline = _build_timeline(db)

    unrealized     = summary["unrealized_pnl"] or 0
    unrealized_pct = summary["unrealized_pnl_pct"] or 0
    realized       = summary["realized_pnl"] or 0
    realized_pct   = summary["realized_pnl_pct"] or 0
    invested       = summary["active_invested"] or 0
    current        = summary["active_current_value"] or 0
    u_color        = "green" if unrealized >= 0 else "red"
    r_color        = "green" if realized   >= 0 else "red"
    u_sg           = "+" if unrealized >= 0 else ""
    r_sg           = "+" if realized   >= 0 else ""

    # ── Header ────────────────────────────────────────────────────────────────
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
    console.rule(f"[bold yellow]OPTCG Tracker[/bold yellow]  [dim]{now_str}[/dim]")

    # ── Stat panels ───────────────────────────────────────────────────────────
    def stat_panel(title, value, sub="", color="yellow"):
        return Panel(
            f"[bold {color}]{value}[/bold {color}]\n[dim]{sub}[/dim]",
            title=f"[dim]{title}[/dim]", border_style="dim",
            padding=(0, 2),
        )

    panels = [
        stat_panel("Invested",     f"{invested:,.2f} €",
                   f"{summary['item_count']} active item{'s' if summary['item_count']!=1 else ''}"),
        stat_panel("Current Value",f"{current:,.2f} €",
                   f"{summary['items_with_price']} priced"),
        stat_panel("Unrealized P&L", f"{u_sg}{unrealized:,.2f} €",
                   f"{u_sg}{unrealized_pct:.1f}%", u_color),
        stat_panel("Realized P&L",   f"{r_sg}{realized:,.2f} €",
                   f"{r_sg}{realized_pct:.1f}%  ·  {summary['sold_count']} sold", r_color),
    ]
    console.print(Columns(panels, equal=True, expand=True))

    console.print()

    # ── Items table ───────────────────────────────────────────────────────────
    TYPE_COLOR = {
        "card": "white", "promo": "magenta",
        "blister": "cyan", "booster_box": "blue", "sealed_set": "yellow",
    }
    tbl = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, expand=True)
    tbl.add_column("#",       style="dim",     width=4)
    tbl.add_column("Name",    min_width=26,    no_wrap=False)
    tbl.add_column("Type",    width=11)
    tbl.add_column("Set",     width=7)
    tbl.add_column("Lang",    width=5)
    tbl.add_column("Paid €",  justify="right", width=9)
    tbl.add_column("Now €",   justify="right", width=10)
    tbl.add_column("P&L €",   justify="right", width=11)
    tbl.add_column("P&L %",   justify="right", width=8)
    tbl.add_column("Date",    width=11)

    for item, p in rows:
        tc      = TYPE_COLOR.get(item["item_type"], "white")
        status  = item["status"] if "status" in item.keys() else "owned"
        pending = status == "pending"
        sold    = status == "sold"
        has_rcp = item["id"] in receipt_ids
        rcp_tag = " [blue dim][RCP][/blue dim]" if has_rcp else ""

        if sold:
            cur_s  = (f"[green]{p['current']:.2f}[/green]"
                      if p["current"] is not None else "[dim]—[/dim]")
            name_s = f"[dim strike]{item['name']}[/dim strike]{rcp_tag} [green dim][SOLD][/green dim]"
        elif pending:
            cur_s  = (f"[yellow]{p['current']:.2f}[/yellow]"
                      if p["current"] is not None else "[yellow]—[/yellow]")
            name_s = f"[yellow]⏳[/yellow] {item['name']}{rcp_tag}"
        else:
            cur_s  = (f"{p['current']:.2f}" if p["current"] is not None else "[dim]—[/dim]")
            name_s = f"{item['name']}{rcp_tag}"

        pnl_s = "[dim]—[/dim]"
        pct_s = "[dim]—[/dim]"
        if p["pnl"] is not None:
            c2    = "green" if p["pnl"] >= 0 else "red"
            sg2   = "+" if p["pnl"] >= 0 else ""
            pnl_s = f"[{c2}]{sg2}{p['pnl']:.2f}[/{c2}]"
            pct_s = f"[{c2}]{sg2}{p['pnl_pct']:.1f}%[/{c2}]"
            if sold:
                pct_s += " [green dim][R][/green dim]"

        tbl.add_row(
            str(item["id"]), name_s,
            f"[{tc}]{item['item_type']}[/{tc}]",
            item["set_code"] or "—", item["language"] or "EN",
            f"{item['purchase_price']:.2f}", cur_s, pnl_s, pct_s,
            item["purchase_date"],
        )

    console.print(tbl)
    console.print(f"[dim]{len(rows)} item(s)[/dim]")

    # ── Portfolio value over time ─────────────────────────────────────────────
    chart_lines = _render_timeline_chart(timeline, width=64, height=8)
    if chart_lines:
        console.print()
        console.rule("[dim]Portfolio value over time[/dim]")
        for line in chart_lines:
            console.print(line)

    # ── P&L bar chart (text) ──────────────────────────────────────────────────
    priced = [(item, p["pnl"]) for item, p in rows if p["pnl"] is not None]
    if priced:
        console.print()
        console.rule("[dim]P&L per item[/dim]")
        max_abs = max(abs(v) for _, v in priced) or 1
        BAR = 20
        for item, v in sorted(priced, key=lambda x: -x[1]):
            blen = int(abs(v) / max_abs * BAR)
            pad  = " " * (BAR - blen)   # pad after bar so value column aligns
            if v >= 0:
                bar  = f"[green]{'█' * blen}[/green]{pad}"
                val  = f"[green]+{v:.2f} €[/green]"
            else:
                bar  = f"[red]{'█' * blen}[/red]{pad}"
                val  = f"[red]{v:.2f} €[/red]"
            # Build disambiguating sub-label: set+card_number, language, condition
            sub_parts = []
            sc = item["set_code"] or ""
            cn = item["card_number"] or ""
            if sc and cn:
                sub_parts.append(f"{sc}-{cn}")
            elif sc:
                sub_parts.append(sc)
            if item["language"] and item["language"] != "EN":
                sub_parts.append(item["language"])
            if item["condition"] and item["item_type"] in ("card", "promo"):
                sub_parts.append(item["condition"])
            if item["variant"]:
                sub_parts.append(item["variant"])
            sub_plain = ("  " + " · ".join(sub_parts)) if sub_parts else ""
            sub_rich  = ("  [dim]" + " · ".join(sub_parts) + "[/dim]") if sub_parts else ""
            # Measure plain visible width and pad to fixed column so bars align
            LABEL_W  = 44
            id_prefix = f"#{item['id']} "         # e.g. "#19 "
            name_max  = LABEL_W - len(id_prefix) - len(sub_plain)
            name_part = item["name"][:max(8, name_max)]
            plain_label = f"{id_prefix}{name_part}{sub_plain}"
            pad = " " * max(0, LABEL_W - len(plain_label))
            rich_label  = f"[bold]{id_prefix}[/bold]{name_part}{sub_rich}"
            console.print(f"  {rich_label}{pad}  {bar}  {val}")

    console.rule()


if __name__ == "__main__":
    main()
