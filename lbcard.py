"""Leaderboard pictures: the board (top 5 + you) and the analysis (last 12 h).

Both are small HTML pages rendered by headless Chromium (already installed for the scraper)
and screenshotted to PNG. All numbers come from track.prepare(), so the pictures say exactly
what the text fallback says.
"""
import html
import re

e = lambda x: html.escape(str(x)) if x not in (None, "") else ""
TAGS = {"SLEEPING": ("\U0001F634 no gain", "#9a9ab0"), "ON FIRE": ("\U0001F525 on fire", "#fb923c"),
        "SURGING": ("\U0001F680 surging", "#3ddc97"), "COOLING": ("\U0001F9CA cooling", "#7fd4ff")}
MEDAL = {1: ("#ffd34d", "#7a5a00"), 2: ("#e5e9ef", "#4b5563"), 3: ("#f0a56b", "#6b3410")}

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=block');
*{box-sizing:border-box;margin:0;padding:0}
body{margin:0;background:transparent;font-family:'Outfit','Noto Sans','DejaVu Sans',sans-serif;color:#f4f4f7}
.card{width:600px;border-radius:30px;padding:26px 24px 20px;position:relative;overflow:hidden;
 background:radial-gradient(120% 60% at 50% 0%,rgba(61,220,151,.28) 0%,rgba(0,0,0,0) 60%),linear-gradient(180deg,#121a17 0%,#0b0f0d 100%);
 border:1.5px solid rgba(61,220,151,.4)}
.card:before{content:"";position:absolute;inset:0;pointer-events:none;background-image:radial-gradient(rgba(255,255,255,.55) 1px,transparent 1.5px);background-size:46px 46px;opacity:.08}
.top{display:flex;justify-content:space-between;align-items:center;position:relative}
.tag{font-size:13px;font-weight:800;letter-spacing:.18em;color:#3ddc97;padding:7px 14px;border-radius:99px;background:rgba(61,220,151,.12);border:1px solid rgba(61,220,151,.4)}
.when{font-size:14px;color:#a1b5ac;font-weight:600}
.av{border-radius:50%;background:#26302c;object-fit:cover;display:flex;align-items:center;justify-content:center;font-weight:800;color:#9fc7b6;flex:none}
.foot{display:flex;justify-content:space-between;margin-top:14px;font-size:12px;color:#6b7d75;position:relative}
/* board */
h1{font-size:34px;font-weight:800;margin:18px 0 4px;position:relative}
.meta{display:flex;gap:8px;margin-bottom:16px;position:relative}
.chip{font-size:14px;font-weight:600;padding:5px 12px;border-radius:99px;background:#ffffff10;color:#cfe3da}
.chip.end{color:#111;background:#3ddc97;font-weight:800}
.row{display:flex;align-items:center;gap:12px;padding:12px 14px;margin-top:8px;border-radius:18px;background:#ffffff0a;border:1px solid #ffffff12;position:relative}
.row.first{background:rgba(255,211,77,.09);border-color:rgba(255,211,77,.35)}
.row.me{background:rgba(61,220,151,.12);border:1.5px solid rgba(61,220,151,.6)}
.rk{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:17px;flex:none}
.mid{flex:1;min-width:0}
.nm{font-size:18px;font-weight:800;display:flex;align-items:center;gap:7px;white-space:nowrap}
.you{font-size:11px;font-weight:800;letter-spacing:.12em;color:#111;background:#3ddc97;padding:2px 7px;border-radius:6px}
.mv{font-size:12px;font-weight:800}.mv.up{color:#3ddc97}.mv.dn{color:#f87171}
.sub{font-size:13px;color:#9aaba4;margin-top:1px;display:flex;align-items:center;gap:6px}
.tg{font-size:12px;font-weight:600;border:1px solid;border-radius:99px;padding:1px 7px}
.spd{display:flex;align-items:center;gap:8px;margin-top:6px}
.bar{flex:1;height:6px;border-radius:99px;background:#ffffff12;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#3ddc97,#a7f3d0)}
.sp{font-size:12px;color:#cfe3da;font-weight:600;white-space:nowrap}.sp.none{color:#6b7d75}
.val{text-align:right;font-size:24px;font-weight:800;white-space:nowrap}
.val small{font-size:12px;color:#9aaba4;margin-left:3px}
.gap{text-align:center;color:#6b7d75;font-size:13px;margin:10px 0 2px;letter-spacing:.1em;position:relative}
/* analysis */
h2{font-size:15px;font-weight:800;letter-spacing:.12em;color:#9fc7b6;text-transform:uppercase;margin:20px 0 8px;position:relative}
.box{background:#ffffff0a;border:1px solid #ffffff12;border-radius:18px;padding:6px 14px;position:relative}
.ln{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #ffffff0d}.ln:first-child{border-top:0}
.ln .t{flex:1;min-width:0;font-size:15px}.ln .t b{font-weight:800}.ln .t small{display:block;color:#9aaba4;font-size:12px}
.ln .r{font-size:13px;color:#cfe3da;font-weight:600;white-space:nowrap;text-align:right}
.em{font-size:20px;width:26px;text-align:center;flex:none}
.ov{display:flex;align-items:center;gap:8px}.ov .ar{color:#3ddc97;font-weight:800;font-size:18px}
.eta{font-size:20px;font-weight:800;color:#3ddc97}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;position:relative}
.pr{display:flex;align-items:center;gap:8px;background:#ffffff0a;border:1px solid #ffffff12;border-radius:14px;padding:6px 10px;font-size:14px;font-weight:600;min-width:0}
.pr .n{font-weight:800;width:24px;color:#9fc7b6}.pr .nm2{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr.me{border-color:rgba(61,220,151,.7);background:rgba(61,220,151,.12)}
.up{color:#3ddc97;font-weight:800;font-size:12px}.dn{color:#f87171;font-weight:800;font-size:12px}.eq{color:#6b7d75;font-size:12px}
.ok{color:#3ddc97;font-weight:800}.no{color:#f87171;font-weight:800}
.note{color:#9aaba4;font-size:13px;margin-top:8px;position:relative}
"""


def _page(body):
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{body}</body></html>'


def av(name, heads, size=40):
    u = heads.get((name or "").lower())
    if u:
        return f'<img class="av" style="width:{size}px;height:{size}px" src="{e(u)}">'
    return f'<div class="av" style="width:{size}px;height:{size}px;font-size:{size // 2.4:.0f}px">{e((name or "?")[0].upper())}</div>'


def build_board(d, heads, sh, board, emoji):
    """d = track.prepare() result. Shows the top 5, then you if you're below #5."""
    shown = [p for p in d["players"] if p["rank"] <= 5]
    me = next((p for p in d["players"] if p["me"]), None)
    if me and me["rank"] > 5:
        shown.append(me)
    top = max([p["avg"] for p in shown if p.get("avg")] or [1]) or 1
    rows = []
    for p in shown:
        if p["me"] and p["rank"] > 6:
            rows.append(f'<div class="gap">· · · {p["rank"] - 6} more · · ·</div>')
        bg, fg = MEDAL.get(p["rank"], ("#ffffff14", "#d7d7e3"))
        tag = ""
        if p["tag"]:
            t, c = TAGS[p["tag"]]
            tag = f'<span class="tg" style="color:{c};border-color:{c}55">{t}</span>'
        mv = ""
        if p.get("new"):
            mv = '<span class="mv up">NEW</span>'
        elif p["move"]:
            arrow, cls = ("▲", "up") if p["move"] > 0 else ("▼", "dn")
            mv = f'<span class="mv {cls}">{arrow}{abs(p["move"])}</span>'
        you = '<span class="you">YOU</span>' if p["me"] else ""
        gap = p["gap"].replace("*", "")
        gap = ("leads by " + gap.replace(" lead", "")) if "lead" in gap else (gap.lstrip("-") + " behind #1" if gap else "")
        if p.get("avg") is not None:
            w = max(3, int(p["avg"] / top * 100))
            spd = f'<div class="bar"><i style="width:{w}%"></i></div><div class="sp">{e(sh(p["avg"]))}/h</div>'
        else:
            spd = '<div class="sp none">no 12h data yet</div>'
        unit = p["value"].split(" ")[-1] if " " in p["value"] else ""
        val = p["value"][: -len(unit)].strip() if unit else p["value"]
        rows.append(f'<div class="row{" me" if p["me"] else ""}{" first" if p["rank"] == 1 else ""}">'
                    f'<div class="rk" style="background:{bg};color:{fg}">{p["rank"]}</div>{av(p["name"], heads, 44)}'
                    f'<div class="mid"><div class="nm">{e(p["name"])} {you}{mv}</div>'
                    f'<div class="sub">{e(gap)} {tag}</div><div class="spd">{spd}</div></div>'
                    f'<div class="val">{e(val)}<small>{e(unit)}</small></div></div>')
    ends = f'<span class="chip end">⏳ ends in {e(d["left"])}</span>' if d.get("left") else ""
    speed_note = f'<span class="chip">bars = {e(d["win"])} average speed</span>' if d.get("win") else ""
    return _page(f'''<div class="card">
 <div class="top"><div class="tag">LEADERBOARD</div><div class="when">{e(d["date"])} · {e(d["time"])}</div></div>
 <h1>{e(emoji)} {e(board)}</h1><div class="meta">{ends}{speed_note}</div>
 {"".join(rows)}
 <div class="foot"><span>Tracking the top 15 every hour</span><span>Pets Universe · Leaderboard Tracker</span></div>
</div>''')


def build_analysis(d, heads):
    A, win = d["A"], d.get("win") or "trend"
    secs = []
    if A["passes"]:
        ov = "".join(f'''<div class="ln"><div class="ov">{av(p["lo"], heads, 36)}<span class="ar">➜</span>{av(p["hi"], heads, 36)}</div>
<div class="t"><b>{e(p["lo"])}</b> passes <b>{e(p["hi"])}</b><small>#{p["lo_rank"]} → #{p["hi_rank"]} · {e(p["sb"])}/h vs {e(p["sa"])}/h</small></div>
<div class="eta">~{e(p["eta"])}</div></div>''' for p in A["passes"])
    else:
        ov = '<div class="ln"><div class="t">No overtakes before the end at current speeds.</div></div>'
    secs.append(f'<h2>\U0001F51C Overtakes coming</h2><div class="box">{ov}</div>')
    if A["final"]:
        grid = ""
        for place, name, move, is_me in A["final"]:
            if move > 0:
                mv = f'<span class="up">▲{move}</span>'
            elif move < 0:
                mv = f'<span class="dn">▼{-move}</span>'
            else:
                mv = '<span class="eq">–</span>'
            grid += (f'<div class="pr{" me" if is_me else ""}"><span class="n">{place}</span>{av(name, heads, 26)}'
                     f'<span class="nm2">{e(name)}</span>{mv}</div>')
        secs.append(f'<h2>\U0001F52E Final top 10 at this pace</h2><div class="grid">{grid}</div>')
    if A["news"]:
        nw = "".join(f'<div class="ln"><span class="em">{ic}</span>{av(n, heads, 32)}<div class="t"><b>{e(n)}</b> {e(t)}</div>'
                     f'<div class="r">{e(r)}</div></div>' for ic, n, t, r in A["news"])
        secs.append(f'<h2>\U0001F4F0 Last {e(win)}</h2><div class="box">{nw}</div>')
    if A["checks"]:
        yes, no = '<span class="ok">✅ yes</span>', '<span class="no">❌ no</span>'
        ch = "".join(f'<div class="ln"><span class="em">{ic}</span><div class="t"><b>{e(a)}</b><small>{e(b)}</small></div>'
                     f'<div class="r">{"" if ok is None else (yes if ok else no)}</div></div>' for ic, a, b, ok in A["checks"])
        left = f' · {e(d["left"])} left' if d.get("left") else ""
        secs.append(f'<h2>\U0001F3C1 Your endgame{left}</h2><div class="box">{ch}</div>')
    if A.get("all_nodata"):
        secs.append('<div class="note">ℹ️ Not enough history yet - predictions start after a few hourly checks.</div>')
    elif A.get("nodata"):
        secs.append(f'<div class="note">ℹ️ No {e(win)} history yet for {e(", ".join(A["nodata"]))}, so they are left out of predictions.</div>')
    return _page(f'''<div class="card">
 <div class="top"><div class="tag">ANALYSIS · LAST {e(win).upper()}</div><div class="when">{e(d["date"])} · {e(d["time"])}</div></div>
 {"".join(secs)}
 <div class="foot"><span>Speeds = {e(win)} average · no guesses</span><span>Pets Universe · Leaderboard Tracker</span></div>
</div>''')


def render(pages):
    """HTML pages -> PNG bytes of their .card (None where it failed or didn't style)."""
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(viewport={"width": 640, "height": 1400}, device_scale_factor=2).new_page()
        for h in pages:
            try:
                pg.set_content(h, wait_until="networkidle", timeout=25000)
                pg.evaluate("document.fonts.ready")
                ok = pg.evaluate("() => { const c = document.querySelector('.card');"
                                 " return !!c && getComputedStyle(c).borderTopLeftRadius === '30px'"
                                 " && c.getBoundingClientRect().width === 600 }")
                if not ok:
                    raise RuntimeError("card styles did not apply")
                out.append(pg.locator(".card").screenshot(omit_background=True, timeout=10000))
            except Exception as ex:
                print("leaderboard card failed:", str(ex)[:120])
                out.append(None)
        b.close()
    return out
