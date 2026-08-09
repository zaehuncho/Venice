import time
from types import SimpleNamespace

from controller_remap import (
    HoldState,
    RemapConfig,
    RemapEngine,
    ShotContext,
    meter_timing_seed_ms,
)
from virtual_controller import ControllerState, DS4Button


def _square_state(pressed=True):
    state = ControllerState()
    state.set_button(DS4Button.SQUARE, pressed)
    return state


def _stick_state(y):
    state = ControllerState()
    state.right_stick_y = y
    return state


def _prime_release(engine, input_state):
    engine.update_cv_data(
        fill_pct=10.0,
        confidence=0.90,
        velocity_pct_s=200.0,
        green_start_pct=96.0,
        green_end_pct=100.0,
        green_center_pct=99.5,
        eta_to_green_ms=400.0,
        consecutive_valid_frames=1,
        meter_detected=True,
    )
    engine.process(input_state)
    # Backdate the shot start so the per-archetype commit gate (the animation
    # play-out window that must elapse before any release) is satisfied. These
    # helpers exercise the release path, not the commit gate.
    engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 1000.0
    engine.update_cv_data(
        fill_pct=99.0,
        confidence=0.95,
        velocity_pct_s=200.0,
        green_start_pct=96.0,
        green_end_pct=100.0,
        green_center_pct=99.5,
        eta_to_green_ms=0.0,
        consecutive_valid_frames=1,
        meter_detected=True,
    )
    engine.process(input_state)
    assert engine.current_state == HoldState.RELEASING


class _FakeRTTEngine:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.align_calls = 0

    def get_snapshot(self):
        return self.snapshot

    def align_release(self, desired_ms):
        self.align_calls += 1
        return desired_ms - 7.0


def test_tick_alignment_requires_verified_phase_authority():
    engine = RemapEngine(RemapConfig())
    diagnostic = _FakeRTTEngine(SimpleNamespace(
        ready=True,
        target_verified=True,
        phase_locked=True,
        phase_source_verified=False,
        phase_confidence=1.0,
    ))
    engine.set_rtt_engine(diagnostic)

    assert engine._align_release_to_verified_tick(100.0) == 100.0
    assert diagnostic.align_calls == 0

    verified = _FakeRTTEngine(SimpleNamespace(
        ready=True,
        target_verified=True,
        phase_locked=True,
        phase_source_verified=True,
        phase_confidence=0.80,
    ))
    engine.set_rtt_engine(verified)

    assert engine._align_release_to_verified_tick(100.0) == 93.0
    assert verified.align_calls == 1


def test_legacy_tick_snapshot_fails_closed_without_new_authority_fields():
    engine = RemapEngine(RemapConfig())
    legacy = _FakeRTTEngine(SimpleNamespace(phase_locked=True))
    engine.set_rtt_engine(legacy)

    assert engine._align_release_to_verified_tick(100.0) == 100.0
    assert legacy.align_calls == 0


def test_diagnostic_phase_cannot_reduce_noisy_fire_confirmation():
    engine = RemapEngine(RemapConfig(confidence_gate=0.5))
    diagnostic = _FakeRTTEngine(SimpleNamespace(
        ready=True,
        target_verified=True,
        phase_locked=True,
        phase_source_verified=False,
        phase_confidence=1.0,
        jitter_ms=0.1,
    ))
    engine.set_rtt_engine(diagnostic)
    noisy = ShotContext(confidence=0.9, velocity_pct_s=5.0,
                        acceleration_pct_s2=0.0)

    assert not engine._confirm_eta_fire(noisy, True)
    assert noisy.eta_fire_streak == 1

    verified = _FakeRTTEngine(SimpleNamespace(
        ready=True,
        target_verified=True,
        phase_locked=True,
        phase_source_verified=True,
        phase_confidence=0.8,
        jitter_ms=0.1,
    ))
    engine.set_rtt_engine(verified)
    verified_noisy = ShotContext(confidence=0.9, velocity_pct_s=5.0,
                                 acceleration_pct_s2=0.0)

    assert engine._confirm_eta_fire(verified_noisy, True)


