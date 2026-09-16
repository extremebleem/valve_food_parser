# Valve Watch

Watches Valve's **public state** and messages you on Telegram when something
moves — a new SteamOS preview build, a bumped game server version, a fresh
Proton release, an unusual burst of commits. The point is lead time: know an
update is coming before it lands.

```
⚡ У Valve что-то происходит
2 сигнала · 12:40

🧪 Новый пре-релизный билд
SteamOS / Steam Deck (beta & preview)
SteamOS 3.9.2 Preview
2026-09-17 09:00 UTC · Community Announcements
⏱ типичная фора: дни–недели
🔗 открыть

🧩 Версия сервера изменилась
Counter-Strike 2
Server version required: 1.41.9.0
14181 → 14205
⏱ типичная фора: минуты–часы
🔗 открыть
```

> ### What this is not
> It observes what Valve has made public. A preview build or a version bump is
> evidence that **something shipped or is about to**; it says nothing about what
> that something is. There is no insider signal here, and the notifications say
> so rather than implying otherwise.

**Everything it uses is free, keyless and official.** No scraping, no API keys,
no paid tier. The only credentials are your Telegram bot token and chat id.

---

## Contents

1. [What it watches](#1-what-it-watches) · 2. [How it decides](#2-how-it-decides) ·
3. [Architecture](#3-architecture) · 4. [Sources and their limits](#4-sources-and-their-limits) ·
5. [Telegram bot](#5-creating-the-telegram-bot) · 6. [Chat id](#6-finding-your-telegram_chat_id) ·
7. [GitHub Secrets](#7-github-secrets-and-variables) · 8. [Running locally](#8-running-locally) ·
9. [Deploying](#9-deploying) · 10. [Testing](#10-testing) ·
11. [Cost](#11-cost) · 12. [Known limitations](#12-known-limitations)

---

## 1. What it watches

### Counter-Strike 2 (the focus)

| Signal | Endpoint | Key | Lead time |
| --- | --- | --- | --- |
| **Matchmaking scheduler / services** | `ICSGOServers_730/GetGameServersStatus` | free | minutes — Valve touches matchmaking before an update lands |
| **App version** | same | free | minutes to hours |
| **Relay network config** (`revision`, 48 datacenters) | `ISteamApps/GetSDRConfig` | none | hours to days — infrastructure work precedes what it is for |
| **Server version** | `ISteamApps/UpToDateCheck` | none | minutes to hours, ahead of the blog post |
| **Player-count collapse** | `GetNumberOfCurrentPlayers` | none | minutes — this is what a server restart looks like |
| Official announcements | `ISteamNews/GetNewsForApp` | none | at announcement |

### Everything else

| Subject | Signal | Typical lead time |
| --- | --- | --- |
| **SteamOS / Steam Deck feed** (appid 1675200) | new preview / beta / client-beta post | **days to weeks** |
| Dota 2, Deadlock, TF2 | `deploy_version` ≠ `active_version` — a rollout **in flight right now** | minutes |
| Dota 2, Deadlock, TF2 | game coordinator + server version | minutes to hours |
| Dota 2 / Deadlock news feeds | newest official announcement | at announcement |
| `ValveSoftware/Proton`, `gamescope`, `SteamOS`, … | newest release | at release |
| the same repositories | commits in the last 24 h | days — a burst precedes a release |

The list lives in [`src/subjects.py`](src/subjects.py) and is deliberately short.
Valve has 55 public repositories and hundreds of app ids; watching all of them
would bury the signal.

---

## 2. How it decides

Two mechanisms, for two different questions.

### Change detection — "is something coming?"

A watched value changes → alert. No baseline, no warm-up, no statistics: a
version going from `14181` to `14205` is an event, not a deviation from a
median. This is what produces the useful notifications.

The **first** time any key is read, its value is recorded silently. Otherwise
the very first run would fire one alert per watched key.

A change is announced once. The `alerts` table keeps a fingerprint of
`(subject, key, new value)`, so a re-read of the same value stays quiet.

### Quiet routine, audible changes

Leave the chat **unmuted**. Routine runs are delivered with
`disable_notification`, so they land in the history without a sound; a real
change is sent normally and rings. The chat therefore doubles as a dashboard,
and silence never leaves you wondering whether the thing is still running.

| | Routine run | Something changed |
| --- | --- | --- |
| Notification sound | suppressed | **on** |
| Content | current CS2 state, counters | what changed, old → new, lead time |

No message carries a personal identifier. That is enforced by a test, because
in dry-run the whole message is printed to stdout and on a public repository
that log is readable by anyone.

`TELEGRAM_HEARTBEAT=false` turns the routine message off.
`TELEGRAM_HEARTBEAT_MIN_INTERVAL_MINUTES` throttles it — 0 means every run
(**144/day at the `*/10` cadence**), 360 means one status message every six
hours, which is what this deployment uses.

### Sharp moves — "did something just happen?"

Player counts and server counts move constantly and have a strong daily cycle,
so a baseline comparison would report every night as a collapse. These are
instead compared against the **previous reading**: a move of more than
`DELTA_ALERT_FRACTION` (default 15%) between two readings half an hour apart is
flagged. That is precisely what a server restart looks like.

### Rate anomalies — "is Valve unusually busy?"

Commit counts are continuous, so they go through robust statistics: one value
per local day, median as the baseline, MAD for the spread.

```
baseline = median(daily commit counts over BASELINE_LOOKBACK_WEEKS)
alert when current >= baseline × RATE_MULTIPLIER
             and current - baseline >= RATE_MIN_ABSOLUTE_DELTA
             and sample_count >= MIN_BASELINE_SAMPLES
```

Median and MAD rather than mean and σ: one merge day carrying fifty commits
would drag a mean-based baseline up and hide the next real burst. The absolute
floor exists because 1 → 3 commits is `+200%` and means nothing.

---

## 3. Architecture

```
valve-watch/
├── src/
│   ├── config.py           environment-driven settings
│   ├── subjects.py         what we watch + the watch list
│   ├── models.py           Baseline / AlertRecord / time helpers
│   ├── http.py             timeouts, retries, backoff, 429, rate limiting
│   ├── logging_utils.py    structured JSON logging + GH step summary
│   ├── storage.py          Postgres + SQLite behind one interface
│   ├── anomaly.py          median / MAD / p90 baselines for rate signals
│   ├── watcher.py          the run: read → compare → event
│   ├── telegram.py         Bot API transport, splitting, dry-run
│   ├── watch_telegram.py   what the notifications say
│   └── providers/
│       ├── base.py             WatchProvider ABC + errors
│       ├── steam.py            UpToDateCheck + GetNewsForApp
│       ├── cs2.py              SDR config, game coordinator, CS2 server status
│       └── github.py           releases + commit rate
├── scripts/
│   ├── watch.py            one pass / --show / --dry-run
│   └── init_db.py          create the schema
├── tests/                  158 tests
├── .github/workflows/      watch.yml · ci.yml
├── migrations/001_init.sql
└── docs/RESEARCH-VALVE.md  what was tested and what was rejected
```

### Storage

State has to outlive the runner. Two backends behind one interface:

| `DATABASE_URL` | Backend | Setup |
| --- | --- | --- |
| **unset** (default) | SQLite inside a GitHub Actions **artifact** | none |
| set | Postgres (Supabase / Neon / any) | one signup |

With no `DATABASE_URL` the workflow restores `data/watch.db.gz` from the
artifact named `monitor-state` at the start of every run and re-uploads it with
`overwrite: true`, so exactly one copy exists and the footprint stays flat.

Tables: `subjects`, `watch_state`, `watch_events`, `subject_observations`,
`alerts`, `runs`.

---

## 4. Sources and their limits

Full evaluation, including what was rejected and why, in
[docs/RESEARCH-VALVE.md](docs/RESEARCH-VALVE.md).

* **`ISteamNews/GetNewsForApp`** — free, keyless. A feed mixes Valve's own
  announcements with syndicated articles from gaming sites, so the provider
  filters on `feedname` and keeps only official posts. Appid 753 is **not**
  watched: its feed returns nothing but PCGamesN articles.
* **`ISteamApps/UpToDateCheck`** — free, keyless. Returns nothing for apps
  without a dedicated server (Deadlock, for instance), which is not an error.
* **GitHub REST API** — 60 requests/hour unauthenticated, 1000/hour with the
  token Actions provides for free. `/repos/*/tags` is deliberately **not**
  watched: it has no documented ordering and returned a stale tag for Proton,
  which would produce false alerts.
* **Depot build ids** would be the strongest early signal — Valve pushes builds
  to private branches before release. The public mirror `api.steamcmd.net`
  truncates every response at 16 256 of 35 865 bytes and is unusable. The
  correct route is `steamcmd` or SteamKit over anonymous PICS; it is **not
  implemented**, because it could not be verified in this environment and
  shipping it untested would be worse than leaving it out.
* **SteamDB** is not used: its terms prohibit scraping.

---

## 5. Creating the Telegram bot

1. Open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AAH...` — that is
   **`TELEGRAM_BOT_TOKEN`**. Treat it as a password.
4. To post into a **group**, add the bot to it. To post into a **channel**, add
   it as an administrator with *Post messages*.

---

## 6. Finding your `TELEGRAM_CHAT_ID`

Send any message to the bot (or post in the group it was added to), then:

```bash
export TELEGRAM_BOT_TOKEN=123456789:AAH...
python - <<'PY'
from src.config import load_settings
from src.telegram import TelegramClient
import json, dataclasses
s = dataclasses.replace(load_settings(None), dry_run=False)
print(json.dumps(TelegramClient(s).recent_chat_ids(), ensure_ascii=False, indent=2))
PY
```

Private chats have a positive id; groups and channels have a negative one.

---

## 7. GitHub Secrets and Variables

### Secrets — *Settings → Secrets and variables → Actions → Secrets*

| Secret | Required | What it is |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | **yes** | from BotFather |
| `TELEGRAM_CHAT_ID` | **yes** | target chat |
| `STEAM_WEB_API_KEY` | no, but **recommended** | free and instant from [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey). Unlocks the CS2 matchmaking/scheduler signal — the earliest CS2 warning available. Without it that provider stays dormant and everything else still works. |
| `DATABASE_URL` | no | leave unset to keep state in a workflow artifact |

### Variables worth setting

| Variable | Why |
| --- | --- |
| `WATCH_INTERVAL` · `WATCH_MAX_RUNTIME` | seconds between passes inside a job, and how long a job runs before chaining (default 300 / 3000) |
| `TELEGRAM_HEARTBEAT_MIN_INTERVAL_MINUTES` | throttle the routine status message. At the `*/10` cadence leave this at **360** or the chat gets one every ten minutes |
| `TIMEZONE` | used to bucket daily rate history; defaults to `America/Los_Angeles` |
| `RATE_MULTIPLIER` · `MIN_BASELINE_SAMPLES` · `BASELINE_LOOKBACK_WEEKS` | commit-burst sensitivity |
| `OBSERVATION_RETENTION_DAYS` | how much numeric history to keep |

A variable the code does not read is silently ignored, which is the worst kind
of misconfiguration — nothing fails, the setting just does nothing. A CI test
cross-checks every variable the workflow sets against every variable the code
reads, so that cannot happen again.

Everything else is keyless, and the GitHub API token is provided by Actions
automatically.

### Variables — *…→ Variables* (optional)

`TIMEZONE` · `RATE_MULTIPLIER` · `MIN_BASELINE_SAMPLES` ·
`BASELINE_LOOKBACK_WEEKS` · `OBSERVATION_RETENTION_DAYS`

All have defaults in [`src/config.py`](src/config.py).

> Never commit `.env`. It is in `.gitignore`, and the `secret-scan` CI job fails
> the build if it becomes tracked or a credential is hardcoded.

---

## 8. Running locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # DRY_RUN=true by default

python -m scripts.init_db     # create the schema
python -m scripts.watch       # first run: records state, stays silent
python -m scripts.watch       # second run: reports anything that changed
python -m scripts.watch --show   # print the current stored state
```

| Command | Effect |
| --- | --- |
| `python -m scripts.watch --dry-run` | never touch Telegram |
| `python -m scripts.watch --send` | force real sending |
| `python -m scripts.watch --show` | dump the stored state and exit |
| `LOG_FORMAT=text LOG_LEVEL=DEBUG python -m scripts.watch` | human-readable logs |

---

## 9. Deploying

1. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets.
   `STEAM_WEB_API_KEY` is optional but unlocks the CS2 matchmaking signal.
2. **Actions** tab → enable workflows if prompted.
3. **watch → Run workflow** with `dry_run = true`. The first run records a
   baseline and reports `first_seen=N, changes=0` — that is correct, not a
   failure.
4. Run it once more with `dry_run = false`.

### Why the cadence does not use `schedule`

GitHub's scheduler never fired for this repository. Over five hours with three
different cron expressions, `event=schedule` stayed at `total_count=0` while
manual dispatches succeeded every time — and the workflow was `active`, on the
default branch, in a public, non-fork, non-archived repo. Every documented
cause was ruled out.

So each job **loops internally** for `WATCH_MAX_RUNTIME` seconds (default 3000,
i.e. 50 minutes), running a pass every `WATCH_INTERVAL` seconds (default 300),
and then **dispatches the next job itself**. The `schedule` trigger is kept as a
second entry point in case it ever starts working; the `state` concurrency group
makes an overlap harmless.

Self-dispatch requires a **PAT**, because GitHub deliberately refuses to start a
run from an event raised with the built-in `GITHUB_TOKEN`:

| Secret | Scope |
| --- | --- |
| `WORKFLOW_CHAIN_TOKEN` | fine-grained PAT, this repository, **Actions: read and write** |

Without it the job logs a warning and the chain simply stops, which is also how
you turn the chain off. The hand-off runs even when a pass failed — otherwise
one bad minute would end the chain permanently — but never sooner than five
minutes after the job started, so a job that dies instantly cannot spin.

> This keeps a free public runner occupied close to continuously (roughly 29
> jobs a day at the defaults). Minutes are free on public repositories, but it
> is worth being deliberate about it: raise `WATCH_INTERVAL` or lower
> `WATCH_MAX_RUNTIME` if that is more than the signal is worth to you.

Every run ends with a one-line summary and a table in the job summary:

```
subjects_total=14 subjects_read=14 subjects_failed=0 values=15 \
changes=0 first_seen=9 rate_anomalies=0 alerts_sent=0 duration=7.0s
```

---

## 10. Testing

```bash
pytest                       # 158 tests
pytest --cov=src --cov-report=term-missing
ruff check src scripts tests
```

Coverage targets the decision surface: first-run silence, change detection,
de-duplication, rate baselines, the official-post filter, malformed and hostile
API payloads for every provider, HTTP retry/429/caching, message splitting and
escaping.

---

## 11. Cost

**Zero.** Every data source is free and keyless. On a public repository Actions
minutes are free too; on a private one this uses roughly 48 runs/day × ~40 s
≈ 550 minutes/month against the Free plan's 2 000.

---

## 12. Known limitations

1. **It watches shipping, not working.** Valve's public GitHub covers Proton,
   SteamOS and Linux tooling; CS2 and Dota development happens in private
   repositories. A signal here means something became public, not that anyone is
   busy right now.
2. **No depot visibility.** The strongest early signal — a build pushed to a
   private branch — is not implemented; see [§4](#4-sources-and-their-limits).
   `GetDepotPatchInfo` was tried and returns an empty object without manifest
   ids, and the CS2 dedicated server is not addressable as its own app id.
3. **CS2 has no game-coordinator signal.** `IGCVersion_730` answers with zeros,
   so the "rollout in flight" signal exists for Dota 2, Deadlock and TF2 but not
   for CS2. The matchmaking scheduler covers the same ground, and needs the free
   Steam key.
4. **Lead times are editorial.** The "typical lead" shown in a notification is a
   documented judgement about each signal type, not a measurement of that
   specific event.
5. **Rate baselines need about a week** before commit bursts can be flagged.
   Change detection works from the second run.
6. **Artifact state expires.** On the default backend history lives in an
   artifact with `retention-days: 30`; a repository idle for a month loses its
   watch state, and the next run silently re-records a baseline. Set
   `DATABASE_URL` if that matters.
7. **A renamed or deleted subject** stops producing values silently. The run
   summary shows `subjects_failed`, which is where that surfaces.

### Possible improvements

* Depot build ids via `steamcmd` / SteamKit anonymous PICS — the missing
  strongest signal.
* Watch Steam client beta branch names, not only announcement posts.
* Per-subject mute, and a `/status` bot command.

---

## License

MIT — see [LICENSE](LICENSE).
