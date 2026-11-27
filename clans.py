"""Clan War clan tracker: every hour, the top 10 clans plus the clans I follow (even outside the top 10).

Data: the site's public API (r=clans&sort=season = the war standings, r=clans&q= for followed clans),
the same numbers as the Clans tab on https://petsuniverse.org/players. History goes to data/clans.json,
one snapshot per check, so every gain and speed is measured, never guessed.

Run: python clans.py            -> loop for ~5h45m, a check at every :00, then exit (the workflow restarts it)
     PREVIEW=true python clans.py -> one report now, nothing saved
"""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

import lbcard
import track  # site API, Discord sending, number format, colors (TRACK_ACCENT / LABEL / HEADER)

TZ = track.TZ
STATE = os.environ.get("CLANS_DB", "data/clans.json")
FOLLOW = [x.strip() for x in os.environ.get("CLANS_FOLLOW", "Sade,TURKIYE").split(",") if x.strip()]
MY_CLAN = os.environ.get("CLANS_MINE", "").strip().upper()  # tag of my own clan, marked on the picture
TOP_N = 10
SPEED_H = 3          # speeds = average points per hour over the last 3 hours
KEEP_DAYS = 9
RUN_MIN = 345        # the job restarts itself after this
SE = lbcard.e
if os.environ.get("CLANS_WEBHOOK_URL"):  # clan reports go to their own channel
    track.WEBHOOK = os.environ["CLANS_WEBHOOK_URL"]


# ---------------------------------------------------------------- data
def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"clans": {}, "snaps": []}


def save(st):
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))


def fetch():
    """-> (clans {id: {name, tag, icon, rank, points}}, battle info)."""
    d = track._api("r=clans&sort=season")
    lst = d.get("list") or []
    if len(lst) < TOP_N:
        raise RuntimeError(f"the clan board only had {len(lst)} clans")
    out = {c["id"]: c for c in lst}
    for name in FOLLOW:
        if any(c["name"].lower() == name.lower() or c["tag"].lower() == name.lower() for c in out.values()):
            continue
        hits = track._api("r=clans&q=" + urllib.request.quote(name)).get("list") or []
        hit = next((c for c in hits if c["name"].lower() == name.lower()), None) or \
            next((c for c in hits if c["tag"].lower() == name.lower()), None)
        if hit:
            out[hit["id"]] = hit
        else:
            print("followed clan not found:", name)
    clans = {i: {"name": c["name"], "tag": c["tag"], "icon": c.get("icon"),
                 "rank": None, "points": float(c.get("points") or 0)} for i, c in out.items()}
    # place = order by war points (the site's own place number can lag behind the points);
    # a followed clan below the board's last clan stays unranked
    board = sorted(lst, key=lambda c: -float(c.get("points") or 0))
    floor = float(board[-1].get("points") or 0)
    for n, i in enumerate(sorted((i for i in clans if clans[i]["points"] >= floor or i in {c["id"] for c in lst}),
                                 key=lambda i: -clans[i]["points"]), 1):
        if n <= len(lst):
            clans[i]["rank"] = n
    return clans, d.get("battle") or {}


def snap_at(st, t, max_age_h):
    """Newest snapshot at or before time t (and not older than t - max_age_h)."""
    best = None
    for s in st["snaps"]:
        ts = datetime.fromisoformat(s["t"])
        if ts <= t and ts >= t - timedelta(hours=max_age_h):
            best = s
    return best


