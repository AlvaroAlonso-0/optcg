"""
Generate a self-contained HTML dashboard.

Desktop-first, tab-based layout. One Piece deep-sea dark theme.
All charts are inline SVG — zero external dependencies.
Progressive enhancement: tabs + search/sort via JS.
Without JS (Quick Look): all sections scroll vertically.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path

from optcg.db import Database

# ── Image cache ───────────────────────────────────────────────────────────────
_IMG_CACHE_DIR = Path.home() / ".config" / "optcg" / "img_cache"


def _fetch_img_b64(url: str) -> str | None:
    """Fetch a CardMarket image URL and return a base64 data URI (file-cached).

    CM product images on S3 require Referer: cardmarket.com — browsers
    opening a file:// page send no Referer and get 403. Embedding as a data
    URI makes the dashboard self-contained.
    """
    if not url:
        return None
    _IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _IMG_CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".b64")
    if cache_file.exists():
        return cache_file.read_text()
    try:
        from optcg.scrapers.cardmarket import _make_session
        r = _make_session().get(
            url,
            headers={"Referer": "https://www.cardmarket.com/"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        data_uri = f"data:{mime};base64,{base64.b64encode(r.content).decode()}"
        cache_file.write_text(data_uri)
        return data_uri
    except Exception:
        return None
from optcg.portfolio import item_pnl, portfolio_summary
from optcg.config import EXPORTS_DIR

# ── Palette ────────────────────────────────────────────────────────────────────
# Deep-sea indigo dark with warm gold accents
_BG    = "#0a0c15"
_SF    = "#10131f"
_SF2   = "#181c2e"
_SF3   = "#1f2440"
_BD    = "#272b42"
_GOLD  = "#f5c842"
_RED   = "#f87171"
_GREEN = "#4ade80"
_BLUE  = "#60a5fa"
_TX    = "#eef2ff"
_MT    = "#5a6480"

_PALETTE = [
    "#f5c842","#f87171","#60a5fa","#34d399","#a78bfa",
    "#fb923c","#4ade80","#f472b6","#38bdf8","#facc15",
    "#2dd4bf","#c084fc","#fb7185","#818cf8","#86efac",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;")
             .replace(">","&gt;").replace('"','&quot;'))

def _fmt(v) -> str:
    if v is None: return "—"
    return f"{float(v):,.2f}"

def _type_label(t: str) -> str:
    return {"card":"Single","promo":"Promo","blister":"Blister",
            "booster_box":"Booster Box","sealed_set":"Sealed Set"}.get(t, t.title())


# ── SVG: portfolio timeline ────────────────────────────────────────────────────

def _svg_line(timeline: list[dict]) -> str:
    if not timeline:
        return '<p class="empty">No price history yet — run <code>optcg price update --all</code></p>'

    W, H = 760, 220
    PL, PR, PT, PB = 60, 18, 18, 30
    IW, IH = W - PL - PR, H - PT - PB

    vals  = [d["value"]    for d in timeline]
    inv   = [d["invested"] for d in timeline]
    dates = [d["date"]     for d in timeline]
    n     = len(timeline)

    lo = min(min(vals), min(inv))
    hi = max(max(vals), max(inv))
    sp = (hi - lo) or 1
    lo -= sp * 0.06; hi += sp * 0.14; sp = hi - lo

    def px(i, v):
        return PL + (i / max(n - 1, 1)) * IW, PT + (1 - (v - lo) / sp) * IH

    def poly(series, color, dash=""):
        pts = " ".join(f"{px(i,v)[0]:.1f},{px(i,v)[1]:.1f}" for i, v in enumerate(series))
        da  = f'stroke-dasharray="{dash}"' if dash else ""
        return (f'<polyline points="{pts}" fill="none" stroke="{color}" '
                f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" {da}/>')

    def area(series, color):
        pts = " ".join(f"{px(i,v)[0]:.1f},{px(i,v)[1]:.1f}" for i, v in enumerate(series))
        lx, _ = px(n - 1, series[-1]); fx, _ = px(0, series[0]); by = PT + IH
        return (f'<polygon points="{pts} {lx:.1f},{by} {fx:.1f},{by}" '
                f'fill="{color}" fill-opacity="0.07"/>')

    grid = ""
    for k in range(6):
        v = lo + k / 5 * sp; _, y = px(0, v)
        grid += (f'<line x1="{PL}" y1="{y:.1f}" x2="{W-PR}" y2="{y:.1f}" '
                 f'stroke="{_BD}" stroke-width="1" stroke-dasharray="4 3"/>'
                 f'<text x="{PL-7}" y="{y+4:.1f}" text-anchor="end" '
                 f'fill="{_MT}" font-size="10" font-family="inherit">{v:,.0f}</text>')

    ticks = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}) if n > 4 else list(range(n))
    xlbls = ""
    for i in ticks:
        x, _ = px(i, lo)
        xlbls += (f'<text x="{x:.1f}" y="{H - 5}" text-anchor="middle" '
                  f'fill="{_MT}" font-size="10" font-family="inherit">{dates[i][5:]}</text>')

    legend = (
        f'<rect x="{W-100}" y="7" width="8" height="8" rx="2" fill="{_GOLD}"/>'
        f'<text x="{W-88}" y="16" fill="{_TX}" font-size="11" font-family="inherit" font-weight="500">Value</text>'
        f'<line x1="{W-54}" y1="11" x2="{W-42}" y2="11" stroke="{_MT}" stroke-width="2" stroke-dasharray="5 3"/>'
        f'<text x="{W-37}" y="16" fill="{_MT}" font-size="11" font-family="inherit">Cost</text>'
    )

    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}{area(vals,_GOLD)}{poly(inv,_MT,"5 4")}{poly(vals,_GOLD)}'
            f'{xlbls}{legend}</svg>')


# ── SVG: horizontal bars ───────────────────────────────────────────────────────

def _svg_hbars(labels, values, colors=None, fmt_val=None, max_rows=30) -> str:
    if not values:
        return '<p class="empty">No data yet</p>'
    labels, values = labels[:max_rows], values[:max_rows]
    ROW, W = 28, 660
    LW, VW = 160, 64
    BW = W - LW - VW
    mx      = max(abs(v) for v in values) or 1
    H       = ROW * len(values) + 10
    has_neg = any(x < 0 for x in values)
    bars    = ""
    for i, (lbl, v) in enumerate(zip(labels, values)):
        y     = i * ROW + 5
        color = (colors[i % len(colors)] if colors else None) or (_GREEN if v >= 0 else _RED)
        blen  = abs(v) / mx * BW
        ox    = LW + (BW / 2 if has_neg else 0)
        bx    = ox if v >= 0 else ox - blen
        vs    = fmt_val(v) if fmt_val else f"{v:,.2f}"
        vx    = bx + blen + 5 if v >= 0 else bx - 5
        van   = "start" if v >= 0 else "end"
        bars += (
            f'<text x="{LW-7}" y="{y+18}" text-anchor="end" fill="{_MT}" '
            f'font-size="11.5" font-family="inherit">{_esc(str(lbl)[:22])}</text>'
            f'<rect x="{bx:.1f}" y="{y+5}" width="{max(blen,2):.1f}" '
            f'height="{ROW-10}" rx="3" fill="{color}" fill-opacity="0.82"/>'
            f'<text x="{vx:.1f}" y="{y+18}" text-anchor="{van}" fill="{color}" '
            f'font-size="11.5" font-weight="600" font-family="inherit">{vs}</text>'
        )
    if has_neg:
        cx = LW + BW / 2
        bars += (f'<line x1="{cx:.1f}" y1="0" x2="{cx:.1f}" y2="{H}" '
                 f'stroke="{_BD}" stroke-width="1"/>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">{bars}</svg>')


def _svg_alloc_bars(by_type: dict, by_set: dict) -> tuple[str, str]:
    def make(d):
        if not d:
            return '<p class="empty">No data</p>'
        total = sum(d.values()) or 1
        lbl   = list(d.keys())
        vals  = [d[k] for k in lbl]
        cols  = _PALETTE[:len(lbl)]
        return _svg_hbars(lbl, vals, cols, fmt_val=lambda v: f"{v/total*100:.0f}%")
    return make(by_type), make(by_set)


# ── Data builder ───────────────────────────────────────────────────────────────

def _build(db: Database) -> dict:
    items_raw = db.fetchall("SELECT * FROM items ORDER BY purchase_date DESC, id DESC")
    summary   = portfolio_summary(db)

    # Which items have receipts
    receipt_rows = db.fetchall("SELECT DISTINCT item_id FROM receipts")
    receipt_ids  = {r["item_id"] for r in receipt_rows}

    items = []
    for item in items_raw:
        p = item_pnl(item, db)
        items.append({
            "id":            item["id"],
            "type":          item["item_type"],
            "type_label":    _type_label(item["item_type"]),
            "name":          item["name"],
            "set_code":      item["set_code"] or "",
            "card_number":   item["card_number"] or "",
            "language":      item["language"] or "EN",
            "condition":     item["condition"] or "",
            "variant":       item["variant"] or "",
            "graded":        bool(item["graded"]),
            "grading_company": item["grading_company"] or "",
            "grade":         item["grade"] or "",
            "purchase_price": p["cost"],
            "current_price": p["current"],
            "pnl":           p["pnl"],
            "pnl_pct":       p["pnl_pct"],
            "purchase_date": item["purchase_date"],
            "notes":         item["notes"] or "",
            "status":        item["status"] if "status" in item.keys() else "owned",
            "has_receipt":   item["id"] in receipt_ids,
            "sell_price":    item["sell_price"] if "sell_price" in item.keys() else None,
            "sell_date":     item["sell_date"]  if "sell_date"  in item.keys() else None,
            "sell_source":   item["sell_source"] if "sell_source" in item.keys() else None,
            "cardmarket_img": _fetch_img_b64(
                item["cardmarket_img"] if "cardmarket_img" in item.keys() else None
            ),
        })

    # Timeline
    snaps = db.fetchall(
        "SELECT item_id, price, fetched_at FROM price_snapshots ORDER BY fetched_at ASC"
    )
    timeline: list[dict] = []
    if snaps:
        dates  = sorted({s["fetched_at"][:10] for s in snaps})
        by_dt: dict[str, dict] = {}
        for s in snaps:
            by_dt.setdefault(s["fetched_at"][:10], {})[s["item_id"]] = s["price"]
        rolling: dict = {}
        for d in dates:
            rolling.update(by_dt.get(d, {}))
            tv = ti = 0.0
            for item in items_raw:
                if item["purchase_date"] <= d:
                    ti += item["purchase_price"]
                    tv += rolling.get(item["id"], item["purchase_price"])
            timeline.append({"date": d, "value": round(tv, 2), "invested": round(ti, 2)})

    # Allocation
    by_type: dict[str, float] = {}
    by_set:  dict[str, float] = {}
    for item in items_raw:
        tl = _type_label(item["item_type"])
        sc = item["set_code"] or "Other"
        by_type[tl] = by_type.get(tl, 0.0) + item["purchase_price"]
        by_set[sc]  = by_set.get(sc,  0.0) + item["purchase_price"]
    by_type = dict(sorted(by_type.items(), key=lambda x: -x[1]))
    by_set  = dict(sorted(by_set.items(),  key=lambda x: -x[1]))

    watchlist = [dict(r) for r in db.fetchall("SELECT * FROM watchlist ORDER BY name")]

    return dict(summary=summary, items=items, timeline=timeline,
                by_type=by_type, by_set=by_set, watchlist=watchlist)


# ── Row builders ───────────────────────────────────────────────────────────────

def _item_row(i: dict) -> str:
    pnl, pct = i["pnl"], i["pnl_pct"]
    has_p    = pnl is not None
    pending  = i.get("status") == "pending"
    sold     = i.get("status") == "sold"

    pnl_cls = ("pnl-pos" if pnl >= 0 else "pnl-neg") if has_p else "pnl-na"
    sg      = "+" if has_p and pnl >= 0 else ""
    pnl_s   = f"{sg}{_fmt(pnl)} €" if has_p else "—"
    pct_s   = f"{sg}{pct:.1f}%" if has_p else "—"
    cur_s   = f"{_fmt(i['current_price'])} €" if i["current_price"] is not None else "—"

    badges = f'<span class="badge b-type">{_esc(i["type_label"])}</span>'
    if sold:
        sell_date_s = (i.get("sell_date") or "")[:10]
        sell_src_s  = f" via {_esc(i['sell_source'])}" if i.get("sell_source") else ""
        badges += f' <span class="badge b-sold" title="Sold {sell_date_s}{sell_src_s}">✔ Sold</span>'
    elif pending:
        badges += ' <span class="badge b-pending">⏳ Pending</span>'
    if i["language"] and i["language"] != "EN":
        badges += f' <span class="badge b-lang">{_esc(i["language"])}</span>'
    if i["graded"]:
        badges += f' <span class="badge b-graded">{_esc(i["grading_company"])} {_esc(i["grade"])}</span>'
    if i.get("has_receipt"):
        badges += ' <span class="badge b-receipt" title="Receipt on file">📄 RCP</span>'

    # Don't show condition for sealed products — they're always mint/sealed
    _SEALED = {"booster_box", "blister", "sealed_set"}
    show_cond = i["condition"] if (not i["graded"] and i["type"] not in _SEALED) else ""
    meta = " · ".join(p for p in [i["card_number"], i["variant"], show_cond] if p)

    if sold:
        sell_date_s = (i.get("sell_date") or "")[:10]
        cur_cell = (f'<span style="color:{_GREEN};font-weight:600">{_esc(cur_s)}</span>'
                    f'<div class="tc-small tc-dim" style="margin-top:2px">{sell_date_s}</div>')
    elif pending:
        price_s  = cur_s if i["current_price"] is not None else "—"
        cur_cell = (f'<span style="color:{_GOLD};font-weight:600">{_esc(price_s)}</span>'
                    f'<div class="tc-small" style="color:{_GOLD};opacity:.6;margin-top:2px">pre-order</div>')
    else:
        cur_cell = f'<span class="price-cur">{_esc(cur_s)}</span>'

    # Sold rows: dim the name visually
    name_style = ' style="opacity:.55;text-decoration:line-through"' if sold else ""

    search   = _esc((i["name"]+" "+i["card_number"]+" "+i["set_code"]+" "+i["type_label"]).lower())
    name_key = _esc(i["name"].lower())

    img_attr = f' data-img="{_esc(i["cardmarket_img"])}"' if i.get("cardmarket_img") else ""
    return (
        f'<tr data-type="{i["type"]}" data-status="{i["status"]}" data-search="{search}" '
        f'data-name="{name_key}"{img_attr} '
        f'data-paid="{i["purchase_price"] or 0}" data-cur="{i["current_price"] or 0}" '
        f'data-pnl="{pnl or 0}" data-pct="{pct or 0}" data-date="{_esc(i["purchase_date"])}">'
        f'<td>'
        f'  <div class="item-name"{name_style}>{_esc(i["name"])}</div>'
        f'  {"<div class=&quot;item-meta&quot;>"+_esc(meta)+"</div>" if meta else ""}'
        f'  <div style="margin-top:6px">{badges}</div>'
        f'</td>'
        f'<td class="tc-dim">{_esc(i["set_code"]) or "—"}</td>'
        f'<td class="tc-right">{_fmt(i["purchase_price"])} €</td>'
        f'<td class="tc-right">{cur_cell}</td>'
        f'<td class="tc-right"><span class="{pnl_cls}">{_esc(pnl_s)}</span></td>'
        f'<td class="tc-right"><span class="{pnl_cls}">{_esc(pct_s)}</span></td>'
        f'<td class="tc-dim tc-small">{_esc(i["purchase_date"])}</td>'
        f'</tr>'
    )


def _wl_row(w: dict) -> str:
    meta = " · ".join(filter(None, [w.get("set_code"), w.get("card_number"), w.get("language")]))
    tp   = f'<span class="target-price">{_fmt(w["target_price"])} €</span>' if w.get("target_price") else "—"
    return (
        f'<tr>'
        f'<td><div class="item-name">{_esc(w["name"])}</div>'
        f'{"<div class=&quot;item-meta&quot;>"+_esc(meta)+"</div>" if meta else ""}</td>'
        f'<td class="tc-right">{tp}</td>'
        f'<td class="tc-dim tc-small">{str(w.get("added_at",""))[:10]}</td>'
        f'<td class="tc-dim tc-small">{_esc(w.get("notes") or "")}</td>'
        f'</tr>'
    )


# ── Main generator ─────────────────────────────────────────────────────────────

def generate_html(db: Database, out_path: Path | None = None) -> Path:
    d         = _build(db)
    s         = d["summary"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    u_pnl = s["unrealized_pnl"] or 0
    u_pct = s["unrealized_pnl_pct"] or 0
    u_sg  = "+" if u_pnl >= 0 else ""
    u_col = _GREEN if u_pnl >= 0 else _RED

    r_pnl = s["realized_pnl"] or 0
    r_pct = s["realized_pnl_pct"] or 0
    r_sg  = "+" if r_pnl >= 0 else ""
    r_col = _GREEN if r_pnl >= 0 else _RED

    def stat(label, value, sub, color=_TX, bar=_GOLD):
        return (f'<div class="stat" style="--bar:{bar}">'
                f'<div class="stat-label">{label}</div>'
                f'<div class="stat-value" style="color:{color}">{value}</div>'
                f'<div class="stat-sub">{sub}</div></div>')

    stats_html = (
        stat("Invested",        f"{_fmt(s['active_invested'])} €",
             f"{s['item_count']} active item{'s' if s['item_count']!=1 else ''}",
             _GOLD, _GOLD) +
        stat("Current Value",   f"{_fmt(s['active_current_value'])} €",
             f"{s['items_with_price']} priced",
             _TX, _BLUE) +
        stat("Unrealized P&amp;L", f"{u_sg}{_fmt(u_pnl)} €",
             f"{u_sg}{u_pct:.1f}%",
             u_col, u_col) +
        stat("Realized P&amp;L",   f"{r_sg}{_fmt(r_pnl)} €",
             f"{r_sg}{r_pct:.1f}%  ·  {s['sold_count']} sold",
             r_col, r_col)
    )

    timeline_svg                  = _svg_line(d["timeline"])
    alloc_type_svg, alloc_set_svg = _svg_alloc_bars(d["by_type"], d["by_set"])

    priced   = sorted([i for i in d["items"] if i["pnl"] is not None],
                      key=lambda x: x["pnl"], reverse=True)  # type: ignore
    pnl_lbls = [((i["card_number"] + " " if i["card_number"] else "") + i["name"])[:30] for i in priced]
    pnl_vals = [i["pnl"] for i in priced]  # type: ignore
    pnl_cols = [_GREEN if v >= 0 else _RED for v in pnl_vals]
    pnl_svg  = _svg_hbars(pnl_lbls, pnl_vals, pnl_cols,  # type: ignore
                           fmt_val=lambda v: f"{'+'if v>=0 else ''}{v:.0f}€")

    items_rows = "".join(_item_row(i) for i in d["items"]) if d["items"] else \
        f'<tr><td colspan="7" class="empty">No items — <code>optcg add card</code></td></tr>'

    if d["watchlist"]:
        wl_rows = "".join(_wl_row(w) for w in d["watchlist"])
    else:
        wl_rows = (f'<tr><td colspan="4" class="empty">Watchlist empty — '
                   f'<code>optcg watchlist add -n "Name" --target 80</code></td></tr>')

    # ── Inline CSS ────────────────────────────────────────────────────────────
    css = """