def test_square_press_must_be_held_before_square_hold_engages():
    """Pressing Square for a single frame must NOT fire a shot.

    Square is suppressed while the engine verifies intent. Only after the
    press is sustained for ``min_press_hold_ms`` does the engine hold virtual
    Square until the CV/timing release.
    """
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        shot_trigger_mode="tempo",
        tempo_wait_ms=0.0,
        tempo_flick_hold_ms=80.0,
        gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0,
    )
    engine = RemapEngine(cfg)

    out = engine.process(_square_state(True))
    assert not out.is_button_pressed(DS4Button.SQUARE), (
        "Single-frame press must be suppressed so a tap cannot shoot"
    )
    assert out.right_stick_y == 0
    assert engine.current_state == HoldState.WARMUP

    # Sustain the press long enough to clear the warmup window, then verify
    # the engine commits and starts hijacking the output.
    time.sleep(0.012)
    out = engine.process(_square_state(True))
    assert out.is_button_pressed(DS4Button.SQUARE)
    assert out.right_stick_y == 0

    _prime_release(engine, _square_state(True))
    out = engine.process(_square_state(True))
    assert not out.is_button_pressed(DS4Button.SQUARE)
    assert out.right_stick_y == 0


def test_square_hold_stays_latched_if_physical_button_is_released_after_arm():
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        shot_trigger_mode="tempo",
        min_hold_ms=40.0,
        gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0,
    )
    engine = RemapEngine(cfg)
    engine.process(_square_state(True))
    time.sleep(0.012)  # clear warmup window
    engine.update_cv_data(
        fill_pct=20.0,
        confidence=0.90,
        velocity_pct_s=180.0,
        green_start_pct=96.0,
        green_end_pct=100.0,
        green_center_pct=99.5,
        eta_to_green_ms=300.0,
        consecutive_valid_frames=1,
        meter_detected=True,
    )
    engine.process(_square_state(True))  # WARMUP -> ARMED
    engine.process(_square_state(True))  # ARMED -> HOLDING
    engine._shot.hold_start_ms = time.perf_counter() * 1000.0 - 60.0

    out = engine.process(_square_state(False))
    assert engine.current_state == HoldState.HOLDING
    assert out.is_button_pressed(DS4Button.SQUARE)
    assert out.right_stick_y == 0


def test_quick_square_tap_does_not_trigger_shot():
    """Regression: pressing Square for one frame and releasing must not fire."""
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        gpc_flick_chain_ms=0.0,
        min_press_hold_ms=80.0,
    )
    engine = RemapEngine(cfg)
    out = engine.process(_square_state(True))
    assert engine.current_state == HoldState.WARMUP
    # Release immediately — warmup should cancel and no shot should fire.
    out = engine.process(_square_state(False))
    assert engine.current_state == HoldState.IDLE
    assert out.right_stick_y == 0
    assert not out.is_button_pressed(DS4Button.SQUARE)


def test_running_plus_square_does_not_trigger_stepback():
    """Regression: moving left stick + Square must not be converted into any
    right-stick tempo/stepback output.
    """
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        gpc_flick_chain_ms=0.0,
        shot_trigger_mode="tempo",
        min_press_hold_ms=40.0,
        movement_abort_threshold=60.0,
    )
    engine = RemapEngine(cfg)
    moving = _square_state(True)
    moving.left_stick_x = 110  # actively dribbling/running
    moving.left_stick_y = -90
    engine.process(moving)
    # While moving, a sustained Square press still commits as a normal Square
    # hold so fades can be timed, but it must not inject right-stick output.
    time.sleep(0.06)
    out = engine.process(moving)
    assert engine.current_state in (HoldState.ARMED, HoldState.HOLDING)
    assert out.is_button_pressed(DS4Button.SQUARE)
    assert out.right_stick_y == 0
    assert out.left_stick_x == 110
    assert out.left_stick_y == -90


def test_quick_stick_flick_down_does_not_trigger_shot():
    """Regression: a fast flick of the right stick down must not fire a shot."""
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        shot_trigger_mode="tempo",
        gpc_flick_chain_ms=0.0,
        min_stick_intent_ms=80.0,
    )
    engine = RemapEngine(cfg)
    down = _stick_state(120)
    out = engine.process(down)
    assert engine.current_state == HoldState.WARMUP
    assert out.right_stick_y == 120
    # Release the flick immediately — no shot should fire.
    out = engine.process(_stick_state(0))
    assert engine.current_state == HoldState.IDLE
    assert out.right_stick_y == 0


