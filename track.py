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
COLLECTED = []


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
        text, seen, found_step = "", {}, None
        more = pg.locator("button", has_text=re.compile(r"load more|show more|see more", re.I))
        for step in range(60):  # scroll down the list until I (and the row below me) are loaded
            text = pg.inner_text("body")
            for r in parse_board(text, lenient=True):
                seen[r["rank"]] = r
            if found_step is None and find_me(list(seen.values())):
                found_step = step
            if found_step is not None and step > found_step:
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


def prepare(now_utc, cur_rows, cur_by, prev_row, prev_by, pp_by, pp_row, trend=None):
    """Everything the report says, as plain data + text lines."""
    import random
    local = now_utc.astimezone(TZ)
    rnd = random.Random(local.strftime("%Y%m%d%H"))
    hours = 0
    if prev_row:
        hours = (now_utc - datetime.fromisoformat(prev_row["ts_utc"])).total_seconds() / 3600
    cur_rates = rates(cur_by, prev_by, hours)
    prev_rates = {}
    if pp_row and prev_row:
        h2 = (datetime.fromisoformat(prev_row["ts_utc"])
              - datetime.fromisoformat(pp_row["ts_utc"])).total_seconds() / 3600
        prev_rates = rates(prev_by, pp_by, h2)
    trend_r, trend_h = {}, 0
    if trend:
        trend_h = trend[2]
        trend_r = rates(cur_by, trend[1], trend_h)
    me = find_me(cur_rows)
    by_rank = {r["rank"]: r for r in cur_rows}
    lead = cur_rows[0]
    T = hours_left(now_utc)

    def rate_now(r):
        x = cur_rates.get(r["name"].lower()) if r else None
        return x[1] if x else None

    def speed(r):
        """Steadier speed for projections: trend average if known, else the last interval."""
        x = trend_r.get(r["name"].lower())
        if x is not None:
            return max(0.0, x[1]), f"{dhm(trend_h)} avg"
        n = rate_now(r)
        return (max(0.0, n), "last reading") if n is not None else (None, "")

    shown, hidden = list(cur_rows[:SHOW_N]), 0
    if me and me["rank"] > SHOW_N:
        extra = []
        above = by_rank.get(me["rank"] - 1)
        if above and above["rank"] > SHOW_N:
            extra.append(above)
        extra.append(me)
        hidden = extra[0]["rank"] - SHOW_N - 1
        shown += extra
    sub = (f"vs reading {hm(hours)} ago" + ("  ·  ⚠️ missed checks" if hours > 1.5 else "")
           if prev_row else "Baseline saved - gains start next check")

    players, spikes = [], []
    for r in shown:
        k = r["name"].lower()
        p = prev_by.get(k) if prev_by else None
        d = {"rank": r["rank"], "name": r["name"], "me": r is me,
             "move": (p["rank"] - r["rank"]) if p is not None else 0,
             "value": f"{sh(r['value'])} {r['unit']}".strip(), "delta": "", "tag": None, "gap": "",
             "hidden_before": 0}
        if r["unit"] == lead["unit"]:
            if r["rank"] == 1 and len(cur_rows) > 1 and cur_rows[1]["unit"] == r["unit"]:
                d["gap"] = f"**+{sh(r['value'] - cur_rows[1]['value'])}** lead"
            elif r["rank"] > 1:
                d["gap"] = f"**-{sh(lead['value'] - r['value'])}**"
        if k in cur_rates:
            g, rate, pct = cur_rates[k]
            arrow = ""
            pr = prev_rates.get(k)
            if g == 0:
                d["tag"] = "SLEEPING"
            elif pr and pr[1] > 0 and rate > 0:
                if rate >= 2 * pr[1]:
                    d["tag"] = "ON FIRE"
                    spikes.append((r["name"], rate / pr[1]))
                elif rate > pr[1] * 1.1:
                    d["tag"], arrow = "SURGING", " ↑"
                elif rate < pr[1] * 0.9:
                    d["tag"], arrow = "COOLING", " ↓"
            d["delta"] = f"{sh(g, True)} \u00B7 {sh(rate)}/h{arrow}" + (
                f" \u00B7 {pct:+.2f}%" if pct is not None else "")
        players.append(d)
    if hidden > 0 and len(players) > SHOW_N:
        players[SHOW_N]["hidden_before"] = hidden

    # ------------------------------------------------ the story (what is going on right now)
    color, title, story = 0x5865F2, "\U0001F3AF You", []
    verdict = []   # the endgame block
    if not me:
        story.append(f"\U0001F50D **{ME}** isn't in the {len(cur_rows)} ranks I could load - I'll look again next check.")
    else:
        mr, u = me["rank"], me["unit"]
        mrate = rate_now(me)
        pm_row = prev_by.get(me["name"].lower()) if prev_by else None
        moved = pm_row["rank"] if pm_row is not None else None
        if mr == 1:
            title = rnd.choice(["\U0001F451 Long live the king!", "\U0001F451 Still on the throne",
                                "\U0001F451 The crown suits you", "\U0001F451 Flower royalty"])
            color = 0xF1C40F
            rv = cur_rows[1] if len(cur_rows) > 1 else None
            if rv and rv["unit"] == u:
                gap = me["value"] - rv["value"]
                story.append(f"**{rv['name']}** is **{sh(gap)} {u}** behind you.")
                rr = rate_now(rv)
                if mrate is not None and rr is not None:
                    diff = rr - mrate
                    if diff > 0:
                        color = 0xFEE75C
                        eta = _eta(gap, diff)
                        story.append(f"\U0001F62C Right now they're growing **{sh(diff)}/h** faster than you" +
                                     (f" - at that pace they pass you in **~{eta}**." if eta else "."))
                    elif diff < 0:
                        story.append(f"\U0001F60E You're out-farming them by **{sh(-diff)}/h** right now.")
                    else:
                        story.append("\U0001F91D Same speed as #2 right now - the gap holds.")
        else:
            title = rnd.choice(["\U0001F3C3 The chase is on", "\U0001F3AF Eyes on the crown",
                                "\U0001F331 Growing toward the top", "\U0001F52D Crown in sight"])
            if lead["unit"] == u:
                gap = lead["value"] - me["value"]
                story.append(f"You're **#{mr}** - **{sh(gap)} {u}** behind **{lead['name']}** (**{me['value'] / lead['value'] * 100:.0f}%** of their total).")
                lr = rate_now(lead)
                if mrate is not None and lr is not None:
                    diff = mrate - lr
                    if diff > 0:
                        color = 0x57F287
                        eta = _eta(gap, diff)
                        story.append(f"\U0001F680 You're closing in at **{sh(diff)}/h**" +
                                     (f" - crown in **~{eta}** if both keep this pace!" if eta else "."))
                    elif diff == 0:
                        story.append("\U0001F91D Same speed as #1 - the gap holds.")
                    else:
                        color = 0xED4245
                        story.append(f"\U0001F40C #1 is pulling away by **{sh(-diff)}/h** - "
                                     f"you need **+{sh(-diff)}/h** just to hold the gap.")
            ab = by_rank.get(mr - 1)
            if ab and ab is not lead and ab["unit"] == u:
                ar = rate_now(ab)
                eta = _eta(ab["value"] - me["value"], (mrate - ar) if mrate is not None and ar is not None else None)
                story.append(f"\U0001F4CD Next target: **{ab['name']}** (#{ab['rank']}), {sh(ab['value'] - me['value'])} ahead" +
                             (f" - you pass them in **~{eta}**." if eta else "."))
            if mr > SHOW_N:
                f5 = by_rank.get(SHOW_N)
                if f5 and f5["unit"] == u:
                    story.append(f"\U0001F6A7 {mr - SHOW_N} places and **{sh(f5['value'] - me['value'])}** from the top {SHOW_N}.")
        bl = by_rank.get(mr + 1)
        if mr != 1 and bl and bl["unit"] == u and mrate is not None and rate_now(bl) is not None and rate_now(bl) > mrate:
            eta = _eta(me["value"] - bl["value"], rate_now(bl) - mrate)
            if eta:
                story.append(f"\U0001F440 **{bl['name']}** (#{bl['rank']}) is gaining on you - passes you in **~{eta}**.")
        if moved and moved != mr:
            n = abs(moved - mr)
            story.append((f"\U0001F4C8 You climbed **{n}** place{'s' * (n != 1)} since last check!") if moved > mr
                         else (f"\U0001F4C9 You slipped **{n}** place{'s' * (n != 1)} since last check."))
        for name, x in spikes[:2]:
            story.append(f"\U0001F525 **{name}** is on a farming spree - {x:.1f}x faster than last reading.")

        # -------------------------------------------- endgame: can anyone still pass / can I still catch #1?
        ms, label = speed(me)
        if T and ms is not None:
            verdict.append(f"⏳ **{dhm(T)} left** - projections use each player's **{label}** speed.")
            same = [r for r in cur_rows if r["unit"] == u and r is not me]
            proj = {r["name"]: r["value"] + (speed(r)[0] or 0) * T for r in same + [me]}
            my_final = proj[me["name"]]
            order = sorted(proj, key=lambda n: -proj[n])
            place = order.index(me["name"]) + 1
            medal = {1: "\U0001F947 ", 2: "\U0001F948 ", 3: "\U0001F949 "}.get(place, "")
            verdict.append(f"\U0001F52E Projected finish: {medal}**#{place}** with ~**{sh(my_final)} {u}**.")
            if mr == 1:
                best = None  # the most dangerous rival = needs the smallest speed-up to catch me
                for r in same:
                    rs = speed(r)[0]
                    need = (me["value"] - r["value"]) / T + ms  # their required speed to tie at the end
                    mult = need / rs if rs and rs > 0 else float("inf")
                    if best is None or mult < best[0]:
                        best = (mult, r, need)
                if best:
                    mult, rv, need = best
                    if mult == float("inf"):
                        verdict.append(f"\U0001F512 **Locked in.** Nobody is growing fast enough to matter - {rv['name']} would have to wake up first.")
                    elif mult > 5:
                        verdict.append(f"\U0001F512 **Practically locked.** **{rv['name']}** would need **{mult:.1f}x** their current speed "
                                       f"({sh(need)}/h) for the rest of the event. They can't realistically pass you anymore.")
                    elif mult > 2:
                        verdict.append(f"✅ **Comfortable.** **{rv['name']}** needs **{mult:.1f}x** their speed ({sh(need)}/h) to catch you - possible, but a big ask.")
                    elif mult > 1:
                        verdict.append(f"⚠️ **Still in range!** **{rv['name']}** only needs **{mult:.1f}x** their speed ({sh(need)}/h) to pass you. Keep farming.")
                    else:
                        gap_end = proj[rv["name"]] - my_final
                        verdict.append(f"\U0001F6A8 **{rv['name']} passes you before the end** at current speeds (finishes ~{sh(gap_end)} ahead). Speed up!")
            elif lead["unit"] == u:
                ls = speed(lead)[0] or 0
                need = (lead["value"] - me["value"]) / T + ls
                mult = need / ms if ms > 0 else float("inf")
                if mult == float("inf"):
                    verdict.append(f"\U0001F634 **You're not gaining right now.** To finish #1 you'd need **{sh(need)}/h** - time to farm!")
                elif mult <= 1:
                    verdict.append(f"\U0001F680 **On course for #1** - at current speeds you finish ~{sh(my_final - proj[lead['name']])} ahead of {lead['name']}.")
                elif mult <= 2:
                    verdict.append(f"\U0001F4AA **Within reach.** To finish #1 you need **{sh(need)}/h** (**{mult:.1f}x** your speed).")
                elif mult <= 5:
                    verdict.append(f"\U0001F3D4️ **Big push needed.** You need **{sh(need)}/h** (**{mult:.1f}x** your speed) to take #1 before the end.")
                else:
                    verdict.append(f"\U0001F62C **Out of reach at this pace.** You'd need **{sh(need)}/h** (**{mult:.1f}x** your speed) - unless {lead['name']} stalls.")
            bl2 = by_rank.get(mr + 1)
            if mr != 1 and bl2 and bl2["unit"] == u:
                bs = speed(bl2)[0] or 0
                need_b = (me["value"] - bl2["value"]) / T + ms
                mult_b = need_b / bs if bs > 0 else float("inf")
                if mult_b <= 1:
                    verdict.append(f"\U0001F440 **{bl2['name']}** (#{bl2['rank']}) will pass you before the end at current speeds.")
                elif mult_b <= 3:
                    verdict.append(f"\U0001F440 **{bl2['name']}** (#{bl2['rank']}) can still reach you - needs {mult_b:.1f}x their speed.")
    nxt = (local + timedelta(hours=1)).replace(minute=0)
    foot = f"Next check {nxt:%H:00}" + (f"  ·  closes in {dhm(T)}" if T else "")
    return {"time": f"{local:%H:%M}", "sub": sub, "players": players, "color": color, "foot": foot,
            "title": title, "story": story, "verdict": verdict, "now": now_utc.isoformat()}


