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
