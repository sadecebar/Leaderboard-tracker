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
        loc = pg.get_by_text("Total Flowers", exact=True)
        n = loc.count()
        if n == 0:
            loc = pg.get_by_text("Total Flowers")
            n = loc.count()
        print("tab matches:", n)
        for i in range(n):
            try:
                loc.nth(i).scroll_into_view_if_needed(timeout=3000)
                loc.nth(i).click(timeout=5000)
                print("clicked match", i)
                break
            except Exception as e:
                print("click failed", i, str(e).splitlines()[0][:120])
        pg.wait_for_timeout(3000)
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
        name = lines[i - 1]
        if name.isdigit():
            continue
        rank = int(lines[i - 2]) if i >= 2 and lines[i - 2].isdigit() else len(rows) + 1
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


# ---------------------------------------------------------------- notify
def notify(msg):
    print(msg)
    if not WEBHOOK:
        print("[no DISCORD_WEBHOOK_URL set - not sent]")
        return False
    body = json.dumps({"content": "```\n" + msg[:1900] + "\n```"}).encode()
    req = urllib.request.Request(WEBHOOK, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "pu-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
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
            notify(f"Tracker check FAILED at {now.astimezone(TZ):%H:%M}: {e}\n"
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
        notify(msg)
    except Exception as ne:
        print("notify failed:", ne, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
