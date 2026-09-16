# Stage A — Research

Everything below was checked on **2026-09-16**. API terms and pricing move; re-check
before relying on a row.

---

## 1. Where is Valve's office?

| Question | Answer | How it was verified |
| --- | --- | --- |
| Company | Valve Corporation | — |
| Building | Lincoln Square South (Lincoln Square Expansion) office tower, floors 11–19 | Valve leased nine floors in 2016 and moved in 2017 |
| Address | **10400 NE 4th St, Bellevue, WA 98004, USA** | Bellevue Downtown Association business directory lists "Valve — 10400 NE 4th St, 14th Floor"; the building at 10400 NE 4th St is confirmed as Lincoln Square South, completed 2017 |
| Coordinates | **47.6142467, -122.2007170** | OpenStreetMap node `5270634805`, `office=it`, `name=Valve Corporation Headquarters`, geocoded through Nominatim |
| Timezone | `America/Los_Angeles` (PST/PDT) | — |

Valve's *previous* HQ was a different building on the same street; several
third-party "company profile" sites still show stale addresses, which is why the
anchor was taken from OSM plus the local business directory rather than from a
data-broker page. It is not hardcoded: `OFFICE_LAT` / `OFFICE_LON` /
`OFFICE_ADDRESS` / `OFFICE_TIMEZONE` override it with no code change.

The default 2 km radius around that point covers all of downtown Bellevue:
The Bellevue Collection, Bellevue Square, Lincoln Square (both towers, including
the food hall in Valve's own building), Old Bellevue, the NE 8th St corridor and
the Main Street restaurant strip.

---

## 2. Source evaluation

`Discovery` = can enumerate venues. `Live load` = current busyness for an
arbitrary third-party venue. `ETA` = delivery/pickup/wait time.

| Source | Discovery | Live load | ETA | Cost | Rate limits | Suitable |
| --- | --- | --- | --- | --- | --- | --- |
| **OpenStreetMap / Overpass** | ✅ full tags: name, cuisine, `delivery`, `takeaway`, `opening_hours`, website, phone | ❌ | ❌ | free (ODbL, attribution) | shared community servers; fair-use, seconds-of-CPU budget per query | ✅ **primary discovery** |
| **Google Places API (New)** | ✅ `places:searchNearby`, authoritative identity + hours + `businessStatus` | ❌ **not exposed** | ❌ | ~$32/1000 Nearby Search (Pro SKU), monthly free credit | high, per-project quota | ⚠️ optional discovery enrichment |
| Google Maps *Popular Times / live busyness* | — | rendered in Maps & Search only | — | — | — | ❌ **no API exists**; scraping it violates the Maps/Google ToS and breaks on every UI change |
| **Foursquare Places API** (FSQ OS Places) | ✅ `places/search`, good category taxonomy | ❌ for arbitrary venues | ❌ | 500 free Pro calls, then metered; v3 endpoints retire 2026-05-15 | per-key | ⚠️ optional discovery enrichment |
| **BestTime.app** | ✅ `venues/search` / `venues/filter` | ✅ `POST /forecasts/live` → `venue_live_busyness` (0–100+, % of that venue's weekly peak), `venue_live_forecasted_delta` | ❌ | credit-based, from ~$5; live = 1 credit, new forecast = 2 credits | 300 req/min general; 30 req/min for search & filter | ✅ **primary load signal** |
| Yelp Fusion — Waitlist / wait times | ✅ business search | ❌ | partner-only | — | — | ❌ requires Yelp Partner status |
| OpenTable / Resy / Tock | ❌ | ❌ | reservation availability, partner-only | — | — | ❌ no public API; their web endpoints are not licensed for this |
| DoorDash Drive / Marketplace | ❌ | ❌ | `POST /drive/v2/quotes` returns a delivery quote — **for deliveries you are creating as the merchant**, not for observing someone else's restaurant | merchant agreement | — | ❌ wrong shape of access |
| Uber Eats Marketplace API | ❌ | ❌ | merchant/POS integration only (4–8 week onboarding) | partner | — | ❌ |
| Olo / Toast / Square POS | ❌ | ❌ | prep & pickup times, but only for **your own** restaurant | merchant | — | ❌ (supported via the generic provider if you *are* the merchant) |
| SafeGraph / Placer.ai / Unacast | ✅ | ✅ foot traffic | ❌ | enterprise contracts | — | ❌ out of scope for a hobby deployment |

### The key negative finding

> **Google does not publish Popular Times or live busyness through any API.**
> The Places resource has no such field, and the data is only rendered in the
> Maps and Search UIs. Every "popular times API" on the market is either a
> scraper (against Google's terms, and structurally fragile) or an independent
> panel-based dataset like BestTime's.

This project therefore does **not** scrape Google. It uses BestTime's licensed
panel data as the live signal, and is explicit that this is a **proxy**.

---

## 3. What the chosen signal actually measures

`venue_live_busyness` is derived from anonymised mobile-device signals and is
expressed as a percentage of *that venue's own weekly peak*. Consequences:

* it is **not** a headcount, and not a share of seating capacity;
* it is comparable **against the same venue over time**, not across venues —
  which is exactly what per-venue baselining needs;
* it is unavailable for some venues (`venue_live_busyness_available: false`),
  in which case this system records nothing rather than substituting a forecast.

Every observation is therefore tagged with a `CongestionDomain`:

| Domain | Meaning | Available signals today |
| --- | --- | --- |
| `physical_occupancy` | how full the room is | BestTime live index (**proxy**), queue length or wait time from a self-hosted source |
| `delivery_congestion` | courier + kitchen backlog | only via a delivery-partner API you hold credentials for |
| `pickup_congestion` | counter/kitchen backlog | only via a POS API you hold credentials for |
| `reservation_congestion` | how far out the first free table is | no public API |

The project is named a **Restaurant Demand Monitor** for this reason. It does not
claim to count people.

---

## 4. Consequences for the design

1. **Discovery and load are different problems** — OSM is excellent at the first
   and useless for the second, so they are separate provider interfaces.
2. **Only per-venue baselines are meaningful.** A "% of weekly peak" index is
   self-referential, so a fixed global threshold would be meaningless. Hence
   median + MAD per (venue, metric, weekday, time-of-day).
3. **The system must survive having no paid key.** Discovery, storage, baselines,
   normalisation, alert gating and the CLI all work without one; only the live
   signal requires BestTime, and its absence is reported as a configuration error
   rather than silently faked.
4. **Bounded API spend.** Discovery is daily, not every 30 minutes. Closed venues
   are never polled. `MAX_VENUES_PER_RUN` and `BESTTIME_MAX_NEW_FORECASTS_PER_RUN`
   cap the worst case.