# ---------------------------------------------------------------- analysis
def analyse(st, now, clans, battle):
    prev = snap_at(st, now - timedelta(minutes=40), 3)           # ~1 hour ago
    old = snap_at(st, now - timedelta(hours=SPEED_H - 0.4), SPEED_H + 3)  # ~3 hours ago
    if old is None and st["snaps"]:                               # early on: the oldest we have
        old = next((s for s in st["snaps"] if now - datetime.fromisoformat(s["t"]) >= timedelta(minutes=40)), None)
    midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today = next((s for s in st["snaps"] if datetime.fromisoformat(s["t"]) >= midnight), None)

    def pts(s, i):
        v = s["c"].get(i) if s else None
        return v[1] if v else None

    rows = []
    for i, c in clans.items():
        r = dict(c, id=i)
        p1 = pts(prev, i)
        r["hour"] = c["points"] - p1 if p1 is not None else None
        r["prev_rank"] = prev["c"][i][0] if prev and prev["c"].get(i) else None
        po = pts(old, i)
        if po is not None:
            h = (now - datetime.fromisoformat(old["t"])).total_seconds() / 3600
            r["speed"], r["speed_h"] = (c["points"] - po) / h, h
        else:
            r["speed"], r["speed_h"] = None, None
        pt = pts(today, i)
        r["today"] = c["points"] - pt if pt is not None else None
        r["today_from"] = datetime.fromisoformat(today["t"]).astimezone(TZ) if today else None
        r["follow"] = any(c["name"].lower() == f.lower() or c["tag"].lower() == f.lower() for f in FOLLOW)
        r["mine"] = bool(MY_CLAN) and c["tag"].upper() == MY_CLAN
        rows.append(r)
    ranked = sorted([r for r in rows if r["rank"]], key=lambda r: r["rank"])
    speeds = sorted([r["speed"] for r in ranked if r["speed"] is not None], reverse=True)
    top = ranked[:TOP_N]
    fast = max(top, key=lambda r: r["speed"] or -1)
    for r in rows:
        r["tag_txt"] = ""
        if r["speed"] is not None:
            if r["speed"] <= 0:
                r["tag_txt"] = "SLEEPING"
            elif r is fast:
                r["tag_txt"] = "ON FIRE"
            r["speed_pos"] = 1 + sum(1 for s in speeds if s > r["speed"]) if speeds else None
    end = datetime.fromtimestamp(battle["end"], timezone.utc) if battle.get("end") else None
    return {"ranked": ranked, "top": top, "rows": rows, "speeds": speeds, "end": end,
            "cut": int(battle.get("top") or 25), "has_speed": bool(speeds),
            "prev_t": datetime.fromisoformat(prev["t"]).astimezone(TZ) if prev else None}


def hours_txt(h):
    if h is None or h < 0:
        return None
    d, rest = divmod(int(round(h)), 24)
    return f"{d}d {rest}h" if d else (f"{rest}h" if rest else "<1h")


def eta(gap, speed_diff):
    return gap / speed_diff if speed_diff and speed_diff > 0 else None


