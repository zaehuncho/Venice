"""tools/timing/epoch_table.py joins one shot's log lines into one row and categorises the residual.

The fixture is a minimal transcript in the production line format (2026-09-01 build): two
released shots, one deadline-missed abort, and one pre-ownership no-fire. What is pinned:
  * the join across epoch/seq (reservation -> release -> landing -> outcome -> abort);
  * the local-linear grade sensitivity against each shot's OWN window;
  * the fire-time vs post-command split (fill_at_rel identical, landing different);
  * the abort attribution to its ownership first_fill.
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "timing"))

import epoch_table  # noqa: E402

D = "2026-09-01T15:39:"


def _shot(t0: int, epoch: int, seq: int, shot: str, far: float, peak: float, gs: float, ge: float, vel: float,
          first_fill: float, latency_accepted: int = 1, dev_offset: float | None = None) -> str:
    lines = [
        f"{D}{t0:02d}.000Z  Physical shot epoch: epoch={epoch} intent=square_edge route=RawInput",
        f"{D}{t0:02d}.700Z  Evidence-backed ownership: mode=3 press_age=712.0ms first_fill={first_fill} current_fill=7.5 epoch={epoch}",
        f"{D}{t0:02d}.720Z  TIP PHASE ANCHOR CONSENSUS: disposition=proposed stage=30 proposal=1 token=5 correction_ms=-0.400 armed_eta_ms=40.0 refined_deadline_eta_ms=39.6 w20_ms=100.0 w25_ms=100.5 w30_ms=99.6 w35_ms=-1.000 witness_range_ms=0.900",
        f"{D}{t0:02d}.721Z  PRECISE FIRE RETARGET: disposition=retargeted stage=30 proposal=1 token=5 eta_ms=39.6",
        f"{D}{t0:02d}.730Z  TIP RESERVATION: disposition=reservation_promoted source=phase command_eta_ms=96.0 lead_kind=validated lead_ms=300.000 predictor_sigma_ms=15.101 fill_pct=21.0 physical_epoch={epoch} shot_attempt={epoch - 1} schedule_token=5 reservation_age_ms=60.0 reservation_updates=9 first_fill={first_fill} arm_site=subtick",
    ]
    if dev_offset is not None:
        lines.append(
            f"{D}{t0:02d}.735Z  DEV FIRE OFFSET DRAW: shot_attempt={epoch - 1} offset_ms={dev_offset:.2f}"
        )
        if dev_offset != 0.0:
            lines.append(
                f"{D}{t0:02d}.736Z  DEV FIRE OFFSET APPLIED: base_deadline_eta_ms=40.0 "
                f"applied_ms={dev_offset:.2f} shot_attempt={epoch - 1}"
            )
    lines.extend([
        f"{D}{t0:02d}.830Z  Release issued: fill {far}% target 100.0% (green 93.2-96.0) plan=Release scheduled reason=release_scheduled / live_meter_tip code=live_meter_tip presence=accepted src=sidecar-fusion conf=1.00 age=10ms offset=3.6ms seq={seq} shot={shot}",
        f"{D}{t0:02d}.830Z  Release timing: seq={seq} code=live_meter_tip anchorAppearMs=-30 appearToRelMs=160 holdToRelMs=150 plannedClockMs=-1 fillAtRel={far} peakFill={far} shot={shot}",
        f"{D}{t0:02d}.830Z  Scheduled fire: seq={seq} scheduled=1 deltaMs=0.31 wifi=0 jitterMs=0.0 heldOffsetMs=3.6 clockVisionDivMs=0.0",
        f"{D}{t0:02d}.830Z  Release submit: seq={seq} ok=1 backend=PIPE square_bit=0 hook_write_us=7 hook_ack_wait_us=271",
        f"{D}{t0:02d}.830Z  Release delivery identity: physical_epoch={epoch} shot_attempt={epoch - 1} release_seq={seq} schedule_token=5 delivery_token=5 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0",
        f"{D}{t0 + 1:02d}.100Z  Sidecar: 2026-09-01 10:39:{t0 + 1:02d},100 INFO latency_estimator: latency observation: seq={seq} calibration=0 accepted={latency_accepted} status=ready reject=- total_ms=221.0 sigma_ms=5.5 fixed_ms=222.5 sd_ms=5.4 n=9 corr=0 freeze=rise f_stop={peak} peak={peak}",
        f"{D}{t0 + 2:02d}.000Z  PHASE SAMPLE: raw_ms=328.0 normalized_ms=328.0 anchor_pct=20.0 accepted=1 band_ms=240..430 shipped_const_ms=451.3 effective_const_ms=421.3 stop_subframe=1 stop_snap_shift_ms=2.33 shot_type={shot}",
        f"{D}{t0 + 2:02d}.000Z  Release landing: seq={seq} graded=1 peak_fill={peak} settled_fill={peak - 1.0} fill_at_rel={far} travel_pp={peak - far:.2f} green_start={gs} green_end={ge} green_center={(gs + ge) / 2:.2f} settled_n=60 green_obs_n=60 green_obs_start={gs} green_obs_width={ge - gs:.2f} vel_at_rel={vel} frame_age_ms=10.3 rtt_ms=3.59 flick_ms=-1.0 flick_hold=-1.0 meter_x=0.41 meter_y=0.56 meter_jump=0.004 shot={shot}",
        f"{D}{t0 + 2:02d}.020Z  Outcome identity: physical_epoch={epoch} shot_attempt={epoch - 1} release_seq={seq} verdict=ungraded-artifact(EARLY) grader_truth=0 armed_source=phase armed_sigma_ms=15.1 armed_fill_pct=24.5 command_eta_ms=96.0 armed_schedule_token=5",
    ])
    return "\n".join(lines)


def _abort(t0: int, epoch: int, first_fill: float) -> str:
    return "\n".join([
        f"{D}{t0:02d}.000Z  Physical shot epoch: epoch={epoch} intent=square_edge route=RawInput",
        f"{D}{t0:02d}.940Z  Evidence-backed ownership: mode=3 press_age=940.4ms first_fill={first_fill} current_fill=39.6 epoch={epoch}",
        f"{D}{t0:02d}.948Z  TIP RESERVATION: disposition=reservation_canceled reason=live_tip_deadline_missed physical_epoch={epoch} shot_attempt={epoch - 1} updates=1 promoted=0 ever_valid=1 fire_at_ms=1.0",
        f"{D}{t0:02d}.948Z  SHOT NOT OWNED: reason=live_tip_deadline_missed physical_epoch={epoch} hold_ms=948.0 shot_type=Standstill",
        f"{D}{t0:02d}.948Z  Shot abort identity: physical_epoch={epoch} shot_attempt={epoch - 1} release_seq=0 schedule_token=0 reason=live_tip_deadline_missed site=abort meter_x=0.44 meter_y=0.50 meter_jump=0.0014 shot_type=Standstill",
    ])


def _not_owned(t0: int, epoch: int) -> str:
    return "\n".join([
        f"{D}{t0:02d}.000Z  Physical shot epoch: epoch={epoch} intent=square_edge route=RawInput",
        f"{D}{t0:02d}.140Z  SHOT NOT OWNED: reason=press_unanswered_no_meter physical_epoch={epoch} "
        "hold_ms=140.0 shot_type=Standstill unstamped=0 stale_samples=0 stamp_epoch_seen=0 "
        "lead_ready=1 sq_latch=0 output=pass_through",
    ])


@pytest.fixture
def transcript() -> str:
    # Same command phase (fill_at_rel 38.0) for both shots; different landings. vel 0.2 %/ms
    # makes the ms arithmetic exact: peak 94.0 vs window [95.0, 97.0] -> 5 ms EARLY of the
    # bottom, 15 ms early of the tip; peak 96.0 in [95.5, 98.0] -> GREEN.
    return "\n".join([
        "2026-08-31T23:59:59.000Z  Release landing: seq=9 graded=1 peak_fill=1 (previous day, must be ignored)",
        _shot(1, 2, 1, "Standstill", 38.0, 94.0, 95.0, 97.0, 0.2, 7.4,
              dev_offset=0.0),
        _shot(10, 3, 2, "Left Fade", 38.0, 96.0, 95.5, 98.0, 0.2, 12.0,
              latency_accepted=0, dev_offset=15.0),
        _abort(20, 4, 33.0),
        _not_owned(22, 5),
    ]) + "\n"


def test_join_grade_and_split(transcript):
    graded, aborts, released = epoch_table.parse(io.StringIO(transcript), "2026-09-01")
    assert len(released) == 2 and len(graded) == 2 and len(aborts) == 2
    ss, lf = graded
    assert (ss["shot"], ss["epoch"], ss["seq"], ss["session"]) == ("Standstill", 2, 1, 1)
    assert ss["grade"] == "EARLY" and lf["grade"] == "GREEN"
    assert ss["e_bottom_ms"] == pytest.approx(-5.0) and ss["e_tip_ms"] == pytest.approx(-15.0)
    assert ss["win_ms"] == pytest.approx(10.0)
    # joined fields from the epoch side
    assert ss["armed_source"] == "phase" and ss["arm_source"] == "phase"
    assert ss["first_fill"] == pytest.approx(7.4) and ss["own_first_fill"] == pytest.approx(7.4)
    assert ss["retargeted30"] is True and ss["c30_corr"] == pytest.approx(-0.4)
    assert ss["d25_20"] == pytest.approx(0.5) and ss["d30_25"] == pytest.approx(-0.9)
    assert ss["sched_token"] == ss["deliv_token"] == 5 and ss["deliv_stage"] == "local_udp_accepted"
    assert ss["deltaMs"] == pytest.approx(0.31) and ss["hook_ack_wait_us"] == pytest.approx(271)
    assert ss["phase_norm"] == pytest.approx(328.0) and ss["phase_acc"] == 1
    assert ss["lat_total_ms"] == pytest.approx(221.0)
    assert ss["lat_accepted"] == 1 and lf["lat_accepted"] == 0
    assert ss["dev_offset_draw_ms"] == pytest.approx(0.0)
    assert ss["dev_offset_applied_ms"] == pytest.approx(0.0)
    assert lf["dev_offset_draw_ms"] == pytest.approx(15.0)
    assert lf["dev_offset_applied_ms"] == pytest.approx(15.0)
    # the fire-time vs post-command split: identical command phase, different landing
    assert ss["far"] == lf["far"] == pytest.approx(38.0)
    assert ss["travel"] == pytest.approx(56.0) and lf["travel"] == pytest.approx(58.0)
    # the abort carries the ownership first_fill that made it unschedulable
    a = aborts[0]
    assert a["terminal_kind"] == "owned_abort"
    assert a["reason"] == "live_tip_deadline_missed" and a["epoch"] == 4
    assert a["own_first_fill"] == pytest.approx(33.0) and a["ever_armed"] is False
    not_owned = aborts[1]
    assert not_owned["terminal_kind"] == "not_owned"
    assert not_owned["reason"] == "press_unanswered_no_meter" and not_owned["epoch"] == 5
    assert not_owned["hold_ms"] == pytest.approx(140.0)


def test_report_runs_and_names_the_categories(transcript, capsys):
    graded, aborts, released = epoch_table.parse(io.StringIO(transcript), "2026-09-01")
    epoch_table.report(graded, aborts, released)
    out = capsys.readouterr().out
    for tag in ["(a) BIAS", "(b) ESTIMATOR", "(c) ANCHOR", "(d) COMMAND PATH",
                "(f) IDENTITY", "LOCAL-LINEAR SENSITIVITY"]:
        assert tag in out
    assert "deadline_missed n=1" in out
    assert "sidecar command->freeze (accepted): n=1" in out
    assert "sidecar command->freeze (rejected): n=1" in out
    assert "4 no-fire terminals (1 owned aborts, 1 not-owned)" not in out
    assert "2 no-fire terminals (1 owned aborts, 1 not-owned)" in out
    assert "press_unanswered_no_meter=1" in out
    assert "all no-fire share: 2/4" in out
    assert "deadline-missed share: 1/4" in out
    assert "DEV DEADLINE A/B" in out
    assert "offset=+0.00ms" in out and "offset=+15.00ms" in out
    assert "near-tip deceleration" in out


def test_identity_report_includes_ungraded_releases(transcript, capsys):
    graded, aborts, released = epoch_table.parse(io.StringIO(transcript), "2026-09-01")
    released.append({
        "sched_token": 7,
        "deliv_token": 8,
        "armed_token": 7,
        "deliv_stage": "local_udp_accepted",
        "own_first_fill": 9.0,
    })
    epoch_table.report(graded, aborts, released)
    out = capsys.readouterr().out
    assert "sched!=deliv token: 1" in out
    assert "all no-fire share: 2/5" in out


def test_session_split_on_gap():
    lines = _shot(1, 2, 1, "Standstill", 38.0, 94.0, 95.0, 97.0, 0.2, 7.4) + "\n"
    # a second shot 15 minutes later starts session 2 (seq restarts per session in production)
    late = _shot(1, 5, 1, "Standstill", 38.0, 96.0, 95.0, 97.0, 0.2, 7.4).replace("T15:39:", "T15:54:")
    graded, _, _ = epoch_table.parse(io.StringIO(lines + late + "\n"), "2026-09-01")
    assert [s["session"] for s in graded] == [1, 2]


def test_missing_latency_observation_cannot_shift_across_restarted_sequence():
    """A missing seq=1 sample in session 1 must not consume session 2's seq=1."""

    first = _shot(
        1, 2, 1, "Standstill", 38.0, 94.0, 95.0, 97.0, 0.2, 7.4
    )
    first = "\n".join(
        line for line in first.splitlines() if "latency observation:" not in line
    )
    second = _shot(
        1, 5, 1, "Standstill", 38.0, 96.0, 95.0, 97.0, 0.2, 7.4
    ).replace("T15:39:", "T15:54:").replace("total_ms=221.0", "total_ms=237.5")

    graded, _, _ = epoch_table.parse(
        io.StringIO(first + "\n" + second + "\n"), "2026-09-01"
    )

    assert [row["session"] for row in graded] == [1, 2]
    assert "lat_total_ms" not in graded[0]
    assert "lat_accepted" not in graded[0]
    assert graded[1]["lat_total_ms"] == pytest.approx(237.5)
    assert graded[1]["lat_accepted"] == 1