def test_zero_stability_or_confidence_does_not_promote_to_holding():
    """Regression: a CV frame reporting zero stable frames / zero confidence
    must NOT satisfy the ARMED->HOLDING stability gate.

    This guards against the orchestrator bug where ``value or default`` spoofed
    a legitimate 0 (no stability / no confidence) into the configured gate
    threshold, prematurely promoting the shot and firing early.
    """
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=2,
        confidence_gate=0.50,
        tempo_fallback_timeout_ms=5000.0,  # keep timeout fallback out of the way
        gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0,
    )
    engine = RemapEngine(cfg)
    engine.process(_square_state(True))  # IDLE -> WARMUP
    time.sleep(0.012)
    engine.process(_square_state(True))  # WARMUP -> ARMED
    assert engine.current_state == HoldState.ARMED

    # Detector legitimately reports an unstable, no-confidence frame.
    engine.update_cv_data(
        fill_pct=5.0,
        confidence=0.0,
        velocity_pct_s=0.0,
        consecutive_valid_frames=0,
        meter_detected=True,
    )
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.ARMED, (
        "Zero stability/confidence must not pass the stability gate"
    )

    # Once the detector reports genuine stability + confidence, promote.
    engine.update_cv_data(
        fill_pct=20.0,
        confidence=0.90,
        velocity_pct_s=180.0,
        consecutive_valid_frames=3,
        meter_detected=True,
    )
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.HOLDING


def test_meter_timing_seed_is_relative_to_mean():
    # Baselines: Arrow/Dial=40 (compact), Arrow2/Pill/Straight/Sword=50.
    # Mean = 46.67, so compact meters seed negative, tall meters seed positive.
    assert meter_timing_seed_ms("Arrow2") > 0.0
    assert meter_timing_seed_ms("Arrow") < 0.0
    assert meter_timing_seed_ms("dial") < 0.0          # case-insensitive
    assert meter_timing_seed_ms("Unknown") == 0.0
    assert meter_timing_seed_ms("") == 0.0
    # Symmetric grouping: the two compact + four tall meters average out near 0.
    seeds = [meter_timing_seed_ms(m) for m in ("Arrow", "Arrow2", "Dial", "Pill", "Straight", "Sword")]
    assert abs(sum(seeds)) < 1e-6


def test_engine_seeds_feedback_offset_from_meter_style():
    seeded = RemapEngine(RemapConfig(meter_style="Arrow2"))
    assert abs(seeded._feedback_offset_ms - meter_timing_seed_ms("Arrow2")) < 1e-6
    # Disabling the seed leaves the offset at zero.
    off = RemapEngine(RemapConfig(meter_style="Arrow2", meter_timing_seed_enabled=False))
    assert off._feedback_offset_ms == 0.0
    # An unknown meter style must not bias the loop.
    unknown = RemapEngine(RemapConfig(meter_style="Mystery"))
    assert unknown._feedback_offset_ms == 0.0


