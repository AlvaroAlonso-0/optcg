"""
Interactive TUI dashboard — `optcg tui`.

Theme: textual-ansi + ansi_color=True
  • ALL Textual theme variables resolve to ansi_default (terminal's native colors)
  • Textual emits ANSI reset codes (\\x1b[49m) instead of RGB hex → WezTerm/iTerm
    shows its own background color exactly as configured
  • This is how lazygit achieves "invisible" theming: it uses tcell ColorDefault
    which is the same concept — emit no color code, let terminal decide

Images:
  • For WezTerm/iTerm2: native inline-image protocol written directly to TTY
    AFTER each Textual render pass (call_after_refresh loop).
    Since CardImage cells have ansi_default background, the native image
    layer from the terminal emulator shows through the blank cells.
  • Fallback: half-block truecolor art at actual widget dimensions

Layout
------
┌─ [summary bar] ──────────────────────────────────────────┐
├─ [filter / search] ──────────────────────────────────────┤
├─ [1] Portfolio list ──────┬─ [2] image │ detail ──────────┤
│  DataTable                │  native img / half-block  text│
│                           ├─ [3] P&L bars ────────────────┤
└──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import io
import os
import sys
import threading
from typing import Optional

from rich.text import Text
from rich.style import Style
from rich.color import Color

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Static


# ── Helpers ───────────────────────────────────────────────────────────────────

_TYPES   = ["all", "card", "promo", "booster_box", "blister", "sealed_set"]
_TLABELS = {
    "all": "All", "card": "Singles", "promo": "Promos",
    "booster_box": "Boxes", "blister": "Blisters", "sealed_set": "Sealed",
}
_SORTS   = ["pnl", "pct", "price", "date", "name"]
_SLABELS = {"pnl": "P&L€↓", "pct": "P&L%↓", "price": "Paid↓", "date": "Date↓", "name": "Name↑"}

_img_bytes_cache: dict[str, bytes] = {}


def _fmt(v, d=2):
    return f"{v:.{d}f}" if v is not None else "—"


def _pnl_mu(pnl, pct=None):
    if pnl is None:
        return "[dim]—[/dim]"
    s = "+" if pnl >= 0 else ""
    c = "green" if pnl >= 0 else "red"
    out = f"[{c}]{s}{pnl:.2f}[/{c}]"
    if pct is not None:
        out += f" [dim]({s}{pct:.1f}%)[/dim]"
    return out


def _fetch_img_bytes(url: str) -> Optional[bytes]:
    """Fetch image with CardMarket Referer header, cached in memory."""
    if url in _img_bytes_cache:
        return _img_bytes_cache[url]
    try:
        from optcg.scrapers.cardmarket import _make_session
        r = _make_session().get(
            url, headers={"Referer": "https://www.cardmarket.com/"}, timeout=10
        )
        if r.status_code == 200:
            _img_bytes_cache[url] = r.content
            return r.content
    except Exception:
        pass
    return None


def _supports_native() -> bool:
    from optcg.search import _supports_inline_images
    return _supports_inline_images()


def _write_native_image(fd: int, data: bytes, width_cols: int) -> None:
    """Write iTerm2/WezTerm inline image protocol directly to a TTY fd.
    doNotMoveCursor=1 keeps cursor in place so Textual's layout is undisturbed.
    Uses ST (ESC \\) as terminator — more widely supported than BEL in TUIs.
    """
    import base64
    b64 = base64.b64encode(data).decode()
    seq = (
        f"\x1b]1337;File=inline=1"
        f";width={width_cols}"
        f";preserveAspectRatio=1"
        f";doNotMoveCursor=1:{b64}\x1b\\"
    )
    os.write(fd, seq.encode())


def _price_key(item: dict) -> tuple:
    """Dedup key: same card/product = same name + set + number + language + condition + type."""
    return (
        item.get("name") or "",
        item.get("set_code") or "",
        item.get("card_number") or "",
        item.get("language") or "",
        item.get("condition") or "",
        item.get("item_type") or "",
    )


def _half_block_render(data: bytes, W: int, H: int) -> Text:
    """Render image as half-block truecolor art at W×H chars (aspect-preserved)."""
    from PIL import Image as PILImage
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        img = PILImage.open(io.BytesIO(data)).convert("RGB")
    src_w, src_h = img.size
    scale = min(W / src_w, (H * 2) / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(2, int(src_h * scale))
    if new_h % 2 != 0:
        new_h -= 1
    img = img.resize((new_w, new_h), PILImage.LANCZOS)
    pix = img.load()
    out = Text()
    pad = (W - new_w) // 2
    for row in range(0, new_h - 1, 2):
        if pad > 0:
            out.append(" " * pad)
        for col in range(new_w):
            r1, g1, b1 = pix[col, row]
            r2, g2, b2 = pix[col, row + 1]
            out.append("▄", Style(
                color=Color.from_rgb(r2, g2, b2),
                bgcolor=Color.from_rgb(r1, g1, b1),
            ))
        out.append("\n")
    return out


# ── Widgets ───────────────────────────────────────────────────────────────────

class SummaryBar(Static):
    def update_stats(self, summary: dict) -> None:
        inv  = summary["active_invested"]
        cur  = summary["active_current_value"]
        upnl = summary["unrealized_pnl"]
        upct = summary["unrealized_pnl_pct"]
        rpnl = summary["realized_pnl"]
        n    = summary["item_count"]
        su = "+" if upnl >= 0 else ""; cu = "green" if upnl >= 0 else "red"
        sr = "+" if rpnl >= 0 else ""; cr = "green" if rpnl >= 0 else "red"
        self.update(
            f"[bold]Invested:[/bold] {inv:.0f}€  "
            f"[bold]Value:[/bold] {cur:.0f}€  "
            f"[bold]Unrealized:[/bold] [{cu}]{su}{upnl:.0f}€ ({su}{upct:.1f}%)[/{cu}]  "
            f"[bold]Realized:[/bold] [{cr}]{sr}{rpnl:.0f}€[/{cr}]  "
            f"[dim]{n} active[/dim]"
        )


class KeyBar(Static):
    """Lazygit-style footer: [key] description pairs, terminal's own colors."""

    _PORTFOLIO_BINDINGS = [
        ("↑↓", "navigate"),
        ("⏎",  "open URL"),
        ("e",  "edit"),
        ("/",  "search"),
        ("f",  "filter"),
        ("s",  "sort"),
        ("p",  "chart"),
        ("u",  "update $"),
        ("a",  "update all"),
        ("w",  "wishlist"),
        ("r",  "reload"),
        ("q",  "quit"),
    ]

    _WISHLIST_BINDINGS = [
        ("↑↓", "navigate"),
        ("⏎",  "open CM"),
        ("n",  "new"),
        ("e",  "edit"),
        ("d",  "delete"),
        ("u",  "fetch price"),
        ("w",  "portfolio"),
        ("r",  "reload"),
        ("q",  "quit"),
    ]

    def on_mount(self) -> None:
        self._draw("portfolio")

    def set_mode(self, mode: str) -> None:
        self._draw(mode)

    def _draw(self, mode: str) -> None:
        bindings = self._WISHLIST_BINDINGS if mode == "wishlist" else self._PORTFOLIO_BINDINGS
        parts = []
        for key, desc in bindings:
            parts.append(f"[bold cyan]{key}[/bold cyan][dim] {desc}[/dim]")
        self.update("  " + "   ".join(parts))


