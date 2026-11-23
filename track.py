#!/usr/bin/env python3
"""Pets Universe - Total Flowers leaderboard tracker.

Loads https://petsuniverse.org/players in a real browser (Playwright), reads the
Total Flowers board, stores a snapshot in SQLite, compares with the previous
successful snapshot using the real elapsed time, and posts a report to Discord.
"""
import os
import re
import sqlite3
import sys
import urllib.request
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

URL = "https://petsuniverse.org/players"
ME = os.environ.get("TRACK_USER", "Imnotsadece")
TZ = ZoneInfo("Europe/Stockholm")
DB = os.environ.get("TRACK_DB", "data/tracker.db")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
SHOW_N = 5      # rows shown in the report
STORE_N = 10    # rows stored per snapshot (plus me, wherever I am)
MIN_GAP_S = 600  # ignore a second run within 10 min (duplicate guard)
MIN_EXP = int(os.environ.get("TRACK_MIN_EXP", "20"))  # flower totals are ~e+25
VAL_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z]{0,8})$")


# ---------------------------------------------------------------- scraping
def fetch_text():
    from playwright.sync_api import sync_playwright
    os.makedirs("data", exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 430, "height": 1800})
        pg.goto(URL, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2000)
        before = pg.inner_text("body")
        print("--- page text BEFORE click (first 40 lines) ---")
        print("\n".join(before.splitlines()[:40]))
        tabs = pg.locator("button", has_text="Total Flowers")
        print("tab matches:", tabs.count())
        btn = tabs.first

        def pressed():
            try:
                return btn.get_attribute("aria-pressed", timeout=2000)
            except Exception:
                return None

        print("aria-pressed before:", pressed())
        if pressed() != "true":
            for how in ("dom", "normal", "force"):
                try:
                    if how == "dom":
                        btn.evaluate("e => e.click()")
                    elif how == "normal":
                        btn.scroll_into_view_if_needed(timeout=3000)
                        btn.click(timeout=4000)
                    else:
                        btn.click(timeout=4000, force=True)
                except Exception as e:
                    print(how, "click failed:", str(e).splitlines()[0][:100])
                    continue
                pg.wait_for_timeout(1500)
                st = pressed()
                print("after", how, "click aria-pressed =", st)
                if st == "true":
                    break
        if pressed() != "true":
            raise RuntimeError("could not select the Total Flowers tab; nothing saved")
        for _ in range(10):  # wait for the board to actually change
            if pg.inner_text("body") != before:
                break
            pg.wait_for_timeout(800)
        pg.wait_for_timeout(2500)
        text = ""
        for _ in range(8):  # scroll so lazy rows load until I'm found
            text = pg.inner_text("body")
            if find_me(parse_board(text)):
                break
            pg.mouse.wheel(0, 2500)
            pg.wait_for_timeout(1200)
        print("--- page text AFTER click (first 40 lines) ---")
        print("\n".join(text.splitlines()[:40]))
        pg.screenshot(path="data/last.png")
        with open("data/last.txt", "w", encoding="utf-8") as f:
            f.write(text)
        b.close()
    return text


def parse_board(text):
    """Rows look like: rank / username / value (e.g. 7.59e+25vg)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for j, l in enumerate(lines):  # skip anything above the tab strip
        if l == "Total Flowers":
            lines = lines[j + 1:]
            break
    rows = []
    for i, l in enumerate(lines):
        m = VAL_RE.match(l)
        if not m or i < 1:
            continue
        num, unit = m.groups()
        if not unit and "e" not in num.lower() and "." not in num:
            continue  # a plain number is a rank, not a score
        k = i - 1
        if lines[k].lower() == "private stats" and k >= 1:
            k -= 1  # extra label line between the username and the value
        name = lines[k]
        if name.isdigit():
            continue
        rank = int(lines[k - 1]) if k >= 1 and lines[k - 1].isdigit() else len(rows) + 1
        if rank == 1 and rows:
            break  # a second board started
        if rank != len(rows) + 1:
            continue
        rows.append({"rank": rank, "name": name, "raw": l,
                     "value": float(num.replace(",", "")), "unit": unit})
    return rows


def exponent(raw):
    m = re.search(r"[eE]\+?(\d+)", raw)
    return int(m.group(1)) if m else 0


def is_me(name):
    n, me = name.lower(), ME.lower()
    if n == me:
        return True
    stem = n.rstrip(".… ")
    return (n != stem) and len(stem) >= 2 and me.startswith(stem)


def find_me(rows):
    return next((r for r in rows if is_me(r["name"])), None)


# ---------------------------------------------------------------- storage
def connect():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS snaps2(
      id INTEGER PRIMARY KEY, ts_utc TEXT, ts_local TEXT, ok INTEGER, error TEXT);
    CREATE TABLE IF NOT EXISTS rows2(
      snapshot_id INTEGER, rank INTEGER, name TEXT, raw TEXT,
      value REAL, unit TEXT, is_me INTEGER);
    """)
    return con