def test_unstable_eta_fire_requires_two_frame_confirmation():
    """Accuracy: when the CV signal is unstable (low velocity / jittery), a
    single frame that momentarily satisfies the fire condition must NOT trigger
    the release. It takes a second confirming frame. This rejects detection
    noise spikes that would otherwise fire early and break the green.
    """
    cfg = RemapConfig(
        input_mode="both",
        stable_frames_required=1,
        confidence_gate=0.10,
        gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0,
        fixed_hold_ms=2000.0,            # keep hybrid late-guard out of the way
        tempo_fallback_timeout_ms=5000.0,
    )
    engine = RemapEngine(cfg)
    engine.process(_square_state(True))  # IDLE -> WARMUP
    time.sleep(0.012)
    # Stable frame commits ARMED -> HOLDING.
    engine.update_cv_data(
        fill_pct=50.0, confidence=0.90, velocity_pct_s=200.0,
        green_center_pct=99.0, eta_to_green_ms=300.0,
        consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(_square_state(True))  # WARMUP -> ARMED
    engine.process(_square_state(True))  # ARMED -> HOLDING
    assert engine.current_state == HoldState.HOLDING
    # Past the commit gate so the ETA-confirmation behaviour is what's tested.
    engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 1000.0

    # Unstable frame (velocity below the 20%/s stability floor) with eta==0 so
    # the timing condition is met. First occurrence must be rejected.
    unstable = dict(
        fill_pct=98.0, confidence=0.90, velocity_pct_s=10.0,
        green_center_pct=99.0, eta_to_green_ms=0.0,
        consecutive_valid_frames=1, meter_detected=True,
    )
    engine.update_cv_data(**unstable)
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.HOLDING, "single noisy frame must not fire"

    # Second confirming frame -> release commits.
    engine.update_cv_data(**unstable)
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.RELEASING


def test_goto_stick_is_passthrough_when_square_only_mode_is_forced():
    cfg = RemapConfig(
        input_mode="square_only",
        shot_trigger_mode="tempo",
        goto_enabled=True,
        goto_arm_frames=1,
        stable_frames_required=1,
        confidence_gate=0.10,
        goto_flick_hold_ms=80.0,
        gpc_flick_chain_ms=0.0,
    )
    engine = RemapEngine(cfg)
    up = _stick_state(-127)

    out = engine.process(up)
    assert out.right_stick_y == -127
    assert engine.current_state == HoldState.IDLE
    engine.update_cv_data(
        fill_pct=99.0,
        confidence=0.95,
        velocity_pct_s=200.0,
        green_start_pct=96.0,
        green_end_pct=100.0,
        green_center_pct=99.5,
        eta_to_green_ms=0.0,
        consecutive_valid_frames=1,
        meter_detected=True,
    )
    out = engine.process(up)
    assert engine.current_state == HoldState.IDLE
    assert out.right_stick_y == -127
    assert not out.is_button_pressed(DS4Button.SQUARE)


def test_button_mode_tap_is_fully_suppressed_from_first_frame():
    """Default 'button' mode: a quick square TAP must never reach the console.

    The very first press frame must NOT forward square (the 'tapping shoots'
    bug), and releasing before the hold threshold cancels with no shot.
    """
    cfg = RemapConfig(stable_frames_required=1, confidence_gate=0.10,
                      gpc_flick_chain_ms=0.0, min_press_hold_ms=80.0)
    assert cfg.shot_trigger_mode == "button"
    engine = RemapEngine(cfg)

    out = engine.process(_square_state(True))
    assert engine.current_state == HoldState.WARMUP
    assert not out.is_button_pressed(DS4Button.SQUARE), "tap must not forward square on frame 1"
    assert out.right_stick_y == 0  # never a tempo/stick flick

    out = engine.process(_square_state(False))  # released before threshold
    assert engine.current_state == HoldState.IDLE
    assert not out.is_button_pressed(DS4Button.SQUARE)


def test_button_mode_hold_outputs_real_square_then_autogreen_releases():
    """Default 'button' mode: HOLDING square outputs the REAL square (no stick
    flick) and the autogreen engine releases it at the green window."""
    cfg = RemapConfig(stable_frames_required=1, confidence_gate=0.10,
                      gpc_flick_chain_ms=0.0, min_press_hold_ms=5.0)
    engine = RemapEngine(cfg)

    engine.process(_square_state(True))      # IDLE -> WARMUP
    time.sleep(0.012)
    engine.update_cv_data(
        fill_pct=20.0, confidence=0.90, velocity_pct_s=180.0,
        green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
        eta_to_green_ms=300.0, consecutive_valid_frames=1, meter_detected=True,
    )
    out = engine.process(_square_state(True))  # WARMUP -> ARMED (square held)
    assert engine.current_state in (HoldState.ARMED, HoldState.HOLDING)
    assert out.is_button_pressed(DS4Button.SQUARE), "hold must output the real square"
    assert out.right_stick_y == 0, "button mode must never flick the stick"

    _prime_release(engine, _square_state(True))
    out = engine.process(_square_state(True))
    assert not out.is_button_pressed(DS4Button.SQUARE), "autogreen releases square"


def test_passthrough_mode_never_arms():
    cfg = RemapConfig(shot_trigger_mode="passthrough", min_press_hold_ms=1.0)
    engine = RemapEngine(cfg)
    out = engine.process(_square_state(True))
    assert engine.current_state == HoldState.IDLE
    assert out.is_button_pressed(DS4Button.SQUARE), "passthrough forwards square raw"


def test_active_shot_type_offset_feeds_release_latency():
    cfg = RemapConfig(active_shot_type="Left Fade",
                      shot_type_offsets={"Left Fade": 12.0, "Standstill": 0.0})
    engine = RemapEngine(cfg)
    assert engine._active_shot_offset() == 12.0
    # Unknown / unset type contributes nothing.
    cfg2 = RemapConfig(active_shot_type="Nonexistent")
    assert RemapEngine(cfg2)._active_shot_offset() == 0.0


def test_classify_shot_type_from_concurrent_input():
    """The shot archetype is auto-detected from the live controller input.

    L2 is the post-move discriminator; the right-stick X component separates a
    diagonal flick (Post Hook) from a straight up hold (Post Go-To)."""
    engine = RemapEngine(RemapConfig())

    def st(l2=0, rsx=0, rsy=0, lsx=0, lsy=0):
        s = _square_state(True)
        s.l2_trigger = l2
        s.right_stick_x = rsx
        s.right_stick_y = rsy
        s.left_stick_x = lsx
        s.left_stick_y = lsy
        return s

    # Standard set (no L2).
    assert engine._classify_shot_type(st()) == "Standstill"
    assert engine._classify_shot_type(st(lsx=-110)) == "Left Fade"
    assert engine._classify_shot_type(st(lsx=110)) == "Right Fade"
    assert engine._classify_shot_type(st(rsy=-110)) == "Go-To"
    # Post moves (L2 held).
    assert engine._classify_shot_type(st(l2=200)) == "No Dip"
    assert engine._classify_shot_type(st(l2=200, lsy=110)) == "Post Fade"
    assert engine._classify_shot_type(st(l2=200, rsy=-110, rsx=80)) == "Post Hook"
    assert engine._classify_shot_type(st(l2=200, rsy=-110, rsx=0)) == "Post Go-To"


def test_r2_held_plus_left_stick_classifies_as_fade():
    """R2 held for the whole shot + left-stick direction + square = fadeaway.
    The held trigger is an explicit fade modifier, so a smaller stick deflection
    than the standard lateral threshold still resolves to a fade."""
    engine = RemapEngine(RemapConfig(movement_abort_threshold=60.0))

    def st(r2=0, lsx=0, lsy=0):
        s = _square_state(True)
        s.r2_trigger = r2
        s.left_stick_x = lsx
        s.left_stick_y = lsy
        return s

    # R2 held + left lean => Left Fade; right lean => Right Fade.
    assert engine._classify_shot_type(st(r2=200, lsx=-90)) == "Left Fade"
    assert engine._classify_shot_type(st(r2=200, lsx=90)) == "Right Fade"
    # R2 lowers the deflection needed (0.6 * 60 = 36) vs the standard gate (60).
    assert engine._classify_shot_type(st(r2=200, lsx=-40)) == "Left Fade"
    assert engine._classify_shot_type(st(r2=0, lsx=-40)) == "Standstill"
    # L2 post-move behaviour is unchanged when R2 is not held.
    assert engine._classify_shot_type(st()) == "Standstill"


def test_commit_gate_blocks_release_until_animation_plays_out():
    """Go-To must keep holding (let the gather animation surface the meter)
    until its commit window elapses, even with the green already in view."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0,
        shot_type_commit_ms={"Go-To": 600.0, "Standstill": 140.0},
    )
    engine = RemapEngine(cfg)
    goto = _square_state(True)
    goto.right_stick_y = -110  # Go-To archetype

    engine.process(goto)  # IDLE -> WARMUP
    time.sleep(0.012)
    engine.update_cv_data(
        fill_pct=99.0, confidence=0.95, velocity_pct_s=200.0,
        green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
        eta_to_green_ms=0.0, consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(goto)  # WARMUP -> ARMED (latches Go-To)
    engine.process(goto)  # ARMED -> HOLDING
    assert engine._shot.shot_type == "Go-To"
    # Green is right there, but the commit window has not elapsed: must hold.
    engine.update_cv_data(
        fill_pct=99.0, confidence=0.95, velocity_pct_s=200.0,
        green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
        eta_to_green_ms=0.0, consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(goto)
    assert engine.current_state == HoldState.HOLDING, "must not release before commit"

    # Once the commit window has passed, the same signal releases.
    engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 1000.0
    engine.update_cv_data(
        fill_pct=99.0, confidence=0.95, velocity_pct_s=200.0,
        green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
        eta_to_green_ms=0.0, consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(goto)
    assert engine.current_state == HoldState.RELEASING


def test_eta_fire_streak_decays_instead_of_hard_reset():
    """A momentary not-reached frame should DECAY the confirmation streak, not
    wipe it, so a single jitter spike doesn't push the release a frame late."""
    engine = RemapEngine(RemapConfig())
    shot = engine._shot
    shot.confidence = 0.0  # force the 'unstable' path -> requires streak >= 2
    shot.velocity_pct_s = 5.0
    # Build the streak up.
    assert engine._confirm_eta_fire(shot, True) is False  # streak 1
    assert engine._confirm_eta_fire(shot, True) is True   # streak 2 -> fire
    # A single not-reached frame decays (2 -> 1) rather than resetting to 0.
    assert engine._confirm_eta_fire(shot, False) is False
    assert shot.eta_fire_streak == 1
    # So the very next reached frame re-confirms immediately (1 -> 2).
    assert engine._confirm_eta_fire(shot, True) is True


def test_frame_age_adds_to_release_latency_compensation():
    """A stale CV reading (large frame_age_ms) must push the fire point earlier
    by exactly that age (input-side latency), so the bot fires sooner to land
    on the green tip despite the staleness."""
    engine = RemapEngine(RemapConfig(gpc_flick_chain_ms=0.0))
    engine.update_cv_data(
        fill_pct=50.0, confidence=0.9, velocity_pct_s=200.0,
        eta_to_green_ms=300.0, consecutive_valid_frames=1, meter_detected=True,
        frame_age_ms=25.0,
    )
    assert engine._shot.frame_age_ms == 25.0
    # frame_age is clamped to a sane bound.
    engine.update_cv_data(
        fill_pct=50.0, confidence=0.9, velocity_pct_s=200.0,
        eta_to_green_ms=300.0, consecutive_valid_frames=1, meter_detected=True,
        frame_age_ms=10_000.0,
    )
    assert engine._shot.frame_age_ms == 80.0


def test_quadratic_crossing_beats_linear_under_acceleration():
    """With an accelerating fill, the quadratic predictor must place the target
    crossing EARLIER than a constant-velocity line drawn from the same samples
    (the fill is speeding up, so it reaches the target sooner)."""
    from controller_remap import TemporalSuperSampler
    s = TemporalSuperSampler(window=8)
    # Accelerating fill: f = 0.001 * t^2 (t in ms). Sample 0..120ms.
    t0 = 0.0
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        s.add_sample(0.001 * t * t, t0 + t)
    target = 0.001 * 200.0 * 200.0  # = 40.0, reached at t=200ms (true)
    quad = s.predict_crossing_ms(target)
    # Linear extrapolation from the last two samples would predict much later.
    last_v = (0.001 * 120 * 120 - 0.001 * 90 * 90) / 30.0
    lin_guess = 120.0 + (target - 0.001 * 120 * 120) / last_v
    assert quad > 0
    assert abs(quad - 200.0) < 25.0, f"quad={quad} should be near the true 200ms"
    assert quad < lin_guess, "quadratic must beat the constant-velocity guess"


def _drive_to_holding(engine, input_state, *, meter=True):
    """Take a fresh engine through IDLE->WARMUP->ARMED->HOLDING. When meter is
    False, HOLDING is reached via the arm timeout (no CV), leaving meter_seen
    False so the meter-gated fallback can be exercised."""
    engine.process(input_state)  # IDLE -> WARMUP
    time.sleep(0.012)
    if meter:
        engine.update_cv_data(
            fill_pct=20.0, confidence=0.90, velocity_pct_s=180.0,
            green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
            eta_to_green_ms=300.0, consecutive_valid_frames=1, meter_detected=True,
        )
        engine.process(input_state)  # WARMUP -> ARMED
        engine.process(input_state)  # ARMED -> HOLDING
    else:
        # No CV at all: force ARMED via warmup, then HOLDING via arm timeout.
        engine.process(input_state)  # WARMUP -> ARMED
        engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 5000.0
        engine.process(input_state)  # ARMED -> HOLDING (timeout fallback)
    assert engine.current_state == HoldState.HOLDING


def test_no_green_window_aims_for_top_of_meter():
    """No confirmed/visible green window: the bot must still release, targeting
    the TOP of the meter (config no_green_target_pct), where the green always
    sits regardless of contest. Filling to that point fires the release."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        output_latency_ms=0.0, min_press_hold_ms=5.0, no_green_target_pct=97.0,
        fixed_hold_ms=5000.0, tempo_fallback_timeout_ms=5000.0,
    )
    engine = RemapEngine(cfg)
    _drive_to_holding(engine, _square_state(True))
    engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 1000.0
    # Meter is at the top but NO green window is reported (green_*_pct = -1).
    engine.update_cv_data(
        fill_pct=98.0, confidence=0.95, velocity_pct_s=200.0,
        green_start_pct=-1.0, green_end_pct=-1.0, green_center_pct=-1.0,
        eta_to_green_ms=-1.0, consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.RELEASING, (
        "must release aiming for the top of the meter when no green window exists"
    )


def test_fallback_release_is_gated_on_meter_seen():
    """The fallback-timeout release must NOT fire until a real meter has been
    seen this shot (so a Go-To/fade animation that hasn't surfaced the meter
    keeps holding), then fires once the meter appears and the timeout passes."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        output_latency_ms=0.0, min_press_hold_ms=5.0,
        tempo_fallback_timeout_ms=50.0, max_hold_ms=100000.0,
        shot_type_commit_ms={"Standstill": 0.0},
    )
    engine = RemapEngine(cfg)
    _drive_to_holding(engine, _square_state(True), meter=False)
    assert engine._shot.meter_seen is False
    # Past commit + fallback window, but no meter ever seen -> must keep holding.
    engine._shot.arm_timestamp_ms = time.perf_counter() * 1000.0 - 5000.0
    engine._shot.hold_start_ms = time.perf_counter() * 1000.0 - 5000.0
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.HOLDING, "no blind release before meter"

    # Meter now surfaces; with the timeout already elapsed the fallback fires.
    engine.update_cv_data(
        fill_pct=5.0, confidence=0.20, velocity_pct_s=5.0,
        consecutive_valid_frames=1, meter_detected=True,
    )
    engine._shot.hold_start_ms = time.perf_counter() * 1000.0 - 5000.0
    engine.process(_square_state(True))
    assert engine.current_state == HoldState.RELEASING


