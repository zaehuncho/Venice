from __future__ import annotations

import copy
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Low-fill green-anchor: require the green make-window above a LOW/short fill before
# accepting it. Rejects static floor/jersey blobs, but the ~1px green chevron at 720p
# is unreliable on a moving fade -> can reject the genuine early rise (late acquisition).
# Toggle OFF (ORION_LOWFILL_GREEN=0) to lean on the (now wrap-correct) purity gate alone.
_LOWFILL_GREEN_ANCHOR = os.environ.get("ORION_LOWFILL_GREEN", "1") != "0"

# GREEN-CHEVRON-FIRST anchor — DEFAULT OFF as of the red-hue-precise rework. Gating detection on
# the green chevron is wrong: the green make-window is often tiny/miniscule, so "no chevron = no
# meter" drops real frames. Detection is now by the calibrated RED signature (hue + BRIGHTNESS/val
# + geometry + play-position); green is only a non-gating release-window cue. The green-chevron
# anchor remains available as an opt-in (ORION_GREEN_FIRST=1) for green-reliable captures.
_GREEN_FIRST_ANCHOR = os.environ.get("ORION_GREEN_FIRST", "0") != "0"
# Trained-locator TEMPORAL region-lock TTL. The YOLO locator (models/orion_meter_n.pt) has ~50%
# per-frame recall but ~0% false-positives; a park shot spans ~24 frames @60fps so the locator fires
# many times per shot yet MISSES ~half the frames. On a miss frame we reuse the last locator box for
# up to this many frames to CONSTRAIN the colour scan (rejecting a red distractor elsewhere) instead
# of falling through to the classical park/anchor path. ~12 frames ~= 200ms @60fps. Env-tunable;
# wholly inert unless the locator is present + enabled (memory is only ever populated by a live hit).
_LOC_MEM_TTL_FRAMES = max(1, int(os.environ.get("ORION_LOC_MEM_TTL", "24") or "24"))
# T6 (2026-07-05): lock-ROI locator fast path — the classic "search a window around the last known
# location, full-scan only on loss" applied to the EXPENSIVE pass. Full-frame YOLO @imgsz960 measures ~60-80ms/frame on a
# CPU-only torch (live cadence bottleneck: ~15fps detection over a ~400ms meter rise). With a FRESH
# anchor (locator memory OR the last SERVED meter position — park/fallback serves count, so an edge
# shot the locator is blind to still rides the cheap path), YOLO runs on a scale-PRESERVING crop:
# the crop is inferred at the same px-per-imgsz ratio as the full frame, so the meter keeps its
# trained apparent size at ~7% of the compute. Full-frame passes still run with no fresh anchor, on
# a periodic sweep every _LOC_ROI_SWEEP frames (a carryover/second meter must not hide outside the
# crop), and on the frame after a crop miss with no live serve (a new shot may appear anywhere).
# Default OFF (classical + test paths byte-identical); the launcher enables it (run_orion.local.ps1).
_LOC_ROI = os.environ.get("ORION_LOC_ROI", "0") != "0"
_LOC_ROI_CROP = max(224, int(os.environ.get("ORION_LOC_ROI_CROP", "512") or "512"))
# RC-2 LOCKED-COAST (2026-07-06): decouple LOCATE from READ. The full-frame YOLO locator costs ~24ms
# on GPU / ~60-110ms on CPU and is the dominant per-frame detect() cost, capping cv_fps well below the
# 60fps feed -> the meter is sampled ~8Hz -> stale/RISE-0.0/rejected. But 60Hz freshness is only needed
# for the FILL (the sub-ms ROI colour read); box LOCATION tolerates ~50ms latency (it drifts slowly
# within a shot). So once a lock is HELD, run the locator only 1 of every _LOC_COAST_EVERY frames and
# deliberately SKIP it between (return None -> the detector's existing loc_mem miss-coast reads the fill
# on the remembered box every frame). The locator re-anchors on ACQUIRE and every _LOC_COAST_EVERY. 1
# disables (locator every frame = pre-RC-2 behaviour). Inert when the locator is absent/disabled.
# DEFAULT OFF (=1): the pivot to the shot-gated reader made this speed lever moot; the mechanism is
# kept opt-in (ORION_LOC_COAST_EVERY>=2) for the legacy locator serving chain only.
_LOC_COAST_EVERY = max(1, int(os.environ.get("ORION_LOC_COAST_EVERY", "1") or "1"))
_LOC_ROI_SWEEP = max(4, int(os.environ.get("ORION_LOC_ROI_SWEEP", "15") or "15"))
_LOC_ROI_SERVE_S = float(os.environ.get("ORION_LOC_ROI_SERVE_S", "0.35") or "0.35")
# T7 (2026-07-05 latency, EXPERIMENTAL — DEFAULT OFF): skip the full-frame park.update() during a
# CONFIRMED locator lock (a live box THIS frame + a valid meter served within _LOC_ROI_SERVE_S or a ready
# box-track, and not spent). On such a frame the locator serves and park's rect is otherwise unused, so
# the pass is redundant. Measured on the 3060 / 20260704_2108 framedump: locked detect() 31.8->22.5ms
# (RECHECK=8) / median 15.9ms (RECHECK=4), and DETECTION rate is preserved (roi_not_found unchanged).
#   CAVEAT (why this is OFF): park.update() is STATEFUL (track EMAs + matchTemplate velocity + static-red
#   cell-EMA). Skipping it gaps that state, so on the next locator-MISS fall-through park emits a slightly
#   different rect, which feeds the MeterBoxKalman -> a deterministic serving CASCADE: ~85 served bboxes
#   shift >4px and ~35 frames flip fed/not-fed across a 530-frame window (incl. the WORKING seq7 shot,
#   -5 fed). No re-check interval removes it (only =1, i.e. no skip, does). So this is NOT the "pure
#   performance / exact serving semantics" change it was meant to be. Also NOTE the original premise was
#   wrong on this HW: park is ~11ms, not 36ms — the LOCATOR (~20ms even on the T6 crop) is the real
#   locked-path cost. Left env-gated for live A/B (where the higher fps may offset the offline fed churn);
#   enable with ORION_PARK_LOCK_SKIP=1. Off = always-on park, byte-for-byte identical (proven).
_PARK_LOCK_SKIP = os.environ.get("ORION_PARK_LOCK_SKIP", "0") != "0"
_PARK_RECHECK = max(1, int(os.environ.get("ORION_PARK_RECHECK", "4") or "4"))
# RED meter BRIGHTNESS floor (the key precision discriminator): the meter fill renders BRIGHT
# (val ~200), while the dark NBA logo / court / shadow reds are val <=130. A val floor isolates the
# bright meter from those WITHOUT needing the green chevron. Calibrated from the live park videos
# (meter val ~207 vs logo ~130 in the 14:59 capture); env-tunable, 0 disables.
_RED_VAL_FLOOR = int(os.environ.get("ORION_RED_VAL_FLOOR", "150") or "150")

# Rising-fill acquisition gate: a NEW low-fill lock only latches if the fill is RISING over the
# warmup. The real meter climbs fast (~3%/frame at 60fps); a static floor/jersey red blob sits
# flat. This lets the low-fill green-anchor be relaxed (ORION_LOWFILL_GREEN=0) for early DURING-rise
# acquisition WITHOUT re-admitting the static low-red false locks. A/B knob; pairs with that toggle.
_RISING_ACQ = os.environ.get("ORION_RISING_ACQ", "0") != "0"

# GREEN-CHEVRON GATE — green-over-red is the meter's signature offline (100% precision), BUT as a
# HARD reject it OVER-REJECTS live: at 720p the ~1px chevron isn't reliably found, so it threw out
# the REAL meter ("detection not working"). DEFAULT OFF. The green-confirm still BOOSTS a candidate's
# confidence when found (below) — it just no longer rejects when absent. ORION_GREEN_GATE=1 to test
# the hard gate once the green detector is reliable at capture scale.
_GREEN_GATE = os.environ.get("ORION_GREEN_GATE", "0") != "0"

# ACQUISITION CORROBORATION (default ON). A NEW lock latches only when corroborated by
# the meter's defining behaviour: a RISING fill (real meter climbs), the green make-window
# above it (green_confirmed), or an unambiguous fast/purity frame. A static red blob (jersey
# number, court logo, sponsor mark, post-shot ~52% deflate) has a flat fill and no chevron,
# so it is never corroborated and never latches — this is the colour-robust discriminator
# that kills the live "locks onto random objects" scatter (detframes 2026-06-28: X-centre
# std 315px across the whole frame) WITHOUT the fragile per-frame green gate. Once TRACKING
# the meter is followed through motion (no re-check), so a real shot is never dropped mid-rise.
# Kill-switch: ORION_ACQ_CORROBORATE=0. Min rise over the acq window to count as "rising":
_ACQ_CORROBORATE = os.environ.get("ORION_ACQ_CORROBORATE", "1") != "0"
_ACQ_RISE_MIN = float(os.environ.get("ORION_ACQ_RISE_MIN", "4.0"))
# WARM RE-ACQUIRE window (2026-07-03 MyCourt): after a corroborated tracking lock is LOST (miss
# streak / track_break / low_conf), a new candidate near the lost position within this many seconds
# inherits corroboration + latches immediately — it's the SAME meter continuing its rise, and making
# it re-prove a rise from scratch left the rest of that rise blind (telemetry: mid-rise dropouts ->
# blind releases). Distractor risk is bounded: the window is short, the radius is ~2 acquisition
# gates, and everything outside it still needs the full rise/green proof. 0 disables.
_WARM_REACQ_S = float(os.environ.get("ORION_WARM_REACQ_S", "1.2"))
# Park direct-emit auto-corroboration. DEFAULT OFF (2026-07-02 live-test fix). A park-zone emit was
# previously treated as self-corroborating, so it latched + FED on its FIRST emit frame — live, a transient
# reveal of a STATIC red graphic (the red-hued Shai wallpaper) satisfied _ParkTracker for ~2 frames, emitted
# a FLAT rect, and latched via this bypass (6 flat fed frames, no rise, no real green cap). With this OFF a
# park emit must prove the SAME rising fill any other ambiguous-red lock does; once _tracking latches, every
# subsequent frame stays valid so a real moving/fading shot is never dropped mid-rise. =1 restores the bypass.
_PARK_AUTO_CORROBORATE = os.environ.get("ORION_PARK_AUTO_CORROBORATE", "0") != "0"
# Meter colours that COLLIDE with common scene content, so colour purity alone can't safely
# acquire a lock — red jerseys / court logos / sponsor stripes / UI are everywhere, which is
# why the live RED detector latched static non-meter red blobs across the whole frame. Only
# these colours require the acquisition corroboration above; RARE colours (Purple/Cyan) are
# discriminating on their own and keep the frame-0 colour-purity acquire the feedforward anchor
# relies on (the off-hue floatie never matches). Override: ORION_AMBIGUOUS_COLOURS="Red,Orange".
_AMBIGUOUS_SCENE_COLOURS = set(
    c.strip() for c in os.environ.get("ORION_AMBIGUOUS_COLOURS", "Red").split(",") if c.strip()
)
# Track T (2026-07-03 MyCourt ship-gate): make the trained v6 locator AUTHORITATIVE so a
# detected-but-rejected moving meter stops starving the engine (live: in-shot detected 81% / fed
# 61%). A YOLO box at or above _LOC_STRONG_CONF is treated as appearance proof — its colour scan
# relaxes the classical size/purity gates, its emitted confidence is not halved, and it corroborates
# a lock on its own. _RECENT_PROX_S lets a candidate near a valid detection moments ago re-latch
# without re-proving a rise (mid-shot the fill is flat/falling). _LOC_BOX_CONF_FLOOR is the relaxed
# internal confidence floor for a box-constrained scan + the box-fallback emit. All are LOCATOR-mode
# only (classical PARK path byte-identical when ORION_METER_LOCATOR is unset).
_LOC_STRONG_CONF = float(os.environ.get("ORION_LOC_STRONG_CONF", "0.55"))
_RECENT_PROX_S = float(os.environ.get("ORION_RECENT_PROX_S", "0.5"))
_LOC_BOX_CONF_FLOOR = float(os.environ.get("ORION_LOC_BOX_CONF_FLOOR", "0.12"))
# T5-b: minimum YOLO confidence for a WEAK (sub-strong) box to reach the box-fallback emit when its
# colour scan misses. Live edge-fade boxes land in the 0.45..0.55 dead-band (runtime conf floor is
# 0.45), and the old `elif loc_strong` gate discarded them outright — a full-shot blackout. Kept
# above genuine junk: a sub-0.40 box (only reachable with a lowered runtime floor) still falls
# through to the park/anchor path instead of fabricating a low-conf emit off a red sliver.
_LOC_WEAK_FB_CONF = float(os.environ.get("ORION_LOC_WEAK_FB_CONF", "0.40"))


@dataclass
class DetectResult:
    detected: bool
    style: str
    color_name: str
    bbox: Tuple[int, int, int, int]
    fill_pct: float
    confidence: float
    consecutive_frames: int
    raw_fill_pct: float = 0.0
    smoothed_fill_pct: float = 0.0
    # Identity of the fill ruler used for ``fill_pct``.  The sidecar carries
    # these fields to native timing so a two-frame anchor crossing is never
    # interpolated across a coarse/sub-pixel (or re-latched) ruler boundary.
    # Zero/empty is the backward-compatible, fail-closed value for producers
    # that do not publish estimator provenance.
    fill_estimator_mode: str = ""
    fill_estimator_generation: int = 0
    velocity: float = 0.0
    accel: float = 0.0
    fill_velocity_pct_s: float = 0.0
    fill_acceleration_pct_s2: float = 0.0
    prediction: float = 0.0
    eta_ms: float = -1.0
    top_pixel_row: int = -1
    release_ready: bool = False
    velocity_stable: bool = False
    aborted: bool = False
    outline_contour: Optional[np.ndarray] = None
    green_cluster_px: int = 0
    roi_locked: bool = False
    fill_pixels: int = 0
    total_fill_pixels: int = 0
    fill_velocity_px_s: float = 0.0
    fill_accel_px_s2: float = 0.0
    green_window_start_pct: float = -1.0
    green_window_end_pct: float = -1.0
    green_window_center_pct: float = -1.0
    green_window_width_pct: float = 0.0
    green_window_confidence: float = 0.0
    green_window_start_row: int = -1
    green_window_end_row: int = -1
    green_window_center_row: int = -1
    eta_to_green_center_ms: float = -1.0
    eta_to_top_pixel_ms: float = -1.0
    meter_roi_locked: bool = False
    meter_tracking_jitter: float = 0.0
    # Specific reason a frame was not a usable timing sample (empty string when
    # accepted). One of: roi_not_found, low_confidence, bbox_unstable,
    # green_not_found, meter_memory, stale_frame, "" (accepted).
    rejection_reason: str = ""
    # A1 rise phase of the served meter: "" | "rising" | "peak" | "spent". "spent" = the previous
    # shot's meter lingering on screen (full/deflating) — the trigger for carryover preemption.
    rise_state: str = ""
    # Production SimpleMeterReader-only provenance. Legacy/classical detectors never set it;
    # automatic cold calibration therefore fails closed on those paths.
    gameplay_structure_verified: bool = False
    # Exact controller-origin physical-shot epoch for that proof. Zero means an
    # untokenized/local arm and is never automatic-calibration authority.
    gameplay_structure_epoch: int = 0
    # Diagnostic frame-start identity, including samples that have not earned proof.
    # This field never grants timing/ownership authority.
    gameplay_sample_epoch: int = 0


@dataclass
class DetectorConfig:
    left_zone_start_pct: float = 5.0
    left_zone_end_pct: float = 50.0
    right_zone_start_pct: float = 50.0
    right_zone_end_pct: float = 95.0
    confidence_threshold: float = 0.35
    aspect_ratio_tolerance: float = 0.40
    # Contour geometry gate (shape-based noise rejection). DEFAULT OFF: video measurement
    # (2026-06-03 real Purple Arrow2, tools/diagnostics) showed our TALL meter's circularity
    # spans ~0.07-0.49 (min 0.071, p10 0.121) and OVERLAPS the floatie/noise range, so any floor
    # high enough to reject noise also rejects thin/early real meters -> net regression. The
    # geometry-filter win was specific to a WIDE arrow shape; for our tall meter the
    # real discriminator is the colour PURITY gate + structure scores (applied below). Kept as a
    # tunable knob (rejection_filters.geometry_gate_enabled / min_circularity) for other styles.
    geometry_gate_enabled: bool = True
    min_circularity: float = 0.05
    position_jump_max_px: float = 80.0
    # Once a shot is ACQUIRED (min_consecutive clean frames), the meter is allowed
    # to translate by up to position_jump_max_px * tracking_jump_scale per frame
    # before we treat it as a teleport and re-acquire. A fade slides the meter
    # across the court; following it is correct, and rejecting that motion as
    # "bbox_unstable" was starving the timing engine on every fade (fill froze ->
    # timeout_fallback). The acquisition gate stays tight to block idle blobs.
    tracking_jump_scale: float = 3.0
    # position_jump_max_px was tuned on a ~720px-wide capture; scale it up with the
    # real frame width so the same proportional jitter isn't over-rejected at the
    # full-res 1280-1550px capture (an absolute 80px gate is ~11% of 720 but only
    # ~5% of 1550, which tripped constantly after the full-res capture change).
    jump_gate_ref_width: int = 720
    ui_exclusion_zones: List[Dict[str, Any]] = field(default_factory=list)
    min_consecutive_valid_frames: int = 1
    # Fast-acquire: a single UNAMBIGUOUS meter frame latches tracking immediately
    # instead of warming up over min_consecutive_valid_frames. An Arrow2 meter rises
    # 0->~95% in ~2-3 frames; the 3-frame warmup suppressed that ENTIRE rise as
    # bbox_unstable (measured: 79 rise frames, conf p10 0.85 / median 0.97, ar~4.6,
    # thrown away), so the engine's first fresh sample landed at the peak and its
    # feedforward anchor was ~2 frames late. A frame is "unambiguous" only when its
    # confidence and aspect are well clear of the floatie/transient regime (genuine
    # non-detections cap at conf 0.82; the pink floatie is wide, ar<1), so this does
    # NOT loosen detection — it skips the warmup ONLY for frames already proven meter.
    fast_acquire_conf: float = 0.85
    fast_acquire_min_ar: float = 1.5
    # Colour-purity fast-acquire (first-shot meter-appearance latch). The generic
    # fast-acquire above needs conf>=0.85 AND ar>=1.5, but a GENUINE low-fill
    # early-rise meter — the very meter-appearance the engine's feedforward anchor
    # must lock onto — measures conf~0.77 / ar~1.37 (tests/fixtures real Purple
    # low-fill; live 2026-05-21 recording: the first 2 frames of every shot run
    # emitted bbox_unstable, so the appearance was lost to the 3-frame warmup and
    # the engine fell back to the less-accurate hold-start clock -> scattered timing
    # / presence=no_sample_ever on early shots). A frame whose masked-median HSV is
    # DEAD-ON the real meter colour (hue within ±tol of the meter band centre AND
    # sat>=floor) is unambiguous BY COLOUR even at low fill, so it may latch
    # immediately. This is the SAME purity signature that already purity-rejects the
    # pink floatie to found=False before stability ever runs (floatie Hue~166/Sat~142
    # never produces a match), so it admits ZERO new false positives. Purple uses the
    # live-proven purple_purity_* band; other colours use their _COLOR_PURITY_GATES
    # centre. purity_fast_acquire_enabled doubles as the master switch.
    purity_fast_acquire_enabled: bool = True
    # Max |median hue - meter band centre| (OpenCV H, 0-179) for a purity latch.
    # Calibrated to the LIVE moving meter, not the clean static crop: on the real
    # capture the early-rise meter's masked-median hue spans ~137-159 (motion blur +
    # stream compression pull it off the clean 150), so a tight ±6 never fired live.
    # ±14 around centre 148 covers 134-162 — the measured live meter band — while the
    # pink floatie (Hue~166) stays out with margin. This band sits INSIDE what the
    # purple purity gate already admits (Hue<=159), so it never widens detection.
    purity_fast_acquire_hue_tol: float = 14.0
    # Min masked-median saturation for a purity latch. The live meter reads Sat
    # ~168-197 during early rise (vs 200+ on the clean crop), so the floor is set to
    # the purple_purity_sat_min (165) the candidate already had to clear to be matched
    # at all — i.e. any candidate reaching this check already satisfies it. The
    # saturated-HUD requirement is what the floatie (Sat~142, already purity-rejected
    # to no-match) and washed-out cosmetics fail.
    purity_fast_acquire_sat_min: float = 165.0
    max_freeze_frames: int = 10
    # Confidence HYSTERESIS (Track T, locator mode only). While TRACKING, a single sub-threshold
    # confidence dip is motion / early-rise noise (live: conf min 0.245 / median 0.358 mid-rise),
    # not a lost meter. Hold the lock for up to this many consecutive positionally-consistent
    # sub-threshold frames before dropping it — mirrors max_freeze_frames for the miss path. 0
    # restores the old drop-on-first-dip behaviour. Inert on the classical path (locator OFF).
    low_conf_grace_frames: int = 8
    confidence_drop_abort_threshold: float = 0.25
    max_position_delta_per_frame_px: float = 50.0
    velocity_rolling_window: int = 5
    velocity_outlier_sigma: float = 2.0
    velocity_max_pct_per_sec: float = 500.0
    velocity_min_pct_per_sec: float = 0.5
    green_window_start_pct: float = 93.0
    green_window_end_pct: float = 100.0
    green_cluster_min_px: int = 3
    total_latency_ms: float = 45.0
    min_stable_frames_before_release: int = 3
    velocity_stability_tolerance: float = 0.20
    meter_color: str = "Purple"
    meter_hsv_low: Optional[List[int]] = None
    meter_hsv_high: Optional[List[int]] = None
    trained_green_hsv_low: Optional[List[int]] = None
    trained_green_hsv_high: Optional[List[int]] = None
    micro_roi_size_px: int = 72
    micro_roi_padding_px: int = 18
    cuda_enabled: bool = True
    tip_target_mode: bool = False
    meter_memory_frames: int = 8
    green_memory_frames: int = 3
    min_outline_score: float = 0.08
    min_meter_density: float = 0.16
    max_meter_density: float = 1.0
    min_vertical_coverage: float = 0.45
    # Purple/Arrow2 meter colour-purity gate. The real Arrow2 Purple shot meter is
    # a saturated true-magenta HUD overlay (median Hue ~150, Sat ~200+), stable
    # across court lighting because it is a UI element. The cosmetic PINK FLOATIE
    # and player pink read pinker / washed-out (Hue ~166, Sat ~142) and were being
    # false-accepted and fed to the timing engine as bogus fill. Reject a Purple
    # candidate whose masked pixels are clearly off the meter's magenta — too-high
    # hue OR too-low saturation. Conservative margins (meter Hue<=151 / Sat>=184)
    # so compression/lighting shifts don't break detection. Purple-scoped only;
    # other selected colours are unaffected. green-cap remains a SOFT booster, not
    # a requirement.
    # NOTE (2026-06-24): the real meter's masked-MEDIAN Sat (the statistic this gate
    # tests) measures 203-206 across low/mid/full fill (tests/fixtures/meter), matching
    # a known-good Arrow2 gate (BGR G<=60 => Sat>=~195), so this 165 floor has
    # ~38pt of headroom. In the live Purple-locked path a bump to 185 is safe (positives
    # clear it by ~18pt; negatives reject on hue), but no fixture exercises a borderline
    # 165-185 magenta-noise blob, so the BENEFIT is RE-supported not fixture-proven —
    # staged as a post-batch detection A/B, not shipped blind. See
    # nexusvision-detector-floatie-purity.
    purple_purity_enabled: bool = True
    purple_purity_hue_max: float = 159.0
    purple_purity_sat_min: float = 165.0
    purple_purity_min_px: int = 24
    # Per-colour masked-median purity gates for the NON-Purple meter colours
    # (same mechanism as the Purple floatie fix above): a real meter fill is a
    # hue-tight saturated HUD overlay; jerseys / court paint / cosmetics drift in
    # hue or wash out in saturation. White is the inverse — a true White meter is
    # LOW-sat HIGH-val, so a tinted blob (sat too high) or grey blob (val too
    # low) is rejected. Purple keeps its dedicated live-proven knobs; this table
    # is consulted only for the other colours. purple_purity_enabled doubles as
    # the master switch so one settings knob disables all colour purity gates.
    # Near-top green sanity floor (style-agnostic geometry). The green CHEVRON sits at the very
    # TOP of the full track (~93-100%). green_span_abs can latch a fill-edge / HUD cluster mid-
    # track and publish a bogus low/wide "green" (live 2026-06-07: center wandered 27-56% on a
    # FULL meter). Fed to the engine's green tracker that set the release target mid-track and
    # fired the shot far too early (EARLY-railed fades). Reject a green whose CENTER maps below
    # green_center_floor_frac of the track, or whose band spans more than green_max_height_frac
    # of it (a chevron is narrow, not track-spanning). Conservative so a real ~93-100% chevron
    # always passes; fill % is never touched. Rejected -> green_not_found -> engine times the tip.
    green_center_floor_frac: float = 0.60
    green_max_height_frac: float = 0.50
    # Trained TEMPLATE ANCHOR (template-matched meter LOCATION). The HSV colour
    # scan alone cannot reliably LOCATE the meter — a saturated red/orange UI blob
    # passes the one-sided Purple purity gate and the stability latch holds it (the
    # live 2026-06-22 false-lock: fill frozen 41.18, bbox stuck (1018,340), med_h=11).
    # A user-trained template of a STABLE, shot-type-invariant HUD glyph is matched
    # each frame (cv2.matchTemplate); the meter sits at a fixed pixel offset from it,
    # so the colour scan is constrained to the true meter region and the blob is never
    # considered. LOCATION only — fill % + green are still read by the HSV/track
    # pipeline inside the located region. When no anchor is trained (or it isn't found
    # this frame) the detector behaves exactly as before (additive, never a regression).
    template_anchor_enabled: bool = False
    template_anchor_path: Optional[str] = None
    # Search band as frame fractions [x0,y0,x1,y1] (0..1) the glyph is matched within
    # (cheaper + fewer false peaks than a whole-frame scan). None = full frame.
    template_anchor_search_band: Optional[List[float]] = None
    # Meter region relative to the matched anchor's top-left, in pixels at train res:
    # [dx,dy,w,h]. Scaled by ref_wh if the live resolution differs.
    template_anchor_offset: Optional[List[int]] = None
    template_anchor_min_score: float = 0.62
    # Frame [W,H] at train time so the offset/band/template scale to a different live
    # resolution. None = assume live res == train res.
    template_anchor_ref_wh: Optional[List[int]] = None
    # Auto meter-COLOR detection ("set_style AUTO" / "set_learned_color").
    # When on, the detector scans candidate colours each frame until one consistently
    # yields a confident meter, then LOCKS that colour for the session (re-scans only
    # after a sustained detection failure). Removes the silent wrong-meter_color
    # failure mode ([[nexusvision-meter-color-red-misconfig]]). Default off (use the
    # configured meter_color). Candidates default to all BGR_COLOR_RANGES keys.
    auto_meter_color: bool = False
    auto_meter_color_candidates: Optional[List[str]] = None
    # PARK (rec-mode) temporal meter locator. The park is full of bright-red distractors
    # (fire/pyro, red jerseys, fixed HUD) that the colour scan + stability latch false-lock
    # on. When enabled, a distractor-rejecting tracker (_ParkTracker) confirms the REAL meter
    # by its temporal signature — a thin pure-red column in the play band rising monotonically
    # from a FIXED bottom — and CONSTRAINS the colour scan to that region (authoritative: a
    # frame with no confirmed park meter yields no detection, so distractors never leak in).
    # Proven 100%/100% standalone (scratchpad/park_temporal_detect.py). Fill %, green + timing
    # are still read by the existing _measure_track/stability pipeline (additive). Default
    # auto-enabled for the Red Arrow2 park meter in load_detector_config.
    park_temporal_enabled: bool = False


def load_detector_config(settings_path: Optional[str] = None) -> DetectorConfig:
    cfg = DetectorConfig()
    if settings_path is None:
        base = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
        desk = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop", "NexusVision")
        for d in (base, desk):
            p = os.path.join(d, "settings.json")
            if os.path.isfile(p):
                settings_path = p
                break
    if settings_path is None or not os.path.isfile(settings_path):
        return cfg

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return cfg

    sr = raw.get("search_region", {})
    if isinstance(sr, dict):
        cfg.left_zone_start_pct = float(sr.get("left_zone_start_pct", cfg.left_zone_start_pct))
        cfg.left_zone_end_pct = float(sr.get("left_zone_end_pct", cfg.left_zone_end_pct))
        cfg.right_zone_start_pct = float(sr.get("right_zone_start_pct", cfg.right_zone_start_pct))
        cfg.right_zone_end_pct = float(sr.get("right_zone_end_pct", cfg.right_zone_end_pct))

    # Legacy settings key: this detector is HSV/contour based, not template matching.
    # RECONCILED with the launcher gate below: both now live in the SAME bounded band
    # [0.10, 0.35]. Previously this path took the raw template value UNCLAMPED (a
    # settings.json 0.45 sailed straight through), while the launcher path clamped to
    # [0.10, 0.35] — so the two knobs could disagree by ~0.28 and, worse, the park
    # locator's derived confidence caps at 0.98 but floors at ~0.20, so a 0.45 gate
    # silently rejected genuine mid-rise emits (conf min ~0.245). Clamping here makes
    # the two gates coherent; the launcher key (if present) still wins as the last write.
    tm = raw.get("template_matching", {})
    if isinstance(tm, dict) and "confidence_threshold" in tm:
        try:
            cfg.confidence_threshold = max(0.10, min(0.35, float(tm.get("confidence_threshold"))))
        except Exception:
            pass
    # Flat launcher value (0-100) override for easier tuning.
    if "detection_confidence_percent" in raw:
        try:
            pct = float(raw.get("detection_confidence_percent", 0.0))
            pct = max(1.0, min(100.0, pct))
            cfg.confidence_threshold = max(0.10, min(0.35, pct / 300.0))
        except Exception:
            pass

    rf = raw.get("rejection_filters", {})
    if isinstance(rf, dict):
        cfg.aspect_ratio_tolerance = float(rf.get("aspect_ratio_tolerance", cfg.aspect_ratio_tolerance))
        cfg.min_circularity = float(rf.get("min_circularity", cfg.min_circularity))
        cfg.geometry_gate_enabled = bool(rf.get("geometry_gate_enabled", cfg.geometry_gate_enabled))
        cfg.position_jump_max_px = float(rf.get("position_jump_max_px", cfg.position_jump_max_px))
        cfg.tracking_jump_scale = float(rf.get("tracking_jump_scale", cfg.tracking_jump_scale))
        cfg.jump_gate_ref_width = int(rf.get("jump_gate_ref_width", cfg.jump_gate_ref_width))
        uiz = rf.get("ui_exclusion_zones")
        if isinstance(uiz, list):
            cfg.ui_exclusion_zones = uiz

    stab = raw.get("stability", {})
    if isinstance(stab, dict):
        cfg.min_consecutive_valid_frames = int(stab.get("min_consecutive_valid_frames", cfg.min_consecutive_valid_frames))
        cfg.fast_acquire_conf = float(stab.get("fast_acquire_conf", cfg.fast_acquire_conf))
        cfg.fast_acquire_min_ar = float(stab.get("fast_acquire_min_ar", cfg.fast_acquire_min_ar))
        cfg.purity_fast_acquire_enabled = bool(stab.get("purity_fast_acquire_enabled", cfg.purity_fast_acquire_enabled))
        cfg.purity_fast_acquire_hue_tol = float(stab.get("purity_fast_acquire_hue_tol", cfg.purity_fast_acquire_hue_tol))
        cfg.purity_fast_acquire_sat_min = float(stab.get("purity_fast_acquire_sat_min", cfg.purity_fast_acquire_sat_min))
        cfg.max_freeze_frames = int(stab.get("max_freeze_frames", cfg.max_freeze_frames))
        cfg.low_conf_grace_frames = int(stab.get("low_conf_grace_frames", cfg.low_conf_grace_frames))
        cfg.confidence_drop_abort_threshold = float(stab.get("confidence_drop_abort_threshold", cfg.confidence_drop_abort_threshold))
        cfg.max_position_delta_per_frame_px = float(stab.get("max_position_delta_per_frame_px", cfg.max_position_delta_per_frame_px))

    vel = raw.get("velocity", {})
    if isinstance(vel, dict):
        cfg.velocity_rolling_window = int(vel.get("rolling_window_frames", cfg.velocity_rolling_window))
        cfg.velocity_outlier_sigma = float(vel.get("outlier_rejection_sigma", cfg.velocity_outlier_sigma))
        cfg.velocity_max_pct_per_sec = float(vel.get("max_velocity_pct_per_sec", cfg.velocity_max_pct_per_sec))
        cfg.velocity_min_pct_per_sec = float(vel.get("min_velocity_pct_per_sec", cfg.velocity_min_pct_per_sec))

    pred = raw.get("prediction", {})
    if isinstance(pred, dict):
        cfg.green_window_start_pct = float(pred.get("green_window_start_pct", cfg.green_window_start_pct))
        cfg.green_window_end_pct = float(pred.get("green_window_end_pct", cfg.green_window_end_pct))

    lat = raw.get("latency", {})
    if isinstance(lat, dict):
        cfg.total_latency_ms = float(lat.get("total_latency_ms", cfg.total_latency_ms))
    if "latency_compensation_ms" in raw:
        try:
            cfg.total_latency_ms = max(0.0, float(raw.get("latency_compensation_ms", cfg.total_latency_ms)))
        except Exception:
            pass

    rel = raw.get("release", {})
    if isinstance(rel, dict):
        cfg.min_stable_frames_before_release = int(rel.get("min_stable_frames_before_release", cfg.min_stable_frames_before_release))
        cfg.velocity_stability_tolerance = float(rel.get("velocity_stability_tolerance", cfg.velocity_stability_tolerance))

    cfg.meter_color = str(raw.get("meter_color", cfg.meter_color))
    if "micro_roi_size_px" in raw:
        try:
            cfg.micro_roi_size_px = int(max(32, min(180, int(raw.get("micro_roi_size_px", cfg.micro_roi_size_px)))))
        except Exception:
            pass
    if "micro_roi_padding_px" in raw:
        try:
            cfg.micro_roi_padding_px = int(max(4, min(80, int(raw.get("micro_roi_padding_px", cfg.micro_roi_padding_px)))))
        except Exception:
            pass
    cuda_raw = raw.get("cuda_enabled", raw.get("use_cuda", None))
    cuda_cfg = raw.get("cuda")
    if isinstance(cuda_cfg, dict):
        cuda_raw = cuda_cfg.get("enabled", cuda_raw)
    if cuda_raw is not None:
        if isinstance(cuda_raw, str):
            cfg.cuda_enabled = cuda_raw.strip().lower() in ("1", "true", "yes", "on", "auto")
        else:
            cfg.cuda_enabled = bool(cuda_raw)
    roi_cfg = raw.get("roi") if isinstance(raw.get("roi"), dict) else {}
    if isinstance(roi_cfg, dict):
        try:
            cfg.micro_roi_size_px = int(max(32, min(180, int(roi_cfg.get("micro_roi_size_px", cfg.micro_roi_size_px)))))
            cfg.micro_roi_padding_px = int(max(4, min(80, int(roi_cfg.get("micro_roi_padding_px", cfg.micro_roi_padding_px)))))
        except Exception:
            pass
    if "release_threshold" in raw:
        try:
            cfg.green_window_start_pct = max(50.0, min(100.0, float(raw.get("release_threshold", cfg.green_window_start_pct))))
        except Exception:
            pass

    detector_raw = raw.get("meter_detector") if isinstance(raw.get("meter_detector"), dict) else {}
    for key, caster, lo, hi in (
        ("meter_memory_frames", int, 0, 12),
        ("green_memory_frames", int, 0, 12),
        ("min_outline_score", float, 0.0, 1.0),
        ("min_meter_density", float, 0.02, 0.95),
        ("max_meter_density", float, 0.05, 1.0),
        ("min_vertical_coverage", float, 0.05, 1.0),
    ):
        raw_value = raw.get(key, detector_raw.get(key))
        if raw_value is None:
            continue
        try:
            setattr(cfg, key, max(lo, min(hi, caster(raw_value))))
        except Exception:
            pass
    if cfg.max_meter_density < cfg.min_meter_density:
        cfg.max_meter_density = cfg.min_meter_density

    custom_hsv_enabled = bool(raw.get("custom_hsv_enabled", True))
    if custom_hsv_enabled:
        custom_lo = raw.get("meter_hsv_low")
        custom_hi = raw.get("meter_hsv_high")
        if not (isinstance(custom_lo, list) and isinstance(custom_hi, list)):
            scalar_keys = (
                "meter_hsv_low_h", "meter_hsv_low_s", "meter_hsv_low_v",
                "meter_hsv_high_h", "meter_hsv_high_s", "meter_hsv_high_v",
            )
            if all(k in raw for k in scalar_keys):
                custom_lo = [raw.get("meter_hsv_low_h"), raw.get("meter_hsv_low_s"), raw.get("meter_hsv_low_v")]
                custom_hi = [raw.get("meter_hsv_high_h"), raw.get("meter_hsv_high_s"), raw.get("meter_hsv_high_v")]
        if (
            isinstance(custom_lo, list) and isinstance(custom_hi, list)
            and len(custom_lo) == 3 and len(custom_hi) == 3
        ):
            try:
                lo_v = [int(max(0, min(255, int(x)))) for x in custom_lo]
                hi_v = [int(max(0, min(255, int(x)))) for x in custom_hi]
                lo_v[0] = int(max(0, min(179, lo_v[0])))
                hi_v[0] = int(max(0, min(179, hi_v[0])))
                if hi_v[0] >= lo_v[0] and hi_v[1] >= lo_v[1] and hi_v[2] >= lo_v[2]:
                    cfg.meter_hsv_low = lo_v
                    cfg.meter_hsv_high = hi_v
            except Exception:
                pass

    _gw_target = raw.get("green_window_target_mode", raw.get("autogreen_target", ""))
    if isinstance(_gw_target, str) and _gw_target.strip().lower() == "tip":
        cfg.tip_target_mode = True

    lo = raw.get("meter_trained_green_hsv_low")
    hi = raw.get("meter_trained_green_hsv_high")
    if (
        isinstance(lo, list) and isinstance(hi, list)
        and len(lo) == 3 and len(hi) == 3
    ):
        try:
            lo_v = [int(max(0, min(255, int(x)))) for x in lo]
            hi_v = [int(max(0, min(255, int(x)))) for x in hi]
            if hi_v[0] >= lo_v[0] and hi_v[1] >= lo_v[1] and hi_v[2] >= lo_v[2]:
                cfg.trained_green_hsv_low = lo_v
                cfg.trained_green_hsv_high = hi_v
        except Exception:
            pass

    # Trained template anchor (meter LOCATION). Written by the in-launcher Train
    # button; consumed here. Binary template lives on disk (path referenced), never
    # in settings.json.
    ta = raw.get("meter_template_anchor")
    if isinstance(ta, dict):
        cfg.template_anchor_enabled = bool(ta.get("enabled", False))
        tp = ta.get("template_path")
        if isinstance(tp, str) and tp.strip():
            cfg.template_anchor_path = tp.strip()
        band = ta.get("search_band")
        if isinstance(band, dict):
            try:
                cfg.template_anchor_search_band = [
                    float(band.get("x0", 0.0)), float(band.get("y0", 0.0)),
                    float(band.get("x1", 1.0)), float(band.get("y1", 1.0)),
                ]
            except Exception:
                pass
        elif isinstance(band, (list, tuple)) and len(band) == 4:
            try:
                cfg.template_anchor_search_band = [float(b) for b in band]
            except Exception:
                pass
        off = ta.get("meter_offset")
        if isinstance(off, dict):
            try:
                cfg.template_anchor_offset = [
                    int(off.get("dx", 0)), int(off.get("dy", 0)),
                    int(off.get("w", 0)), int(off.get("h", 0)),
                ]
            except Exception:
                pass
        elif isinstance(off, (list, tuple)) and len(off) == 4:
            try:
                cfg.template_anchor_offset = [int(v) for v in off]
            except Exception:
                pass
        try:
            cfg.template_anchor_min_score = float(ta.get("match_confidence_min", cfg.template_anchor_min_score))
        except Exception:
            pass
        ref = ta.get("ref_wh")
        if isinstance(ref, (list, tuple)) and len(ref) == 2:
            try:
                cfg.template_anchor_ref_wh = [int(ref[0]), int(ref[1])]
            except Exception:
                pass

    cfg.auto_meter_color = bool(raw.get("auto_meter_color", cfg.auto_meter_color))
    amc = raw.get("auto_meter_color_candidates")
    if isinstance(amc, list) and amc:
        cands = [str(c) for c in amc if isinstance(c, str) and str(c)]
        if cands:
            cfg.auto_meter_color_candidates = cands
    # PARK temporal locator: explicit opt-in (default OFF). It is AUTHORITATIVE and needs a
    # multi-frame rise to confirm, so it must NOT change the default single-frame detection
    # contract that the unit tests + non-park modes rely on. The LIVE park path enables it on
    # the orchestrator-built detector config (remote_play_orchestrator) instead of riding along
    # with every settings.json load. An explicit "park_temporal_enabled" settings key still works.
    cfg.park_temporal_enabled = bool(raw.get("park_temporal_enabled", cfg.park_temporal_enabled))
    return cfg


