"""Fade-tracking regression guards for the WHITE 2K27 meter (simple_meter_reader).

Grounded in session_20260831_131603 (owner verdict: "the bot isn't good at fades").
Per-frame forensics on that session's fades (Right_Fade seq9/seq16, Left_Fade epoch23)
established that the READER is NOT the fade bottleneck:

  * the meter fill VELOCITY on a fade is the same as a standstill (~0.18 %/ms) -- the
    "slow fade meter" theory is refuted by the frames;
  * the reader tracks the translating fade ribbon with ZERO rise-phase dropout even
    through ~200px of leftward pan (epoch23: box x 326->122, valid=1 throughout) at up
    to ~13 px/frame -- the ORION_METER_TRACK_CONTINUITY fix (default ON) holds;
  * the fade deficit is engine/animation side (later meter onset, contested tip-limit
    green windows, a universal ~7pp landing undershoot), not a reader defect.

These tests pin the reader behaviour the forensics relied on so a future reader edit
cannot silently regress fades. The existing continuity coverage
(test_simple_reader_lock_lifecycle::test_pan_with_stale_detector_keeps_genuine_fill)
exercises a MODERATE pan (~4 px/frame, -240 px/s); the measured fades in this session
translate ~3x faster, so this guards that faster regime explicitly, for the WHITE meter
at the live 720p geometry.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

W, H = 1280, 720          # the live capture-card geometry
STEP = 1.0 / 60.0


class _WhiteCfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


class _FakeDet:
    """Duck-typed AsyncMeterLocator: scripted per-timestamp results, like the async YOLO
    worker whose verdict is always a few frames stale on a moving meter."""
    provider = "fake"
    infer_ms = 1.0

    def __init__(self):
        self.script = {}
        self._res = (False, None, 0.0, -1.0)

    def key(self, ts):
        return round(ts, 3)

    def submit(self, frame, ts):
        self._res = self.script.get(self.key(ts), (False, None, 0.0, float(ts)))

    def latest(self):
        return self._res

    def detect_now(self, frame, ts):
        self.submit(frame, ts)
        return self._res


def _white_ribbon(x, y=300, w=24, h=110, fill=0.5):
    """A white meter ribbon at (x,y,w,h) filled from the bottom to `fill` -- the 2K27 style."""
    f = np.zeros((H, W, 3), np.uint8)
    top = y + int(round(h * (1.0 - fill)))
    f[top:y + h, x:x + w] = 255
    return f


def _mk_reader(monkeypatch, **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")   # inject the fake locator instead of a model
    monkeypatch.setenv("ORION_METER_LIFECYCLE", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = SimpleMeterReader(cfg=_WhiteCfg(), require_gameplay_eligibility=False)
    r._meter_detector = _FakeDet()
    return r


def _fade_run(monkeypatch, cont, vx_px_s, lag_s=0.10, refresh=3, n=60, x0=1000.0,
              detector_jitter_px=0.0, vy_px_s=0.0, y0=300.0,
              detector_jitter_y_px=0.0):
    """A WHITE ribbon pans at vx while its fill rises 5%->95% (a fade). The scripted
    detector refreshes every `refresh` frames and is always `lag_s` stale, reproducing the
    async-worker condition. `vx_px_s=-780` == ~13 px/frame, the fastest fade measured in
    session_20260831_131603 (epoch23 Left_Fade)."""
    r = _mk_reader(monkeypatch, ORION_METER_TRACK_CONTINUITY=cont)
    fd = r._meter_detector
    w, h = 24, 110
    lo, hi = 40, W - w - 40
    ylo, yhi = 40, H - h - 40
    out = []
    last = None
    for i in range(n):
        t = 1000.0 + i * STEP
        x_true = min(hi, max(lo, x0 + vx_px_s * (i * STEP)))
        y_true = min(yhi, max(ylo, y0 + vy_px_s * (i * STEP)))
        fill = min(0.95, 0.05 + 0.015 * i)
        if i % refresh == 0:
            x_stale = min(hi, max(lo, x0 + vx_px_s * max(0.0, (i * STEP) - lag_s)))
            y_stale = min(yhi, max(ylo, y0 + vy_px_s * max(0.0, (i * STEP) - lag_s)))
            # Learned-detector placement error is correlated across a refresh, then often
            # changes sign at the next refresh.  This is the live moving-shot case that
            # defeated the old min(first-last, pair-median) velocity rule.
            if detector_jitter_px:
                x_stale += (float(detector_jitter_px)
                            if (i // refresh) % 2 == 0 else -float(detector_jitter_px))
            if detector_jitter_y_px:
                y_stale += (float(detector_jitter_y_px)
                            if (i // refresh) % 2 == 0 else -float(detector_jitter_y_px))
            last = (True, (int(round(x_stale)), int(round(y_stale)), w, h),
                    0.9, t - lag_s)
        fd.script[fd.key(t)] = last
        r.set_shot_state(True, 1.0, True)
        res = r.detect(_white_ribbon(int(round(x_true)), int(round(y_true)), w, h, fill), ts=t)
        out.append((i, x_true, res))
    return out


def test_white_fade_fast_pan_keeps_genuine_rising_fill(monkeypatch):
    """At the measured fastest fade speed (~13 px/frame), a WHITE rising meter must be
    served with a GENUINE non-zero fill and a box that sits on the ribbon -- no hard-zero
    dropout from the translation (the concern: 'fill hard-zeroes beyond box offset')."""
    out = _fade_run(monkeypatch, cont="1", vx_px_s=-780.0)
    # frames < 24 are the tracker's bias-decay window (matches the moderate-pan continuity
    # test); steady-state serving is judged past it.
    served = [(i, xt, res) for i, xt, res in out if i >= 24 and getattr(res, "detected", False)]
    assert len(served) >= 25, f"reader stopped serving the fade meter ({len(served)} served)"
    zeros = [i for i, _, res in served if res.fill_pct <= 0.5]
    assert zeros == [], f"hard-zero fill on a tracked fade meter at frames {zeros}"
    # box must follow the ribbon (centre error bounded)
    errs = [abs((res.bbox[0] + res.bbox[2] * 0.5) - (xt + 12.0)) for _, xt, res in served]
    assert max(errs) < 16.0, f"box lost the panning ribbon (max centre err {max(errs):.1f}px)"
    # and the served fill must actually climb with the bar, not sit flat / clip low
    fills = [res.fill_pct for _, _, res in served]
    assert fills[-1] - fills[0] > 30.0, f"fade fill barely moved: {fills[0]:.1f}->{fills[-1]:.1f}"
    assert max(fills) > 80.0, f"fade peak fill clipped low: {max(fills):.1f}"


def test_white_fade_fill_velocity_matches_a_standstill(monkeypatch):
    """The forensic finding that must not silently break: a fade meter's fill RATE read by
    the reader is the same as a stationary meter's (the deficit is onset/window/undershoot,
    not a slow read). A panning rise and a stationary rise of identical true rate must be
    read with matching per-frame slope."""
    def _slope(out):
        pts = [(i, res.fill_pct) for i, _, res in out
               if getattr(res, "detected", False) and 0.5 < res.fill_pct < 92.0]
        if len(pts) < 8:
            return None
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        n = len(xs); sx = sum(xs); sy = sum(ys)
        sxx = sum(a * a for a in xs); sxy = sum(a * b for a, b in zip(xs, ys))
        return (n * sxy - sx * sy) / (n * sxx - sx * sx)

    moving = _slope(_fade_run(monkeypatch, cont="1", vx_px_s=-780.0))
    still = _slope(_fade_run(monkeypatch, cont="1", vx_px_s=0.0))
    assert moving is not None and still is not None, "insufficient reads to fit a slope"
    # same true rise rate -> slopes within 20% (translation must not depress the read rate)
    assert abs(moving - still) / still < 0.20, \
        f"fade read-rate {moving:.3f} diverges from standstill {still:.3f} %/frame"


def test_fast_fade_rejects_alternating_detector_placement_jitter(monkeypatch):
    """A delayed detector refresh may land +/-12px around a translating meter.

    Live, the previous velocity rule toggled between about -300 and -876 px/s for a
    genuine -780 px/s pan.  Its extrapolated box then strode 66-116px off the ribbon,
    hard-zeroing most fill reads.  The robust velocity fit plus match-before-refresh
    tracker must keep the 60fps position continuous instead of reproducing the slower
    detector's alternating placement error.
    """
    out = _fade_run(monkeypatch, cont="1", vx_px_s=-780.0,
                    detector_jitter_px=12.0)
    served = [(i, xt, res) for i, xt, res in out
              if i >= 24 and getattr(res, "detected", False)]
    assert len(served) >= 25
    assert [i for i, _, res in served if res.fill_pct <= 0.5] == []
    errs = [abs((res.bbox[0] + res.bbox[2] * 0.5) - (xt + 12.0))
            for _, xt, res in served]
    # The real red-court replay's tighter moving reconciliation cap improves p95/max
    # substantially at the cost of one synthetic pixel here; 9px remains inside the
    # measured 10-12px fill-read tolerance and the zero-dropout assertion above is load-bearing.
    assert max(errs) <= 9.0, f"jittered fade max centre error {max(errs):.1f}px"
    centres = [res.bbox[0] + res.bbox[2] * 0.5 for _, _, res in served]
    assert all(b <= a for a, b in zip(centres, centres[1:])), \
        "tracker reversed direction on a monotonic fade pan"


def test_diagonal_fade_tracks_vertical_camera_dive(monkeypatch):
    """A moving meter can translate vertically with the shooter/camera as well as sideways.

    Keep the match local in each axis, but predict both axes so a diagonal fade cannot
    outrun the smaller vertical search window or periodically snap back to the delayed
    detector box.
    """
    vy = -300.0  # 5px/frame: enough to outrun an x-only tracker across detector refreshes
    y0 = 500.0
    out = _fade_run(monkeypatch, cont="1", vx_px_s=-540.0, vy_px_s=vy, y0=y0,
                    detector_jitter_px=8.0, detector_jitter_y_px=8.0)
    served = [(i, xt, res) for i, xt, res in out
              if i >= 24 and getattr(res, "detected", False)]
    assert len(served) >= 25
    assert [i for i, _, res in served if res.fill_pct <= 0.5] == []
    bottom_errs = []
    bottoms = []
    for i, _, res in served:
        y_true = y0 + vy * (i * STEP)
        bottom = res.bbox[1] + res.bbox[3]
        bottoms.append(bottom)
        bottom_errs.append(abs(bottom - (y_true + 110.0)))
    assert max(bottom_errs) <= 8.0, \
        f"diagonal fade max bottom-edge error {max(bottom_errs):.1f}px"
    assert all(b <= a for a, b in zip(bottoms, bottoms[1:])), \
        "tracker reversed vertical direction on a monotonic camera dive"


def test_detector_velocity_theil_sen_rejects_one_box_hop(monkeypatch):
    """One in-gate detector hop cannot create motion on an otherwise static meter."""
    r = _mk_reader(monkeypatch)
    t0 = 1000.0
    # One 112px acquisition split is less than half of all pairwise slopes; the
    # Theil-Sen median therefore stays at the static majority instead of opening a
    # court-wide extrapolation/deviation budget.
    xs = [600.0, 600.0, 712.0, 600.0, 600.0, 600.0]
    r._det_box_hist.clear()
    for i, x in enumerate(xs):
        r._det_box_hist.append((t0 + 0.05 * i, x, 350.0, 24.0, 110.0))
    vx, vy, speed, trusted = r._det_hist_vel()
    assert trusted
    assert abs(vx) < 1.0 and abs(vy) < 1.0 and speed < 1.0


def test_detector_vertical_velocity_is_bottom_anchored_across_height_change(monkeypatch):
    """Detector height breathing cannot manufacture vertical meter motion.

    Serving, NCC and display smoothing all anchor Y to the meter's bottom edge.  A detector
    box whose bottom stays at 410 while its height grows 110->140 is therefore stationary.
    Centre-Y history would incorrectly fit -100px/s and extrapolate the fill box ~10px off the
    ribbon at a normal 100ms detector age -- almost the measured 12px dropout boundary.
    """
    r = _mk_reader(monkeypatch)
    r._det_box_hist.clear()
    t0 = 1000.0
    bottom = 410.0
    for i, height in enumerate((110.0, 120.0, 130.0, 140.0, 150.0)):
        r._det_box_hist.append((t0 + 0.05 * i, 612.0, bottom, 24.0, height))
    vx, vy, speed, trusted = r._det_hist_vel()
    assert trusted
    assert abs(vx) < 1e-9
    assert abs(vy) < 1e-9, "fixed bottom + changing height must have zero vertical velocity"
    assert abs(speed) < 1e-9


def test_motion_tracker_rejects_one_frame_duplicate_scene_hop(monkeypatch):
    """A strong duplicate patch inside the broad moving-shot search cannot reach output.

    Fast fades legitimately open the detector-deviation budget, so the old next-frame snap
    could not stop a same-frame NCC hop.  Construct an exact duplicate of the seeded bottom
    notch 34px below the current meter while the tracker predicts +10px: raw NCC must choose
    it, but its 24px innovation is beyond the motion-aware 22.5px allowance and must fall back
    to the held detector box without poisoning vertical velocity.
    """
    held = (600, 300, 24, 110)
    cx = held[0] + held[2] // 2
    bottom = held[1] + held[3]
    rng = np.random.default_rng(20260901)
    patch = rng.integers(0, 256, size=(17, 40), dtype=np.uint8)

    def put_patch(frame, patch_bottom):
        x0 = cx - patch.shape[1] // 2
        y0 = int(patch_bottom) - 12
        frame[y0:y0 + patch.shape[0], x0:x0 + patch.shape[1]] = patch[:, :, None]

    seed = np.zeros((H, W, 3), np.uint8)
    put_patch(seed, bottom)
    distractor = np.zeros_like(seed)
    put_patch(distractor, bottom + 34)

    def prepared_reader():
        reader = _mk_reader(monkeypatch)
        reader.W, reader.H = W, H
        reader._seed_track_template(seed, held)
        assert reader._det_tmpl is not None and reader._det_tmpl_std > reader._det_tmpl_std_min
        reader._det_tmpl_ts = 1000.0
        reader._det_track_box = held
        reader._det_track_vx = 0.0
        reader._det_track_vy = 10.0
        reader._det_fresh_accept = False
        # A genuine moving-shot history opens the old deviation cap wide enough that the raw
        # +34px hop would otherwise be served.  Histories are centre-X + BOTTOM-Y.
        reader._det_box_hist.clear()
        for i in range(6):
            reader._det_box_hist.append(
                (999.75 + 0.05 * i, float(cx), float(bottom - 15 * (5 - i)), 24.0, 110.0))
        assert reader._det_hist_speed() > 250.0
        return reader

    raw_reader = prepared_reader()
    # The continuity matcher now excludes off-path global peaks before selecting
    # its winner; the legacy primitive still demonstrates the raw NCC argmax.
    raw_reader._det_track_cont = False
    raw_reader._det_track_pad_y = 35  # same window as the +10px continuity path
    raw = raw_reader._track_box_ncc(distractor, held)
    assert raw[1] + raw[3] == bottom + 34
    assert raw_reader._det_track_score >= raw_reader._det_track_min

    guarded = prepared_reader()
    out = guarded._det_track_step(distractor, held, 1000.25)
    assert out == held, "implausible strong match must fall back before it reaches fill/output"
    assert guarded._det_track_score == 0.0
    assert guarded._det_track_vx == 0.0
    assert guarded._det_track_vy == 10.0, "rejected candidate must not poison motion state"


def test_trusted_motion_caps_reconcile_pull_from_stale_detector(monkeypatch):
    """A strong current-frame match must not be dragged back toward a stale fade box.

    At the live 0.15 gain a 50px detector-age gap used to pull the matched box 7.5px
    backward in one frame.  Trusted motion caps that correction to 2px while keeping
    the detector deviation bound and innovation guard intact.
    """
    r = _mk_reader(monkeypatch)
    r.W, r.H = W, H
    held = (600, 300, 24, 110)
    matched = (650, 300, 24, 110)
    r._det_track_box = matched
    r._det_track_snap_px = 100.0
    r._det_track_dev_base = 100.0
    r._det_tmpl = np.ones((17, 40), np.uint8)
    r._det_tmpl_std = r._det_tmpl_std_min + 1.0
    r._det_fresh_accept = False

    def strong_match(_frame, _ref):
        r._det_track_score = 1.0
        return matched

    r._track_box_ncc = strong_match
    r._det_hist_vel = lambda: (600.0, 0.0, 600.0, True)
    out = r._det_track_step(np.zeros((H, W, 3), np.uint8), held, 1000.0)
    assert out[0] == matched[0] - 2, out


def test_static_track_keeps_full_reconcile_gain(monkeypatch):
    """The moving cap must not weaken detector correction on an untrusted/static lock."""
    r = _mk_reader(monkeypatch)
    r.W, r.H = W, H
    held = (600, 300, 24, 110)
    matched = (620, 300, 24, 110)
    r._det_track_box = matched
    r._det_track_snap_px = 100.0
    r._det_track_dev_base = 100.0
    r._det_tmpl = np.ones((17, 40), np.uint8)
    r._det_tmpl_std = r._det_tmpl_std_min + 1.0
    r._det_fresh_accept = False

    def strong_match(_frame, _ref):
        r._det_track_score = 1.0
        return matched

    r._track_box_ncc = strong_match
    r._det_hist_vel = lambda: (0.0, 0.0, 0.0, False)
    out = r._det_track_step(np.zeros((H, W, 3), np.uint8), held, 1000.0)
    assert out[0] == matched[0] - 3, out       # 0.15 * 20px, unchanged static gain


def test_strong_ncc_bottom_writes_through_emit_slew(monkeypatch):
    """Diagonal fades get the same strong-match write-through on Y as on X."""
    r = _mk_reader(monkeypatch)
    r.W, r.H = W, H
    r._det_emit = [612.0, 410.0, 24.0, 110.0]
    r._det_tmpl = np.ones((17, 40), np.uint8)
    r._det_track_score = 1.0

    out = r._det_smooth_emit((613, 320, 24, 110))   # centre=625, bottom=430
    assert out[0] + out[2] * 0.5 == pytest.approx(625.0, abs=0.5)
    assert out[1] + out[3] == 430


@pytest.mark.parametrize("fill", [float("nan"), float("inf"), -1.0, 101.0])
def test_armed_publish_never_promotes_invalid_fill(monkeypatch, fill):
    r = _mk_reader(monkeypatch)
    r._shot_armed_hw = True
    assert not r._idle_publish_ok(True, (600, 300, 26, 107), fill, 1.0)


def test_publish_gate_error_is_not_meter_evidence(monkeypatch):
    r = _mk_reader(monkeypatch)
    r._shot_armed_hw = True
    r._idle_pub_fills = None  # controlled broken state; never manufacture a valid sample
    assert not r._idle_publish_ok(True, (600, 300, 26, 107), 20.0, 1.0)


@pytest.mark.parametrize("bbox,ts", [
    (None, 1.0), ((600, 300, 0, 107), 1.0),
    ((float("nan"), 300, 26, 107), 1.0),
    ((600, 300, float("inf"), 107), 1.0),
    ((600, 300, 26, 107), float("nan")),
    ((600, 300, 26, 107), float("inf")),
])
def test_armed_publish_never_promotes_invalid_geometry_or_time(monkeypatch, bbox, ts):
    r = _mk_reader(monkeypatch)
    r._shot_armed_hw = True
    assert not r._idle_publish_ok(True, bbox, 20.0, ts)
    assert r._idle_pub_box is None


@pytest.mark.parametrize("ts", [None, 1.0])
def test_armed_publish_keeps_valid_measurements(monkeypatch, ts):
    r = _mk_reader(monkeypatch)
    r._shot_armed_hw = True
    assert r._idle_publish_ok(True, (600, 300, 26, 107), 20.0, ts)
