"""[ORION_CAPTURE_CADENCE_RELOCK 2026-09-14] A driver may ACCEPT a frame rate the device cannot
deliver (Elgato HD60 X: 1080p120 accepted, 60 fps delivered). The cadence lock must follow the
DELIVERED rate, not the requested one, or every frame is flagged late and the snapping grid is
wrong (measured: raw_late ~300 per health window at a 120 request on the owner's rig)."""
from __future__ import annotations

import capture_card_backend as ccb


def _intervals(fps: float, n: int = 90, jitter_ns: int = 0):
    base = int(round(1e9 / fps))
    out = []
    for i in range(n):
        j = ((i * 7919) % 11 - 5) * jitter_ns // 5 if jitter_ns else 0
        out.append(base + j)
    return out


def test_honoured_request_needs_no_relock():
    assert ccb.delivered_cadence_fps(_intervals(60.0), 60.0) is None
    assert ccb.delivered_cadence_fps(_intervals(120.0), 120.0) is None
    # NTSC-ish 59.94 delivered at a 60 request is inside the tolerance.
    assert ccb.delivered_cadence_fps(_intervals(59.94), 60.0) is None


def test_120_request_delivered_at_60_relocks_to_60():
    assert ccb.delivered_cadence_fps(_intervals(60.0, jitter_ns=800_000), 120.0) == 60.0


def test_60_request_delivered_at_30_relocks_to_30():
    assert ccb.delivered_cadence_fps(_intervals(30.0), 60.0) == 30.0


def test_odd_delivered_rate_is_reported_as_measured():
    # 45 fps is not a common rate: report the measurement itself (rounded), not a snap.
    got = ccb.delivered_cadence_fps(_intervals(45.0), 60.0)
    assert got is not None and abs(got - 45.0) < 0.2


def test_median_survives_dropped_and_duplicated_frames():
    ivals = _intervals(60.0)
    ivals[::9] = [int(2e9 / 60)] * len(ivals[::9])      # every 9th interval is a drop (2 periods)
    ivals[3::17] = [1] * len(ivals[3::17])               # a few near-zero duplicates
    assert ccb.delivered_cadence_fps(ivals, 120.0) == 60.0


def test_too_few_samples_or_bad_request_is_undecided():
    assert ccb.delivered_cadence_fps(_intervals(60.0, n=5), 120.0) is None
    assert ccb.delivered_cadence_fps(_intervals(60.0), 0.0) is None
    assert ccb.delivered_cadence_fps([], 60.0) is None


def test_backend_state_starts_undecided(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_PTS_LOCK", "1")
    be = ccb.CaptureCardBackend(device_index=0, fps=120)
    assert be._relock_done is False
    assert be._cadence_delivered_fps == 0.0
    assert be._relock_intervals.maxlen == ccb._CADENCE_RELOCK_WARMUP


def test_relock_adopts_the_delivered_rate_for_every_consumer(monkeypatch):
    """After the verdict the backend must not keep judging late gaps / health / PTS against
    the requested rate (that is what produced raw_late ~300 per window on the owner's rig)."""
    monkeypatch.setenv("ORION_CAPTURE_PTS_LOCK", "1")
    be = ccb.CaptureCardBackend(device_index=0, fps=120)
    assert be._fps == 120 and be._fps_requested == 120
    base = int(1e9 / 60)
    t = 10_000_000_000
    for _ in range(ccb._CADENCE_RELOCK_WARMUP + 2):
        t += base
        be._stamp_frame(t, t + 5_000_000_000)
    assert be._relock_done is True
    assert be._cadence_delivered_fps == 60.0
    assert be._fps == 60 and be._fps_requested == 120
    assert be._min_health_fps <= 60.0
