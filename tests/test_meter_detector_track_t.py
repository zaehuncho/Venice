"""Track T coverage — make meter detection STICK on motion (the MyCourt ship-gate).

Live evidence (2026-07-03 20:31 gameplay, locator ON): the trained v6 meter locator tracked the
panning meter ~90% of frames, but the DOWNSTREAM validator + non-authoritative locator path chopped
the served signal (in-shot detected 81% / fed 61%) so the timing engine starved. These tests pin the
four fixes, and — critically — pin that every one of them is INERT on the classical path (locator OFF)
so the PARK regression stays byte-identical:

  * T-a1 confidence HYSTERESIS: a single sub-threshold dip must NOT drop a healthy track;
  * T-a2 warm-arm on ANY recent-valid loss: a sub-threshold-START shot still warm-reacquires;
  * T-a3 pan-widened re-acquire gate: a steadily panning meter re-latches, not perpetual reset;
  * T-a4 + T-b: a STRONG v6 box is authoritative — it serves/feeds a detection (relaxed gates +
    box-fallback + loc_strong corroboration) where the classical path alone would reject it.
"""
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import DetectorConfig, MeterDetector, _StabilityValidator


# --------------------------------------------------------------------------- #
# Validator-level fixtures (T-a)
# --------------------------------------------------------------------------- #
def _match(found=True, conf=0.9, x=200.0, y=400.0):
    return SimpleNamespace(found=found, confidence=float(conf), x=float(x), y=float(y))


def _cfg(**kw):
    c = DetectorConfig()
    c.min_consecutive_valid_frames = 3
    c.position_jump_max_px = 80.0
    c.tracking_jump_scale = 3.0
    c.jump_gate_ref_width = 720
    c.confidence_threshold = 0.35
    c.low_conf_grace_frames = 2
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _locator_validator(**kw):
    v = _StabilityValidator(_cfg(**kw))
    v._locator_mode = True  # what MeterDetector sets when the YOLO locator is active
    v.note_frame_width(1280)
    return v


def _acquire(v, x=200.0, n=3, ts0=0.0):
    ts = ts0
    for i in range(n):
        v.validate(_match(x=x, conf=0.9), 10 + i, ts)
        ts += 0.016
    return ts


# --------------------------------------------------------------------------- #
# T-a1 — confidence hysteresis
# --------------------------------------------------------------------------- #
def test_single_low_conf_dip_does_not_drop_track():
    v = _locator_validator()
    ts = _acquire(v, x=200)
    assert v._tracking is True
    # A single sub-threshold, positionally-consistent frame is graced -> stays fed + tracking.
    st = v.validate(_match(x=201, conf=0.1), 20, ts)
    assert st.valid is True
    assert v._tracking is True
    assert v.last_event == "low_conf_grace"
    # Confidence recovers -> the lock was never lost.
    st = v.validate(_match(x=202, conf=0.9), 21, ts + 0.016)
    assert st.valid is True and v._tracking is True


def test_low_conf_grace_exhausts_then_drops():
    v = _locator_validator(low_conf_grace_frames=2)
    ts = _acquire(v, x=200)
    # Two graced dips hold the lock...
    assert v.validate(_match(x=200, conf=0.1), 1, ts).valid is True
    assert v.validate(_match(x=200, conf=0.1), 1, ts + 0.016).valid is True
    # ...the third consecutive sub-threshold frame exceeds the grace and drops it.
    st = v.validate(_match(x=200, conf=0.1), 1, ts + 0.032)
    assert st.valid is False
    assert v._tracking is False
    assert v.last_event == "low_conf_reset"


def test_classical_low_conf_drops_on_first_dip():
    """Gating guard: with the locator OFF (default), a sub-threshold frame drops the track on the
    FIRST dip exactly as before — the hysteresis must not leak into the classical PARK path."""
    v = _StabilityValidator(_cfg())  # _locator_mode stays False
    v.note_frame_width(1280)
    ts = _acquire(v, x=200)
    assert v._tracking is True
    st = v.validate(_match(x=200, conf=0.1), 20, ts)
    assert st.valid is False
    assert v._tracking is False
    assert v.last_event == "low_conf_reset"


# --------------------------------------------------------------------------- #
# T-a2 — warm arms on ANY recent-valid loss (sub-threshold-start shot)
# --------------------------------------------------------------------------- #
def test_subthreshold_start_arms_warm_without_prior_tracking():
    v = _locator_validator(min_consecutive_valid_frames=3)
    ts = 0.0
    # ONE valid detection — not enough to latch tracking (min_consecutive=3).
    st = v.validate(_match(x=500, y=300, conf=0.9), 10, ts)
    assert st.valid is False and v._tracking is False
    ts += 0.016
    # The early-rise meter dips sub-threshold -> loss. Warm must arm off the RECENT valid detection
    # even though tracking never latched (the live 25-detected / 0-fed sub-threshold-start shot).
    v.validate(_match(x=500, y=300, conf=0.1), 5, ts)
    assert v._warm_pos is not None
    ts += 0.016
    # The same meter reappears near the lost spot -> inherits corroboration + latches immediately.
    st = v.validate(_match(x=506, y=302, conf=0.9), 10, ts)
    assert st.valid is True and v._tracking is True
    assert v.last_event == "warm_reacquired"