BGR_COLOR_RANGES: Dict[str, Tuple[List[int], List[int]]] = {
    "Purple": ([170, 0, 165], [255, 85, 255]),
    "White": ([235, 235, 235], [255, 255, 255]),
    "Orange": ([0, 80, 190], [55, 170, 255]),
    "Yellow": ([0, 170, 145], [70, 255, 255]),
    "Red": ([0, 0, 170], [70, 70, 255]),
    "Green": ([0, 180, 0], [70, 255, 70]),
    "Cyan": ([170, 110, 0], [255, 230, 80]),
}

_HSV_FILL_RANGES: Dict[str, Tuple[List[int], List[int]]] = {
    "Purple": ([140, 150, 150], [158, 255, 255]),
    "White": ([0, 0, 225], [179, 45, 255]),
    "Orange": ([8, 150, 150], [22, 255, 255]),
    "Yellow": ([25, 150, 150], [38, 255, 255]),
    "Red": ([0, 140, 140], [10, 255, 255]),
    "Green": ([50, 140, 130], [68, 255, 255]),
    "Cyan": ([90, 130, 140], [108, 255, 255]),
}

# Masked-median purity gates for non-Purple meter colours (Purple uses the
# dedicated purple_purity_* knobs). Keys: hue_min/hue_max (OpenCV H 0-179;
# hue_min < 0 means the band wraps through 0, e.g. Red), sat_min/sat_max,
# val_min (0-255). A candidate whose masked-pixel medians fall outside its
# colour's band is a jersey/cosmetic blob, not the HUD meter — reject it
# before it can feed bogus fill to the timing engine.
_COLOR_PURITY_GATES: Dict[str, Dict[str, float]] = {
    "Yellow": {"hue_min": 20.0, "hue_max": 42.0, "sat_min": 140.0},
    "Orange": {"hue_min": 5.0, "hue_max": 25.0, "sat_min": 140.0},
    "Red": {"hue_min": -12.0, "hue_max": 14.0, "sat_min": 130.0},
    "Green": {"hue_min": 45.0, "hue_max": 72.0, "sat_min": 130.0},
    "Cyan": {"hue_min": 86.0, "hue_max": 112.0, "sat_min": 120.0},
    # A true White meter is desaturated and bright; tinted (sat high) or grey
    # (val low) blobs — nets, jersey whites in shadow — are not the meter.
    "White": {"sat_max": 70.0, "val_min": 205.0},
}


def _purity_gate_rejects(meter_color: str, med_h: float, med_s: float, med_v: float) -> bool:
    """True when the masked-median HSV of a candidate violates its colour's
    purity band (see _COLOR_PURITY_GATES). Unknown colours never reject."""
    gate = _COLOR_PURITY_GATES.get(meter_color)
    if gate is None:
        return False
    hue_min = gate.get("hue_min")
    hue_max = gate.get("hue_max")
    if hue_min is not None and hue_max is not None:
        if hue_min < 0.0:
            # Band wraps through 0 (Red): accept H <= hue_max or H >= 180 + hue_min.
            if not (med_h <= hue_max or med_h >= 180.0 + hue_min):
                return True
        elif not (hue_min <= med_h <= hue_max):
            return True
    if "sat_min" in gate and med_s < gate["sat_min"]:
        return True
    if "sat_max" in gate and med_s > gate["sat_max"]:
        return True
    if "val_min" in gate and med_v < gate["val_min"]:
        return True
    return False


# Per-colour hue-band CENTRE (OpenCV H, 0-179) used by the colour-purity
# fast-acquire latch. The real meter's masked-median hue clusters tightly at the
# band centre; a frame whose median sits within ±tol of it AND is saturated is
# unambiguously the meter (the floatie/jersey blobs that drift in hue are already
# rejected by the purity gate before stability runs). Purple's centre is the
# live-measured ~150 (tests/fixtures: med_h 149-150 across all fills); the other
# colours take the midpoint of their _COLOR_PURITY_GATES band.
_METER_HUE_CENTER: Dict[str, float] = {
    # Purple centred at 148 (live moving-meter median spans ~137-159; the clean
    # static crop reads ~149-150). With hue_tol 14 this covers the measured live
    # band and excludes the pink floatie (Hue~166).
    "Purple": 148.0,
    "Yellow": 31.0,
    "Orange": 15.0,
    "Red": 0.0,      # band wraps 0; |hue-0| handled circularly below
    "Green": 58.0,
    "Cyan": 99.0,
}


def _purity_fast_acquire_ok(cfg: "DetectorConfig", meter_color: str,
                            med_h: float, med_s: float) -> bool:
    """True when a candidate's masked-median HSV is DEAD-ON the meter colour, so a
    genuine low-fill early-rise meter can latch immediately (colour-unambiguous)
    even when its confidence/aspect fall short of the generic fast-acquire gate.

    Conservative: requires a measured median (>=0), a saturation at/above the floor,
    and a hue within ±tol of the colour's band centre. White has no stable hue, so
    it is excluded (it relies on the generic conf/aspect fast-acquire). Returns
    False on any malformed input so it can never widen acquisition unexpectedly."""
    if not getattr(cfg, "purity_fast_acquire_enabled", False):
        return False
    if med_h < 0.0 or med_s < 0.0:
        return False
    if meter_color == "White":
        return False
    center = _METER_HUE_CENTER.get(meter_color)
    if center is None:
        return False
    sat_min = float(getattr(cfg, "purity_fast_acquire_sat_min", 185.0))
    if med_s < sat_min:
        return False
    tol = float(getattr(cfg, "purity_fast_acquire_hue_tol", 6.0))
    # Circular hue distance (handles Red's wrap through 0/179).
    d = abs(float(med_h) - float(center))
    d = min(d, 180.0 - d)
    return d <= tol


_STYLE_GEOM_PROFILES: Dict[str, Dict[str, float]] = {
    # ar_max widened to fit each meter's per-style geometry: a FULL thin meter has a
    # very high height/width ratio (Pill ~27, Straight ~108, Sword ~10) and was
    # being rejected at high fill by the old ceilings. Widening only admits more
    # candidates; density/coverage/outline gates still reject false positives.
    "PILL": {"ar_min": 1.35, "ar_max": 28.0, "row_min": 0.52, "col_min": 0.34, "density_scale": 0.92, "outline_scale": 1.00, "bias": 0.020},
    "STRAIGHT": {"ar_min": 1.60, "ar_max": 110.0, "row_min": 0.58, "col_min": 0.32, "density_scale": 0.95, "outline_scale": 1.05, "bias": 0.018},
    "SWORD": {"ar_min": 1.15, "ar_max": 11.0, "row_min": 0.46, "col_min": 0.28, "density_scale": 0.82, "outline_scale": 0.90, "bias": 0.015},
    "DIAL": {"ar_min": 0.55, "ar_max": 5.0, "row_min": 0.30, "col_min": 0.30, "density_scale": 0.70, "outline_scale": 0.82, "bias": 0.010},
    "ARROW": {"ar_min": 0.85, "ar_max": 8.2, "row_min": 0.40, "col_min": 0.28, "density_scale": 0.78, "outline_scale": 0.86, "bias": 0.015},
    "ARROW2": {"ar_min": 0.85, "ar_max": 8.2, "row_min": 0.40, "col_min": 0.28, "density_scale": 0.78, "outline_scale": 0.86, "bias": 0.018},
}


