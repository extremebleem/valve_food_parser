# Valve Restaurant Demand Monitor

Watches the food venues around **Valve's HQ in Bellevue, WA** and posts to
Telegram when one of them is unusually busy *for that venue, at that time, on
that day of the week*.

```
🔥 Повышенная загрузка рядом с Valve Corporation HQ
2 заведения выше обычного · проверка 12:40

1. Din Tai Fung — +70%
    📊 78/100 (обычно 46) · 📍 650 м · besttime
    📶 Live busyness: 78% от недельного пика

2. Lincoln Square South Food Hall — +69%
    📊 88/100 (обычно 52) · 📍 17 м · besttime
    📶 Live busyness: 88% от недельного пика
```

> ### What this is not
> This is a **demand / congestion monitor built on proxy metrics**. It does not
> and cannot count the people inside a restaurant. No public API exposes physical
> occupancy for third-party venues — see [docs/RESEARCH.md](docs/RESEARCH.md).
> Every observation is tagged with which kind of congestion it describes
> (`physical_occupancy` · `delivery_congestion` · `pickup_congestion` ·
> `reservation_congestion`) and with a confidence and signal-quality rating, and
> every Telegram message says so.

---

## Contents

1. [What it does](#1-what-it-does) · 2. [Architecture](#2-architecture) ·
3. [Where the venue list comes from](#3-where-the-venue-list-comes-from) ·
4. [Where the load numbers come from](#4-where-the-load-numbers-come-from) ·
5. [API limitations](#5-api-limitations) · 6. [Baseline](#6-how-the-baseline-is-computed) ·
7. [Anomaly](#7-how-an-anomaly-is-decided) · 8. [Telegram bot](#8-creating-the-telegram-bot) ·
9. [Chat id](#9-finding-your-telegram_chat_id) · 10. [GitHub Secrets](#10-github-secrets-and-variables) ·
11. [Running locally](#11-running-locally) · 12. [Testing](#12-testing) ·
13. [Enabling the workflows](#13-enabling-github-actions) · 14. [Cost](#14-api-usage-and-cost-estimate) ·
15. [Known limitations](#15-known-limitations)

---

## 1. What it does

Every 20 minutes, for every venue within `SEARCH_RADIUS_METERS` of the office
that is open right now:

1. read whatever current-load signal is available;
2. normalise it onto a `load_score` of 0–100;
3. store the observation;
4. build a baseline from the same venue's history for the same weekday and
   roughly the same local time over the last 4–8 weeks;
5. decide whether the current reading is anomalously high;
6. apply cooldown and de-duplication;
7. send one aggregated Telegram message.

Once a day it rebuilds the venue list from the discovery sources. Nothing is
maintained by hand.

| `load_score` | Band |
| --- | --- |
| 0–30 | `low` |
| 31–60 | `normal` |
| 61–80 | `busy` |
| 81–100 | `extremely_busy` |

### The office

| | |
| --- | --- |
| **Address** | 10400 NE 4th St, Bellevue, WA 98004, USA (Lincoln Square South, floors 11–19) |
| **Coordinates** | `47.6142467, -122.2007170` |
| **Timezone** | `America/Los_Angeles` |
| **Verified** | 2026-09-16, against OSM node `5270634805` + the Bellevue Downtown Association directory |

Not hardcoded — set `OFFICE_LAT` / `OFFICE_LON` / `OFFICE_ADDRESS` /
`OFFICE_TIMEZONE` / `SEARCH_RADIUS_METERS` to point the whole system at a
different office without touching the code.

---

## 2. Architecture

```
valve-food-monitor/
├── src/
│   ├── config.py           environment-driven settings
│   ├── models.py           Venue / Observation / Baseline / Alert
│   ├── geo.py              haversine, bounding boxes, formatting
│   ├── http.py             timeouts, retries, backoff, 429, rate limiting, cache
│   ├── logging_utils.py    structured JSON logging + GH step summary
│   ├── opening_hours.py    OSM opening_hours parser ("don't poll closed venues")
│   ├── storage.py          Postgres + SQLite behind one interface
│   ├── discovery.py        fan-out + cross-source de-duplication
│   ├── normalization.py    raw metric -> load_score 0..100
│   ├── anomaly.py          median / MAD / p90 baseline, anomaly gates
│   ├── monitor.py          the 20-minute run
│   ├── telegram.py         gating, message rendering, Bot API
│   └── providers/
│       ├── base.py             DiscoveryProvider / LoadProvider ABCs
│       ├── registry.py         wiring from configuration
│       ├── osm.py              Overpass discovery              (free)
│       ├── google_places.py    Places API (New) discovery       (optional)
│       ├── foursquare.py       Places API discovery             (optional)
│       ├── besttime.py         live foot traffic  <- load signal
│       └── generic_http.py     declarative JSON load provider
├── scripts/
│   ├── discover.py         rebuild the venue list
│   ├── monitor.py          one monitoring pass / --test-telegram / --chat-ids
│   ├── init_db.py          create the schema
│   └── selftest.py         end-to-end dry run on a synthetic signal
├── tests/                  170 test functions -> 279 cases
├── .github/workflows/      monitor.yml · discovery.yml · ci.yml
├── migrations/001_init.sql
├── config/providers.example.json
└── docs/RESEARCH.md · docs/ARCHITECTURE.md
```

Full rationale in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Storage

A single `DATABASE_URL` pointing at **Postgres (Supabase / Neon / any provider)**.
The runner is ephemeral and the monitor runs 72×/day, so committing a database
back into git would mean ~72 commits/day, races between overlapping runs and an
ever-growing binary blob in history. `sqlite://` is supported for local
development and the tests, through the same code path.

Tables: `venues`, `observations`, `baselines`, `alerts`, `alert_state`, `runs`.

---

## 3. Where the venue list comes from

| Source | Role | Key needed | Notes |
| --- | --- | --- | --- |
| **OpenStreetMap / Overpass** | primary | no | ODbL; carries `cuisine`, `delivery`, `takeaway`, `opening_hours`, website, phone |
| Google Places API (New) | optional enrichment | `GOOGLE_MAPS_API_KEY` | authoritative hours and `businessStatus`; finds venues inside malls that OSM misses |
| Foursquare Places API | optional enrichment | `FOURSQUARE_API_KEY` | good category taxonomy |

A live run against the default office returned **251 raw candidates → 248 venues**
after de-duplication:

| Category | Count | | Category | Count |
| --- | --- | --- | --- | --- |
| asian | 66 | | dessert | 13 |
| restaurant | 53 | | bakery | 9 |
| fast_food | 27 | | burger | 6 |
| coffee | 26 | | pizzeria | 5 |
| cafe | 23 | | deli | 2 |
| bar_pub | 17 | | food_court | 1 |

This covers every bucket the brief asks for: restaurants, cafés, fast food,
bars/pubs with a kitchen, pizzerias, burger joints, Asian venues, bakeries and
coffee shops that serve food, take-away counters and delivery-capable venues.
Attribute completeness in this dataset: 101 venues with `opening_hours` (41%),
61 tagged `takeaway=yes|only`, 3 tagged `delivery=yes`, 118 with a website,
99 with a phone number. The nearest five are 14–23 m away — the food hall and
restaurants in Valve's own building.

De-duplication joins records across sources when a source id matches, or when the
accent-folded names are similar *and* the points are close; identical names more
than 150 m apart stay separate so chains are not collapsed.

Venues not re-seen for `VENUE_STALE_DAYS` are **deactivated, not deleted**, so
their history stays available for baselines.

---

## 4. Where the load numbers come from

| Provider | Metric | Domain | Quality | Key |
| --- | --- | --- | --- | --- |
| **BestTime.app** `POST /forecasts/live` | `live_busyness_index` (0–100+, % of that venue's weekly peak) | `physical_occupancy` *(proxy)* | high | `BESTTIME_API_KEY_PRIVATE` |
| BestTime forecast fallback | `forecast_busyness_index` | `physical_occupancy` | **low** | opt-in via `BESTTIME_ALLOW_FORECAST_AS_LOAD` |
| `GenericHttpProvider` | any of `pickup_eta_minutes`, `delivery_eta_minutes`, `prep_time_minutes`, `wait_time_minutes`, `queue_length`, `next_reservation_minutes`, `load_score_direct` | declared per provider | declared | whatever that endpoint needs |

`LOAD_PROVIDERS` sets the priority order. The first provider that returns a
signal wins; a provider that errors is skipped for that venue only.

### Normalisation

Each metric maps onto 0–100 through a configurable, monotonic envelope:

| Metric | 0 ⟵ | ⟶ 100 | Env vars |
| --- | --- | --- | --- |
| `live_busyness_index` | pass-through, clamped | | — |
| `delivery_eta_minutes` | 15 min | 75 min | `NORM_DELIVERY_ETA_LOW_MIN` / `_HIGH_MIN` |
| `pickup_eta_minutes` | 5 min | 45 min | `NORM_PICKUP_ETA_*` |
| `wait_time_minutes` | 0 min | 60 min | `NORM_WAIT_*` |
| `next_reservation_minutes` | 0 min | 180 min | `NORM_RESERVATION_*` |
| `queue_length` | 0 people | 25 people | `NORM_QUEUE_*` |
| `live_vs_forecast_delta` | −100 pp | +100 pp (centred on 50) | — |

Absolute calibration is deliberately coarse — alerts come from comparing a venue
against **its own** history, so the mapping only has to be monotonic and stable.
Every observation additionally carries `confidence` (0–1) and `signal_quality`
(`low`/`medium`/`high`), and different congestion domains are never averaged
together.

### Adding your own source

If you hold merchant-level credentials (your POS, a delivery partner, your own
queue counter), declare the endpoint in `config/providers.json` — no Python:

```json
{
  "providers": [{
    "name": "pos_pickup_eta",
    "metric_type": "pickup_eta_minutes",
    "domain": "pickup_congestion",
    "signal_quality": "high",
    "confidence": 0.9,
    "url_template": "https://pos.example.com/v1/stores/{source_id}/quote",
    "source_id_key": "pos",
    "require_source_id": true,
    "headers": { "Authorization": "Bearer ${POS_API_TOKEN}" },
    "value_path": "quote.pickup_eta_minutes"
  }]
}
```

Secrets are referenced as `${ENV_VAR}` and resolved at request time, so the file
never contains a credential. See `config/providers.example.json`.

---

## 5. API limitations

* **Google publishes no Popular Times / live busyness API.** The data exists only
  in the Maps and Search UIs. Scraping it violates Google's terms and breaks on
  every UI change, so this project does not do it. Google Places is used for
  discovery and opening hours only.
* **Yelp's Waitlist API is partner-only.** So are OpenTable, Resy and Tock.
* **DoorDash Drive and Uber Eats are merchant APIs.** Their quote/ETA endpoints
  describe deliveries *you* are creating, not third-party restaurant congestion.
* **BestTime's live signal is not available for every venue.** When
  `venue_live_busyness_available` is `false` the monitor records nothing rather
  than substituting a forecast.
* **Overpass is a shared community resource.** Discovery therefore runs once a
  day, sends a descriptive `User-Agent` and falls back across mirrors.
* **Scheduled GitHub Actions are best-effort.** Runs are routinely late and are
  sometimes skipped. Nothing assumes an exact cadence.

---

## 6. How the baseline is computed

For each observation, comparable history is selected as:

* the **same venue**,
* the **same `metric_type`** (a delivery ETA never contaminates a footfall baseline),
* the **same local weekday**,
* local time within **± `BASELINE_WINDOW_MINUTES`** (default 60, wrapping correctly across midnight),
* not older than **`BASELINE_LOOKBACK_WEEKS`** (default 8),
* excluding the current observation itself.

From those samples:

```
median  = median(load_score)          # the baseline
mad     = median(|x − median|)        # spread; 1.4826·MAD ≈ σ
p90     = 90th percentile
```

Median and MAD rather than mean and standard deviation, because the events we
want to detect are exactly the ones that would drag a mean-based baseline upward
and hide themselves.

If `sample_count < MIN_BASELINE_SAMPLES` (default 5) the baseline status is
**`learning_baseline`** and the venue **cannot alert**, regardless of how high the
reading is. A cautious absolute-score fallback exists but is off by default
(`ANOMALY_FALLBACK_ABSOLUTE_ENABLED`).

Timestamps are stored in UTC; bucketing uses the office's local wall clock via
`zoneinfo`, so DST transitions are handled correctly.

---

## 7. How an anomaly is decided

```
ratio    = current / baseline_median
delta    = current − baseline_median
robust_z = (current − baseline_median) / (1.4826 × MAD)
```

All of these must hold:

| Gate | Env var | Default |
| --- | --- | --- |
| `ratio ≥ multiplier` | `ANOMALY_MULTIPLIER` | 1.5 |
| `delta ≥ min absolute delta` | `ANOMALY_MIN_ABSOLUTE_DELTA` | 10 |
| `current ≥ score floor` | `ANOMALY_MIN_SCORE` | 55 |
| `robust_z ≥ threshold` (skipped when MAD = 0) | `ANOMALY_MIN_ROBUST_Z` | 3.0 |
| `confidence ≥ threshold` | `ANOMALY_MIN_CONFIDENCE` | 0.4 |
| baseline is not `learning_baseline` | `MIN_BASELINE_SAMPLES` | 5 |

The extra gates exist because the headline ratio alone is not enough: a jump from
4 to 10 is `+150%` and completely uninteresting, and a venue that is busy by its
own standards while still objectively quiet is not worth a notification.

### Active window

Monitoring only runs between `ACTIVE_HOURS_START` and `ACTIVE_HOURS_END` **in the
office's local time** (`OFFICE_TIMEZONE`), defaulting to **14:00–21:00**. Outside
it the run exits immediately having touched no API at all.

The window is evaluated against the local wall clock rather than UTC, so it does
not drift by an hour at each DST transition the way a UTC cron does. The cron in
`monitor.yml` (`*/20 0-4,21-23 * * *`) is only the coarse filter — it spans the
union of the window under both PDT and PST, and a cheap shell step skips the
Python setup entirely on the ~3 ticks/day that fall outside. **24 ticks fire per
day, 21 actually do work.**

`ACTIVE_HOURS_START == ACTIVE_HOURS_END` disables the gate (24/7). The window may
wrap midnight (`18`–`2`). `ACTIVE_WEEKDAYS` (0 = Monday) restricts it further;
unset means every day.

> The 14:00 default deliberately **excludes the lunch peak** (roughly
> 11:30–13:30), which for an office food monitor is usually the most interesting
> hour of the day. Set `ACTIVE_HOURS_START=11` to cover it — the cron already
> spans enough UTC hours that no other change is needed.

### Anti-spam

`alert_state` tracks whether each venue is currently in an alerting state, when
it last notified, its last score and its peak. A repeat is sent when:

* `ALERT_COOLDOWN_MINUTES` (default 120) has elapsed, **or**
* the score climbed by `ALERT_ESCALATION_DELTA` (default 15) — it got worse, **or**
* the venue recovered and became anomalous again (subject to an
  `ALERT_REARM_MINUTES` floor, default 30, so a venue flapping around the
  threshold cannot spam the chat).

`MAX_ALERTS_PER_RUN` caps the worst case. When several venues fire at once a
single aggregated message is sent.

### Recovery notifications

With `SEND_RECOVERY_ALERTS=true`, a venue that drops back to
`baseline × RECOVERY_RATIO` (default 1.15) produces:

```
✅ Загрузка нормализовалась

Din Tai Fung
Было: 91/100
Сейчас: 58/100
Обычно в это время: 50/100
```

---

## 8. Creating the Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AAH...`. That is
   **`TELEGRAM_BOT_TOKEN`** — treat it as a password.
4. To post into a **group**: add the bot to the group. If the group has topics or
   the bot must read messages, disable privacy mode via
   `/setprivacy` → your bot → *Disable*.
5. To post into a **channel**: add the bot as an administrator with
   *Post messages* permission.

---

## 9. Finding your `TELEGRAM_CHAT_ID`

```bash
export TELEGRAM_BOT_TOKEN=123456789:AAH...
python -m scripts.monitor --chat-ids
```

Send any message to the bot (or post in the group/channel it was added to) first,
then run the command. It prints every chat the bot can currently see:

```json
[{ "id": -1001234567890, "type": "supergroup", "title": "Lunch alerts" }]
```

Private chats have a positive id; groups and channels have a negative one
(supergroups start with `-100`). Verify end to end with:

```bash
python -m scripts.monitor --test-telegram --send
```

This calls `getChat` to prove the id resolves, then sends a real test message.

---

## 10. GitHub Secrets and Variables

### Secrets — *Settings → Secrets and variables → Actions → Secrets*

| Secret | Required | Used by | What it is |
| --- | --- | --- | --- |
| `DATABASE_URL` | **yes** | both workflows | `postgresql://user:password@host:5432/postgres?sslmode=require` |
| `TELEGRAM_BOT_TOKEN` | **yes** | monitor | from BotFather |
| `TELEGRAM_CHAT_ID` | **yes** | monitor | target chat |
| `BESTTIME_API_KEY_PRIVATE` | **yes** | monitor | live busyness; without it there is no load signal |
| `BESTTIME_API_KEY_PUBLIC` | no | monitor | read-only queries |
| `GOOGLE_MAPS_API_KEY` | no | discovery | enables Google Places discovery |
| `FOURSQUARE_API_KEY` | no | discovery | enables Foursquare discovery |

### Variables — *…→ Variables* (non-secret, editable from the UI without a commit)

`OFFICE_NAME` · `OFFICE_ADDRESS` · `OFFICE_LAT` · `OFFICE_LON` ·
`OFFICE_TIMEZONE` · `SEARCH_RADIUS_METERS` · `ANOMALY_MULTIPLIER` ·
`MIN_BASELINE_SAMPLES` · `BASELINE_LOOKBACK_WEEKS` · `BASELINE_WINDOW_MINUTES` ·
`ALERT_COOLDOWN_MINUTES` · `SEND_RECOVERY_ALERTS` · `MAX_VENUES_PER_RUN` ·
`ENABLE_GOOGLE_DISCOVERY` · `ENABLE_FOURSQUARE_DISCOVERY` · `VENUE_STALE_DAYS` ·
`OBSERVATION_RETENTION_DAYS` (default 70) · `ACTIVE_HOURS_START` (default 14) ·
`ACTIVE_HOURS_END` (default 21) · `ACTIVE_WEEKDAYS`

All are optional — every one has a default in [`src/config.py`](src/config.py).

> Never commit `.env` or `config/providers.json`. Both are in `.gitignore`, and
> the `secret-scan` CI job fails the build if either becomes tracked or if a
> credential is hardcoded.

---

## 11. Running locally

```bash
git clone <your-fork> valve-food-monitor && cd valve-food-monitor
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # edit it; DRY_RUN=true by default
python -m scripts.init_db     # create the schema

python -m scripts.discover              # build the venue list (no API key needed)
python -m scripts.monitor --dry-run     # one pass, messages printed to stdout
python -m scripts.init_db --stats       # row counts
```

Useful flags:

| Command | Effect |
| --- | --- |
| `python -m scripts.discover --radius 3000 --output venues.json` | wider search, write a snapshot |
| `python -m scripts.discover --no-store` | discovery without touching the database |
| `python -m scripts.monitor --limit 20` | only the 20 nearest open venues |
| `python -m scripts.monitor --test-telegram` | send a test alert |
| `python -m scripts.monitor --chat-ids` | list chat ids |
| `python -m scripts.monitor --purge-days 120 --purge-only` | housekeeping only |
| `LOG_FORMAT=text LOG_LEVEL=DEBUG python -m scripts.monitor` | human-readable logs |

### Deploying

1. **Database.** Create a free Supabase or Neon project, copy the connection
   string, and run `DATABASE_URL=... python -m scripts.init_db` once (the
   workflows also migrate on every run, so this is optional).
2. **Secrets.** Add the secrets from [§10](#10-github-secrets-and-variables).
3. **Seed the venue list.** *Actions → discovery → Run workflow*.
4. **Verify Telegram.** *Actions → monitor → Run workflow* with
   `dry_run = true`, and read the job summary.
5. **Go live.** Run `monitor` once with `dry_run = false`; the schedule takes
   over from there.
6. **Wait for baselines.** For the first ~1–2 weeks most venues will be
   `learning_baseline` and will deliberately not alert. This is the system
   working correctly, not a failure.

---

## 12. Testing

```bash
pytest                              # 170 test functions -> 279 cases
pytest --cov=src --cov-report=term-missing
ruff check src scripts tests
python -m scripts.selftest          # end-to-end dry run, synthetic signal
```

Coverage focuses on the decision surface: baseline statistics, every anomaly
gate, alert cooldown/de-duplication/re-arm, normalisation of every metric,
malformed and hostile API responses for every provider, the opening-hours parser,
the midnight-wrapping baseline query, HTTP retry/429/caching, and the full
monitoring run with fake providers.

`scripts/selftest.py` seeds a throwaway database with an explicitly synthetic
history and walks the entire pipeline, so the whole flow can be verified without
any paid key. It is **not** a data source — nothing in the production path
invents busyness numbers.

---

## 13. Enabling GitHub Actions

1. Push to GitHub and open the **Actions** tab. On a fork, press
   *"I understand my workflows, go ahead and enable them"*.
2. Add the secrets and variables from [§10](#10-github-secrets-and-variables).
3. Run **discovery** manually once to seed `venues`.
4. Run **monitor** manually with `dry_run = true` and check the job summary.
5. The schedules then run by themselves:

| Workflow | Schedule | Purpose |
| --- | --- | --- |
| `monitor.yml` | `*/20 0-4,21-23 * * *` + manual | the monitoring pass, gated to 14:00–21:00 office-local |
| `discovery.yml` | `17 11 * * *` + manual | rebuild the venue list, upload a snapshot artifact |
| `ci.yml` | push / PR | lint, tests, self-test, secret scan |

Both scheduled workflows use a `concurrency` group so a delayed run never
overlaps the next one. The monitor exits non-zero only on a **systemic** failure
(no database, no providers configured, empty venue table) — one unreachable
restaurant or one failing provider is logged, counted and skipped.

> GitHub disables scheduled workflows in repositories with no activity for
> 60 days. Push a commit, or re-enable them from the Actions tab.

Every run ends with a one-line summary and a Markdown table in the job summary:

```
venues_total=248 venues_open=57 venues_checked=54 venues_failed=3 \
venues_learning=6 observations=54 anomalies=4 recoveries=1 \
alerts_sent=1 alerts_suppressed=3 duration=41.2s
```

---

## 14. API usage and cost estimate

Measured against the real 248-venue dataset around the default office
(`scripts/monitor` was run with a no-op provider to count the open/closed gate):

| Local time | Venues open | Polled per run (default caps) |
| --- | --- | --- |
| 06:40 | 161 | 150 |
| 09:40 | 186 | 150 |
| 12:40 | 238 | 150 |
| 19:40 | 220 | 150 |
| 22:40 | 162 | 150 |

The active window (14:00–21:00 office-local) means **21 effective runs/day**,
not 72. At `MAX_VENUES_PER_RUN=150` that is **~3 150 BestTime credits/day
≈ 94 500/month** — still the dominant cost, but ~3.4× below an ungated schedule.

| Source | Calls/day (defaults) | Unit cost | Note |
| --- | --- | --- | --- |
| Overpass (discovery) | **1** | free | daily, not per run — the largest single saving in the design |
| BestTime live | **~3 150** | 1 credit each | **the dominant cost**; see the levers below |
| BestTime new forecasts | ≤ 10/run while onboarding, then ~0 | 2 credits | ~500 credits one-off for 248 venues |
| Google Places Nearby (optional) | ~40 tiles × 1 run | ~$32/1000 Pro | ~$38/month, minus Google's monthly free credit |
| Foursquare (optional) | ≤ 10 | 500 free Pro calls, then metered | ~$0 |
| Supabase / Neon free tier | — | — | **$0** |
| GitHub Actions (public repo) | ~72 × ~1 min | free for public repos | **$0** |

### Recommended starter configuration

```env
ACTIVE_HOURS_START=11              # covers the lunch peak as well as dinner
ACTIVE_HOURS_END=21
MAX_VENUES_PER_RUN=40
ASSUME_OPEN_WHEN_UNKNOWN=false     # 41% of OSM venues here carry opening_hours
STORE_RAW_VALUE=false              # halves database growth
```

That polls ~40 venues over 30 runs/day ≈ **1 200 credits/day**, covering both
lunch and dinner for the nearest 40 venues. No workflow edit is needed — the
cron already spans the UTC hours these windows map to.

| Lever | Measured effect |
| --- | --- |
| `SKIP_CLOSED_VENUES=true` (default) | at 06:40, 87 of 248 venues are skipped outright |
| `STORE_RAW_VALUE=false` | observation row 412 → **222 bytes** (−46% database growth) |
| `ASSUME_OPEN_WHEN_UNKNOWN=false` | 150 → **87** venues polled at lunch (the 151 venues with no `opening_hours` are skipped) |
| `MAX_VENUES_PER_RUN=40` | 150 → 40 venues per run |
| `ACTIVE_HOURS_START/END` (default 14–21) | 72 → **21** effective runs/day |
| `BESTTIME_MAX_NEW_FORECASTS_PER_RUN` | caps the one-off onboarding spend |

Discovery deliberately runs **daily, not every 20 minutes**.

### Database growth

Measured on this schema: **412 bytes per observation** with the provider's raw
JSON retained, **222 bytes** without it (Postgres runs 30–60% heavier again).
`monitor.yml` purges observations older than `OBSERVATION_RETENTION_DAYS`
(default **70** — `BASELINE_LOOKBACK_WEEKS` is 8 weeks = 56 days, plus slack;
retaining longer buys nothing because the baseline query never looks past the
horizon).

| Configuration | Rows/day | Size at 70-day retention |
| --- | --- | --- |
| defaults (150 venues × 21 runs) | 3 150 | ~91 MB |
| starter (40 venues × 30 runs) | 1 200 | ~35 MB |
| starter + `STORE_RAW_VALUE=false` | 1 200 | ~19 MB |

Supabase's free tier is 500 MB and pauses a project after 7 days without
requests — the 20-minute cadence keeps it awake, but the size limit is real.

### GitHub Actions minutes

Free minutes are unlimited on public repositories. On a **private** repo the
Free plan gives **2 000 minutes/month**. With the active window this monitor uses
21 full runs plus ~3 near-instant skips per day ≈ **770 minutes/month**, which
fits comfortably. Widening the window is the thing to watch: an ungated
`*/20 * * * *` schedule would need ~3 000 minutes and does **not** fit.

---

## 15. Known limitations

1. **Proxy metrics, not occupancy.** The live index is a panel-derived estimate
   expressed as a percentage of each venue's own weekly peak. It is not a
   headcount and is not comparable between venues. Delivery, pickup and
   reservation congestion are tracked as *separate domains* and are never mixed.
2. **The live signal needs a paid key.** Everything else — discovery, storage,
   baselines, normalisation, alert gating, the CLI — works without one, but with
   no `BESTTIME_API_KEY_PRIVATE` and no declared generic provider there is
   nothing to measure, and the monitor says so instead of pretending.
3. **Coverage is partial.** BestTime has no live data for some venues; those are
   simply not observed.
4. **Cold start.** Baselines need ~1–2 weeks before most venues leave
   `learning_baseline`. This is intentional.
5. **Rare slots stay unbaselined.** A venue open only on Sunday evenings
   accumulates samples slowly.
6. **Opening hours are incomplete.** Exactly **101 of 248** venues here (41%)
   carry `opening_hours` in OSM; the rest fall back to
   `ASSUME_OPEN_WHEN_UNKNOWN`, which trades API spend against coverage.
   Unparseable specs are treated as *unknown*, never as closed, so a venue is
   never silently dropped because of a syntax the parser does not support.
7. **Schedule drift.** GitHub's scheduler is best-effort; the ±60-minute baseline
   window absorbs it, but a heavily delayed run still samples a different moment.
   A tick delayed past `ACTIVE_HOURS_END` is skipped rather than run late.
8. **Nothing is learned outside the active window.** Baselines only ever exist
   for slots that were sampled, so widening the window later starts a fresh
   learning period for the newly covered hours.
9. **Correlation, not causation.** A spike may be a Valve all-hands, a conference
   at the Hyatt across the street, a concert, or a panel-data artifact. The system
   reports that a venue is unusually busy; it does not explain why.
10. **Timezone assumption.** All venues within the radius are assumed to share the
   office timezone. True at 2 km; revisit before using a very large radius.

### Possible improvements

* Weather and event-calendar covariates to explain (and suppress) expected spikes.
* Seasonal decomposition (STL) instead of a fixed weekday/time-of-day bucket.
* Cross-venue correlation: a whole-block spike is a district-level event, not a
  restaurant-level one, and deserves a different message.
* Reservation availability via an official partner API, if access is ever granted.
* A `/status` bot command that answers "where can I get lunch right now?".
* Backfill BestTime's weekly forecast curve to shorten the cold-start period.

---

## License

MIT — see [LICENSE](LICENSE).

Venue data from **© OpenStreetMap contributors** ([ODbL](https://www.openstreetmap.org/copyright)).
Live foot-traffic data, where configured, from BestTime.app under their terms.
