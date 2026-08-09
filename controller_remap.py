import logging
import math
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict
from virtual_controller import ControllerState, DS4Button
logger = logging.getLogger('ControllerRemap')

# Set ORION_REMAP_VERBOSE=1 to surface shot state transitions at INFO level
# (arm / hold / release) on stderr. Used to confirm the bot is advancing the
# HoldState machine and actually deciding+submitting releases at runtime.
_REMAP_VERBOSE = os.environ.get('ORION_REMAP_VERBOSE', '').strip().lower() not in ('', '0', 'false', 'no', 'off')


def _tlog(msg, *args):
    if _REMAP_VERBOSE:
        logger.info(msg, *args)
    else:
        logger.debug(msg, *args)

# Per-meter release-timing baselines (empirical starting points, ms).
# These baselines reflect LOCAL (no-network) tuning, so the ABSOLUTE values are NOT
# additive to our Remote Play pipeline (which already compensates network +
# mechanical lead via RTT + early_late_offset + gpc_flick_chain). Only the
# RELATIVE per-meter difference transfers: tall meters (Arrow2/Pill/Straight/
# Sword = 50) were tuned ~10ms hotter than the compact ones (Arrow/Dial = 40).
# We use (value - mean) as the INITIAL feedback-offset seed so the converging
# median feedback loop starts pre-biased for the active meter style instead of 0.
METER_TIMING_BASELINES_MS = {
    'Arrow': 40.0,
    'Arrow2': 50.0,
    'Dial': 40.0,
    'Pill': 50.0,
    'Straight': 50.0,
    'Sword': 50.0,
}


def meter_timing_seed_ms(meter_style, clamp_ms=25.0):
    """Relative-to-mean per-meter feedback seed (ms). 0.0 for unknown styles."""
    if not meter_style:
        return 0.0
    key = str(meter_style).strip()
    val = METER_TIMING_BASELINES_MS.get(key)
    if val is None:
        for k, v in METER_TIMING_BASELINES_MS.items():
            if k.lower() == key.lower():
                val = v
                break
    if val is None:
        return 0.0
    mean = sum(METER_TIMING_BASELINES_MS.values()) / len(METER_TIMING_BASELINES_MS)
    return max(-clamp_ms, min(clamp_ms, val - mean))


@dataclass
class RemapConfig:
    enabled: bool = True
    # When False, the engine routes input (stick remap, button passthrough) but
    # does NOT trigger releases or run self-learning. The C++ AutomationEngine
    # is the sole automation authority. Default True for backward compat.
    release_enabled: bool = True
    input_mode: str = 'square_only'
    tempo_wait_ms: float = 0.0
    tempo_flick_hold_ms: float = 50.0
    tempo_min_stick_hold_ms: float = 0.0
    tempo_fallback_timeout_ms: float = 650.0
    min_hold_ms: float = 90.0
    max_hold_ms: float = 1200.0
    fixed_hold_ms: float = 650.0
    hold_release_strategy: str = 'hybrid'
    release_pulse_ms: float = 50.0
    early_late_offset_ms: float = 0.0
    # Aim for the CENTER of the green window by default. Over Remote Play the
    # dominant error source is network jitter; centering the release point
    # gives symmetric tolerance (half the window width on each side) instead
    # of 'end' targeting which only tolerates being early. This is the single
    # biggest robustness win against jitter.
    green_window_target: str = 'tip'
    green_window_priority: bool = True
    # When no green window is detected/confirmed, aim for the TOP of the meter:
    # the green release window in 2K always sits at the very top of the bar
    # regardless of contest or court position, so the top is the correct target
    # for contested/suppressed-window shots. Used by _check_predictive_release.
    no_green_target_pct: float = 97.0
    # OPTIONAL extra output-side latency, on TOP of gpc_flick_chain_ms (which the
    # orchestrator already sets from latency_compensation_ms and which models the
    # ViGEm submit -> console -> render hop). Defaults to 0.0 so it is a no-op
    # and never double-counts the existing comp; expose it only to fine-tune the
    # button-up debounce on a specific rig without disturbing the main budget.
    output_latency_ms: float = 0.0
    # Active shot-meter style (e.g. 'Arrow2'). Used to seed the feedback offset
    # from the per-meter timing baselines so convergence starts ahead.
    meter_style: str = ''
    meter_timing_seed_enabled: bool = True
    # How a square press is turned into a shot:
    #   'button'      -> HOLD square = autogreen (detect meter + auto-release
    #                    real square at the green window). Quick taps are
    #                    suppressed and never reach the console. NO tempo, NO
    #                    stick-flick remap. This is the default/simple mode.
    #   'tempo'       -> legacy right-stick tempo + stick/goto remap (disabled
    #                    by default; kept only for future re-enable).
    #   'passthrough' -> engine never arms; inputs flow through untouched.
    shot_trigger_mode: str = 'button'
    # Fixed per-shot-type release offsets (ms, signed). The DETECTED shot type's
    # value is added to the release lead so each shot archetype can be nudged
    # independently. The shot type is auto-classified every frame from the live
    # controller input (see RemapEngine._classify_shot_type) and latched at the
    # moment the square-hold commits. active_shot_type is only a fallback used
    # if classification yields nothing.
    active_shot_type: str = 'Standstill'
    shot_type_offsets: Dict[str, float] = field(default_factory=lambda: {
        'Standstill': 0.0,
        'Left Fade': 0.0,
        'Right Fade': 0.0,
        'Go-To': 0.0,
        'No Dip': 0.0,
        'Post Fade': 0.0,
        'Post Hook': 0.0,
        'Post Go-To': 0.0,
    })
    # Minimum time (ms, measured from the square-hold commit / shot start) the
    # bot must keep holding before ANY release is allowed, per archetype. This
    # forces the shot animation to play out so the meter actually surfaces before
    # the engine tries to time it — critical for Go-To and fades, whose gather
    # animation appears well after the button goes down. Standstill needs almost
    # none. Mirrors the native AutomationEngine commit-min constants.
    shot_type_commit_ms: Dict[str, float] = field(default_factory=lambda: {
        'Standstill': 140.0,
        'Left Fade': 300.0,
        'Right Fade': 300.0,
        'Go-To': 650.0,
        'No Dip': 140.0,
        'Post Fade': 320.0,
        'Post Hook': 400.0,
        'Post Go-To': 650.0,
    })
    stick_down_threshold: float = 50.0
    stick_up_threshold: float = 50.0
    goto_enabled: bool = False
    goto_flick_hold_ms: float = 50.0
    goto_arm_frames: int = 2
    confidence_gate: float = 0.32
    stable_frames_required: int = 3
    gpc_flick_chain_ms: float = 20.0
    input_release_grace_ms: float = 95.0
    stick_hysteresis_pct: float = 8.0
    # Minimum sustained press duration (ms) before a square press
    # is treated as a tempo-hold intent. Filters quick taps and
    # dribble-move presses that must NOT fire the shot meter.
    min_press_hold_ms: float = 75.0
    # Minimum sustained stick deflection (ms) before a stick
    # flick is treated as a tempo intent. Filters quick flicks.
    min_stick_intent_ms: float = 100.0
    # Left-stick magnitude (0..127) above which the player is
    # considered to be actively moving. If movement exceeds this
    # during the warmup window we abort the shot intent, which
    # stops running+square from becoming a stepback gesture.
    movement_abort_threshold: float = 60.0

class ShotMode(Enum):
    TEMPO_SQUARE = auto()
    TEMPO_STICK = auto()
    GOTO_STICK = auto()
    BUTTON_SHOT = auto()

class HoldState(Enum):
    IDLE = auto()
    # WARMUP gates the transition from IDLE to ARMED. While in
    # WARMUP we pass the physical input through unchanged so the
    # player's dribble/movement input is not corrupted, and we
    # only commit to a shot after a sustained-press confirmation.
    WARMUP = auto()
    ARMED = auto()
    HOLDING = auto()
    GREEN_WINDOW = auto()
    RELEASING = auto()
    COOLDOWN = auto()