def test_classical_warm_not_armed_without_tracking():
    """Gating guard: classically, warm only arms on the loss of a LATCHED tracking lock."""
    v = _StabilityValidator(_cfg(min_consecutive_valid_frames=3))  # _locator_mode False
    v.note_frame_width(1280)
    ts = 0.0
    v.validate(_match(x=500, y=300, conf=0.9), 10, ts)
    ts += 0.016
    v.validate(_match(x=500, y=300, conf=0.1), 5, ts)
    assert v._warm_pos is None


# --------------------------------------------------------------------------- #
# T-a3 — pan-widened re-acquire gate
# --------------------------------------------------------------------------- #
def test_continuous_pan_reacquires_in_locator_mode():
    v = _locator_validator(min_consecutive_valid_frames=3)
    v._pan_px = 100.0  # the detector reports a fast locator pan (px/frame)
    ts = 0.0
    x = 300.0
    events, valids = [], []
    for _ in range(6):
        st = v.validate(_match(x=x, y=300, conf=0.9), 10, ts)
        events.append(v.last_event)
        valids.append(st.valid)
        x += 180.0  # jump 180px/frame: > base acq gate (~142) but < pan-widened gate (~342)
        ts += 0.016
    assert "acq_jump_reset" not in events, f"a steady pan must not perpetually reset: {events}"
    assert any(valids), f"a steadily panning meter must acquire: {valids}"


def test_classical_continuous_pan_stays_gated_tight():
    """Gating guard: the same steady 180px/frame motion classically exceeds the tight acquire gate
    every frame -> never latches (no pan widening off the locator)."""
    v = _StabilityValidator(_cfg(min_consecutive_valid_frames=3))  # _locator_mode False
    v.note_frame_width(1280)
    ts = 0.0
    x = 300.0
    valids = []
    for _ in range(6):
        valids.append(v.validate(_match(x=x, y=300, conf=0.9), 10, ts).valid)
        x += 180.0
        ts += 0.016
    assert not any(valids)


# --------------------------------------------------------------------------- #
# T-b — a STRONG v6 box is authoritative
# --------------------------------------------------------------------------- #
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLES = str(ROOT / "meter_styles")
MX, MW, BOT, FULL_H = 640, 18, 430, 100


def _park_cfg():
    c = DetectorConfig()
    c.meter_color = "Red"
    c.auto_meter_color = False
    c.park_temporal_enabled = True
    return c


def _park_frame(fill_h):
    rng = np.random.default_rng(7)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    top = BOT - fill_h
    cv2.rectangle(fr, (MX - 3, (BOT - FULL_H) - 3), (MX + MW + 3, BOT + 3), (8, 8, 8), 3)
    cv2.rectangle(fr, (MX, top), (MX + MW, BOT), (30, 30, 230), -1)          # pure-red fill
    cv2.rectangle(fr, (MX, BOT - FULL_H), (MX + MW, BOT - FULL_H + 6), (40, 255, 60), -1)  # green tip
    return fr


class _FakeLocator:
    """Stand-in for meter_locator_infer.MeterLocator: returns a fixed box + confidence."""

    def __init__(self, box, conf):
        self.enabled = True
        self._box = box
        self._conf = float(conf)

    def locate(self, frame_bgr):
        return self._box

    @property
    def last_conf(self):
        return self._conf


def _fed(r):
    """Mirror RemotePlayOrchestrator._should_feed_engine: a detection reaches the timing engine."""
    return bool(r.detected and r.bbox[2] > 0 and r.bbox[3] > 0
                and r.rejection_reason in ("", "green_not_found"))


def test_box_fallback_match_serves_red_and_rejects_empty():
    det = MeterDetector(STYLES, _park_cfg())
    red_crop = np.zeros((120, 40, 3), np.uint8)
    cv2.rectangle(red_crop, (10, 20), (30, 110), (30, 30, 230), -1)
    grey_crop = np.full((120, 40, 3), 40, np.uint8)
    m_red = det._box_fallback_match(red_crop, (600, 300, 40, 120), 0.9)
    assert m_red is not None and m_red.found and m_red.loc_strong
    assert m_red.x == 600.0 and m_red.w == 40   # emitted FROM the box
    # A genuinely empty (no-red) box must NOT be forced to a detection.
    assert det._box_fallback_match(grey_crop, (600, 300, 40, 120), 0.9) is None


