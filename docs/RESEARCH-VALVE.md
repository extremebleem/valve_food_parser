# Stage A(2) — Early-warning signals for a Valve update

Checked live on 2026-09-17. Everything below is free, keyless and used through
an official endpoint — no scraping, no third-party mirror.

## The question

Not "did Valve ship?" (that is trivially visible in the news feed after the
fact) but **"is something coming?"** — enough warning to be ready.

## What was tested

| Signal | Endpoint | Key | Lead time | Verdict |
| --- | --- | --- | --- | --- |
| **Beta / preview channel posts** | `ISteamNews/GetNewsForApp` appid `1675200`, `753` | none | **days to weeks** | ✅ best lead time |
| **Required game version** | `ISteamApps/UpToDateCheck` | none | minutes to hours before the blog post | ✅ precise, instant |
| **GitHub tags / releases** | `api.github.com/repos/ValveSoftware/*` | none (1000/h with `GITHUB_TOKEN` in CI) | days | ✅ |
| **GitHub commit bursts** | same | none | days | ✅ rate signal |
| Depot buildid per branch | `api.steamcmd.net` | none | hours to days | ❌ mirror truncates every response at 16 256 of 35 865 bytes, on every retry and every combination of `--compressed` / `--http1.1`. Unusable. |
| Depot buildid per branch | steamcmd / SteamKit anonymous PICS | none | hours to days | ⏳ the correct route (Valve's own protocol, anonymous login is supported), but it needs a dependency that could not be installed and therefore not verified here. Deliberately left out of v1. |
| SteamDB | — | — | — | ❌ scraping prohibited by its terms |

### Live readings at the time of writing

```
UpToDateCheck 730 -> required_version 14181  ("Server version required: 1.41.8.1")
UpToDateCheck 570 -> required_version 37
UpToDateCheck 440 -> required_version 10828683

appid 1675200 news:
  2026-09-14  SteamOS 3.9.1 Preview
  2026-09-14  SteamOS 3.8.27 Beta
  2026-09-11  Steam Beta Client Update: September 10th

ValveSoftware on GitHub: 55 public repos, 8 pushed in the last 14 days,
60 commits over those 14 days (48 of them in gamescope alone).
```

## Round 2 — CS2 specifically (2026-09-17)

CS2 exposes more public state than any other Valve title.

| Signal | Endpoint | Key | Result |
| --- | --- | --- | --- |
| **Relay network config** | `ISteamApps/GetSDRConfig?appid=730` | none | ✅ `revision` is a unix timestamp of the last change (read `1787769460` = 2026-08-26 18:37 UTC); `pops` lists **48** relay datacenters including Valve's own `sea` and `eat` |
| **Matchmaking / scheduler / load** | `ICSGOServers_730/GetGameServersStatus` | **free key** | ✅ richest CS2 signal: app version, scheduler state, online/searching players, average search time, per-datacenter load. 403 without a key; keys are instant at steamcommunity.com/dev/apikey |
| **Game coordinator rollout** | `IGCVersion_<appid>/GetServerVersion` | none | ✅ for Dota 2 (6933), Deadlock (6689), TF2. `deploy_version != active_version` means a rollout is in flight **right now**. ❌ for CS2 — `IGCVersion_730` answers `deploy_version: 0, active_version: 0` |
| **Concurrent players** | `ISteamUserStats/GetNumberOfCurrentPlayers` | none | ✅ but only as a *sharp move vs the previous reading*; the daily cycle makes baseline comparison useless |
| CS2 dedicated server as its own app | `UpToDateCheck?appid=740` | none | ❌ "Couldn't get app info" |
| Steam Linux Runtime (sniper, soldier, scout) | `UpToDateCheck` on 1628350 / 1391110 / 1070560 | none | ❌ same — runtimes are not apps with a server version |
| CS2 blog RSS | `blog.counter-strike.net/index.php/feed/` | none | ❌ **dead**: valid RSS, but the newest post is from 2023-04-25. Valve moved to the Steam news feed at the CS2 launch |
| Depot patch info | `IContentServerDirectoryService/GetDepotPatchInfo` | none | ❌ returns `{"response":{}}` for depots 731/732/741 without manifest ids |
| `IGCVersion_730/GetClientVersion` | — | — | ❌ 404, the method does not exist for 730 |

`ISteamWebAPIUtil/GetSupportedAPIList` lists **27** interfaces reachable without
a key; the ones above are everything in it relevant to this project.

## Round 3 — SteamPipe and Steamworks (2026-09-17)

The premise being tested: when Valve rolls an update out, the plumbing feels
it before anyone posts about it.

| Signal | Endpoint | Result |
| --- | --- | --- |
| **Content delivery load** | `GetServersForSteamPipe?cell_id=0` | ✅ partner CDNs (fastly, alibaba, edgenext) report `load: 0`, but Valve's own `cache1-sto2`…`cache6-sto2` reported **77-80**. A real utilisation number, and everyone downloading at once is what a rollout looks like |
| **Content delivery host set** | same | ✅ 10 hosts; adding or dropping a CDN partner is infrastructure news |
| **Delivery domains** | `ISteamDirectory/GetSteamPipeDomains` | ✅ 28 domains |
| **Client update hosts** | `GetClientUpdateHosts` | ✅ ~1 KB KV blob, 5 hosts, hashable |
| Depot manifests via SteamPipe | `GetDepotPatchInfo`, direct CDN paths | ❌ **conclusively not available**: the endpoint returns `{"response":{}}` even when given `source_manifestid` and `target_manifestid`, and CDN paths need a manifest id that only PICS `app_info` provides. SteamPipe answers "where is content served from", never "which build is there" |
| Steamworks docs | `partner.steamgames.com/doc/*` | ⚠️ readable without login (~120 KB HTML), but diffing SPA markup for new sections is noise with no defensible payoff |
| Steamworks SDK downloads | `partner.steamgames.com/downloads/list` | ❌ page renders without login but carries **no version strings**; the real list is behind partner auth |
| Whole app catalogue | `ISteamApps/GetAppList` | ❌ 404 on v1/v2/v0002. `IStoreService/GetAppList` needs the key, and with no publisher filter, attributing new appids to Valve would mean an `appdetails` call per appid against hundreds registered daily |
| **Per-game DLC list** | `store/api/appdetails?appids=730` | ✅ the workable version of the above: CS2 lists `dlc: [2678630]`, one request, and a new entry means a new Valve product registered |
| CS2 achievements | `GetGlobalAchievementPercentagesForApp` | ❌ returns exactly **1** achievement; "new achievement = new content" does not work |
| `ValveSoftware/steam-runtime` releases | GitHub | ❌ zero releases — it uses dated tags (`v0.20260818.0`) instead |

## Round 4 — depot build ids, finally reachable (2026-09-17)

Written off three times as unreachable. It is not: **`steamcmd +login anonymous
+app_info_print 730`** works, needs no account, and returns the whole
`depots > branches` block.

```
public       buildid=25218825   timeupdated 2026-09-09 22:49 UTC
csgo_legacy  buildid=12426195
1.41.7.4     buildid=24537688   2026-08-03 21:18 UTC
… 13 branches in total
```

A run takes about 6 seconds against an already-bootstrapped steamcmd. This is
the earliest public signal that exists: the build id changes when Valve pushes
content, before any announcement and before anyone dumps the files. The *set*
of branch names is watched too, because a new pinned version branch tends to
appear ahead of the public push.

### What this replaced

`SteamTracking/GameTracking-*` was considered and dropped. Its commit message
looks like a build id but `update.sh` line 73 reads it out of the downloaded
game files:

```sh
CreateCommit "$(grep "ClientVersion=" game/csgo/steam.inf | grep -o '[0-9\.]*')"
```

So it can only appear *after* the depot is public and the files have been
pulled — a confirmation, not a warning. The same timing objection applies to
`Protobufs`, `SteamTracking` and `SteamworksDocumentation`: all are dumped from
shipped builds. Useful for datamining what is coming next, useless for knowing
that something is coming.

`steamstat.us` is not used at all: its maintainers state the data endpoint is
for that site only.

### Still out of reach

The CS2 dedicated server is appid **2347773** (from `steam.inf`, not 740 as
guessed earlier). It answers nothing on `UpToDateCheck`, `IGCVersion`,
`GetSDRConfig` or `GetNewsForApp` — a depot-only app, reachable through PICS
alone.

## Correction — the content-delivery host set is not a signal (2026-09-17)

Shipped as a change signal, then withdrawn after it produced a false alert on
its first day. Three measurements settled it:

* `GetServersForSteamPipe` **ignores `cell_id`** and answers by the caller's
  address — `fra1`/`sto2` from Europe, `atl`/`iad` from a US GitHub runner.
* Two identical back-to-back calls from one address return **different sets**
  (`alibaba` in one, `steampipe` in the next).
* Everything else from the same family is stable across repeated calls:
  `GetSteamPipeDomains`, `GetClientUpdateHosts` and `GetSDRConfig` all return the
  same digest three times running.

So the host set is gone. The **load** figure survives, because it is steady
(31-32 across five calls ten seconds apart) — but it describes whichever
regional caches answered, so it is stored per datacentre
(`steampipe_load_fra1`, `steampipe_load_iad`, …) and a delta only ever compares
two readings from the same place. A run on a European runner followed by one on
a US runner would otherwise read as a collapse.

The same round replaced digests with sorted lists for every set-valued key, so
a notification can say *which* datacentre or domain appeared rather than only
that the count moved.

## Consequence for the design

Two different mechanisms, not one:

1. **Change detection on discrete state** — a watched value (required version,
   latest tag, latest news id) changes -> alert immediately. This is what
   answers "something is happening". No baseline, no statistics, no warm-up.
2. **Rate anomalies** — commits/day, news items/week. These are continuous and
   feed the existing median/MAD/robust-z engine unchanged.

The old restaurant pipeline polled every 30 minutes because footfall changes
that fast. Valve activity does not: roughly one public event per day. Discrete
watchers are cheap enough to keep a short poll interval (they are free), while
rate baselines move to daily buckets.

## What this signal is and is not

It observes **Valve directly** rather than a proxy, which is a large improvement
over restaurant footfall. But the public GitHub org covers Proton, SteamOS and
Linux tooling; CS2 and Dota development happens in private repositories. A beta
channel post or a version bump is evidence that something **shipped or is about
to ship**, not a measurement of how hard anyone is working.
