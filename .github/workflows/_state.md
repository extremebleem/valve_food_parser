# State persistence

Two interchangeable backends, chosen automatically:

| `DATABASE_URL` secret | Backend | Notes |
| --- | --- | --- |
| set | Postgres | durable, queryable from outside CI, no size cliff |
| unset | SQLite in a GitHub Actions artifact | zero setup, works immediately |

The artifact backend keeps `data/monitor.db.gz` in an artifact named
`monitor-state`, restored at the start of every run and re-uploaded at the end
with `overwrite: true` (so exactly one copy exists and the storage quota stays
flat).

**Both workflows write to the same state**: `monitor` writes observations,
baselines and alerts; `discovery` writes venues. They therefore share the
`concurrency: state` group so they can never interleave and clobber each other.

Known trade-offs versus Postgres:

* `overwrite: true` deletes before it uploads. A run killed inside that window
  loses the state, which is why `discovery` also uploads a dated
  `monitor-state-backup-<date>` once a day (14-day retention).
* Artifacts expire. `retention-days: 7` on the live artifact means a repository
  left idle for a week loses its history and every baseline restarts.
* Artifact storage counts against the plan quota (500 MB on Free for private
  repos). Keep `STORE_RAW_VALUE=false` if that gets tight.
