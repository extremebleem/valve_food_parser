"""Telegram notification: gating, formatting and delivery.

Three separable concerns, deliberately kept apart so each is testable:

* :class:`AlertGate`     -- may this alert be sent at all? (cooldown, dedupe)
* :class:`MessageBuilder` -- what does the message say?
* :class:`TelegramClient` -- put it on the wire (or on stdout in dry-run)
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .config import AlertConfig, Settings
from .geo import format_distance
from .http import HttpError, client_from_settings
from .logging_utils import get_logger, redact
from .models import (
    AlertKind,
    AlertRecord,
    AnomalyResult,
    BaselineStatus,
    band_for_score,
    utcnow,
)
from .normalization import BAND_LABELS, DOMAIN_LABELS, Normalizer
from .storage import BaseStorage

log = get_logger(__name__)

TELEGRAM_MAX_CHARS = 4096


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Russian pluralisation (1 заведение / 2 заведения / 5 заведений)."""
    tail_100 = abs(count) % 100
    tail_10 = abs(count) % 10
    if 11 <= tail_100 <= 14:
        return many
    if tail_10 == 1:
        return one
    if 2 <= tail_10 <= 4:
        return few
    return many


def message_hash(venue_id: str, kind: str, score: float, baseline: float) -> str:
    """Content fingerprint used to suppress repeats that say the same thing.

    Scores are floored into 5-point bins, so a small wobble inside a bin
    produces an identical hash. This is a secondary guard only -- the cooldown
    in :class:`AlertGate` is what actually bounds the notification rate, because
    two scores either side of a bin edge still hash differently.
    """
    key = "{}|{}|{:.0f}|{:.0f}".format(venue_id, kind, score // 5, baseline // 5)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


@dataclass
class AlertDecision:
    should_send: bool
    reason: str
    kind: str = AlertKind.ANOMALY.value
    escalated: bool = False


class AlertGate:
    """Cooldown + de-duplication.

    A repeat notification for a venue that is still anomalous is allowed when:

    * ``ALERT_COOLDOWN_MINUTES`` has elapsed since the last one, **or**
    * the load score climbed by at least ``ALERT_ESCALATION_DELTA`` (the
      situation got materially worse).

    A venue that recovered and became anomalous again re-arms immediately, with
    an ``ALERT_REARM_MINUTES`` floor so a venue oscillating around the threshold
    cannot spam the chat.
    """

    def __init__(self, storage: BaseStorage, config: Optional[AlertConfig] = None) -> None:
        self.storage = storage
        self.config = config or AlertConfig()

    def decide(self, result: AnomalyResult, now: Optional[datetime] = None) -> AlertDecision:
        now = now or utcnow()
        cfg = self.config
        venue_id = result.venue.id
        state = self.storage.alert_state(venue_id)
        last_at = state.get("last_alert_at")
        elapsed_minutes = (
            (now - last_at).total_seconds() / 60.0 if isinstance(last_at, datetime) else None
        )

        if not result.is_anomaly:
            return AlertDecision(False, "not an anomaly")

        current = result.current_score
        fingerprint = message_hash(venue_id, AlertKind.ANOMALY.value, current, result.baseline_score)

        if not state.get("active"):
            if elapsed_minutes is not None and elapsed_minutes < cfg.rearm_minutes:
                return AlertDecision(
                    False,
                    "re-arm window: {:.0f} min < {} min".format(elapsed_minutes, cfg.rearm_minutes),
                )
            return AlertDecision(True, "new anomaly")

        if elapsed_minutes is None:
            return AlertDecision(True, "active without timestamp")

        if elapsed_minutes >= cfg.cooldown_minutes:
            return AlertDecision(
                True, "cooldown elapsed ({:.0f} min)".format(elapsed_minutes)
            )

        last_score = float(state.get("last_score") or 0.0)
        if current >= last_score + cfg.escalation_delta:
            return AlertDecision(
                True,
                "escalation +{:.1f} (>= {:.1f})".format(current - last_score, cfg.escalation_delta),
                escalated=True,
            )

        previous = self.storage.last_alert(venue_id, AlertKind.ANOMALY.value)
        if previous and previous.message_hash == fingerprint:
            return AlertDecision(
                False,
                "duplicate of alert {:.0f} min ago".format(elapsed_minutes),
            )
        return AlertDecision(
            False,
            "cooldown active ({:.0f}/{} min)".format(elapsed_minutes, cfg.cooldown_minutes),
        )

    def decide_recovery(self, result: AnomalyResult, now: Optional[datetime] = None) -> AlertDecision:
        now = now or utcnow()
        if not self.config.send_recovery:
            return AlertDecision(False, "recovery alerts disabled", AlertKind.RECOVERY.value)
        state = self.storage.alert_state(result.venue.id)
        if not state.get("active"):
            return AlertDecision(False, "venue was not in an alerting state", AlertKind.RECOVERY.value)
        if result.is_anomaly:
            return AlertDecision(False, "still anomalous", AlertKind.RECOVERY.value)
        if result.baseline is None or result.baseline.status is not BaselineStatus.OK:
            return AlertDecision(False, "no usable baseline", AlertKind.RECOVERY.value)
        if result.current_score > result.baseline.median * self.config.recovery_ratio:
            return AlertDecision(False, "not back to normal yet", AlertKind.RECOVERY.value)
        return AlertDecision(True, "recovered", AlertKind.RECOVERY.value)


class MessageBuilder:
    """Renders Telegram messages (HTML parse mode)."""

    def __init__(self, settings: Settings, normalizer: Optional[Normalizer] = None) -> None:
        self.settings = settings
        self.normalizer = normalizer or Normalizer(settings.normalization)

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def esc(text: Any) -> str:
        return html.escape(str(text), quote=False)

    def local_time(self, moment: datetime) -> str:
        return moment.astimezone(self._tz()).strftime("%H:%M")

    def _tz(self):
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.settings.office.timezone)
        except Exception:  # pragma: no cover - tzdata missing
            from datetime import timezone

            return timezone.utc

    def _metric_lines(self, result: AnomalyResult) -> List[str]:
        """Raw-metric context ("Delivery ETA 55 min, usually ~32")."""
        obs = result.observation
        lines = [self.normalizer.describe_metric(obs.metric_type, obs.metric_value)]
        if result.baseline is not None and result.baseline.usable:
            usual = self.normalizer.invert(obs.metric_type, result.baseline.median)
            if usual is not None and self.normalizer.envelope(obs.metric_type) is not None:
                lines.append("🕐 Обычно: ~{:.0f} мин".format(usual))
        return lines

    # -- messages ---------------------------------------------------------- #

    def anomaly(self, result: AnomalyResult, *, escalated: bool = False) -> str:
        venue = result.venue
        obs = result.observation
        header = "🔥 <b>Необычно высокая загрузка</b>"
        if escalated:
            header = "🔥🔥 <b>Загрузка продолжает расти</b>"

        parts = [
            header,
            "",
            "<b>{}</b>".format(self.esc(venue.name)),
        ]
        if venue.category:
            parts.append("<i>{}</i>".format(self.esc(venue.category)))
        parts += [
            "",
            "📍 {} от офиса {}".format(
                format_distance(venue.distance_meters), self.esc(self.settings.office.name)
            ),
            "📊 Сейчас: {:.0f}/100 ({})".format(
                obs.load_score, BAND_LABELS.get(band_for_score(obs.load_score), "")
            ),
        ]
        if result.baseline is not None and result.baseline.usable:
            parts.append("📈 Обычно в это время: {:.0f}/100".format(result.baseline.median))
            parts.append("⚠️ {:+.0f}% выше обычного".format(result.deviation_percent))
            parts.append(
                "🧮 robust z = {:.1f}, выборка {} набл. за {} нед.".format(
                    result.robust_z,
                    result.baseline.sample_count,
                    self.settings.anomaly.lookback_weeks,
                )
            )
        else:
            parts.append("📈 Baseline: недостаточно истории (learning_baseline)")

        parts.append("")
        parts.extend(self._metric_lines(result))
        parts.append("")
        parts.append(
            "🧭 Тип сигнала: {}".format(
                self.esc(DOMAIN_LABELS.get(_domain(obs.domain), obs.domain))
            )
        )
        parts.append(
            "🔌 Источник: {} (качество: {}, confidence {:.2f})".format(
                self.esc(obs.source), self.esc(obs.signal_quality), obs.confidence
            )
        )
        parts.append("🕓 Время проверки: {}".format(self.local_time(obs.timestamp)))
        if venue.website:
            parts.append('🔗 <a href="{}">сайт</a>'.format(self.esc(venue.website)))
        return "\n".join(parts)

    def aggregate(self, results: Sequence[AnomalyResult]) -> str:
        ordered = sorted(results, key=lambda r: -r.deviation_percent)
        moment = ordered[0].observation.timestamp if ordered else utcnow()
        parts = [
            "🔥 <b>Повышенная загрузка рядом с {}</b>".format(self.esc(self.settings.office.name)),
            "<i>{} {} выше обычного · проверка {}</i>".format(
                len(ordered),
                plural_ru(len(ordered), "заведение", "заведения", "заведений"),
                self.local_time(moment),
            ),
            "",
        ]
        for index, result in enumerate(ordered, start=1):
            obs = result.observation
            baseline_text = (
                "{:.0f}".format(result.baseline.median)
                if result.baseline is not None and result.baseline.usable
                else "—"
            )
            parts.append(
                "<b>{}. {}</b> — {:+.0f}%".format(
                    index, self.esc(result.venue.name), result.deviation_percent
                )
            )
            parts.append(
                "    📊 {:.0f}/100 (обычно {}) · 📍 {} · {}".format(
                    obs.load_score,
                    baseline_text,
                    format_distance(result.venue.distance_meters),
                    self.esc(obs.source),
                )
            )
            detail = self.normalizer.describe_metric(obs.metric_type, obs.metric_value)
            parts.append("    {}".format(detail))
            parts.append("")
        parts.append(
            "<i>Показатель — proxy-метрика ({}), не фактическое число посетителей.</i>".format(
                self.esc(
                    ", ".join(
                        sorted({DOMAIN_LABELS.get(_domain(r.observation.domain), "n/a") for r in ordered})
                    )
                )
            )
        )
        return "\n".join(parts)

    def recovery(self, result: AnomalyResult, peak_score: float) -> str:
        return "\n".join(
            [
                "✅ <b>Загрузка нормализовалась</b>",
                "",
                "<b>{}</b>".format(self.esc(result.venue.name)),
                "Было: {:.0f}/100".format(peak_score or result.current_score),
                "Сейчас: {:.0f}/100".format(result.current_score),
                "Обычно в это время: {:.0f}/100".format(result.baseline_score),
                "",
                "🕓 {}".format(self.local_time(result.observation.timestamp)),
            ]
        )

    def test_message(self) -> str:
        office = self.settings.office
        return "\n".join(
            [
                "✅ <b>valve-food-monitor: тестовое сообщение</b>",
                "",
                "Бот настроен правильно.",
                "Офис: <b>{}</b>".format(self.esc(office.name)),
                "Адрес: {}".format(self.esc(office.address)),
                "Координаты: {:.5f}, {:.5f}".format(office.latitude, office.longitude),
                "Радиус поиска: {} м".format(office.radius_meters),
                "Часовой пояс: {}".format(self.esc(office.timezone)),
                "Время: {}".format(self.local_time(utcnow())),
            ]
        )


