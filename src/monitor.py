"""The monitoring run.

    active venues -> open now? -> load signal -> normalise -> store observation
        -> historical baseline -> deviation -> anomaly? -> cooldown -> Telegram

Failure containment is the rule everywhere: a venue that errors is counted and
skipped, a provider that errors is demoted to the next one, and the process only
exits non-zero for a *systemic* problem (no database, no providers at all, every
venue failing).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import opening_hours as oh
from .anomaly import AnomalyDetector, compute_baseline, district_index
from .config import Settings
from .logging_utils import get_logger, github_summary, set_output
from .models import (
    AnomalyResult,
    Baseline,
    BaselineStatus,
    Direction,
    LoadSignal,
    Observation,
    RunStats,
    Venue,
    utcnow,
)
from .normalization import NormalizationError, Normalizer
from .providers.base import LoadProvider, ProviderError
from .providers.registry import build_load_providers
from .storage import BaseStorage
from .telegram import Notifier, mark_state_from_observation

log = get_logger(__name__)


class MonitorError(RuntimeError):
    """Systemic failure -- the workflow should go red."""


def office_timezone(settings: Settings):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.office.timezone)
    except Exception as exc:  # pragma: no cover - missing tzdata
        log.error(
            "cannot load timezone, falling back to UTC",
            extra={"timezone": settings.office.timezone, "error": str(exc)},
        )
        from datetime import timezone

        return timezone.utc


def local_slot(moment: datetime, tzinfo) -> Tuple[int, int]:
    """(weekday, minutes-since-local-midnight) for baseline bucketing.

    Derived from the venue's local wall clock, so a baseline always compares
    "Wednesday around 12:40 local" with the same slot in previous weeks --
    correct across DST transitions, because UTC offsets are resolved per
    timestamp rather than assumed.
    """
    local = moment.astimezone(tzinfo)
    return local.weekday(), local.hour * 60 + local.minute


class Monitor:
    def __init__(
        self,
        settings: Settings,
        storage: BaseStorage,
        providers: Optional[Sequence[LoadProvider]] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.providers: List[LoadProvider] = (
            list(providers) if providers is not None else build_load_providers(settings)
        )
        self.normalizer = Normalizer(settings.normalization)
        self.detector = AnomalyDetector(settings.anomaly)
        self.notifier = notifier or Notifier(settings, storage)
        self.tz = office_timezone(settings)

    # -- open/closed gate -------------------------------------------------- #

    def is_open(self, venue: Venue, moment: datetime) -> Optional[bool]:
        if venue.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}:
            return False
        return oh.is_open(venue.opening_hours, moment.astimezone(self.tz))

    def is_active_now(self, moment: datetime) -> bool:
        """Is the office's local clock inside ACTIVE_HOURS_START..ACTIVE_HOURS_END?"""
        local = moment.astimezone(self.tz)
        return self.settings.active_window.contains(local.weekday(), local.hour)

    def select_venues(self, moment: datetime) -> Tuple[List[Venue], Dict[str, int]]:
        """Pick the venues worth spending API quota on this run."""
        venues = self.storage.list_venues(active_only=True)
        counters = {"total": len(venues), "closed": 0, "unknown_hours": 0, "open": 0}

        selected: List[Venue] = []
        for venue in venues:
            state = self.is_open(venue, moment)
            if state is None:
                counters["unknown_hours"] += 1
                if not self.settings.assume_open_when_unknown:
                    continue
            elif state is False:
                counters["closed"] += 1
                if self.settings.skip_closed_venues:
                    continue
            selected.append(venue)

        counters["open"] = len(selected)
        limit = self.settings.max_venues_per_run
        if limit and len(selected) > limit:
            log.warning(
                "venue budget reached, monitoring the nearest venues only",
                extra={"selected": len(selected), "limit": limit},
            )
            selected = selected[:limit]
        return selected, counters

    # -- signal collection ------------------------------------------------- #

    def collect_signal(self, venue: Venue) -> Tuple[Optional[LoadSignal], Optional[str]]:
        """Try providers in priority order. Returns (signal, error)."""
        last_error: Optional[str] = None
        for provider in self.providers:
            if not provider.supports(venue):
                continue
            try:
                signal = provider.get_current_load(venue)
            except ProviderError as exc:
                last_error = "{}: {}".format(provider.name, exc)
                log.warning(
                    "provider error, trying next",
                    extra={"provider": provider.name, "venue": venue.name, "error": str(exc)[:200]},
                )
                continue
            except Exception as exc:  # a provider bug must not abort the run
                last_error = "{}: unexpected {}".format(provider.name, exc)
                log.exception(
                    "provider crashed", extra={"provider": provider.name, "venue": venue.name}
                )
                continue
            if signal is not None:
                return signal, None
        return None, last_error

    def to_observation(self, venue: Venue, signal: LoadSignal, moment: datetime) -> Optional[Observation]:
        try:
            normalized = self.normalizer.normalize(signal)
        except NormalizationError as exc:
            log.warning(
                "cannot normalise signal",
                extra={"venue": venue.name, "source": signal.source, "error": str(exc)},
            )
            return None
        timestamp = signal.observed_at or moment
        weekday, minutes = local_slot(timestamp, self.tz)
        return Observation(
            venue_id=venue.id,
            timestamp=timestamp,
            source=signal.source,
            metric_type=normalized.metric_type,
            metric_value=normalized.metric_value,
            raw_value=signal.raw_value if self.settings.store_raw_value else None,
            load_score=normalized.load_score,
            domain=normalized.domain.value,
            confidence=normalized.confidence,
            signal_quality=normalized.signal_quality.value,
            local_weekday=weekday,
            local_minutes=minutes,
        )

    # -- main loop --------------------------------------------------------- #

    def run(self, *, concurrency: int = 6) -> RunStats:
        started_wall = time.monotonic()
        started = utcnow()
        stats = RunStats()

        # The window is checked first, deliberately: a tick outside the active
        # hours must exit cleanly whatever else is (mis)configured, so ~3 of the
        # 24 daily cron ticks can never turn the workflow red.
        if not self.is_active_now(started):
            local = started.astimezone(self.tz)
            stats.skipped_reason = "outside active window {} (local {})".format(
                self.settings.active_window.describe(), local.strftime("%a %H:%M %Z")
            )
            log.info("run skipped", extra={"reason": stats.skipped_reason})
            print("skipped: " + stats.skipped_reason)
            github_summary("### Restaurant Demand Monitor\n\nSkipped — " + stats.skipped_reason)
            set_output("skipped", "true")
            return stats

        if not self.providers:
            raise MonitorError(
                "no load providers are configured -- set BESTTIME_API_KEY_PRIVATE or "
                "declare one in config/providers.json"
            )

        venues, counters = self.select_venues(started)
        stats.venues_total = counters["total"]
        stats.venues_open = counters["open"]

        if stats.venues_total == 0:
            raise MonitorError("venue table is empty -- run scripts/discover.py first")

        for provider in self.providers:
            try:
                provider.prefetch(venues)
            except Exception as exc:  # optional hook
                log.warning("prefetch failed", extra={"provider": provider.name, "error": str(exc)})
            resolver = getattr(provider, "resolved_ids", None)
            if callable(resolver):
                for venue_id, provider_id in resolver().items():
                    self.storage.update_venue_source_ids(venue_id, {provider.name: provider_id})
                    for venue in venues:
                        if venue.id == venue_id:
                            venue.source_ids[provider.name] = provider_id

        observations: List[Observation] = []
        results: List[AnomalyResult] = []
        failures: Dict[str, int] = {}

        def work(venue: Venue) -> Tuple[Venue, Optional[Observation], Optional[str]]:
            signal, error = self.collect_signal(venue)
            if signal is None:
                return venue, None, error
            return venue, self.to_observation(venue, signal, started), None

        workers = max(1, min(int(concurrency), 16))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for venue, observation, error in pool.map(work, venues):
                if error:
                    stats.venues_failed += 1
                    key = error.split(":", 1)[0]
                    failures[key] = failures.get(key, 0) + 1
                    continue
                if observation is None:
                    continue  # provider simply has no data for this venue
                stats.venues_checked += 1
                observations.append(observation)

        stats.provider_errors = failures
        stats.observations_written = self.storage.insert_observations(observations)

        by_id = {venue.id: venue for venue in venues}

        # Pass 1: baselines only. The district index needs every venue's ratio
        # before any single venue can be judged against it.
        baselines: Dict[str, Baseline] = {}
        ratios: List[float] = []
        for observation in observations:
            baseline = self.baseline_for(observation)
            baselines[observation.venue_id] = baseline
            if baseline.usable and baseline.median > 0:
                ratios.append(observation.load_score / baseline.median)

        district = (
            district_index(ratios, self.settings.anomaly.district_min_venues)
            if self.settings.anomaly.use_district_index
            else None
        )
        stats.district_index = district if district is not None else 1.0
        log.info(
            "district index",
            extra={
                "index": stats.district_index,
                "venues_with_baseline": len(ratios),
                "min_required": self.settings.anomaly.district_min_venues,
                "applied": district is not None,
            },
        )

        # Pass 2: judge each venue against its own baseline and the district.
        for observation in observations:
            venue = by_id.get(observation.venue_id)
            if venue is None:  # pragma: no cover - defensive
                continue
            result = self.evaluate(venue, observation, baselines[observation.venue_id], district)
            results.append(result)
            if result.status is not BaselineStatus.OK:
                stats.venues_learning += 1
            if result.is_anomaly:
                if result.direction is Direction.LOW:
                    stats.anomalies_low += 1
                else:
                    stats.anomalies += 1
            else:
                mark_state_from_observation(self.storage, result)

        outcome = self.notifier.process(results)
        stats.alerts_sent = int(outcome["alerts_sent"])
        stats.alerts_suppressed = int(outcome["alerts_suppressed"])
        stats.recoveries = int(outcome["recoveries_sent"])
        stats.duration_seconds = round(time.monotonic() - started_wall, 2)

        self._report(stats, counters, outcome, results, started)
        return stats

    def baseline_for(self, observation: Observation) -> Baseline:
        """Fetch comparable history for this slot, summarise it and persist it."""
        samples = self.storage.fetch_baseline_samples(
            venue_id=observation.venue_id,
            metric_type=observation.metric_type,
            weekday=observation.local_weekday,
            minutes=observation.local_minutes,
            window_minutes=self.settings.anomaly.window_minutes,
            lookback_weeks=self.settings.anomaly.lookback_weeks,
            exclude_after=observation.timestamp,
        )
        baseline = compute_baseline(
            observation.venue_id,
            observation.metric_type,
            observation.local_weekday,
            observation.local_minutes,
            samples,
            self.settings.anomaly,
        )
        self.storage.upsert_baseline(baseline)
        return baseline

    def evaluate(
        self,
        venue: Venue,
        observation: Observation,
        baseline: Optional[Baseline] = None,
        district: Optional[float] = None,
    ) -> AnomalyResult:
        if baseline is None:
            baseline = self.baseline_for(observation)
        return self.detector.evaluate(venue, observation, baseline, district)

    # -- observability ----------------------------------------------------- #

    def _report(
        self,
        stats: RunStats,
        counters: Dict[str, int],
        outcome: Dict[str, Any],
        results: Sequence[AnomalyResult],
        started: datetime,
    ) -> None:
        provider_stats = {p.name: p.stats() for p in self.providers if p.stats()}
        log.info(
            "run summary",
            extra={
                "venues_total": stats.venues_total,
                "venues_open": stats.venues_open,
                "venues_checked": stats.venues_checked,
                "venues_failed": stats.venues_failed,
                "venues_learning": stats.venues_learning,
                "anomalies": stats.anomalies,
                "anomalies_low": stats.anomalies_low,
                "district_index": stats.district_index,
                "alerts_sent": stats.alerts_sent,
                "alerts_suppressed": stats.alerts_suppressed,
                "recoveries": stats.recoveries,
                "closed_now": counters["closed"],
                "unknown_hours": counters["unknown_hours"],
                "duration_s": stats.duration_seconds,
                "providers": provider_stats,
            },
        )
        print(stats.as_logline())

        self.storage.record_run(
            "monitor",
            started,
            {
                "stats": stats.as_logline(),
                "counters": counters,
                "providers": provider_stats,
                "suppressed": outcome.get("suppressed_reasons", []),
                "dry_run": self.settings.dry_run,
            },
        )

        set_output("anomalies", str(stats.anomalies))
        set_output("alerts_sent", str(stats.alerts_sent))

        top = sorted(results, key=lambda r: -abs(r.relative_percent))[:10]
        lines = [
            "### Restaurant Demand Monitor",
            "",
            "`{}`".format(stats.as_logline()),
            "",
            "District index: **{:.2f}** (1.00 = the whole radius is behaving normally)".format(
                stats.district_index
            ),
            "",
            "| Venue | Now | Baseline | Deviation | vs district | Samples | |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for result in top:
            marker = ""
            if result.direction is Direction.HIGH:
                marker = "🔥"
            elif result.direction is Direction.LOW:
                marker = "📉"
            elif result.status is not BaselineStatus.OK:
                marker = "📚"
            lines.append(
                "| {} | {:.0f} | {} | {:+.0f}% | {:+.0f}% | {} | {} |".format(
                    result.venue.name.replace("|", "/"),
                    result.current_score,
                    "{:.0f}".format(result.baseline.median) if result.baseline else "—",
                    result.deviation_percent,
                    result.relative_percent,
                    result.baseline.sample_count if result.baseline else 0,
                    marker,
                )
            )
        if self.settings.dry_run:
            lines += ["", "> DRY_RUN was enabled: no Telegram message was delivered."]
        github_summary("\n".join(lines))