def test_robust_stats_helpers():
    assert epoch_table.med([1.0, 3.0, 2.0]) == 2.0
    assert math.isnan(epoch_table.med([]))
    assert epoch_table.mad([1.0, 1.0, 1.0, 5.0]) == 0.0
    r, n = epoch_table.corr([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0])
    assert r == pytest.approx(1.0) and n == 8
    b, res = epoch_table.slope([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], [3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 17.0])
    assert b == pytest.approx(2.0) and res == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("missing_field", "present_field", "present_value"),
    [
        ("w20_ms=100.0", "d30_25", -0.9),
        ("w30_ms=99.6", "d25_20", 0.5),
    ],
)
def test_missing_phase_witness_sentinel_never_becomes_an_interval(
    missing_field, present_field, present_value
):
    """The sidecar emits -1.0 for a missing witness, not NaN.

    One missing endpoint must suppress only its own adjacent interval.  It must
    never be subtracted from an absolute monotonic timestamp.
    """

    transcript = _shot(
        1, 2, 1, "Standstill", 38.0, 94.0, 95.0, 97.0, 0.2, 7.4
    ).replace(missing_field, missing_field.split("=")[0] + "=-1.000")
    graded, _, _ = epoch_table.parse(io.StringIO(transcript), "2026-09-01")

    assert len(graded) == 1
    row = graded[0]
    missing_interval = "d25_20" if missing_field.startswith("w20") else "d30_25"
    assert missing_interval not in row
    assert row[present_field] == pytest.approx(present_value)
