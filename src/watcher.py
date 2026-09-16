"""The watch run.

    subjects -> providers read current public state
             -> compare against what we stored last time
             -> discrete change  => event, notify immediately
             -> numeric rate     => history, baseline, anomaly if unusual

Two mechanisms on purpose. "Is something coming?" is answered by a state change
(a new preview build, a bumped server version, a fresh release), which needs no
warm-up and no statistics. "Is Valve unusually busy?" is a rate question, and
that is where the median/MAD machinery earns its keep.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .anomaly import compute_baseline
from .config import Settings
from .logging_utils import get_logger, github_summary, set_output
from .models import utcnow
from .providers.base import ProviderError, WatchProvider
from .storage import BaseStorage
from .subjects import Subject, WatchEvent, WatchValue, default_subjects

log = get_logger(__name__)

#: keys whose change is an event in itself
CHANGE_KEYS = frozenset(
    {
        "required_version",
        "latest_news",
        "latest_prerelease",
        "latest_release",
        "sdr_revision",
        "sdr_pops",
        "gc_active_version",
        "gc_deploy_in_flight",
        "cs2_app_version",
        "cs2_scheduler",
        "cs2_services",
    }
)

#: Keys that move constantly, where only a *sharp* move matters. Comparing them
#: against a daily baseline would be wrong: player counts have a strong daily
#: cycle, so every night would read as a collapse. A large relative jump
#: against the previous reading has no such problem, and is exactly what a
#: server restart looks like.
DELTA_KEYS = frozenset({"players_current", "cs2_online_players", "cs2_online_servers"})


class WatchStats:
    def __init__(self) -> None:
        self.subjects_total = 0
        self.subjects_read = 0
        self.subjects_failed = 0
        self.values_read = 0
        self.changes = 0
        self.changes_first_seen = 0
        self.rate_anomalies = 0
        self.sharp_moves = 0
        self.alerts_sent = 0
        self.duration_seconds = 0.0

    def as_logline(self) -> str:
        return (
            "subjects_total={} subjects_read={} subjects_failed={} values={} "
            "changes={} first_seen={} rate_anomalies={} sharp_moves={} "
            "alerts_sent={} duration={:.1f}s".format(
                self.subjects_total,
                self.subjects_read,
                self.subjects_failed,
                self.values_read,
                self.changes,
                self.changes_first_seen,
                self.rate_anomalies,
                self.sharp_moves,
                self.alerts_sent,
                self.duration_seconds,
            )
        )

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class Watcher:
    def __init__(
        self,
        settings: Settings,
        storage: BaseStorage,
        providers: Sequence[WatchProvider],
        notifier: Any = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.providers = list(providers)
        self.notifier = notifier

    # -- helpers ----------------------------------------------------------- #

    def _tz(self):
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.settings.timezone)
        except Exception:  # pragma: no cover
            from datetime import timezone

            return timezone.utc

    def seed_subjects(self) -> Dict[str, int]:
        return self.storage.upsert_subjects(default_subjects())

    # -- reading ----------------------------------------------------------- #

    def read_subject(self, subject: Subject) -> Tuple[List[WatchValue], Optional[str]]:
        values: List[WatchValue] = []
        error: Optional[str] = None
        for provider in self.providers:
            if not provider.supports(subject):
                continue
            try:
                values.extend(provider.read(subject))
            except ProviderError as exc:
                error = "{}: {}".format(provider.name, exc)
                log.warning(
                    "watch provider failed",
                    extra={"provider": provider.name, "subject": subject.name, "error": str(exc)[:200]},
                )
            except Exception as exc:  # a provider bug must not abort the run
                error = "{}: unexpected {}".format(provider.name, exc)
                log.exception(
                    "watch provider crashed",
                    extra={"provider": provider.name, "subject": subject.name},
                )
        return values, error

    # -- change detection -------------------------------------------------- #

    def detect_change(self, subject: Subject, value: WatchValue) -> Optional[WatchEvent]:
        previous = self.storage.get_watch_value(subject.id, value.key)
        self.storage.set_watch_value(value)

        if previous is None:
            # First time we have ever seen this key. Not news -- it is simply
            # the start of the record, and alerting here would fire once per
            # watched key on the very first run.
            log.info(
                "watch baseline established",
                extra={"subject": subject.name, "key": value.key, "value": value.value[:60]},
            )
            return WatchEvent(
                subject=subject,
                key=value.key,
                old_value="",
                new_value=value.value,
                label=value.label,
                detail=value.detail,
                url=value.url,
                detected_at=value.observed_at or utcnow(),
                first_ever=True,
            )

        if str(previous["value"]) == str(value.value):
            return None

        return WatchEvent(
            subject=subject,
            key=value.key,
            old_value=str(previous["value"]),
            new_value=str(value.value),
            label=value.label,
            detail=value.detail,
            url=value.url,
            detected_at=value.observed_at or utcnow(),
            first_ever=False,
        )

    # -- sharp moves on constantly-changing counters ----------------------- #

    def check_delta(self, subject: Subject, value: WatchValue) -> Optional[Dict[str, Any]]:
        """Flag a large relative move against the *previous reading*.

        Not against a daily baseline: player counts swing by a factor of two
        over a day, so a baseline comparison would report every night as a
        collapse. A 15% move between two readings half an hour apart is a
        different thing entirely -- it is what a server restart looks like.
        """
        cfg = self.settings.anomaly
        try:
            current = float(value.value)
        except (TypeError, ValueError):
            return None

        previous = self.storage.get_watch_value(subject.id, value.key)
        self.storage.set_watch_value(value)
        if previous is None:
            return None
        try:
            before = float(previous["value"])
        except (TypeError, ValueError):
            return None
        if before < cfg.delta_min_absolute:
            return None

        change = (current - before) / before
        if abs(change) < cfg.delta_alert_fraction:
            return None
        return {
            "subject": subject,
            "key": value.key,
            "current": current,
            "previous": before,
            "change": change,
            "label": value.label,
            "url": value.url,
        }

    # -- rate anomalies ---------------------------------------------------- #

    def check_rate(self, subject: Subject, value: WatchValue, moment: datetime) -> Optional[Dict[str, Any]]:
        try:
            current = float(value.value)
        except (TypeError, ValueError):
            return None

        local_day = moment.astimezone(self._tz()).strftime("%Y-%m-%d")
        self.storage.record_subject_value(subject.id, value.key, current, moment, local_day)

        history = self.storage.daily_series(
            subject.id,
            value.key,
            lookback_days=max(14, self.settings.anomaly.lookback_weeks * 7),
            exclude_day=local_day,
        )
        baseline = compute_baseline(subject.id, value.key, 0, 0, history, self.settings.anomaly)
        if not baseline.usable:
            return None
        if current < self.settings.anomaly.multiplier * max(baseline.median, 1.0):
            return None
        if current - baseline.median < 3:  # a jump from 1 to 2 commits is noise
            return None
        return {
            "subject": subject,
            "key": value.key,
            "current": current,
            "median": baseline.median,
            "samples": baseline.sample_count,
            "label": value.label,
            "url": value.url,
        }

    # -- main -------------------------------------------------------------- #

    def run(self) -> WatchStats:
        started_wall = time.monotonic()
        started = utcnow()
        stats = WatchStats()

        self.seed_subjects()
        subjects = self.storage.list_subjects(active_only=True)
        stats.subjects_total = len(subjects)
        if not subjects:
            raise RuntimeError("no subjects to watch")

        events: List[WatchEvent] = []
        rate_hits: List[Dict[str, Any]] = []
        delta_hits: List[Dict[str, Any]] = []

        for subject in subjects:
            values, error = self.read_subject(subject)
            if error and not values:
                stats.subjects_failed += 1
                continue
            stats.subjects_read += 1
            stats.values_read += len(values)

            for value in values:
                if value.key in CHANGE_KEYS:
                    event = self.detect_change(subject, value)
                    if event is None:
                        continue
                    self.storage.record_watch_event(event, notified=False)
                    if event.first_ever:
                        stats.changes_first_seen += 1
                    else:
                        stats.changes += 1
                        events.append(event)
                elif value.key in DELTA_KEYS:
                    hit = self.check_delta(subject, value)
                    if hit:
                        delta_hits.append(hit)
                        stats.sharp_moves += 1
                else:
                    self.storage.set_watch_value(value)
                    hit = self.check_rate(subject, value, started)
                    if hit:
                        rate_hits.append(hit)
                        stats.rate_anomalies += 1

        if self.notifier is not None and (events or rate_hits or delta_hits):
            stats.alerts_sent = self.notifier.notify(events, rate_hits, delta_hits)

        stats.duration_seconds = round(time.monotonic() - started_wall, 2)
        self._report(stats, started, events, rate_hits)
        return stats

    def _report(self, stats: WatchStats, started: datetime, events, rate_hits) -> None:
        log.info("watch summary", extra=stats.as_dict())
        print(stats.as_logline())
        self.storage.record_run("watch", started, stats.as_dict())
        set_output("changes", str(stats.changes))
        set_output("alerts_sent", str(stats.alerts_sent))

        lines = ["### Valve watch", "", "`{}`".format(stats.as_logline()), ""]
        if events:
            lines += ["| Что | Было | Стало |", "| --- | --- | --- |"]
            for e in events:
                lines.append(
                    "| {} · {} | `{}` | `{}` |".format(
                        e.subject.name.replace("|", "/"), e.key, e.old_value[:28] or "—", e.new_value[:28]
                    )
                )
        elif stats.changes_first_seen:
            lines.append("Первый запуск: записано {} значений, уведомлений нет.".format(
                stats.changes_first_seen))
        else:
            lines.append("Изменений нет.")
        github_summary("\n".join(lines))