def test_strong_locator_box_feeds_where_classical_does_not():
    # Corrected loc_strong contract (2026-07-06): a STRONG v6 box is NOT a standalone corroboration
    # (a single conf>=0.55 box on decor would otherwise latch with zero behavioral evidence). Paired
    # with a RISE it feeds where the un-corroborated classical single frame does not.
    base = MeterDetector(STYLES, _park_cfg())
    assert _fed(base.detect(_park_frame(20))) is False   # classical single frame: un-corroborated
    det = MeterDetector(STYLES, _park_cfg())
    det._stability._locator_mode = True
    fed_any = False
    for h in (8, 12, 16, 20, 24):   # a RISING meter under a strong box -> loc_strong AND rising
        det._locator = _FakeLocator((MX - 6, BOT - h - 6, MW + 12, h + 12), 0.92)
        fed_any = fed_any or _fed(det.detect(_park_frame(h)))
    assert fed_any is True


def test_static_strong_locator_box_does_not_bypass_corroboration():
    # The closed cross-decor bypass: a single STATIC strong box with no rise / green / recent-prox
    # must NOT feed (it would have under the old bare-loc_strong corroboration term).
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _FakeLocator((MX - 6, BOT - FULL_H - 6, MW + 12, FULL_H + 12), 0.92)
    det._stability._locator_mode = True
    assert _fed(det.detect(_park_frame(20))) is False


def test_weak_locator_box_over_no_meter_does_not_fabricate():
    """A WEAK box (below _LOC_STRONG_CONF) over a red-free region must not fabricate a detection —
    it falls through to the classical park path, which finds no confirmed meter."""
    fr = _park_frame(0)  # no meter drawn (only background noise)
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _FakeLocator((MX - 6, BOT - FULL_H - 6, MW + 12, FULL_H + 12), 0.30)
    det._stability._locator_mode = True
    assert det.detect(fr).detected is False


# --------------------------------------------------------------------------- #
# MeterBoxKalman (ORION_METER_TRACK) — the served-geometry stabilizer.
#
# Unit half: the filter itself (position tracks a pan with no lag, size is
# sliver-gated slow, outliers re-seed instead of drift, predict bridges a miss
# purely). Detector half: the wiring (smoothed width protects the fill
# denominator, the echo coasts, grow-stuck height recovers) and — critically —
# that with the flag OFF the module is never imported and serving is unchanged.
# --------------------------------------------------------------------------- #
import subprocess
import sys

import meter_detector as _md_mod
from meter_box_kalman import MeterBoxKalman, try_load as _bt_try_load

DT = 1.0 / 60.0


def _fed_pose(bt, xs, cb=430.0, w=18.0, t0=0.0, gate=200.0):
    """Drive update_pose over a cx sequence at 60fps; returns ([events], t_end)."""
    evs, t = [], t0
    for x in xs:
        evs.append(bt.update_pose(float(x), cb, w, t, gate))
        t += DT
    return evs, t


def test_bt_static_jitter_smoothed():
    rng = np.random.default_rng(11)
    bt = MeterBoxKalman()
    xs = 300.0 + rng.normal(0.0, 2.0, 90)
    ws = 18.0 + rng.normal(0.0, 1.2, 90)
    t, sm_x, sm_w = 0.0, [], []
    for x, w in zip(xs, ws):
        bt.update_pose(float(x), 430.0, float(w), t, 200.0)
        t += DT
        sm_x.append(bt.cx())
        sm_w.append(bt.width())
    # after convergence the smoothed traces must be meaningfully quieter than the raw jitter
    assert np.std(sm_x[30:]) < 0.75 * np.std(xs[30:])
    assert np.std(sm_w[30:]) < 0.50 * np.std(ws[30:])


def test_bt_linear_pan_tracks_with_low_lag():
    bt = MeterBoxKalman()
    v = 360.0  # px/s = 6 px/frame @60fps (a fast pan)
    t = 0.0
    for i in range(40):
        true = 300.0 + v * t
        assert bt.update_pose(true, 430.0, 18.0, t, 200.0) in ("seed", "accept")
        if i >= 15:  # converged: a constant-velocity pan must be tracked with (near) zero lag
            assert abs(bt.cx() - true) < 2.5, f"frame {i}: lag {abs(bt.cx() - true):.2f}px"
        t += DT


def test_bt_predict_pose_bridges_miss_and_is_pure():
    bt = MeterBoxKalman(coast_s=0.35)
    v = 240.0
    t = 0.0
    for _ in range(20):
        bt.update_pose(300.0 + v * t, 430.0, 18.0, t, 200.0)
        t += DT
    t_last = t - DT
    # a 2-frame miss: the prediction continues the pan
    p1 = bt.predict_pose(t_last + 2 * DT)
    assert p1 is not None
    assert abs(p1[0] - (300.0 + v * (t_last + 2 * DT))) < 3.0
    # PURE: repeated calls do not mutate state
    p2 = bt.predict_pose(t_last + 2 * DT)
    assert p1 == p2
    # beyond the coast horizon -> None (the caller falls back to the frozen echo)
    assert bt.predict_pose(t_last + 0.5) is None
    # not-ready filter -> None
    assert MeterBoxKalman().predict_pose(0.0) is None