:root{
  --bg:#0a0c15;--sf:#10131f;--sf2:#181c2e;--sf3:#1f2440;--bd:#272b42;
  --gold:#f5c842;--red:#f87171;--green:#4ade80;--blue:#60a5fa;
  --tx:#eef2ff;--mt:#5a6480
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif;
  background:var(--bg);color:var(--tx);line-height:1.5;min-height:100vh;
  -webkit-text-size-adjust:100%
}

/* ── Top bar ── */
.topbar{
  position:sticky;top:0;z-index:200;
  background:rgba(10,12,21,.92);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--bd);
  display:flex;align-items:stretch;padding:0 40px;
  height:56px
}
.logo-area{
  display:flex;align-items:center;gap:10px;
  padding-right:28px;border-right:1px solid var(--bd);margin-right:4px
}
.logo-title{
  font-size:18px;font-weight:900;letter-spacing:-0.5px;
  background:linear-gradient(120deg,var(--gold) 30%,#f87c2a);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text
}
.logo-sub{font-size:9px;color:var(--mt);letter-spacing:1px;text-transform:uppercase;margin-top:1px}
.tabs{display:flex;align-items:stretch;gap:0}
.tab{
  display:flex;align-items:center;gap:7px;
  padding:0 18px;
  color:var(--mt);font-size:13px;font-weight:500;letter-spacing:.1px;
  background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;transition:color .15s,border-color .15s;
  -webkit-tap-highlight-color:transparent
}
.tab svg{width:15px;height:15px;stroke-width:1.8;flex-shrink:0}
.tab:hover{color:var(--tx)}
.tab.active{color:var(--gold);border-bottom-color:var(--gold)}
.topbar-right{
  margin-left:auto;display:flex;align-items:center;
  font-size:11px;color:var(--mt);padding-left:16px
}

/* ── Sections ── */
.content{padding:36px 40px;max-width:1300px;margin:0 auto}
.section{display:block} /* all visible by default; JS hides inactive */
.section+.section{margin-top:48px} /* only shows when no JS */
.sec-title{
  font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--mt);margin-bottom:20px;display:flex;align-items:center;gap:10px
}
.sec-title::after{content:'';flex:1;height:1px;background:var(--bd)}