def last_ok(con, n=2):
    return con.execute(
        "SELECT * FROM snaps2 WHERE ok=1 ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def load_rows(con, sid):
    return {r["name"].lower(): r for r in con.execute(
        "SELECT * FROM rows2 WHERE snapshot_id=?", (sid,))}


def save(con, now_utc, rows):
    top = rows[:STORE_N]
    me = find_me(rows)
    if me and me not in top:
        top = top + [me]
    cur = con.execute("INSERT INTO snaps2(ts_utc,ts_local,ok) VALUES(?,?,1)",
                      (now_utc.isoformat(), now_utc.astimezone(TZ).isoformat()))
    sid = cur.lastrowid
    con.executemany(
        "INSERT INTO rows2 VALUES(?,?,?,?,?,?,?)",
        [(sid, r["rank"], r["name"], r["raw"], r["value"], r["unit"],
          1 if r is me else 0) for r in top])
    con.commit()
    return sid


def save_failure(con, now_utc, err):
    con.execute("INSERT INTO snaps2(ts_utc,ts_local,ok,error) VALUES(?,?,0,?)",
                (now_utc.isoformat(), now_utc.astimezone(TZ).isoformat(), str(err)[:500]))
    con.commit()


# ---------------------------------------------------------------- maths
def fnum(v, unit, signed=False):
    s = f"{v:+.4e}" if signed else f"{v:.4e}"
    return f"{s} {unit}".strip()


def rates(cur, prev, hours):
    """cur/prev: {name_lower: row}. Returns {name_lower: (gain, rate, pct)}."""
    out = {}
    if not prev or hours <= 0:
        return out
    for k, c in cur.items():
        p = prev.get(k)
        if p is None or p["unit"] != c["unit"]:
            continue  # no verified unit conversion -> don't compare
        gain = c["value"] - p["value"]
        out[k] = (gain, gain / hours, (gain / p["value"] * 100) if p["value"] else None)
    return out


def hm(hours):
    m = int(round(hours * 60))
    return f"{m // 60}h {m % 60:02d}m"


def build_report(now_utc, cur_rows, cur_by, prev_row, prev_by, prevprev_by, prevprev_row):
    local = now_utc.astimezone(TZ)
    out = [f"FLOWER LEADERBOARD - {local:%H:%M} ({local:%a %d %b})"]
    hours = 0
    if prev_row:
        pts = datetime.fromisoformat(prev_row["ts_utc"])
        hours = (now_utc - pts).total_seconds() / 3600
        out.append(f"vs previous reading {hm(hours)} ago"
                   + ("  [STALE: missed checks]" if hours > 1.5 else ""))
    else:
        out.append("Baseline saved - gains start next check")
    cur_rates = rates(cur_by, prev_by, hours)
    prev_rates = {}
    if prevprev_row and prev_row:
        h2 = (datetime.fromisoformat(prev_row["ts_utc"])
              - datetime.fromisoformat(prevprev_row["ts_utc"])).total_seconds() / 3600
        prev_rates = rates(prev_by, prevprev_by, h2)

    shown = cur_rows[:SHOW_N]
    me = find_me(cur_rows)
    if me and me not in shown:
        shown = shown + [me]
    out.append("")
    notes = []
    for r in shown:
        k = r["name"].lower()
        line = f"#{r['rank']} {r['name']}{' (you)' if r is me else ''}: {fnum(r['value'], r['unit'])}"
        if k in cur_rates:
            gain, rate, pct = cur_rates[k]
            line += f" | {fnum(gain, r['unit'], True)} ({fnum(rate, r['unit'], True)}/h"
            line += f", {pct:+.2f}%)" if pct is not None else ")"
            pr = prev_rates.get(k)
            if pr and pr[1] > 0 and rate > 0:
                if rate >= 2 * pr[1]:
                    notes.append(f"SPIKE: {r['name']} earning {rate / pr[1]:.1f}x faster than last hour")
                elif rate > pr[1] * 1.1:
                    line += " ^"
                elif rate < pr[1] * 0.9:
                    line += " v"
        p = prev_by.get(k) if prev_by else None
        if p is not None and p["rank"] != r["rank"]:
            line += f" [rank {p['rank']}->{r['rank']}]"
        elif prev_by and p is None:
            line += " [new in stored top]"
        out.append(line)
    out.append("")

    if not me:
        out.append(f"{ME} not found on the board - check data/last.png")
    else:
        ref = cur_rows[0] if me["rank"] != 1 else (cur_rows[1] if len(cur_rows) > 1 else None)
        out.append(f"You: rank #{me['rank']}")
        if ref and ref["unit"] == me["unit"]:
            gap = abs(ref["value"] - me["value"])
            who = "leader" if me["rank"] != 1 else "#2"
            out.append(f"{'Gap to' if me['rank'] != 1 else 'Lead over'} {who} ({ref['name']}): "
                       f"{fnum(gap, me['unit'])}")
            mk, rk = me["name"].lower(), ref["name"].lower()
            if mk in cur_rates and rk in cur_rates and prev_by:
                my_rate, ref_rate = cur_rates[mk][1], cur_rates[rk][1]
                pm, pr_ = prev_by.get(mk), prev_by.get(rk)
                if pm and pr_ and pm["unit"] == pr_["unit"] == me["unit"]:
                    prev_gap = abs(pr_["value"] - pm["value"])
                    out.append(f"Gap change: {fnum(prev_gap - gap, me['unit'], True)} since last check")
                if me["rank"] != 1:
                    diff = my_rate - ref_rate
                    if diff > 0:
                        out.append(f"Catching up: {fnum(diff, me['unit'])}/h")
                        out.append(f"Estimated overtake at this pace: {hm(gap / diff)} "
                                   f"(only valid if both rates hold)")
                        out.append("Verdict: gaining on #1.")
                    else:
                        out.append(f"Falling behind: {fnum(-diff, me['unit'])}/h")
                        out.append(f"Need +{fnum(-diff, me['unit'])}/h more to match #1's rate")
                        out.append("Verdict: not catching #1 at current rates.")
                else:
                    diff = my_rate - ref_rate
                    out.append(f"Lead {'growing' if diff > 0 else 'shrinking'} by "
                               f"{fnum(abs(diff), me['unit'])}/h")
        elif ref:
            out.append("Units differ from the leader - no verified conversion, gap not computed.")
    out += [""] + notes
    return "\n".join(out).strip()


MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949", 4: "4️⃣", 5: "5️⃣"}
SUFFIX = ["", "K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No", "Dc", "UDc", "DDc"]


def sh(v, signed=False):
    """107.92e24 -> '107.92Sp' (K, M, B, T, Qa, Qi, Sx, Sp, Oc, No, Dc ...)."""
    sign = ("-" if v < 0 else "+") if signed else ("-" if v < 0 else "")
    av = abs(v)
    if av < 999.995:
        return f"{sign}{av:.2f}"
    e = int(math.floor(math.log10(av))) // 3
    mant = av / 10 ** (3 * e)
    if round(mant, 2) >= 1000:
        e, mant = e + 1, mant / 1000
    if e >= len(SUFFIX):
        return f"{sign}{av:.2e}".replace("e+", "e")
    return f"{sign}{mant:.2f}{SUFFIX[e]}"


UA = "Mozilla/5.0 (compatible; pu-tracker/1.0)"
RANK_COLORS = {1: 0xF1C40F, 2: 0xBDC3C7, 3: 0xCD7F32}
_HS = {}


def headshots(names):
    """Roblox usernames -> headshot URLs (two public API calls for all names)."""
    names = [n for n in names if re.fullmatch(r"[A-Za-z0-9_]{3,20}", n)]
    need = [n for n in names if n.lower() not in _HS]
    if need:
        try:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/usernames/users",
                data=json.dumps({"usernames": need, "excludeBannedUsers": False}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                ids = {u["requestedUsername"].lower(): u["id"] for u in json.load(r)["data"]}
            if ids:
                url = ("https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds="
                       + ",".join(str(i) for i in ids.values())
                       + "&size=150x150&format=Png&isCircular=false")
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                            timeout=15) as r:
                    by_id = {t["targetId"]: t.get("imageUrl") for t in json.load(r)["data"]}
                for name, uid in ids.items():
                    _HS[name] = by_id.get(uid)
        except Exception as e:
            print("headshots failed:", str(e)[:100])
        for n in need:
            _HS.setdefault(n.lower(), None)
    return {n.lower(): _HS.get(n.lower()) for n in names}


def prepare(now_utc, cur_rows, cur_by, prev_row, prev_by, pp_by, pp_row):
    """All numbers/text for the report, independent of how it is drawn."""
    local = now_utc.astimezone(TZ)
    hours = 0
    if prev_row:
        hours = (now_utc - datetime.fromisoformat(prev_row["ts_utc"])).total_seconds() / 3600
    cur_rates = rates(cur_by, prev_by, hours)
    prev_rates = {}
    if pp_row and prev_row:
        h2 = (datetime.fromisoformat(prev_row["ts_utc"])
              - datetime.fromisoformat(pp_row["ts_utc"])).total_seconds() / 3600
        prev_rates = rates(prev_by, pp_by, h2)
    me = find_me(cur_rows)
    shown = cur_rows[:SHOW_N]
    if me and me not in shown:
        shown = shown + [me]
    if prev_row:
        sub = f"vs reading {hm(hours)} ago" + ("  ·  ⚠️ missed checks" if hours > 1.5 else "")
    else:
        sub = "Baseline saved - gains start next check"

    players, spikes = [], []
    for r in shown:
        k = r["name"].lower()
        p = prev_by.get(k) if prev_by else None
        move = (p["rank"] - r["rank"]) if p is not None else 0   # +n = moved up
        d = {"rank": r["rank"], "name": r["name"], "me": r is me, "move": move,
             "value": f"{sh(r['value'])} {r['unit']}".strip(), "delta": ""}
        if k in cur_rates:
            g, rate, pct = cur_rates[k]
            arrow = ""
            pr = prev_rates.get(k)
            if pr and pr[1] > 0 and rate > 0:
                if rate >= 2 * pr[1]:
                    spikes.append(f"⚡ **{r['name']}** earning {rate / pr[1]:.1f}x faster than last reading")
                elif rate > pr[1] * 1.1:
                    arrow = " ↑"
                elif rate < pr[1] * 0.9:
                    arrow = " ↓"
            d["delta"] = f"{sh(g, True)}  ·  {sh(rate)}/h{arrow}" + (
                f"  ·  {pct:+.2f}%" if pct is not None else "")
        players.append(d)

    color, lines, title = 0x5865F2, [], "\U0001F3AF You"
    if not me:
        lines.append(f"{ME} not found on the board")
    else:
        title = f"\U0001F3AF You: rank #{me['rank']}"
        ref = cur_rows[0] if me["rank"] != 1 else (cur_rows[1] if len(cur_rows) > 1 else None)
        if ref and ref["unit"] == me["unit"]:
            gap = abs(ref["value"] - me["value"])
            u = me["unit"]
            mk, rk = me["name"].lower(), ref["name"].lower()
            body = [f"{'Gap to #1' if me['rank'] != 1 else 'Lead over #2'}: **{sh(gap)} {u}**"]
            pm, pr_ = (prev_by.get(mk), prev_by.get(rk)) if prev_by else (None, None)
            if pm and pr_ and pm["unit"] == pr_["unit"] == u and me["rank"] != 1:
                chg = abs(pr_["value"] - pm["value"]) - gap
                body.append(f"Since last check: {'↓ closed' if chg > 0 else '↑ grew'} {sh(abs(chg))}")
            verdict = ""
            if mk in cur_rates and rk in cur_rates:
                diff = cur_rates[mk][1] - cur_rates[rk][1]
                if me["rank"] != 1:
                    if diff > 0:
                        color = 0x57F287
                        verdict = "✅ Gaining on #1"
                        body.append(f"Closing: **{sh(diff)} {u}/h**")
                        body.append(f"Overtake: **~{hm(gap / diff)}** if both rates hold")
                    else:
                        color = 0xED4245
                        verdict = "⚠️ Not catching #1"
                        body.append(f"Falling behind: **{sh(-diff)} {u}/h**")
                        body.append(f"Need **+{sh(-diff)} {u}/h** more to match #1")
                else:
                    color = 0x57F287 if diff >= 0 else 0xFEE75C
                    verdict = "\U0001F451 You're #1"
                    body.append(f"Lead {'growing' if diff >= 0 else 'shrinking'}: **{sh(abs(diff))} {u}/h**")
            lines = ([f"**{verdict}**"] if verdict else []) + body
        elif ref:
            lines.append("Units differ from the leader - gap not computed.")
    lines += spikes
    return {"time": f"{local:%H:%M}", "sub": sub, "players": players, "color": color,
            "title": title, "lines": lines, "now": now_utc.isoformat()}


# ---------------------------------------------------------------- picture
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/truetype/liberation/",
             "/usr/share/fonts/truetype/noto/", "/usr/share/fonts/dejavu/"]