class DetailPane(Static):
    def show(self, item: Optional[dict], pnl: Optional[dict]) -> None:
        if not item:
            self.update("[dim]↑↓ select item[/dim]")
            return
        status = item.get("status", "owned")
        lines  = [f"[bold]#{item['id']} {item['name']}[/bold]", ""]
        meta = []
        if item.get("set_code"):    meta.append(item["set_code"])
        if item.get("card_number"): meta.append(item["card_number"])
        if item.get("variant"):     meta.append(item["variant"])
        if item.get("language"):    meta.append(item["language"])
        if item.get("condition"):   meta.append(item["condition"])
        if meta:
            lines.append("[dim]" + " · ".join(meta) + "[/dim]")
        lines += [
            f"[dim]Type:[/dim]   {item['item_type'].replace('_',' ').title()}",
            f"[dim]Paid:[/dim]   {item['purchase_price']:.2f}€  [dim]({item['purchase_date']})[/dim]",
        ]
        if item.get("purchase_source"):
            lines.append(f"[dim]Source:[/dim] {item['purchase_source']}")
        if status == "sold":
            sp = item.get("sell_price"); sd = item.get("sell_date") or ""
            lines += ["", f"[green bold]✔ Sold[/green bold]  {_fmt(sp)}€  [dim]{sd}[/dim]"]
        elif status == "pending":
            lines.append("[yellow]⏳ Pending / pre-order[/yellow]")
        if pnl:
            cur = pnl.get("current"); p = pnl.get("pnl"); pct = pnl.get("pnl_pct")
            src = pnl.get("price_source",""); dt = pnl.get("price_date","")
            lines += [
                "",
                f"[dim]Now:[/dim]    {_fmt(cur)}€  [dim]{src} · {dt}[/dim]",
                f"[dim]P&L:[/dim]    " + _pnl_mu(p, pct),
            ]
        if item.get("notes"):
            lines += ["", f"[dim]{item['notes']}[/dim]"]
        self.update("\n".join(lines))

    def show_wishlist(self, item: Optional[dict], market_price: Optional[float] = None) -> None:
        if not item:
            self.update("[dim]↑↓ select item[/dim]")
            return
        lines = [f"[bold]#{item['id']} {item['name']}[/bold]", ""]
        meta = []
        if item.get("set_code"):    meta.append(item["set_code"])
        if item.get("card_number"): meta.append(item["card_number"])
        if item.get("variant"):     meta.append(f"[magenta]{item['variant']}[/magenta]")
        if item.get("language"):    meta.append(item["language"])
        if meta:
            lines.append("[dim]" + " · ".join(meta) + "[/dim]")
        tp = item.get("target_price")
        lines.append(f"[dim]Target:[/dim]  {f'{tp:.2f}€' if tp else '—'}")
        if market_price is not None:
            diff = market_price - tp if tp else None
            lines.append(f"[dim]Market:[/dim]  {market_price:.2f}€")
            if diff is not None:
                s = "+" if diff >= 0 else ""; c = "red" if diff >= 0 else "green"
                lines.append(f"[dim]Gap:[/dim]     [{c}]{s}{diff:.2f}€[/{c}]  [dim](vs target)[/dim]")
        lines.append(f"[dim]Added:[/dim]   {item.get('added_at','')[:10]}")
        if item.get("notes"):
            lines += ["", f"[dim]{item['notes']}[/dim]"]
        self.update("\n".join(lines))


class CardImage(Static):
    """Card image widget.

    WezTerm/iTerm2: uses iTerm2 inline protocol via a set_interval timer.
    A dedicated 150ms timer continuously redraws the image directly to the
    TTY fd, overriding whatever Textual renders in those cells.
    Because CardImage has ansi_default background (transparent), the native
    image layer shows through between timer ticks.

    Fallback: half-block truecolor art at actual widget dimensions.
    """

    _current_url:  Optional[str]   = None
    _current_data: Optional[bytes] = None
    _native_data:  Optional[bytes] = None
    _native_supported: Optional[bool] = None
    _tty_fd: Optional[int] = None

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)

    @property
    def _use_native(self) -> bool:
        if CardImage._native_supported is None:
            CardImage._native_supported = _supports_native()
        return CardImage._native_supported

    def on_mount(self) -> None:
        # Grab TTY fd once; paused until an image loads
        try:
            fd = sys.stdout.fileno()
            if os.isatty(fd):
                self._tty_fd = fd
        except Exception:
            pass
        if self._use_native and self._tty_fd is not None:
            self._timer = self.set_interval(0.15, self._tick, pause=True)

    def on_unmount(self) -> None:
        # Clear data so any in-flight timer tick is a no-op
        self._native_data = None
        self._current_data = None

    def clear(self) -> None:
        self._current_url  = None
        self._current_data = None
        self._native_data  = None
        self.update("")
        if self._use_native and self._tty_fd is not None:
            self._timer.pause()

    def show_url(self, url: Optional[str]) -> None:
        if url == self._current_url:
            return
        self._current_url  = url
        self._current_data = None
        self._native_data  = None
        if self._use_native and self._tty_fd is not None:
            self._timer.pause()
        if not url:
            self.update("[dim]no image[/dim]")
            return
        self.update("[dim]loading…[/dim]")
        app = self.app
        threading.Thread(target=self._load, args=(url, app), daemon=True).start()

    def _load(self, url: str, app) -> None:
        data = _fetch_img_bytes(url)
        if not data:
            app.call_from_thread(self.update, "[dim]unavailable[/dim]")
            return
        self._current_data = data
        if self._use_native and self._tty_fd is not None:
            self._native_data = data
            app.call_from_thread(self._start_timer)
        else:
            app.call_from_thread(self._render_halfblock)

    # ── Native protocol path ───────────────────────────────────────────────

    def _start_timer(self) -> None:
        self.update("")          # blank cells → ansi_default bg → transparent
        self._timer.resume()     # start ticking at 150ms intervals

    def _tick(self) -> None:
        """Timer callback: write image to TTY if data still set."""
        if not self._native_data or self._tty_fd is None:
            self._timer.pause()
            return
        region = self.content_region
        if region.width < 4 or region.height < 2:
            return
        col = region.x + 1     # ANSI cursor is 1-indexed
        row = region.y + 1
        os.write(self._tty_fd, f"\x1b[{row};{col}H".encode())
        _write_native_image(self._tty_fd, self._native_data, width_cols=region.width)

    # ── Half-block fallback ────────────────────────────────────────────────

    def _render_halfblock(self) -> None:
        if not self._current_data:
            return
        W = self.content_region.width
        H = self.content_region.height
        if W < 4 or H < 2:
            return
        try:
            self.update(_half_block_render(self._current_data, W, H))
        except Exception:
            self.update("[dim]render error[/dim]")

    def on_resize(self) -> None:
        if not self._native_data and self._current_data:
            self._render_halfblock()


