"""Public detect() must not turn a repeated async slot into a template refresh.

Each current-frame NCC localization remains active; only appearance reseeding is
source-clocked. These fixtures use real OpenCV matching with an injected locator.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
STEP = 1.0 / 60.0
BOX = (600, 300, 24, 110)


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


class _Locator:
    provider = "scripted"
    infer_ms = 1.0

    def __init__(self):
        self.result = (False, None, 0.0, -1.0)

    def submit(self, frame, ts):
        pass

    def submit_priority(self, frame, ts):
        return True

    def latest(self):
        return self.result


def _frame(x=600, bottom=410, fill=0.5):
    frame = np.zeros((H, W, 3), np.uint8)
    frame[bottom - int(110 * fill):bottom, x:x + 24] = 255
    # A stable textured border makes NCC unambiguous without modifying the
    # white fill column. Real meter frames are covered by the local replay.
    patch = np.random.default_rng(42).integers(0, 256, (17, 8), np.uint8)
    frame[bottom - 12:bottom + 5, x - 8:x] = patch[:, :, None]
    frame[bottom - 12:bottom + 5, x + 24:x + 32] = patch[:, :, None]
    return frame


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_LIFECYCLE", "1")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    r = SimpleMeterReader(W, H, cfg=_Cfg(), require_gameplay_eligibility=False)
    r._meter_detector = _Locator()
    r.set_shot_state(True, 1.0, True)
    seeds = []
    original = r._seed_track_template

    def record(frame, box):
        seeds.append(tuple(box))
        return original(frame, box)

    r._seed_track_template = record
    return r, seeds


def _lock(r, ts=1000.0):
    r._meter_detector.result = (True, BOX, 0.9, ts)
    result = r.detect(_frame(), ts=ts)
    assert r._det_state == "locked" and result.detected
    assert r._det_track_score >= r._det_track_min
    return result


@pytest.mark.parametrize("refresh", [2, 3, 5])
def test_repeated_latest_tracks_every_frame_but_refreshes_only_new_sources(monkeypatch, refresh):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    counts = []
    for i in range(1, 13):
        ts = 1000.0 + STEP * i
        fresh = i % refresh == 0
        if fresh:
            r._meter_detector.result = (True, BOX, 0.9, ts)
        before = len(seeds)
        result = r.detect(_frame(), ts=ts)
        counts.append(len(seeds) - before)
        assert result.detected and result.fill_pct > 1.0
        assert r._det_track_score >= r._det_track_min
        # Freshness and direct-fill/width authority remain independent of newness.
        assert r._det_fresh_accept
    assert counts == [int(i % refresh == 0) for i in range(1, 13)]


def test_repeated_source_cannot_refresh_a_brief_occluder_into_the_template(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    template = r._det_tmpl.copy()
    count = len(seeds)
    result = r.detect(np.zeros((H, W, 3), np.uint8), ts=1000.0 + STEP)
    assert result.fill_pct == 0.0, "full occlusion must remain no measured fill"
    assert len(seeds) == count
    assert np.array_equal(r._det_tmpl, template)
    # The original appearance, not the occluder, is available immediately on return.
    result = r.detect(_frame(), ts=1000.0 + 2 * STEP)
    assert result.detected and result.fill_pct > 1.0
    assert r._det_track_score >= r._det_track_min


def test_fresh_source_still_refreshes_even_when_box_is_unchanged(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    count = len(seeds)
    ts = 1000.0 + STEP
    r._meter_detector.result = (True, BOX, 0.9, ts)
    r.detect(_frame(), ts=ts)
    assert len(seeds) == count + 1


@pytest.mark.parametrize("region", [(592, 398, 8, 17), (592, 413, 40, 2)])
def test_partial_occlusion_cannot_become_appearance_on_a_repeated_slot(monkeypatch, region):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    template = r._det_tmpl.copy()
    count = len(seeds)
    frame = _frame()
    x, y, w, h = region
    frame[y:y + h, x:x + w] = 0
    r.detect(frame, ts=1000.0 + STEP)
    assert len(seeds) == count and np.array_equal(r._det_tmpl, template)
    result = r.detect(_frame(), ts=1000.0 + 2 * STEP)
    assert result.detected and result.fill_pct > 1.0
    assert r._det_track_score >= r._det_track_min


def test_missing_locator_does_not_replay_previous_refresh_intent(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    count = len(seeds)
    r._meter_detector = None
    r.detect(_frame(), ts=1000.0 + STEP)
    assert len(seeds) == count


def test_negative_result_does_not_refresh_and_drop_reacquire_bootstraps(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    count = len(seeds)
    for i in range(1, 4):
        ts = 1000.0 + i * STEP
        r._meter_detector.result = (False, None, 0.0, ts)
        r.detect(np.zeros((H, W, 3), np.uint8), ts=ts)
    assert r._det_state != "locked"
    assert len(seeds) == count
    assert r._det_tmpl is None
    r._meter_detector.result = (True, BOX, 0.9, 1000.1)
    result = r.detect(_frame(), ts=1000.1)
    assert r._det_state == "locked" and result.detected
    assert len(seeds) > count


def test_refused_far_result_never_reseeds_a_good_lock(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    count = len(seeds)
    r._meter_detector.result = (True, (50, 20, 24, 110), 0.9, 1000.0 + STEP)
    result = r.detect(_frame(), ts=1000.0 + STEP)
    assert not r._det_fresh_accept
    assert result.detected
    assert len(seeds) == count


@pytest.mark.parametrize("shift", [(8, 0), (0, 8), (8, -6)])
def test_reconciliation_converges_without_relearning_pixels_or_inventing_motion(monkeypatch, shift):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    r._det_fresh_accept = False
    r._det_new_accept = False
    template = r._det_tmpl.copy()
    count = len(seeds)
    held = (BOX[0] + shift[0], BOX[1] + shift[1], BOX[2], BOX[3])
    outputs = [r._det_track_step(_frame(), held, 1000.01 + i * 0.01)
               for i in range(12)]
    errors = [np.hypot(out[0] - held[0], out[1] - held[1]) for out in outputs]
    assert errors[-1] < errors[0], "anchor correction cannot disappear on the next match"
    assert len(seeds) == count and np.array_equal(r._det_tmpl, template)
    assert r._det_track_vx == pytest.approx(0.0, abs=1e-9)
    assert r._det_track_vy == pytest.approx(0.0, abs=1e-9)


def test_locator_failure_does_not_replay_previous_refresh_intent(monkeypatch):
    r, seeds = _reader(monkeypatch)
    _lock(r)
    count = len(seeds)

    def failed_latest():
        raise RuntimeError("local fixture: unavailable latest slot")

    r._meter_detector.latest = failed_latest
    r.detect(_frame(), ts=1000.0 + STEP)
    assert len(seeds) == count
