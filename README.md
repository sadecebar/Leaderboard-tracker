<p align="center">
  <img src="assets/banner.svg" alt="Leaderboard Tracker - hourly race reports for Pets Universe leaderboards" width="100%">
</p>

<p align="center">
  <b>Every hour, on the hour, a race report for a <a href="https://petsuniverse.org/players">Pets Universe leaderboard</a> lands in Discord:<br>who's farming, who's sleeping, and whether anyone can still catch you before the event ends.</b>
</p>

<p align="center">
  <img alt="runs on GitHub Actions" src="https://img.shields.io/badge/runs%20on-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-headless%20Chromium-2EAD33?logo=playwright&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/history-SQLite-003B57?logo=sqlite&logoColor=white">
  <img alt="hourly" src="https://img.shields.io/badge/report-every%20hour-3ddc97">
</p>

---

## 🆕 What's new

| Date | Update |
|---|---|
| **26 Nov 2026** | 🖼️ The hourly report is now **two pictures**: the board (top 5 + you, with Roblox avatars and speed bars) and a 12-hour analysis |
| **26 Nov 2026** | 🔟 Tracks the **top 15** every hour: overtakes coming, who enters or drops out of the top 10, and the projected final top 10 |
| **26 Nov 2026** | 📏 Every judgement uses each player's **12-hour average speed**, never a single hour, and never guesses when there's no history |

## 🏁 What it does

Leaderboard events are a marathon. Checking the board every hour from your phone gets old fast, so this does it for you.

- ⏰ Posts **one report every hour at :00** to a Discord channel, as two pictures
- 🥇 The **top 5** with their Roblox avatars, plus **you** if you're further down; the **top 15** are tracked behind the scenes
- 📈 Each player's **12h average speed** as a bar, with mood tags: 🔥 on fire · 🚀 surging · 🧊 cooling · 😴 no gain
- 🔜 **Overtakes coming**: who passes whom, and how soon
- 🔮 **Endgame maths**: the projected final top 10, where you finish, and ✅/❌ checks for catching #1, passing the player above and holding off the one below
- 🧾 Big numbers stay readable: `1.29e+26` becomes **`129.04Sp`** (K, M, B, T, Qa, Qi, Sx, Sp, Oc, No, Dc…)
- 🛑 Stops by itself when the event ends, with a final "tracker finished" message

## 📸 What a report looks like

Every hour, one Discord message with two pictures *(example data, made-up players)*:

<table>
<tr>
<td align="center" valign="top" width="50%"><b>1 · The board</b><br><br><img src="assets/example-board.png" alt="Example board picture: top 5 and you" width="100%"></td>
<td align="center" valign="top" width="50%"><b>2 · The analysis</b><br><br><img src="assets/example-analysis.png" alt="Example analysis picture: overtakes, final top 10, last 12h, endgame" width="100%"></td>
</tr>
</table>

- **The board:** rank badges, avatars, places moved in 12h (▲▼), the gap to #1 and a speed bar per player. Your own row is highlighted.
- **The analysis:** 🔜 overtakes coming · 🔮 the final top 10 if everyone keeps their pace · 📰 what changed in the last 12 hours · 🏁 your endgame.

If a picture ever can't be drawn, that hour's report is sent as text instead, so you never miss one.

## ⚙️ How it works

```mermaid
flowchart LR
    A["⏱️ GitHub Actions<br/>runner.py loop"] -->|"top of every hour"| B["🌐 Headless Chromium<br/>opens /players"]
    B --> C["🖱️ Clicks the board tab<br/>scrolls until you're loaded"]
    C --> D["💾 Snapshot into SQLite<br/>data/*.db"]
    D --> E["🧮 Speeds, trends,<br/>endgame projections"]
    E --> F["🖼️ Board + analysis pictures<br/>sent to Discord"]
    F --> A
```

- **Honest by design**: if the site doesn't load, it retries twice (3 min apart). It only reports a failure after the 3rd try, and it never makes up numbers.
- A **wrong-board guard** makes sure it really read the board you asked for, not the default one.
- The job runs ~6 hours and then **starts its own next run**, so it keeps going for the whole event.

## 🔁 Track a different leaderboard

Everything board-specific lives in **one block** at the top of `.github/workflows/track.yml`:

| Setting | Example | Meaning |
|---|---|---|
| `TRACK_BOARD` | `Total Flowers` | the exact tab name on the players page |
| `TRACK_TITLE` | `Flower` | shown as "Flower leaderboard" |
| `TRACK_EMOJI` | `🌸` | emoji in the report title |
| `TRACK_MIN_EXP` | `20` | sanity check: the #1 value must be at least 10^20 |
| `TRACK_DB` | `data/tracker.db` | history file. **Use a new file for each board** |
| `TRACK_USER` | your Roblox name | the player to follow |
| `STOP_AT` | `2026-11-27T19:00:00+01:00` | when the event ends (sends a final message, then stops) |

## 🚀 Run your own

1. **Fork** this repo (keep it **public** for unlimited free Actions minutes).
2. Create a Discord webhook (*Channel settings → Integrations → Webhooks*).
3. Add the secret `DISCORD_WEBHOOK_URL` under *Settings → Secrets and variables → Actions*.
4. Edit the settings block above, then *Actions → tracker-runner → Run workflow*.
   Tick **preview** to get a report immediately.

## 📁 What's inside

| File | Job |
|---|---|
| `track.py` | reads the board, stores snapshots, works out speeds and the endgame, builds the report |
| `lbcard.py` | draws the two report pictures (board + analysis) |
| `runner.py` | the long-running loop: hourly checks, retries, saving state, starting the next run |
| `.github/workflows/track.yml` | the workflow + **the leaderboard settings** |
| `data/*.db` | the leaderboard history (SQLite) |

---

<p align="center"><sub>Fan-made side project. Not affiliated with Pets Universe or Roblox. May contain traces of flowers. 🌸</sub></p>