def _font(size, bold=False):
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf"])
    for d in FONT_DIRS:
        for n in names:
            if os.path.exists(d + n):
                return ImageFont.truetype(d + n, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _pillow():
    try:
        import PIL  # noqa
    except ImportError:
        import subprocess
        for extra in ([], ["--break-system-packages"], ["--user"]):
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pillow", *extra],
                               capture_output=True, text=True, timeout=180)
            print("pip install pillow", extra, "->", r.returncode, (r.stderr or "")[-300:])
            if r.returncode == 0:
                break
        import importlib
        importlib.invalidate_caches()
    from PIL import Image  # noqa
    return True


def _avatar(url, size):
    from PIL import Image
    import io
    if not url:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            im = Image.open(io.BytesIO(r.read())).convert("RGBA")
        return im.resize((size, size), Image.LANCZOS)
    except Exception as e:
        print("avatar failed:", str(e)[:80])
        return None


def render_png(data, avatars):
    """One tall image: big avatar on the left, name / total / gains on the right."""
    _pillow()
    from PIL import Image, ImageDraw
    import io
    W, PAD, GAP, ROW, AV = 1000, 14, 12, 192, 156
    n = len(data["players"])
    H = PAD + n * (ROW + GAP)
    img = Image.new("RGB", (W, H), (30, 31, 34))
    dr = ImageDraw.Draw(img)
    f_name, f_val, f_sub, f_badge = _font(44, True), _font(60, True), _font(34), _font(30, True)
    medal = {1: (241, 196, 15), 2: (189, 195, 199), 3: (205, 127, 50)}
    y = PAD
    for p in data["players"]:
        accent = (87, 242, 135) if p["me"] else medal.get(p["rank"], (88, 101, 242))
        dr.rounded_rectangle((PAD, y, W - PAD, y + ROW), 22, fill=(43, 45, 49))
        dr.rounded_rectangle((PAD, y, PAD + 10, y + ROW), 5, fill=accent)
        ax, ay = PAD + 26, y + (ROW - AV) // 2
        av = avatars.get(p["name"].lower())
        if av is not None:
            mask = Image.new("L", (AV, AV), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, AV, AV), 26, fill=255)
            img.paste(av.convert("RGB"), (ax, ay), mask)
        else:
            dr.rounded_rectangle((ax, ay, ax + AV, ay + AV), 26, fill=(64, 66, 73))
            dr.text((ax + AV // 2, ay + AV // 2), p["name"][:1].upper(), font=f_val,
                    fill=(200, 200, 205), anchor="mm")
        tx = ax + AV + 28
        # rank badge + name (+ you pill, rank move)
        dr.ellipse((tx, y + 18, tx + 46, y + 64), fill=accent)
        dr.text((tx + 23, y + 41), str(p["rank"]), font=f_badge,
                fill=(20, 20, 22) if (p["me"] or p["rank"] <= 3) else (255, 255, 255), anchor="mm")
        nx, maxw = tx + 60, W - PAD - 20
        name = p["name"]
        while dr.textlength(name, font=f_name) > 430 and len(name) > 4:
            name = name[:-2]
        if name != p["name"]:
            name += "…"
        dr.text((nx, y + 41), name, font=f_name, fill=(255, 255, 255), anchor="lm")
        cx = nx + dr.textlength(name, font=f_name) + 16
        if p["me"]:
            w = dr.textlength("YOU", font=f_badge) + 28
            dr.rounded_rectangle((cx, y + 20, cx + w, y + 62), 14, fill=(87, 242, 135))
            dr.text((cx + w / 2, y + 41), "YOU", font=f_badge, fill=(20, 20, 22), anchor="mm")
            cx += w + 12
        if p["move"]:
            up = p["move"] > 0
            dr.text((cx, y + 41), f"{'▲' if up else '▼'}{abs(p['move'])}", font=f_badge,
                    fill=(87, 242, 135) if up else (237, 66, 69), anchor="lm")
        dr.text((tx, y + 112), p["value"], font=f_val, fill=(255, 255, 255), anchor="lm")
        if p["delta"]:
            dr.text((tx, y + 166), p["delta"].replace("  ", " "), font=f_sub, fill=(170, 174, 182), anchor="lm")
        y += ROW + GAP
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def discord_message(now_utc, *args):
    """ONE embed. With the picture: verdict text on top, leaderboard image below.
    If the picture can't be made: the same info as plain text lines."""
    d = prepare(now_utc, *args)
    emb = {"title": f"\U0001F338 Flower leaderboard  ·  {d['time']}", "color": d["color"],
           "timestamp": d["now"]}
    verdict = "\n".join(d["lines"]) or "-"
    png = None
    try:
        hs = headshots([p["name"] for p in d["players"]])
        avatars = {k: _avatar(u, 156) for k, u in hs.items() if u}
        png = render_png(d, avatars)
    except Exception as e:
        import traceback
        print("picture failed, using text:", repr(e), file=sys.stderr)
        traceback.print_exc()
    if png:
        emb["description"] = f"{d['sub']}\n\n**{d['title']}**\n{verdict}"
        emb["image"] = {"url": "attachment://board.png"}
    else:
        rows = []
        for p in d["players"]:
            you = "  \U0001F448 you" if p["me"] else ""
            rows.append(f"{MEDALS.get(p['rank'], '#' + str(p['rank']))} **{p['name']}**{you}\n"
                        f"{p['value']}" + (f"\n*{p['delta']}*" if p["delta"] else ""))
        emb["description"] = (d["sub"] + "\n\n" + "\n\n".join(rows) +
                              f"\n\n**{d['title']}**\n{verdict}")
    return emb, png


# ---------------------------------------------------------------- notify
def _multipart(payload, png):
    b = "----pu" + os.urandom(8).hex()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n").encode()
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"board.png\"\r\n"
             f"Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def notify(msg, embed=None, png=None):
    print(msg)
    if not WEBHOOK:
        print("[no DISCORD_WEBHOOK_URL set - not sent]")
        return False
    payload = {"embeds": embed if isinstance(embed, list) else [embed]} if embed else {"content": msg[:1900]}
    if png:
        data, ctype = _multipart(payload, png)
    else:
        data, ctype = json.dumps(payload).encode(), "application/json"
    req = urllib.request.Request(WEBHOOK, data=data, headers={
        "Content-Type": ctype, "User-Agent": "pu-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("discord status", r.status)
    return True


# ---------------------------------------------------------------- main
def main():
    con = connect()
    now = datetime.now(timezone.utc)
    prevs = last_ok(con, 2)
    if prevs and not os.environ.get("FORCE"):
        age = (now - datetime.fromisoformat(prevs[0]["ts_utc"])).total_seconds()
        if age < MIN_GAP_S:
            print(f"snapshot {int(age)}s ago exists - skipping duplicate run")
            return 0
    try:
        rows = parse_board(fetch_text())
        if len(rows) < SHOW_N:
            raise RuntimeError(f"parsed only {len(rows)} leaderboard rows")
        if exponent(rows[0]["raw"]) < MIN_EXP:
            raise RuntimeError(f"looks like the wrong board (top value {rows[0]['raw']}); nothing saved")
    except Exception as e:
        save_failure(con, now, e)
        when = (datetime.fromisoformat(prevs[0]["ts_local"]).strftime("%a %H:%M")
                if prevs else "never")
        try:
            notify(f"\u26A0\uFE0F **Tracker check FAILED** at {now.astimezone(TZ):%H:%M}: {e}\n"
                   f"Last good snapshot: {when}. Nothing was invented.")
        except Exception as ne:
            print("notify failed:", ne)
        print("fetch failed:", e, file=sys.stderr)
        return 1

    top = rows[:STORE_N]
    me = find_me(rows)
    if me and me not in top:
        top = top + [me]
    cur_by = {r["name"].lower(): r for r in top}
    prev_row = prevs[0] if prevs else None
    pp_row = prevs[1] if len(prevs) > 1 else None
    prev_by = load_rows(con, prev_row["id"]) if prev_row else {}
    pp_by = load_rows(con, pp_row["id"]) if pp_row else {}
    save(con, now, rows)
    msg = build_report(now, rows, cur_by, prev_row, prev_by, pp_by, pp_row)
    try:
        embed, png = discord_message(now, rows, cur_by, prev_row, prev_by, pp_by, pp_row)
        notify(msg, embed, png)
    except Exception as ne:
        print("notify failed:", ne, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
