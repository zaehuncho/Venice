"""CompressedMeterReader: chroma-first collapse, K2 luma fill fallback, encoder-skip stale gate.

The collapse rule is the load-bearing contract: on pristine (chroma-alive) frames this reader
must behave EXACTLY like SimpleMeterReader -- the luma branches are dead code there. The luma
fill path may only replace a read the chroma path would MISS outright.
"""
import os

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader, ReaderParams
from compressed_meter_reader import CompressedMeterReader

W, H = 1920, 1080
COL_X, COL_W = 900, 24
TRACK_TOP_Y, FLOOR_Y = 470, 600
RED_A, RED_B = (0, 0, 255), (40, 40, 255)
GREEN = (60, 200, 60)


def _chroma_frame(fill_frac: float) -> np.ndarray:
    """The standard synthetic CHROMA meter (textured red column + green cap), matching
    tests/test_simple_meter_reader.py's fixture geometry."""
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = (60, 60, 60)
    track_h = FLOOR_Y - TRACK_TOP_Y
    top = FLOOR_Y - int(track_h * fill_frac)
    if top < FLOOR_Y:
        f[top:FLOOR_Y, COL_X:COL_X + COL_W // 2] = RED_A
        f[top:FLOOR_Y, COL_X + COL_W // 2:COL_X + COL_W] = RED_B
    f[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X:COL_X + COL_W] = GREEN
    return f


def _luma_frame(fill_frac: float) -> np.ndarray:
    """The same meter with CHROMA DESTROYED (everything neutral grey, structure in Y only):
    dark track, bright bevel rails, mid-luma fill, bright tip sliver. Red/green inRange find
    NOTHING here -- only the luma path can read it."""
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = (60, 60, 60)
    track_h = FLOOR_Y - TRACK_TOP_Y
    f[TRACK_TOP_Y:FLOOR_Y, COL_X:COL_X + COL_W] = (25, 25, 25)          # empty track
    f[TRACK_TOP_Y:FLOOR_Y, COL_X - 2:COL_X] = (190, 190, 190)           # left bevel rail
    f[TRACK_TOP_Y:FLOOR_Y, COL_X + COL_W:COL_X + COL_W + 2] = (190, 190, 190)
    top = FLOOR_Y - int(track_h * fill_frac)
    if top < FLOOR_Y:
        f[top:FLOOR_Y, COL_X:COL_X + COL_W] = (90, 90, 90)              # fill body (red-in-Y)
    f[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X:COL_X + COL_W] = (160, 160, 160)  # tip sliver
    return f


def _lock_col(fill_frac: float):
    """The red-column box a lock would carry for the given fill (x, y, w, h)."""
    track_h = FLOOR_Y - TRACK_TOP_Y
    top = FLOOR_Y - int(track_h * fill_frac)
    return (COL_X, top, COL_W, FLOOR_Y - top)


# --------------------------------------------------------------------------- #
#  chroma-first collapse
# --------------------------------------------------------------------------- #
def test_collapse_parity_on_chroma_frames():
    """On chroma-alive frames the compressed reader's read() must equal SimpleMeterReader's,
    field for field, across a whole rise."""
    a = SimpleMeterReader(W, H)
    b = CompressedMeterReader(W, H)
    for k, frac in enumerate([0.0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0]):
        fr = _chroma_frame(frac)
        ts = k / 60.0
        ra = a.read(fr, ts=ts)
        rb = b.read(fr, ts=ts)
        for key in ("detected", "fill", "fill_coarse", "bbox", "stage",
                    "confidence", "rejection_reason", "rise_state", "green"):
            assert ra.get(key) == rb.get(key), (key, frac, ra.get(key), rb.get(key))


def test_params_passthrough():
    r = CompressedMeterReader(W, H, params=ReaderParams(ncc_lock=0.5))
    assert r.NCC_LOCK == 0.5


# --------------------------------------------------------------------------- #
#  K2 luma fill read
# --------------------------------------------------------------------------- #
def test_luma_fill_read_matches_geometry():
    r = CompressedMeterReader(W, H)
    for frac in (0.3, 0.5, 0.7, 0.9):
        fr = _luma_frame(frac)
        col = _lock_col(frac)
        coarse, subpix, tbox, top_row, green = r._read_fill(fr, col)
        # analytic fill on the strip scale: floor+3 clamp mirrors the chroma path
        strip_bottom = FLOOR_Y + 3
        sliver_bottom = TRACK_TOP_Y            # tip sliver ends at the track top
        fillable = strip_bottom - sliver_bottom
        expected = (strip_bottom - (FLOOR_Y - (FLOOR_Y - TRACK_TOP_Y) * frac)) / fillable * 100.0
        assert r._luma_diag.get("fill_path") == "luma"
        assert subpix == pytest.approx(expected, abs=3.0), (frac, subpix, expected)
    assert r._luma_diag.get("sig_width") is not None


def test_luma_fill_monotonic_over_rise():
    r = CompressedMeterReader(W, H)
    fills = []
    for frac in np.linspace(0.15, 0.95, 12):
        _, subpix, _, _, _ = r._read_fill(_luma_frame(float(frac)), _lock_col(float(frac)))
        fills.append(subpix)
    assert all(b >= a - 1.0 for a, b in zip(fills, fills[1:])), fills


def test_luma_tip_sliver_emits_pinned_green_window():
    """Anchor stabilizer: a chroma-dead sliver needs TIP_PERSIST_MIN stable frames before
    the green window is emitted (ladder eval: single-frame sliver green over-fires on
    low-bitrate blocking). Geometry (fillable_h) is anchored from frame one regardless."""
    r = CompressedMeterReader(W, H)
    green = None
    for _ in range(r.TIP_PERSIST_MIN + 1):
        _, _, _, _, green = r._read_fill(_luma_frame(0.6), _lock_col(0.6))
    assert green is not None
    g_start, g_end = green[0], green[1]
    assert g_end == 100.0 and 80.0 <= g_start < 100.0


def test_luma_green_gated_on_first_sight_without_chroma():
    """A single-frame luma sliver with NO chroma corroboration must NOT emit green (the
    over-fire class); the fill read itself is untouched."""
    r = CompressedMeterReader(W, H)
    _, subpix, _, _, green = r._read_fill(_luma_frame(0.6), _lock_col(0.6))
    assert green is None
    assert subpix > 0.0
    assert r._luma_diag.get("fill_path") == "luma"


def test_luma_green_immediate_with_chroma_corroboration():
    """A sliver whose rows also carry REAL chroma green must emit immediately (the
    corroborated case: high-bitrate where the mask partially survives)."""
    f = _luma_frame(0.6)
    f[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X:COL_X + COL_W] = GREEN   # real green cap
    r = CompressedMeterReader(W, H)
    _, _, _, _, green = r._read_fill(f, _lock_col(0.6))
    assert green is not None and green[1] == 100.0


def test_luma_rejects_capped_bright_interior():
    """Capped meter: interior all-bright (green-in-Y ~150). The luma path must REJECT (the
    base red-miss -> green-only-hold upstream is the correct behavior), never read the bright
    interior as fill."""
    f = _luma_frame(0.0)
    f[TRACK_TOP_Y:FLOOR_Y, COL_X:COL_X + COL_W] = (150, 150, 150)
    r = CompressedMeterReader(W, H)
    coarse, subpix, _, top_row, _ = r._read_fill(f, (COL_X, TRACK_TOP_Y, COL_W, FLOOR_Y - TRACK_TOP_Y))
    assert top_row == -1 and subpix == 0.0
    assert r._luma_diag.get("fill_path") == "luma_reject"


# --------------------------------------------------------------------------- #
#  stale gate (encoder-skip duplicates are missing data)
# --------------------------------------------------------------------------- #
def _rise_to_lock(r, until=0.7, n=10):
    """Drive a synthetic chroma rise so the reader is locked with high velocity."""
    ts = 0.0
    for k, frac in enumerate(np.linspace(0.2, until, n)):
        ts = k / 60.0
        out = r.read(_chroma_frame(float(frac)), ts=ts)
    return out, ts


def test_stale_gate_holds_on_frozen_pixels_while_rising():
    r = CompressedMeterReader(W, H)
    out, ts = _rise_to_lock(r)
    assert out["detected"] and out["velocity_pct_s"] > 40.0
    frozen = _chroma_frame(0.7)
    r.read(frozen, ts=ts + 1 / 60.0)              # first repeat: stores the ROI baseline
    before_fill = r.last_fill
    before_conf = r.conf
    before_vel = r._velocity
    out2 = r.read(frozen, ts=ts + 2 / 60.0)       # second repeat: SAD ~0 while vel high
    assert out2["rejection_reason"] == "stale_frame"
    assert out2["stage"] == "stale"
    assert out2["fill"] == before_fill
    assert r.conf == before_conf                  # NO confidence decay on missing data
    assert r._velocity == before_vel              # NO velocity append

    # a genuinely new frame resumes normal reads
    out3 = r.read(_chroma_frame(0.8), ts=ts + 3 / 60.0)
    assert out3["rejection_reason"] != "stale_frame"
    assert out3["fill"] > before_fill


def test_stale_run_cap_prevents_frozen_echo():
    """A genuine scene change (capture freeze / cut to static content) must NOT hold the
    fill forever: velocity is frozen while stale, so without the run cap the stale gate
    deadlocks -- the frozen-echo failure class. Past STALE_RUN_MAX the frames must fall
    through to the honest decay/drop cascade."""
    r = CompressedMeterReader(W, H, luma_tracking=True)
    out, ts = _rise_to_lock(r)
    assert out["velocity_pct_s"] > 40.0
    static_scene = np.full((H, W, 3), 45, np.uint8)      # meter gone, scene static
    dropped = False
    for k in range(1, r.STALE_RUN_MAX + 30):
        out = r.read(static_scene, ts=ts + k / 60.0)
        if not out["detected"]:
            dropped = True
            break
    assert dropped, "stale gate held a vanished meter indefinitely (frozen echo)"


def test_static_meter_is_not_stale():
    """A parked meter (velocity ~0) repeating identical pixels is a VALID zero-motion
    observation, not encoder skip."""
    r = CompressedMeterReader(W, H)
    fr = _chroma_frame(0.5)
    for k in range(12):
        out = r.read(fr, ts=k / 60.0)
    assert out["rejection_reason"] != "stale_frame"
    assert abs(out["velocity_pct_s"]) < 20.0


def test_duplicate_timestamp_is_stale_when_locked_and_rising():
    r = CompressedMeterReader(W, H)
    out, ts = _rise_to_lock(r)
    frozen = _chroma_frame(0.7)
    r.read(frozen, ts=ts + 1 / 60.0)
    out2 = r.read(frozen, ts=ts + 1 / 60.0)       # same PTS: duplicate frame
    assert out2["rejection_reason"] == "stale_frame"


# --------------------------------------------------------------------------- #
#  K1 luma acquisition (rails + evidence + E4 + registry)
# --------------------------------------------------------------------------- #
def test_luma_acquire_and_track_full_rise_armed():
    """End-to-end on chroma-dead frames: armed reader must acquire via the rail pair,
    confirm on the rise (E4), and read an increasing fill."""
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    fills = []
    for k, frac in enumerate(np.linspace(0.25, 0.9, 10)):
        out = r.read(_luma_frame(float(frac)), ts=k / 60.0)
        if out["detected"] and out["stage"] != "coast":
            fills.append(out["fill"])
    assert len(fills) >= 6, "luma acquisition failed to lock"
    assert fills[-1] > fills[0] + 30.0
    assert r._luma_pending == 0                    # E4 confirmed by the rise


def test_luma_lock_without_rise_is_zeroed_e4():
    """A standing rail pair that never rises while armed is décor: the E4 confirm must
    zero the lock within ~4 frames."""
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    fr = _luma_frame(0.5)                          # static fill
    detected_late = None
    for k in range(10):
        out = r.read(fr, ts=k / 60.0)
        detected_late = out
    assert r.box is None                            # lock zeroed
    assert detected_late["detected"] is False or detected_late["stage"] == "no_meter"


def test_decor_rails_without_structure_never_acquire():
    """Bright rail pair with a dark flat interior but NO tip sliver and NO fill step
    (a window mullion): no acquisition, armed or not."""
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = (60, 60, 60)
    f[300:640, COL_X:COL_X + COL_W] = (25, 25, 25)
    f[300:640, COL_X - 2:COL_X] = (190, 190, 190)
    f[300:640, COL_X + COL_W:COL_X + COL_W + 2] = (190, 190, 190)
    for armed in (False, True):
        r = CompressedMeterReader(W, H)
        r.set_shot_state(armed, 1.0)
        for k in range(6):
            out = r.read(f, ts=k / 60.0)
        assert out["detected"] is False, f"armed={armed} locked onto structureless rails"


def test_registry_demands_full_structure_from_standing_pairs():
    """A rail pair recorded in the pre-arm registry must need BOTH sliver and fill step
    to acquire while armed (fill step alone suffices for a fresh pair)."""
    fill_only = _luma_frame(0.5)
    fill_only[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X:COL_X + COL_W] = (60, 60, 60)  # no sliver
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    assert r._luma_acquire(fill_only) is not None   # fresh pair: E3 alone is enough armed
    r2 = CompressedMeterReader(W, H)
    r2._last_read_ts = 0.0
    r2._update_registry(fill_only, 0.0)
    assert r2._registry_match(COL_X + COL_W / 2, COL_W + 3, 0.0) or r2._decor_registry
    r2.set_shot_state(True, 1.0)
    assert r2._luma_acquire(fill_only) is None      # registered pair: needs E2 AND E3


def test_luma_rails_relocate_holds_lock_when_chroma_dies():
    """Lock on a chroma rise, then feed the SAME meter with chroma destroyed: the rails
    relocate step must keep the box glued (evidence 'rails')."""
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    ts = 0.0
    for k, frac in enumerate(np.linspace(0.2, 0.6, 8)):
        ts = k / 60.0
        r.read(_chroma_frame(float(frac)), ts=ts)
    assert r.box is not None
    out = r.read(_luma_frame(0.65), ts=ts + 1 / 60.0)
    assert out["detected"] is True
    assert out["stage"] != "no_meter"


def test_acquire_hint_contract_never_locks_alone():
    """A hint over empty background must not create a lock (relax-never-suppress)."""
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    flat = np.full((H, W, 3), 60, np.uint8)
    r._last_read_ts = 0.0
    r.set_acquire_hint((500, 400, 30, 120), 0.9, ts=0.0)
    assert r._luma_acquire(flat) is None


# --------------------------------------------------------------------------- #
#  quality layer: rung prior seeding (bug #1) + the UNARMED luma acquire (2a)
# --------------------------------------------------------------------------- #
def test_stream_profile_seeds_quality_band():
    """Bug #1: without the profile the estimator starts PRISTINE; the 720p/4M rung must
    seed q_session into the degraded bands so the luma branches can gate ON."""
    r = CompressedMeterReader(W, H)
    assert r._qe.q_session == 1.0
    r.set_stream_profile(1280, 720, 60.0, 4000.0)
    assert r._qe.q_session <= r.UNARMED_ACQ_Q_MAX


def test_unarmed_luma_acquire_quality_gated_full_rise():
    """UNARMED acquire (2a): with the 4M rung prior seeded and chroma dead for the
    required run, the rail acquire must lock the rising luma meter PRE-ARM, confirm via
    E4, and read an increasing fill."""
    r = CompressedMeterReader(W, H)
    r.set_stream_profile(1280, 720, 60.0, 4000.0)
    k = 0
    # chroma-dead warmup on empty frames (also lets the decor registry tick)
    flat = np.full((H, W, 3), 60, np.uint8)
    for _ in range(r.UNARMED_CHROMA_DEAD_MIN + 2):
        r.read(flat, ts=k / 60.0)
        k += 1
    fills = []
    for frac in np.linspace(0.25, 0.9, 14):
        out = r.read(_luma_frame(float(frac)), ts=k / 60.0)
        k += 1
        if out["detected"] and out["stage"] != "coast":
            fills.append(out["fill"])
    assert len(fills) >= 6, "unarmed luma acquisition failed to lock"
    assert fills[-1] > fills[0] + 25.0
    assert r._luma_pending == 0                    # E4 confirmed by the rise


def test_unarmed_luma_acquire_off_when_pristine():
    """The collapse guard: with NO rung profile (pristine prior) the unarmed path must
    stay off no matter how long chroma is dead -- armed-only behaviour is unchanged."""
    r = CompressedMeterReader(W, H)
    k = 0
    flat = np.full((H, W, 3), 60, np.uint8)
    for _ in range(r.UNARMED_CHROMA_DEAD_MIN + 2):
        r.read(flat, ts=k / 60.0)
        k += 1
    for frac in np.linspace(0.25, 0.9, 14):
        r.read(_luma_frame(float(frac)), ts=k / 60.0)
        k += 1
    assert r.box is None


def test_unarmed_luma_acquire_nonrising_zeroed_e4():
    """UNARMED + static luma structure: E4 must zero the lock (decor), same as armed."""
    r = CompressedMeterReader(W, H)
    r.set_stream_profile(1280, 720, 60.0, 4000.0)
    k = 0
    flat = np.full((H, W, 3), 60, np.uint8)
    for _ in range(r.UNARMED_CHROMA_DEAD_MIN + 2):
        r.read(flat, ts=k / 60.0)
        k += 1
    fr = _luma_frame(0.5)                          # static fill
    for _ in range(10):
        r.read(fr, ts=k / 60.0)
        k += 1
    assert r.box is None


# --------------------------------------------------------------------------- #
#  MeterReaderY.verify() confirmer (bug #2): relax-never-suppress
# --------------------------------------------------------------------------- #
class _FakeReaderY:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def verify(self, y_crop):
        self.calls += 1
        return self.answer


def _mullion_frame():
    """Bright rail pair, dark flat interior, NO sliver, NO fill step (a window mullion).
    Rails are meter-sized (130 rows) so the pair PASSES the rail extent gates; the dark
    interior RUNS PAST the rail bottom so the evidence window sees no dark->mid step at
    all (a dark column ENDING at the rail floor reads as a legitimate E3 step against
    the brighter background) -- only e2/e3 fail, the exact case the Y-student confirms."""
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = (60, 60, 60)
    f[TRACK_TOP_Y:FLOOR_Y + 60, COL_X:COL_X + COL_W] = (25, 25, 25)
    f[TRACK_TOP_Y:FLOOR_Y, COL_X - 2:COL_X] = (190, 190, 190)
    f[TRACK_TOP_Y:FLOOR_Y, COL_X + COL_W:COL_X + COL_W + 2] = (190, 190, 190)
    return f


def test_reader_y_verify_confirms_scan_but_never_locks():
    """verify()==True on a structureless rail pair may only SHRINK the scan (feed the
    acquire hint) -- it must never create a lock (rails + evidence + E4 still required)."""
    f = _mullion_frame()
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    r._reader_y = _FakeReaderY(True)
    out = None
    for k in range(6):
        out = r.read(f, ts=k / 60.0)
    assert out["detected"] is False                # confirmer can never lock alone
    assert r._acq_hint is not None                 # but it DID shrink the scan
    assert r._reader_y.calls >= 1


def test_reader_y_verify_false_suppresses_nothing():
    """verify()==False must change NOTHING: a candidate passing the classical gates still
    locks (relax-never-suppress), and a failing one stays failed with no hint."""
    r = CompressedMeterReader(W, H)
    r.set_shot_state(True, 1.0)
    r._reader_y = _FakeReaderY(False)
    out = None
    for k, frac in enumerate(np.linspace(0.25, 0.9, 10)):
        out = r.read(_luma_frame(float(frac)), ts=k / 60.0)
    assert out["detected"] is True                 # classical lock unaffected by False
    r2 = CompressedMeterReader(W, H)
    r2.set_shot_state(True, 1.0)
    r2._reader_y = _FakeReaderY(False)
    for k in range(6):
        r2.read(_mullion_frame(), ts=k / 60.0)
    assert r2._acq_hint is None                    # no confirm -> no hint, no lock


# --------------------------------------------------------------------------- #
#  collapse regression on REAL capture-card framedumps (skipped when absent)
# --------------------------------------------------------------------------- #
_SESSION = os.path.join(os.path.dirname(__file__), "..", "logs", "diagnostics",
                        "framedump", "session_20260704_210801")


def _run_pair(pngs, luma_tracking):
    import cv2
    a = SimpleMeterReader()
    b = CompressedMeterReader(luma_tracking=luma_tracking)
    rows = []
    for k, p in enumerate(pngs):
        fr = cv2.imread(p)
        if fr is None:
            continue
        ts = k * (64.6 / 1000.0)
        rows.append((os.path.basename(p), a.read(fr, ts=ts), b.read(fr, ts=ts)))
    return rows


@pytest.mark.skipif(not os.path.isdir(_SESSION), reason="framedump session not on disk")
def test_collapse_parity_on_real_framedumps_luma_off():
    """The collapse row: with luma tracking gated OFF (what M4's q_session=1 will do on a
    pristine source) the compressed reader must be BYTE-IDENTICAL to SimpleMeterReader on
    real capture-card frames -- proving the chroma paths are untouched and the stale gate
    never fires on genuinely fresh frames."""
    import glob
    pngs = sorted(glob.glob(os.path.join(_SESSION, "f*_raw.png")))[:150]   # contiguous ~10s
    assert pngs, "no frames"
    mismatches = []
    for name, ra, rb in _run_pair(pngs, luma_tracking=False):
        for key in ("detected", "fill", "bbox", "stage", "rejection_reason"):
            if ra.get(key) != rb.get(key):
                mismatches.append((name, key, ra.get(key), rb.get(key)))
    assert not mismatches, mismatches[:8]


@pytest.mark.skipif(not os.path.isdir(_SESSION), reason="framedump session not on disk")
def test_luma_tracking_on_real_framedumps_only_adds():
    """With luma tracking ON (the compressed-path default) divergence from the base is
    allowed ONLY as addition: at least as many detections, divergence bounded, and every
    detected fill still sane. (Byte parity is impossible by construction here: a luma hold
    on a frame the base coasts changes the box history downstream.)"""
    import glob
    pngs = sorted(glob.glob(os.path.join(_SESSION, "f*_raw.png")))[:150]   # contiguous ~10s
    assert pngs, "no frames"
    rows = _run_pair(pngs, luma_tracking=True)
    det_a = sum(1 for _, ra, _ in rows if ra.get("detected"))
    det_b = sum(1 for _, _, rb in rows if rb.get("detected"))
    diverged = sum(1 for _, ra, rb in rows
                   if (ra.get("detected"), round(ra.get("fill", 0), 1))
                   != (rb.get("detected"), round(rb.get("fill", 0), 1)))
    assert det_b >= det_a, (det_a, det_b)
    assert diverged <= max(6, int(0.10 * len(rows))), (diverged, len(rows))
