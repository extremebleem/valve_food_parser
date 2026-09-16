"""Message rendering and transport behaviour."""

from __future__ import annotations

import dataclasses

import pytest

from src.models import (
    AnomalyResult,
    Baseline,
    BaselineStatus,
    Direction,
    MetricType,
    Venue,
    utcnow,
)
from src.telegram import (
    AlertGate,
    MessageBuilder,
    Notifier,
    TelegramClient,
    message_hash,
    plural_ru,
    split_message,
)

from .conftest import make_observation


def build_result(venue, score, baseline_median, *, samples=20, metric=None, anomaly=True,
                 direction=None, district=1.0):
    baseline = (
        Baseline(venue.id, metric or "live_busyness_index", 2, 760, samples, baseline_median, 2.0,
                 baseline_median + 5, baseline_median, BaselineStatus.OK)
        if samples
        else None
    )
    observation = make_observation(
        venue.id,
        score,
        metric_type=metric or MetricType.LIVE_BUSYNESS_INDEX.value,
        metric_value=score if metric is None else 55,
    )
    ratio = score / baseline_median if baseline_median else 1.0
    if direction is None:
        direction = Direction.HIGH if anomaly else Direction.NONE
    return AnomalyResult(
        venue=venue,
        observation=observation,
        baseline=baseline,
        is_anomaly=anomaly,
        deviation_ratio=ratio,
        deviation_percent=(ratio - 1) * 100,
        robust_z=-8.0 if direction is Direction.LOW else 8.0,
        status=BaselineStatus.OK if samples else BaselineStatus.NO_DATA,
        direction=direction,
        district_index=district,
        relative_ratio=round(ratio / district, 3) if district else ratio,
    )


def test_single_anomaly_message_has_every_required_field(settings, venue):
    text = MessageBuilder(settings).anomaly(build_result(venue, 87, 54))
    for fragment in (
        "Необычно высокая загрузка",
        "Din Tai Fung",
        "650 м",
        "87/100",
        "54/100",
        "+61%",
        "Источник",
        "Время проверки",
    ):
        assert fragment in text, fragment


def test_message_reports_the_raw_metric_and_its_usual_value(settings, venue):
    text = MessageBuilder(settings).anomaly(
        build_result(venue, 66.7, 28.3, metric=MetricType.DELIVERY_ETA_MINUTES.value)
    )
    assert "Delivery ETA: 55 мин" in text
    assert "Обычно: ~32 мин" in text


def test_message_names_the_congestion_domain(settings, venue):
    text = MessageBuilder(settings).anomaly(build_result(venue, 87, 54))
    assert "Тип сигнала" in text
    assert "proxy" in text


def test_learning_baseline_is_stated_not_faked(settings, venue):
    text = MessageBuilder(settings).anomaly(build_result(venue, 87, 0, samples=0))
    assert "learning_baseline" in text
    assert "Обычно в это время" not in text


def test_aggregate_message_lists_venues_by_deviation(settings):
    results = [
        build_result(Venue(id="a", name="Restaurant A", distance_meters=100), 86, 50),
        build_result(Venue(id="b", name="Restaurant B", distance_meters=200), 76, 50),
        build_result(Venue(id="c", name="Restaurant C", distance_meters=300), 74, 50),
    ]
    text = MessageBuilder(settings).aggregate(results)
    assert "Необычная активность рядом с" in text
    assert text.index("Restaurant A") < text.index("Restaurant B") < text.index("Restaurant C")
    assert "1. Restaurant A" in text and "+72%" in text
    assert "3 заведения" in text


def test_html_is_escaped_in_venue_names(settings):
    venue = Venue(id="x", name="<script>alert(1)</script> & Co", distance_meters=10)
    text = MessageBuilder(settings).anomaly(build_result(venue, 87, 54))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp; Co" in text


def test_recovery_message(settings, venue):
    text = MessageBuilder(settings).recovery(build_result(venue, 58, 50, anomaly=False), peak_score=91)
    assert "Загрузка вернулась к норме" in text
    assert "Было: 91/100" in text
    assert "Сейчас: 58/100" in text


def test_test_message_describes_the_configuration(settings):
    text = MessageBuilder(settings).test_message()
    assert "тестовое сообщение" in text
    assert settings.office.name in text
    assert "47.61425" in text


@pytest.mark.parametrize(
    "count,expected",
    [(1, "заведение"), (2, "заведения"), (4, "заведения"), (5, "заведений"),
     (11, "заведений"), (21, "заведение"), (22, "заведения"), (25, "заведений")],
)
def test_russian_plurals(count, expected):
    assert plural_ru(count, "заведение", "заведения", "заведений") == expected


def test_long_messages_are_split_under_the_telegram_limit():
    text = "\n\n".join("block {}".format(i) * 50 for i in range(200))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "block 0" in chunks[0]


def test_an_oversized_single_block_is_hard_split():
    chunks = split_message("x" * 9000)
    assert len(chunks) == 3
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_short_message_is_not_split():
    assert split_message("hello") == ["hello"]


def test_dry_run_prints_instead_of_sending(settings, capsys):
    client = TelegramClient(dataclasses.replace(settings, dry_run=True))
    assert client.send_message("hello world") is True
    captured = capsys.readouterr()
    assert "DRY_RUN" in captured.out
    assert "hello world" in captured.out
    assert client.sent_count == 0