def test_bt_width_sliver_gated_but_position_updates():
    bt = MeterBoxKalman()
    _fed_pose(bt, [300.0] * 12)
    w0 = bt.width()
    # a 1-frame sliver (w=7 ~ 0.39x) and a 1-frame merge (w=41 ~ 2.3x): width HOLDS...
    assert bt.update_pose(304.0, 430.0, 7.0, 12 * DT, 200.0) == "accept"
    assert abs(bt.width() - w0) < 1.0
    assert bt.update_pose(308.0, 430.0, 41.0, 13 * DT, 200.0) == "accept"
    assert abs(bt.width() - w0) < 1.0
    # ...while the position channel still followed those frames
    assert bt.cx() > 300.5


def test_bt_distractor_jump_reseeds_not_drifts():
    bt = MeterBoxKalman()
    _fed_pose(bt, [300.0] * 10)
    # two far outliers are REJECTED (state holds near 300 — a sliver can't yank the filter)...
    assert bt.update_pose(800.0, 430.0, 18.0, 10 * DT, 150.0) == "reject"
    assert abs(bt.cx() - 300.0) < 5.0
    assert bt.update_pose(800.0, 430.0, 18.0, 11 * DT, 150.0) == "reject"
    assert abs(bt.cx() - 300.0) < 5.0
    # ...the third consecutive far measurement is a REAL new meter -> adopt it (re-seed, no drift arc)
    assert bt.update_pose(800.0, 430.0, 18.0, 12 * DT, 150.0) == "reseed"
    assert abs(bt.cx() - 800.0) < 1e-6


def test_bt_gap_or_backwards_time_reseeds():
    bt = MeterBoxKalman(max_dt_s=0.6)
    _fed_pose(bt, [300.0] * 5)
    assert bt.update_pose(300.0, 430.0, 18.0, 5 * DT + 0.7, 200.0) == "reseed"   # gap > max_dt
    assert bt.update_pose(300.0, 430.0, 18.0, 5 * DT + 0.69, 200.0) == "reseed"  # dt <= 0
    assert not bt.ready  # a re-seed is a fresh episode (n=1)


def test_bt_never_raises_or_corrupts():
    bt = MeterBoxKalman()
    _fed_pose(bt, [300.0] * 6)
    for bad in (None, "x", float("nan"), float("inf")):
        assert bt.update_pose(bad, 430.0, 18.0, 6 * DT, 200.0) == "reject"
        bt.update_height(bad, 6 * DT)
    assert np.isfinite(bt.cx()) and abs(bt.cx() - 300.0) < 5.0
    assert np.isfinite(bt.width())
    # a NaN timestamp must not poison the clock either
    assert bt.update_pose(300.0, 430.0, 18.0, float("nan"), 200.0) == "reject"
    assert bt.update_pose(301.0, 430.0, 18.0, 7 * DT, 200.0) in ("accept", "reseed")


def test_bt_height_channel_green_fed():
    bt = MeterBoxKalman()
    t = 0.0
    for _ in range(4):
        bt.update_height(100.0, t)
        t += DT
    assert bt.ready_h
    assert abs(bt.height() - 100.0) < 2.0
    # sliver-gate mirrors the width channel: a 3x spike is held
    bt.update_height(300.0, t)
    assert bt.height() < 110.0
    # a very old sighting is a different episode/scale -> re-seed
    bt.update_height(60.0, t + 5.0)
    assert abs(bt.height() - 60.0) < 1e-6


def test_bt_try_load_env_gated(monkeypatch):
    monkeypatch.delenv("ORION_METER_TRACK", raising=False)
    assert _bt_try_load() is None
    monkeypatch.setenv("ORION_METER_TRACK", "1")
    assert isinstance(_bt_try_load(), MeterBoxKalman)


# --------------------------------------------------------------------------- #
# MeterBoxKalman detector wiring (synthetic frames, fake 60fps clock)
# --------------------------------------------------------------------------- #
def _meter_frame(x_left, fill_h, w=MW, full_h=FULL_H, chevron_top_off=None, seed=7):
    """_park_frame generalized: meter column at x_left, fill width w, green chevron at
    full-track height chevron_top_off (default full_h) above the notch."""
    rng = np.random.default_rng(seed)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    cho = full_h if chevron_top_off is None else chevron_top_off
    top = BOT - fill_h
    cv2.rectangle(fr, (x_left - 3, (BOT - full_h) - 3), (x_left + w + 3, BOT + 3), (8, 8, 8), 3)
    if fill_h > 0:
        cv2.rectangle(fr, (x_left, top), (x_left + w, BOT), (30, 30, 230), -1)
    cv2.rectangle(fr, (x_left, BOT - cho), (x_left + w, BOT - cho + 6), (40, 255, 60), -1)
    return fr


