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
    "required_version": "🧩 Версия сервера изменилась",
    "latest_prerelease": "🧪 Новый пре-релизный билд",
    "latest_news": "📰 Официальный пост",
    "latest_release": "🏷 Новый релиз",
}

#: A pre-release post is the whole point of this project: it is the signal with
#: the longest lead time over a stable update.
PRIORITY = {"latest_prerelease": 0, "required_version": 1, "latest_release": 2, "latest_news": 3}


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

    def render_event(self, event: WatchEvent) -> List[str]:
        lines = [
            "{}".format(KEY_TITLES.get(event.key, "🔔 Изменение")),
            "<b>{}</b>".format(self.esc(event.subject.name)),
        ]
        if event.label:
            lines.append("<i>{}</i>".format(self.esc(event.label)))
        if event.key == "required_version":
            lines.append("<code>{} → {}</code>".format(self.esc(event.old_value), self.esc(event.new_value)))
        if event.detail:
            lines.append(self.esc(event.detail))
        lead = LEAD_TIME.get(event.key)
        if lead:
            lines.append("⏱ типичная фора: {}".format(lead))
        if event.url:
            lines.append('🔗 <a href="{}">открыть</a>'.format(self.esc(event.url)))
        return lines

    def build(self, events: Sequence[WatchEvent], rate_hits: Sequence[Dict[str, Any]]) -> str:
        ordered = sorted(events, key=lambda e: (PRIORITY.get(e.key, 9), e.subject.priority))
        total = len(ordered) + len(rate_hits)
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

    def notify(self, events: Sequence[WatchEvent], rate_hits: Sequence[Dict[str, Any]]) -> int:
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

        if not fresh and not rate_hits:
            return 0

        text = self.build(fresh, rate_hits)
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
        return (len(fresh) + len(rate_hits)) if delivered else 0
