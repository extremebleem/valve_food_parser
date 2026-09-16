"""De-duplication on both axes: venue records and Telegram alerts."""

from __future__ import annotations

from datetime import timedelta


from src.config import AlertConfig
from src.discovery import VenueMerger, name_similarity
from src.models import AlertKind, AlertRecord, AnomalyResult, Venue, make_venue_id, utcnow
from src.telegram import AlertGate, message_hash

from .conftest import make_observation


def make_venue(name, lat, lon, source, **kwargs):
    return Venue(
        id=make_venue_id(name, lat, lon),
        name=name,
        latitude=lat,
        longitude=lon,
        sources=[source],
        **kwargs
    )


# --------------------------------------------------------------------------- #
# venue de-duplication
# --------------------------------------------------------------------------- #


def test_same_venue_from_two_sources_is_merged():
    merger = VenueMerger()
    merged = merger.merge(
        [
            make_venue("Din Tai Fung", 47.6165, -122.2010, "osm", takeaway=True),
            make_venue(
                "Din Tai Fung Bellevue",
                47.61651,
                -122.20102,
                "google_places",
                address="700 Bellevue Way NE",
                delivery=True,
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].sources == ["google_places", "osm"]
    assert merged[0].delivery is True and merged[0].takeaway is True
    assert merged[0].address == "700 Bellevue Way NE"


def test_two_branches_of_a_chain_stay_separate():
    merger = VenueMerger()
    merged = merger.merge(
        [
            make_venue("Starbucks", 47.6100, -122.2000, "osm"),
            make_venue("Starbucks", 47.6150, -122.2050, "osm"),
        ]
    )
    assert len(merged) == 2


def test_accents_and_spelling_variants_collapse():
    merger = VenueMerger()
    merged = merger.merge(
        [
            make_venue("Fogo de Chão", 47.61460, -122.20080, "osm"),
            make_venue("Fogo de Chao", 47.61462, -122.20079, "foursquare"),
        ]
    )
    assert len(merged) == 1


def test_shared_source_id_forces_a_merge_even_when_names_differ():
    left = make_venue("Coffee Place", 47.6100, -122.2000, "osm")
    right = make_venue("Totally Different Name", 47.6101, -122.2001, "google_places")
    left.source_ids = {"google_places": "abc123"}
    right.source_ids = {"google_places": "abc123"}
    assert VenueMerger().are_duplicates(left, right) is True


def test_merged_id_is_order_independent():
    merger = VenueMerger()
    a = make_venue("Din Tai Fung", 47.6165, -122.2010, "osm")
    b = make_venue("Din Tai Fung Bellevue", 47.61651, -122.20102, "google_places")
    assert merger.merge([a, b])[0].id == merger.merge([b, a])[0].id


def test_most_specific_category_wins():
    merged = VenueMerger().merge(
        [
            make_venue("Pagliacci", 47.6100, -122.2000, "osm", category="pizzeria"),
            make_venue("Pagliacci", 47.61001, -122.20001, "google_places", category="restaurant"),
        ]
    )
    assert merged[0].category == "pizzeria"


def test_empty_input_is_handled():
    assert VenueMerger().merge([]) == []


def test_name_similarity_edges():
    assert name_similarity("", "anything") == 0.0
    assert name_similarity("Din Tai Fung", "din tai fung") == 1.0
    assert name_similarity("Din Tai Fung", "Sushi Kashiba") < 0.5


# --------------------------------------------------------------------------- #
# Telegram alert de-duplication / cooldown
# --------------------------------------------------------------------------- #


def result_for(venue, score, baseline_score, *, anomaly=True):
    from src.models import Baseline, BaselineStatus

    baseline = Baseline(
        venue_id=venue.id,
        metric_type="live_busyness_index",
        weekday=2,
        minutes=760,
        sample_count=20,
        median=baseline_score,
        mad=2.0,
        p90=baseline_score + 5,
        mean=baseline_score,
        status=BaselineStatus.OK,
    )
    return AnomalyResult(
        venue=venue,
        observation=make_observation(venue.id, score),
        baseline=baseline,
        is_anomaly=anomaly,
        deviation_ratio=score / max(baseline_score, 1e-9),
        deviation_percent=(score / max(baseline_score, 1e-9) - 1) * 100,
        robust_z=8.0,
    )


def record(storage, venue_id, score, baseline, minutes_ago, *, active=True, kind=AlertKind.ANOMALY.value):
    sent_at = utcnow() - timedelta(minutes=minutes_ago)
    storage.record_alert(
        AlertRecord(
            venue_id=venue_id,
            kind=kind,
            load_score=score,
            baseline_score=baseline,
            deviation_percent=(score / baseline - 1) * 100,
            metric_type="live_busyness_index",
            sent_at=sent_at,
            message_hash=message_hash(venue_id, kind, score, baseline),
        )
    )
    storage.set_alert_state(
        venue_id,
        metric_type="live_busyness_index",
        active=active,
        last_alert_at=sent_at,
        last_score=score,
        last_deviation=(score / baseline - 1) * 100,
        peak_score=score,
    )


def test_first_anomaly_is_always_sent(storage, venue):
    gate = AlertGate(storage, AlertConfig())
    decision = gate.decide(result_for(venue, 78, 46))
    assert decision.should_send is True
    assert decision.reason == "new anomaly"


def test_non_anomaly_is_never_sent(storage, venue):
    gate = AlertGate(storage, AlertConfig())
    assert gate.decide(result_for(venue, 50, 46, anomaly=False)).should_send is False


def test_repeat_within_cooldown_is_suppressed(storage, venue):
    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120))
    record(storage, venue.id, 78, 46, minutes_ago=20)
    decision = gate.decide(result_for(venue, 78, 46))
    assert decision.should_send is False
    assert "duplicate" in decision.reason or "cooldown active" in decision.reason