class BottomPanel(Static):
    """P&L bars  ←p→  Price-over-time chart for selected item."""

    _view: str = "pnl"          # "pnl" | "history"
    _pnl_rows: list  = []
    _item: Optional[dict] = None

    # ── public API ────────────────────────────────────────────────────────

    def set_pnl(self, rows: list[tuple]) -> None:
        self._pnl_rows = rows
        if self._view == "pnl":
            self._draw_pnl()

    def set_item(self, item: Optional[dict]) -> None:
        self._item = item
        if self._view == "history":
            self._draw_history()

    def toggle(self) -> None:
        self._view = "history" if self._view == "pnl" else "pnl"
        self._redraw()

    # ── drawing ───────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        if self._view == "pnl":
            self._draw_pnl()
        else:
            self._draw_history()

    def _draw_pnl(self) -> None:
        priced = [(l, v) for l, v in self._pnl_rows if v is not None]
        if not priced:
            self.update("[dim]No prices yet — press [bold]u[/bold] to fetch[/dim]")
            return
        priced.sort(key=lambda x: -x[1])
        max_abs = max(abs(v) for _, v in priced) or 1
        BAR = 12; LW = 16
        lines = ["[dim]P&L per item  [bold]p[/bold]=history[/dim]"]
        for lbl, v in priced:
            pad = " " * max(0, LW - len(lbl[:LW]))
            s = "+" if v >= 0 else ""; c = "green" if v >= 0 else "red"
            blen = int(abs(v) / max_abs * BAR)
            bar = f"[{c}]{'█' * blen}[/{c}]" + " " * (BAR - blen)
            lines.append(f"[dim]{lbl[:LW]}[/dim]{pad} {bar} [{c}]{s}{v:.2f}€[/{c}]")
        self.update("\n".join(lines))

    def _draw_history(self) -> None:
        if not self._item:
            self.update("[dim]Select an item to view history[/dim]")
            return
        item_id   = self._item["id"]
        item_name = self._item["name"]
        paid      = self._item.get("purchase_price") or 0
        try:
            from optcg.db import get_connection, Database
            conn = get_connection()
            db   = Database(conn)
            rows = db.fetchall(
                "SELECT date(fetched_at) AS d, price_type, AVG(price) AS price "
                "FROM price_snapshots WHERE item_id=? AND price_type='trend' "
                "GROUP BY date(fetched_at) ORDER BY d",
                (item_id,),
            )
            conn.close()
        except Exception:
            self.update("[dim]error loading history[/dim]")
            return

        title = f"[dim]Trend history  [bold]p[/bold]=P&L[/dim]  {item_name[:24]}"
        if not rows:
            self.update(f"{title}\n[dim]No snapshots yet — press [bold]u[/bold][/dim]")
            return

        data   = [(r["d"], r["price"]) for r in rows]
        prices = [p for _, p in data]
        mn, mx = min(prices), max(prices)
        rng    = mx - mn or 1
        BAR    = 18
        lines  = [title]
        for d, p in data[-12:]:   # last 12 days
            blen = max(1, int((p - mn) / rng * BAR))
            c    = "green" if p >= paid else "red"
            bar  = f"[{c}]{'█' * blen}[/{c}]"
            lines.append(f"[dim]{d[5:]}[/dim]  {p:8.2f}  {bar}")

        if len(prices) >= 2:
            delta = prices[-1] - prices[0]
            s = "+" if delta >= 0 else ""
            c = "green" if delta >= 0 else "red"
            lines.append(
                f"\n[dim]Δ total[/dim] [{c}]{s}{delta:.2f}€[/{c}]"
                f"  [dim]{prices[0]:.2f} → {prices[-1]:.2f}[/dim]"
            )
        self.update("\n".join(lines))


# ── Step wizard modal ─────────────────────────────────────────────────────────