def _domain(value: Any):
    from .models import CongestionDomain

    try:
        return CongestionDomain(str(value))
    except ValueError:
        return CongestionDomain.UNKNOWN


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> List[str]:
    """Split on blank lines so an HTML tag is never cut in half."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for block in text.split("\n\n"):
        block_size = len(block) + 2
        if size + block_size > limit and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        if block_size > limit:  # single oversized block: hard split
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            for start in range(0, len(block), limit):
                chunks.append(block[start : start + limit])
            continue
        current.append(block)
        size += block_size
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class TelegramClient:
    """Bot API transport. In dry-run mode it prints instead of sending."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.telegram
        self.dry_run = settings.dry_run
        self.client = client_from_settings(settings, rate_limit_rps=1.0)
        self.client.cache_ttl = 0
        self.sent_count = 0

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _url(self, method: str) -> str:
        return "{}/bot{}/{}".format(self.cfg.api_base.rstrip("/"), self.cfg.bot_token, method)

    def send_message(self, text: str) -> bool:
        """Returns True when every chunk was accepted (or printed in dry-run)."""
        chunks = split_message(text)

        if self.dry_run or not self.configured:
            reason = "DRY_RUN" if self.dry_run else "telegram not configured"
            for index, chunk in enumerate(chunks, start=1):
                print(
                    "\n--- [{}] telegram message {}/{} ---\n{}\n".format(
                        reason, index, len(chunks), chunk
                    )
                )
            log.info(
                "telegram message not sent",
                extra={"reason": reason, "chunks": len(chunks), "chars": len(text)},
            )
            return True

        ok = True
        for index, chunk in enumerate(chunks, start=1):
            try:
                payload = self.client.post_json(
                    self._url("sendMessage"),
                    json_body={
                        "chat_id": self.cfg.chat_id,
                        "text": chunk,
                        "parse_mode": self.cfg.parse_mode,
                        "disable_web_page_preview": True,
                        "disable_notification": self.cfg.disable_notification,
                    },
                    cache_ttl=0,
                )
                if isinstance(payload, dict) and not payload.get("ok", False):
                    log.error("telegram rejected message", extra={"response": str(payload)[:300]})
                    ok = False
                else:
                    self.sent_count += 1
            except HttpError as exc:
                log.error(
                    "telegram send failed",
                    extra={
                        "error": str(exc),
                        "status": exc.status,
                        "body": exc.body,
                        "chunk": index,
                        "token": redact(self.cfg.bot_token),
                    },
                )
                ok = False
        return ok

    def describe_chat(self) -> Dict[str, Any]:
        """``getChat`` -- used by ``--test-telegram`` to prove the id is right."""
        if not self.configured:
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}
        try:
            return self.client.post_json(
                self._url("getChat"), json_body={"chat_id": self.cfg.chat_id}, cache_ttl=0
            )
        except HttpError as exc:
            return {"ok": False, "error": str(exc), "status": exc.status}

    def recent_chat_ids(self) -> List[Dict[str, Any]]:
        """``getUpdates`` helper for finding TELEGRAM_CHAT_ID the first time."""
        if not self.cfg.bot_token:
            return []
        try:
            payload = self.client.get_json(self._url("getUpdates"), cache_ttl=0)
        except HttpError as exc:
            log.error("getUpdates failed", extra={"error": str(exc)})
            return []
        found: List[Dict[str, Any]] = []
        for update in (payload or {}).get("result", []) if isinstance(payload, dict) else []:
            for key in ("message", "channel_post", "edited_message", "my_chat_member"):
                chat = ((update.get(key) or {}).get("chat")) if isinstance(update, dict) else None
                if isinstance(chat, dict) and chat.get("id") is not None:
                    found.append(
                        {
                            "id": chat.get("id"),
                            "type": chat.get("type"),
                            "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                        }
                    )
        unique: Dict[Any, Dict[str, Any]] = {item["id"]: item for item in found}
        return list(unique.values())


class Notifier:
    """Glues the gate, the builder, the transport and the alert log together."""

    def __init__(
        self,
        settings: Settings,
        storage: BaseStorage,
        client: Optional[TelegramClient] = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.gate = AlertGate(storage, settings.alerts)
        self.builder = MessageBuilder(settings)
        self.client = client or TelegramClient(settings)

    def process(self, results: Sequence[AnomalyResult]) -> Dict[str, Any]:
        """Decide, send and record. Returns counters for the run summary."""
        now = utcnow()
        cfg = self.settings.alerts

        to_alert: List[AnomalyResult] = []
        suppressed: List[str] = []
        escalated_ids = set()

        for result in results:
            if not result.is_anomaly:
                continue
            decision = self.gate.decide(result, now)
            if decision.should_send:
                to_alert.append(result)
                if decision.escalated:
                    escalated_ids.add(result.venue.id)
            else:
                suppressed.append("{}: {}".format(result.venue.name, decision.reason))
                log.info(
                    "alert suppressed",
                    extra={"venue": result.venue.name, "reason": decision.reason},
                )

        to_alert.sort(key=lambda r: -r.deviation_percent)
        overflow = max(0, len(to_alert) - cfg.max_alerts_per_run)
        if overflow:
            log.warning("alert budget exceeded", extra={"dropped": overflow})
            suppressed.extend(
                "{}: over MAX_ALERTS_PER_RUN".format(r.venue.name)
                for r in to_alert[cfg.max_alerts_per_run :]
            )
            to_alert = to_alert[: cfg.max_alerts_per_run]

        sent = 0
        if to_alert:
            if cfg.aggregate and len(to_alert) > 1:
                text = self.builder.aggregate(to_alert)
            else:
                text = self.builder.anomaly(
                    to_alert[0], escalated=to_alert[0].venue.id in escalated_ids
                )
            delivered = self.client.send_message(text)
            sent = len(to_alert) if delivered else 0
            for result in to_alert:
                self._record(result, AlertKind.ANOMALY.value, delivered, now)

        recoveries = 0
        for result in results:
            decision = self.gate.decide_recovery(result, now)
            if not decision.should_send:
                continue
            state = self.storage.alert_state(result.venue.id)
            text = self.builder.recovery(result, float(state.get("peak_score") or 0.0))
            delivered = self.client.send_message(text)
            self._record(result, AlertKind.RECOVERY.value, delivered, now, clear=True)
            recoveries += 1 if delivered else 0

        return {
            "alerts_sent": sent,
            "alerts_suppressed": len(suppressed),
            "recoveries_sent": recoveries,
            "suppressed_reasons": suppressed[:20],
        }

    def _record(
        self,
        result: AnomalyResult,
        kind: str,
        delivered: bool,
        now: datetime,
        *,
        clear: bool = False,
    ) -> None:
        self.storage.record_alert(
            AlertRecord(
                venue_id=result.venue.id,
                kind=kind,
                load_score=result.current_score,
                baseline_score=result.baseline_score,
                deviation_percent=result.deviation_percent,
                metric_type=result.observation.metric_type,
                sent_at=now,
                message_hash=message_hash(
                    result.venue.id, kind, result.current_score, result.baseline_score
                ),
                delivered=delivered,
            )
        )
        if clear:
            self.storage.set_alert_state(
                result.venue.id,
                metric_type=result.observation.metric_type,
                active=False,
                last_alert_at=now,
                last_score=result.current_score,
                last_deviation=result.deviation_percent,
                peak_score=0.0,
            )
        else:
            previous = self.storage.alert_state(result.venue.id)
            self.storage.set_alert_state(
                result.venue.id,
                metric_type=result.observation.metric_type,
                active=True,
                last_alert_at=now,
                last_score=result.current_score,
                last_deviation=result.deviation_percent,
                peak_score=max(float(previous.get("peak_score") or 0.0), result.current_score),
            )


def mark_state_from_observation(storage: BaseStorage, result: AnomalyResult) -> None:
    """Track the peak while a venue stays anomalous between notifications."""
    state = storage.alert_state(result.venue.id)
    if not state.get("active"):
        return
    peak = max(float(state.get("peak_score") or 0.0), result.current_score)
    if peak > float(state.get("peak_score") or 0.0):
        storage.set_alert_state(
            result.venue.id,
            metric_type=result.observation.metric_type,
            active=True,
            last_alert_at=state.get("last_alert_at"),
            last_score=float(state.get("last_score") or 0.0),
            last_deviation=float(state.get("last_deviation") or 0.0),
            peak_score=peak,
        )

