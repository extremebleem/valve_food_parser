"""Telegram notifications for watch events.

Wording rule: say what changed and where to look. A version bump or a preview
build is evidence that something shipped or is about to; it is not evidence of
what that something is, and the message must not pretend otherwise.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence

from .config import Settings
from .logging_utils import get_logger
from .models import AlertRecord, utcnow
from .storage import BaseStorage
from .subjects import LEAD_TIME, WatchEvent
from .telegram import MessageBuilder, TelegramClient, plural_ru

log = get_logger(__name__)

KEY_TITLES = {
    "gc_deploy_in_flight": "🚀 Выкатка идёт прямо сейчас",
    "cs2_scheduler": "🎯 Матчмейкинг CS2 сменил состояние",
    "cs2_services": "🛠 Сервисы CS2 сменили состояние",
    "latest_prerelease": "🧪 Новый пре-релизный билд",
    "sdr_pops": "🌐 Изменился состав релейных дата-центров",
    "sdr_revision": "🛰 Обновлён конфиг сети CS2",
    "required_version": "🧩 Версия сервера изменилась",
    "cs2_app_version": "🧩 Версия CS2 изменилась",
    "gc_active_version": "🧩 Версия game coordinator изменилась",
    "latest_release": "🏷 Новый релиз",
    "latest_news": "📰 Официальный пост",
}

#: Ordered by how much warning the signal gives. A deploy in flight and a
#: matchmaking state change are happening *now*; a pre-release post and a
#: datacenter change are days out. Both ends matter more than the middle.
PRIORITY = {
    "gc_deploy_in_flight": 0,
    "cs2_scheduler": 1,
    "cs2_services": 2,
    "latest_prerelease": 3,
    "sdr_pops": 4,
    "sdr_revision": 5,
    "cs2_app_version": 6,
    "required_version": 7,
    "gc_active_version": 8,
    "latest_release": 9,
    "latest_news": 10,
}


def event_hash(event: WatchEvent) -> str:
    return hashlib.sha1(
        "{}|{}|{}".format(event.subject.id, event.key, event.new_value).encode("utf-8")
    ).hexdigest()[:20]


class WatchNotifier:
    def __init__(
        self,
        settings: Settings,
        storage: BaseStorage,
        client: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.client = client or TelegramClient(settings)
        self.time = MessageBuilder(settings)

    # -- rendering --------------------------------------------------------- #

    def esc(self, text: Any) -> str:
        return MessageBuilder.esc(text)

    @staticmethod
    def title_for(event: WatchEvent) -> str:
        """Title depends on the new value, not only on the key.

        ``gc_deploy_in_flight`` going yes->no means a rollout *finished*, which
        is different news from one starting; titling both "a rollout is in
        flight" was simply wrong.
        """
        value = str(event.new_value).strip().lower()
        if event.key == "gc_deploy_in_flight":
            return (
                "🚀 Выкатка идёт прямо сейчас" if value == "yes" else "✅ Выкатка завершилась"
            )
        if event.key == "cs2_scheduler":
            return (
                "🎯 Матчмейкинг CS2 вернулся в норму"
                if value == "normal"
                else "🎯 Матчмейкинг CS2: {}".format(value)
            )
        if event.key == "cs2_services":
            return "🛠 Сервисы CS2 сменили состояние"
        return KEY_TITLES.get(event.key, "🔔 Изменение")

    @staticmethod
    def parse_services(text: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for part in str(text).split(","):
            if "=" in part:
                key, _, val = part.partition("=")
                out[key.strip()] = val.strip()
        return out

    @classmethod
    def service_changes(cls, old: str, new: str) -> List[str]:
        """Only the services that actually moved.

        Valve reports IEconItems=offline and Leaderboards=idle as steady
        states, so listing everything that is not "normal" would repeat the
        same non-news every time.
        """
        before, after = cls.parse_services(old), cls.parse_services(new)
        changed = []
        for key in sorted(set(before) | set(after)):
            was, now = before.get(key, "—"), after.get(key, "—")
            if was != now:
                changed.append("{}: {} → {}".format(key, was, now))
        return changed

    def render_event(self, event: WatchEvent) -> List[str]:
        lines = [
            self.title_for(event),
            "<b>{}</b>".format(self.esc(event.subject.name)),
        ]
        if event.label and event.key not in ("gc_deploy_in_flight", "cs2_scheduler", "cs2_services"):
            lines.append("<i>{}</i>".format(self.esc(event.label)))
        if event.key in ("required_version", "cs2_app_version", "gc_active_version"):
            lines.append("<code>{} → {}</code>".format(self.esc(event.old_value), self.esc(event.new_value)))
        if event.key == "cs2_services":
            changed = self.service_changes(event.old_value, event.new_value)
            lines.extend("<code>{}</code>".format(self.esc(c)) for c in changed[:8])
        elif event.detail:
            lines.append(self.esc(event.detail))
        lead = LEAD_TIME.get(event.key)
        if lead and not (event.key == "gc_deploy_in_flight" and event.new_value != "yes"):
            lines.append("⏱ типичная фора: {}".format(lead))
        if event.url:
            lines.append('🔗 <a href="{}">открыть</a>'.format(self.esc(event.url)))
        return lines

    def build(
        self,
        events: Sequence[WatchEvent],
        rate_hits: Sequence[Dict[str, Any]],
        delta_hits: Sequence[Dict[str, Any]] = (),
    ) -> str:
        ordered = sorted(events, key=lambda e: (PRIORITY.get(e.key, 99), e.subject.priority))
        total = len(ordered) + len(rate_hits) + len(delta_hits)
        parts = [
            "⚡ <b>У Valve что-то происходит</b>",
            "<i>{} {} · {}</i>".format(
                total,
                plural_ru(total, "сигнал", "сигнала", "сигналов"),
                self.time.local_time(utcnow()),
            ),
            "",
        ]
        for event in ordered:
            parts.extend(self.render_event(event))
            parts.append("")

        if delta_hits:
            parts.append("📊 <b>Резкое изменение</b>")
            for hit in delta_hits:
                arrow = "📉" if hit["change"] < 0 else "📈"
                parts.append(
                    "{} <b>{}</b> — {:+.0f}%".format(
                        arrow, self.esc(hit["subject"].name), hit["change"] * 100.0
                    )
                )
                parts.append(
                    "    <i>{:,.0f} → {:,.0f} · {}</i>".format(
                        hit["previous"], hit["current"], self.esc(hit["key"])
                    ).replace(",", " ")
                )
                parts.append("")

        if rate_hits:
            parts.append("📈 <b>Всплеск активности</b>")
            for hit in rate_hits:
                parts.append(
                    "<b>{}</b> — {:.0f} против обычных {:.0f}".format(
                        self.esc(hit["subject"].name), hit["current"], hit["median"]
                    )
                )
                parts.append(
                    "    <i>{} · выборка {} дн.</i>".format(self.esc(hit["key"]), hit["samples"])
                )
                if hit.get("url"):
                    parts.append('    🔗 <a href="{}">коммиты</a>'.format(self.esc(hit["url"])))
                parts.append("")

        parts.append(
            "<i>Это наблюдение за публичным состоянием, а не инсайд: видно, что "
            "изменилось, но не что именно готовится.</i>"
        )
        return "\n".join(parts)

    # -- delivery ---------------------------------------------------------- #

    def notify(
        self,
        events: Sequence[WatchEvent],
        rate_hits: Sequence[Dict[str, Any]],
        delta_hits: Sequence[Dict[str, Any]] = (),
    ) -> int:
        fresh: List[WatchEvent] = []
        for event in events:
            if event.first_ever:
                continue
            fingerprint = event_hash(event)
            previous = self.storage.last_alert(event.subject.id, "watch")
            if previous and previous.message_hash == fingerprint:
                log.info(
                    "watch alert suppressed as duplicate",
                    extra={"subject": event.subject.name, "key": event.key},
                )
                continue
            fresh.append(event)

        if not fresh and not rate_hits and not delta_hits:
            return 0

        text = self.build(fresh, rate_hits, delta_hits)
        delivered = self.client.send_message(text)

        for event in fresh:
            self.storage.record_alert(
                AlertRecord(
                    venue_id=event.subject.id,
                    kind="watch",
                    load_score=0.0,
                    baseline_score=0.0,
                    deviation_percent=0.0,
                    metric_type=event.key,
                    sent_at=utcnow(),
                    message_hash=event_hash(event),
                    delivered=delivered,
                )
            )
        return (len(fresh) + len(rate_hits) + len(delta_hits)) if delivered else 0
