from datetime import datetime, timezone

import pytest

from tools.timing.shot_pipeline_audit import audit, distribution, identity


T = 1789694889000.0


def line(offset, payload):
    stamp = datetime.fromtimestamp((T + offset) / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"{stamp}  {payload}"


def record(epoch=1, outcome="released", banner="EXCELLENT"):
    return dict(session="one", epoch=epoch, press_ts_ms=T, closed_ts_ms=T + 2000,
                outcome=outcome, banner={"timing": banner}, disarm_reason=None)


def release(epoch=1, seq=1, offset=1000):
    return line(offset, f"Release delivery identity: physical_epoch={epoch} release_seq={seq}")


def test_session_fence_rejects_previous_session_reused_ids():
    logs = [release(offset=-10000), line(-10000, "Scheduled fire: seq=1 deltaMs=99"),
            release(), line(1000, "Scheduled fire: seq=1 deltaMs=0.2 aligned_ms=100"),
            line(1000, "Precise dispatch timing: seq=1 command_issued_ms=100.25 active_route_wait_ms=0.3")]
    result = audit([record()], logs)
    assert result["counts"]["joined_releases"] == 1
    assert result["distributions"]["scheduler_error_ms_rounded"]["mean"] == .2
    assert result["shots"][0]["measurements"]["command_minus_deadline_ms"] == .25


def test_unknown_or_conflicting_identity_never_guesses_join():
    logs = [line(1000, "Scheduled fire: seq=1 deltaMs=5")]
    assert not audit([record()], logs)["distributions"]
    result = audit([record(1), record(2)], [release(1), release(2), *logs])
    assert result["counts"]["ambiguous_release_sequences"] == [1]
    assert not result["distributions"]


def test_two_release_sequences_for_one_press_do_not_choose_last_writer():
    result = audit([record()], [release(seq=1), release(seq=2),
                              line(1000, "Scheduled fire: seq=2 deltaMs=7")])
    assert result["counts"]["ambiguous_release_sequences"] == [1, 2]
    assert not result["distributions"]


def test_duplicate_lines_are_idempotent_but_conflicting_stage_is_unmeasured():
    event = line(1000, "Scheduled fire: seq=1 deltaMs=1")
    result = audit([record()], [release(), event, event])
    assert result["distributions"]["scheduler_error_ms_rounded"]["n"] == 1
    result = audit([record()], [release(), event, line(1001, "Scheduled fire: seq=1 deltaMs=2")])
    assert not result["distributions"]
    assert result["shots"][0]["ambiguous_stages"] == ["schedule"]


def test_unvalidated_packet_timing_is_not_a_zero_latency_measurement():
    result = audit([record()], [release(), line(1000, "Precise dispatch timing: seq=1 pipe_write_us=0 pipe_write_us_valid=0 pipe_ack_wait_us=nan pipe_ack_wait_us_valid=1")])
    assert not result["distributions"]


def test_uint64_epoch_is_not_rounded():
    epoch = 2**53 + 1
    result = audit([record(epoch)], [release(epoch)])
    assert result["shots"][0]["epoch"] == epoch
    assert result["counts"]["joined_releases"] == 1
    assert identity(True) is None
    assert identity("1.0") is None


def test_outcomes_and_disarms_are_distinct_and_missing_banner_is_unknown():
    records = [record(1), record(2, banner=None), record(3, outcome="disarmed", banner=None)]
    records[-1]["disarm_reason"] = "square_early_release"
    result = audit(records, [])
    assert result["counts"]["release_banners"] == {"EXCELLENT": 1, "unknown": 1}
    assert result["counts"]["disarm_reasons"] == {"square_early_release": 1}
    assert result["shots"][1]["preceding_excellent_releases"] == 1
    assert result["shots"][2]["preceding_excellent_releases"] == 0


@pytest.mark.parametrize("records", [[], [record(), record()], [dict(record(), session="one"), dict(record(2), session="two")]])
def test_rejects_ambiguous_session_input(records):
    with pytest.raises(ValueError):
        audit(records, [])


def test_distribution_excludes_nonfinite_and_does_not_invent_missing_values():
    assert distribution([None, float("nan"), float("inf")])["mean"] is None
    d = distribution([-1, 0, 1])
    assert d["mean"] == 0
    assert d["stddev"] == pytest.approx((2 / 3)**.5)
    assert d["p95"] == pytest.approx(.9)


def test_past_target_and_ruler_resets_join_before_a_release_sequence_exists():
    result = audit([record()], [
        line(500, "TIP SAMPLER RULER RESET: physical_epoch=1 old_generation=2 new_generation=3"),
        line(600, "TIP SAMPLER RULER RESET: physical_epoch=1 old_generation=3 new_generation=4"),
        line(900, "TIP DEADLINE DECISION: physical_epoch=1 lateness_ms=44.461"),
    ])
    row = result["shots"][0]
    assert row["ruler_resets"] == 2
    assert row["measurements"]["late_decision_ms"] == 44.461
    assert not row["ambiguous_stages"]
    assert "command_minus_deadline_ms" not in row["measurements"]


def test_reported_green_open_late_requires_release_identity_not_text_alone():
    r = record(banner="LATE")
    r["banner"]["coverage"] = "WIDE OPEN"
    logs = [release(),
            line(1000, "Release issued: fill 42.0% target 100.0% (green 92.0-96.0) seq=1"),
            line(1000, "Release attribution: seq=1 greenConfirmed=1 targetMode=meter_tip_phase greenWidth=4.0")]
    result = audit([r], logs)
    row = result["shots"][0]
    assert row["release_green_confirmed"] is True
    assert row["release_green_window_pct"] == [92.0, 96.0]
    assert result["counts"]["reported_green_open_late_epochs"] == [1]
    result = audit([r], logs[1:])
    assert result["shots"][0]["release_green_confirmed"] is None
    assert result["counts"]["reported_green_open_late_epochs"] == []


def test_first_valid_tip_evaluation_is_distinct_from_late_fire_and_onset():
    r = record(banner="LATE")
    r["onset_ms"] = 500.0
    logs = [line(600, "TIP RESERVATION: disposition=reservation_created source=registration "
                       "valid=0 command_eta_ms=-38.0 fill_pct=40.6 physical_epoch=1"),
            line(660, "TIP RESERVATION: disposition=reservation_updated source=phase_firstsight "
                       "valid=1 command_eta_ms=-34.0 fill_pct=44.1 lead_ms=279.0 frame_age_ms=22.3 physical_epoch=1"),
            line(660, "TIP DEADLINE DECISION: disposition=fired_late source=phase_firstsight "
                       "lateness_ms=34.0 fill_pct=44.1 physical_epoch=1"), release(offset=665)]
    row = audit([r], logs)["shots"][0]
    assert row["first_valid_tip_evaluation"] == {
        "after_press_ms": 660.0, "after_onset_ms": 160.0,
        "command_eta_ms": -34.0, "ahead_of_deadline": False,
        "fill_pct": 44.1, "lead_ms": 279.0, "frame_age_ms": 22.3,
        "source": "phase_firstsight", "disposition": "reservation_updated"}
    assert row["late_decision"]["disposition"] == "fired_late"
    assert row["late_decision"]["lateness_ms"] == 34.0


def test_first_valid_future_reservation_is_not_conflated_with_a_later_fired_late():
    row = audit([record()], [
        line(600, "TIP RESERVATION: disposition=reservation_created source=phase "
                  "valid=1 command_eta_ms=54.0 fill_pct=20.0 physical_epoch=1"),
        line(800, "TIP DEADLINE DECISION: disposition=fired_late lateness_ms=7.0 physical_epoch=1"),
        release(offset=801),
    ])["shots"][0]
    assert row["first_valid_tip_evaluation"]["ahead_of_deadline"] is True
    assert row["late_decision"]["disposition"] == "fired_late"


def test_ownership_proof_census_and_geometry_restarts_are_epoch_fenced():
    r = record(outcome="disarmed", banner=None)
    r["disarm_reason"] = "ownership_proof_incomplete"
    logs = [line(-500, "SHOT NOT OWNED: reason=ownership_proof_incomplete samples=99 physical_epoch=1"),
            line(700, "SHOT NOT OWNED: reason=ownership_proof_incomplete samples=2 "
                      "first_fill=17.4 last_fill=44.4 restarts=8 break_geometry=8 "
                      "break_anchor=0 unstamped=0 stamp_epoch_seen=0 physical_epoch=1")]
    row = audit([r], logs)["shots"][0]
    assert row["ownership_proof"] == {"reason": "ownership_proof_incomplete", "samples": 2,
                                       "first_fill_pct": 17.4, "last_fill_pct": 44.4,
                                       "restarts": 8, "break_geometry": 8,
                                       "break_anchor": 0, "unstamped": 0,
                                       "stamp_epoch_seen": 0}


def test_missing_or_conflicting_tip_evidence_stays_unknown():
    logs = [line(600, "TIP RESERVATION: disposition=reservation_created source=phase "
                      "valid=1 command_eta_ms=50 fill_pct=20 physical_epoch=1"),
            line(600, "TIP RESERVATION: disposition=reservation_created source=phase "
                      "valid=1 command_eta_ms=10 fill_pct=40 physical_epoch=1")]
    assert audit([record()], logs)["shots"][0]["first_valid_tip_evaluation"] is None
    assert audit([record()], [line(-500, "TIP DEADLINE DECISION: disposition=fired_late "
                                             "lateness_ms=100 physical_epoch=1")])["shots"][0]["late_decision"] is None
