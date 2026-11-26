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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

URL = "https://petsuniverse.org/players"
# ---- which leaderboard to track: all set from the workflow file (.github/workflows/track.yml)
BOARD = os.environ.get("TRACK_BOARD", "Total Flowers")    # exact tab name on the page
TITLE = os.environ.get("TRACK_TITLE", "Flower")           # shown as "<TITLE> leaderboard"
EMOJI = os.environ.get("TRACK_EMOJI", "\U0001F338")
ME = os.environ.get("TRACK_USER", "Imnotsadece")
TZ = ZoneInfo("Europe/Stockholm")
DB = os.environ.get("TRACK_DB", "data/tracker.db")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
SHOW_N = 10     # rows shown in the report
STORE_N = 15    # rows stored per snapshot (plus me, wherever I am) - #11-15 show who may enter/leave the top 10
TREND_H = float(os.environ.get("TRACK_TREND_H", "12"))  # judgements use average speed over this window
MIN_GAP_S = 600  # ignore a second run within 10 min (duplicate guard)
MIN_EXP = int(os.environ.get("TRACK_MIN_EXP", "20"))  # flower totals are ~e+25
VAL_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z]{0,8})$")


# ---------------------------------------------------------------- scraping
COLLECTED = []


def fetch_text():
    from playwright.sync_api import sync_playwright
    os.makedirs("data", exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 430, "height": 1800})
        def pressed(btn):
            try:
                return btn.get_attribute("aria-pressed", timeout=2000)
            except Exception:
                return None

        def select_tab(btn):
            if pressed(btn) == "true":
                return True
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
                for _ in range(10):  # give the page up to 5 s to react
                    pg.wait_for_timeout(500)
                    if pressed(btn) == "true":
                        print("tab selected via", how, "click")
                        return True
            return False

        before, ok = "", False
        for attempt in range(1, 4):  # the page sometimes loads slowly: reload and retry
            print(f"--- attempt {attempt} ---")
            try:
                pg.goto(URL, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print("goto problem:", str(e).splitlines()[0][:100])
            pg.wait_for_timeout(2000 + 2500 * attempt)
            before = pg.inner_text("body")
            if attempt == 1:
                print("--- page text BEFORE click (first 40 lines) ---")
                print("\n".join(before.splitlines()[:40]))
            tabs = pg.locator("button", has_text=BOARD)
            print("tab matches:", tabs.count())
            try:
                tabs.first.wait_for(state="visible", timeout=15000)
            except Exception:
                print("tab button not visible yet")
                continue
            if select_tab(tabs.first):
                ok = True
                break
        if not ok:
            raise RuntimeError(f"could not select the {BOARD} tab after 3 tries; nothing saved")
        for _ in range(10):  # wait for the board to actually change
            if pg.inner_text("body") != before:
                break
            pg.wait_for_timeout(800)
        pg.wait_for_timeout(2500)
        text, seen, found_step = "", {}, None
        more = pg.locator("button", has_text=re.compile(r"load more|show more|see more", re.I))
        for step in range(60):  # scroll down the list until I (and the row below me) are loaded
            text = pg.inner_text("body")
            for r in parse_board(text, lenient=True):
                seen[r["rank"]] = r
            if found_step is None and find_me(list(seen.values())):
                found_step = step
            if found_step is not None and step > found_step and max(seen, default=0) > STORE_N:
                break
            try:
                if more.count():
                    more.first.evaluate("e => e.click()")
            except Exception:
                pass
            pg.evaluate("window.scrollBy(0, 1500)")
            pg.mouse.wheel(0, 1500)
            pg.wait_for_timeout(900)
        COLLECTED[:] = [seen[k] for k in sorted(seen)]
        print(f"collected {len(COLLECTED)} rows, ranks {COLLECTED[0]['rank'] if COLLECTED else '-'}"
              f"..{COLLECTED[-1]['rank'] if COLLECTED else '-'}")
        print("--- page text AFTER click (first 40 lines) ---")
        print("\n".join(text.splitlines()[:40]))
        pg.screenshot(path="data/last.png")
        with open("data/last.txt", "w", encoding="utf-8") as f:
            f.write(text)
        b.close()
    return text


def parse_board(text, lenient=False):
    """Rows look like: rank / username / value (e.g. 7.59e+25vg)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for j, l in enumerate(lines):  # skip anything above the tab strip
        if l == BOARD:
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
        has_rank = k >= 1 and lines[k - 1].isdigit()
        if lenient and not has_rank:
            continue
        rank = int(lines[k - 1]) if has_rank else len(rows) + 1
        if rank == 1 and rows:
            break  # a second board started
        if not lenient and rank != len(rows) + 1:
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


def keep_rows(rows):
    """Rows worth storing: the top STORE_N, me, and the players right above/below me."""
    top = rows[:STORE_N]
    me = find_me(rows)
    if me:
        top = top + [r for r in rows if abs(r["rank"] - me["rank"]) <= 1 and r not in top]
    return top


def save(con, now_utc, rows):
    top = keep_rows(rows)
    me = find_me(rows)
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
    out = [f"{TITLE.upper()} LEADERBOARD - {local:%H:%M} ({local:%a %d %b})"]
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


def _eta(gap, speed):
    return dhm(gap / speed) if speed and speed > 0 and gap > 0 else None


def hours_left(now_utc=None):
    """Hours until STOP_AT (the leaderboard closing), or None."""
    stop = os.environ.get("STOP_AT")
    if not stop:
        return None
    try:
        sec = (datetime.fromisoformat(stop) - (now_utc or datetime.now(timezone.utc))).total_seconds()
    except ValueError:
        return None
    return sec / 3600 if sec > 0 else None


def dhm(hours):
    m = int(round(hours * 60))
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + ([f"{mm}m"] if mm and not d else [])
    return " ".join(parts) or "0m"


def bar(ratio, cells=10):
    n = max(0, min(cells, int(round(ratio * cells))))
    return "▰" * n + "▱" * (cells - n)


def trend_snapshot(con, now_utc, want_h=6.0):
    """An older good snapshot (about want_h ago, at least ~40 min) for steadier speeds."""
    best = None
    for r in con.execute("SELECT * FROM snaps2 WHERE ok=1 ORDER BY id DESC LIMIT 80"):
        age = (now_utc - datetime.fromisoformat(r["ts_utc"])).total_seconds() / 3600
        if age < 0.65:
            continue
        if best is None or abs(age - want_h) < abs(best[0] - want_h):
            best = (age, r)
    return (best[1], load_rows(con, best[1]["id"]), best[0]) if best else None


TAGS = {"SLEEPING": "\U0001F634 no gain", "ON FIRE": "\U0001F525 on fire", "SURGING": "\U0001F680 surging",
        "COOLING": "\U0001F9CA cooling"}


def _names(xs):
    xs = [f"**{x}**" for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def prepare(now_utc, cur_rows, cur_by, prev_row, prev_by, pp_by, pp_row, trend=None):
    """Everything the report says, as plain data + text lines.
    All judgements (tags, who passes whom, projections) use each player's average speed over the
    last ~TREND_H hours, never a single hour. Players without that history are named, not guessed."""
    import random
    local = now_utc.astimezone(TZ)
    rnd = random.Random(local.strftime("%Y%m%d%H"))
    hours = 0
    if prev_row:
        hours = (now_utc - datetime.fromisoformat(prev_row["ts_utc"])).total_seconds() / 3600
    cur_rates = rates(cur_by, prev_by, hours)
    trend_r, trend_h, trend_by = {}, 0, {}
    if trend:
        trend_by, trend_h = trend[1], trend[2]
        trend_r = rates(cur_by, trend_by, trend_h)
    win = (f"{trend_h:.0f}h" if trend_h >= 1 else dhm(trend_h)) if trend_r else ""
    me = find_me(cur_rows)
    by_rank = {r["rank"]: r for r in cur_rows}
    lead = cur_rows[0]
    u = lead["unit"]
    T = hours_left(now_utc)

    def last(r):
        x = cur_rates.get(r["name"].lower()) if r else None
        return x[1] if x else None

    def avg(r):
        x = trend_r.get(r["name"].lower()) if r else None
        return max(0.0, x[1]) if x else None

    # ------------------------------------------------ the board
    shown, hidden = list(cur_rows[:SHOW_N]), 0
    if me and me["rank"] > SHOW_N:
        extra = []
        above = by_rank.get(me["rank"] - 1)
        if above and above["rank"] > SHOW_N:
            extra.append(above)
        extra.append(me)
        hidden = extra[0]["rank"] - SHOW_N - 1
        shown += extra
    sub = (f"right = gap to #1  ·  tags = last hour vs {win} average" if win else "right = gap to #1")
    if prev_row and hours > 1.5:
        sub += "  ·  ⚠️ missed checks"
    if not prev_row:
        sub = "Baseline saved - gains start next check"

    players = []
    for r in shown:
        k = r["name"].lower()
        old = trend_by.get(k)
        d = {"rank": r["rank"], "name": r["name"], "me": r is me, "move": 0, "new": False,
             "value": f"{sh(r['value'])} {r['unit']}".strip(), "delta": "", "tag": None, "gap": "",
             "hidden_before": 0}
        if trend_by:
            if old is not None:
                d["move"] = old["rank"] - r["rank"]
            elif r["rank"] <= STORE_N:
                d["new"] = True  # was not even in the stored top STORE_N
        if r["unit"] == u:
            if r["rank"] == 1 and len(cur_rows) > 1 and cur_rows[1]["unit"] == u:
                d["gap"] = f"**+{sh(r['value'] - cur_rows[1]['value'])}** lead"
            elif r["rank"] > 1:
                d["gap"] = f"**-{sh(lead['value'] - r['value'])}**"
        g = cur_rates.get(k)
        a = avg(r)
        if g is not None:
            l = g[1]
            if g[0] == 0:
                d["tag"] = "SLEEPING"
            elif a and a > 0 and l > 0:
                if l >= 2 * a:
                    d["tag"] = "ON FIRE"
                elif l >= 1.25 * a:
                    d["tag"] = "SURGING"
                elif l <= 0.6 * a:
                    d["tag"] = "COOLING"
        bits = []
        if g is not None:
            bits.append(f"1h {sh(g[0], True)}")
        if a is not None:
            bits.append(f"{win} avg {sh(a)}/h")
        d["delta"] = " · ".join(bits)
        players.append(d)
    if hidden > 0 and len(players) > SHOW_N:
        players[SHOW_N]["hidden_before"] = hidden

    # ------------------------------------------------ top 10: what happened and what's coming
    race = []
    pool = [r for r in cur_rows[:STORE_N + 1] if r["unit"] == u]
    nodata = [r["name"] for r in pool[:10] if avg(r) is None]
    if trend_by:
        old_top = sorted((r for r in trend_by.values() if r["rank"] <= 10), key=lambda r: r["rank"])
        now_top = {r["name"].lower() for r in pool[:10]}
        for r in pool[:10]:
            o = trend_by.get(r["name"].lower())
            if o is None or o["rank"] > 10:
                was = f"#{o['rank']}" if o is not None else "outside the top 10"
                race.append(f"\U0001F195 **{r['name']}** entered the top 10 → now **#{r['rank']}** (was {was} {win} ago)")
        for o in old_top:
            if o["name"].lower() not in now_top:
                c = cur_by.get(o["name"].lower())
                race.append(f"\U0001F4E4 **{o['name']}** fell out of the top 10 (#{o['rank']} → "
                            f"{'#' + str(c['rank']) if c is not None else 'below #' + str(STORE_N)})")
        moves = [(trend_by[r["name"].lower()]["rank"] - r["rank"], r) for r in pool[:10]
                 if r["name"].lower() in trend_by and trend_by[r["name"].lower()]["rank"] <= 10]
        up = max(moves, key=lambda m: m[0], default=None)
        dn = min(moves, key=lambda m: m[0], default=None)
        if up and up[0] >= 2:
            race.append(f"\U0001F4C8 Biggest climber: **{up[1]['name']}** +{up[0]} places in {win} (now #{up[1]['rank']})")
        if dn and dn[0] <= -2:
            race.append(f"\U0001F4C9 Biggest drop: **{dn[1]['name']}** {dn[0]} places in {win} (now #{dn[1]['rank']})")

    sp = [(avg(r), r) for r in pool[:10] if avg(r) is not None]
    if sp:
        f = max(sp, key=lambda x: x[0])
        race.append(f"⚡ Fastest in the top 10: **{f[1]['name']}** (#{f[1]['rank']}) · {sh(f[0])}/h over {win}")
        idle = [r["name"] for a, r in sp if a == 0]
        if idle:
            race.append(f"\U0001F634 No gain at all in {win}: {_names(idle)}")

    # overtakes that happen before the end at current (window-average) speeds
    passes = []
    for i in range(len(pool) - 1):
        hi, lo = pool[i], pool[i + 1]
        sa, sb = avg(hi), avg(lo)
        if sa is None or sb is None or sb <= sa:
            continue
        eta = (hi["value"] - lo["value"]) / (sb - sa)
        if T is None or eta <= T:
            passes.append((eta, lo, hi, sb, sa))
    passes.sort(key=lambda x: x[0])
    if passes:
        race.append("")
        race.append(f"**\U0001F51C Overtakes coming** *(at {win} average speeds)*")
        for eta, lo, hi, sb, sa in passes[:4]:
            race.append(f"• **{lo['name']}** (#{lo['rank']}) passes **{hi['name']}** (#{hi['rank']}) in **~{dhm(eta)}**"
                        f"\n {sh(sb)}/h vs {sh(sa)}/h")

    # projected final order (only players with a known average speed)
    if T and sp:
        known = [r for r in pool if avg(r) is not None]
        proj = sorted(known, key=lambda r: -(r["value"] + avg(r) * T))
        final = {r["name"]: i + 1 for i, r in enumerate(proj)}
        cur_place = {r["name"]: i + 1 for i, r in enumerate(known)}  # same set, so places are comparable
        lines = []
        for r in known:
            a, b = cur_place[r["name"]], final[r["name"]]
            if b < a and b <= 10:
                passed = [x for x in known if cur_place[x["name"]] < a and final[x["name"]] > b]
                who = ", ".join(f"**{x['name']}** ({sh(avg(x))}/h)" for x in passed[:2])
                if len(passed) > 2:
                    who += f" +{len(passed) - 2} more"
                lines.append(f"\U0001F53A **{r['name']}** #{r['rank']} → **#{r['rank'] - (a - b)}**"
                             f" · {sh(avg(r))}/h" + (f"\n passes {who}" if who else ""))
            elif b > a and a <= 10:
                lines.append(f"\U0001F53B **{r['name']}** #{r['rank']} → #{r['rank'] + (b - a)}"
                             + (" (out of the top 10)" if r["rank"] <= 10 < r["rank"] + (b - a) else ""))
        race.append("")
        race.append(f"**\U0001F52E Final top 10 if everyone keeps their {win} pace**")
        race += lines[:7] if lines else ["• No changes: every place holds until the end."]
    if nodata and len(nodata) == len(pool[:10]):
        race.append("ℹ️ Not enough history yet - predictions start after a few hourly checks.")
    elif nodata:
        race.append(f"ℹ️ No {win or 'trend'} history yet for {_names(nodata)}, so they're left out of predictions.")

    # ------------------------------------------------ you
    color, title, story, verdict = 0x5865F2, "\U0001F3AF You", [], []
    if not me:
        story.append(f"\U0001F50D **{ME}** isn't in the {len(cur_rows)} ranks I could load - I'll look again next check.")
    else:
        mr = me["rank"]
        ms = avg(me)
        if mr == 1:
            title = rnd.choice(["\U0001F451 Long live the king!", "\U0001F451 Still on the throne",
                                "\U0001F451 The crown suits you", f"\U0001F451 {TITLE} royalty"])
            color = 0xF1C40F
        else:
            title = rnd.choice(["\U0001F3C3 The chase is on", "\U0001F3AF Eyes on the crown",
                                "\U0001F331 Growing toward the top", "\U0001F52D Crown in sight"])
        if me["unit"] == u:
            if mr == 1 and len(cur_rows) > 1:
                story.append(f"You're **#1**, **{sh(me['value'] - cur_rows[1]['value'])}** ahead of **{cur_rows[1]['name']}**.")
            elif mr > 1:
                story.append(f"You're **#{mr}**, **{sh(lead['value'] - me['value'])}** behind **{lead['name']}** "
                             f"({me['value'] / lead['value'] * 100:.0f}% of their total).")
        if ms is not None:
            story.append(f"\U0001F4CA Your {win} average: **{sh(ms)}/h**" +
                         (f" (last hour {sh(last(me))}/h)" if last(me) is not None else ""))
        m2 = by_rank.get(mr + 1)
        if trend_by and me["name"].lower() in trend_by:
            mv = trend_by[me["name"].lower()]["rank"] - mr
            if mv:
                story.append(f"\U0001F4C8 Up **{mv}** place{'s' * (abs(mv) != 1)} in {win}." if mv > 0
                             else f"\U0001F4C9 Down **{-mv}** place{'s' * (abs(mv) != 1)} in {win}.")

        # -------------------------------------------- endgame
        if T and ms is not None and me["unit"] == u:
            known = [r for r in cur_rows[:STORE_N + 1] if r["unit"] == u and avg(r) is not None]
            if me not in known:
                known.append(me)
            fin = {r["name"]: r["value"] + avg(r) * T for r in known}
            place = sorted(fin, key=lambda n: -fin[n]).index(me["name"]) + 1
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(place, "\U0001F52E")
            verdict.append(f"{medal} You finish **#{place}** with ~**{sh(fin[me['name']])} {u}**")
            if mr == 1:
                threats = []
                for r in known:
                    if r is me or not avg(r):
                        continue
                    need = (me["value"] - r["value"]) / T + ms
                    threats.append((need / avg(r), r, need))
                if threats:
                    mult, r, need = min(threats, key=lambda x: x[0])
                    if mult > 5:
                        verdict.append(f"\U0001F512 **Safe.** Closest threat {r['name']} would need **{mult:.1f}×** their speed.")
                    elif mult > 1:
                        verdict.append(f"⚠️ **{r['name']}** can still catch you with **{mult:.1f}×** their speed ({sh(need)}/h).")
                    else:
                        verdict.append(f"\U0001F6A8 **{r['name']}** passes you before the end at this pace. Speed up!")
            elif avg(lead) is None:
                verdict.append(f"\U0001F451 **#1 {lead['name']}**: no {win} history yet, can't project the race for #1")
            else:
                need = (lead["value"] - me["value"]) / T + avg(lead)
                mult = need / ms if ms > 0 else float("inf")
                mark = ("✅ on course" if mult <= 1 else "\U0001F4AA within reach" if mult <= 2
                        else "\U0001F3D4️ big push" if mult <= 5 else "❌ out of reach")
                verdict.append(f"\U0001F451 **#1 {lead['name']}**: needs **{sh(need)}/h**"
                               + (f" = **{mult:.1f}×** your speed" if mult != float('inf') else "")
                               + f" → {mark}")
                ab = by_rank.get(mr - 1)
                if ab and ab is not lead and ab["unit"] == u and avg(ab) is not None:
                    gap, diff = ab["value"] - me["value"], ms - avg(ab)
                    if diff > 0:
                        eta = gap / diff
                        ok = "✅ before the end" if eta <= T else "❌ not before the end"
                        verdict.append(f"⬆️ **#{ab['rank']} {ab['name']}**: you pass them in ~{dhm(eta)} → {ok}")
                    else:
                        verdict.append(f"⬆️ **#{ab['rank']} {ab['name']}**: they're faster ({sh(avg(ab))}/h) → ❌ you won't catch them")
            bl = m2
            if bl and bl["unit"] == u and avg(bl) is not None:
                diff = avg(bl) - ms
                if diff > 0:
                    eta = (me["value"] - bl["value"]) / diff
                    if eta <= T:
                        verdict.append(f"⚠️ **#{bl['rank']} {bl['name']}** passes you in **~{dhm(eta)}** → speed up to hold")
                    else:
                        verdict.append(f"\U0001F6E1️ **#{bl['rank']} {bl['name']}** is faster but runs out of time → you hold")
                else:
                    verdict.append(f"\U0001F6E1️ **#{bl['rank']} {bl['name']}** is slower → you hold your place")
    nxt = (local + timedelta(hours=1)).replace(minute=0)
    foot = f"Next check {nxt:%H:00}" + (f"  ·  closes in {dhm(T)}" if T else "")
    return {"time": f"{local:%H:%M}", "sub": sub, "players": players, "color": color, "foot": foot,
            "title": title, "story": story, "verdict": verdict, "race": race, "win": win,
            "left": dhm(T) if T else "", "now": now_utc.isoformat()}


def discord_message(now_utc, *args, **kw):
    """Two embeds: the board, then the analysis (top 10 race, you, endgame)."""
    d = prepare(now_utc, *args, **kw)
    rows = []
    for p in d["players"]:
        if p["hidden_before"]:
            rows.append(f"· · ·  *{p['hidden_before']} more players*  · · ·")
        head = f"{MEDALS.get(p['rank'], '`#' + str(p['rank']) + '`')} **{p['name']}**"
        if p["me"]:
            head += "  \U0001F448 **you**"
        if p["new"]:
            head += "  \U0001F195"
        elif p["move"]:
            arrow = "\U0001F53A" if p["move"] > 0 else "\U0001F53B"
            head += f"  {arrow}{abs(p['move'])}"
        if p["tag"]:
            head += f"  ·  {TAGS[p['tag']]}"
        line2 = f"`{p['value']}`" + (f" · {p['gap']}" if p["gap"] else "")
        line3 = f"*{p['delta']}*" if p["delta"] else ""
        rows.append("\n".join(x for x in (head, line2, line3) if x))
    board = "\n".join([f"*{d['sub']}*", f"*\U0001F53A\U0001F53B = places moved in {d['win']}*" if d["win"] else "",
                       "", "\n\n".join(rows)])
    emb1 = {"title": f"{EMOJI} {TITLE} leaderboard  ·  {d['time']}", "color": d["color"],
            "description": board[:4000]}
    parts = []
    if d["race"]:
        parts += [f"**\U0001F51F Top 10 · last {d['win']}**" if d["win"] else "**\U0001F51F Top 10**", *d["race"], ""]
    parts += [f"**{d['title']}**", *d["story"]]
    if d["verdict"]:
        parts += ["", f"**\U0001F3C1 Endgame** · {d['left']} left", *d["verdict"]]
    emb2 = {"color": d["color"], "description": "\n".join(parts)[:4000],
            "timestamp": d["now"], "footer": {"text": d["foot"]}}
    try:
        icon = headshots([ME]).get(ME.lower())
        if icon:
            emb1["author"] = {"name": f"{ME} · {TITLE} tracker", "icon_url": icon}
    except Exception as e:
        print("thumbnail failed:", e)
    return [emb1, emb2], None


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
def main(quiet_fail=False):
    con = connect()
    now = datetime.now(timezone.utc)
    prevs = last_ok(con, 2)
    if prevs and not os.environ.get("FORCE"):
        age = (now - datetime.fromisoformat(prevs[0]["ts_utc"])).total_seconds()
        if age < MIN_GAP_S:
            print(f"snapshot {int(age)}s ago exists - skipping duplicate run")
            return 0
    try:
        text = fetch_text()
        rows = list(COLLECTED) or parse_board(text)
        if [r["rank"] for r in rows[:SHOW_N]] != list(range(1, SHOW_N + 1)):
            raise RuntimeError(f"top {SHOW_N} not read cleanly (got {len(rows)} rows)")
        if exponent(rows[0]["raw"]) < MIN_EXP:
            raise RuntimeError(f"looks like the wrong board (top value {rows[0]['raw']}); nothing saved")
    except Exception as e:
        save_failure(con, now, e)
        when = (datetime.fromisoformat(prevs[0]["ts_local"]).strftime("%a %H:%M")
                if prevs else "never")
        try:
            if not quiet_fail:  # a retry is coming - only the last failure is announced
              notify(f"\u26A0\uFE0F **Tracker check FAILED** at {now.astimezone(TZ):%H:%M}: {e}\n"
                   f"Last good snapshot: {when}. Nothing was invented.")
        except Exception as ne:
            print("notify failed:", ne)
        print("fetch failed:", e, file=sys.stderr)
        return 1

    top = keep_rows(rows)
    cur_by = {r["name"].lower(): r for r in top}
    prev_row = prevs[0] if prevs else None
    pp_row = prevs[1] if len(prevs) > 1 else None
    prev_by = load_rows(con, prev_row["id"]) if prev_row else {}
    pp_by = load_rows(con, pp_row["id"]) if pp_row else {}
    try:
        trend = trend_snapshot(con, now, TREND_H)
    except Exception as e:
        print("trend failed:", e)
        trend = None
    save(con, now, rows)
    msg = build_report(now, rows, cur_by, prev_row, prev_by, pp_by, pp_row)
    try:
        embed, png = discord_message(now, rows, cur_by, prev_row, prev_by, pp_by, pp_row, trend=trend)
        notify(msg, embed, png)
    except Exception as ne:
        print("notify failed:", ne, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