class StepModal(ModalScreen):
    """
    Two-mode step wizard:
      • add mode  — ↑↓ navigate fields, ctrl+s / ⏎-on-last to save
      • edit mode — opens a numbered list first; press 1-9 to jump to a field,
                    then ↑↓ navigate; esc in field view = back to list
    """

    CSS = """
    StepModal { align: center middle; }
    #step-box {
        width: 54; height: auto;
        border: round ansi_green; padding: 1 2;
        background: $background;
    }
    #step-title   { color: ansi_cyan; text-style: bold; }
    #step-meta    { color: ansi_default; }
    #step-body    { color: ansi_default; margin-top: 1; }
    #step-input   { margin-top: 0; margin-bottom: 0; }
    #step-hint    { color: ansi_default; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape",  "back_or_cancel", show=False),
        Binding("ctrl+s",  "save_all",       show=False),
        Binding("up",      "prev_step",      show=False),
        Binding("down",    "next_step",      show=False),
    ]

    def __init__(self, title: str, steps: list[dict], edit_mode: bool = False) -> None:
        """
        steps: list of dicts — id, label, default, placeholder, required
        edit_mode: if True, open the numbered field picker first
        """
        super().__init__()
        self._title     = title
        self._steps     = steps
        self._edit_mode = edit_mode
        self._idx       = 0
        self._view      = "select" if edit_mode else "field"
        # seed from defaults so ctrl+s from list works without visiting every field
        self._values: dict = {s["id"]: s.get("default", "") for s in steps}

    def compose(self) -> ComposeResult:
        with Vertical(id="step-box"):
            yield Label(self._title, id="step-title")
            yield Label("",          id="step-meta")
            yield Label("",          id="step-body")
            yield Input("",          id="step-input")
            yield Label("",          id="step-hint")

    def on_mount(self) -> None:
        if self._view == "select":
            self._show_select()
        else:
            self._show_field(0)

    # ── Select (list) view ────────────────────────────────────────────────────

    def _show_select(self) -> None:
        self._view = "select"
        inp = self.query_one("#step-input", Input)
        inp.display   = False
        inp.can_focus = False
        self.set_focus(None)          # give key events to the Screen, not Input
        self.query_one("#step-meta", Label).update("")
        lines = []
        for i, s in enumerate(self._steps):
            key = str(i + 1) if i < 9 else "0"   # 1-9, then 0 for 10th
            val = self._values.get(s["id"]) or "—"
            if len(val) > 22:
                val = val[:21] + "…"
            lines.append(
                f"  [bold cyan]{key}[/bold cyan]  "
                f"{s['label']:<22} [dim]{val}[/dim]"
            )
        self.query_one("#step-body", Label).update("\n".join(lines))
        self.query_one("#step-hint", Label).update(
            "[dim]1–9 / 0 jump to field   ctrl+s save   esc cancel[/dim]"
        )

    def on_key(self, event) -> None:
        if self._view != "select":
            return
        ch = event.character
        if not ch or not ch.isdigit():
            return
        n = int(ch)
        # 1-9 → index 0-8, 0 → index 9
        idx = (n - 1) if n != 0 else 9
        if 0 <= idx < len(self._steps):
            event.stop()
            self._show_field(idx)

    # ── Field view ────────────────────────────────────────────────────────────

    def _save_current(self) -> None:
        if self._view == "field":
            s = self._steps[self._idx]
            self._values[s["id"]] = self.query_one("#step-input", Input).value.strip()

    def _show_field(self, idx: int) -> None:
        self._save_current()
        self._view = "field"
        self._idx  = idx
        s   = self._steps[idx]
        n   = len(self._steps)
        opt = "" if s.get("required") else "  [dim](optional)[/dim]"

        self.query_one("#step-meta", Label).update(f"[dim]{idx + 1} / {n}[/dim]")
        self.query_one("#step-body", Label).update(f"[bold]{s['label']}[/bold]{opt}")

        back_hint = "   [dim]esc=list[/dim]" if self._edit_mode else ""
        self.query_one("#step-hint", Label).update(
            f"[dim]↑↓ navigate   ctrl+s save   ⏎ next{back_hint}[/dim]"
        )

        inp             = self.query_one("#step-input", Input)
        inp.can_focus   = True
        inp.display     = True
        inp.placeholder = s.get("placeholder", "")
        inp.value       = self._values.get(s["id"], "") or ""
        inp.focus()

    # ── Navigation actions ────────────────────────────────────────────────────

    def action_prev_step(self) -> None:
        if self._view != "field" or self._idx == 0:
            return
        self._show_field(self._idx - 1)

    def action_next_step(self) -> None:
        if self._view != "field" or self._idx >= len(self._steps) - 1:
            return
        self._show_field(self._idx + 1)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        if self._view != "field":
            return
        s   = self._steps[self._idx]
        val = self.query_one("#step-input", Input).value.strip()
        if s.get("required") and not val:
            self.query_one("#step-hint", Label).update(
                "[red]Required — fill in a value[/red]"
            )
            return
        self._values[s["id"]] = val
        if self._idx < len(self._steps) - 1:
            self._show_field(self._idx + 1)
        else:
            self.dismiss(self._values)

    def action_save_all(self) -> None:
        self._save_current()
        # Validate required fields
        for s in self._steps:
            if s.get("required") and not self._values.get(s["id"]):
                self._show_field(self._steps.index(s))
                self.query_one("#step-hint", Label).update(
                    f"[red]{s['label']} is required[/red]"
                )
                return
        self.dismiss(self._values)

    def action_back_or_cancel(self) -> None:
        if self._view == "field" and self._edit_mode:
            self._save_current()
            self._show_select()
        else:
            self.dismiss(None)


def _edit_item_steps(item: dict) -> list[dict]:
    """Build step list for portfolio item edit."""
    i = item
    return [
        dict(id="language",        label="Language",        default=i.get("language") or "",         placeholder="EN / JP / …"),
        dict(id="condition",       label="Condition",       default=i.get("condition") or "",         placeholder="M / NM / EX / GD / LP / PL / P"),
        dict(id="purchase_price",  label="Purchase price (€)", default=str(i.get("purchase_price") or ""), required=True, placeholder="e.g. 12.50"),
        dict(id="purchase_date",   label="Purchase date",   default=i.get("purchase_date") or "",     required=True, placeholder="YYYY-MM-DD"),
        dict(id="purchase_source", label="Source",          default=i.get("purchase_source") or "",   placeholder="cardmarket / ebay / …"),
        dict(id="status",          label="Status",          default=i.get("status") or "owned",       required=True, placeholder="owned / sold / pending"),
        dict(id="sell_price",      label="Sell price (€)",  default=str(i.get("sell_price") or ""),   placeholder="leave blank if not sold"),
        dict(id="sell_date",       label="Sell date",       default=i.get("sell_date") or "",         placeholder="YYYY-MM-DD"),
        dict(id="sell_source",     label="Sell source",     default=i.get("sell_source") or "",       placeholder="cardmarket / ebay / …"),
        dict(id="notes",           label="Notes",           default=i.get("notes") or "",             placeholder="free text"),
    ]


def _parse_edit_item_result(data: dict, item: dict) -> dict:
    try:    purchase_price = float(data["purchase_price"])
    except: purchase_price = item["purchase_price"]
    try:    sell_price = float(data["sell_price"]) if data.get("sell_price") else None
    except: sell_price = item.get("sell_price")
    return {
        "language":        data.get("language") or None,
        "condition":       data.get("condition") or None,
        "purchase_price":  purchase_price,
        "purchase_date":   data.get("purchase_date") or item["purchase_date"],
        "purchase_source": data.get("purchase_source") or None,
        "notes":           data.get("notes") or None,
        "status":          data.get("status") or "owned",
        "sell_price":      sell_price,
        "sell_date":       data.get("sell_date") or None,
        "sell_source":     data.get("sell_source") or None,
    }


# ── Wishlist step helpers ─────────────────────────────────────────────────────

def _wishlist_steps(item: Optional[dict] = None) -> list[dict]:
    w = item or {}
    return [
        dict(id="name",         label="Card / product name", default=w.get("name") or "",              required=True, placeholder="e.g. Monkey D. Luffy"),
        dict(id="set_code",     label="Set code",            default=w.get("set_code") or "",          placeholder="OP01 / PRB-01 / …"),
        dict(id="card_number",  label="Card number",         default=w.get("card_number") or "",       placeholder="OP01-001"),
        dict(id="variant",      label="Variant",             default=w.get("variant") or "",           placeholder="V.1 / V.2 / V.3 / Alt Art / …"),
        dict(id="language",     label="Language",            default=w.get("language") or "",          placeholder="EN / JP / …"),
        dict(id="target_price", label="Target price (€)",   default=str(w.get("target_price") or ""), placeholder="e.g. 4.50"),
        dict(id="notes",        label="Notes",               default=w.get("notes") or "",             placeholder="free text"),
    ]


def _parse_wishlist_result(data: dict) -> Optional[dict]:
    if not data.get("name"):
        return None
    try:    target = float(data["target_price"]) if data.get("target_price") else None
    except: target = None
    return {
        "name":         data["name"],
        "set_code":     data.get("set_code") or None,
        "card_number":  data.get("card_number") or None,
        "variant":      data.get("variant") or None,
        "language":     data.get("language") or None,
        "target_price": target,
        "notes":        data.get("notes") or None,
    }


# ── App ───────────────────────────────────────────────────────────────────────

class OptcgTUI(App):
    """
    lazygit-style TUI.

    textual-ansi theme + ansi_color=True = same "invisible" approach lazygit uses:
      • $background / $panel / $surface all resolve to ansi_default
      • Textual emits \\x1b[49m (ANSI default bg reset) instead of RGB codes
      • WezTerm/iTerm2 shows their own configured background — no imposed color
      • Borders in terminal-default fg color; focused panel gets green border
    """

    ENABLE_COMMAND_PALETTE = False
    TITLE = "optcg"

    CSS = """
    Screen { layout: vertical; }

    SummaryBar  { height: 1; padding: 0 1; }
    #filter-row { height: 1; padding: 0 1; }
    #filter-row Label { width: auto; }
    #search-input { width: 1fr; border: none; padding: 0 0; height: 1; }

    #main-row { height: 1fr; }

    /* ansi_white = visible border on any dark terminal (lazygit inactive border) */
    #portfolio  { width: 60%; border: round ansi_white; }
    #portfolio:focus { border: round ansi_green; }

    #right-col  { width: 40%; }

    #detail-row { height: 55%; border: round ansi_white; }
    #detail-row:focus-within { border: round ansi_green; }

    CardImage  { width: 45%; height: 1fr; padding: 0; border-right: solid ansi_white; }
    DetailPane { width: 1fr; height: 1fr; padding: 1 2; }

    BottomPanel { height: 45%; padding: 1 2; border: round ansi_white; }

    /* KeyBar: 1-line footer, transparent bg */
    KeyBar { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q",      "quit",             show=False),
        Binding("e",      "edit_item",        show=False),
        Binding("f",      "cycle_filter",     show=False),
        Binding("s",      "cycle_sort",       show=False),
        Binding("p",      "toggle_chart",     show=False),
        Binding("u",      "update_price",     show=False),
        Binding("a",      "update_all",       show=False),
        Binding("r",      "reload",           show=False),
        Binding("slash",  "enter_search",     show=False),
        Binding("escape", "exit_search",      show=False),
        Binding("w",      "toggle_view",      show=False),
        Binding("n",      "add_wishlist",     show=False),
        Binding("d",      "delete_wishlist",  show=False),
    ]

    filter_type: reactive[str] = reactive("all")
    sort_key:    reactive[str] = reactive("pnl")

    def __init__(self):
        super().__init__(ansi_color=True)  # emit ANSI default codes, not RGB
        self._all_rows: list[dict] = []
        self._visible:  list[dict] = []
        self._cursor:   int        = 0
        self._view_mode: str       = "portfolio"   # "portfolio" | "wishlist"
        self._wl_rows:  list[dict] = []
        self._wl_visible: list[dict] = []
        self._wl_cursor: int       = 0

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield SummaryBar(id="summary")
        with Horizontal(id="filter-row"):
            yield Label("", id="filter-label")
            yield Input(placeholder="  / to search…", id="search-input")
        with Horizontal(id="main-row"):
            yield DataTable(id="portfolio", cursor_type="row", zebra_stripes=False)
            with Vertical(id="right-col"):
                with Horizontal(id="detail-row"):
                    yield CardImage(id="card-image")
                    yield DetailPane(id="detail")
                yield BottomPanel(id="bottom-panel")
        yield KeyBar(id="keybar")

    def on_mount(self) -> None:
        # textual-ansi: all theme vars → ansi_default → terminal's own background/fg
        # Must be set here (not as class attr) — only recognized as reactive setter
        self.theme = "textual-ansi"
        si: Input = self.query_one("#search-input")
        si.can_focus = False
        si.disabled  = True
        self._load_data()
        self._load_wishlist()
        self._build_table()
        self._refresh_right()
        self._update_filter_bar()
        self.query_one("#portfolio").focus()

    # ── Data ──────────────────────────────────────────────────────────────────

    def _load_data(self) -> None:
        from optcg.db import get_connection, Database
        from optcg.portfolio import item_pnl, portfolio_summary
        conn = get_connection()
        db   = Database(conn)
        items = db.fetchall("SELECT * FROM items")
        self._all_rows = [{"item": dict(i), "pnl": item_pnl(i, db)} for i in items]
        self._summary  = portfolio_summary(db)
        conn.close()
        self.query_one("#summary", SummaryBar).update_stats(self._summary)

    def _load_wishlist(self) -> None:
        from optcg.db import get_connection, Database
        conn = get_connection()
        db   = Database(conn)
        rows = db.fetchall("SELECT * FROM watchlist ORDER BY added_at DESC")
        self._wl_rows = [dict(r) for r in rows]
        conn.close()

    def _filtered_sorted(self) -> list[dict]:
        rows = self._all_rows
        if self.filter_type != "all":
            rows = [r for r in rows if r["item"]["item_type"] == self.filter_type]
        q = (self.query_one("#search-input", Input).value or "").lower().strip()
        if q:
            rows = [r for r in rows if
                    q in r["item"]["name"].lower() or
                    q in (r["item"]["card_number"] or "").lower() or
                    q in (r["item"]["set_code"] or "").lower()]
        def _sk(r):
            p = r["pnl"]
            if self.sort_key == "pnl":   return -(p["pnl"] or 0) if p else 0
            if self.sort_key == "pct":   return -(p["pnl_pct"] or 0) if p else 0
            if self.sort_key == "price": return -(r["item"]["purchase_price"] or 0)
            if self.sort_key == "date":  return -(r["item"]["purchase_date"] or "")
            if self.sort_key == "name":  return r["item"]["name"].lower()
            return 0
        return sorted(rows, key=_sk)

    # ── Table ─────────────────────────────────────────────────────────────────

    def _build_table(self) -> None:
        if self._view_mode == "wishlist":
            self._build_wishlist_table()
        else:
            self._build_portfolio_table()

    def _build_portfolio_table(self) -> None:
        tbl: DataTable = self.query_one("#portfolio")
        tbl.clear(columns=True)
        tbl.add_column("#",     width=4)
        tbl.add_column("Name",  width=26)
        tbl.add_column("Type",  width=7)
        tbl.add_column("Set",   width=9)
        tbl.add_column("Paid €",  width=9)
        tbl.add_column("Now €",   width=9)
        tbl.add_column("P&L €",   width=9)
        tbl.add_column("%",     width=7)
        self._visible = self._filtered_sorted()
        for r in self._visible:
            i = r["item"]; p = r["pnl"]
            status = i.get("status", "owned")
            name = i["name"][:26]
            if status == "sold":      name = f"[dim strike]{name}[/dim strike]"
            elif status == "pending": name = f"[yellow]⏳[/yellow] {name}"
            itype = {"booster_box":"Box   ","sealed_set":"Sealed",
                     "blister":"Blister","card":"Single","promo":"Promo "
                     }.get(i["item_type"], i["item_type"])
            paid_s = f"{i['purchase_price']:.2f}"
            cur_s  = _fmt(p["current"], 2) if p and p["current"] is not None else "—"
            pnl_s = pct_s = ""
            if p and p["pnl"] is not None:
                s = "+" if p["pnl"] >= 0 else ""; c = "green" if p["pnl"] >= 0 else "red"
                pnl_s = f"[{c}]{s}{p['pnl']:.2f}[/{c}]"
                pct_s = f"[{c}]{s}{p['pnl_pct']:.1f}%[/{c}]"
            tbl.add_row(str(i["id"]), name, itype,
                        i["set_code"] or "—",
                        paid_s, cur_s, pnl_s, pct_s)
        if self._visible and self._cursor < len(self._visible):
            tbl.move_cursor(row=self._cursor)

    def _build_wishlist_table(self) -> None:
        tbl: DataTable = self.query_one("#portfolio")
        tbl.clear(columns=True)
        tbl.add_column("#",        width=4)
        tbl.add_column("Name",     width=24)
        tbl.add_column("Variant",  width=7)
        tbl.add_column("Set",      width=8)
        tbl.add_column("Number",   width=11)
        tbl.add_column("Lang",     width=4)
        tbl.add_column("Target €", width=9)
        tbl.add_column("Notes",    width=16)
        self._wl_visible = list(self._wl_rows)
        for w in self._wl_visible:
            tp  = f"{w['target_price']:.2f}" if w.get("target_price") else "—"
            var = w.get("variant") or "—"
            tbl.add_row(
                str(w["id"]),
                w["name"][:24],
                f"[magenta]{var}[/magenta]" if w.get("variant") else "[dim]—[/dim]",
                w.get("set_code") or "—",
                w.get("card_number") or "—",
                w.get("language") or "—",
                tp,
                (w.get("notes") or "")[:16],
            )
        if self._wl_visible and self._wl_cursor < len(self._wl_visible):
            tbl.move_cursor(row=self._wl_cursor)

    def _refresh_right(self) -> None:
        if self._view_mode == "wishlist":
            self._refresh_right_wishlist()
        else:
            self._refresh_right_portfolio()

    def _refresh_right_portfolio(self) -> None:
        detail: DetailPane  = self.query_one("#detail")
        img:    CardImage   = self.query_one("#card-image")
        panel:  BottomPanel = self.query_one("#bottom-panel")
        if not self._visible:
            detail.show(None, None); img.clear()
            panel.set_pnl([]); panel.set_item(None); return
        idx = min(self._cursor, len(self._visible) - 1)
        row = self._visible[idx]
        detail.show(row["item"], row["pnl"])
        img.show_url(row["item"].get("cardmarket_img"))
        panel.set_item(row["item"])
        panel.set_pnl([(f"#{r['item']['id']} {r['item']['name'][:12]}",
                        r["pnl"]["pnl"] if r["pnl"] else None)
                       for r in self._visible])

    def _refresh_right_wishlist(self) -> None:
        detail: DetailPane  = self.query_one("#detail")
        img:    CardImage   = self.query_one("#card-image")
        panel:  BottomPanel = self.query_one("#bottom-panel")
        panel.set_pnl([])
        panel.set_item(None)
        if not self._wl_visible:
            detail.show_wishlist(None)
            img.clear()
            return
        idx = min(self._wl_cursor, len(self._wl_visible) - 1)
        w = self._wl_visible[idx]
        detail.show_wishlist(w)
        # Show cached image or kick off background fetch from cm_url
        if w.get("cardmarket_img"):
            img.show_url(w["cardmarket_img"])
        elif w.get("cm_url"):
            img.show_url(None)
            img.update("[dim]loading…[/dim]")
            app = self.app
            wid = w["id"]
            cm_url = w["cm_url"]
            def _fetch_wl_img():
                from optcg.search import _image_url_from_page
                img_url = _image_url_from_page(cm_url)
                if img_url:
                    from optcg.db import db_conn, Database
                    with db_conn() as conn:
                        Database(conn).execute(
                            "UPDATE watchlist SET cardmarket_img=? WHERE id=?",
                            (img_url, wid),
                        )
                    # Update in-memory row too so next hover is instant
                    for row in self._wl_rows:
                        if row["id"] == wid:
                            row["cardmarket_img"] = img_url
                    app.call_from_thread(img.show_url, img_url)
                else:
                    app.call_from_thread(img.update, "[dim]no image[/dim]")
            threading.Thread(target=_fetch_wl_img, daemon=True).start()
        else:
            img.clear()

    def _update_filter_bar(self) -> None:
        if self._view_mode == "wishlist":
            n = len(self._wl_visible)
            self.query_one("#filter-label", Label).update(
                f"[bold yellow]Wishlist[/bold yellow]  "
                f"[dim]{n} item{'s' if n!=1 else ''}[/dim]  "
                f"[dim]n=add  e=edit  d=delete  w=portfolio[/dim]  "
            )
        else:
            tl = _TLABELS.get(self.filter_type, self.filter_type)
            sl = _SLABELS.get(self.sort_key, self.sort_key)
            n  = len(self._visible)
            self.query_one("#filter-label", Label).update(
                f"[bold cyan]Portfolio[/bold cyan]  "
                f"[dim]type:[/dim][bold]{tl}[/bold]  "
                f"[dim]sort:[/dim][bold]{sl}[/bold]  "
                f"[dim]{n} item{'s' if n!=1 else ''}[/dim]  "
            )
        self.query_one("#keybar", KeyBar).set_mode(self._view_mode)

    # ── Events ────────────────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._view_mode == "wishlist":
            self._wl_cursor = event.cursor_row
        else:
            self._cursor = event.cursor_row
        self._refresh_right()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row → open CardMarket URL in browser."""
        if self._view_mode == "wishlist":
            if not self._wl_visible:
                return
            w = self._wl_visible[min(self._wl_cursor, len(self._wl_visible) - 1)]
            url = w.get("cm_url")
            if not url:
                self.notify("No CardMarket URL stored", severity="warning")
                return
            import webbrowser
            webbrowser.open(url)
            self.notify(f"[bold]{w['name']}[/bold] → browser", timeout=3)
            return
        if not self._visible:
            return
        idx  = event.cursor_row
        if idx >= len(self._visible):
            return
        item = self._visible[idx]["item"]
        url  = item.get("cardmarket_url") or item.get("ebay_url")
        if not url:
            self.notify("No URL stored for this item", severity="warning")
            return
        import webbrowser
        webbrowser.open(url)
        self.notify(f"[bold]#{item['id']}[/bold] {item['name']} → browser", timeout=3)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self._cursor = 0
            self._build_table()
            self._refresh_right()
            self._update_filter_bar()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_exit_search()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_enter_search(self) -> None:
        si: Input = self.query_one("#search-input")
        si.disabled  = False
        si.can_focus = True
        si.focus()

    def action_exit_search(self) -> None:
        si: Input = self.query_one("#search-input")
        si.value     = ""
        si.disabled  = True
        si.can_focus = False
        self._cursor = 0
        self._build_table()
        self._refresh_right()
        self._update_filter_bar()
        self.query_one("#portfolio").focus()

    def action_edit_item(self) -> None:
        """Open edit modal for the selected item (portfolio or wishlist)."""
        if self._view_mode == "wishlist":
            self._action_edit_wishlist()
            return
        if not self._visible:
            return
        item = self._visible[min(self._cursor, len(self._visible)-1)]["item"]

        def _on_dismiss(data: Optional[dict]) -> None:
            if not data:
                return
            parsed = _parse_edit_item_result(data, item)
            from optcg.db import db_conn, Database
            with db_conn() as conn:
                db = Database(conn)
                db.execute(
                    """UPDATE items SET
                        language=?, condition=?, purchase_price=?, purchase_date=?,
                        purchase_source=?, notes=?, status=?,
                        sell_price=?, sell_date=?, sell_source=?,
                        updated_at=datetime('now')
                       WHERE id=?""",
                    (parsed["language"], parsed["condition"], parsed["purchase_price"],
                     parsed["purchase_date"], parsed["purchase_source"], parsed["notes"],
                     parsed["status"], parsed["sell_price"], parsed["sell_date"],
                     parsed["sell_source"], item["id"]),
                )
            self.notify(f"#{item['id']} saved", timeout=3)
            self._load_data()
            self._build_table(); self._refresh_right(); self._update_filter_bar()

        self.push_screen(
            StepModal(f"[bold]#{item['id']}[/bold] {item['name'][:36]}", _edit_item_steps(item), edit_mode=True),
            _on_dismiss,
        )

    def _action_edit_wishlist(self) -> None:
        if not self._wl_visible:
            return
        w = self._wl_visible[min(self._wl_cursor, len(self._wl_visible)-1)]

        def _on_dismiss(data: Optional[dict]) -> None:
            if not data:
                return
            parsed = _parse_wishlist_result(data)
            if not parsed:
                return
            from optcg.db import db_conn, Database
            with db_conn() as conn:
                db = Database(conn)
                db.execute(
                    """UPDATE watchlist SET
                        name=?, set_code=?, card_number=?, variant=?,
                        language=?, target_price=?, notes=?
                       WHERE id=?""",
                    (parsed["name"], parsed["set_code"], parsed["card_number"],
                     parsed["variant"], parsed["language"], parsed["target_price"],
                     parsed["notes"], w["id"]),
                )
            self.notify(f"#{w['id']} updated", timeout=3)
            self._load_wishlist()
            self._build_table(); self._refresh_right(); self._update_filter_bar()

        self.push_screen(
            StepModal(f"[bold]Edit #{w['id']}[/bold] {w['name'][:36]}", _wishlist_steps(w), edit_mode=True),
            _on_dismiss,
        )

    def action_add_wishlist(self) -> None:
        """Add a new wishlist item (only active in wishlist mode)."""
        if self._view_mode != "wishlist":
            return

        def _on_dismiss(data: Optional[dict]) -> None:
            if not data:
                return
            parsed = _parse_wishlist_result(data)
            if not parsed:
                return
            from optcg.db import db_conn, Database
            with db_conn() as conn:
                db = Database(conn)
                db.execute(
                    """INSERT INTO watchlist (name, set_code, card_number, variant, language, target_price, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (parsed["name"], parsed["set_code"], parsed["card_number"],
                     parsed["variant"], parsed["language"], parsed["target_price"], parsed["notes"]),
                )
            self.notify(f"Added: {parsed['name']}", timeout=3)
            self._load_wishlist()
            self._build_table(); self._refresh_right(); self._update_filter_bar()

        self.push_screen(StepModal("[bold yellow]Add to Wishlist[/bold yellow]", _wishlist_steps()), _on_dismiss)

    def action_delete_wishlist(self) -> None:
        """Delete selected wishlist item (only active in wishlist mode)."""
        if self._view_mode != "wishlist" or not self._wl_visible:
            return
        w = self._wl_visible[min(self._wl_cursor, len(self._wl_visible)-1)]
        from optcg.db import db_conn, Database
        with db_conn() as conn:
            db = Database(conn)
            db.execute("DELETE FROM watchlist WHERE id=?", (w["id"],))
        self.notify(f"Removed: {w['name']}", timeout=3)
        self._wl_cursor = max(0, self._wl_cursor - 1)
        self._load_wishlist()
        self._build_table(); self._refresh_right(); self._update_filter_bar()

    def action_toggle_view(self) -> None:
        """Toggle between Portfolio and Wishlist view."""
        self._view_mode = "wishlist" if self._view_mode == "portfolio" else "portfolio"
        self._build_table()
        self._refresh_right()
        self._update_filter_bar()

    def action_toggle_chart(self) -> None:
        if self._view_mode != "portfolio":
            return
        self.query_one("#bottom-panel", BottomPanel).toggle()

    def action_cycle_filter(self) -> None:
        if self._view_mode != "portfolio":
            return
        idx = _TYPES.index(self.filter_type)
        self.filter_type = _TYPES[(idx + 1) % len(_TYPES)]
        self._cursor = 0
        self._build_table(); self._refresh_right(); self._update_filter_bar()

    def action_cycle_sort(self) -> None:
        if self._view_mode != "portfolio":
            return
        idx = _SORTS.index(self.sort_key)
        self.sort_key = _SORTS[(idx + 1) % len(_SORTS)]
        self._cursor = 0
        self._build_table(); self._refresh_right(); self._update_filter_bar()

    def action_reload(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load_data()
        self._load_wishlist()
        self._build_table(); self._refresh_right(); self._update_filter_bar()
        n = len(self._wl_rows) if self._view_mode == "wishlist" else len(self._visible)
        self.notify(f"Refreshed — {n} item{'s' if n != 1 else ''}", timeout=2)


    def action_update_price(self) -> None:
        """Update price for selected item — and all duplicates that share the same card."""
        if self._view_mode == "wishlist":
            self._action_fetch_wishlist_price()
            return
        if not self._visible:
            return
        selected = self._visible[min(self._cursor, len(self._visible)-1)]["item"]
        key = _price_key(selected)
        # All items (portfolio-wide) that are the same card
        siblings = [r["item"] for r in self._all_rows if _price_key(r["item"]) == key]
        ids = [i["id"] for i in siblings]
        extra = f" ({len(ids)} copies)" if len(ids) > 1 else ""
        self.notify(f"Fetching #{selected['id']}{extra}…")
        app = self.app
        def _do():
            from optcg.scrapers.cardmarket import get_card_prices
            from optcg.db import db_conn, Database
            try:
                cm = get_card_prices(
                    selected["name"], selected["set_code"], selected["card_number"],
                    selected["language"], selected["item_type"],
                    known_url=selected.get("cardmarket_url"),
                    condition=selected.get("condition"),
                )
                with db_conn() as conn:
                    db = Database(conn)
                    for item_id in ids:
                        for pt in ("trend", "low", "market"):
                            if cm.get(pt):
                                db.execute(
                                    "INSERT INTO price_snapshots"
                                    " (item_id,source,price_type,price,url) VALUES (?,?,?,?,?)",
                                    (item_id, "cardmarket", pt, cm[pt], cm.get("url")),
                                )
                        if cm.get("url"):
                            db.execute("UPDATE items SET cardmarket_url=? WHERE id=?",
                                       (cm["url"], item_id))
                app.call_from_thread(self._after_price, ids, cm)
            except Exception as exc:
                app.call_from_thread(self.notify, f"Failed: {exc}", severity="error")
        threading.Thread(target=_do, daemon=True).start()

    def _action_fetch_wishlist_price(self) -> None:
        """Fetch current CardMarket price for the selected wishlist item."""
        if not self._wl_visible:
            return
        w = self._wl_visible[min(self._wl_cursor, len(self._wl_visible)-1)]
        self.notify(f"Fetching price for {w['name'][:20]}…", timeout=4)
        app = self.app

        def _do():
            from optcg.scrapers.cardmarket import get_card_prices
            try:
                cm = get_card_prices(
                    w["name"], w.get("set_code"), w.get("card_number"),
                    w.get("language"), w.get("item_type") or "card",
                    known_url=w.get("cm_url"),
                )
                price = cm.get("low") or cm.get("trend")
                lbl   = "low" if cm.get("low") else "trend"
                if price:
                    # Cache any new url/img back to watchlist row
                    updates = {}
                    if cm.get("url"):    updates["cm_url"] = cm["url"]
                    if cm.get("img"):    updates["cardmarket_img"] = cm["img"]
                    if updates:
                        from optcg.db import db_conn, Database
                        sets = ", ".join(f"{k}=?" for k in updates)
                        with db_conn() as conn:
                            Database(conn).execute(
                                f"UPDATE watchlist SET {sets} WHERE id=?",
                                (*updates.values(), w["id"]),
                            )
                        for row in self._wl_rows:
                            if row["id"] == w["id"]:
                                row.update(updates)
                    app.call_from_thread(
                        self.notify,
                        f"{w['name'][:20]}: {lbl} {price:.2f}€"
                        + (f"  [dim]target {w['target_price']:.2f}€[/dim]" if w.get("target_price") else ""),
                        timeout=6,
                    )
                else:
                    app.call_from_thread(self.notify, f"{w['name'][:20]}: no price found", severity="warning", timeout=4)
            except Exception as exc:
                app.call_from_thread(self.notify, f"Failed: {exc}", severity="error", timeout=4)

        threading.Thread(target=_do, daemon=True).start()

    def action_update_all(self) -> None:
        """Fetch prices for all visible items — one fetch per unique card, applied to all copies."""
        if self._view_mode == "wishlist":
            return
        if not self._visible:
            return
        # Group by card key — fetch once, write to all item_ids with that key
        groups: dict[tuple, list[dict]] = {}
        for r in self._visible:
            k = _price_key(r["item"])
            groups.setdefault(k, []).append(r["item"])
        # Also include off-screen copies of the same cards from the full portfolio
        for r in self._all_rows:
            k = _price_key(r["item"])
            if k in groups and r["item"] not in groups[k]:
                groups[k].append(r["item"])

        n_unique = len(groups)
        n_total  = sum(len(v) for v in groups.values())
        self.notify(
            f"Updating {n_unique} unique card{'s' if n_unique>1 else ''}"
            f" ({n_total} item{'s' if n_total>1 else ''})…",
            timeout=4,
        )
        app = self.app
        def _do_all():
            from optcg.scrapers.cardmarket import get_card_prices
            from optcg.db import db_conn, Database
            done = 0
            for k, items in groups.items():
                rep  = items[0]   # representative item for the fetch
                ids  = [i["id"] for i in items]
                try:
                    cm = get_card_prices(
                        rep["name"], rep["set_code"], rep["card_number"],
                        rep["language"], rep["item_type"],
                        known_url=rep.get("cardmarket_url"),
                        condition=rep.get("condition"),
                    )
                    with db_conn() as conn:
                        db = Database(conn)
                        for item_id in ids:
                            for pt in ("trend", "low", "market"):
                                if cm.get(pt):
                                    db.execute(
                                        "INSERT INTO price_snapshots"
                                        " (item_id,source,price_type,price,url) VALUES (?,?,?,?,?)",
                                        (item_id, "cardmarket", pt, cm[pt], cm.get("url")),
                                    )
                            if cm.get("url"):
                                db.execute("UPDATE items SET cardmarket_url=? WHERE id=?",
                                           (cm["url"], item_id))
                    done += 1
                    copies = f" ×{len(ids)}" if len(ids) > 1 else ""
                    app.call_from_thread(
                        self.notify,
                        f"[dim]{done}/{n_unique}[/dim] {rep['name'][:20]}{copies}",
                        timeout=3,
                    )
                except Exception as exc:
                    app.call_from_thread(
                        self.notify, f"{rep['name'][:20]} failed: {exc}",
                        severity="warning", timeout=4,
                    )
            app.call_from_thread(self._finish_update_all, done, n_unique, n_total)
        threading.Thread(target=_do_all, daemon=True).start()

    def _finish_update_all(self, done: int, n_unique: int, n_total: int) -> None:
        self.notify(
            f"Done — {done}/{n_unique} cards, {n_total} items updated",
            severity="information", timeout=5,
        )
        self._load_data()
        self._build_table(); self._refresh_right(); self._update_filter_bar()

    def _after_price(self, ids: list[int], cm: dict) -> None:
        cm_p = cm.get("low") or cm.get("trend")
        lbl  = "CM low" if cm.get("low") else "CM trend"
        copies = f" ×{len(ids)}" if len(ids) > 1 else ""
        msg  = f"#{ids[0]}{copies} → {lbl} {cm_p:.2f}€" if cm_p else f"#{ids[0]} no price data"
        self.notify(msg, severity="information" if cm_p else "warning", timeout=4)
        self._load_data()
        self._build_table(); self._refresh_right(); self._update_filter_bar()


def run_tui() -> None:
    OptcgTUI().run()