class _PathLocator:
    """A locator whose box follows a scripted path (what v6 does on a panning meter)."""

    def __init__(self, boxes, conf=0.92):
        self.enabled = True
        self._boxes = list(boxes)
        self._i = 0
        self._conf = float(conf)

    def locate(self, frame_bgr):
        b = self._boxes[min(self._i, len(self._boxes) - 1)]
        self._i += 1
        return b

    @property
    def last_conf(self):
        return self._conf


def _fake_clock(monkeypatch):
    """Pin meter_detector's time.perf_counter to a hand-stepped 60fps clock so the
    filter/validator see live-like dt instead of test-machine wall time."""
    clk = [1000.0]
    monkeypatch.setattr(_md_mod.time, "perf_counter", lambda: clk[0])

    def tick():
        clk[0] += DT
    return tick


def _loc_box(x_left, full_h=FULL_H, w=MW):
    return (x_left - 6, BOT - full_h - 6, w + 12, full_h + 12)


def _run_pan(det, xs, fill0=20, rise=2, locator_conf=0.92, tick=None):
    """Feed a panning, rising meter; returns the list of DetectResults."""
    det._locator = _PathLocator([_loc_box(int(x)) for x in xs], locator_conf)
    det._stability._locator_mode = True
    out = []
    for i, x in enumerate(xs):
        if tick:
            tick()
        out.append(det.detect(_meter_frame(int(x), min(95, fill0 + rise * i), seed=7 + i)))
    return out


def test_flag_off_box_track_none_and_deterministic(monkeypatch):
    monkeypatch.delenv("ORION_METER_TRACK", raising=False)
    rng = np.random.default_rng(5)
    xs = [400 + 4 * i + int(j) for i, j in enumerate(rng.integers(-3, 4, 24))]
    tick = _fake_clock(monkeypatch)
    a = MeterDetector(STYLES, _park_cfg())
    boxes_a = [r.bbox for r in _run_pan(a, xs, tick=tick)]
    b = MeterDetector(STYLES, _park_cfg())
    boxes_b = [r.bbox for r in _run_pan(b, xs, tick=tick)]
    assert a._box_track is None and b._box_track is None
    assert boxes_a == boxes_b  # classical serving is deterministic (the flag-off identity pin)


def test_flag_off_never_imports_module():
    """The house import-guard contract: with ORION_METER_TRACK unset, meter_box_kalman is never
    imported and the detector runs without it (byte-identical classical path)."""
    code = (
        "import sys, os\n"
        "sys.path.insert(0, r'%s')\n"
        "os.environ.pop('ORION_METER_TRACK', None)\n"
        "import numpy as np\n"
        "import meter_detector as md\n"
        "cfg = md.DetectorConfig(); cfg.meter_color = 'Red'; cfg.auto_meter_color = False\n"
        "d = md.MeterDetector(r'%s', cfg)\n"
        "d.detect(np.zeros((720, 1280, 3), np.uint8))\n"
        "print('OK', d._box_track is None and 'meter_box_kalman' not in sys.modules)\n"
    ) % (str(ROOT), STYLES)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    assert "OK True" in r.stdout


def test_flag_on_smooths_pan_jitter(monkeypatch):
    """Mode 5: a jittery-but-present pan must be SMOOTHED, not dropped — the served centre hugs
    the true linear path tighter than the raw draw jitter, and frame-to-frame jumps shrink."""
    rng = np.random.default_rng(9)
    jit = rng.integers(-3, 4, 26)
    xs = [400 + 4 * i + int(j) for i, j in enumerate(jit)]
    monkeypatch.delenv("ORION_METER_TRACK", raising=False)
    tick = _fake_clock(monkeypatch)
    off = [r.bbox for r in _run_pan(MeterDetector(STYLES, _park_cfg()), xs, tick=tick)]
    monkeypatch.setenv("ORION_METER_TRACK", "1")
    det_on = MeterDetector(STYLES, _park_cfg())
    assert det_on._box_track is not None
    on = [r.bbox for r in _run_pan(det_on, xs, tick=tick)]

    def _dev_from_linear(boxes):
        pts = [(i, b[0] + b[2] * 0.5) for i, b in enumerate(boxes) if b[2] > 0]
        idx = np.array([p[0] for p in pts], float)
        cxs = np.array([p[1] for p in pts], float)
        fit = np.polyval(np.polyfit(idx, cxs, 1), idx)
        return float(np.std(cxs - fit)), float(np.max(np.abs(np.diff(cxs)))) if len(cxs) > 1 else 0.0

    dev_off, jump_off = _dev_from_linear(off[6:])
    dev_on, jump_on = _dev_from_linear(on[6:])
    assert dev_on < dev_off, f"smoothed deviation {dev_on:.2f} !< raw {dev_off:.2f}"
    assert jump_on <= jump_off + 1e-9