/* ── Stat cards ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}
.stat{
  background:var(--sf);border:1px solid var(--bd);border-radius:14px;
  padding:20px 22px;position:relative;overflow:hidden
}
.stat::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--bar,var(--gold));border-radius:14px 14px 0 0
}
.stat-label{font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.9px;margin-bottom:8px}
.stat-value{font-size:28px;font-weight:800;letter-spacing:-.5px;line-height:1}
.stat-sub{font-size:12px;color:var(--mt);margin-top:8px}

/* ── Chart card ── */
.chart-card{
  background:var(--sf);border:1px solid var(--bd);border-radius:14px;padding:22px 24px
}
.chart-title{
  font-size:10px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;
  color:var(--mt);margin-bottom:16px
}
.alloc-row{display:grid;grid-template-columns:1fr 1fr;gap:16px}

/* ── Controls ── */
.controls{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.search-wrap{position:relative}
.search-wrap svg{
  position:absolute;left:12px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;stroke:var(--mt);stroke-width:2;pointer-events:none
}
.search-input{
  background:var(--sf);border:1px solid var(--bd);border-radius:9px;
  padding:9px 14px 9px 36px;color:var(--tx);font-size:13.5px;outline:none;
  width:270px;transition:border-color .15s,box-shadow .15s;font-family:inherit
}
.search-input:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(245,200,66,.1)}
.search-input::placeholder{color:var(--mt)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{
  padding:6px 14px;border-radius:20px;border:1px solid var(--bd);
  background:var(--sf);color:var(--mt);font-size:12px;cursor:pointer;
  transition:all .12s;white-space:nowrap;font-family:inherit;
  -webkit-tap-highlight-color:transparent
}
.chip:hover{border-color:var(--mt);color:var(--tx)}
.chip.on{
  background:rgba(245,200,66,.1);border-color:rgba(245,200,66,.5);
  color:var(--gold);font-weight:600
}
.result-count{font-size:12px;color:var(--mt);margin-left:auto}

/* ── Items table ── */
.tbl-wrap{overflow-x:auto;border:1px solid var(--bd);border-radius:14px}
.dtbl{width:100%;border-collapse:collapse;font-size:13px}
.dtbl th{
  padding:11px 16px;text-align:left;
  font-size:10px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--mt);background:var(--sf2);border-bottom:1px solid var(--bd);
  cursor:pointer;white-space:nowrap;user-select:none
}
.dtbl th:hover{color:var(--tx)}
.dtbl th.s-asc::after{content:" ↑";color:var(--gold)}
.dtbl th.s-desc::after{content:" ↓";color:var(--gold)}
.dtbl th.s-asc,.dtbl th.s-desc{color:var(--gold)}
.dtbl td{
  padding:12px 16px;border-bottom:1px solid rgba(39,43,66,.7);vertical-align:middle
}
.dtbl tr:last-child td{border-bottom:none}
.dtbl tbody tr:hover td{background:rgba(31,36,64,.5)}
.item-name{font-weight:600;color:var(--tx);font-size:14px}
.item-meta{font-size:11px;color:var(--mt);margin-top:3px}
.price-cur{font-weight:700;font-size:14px}
.pnl-pos{color:var(--green);font-weight:600}
.pnl-neg{color:var(--red);font-weight:600}
.pnl-na{color:var(--mt)}
.tc-right{text-align:right}
.tc-dim{color:var(--mt)}
.tc-small{font-size:12px}
.badge{
  display:inline-block;padding:3px 7px;border-radius:5px;
  font-size:10px;font-weight:700;text-transform:uppercase;margin-right:3px
}
.b-type{background:var(--sf3);color:var(--mt)}
.b-lang{background:rgba(245,200,66,.1);color:var(--gold)}
.b-pending{background:rgba(251,191,36,.1);color:#f5c842}
.b-graded{background:rgba(255,215,0,.1);color:#ffd700}
.b-receipt{background:rgba(96,165,250,.1);color:var(--blue)}
.b-sold{background:rgba(74,222,128,.12);color:var(--green)}
.target-price{color:var(--gold);font-weight:700;font-size:15px}

/* ── Misc ── */
.empty{color:var(--mt);text-align:center;padding:44px 16px;font-size:14px;line-height:1.9}
code{
  background:var(--sf2);border:1px solid var(--bd);border-radius:5px;
  padding:2px 7px;font-size:12.5px;color:var(--gold)
}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--mt)}