def test_pump_fake_not_aborted_once_meter_has_surfaced():
    """A brief input dropout under min_hold must NOT abort as a pump fake once
    the meter is up and filling — that is a committed shot, not a fake."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0, min_hold_ms=200.0, input_release_grace_ms=0.0,
        fixed_hold_ms=5000.0, tempo_fallback_timeout_ms=5000.0,
        shot_type_commit_ms={"Standstill": 5000.0},  # keep it in HOLDING
    )
    engine = RemapEngine(cfg)
    _drive_to_holding(engine, _square_state(True))
    assert engine._shot.meter_seen is True
    engine._shot.hold_start_ms = time.perf_counter() * 1000.0 - 10.0  # < min_hold
    # Physical square released early; meter already seen -> NOT a pump fake.
    # Two frames: first arms the release-grace timer, second evaluates the guard.
    engine.process(_square_state(False))
    engine.process(_square_state(False))
    assert engine.current_state == HoldState.HOLDING, "committed shot must not abort"
    assert engine.get_stats()["shots_aborted"] == 0


def test_pump_fake_still_aborts_before_meter_surfaces():
    """Symmetric guard: an early release BEFORE any meter has surfaced is still
    treated as a pump fake and aborts the shot."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0, min_hold_ms=200.0, input_release_grace_ms=0.0,
        tempo_fallback_timeout_ms=50.0, max_hold_ms=100000.0,
        shot_type_commit_ms={"Standstill": 0.0},
    )
    engine = RemapEngine(cfg)
    _drive_to_holding(engine, _square_state(True), meter=False)
    assert engine._shot.meter_seen is False
    engine._shot.hold_start_ms = time.perf_counter() * 1000.0 - 10.0  # < min_hold
    # Two frames: first arms the release-grace timer, second triggers the abort.
    engine.process(_square_state(False))
    engine.process(_square_state(False))
    assert engine.current_state == HoldState.IDLE
    assert engine.get_stats()["shots_aborted"] == 1


