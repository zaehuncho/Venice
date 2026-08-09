"""Plan B2 robustness coverage (ORION_READER_ROBUST, default OFF = byte-identical reader):

* occlusion-tolerant fill read — 20th PERCENTILE of per-column tops (definition-preserving:
  exact equivalence on clean frames; survives heavy column occlusion where the row-mean read
  breaks),
* the trajectory gate ('fill_gated' emission, 6-rejection cap then accept+reset),
* dead-reckoned coast ('dead_reckoned', fit >= 8 samples, 10-frame cap, killed on disarm),
* the relocate-window right-edge clip (the NBA-banner decor-lock path [fix]),
* the feed tiers: _should_feed_engine gains the sampler tier, _is_raw_accepted excludes it,
* hw-arm gate separation (physical vs CV self-arm) + the 3-arg set_shot_state push guard.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

H, W = 1080, 1920
FLOOR_Y = 600
TRACK_TOP_Y = 470
COL_X = 900
COL_W = 24
RED_A = (0, 0, 255)
RED_B = (40, 40, 255)
GREEN = (60, 200, 60)


def _frame(fill_frac=1.0, col_x=COL_X, col_w=COL_W, green=True, occlude_cols=0, occl_depth=40):
    f = np.full((H, W, 3), 40, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    red_top = int(round(FLOOR_Y - fill_frac * track_h))
    half = col_w // 2
    if fill_frac > 0:
        f[red_top:FLOOR_Y, col_x:col_x + half] = RED_A
        f[red_top:FLOOR_Y, col_x + half:col_x + col_w] = RED_B
    if occlude_cols > 0:
        # a hand/limb over the LEFT occlude_cols columns: their top-of-red drops by occl_depth
        f[red_top:red_top + occl_depth, col_x:col_x + occlude_cols] = (90, 90, 90)
    if green:
        f[TRACK_TOP_Y - 8:TRACK_TOP_Y, col_x:col_x + col_w] = GREEN
    return f


def _robust_reader(monkeypatch):
    monkeypatch.setenv("ORION_READER_ROBUST", "1")
    return SimpleMeterReader(W, H)


# --------------------------------------------------------------------------- #
#  occlusion-tolerant fill read
# --------------------------------------------------------------------------- #
def test_percentile_fill_read_equivalent_on_clean_frames(monkeypatch):
    """Definition-preserving [fix]: the 20th-percentile column-top read must equal the legacy
    0.20 row-mean crossing on clean frames — a median would shift the fill scale 1-3pp and
    every EMA-dialled timing constant with it."""
    for frac in (0.35, 0.55, 0.75, 0.95):
        monkeypatch.delenv("ORION_READER_ROBUST", raising=False)
        legacy = SimpleMeterReader(W, H).read(_frame(frac), ts=0.0)
        robust = _robust_reader(monkeypatch).read(_frame(frac), ts=0.0)
        assert legacy["detected"] and robust["detected"]
        assert robust["fill_coarse"] == pytest.approx(legacy["fill_coarse"], abs=0.75), frac


def test_percentile_fill_read_survives_column_occlusion(monkeypatch):
    """19 of 24 columns occluded 40px down: the legacy row-mean read under-reads by ~30pp
    (5/30 red columns < the 0.20 row threshold at the true top); the 20th-percentile column
    read stays on the true fill."""
    frame = _frame(0.85, occlude_cols=19)
    truth = SimpleMeterReader(W, H).read(_frame(0.85), ts=0.0)["fill_coarse"]
    monkeypatch.delenv("ORION_READER_ROBUST", raising=False)
    # Explicit "0", not delenv: PCTL_FILL shipped default-ON 2026-08-04 (it had been ON only via
    # the gitignored dev launcher, so customers ran an unmeasured read path). Unsetting the var no
    # longer selects the legacy read, so the opt-out has to be explicit to isolate it.
    monkeypatch.setenv("ORION_READER_PCTL_FILL", "0")   # isolate the true legacy (non-pctl) path
    legacy = SimpleMeterReader(W, H).read(frame, ts=0.0)
    robust = _robust_reader(monkeypatch).read(frame, ts=0.0)
    assert robust["detected"]
    assert abs(robust["fill_coarse"] - truth) <= 2.0, (robust["fill_coarse"], truth)
    if legacy["detected"]:
        assert abs(legacy["fill_coarse"] - truth) > 10.0   # the failure the new read fixes
    # occlusion surfaced in diagnostics
    assert robust.get("stage") in ("acquire", "track")


# --------------------------------------------------------------------------- #
#  trajectory gate
# --------------------------------------------------------------------------- #
class _FakeFitProvider:
    def __init__(self):
        self.resp = None

    def __call__(self, t_ms):
        return self.resp


def test_trajectory_gate_rejects_glitch_then_caps_at_six(monkeypatch):
    r = _robust_reader(monkeypatch)
    fit = _FakeFitProvider()
    r.set_fit_provider(fit)
    # frame 0: fill ~60, fit agrees -> accepted
    fit.resp = {"fill": 60.0, "vel_pp_ms": 0.18, "sigma_pp": 1.0, "n": 8, "conf": 0.8, "age_ms": 5.0}
    out0 = r.read(_frame(0.6), ts=0.0)
    assert out0["rejection_reason"] == "green_not_found"
    base_fill = out0["fill"]
    # glitch frames: real read says ~90 while the fit says 60 -> tau ~4.25 -> gated
    reasons, fills = [], []
    for i in range(1, 8):
        out = r.read(_frame(0.9), ts=i / 60.0)
        reasons.append(out["rejection_reason"])
        fills.append(out["fill"])
    assert reasons[:6] == ["fill_gated"] * 6          # 6 consecutive rejections...
    assert reasons[6] == "green_not_found"            # ...then accept + reset (the cap)
    for f_ in fills[:6]:
        assert f_ == pytest.approx(base_fill, abs=1.0)  # gated frames HOLD the accepted fill
    assert fills[6] > 85.0                            # the capped accept takes the new truth


def test_trajectory_gate_prefit_rate_gate_allows_dup_double_step(monkeypatch):
    """Pre-fit rate gate measures from the last ACCEPTED sample: a double-step after a skipped/
    duplicate frame passes because dt doubled [fix]."""
    r = _robust_reader(monkeypatch)                   # no fit provider -> pre-fit branch
    r.read(_frame(0.40), ts=0.0)
    # a 33.4ms gap (one dropped frame) with a double fill step: allowance 0.75*33.4+3.5 ~ 28.6pp
    out = r.read(_frame(0.55), ts=2 / 60.0)           # +15pp over 2 frame times -> passes
    assert out["rejection_reason"] == "green_not_found"
    # an impossible tear (+45pp in one frame time) is gated
    out2 = r.read(_frame(0.95), ts=3 / 60.0)
    assert out2["rejection_reason"] == "fill_gated"


def test_trajectory_gate_off_without_flag(monkeypatch):
    monkeypatch.delenv("ORION_READER_ROBUST", raising=False)
    r = SimpleMeterReader(W, H)
    fit = _FakeFitProvider()
    r.set_fit_provider(fit)
    fit.resp = {"fill": 60.0, "vel_pp_ms": 0.18, "sigma_pp": 1.0, "n": 8, "conf": 0.8, "age_ms": 5.0}
    r.read(_frame(0.6), ts=0.0)
    out = r.read(_frame(0.9), ts=1 / 60.0)
    assert out["rejection_reason"] == "green_not_found"   # default behaviour untouched


# --------------------------------------------------------------------------- #
#  dead-reckoned coast
# --------------------------------------------------------------------------- #
def test_dead_reckoned_coast_caps_and_dies_on_disarm(monkeypatch):
    r = _robust_reader(monkeypatch)
    fit = _FakeFitProvider()
    r.set_fit_provider(fit)
    r.set_shot_state(True, 1.0, True)
    fit.resp = {"fill": 60.0, "vel_pp_ms": 0.18, "sigma_pp": 1.0, "n": 8, "conf": 0.8, "age_ms": 5.0}
    assert r.read(_frame(0.6), ts=0.0)["detected"]
    blank = np.full((H, W, 3), 40, np.uint8)
    # armed coast with an active >=8-sample fit -> dead-reckoned samples, 10-frame hard cap
    reasons = []
    for i in range(1, 13):
        r.set_shot_state(True, 1.0, True)
        fit.resp = {"fill": 60.0 + 3.0 * i, "vel_pp_ms": 0.18, "sigma_pp": 1.0,
                    "n": 8, "conf": 0.8, "age_ms": 5.0}
        out = r.read(blank, ts=i / 60.0)
        reasons.append(out["rejection_reason"])
        if out["rejection_reason"] == "dead_reckoned":
            assert out["fill"] >= 60.0                      # template coast, not a flat hold
            assert out["stage"] == "coast"
    assert reasons[:10] == ["dead_reckoned"] * 10
    assert set(reasons[10:]) == {"meter_memory"}            # hard cap at 10 frames
    # disarm KILLS dead reckoning instantly
    r2 = _robust_reader(monkeypatch)
    r2.set_fit_provider(fit)
    r2.set_shot_state(True, 1.0, True)
    fit.resp = {"fill": 60.0, "vel_pp_ms": 0.18, "sigma_pp": 1.0, "n": 8, "conf": 0.8, "age_ms": 5.0}
    r2.read(_frame(0.6), ts=0.0)
    r2.set_shot_state(False, 0.0, False)
    out = r2.read(blank, ts=1 / 60.0)
    assert out["rejection_reason"] == "meter_memory"


def test_dead_reckoning_requires_eight_fed_samples(monkeypatch):
    r = _robust_reader(monkeypatch)
    fit = _FakeFitProvider()
    r.set_fit_provider(fit)
    r.set_shot_state(True, 1.0, True)
    fit.resp = {"fill": 60.0, "vel_pp_ms": 0.18, "sigma_pp": 1.0, "n": 5, "conf": 0.8, "age_ms": 5.0}
    r.read(_frame(0.6), ts=0.0)
    out = r.read(np.full((H, W, 3), 40, np.uint8), ts=1 / 60.0)
    assert out["rejection_reason"] == "meter_memory"        # n=5 < 8 -> no synthetic fill


# --------------------------------------------------------------------------- #
#  relocate-window band clip (decor-lock guard)
# --------------------------------------------------------------------------- #
def test_relocate_window_clipped_to_band_right_edge():
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(1.0, col_x=1700), ts=0.0)["detected"]
    # a hard rightward slide: the motion-widened window must still stop at the band edge
    r._bvx = 60.0
    win = r._relocate_window()
    assert win[2] <= r._band[2], win                       # right edge clipped to 1815


# --------------------------------------------------------------------------- #
#  feed tiers (orchestrator statics)
# --------------------------------------------------------------------------- #
class _Res:
    def __init__(self, reason, detected=True, bbox=(10, 10, 20, 40)):
        self.detected = detected
        self.bbox = bbox
        self.rejection_reason = reason


def test_feed_tiers_sampler_vs_raw():
    from remote_play_orchestrator import RemotePlayOrchestrator as O
    for reason in ("", "green_not_found"):
        assert O._should_feed_engine(_Res(reason)) is True
        assert O._is_raw_accepted(_Res(reason)) is True
    for reason in ("fill_gated", "dead_reckoned"):
        assert O._should_feed_engine(_Res(reason)) is True   # sampler tier: FED to the engine
        assert O._is_raw_accepted(_Res(reason)) is False     # ...but NEVER raw/fresh [fix]
    for reason in ("meter_memory", "roi_not_found", "bbox_unstable", "stale_frame"):
        assert O._should_feed_engine(_Res(reason)) is False
        assert O._is_raw_accepted(_Res(reason)) is False
    assert O._should_feed_engine(_Res("", detected=False)) is False
    assert O._is_raw_accepted(_Res("", bbox=(0, 0, 0, 0))) is False


# --------------------------------------------------------------------------- #
#  hw-arm gate separation + 3-arg push plumbing
# --------------------------------------------------------------------------- #
def _build_orch(monkeypatch):
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False)
    return RemotePlayOrchestrator(cfg)


def test_arm_shot_gate_sets_hw_deadline_and_source(monkeypatch):
    orch = _build_orch(monkeypatch)
    reader = orch._meter_detector
    orch._frame_seq = 500
    orch._arm_shot_gate("square")
    assert orch._shot_gate_hw_deadline_seq == 500 + orch._shot_gate_arm_frames
    assert orch._shot_gate_deadline_seq == orch._shot_gate_hw_deadline_seq
    assert orch._shot_gate_source == "square"
    assert reader._shot_armed is True
    assert reader._shot_armed_hw is True                    # the 3rd arg landed


def test_cv_self_arm_never_touches_hw_deadline(monkeypatch):
    """The CV self-arm refreshes only the MERGED deadline; a per-frame push after the hw window
    expired must disarm the reader's hw bit while keeping the merged arm alive [fix]."""
    orch = _build_orch(monkeypatch)
    reader = orch._meter_detector
    orch._frame_seq = 100
    orch._arm_shot_gate("pose")
    hw_deadline = orch._shot_gate_hw_deadline_seq
    # window expires; the CV self-arm refreshes ONLY the merged deadline (as _processing_loop does)
    orch._frame_seq = hw_deadline + 1
    orch._shot_gate_deadline_seq = orch._frame_seq + orch._shot_gate_arm_frames
    assert orch._shot_gate_hw_deadline_seq == hw_deadline   # untouched
    armed = orch._frame_seq <= orch._shot_gate_deadline_seq
    armed_hw = orch._frame_seq <= orch._shot_gate_hw_deadline_seq
    reader.set_shot_state(armed, 0.0, armed_hw)
    assert reader._shot_armed is True
    assert reader._shot_armed_hw is False


def test_arm_shot_gate_guards_legacy_two_arg_hook(monkeypatch):
    """A reader with the legacy 2-arg set_shot_state must keep working (TypeError guard)."""
    orch = _build_orch(monkeypatch)

    class Legacy:
        def __init__(self):
            self.calls = []

        def set_shot_state(self, armed, pose_confidence=0.0):
            self.calls.append((armed, pose_confidence))

    legacy = Legacy()
    orch._meter_detector = legacy
    orch._frame_seq = 10
    orch._arm_shot_gate("stick_up")                          # must not raise
    assert legacy.calls and legacy.calls[-1][0] is True


def test_set_shot_state_full_disarm_closes_hw_window():
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True, 1.0, True)
    assert r._shot_armed_hw is True
    r.set_shot_state(False)                                  # legacy-style full disarm
    assert r._shot_armed_hw is False