def discord_message(now_utc, *args, **kw):
    """ONE embed, text only (plus your Roblox head as a small corner picture)."""
    d = prepare(now_utc, *args, **kw)
    rows = []
    for p in d["players"]:
        if p["hidden_before"]:
            rows.append(f"· · ·  *{p['hidden_before']} more players*  · · ·")
        head = f"{MEDALS.get(p['rank'], '`#' + str(p['rank']) + '`')} **{p['name']}**"
        if p["me"]:
            head += "  \U0001F448 **you**"
        if p["move"]:
            head += f"  {'\U0001F53A' if p['move'] > 0 else '\U0001F53B'}{abs(p['move'])}"
        if p["tag"]:
            head += f"  ·  {TAGS[p['tag']]}"
        line2 = f"`{p['value']}`" + (f" \u00B7 {p['gap']}" if p["gap"] else "")
        line3 = f"*{p['delta']}*" if p["delta"] else ""
        rows.append("\n".join(x for x in (head, line2, line3) if x))
    parts = [f"*{d['sub']}  \u00B7  right = gap to #1*", "", "\n\n".join(rows), "", f"**{d['title']}**", *d["story"]]
    if d["verdict"]:
        parts += ["", "**\U0001F3C1 The endgame**", *d["verdict"]]
    emb = {"title": f"\U0001F338 Flower leaderboard  ·  {d['time']}", "color": d["color"],
           "description": "\n".join(parts)[:4000], "timestamp": d["now"], "footer": {"text": d["foot"]}}
    try:
        icon = headshots([ME]).get(ME.lower())
        if icon:
            emb["author"] = {"name": f"{ME} \u00B7 Flower tracker", "icon_url": icon}
    except Exception as e:
        print("thumbnail failed:", e)
    return emb, None


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
        trend = trend_snapshot(con, now)
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
