"""Telegram transport: splitting, escaping, dry-run and failure handling."""

from __future__ import annotations

import dataclasses

import pytest

from src.telegram import MessageBuilder, TelegramClient, plural_ru, split_message


@pytest.mark.parametrize(
    "count,expected",
    [(1, "сигнал"), (2, "сигнала"), (4, "сигнала"), (5, "сигналов"),
     (11, "сигналов"), (21, "сигнал"), (22, "сигнала"), (25, "сигналов")],
)
def test_russian_plurals(count, expected):
    assert plural_ru(count, "сигнал", "сигнала", "сигналов") == expected


def test_short_message_is_not_split():
    assert split_message("hello") == ["hello"]


def test_long_messages_are_split_under_the_limit():
    text = "\n\n".join("block {}".format(i) * 50 for i in range(200))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "block 0" in chunks[0]


def test_an_oversized_single_block_is_hard_split():
    chunks = split_message("x" * 9000)
    assert len(chunks) == 3
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_html_escaping():
    assert MessageBuilder.esc("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_dry_run_prints_instead_of_sending(settings, capsys):
    client = TelegramClient(dataclasses.replace(settings, dry_run=True))
    assert client.send_message("hello world") is True
    captured = capsys.readouterr()
    assert "DRY_RUN" in captured.out and "hello world" in captured.out
    assert client.sent_count == 0


def test_unconfigured_telegram_does_not_crash_the_run(settings, capsys):
    client = TelegramClient(dataclasses.replace(settings, dry_run=False))
    assert client.configured is False
    assert client.send_message("hello") is True
    assert "not configured" in capsys.readouterr().out


def test_describe_chat_without_configuration(settings):
    result = TelegramClient(dataclasses.replace(settings, dry_run=False)).describe_chat()
    assert result["ok"] is False


def test_test_message_mentions_the_timezone(settings):
    text = MessageBuilder(settings).test_message()
    assert "America/Los_Angeles" in text
    assert "тестовое сообщение" in text


def test_local_time_uses_the_configured_zone(settings):
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 15, 19, 40, tzinfo=timezone.utc)  # 12:40 PDT
    assert MessageBuilder(settings).local_time(moment) == "12:40"
    berlin = dataclasses.replace(settings, timezone="Europe/Berlin")
    assert MessageBuilder(berlin).local_time(moment) == "21:40"


def test_no_message_can_carry_a_personal_identifier(settings, storage):
    """Dry-run prints the whole message to stdout, and on a public repository
    that log is readable by anyone. Nothing in a message may identify a person.
    """
    import re

    from src.subjects import Subject, WatchEvent
    from src.watch_telegram import WatchNotifier

    notifier = WatchNotifier(settings, storage, client=_Recorder())
    subject = Subject.steam_app(730, "Counter-Strike 2")
    texts = [
        notifier.build([WatchEvent(subject, "depot_public_buildid", "1", "2")], []),
        notifier.build_heartbeat(_Stats()),
    ]
    for text in texts:
        assert re.search(r"@[A-Za-z0-9_]{4,}", text) is None, text[:200]


def test_telegram_config_has_no_mention_field(settings):
    """Removed deliberately: the chat is unmuted, so a mention bought nothing
    and would have leaked a username into a public log."""
    assert not hasattr(settings.telegram, "mention")


class _Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, text, silent=None):
        self.messages.append(text)
        return True


class _Stats:
    values_read = 3
    subjects_read = 2
    subjects_failed = 0


# --------------------------------------------------------------------------- #
# one self-updating status message
# --------------------------------------------------------------------------- #


class EditingClient:
    """Records sends and edits, and hands out message ids like Telegram does."""

    def __init__(self, edit_ok=True):
        self.sent = []
        self.edited = []
        self.edit_ok = edit_ok
        self._next_id = 100

    def send_message(self, text, silent=None):
        self.sent.append({"text": text, "silent": silent})
        return True

    def send_and_get_id(self, text, silent=None):
        self.send_message(text, silent=silent)
        self._next_id += 1
        return True, self._next_id

    def edit_message(self, message_id, text, silent=None):
        self.edited.append({"id": message_id, "text": text})
        return self.edit_ok


def notifier_with(settings, storage, client):
    from src.watch_telegram import WatchNotifier

    return WatchNotifier(settings, storage, client=client)


class _S:
    values_read = 5
    subjects_read = 3
    subjects_failed = 0


def test_the_first_status_message_is_sent_then_edited(settings, storage):
    client = EditingClient()
    n = notifier_with(settings, storage, client)

    assert n.send_heartbeat(_S()) is True
    assert len(client.sent) == 1 and client.edited == []

    assert n.send_heartbeat(_S()) is True
    assert len(client.sent) == 1, "a second status message must not be sent"
    assert len(client.edited) == 1
    assert client.edited[0]["id"] == 101


def test_an_alert_is_never_overwritten(settings, storage):
    """After an alert the stored id is dropped, so the next status starts a
    fresh message below it instead of editing the alert away."""
    from src.subjects import Subject, WatchEvent

    client = EditingClient()
    n = notifier_with(settings, storage, client)
    n.send_heartbeat(_S())           # status #101
    n.send_heartbeat(_S())           # edited in place
    assert len(client.edited) == 1

    subject = Subject.steam_app(730, "CS2")
    storage.upsert_subjects([subject])
    n.notify([WatchEvent(subject, "depot_public_buildid", "1", "2")], [])

    before = len(client.edited)
    n.send_heartbeat(_S())
    assert len(client.edited) == before, "the alert was edited instead of preserved"
    assert len(client.sent) == 3, "a new status message should follow the alert"


def test_a_refused_edit_falls_back_to_sending(settings, storage):
    """Telegram refuses to edit a message that is gone or older than 48 hours."""
    client = EditingClient(edit_ok=False)
    n = notifier_with(settings, storage, client)
    n.send_heartbeat(_S())
    assert len(client.sent) == 1

    assert n.send_heartbeat(_S()) is True
    assert len(client.edited) == 1      # tried
    assert len(client.sent) == 2        # and then sent instead


def test_the_stored_id_is_cleared_when_an_edit_is_refused(settings, storage):
    client = EditingClient(edit_ok=False)
    n = notifier_with(settings, storage, client)
    n.send_heartbeat(_S())
    first = storage.get_meta(n.HEARTBEAT_MESSAGE_KEY)
    n.send_heartbeat(_S())
    assert storage.get_meta(n.HEARTBEAT_MESSAGE_KEY) != first


def test_status_messages_stay_silent_whether_sent_or_edited(settings, storage):
    client = EditingClient()
    n = notifier_with(settings, storage, client)
    n.send_heartbeat(_S())
    n.send_heartbeat(_S())
    assert all(m["silent"] is True for m in client.sent)


def test_meta_storage_round_trip(storage):
    assert storage.get_meta("nothing") is None
    storage.set_meta("k", "42")
    assert storage.get_meta("k") == "42"
    storage.set_meta("k", "43")
    assert storage.get_meta("k") == "43"
    storage.clear_meta("k")
    assert storage.get_meta("k") is None


def test_a_multi_chunk_status_is_not_editable(settings):
    """editMessageText cannot span chunks, so a long text must not claim an id."""
    import dataclasses

    client = TelegramClient(dataclasses.replace(settings, dry_run=True))
    ok, message_id = client.send_and_get_id("x" * 9000, silent=True)
    assert ok is True and message_id is None