@dataclass
class ShotContext:
    mode: ShotMode = ShotMode.TEMPO_SQUARE
    hold_state: HoldState = HoldState.IDLE
    arm_timestamp_ms: float = 0.0
    hold_start_ms: float = 0.0
    green_enter_ms: float = 0.0
    release_trigger_ms: float = 0.0
    release_complete_ms: float = 0.0
    cooldown_end_ms: float = 0.0
    fill_pct: float = 0.0
    confidence: float = 0.0
    velocity_pct_s: float = 0.0
    acceleration_pct_s2: float = 0.0
    green_window_start_pct: float = -1.0
    green_window_end_pct: float = -1.0
    green_window_center_pct: float = -1.0
    eta_to_green_ms: float = -1.0
    consecutive_valid_frames: int = 0
    # Age of the CV reading that produced the values above: ms between the frame
    # being captured off the wire and update_cv_data being applied. Folded into
    # eff_latency so the release accounts for how stale the meter sample is.
    frame_age_ms: float = 0.0
    rtt_offset_ms: float = 0.0
    dynamic_offset_ms: float = 0.0
    hold_frames: int = 0
    # Consecutive frames the ETA fire-condition has held true. Used to reject
    # single-frame detection-noise spikes that would otherwise fire early.
    eta_fire_streak: int = 0
    peak_fill_pct: float = 0.0
    # Set True the first time a real meter sample (detected, fill>0) arrives
    # during this shot. The fallback-timeout release is gated on this so a
    # Go-To/fade whose gather animation has not yet surfaced the meter keeps
    # holding (up to max_hold_ms) instead of firing blind at 0% fill.
    meter_seen: bool = False
    released: bool = False
    release_fill_pct: float = 0.0
    aborted: bool = False
    abort_reason: str = ''
    # Auto-classified shot archetype (Standstill / Left Fade / Right Fade /
    # Go-To / No Dip / Post Fade / Post Hook / Post Go-To), latched from the
    # live controller input at the moment the shot commits. Drives the
    # per-shot-type release offset in _active_shot_offset().
    shot_type: str = ''
    input_lost_since_ms: float = 0.0
    # History tracking (3-frame rolling window)
    right_stick_y_history: deque = field(default_factory=lambda: deque(maxlen=3))
    right_stick_x_history: deque = field(default_factory=lambda: deque(maxlen=3))
    x_button_history: deque = field(default_factory=lambda: deque(maxlen=3))
    # Matchup-specific history (for defense/matchup analysis)
    matchup_right_stick_y_history: deque = field(default_factory=lambda: deque(maxlen=3))
    matchup_right_stick_x_history: deque = field(default_factory=lambda: deque(maxlen=3))
    matchup_x_button_history: deque = field(default_factory=lambda: deque(maxlen=3))
    # Smoothed values
    smooth_right_stick_y: float = 0.0
    smooth_right_stick_x: float = 0.0
    x_button_consistent: bool = True

    def elapsed_hold_ms(self):
        if self.hold_start_ms <= 0:
            return 0.0
        return _now_ms() - self.hold_start_ms

    def elapsed_arm_ms(self):
        if self.arm_timestamp_ms <= 0:
            return 0.0
        return _now_ms() - self.arm_timestamp_ms

def _now_ms():
    return time.perf_counter() * 1000.0


def _normalize_input_mode(value, fallback='both'):
    mode = str(value or '').strip().lower().replace('-', '_')
    if mode in ('square', 'square_only', 'button'):
        return 'square_only'
    if mode in ('stick', 'stick_only'):
        return 'stick_only'
    if mode in ('both', 'auto', 'square_stick', 'square_and_stick'):
        return 'both'
    return fallback

class TemporalSuperSampler:
    """Exponentially-weighted least-squares predictor for the meter fill.

    Fits the recent (fill, time) samples with a weighted line and — once enough
    samples exist — a weighted quadratic, then returns the absolute time the
    fill is predicted to cross a target %. The quadratic is adopted ONLY when it
    explains the samples meaningfully better than the line (by weighted residual
    sum of squares), so detection noise can never let an over-flexible quadratic
    blow the prediction up. Time is centred at the latest sample (u<=0 for
    history) for numerical conditioning.
    """

    # The quadratic must cut the weighted residual to <= this fraction of the
    # line's residual to be trusted; otherwise the stiffer line wins. A quadratic
    # always fits at least as well (it is a superset model), so a flat ratio
    # threshold is what rejects marginal, noise-driven curvature.
    _QUAD_RESIDUAL_RATIO = 0.80
    # Reject any crossing predicted more than this far out as model blow-up.
    _MAX_HORIZON_MS = 1000.0

    def __init__(self, window=8):
        self._samples = deque(maxlen=max(4, window))

    def add_sample(self, fill_pct, timestamp_ms):
        self._samples.append((float(fill_pct), float(timestamp_ms)))

    def predict_crossing_ms(self, target_pct):
        n = len(self._samples)
        if n < 3:
            return -1.0
        fills = [s[0] for s in self._samples]
        times = [s[1] for s in self._samples]
        current_fill = fills[-1]
        current_time = times[-1]
        if current_fill >= target_pct:
            return current_time

        lam = 0.5
        weights = [math.exp(-lam * (n - 1 - i)) for i in range(n)]
        us = [t - current_time for t in times]

        lin_cross, lin_wrss = self._linear_crossing(us, fills, weights, target_pct, current_time)
        if n >= 4:
            quad_cross, quad_wrss = self._quadratic_crossing(us, fills, weights, target_pct, current_time)
            if (quad_cross is not None and quad_cross > 0.0
                    and quad_wrss <= lin_wrss * self._QUAD_RESIDUAL_RATIO):
                return quad_cross
        if lin_cross is not None and lin_cross > 0.0:
            return lin_cross
        return -1.0

    @staticmethod
    def _linear_crossing(us, fills, weights, target_pct, current_time):
        """Returns (crossing_ms_or_None, weighted_residual_sum_of_squares)."""
        sw = sum(weights)
        if sw < 1e-09:
            return None, float('inf')
        um = sum(w * u for w, u in zip(weights, us)) / sw
        fm = sum(w * f for w, f in zip(weights, fills)) / sw
        num = sum(w * (u - um) * (f - fm) for w, u, f in zip(weights, us, fills))
        den = sum(w * (u - um) ** 2 for w, u in zip(weights, us))
        if abs(den) < 1e-09:
            return None, float('inf')
        slope = num / den  # %/ms
        intercept = fm - slope * um  # fitted value at u=0 (the latest sample)
        wrss = sum(w * (f - (intercept + slope * u)) ** 2 for w, u, f in zip(weights, us, fills))
        if slope <= 0.01:
            return None, wrss
        delta_pct = target_pct - intercept
        if delta_pct <= 0:
            return current_time, wrss
        return current_time + delta_pct / slope, wrss

    @classmethod
    def _quadratic_crossing(cls, us, fills, weights, target_pct, current_time):
        """Weighted LS fit f = a*u^2 + b*u + c via normal equations.
        Returns (crossing_ms_or_None, weighted_residual_sum_of_squares)."""
        try:
            s0 = sum(weights)
            s1 = sum(w * u for w, u in zip(weights, us))
            s2 = sum(w * u * u for w, u in zip(weights, us))
            s3 = sum(w * u ** 3 for w, u in zip(weights, us))
            s4 = sum(w * u ** 4 for w, u in zip(weights, us))
            t0 = sum(w * f for w, f in zip(weights, fills))
            t1 = sum(w * u * f for w, u, f in zip(weights, us, fills))
            t2 = sum(w * u * u * f for w, u, f in zip(weights, us, fills))
            # Solve 3x3 [[s4,s3,s2],[s3,s2,s1],[s2,s1,s0]] [a,b,c]^T = [t2,t1,t0]^T
            m = [[s4, s3, s2], [s3, s2, s1], [s2, s1, s0]]
            v = [t2, t1, t0]
            det = (
                m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
            )
            if abs(det) < 1e-12:
                return None, float('inf')

            def _solve_col(col):
                mm = [row[:] for row in m]
                for r in range(3):
                    mm[r][col] = v[r]
                return (
                    mm[0][0] * (mm[1][1] * mm[2][2] - mm[1][2] * mm[2][1])
                    - mm[0][1] * (mm[1][0] * mm[2][2] - mm[1][2] * mm[2][0])
                    + mm[0][2] * (mm[1][0] * mm[2][1] - mm[1][1] * mm[2][0])
                ) / det

            a = _solve_col(0)
            b = _solve_col(1)
            c = _solve_col(2)
            wrss = sum(w * (f - (a * u * u + b * u + c)) ** 2 for w, u, f in zip(weights, us, fills))
            # Solve a*u^2 + b*u + (c - target) = 0 for the smallest u > 0.
            cc = c - target_pct
            if abs(a) < 1e-09:
                if abs(b) < 1e-12:
                    return None, wrss
                u = -cc / b
                return (current_time + u, wrss) if u > 0 else (None, wrss)
            disc = b * b - 4.0 * a * cc
            if disc < 0:
                return None, wrss
            sq = math.sqrt(disc)
            roots = [(-b + sq) / (2.0 * a), (-b - sq) / (2.0 * a)]
            positive = sorted(u for u in roots if u > 1e-06)
            if not positive:
                return None, wrss
            u = positive[0]
            if u > cls._MAX_HORIZON_MS:
                return None, wrss
            return current_time + u, wrss
        except Exception:
            return None, float('inf')

    def reset(self):
        self._samples.clear()