/* ── Mobile fallback ── */
@media(max-width:820px){
  .topbar{padding:0 16px;height:auto;flex-wrap:wrap}
  .logo-area{padding:12px 0;border:none;margin:0;width:100%;border-bottom:1px solid var(--bd)}
  .tabs{overflow-x:auto;width:100%;padding:4px 0;gap:0}
  .tab{padding:8px 12px;font-size:12px;border-bottom-width:2px}
  .topbar-right{display:none}
  .content{padding:16px}
  .stats{grid-template-columns:1fr 1fr}
  .stat-value{font-size:22px}
  .alloc-row{grid-template-columns:1fr}
  .controls{flex-direction:column;align-items:stretch}
  .search-input{width:100%}
  .result-count{margin-left:0}
  .tbl-wrap{border:none;border-radius:0}
  .dtbl thead{display:none}
  .dtbl,.dtbl tbody,.dtbl tr,.dtbl td{display:block}
  .dtbl tr{
    background:var(--sf);border:1px solid var(--bd);border-radius:12px;
    margin-bottom:10px;padding:6px 12px
  }
  .dtbl td{padding:5px 0;border:none;font-size:13px}
  .dtbl td.tc-right{text-align:left}
}
"""

    # ── Inline JS ─────────────────────────────────────────────────────────────
    js = """
