"""Configuration parsing and the verified default office anchor."""

from __future__ import annotations

import pytest

from src.config import (
    ConfigError,
    DEFAULT_OFFICE_ADDRESS,
    DEFAULT_OFFICE_LAT,
    DEFAULT_OFFICE_LON,
    OfficeConfig,
    env_bool,
    env_float,
    env_int,
    env_list,
    load_settings,
)


def test_default_office_is_the_verified_valve_hq(monkeypatch):
    for key in ("OFFICE_LAT", "OFFICE_LON", "OFFICE_ADDRESS", "SEARCH_RADIUS_METERS"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(None)
    assert "10400 NE 4th St" in DEFAULT_OFFICE_ADDRESS
    assert settings.office.latitude == pytest.approx(DEFAULT_OFFICE_LAT)
    assert settings.office.longitude == pytest.approx(DEFAULT_OFFICE_LON)
    assert settings.office.timezone == "America/Los_Angeles"
    assert settings.office.radius_meters == 2000


def test_office_can_be_relocated_without_code_changes(monkeypatch):
    monkeypatch.setenv("OFFICE_LAT", "52.52")
    monkeypatch.setenv("OFFICE_LON", "13.405")
    monkeypatch.setenv("OFFICE_NAME", "Berlin Office")
    monkeypatch.setenv("OFFICE_TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("SEARCH_RADIUS_METERS", "1500")
    settings = load_settings(None)
    assert settings.office.name == "Berlin Office"
    assert settings.office.latitude == 52.52
    assert settings.office.radius_meters == 1500
    assert settings.office.timezone == "Europe/Berlin"


@pytest.mark.parametrize("lat,lon", [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0)])
def test_invalid_coordinates_are_rejected(lat, lon):
    with pytest.raises(ConfigError):
        OfficeConfig("x", "y", lat, lon, "UTC", 2000).validate()


@pytest.mark.parametrize("radius", [0, 10, 100_000])
def test_invalid_radius_is_rejected(radius):
    with pytest.raises(ConfigError):
        OfficeConfig("x", "y", 47.6, -122.2, "UTC", radius).validate()


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


def test_dry_run_defaults_to_true(monkeypatch):
    """Safe by default: a fresh checkout cannot spam a chat by accident."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert load_settings(None).dry_run is True


def test_providers_are_disabled_without_keys(monkeypatch):
    for key in ("GOOGLE_MAPS_API_KEY", "FOURSQUARE_API_KEY", "BESTTIME_API_KEY_PRIVATE"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(None)
    assert settings.discovery.enable_osm is True
    assert settings.discovery.enable_google is False
    assert settings.discovery.enable_foursquare is False
    assert settings.providers.besttime_private_key is None


def test_thresholds_are_configurable(monkeypatch):
    monkeypatch.setenv("ANOMALY_MULTIPLIER", "2.0")
    monkeypatch.setenv("MIN_BASELINE_SAMPLES", "12")
    monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", "45")
    monkeypatch.setenv("SEND_RECOVERY_ALERTS", "false")
    settings = load_settings(None)
    assert settings.anomaly.multiplier == 2.0
    assert settings.anomaly.min_baseline_samples == 12
    assert settings.alerts.cooldown_minutes == 45
    assert settings.alerts.send_recovery is False


def test_active_window_parsing(monkeypatch):
    monkeypatch.setenv("ACTIVE_HOURS_START", "14")
    monkeypatch.setenv("ACTIVE_HOURS_END", "21")
    monkeypatch.setenv("ACTIVE_WEEKDAYS", "0,1,2,3,4")
    window = load_settings(None).active_window
    assert (window.start_hour, window.end_hour) == (14, 21)
    assert window.weekdays == (0, 1, 2, 3, 4)
    assert window.always_on is False
    assert window.describe() == "14:00-21:00 local (MTWTF)"


@pytest.mark.parametrize(
    "start,end,weekdays",
    [("25", "21", "0,1"), ("14", "-1", "0,1"), ("14", "21", "9")],
)
def test_invalid_active_window_is_rejected(monkeypatch, start, end, weekdays):
    monkeypatch.setenv("ACTIVE_HOURS_START", start)
    monkeypatch.setenv("ACTIVE_HOURS_END", end)
    monkeypatch.setenv("ACTIVE_WEEKDAYS", weekdays)
    with pytest.raises(ConfigError):
        load_settings(None)


def test_unset_weekday_variable_means_every_day(monkeypatch):
    """An unset GitHub Actions variable arrives as an empty string, and must
    fall back to the default rather than failing the run."""
    monkeypatch.setenv("ACTIVE_HOURS_START", "14")
    monkeypatch.setenv("ACTIVE_HOURS_END", "21")
    monkeypatch.setenv("ACTIVE_WEEKDAYS", "")
    window = load_settings(None).active_window
    assert window.weekdays == (0, 1, 2, 3, 4, 5, 6)
    assert window.contains(5, 15) is True


def test_default_active_window_matches_the_shipped_configuration(monkeypatch):
    for key in ("ACTIVE_HOURS_START", "ACTIVE_HOURS_END", "ACTIVE_WEEKDAYS"):
        monkeypatch.delenv(key, raising=False)
    window = load_settings(None).active_window
    assert (window.start_hour, window.end_hour) == (14, 21)
    assert window.always_on is False