def _norm_style_name(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").replace("-", "").upper()


def _style_geom_profile(style_name: Any) -> Dict[str, float]:
    return _STYLE_GEOM_PROFILES.get(_norm_style_name(style_name), {
        "ar_min": 0.85,
        "ar_max": 9.5,
        "row_min": 0.35,
        "col_min": 0.25,
        "density_scale": 0.72,
        "outline_scale": 0.78,
        "bias": -0.015,
    })


def _builtin_style(style_name: str) -> Dict[str, Any]:
    return {
        "style": style_name,
        "search": {"left": 5, "top": 216, "right": 1915, "bottom": 920},
        # Orion: w_min=2 (the Arrow2 meter is 2px wide at 1920x1080 — the old
        # w_min=8 rejected 82% of real detections), h_min=20 (low-fill early-rise
        # meters can be as short as 20px). Horizontal dilation in _hsv_mask
        # connects the 2px bar into a 4-6px contour for robust detection.
        "contour": {"w_min": 2, "w_max": 40, "h_min": 20, "h_max": 240},
        "colors": {
            color: {"low": lo, "high": hi}
            for color, (lo, hi) in BGR_COLOR_RANGES.items()
        },
    }


def _style_bgr_bounds_to_hsv(lo_bgr: np.ndarray, hi_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Convert a meter_styles BGR color box into the HSV range used by cv2.inRange."""
    lo = np.array(lo_bgr, dtype=np.uint8).reshape(3)
    hi = np.array(hi_bgr, dtype=np.uint8).reshape(3)
    b_vals = (int(lo[0]), int(hi[0]))
    g_vals = (int(lo[1]), int(hi[1]))
    r_vals = (int(lo[2]), int(hi[2]))
    samples = []
    for b in b_vals:
        for g in g_vals:
            for r in r_vals:
                samples.append([b, g, r])
    hsv = cv2.cvtColor(np.array(samples, dtype=np.uint8).reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    h = hsv[:, 0].astype(np.int16)
    s = hsv[:, 1].astype(np.int16)
    v = hsv[:, 2].astype(np.int16)
    hsv_lo = np.array([int(max(0, h.min() - 2)), int(max(0, s.min() - 12)), int(max(0, v.min() - 12))], dtype=np.uint8)
    hsv_hi = np.array([int(min(179, h.max() + 2)), int(min(255, s.max() + 12)), int(min(255, v.max() + 12))], dtype=np.uint8)
    return hsv_lo, hsv_hi


class _CudaMaskBackend:
    """Optional OpenCV-CUDA mask builder.

    Standard pip OpenCV wheels expose cv2.cuda partially but are usually built
    without CUDA image operations. This class treats CUDA as opportunistic:
    use it when the runtime has the required ops, otherwise fall back to CPU.
    """

    _REQUIRED_OPS = ("GpuMat", "cvtColor", "inRange", "bitwise_or", "getCudaEnabledDeviceCount")

    def __init__(self, requested: bool = True) -> None:
        self._requested = bool(requested)
        self._available = False
        self._enabled = False
        self._device_count = 0
        self._reason = ""
        self._runtime_failures = 0
        self._probe()

    def configure(self, requested: bool) -> None:
        requested = bool(requested)
        if requested != self._requested:
            self._requested = requested
            self._runtime_failures = 0
            self._probe()
            return
        if requested and not self._enabled and self._reason.startswith("runtime error"):
            self._runtime_failures = 0
            self._probe()

    def _probe(self) -> None:
        self._available = False
        self._enabled = False
        self._device_count = 0
        self._reason = ""
        if not self._requested:
            self._reason = "disabled by settings"
            return
        cuda_mod = getattr(cv2, "cuda", None)
        if cuda_mod is None:
            self._reason = "cv2.cuda module unavailable"
            return
        missing = [name for name in self._REQUIRED_OPS if not hasattr(cuda_mod, name)]
        if missing:
            self._reason = "OpenCV CUDA ops missing: " + ",".join(missing)
            return
        try:
            self._device_count = int(cuda_mod.getCudaEnabledDeviceCount())
        except Exception as exc:
            self._reason = f"CUDA probe failed: {str(exc)[:120]}"
            return
        if self._device_count <= 0:
            self._reason = "OpenCV was not built with an enabled CUDA device"
            return
        self._available = True
        self._enabled = True
        self._reason = "opencv-cuda ready"

    def status(self) -> Dict[str, Any]:
        return {
            "requested": bool(self._requested),
            "available": bool(self._available),
            "enabled": bool(self._enabled),
            "backend": "opencv-cuda" if self._enabled else "cpu",
            "device_count": int(self._device_count),
            "reason": self._reason,
        }

    def mask_ranges(
        self,
        roi_bgr: np.ndarray,
        ranges: List[Tuple[np.ndarray, np.ndarray]],
        kernel_size: Tuple[int, int] = (5, 5),
    ) -> Optional[np.ndarray]:
        if not self._enabled or roi_bgr is None or roi_bgr.size == 0:
            return None
        try:
            cuda_mod = cv2.cuda
            gpu = cuda_mod.GpuMat()
            gpu.upload(roi_bgr)
            hsv_gpu = cuda_mod.cvtColor(gpu, cv2.COLOR_BGR2HSV)
            mask_gpu = None
            for lo, hi in ranges:
                lo_u8 = np.array(lo, dtype=np.uint8).reshape(3)
                hi_u8 = np.array(hi, dtype=np.uint8).reshape(3)
                part_gpu = cuda_mod.inRange(hsv_gpu, lo_u8, hi_u8)
                if mask_gpu is None:
                    mask_gpu = part_gpu
                else:
                    mask_gpu = cuda_mod.bitwise_or(mask_gpu, part_gpu)
            if mask_gpu is None:
                return None
            if hasattr(cuda_mod, "createMorphologyFilter") and kernel_size[0] > 1 and kernel_size[1] > 1:
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, kernel_size)
                try:
                    src_type = mask_gpu.type()
                except Exception:
                    src_type = cv2.CV_8UC1
                morph = cuda_mod.createMorphologyFilter(cv2.MORPH_CLOSE, src_type, kernel)
                mask_gpu = morph.apply(mask_gpu)
            return mask_gpu.download()
        except Exception as exc:
            self._runtime_failures += 1
            self._reason = f"runtime error, using cpu: {str(exc)[:120]}"
            if self._runtime_failures >= 3:
                self._enabled = False
            return None


@dataclass
class _FillMetrics:
    fill_pct: float = 0.0
    top_pixel_row: int = -1
    fill_pixels: int = 0
    total_pixels: int = 0
    filled_units: float = 0.0
    total_units: float = 1.0
    is_vertical: bool = True


@dataclass
class _TrackMetrics:
    """Fill + green measured against the FULL meter track (green-top -> fill
    bottom anchor), NOT the fill bounding box. The detector's colour contour
    bounds only the filled portion, so a bbox-relative fill is always ~100%
    (the "dumps at 100%" bug). The track model measures how far the fill has
    risen toward the green window at the top of the track."""
    fill_pct: float = 0.0
    fill_top_row: int = -1
    track_top_row: int = -1
    track_bottom_row: int = -1
    track_height: float = 1.0
    green_found: bool = False
    green_start_pct: float = -1.0
    green_end_pct: float = -1.0
    green_center_pct: float = -1.0
    green_width_pct: float = 0.0
    green_confidence: float = 0.0
    green_cluster_px: int = 0
    green_top_row: int = -1
    green_bottom_row: int = -1
    green_remembered: bool = False
    rejection_reason: str = ""


def _gauss_smooth_1d(arr: np.ndarray, sigma: float = 1.2) -> np.ndarray:
    """Fast Gaussian smoothing of a 1-D profile using NumPy convolution.
    Avoids scipy dependency; fallback to original if array is too short."""
    n = len(arr)
    if n < 3:
        return arr
    radius = max(1, int(3.0 * sigma + 0.5))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / max(1e-6, sigma)) ** 2)
    kernel /= kernel.sum()
    return np.convolve(arr, kernel, mode="same")


def _gaussian_threshold_crossing(profile: np.ndarray, thr: float, from_start: bool = True) -> float:
    """Find the sub-pixel position where a smoothed profile crosses `thr`.
    `from_start=True`  -> find first rising crossing (used for top fill edge).
    `from_start=False` -> find last falling crossing from end (used for right edge).
    Returns the crossing index as a float, or -1.0 if not found."""
    n = len(profile)
    if from_start:
        for i in range(1, n):
            a, b = float(profile[i - 1]), float(profile[i])
            if a < thr <= b:
                # Linear interpolation of exact crossing, then parabolic refinement
                t = (thr - a) / max(1e-9, b - a)
                cross = float(i - 1) + t
                # Parabolic refinement using immediate neighbours
                if 0 < i < n - 1:
                    pm = float(profile[i - 1])
                    p0 = float(profile[i])
                    pp = float(profile[i + 1])
                    denom = pm - 2.0 * p0 + pp
                    if abs(denom) > 1e-9:
                        offset = 0.5 * (pm - pp) / denom
                        cross = float(i) + max(-0.5, min(0.5, float(offset)))
                return max(0.0, cross)
    else:
        for i in range(n - 2, -1, -1):
            a, b = float(profile[i + 1]), float(profile[i])
            if a < thr <= b:
                t = (thr - a) / max(1e-9, b - a)
                cross = float(i + 1) - t
                if 0 < i < n - 1:
                    pm = float(profile[i - 1])
                    p0 = float(profile[i])
                    pp = float(profile[i + 1])
                    denom = pm - 2.0 * p0 + pp
                    if abs(denom) > 1e-9:
                        offset = 0.5 * (pm - pp) / denom
                        cross = float(i) + max(-0.5, min(0.5, float(offset)))
                return min(float(n - 1), cross)
    return -1.0


def subpixel_fill_metrics(
    frame_bgr: np.ndarray,
    x: float,
    y: float,
    w: int,
    h: int,
    meter_color: str = "Purple",
    bgr_lo: Optional[np.ndarray] = None,
    bgr_hi: Optional[np.ndarray] = None,
    hsv_lo: Optional[np.ndarray] = None,
    hsv_hi: Optional[np.ndarray] = None,
) -> _FillMetrics:
    H, W = frame_bgr.shape[:2]
    ix, iy = int(round(x)), int(round(y))
    x2 = min(W, ix + int(w))
    y2 = min(H, iy + int(h))
    ix, iy = max(0, ix), max(0, iy)
    if x2 <= ix or y2 <= iy:
        return _FillMetrics()

    patch = frame_bgr[iy:y2, ix:x2]
    ph, pw = patch.shape[:2]
    if ph <= 0 or pw <= 0:
        return _FillMetrics()

    patch_hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    mask = np.zeros((ph, pw), dtype=np.uint8)
    if hsv_lo is not None and hsv_hi is not None:
        mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, hsv_lo, hsv_hi))
    elif meter_color in _HSV_FILL_RANGES:
        hlo, hhi = _HSV_FILL_RANGES[meter_color]
        mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, np.array(hlo, np.uint8), np.array(hhi, np.uint8)))

    # Red wraps the hue circle (0/180): the BGR->HSV style bounds only cover the 0-side, so
    # on a stream that renders the meter nearer H~179 the fill was under-measured (counted the
    # green tip but not the red column). OR in BOTH red sides so red fill is counted wherever
    # it sits around 0. (Cheap, Red-only; other colours unchanged.)
    if meter_color == "Red":
        mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, np.array([168, 110, 110], np.uint8), np.array([179, 255, 255], np.uint8)))
        mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, np.array([0, 110, 110], np.uint8), np.array([10, 255, 255], np.uint8)))

    mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, np.array([40, 80, 80], np.uint8), np.array([80, 255, 255], np.uint8)))
    mask = cv2.bitwise_or(mask, cv2.inRange(patch_hsv, np.array([20, 100, 100], np.uint8), np.array([35, 255, 255], np.uint8)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)

    fill_px = int(cv2.countNonZero(mask))
    total_px = int(ph * pw)
    if fill_px <= 0 or total_px <= 0:
        return _FillMetrics(fill_pixels=fill_px, total_pixels=total_px)

    is_vertical = bool(ph >= pw * 0.70)
    if is_vertical:
        row_counts = np.count_nonzero(mask, axis=1).astype(np.float64)
        thr_cols = max(1.0, pw * 0.20)
        filled_rows = row_counts >= thr_cols
        if not np.any(filled_rows):
            return _FillMetrics(fill_pixels=fill_px, total_pixels=total_px, is_vertical=True, total_units=float(ph))
        idxs = np.flatnonzero(filled_rows)
        # Gaussian-smoothed profile: find sub-pixel threshold crossing with
        # parabolic interpolation for ±0.2 row precision (vs ±0.5 for linear).
        smooth = _gauss_smooth_1d(row_counts, sigma=1.2)
        cross = _gaussian_threshold_crossing(smooth, thr_cols, from_start=True)
        if cross >= 0.0:
            sub_top = cross
        else:
            # Fallback: linear interpolation from original (legacy path)
            top_filled = float(idxs[0])
            if top_filled > 0.0:
                above = float(row_counts[int(top_filled) - 1])
                at = float(row_counts[int(top_filled)])
                if at > above and at > 0.0:
                    frac = (thr_cols - above) / max(0.001, at - above)
                    sub_top = top_filled - (1.0 - max(0.0, min(1.0, frac)))
                else:
                    sub_top = top_filled
            else:
                sub_top = 0.0
        sub_top = max(0.0, min(float(ph), sub_top))

        filled_units = float(ph) - sub_top
        fill_pct = (filled_units / float(ph)) * 100.0
        return _FillMetrics(max(0.0, min(100.0, round(fill_pct, 3))), iy + int(round(sub_top)), fill_px, total_px, filled_units, float(ph), True)

    col_counts = np.count_nonzero(mask, axis=0).astype(np.float64)
    thr_rows = max(1.0, ph * 0.20)
    filled_cols = col_counts >= thr_rows
    if not np.any(filled_cols):
        return _FillMetrics(fill_pixels=fill_px, total_pixels=total_px, is_vertical=False, total_units=float(pw))

    idxs = np.flatnonzero(filled_cols)
    # Gaussian-smoothed profile for horizontal meters: find right-edge sub-pixel crossing.
    smooth_c = _gauss_smooth_1d(col_counts, sigma=1.2)
    cross_r = _gaussian_threshold_crossing(smooth_c, thr_rows, from_start=False)
    if cross_r >= 0.0:
        sub_right = cross_r
    else:
        # Fallback: linear interpolation (legacy)
        rightmost = float(idxs[-1])
        if int(rightmost) < pw - 1:
            at = float(col_counts[int(rightmost)])
            nxt = float(col_counts[int(rightmost) + 1])
            if at > nxt and at > 0.0:
                frac = (thr_rows - nxt) / max(0.001, at - nxt)
                sub_right = rightmost + max(0.0, min(1.0, frac))
            else:
                sub_right = rightmost
        else:
            sub_right = float(idxs[-1])
    sub_right = max(0.0, min(float(pw - 1), sub_right))

    filled_units = sub_right + 1.0
    fill_pct = (filled_units / float(pw)) * 100.0
    return _FillMetrics(max(0.0, min(100.0, round(fill_pct, 3))), iy, fill_px, total_px, filled_units, float(pw), False)


def subpixel_fill_pct(frame_bgr: np.ndarray, x: float, y: float, w: int, h: int, meter_color: str = "Purple", bgr_lo: Optional[np.ndarray] = None, bgr_hi: Optional[np.ndarray] = None) -> Tuple[float, int]:
    m = subpixel_fill_metrics(frame_bgr, x, y, w, h, meter_color, bgr_lo, bgr_hi)
    return m.fill_pct, m.top_pixel_row

@dataclass
class _MatchResult:
    found: bool = False
    template_name: str = ""
    confidence: float = 0.0
    x: float = 0.0
    y: float = 0.0
    w: int = 0
    h: int = 0
    zone: str = ""
    bgr_lo: Optional[np.ndarray] = None
    bgr_hi: Optional[np.ndarray] = None
    hsv_lo: Optional[np.ndarray] = None
    hsv_hi: Optional[np.ndarray] = None
    style_ref: Optional[Dict[str, Any]] = None
    # Masked-median HSV of the winning candidate's mask pixels (the colour-purity
    # statistic). -1.0 when the purity gate did not measure it (too few mask px /
    # gate disabled). Carried so the stability validator can fast-acquire a frame
    # that is UNAMBIGUOUS by colour even when its confidence/aspect fall just short
    # of the generic fast-acquire thresholds (a genuine LOW-FILL early-rise meter).
    med_h: float = -1.0
    med_s: float = -1.0
    # True when the neon-green make-window was found directly above this candidate's
    # fill (the meter's signature). Used by the stability validator's acquisition
    # corroboration so a green-confirmed meter latches immediately while a static red
    # blob (no chevron) must instead prove a rising fill before it can latch.
    green_confirmed: bool = False
    # True when this match came from a STRONG trained-locator (v6) box (yolo_conf >= _LOC_STRONG_CONF).
    # The learned locator firing confidently HERE is the meter-appearance proof the rise-check stands
    # in for, so the stability validator lets it corroborate a lock on its own (Track T). Only ever set
    # on the locator-served path, so the classical validator behaviour is unchanged.
    loc_strong: bool = False


@dataclass
class _FrameState:
    valid: bool = False
    fill_pct: float = 0.0
    confidence: float = 0.0
    pos_x: float = 0.0
    pos_y: float = 0.0
    timestamp_s: float = 0.0


class _StabilityValidator:
    def __init__(self, cfg: DetectorConfig):
        self._cfg = cfg
        self._consecutive_valid = 0
        self._freeze_count = 0
        self._aborted = False
        self._last_valid: Optional[_FrameState] = None
        # Latched True once a shot is ACQUIRED (min_consecutive clean frames). While
        # tracking we follow the meter through motion (fades) instead of re-gating it.
        self._tracking = False
        self._frame_w = 0
        # Per-frame diagnostics (read by MeterDetector.last_debug -> detframes.csv):
        # what the position-jump gate measured and which validate() path fired.
        self.last_jump_px: float = -1.0
        self.last_acq_gate_px: float = -1.0
        self.last_event: str = ""
        self._acq_fills: deque = deque(maxlen=6)
        # Warm re-acquire memory: where+when a corroborated tracking lock was last LOST, so the
        # same meter re-latching moments later doesn't have to re-prove its rise (see _WARM_REACQ_S).
        self._warm_pos: Optional[Tuple[float, float]] = None
        self._warm_ts: float = -1.0
        # Track T (locator era). Every fix below is gated on _locator_mode so the classical PARK
        # path (ORION_METER_LOCATOR unset) is byte-identical. _low_conf_streak drives the confidence
        # hysteresis; _pan_px (|loc_mem velocity| px/frame, set per-frame by the detector) widens the
        # re-acquire jump gate during a fast pan. MeterDetector flips _locator_mode True at construction.
        self._low_conf_streak = 0
        self._locator_mode = False
        self._pan_px = 0.0

    def _note_lock_loss(self, ts: float) -> None:
        if not self._locator_mode:
            # Classical path unchanged: warm only arms on the loss of a LATCHED tracking lock.
            if self._tracking and self._last_valid is not None:
                self._warm_pos = (float(self._last_valid.pos_x), float(self._last_valid.pos_y))
                self._warm_ts = float(ts)
            return
        # Locator mode (Track T-a2): arm warm on ANY loss that had a RECENT valid detection, not only
        # a latched tracking loss. A shot that starts sub-threshold never latches _tracking, so the old
        # _tracking-gated arm left the whole rise blind (live: 25 detected / 0 fed). Recency-bounded so
        # a stale anchor can't arm warm long after the meter left.
        if self._last_valid is not None and (float(ts) - float(self._last_valid.timestamp_s)) <= _WARM_REACQ_S:
            self._warm_pos = (float(self._last_valid.pos_x), float(self._last_valid.pos_y))
            self._warm_ts = float(ts)

    def reset(self) -> None:
        self._consecutive_valid = 0
        self._freeze_count = 0
        self._aborted = False
        self._last_valid = None
        self._tracking = False
        self.last_jump_px = -1.0
        self.last_acq_gate_px = -1.0
        self.last_event = ""
        self._low_conf_streak = 0
        self._acq_fills.clear()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def consecutive_valid(self) -> int:
        return self._consecutive_valid

    @property
    def tracking(self) -> bool:
        return self._tracking

    def note_frame_width(self, w: int) -> None:
        """Record the captured frame width so the position-jump gate can scale with
        it (an absolute-pixel gate is too strict at the full-res capture)."""
        if w and w > 0:
            self._frame_w = int(w)

    def _acq_jump_gate_px(self) -> float:
        """Acquisition position-jump tolerance, scaled from the reference width the
        absolute gate was tuned on up to the real frame width."""
        base = float(self._cfg.position_jump_max_px)
        ref = max(1, int(getattr(self._cfg, "jump_gate_ref_width", 720)))
        return base * max(1.0, float(self._frame_w) / ref)

    def validate(self, match: _MatchResult, fill_pct: float, ts: float) -> _FrameState:
        state = _FrameState(False, fill_pct, match.confidence, match.x, match.y, ts)
        self.last_jump_px = -1.0
        self.last_acq_gate_px = self._acq_jump_gate_px()
        # Snapshot the PREVIOUS valid detection before this frame's paths overwrite _last_valid, so
        # the recent-proximity corroboration below can ask "was there a valid meter near here <0.5s
        # ago?" without the snapshot collapsing to distance 0 / age 0. Locator mode only.
        _prev_pos = None
        _prev_ts = -1.0
        if self._last_valid is not None:
            _prev_pos = (float(self._last_valid.pos_x), float(self._last_valid.pos_y))
            _prev_ts = float(self._last_valid.timestamp_s)

        if not match.found:
            self.last_event = "miss"
            self._freeze_count += 1
            if self._freeze_count > self._cfg.max_freeze_frames:
                self._aborted = True
                self._consecutive_valid = 0
                self._note_lock_loss(ts)
                self._tracking = False
                self._acq_fills.clear()
            return state

        self._freeze_count = 0
        if match.confidence < self._cfg.confidence_threshold:
            # HYSTERESIS (Track T-a1, locator mode). A single sub-threshold dip during a HEALTHY,
            # positionally-consistent track is motion / early-rise noise (live: conf min 0.245 /
            # median 0.358 mid-rise), not a lost meter. Hold the lock (do NOT clear _acq_fills) for up
            # to low_conf_grace_frames such frames before dropping it — mirrors the max_freeze_frames
            # miss grace. A real loss sustains the dip past the grace and resets normally below.
            if self._locator_mode and self._tracking and self._last_valid is not None:
                _gdx = abs(match.x - self._last_valid.pos_x)
                _gdy = abs(match.y - self._last_valid.pos_y)
                _gjump = float(np.hypot(_gdx, _gdy))
                _gtrack = self._acq_jump_gate_px() * max(1.0, float(self._cfg.tracking_jump_scale))
                if _gjump <= _gtrack:
                    self._low_conf_streak += 1
                    if self._low_conf_streak <= max(0, int(self._cfg.low_conf_grace_frames)):
                        self.last_event = "low_conf_grace"
                        self.last_jump_px = _gjump
                        state.valid = True
                        self._last_valid = state  # follow position so the re-latch gate stays tight
                        return state
            # Below the trust floor entirely (or grace exhausted) — drop the lock and re-acquire.
            self.last_event = "low_conf_reset"
            self._consecutive_valid = 0
            self._note_lock_loss(ts)
            self._tracking = False
            self._acq_fills.clear()
            self._low_conf_streak = 0
            return state
        self._low_conf_streak = 0

        if self._last_valid is not None:
            dx = abs(match.x - self._last_valid.pos_x)
            dy = abs(match.y - self._last_valid.pos_y)
            jump = float(np.hypot(dx, dy))
            acq_gate = self._acq_jump_gate_px()
            self.last_jump_px = jump
            if self._tracking:
                # Shot in progress: ALLOW the meter to move. A fade slides it across
                # the court frame-to-frame — real motion, not instability. Only a
                # teleport-sized jump (a different blob entirely) breaks the lock.
                track_gate = acq_gate * max(1.0, float(self._cfg.tracking_jump_scale))
                if jump > track_gate:
                    self.last_event = "track_break"
                    self._consecutive_valid = 0
                    self._note_lock_loss(ts)
                    self._tracking = False
                    self._acq_fills.clear()
                    self._last_valid = state  # re-anchor at the new spot so we can re-acquire
                    return state
            else:
                # Still acquiring: keep the tight gate so a wandering false positive
                # can't latch. (This is what keeps idle blobs out of the engine.)
                eff_acq_gate = acq_gate
                if self._locator_mode:
                    # Track T-a3: a meter that PANNED far during a re-acquire must re-latch, not
                    # perpetually acq_jump_reset (live: 8 acq_jump_reset in one shot). Widen the gate
                    # to the TRACKING gate while a warm window is active (a corroborated lock was lost
                    # moments ago — the same meter is moving back), and/or by the measured locator pan
                    # speed (2 frames of motion). The base gate stays tight when nothing moved.
                    _warm_active = (_WARM_REACQ_S > 0.0 and self._warm_pos is not None
                                    and (ts - self._warm_ts) <= _WARM_REACQ_S)
                    if _warm_active:
                        eff_acq_gate = max(eff_acq_gate, acq_gate * max(1.0, float(self._cfg.tracking_jump_scale)))
                    if self._pan_px > 0.0:
                        eff_acq_gate = max(eff_acq_gate, acq_gate + 2.0 * float(self._pan_px))
                if jump > eff_acq_gate:
                    # RE-ANCHOR to the current position — leaving _last_valid stale here
                    # was a latch-killer: a meter that appears far from the previous
                    # detection (new shot / different court position) measured a huge jump
                    # from the stale anchor EVERY frame and could NEVER acquire (perpetual
                    # bbox_unstable -> engine starved -> timeout_fallback). Reset the streak
                    # but move the anchor so a sustained new position acquires normally; a
                    # genuinely wandering blob still can't get min_consecutive in a row.
                    self.last_event = "acq_jump_reset"
                    self._consecutive_valid = 0
                    self._acq_fills.clear()
                    self._last_valid = state
                    return state
                if match.confidence < self._cfg.confidence_drop_abort_threshold and self._last_valid.confidence >= self._cfg.confidence_drop_abort_threshold + 0.1:
                    self.last_event = "conf_drop_reset"
                    self._consecutive_valid = 0
                    return state

        # This frame cleared every gate (found, confident, positionally stable or a
        # legitimately moving tracked shot). Count it; only ACQUIRE (latch tracking)
        # once we've seen min_consecutive_valid_frames in a row — a transient purple
        # HUD/jersey blob can't sustain that, which keeps idle false positives out of
        # the timing engine. Once latched, every subsequent frame stays VALID through
        # motion so the engine gets a continuous fill signal (no more fade freeze).
        self._consecutive_valid += 1
        self._aborted = False
        self._last_valid = state
        if not self._tracking:
            self._acq_fills.append(float(fill_pct))
        # Fast-acquire: an UNAMBIGUOUS meter frame (high confidence AND clearly taller
        # than wide) latches immediately so the fast Arrow2 rise isn't eaten by the
        # warmup. A wide cosmetic blob (floatie, ar<1) or a low-confidence transient
        # (genuine non-detections cap at conf 0.82) can never satisfy both, so this
        # keeps idle false positives out while feeding the rise the engine needs.
        # Shape comes from the real _MatchResult (always has w/h). Guard with getattr
        # so a stripped test double / payload without geometry simply skips the
        # fast path and warms up normally instead of raising.
        _mw = float(getattr(match, "w", 0.0) or 0.0)
        _mh = float(getattr(match, "h", 0.0) or 0.0)
        ar = _mh / max(1.0, _mw)
        fast = (_mw > 0.0
                and match.confidence >= float(self._cfg.fast_acquire_conf)
                and ar >= float(self._cfg.fast_acquire_min_ar))
        # Colour-purity fast-acquire: a frame that is UNAMBIGUOUS by colour (masked
        # median dead-on the meter band, saturated) latches even when conf/aspect
        # fall just short of the generic gate above — this is the genuine low-fill
        # early-rise meter the feedforward anchor must lock onto. The same purity
        # signature already rejects the floatie to found=False upstream, so this
        # admits no new false positives. med_h/med_s come from the real
        # _MatchResult; a stripped test double without them returns -1 -> no latch.
        purity_fast = (_mw > 0.0 and _purity_fast_acquire_ok(
            self._cfg, str(getattr(self._cfg, "meter_color", "") or ""),
            float(getattr(match, "med_h", -1.0)), float(getattr(match, "med_s", -1.0))))
        fast = fast or purity_fast
        # ACQUISITION CORROBORATION (colour-aware). For a RARE meter colour (Purple/Cyan) the
        # colour purity is unambiguous, so the fast/purity acquire above feeds the meter-
        # appearance on frame 0 unchanged (the feedforward anchor needs it; the off-hue floatie
        # never matches). For an AMBIGUOUS scene colour (RED: red jerseys/court-logos/sponsor-
        # stripes/UI are everywhere), colour is NOT a discriminator, so a NEW red lock must prove
        # the meter's OWN behaviour before it latches: a RISING fill, or the neon-GREEN chevron
        # above it from the CLASSICAL scan (a red jersey has no chevron). This is the kill for the
        # live red "locks onto random objects" scatter (detframes degraded A/B: 14 static false-
        # lock episodes, X-centre std 511px -> 0, real meters 12 -> 12) while still catching the
        # genuine red appearance via its green tip. green_first results are EXCLUDED from the green
        # bypass (that loose locator finds green-over-red anywhere) and must rise. Net-rise +
        # "latest at the running peak" rejects detection noise on a static blob. Once TRACKING we
        # follow through motion (no re-check). Kill-switch: ORION_ACQ_CORROBORATE=0.
        colour_ambiguous = str(getattr(self._cfg, "meter_color", "") or "") in _AMBIGUOUS_SCENE_COLOURS
        # A classical green chevron above the bar LOWERS the rise bar (a green-tipped candidate
        # is very likely the meter, so a smaller proven rise corroborates it faster) — but it is
        # NOT a standalone bypass: a STATIC green-over-red object (a court logo) has 0 rise and so
        # is still rejected (offline: green-bypass left 1 static false-lock; rise-only leaves 0).
        # green_first results are excluded (that loose locator finds green-over-red anywhere).
        green_ok = (bool(getattr(match, "green_confirmed", False))
                    and str(getattr(match, "zone", "") or "") != "green_first")
        af = self._acq_fills
        rise_need = (_ACQ_RISE_MIN * 0.4) if green_ok else _ACQ_RISE_MIN
        rising = (len(af) >= 2 and (float(af[-1]) - float(af[0])) >= rise_need
                  and float(af[-1]) >= max(af) - 1.0)
        # A PARK-zone match is already authoritative: _ParkTracker only emits after its own strong
        # conjunction (thin pure-red column in the X/Y play band + bottom notch + a monotone rise from a
        # near-fixed bottom over >=2 frames + static-red EMA suppression) — STRONGER than this generic
        # rise-check. So it corroborates standalone, letting the early park lock reach the overlay on its
        # first emit frame instead of waiting ~1 more frame for this validator's own rise history.
        park_zone = str(getattr(match, "zone", "") or "") == "park"
        # A park-zone emit is NO LONGER auto-authoritative: it must prove a RISING fill (or already be
        # tracking) like every other ambiguous-red lock, so a static red wallpaper can't latch on its first
        # emit. Restore the old first-frame bypass with ORION_PARK_AUTO_CORROBORATE=1. [[live-test 2026-07-02]]
        # WARM RE-ACQUIRE: this candidate is near where a corroborated lock was lost moments ago —
        # it's the same meter continuing its rise. Inherit corroboration AND latch immediately so a
        # 1-2 frame dropout mid-rise doesn't leave the rest of the rise blind re-proving itself.
        warm = False
        if _WARM_REACQ_S > 0.0 and self._warm_pos is not None and (ts - self._warm_ts) <= _WARM_REACQ_S:
            wdx = abs(match.x - self._warm_pos[0]); wdy = abs(match.y - self._warm_pos[1])
            warm = float(np.hypot(wdx, wdy)) <= self._acq_jump_gate_px() * 2.0
        # Track T-a4 (locator mode). A STRONG v6 box IS the appearance proof the rise-check stands in
        # for — the learned meter detector fired confidently HERE — so it corroborates on its own. And a
        # candidate within ~1 acq gate of a valid detection <_RECENT_PROX_S ago is the same meter
        # continuing: mid-shot the fill is flat/falling and can't re-prove a rise, so let proximity to a
        # recent lock re-latch it. Both are locator-mode only, so the classical corroboration is unchanged.
        loc_strong = self._locator_mode and bool(getattr(match, "loc_strong", False))
        recent_prox = False
        if self._locator_mode and _prev_pos is not None and _RECENT_PROX_S > 0.0:
            _rdx = abs(match.x - _prev_pos[0]); _rdy = abs(match.y - _prev_pos[1])
            recent_prox = (float(np.hypot(_rdx, _rdy)) <= self._acq_jump_gate_px()
                           and (float(ts) - _prev_ts) <= _RECENT_PROX_S)
        # loc_strong (a conf>=0.55 v6 box) is NOT a standalone corroboration: with
        # min_consecutive_valid_frames=1 a single strong box on decor would latch with ZERO
        # behavioral evidence. Require it PAIRED with a behavioral signal (rise / green chevron /
        # proximity to a recent valid lock) so the learned-detector confidence corroborates real
        # meter dynamics, not a static prop that merely looks meter-like to v6.
        corroborated = (self._tracking or rising or warm or (park_zone and _PARK_AUTO_CORROBORATE)
                        or (loc_strong and (rising or green_ok or recent_prox)) or recent_prox)
        latch = (self._consecutive_valid >= max(1, int(self._cfg.min_consecutive_valid_frames)) or fast or warm)
        if latch and _ACQ_CORROBORATE and colour_ambiguous and not corroborated:
            # Ambiguous-colour lock that isn't rising and has no classical green chevron ->
            # a static red blob (jersey/logo/banner/floor/post-shot deflate). Hold the latch
            # until it proves a rise; a real meter does so within ~1-2 frames, a blob never does.
            self.last_event = "uncorroborated"
            state.valid = False
            return state
        if latch:
            self._tracking = True
            self._warm_pos = None; self._warm_ts = -1.0   # consumed — a fresh loss must re-arm it
        state.valid = self._tracking
        if self._tracking:
            if warm and self._consecutive_valid < max(1, int(self._cfg.min_consecutive_valid_frames)):
                self.last_event = "warm_reacquired"
            elif fast and self._consecutive_valid < max(1, int(self._cfg.min_consecutive_valid_frames)):
                # Distinguish the colour-purity latch (low-fill early-rise meter) from
                # the generic conf/aspect latch in detframes.csv diagnostics.
                self.last_event = "purity_acquired" if (purity_fast and not (
                    match.confidence >= float(self._cfg.fast_acquire_conf)
                    and ar >= float(self._cfg.fast_acquire_min_ar))) else "fast_acquired"
            else:
                self.last_event = "acquired"
        else:
            self.last_event = "count"
        return state


class _MotionEstimator:
    _LWLS_LAMBDA = 0.5

    def __init__(self, cfg: DetectorConfig):
        self._cfg = cfg
        self._samples: deque = deque(maxlen=max(6, cfg.velocity_rolling_window + 2))
        self._vel_hist: deque = deque(maxlen=cfg.velocity_rolling_window)
        self.velocity_pct_s = 0.0
        self.accel_pct_s2 = 0.0
        self.velocity_px_s = 0.0
        self.accel_px_s2 = 0.0
        self._stable = False
        self._prev_ts = 0.0
        self._prev_v_pct = 0.0
        self._prev_v_px = 0.0

    def reset(self) -> None:
        self._samples.clear()
        self._vel_hist.clear()
        self.velocity_pct_s = 0.0
        self.accel_pct_s2 = 0.0
        self.velocity_px_s = 0.0
        self.accel_px_s2 = 0.0
        self._stable = False
        self._prev_ts = 0.0
        self._prev_v_pct = 0.0
        self._prev_v_px = 0.0

    @property
    def stable(self) -> bool:
        return self._stable

    @staticmethod
    def _weighted_slope(values: np.ndarray, times: np.ndarray) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        lam = _MotionEstimator._LWLS_LAMBDA
        w = np.array([np.exp(-lam * (n - 1 - i)) for i in range(n)], dtype=np.float64)
        t = times - times[0]
        sw = float(np.sum(w))
        if sw < 1e-9:
            return 0.0
        tm = float(np.dot(w, t) / sw)
        vm = float(np.dot(w, values) / sw)
        num = float(np.dot(w, (t - tm) * (values - vm)))
        den = float(np.dot(w, (t - tm) ** 2))
        if den < 1e-9:
            return 0.0
        return num / den

    @staticmethod
    def _weighted_quadratic(values: np.ndarray, times: np.ndarray):
        """Exponentially-weighted least-squares fit f = a*u^2 + b*u + c with u
        centred at the LATEST sample (u<=0). Returns (velocity_at_latest=b,
        acceleration=2a) in the same units/sec(^2) as the inputs, or None when
        there are too few samples or the system is ill-conditioned.

        Deriving both velocity and acceleration from one curve fit is far
        smoother than differencing consecutive linear-slope estimates (which
        squares the per-frame detection noise)."""
        n = len(values)
        if n < 4:
            return None
        lam = _MotionEstimator._LWLS_LAMBDA
        w = np.array([np.exp(-lam * (n - 1 - i)) for i in range(n)], dtype=np.float64)
        u = times - times[-1]
        s0 = float(np.sum(w)); s1 = float(np.sum(w * u)); s2 = float(np.sum(w * u * u))
        s3 = float(np.sum(w * u ** 3)); s4 = float(np.sum(w * u ** 4))
        t0 = float(np.sum(w * values)); t1 = float(np.sum(w * u * values)); t2 = float(np.sum(w * u * u * values))
        m = np.array([[s4, s3, s2], [s3, s2, s1], [s2, s1, s0]], dtype=np.float64)
        v = np.array([t2, t1, t0], dtype=np.float64)
        try:
            if abs(float(np.linalg.det(m))) < 1e-12:
                return None
            a, b, _c = np.linalg.solve(m, v)
        except Exception:
            return None
        return float(b), float(2.0 * a)

    def update(self, fill_pct: float, fill_units: float, ts: float, valid: bool) -> None:
        if not valid:
            return
        self._samples.append((float(fill_pct), float(fill_units), float(ts)))
        if len(self._samples) < 2:
            self.velocity_pct_s = 0.0
            self.accel_pct_s2 = 0.0
            self.velocity_px_s = 0.0
            self.accel_px_s2 = 0.0
            self._stable = False
            return

        fp = np.array([s[0] for s in self._samples], dtype=np.float64)
        fu = np.array([s[1] for s in self._samples], dtype=np.float64)
        tm = np.array([s[2] for s in self._samples], dtype=np.float64)

        vp = self._weighted_slope(fp, tm)
        vx = self._weighted_slope(fu, tm)

        vmax = self._cfg.velocity_max_pct_per_sec

        # Prefer a single weighted-quadratic fit for fill velocity + acceleration
        # (smoother accel than differencing consecutive slopes). Fall back to the
        # linear slope + finite-difference accel until enough samples exist.
        quad = self._weighted_quadratic(fp, tm)
        if quad is not None:
            vq, aq = quad
            vp = max(-vmax, min(vmax, float(vq)))
            self.accel_pct_s2 = max(-2000.0, min(2000.0, float(aq)))
        else:
            vp = max(-vmax, min(vmax, float(vp)))
            if self._prev_ts > 0.0 and ts > self._prev_ts:
                dt = ts - self._prev_ts
                self.accel_pct_s2 = max(-2000.0, min(2000.0, (vp - self._prev_v_pct) / dt))
            else:
                self.accel_pct_s2 = 0.0

        # Pixel-space acceleration stays a finite difference (telemetry only).
        if self._prev_ts > 0.0 and ts > self._prev_ts:
            dt = ts - self._prev_ts
            self.accel_px_s2 = max(-5000.0, min(5000.0, (vx - self._prev_v_px) / dt))
        else:
            self.accel_px_s2 = 0.0

        self.velocity_pct_s = vp
        self.velocity_px_s = float(vx)
        self._prev_ts = ts
        self._prev_v_pct = vp
        self._prev_v_px = vx

        self._vel_hist.append(vp)
        if len(self._vel_hist) >= self._cfg.velocity_rolling_window:
            arr = np.array(list(self._vel_hist), dtype=np.float64)
            mu = float(np.mean(arr))
            self._stable = (float(np.std(arr)) / max(0.001, abs(mu))) <= self._cfg.velocity_stability_tolerance
        else:
            self._stable = False

    @staticmethod
    def eta_to_target_ms(fill_pct: float, target_pct: float, vel_pct_s: float, acc_pct_s2: float, latency_ms: float, velocity_floor: float) -> float:
        if vel_pct_s <= max(0.01, velocity_floor * 0.2):
            return -1.0
        remaining = float(target_pct) - float(fill_pct)
        if remaining <= 0.0:
            return 0.0

        t = -1.0
        if abs(acc_pct_s2) > 0.1:
            disc = vel_pct_s * vel_pct_s + 2.0 * acc_pct_s2 * remaining
            if disc >= 0.0:
                root = float(np.sqrt(disc))
                roots = [
                    rt for rt in (
                        (-vel_pct_s + root) / acc_pct_s2,
                        (-vel_pct_s - root) / acc_pct_s2,
                    )
                    if rt >= 0.0
                ]
                if roots:
                    t = min(roots)
                
        if t < 0.0:
            t = remaining / max(0.01, vel_pct_s)

        # Return the raw time-to-target. Callers compare this against their live
        # latency budget instead of subtracting latency twice.
        eta = t * 1000.0
        return float(max(0.0, min(5000.0, eta)))


class DynamicROILock:
    _PADDING = 18
    _MIN_SIZE = 72
    # EMA coefficient: 0.35 = responsive but stable. Lower = smoother, higher = more responsive.
    _EMA_ALPHA: float = 0.35
    # Minimum position delta (px) required before EMA updates — eliminates 1-2 px flutter.
    _MIN_DELTA_PX: float = 3.0
    # Horizontal jump (px) beyond which the meter is treated as a NEW court position
    # (new shot on the other side/wing) and the lock SNAPS to it instead of EMA-
    # drifting. Well above normal drift, well below a left<->right court jump (~800px).
    _JUMP_SNAP_PX: float = 90.0
    # --- Motion-cone prediction --------------------------------------------------
    # On a MOVING meter (fade / Go-To) the old tight crop sat one frame BEHIND the
    # meter: it was centred on the EMA-smoothed centre, which lags the true centre by
    # more than the crop's own half-width once the meter slides tens of px/frame. So
    # EVERY locked-crop scan missed (measured live: 100% of locked_crop frames), the
    # detector fell back to wide, re-locked, and thrashed wide<->crop — discarding
    # ~half the live-meter frames, which then decayed into meter_memory echoes the
    # engine counts as stale. The locked crop is now a velocity-PREDICTED motion
    # cone: centred where the meter is heading and widened by its measured speed, so
    # a fade/Go-To stays inside it. When the meter is static the velocity is ~0 and
    # the cone collapses back to the old tight crop (no loss on standstill).
    _VEL_ALPHA: float = 0.5      # EMA on per-frame centre velocity (responsive, px/frame)
    _CONE_K: float = 1.3         # cone half-extent grown per px/frame of meter speed
    _PREDICT_LEAD: float = 1.0   # frames ahead (the crop is built now, used NEXT frame)
    # Go WIDE the instant we miss the locked crop (tolerance 0). A narrow predicted
    # crop held across misses just SUPPRESSES the wide re-acquire that actually finds a
    # meter after a dropout (measured: tolerating misses dropped fed 8.1%->3.4%). The
    # cone's job is to make the crop HIT while the meter is found, not to chase it
    # blind. Kept as a constant so the (harmless) miss-extrapolation path is reachable
    # for experiments without re-introducing the regression by default.
    _PREDICT_MISS_TOL: int = 0
    _MISS_GROW_PX: float = 26.0  # cone growth per miss frame (uncertainty fans out)

    def __init__(self, lock_after_frames: int = 1, timeout_frames: int = 16):
        self._lock_after = lock_after_frames
        self._timeout = timeout_frames
        self._consec_found = 0
        self._miss_frames = 0
        self._locked_bbox: Optional[Tuple[int, int, int, int]] = None
        self._locked = False
        self._centers: deque = deque(maxlen=10)
        self._jitter_px = 0.0
        # EMA smoothing state
        self._ema_cx: float = 0.0
        self._ema_cy: float = 0.0
        self._ema_w: float = 0.0
        self._ema_h: float = 0.0
        self._ema_init: bool = False
        # Motion-cone state: EMA'd per-frame centre velocity + last raw centre so we
        # can predict where the meter is heading and size the search cone to its speed.
        self._vel_cx: float = 0.0
        self._vel_cy: float = 0.0
        self._last_raw_cx: float = 0.0
        self._last_raw_cy: float = 0.0

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def jitter_px(self) -> float:
        return self._jitter_px

    @property
    def miss_frames(self) -> int:
        return self._miss_frames

    def update(self, found: bool, bbox: Optional[Tuple[int, int, int, int]]) -> None:
        if found and bbox is not None:
            # Frames since the previous found sample (>=1). Velocity is normalised by
            # this so a re-acquire after a brief dropout doesn't register as one giant
            # one-frame jump.
            gap = float(self._miss_frames + 1)
            self._miss_frames = 0
            self._consec_found += 1

            bx, by, bw, bh = bbox
            raw_cx = float(bx) + float(bw) * 0.5
            raw_cy = float(by) + float(bh) * 0.5

            if not self._ema_init:
                # Bootstrap: accept first detection as-is.
                self._ema_cx = raw_cx
                self._ema_cy = raw_cy
                self._ema_w = float(bw)
                self._ema_h = float(bh)
                self._ema_init = True
                self._vel_cx = 0.0
                self._vel_cy = 0.0
            elif abs(raw_cx - self._ema_cx) >= self._JUMP_SNAP_PX:
                # Large HORIZONTAL jump = the meter is at a NEW court position (a new
                # shot on the other side/wing), not jitter. SNAP the lock straight to
                # it instead of EMA-drifting from the old position — drifting would
                # leave the tight crop at the old spot and thrash wide<->tight for
                # several frames. (Vertical fill-rise doesn't trigger this; it's an X
                # test.) A new shot also invalidates the old velocity vector.
                self._ema_cx = raw_cx
                self._ema_cy = raw_cy
                self._ema_w = float(bw)
                self._ema_h = float(bh)
                self._vel_cx = 0.0
                self._vel_cy = 0.0
            else:
                alpha = self._EMA_ALPHA
                # Per-frame centre velocity (px/frame), EMA'd for stability. Measured
                # from the RAW centre (not the lagged EMA centre) so the cone leads the
                # true meter, and normalised by the inter-sample gap.
                va = self._VEL_ALPHA
                inst_vx = (raw_cx - self._last_raw_cx) / gap
                inst_vy = (raw_cy - self._last_raw_cy) / gap
                self._vel_cx = va * inst_vx + (1.0 - va) * self._vel_cx
                self._vel_cy = va * inst_vy + (1.0 - va) * self._vel_cy
                # Only shift the smoothed center when delta exceeds min-threshold
                # — this prevents 1-2 px noise from causing visible flutter.
                delta = float(np.hypot(raw_cx - self._ema_cx, raw_cy - self._ema_cy))
                if delta >= self._MIN_DELTA_PX:
                    self._ema_cx = alpha * raw_cx + (1.0 - alpha) * self._ema_cx
                    self._ema_cy = alpha * raw_cy + (1.0 - alpha) * self._ema_cy
                # Size always EMA-smoothed (W/H rarely jitter but smooth anyway).
                self._ema_w = alpha * float(bw) + (1.0 - alpha) * self._ema_w
                self._ema_h = alpha * float(bh) + (1.0 - alpha) * self._ema_h

            self._last_raw_cx = raw_cx
            self._last_raw_cy = raw_cy

            # Rebuild locked_bbox from smoothed centre + size.
            sx = int(round(self._ema_cx - self._ema_w * 0.5))
            sy = int(round(self._ema_cy - self._ema_h * 0.5))
            self._locked_bbox = (sx, sy, int(round(self._ema_w)), int(round(self._ema_h)))

            # Jitter tracking: compare raw center against smoothed center.
            self._centers.append((raw_cx, raw_cy))
            if len(self._centers) >= 2:
                arr = np.array(self._centers, dtype=np.float64)
                mean = np.mean(arr, axis=0)
                dist = np.sqrt(np.sum((arr - mean) ** 2, axis=1))
                self._jitter_px = float(np.mean(dist))
            else:
                self._jitter_px = 0.0
            if self._consec_found >= self._lock_after:
                self._locked = True
            return

        self._consec_found = 0
        self._miss_frames += 1
        if self._miss_frames > self._timeout:
            self._locked = False
            self._locked_bbox = None
            self._centers.clear()
            self._jitter_px = 0.0
            self._miss_frames = 0
            self._ema_init = False
            self._vel_cx = 0.0
            self._vel_cy = 0.0

    def get_search_zones(self, W: int, H: int, cfg: DetectorConfig) -> List[Tuple[str, int, int, int, int]]:
        # Use the predicted motion-cone crop while the lock holds AND we've missed no
        # more than _PREDICT_MISS_TOL frames. The OLD code dropped to wide the instant
        # _miss_frames hit 1 — which on a moving meter was EVERY frame (the lagged
        # tight crop never contained the sliding meter), so it thrashed wide<->crop and
        # starved the engine. Now the cone is centred where the meter is HEADING and
        # widened by its speed, so consecutive frames stay inside it; tolerating a few
        # misses (extrapolating along the velocity vector, cone fanned out by the miss
        # count) bridges brief detector dropouts instead of thrashing. A true court
        # jump still re-acquires: after _PREDICT_MISS_TOL misses we fall to wide, and
        # JUMP_SNAP re-anchors the lock the instant the meter is re-found.
        if (self._locked and self._locked_bbox is not None
                and self._miss_frames <= self._PREDICT_MISS_TOL):
            bx, by, bw, bh = self._locked_bbox
            p = int(max(4, min(80, getattr(cfg, "micro_roi_padding_px", self._PADDING))))
            miss = int(self._miss_frames)
            vx = float(self._vel_cx)
            vy = float(self._vel_cy)
            if miss == 0:
                # Just found it — lead the meter by one frame (this crop is consumed
                # on the NEXT detect()).
                pcx = self._last_raw_cx + vx * self._PREDICT_LEAD
                pcy = self._last_raw_cy + vy * self._PREDICT_LEAD
                grow = 0.0
            else:
                # In a dropout — hold the last true centre and let the cone fan out by
                # the miss count, so a meter that kept moving OR stopped is still
                # covered without chasing it into empty space.
                pcx = self._last_raw_cx
                pcy = self._last_raw_cy
                grow = miss * self._MISS_GROW_PX
            # Cone half-extents scale with the meter's measured speed on each axis.
            hslack = abs(vx) * self._CONE_K + grow
            vslack = abs(vy) * self._CONE_K + grow
            # The (vertical) meter is bottom-anchored and grows UPWARD as it fills, so
            # keep the TALL full-meter window anchored at the (predicted) fill bottom
            # plus symmetric vertical motion slack; widen horizontally by the cone.
            horiz = int(max(self._MIN_SIZE, int(bw) + p * 4) + 2.0 * hslack)
            vert = int(max(int(H * 0.22), int(bh) + p * 3) + 2.0 * vslack)
            fill_bottom = int(round(pcy + float(bh) * 0.5))
            cx = int(round(pcx))
            y2 = min(H, fill_bottom + p + int(vslack))
            y1 = max(0, y2 - vert)
            x1 = max(0, cx - horiz // 2)
            x2 = min(W, x1 + horiz)
            x1 = max(0, x2 - horiz)
            return [("locked", x1, y1, x2, y2)]

        return [self.wide_zone(W, H, cfg)]

    def wide_zone(self, W: int, H: int, cfg: DetectorConfig) -> Tuple[str, int, int, int, int]:
        """The full unlocked search band. Used both as the lock's fallback zone and
        as the SAME-FRAME wide fallback when a tight locked-crop scan misses a meter
        that is actually present (a contour split at the crop edge can hide it)."""
        y_top = int(H * 0.20)
        y_bot = int(H * 0.95)
        x1 = int(W * min(cfg.left_zone_start_pct, cfg.right_zone_start_pct, 35.0) / 100.0)
        x2 = int(W * max(cfg.left_zone_end_pct, cfg.right_zone_end_pct, 65.0) / 100.0)
        x1 = max(0, min(W - 1, x1))
        x2 = max(x1 + 1, min(W, x2))
        return ("wide", x1, y_top, x2, y_bot)


@dataclass
class _GreenWindowResult:
    found: bool = False
    cluster_px: int = 0
    start_pct: float = -1.0
    end_pct: float = -1.0
    center_pct: float = -1.0
    width_pct: float = 0.0
    confidence: float = 0.0
    start_row: int = -1
    end_row: int = -1
    center_row: int = -1


class _GreenWindowScanner:
    # Sampled from the shared green-window swatch: RGB ~= #31FF1F, OpenCV HSV ~= [58, 224, 255].
    _NEON_GREEN_HSV_LO = np.array([48, 140, 140], dtype=np.uint8)
    _NEON_GREEN_HSV_HI = np.array([70, 255, 255], dtype=np.uint8)
    _GREEN_HSV_LO = np.array([35, 80, 80], dtype=np.uint8)
    _GREEN_HSV_HI = np.array([90, 255, 255], dtype=np.uint8)
    _YELLOW_HSV_LO = np.array([20, 100, 100], dtype=np.uint8)
    _YELLOW_HSV_HI = np.array([35, 255, 255], dtype=np.uint8)
    # Item 5: Adaptive green hue center. Stream compression and color grading can
    # shift the green hue by ±5. Track the detected green cluster's median hue
    # with a slow EMA and adjust the neon green range accordingly.
    _ADAPTIVE_HUE_CENTER_DEFAULT = 58.0  # nominal neon green hue center
    _ADAPTIVE_HUE_TOLERANCE = 10.0       # ±tolerance around center
    _ADAPTIVE_HUE_GAIN = 0.05            # slow EMA gain (5% per shot)
    _ADAPTIVE_HUE_CLAMP = 10.0           # max drift from default

    @staticmethod
    def _row_to_pct(rel_row: float, bh: int) -> float:
        return float(max(0.0, min(100.0, ((float(bh) - float(rel_row)) / max(1.0, float(bh))) * 100.0)))

    @staticmethod
    def _col_to_pct(rel_col: float, bw: int) -> float:
        return float(max(0.0, min(100.0, ((float(rel_col) + 1.0) / max(1.0, float(bw))) * 100.0)))

    def __init__(self, cfg: Optional[DetectorConfig] = None):
        self._cfg = cfg
        self._adaptive_hue_center = self._ADAPTIVE_HUE_CENTER_DEFAULT

    def set_cfg(self, cfg: Optional[DetectorConfig]) -> None:
        self._cfg = cfg

    def _adaptive_neon_lo(self) -> np.ndarray:
        """Neon green HSV lower bound adjusted by the adaptive hue center."""
        center = self._adaptive_hue_center
        lo = max(0, int(center - self._ADAPTIVE_HUE_TOLERANCE))
        return np.array([lo, 140, 140], dtype=np.uint8)

    def _adaptive_neon_hi(self) -> np.ndarray:
        """Neon green HSV upper bound adjusted by the adaptive hue center."""
        center = self._adaptive_hue_center
        hi = min(179, int(center + self._ADAPTIVE_HUE_TOLERANCE))
        return np.array([hi, 255, 255], dtype=np.uint8)

    def update_adaptive_hue(self, green_pixels_hsv: np.ndarray) -> None:
        """Item 5: Update the adaptive green hue center from detected green pixels.
        EMA-tracks the median hue of the green cluster, clamped to ±_ADAPTIVE_HUE_CLAMP
        from the default center. Called after a successful green window detection.
        """
        if green_pixels_hsv is None or green_pixels_hsv.size == 0:
            return
        med_h = float(np.median(green_pixels_hsv[:, 0]))
        if med_h > 0:
            self._adaptive_hue_center = (
                self._adaptive_hue_center * (1.0 - self._ADAPTIVE_HUE_GAIN)
                + med_h * self._ADAPTIVE_HUE_GAIN
            )
            # Clamp to ±_ADAPTIVE_HUE_CLAMP from default
            lo = self._ADAPTIVE_HUE_CENTER_DEFAULT - self._ADAPTIVE_HUE_CLAMP
            hi = self._ADAPTIVE_HUE_CENTER_DEFAULT + self._ADAPTIVE_HUE_CLAMP
            self._adaptive_hue_center = max(lo, min(hi, self._adaptive_hue_center))

    @staticmethod
    def _shape_confidence(mask: np.ndarray, span: int, cross_span: int, cluster_px: int) -> float:
        if mask is None or mask.size == 0 or span <= 0 or cross_span <= 0 or cluster_px <= 0:
            return 0.0
        area = float(max(1, span * cross_span))
        density = float(cluster_px) / area
        density_score = max(0.0, min(1.0, density / 0.42))
        tiny_bonus = 0.22 if span <= 6 else 0.0
        # Green windows are compact marks on the meter. Very large masks are
        # usually the fill color or HUD spill, so lower confidence without
        # discarding the candidate outright.
        long_axis = float(max(mask.shape[:2]))
        span_ratio = float(span) / max(1.0, long_axis)
        compact_score = 1.0 - max(0.0, min(1.0, (span_ratio - 0.34) / 0.40))
        return float(max(0.05, min(1.0, density_score * 0.58 + compact_score * 0.34 + tiny_bonus)))

    @staticmethod
    def _green_dominance_mask(roi_bgr: np.ndarray) -> np.ndarray:
        if roi_bgr is None or roi_bgr.size == 0:
            return np.zeros((0, 0), dtype=np.uint8)
        b, g, r = cv2.split(roi_bgr)
        g16 = g.astype(np.int16)
        dominance = (
            (g16 >= 118)
            & ((g16 - r.astype(np.int16)) >= 34)
            & ((g16 - b.astype(np.int16)) >= 24)
        )
        return dominance.astype(np.uint8) * 255

    def green_span_abs(
        self,
        frame_bgr: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        col_min_ratio: float = 0.15,
    ) -> Optional[Tuple[int, int, int, float]]:
        """Locate the green window's vertical span inside an ABSOLUTE-coords
        rectangle (the full meter track column, extending above the fill), and
        return (green_top_abs, green_bottom_abs, cluster_px, confidence) in
        frame coordinates, or None when no green band is present.

        Unlike scan(), this does NOT assume the rectangle is the fill bbox, so
        it can find the green window that sits above the rising fill."""
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        H, W = frame_bgr.shape[:2]
        x1 = max(0, int(x1)); y1 = max(0, int(y1))
        x2 = min(W, int(x2)); y2 = min(H, int(y2))
        if x2 <= x1 or y2 <= y1:
            return None
        roi = frame_bgr[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        if rh <= 0 or rw <= 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        neon = cv2.inRange(hsv, self._NEON_GREEN_HSV_LO, self._NEON_GREEN_HSV_HI)
        neon_px = int(cv2.countNonZero(neon))
        if neon_px >= 4:
            mask = neon
        else:
            broad = cv2.inRange(hsv, self._GREEN_HSV_LO, self._GREEN_HSV_HI)
            dom = self._green_dominance_mask(roi)
            mask = cv2.bitwise_or(broad, dom)

        # Optional trained green override from launcher/CV training.
        if self._cfg is not None:
            tlo = getattr(self._cfg, "trained_green_hsv_low", None)
            thi = getattr(self._cfg, "trained_green_hsv_high", None)
            if (isinstance(tlo, list) and isinstance(thi, list)
                    and len(tlo) == 3 and len(thi) == 3):
                try:
                    lo = np.array([max(0, min(179, int(tlo[0]))), max(0, min(255, int(tlo[1]))), max(0, min(255, int(tlo[2]))) ], np.uint8)
                    hi = np.array([max(0, min(179, int(thi[0]))), max(0, min(255, int(thi[1]))), max(0, min(255, int(thi[2]))) ], np.uint8)
                    mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
                except Exception:
                    pass

        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
        cluster_px = int(cv2.countNonZero(mask))
        if cluster_px < 2:
            return None

        row_counts = np.count_nonzero(mask, axis=1)
        thr = max(1.0, rw * col_min_ratio)
        rows = np.flatnonzero(row_counts >= thr)
        if rows.size == 0:
            rows = np.flatnonzero(row_counts > 0)
            if rows.size == 0:
                return None
        top_abs = y1 + int(rows[0])
        bottom_abs = y1 + int(rows[-1])

        band = mask[int(rows[0]):int(rows[-1]) + 1, :]
        band_area = float(max(1, band.shape[0] * band.shape[1]))
        density = float(cv2.countNonZero(band)) / band_area
        confidence = float(max(0.05, min(1.0, density * 1.3 + (0.20 if neon_px >= 4 else 0.0))))
        return top_abs, bottom_abs, cluster_px, confidence

    def scan(self, frame_bgr: np.ndarray, bx: int, by: int, bw: int, bh: int, is_vertical: bool) -> _GreenWindowResult:
        if frame_bgr is None or frame_bgr.size == 0 or bw <= 0 or bh <= 0:
            return _GreenWindowResult()

        H, W = frame_bgr.shape[:2]
        x1, y1 = max(0, bx), max(0, by)
        x2, y2 = min(W, bx + bw), min(H, by + bh)
        if x2 <= x1 or y2 <= y1:
            return _GreenWindowResult()

        roi = frame_bgr[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        scan_axis = rh if is_vertical else rw
        # When the ROI is very small (< 40 px on the scan axis) upscale 2x
        # so that a 2-3 pixel green mark becomes 4-6 px — much easier to mask.
        _upscale = 1
        if scan_axis > 0 and scan_axis < 40:
            _upscale = 2
            roi = cv2.resize(roi, (rw * 2, rh * 2), interpolation=cv2.INTER_LINEAR)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        neon_mask = cv2.inRange(hsv, self._NEON_GREEN_HSV_LO, self._NEON_GREEN_HSV_HI)
        gmask = cv2.inRange(hsv, self._GREEN_HSV_LO, self._GREEN_HSV_HI)
        ymask = cv2.inRange(hsv, self._YELLOW_HSV_LO, self._YELLOW_HSV_HI)
        dominance_mask = self._green_dominance_mask(roi)
        broad_mask = cv2.bitwise_or(gmask, ymask)
        neon_px = int(cv2.countNonZero(neon_mask))
        # Tiered green selection (was either/or, which dropped dim/desaturated
        # green windows entirely):
        #   strong neon  -> trust the tight band alone (rejects fill/HUD spill)
        #   weak neon     -> union neon with broad green so a dim/compressed
        #                    green mark still contributes its pixels
        #   no neon       -> broad green/yellow fallback
        if neon_px >= 6:
            mask = neon_mask
        elif neon_px >= 1:
            mask = cv2.bitwise_or(neon_mask, broad_mask)
        else:
            mask = broad_mask
        if int(cv2.countNonZero(dominance_mask)) >= 2:
            mask = cv2.bitwise_or(mask, dominance_mask)

        # Optional trained window override from launcher/CV training.
        trained_lo = None
        trained_hi = None
        if self._cfg is not None:
            trained_lo = self._cfg.trained_green_hsv_low
            trained_hi = self._cfg.trained_green_hsv_high
        if (
            isinstance(trained_lo, list) and isinstance(trained_hi, list)
            and len(trained_lo) == 3 and len(trained_hi) == 3
        ):
            try:
                lo = np.array(
                    [
                        max(0, min(179, int(trained_lo[0]))),
                        max(0, min(255, int(trained_lo[1]))),
                        max(0, min(255, int(trained_lo[2]))),
                    ],
                    dtype=np.uint8,
                )
                hi = np.array(
                    [
                        max(0, min(179, int(trained_hi[0]))),
                        max(0, min(255, int(trained_hi[1]))),
                        max(0, min(255, int(trained_hi[2]))),
                    ],
                    dtype=np.uint8,
                )
                if int(hi[0]) >= int(lo[0]) and int(hi[1]) >= int(lo[1]) and int(hi[2]) >= int(lo[2]):
                    tmask = cv2.inRange(hsv, lo, hi)
                    tcount = int(cv2.countNonZero(tmask))
                    # If trained mask has enough signal, prioritize it. Otherwise keep broad fallback.
                    if tcount >= 2:
                        mask = cv2.bitwise_or(mask, tmask)
            except Exception:
                pass
        _morph_k = 2 if scan_axis < 40 else 3
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_morph_k, _morph_k)),
            iterations=1,
        )

        cluster_px = int(cv2.countNonZero(mask))
        if cluster_px <= 0:
            return _GreenWindowResult(cluster_px=0)

        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        ph, pw = mask.shape[:2]
        if is_vertical:
            top_r, bottom_r = -1, -1
            if cnts:
                # Keep dominant contours and sort by Y to get precise top-edge target.
                contour_areas = [(float(cv2.contourArea(c)), c) for c in cnts]
                _min_area = 0.5 if scan_axis < 40 else 2.0
                contour_areas = [(a, c) for (a, c) in contour_areas if a >= _min_area]
                if contour_areas:
                    contour_areas.sort(key=lambda x: x[0], reverse=True)
                    max_area = contour_areas[0][0]
                    dominant = [c for (a, c) in contour_areas if a >= max(_min_area, max_area * 0.12)]
                    if not dominant:
                        dominant = [contour_areas[0][1]]
                    pts = np.concatenate([c.reshape(-1, 2) for c in dominant], axis=0)
                    ys = np.sort(pts[:, 1].astype(np.int32))
                    if ys.size > 0:
                        top_r = int(ys[0])
                        bottom_r = int(ys[-1])

            if top_r < 0 or bottom_r < 0:
                # Fallback to row profile if contour extraction fails.
                row_counts = np.count_nonzero(mask, axis=1)
                row_thr = max(1, int(pw * (0.08 if cluster_px <= max(8, pw) else 0.18)))
                rows = np.flatnonzero(row_counts >= row_thr)
                if rows.size == 0:
                    return _GreenWindowResult(cluster_px=cluster_px)
                top_r, bottom_r = int(rows[0]), int(rows[-1])

            center_r = int(round((top_r + bottom_r) * 0.5))
            # Use upscaled bh for pct conversion so pixel→% math stays correct.
            eff_bh = bh * _upscale
            sp = self._row_to_pct(bottom_r, eff_bh)
            ep = self._row_to_pct(top_r, eff_bh)
            width_pct = max(100.0 / max(1.0, float(eff_bh)), abs(float(ep) - float(sp)))
            span = max(1, int(bottom_r - top_r + 1))
            dominance_hits = int(cv2.countNonZero(dominance_mask[top_r:bottom_r + 1, :]))
            dominance_bonus = min(0.18, float(dominance_hits) / max(1.0, float(cluster_px)) * 0.18)
            conf = min(1.0, self._shape_confidence(mask, span, pw, cluster_px) + dominance_bonus)
            # Scale absolute row positions back to original frame coords.
            abs_top = by + int(round(top_r / _upscale))
            abs_bot = by + int(round(bottom_r / _upscale))
            abs_ctr = by + int(round(center_r / _upscale))
            return _GreenWindowResult(
                True,
                cluster_px,
                min(sp, ep),
                max(sp, ep),
                self._row_to_pct(center_r, eff_bh),
                width_pct,
                conf,
                abs_top,
                abs_bot,
                abs_ctr,
            )

        left_c, right_c = -1, -1
        if cnts:
            contour_areas = [(float(cv2.contourArea(c)), c) for c in cnts]
            _min_area_h = 0.5 if scan_axis < 40 else 2.0
            contour_areas = [(a, c) for (a, c) in contour_areas if a >= _min_area_h]
            if contour_areas:
                contour_areas.sort(key=lambda x: x[0], reverse=True)
                max_area = contour_areas[0][0]
                dominant = [c for (a, c) in contour_areas if a >= max(_min_area_h, max_area * 0.12)]
                if not dominant:
                    dominant = [contour_areas[0][1]]
                pts = np.concatenate([c.reshape(-1, 2) for c in dominant], axis=0)
                xs = np.sort(pts[:, 0].astype(np.int32))
                if xs.size > 0:
                    left_c = int(xs[0])
                    right_c = int(xs[-1])

        if left_c < 0 or right_c < 0:
            col_counts = np.count_nonzero(mask, axis=0)
            col_thr = max(1, int(ph * (0.08 if cluster_px <= max(8, ph) else 0.18)))
            cols = np.flatnonzero(col_counts >= col_thr)
            if cols.size == 0:
                return _GreenWindowResult(cluster_px=cluster_px)
            left_c, right_c = int(cols[0]), int(cols[-1])

        center_c = int(round((left_c + right_c) * 0.5))
        eff_bw = bw * _upscale
        sp = self._col_to_pct(left_c, eff_bw)
        ep = self._col_to_pct(right_c, eff_bw)
        width_pct = max(100.0 / max(1.0, float(eff_bw)), abs(float(ep) - float(sp)))
        span = max(1, int(right_c - left_c + 1))
        dominance_hits = int(cv2.countNonZero(dominance_mask[:, left_c:right_c + 1]))
        dominance_bonus = min(0.18, float(dominance_hits) / max(1.0, float(cluster_px)) * 0.18)
        conf = min(1.0, self._shape_confidence(mask, span, ph, cluster_px) + dominance_bonus)
        return _GreenWindowResult(True, cluster_px, min(sp, ep), max(sp, ep), self._col_to_pct(center_c, eff_bw), width_pct, conf, -1, -1, -1)


class _AutoCalibrator:
    _HSV_RANGES: Dict[str, Tuple[List[int], List[int]]] = {
        "Purple": ([140, 150, 150], [158, 255, 255]),
        "White": ([0, 0, 225], [179, 45, 255]),
        "Orange": ([8, 150, 150], [22, 255, 255]),
        "Yellow": ([25, 150, 150], [38, 255, 255]),
        "Red": ([0, 140, 140], [10, 255, 255]),
        "Green": ([50, 140, 130], [68, 255, 255]),
        "Cyan": ([90, 130, 140], [108, 255, 255]),
    }

    def scan(self, frame_bgr: np.ndarray, meter_color: str) -> Optional[Tuple[int, int, int, int]]:
        if frame_bgr is None or frame_bgr.size == 0:
            return None

        H, W = frame_bgr.shape[:2]
        y_start = int(H * 0.22)
        y_end = int(H * 0.95)
        if y_end <= y_start:
            return None
        roi = frame_bgr[y_start:y_end, :]
        hsv_range = self._HSV_RANGES.get(meter_color)
        if hsv_range is None:
            return None

        lo = np.array(hsv_range[0], dtype=np.uint8)
        hi = np.array(hsv_range[1], dtype=np.uint8)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, lo, hi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)

        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_area = 0.0
        best_bbox = None
        for cnt in cnts:
            area = float(cv2.contourArea(cnt))
            if area < 120.0:
                continue
            cx, cy, cw, ch = cv2.boundingRect(cnt)
            if cw <= 0:
                continue
            ar = float(ch) / float(cw)
            if ar < 1.5:
                continue
            if ch < int(H * 0.03) or ch > int(H * 0.32):
                continue
            if cw < int(W * 0.003) or cw > int(W * 0.06):
                continue
            abs_bottom = cy + ch + y_start
            if abs_bottom > int(H * 0.96):
                continue

            box = mask[cy:cy + ch, cx:cx + cw]
            box_area = float(max(1, cw * ch))
            density = float(cv2.countNonZero(box)) / box_area
            if density < 0.18:
                continue

            score = area * (0.7 + density)
            if score > best_area:
                best_area = score
                best_bbox = (cx, cy + y_start, cw, ch)
        return best_bbox


class _TemplateAnchor:
    """User-trained HUD-glyph template that LOCATES the meter at a fixed offset.

    A stable, shot-type-invariant HUD landmark (the user trains it via the launcher)
    is matched each frame with ``cv2.matchTemplate``; the meter sits at a trained
    pixel offset from the matched glyph. This LOCATES the meter region so the HSV
    scan can't false-lock a cosmetic blob elsewhere on screen. Location only — fill %
    and the green window are still read by the HSV/track pipeline inside the located
    region. Reimplemented from the public matchTemplate technique (no competitor code).

    ``locate()`` returns ``(meter_x, meter_y, meter_w, meter_h, score)`` in absolute
    frame coordinates, or ``None`` when disabled / not configured / below the match
    floor. Never raises — a failure degrades to ``None`` so detection falls back to
    the plain colour scan.
    """

    def __init__(self, cfg: DetectorConfig, base_dirs: Optional[List[str]] = None) -> None:
        self.enabled = False
        self.last_score = -1.0
        self._tmpl_gray: Optional[np.ndarray] = None
        self._band: Optional[Tuple[float, float, float, float]] = None
        self._offset: Optional[Tuple[int, int, int, int]] = None
        self._min_score = 0.62
        self._ref_wh: Optional[Tuple[int, int]] = None
        self.configure(cfg, base_dirs)

    @staticmethod
    def _resolve(path: str, base_dirs: Optional[List[str]]) -> Optional[str]:
        if os.path.isabs(path):
            return path if os.path.isfile(path) else None
        bases = list(base_dirs or [])
        bases.append(os.path.dirname(os.path.abspath(__file__)) or os.getcwd())
        bases.append(os.path.join(
            os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop", "NexusVision"))
        for b in bases:
            cand = os.path.join(b, path)
            if os.path.isfile(cand):
                return cand
        return None

    def configure(self, cfg: DetectorConfig, base_dirs: Optional[List[str]] = None) -> None:
        self.enabled = False
        self._tmpl_gray = None
        self._min_score = float(getattr(cfg, "template_anchor_min_score", 0.62))
        if not bool(getattr(cfg, "template_anchor_enabled", False)):
            return
        path = getattr(cfg, "template_anchor_path", None)
        off = getattr(cfg, "template_anchor_offset", None)
        if not path or not isinstance(off, (list, tuple)) or len(off) != 4:
            return
        resolved = self._resolve(str(path), base_dirs)
        if resolved is None:
            logger.warning("Template anchor enabled but template not found: %s", path)
            return
        img = None
        try:
            img = cv2.imread(resolved, cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None or img.size == 0:
            logger.warning("Template anchor image unreadable: %s", resolved)
            return
        try:
            self._tmpl_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            self._offset = (int(off[0]), int(off[1]), int(off[2]), int(off[3]))
        except Exception:
            self._tmpl_gray = None
            return
        band = getattr(cfg, "template_anchor_search_band", None)
        if isinstance(band, (list, tuple)) and len(band) == 4:
            try:
                self._band = (float(band[0]), float(band[1]), float(band[2]), float(band[3]))
            except Exception:
                self._band = None
        ref = getattr(cfg, "template_anchor_ref_wh", None)
        if isinstance(ref, (list, tuple)) and len(ref) == 2:
            try:
                rw, rh = int(ref[0]), int(ref[1])
                if rw > 0 and rh > 0:
                    self._ref_wh = (rw, rh)
            except Exception:
                self._ref_wh = None
        self.enabled = True
        logger.info("Template anchor armed (template %dx%d, offset %s, min_score %.2f)",
                    self._tmpl_gray.shape[1], self._tmpl_gray.shape[0], self._offset, self._min_score)

    def locate(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int, float]]:
        if not self.enabled or self._tmpl_gray is None or self._offset is None:
            return None
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        try:
            H, W = frame_bgr.shape[:2]
            sx = sy = 1.0
            if self._ref_wh is not None:
                rw, rh = self._ref_wh
                sx = float(W) / float(rw)
                sy = float(H) / float(rh)
            if self._band is not None:
                x0 = int(max(0, min(W - 1, round(self._band[0] * W))))
                y0 = int(max(0, min(H - 1, round(self._band[1] * H))))
                x1 = int(max(x0 + 1, min(W, round(self._band[2] * W))))
                y1 = int(max(y0 + 1, min(H, round(self._band[3] * H))))
            else:
                x0, y0, x1, y1 = 0, 0, W, H
            band = frame_bgr[y0:y1, x0:x1]
            if band.size == 0:
                return None
            tmpl = self._tmpl_gray
            if abs(sx - 1.0) > 0.02 or abs(sy - 1.0) > 0.02:
                tw = max(4, int(round(tmpl.shape[1] * sx)))
                th = max(4, int(round(tmpl.shape[0] * sy)))
                tmpl = cv2.resize(tmpl, (tw, th), interpolation=cv2.INTER_AREA)
            bh, bw = band.shape[:2]
            if tmpl.shape[0] >= bh or tmpl.shape[1] >= bw:
                return None
            band_gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            res = cv2.matchTemplate(band_gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _minv, maxv, _minl, maxl = cv2.minMaxLoc(res)
            self.last_score = float(maxv)
            if float(maxv) < self._min_score:
                return None
            ax = x0 + int(maxl[0])
            ay = y0 + int(maxl[1])
            dx, dy, mw, mh = self._offset
            mx = ax + int(round(dx * sx))
            my = ay + int(round(dy * sy))
            mw = max(1, int(round(mw * sx)))
            mh = max(1, int(round(mh * sy)))
            return (mx, my, mw, mh, float(maxv))
        except Exception:
            return None


class _ParkTracker:
    """Park (rec-mode) distractor-rejecting meter LOCATOR.

    The park is full of bright-red distractors — fire/pyro (the #1 confounder, it MIMICS the
    meter's rising fixed-bottom signature; only COLOUR separates them: meter = pure-red Sat~255,
    fire = orange Sat~141), red jerseys (MOVE), fixed HUD (shot-clock / score / stamina) — that
    the colour scan + stability latch false-lock on. This tracker confirms the REAL meter by the
    conjunction the distractors never all satisfy: a THIN PURE-RED column IN THE PLAY BAND that
    rises MONOTONICALLY from a FIXED bottom with low x-excursion and is not long-lived. It returns
    the confirmed meter REGION so the colour scan is constrained to it. Proven 100% recall /
    100% precision standalone (scratchpad/park_temporal_detect.py vs park_gt.csv). All px
    thresholds are tuned at a 720p reference and scaled by frame height -> resolution-independent.
    """
    _REF_W = 1280; _REF_H = 720   # park detection runs at a FIXED 1280x720 (== the proven standalone)
    _S_MIN = 180; _VAL_FLOOR = 150; _HUE_LO = 2; _HUE_HI = 176   # PURE red only (fire is orange -> excluded)
    _W_MIN = 3; _W_MAX = 34; _H_MIN = 4                          # thin bar
    _Y_TOP = 0.22; _Y_BOT = 0.85                                 # PLAY BAND mask crop (excludes top + bottom HUD)
    # _Y_BOT 0.68->0.85 (2026-07-05, framedump session 20260704_2108): a LOW fade meter rides at
    # y~0.75-0.82H (seq13: bottom ~890@1080) — the old 0.68 cropped it out of park entirely and
    # only the (edge-blind) YOLO path happened to cover it. 0.85 keeps the true bottom HUD
    # (scorebug/overlays at >0.85H) excluded; _BOT_MIN, the thin-bar shape gates and the
    # rise-confirm still reject in-band distractors.
    _BOT_MIN = 0.40                                              # the meter NOTCH sits over the shooter (lower court);
    #                                                             reject candidates whose bottom is high (top-corner HUD flashes)
    _X_LEFT = 0.20; _X_RIGHT = 0.85                              # HORIZONTAL play band CORE: the meter usually sits over the
    #                                                             camera-centred shooter. Rejects off-centre reds the rise+mono
    #                                                             confirm otherwise admits when they fade/scroll in — MyTEAM/menu
    #                                                             cards (left ~0.18W), wall NBA-logo / shot-clock (right).
    # 2026-07-02 EDGE ZONE: the "NEVER at the screen edges" claim was REFUTED by the real-park labels —
    # genuine FADE meters slide to cx≈0.107 (visually verified, tall h>0.08 with a green tip). The crop now
    # extends to the edge zone, but an edge candidate must be TALL (>= _X_EDGE_MIN_H of frame height) to
    # survive: the verified edge junk (scorebug h~0.026, menus/cutscene bits h<0.054) is short, real edge
    # fades are h~0.147. Core-band behaviour is byte-identical.
    _X_LEFT_EDGE = 0.08; _X_RIGHT_EDGE = 0.95                    # widened crop bounds (edge zone = between EDGE and core)
    _X_EDGE_MIN_H = 0.07                                         # min candidate height (frame fraction) in the edge zone
    # _X_EDGE_MIN_H 0.08->0.07 (2026-07-05, framedump session 20260704_2108 blind1): the real
    # mid-rise edge-fade meter measured h=54-55px@720 (0.0757H) and was rejected by 2-3px against
    # the 0.08 (57.6px) floor — the meter's red was HEALTHY (mean S 211-245; blur-desaturation
    # refuted on the same frames) and YOLO conf was 0 at the edge, so this gate alone blacked out
    # the shot until the engine blind-fired. 0.07 (50.4px) admits it with margin while every
    # verified edge distractor (scorebug h~0.026H, menu/cutscene bits h<0.054H) stays rejected.
    _X_TOL = 18; _BOTTOM_TOL = 12; _GAP_MAX = 5
    _BOTTOM_TOL_LOCKED = 20       # a LOCKED track may slide ~9px/frame on recede (banner translates with the
    #                               camera); a wider match tol keeps it alive so the box rides the recede
    #                               instead of popping out mid-shot. Acquisition still uses the strict _BOTTOM_TOL.
    # _MIN_FRAMES 5->2: the meter's distractor-unique conjunction (small-init + monotone rise from a near-fixed
    # bottom + thin pure-red column in the X/Y play band) is already satisfied at rise-frame 2; waiting for 5
    # only delayed the lock to the PEAK (the box then spanned the full meter). 5->2 locks at ~40% fill with
    # ZERO new false-locks across the full session (spatial+static gates do the distractor rejection, not a long rise).
    # _X_EXC_MAX 25->50: on a FADE the shooter (and the meter over them) translates ~6-8px/frame toward the edge,
    # so a 25px excursion cap over the 14-frame window wrongly rejected fade shots. The meter/jersey discriminator on
    # a fade is the monotone RISE + thin pure-red column + static-EMA (both translate), NOT low x-excursion.
    _MIN_FRAMES = 2; _WIN = 14; _BOTTOM_STD_MAX = 6.0; _RISE_MIN = 8; _MONO_MIN = 0.75; _X_EXC_MAX = 50
    _BOTTOM_RESID_MAX = 4.0       # drift-tolerant bottom gate: max std of the residual after a linear fit of the
    #                               bottom over the window (the bottom legitimately TRANSLATES ~5px/frame, so raw
    #                               std falsely grows with window length; the residual stays small for the meter)
    _LONG_LIVED = 150
    _INIT_H_MAX = 60             # RELAXED 22->60 (agent #3 measured: real-shot clips lock 2/8 -> 8/8 at ZERO idle
    #                              false-locks). The old 22 rejected 75% of real shots: a fast/fade meter's FIRST
    #                              detected frame is already 32-58px tall, AND a 1-frame track drop re-seeds the
    #                              track at the current tall height -> init_h doomed the rest of the shot. The
    #                              rise + static-red EMA gates already reject static reds (a static red can't rise),
    #                              so init_h is largely redundant; 60 keeps a loose guard against a big pre-existing blob.
    _CELL = 32; _BG_THRESH = 0.5; _BG_ALPHA = 0.01   # online static-red cell-EMA mask (fixed HUD / NBA logo / court)
    # PEAK-HOLD (P2). A track PEAKS once it has risen by >= _PEAK_RISE_MIN px (a genuine cold-rise to
    # the cap) and stopped climbing. While a peak was latched within _PEAK_HOLD_FRAMES ago, the lock
    # coasts on red-presence for _PEAK_HOLD_FRAMES contour-miss frames (vs the default _TPL_COAST) and
    # _confirm relaxes its RISE/MONO/INIT-H gates so a full, no-longer-rising meter can (re-)lock through
    # the cap+deflation. Bounded: ~_PEAK_HOLD_FRAMES @60fps ~= 0.4s / @30fps ~= 0.8s — long enough to
    # ride the 285ms-2.6s release without hallucinating a meter for seconds after the red truly vanishes.
    _PEAK_RISE_MIN = 16          # px rise (== _RISE_MIN * 2, the confidence "full-rise" scale) to latch a peak
    _PEAK_HOLD_FRAMES = 24
    # RC-3 post-peak WALL-CLOCK + CONTENT bound. Frame counts stall during a capture freeze (no frames
    # arrive), so a frame-based coast can't expire during exactly the stall it must survive-bound; the
    # committed red-presence coast then LATCHES a frozen/décor frame (frozen red satisfies red-presence
    # forever). Bound the peaked coast to _PEAK_HOLD_S seconds of wall-clock AND require the tracked ROI
    # content to CHANGE — _PEAK_STATIC_MAX consecutive byte-identical ROIs (a frozen frame / static décor)
    # decays the track. Both gates are only active when a real ts is supplied (live + replay); ts=None
    # (legacy callers/tests) preserves the pure frame-count behaviour.
    _PEAK_HOLD_S = 0.6
    _PEAK_STATIC_MAX = 2

    class _Tk:
        # cx/bottom/w/top are SMOOTHED (EMA) state used to draw the box; hist keeps the RAW per-frame samples
        # for _confirm. Emitting from the EMA state (not the raw last contour) kills the box drift: the thin
        # 720p bar contour shimmers ~5px every frame and spikes ~50px on a 1-frame contour split.
        # c = [cx, top(y), bottom(y+h), w, h].
        # patch/tplw/vx/vy/miss/tlast = the LOCK-THEN-TRACK state (agent #1 measured matchTemplate at 0.12px MAE
        # vs the contour+EMA's 5.24px). Once locked we grab a gray NOTCH patch and matchTemplate-track it each
        # frame (precise, spike-immune, no cap), velocity-coasting through brief contour dropouts.
        __slots__ = ("cx", "cx_emit", "bottom", "w", "top", "hist", "last", "locked",
                     "patch", "tplw", "vx", "vy", "miss", "tlast", "clear", "peaked", "h_max",
                     "peak_hold_fi", "peak_ts", "roi_hash", "static_run")
        def __init__(self, fi, c):
            self.cx = float(c[0]); self.cx_emit = float(c[0])
            self.bottom = float(c[2]); self.w = float(c[3]); self.top = float(c[1])
            self.hist = [(fi, c[0], c[1], c[2], c[4])]; self.last = fi; self.locked = False
            self.patch = None; self.tplw = 0; self.vx = 0.0; self.vy = 0.0; self.miss = 0; self.tlast = fi
            self.clear = (int(c[5]) if len(c) > 5 else 0)   # this frame's candidate is a FRESH (static-EMA-clear) red
            # PEAK-HOLD (P2): a locked track that has RISEN to (near-)full then stopped climbing has
            # PEAKED — the shot is at the cap and about to release/deflate. Once latched, the lock is
            # held on red-presence through the whole cap->deflation (the rise gate can no longer be met,
            # so without this the lock drops at the tip and re-lock is impossible for the entire release).
            self.peaked = False
            self.h_max = float(c[4])
            # PER-TRACK peak-hold frame (P2). Was a SHARED instance scalar, which let ANY peaking
            # track hold-open the re-lock gate for a brand-new static-red blob (cross-track leak).
            # Stale -10**9 on a fresh _Tk forces it to prove its own rise before it can peak-hold.
            self.peak_hold_fi = -10 ** 9
            # RC-3 post-peak bound: wall-clock the moment this track PEAKED (-1 = not yet), and a
            # rolling content hash of the tracked ROI + a run-length of BYTE-IDENTICAL frames. A
            # peaked track may only coast on red-presence while the clock is within _PEAK_HOLD_S AND
            # its ROI keeps CHANGING -> a frozen capture duplicate / static court décor (identical
            # ROI, or past the wall-clock bound) decays instead of latching the frozen frame forever.
            self.peak_ts = -1.0
            self.roi_hash = None
            self.static_run = 0
        def match(self, c, xtol, btol):
            return abs(c[0] - self.cx) < xtol and abs(c[2] - self.bottom) < btol
        def add(self, fi, c):
            self.clear = (int(c[5]) if len(c) > 5 else 0)
            # MATCHING centre: responsive 0.5 EMA. This MUST stay responsive — match tol is only 18px, so a heavy
            # EMA lags a fade/recede slide (~9px/frame) past 18px and DROPS the locked track. Used by match()/_confirm.
            self.cx = 0.5 * self.cx + 0.5 * c[0]
            # DISPLAY centre (drawn box only): follow the already-smoothed matching cx but CAP the per-frame move at
            # 8px so a ~50px 1-frame contour-SPLIT spike can't stride the box. Matching is untouched (no detection
            # regression) — this just keeps the drawn box glued. The emit reads cx_emit, not cx.
            self.cx_emit += max(-8.0, min(8.0, self.cx - self.cx_emit))
            self.bottom = 0.5 * self.bottom + 0.5 * c[2]
            self.w = 0.65 * self.w + 0.35 * c[3]
            # top rises ~20px/frame -> ASYMMETRIC EMA: snap UP fast so the box hugs the live fill with no lag,
            # but damp downward/noise moves so recede + the +50px contour-split spikes don't make it jitter.
            nt = float(c[1])
            self.top = (0.25 * self.top + 0.75 * nt) if nt < self.top else (0.6 * self.top + 0.4 * nt)
            self.hist.append((fi, c[0], c[1], c[2], c[4])); self.last = fi
            self.h_max = max(self.h_max, float(c[4]))

    def __init__(self):
        self.reset()

    def reset(self):
        self._tracks = []
        self._fi = 0
        self._bg_ema = None       # per-cell red-frequency EMA (online static-red mask)
        self.last_confidence = 0.0   # derived per-emit confidence (see update(); replaces the 0.95 hard-code)
        # (P2 peak-hold fi is now PER-TRACK on _Tk.peak_hold_fi -- a shared scalar leaked the hold
        # across tracks, letting a new static-red blob skip the rise gates while any track peaked.)

    @staticmethod
    def _red_mask(bgr, hue_lo, hue_hi, s_min, v_floor):
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        return (((Hh <= hue_lo) | (Hh >= hue_hi)) & (Ss >= s_min) & (Vv >= v_floor)).astype(np.uint8)

    def _find(self, frame, s):
        H, W = frame.shape[:2]
        # PERF: run the heavy cvtColor + red mask + static-red EMA on the PLAY-BAND CROP ONLY (rows Y_TOP..Y_BOT,
        # cols X_LEFT..X_RIGHT = ~30% of the 1280x720 pixels) instead of the whole frame. _find used to mask the
        # full frame then ZERO everything outside this exact band, so cropping FIRST is bit-identical inside the
        # band (zero accuracy loss) but cuts _find ~7.7ms -> ~1.7ms — the alloc-heavy full-frame numpy was the
        # source of the 85ms detect spike under live CPU contention (and thus the 60fps capture dips).
        cy0 = max(0, int(H * self._Y_TOP)); cy1 = min(H, int(H * self._Y_BOT))
        cx0 = max(0, int(W * self._X_LEFT_EDGE)); cx1 = min(W, int(W * self._X_RIGHT_EDGE))
        if cy1 <= cy0 or cx1 <= cx0:
            return []
        crop = frame[cy0:cy1, cx0:cx1]
        ch, cw = crop.shape[:2]
        red = self._red_mask(crop, self._HUE_LO, self._HUE_HI, self._S_MIN, self._VAL_FLOOR)
        # ONLINE STATIC-RED suppression: a per-cell red-frequency EMA over the band. Cells red across MANY frames
        # (fixed HUD / shot-clock / court paint in-band) are blocked; the transient meter (red ~24 frames/shot at
        # a per-shot position) stays under the threshold at alpha=0.01 (~100-frame memory). The tighter _confirm
        # is the primary static rejecter; this is belt-and-suspenders (~1s warmup before fixed reds cross).
        cols = max(1, cw // self._CELL); rows = max(1, ch // self._CELL)
        present = (cv2.resize((red > 0).astype(np.float32), (cols, rows), interpolation=cv2.INTER_AREA) > 0.04).astype(np.float32)
        # SELF-POISONING GUARD (2026-07-03 MyCourt): a shooter who fires from the SAME spot pins the
        # meter to the same cells every shot — the EMA crossed the 0.10 "clear" cutoff within 1-2
        # shots (killing instant-acquire) and headed for active suppression. A LOCKED track is a
        # confirmed meter, so its own cells (±1 around the column, over its vertical span) must not
        # TEACH the EMA "static red here": zero their `present` so those cells DECAY instead. True
        # static reds (HUD/wallpaper) never lock (rise/clear gates) and keep accumulating as before.
        for _t in self._tracks:
            if not _t.locked or not _t.hist:
                continue
            _, _tcx, _ttop, _tbot, _th = _t.hist[-1]
            tc = int((_tcx - cx0) // self._CELL)
            r0 = int((_ttop - cy0) // self._CELL); r1 = int((_tbot - cy0) // self._CELL)
            present[max(0, r0 - 1):min(rows, r1 + 2), max(0, tc - 1):min(cols, tc + 2)] = 0.0
        if self._bg_ema is None or self._bg_ema.shape != present.shape:
            self._bg_ema = present.copy()
        else:
            self._bg_ema = (1.0 - self._BG_ALPHA) * self._bg_ema + self._BG_ALPHA * present
        if self._fi > self._MIN_FRAMES:
            blocked = (self._bg_ema > self._BG_THRESH).astype(np.uint8)
            if int(blocked.sum()) > 0:
                red[cv2.resize(blocked, (cw, ch), interpolation=cv2.INTER_NEAREST) > 0] = 0
        red = cv2.morphologyEx(red * 255, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        wmin = max(2, int(round(self._W_MIN * s))); wmax = int(round(self._W_MAX * s)); hmin = max(2, int(round(self._H_MIN * s)))
        thin_h = int(round(9 * s))
        bot_min = int(H * self._BOT_MIN)
        out = []
        eh, ew = (self._bg_ema.shape if self._bg_ema is not None else (1, 1))
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)   # crop coords
            # static-red EMA at this candidate's cell (crop grid): < 0.10 = RARELY red here = a FRESH red
            # appearance (= the meter), not a persistent HUD/court blob -> eligible for INSTANT frame-1 acquire.
            clear = 1
            if self._bg_ema is not None:
                ccol = min(ew - 1, (x + w // 2) // self._CELL); crow = min(eh - 1, (y + h // 2) // self._CELL)
                clear = 1 if float(self._bg_ema[crow, ccol]) < 0.10 else 0
            x += cx0; y += cy0    # crop -> full-frame coords
            if w < wmin or w > wmax or h < hmin:
                continue
            if (y + h) < bot_min:   # notch too high -> a top-corner HUD flash, not the shooter's meter
                continue
            cx = x + w // 2
            if cx < int(W * self._X_LEFT_EDGE) or cx > int(W * self._X_RIGHT_EDGE):
                continue   # outside even the widened band
            if (cx < int(W * self._X_LEFT) or cx > int(W * self._X_RIGHT)) and h < int(H * self._X_EDGE_MIN_H):
                continue   # EDGE ZONE: only a TALL bar (a real slid-out fade meter) may live here; short
                #            edge reds (scorebug/menu/cutscene bits) stay rejected exactly as before
            if h >= thin_h and h / float(max(1, w)) < 0.8:   # tall blob, not thin -> not a meter column
                continue
            out.append([cx, y, y + h, w, h, clear])
        return out

    def _confirm(self, t, s):
        if len(t.hist) < self._MIN_FRAMES:
            return False
        if t.hist[-1][0] - t.hist[0][0] > self._LONG_LIVED:   # persistent distractor
            return False
        # PEAK-HOLD re-lock (P2): while a peak was latched within _PEAK_HOLD_FRAMES ago, the meter is at
        # the cap / deflating and the RISE + INIT-H gates below can no longer be met (a full meter neither
        # rises nor appears small). Without this a lock that drops during the release could never re-lock
        # and the meter went missing the whole 285ms-2.6s deflation. Hold on RED-PRESENCE only: the notch
        # position + column geometry still had to pass the drift/excursion gates above, so this only skips
        # the RISE-shape gates, never the spatial ones.
        peak_hold = (self._fi - t.peak_hold_fi) <= self._PEAK_HOLD_FRAMES
        # INITIAL-HEIGHT gate: a real meter APPEARS small and grows from ~0; a track already tall on its
        # FIRST frame is a pre-existing static red (NBA logo / HUD / court paint), never the meter.
        if not peak_hold and t.hist[0][4] > self._INIT_H_MAX * s:
            return False
        rec = t.hist[-self._WIN:]
        cxs = [c for (_, c, _, _, _) in rec]
        if (max(cxs) - min(cxs)) > self._X_EXC_MAX * s:       # moving (jersey/player) -> not the meter
            return False
        bots = [b for (_, _, _, b, _) in rec]; hs = [h for (_, _, _, _, h) in rec]
        # DRIFT-TOLERANT bottom gate. The meter notch TRANSLATES steadily (~5px/frame as the camera pans with
        # the shooter), so raw std grows with the window and falsely fails one frame after the lock (the old
        # fragile short-lived lock). Fit a line to the bottom over the window and gate on the RESIDUAL std: the
        # meter's bottom is linear (small residual) while a jersey/player/HUD bottom wanders (large residual).
        if len(bots) >= 3:
            _idx = np.arange(len(bots), dtype=np.float64)
            _coef = np.polyfit(_idx, np.asarray(bots, dtype=np.float64), 1)
            if np.std(np.asarray(bots, dtype=np.float64) - np.polyval(_coef, _idx)) > self._BOTTOM_RESID_MAX * s:
                return False
        elif np.std(bots) > self._BOTTOM_STD_MAX * s:         # <3 pts: line fit is trivial, fall back to raw std
            return False
        if not peak_hold:
            if hs[-1] - hs[0] < self._RISE_MIN * s:               # FULL-SPAN sustained rise (not a late blip vs min)
                return False
            deltas = [hs[i + 1] - hs[i] for i in range(len(hs) - 1)]
            if sum(1 for d in deltas if d >= -1) / float(max(1, len(deltas))) < self._MONO_MIN:   # mostly-monotonic rise
                return False
        return True

    # ---- LOCK-THEN-TRACK (matchTemplate notch tracker; agent #1 measured 0.12px MAE @ 0.095ms) -------------
    _TPL_WIN = 30          # +/- search window around the predicted notch (px @720p)
    _TPL_SCORE = 0.55      # matchTemplate TM_CCOEFF_NORMED loss threshold (visible 0.74-1.0, gone <=0.24)
    _TPL_COAST = 8         # max consecutive CONTOUR-miss frames a locked track velocity-coasts (bridges) before
    #                      (5->8 2026-07-03: a MyCourt zoom kills contour+template together; 5 frames at ~30fps
    #                      was too short to ride out the zoom transition)
    #                        it is dropped (red gone -> meter gone). Agent-validated at 5: kills the 77s stuck
    #                        lock (max age 434->23 frames) while preserving all real acquisitions incl. fast fades.
    _FILL_SCAN_FRAC = 0.32 # how far above the notch to scan the red column for the fill top

    def _grab_patch(self, t, gray):
        """Capture a gray NOTCH patch (the appearance-stable bottom edge + lower fill) to track by correlation."""
        H, W = gray.shape[:2]
        cx = int(round(t.cx)); b = int(round(t.bottom)); w = int(round(t.w))
        half = max(16, w // 2 + 8)
        x0 = max(0, cx - half); x1 = min(W, cx + half)
        y0 = max(0, b - 12); y1 = min(H, b + 5)
        if (x1 - x0) >= 12 and (y1 - y0) >= 8:
            t.patch = gray[y0:y1, x0:x1].copy(); t.tplw = t.patch.shape[1]

    def _track_locked(self, t, gray) -> float:
        """Refine the HORIZONTAL box position of a LOCKED meter by matchTemplate around the velocity-predicted
        notch (kills the camera-pan stride: the contour cx_emit cap can't keep up with the ~9-13px/frame slide).
        POSITION ONLY — survival is gated on the contour/red presence by the caller (the gray patch over-matches
        the static court, so it must NEVER decide loss). Returns the match score so the caller can re-grab the
        patch when it goes stale (a ZOOM rescales the meter -> the fixed patch stops matching)."""
        if t.patch is None:
            return -1.0
        H, W = gray.shape[:2]
        ph, pw = t.patch.shape[:2]
        pcx = t.cx_emit + t.vx; pb = t.bottom
        win = self._TPL_WIN
        x0 = int(max(0, pcx - pw / 2.0 - win)); x1 = int(min(W, pcx + pw / 2.0 + win))
        y0 = int(max(0, pb - 12 - win)); y1 = int(min(H, pb + 5 + win))
        score = -1.0; mcx = pcx
        if (x1 - x0) >= pw and (y1 - y0) >= ph:
            res = cv2.matchTemplate(gray[y0:y1, x0:x1], t.patch, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            mcx = x0 + loc[0] + pw / 2.0
        if score >= self._TPL_SCORE:
            t.vx = 0.6 * t.vx + 0.4 * (mcx - t.cx_emit)
            t.cx_emit = mcx
        else:
            # matchTemplate unreliable (a ZOOM rescaled the meter so the fixed patch no longer matches) -> fall
            # back to the SCALE-ADAPTIVE contour centre t.cx (the _find candidate the track matched, already
            # smoothed), NOT a velocity-coast which drifts off. Keeps the box glued through camera zoom.
            t.cx_emit = t.cx
        return score

    def _roi_hash(self, work, t) -> Optional[int]:
        """RC-3: a cheap content hash of the tracked meter ROI in the 720p work frame. Byte-identical
        across frames == a frozen capture duplicate or static décor (not a live rising/deflating meter).
        Returns None on a degenerate/out-of-bounds ROI (treated as 'changed')."""
        try:
            H, W = work.shape[:2]
            cx = int(round(t.cx_emit)); w = int(round(max(2.0, t.w)))
            b = int(round(t.bottom)); tp = int(round(t.top))
            x0 = max(0, cx - w); x1 = min(W, cx + w)
            y0 = max(0, tp - 4); y1 = min(H, b + 4)
            if (x1 - x0) < 4 or (y1 - y0) < 4:
                return None
            return hash(work[y0:y1, x0:x1].tobytes())
        except Exception:
            return None

    def update(self, frame_bgr, ts: Optional[float] = None) -> Optional[Tuple[int, int, int, int]]:
        """Confirm + return the RED-FILL rect (x, y, w, h) in NATIVE-frame px this frame, or None.

        Detection runs at a FIXED 1280x720 (== the proven standalone) so the morphology/contour
        behaviour is identical regardless of the live capture resolution; the rect is scaled back to
        the native frame. The fill rect is emitted DIRECTLY (the caller skips the colour re-scan).

        ts (perf_counter seconds) enables the RC-3 post-peak wall-clock + content-hash bound; ts=None
        keeps the legacy pure frame-count coast."""
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        Hn, Wn = frame_bgr.shape[:2]
        work = frame_bgr if (Wn, Hn) == (self._REF_W, self._REF_H) else cv2.resize(
            frame_bgr, (self._REF_W, self._REF_H), interpolation=cv2.INTER_AREA)
        s = 1.0                                        # work frame is the 720p reference
        self._fi += 1; fi = self._fi
        cands = self._find(work, s)
        xtol = self._X_TOL * s
        used = set()
        for t in self._tracks:
            tbtol = (self._BOTTOM_TOL_LOCKED if t.locked else self._BOTTOM_TOL) * s
            for j, c in enumerate(cands):
                if j not in used and t.match(c, xtol, tbtol):
                    t.add(fi, c); used.add(j); break       # contour drives the notch + FILL (proven path)
        for j, c in enumerate(cands):
            if j not in used:
                self._tracks.append(self._Tk(fi, c))
        self._tracks = [t for t in self._tracks if fi - t.last <= self._GAP_MAX]
        gray = None
        survivors = []
        best = None
        for t in self._tracks:
            contour_seen = (t.last == fi)               # the contour found this track's RED column this frame
            # INSTANT acquire: a candidate whose static-EMA cell is CLEAR (t.clear, a fresh red appearance = the
            # meter, not a persistent blob) locks on FRAME 1 — no multi-frame rise wait, so the box appears the
            # instant the meter shows even if it's briefly held static. The rise-based _confirm stays the fallback.
            # The contour/red-presence survival gate drops a transient that doesn't sustain, so this can't stick.
            if not t.locked and contour_seen and (t.clear or self._confirm(t, s)):
                t.locked = True
                gray = gray if gray is not None else cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
                self._grab_patch(t, gray)               # grab the notch template on lock
            # PEAK-HOLD latch (P2): a locked track that has RISEN by >= _PEAK_RISE_MIN and stopped
            # climbing (current height off its own max) is at the cap. Latch it so the coast + re-lock
            # relax below survive the release; refresh THIS track's peak_hold_fi while at/near its peak.
            if t.locked and (t.h_max - t.hist[0][4]) >= self._PEAK_RISE_MIN * s:
                if not t.peaked and t.hist[-1][4] <= t.h_max - max(2.0, 0.05 * t.h_max):
                    t.peaked = True
                    if ts is not None and t.peak_ts < 0.0:
                        t.peak_ts = float(ts)     # RC-3: stamp the wall-clock at first peak
                if t.peaked or t.hist[-1][4] >= 0.85 * t.h_max:
                    t.peak_hold_fi = fi
            keep = True
            if t.locked and t.patch is not None:
                gray = gray if gray is not None else cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
                tpl_score = self._track_locked(t, gray)  # matchTemplate refines cx_emit (POSITION only)
                # SURVIVAL = red presence (the contour), NOT the matchTemplate score. The gray notch patch
                # over-matches the static court, so keying survival off it left a lock stuck on background for 77s.
                # While the contour sees the red column the lock is healthy; matchTemplate only BRIDGES a short
                # (<=_TPL_COAST) contour gap, then the lock DROPS -> reacquire next shot.
                if contour_seen:
                    t.miss = 0
                    if tpl_score < self._TPL_SCORE:
                        self._grab_patch(t, gray)        # template stale (zoom rescaled it) but contour confirms
                        #                                  the position -> re-grab at the new scale so it re-glues
                else:
                    t.miss += 1
                    # A PEAKED track coasts longer (the cap+deflation gaps the thin-column contour as the
                    # green cap VFX erupts over the red): bridge up to _PEAK_HOLD_FRAMES instead of _TPL_COAST.
                    coast_lim = self._PEAK_HOLD_FRAMES if t.peaked else self._TPL_COAST
                    if t.miss <= coast_lim:
                        t.last = fi                     # bridge a brief contour dropout (position velocity-coasted)
                    else:
                        keep = False                    # red gone for > coast frames -> meter gone -> drop the lock
            # RC-3 post-peak bound (wall-clock + content). A PEAKED track holds on red-presence through the
            # cap->deflation, but a frozen capture DUPLICATE or static court décor keeps red-presence forever
            # (contour_seen stays true) and LATCHES the frozen frame — the exact failure the frame-count coast
            # can't catch. Decay a peaked track when its ROI content goes byte-identical for _PEAK_STATIC_MAX
            # frames (frozen/static signature), or when a COASTING (no fresh contour) track rides past
            # _PEAK_HOLD_S wall-clock since the peak. Live meters change content / show fresh contour, so this
            # never drops a genuine in-flight meter. Only active with a real ts (live/replay).
            if keep and t.locked and t.peaked and ts is not None:
                _rh = self._roi_hash(work, t)
                if _rh is not None and _rh == t.roi_hash:
                    t.static_run += 1
                else:
                    t.static_run = 0
                t.roi_hash = _rh
                if t.static_run >= self._PEAK_STATIC_MAX:
                    keep = False
                elif t.peak_ts >= 0.0 and (float(ts) - t.peak_ts) > self._PEAK_HOLD_S and not contour_seen:
                    keep = False
            if not keep:
                continue
            survivors.append(t)
            if t.locked and t.last == fi and (best is None or t.hist[-1][4] > best.hist[-1][4]):
                best = t
        self._tracks = survivors
        if best is None:
            self.last_confidence = 0.0
            return None
        # DERIVED confidence (2026-07-02, kills the 0.95 hard-code that DISABLED the validator's
        # confidence gates live — every park emit read as maximally trustworthy, so a weak/static
        # latch could never be conf-gated). Built from the track's own evidence: a sustained RISING
        # fill is the meter's signature (a static wallpaper/HUD blob holds ~0.5, a genuine rise ~0.9),
        # aged tracks and static-EMA-clear acquires add trust, template-coasting subtracts.
        rec = best.hist[-self._WIN:]
        hs = [h for (_, _, _, _, h) in rec]
        rise_px = (hs[-1] - hs[0]) if len(hs) >= 2 else 0.0
        rise_norm = max(0.0, min(1.0, rise_px / max(1.0, self._RISE_MIN * s * 2.0)))
        deltas = [hs[i + 1] - hs[i] for i in range(len(hs) - 1)]
        mono = (sum(1 for d in deltas if d >= -1) / float(len(deltas))) if deltas else 0.0
        age_norm = min(1.0, len(best.hist) / 8.0)
        coast = min(1.0, best.miss / float(max(1, self._TPL_COAST)))
        conf = (0.30 + 0.35 * rise_norm + 0.12 * age_norm
                + (0.08 if best.clear else 0.0) + 0.08 * mono * rise_norm - 0.15 * coast)
        self.last_confidence = float(max(0.20, min(0.98, conf)))
        # the RED-FILL rect (the column) in 720p coords -> scale to the native frame. cx_emit is the
        # matchTemplate-tracked horizontal centre (0.12px, no cap/drift through the camera-pan slide); top is the
        # contour fill top (proven fill path), bottom the contour notch, height = bottom - top.
        w720 = max(1.0, best.w)
        x720 = best.cx_emit - w720 / 2.0   # matchTemplate horizontal centre (no cap); fill/notch from the contour
        top720 = best.top
        h720 = max(1.0, best.bottom - best.top)
        sx = Wn / float(self._REF_W); sy = Hn / float(self._REF_H)
        rx = int(round(max(0.0, x720) * sx)); ry = int(round(max(0.0, top720) * sy))
        rw = max(1, int(round(w720 * sx))); rh = max(1, int(round(h720 * sy)))
        rw = min(rw, Wn - rx); rh = min(rh, Hn - ry)
        if rw <= 0 or rh <= 0:
            return None
        return (rx, ry, rw, rh)


class MeterDetector:
    _JSON_W = 1920
    _JSON_H = 1080

    def __init__(self, styles_dir: Union[str, List[str], Tuple[str, ...]], cfg: Optional[DetectorConfig] = None) -> None:
        if cfg is None:
            cfg = load_detector_config()
        self._cfg = cfg
        self._stability = _StabilityValidator(cfg)
        self._motion = _MotionEstimator(cfg)
        # Shorter miss-timeout (8 vs 16 frames) so a lock left over a transient
        # blob clears quickly when the real meter isn't there — reduces idle
        # false-positive self-reinforcement. In-shot blips are bridged by the
        # engine's freshness window, so this doesn't drop real shots.
        self._roi_lock = DynamicROILock(timeout_frames=8)
        self._green = _GreenWindowScanner(cfg)
        # Trained template anchor (meter LOCATION). Disabled until a template is
        # trained; when armed it constrains the colour scan to the located region.
        self._anchor = _TemplateAnchor(cfg)
        # PARK temporal meter locator (distractor rejection). When park_temporal_enabled,
        # it confirms the real rising meter and emits its red-fill rect directly.
        self._park = _ParkTracker()
        # A1 rise-state / spent-carryover preemption. Tracks the served meter's fill trajectory so
        # detect() can tell a still-RISING live meter from a SPENT one (the previous shot's meter
        # lingering at ~full/deflating), which is the trigger to stop the carryover blocking the
        # next shot's acquisition. Reset per served episode (gap or position jump).
        self.last_rise_state: str = ""
        self._served_peak_fill = 0.0
        self._rise_noninc = 0                 # consecutive served frames with non-increasing fill
        self._rise_prev_fill = -1.0
        self._last_served_bbox: Optional[Tuple[int, int, int, int]] = None
        self._last_served_ts = 0.0
        # Trained YOLO meter LOCATOR (reject-random-objects). Flag-gated (ORION_METER_LOCATOR=1) +
        # model-optional -> None/inert when the model or flag is absent, so classical detection is
        # byte-for-byte unchanged. When present it is AUTHORITATIVE in detect(): it finds the meter
        # ANYWHERE (player-attached, variable position) and constrains the colour scan to its box.
        self._locator = None
        try:
            from meter_locator_infer import try_load as _load_meter_locator
            self._locator = _load_meter_locator()
        except Exception:
            self._locator = None
        # Track T: gate every locator-era stickiness fix in _StabilityValidator on the locator being
        # active, so the classical PARK path (locator OFF) validates byte-identically. Set once here.
        self._stability._locator_mode = self._locator is not None
        # Sub-pixel meter READER (model A). Flag-gated (ORION_METER_READER=1) + model-optional -> None
        # /inert when absent, so detection is byte-for-byte unchanged. When present it refines the
        # located-crop fill% to continuous sub-pixel (kills the ~1-2% row-count quantization).
        self._reader = None
        try:
            from meter_reader_infer import try_load as _load_meter_reader
            self._reader = _load_meter_reader()
        except Exception:
            self._reader = None
        self._red_bounds_cache: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        # Auto meter-colour lock state. Until a colour wins enough
        # CONSECUTIVE frames the detector scans all candidates each frame; once locked
        # it uses that single colour for the session (cleared on reload_config).
        self._auto_color_locked: Optional[str] = None
        self._auto_color_cand: Optional[str] = None
        self._auto_color_streak: int = 0
        # SEED the auto-colour lock with the configured production colour. The
        # every-frame candidate wide-scan was OVERRIDING a correctly-configured
        # colour and thrashing — offline framedump (real RED meter): Red+auto = 15%
        # on-meter detection with ~543 false locks, vs Red pinned = 59% clean. So
        # pin the configured colour from frame 1; auto stays opt-in for genuinely
        # unknown colours (no colour configured) without second-guessing a known-good
        # one. _seed_auto_color_lock() is re-run on reload_config (live colour change).
        self._seed_auto_color_lock()
        self._auto_cal = _AutoCalibrator()
        self._cuda = _CudaMaskBackend(getattr(cfg, "cuda_enabled", True))
        self._last_result: Optional[DetectResult] = None
        self._meter_memory_left = 0
        self._last_green: Optional[_GreenWindowResult] = None
        self._green_memory_left = 0
        # Per-frame gate diagnostics, rebuilt by every detect() call (see the
        # assembly at the end of detect()). Consumed by the orchestrator's
        # detframes.csv columns; scalars only, cheap at frame rate.
        self._last_scan: Dict[str, Any] = {}
        self.last_debug: Dict[str, Any] = {}
        # Track model: the meter is size-stable for a session, so the green
        # window's offset from the fill bottom anchor and the full track height
        # can be remembered across frames (and shots) and only invalidated on a
        # style/colour change (_reset). _track_green_left guards against a stale
        # offset if green is not re-seen for a while.
        self._track_green_off: Optional[Tuple[int, int]] = None
        self._track_full_h: float = 0.0
        self._track_full_h_w: float = 0.0   # candidate width at learn time (zoom-staleness proxy)
        self._track_green_left: int = 0
        # MeterBoxKalman (ORION_METER_TRACK=1): persistent constant-velocity filter over the SERVED
        # meter geometry (centre-x, notch, width, full-height). Replaces raw per-frame contour
        # geometry at the emit point so the box can't drift/jitter/collapse on a one-frame outlier,
        # and stabilizes the fill-denominator memories against width blips. None when the flag is
        # off -> never imported -> classical path byte-identical.
        self._box_track = None
        try:
            if os.environ.get("ORION_METER_TRACK", "0") == "1":
                from meter_box_kalman import try_load as _load_box_track
                self._box_track = _load_box_track()
        except Exception:
            self._box_track = None
        # POST-RELEASE TRACK-HOLD source (2026-07-04 batch #2): last LADDER-found match (template/
        # bands) + the wall-clock window inside which a warm track may pixel-confirm-and-serve the
        # static carryover bar that the rising-fill cold-acquisition latch (correctly) refuses.
        self._bt_hold_match = None
        self._bt_hold_until = 0.0
        try:
            self._bt_hold_s = max(0.0, float(os.environ.get("ORION_METER_TRACK_HOLD_S", "3.0")))
        except Exception:
            self._bt_hold_s = 3.0

        self._styles: List[Dict[str, Any]] = []
        if isinstance(styles_dir, str):
            dirs = [styles_dir] if os.path.isdir(styles_dir) else []
        elif isinstance(styles_dir, (list, tuple)):
            dirs = [d for d in styles_dir if isinstance(d, str) and os.path.isdir(d)]
        else:
            dirs = []

        seen: set = set()
        for d in dirs:
            real = os.path.realpath(d)
            if real in seen:
                continue
            seen.add(real)
            for name in sorted(os.listdir(d)):
                if not name.lower().endswith(".json"):
                    continue
                path = os.path.join(d, name)
                try:
                    with open(path, "r", encoding="utf-8-sig") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        self._styles.append(raw)
                except Exception:
                    continue

        have_styles = {_norm_style_name(s.get("style", "")) for s in self._styles}
        for style_name in ("Pill", "Straight", "Sword", "Dial", "Arrow", "Arrow2"):
            if _norm_style_name(style_name) not in have_styles:
                self._styles.append(_builtin_style(style_name))

        logger.info("MeterDetector loaded %d style(s): %s", len(self._styles), [s.get("style", "?") for s in self._styles])
        self._active: Optional[str] = None
        self._reset()

    def _reset(self) -> None:
        self._stability.reset()
        self._motion.reset()
        self._park.reset()
        # Shorter miss-timeout (8 vs 16 frames) so a lock left over a transient
        # blob clears quickly when the real meter isn't there — reduces idle
        # false-positive self-reinforcement. In-shot blips are bridged by the
        # engine's freshness window, so this doesn't drop real shots.
        self._roi_lock = DynamicROILock(timeout_frames=8)
        self._last_result = None
        self._meter_memory_left = 0
        self._last_green = None
        self._green_memory_left = 0
        self._track_green_off = None
        self._track_full_h = 0.0
        self._track_full_h_w = 0.0
        self._track_green_left = 0
        # MeterBoxKalman: a reset_tracking / style / colour change is a hard episode boundary — drop
        # the geometry track with the other px memories so a NEW meter <max_dt later can't inherit
        # the old velocity/size (None when ORION_METER_TRACK is off -> no-op).
        if getattr(self, "_box_track", None) is not None:
            self._box_track.reset()
        # Trained-locator temporal region-lock memory: the most recent live YOLO locator box
        # (native-frame px) + frames elapsed since that hit. Populated ONLY when the locator
        # fires, so with the locator absent/disabled it stays None and the classical path is
        # byte-for-byte unchanged. Reset here so a shot-end / reset_tracking clears the lock.
        self._loc_mem_box: Optional[Tuple[int, int, int, int]] = None
        self._loc_mem_age = 0
        # Motion-aware region-lock: track the located box CENTRE velocity (px/frame) so a reuse crop on a
        # locator-miss frame follows a MOVING player-attached meter instead of holding a stale position.
        self._loc_mem_prev_c: Optional[Tuple[float, float]] = None
        self._loc_mem_vx = 0.0
        self._loc_mem_vy = 0.0
        self._loc_miss_streak = 0   # consecutive locator misses (>=3 zeroes the coast velocity)
        # T6 lock-ROI fast path state: frame counter for the periodic full-frame sweep, the
        # "pay one full-frame pass next frame" latch after an unanchored crop miss, and the last
        # SERVED meter position/time (any tier — park/fallback serves keep the cheap path alive
        # on shots the locator is blind to). Reset with the rest of the temporal state.
        self._loc_roi_n = 0
        self._loc_roi_force_full = False
        # RC-2 locked-coast frame counter: advances only while a lock is HELD; when
        # (n % _LOC_COAST_EVERY)!=0 the full-frame locator is skipped this frame and the
        # detector coasts on the remembered box (cheap ROI fill read). Reset off-lock.
        self._loc_coast_n = 0
        # T7 park-skip re-check counter: consecutive frames the (~36ms) full-frame park pass was skipped
        # during a confirmed locator lock. Forces one park pass every _PARK_RECHECK skips so the temporal
        # tracker stays warm for the hand-off when the locator drops. Reset on any non-skipped frame.
        self._park_skip_streak = 0
        self._serve_pos: Optional[Tuple[float, float, float]] = None
        self._serve_ts = -1.0
        # Width of the most recently SERVED locator colour match (Track T-c2): a loc_mem coast match far
        # thinner than this is a distractor sliver the widened crop swallowed, not the tracked meter.
        self._last_served_w = 0.0

    def reset_tracking(self) -> None:
        self._reset()

    def set_active_style(self, style_name: Optional[str]) -> None:
        self._active = style_name
        self._reset()

    def reload_config(self, cfg: Optional[DetectorConfig] = None) -> None:
        if cfg is None:
            cfg = load_detector_config()
        self._cfg = cfg
        self._stability._cfg = cfg
        self._motion._cfg = cfg
        self._green.set_cfg(cfg)
        self._anchor.configure(cfg)
        # Config changed -> re-evaluate the auto meter-colour lock from scratch,
        # then re-seed it with the (possibly new) configured production colour so a
        # live colour change pins the right colour instead of re-thrashing.
        self._auto_color_locked = None
        self._auto_color_cand = None
        self._auto_color_streak = 0
        self._seed_auto_color_lock()
        self._park.reset()                 # drop stale park tracks across a live colour/style change
        self._cuda.configure(getattr(cfg, "cuda_enabled", True))

    def cuda_status(self) -> Dict[str, Any]:
        return self._cuda.status()

    def _hsv_mask(
        self,
        roi_bgr: np.ndarray,
        ranges: List[Tuple[np.ndarray, np.ndarray]],
        kernel_size: Tuple[int, int] = (5, 5),
    ) -> np.ndarray:
        mask = self._cuda.mask_ranges(roi_bgr, ranges, kernel_size)
        if mask is not None:
            # Orion: horizontal dilation for thin 2px meters (same as CPU path)
            mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1)), iterations=1)
            return mask

        roi_hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        h, w = roi_hsv.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        for lo, hi in ranges:
            lo_u8 = np.array(lo, dtype=np.uint8).reshape(3)
            hi_u8 = np.array(hi, dtype=np.uint8).reshape(3)
            mask = cv2.bitwise_or(mask, cv2.inRange(roi_hsv, lo_u8, hi_u8))
        if kernel_size[0] > 1 and kernel_size[1] > 1:
            mask = cv2.morphologyEx(
                mask,
                cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, kernel_size),
                iterations=1,
            )
        # Orion: horizontal dilation (3x1) connects thin 2px vertical meters
        # into a 4-6px contour so cv2.findContours produces a stable blob.
        # Without this a single-pixel gap splits the meter and detection drops.
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1)), iterations=1)
        return mask

    def _overlaps_ui(self, x: float, y: float, w: int, h: int) -> bool:
        for z in self._cfg.ui_exclusion_zones:
            zx, zy = int(z.get("x", 0)), int(z.get("y", 0))
            zw, zh = int(z.get("w", 0)), int(z.get("h", 0))
            if x < zx + zw and x + w > zx and y < zy + zh and y + h > zy:
                return True
        return False

    @staticmethod
    def _bbox_center(bbox: Tuple[int, int, int, int]) -> Tuple[float, float]:
        x, y, w, h = bbox
        return float(x) + float(w) * 0.5, float(y) + float(h) * 0.5

    def _near_last_result(self, bbox: Tuple[int, int, int, int], scale: float = 2.5) -> bool:
        if self._last_result is None or not self._last_result.detected:
            return False
        ax, ay = self._bbox_center(bbox)
        bx, by = self._bbox_center(self._last_result.bbox)
        last_w = max(1.0, float(self._last_result.bbox[2]))
        last_h = max(1.0, float(self._last_result.bbox[3]))
        max_dist = max(24.0, max(last_w, last_h) * float(scale))
        return float(np.hypot(ax - bx, ay - by)) <= max_dist

    def _candidate_structure_scores(
        self,
        roi_bgr: np.ndarray,
        mask: np.ndarray,
        cx: int,
        cy: int,
        cw: int,
        ch: int,
    ) -> Tuple[float, float, float, float, float]:
        if roi_bgr is None or roi_bgr.size == 0 or mask is None or mask.size == 0 or cw <= 0 or ch <= 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0

        box_mask = mask[cy:cy + ch, cx:cx + cw]
        bbox_area = float(max(1, cw * ch))
        density = float(cv2.countNonZero(box_mask)) / bbox_area

        row_counts = np.count_nonzero(box_mask > 0, axis=1) if box_mask.size else np.array([], dtype=np.int32)
        col_counts = np.count_nonzero(box_mask > 0, axis=0) if box_mask.size else np.array([], dtype=np.int32)
        row_coverage = float(np.count_nonzero(row_counts > 0)) / max(1.0, float(ch))
        col_coverage = float(np.count_nonzero(col_counts > 0)) / max(1.0, float(cw))

        rh, rw = roi_bgr.shape[:2]
        pad = max(2, int(round(min(cw, ch) * 0.20)))
        x1 = max(0, cx - pad)
        y1 = max(0, cy - pad)
        x2 = min(rw, cx + cw + pad)
        y2 = min(rh, cy + ch + pad)
        expanded = roi_bgr[y1:y2, x1:x2]
        if expanded.size == 0:
            return density, row_coverage, col_coverage, 0.0, 0.0

        gray = cv2.cvtColor(expanded, cv2.COLOR_BGR2GRAY)
        dark = (gray < 78).astype(np.uint8)
        eh, ew = dark.shape[:2]
        strip = max(1, min(4, int(round(min(ew, eh) * 0.20))))
        left_dark = float(np.mean(dark[:, :strip])) if ew >= strip else 0.0
        right_dark = float(np.mean(dark[:, ew - strip:])) if ew >= strip else 0.0
        top_dark = float(np.mean(dark[:strip, :])) if eh >= strip else 0.0
        bottom_dark = float(np.mean(dark[eh - strip:, :])) if eh >= strip else 0.0
        # A true meter usually has at least one dark side outline plus some
        # edge structure. Shoes/jerseys tend to be color blobs with weak borders.
        outline_score = max(left_dark, right_dark) * 0.60 + max(top_dark, bottom_dark) * 0.40

        edges = cv2.Canny(gray, 40, 120)
        edge_density = min(1.0, float(cv2.countNonZero(edges)) / max(1.0, float(ew * eh)))
        return density, row_coverage, col_coverage, outline_score, edge_density

    def _style_candidates(self) -> List[Dict[str, Any]]:
        if not self._active:
            return self._styles
        active_norm = _norm_style_name(self._active)
        filtered = [s for s in self._styles if _norm_style_name(s.get("style", "")) == active_norm]
        if filtered:
            return filtered
        # Unknown or stale launcher style: keep detection alive by trying the
        # Known style family first, then any custom styles that may exist.
        preferred = [s for s in self._styles if _norm_style_name(s.get("style", "")) in _STYLE_GEOM_PROFILES]
        custom = [s for s in self._styles if _norm_style_name(s.get("style", "")) not in _STYLE_GEOM_PROFILES]
        return preferred + custom

    def _get_detect_crop(self, frame_bgr: np.ndarray) -> Tuple[int, int, np.ndarray]:
        """Return a locked ROI crop when available, otherwise the original frame.

        `_color_detect()` already applies the wide search zones for unlocked
        frames.  This method only pre-crops once the dynamic ROI is locked so
        the detector can scan fewer pixels without losing the original
        full-frame discovery behavior.
        """
        if frame_bgr is None or frame_bgr.size == 0:
            return 0, 0, frame_bgr

        try:
            H, W = frame_bgr.shape[:2]
            zones = self._roi_lock.get_search_zones(W, H, self._cfg)
            if not zones:
                return 0, 0, frame_bgr

            label, x1, y1, x2, y2 = zones[0]
            if label != "locked":
                return 0, 0, frame_bgr

            x1 = max(0, min(W - 1, int(x1)))
            y1 = max(0, min(H - 1, int(y1)))
            x2 = max(x1 + 1, min(W, int(x2)))
            y2 = max(y1 + 1, min(H, int(y2)))
            crop = frame_bgr[y1:y2, x1:x2]
            if crop.size == 0:
                return 0, 0, frame_bgr
            return x1, y1, crop
        except Exception:
            return 0, 0, frame_bgr

    def _anchor_detect_crop(
        self, frame_bgr: np.ndarray, anchor_roi: Tuple[int, int, int, int]
    ) -> Tuple[int, int, np.ndarray]:
        """Crop to the template-located meter region (inflated by a margin so the
        colour contour has room to breathe). Returns (offset_x, offset_y, crop)."""
        try:
            H, W = frame_bgr.shape[:2]
            mx, my, mw, mh = anchor_roi
            pad = int(max(4, min(80, getattr(self._cfg, "micro_roi_padding_px", 18))))
            padx = max(pad, int(mw * 0.6))
            pady = max(pad, int(mh * 0.3))
            x1 = max(0, min(W - 1, mx - padx))
            y1 = max(0, min(H - 1, my - pady))
            x2 = max(x1 + 1, min(W, mx + mw + padx))
            y2 = max(y1 + 1, min(H, my + mh + pady))
            crop = frame_bgr[y1:y2, x1:x2]
            if crop.size == 0:
                return 0, 0, frame_bgr
            return x1, y1, crop
        except Exception:
            return 0, 0, frame_bgr

    def _loc_mem_expand(
        self, box: Tuple[int, int, int, int], W: int, H: int,
        age: int = 1, vx: float = 0.0, vy: float = 0.0,
    ) -> Tuple[int, int, int, int]:
        """Inflate + EXTRAPOLATE a remembered locator box (temporal region-lock) so a MOVING
        player-attached meter stays inside the scan crop on a locator-miss frame. The box centre is
        shifted along the tracked box velocity (vx,vy px/frame) by `age` frames since the last hit, and
        the padding GROWS with age (uncertainty compounds the longer we coast). _anchor_detect_crop pads
        further. With age=1,vx=vy=0 this reduces to the old modest 15% pad (stationary meter)."""
        try:
            x, y, w, h = int(box[0]), int(box[1]), int(box[2]), int(box[3])
            cx = x + w * 0.5 + vx * age                 # extrapolate the centre along tracked velocity
            cy = y + h * 0.5 + vy * age
            # Track T-c1: CAP the coast pad at 0.8 (was 2.0). A 2.0x pad grew the crop to ~5x the box
            # and swallowed off-meter red slivers as the coast aged (live: loc_mem returned w=7 fill 3%
            # at a 155px-jumped spot). The centre still tracks the pan via the velocity extrapolation
            # above; the pad only covers uncertainty AROUND it, so 0.8 is ample without over-reaching.
            grow = min(0.8, 0.15 + 0.35 * age)          # pad fraction grows with frames-since-hit (capped)
            ex = max(3, int(round(w * grow)))
            ey = max(3, int(round(h * grow)))
            nw = min(W, w + 2 * ex)
            nh = min(H, h + 2 * ey)
            nx = int(max(0, min(W - nw, cx - nw * 0.5)))
            ny = int(max(0, min(H - nh, cy - nh * 0.5)))
            return (nx, ny, max(1, int(nw)), max(1, int(nh)))
        except Exception:
            return box

    def _loc_mem_size_ok(self, w: float) -> bool:
        """Track T-c2 size-consistency guard for a loc_mem COAST match. Reject a colour match far
        THINNER than the recently-served meter — the widened coast crop can enclose an off-meter red
        sliver (live: w=7 fill 3% while the tracked meter was w~31). Permissive until a served width is
        known so a first coast frame isn't blocked."""
        ref = float(getattr(self, "_last_served_w", 0.0) or 0.0)
        if ref <= 0.0:
            return True
        return float(w) >= 0.45 * ref

    def _locate_meter(self, frame_bgr: np.ndarray, W: int, H: int, ts: float):
        """T6 lock-ROI locator fast path (see the _LOC_ROI flag block for the full rationale).

        Runs the YOLO locator on a scale-preserving crop around the freshest known meter
        position instead of the full frame, cutting the per-frame cost to ~7% while a lock
        holds. Anchor priority: the locator's own fresh memory (extrapolated along its coast
        velocity), else the last SERVED position (park / wide-fallback serves included, so an
        edge shot the locator cannot see still rides the cheap path). Full-frame passes: no
        fresh anchor, the periodic sweep, the post-miss latch, or a degenerate crop."""
        # RC-2 LOCKED-COAST: while a lock is HELD (fresh locator memory + a valid meter served within
        # _LOC_ROI_SERVE_S + the shot not spent), skip the expensive full-frame locator on
        # (_LOC_COAST_EVERY-1) of every _LOC_COAST_EVERY frames. Returning None routes the detector into
        # its existing loc_mem miss-coast branch, which reads the fill on the velocity-extrapolated
        # remembered box -> the fill stays fresh every frame while the locator amortizes ~1/N. The
        # locator still runs on the Nth frame (re-anchor) and whenever the lock is not held (acquire).
        if (_LOC_COAST_EVERY > 1 and not self._loc_roi_force_full
                and self._loc_mem_box is not None
                and self._loc_mem_age < _LOC_MEM_TTL_FRAMES
                and self._serve_pos is not None and self._serve_ts >= 0.0
                and (ts - self._serve_ts) <= _LOC_ROI_SERVE_S
                and self.last_rise_state != "spent"):
            self._loc_coast_n += 1
            if (self._loc_coast_n % _LOC_COAST_EVERY) != 0:
                return None
        else:
            self._loc_coast_n = 0
        if not _LOC_ROI:
            return self._locator.locate(frame_bgr)
        self._loc_roi_n += 1
        anchor = None
        if self._loc_mem_box is not None and self._loc_mem_age <= max(2, _LOC_MEM_TTL_FRAMES // 2):
            bx, by, bw, bh = self._loc_mem_box
            g = float(self._loc_mem_age + 1)
            anchor = (bx + bw * 0.5 + self._loc_mem_vx * g,
                      by + bh * 0.5 + self._loc_mem_vy * g, float(max(bw, bh)))
        elif self._serve_pos is not None and (ts - self._serve_ts) <= _LOC_ROI_SERVE_S:
            anchor = self._serve_pos
        sweep_due = (self._loc_roi_n % _LOC_ROI_SWEEP) == 0
        if anchor is None or sweep_due or self._loc_roi_force_full:
            self._loc_roi_force_full = False
            return self._locator.locate(frame_bgr)
        acx, acy, adim = anchor
        half = int(max(_LOC_ROI_CROP // 2, 1.6 * float(adim)))
        x2 = min(W, int(acx) + half); x1 = max(0, x2 - 2 * half); x2 = min(W, x1 + 2 * half)
        y2 = min(H, int(acy) + half); y1 = max(0, y2 - 2 * half); y2 = min(H, y1 + 2 * half)
        if (x2 - x1) < 96 or (y2 - y1) < 96:
            return self._locator.locate(frame_bgr)
        # Scale-preserving inference size: infer the crop at the SAME px-per-imgsz ratio the model
        # sees on the full frame, so the meter's apparent size matches training (a raw crop at the
        # full imgsz would UPSAMPLE — costing the same and shifting the object scale).
        full_imgsz = int(getattr(self._locator, "_imgsz", 0) or 0)
        crop = frame_bgr[y1:y2, x1:x2]
        if full_imgsz > 0:
            ratio = full_imgsz / float(max(W, H))
            roi_imgsz = int(max(160, min(full_imgsz,
                                         round((max(x2 - x1, y2 - y1) * ratio) / 32.0) * 32)))
            box = self._locator.locate(crop, imgsz=roi_imgsz)
        else:
            box = self._locator.locate(crop)
        if box is None:
            # Nothing under the crop. While a live serve still tracks the meter (a locator-blind
            # edge shot), stay on cheap crops; otherwise the meter may be anywhere -> pay one
            # full-frame pass next frame.
            if not (self._serve_pos is not None and (ts - self._serve_ts) <= _LOC_ROI_SERVE_S):
                self._loc_roi_force_full = True
            return None
        return (int(box[0]) + x1, int(box[1]) + y1, int(box[2]), int(box[3]))

    def _box_fallback_match(self, crop_bgr: np.ndarray, loc_box: Tuple[int, int, int, int],
                            yolo_conf: float, strong: bool = True) -> Optional["_MatchResult"]:
        """Track T-b2: a v6 box whose constrained colour scan found no clean meter contour. The
        learned locator firing HERE is appearance proof, so emit a detection FROM the box
        (position = box; fill/green are measured downstream by _measure_track) instead of falling through
        to the park path — that fall-through is what SUPPRESSED the located meter for ~60 frames ("meter
        disappears while moving"). A genuinely EMPTY box (no red at all — the ~3% no-red false-box case)
        returns None so it still can't latch. Locator-served path only; never reached with the locator off.
        T5-b (2026-07-05): WEAK boxes (runtime conf floor .. _LOC_STRONG_CONF) now reach this too —
        live edge-fade boxes land in that dead-band and were discarded outright, blacking out a
        locator-detected meter. strong=False withholds the loc_strong corroboration privilege, so a
        weak box's emit must still corroborate downstream (rise / green / recent-prox) before it
        serves; the red-presence floor below rejects an empty weak box exactly like a strong one."""
        try:
            if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
                return None
            _blo, _bhi, _hlo, _hhi = self._red_bounds()
            mask = self._hsv_mask(crop_bgr, [(_hlo, _hhi)], (5, 5))
            red_px = int(cv2.countNonZero(mask))
            # Require SOME red inside the box; an empty/no-red box must not be forced to a detection.
            if red_px < max(8, int(0.0008 * crop_bgr.shape[0] * crop_bgr.shape[1])):
                return None
            bx, by, bw, bh = int(loc_box[0]), int(loc_box[1]), int(loc_box[2]), int(loc_box[3])
            # Mid confidence: yolo-weighted but kept below the strong colour-confirmed blend so the
            # validator's conf/corroboration gates still act on a box we could not photometrically confirm.
            conf = float(min(0.90, max(_LOC_BOX_CONF_FLOOR, 0.75 * float(yolo_conf))))
            return _MatchResult(
                found=True, template_name=str(self._active or "Arrow2"), confidence=conf,
                x=float(bx), y=float(by), w=int(max(1, bw)), h=int(max(1, bh)),
                zone="loc_box", bgr_lo=_blo, bgr_hi=_bhi, hsv_lo=_hlo, hsv_hi=_hhi,
                med_h=-1.0, med_s=-1.0, green_confirmed=False, loc_strong=bool(strong),
            )
        except Exception:
            return None

    def _anchor_clear_stale(self, anchor_roi: Tuple[int, int, int, int]) -> None:
        """Drop a held lock/echo that sits FAR from the template-located region.

        Kills the false-lock: once the anchor relocates the true meter region, a
        cosmetic blob held by the stability latch / meter-memory echo elsewhere on
        screen is discarded so it can never be echoed as fill. A held bbox INSIDE
        the located region is the real meter mid-dropout — left intact so legit
        bridging is preserved."""
        last = self._last_result
        if last is None or not getattr(last, "detected", False):
            return
        try:
            ax, ay, aw, ah = anchor_roi
            acx = ax + aw * 0.5
            acy = ay + ah * 0.5
            bx, by, bw, bh = last.bbox
            bcx = bx + bw * 0.5
            bcy = by + bh * 0.5
            tol = 0.5 * float(np.hypot(aw, ah)) + float(max(aw, ah))
            if float(np.hypot(bcx - acx, bcy - acy)) > tol:
                self._last_result = None
                self._meter_memory_left = 0
                self._roi_lock = DynamicROILock(timeout_frames=8)
                self._stability.reset()
        except Exception:
            pass

    _AUTO_COLOR_LOCK_FRAMES = 3

    def _seed_auto_color_lock(self) -> None:
        """Pin the auto-colour lock to the configured production colour so the
        every-frame candidate wide-scan can't override a known-good colour (see
        __init__). No-op when auto is off or no valid colour is configured."""
        try:
            if getattr(self._cfg, "auto_meter_color", False):
                col = str(getattr(self._cfg, "meter_color", "") or "")
                if col in BGR_COLOR_RANGES:
                    self._auto_color_locked = col
                    self._auto_color_cand = col
        except Exception:
            pass

    def _auto_color_candidates(self) -> List[str]:
        cands = getattr(self._cfg, "auto_meter_color_candidates", None)
        if cands:
            picked = [c for c in cands if c in BGR_COLOR_RANGES]
            if picked:
                return picked
        return list(BGR_COLOR_RANGES.keys())

    def _auto_select_color(self, frame_bgr: np.ndarray,
                           scale_wh: Optional[Tuple[int, int]]) -> Tuple[Optional[str], float]:
        """Wide-scan every candidate colour and return the highest-confidence one
        that produces a meter above the confidence floor (else (None, 0)). Restores
        the configured colour. Runs only while the auto-lock is open."""
        best_color: Optional[str] = None
        best_conf = 0.0
        saved = self._cfg.meter_color
        try:
            for color in self._auto_color_candidates():
                self._cfg.meter_color = color
                try:
                    m = self._color_detect(frame_bgr, force_wide=True, scale_wh=scale_wh)
                except Exception:
                    continue
                if m.found and m.confidence > best_conf:
                    best_conf = m.confidence
                    best_color = color
        finally:
            self._cfg.meter_color = saved
        if best_color is not None and best_conf >= self._cfg.confidence_threshold:
            return best_color, best_conf
        return None, 0.0

    def _auto_color_step(self, frame_bgr: np.ndarray, scale_wh: Tuple[int, int]) -> None:
        """Pick + lock the meter colour. While unlocked, scan candidates and set the
        frame's colour to the current best; lock after _AUTO_COLOR_LOCK_FRAMES
        consecutive agreeing frames. Once locked, just pin the colour."""
        if self._auto_color_locked is not None:
            self._cfg.meter_color = self._auto_color_locked
            return
        win, _conf = self._auto_select_color(frame_bgr, scale_wh)
        if win is None:
            return  # no meter on screen this frame — leave colour as-is
        if win == self._auto_color_cand:
            self._auto_color_streak += 1
        else:
            self._auto_color_cand = win
            self._auto_color_streak = 1
        self._cfg.meter_color = win
        if self._auto_color_streak >= self._AUTO_COLOR_LOCK_FRAMES:
            self._auto_color_locked = win
            logger.info("Auto meter colour locked: %s", win)

    def _color_detect(self, frame_bgr: np.ndarray, force_full_zone: bool = False,
                      scale_wh: Optional[Tuple[int, int]] = None,
                      force_wide: bool = False, box_constrained: bool = False) -> _MatchResult:
        # box_constrained (Track T-b3): the scan crop is a STRONG v6 locator box, which already
        # localises the meter. Relax the classical size/aspect + purity gates and lower the internal
        # confidence floor inside it — those gates otherwise over-reject a small/blurred early-rise
        # fill the learned locator already vouched for. Default False, so every non-locator caller
        # (classical / park / anchor) is byte-identical.
        # Reset the per-frame scan counters up front so early returns (empty
        # frame / no styles) still leave _last_scan reflecting THIS call.
        self._last_scan = {
            "cand_n": 0, "cand_size_ok": 0, "purity_rej": 0,
            "med_h": -1.0, "med_s": -1.0, "zone": "",
        }
        if frame_bgr is None or frame_bgr.size == 0:
            return _MatchResult()

        H, W = frame_bgr.shape[:2]
        # Contour size gates + the style search box are defined in full-frame
        # (1920x1080) reference coords. When the frame has been pre-cropped to
        # the locked ROI, the crop is a 1:1 sub-image (true pixel scale), so the
        # scale factors MUST come from the ORIGINAL frame size, not the crop —
        # otherwise h_max collapses (e.g. 182 * crop_h/1080 ≈ 36px) and the meter
        # is rejected as "too tall" the moment it fills, which caused the
        # periodic detection dropouts seen on the real video.
        sw, sh = (scale_wh if scale_wh is not None else (W, H))
        sx = float(sw) / float(self._JSON_W)
        sy = float(sh) / float(self._JSON_H)

        meter_color = self._cfg.meter_color
        # When the frame has already been pre-cropped, treat the entire frame
        # as the search zone — the crop IS the locked zone. force_wide bypasses the
        # ROI lock's tight cone and scans the full band (the same-frame fallback when
        # the locked-crop fast path misses a meter that is actually on screen).
        if force_full_zone:
            search_zones = [("crop", 0, 0, W, H)]
        elif force_wide:
            search_zones = [self._roi_lock.wide_zone(W, H, self._cfg)]
        else:
            search_zones = self._roi_lock.get_search_zones(W, H, self._cfg)
        styles = self._style_candidates()
        if not styles:
            return _MatchResult()

        best = _MatchResult()
        # Optional diagnostic sink (set self._debug = {} before detect()): records the
        # search regions, size/aspect gate thresholds, and every colour candidate's
        # bbox so a harness can draw WHY a visible meter was missed (roi_not_found).
        dbg = getattr(self, "_debug", None)
        # Always-on lightweight scan counters (scalars only — safe at frame rate).
        # Candidates are counted per style×zone pass, so a blob scanned under two
        # styles counts twice; the counts are diagnostic volume, not unique blobs.
        scan_cand_total = 0
        scan_cand_size_ok = 0
        scan_purity_rej = 0
        scan_best_med_h = -1.0
        scan_best_med_s = -1.0

        for style in styles:
            style_name = str(style.get("style", "unknown") or "unknown")
            profile = _style_geom_profile(style_name)
            colors = style.get("colors", {})
            if meter_color not in colors:
                if meter_color not in BGR_COLOR_RANGES:
                    continue
                colors = _builtin_style(style_name).get("colors", {})
            try:
                lo = np.array(colors[meter_color]["low"], dtype=np.uint8)
                hi = np.array(colors[meter_color]["high"], dtype=np.uint8)
                hsv_lo_style, hsv_hi_style = _style_bgr_bounds_to_hsv(lo, hi)
            except Exception:
                if meter_color not in _HSV_FILL_RANGES:
                    continue
                hlo, hhi = _HSV_FILL_RANGES[meter_color]
                lo = np.array(BGR_COLOR_RANGES.get(meter_color, ([0, 0, 0], [255, 255, 255]))[0], dtype=np.uint8)
                hi = np.array(BGR_COLOR_RANGES.get(meter_color, ([0, 0, 0], [255, 255, 255]))[1], dtype=np.uint8)
                hsv_lo_style = np.array(hlo, dtype=np.uint8)
                hsv_hi_style = np.array(hhi, dtype=np.uint8)

            contour = style.get("contour", {})
            w_min = max(2, int(float(contour.get("w_min", 5)) * sx * 0.75))
            w_max = max(w_min + 2, int(contour.get("w_max", 100) * sx))
            h_min = max(8, int(float(contour.get("h_min", 20)) * sy * 0.65))
            h_max = max(h_min + 4, int(contour.get("h_max", 300) * sy))
            if dbg is not None:
                dbg.setdefault("gates", []).append({
                    "style": style_name, "sx": round(sx, 3), "sy": round(sy, 3),
                    "w_min": w_min, "w_max": w_max, "h_min": h_min, "h_max": h_max,
                    "ar_min": float(profile.get("ar_min", 0.85)), "ar_max": float(profile.get("ar_max", 9.5)),
                })

            if force_full_zone:
                # Cropped frame: the crop IS the search region. The style search
                # box is in full-frame coords and is meaningless in crop-local
                # coordinates, so scan the whole crop.
                jx1, jy1, jx2, jy2 = 0, 0, W, H
            else:
                search = style.get("search", {})
                jx1 = int(search.get("left", 0) * sx)
                jy1 = int(search.get("top", 0) * sy)
                jx2 = int(search.get("right", W) * sx)
                jy2 = int(search.get("bottom", H) * sy)
                jx1 = max(0, min(W - 1, jx1))
                jy1 = max(0, min(H - 1, jy1))
            jx2 = max(jx1 + 1, min(W, jx2))
            jy2 = max(jy1 + 1, min(H, jy2))

            for zone_label, zx1, zy1, zx2, zy2 in search_zones:
                rx1 = max(zx1, jx1)
                ry1 = max(zy1, jy1)
                rx2 = min(zx2, jx2)
                ry2 = min(zy2, jy2)
                if rx2 <= rx1 or ry2 <= ry1:
                    continue
                if dbg is not None:
                    dbg.setdefault("regions", []).append((rx1, ry1, rx2, ry2, zone_label))

                roi = frame_bgr[ry1:ry2, rx1:rx2]
                if roi.size == 0:
                    continue

                hsv_lo = hsv_lo_style
                hsv_hi = hsv_hi_style
                mask = self._hsv_mask(roi, [(hsv_lo, hsv_hi)], (5, 5))
                cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for cnt in cnts:
                    area = float(cv2.contourArea(cnt))
                    if area < 12.0:
                        continue

                    cx, cy, cw, ch = cv2.boundingRect(cnt)
                    scan_cand_total += 1
                    if w_min <= cw <= w_max and h_min <= ch <= h_max:
                        scan_cand_size_ok += 1
                    if dbg is not None:
                        _ar = float(ch) / float(max(1, cw))
                        dbg.setdefault("cand", []).append({
                            "x": int(rx1 + cx), "y": int(ry1 + cy), "w": int(cw), "h": int(ch),
                            "ar": round(_ar, 2), "area": round(area, 0),
                            "size_ok": bool(w_min <= cw <= w_max and h_min <= ch <= h_max),
                        })
                    # WIDTH is the meter's STABLE dimension (a vertical fill bar's width barely
                    # changes with fill); HEIGHT is the fill SIGNAL -- short at low fill. The strict
                    # height/aspect floors were dropping the whole early rise (0-50%): the #1 recall
                    # loss (offline: ~54% of present-but-missed frames were size-rejected). So for
                    # the production PURPLE meter, gate a SHORT candidate hard on width + a tiny
                    # floor and let it through to the purity/green-anchor gates below (which hold
                    # noise out). Other colours / the auto-colour scan KEEP the strict gates (a green
                    # target must not qualify as a low-fill green meter -- that mis-locks auto-colour).
                    low_fill_floor = max(6, int(H * 0.006))
                    # The low-fill early-rise relaxation is the meter's physical SHAPE, not a
                    # colour property: the production meter (now Red, historically Purple) loses
                    # its whole 0-50% rise to the strict height/aspect gates otherwise (the #1
                    # recall loss). Apply it to the configured production colour, but NOT during
                    # the auto-colour WIDE scan (force_wide) so a green target can't qualify as a
                    # low-fill meter and mis-lock auto-colour.
                    _production_color = (not force_wide) and meter_color in ("Purple", "Red")
                    is_low_fill = _production_color and (ch < h_min or ch < int(H * 0.025))
                    if is_low_fill or box_constrained:
                        # box_constrained scans use the low-fill-relaxed size gate for ANY candidate:
                        # the v6 box already localised the meter, so the strict h_min floor only drops
                        # a genuine short/blurred fill inside a good box.
                        if cw < w_min or cw > w_max or ch > h_max or ch < low_fill_floor:
                            continue
                    else:
                        if cw < w_min or cw > w_max or ch < h_min or ch > h_max:
                            continue

                    ar = float(ch) / float(max(1, cw))
                    ar_min = float(profile.get("ar_min", 0.85))
                    ar_max = float(profile.get("ar_max", 9.5))
                    if ar > ar_max:
                        continue
                    if not is_low_fill and not box_constrained:
                        if ar < ar_min:
                            continue
                        if ch < (H * 0.025):
                            continue

                    # --- Contour geometry gate (circularity) ---
                    # Reject ragged colour-matched noise (text / jersey / court-line fragments)
                    # that clears the bbox size+aspect gates but isn't a clean compact bar.
                    # circularity = 4*pi*area / perimeter^2: a filled bar is compact (~0.35-0.7
                    # for our vertical meter across fill levels); ragged noise is far lower.
                    perim = float(cv2.arcLength(cnt, True))
                    circularity = (4.0 * float(np.pi) * area / (perim * perim)) if perim > 1.0 else 0.0
                    if dbg is not None and dbg.get("cand"):
                        dbg["cand"][-1]["circ"] = round(circularity, 3)
                    if self._cfg.geometry_gate_enabled and circularity < float(self._cfg.min_circularity):
                        if dbg is not None:
                            dbg.setdefault("circ_reject", []).append({
                                "x": int(rx1 + cx), "y": int(ry1 + cy), "w": int(cw), "h": int(ch),
                                "circ": round(circularity, 3),
                            })
                        continue

                    density, row_coverage, col_coverage, outline_score, edge_density = self._candidate_structure_scores(
                        roi, mask, cx, cy, cw, ch
                    )
                    density_min = max(0.01, float(self._cfg.min_meter_density) * float(profile.get("density_scale", 1.0)))
                    density_max = min(1.0, max(density_min, float(self._cfg.max_meter_density) * max(1.0, 1.15 / max(0.2, float(profile.get("density_scale", 1.0))))))
                    if density < density_min or density > density_max:
                        continue
                    row_req = max(0.05, min(1.0, float(profile.get("row_min", self._cfg.min_vertical_coverage))))
                    col_req = max(0.05, min(1.0, float(profile.get("col_min", 0.35))))
                    if row_coverage < row_req:
                        continue
                    if col_coverage < col_req:
                        continue
                    outline_req = max(0.0, float(self._cfg.min_outline_score) * float(profile.get("outline_scale", 1.0)))
                    if outline_score < outline_req and edge_density < 0.045:
                        continue

                    # --- RED BRIGHTNESS (val) gate — the green-less precision discriminator ---
                    # The meter fill renders BRIGHT (val ~200); the dark NBA logo / court / shadow
                    # reds are val <=130. Reject a DARK-red candidate at EVERY fill: this is what
                    # lets detection drop the (tiny/unreliable) green-chevron requirement and still
                    # never lock the logo/court. Calibrated floor: _RED_VAL_FLOOR.
                    if meter_color == "Red" and _RED_VAL_FLOOR > 0:
                        _vm = mask[cy:cy + ch, cx:cx + cw] > 0
                        if int(np.count_nonzero(_vm)) >= 6:
                            _vv = cv2.cvtColor(roi[cy:cy + ch, cx:cx + cw], cv2.COLOR_BGR2HSV)[:, :, 2][_vm]
                            if float(np.median(_vv)) < float(_RED_VAL_FLOOR):
                                if dbg is not None:
                                    dbg.setdefault("val_reject", []).append({
                                        "x": int(rx1 + cx), "y": int(ry1 + cy),
                                        "w": int(cw), "h": int(ch), "v": int(np.median(_vv)),
                                    })
                                continue

                    # --- Meter colour-purity gate ---
                    # Reject cosmetic blobs (pink floatie, jerseys, court paint)
                    # whose masked-median hue/sat is clearly off the selected
                    # meter colour. Purple keeps its dedicated live-proven knobs
                    # (real meter Hue ~150 / Sat ~200+, floatie Hue ~166 /
                    # Sat ~142); the other colours use _COLOR_PURITY_GATES.
                    med_h = med_s = -1.0
                    if self._cfg.purple_purity_enabled and (
                        meter_color == "Purple" or meter_color in _COLOR_PURITY_GATES
                    ):
                        box_mask = mask[cy:cy + ch, cx:cx + cw]
                        if int(cv2.countNonZero(box_mask)) >= int(self._cfg.purple_purity_min_px):
                            box_hsv = cv2.cvtColor(roi[cy:cy + ch, cx:cx + cw], cv2.COLOR_BGR2HSV)
                            sel = box_mask > 0
                            _hue_vals = box_hsv[:, :, 0][sel].astype(np.int16)
                            # CIRCULAR median for bands that wrap 0/180 (Red): a naive median of red
                            # pixels split across 0-14 + 168-180 lands ~89 (mid-range), and the purity
                            # gate then wrongly rejects the REAL meter (this killed ~95% of red-meter
                            # detections live). Shift hues >90 negative, median, map back, so the
                            # median lands on the true red (~170). Non-wrapping colours (Purple etc.)
                            # keep the plain median.
                            _gw = _COLOR_PURITY_GATES.get(meter_color, {}).get("hue_min")
                            if _gw is not None and _gw < 0.0:
                                _m = float(np.median(np.where(_hue_vals > 90, _hue_vals - 180, _hue_vals)))
                                med_h = _m + 180.0 if _m < 0.0 else _m
                            else:
                                med_h = float(np.median(_hue_vals))
                            med_s = float(np.median(box_hsv[:, :, 1][sel]))
                            if is_low_fill:
                                # Thin low-fill bar: antialiased edge pixels drag the masked-median
                                # SAT down, but the HUE stays on the meter line. Gate on hue proximity
                                # to the colour CENTRE (circular distance handles Red's 0/180 wrap) and
                                # drop the sat floor -- admits the genuine early rise, still rejects
                                # off-hue cosmetics (floatie/jersey/court). Colour-agnostic so the
                                # production meter gets it whether it renders Purple or Red.
                                center = _METER_HUE_CENTER.get(meter_color)
                                if center is None:
                                    rejected = False
                                else:
                                    dh = abs(med_h - center)
                                    dh = min(dh, 180.0 - dh)
                                    rejected = dh > 10.0
                            elif meter_color == "Purple":
                                rejected = (med_h > self._cfg.purple_purity_hue_max
                                            or med_s < self._cfg.purple_purity_sat_min)
                            else:
                                med_v = float(np.median(box_hsv[:, :, 2][sel]))
                                rejected = _purity_gate_rejects(meter_color, med_h, med_s, med_v)
                            # box_constrained (Track T-b3): the v6 box is the localisation proof and the
                            # classical purity gate over-rejected the BLURRED fill inside it (live:
                            # 105/108 recall holes HAD red) — but blur shifts SAT/VAL, never red's HUE
                            # to green. So the bypass only applies to a candidate whose masked-median
                            # hue is still in the red wrap band; a wrong-hue winner (e.g. the meter's
                            # own GREEN CHEVRON at low fill, med_h~57 -> fill misread ~87% -> premature
                            # release) must keep purity-rejecting even inside a strong box.
                            purity_bypass = False
                            if rejected and box_constrained:
                                if meter_color == "Red":
                                    purity_bypass = (med_h >= 150.0) or (med_h <= 25.0)
                                else:
                                    purity_bypass = True
                            if rejected and not purity_bypass:
                                scan_purity_rej += 1
                                if dbg is not None:
                                    dbg.setdefault("purity_reject", []).append({
                                        "x": int(rx1 + cx), "y": int(ry1 + cy), "w": int(cw), "h": int(ch),
                                        "med_h": round(med_h, 1), "med_s": round(med_s, 1),
                                    })
                                continue

                    # Low-fill GREEN-ANCHOR (low/short fill ONLY): a genuine early-rise meter has the
                    # green make-window above it within the track; a stray short red blob does not, so
                    # require green above ONLY for a low fill (which is ambiguous with red noise).
                    # NOT at all fills: requiring green everywhere over-rejected real meters at 720p
                    # (the ~1px green chevron isn't reliably detected, especially on moving fades ->
                    # 14% live detection). Tall, clearly meter-shaped bars pass on geometry alone.
                    # RED-HUE-PRECISE REWORK: skipped entirely for Red — the green make-window is
                    # often tiny, so it must NOT gate detection; the brightness(val) + geometry gates
                    # above hold low-fill red noise out instead. (Kept for Purple/other colours.)
                    if is_low_fill and _LOWFILL_GREEN_ANCHOR and meter_color != "Red":
                        _gband = max(int(H * 0.18), int(ch * 6))
                        _gx1 = int(max(0.0, (rx1 + cx) - cw * 0.6))
                        _gx2 = int((rx1 + cx) + cw + int(cw * 0.6))
                        _gy1 = int(max(0.0, (ry1 + cy) - _gband))
                        _gy2 = int((ry1 + cy) + int(ch * 0.3))
                        if self._green.green_span_abs(frame_bgr, _gx1, _gy1, _gx2, _gy2) is None:
                            continue

                    ar_mid = max(0.01, (ar_min + ar_max) * 0.5)
                    ar_spread = max(0.5, (ar_max - ar_min) * 0.5)
                    ar_score = 1.0 - min(1.0, abs(ar - ar_mid) / ar_spread)
                    if is_low_fill:
                        # A genuine early fill is legitimately short-wide -> don't penalise its
                        # aspect (it's the fill signal, not a defect) or it sinks below threshold.
                        ar_score = max(ar_score, 0.6)
                    continuity_score = min(1.0, row_coverage * 0.75 + col_coverage * 0.25)
                    border_score = min(1.0, outline_score * 0.70 + edge_density * 1.60)
                    confidence = min(
                        1.0,
                        density * 0.38
                        + ar_score * 0.20
                        + continuity_score * 0.24
                        + border_score * 0.18,
                    )
                    confidence = max(0.0, min(1.0, confidence + float(profile.get("bias", 0.0))))

                    # Clean compact bar (high circularity) outranks a ragged near-miss blob.
                    if circularity >= 0.45:
                        confidence = min(1.0, confidence + 0.03)

                    # --- Soft meter-positive boosters (never hard requirements) ---
                    # Only RAISE confidence for meter-like candidates so a real
                    # meter outranks a borderline pink blob when both are present;
                    # a missing green cap never blocks a detection.
                    abs_cx = float(rx1 + cx)
                    abs_cy = float(ry1 + cy)
                    if meter_color == "Purple" and med_h >= 0.0 and abs(med_h - 150.0) <= 6.0 and med_s >= 185.0:
                        # Tight magenta purity bonus (hue close to the meter's ~150).
                        confidence = min(1.0, confidence + 0.04)
                    # Soft Purple green-cap booster (never a hard requirement). The Red green-confidence
                    # floor was dropped — it added a green_span_abs call PER candidate (frame-rate cost)
                    # for no recall gain (the meters the classical scan misses are recovered once by the
                    # GREEN-FIRST FALLBACK below, not per-candidate here).
                    if meter_color == "Purple":
                        try:
                            band = max(8, int(ch * 1.2))
                            gx1 = int(max(0.0, abs_cx - cw * 0.5))
                            gx2 = int(abs_cx + cw + cw * 0.5)
                            gy1 = int(max(0.0, abs_cy - band))
                            gy2 = int(abs_cy + ch * 0.2)
                            if self._green.green_span_abs(frame_bgr, gx1, gy1, gx2, gy2) is not None:
                                confidence = min(1.0, confidence + 0.05)
                        except Exception:
                            pass

                    _conf_floor = float(self._cfg.confidence_threshold)
                    if box_constrained:
                        # A box-constrained candidate's colour confidence is blended UP with the strong
                        # v6 score after this returns; keep the internal floor low so a genuine low-fill
                        # fill inside the box survives to be emitted (the empty-box guard is red-presence,
                        # not colour confidence).
                        _conf_floor = min(_conf_floor, _LOC_BOX_CONF_FLOOR)
                    if confidence < _conf_floor:
                        continue

                    abs_x = float(rx1 + cx)
                    abs_y = float(ry1 + cy)
                    # Reject frequent floor/overlay false-positives near the lower edge.
                    if (abs_y + float(ch)) > (H * 0.96):
                        continue
                    if self._overlaps_ui(abs_x, abs_y, cw, ch):
                        continue

                    if confidence > best.confidence:
                        best = _MatchResult(True, style_name, float(confidence), abs_x, abs_y, int(cw), int(ch), zone_label, lo, hi, hsv_lo, hsv_hi, style, med_h=float(med_h), med_s=float(med_s))
                        scan_best_med_h = float(med_h)
                        scan_best_med_s = float(med_s)

        # Green-tip corroboration for the WINNING candidate (computed once per frame, not
        # per candidate — the per-candidate cost was why the Red green floor was dropped).
        # A genuine meter carries the neon-green make-window directly above its fill; record
        # it so the stability validator can require green-OR-rising to ACQUIRE a new lock.
        if best.found and str(getattr(self._cfg, "meter_color", "") or "") in ("Red", "Purple"):
            try:
                _bb = max(8, int(best.h * 1.4))
                _gx1 = int(max(0.0, best.x - best.w * 0.6))
                _gx2 = int(best.x + best.w + best.w * 0.6)
                _gy1 = int(max(0.0, best.y - _bb))
                _gy2 = int(best.y + best.h * 0.25)
                if self._green.green_span_abs(frame_bgr, _gx1, _gy1, _gx2, _gy2) is not None:
                    best.green_confirmed = True
            except Exception:
                pass

        # GREEN-FIRST FALLBACK: the classical scan found no usable candidate (roi_not_found), but the
        # meter is often still present as a thin LOW-CONFIDENCE bar the gates dropped — measured on live
        # frames, the scan misses ~half the real meters this way. Locate it by its unique green-chevron +
        # saturated-red signature and take it. It NO LONGER bypasses safety: the result must clear the
        # UI/bottom-edge checks below, and the stability validator still requires acquisition
        # corroboration (green_confirmed is True here by construction), so a green-over-red jersey/logo
        # that is static cannot latch. Only runs on a scan miss, so it never overrides a real lock.
        if not best.found and str(getattr(self._cfg, "meter_color", "") or "") in ("Red", "Purple"):
            try:
                _gl = self._green_first_locate(frame_bgr)
                if _gl is not None:
                    _gx, _gy, _gw, _gh = _gl
                    _by2 = float(_gy) + float(_gh)
                    if _by2 <= (H * 0.96) and not self._overlaps_ui(float(_gx), float(_gy), int(_gw), int(_gh)):
                        best = _MatchResult(True, style_name, 0.9, float(_gx), float(_gy), int(_gw), int(_gh),
                                            "green_first", lo, hi, hsv_lo, hsv_hi, style,
                                            med_h=-1.0, med_s=-1.0, green_confirmed=True)
            except Exception:
                pass

        self._last_scan = {
            "cand_n": scan_cand_total,
            "cand_size_ok": scan_cand_size_ok,
            "purity_rej": scan_purity_rej,
            "med_h": scan_best_med_h,
            "med_s": scan_best_med_s,
            "zone": best.zone if best.found else "",
        }
        return best

    def _measure_track(self, frame_bgr: np.ndarray, match: "_MatchResult") -> _TrackMetrics:
        """Measure fill + green relative to the FULL meter track.

        The track is bottom-anchored at the fill bottom (stable during a shot)
        and topped by the green window. fill_pct is how far the fill top has
        risen from the bottom toward the green at the top:
            fill_pct = (track_bottom - fill_top) / (track_bottom - track_top) * 100
        Green is scanned over the full track column (above the fill) and mapped
        into the same 0..100 track scale (so the green window lands near the top,
        ~88-100%, matching release_threshold ~93)."""
        tm = _TrackMetrics()
        if frame_bgr is None or frame_bgr.size == 0 or not match.found:
            tm.rejection_reason = "no_match"
            return tm

        H, W = frame_bgr.shape[:2]
        mx, my = int(round(match.x)), int(round(match.y))
        mw, mh = int(match.w), int(match.h)
        fill_bottom = my + mh          # track bottom anchor (stable during a shot)
        fill_top = my                  # current fill level (rises toward green)

        # Full-track scan height from the style contour h_max, scaled to frame.
        sy = H / float(self._JSON_H)
        h_max_style = 182.0
        try:
            st = getattr(match, "style", None)
            if isinstance(st, dict):
                h_max_style = float(st.get("contour", {}).get("h_max", 182))
        except Exception:
            h_max_style = 182.0
        full_h_est = max(float(mh), h_max_style * max(0.2, sy))
        scan_h = int(round(full_h_est * 1.25))
        col_pad = max(3, int(round(mw * 0.45)))
        cx1 = max(0, mx - col_pad)
        cx2 = min(W, mx + mw + col_pad)
        cy_top = max(0, fill_bottom - scan_h)

        # FILL-TOP COLUMN GUARD (2026-07-04 live batch): a specular flash / split contour can
        # momentarily drop the matched contour's TOP by 30-50px — a numerator-only under-read that
        # published 1-2-frame fill dips of -22..-28pts mid-rise in 11/13 live episodes (the bottom
        # was already notch-stable via the box-track), spiking the native velocity estimator. With
        # the box-track glued this frame ("accept"), re-measure the fill top from the RED COLUMN
        # inside the served box and take the higher edge; the contour stays authoritative whenever
        # it already spans the column. Box-track-gated: classical/park path byte-identical.
        if (self._box_track is not None and self._box_track.ready
                and getattr(self._box_track, "last_event", "") == "accept" and mh >= 8
                and os.environ.get("ORION_METER_FILLTOP_GUARD", "1") != "0"):
            try:
                _fx1 = max(0, mx - 1); _fx2 = min(W, mx + mw + 1)
                _fy1 = cy_top; _fy2 = min(H, fill_bottom)
                if (_fx2 - _fx1) >= 4 and (_fy2 - _fy1) >= 8:
                    _fhsv = cv2.cvtColor(frame_bgr[_fy1:_fy2, _fx1:_fx2], cv2.COLOR_BGR2HSV)
                    if self._cfg.meter_color == "Red":
                        _fH, _fS, _fV = _fhsv[..., 0], _fhsv[..., 1], _fhsv[..., 2]
                        _fmask = ((_fH <= 10) | (_fH >= 168)) & (_fS >= 110) & (_fV >= 90)
                    else:
                        _fmask = cv2.inRange(_fhsv, np.array(match.hsv_lo, np.uint8),
                                             np.array(match.hsv_hi, np.uint8)) > 0
                    _rows = _fmask.sum(axis=1)
                    # a REAL fill edge is a solid block: demand 60% width on 2 CONSECUTIVE rows
                    # (single-row pan-blur smear above the fill passed a looser 50% test and
                    # over-read fill on the 720p clips), and bound the lift to the dip magnitude
                    # actually observed live (30-50px on a 164px track).
                    _g = _rows >= max(3, int(0.6 * mw))
                    _good = np.flatnonzero(_g[:-1] & _g[1:])
                    if _good.size:
                        _col_top = _fy1 + int(_good[0])
                        # MIN-LIFT floor: only repair SUBSTANTIAL contour-split under-reads (the
                        # live dips were 30-50px on a 164px track). Small borderline lifts toggle
                        # frame-to-frame and sawtooth the published fill (clip 212008 fill-mono
                        # 99.5->96.4 when they were allowed).
                        _min_lift = max(8, int(0.08 * full_h_est))
                        _lift_floor = fill_top - int(0.35 * full_h_est)
                        if _lift_floor <= _col_top <= fill_top - _min_lift:
                            fill_top = _col_top
            except Exception:
                pass

        # SUB-PIXEL FILL-TOP (2026-07-06): the served locator/park-path fill_top is an INTEGER box/
        # column edge, so track.fill_pct is pixel-QUANTIZED (~1 row = 0.5-1% on a 720p track). That
        # quantization is what feeds _stability.validate + the native velocity estimator -> tip
        # prediction -> release jitter. Sharpen the fill-top to sub-pixel by LINEAR half-max crossing
        # of the served box's red-column row profile (red px/row rising from ~0 above the fill to full
        # below it). NB: the classical subpixel_fill_metrics' _gaussian_threshold_crossing adds a
        # PARABOLIC peak-refinement that is tuned for a bump/flank and SNAPS a monotone fill edge to
        # half-integers (measured: it REDUCES fill resolution here), so this uses the plain linear
        # crossing -- genuinely sub-pixel + monotone on a rising edge. NUMERATOR-ONLY: fill_top stays
        # the integer edge for green sanity / slicing / fill_top_row; only the fill_pct crossing is
        # sharpened, accepted only within ~3 rows of the edge (a sharpening, never a relocation).
        # No model, no sim2real. Env-gated (ORION_METER_SUBPIXEL_FILL, default on).
        fill_top_f = float(fill_top)
        if (os.environ.get("ORION_METER_SUBPIXEL_FILL", "1") != "0"
                and mw >= 4 and (fill_bottom - cy_top) >= 8):
            try:
                _sx1 = max(0, mx); _sx2 = min(W, mx + mw)
                _sy1 = max(0, cy_top); _sy2 = min(H, fill_bottom)
                if (_sx2 - _sx1) >= 4 and (_sy2 - _sy1) >= 8:
                    _shsv = cv2.cvtColor(frame_bgr[_sy1:_sy2, _sx1:_sx2], cv2.COLOR_BGR2HSV)
                    if self._cfg.meter_color == "Red":
                        _sH, _sS, _sV = _shsv[..., 0], _shsv[..., 1], _shsv[..., 2]
                        _smask = (((_sH <= 10) | (_sH >= 168)) & (_sS >= 110)
                                  & (_sV >= 90)).astype(np.float32)
                    else:
                        _smask = (cv2.inRange(_shsv, np.array(match.hsv_lo, np.uint8),
                                              np.array(match.hsv_hi, np.uint8)) > 0).astype(np.float32)
                    _prof = _smask.sum(axis=1)   # red px per row, top->bottom of the scan column
                    _wcol = float(_sx2 - _sx1)
                    if _prof.size >= 3 and float(_prof.max()) >= max(3.0, 0.4 * _wcol):
                        _thr = max(2.0, min(0.6 * _wcol, 0.5 * float(_prof.max())))
                        # first rising linear crossing of _thr, top->bottom == the sub-pixel fill top.
                        _cross = -1.0
                        for _ri in range(1, int(_prof.size)):
                            _a = float(_prof[_ri - 1]); _b = float(_prof[_ri])
                            if _a < _thr <= _b:
                                _cross = (_ri - 1) + (_thr - _a) / max(1e-9, _b - _a)
                                break
                        if _cross >= 0.0:
                            _cand = float(_sy1) + float(_cross)
                            if abs(_cand - float(fill_top)) <= 3.0:
                                fill_top_f = _cand
            except Exception:
                pass

        # ZOOM STALENESS GUARD (2026-07-03 MyCourt): the remembered full-height + green offsets were
        # learned at a PAST camera scale and are only cleared on a colour/style reset — a zoom carried
        # them across, corrupting the fill% denominator even with a perfect box. The candidate WIDTH
        # is a fill-independent scale proxy (the bar doesn't widen as it fills): >30% width divergence
        # vs learn-time means the scale changed -> forget the px memories and re-learn from the next
        # real green sighting (the fresh style estimate serves meanwhile).
        if (self._track_full_h > 8.0 and self._track_full_h_w > 2.0
                and abs(float(mw) - self._track_full_h_w) > 0.30 * self._track_full_h_w):
            self._track_full_h = 0.0
            self._track_full_h_w = 0.0
            self._track_green_off = None
            self._track_green_left = 0

        # --- locate the green band over the full track column (above the fill) ---
        green_top_abs = green_bottom_abs = -1
        green_conf = 0.0
        green_cluster = 0
        green_found = False
        green_remembered = False
        span = self._green.green_span_abs(frame_bgr, cx1, cy_top, cx2, fill_bottom)
        if span is not None:
            gt, gb, gcluster, gconf = span
            # Sanity: green is the target at the TOP, so it must sit at/above the
            # current fill level. A green hit well below fill_top is fill-edge or
            # HUD noise (this is what produced bogus green at ~1-4%). The absolute
            # near-top floor is applied AFTER the track scale is known (below), in the
            # same %% the engine consumes, so an inflated bbox can't slip a mid-track
            # green through a separate height estimate.
            if gt <= fill_top + max(4, int(mh * 0.30)):
                green_top_abs, green_bottom_abs = gt, gb
                green_conf, green_cluster = gconf, gcluster
                green_found = True
                self._track_green_off = (fill_bottom - gt, fill_bottom - gb)
                self._track_green_left = 20
        if not green_found and self._track_green_off is not None and self._track_green_left > 0:
            top_off, bot_off = self._track_green_off
            green_top_abs = fill_bottom - int(top_off)
            green_bottom_abs = fill_bottom - int(bot_off)
            self._track_green_left -= 1
            green_conf = 0.40
            green_remembered = True
            green_found = green_top_abs >= 0
        elif not green_found:
            self._track_green_left = max(0, self._track_green_left - 1)

        # --- track geometry ---
        if green_top_abs >= 0:
            track_top = float(green_top_abs)
        elif self._track_full_h > 8.0:
            track_top = float(max(0, fill_bottom - int(round(self._track_full_h))))
        elif self._box_track is not None and self._box_track.ready_h:
            # MeterBoxKalman fallback: the smoothed full height survives a zoom-guard wipe of the
            # remembered denominator until the next green sighting — strictly closer to the true
            # scale than the frame-scaled style estimate below.
            track_top = float(max(0, fill_bottom - int(round(self._box_track.height()))))
        else:
            track_top = float(max(0, fill_bottom - int(round(full_h_est))))
        # The fill top can never be above the track top by definition.
        track_top = min(track_top, float(fill_top))
        track_height = max(8.0, float(fill_bottom) - track_top)
        if green_found and not green_remembered:
            # Learn the authoritative full height from a real green sighting (+ the width it was
            # learned at — the zoom-staleness proxy above).
            self._track_full_h = track_height
            self._track_full_h_w = float(mw)
            if self._box_track is not None:
                # Only REAL green sightings feed the height channel (a fallback-derived height would
                # be circular). Slow-adapting: jitter-free but allowed to shrink on zoom-out.
                self._box_track.update_height(track_height, time.perf_counter())

        # Sub-pixel fill-top in the numerator (fill_top_f == float(fill_top) when the sharpening
        # is off / rejected, so this is byte-identical unless the crossing refined the edge).
        fill_pct = max(0.0, min(100.0, (float(fill_bottom) - fill_top_f) / track_height * 100.0))

        tm.fill_pct = round(fill_pct, 3)
        tm.fill_top_row = fill_top
        tm.track_top_row = int(round(track_top))
        tm.track_bottom_row = int(fill_bottom)
        tm.track_height = track_height
        if green_found and green_top_abs >= 0 and green_bottom_abs >= 0:
            gs = max(0.0, min(100.0, (float(fill_bottom) - float(green_bottom_abs)) / track_height * 100.0))
            ge = max(0.0, min(100.0, (float(fill_bottom) - float(green_top_abs)) / track_height * 100.0))
            if ge < gs:
                gs, ge = ge, gs
            gc = (gs + ge) * 0.5
            gw = ge - gs
            # Absolute near-top floor, IN THE PUBLISHED TRACK SCALE the engine consumes: the green
            # chevron sits at the very top (~93-100%). Reject a green whose CENTER maps below the
            # floor, or whose band spans more than the max width. A track-spanning band is fill-edge
            # / HUD noise; a low-but-narrow band means an inflated bbox dropped the track bottom and
            # mapped the REAL top chevron to mid-track. Either is unusable as a release target -> we
            # publish green_not_found so the engine times the TIP instead of aiming mid-track. fill %
            # is untouched. (Live 2026-06-07: bogus green centers 27-56% on full meters drove the
            # engine's release target mid-track and fired fades far too early -> EARLY-railed.)
            if gc < (self._cfg.green_center_floor_frac * 100.0) or gw > (self._cfg.green_max_height_frac * 100.0):
                tm.rejection_reason = "green_not_found"
            else:
                tm.green_found = True
                tm.green_start_pct = round(gs, 3)
                tm.green_end_pct = round(ge, 3)
                tm.green_center_pct = round(gc, 3)
                tm.green_width_pct = round(gw, 3)
                tm.green_confidence = round(green_conf, 4)
                tm.green_cluster_px = green_cluster
                tm.green_top_row = int(green_top_abs)
                tm.green_bottom_row = int(green_bottom_abs)
                tm.green_remembered = green_remembered
        else:
            tm.rejection_reason = "green_not_found"
        return tm

    def _green_first_locate(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """GREEN-CHEVRON-FIRST meter locator (empirically spec'd from 372 confirmed detections,
        1080p+720p). The meter's neon-GREEN upward CHEVRON sits FIXED at the track TOP and is
        present at full size/colour from the INSTANT the meter appears (measured at <10% fill) —
        so locking on it gives acquisition from frame 0 (this refutes the old "green only at high
        fill" belief, which was a degraded-live-capture artifact). The red fill rises from a
        notched track BOTTOM, so at low fill the red is a stub at the bottom of the column, NOT
        capping the chevron — we therefore anchor on the chevron, project the track column DOWN,
        and confirm red ANYWHERE in it. Requiring this chevron above an x-centred, same-width red
        column removes ~65% of red false-locks (jerseys/logos/sponsor-red have no chevron). All red
        gates are RELATIVE to the detected chevron width (the meter scales ~2x with player/embed
        size). Returns the track bbox (x, y, w, h) or None."""
        try:
            col = str(getattr(self._cfg, "meter_color", "") or "")
            if col not in ("Red", "Purple"):
                return None
            H, W = frame_bgr.shape[:2]
            hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
            Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
            # Play band: below the top scoreboard HUD, above the floor/name-plate.
            y_top, y_bot = int(H * 0.12), int(H * 0.90)
            # GREEN chevron — TIGHT neon-green hue 52-66 (measured 55-64, centre 58, resolution-
            # invariant); sat/val floors tolerate antialiased edges. Hue this tight is what keeps
            # court-green/teal cosmetics out.
            green = (((Hh >= 52) & (Hh <= 66) & (Ss >= 90) & (Vv >= 120))).astype(np.uint8)
            green[:y_top] = 0; green[y_bot:] = 0
            if int(np.count_nonzero(green)) < 6:
                return None
            # RED fill — hue WRAPS 0/180; BOTH sides are mandatory (a single side under-measures).
            if col == "Red":
                red = ((((Hh <= 9) | (Hh >= 170)) & (Ss >= 110) & (Vv >= 110))).astype(np.uint8)
            else:
                red = (((Hh >= 138) & (Hh <= 162) & (Ss >= 110) & (Vv >= 110))).astype(np.uint8)
            red[:y_top] = 0; red[y_bot:] = 0
            g_area_min = 35 if W < 1600 else 60        # chevron area floor scales with resolution
            g = cv2.morphologyEx(green * 255, cv2.MORPH_CLOSE,
                                 cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
            cnts, _ = cv2.findContours(g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best = None; bscore = -1.0
            for cc in cnts:
                gx, gy, gw, gh = cv2.boundingRect(cc)
                area = float(cv2.contourArea(cc))
                if gw < 6 or gw > int(W * 0.05) or area < g_area_min:   # chevron width band 8-34px@1080p
                    continue
                # Compact arrowhead: wider-than-tall-ish (h ~0.3-1.1x w), solid (fill >= 0.45).
                if not (0.30 <= gh / max(1.0, gw) <= 1.15):
                    continue
                if area / max(1.0, float(gw * gh)) < 0.45:
                    continue
                cxg = gx + gw // 2
                gbot = gy + gh
                # PROJECT the track column DOWN from the chevron: x-centred, ~chevron width, up to
                # ~6x chevron-width tall (the full track ~5x; clamp to the play band).
                xr = max(3, int(gw * 0.8))
                track_h = min(int(H * 0.45), max(int(gw * 2), gw * 6))
                x0 = max(0, cxg - xr); x1 = min(W, cxg + xr + 1)
                colr = red[gbot:min(H, gbot + track_h), x0:x1]
                if colr.size == 0:
                    continue
                ys = np.flatnonzero(colr.any(axis=1))
                # The red must be PRESENT in the column (the bottom notch is always there, even at
                # ~0% fill) — but it need NOT cap the chevron (low fill = a stub far below).
                if int(np.count_nonzero(colr)) < max(4, gw) or ys.size < 2:
                    continue
                rtop = gbot + int(ys[0]); rbot = gbot + int(ys[-1])
                rmid = (rtop + rbot) // 2
                rw = int(np.count_nonzero(red[rmid, max(0, cxg - int(W * 0.03)):cxg + int(W * 0.03) + 1]))
                # Red bar must be THIN — an absolute frame-scaled band (~8-42px @1080p, 5-28 @720p).
                # NOT chevron-relative: at high fill the red overlaps and shrinks the visible green
                # (gw < rw), so a gw-relative ceiling false-rejects full meters. The chevron presence
                # + position already discriminate the meter; width only needs to exclude wide blobs.
                rw_min = max(3, int(W * 0.004)); rw_max = max(rw_min + 6, int(W * 0.022))
                if not (rw_min <= rw <= rw_max):
                    continue
                # DENSITY (replaces the buggy sat-purity gate, which averaged in the transparent
                # banner background and false-rejected real meters): the red must be a SOLID column,
                # i.e. most rows between its top and bottom carry red — a scattered red speckle does
                # not. Real meter density ~0.8+; a jersey/logo speckle under a stray green is sparse.
                rrows = np.flatnonzero(red[rtop:rbot + 1, x0:x1].any(axis=1))
                if (rbot - rtop) > 4 and rrows.size / float(rbot - rtop + 1) < 0.6:
                    continue
                # The track spans the green TOP down to the red-notch bottom (fixed per shot).
                top = gy
                # Prefer the most red-complete column (the real meter); slight 1080p tie-break.
                score = float(ys.size) + (2.0 if W >= 1600 else 0.0)
                if score > bscore:
                    bscore = score
                    best = (max(0, cxg - max(rw, gw) // 2), int(top),
                            max(6, max(rw, gw)), max(8, int(rbot - top + 1)))
            return best
        except Exception:
            return None

    def _red_bounds(self):
        """BGR+HSV bounds for the RED park meter (cached) — feeds the direct-emit match's
        subpixel_fill_metrics / _measure_track, mirroring what _color_detect would set."""
        if self._red_bounds_cache is None:
            lo = np.array(BGR_COLOR_RANGES["Red"][0], dtype=np.uint8)
            hi = np.array(BGR_COLOR_RANGES["Red"][1], dtype=np.uint8)
            hlo, hhi = _style_bgr_bounds_to_hsv(lo, hi)
            self._red_bounds_cache = (lo, hi, hlo, hhi)
        return self._red_bounds_cache

    def _update_rise_state(self, served: bool, fill: float, ts: float,
                           bbox: Optional[Tuple[int, int, int, int]]) -> None:
        """Track the served meter's fill trajectory (A1). Distinguishes a still-RISING live meter
        from a SPENT one (the previous shot's meter lingering at ~full/deflating). "spent" is the
        trigger for carryover preemption in detect(). Resets on a served gap (>0.6s) or a served
        position jump beyond ~1.5x the acquire jump gate (a genuinely different meter = new episode)."""
        gate = self._stability._acq_jump_gate_px()
        reset = bool(self._last_served_ts) and (ts - self._last_served_ts) > 0.6
        if served and self._last_served_bbox is not None and bbox is not None:
            d = float(np.hypot(
                (bbox[0] + bbox[2] * 0.5) - (self._last_served_bbox[0] + self._last_served_bbox[2] * 0.5),
                (bbox[1] + bbox[3] * 0.5) - (self._last_served_bbox[1] + self._last_served_bbox[3] * 0.5)))
            if d > gate * 1.5:
                reset = True
        if reset:
            self.last_rise_state = ""
            self._served_peak_fill = 0.0
            self._rise_noninc = 0
            self._rise_prev_fill = -1.0
        if not served:
            return
        prev = self._rise_prev_fill
        self._served_peak_fill = max(self._served_peak_fill, fill)
        if prev >= 0.0:
            self._rise_noninc = self._rise_noninc + 1 if fill <= prev + 0.3 else 0
        self._rise_prev_fill = fill
        self._last_served_bbox = bbox
        self._last_served_ts = ts
        # spent: reached ~full then stopped climbing for ~8 served frames (~130ms @60fps) = the
        # post-shot meter lingering/deflating. peak: at/near full and still fresh. else rising.
        if self._served_peak_fill >= 90.0 and self._rise_noninc >= 8:
            self.last_rise_state = "spent"
        elif fill >= 92.0:
            self.last_rise_state = "peak"
        else:
            self.last_rise_state = "rising"

    def _box_track_confirm_bar(self, frame_bgr, pcx, pcb, pw, hsv_bands=None):
        """Is a meter-like red bar STILL at the box-track's predicted spot? Returns the locally
        measured (cx, cb, w, bar_h) or None. Called only on box-track "reject" frames: the raw
        match jumped far away (live case: the post-release green-flame VFX promotes a stable blob
        above the meter) while the meter itself usually remains exactly where the track says.
        Confirming from PIXELS — instead of trusting jump geometry — lets the tracker keep
        rejecting a stationary distractor forever (its re-seed strikes reset on every confirmed
        frame) yet still adopt a GENUINE relocation (old spot empty -> no confirm -> 3 consistent
        strikes re-seed). Cost: one cvtColor on a ~3w x 1.4h crop, reject frames only."""
        try:
            fh, fw = frame_bgr.shape[:2]
            bt = self._box_track
            h_ref = float(bt.height()) if bt.ready_h else max(60.0, 3.5 * float(pw))
            x0 = int(max(0.0, pcx - 1.5 * pw)); x1 = int(min(float(fw), pcx + 1.5 * pw + 1.0))
            y0 = int(max(0.0, pcb - 1.25 * h_ref)); y1 = int(min(float(fh), pcb + 0.15 * h_ref + 1.0))
            if (x1 - x0) < 4 or (y1 - y0) < 12:
                return None
            hsv = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
            if self._cfg.meter_color == "Red":
                Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
                mask = (((Hh <= 10) | (Hh >= 168)) & (Ss >= 110) & (Vv >= 90)).astype(np.uint8)
            elif hsv_bands is not None:
                lo = np.array(hsv_bands[0], np.uint8); hi = np.array(hsv_bands[1], np.uint8)
                mask = (cv2.inRange(hsv, lo, hi) > 0).astype(np.uint8)
            else:
                return None
            colsum = mask.sum(axis=0).astype(np.float32)
            good = colsum >= max(6.0, 0.25 * h_ref)   # deflating post-release bar still qualifies
            idx = np.flatnonzero(good)
            if idx.size == 0:
                return None
            splits = np.flatnonzero(np.diff(idx) > 1)
            run = max(np.split(idx, splits + 1), key=len)
            rw = float(run[-1] - run[0] + 1)
            if rw < max(3.0, 0.40 * pw) or rw > 2.4 * pw:
                return None
            ys = np.flatnonzero(mask[:, run[0]:run[-1] + 1].any(axis=1))
            if ys.size == 0:
                return None
            cb_local = float(y0) + float(ys[-1]) + 1.0
            if abs(cb_local - pcb) > max(12.0, 0.25 * h_ref):
                return None   # red present but its BOTTOM is off the notch -> not our bar
            cx_local = float(x0) + (float(run[0]) + float(run[-1]) + 1.0) * 0.5
            bar_h = max(4.0, float(ys[-1]) - float(ys[0]) + 1.0)
            return (cx_local, cb_local, rw, bar_h)
        except Exception:
            return None

    def detect(self, frame_bgr: np.ndarray, ts: Optional[float] = None) -> DetectResult:
        # ts (perf_counter seconds) is normally self-stamped, but an OFFLINE replay harness passes the
        # ORIGINAL capture timestamp so it can re-inject DUPLICATE + stall-gap frames faithfully — the
        # production framedump stores only UNIQUE frames, so a self-stamped clock makes stalls invisible
        # offline (RC-4: why last round's offline-green fixes died live). Wall-clock temporal holds
        # (post-peak coast, box-track hold) then behave exactly as they did live. Default = live behaviour.
        ts = time.perf_counter() if ts is None else float(ts)
        empty = DetectResult(False, "", self._cfg.meter_color, (0, 0, 0, 0), 0.0, 0.0, 0, aborted=self._stability.aborted)
        if frame_bgr is None or frame_bgr.size == 0:
            return empty

        # Pre-crop to the locked ROI when available (~80x fewer pixels to convert).
        orig_h, orig_w = frame_bgr.shape[:2]

        # Auto meter-COLOUR: scan candidates + lock the winning colour.
        # Runs only while unlocked; once locked it just pins self._cfg.meter_color so
        # the rest of detection uses the right colour. Prevents the silent
        # wrong-meter_color failure. No-op unless cfg.auto_meter_color is on.
        if bool(getattr(self._cfg, "park_temporal_enabled", False)):
            # Park IS the RED meter: PIN Red + never auto-thrash the colour. Auto-color flipping
            # meter_color away from "Red" would flip park_active OFF -> the old false-locking path.
            # Defense-in-depth even if a settings reload re-enabled auto_meter_color.
            self._cfg.meter_color = "Red"
            self._cfg.auto_meter_color = False
        elif getattr(self._cfg, "auto_meter_color", False):
            self._auto_color_step(frame_bgr, (orig_w, orig_h))

        # Trained TEMPLATE ANCHOR (meter LOCATION) takes precedence when armed. The
        # glyph match locates the meter region; the colour scan is constrained to it,
        # so a cosmetic red/orange blob elsewhere on screen is never considered (the
        # live false-lock). The anchored region is AUTHORITATIVE — on a colour miss
        # inside it we do NOT fall back to a full-frame scan (which is what re-admits
        # the blob); the meter simply isn't up yet. When the anchor is not armed, or
        # not found this frame, detection behaves exactly as before (additive).
        # Park gating is Red-meter-specific (the park shot meter is RED). Scoping it to Red here
        # means a Purple/other config is never routed through the red-only authoritative park path
        # (it would suppress detection of a non-red meter), so non-park detection is unaffected.
        park_active = bool(getattr(self._cfg, "park_temporal_enabled", False)) and str(getattr(self._cfg, "meter_color", "")) == "Red"
        # Trained YOLO LOCATOR (models/orion_meter_n.pt, ORION_METER_LOCATOR=1): when it fires it is
        # AUTHORITATIVE, taking precedence over BOTH the park temporal search AND the classical anchor.
        # It learned the meter's appearance, so it finds the player-attached meter ANYWHERE and a red
        # distractor (jersey/UI/court) elsewhere is NEVER scanned -> the "locks onto random objects" fix.
        # Same crop->constrained _color_detect->_measure_track path as the anchor (fill%/green read below).
        # Run before park (T7): its result gates the optional park-skip below. This reorder is a no-op with
        # the skip OFF — _locate_meter and _park.update touch disjoint state (proven byte-identical).
        loc_active = self._locator is not None and getattr(self._locator, "enabled", False)
        loc_box = self._locate_meter(frame_bgr, orig_w, orig_h, ts) if loc_active else None
        # A1 runs the park temporal tracker EVERY frame so its rise-confirm machinery gets consecutive
        # frames (the park-serves-0 fix). T7 (EXPERIMENTAL, _PARK_LOCK_SKIP; DEFAULT OFF) optionally skips
        # it on a CONFIRMED locator lock — see the flag's block comment for the measured serving-cascade
        # caveat. With the flag off park_skip stays False and park runs every frame exactly as before.
        served_recent = (self._serve_ts >= 0.0) and ((ts - self._serve_ts) <= _LOC_ROI_SERVE_S)
        bt_holding = self._box_track is not None and bool(getattr(self._box_track, "ready", False))
        park_skip = False
        if (_PARK_LOCK_SKIP and park_active and loc_box is not None
                and (served_recent or bt_holding) and self.last_rise_state != "spent"):
            self._park_skip_streak += 1
            if self._park_skip_streak >= _PARK_RECHECK:
                self._park_skip_streak = 0          # periodic re-check: run park to keep it warm
            else:
                park_skip = True
        else:
            self._park_skip_streak = 0
        # REUSED in the fall-through below; calling update() twice per frame double-advances it, so the
        # fall-through only re-runs it when it was skipped here (park_skip and park_rect is None).
        park_rect = None if park_skip else (self._park.update(frame_bgr, ts) if park_active else None)
        # A1 spent-carryover preemption: after a shot the previous meter LINGERS (fill ~100, panning)
        # and the locator's argmax keeps returning IT -> the NEW rising meter never gets the served slot
        # (batch-2: acquired ~350ms late). When the served track is SPENT and park has confirmed a fresh
        # meter at a clearly DIFFERENT position, drop the carryover box + region-lock so the fall-through
        # park path serves the new one instead (park only emits post rise-confirm, so it corroborates).
        if (park_active and park_rect is not None and self.last_rise_state == "spent"
                and loc_box is not None):
            _pcx, _pcy = park_rect[0] + park_rect[2] * 0.5, park_rect[1] + park_rect[3] * 0.5
            _lcx, _lcy = loc_box[0] + loc_box[2] * 0.5, loc_box[1] + loc_box[3] * 0.5
            if float(np.hypot(_pcx - _lcx, _pcy - _lcy)) > self._stability._acq_jump_gate_px() * 1.5:
                loc_box = None
                self._loc_mem_box = None
                self._loc_miss_streak = 0
                if self._box_track is not None:
                    # A confirmed NEW meter is being adopted — drop the old geometry track immediately
                    # instead of burning 3 outlier-rejects to re-seed onto it. The track-hold must die
                    # with it: holding the spent bar would undo this preemption.
                    self._box_track.reset()
                    self._bt_hold_match = None
                    self._bt_hold_until = 0.0
        # Temporal region-lock: TRUE only on a locator-MISS frame where we reuse the remembered
        # box (distinguished from a live hit in last_debug["zone"] below). Always defined so the
        # debug dict is safe on every path; only ever True when the locator is active.
        loc_mem_used = False
        # T5: TRUE when this frame's match came from the park-branch wide-scan FALLBACK. The park
        # path is authoritative ("no confirmed meter -> no detection"), so an UNCORROBORATED
        # fallback candidate is demoted back to not-found after validation below — it may only
        # surface once the stability validator corroborates it (rise / green). Distractor-only
        # scenes therefore still read not-detected, exactly like pre-T5.
        park_fb_candidate = False
        # Sub-pixel READER crop (model A): the full-meter located crop, captured ONLY in the branches
        # where detect_frame IS a located meter box (locator / region-lock / anchor) — never the park
        # fill-rect (that would read ~100%). Consumed after _measure_track below; inert unless enabled.
        reader_crop = None
        # LOCATOR is NON-AUTHORITATIVE (2026-07-02 live-test fix). Live evidence: the trained YOLO locator
        # has ~0% recall on the real moving meter (fired 0 times across 29 real shots) and occasionally boxes
        # a STATIC distractor; being authoritative-on-miss it SUPPRESSED the proven park detector for ~60
        # frames = the user's "disappears while moving". So a located/remembered box is used ONLY when its
        # colour scan actually FINDS a meter; on a MISS we fall through to the park/anchor workhorse (which
        # has its own distractor rejection). Flag-safe: loc_active is False when the locator is off, so
        # loc_handled stays False and the classical path is byte-for-byte unchanged.
        loc_handled = False
        if loc_box is not None:
            # LIVE locator hit -> track box-centre velocity (px/frame over the gap since the last hit) +
            # refresh the region-lock memory, then colour-scan the located crop.
            cx = loc_box[0] + loc_box[2] * 0.5
            cy = loc_box[1] + loc_box[3] * 0.5
            self._loc_miss_streak = 0
            gap = max(1, self._loc_mem_age + 1)
            # An EVERY-skip frame reuses the identical box; feeding its zero motion into the EMA
            # damps the velocity and lags real pans — only update on a genuinely new box.
            if self._loc_mem_prev_c is not None and (cx, cy) != self._loc_mem_prev_c:
                self._loc_mem_vx = 0.5 * self._loc_mem_vx + 0.5 * (cx - self._loc_mem_prev_c[0]) / gap
                self._loc_mem_vy = 0.5 * self._loc_mem_vy + 0.5 * (cy - self._loc_mem_prev_c[1]) / gap
            self._loc_mem_prev_c = (cx, cy)
            self._loc_mem_box = (int(loc_box[0]), int(loc_box[1]), int(loc_box[2]), int(loc_box[3]))
            self._loc_mem_age = 0
            crop_x, crop_y, detect_frame = self._anchor_detect_crop(frame_bgr, loc_box)
            yolo_conf = float(getattr(self._locator, "last_conf", 0.0) or 0.0)
            # Track T-b: a STRONG v6 box is AUTHORITATIVE — relax the colour scan's classical size/purity
            # gates inside it (box_constrained), and if the scan still finds nothing, emit FROM the box
            # rather than suppressing the located meter. v6 recall is ~90% on the real moving meter; of the
            # in-shot recall holes 105/108 HAD red the classical confirm rejected.
            loc_strong = yolo_conf >= _LOC_STRONG_CONF
            match = self._color_detect(detect_frame, force_full_zone=True, scale_wh=(orig_w, orig_h),
                                       box_constrained=loc_strong)
            if match.found:
                anchor_loc = loc_box
                anchor_found = True
                reader_crop = detect_frame
                cropped = (crop_x > 0 or crop_y > 0)
                self._anchor_clear_stale(loc_box)
                if cropped:
                    match.x += float(crop_x)
                    match.y += float(crop_y)
                # Blend the REAL YOLO box score into the emitted confidence: two independent witnesses —
                # the learned locator (shape/context) and the colour scan (photometry). For a STRONG box
                # (Track T-b1) take the MAX of the colour score and a yolo-WEIGHTED blend — the old flat
                # 0.5/0.5 HALVED v6's ~0.9 toward the trust floor and tripped low_conf_reset. A weak box
                # keeps the conservative 0.5/0.5 blend so a lucky red blob under it stays low. Geometry
                # (w/h) stays the COLOUR contour's own measurement (the YOLO box widths were fake).
                if yolo_conf > 0.0:
                    if loc_strong:
                        match.confidence = min(0.98, max(match.confidence, 0.75 * yolo_conf + 0.25 * match.confidence))
                        match.loc_strong = True
                    else:
                        match.confidence = min(0.98, 0.5 * match.confidence + 0.5 * yolo_conf)
                self._last_served_w = float(match.w)
                loc_handled = True
            else:
                # Colour scan found no clean meter inside the box -> emit FROM the box (T-b2). A genuinely
                # empty/no-red box returns None here and still falls through to the park/anchor path.
                # T5-b (2026-07-05): WEAK boxes (0.45..0.55 live) get the same red-gated fallback — the
                # old `elif loc_strong` discarded them outright when their scan missed, which blacked out
                # a locator-detected edge-fade meter. strong=False keeps the validator's corroboration
                # honest (no loc_strong privilege), so a weak emit still can't latch uncorroborated.
                # _LOC_WEAK_FB_CONF floors the path so genuine junk boxes keep falling through to park.
                fb = (self._box_fallback_match(detect_frame, loc_box, yolo_conf, strong=loc_strong)
                      if (loc_strong or yolo_conf >= _LOC_WEAK_FB_CONF) else None)
                if fb is not None:
                    match = fb
                    anchor_loc = loc_box
                    anchor_found = True
                    reader_crop = detect_frame
                    cropped = False   # match is emitted at absolute box coords (no crop offset to add)
                    self._anchor_clear_stale(loc_box)
                    self._last_served_w = float(match.w)
                    loc_handled = True
                # else: the box held no red at all -> fall through to the park/anchor path below
                #       (its own distractor rejection is authoritative there).
        elif loc_active and self._loc_mem_box is not None and self._loc_mem_age < _LOC_MEM_TTL_FRAMES:
            # Locator MISS but the last hit is still FRESH: reuse the remembered box, EXTRAPOLATED along the
            # tracked box velocity + widened with age (motion-aware), so a meter that MOVED since the hit
            # stays inside the scan. Non-authoritative too -> a colour miss falls through to park.
            self._loc_mem_age += 1
            self._loc_miss_streak += 1
            if self._loc_miss_streak == 3:
                # A pan that STOPPED must not keep flinging the extrapolated crop along the old
                # velocity for the rest of the TTL — after 3 straight misses, coast in place.
                self._loc_mem_vx = 0.0; self._loc_mem_vy = 0.0
            mem_box = self._loc_mem_expand(self._loc_mem_box, orig_w, orig_h,
                                           self._loc_mem_age, self._loc_mem_vx, self._loc_mem_vy)
            crop_x, crop_y, detect_frame = self._anchor_detect_crop(frame_bgr, mem_box)
            # Carried YOLO score DECAYED by miss-age (a long coast reads progressively less trustworthy).
            # A still-STRONG coast relaxes the box gates + corroborates like a live strong hit (Track T-b);
            # a stale one stays conservative, and the size-consistency guard rejects a swallowed sliver.
            _mem_yolo = float(getattr(self._locator, "last_conf", 0.0) or 0.0) * (0.90 ** self._loc_mem_age)
            _mem_strong = _mem_yolo >= _LOC_STRONG_CONF
            match = self._color_detect(detect_frame, force_full_zone=True, scale_wh=(orig_w, orig_h),
                                       box_constrained=_mem_strong)
            if match.found and self._loc_mem_size_ok(match.w):
                loc_mem_used = True
                anchor_loc = mem_box
                anchor_found = True
                reader_crop = detect_frame
                cropped = (crop_x > 0 or crop_y > 0)
                self._anchor_clear_stale(mem_box)
                if cropped:
                    match.x += float(crop_x)
                    match.y += float(crop_y)
                if _mem_yolo > 0.0:
                    if _mem_strong:
                        match.confidence = min(0.98, max(match.confidence, 0.75 * _mem_yolo + 0.25 * match.confidence))
                        match.loc_strong = True
                    else:
                        match.confidence = min(0.98, 0.5 * match.confidence + 0.5 * _mem_yolo)
                self._last_served_w = float(match.w)
                # RE-SEED the region-lock on the colour-confirmed position: the remembered YOLO box is
                # stale by construction during a coast — recentre it on the column the scan actually
                # FOUND (x on the column centre; the box bottom anchored at the stable fill bottom) so
                # the crop stays glued through a pan instead of drifting off on old velocity. Size keeps
                # the remembered YOLO dims; age keeps counting so the TTL still bounds a blind ride.
                fx = float(match.x) + float(match.w) * 0.5
                fb = float(match.y) + float(match.h)
                bw = int(self._loc_mem_box[2]); bh = int(self._loc_mem_box[3])
                self._loc_mem_box = (int(fx - bw * 0.5), int(fb - bh), bw, bh)
                self._loc_mem_prev_c = (fx, fb - bh * 0.5)
                loc_handled = True

        if not loc_handled:
            if park_active:
                # T7 lazy park (only reachable with _PARK_LOCK_SKIP on): we skipped the park pass upfront
                # for a locator lock but the locator did NOT handle this frame (its box held no red). Pay
                # the pass NOW so the park emit + T5 fall-through match the always-on behaviour on the rare
                # frame that needs it. park_skip guards the double-advance (park_rect is None here only
                # because the upfront pass was skipped); with the flag off park_skip is always False.
                if park_skip and park_rect is None:
                    park_rect = self._park.update(frame_bgr, ts)
                    self._park_skip_streak = 0
                # PARK: emit the temporally-confirmed RED-FILL rect DIRECTLY, exactly like the proven
                # standalone (RED_LIVE_NEON). NO _color_detect / green-first / stability re-acquire — that
                # downstream re-admitted park distractors inside the crop and the meter_memory echo
                # amplified a 1-frame slip into a 40-frame lock. _measure_track still reads fill%/green
                # from this rect below. No confirmed park meter this frame -> not found (no fallback scan).
                # REUSE the (top-of-frame or lazily-run) park_rect -- do not re-update or it double-advances.
                anchor_loc = park_rect
                anchor_found = park_rect is not None
                crop_x = crop_y = 0
                cropped = False
                if park_rect is not None:
                    self._anchor_clear_stale(park_rect)
                    _blo, _bhi, _hlo, _hhi = self._red_bounds()
                    match = _MatchResult(
                        # 2026-07-02: REAL derived confidence (was a 0.95 constant, which pinned every
                        # park emit at max trust and disabled the validator's conf gates live).
                        found=True, template_name=str(self._active or "Arrow2"),
                        confidence=float(self._park.last_confidence),
                        x=float(park_rect[0]), y=float(park_rect[1]), w=int(park_rect[2]), h=int(park_rect[3]),
                        zone="park", bgr_lo=_blo, bgr_hi=_bhi, hsv_lo=_hlo, hsv_hi=_hhi,
                        med_h=-1.0, med_s=-1.0, green_confirmed=False,
                    )
                else:
                    # T5 (2026-07-05 edge-fade blackout): the locator didn't handle the frame AND park
                    # has no confirmed meter. Live (session 20260704_2108): on L/R-Fade pans the meter
                    # slides toward the screen edge and BOTH the learned locator and the park tracker
                    # miss for entire 600-800ms shots -> match.found stayed False -> 'roi_not_found' ->
                    # the engine fired blind (5/20 shots, detsummary fresh=0). Run the classical
                    # WIDE-band scan (+ its internal green-first fallback) as the last tier instead of
                    # giving up. This does NOT re-open the historical park false-lock: that was killed
                    # by the DOWNSTREAM acquisition-corroboration gate (a static ambiguous-red candidate
                    # stays fstate.valid=False -> 'bbox_unstable' -> never served, never fed, and never
                    # enters meter_memory, which only stores served results) — the absent scan was never
                    # the guard. A real mid-shot meter corroborates via its rising fill / green anchor
                    # within 2-3 frames and serves.
                    match = self._color_detect(frame_bgr, force_wide=True, scale_wh=(orig_w, orig_h))
                    park_fb_candidate = bool(match.found)
            else:
                anchor_loc = self._anchor.locate(frame_bgr) if self._anchor.enabled else None
                if anchor_loc is None and _GREEN_FIRST_ANCHOR:
                    # Dynamic green-first anchor: locate the meter by its unique green-over-red signature.
                    anchor_loc = self._green_first_locate(frame_bgr)
                anchor_found = anchor_loc is not None
                if anchor_loc is not None:
                    anchor_roi = (anchor_loc[0], anchor_loc[1], anchor_loc[2], anchor_loc[3])
                    self._anchor_clear_stale(anchor_roi)   # discard a held lock/echo far from the located region
                    crop_x, crop_y, detect_frame = self._anchor_detect_crop(frame_bgr, anchor_roi)
                    reader_crop = detect_frame
                    cropped = (crop_x > 0 or crop_y > 0)
                    match = self._color_detect(detect_frame, force_full_zone=True, scale_wh=(orig_w, orig_h))
                    if match.found and cropped:
                        match.x += float(crop_x)
                        match.y += float(crop_y)
                else:
                    crop_x, crop_y, detect_frame = self._get_detect_crop(frame_bgr)
                    cropped = (crop_x > 0 or crop_y > 0)
                    match = self._color_detect(detect_frame, force_full_zone=cropped, scale_wh=(orig_w, orig_h))
                    # Locked-crop miss must never cost a live detection -> immediately re-scan full frame.
                    if cropped and not match.found:
                        full = self._color_detect(frame_bgr, force_wide=True, scale_wh=(orig_w, orig_h))
                        if full.found:
                            match = full
                            crop_x = crop_y = 0
                            cropped = False
                    if match.found and cropped:
                        match.x += float(crop_x)
                        match.y += float(crop_y)

        # POST-RELEASE TRACK-HOLD (2026-07-04 batch #2): after the release the bar deflates and goes
        # STATIC — the wide-scan's rising-fill latch (correctly) refuses static red at COLD
        # acquisition, so one low-conf hiccup blacked out the still-VISIBLE carryover bar for 1-2s
        # (the user-visible "disappears during shots"; also starves the settle grader, greens=0).
        # With a WARM box-track, temporal continuity vouches for the bar: pixel-confirm it at the
        # tracked spot and synthesize this frame's match from the confirmed geometry — everything
        # downstream (pose smoothing, fill, validator, serve) runs the normal flow. The hold window
        # is refreshed only by LADDER finds (self-perpetuation impossible), dies with the A1
        # preemption reset, and cold acquisition still requires the rise. ORION_METER_TRACK_HOLD_S=0
        # disables.
        if match.found and self._box_track is not None and self._bt_hold_s > 0.0:
            self._bt_hold_match = copy.copy(match)
            self._bt_hold_until = ts + self._bt_hold_s
        elif (not match.found and self._box_track is not None and self._box_track.ready
                and self._bt_hold_s > 0.0 and self._bt_hold_match is not None
                and ts <= self._bt_hold_until):
            _pb = self._box_track.predict_pose(ts)
            _pcx, _pcb, _pw = _pb if _pb is not None else (
                self._box_track.cx(), self._box_track.cb(), self._box_track.width())
            _cf = self._box_track_confirm_bar(
                frame_bgr, _pcx, _pcb, _pw,
                None if self._cfg.meter_color == "Red"
                else (getattr(self._bt_hold_match, "hsv_lo", None),
                      getattr(self._bt_hold_match, "hsv_hi", None)))
            if _cf is not None:
                match = copy.copy(self._bt_hold_match)
                match.found = True
                match.w = max(1, int(round(_cf[2])))
                match.h = max(4, int(round(_cf[3])))
                match.x = _cf[0] - match.w * 0.5
                match.y = _cf[1] - match.h

        # MeterBoxKalman: smooth the found match's STABLE anchor (centre-x, notch=bottom, width) BEFORE
        # fill measurement, so the scan column, the zoom-staleness guard, the validator and the served
        # bbox all see glued-to-the-meter geometry. The fill EXTENT (match.h / the rect top) is left
        # raw — it IS the measurement. On "reject" (the filter thinks this frame is a VFX/distractor
        # outlier) the COASTED prediction is served, not the raw jump; a genuinely relocated meter
        # re-seeds the track via 3 mutually-consistent strikes, so it is never starved for long.
        if self._box_track is not None and match.found:
            _bt_ev = self._box_track.update_pose(
                float(match.x) + float(match.w) * 0.5,
                float(match.y) + float(match.h),
                float(match.w), ts,
                self._stability._acq_jump_gate_px() * 1.5)
            if _bt_ev == "reseed" and getattr(self._box_track, "_reseed_undo", None) is not None:
                # STRIKE re-seed: three consistent far measurements adopted a new spot. A
                # stationary VFX blob passes every geometric test, so pixel-verify the adopted
                # spot — no coherent bottom-anchored bar there means it was a distractor: roll
                # the track back onto the real meter and handle this frame as a reject.
                _cf2 = self._box_track_confirm_bar(
                    frame_bgr, self._box_track.cx(), self._box_track.cb(),
                    self._box_track.width(),
                    None if self._cfg.meter_color == "Red"
                    else (match.hsv_lo, match.hsv_hi))
                if _cf2 is None and self._box_track.rollback_reseed():
                    _bt_ev = "reject"
            if _bt_ev == "accept" and self._box_track.ready:
                _bt_w = self._box_track.width()
                _bt_cb = self._box_track.cb()
                match.x = self._box_track.cx() - _bt_w * 0.5
                match.y = _bt_cb - float(match.h)   # bottom-anchored: keep the raw fill extent
                match.w = max(1, int(round(_bt_w)))
            elif _bt_ev == "reject" and self._box_track.ready:
                # An established track REJECTED this measurement as an outlier (live case: the
                # post-release green-flame VFX promotes a stable blob above the meter). Serving
                # the raw outlier is exactly the flick the tracker exists to kill. First LOOK at
                # the predicted spot itself: if a red bar is still there, feed IT as this frame's
                # measurement (accept -> strikes reset -> a stationary VFX blob can never win the
                # 3-strike re-seed while the meter physically remains). Only when the old spot is
                # EMPTY does the coast serve, letting a genuine relocation re-seed via strikes.
                _pb = self._box_track.predict_pose(ts)
                if _pb is not None:
                    _pcx, _pcb, _pw = _pb
                    _cf = self._box_track_confirm_bar(
                        frame_bgr, _pcx, _pcb, _pw,
                        None if self._cfg.meter_color == "Red"
                        else (match.hsv_lo, match.hsv_hi))
                    _ev2 = ""
                    if _cf is not None:
                        # confirm_feed: glues the track but keeps the outlier strikes alive, so a
                        # lingering spent-carryover bar can't starve the re-seed onto a real new
                        # meter rising elsewhere (2K keeps the spent meter on screen 1-2s).
                        _ev2 = self._box_track.update_pose(
                            _cf[0], _cf[1], _cf[2], ts,
                            self._stability._acq_jump_gate_px() * 1.5,
                            confirm_feed=True)
                    if _ev2 == "accept":
                        _bt_w = self._box_track.width()
                        _bt_cb = self._box_track.cb()
                        match.x = self._box_track.cx() - _bt_w * 0.5
                        match.y = _bt_cb - float(_cf[3])   # confirmed bar extent, not the outlier's
                        match.h = int(round(_cf[3]))
                        match.w = max(1, int(round(_bt_w)))
                    else:
                        match.x = _pcx - _pw * 0.5
                        match.y = _pcb - float(match.h)
                        match.w = max(1, int(round(_pw)))

        det_bbox = (int(round(match.x)), int(round(match.y)), match.w, match.h) if match.found else None
        self._roi_lock.update(match.found, det_bbox)

        metrics = _FillMetrics()
        track = _TrackMetrics()
        if match.found:
            metrics = subpixel_fill_metrics(
                frame_bgr, match.x, match.y, match.w, match.h,
                self._cfg.meter_color, match.bgr_lo, match.bgr_hi,
                match.hsv_lo, match.hsv_hi,
            )
            # Track-relative fill: the colour contour bounds only the filled
            # portion of the meter, so metrics.fill_pct (filled / bbox-height) is
            # ~100% for the whole shot — the "dumps at 100%" bug. Measure against
            # the full track (bottom anchor -> green window at the top) so fill
            # sweeps 0->100 as the meter rises.
            track = self._measure_track(frame_bgr, match)
            # Sub-pixel READER (model A, ORION_METER_READER=1 + models/meter_reader.pt): replace the
            # row-counted track.fill_pct with the CNN's continuous sub-pixel fill on the located crop.
            # Same 0..100 bar-travel semantics; removes the ~1-2% quantization that feeds velocity/tip
            # jitter downstream. Inert when the reader is absent/disabled or the crop is a non-located
            # (park) path. NOTE: validate crop-semantics + sim-to-real before enabling (see reader notes).
            if (self._reader is not None and getattr(self._reader, "enabled", False)
                    and reader_crop is not None and getattr(reader_crop, "size", 0)):
                _rd = self._reader.read(reader_crop)
                if _rd is not None:
                    track.fill_pct = float(_rd["fill_pct"])

        self._stability.note_frame_width(orig_w)
        # Track T-a3: hand the validator the current locator pan speed (|loc_mem velocity| px/frame) so it
        # can widen the re-acquire jump gate during a fast pan. 0 on the classical path (velocity stays 0).
        self._stability._pan_px = float(np.hypot(self._loc_mem_vx, self._loc_mem_vy)) if self._locator is not None else 0.0
        fstate = self._stability.validate(match, track.fill_pct, ts)
        self._motion.update(fstate.fill_pct, metrics.filled_units, ts, fstate.valid)

        # T5: park authoritative contract — an UNCORROBORATED wide-fallback candidate must not
        # surface as a park detection (a distractor-only scene stays not-detected). The validator
        # above has already banked this frame's acquisition state (_acq_fills / warm / last_valid),
        # so a REAL rising meter corroborates and serves on the following frames; only the surfaced
        # result is demoted here.
        park_fb_demoted = False
        if park_fb_candidate and match.found and not fstate.valid:
            match.found = False
            park_fb_demoted = True

        # Per-frame gate diagnostics for detframes.csv. Always reflects THIS
        # frame's scan + stability evaluation, even when the value returned
        # below is a meter_memory echo of an older frame (analysis tools key on
        # mem_left to tell the two apart). mem_left is sampled BEFORE the echo
        # branch decrements it.
        scan = self._last_scan or {}
        scan_zone = str(scan.get("zone", "") or "")
        self.last_debug = {
            "stab_streak": int(self._stability.consecutive_valid),
            "stab_tracking": bool(self._stability.tracking),
            "stab_event": str(self._stability.last_event),
            "stab_jump_px": float(self._stability.last_jump_px),
            "acq_gate_px": float(self._stability.last_acq_gate_px),
            "zone": ("loc_mem" if loc_mem_used else ("anchor" if anchor_found else (scan_zone if scan_zone else ("locked_crop" if cropped else "wide")))),
            "roi_miss": int(self._roi_lock.miss_frames),
            "cand_n": int(scan.get("cand_n", -1)),
            "cand_size_ok": int(scan.get("cand_size_ok", -1)),
            "purity_rej": int(scan.get("purity_rej", -1)),
            "med_h": float(scan.get("med_h", -1.0)),
            "med_s": float(scan.get("med_s", -1.0)),
            "mem_left": int(self._meter_memory_left),
            "anchor_found": 1 if anchor_found else 0,
            "anchor_score": float(self._anchor.last_score),
            "anchor_x": int(anchor_loc[0]) if anchor_loc is not None else -1,
            "anchor_y": int(anchor_loc[1]) if anchor_loc is not None else -1,
            "auto_color": str(self._auto_color_locked or ""),
            "park": bool(park_active),
        }

        vel = self._motion.velocity_pct_s
        accel = self._motion.accel_pct_s2
        lat_s = self._cfg.total_latency_ms / 1000.0
        prediction = max(0.0, min(100.0, fstate.fill_pct + vel * lat_s + 0.5 * accel * lat_s * lat_s))

        eta_top = _MotionEstimator.eta_to_target_ms(
            fill_pct=fstate.fill_pct,
            target_pct=100.0,
            vel_pct_s=vel,
            acc_pct_s2=accel,
            latency_ms=self._cfg.total_latency_ms,
            velocity_floor=self._cfg.velocity_min_pct_per_sec,
        )

        # Green window from the full-track scan (done in _measure_track over the
        # whole track column ABOVE the fill). The old self._green.scan() looked
        # only INSIDE the fill bbox, so it missed the green at the top of the
        # track and occasionally locked fill-edge noise at ~1-4%.
        gw_found = track.green_found
        gw_start = track.green_start_pct if gw_found else self._cfg.green_window_start_pct
        gw_end = track.green_end_pct if gw_found else self._cfg.green_window_end_pct
        if gw_end < gw_start:
            gw_start, gw_end = gw_end, gw_start
        gw_center = track.green_center_pct if gw_found else ((gw_start + gw_end) * 0.5)
        gw_width = track.green_width_pct if gw_found else max(0.0, abs(float(gw_end) - float(gw_start)))
        gw_confidence = track.green_confidence if gw_found else 0.0
        gw_cluster_px = track.green_cluster_px

        eta_green = _MotionEstimator.eta_to_target_ms(
            fill_pct=fstate.fill_pct,
            target_pct=gw_center,
            vel_pct_s=vel,
            acc_pct_s2=accel,
            latency_ms=self._cfg.total_latency_ms,
            velocity_floor=self._cfg.velocity_min_pct_per_sec,
        )

        stable_count = self._stability.consecutive_valid
        vel_stable = self._motion.stable
        conf_ok = fstate.confidence >= self._cfg.confidence_threshold

        eta_trigger = (eta_green >= 0.0 and eta_green <= self._cfg.total_latency_ms)
        pred_trigger = prediction >= gw_center
        micro_trigger = gw_cluster_px >= self._cfg.green_cluster_min_px
        green_trackable = bool(gw_found and gw_confidence > 0.0 and gw_center > 0.0)
        fallback_target_trackable = bool(gw_center > 0.0 and gw_width > 0.0)
        motion_possible = bool(vel >= -5.0 or micro_trigger)

        release_ready = (
            (eta_trigger or pred_trigger or micro_trigger)
            and conf_ok
            and stable_count >= 1
            and (green_trackable or fallback_target_trackable or self._cfg.tip_target_mode)
            and motion_possible
            and match.found
        )

        outline = None
        if match.found:
            bx, by = int(round(match.x)), int(round(match.y))
            bx2, by2 = bx + match.w, by + match.h
            outline = np.array([[[bx, by]], [[bx2, by]], [[bx2, by2]], [[bx, by2]]], dtype=np.int32)

        # OVERLAY/ENGINE bbox = the FULL meter (notch -> tip), NOT just the filled portion. _measure_track already
        # computed the full extent: track.track_top_row (the tip: green window -> learned full-height -> style
        # estimate) and track.track_bottom_row (the notch). Emitting the full meter makes the neon box span the
        # WHOLE meter with the fill rising INSIDE it (RED_PARK_NEON look), size-INVARIANT to shot speed (so a fast
        # square shot looks the same as a slow stick shot, not "cut in half") and to the post-shot recede/zoom
        # (no vertical shrink). It also IMPROVES the native meter-settle grader: the old fill rect's centre drifted
        # UP as the fill rose (false "motion" on a static Standstill meter); the full-meter centre is fixed, cleanly
        # separating court-slide from fill-rise. A few px of pad hugs a touch more generously ("a little bigger").
        # Fallback to the fill rect when the track geometry is unavailable.
        _PAD_Y, _PAD_X = 3, 2
        if match.found and track.track_top_row >= 0 and track.track_bottom_row > track.track_top_row:
            _full_h = int(track.track_bottom_row) - int(track.track_top_row)
            # MAX-HOLD the full height across the shot episode so the box doesn't SHRINK on the post-shot
            # recede/zoom: the meter WIDGET stays the same size, only the fill deflates inside it. Anchored to
            # the CURRENT notch so it still rides a sliding fade, and it GROWS if the camera zooms in (max takes
            # the larger height). Reset per episode by the no-detection branch.
            _prev_hold = int(getattr(self, "_park_box_max_h", 0))
            if (self._box_track is not None
                    and os.environ.get("ORION_METER_RATCHET_DECAY", "1") != "0"):
                # RATCHET DECAY (2026-07-04 live batch): before ready_h (no green sighting yet —
                # the exact window where reflections/VFX flares inflate the track geometry), one
                # tall frame could ratchet the grow-only max-hold to 245px on a 164px meter and
                # STICK all episode. Bleed the ratchet 2%/frame so an inflated hold recovers in
                # ~0.5s while a LEGIT tall reading re-asserts itself through the max() every
                # frame (a width-scaled hard cap was tried first and clipped legit geometry on
                # sliver-width pan frames). Box-track-gated: classical path byte-identical.
                _prev_hold = int(round(_prev_hold * 0.98))
            self._park_box_max_h = max(_prev_hold, _full_h)
            _notch = int(track.track_bottom_row)
            _hold_h = self._park_box_max_h
            if self._box_track is not None and self._box_track.ready_h:
                # MeterBoxKalman: the smoothed full height replaces the grow-only max-hold — still
                # jitter-free, but ALLOWED to decrease on a genuine zoom-out/recede instead of one
                # tall frame pinning the box tall for the whole episode (the grow-stuck mode).
                _hold_h = max(8, int(round(self._box_track.height())))
            _box_y = max(0, _notch - _hold_h - _PAD_Y)
            _box_h = _notch + _PAD_Y - _box_y
            _box_x = max(0, int(round(match.x)) - _PAD_X)
            _box_w = match.w + 2 * _PAD_X
        else:
            self._park_box_max_h = 0
            _box_x, _box_y, _box_w, _box_h = int(round(match.x)), int(round(match.y)), match.w, match.h

        result = DetectResult(
            detected=match.found,
            style=match.template_name,
            color_name=self._cfg.meter_color,
            bbox=(_box_x, _box_y, _box_w, max(1, _box_h)),
            fill_pct=fstate.fill_pct,
            confidence=fstate.confidence,
            consecutive_frames=stable_count,
            raw_fill_pct=track.fill_pct,
            smoothed_fill_pct=fstate.fill_pct,
            velocity=vel,
            accel=accel,
            fill_velocity_pct_s=vel,
            fill_acceleration_pct_s2=accel,
            prediction=prediction,
            eta_ms=eta_top,
            top_pixel_row=metrics.top_pixel_row,
            release_ready=release_ready,
            velocity_stable=vel_stable,
            aborted=self._stability.aborted,
            outline_contour=outline,
            green_cluster_px=gw_cluster_px,
            roi_locked=self._roi_lock.locked,
            fill_pixels=metrics.fill_pixels,
            total_fill_pixels=metrics.total_pixels,
            fill_velocity_px_s=self._motion.velocity_px_s,
            fill_accel_px_s2=self._motion.accel_px_s2,
            green_window_start_pct=gw_start,
            green_window_end_pct=gw_end,
            green_window_center_pct=gw_center,
            green_window_width_pct=gw_width,
            green_window_confidence=gw_confidence,
            green_window_start_row=track.green_top_row,
            green_window_end_row=track.green_bottom_row,
            green_window_center_row=(int((track.green_top_row + track.green_bottom_row) / 2) if track.green_found else -1),
            eta_to_green_center_ms=eta_green,
            eta_to_top_pixel_ms=eta_top,
            meter_roi_locked=self._roi_lock.locked,
            meter_tracking_jitter=self._roi_lock.jitter_px,
        )

        # Specific, machine-readable reason a frame is not a usable sample.
        if not match.found:
            result.rejection_reason = "roi_not_found"
        elif not conf_ok:
            result.rejection_reason = "low_confidence"
        elif not fstate.valid:
            result.rejection_reason = "bbox_unstable"
        elif not gw_found:
            result.rejection_reason = "green_not_found"
        else:
            result.rejection_reason = ""

        # A1: update the served meter's rise phase (drives spent-carryover preemption on the NEXT
        # frame) and surface it on the result for telemetry/scoring. Served = a meter detected here.
        self._update_rise_state(match.found, fstate.fill_pct, ts, result.bbox if match.found else None)
        result.rise_state = self.last_rise_state

        if match.found and fstate.valid and conf_ok:
            # T6: remember the served position so the lock-ROI locator crop can anchor on it even
            # when the serve came from a tier the locator can't see (park / wide fallback).
            self._serve_pos = (float(match.x) + float(match.w) * 0.5,
                               float(match.y) + float(match.h) * 0.5,
                               float(max(match.w, match.h)))
            self._serve_ts = ts
            self._last_result = result
            self._meter_memory_left = max(0, int(self._cfg.meter_memory_frames))
            return result

        if self._last_result is not None and self._meter_memory_left > 0 and not self._stability.aborted:
            self._meter_memory_left -= 1
            decayed_conf = max(0.05, min(1.0, float(self._last_result.confidence) * 0.82))
            # MeterBoxKalman: a MOVING echo. The memory echo used to freeze the last bbox in place, so
            # under a pan the box visibly lagged/flicked until re-detection. With a live filter the
            # echo follows the predicted trajectory instead — same freshness semantics (still
            # rejection_reason="meter_memory", fed=0; the engine never trusts it as fresh), the BOX
            # just stays glued to where the meter actually is.
            _mem_bbox = self._last_result.bbox
            if self._box_track is not None:
                _pb = self._box_track.predict_pose(ts)
                if _pb is not None:
                    _pcx, _pcb, _pw = _pb
                    # Same _PAD_X/_PAD_Y dressing as the found-path box so the served box does not
                    # visibly shrink by the pad on a miss frame and re-grow on re-detection (the
                    # last flicker). ready_h height is UNPADDED (like _hold_h); the last-served
                    # bbox height is already padded, so strip its pads before re-dressing.
                    _ph = (int(round(self._box_track.height())) if self._box_track.ready_h
                           else max(8, int(self._last_result.bbox[3]) - 2 * _PAD_Y))
                    _mem_bbox = (max(0, int(round(_pcx - _pw * 0.5)) - _PAD_X),
                                 max(0, int(round(_pcb)) + _PAD_Y - (_ph + 2 * _PAD_Y)),
                                 max(1, int(round(_pw)) + 2 * _PAD_X),
                                 max(1, _ph + 2 * _PAD_Y))
            memory_result = replace(
                self._last_result,
                bbox=_mem_bbox,
                confidence=decayed_conf,
                consecutive_frames=max(0, int(self._last_result.consecutive_frames) - 1),
                release_ready=False,
                aborted=False,
                roi_locked=self._roi_lock.locked,
                meter_roi_locked=self._roi_lock.locked,
                meter_tracking_jitter=self._roi_lock.jitter_px,
                rejection_reason="meter_memory",
                rise_state=self.last_rise_state,
            )
            self._last_result = memory_result
            return memory_result

        return result

    def trigger_auto_calibration(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        bbox = self._auto_cal.scan(frame_bgr, self._cfg.meter_color)
        if bbox is not None:
            for _ in range(8):
                self._roi_lock.update(True, bbox)
        return bbox


def get_search_rois(W: int, H: int, cfg: Optional[DetectorConfig] = None) -> List[Tuple[int, int, int, int]]:
    if cfg is None:
        cfg = DetectorConfig()
    y_top = int(H * 0.20)
    y_bot = int(H * 0.95)
    x1 = int(W * min(cfg.left_zone_start_pct, cfg.right_zone_start_pct, 35.0) / 100.0)
    x2 = int(W * max(cfg.left_zone_end_pct, cfg.right_zone_end_pct, 65.0) / 100.0)
    x1 = max(0, min(W - 1, x1))
    x2 = max(x1 + 1, min(W, x2))
    return [(x1, y_top, x2, y_bot)]