def test_shot_type_relatched_live_during_hold():
    """The archetype must track the live input during HOLDING, not just at
    commit, so a mid-animation change updates the per-type timing."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0, fixed_hold_ms=5000.0,
        tempo_fallback_timeout_ms=5000.0,
        shot_type_commit_ms={"Standstill": 5000.0, "Go-To": 5000.0},
    )
    engine = RemapEngine(cfg)
    _drive_to_holding(engine, _square_state(True))  # committed as Standstill
    assert engine._shot.shot_type == "Standstill"
    # Player flicks the right stick up mid-hold -> archetype re-latches to Go-To.
    goto = _square_state(True)
    goto.right_stick_y = -110
    engine.process(goto)
    assert engine._shot.shot_type == "Go-To"


def _run_cooldown_shot(engine, peak, target=95.0, vel=100.0):
    """Drive one completed shot through the feedback step in _process_cooldown."""
    engine._shot = ShotContext(
        hold_state=HoldState.COOLDOWN, released=True, aborted=False,
        peak_fill_pct=peak, green_window_center_pct=target, velocity_pct_s=vel,
        cooldown_end_ms=0.0,
    )
    engine._process_cooldown(ControllerState(), time.perf_counter() * 1000.0)


def test_feedback_loop_converges_in_correct_direction():
    """The self-tuning offset must move so future shots correct the error:
    overshoot (released late) -> MORE latency comp (fire earlier) -> offset up;
    undershoot (released early) -> LESS comp (fire later) -> offset down.
    A wrong sign here makes the loop diverge and the bot fire consistently off."""
    over = RemapEngine(RemapConfig(meter_style=""))
    assert over._feedback_offset_ms == 0.0
    _run_cooldown_shot(over, peak=100.0, target=90.0)  # overshoot
    _run_cooldown_shot(over, peak=100.0, target=90.0)  # n>=2 -> adjusts
    assert over._feedback_offset_ms > 0.0, "overshoot must increase latency comp"

    under = RemapEngine(RemapConfig(meter_style=""))
    _run_cooldown_shot(under, peak=70.0, target=95.0)  # undershoot
    _run_cooldown_shot(under, peak=70.0, target=95.0)
    assert under._feedback_offset_ms < 0.0, "undershoot must decrease latency comp"


def test_predictor_rejects_noise_driven_quadratic():
    """Near-linear fill with small noise must NOT be fit by a wild quadratic that
    blows the crossing far from the true linear crossing. The residual-ratio gate
    should keep the prediction sane (close to the constant-velocity estimate)."""
    from controller_remap import TemporalSuperSampler
    s = TemporalSuperSampler(window=8)
    # Linear fill 0.2 %/ms with tiny alternating noise; sampled every 16ms.
    noise = [0.0, 0.15, -0.15, 0.1, -0.1, 0.12, -0.12]
    for i, dn in enumerate(noise):
        t = i * 16.0
        s.add_sample(0.2 * t + dn, t)
    # True linear crossing of 40% from fill at last sample.
    cross = s.predict_crossing_ms(40.0)
    last_t = (len(noise) - 1) * 16.0
    last_fill = 0.2 * last_t + noise[-1]
    lin_eta = (40.0 - last_fill) / 0.2
    assert cross > 0
    # Prediction must be within a tight band of the linear crossing, i.e. the
    # quadratic was not allowed to run away on the noise.
    assert abs((cross - last_t) - lin_eta) < 25.0, f"crossing {cross} drifted from linear {last_t + lin_eta}"


def test_fused_eta_blends_detector_and_sampler():
    """The primary release ETA fuses the detector's kinematic ETA with the
    super-sampler crossing to the same target (variance reduction). With both
    present it returns their average; with one missing it falls back cleanly."""
    engine = RemapEngine(RemapConfig())
    now = time.perf_counter() * 1000.0
    # Linear ramp 0.2 %/ms ending at fill=16 at `now`.
    for i in range(6):
        engine._sampler.add_sample(0.2 * i * 16.0, now - (5 - i) * 16.0)
    sampler_only = engine._fused_eta_ms(None, 80.0, now)
    assert sampler_only > 0, "sampler should predict a crossing for a clean ramp"
    fused = engine._fused_eta_ms(100.0, 80.0, now)
    # Confidence-weighted blend: sampler weight = min(1, n/maxlen)*0.6.
    n = len(engine._sampler._samples)
    maxlen = engine._sampler._samples.maxlen
    w_samp = min(1.0, n / float(maxlen)) * 0.6
    expected = (1.0 - w_samp) * 100.0 + w_samp * sampler_only
    assert abs(fused - expected) < 1.0, "fused = confidence-weighted blend"
    # Blend lies strictly between the two single estimates.
    assert min(100.0, sampler_only) <= fused <= max(100.0, sampler_only)
    # No center target -> detector ETA passes through unchanged.
    assert engine._fused_eta_ms(100.0, -1.0, now) == 100.0
    # No detector ETA -> sampler estimate is used.
    assert engine._fused_eta_ms(None, 80.0, now) == sampler_only


def test_predictor_returns_current_time_when_already_past_target():
    from controller_remap import TemporalSuperSampler
    s = TemporalSuperSampler(window=8)
    for i in range(4):
        s.add_sample(90.0 + i, i * 16.0)  # already above a 50% target
    assert s.predict_crossing_ms(50.0) == 3 * 16.0


def test_detected_shot_type_latched_at_commit_and_drives_offset():
    """Committing a hold with lateral left-stick movement latches 'Left Fade'
    onto the shot and applies that type's offset (not active_shot_type)."""
    cfg = RemapConfig(
        stable_frames_required=1, confidence_gate=0.10, gpc_flick_chain_ms=0.0,
        min_press_hold_ms=5.0, active_shot_type="Standstill",
        shot_type_offsets={"Standstill": 0.0, "Left Fade": 15.0},
    )
    engine = RemapEngine(cfg)
    fade = _square_state(True)
    fade.left_stick_x = -110  # running left + square => Left Fade

    engine.process(fade)  # IDLE -> WARMUP
    time.sleep(0.012)
    engine.update_cv_data(
        fill_pct=20.0, confidence=0.90, velocity_pct_s=180.0,
        green_start_pct=96.0, green_end_pct=100.0, green_center_pct=99.5,
        eta_to_green_ms=300.0, consecutive_valid_frames=1, meter_detected=True,
    )
    engine.process(fade)  # WARMUP -> ARMED, latches detected type
    assert engine._shot.shot_type == "Left Fade"
    assert engine._active_shot_offset() == 15.0