class GreenWindowTracker:

    def __init__(self):
        self._start_pct_history = deque(maxlen=12)
        self._end_pct_history = deque(maxlen=12)
        self._confirmed = False
        self._stable_frames = 0
        self._best_start_pct = -1.0
        self._best_end_pct = -1.0
        self._best_center_pct = -1.0
        self._width_pct = 0.0
        self._confidence = 0.0

    def update(self, start_pct, end_pct, confidence):
        if start_pct < 0 or end_pct < 0 or end_pct <= start_pct:
            self._stable_frames = max(0, self._stable_frames - 1)
            return
        self._start_pct_history.append(start_pct)
        self._end_pct_history.append(end_pct)
        if len(self._start_pct_history) >= 3:
            starts = list(self._start_pct_history)[-3:]
            ends = list(self._end_pct_history)[-3:]
            start_range = max(starts) - min(starts)
            end_range = max(ends) - min(ends)
            if start_range < 2.0 and end_range < 2.0:
                self._stable_frames += 1
                self._best_start_pct = sorted(starts)[len(starts) // 2]
                self._best_end_pct = sorted(ends)[len(ends) // 2]
                self._best_center_pct = (self._best_start_pct + self._best_end_pct) / 2.0
                self._width_pct = self._best_end_pct - self._best_start_pct
                self._confidence = confidence
                self._confirmed = self._stable_frames >= 3
            else:
                self._stable_frames = max(0, self._stable_frames - 1)
        else:
            self._stable_frames = 0

    def get_target_pct(self, mode='end'):
        if not self._confirmed:
            return -1.0
        if mode == 'start':
            return self._best_start_pct
        elif mode == 'center':
            return self._best_center_pct
        elif mode == 'tip':
            return self._best_start_pct + self._width_pct * 0.9
        else:
            return self._best_end_pct

    @property
    def confirmed(self):
        return self._confirmed

    @property
    def width_pct(self):
        return self._width_pct

    @property
    def center_pct(self):
        return self._best_center_pct

    def reset(self):
        self._start_pct_history.clear()
        self._end_pct_history.clear()
        self._confirmed = False
        self._stable_frames = 0
        self._best_start_pct = -1.0
        self._best_end_pct = -1.0
        self._best_center_pct = -1.0
        self._width_pct = 0.0
        self._confidence = 0.0

class RemapEngine:

    def __init__(self, config=None):
        self._config = config or RemapConfig()
        self._config.input_mode = _normalize_input_mode(self._config.input_mode, 'both')
        self._shot = ShotContext()
        self._sampler = TemporalSuperSampler(window=8)
        self._green_tracker = GreenWindowTracker()
        self._lock = threading.Lock()
        self._rtt_engine = None
        self._feedback_seed_ms = 0.0
        self._feedback_offset_ms = 0.0
        self._seed_feedback_offset(self._config)
        self._prev_square = False
        self._prev_stick_down = False
        self._prev_stick_up = False
        # Shot archetype classified from the live input every frame; latched
        # into the ShotContext when a shot commits.
        self._live_shot_type = 'Standstill'
        self._stick_down_latched = False
        self._stick_up_latched = False
        self._stick_up_frames = 0
        self._stats = {'shots_attempted': 0, 'shots_released': 0, 'shots_aborted': 0, 'avg_hold_ms': 0.0, 'last_release_fill_pct': 0.0, 'greens_hit': 0}
        self._hold_times = deque(maxlen=50)
        # Rolling window of recent timing errors (ms, signed). Used to drive a
        # median-filtered feedback correction that ignores per-shot jitter
        # outliers and converges on the systematic bias within a few shots.
        self._release_errors = deque(maxlen=7)

    @property
    def config(self):
        return self._config

    def _seed_feedback_offset(self, config):
        """Pre-bias the feedback offset from the per-meter timing baseline so
        the converging loop starts near the right per-meter value. Only seeds
        when no shots have been recorded yet (or the offset is still at the
        previous seed), so it never clobbers a loop that has already learned."""
        seed = 0.0
        if getattr(config, 'meter_timing_seed_enabled', True):
            seed = meter_timing_seed_ms(getattr(config, 'meter_style', ''))
        # Only move the live offset if it is still untouched / equal to the old
        # seed; a loop that has already adapted away from the seed is left alone.
        if abs(self._feedback_offset_ms - self._feedback_seed_ms) < 1e-6:
            self._feedback_offset_ms = seed
        self._feedback_seed_ms = seed

    def set_rtt_engine(self, rtt_engine):
        with self._lock:
            self._rtt_engine = rtt_engine

    def _verified_tick_snapshot(self):
        """Return the RTT snapshot only when every phase-authority gate is true.

        ``phase_locked`` by itself only says that relayed packet arrivals have a
        stable cadence.  It does not prove that their phase is calibrated to the
        console/game clock.  Treat absent fields from an older sidecar as false
        so a compatibility downgrade cannot restore the old diagnostic-phase
        timing path.
        """
        engine = self._rtt_engine
        if engine is None:
            return None
        try:
            snap = engine.get_snapshot()
            phase_authoritative = (
                bool(getattr(snap, 'ready', False))
                and bool(getattr(snap, 'target_verified', False))
                and bool(getattr(snap, 'phase_locked', False))
                and bool(getattr(snap, 'phase_source_verified', False))
                and float(getattr(snap, 'phase_confidence', 0.0) or 0.0) >= 0.55
            )
            if phase_authoritative:
                return snap
        except Exception:
            # RTT/tick telemetry is optional.  Any malformed or stale snapshot
            # must fail closed to the unaligned detector deadline.
            pass
        return None

    def _align_release_to_verified_tick(self, desired_release_ms):
        """Apply tick alignment only when phase authority is verified."""
        if self._verified_tick_snapshot() is not None:
            try:
                return self._rtt_engine.align_release(desired_release_ms)
            except Exception:
                pass
        return desired_release_ms

    def update_config(self, config):
        with self._lock:
            config.input_mode = _normalize_input_mode(getattr(config, 'input_mode', 'both'), self._config.input_mode)
            self._config = config
            self._seed_feedback_offset(config)

    # Compatibility alias used by sidecar update_remap hot-path.
    def set_config(self, config):
        self.update_config(config)

    def update_cv_data(self, fill_pct, confidence, velocity_pct_s, green_start_pct=-1.0, green_end_pct=-1.0, green_center_pct=-1.0, eta_to_green_ms=-1.0, consecutive_valid_frames=0, meter_detected=False, acceleration_pct_s2=0.0, frame_age_ms=0.0):
        with self._lock:
            self._shot.fill_pct = fill_pct
            self._shot.confidence = confidence
            self._shot.velocity_pct_s = velocity_pct_s
            self._shot.acceleration_pct_s2 = acceleration_pct_s2
            self._shot.green_window_start_pct = green_start_pct
            self._shot.green_window_end_pct = green_end_pct
            self._shot.green_window_center_pct = green_center_pct
            self._shot.eta_to_green_ms = eta_to_green_ms
            self._shot.consecutive_valid_frames = consecutive_valid_frames
            try:
                self._shot.frame_age_ms = max(0.0, min(80.0, float(frame_age_ms)))
            except (TypeError, ValueError):
                self._shot.frame_age_ms = 0.0
            self._shot.peak_fill_pct = max(self._shot.peak_fill_pct, fill_pct)
            if meter_detected and fill_pct > 0:
                self._shot.meter_seen = True
                self._sampler.add_sample(fill_pct, _now_ms())
            if green_start_pct >= 0 and green_end_pct >= 0:
                self._green_tracker.update(green_start_pct, green_end_pct, confidence)

    def update_network_offset(self, offset_ms):
        with self._lock:
            self._shot.rtt_offset_ms = offset_ms

    def update_dynamic_offset(self, offset_ms):
        """Apply a bounded per-shot correction that may change every frame."""
        with self._lock:
            try:
                value = float(offset_ms)
            except (TypeError, ValueError):
                value = 0.0
            self._shot.dynamic_offset_ms = max(-20.0, min(20.0, value))

    def process(self, state):
        with self._lock:
            if not self._config.enabled:
                return state
            now = _now_ms()
            
            # Store original button bytes (preserves source)
            original_button_byte = state.buttons
            
            # Update tracking history (3-frame rolling window)
            self.update_tracking_history(state, self._shot)
            
            # Update shooting state with smoothed values
            self.update_shooting_state(state, self._shot)
            
            # Decode face buttons from input
            square_now = bool(original_button_byte & DS4Button.SQUARE)
            stick_down_now, stick_up_now = self._compute_stick_states(state)
            square_rising = square_now and (not self._prev_square)
            stick_down_rising = stick_down_now and (not self._prev_stick_down)
            stick_up_rising = stick_up_now and (not self._prev_stick_up)
            self._prev_square = square_now
            self._prev_stick_down = stick_down_now
            if stick_up_now:
                self._stick_up_frames += 1
            else:
                self._stick_up_frames = 0
            self._prev_stick_up = stick_up_now
            # Auto-classify the shot archetype from the concurrent input every
            # frame. Latched into the ShotContext when the shot commits
            # (_begin_shot), and used to pick the per-type release offset.
            self._live_shot_type = self._classify_shot_type(state)
            shot = self._shot
            hs = shot.hold_state
            input_active = False
            if shot.mode == ShotMode.TEMPO_SQUARE:
                input_active = square_now
            elif shot.mode == ShotMode.TEMPO_STICK:
                input_active = stick_down_now
            elif shot.mode == ShotMode.GOTO_STICK:
                input_active = stick_up_now
            elif shot.mode == ShotMode.BUTTON_SHOT:
                input_active = square_now
            
            # Encode face buttons selectively
            output = self._encode_face_buttons_selective(original_button_byte, shot, hs)
            
            # Copy axes and other fields
            output.dpad = state.dpad
            output.left_stick_x = state.left_stick_x
            output.left_stick_y = state.left_stick_y
            output.right_stick_x = state.right_stick_x
            output.right_stick_y = state.right_stick_y
            output.l2_trigger = state.l2_trigger
            output.r2_trigger = state.r2_trigger
            output.timestamp_ns = state.timestamp_ns
            
            if hs == HoldState.IDLE:
                output = self._process_idle(output, square_rising, stick_down_rising, stick_up_rising, now)
            elif hs == HoldState.WARMUP:
                output = self._process_warmup(output, input_active, now)
            elif hs == HoldState.ARMED:
                output = self._process_armed(output, input_active, now)
            elif hs == HoldState.HOLDING:
                output = self._process_holding(output, input_active, now)
            elif hs == HoldState.GREEN_WINDOW:
                output = self._process_green_window(output, now)
            elif hs == HoldState.RELEASING:
                output = self._process_releasing(output, now)
            elif hs == HoldState.COOLDOWN:
                output = self._process_cooldown(output, now)
            return output
    
    def _encode_face_buttons_selective(self, original_button_byte, shot, hold_state):
        """Pass everything through except Square while the bot owns a shot."""
        output_buttons = original_button_byte
        if hold_state != HoldState.IDLE:
            output_buttons &= ~DS4Button.SQUARE
        output = ControllerState(buttons=output_buttons)
        return output
    
    def update_tracking_history(self, state, shot):
        """Update rolling windows for X button and right stick values from all 3 frames"""
        # Update X button history
        x_button_pressed = bool(state.buttons & DS4Button.SQUARE)
        shot.x_button_history.append(x_button_pressed)
        
        # Update right stick history
        shot.right_stick_y_history.append(state.right_stick_y)
        shot.right_stick_x_history.append(state.right_stick_x)
    
    def update_shooting_state(self, state, shot):
        """Update shooting state properties (called automatically from get_inputs)"""
        # Calculate smoothed values from history
        if len(shot.right_stick_y_history) >= 3:
            shot.smooth_right_stick_y = sum(shot.right_stick_y_history) / len(shot.right_stick_y_history)
        else:
            shot.smooth_right_stick_y = state.right_stick_y
        
        if len(shot.right_stick_x_history) >= 3:
            shot.smooth_right_stick_x = sum(shot.right_stick_x_history) / len(shot.right_stick_x_history)
        else:
            shot.smooth_right_stick_x = state.right_stick_x
        
        # Calculate X button consistency
        if len(shot.x_button_history) >= 3:
            shot.x_button_consistent = all(shot.x_button_history) or not any(shot.x_button_history)
        else:
            shot.x_button_consistent = True

    def _compute_stick_states(self, state):
        cfg = self._config
        down_on = max(0.0, cfg.stick_down_threshold) * 1.27
        up_on = max(0.0, cfg.stick_up_threshold) * 1.27
        hysteresis = max(0.0, cfg.stick_hysteresis_pct) * 1.27
        down_off = max(0.0, down_on - hysteresis)
        up_off = max(0.0, up_on - hysteresis)
        y = float(state.right_stick_y)

        if self._stick_down_latched:
            self._stick_down_latched = y > down_off
        else:
            self._stick_down_latched = y > down_on

        if self._stick_up_latched:
            self._stick_up_latched = y < -up_off
        else:
            self._stick_up_latched = y < -up_on

        return self._stick_down_latched, self._stick_up_latched

    def _classify_shot_type(self, state):
        """Classify the shot archetype from the concurrent controller input.

        Detection (per the 2K input signatures), most specific first. L2 is the
        discriminator that separates post moves from the standard set; the right
        stick X component separates a diagonal flick (Post Hook) from a straight
        up hold (Post Go-To):

            L2 + right-stick up DIAGONAL ............ Post Hook
            L2 + right-stick up straight ............ Post Go-To
            L2 + left-stick DOWN .................... Post Fade
            L2 (square only) ........................ No Dip
            right-stick up (no L2) .................. Go-To
            left-stick lateral (no L2) ............. Left/Right Fade
            (square only) .......................... Standstill

        Square is always the trigger (raw button mode); these other inputs are
        held concurrently and only select which per-type offset is applied.
        """
        cfg = self._config
        try:
            l2 = float(getattr(state, 'l2_trigger', 0) or 0)
            r2 = float(getattr(state, 'r2_trigger', 0) or 0)
            rs_x = float(getattr(state, 'right_stick_x', 0) or 0)
            rs_y = float(getattr(state, 'right_stick_y', 0) or 0)
            ls_x = float(getattr(state, 'left_stick_x', 0) or 0)
            ls_y = float(getattr(state, 'left_stick_y', 0) or 0)
        except Exception:
            return 'Standstill'

        # Triggers travel 0..255; a deliberate hold is well above noise.
        l2_held = l2 >= 80.0
        r2_held = r2 >= 80.0
        # Stick deflection thresholds (axes are -128..127; up is negative Y).
        up_on = max(1.0, cfg.stick_up_threshold) * 1.27
        rs_up = rs_y < -up_on
        rs_diagonal = abs(rs_x) >= 45.0
        move_on = max(1.0, cfg.movement_abort_threshold)
        ls_down = ls_y > move_on
        ls_lateral = abs(ls_x) >= move_on and abs(ls_x) >= abs(ls_y)

        # --- Post moves: all require L2 held (unchanged) ---
        if l2_held:
            if rs_up and rs_diagonal:
                return 'Post Hook'
            if rs_up:
                return 'Post Go-To'
            if ls_down:
                return 'Post Fade'
            return 'No Dip'

        # --- R2 fade: R2 is held for the whole shot + left-stick direction +
        # square = fadeaway. Because R2 is an explicit fade modifier, a smaller
        # stick deflection still counts (the held trigger disambiguates a fade
        # from an accidental drift). Direction comes from the dominant stick axis.
        if r2_held:
            r2_on = max(1.0, move_on * 0.6)
            if abs(ls_x) >= r2_on and abs(ls_x) >= abs(ls_y):
                return 'Left Fade' if ls_x < 0 else 'Right Fade'
            if abs(ls_y) >= r2_on:
                # Backward/forward hold with R2 still resolves to a fade; pick
                # the side from whatever lateral lean exists (defaults right).
                return 'Left Fade' if ls_x < 0 else 'Right Fade'

        # --- Standard set (no trigger) ---
        if rs_up:
            return 'Go-To'
        if ls_lateral:
            return 'Left Fade' if ls_x < 0 else 'Right Fade'
        return 'Standstill'

    def _process_idle(self, state, sq_rise, stick_down_rise, stick_up_rise, now):
        cfg = self._config
        trigger = getattr(cfg, 'shot_trigger_mode', 'button')
        if trigger == 'passthrough':
            return state

        input_mode = getattr(cfg, 'input_mode', 'both')

        if sq_rise and input_mode in ('square_only', 'both'):
            self._begin_warmup(ShotMode.BUTTON_SHOT, now)
            state.set_button(DS4Button.SQUARE, False)
        elif stick_down_rise and input_mode in ('stick_only', 'both'):
            self._begin_warmup(ShotMode.TEMPO_STICK, now)
            state.set_button(DS4Button.SQUARE, False)
        elif stick_up_rise and input_mode in ('stick_only', 'both'):
            self._begin_warmup(ShotMode.GOTO_STICK, now)
            state.set_button(DS4Button.SQUARE, False)
        else:
            state.set_button(DS4Button.SQUARE, False)

        return state

    def _begin_warmup(self, mode, now):
        # Light-touch sibling of _begin_shot: arms a candidate shot
        # without hijacking the controller. If the player releases or
        # starts moving, the warmup is cancelled and no shot fires.
        self._shot = ShotContext(
            mode=mode,
            hold_state=HoldState.WARMUP,
            arm_timestamp_ms=now,
            rtt_offset_ms=self._shot.rtt_offset_ms,
            dynamic_offset_ms=self._shot.dynamic_offset_ms,
        )
        self._sampler.reset()
        self._green_tracker.reset()
        logger.debug('Shot WARMUP mode=%s', mode.name)

    def _process_warmup(self, state, input_active, now):
        shot = self._shot
        cfg = self._config
        # Cancel if the user released the trigger before commit.
        if not input_active:
            self._shot = ShotContext(
                hold_state=HoldState.IDLE,
                rtt_offset_ms=shot.rtt_offset_ms,
                dynamic_offset_ms=shot.dynamic_offset_ms,
            )
            logger.debug('Shot WARMUP cancelled: input released')
            return state

        if shot.mode == ShotMode.BUTTON_SHOT:
            state.set_button(DS4Button.SQUARE, False)

        # Cancel if the user is moving — this is what prevented
        # running + square from becoming a stepback before. We just
        # do not hijack the stick; the physical input flows through.
        # In BUTTON_SHOT mode the player is explicitly HOLDING square to
        # shoot, so movement (drive/fade) is intentional and must NOT abort.
        if shot.mode != ShotMode.BUTTON_SHOT:
            ls_mag = max(abs(int(state.left_stick_x)), abs(int(state.left_stick_y)))
            if ls_mag >= cfg.movement_abort_threshold:
                self._shot = ShotContext(
                    hold_state=HoldState.IDLE,
                    rtt_offset_ms=shot.rtt_offset_ms,
                    dynamic_offset_ms=shot.dynamic_offset_ms,
                )
                logger.debug('Shot WARMUP cancelled: movement detected mag=%d', ls_mag)
                return state
        elapsed = now - shot.arm_timestamp_ms
        threshold = (
            cfg.min_press_hold_ms
            if shot.mode in (ShotMode.TEMPO_SQUARE, ShotMode.BUTTON_SHOT)
            else cfg.min_stick_intent_ms
        )
        if elapsed >= threshold:
            # Commit: promote to ARMED and start hijacking the output.
            self._begin_shot(shot.mode, now)
            return self._apply_hold_output(state, shot.mode)
        return state

    def _begin_shot(self, mode, now):
        prev = self._shot
        self._shot = ShotContext(
            mode=mode,
            hold_state=HoldState.ARMED,
            arm_timestamp_ms=now,
            rtt_offset_ms=prev.rtt_offset_ms,
            dynamic_offset_ms=prev.dynamic_offset_ms,
            # Preserve CV signals captured during WARMUP so the immediate
            # next frame can transition ARMED -> HOLDING when the meter is
            # already in view. Without this preservation we drop the
            # accumulated frames every time we promote the shot.
            fill_pct=prev.fill_pct,
            confidence=prev.confidence,
            velocity_pct_s=prev.velocity_pct_s,
            acceleration_pct_s2=prev.acceleration_pct_s2,
            green_window_start_pct=prev.green_window_start_pct,
            green_window_end_pct=prev.green_window_end_pct,
            green_window_center_pct=prev.green_window_center_pct,
            eta_to_green_ms=prev.eta_to_green_ms,
            consecutive_valid_frames=prev.consecutive_valid_frames,
            peak_fill_pct=prev.peak_fill_pct,
            meter_seen=prev.meter_seen,
            # Latch the live-classified archetype so the per-type release
            # offset is locked in at commit (inputs may change mid-animation).
            shot_type=self._live_shot_type,
        )
        self._sampler.reset()
        self._green_tracker.reset()
        self._stats['shots_attempted'] += 1
        _tlog('Shot ARMED mode=%s type=%s', mode.name, self._live_shot_type)

    def _process_armed(self, state, input_active, now):
        shot = self._shot
        cfg = self._config
        if not input_active:
            if shot.input_lost_since_ms <= 0.0:
                shot.input_lost_since_ms = now
            elif (now - shot.input_lost_since_ms) >= cfg.input_release_grace_ms:
                self._abort(f'{shot.mode.name.lower()}_released_before_meter')
                return state
        else:
            shot.input_lost_since_ms = 0.0
        if shot.consecutive_valid_frames >= cfg.stable_frames_required and shot.confidence >= cfg.confidence_gate:
            shot.hold_state = HoldState.HOLDING
            shot.hold_start_ms = now
            _tlog('Shot HOLDING (fill=%.1f%%, conf=%.2f)', shot.fill_pct, shot.confidence)
        elif shot.elapsed_arm_ms() > cfg.tempo_fallback_timeout_ms:
            shot.hold_state = HoldState.HOLDING
            shot.hold_start_ms = now
            _tlog('Shot HOLDING (timeout fallback)')
        return self._apply_hold_output(state, shot.mode)

    def _process_holding(self, state, input_active, now):
        shot = self._shot
        cfg = self._config
        shot.hold_frames += 1
        state = self._apply_hold_output(state, shot.mode)
        # Live shot-type adjustment: the player's stick/trigger may change as the
        # animation develops (e.g. a Go-To that becomes a fade). Re-latch the
        # archetype every frame from the live-classified input so the per-type
        # release offset and commit-min track what is actually being shot.
        # Only adopt a concrete classification; never blank out a latched type.
        if self._live_shot_type:
            shot.shot_type = self._live_shot_type
        elapsed = shot.elapsed_hold_ms()
        if elapsed > cfg.max_hold_ms:
            self._abort('max_hold_exceeded')
            return state
        if not input_active:
            if shot.input_lost_since_ms <= 0.0:
                shot.input_lost_since_ms = now
            elif (now - shot.input_lost_since_ms) >= cfg.input_release_grace_ms and elapsed < cfg.min_hold_ms:
                # Pump-fake guard: only abort if the meter has NOT meaningfully
                # surfaced yet. Once the meter is up and filling, a brief input
                # dropout is controller/USB noise on a committed shot, not a
                # pump fake — aborting there would brick a real shot.
                if not shot.meter_seen and shot.peak_fill_pct < 15.0:
                    self._abort('early_release_pump_fake')
                    return state
        else:
            shot.input_lost_since_ms = 0.0
        # Commit gate: keep holding until the archetype's animation has played
        # out, so the meter has surfaced and can actually be timed. Go-To/fade
        # gather animations show the meter well after the button goes down;
        # releasing before that fires at 0% fill and bricks the shot. Measured
        # from shot arm (button-down) rather than hold_start so the whole
        # animation counts, not just the post-meter portion.
        if shot.elapsed_arm_ms() < self._active_commit_min_ms():
            return state
        strategy = cfg.hold_release_strategy
        if strategy == 'fixed':
            if elapsed >= cfg.fixed_hold_ms:
                self._trigger_release(now)
        elif strategy == 'predictive' or strategy == 'hybrid':
            self._check_predictive_release(now, elapsed, strategy)
        elif shot.fill_pct >= 90.0:
            self._trigger_release(now)
        return state

    def _confirm_eta_fire(self, shot, time_reached):
        """Require the ETA fire-condition to hold for >=1 frame (clean signal)
        or >=2 frames (noisy/unstable signal) before committing the release.

        Because ETA decreases monotonically as the meter fills, a genuine
        crossing keeps this condition true on every subsequent frame, so the
        streak accumulates naturally. An isolated low-ETA spike from detection
        noise resets the streak and is rejected. The (at most) one-frame extra
        delay on noisy crossings is a constant the feedback loop pre-compensates.
        """
        if not time_reached:
            # Decay rather than hard-reset: a single jitter spike that briefly
            # flips the condition false should not throw away an accumulating
            # genuine crossing and push the release a whole frame (~16.7ms) late.
            shot.eta_fire_streak = max(0, shot.eta_fire_streak - 1)
            return False
        shot.eta_fire_streak += 1
        cfg = self._config
        stable = (
            shot.confidence >= cfg.confidence_gate
            and shot.velocity_pct_s >= 20.0
            and abs(shot.acceleration_pct_s2) <= 350.0
        )
        required = 1 if stable else 2
        # Low-jitter override: when the network is phase-locked and jitter is
        # tiny, the crossing estimate is trustworthy on the first confirmed
        # frame, so accept it immediately even if velocity briefly dipped. This
        # removes the ~one-frame (~8-16ms) late bias under ideal conditions,
        # which is where the bot should be perfect.
        if required > 1:
            snap = self._verified_tick_snapshot()
            if snap is not None:
                try:
                    if float(getattr(snap, 'jitter_ms', 99.0)) <= 4.0:
                        required = 1
                except (TypeError, ValueError):
                    pass
        return shot.eta_fire_streak >= required

    def _active_shot_offset(self):
        """Fixed release offset (ms) for the shot type detected at commit.

        Prefers the archetype auto-classified from the live input and latched
        into the ShotContext at _begin_shot; falls back to config.active_shot_type
        only if no type was detected (e.g. before the first shot commits)."""
        try:
            offsets = getattr(self._config, 'shot_type_offsets', None) or {}
            detected = getattr(self._shot, 'shot_type', '') or ''
            key = detected or getattr(self._config, 'active_shot_type', '')
            return float(offsets.get(key, 0.0))
        except Exception:
            return 0.0

    def _active_commit_min_ms(self):
        """Minimum hold time (ms, since shot arm) before a release is allowed for
        the latched archetype. Forces the shot animation to play out so the meter
        surfaces before the engine tries to time it (essential for Go-To/fade)."""
        try:
            commits = getattr(self._config, 'shot_type_commit_ms', None) or {}
            detected = getattr(self._shot, 'shot_type', '') or ''
            key = detected or getattr(self._config, 'active_shot_type', '')
            return max(0.0, float(commits.get(key, 0.0)))
        except Exception:
            return 0.0

    def _fused_eta_ms(self, detector_eta_ms, center_pct, now):
        """Fuse the detector's instantaneous (kinematic) ETA with the
        super-sampler's multi-point weighted-LS crossing ETA to the SAME target.
        Two independent estimators of the same quantity -> blending cuts the
        release-time variance, so greens land more repeatably.

        The blend is CONFIDENCE-WEIGHTED: the super-sampler earns trust as its
        sample window fills (more points -> lower-variance fit), capped so the
        responsive instantaneous estimate always retains a meaningful share.
        Falls back to whichever single estimate is available."""
        sampler_eta = -1.0
        n_samp = len(self._sampler._samples)
        if center_pct and center_pct > 0.0 and n_samp >= 3:
            cross = self._sampler.predict_crossing_ms(center_pct)
            if cross > 0.0:
                sampler_eta = max(0.0, cross - now)
        have_det = detector_eta_ms is not None and detector_eta_ms >= 0.0
        have_samp = sampler_eta >= 0.0
        if have_det and have_samp:
            maxlen = self._sampler._samples.maxlen or n_samp
            w_samp = min(1.0, n_samp / float(maxlen)) * 0.6  # cap sampler at 60%
            return (1.0 - w_samp) * detector_eta_ms + w_samp * sampler_eta
        if have_det:
            return detector_eta_ms
        return sampler_eta

    def _check_predictive_release(self, now, elapsed_hold, strategy):
        shot = self._shot
        cfg = self._config
        # When release_enabled is False, the C++ AutomationEngine is the sole
        # release authority. Skip all release logic — input routing (stick
        # remap, button passthrough) still flows through process().
        if not getattr(cfg, 'release_enabled', True):
            return
        # eff_latency is subtracted from the desired fire time, so a larger value
        # fires earlier. frame_age_ms is the INPUT-side staleness (capture ->
        # detection -> this decision); eta/fill were measured at capture time but
        # are compared against the current clock, so the true remaining eta is
        # (eta - frame_age). Folding frame_age in here exactly corrects for that.
        # It is additive to gpc_flick_chain_ms, which is the OUTPUT-side latency
        # (ViGEm -> console -> render), not double counting.
        eff_latency = cfg.gpc_flick_chain_ms + shot.rtt_offset_ms + cfg.early_late_offset_ms + shot.dynamic_offset_ms + self._feedback_offset_ms + self._active_shot_offset() + shot.frame_age_ms + cfg.output_latency_ms
        is_velocity_unstable = (abs(shot.acceleration_pct_s2) > 350.0 or shot.velocity_pct_s < 20.0)
        required_stable = cfg.stable_frames_required + (1 if is_velocity_unstable else 0)
        signal_ok = (
            shot.hold_state == HoldState.HOLDING
            or (shot.consecutive_valid_frames >= required_stable and shot.confidence >= cfg.confidence_gate)
        )

        # ----- Determine whether the meter has reached the predicted fire point.
        # Every sub-condition below is collapsed into a single `fire_now` flag so
        # that the noise-rejecting confirmation gate (_confirm_eta_fire) applies
        # uniformly. Previously each path fired independently, so a blocked path
        # would simply fall through to the next and fire unconfirmed.
        fire_now = False
        reason = ''

        def _time_reached(eta_ms):
            desired_fire_at = now + eta_ms - eff_latency
            desired_fire_at = self._align_release_to_verified_tick(desired_fire_at)
            return now >= desired_fire_at

        # 1) Fused ETA to the green centre (primary, latency-compensated): the
        # detector's kinematic ETA blended with the super-sampler crossing to the
        # same target for lower variance.
        if not fire_now and signal_ok and shot.eta_to_green_ms >= 0.0:
            fused_eta = self._fused_eta_ms(shot.eta_to_green_ms, shot.green_window_center_pct, now)
            if fused_eta >= 0.0 and _time_reached(fused_eta):
                fire_now, reason = True, 'fused_eta'

        # 2) Live green-window target fill already crossed (we are at/just past it).
        if not fire_now and signal_ok and shot.green_window_start_pct >= 0.0 and shot.green_window_end_pct >= shot.green_window_start_pct:
            direct_target_pct = self._target_from_window(
                shot.green_window_start_pct, shot.green_window_end_pct, cfg.green_window_target,
            )
            if direct_target_pct > 0.0 and shot.fill_pct >= direct_target_pct:
                fire_now, reason = True, 'window_fill'

        # 3) Tracked/confirmed green window: fused ETA, regression crossing, or fill.
        if not fire_now and (self._green_tracker.confirmed or shot.hold_state == HoldState.HOLDING):
            target_pct = self._green_tracker.get_target_pct(cfg.green_window_target)
            if target_pct <= 0:
                # No confirmed green window: aim for the TOP of the meter, where
                # the green window always sits regardless of contest/court spot.
                target_pct = shot.green_window_center_pct if shot.green_window_center_pct > 0 else cfg.no_green_target_pct
            if target_pct > 0:
                if shot.eta_to_green_ms >= 0.0 and _time_reached(shot.eta_to_green_ms):
                    fire_now, reason = True, 'tracked_eta'
                else:
                    crossing_ms = self._sampler.predict_crossing_ms(target_pct)
                    if crossing_ms > 0:
                        fire_at = crossing_ms - eff_latency
                        fire_at = self._align_release_to_verified_tick(fire_at)
                        if now >= fire_at:
                            fire_now, reason = True, 'crossing'
                    if not fire_now and shot.fill_pct >= target_pct:
                        fire_now, reason = True, 'tracked_fill'

        if self._confirm_eta_fire(shot, fire_now):
            shot.hold_state = HoldState.GREEN_WINDOW
            shot.green_enter_ms = now
            _tlog('GREEN_WINDOW entered via %s (eta=%.1fms, fill=%.1f%%, latency_comp=%.1fms)',
                  reason, shot.eta_to_green_ms, shot.fill_pct, eff_latency)
            self._trigger_release(now)
            return

        # Hybrid safety: close enough to the fixed-hold deadline that waiting
        # longer would overshoot. Fires immediately (not subject to confirmation
        # because this IS the late-side guard).
        if strategy == 'hybrid' and shot.fill_pct >= 70.0:
            remaining_fixed = cfg.fixed_hold_ms - elapsed_hold
            if remaining_fixed <= eff_latency:
                self._trigger_release(now)
                return
        # Fallback-timeout release: only once a real meter has actually surfaced
        # this shot. For Go-To/fades (and right-stick-up holds) the gather
        # animation shows the meter well after button-down; firing the fallback
        # before that would release blind at ~0% fill. If the meter never
        # appears, the max_hold_ms guard in _process_holding ends the shot.
        if shot.meter_seen and elapsed_hold >= cfg.tempo_fallback_timeout_ms:
            self._trigger_release(now)

    def _process_green_window(self, state, now):
        self._shot.hold_state = HoldState.RELEASING
        return self._process_releasing(state, now)

    def _trigger_release(self, now):
        shot = self._shot
        shot.hold_state = HoldState.RELEASING
        shot.release_trigger_ms = now
        shot.release_fill_pct = shot.fill_pct
        self._hold_times.append(shot.elapsed_hold_ms())
        _tlog('RELEASING (fill=%.1f%%, hold=%.0fms, type=%s, mode=%s)', shot.fill_pct, shot.elapsed_hold_ms(), shot.shot_type or '-', shot.mode.name)

    def _process_releasing(self, state, now):
        shot = self._shot
        cfg = self._config
        elapsed_since_trigger = now - shot.release_trigger_ms
        
        # Suppress Square in all shooting modes during release
        state.set_button(DS4Button.SQUARE, False)
        
        if shot.mode == ShotMode.BUTTON_SHOT:
            if elapsed_since_trigger >= cfg.release_pulse_ms:
                self._complete_release(now)
        elif shot.mode == ShotMode.TEMPO_STICK:
            # Rhythm shot: flick right stick UP (Y = -128) during release
            state.right_stick_y = -128
            state.right_stick_x = 0
            if elapsed_since_trigger >= cfg.tempo_flick_hold_ms:
                self._complete_release(now)
        elif shot.mode == ShotMode.GOTO_STICK:
            # Go-To shot: return right stick to neutral (Y = 0)
            state.right_stick_y = 0
            state.right_stick_x = 0
            if elapsed_since_trigger >= cfg.release_pulse_ms:
                self._complete_release(now)
        elif shot.mode == ShotMode.TEMPO_SQUARE:
            if elapsed_since_trigger >= cfg.release_pulse_ms:
                self._complete_release(now)
                
        return state

    @staticmethod
    def _apply_hold_output(state, mode):
        """Apply hold outputs for the active shot mode."""
        if mode == ShotMode.BUTTON_SHOT:
            state.set_button(DS4Button.SQUARE, True)
        elif mode == ShotMode.TEMPO_STICK:
            state.right_stick_y = 127
            state.right_stick_x = 0
            state.set_button(DS4Button.SQUARE, False)
        elif mode == ShotMode.GOTO_STICK:
            state.right_stick_y = -128
            state.right_stick_x = 0
            state.set_button(DS4Button.SQUARE, False)
        return state

    @staticmethod
    def _target_from_window(start_pct, end_pct, mode='tip'):
        start = max(0.0, min(100.0, float(start_pct)))
        end = max(0.0, min(100.0, float(end_pct)))
        if end < start:
            start, end = end, start
        width = max(0.0, end - start)
        mode = str(mode or 'tip').lower()
        if mode == 'start':
            return start
        if mode == 'center':
            return start + width * 0.5
        if mode == 'end':
            return end
        return start + width * 0.9

    def _complete_release(self, now):
        shot = self._shot
        shot.hold_state = HoldState.COOLDOWN
        shot.release_complete_ms = now
        shot.cooldown_end_ms = now + 200.0
        shot.released = True
        self._stats['shots_released'] += 1
        if shot.release_fill_pct >= 90.0:
            self._stats['greens_hit'] += 1
        if self._hold_times:
            self._stats['avg_hold_ms'] = round(sum(self._hold_times) / len(self._hold_times), 1)
        self._stats['last_release_fill_pct'] = round(shot.release_fill_pct, 1)
        logger.debug('RELEASED fill=%.1f%% hold=%.0fms mode=%s', shot.release_fill_pct, shot.elapsed_hold_ms(), shot.mode.name)

    def _process_cooldown(self, state, now):
        shot = self._shot
        if now >= shot.cooldown_end_ms:
            # Skip self-learning feedback when release_enabled is False — the
            # C++ AutomationEngine owns the learning loop in that mode.
            if shot.released and not shot.aborted and shot.peak_fill_pct > 15.0 \
                    and getattr(self._config, 'release_enabled', True):
                # Same target the release aimed at, so the feedback error is
                # measured against the actual goal (top-of-meter when no green).
                target_pct = shot.green_window_center_pct if shot.green_window_center_pct > 0.0 else self._config.no_green_target_pct
                if target_pct > 0.0:
                    # Convert the where-it-landed error (peak fill vs target) into
                    # a timing error in ms using the meter velocity at release.
                    error_pct = shot.peak_fill_pct - target_pct
                    vel_ms = max(0.05, shot.velocity_pct_s / 1000.0)
                    error_ms = max(-35.0, min(35.0, error_pct / vel_ms))
                    self._release_errors.append(error_ms)
                    # Median over the recent window rejects single jittery shots
                    # (a one-off network spike) so we only chase the persistent
                    # systematic bias, not the noise.
                    samples = sorted(self._release_errors)
                    n = len(samples)
                    # Guard: never move the offset off a single shot. With one
                    # sample the "median" IS that shot, so a lone 30ms jitter
                    # spike would bias the offset up to ~±19ms and take many
                    # shots to decay. Require at least two samples first.
                    if n >= 2:
                        median_err = samples[n // 2] if n % 2 else 0.5 * (samples[n // 2 - 1] + samples[n // 2])
                        # Adaptive gain: converge fast while we are still learning
                        # (few samples / large persistent error), then settle to a
                        # gentle gain so the locked offset stays stable.
                        k_p = 0.55 if n < 4 else 0.32
                        if abs(median_err) > 18.0:
                            k_p = min(0.7, k_p + 0.15)
                        # Sign: a positive error means the meter OVERSHOT the
                        # target (released too late), which means we need MORE
                        # latency compensation so the next shot fires earlier.
                        # eff_latency is subtracted from the fire time (larger ->
                        # earlier), and _feedback_offset_ms is an additive term in
                        # it, so the correction is POSITIVELY correlated with the
                        # error. (A negative sign here made the loop diverge to the
                        # clamp and fire consistently the wrong way.)
                        adjustment_ms = median_err * k_p
                        self._feedback_offset_ms = max(-25.0, min(25.0, self._feedback_offset_ms + adjustment_ms))
                        logger.info("Feedback: Peak=%.1f%%, Target=%.1f%%, Err=%.1fms, MedErr=%.1fms, k=%.2f, Adj=%.1fms, Offset=%.1fms",
                                    shot.peak_fill_pct, target_pct, error_ms, median_err, k_p, adjustment_ms, self._feedback_offset_ms)
            self._shot = ShotContext(hold_state=HoldState.IDLE, rtt_offset_ms=self._shot.rtt_offset_ms, dynamic_offset_ms=self._shot.dynamic_offset_ms)
        state.set_button(DS4Button.SQUARE, False)
        if shot.mode in (ShotMode.TEMPO_STICK, ShotMode.GOTO_STICK):
            state.right_stick_y = 0
            state.right_stick_x = 0
        return state

    def _abort(self, reason):
        self._shot.aborted = True
        self._shot.abort_reason = reason
        self._shot.hold_state = HoldState.IDLE
        self._stats['shots_aborted'] += 1
        logger.debug('Shot ABORTED: %s', reason)
        self._shot = ShotContext(hold_state=HoldState.IDLE, rtt_offset_ms=self._shot.rtt_offset_ms, dynamic_offset_ms=self._shot.dynamic_offset_ms)

    def get_shot_context(self):
        with self._lock:
            return ShotContext(mode=self._shot.mode, hold_state=self._shot.hold_state, arm_timestamp_ms=self._shot.arm_timestamp_ms, hold_start_ms=self._shot.hold_start_ms, fill_pct=self._shot.fill_pct, confidence=self._shot.confidence, velocity_pct_s=self._shot.velocity_pct_s, green_window_start_pct=self._shot.green_window_start_pct, green_window_end_pct=self._shot.green_window_end_pct, eta_to_green_ms=self._shot.eta_to_green_ms, consecutive_valid_frames=self._shot.consecutive_valid_frames, rtt_offset_ms=self._shot.rtt_offset_ms, dynamic_offset_ms=self._shot.dynamic_offset_ms, hold_frames=self._shot.hold_frames, peak_fill_pct=self._shot.peak_fill_pct, released=self._shot.released, release_fill_pct=self._shot.release_fill_pct, aborted=self._shot.aborted, abort_reason=self._shot.abort_reason)

    def get_stats(self):
        with self._lock:
            return dict(self._stats)

    def reset(self):
        with self._lock:
            rtt = self._shot.rtt_offset_ms
            dyn = self._shot.dynamic_offset_ms
            self._shot = ShotContext(hold_state=HoldState.IDLE, rtt_offset_ms=rtt, dynamic_offset_ms=dyn)
            self._sampler.reset()
            self._green_tracker.reset()
            self._prev_square = False
            self._prev_stick_down = False
            self._prev_stick_up = False
            self._stick_down_latched = False
            self._stick_up_latched = False
            self._stick_up_frames = 0
            # Re-seed the converging feedback offset back to the per-meter
            # baseline rather than 0 so a fresh session starts pre-tuned.
            self._release_errors.clear()
            self._feedback_offset_ms = self._feedback_seed_ms

    @property
    def is_shooting(self):
        with self._lock:
            return self._shot.hold_state not in (HoldState.IDLE, HoldState.COOLDOWN)

    @property
    def current_state(self):
        with self._lock:
            return self._shot.hold_state

def load_remap_config(settings_path=None):
    from orion_config_io import load_settings_raw
    cfg = RemapConfig()
    raw = load_settings_raw(settings_path)
    if raw is None:
        return cfg
    cfg.enabled = bool(raw.get('meter_enabled', True))
    cfg.input_mode = _normalize_input_mode(raw.get('tempo_input_mode', 'both'))
    for key, attr, lo, hi, cast in [
        ('tempo_wait_ms', 'tempo_wait_ms', 0.0, 500.0, float),
        ('tempo_flick_hold_ms', 'tempo_flick_hold_ms', 10.0, 500.0, float),
        ('tempo_min_stick_hold_ms', 'tempo_min_stick_hold_ms', 0.0, 500.0, float),
        ('tempo_fallback_timeout_ms', 'tempo_fallback_timeout_ms', 100.0, 3000.0, float),
        ('fixed_hold_time_ms', 'fixed_hold_ms', 100.0, 2000.0, float),
        ('early_late_offset_ms', 'early_late_offset_ms', -50.0, 50.0, float),
        ('stick_down_threshold', 'stick_down_threshold', 10.0, 95.0, float),
        ('stick_up_threshold', 'stick_up_threshold', 10.0, 95.0, float),
        ('hold_stable_frames', 'stable_frames_required', 1, 10, int),
        ('detection_confidence_percent', 'confidence_gate', 5.0, 100.0, float),
        ('goto_flick_hold_ms', 'goto_flick_hold_ms', 10.0, 500.0, float),
        ('goto_arm_frames', 'goto_arm_frames', 1, 8, int),
        ('input_release_grace_ms', 'input_release_grace_ms', 0.0, 500.0, float),
        ('stick_hysteresis_pct', 'stick_hysteresis_pct', 0.0, 20.0, float),
        ('no_green_target_pct', 'no_green_target_pct', 50.0, 100.0, float),
        ('output_latency_ms', 'output_latency_ms', 0.0, 60.0, float),
    ]:
        v = raw.get(key)
        if v is not None:
            try:
                val = max(lo, min(hi, cast(v)))
                if attr == 'confidence_gate':
                    val = max(0.05, min(1.0, float(val) / 100.0))
                setattr(cfg, attr, val)
            except (TypeError, ValueError):
                pass
    gw = raw.get('green_window_target_mode', raw.get('autogreen_target', ''))
    if isinstance(gw, str) and gw.strip():
        cfg.green_window_target = gw.strip().lower()
    ms = raw.get('meter_style')
    if isinstance(ms, str) and ms.strip():
        cfg.meter_style = ms.strip()
    cfg.meter_timing_seed_enabled = bool(raw.get('meter_timing_seed_enabled', cfg.meter_timing_seed_enabled))
    cfg.green_window_priority = bool(raw.get('green_window_priority', cfg.green_window_priority))
    cfg.goto_enabled = False
    hrs = raw.get('hold_release_strategy', '')
    if isinstance(hrs, str) and hrs.strip().lower() in ('predictive', 'fixed', 'hybrid'):
        cfg.hold_release_strategy = hrs.strip().lower()
    stm = raw.get('shot_trigger_mode', '')
    if isinstance(stm, str) and stm.strip().lower() == 'passthrough':
        cfg.shot_trigger_mode = 'passthrough'
    else:
        cfg.shot_trigger_mode = 'button'
    ast = raw.get('active_shot_type')
    if isinstance(ast, str) and ast.strip():
        cfg.active_shot_type = ast.strip()
    sto = raw.get('shot_type_offsets')
    if isinstance(sto, dict):
        merged = dict(cfg.shot_type_offsets)
        for name, val in sto.items():
            try:
                merged[str(name)] = max(-80.0, min(80.0, float(val)))
            except (TypeError, ValueError):
                continue
        cfg.shot_type_offsets = merged
    return cfg