def test_width_blip_does_not_wipe_denominator(monkeypatch):
    """Mode 2: ONE fat-width frame (contour merge / fake YOLO width) must not wipe the learned
    fill denominator when the filter is on — and must keep wiping it when it is off (the pinned
    legacy behaviour the guard was built around). The blip frame carries NO green chevron: the
    live harm case is blur (which kills the green sighting first), and a same-frame green would
    immediately re-learn the denominator, masking the wipe."""
    def _blip_frame():
        rng = np.random.default_rng(99)
        fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
        x = MX - 17
        cv2.rectangle(fr, (x - 3, (BOT - FULL_H) - 3), (x + 52 + 3, BOT + 3), (8, 8, 8), 3)
        cv2.rectangle(fr, (x, BOT - 40), (x + 52, BOT), (30, 30, 230), -1)   # w=52 red, NO chevron
        return fr

    def _run(flag):
        if flag:
            monkeypatch.setenv("ORION_METER_TRACK", "1")
        else:
            monkeypatch.delenv("ORION_METER_TRACK", raising=False)
        tick = _fake_clock(monkeypatch)
        det = MeterDetector(STYLES, _park_cfg())
        boxes = [_loc_box(MX)] * 10 + [_loc_box(MX, w=53)] + [_loc_box(MX)]
        det._locator = _PathLocator(boxes)
        det._stability._locator_mode = True
        for i in range(10):
            tick()
            det.detect(_meter_frame(MX, 20 + 2 * i, seed=7 + i))
        assert det._track_full_h > 50.0, "precondition: denominator learned from the green sighting"
        tick()
        det.detect(_blip_frame())        # the 1-frame ~2.5x width blip, green-free
        wiped = det._track_full_h <= 8.0
        return wiped

    assert _run(flag=False) is True    # legacy: the blip wipes the denominator
    assert _run(flag=True) is False    # filter on: smoothed width shields it


def test_miss_echo_coasts_with_pan(monkeypatch):
    """Mode 4: on a v6+colour MISS mid-pan the memory echo must FOLLOW the predicted trajectory
    (flag on) instead of freezing at the last served position (flag off)."""
    xs = [400 + 6 * i for i in range(14)]

    def _run(flag):
        if flag:
            monkeypatch.setenv("ORION_METER_TRACK", "1")
        else:
            monkeypatch.delenv("ORION_METER_TRACK", raising=False)
        tick = _fake_clock(monkeypatch)
        # park OFF for this test: the park tracker's template-coast would otherwise legitimately
        # serve the blank frame (the layered no-flicker design) and the memory-echo path — the
        # thing under test — would never be reached.
        cfg = _park_cfg()
        cfg.park_temporal_enabled = False
        det = MeterDetector(STYLES, cfg)
        res = _run_pan(det, xs, tick=tick)
        assert res[-1].detected and res[-1].rejection_reason in ("", "green_not_found")
        last_cx = res[-1].bbox[0] + res[-1].bbox[2] * 0.5
        det._locator._boxes.append(None)  # locator misses next frame
        det._locator._i = len(det._locator._boxes) - 1
        rng = np.random.default_rng(123)
        empty = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
        tick()
        echo = det.detect(empty)
        assert echo.rejection_reason == "meter_memory"
        return last_cx, echo.bbox[0] + echo.bbox[2] * 0.5

    last_off, echo_off = _run(flag=False)
    assert abs(echo_off - last_off) < 1e-6          # legacy echo: frozen bbox
    last_on, echo_on = _run(flag=True)
    assert 2.0 < (echo_on - last_on) < 14.0, (       # coasting echo: continues the ~6px/frame pan
        f"echo cx moved {echo_on - last_on:.1f}px; expected a ~6px/frame coast")


def test_grow_stuck_height_recovers(monkeypatch):
    """Mode 1: two frames of a too-tall green reading ratchet the legacy max-hold box for the
    REST of the episode; the filtered height must come back down once normal sightings resume."""
    def _final_h(flag):
        if flag:
            monkeypatch.setenv("ORION_METER_TRACK", "1")
        else:
            monkeypatch.delenv("ORION_METER_TRACK", raising=False)
        tick = _fake_clock(monkeypatch)
        det = MeterDetector(STYLES, _park_cfg())
        det._locator = _PathLocator([_loc_box(MX)] * 60)
        det._stability._locator_mode = True
        h_last = 0
        for i in range(40):
            tick()
            cho = 140 if i in (10, 11) else None     # 2 frames of an inflated green height
            r = det.detect(_meter_frame(MX, 30, chevron_top_off=cho, seed=7 + i))
            if r.detected:
                h_last = r.bbox[3]
        return h_last

    h_off = _final_h(flag=False)
    h_on = _final_h(flag=True)
    assert h_off >= 135, f"precondition: legacy max-hold stays ratcheted (got {h_off})"
    assert h_on <= 120, f"filtered height must recover after the blip (got {h_on})"
    assert h_on < h_off - 10


