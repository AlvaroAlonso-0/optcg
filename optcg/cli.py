"""
optcg — One Piece TCG Investment Tracker
"""
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
) -> int:
    with db_conn() as conn:
        db = Database(conn)
        return db.lastrowid(
            """INSERT INTO items
               (item_type, name, set_code, card_number, language, condition,
                foil, variant, graded, grading_company, grade, cert_number,
                purchase_price, purchase_date, purchase_source, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_type, name, set_code, card_number, language, condition,
             int(foil), variant, int(graded), grading_company, grade, cert_number,
             price, purchase_date, source, notes),
        )


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
def add_card(name, set_code, card_number, lang, condition, foil, variant,
             graded, grading_company, grade, cert, price, purchase_date, source, notes):
    """Add a single card. Omit --name to search CardMarket interactively.

    \b
    Examples:
      optcg add card                                  # search & pick interactively
      optcg add card -n "Monkey D. Luffy" -s OP-01 -p 45.00
      optcg add card -n "Zoro" -s OP-01 -l JP -c NM -p 120.00 --foil
      optcg add card -n "Shanks" -p 200.00 --graded --gc PSA --grade 10 --cert 12345678
      optcg add card -n "Luffy" -p 30.00 --variant "Alt Art" --source CardMarket
    """
    # ── Interactive card search ────────────────────────────────────────────────
    if not name:
        query = click.prompt("Search CardMarket")
        result = pick_card(query, language=lang)
        if not result:
            console.print("[yellow]Cancelled.[/yellow]")
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

    row_id = _insert_item("card", name, set_code, card_number, lang.upper(), condition,
                           foil, variant, graded, grading_company, grade, cert,
                           price, purchase_date, source, notes)
    console.print(f"[green]✓[/green] Added card [bold]#{row_id}[/bold]: {name}")


@add.command("promo")
@click.option("--name",      "-n", default=None,   help="Card name (omit to search interactively)")
@click.option("--card-num",        "card_number",   default=None)
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
def add_promo(name, card_number, lang, condition, graded, grading_company,
              grade, cert, price, purchase_date, source, notes):
    """Add a promo card. Omit --name to search CardMarket interactively.

    \b
    Examples:
      optcg add promo                            # search & pick interactively
      optcg add promo -n "Monkey D. Luffy" --card-num P-043 -p 8.00
      optcg add promo -n "Nami" --card-num P-001 -l JP -p 25.00
    """
    if not name:
        query = click.prompt("Search CardMarket")
        result = pick_card(query, language=lang)
        if not result:
            console.print("[yellow]Cancelled.[/yellow]")
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
    row_id = _insert_item("promo", name, None, card_number, lang.upper(), condition,
                           False, None, graded, grading_company, grade, cert,
                           price, purchase_date, source, notes)
    console.print(f"[green]✓[/green] Added promo [bold]#{row_id}[/bold]: {name}")


@add.command("blister")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
def add_blister(name, set_code, lang, price, purchase_date, source, notes):
    """Add a blister pack."""
    row_id = _insert_item("blister", name, set_code, None, lang.upper(), "M",
                           False, None, False, None, None, None,
                           price, purchase_date, source, notes)
    console.print(f"[green]✓[/green] Added blister [bold]#{row_id}[/bold]: {name}")


@add.command("box")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
def add_box(name, set_code, lang, price, purchase_date, source, notes):
    """Add a booster box."""
    row_id = _insert_item("booster_box", name, set_code, None, lang.upper(), "M",
                           False, None, False, None, None, None,
                           price, purchase_date, source, notes)
    console.print(f"[green]✓[/green] Added booster box [bold]#{row_id}[/bold]: {name}")


@add.command("sealed")
@click.option("--name",  "-n", required=True)
@click.option("--set",   "-s", "set_code",  default=None)
@click.option("--lang",  "-l", default="EN", type=_LANG_CHOICE, show_default=True)
@click.option("--price", "-p", required=True, type=float)
@click.option("--date",  "-d", "purchase_date", default=str(date.today()))
@click.option("--source",      default=None)
@click.option("--notes",       default=None)
def add_sealed(name, set_code, lang, price, purchase_date, source, notes):
    """Add a sealed set / special product."""
    row_id = _insert_item("sealed_set", name, set_code, None, lang.upper(), "M",
                           False, None, False, None, None, None,
                           price, purchase_date, source, notes)
    console.print(f"[green]✓[/green] Added sealed [bold]#{row_id}[/bold]: {name}")


# ══════════════════════════════════════════════════════════════════════════════
# LIST
# ══════════════════════════════════════════════════════════════════════════════

@main.command("list")
@click.option("--type",    "item_type", default=None, type=_TYPE_CHOICE)
@click.option("--set",     "set_code",  default=None)
@click.option("--lang",                default=None)
@click.option("--graded",  "graded_only", is_flag=True, default=False)
@click.option("--sort",    default="date", type=_SORT_CHOICE, show_default=True)
def list_items(item_type, set_code, lang, graded_only, sort):
    """List portfolio items.

    \b
    Examples:
      optcg list                          # all items, newest first
      optcg list --sort pnl               # best P&L at top
      optcg list --type card --lang JP    # Japanese singles only
      optcg list --set OP-01              # Romance Dawn set only
      optcg list --graded                 # graded slabs only
      optcg list --sort price             # sorted by purchase price
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
        if graded_only:
            where.append("graded = 1")

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
            c = TYPE_COLOR.get(item["item_type"], "white")
            cur_str = f"{pnl['current']:.2f}" if pnl["current"] is not None else "[dim]—[/dim]"
            tbl.add_row(
                str(item["id"]),
                f"[{c}]{item['item_type']}[/{c}]",
                item["name"],
                item["set_code"] or "—",
                item["card_number"] or "—",
                item["language"] or "EN",
                _fmt_grade(item),
                f"{item['purchase_price']:.2f}",
                cur_str,
                _fmt_pnl(pnl["pnl"], pnl["pnl_pct"]),
                item["purchase_date"],
            )

        console.print(tbl)
        console.print(f"[dim]{len(rows)} item(s)[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════════════════════════

_SORT_CHOICE_SEARCH = click.Choice(list(SORT_OPTIONS.keys()), case_sensitive=False)


@main.command("search")
@click.argument("query")
@click.option("--lang",  "-l", default=None,       type=_LANG_CHOICE, help="Filter by language")
@click.option("--sort",  "-s", default="popular",  type=_SORT_CHOICE_SEARCH, show_default=True,
              help="popular | cheap | expensive | name | number | new | old")
@click.option("--page",  "-p", default=1,          type=int, show_default=True, help="Page number")
@click.option("--image", "-i", is_flag=True,       default=False,
              help="Prompt to show a card image inline (WezTerm/iTerm2/Kitty)")
def search_cmd(query: str, lang: str, sort: str, page: int, image: bool):
    """Search CardMarket for One Piece singles.

    \b
    Sort options: popular, cheap, expensive, name, number, new, old
    Examples:
      optcg search "Luffy" --sort cheap --page 2
      optcg search "Zoro" --sort new --image
    """
    from rich.table import Table
    from rich import box as rbox

    with console.status(
        f"[dim]Searching [bold]{query}[/bold]  [page {page}, sort: {sort}]…"
    ):
        results = search_cardmarket(query, language=lang, sort=sort, page=page)

    if not results:
        console.print("[yellow]No results.[/yellow]")
        return

    t = Table(box=rbox.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False)
    t.add_column("#",      style="dim",        width=3,  no_wrap=True)
    t.add_column("Name",   style="bold white", min_width=26)
    t.add_column("Number", style="cyan",       width=13, no_wrap=True)
    t.add_column("Set",    style="dim",        min_width=16)
    t.add_column("Var",    style="magenta",    width=5,  no_wrap=True)
    t.add_column("From",   style="green",      width=10, no_wrap=True)

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
        f"[dim]{len(results)} result(s) · page {page} · sorted by {sort}"
        f"  |  --page {page+1} for more, --sort [popular|cheap|expensive|name|number|new|old][/dim]"
    )

    if image:
        if not _supports_inline_images():
            console.print("[yellow]Inline images not supported in this terminal.[/yellow]")
            return
        idx_str = click.prompt("Show image for # (or Enter to skip)", default="")
        if idx_str.strip().isdigit():
            idx = int(idx_str.strip()) - 1
            if 0 <= idx < len(results):
                from optcg.search import fetch_card_image_bytes, _iterm2_inline, _write_to_tty
                with console.status("[dim]Loading image…"):
                    img_data = fetch_card_image_bytes(results[idx])
                # Write image AFTER status exits — avoids escape seq mangling
                if img_data:
                    _write_to_tty(_iterm2_inline(img_data, width_cols=30))
                else:
                    console.print(f"[dim]{results[idx].image_url}[/dim]")


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
def edit_item(item_id, name, condition, price, purchase_date, source, notes, variant, grade, cert, lang):
    """Edit fields on an existing item.

    \b
    Examples:
      optcg edit 3 --condition LP
      optcg edit 3 --price 55.00
      optcg edit 3 --grade 9.5 --cert 87654321
      optcg edit 3 --notes "bought at GP Madrid"
      optcg edit 3 --variant "Full Art"
    """
    field_map = [
        ("name", name), ("condition", condition), ("purchase_price", price),
        ("purchase_date", purchase_date), ("purchase_source", source),
        ("notes", notes), ("variant", variant), ("grade", grade),
        ("cert_number", cert), ("language", lang),
    ]
    updates = [(f, v) for f, v in field_map if v is not None]
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
    console.print(f"[green]✓[/green] Updated #{item_id}")


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
    subprocess.run(["open", str(path)], check=False)


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

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as prog:
            for item in items:
                task = prog.add_task(f"[cyan]{item['name'][:45]}[/cyan]", total=None)
                saved = False

                # ── CardMarket ────────────────────────────────────────────────
                cm = get_card_prices(
                    item["name"], item["set_code"], item["card_number"],
                    item["language"], item["item_type"],
                )
                for ptype in ("trend", "low", "market"):
                    if cm.get(ptype):
                        db.execute(
                            "INSERT INTO price_snapshots "
                            "(item_id, source, price_type, price, url) VALUES (?,?,?,?,?)",
                            (item["id"], "cardmarket", ptype, cm[ptype], cm.get("url")),
                        )
                        saved = True

                # ── eBay sold comps ────────────────────────────────────────────
                query = f"One Piece {item['card_number'] or ''} {item['name']}".strip()
                sold  = search_sold_listings(query, max_results=5)
                if sold:
                    avg = sum(l["price"] for l in sold) / len(sold)
                    db.execute(
                        "INSERT INTO price_snapshots "
                        "(item_id, source, price_type, price) VALUES (?,?,?,?)",
                        (item["id"], "ebay", "sold_avg", avg),
                    )
                    saved = True

                prog.remove_task(task)

                if saved:
                    updated += 1
                    parts = []
                    if cm.get("trend"): parts.append(f"CM trend: {cm['trend']:.2f} €")
                    if sold:            parts.append(f"eBay avg: {avg:.2f} €")
                    console.print(f"  [green]✓[/green] #{item['id']} {item['name'][:38]:<38}  {'  |  '.join(parts)}")
                else:
                    failed += 1
                    err = cm.get("error") or "no data"
                    console.print(f"  [red]✗[/red] #{item['id']} {item['name'][:38]:<38}  {err}")

        # Auto-export CSVs after updating
        auto_export(db)

    console.print(
        f"\n[green]Updated {updated}[/green]  [red]failed {failed}[/red]"
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
@click.option("--lang",     "-l", default=None,  type=_LANG_CHOICE)
@click.option("--target",         "target_price", type=float, default=None,
              help="Alert if price drops below this")
@click.option("--notes",          default=None)
def watchlist_add(name, set_code, card_number, lang, target_price, notes):
    """Add a card to your watchlist.

    \b
    Examples:
      optcg watchlist add -n "Shanks" -s OP-01 --target 80.00
      optcg watchlist add -n "Kaido" --card-num OP01-060 -l JP
      optcg watchlist add -n "Big Mom" --target 25.00 --notes "want NM or better"
    """
    with db_conn() as conn:
        db = Database(conn)
        row_id = db.lastrowid(
            "INSERT INTO watchlist (name, set_code, card_number, language, target_price, notes) "
            "VALUES (?,?,?,?,?,?)",
            (name, set_code, card_number, lang, target_price, notes),
        )
    console.print(f"[green]✓[/green] Watchlist #{row_id}: {name}")


@watchlist.command("list")
def watchlist_list():
    """Show all watchlist entries."""
    with db_conn() as conn:
        db   = Database(conn)
        rows = db.fetchall("SELECT * FROM watchlist ORDER BY name")
    if not rows:
        console.print("[dim]Watchlist empty. Add with: optcg watchlist add --name '...'[/dim]")
        return
    tbl = Table("#", "Name", "Set", "Card #", "Lang", "Target €", "Notes",
                box=box.ROUNDED, header_style="bold cyan")
    for r in rows:
        tbl.add_row(
            str(r["id"]), r["name"], r["set_code"] or "—", r["card_number"] or "—",
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
        console.print(f"\n[bold cyan]{row['name']}[/bold cyan]"
                      f"  [dim]{row['set_code'] or ''} {row['card_number'] or ''}[/dim]")
        with console.status("Fetching…"):
            cm = get_card_prices(row["name"], row["set_code"], row["card_number"], row["language"])

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


if __name__ == "__main__":
    main()
