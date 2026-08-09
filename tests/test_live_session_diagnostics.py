from pathlib import Path
from io import StringIO
from unittest.mock import patch

from tools.diagnostics import meter_stability_report as stability
from tools.diagnostics import session_report
from tools.timing import live_batch_report


def test_session_report_uses_latest_redacted_marker():
    content = (
        "old\n"
        "Sidecar env: ORION_CAPTURE_CARD=0 ORION_LICENSE_KEY=do-not-copy\n"
        "old shot\n"
        "Sidecar env keys: ORION_CAPTURE_CARD ORION_LICENSE_KEY\n"
        "new shot\n"
    )
    with patch("builtins.open", return_value=StringIO(content)):
        lines, start = session_report.parse_last_session("unused.log")

    assert start == 3
    assert lines == [
        "Sidecar env keys: ORION_CAPTURE_CARD ORION_LICENSE_KEY\n",
        "new shot\n",
    ]


def test_live_batch_report_defaults_to_checked_out_repo():
    expected = Path(__file__).resolve().parents[1]
    assert Path(live_batch_report.ROOT).resolve() == expected
    assert Path(live_batch_report.LOG).resolve() == expected / "logs" / "orion_native.log"


def test_live_batch_report_preserves_multiword_shot_type():
    content = (
        "Release issued: fill 91.9% target 96.0% code=green_confirmed "
        "offset=75ms seq=18 shot=Right Fade\n"
    )
    with patch("builtins.open", return_value=StringIO(content)):
        shots = live_batch_report.parse("unused.log")

    assert shots[18]["shot"] == "Right Fade"


def test_live_batch_report_uses_latest_session_and_requires_exact_local_acceptance():
    content = (
        "Sidecar env keys: FIRST\n"
        "Release issued: fill 20.0% target 90.0% code=old offset=70ms seq=1 shot=Old Shot\n"
        "Release submit: seq=1 ok=1 backend=PIPE delivery_stage=pipe_write_only\n"
        "Sidecar env keys: SECOND\n"
        "Release issued: fill 80.0% target 96.0% code=green_confirmed "
        "offset=12ms seq=1 shot=Right Fade\n"
        "Release submit: seq=1 ok=1 backend=PIPE delivery_stage=local_udp_accepted "
        "console_ack=0\n"
        "Release-window diagnostic (not game outcome): seq=1 position=IN_WINDOW "
        "fill=98.0 window_start=97.0 shot=Right Fade\n"
    )
    with patch("builtins.open", return_value=StringIO(content)):
        shots = live_batch_report.parse("unused.log")

    assert shots[1]["shot"] == "Right Fade"
    assert shots[1]["offset"] == 12.0
    assert shots[1]["delivery_stage"] == "local_udp_accepted"
    assert shots[1]["local_accepted"] is True
    assert shots[1]["console_ack"] is False
    assert shots[1]["window_position"] == "IN_WINDOW"


def test_live_batch_report_legacy_pipe_write_fails_closed():
    content = "Release submit: seq=9 ok=1 backend=PIPE square_bit=0\n"
    with patch("builtins.open", return_value=StringIO(content)):
        shots = live_batch_report.parse("unused.log")

    assert shots[9]["delivery_stage"] == "legacy_unverified"
    assert shots[9]["local_accepted"] is False


def test_live_batch_report_joins_latency_oracle_result_by_native_release_id():
    content = (
        "Sidecar env keys: CURRENT\n"
        "Release issued: fill 46.2% target 90.6% code=latency_calibration_validation "
        "offset=4ms seq=2 shot=Standstill\n"
        "2026-08-01 22:38:02,400 INFO latency_estimator: latency observation: "
        "seq=2 calibration=1 accepted=0 status=rejected_validation_stop_residual "
        "reject=validation_stop_residual total_ms=271.4 sigma_ms=5.0 fixed_ms=217.6 "
        "sd_ms=5.5 n=1 freeze=rise controlled=1 validated=0 target_pct=90.6 "
        "tolerance_pct=3.3 f_stop=98.2 peak=98.4\n"
    )
    with patch("builtins.open", return_value=StringIO(content)):
        shots = live_batch_report.parse("unused.log")

    assert shots[2]["latency_observation"] is True
    assert shots[2]["latency_accepted"] is False
    assert shots[2]["latency_status"] == "rejected_validation_stop_residual"
    assert shots[2]["latency_reject"] == "validation_stop_residual"
    assert shots[2]["latency_n"] == "1"
    assert shots[2]["latency_total_ms"] == "271.4"
    assert shots[2]["latency_target_pct"] == "90.6"
    assert shots[2]["latency_stop_pct"] == "98.2"


def test_live_batch_report_surfaces_shots_that_never_reached_release():
    content = (
        "Sidecar env keys: CURRENT\n"
        "Shot automation aborted: no_meter_abort\n"
        "SHOT NOT OWNED: reason=ownership_proof_incomplete samples=2\n"
        "Shot automation aborted: ownership_proof_incomplete\n"
        "OWNED-SHOT UNRESOLVED: reason=owned_square_detector_unresolved mode=ButtonShot\n"
        "Release issued: fill 88.0% target 100.0% code=live_meter_tip "
        "offset=0ms seq=1 shot=No Dip\n"
        "Release submit: seq=1 ok=1 backend=PIPE delivery_stage=local_udp_accepted\n"
    )
    with patch("builtins.open", return_value=StringIO(content)):
        events = live_batch_report.parse_ownership_events("unused.log")

    assert events == {
        "aborts": ["no_meter_abort", "ownership_proof_incomplete"],
        "unowned": ["ownership_proof_incomplete"],
        "unresolved": ["owned_square_detector_unresolved"],
    }


def test_stability_metrics_detect_shape_warp():
    shot = [
        {"detected": True, "bbox": [10, 10, 30, 160], "fill": 20},
        {"detected": True, "bbox": [11, 10, 30, 160], "fill": 50},
        {"detected": True, "bbox": [12, 10, 60, 80], "fill": 90},
        {"detected": True, "bbox": [13, 10, 30, 160], "fill": 100},
    ]

    metrics = stability.shot_metrics(shot)

    assert metrics["w_cv"] > stability.MAX_W_CV
    assert metrics["h_cv"] > stability.MAX_H_CV
    assert metrics["aspect_step_max"] > stability.MAX_ASPECT_STEP