# --------------------------------------------------------------------------- #
# T5 — edge-fade blackout (live session 20260704_2108: 5/20 shots fired blind
# on full-shot roi_not_found streaks). When BOTH the locator and the park
# tracker miss, the park branch must fall back to the classical wide scan
# (+ green-first) instead of giving up — while the acquisition-corroboration
# gate keeps static distractors unserved exactly as before.
# --------------------------------------------------------------------------- #
EDGE_X = 1150  # 0.90 of 1280 — park EDGE zone (core band ends 0.85W), where short
               # early-rise fills are height-gated out of the park tracker entirely.


def test_park_edge_meter_fallback_acquires_without_locator():
    """No locator at all: a rising edge meter must be found + served by the wide-scan
    fallback within a few frames (pre-T5 this was a full-shot roi_not_found blackout)."""
    det = MeterDetector(STYLES, _park_cfg())
    results = [det.detect(_meter_frame(EDGE_X, 20 + 8 * i, seed=7 + i)) for i in range(6)]
    assert any(_fed(r) for r in results), \
        f"edge meter never served: {[(r.detected, r.rejection_reason) for r in results]}"
    served = next(r for r in results if _fed(r))
    assert abs((served.bbox[0] + served.bbox[2] * 0.5) - (EDGE_X + MW * 0.5)) < 60, \
        f"served bbox {served.bbox} is not the edge meter at x={EDGE_X}"


def test_park_edge_meter_fallback_acquires_on_locator_miss():
    """Locator ACTIVE but blind (returns None every frame — the live v6 edge-fade failure):
    the same fallback must serve the rising edge meter."""

    class _BlindLocator:
        enabled = True
        last_conf = 0.0

        def locate(self, frame_bgr):
            return None

    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _BlindLocator()
    det._stability._locator_mode = True
    results = [det.detect(_meter_frame(EDGE_X, 20 + 8 * i, seed=7 + i)) for i in range(6)]
    assert any(_fed(r) for r in results), \
        f"edge meter never served with a blind locator: {[(r.detected, r.rejection_reason) for r in results]}"


def test_park_fallback_static_distractor_never_serves():
    """The guard that makes the restored fallback safe: a static, meter-shaped red bar
    (no rise, no green chevron) may be FOUND by the wide scan but must never corroborate,
    serve, feed, or enter meter memory."""
    rng = np.random.default_rng(3)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(fr, (900, 330), (900 + MW, 430), (30, 30, 230), -1)  # thin static red bar
    det = MeterDetector(STYLES, _park_cfg())
    for _ in range(10):
        r = det.detect(fr.copy())
        assert _fed(r) is False, f"static distractor must never feed: {r.rejection_reason!r}"
        assert r.rejection_reason != "", "static distractor must never fully validate"
    assert det._last_result is None, "an uncorroborated distractor must not enter meter memory"


def test_weak_box_fallback_unit_serves_red_without_strong_flag():
    """T5-b unit: a WEAK box over red emits from the box with loc_strong=False (no
    corroboration privilege); an empty weak box still returns None."""
    det = MeterDetector(STYLES, _park_cfg())
    red_crop = np.zeros((120, 40, 3), np.uint8)
    cv2.rectangle(red_crop, (10, 20), (30, 110), (30, 30, 230), -1)
    m = det._box_fallback_match(red_crop, (600, 300, 40, 120), 0.50, strong=False)
    assert m is not None and m.found and m.loc_strong is False
    assert m.confidence >= 0.35, f"weak-box emit must clear the trust floor (got {m.confidence})"
    grey_crop = np.full((120, 40, 3), 40, np.uint8)
    assert det._box_fallback_match(grey_crop, (600, 300, 40, 120), 0.50, strong=False) is None


def test_weak_box_over_unscannable_red_is_no_longer_invisible():
    """T5-b integration: a WEAK box over red the classical contour gates reject used to be
    discarded outright (roi_not_found blackout). Now it emits from the box — still
    corroboration-gated (may reject bbox_unstable), but VISIBLE."""
    rng = np.random.default_rng(9)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(fr, (620, 320), (700, 400), (30, 30, 230), -1)  # fat blob: wrong meter aspect
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _FakeLocator((610, 310, 100, 100), 0.50)  # WEAK (< _LOC_STRONG_CONF)
    det._stability._locator_mode = True
    r = det.detect(fr)
    assert r.detected is True, f"weak box over red must be visible, got {r.rejection_reason!r}"
    assert r.rejection_reason != "roi_not_found"


