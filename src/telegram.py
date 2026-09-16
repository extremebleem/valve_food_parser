"""Telegram Bot API transport and message plumbing.

Domain-free on purpose: this module knows how to put text on the wire, split it
under the 4096-character limit and print it instead when ``DRY_RUN`` is set.
What the text *says* belongs to :mod:`src.watch_telegram`.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .http import HttpError, client_from_settings
from .logging_utils import get_logger, redact
from .models import utcnow

log = get_logger(__name__)

TELEGRAM_MAX_CHARS = 4096


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Russian pluralisation (1 сигнал / 2 сигнала / 5 сигналов)."""
    tail_100 = abs(count) % 100
    tail_10 = abs(count) % 10
    if 11 <= tail_100 <= 14:
        return many
    if tail_10 == 1:
        return one
    if 2 <= tail_10 <= 4:
        return few
    return many


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


class MessageBuilder:
    """Shared formatting helpers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def esc(text: Any) -> str:
        return html.escape(str(text), quote=False)

    def tz(self):
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.settings.timezone)
        except Exception:  # pragma: no cover - tzdata missing
            from datetime import timezone

            return timezone.utc

    def local_time(self, moment: datetime) -> str:
        return moment.astimezone(self.tz()).strftime("%H:%M")

    def test_message(self) -> str:
        return "\n".join(
            [
                "✅ <b>valve-watch: тестовое сообщение</b>",
                "",
                "Бот настроен правильно.",
                "Часовой пояс: {}".format(self.esc(self.settings.timezone)),
                "Время: {}".format(self.local_time(utcnow())),
            ]
        )


class TelegramClient:
    """Bot API transport. In dry-run mode it prints instead of sending."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.telegram
        self.dry_run = settings.dry_run
        self.client = client_from_settings(settings, rate_limit_rps=1.0)
        self.client.cache_ttl = 0
        self.sent_count = 0
        self._last_message_id: Optional[int] = None

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _url(self, method: str) -> str:
        return "{}/bot{}/{}".format(self.cfg.api_base.rstrip("/"), self.cfg.bot_token, method)

    def send_and_get_id(
        self, text: str, silent: Optional[bool] = None
    ) -> Tuple[bool, Optional[int]]:
        """Send and report the resulting message id, when there is one.

        ``None`` for the id means "sent, but not editable afterwards": dry-run,
        an unconfigured bot, or a message long enough to be split into chunks.
        """
        chunks = split_message(text)
        ok = self.send_message(text, silent=silent)
        if not ok or len(chunks) != 1:
            return ok, None
        if self.dry_run or not self.configured:
            # Hand back a synthetic id so the edit path is exercised and
            # visible in a dry run. Without it every dry run looks like a
            # fresh send and the operator cannot see what production does.
            self._last_message_id = (self._last_message_id or 0) + 1
            return ok, self._last_message_id
        return ok, self._last_message_id

    def edit_message(self, message_id: int, text: str, silent: Optional[bool] = None) -> bool:
        """Rewrite a message we sent earlier.

        Returns ``False`` when Telegram will not edit it -- most often because
        the message is gone or older than the 48 hours Telegram allows -- so the
        caller can fall back to sending a new one. "Not modified" counts as
        success: the text is already what we wanted.
        """
        if self.dry_run or not self.configured:
            print(
                "\n--- [{}, edit #{}] telegram message ---\n{}\n".format(
                    "DRY_RUN" if self.dry_run else "telegram not configured", message_id, text
                )
            )
            return True
        if len(split_message(text)) != 1:
            return False
        try:
            payload = self.client.post_json(
                self._url("editMessageText"),
                json_body={
                    "chat_id": self.cfg.chat_id,
                    "message_id": int(message_id),
                    "text": text,
                    "parse_mode": self.cfg.parse_mode,
                    "disable_web_page_preview": True,
                },
                cache_ttl=0,
            )
        except HttpError as exc:
            body = (exc.body or "").lower()
            if "not modified" in body:
                return True
            log.info(
                "telegram edit declined, will send instead",
                extra={"message_id": message_id, "status": exc.status, "body": exc.body[:160]},
            )
            return False
        if isinstance(payload, dict) and not payload.get("ok", False):
            log.info("telegram edit rejected", extra={"response": str(payload)[:200]})
            return False
        return True

    def send_message(self, text: str, silent: Optional[bool] = None) -> bool:
        """Returns True when every chunk was accepted (or printed in dry-run).

        ``silent=True`` suppresses the notification sound for this message even
        when the chat is not muted -- used for routine status messages, so only
        real changes ever ping.
        """
        quiet = self.cfg.disable_notification if silent is None else bool(silent)
        chunks = split_message(text)

        if self.dry_run or not self.configured:
            reason = "DRY_RUN" if self.dry_run else "telegram not configured"
            for index, chunk in enumerate(chunks, start=1):
                print(
                    "\n--- [{}{}] telegram message {}/{} ---\n{}\n".format(
                        reason, ", silent" if quiet else "", index, len(chunks), chunk
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
                        "disable_notification": quiet,
                    },
                    cache_ttl=0,
                )
                if isinstance(payload, dict) and not payload.get("ok", False):
                    log.error("telegram rejected message", extra={"response": str(payload)[:300]})
                    ok = False
                else:
                    self.sent_count += 1
                    result = payload.get("result") if isinstance(payload, dict) else None
                    if isinstance(result, dict) and result.get("message_id") is not None:
                        self._last_message_id = int(result["message_id"])
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
        """``getChat`` -- proves the configured chat id resolves."""
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
                            "title": chat.get("title")
                            or chat.get("username")
                            or chat.get("first_name"),
                        }
                    )
        unique: Dict[Any, Dict[str, Any]] = {item["id"]: item for item in found}
        return list(unique.values())