// ── Tab switching ─────────────────────────────────────────────────────────────
const sections = document.querySelectorAll('.section[data-tab]');
const tabBtns  = document.querySelectorAll('.tab[data-tab]');

function showTab(id) {
  sections.forEach(s => s.style.display = s.dataset.tab === id ? '' : 'none');
  tabBtns.forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  window.scrollTo({top:0,behavior:'instant'});
}

tabBtns.forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
showTab('overview'); // default

// ── Items search + filter + column sort ───────────────────────────────────────
const searchEl  = document.getElementById('item-search');
const chips     = document.querySelectorAll('.chip[data-type]');
const tbody     = document.querySelector('#tbl-items tbody');
const rows      = Array.from(tbody?.querySelectorAll('tr[data-type]') || []);
const countEl   = document.getElementById('result-count');
const ths       = document.querySelectorAll('#tbl-items th[data-sort]');

let activeType = 'all', sortKey = 'date', sortDir = -1;

const STR_KEYS = new Set(['name','date']);

function render() {
  const q = (searchEl?.value || '').toLowerCase().trim();
  let shown = 0;
  rows.forEach(r => {
    let typeOk;
    if (activeType === 'all')          typeOk = r.dataset.status !== 'sold';
    else if (activeType === '__sold__') typeOk = r.dataset.status === 'sold';
    else                               typeOk = r.dataset.type === activeType && r.dataset.status !== 'sold';
    const ok = typeOk && (!q || (r.dataset.search || '').includes(q));
    r.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  if (countEl) countEl.textContent = shown + ' item' + (shown !== 1 ? 's' : '');
  ths.forEach(th => {
    th.classList.remove('s-asc','s-desc');
    if (th.dataset.sort === sortKey)
      th.classList.add(sortDir === 1 ? 's-asc' : 's-desc');
  });
}

function sortRows() {
  const sorted = [...rows].sort((a, b) => {
    const ak = a.dataset[sortKey] || '', bk = b.dataset[sortKey] || '';
    return STR_KEYS.has(sortKey)
      ? sortDir * ak.localeCompare(bk)
      : sortDir * ((parseFloat(ak) || 0) - (parseFloat(bk) || 0));
  });
  sorted.forEach(r => tbody?.appendChild(r));
  render();
}

if (searchEl) searchEl.addEventListener('input', render);
chips.forEach(c => c.addEventListener('click', () => {
  chips.forEach(x => x.classList.remove('on'));
  c.classList.add('on');
  activeType = c.dataset.type;
  render();
}));
ths.forEach(th => th.addEventListener('click', () => {
  if (sortKey === th.dataset.sort) {
    sortDir *= -1;
  } else {
    sortKey = th.dataset.sort;
    // Default direction: asc for name, desc for everything else
    sortDir = sortKey === 'name' ? 1 : -1;
  }
  sortRows();
}));

sortRows();

// ── CardMarket image tooltip ───────────────────────────────────────────────
const tip = document.createElement('div');
tip.id = 'cm-tip';
tip.style.cssText = [
  'position:fixed','display:none','z-index:9999','pointer-events:none',
  'padding:6px','border-radius:10px','background:rgba(10,12,21,.92)',
  'border:1px solid rgba(245,200,66,.25)',
  'box-shadow:0 8px 32px rgba(0,0,0,.6)',
  'backdrop-filter:blur(8px)','-webkit-backdrop-filter:blur(8px)',
  'transition:opacity .1s'
].join(';');
const tipImg = document.createElement('img');
tipImg.style.cssText = 'display:block;width:200px;border-radius:6px';
tip.appendChild(tipImg);
document.body.appendChild(tip);

rows.forEach(r => {
  if (!r.dataset.img) return;
  r.style.cursor = 'default';
  r.addEventListener('mouseenter', () => {
    tipImg.src = r.dataset.img;
    tip.style.display = 'block';
  });
  r.addEventListener('mousemove', e => {
    const x = e.clientX + 18, y = e.clientY - 10;
    const W = window.innerWidth, H = window.innerHeight;
    tip.style.left = (x + 210 > W ? e.clientX - 225 : x) + 'px';
    tip.style.top  = (y + 220 > H ? e.clientY - 225 : y) + 'px';
  });
  r.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
});
"""

    # ── SVG tab icons ─────────────────────────────────────────────────────────
    def ico(path):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                f'fill="none" stroke="currentColor">{path}</svg>')

    i_overview  = ico('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>')
    i_portfolio = ico('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><polyline points="3 6 4 6"/><polyline points="3 12 4 12"/><polyline points="3 18 4 18"/>')
    i_charts    = ico('<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/>')
    i_watchlist = ico('<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>')

    # ── Full HTML ─────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-title" content="OPTCG"/>
<title>OPTCG Tracker</title>
<style>{css}</style>
</head>
<body>

<header class="topbar">
  <div class="logo-area">
    <div>
      <div class="logo-title">OPTCG</div>
      <div class="logo-sub">Tracker</div>
    </div>
  </div>
  <nav class="tabs">
    <button class="tab active" data-tab="overview">{i_overview} Overview</button>
    <button class="tab" data-tab="portfolio">{i_portfolio} Portfolio</button>
    <button class="tab" data-tab="charts">{i_charts} P&amp;L</button>
    <button class="tab" data-tab="watchlist">{i_watchlist} Watchlist</button>
  </nav>
  <div class="topbar-right">Updated {generated}</div>
</header>

<div class="content">

  <!-- ── Overview tab ─────────────────────────────────────────── -->
  <section class="section" data-tab="overview">
    <div class="stats">{stats_html}</div>

    <div class="chart-card" style="margin-bottom:16px">
      <div class="chart-title">Portfolio Value Over Time</div>
      {timeline_svg}
    </div>

    <div class="alloc-row">
      <div class="chart-card">
        <div class="chart-title">Allocation by Type</div>
        {alloc_type_svg}
      </div>
      <div class="chart-card">
        <div class="chart-title">Allocation by Set</div>
        {alloc_set_svg}
      </div>
    </div>
  </section>

  <!-- ── Portfolio tab ────────────────────────────────────────── -->
  <section class="section" data-tab="portfolio">
    <div class="controls">
      <div class="search-wrap">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input id="item-search" class="search-input" type="search"
               placeholder="Search by name, set, number…" autocomplete="off"/>
      </div>
      <div class="chips">
        <span class="chip on" data-type="all">All Active</span>
        <span class="chip" data-type="card">Singles</span>
        <span class="chip" data-type="booster_box">Boxes</span>
        <span class="chip" data-type="blister">Blisters</span>
        <span class="chip" data-type="sealed_set">Sealed</span>
        <span class="chip" data-type="promo">Promos</span>
        <span class="chip" data-type="__sold__" style="border-color:rgba(74,222,128,.3)">✔ Sold</span>
      </div>
      <span id="result-count" class="result-count"></span>
    </div>

    <div class="tbl-wrap">
      <table id="tbl-items" class="dtbl">
        <thead>
          <tr>
            <th data-sort="name">Name</th>
            <th>Set</th>
            <th data-sort="paid" class="tc-right">Paid €</th>
            <th data-sort="cur"  class="tc-right">Current €</th>
            <th data-sort="pnl"  class="tc-right">P&amp;L €</th>
            <th data-sort="pct"  class="tc-right">P&amp;L %</th>
            <th data-sort="date">Date</th>
          </tr>
        </thead>
        <tbody>
          {items_rows}
        </tbody>
      </table>
    </div>
  </section>

  <!-- ── P&L tab ──────────────────────────────────────────────── -->
  <section class="section" data-tab="charts">
    <div class="chart-card">
      {pnl_svg}
    </div>
  </section>

  <!-- ── Watchlist tab ────────────────────────────────────────── -->
  <section class="section" data-tab="watchlist">
    <div class="tbl-wrap">
      <table class="dtbl">
        <thead>
          <tr>
            <th>Name</th>
            <th class="tc-right">Target</th>
            <th>Added</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>{wl_rows}</tbody>
      </table>
    </div>
  </section>

</div>
<script>{js}</script>
</body>
</html>"""

    if out_path is None:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = EXPORTS_DIR / "dashboard.html"

    out_path.write_text(html, encoding="utf-8")
    return out_path