def story(A, r, now):
    """Plain-language lines about one followed clan."""
    sh = track.sh
    out, left = [], ((A["end"] - now).total_seconds() / 3600 if A["end"] else None)
    ranked = A["ranked"]
    gained = []
    if r["hour"] is not None:
        gained.append(f"+{sh(r['hour'])} last hour")
    if r["today"] is not None:
        since = "today" if r["today_from"] and r["today_from"].hour == 0 else f"since {r['today_from']:%H:%M}"
        gained.append(f"+{sh(r['today'])} {since}")
    if r["speed"] is not None:
        gained.append(f"{sh(r['speed'])}/h avg ({hours_txt(r['speed_h'])})")
    if gained:
        out.append(" · ".join(gained))

    if r["speed"] is None:
        verdict = ("⏳", "first check: speed shows from the next one")
    elif r["speed"] <= 0:
        verdict = ("😴", "no points gained lately")
    else:
        n, pos = len(A["speeds"]), r.get("speed_pos") or 0
        if pos <= max(1, n // 5):
            verdict = ("🔥", f"doing great: #{pos} fastest of the {n} clans")
        elif pos <= n // 2:
            verdict = ("👍", f"doing well: #{pos} fastest of the {n} clans")
        else:
            verdict = ("🐢", f"slower than most: #{pos} fastest of the {n} clans")

    if not r["rank"]:
        out.append("Not in the war ranking (top 50) right now.")
        last = ranked[-1] if ranked else None
        if last:
            gap = last["points"] - r["points"]
            line = f"To get in: pass #{last['rank']} **{last['name']}** ({sh(gap)} pts behind)"
            t = eta(gap, (r["speed"] or 0) - (last["speed"] or 0)) if r["speed"] is not None else None
            out.append(line + (f", ~{hours_txt(t)} at current speeds." if t is not None else "."))
        return verdict, out

    above = next((x for x in ranked if x["rank"] == r["rank"] - 1), None)
    below = next((x for x in ranked if x["rank"] == r["rank"] + 1), None)
    if r["prev_rank"] and r["prev_rank"] != r["rank"]:
        d = r["prev_rank"] - r["rank"]
        out.append(("⬆️ up " if d > 0 else "⬇️ down ") + f"{abs(d)} place{'s' if abs(d) > 1 else ''} since last hour")
    if above:
        gap = above["points"] - r["points"]
        t = eta(gap, (r["speed"] or 0) - (above["speed"] or 0)) if r["speed"] is not None and above["speed"] is not None else None
        when = (f" · passes them in ~{hours_txt(t)}" + (" (before the end)" if left and t < left else " (not before the end)")) \
            if t is not None else (" · not catching up at current speeds" if r["speed"] is not None and above["speed"] is not None else "")
        out.append(f"#{above['rank']} **{above['name']}** is {sh(gap)} ahead{when}")
    if below:
        gap = r["points"] - below["points"]
        t = eta(gap, (below["speed"] or 0) - (r["speed"] or 0)) if r["speed"] is not None and below["speed"] is not None else None
        out.append(f"#{below['rank']} **{below['name']}** is {sh(gap)} behind"
                   + (f" and catching up (~{hours_txt(t)})" if t is not None else (" and falling back" if below["speed"] is not None and r["speed"] is not None else "")))
    for goal, label in ((A["cut"], "rewards"), (TOP_N, "top 10")):
        if r["rank"] > goal:
            g = next((x for x in ranked if x["rank"] == goal), None)
            if g:
                out.append(f"🎯 {label}: {sh(g['points'] - r['points'])} pts to #{goal} **{g['name']}**")
            break
    else:
        if r["rank"] <= A["cut"]:
            out.append(f"🏆 inside the top {A['cut']}: gets war rewards if it holds")
    return verdict, out


# ---------------------------------------------------------------- picture + post
def icons(rows):
    ids = [str(r["icon"]) for r in rows if r.get("icon")]
    if not ids:
        return {}
    try:
        url = ("https://thumbnails.roblox.com/v1/assets?assetIds=" + ",".join(ids)
               + "&returnPolicy=PlaceHolder&size=150x150&format=Png&isCircular=false")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": track.UA}), timeout=15) as f:
            by = {str(t["targetId"]): t.get("imageUrl") for t in json.load(f)["data"]}
        return {r["name"].lower(): by.get(str(r["icon"])) for r in rows if r.get("icon")}
    except Exception as e:
        print("clan icons failed:", str(e)[:100])
        return {}


TAGS = {"SLEEPING": ("😴 no gain", "#9a9ab0"), "ON FIRE": ("🔥 fastest in top 10", "#fb923c")}


def row_html(r, heads, top_speed):
    sh = track.sh
    bg, fg = lbcard.MEDAL.get(r["rank"], ("#ffffff14", "#d7d7e3"))
    mv = ""
    if r["rank"] and r["prev_rank"] and r["prev_rank"] != r["rank"]:
        d = r["prev_rank"] - r["rank"]
        mv = f'<span class="mv {"up" if d > 0 else "dn"}">{"▲" if d > 0 else "▼"}{abs(d)}</span>'
    badge = '<span class="you">YOUR CLAN</span>' if r["mine"] else ('<span class="you">WATCHING</span>' if r["follow"] else "")
    tag = ""
    if r["tag_txt"] in TAGS:
        t, c = TAGS[r["tag_txt"]]
        tag = f'<span class="tg" style="color:{c};border-color:{c}55">{t}</span>'
    sub = f'+{sh(r["hour"])} last hour' if r["hour"] is not None else "first check"
    if r["speed"] is not None:
        w = max(3, int(max(r["speed"], 0) / (top_speed or 1) * 100))
        spd = f'<div class="bar"><i style="width:{w}%"></i></div><div class="sp">{SE(sh(r["speed"]))}/h</div>'
    else:
        spd = '<div class="sp none">speed from the next check</div>'
    hl = " me" if (r["follow"] or r["mine"]) else ""
    return (f'<div class="row{hl}{" first" if r["rank"] == 1 else ""}">'
            f'<div class="rk" style="background:{bg};color:{fg}">{r["rank"] or "–"}</div>{lbcard.av(r["name"], heads, 44)}'
            f'<div class="mid"><div class="nm">{SE(r["name"])} <small style="color:#9aaba4;font-size:13px">[{SE(r["tag"])}]</small> {badge}{mv}</div>'
            f'<div class="sub">{SE(sub)} {tag}</div><div class="spd">{spd}</div></div>'
            f'<div class="val">{SE(sh(r["points"]))}</div></div>')


def bold(text):
    """**x** -> <b>x</b>, everything else escaped."""
    return lbcard.re.sub(r"[*][*](.+?)[*][*]", r"<b>\1</b>", SE(text))


def build_pages(A, now):
    loc = now.astimezone(TZ)
    when = f"{loc:%a %d %b} · {loc:%H:%M}"
    heads = icons(A["rows"])
    shown = list(A["top"])
    extra = sorted([r for r in A["rows"] if (r["follow"] or r["mine"]) and r not in shown],
                   key=lambda r: (r["rank"] or 999, -r["points"]))
    top_speed = max([r["speed"] for r in shown + extra if r["speed"]] or [1])
    rows = [row_html(r, heads, top_speed) for r in shown]
    if extra:
        rows.append('<div class="gap">· · · watching · · ·</div>')
        rows += [row_html(r, heads, top_speed) for r in extra]
    left = hours_txt((A["end"] - now).total_seconds() / 3600) if A["end"] and A["end"] > now else None
    chips = (f'<span class="chip end">⏳ ends in {SE(left)}</span>' if left else "") + \
            f'<span class="chip">top {A["cut"]} clans get rewards</span>'
    board = lbcard._page(f'''<div class="card">
 <div class="top"><div class="tag">{SE(lbcard.THEME.get("label") or "CLANS")}</div><div class="when">{SE(when)}</div></div>
 <h1>🛡️ Clan War · Top clans</h1><div class="meta">{chips}</div>
 {"".join(rows)}
 <div class="foot"><span>Bars = average points per hour (last {SPEED_H}h) · every hour</span><span>Pets Universe · Clan Tracker</span></div>
</div>''')

    secs = []
    for r in [x for x in A["rows"] if x["follow"]] + [x for x in A["rows"] if x["mine"] and not x["follow"]]:
        (icon, verdict), lines = story(A, r, now)
        place = f"#{r['rank']}" if r["rank"] else "unranked"
        body = "".join(f'<div class="ln"><div class="t">{bold(l)}</div></div>' for l in lines)
        secs.append(f'''<h2>{SE(r["name"])} [{SE(r["tag"])}] · {SE(place)} · {SE(track.sh(r["points"]))} pts</h2>
<div class="box"><div class="ln"><div class="eta">{icon}</div><div class="t"><b>{SE(verdict)}</b></div></div>{body}</div>''')
    analysis = lbcard._page(f'''<div class="card">
 <div class="top"><div class="tag">{SE((lbcard.THEME.get("label") + " · ") if lbcard.THEME.get("label") else "")}WATCHING</div><div class="when">{SE(when)}</div></div>
 {"".join(secs)}
 <div class="note">Speeds compare all {len(A["speeds"])} clans on the war board. Times assume everyone keeps their current pace.</div>
</div>''') if secs else None
    return board, analysis


def text_report(A, now):
    sh = track.sh
    lines = [f"🛡️ **Clan War · top clans** · {now.astimezone(TZ):%H:%M}"]
    for r in A["top"]:
        g = f" (+{sh(r['hour'])})" if r["hour"] is not None else ""
        lines.append(f"`#{r['rank']}` **{r['name']}** [{r['tag']}] {sh(r['points'])}{g}")
    for r in [x for x in A["rows"] if x["follow"] or x["mine"]]:
        (icon, verdict), ls = story(A, r, now)
        lines.append(f"\n{icon} **{r['name']}** [{r['tag']}] {('#' + str(r['rank'])) if r['rank'] else 'unranked'} "
                     f"· {sh(r['points'])} pts · {verdict}")
        lines += ["  " + l for l in ls]
    return "\n".join(lines)


def post(A, now):
    msg = text_report(A, now)
    try:
        board, analysis = build_pages(A, now)
        pages = [board] + ([analysis] if analysis else [])
        pngs = lbcard.render(pages)
        if all(pngs):
            names = ["board.png", "analysis.png"][:len(pngs)]
            embeds = [{"image": {"url": f"attachment://{n}"}} for n in names]
            embeds[-1].update(timestamp=now.isoformat(),
                              footer={"text": f"Next check {(now.astimezone(TZ) + timedelta(hours=1)):%H}:00"})
            if track.notify(msg, embeds, list(zip(names, pngs))):
                return True
    except Exception as e:
        print("clan cards failed, sending text:", str(e)[:150])
    return track.notify(msg)


# ---------------------------------------------------------------- loop
def sh_cmd(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode
    except Exception as e:
        print("cmd failed:", cmd[:2], e)
        return 1


def commit():
    sh_cmd("git", "config", "user.name", "github-actions[bot]")
    sh_cmd("git", "config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
    sh_cmd("git", "add", STATE)
    if sh_cmd("git", "diff", "--cached", "--quiet") != 0:
        sh_cmd("git", "commit", "-m", "clan snapshot")
        if sh_cmd("git", "pull", "--rebase", "-X", "theirs") != 0:
            sh_cmd("git", "rebase", "--abort")
        sh_cmd("git", "push")


def check(preview=False):
    st = load()
    now = datetime.now(timezone.utc)
    for attempt in range(3):
        try:
            clans, battle = fetch()
            break
        except Exception as e:
            print("fetch failed:", e)
            if attempt == 2:
                if not preview:
                    track.notify(f"⚠️ **Clan check FAILED** at {now.astimezone(TZ):%H:%M}: {e}. Nothing was invented.")
                return False
            time.sleep(20 * (attempt + 1))
    A = analyse(st, now, clans, battle)
    post(A, now)
    if preview:
        return True
    for i, c in clans.items():
        st["clans"][i] = {"name": c["name"], "tag": c["tag"], "icon": c["icon"]}
    st["snaps"].append({"t": now.isoformat(timespec="seconds"),
                        "c": {i: [c["rank"], c["points"]] for i, c in clans.items()}})
    cutoff = now - timedelta(days=KEEP_DAYS)
    st["snaps"] = [s for s in st["snaps"] if datetime.fromisoformat(s["t"]) >= cutoff]
    save(st)
    commit()
    return True


def main():
    if os.environ.get("PREVIEW", "").lower() == "true":
        return 0 if check(preview=True) else 1
    stop = datetime.fromisoformat(os.environ["STOP_AT"]) if os.environ.get("STOP_AT") else None
    started = time.time()
    st = load()
    last = datetime.fromisoformat(st["snaps"][-1]["t"]) if st["snaps"] else None
    done = last.astimezone(TZ).strftime("%Y%m%d%H") if last else None
    if last is None or datetime.now(timezone.utc) - last > timedelta(minutes=55):
        try:
            check()  # overdue (or the very first run): check now
            done = datetime.now(TZ).strftime("%Y%m%d%H")
        except Exception:
            traceback.print_exc()
    while time.time() - started < RUN_MIN * 60:
        now = datetime.now(TZ)
        if stop and now >= stop:
            track.notify("🏁 **Clan tracker finished**: the Clan War is over.")
            path = os.environ.get("GITHUB_OUTPUT")
            if path:
                with open(path, "a") as f:
                    f.write("done=true\n")
            return 0
        key = now.strftime("%Y%m%d%H")
        if now.minute < 5 and key != done:
            try:
                check()
            except Exception:
                traceback.print_exc()
            done = key
        time.sleep(20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
