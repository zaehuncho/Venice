"""[ORION_CAPTURE_FPS 2026-09-14] ORION_CAPTURE_FPS -> CaptureCardBackend(fps=...).

The customer-facing setting is `capture_card_fps` in the native settings file; the native
hands it to the sidecar as ORION_CAPTURE_FPS (RemotePlaySession.cpp, capture-card branch
only — the decoder branch removes the variable).  CaptureCardBackend derives its
CadenceLock, its nominal frame period, its arrival/cadence window sizes and its health
floor from `fps`, so a wrong value here does not merely mis-label a log line: it makes the
backend model a frame grid the card is not producing.

What is pinned here is the CONTRACT, not the wiring: allowed set, snap direction, tie
direction, and the "absent means 60" default that keeps every pre-setting install byte
identical.
"""

import pytest

from remote_play_orchestrator import (
    CAPTURE_FPS_ALLOWED,
    CAPTURE_FPS_DEFAULT,
    requested_capture_fps,
    snap_capture_fps,
)


def test_allowed_set_and_default_match_the_native_setting():
    # Mirrored from native_orion/src/AppConfig.h (snappedCaptureCardFps + captureCardFps).
    # If these ever diverge, the sidecar models a cadence the native never requested.
    assert CAPTURE_FPS_ALLOWED == (30, 60, 120)
    assert CAPTURE_FPS_DEFAULT == 60


@pytest.mark.parametrize('value', [30, 60, 120])
def test_allowed_values_pass_through_unchanged(value):
    assert snap_capture_fps(value) == value
    assert requested_capture_fps({'ORION_CAPTURE_FPS': str(value)}) == value


@pytest.mark.parametrize(
    'requested,expected',
    [
        (0, 30),
        (29, 30),
        (31, 30),
        (44, 30),
        (45, 30),      # exact tie 30/60 -> the LOWER rate (safer on a USB card)
        (46, 60),
        (59, 60),
        (61, 60),
        (89, 60),
        (90, 60),      # exact tie 60/120 -> the LOWER rate
        (91, 120),
        (144, 120),
        (240, 120),
        (-5, 30),
    ],
)
def test_out_of_set_values_snap_to_the_nearest_allowed_rate(requested, expected):
    # SNAP, not clamp: 144 is a real monitor rate a customer might type, and the only
    # honest answer is the nearest mode the card actually has, not a band edge.
    assert snap_capture_fps(requested) == expected


def test_missing_or_unusable_env_is_the_shipped_default():
    # The decoder branch REMOVES ORION_CAPTURE_FPS, and a standalone sidecar run never
    # sets it.  Both must land on 60 — the rate every build before this setting existed
    # hard-coded — so an install that never touches the slider is unchanged.
    assert requested_capture_fps({}) == 60
    assert requested_capture_fps({'ORION_CAPTURE_FPS': ''}) == 60
    assert requested_capture_fps({'ORION_CAPTURE_FPS': '   '}) == 60
    assert requested_capture_fps({'ORION_CAPTURE_FPS': 'sixty'}) == 60
    assert requested_capture_fps({'ORION_CAPTURE_FPS': '60.5'}) == 60


def test_env_value_is_whitespace_tolerant():
    assert requested_capture_fps({'ORION_CAPTURE_FPS': ' 120 '}) == 120


def test_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv('ORION_CAPTURE_FPS', '30')
    assert requested_capture_fps() == 30
    monkeypatch.delenv('ORION_CAPTURE_FPS')
    assert requested_capture_fps() == CAPTURE_FPS_DEFAULT


def test_backend_derives_its_whole_cadence_model_from_the_requested_rate():
    # The reason this setting has to reach the backend rather than only the log: every
    # cadence-shaped quantity inside CaptureCardBackend is computed from `fps`.
    from capture_card_backend import CaptureCardBackend

    slow = CaptureCardBackend(device_index=0, fps=snap_capture_fps(30))
    fast = CaptureCardBackend(device_index=0, fps=snap_capture_fps(120))
    assert slow._fps == 30
    assert fast._fps == 120
    # Nominal period (ns) and the health floor both track the requested grid.
    assert slow._cadence.dt_nom > fast._cadence.dt_nom
    assert slow._min_health_fps < fast._min_health_fps


def test_suspect_run_thresholds_are_the_same_wall_clock_at_every_rate():
    """The degraded-feed warning must mean the same DURATION at 30/60/120.

    The thresholds were frame counts hard-sized for a 60fps card (15 and 30 frames =
    0.25 s and 0.5 s).  Left alone they would have meant 0.5 s / 1.0 s at 30 and
    0.125 s / 0.25 s at 120 -- one warning, three meanings.
    """
    from remote_play_orchestrator import capture_suspect_run_frames

    # 60 reproduces the historical constants EXACTLY, so no shipped install changes.
    assert capture_suspect_run_frames(60) == (15, 30)
    assert capture_suspect_run_frames(30) == (8, 15)
    assert capture_suspect_run_frames(120) == (30, 60)

    # Same wall-clock, within one frame, at every supported rate.
    for rate in CAPTURE_FPS_ALLOWED:
        black, static = capture_suspect_run_frames(rate)
        assert abs(black / rate - 0.25) <= 1.0 / rate
        assert abs(static / rate - 0.5) <= 1.0 / rate

    # A junk rate snaps first, so the thresholds can never be built from a mode the card
    # does not have.
    assert capture_suspect_run_frames(None) == (15, 30)
    assert capture_suspect_run_frames(0) == capture_suspect_run_frames(30)