def test_unconfigured_telegram_does_not_crash_the_run(settings, capsys):
    client = TelegramClient(dataclasses.replace(settings, dry_run=False))
    assert client.configured is False
    assert client.send_message("hello") is True
    assert "not configured" in capsys.readouterr().out


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def test_notifier_aggregates_and_records_state(settings, storage, venue):
    storage.upsert_venues([venue])
    other = Venue(id="other", name="Second Place", distance_meters=300)
    storage.upsert_venues([other])
    client = RecordingClient()
    notifier = Notifier(settings, storage, client=client)

    outcome = notifier.process([build_result(venue, 87, 50), build_result(other, 80, 50)])
    assert outcome["alerts_sent"] == 2
    assert len(client.messages) == 1  # one aggregated message
    assert storage.alert_state(venue.id)["active"] is True

    repeat = notifier.process([build_result(venue, 87, 50), build_result(other, 80, 50)])
    assert repeat["alerts_sent"] == 0
    assert repeat["alerts_suppressed"] == 2


def test_notifier_respects_the_per_run_alert_budget(settings, storage):
    tuned = dataclasses.replace(settings, alerts=dataclasses.replace(settings.alerts, max_alerts_per_run=2))
    venues = [Venue(id="v{}".format(i), name="V{}".format(i), distance_meters=i * 10) for i in range(5)]
    storage.upsert_venues(venues)
    client = RecordingClient()
    outcome = Notifier(tuned, storage, client=client).process(
        [build_result(v, 90 - i, 50) for i, v in enumerate(venues)]
    )
    assert outcome["alerts_sent"] == 2
    assert outcome["alerts_suppressed"] == 3


def test_notifier_sends_a_recovery_after_an_anomaly(settings, storage, venue):
    storage.upsert_venues([venue])
    client = RecordingClient()
    notifier = Notifier(settings, storage, client=client)
    notifier.process([build_result(venue, 91, 50)])
    client.messages.clear()

    outcome = notifier.process([build_result(venue, 52, 50, anomaly=False)])
    assert outcome["recoveries_sent"] == 1
    assert "вернулась к норме" in client.messages[0]
    assert storage.alert_state(venue.id)["active"] is False


# --------------------------------------------------------------------------- #
# unusually quiet + district index
# --------------------------------------------------------------------------- #


def test_quiet_message_states_the_drop_without_claiming_a_cause(settings, venue):
    result = build_result(venue, 24, 62, direction=Direction.LOW)
    text = MessageBuilder(settings).anomaly(result)
    assert "Необычно низкая загрузка" in text
    assert "61% ниже обычного" in text
    assert "24/100" in text
    assert "наблюдение, а не вывод" in text
    # must not assert a cause it cannot know
    for forbidden in ("кранч", "crunch", "заказывают", "доставк"):
        assert forbidden not in text.lower()


def test_message_says_whether_the_district_moved_too(settings, venue):
    local = MessageBuilder(settings).anomaly(build_result(venue, 24, 62, direction=Direction.LOW))
    assert "Район в целом в норме" in local

    district_wide = MessageBuilder(settings).anomaly(
        build_result(venue, 24, 62, direction=Direction.LOW, district=0.55)
    )
    assert "Район: -45%" in district_wide
    assert "Район в целом в норме" not in district_wide


def test_aggregate_splits_the_two_directions(settings):
    results = [
        build_result(Venue(id="a", name="Busy A", distance_meters=100), 86, 50),
        build_result(Venue(id="b", name="Quiet B", distance_meters=20), 20, 60, direction=Direction.LOW),
        build_result(Venue(id="c", name="Quiet C", distance_meters=30), 28, 62, direction=Direction.LOW),
    ]
    text = MessageBuilder(settings).aggregate(results)
    assert "Выше обычного" in text and "Ниже обычного" in text
    assert text.index("Busy A") < text.index("Quiet B")
    assert "3 заведения" in text
    # steepest drop first within its section
    assert text.index("Quiet B") < text.index("Quiet C")


def test_aggregate_with_only_drops_has_no_empty_section(settings):
    results = [
        build_result(Venue(id="b", name="Quiet B", distance_meters=20), 20, 60, direction=Direction.LOW)
    ]
    text = MessageBuilder(settings).aggregate(results)
    assert "Ниже обычного" in text
    assert "Выше обычного" not in text
    assert "1 заведение" in text


def test_a_flip_from_busy_to_quiet_is_a_new_alert_not_a_repeat(storage, venue, settings):
    from src.config import AlertConfig

    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120))
    storage.set_alert_state(
        venue.id, active=True, last_alert_at=utcnow(), last_score=88, direction="high"
    )
    decision = gate.decide(build_result(venue, 22, 60, direction=Direction.LOW))
    assert decision.should_send is True
    assert "direction flipped" in decision.reason


def test_a_deepening_drop_escalates(storage, venue, settings):
    from src.config import AlertConfig

    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120, escalation_delta=15.0))
    storage.set_alert_state(
        venue.id, active=True, last_alert_at=utcnow(), last_score=40, direction="low"
    )
    # 40 -> 20 is a 20-point deepening, past the 15-point escalation threshold
    assert gate.decide(build_result(venue, 20, 62, direction=Direction.LOW)).should_send is True
    # 40 -> 35 is not
    assert gate.decide(build_result(venue, 35, 62, direction=Direction.LOW)).should_send is False


def test_recovery_from_a_drop_means_coming_back_up(storage, venue, settings):
    from src.config import AlertConfig

    gate = AlertGate(storage, AlertConfig(send_recovery=True, recovery_ratio=1.15))
    storage.set_alert_state(
        venue.id, active=True, last_alert_at=utcnow(), last_score=22, peak_score=22, direction="low"
    )
    still_low = build_result(venue, 30, 62, anomaly=False, direction=Direction.NONE)
    assert gate.decide_recovery(still_low).should_send is False
    back_up = build_result(venue, 58, 62, anomaly=False, direction=Direction.NONE)
    assert gate.decide_recovery(back_up).should_send is True


def test_message_hash_separates_directions():
    assert message_hash("v", "anomaly", 40, 60, "high") != message_hash("v", "anomaly", 40, 60, "low")