# --------------------------------------------------------------------------- #
# T6 — lock-ROI locator fast path (ORION_LOC_ROI): YOLO runs on a scale-
# preserving crop around the freshest anchor; full-frame on acquisition,
# periodic sweep, and after an unanchored miss. Flag OFF (default) is
# byte-identical — every other test in this file runs flag-off.
# --------------------------------------------------------------------------- #
class _RoiProbeLocator:
    """Crop-aware fake: returns the meter's abs box on FULL-frame calls; on crop calls
    translates it into crop coords using the offsets the test pre-computes. Records
    every call's (w, h, imgsz)."""

    def __init__(self, abs_box, offsets, conf=0.92):
        self.enabled = True
        self._abs = abs_box
        self._offsets = offsets      # {(w,h): (x1,y1)} expected crop geometries
        self._conf = float(conf)
        self.calls = []
        self.miss_crops = False

    def locate(self, frame_bgr, imgsz=None):
        h, w = frame_bgr.shape[:2]
        self.calls.append((w, h, imgsz))
        if w >= 1280:                # full frame
            return self._abs
        if self.miss_crops:
            return None
        x1, y1 = self._offsets.get((w, h), (None, None))
        assert x1 is not None, f"unexpected crop geometry {(w, h)} — offset math changed?"
        return (self._abs[0] - x1, self._abs[1] - y1, self._abs[2], self._abs[3])

    @property
    def last_conf(self):
        return self._conf


def _roi_expected_crop(cx, cy, dim, W=1280, H=720, crop=512):
    """Mirror of _locate_meter's crop maths (pinned intentionally)."""
    half = int(max(crop // 2, 1.6 * float(dim)))
    x2 = min(W, int(cx) + half); x1 = max(0, x2 - 2 * half); x2 = min(W, x1 + 2 * half)
    y2 = min(H, int(cy) + half); y1 = max(0, y2 - 2 * half); y2 = min(H, y1 + 2 * half)
    return x1, y1, x2 - x1, y2 - y1


def test_loc_roi_crop_path_serves_and_translates(monkeypatch):
    """After a full-frame acquisition, subsequent frames must run on a CROP (smaller than the
    frame) and the translated box must still serve the meter at its true position."""
    monkeypatch.setattr(_md_mod, "_LOC_ROI", True)
    fill0, rise = 30, 6
    abs_box = _loc_box(MX)
    acx, acy = abs_box[0] + abs_box[2] * 0.5, abs_box[1] + abs_box[3] * 0.5
    dim = max(abs_box[2], abs_box[3])
    offs = {}
    x1, y1, cw, ch = _roi_expected_crop(acx, acy, dim)
    offs[(cw, ch)] = (x1, y1)
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _RoiProbeLocator(abs_box, offs)
    det._stability._locator_mode = True
    results = [det.detect(_meter_frame(MX, fill0 + rise * i, seed=7 + i)) for i in range(6)]
    # frame 1 = full-frame acquisition; later frames = crops
    assert det._locator.calls[0][0] >= 1280
    crop_calls = [c for c in det._locator.calls[1:] if c[0] < 1280]
    assert crop_calls, f"no crop calls happened: {det._locator.calls}"
    assert all(c[0] <= 520 and c[1] <= 520 for c in crop_calls), f"crop too large: {crop_calls}"
    assert any(_fed(r) for r in results), "ROI path must still serve the meter"
    served = next(r for r in results if _fed(r))
    assert abs((served.bbox[0] + served.bbox[2] * 0.5) - (MX + MW * 0.5)) < 40


def test_loc_roi_unanchored_miss_forces_full_frame(monkeypatch):
    """A crop miss with NO live serve must pay one full-frame pass on the next frame."""
    monkeypatch.setattr(_md_mod, "_LOC_ROI", True)
    abs_box = _loc_box(MX)
    acx, acy = abs_box[0] + abs_box[2] * 0.5, abs_box[1] + abs_box[3] * 0.5
    x1, y1, cw, ch = _roi_expected_crop(acx, acy, max(abs_box[2], abs_box[3]))
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _RoiProbeLocator(abs_box, {(cw, ch): (x1, y1)})
    det._stability._locator_mode = True
    fr_empty = np.random.default_rng(4).integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    det.detect(_meter_frame(MX, 30))            # full-frame acquisition -> loc_mem fresh
    det._locator.miss_crops = True
    det._serve_pos = None                        # no live serve
    det.detect(fr_empty)                         # crop miss -> force_full latched
    n_before = len(det._locator.calls)
    det.detect(fr_empty)                         # must be FULL frame
    assert det._locator.calls[n_before][0] >= 1280, \
        f"post-miss frame was not full-frame: {det._locator.calls[n_before:]}"


def test_loc_roi_flag_off_always_full_frame():
    """Default flag OFF: every locator call is the full frame (byte-identical legacy path)."""
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _RoiProbeLocator(_loc_box(MX), {})
    det._stability._locator_mode = True
    for i in range(4):
        det.detect(_meter_frame(MX, 30 + 5 * i, seed=7 + i))
    assert all(c[0] >= 1280 for c in det._locator.calls), det._locator.calls
