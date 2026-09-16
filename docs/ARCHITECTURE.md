# Stage B — Architecture

## Data flow

```
                    ┌──────────────── GitHub Actions ────────────────┐
                    │                                                │
  discovery.yml ───▶│  scripts.discover      (daily, 03:17 local)    │
    (cron: daily)   │    ├─ OverpassProvider        (OSM, free)      │
                    │    ├─ GooglePlacesProvider    (optional)       │
                    │    ├─ FoursquareProvider      (optional)       │
                    │    └─ VenueMerger  ── dedupe ──▶ venues        │
                    │                                                │
  monitor.yml  ────▶│  scripts.monitor       (every 20 minutes)      │
   (cron: */20)     │    1. storage.list_venues(active)              │
                    │    2. opening_hours gate  ── skip closed ──▶   │
                    │    3. LoadProvider chain (besttime → generic)  │
                    │    4. Normalizer  raw metric ──▶ load_score    │
                    │    5. storage.insert_observations              │
                    │    6. storage.fetch_baseline_samples           │
                    │       (same venue/metric/weekday, ±60 min,     │
                    │        last 8 weeks)                           │
                    │    7. compute_baseline  median / MAD / p90     │
                    │    8. AnomalyDetector   ratio + z + floors     │
                    │    9. AlertGate         cooldown + dedupe      │
                    │   10. TelegramClient    aggregated message     │
                    └────────────────────┬───────────────────────────┘
                                         │
                            Postgres (Supabase / Neon)
                     venues · observations · baselines · alerts
                            alert_state · runs
```

## Why these choices

### Discovery: OpenStreetMap / Overpass as primary

Free, no key, licensed for programmatic use, and it carries precisely the
attributes the venue record needs (`cuisine`, `delivery`, `takeaway`,
`opening_hours`, `website`, `phone`). Downtown Bellevue is densely mapped: a
single 2 km query returns **251 raw candidates → 248 after de-duplication**,
covering restaurants, cafés, fast food, bars/pubs, pizzerias, burger joints,
Asian venues, bakeries, delis, coffee shops, dessert shops and the food court in
Valve's own building.

Google Places and Foursquare are wired in as *optional enrichment*: they add
venues OSM misses (especially inside malls) and contribute authoritative opening
hours and `businessStatus`. They are off unless their key is present, because
both charge per request and neither is required for the system to work.

De-duplication is union-find over a coarse spatial grid, joining records when a
source id matches, or when names are similar (accent-folded, noise-word-stripped,
`difflib` ratio) *and* the points are close. Identical names more than 150 m
apart stay separate so that two branches of a chain are not collapsed into one.

### Load: BestTime.app live foot traffic

The only source found that gives a *live* busyness reading for arbitrary
third-party venues through a documented, paid, ToS-compliant API. Google does not
expose Popular Times through any API and scraping it is both prohibited and
fragile — see [RESEARCH.md](RESEARCH.md).

`LoadProvider` is an abstraction, not a wrapper: providers are tried in the order
given by `LOAD_PROVIDERS`, the first one returning a signal wins, and a provider
that raises is demoted for that venue only. `GenericHttpProvider` lets an
operator who holds merchant-level credentials (POS prep time, delivery partner
quote, a self-hosted queue counter) declare that endpoint in JSON without writing
Python.

### Storage: external Postgres, not a file in git

The runner is ephemeral and the monitor runs 72×/day. Committing a database back
to the repository would mean ~72 commits/day, races between overlapping runs, an
unreadable history and an ever-growing binary blob. Instead a single
`DATABASE_URL` secret points at a free-tier Supabase/Neon Postgres:

* real indexes for the hot path — `(venue_id, metric_type, local_weekday, local_minutes, ts DESC)`;
* history survives the runner and is queryable from outside CI;
* one secret, no service-account files.

`SQLiteStorage` implements the same interface for local development and the test
suite, so nothing in the pipeline knows which backend it is talking to.

### Baseline and anomaly

A busyness index expressed as "% of this venue's own weekly peak" is
self-referential — a single global threshold would be meaningless. The baseline
is therefore per **(venue, metric_type, weekday, time-of-day)**:

```
samples  = load_score WHERE venue = v AND metric = m
                        AND local_weekday = today
                        AND local_minutes ∈ [now ± BASELINE_WINDOW_MINUTES]
                        AND ts ≥ now − BASELINE_LOOKBACK_WEEKS
baseline = median(samples)
spread   = MAD(samples)          # 1.4826·MAD ≈ σ for normal data
```

Median and MAD rather than mean and σ: the events we are trying to *detect* are
exactly the ones that would poison a mean-based baseline.

An anomaly requires **all** of:

| Gate | Default | Why |
| --- | --- | --- |
| `current ≥ baseline × ANOMALY_MULTIPLIER` | 1.5 | the headline rule |
| `current − baseline ≥ ANOMALY_MIN_ABSOLUTE_DELTA` | 10 | "+150%" from 4 to 10 is noise |
| `current ≥ ANOMALY_MIN_SCORE` | 55 | busy-for-itself but objectively quiet is not interesting |
| robust `z ≥ ANOMALY_MIN_ROBUST_Z` (skipped when MAD = 0) | 3.0 | scales the threshold to each venue's own volatility |
| `confidence ≥ ANOMALY_MIN_CONFIDENCE` | 0.4 | keeps low-quality signals out of the chat |
| `sample_count ≥ MIN_BASELINE_SAMPLES` | 5 | below this the venue is `learning_baseline` and **never** alerts |

### Time handling

Timestamps are stored in UTC. Bucketing uses the office's local wall clock via
`zoneinfo`, so "Wednesday ~12:40 local" always compares against the same local
slot in previous weeks, and DST transitions are resolved per timestamp rather
than assumed. The ±60-minute window also absorbs GitHub's irregular scheduler.

### Anti-spam

`alert_state` holds one row per venue: is it currently alerting, when was the
last notification, what was the score, what was the peak. A repeat is allowed
when the cooldown expired **or** the score climbed by `ALERT_ESCALATION_DELTA`.
A venue that recovered re-arms immediately, subject to an `ALERT_REARM_MINUTES`
floor that stops a venue oscillating around the threshold from spamming the chat.
`alerts` is the append-only log of everything that was sent.