def test_repeat_after_cooldown_is_sent(storage, venue):
    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120))
    record(storage, venue.id, 78, 46, minutes_ago=121)
    decision = gate.decide(result_for(venue, 78, 46))
    assert decision.should_send is True
    assert "cooldown elapsed" in decision.reason


def test_escalation_breaks_the_cooldown(storage, venue):
    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120, escalation_delta=15.0))
    record(storage, venue.id, 78, 46, minutes_ago=20)
    decision = gate.decide(result_for(venue, 95, 46))
    assert decision.should_send is True
    assert decision.escalated is True


def test_small_worsening_does_not_break_the_cooldown(storage, venue):
    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120, escalation_delta=15.0))
    record(storage, venue.id, 78, 46, minutes_ago=20)
    assert gate.decide(result_for(venue, 85, 46)).should_send is False


def test_recovered_then_anomalous_again_re_arms(storage, venue):
    gate = AlertGate(storage, AlertConfig(cooldown_minutes=120, rearm_minutes=30))
    record(storage, venue.id, 78, 46, minutes_ago=45, active=False)
    decision = gate.decide(result_for(venue, 80, 46))
    assert decision.should_send is True
    assert decision.reason == "new anomaly"


def test_flapping_inside_the_rearm_window_is_suppressed(storage, venue):
    gate = AlertGate(storage, AlertConfig(rearm_minutes=30))
    record(storage, venue.id, 78, 46, minutes_ago=5, active=False)
    decision = gate.decide(result_for(venue, 80, 46))
    assert decision.should_send is False
    assert "re-arm" in decision.reason


def test_recovery_requires_an_active_alert(storage, venue):
    gate = AlertGate(storage, AlertConfig(send_recovery=True))
    assert gate.decide_recovery(result_for(venue, 50, 46, anomaly=False)).should_send is False
    record(storage, venue.id, 91, 46, minutes_ago=60)
    assert gate.decide_recovery(result_for(venue, 50, 46, anomaly=False)).should_send is True


def test_recovery_needs_the_score_to_actually_come_down(storage, venue):
    gate = AlertGate(storage, AlertConfig(send_recovery=True, recovery_ratio=1.15))
    record(storage, venue.id, 91, 46, minutes_ago=60)
    assert gate.decide_recovery(result_for(venue, 70, 46, anomaly=False)).should_send is False


def test_recovery_can_be_disabled(storage, venue):
    gate = AlertGate(storage, AlertConfig(send_recovery=False))
    record(storage, venue.id, 91, 46, minutes_ago=60)
    assert gate.decide_recovery(result_for(venue, 50, 46, anomaly=False)).should_send is False


def test_message_hash_buckets_small_wobbles_together():
    # 86 and 88 fall in the same 5-point bin
    assert message_hash("v1", "anomaly", 86, 54) == message_hash("v1", "anomaly", 88, 54)
    assert message_hash("v1", "anomaly", 87, 54) != message_hash("v1", "anomaly", 97, 54)
    assert message_hash("v1", "anomaly", 87, 54) != message_hash("v2", "anomaly", 87, 54)
    assert message_hash("v1", "anomaly", 87, 54) != message_hash("v1", "recovery", 87, 54)
