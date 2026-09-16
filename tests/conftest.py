"""Shared fixtures. Everything runs against an in-memory SQLite database."""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings  # noqa: E402
from src.storage import create_storage  # noqa: E402


@pytest.fixture
def settings():
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DATABASE_URL", "GITHUB_TOKEN"):
        os.environ.pop(key, None)
    base = load_settings(None)
    return dataclasses.replace(base, dry_run=True, database_url="sqlite://:memory:")


@pytest.fixture
def storage():
    store = create_storage("sqlite://:memory:")
    store.connect()
    store.migrate()
    yield store
    store.close()
