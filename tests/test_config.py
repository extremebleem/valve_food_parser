"""Configuration parsing."""

from __future__ import annotations

import pytest

from src.config import (
    ConfigError,
    env_bool,
    env_float,
    env_int,
    env_list,
    load_settings,
)


def test_defaults_are_safe(monkeypatch):
    for key in ("DRY_RUN", "DATABASE_URL", "TIMEZONE"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(None)
    # a fresh checkout cannot spam a chat by accident
    assert settings.dry_run is True
    assert settings.timezone == "America/Los_Angeles"
    assert settings.database_url.startswith("sqlite://")
    assert settings.telegram.configured is False


def test_thresholds_are_configurable(monkeypatch):
    monkeypatch.setenv("RATE_MULTIPLIER", "4.0")
    monkeypatch.setenv("MIN_BASELINE_SAMPLES", "12")
    monkeypatch.setenv("BASELINE_LOOKBACK_WEEKS", "6")
    monkeypatch.setenv("RATE_MIN_ABSOLUTE_DELTA", "8")
    settings = load_settings(None)
    assert settings.anomaly.multiplier == 4.0
    assert settings.anomaly.min_baseline_samples == 12
    assert settings.anomaly.lookback_weeks == 6
    assert settings.anomaly.min_absolute_delta == 8.0


def test_telegram_is_configured_only_with_both_halves(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert load_settings(None).telegram.configured is False
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    assert load_settings(None).telegram.configured is True


def test_timezone_is_overridable(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    assert load_settings(None).timezone == "Europe/Berlin"


@pytest.mark.parametrize("raw,expected", [("true", True), ("YES", True), ("0", False), ("off", False)])
def test_env_bool(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert env_bool("SOME_FLAG") is expected


def test_env_bool_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "maybe")
    with pytest.raises(ConfigError):
        env_bool("SOME_FLAG")


def test_numeric_parsing(monkeypatch):
    monkeypatch.setenv("N", "42")
    monkeypatch.setenv("F", "1.5")
    monkeypatch.setenv("BAD", "abc")
    assert env_int("N", 0) == 42
    assert env_float("F", 0.0) == 1.5
    assert env_int("MISSING", 7) == 7
    with pytest.raises(ConfigError):
        env_int("BAD", 0)
    with pytest.raises(ConfigError):
        env_float("BAD", 0.0)


def test_env_list(monkeypatch):
    monkeypatch.setenv("L", " a , b ,, c ")
    assert env_list("L") == ["a", "b", "c"]
    assert env_list("MISSING", "x,y") == ["x", "y"]
    assert env_list("MISSING") == []


def test_unset_variable_falls_back_to_the_default(monkeypatch):
    """An unset GitHub Actions variable arrives as an empty string and must not
    fail the run."""
    monkeypatch.setenv("MIN_BASELINE_SAMPLES", "")
    monkeypatch.setenv("TIMEZONE", "")
    settings = load_settings(None)
    assert settings.anomaly.min_baseline_samples == 7
    assert settings.timezone == "America/Los_Angeles"


def test_the_workflow_sets_no_environment_variable_the_code_ignores():
    """Three variables went stale when the project pivoted -- OFFICE_TIMEZONE,
    ANOMALY_MULTIPLIER and a mention read from the wrong tab. Each was set in
    the workflow and silently ignored by the code, which is the worst kind of
    misconfiguration: nothing fails, the setting just does nothing.
    """
    import os
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "watch.yml").read_text()
    config = (root / "src" / "config.py").read_text()
    code = "\n".join(p.read_text() for p in (root / "src").rglob("*.py"))

    declared = set(re.findall(r"^\s{10}([A-Z][A-Z0-9_]+):", workflow, re.M))
    read = set(re.findall(r'env_(?:str|bool|int|float|list)\("([A-Z0-9_]+)"', config))
    read |= set(re.findall(r'environ\.get\("([A-Z0-9_]+)"', code))
    # consumed by the runtime or the harness rather than by our own config
    read |= {"GITHUB_TOKEN", "GH_TOKEN", "PYTHONUNBUFFERED"}

    assert declared, "no env block found -- has the workflow layout changed?"
    assert sorted(declared - read) == [], "workflow sets variables nothing reads"
    assert os.sep  # keep the import meaningful on every platform
