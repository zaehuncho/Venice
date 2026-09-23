#!/usr/bin/env python
"""
simple_meter_reader.py -- PRODUCTION shot-gated simple meter reader.

Promoted from tools/diagnostics/simple_meter_reader.py after a head-to-head proved a
SHOT-GATED, REGION-ANCHORED, SIMPLE reader MATCHES Orion's ~5,700-LOC serving chain on
accuracy (peak fill identical to 0.3pp, rise within 0.5pp) at ~1% of the code, ~0.6ms/frame,
NO ML model, and ZERO decor false-locks (see logs/diagnostics/simple_vs_chain/FINAL_*.txt).

Grounded on TWO shipping 2K tools (NOT invented):
  * 2k_Vision (Arrow2.json): BGR inRange -> findContours -> per-style size/aspect gate +
    CONFIDENCE-DECAY lock (no warmup: trust a strong frame immediately, decay per frame).
    EXACT params reused: RED BGR [0,0,220]-[60,60,255]; contour w23-30 h33-165; search band
    5/250/1915/770; confidence initial 1.0 / min 0.95 / rate 0.01.
  * 2k26.py / Starzen: cv2.matchTemplate (TM_CCOEFF_NORMED) FAST-PATH in a small window around
    the last-known meter location (the efficiency trick); pose-based SHOT-GATING.

Three stages vs the chain's ~8:
  1. SHOT-GATE  : a tall thin red column exists in the plausible player band? This IS the
                  entire false-lock guard (self-contained). LIVE it can ALSO consult the bot's
                  arm/hold state + the v9 player/pose model (orion_player_detect_v9) as
                  corroboration -- wire the hook (set_shot_state) but keep it OPTIONAL: it only
                  RELAXES the early-rise acquisition gate, it never SUPPRESSES a real column.
  2. LOCALISE   : cv2.matchTemplate NCC fast-path around the last box when LOCKED (~sub-ms), with
                  a lightweight motion-follow (velocity term on the box) so a fast-sliding blurred
                  FADE meter stays tracked. Full colour->contour->size/aspect gate scan in the band
                  only on (RE)ACQUIRE. A CONFIDENCE-DECAY lock (no N-frame warmup) coasts a brief
                  miss, drops on decay.
  3. READ       : full track anchored by the GREEN make-window TIP as the cap (present at every
                  fill level; the 2kMETER gray-cap [80-120] anchors the COURT not the meter, so it
                  is NOT used). fill% = red extent above the red floor / fillable track height.

Fill is reported as a robust % independent of on-screen position. A wall-time velocity
(velocity_pct_s, clamp 500, mirrors the chain's _MotionEstimator) is emitted every frame so the
timing stack can drive the release.

detect() returns a DetectResult IDENTICAL in shape to meter_detector.MeterDetector.detect() so the
sidecar can flag-select this reader (ORION_SIMPLE_READER) with NOTHING else downstream changing.
"""
from __future__ import annotations

import datetime as _datetime
import json as _json
import collections as _collections
import logging as _logging
import os as _os
import sys as _sys
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import cv2

import meter_bar_colors as _mbc

try:                                    # [ORION_PLAYER_ANCHOR] optional; inert when absent
    import player_anchor as _player_anchor
except Exception:                       # pragma: no cover - never break the shipped reader
    _player_anchor = None

_cal_logger = _logging.getLogger("meter_color_cal")
_gz_logger = _logging.getLogger("green_zone")
# Lock-seat forensics (one line per acquisition; unarmed seats rate-limited). Named so the
# sidecar relay shows the reader as its own component, like meter_color_cal/green_zone.
_acq_logger = _logging.getLogger("simple_reader")

# Neon make-window band (#1CFE1C, BGR ~[19,254,28]) -- the SAME HSV swatch validated offline by
# tools/diagnostics/green_zone_grader.py (reproduces the bimodal part0 engine-log window). The
# LOWER edge of this band, read on frames where the rising red has NOT yet covered it, IS the
# per-shot make-window start on the fill scale (GREEN = MAKE, user-confirmed).
_NEON_LO = (48, 140, 140)
_NEON_HI = (70, 255, 255)


# --------------------------------------------------------------------------- #
#  DetectResult: reuse the real dataclass when meter_detector is importable so
#  the emission contract is guaranteed field-identical; fall back to a local
#  duck-type with the same fields so the reader can be unit-tested standalone.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - trivial import shim
    from meter_detector import DetectResult as _DetectResult  # type: ignore
    _HAVE_REAL_RESULT = True
except Exception:  # pragma: no cover
    from dataclasses import dataclass, field
    from typing import Tuple

    @dataclass
    class _DetectResult:  # minimal mirror of meter_detector.DetectResult
        detected: bool
        style: str
        color_name: str
        bbox: Tuple[int, int, int, int]
        fill_pct: float
        confidence: float
        consecutive_frames: int
        raw_fill_pct: float = 0.0
        smoothed_fill_pct: float = 0.0
        fill_estimator_mode: str = ""
        fill_estimator_generation: int = 0
        fill_velocity_pct_s: float = 0.0
        fill_acceleration_pct_s2: float = 0.0
        eta_ms: float = -1.0
        top_pixel_row: int = -1
        green_window_start_pct: float = -1.0
        green_window_end_pct: float = -1.0
        green_window_center_pct: float = -1.0
        green_window_width_pct: float = 0.0
        green_window_confidence: float = 0.0
        eta_to_green_center_ms: float = -1.0
        rejection_reason: str = ""
        rise_state: str = ""
        gameplay_structure_verified: bool = False
        gameplay_structure_epoch: int = 0
        # Diagnostic frame-start identity, including samples that have not earned proof.
        # This field never grants timing/ownership authority.
        gameplay_sample_epoch: int = 0
    _HAVE_REAL_RESULT = False


@dataclass
class ReaderParams:
    """Every behavioural tunable of the reader in one struct.

    The DEFAULTS ARE the shipped capture-card tuning (the "q=1 row"): constructing a
    SimpleMeterReader with ReaderParams() changes NOTHING, byte-for-byte -- asserted by
    the collapse regression. The compressed-stream quality layer (stream_quality.py)
    derives degraded rows for stream quality q<1; every field here must therefore stay
    monotone-adaptable in q. Geometry priors (BAND, W_MIN, ...) remain class constants
    on SimpleMeterReader: they describe the METER, not the reader's evidence thresholds,
    and do not adapt with stream quality.
    """
    # acquisition gates
    h_acq: int = 33                # cold-acquire min contour height @1080p
    h_acq_armed: int = 15          # relaxed acquire height when the shot-gate is ARMED
    h_hold: int = 12               # relaxed min height to HOLD a tracked lock
    ar_min: float = 1.8            # tall-thin aspect floor (cold acquire)
    ar_min_armed: float = 0.7      # relaxed aspect floor when ARMED
    relocate_ar_min: float = 0.35  # aspect floor inside the bounded relocate window
    red_frac_min: float = 0.45     # min red fill fraction inside an acquire bbox
    # confidence-decay lock (EXACT Arrow2)
    conf_init: float = 1.0
    conf_min: float = 0.95
    conf_rate: float = 0.01
    conf_rate_armed: float = 0.002    # ~25-frame armed coast: survives a full arm/body crossing of
                                      # a STATIONARY meter (occlusion), not just a fast-fade blur
    # grayscale NCC fast-path accept
    ncc_lock: float = 0.60
    # green make-window tip
    g_area_min: int = 200          # green tip min filled area @1080p
    green_s_floor: int = 90        # HSV saturation floor of the green mask
    green_v_floor: int = 90        # HSV value floor of the green mask
    # 1-D fill-profile thresholds
    red_row_thr: float = 0.20      # red row-mean crossing = the fill boundary
    green_row_thr: float = 0.15    # green row-mean threshold = tip rows
    # temporal
    vel_win: int = 6               # rolling (ts, fill) samples for the velocity slope

    @classmethod
    def for_quality(cls, q: float) -> "ReaderParams":
        """The adaptation law: every tunable a MONOTONE function of stream quality
        q in [0,1] that evaluates to the shipped constants at q=1 exactly
        (for_quality(1.0) == ReaderParams(); asserted by the collapse test). Lower q
        relaxes evidence thresholds and lengthens coasts -- the compressed stream keeps
        weaker (but real) evidence alive instead of dropping the lock."""
        q = max(0.0, min(1.0, float(q)))
        return cls(
            red_frac_min=round(0.45 - 0.25 * (1.0 - q), 4),
            ncc_lock=round(0.60 - 0.25 * (1.0 - q), 4),
            conf_rate=round(0.01 * (0.4 + 0.6 * q), 6),
            conf_rate_armed=round(0.002 * (0.4 + 0.6 * q), 6),
            # GREEN gates do NOT adapt (design choice, not yet measurement-backed): the
            # green tip is the anchor that HOLDS the lock through cap/deflate, and the
            # decor false-lock history says lock anchors must stay precision-first --
            # admitting desaturated court-green risks dragging the box. Compression's
            # green-recall loss is covered by the LUMA tip sliver instead. (The rung
            # A/B probe could not test this either way: its re-encodes kill the green
            # mask entirely. Revisit with dual-capture ground truth.)
            g_area_min=200,
            green_s_floor=90,
            vel_win=int(round(6 + 7.2 * (1.0 - q))),
        )


# --------------------------------------------------------------------------- #
#  ColorCalibrator (Track B / B1): stage-per-shot, commit-or-discard colour
#  learner + calibration lifecycle (B3). Principle: LEARN NARROWER, NEVER WIDER.
#  The v1 envelope IS the shipped constants, so a learned band can only NARROW
#  production -- decor stays unrepresentable by construction. Training frames are
#  HARDWARE-ARMED only (a CV self-arm can never train itself: a false lock must
#  never learn its own colours). Fades ride the default band: their hw-arm lands
#  post-release, so they contribute ~zero qualifying frames by design (plan B1).
# --------------------------------------------------------------------------- #

def _hist_median_mad(h) -> Optional[tuple]:
    """(median, MAD) of a value histogram (index = value). O(bins).

    The MAD uses the UPPER median of the deviations (total//2 + 1): with the lower median an
    exact 50/50 bimodal distribution reads MAD 0 â€” the one shape the commit gate exists to
    reject. The upper median is conservative (wider MAD -> stricter commit gate, wider derived
    half-width that the envelope clamps anyway)."""
    h = np.asarray(h, np.int64)
    total = int(h.sum())
    if total <= 0:
        return None
    c = np.cumsum(h)
    med = int(np.searchsorted(c, (total + 1) // 2))
    dev = np.abs(np.arange(h.size) - med)
    order = np.argsort(dev, kind="stable")
    dc = np.cumsum(h[order])
    mad = int(dev[order][np.searchsorted(dc, min(total, total // 2 + 1))])
    return med, mad


def _hist_percentile(h, p: float) -> Optional[int]:
    h = np.asarray(h, np.int64)
    total = int(h.sum())
    if total <= 0:
        return None
    c = np.cumsum(h)
    return int(np.searchsorted(c, max(1, int(round(total * p / 100.0)))))


def _band_iou(a_lo, a_hi, b_lo, b_hi) -> float:
    """Volumetric IoU of two axis-aligned boxes in channel space (per-channel intervals)."""
    inter = 1.0
    va = vb = 1.0
    for al, ah, bl, bh in zip(a_lo, a_hi, b_lo, b_hi):
        la = max(1.0, float(ah) - float(al))
        lb = max(1.0, float(bh) - float(bl))
        ov = max(0.0, min(float(ah), float(bh)) - max(float(al), float(bl)))
        inter *= ov
        va *= la
        vb *= lb
    union = va + vb - inter
    return inter / union if union > 0 else 0.0


class ColorCalibrator:
    """Per-shot staged colour learner for the simple reader.

    Lifecycle (B3): SEED -> LEARNING -> LOCKED -> PROVISIONAL -> (LOCKED | RELEARN).
    Learning: stage histograms per hardware-armed shot; COMMIT the whole shot only
    when every gate passes (peak >= 70, >= 8 qualifying frames, R-channel MAD <= 60,
    green hue MAD <= 8) -- a contaminated lock contributes NOTHING. Bake ONCE at
    FROZEN (5 committed shots): red learned+baked in BGR with the channel-gap
    invariant (hi_G, hi_B <= lo_R - 80), green in HSV (hue gain-invariant, relative
    S/V floors). Everything is intersected with the v1 ENVELOPE = the shipped
    constants, so learning strictly narrows. Re-learn = revert to defaults + zero
    histograms; there is NO EWMA widening path, ever."""

    # v1 envelope == the SHIPPED constants (plan B1 [fix]: the safety claim demands it).
    RED_ENV = ((0, 0, 220), (60, 60, 255))
    GREEN_ENV = ((38, 90, 90), (85, 255, 255))
    # commit gates (per staged shot)
    COMMIT_PEAK_MIN = 70.0
    COMMIT_FRAMES_MIN = 8
    COMMIT_RED_MAD_MAX = 60
    COMMIT_GREEN_HUE_MAD_MAX = 8
    FROZEN_SHOTS = 5
    CLIP_K = 3.5
    # half-width floors/caps per BGR channel (floors keep a band usable, caps keep it tight)
    RED_HW_FLOOR = (10, 10, 12)
    RED_HW_CAP = (45, 45, 30)
    GREEN_HUE_FLOOR, GREEN_HUE_CAP = 4, 18
    CHANNEL_GAP = 80          # hi_G, hi_B <= lo_R - 80: gray/mullion unrepresentable
    # HSV-colour equivalents of CHANNEL_GAP. The BGR invariant's PURPOSE is that a learned band
    # must stay incapable of representing neutral grey -- that is what makes a contaminated lock
    # contribute nothing rather than something subtly wrong. On an HSV row the same purpose is
    # served by (a) a hard SATURATION FLOOR, since grey is exactly "no saturation", and (b) a
    # bounded HUE SPAN, since a band that sprawls in hue is no longer a colour. Both are applied
    # BEFORE the envelope clamp, exactly like the BGR gap, so the envelope can only tighten
    # further and learning still strictly NARROWS.
    HSV_SAT_FLOOR_MIN = 90    # a learned S floor may never drop below this
    HSV_HUE_SPAN_MAX = 24     # hue_hi - hue_lo may never exceed this
    HSV_HUE_HW_FLOOR, HSV_HUE_HW_CAP = 3, 12    # learned hue half-width clamp
    IOU_AGREE = 0.70
    MAX_RESETS = 3
    # staleness triggers (LOCKED)
    INLIER_LOW = 0.55
    INLIER_LOW_SHOTS = 2
    SLOW_LOCK_FRAMES = 30
    SLOW_LOCK_SHOTS = 2
    NCC_EWMA_LOW = 0.70
    GREEN_PRESENCE_LOW = 0.30
    GREEN_LOW_SHOTS = 2
    LEARNED_MISS_STREAK = 2
    # provisional counters
    ARMED_NOLOCK_TO_PROVISIONAL = 3
    GATED_RATE_TO_PROVISIONAL = 0.40
    CLEAN_LOCKS_TO_LOCKED = 3
    NOLOCKS_TO_RELEARN = 6
    VERIFY_INLIER = 0.75
    FINGERPRINT_D = 0.35

    def __init__(self, profile_path: Optional[str] = None, style: str = "Arrow2",
                 color: str = "Red", styles_dir: Optional[str] = None):
        self.style = str(style)
        # The calibrator learns the BAR colour, so it must know WHICH colour and in which space.
        # Normalising here (rather than trusting the raw config string) keeps a mislabeled config
        # from silently learning under a colour name that no band table knows.
        self.color = _mbc.normalize(color)
        self._styles_dir = styles_dir
        root = _os.path.dirname(_os.path.abspath(__file__))
        self.profile_path = profile_path or _os.path.join(root, "calibration",
                                                          "meter_color_profiles.json")
        self._load_style_config(styles_dir or _os.path.join(root, "meter_styles"))
        self.state = "seed"          # seed|learning|locked|provisional|relearn
        self.baked = None            # dict(red_lo, red_hi, green_lo, green_hi) once FROZEN
        self.learned_date = ""
        self.committed_shots = 0
        self.reset_count = 0
        self._acc_red = np.zeros((3, 256), np.int64)     # committed accumulation (B,G,R)
        self._acc_green = np.zeros((3, 256), np.int64)   # committed accumulation (H,S,V)
        self._stage = None                               # per-shot staging
        self._status_version = 1
        # staleness (LOCKED probe path keeps these EWMAs alive even while frozen)
        self.inlier_ewma = 1.0
        self._inlier_low_shots = 0
        self._slow_lock_shots = 0
        self.ncc_ewma = 1.0
        self._green_low_shots = 0
        self.learned_miss_streak = 0
        # provisional / relearn counters (persisted with the profile)
        self._armed_nolock_streak = 0
        self._provisional_clean = 0
        self._provisional_nolocks = 0
        self._verify_pending = False
        # arena fingerprint (32-bin hue hist of the search band at hw-arm)
        self.fingerprint = None
        self._user_cal = None        # calibrate_meter window state
        self._snapshot = None
        self._profiles = self._load_profiles()

    # ---- config / persistence -------------------------------------------------------------------
    def _load_style_config(self, styles_dir: str) -> None:
        """Read the NEW blocks (default_bands / envelope / calibration / confidence) from the
        style JSON. NEVER the legacy loose `colors`/`search` blocks -- Red (0,0,170) and
        right:1915 would regress day-one (plan B1 [fix]). default_bands must equal the shipped
        constants; a mismatch is refused loudly and the class constants win."""
        # The BAR envelope. For a BGR colour (Red) it stays the shipped BGR constants and the
        # whole learning path below is unchanged. For an HSV colour the envelope IS that colour's
        # nominal tagged row -- so the "learning can only NARROW production" guarantee is the same
        # statement in both spaces: the learned band is intersected with the shipped band.
        self.band_kind = _mbc.band(self.color, _mbc.BAND_NOMINAL)[0]
        self.bar_env = _mbc.band(self.color, _mbc.BAND_NOMINAL)
        self.red_env = (tuple(self.RED_ENV[0]), tuple(self.RED_ENV[1]))
        self.green_env = (tuple(self.GREEN_ENV[0]), tuple(self.GREEN_ENV[1]))
        self.cal_cfg = {}
        if self.band_kind != "bgr":
            # The style JSON's default_bands/envelope blocks describe the RED BGR bands only;
            # applying them to an HSV colour would be meaningless. Keep the tagged envelope.
            try:
                path = _os.path.join(styles_dir, f"{self.style}.json")
                with open(path, encoding="utf-8") as fh:
                    self.cal_cfg = dict((_json.load(fh).get("calibration", {}) or {}))
            except Exception:
                self.cal_cfg = {}
            return
        try:
            path = _os.path.join(styles_dir, f"{self.style}.json")
            with open(path, encoding="utf-8") as fh:
                sj = _json.load(fh)
            db = sj.get("default_bands", {})
            red_db = db.get(self.color) or db.get("Red")
            if red_db and (tuple(red_db.get("low", ())) != self.RED_ENV[0]
                           or tuple(red_db.get("high", ())) != self.RED_ENV[1]):
                _cal_logger.warning("style default_bands != shipped constants; refusing style bands")
            env = sj.get("envelope", {})
            if env:
                r = env.get("red", {})
                g = env.get("green", {})
                lo, hi = tuple(r.get("low", self.RED_ENV[0])), tuple(r.get("high", self.RED_ENV[1]))
                # the envelope may only be equal to (v1) the shipped constants; a WIDER v2
                # envelope ships only after the mandatory 14-session replay gate (plan B1).
                if lo == self.RED_ENV[0] and hi == self.RED_ENV[1]:
                    self.red_env = (lo, hi)
                glo, ghi = tuple(g.get("low", self.GREEN_ENV[0])), tuple(g.get("high", self.GREEN_ENV[1]))
                if glo == self.GREEN_ENV[0] and ghi == self.GREEN_ENV[1]:
                    self.green_env = (glo, ghi)
            self.cal_cfg = dict(sj.get("calibration", {}) or {})
        except Exception:
            pass

    def baked_bar_row(self):
        """The learned band as a TAGGED row, or None.

        `baked["red_lo"]/["red_hi"]` are always the low/high vectors of a single cv2.inRange, in
        whichever space this colour learns in -- (B,G,R) for a BGR colour, (H,S,V) for an HSV one.
        That is what lets the IoU agreement test and the inlier probe stay space-agnostic.
        """
        b = self.baked
        if b is None:
            return None
        if str(b.get("kind", "bgr")) == "hsv":
            # low = (h_lo, s_floor, v_floor); high = (h_hi, 255, 255)
            return ("hsv", int(b["red_lo"][0]), int(b["red_hi"][0]),
                    int(b["red_lo"][1]), int(b["red_lo"][2]))
        return ("bgr", tuple(b["red_lo"]), tuple(b["red_hi"]))

    def set_colour(self, color) -> None:
        """Live bar-colour change: re-envelope and throw away everything learned for the OLD
        colour. Bands learned for Red say nothing whatsoever about a Purple meter, and carrying
        them across would hand a wrong-colour band full lock authority."""
        new = _mbc.normalize(color)
        if new == self.color:
            return
        _cal_logger.warning("calibrator colour %s -> %s: reverting to defaults + LEARNING",
                            self.color, new)
        self.color = new
        root = _os.path.dirname(_os.path.abspath(__file__))
        self._load_style_config(self._styles_dir or _os.path.join(root, "meter_styles"))
        self.fingerprint = None
        self._revert_to_learning()
        self._bump()

    def _load_profiles(self) -> list:
        try:
            with open(self.profile_path, encoding="utf-8") as fh:
                data = _json.load(fh)
            profs = data.get("profiles", [])
            return profs if isinstance(profs, list) else []
        except Exception:
            return []

    def _persist(self) -> None:
        """Write the LOCKED profile (fingerprint + learned bands + stats + counters) to
        calibration/meter_color_profiles.json. REPLACE-not-blend: an existing profile whose
        fingerprint matches this arena is replaced wholesale. NOTE: court_profiles.json belongs
        to rtt_sync_engine.CourtProfileDB -- never that file (plan B1 [fix])."""
        if self.baked is None:
            return
        prof = {
            "style": self.style, "color": self.color,
            # Which SPACE red_lo/red_hi are expressed in. Restore refuses a profile whose kind
            # disagrees with the configured colour's kind, so an old kind-less (== "bgr") profile
            # can never be loaded onto an HSV colour.
            "kind": str(self.baked.get("kind", "bgr")),
            "fingerprint": [round(float(v), 5) for v in (self.fingerprint if self.fingerprint
                                                         is not None else [])],
            "red_lo": [int(v) for v in self.baked["red_lo"]],
            "red_hi": [int(v) for v in self.baked["red_hi"]],
            "green_lo": [int(v) for v in self.baked["green_lo"]],
            "green_hi": [int(v) for v in self.baked["green_hi"]],
            "learned_date": self.learned_date,
            "stats": {"committed_shots": int(self.committed_shots),
                      "reset_count": int(self.reset_count),
                      "inlier_ewma": round(float(self.inlier_ewma), 4)},
        }
        kept = []
        for p in self._profiles:
            try:
                if (p.get("style") == self.style and p.get("color") == self.color
                        and self._fp_distance(p.get("fingerprint"), self.fingerprint)
                        <= self.FINGERPRINT_D):
                    continue     # same arena -> replaced, not blended
            except Exception:
                pass
            kept.append(p)
        self._profiles = (kept + [prof])[-8:]
        try:
            _os.makedirs(_os.path.dirname(self.profile_path), exist_ok=True)
            tmp = self.profile_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump({"version": 1, "profiles": self._profiles}, fh, indent=1)
            _os.replace(tmp, self.profile_path)
        except Exception as exc:
            _cal_logger.warning("profile persist failed: %s", exc)

    @staticmethod
    def _fp_distance(a, b) -> float:
        """0.5 * L1 distance of two normalized 32-bin hue hists, in [0,1]. 1.0 if either missing."""
        try:
            if a is None or b is None:
                return 1.0
            av = np.asarray(a, float)
            bv = np.asarray(b, float)
            if av.size != bv.size or av.size == 0:
                return 1.0
            av = av / max(1e-9, av.sum())
            bv = bv / max(1e-9, bv.sum())
            return float(0.5 * np.abs(av - bv).sum())
        except Exception:
            return 1.0

    # ---- status / IPC ----------------------------------------------------------------------------
    def _bump(self) -> None:
        self._status_version += 1

    def status(self) -> dict:
        shots_done = self.committed_shots
        if self._user_cal is not None:
            shots_done = int(self._user_cal.get("commits", 0))
        return {"version": int(self._status_version), "state": str(self.state),
                "shots_done": int(shots_done), "shots_needed": int(self.FROZEN_SHOTS),
                "learned_date": str(self.learned_date),
                "calibrating": self._user_cal is not None}

    # ---- shot lifecycle (driven by the reader's hw-arm edges) ------------------------------------
    def begin_shot(self, fingerprint=None) -> None:
        """Rising hw-arm edge: open a staging window + run the arena fingerprint check."""
        self._stage = {
            "red": np.zeros((3, 256), np.int64), "green": np.zeros((3, 256), np.int64),
            "frames": 0, "peak": 0.0, "last_fill": None, "release": False,
            "locked_any": False, "frames_to_lock": -1, "frames_seen": 0,
            "green_seen": 0, "red_frames": 0,
            "inlier_num": 0, "inlier_den": 0,
            "gated": 0, "fed": 0,
        }
        if fingerprint is not None:
            self._check_fingerprint(np.asarray(fingerprint, float))
        self._bump()

    def _check_fingerprint(self, fp) -> None:
        """Arena-switch detection at hw-arm (B1 staleness): d > 0.35 vs the locked profile's
        fingerprint -> match saved profiles (verify-before-trust) or revert + LEARNING."""
        if self.state != "locked" or self.fingerprint is None:
            if self.fingerprint is None:
                self.fingerprint = fp
            return
        d = self._fp_distance(self.fingerprint, fp)
        if d <= self.FINGERPRINT_D:
            return
        # arena switch: try the saved profiles first.
        # STYLE+COLOUR FILTER (2026-08-04 fix): this matched on the arena FINGERPRINT ALONE while
        # _persist keys profiles by (style, color, fingerprint). The fingerprint is a hue
        # histogram of the arena, which is the SAME arena whatever colour the user set their
        # meter to -- so playing the same court after switching Red -> Purple loaded the
        # RED-learned band onto a Purple meter, straight into `locked`/`provisional` with full
        # lock authority. Restore must apply the same key the profile was written under.
        best, best_d = None, 1.0
        for p in self._profiles:
            if p.get("style") != self.style or p.get("color") != self.color:
                continue
            pd = self._fp_distance(p.get("fingerprint"), fp)
            if pd < best_d:
                best, best_d = p, pd
        if best is not None and best_d <= self.FINGERPRINT_D:
            try:
                self.baked = {"kind": str(best.get("kind", "bgr")),
                              "red_lo": tuple(int(v) for v in best["red_lo"]),
                              "red_hi": tuple(int(v) for v in best["red_hi"]),
                              "green_lo": tuple(int(v) for v in best["green_lo"]),
                              "green_hi": tuple(int(v) for v in best["green_hi"])}
                if self.baked["kind"] != self.band_kind:
                    raise ValueError("profile band kind mismatch")   # -> revert + LEARNING
                self.learned_date = str(best.get("learned_date", ""))
                self.fingerprint = fp
                self.state = "provisional"
                self._verify_pending = True   # first shot must show inlier >= 0.75
                self._provisional_clean = 0
                self._provisional_nolocks = 0
                _cal_logger.warning("arena switch d=%.2f -> matched profile (verify-before-trust)", d)
                self._bump()
                return
            except Exception:
                pass
        # no matching profile: revert to defaults + LEARNING from scratch
        _cal_logger.warning("arena switch d=%.2f -> no profile match; revert to defaults + LEARNING", d)
        self._revert_to_learning()
        self.fingerprint = fp

    def _revert_to_learning(self) -> None:
        self.baked = None
        self._acc_red[:] = 0
        self._acc_green[:] = 0
        self.committed_shots = 0
        self.state = "learning"
        self._verify_pending = False
        self._inlier_low_shots = self._slow_lock_shots = self._green_low_shots = 0
        self.learned_miss_streak = 0
        self._armed_nolock_streak = 0
        self._provisional_clean = self._provisional_nolocks = 0
        self._bump()

    def wants_frames(self) -> bool:
        """True while a staging window is open and the calibrator is learning (SEED counts:
        the first committed shot moves SEED -> LEARNING)."""
        return self._stage is not None and self.state in ("seed", "learning", "relearn")

    def observe_shot_frame(self, locked: bool) -> None:
        """Every hw-armed frame (cheap): time-to-first-lock + armed no-lock accounting."""
        st = self._stage
        if st is None:
            return
        st["frames_seen"] += 1
        if locked:
            st["locked_any"] = True
            if st["frames_to_lock"] < 0:
                st["frames_to_lock"] = st["frames_seen"]

    def observe_feed(self, gated: bool) -> None:
        st = self._stage
        if st is None:
            return
        st["fed"] += 1
        if gated:
            st["gated"] += 1

    def note_release(self) -> None:
        """Corroboration proxy half 1: a release_marker arrived for this shot (plan B3 [fix]:
        no grade event crosses native->sidecar; release_marker + calibrator peak >= 70 is the
        proxy)."""
        if self._stage is not None:
            self._stage["release"] = True

    def note_ncc(self, score: float) -> None:
        self.ncc_ewma = 0.8 * self.ncc_ewma + 0.2 * float(score)

    def note_learned_miss(self, missed: bool) -> None:
        """Acquire attempted with the learned band; missed=True means the same-frame default-band
        retry succeeded where the learned band failed."""
        if missed:
            self.learned_miss_streak += 1
        else:
            self.learned_miss_streak = 0

    def observe_red_frame(self, frame, col, fill: float, velocity: float, consec: int,
                          rise_state: str, geom_gates: dict, green_seen: bool) -> None:
        """One qualifying-candidate frame (called by the reader ONLY while hardware-armed and
        evidence=='red'). Applies the B1 training gates, then harvests red-body + green-tip
        pixels into the per-shot staged histograms. In LOCKED this becomes the cheap
        inlier-probe path instead (staleness EWMAs still update)."""
        st = self._stage
        if st is None:
            return
        try:
            # the calibrator's OWN shot peak (B1 [fix]: the reader zeroes _peak_fill at lock
            # drop, long before the gate expiry -- the commit gate + corroboration proxy need
            # a peak that survives to end_shot). Tracked BEFORE any gating, in every state.
            st["peak"] = max(st["peak"], float(fill))
            cx, cy, cw, ch = [int(v) for v in col]
            H, W = frame.shape[:2]
            cx, cy = max(0, cx), max(0, cy)
            x1, y1 = min(W, cx + cw), min(H, cy + ch)
            if x1 - cx < 3 or y1 - cy < 3:
                return
            sub = frame[cy:y1, cx:x1]
            # `bar_src` is the sub-image in the space this colour LEARNS in. For a BGR colour it
            # is `sub` itself and every op below is byte-identical to the shipped path; for an
            # HSV colour the whole harvest (envelope mask, erosion, per-channel histograms,
            # inlier probe) simply runs on the HSV image instead, so one set of code learns both.
            if self.band_kind == "hsv":
                bar_src = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
                _, h_lo, h_hi, s_min, v_min = self.bar_env
                lo = np.array((int(h_lo), int(s_min), int(v_min)), np.uint8)
                hi = np.array((int(h_hi), 255, 255), np.uint8)
            else:
                bar_src = sub
                lo = np.array(self.red_env[0], np.uint8)
                hi = np.array(self.red_env[1], np.uint8)
            raw = cv2.inRange(bar_src, lo, hi)
            # LOCKED: cheap inlier probe (T-gates + one extra inRange count, ~0.05ms) so the
            # staleness EWMA keeps updating while the learned bands stay frozen.
            if self.state in ("locked", "provisional") and self.baked is not None:
                den = int(cv2.countNonZero(raw))
                if den >= 30:
                    lm = cv2.inRange(bar_src, np.array(self.baked["red_lo"], np.uint8),
                                     np.array(self.baked["red_hi"], np.uint8))
                    num = int(cv2.countNonZero(cv2.bitwise_and(raw, lm)))
                    st["inlier_num"] += num
                    st["inlier_den"] += den
                if green_seen:
                    st["green_seen"] += 1
                st["red_frames"] += 1
                return
            if not self.wants_frames():
                return
            # -------- B1 training-frame gates --------
            if consec < 4:
                return
            # cold-strictness geometry even while armed
            if not (geom_gates["w_min"] <= cw <= geom_gates["w_max"]):
                return
            if not (geom_gates["h_acq"] <= ch <= geom_gates["h_max"]):
                return
            if ch / float(max(1, cw)) < geom_gates["ar_min"]:
                return
            # column red_frac >= 0.55
            red_frac = cv2.countNonZero(raw) / float(max(1, (x1 - cx) * (y1 - cy)))
            if red_frac < 0.55:
                return
            # trajectory sanity: rising, or peak with a CHANGED fill (kills frozen-echo dupes)
            rising = velocity > 25.0 or rise_state == "rising"
            changed = st["last_fill"] is None or abs(fill - st["last_fill"]) > 1e-6
            if not (rising or (rise_state == "peak" and changed)):
                return
            if not changed:
                return
            st["last_fill"] = float(fill)
            # -------- harvest: red body (2px erosion vs MJPG chroma bleed) --------
            # Histogrammed in the LEARNING space (`bar_src`), so an HSV colour accumulates
            # (H, S, V) channel histograms where a BGR colour accumulates (B, G, R).
            er = cv2.erode(raw, np.ones((5, 5), np.uint8))
            pix = bar_src[er > 0]
            if pix.shape[0] >= 40:
                for c in range(3):
                    st["red"][c] += np.bincount(pix[:, c], minlength=256).astype(np.int64)
            else:
                return   # not enough clean body pixels -> not a qualifying frame
            # -------- harvest: green tip (fancy-index the ACTUAL green pixels, 3px erosion) ----
            gy0 = max(0, cy - 60)
            gsub = frame[gy0:cy + 8, max(0, cx - 6):min(W, x1 + 6)]
            if gsub.shape[0] >= 8 and gsub.shape[1] >= 4:
                ghsv = cv2.cvtColor(gsub, cv2.COLOR_BGR2HSV)
                gm = cv2.inRange(ghsv, np.array(self.green_env[0], np.uint8),
                                 np.array(self.green_env[1], np.uint8))
                gm = cv2.erode(gm, np.ones((7, 7), np.uint8))
                gpix = ghsv[gm > 0]
                if gpix.shape[0] >= 12:
                    for c in range(3):
                        st["green"][c] += np.bincount(gpix[:, c], minlength=256).astype(np.int64)
                    st["green_seen"] += 1
            st["red_frames"] += 1
            st["frames"] += 1
        except Exception:
            pass

    def end_shot(self) -> None:
        """Falling hw-arm edge: run the commit-or-discard gate, drive the lifecycle."""
        st, self._stage = self._stage, None
        if st is None:
            return
        try:
            corroborated = bool(st["release"]) and st["peak"] >= self.COMMIT_PEAK_MIN
            if self._user_cal is not None:
                self._user_cal["shots_seen"] += 1
            if self.state in ("seed", "learning", "relearn"):
                self._end_learning_shot(st)
            elif self.state == "locked":
                self._end_locked_shot(st)
            elif self.state == "provisional":
                self._end_provisional_shot(st, corroborated)
            if self._user_cal is not None:
                uc = self._user_cal
                if self.state == "locked":
                    self._user_cal = None      # baked + persisted inside _end_learning_shot
                elif uc["shots_seen"] >= uc["window"]:
                    # 10-shot window exhausted without FROZEN -> restore the snapshot
                    self._restore_snapshot()
                    self._user_cal = None
                    _cal_logger.warning("calibrate_meter: window exhausted -> snapshot restored")
        finally:
            self._bump()

    def _end_learning_shot(self, st) -> bool:
        """Commit-or-discard the staged shot. Returns True on commit."""
        ok = (st["peak"] >= self.COMMIT_PEAK_MIN and st["frames"] >= self.COMMIT_FRAMES_MIN)
        if ok:
            mm = _hist_median_mad(st["red"][2])   # explicitly staged R-channel histogram
            ok = mm is not None and mm[1] <= self.COMMIT_RED_MAD_MAX
        if ok:
            gm = _hist_median_mad(st["green"][0])
            ok = gm is not None and gm[1] <= self.COMMIT_GREEN_HUE_MAD_MAX
        if not ok:
            return False
        # 70% IoU agreement across shots: a committed shot whose own band disagrees with the
        # accumulation resets the accumulation (3 resets -> SEED).
        shot_bands = self._derive_bands(st["red"], st["green"])
        if shot_bands is None:
            return False
        if self.committed_shots >= 1:
            acc_bands = self._derive_bands(self._acc_red, self._acc_green)
            if acc_bands is not None:
                iou_r = _band_iou(shot_bands["red_lo"], shot_bands["red_hi"],
                                  acc_bands["red_lo"], acc_bands["red_hi"])
                iou_g = _band_iou(shot_bands["green_lo"], shot_bands["green_hi"],
                                  acc_bands["green_lo"], acc_bands["green_hi"])
                if min(iou_r, iou_g) < self.IOU_AGREE:
                    self.reset_count += 1
                    self._acc_red[:] = 0
                    self._acc_green[:] = 0
                    self.committed_shots = 0
                    if self.reset_count >= self.MAX_RESETS:
                        self.state = "seed"
                        _cal_logger.warning("colour learning: %d band resets -> SEED", self.reset_count)
                    return False
        self._acc_red += st["red"]
        self._acc_green += st["green"]
        self.committed_shots += 1
        if self._user_cal is not None:
            self._user_cal["commits"] = self._user_cal.get("commits", 0) + 1
        if self.state == "seed":
            self.state = "learning"
        if self.committed_shots >= self.FROZEN_SHOTS:
            self._bake()
        return True

    def _bake(self) -> bool:
        """FROZEN: derive + clamp + persist ONCE (no mid-learning fill-scale drift under the
        timing EMAs -- plan B1 [fix])."""
        bands = self._derive_bands(self._acc_red, self._acc_green)
        if bands is None:
            return False
        self.baked = bands
        self.learned_date = _datetime.date.today().isoformat()
        self.state = "locked"
        self._inlier_low_shots = self._slow_lock_shots = self._green_low_shots = 0
        self.learned_miss_streak = 0
        self._armed_nolock_streak = 0
        self.inlier_ewma = 1.0
        self._persist()
        _cal_logger.warning("colour bands FROZEN after %d shots: red %s-%s green %s-%s",
                            self.committed_shots, bands["red_lo"], bands["red_hi"],
                            bands["green_lo"], bands["green_hi"])
        self._bump()
        return True

    def _derive_bar_bgr(self, red_h, k):
        """BGR bar band: median +- clip(k*MAD) per channel + the CHANNEL-GAP invariant.

        Returns (lo, hi, env_lo, env_hi) or None. Byte-identical to the shipped inline code.
        """
        floors = self.cal_cfg.get("red_halfwidth_floor", self.RED_HW_FLOOR)
        caps = self.cal_cfg.get("red_halfwidth_cap", self.RED_HW_CAP)
        red_lo, red_hi = [0, 0, 0], [0, 0, 0]
        for c in range(3):
            mm = _hist_median_mad(red_h[c])
            if mm is None:
                return None
            med, mad = mm
            w = min(float(caps[c]), max(float(floors[c]), k * mad))
            red_lo[c], red_hi[c] = med - w, med + w
        # channel-gap invariant BEFORE the envelope clamp (it can only tighten further)
        gap_hi = red_lo[2] - float(self.cal_cfg.get("channel_gap", self.CHANNEL_GAP))
        red_hi[0] = min(red_hi[0], gap_hi)
        red_hi[1] = min(red_hi[1], gap_hi)
        return red_lo, red_hi, self.red_env[0], self.red_env[1]

    def _derive_bar_hsv(self, bar_h, k):
        """HSV bar band: learned as the low/high vectors of one cv2.inRange on the HSV image,
        i.e. lo = (hue_lo, s_floor, v_floor) and hi = (hue_hi, 255, 255).

        THE HSV EQUIVALENT OF THE CHANNEL-GAP INVARIANT. The BGR gap (hi_G, hi_B <= lo_R - 80)
        exists so a learned band is structurally INCAPABLE of representing neutral grey -- that is
        what makes a contaminated lock contribute nothing instead of something subtly wrong. Two
        conditions carry that same guarantee here:

          * SATURATION FLOOR >= HSV_SAT_FLOOR_MIN (90). Grey IS "saturation ~0", so a hard floor
            makes the entire grey axis unrepresentable, exactly as the gap does. This is the
            direct analogue and it is the load-bearing one.
          * HUE SPAN <= HSV_HUE_SPAN_MAX (24). A band that sprawls in hue has stopped being a
            colour and starts merging with its neighbours; capping the span keeps a noisy shot
            from widening the band into adjacent hues.

        Both are applied BEFORE the envelope intersection in _derive_bands, so the envelope can
        only tighten the result further and learning still strictly NARROWS. `hi[1]`/`hi[2]` are
        pinned at 255 (a saturation/value CEILING would reject the brightest, cleanest meter
        pixels -- the floors are the whole mechanism).
        """
        hm = _hist_median_mad(bar_h[0])
        if hm is None:
            return None
        hw = min(float(self.cal_cfg.get("bar_hue_halfwidth_cap", self.HSV_HUE_HW_CAP)),
                 max(float(self.cal_cfg.get("bar_hue_halfwidth_floor", self.HSV_HUE_HW_FLOOR)),
                     k * hm[1]))
        lo = [hm[0] - hw, 0.0, 0.0]
        hi = [hm[0] + hw, 255.0, 255.0]
        # HUE SPAN cap: shrink symmetrically about the measured median.
        span_max = float(self.cal_cfg.get("bar_hue_span_max", self.HSV_HUE_SPAN_MAX))
        if hi[0] - lo[0] > span_max:
            lo[0], hi[0] = hm[0] - span_max / 2.0, hm[0] + span_max / 2.0
        # S/V floors from the observed distribution, then the GREY-EXCLUSION floor on S.
        for i in (1, 2):
            mm = _hist_median_mad(bar_h[i])
            p05 = _hist_percentile(bar_h[i], 5)
            if mm is None or p05 is None:
                return None
            lo[i] = min(p05 - 20.0, 0.55 * mm[0])
        lo[1] = max(lo[1], float(self.cal_cfg.get("bar_sat_floor_min", self.HSV_SAT_FLOOR_MIN)))
        # Envelope for the intersection + seed-center containment in _derive_bands, in the same
        # (h, s, v) vector shape: the shipped nominal row for this colour.
        _, e_h_lo, e_h_hi, e_s, e_v = self.bar_env
        return lo, hi, (e_h_lo, e_s, e_v), (e_h_hi, 255, 255)

    def _derive_bands(self, red_h, green_h) -> Optional[dict]:
        """median +- clip(3.5*MAD, floor, cap) per channel; red baked in BGR with the channel-gap
        invariant, green in HSV with relative S/V floors; EVERYTHING intersected with the v1
        envelope (learned bands can only NARROW production)."""
        try:
            g_env_lo, g_env_hi = self.green_env
            k = float(self.cal_cfg.get("clip_k", self.CLIP_K))
            if self.band_kind == "hsv":
                bar = self._derive_bar_hsv(red_h, k)
                if bar is None:
                    return None
                red_lo, red_hi, r_env_lo, r_env_hi = bar
            else:
                bar = self._derive_bar_bgr(red_h, k)
                if bar is None:
                    return None
                red_lo, red_hi, r_env_lo, r_env_hi = bar
            # seed-center containment (B3): the learned interval must contain the envelope center
            # (floor/ceil so the uint8 rounding can never push the bound past the center)
            for c in range(3):
                center = 0.5 * (r_env_lo[c] + r_env_hi[c])
                red_lo[c] = min(red_lo[c], float(np.floor(center)))
                red_hi[c] = max(red_hi[c], float(np.ceil(center)))
                # envelope intersection = strict narrowing
                red_lo[c] = max(red_lo[c], r_env_lo[c])
                red_hi[c] = min(red_hi[c], r_env_hi[c])
                if red_lo[c] > red_hi[c]:
                    return None
            # green: hue med +- clip; S/V relative floors max(env, min(p05-20, 0.55*med))
            hm = _hist_median_mad(green_h[0])
            if hm is None:
                return None
            hw = min(float(self.cal_cfg.get("green_hue_halfwidth_cap", self.GREEN_HUE_CAP)),
                     max(float(self.cal_cfg.get("green_hue_halfwidth_floor", self.GREEN_HUE_FLOOR)),
                         k * hm[1]))
            g_lo = [hm[0] - hw, 0, 0]
            g_hi = [hm[0] + hw, g_env_hi[1], g_env_hi[2]]
            for i, ch in ((1, 1), (2, 2)):     # S, V floors
                mm = _hist_median_mad(green_h[ch])
                p05 = _hist_percentile(green_h[ch], 5)
                if mm is None or p05 is None:
                    return None
                g_lo[i] = max(float(g_env_lo[i]), min(p05 - 20.0, 0.55 * mm[0]))
            # hue: seed-center containment + envelope
            center_h = 0.5 * (g_env_lo[0] + g_env_hi[0])
            g_lo[0] = min(g_lo[0], float(np.floor(center_h)))
            g_hi[0] = max(g_hi[0], float(np.ceil(center_h)))
            g_lo[0] = max(g_lo[0], g_env_lo[0])
            g_hi[0] = min(g_hi[0], g_env_hi[0])
            if g_lo[0] > g_hi[0]:
                return None
            clamp8 = lambda v: int(max(0, min(255, round(v))))
            return {"kind": self.band_kind,
                    "red_lo": tuple(clamp8(v) for v in red_lo),
                    "red_hi": tuple(clamp8(v) for v in red_hi),
                    "green_lo": tuple(clamp8(v) for v in g_lo),
                    "green_hi": tuple(clamp8(v) for v in g_hi)}
        except Exception:
            return None

    def _end_locked_shot(self, st) -> None:
        """LOCKED: probe-path staleness accounting. Any tripped trigger -> PROVISIONAL."""
        trip = None
        if st["locked_any"]:
            self._armed_nolock_streak = 0
        else:
            self._armed_nolock_streak += 1
            if self._armed_nolock_streak >= self.ARMED_NOLOCK_TO_PROVISIONAL:
                trip = "armed_no_lock_x3"
        if st["inlier_den"] >= 200:
            frac = st["inlier_num"] / float(st["inlier_den"])
            self.inlier_ewma = 0.5 * self.inlier_ewma + 0.5 * frac
            if frac < self.INLIER_LOW:
                self._inlier_low_shots += 1
                if self._inlier_low_shots >= self.INLIER_LOW_SHOTS:
                    trip = trip or "inlier_low_x2"
            else:
                self._inlier_low_shots = 0
        if st["frames_to_lock"] > self.SLOW_LOCK_FRAMES:
            self._slow_lock_shots += 1
            if self._slow_lock_shots >= self.SLOW_LOCK_SHOTS:
                trip = trip or "slow_lock_x2"
        elif st["locked_any"]:
            self._slow_lock_shots = 0
        if st["red_frames"] >= 8:
            gp = st["green_seen"] / float(st["red_frames"])
            if gp < self.GREEN_PRESENCE_LOW:
                self._green_low_shots += 1
                if self._green_low_shots >= self.GREEN_LOW_SHOTS:
                    trip = trip or "green_presence_low_x2"
            else:
                self._green_low_shots = 0
        if self.ncc_ewma < self.NCC_EWMA_LOW:
            trip = trip or "ncc_ewma_low"
        if self.learned_miss_streak >= self.LEARNED_MISS_STREAK:
            trip = trip or "learned_miss_streak"
        if st["fed"] >= 10 and st["gated"] / float(st["fed"]) > self.GATED_RATE_TO_PROVISIONAL:
            trip = trip or "gated_rate_high"
        if trip is not None:
            self.state = "provisional"
            self._provisional_clean = 0
            self._provisional_nolocks = 0
            _cal_logger.warning("colour calibration -> PROVISIONAL (%s); learned bands frozen, "
                                "acquisition seed-union", trip)

    def _end_provisional_shot(self, st, corroborated: bool) -> None:
        """PROVISIONAL: 3 clean corroborated locks -> LOCKED; 6 no-locks (or user button)
        -> RELEARN. Learned bands stay FROZEN throughout."""
        inlier_ok = True
        if st["inlier_den"] >= 200:
            frac = st["inlier_num"] / float(st["inlier_den"])
            self.inlier_ewma = 0.5 * self.inlier_ewma + 0.5 * frac
            inlier_ok = frac >= (self.VERIFY_INLIER if self._verify_pending else self.INLIER_LOW)
        elif self._verify_pending:
            inlier_ok = False    # verify-before-trust demands demonstrated inliers
        if st["locked_any"]:
            if corroborated and inlier_ok:
                self._provisional_clean += 1
                self._verify_pending = False
                if self._provisional_clean >= self.CLEAN_LOCKS_TO_LOCKED:
                    self.state = "locked"
                    self._inlier_low_shots = self._slow_lock_shots = 0
                    self._green_low_shots = 0
                    self.learned_miss_streak = 0
                    self._armed_nolock_streak = 0
                    _cal_logger.warning("colour calibration -> LOCKED (3 clean corroborated locks)")
            elif self._verify_pending and not inlier_ok:
                # matched profile failed verification -> revert + LEARNING (verify-before-trust)
                _cal_logger.warning("profile verification failed -> revert + LEARNING")
                self._revert_to_learning()
        else:
            self._provisional_nolocks += 1
            if self._provisional_nolocks >= self.NOLOCKS_TO_RELEARN:
                _cal_logger.warning("colour calibration -> RELEARN (6 no-locks)")
                self._relearn()

    def _relearn(self) -> None:
        """Re-learn = revert to defaults + zero histograms. No EWMA widening, ever."""
        self.baked = None
        self._acc_red[:] = 0
        self._acc_green[:] = 0
        self.committed_shots = 0
        self.state = "relearn"
        self._verify_pending = False
        self._bump()

    # ---- calibrate_meter (the wired UI stub, B3) ---------------------------------------------------
    def _take_snapshot(self) -> None:
        self._snapshot = {
            "state": self.state, "baked": dict(self.baked) if self.baked else None,
            "learned_date": self.learned_date, "committed_shots": self.committed_shots,
            "reset_count": self.reset_count,
            "acc_red": self._acc_red.copy(), "acc_green": self._acc_green.copy(),
        }

    def _restore_snapshot(self) -> None:
        s = self._snapshot
        if s is None:
            return
        self.state = s["state"]
        self.baked = dict(s["baked"]) if s["baked"] else None
        self.learned_date = s["learned_date"]
        self.committed_shots = s["committed_shots"]
        self.reset_count = s["reset_count"]
        self._acc_red = s["acc_red"].copy()
        self._acc_green = s["acc_green"].copy()
        self._snapshot = None
        self._bump()

    def start_user_calibration(self, target: int = 5, window: int = 10) -> None:
        """start = snapshot + reset + RELEARN with a 10-shot window."""
        self._take_snapshot()
        self._relearn()
        self._user_cal = {"target": max(1, int(target or 5)), "window": max(1, int(window or 10)),
                          "shots_seen": 0, "commits": 0}
        self._bump()

    def cancel_user_calibration(self) -> None:
        """cancel = restore the snapshot."""
        if self._user_cal is None and self._snapshot is None:
            return
        self._user_cal = None
        self._restore_snapshot()

    def finish_user_calibration(self) -> None:
        """finish = derive/clamp/persist + LOCKED (>=1 committed shot; the envelope clamp makes
        even a 1-shot bake safe -- it can only narrow). Nothing committed -> restore."""
        if self._user_cal is None:
            return
        uc, self._user_cal = self._user_cal, None
        if uc.get("commits", 0) >= 1 and self.state in ("learning", "relearn", "seed"):
            if not self._bake():
                self._restore_snapshot()
        elif self.state != "locked":
            self._restore_snapshot()
        self._snapshot = None
        self._bump()


class SimpleMeterReader:
    """Shot-gated, matchTemplate-tracked, confidence-decay, green-cap-anchored fill reader for the
    Red/Arrow2 park meter. Drop-in for MeterDetector.detect() behind ORION_SIMPLE_READER.

    Construct either as SimpleMeterReader(frame_w, frame_h) (explicit dims, e.g. the head-to-head
    harness / unit tests) or SimpleMeterReader(cfg=det_config) (live wiring; dims are picked up
    lazily from the first frame)."""

    # --- meter geometry priors: EXACT 2k_Vision Arrow2.json params (@1080p) ---
    # search band 5/250/1915/770 -> right trimmed to 1815 to drop the STATIC NBA-logo banner
    # (x~1846-1902) which yields a width-30 red contour the shape gate alone can't reject.
    BAND = (5, 250, 1815, 770)        # x0,y0,x1,y1 px @1080p
    # Notch band around the meter's bottom edge (meter_detector._grab_patch: b-12 .. b+5).
    _NOTCH_ABOVE = 12
    _NOTCH_BELOW = 5
    W_MIN, W_MAX = 20, 34             # Arrow2 contour width 23-30 (+/- capture slack)
    H_ACQ = 33                        # Arrow2 contour height min 33 (cold acquire)
    H_ACQ_ARMED = 15                  # RELAXED acquire height when the shot-gate is ARMED (early rise)
    H_MAX = 165                       # Arrow2 contour height max 165
    H_HOLD = 12                       # relaxed min to HOLD a tracked lock (deflate tail)
    AR_MIN = 1.8
    # When the shot-gate is ARMED, relax BOTH the height AND the aspect floor: an early-rise red
    # column is SHORT (height < 33) and therefore not yet "tall thin" (aspect < 1.8), so the height
    # gate alone can't admit it (the 1.8 aspect on a >=20px-wide column already implies height >=36).
    # Relaxing aspect too is what actually recovers the first 3-7 rise frames -- safe because it only
    # applies while the bot is provably shooting (held-trigger / v9 pose corroboration).
    AR_MIN_ARMED = 0.7
    # confidence-decay lock: EXACT Arrow2 {initial 1.0, minimum 0.95, rate 0.01}. No warmup.
    # ~5 coast frames (~83ms @60fps). WHILE ARMED, decay slower so the lock survives a full
    # occlusion string (~25 frames / ~416ms) -- safe because it is shot-gated (the bot is provably
    # shooting, so a run of blurred/occluded/overlay-absent misses is the meter, not a false hold).
    # 25 frames covers an arm/body crossing a STATIONARY meter, not just a fast-fade blur.
    CONF_INIT, CONF_MIN, CONF_RATE = 1.0, 0.95, 0.01
    CONF_RATE_ARMED = 0.002
    NCC_LOCK = 0.60                   # grayscale matchTemplate TM_CCOEFF_NORMED (2k26.py uses 0.8)
    ROI_PAD_X, ROI_PAD_UP, ROI_PAD_DN = 34, 110, 26
    # RED fill: EXACT Arrow2 BGR [0,0,220]-[60,60,255]
    _RED_LO, _RED_HI = (0, 0, 220), (60, 60, 255)
    _G = ((38, 90, 90), (85, 255, 255))        # green make-window cap (2k26 green band)
    # Distant, anti-aliased meters can lose their last pristine centre pixel even though the
    # red body and green cap remain plainly connected on screen.  These bands are deliberately
    # available ONLY to the physically-armed court-wide *two-colour structure* fallback below.
    # They are the same vetted relaxed red/SV floors already used by the bounded occlusion
    # relocate path; ordinary/menu/cold scans keep the production bands above byte-for-byte.
    _COURTWIDE_RED = ((0, 0, 170), (70, 70, 255))
    _COURTWIDE_GREEN = ((38, 75, 75), (85, 255, 255))
    # Sub-quarter-scale H.264/JPEG chroma averaging raises the red pixel's B/G
    # channels and lowers the tiny green cap's saturation/value.  These wider
    # bands are NEVER used by normal acquire: only the physically-armed,
    # two-unique-frame micro structure tier may select and latch them.
    _MICRO_RED = ((0, 0, 110), (110, 110, 255))
    _MICRO_GREEN = ((35, 45, 45), (90, 255, 255))
    # The micro tier's OWN scale ceiling: the largest green cap it will look at, as a fraction
    # of the nominal (seed) cap width. It is what makes the tier "sub-quarter-scale" -- and it
    # is therefore also the scale claim a micro candidate implicitly makes about the capture,
    # which the B9 session-scale gate checks against the reader's measured full-track height
    # (see _micro_tier_plausible). Consumed by _acquire_structure_tip_first(micro=True).
    MICRO_CAP_SCALE = 0.44
    # GREEN make-window TIP = the meter's FIXED STRUCTURE (present at EVERY fill level: rise, cap,
    # deflate, arc). Measured on the live Red/Arrow2 meter: w~28-30, h~24-30, area~500-690 @1080p.
    # This is the invariant the lock anchors on so the box STAYS GLUED when the red fill caps (turns
    # green) or deflates -- the red column is only READ, never the sole lock authority. DÃ©cor
    # (window mullions, court, logos) carries NO saturated green in the band -> no false hold.
    GW_MIN, GW_MAX = 16, 46           # green tip contour width @1080p
    GH_MIN, GH_MAX = 6, 40            # green tip contour height @1080p
    G_AREA_MIN = 200                  # green tip min filled area @1080p
    # --- POP-IN (fade) STRUCTURE ACQUIRE priors @1080p. On a FADE the meter is NOT on screen
    # during most of the hold: it POPS IN fully-formed (ring + tiny green tip dot + a SHORT red
    # stub, h~16-38, aspect 0.5-1.6) only ~60ms before the release, crisp, no blur (measured on
    # session_20260706_203024 f01094-f01096 / f01168-f01171). The cold gates (H_ACQ=33, AR 1.8)
    # reject exactly those frames, so the whole visible rise is lost. The pop-in invariant is
    # STRUCTURE: a short saturated-red stub with the meter's green tip dot a fixed ~150-165px
    # above its floor (track height), horizontally centred on it. Requiring BOTH colours in that
    # geometric relation is the decor guard (no gate/arm needed): a red banner has no green dot
    # 110-190px above it within +/-24px of centre; a green scoreboard has no red stub below.
    TIP_DX = 24                       # tip-dot horizontal window half-width around stub centre
    TIP_UP_MIN, TIP_UP_MAX = 110, 190 # tip dot sits this far above the stub FLOOR (track ~150-165)
    TIP_PX_MIN = 4                    # min strict-green px in the tip window (dot is ~6-36px)
    STUB_H_MIN = 10                   # min pop-in red stub height
    # N7 EARLY-STUB: the floor a stub may reach when it is corroborated by the STRICT
    # connected green tip instead of the loose tip-pixel count.  Real-pixel ramp
    # (framedump tracks re-cut to a known fill from their OWN unfilled-track pixels):
    # pop-in stub floor 10px (~8.5% fill) vs 2-4px on the court-wide tiers.  10 -> 4 px
    # is ~3.7pp of meter, ~20ms at the measured 0.189 pp/ms live rise -- the
    # recoverable part of the cold re-acquire.
    # RECORD CORRECTED 2026-08-03: this gap was first blamed on the reader running
    # UNARMED through acquisition, inferred from the "POSE ARM" log line landing ~1ms
    # AFTER ownership.  That line belongs to the LATER pose_arm command; the t=0
    # native shot_gate_arm (silent at the time) demonstrably lands: same session,
    # 02:48:45.350 "Physical shot epoch: epoch=1" -> 02:48:45.352 "SHOT-GATE ARMED
    # ... src=square_edge" (a src only the native first-edge command can set; the CV
    # self-arm writes 'cv', pose_arm writes 'pose'), and no DISARMED follows for the
    # rest of the session, so `armed`/`hw` held through every acquisition.  The 10px
    # floor is real but its cause is TIER ORDERING/corroboration strength (armed scan
    # floor 15px; the 2-4px tiers demand stricter structure), not a closed gate.  The
    # "shot_gate_arm send" / "SHOT-GATE ARM RECEIPT" / "READER ACQUIRE" forensic
    # lines now make the armed state at every lock seat directly observable.
    STUB_H_MIN_EARLY = 4              # strict-tip-corroborated stub floor (nominal band)
    STUB_RED_FRAC = 0.35              # merged stub fill ratio (pop-in stub can fragment)
    VEL_MAX = 500.0                   # clamp, mirrors DetectorConfig.velocity_max_pct_per_sec
    VEL_WIN = 6                       # rolling wall-time fill samples for the velocity slope
    _REF_W, _REF_H = 1920.0, 1080.0   # priors are px @1080p; scaled to the live capture size
    # EPOCH-2: instance-level twin of _read_fill(probe=...) -- see that method. Class-level default
    # so it is always present (subclass __init__ ordering / pickled readers) and always False
    # outside the one speculative call site that latches it.
    _probe_read = False
    # EPOCH-3/5: set by _read_fill when the fill it just computed rests on a denominator that is
    # NOT a measured full track ('no_cap_anchor' -> geometric prior, 'strip_truncated' -> the strip
    # was clipped shorter than the denominator). read() refuses to emit those numbers.
    _read_bail = None
    # EPOCH-3: has this reader EVER measured a real full-track height? SESSION-scoped -- deliberately
    # NOT cleared by _reset_state(), because it is what distinguishes the two ways _track_h_hist can
    # be empty: a COLD reader that has simply not seen a cap yet (legitimate -- the green-absent
    # frame is still fed, engine times the tip) from a reader whose history was WIPED by
    # reset_tracking() mid-session (a live colour/style change), where an unanchored read is a
    # confidently-wrong number the bot would time a shot off.
    _ever_capped = False
    # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] class-level defaults so the press-clock veto
    # state is always present (subclass __init__ ordering / pickled readers): flag-off and
    # reference-less until __init__ / the per-press reset install the live values.
    _press_onset_plaus = False
    _press_last_pub_fill = None
    _press_last_pub_ts = None
    _press_onset_cand = None
    _press_implausible_n = 0
    _press_implausible_logged = False
    _press_rise_run = 0

    def __init__(self, frame_w: Optional[int] = None, frame_h: Optional[int] = None,
                 cfg=None, shot_gate=None, params: Optional[ReaderParams] = None,
                 calibrator: Optional[ColorCalibrator] = None,
                 require_gameplay_eligibility: bool = False):
        self.W = int(frame_w) if frame_w else 0
        self.H = int(frame_h) if frame_h else 0
        self._cfg = cfg                          # DetectorConfig (orch update_meter reads det._cfg)
        # The orchestrator mutates the shared cfg BEFORE calling set_active_style.
        # Keep our own identity so a real profile change cannot look like a no-op.
        self._tracking_meter_style = str(getattr(cfg, "meter_style", "") or "").strip().casefold()
        self._shot_gate = shot_gate              # optional callable() -> (armed, pose_conf)
        self._shot_armed = False
        self._shot_armed_hw = False              # PHYSICAL arm only (set_shot_state 3rd arg);
        self._prev_hw_armed = False              # the CV self-arm can NEVER set this (B1 [fix])
        self._shot_pose_conf = 0.0
        # Production trust boundary.  A red HUD/logo candidate is not proof that gameplay (or a
        # shot) exists: the live ESRB screen produced exactly that false lock.  The orchestrator
        # enables this gate and supplies `_shot_armed_hw` only from controller/native shot-start
        # events.  Standalone replay/diagnostic readers keep the historical ungated behaviour.
        self._require_gameplay_eligibility = bool(require_gameplay_eligibility)
        self._gameplay_gate_closed = True
        self._gameplay_lock_authorized = False
        # Independent proof for the CURRENT physical hardware-shot epoch. Motion/rise alone
        # may authorize ordinary detector publication, but it cannot set this bit: an animated
        # loading/menu bar can rise too. Only connected green-meter structure (or the reader's
        # strict court-wide structural lock) may latch it, and physical arm boundaries clear it.
        self._gameplay_structure_verified = False
        self._gameplay_structure_proof_epoch = 0
        self._physical_shot_epoch = 0
        # [ORION_PLAYER_ANCHOR 2026-09-15] The press window, republished on the FRAME clock.
        # The locator runs on the detector worker thread and never sees an epoch, a press or
        # a release; this reader is the only object that holds a press epoch and a frame
        # timestamp at the same instant, so it is the publisher. See player_anchor.ArmState.
        self._pa_epoch = 0
        # [ORION_SHOT_GATE_TYPE 2026-09-15] The engine's own classification of the live press
        # ("Standstill" / "Left Fade" / ...) and whether this configuration releases with the
        # Rhythm flick, both delivered by the shot_gate_arm command and republished with the
        # press below. Empty type = unknown, which is what an old engine, a stick/probe edge
        # or a local self-arm produces; the locator then keeps the UNION onset window.
        self._pa_shot_type = ""
        self._pa_rhythm = False
        self._pa_shot_type_epoch = 0
        # [ORION_SHOT_GATE_RELEASE 2026-09-15] The epoch whose press window has already been
        # closed by an engine release/disarm marker. Without it the per-frame republish below
        # would simply re-arm the press it just closed, one frame later.
        self._pa_released_epoch = 0
        # Last frame timestamp the republish saw: the press window lives on the FRAME clock,
        # but a release marker arrives on the control thread with no frame in hand.
        self._pa_last_ts = None
        self._gameplay_verify_frames = 0
        # STRUCTURE NECROPSY (2026-08-30, diagnostics only -- zero behaviour change).
        # Live 20260830_112306: the engine aborted 18 presses with
        # ownership_structure_stamp_missing / stamp_epoch_seen=0, yet a faithful offline
        # replay of the SAME frames + press times latches the proof on every one of those
        # epochs (green cap present on 37/37 rises; latch <=40% fill on 37/37 runs). The
        # divergence is live-only state this census names at the NEXT live run: per hw
        # epoch, how many frames detect() EMITTED as detected, how many reached
        # _qualify_gameplay_sample detected, how many of those carried the green cap, the
        # best consecutive-fresh-rise count, and whether the latch ever fired. Logged at
        # ERROR once per closing epoch that had frames but no latch (untrottled by the
        # native relay, ~one line per silent shot).
        self._ep_census = {"epoch": 0, "emit_det": 0, "qual_det": 0, "green": 0,
                           "max_fresh_up": 0, "latched": False, "authorized": 0,
                           "last_stage": ""}
        # A physically-armed discontinuous re-seat is a new visual identity, so it must prove
        # structure/rise again before timing can trust it.  Keep that bounded re-verification
        # visible to the overlay as sampler-stale continuity; otherwise the production scene gate
        # turns read()'s intentional `held_reseat` into a detected=False blink.
        self._gameplay_reverify_visible = False
        self.last_debug: dict = {}               # orch reads getattr(det,'last_debug',{})
        # --- Track B (Phase 3) flags: default OFF = byte-identical shipped behaviour ---
        # ORION_COLOR_CAL=1  -> ambient ColorCalibrator (B1) + lifecycle (B3)
        # ORION_READER_ROBUST=1 -> occlusion fill read / trajectory gate / dead-reckon coast /
        #                          blur adaptations / scale rebase (B2)
        self._robust = _os.environ.get('ORION_READER_ROBUST', '0').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        # ORION_READER_OCCLUSION -> the within-shot occlusion mitigations (DEFAULT ON, so the live
        # process gets them without any env var): stationary-meter relaxed-red relocate retry,
        # forward-predicted coast box, velocity carried through green-hold, top-cap fill anchor.
        # These are all ADDITIVE (relaxed retry runs only after the strict scan misses; box predict
        # only shifts when the box is moving; top-cap == longest on a single-run cap) so a pristine
        # clean frame is byte-identical -- quality stays on the pristine path. Set '0' to disable.
        self._occl = _os.environ.get('ORION_READER_OCCLUSION', '1').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        # ------------------------------------------------------------------ #
        #  CAMERA-ADAPTIVE / BULLETPROOF toggles. Every one defaults to the
        #  current tuned behaviour on a NOMINAL, standstill camera and is guarded
        #  so a pristine read stays BYTE-IDENTICAL:
        #    * scale-adapt only ENGAGES once a confident lock has MEASURED the meter
        #      off-nominal (beyond a deadband); nominal -> gates unchanged.
        #    * tip-enforce only ADDS upward lift when the walk/cap would otherwise
        #      clip; it never lowers the top and is decoupled from the fill scale.
        #    * armed-hold only extends the MISS-frame coast while the shot-gate is
        #      ARMED; present-meter frames are untouched.
        #  Set the env var to '0' to disable (numeric envs override the default).
        # ------------------------------------------------------------------ #
        def _flag(name, dflt):
            return _os.environ.get(name, dflt).strip().lower() in ('1', 'true', 'yes', 'on')

        def _fnum(name, dflt):
            try:
                return float(_os.environ.get(name, '').strip() or dflt)
            except Exception:
                return float(dflt)

        def _inum(name, dflt):
            try:
                return int(float(_os.environ.get(name, '').strip() or dflt))
            except Exception:
                return int(dflt)
        # (1) size-gate scale adaptivity (default ON) + its rolling estimate window + deadband.
        self._scale_adapt = _flag('ORION_READER_SCALE_ADAPT', '1')
        try:
            _wlo, _whi = (_os.environ.get('ORION_READER_SCALE_WINDOW', '') or '0.6,1.6').split(',')
            self._scale_win_lo, self._scale_win_hi = float(_wlo), float(_whi)
        except Exception:
            self._scale_win_lo, self._scale_win_hi = 0.6, 1.6
        # deadband can be tight because the estimate is SELF-REFERENCED (nominal reads ~1.0
        # exactly), so only real zoom crosses it while a nominal read stays dormant.
        self._scale_deadband = max(0.0, _fnum('ORION_READER_SCALE_DEADBAND', 0.10))
        self._scale_min_samples = max(2, _inum('ORION_READER_SCALE_MIN_SAMPLES', 5))
        # EPOCH-7: how far a single width sample may sit from the running estimate before it is
        # treated as measurement noise rather than zoom, and how tightly two consecutive outliers
        # must agree to be believed anyway (a genuine hard camera cut). See _note_scale_sample.
        self._scale_step_max = max(0.05, _fnum('ORION_READER_SCALE_STEP_MAX', 0.35))
        self._scale_corrob_tol = max(0.01, _fnum('ORION_READER_SCALE_CORROB_TOL', 0.15))
        self._scale_outlier = None       # last refused sample (pending corroboration)
        self._aspect_tol = max(1.0, _fnum('ORION_READER_ASPECT_TOL', 1.15))
        # (2) tip-capture enforcement (default ON) + the style chevron lift (px @1080p).
        self._tip_enforce = _flag('ORION_READER_TIP_ENFORCE', '1')
        self._tip_lift_px = max(0, _inum('ORION_READER_TIP_LIFT_PX', 7))
        # (2b) FIX-1 "no elongation" (ORION_READER_TIP_TIGHT; set '0' for the legacy fixed lift):
        # the shipped tip-capture guarantee ALWAYS raises the emitted box top to a FIXED
        # `_apex_lift` px ABOVE the green cap -- even when the chevron/apex walk found NOTHING
        # up there -- so the box overhangs the meter into empty space (vertically ELONGATED).
        # TIGHT mode lifts the top ONLY to the ACTUALLY-DETECTED apex row (the silver chevron
        # the walk really saw); with no apex the box hugs the green cap (zero forced headroom).
        # A real arrow apex is still always covered (the walk's row IS the apex), the fill %%
        # is untouched (it reads red_top/fillable_h, never the box), and the lift stays
        # raise-only, so a real tip can never be clipped that was not clipped before.
        self._tip_tight = _flag('ORION_READER_TIP_TIGHT', '1')   # FLIPPED default-ON 2026-07-19: box_excess_headroom p50 -> 0 px + uncovered-apex frames 5/88/77 -> 0 on sessions 231912/190737/210801; 51/51 gates, top_clips 0, fill byte-identical
        # (2c) BOX-HUG (ORION_READER_BOX_HUG, default ON) -- the served box must trace the METER,
        # not the coloured TRACK. Measured on session_20260804_032333 f01806 (a pristine ~97%
        # frame, 1280x720): the meter's drawn outline spans x 324-347 / y 321-437 (the silver
        # frame plus the top arrow apex and the bottom chevron), while the served bbox is
        # x 328-344 / y 325-433 -- i.e. the box is drawn ON the red bar, INSIDE the frame, with
        # the apex and the bottom chevron OUTSIDE it (4 px short on all four sides). That is not
        # a draw inset: `_enclose_meter_bbox` deliberately RESETS the output width to the
        # measured red body (its own comment: the +-3px timing pad "is not visible meter
        # structure"), and the only structure it adds back is a green cap and a SHORT silver
        # contour directly above it -- the meter's frame is one ~24x115 connected component, far
        # past that method's `max_silver_h` (~11 px @720p), so it can never be unioned.
        #
        # The fix measures the outline instead of assuming it. The frame is a FLAT-SHADED HUD
        # stroke: its columns are constant down the whole track (measured per-column vertical
        # sd over the track's interior rows: 0.0-0.5 on every meter column, 4.7-13.5 on the very
        # first background column either side) while the world behind the meter never is. Walk
        # out from the red body while the columns stay flat AND differ from the local background,
        # and the outline's horizontal extent falls out by measurement. The stroke thickness that
        # reveals is the SAME stroke that wraps the top apex and the bottom chevron, so the
        # identical margin closes the box vertically. See _hug_outline_bbox.
        #
        # Strictly grow-only and output-only: fill/velocity/masks/`self.box` are untouched, so
        # `top_clips` (box top vs the red top row) can only improve. A synthetic/flat-background
        # fixture has no outline to find (the adjacent columns ARE the background) -> zero
        # growth -> byte-identical, which is why the unit suite's exact-bbox assertions still hold.
        self._box_hug = _flag('ORION_READER_BOX_HUG', '1')
        # Per-column vertical sd (mean over BGR) below which a column is flat-shaded HUD.
        self._box_hug_sd = max(0.1, _fnum('ORION_READER_BOX_HUG_SD', 2.5))
        # ...and the minimum per-channel distance from the local background that column must
        # keep, so a flat BACKGROUND (a synthetic fixture, a plain wall) is never walked into.
        self._box_hug_bg_delta = max(1.0, _fnum('ORION_READER_BOX_HUG_BG_DELTA', 18.0))
        # (3) no-disappear-during-shot armed hold (default ON) + the wall-clock coast cap (frames).
        self._armed_hold = _flag('ORION_READER_ARMED_HOLD', '1')
        self._armed_coast_max = max(1, _inum('ORION_READER_ARMED_COAST', 180))
        # Seconds a WHITE lock may report with no green cap before it is broken.
        # 2.5 against a measured genuine worst case of ~1.75s = ~40% headroom.
        # 0 disables.
        self._capless_max_s = max(0.0, _fnum('ORION_READER_WHITE_CAPLESS_S', 2.5))
        # Held fill at/above which a NEW physical shot epoch drops the inherited
        # lock as stale. Matches AppConfig.anchorMaxFirstFillPct (40): above that
        # the engine will refuse to own the shot anyway, so carrying the lock
        # across can only poison the press. 0 disables.
        self._stale_press_drop_pct = max(0.0, _fnum('ORION_READER_STALE_PRESS_DROP_PCT', 40.0))
        # Green pixels inside the top 35% of the box that count as "cap present".
        # 8 measured: keeps 91% of true locks, admits 5% of false ones.
        self._capless_px_min = max(1, _inum('ORION_READER_WHITE_CAP_PX', 8))
        # Max fraction of a window's frames that may show a cap and still count
        # as capless. A true meter measures ~91%; 0.25 leaves wide margin.
        self._capless_rate_max = min(1.0, max(0.0,
            _fnum('ORION_READER_WHITE_CAP_RATE_MAX', 0.25)))
        # (3c) DETECTOR-DRIVEN LOCATION (ORION_METER_DETECTOR; default OFF in code, ENABLED
        # by run_orion.local.ps1 for live 2K27 -- off keeps the unit suite's synthetic frames,
        # which the real-meter detector correctly rejects, from being vetoed). The colour
        # locator finds a meter by scanning for bright columns, and an arena is full of
        # them; measured on the 2K27 white meter it mis-located 99% of its detections
        # (latching the white jersey ~49px beside the real bar: reader x534 vs true x485)
        # and MISSED 471 of 756 real meters a trained detector found. So a single-class
        # YOLO11n (2079 labelled 2K27 frames + 693 non-gameplay negatives, mAP50-95 0.973)
        # PROPOSES where the meter is; the colour reader still measures fill/green INSIDE
        # that box through its shipped relocate/_read_fill path. The detector runs on an
        # async worker thread so its inference (~14 ms GPU / ~174 ms CPU) NEVER blocks the
        # timing loop -- the reader consumes the freshest result with a TTL staleness guard.
        # Fully subtractive w.r.t. risk: if onnxruntime or the model is absent the locator
        # is None and every path below is byte-identical to the shipped colour reader.
        self._meter_detector = None
        self._det_seeded = False
        self._det_no_meter = False
        self._det_region = None
        # DETECTOR-AUTHORITATIVE state. Live, read()'s white-blind acquire wandered back onto
        # décor even with the box seeded (rendered proof: detector dead-on the meter, reader on
        # the player at 0% fill). So when the detector holds a meter we measure fill DIRECTLY in
        # its box and emit that. _det_last_box/ts bridge brief async found=False gaps so the box
        # persists through the shot; _det_fill_hist is an independent velocity history (read()'s
        # is polluted by the décor lock).
        self._det_active_box = None
        self._det_last_box = None
        self._det_last_found_ts = -1.0e9
        self._det_fill_hist = _collections.deque(maxlen=12)
        # Press/ghost ownership uses the same row-quantized ruler native consumes. Keep a
        # parallel history so that choice does not lower the resolution of the velocity fit.
        self._det_coarse_fill_hist = _collections.deque(maxlen=12)
        self._det_hold_s = max(0.0, _fnum('ORION_METER_DETECTOR_HOLD_S', 0.35))
        # BOX MOTION EXTRAPOLATION. The detector runs on its own thread at ~50ms inference
        # (+ throttle), so its newest box is tens of ms STALE -- and the 2K27 meter is anchored
        # to the SHOOTING PLAYER, so a stale box trails a moving meter. That is both the visible
        # "detection lags behind the meter" and a silent accuracy bug: fill is measured inside a
        # mispositioned box, which feeds the engine bad first-fills (-> ownership_proof_incomplete)
        # and late samples (-> live_tip_deadline_missed). Tracking the box's own velocity and
        # extrapolating it to NOW cancels that staleness with no extra GPU cost.
        # Detector motion is represented by the same anchors used everywhere downstream:
        # horizontal centre + BOTTOM edge.  Do not store vertical centre here.  The learned
        # detector's height breathes by a few pixels as the ribbon fills/zooms; with a fixed
        # bottom, ``cy = bottom - h/2`` therefore moves even though the meter does not.  Feeding
        # that artefact to vertical extrapolation manufactured ~5-10px tracking errors -- nearly
        # the measured 12px fill-dropout threshold.  Tuple = (source_ts, cx, bottom, w, h).
        self._det_box_hist = _collections.deque(maxlen=6)
        self._det_hist_velocity_cache = None
        # Per-frame NCC tracker (see _track_box_ncc): template + search window + accept floor.
        self._det_tmpl = None
        self._det_tmpl_size = None      # detector dimensions at the appearance seed
        self._det_tmpl_cx_offset = 0.0
        self._det_tmpl_bottom_offset = 0.0
        self._det_track_score = 0.0
        # Smoothed per-axis velocity of the tracked notch (px/frame).  Horizontal motion
        # dominates ordinary pans, but moving/fade shots also camera-dive vertically; both
        # axes must predict the search centre or the vertical matcher trails and drops weak.
        self._det_track_vx = 0.0
        self._det_track_vy = 0.0
        self._det_track_last_match = None
        # Velocity retains the historical px/sample units, paired with the actual
        # source-time interval that produced it. A dropped/occluded frame is not
        # one nominal frame of movement; only the bounded SEARCH centre advances
        # by elapsed time, never the served position or visibility authority.
        self._det_track_velocity_dt_s = 1.0 / 60.0
        self._det_track_box_ts = None
        self._det_track_last_match_ts = None
        self._det_track_sample_ts = None
        self._det_track_clock_timed = None
        self._det_track_scale_match = None  # current-frame appearance/geometry proposal only
        self._subpx_camera_ref = None       # canonical ruler + independently measured span
        self._subpx_camera_scale = 1.0
        self._det_scale_reference = None    # fixed appearance at an accepted physical scale
        self._det_scale_pending = None      # two unique local-pixel resize observations
        self._det_track = _flag('ORION_METER_DETECTOR_TRACK', '1')
        # +/-30px @720p is the retired tracker's _TPL_WIN, sized for the ~9-13px/frame camera stride.
        self._det_track_pad_x = max(4, _inum('ORION_METER_DETECTOR_TRACK_PAD_X', 30))
        self._det_track_pad_y = max(4, _inum('ORION_METER_DETECTOR_TRACK_PAD_Y', 22))
        # 0.55 = the retired tracker's measured floor on this same notch patch (match is 0.74-1.0
        # while the meter is visible, <=0.24 once it is gone), so it is a real discriminator
        # rather than the 0.45 guess the whole-box version used.
        self._det_track_min = min(1.0, max(0.0, _fnum('ORION_METER_DETECTOR_TRACK_MIN', 0.55)))
        # ---- TRACK CONTINUITY (2026-08-30, the Left_Fade fill-dropout fix) -------------- #
        # Measured on session_20260828_195034 e12 (instrumented replay at bench pacing): during
        # a fade pan the meter moves ~0.12px/ms while the detector's newest box is 60-180ms
        # stale, so every fresh box is already 7-22px behind the meter -- and the fill read
        # hard-zeroes past ~12px of box error (offset sweep on frames 2371/2374/2377: clean
        # plateau within +-10px, 0.00 at +-12..14). The shipped wiring then made it worse:
        # the template was seeded from the CURRENT frame at that STALE box (baking the lag
        # into the template) and the tracker was re-based on the stale detector box every
        # frame, so the NCC match (score ~1.0, a partial self-match) SNAPPED the box back to
        # the stale position, actively undoing the velocity extrapolation. Result: fill 0.00
        # on 69-75% of every Left_Fade rise -> engine starved -> detector_authority_lost_abort
        # on 100% of typed Left_Fade runs (bench run_20260830). _det_track_step keeps the
        # tracker's OWN box across frames instead (1-frame-old reference), refreshes the
        # template from the TRACKED position at detector cadence after localization, and stays
        # bounded to the detector through a small per-frame pull toward its (extrapolated) box
        # plus a hard snap when they disagree by more than the snap gate.
        self._det_track_cont = _flag('ORION_METER_TRACK_CONTINUITY', '1')
        # Per-frame fraction of the (tracked - detector-box) gap pulled back toward the
        # detector: template random-walk drift and any seed bias decay geometrically at this
        # gain, while the equilibrium cost during a pan is only ~gain * detector-lag (~1-2px).
        self._det_track_gain = min(1.0, max(0.0, _fnum('ORION_METER_TRACK_RECONCILE_GAIN', 0.15)))
        # On a trusted moving track the detector proposal is commonly 40-100px behind
        # the current frame.  A percentage-only pull therefore drags an accurate NCC
        # match backward by 6-15px in one frame.  Retain the strong static correction,
        # but cap the moving-track reconciliation impulse to a physical 2px/frame.
        self._det_track_reconcile_max_px = max(
            0.0, _fnum('ORION_METER_TRACK_RECONCILE_MAX_PX', 1.5))
        # Tracked-vs-detector disagreement (px @720p width scale, either axis) beyond which
        # the detector wins outright and the template is dropped with the position. Must sit
        # ABOVE the worst honest pan lag of a raw age-capped detector box (~22px measured) or
        # the stale box steals the lock back mid-pan; teleports are hundreds of px.
        self._det_track_snap_px = max(4.0, _fnum('ORION_METER_TRACK_SNAP_PX', 26.0))
        # A template that keeps matching weak for longer than this is stale appearance
        # (occlusion/lighting), not a briefly-blurred notch -> re-bootstrap at the detector box.
        self._det_tmpl_max_s = max(0.0, _fnum('ORION_METER_TRACK_TMPL_MAX_S', 0.5))
        # (Two rejected template-reseed policies, both MEASURED on 195034 e14, a NEAR-STATIC
        # meter at rise onset: reseeding from the TRACKED position every strong frame let
        # the box random-walk 19-22px off the meter -- the moving fill edge lives INSIDE
        # the notch band below ~12% fill, so every-frame reseeds capture a transient and
        # compound their own match error; and reseed-only-on-box-motion could not tell real
        # motion from match error, so it walked identically. Detection-cadence refresh still
        # keeps statics stable, but it now happens AFTER the old template localizes the frame;
        # refreshing before the match created an exact self-match that copied detector lag and
        # placement jitter into the 60fps output -- see _det_track_step.)
        # DEVIATION BUDGET (measured on 195034 e14): even with motion-gated reseeding the
        # match itself ran off a NEAR-STATIC meter during the low-fill onset (the fill edge
        # transits the notch band below ~12% fill, so the anchored template mismatches the
        # meter exactly then, and similar-looking decor nearby out-correlates it: box err
        # grew to 22px at score 0.75-0.95 while the detector box stood still). The tracker
        # exists ONLY to cancel detector STALENESS, so its deviation from the detector's
        # (extrapolated) box is clamped to what staleness can explain:
        #     cap = DEV_BASE + speed * DEV_S      (speed from the detection history, px/s)
        # Static meter -> cap ~7.1px @1280 (runaway impossible; the fill read tolerates
        # ~10-12px, and a static meter's legit deviation is only detector noise ~2-3px);
        # measured fast pans (300-580 px/s) -> cap 67-123px (full lag correction allowed).
        # 4px base (was 6, was 8): the onset transient -- the fill edge transiting the notch
        # band below ~12% fill -- makes the between-seed match wander toward nearby decor on
        # near-static meters (e14 measured 13.5px, 003937 e21 7-10px), and any excursion
        # past ~10px zeroes the read. 4px keeps a full-cap excursion safely inside read
        # tolerance while still covering static detector noise.
        self._det_track_dev_base = max(0.0, _fnum('ORION_METER_TRACK_DEV_BASE_PX', 4.0))
        self._det_track_dev_s = max(0.0, _fnum('ORION_METER_TRACK_DEV_S', 0.20))
        # MATCH-INNOVATION GATE.  NCC needs a broad search window to follow a fast fade, but a
        # broad window also contains repeated court/jersey texture.  A high NCC score alone can
        # therefore jump to a duplicate patch for one frame; the detector snap notices only on
        # the NEXT frame, after the bad box has already reached fill measurement/overlay.
        #
        # Judge displacement against the tracker's one-frame velocity prediction, not against
        # the previous box.  At the measured fastest fade (~13px/frame), the default allowance
        # is 16 + .65*13 = 24.45px, almost twice the physical step and comfortably above camera
        # acceleration.  A 28px cap remains over 2x that measured step while staying below the
        # 30-60px search radius where duplicate-scene matches live.  Rejection falls back to the
        # detector-held box and rolls back the candidate's velocity update; it never kills a
        # lock or extends its lifetime.
        self._det_track_innov_base = max(
            4.0, _fnum('ORION_METER_TRACK_INNOV_BASE_PX', 16.0))
        self._det_track_innov_vel_gain = max(
            0.0, _fnum('ORION_METER_TRACK_INNOV_VEL_GAIN', 0.65))
        self._det_track_innov_max = max(
            self._det_track_innov_base,
            _fnum('ORION_METER_TRACK_INNOV_MAX_PX', 28.0))
        # Template-texture floor (grey std) below which the NCC match is treated as
        # unmatchable this frame (see _seed_track_template). Real locked-meter notch crops
        # measure std 40+; the hazardous onset/pre-fill crops sit far below.
        self._det_tmpl_std_min = max(0.0, _fnum('ORION_METER_TRACK_TMPL_STD_MIN', 7.0))
        self._det_tmpl_std = 0.0
        # Minimum detection-history span before ANY velocity estimate is trusted (see
        # _det_hist_vel: below this, detector placement jitter masquerades as motion).
        self._det_vel_min_span_s = max(0.0, _fnum('ORION_METER_TRACK_VEL_MIN_SPAN_S', 0.15))
        # Magnitude clamp on an UNTRUSTED (short-history) velocity estimate, px/s -- lets
        # the extrapolation apply a small bounded correction at a fast fade's onset while
        # a jitter-faked "velocity" on a static meter can shift the box only a few px.
        self._det_early_v_max = max(0.0, _fnum('ORION_METER_TRACK_EARLY_V_MAX', 100.0))
        # Absolute cap (px) on the extrapolation SHIFT while velocity is untrusted.
        self._det_early_shift_max = max(0.0, _fnum('ORION_METER_TRACK_EARLY_SHIFT_MAX', 8.0))
        self._det_track_box = None       # last TRACKED box (pre-emit-smoothing), per-frame ref
        # P2 experimental reader lane. Display damping must never determine the
        # measurement crop. Pixel geometry requires BOTH a local green cap and a
        # narrow neutral ribbon; brightness alone previously selected jerseys.
        self._det_measure_raw = _flag('ORION_METER_MEASUREMENT_BOX', '0')
        # [ORION_READER_LOCK_BOX_DIMS 2026-09-11] The fill ruler is the MEASUREMENT box's height
        # (denom = bh-1, the notch zone, the box-relative base hold). That box is the tracked box
        # after the emit EMA, and its dims drift away from the detector's: live 2026-09-11
        # (session 11:28, detframes.csv) the CV proposer served 107-110 px boxes while the reader
        # measured in 118-121 px boxes on 33 of 36 practice shots and 130-145 px x 30-44 px
        # boxes on the game shots -- a 9-30 % ruler change that moves every anchor and differs
        # per shot. With the landmark proposer the box IS the ruler (tip-to-notch is a constant
        # 100 px), so the measurement/display dims are pinned to the detector's while a fresh
        # detector box exists; the tracker keeps ownership of x/y (bottom-aligned). Applies to
        # the cv-contour proposer only unless forced; "0" disables.
        self._det_lock_dims = _flag('ORION_READER_LOCK_BOX_DIMS', '1')
        self._det_lock_dims_force = _flag('ORION_READER_LOCK_BOX_DIMS_FORCE', '0')
        self._det_lock_dims_max_age_s = max(0.05, _fnum('ORION_READER_LOCK_BOX_DIMS_MAX_AGE_S', 0.35))
        # [ORION_READER_DETECTOR_ONLY_SEAT] see _det_only_seat_veto. cv-contour only unless forced.
        self._det_only_seat = _flag('ORION_READER_DETECTOR_ONLY_SEAT', '1')
        self._det_only_seat_force = _flag('ORION_READER_DETECTOR_ONLY_SEAT_FORCE', '0')
        self._det_only_seat_max_age_s = max(0.02, _fnum('ORION_READER_DETECTOR_ONLY_SEAT_MAX_AGE_S', 0.12))
        self._det_pixel_ruler = _flag('ORION_METER_PIXEL_RULER', '0')
        self._det_kalman_search = _flag('ORION_METER_KALMAN_SEARCH', '0')
        self._det_pixel_geometry = None
        self._det_pixel_ruler_reject = False
        self._det_pixel_span_hist = _collections.deque(maxlen=128)
        self._det_pixel_ruler_epoch = 0
        self._det_kalman_state = None
        self._det_measurement_box = None
        self._det_display_box = None
        if self._det_measure_raw or self._det_pixel_ruler or self._det_kalman_search:
            _acq_logger.warning(
                "METER PIXEL TRACKING: measurement_box=%d pixel_ruler=%d "
                "kalman_search=%d prediction_authority=0",
                self._det_measure_raw, self._det_pixel_ruler, self._det_kalman_search)
        self._det_tmpl_ts = -1.0e9       # when the current template was seeded
        self._det_tmpl_pos = None        # (x, y) the current template was seeded at
        self._det_fresh_accept = False   # a fresh detector proposal was accepted THIS frame
        self._det_new_accept = False     # that proposal is a NEW source result, not latest() replay
        # Fractional box-height change that counts as a genuine meter RESCALE rather than
        # jitter (used by the sub-pixel per-shot latch below). 0.15 sits far above the
        # measured within-shot wobble and far below a real zoom step.
        self._fill_denom_relatch = max(0.02, _fnum('ORION_METER_FILL_DENOM_RELATCH', 0.15))
        # FILL-DENOMINATOR LATCH: REMOVED 2026-08-30 after a valid A/B refuted it.
        # (ORION_METER_FILL_DENOM_LATCH, default-off, latched the first confident box height
        # as the denominator per shot.) The A/B that killed it resolved shot runs ONCE and
        # replayed the SAME frames through every arm (offline recompute from identical
        # per-frame top/bbox measurables, so the arms cannot diverge -- the flaw that
        # invalidated the earlier attempt), 3 sessions / 22 evaluated rises:
        #   * the motivating "4.5-6.0px height wobble within a rise" does NOT reproduce:
        #     within the pre-commit fit window the box-height range is 1-2px median (live
        #     frames.csv AND replay); 4.5-6px matches whole-LOCK ranges, i.e. acquisition +
        #     post-peak frames the slope fit never sees.
        #   * the latch arm measured WORSE than the live denominator: sigma_y about a
        #     pre-commit line 0.97 vs 0.87 med (+12%), worse half-window slope drift, worse
        #     engine-replica crossing stability. Mechanism: fill=(D-top)/D with D latched
        #     re-anchors the numerator to the box TOP edge (y0 jitter, ~0.9px detrended sd,
        #     enters at full 0.93%/px weight), while live fill=(bh-1-top)/(bh-1) is
        #     bottom-anchored and takes height wobble only multiplicatively (~fill*d(bh)/bh,
        #     ~0.3%/px at commit fills).
        #   * the one part that helped -- a latched SCALE under a bottom/base-anchored
        #     numerator (sigma_y -2.5%) -- ships inside the sub-pixel path as _subpx_D.
        # Do not rebuild a top-anchored denominator latch without beating that A/B.
        # ---- SUB-PIXEL FILL EDGE (2026-08-30) ------------------------------------------- #
        # The coarse walk quantizes the fill edge to whole rows (1 row ~= 0.93% of scale on
        # the ~107px meter) and its per-frame noise is the sigma_y that dominates the tip-
        # crossing variance (sigma(t*) ~= (sigma_y/b)*sqrt(1/N + (t*-tbar)^2/S_tt); the
        # extrapolation term is ~98% of the observed ~12ms). This measures the edge
        # CONTINUOUSLY: collapse the bar's CORE columns (white below the edge -- the box also
        # contains outline/shadow columns, measured wfrac saturates at 0.50) to a 1-D LUMA
        # profile (V only: capture chroma is 4:2:0, i.e. subsampled along the axis being
        # localized -- the bt601/709 class of bug), estimate BOTH plateau levels (empty track
        # ~V138, fill core V255 measured), and take the 50% crossing with linear
        # interpolation. Synthetic ground-truth sweep (edge swept across sub-pixel phases,
        # measured noise/blur/JPEG): coarse RMS 0.288px (= textbook q/sqrt(12)), 50%-crossing
        # RMS 0.038-0.070px, phase-locking bias <= 0.08px; probit/erf fits measured equal
        # within noise, so the closed-form crossing ships. The BASE of the white run (the
        # meter's own bottom structure) is measured the same way and used as a per-frame
        # ANCHOR: the fill numerator becomes (base - edge), both sub-pixel image
        # measurements, so per-frame BOX-EDGE jitter (measured ~0.7-0.9px detrended sd,
        # corr +0.86..0.90 with the fill residual -- it, not luma noise, dominates sigma_y)
        # cancels out of the numerator entirely; the box only sets latched per-shot
        # constants. MEASURED on 24 replayed rises across 3 framedump sessions
        # (20260828_201813 / 20260830_003937 / 20260829_123758), runs resolved once and
        # both estimators computed on identical frames:
        #   * sigma_y about a line over the engine's commit band (fill 15-40):
        #     1.10 -> 0.54 %-of-scale median (-51%); whole pre-window 1.00 -> 0.73.
        #   * engine-replica (exp-weighted lambda=.5, 180ms window) crossing-prediction
        #     scatter: median -23%, p90 -37%.
        #   * run-to-run fitted-slope spread (IQR/med): 0.19 -> 0.12 (-35%).
        #   * SLOPE SHIFT, SAY IT LOUDLY: the fitted rise slope reads ~+7.7% HIGHER
        #     (paired median). Green-cap referee (the cap is fixed meter structure,
        #     recovered box-independently) shows why: the DETECTOR BOX slides -9.5px per
        #     100pp of fill against the meter during a rise (IQR -16.6..-4.1), so the
        #     box-anchored coarse fill UNDERSTATES the true rate by ~9% with per-shot
        #     variance; the base anchor holds at +1.1px/100pp. The shift is bias REMOVAL,
        #     not scale drift -- but the learned rate constant (0.196 %/ms, learned
        #     through the coarse instrument) will re-learn ~7% higher once this is live.
        #   * parity at matched positions: +0.0pp at rise start; grows to ~+1.4pp
        #     mid-run, which is the coarse reading's own box-slide error accumulating,
        #     not a scale change in this estimator. Per-shot constant offset sd ~1.0pp
        #     (off/D latch seed noise) -- the cost paid for a jitter-free anchor.
        # Fill emission switches to the sub-pixel value; the coarse value still ships in
        # fill_coarse/raw_fill_pct as a permanent per-frame A/B breadcrumb, and the
        # false-lock breakers keep judging the coarse value (see the production boundary).
        self._subpx_fill = _flag('ORION_METER_SUBPIXEL_FILL', '1')
        # Parity offset (rows) aligning the sub-pixel 50%-crossing to the coarse walk's
        # first-white-row convention, so the FILL SCALE the engine's constants were tuned on
        # (rate 0.196 %/ms, green tip ~96%) does not move. Measured on replayed real rises:
        # median(coarse_top - y50) = 0.67-0.71 rows (n=335 clean frames, 3 sessions); with
        # 0.71 the matched-frame parity at rise start is +0.0pp. A wrong value here shifts
        # fill by a constant, it never changes the slope.
        self._subpx_bias = _fnum('ORION_METER_SUBPIXEL_BIAS_ROWS', 0.71)
        # Per-lock latched scale/anchor constants (seeded from the median of the first 3
        # frames where both edges measured cleanly -- the box is least accurate at
        # acquisition, so a first-frame latch freezes a bad height; med3 measured tighter):
        #   _subpx_D   = latched fill denominator (px)
        #   _subpx_off = latched (box_bottom-1 - base_abs) alignment so the base-anchored
        #                numerator stays on the box-relative scale the engine knows.
        self._subpx_D = None; self._subpx_provisional = False
        self._subpx_off = None
        self._subpx_seed = []
        self._subpx_base_hist = []; self._subpx_last_base_rel = None
        # ---- SESSION RULER (2026-09-02, ORION_METER_SUBPIXEL_SESSION_RULER) ------------- #
        # The 3-frame per-shot seed above is the ruler's weak point: it is taken at
        # acquisition (fill 5-30%, the box still settling, the shooter often moving on a
        # fade / off-the-dribble), so (D, off) carry a per-SHOT error the rest of the shot
        # inherits as a constant. MEASURED on session_20260901_190427 (47 shots, right corner
        # -> left corner -> off-dribble -> fades, 733 dumped frames, true edges measured
        # from pixels): the reader's ruler moved 2.2pp rMAD between shots and its read of
        # the GREEN CAP -- fixed meter structure that must read the same every shot --
        # wandered 93-99 (rMAD 2.15, corr with screen x -0.44) while a ruler built on the
        # served box read the same cap at rMAD 0.81, corr -0.10. On the same frames the
        # 20% phase anchor was dated up to 19 ms apart by the two rulers. 1pp of ruler
        # error is ~5 ms of anchor and 1pp of landing, the size of the residual earlies /
        # lates. The meter's on-screen SCALE does not change between shots of one session
        # (same resolution, same detector), so the ruler should not either: keep every
        # shot's seed and emit the MEDIAN over the last N seeds once M have accumulated.
        # The numerator (base - edge, both image measurements) is untouched, so per-frame
        # box jitter still cancels; only the per-shot constant stops moving. Units are the
        # same box-relative scale (the median of today's seeds), so the engine's aim, the
        # learned rate and the 0.71-row parity are unchanged. A genuine rescale (>15%
        # height jump, the existing relatch) or a seed that disagrees with the session
        # median by the same margin clears the history; every per-shot / per-lock reset
        # keeps it. Fail-open: fewer than M seeds -> today's per-shot latch, byte-identical.
        self._subpx_session_ruler = _flag('ORION_METER_SUBPIXEL_SESSION_RULER', '0')
        self._subpx_session_window = max(3, int(_fnum('ORION_METER_SUBPIXEL_SESSION_WINDOW', 15)))
        self._subpx_session_min = max(2, min(self._subpx_session_window,
                                             int(_fnum('ORION_METER_SUBPIXEL_SESSION_MIN', 3))))
        self._subpx_ruler_hist = []       # [(D, off)] one entry per completed per-shot seed
        self._subpx_ruler_kind = ""       # "shot" | "session" for the frame's _dbg_subpx
        # ---- PROVISIONAL SESSION RULER AT LOCK START (2026-09-02, fleet reader lane) ------ #
        # The session median above only takes over once a lock's OWN 3-frame seed completes;
        # replaying today's four dumps showed 92% of shots date their 20% anchor crossing
        # BEFORE that, on the seeding path (anchor = this frame's box bottom, the per-shot /
        # per-jitter scale). Paired at the same edge, that path differs from the session ruler
        # by 0.46-0.63 pp rMAD per shot = 2.5-3.5 ms of anchor jitter that the session ruler
        # was built to remove and never touched. With this flag a new lock adopts the session
        # median from its FIRST measured frame (validated against the frame's box height
        # within the carry jitter, so a genuine rescale still cold-seeds), keeps collecting its
        # own seed, and re-adopts the updated median when the seed completes. Byte-identical
        # while the history holds fewer than the minimum seeds.
        self._subpx_session_provisional = _flag('ORION_METER_SUBPIXEL_SESSION_PROVISIONAL', '0')
        # [ORION_METER_SUBPIXEL_RULER_LOCK 2026-09-03] a lock that started on the provisional
        # session ruler keeps it for the whole shot; its own seed joins the history for the NEXT
        # shot. Live e71: the seed completing mid-shot re-emitted the ruler twice (seed, then the
        # new median), the engine saw a +7.3 pp step 19 ms before a scheduled fire and killed it.
        self._subpx_ruler_lock = _flag('ORION_METER_SUBPIXEL_RULER_LOCK', '1')
        # [ORION_METER_SUBPIXEL_NOTCH 2026-09-08] anchor the sub-pixel fill on the meter's own
        # chevron-notch apex (2 centre columns, whiteness) instead of the core-mean base, and
        # measure the fill edge on whiteness min(B,G,R) instead of V. Measured on 3 framedump
        # sessions (2519 frames): anchor sd 0.4-0.5 px -> 0.065 px; V-only edge loses the
        # red-court frames (26 counts of contrast), whiteness keeps them (210).
        self._subpx_notch = _flag('ORION_METER_SUBPIXEL_NOTCH', '1')
        self._subpx_provisional = False   # this lock is on the provisional session ruler
        # ---- BOX-RELATIVE BASE HOLD (2026-09-02, ORION_METER_SUBPIXEL_BASE_HOLD) -------- #
        # When the base falloff is not measurable on a frame (occluder, dark shelf, plateau)
        # and the velocity hold above has aged out (<= 120 ms), the ruler used to drop to the
        # BOX anchor for that frame: a different ruler family, so native saw an estimator
        # generation step mid-shot. Live 2026-09-02 13:00: 7 of 35 shots stepped 2-3 pp after
        # the arm (tolerated since the fence change, but the landing read and the post-arm
        # refinement still jump). The detector box tracks the meter, so the best estimate of
        # an unmeasurable base is the box bottom plus this shot's LAST MEASURED base-to-box
        # offset -- the same ruler family, no step. Bounded by age; a new lock clears it.
        self._subpx_base_hold = _flag('ORION_METER_SUBPIXEL_BASE_HOLD', '0')
        self._subpx_base_hold_max_s = min(2.0, max(0.1, _fnum('ORION_METER_SUBPIXEL_BASE_HOLD_MAX_S', 0.8)))
        self._subpx_last_base_rel = None  # last measured (base_row - (bh-1)) of this lock
        self._subpx_last_base_ts = -1.0e9
        # A controller press does not, by itself, move or resize a bridged meter lock.
        # Keep a validated base-anchored ruler across that boundary, then validate its
        # scale against the first box of the new shot before it is allowed to emit.
        # This avoids paying the three-frame latch delay on every rapid/fade shot while
        # still dropping the carry immediately when camera distance really changed.
        self._subpx_carry_pending = False
        self._subpx_carry_jitter_px = max(
            0.5, _fnum('ORION_METER_SUBPIXEL_CARRY_JITTER_PX', 2.0))
        self._subpx_bridge_max_s = min(
            0.20, max(0.02, _fnum('ORION_METER_SUBPIXEL_BRIDGE_MAX_S', 0.12)))
        self._dbg_subpx = None
        # [ORION_GREEN_SCALE_UNIFY] per-frame (anchor, D) of the last successful
        # sub-pixel fill measure; pairs the green band onto the emitted fill's ruler.
        self._last_subpx_transform = None
        self._last_fill_coarse = 0.0
        # Process-monotonic identity for the exact ruler that emitted fill_pct.
        # Native phase timing may interpolate only between samples carrying the
        # same non-zero generation.  The identity key changes on coarse/sub-pixel
        # fallback, sub-pixel latch/re-latch, or a base-vs-box anchor change.
        self._fill_estimator_generation = 0
        self._fill_estimator_identity = None
        self._last_fill_estimator_mode = ""
        self._last_fill_estimator_generation = 0
        # The coarse row walk divides by (detector-box height - 1).  Treating every
        # height as the same "row_walk" ruler allowed native to interpolate an
        # anchor across a genuine detector rescale.  Do not key directly on every
        # height either: latest live telemetry measured all near-adjacent det_h
        # steps within two pixels, so doing that would churn provenance on ordinary
        # tracking quantization and suppress otherwise valid crossings.  Latch one
        # identity reference and start a new local ruler epoch only outside that
        # measured +/-2 px band.  This labels provenance only; it does not alter the
        # coarse numerical measurement or its previously-refuted top-edge latch.
        self._coarse_denom_ref = None
        self._coarse_denom_epoch = 0
        self._coarse_denom_jitter_px = 2.0
        # ---- RUNG-TOLERANT FILL (2026-08-30, the Pill flat-0.0 fix) -------------------- #
        # The 2K27 Pill (park/rec) capsule is a LADDER: the fill is a stack of bright
        # segments separated by 1-3-row dark divider lines, and the glossy fill core is
        # only ~6 of the ~30 detector-box columns @1080p (~4 of ~20 @720p). Measured on
        # the seven 1080p owner clips: per-row wfrac tops out at 0.20-0.26 (0.14-0.20
        # @720p), so the coarse walk's 0.28 floor NEVER passes and the reader emitted
        # fill 0.00 on 190/192 ground-truth frames while the true fill rose 4->95pp.
        # (The empty track also carries 1-row bright rung TICKS every ~12.7 rows @1080p
        # -- 0.23 wfrac, brighter than some fill rows -- which alias away at 720p.)
        # _rung_fill_block measures the fill as the base-touching stack of bright rows
        # over the meter's own narrow CORE column band, bridging divider-sized gaps.
        # It is geometry-gated, not style-gated (narrow flank-dark band, base contact,
        # measured gap structure), so a wrong/changed style setting cannot break it:
        # censused over 6064 detected Arrow2 framedump frames it changes 2 (0.03%),
        # both mid-onset zero-fill dropout frames whose neighbours read 9.43 live and
        # which it reads at 12.3 -- i.e. it never touches a frame the solid-ribbon walk
        # already reads. ORION_METER_RUNG_FILL=0 restores the shipped walk byte-for-byte.
        self._rung_fill = _flag('ORION_METER_RUNG_FILL', '1')
        self._dbg_rung = None
        self._det_extrap = _flag('ORION_METER_DETECTOR_EXTRAP', '1')
        # Never extrapolate further than this (a long gap means the box is unknown, not fast).
        # 0.14 -> 0.22 (2026-08-30): measured at bench pacing the consumed result's age runs
        # 50-185ms between accepted detections mid-shot, and the frames whose age fell past
        # the old cap served the RAW stale box -- on a fade pan that is 16-22px behind the
        # meter, which is past the fill read's ~10-12px tolerance and was one leg of the
        # Left_Fade zero-fill dropout. 0.22 covers the observed refresh jitter while the
        # 0.20 TTL and the 0.35s hold still bound a genuinely dead result.
        self._det_extrap_max_s = max(0.0, _fnum('ORION_METER_DETECTOR_EXTRAP_MAX_S', 0.22))
        # A result older than TTL (meter moves with the shooter) is ignored -> fall back
        # to the colour reader rather than seat a stale box.
        self._det_ttl_s = max(0.0, _fnum('ORION_METER_DETECTOR_TTL_S', 0.20))
        # Already tracking a box this close to the detector's -> let relocate keep it;
        # only re-seat when the colour lock has drifted off the detector's meter.
        self._det_seat_iou = min(1.0, max(0.0, _fnum('ORION_METER_DETECTOR_SEAT_IOU', 0.30)))
        # PRIORITY-ON-ACQUIRE (legacy knob name: SYNC_ACQUIRE): while the reader holds NO detector
        # box, ask the async locator to process the freshest frame without its ordinary breathing
        # gap.  This used to call ORT synchronously on the capture callback.  A live real-game run
        # measured 44-70ms per call, 80-170ms callback gaps and source/presentation collapse from
        # 60fps into the 30s when an arm remained active or read-rescue retriggered.  Priority keeps
        # the early/current-frame scheduling intent while inference stays exclusively on the worker.
        #
        # Historical acquisition rationale: while the reader holds NO detector box (acquisition), the
        # async single-worker pipeline delivers the FIRST box ~4-5 frames late -- the worker is
        # almost always mid-inference (30ms > 16.7ms/frame) when the meter pops in, so the onset
        # frame waits ~2 frames to be picked up, ~2 frames to infer, +1 read round-trip. Measured on
        # session_20260828_201813 the meter fills ~5-6%/frame at onset, so those 4-5 frames put the
        # first REPORTED fill at ~25% (SYNC replay: ~5%). When enabled, the acquire path runs one
        # high-priority detection request on the current frame so the worker grabs the newest pixels
        # immediately.  The moment a meter is held we return to ordinary async cadence + box-hold.
        # No-meter veto and lifecycle policy are unchanged; only where expensive inference executes
        # changed.
        #
        # MODE (ORION_METER_DETECTOR_SYNC_ACQUIRE): '0' off (default); '1' ARMED-gated -- priority only
        # while the reader is physically shot-armed (_shot_armed_hw), i.e. the bounded ~250ms between
        # the shot-button press and the meter appearing, so normal no-meter gameplay stays fully async
        # (no added GIL load / preview stutter, and it degrades to the async baseline if the arm
        # signal never arrives -- never worse); '2'/'always' unconditional on every acquisition frame
        # (for rigs with no reliable arm signal, or offline A/B -- costs continuous background
        # acquisition inference, but never blocks the timing/capture callback).
        # Measured on session_20260828_201813 (mode 'always', acq=33ms): first-fill 26% -> ~10.5%.
        _sa = _os.environ.get('ORION_METER_DETECTOR_SYNC_ACQUIRE', '0').strip().lower()
        self._det_sync_acquire = _sa in ('1', '2', 'true', 'yes', 'armed', 'always')
        self._det_sync_acq_always = _sa in ('2', 'always')
        self._det_acq_interval_s = max(0.0, _fnum('ORION_METER_DETECTOR_ACQ_INTERVAL_MS', 33.0) / 1000.0)
        self._det_last_sync_acq = -1.0e9
        # One priority wake at each physical epoch plus one for each new PENDING
        # candidate is enough in the ordinary full-frame path. Repeating it every
        # 33ms while a hardware arm was stuck removed the worker's GIL breathing gap
        # indefinitely. The optional three-view phased scan still needs periodic
        # opportunities, but at a bounded 100ms default rather than per-inference.
        self._det_priority_epoch = None
        self._det_priority_pending_ts = -1.0e9
        # [ORION_METER_DETECTOR_ARMED_HOT 2026-09-08] while the shot button is physically held
        # (the meter appears 340-630 ms later and fills for ~600 ms) every frame goes to the
        # worker with the priority wake, so inference runs back to back on the freshest frame
        # instead of at the 15 ms-gap cadence: measured onset->first read 105 ms median, ~64 ms
        # of it cadence. Bounded per arm epoch so a stuck hardware arm cannot pin the worker.
        self._det_armed_hot = _flag('ORION_METER_DETECTOR_ARMED_HOT', '1')
        self._det_armed_hot_max_s = max(0.0, _fnum('ORION_METER_DETECTOR_ARMED_HOT_MAX_MS', 1200.0) / 1000.0)
        self._det_hot_epoch = None
        self._det_hot_open_ts = -1.0e9
        self._det_phased_priority_s = max(
            self._det_acq_interval_s,
            _fnum('ORION_METER_DETECTOR_PHASED_PRIORITY_MS', 100.0) / 1000.0)
        # PHASED EDGE ACQUIRE (default OFF; local batch opt-in). Full-frame 960px
        # inference reduces a 23px-wide 720p meter to ~17 model pixels. On the hard
        # human-audited holdout, full + two overlapping 55%-width edge views recovered
        # 11 additional meters (436/460 -> 447/460) with 0 new finds across 727
        # held-out no-meter frames. Sequentially stacking all three costs ~77ms and
        # starves capture, so acquisition rotates ONE view per sync opportunity:
        # last proven side, full, opposite (or full/left/right without history).
        # Confidence, full-frame geometry, lifecycle and ownership gates are unchanged.
        self._det_phased_acquire = _flag(
            'ORION_METER_DETECTOR_PHASED_ACQUIRE', '0')
        self._det_acq_scan_epoch = None
        self._det_acq_scan_index = 0
        self._det_acq_last_side = None       # left/right only after a proven meter rise
        self._det_acq_scan_last = 'full'
        # READ-RESCUE (2026-08-30, the Standstill/pan zero-fill dropout fix). MEASURED on
        # session_20260830_112306 (live, 37 Arrow2 Standstill runs, per-frame census against
        # synchronous YOLO ground truth on the SAME frames): 131 mid-rise frames reported
        # detected=1 with fill 0.0, and on 113 of them (86%) the SERVED box sat 12-44px off
        # the meter (|dcx| med 16, p90 29) during a camera pan burst/reversal while the
        # same-frame YOLO found the meter at conf .90-.96 and the fill read 92-94% inside
        # its box. Every box-serving mechanism is bounded BELOW that error by design (the
        # extrapolation shift cap, the tracker's deviation budget, the emit slew -- each
        # tuned against static-meter walk-offs), and the async result itself is 60-100ms
        # stale, so during a pan burst no served box can stay on the meter. The rescue:
        # when the held box cannot measure ANY fill run (top_row < 0), enqueue THIS frame
        # as a priority/latest-worker request. The trigger frame stays an honest zero; the
        # next unique full-frame result passes the ordinary lifecycle/jump/no-meter gates
        # and, if accepted, hard-reseats emit/tracker state on the following callback.
        # One request remains in flight until result/TTL, and RESCUE_GAP_MS bounds later
        # retries. No preprocessing or inference ever runs on the timing/capture thread.
        self._read_rescue = _flag('ORION_METER_READ_RESCUE', '1')
        self._rescue_gap_s = max(0.0, _fnum('ORION_METER_RESCUE_GAP_MS', 35.0) / 1000.0)
        self._det_last_rescue = -1.0e9
        # Source timestamp of a non-blocking read-rescue request awaiting one fresh
        # full-frame locator result.  A qualifying result earns the same lifecycle-gated
        # hard reseat the old inline call earned, on a later callback; it expires at TTL.
        self._det_rescue_request_ts = -1.0e9
        # DETECTOR-FILL STRUCTURE LATCH (2026-08-30, the live silent-shot fix).
        # THE MEASURED LIVE CHAIN (session_20260830_112306 + orion_native.log): the
        # sidecar config said meter_color=Red against 2K27's WHITE meter (the exact
        # settings drift _measure_fill_in_box exists for). read()'s colour path masks
        # the CONFIGURED colour, so its qualify-time sample almost never carries red
        # evidence -- and _qualify_gameplay_sample (which runs on READ's sample,
        # BEFORE the colour-agnostic detector-fill override) therefore never latches
        # the structure proof. The emitted detector-fill samples still flowed
        # (fills 6->47 reached the engine), so the engine saw genuine rising frames
        # with NO stamp: 18 presses aborted ownership_structure_stamp_missing with
        # stamp_epoch_seen=0. A faithful offline replay (same frames, real press
        # times/epochs, live-like detector staleness) with the colour CORRECT
        # latches every one of those epochs, which acquits the fill dropouts and
        # convicts the read-side evidence starvation.
        #
        # THE FIX: the authoritative, colour-agnostic detector-fill sample may latch
        # the proof ITSELF -- but only on the meter's own identity, the green
        # make-window cap ("nothing in the scene fakes the chevron"), measured
        # >=8 green px in the box (the same floor the capless breaker measured:
        # 91% of true locks carry it, 95% of false locks do not) on 2 CONSECUTIVE
        # detector-fill frames of a LOCKED lifecycle box, inside the current armed
        # hw epoch. No authorization or publication state is touched; a no-green
        # (contested/Go-To) rise still needs the existing read-side rise proof.
        self._detfill_green_latch = _flag('ORION_METER_DETFILL_GREEN_LATCH', '1')
        self._df_green_streak = 0
        # [ORION_READER_GHOST_PRESS_BREAK] LEFTOVER-METER (GHOST) EVICTION AT/AFTER A PRESS
        # (2026-08-30, the stale-meter-at-press category).
        #
        # THE MEASURED MECHANISM (session_20260830_112306 + orion_native.log, epochs 4/21/23/
        # 33/34/35/44/45; session_20260830_113732 epochs 12/13; session_20260828_195034 epochs
        # 5/20/23): 2K27 leaves the PREVIOUS rep's meter rendered for seconds between shots --
        # frozen at ~43-55% after an early/aborted release, at ~88-95% after a full release
        # (frame crops verified: a real meter image, green cap and all, drifting with the
        # camera pan). The reader idles LOCKED on it, and the lock BRIDGES the next press:
        # the engine's first genuine samples are a static ~46%, which its ownership anchor
        # rightly refuses (anchorMaxFirstFillPct=40 -- a shot first seen high was never seen
        # starting). The bridge costs the press twice over: the ghost's frames are stamped and
        # stale-censused, and the lock is BUSY when the real meter renders ~450-650ms after
        # the press, so the true onset is acquired 1-3 frames late -- measured proof-completion
        # ~40-80ms later than clean presses (fill ~25-30 vs ~15-20 at proof; the fire deadline
        # at the shipped lead sits at ~35).
        #
        # Two defenses, both PRESS-SCOPED (they exist only between a physical press and the
        # first low-fill sighting of that press's own meter, bounded by _ghost_press_window_s):
        #   (1) notify_physical_shot_start's stale-lock drop now judges the RECENT nonzero
        #       fill (max of last_fill and the detector-fill history) -- the ghost's fill
        #       read flickers to 0.0 on pan-blurred frames, and the old instantaneous test
        #       let exactly those presses bridge (epochs 41/45: held ghost read 0.00 at the
        #       press instant, 88.9/45.1 one frame later) -- and it now drops the DETECTOR
        #       lifecycle lock too (state/warm/history), which the original drop predates:
        #       clearing self.box alone is undone one frame later when the held detector box
        #       re-seats it.
        #   (2) a post-press breaker: while this press has never yet seen its own meter low
        #       (no nonzero fill <= _ghost_press_low_pct), a lock that reads >= the stale
        #       floor (_stale_press_drop_pct, 40) on _ghost_press_frames nonzero frames whose
        #       spread stays inside _ghost_press_spread_pp is the leftover meter, never this
        #       shot's: a real rise crosses that band at ~6pp/frame and cannot hold the
        #       spread, and a real rise that STARTED below the floor has already disarmed the
        #       guard via the low sighting. Zero reads (blur/fade) neither feed nor reset the
        #       run, so the flicker cannot shield the ghost.
        #
        # FAIL-CLOSED: both paths only ever DROP a lock the engine could never own (first
        # sight above the anchor bound) inside the press window; neither invents evidence,
        # touches the structure latch, nor fires outside a physically-armed epoch. The
        # settled-meter plateau that follows OUR OWN release (grading needs it) is protected
        # by the low-sighting disarm: a graded shot's rise passed through low fills first.
        # THE QUARANTINE ZONE (measured necessity, replay A/B 2026-08-30): plain eviction is
        # NOT enough. Each evict->relock cycle reseeds the per-lock sub-pixel calibration, and
        # the fresh lock's first reads on the SAME ghost land noisy -- observed 35.0/40.0
        # against a stable 46.5 -- which crosses the engine's 40% first-sight bound and OPENED
        # false ownership episodes on the static ghost in the offline replay (epochs 23/24/38:
        # proof at +128..235ms on a leftover meter, before the real meter could even render).
        # So an identified ghost's POSITION is quarantined for the rest of the guard: in-zone
        # reads are suppressed (never published) and the lock is re-evicted, with one
        # exemption -- two CONSECUTIVE in-zone nonzero reads at/below _ghost_zone_low_pct are
        # a real meter rendering inside the zone (its onset reads 0-20; a single blurred
        # partial read of the ghost cannot make two in a row), which publishes and stands the
        # guard down. The zone FOLLOWS the ghost (center updates on every suppressed read) so
        # a camera pan cannot walk it out of its own quarantine.
        # [ORION_READER_BOX_WIDTH_GATE 2026-09-03] A detector box far wider than this session's
        # accepted meter boxes is not the meter (live e50: 39-41 px against 25-28 px re-read the
        # previous shot's frozen bar as a rising 33/39.6/46.2 and armed an early). Keep the last
        # accepted widths; once >= min_n exist, a box wider than ratio x median (or narrower than
        # 1/ratio) gets NO fill read this frame (0.0, no top row). Style-agnostic: the median is
        # whatever this session's detector has been accepting.
        self._box_w_gate = _flag('ORION_READER_BOX_WIDTH_GATE', '1')
        # [ORION_READER_RESEAT_X 2026-09-08] a held box that reads 0.0 is re-seated on the
        # white ribbon found on THIS frame within +-18 px (the served box is the worker's
        # 30-50 ms-old proposal; during a fade pan it sits 7-34 px beside the meter and the
        # row walk hard-zeroes past ~12 px: 26/329 shots lost 50-180 ms of the rise that way).
        self._reseat_x = _flag('ORION_READER_RESEAT_X', '1')
        self._reseat_x_max = max(4, _inum('ORION_READER_RESEAT_X_MAX_PX', 18))
        self._reseat_x_n = 0
        self._box_w_ratio = max(1.1, _fnum('ORION_READER_BOX_WIDTH_RATIO', 1.35))
        self._box_w_min_n = max(3, _inum('ORION_READER_BOX_WIDTH_MIN_N', 8))
        self._box_w_hist = deque(maxlen=40)
        self._box_w_gate_hits = 0
        self._box_w_consec = 0
        self._box_w_retire_n = max(1, _inum('ORION_READER_BOX_WIDTH_RETIRE_N', 3))
        self._box_w_retired = 0
        # No consecutive-hit "reset": the ratio key is already scale-invariant, and clearing the
        # history after N hits would be a deterministic fail-open for a persistent false geometry
        # (Sol, review #59). Baseline invalidation, if ever needed, must come from an explicit
        # source/style/session lifecycle change, never from persistence of the anomaly itself.
        self._box_w_log_ts = -1e9
        self._ghost_press_break = _flag('ORION_READER_GHOST_PRESS_BREAK', '1')
        self._ghost_press_window_s = max(0.0, _fnum('ORION_READER_GHOST_PRESS_WINDOW_S', 1.6))
        self._ghost_press_low_pct = max(0.0, _fnum('ORION_READER_GHOST_PRESS_LOW_PCT', 35.0))
        self._ghost_press_frames = max(2, _inum('ORION_READER_GHOST_PRESS_FRAMES', 3))
        self._ghost_press_spread_pp = max(0.0, _fnum('ORION_READER_GHOST_PRESS_SPREAD_PP', 2.5))
        self._ghost_zone_low_pct = max(0.0, _fnum('ORION_READER_GHOST_ZONE_LOW_PCT', 25.0))
        self._ghost_zone_radius_scale = max(0.5, _fnum('ORION_READER_GHOST_ZONE_RADIUS_SCALE', 1.8))
        # LEVEL-AWARE escape (measured on session_20260830_113732 epoch 18): the zone
        # remembers the ghost's own fill LEVEL, and only reads within _ghost_zone_band_pp
        # below it (or above) are the ghost; a read far UNDER the level is a real meter
        # rendering inside the zone. A fixed low threshold was tried first and lost a real
        # shot: a ~93% leftover bridged the press, the real onset rendered in-zone reading
        # 25.5/31.1 -- above the fixed 25 exemption, far below 93 -- and was quarantined
        # until the guard window expired. Two CONSECUTIVE sub-band nonzero reads stand the
        # guard down (one blurred partial read of the ghost cannot make two in a row);
        # suppressed ghost reads reset the pair, zero reads leave it alone.
        self._ghost_zone_band_pp = max(0.0, _fnum('ORION_READER_GHOST_ZONE_BAND_PP', 15.0))
        # ZERO-READ HOLD (2026-08-30 session_20260830_191051): an in-zone lock reading 0.0
        # used to be evicted instantly ("a held zero keeps the lock busy"), but 51 of the
        # session's 336 evictions carried fill=0.0 and the late cluster (press+0.44-0.63s)
        # sat 30-80px from the ghost -- exactly where that press's REAL meter renders its
        # first EMPTY frame. Each such eviction cost a cold re-acquire plus a reseeded
        # sub-pixel calibration at the very moment the engine anchors its first fills.
        # So a zero read now SUPPRESSES the publish but keeps the lock; only
        # _ghost_zero_evict_n CONSECUTIVE zero reads evict (a fading/blurred leftover keeps
        # reading zero and still dies, at most ~2 frames later than before; an empty real
        # meter shows a nonzero fill within a frame or two and is then classified by the
        # level band). Set to 1 to restore the instant-evict behaviour.
        self._ghost_zero_evict_n = max(1, _inum('ORION_READER_GHOST_ZERO_EVICT_N', 3))
        # GHOST IDENTITY RETIREMENT (2026-09-01): a quarantine belongs to one rendered
        # leftover meter, not to a permanent court location. Two consecutive UNIQUE,
        # fresh, FULL-frame locator verdicts with no meter prove that identity ended. The
        # next meter may legitimately spawn in the same player-relative zone and must be
        # judged as a new low/rising onset. Partial left/right scans never count as absence.
        self._ghost_full_absence_results = max(
            2, _inum('ORION_READER_GHOST_FULL_ABSENCE_RESULTS', 2))
        self._press_low_seen = True     # no press yet -> guard idle until the first epoch
        self._press_ghost_reads = _collections.deque(maxlen=max(
            2, _inum('ORION_READER_GHOST_PRESS_FRAMES', 3)))
        self._press_ghost_zone = None   # (cx, cy) of the quarantined leftover meter
        self._press_ghost_level = None  # the leftover meter's own fill level (EMA)
        self._press_ghost_zone_low_n = 0
        self._press_ghost_zero_n = 0    # consecutive in-zone zero reads (zero-read hold)
        self._press_ghost_full_nofind_n = 0
        # EVICTION LOG IDEMPOTENCE (2026-08-30): the per-frame suppress+evict cycle is the
        # mechanism (it frees the single locker so the real onset can be acquired the frame
        # it renders), but logging EVERY cycle at ERROR spammed 336 lines / 62 presses
        # through the throttled native relay (5.4 per press; epochs 29/30/56/61 logged every
        # 15-20ms). Log the FIRST eviction of a press in full, again only when the zone
        # centre moves >64px (a different object) or 0.5s passes, and emit ONE per-press
        # summary carrying the totals when the guard stands down (or at the next press).
        self._press_ghost_log_ts = -1.0e9
        self._press_ghost_log_zone = None
        self._press_ghost_evict_total = 0     # lifecycle resets this press
        self._press_ghost_suppress_total = 0  # zero-read holds this press
        self._press_ghost_summary_epoch = 0   # epoch the totals belong to
        # ------------------------------------------------------------------ 2026-09-15
        # [ORION_READER_GHOST_FORGET_LOCATOR] THE RE-SEED LOOP (live 2026-09-15 22:37Z, rapid
        # drill, one press every 2.5-3s; epochs 9-15 all graded LATE). Measured: every one of
        # those presses dropped a leftover at 83-94%% (STALE LOCK DROPPED AT PRESS), evicted it
        # again post-press, retired its identity after full absence -- and then did it 2-11 MORE
        # times in the same press window (GHOST PRESS SUMMARY evictions=2..11), with DETECTOR
        # HEALTH showing locks +5/+7/+8/+12/+10 between 2s samples while drops stayed flat at
        # 26-27 and seeded climbed in lockstep (23->65). Locks WITHOUT drops are eviction
        # re-locks: the evict path resets the lifecycle to idle directly (no _det_drop_lock, so
        # no drop is counted) and the very next proposal re-locks through _det_on_found's
        # strong-and-armed shortcut. WHY the proposal keeps being the ghost: meter_locator_cv
        # keeps its OWN positional memory (_last_box/_last_ts) and three of its acceptance
        # paths -- the roaming ROI, the gate-9 bridge, and the tip-corroborate / outline-support
        # escapes -- accept a candidate with NO confirmed green tip or NO outline support purely
        # because something was accepted at that spot moments ago. The reader's eviction and its
        # ghost retirement clear every reader-side seed (_det_warm_pos, _det_active_box, box)
        # but never touched that locator memory, so the retired ghost seeded its own successor.
        # Each cycle also re-derives the per-lock ruler (_det_reset_lock_state clears _subpx_D,
        # _coarse_denom_ref, _det_scale_reference), which is the project's "fill denominator is
        # the detector box -- latch it" invariant being thrown away 5-11 times per press.
        # FIX: whenever the reader drops/evicts/retires a leftover, it also tells the locator to
        # forget WHERE it last saw a meter. A real meter loses at most one confirmation frame
        # (it still has its own tip and outline); the ghost loses its free pass.
        self._ghost_forget_locator = _flag('ORION_READER_GHOST_FORGET_LOCATOR', '1')
        self._loc_forget_n = 0
        # ------------------------------------------------------------------ 2026-09-16
        # [ORION_READER_FORGET_RATE_LIMIT] ...AND THE FIX ABOVE, UNRATED, CAUSED THE BLIND RUN.
        #
        # Live 2026-09-16 14:20:46-14:21:11, epochs 22-27: SIX consecutive presses with no
        # vision sample at all (all six backstopped blind). DETECTOR HEALTH over the window:
        # `loc_forget` 0 -> 148 climbing in lockstep with `locks` while `drops` stayed flat
        # (39 locks / 42 seeds / 39 forgets in 4 s) -- i.e. ~10 evictions per SECOND while the
        # previous shot's meter sat on screen through a rapid-fire re-press, against 0.76
        # evictions per press offline on the 8.8 fps framedump (which is exactly why the
        # replay could not reproduce it).
        #
        # Each of those evictions called forget_position(), and the first version of that
        # method wiped the locator's TWO-FRAME PROMOTION PAIRS along with the ghost's box:
        # `_pending` (the outline-less first sight), `_expect` (the sub-floor first sight the
        # relaxation trio needs to accept a 12-14% onset) and `_tipless` (the tip-less rise
        # pair). A pair needs TWO CONSECUTIVE FRAMES to promote; at 10 wipes a second the real
        # meter's pair never survived to its second frame, so its first publication landed
        # 100-170 ms late (BOX LATCHED 810-912 ms after the press vs the 750 ms deadline).
        #
        # TWO bounds, both of them about the same thing -- a forget must cost the ghost, never
        # the shot:
        #   (a) ONE forget per press epoch per ZONE. The second eviction of the same ghost in
        #       the same press is a no-op: the locator already forgot that box, and calling
        #       again can only destroy memory formed SINCE, which by definition is not the
        #       ghost's.
        #   (b) NEVER while a first-sight pair is still inside its promotion window. The pair
        #       is one frame from deciding; let it decide. If the ghost is still there next
        #       frame the eviction fires again and the forget happens then (bounded by
        #       ORION_READER_FORGET_DEFER_MAX frames so a pathologically self-renewing pair
        #       can never hold the forget off for good).
        self._forget_rate_limit = _flag('ORION_READER_FORGET_RATE_LIMIT', '1')
        # 64 px: the same "a different object" radius the ghost eviction log already uses.
        self._forget_zone_px = max(8.0, _fnum('ORION_READER_FORGET_ZONE_PX', 64.0))
        self._forget_defer_max = max(0, int(_fnum('ORION_READER_FORGET_DEFER_MAX', 12.0)))
        self._loc_forget_epoch = -1          # press the zone list below belongs to
        self._loc_forget_zones = []          # zones already forgotten in that press
        self._loc_forget_unzoned = False     # a caller with no box got its one forget
        self._loc_forget_logged = False      # the one ERROR line of this press is out
        self._loc_forget_defer_n = 0         # consecutive deferrals in this press
        self._frame_ts_last = None           # last frame ts seen by detect() (frame clock)
        self._loc_forget_suppressed = 0      # rate-limited calls (lifetime)
        self._loc_forget_deferred = 0        # pair-deferred calls (lifetime)
        # ------------------------------------------------------------------ 2026-09-16
        # [ORION_READER_PRESS_WITHHOLD_SUMMARY] EVERY LAYER GETS A LIVE COUNTER.
        #
        # The 09-16 blind run took a day to attribute because three of the six publication
        # layers had no number that survived to the log: `idle_unpublished=` and `idle_reuse=`
        # sit past the 300-char trim the native relay applies to every sidecar WARNING/ERROR
        # line (RemotePlaySession.cpp: `trimmed.left(300)`), and STATIC ZONE WITHHELD /
        # RESEED REFUSED (repeat) / COLD FIRST-READ VETOED are DEBUG lines that never leave
        # the sidecar at all. So: the health line carries the layer counters FIRST (below),
        # and every press closes with ONE ERROR summary naming what each layer withheld.
        self._pw_epoch = 0                   # press these counters belong to
        self._pw_flushed_epoch = 0           # press whose summary is already out
        self._pw_ghost_static = 0            # ghost breaker: evictions + zero-read holds
        self._pw_cold_first = 0              # COLD FIRST-READ VETOED
        self._pw_idle_gate = 0               # ORION_READER_IDLE_PUBLISH_GATE refusals
        self._pw_fresh_zone = 0              # RESEED REFUSED (retired ghost's zone)
        self._pw_static_zone = 0             # STATIC ZONE WITHHELD
        self._pw_reseed = 0                  # RESEED REFUSED (box latch)
        # [ORION_READER_FRESH_AFTER_GHOST] ...and the zone the ghost occupied keeps a WEAK
        # requirement after its identity is retired: GHOST IDENTITY RETIRED AFTER FULL ABSENCE
        # used to clear the quarantine outright, after which ANY next lock there published
        # freely -- including a re-rendered leftover, which is exactly what the 3-frame static
        # window then had to re-identify from scratch while publishing its first reads. While
        # this press still has not seen its own meter low, a lock inside the retired ghost's
        # zone must prove a LOW (< the engine's 40%% ownership bound) RISING pair before its
        # reads are handed to the engine. Sub-40 reads publish exactly as before (a single low
        # frame is harmless and the engine can own it); only unproven >=40 reads are withheld,
        # which the engine would refuse to anchor on anyway. Inert once the press has seen its
        # onset (_press_low_seen) and outside a press window.
        # [SHIP CONFIG 2026-09-17] DEFAULT OFF. GHOST_FORGET_LOCATOR (rate-limited) is what
        # the graded ship sessions actually ran to close the leftover-meter class; this
        # second, overlapping layer was in the OFF half of the 09-16 launch line and has no
        # graded live hours of its own. The code and its tests are KEPT --
        # ORION_READER_FRESH_AFTER_GHOST=1 restores it byte-for-byte.
        self._fresh_after_ghost = _flag('ORION_READER_FRESH_AFTER_GHOST', '0')
        self._press_fresh_zone = None        # (cx, cy) the retired/dropped ghost occupied
        self._press_fresh_zone_logged = False
        self._press_fresh_withheld = 0
        # [ORION_READER_BOX_LATCH] Once this press's own meter has been sighted and published,
        # the lock's BOX IDENTITY is latched for the rest of the press: the teleport re-seed
        # (three consistent far proposals adopted as a brand-new lock, _det_on_found) may not
        # silently replace the geometry the engine is measuring against mid-shot. A genuine
        # break still goes through _det_drop_lock, which the engine sees. Same invariant as
        # "fill denominator is the detector box -- latch it".
        self._box_latch = _flag('ORION_READER_BOX_LATCH', '1')
        self._box_latch_epoch = 0            # physical epoch owning the latch (0 = none)
        self._box_latch_generation = 0       # lock generation the latch belongs to
        self._box_latch_box = None
        self._box_latch_refused = 0
        # [ORION_READER_STATIC_ZONE_QUARANTINE] The owner's "false locks onto the court white
        # lines": live 2026-09-15 22:36:26-22:36:58Z, with NO press anywhere in the window, a
        # static column at box=[777,342,26,110] was locked and dropped ~20 times in 40s
        # (DETECTOR HEALTH locks 1->26 tracking drops 1->25 one-for-one). _det_drop_lock ARMS
        # the warm re-acquire memory at the spot the lock died, and _det_on_found's `warm`
        # shortcut re-locks there instantly -- a lock that never proved a rise is re-seeded from
        # its own corpse. So: a drop whose lock NEVER rose arms no warm memory and scores a
        # strike against that position; once a position has _static_zone_strikes strikes its
        # reads are WITHHELD (never published) until a lock there proves _static_zone_rise_pp of
        # rise, and the warm shortcut stays refused. A real meter rises on its second read, so
        # it pays one frame; a court line never rises at all. Sustained full-frame absence
        # decays the strikes (the object is gone), and records expire after the TTL.
        self._static_zone_q = _flag('ORION_READER_STATIC_ZONE_QUARANTINE', '1')
        self._static_zone_px = max(8.0, _fnum('ORION_READER_STATIC_ZONE_PX', 48.0))
        self._static_zone_strikes = max(1, _inum('ORION_READER_STATIC_ZONE_STRIKES', 2))
        self._static_zone_min_reads = max(1, _inum('ORION_READER_STATIC_ZONE_MIN_READS', 3))
        self._static_zone_rise_pp = max(0.0, _fnum('ORION_READER_STATIC_ZONE_RISE_PP', 2.0))
        self._static_zone_ttl_s = max(1.0, _fnum('ORION_READER_STATIC_ZONE_TTL_S', 20.0))
        self._static_zone_absence_results = max(
            1, _inum('ORION_READER_STATIC_ZONE_ABSENCE_RESULTS', 30))
        self._static_zone_max = max(1, _inum('ORION_READER_STATIC_ZONE_MAX', 8))
        # [STATIC ZONE 2026-09-21 owner: "false locks onto white objects in the park"] Two
        # holes measured live 2026-09-21 03:18-04:19 (15 quarantines, 5 releases):
        #  (a) RELEASE was a single read >= _static_zone_rise_pp above the lock's first read.
        #      One jittery +2pp read on a jersey / HUD bar "proved a rise", the record was
        #      retired, and the same spot was re-quarantined 1-2 s later ((156,316) 08:49:59
        #      -> 08:50:01; (474,447) -> (501,465) 09:05:08 -> 09:05:09). With NO press
        #      armed, release now needs the same ORDERED proof read() demands before it will
        #      say "rising": _fresh_up_req() consecutive reads each _fresh_up_pp higher, reset
        #      by any drop of that size. An ARMED press keeps the old first-rise release, so a
        #      real shot whose meter lands in a quarantined zone pays exactly what it paid
        #      before (one frame past the rise). Idle is where every false release happened.
        #  (b) The record EXPIRES after _static_zone_ttl_s and the object has to be caught
        #      lying twice more -- two more published false locks, two more "Shot meter
        #      detected" notices -- each time. The left-edge HUD element at (85, 526-557) was
        #      quarantined 4 times in 4 minutes. A REPEAT offender (same 96px bucket
        #      quarantined again after its record expired) now gets a longer TTL each time,
        #      doubling up to _static_zone_repeat_max_x; the ledger forgets a bucket
        #      _static_zone_repeat_forget_s after its last quarantine, and a genuine meter
        #      proving itself there clears it. ORION_READER_STATIC_ZONE_REPEAT_ESCALATE=0
        #      pins (b) off (every record gets the base TTL, byte-identical to before).
        self._static_zone_repeat_escalate = _flag('ORION_READER_STATIC_ZONE_REPEAT_ESCALATE', '1')
        self._static_zone_repeat_max_x = max(1, _inum('ORION_READER_STATIC_ZONE_REPEAT_MAX_X', 8))
        self._static_zone_repeat_forget_s = max(
            1.0, _fnum('ORION_READER_STATIC_ZONE_REPEAT_FORGET_S', 600.0))
        self._static_zone_repeat = {}        # bucket -> {'n': quarantines, 'ts': last}
        self._static_zones = []              # [{'cx','cy','n','ts','logged','ttl'}]
        self._static_zone_nofind_n = 0
        self._static_zone_withheld = 0
        # [ANCHOR INSTRUMENT 2026-09-21] A withheld read whose box sits where the PREVIOUS
        # withheld read sat = the proposer handing the same quarantined object back again.
        # Live epoch 60 (08:47-08:51) withheld 90 reads in one press and that press's real
        # meter was first sighted 1499 ms after the press: the quarantine silences the read
        # but nothing steers the proposer off the object. This counts how often that happens.
        self._static_zone_withheld_repeat = 0
        self._static_zone_withheld_last = None
        # [ORION_READER_COLD_FIRST_READ_VETO] COLD-LOCK FIRST-READ SANITY (2026-08-30,
        # session_20260830_191051). A COLD lock's first 1-2 fill reads on a raw proposal
        # box can measure garbage-HIGH before the box/denominator settles: measured live,
        # stick-shot first locks published 72.6/73.6/77.4 then corrected to 11.8/13.5 one
        # frame later (epochs 46/52; epoch 16 pre-press read 72.9 then 11.8). The engine's
        # ownership anchor refuses an episode whose FIRST sight is > 40 (a shot first seen
        # high was never seen starting), so one garbage frame killed those stick presses
        # outright -- and the same 2-frame pre-identification window let epoch 18's SECOND
        # leftover (a 44.8% aborted-shot ghost the press-time drop did not hold) reach the
        # sampler and become a reservation that instantly missed its deadline.
        # So: the first _cold_first_read_n reads of a COLD (not warm-reacquired) lock are
        # NOT published when they measure >= _cold_first_read_pct (the engine's anchor
        # bound: such a first sight is unownable anyway). The lock is KEPT -- read #2/#3
        # publishes the corrected value, so a stick shot's first PUBLISHED sight is the
        # ownable low read. A real onset (reads 0-10) is never touched, a warm mid-rise
        # re-lock is exempt, and the ghost breaker runs FIRST (zone-following and eviction
        # cadence unchanged). Fail-closed: this only ever WITHHOLDS frames the engine
        # would refuse to anchor on.
        self._cold_first_read_veto = _flag('ORION_READER_COLD_FIRST_READ_VETO', '1')
        self._cold_first_read_n = max(1, _inum('ORION_READER_COLD_FIRST_READ_N', 2))
        self._cold_first_read_pct = max(0.0, _fnum('ORION_READER_COLD_FIRST_READ_PCT', 40.0))
        # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] PRESS-CLOCK SERVE PLAUSIBILITY (2026-08-31,
        # live 04:51-04:54Z bout, epochs 32/50/51/58 = all four detector_authority_lost_abort).
        # The leftover meter's FADE-OUT defeats the ghost guard's low-sighting model: as the
        # alpha fade collapses the masked fill, the read DECLINES THROUGH the low band and
        # manufactures exactly the "real onset" signature the guard trusts -- one low read
        # (2.8/5.1/12.7/32.4 measured) stood the guard down, the very next read re-locked the
        # fading leftover HIGH (63.2/94.3/51.4/56.2), the engine anchored its ownership on
        # that trace, and the leftover then finished fading to 0.0 -> presence rejected ->
        # authority lost, in every case BEFORE the real meter had even rendered (ownership at
        # press_age 36-292ms vs the measured real-onset floor of ~400ms; capture+render alone
        # is ~230ms, so NO real meter can be on screen that early).
        #
        # Two physical invariants close the hole, both press-scoped and inert once the press
        # has seen its own genuine low (same fail-closed gate shape as the guards above):
        #   * ONSET CLOCK -- a real meter cannot show fill F before the press clock allows it:
        #     F <= first_margin + max_rate * (press_age - onset_floor). Fastest measured live
        #     rate 0.2835 %/ms (cap 0.35 with headroom); earliest measured genuine ownership
        #     226ms (floor 120ms with headroom; capture latency alone is ~230ms).
        #   * RISE STEP -- an UPWARD jump between published serves that lands AT/ABOVE the
        #     engine's 40% anchor bound and outruns max_rate*dt (+step margin) is a tracker
        #     OBJECT SWITCH, never the same meter rising (the failures jumped
        #     +38.7..+89.2pp in <=85ms, all landing 51-94). Jumps landing BELOW 40 are
        #     always served: a genuine POP-IN serve out-steps the ribbon rate while the bar
        #     itself is still growing (measured 0.59 pct/ms on real rises, epochs 25/26/40
        #     of session_20260830_191051) but always lands low -- clamping those benched
        #     three real shots in the replay A/B before this narrowing.
        # A read that violates either is withheld; at/above the anchor bound the lock is
        # also evicted (it is holding the single locker the real onset needs) and, on an
        # onset-clock veto, the ghost quarantine is armed at its box so the fading object's
        # follow-up reads are owned by the level-band machinery. The stand-down paths
        # additionally demand a RISING pair (see _press_pair_min_rise_pp): two low reads
        # that do not rise are a fade, not an onset. Real onsets are untouched: every
        # genuine first serve measured tonight (3.0-38.7 at press_age 226-805ms) passes the
        # clock with >=2x margin.
        self._press_onset_plaus = _flag('ORION_READER_PRESS_ONSET_PLAUSIBILITY', '1')
        self._press_max_rate_pct_ms = max(0.0, _fnum('ORION_READER_PRESS_MAX_RATE_PCT_MS', 0.35))
        self._press_onset_floor_ms = max(0.0, _fnum('ORION_READER_PRESS_ONSET_FLOOR_MS', 120.0))
        self._press_first_margin_pp = max(0.0, _fnum('ORION_READER_PRESS_FIRST_MARGIN_PP', 6.0))
        self._press_step_margin_pp = max(0.0, _fnum('ORION_READER_PRESS_STEP_MARGIN_PP', 4.0))
        self._press_pair_min_rise_pp = max(0.0, _fnum('ORION_READER_ONSET_PAIR_MIN_RISE_PP', 0.2))
        self._press_last_pub_fill = None   # last PUBLISHED nonzero detector fill this press
        self._press_last_pub_ts = None
        self._press_onset_cand = None      # (fill, ts) sliding onset-pair candidate (both paths)
        self._press_implausible_n = 0      # per-press veto count (flushed into the summary)
        self._press_implausible_logged = False
        self._press_rise_run = 0           # consecutive plausible rising published steps
        # ------------------------------------------------------------------------------------ #
        # LOCK LIFECYCLE (port of the retired meter_detector.py's ACQUIRE/KEEP/COAST/DROP,
        # 2026-08-30). The owner's direction: "the meter detector previous to this one had all
        # of this solved" -- its lifecycle was stable; only its colour-based LOCATING failed on
        # the 2K27 white meter. Here YOLO (+ the region/size plausibility gate) IS the locator,
        # and the retired lifecycle governs what a proposal may do:
        #
        #   ACQUIRE  a lock needs _det_acq_frames CONSISTENT fresh proposals (retired
        #            _MIN_FRAMES=2: "2 locks at ~40% fill with ZERO new false-locks"), OR one
        #            STRONG proposal paired with behavioural evidence (retired T-a4: "loc_strong
        #            is NOT a standalone corroboration ... require it PAIRED with a behavioral
        #            signal") -- our pairing is the PHYSICAL shot arm (a press just happened, a
        #            meter is expected; the same signal sync-on-acquire already trusts), so the
        #            armed first lock still lands on the onset frame (first-fill 0-10% today,
        #            must not regress), OR a WARM re-acquire near a recently dropped lock
        #            (retired _WARM_REACQ_S=1.2s: "a 1-2 frame dropout mid-rise doesn't leave
        #            the rest of the rise blind re-proving itself").
        #   KEEP     a fresh proposal within the tracking jump gate updates the lock; one that
        #            TELEPORTS does NOT move it (retired MeterBoxKalman: "on 'reject' the
        #            COASTED prediction is served, not the raw jump") -- it must win
        #            _det_reseed_n mutually-consistent strikes first (retired 3-strike re-seed).
        #            This kills the live 543px |dcx| box teleport and the off/on churn around it.
        #   COAST    misses are bridged by the held+extrapolated box for _det_hold_s as today,
        #            EXTENDED to _det_coast_max_s once the lock has PROVEN a rise (retired
        #            peak-hold: at the cap the meter is hardest to detect exactly when the shot
        #            matters most; _PEAK_HOLD_S=0.6 wall-clock bound so it can't hallucinate).
        #   DROP     needs MORE evidence than keep (retired hysteresis: max_freeze_frames=10 /
        #            low_conf_grace_frames=8): _det_nometer_drop consecutive fresh confident
        #            no-meter results force the drop even inside the coast, so the veto's safety
        #            property survives; a single no-meter blip no longer flickers the lock.
        #
        # ORION_METER_LIFECYCLE=0 restores the pre-port behaviour byte-for-byte.
        self._det_lifecycle = _flag('ORION_METER_LIFECYCLE', '1')
        self._det_state = 'idle'            # idle | pending | locked
        self._det_streak = 0                # consecutive CONSISTENT fresh proposals (pending)
        self._det_pend_box = None
        self._det_pend_ts = -1.0e9
        self._det_seen_dts = -1.0e9         # newest CONSUMED detector-result stamp: in async
        #                                     mode latest() repeats one result across frames, and
        #                                     counting it twice would fake a 2-frame streak.
        self._det_nm_strikes = 0            # consecutive fresh no-meter results while locked
        self._det_strike_box = None         # teleport re-seed candidate + count (3-strike)
        self._det_strike_n = 0
        self._det_lock_fill0 = None         # first fill of this lock (rise corroboration)
        self._det_lock_fill_max = -1.0
        self._det_lock_prev_fill = None   # last accepted read (ordered-rise run)
        self._det_lock_up_n = 0            # consecutive reads each _fresh_up_pp higher
        self._det_lock_was_warm = False     # warm re-acquire -> first-read veto exempt
        self._det_lock_read_n = 0           # fill reads served by THIS lock
        # Monotonic identity for a lifecycle-approved detector lock.  The capless
        # onset proof below must never combine measurements from two acquisitions,
        # even when both happen to occupy nearly the same pixels.
        self._det_lock_generation = 0
        self._df_nogreen_key = None
        self._df_nogreen_samples = []
        self._det_last_presence = False     # last frame's box still measured a white ribbon
        self._det_warm_pos = None           # where a lock was lost (warm re-acquire memory)
        self._det_warm_ts = -1.0e9
        self._det_emit = None               # smoothed EMIT state [cx, bottom, w, h] -- retired
        #                                     _Tk kept RESPONSIVE matching state but emitted an
        #                                     EMA'd, slew-capped box ("kills the box drift")
        # 2 = retired _ParkTracker._MIN_FRAMES (5->2 measured: locks at ~40% fill, zero new
        # false-locks; the spatial gates do the rejection, not a long wait).
        self._det_acq_frames = max(1, _inum('ORION_METER_ACQ_FRAMES', 2))
        # 0.55 = retired _LOC_STRONG_CONF (a conf>=0.55 learned-detector box was "the appearance
        # proof the rise-check stands in for").
        self._det_acq_strong = _fnum('ORION_METER_ACQ_STRONG_CONF', 0.55)
        # 80px @720-wide reference, scaled by frame width = retired position_jump_max_px +
        # _acq_jump_gate_px scaling. The outlier scale is 1.5 = the retired MeterBoxKalman's
        # MOVEMENT gate (meter_detector.py ~4749: update_pose(..., _acq_jump_gate_px() * 1.5))
        # -- the retired system had TWO gates and the validator's looser x3 only decided lock
        # SURVIVAL, which strikes now govern; whether a measurement may MOVE the box was always
        # the tighter 1.5x (~213px @1280), and real motion is ~9-13px/frame, far inside it.
        self._det_jump_base = _fnum('ORION_METER_JUMP_GATE_PX', 80.0)
        self._det_track_scale = _fnum('ORION_METER_OUTLIER_JUMP_SCALE', 1.5)
        self._det_reseed_n = max(1, _inum('ORION_METER_RESEED_STRIKES', 3))
        # 3 fresh full-frame "no meter anywhere" verdicts to drop a held lock. The retired
        # detector granted 8-10 frames of grace on a mere CONTOUR miss; a trained detector's
        # explicit no-find is much stronger evidence, so 3 is already conservative -- and it is
        # deliberately MORE than the acquire streak (2), the drop-harder-than-keep hysteresis.
        self._det_nm_drop = max(1, _inum('ORION_METER_NOMETER_DROP', 3))
        # 0.6s = retired _PEAK_HOLD_S (RC-3 wall-clock bound on the post-peak coast).
        self._det_coast_max_s = max(0.0, _fnum('ORION_METER_COAST_MAX_S', 0.6))
        # 8pp fill rise to earn the extended coast ~= retired _RISE_MIN=8px on the ~107px bar
        # (the peak-hold latch demanded a PROVEN rise so static decor never earns the hold).
        self._det_coast_rise_pp = _fnum('ORION_METER_COAST_RISE_MIN_PP', 8.0)
        self._det_warm_s = max(0.0, _fnum('ORION_METER_WARM_REACQ_S', 1.2))
        # PARTIAL-OCCLUSION FILL RECOVERY.  The locator/lifecycle owns *whether* a
        # meter exists; this path only recovers *where the white fill edge is* when
        # a foreground limb masks enough of the ribbon that the ordinary row-mean
        # walk falls below its 28% width threshold.  It is deliberately incapable
        # of cold acquisition: recovery requires a current hardware-shot epoch, a
        # lifecycle-approved lock, structure proof for that same epoch, three
        # recent direct rising reads, and surviving bottom-connected meter pixels.
        # Recovered reads never refresh their own deadline/history, so a stale box
        # or a fully hidden meter expires after this short wall-clock budget.
        self._det_occ_fill = _flag('ORION_METER_PARTIAL_OCCLUSION', '1')
        self._det_occ_max_gap_s = min(0.25, max(
            0.0, _fnum('ORION_METER_PARTIAL_OCCLUSION_MAX_MS', 120.0) / 1000.0))
        self._det_occ_negative_bridge_s = min(0.045, max(
            0.0, _fnum('ORION_METER_NEGATIVE_BRIDGE_MAX_MS', 45.0) / 1000.0))
        self._det_occ_min_direct = max(
            3, _inum('ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT', 3))
        self._det_occ_min_cols = max(
            2, _inum('ORION_METER_PARTIAL_OCCLUSION_MIN_COLS', 2))
        self._det_occ_direct = _collections.deque(maxlen=8)
        self._det_occ_key = None
        # Verdict from the newest unique locator result.  A new full-frame
        # negative, stale result, refused proposal, or pre-arm result clears it;
        # repeated reads of one still-fresh accepted positive leave it set.
        self._det_occ_locator_source_positive = False
        self._det_occ_recovered = False
        self._det_occ_support = 0.0
        self._det_occ_kind = ""
        self._det_occ_censored_reject = False
        self._det_occ_current_full_negative = False
        self._det_occ_current_source_age = float("inf")
        self._det_occ_negative_bridge_used = False
        # 8px/frame emit slew = retired _Tk.add's display cap ("a ~50px 1-frame contour-SPLIT
        # spike can't stride the box"); the NCC-matched position bypasses the cap exactly as the
        # retired template match wrote cx_emit directly (0.12px MAE beats any smoothing).
        self._det_emit_slew = _fnum('ORION_METER_EMIT_SLEW_PX', 8.0)
        # Live health counters (see the throttled DETECTOR HEALTH log in detect()).
        self._det_diag = {"calls": 0, "found": 0, "fresh": 0, "seeded": 0,
                          "vetoed": 0, "stale": 0, "nofound": 0, "sync_acq": 0,
                     "locator_exception": 0, "fill_exception": 0,
                     "health_exception": 0,
                          "priority_acq": 0,
                          "lock": 0, "drop": 0, "reseed": 0, "outlier": 0,
                          "rescue": 0, "rescue_seat": 0,
                          "scan_full": 0, "scan_full_hit": 0,
                          "scan_left": 0, "scan_left_hit": 0,
                          "scan_right": 0, "scan_right_hit": 0,
                          "scan_partial_miss": 0, "occlusion_fill": 0,
                          "occlusion_top": 0, "occlusion_negative": 0,
                          "occlusion_reject": 0, "idle_unpublished": 0}
        self._det_diag_last = 0.0
        # [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] see _idle_publish_ok for the whole
        # argument.
        # [SHIP CONFIG 2026-09-17] DEFAULT OFF. It was in the OFF half of the 09-16 launch
        # line: withholding idle publications is a cosmetic/eviction win, but it is one more
        # layer that can refuse a real first read, and the 09-16 blind run (six consecutive
        # backstopped presses) was traced to exactly that kind of unrated publication layer.
        # STATIC_ZONE_QUARANTINE + BOX_LATCH + the rate-limited GHOST_FORGET are the layers
        # that are graded. ORION_READER_IDLE_PUBLISH_GATE=1 restores the gate unchanged.
        self._idle_pub_gate = _flag('ORION_READER_IDLE_PUBLISH_GATE', '0')
        self._idle_pub_rise_pp = max(0.0, _fnum('ORION_READER_IDLE_PUBLISH_RISE_PP', 3.0))
        self._idle_pub_cont_s = max(0.0, _fnum('ORION_READER_IDLE_PUBLISH_CONT_S', 0.5))
        self._idle_pub_tol_px = max(0.0, _fnum('ORION_READER_IDLE_PUBLISH_TOL_PX', 24.0))
        self._idle_pub_fills = deque(maxlen=3)   # last 3 reads of the CURRENT column
        self._idle_pub_seen = None               # (cx, cy, ts) of the last read column
        self._idle_pub_box = None                # (cx, cy, ts) of the last PUBLISHED column
        if _flag('ORION_METER_DETECTOR', '0'):
            try:
                import meter_detector_yolo as _mdy
                self._meter_detector = _mdy.get_async_locator()
                # ERROR level so the native relay does not throttle this away; it is a
                # one-line load record, not spam. Reports the actual ORT provider so a
                # silent CPU fallback (which would run ~174 ms and starve the seed) is visible.
                _acq_logger.error(
                    "METER DETECTOR load: ok=%s provider=%s model=%s gpu_prep=%s%s",
                    self._meter_detector is not None,
                    getattr(self._meter_detector, "provider", "?"),
                    getattr(getattr(self._meter_detector, "_base", None), "model_path", "?"),
                    getattr(getattr(self._meter_detector, "_base", None), "_prep_sess", None) is not None,
                    ("" if getattr(getattr(self._meter_detector, "_base", None), "_prep_sess", None) is not None
                     else " (%s)" % getattr(getattr(self._meter_detector, "_base", None), "_prep_disabled", "?")))
            except Exception as _e:
                self._meter_detector = None
                try:
                    _acq_logger.error("METER DETECTOR load FAILED: %r", _e)
                except Exception:
                    pass
        # (3b) FAKE-LOCK / DEAD-HOLD breaker (default ON): suppress a detection whose REPORTED fill
        # is byte-identical for longer than a physical hold (static red dÃ©cor / stuck hold). Caps are
        # frames @ the mid-fill / near-tip tiers. DEFAULT-OFF: it catches the real 222-frame dead-holds
        # but over-suppresses legit within-shot frames (costs 3 regression-gate cases); N4 rise-probation
        # is the surgical replacement. Live-A/B it (ORION_READER_FAKELOCK_BREAK=1) before defaulting on.
        # SHIPPED DEFAULT-ON 2026-08-04. Until now this was default-OFF in code and ON only
        # via the dev launcher, so a CUSTOMER ran a materially different detector from the
        # one measured on the rig -- the 96.9%% fire-rate batch was flag-ON, and nobody
        # else would have been running it. Set the env var to '0' to A/B back.
        self._fakelock_break = _flag('ORION_READER_FAKELOCK_BREAK', '1')
        self._fakelock_cap_mid = max(4, _inum('ORION_READER_FAKELOCK_CAP_MID', 40))
        self._fakelock_cap_hi = max(4, _inum('ORION_READER_FAKELOCK_CAP_HI', 60))
        # (3b'') B6 ARM-GRACE CAP -- THE BREAKER WAS UNREACHABLE IN PRODUCTION (2026-08-04).
        # detect() only runs read() while `_shot_armed_hw` is True on the live path
        # (require_gameplay_eligibility=True, orchestrator :1330/:1358), and the B2 arm-guard
        # below hands `_shot_armed_hw` blanket breaker grace -- which zeroes `_static_fill_n`
        # EVERY frame. So on the shipped wiring the counter can never leave 0 and the
        # suppression can never fire: `static_fake_lock` appears ZERO times in every
        # 2026-08-03/04 live log, while the logs carry 2.1-8.8 s dead/false locks it was built
        # to kill (a window mullion at x~0.11W y~0.08H holding fill 10-18% for 8826 ms with
        # green_c=-1.0 throughout). The breaker's own unit tests never caught this because they
        # construct SimpleMeterReader(W, H) WITHOUT require_gameplay_eligibility, the one
        # configuration production never uses.
        #
        # The A2 intent was to protect the WITHIN-SHOT hold (a real release/outcome freeze sits
        # byte-frozen for a few hundred ms and breaking there minted a 7-frame gate gap). That
        # intent is a BOUNDED window, not an unbounded one: cap the hw-arm grace at
        # `_arm_grace_ms` wall-time from the physical arm edge. After it expires the breaker
        # still needs its FULL 40/60-frame cap, so nothing can be suppressed earlier than
        # ~arm+2.67 s -- comfortably past the meter's life on a standstill AND past the Go-To
        # ~1600 ms blind floor. Replayed over the REAL logged fill sequences this kills 17/18
        # long dead/false locks (all four mullion episodes, ~950 ms into the run) and leaves the
        # one genuine monotone rise in that set untouched. 0 -> legacy unbounded grace.
        self._arm_grace_ms = max(0.0, _fnum('ORION_READER_ARM_GRACE_MS', 2000.0))
        self._hw_arm_ts = None          # wall-time of the current physical arm edge
        self._hw_arm_grace_epoch = 0    # the hardware epoch that edge belongs to
        # (3b''') B7 ARM-EDGE PRIOR-PRESENCE VETO -- "the meter did not exist at the press".
        # The 2026-08-04 false lock is a window mullion at x~0.11W that the RELAXED `_MICRO_RED`
        # band ((0,0,110)-(110,110,255)) reads as a textbook Arrow2 column: measured over
        # session_20260804_032333, x<280 yields 18 qualifying columns under the production band
        # (all of them the REAL meter panning past) but 1699 under the micro band, 1583 of them
        # parked at x 96-127 -- w 14-15, h 30-35, aspect ~2.2, red_frac 0.46-0.51 (the floor is
        # 0.45). Once the micro band LATCHES into `_lock_red` the mullion reads as solid red for
        # the whole lock life, so `_relocate` re-finds it every frame forever.
        #
        # Neither shape nor position nor pixel-variance can separate it:
        #   * geometry IS meter-shaped (that is why it locks);
        #   * position is not usable -- a REAL meter in this very session seats at cx=0.115, the
        #     mullion's own x, so any left-edge veto costs real shots;
        #   * a static-pixel/variance veto FAILS -- measured ROI mean|diff| vs 15 frames earlier
        #     is 27.0 for the mullion vs 29.3 for real meters (the camera pans constantly, so
        #     decor moves as much as the HUD does).
        # What DOES separate them is PRESENCE: a shot meter is a HUD element that does not exist
        # until the shot; a mullion is in every frame. Measured at the physical arm edge, the red
        # fraction inside the candidate box is 0.000 median / 0.020 MAX over 465 real-meter
        # candidates, versus 0.350 median over 707 mullion candidates. A 0.10 threshold rejects
        # 68% of the decor at 0.00% real-meter cost -- a 5x margin over the worst real meter.
        #
        # Applied ONLY to a COURT-WIDE/relaxed-band cold acquire inside a hw epoch whose arm-edge
        # frame we actually captured; nominal-band acquires and every mid-shot relocate/hold are
        # never tested. Fails OPEN whenever the reference is missing or stale.
        #
        # DEFAULT-OFF, live-A/B (same convention as FAKELOCK_BREAK). The veto assumes the
        # arm-edge frame is PRE-HUD, which is what the live measurement shows (real meters read
        # 0.000/0.020 there). It cannot, however, distinguish decor from a meter that was ALREADY
        # on screen when the epoch was latched -- and that is exactly what the synthetic
        # half-court/court-wide acquisition tests construct, so enabling it by default would
        # trade this false lock for a half-court acquisition regression. Since B6 above already
        # kills the SUSTAINED lock (the actual damage) with zero measured cost, B7 ships as the
        # opt-in second layer: set ORION_READER_ARM_EDGE_VETO=1 to A/B it live.
        self._arm_edge_veto = _flag('ORION_READER_ARM_EDGE_VETO', '0')
        self._arm_edge_pp = max(0.0, _fnum('ORION_READER_ARM_EDGE_PP', 0.10))
        self._arm_edge_mask = None      # widest-band red mask of the arm-edge frame
        self._arm_edge_mask_epoch = 0   # the hardware epoch it belongs to
        self._arm_edge_vetoes = 0       # observability
        # (3b'''') B8 FRAME-BORDER VETO. A cold acquire whose box is CLIPPED by the frame edge
        # is a sliver of background architecture running off screen, not a player-attached HUD.
        # Measured cost on 2513 real in-shot meter boxes (session_20260804_032333, 29 shots):
        # ZERO -- min x = 3, min y = 191, 0.00% touch any border. See _clipped_at_frame_border.
        #
        # DEFAULT-OFF for the same reason as B7: that ZERO cost is measured on ONE session at one
        # camera range, whereas test_served_bbox_contains_apex_cap_body_and_floor_without_shot_
        # stretch pins a 6px-wide meter at col_x 3 / 1271 as a SUPPORTED 720p scale -- i.e. the
        # suite asserts that a narrow, edge-riding meter is real. Rather than trade this false
        # lock for that supported case, B8 ships as an opt-in lever: ORION_READER_BORDER_VETO=1.
        self._border_veto = _flag('ORION_READER_BORDER_VETO', '0')
        self._border_vetoes = 0         # observability
        # (3b''''') B9 MICRO SESSION-SCALE GATE -- what ACTUALLY admits the mullion (2026-08-04).
        #
        # The acquire forensics settle the attribution the earlier passes had to infer. Every
        # sustained decor lock in the live logs was seated by ONE tier:
        #   10:13:20 / 10:13:26 / 10:13:29 (the 05:13 screenshot, epoch 27, three re-locks on the
        #   SAME arena facade with no meter on screen)
        #     "READER ACQUIRE: tier=courtwide:micro_compressed_cap_first floor_px=0 h=36..44 w=12..13"
        # and a stateless per-frame census of session_20260804_032333 (17341 frames, every tier
        # evaluated on every frame) makes it quantitative:
        #     tier                 frames   agrees with the nominal-band meter
        #     scan (nominal)         1118   -- the reference
        #     popin_stub              829   yes
        #     courtwide strict_red     787   750 (the 37 others are the same meter BELOW the band)
        #     courtwide codec_cap      266   249
        #     courtwide MICRO          638   *** 0 ***
        # All 638 micro candidates were decor: 618 had no corroboration from ANY other tier and
        # 580 of those sat in one 40x40 px patch of arena facade (x 240-280, y 180-220); the 20
        # frames where a real meter WAS on screen put the micro candidate hundreds of px away
        # from it (e.g. micro [177,222] vs meter [530,315]). Widths are disjoint too: micro 3-13
        # (median 9) versus 13-20 (median 16) for every real-meter tier.
        #
        # WHY it can only ever be decor here: the micro tier is a bootstrap for a capture where
        # the meter rasterizes BELOW the strict tiers' geometry floor -- its own admission
        # ceiling is a green cap <= 0.44x nominal (`max_micro_green_w` in
        # _acquire_structure_tip_first) with the body scaled to match. That is a claim about the
        # CAPTURE, and the reader has already MEASURED the capture: `_track_h_hist` is the
        # full-track height (green-cap top -> floor), seeded only from near-full caps, guarded by
        # the A1 two-sided gate and the A4 high-side cap, and 106-111 px all session here against
        # a ~100 px nominal track. A meter rendering at ~1.08x nominal cannot simultaneously be
        # the <=0.44x meter this tier exists to rescue, so running it buys nothing and pays a
        # full-frame scan at 4x colour recall and 0.18x geometry floors.
        #
        # B9a -- THE ADMISSION ITSELF: `_MICRO_RED` IS NOT A RED BAND.
        # It is the BGR box [0,0,110]-[110,110,255], i.e. "B<=110 and G<=110 and R>=110". The
        # corner (110,110,110) is NEUTRAL GREY, so the band admits every dim grey, brown and
        # warm-stone pixel in the arena -- which is precisely the material a facade is made of.
        # Measured over the 638 micro candidates of session_20260804_032333 against the 1163
        # real-meter candidates of the SAME frames, using R - max(G,B) over the mask pixels
        # inside each candidate box:
        #     micro candidates : min 7   p05 8    median 8    max 24     ((R-max(G,B))/R = 0.1)
        #     real meter bodies: min 220 p05 228  median 228  max 228    (ratio 1.0)
        # Total separation, no overlap, a 9x gap. The decor the tier locks is not dim red at
        # all; it is GREY that the band's corner lets through.
        #
        # So intersect the band with the one property a BGR box cannot express -- that red
        # actually DOMINATES. The floor is set against the SOFTEST red the neighbouring tiers
        # are willing to call red, not against this session: the codec tier's `_COURTWIDE_RED`
        # worst case (170,70,70) and the micro unit fixture's own MICRO_RED (190,90,90) both
        # sit at dominance 100, and the softened test reds SOFT_RED_A/B at 170/145. A floor of
        # 40 is therefore 5x above the worst measured decor and still 2.5x BELOW the softest
        # legitimate red anywhere in the stack -- it removes grey, not chroma-averaged red.
        # 0 disables (byte-identical to HEAD). See _micro_redmask.
        self._micro_red_dom = max(0, _inum('ORION_READER_MICRO_RED_DOM', 40))
        # B9b SESSION-SCALE GATE (second layer, for decor that IS genuinely red).
        # Once the session has measured its own meter scale, refuse the micro tier when that
        # measurement is more than `_micro_gate_ratio` x the tier's own scale ceiling. This is
        # what B7/B8 could not be: it FAILS OPEN exactly where they collided. A cold reader (the
        # micro unit fixtures, a genuinely sub-quarter-scale capture) has no measurement and is
        # untouched; a real camera-scale change is adopted by A1's low-side recovery within
        # `_scale_reset_m` consistent frames, so the gate follows the capture instead of pinning
        # it. Nothing outside the micro tier is tested -- strict/codec court-wide, the nominal
        # scans, pop-in and early-stub acquire byte-identically.
        #
        # The measurement it reads is `_sess_track_h`, NOT `_track_h_hist`: the latter is wiped
        # by `_reset_state` on every hardware disarm (so it is empty at the start of every shot,
        # where this gate has to decide) and, worse, a micro decor lock latches `_MICRO_GREEN`
        # as its own cap band and then SEEDS that history with its own ~40px decor track --
        # a self-poisoning loop that held the gate open in the offline replay. `_sess_track_h`
        # is session-scoped and is only ever written by a lock that the micro tier did not seat.
        self._micro_scale_gate = _flag('ORION_READER_MICRO_SCALE_GATE', '1')
        self._micro_gate_min_n = max(2, _inum('ORION_READER_MICRO_GATE_MIN_N', 5))
        self._micro_gate_ratio = max(1.0, _fnum('ORION_READER_MICRO_GATE_RATIO', 2.0))
        self._micro_gate_blocks = 0     # observability
        # ---------------------------------------------------------------- #
        #  F3 INSTRUMENT FIXES (default ON, all three OBSERVABILITY-ONLY).
        #  Each records a quantity the reader already computed and threw away, so the batch can
        #  measure its own accuracy. None of them changes an emitted value: with all three OFF
        #  the reader is byte-identical to HEAD (see test_f3_*_flag_off_is_byte_identical).
        # ---------------------------------------------------------------- #
        self._fill_overflow_diag = _flag('ORION_READER_FILL_OVERFLOW', '1')
        self._fill_raw = (0.0, 0.0)     # PRE-clamp (coarse, subpix) fill %
        self._fill_overflow = 0         # 1 when the pre-clamp fill exceeded 100
        self._fill_underflow = 0        # 1 when the pre-clamp fill went below 0
        self._fill_overflow_n = 0       # session count of overflow frames
        self._gz_top_edge = _flag('ORION_READER_GZ_TOP_EDGE', '1')
        self._gz_ends = []              # neon-band TOP edge samples, fill-% scale
        self._green_band_diag = _flag('ORION_READER_GREEN_BAND_DIAG', '1')
        self._green_band_raw = -1.0     # PRE-clamp green cap band width, pp
        self._green_band_bound = ""     # "floor" | "ceil" | "" (unbound = a real measurement)
        # SESSION-scoped full-track heights from NON-micro locks. Deliberately NOT cleared by
        # _reset_state (that wipe is per-lock; this is the capture's scale) -- only
        # reset_session_scale(), i.e. a real meter/style change, ends it.
        self._sess_track_h = []
        # (3b') SHOT-COAST grace (N6, default OFF -> byte-identical, live-gated). The armed-hold
        # coast (3) floors the confidence ONLY while the MERGED gate stays ARMED. LIVE the hardware
        # hold-trigger releases AT the shot, so `armed` flips False exactly when the player's arm/
        # body crosses the meter (the ~10-20 frame shooting-motion occlusion) -- the coast then
        # falls back to the fast unarmed decay (CONF_RATE 0.01 ~= 5 frames) and the lock DROPS mid-
        # occlusion, so the release fires BLIND (offline-reproduced: session_20260717_231912 shot 1
        # drops at occlusion frame 5). This grace latches a short countdown whenever a RISING RED
        # column is read while locked; while the countdown is live the coast stays protected (armed
        # decay + CONF_MIN floor) and N4 probation / the fake-lock breaker are exempt -- so a real
        # shot's release occlusion coasts through on the held peak fill (a vision sample) instead of
        # vanishing. Self-bounding: the countdown decays every frame (incl. coast frames), so the
        # grace lasts at most _shot_coast_max frames past the last rising read, then normal decay
        # resumes. It only ever EXTENDS an existing coast on a provably-rising lock -> a pristine
        # present-meter frame and a non-rising dÃ©cor lock are both untouched.
        self._shot_coast = _flag('ORION_READER_SHOT_COAST', '0')
        self._shot_coast_max = max(1, _inum('ORION_READER_SHOT_COAST_FRAMES', 24))
        # NF-2 [fix]: the grace budget is an fps-INVARIANT WALL-TIME (ms), not a frame count.
        # The shooting-motion occlusion is a fixed wall-time (~0.5-0.6 s), so a frame count shrinks
        # in wall-time as fps rises (24f = 0.68 s @35fps but 0.20 s @120fps -> can't cover a 10-20
        # frame occlusion on the 60-120fps fork) and erodes during the visible peak/hold that
        # precedes the release. A wall-time deadline (from the frame ts, seconds) covers peak + hold
        # + occlusion regardless of fps, and self-bounds naturally: a true dÃ©cor dead-hold never
        # latches (latch still needs a RISING red read, vel>25) and, once latched, expires by wall
        # clock. Default 520 ms (measured occlusion ~0.5-0.6 s + a slow-release peak/hold margin;
        # tuned against the framedump replays). Set ORION_READER_SHOT_COAST_MS=0 to fall back to the
        # legacy frame count (_shot_coast_max) for offline A/B. Still gated behind ORION_READER_
        # SHOT_COAST -> flag OFF is byte-identical.
        self._shot_coast_ms = max(0.0, _fnum('ORION_READER_SHOT_COAST_MS', 520.0))
        # (3c) ANCHOR bundle (N1/N2/N3 geometry bug-fixes, default OFF -> byte-identical shipped):
        #   N1  box-VELOCITY anchored to the fill-invariant floor/notch (cy+ch), not the fill-column
        #       centre (cy+ch*0.5) which gains a phantom upward term on a static rising meter.
        #   N3  the EMITTED box WIDTH is a 5-sample median (raw contour width still drives masks/scale).
        #   (N2 -- full-track height in _box_from_tip -- was tried and REVERTED: it regressed the
        #    committed gate's median_shot_peak below floor + added a false-lock; see _box_from_tip.)
        # The master flag turns on N1+N3; per-item overrides (default = master) allow isolated
        # offline A/B attribution without re-plumbing (e.g. ANCHOR=1 ANCHOR_N1=0 tests N3 only).
        # SHIPPED DEFAULT-ON 2026-08-04. Until now this was default-OFF in code and ON only
        # via the dev launcher, so a CUSTOMER ran a materially different detector from the
        # one measured on the rig -- the 96.9%% fire-rate batch was flag-ON, and nobody
        # else would have been running it. Set the env var to '0' to A/B back.
        self._anchor = _flag('ORION_READER_ANCHOR', '1')

        def _flag_def(name, dflt_bool):
            v = _os.environ.get(name)
            return dflt_bool if v is None else v.strip().lower() in ('1', 'true', 'yes', 'on')
        self._anchor_n1 = _flag_def('ORION_READER_ANCHOR_N1', self._anchor)
        self._anchor_n3 = _flag_def('ORION_READER_ANCHOR_N3', self._anchor)
        # (3d) RISE-PROBATION (N4, default OFF, live-gated): a fresh COLD (non-hw, non-pop-in) acquire
        #   is tentative -- if the fill has not risen >= _prob_rise_pp with >= _prob_inc_min increasing
        #   steps within _prob_frames frames AND no green tip appeared, DROP it (rejection 'no_rise')
        #   and refuse a cold re-acquire for _prob_refuse_frames frames. Kills static red dÃ©cor in ~5
        #   frames instead of the reactive breaker's 40/60. hw-armed + pop-in acquires stay EXEMPT.
        self._rise_probation = _flag('ORION_READER_RISE_PROBATION', '0')
        self._prob_frames = max(2, _inum('ORION_READER_RISE_PROBATION_FRAMES', 6))
        self._prob_rise_pp = max(0.5, _fnum('ORION_READER_RISE_PROBATION_PP', 4.0))
        self._prob_inc_min = max(1, _inum('ORION_READER_RISE_PROBATION_STEPS', 3))
        self._prob_refuse_frames = max(0, _inum('ORION_READER_RISE_PROBATION_REFUSE', 6))
        self._prob_refuse_n = 0    # cooldown; persists across lock drops (NOT in _reset_state)
        self._prob_limit = self._prob_frames   # ACTIVE probation window; a steal probation
                                               #   (ORION_READER_STEAL_PROBATION) shortens it
        self._prob_from_steal = False          # provenance of the active probation (steal vs cold)
        # (3e) PCTL-FILL (N5, default OFF, live-gated): the occlusion-tolerant PERCENTILE red-extent
        #   read, split OUT of the monolithic ORION_READER_ROBUST bundle so it can ship on its own
        #   (survives ~2/3 column occlusion) without the trajectory-gate / dead-reckon / scale-rebase.
        # SHIPPED DEFAULT-ON 2026-08-04. Until now this was default-OFF in code and ON only
        # via the dev launcher, so a CUSTOMER ran a materially different detector from the
        # one measured on the rig -- the 96.9%% fire-rate batch was flag-ON, and nobody
        # else would have been running it. Set the env var to '0' to A/B back.
        self._pctl_fill = _flag('ORION_READER_PCTL_FILL', '1')
        # (4) colour tolerance: additive widen of the red/green bands (channel units). 0 = default.
        self._color_tol = max(0.0, _fnum('ORION_READER_COLOR_TOL', 0.0))
        # ------------------------------------------------------------------ #
        #  SESSION-DECAY / STALE-LOCK fix flags (2026-07-18). Each DEFAULT OFF ->
        #  flag-off is byte-identical to HEAD; A/B-verify on the framedump
        #  replays before any default flip:
        #    ORION_READER_SCALE_RESET  (A1) TWO-SIDED full-track seed gate -- the
        #      fill denominator median can RECOVER after a tall-outlier poisoning
        #      (dÃ©cor false-lock / reflection / momentary zoom-in), and a
        #      confirmed lock DROP also clears the fill-scale histories so the
        #      poison cannot survive across shots.
        #    ORION_READER_STALEBREAK   (A2) "rising" (and the advertised velocity)
        #      on a coast/green-hold frame requires a FRESH rising RED read within
        #      the freshness window -- a stale frozen-fill dead-hold can no longer
        #      re-arm the orchestrator's CV shot-gate; and the fake-lock breaker is
        #      enabled + HARDENED to BREAK the lock (drop state) so the cold
        #      re-acquire actually runs instead of the warm box re-locking dÃ©cor.
        #    ORION_READER_SCALE_GUARD  (A3) _scale_est is fed ONLY from hw-armed /
        #      genuinely-rising red locks (dÃ©cor/memory locks can't ratchet the
        #      gates open) and DECAYS back toward 1.0 when no qualified fresh
        #      sample arrives for a while.
        #    ORION_READER_OCCL_WIDE    (B1) a WIDER in-shot occlusion coast budget
        #      (covers a full left-side arm-cross), latched ONLY on an armed
        #      rising red read -- off-shot dÃ©cor acceptance is unchanged.
        #    ORION_READER_VZOOM        (B2) vertical follow: the relocate window
        #      and re-acquire band SHIFT with the locked meter's tracked floor-y
        #      (mirror of the horizontal _scale_est) so a camera zoom that lifts
        #      the meter out of the fixed prior band no longer loses it.
        #    ORION_READER_VZOOM_DOWN   (1a) arm-dive bottom WIDEN: at shot ARM the
        #      camera dive carries the meter floor THROUGH and BELOW the fixed
        #      band bottom (y1~770 -> ~891 @1080p, session_20260717_231912
        #      f100-260) -- the up-only VZOOM follow leaves the descent unhandled,
        #      so the lock coasts/drops, green-holds pin a stale fill and the
        #      truncated column misreads ~50% as ~16-44%. Fix: while a LIVE lock's
        #      tracked floor has genuinely dived below y1-margin, EXTEND y1 DOWN
        #      (a bottom WIDEN -- y0 never moves, bounded by
        #      ORION_READER_VZOOM_DOWN_MAX_PX) so relocate/coast/steal follow the
        #      dive. NOT the reverted bottom-SHIFT follow: gated on self.box (a
        #      cold/off-shot frame never widens, no decor area admitted off-shot)
        #      and a nominal floor comfortably inside the band never triggers.
        #      Requires VZOOM (the floor ref _vz_ref is only maintained under it).
        # ------------------------------------------------------------------ #
        self._scale_reset = _flag('ORION_READER_SCALE_RESET', '1')   # FLIPPED default-ON 2026-07-18 (A/B proven; 46/46 gates)
        self._scale_reset_m = max(2, _inum('ORION_READER_SCALE_RESET_M', 4))
        # ------------------------------------------------------------------ #
        #  A4 (ORION_READER_TRACK_H_CAP) -- the HIGH half of the full-track seed gate.
        #  See _read_fill: A1 gave the seed gate a LOW bound (0.90*max) and a recovery, but
        #  never a HIGH bound, so an inflated full-track candidate is admitted no matter how
        #  tall it is. The arrow-tip apex walk may chain up to 12px of background above the
        #  green cap; when it does, the 5-sample median rises with it and EVERY fill for the
        #  rest of that lock is divided by a track ~10% too tall -- a CONSTANT ~9pp under-read
        #  across the shot (horizon-flat, correlated within the shot, so no amount of frame
        #  averaging removes it). Measured on session_20260804_032333 (29 shots / 1182 locked
        #  frames): the denominator family is 106-109px on 96.9% of frames and 118-119px on
        #  the remaining 0.8%, and those frames sit on the EARLY RISE of two shots -- the exact
        #  window the release predictor extrapolates from -- mis-scaling them by 8.6pp (~46ms).
        #  TOL is RELATIVE to the reader's own running median (no absolute pixel constant, so
        #  it is resolution- and machine-independent); the meter is a fixed-size HUD whose
        #  full-track height does not change inside a lock, and the legitimate spread measured
        #  across a whole session is 106-109px (~+-1.4% about the median), so 6% is a >4x
        #  margin over real jitter while still rejecting the +9.3% apex-walk artefact.
        #  _M is the HIGH-side recovery length: a genuinely taller track (a real scale change,
        #  or a cold history seeded from one short outlier) is present on EVERY frame of the
        #  meter's life and so yields a long consistent run, whereas the apex-walk artefact
        #  measured here lasted 5 and 7 frames. Deliberately much longer than the LOW-side
        #  _scale_reset_m: admitting a high candidate too eagerly re-poisons the denominator,
        #  which is the failure this flag exists to stop.
        # ------------------------------------------------------------------ #
        # SHIPPED DEFAULT-ON 2026-08-04. Until now this was default-OFF in code and ON only
        # via the dev launcher, so a CUSTOMER ran a materially different detector from the
        # one measured on the rig -- the 96.9%% fire-rate batch was flag-ON, and nobody
        # else would have been running it. Set the env var to '0' to A/B back.
        self._track_h_cap = _flag('ORION_READER_TRACK_H_CAP', '1')
        self._track_h_cap_tol = min(0.50, max(0.02, _fnum('ORION_READER_TRACK_H_CAP_TOL', 0.06)))
        self._track_h_cap_m = max(4, _inum('ORION_READER_TRACK_H_CAP_M', 12))
        # ------------------------------------------------------------------ #
        #  SUBPIX-EDGE (ORION_READER_SUBPIX_EDGE) -- replace the whole-pixel fill-boundary
        #  refinement with a 50%-COVERAGE interpolation of the colour ramp across the edge.
        #  The shipped refinement thresholds a BLURRED copy of the red MASK row-mean, which
        #  pins 41% of frames at exactly -1.0px (the clamp) and carries almost no sub-pixel
        #  information; _subpix_edge instead projects the row-mean colour onto the local
        #  background->fill axis and interpolates the true coverage ramp.
        #  DEFAULT OFF, and the offline evidence says leave it off: on
        #  session_20260804_032333 it lowers the plateau read-noise sd from 0.474pp (2.56ms)
        #  to 0.432pp (2.33ms) -- a 0.23ms gain -- but MOVES THE MEAN FILL by -0.35pp
        #  (-1.88ms). The release lead is hand-calibrated against the shipped mean, so
        #  enabling this without a matching -1.9ms lead re-calibration is a net DETUNE: the
        #  bias it introduces is 8x the noise it removes.
        # ------------------------------------------------------------------ #
        self._subpix_edge = _flag('ORION_READER_SUBPIX_EDGE', '0')
        self._stalebreak = _flag('ORION_READER_STALEBREAK', '1')     # FLIPPED default-ON 2026-07-18 (A/B proven; 46/46 gates)
        self._stale_rise_frames = max(1, _inum('ORION_READER_STALE_RISE_FRAMES', 30))
        self._scale_guard = _flag('ORION_READER_SCALE_GUARD', '1')   # FLIPPED default-ON 2026-07-18 (A/B proven; 46/46 gates)
        self._scale_guard_decay_s = max(0.5, _fnum('ORION_READER_SCALE_GUARD_DECAY_S', 5.0))
        self._occl_wide = _flag('ORION_READER_OCCL_WIDE', '1')       # FLIPPED default-ON 2026-07-19: the A2xB1 mid_rise seam is laundered by _held_run (a1f10ea) and the decor steal path is closed by STEAL_PROBATION -- full 5-session sweep green (falselock 0, mid_rise 0, ghosts 0)
        self._occl_wide_ms = max(0.0, _fnum('ORION_READER_OCCL_WIDE_MS', 900.0))
        # ------------------------------------------------------------------ #
        #  A2 coast-steal hardening (2026-07-19, the ghost-mid-shot fix; both
        #  FLIPPED default-ON 2026-07-19 after the full sweep -- ghosts 2->0 on
        #  session_20260706_190737 and 0 on all five suite sessions, falselock 0,
        #  mid_rise 0; flag-off replay stays byte-identical to pre-fix HEAD):
        #    ORION_READER_STEAL_PROBATION (fix 1d): a steal reseat with NO live
        #      evidence (not hw-armed, no fresh rising red inside the A2
        #      freshness window, no shot-coast grace) arms an N4-style
        #      rise-probation on the reseated lock with a SHORT window
        #      (_FRAMES=4 < the falselock metric's 5-frame MIN_CORE) -- a
        #      non-rising decor/arm steal must prove a rise (or show green)
        #      within 4 read frames or be dropped, so A2's band-wide reseat can
        #      no longer latch static decor (the open item that pinned B1 off).
        #      hw / fresh-rise / grace steals stay EXEMPT (frame-1 pass) so
        #      A2's supersession of a stale dead-hold is preserved.
        #    ORION_READER_SILENT_RESEAT (ghost fix): a steal whose stolen
        #      column reads a fill CONTINUOUS with the coasted hold (no >20pp
        #      backward step -- the mid_rise_glitches boundary) is the SAME
        #      meter re-found after a pan/occlusion; emit the held fill/box as
        #      ONE detected:True "held_reseat" frame instead of the
        #      detected:False launder, so a same-meter reseat never blinks.
        #      (Position is deliberately NOT the test: the measured same-meter
        #      reseat on session_20260706_190737 o=734 jumped ~150px under a
        #      camera pan while the fill moved 92.7->91.6.) The detected:False
        #      launder stays ONLY for a genuinely discontinuous >20pp
        #      backward-step reseat -- and it now seeds last_fill/last_tbox
        #      from the stolen column so the NEXT coast frame can never emit
        #      the detected:True zero-box ghost (the second ghost frame).
        # ------------------------------------------------------------------ #
        self._steal_probation = _flag('ORION_READER_STEAL_PROBATION', '1')
        self._steal_prob_frames = max(1, _inum('ORION_READER_STEAL_PROBATION_FRAMES', 4))
        self._silent_reseat = _flag('ORION_READER_SILENT_RESEAT', '1')
        # ------------------------------------------------------------------ #
        #  A2c STEAL-COURTWIDE (2026-08-08, the beyond-half-court Go-To fix).
        #  LIVE EVIDENCE (logs/orion_native.log, owner rig 2026-08-08): epoch=27
        #  21:30:54.537Z stick_up_edge press -> "SHOT NOT OWNED:
        #  reason=ownership_proof_incomplete mode=2 samples=0 first_fill=97.2
        #  last_fill=97.2 wait_ms=2291.4". The previous shot's SPENT meter (epoch=26
        #  landed 0.7s earlier at ~97 fill) held the lock; when it faded, the ARMED
        #  coast (cap 180 frames) served the frozen 97.2 echo for the entire pending
        #  window, and the coast-steal -- the ONE mechanism designed to supersede a
        #  stale hold with fresh evidence -- scans ONLY the nominal band at the
        #  armed acquire floor (10px here). A beyond-half-court meter is h=4-9
        #  (this session's courtwide/pop-in acquires: h=4,5,6,7,8,9), i.e.
        #  STRUCTURALLY INVISIBLE to the steal scan, so the far meter could never
        #  supersede the frozen echo and the shot died unowned. The very next press
        #  (epoch=28, reader by then cold) seated the SAME far meter via
        #  "courtwide:strict_red_first h=6" within one frame of its render and
        #  released green -- proving the courtwide tiers catch it whenever they are
        #  allowed to run. Close-court back-to-back shots never hit this because
        #  their h>=10 in-band meter IS visible to the nominal steal.
        #  FIX: when the nominal steal scan misses AND the shot is PHYSICALLY armed
        #  (hw -- same authority gate as the cold courtwide path), retry the steal
        #  with _acquire_courtwide_structure: the identical strict red+green
        #  structure proofs, tier ladder, B7 prior-presence veto and B8 border veto
        #  the cold acquire already applies. This is NOT a new acceptance surface:
        #  it grants a LOCKED-but-coasting frame exactly what the same frame would
        #  get had the lock already dropped. Bounded per-shot (hw windows only),
        #  nominal steal still wins first, and off-shot/unarmed steals are
        #  byte-identical. Kill switch: "0".
        # ------------------------------------------------------------------ #
        self._steal_courtwide = _flag('ORION_READER_STEAL_COURTWIDE', '1')
        self._steal_cw_log_epoch = 0      # forensics dedup: first search per epoch
        self._steal_cw_log_last = 0.0     # + rate floor so the 1/s relay throttle keeps it
        self._vzoom = _flag('ORION_READER_VZOOM', '1')               # FLIPPED default-ON 2026-07-18 (A/B proven; 46/46 gates)
        self._vzoom_max_px = max(0, _inum('ORION_READER_VZOOM_MAX_PX', 260))
        self._vzoom_margin = max(4, _inum('ORION_READER_VZOOM_MARGIN_PX', 40))
        self._vzoom_ref_frames = max(0, _inum('ORION_READER_VZOOM_REF_FRAMES', 90))
        self._vzoom_down = _flag('ORION_READER_VZOOM_DOWN', '1')     # 1a: FLIPPED default-ON 2026-07-18 (A/B proven: dive fresh-red 83->93, green-hold pins 10->0; 46/46 gates both configs)
        self._vzoom_down_max_px = max(0, _inum('ORION_READER_VZOOM_DOWN_MAX_PX', 260))
        # ------------------------------------------------------------------ #
        #  PHANTOM-LOCK bundle (2026-07-24). LIVE EVIDENCE: the overlay read
        #  "FILL 87% RISING" on the ESRB legal screen and "FILL 5% RISING" in a
        #  city plaza -- frames with NO meter anywhere -- while the engine
        #  presence tally read idle_overlay 109 / stale 84 / accepted 30 and
        #  `static_fake_lock` fired ZERO times for the whole session (ARMED
        #  23:18:06 -> DISARMED 23:19:00 = 54 s continuously armed with zero
        #  shots). Four COUPLED defects made a decor lock immortal; every fix
        #  below is a TIGHTENING (it can only ever REJECT evidence), so a real
        #  meter's path is unchanged:
        #    ORION_READER_ARM_GUARD  (B2) the fake-lock breaker's grace may only
        #      be opened by a PHYSICAL arm (or a genuinely FRESH rising red read
        #      inside the A2 freshness window) -- never by the blanket merged
        #      `armed`. The merged gate is refreshed by the orchestrator's CV
        #      self-arm on `detected && rising`, so a phantom that reports
        #      "rising" re-armed the gate every frame, the armed frame counted as
        #      breaker grace, and the grace zeroed `_static_fill_n` forever: the
        #      phantom DISABLED ITS OWN KILLER (positive feedback).
        #    ORION_READER_VAR_BREAK  (B3) the breaker counted only BYTE-frozen
        #      fills (|dfill| < 0.05). On a live 60fps H.264 feed a decor blob's
        #      fill jitters more than that every frame, so the counter was
        #      perpetually reset and a MOVING false lock could never trip it.
        #      Count instead on NON-PHYSICAL fill behaviour over a rolling
        #      window: bounded variance (no net progress) OR two-sided
        #      oscillation (a real rising meter never oscillates down; a real
        #      deflate never oscillates up).
        #    ORION_READER_RELOCK_GEOM (B4) `_relocate` re-finds the lock with far
        #      weaker gates than a cold acquire (h_hold=12 / ar>=0.35 vs h_acq=33
        #      / ar>=1.8) and the accept path resets conf to CONF_INIT EVERY
        #      frame, so confidence decay was structurally UNREACHABLE and the
        #      lock immortal. A relocate hit whose COLUMN WIDTH is inconsistent
        #      with the acquiring lock's width is no longer evidence: it is
        #      dropped to the ordinary miss path, where the existing decay /
        #      armed-hold / coast-steal machinery applies unchanged.
        #    ORION_READER_FRESH_RISE (B5) A2 gated stale "rising" on the COAST /
        #      meter_memory return only; a phantom that `_relocate` re-finds every
        #      frame is a FRESH read and kept advertising "rising" (+ its
        #      velocity) straight into the CV self-arm (confirmed: meter_memory /
        #      steal_reseat occurred 0 times last session -- the phantom rode the
        #      fresh path exclusively). Require N genuinely CONSECUTIVE rising
        #      fresh samples before a fresh read may claim "rising".
        #    ORION_READER_SHOT_EPOCH (B6) each shot is an EPOCH: on the PHYSICAL
        #      arm edge the per-shot tracking state is cleared and a cold
        #      re-acquire forced; on the physical disarm edge the lock is dropped
        #      so no frozen tail straddles the shot boundary. Rides the HARDWARE
        #      arm edges ONLY (the same edges the ColorCalibrator lifecycle uses)
        #      -- a CV self-arm is exactly the signal a phantom forges, so it can
        #      never open or close an epoch.
        # ------------------------------------------------------------------ #
        self._arm_guard = _flag('ORION_READER_ARM_GUARD', '1')
        self._var_break = _flag('ORION_READER_VAR_BREAK', '1')
        self._static_win = max(4, _inum('ORION_READER_STATIC_WIN', 20))
        self._static_std_pp = max(0.05, _fnum('ORION_READER_STATIC_STD_PP', 1.5))
        self._static_osc_min = max(1, _inum('ORION_READER_STATIC_OSC_MIN', 3))
        self._static_osc_pp = max(0.05, _fnum('ORION_READER_STATIC_OSC_PP', 1.0))
        self._relock_geom = _flag('ORION_READER_RELOCK_GEOM', '1')
        self._relock_w_tol = max(0.05, _fnum('ORION_READER_RELOCK_W_TOL', 0.40))
        self._fresh_rise_gate = _flag('ORION_READER_FRESH_RISE', '1')
        self._fresh_up_min = max(1, _inum('ORION_READER_FRESH_RISE_STEPS', 3))
        # [ORION_READER_FRESH_RISE_STEPS_ARMED] P2 acquisition-speed knob (default = the base
        # value -> byte-identical). While the PHYSICAL shot gate (`_shot_armed_hw`) vouches
        # that a shot is genuinely in progress, the N-consecutive-rising-frames proof may use
        # this count instead of `_fresh_up_min` at its four consumers (_can_bridge_arm_epoch,
        # the monotonic_rise structure proof in _qualify_gameplay_sample, the arm-bridge occl
        # seed, and the B5 rise gate -- the last is already hw-exempt). At 2 it saves 1-2
        # frames (17-33ms) of the measured 21-36%%-fill late first sight; the engine's own
        # 3-frame strict ownership proof (AutomationEngine, ownership_proof_two_frame OFF)
        # is UNTOUCHED and remains the fail-closed release guard. Off-shot/unarmed frames
        # always use the base 3-step rule, so the CV self-arm / phantom machinery sees no
        # change. Quantified offline before arming: see
        # tools/regression/measure_reader_acquisition.py.
        self._fresh_up_min_armed = max(1, _inum(
            'ORION_READER_FRESH_RISE_STEPS_ARMED', self._fresh_up_min))
        self._fresh_up_pp = max(0.0, _fnum('ORION_READER_FRESH_RISE_PP', 0.25))
        self._shot_epoch = _flag('ORION_READER_SHOT_EPOCH', '1')
        # A physical arm edge used to clear an already-good meter lock unconditionally.  On the
        # live input path the meter can be visible (and rising) a few frames before the native arm
        # notification reaches the reader; clearing it at that edge creates the exact
        # visible -> blank -> re-lock sequence the epoch was meant to prevent.  Preserve only a
        # tightly-qualified lock: consecutive fresh red reads, a measured full-track history,
        # full confidence, and no coast frame.  Anything stale/held/phantom still takes the hard
        # epoch reset below.
        self._epoch_bridge = _flag('ORION_READER_EPOCH_BRIDGE', '1')
        self._epoch_bridge_frames = max(2, _inum('ORION_READER_EPOCH_BRIDGE_FRAMES', 3))
        self._epoch_bridge_ms = max(20.0, _fnum('ORION_READER_EPOCH_BRIDGE_MS', 120.0))
        # During a PHYSICAL shot, bbox dimensions are an identity, not a per-frame contour
        # measurement.  Latch w/h once and let only centre/floor translate; this removes H.264
        # contour/apex breathing without changing the pixels used for fill or the search anchor.
        # The next physical arm edge (or a real lock drop) starts a new shape epoch.
        self._shot_shape_lock = _flag('ORION_READER_SHOT_SHAPE_LOCK', '1')
        # N7 EARLY-STUB: one extra COLD nominal-band acquire pass that trades the loose
        # tip-pixel count for the STRICT connected-tip proof in exchange for a 4px stub
        # floor.  It runs only after every shipped cold path has already missed, so it can
        # add acceptance but never remove or reorder any.
        self._early_stub = _flag('ORION_READER_EARLY_STUB', '1')
        # A physically-armed coast steal has already proved that a shot is in progress.  Its
        # first discontinuous fresh read must stay sampler-stale, but need not blank the overlay.
        self._hw_reseat_continuity = _flag('ORION_READER_HW_RESEAT_CONTINUITY', '1')
        # ------------------------------------------------------------------ #
        #  [ORION_READER_POST_RELEASE_YIELD] P1 post-release lock yield (default OFF ->
        #  byte-identical). Measured on back-to-back attempts (2026-08-06 diagnosis): after
        #  OUR OWN release the previous shot's meter freezes near-full, and when it retracts
        #  the reader COASTS the frozen echo as meter_memory for ~17 frames (fed=0), then a
        #  coast-steal + a 2-frame held_reseat launder + re-verify -- the reader saw the new
        #  rise at fill 17.8 while the engine's first fed sample was 35.95 (~100ms/18pp of
        #  the decision budget gone before the engine sees anything). The release seq is
        #  already relayed to the reader (notify_release), so the spent-meter provenance is
        #  KNOWN, not inferred:
        #    (a) once the released shot's fill has been static (<=0.75pp step, well under the
        #        ~3pp/frame of a real near-tip rise) at/above _pr_yield_fill_min for
        #        _pr_yield_frames emissions, the FIRST would-be coast frame force-unlocks to
        #        hunting (the canonical conf<CONF_MIN lock-drop path) instead of coasting the
        #        echo -- the next frame runs the ordinary armed cold acquire on the new rise.
        #    (b) an hw-vouched coast-steal FROM such a post-release-frozen lock serves the
        #        stolen column's FRESH read immediately instead of the held-fill display
        #        launder, and skips the reverify de-authorization (the identity that lost the
        #        lock was our own spent meter, not decor; the engine's 3-frame strict
        #        ownership proof remains the release guard, unchanged).
        #  The precondition (own release seen + static >=90) cannot occur mid-rise, and the
        #  pending latch is cleared on every physical arm edge / explicit shot start, so a
        #  live rising meter can never be yielded.
        # ------------------------------------------------------------------ #
        self._pr_yield = _flag('ORION_READER_POST_RELEASE_YIELD', '0')
        self._pr_yield_fill_min = min(100.0, max(50.0, _fnum(
            'ORION_READER_POST_RELEASE_FILL_MIN', 90.0)))
        self._pr_yield_frames = max(2, _inum('ORION_READER_POST_RELEASE_FREEZE_N', 12))
        self._pr_release_pending = False   # notify_release -> True; cleared on the next arm edge
        self._pr_frozen_n = 0              # consecutive static >=fill_min emissions while pending
        self._pr_prev_fill = None          # previous emitted fill for the static test (P1 only)
        self._pr_yield_n = 0               # observability: force-unlocks fired this session
        # ------------------------------------------------------------------ #
        #  RELEASE ORACLE (ORION_RELEASE_ORACLE, default ON) -- DIAGNOSTIC ONLY.
        #
        #  [2026-09-15] Measured on session_20260915_185359 (29 releases, 27 gradable):
        #  ~35-50 ms after the bar tops out 2K27 RETRACTS the white fill by a few pixels
        #  and HOLDS it there, and that settled gap between the white fill's top and the
        #  green band's BOTTOM separates the game's own banner verdict perfectly --
        #  EXCELLENT <= 3 px, LATE/EARLY >= 4 px (settled_fill EXCELLENT 90.06 +- 0.32
        #  vs LATE 85.47 +- 2.79). It is the only per-shot signal the reader can measure
        #  that agrees with the banner without reading the banner, so it is worth logging
        #  on every shot.
        #
        #  IT IS NOT A TIMING INPUT AND MUST NEVER BECOME ONE. It is measured 300-500 ms
        #  AFTER the release command, i.e. long after every decision this reader feeds;
        #  nothing here touches a fill, a box, a window or a gate. The whole feature is
        #  one measurement taken from numbers _read_fill already computed, plus one ERROR
        #  line per release and one append-only key on the release diagnostic record.
        # ------------------------------------------------------------------ #
        self._ro_on = _flag('ORION_RELEASE_ORACLE', '1')
        self._ro_lo_s = max(0.0, _fnum('ORION_RELEASE_ORACLE_LO_MS', 300.0)) / 1000.0
        self._ro_hi_s = max(self._ro_lo_s, _fnum(
            'ORION_RELEASE_ORACLE_HI_MS', 500.0) / 1000.0)
        self._ro_thr_px = max(0.0, _fnum('ORION_RELEASE_ORACLE_GAP_PX', 3.5))
        self._ro_pending = False       # a release is open and its settle window is filling
        self._ro_seq = 0               # the native release id the window belongs to
        self._ro_epoch = 0             # the SHOT-GATE epoch (machine line's release_seq)
        self._ro_t0 = None             # ts of the first frame seen after the command
        self._ro_samples = []          # (dt_s, gap_px|None, gap_pct|None, fill, g_bot_pct|None)
        self._ro_frame = None          # per-frame handoff out of _read_fill
        self.last_release_oracle = None  # the last emitted record (tests + the sidecar read it)
        # B6 sub-flag: also end the fast per-shot CALIBRATION (width baseline / vertical-zoom
        # reference / dim rebase) at the arm edge, via reset_session_scale(). Separately
        # switchable because it is the one part of the epoch the offline gates cannot exercise
        # (they never set the HARDWARE arm) and it briefly clears `_vz_ref` at the exact frame
        # the arm-dive starts -- set to 0 to keep the epoch's lock reset without it.
        self._shot_epoch_scale = _flag('ORION_READER_SHOT_EPOCH_SCALE', '1')
        # ------------------------------------------------------------------ #
        #  FIX-2 "snappy" (ORION_READER_BOX_PREDICT, default OFF -> byte-identical):
        #  on green-hold / fill-gated frames the EMITTED box is the FROZEN last_tbox,
        #  so on a fast fade slide the drawn box TRAILS the moving meter. Forward-
        #  predict the REPORTED box along the box velocity -- ONLY on frames whose
        #  velocity is FRESH (the relocate found the meter's colour THIS frame:
        #  green_hold / fill_gated), never on coast frames. Coast extension was
        #  MEASURED and REJECTED (framedump A/B 2026-07-19, sessions 231912+190737):
        #  the coast velocity is frozen pre-vanish and the shipped occl shift already
        #  leads the first 6 coast frames with decay -- extending it moved the box
        #  AWAY from the reappear position on 32/47 changed frames (held_box_trail
        #  mean 111->117 / 153->156 px), because live pans decelerate/reverse.
        #    * per-AXIS deadband (|v| < ~2 px/frame -> that axis stays byte-frozen,
        #      so a stationary meter never jitters/overshoots -- incl. the phantom
        #      upward centre-y drift of a static rising meter),
        #    * per-run decay + a TOTAL-shift cap (a long hold settles, never runs),
        #    * clamped into _band_eff() (the drawn box can never walk onto decor),
        #    * x/y shift ONLY -- w/h fixed (never a tip clip / shrink),
        #    * KILL-ON-DISARM: prediction only runs while the shot-gate is armed and
        #      the accumulator dies the moment it disarms -- a prediction is never
        #      ridden into post-release.
        #  REPORTED-box only: last_tbox itself is never mutated here, so the search
        #  anchor's own dead-reckon (self.box / the occl coast shift) is untouched
        #  and can never double-apply.
        # ------------------------------------------------------------------ #
        self._box_predict = _flag('ORION_READER_BOX_PREDICT', '0')
        self._bp_deadband = max(0.0, _fnum('ORION_READER_BOX_PREDICT_DEADBAND', 2.0))
        self._bp_cap_px = max(0, _inum('ORION_READER_BOX_PREDICT_CAP_PX', 40))
        self._bp_decay = min(1.0, max(0.5, _fnum('ORION_READER_BOX_PREDICT_DECAY', 0.9)))
        # ------------------------------------------------------------------ #
        #  BOX-TIGHT (ORION_READER_BOX_TIGHT, default 0 -> byte-identical):
        #  the DRAWN box hugs the METER instead of the presentation envelope. The
        #  served box is the FULL-STRUCTURE envelope (full-track height + tip lift +
        #  cap/apex union up to ~1.8x the red width + the outline hug's stroke pad on
        #  all four sides + the per-shot shape latch) -- measured live 2026-08-06 as a
        #  magenta box 1.5-2x wider than the bar with margin both sides, extending
        #  above/below it, and riding higher/lower than the bar (the latched shape).
        #  MODES (int):
        #    1 = BAR-HUG: the outgoing bbox becomes the raw detected red-column bbox
        #        on every fresh red frame (grows/shrinks with the fill).
        #    2 = REFERENCE-HUG (owner reference: Screenshot 2026-06-29 193423,
        #        measured 2026-08-06): the drawn rect reproduces the reference
        #        screenshot's stroke-inner rect. Width = 2.25x the bar body
        #        (reference: box inner 63px vs bar 28px -> 0.625*cw of margin per
        #        side, floored to clear a wider measured housing stroke); height =
        #        the LIVE cap-to-chevron track extent plus 4.5% of that extent of
        #        air per side (reference: 7-8px over a 158px housing), so BOTH
        #        arrow caps sit inside with visible clearance. 5-sample-median size
        #        smoothing and a NEVER-SHRINK clamp so the drawn box always contains
        #        the current red bar. Everything derives from per-frame measured
        #        geometry (ratios, not px), so the box scales with the on-screen
        #        meter (dynamic).
        #  Both modes transform the OUTGOING bbox at the detect() boundary ONLY, and
        #  translate the tight shape along the served box's own glue/predict motion on
        #  hold/coast frames. DISPLAY-ONLY: last_tbox / _pre_hug_tbox / self.box /
        #  masks / fill and every internal consumer (shape latch, VZOOM ref, scale
        #  EMA, bridge gate) are untouched, so tracking's anti-collapse floors keep
        #  protecting the SEARCH state without inflating what is drawn.
        # ------------------------------------------------------------------ #
        self._box_tight = max(0, _inum('ORION_READER_BOX_TIGHT', 0))
        # Mode-2 REFERENCE margins (owner screenshot 2026-06-29 193423, pixel-measured
        # 2026-08-06): rect width = 2.25x the bar body (stroke-inner 63px / bar-body
        # 28px => 0.625*cw of margin per side) and 4.5% of the cap-to-chevron track
        # extent of air above the cap apex / below the chevron tip (7-8px over the
        # reference's 158px housing). FRACTIONS of per-frame measurements -- never px
        # -- so the drawn box scales with the on-screen meter. Env-overridable so the
        # owner can tune the look live; display-only either way.
        self._tight_side_frac = min(2.0, max(0.0, _fnum(
            'ORION_READER_BOX_TIGHT_SIDE_FRAC', 0.625)))
        self._tight_vpad_frac = min(0.5, max(0.0, _fnum(
            'ORION_READER_BOX_TIGHT_VPAD_FRAC', 0.045)))
        # DISPLAY VERTICAL HUG CLAMP (2026-08-31, session_20260831_114137). Max px
        # the DRAWN box may extend beyond the served box, above the top and below the
        # base. The served box wraps the meter cap->chevron on 100% of frames (its
        # height never moves >6px), but the mode-2 pre-latch `pre` union and the
        # translate-branch top_row raise intermittently ran the drawn top 44-66px
        # above it onto the wall seam behind the meter (and, less often, the base
        # 24-32px below it, or floated the whole box off the meter on a stale
        # translate offset), toggling the drawn height 120<->176 on 9.3% of detected
        # frames -- the "flicker" the owner reports. The correct reference-hug sits
        # 0-15px past each served edge (bimodal, gap 15-30), so 18 is inert on every
        # correct frame and pulls only the over-reach / float-off back to a hug.
        # Env-tunable; a negative TOP value disables the whole clamp. The clamp only
        # trusts the served box as the meter when the served box is itself
        # meter-plausibly tall (>= HUG_MIN_FRAC of the frame height, ~40px @720p);
        # below that the served box is a bar-only stub and mode-2's design of
        # UNIONING a taller reconstructed track is left intact, never clipped. In
        # practice detect() passes the STABILIZED full-meter box (measured ~106px,
        # min 104 on session_20260831_114137), so the gate is live on every real
        # frame and only spares the synthetic short-stub case.
        self._tight_top_reach = _inum('ORION_READER_BOX_TIGHT_TOP_REACH', 18)
        self._tight_bot_reach = _inum('ORION_READER_BOX_TIGHT_BOT_REACH', 18)
        # Horizontal twin of the vertical hug clamp.  A fresh colour-path source
        # can disagree with the detector-authoritative full-meter box (for example,
        # a red court/decor column while detector_fill remains locked on the real
        # meter).  Without an x-axis bound that unrelated source can move the drawn
        # box hundreds of pixels away even though fill/timing stayed healthy.  Keep
        # the reference air when it is local, but never let presentation abandon the
        # served meter span.  Negative disables only the horizontal half; the legacy
        # negative TOP_REACH still disables the entire hug clamp below.
        self._tight_side_reach = _inum('ORION_READER_BOX_TIGHT_SIDE_REACH', 18)
        self._tight_hug_min_frac = min(0.5, max(0.0, _fnum(
            'ORION_READER_BOX_TIGHT_HUG_MIN_FRAC', 0.055)))
        # ------------------------------------------------------------------ #
        #  ORION_READER_PERF: the two OUTPUT-IDENTICAL latency sheds (S1+S2,
        #  spec docs/ORION_PERF_AND_CROSSING_SPEC.md Part A). The reader's
        #  read_ms median is dominated by DEAD off-shot work, not the armed path:
        #    S1 empty-band early-exit -- in _scan, when the raw red mask is so
        #      sparse that NO contour could pass the size x red_frac gates even
        #      if the CLOSE kernel dilated every raw pixel maximally, skip the
        #      morphology + findContours entirely. Mathematically exact:
        #      close(raw) is a subset of dilate(raw) and the 5x3 all-ones kernel
        #      bounds countNonZero(dilate(raw)) <= 15*countNonZero(raw), so
        #      15*raw_px < red_frac_min*w_min*hmin proves every candidate bbox
        #      fails the red_frac gate -> (None, 0.0), exactly the full path's
        #      return on such a frame.
        #    S2 band-mask reuse -- the cold-miss path computes the SAME full-band
        #      inRange twice (byte-identical: _scan's default-band scan, then
        #      _acquire_structure on the identical region + resolved bounds).
        #      Memoize the raw mask per frame (cleared at the top of read()) and
        #      reuse it only on an EXACT (region, bounds) key match.
        #  Both are pure latency sheds -- zero behaviour change (frame-exact
        #  A/B-verified on the framedump replays). Flag OFF -> neither runs.
        # ------------------------------------------------------------------ #
        self._perf = _flag('ORION_READER_PERF', '1')  # FLIPPED default-ON 2026-07-18: frame-exact identical to OFF on 3813+5999 replay frames (0 divergent rows), lat median 2.69->1.22 / 2.77->0.87ms, 46/46 gates
        self._scan_raw_key = None     # S2 per-frame memo key: (x0,y0,x1,y1,lo,hi)
        self._scan_raw = None         # ...the raw inRange mask for that key
        self._perf_early_exits = 0    # S1 observability (tests / diagnostics)
        self._perf_mask_reuses = 0    # S2 observability (tests / diagnostics)
        # A3 session state: ts of the last GUARD-qualified scale sample (None = never).
        self._scale_last_ts = None
        # B2 session state (persists across lock drops so the RE-ACQUIRE band shifts too):
        # (top_y, floor_y) of the last confident red-locked track box. NOT an EMA offset from a
        # session baseline -- the meter's y legitimately varies shot-to-shot (player-attached),
        # so a baseline-offset design chased that variance and shifted the band off REAL shots
        # (first offline A/B). The band only ever shifts the MINIMUM needed to contain the last
        # locked box with a margin, so a meter comfortably inside the band never moves it; and
        # the shift only applies while LOCKED (following the live box) or for a bounded
        # re-acquire window after a drop (_vz_ref_age <= _vzoom_ref_frames) -- a STALE ref from
        # minutes ago must never displace the cold-acquire band off the next nominal shot
        # (second offline A/B caught exactly that: vy=-191 blocked a real rise acquire).
        self._vz_ref = None
        self._vz_ref_age = 0
        self._vy_est = 0.0            # observability: the applied band shift (px, +down)
        self._vy_down = 0.0           # observability: the applied 1a bottom widen (px, +down)
        # ------------------------------------------------------------------ #
        #  GREEN-ZONE stack (ceiling build, default OFF -> byte-identical):
        #    ORION_GREEN_ZONE_WINDOW=1 -> per shot, read the neon make-window band's
        #      (#1CFE1C) LOWER edge on un-occluded frames and EMIT the colour-derived
        #      green window [g_lo, 100] on the current frame's fill scale (port of
        #      tools/diagnostics/green_zone_grader.py). This IS the make target.
        #    ORION_GREEN_SELF_GRADE=1 -> legacy flag name for the detector-only release-window
        #      diagnostic: fill-at-release (or peak proxy) vs the colour window, exposed on
        #      self.last_green_grade + an optional sink + a JSONL diagnostic log.  This compares
        #      the release COMMAND to detector pixels; it is not an NBA 2K gameplay outcome.
        #  Both OFF (the default): no new work runs anywhere -- every call site is
        #  flag-guarded, so the shipped read is byte-identical.
        # ------------------------------------------------------------------ #
        self._gz_window = _flag('ORION_GREEN_ZONE_WINDOW', '0')
        self._gz_grade = _flag('ORION_GREEN_SELF_GRADE', '0')
        self._gz_expose_px = max(1, _inum('ORION_GREEN_ZONE_EXPOSE_PX', 3))
        self._gz_tol = max(0.0, _fnum('ORION_GREEN_ZONE_TOL', 2.0))
        self._gz_starts: list = []        # per-shot un-occluded neon lower-edge samples (fill-%)
        self._gz_fill_at_release = None   # fill on the real release frame (notify_release)
        self._gz_release_seq = 0          # immutable native release id for this physical shot
        self._gz_release_physical_epoch = 0
        self._gz_release_shot_attempt = 0
        self._gz_release_identity_verified = False
        self._gz_release_latched = False  # first release marker wins, including invalid/proxy ids
        self._gz_graded = False
        self._gz_sink = None              # optional callable(record: dict) -- telemetry wiring
        self._gz_log_path = None
        self.last_green_grade = None      # most recent per-shot grade record (calibration source)
        # optional explicit search-band override "x0,y0,x1,y1" (@1080p); empty = the scaled prior.
        self._band_override = None
        try:
            _bo = _os.environ.get('ORION_READER_BAND', '').strip()
            if _bo:
                self._band_override = tuple(int(float(v)) for v in _bo.split(','))[:4]
        except Exception:
            self._band_override = None
        # rolling scale estimate (persists across coasts within a session -- camera scale is a
        # session property, so it is NOT cleared by _reset_state). Self-referencing: the first
        # confident locks establish a per-session BASELINE full-track height; the estimate is the
        # ratio of the recent median to that baseline, so a nominal standstill reads exactly 1.0
        # (dormant, byte-identical) and only a real zoom/pan/tilt drives it off nominal.
        self._size_hist = deque(maxlen=10)
        self._size_base = 0.0                    # 0 = baseline not yet established this session
        self._scale_est = 1.0
        self._coast_n = 0                        # consecutive coast frames (bounds box forward-pred)
        # [ORION_WHITE_CAPLESS_BREAKER 2026-08-27] wall-clock start of the current
        # run of DETECTED frames that showed no green make-window cap. Wall clock,
        # not frames, so the bound is independent of detector rate.
        self._capless_since = None
        self._capless_hist = _collections.deque()
        # Latch the configured bar colour BEFORE anything that depends on it (the calibrator's
        # envelope, the colour-tolerance widening, the tier band table).
        self._resolve_colour()
        _cal_on = _os.environ.get('ORION_COLOR_CAL', '0').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        self._calibrator = calibrator if calibrator is not None else \
            (ColorCalibrator(color=self._meter_color) if _cal_on else None)
        # per-lock active colour band: None = the shipped defaults. The band that ACQUIRED the
        # lock is latched for the lock's whole life so relocate/read/fill never disagree.
        self._lock_red = None
        self._lock_green = None
        self._fit_provider = None                # callable(t_ms)->dict|None (tip_reg.predict_fill)
        # trajectory gate (B2) per-shot state
        self._traj_last_acc = None               # (ts, fill) of the last ACCEPTED sample
        self._traj_rejects = 0
        # dead-reckoned coast (B2)
        self._dr_frames = 0
        # scale rebase (B2): per-shot (w, track_h) samples from locked red frames; base once/session
        self._dims_samples = []
        self._dims_rebased = False
        self._meas_w_min = self._meas_w_max = None
        self._meas_h_max = None
        self._meas_g_area = None
        self._last_ncc_score = -1.0
        self._last_occl_frac = 0.0
        self._armed_now_cached = False
        # Acquire-forensics rate limiter (monotonic seconds of the last UNARMED seat line;
        # armed/hw/epoch-bearing seats always log -- they are ~1-3 lines per shot).
        self._acq_log_last = 0.0
        self._shot_release_seen = False
        self._learned_red = None                 # TAGGED band row baked by the calibrator, else None
        self._learned_green = None
        if self._calibrator is not None:
            self._apply_baked_bands()
        # Behavioural tunables: instance shadows of the class constants so every existing
        # code path (incl. _recompute_scale) picks the param value up unchanged. With the
        # default ReaderParams() the shadows equal the class constants exactly.
        p = params if params is not None else ReaderParams()
        self.params = p
        self.H_ACQ, self.H_ACQ_ARMED, self.H_HOLD = p.h_acq, p.h_acq_armed, p.h_hold
        self.AR_MIN, self.AR_MIN_ARMED = p.ar_min, p.ar_min_armed
        self.CONF_INIT, self.CONF_MIN = p.conf_init, p.conf_min
        self.CONF_RATE, self.CONF_RATE_ARMED = p.conf_rate, p.conf_rate_armed
        self.NCC_LOCK = p.ncc_lock
        self.G_AREA_MIN = p.g_area_min
        self.VEL_WIN = p.vel_win
        self._G = ((self._G[0][0], p.green_s_floor, p.green_v_floor), self._G[1])
        # (4) COLOUR TOLERANCE (default 0 -> byte-identical): additively widen the red/green
        # acquisition bands so a differently-white-balanced / dimmer capture still masks the
        # meter. Red widens the R floor DOWN + the G/B ceilings UP; green widens the hue span
        # and drops the S/V floors. Applied to the instance bands only when the toggle is set.
        # `self._meter_color` was latched earlier in __init__; the widening below needs it to
        # know whether the bar row is BGR (widenable per channel) or HSV (not).
        if self._color_tol > 0:
            t = float(self._color_tol)
            _c8 = lambda v: int(max(0, min(255, round(v))))
            # BAR widening is per-BGR-CHANNEL arithmetic and is therefore meaningful ONLY for a
            # BGR bar row. Applied to an ("hsv", h_lo, h_hi, s, v) row it would walk the hue
            # window sideways into a neighbouring colour -- for Purple, straight up into the pink
            # floatie at Hue~166 that the band exists to exclude. HSV colours express the same
            # "be more tolerant" idea through their relaxed courtwide/micro tiers instead.
            # Gated on Red BY NAME for the same reason as _rebuild_bands: the
            # widening below is red-shaped per-channel arithmetic applied to the
            # RED instance constants. White is a BGR row too, so the row-form
            # test alone would drag a white reader through red arithmetic on
            # constants its bands no longer come from. White widens through its
            # relaxed courtwide tier instead, exactly as the HSV colours do.
            if (self._meter_color == "Red"
                    and _mbc.supports_channel_widening(
                        _mbc.band(self._meter_color, _mbc.BAND_NOMINAL))):
                rl, rh = self._RED_LO, self._RED_HI
                self._RED_LO = (_c8(rl[0]), _c8(rl[1]), _c8(rl[2] - t))
                self._RED_HI = (_c8(rh[0] + t), _c8(rh[1] + t), _c8(rh[2]))
            # The GREEN make-window cap is colour-INVARIANT (it is the same green cap whatever
            # the bar colour), so its widening always applies.
            gl, gh = self._G
            self._G = ((_c8(gl[0] - t), _c8(gl[1] - t), _c8(gl[2] - t)),
                       (_c8(gh[0] + t), _c8(gh[1]), _c8(gh[2])))
        self._rebuild_bands()
        self._reset_state()
        if self.W and self.H:
            self._recompute_scale()

    # ------------------------------------------------------------------ #
    #  configured bar colour -> the three tagged tier bands
    # ------------------------------------------------------------------ #
    def _resolve_colour(self) -> str:
        """Latch `cfg.meter_color` onto a SUPPORTED colour name (Red on anything unknown)."""
        raw = getattr(self._cfg, "meter_color", None) if self._cfg is not None else None
        self._meter_color = _mbc.normalize(raw if raw else _mbc.FALLBACK)
        return self._meter_color

    def _rebuild_bands(self) -> None:
        """Resolve `cfg.meter_color` into `self._meter_color` + `self._bands`.

        Red's three rows are built from the LIVE instance constants (`_RED_LO`/`_RED_HI`,
        `_COURTWIDE_RED`, `_MICRO_RED`) rather than from the table, so:
          * ORION_READER_COLOR_TOL still widens the nominal row exactly as it always did, and
          * a Red reader provably reaches the identical cv2.inRange constants it ships with.
        Any other supported colour takes its rows from meter_bar_colors. Unsupported/unknown
        names fall back to Red with a warning (see meter_bar_colors.normalize) -- the reader must
        keep reading on a mislabeled config rather than go blind.
        """
        colour = self._resolve_colour()
        if colour == "Red":
            # Red ONLY: mirror the instance constants verbatim.
            #
            # This MUST key on the colour name, not on `is_bgr(row)`. White is
            # also a BGR row, and under the old form-test a White-configured
            # reader took this branch and silently scanned with _RED_LO/_RED_HI --
            # a red mask on a white meter, i.e. zero detections and no
            # explanation. The byte-identity guarantee this branch exists for is
            # a claim about RED specifically, so name Red specifically.
            self._bands = {
                _mbc.BAND_NOMINAL:   ("bgr", tuple(self._RED_LO), tuple(self._RED_HI)),
                _mbc.BAND_COURTWIDE: ("bgr", tuple(self._COURTWIDE_RED[0]),
                                      tuple(self._COURTWIDE_RED[1])),
                _mbc.BAND_MICRO:     ("bgr", tuple(self._MICRO_RED[0]),
                                      tuple(self._MICRO_RED[1])),
            }
        else:
            self._bands = {t: _mbc.band(colour, t) for t in
                           (_mbc.BAND_NOMINAL, _mbc.BAND_COURTWIDE, _mbc.BAND_MICRO)}

    @staticmethod
    def _merge_bands(a, b):
        """The UNION of two same-kind tagged bands (relaxed tier OR strict tier).

        BGR: per-channel min/max -- byte-identical to the inline zip(min)/zip(max) this replaced.
        HSV: widest hue window and the LOWER of each floor. Mixed kinds cannot occur (both rows
        always come from the same colour's tier table) but fall back to `a` rather than produce
        an ill-typed row.
        """
        if a[0] != b[0]:
            return a
        if a[0] == "bgr":
            return ("bgr",
                    tuple(min(x, y) for x, y in zip(a[1], b[1])),
                    tuple(max(x, y) for x, y in zip(a[2], b[2])))
        return ("hsv", min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3]), min(a[4], b[4]))

    # ------------------------------------------------------------------ #
    #  tracking state
    # ------------------------------------------------------------------ #
    def _reset_state(self):
        self.conf = 0.0
        self.box = None            # last red-column bbox (x,y,w,h) full-frame
        # True only for a lock acquired by the physically-armed, court-wide
        # structural fallback.  It changes only the CLIP bounds for the already
        # tight relocate window; it never turns normal tracking into a global scan.
        self._courtwide_lock = False
        self._courtwide_acquire_tier = ""
        # A sub-quarter-scale meter is too small to trust from one rasterized
        # frame.  The physically-armed court-wide micro tier therefore keeps one
        # *candidate* only, keyed to the current hardware-shot epoch and capture
        # timestamp.  It is presentation/timing inert until a second unique,
        # geometrically-consistent frame corroborates it.
        self._micro_pending_col = None
        self._micro_pending_ts = None
        self._micro_pending_epoch = 0
        self._micro_pending_tier = ""
        self._micro_pending_count = 0
        # A new lock must independently prove meter structure or temporal rise.  Never let a
        # previously-authorized logo/box transfer authority across a drop, style change, or shot.
        if hasattr(self, "_gameplay_lock_authorized"):
            self._gameplay_lock_authorized = False
            self._gameplay_verify_frames = 0
            self._gameplay_reverify_visible = False
        # [ORION_READER_POST_RELEASE_YIELD] the frozen streak describes the tracked lock;
        # it dies with the state (the pending release latch is a timeline fact and survives).
        if hasattr(self, "_pr_frozen_n"):
            self._pr_frozen_n = 0
            self._pr_prev_fill = None
        self.tmpl = None           # last meter GRAYSCALE crop (matchTemplate fast-path template)
        self.tmpl_wh = None
        self._fillable_hist = []   # rolling green-cap fillable heights (fallback anchor)
        self._track_h_hist = []    # rolling FULL-track heights (green-cap-top -> floor); a 5-sample
                                   #   median steadies the denominator + box height (jitter) and
                                   #   carries a green-absent frame -- WITHOUT lagging the fill (fill
                                   #   tracks red_top directly) or the box X (it follows meter motion)
        self._track_w_hist = []    # N3: rolling EMITTED box widths; a 5-sample median steadies the
                                   #   reported width (raw contour width still drives masks/scale)
        self.last_fill = 0.0       # last good reading (held during a coast, like the chain)
        self.last_coarse = 0.0
        self.last_tbox = [0, 0, 0, 0]
        # BOX-TIGHT display twins (attribute-only; the tracking state above never reads them):
        self._tight_src = None     # THIS frame's fresh red evidence: (col, top_row, pre-latch tbox,
                                   #   measured hug stroke) or None
        self._tight_off = None     # (dx, dy, w, h): tight-vs-served offsets from the last fresh frame
        self._tight_wh_hist = deque(maxlen=5)   # mode-2 SIZE smoothing (drawn w,h; display-only)
        self._hug_stroke = None    # per-frame measured outline stroke (dl, dr, pad) or None
        self._bvx = 0.0            # box-centre velocity px/frame (motion-follow)
        self._bvy = 0.0
        # FIX-2 (BOX_PREDICT): accumulated REPORTED-box forward-shift (px) + frames in the
        # current predicted run. Dead floats when the flag is off (never read).
        self._bp_dx = 0.0
        self._bp_dy = 0.0
        self._bp_n = 0
        self._last_green_hold = False        # observability (measure scripts / tests)
        self._tip_dbg = (-1, -1, -1)         # FIX-1 observability: (cap_abs, apex_abs, box_top)
        self._pre_hug_tbox = None            # BOX-HUG: the track box BEFORE the outline hug --
                                             #   what SEARCH state (VZOOM ref / dim rebase) reads,
                                             #   so the presentation margin never moves the band
        self._prev_cx = None
        self._prev_cy = None
        self._prev_floor_y = None  # N1: last red-column FLOOR y (cy+ch), fill-invariant _bvy anchor
        # N4 rise-probation per-lock state (refuse counter persists across drops -> __init__ only)
        self._prob_active = False
        self._prob_n = 0
        self._prob_start = None
        self._prob_prev = None
        self._prob_inc = 0
        self._prob_green = False
        self._vel_hist = deque(maxlen=self.VEL_WIN)   # (ts, fill) for wall-time velocity
        self._velocity = 0.0
        self._accel = 0.0
        self._prev_vel = 0.0
        self._prev_vel_ts = None
        self._consec = 0
        self._peak_fill = 0.0
        self._coast_n = 0
        self._static_fill_n = 0    # consecutive byte-identical reported fills (fake-lock breaker)
        self._rep_fill_prev = None  # last REPORTED fill (detect() boundary), for the breaker
        # B3 (VAR_BREAK): rolling window of REPORTED fills at the detect() boundary. The
        # byte-frozen test above cannot see a dÃ©cor blob whose fill JITTERS; this window is
        # what the variance / oscillation tests read. Dead deque when the flag is off.
        self._rep_fill_hist = deque(maxlen=self._static_win)
        # B4 (RELOCK_GEOM): the acquiring lock's red-column WIDTH (px, slow EMA over accepted
        # fresh red reads) + the consecutive count of relocate hits refused against it.
        self._lock_w_ref = 0.0
        self._relock_reject_n = 0
        # B5 (FRESH_RISE): consecutive strictly-increasing FRESH red samples, and the previous
        # fresh sample they are measured against. A real rise racks these up in 3 frames; a
        # phantom's two-sided jitter never does.
        self._fresh_up_n = 0
        self._fresh_prev_fill = None
        # Fresh-lock qualification for the physical arm-edge bridge.  Updated only after an
        # accepted, non-gated RED read; coast/green-hold/suspect reads reset the streak.
        self._fresh_lock_ts = None
        self._fresh_lock_streak = 0
        # Output-only physical-shot geometry latch.  It never changes the raw contour, fill strip,
        # template, or relocation box -- only the bbox served to overlay/native consumers.
        self._shot_shape = None
        self._shot_shape_live = False
        self._epoch_bridge_active = False
        self._rise_recent_n = 0    # N6 shot-coast: frames of grace remaining since the last RISING
                                   #   red read (legacy frame-count mode; latched to _shot_coast_max)
        self._grace_until_ts = 0.0  # NF-2: wall-time deadline (frame-ts seconds) of the shot-coast
                                    #   grace; latched to ts + _shot_coast_ms/1000 on a rising read
        # A1 (SCALE_RESET): refused-but-full-cap track-height candidates -- M consecutive
        # consistent ones prove the running max is a stale outlier and rebase the median down.
        self._low_cand_hist = []
        # A4 (TRACK_H_CAP): the HIGH-side twin -- candidates refused for being too TALL.
        self._high_cand_hist = []
        # A2 (STALEBREAK): frames of "rising" freshness left since the last FRESH rising RED read.
        self._fresh_rise_left = 0
        # [ORION_DEAD_HOLD_GRACE] Has THIS lock ever been observed genuinely rising? Unlike
        # `_fresh_rise_left` (a 30-frame countdown) this does not expire inside the lock, because
        # a legitimate release occlusion can hold for far longer than 30 frames. Cleared wherever
        # the lock identity is discarded, so it can never carry across a re-lock.
        self._lock_ever_rose = False
        # B1 (OCCL_WIDE): wall-time deadline of the widened in-shot occlusion coast window.
        self._occl_wide_until = 0.0
        self._occl_wide_live = False
        # B1 de-conflict (A2xB1): consecutive HELD-fill emissions (coast meter_memory +
        # green_hold frames). Unlike _coast_n it is NOT reset by a green_hold accept, so the
        # >20pp post-occlusion launder floor cannot be defeated by a degenerate green-hold
        # re-lock at the inter-shot seam. Only READ behind self._occl_wide (dead counter
        # when B1 is off -> shipped default set behaviour-identical).
        self._held_run = 0
        # Track B per-lock / per-shot state (guarded: subclass __init__ ordering)
        if hasattr(self, "_lock_red"):
            self._lock_red = None
            self._lock_green = None
            self._traj_last_acc = None
            self._traj_rejects = 0
            self._dr_frames = 0

    def reset_session_scale(self):
        """EPOCH-8: forget everything learned about the PREVIOUS meter's SIZE/POSITION.

        _reset_state() clears the per-lock tracking state, but the SESSION-scoped calibration --
        the width baseline (_size_hist/_size_base/_scale_est), the vertical-zoom reference
        (_vz_ref), and the once-per-session measured dimension rebase (_dims_*/_meas_*) -- survived
        it. That is correct for a lock DROP (the meter went away and will come back the same size:
        the drop path at the confidence-decay branch deliberately preserves it), but WRONG for a
        colour/style CHANGE, where the next meter is a DIFFERENT meter. Carrying the old baseline
        divides the NEW meter's width by the OLD meter's baseline, so _scale_est lands off nominal
        from the very first sample and every acquire/track gate is mis-widened for the rest of the
        session. Each shot should be an EPOCH: this is where the previous epoch's calibration ends.
        """
        self._size_hist.clear()
        self._size_base = 0.0
        self._scale_est = 1.0
        self._scale_outlier = None
        self._scale_last_ts = None
        self._vz_ref = None
        self._vz_ref_age = 0
        self._dims_samples = []
        self._dims_rebased = False
        self._meas_w_min = self._meas_w_max = None
        self._meas_h_max = None
        self._meas_g_area = None
        # NOTE: _sess_track_h (B9b) is deliberately NOT cleared here. This method also runs at
        # every hardware ARM EDGE (the per-shot calibration epoch), and the capture's meter SCALE
        # is exactly the thing that does NOT change from shot to shot -- clearing it per shot
        # would leave the micro gate cold at the start of every shot, i.e. never armed at the
        # only moment it has to decide. Only reset_tracking() (a real colour/style change: a
        # DIFFERENT meter) ends it.
        # NOTE: _ever_capped is deliberately NOT cleared. The track-height histories ARE wiped (by
        # _reset_state), and _ever_capped is precisely the memory that this wipe was a reset, not a
        # cold start -- it is what makes EPOCH-3 refuse a prior-denominator fill on the new meter's
        # first cap-less frames instead of emitting a confidently-wrong number.

    def reset_tracking(self):
        """End a source/profile epoch, not an ordinary per-shot lock.

        Session rulers reduce shot-to-shot jitter only for the SAME meter. A new
        source, geometry or profile must not inherit its box/base offset, warm
        lock, template or an in-flight locator result from the previous one.
        """
        reset_locator = getattr(self._meter_detector, "reset", None)
        if callable(reset_locator):
            reset_locator()  # generation fence; no wait for in-flight inference
        self._clear_gameplay_structure_proof()
        self._reset_state()
        self._det_reset_lock_state()
        self._det_active_box = None
        self._det_warm_pos = None
        self._det_warm_ts = -1.0e9
        self._det_seen_dts = -1.0e9
        self._det_no_meter = False
        self._det_region = None
        self._det_priority_epoch = None
        self._det_priority_pending_ts = -1.0e9
        self._det_hot_epoch = None
        self._det_hot_open_ts = -1.0e9
        self._det_last_sync_acq = -1.0e9
        self._det_acq_scan_epoch = None
        self._det_acq_scan_index = 0
        self._det_acq_last_side = None
        self._det_acq_scan_last = 'full'
        self._det_pixel_span_hist.clear()
        self._det_pixel_ruler_epoch += 1
        self._subpx_ruler_hist = []
        self._subpx_ruler_kind = ""
        self._last_subpx_transform = None
        self._dbg_subpx = None
        # Preserve the monotonic counter so native never joins two source rulers.
        self._fill_estimator_identity = None
        self._last_fill_estimator_mode = ""
        self._last_fill_estimator_generation = 0
        self._arm_edge_mask = None
        self._arm_edge_mask_epoch = 0
        self._scan_raw_key = None
        self._scan_raw = None
        self._capless_since = None
        self._capless_hist.clear()
        self._prob_refuse_n = 0
        # A colour/style change is a NEW METER, not a re-acquire of the same one -> end the epoch.
        # Deliberately NOT called from the confidence-decay lock-drop path, which keeps the scale
        # (same meter, same size, it will be back).
        self.reset_session_scale()
        # B9b: ...and a DIFFERENT meter invalidates the measured scale the micro gate reads.
        self._sess_track_h = []
        self._tracking_meter_style = str(
            getattr(self._cfg, "meter_style", "") or "").strip().casefold()
        # reload_config rebuilt the bands; same-sized colour changes must also
        # refresh colour-specific width/green-cap floors, not keep the old ones.
        self._recompute_scale()

    def _prepare_frame_geometry(self, frame):
        """Refresh size-dependent priors before either production or legacy read.

        detect() can bypass read() on the detector-fill path, so both entry points
        call this idempotent helper. Same-size frames never discard calibration.
        """
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if (height, width) == (self.H, self.W):
            return
        if self.H and self.W:
            self.reset_tracking()
        self.H, self.W = height, width
        self._recompute_scale()

    # scaled geometry priors for the live capture size (identity at 1920x1080)
    def _recompute_scale(self):
        sx = self.W / self._REF_W if self.W else 1.0
        sy = self.H / self._REF_H if self.H else 1.0
        self._band = (int(round(self.BAND[0] * sx)), int(round(self.BAND[1] * sy)),
                      int(round(self.BAND[2] * sx)), int(round(self.BAND[3] * sy)))
        # Colour-scoped floor: 2K27's white ribbon is far narrower than the
        # Arrow2 red bar W_MIN was measured from, and the unscoped floor rejected
        # it at every resolution. Red/Purple resolve to self.W_MIN unchanged.
        self._w_min = max(3, int(round(
            _mbc.width_floor(getattr(self, "_meter_color", None), self.W_MIN) * sx)))
        self._w_max = max(self._w_min + 1, int(round(self.W_MAX * sx)))
        self._h_acq = max(4, int(round(self.H_ACQ * sy)))
        self._h_acq_armed = max(3, int(round(self.H_ACQ_ARMED * sy)))
        self._h_max = max(self._h_acq + 1, int(round(self.H_MAX * sy)))
        self._h_hold = max(3, int(round(self.H_HOLD * sy)))
        self._pad_x = max(6, int(round(self.ROI_PAD_X * sx)))
        self._pad_up = max(8, int(round(self.ROI_PAD_UP * sy)))
        self._pad_dn = max(4, int(round(self.ROI_PAD_DN * sy)))
        # Colour-scoped green-tip floors. The shipped constants were measured on the
        # RED Arrow2 cap (w~28-30, h~24-30, area~500-690 @1080p); 2K27's white meter has a
        # much smaller apex, so on those floors the tip anchor never fires. Red/Purple
        # resolve to the class constants unchanged. See meter_bar_colors.green_tip_floors.
        _gwf, _ghf, _gaf = _mbc.green_tip_floors(
            getattr(self, "_meter_color", None),
            (self.GW_MIN, self.GH_MIN, self.G_AREA_MIN))
        self._gw_min = max(4, int(round(_gwf * sx)))
        self._gw_max = max(self._gw_min + 1, int(round(self.GW_MAX * sx)))
        self._gh_min = max(1, int(round(_ghf * sy)))
        self._gh_max = max(self._gh_min + 1, int(round(self.GH_MAX * sy)))
        self._g_area_min = max(8, int(round(_gaf * sx * sy)))
        self._tip_dx = max(8, int(round(self.TIP_DX * sx)))
        self._tip_up_min = max(20, int(round(self.TIP_UP_MIN * sy)))
        self._tip_up_max = max(self._tip_up_min + 8, int(round(self.TIP_UP_MAX * sy)))
        self._tip_px_min = max(2, int(round(self.TIP_PX_MIN * sx * sy)))
        self._stub_h_min = max(3, int(round(self.STUB_H_MIN * sy)))
        # N7: never let the early floor exceed the ordinary pop-in floor -- the early pass
        # must only ever ADD acceptance below it, never move the shipped floor upward.
        self._stub_h_min_early = min(
            self._stub_h_min, max(3, int(round(self.STUB_H_MIN_EARLY * sy))))
        # style chevron-cap lift: the arrow-tip apex sits ~this many px above the GREEN cap top.
        # Used by the tip-capture guarantee so the box top always covers the tip even when the
        # silver-chevron walk finds nothing (faint/blurred cap).
        self._apex_lift = max(1, int(round(self._tip_lift_px * sy))) if self._tip_lift_px else 0
        # explicit search-band override (@1080p priors, scaled to the live capture size).
        if getattr(self, "_band_override", None):
            bo = self._band_override
            self._band = (int(round(bo[0] * sx)), int(round(bo[1] * sy)),
                          int(round(bo[2] * sx)), int(round(bo[3] * sy)))

    # ------------------------------------------------------------------ #
    #  CAMERA-ANGLE SCALE ADAPTATION. The meter is player-attached: it shrinks
    #  (zoom-out / pan-away) or grows (zoom-in) on screen. On every CONFIDENT
    #  red lock we learn the meter's actual width and keep a rolling median; the
    #  acquire/track size gates then adapt around that estimate so an off-nominal
    #  meter still passes. GUARDED: the estimate must be off nominal by more than
    #  the deadband AND backed by >= min-samples before ANY gate changes, so a
    #  nominal-camera standstill read is byte-identical.
    # ------------------------------------------------------------------ #
    def _note_scale_sample(self, red_w: float) -> None:
        """Harvest the meter's RAW red-column WIDTH on a confident red lock. Width is the cleanest
        zoom signal: it is un-smoothed (unlike the deliberately-steadied track height) and
        fill-independent (unlike the column height). The first `min_samples` establish the session
        BASELINE (their median); thereafter the estimate is a responsive EMA of
        (current width / baseline) = the relative zoom since the meter first appeared. The EMA
        (not a lagging median) is what lets the gates widen BEFORE a shrinking column falls below
        the width floor. Width saturates at the gate ceiling when zooming IN, which still lifts the
        estimate enough to widen the (binding) height ceiling."""
        if not self._scale_adapt or red_w <= 0:
            return
        if self._size_base <= 0:
            self._size_hist.append(float(red_w))
            if len(self._size_hist) >= self._scale_min_samples:
                self._size_base = float(np.median(self._size_hist))
                self._scale_est = 1.0
            return
        ratio = float(red_w) / self._size_base
        # EPOCH-7: a SINGLE bad width sample must not be able to move the estimate. A blurred /
        # merged / clipped relocate width is not a zoom observation, yet one of them moved
        # _scale_est ~30% in ONE frame -- enough to open the 0.10 deadband and widen every acquire
        # gate for >=8s (the guard decay). Guard the INPUT, not the rate: a real camera zoom is a
        # RAMP, so every genuine sample lands near the running estimate, while a sample that
        # TELEPORTS away from it is noise. Refuse the teleport -- unless the NEXT sample
        # CORROBORATES it (two independent frames agreeing = a real hard camera cut), which costs
        # one frame of lag and keeps even an abrupt scale change reachable.
        #
        # Slowing the EMA (the other candidate fix) was tried and REVERTED: at alpha 0.2 the
        # estimate cannot widen the width floor BEFORE a shrinking column falls through it --
        # test_scale_adapt_accepts_zoomed_out_meter drops to 0/8 red reads at 0.6x. Keeping a real
        # 0.6x/1.6x zoom tracked is a live requirement, so the rate stays fast and the OUTLIER
        # REJECTION (which costs legitimate ramps nothing) is what kills the one-bad-frame poison.
        if abs(ratio - self._scale_est) > self._scale_step_max:
            prev = self._scale_outlier
            self._scale_outlier = ratio
            if prev is None or abs(ratio - prev) > self._scale_corrob_tol:
                return                     # lone teleport -> evidence of nothing, drop it
        else:
            self._scale_outlier = None
        # EMA (alpha 0.5): fast enough to widen the width floor before a shrinking column is lost,
        # smooth enough that a nominal meter's width jitter stays inside the deadband -> dormant +
        # byte-identical.
        self._scale_est = min(self._scale_win_hi,
                              max(self._scale_win_lo, 0.5 * self._scale_est + 0.5 * ratio))

    def _scale_active(self) -> bool:
        return (self._scale_adapt and self._size_base > 0
                and len(self._size_hist) >= self._scale_min_samples
                and abs(self._scale_est - 1.0) > self._scale_deadband)

    def _scale_gates(self, w_min, w_max, h_max, hmin, ar_min):
        """Widen the red size/aspect gates around the rolling scale estimate. Returns the
        NOMINAL gates UNCHANGED when the estimate is nominal (deadband) or unconfident ->
        byte-identical. Only ever WIDENS (min on the floors, max on the ceilings)."""
        if not self._scale_active():
            return w_min, w_max, h_max, hmin, ar_min
        s = self._scale_est
        s_lo = max(self._scale_win_lo, min(1.0, s * 0.85))
        s_hi = min(self._scale_win_hi, max(1.0, s * 1.15))
        w_min2 = min(w_min, max(3, int(round(w_min * s_lo))))
        w_max2 = max(w_max, int(round(w_max * s_hi)))
        h_max2 = max(h_max, int(round(h_max * s_hi)))
        hmin2 = min(hmin, max(3, int(round(hmin * s_lo)))) if hmin else hmin
        # modest aspect tolerance for perspective skew, ONLY once an off-nominal scale is proven
        ar2 = (ar_min / self._aspect_tol) if ar_min else ar_min
        return w_min2, w_max2, h_max2, hmin2, ar2

    def _scale_green_gates(self, gw_min, gw_max, gh_min, gh_max, g_area):
        """Green-tip counterpart of _scale_gates (identity when scale-adapt is inactive)."""
        if not self._scale_active():
            return gw_min, gw_max, gh_min, gh_max, g_area
        s = self._scale_est
        s_lo = max(self._scale_win_lo, min(1.0, s * 0.85))
        s_hi = min(self._scale_win_hi, max(1.0, s * 1.15))
        return (min(gw_min, max(3, int(round(gw_min * s_lo)))),
                max(gw_max, int(round(gw_max * s_hi))),
                min(gh_min, max(2, int(round(gh_min * s_lo)))),
                max(gh_max, int(round(gh_max * s_hi))),
                min(g_area, max(12, int(round(g_area * s_lo * s_lo)))))

    def _courtwide_track_scale(self) -> float:
        """Scale of a structurally-proven court-wide lock relative to the seed width.

        A half-court Go-To camera can render the player-attached meter narrower than the
        nominal 1280x720 width floor.  The ordinary session scale estimator cannot solve that
        bootstrap problem because it needs several already-accepted red frames before it may
        widen any gates.  A court-wide lock is different: it was admitted only inside a trusted
        physical-shot window and only after connected red+green meter structure passed the strict
        relation check.  Let that one lock carry its measured scale into its *bounded relocate
        window*.  No cold/menu/global red scan receives these relaxed gates.
        """
        if not self._courtwide_lock or self._lock_w_ref <= 0.0:
            return 1.0
        seed_w = max(1.0, 0.5 * float(self._w_min + self._w_max))
        # A micro lock reached this point only after hardware-token-bounded,
        # two-frame red+green structural corroboration.  Carry its measured
        # sub-quarter scale into the already-tight relocate ROI; cold/global
        # scans never consult this value because ``_courtwide_lock`` is false.
        return max(0.18, min(1.0, float(self._lock_w_ref) / seed_w))

    # ------------------------------------------------------------------ #
    #  B2 (ORION_READER_VZOOM): VERTICAL follow -- the y-axis mirror of the
    #  width-based _scale_est. No vertical adaptation existed at all: the search
    #  band's y-extent is a FIXED prior and _relocate_window HARD-CLAMPS to it,
    #  so a camera zoom that lifts the meter above y~250 (@1080p) loses the lock
    #  AND the cold re-acquire (same clamped band) can never re-find it.
    # ------------------------------------------------------------------ #
    def _note_vshift_sample(self, top_y: float, floor_y: float) -> None:
        """Remember the last confident red-locked track box's vertical extent (top, floor).
        Session-persistent (survives lock drops) so the RE-ACQUIRE band can follow a meter that
        a zoom carried out of the fixed prior band."""
        if self._vzoom and floor_y > top_y >= 0:
            self._vz_ref = (float(top_y), float(floor_y))
            self._vz_ref_age = 0

    def _band_eff(self):
        """The search band, vertically SHIFTED UP by the MINIMUM needed to keep the last locked
        box's TOP inside the band with `_vzoom_margin` px of headroom. TOP-edge only: the
        confirmed live failure is a zoom LIFTING the meter above the fixed y0~250 prior; the
        band's BOTTOM edge must never follow, because the meter's floor legitimately rides
        within a few tens of px of y1~770 on nominal shots (fourth A/B iteration: a bottom-edge
        follow shifted the band on ~2900 nominal frames and turned one real shot's fresh reads
        into stale echoes). A track comfortably below y0+margin never moves the band; the shift
        persists for the cold re-acquire after a drop (bounded by _vz_ref_age) then ages out.
        A SHIFT (not a widen), bounded to _vzoom_max_px, so no extra dÃ©cor area is ever
        admitted. Flag OFF -> the fixed prior band, byte-identical.

        1a (ORION_READER_VZOOM_DOWN) rides on top: the shot-ARM camera dive carries the meter
        floor THROUGH and BELOW the fixed band bottom (y1~770 -> ~891 @1080p) -- unhandled by
        the up-only shift, the lock coasts/drops and the truncated column misreads. While a
        LIVE lock's tracked floor (_vz_ref[1]) has genuinely dived below y1-margin, the band
        BOTTOM is WIDENED down (y0 untouched) to contain floor+margin, bounded by
        _vzoom_down_max_px below the nominal bottom. Unlike the reverted bottom-SHIFT: it is
        a widen (the nominal area is always still scanned), it requires a live lock
        (self.box -- a cold/off-shot frame never widens, so no off-shot dÃ©cor area is ever
        admitted) and a floor comfortably inside the band never triggers it."""
        # DETECTOR REGION: when the trained detector has located the meter this frame, every
        # colour search (cold acquire, steal, tracking bounds) is confined to that box so read()
        # cannot re-lock décor it cannot tell from a white meter. Overrides the nominal band and
        # the vzoom shift for this frame; None (detector off/stale) -> shipped behaviour.
        if getattr(self, "_det_region", None) is not None:
            return self._det_region
        if not self._vzoom or self._vz_ref is None:
            return self._band
        if self.box is None and self._vz_ref_age > self._vzoom_ref_frames:
            return self._band            # stale ref: cold acquire must scan the nominal band
        x0, y0, x1, y1 = self._band
        sy = self.H / self._REF_H if self.H else 1.0
        m = self._vzoom_margin * sy
        dy = min(0.0, self._vz_ref[0] - (y0 + m))   # negative only: meter above the top -> shift up
        dy = max(-self._vzoom_max_px * sy, dy)
        self._vy_est = float(dy)
        if dy > -1.0:
            band = self._band
        else:
            dy = int(round(dy))
            band = (x0, max(0, y0 + dy), x1, min(self.H, y1 + dy))
        return self._band_down_widen(band)

    def _tracking_bounds(self):
        """Clip bounds for the current lock's tight relocate/dead-reckon window.

        Cold/unverified locks retain the proven court band.  Once a physical shot
        has authorized a real lock, its player-attached meter may translate across
        that nominal band's edge (notably the half-court Go-To camera) without
        becoming a bounded ``meter_memory`` echo.  Court-wide structural acquires
        get the same treatment immediately.  In both cases the searched rectangle
        remains the small velocity-followed ROI around ``self.box``; full-frame is
        only a clipping boundary and never another global colour scan.
        """
        if self._bounds_relaxed():
            return (0, 0, int(self.W), int(self.H))
        return self._band_eff()

    def _bounds_relaxed(self) -> bool:
        """True exactly when _tracking_bounds() has given up the band for the full frame.

        Single source of truth shared with _tracking_bounds() so the F2 jump gate can never
        disagree with the condition that actually removed the positional bound.
        """
        return bool(
            self._courtwide_lock
            or (self.box is not None and self._shot_armed_hw
                and self._gameplay_lock_authorized))

    def _band_down_widen(self, band):
        """1a (ORION_READER_VZOOM_DOWN): extend the band BOTTOM down to contain a LIVE lock's
        diving floor (see _band_eff docstring). Flag OFF / no live lock / floor comfortably
        inside the band -> `band` returned unchanged (byte-identical)."""
        self._vy_down = 0.0
        if not self._vzoom_down or self.box is None or self._vz_ref is None:
            return band
        x0, y0, x1, y1 = band
        sy = self.H / self._REF_H if self.H else 1.0
        m = self._vzoom_margin * sy
        floor = self._vz_ref[1]
        if floor <= y1 - m:
            return band                  # nominal: floor comfortably inside -> never widen
        cap = min(float(self.H), self._band[3] + self._vzoom_down_max_px * sy)
        y1n = int(round(min(cap, floor + m)))
        if y1n <= y1:
            return band
        self._vy_down = float(y1n - y1)
        return (x0, y0, x1, y1n)

    def _apply_tip_lift(self, tbox, cap_top, top_search, apex_top=None):
        """TIP-CAPTURE GUARANTEE (decoupled from the fill scale): ensure the box TOP sits at
        least `_apex_lift` px ABOVE the green cap top so the silver arrow-tip apex is always
        inside the box, even when the chevron walk found nothing (faint/blurred cap). Only ever
        RAISES the top (grows the height, keeps the floor); the fill %% is unchanged (it reads
        from fillable_h / red_top, never from the box). No-op when no green cap was seen.

        FIX-1 (ORION_READER_TIP_TIGHT): the FIXED `_apex_lift` headroom above the cap is what
        vertically ELONGATED the emitted box (it was applied even when the apex walk found
        NOTHING above the cap). Tight mode lifts only to `apex_top` -- the apex row the walk
        ACTUALLY detected (silver chevron / green evidence) -- and, with no detected apex,
        to the green cap top itself (hug the cap, zero forced headroom). Still raise-only."""
        if not self._tip_enforce or self._apex_lift <= 0 or cap_top is None:
            return tbox
        x, y, w, h = tbox
        floor = y + h
        if self._tip_tight:
            tgt = int(apex_top) if (apex_top is not None and int(apex_top) < int(cap_top)) \
                else int(cap_top)
            guarantee = top_search + tgt
        else:
            guarantee = top_search + int(cap_top) - self._apex_lift
        if guarantee < y:
            y = max(0, guarantee)
            h = floor - y
        return (x, y, w, h)

    def _enclose_meter_bbox(self, frame, col, tbox,
                            cap_top_abs=None, cap_bot_abs=None):
        """Served-bbox presentation envelope = the connected cap/apex union below, then the
        measured OUTLINE hug (`_hug_outline_bbox`).

        The two steps are wrapped here rather than sequenced at the call site so BOTH readers
        get them: the compressed reader's luma fill path builds its own track box and calls
        this method directly (compressed_meter_reader.py), and the meter it is drawing has the
        same frame stroke and the same two arrow caps.

        `_pre_hug_tbox` records the result BEFORE the hug, for the consumers in read() that
        feed SEARCH state (the VZOOM floor reference, the robust dim rebase) rather than
        presentation -- so the outline margin can never move the band or the acquire gates.
        """
        enclosed = self._enclose_meter_bbox_core(
            frame, col, tbox, cap_top_abs, cap_bot_abs)
        self._pre_hug_tbox = enclosed
        return self._hug_outline_bbox(frame, col, enclosed)

    def _enclose_meter_bbox_core(self, frame, col, tbox,
                                 cap_top_abs=None, cap_bot_abs=None):
        """Expand only the SERVED bbox to the meter structure actually present.

        Fill/timing continues to use the narrow ``_fill_strip`` masks and its measured
        denominator.  This second, tiny ROI exists solely because the meter's green cap and
        silver apex may be wider than the red column (especially a distant half-court Go-To).
        The historical ``red column +/- 3px`` presentation box could therefore clip the cap
        even though the timing read was valid.  We union only geometrically-related connected
        green/silver components with the existing box; no guessed minimum width/height is
        imposed and no pixel used by the timing calculation is changed.
        """
        try:
            x, y, w, h = (int(v) for v in tbox)
            cx, cy, cw, ch = (int(v) for v in col)
        except (TypeError, ValueError):
            return tbox
        if frame is None or w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
            return tbox

        frame_h, frame_w = frame.shape[:2]
        if frame_w <= 0 or frame_h <= 0:
            return tbox

        # The timing strip is deliberately padded +/-3px around the red column, but that padding
        # is not visible meter structure and made a distant meter's presentation box look wide.
        # Keep the timing box's authoritative vertical span while starting the output width on the
        # measured red body; the related cap/apex components below add back every visible pixel.
        # QRect-style right/bottom values are exclusive, matching numpy slices.
        left = max(0, cx)
        top = max(0, min(y, cy))
        right = min(frame_w, cx + cw)
        bottom = min(frame_h, max(y + h, cy + ch))
        if right <= left or bottom <= top:
            return tbox

        sy = frame_h / self._REF_H if frame_h else 1.0
        red_cx = cx + 0.5 * cw
        # The strict court-wide acquisition contract permits a connected cap up to 1.75x the red
        # width.  Search just beyond that relation, but keep selection compact and centred so a
        # nearby scoreboard/jersey component cannot horizontally inflate the served box.
        x_radius = max(8, int(np.ceil(1.85 * cw)))
        apex_look_up = max(4, int(np.ceil(14.0 * sy)))
        rx0 = max(0, min(left, int(np.floor(red_cx - x_radius))))
        rx1 = min(frame_w, max(right, int(np.ceil(red_cx + x_radius))))
        anchor_top = int(cap_top_abs) if cap_top_abs is not None else top
        ry0 = max(0, min(top, anchor_top - apex_look_up))
        ry1 = min(frame_h, max(bottom, cy + ch + 3))
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            return (left, top, right - left, bottom - top)

        roi = frame[ry0:ry1, rx0:rx1]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        green_mask = self._greenmask(hsv)
        green_contours, _ = cv2.findContours(
            green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        cap_tol = max(1, int(round(2.0 * sy)))
        max_cap_w = max(4, int(np.ceil(1.80 * cw)))
        max_cap_h = max(4, int(np.ceil(16.0 * sy)))
        centre_tol = max(3.0, 0.55 * cw)
        green_box = None
        green_score = float("inf")
        for contour in green_contours:
            gx, gy, gw, gh = cv2.boundingRect(contour)
            if gw <= 0 or gh <= 0 or gw > max_cap_w or gh > max_cap_h:
                continue
            area = int(cv2.countNonZero(green_mask[gy:gy + gh, gx:gx + gw]))
            if area < 2 or area / float(max(1, gw * gh)) < 0.20:
                continue
            agx, agy = rx0 + gx, ry0 + gy
            agx1, agy1 = agx + gw, agy + gh
            gcx = agx + 0.5 * gw
            if abs(gcx - red_cx) > centre_tol:
                continue
            if cap_top_abs is not None:
                cap_lo = int(cap_top_abs) - cap_tol
                cap_hi = int(cap_bot_abs if cap_bot_abs is not None
                             else cap_top_abs) + cap_tol + 1
                if agy1 <= cap_lo or agy >= cap_hi:
                    continue
                vertical_error = abs(agy - int(cap_top_abs))
            else:
                # Luma fallback has no chroma cap row.  A candidate still has to be in the
                # full-track top zone and above the red fill, never an arbitrary court reflection.
                top_zone = max(8, int(round(0.25 * h)))
                if agy1 > cy + cap_tol or abs(agy - top) > top_zone:
                    continue
                vertical_error = abs(agy - top)
            score = abs(gcx - red_cx) + 2.0 * vertical_error - 0.01 * area
            if score < green_score:
                green_score = score
                green_box = (agx, agy, agx1, agy1)

        if green_box is not None:
            left = min(left, green_box[0])
            top = min(top, green_box[1])
            right = max(right, green_box[2])
            bottom = max(bottom, green_box[3])

            # The apex is bright/low-saturation and immediately joins (or sits within a tiny
            # anti-alias gap above) the chosen green cap.  Selecting relative to that already-
            # corroborated component keeps white court lines and UI text out of the bbox.
            silver_mask = cv2.inRange(
                hsv, np.array((0, 0, 141), np.uint8),
                np.array((179, 89, 255), np.uint8))
            silver_contours, _ = cv2.findContours(
                silver_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            max_silver_w = max(4, int(np.ceil(1.90 * cw)))
            max_silver_h = max(3, int(np.ceil(16.0 * sy)))
            max_gap = max(2, int(np.ceil(3.0 * sy)))
            silver_box = None
            silver_score = float("inf")
            ggx0, ggy0, ggx1, _ggy1 = green_box
            green_cx = 0.5 * (ggx0 + ggx1)
            for contour in silver_contours:
                sx, sy0, sw, sh = cv2.boundingRect(contour)
                if sw <= 0 or sh <= 0 or sw > max_silver_w or sh > max_silver_h:
                    continue
                area = int(cv2.countNonZero(silver_mask[sy0:sy0 + sh, sx:sx + sw]))
                if area < max(2, int(np.ceil(0.20 * cw))):
                    continue
                asx, asy = rx0 + sx, ry0 + sy0
                asx1, asy1 = asx + sw, asy + sh
                gap = ggy0 - asy1
                if gap < -cap_tol or gap > max_gap:
                    continue
                scx = asx + 0.5 * sw
                if abs(scx - green_cx) > max(3.0, 0.55 * max(cw, ggx1 - ggx0)):
                    continue
                # The chevron must overlap the cap horizontally (one-pixel tolerance for
                # sub-pixel anti-aliasing); nearby white decor cannot merely share its height.
                if asx1 < ggx0 - 1 or asx > ggx1 + 1:
                    continue
                score = 4.0 * abs(gap) + abs(scx - green_cx) - 0.01 * area
                if score < silver_score:
                    silver_score = score
                    silver_box = (asx, asy, asx1, asy1)
            if silver_box is not None:
                left = min(left, silver_box[0])
                top = min(top, silver_box[1])
                right = max(right, silver_box[2])
                bottom = max(bottom, silver_box[3])

        left = max(0, min(left, frame_w))
        top = max(0, min(top, frame_h))
        right = max(left, min(right, frame_w))
        bottom = max(top, min(bottom, frame_h))
        if left == 0 or right == frame_w:
            # At a frame edge the observed cap can itself be clipped.  Latching that clipped
            # width for the physical shot would later cut the cap when the player moves inward.
            # Preserve one fixed, scale-relative envelope (the same 1.75x cap-width ceiling the
            # strict structural acquire accepts) and shift it wholly inside the frame.  This is
            # a one-time edge identity size, not per-frame horizontal stretching.
            target_w = min(frame_w, max(right - left, int(np.ceil(1.75 * cw))))
            preferred_left = int(round(red_cx - 0.5 * target_w))
            preferred_left = max(right - target_w, min(preferred_left, left))
            left = max(0, min(preferred_left, frame_w - target_w))
            right = left + target_w
        return (left, top, right - left, bottom - top)

    def _hug_outline_bbox(self, frame, col, tbox):
        """Grow the SERVED bbox from the coloured TRACK out to the meter's drawn OUTLINE.

        Returns a box that encloses the silver frame and both arrow caps, or ``tbox`` unchanged
        when no outline is measurable. STRICTLY GROW-ONLY and OUTPUT-ONLY: no fill, mask,
        denominator, template or search geometry reads this result (see the ORION_READER_BOX_HUG
        note in __init__ for the measurement this is built on).

        Method -- the meter is a flat-shaded overlay, the world behind it is not:
          1. take the red body's INTERIOR rows (both chevron notches excluded);
          2. walk outward one column at a time from each side of the red body while that column
             is (a) vertically FLAT over those rows and (b) not the local background colour;
          3. a side that stays flat all the way to the search limit has found no edge -- that is
             flat background, not a stroke -- so it contributes nothing;
          4. the resolved stroke thickness closes the box at the top and bottom, where the same
             stroke wraps into the apex and the bottom chevron.
        """
        if not self._box_hug or frame is None:
            return tbox
        try:
            x, y, w, h = (int(v) for v in tbox)
            cx, cy, cw, ch = (int(v) for v in col)
        except (TypeError, ValueError):
            return tbox
        if w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
            return tbox
        fh, fw = frame.shape[:2]
        if fh <= 0 or fw <= 0:
            return tbox
        # The stroke is a small fraction of the body width; anything further out is not the
        # meter. This bound is also what makes step 3 above meaningful, and 0.40 keeps the
        # widest possible result inside the 1.80x-of-body presentation envelope the strict
        # structural acquire already documents (and that the served-bbox suite asserts).
        # Measured stroke on session_20260804_032333: p95 4 px, max 5 px on a 16-18 px body.
        lim = max(2, int(0.40 * cw))
        inset = max(1, ch // 8)
        ry0 = max(0, cy + inset)
        ry1 = min(fh, cy + ch - inset)
        if ry1 - ry0 < 6:
            return tbox
        rx0 = max(0, cx - lim)
        rx1 = min(fw, cx + cw + lim)
        if rx1 - rx0 < cw + 2:
            return tbox
        try:
            roi = frame[ry0:ry1, rx0:rx1].astype(np.float32)
            col_sd = roi.std(axis=0).mean(axis=1)          # per-column flatness (mean over BGR)
            col_mean = roi.mean(axis=0)                    # per-column mean colour
        except Exception:
            return tbox
        n = col_sd.shape[0]
        i_lo = cx - rx0                                    # ROI index of the body's left edge
        i_hi = cx + cw - 1 - rx0                           # ...and its right edge
        if not (0 <= i_lo <= i_hi < n):
            return tbox
        bg_lo = col_mean[0]
        bg_hi = col_mean[n - 1]

        def _walk(start, step, budget, bg):
            got = 0
            i = start
            while got < budget:
                i += step
                if i < 0 or i >= n:
                    return 0                               # ran off the ROI -> unresolved
                if col_sd[i] > self._box_hug_sd:
                    return got                             # a real edge: the world behind
                if float(np.max(np.abs(col_mean[i] - bg))) < self._box_hug_bg_delta:
                    return got                             # this IS the background -> stop
                got += 1
            return 0                                       # flat all the way out -> no edge found

        dl = _walk(i_lo, -1, lim, bg_lo)
        dr = _walk(i_hi, +1, lim, bg_hi)
        if dl <= 0 and dr <= 0:
            return tbox
        sides = [d for d in (dl, dr) if d > 0]
        pad = int(round(float(sum(sides)) / len(sides)))
        # BOX-TIGHT mode 2 observability: the MEASURED stroke this frame (attribute-only;
        # cleared per-call at the top of _read_fill so a stale value can never cross frames).
        self._hug_stroke = (int(dl), int(dr), int(pad))
        left = max(0, min(x, cx - dl))
        right = min(fw, max(x + w, cx + cw + dr))
        top = max(0, min(y, y - pad))
        bottom = min(fh, max(y + h, y + h + pad))
        if right - left <= 0 or bottom - top <= 0:
            return tbox
        return (left, top, right - left, bottom - top)

    def _fresh_up_req(self) -> int:
        """[ORION_READER_FRESH_RISE_STEPS_ARMED] P2: the N-consecutive-rising-frames proof
        requirement for THIS frame. The armed value applies only while the PHYSICAL shot gate
        vouches a shot is in progress; every unarmed/off-shot frame keeps the base 3-step rule
        (the CV self-arm cannot lower its own bar). Default armed == base -> byte-identical."""
        return self._fresh_up_min_armed if self._shot_armed_hw else self._fresh_up_min

    def _can_bridge_arm_epoch(self, ts: float) -> bool:
        """Whether the current lock is fresh enough to survive a physical arm edge.

        This deliberately does not accept confidence/memory alone.  A bridge requires several
        consecutive accepted RED reads, a measured full-track denominator, a plausible full-track
        bbox, and zero coast frames.  A held tail, green-only echo, gated fill, or phantom lock that
        has not proved this geometry therefore still gets the original hard epoch reset.
        """
        if not self._epoch_bridge or self.box is None or self.conf < self.CONF_MIN:
            return False
        if self._fresh_lock_streak < self._epoch_bridge_frames or self._coast_n != 0:
            return False
        # Fresh-looking geometry is not sufficient: a static red decoration can produce a clean
        # contour + plausible green/full-track anchor for arbitrarily many frames.  Require the
        # same monotonic-rise proof used to authorize a fresh `rising` claim, plus a currently
        # positive measured slope.  The slope window makes the proof recent; a meter that rose in
        # the past and has since sat flat cannot carry that authority into the next physical arm.
        if self._fresh_up_n < self._fresh_up_req() or self._velocity <= 25.0:
            return False
        if self._fresh_lock_ts is None or ts - self._fresh_lock_ts > self._epoch_bridge_ms / 1000.0:
            return False
        if len(self._track_h_hist) < self._epoch_bridge_frames or self._lock_w_ref <= 0.0:
            return False
        if not self.last_tbox or len(self.last_tbox) < 4:
            return False
        _x, _y, w, h = (int(v) for v in self.last_tbox)
        if w <= 0 or h <= 0 or h < 1.5 * w:
            return False
        return 0 <= _x < self.W and 0 <= _y < self.H and _x + w <= self.W and _y + h <= self.H

    def _stabilize_shot_tbox(self, tbox):
        """Make a physical-shot bbox translation-only while leaving measurement untouched.

        The meter itself may move anywhere in the search band, so each fresh read supplies the
        current centre-x and floor-y.  Width/height come from the physical shot's latched shape.
        This is output-only: `_read_fill`, `self.box`, masks, and templates retain raw geometry.
        """
        out = [int(v) for v in tbox] if tbox else [0, 0, 0, 0]
        if not self._shot_shape_lock or not self._shot_shape_live or out[2] <= 0 or out[3] <= 0:
            return out
        if self._shot_shape is None:
            self._shot_shape = (int(out[2]), int(out[3]))
            return out
        w, h = self._shot_shape
        cx = float(out[0]) + 0.5 * float(out[2])
        floor = int(out[1]) + int(out[3])
        x = int(round(cx - 0.5 * float(w)))
        y = int(round(float(floor - h)))
        x = max(0, min(x, max(0, self.W - w)))
        y = max(0, min(y, max(0, self.H - h)))
        return [x, y, int(w), int(h)]

    def _visual_drop_shot_shape(self, hw: bool, preserve: bool) -> None:
        """End a visual lock without accidentally ending the physical shot's shape epoch.

        Square normally arrives several frames before the meter is painted.  Treating that normal
        pre-meter miss as a shot end disabled translation-only geometry before the first usable
        frame.  While hardware still owns the shot, keep the epoch live; preserve an existing shape
        only after the old visual identity was trusted.  An unverified/decor identity is cleared so
        the next real meter may establish the epoch dimensions itself.
        """
        if self._shot_shape_lock and bool(hw):
            self._shot_shape_live = True
            if not preserve:
                self._shot_shape = None
            return
        self._shot_shape = None
        self._shot_shape_live = False

    def _predict_tbox(self, tbox, armed, vy=None):
        """FIX-2 (ORION_READER_BOX_PREDICT): forward-predict the REPORTED (held/frozen) box
        along the box velocity so it tracks a sliding meter on FRESH-velocity hold frames
        (green_hold / fill_gated -- the relocate found the meter's colour THIS frame)
        instead of trailing frozen at the last read position. NEVER called on coast frames:
        the coast velocity is frozen pre-vanish and extending it was measured harmful (see
        the flag comment in __init__). REPORTED-box only: `tbox` is copied, last_tbox is
        never mutated -> the search anchor's own dead-reckon can never double-apply.
        Per-axis deadband -> a stationary meter stays BYTE-frozen (zero jitter/overshoot);
        decay + total-shift cap bound a long hold; the shift is clamped into the active
        tracking bounds (never onto decor); x/y only, w/h fixed. KILL-ON-DISARM: unarmed frames zero the
        accumulator and return the frozen box, so a prediction is never ridden into
        post-release."""
        out = [int(v) for v in tbox] if tbox else [0, 0, 0, 0]
        if not self._box_predict or not out[2]:
            return out
        if not armed:
            self._bp_dx = self._bp_dy = 0.0
            self._bp_n = 0
            return out
        # vy override: on a green_hold frame the relocate col flips from the tall RED
        # column to the short GREEN tip, so its centre-y TELEPORTS and _bvy spikes ~tens
        # of px/frame -- an evidence-type artifact, not motion. Callers there pass vy=0
        # (x-only prediction); a fill_gated frame's red-learned velocity is trusted.
        _vy = self._bvy if vy is None else float(vy)
        d = self._bp_decay ** self._bp_n
        cap = float(self._bp_cap_px) * (self.W / self._REF_W if self.W else 1.0)
        moved = False
        if abs(self._bvx) >= self._bp_deadband:
            self._bp_dx = max(-cap, min(cap, self._bp_dx + self._bvx * d))
            moved = True
        if abs(_vy) >= self._bp_deadband:
            self._bp_dy = max(-cap, min(cap, self._bp_dy + _vy * d))
            moved = True
        if moved:
            self._bp_n += 1
        if self._bp_dx == 0.0 and self._bp_dy == 0.0:
            return out
        tx, ty, tw, th = out
        tx = int(round(tx + self._bp_dx))
        ty = int(round(ty + self._bp_dy))
        bnd = self._tracking_bounds()
        tx = max(bnd[0], min(tx, max(bnd[0], bnd[2] - tw)))
        ty = max(bnd[1], min(ty, max(bnd[1], bnd[3] - th)))
        return [tx, ty, tw, th]

    def _tight_display_box(self, bbox, top_row=-1, h_cap=0):
        """BOX-TIGHT (ORION_READER_BOX_TIGHT): re-shape the outgoing bbox so the drawn
        overlay hugs the meter, not the presentation envelope. Called ONLY at the detect()
        production boundary, AFTER the frame's dict is final -- no internal state
        (last_tbox / _pre_hug_tbox / self.box / histories) is read from or written to,
        except the display-twin attrs (_tight_off / _tight_wh_hist).

        Fresh red frame (`_tight_src` set by read() this frame):
          mode 1 (BAR-HUG): serve the red column (x, y, w, h) itself, top raised to this
          frame's measured red top row when that sits above the contour top (the same
          "never draw the box top below the red top" contract the top_clips gate asserts).
          mode 2 (REFERENCE-HUG, owner screenshot 2026-06-29 193423): width = the bar
          body plus 0.625*cw of margin per side (the reference's 2.25x stroke-inner
          rect; floored to clear a wider measured housing stroke by 2px -- the cap/apex
          flare can never re-inflate it); vertical extent = the union of this frame's
          PRE-LATCH track geometry (live-measured full track: median-steadied denominator +
          detected cap/apex top + chevron/hug bottom -- never the per-shot latched shape
          that made the box ride high/low) with the bar itself, padded by 4.5%% of that
          extent per side so both arrow caps sit inside with the reference's clearance.
          SIZE ONLY is steadied by a 5-sample median (no rise pumping); POSITION stays
          per-frame (bar centre-x + measured bottom), so pans track without lag. A final
          NEVER-SHRINK clamp expands any violated edge so the drawn box always CONTAINS
          the current red bar. Every input is a per-frame measurement and every margin a
          FRACTION of one, so the box is DYNAMIC: it scales with the on-screen meter and
          is never a fixed/latched size.
        Either mode records the tight box's offsets against the served box; a hold/coast/
        gated frame then TRANSLATES the last fresh tight shape along the served box's glue /
        forward-predict / dead-reckon motion (a valid top_row on such a frame still raises
        the top so a rising bar is never clipped). Zero/absent box or no fresh history yet:
        emit the box unchanged -- this flag never invents a detection and never changes
        WHICH frames carry a box, only the rectangle's geometry."""
        out = [int(v) for v in bbox] if bbox else [0, 0, 0, 0]
        if not self._box_tight or len(out) < 4 or out[2] <= 0 or out[3] <= 0:
            return out
        src = self._tight_src
        if src is not None:
            (cx, cy, cw, ch), srow, pre, stroke = src
            bar_top = min(cy, srow) if srow >= 0 else cy
            bar_bot = cy + ch
            if self._box_tight >= 2:
                # REFERENCE-HUG margins (see __init__): side = 0.625*cw of margin
                # from the bar body edge (reproduces the reference's 2.25x rect),
                # floored so a measured housing stroke wider than the ratio is
                # still cleared by 2px; vertical = the live cap-to-chevron track
                # union padded by 4.5% of its own extent per side, so both arrow
                # caps stay inside the rect with visible clearance.
                dl = int(stroke[0]) if stroke else 0
                dr = int(stroke[1]) if stroke else 0
                side = max(int(round(self._tight_side_frac * cw)), dl + 2, dr + 2)
                left, right = cx - side, cx + cw + side
                if pre is not None and pre[2] > 0 and pre[3] > 0:
                    top = min(int(pre[1]), bar_top)
                    bot = max(int(pre[1]) + int(pre[3]), bar_bot)
                else:
                    top, bot = bar_top, bar_bot
                vpad = max(1, int(round(self._tight_vpad_frac * max(1, bot - top))))
                top -= vpad
                bot += vpad
                # DETECTOR-FILL HEIGHT CLAMP (2026-08-30). On a detector_fill frame the
                # SERVED box is already the meter's full tip-to-base extent, so the
                # legitimate mode-2 height is <= ~1.25x of it (track + 4.5% vpad).
                # Under a meter_color drift, read()'s red-masked path latches decor and
                # hands _tight_src a track candidate spanning most of the frame --
                # measured live (session_20260830_112306, meter_color=Red vs the white
                # meter): 227 frames served 29x320..556 rectangles while the emitted
                # fill (detector-authoritative) read fine. The engine consumes this
                # rectangle (geometry continuity / meter_x/meter_y), so cap the
                # vertical extent at h_cap anchored on the served box; the never-shrink
                # containment below then applies to a sane extent. h_cap==0 (colour
                # path, where a short bar legitimately unions a taller track) is inert.
                if h_cap > 0 and (bot - top) > h_cap:
                    top = out[1] - max(0, (h_cap - out[3]) // 2)
                    bot = top + h_cap
                self._tight_wh_hist.append((right - left, bot - top))
                ws = sorted(p[0] for p in self._tight_wh_hist)
                hs = sorted(p[1] for p in self._tight_wh_hist)
                w_d, h_d = ws[len(ws) // 2], hs[len(hs) // 2]
                ctr = cx + 0.5 * cw
                tight = [int(round(ctr - 0.5 * w_d)), int(round(bot - h_d)),
                         int(w_d), int(h_d)]
                l2 = min(tight[0], cx)
                t2 = min(tight[1], bar_top if h_cap <= 0 else max(bar_top, top))
                r2 = max(tight[0] + tight[2], cx + cw)
                b2 = max(tight[1] + tight[3], bar_bot if h_cap <= 0
                         else min(bar_bot, bot))
                tight = [l2, t2, r2 - l2, b2 - t2]
            else:
                tight = [int(cx), int(bar_top), int(cw), int(bar_bot - bar_top)]
            self._tight_off = (tight[0] - out[0], tight[1] - out[1], tight[2], tight[3])
        elif self._tight_off is not None:
            dx, dy, tw, th = self._tight_off
            tight = [out[0] + int(dx), out[1] + int(dy), int(tw), int(th)]
            if top_row >= 0 and tight[1] > int(top_row):
                tight[3] = tight[1] + tight[3] - int(top_row)
                tight[1] = int(top_row)
            # TRANSLATED-SHAPE HEIGHT CLAMP (2026-08-30, session_20260830_191051): the
            # detector-fill h_cap above only bounded the FRESH branch; this translate
            # branch re-served a poisoned remembered shape (or a bogus top_row from the
            # White track-top search running up a bright wall seam) unbounded -- live
            # epochs 16/32/41/42 published 25x349..420 rectangles (bottom anchored on the
            # real meter, top ~230px up the wall) for 100-200ms right after the cold
            # post-ghost acquire. The engine consumes this rectangle (meter_x/meter_y
            # continuity), read it as a teleport (meter_jump 0.16-0.26) and distrusted
            # the early rise -- reservation validation late by ~200ms on the two
            # live_tip_deadline_missed shots. Clamp BOTTOM-ANCHORED: the bar base was the
            # measured-stable edge on every tall live frame, the wandering top is the
            # artefact. h_cap==0 (colour path) stays inert, matching the fresh branch.
            if h_cap > 0 and tight[3] > h_cap:
                tight[1] = tight[1] + tight[3] - h_cap
                tight[3] = h_cap
        else:
            return out
        if tight[2] <= 0 or tight[3] <= 0:
            return out
        # DISPLAY VERTICAL HUG CLAMP (2026-08-31, session_20260831_114137). The
        # round-2 h_cap clamps above bound the drawn height at 1.6x the served box,
        # but 1.6x is looser than the reference-hug's OWN legitimate extent (that
        # clamp's comment states "<= ~1.25x of it (track + 4.5% vpad)"), so it still
        # permits the mode-2 pre-latch `pre` union (fresh branch) and the top_row
        # raise (translate branch) to run the DRAWN top 44-66px ABOVE the served
        # meter box onto the wall seam / court behind it, the base 24-32px BELOW it,
        # or -- on a stale translate offset -- to float the whole box off the meter.
        # Measured on the byte-identical (ORION_METER_DETECTOR_SYNC) replay of this
        # session's raw frames: the served box `out` hugs the green cap->chevron on
        # 100% of frames and its height never moves >6px, while the DRAWN height
        # toggled 120<->176 on 349/3755 detected frames (9.3%), jumping >=15px
        # frame-to-frame on 2.6% -- exactly the vertical flicker the owner sees.
        # Force the drawn box to COVER the served meter span [out_top, out_bot] and
        # to extend no further than `_tight_top_reach` above / `_tight_bot_reach`
        # below it: an over-reaching edge is pulled in, a floated-off edge is pushed
        # out to re-hug. The correct reference-hug already sits 0-15px past each
        # served edge (bimodal, clean 15-30px gap), so 18 (~= the round-2 clamp's own
        # 1.25x for a ~107px meter) is inert on every correct frame. Applies on BOTH
        # paths (the emit stage here is detector_fill, h_cap>0), anchored on `out`,
        # the reliable served box on both -- the YOLO/served meter box, never the
        # decor-poisoned `pre` the round-2 clamp guarded against. Because the drawn
        # box always contains [out_top, out_bot] and the fill lives inside `out`, a
        # rising bar can never be clipped. DISPLAY-ONLY: `out` (the served/engine
        # box) and every timing sample (fill/velocity/green) are already final and
        # unchanged at this production boundary.
        _hug_min_h = int(self._tight_hug_min_frac * self.H) if self.H else 0
        if (self._tight_top_reach >= 0 and out[3] > _hug_min_h):
            _o_left = out[0]
            _o_right = out[0] + out[2]
            _o_top = out[1]
            _o_bot = out[1] + out[3]
            _t_right = tight[0] + tight[2]
            _t_bot = tight[1] + tight[3]
            if self._tight_side_reach >= 0:
                _n_left = min(
                    _o_left,
                    max(tight[0], _o_left - int(self._tight_side_reach)))
                _n_right = max(
                    _o_right,
                    min(_t_right, _o_right + int(self._tight_side_reach)))
                tight[0] = _n_left
                tight[2] = max(1, _n_right - _n_left)
            _n_top = min(_o_top, max(tight[1], _o_top - int(self._tight_top_reach)))
            _n_bot = max(_o_bot, min(_t_bot, _o_bot + int(self._tight_bot_reach)))
            tight[1] = _n_top
            tight[3] = max(1, _n_bot - _n_top)
        if self.W and self.H:
            tight[2] = max(1, min(tight[2], int(self.W)))
            tight[3] = max(1, min(tight[3], int(self.H)))
            tight[0] = max(0, min(tight[0], int(self.W) - tight[2]))
            tight[1] = max(0, min(tight[1], int(self.H) - tight[3]))
        return tight

    # ------------------------------------------------------------------ #
    #  live shot-gate corroboration hook (OPTIONAL). The self-contained column
    #  gate is the real false-lock guard; this only RELAXES the early-rise
    #  acquisition height so the first 3-7 rise frames aren't missed while the bot
    #  is provably shooting. It NEVER suppresses a real column. Live the sidecar
    #  can push (held-trigger AND/OR v9 pose 'shooting') here.
    # ------------------------------------------------------------------ #
    def set_shot_state(self, armed: bool, pose_confidence: float = 0.0,
                       armed_hw: Optional[bool] = None):
        """armed = the MERGED gate (physical signals OR the CV self-arm refresh) -- relaxes
        acquisition/coast exactly as before. armed_hw = the PHYSICAL-only gate (pushed as a 3rd
        arg by the orchestrator, guarded for the legacy chain): the ColorCalibrator trains ONLY
        inside hw-armed windows so a CV false lock can never teach itself its own colours."""
        self._shot_armed = bool(armed)
        self._shot_pose_conf = float(pose_confidence)
        if armed_hw is not None:
            next_hw = bool(armed_hw)
            if self._shot_armed_hw and not next_hw:
                self._close_epoch_census("hw_disarm")
                self._clear_gameplay_structure_proof()
                self._physical_shot_epoch = 0
                self._clear_micro_candidate()
                # [ORION_PICKUP_PER_PRESS 2026-09-15] THE DISARM EDGE IS THE LAST PRESS'S
                # CLOSE. The engine DOES send shot_gate_release on the live path (the native
                # logs "shot_gate_release send: ... sent=1" per release and autogreen_sidecar
                # dispatches it to release_shot_gate); its receipt line is a WARNING and is
                # routinely eaten by the native relay's 1 s WARNING throttle, which is why it
                # is absent from session logs. notify_physical_shot_release therefore runs and
                # flushes first; this edge -- the hw arm window falling -- is the fallback
                # close for a press whose release edge was lost, taps and aborts. Forensics
                # only: it runs once per disarm, reads one dict and logs.
                self._flush_pickup_line()
            self._shot_armed_hw = next_hw
        elif not armed:
            if self._shot_armed_hw:
                self._close_epoch_census("full_disarm")
                self._flush_pickup_line()     # same close, the 2-arg legacy hook's path
            self._shot_armed_hw = False   # a full disarm always closes the hw window too
            self._clear_gameplay_structure_proof()
            self._physical_shot_epoch = 0
            self._clear_micro_candidate()

    def _clear_gameplay_structure_proof(self) -> None:
        self._gameplay_structure_verified = False
        self._gameplay_structure_proof_epoch = 0
        # A proof clear is an epoch/identity boundary.  In particular,
        # notify_physical_shot_start clears this before installing the next epoch;
        # capless rise evidence from the previous press must not survive it.
        self._reset_detfill_nogreen_evidence()

    def _close_epoch_census(self, reason: str) -> None:
        """STRUCTURE NECROPSY (see __init__): one ERROR line when a hw epoch that saw
        detected frames closes without EVER latching the structure proof -- the exact
        silent-shot signature the engine logs as ownership_structure_stamp_missing.
        Pure diagnostics; resets the census either way."""
        c = self._ep_census
        try:
            if (int(c.get("epoch", 0)) > 0 and int(c.get("emit_det", 0)) >= 10
                    and not c.get("latched")):
                _acq_logger.error(
                    "STRUCTURE NECROPSY epoch=%d (%s): never latched under this press"
                    " - emit_det=%d qual_det=%d qual_green=%d max_fresh_up=%d"
                    " authorized_frames=%d last_stage=%s color=%s det_state=%s"
                    " (qual_det=0 with emit_det>0 => read()'s colour path starved"
                    " the latch: check meter_color vs the on-screen meter)",
                    int(c.get("epoch", 0)), str(reason), int(c.get("emit_det", 0)),
                    int(c.get("qual_det", 0)), int(c.get("green", 0)),
                    int(c.get("max_fresh_up", 0)), int(c.get("authorized", 0)),
                    str(c.get("last_stage", "") or "-"),
                    str(getattr(self, "_meter_color", "?")),
                    str(getattr(self, "_det_state", "?")))
        except Exception:
            pass
        self._ep_census = {"epoch": 0, "emit_det": 0, "qual_det": 0, "green": 0,
                           "max_fresh_up": 0, "latched": False, "authorized": 0,
                           "last_stage": ""}

    def _clear_per_shot_rise_evidence(self) -> None:
        """Drop the RISE evidence of the previous press so the next one can re-prove itself.

        THE SILENT-SHOT BUG (measured 2026-08-29: 11 of 19 presses produced no fire, no abort,
        no log at all). Clearing the structure proof at a press is not enough, because the proof
        can only be RE-LATCHED while `monotonic_rise` holds -- and monotonic_rise is computed
        from evidence that spans the press when the lock BRIDGES it. A bridged lock carries the
        previous shot's tail (90-95%) or the inter-shot plateau (~42-47%) in its fill history, so
        the new meter starting at 0 reads as a DROP, not a rise: rise_state leaves "rising",
        `_fresh_up_n` is starved, no latch fires, and AutomationEngine discards every ownership
        frame on the stamp clause -- silently, since no episode ever opens (see the census the
        engine now emits as `ownership_structure_stamp_missing`).

        The engine's requirement is explicit: publish gameplay_structure_verified with
        gameplay_structure_epoch == N on the fresh rise frames of press N, BEFORE fill crosses
        ~40% (~250-300ms of the ~650ms rise). That is only reachable if the rise counters start
        from this shot's own frames.

        STATUS (be honest): this is PRINCIPLED but UNVALIDATED. A synthetic bridged-press replay
        (shot A to peak -> new press while the lock is held at 0% -> shot B's rise) latches proof
        at frame 1 / 14.9% fill BOTH with and without this reset, so it does not reproduce the
        live failure and this change is not proven to fix it. It is kept because rise evidence
        crossing a press boundary is wrong on its own terms -- the same reason the structure
        proof beside it is cleared here. `_peak_fill` was deliberately left OUT of the reset: it
        feeds other peak/stale logic and clearing it was not justified by any evidence. The
        engine's new per-press census (`ownership_structure_stamp_missing`, with
        `stamp_epoch_seen` = 0 vs N-1) will name the real upstream mode on the next live run.

        Scope: per-shot EVIDENCE only. Pixel/tracker state (box/tmpl/conf) is deliberately left
        alone -- the bridge-vs-cold-acquire policy remains its sole owner, exactly as
        notify_physical_shot_start documents -- so this narrows what crosses a press without
        dropping a lock that is still on the meter. Fail-closed: it can only DELAY proof, never
        manufacture it; every engine gate (3 unique frames, first sight <= 40%, >= 3.0pp rise,
        live current-epoch stick-up) is untouched.
        """
        # A validated sub-pixel D/off describes the physical meter scale, not rise
        # evidence.  Preserve it across a bridged press and guard it against the first
        # new-shot box height in _measure_fill_in_box.  Base positions are temporal and
        # must never cross the press, so their history is still cleared.
        _carry_subpx = (getattr(self, "_subpx_D", None) is not None
                        and getattr(self, "_subpx_off", None) is not None)
        if not _carry_subpx:
            self._subpx_D = None; self._subpx_provisional = False
            self._subpx_off = None
        self._subpx_carry_pending = bool(_carry_subpx)
        self._subpx_camera_ref = None
        self._subpx_camera_scale = 1.0
        self._det_scale_reference = None
        self._det_scale_pending = None
        self._det_track_scale_match = None
        self._last_subpx_transform = None
        for _attr, _val in (("_fresh_up_n", 0),
                            ("_fresh_prev_fill", None),
                            ("_fresh_rise_left", 0),
                            ("_fresh_lock_streak", 0),
                            # detector-fill chevron streak: green seen under the OLD
                            # press must not seed the new press's structure latch
                            ("_df_green_streak", 0),
                            ("_static_fill_n", 0),
                            ("_rep_fill_prev", None),
                            ("_rise_recent_n", 0),
                            ("_lock_ever_rose", False),
                            ("_subpx_seed", []),
                            ("_subpx_base_hist", []),
                             # Coarse detector-box scale is shot/lock scoped too.
                             # Clearing only the reference makes the next valid
                             # coarse sample mint a distinct local ruler epoch.
                             ("_coarse_denom_ref", None)):
            if hasattr(self, _attr):
                try:
                    setattr(self, _attr, _val)
                except Exception:
                    pass
        _hist = getattr(self, "_rep_fill_hist", None)
        if _hist is not None:
            try:
                _hist.clear()
            except Exception:
                pass
        _occ_hist = getattr(self, "_det_occ_direct", None)
        if _occ_hist is not None:
            try:
                _occ_hist.clear()
                self._det_occ_key = None
                self._det_occ_locator_source_positive = False
                self._det_occ_recovered = False
                self._det_occ_support = 0.0
                self._det_occ_kind = ""
                self._det_occ_censored_reject = False
                self._det_occ_current_full_negative = False
                self._det_occ_current_source_age = float("inf")
                self._det_occ_negative_bridge_used = False
            except Exception:
                pass

    def _latch_gameplay_structure_proof(self, shot_epoch) -> None:
        """Attach structural evidence to its captured epoch, never the mutable current epoch."""
        try:
            parsed_epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            parsed_epoch = 0
        self._gameplay_structure_proof_epoch = (
            parsed_epoch if 0 < parsed_epoch <= 0xFFFFFFFFFFFFFFFF else 0)
        self._gameplay_structure_verified = self._gameplay_structure_proof_epoch != 0
        if (self._gameplay_structure_verified
                and self._ep_census.get("epoch") == self._gameplay_structure_proof_epoch):
            self._ep_census["latched"] = True

    # ------------------------------------------------------------------ #
    #  [ORION_PLAYER_ANCHOR] the press window, on the FRAME clock
    # ------------------------------------------------------------------ #
    def _publish_press_window(self, ts) -> None:
        """Hand the locator the press it cannot see.

        The orchestrator arms this reader (``notify_physical_shot_start`` /
        ``set_shot_state``) on the control thread, with no frame timestamp; the locator
        judges everything on the FRAME clock. Republishing the arm here -- at the
        production boundary, with this frame's ts -- puts the press in the locator's own
        time base at a cost of at most one frame of quantisation, and needs no new engine
        message and no change to the async locator wrapper.

        Inert unless ORION_PLAYER_ANCHOR is on. Never raises: a broken anchor must cost the
        reader nothing.
        """
        if _player_anchor is None:
            return
        try:
            now = float(ts) if ts is not None else _time.monotonic()
            self._pa_last_ts = now
            # [ORION_CV_TIPLESS_ARMED 2026-09-15] The ARM is not the anchor. The anchor needs
            # WHERE (a nameplate search, real per-frame cost, default OFF); the tipless
            # acceptance path needs only WHEN -- "a press is open and the meter is due" -- and
            # ships ON. Publishing the press window is one tuple swap per press edge, so the
            # two switches are separated here rather than making the tipless path depend on a
            # feature nobody has turned on.
            if not (_player_anchor.enabled() or self._pa_arm_only()
                    or getattr(self, "_tracking_meter_style", "") == "pill"):
                if self._pa_epoch:
                    self._pa_epoch = 0
                    _player_anchor.ARM.reset()
                return
            epoch = int(self._physical_shot_epoch or 0)
            # [ORION_SHOT_GATE_RELEASE 2026-09-15] A press the engine has already answered
            # (release edge issued, or the player cancelled) is OVER, even while the reader's
            # hardware arm window stays open for the post-release tail. Without this term the
            # marker's close would be undone by the very next frame's republish.
            armed = (bool(self._shot_armed_hw) and epoch != 0
                     and epoch != int(self._pa_released_epoch or 0))
            if armed and epoch != self._pa_epoch:
                self._pa_epoch = epoch
                # [ORION_SHOT_GATE_TYPE 2026-09-15] The engine ships its classification with
                # the arm, so the expectation window is this shot type's own. An empty type
                # (old engine, stick/probe edge, local self-arm) keeps the union -- see
                # player_anchor.onset_window_ms.
                _player_anchor.ARM.note_press(
                    epoch, now, self._pa_shot_type_for(epoch), self._pa_rhythm)
                # a new press is a new player position: drop the plate TRACK, keep identity
                _player_anchor.ANCHOR.reset(keep_identity=True)
                # [ORION_PICKUP_PER_PRESS 2026-09-15] LAST, for the same reason the release
                # path notes the release first: arming the anchor is BEHAVIOUR, the PICKUP
                # record is only forensics. This press's record is opened HERE, by the press
                # itself, so that every press gets a line at its close -- including the ones
                # the locator never found a meter for, which are precisely the misses the
                # owner is complaining about. Any predecessor that never reached a close (a
                # dropped release marker, an arm that fell with no frame to notice it) is
                # flushed first, so no record is overwritten unlogged. One operation: the
                # control thread's notify_physical_shot_start may already have rolled this
                # same press, and a bare flush here would log THIS press's empty record.
                # [ORION_CV_TIPLESS_ARMED] CV remains behind the ANCHOR switch: with
                # arm-only publication there is no plate search. Pill has a separate
                # YOLO/reader measurement below and needs its own pickup census.
                if (_player_anchor.enabled()
                        or getattr(self, "_tracking_meter_style", "") == "pill"):
                    self._roll_pickup_record(epoch)
            elif not armed and self._pa_epoch:
                # release FIRST: the anchor stopping is behaviour, the PICKUP line is only
                # forensics, and a broken log line must never leave the anchor armed.
                closing = self._pa_epoch
                self._pa_epoch = 0
                _player_anchor.ARM.note_release(closing, now)
                if (_player_anchor.enabled()
                        or getattr(self, "_tracking_meter_style", "") == "pill"):
                    self._flush_pickup_line()
        except Exception:
            pass

    @staticmethod
    def _pa_arm_only() -> bool:
        """[ORION_CV_TIPLESS_ARMED] Publish the press window even with the anchor switched off?

        True whenever a consumer needs WHEN but not WHERE. Today that is exactly the locator's
        tipless acceptance path, which is on by default; read per call so a live toggle takes
        effect without a restart, like ``player_anchor.enabled()`` itself.
        """
        try:
            v = _os.environ.get("ORION_CV_TIPLESS_ARMED", "").strip()
            return float(v) > 0.0 if v else True
        except Exception:
            return True

    def _flush_tipless_line(self) -> None:
        """One `TIPLESS LOCK:` ERROR line per press whose lock the tipless path produced.

        [ORION_CV_TIPLESS_ARMED 2026-09-15] Emitted from the reader, not from the locator: the
        locator runs on the detector worker thread, where a logging call takes the handler lock
        on the same thread the frame budget belongs to. The locator writes a small record and
        this drains it on the reader's own thread, at most once per press.

        ERROR, not WARNING, for the same reason the PICKUP: line is ERROR -- every sidecar
        WARNING shares one 1 s throttle slot that the press's own arm receipt has already
        taken, so a WARNING here would be dropped on exactly the presses it describes.
        """
        try:
            base = getattr(getattr(self, "_meter_detector", None), "_base", None)
            rec = getattr(base, "tipless", None)
            if not isinstance(rec, dict) or rec.get("logged"):
                return
            if int(rec.get("epoch", 0) or 0) <= 0 or float(rec.get("fill", -1.0)) < 0.0:
                return              # no press, or the tipless path never produced this lock
            rec["logged"] = 1
            _acq_logger.error(
                "TIPLESS LOCK: epoch=%d fill=%.1f rise_frames=%d conf=%.2f",
                int(rec.get("epoch", 0)), float(rec.get("fill", -1.0)),
                int(rec.get("rise_frames", 0)), float(rec.get("conf", 0.0)))
        except Exception:
            pass

    def _pa_shot_type_for(self, epoch: int) -> str:
        """The engine's classification for `epoch`, or "" when none was delivered for it.

        Epoch-keyed on purpose: the type arrives on the control thread and the press is
        republished on the frame clock, so a type left over from the PREVIOUS shot must never
        narrow this one's onset window -- an unknown type costs a wider window, a wrong type
        costs a refused meter.
        """
        return (self._pa_shot_type
                if int(self._pa_shot_type_epoch or 0) == int(epoch or 0) else "")

    def notify_physical_shot_type(self, shot_epoch=0, shot_type="", rhythm=False) -> None:
        """The engine's shot type for a press (from the native shot_gate_arm command).

        [ORION_SHOT_GATE_TYPE 2026-09-15] Called twice at most per shot: once with the arm
        (the type the press edge classified) and, when the engine's blind 200 ms grace
        re-types a Standstill into a fade, once more on the SAME epoch. The second call must
        NOT move the press, so an already-armed epoch is updated in place.

        Additive and fail-open: an old engine never calls it and the union window stands.
        """
        try:
            epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            epoch = 0
        if not (0 < epoch <= 0xFFFFFFFFFFFFFFFF):
            return
        self._pa_shot_type = str(shot_type or "")[:24].strip()
        self._pa_rhythm = bool(rhythm)
        self._pa_shot_type_epoch = epoch
        if _player_anchor is None:
            return
        try:
            if _player_anchor.enabled() and self._pa_epoch == epoch:
                _player_anchor.ARM.note_shot_type(
                    epoch, self._pa_shot_type, self._pa_rhythm)
        except Exception:
            pass

    def notify_physical_shot_release(self, shot_epoch=0, release_ms=None,
                                     reason="release") -> bool:
        """The engine issued this press's release edge (or cancelled the press). -> closed?

        [ORION_SHOT_GATE_RELEASE 2026-09-15] The press window used to end only when the
        reader's hardware arm fell, i.e. up to ORION_ANCHOR_ARM_S (2.5 s) after a meter whose
        whole life is under 1.5 s. That left the nameplate anchor running through the post-
        release tail and left a RETIRED press able to license a sub-floor candidate on the
        next screen. The engine knows the instant exactly, on every release path, so it says
        so.

        A marker for an epoch other than the live one is ignored: a late message for a
        retired press must never close the press that replaced it.
        """
        try:
            epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            epoch = 0
        current = int(self._physical_shot_epoch or 0)
        if epoch and current and epoch != current:
            return False
        epoch = epoch or current
        if epoch <= 0:
            return False
        self._pa_released_epoch = epoch
        # [ORION_READER_PRESS_WITHHOLD_SUMMARY] the press is CLOSED: one ERROR line naming what
        # each publication layer kept from the engine on this shot. Before the anchor early-out
        # below -- the census is about the reader, not about the anchor being installed. Guarded
        # because this method is also borrowed unbound by press-window harnesses that carry no
        # publication state at all: a forensics census may never break the release hook.
        try:
            self._flush_press_withhold_summary(str(reason or "release"))
        except AttributeError:
            pass
        if _player_anchor is None:
            if getattr(self, "_tracking_meter_style", "") == "pill":
                self._flush_pickup_line()
            return True
        try:
            if self._pa_epoch == epoch:
                self._pa_epoch = 0
                _player_anchor.ARM.note_release(epoch, self._pa_last_ts)
                if (_player_anchor.enabled()
                        or getattr(self, "_tracking_meter_style", "") == "pill"):
                    self._flush_pickup_line()
        except Exception:
            pass
        # The control-thread press can close before its first frame publishes _pa_epoch.
        # The Pill record was already opened on that press; do not lose its miss line.
        if getattr(self, "_tracking_meter_style", "") == "pill":
            self._flush_pickup_line()
        return True

    def _open_pickup_record(self, epoch) -> None:
        """A new press is live: open its pickup record on the locator.

        [ORION_PICKUP_PER_PRESS 2026-09-15] The record carries `logged`, so opening it is
        what re-arms the next flush. Never raises and never touches detection -- the locator
        method writes one dict and nothing else.

        `self._meter_detector` is the process-wide ``AsyncMeterLocator`` (see __init__) and
        `_base` is its proposer. CV owns its existing record; Pill gets the same schema
        on its YOLO proposer so the orchestrator's existing pickup join can read it.
        """
        try:
            base = getattr(getattr(self, "_meter_detector", None), "_base", None)
            opener = getattr(base, "open_pickup_record", None)
            if callable(opener):
                opener(int(epoch))
            elif self._tracking_meter_style == "pill" and base is not None:
                ep = int(epoch)
                if ep <= 0:
                    return
                prior = getattr(base, "pickup", None)
                if isinstance(prior, dict) and int(prior.get("epoch", 0) or 0) == ep:
                    return
                base.pickup = {
                    "epoch": ep, "first_sight_fill": -1.0,
                    "first_sight_ms_after_press": -1.0, "anchor_used": 0,
                    "anchor_conf": 0.0, "refused_outside_patch": 0,
                    "expect_accept": 0, "anchor_ms": 0.0, "logged": 0,
                }
        except Exception:
            pass

    def _note_pill_pickup(self, ts, fill) -> None:
        """Record the first valid Pill white-fill read on the frame clock.

        YOLO proposes a box but has no column-height estimate. The Pill landmark
        ruler supplies the fill only after the reader measures that box. This is
        therefore a first *valid measured fill*, not raw YOLO proposal latency.
        Epoch and frame-time checks reject old in-flight frames and post-release
        measurements; this is forensics only and never changes acceptance.
        """
        try:
            base = getattr(getattr(self, "_meter_detector", None), "_base", None)
            p = getattr(base, "pickup", None)
            if (not isinstance(p, dict) or p.get("logged") or _player_anchor is None
                    or callable(getattr(base, "open_pickup_record", None))):
                return
            ep, press_ts, _kind, armed, _rhythm, _release_ts = _player_anchor.ARM.state()
            if (not armed or int(ep) <= 0 or int(p.get("epoch", 0)) != int(ep)
                    or int(self._physical_shot_epoch or 0) != int(ep)
                    or float(p.get("first_sight_fill", -1.0)) >= 0.0):
                return
            dt_ms = 1000.0 * (float(ts) - float(press_ts))
            value = float(fill)
            if not (np.isfinite(dt_ms) and dt_ms >= 0.0
                    and np.isfinite(value) and 0.0 < value <= 100.0):
                return
            p["first_sight_fill"] = round(value, 1)
            p["first_sight_ms_after_press"] = round(dt_ms, 1)
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass

    def _roll_pickup_record(self, epoch) -> None:
        """Close whatever record is open and open `epoch`'s -- in that order, once.

        [ORION_PICKUP_PER_PRESS 2026-09-15] Both press edges call this: the control thread's
        notify_physical_shot_start and the frame clock's _publish_press_window. Rolling has
        to be one operation because the flush is unconditional: whichever edge arrives second
        would otherwise flush the record the FIRST edge just opened for this very press, and
        emit its line before a single frame had been looked at (first_sight_fill=-1.0 on a
        press whose meter was found 40 ms later). Guarding the flush on "the open record
        belongs to a different press" makes the second call a no-op, so the two edges cannot
        produce two lines -- or one wrong one -- for one press.
        """
        try:
            ep = int(epoch or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if ep <= 0:
            return
        try:
            base = getattr(getattr(self, "_meter_detector", None), "_base", None)
            open_rec = getattr(base, "pickup", None)
            open_ep = int(open_rec.get("epoch", 0) or 0) if isinstance(open_rec, dict) else 0
        except Exception:
            open_ep = 0
        if open_ep != ep:
            self._flush_pickup_line()
        self._open_pickup_record(ep)

    def _flush_pickup_line(self) -> None:
        """One PICKUP: line per press -- where and when the meter was first seen, and
        whether the anchor is what found it. This is the number the owner's complaint is
        about ("late reads"), so it is logged unconditionally, not sampled.

        [ORION_PICKUP_PER_PRESS 2026-09-15] A press whose meter was NEVER picked up is the
        most valuable line here, not a line to skip: it flushes with first_sight_fill=-1.0
        and anchor_used=0, which is what makes `grep PICKUP:` a complete census of presses
        rather than a list of the hits.

        [ORION_PICKUP_LEVEL 2026-09-15] ERROR, NOT WARNING -- THIS is why the line was
        silent live, not the record bookkeeping. RemotePlaySession.cpp:2884 relays the
        sidecar's stderr, and EVERY sidecar WARNING line shares ONE global slot of
        kSidecarWarnThrottleMs = 1000 ms (RemotePlaySession.h:530); ERROR/CRITICAL bypass it
        (`isError`). A press emits the orchestrator's own `SHOT-GATE ARM RECEIPT` WARNING on
        the control thread and this flush one frame (<=17 ms) later on the processing
        thread, so the receipt took the slot on EVERY press and the PICKUP line lost it on
        EVERY press: 30 presses, 0 lines. Before the flush moved to the press it fired at
        the hw-disarm edge instead -- ORION_SHOT_GATE arm windows are 20 s
        (`_shot_gate_max_seconds`), i.e. once per RALLY, far from any other warning -- which
        is exactly why the same code logged 2 lines for 37 presses. Same remedy, same
        reason, as the "METER DETECTOR load" line above and the probe diagnostics' explicit
        bypass. The line still stays out of the customer Activity ring: it carries nine
        key=value pairs, so UiNotificationPolicy's counter-line heuristic classifies it as
        engineering.
        """
        try:
            base = getattr(getattr(self, "_meter_detector", None), "_base", None)
            p = getattr(base, "pickup", None)
            if not isinstance(p, dict) or p.get("logged"):
                return
            # A record that no press ever opened (epoch 0, the locator's blank) describes
            # nothing; only a real press epoch is a line.
            try:
                if int(p.get("epoch", 0) or 0) <= 0:
                    return
            except (TypeError, ValueError, OverflowError):
                return
            p["logged"] = 1
            st = getattr(base, "stats", {}) or {}
            _acq_logger.error(
                "PICKUP: epoch=%d first_sight_fill=%.1f first_sight_ms_after_press=%.1f "
                "anchor_used=%d anchor_conf=%.2f refused_outside_patch=%d "
                "expect_accept=%d anchor_ms=%.2f patch_hits=%d",
                int(p.get("epoch", 0)), float(p.get("first_sight_fill", -1.0)),
                float(p.get("first_sight_ms_after_press", -1.0)),
                int(p.get("anchor_used", 0)), float(p.get("anchor_conf", 0.0)),
                int(p.get("refused_outside_patch", 0)), int(p.get("expect_accept", 0)),
                float(p.get("anchor_ms", 0.0)), int(st.get("anchor_patch_hit", 0)))
        except Exception:
            pass

    def notify_physical_shot_start(self, shot_epoch=0) -> None:
        """Start a new hardware-owned shot even if the prior arm window is still open.

        Long Go-To windows intentionally outlive the old two-second frame cap.  Without an
        explicit edge, a quick next shot would see ``_prev_hw_armed`` still true and inherit the
        previous shot's tracker/calibration epoch.  Close the old bookkeeping, then make the next
        read observe a genuine rising hardware edge.  Pixel/tracker state is left intact here so
        the reader's existing bridge-vs-cold-acquire policy remains the sole owner of that choice.
        """
        # Explicit native/controller shot identity is authoritative even when a long hardware
        # arm window has not fallen yet. Structure from the prior press must never cross it.
        # Clear proof BEFORE installing the new identity. The stdin/control thread may run
        # between detector bytecodes; this ordering can expose false/old or false/new, never
        # a prior proof paired to the new shot epoch.
        self._close_epoch_census("next_press")
        self._clear_gameplay_structure_proof()
        self._clear_per_shot_rise_evidence()
        self._clear_micro_candidate()
        # [ORION_READER_POST_RELEASE_YIELD] an explicit new shot identity ends the
        # post-release window exactly like the read()-side arm edge does.
        self._pr_release_pending = False
        self._pr_frozen_n = 0
        self._pr_prev_fill = None
        # RELEASE ORACLE (diagnostic): a new press ends the previous release's settle window
        # whether or not it ever filled, so no record is carried across a shot boundary.
        self._ro_flush("next_press")
        try:
            parsed_epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            parsed_epoch = 0
        _prev_epoch = int(getattr(self, "_physical_shot_epoch", 0) or 0)
        self._physical_shot_epoch = (
            parsed_epoch if 0 < parsed_epoch <= 0xFFFFFFFFFFFFFFFF else 0)
        self._ep_census["epoch"] = int(self._physical_shot_epoch)
        # [ORION_PICKUP_PER_PRESS 2026-09-15] OPEN THE FORENSICS RECORD ON THE CONTROL
        # THREAD, WHERE THE PRESS IDENTITY IS BORN.
        #
        # _publish_press_window opens it too, from the frame clock, and that is still the
        # only place the ANCHOR is armed -- behaviour is untouched here. But the frame-clock
        # open can lose a press outright: the processing loop pushes
        # set_shot_state(..., armed_hw) every frame from its own snapshot of the gate
        # deadlines, and a snapshot taken just before _arm_shot_gate extended them lands
        # AFTER this method with armed_hw=False, whose `hw disarm` branch zeroes
        # _physical_shot_epoch. The next detect() then sees epoch 0, never publishes this
        # press, and the press disappears from the census with no line at all. The record is
        # pure forensics, so opening it from the authoritative press edge costs nothing and
        # makes `grep PICKUP:` a complete census whatever the gate does afterwards.
        #
        # Flush FIRST (the predecessor's line) and only then open, exactly as the frame-clock
        # path does; open_pickup_record is idempotent per epoch so the two paths cannot
        # produce two records for one press.
        if self._physical_shot_epoch:
            try:
                if (self._tracking_meter_style == "pill"
                        or (_player_anchor is not None and _player_anchor.enabled())):
                    self._roll_pickup_record(self._physical_shot_epoch)
            except Exception:
                pass
        # STALE-LOCK DROP AT THE PRESS.
        #
        # This method deliberately leaves pixel/tracker state intact (see the
        # docstring) so a genuinely continuing meter can bridge across a new
        # shot identity. That is right for a meter that is still rising; it is
        # WRONG for a lock that outlived its shot, and on 2K27 that case
        # dominates -- ~half of published locks sit on decor or on the previous
        # shot's meter.
        #
        # The consequence is not cosmetic. AutomationEngine opens its ownership
        # episode AT the press and refuses to own a shot whose FIRST observed
        # fill exceeds anchorMaxFirstFillPct (40%), because that cannot be the
        # beginning of a new meter. Live 2026-08-27, every shot aborted with
        # `SHOT NOT OWNED: reason=ownership_proof_incomplete samples=0
        # first_fill=51.0` -- the engine's first sample WAS the stale lock. The
        # bot armed and then refused itself, on every single shot.
        #
        # A brand-new shot's meter starts near empty, so a HELD HIGH FILL at a
        # fresh press is stale by definition. Drop it and let the next frame run
        # the ordinary cold acquire on the real rise. Self-limiting: it only
        # fires on a genuine new epoch that inherited a high fill, which is
        # exactly the poisoning case, and never touches a low/rising lock.
        # [ORION_READER_GHOST_PRESS_BREAK] Arm the per-press ghost guard (see __init__): a new
        # epoch has, by definition, not yet seen its own meter low. Consumed by the post-press
        # breaker at the production boundary; inert once the low sighting disarms it.
        if self._physical_shot_epoch != 0 and self._physical_shot_epoch != _prev_epoch:
            # Flush the PREVIOUS press's eviction summary if its guard never stood down
            # (window expired with the leftover still on screen) -- exactly one summary
            # line per press either way.
            self._ghost_press_flush_summary("window_end")
            # [ORION_READER_PRESS_WITHHOLD_SUMMARY] and the withhold census for the press that
            # just ended, if its own release never closed it (abort / cancelled / tap).
            self._flush_press_withhold_summary("next_press")
            self._pw_epoch = int(self._physical_shot_epoch or 0)
            self._press_low_seen = False
            self._press_ghost_reads.clear()
            self._press_ghost_zone = None
            self._press_ghost_level = None
            self._press_ghost_zone_low_n = 0
            self._press_ghost_zero_n = 0
            self._press_ghost_full_nofind_n = 0
            self._press_ghost_log_ts = -1.0e9
            self._press_ghost_log_zone = None
            self._press_ghost_summary_epoch = int(self._physical_shot_epoch or 0)
            # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] fresh press -> fresh serve clock
            self._press_last_pub_fill = None
            self._press_last_pub_ts = None
            self._press_onset_cand = None
            self._press_implausible_n = 0
            self._press_implausible_logged = False
            self._press_rise_run = 0
            # [ORION_READER_FRESH_AFTER_GHOST / _BOX_LATCH] both are per-press artefacts.
            self._press_fresh_zone = None
            self._press_fresh_zone_logged = False
            self._clear_box_latch('new_press')
        # ROBUST held fill: the ghost's read flickers to 0.0 on pan-blurred frames (measured
        # epochs 41/45: 0.00 at the press instant, 88.9/45.1 one frame later), so judge the
        # recent nonzero detector-fill history alongside the instantaneous value. The history
        # deque is pruned to ~0.5s by the detector-fill path, so it cannot resurrect a meter
        # from a previous shot cycle.
        _detector_holds_lock = bool(
            getattr(self, "_det_state", "idle") == "locked"
            or getattr(self, "_det_active_box", None) is not None)
        # Native ownership consumes detector-fill's row-quantized value.  Stale/bridge
        # classification must use that same ruler: a 41.5 sub-pixel / 38.7 coarse sample
        # is ownable, so it must bridge rather than seed a high-fill quarantine here.
        _held_now = float(getattr(
            self, "last_coarse" if _detector_holds_lock else "last_fill", 0.0) or 0.0)
        _held_recent = 0.0
        try:
            _held_hist = (self._det_coarse_fill_hist
                          if _detector_holds_lock else self._det_fill_hist)
            for _, _hf in _held_hist:
                if float(_hf) > _held_recent:
                    _held_recent = float(_hf)
        except Exception:
            _held_recent = 0.0
        _held_lock = (self.box is not None
                      or getattr(self, "_det_last_box", None) is not None
                      or getattr(self, "_det_state", "idle") == 'locked')
        if (self._stale_press_drop_pct > 0.0
                and self._physical_shot_epoch != 0
                and self._physical_shot_epoch != _prev_epoch
                and _held_lock
                and max(_held_now, _held_recent) >= self._stale_press_drop_pct):
            _stale_fill = max(_held_now, _held_recent)
            # Captured BEFORE the clears below zero it: the quarantine zone needs the
            # dropped lock's position whichever side (detector lifecycle or colour
            # reader) was holding it.
            _zb = (getattr(self, "_det_last_box", None)
                   or getattr(self, "box", None))
            self.conf = 0.0
            self.box = None
            self.tmpl = None
            self._consec = 0
            self._peak_fill = 0.0
            self._coast_n = 0
            self._capless_since = None
            # The detector lifecycle holds its own copy of the lock (state/box/history) and
            # re-seats self.box from it one frame later, which made the original drop a
            # single-frame no-op under detector-authoritative serving. The lifecycle dies
            # with the lock -- and WITHOUT arming the warm re-acquire memory, which would
            # re-latch the same ghost within its 1.2s window.
            try:
                # Seed the quarantine zone from the dropped lock's position (captured
                # above): we KNOW where the leftover meter is (we just dropped it for
                # holding a high fill at a fresh press), so post-press relocks there are
                # suppressed immediately instead of publishing 2-3 noisy reads first
                # (the false-episode source measured in the replay A/B).
                if self._ghost_press_break and _zb is not None:
                    self._press_ghost_zone = (float(_zb[0]) + float(_zb[2]) * 0.5,
                                              float(_zb[1]) + float(_zb[3]) * 0.5)
                    self._press_ghost_level = float(_stale_fill)
                    self._press_ghost_zone_low_n = 0
                    self._press_ghost_zero_n = 0
                    self._press_ghost_full_nofind_n = 0
                self._det_reset_lock_state()
                self._det_warm_pos = None
                self._det_warm_ts = -1.0e9
                self._det_active_box = None
            except Exception:
                pass
            # [ORION_READER_GHOST_FORGET_LOCATOR / _FRESH_AFTER_GHOST] The proposer's own
            # positional memory is a seed too: without this the very next proposal is the
            # dropped leftover's own column, bridged on co-location alone (no green tip, no
            # outline support needed), and the evict/re-lock loop starts. The zone also keeps
            # a fresh-onset requirement for the rest of this press.
            self._det_forget_locator_position('stale_press_drop', box=_zb,
                                              ts=self._frame_ts_last)
            self._arm_press_fresh_zone(_zb, 'stale_press_drop')
            # Name it in the log. Offline replay could NOT reproduce the stale
            # state this fixes (the harness's synthetic presses do not align with
            # real shot cycles), so the live log is the only arbiter of whether
            # this fires and whether it helps. ERROR level for the same reason as
            # _scope_reject: the native relay throttles sidecar WARNINGs.
            _acq_logger.error(
                "STALE LOCK DROPPED AT PRESS: epoch=%d held_fill=%.1f%% (>= %.1f%%) "
                "- the engine would have refused to own this shot",
                self._physical_shot_epoch, _stale_fill, self._stale_press_drop_pct)
        elif (self._press_onset_plaus and self._ghost_press_break
                and self._physical_shot_epoch != 0
                and self._physical_shot_epoch != _prev_epoch
                and _held_lock
                and 0.0 < max(_held_now, _held_recent) < self._stale_press_drop_pct):
            # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] BRIDGED PRESS: the stale drop above
            # deliberately spares a LOW/RISING lock ("a genuinely continuing meter can
            # bridge across a new shot identity" -- the docstring's design intent), and on
            # a rapid re-press that lock IS a real meter mid-rise (measured
            # session_20260830_191051 press 16, 641ms after press 15: the held lock read
            # 27->34 rising at the press and the shot continued on-screen). Arming the
            # per-press guard against it starves that real meter: the guard demands this
            # press's OWN low sighting, which a meter already at 30-40%% can never give.
            # A low VALUE alone is not continuity. Epoch 38 carried a static 14.3% court/
            # player false box across the press and the engine eventually owned it before
            # the real fade meter rendered. Bridge only when the immediately preceding
            # canonical-fill pair genuinely rose and the locator corroborated the held box
            # recently. Otherwise retire the unproven pre-arm identity and require the new
            # press to acquire fresh evidence; no quarantine is seeded for a low unknown.
            if self._press_bridge_has_fresh_rise():
                self._press_low_seen = True
                self._press_ghost_zone = None
                self._press_ghost_level = None
                self._press_ghost_zone_low_n = 0
                self._press_ghost_full_nofind_n = 0
                _acq_logger.debug(
                    "PRESS BRIDGED BY LOW/RISING LOCK: epoch=%d held_fill=%.1f%% "
                    "(fresh rising continuity proved; the continuing meter keeps serving)",
                    self._physical_shot_epoch, max(_held_now, _held_recent))
            else:
                # Name the object being refused BEFORE the seeds are cleared: the surgical
                # forget needs the ghost's column to know which first-sight pairs are its own.
                _ub = self.box or getattr(self, "_det_active_box", None)
                self.conf = 0.0
                self.box = None
                self.tmpl = None
                try:
                    self._det_reset_lock_state()
                    self._det_warm_pos = None
                    self._det_warm_ts = -1.0e9
                    self._det_active_box = None
                except Exception:
                    pass
                self._det_forget_locator_position('unproven_low_press_drop', box=_ub,
                                                  ts=self._frame_ts_last)
                _acq_logger.error(
                    "UNPROVEN LOW LOCK DROPPED AT PRESS: epoch=%d held_fill=%.1f%% "
                    "(no fresh rising continuity; new press must reacquire)",
                    self._physical_shot_epoch, max(_held_now, _held_recent))
        if self._prev_hw_armed:
            if self._calibrator is not None:
                try:
                    self._end_hw_shot(self._calibrator)
                except Exception:
                    pass
            if self._gz_window or self._gz_grade:
                try:
                    self._gz_end_shot("next_hw_arm")
                except Exception:
                    pass
        self._prev_hw_armed = False

    @property
    def gameplay_structure_verified(self) -> bool:
        """Whether meter-specific structure was proved in the current physical shot epoch."""
        return bool(
            self._shot_armed_hw and self._gameplay_structure_verified
            and self._physical_shot_epoch != 0
            and self._gameplay_structure_proof_epoch == self._physical_shot_epoch)

    @property
    def gameplay_structure_epoch(self) -> int:
        """Exact tokenized hardware-shot epoch owning the current structural proof."""
        if not self.gameplay_structure_verified:
            return 0
        return int(self._physical_shot_epoch)

    def set_fit_provider(self, fn) -> None:
        """Optional hook: callable(t_ms)->dict(fill, vel_pp_ms, sigma_pp, n, conf, age_ms) or
        None -- RegistrationPredictor.predict_fill, wired by the orchestrator. Powers the B2
        trajectory gate + dead-reckoned coast. Absent -> both features stay inert."""
        self._fit_provider = fn

    def notify_release(self, seq: int = 0, *, physical_epoch: int = 0,
                       shot_attempt: int = 0,
                       identity_verified: bool = False) -> None:
        """release_marker relay (corroboration proxy half 1 -- plan B3 [fix])."""
        if self._calibrator is not None:
            try:
                self._calibrator.note_release()
            except Exception:
                pass
        self._shot_release_seen = True
        # RELEASE ORACLE (diagnostic, see __init__): open this release's settle window. It
        # only starts a measurement buffer -- no reader state below it can see the call.
        self._ro_open(seq, physical_epoch)
        # [ORION_READER_POST_RELEASE_YIELD] OUR OWN release for the current hw window was
        # relayed: from here (until the next physical arm edge) a static >=90 lock is a spent
        # meter, and P1 may yield it. Latched unconditionally; consumption is flag-gated.
        self._pr_release_pending = True
        # Release-window diagnostic: latch the fill AND native identity atomically on the first
        # marker for this shot.  The old orchestrator stamped whichever release id was newest when
        # the delayed grade callback ran, so shot 1 could be mislabeled as shot 2.  Keeping identity
        # in the reader's per-shot epoch makes that pairing immutable.  Calibration corroboration
        # above remains independent of whether the native id is valid.
        if self._gz_grade and not self._gz_release_latched:
            self._gz_release_latched = True
            try:
                release_seq = int(seq)
            except (TypeError, ValueError, OverflowError):
                release_seq = 0
            self._gz_release_seq = release_seq if release_seq > 0 else 0
            try:
                self._gz_release_physical_epoch = max(0, int(physical_epoch))
                self._gz_release_shot_attempt = max(0, int(shot_attempt))
            except (TypeError, ValueError, OverflowError):
                self._gz_release_physical_epoch = 0
                self._gz_release_shot_attempt = 0
            self._gz_release_identity_verified = bool(
                identity_verified and self._gz_release_seq > 0
                and self._gz_release_physical_epoch > 0
                and self._gz_release_shot_attempt > 0)
            try:
                self._gz_fill_at_release = float(self.last_fill)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  RELEASE ORACLE (ORION_RELEASE_ORACLE) -- see __init__. Diagnostic only.
    # ------------------------------------------------------------------ #
    def _ro_measure(self, hsv, ph, pw, track_top, fillable_h, sy):
        """RELEASE ORACLE: (gap_px, gap_pct, green_bottom_pct) for this frame, or (None,)*3.

        Deliberately its OWN landmark pair rather than the emitted `red_top`/`cap_bot`, because
        the emitted pair cannot see the case the finding is about. A shot that lands AT the tip
        buries the green cap under the fill; the reader's cap anchor only looks for green ABOVE
        the fill and its row gate wants 15 % of the strip width lit, so on
        session_20260915_185359 seq 4/11/14/22/23 it read "no cap at all" while 3-6 green pixels
        of the arrow head were still on screen -- exactly the frames whose gap is <= 0, i.e. the
        best shots in the batch. This is the rule the finding was measured with (the forensics in
        _analysis/pixmeasure.py): the meter's own interior columns, a row counts as fill at >= 3
        white pixels and as band at >= 2 green pixels, and the fill's top is the top of the
        BOTTOM-ANCHORED run with <= 2-row gaps bridged.

        Vectorized end to end (one 3x1 morphological close, no per-row Python) and only ever
        called while a release window is open, so the detect thread pays for it for at most
        half a second per shot.
        """
        try:
            c0, c1 = (3, pw - 3) if pw > 6 else (0, pw)
            y0 = max(0, int(track_top) - int(round(34.0 * float(sy))))
            sub = hsv[y0:ph, c0:c1]
            if sub.size == 0 or fillable_h <= 0:
                return (None, None, None)
            hh = sub[:, :, 0].astype(np.int16)
            ss = sub[:, :, 1].astype(np.int16)
            vv = sub[:, :, 2].astype(np.int16)
            wrow = ((vv >= 235) & (ss <= 40)).sum(axis=1)
            grow = ((ss >= 90) & (hh >= 35) & (hh <= 95) & (vv >= 120)).sum(axis=1)
            grows = np.flatnonzero(grow >= 2)
            if grows.size == 0:
                return (None, None, None)
            wcol = np.where(wrow >= 3, 255, 0).astype(np.uint8).reshape(-1, 1)
            # odd kernel -> centred anchor -> the run does not walk a row (see meter_locator_cv)
            wcol = cv2.morphologyEx(wcol, cv2.MORPH_CLOSE, np.ones((3, 1), np.uint8))
            widx = np.flatnonzero(wcol.ravel() > 0)
            if widx.size == 0:
                return (None, None, None)
            brk = np.flatnonzero(np.diff(widx) > 1)
            top = int(widx[brk[-1] + 1]) if brk.size else int(widx[0])
            gbot = int(grows.max())
            gap = float(top - gbot)
            return (gap, gap / fillable_h * 100.0,
                    (ph - float(y0 + gbot)) / fillable_h * 100.0)
        except Exception:
            return (None, None, None)

    def _ro_open(self, seq, physical_epoch: int = 0) -> None:
        """A release command was issued: flush the predecessor, open this one's window.

        `physical_epoch` is the SHOT-GATE epoch (the pose arm token) this release belongs to.
        It is latched HERE, at the command, because it is the id the machine line has to
        carry: the banner verdict is attributed on the same token, so the native can join the
        two instruments per shot. `seq` (the release-marker counter) stays the human line's id.
        """
        if not self._ro_on:
            return
        self._ro_flush("superseded")
        try:
            self._ro_seq = max(0, int(seq))
        except (TypeError, ValueError, OverflowError):
            self._ro_seq = 0
        try:
            self._ro_epoch = max(0, int(physical_epoch))
        except (TypeError, ValueError, OverflowError):
            self._ro_epoch = 0
        self._ro_pending = True
        self._ro_t0 = None
        self._ro_samples = []
        self._ro_frame = None

    def _ro_tick(self, ts) -> None:
        """Per-frame clock for the open window: start it, and close it when it has run out.

        Separate from _ro_note because a release whose meter vanished immediately produces no
        samples at all -- _read_fill never runs -- and such a release still has to emit its
        (unknown) line rather than sit open until the next press.
        """
        if not (self._ro_on and self._ro_pending):
            return
        try:
            now = float(ts)
        except (TypeError, ValueError):
            return
        if self._ro_t0 is None:
            self._ro_t0 = now
        elif now - self._ro_t0 >= self._ro_hi_s:
            self._ro_flush("settled")

    def _ro_note(self, ts, fill) -> None:
        """Fold one frame into the open window; emit once it has run past the settle window.

        `t0` is the first frame the reader processed after the command, not the command
        itself (the marker arrives on the control thread with no frame clock), so every
        offset here is late by at most one frame -- irrelevant to a 300-500 ms window and
        honest about what was actually measured.
        """
        try:
            now = float(ts)
        except (TypeError, ValueError):
            return
        if self._ro_t0 is None:
            self._ro_t0 = now
        dt = now - self._ro_t0
        if dt < 0.0:
            return                          # an out-of-order frame is not a settle sample
        f = self._ro_frame
        if f is not None:
            self._ro_samples.append((dt, f[0], f[1], float(fill), f[2]))
            if len(self._ro_samples) > 240:
                self._ro_samples = self._ro_samples[-240:]

    def _ro_flush(self, reason: str) -> None:
        """Emit ONE `RELEASE ORACLE:` ERROR line for the open release, then close it.

        The window is the samples between LO and HI ms after the command; the reported
        numbers are their MEDIANS, so one occluded or mid-retraction frame cannot move the
        verdict. A window with no green band anywhere in it is `verdict_proxy=unknown` --
        the measurement was not available, which is not the same as a miss.
        """
        if not (self._ro_on and self._ro_pending):
            return
        self._ro_pending = False
        samples, seq = self._ro_samples, int(self._ro_seq)
        epoch = int(self._ro_epoch)
        self._ro_samples = []
        self._ro_t0 = None
        self._ro_frame = None
        try:
            win = [s for s in samples if self._ro_lo_s <= s[0] <= self._ro_hi_s]
            if not win:
                win = list(samples)         # sparse feed: judge on what actually arrived
            gaps = [s[1] for s in win if s[1] is not None]
            if gaps:
                gap_px = float(np.median(gaps))
                gap_pct = float(np.median([s[2] for s in win if s[2] is not None]))
                g_bot = float(np.median([s[4] for s in win if s[4] is not None]))
                verdict = "green" if gap_px <= self._ro_thr_px else "miss"
            else:
                gap_px = gap_pct = g_bot = -1.0
                verdict = "unknown"
            fills = [s[3] for s in win]
            settled = float(np.median(fills)) if fills else -1.0
            rec = {"seq": seq, "gap_px": round(gap_px, 2), "gap_pct": round(gap_pct, 2),
                   "settled_fill": round(settled, 2), "green_bottom_pct": round(g_bot, 2),
                   "verdict_proxy": verdict, "n": len(win), "end_reason": str(reason)}
            self.last_release_oracle = rec
            _acq_logger.error(
                "RELEASE ORACLE: seq=%d gap_px=%.2f gap_pct=%.2f settled_fill=%.2f "
                "green_bottom_pct=%.2f verdict_proxy=%s",
                seq, gap_px, gap_pct, settled, g_bot, verdict)
            self._ro_emit_machine(epoch or seq, rec)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  RELEASE ORACLE -> native, on the banner verdict's own stdout channel.
    #
    #  [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] RemotePlaySession.cpp already parses this
    #  line (`event == "release_oracle"`): gap_px + verdict_proxy reach the engine's Shot Lead
    #  trim, the rest is forensics for the native log. The ERROR line above stays exactly as it
    #  was -- it is the human/greppable half and several diagnostics read it.
    #
    #  FRAMING is the banner verdict's, byte for byte: ONE compact-separator JSON object plus
    #  '\n', written through remote_play_orchestrator.emit_stdout_jsonl, which holds
    #  STDOUT_EMIT_LOCK so the orchestrator's, the sidecar's and this writer's lines can never
    #  interleave mid-object on the pipe the native reads. The module is looked up in
    #  sys.modules rather than imported: the orchestrator imports THIS module, so importing it
    #  back would be a cycle, and a reader running outside the sidecar (tests, replay) must not
    #  drag the whole orchestrator in just to log a diagnostic.
    #
    #  IDENTITY: `release_seq` is the SHOT-GATE EPOCH (the pose arm token), which is what the
    #  banner verdict is attributed on -- so the native joins the two instruments per shot. A
    #  release relayed without an epoch (legacy 1-arg notify_release, tests) falls back to the
    #  release-marker counter; that will simply never join a banner, which is the honest
    #  outcome for a release whose identity was never published.
    #
    #  `event` is FIRST and authoritative: it is the key every other message on this stdout
    #  channel uses and the one RemotePlaySession::handleSidecarMessage dispatches on. The
    #  native also accepts `type` as a fallback when `event` is absent, and `type` is the key
    #  the agreed schema was written with, so both are emitted: the line matches the written
    #  schema and takes the channel's own convention. The native ignores `type` whenever
    #  `event` is present, so the duplicate can never route a line twice.
    #
    #  UNGATED by ORION_GREEN_SELF_GRADE on purpose: the self-grade flag owns the green-zone
    #  window's own experiment, and the owner asked for this measurement on every release.
    #  ORION_RELEASE_ORACLE=0 turns the whole feature off (window, line and this emit together).
    # ------------------------------------------------------------------ #
    _ro_sink = None          # set_release_oracle_sink() -- tests/sidecar override

    def set_release_oracle_sink(self, fn) -> None:
        """Route the machine line somewhere other than stdout (tests, a sidecar relay)."""
        self._ro_sink = fn

    def _ro_emit_machine(self, release_seq, rec) -> None:
        payload = {
            "event": "release_oracle",
            "type": "release_oracle",
            "release_seq": int(release_seq),
            "gap_px": float(rec["gap_px"]),
            "gap_pct": float(rec["gap_pct"]),
            "settled_fill": float(rec["settled_fill"]),
            "green_bottom_pct": float(rec["green_bottom_pct"]),
            "verdict_proxy": str(rec["verdict_proxy"]),
            "t_ms": round(_time.time() * 1000.0, 1),
        }
        line = _json.dumps(payload, separators=(",", ":")) + "\n"
        sink = self._ro_sink
        if sink is None:
            _orch = _sys.modules.get("remote_play_orchestrator")
            sink = getattr(_orch, "emit_stdout_jsonl", None) if _orch is not None else None
        try:
            if callable(sink):
                sink(line)
            else:
                _sys.stdout.write(line)
                _sys.stdout.flush()
        except Exception:
            # A lost oracle line is cosmetic; it must never take the reader down.
            pass

    # ------------------------------------------------------------------ #
    #  IDLE PUBLICATION GATE (ORION_READER_IDLE_PUBLISH_GATE, default ON)
    #
    #  THE COMPLAINT. Between shots the locator locks white columns that are not meters --
    #  court lines, the scoreboard, the shot chart, the owner's own jersey. Measured on the
    #  2026-09-15 session: `DETECTOR HEALTH found=2745` against ~600 frames that carried a
    #  real meter, and one static column at [777,342,26,110] locked and dropped TWENTY times
    #  in 40 s. The engine already ignores them (no arm, no fire), so this was filed as
    #  cosmetic. It is not: the overlay draws every one of those boxes on the owner's screen,
    #  and each one is a lock the next press has to EVICT -- `STALE LOCK DROPPED AT PRESS`,
    #  followed by a re-acquire that costs the first frames of the real meter.
    #
    #  THE GATE. A lock is tracked internally exactly as before (its memory, its ruler, its
    #  eviction rules all still run); it is PUBLISHED -- handed to the engine and the overlay
    #  -- only when one of three things is true:
    #    (a) A PRESS IS ARMED. A shot always publishes, from its first accepted frame, with
    #        no history required and no delay: this is the rule that makes the gate free.
    #    (b) THE FILL ROSE >= ORION_READER_IDLE_PUBLISH_RISE_PP (3) over the last 3 reads of
    #        THE SAME column. That is the owner shooting without the bot -- the meter is real
    #        and has to stay visible. A real meter climbs ~0.16-0.25 pp/ms, i.e. 3-4 pp per
    #        60 fps frame, so it qualifies on its second read; a static false lock's fill
    #        jitters by well under a point and never does.
    #    (c) IT IS THE CONTINUATION OF AN ALREADY-PUBLISHED LOCK: the same column, within
    #        ORION_READER_IDLE_PUBLISH_CONT_S (0.5) of the last published frame. This is what
    #        keeps a shot on screen through the post-release settle, after the gate disarms,
    #        and across the brief found=False blinks the async locator produces mid-shot.
    #  Nothing else is published, and every refusal is counted as `idle_unpublished=` on the
    #  DETECTOR HEALTH line, so the gate's cost is visible in the same place its benefit is.
    #
    #  A lock that has never been published cannot BE a continuation, so an idle false lock
    #  gets no free pass from (c): (a) and (b) are the only two doors, and both of them are
    #  the presence of a real shot.
    # ------------------------------------------------------------------ #
    def _idle_publish_ok(self, detected: bool, bbox, fill: float, ts) -> bool:
        """Return whether this frame's detection may be published. Never raises."""
        try:
            if not detected:
                return False
            # A hardware press grants permission to measure, not permission to
            # publish malformed fill/geometry as current meter evidence.
            fill = float(fill)
            if not np.isfinite(fill) or not 0.0 <= fill <= 100.0:
                return False
            if ts is not None and not np.isfinite(float(ts)):
                return False
            now = float(ts) if (ts is not None and ts == ts) else _time.monotonic()
            cx = cy = None
            try:
                if bbox and len(bbox) >= 4 and int(bbox[2]) > 0 and int(bbox[3]) > 0:
                    cx = float(bbox[0]) + float(bbox[2]) * 0.5
                    cy = float(bbox[1]) + float(bbox[3]) * 0.5
            except (TypeError, ValueError, IndexError):
                cx = cy = None
            if cx is None or cy is None or not np.isfinite(cx) or not np.isfinite(cy):
                return False
            scale = max(0.5, float(self.H or 720) / 720.0)

            def _near(prev):
                # A meter on a fade slides up to ~0.6 px/ms, so the tolerance grows with the
                # gap exactly as the locator's own co-location tolerance does -- and is cut
                # off at the continuation window, because "the same column a second later" is
                # a claim about a meter's life, not about a coordinate.
                if prev is None or cx is None:
                    return False
                dt = max(0.0, now - prev[2])
                if dt > self._idle_pub_cont_s:
                    return False
                tol = (self._idle_pub_tol_px + min(90.0, 600.0 * dt)) * scale
                return abs(cx - prev[0]) <= tol and abs(cy - prev[1]) <= tol

            # (b) the RISE, measured on the last reads of THIS column only: a fill history
            # carried across a re-seat onto a different column would read as growth.
            if not _near(self._idle_pub_seen):
                self._idle_pub_fills.clear()
            hist = self._idle_pub_fills
            rose = bool(hist) and (fill - min(hist)) >= self._idle_pub_rise_pp
            hist.append(float(fill))
            if cx is not None:
                self._idle_pub_seen = (cx, cy, now)

            ok = bool(self._shot_armed or self._shot_armed_hw)          # (a)
            if not ok and rose:                                         # (b)
                ok = True
            if not ok and self._idle_pub_box is not None:               # (c)
                ok = ((now - self._idle_pub_box[2]) <= self._idle_pub_cont_s
                      and _near(self._idle_pub_box))
            if ok:
                if cx is not None:
                    self._idle_pub_box = (cx, cy, now)
                return True
            self._det_diag["idle_unpublished"] = int(
                self._det_diag.get("idle_unpublished", 0)) + 1
            self._pw_idle_gate += 1
            return False
        except Exception:
            return False         # a broken gate cannot create actionable meter evidence

    @staticmethod
    def _no_meter_sample(reason: str, stage: str) -> dict:
        """Canonical fail-closed read result used before the DetectResult adapter boundary."""
        return {"detected": False, "meter_present": False, "fill": 0.0,
                "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": stage,
                "confidence": 0.0, "velocity_pct_s": 0.0,
                "rejection_reason": reason, "rise_state": ""}

    def _close_gameplay_gate(self) -> dict:
        """Drop all actionable vision state while no trusted physical shot window exists.

        This runs once per closed interval, not once per frame.  Besides preventing menu/loading
        scans, that saves the full CV cost while the user is not shooting.  A pending calibration
        or self-grade shot is closed on the same trusted hardware edge before tracking is reset.
        """
        if not self._gameplay_gate_closed:
            if self._prev_hw_armed:
                if self._calibrator is not None:
                    try:
                        self._end_hw_shot(self._calibrator)
                    except Exception:
                        pass
                if self._gz_window or self._gz_grade:
                    try:
                        self._gz_end_shot("gameplay_gate_closed")
                    except Exception:
                        pass
            self._reset_state()
            self._prev_hw_armed = False
            self._gameplay_gate_closed = True
        self._clear_gameplay_structure_proof()
        # CompressedMeterReader's decoder Y plane is a one-shot companion to this frame.  The
        # gate skips read(), so consume it explicitly instead of retaining blocked-scene pixels.
        if hasattr(self, "_native_y"):
            self._native_y = None
        self.last_debug = {"stage": "gameplay_ineligible", "armed": False,
                           "armed_hw": False, "conf": 0.0}
        return self._no_meter_sample("gameplay_ineligible", "gameplay_ineligible")

    def _qualify_gameplay_sample(self, sample: dict, proof_epoch=None) -> dict:
        """Require independent meter evidence before a shot-window candidate is published.

        The trusted physical window proves shot intent, not pixels: SQUARE can also be pressed in a
        menu.  Therefore a first lock additionally needs either the connected green meter structure
        (the court-wide path is always strict-structure) or the existing N-step monotonic fresh-rise
        proof.  A pure static red NBA/WNBA logo is held behind this boundary and periodically dropped
        so it cannot occupy the tracker when the real meter appears.
        """
        if proof_epoch is None:
            proof_epoch = self._physical_shot_epoch
        try:
            proof_epoch = int(proof_epoch)
        except (TypeError, ValueError, OverflowError):
            proof_epoch = 0
        # This frame may have started before a newer native arm arrived on the command
        # thread. Only a nonzero epoch that remained current through qualification may
        # latch structural provenance; ordinary meter publication remains independent.
        proof_epoch_current = bool(
            proof_epoch > 0 and self._shot_armed_hw
            and self._physical_shot_epoch == proof_epoch)
        stage = str(sample.get("stage", "") or "")
        # STRUCTURE NECROPSY bookkeeping (see __init__; pure counters, no behaviour).
        if proof_epoch > 0 and self._ep_census.get("epoch") == proof_epoch:
            c = self._ep_census
            c["last_stage"] = stage
            if bool(sample.get("detected")):
                c["qual_det"] += 1
                if sample.get("green") is not None:
                    c["green"] += 1
                if self._gameplay_lock_authorized:
                    c["authorized"] += 1
            if int(self._fresh_up_n) > int(c.get("max_fresh_up", 0)):
                c["max_fresh_up"] = int(self._fresh_up_n)
        # A discontinuous coast-steal is a new visual identity.  It may remain visible as held
        # continuity internally, but it cannot inherit the old meter's timing authority.  A
        # fill-continuous same-meter reseat (`held_reseat`, no hw_reseat marker) stays authorized.
        # When the old identity was already authorized and hardware still vouches for this shot,
        # retain only a short DISPLAY lease while the new identity re-proves itself.  Every leased
        # frame is tagged `held_reseat`, which both engine feed tiers reject as stale.
        _hw_reseat = bool(stage == "steal_reseat" and self.last_debug.get("hw_reseat"))
        # [ORION_READER_POST_RELEASE_YIELD] P1(b): a steal from OUR OWN post-release-frozen
        # lock (marker set only by the flag-gated read() path) keeps its authorization and
        # structure proof -- the identity that lost the lock was this hw window's already
        # released, already proven meter, not an unknown visual. Frames therefore feed the
        # engine immediately instead of spending 2+ frames as held_reseat display. The
        # engine-side strict ownership proof is untouched and remains the release guard.
        _pr_fresh_steal = bool(stage == "steal_reseat"
                               and self.last_debug.get("pr_frozen_steal"))
        _had_authority = bool(self._gameplay_lock_authorized)
        if stage == "steal_reseat" and not _pr_fresh_steal and (
                not bool(sample.get("detected")) or _hw_reseat):
            self._gameplay_lock_authorized = False
            # Every steal_reseat is a discontinuous new visual identity, whether the optional
            # display-continuity path exposes a held frame or suppresses it. It must earn
            # connected structure again; same-meter held_reseat continuity is unaffected.
            self._clear_gameplay_structure_proof()
            self._gameplay_verify_frames = 0
            self._gameplay_reverify_visible = bool(
                _hw_reseat and _had_authority and sample.get("detected")
                and any(int(v) > 0 for v in sample.get("bbox", (0, 0, 0, 0))[2:]))

        if not bool(sample.get("detected")):
            if self.box is None:
                self._gameplay_lock_authorized = False
                self._gameplay_verify_frames = 0
                self._gameplay_reverify_visible = False
            return sample
        monotonic_rise = bool(
            str(sample.get("rise_state", "") or "") == "rising"
            # [ORION_READER_FRESH_RISE_STEPS_ARMED] armed-aware N (default = base 3).
            and self._fresh_up_n >= self._fresh_up_req()
            and float(sample.get("velocity_pct_s", 0.0) or 0.0) > 25.0
        )
        # A GREEN segment is structure on its own -- nothing in the scene fakes the chevron.
        # A COURTWIDE seat is not. It used to auto-satisfy this test, and on 2026-08-03 that let a
        # window mullion at the left screen edge (a tall thin high-contrast vertical bar, which is
        # exactly what an unfilled meter looks like) take ownership of 4 of 34 shots, 138-148ms
        # after the Square press -- inside the ~300-400ms gap before the real meter HUD renders.
        # Those 4 epochs produced 4 aborts (all 3 live_trajectory_timeouts plus the lone
        # detector_authority_lost), i.e. 8% of the session's shots, though none ever fired.
        #
        # So a courtwide seat must now be corroborated by the rise proof that already exists below.
        # Decor cannot pass it: the mullion's apparent fill thrashes non-monotonically
        # (23.8 -> 37.0 -> 28.9 -> 38.8 across four frames) because it is not filling, it is being
        # re-measured. A real meter passes in 3-4 frames, so the only cost is ~50-67ms of ownership
        # delay on the rare courtwide path. Nominal-band acquires are untouched.
        structure = bool(sample.get("green") is not None
                         or (self._courtwide_lock and monotonic_rise))
        # [ORION_GOTO_NO_GREEN] A NOMINAL-BAND monotonic rise is structure proof too.
        #
        # THE BUG THIS FIXES, measured 2026-08-05 on epochs 40-49 of a live Go-To run. Ten
        # consecutive Go-To presses; two produced a release (both graded EXCELLENT) and eight
        # produced NOTHING -- no ownership, no abort, no fault, no telemetry at all. The
        # discriminator was single-valued and perfect: every success had >=11 frames that were
        # simultaneously below 40% fill AND carried a green make-window; every failure had ZERO.
        #
        # The chain: no green cap -> structure False -> the latch below never fires ->
        # gameplay_structure_verified is emitted False -> AutomationEngine.cpp:3663-3667 discards
        # EVERY ownership evidence frame -> no episode ever opens -> pendingStickFault never fires
        # either, which is why the failure was completely silent. Meanwhile the orchestrator sets
        # _last_meter_bbox on any detected frame, so the BOX STILL RENDERS. That is exactly the
        # reported symptom: "meter detection shows but the bot doesnt even attempt to time it".
        #
        # 2K shrinks the green make-window with shot difficulty and range, so a green cap is not
        # available at every distance -- but the meter is still real and still rising. Requiring
        # the chevron made ownership a function of shot difficulty, which was never the intent.
        #
        # WHY THIS IS NARROWER THAN WHAT ALREADY SHIPS, not wider: the courtwide tier -- the
        # strictly RISKIER one, which reaches outside the court band -- already accepts
        # `courtwide AND monotonic_rise` as structure on the line above. This grants the same
        # proof to the nominal band, which is the conservative tier. The 2026-08-03 window-mullion
        # false lock that motivated the courtwide corroboration was itself courtwide, and could
        # not pass monotonic_rise regardless: its apparent fill THRASHED (23.8 -> 37.0 -> 28.9 ->
        # 38.8) because it was being re-measured, not filling. monotonic_rise additionally demands
        # rise_state == "rising", >= _fresh_up_min consecutive fresh upward frames, and velocity
        # > 25 %/s.
        #
        # FAIL-CLOSED IS UNCHANGED. The engine still independently requires 3 unique,
        # identity-advancing, geometry-continuous frames (AutomationEngine.cpp:3703-3752, 3881),
        # first sight <= 40% fill (:3811), a >= 3.0 pp rise off the episode anchor (:3861), and a
        # live current-epoch stick-up (:3646-3654). The ONLY requirement removed is that the green
        # chevron be on screen. Nothing here manufactures a release.
        nominal_rise_proof = bool(monotonic_rise and not self._courtwide_lock)
        if self._gameplay_lock_authorized:
            if (structure or nominal_rise_proof) and proof_epoch_current:
                # A bridge/relock may already have ordinary publication authority. Still require
                # structure observed inside this new hardware epoch before cold calibration trusts it.
                self._latch_gameplay_structure_proof(proof_epoch)
            return sample

        if structure or monotonic_rise:
            self._gameplay_lock_authorized = True
            if (structure or nominal_rise_proof) and proof_epoch_current:
                # Latched from a green cap, a corroborated COURTWIDE rise, or a NOMINAL-BAND rise.
                # Native cold-start consumes the bit as independent corroboration before owning
                # Square or emitting a calibration marker.
                self._latch_gameplay_structure_proof(proof_epoch)
            self._gameplay_verify_frames = 0
            self._gameplay_reverify_visible = False
            # [ORION_GOTO_NO_GREEN] Distinguish the new nominal-band rise proof in telemetry, so a
            # live batch can show which shots it rescued rather than only that they worked.
            self.last_debug["gameplay_evidence"] = (
                "structure" if structure
                else ("nominal_rise_proof" if nominal_rise_proof else "monotonic_rise"))
            return sample

        self._gameplay_verify_frames += 1
        verify_n = self._gameplay_verify_frames
        # Four 60-fps frames are enough for the existing three-step rise proof.  A candidate that
        # remains static longer is decor; reset it so the real meter gets a cold/full search.
        if verify_n >= max(4, self._fresh_up_min + 1):
            self._reset_state()
            # The verification candidate failed, not the hardware shot.  Keep the physical
            # geometry epoch open (with no inherited dimensions) for the real meter that may
            # appear a frame later; Square commonly precedes the HUD animation.
            self._visual_drop_shot_shape(self._shot_armed_hw, preserve=False)
        elif self._gameplay_reverify_visible:
            # Geometry comes from the newly-found candidate and has already passed the reader's
            # per-shot translation-only stabilizer.  Keep it painted without granting freshness:
            # zero motion/rise, remove any target, and use a reason excluded by both timing tiers.
            visible = dict(sample)
            visible.update({"detected": True, "meter_present": True,
                            "velocity_pct_s": 0.0, "rise_state": "", "green": None,
                            "rejection_reason": "held_reseat"})
            self.last_debug = {"stage": "shot_candidate_reverify", "armed": True,
                               "armed_hw": True, "verify_frames": verify_n,
                               "display_continuity": 1,
                               "conf": float(sample.get("confidence", 0.0) or 0.0)}
            return visible
        self.last_debug = {"stage": "shot_candidate_unverified", "armed": True,
                           "armed_hw": True, "verify_frames": verify_n,
                           "conf": 0.0}
        return self._no_meter_sample("shot_candidate_unverified",
                                     "shot_candidate_unverified")

    # ---- calibrate_meter plumbing (B3; called by the orchestrator's calibrate_meter) ----
    def ensure_calibrator(self) -> ColorCalibrator:
        if self._calibrator is None:
            style = str(getattr(self._cfg, "meter_style", "Arrow2") or "Arrow2") if self._cfg else "Arrow2"
            color = str(getattr(self._cfg, "meter_color", "Red") or "Red") if self._cfg else "Red"
            self._calibrator = ColorCalibrator(style=style, color=color)
        return self._calibrator

    def calibration_status(self) -> Optional[dict]:
        return self._calibrator.status() if self._calibrator is not None else None

    def start_color_calibration(self, target: int = 5, window: int = 10) -> None:
        self.ensure_calibrator().start_user_calibration(target, window)

    def finish_color_calibration(self) -> None:
        if self._calibrator is not None:
            self._calibrator.finish_user_calibration()
            self._apply_baked_bands()

    def cancel_color_calibration(self) -> None:
        if self._calibrator is not None:
            self._calibrator.cancel_user_calibration()
            self._apply_baked_bands()

    def _apply_baked_bands(self) -> None:
        """Refresh the learned-band cache from the calibrator (called on bake/restore/state
        change). Learned bands are used ACQUISITION-first with a same-frame default fallback."""
        c = self._calibrator
        self._learned_red = None
        self._learned_green = None
        if c is not None and c.baked is not None and c.state in ("locked", "provisional"):
            # TAGGED row. This value is passed as `bounds` and then LATCHED into `_lock_red` for
            # the life of the lock, so an untagged pair here would resolve as BGR and revert an
            # HSV colour's whole lock to a red mask.
            self._learned_red = c.baked_bar_row()
            self._learned_green = (tuple(c.baked["green_lo"]), tuple(c.baked["green_hi"]))

    def _armed(self) -> bool:
        if self._shot_gate is not None:
            try:
                res = self._shot_gate()
                if isinstance(res, tuple):
                    a, pc = res
                    self._shot_armed = bool(a); self._shot_pose_conf = float(pc)
                else:
                    self._shot_armed = bool(res)
            except Exception:
                pass
        return self._shot_armed

    # ------------------------------------------------------------------ #
    #  masks (EXACT 2k_Vision BGR red; HSV green). `bounds` selects an explicit
    #  band; None rides the PER-LOCK active band (learned when it acquired the
    #  lock, else the shipped defaults) -- so a whole lock lifetime always reads
    #  with ONE consistent band (B1 fallback plumbing).
    # ------------------------------------------------------------------ #
    # -- TAGGED BAND UNION (2026-08-04, multi-colour) ------------------------------------------
    #  A band is one of
    #      ("bgr", lo_bgr, hi_bgr)                     -> literal cv2.inRange on the BGR frame
    #      ("hsv", hue_lo, hue_hi, s_floor, v_floor)   -> hue window ANDed with S/V floors
    #  A bare legacy ``(lo, hi)`` pair is treated as BGR, so every pre-existing call site that
    #  passes raw constants keeps its exact behaviour.
    #
    #  WHY A UNION AND NOT "EVERYTHING IN HSV": Red must stay byte-identical, and the only way to
    #  guarantee that by INSPECTION (rather than by replay, which is no longer available) is for a
    #  Red-configured reader to reach the identical cv2.inRange call with the identical constants.
    #  It does: `self._bands["nominal"]` for Red is literally ("bgr", self._RED_LO, self._RED_HI),
    #  so `_redmask(sub)` resolves to cv2.inRange(sub, _RED_LO, _RED_HI) -- the shipped expression,
    #  unchanged. Red never enters the HSV arm at all.
    #
    #  WHY PURPLE CANNOT BE A BGR BOX: red sits on a CORNER of the colour cube (high R, low G, low
    #  B), so an axis-aligned box isolates it. Purple is high R AND high B with low G -- not a
    #  corner -- so any box wide enough to contain it also contains neutral grey. That is exactly
    #  the `_MICRO_RED` pathology (its corner (110,110,110) is grey) that needed the dominance
    #  floor bolted on. An HSV row has no such hole: the saturation floor makes the grey axis
    #  unrepresentable by construction.
    @staticmethod
    def _as_band(bounds):
        """Normalise `bounds` to a tagged row. Legacy 2-tuples are BGR."""
        if bounds and isinstance(bounds[0], str):
            return bounds
        return ("bgr", bounds[0], bounds[1])

    def _band_for(self, tier):
        """The configured colour's tagged band at `tier` (nominal/courtwide/micro).

        For Red this returns the LIVE instance constants (which ORION_READER_COLOR_TOL may have
        widened), not the table copy, so the tolerance path keeps working untouched.
        """
        return self._bands[tier]

    def _resolve_red_band(self, bounds):
        """Resolve an explicit `bounds`, else the per-lock latched band, else the nominal band."""
        if bounds is not None:
            return self._as_band(bounds)
        if self._lock_red is not None:
            return self._as_band(self._lock_red)
        return self._bands[_mbc.BAND_NOMINAL]

    def _hsv_band_mask(self, bgr, row, hsv=None):
        """Mask for an ("hsv", hue_lo, hue_hi, s_floor, v_floor) row.

        The S and V floors are CONJUNCTS of the hue test, never a fallback branch: they are the
        low bounds of the same cv2.inRange, so a desaturated or dark pixel is rejected before its
        hue is ever consulted. That matters because hue is numerically meaningless near the grey
        axis -- a branch that let a washed-out pixel "still try the hue test" would readmit
        precisely the arena greys the floors exist to exclude.
        """
        _, h_lo, h_hi, s_min, v_min = row
        # `hsv` is an optional PRE-CONVERTED frame supplied by callers that need several bar
        # masks off one image. Ignore it unless it is the same size as `bgr`: a caller that
        # passes a full-frame HSV alongside a cropped BGR must get a correct mask, not a crash
        # or (worse) a silently misaligned one.
        if hsv is not None and hsv.shape[:2] != bgr.shape[:2]:
            hsv = None
        src = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV) if hsv is None else hsv
        if h_lo < 0:
            # Wrap through 0 (e.g. a red HSV row). BOTH halves carry the S/V floors, so the
            # conjunction property is preserved on either side of the seam.
            lo_a = np.array((0, s_min, v_min), np.uint8)
            hi_a = np.array((int(h_hi), 255, 255), np.uint8)
            lo_b = np.array((int(180 + h_lo), s_min, v_min), np.uint8)
            hi_b = np.array((179, 255, 255), np.uint8)
            return cv2.bitwise_or(cv2.inRange(src, lo_a, hi_a),
                                  cv2.inRange(src, lo_b, hi_b))
        return cv2.inRange(src,
                           np.array((int(h_lo), s_min, v_min), np.uint8),
                           np.array((int(h_hi), 255, 255), np.uint8))

    def _det_track_template_candidates(self, width, height):
        """Old appearance, plus one detector-supported camera-scale transform.

        A fixed-size notch cannot correlate with a physically zoomed notch. The
        old weak/rescale branch then learned current pixels at the inference-old
        location, baking a moving camera's lag into the new template. Test the
        measured dimension ratio BEFORE that fallback. Keep the original too:
        detector box breathing is not proof that the actual pixels rescaled.

        This is not a scale sweep or presence detector. One measured dimension
        ratio gets a second candidate, bounded by the existing 35% scale-step
        limit. Do not gate this on fill-denominator relatching: even an 8% zoom
        can destroy an otherwise exact notch correlation, well below the ruler's
        relatch threshold. Integer-identical crops cost no extra match. Score,
        texture, local-search,
        innovation, deviation, source cadence and lifetime checks stay intact.
        """
        tmpl = self._det_tmpl
        candidates = [(tmpl, float(self._NOTCH_ABOVE),
                       float(self._det_tmpl_cx_offset),
                       float(self._det_tmpl_bottom_offset))]
        size = self._det_tmpl_size
        if not self._det_track_cont or tmpl is None or size is None:
            return candidates
        sx = float(width) / max(1., float(size[0]))
        sy = float(height) / max(1., float(size[1]))
        delta = max(abs(sx - 1.), abs(sy - 1.))
        # Inclusive geometric limits need roundoff tolerance: 1.35 - 1.0
        # exceeds 0.35 in binary floating point. This is far below a pixel.
        if not (0.0 < delta <= min(.35, float(self._scale_step_max)) + 1e-12):
            return candidates
        th, tw = tmpl.shape[:2]
        nw, nh = int(round(tw * sx)), int(round(th * sy))
        if nw < 8 or nh < 4 or (nw == tw and nh == th):
            return candidates
        scaled = cv2.resize(tmpl, (nw, nh), interpolation=cv2.INTER_LINEAR)
        if float(scaled.std()) < float(self._det_tmpl_std_min):
            return candidates
        # Resize uses integer output dimensions; carry those actual ratios into
        # the template's centre/bottom anchor rather than rounding the motion.
        sx, sy = float(nw) / tw, float(nh) / th
        candidates.append((scaled, float(self._NOTCH_ABOVE) * sy,
                           float(self._det_tmpl_cx_offset) * sx,
                           float(self._det_tmpl_bottom_offset) * sy))
        return candidates

    def _det_track_predicted_step(self, vx, vy):
        """Elapsed-source-time search displacement from the retained pixel origin.

        Previously a preserved origin always predicted just one px/frame step,
        even after several hidden frames or a decoder callback gap. Keep that
        origin immutable while advancing its SEARCH centre across at most the
        existing short base-bridge horizon. Innovation/deviation and detector
        lifetime checks still decide whether current pixels can be served.
        """
        now, origin = self._det_track_sample_ts, self._det_track_box_ts
        k = self._det_kalman_state if self._det_kalman_search else None
        if k is not None and now is not None and origin is not None:
            age = float(now) - k['ts']
            dt = float(now) - float(origin)
            horizon = min(float(self._subpx_bridge_max_s), float(self._det_tmpl_max_s))
            if k['n'] >= 2 and 0.0 <= age <= horizon and 0.0 < dt <= horizon:
                # Retain the last accepted pixel origin. Kalman velocity only
                # steers the bounded NCC search/innovation window across a gap.
                return float(k['x'][2]) * dt, float(k['x'][3]) * dt
        if now is None or origin is None:
            return float(vx), float(vy)
        dt = float(now) - float(origin)
        if not np.isfinite(dt) or dt <= 0.0:
            return 0.0, 0.0
        horizon = min(float(self._subpx_bridge_max_s), float(self._det_tmpl_max_s))
        ratio = min(dt, max(0.0, horizon)) / max(
            1.0e-3, float(self._det_track_velocity_dt_s))
        return float(vx) * ratio, float(vy) * ratio

    def _remember_det_scale_reference(self, ref, scale, bh, span, ts):
        """Keep scale evidence independent of detector-cadence appearance refresh.

        Store only a current, already-localized mid-rise notch with a directly
        measured cap/base span at the ACCEPTED ruler scale. Ordinary template
        refresh must not move this reference: repeated one-pixel zoom steps
        otherwise get absorbed before the 17-row notch ever needs resizing.
        """
        previous = self._det_scale_reference
        if previous is not None and previous['ruler'] == ref[0] and previous['scale'] == scale:
            return
        evidence = self._det_track_scale_match
        if (ts is None or not np.isfinite(float(ts))
                or evidence is None or len(evidence) != 4
                or abs(float(evidence[3]) - float(ts)) > 1e-6
                or abs(float(self._det_tmpl_ts) - float(ts)) > 1e-6
                or not self._det_new_accept or self._det_tmpl is None
                or self._det_tmpl_size is None
                or self._det_track_score < self._det_track_min
                or self._det_tmpl_std < self._det_tmpl_std_min
                or self._det_occ_current_full_negative or self._det_occ_recovered
                or not (15.0 <= float(self._last_fill_coarse) <= 80.0)):
            return
        # Below ~12% fill the moving edge crosses the notch itself; do not
        # freeze that transient as independent camera-scale appearance.
        target_h = float(ref[2]) * float(scale)
        if (abs(float(bh) - target_h) > 2.0
                or abs(float(self._det_tmpl_size[1]) - target_h) > 2.0
                or abs(float(span) - float(ref[1]) * float(scale)) > 2.0):
            return
        self._det_scale_reference = {
            'ruler': ref[0], 'scale': float(scale),
            'template': self._det_tmpl.copy(), 'size': tuple(self._det_tmpl_size),
            'cx_offset': float(self._det_tmpl_cx_offset),
            'bottom_offset': float(self._det_tmpl_bottom_offset),
        }
        self._det_scale_pending = None

    def _det_cumulative_scale_match(self, frame, box, now):
        """Corroborate gradual zoom at an ALREADY localized notch, not a new position.

        The active patch may match perfectly after every small camera step was
        refreshed into it. Compare a persistent accepted-scale reference with
        its one detector-proposed resize at the same current anchor. Two unique
        agreeing frames must favor the resize, and the previous vote must have
        earned independent measured cap/base-span proof. The current frame's
        span still has to certify any resulting ruler correction afterward.
        """
        reference = self._det_scale_reference
        if (reference is None or self._subpx_camera_ref is None
                or reference['ruler'] != (self._subpx_D, self._subpx_off)
                or reference['scale'] != self._subpx_camera_scale
                or self._det_track_sample_ts is None
                or self._det_occ_current_full_negative or self._det_occ_recovered):
            self._det_scale_pending = None
            return False
        x, y, w, h = (int(v) for v in box)
        template = reference['template']
        th, tw = template.shape[:2]
        sx = float(w) / max(1.0, float(reference['size'][0]))
        sy = float(h) / max(1.0, float(reference['size'][1]))
        delta = max(abs(sx - 1.0), abs(sy - 1.0))
        nw, nh = int(round(tw * sx)), int(round(th * sy))
        if (not (0.0 < delta <= min(.35, float(self._scale_step_max)) + 1e-12)
                or nw < 8 or nh < 4 or (nw, nh) == (tw, th)):
            self._det_scale_pending = None
            return False
        scaled = cv2.resize(template, (nw, nh), interpolation=cv2.INTER_LINEAR)
        if float(scaled.std()) < float(self._det_tmpl_std_min):
            self._det_scale_pending = None
            return False
        H, W = frame.shape[:2]
        cx, bottom = x + w * .5, y + h

        def score_at_anchor(patch, above, off_x, off_bottom):
            ph, pw = patch.shape[:2]
            # Only two rasterization pixels around the independently localized
            # anchor; this secondary check never searches for another meter.
            x0 = max(0, int(np.floor(cx + off_x - pw * .5 - 2.0)))
            x1 = min(W, int(np.ceil(cx + off_x + pw * .5 + 2.0)))
            y0 = max(0, int(np.floor(bottom + off_bottom - above - 2.0)))
            y1 = min(H, int(np.ceil(bottom + off_bottom - above + ph + 2.0)))
            if x1 - x0 < pw or y1 - y0 < ph:
                return -1.0
            gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            scores = cv2.matchTemplate(gray, patch, cv2.TM_CCOEFF_NORMED)
            cols = x0 + np.arange(scores.shape[1]) + pw * .5 - off_x
            rows = y0 + np.arange(scores.shape[0]) + above - off_bottom
            allowed = ((np.abs(rows - bottom) <= 2.0)[:, None]
                       & (np.abs(cols - cx) <= 2.0)[None, :])
            scores[~allowed] = -1.0
            _, value, _, _ = cv2.minMaxLoc(scores)
            return float(value) if np.isfinite(value) else -1.0

        raw_score = score_at_anchor(template, float(self._NOTCH_ABOVE),
                                    reference['cx_offset'], reference['bottom_offset'])
        sx, sy = float(nw) / tw, float(nh) / th
        scaled_score = score_at_anchor(scaled, float(self._NOTCH_ABOVE) * sy,
                                       reference['cx_offset'] * sx,
                                       reference['bottom_offset'] * sy)
        if (raw_score < 0.0 or scaled_score < float(self._det_track_min)
                or scaled_score <= raw_score + .03):
            self._det_scale_pending = None
            return False
        previous = self._det_scale_pending
        count = 1
        if (previous is not None
                and 0.0 < float(now) - previous[0] <= float(self._subpx_bridge_max_s)
                and abs(w - previous[1]) <= 1 and abs(h - previous[2]) <= 2
                and previous[4]):
            count = min(2, previous[3] + 1)
        # This frame's cap/base measurement happens after position tracking.
        # It must certify this exact vote before a following frame can use it.
        self._det_scale_pending = (float(now), w, h, count, False)
        return count >= 2

    def _track_box_ncc(self, frame, box):
        """Per-frame METER tracker: NCC match on the meter's BOTTOM NOTCH, around a
        velocity-predicted centre.

        WHY A NOTCH, NOT THE WHOLE BOX (2026-08-29): the YOLO detector costs ~30ms, so the box
        only refreshes every ~45ms and drifts off a meter that moves with the shooter. The first
        version of this tracker templated the WHOLE box -- but the box's appearance CHANGES as
        the meter fills, so the match decayed within a few frames, fell under the accept floor,
        and the lock snapped back to the stale detector box: accurate on average, visibly laggy
        and unstable frame to frame (owner-reported).

        The retired 5700-LOC detector (meter_detector.py `_track_locked`, "LOCK-THEN-TRACK",
        measured 0.12px MAE @ 0.095ms) had already solved this, and this is a port of its idea:
        template the meter's BOTTOM EDGE + lower fill -- the one region whose appearance is
        STABLE while the bar fills -- and search a tight window around `cx + vx`, the
        velocity-predicted centre, so a camera pan (~9-13px/frame) never outruns the search.
        Velocity retains the old 0.6/0.4 blend on ordinary intervals, with
        source-time normalization across changing callback cadence.

        POSITION ONLY, exactly as in the original: a weak match returns the box unchanged and
        never decides that the meter is gone -- survival stays with the detector's veto, because
        a grey patch over-matches static court furniture."""
        try:
            self._det_track_score = 0.0
            self._det_track_scale_match = None
            tmpl = self._det_tmpl
            if tmpl is None:
                return box
            x, y, w, h = (int(v) for v in box)
            if w <= 0 or h <= 0:
                return box
            th, tw = tmpl.shape[:2]
            if th < 4 or tw < 8:
                return box
            H, W = frame.shape[:2]
            # Predict where the notch is NOW from the last centre/bottom + smoothed
            # per-axis velocity.  The old path predicted x only and used the x padding
            # on y as well, so a camera dive could outrun the vertical search even while
            # horizontal tracking remained healthy.
            cx = x + w * 0.5
            step_x, step_y = (self._det_track_predicted_step(
                self._det_track_vx, self._det_track_vy) if self._det_track_cont
                else (float(self._det_track_vx), float(self._det_track_vy)))
            pcx = cx + step_x
            b = y + h             # meter bottom edge (the notch anchor)
            pb = b + step_y
            win_x = int(self._det_track_pad_x)
            win_y = int(self._det_track_pad_y)
            if self._det_track_cont:
                # Motion-aware, per-axis widening. Bounded at 60px so an uncertain
                # velocity can never turn the local tracker into a court-wide matcher.
                win_x = int(max(win_x, min(
                    60.0, abs(step_x) * 2.5 + 10.0)))
                win_y = int(max(win_y, min(
                    60.0, abs(step_y) * 2.5 + 10.0)))
            best = None
            for candidate, above, cx_offset, bottom_offset in (
                    self._det_track_template_candidates(w, h)):
                th, tw = candidate.shape[:2]
                x0 = int(max(0, pcx - tw * 0.5 - win_x))
                x1 = int(min(W, pcx + tw * 0.5 + win_x))
                y0 = int(max(0, pb - above - win_y))
                y1 = int(min(H, pb + (th - above) + win_y))
                if (x1 - x0) < tw + 2 or (y1 - y0) < th + 2:
                    continue
                gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
                if float(gray.std()) < 1e-3 or float(candidate.std()) < 1e-3:
                    continue
                res = cv2.matchTemplate(gray, candidate, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if self._det_track_cont and float(mx) >= float(self._det_track_min):
                    candidate_box = (
                        max(0, min(W - w, int(round(
                            x0 + loc[0] + tw * .5 - cx_offset - w * .5)))),
                        max(0, min(H - h, int(round(
                            y0 + loc[1] + above - bottom_offset - h)))), w, h)
                    if not self._det_track_innovation_allowed(
                            self._det_track_vx, self._det_track_vy, box, candidate_box):
                        # An exact off-path court/jersey duplicate can outscore
                        # the real, slightly blurred/occluded notch. Rejecting
                        # that global argmax AFTER matching discarded genuine
                        # in-gate pixels and snapped back to the delayed box.
                        # Search the SAME correlation surface inside the SAME
                        # innovation envelope; no broader motion/presence budget
                        # and no second template inference are introduced.
                        pred_x, pred_y, allow_x, allow_y = self._det_track_innovation_limits(
                            self._det_track_vx, self._det_track_vy, box)
                        # Mirror the rounded/clipped output geometry exactly,
                        # including odd-width half-pixel centres and frame edges.
                        cols = np.clip(np.rint(
                            x0 + np.arange(res.shape[1]) + tw * .5
                            - cx_offset - w * .5), 0, W - w) + w * .5
                        rows = np.clip(np.rint(
                            y0 + np.arange(res.shape[0]) + above
                            - bottom_offset - h), 0, H - h) + h
                        allowed = ((np.abs(rows - pred_y) <= allow_y)[:, None]
                                   & (np.abs(cols - pred_x) <= allow_x)[None, :])
                        res[~allowed] = -1.0
                        _, mx, _, loc = cv2.minMaxLoc(res)
                        if float(mx) < float(self._det_track_min):
                            continue  # no admissible current-pixel match
                if best is None or float(mx) > best[0]:
                    best = (float(mx), x0 + loc[0] + tw * .5,
                            y0 + loc[1] + above, cx_offset, bottom_offset,
                            candidate is not tmpl)
            if best is None:
                return box
            mx, raw_mcx, raw_mby, cx_offset, bottom_offset, rescaled = best
            self._det_track_score = float(mx)
            if mx < self._det_track_min:
                return box                      # weak match -> keep the detector's box
            if self._det_track_cont:
                self._det_track_scale_match = (bool(rescaled), int(w), int(h))
            # Integer crop bounds can sit half a pixel left of an odd-width
            # bbox centre. Carry that seed offset instead of re-learning it
            # as a position shift at every detector-cadence template refresh.
            mcx = (raw_mcx - cx_offset
                   if self._det_track_cont else raw_mcx)
            # Differentiate accepted TEMPLATE anchors, not rounded output boxes.
            # An odd-width integer bbox has a half-pixel centre while an even-width
            # notch crop can match at an integer centre. Subtracting the bbox centre
            # injected a persistent +/-0.5px/frame "motion" on a stationary meter.
            # The exact template anchor also excludes deliberate reconciliation of
            # the served rectangle from physical motion. A weak frame clears this
            # baseline; reacquisition seeds a new pair instead of assigning a
            # multi-frame displacement to one frame. The legacy/off route is unchanged.
            last_match = self._det_track_last_match if self._det_track_cont else None
            velocity_ratio = 1.0
            velocity_gain = 0.4
            update_velocity = not self._det_track_cont or last_match is not None
            if (self._det_track_cont and last_match is not None
                    and self._det_track_sample_ts is not None
                    and self._det_track_last_match_ts is not None):
                pair_dt = (float(self._det_track_sample_ts)
                           - float(self._det_track_last_match_ts))
                update_velocity = 0.0 < pair_dt <= float(self._subpx_bridge_max_s)
                if update_velocity:
                    # Blend velocities in the SAME elapsed-time units. Otherwise
                    # a 33ms frame followed by a 16ms frame is interpreted as
                    # camera acceleration/deceleration on a constant-speed pan.
                    old_dt = max(1.0e-3, float(self._det_track_velocity_dt_s))
                    effective_dt = max(1.0e-3, pair_dt)
                    velocity_ratio = effective_dt / old_dt
                    self._det_track_velocity_dt_s = effective_dt
                    if pair_dt < max(1.0e-3, 0.25 * old_dt):
                        # A compressed timestamp interval must not magnify one
                        # raster/placement pixel into a huge next-frame stride.
                        # Re-express the old velocity in the new cadence, but
                        # require its next pair before learning displacement.
                        # Current pixels still locate and serve the box now.
                        velocity_gain = 0.0
            elif (self._det_track_cont and last_match is not None
                    and self._det_track_sample_ts is not None):
                # A caller may have supplied appearance before its first timed
                # step. Attach that first displacement to the known seed age,
                # without rescaling an as-yet uncalibrated px/sample velocity.
                seed_dt = float(self._det_track_sample_ts) - float(self._det_tmpl_ts)
                if 0.0 < seed_dt <= float(self._subpx_bridge_max_s):
                    if seed_dt < 1.0e-3:
                        velocity_gain = 0.0
                        velocity_ratio = 1.0e-3 / max(
                            1.0e-3, float(self._det_track_velocity_dt_s))
                    self._det_track_velocity_dt_s = max(1.0e-3, seed_dt)
            if update_velocity:
                prev_cx = float(last_match[0]) if last_match is not None else cx
                self._det_track_vx = ((1.0 - velocity_gain) * float(self._det_track_vx) * velocity_ratio
                                      + velocity_gain * ((raw_mcx if self._det_track_cont else mcx) - prev_cx))
            nx = int(round(mcx - w * 0.5))
            nx = max(0, min(W - w, nx))
            # Vertical: the notch match also pins the bottom edge, so carry y with it.
            mby = (raw_mby - bottom_offset
                   if self._det_track_cont else raw_mby)
            if update_velocity:
                prev_bottom = float(last_match[1]) if last_match is not None else b
                self._det_track_vy = ((1.0 - velocity_gain) * float(self._det_track_vy) * velocity_ratio
                                      + velocity_gain * ((raw_mby if self._det_track_cont else mby) - prev_bottom))
            if self._det_track_cont:
                self._det_track_last_match = (float(raw_mcx), float(raw_mby))
                self._det_track_last_match_ts = self._det_track_sample_ts
            ny = int(round(mby - h))
            ny = max(0, min(H - h, ny))
            return (nx, ny, int(w), int(h))
        except Exception:
            # A failed coordinate/motion update is not an accepted localization,
            # even if matchTemplate produced a strong score before the failure.
            self._det_track_score = 0.0
            self._det_track_scale_match = None
            return box

    def _det_hist_vel(self):
        """Robust meter velocity (centre-vx, bottom-vy, px/s) from detector boxes.

        Use a Theil-Sen slope (the median of every pairwise slope) on each component.
        The old ``min(first-to-last, consecutive-pair median)`` rule protected static
        locks from one detector hop, but it was biased on a translating fade whenever
        the detector's normal +/-8..12 px placement error alternated across refreshes.
        A measured/synthetic -780 px/s pan then toggled between about -300 and -876
        px/s; extrapolation moved the served box 66-116 px off the ribbon and the fill
        read hard-zeroed.  The pairwise median rejects one acquisition hop (at most
        n-1 of n*(n-1)/2 slopes) while averaging placement error across all baselines,
        so the same pan remains centred on its physical velocity.  The vertical component is
        the box BOTTOM edge, deliberately matching NCC, emit smoothing and fill geometry.  A
        changing detector height at a stationary bottom must therefore contribute exactly zero
        vertical motion instead of the false ``-dh/2`` produced by centre-Y history.

        Velocity still earns TRUST only after >=4 detections spanning
        ``VEL_MIN_SPAN_S``.  A shorter estimate is magnitude-clamped to EARLY_V_MAX;
        consumers which open motion budgets continue to require the trusted bit.

        Returns (vx, vy, speed, trusted)."""
        try:
            if len(self._det_box_hist) < 3:
                return 0.0, 0.0, 0.0, False
            h = tuple(tuple(row) for row in self._det_box_hist)
            # Extrapolation, reconciliation and the deviation bound ask for the
            # same robust velocity within one frame (and between async results).
            # Cache only by the entire immutable history plus both policy knobs:
            # repeated source slots save work, while any mutation/retune misses.
            key = (h, float(self._det_vel_min_span_s), float(self._det_early_v_max))
            cached = getattr(self, '_det_hist_velocity_cache', None)
            if cached is not None and cached[0] == key:
                return cached[1]
            span = float(h[-1][0]) - float(h[0][0])
            if span < 0.05:
                return 0.0, 0.0, 0.0, False
            trusted = (len(h) >= 4 and span >= float(self._det_vel_min_span_s))
            pvx, pvy = [], []
            for i, a in enumerate(h[:-1]):
                for b in h[i + 1:]:
                    dt = float(b[0]) - float(a[0])
                    if dt >= 0.01:
                        pvx.append((b[1] - a[1]) / dt)
                        pvy.append((b[2] - a[2]) / dt)
            if not pvx:
                return 0.0, 0.0, 0.0, False
            vx = float(np.median(pvx))
            vy = float(np.median(pvy))
            spd = float(np.hypot(vx, vy))
            if not trusted and spd > float(self._det_early_v_max):
                sc = float(self._det_early_v_max) / max(1e-6, spd)
                vx *= sc; vy *= sc; spd = float(self._det_early_v_max)
            result = (float(vx), float(vy), float(spd), trusted)
            self._det_hist_velocity_cache = (key, result)
            return result
        except Exception:
            return 0.0, 0.0, 0.0, False

    def _det_track_innovation_limits(self, prior_vx, prior_vy, ref):
        """One shared envelope for peak selection and the final output guard."""
        rcx = float(ref[0]) + float(ref[2]) * 0.5
        rbot = float(ref[1]) + float(ref[3])
        gain = float(self._det_track_innov_vel_gain)
        cap = float(self._det_track_innov_max)
        base = float(self._det_track_innov_base)
        step_x, step_y = self._det_track_predicted_step(prior_vx, prior_vy)
        return (rcx + step_x, rbot + step_y,
                min(cap, base + gain * abs(step_x)),
                min(cap, base + gain * abs(step_y)))

    def _det_track_innovation_allowed(self, prior_vx, prior_vy, ref, candidate,
                                      limits=None):
        """Return True when an NCC match is a plausible one-frame continuation.

        This is intentionally position-only and cannot create detector authority.  It closes
        the one-frame interval between a bad strong NCC match and the next frame's detector-snap
        check.  Each axis gets a bounded allowance around its own velocity prediction so a fast
        horizontal fade does not widen the vertical court-search budget (or vice versa).
        """
        try:
            ccx = float(candidate[0]) + float(candidate[2]) * 0.5
            cbot = float(candidate[1]) + float(candidate[3])
            pred_x, pred_y, allow_x, allow_y = (
                limits if limits is not None else self._det_track_innovation_limits(
                    prior_vx, prior_vy, ref))
            innov_x = ccx - pred_x
            innov_y = cbot - pred_y
            return abs(innov_x) <= allow_x and abs(innov_y) <= allow_y
        except Exception:
            # A malformed candidate is never evidence that earns a broad tracker move.
            return False

    def _det_hist_speed(self):
        """Meter speed (px/s) for BUDGET-opening consumers: 0.0 until trusted -- an
        untrusted estimate must never widen the tracker's deviation budget (that is how
        onset jitter let the match wander to zero-fill distances)."""
        v = self._det_hist_vel()
        return v[2] if v[3] else 0.0

    def _det_track_step(self, frame, held, now):
        """Tracked-box CONTINUITY wrapper around the NCC primitive (2026-08-30; see the
        ORION_METER_TRACK_CONTINUITY knob block in __init__ for the measured failure it
        fixes -- the Left_Fade zero-fill dropout).

        Contract per frame, `held` = the detector's held/extrapolated box:
          1. Reference = LAST FRAME'S TRACKED BOX (a 1-frame-old, ~+-4px reference) instead
             of the stale detector box -- unless the two disagree by more than the snap
             gate, in which case the detector wins outright (teleport / drift bound) and
             the position-anchored template dies with the old position.
          2. NCC match with the OLD template (never seeded this frame at this position, so
             a self-match can no longer pin the box to a stale spot).
          3. On an accepted match: pull the result a small fraction toward the detector's
             box (seed bias / template random-walk drift decays geometrically), persist it
             as next frame's reference, and at DETECTOR CADENCE refresh the template from
             the TRACKED position after matching so the next frame never self-matches a
             delayed/jittery detector proposal.
          4. On a weak match: fall back to the detector's box exactly as the shipped path
             did; a template that stays weak past _det_tmpl_max_s re-bootstraps there.
        A wrong box that reports a confident fill is worse than a dropout: nothing here
        extends lock LIFETIME (presence/hold/coast/veto decide that upstream), it only
        moves WHERE an already-held box sits between detector refreshes."""
        if not self._det_track_cont:
            self._det_track_scale_match = None
            return self._track_box_ncc(frame, held)
        try:
            self._det_track_scale_match = None
            source_now = float(now) if now is not None else None
            self._det_track_sample_ts = (
                source_now if source_now is not None and np.isfinite(source_now) else None)
            # Missing source timing is a documented capture fallback, not a
            # stream of frames captured at timestamp zero. Preserve nominal
            # px/call motion there; use wall time ONLY for template lifetime.
            now = (self._det_track_sample_ts if self._det_track_sample_ts is not None
                   else _time.monotonic())
            timed = self._det_track_sample_ts is not None
            if (self._det_track_clock_timed is not None
                    and self._det_track_clock_timed != timed):
                # A helper caller switching between source and fallback clocks
                # cannot compare template ages or differentiate across domains.
                # Discard position/appearance only; detector lock authority and
                # the fill ruler retain their own independent lifecycle rules.
                self._det_tmpl = None
                self._det_tmpl_size = None
                self._det_tmpl_pos = None
                self._det_tmpl_cx_offset = 0.0
                self._det_tmpl_bottom_offset = 0.0
                self._det_track_box = None
                self._det_track_box_ts = None
                self._det_track_last_match = None
                self._det_track_last_match_ts = None
                self._det_track_vx = self._det_track_vy = 0.0
                self._det_track_velocity_dt_s = 1.0 / 60.0
                self._det_scale_reference = None
                self._det_scale_pending = None
            if self._det_tmpl is None:
                self._det_kalman_state = None
            self._det_track_clock_timed = timed
            held = tuple(int(v) for v in held)
            ref = held
            tb = self._det_track_box
            # A genuine detector rescale is different from a momentary hidden
            # notch. Use the existing scale-change criterion so preserving an
            # old appearance cannot freeze the pre-rebase seed at shot onset.
            geometry_rebased = bool(tb is not None and (
                abs(float(held[2]) / max(1.0, float(tb[2])) - 1.0)
                    > float(self._fill_denom_relatch)
                or abs(float(held[3]) / max(1.0, float(tb[3])) - 1.0)
                    > float(self._fill_denom_relatch)))
            snap = float(self._det_track_snap_px) * max(1.0, float(self.W or 720.0) / 720.0)
            if tb is not None:
                dx = (tb[0] + tb[2] * 0.5) - (held[0] + held[2] * 0.5)
                dy = (tb[1] + tb[3]) - (held[1] + held[3])
                step_x, step_y = self._det_track_predicted_step(
                    self._det_track_vx, self._det_track_vy)
                if ((abs(dx) <= snap and abs(dy) <= snap)
                        or (abs(dx + step_x) <= snap and abs(dy + step_y) <= snap)):
                    # continuity reference: tracked position, detector's size (the size
                    # feeds the fill denominator and the detector owns it).
                    # Keep the position anchors when detector dimensions breathe.
                    # Copying top-left with NEW width/height moves the implied centre
                    # and bottom before NCC runs, so a stationary notch manufactures
                    # -dw/2 and -dh velocity and can leave the local search window.
                    # The detector still owns size; only the continuity reference is
                    # rebuilt around the last tracked centre/bottom.
                    ref = (int(round(tb[0] + tb[2] * 0.5 - held[2] * 0.5)),
                           int(tb[1] + tb[3] - held[3]),
                           int(held[2]), int(held[3]))
                else:
                    self._det_tmpl = None
                    self._det_tmpl_size = None
                    self._det_tmpl_cx_offset = 0.0
                    self._det_tmpl_bottom_offset = 0.0
                    self._det_tmpl_pos = None
                    self._det_track_box = None
                    self._det_track_vx = 0.0
                    self._det_track_vy = 0.0
                    self._det_track_last_match = None
                    self._det_track_last_match_ts = None
                    self._det_track_box_ts = None
                    self._det_track_velocity_dt_s = 1.0 / 60.0
                    self._det_scale_reference = None
                    self._det_scale_pending = None
            if self._det_tmpl is None:
                self._det_kalman_state = None
            refresh_template = bool(self._det_new_accept)
            had_template = self._det_tmpl is not None
            if self._det_tmpl is None:
                # Bootstrap only.  Once a textured template exists, match it BEFORE
                # refreshing: seeding from this frame at every new detector proposal
                # creates an exact same-frame self-match and makes the supposedly 60fps
                # tracker reproduce every delayed/jittery detector stride.  A successful
                # old-template match is refreshed from its tracked position below.
                self._seed_track_template(frame, held if refresh_template else ref)
                self._det_tmpl_ts = float(now)
                self._det_track_box_ts = self._det_track_sample_ts
            # Clear at the wrapper boundary as well as inside the primitive, so
            # instrumentation/adapter fall-throughs cannot carry a prior strong
            # score into a frame that was never successfully localized.
            self._det_track_score = 0.0
            prior_vx = float(self._det_track_vx)
            prior_vy = float(self._det_track_vy)
            prior_velocity_dt = float(self._det_track_velocity_dt_s)
            prior_innovation_limits = self._det_track_innovation_limits(
                prior_vx, prior_vy, ref)
            out = tuple(int(v) for v in self._track_box_ncc(frame, ref))
            # TEXTURE FLOOR: a low-variance template normalizes to ~1.0 anywhere, so its
            # argmax is noise (003937 e15: onset crop matched 26px off a static meter at
            # score 1.00). Below the floor the match is unmatchable regardless of score ->
            # take the weak path (the detector's box), which is exactly right at onset.
            if float(getattr(self, "_det_tmpl_std", 0.0)) < float(self._det_tmpl_std_min):
                self._det_track_score = 0.0
            # A broad local search is necessary, but a broad DISPLACEMENT is not.  Reject a
            # strong duplicate-scene match before it can reach the existing detector-deviation
            # clamp: that clamp intentionally opens on moving shots and otherwise permits one
            # bad 30-60px match to be served for a frame.  _track_box_ncc updates velocity when
            # it finds a strong candidate, so restore the pre-match state on refusal as well.
            if (float(self._det_track_score) >= float(self._det_track_min)
                    and not self._det_track_innovation_allowed(
                        prior_vx, prior_vy, ref, out, limits=prior_innovation_limits)):
                self._det_track_vx = prior_vx
                self._det_track_vy = prior_vy
                self._det_track_velocity_dt_s = prior_velocity_dt
                self._det_track_score = 0.0
            if float(self._det_track_score) >= float(self._det_track_min):
                matched = out
                g = float(self._det_track_gain)
                if g > 0.0:
                    pull_x = g * ((held[0] + held[2] * 0.5)
                                  - (out[0] + out[2] * 0.5))
                    pull_y = g * ((held[1] + held[3]) - (out[1] + out[3]))
                    # The trusted-motion history proves that a large detector/track gap
                    # can be ordinary inference age, not template drift.  Cap the vector
                    # impulse there; on a static/untrusted history keep the original gain
                    # so detector evidence still arrests template random walk quickly.
                    _motion_trusted = bool(self._det_hist_vel()[3])
                    # This is expressed in pixels of the frame being tracked (the live
                    # 1280x720 replay that established the 2px bound), unlike the older
                    # deviation knob whose historical units are width-scaled.
                    _pull_cap = float(self._det_track_reconcile_max_px)
                    _pull_mag = float(np.hypot(pull_x, pull_y))
                    if _motion_trusted and _pull_cap > 0.0 and _pull_mag > _pull_cap:
                        _psc = _pull_cap / max(1.0e-6, _pull_mag)
                        pull_x *= _psc
                        pull_y *= _psc
                    nx = out[0] + pull_x
                    ny = out[1] + pull_y
                    out = (int(round(nx)), int(round(ny)), int(out[2]), int(out[3]))
                # DEVIATION BUDGET (see the DEV knob note): clamp the tracked box to within
                # cap px of the detector's held box on BOTH axes -- the tracker may deviate
                # only as far as detector staleness at the measured meter speed can explain.
                cap = (float(self._det_track_dev_base) * max(1.0, float(self.W or 720.0) / 720.0)
                       + self._det_hist_speed() * float(self._det_track_dev_s))
                ddx = out[0] - held[0]
                ddy = out[1] - held[1]
                match_clipped = abs(ddx) > cap or abs(ddy) > cap
                if match_clipped:
                    # A clipped match may not become the next derivative origin
                    # outside the detector's permitted deviation envelope.
                    self._det_track_last_match = None
                    self._det_track_last_match_ts = None
                    self._det_track_vx = prior_vx
                    self._det_track_vy = prior_vy
                    self._det_track_velocity_dt_s = prior_velocity_dt
                    self._det_track_scale_match = None
                    self._det_scale_pending = None
                    out = (int(held[0] + max(-cap, min(cap, ddx))),
                           int(held[1] + max(-cap, min(cap, ddy))),
                           int(out[2]), int(out[3]))
                # Reconciliation changes the box-to-template anchor, not appearance.
                # Previously every-frame reseeding accidentally accumulated this
                # correction while also learning each transient fill edge/occluder.
                # Preserve that geometric convergence between genuine source
                # refreshes without touching the stable template pixels or the raw
                # matched-anchor derivative used for physical velocity.
                if not match_clipped:
                    self._det_tmpl_cx_offset -= float(out[0] - matched[0])
                    self._det_tmpl_bottom_offset -= float(out[1] - matched[1])
                if self._det_kalman_search and not match_clipped and had_template:
                    self._det_kalman_observe(matched, self._det_track_sample_ts)
                self._det_track_box = out
                self._det_track_box_ts = self._det_track_sample_ts
                if not had_template:
                    # A same-frame bootstrap cannot prove a camera transform.
                    self._det_track_scale_match = None
                    self._det_scale_pending = None
                elif self._det_track_scale_match is not None:
                    self._det_track_scale_match = (*self._det_track_scale_match, float(now))
                    if self._det_track_scale_match[0]:
                        self._det_scale_pending = None
                    else:
                        try:
                            if self._det_cumulative_scale_match(frame, matched, now):
                                self._det_track_scale_match = (
                                    True, int(out[2]), int(out[3]), float(now))
                        except Exception:
                            # Optional scale corroboration must never discard
                            # this frame's already-accepted primary position.
                            self._det_scale_pending = None
                # Refresh only at detector cadence, AFTER the old template localized this
                # frame.  This preserves the proven no-per-frame-reseed rule (the low-fill
                # transient otherwise compounds) while preventing a fresh detector box
                # from self-pinning the output to its own stale/jittery location.
                # A deviation-clipped output is deliberately NOT the location
                # verified by NCC. Keep the prior appearance rather than learning
                # the pixels between that refused match and the detector box.
                if refresh_template and not match_clipped:
                    self._seed_track_template(frame, out)
                    self._det_tmpl_ts = float(now)
                elif (match_clipped and float(now) - float(self._det_tmpl_ts)
                        > float(self._det_tmpl_max_s)):
                    # Keep the same expiry as weak matches; persistent appearance
                    # change must still re-bootstrap from the detector geometry.
                    self._seed_track_template(frame, held)
                    self._det_tmpl_ts = float(now)
                return out
            # A rejected/low-texture match may have updated the primitive's motion
            # state before its caller rejected it. Keep neither its velocity impulse
            # nor an anchor that would join across this unobserved frame.
            self._det_track_vx = prior_vx
            self._det_track_vy = prior_vy
            self._det_track_velocity_dt_s = prior_velocity_dt
            self._det_track_last_match = None
            self._det_track_last_match_ts = None
            self._det_track_scale_match = None
            self._det_scale_pending = None
            # A fresh async detector rectangle is not evidence that its current
            # notch pixels are visible. Replacing a usable template on a weak
            # frame learned the occluder (even a flat/blank patch), so the first
            # revealed moving frame could not match the old meter appearance.
            # Keep that appearance through the existing bounded expiry. A
            # low-texture bootstrap has no usable appearance to preserve. Fresh
            # detector rescale evidence and the original expiry still re-bootstrap.
            # Output/fill/presence authority remain on the detector-held box.
            if ((refresh_template
                    and (geometry_rebased
                         or float(getattr(self, '_det_tmpl_std', 0.0))
                            < float(self._det_tmpl_std_min)))
                    or (float(now) - float(self._det_tmpl_ts)) > float(self._det_tmpl_max_s)):
                self._seed_track_template(frame, held)
                self._det_tmpl_ts = float(now)
                self._det_track_box = held
                self._det_track_box_ts = self._det_track_sample_ts
            elif (self._det_tmpl is not None
                    and float(getattr(self, '_det_tmpl_std', 0.0))
                        >= float(self._det_tmpl_std_min)):
                # Keep the search origin together with the preserved appearance.
                # One hidden notch must not move next frame's NCC search from the
                # last localized position back to the inference-old detector box:
                # on a pan that otherwise loses the first visible frame despite
                # retaining an exact-match template. `ref` carries detector-owned
                # dimensions and already passed the existing detector snap bound.
                # This is SEARCH state only: return the held box below, clear the
                # derivative pair above, and require ordinary current-pixel NCC /
                # innovation / deviation checks before serving any relocated box.
                self._det_track_box = ref
            else:
                self._det_track_box = held
                self._det_track_box_ts = self._det_track_sample_ts
            return held
        except Exception:
            self._det_track_scale_match = None
            self._det_scale_pending = None
            return held

    def _seed_track_template(self, frame, box) -> None:
        """Store the meter's BOTTOM-NOTCH crop (grey) from a FRESH detector box.

        The notch -- the bottom edge plus a little of the lower fill -- is the part of the meter
        whose appearance does NOT change as the bar fills, which is why the retired detector
        tracked it instead of the whole bar (see _track_box_ncc). Under TRACK CONTINUITY it is
        refreshed from the TRACKED position at detector cadence, after the old template has
        localized the current frame (see _det_track_step); with continuity off, it refreshes on
        every fresh detection. Either way a zoom/scale change cannot leave a stale patch behind."""
        try:
            x, y, w, h = (int(v) for v in box)
            if w <= 0 or h <= 0:
                return
            H, W = frame.shape[:2]
            cx = x + w * 0.5
            b = y + h                                   # bottom edge
            half = max(16, w // 2 + 8)                  # a little wider than the bar
            x0 = int(max(0, cx - half)); x1 = int(min(W, cx + half))
            y0 = int(max(0, b - self._NOTCH_ABOVE)); y1 = int(min(H, b + self._NOTCH_BELOW))
            if (x1 - x0) >= 12 and (y1 - y0) >= 6:
                self._det_tmpl = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
                self._det_tmpl_size = (w, h)
                self._det_tmpl_pos = (int(x), int(y))
                # Texture of the seeded patch. A LOW-variance template makes
                # TM_CCOEFF_NORMED meaningless -- tiny correlations normalize to ~1.0
                # anywhere, and the argmax wanders (measured on 003937 e15: an onset
                # notch crop, pre-fill, matched 26px left of a STATIC meter at score
                # 1.00 and walked the box 78px off). The step treats a template below
                # the std floor as unmatchable and serves the detector box instead.
                self._det_tmpl_std = float(self._det_tmpl.std())
                # A refreshed patch has a new coordinate origin; use its actual
                # integer crop boundaries, not the odd-width box's implied centre.
                self._det_tmpl_cx_offset = 0.5 * float(x0 + x1) - float(cx)
                self._det_tmpl_bottom_offset = float(y0 + self._NOTCH_ABOVE) - float(b)
                self._det_track_last_match = (
                    0.5 * float(x0 + x1), float(y0 + self._NOTCH_ABOVE))
                self._det_track_last_match_ts = self._det_track_sample_ts
            else:
                self._det_track_last_match = None
                self._det_track_last_match_ts = None
                self._det_tmpl = None
                self._det_tmpl_size = None
                self._det_tmpl_cx_offset = 0.0
                self._det_tmpl_bottom_offset = 0.0
                self._det_tmpl_pos = None
                self._det_tmpl_std = 0.0
        except Exception:
            self._det_track_last_match = None
            self._det_track_last_match_ts = None
            self._det_tmpl = None
            self._det_tmpl_size = None
            self._det_tmpl_cx_offset = 0.0
            self._det_tmpl_bottom_offset = 0.0
            self._det_tmpl_pos = None
            self._det_tmpl_std = 0.0

    # ---------------------------------------------------------------------------------- #
    #  LOCK LIFECYCLE (retired meter_detector.py port -- see the __init__ knob block for
    #  the full design note and the retired-code landmarks each constant comes from).
    # ---------------------------------------------------------------------------------- #
    def _det_jump_gate(self) -> float:
        """Position-jump tolerance in px, scaled with frame width exactly as the retired
        _StabilityValidator._acq_jump_gate_px did (base 80px was tuned on a ~720-wide capture;
        at 1280 it is ~142px)."""
        try:
            w = float(self.W or 0.0)
        except Exception:
            w = 0.0
        return float(self._det_jump_base) * max(1.0, w / 720.0)

    def _det_result_this_arm(self, result_ts) -> bool:
        """Whether a detector result can describe the current physical-arm epoch.

        An unlocked reader must fail closed when the locator omits its source timestamp:
        without that clock there is no way to distinguish a current-frame meter from the
        pre-press async result that produced the live court snaps.  An existing LOCKED
        low/rising bridge is judged before this predicate in ``_det_on_found`` and remains
        intentionally untouched.
        """
        if not self._shot_armed_hw or self._hw_arm_ts is None:
            return True
        try:
            _rts = float(result_ts)
            return bool(np.isfinite(_rts)
                        and _rts + 1.0e-6 >= float(self._hw_arm_ts))
        except (TypeError, ValueError, OverflowError):
            return False

    def _reset_detfill_nogreen_evidence(self) -> None:
        """Clear the detector-only capless-onset evidence window."""
        self._df_nogreen_key = None
        self._df_nogreen_samples = []

    def _start_det_lock_generation(self) -> None:
        """Mint a process-local identity for one lifecycle-approved lock."""
        self._det_lock_generation = int(
            getattr(self, "_det_lock_generation", 0) or 0) + 1
        self._reset_detfill_nogreen_evidence()

    def _detfill_nogreen_time_limits(self):
        """Return (maximum adjacent gap, maximum three-frame span) in seconds.

        At the shipped 33ms acquisition cadence, four opportunities (132ms) may
        separate adjacent accepted results and six (198ms) may cover the whole
        three-frame proof.  The detector TTL (200ms shipped) is a hard ceiling on
        both, while the 60fps floors retain headroom when sync acquisition is
        configured with a zero/smaller interval.  Evidence therefore represents
        one onset burst and can never accumulate over a long held arm.
        """
        ttl = max(0.0, float(self._det_ttl_s))
        cadence = max(1.0 / 60.0, float(self._det_acq_interval_s))
        max_gap = min(ttl, max(6.0 / 60.0, 4.0 * cadence))
        max_span = min(ttl, max(9.0 / 60.0, 6.0 * cadence))
        return max_gap, max_span

    @staticmethod
    def _detfill_nogreen_box_continuous(previous, current) -> bool:
        """Whether two locator boxes can be the same moving meter.

        Compare horizontal centre and bottom edge because detector-height breathing
        must not manufacture motion.  The 0.75-height allowance covers the measured
        moving-shot stride while still treating a scene-object switch as a new run.
        Lifecycle's stricter ownership identity remains the primary boundary.
        """
        try:
            ax, ay, aw, ah = (float(v) for v in previous)
            bx, by, bw, bh = (float(v) for v in current)
            if min(aw, ah, bw, bh) <= 0.0:
                return False
            wr = max(aw, bw) / min(aw, bw)
            hr = max(ah, bh) / min(ah, bh)
            if wr > 1.60 or hr > 1.35:
                return False
            acx, bcx = ax + aw * 0.5, bx + bw * 0.5
            abot, bbot = ay + ah, by + bh
            motion = float(np.hypot(bcx - acx, bbot - abot))
            return motion <= max(16.0, 0.75 * max(ah, bh))
        except (TypeError, ValueError, OverflowError):
            return False

    def _advance_detfill_nogreen_rise(
            self, *, shot_epoch, lock_generation, source_ts, sample_ts,
            coarse_fill, locator_box, white_ribbon, locator_fresh) -> bool:
        """Latch structure after three fresh, low, capless rising meter frames.

        This is deliberately detector-only and latch-only.  A frame advances the
        window only when it carries a unique, fresh locator result accepted into the
        current lifecycle lock.  Static, falling, high-fill, stale, re-seated, or
        geometrically discontinuous evidence clears the window and cannot stamp.
        """
        if not locator_fresh:
            # Repeated async slots are neutral: they may neither advance nor fake the
            # three distinct source frames required by this proof.
            return False
        try:
            epoch = int(shot_epoch)
            generation = int(lock_generation)
            src = float(source_ts)
            now = float(sample_ts)
            fill = float(coarse_fill)
            box = tuple(float(v) for v in locator_box)
        except (TypeError, ValueError, OverflowError):
            self._reset_detfill_nogreen_evidence()
            return False
        current = bool(
            self._detfill_green_latch and self._require_gameplay_eligibility
            and self._det_lifecycle and self._det_state == 'locked'
            and self._shot_armed_hw and epoch > 0
            and epoch == int(self._physical_shot_epoch)
            and generation > 0
            and generation == int(getattr(self, "_det_lock_generation", 0) or 0)
            and white_ribbon and len(box) == 4
            and np.isfinite(src) and np.isfinite(now) and np.isfinite(fill)
            and self._det_result_this_arm(src)
            and -1.0e-6 <= now - src <= float(self._det_ttl_s) + 1.0e-6
            and 0.0 <= fill <= 20.0)
        if not current:
            self._reset_detfill_nogreen_evidence()
            return False

        key = (epoch, generation)
        if self._df_nogreen_key != key:
            self._reset_detfill_nogreen_evidence()
            self._df_nogreen_key = key

        samples = self._df_nogreen_samples
        if samples:
            prev_src, prev_now, prev_fill, prev_box = samples[-1]
            # Equal source stamps are one async detector result replayed across
            # capture frames.  Older stamps are a source discontinuity.
            if abs(src - prev_src) <= 1.0e-9:
                return False
            if src < prev_src or now <= prev_now:
                self._reset_detfill_nogreen_evidence()
                self._df_nogreen_key = key
                self._df_nogreen_samples.append((src, now, fill, box))
                return False
            max_gap, max_span = self._detfill_nogreen_time_limits()
            first_src, first_now = samples[0][0], samples[0][1]
            if (src - prev_src > max_gap + 1.0e-6
                    or now - prev_now > max_gap + 1.0e-6
                    or src - first_src > max_span + 1.0e-6
                    or now - first_now > max_span + 1.0e-6):
                # A long-held arm can see unrelated low white objects seconds
                # apart.  Start over at this frame; only one short detector burst
                # may satisfy the three-sample proof.
                self._reset_detfill_nogreen_evidence()
                self._df_nogreen_key = key
                self._df_nogreen_samples.append((src, now, fill, box))
                return False
            if not self._detfill_nogreen_box_continuous(prev_box, box):
                self._reset_detfill_nogreen_evidence()
                self._df_nogreen_key = key
                self._df_nogreen_samples.append((src, now, fill, box))
                return False
            step = fill - prev_fill
            dt_ms = (now - prev_now) * 1000.0
            max_step = (float(self._press_step_margin_pp)
                        + max(float(self._press_max_rate_pct_ms), 0.70) * dt_ms)
            if not (float(self._press_pair_min_rise_pp) <= step <= max_step):
                # Static/falling/non-physical evidence begins a new possible run at
                # this frame, but can never count as the second or third frame.
                self._reset_detfill_nogreen_evidence()
                self._df_nogreen_key = key
                self._df_nogreen_samples.append((src, now, fill, box))
                return False

        samples = self._df_nogreen_samples
        samples.append((src, now, fill, box))
        if len(samples) > 3:
            del samples[:-3]
        if len(samples) == 3 and samples[-1][2] - samples[0][2] >= 4.0:
            self._latch_gameplay_structure_proof(epoch)
            return True
        return False

    def _det_reset_lock_state(self) -> None:
        """Clear every per-lock artefact so nothing stale can steer the next lock (the retired
        reset() + the existing veto-clear discipline: 'stale motion must not extrapolate a new
        lock')."""
        if getattr(self, '_box_latch_epoch', 0):
            self._clear_box_latch('lock_state_reset')
        self._det_state = 'idle'
        self._det_streak = 0
        self._det_pend_box = None
        self._det_pend_ts = -1.0e9
        self._det_nm_strikes = 0
        self._det_strike_box = None
        self._det_strike_n = 0
        self._det_lock_fill0 = None
        self._det_lock_fill_max = -1.0
        self._det_lock_prev_fill = None   # last accepted read (ordered-rise run)
        self._det_lock_up_n = 0            # consecutive reads each _fresh_up_pp higher
        self._det_lock_was_warm = False
        self._det_lock_read_n = 0
        self._det_last_presence = False
        self._det_emit = None
        self._det_kalman_state = None
        self._det_pixel_geometry = None
        self._det_pixel_ruler_reject = False
        self._det_measurement_box = None
        self._det_display_box = None
        self._det_last_box = None
        self._det_last_found_ts = -1.0e9
        self._det_fill_hist.clear()
        self._det_coarse_fill_hist.clear()
        self._det_box_hist.clear()
        self._det_hist_velocity_cache = None
        self._det_tmpl = None
        self._det_tmpl_size = None
        self._det_tmpl_cx_offset = 0.0
        self._det_tmpl_bottom_offset = 0.0
        self._det_track_vx = 0.0
        self._det_track_vy = 0.0
        self._det_track_last_match = None
        self._det_track_box = None
        self._det_track_box_ts = None
        self._det_track_last_match_ts = None
        self._det_track_sample_ts = None
        self._det_track_clock_timed = None
        self._det_track_velocity_dt_s = 1.0 / 60.0
        self._det_track_scale_match = None
        self._subpx_camera_ref = None
        self._subpx_camera_scale = 1.0
        self._det_scale_reference = None
        self._det_scale_pending = None
        self._det_tmpl_ts = -1.0e9
        self._det_tmpl_pos = None
        self._det_rescue_request_ts = -1.0e9
        self._df_green_streak = 0        # chevron streak dies with the lock
        self._reset_detfill_nogreen_evidence()
        # Sub-pixel per-shot constants are per-LOCK artefacts too: a new lock must re-seed
        # its own scale/anchor rather than inherit the dead lock's (same discipline as the
        # veto-clear below; the press reset covers the bridged-lock case).
        self._subpx_D = None; self._subpx_provisional = False
        self._subpx_off = None
        self._subpx_seed = []
        self._subpx_base_hist = []; self._subpx_last_base_rel = None
        self._subpx_carry_pending = False
        self._coarse_denom_ref = None
        self._det_track_score = 0.0
        self._det_occ_direct.clear()
        self._det_occ_key = None
        self._det_occ_locator_source_positive = False
        self._det_occ_recovered = False
        self._det_occ_support = 0.0
        self._det_occ_kind = ""
        self._det_occ_censored_reject = False
        self._det_occ_current_full_negative = False
        self._det_occ_current_source_age = float("inf")
        self._det_occ_negative_bridge_used = False

    def _ghost_press_flush_summary(self, reason: str) -> None:
        """[ORION_READER_GHOST_PRESS_BREAK] Emit the one-per-press eviction summary.

        Called when the guard stands down (real onset seen) and at the next press arm
        (covers a window that simply expired). ERROR level so the native relay carries it
        (WARNINGs are throttled); the per-frame lines it replaces are logged at DEBUG."""
        _n_ev = int(getattr(self, "_press_ghost_evict_total", 0) or 0)
        _n_sup = int(getattr(self, "_press_ghost_suppress_total", 0) or 0)
        _n_imp = int(getattr(self, "_press_implausible_n", 0) or 0)
        if _n_ev <= 0 and _n_sup <= 0 and _n_imp <= 0:
            return
        try:
            _acq_logger.error(
                "GHOST PRESS SUMMARY: epoch=%d evictions=%d zero_holds=%d implausible=%d "
                "end=%s",
                int(self._press_ghost_summary_epoch or 0), _n_ev, _n_sup, _n_imp, reason)
        except Exception:
            pass
        self._press_ghost_evict_total = 0
        self._press_ghost_suppress_total = 0
        self._press_implausible_n = 0

    def _press_fill_allowance(self, ts: float) -> float:
        """[ORION_READER_PRESS_ONSET_PLAUSIBILITY] The ONSET-CLOCK bound alone: no real
        meter can show fill F before the press clock allows it. Used by the ghost guard's
        stand-down paths (a 'low' read the clock forbids is the fading leftover passing
        down through the band, never this press's onset). The RISE-STEP bound is judged
        separately in the serve veto (see there): it applies only to jumps landing at or
        above the engine's 40%% anchor bound, because a GENUINE pop-in serve legitimately
        out-steps the ribbon rate while the bar itself is still growing (measured
        session_20260830_191051 epochs 25/26/40: 4.8 -> 16.0 in 19ms = 0.59 pct/ms, real
        rise) -- but a genuine pop-in read is always LOW; only a leftover jump lands high."""
        _age_ms = max(0.0, (float(ts) - float(self._hw_arm_ts)) * 1000.0)
        return (self._press_first_margin_pp
                + self._press_max_rate_pct_ms
                * max(0.0, _age_ms - self._press_onset_floor_ms))

    def _press_step_allowance(self, ts: float):
        """[ORION_READER_PRESS_ONSET_PLAUSIBILITY] The RISE-STEP bound: how high a serve
        may land given the last published serve of this press. None when no serve has
        published yet (the onset clock alone judges the first sight)."""
        if self._press_last_pub_fill is None or self._press_last_pub_ts is None:
            return None
        _dt_ms = max(0.0, (float(ts) - float(self._press_last_pub_ts)) * 1000.0)
        return (float(self._press_last_pub_fill)
                + self._press_step_margin_pp
                + self._press_max_rate_pct_ms * _dt_ms)

    def _press_onset_pair_advance(self, fill: float, ts: float) -> bool:
        """[ORION_READER_PRESS_ONSET_PLAUSIBILITY] Sliding rising-pair check for the ghost
        guard's stand-down paths: True when (previous candidate, this read) form a genuine
        onset step -- rising by at least _press_pair_min_rise_pp and by no more than the
        physical step allowance. A fading leftover DECLINES through the low band (measured
        epochs 32/50/51/58) and can never satisfy the rise; a real onset always does
        (measured genuine pairs rise 3-7pp per serve). The candidate always slides to the
        current read so a later genuine pair is judged on fresh evidence."""
        _cand = self._press_onset_cand
        self._press_onset_cand = (float(fill), float(ts))
        if _cand is None:
            return False
        if (self._hw_arm_ts is not None
                and (float(ts) - float(self._hw_arm_ts)) * 1000.0
                < self._press_onset_floor_ms):
            # No press's meter can complete its first two reads before the onset floor
            # (capture+render alone is ~230ms; earliest measured genuine ownership 226ms).
            # The candidate still slides, so a real pair straddling the floor stands the
            # guard down on its first read past it.
            return False
        _d = float(fill) - float(_cand[0])
        _dt_ms = max(0.0, (float(ts) - float(_cand[1])) * 1000.0)
        return (self._press_pair_min_rise_pp <= _d
                <= self._press_step_margin_pp
                + self._press_max_rate_pct_ms * _dt_ms)

    def _press_bridge_has_fresh_rise(self) -> bool:
        """Prove that a sub-40 lock crossing a new press is one continuing meter.

        A pre-arm pixel lock is not ownership evidence by itself. Require the last two
        canonical (coarse/native) fills to be a recent, physically plausible rise and the
        detector locator to have corroborated the held box within its normal freshness
        window. This preserves measured rapid 27->34% re-press continuity while rejecting
        a static low court/player box such as live epoch 38's repeated 14.3% false lock.
        """
        if (getattr(self, "_det_state", "idle") != "locked"
                or getattr(self, "_det_last_box", None) is None):
            return False
        try:
            _hist = [(float(t), float(f)) for t, f in self._det_coarse_fill_hist
                     if np.isfinite(float(t)) and np.isfinite(float(f)) and float(f) > 0.0]
        except Exception:
            return False
        if len(_hist) < 2:
            return False
        (_t0, _f0), (_t1, _f1) = _hist[-2], _hist[-1]
        _dt_s = _t1 - _t0
        if not (0.0 < _dt_s <= 0.120):
            return False
        _rise = _f1 - _f0
        # Low-fill pop-in steps are measured as fast as ~0.59 pp/ms and are explicitly
        # allowed by the normal press gate while they remain below 40. Use the same safe
        # class here with headroom; the lower rise bound and fresh locator identity do the
        # static-false-lock rejection.
        _max_rise = (self._press_step_margin_pp
                     + max(self._press_max_rate_pct_ms, 0.70) * _dt_s * 1000.0)
        if not (self._press_pair_min_rise_pp <= _rise <= _max_rise):
            return False
        try:
            _locator_age = _t1 - float(self._det_last_found_ts)
        except (TypeError, ValueError, OverflowError):
            return False
        return (-1e-6 <= _locator_age
                <= max(0.0, float(self._det_ttl_s)) + 1e-6)

    # ------------------------------------------------------------------ box identity latch
    def _box_latch_live(self) -> bool:
        """True while this press's published lock owns the box geometry."""
        if not self._box_latch or self._box_latch_epoch == 0:
            return False
        if int(self._box_latch_epoch) != int(self._physical_shot_epoch or 0):
            return False
        if int(self._box_latch_generation) != int(getattr(self, '_det_lock_generation', 0) or 0):
            return False
        return self._det_state == 'locked'

    def _arm_box_latch(self, box, ts) -> None:
        """[ORION_READER_BOX_LATCH] Latch the box identity once this press's own meter has
        been sighted and published. From here the geometry may only change through an
        explicit drop, which the engine sees; a silent mid-shot re-seed may not replace it."""
        if not self._box_latch or box is None:
            return
        _ep = int(self._physical_shot_epoch or 0)
        if _ep == 0 or not self._shot_armed_hw:
            return
        _gen = int(getattr(self, '_det_lock_generation', 0) or 0)
        if self._box_latch_epoch == _ep and self._box_latch_generation == _gen:
            return
        try:
            self._box_latch_box = tuple(int(v) for v in box)
        except (TypeError, ValueError, OverflowError):
            return
        self._box_latch_epoch = _ep
        self._box_latch_generation = _gen
        _acq_logger.error(
            "BOX LATCHED: epoch=%d generation=%d box=[%d,%d,%d,%d] "
            "reader_geometry_latched=1 native_ownership=unconfirmed",
            _ep, _gen, *self._box_latch_box)

    def _clear_box_latch(self, why: str) -> None:
        if self._box_latch_epoch == 0:
            return
        _acq_logger.debug("BOX LATCH CLEARED: epoch=%d why=%s",
                          int(self._box_latch_epoch), why)
        self._box_latch_epoch = 0
        self._box_latch_generation = 0
        self._box_latch_box = None

    def _locator_pending_pairs(self, ts=None):
        """[ORION_READER_FORGET_RATE_LIMIT] The proposer's live first-sight pairs, or ()."""
        fn = getattr(getattr(self, "_meter_detector", None), "pending_pairs", None)
        if not callable(fn):
            return ()
        try:
            return tuple(fn(ts) or ())
        except Exception:
            return ()

    def _det_forget_locator_position(self, why: str, box=None, ts=None) -> bool:
        """[ORION_READER_GHOST_FORGET_LOCATOR] Tell the proposer to forget WHERE it last saw
        a meter, so the object this reader just refused cannot bridge the proposer's own
        acceptance gates into the next proposal. Inert for a proposer without the API.

        `box` is the GHOST: a 4-tuple (x, y, w, h) or a 2-tuple centre. It is what makes the
        forget surgical -- the proposer keeps the first-sight pairs that belong to a DIFFERENT
        column (this press's real meter) and drops only the ghost's own. Passing None keeps the
        original total amnesia, so a caller that genuinely does not know stays fail-closed.

        [ORION_READER_FORGET_RATE_LIMIT 2026-09-16] Rate-limited: one forget per press per
        zone, and never while a pair is mid-promotion (see the __init__ knob block -- unrated,
        this call at ~10 Hz is what blinded six consecutive presses on 09-16). -> did it fire?
        """
        if not self._ghost_forget_locator:
            return False
        fn = getattr(getattr(self, "_meter_detector", None), "forget_position", None)
        if not callable(fn):
            return False
        epoch = int(getattr(self, "_physical_shot_epoch", 0) or 0)
        if epoch != self._loc_forget_epoch:
            self._loc_forget_epoch = epoch
            self._loc_forget_zones = []
            self._loc_forget_unzoned = False
            self._loc_forget_logged = False
            self._loc_forget_defer_n = 0
        zone = None
        if box is not None:
            try:
                if len(box) >= 4 and float(box[2]) > 0.0:
                    zone = (float(box[0]) + float(box[2]) * 0.5,
                            float(box[1]) + float(box[3]) * 0.5)
                elif len(box) >= 2:
                    zone = (float(box[0]), float(box[1]))
            except (TypeError, ValueError, IndexError, OverflowError):
                zone = None
        if self._forget_rate_limit:
            # (a) the same ghost, again, in the same press: the proposer already forgot it.
            if zone is not None:
                for _z in self._loc_forget_zones:
                    if (abs(_z[0] - zone[0]) <= self._forget_zone_px
                            and abs(_z[1] - zone[1]) <= self._forget_zone_px):
                        self._loc_forget_suppressed += 1
                        return False
            elif self._loc_forget_unzoned:
                self._loc_forget_suppressed += 1
                return False
            # (b) a pair is one frame from deciding -- let it decide.
            _live = self._locator_pending_pairs(ts)
            if _live and self._loc_forget_defer_n < self._forget_defer_max:
                self._loc_forget_defer_n += 1
                self._loc_forget_deferred += 1
                _acq_logger.debug(
                    "LOCATOR FORGET DEFERRED: epoch=%d why=%s pairs=%s n=%d",
                    epoch, why, ",".join(_live), self._loc_forget_defer_n)
                return False
        try:
            kept = fn(box) if box is not None else fn()
        except TypeError:
            try:      # a proposer that predates `box=`: total amnesia, as it always did
                kept = fn()
            except Exception:
                return False
        except Exception:
            return False
        try:
            kept = tuple(kept or ())
        except TypeError:
            kept = ()
        self._loc_forget_defer_n = 0
        if zone is not None:
            self._loc_forget_zones.append(zone)
        else:
            self._loc_forget_unzoned = True
        self._loc_forget_n += 1
        self._det_diag['loc_forget'] = self._det_diag.get('loc_forget', 0) + 1
        _msg = ("LOCATOR POSITION FORGOTTEN: epoch=%d zone=%s kept_pairs=%s why=%s")
        _args = (epoch,
                 ("(%d,%d)" % (int(zone[0]), int(zone[1]))) if zone is not None else "-",
                 (",".join(kept) if kept else "-"), why)
        if not self._loc_forget_logged:
            self._loc_forget_logged = True
            _acq_logger.error(_msg, *_args)     # ERROR: the native relay throttles WARNINGs
        else:
            _acq_logger.debug(_msg, *_args)
        return True

    # ------------------------------------------------------------ press withhold summary
    def _flush_press_withhold_summary(self, reason: str) -> None:
        """[ORION_READER_PRESS_WITHHOLD_SUMMARY] One ERROR line per press naming, per layer,
        how many reads it kept from the engine. Idempotent per epoch; silent when nothing was
        withheld. See the __init__ counter block for why this exists at ERROR."""
        _ep = int(self._pw_epoch or 0)
        _tot = (self._pw_ghost_static + self._pw_cold_first + self._pw_idle_gate
                + self._pw_fresh_zone + self._pw_static_zone + self._pw_reseed)
        if _ep and _tot > 0 and _ep != self._pw_flushed_epoch:
            self._pw_flushed_epoch = _ep
            try:
                _acq_logger.error(
                    "PRESS WITHHOLD SUMMARY: epoch=%d ghost_static=%d cold_first=%d "
                    "idle_gate=%d fresh_zone=%d static_zone=%d reseed=%d end=%s",
                    _ep, self._pw_ghost_static, self._pw_cold_first, self._pw_idle_gate,
                    self._pw_fresh_zone, self._pw_static_zone, self._pw_reseed, reason)
            except Exception:
                pass
        self._pw_ghost_static = 0
        self._pw_cold_first = 0
        self._pw_idle_gate = 0
        self._pw_fresh_zone = 0
        self._pw_static_zone = 0
        self._pw_reseed = 0

    def _arm_press_fresh_zone(self, box, why: str) -> None:
        """[ORION_READER_FRESH_AFTER_GHOST] Remember where a leftover was refused at/after this
        press. Until this press sights its own meter low AND rising, an unproven >=40% read
        from a lock in that zone is withheld (the engine could not own it anyway)."""
        # Part of the ghost-press guard family: ORION_READER_GHOST_PRESS_BREAK=0 pins every
        # leftover-meter layer off, this one included.
        if not (self._fresh_after_ghost and self._ghost_press_break) or box is None:
            return
        try:
            self._press_fresh_zone = (float(box[0]) + float(box[2]) * 0.5,
                                      float(box[1]) + float(box[3]) * 0.5)
        except (TypeError, ValueError, IndexError, OverflowError):
            return
        self._press_fresh_zone_logged = False
        _acq_logger.debug("FRESH-ONSET REQUIRED IN ZONE: why=%s zone=(%d,%d)",
                          why, int(self._press_fresh_zone[0]), int(self._press_fresh_zone[1]))

    # ------------------------------------------------------------ static-zone quarantine
    def _static_zone_find(self, cx: float, cy: float, now: float):
        """The remembered static-drop record covering (cx, cy), pruning expired ones."""
        if not self._static_zone_q:
            return None
        keep = []
        hit = None
        for z in self._static_zones:
            if (now - float(z['ts'])) > float(z.get('ttl', self._static_zone_ttl_s)):
                continue
            keep.append(z)
            if (abs(cx - float(z['cx'])) <= self._static_zone_px
                    and abs(cy - float(z['cy'])) <= self._static_zone_px * 2.0):
                hit = z
        self._static_zones = keep
        return hit

    def _note_static_zone_drop(self, now: float, reason: str, box) -> bool:
        """Score a strike when a lock dies WITHOUT ever having proved a rise.

        Returns True when the caller must NOT arm the warm re-acquire memory: re-seeding a
        never-risen lock from the spot it died is exactly the idle lock/drop churn measured
        live (~20 cycles in 40s on one court line, with no press anywhere near)."""
        if not self._static_zone_q or box is None:
            return False
        if int(getattr(self, '_det_lock_read_n', 0) or 0) < self._static_zone_min_reads:
            return False
        _f0 = getattr(self, '_det_lock_fill0', None)
        _fmax = float(getattr(self, '_det_lock_fill_max', -1.0) or -1.0)
        if _f0 is not None and (_fmax - float(_f0)) >= self._static_zone_rise_pp:
            return False                     # it rose: a real meter lived here
        try:
            cx = float(box[0]) + float(box[2]) * 0.5
            cy = float(box[1]) + float(box[3]) * 0.5
        except (TypeError, ValueError, IndexError, OverflowError):
            return False
        z = self._static_zone_find(cx, cy, now)
        if z is None:
            z = {'cx': cx, 'cy': cy, 'n': 0, 'ts': now, 'logged': False,
                 'ttl': self._static_zone_repeat_ttl(cx, cy, now)}
            self._static_zones.append(z)
            if len(self._static_zones) > self._static_zone_max:
                self._static_zones.pop(0)
        # Follow the object (a court line drifts with the camera) and score the strike.
        z['cx'] = cx * 0.5 + float(z['cx']) * 0.5
        z['cy'] = cy * 0.5 + float(z['cy']) * 0.5
        z['n'] = int(z['n']) + 1
        z['ts'] = float(now)
        if int(z['n']) >= self._static_zone_strikes and not z['logged']:
            z['logged'] = True
            _rep = self._static_zone_note_repeat(z, now)
            _acq_logger.error(
                "STATIC ZONE QUARANTINED: zone=(%d,%d) strikes=%d reason=%s reads=%d "
                "rise=%.1fpp (< %.1f) ttl=%.0fs repeat=%d - no warm re-seed and no publish "
                "there until it rises in order or goes absent",
                int(z['cx']), int(z['cy']), int(z['n']), reason,
                int(getattr(self, '_det_lock_read_n', 0) or 0),
                (_fmax - float(_f0)) if _f0 is not None else -1.0,
                self._static_zone_rise_pp, float(z.get('ttl', self._static_zone_ttl_s)), _rep)
        # The FIRST never-risen drop at a position still arms the warm memory: a genuine
        # mid-shot dropout must re-latch on one proposal (pinned by
        # test_simple_reader_lock_lifecycle::test_warm_reacquire_relatches_on_one_proposal,
        # and a meter can legitimately die before it is measured rising). Only a REPEAT
        # offender -- the same position producing _static_zone_strikes never-risen locks -- is
        # refused the seed and quarantined, which is exactly the live idle churn's signature.
        return int(z['n']) >= self._static_zone_strikes

    def _publish_static_rank_zones(self, now: float) -> None:
        """Give the CV proposer armed, live quarantine hints; never remove evidence.

        A static object can otherwise keep winning on area while a real smaller meter
        waits elsewhere. This is a ranking hint, NOT an exclusion or an ownership grant.
        Legacy/ONNX proposers have no setter and keep their existing behavior.
        """
        base = getattr(getattr(self, "_meter_detector", None), "_base", None)
        setter = getattr(base, "set_static_rank_zones", None)
        if not callable(setter):
            return
        zones = []
        if self._static_zone_q and self._shot_armed_hw:
            for z in self._static_zones:
                start = float(z['ts'])
                end = start + float(z.get('ttl', self._static_zone_ttl_s))
                if int(z['n']) >= self._static_zone_strikes and start <= now <= end:
                    zones.append((float(z['cx']), float(z['cy']),
                                  float(self._static_zone_px), float(self._static_zone_px * 2),
                                  start, end))
        setter(tuple(zones))

    def _static_zone_quarantined(self, box, now: float):
        """The armed record covering this box, or None."""
        if not self._static_zone_q or box is None:
            return None
        try:
            cx = float(box[0]) + float(box[2]) * 0.5
            cy = float(box[1]) + float(box[3]) * 0.5
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        z = self._static_zone_find(cx, cy, now)
        if z is None or int(z['n']) < self._static_zone_strikes:
            return None
        return z

    # -- repeat-offender ledger (see the knob block) ---------------------------------
    def _static_zone_bucket(self, cx: float, cy: float):
        """96px cells: a record matches +-48px in x / +-96px in y, so one cell per object."""
        return (int(float(cx) // 96.0), int(float(cy) // 96.0))

    def _static_zone_repeat_ttl(self, cx: float, cy: float, now: float) -> float:
        """TTL for a NEW record at (cx, cy): base, doubled per prior quarantine of the cell."""
        base = float(self._static_zone_ttl_s)
        if not self._static_zone_repeat_escalate:
            return base
        rec = self._static_zone_repeat.get(self._static_zone_bucket(cx, cy))
        if rec is None or (float(now) - float(rec['ts'])) > self._static_zone_repeat_forget_s:
            return base
        return base * float(min(2 ** int(rec['n']), self._static_zone_repeat_max_x))

    def _static_zone_note_repeat(self, z, now: float) -> int:
        """The record just reached the strike threshold: count it against its cell and give
        the record the escalated TTL. Returns the cell's quarantine count (1 = first time)."""
        if not self._static_zone_repeat_escalate:
            return 1
        key = self._static_zone_bucket(z['cx'], z['cy'])
        rec = self._static_zone_repeat.get(key)
        if rec is None or (float(now) - float(rec['ts'])) > self._static_zone_repeat_forget_s:
            rec = {'n': 0, 'ts': float(now)}
        rec['n'] = int(rec['n']) + 1
        rec['ts'] = float(now)
        self._static_zone_repeat[key] = rec
        z['ttl'] = float(self._static_zone_ttl_s) * float(
            min(2 ** (int(rec['n']) - 1), self._static_zone_repeat_max_x))
        # Bound the ledger: forget the stalest cells past the record cap.
        if len(self._static_zone_repeat) > 4 * self._static_zone_max:
            stale = sorted(self._static_zone_repeat.items(), key=lambda kv: kv[1]['ts'])
            for k, _ in stale[:len(stale) - 4 * self._static_zone_max]:
                self._static_zone_repeat.pop(k, None)
        return int(rec['n'])

    def _static_zone_release(self, z, why: str) -> None:
        """A meter proved itself here: retire the record outright, and the cell's history
        with it -- a spot a real meter rose in is not a repeat offender."""
        if z is None:
            return
        try:
            self._static_zones.remove(z)
        except ValueError:
            return
        self._static_zone_repeat.pop(self._static_zone_bucket(z['cx'], z['cy']), None)
        _acq_logger.error(
            "STATIC ZONE RELEASED: zone=(%d,%d) why=%s (a real meter proved a rise here)",
            int(z['cx']), int(z['cy']), why)

    def _note_static_zone_absence(self, *, found: bool, fresh: bool, scope: str,
                                  new_result: bool) -> None:
        """Sustained full-frame absence decays the strikes: the object is gone, so the
        evidence that this position is a persistent false lock decays with it. A find, a
        partial scan or a live lock all reset the run (only a full scan can prove absence)."""
        if not self._static_zone_q or not self._static_zones:
            return
        if not fresh or not new_result or scope != 'full':
            return
        if found or self._det_state == 'locked':
            self._static_zone_nofind_n = 0
            return
        self._static_zone_nofind_n += 1
        if self._static_zone_nofind_n < self._static_zone_absence_results:
            return
        self._static_zone_nofind_n = 0
        keep = []
        for z in self._static_zones:
            z['n'] = int(z['n']) - 1
            if int(z['n']) < self._static_zone_strikes:
                z['logged'] = False
            if int(z['n']) > 0:
                keep.append(z)
        self._static_zones = keep

    def _note_press_ghost_full_result(self, *, found: bool, fresh: bool,
                                      scope: str, new_result: bool) -> None:
        """Retire one quarantined meter identity after proven full-frame absence.

        The zone is a visual identity guard, not a permanent location ban. Two unique
        fresh FULL-frame nofinds prove the old meter disappeared; drop its coasted lock
        without warm memory and let a newly rendered same-zone meter acquire normally.
        A partial scan cannot prove absence and a full-frame find resets the run.
        """
        if self._press_ghost_zone is None or not fresh or not new_result:
            return
        if scope != "full":
            return
        if found:
            self._press_ghost_full_nofind_n = 0
            return
        self._press_ghost_full_nofind_n += 1
        if self._press_ghost_full_nofind_n < self._ghost_full_absence_results:
            return

        _count = self._press_ghost_full_nofind_n
        _retired_zone = self._press_ghost_zone
        self._press_ghost_zone = None
        self._press_ghost_level = None
        self._press_ghost_zone_low_n = 0
        self._press_ghost_zero_n = 0
        self._press_ghost_full_nofind_n = 0
        self.conf = 0.0
        self.box = None
        self.tmpl = None
        try:
            self._det_reset_lock_state()
            self._det_warm_pos = None
            self._det_warm_ts = -1.0e9
            self._det_active_box = None
        except Exception:
            pass
        # [ORION_READER_GHOST_FORGET_LOCATOR] The reader-side seeds are cleared above; the
        # PROPOSER's own positional memory is the one that survived and re-proposed the
        # retired ghost's column on co-location alone (see the __init__ knob block).
        self._det_forget_locator_position('ghost_identity_retired', box=_retired_zone,
                                          ts=self._frame_ts_last)
        # [ORION_READER_FRESH_AFTER_GHOST] The identity is retired, not the requirement: until
        # this press sights its own meter low AND rising, an unproven >=40% read from a lock
        # in that zone stays withheld.
        if (_retired_zone is not None and self._fresh_after_ghost
                and self._ghost_press_break):
            self._press_fresh_zone = (float(_retired_zone[0]), float(_retired_zone[1]))
            self._press_fresh_zone_logged = False
        _acq_logger.error(
            "GHOST IDENTITY RETIRED AFTER FULL ABSENCE: epoch=%d full_nofinds=%d "
            "(same-zone next meter may acquire fresh)",
            int(self._physical_shot_epoch or 0), int(_count))

    def _det_next_acq_scan_region(self, shot_epoch: int) -> str:
        """Return the next one-inference acquisition view for this physical epoch.

        A remembered side is only written after the held detector box proves a real
        fill rise. It is therefore a priority hint, never ownership evidence. Every
        three opportunities still cover full + both overlapping edges.
        """
        if not self._det_phased_acquire:
            self._det_acq_scan_last = 'full'
            return 'full'
        try:
            epoch = int(shot_epoch or 0)
        except (TypeError, ValueError, OverflowError):
            epoch = 0
        if self._det_acq_scan_epoch != epoch:
            self._det_acq_scan_epoch = epoch
            self._det_acq_scan_index = 0
        side = self._det_acq_last_side
        if side == 'left':
            order = ('left', 'full', 'right')
        elif side == 'right':
            order = ('right', 'full', 'left')
        else:
            order = ('full', 'left', 'right')
        region = order[int(self._det_acq_scan_index) % len(order)]
        self._det_acq_scan_index = (int(self._det_acq_scan_index) + 1) % len(order)
        self._det_acq_scan_last = region
        return region

    def _det_drop_lock(self, now: float, reason: str) -> None:
        """Drop the lock and arm the WARM re-acquire memory at the spot it died (retired
        _note_lock_loss -> _warm_pos: 'a candidate near where a corroborated lock was lost
        moments ago is the same meter continuing its rise').

        [ORION_READER_STATIC_ZONE_QUARANTINE] That sentence is only true of a lock that was
        RISING. A lock that never rose is decor, and warm-seeding the next acquisition from
        the spot it died is what produced ~20 lock/drop cycles in 40s on one court line with
        no press anywhere near (live 2026-09-15 22:36:26-58Z). Such a drop scores a strike
        against the position and arms NO warm memory."""
        _static_hit = False
        if self._static_zone_q:
            _sb = self._det_last_box
            if _sb is None and self._det_emit is not None:
                _e = self._det_emit
                _sb = (float(_e[0]) - float(_e[2]) * 0.5, float(_e[1]) - float(_e[3]),
                       float(_e[2]), float(_e[3]))
            try:
                _static_hit = self._note_static_zone_drop(now, reason, _sb)
            except Exception:
                _static_hit = False
        if not _static_hit:
            if self._det_emit is not None:
                self._det_warm_pos = (float(self._det_emit[0]),
                                      float(self._det_emit[1]) - float(self._det_emit[3]) * 0.5)
            elif self._det_last_box is not None:
                lb = self._det_last_box
                self._det_warm_pos = (lb[0] + lb[2] * 0.5, lb[1] + lb[3] * 0.5)
            self._det_warm_ts = float(now)
        else:
            self._det_warm_pos = None
            self._det_warm_ts = -1.0e9
        self._det_diag['drop'] += 1
        try:
            self.last_debug = dict(self.last_debug or {})
            self.last_debug['det_drop'] = reason
        except Exception:
            pass
        self._clear_box_latch('lock_dropped:' + str(reason))
        self._det_reset_lock_state()

    def _det_on_found(self, box, conf: float, now: float, new_result: bool,
                      result_ts=None) -> bool:
        """Lifecycle decision for a FRESH detector proposal. Returns True when the proposal is
        ACCEPTED into the lock (the caller then updates _det_last_box / history / template);
        False = the proposal must not move anything this frame (pending warm-up, or a
        teleport outlier accruing re-seed strikes)."""
        # After this press ends, keep measuring only its existing meter for the
        # landing/oracle tail. A warm memory, lingering hardware arm, or repeated
        # strong court proposal must not acquire/reseed a different object.
        epoch = int(getattr(self, '_physical_shot_epoch', 0) or 0)
        closed = epoch > 0 and epoch == int(getattr(self, '_pa_released_epoch', 0) or 0)
        if closed:
            reference = None
            if self._det_state == 'locked':
                if self._det_emit is not None:
                    reference = (float(self._det_emit[0]),
                                 float(self._det_emit[1]) - float(self._det_emit[3]) * 0.5)
                elif self._det_last_box is not None:
                    x, y, w, h = self._det_last_box
                    reference = (x + w * 0.5, y + h * 0.5)
            x, y, w, h = (float(v) for v in box)
            jump = (float(np.hypot(x + w * 0.5 - reference[0],
                                   y + h * 0.5 - reference[1]))
                    if reference is not None else float('inf'))
            if not np.isfinite(jump) or jump > self._det_jump_gate() * max(1.0, float(self._det_track_scale)):
                self._det_diag['post_release_acquire_refused'] = (
                    self._det_diag.get('post_release_acquire_refused', 0) + 1)
                return False
        if not self._det_lifecycle:
            return True                      # pre-port behaviour: every fresh box is adopted
        bx, by, bw, bh = (float(v) for v in box)
        cx = bx + bw * 0.5
        cy = by + bh * 0.5
        gate = self._det_jump_gate()
        if self._det_state == 'locked':
            # Reference = where the lock IS now (smoothed emit tracks the meter through the
            # coast; the raw last box can be a stale async result).
            if self._det_emit is not None:
                rcx = float(self._det_emit[0])
                rcy = float(self._det_emit[1]) - float(self._det_emit[3]) * 0.5
            elif self._det_last_box is not None:
                lb = self._det_last_box
                rcx = lb[0] + lb[2] * 0.5
                rcy = lb[1] + lb[3] * 0.5
            else:
                rcx, rcy = cx, cy
            jump = float(np.hypot(cx - rcx, cy - rcy))
            if jump <= gate * max(1.0, float(self._det_track_scale)):
                # KEEP: in-gate motion (a fade slides the meter ~9-13px/frame -- real motion,
                # not instability). Fresh evidence heals every drop counter.
                self._det_nm_strikes = 0
                self._det_strike_box = None
                self._det_strike_n = 0
                return True
            # TELEPORT outlier: never move the lock on one far proposal (the live 543px |dcx|
            # snap). Count re-seed strikes on UNIQUE results only -- an async latest() repeat
            # must not double-count -- and require them mutually consistent (retired
            # MeterBoxKalman 3-strike re-seed).
            if not new_result:
                return False
            self._det_diag['outlier'] += 1
            if (self._det_strike_box is not None
                    and float(np.hypot(cx - self._det_strike_box[0],
                                       cy - self._det_strike_box[1])) <= gate):
                self._det_strike_n += 1
            else:
                self._det_strike_box = (cx, cy)
                self._det_strike_n = 1
            if self._det_strike_n >= int(self._det_reseed_n):
                # [ORION_READER_BOX_LATCH] ...but not while the engine owns this shot on this
                # lock. Adopting a far proposal as a FRESH lock swaps the box the fill
                # denominator is measured against, mid-shot, with no drop for the engine to
                # see. A genuine break must go through _det_drop_lock instead.
                if self._box_latch_live():
                    self._box_latch_refused += 1
                    self._pw_reseed += 1
                    self._det_diag['reseed_refused'] = (
                        self._det_diag.get('reseed_refused', 0) + 1)
                    self._det_strike_n = 0
                    self._det_strike_box = None
                    _acq_logger.error(
                        "RESEED REFUSED: epoch=%d generation=%d latched=[%d,%d,%d,%d] "
                        "proposal=(%d,%d) - the engine owns this shot; a break must drop the "
                        "lock, not re-seat it",
                        int(self._box_latch_epoch), int(self._box_latch_generation),
                        *(self._box_latch_box or (0, 0, 0, 0)), int(cx), int(cy))
                    return False
                # Genuine relocation (three consistent far proposals ~ a new shot elsewhere):
                # adopt it as a FRESH lock -- full state reset so the old lock's motion/fill
                # cannot bleed into the new one.
                self._det_diag['reseed'] += 1
                self._det_reset_lock_state()
                self._det_state = 'locked'
                self._start_det_lock_generation()
                self._det_diag['lock'] += 1
                return True
            return False
        # --- idle / pending: ACQUIRE hysteresis ---
        # A detector result proves what was present in ITS SOURCE FRAME, not what is
        # present in the physical-shot epoch active when latest() happens to be consumed.
        # In async mode an inference submitted just before a press can complete after the
        # press.  The press-time stale-lock breaker resets the lifecycle, and the old code's
        # armed/strong shortcut then immediately re-installed that pre-press box.  Live
        # session_20260831_210338 epoch 53 reproduced the consequence: the old meter box sat
        # on bare court and measured a plausible 0->53% rise until the first post-press
        # result arrived.  Fence every COLD acquisition to a source frame from this epoch;
        # an already-locked low/rising bridge remains untouched above.
        if self._shot_armed_hw and self._hw_arm_ts is not None:
            # A pending acquire is evidence from its own consumption epoch.  Do not let
            # one weak pre-press candidate combine with one post-press candidate to fake
            # the two-result acquisition streak.  This is deliberately narrower than a
            # lifecycle reset: a locked low/rising bridge already returned above.
            if (self._det_state == 'pending'
                    and float(self._det_pend_ts) + 1.0e-6 < float(self._hw_arm_ts)):
                self._det_state = 'idle'
                self._det_streak = 0
                self._det_pend_box = None
                self._det_pend_ts = -1.0e9
            if not self._det_result_this_arm(result_ts):
                return False
        warm = (self._det_warm_pos is not None
                and (now - self._det_warm_ts) <= float(self._det_warm_s)
                and float(np.hypot(cx - self._det_warm_pos[0],
                                   cy - self._det_warm_pos[1])) <= gate * 2.0)
        if warm and self._static_zone_quarantined(box, now) is not None:
            # [ORION_READER_STATIC_ZONE_QUARANTINE] A quarantined position may still be
            # acquired -- the real meter can render there, and only a lock can watch it rise
            # -- but never through the WARM shortcut, which exists to continue a rising meter
            # and here would just re-seed the decor from its own corpse. The ordinary
            # two-result acquire streak still applies.
            warm = False
        # STRONG proposal paired with the PHYSICAL arm: the press is the behavioural evidence
        # (retired T-a4 refused loc_strong standalone). This is what keeps first-detected fill
        # at 0-10% -- the armed onset frame may latch immediately.
        # Confidence + a physical press may waive the two-result acquire streak only for
        # a UNIQUE result. latest() repeats one slot across frames; replaying the same box
        # after a production-boundary ghost eviction is not new corroboration.
        strong = (float(conf) >= float(self._det_acq_strong)
                  and bool(self._shot_armed_hw) and bool(new_result))
        if new_result:
            if (self._det_pend_box is not None
                    and (now - self._det_pend_ts) <= 0.5   # pend TTL = retired _RECENT_PROX_S
                    and float(np.hypot(cx - self._det_pend_box[0],
                                       cy - self._det_pend_box[1])) <= gate):
                self._det_streak += 1
            else:
                self._det_streak = 1
            self._det_pend_box = (cx, cy)
            self._det_pend_ts = float(now)
            self._det_state = 'pending'
        if warm or strong or self._det_streak >= int(self._det_acq_frames):
            self._det_state = 'locked'
            self._start_det_lock_generation()
            self._det_streak = 0
            self._det_pend_box = None
            self._det_lock_fill0 = None
            self._det_lock_fill_max = -1.0
            self._det_lock_prev_fill = None   # last accepted read (ordered-rise run)
            self._det_lock_up_n = 0            # consecutive reads each _fresh_up_pp higher
            self._det_lock_was_warm = bool(warm)   # warm mid-rise re-locks skip the veto
            self._det_lock_read_n = 0
            self._det_last_presence = False
            self._det_emit = None
            self._det_nm_strikes = 0
            self._det_strike_box = None
            self._det_strike_n = 0
            if warm:
                self._det_warm_pos = None       # consumed -- a fresh loss must re-arm it
                self._det_warm_ts = -1.0e9
            self._det_diag['lock'] += 1
            return True
        return False

    def _det_on_nofound(self, now: float, new_result: bool) -> bool:
        """A FRESH confident 'no meter anywhere' verdict. Returns True when it dropped the
        lock.

        Hysteresis, exactly as the retired detector split it: a lock backed by INDEPENDENT
        corroboration survives locator misses (its red-presence coast; ours = a PROVEN
        >=_det_coast_rise_pp fill rise AND the white ribbon still measured at the tracked spot
        last frame), bounded by the wall-clock coast; a lock with NO such corroboration -- the
        decor false-lock case the veto exists for -- dies after _det_nm_drop consecutive UNIQUE
        verdicts, which is ~50ms at sync cadence, FASTER than the old 0.35s hold expiry.

        WHY not drop every lock at N strikes: measured on session_20260830_003937, YOLO goes
        found=0 for 17-33 CONSECUTIVE frames inside real shot runs (motion blur / the green cap
        VFX -- the exact cap-detection gap the retired _PEAK_HOLD existed for). Any small strike
        cap would re-create the owner's on/off/on churn wholesale."""
        if not self._det_lifecycle or self._det_state != 'locked':
            return False
        if not new_result:
            return False
        self._det_nm_strikes += 1
        rise_ok = (self._det_lock_fill0 is not None
                   and (self._det_lock_fill_max - self._det_lock_fill0)
                   >= float(self._det_coast_rise_pp))
        if self._det_nm_strikes >= int(self._det_nm_drop) and not (
                rise_ok and self._det_last_presence):
            self._det_drop_lock(now, 'nometer_strikes')
            return True
        return False

    def _det_submit_priority(self, frame, now: float, region: str = 'full') -> int:
        """Queue newest pixels on the locator worker, never call ORT on this thread.

        AsyncMeterLocator exposes priority APIs.  The ordinary submit fallback keeps
        duck-typed/legacy locators working, but live production always takes the
        non-blocking priority path.
        """
        try:
            if self._meter_detector is None:
                return False
            fn = getattr(self._meter_detector, 'submit_priority_region', None)
            if callable(fn):
                fn(frame, float(now), region)
                return 2
            fn = getattr(self._meter_detector, 'submit_priority', None)
            if callable(fn):
                fn(frame, float(now))
                return 2
            fn = getattr(self._meter_detector, 'submit', None)
            if callable(fn):
                fn(frame, float(now))
                return 1
        except Exception:
            return False
        return False

    def _det_hard_reseat_serving(self, frame, box, now: float) -> None:
        """Reset position-anchored serving state on an accepted rescue result."""
        bb = tuple(int(v) for v in box)
        self._det_tmpl = None
        self._det_tmpl_size = None
        self._det_tmpl_cx_offset = 0.0
        self._det_tmpl_bottom_offset = 0.0
        self._det_tmpl_pos = None
        self._det_track_box = None
        self._det_track_vx = 0.0
        self._det_track_vy = 0.0
        self._det_track_last_match = None
        self._det_track_box_ts = None
        self._det_track_last_match_ts = None
        self._det_track_sample_ts = float(now)
        self._det_track_clock_timed = True
        self._det_track_velocity_dt_s = 1.0 / 60.0
        self._det_scale_reference = None
        self._det_scale_pending = None
        self._seed_track_template(frame, bb)
        self._det_tmpl_ts = float(now)
        self._det_emit = [bb[0] + bb[2] * 0.5, float(bb[1] + bb[3]),
                          float(bb[2]), float(bb[3])]

    def _det_read_rescue(self, frame, now):
        """Request a freshest-frame box after a held-box fill read fails.

        The old implementation ran one full ORT inference inline here.  During the
        real-game failure that meant repeated 44-70ms capture-callback stalls and a
        60->31fps collapse.  The worker now receives a priority/latest-frame request;
        the next unique full-frame result still passes the ordinary lifecycle/jump
        policy, then earns the same hard serving-state reseat.  Until then the caller
        keeps the held box and its honest zero -- no synthetic fill and no false lock.
        """
        try:
            active = float(getattr(self, '_det_rescue_request_ts', -1.0e9))
            if (active > -1.0e8
                    and float(now) - active <= max(float(self._det_ttl_s), 0.20)):
                return None
            queued = self._det_submit_priority(frame, float(now), 'full')
            if not queued:
                return None
            self._det_diag['rescue'] = self._det_diag.get('rescue', 0) + 1
            # Only the priority API gives this request a worker-result identity. A
            # legacy ordinary-submit fallback is useful as a wake, but a later normal
            # result must not be mistaken for the requested hard-reseat response.
            if queued >= 2:
                self._det_rescue_request_ts = float(now)
        except Exception:
            return None
        return None

    def _det_kalman_observe(self, box, ts):
        """Constant-velocity position filter; only independently accepted NCC updates it.

        X uses the centre and Y uses the bottom anchor so detector height breathing
        cannot masquerade as camera motion. Convert to centre-Y only for rendering.
        No prediction changes visibility, lock lifetime, fill, or ownership.
        """
        if ts is None or not np.isfinite(ts):
            self._det_kalman_state = None
            return
        x, y, w, h = (float(v) for v in box)
        z = np.array([x + w * .5, y + h], dtype=np.float64)
        k = self._det_kalman_state
        dt = float(ts) - k['ts'] if k is not None else 0.0
        if k is None or not (0.0 < dt <= float(self._subpx_bridge_max_s)):
            # Repeated source frames are not independent measurements.
            if k is not None and dt == 0.0:
                return
            self._det_kalman_state = dict(
                x=np.array([z[0], z[1], 0., 0.]),
                P=np.diag([4., 4., 40000., 40000.]), ts=float(ts), n=1)
            return
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        G = np.array([[.5 * dt * dt, 0.], [0., .5 * dt * dt],
                      [dt, 0.], [0., dt]])
        xp = F @ k['x']
        P = F @ k['P'] @ F.T + (G @ G.T) * 250000.
        K = np.linalg.solve(P[:2, :2] + np.eye(2) * 2.25, P[:2, :]).T
        xnew = xp + K @ (z - xp[:2])
        # Joseph form keeps covariance symmetric/positive under long sessions.
        A = np.eye(4)
        A[:, :2] -= K
        Pnew = A @ P @ A.T + 2.25 * K @ K.T
        self._det_kalman_state = dict(x=xnew, P=Pnew, ts=float(ts), n=k['n'] + 1)

    def _det_bar_geometry(self, frame, box, ts=None):
        """Find local cap + ribbon edges, never the brightest nearby column alone.

        This is a bounded observation inside a lifecycle-accepted box neighbourhood,
        not a locator. Both cap and narrow white base must exist in THIS frame.
        Hidden pixels never inherit geometry from an earlier frame.
        """
        try:
            x, y, w, h = (int(v) for v in box)
            if w < 6 or h < 20:
                return None
            H, W = frame.shape[:2]
            step = self._det_track_predicted_step(self._det_track_vx, self._det_track_vy)
            padx = min(60, max(10, int(w), int(abs(step[0]) * 2.5 + 10)))
            pady = min(35, max(8, int(h * .15), int(abs(step[1]) * 1.5 + 8)))
            x0, x1 = max(0, x - padx), min(W, x + w + padx)
            y0, y1 = max(0, y - pady), min(H, y + h + pady)
            if x1 - x0 < 8 or y1 - y0 < 20:
                return None
            hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
            hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
            green = ((hue >= 40) & (hue <= 85) & (sat >= 90) & (val >= 90))
            # Only the tip neighbourhood may propose a cap. Court logos below
            # the expected tip cannot turn into a different full-track ruler.
            green[max(0, min(green.shape[0], int(y - y0 + .30 * h))):] = False
            n, _, stats, centers = cv2.connectedComponentsWithStats(green.astype(np.uint8), 8)
            candidates = []
            for i in range(1, n):
                gx, gy, gw, gh, area = stats[i]
                if (area < 8 or gw < 3 or gw > max(8, 1.5 * w)
                        or gh > max(12, .18 * h) or gy <= 0):
                    continue
                cx = float(centers[i, 0]) + x0
                if abs(cx - (x + w * .5)) > padx:
                    continue
                candidates.append((abs(cx - (x + w * .5)) + .2 * abs(y0 + gy - y), i))
            white = ((val >= 200) & (sat <= 65))
            for _, i in sorted(candidates):
                gx, gy, gw, gh, area = (int(v) for v in stats[i])
                ccx = float(centers[i, 0])
                # Restrict to a cap-aligned ribbon; a larger white jersey outside
                # it does not participate in either the profile or edge estimate.
                lo = max(0, int(ccx - max(4., gw)))
                hi = min(white.shape[1], int(ccx + max(4., gw)) + 1)
                ribbon = white[:, lo:hi].copy()
                ribbon[:gy + gh] = False
                rn, _, rs, _ = cv2.connectedComponentsWithStats(ribbon.astype(np.uint8), 8)
                pieces = []
                for j in range(1, rn):
                    bx, by, bw, bh, mass = (int(v) for v in rs[j])
                    bot = by + bh
                    if (bh < 4 or mass < 12 or bw < 3 or bw > max(6., 1.9 * gw)
                            or bx <= 0 or bx + bw >= hi - lo
                            or bot >= ribbon.shape[0] - 2
                            or abs((lo + bx + .5 * bw) - ccx) > max(3., .6 * gw)
                            or not (.55 * h <= bot - gy <= 1.25 * h)):
                        continue
                    pieces.append((abs((bot + y0) - (y + h - 8)), bx, by, bw, bh))
                if not pieces:
                    continue
                _, bx, by, bw, bh = min(pieces)
                cols = slice(lo + bx, lo + bx + bw)
                rowprof = val[:, cols].mean(axis=1).astype(float)
                body = float(np.median(rowprof[by:min(by + bh, by + 4)]))
                upper = float(np.median(rowprof[max(0, by - 4):by]))
                lower = float(np.median(rowprof[by + bh:by + bh + 3]))
                if body - upper < 40. or body - lower < 40.:
                    continue
                def cross(a, b, level):
                    return a + (level - rowprof[a]) / (rowprof[b] - rowprof[a])
                top = cross(by - 1, by, .5 * (body + upper))
                base = cross(by + bh - 1, by + bh, .5 * (body + lower))
                xp = val[by:by + bh, :].mean(axis=0).astype(float)
                lefti, righti = lo + bx, lo + bx + bw - 1
                if (xp[lefti] - xp[lefti - 1] < 40.
                        or xp[righti] - xp[righti + 1] < 40.):
                    continue
                # Half-height crossings of the actual local white edges.
                left, right = lefti - .5, righti + .5
                # Cap paint count has an antialiased edge; use its half-plateau
                # crossing instead of the integer first-green row alone.
                gp = green[:, gx:gx + gw].mean(axis=1)
                peak = float(np.max(gp[gy:gy + gh]))
                cap_top = gy - 1 + (.5 * peak - gp[gy - 1]) / max(1e-6, gp[gy] - gp[gy - 1])
                cap_bottom = gy + gh - 1 + (gp[gy + gh - 1] - .5 * peak) / max(
                    1e-6, gp[gy + gh - 1] - gp[gy + gh])
                span = float(base - cap_top)
                if not (.55 * h <= span <= 1.25 * h) or not (cap_top < top < base):
                    continue
                return dict(cx=x0 + .5 * (left + right), left=x0 + left,
                    right=x0 + right, cap_top=y0 + cap_top, cap_bottom=y0 + cap_bottom,
                    base=y0 + base, edge=y0 + top, coarse_edge=y0 + by,
                    span=span, green_pixels=area, ts=ts)
        except (ValueError, TypeError, IndexError, cv2.error, ZeroDivisionError):
            return None
        return None

    def _proposer_stats(self):
        """Compact counters of the box PROPOSER (the CV locator keeps gate counters; the ONNX
        locator has none). Read-only; '-' when there is nothing to report."""
        try:
            base = getattr(self._meter_detector, "_base", None)
            st_ = getattr(base, "stats", None)
            if not isinstance(st_, dict):
                return "-"
            # [ORION_LOCATOR_IDLE_REUSE 2026-09-15] `idle_reuse` rides here so the scans the
            # locator did NOT run on frozen menu/loading frames are visible next to the ones
            # it did; a live run that shows idle_reuse=0 across a menu means the signature is
            # not seeing the feed as static (noise, a moving screensaver, or the knob is off).
            keys = ("hit", "no_tip", "outline_weak", "no_outline", "outline_bridged", "roi",
                    "full", "idle_reuse",
                    # [ANCHOR INSTRUMENT 2026-09-21] see meter_locator_cv stats
                    "idle_hit", "anchor_patch_hit", "refused_outside",
                    "anchor_none", "anchor_conf_lo", "anchor_conf_mid", "anchor_conf_hi")
            return "{" + ",".join("%s=%s" % (k, st_.get(k, 0)) for k in keys if k in st_) + "}"
        except Exception:
            return "-"

    def detector_health_snapshot(self):
        """One dict for the UI's Meter Detection card (sidecar -> native telemetry):
        provider, inference ms, lifecycle counters and the proposer's own gate counters.
        Presentation-only: nothing here feeds the engine."""
        try:
            det = self._meter_detector
            _dg = getattr(self, "_det_diag", None) or {}
            base = getattr(det, "_base", None)
            st_ = getattr(base, "stats", None)
            out = {
                "provider": str(getattr(det, "provider", "none")) if det is not None else "none",
                "infer_ms": round(float(getattr(det, "infer_ms", 0.0) or 0.0), 2) if det is not None else 0.0,
                "state": str(getattr(self, "_det_state", "-")),
                "calls": int(_dg.get("calls", 0)), "found": int(_dg.get("found", 0)),
                "locks": int(_dg.get("lock", 0)), "drops": int(_dg.get("drop", 0)),
                "hot_submit": int(_dg.get("hot_submit", 0)), "reseat_x": int(_dg.get("reseat_x", 0)),
                "dims_locked": int(_dg.get("dims_locked", 0)),
                "det_only_veto": int(_dg.get("det_only_veto", 0)),
                "locator_exception": int(_dg.get("locator_exception", 0)),
                "fill_exception": int(_dg.get("fill_exception", 0)),
                "health_exception": int(_dg.get("health_exception", 0)),
            }
            if isinstance(st_, dict):
                for k in ("hit", "no_tip", "outline_weak", "no_outline", "outline_bridged",
                          "shape_irregular", "tip_spill", "shape_error", "shape_bridge_tip_refused",
                          "shape_full_body_bridged",
                          "idle_hit", "anchor_patch_hit", "refused_outside"):
                    out["cv_" + k] = int(st_.get(k, 0))
            return out
        except Exception:
            return None

    def _reseat_x_dx(self, frame, box):
        """[ORION_READER_RESEAT_X] Horizontal offset (px) that puts `box` on the white ribbon
        visible in this frame, or None. Whiteness = min(B,G,R) >= 200 counted per column over
        the box rows in a +-max window; exactly ONE contiguous run of ribbon width, whose
        white rows form a single vertical run ending in the lower half of the box, and whose
        centre is 3..max px from the box centre. Anything else fails closed. (Run width is
        judged against the box width, 0.30..0.85 w; the ribbon core is ~half the box.)"""
        try:
            x, y, w, h = (int(v) for v in box)
            H, W = frame.shape[:2]
            pad = int(self._reseat_x_max)
            x0, x1 = max(0, x - pad), min(W, x + w + pad)
            y0, y1 = max(0, y), min(H, y + h)
            if x1 - x0 < w + 6 or y1 - y0 < 20:
                return None
            Wn = frame[y0:y1, x0:x1].min(axis=2)
            white = Wn >= 200
            col = white.sum(axis=0)
            hot = col >= 3
            idx = np.flatnonzero(hot)
            if idx.size < 8:
                return None
            # the ribbon core is ~half the detector box width (11-13 of 23 px @720p); a run
            # narrower than 0.3 w is a court line, wider than 0.85 w is not a ribbon
            w_lo, w_hi = max(4, int(round(0.30 * w))), max(6, int(round(0.85 * w)))
            runs = [ru for ru in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if ru.size >= w_lo]
            runs = [ru for ru in runs if ru.size <= w_hi]
            if len(runs) != 1:
                return None
            ru = runs[0]
            # the ribbon's white rows are one vertical run per column, ending in the lower half
            core = white[:, ru[1]:ru[-1]]              # drop the run's edge columns
            rows = np.flatnonzero(core.mean(axis=1) >= 0.6)
            if rows.size < 3 or (rows[-1] - rows[0] + 1) != rows.size:
                return None
            if rows[-1] < 0.5 * (y1 - y0):
                return None
            cx_run = x0 + 0.5 * (float(ru[0]) + float(ru[-1]))
            dx = cx_run - (x + 0.5 * w)
            if not (3.0 <= abs(dx) <= float(pad)):
                return None
            return int(round(dx))
        except Exception:
            return None

    def _det_only_seat_veto(self, col, ts):
        """True when a colour-tier candidate must NOT seat: the proposer is the landmark CV
        locator and no fresh detector box overlaps the candidate (IoU < 0.3)."""
        try:
            if not self._det_only_seat:
                return False
            if str(getattr(self._meter_detector, "provider", "")) != "cv-contour" and not self._det_only_seat_force:
                return False
            box = self._det_last_box
            fresh = (box is not None and ts is not None and self._det_last_found_ts is not None
                     and 0.0 <= float(ts) - float(self._det_last_found_ts) <= self._det_only_seat_max_age_s)
            if fresh:
                ax, ay, aw, ah = (int(v) for v in box); bx, by, bw, bh = (int(v) for v in col)
                ix0, iy0 = max(ax, bx), max(ay, by); ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
                inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
                union = aw * ah + bw * bh - inter
                if union > 0 and inter / float(union) >= 0.3:
                    return False
            self._det_diag["det_only_veto"] = self._det_diag.get("det_only_veto", 0) + 1
            return True
        except Exception:
            return False

    def _det_lock_dims_active(self):
        if not self._det_lock_dims:
            return False
        if self._det_lock_dims_force:
            return True
        return str(getattr(self._meter_detector, "provider", "")) == "cv-contour"

    def _det_pin_dims(self, box, ts):
        """[ORION_READER_LOCK_BOX_DIMS] the detector's (w, h) on the tracker's bottom-centre."""
        try:
            if not self._det_lock_dims_active() or self._det_last_box is None:
                return box
            if ts is not None and self._det_last_found_ts is not None:
                if not (0.0 <= float(ts) - float(self._det_last_found_ts) <= self._det_lock_dims_max_age_s):
                    return box
            dw, dh = int(self._det_last_box[2]), int(self._det_last_box[3])
            x, y, w, h = (int(v) for v in box)
            if dw <= 0 or dh <= 0 or (w == dw and h == dh):
                return box
            cx = x + w * 0.5
            bottom = y + h
            self._det_diag["dims_locked"] = self._det_diag.get("dims_locked", 0) + 1
            return (int(round(cx - dw * 0.5)), int(bottom - dh), dw, dh)
        except Exception:
            return box

    def _det_measurement_boxes(self, frame, tracked, ts=None):
        """Keep the old display contract; an opt-in pixel crop never sees its EMA."""
        tracked = self._det_pin_dims(tuple(int(v) for v in tracked), ts)
        display = (tuple(int(v) for v in self._det_smooth_emit(tracked))
                   if self._det_lifecycle else tracked)
        display = self._det_pin_dims(display, ts)      # the emit EMA must not re-inflate the ruler
        self._det_display_box = display
        self._det_pixel_geometry = None
        measured = display
        if self._det_measure_raw:
            measured = tracked
            geometry = self._det_bar_geometry(frame, tracked, ts)
            self._det_pixel_geometry = geometry
            if geometry is not None:
                H, W = frame.shape[:2]
                x0 = max(0, int(np.floor(geometry['left'])) - 3)
                x1 = min(W, int(np.ceil(geometry['right'])) + 4)
                y0 = max(0, int(np.floor(geometry['cap_top'])) - 4)
                y1 = min(H, int(np.ceil(geometry['base'])) + 5)
                measured = (x0, y0, x1 - x0, y1 - y0)
        self._det_measurement_box = measured
        return measured, display

    def _measure_pixel_ruler(self, frame, box, ts, direct_top):
        """An opt-in independent bar ruler. Never mix missing geometry with box scale."""
        g = self._det_bar_geometry(frame, box, ts)
        self._det_pixel_geometry = g
        self._last_fill_coarse = 0.0
        self._last_subpx_transform = None
        self._dbg_subpx = dict(ok=0, gate='pixel_geometry_missing', ruler='pixel_bar')
        if g is None or abs(g['coarse_edge'] - (box[1] + direct_top)) > 3.0:
            self._det_pixel_ruler_reject = True
            self._last_fill_estimator_mode = ""
            self._last_fill_estimator_generation = 0
            return 0.0, None, -1
        D = g['span']
        hist = self._det_pixel_span_hist
        # Constant screen-space geometry: a camera pan never authorizes a new
        # scale. The session median is only an outlier guard, not a substitute
        # for this frame's directly observed cap-to-base denominator.
        if len(hist) >= 3:
            ref = float(np.median(hist))
            if abs(D - ref) > max(3., ref * .08):
                self._det_pixel_ruler_reject = True
                self._dbg_subpx['gate'] = 'pixel_ruler_outlier'
                self._last_fill_estimator_mode = ''
                self._last_fill_estimator_generation = 0
                return 0.0, None, -1
        if not hist:
            self._det_pixel_ruler_epoch += 1
        hist.append(D)
        coarse = np.clip((g['base'] - g['coarse_edge']) / D * 100., 0., 100.)
        sub = np.clip((g['base'] - g['edge']) / D * 100., 0., 100.)
        fill = float(sub if self._subpx_fill else coarse)
        self._last_fill_coarse = round(float(coarse), 2)
        mode = 'subpixel' if self._subpx_fill else 'coarse'
        self._stamp_fill_estimator(mode, ('pixel_bar', self._det_pixel_ruler_epoch))
        start = float(np.clip((g['base'] - g['cap_bottom']) / D * 100., 0., 100.))
        green = (round(start, 2), 100., round((start + 100.) * .5, 2),
                 round(100. - start, 2), round(min(1., g['green_pixels'] / 40.), 3),
                 g['green_pixels'])
        self._dbg_subpx = dict(ok=1, gate='', ruler='pixel_bar', anchor='pixel_base',
            top_sub=g['edge'] - box[1], base_sub=g['base'] - box[1],
            fill_sub=round(float(sub), 3), ruler_n=len(hist), measured_span=round(D, 4))
        self._last_subpx_transform = (g['base'] - box[1], D)
        return round(fill, 2), green, int(g['coarse_edge'] - box[1])

    def _det_smooth_emit(self, box):
        """Smoothed EMIT box (retired _Tk: responsive matching state, damped display state).
        The NCC-matched centre writes through uncapped (the retired template match wrote
        cx_emit directly -- 0.12px MAE beats any smoothing); everything else moves under a
        +/-8px/frame slew with a 0.65/0.35 EMA on the dims, so a single jittery YOLO box can
        no longer stride the served rectangle (live p90 |dcx| 10px, max 543px)."""
        try:
            x, y, w, h = (int(v) for v in box)
            if w <= 0 or h <= 0:
                return box
            cx = x + w * 0.5
            bot = float(y + h)
            e = self._det_emit
            if e is None:
                self._det_emit = [float(cx), bot, float(w), float(h)]
                return box
            slew = float(self._det_emit_slew)
            ncc_strong = (self._det_tmpl is not None
                          and float(self._det_track_score) >= float(self._det_track_min))
            if ncc_strong:
                e[0] = float(cx)
                # A strong match has already passed the same per-axis innovation gate
                # that protects X.  Write the meter's bottom edge through as well: an
                # unconditional 8px Y slew visibly trails diagonal/deep fades whose meter
                # moves 9-13px/frame, even while NCC is locked on the correct pixels.
                e[1] = bot
            else:
                e[0] += max(-slew, min(slew, cx - e[0]))
                e[1] += max(-slew, min(slew, bot - e[1]))
            e[2] = 0.65 * e[2] + 0.35 * float(w)
            e[3] = 0.65 * e[3] + 0.35 * float(h)
            scale_match = getattr(self, '_det_track_scale_match', None)
            if ncc_strong and scale_match is not None and scale_match[0]:
                # Current pixels matched the detector-proposed resize, not just
                # its original appearance. A dimension EMA would crop the newly
                # enlarged cap and turn camera zoom into apparent fill progress.
                # Ordinary detector breathing still takes the original-template
                # route and retains the dimension damping above.
                e[2], e[3] = float(w), float(h)
            nw = max(1, int(round(e[2])))
            nh = max(1, int(round(e[3])))
            nx = int(round(e[0] - nw * 0.5))
            ny = int(round(e[1] - nh))
            try:
                W = int(self.W or 0)
                H = int(self.H or 0)
            except Exception:
                W = H = 0
            if W > 0:
                nx = max(0, min(W - nw, nx))
            if H > 0:
                ny = max(0, min(H - nh, ny))
            return (nx, ny, nw, nh)
        except Exception:
            return box

    def _refine_box_local(self, frame, box):
        """Re-centre the detector box on the ACTUAL meter column, every frame, on CPU.

        DETECT-THEN-TRACK: the YOLO detector only updates at its own cadence (~72ms), so
        between detections the box is a guess -- it drifts off a meter that moves with the
        shooting player (visible live: the box sitting ~45px left of the white bar). Snapping
        to the strongest bright-neutral COLUMN inside a padded window costs ~0.2ms and gives a
        60fps position lock, so the box sticks to the meter instead of trailing it.

        Returns a corrected (x, y, w, h), or the input box when no confident column is found
        (fail-safe: never move the box on weak evidence)."""
        try:
            x, y, w, h = (int(v) for v in box)
            if w <= 0 or h <= 0:
                return box
            H, W = frame.shape[:2]
            padx = max(10, int(w * 1.6))       # search window: wide enough to catch the drift
            x0 = max(0, x - padx); x1 = min(W, x + w + padx)
            y0 = max(0, y); y1 = min(H, y + h)
            if x1 - x0 < w + 2 or y1 - y0 < 10:
                return box
            hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
            bright = ((hsv[:, :, 2] >= 200) & (hsv[:, :, 1] <= 65)).astype(np.float32)
            colsum = bright.sum(axis=0)        # per-column count of meter-white pixels
            if colsum.max() < 3.0:
                return box                     # no bright column here -> keep the detector box
            # Best w-wide window (the meter is a narrow ribbon): boxcar over the column profile.
            k = max(3, min(int(w), colsum.size))
            csum = np.cumsum(np.concatenate(([0.0], colsum)))
            win = csum[k:] - csum[:-k]
            bi = int(np.argmax(win))
            if win[bi] < 6.0:
                return box                     # too weak to trust
            nx = x0 + bi
            nx = max(0, min(W - w, nx))
            # Reject implausible jumps: a real meter does not teleport between frames.
            if abs(nx - x) > padx:
                return box
            return (int(nx), y, w, h)
        except Exception:
            return box

    def _stamp_fill_estimator(self, mode, identity):
        """Publish a stable, process-monotonic identity for one emitted fill ruler.

        ``mode`` is deliberately small on the wire (``coarse`` or ``subpixel``).
        ``identity`` remains local and captures sub-pixel latch/anchor-family
        changes which can alter the numerical ruler without changing that mode.
        A new generation is issued only when the identity changes, so ordinary
        consecutive samples remain eligible for sub-frame interpolation.
        """
        mode = str(mode or "").strip().lower()
        if mode not in ("coarse", "subpixel"):
            self._last_fill_estimator_mode = ""
            self._last_fill_estimator_generation = 0
            return
        key = (mode, identity)
        if key != self._fill_estimator_identity:
            self._fill_estimator_generation += 1
            self._fill_estimator_identity = key
        self._last_fill_estimator_mode = mode
        self._last_fill_estimator_generation = int(self._fill_estimator_generation)

    def _coarse_fill_ruler_identity(self, denom):
        """Return a jitter-stable identity for the current coarse row-walk scale.

        ``denom`` remains frame-local for the actual fill calculation.  This helper
        only tells native which consecutive samples are safe to interpolate.  The
        detector's measured +/-2 px height quantization stays one identity; a larger
        change (or a shot/lock reset, which clears the reference) starts a new epoch
        before this frame is published, so the crossing pair fails closed.
        """
        d = float(max(1.0, denom))
        ref = self._coarse_denom_ref
        if (ref is None
                or abs(d - float(ref)) > float(self._coarse_denom_jitter_px)):
            self._coarse_denom_ref = d
            self._coarse_denom_epoch += 1
        return ("row_walk", int(self._coarse_denom_epoch),
                float(self._coarse_denom_ref))

    def _predict_subpx_base_abs(self, ts):
        """Predict the meter-base image row across a short measurement dropout.

        The history contains measured base rows only.  A median of pairwise slopes is
        used instead of the old first/last slope so one noisy base localization cannot
        create a large moving-shot extrapolation.  This is intentionally short-lived;
        after ``_subpx_bridge_max_s`` the caller must fall back and mint a new ruler.
        """
        try:
            if ts is None or len(self._subpx_base_hist) < 2:
                return None
            now = float(ts)
            hist = [(float(t), float(b)) for t, b in self._subpx_base_hist
                    if np.isfinite(float(t)) and np.isfinite(float(b))]
            if len(hist) < 2:
                return None
            t_last, b_last = hist[-1]
            age = now - t_last
            if not (0.0 <= age <= float(self._subpx_bridge_max_s)):
                return None
            slopes = []
            for i in range(len(hist) - 1):
                for j in range(i + 1, len(hist)):
                    dt = hist[j][0] - hist[i][0]
                    if dt > 1.0e-3:
                        slopes.append((hist[j][1] - hist[i][1]) / dt)
            if not slopes:
                return None
            vel = float(np.median(np.asarray(slopes, dtype=np.float64)))
            return b_last + vel * age
        except (TypeError, ValueError, OverflowError):
            return None

    def _subpixel_camera_scale(self, base, gfrac, bh, ts, measured):
        """Map current pixels onto a canonical ruler only with independent scale proof.

        A small camera zoom formerly left D latched while stretching the white
        run, creating about 6.7pp of false progress at +10% zoom. A detector size
        proposal alone is not enough: require an accepted current-frame notch,
        a directly measured base, and a green-cap/base span which agrees with
        that proposed scale within two endpoint-rasterization pixels. The span
        never uses the moving fill edge. Canonical D/off and estimator identity
        stay fixed; only the effective pixel transform changes.
        """
        scale = float(getattr(self, '_subpx_camera_scale', 1.0))
        ruler = (self._subpx_D, self._subpx_off)
        ref = getattr(self, '_subpx_camera_ref', None)
        if ref is not None and ref[0] != ruler:
            self._subpx_camera_ref = ref = None
            self._subpx_camera_scale = scale = 1.0
            self._det_scale_reference = None
            self._det_scale_pending = None
        if (ruler[0] is None or ruler[1] is None or self._subpx_provisional):
            self._subpx_camera_ref = None
            self._subpx_camera_scale = 1.0
            self._det_scale_reference = None
            self._det_scale_pending = None
            return 1.0
        if not measured or base is None:
            return scale
        grows = np.flatnonzero(gfrac >= .20)
        if grows.size < 2:
            return scale  # absent/tiny/censored cap cannot establish scale
        span = float(base) - float(grows[0])
        if not (.5 * float(ruler[0]) * scale <= span
                <= 1.25 * float(ruler[0]) * scale):
            return scale
        if ref is None:
            # The completed clean latch defines canonical box pixels. This
            # reference dies on a new lock/press/source or a real ruler relatch.
            # If the cap first becomes visible only AFTER a scale change, there
            # is no measured canonical span to recover; do not invent one.
            if abs(float(bh) - (float(ruler[0]) + 1.0)) > 2.0:
                return scale
            self._subpx_camera_ref = (ruler, span, float(ruler[0]) + 1.0)
            self._remember_det_scale_reference(self._subpx_camera_ref, 1.0, bh, span, ts)
            return 1.0
        self._remember_det_scale_reference(ref, scale, bh, span, ts)
        evidence = getattr(self, '_det_track_scale_match', None)
        if (evidence is None or len(evidence) != 4 or ts is None
                or not np.isfinite(float(ts)) or not np.isfinite(float(evidence[3]))
                or abs(float(ts) - float(evidence[3])) > 1e-6):
            return scale
        resized, _w, height, _when = evidence
        proposed = float(height) / ref[2]
        if (not np.isfinite(proposed) or proposed <= 0.0
                or abs(proposed / scale - 1.0) > min(.35, float(self._scale_step_max)) + 1e-12
                or abs(span - ref[1] * proposed) > 2.0):
            return scale
        pending = self._det_scale_pending
        if (pending is not None and abs(pending[0] - float(ts)) <= 1e-6
                and pending[1:3] == (int(_w), int(height))):
            self._det_scale_pending = (*pending[:4], True)
        # An original-template winner normally means detector breathing. After
        # an established zoom it may also mean a return to the original pixels;
        # the independent span must confirm that reversal as well.
        if not resized and abs(scale - 1.0) < 1e-9:
            return scale
        # Do not turn one row of cap quantization into calibration movement.
        # Constant-scale frames assign an absolute ratio, never multiply it.
        if abs((proposed - scale) * ref[1]) <= 2.0:
            return scale
        self._subpx_camera_scale = proposed
        self._remember_det_scale_reference(ref, proposed, bh, span, ts)
        # Old absolute base positions mix camera motion/scale and may not bridge
        # the first hidden frame correctly. Keep only this measured anchor.
        self._subpx_base_hist = self._subpx_base_hist[-1:]
        # Session seeds are pixel-valued; future locks must not adopt seeds from
        # the old physical scale. The current lock's canonical ruler is retained.
        self._subpx_ruler_hist = []
        return proposed

    def _bridge_subpixel_from_coarse(self, top, bh, y0, ts):
        """Keep one base-anchored ruler through a transient edge-quality failure.

        The coarse walk's first-white row is already parity-aligned with
        ``ysub + _subpx_bias``.  Combining that row with the short-horizon predicted
        physical base therefore loses sub-row edge precision for this frame, but does
        *not* change coordinate systems.  Native can retain its earlier clean samples
        instead of restarting ownership on a coarse/sub-pixel mode toggle.
        """
        try:
            if (not self._subpx_fill or self._subpx_D is None
                    or self._subpx_off is None):
                return None
            base_abs = self._predict_subpx_base_abs(ts)
            if base_abs is None:
                return None
            base = float(base_abs) - float(y0)
            # Reject a prediction that has left the tracked meter box.  A few pixels of
            # tolerance cover ordinary detector quantization without accepting a stale
            # base from another object/lock.
            if not (-3.0 <= base <= float(bh) + 3.0):
                return None
            scale = float(getattr(self, '_subpx_camera_scale', 1.0))
            D = float(self._subpx_D) * scale
            anchor = base + float(self._subpx_off) * scale
            fill_sub = (anchor - float(top)) / D * 100.0
            if not (-3.0 <= fill_sub <= 103.0):
                return None
            fill_sub = min(100.0, max(0.0, fill_sub))
            q = self._dbg_subpx if isinstance(self._dbg_subpx, dict) else {}
            q.update({"ok": 1, "bridge": 1, "anchor": "base_bridge",
                      "base_sub": round(base, 4),
                      "fill_sub": round(fill_sub, 3)})
            self._dbg_subpx = q
            self._last_subpx_transform = (float(anchor), D)
            return round(fill_sub, 3)
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None

    def _record_session_ruler_seed(self, d_shot, off_shot):
        """Append a completed per-shot seed to the session history WITHOUT changing the
        emitted ruler (the lock stays on the ruler it started on)."""
        if not self._subpx_session_ruler:
            return
        hist = self._subpx_ruler_hist
        if hist:
            d_ref = float(np.median([d for d, _ in hist]))
            if abs(float(d_shot) - d_ref) > max(6.0, d_ref * self._fill_denom_relatch):
                hist.clear()
        hist.append((float(d_shot), float(off_shot)))
        while len(hist) > int(self._subpx_session_window):
            hist.pop(0)

    def _adopt_session_ruler(self):
        """[ORION_METER_SUBPIXEL_SESSION_RULER] Fold the per-shot seed that was just latched
        into the session history and, once enough seeds exist, replace the emitted (D, off)
        with the session MEDIAN. See the __init__ note for the measurement behind it.

        Called only at the moment a per-shot seed completes, so the estimator identity
        changes exactly where it changes today (None -> latched); no extra generation churn
        mid-rise. A seed whose D disagrees with the session median by the relatch margin
        means a rescale happened while no ruler was latched: the history restarts from it.
        Flag off, or fewer than the minimum seeds: the per-shot latch stands untouched."""
        if self._subpx_D is None or self._subpx_off is None:
            return
        if not self._subpx_session_ruler:
            self._subpx_ruler_kind = "shot"
            return
        d_shot = float(self._subpx_D)
        off_shot = float(self._subpx_off)
        hist = self._subpx_ruler_hist
        if hist:
            d_ref = float(np.median([d for d, _ in hist]))
            if abs(d_shot - d_ref) > max(6.0, d_ref * self._fill_denom_relatch):
                hist.clear()
        hist.append((d_shot, off_shot))
        while len(hist) > int(self._subpx_session_window):
            hist.pop(0)
        if len(hist) >= int(self._subpx_session_min):
            self._subpx_D = max(1.0, float(np.median([d for d, _ in hist])))
            self._subpx_off = float(np.median([o for _, o in hist]))
            self._subpx_ruler_kind = "session"
        else:
            self._subpx_ruler_kind = "shot"

    def _record_direct_occlusion_fill(self, ts, coarse_fill) -> None:
        """Record one directly visible detector-box edge for a bounded occlusion bridge.

        This history is shot- and lifecycle-generation-scoped.  It contains only
        ordinary/rung measurements; recovered frames never enter it and therefore
        cannot keep a stale prediction alive.  A material fall starts a new run so
        a release/deflate tail cannot be extrapolated as another rise.
        """
        try:
            now = float(ts)
            fill = float(coarse_fill)
            epoch = int(self._physical_shot_epoch or 0)
            generation = int(getattr(self, "_det_lock_generation", 0) or 0)
            current = bool(
                self._det_occ_fill and np.isfinite(now) and np.isfinite(fill)
                and 0.0 < fill <= 100.0 and epoch > 0 and generation > 0
                and self._shot_armed_hw and self._det_lifecycle
                and self._det_state == "locked"
                and self._det_occ_locator_source_positive)
            if not current:
                self._det_occ_direct.clear()
                self._det_occ_key = None
                return
            key = (epoch, generation)
            if key != self._det_occ_key:
                self._det_occ_direct.clear()
                self._det_occ_key = key
            hist = self._det_occ_direct
            if hist:
                prev_t, prev_f = hist[-1]
                if now <= float(prev_t) + 1.0e-9:
                    return
                # Evidence must remain one short visual burst.  Do not combine
                # sparse sightings across a long hold or a hidden shot boundary.
                if now - float(prev_t) > max(0.20, 2.0 * self._det_occ_max_gap_s):
                    hist.clear()
                elif fill < float(prev_f) - 2.0:
                    hist.clear()
            hist.append((now, fill))
            # Only a new directly visible measurement under a positive locator
            # replenishes the one-frame negative bridge budget.
            self._det_occ_negative_bridge_used = False
        except (TypeError, ValueError, OverflowError):
            self._det_occ_direct.clear()
            self._det_occ_key = None

    @staticmethod
    def _bottom_connected_white_columns(white, gap_tol=2, bottom_gap_tol=3):
        """Return ``(column, top, bottom, white_count)`` for base-reaching runs.

        A short vertical gap is bridged for compression noise or a thin foreground
        edge, but a run must still contain real white pixels and terminate within a
        tightly bounded distance of the detector-box floor.  Merely entering the
        lower fifth is not base connection: a jersey/highlight can float 10-20 rows
        above the meter floor and must not become a recovered timing edge.
        """
        out = []
        try:
            bh, bw = white.shape[:2]
            max_bottom_gap = max(0, min(
                int(bottom_gap_tol), max(1, int(round(0.04 * bh)))))
            base_lim = max(0, bh - 1 - max_bottom_gap)
            for c in range(bw):
                rows = np.flatnonzero(white[:, c])
                if rows.size == 0:
                    continue
                # Start at the lowest visible white pixel.  It must reach the
                # actual base tolerance; otherwise this is a floating fragment.
                bottom = int(rows[-1])
                if bottom < base_lim:
                    continue
                top = bottom
                count = 1
                last = bottom
                for rr in rows[-2::-1]:
                    r = int(rr)
                    if last - r - 1 > int(gap_tol):
                        break
                    top = r
                    count += 1
                    last = r
                if count >= 4 and bottom - top + 1 >= 6:
                    out.append((c, top, bottom, count))
        except Exception:
            return []
        return out

    @staticmethod
    def _top_occluder_visual_evidence(box_bgr, predicted_top, observed_top):
        """Require a visible foreground band before treating a low edge as censored.

        Trajectory lag alone is ambiguous: a fully visible meter that plateaus or
        starts falling has exactly the same white mask as a higher fill whose top
        was painted over.  Recovery is therefore allowed only when the would-be
        hidden rows contain a broad appearance change relative to the immediately
        preceding track rows.  This is direct evidence of a crossing foreground
        object; an achromatic/indistinguishable mask remains an honest direct read.
        """
        try:
            image = np.asarray(box_bgr)
            if image.ndim != 3 or image.shape[2] < 3:
                return False
            bh, bw = image.shape[:2]
            ptop = int(predicted_top)
            otop = int(observed_top)
            if not (2 <= ptop < otop <= bh - 1) or otop - ptop < 3 or bw < 8:
                return False
            xpad = max(2, int(round(0.08 * bw)))
            if bw - 2 * xpad < 4:
                return False
            inner = image[:, xpad:bw - xpad, :3].astype(np.int16, copy=False)
            # The foreground may begin a few rows before the trajectory's
            # predicted edge (the prediction is deliberately approximate). Find
            # a broad horizontal appearance edge in a short look-back, then prove
            # that its changed material persists through the purported hidden
            # rows. The observed white-fill boundary is excluded from the search.
            search_lo = max(2, ptop - min(12, max(4, otop - ptop)))
            search_hi = min(ptop + 1, otop - 2)
            for edge in range(search_lo, search_hi + 1):
                before = np.median(inner[edge - 2:edge], axis=0)
                after = np.median(inner[edge:edge + 2], axis=0)
                edge_delta = np.max(np.abs(after - before), axis=1)
                if float(np.mean(edge_delta >= 24.0)) < 0.45:
                    continue
                band = inner[ptop:otop]
                # Max-channel distance retains saturated/dark foreground evidence
                # without letting ordinary codec speckle count as a mask.
                delta = np.max(np.abs(band - before[None, :, :]), axis=2)
                changed = delta >= 24.0
                row_support = changed.mean(axis=1)
                if (np.count_nonzero(row_support >= 0.45) >= 2
                        and float(changed.mean()) >= 0.35):
                    return True
            return False
        except (TypeError, ValueError, OverflowError, IndexError):
            return False

    def _occlusion_rise_model(self, ts):
        """Return the current lock's robust direct-rise model, or ``None``.

        This establishes identity and motion only.  A caller that wants to emit
        an inferred edge must separately require a still-positive/fresh locator
        source and the short recovery deadline.  Keeping those decisions split
        lets a newly negative source classify a truncated edge as censored and
        reject it without ever authorizing a predicted fill.
        """
        try:
            now = float(ts)
            epoch = int(self._physical_shot_epoch or 0)
            generation = int(getattr(self, "_det_lock_generation", 0) or 0)
            key = (epoch, generation)
            if not (
                    self._det_occ_fill and np.isfinite(now)
                    and epoch > 0 and generation > 0
                    and self._shot_armed_hw and self._det_lifecycle
                    and self._det_state == "locked"
                    and self._det_occ_key == key
                    and int(self._gameplay_structure_proof_epoch or 0) == epoch
                    and len(self._det_occ_direct) >= int(self._det_occ_min_direct)):
                return None
            hist = list(self._det_occ_direct)
            last_t, last_fill = float(hist[-1][0]), float(hist[-1][1])
            age = now - last_t
            if not (np.isfinite(last_t) and np.isfinite(last_fill) and age > 0.0):
                return None

            recent = [(float(t), float(f)) for t, f in hist
                      if last_t - float(t) <= 0.25]
            if len(recent) < int(self._det_occ_min_direct):
                return None
            fills = np.asarray([p[1] for p in recent], dtype=np.float64)
            if np.any(np.diff(fills) < -1.5) or float(fills[-1] - fills[0]) < 4.0:
                return None
            slopes = []
            for i, a in enumerate(recent[:-1]):
                for b in recent[i + 1:]:
                    dt = b[0] - a[0]
                    if dt > 1.0e-3:
                        slopes.append((b[1] - a[1]) / dt)
            if not slopes:
                return None
            rate = float(np.median(np.asarray(slopes, dtype=np.float64)))
            max_rate = max(350.0, float(self._press_max_rate_pct_ms) * 1000.0)
            if not (20.0 <= rate <= max_rate):
                return None
            t0, f0 = recent[0]
            residual = np.asarray(
                [f - (f0 + rate * (t - t0)) for t, f in recent],
                dtype=np.float64)
            if float(np.max(np.abs(residual))) > 3.5:
                return None
            predicted = min(100.0, last_fill + rate * age)
            loc_age = now - float(self._det_last_found_ts)
            return now, last_t, last_fill, rate, predicted, age, loc_age
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None

    def _partial_occlusion_fill_edge(self, white, bh, bw, denom, ts):
        """Recover a white fill edge from the unoccluded meter columns.

        The normal detector-box reader intentionally requires a wide row consensus.
        During a side-on limb crossing, however, two thirds of the bar can disappear
        while the remaining columns still carry a clean bottom-connected fill edge.
        This path uses those columns only after the current shot has independently
        established identity and a rising trajectory.  It returns ``(top, run_len,
        support_fraction)`` or ``None`` and cannot acquire, stamp, or extend itself.
        """
        self._det_occ_recovered = False
        self._det_occ_support = 0.0
        self._det_occ_kind = ""
        try:
            if not self._det_occ_fill or ts is None or bw < 8 or bh < 20:
                return None
            model = self._occlusion_rise_model(ts)
            if model is None or not self._det_occ_locator_source_positive:
                return None
            _, _, last_fill, _, predicted, age, loc_age = model
            # The locator must also have corroborated this box recently.  A held
            # negative frame cannot use old trajectory evidence from another scene.
            if not (0.0 < age <= float(self._det_occ_max_gap_s)
                    and 0.0 <= loc_age <= float(self._det_occ_max_gap_s)):
                return None

            # Ignore the detector box's outer two columns (outline/padding), then
            # recover per-column base-connected runs.  The selected 20th-percentile
            # top is definition-compatible with the existing robust colour reader:
            # at least one fifth of surviving columns must agree on the leading edge.
            xpad = max(2, int(round(0.08 * bw)))
            if bw - 2 * xpad < int(self._det_occ_min_cols):
                return None
            cols = self._bottom_connected_white_columns(
                white[:, xpad:bw - xpad], gap_tol=2)
            if len(cols) < int(self._det_occ_min_cols):
                return None
            tops = sorted(int(v[1]) for v in cols)
            qidx = int(np.floor(0.20 * max(0, len(tops) - 1)))
            top = int(tops[qidx])
            supporting = sorted(
                int(v[0]) for v in cols if abs(int(v[1]) - top) <= 6)
            longest = run = 0
            prev = None
            for c in supporting:
                run = run + 1 if prev is not None and c == prev + 1 else 1
                longest = max(longest, run)
                prev = c
            if longest < int(self._det_occ_min_cols):
                return None

            candidate = (float(denom) - float(top)) / float(denom) * 100.0
            # A few rows of compression/occluder edge are expected; a large jump
            # is an object switch or corrupted read and remains an honest miss.
            tol = max(5.0, 6.0 * 100.0 / max(1.0, float(denom)))
            if (candidate < last_fill - 2.0
                    or abs(candidate - predicted) > tol
                    or not (0.0 < candidate <= 100.0)):
                return None
            support_fraction = float(len(supporting)) / float(max(1, bw - 2 * xpad))
            selected = [v for v in cols if abs(int(v[1]) - top) <= 6]
            run_len = int(round(float(np.median(
                [int(v[2]) - int(v[1]) + 1 for v in selected]))))
            self._det_occ_recovered = True
            self._det_occ_support = support_fraction
            self._det_occ_kind = "partial_columns"
            self._det_diag["occlusion_fill"] = int(
                self._det_diag.get("occlusion_fill", 0)) + 1
            return top, max(4, run_len), support_fraction
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None

    def _top_censored_fill_edge(self, white, box_bgr, bh, bw, denom,
                                best_top, best_len, ts):
        """Recover, or explicitly reject, a trajectory-truncated top edge.

        A horizontal foreground strip can hide the true leading edge while
        leaving a wide, base-connected white remainder.  The ordinary row walk
        then returns a perfectly plausible but too-low fill, which would delay a
        release.  When that visible edge materially lags a proven current-shot
        rise, it is a *censored lower bound*, not a new direct measurement.

        A still-positive recent locator may bridge it for at most
        ``_det_occ_max_gap_s``.  One fresh full-frame negative may bridge for at
        most 45 ms only when at least half the inner meter width remains; that
        one-shot budget is replenished solely by a new positive direct read.
        Stale or subsequent negative slots can only cause an explicit rejection.
        ``False`` is the rejection sentinel, ``None`` means an ordinary direct
        edge, and a tuple is the bounded recovered edge.
        """
        try:
            if best_top is None or bw < 8 or bh < 20:
                return None
            model = self._occlusion_rise_model(ts)
            if model is None:
                return None
            _, _, last_fill, _, predicted, age, loc_age = model
            observed = ((float(denom) - float(best_top))
                        / float(denom) * 100.0)
            # Require a multi-row disagreement.  Ordinary row quantization and
            # detector-height jitter stay direct; only a timing-material lag is
            # classified as a hidden leading edge.
            lag_floor = max(4.0, 4.0 * 100.0 / max(1.0, float(denom)))
            if (predicted - observed) < lag_floor:
                return None

            xpad = max(2, int(round(0.08 * bw)))
            inner_w = bw - 2 * xpad
            if inner_w < int(self._det_occ_min_cols):
                return None
            cols = self._bottom_connected_white_columns(
                white[:, xpad:bw - xpad], gap_tol=2)
            # The remaining lower block must be broad and base-connected.  This
            # distinguishes a top censor from the two-column side bridge and from
            # a floating jersey/highlight crossing the held box.
            supporting = sorted(int(v[0]) for v in cols
                                if abs(int(v[1]) - int(best_top)) <= 2)
            longest = run = 0
            prev = None
            for c in supporting:
                run = run + 1 if prev is not None and c == prev + 1 else 1
                longest = max(longest, run)
                prev = c
            min_broad = max(int(self._det_occ_min_cols),
                            int(np.ceil(0.20 * float(inner_w))))
            if longest < min_broad or int(best_len) < 6:
                return None

            support_fraction = float(len(supporting)) / float(max(1, inner_w))
            predicted_top = int(round(float(denom)
                                      * (1.0 - predicted / 100.0)))
            predicted_top = max(0, min(int(bh) - 1, predicted_top))
            if int(best_top) - predicted_top < 3:
                return None
            # A plateau/fall and a top-painted rising fill are identical in the
            # white mask.  Do not infer from trajectory alone: require the current
            # pixels to show a broad foreground band in the purported hidden rows.
            if not self._top_occluder_visual_evidence(
                    box_bgr, predicted_top, int(best_top)):
                return None
            positive_live = bool(
                self._det_occ_locator_source_positive
                and 0.0 < age <= float(self._det_occ_max_gap_s)
                and 0.0 <= loc_age <= float(self._det_occ_max_gap_s))
            min_negative_broad = int(np.ceil(0.50 * float(inner_w)))
            negative_once = bool(
                not positive_live
                and self._det_occ_current_full_negative
                and not self._det_occ_negative_bridge_used
                and longest >= min_negative_broad
                and support_fraction >= 0.50
                and 0.0 < age <= float(self._det_occ_negative_bridge_s)
                and 0.0 <= loc_age <= float(self._det_occ_negative_bridge_s)
                and 0.0 <= float(self._det_occ_current_source_age)
                <= float(self._det_occ_negative_bridge_s))
            if not positive_live and not negative_once:
                # Classification is allowed to fail closed for only the current
                # lock's ordinary coast horizon.  It never emits a fill and the
                # lifecycle still owns the eventual drop/re-acquisition.
                reject_horizon = min(0.40, max(
                    float(self._det_occ_max_gap_s),
                    float(getattr(self, "_det_hold_s", 0.35))))
                if age <= reject_horizon:
                    self._det_occ_censored_reject = True
                    self._det_occ_kind = "top_censored_reject"
                    self._det_diag["occlusion_reject"] = int(
                        self._det_diag.get("occlusion_reject", 0)) + 1
                    return False
                return None

            self._det_occ_recovered = True
            self._det_occ_support = support_fraction
            if negative_once:
                self._det_occ_negative_bridge_used = True
                self._det_occ_kind = "top_censored_negative_once"
                self._det_diag["occlusion_negative"] = int(
                    self._det_diag.get("occlusion_negative", 0)) + 1
            else:
                self._det_occ_kind = "top_censored"
            self._det_diag["occlusion_fill"] = int(
                self._det_diag.get("occlusion_fill", 0)) + 1
            self._det_diag["occlusion_top"] = int(
                self._det_diag.get("occlusion_top", 0)) + 1
            recovered_len = max(4, int(best_len) + int(best_top) - predicted_top)
            return predicted_top, recovered_len, support_fraction
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None

    def _measure_fill_in_box(self, frame, box, ts=None):
        self._det_pixel_ruler_reject = False
        result = self._measure_fill_in_box_legacy(frame, box, ts)
        if self._tracking_meter_style == "pill":
            # [ORION_PILL_RULER 2026-09-19] The Pill proposer box carries ~16px of
            # pedestal+cap padding around the capsule, so its box-relative ruler reads
            # 7.5 + 0.88*true and fires the 20% phase anchor ~30ms early. Re-express the
            # same measured edge against the capsule's OWN base/apex landmarks (see
            # pill_fill_ruler.py). Fail-open: a missing landmark returns `result`
            # unchanged, and no other style can reach this branch.
            try:
                import pill_fill_ruler as _pfr
                result = _pfr.measure(self, frame, box, ts, result)
            except Exception:
                pass
        if not self._det_pixel_ruler:
            return result
        # Physical geometry can only tighten the existing direct-read verdict.
        # Never bypass rung/censor/occlusion checks or promote a recovered edge
        # into independent current pixels for this experimental ruler.
        if (result[2] < 0 or self._det_occ_censored_reject or self._det_occ_recovered):
            self._det_pixel_ruler_reject = True
            self._last_fill_estimator_mode = ""
            self._last_fill_estimator_generation = 0
            self._last_fill_coarse = 0.0
            return 0.0, None, -1
        return self._measure_pixel_ruler(frame, box, ts, direct_top=result[2])

    def _measure_fill_in_box_legacy(self, frame, box, ts=None):
        """Colour-AGNOSTIC white-meter fill + green make-window, measured directly in the
        detector box. Returns (fill_pct, green_tuple_or_None, top_row) on the box 0-100 scale
        (0 = box bottom, 100 = box top).

        WHY NOT _read_fill: _read_fill masks the CONFIGURED bar colour (red rails), so on a 2K27
        WHITE meter it finds nothing and returns 0 unless meter_color is exactly 'White' -- which
        is not guaranteed live (settings drift; the White lock needs a rebuild). Rendered proof:
        the meter was filling to 92% while _read_fill reported 0.00 every frame. A bright-neutral
        row scan reads the fill regardless of the colour setting. The detector box already bounds
        the meter tip-to-base, so a box-relative scale is the fill scale."""
        # Invalid/no-fill measurements carry no timing provenance.  A successful
        # measure below replaces these values before returning.
        self._last_fill_estimator_mode = ""
        self._last_fill_estimator_generation = 0
        self._det_occ_recovered = False
        self._det_occ_support = 0.0
        self._det_occ_kind = ""
        self._det_occ_censored_reject = False
        try:
            x, y, w, h = (int(v) for v in box)
            H, W = frame.shape[:2]
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(W, x + w), min(H, y + h)
            bh = y1 - y0
            if x1 - x0 < 3 or bh < 10:
                return 0.0, None, -1
            box_bgr = frame[y0:y1, x0:x1]
            hsv = cv2.cvtColor(box_bgr, cv2.COLOR_BGR2HSV)
            V = hsv[:, :, 2]; S = hsv[:, :, 1]; Hh = hsv[:, :, 0]
            white = ((V >= 200) & (S <= 65))
            wfrac = white.mean(axis=1)
            # denominator = this frame's box height. A per-shot LATCH here was tried and
            # REFUTED by A/B (see the FILL-DENOMINATOR LATCH note in __init__): latching D
            # re-anchors the numerator to the jittery box TOP edge and measured WORSE. The
            # sub-pixel path carries the surviving idea (latched scale + base anchor).
            denom = float(max(1, bh - 1))
            # The first box after a physical press validates a carried ruler before any
            # sample is published.  Adjacent detector heights in live telemetry vary by
            # at most two pixels; anything beyond that is a real scale change (or an
            # unsafe acquisition), so cold-seed rather than mixing coordinate systems.
            if self._subpx_carry_pending:
                self._subpx_carry_pending = False
                if (self._subpx_D is None or self._subpx_off is None
                        or abs(denom - float(self._subpx_D))
                        > float(self._subpx_carry_jitter_px)):
                    self._subpx_D = None; self._subpx_provisional = False
                    self._subpx_off = None
                    self._subpx_seed = []
                    self._subpx_base_hist = []; self._subpx_last_base_rel = None
            # [ORION_METER_SUBPIXEL_SESSION_PROVISIONAL] a fresh lock with no ruler yet takes
            # the session median now, so the anchor-band frames are on the stable ruler.
            if (self._subpx_D is None and self._subpx_session_ruler
                    and self._subpx_session_provisional
                    and len(self._subpx_ruler_hist) >= int(self._subpx_session_min)):
                _d_s = float(np.median([d for d, _ in self._subpx_ruler_hist]))
                if abs(denom - _d_s) <= float(self._subpx_carry_jitter_px):
                    self._subpx_D = max(1.0, _d_s)
                    self._subpx_off = float(np.median([o for _, o in self._subpx_ruler_hist]))
                    self._subpx_provisional = True
                    self._subpx_seed = []
                    self._subpx_ruler_kind = "session_provisional"
            # largest contiguous run of white rows = the fill block (ignores the meter's
            # non-white rounded base at the very bottom, which broke a naive bottom-anchor).
            best_top = None; best_len = 0; run_top = None
            base_top = None; base_len = 0
            base_region = bh - max(8, int(round(0.20 * bh)))
            for r in range(bh):
                if wfrac[r] >= 0.28:
                    if run_top is None:
                        run_top = r
                else:
                    if run_top is not None:
                        if (r - run_top) > best_len:
                            best_len = r - run_top; best_top = run_top
                        if r - 1 >= base_region and r - run_top >= 3:
                            if r - run_top > base_len:
                                base_top, base_len = run_top, r - run_top
                        run_top = None
            if run_top is not None:
                if (bh - run_top) > best_len:
                    best_len = bh - run_top; best_top = run_top
                if bh - run_top >= 3 and bh - run_top > base_len:
                    base_top, base_len = run_top, bh - run_top
            # A floating jersey/court stripe can be longer than the real low fill.
            # Prefer an independently observed base-reaching solid run over that
            # detached stripe; the rung rescue below still handles divided Pill fill.
            if (base_top is not None and best_top is not None
                    and best_top + best_len - 1 < base_region):
                base_support = white[base_top:base_top + base_len].sum(axis=0)
                coherent = np.count_nonzero(base_support >= .8 * base_len)
                if coherent < int(np.ceil(.28 * (x1 - x0))):
                    # Socks over nameplate text can imitate two stacked runs.
                    # Refuse that ambiguity rather than turning a false 45% lock
                    # into an ownable false 18% onset by selecting the lower text.
                    self._last_fill_coarse = 0.0
                    self._dbg_subpx = dict(ok=0, gate='floating_without_base_ribbon')
                    return 0.0, None, -1
                best_top, best_len = base_top, base_len
            # ---- RUNG-TOLERANT LADDER RESCUE (see __init__ doc; ORION_METER_RUNG_FILL).
            # On the Pill capsule the walk above finds nothing (wfrac ceiling 0.20-0.26
            # vs the 0.28 floor) or an occasional 2-3-row scrap; the ladder block is the
            # real fill. Selection is evidence-ranked, never additive:
            #   * walk found nothing        -> the gated ladder block stands in;
            #   * walk found a SCRAP inside a measured >=3-segment ladder -> the ladder
            #     wins (the scrap is one segment of it);
            #   * walk found a FLOATING run (not reaching the base region) while a
            #     >=2-segment ladder does touch the base -> the ladder wins (kills the
            #     one-frame 66%-fill occluder spike measured on clip 23af440b f642).
            # A solid-ribbon (Arrow2) walk result that touches the base region is NEVER
            # overridden -- the ladder block barely exists there (narrow-band gate).
            self._dbg_rung = None
            _rb = None
            if self._rung_fill:
                _bw = x1 - x0
                _blim = bh - max(8, int(round(0.20 * bh)))
                _rb = self._rung_fill_block(white, bh, _bw)
                if (_rb is None and best_top is not None
                        and (best_top + best_len - 1) < _blim):
                    # The walk's run FLOATS above the base region -- a bright occluder
                    # band / overlay, not fill. Such a band adds its own height to every
                    # column's white fraction, which can defeat the flanking-dark gate
                    # (the flanks look ~as bright as a short fill core). Re-measure the
                    # ladder with the floating run's rows blanked: the occluder cannot
                    # poison the geometry of a bar it is not part of.
                    _wm = white.copy()
                    _wm[best_top:best_top + best_len] = False
                    _rb = self._rung_fill_block(_wm, bh, _bw)
                if _rb is not None:
                    _r_top, _r_len, _r_nseg = _rb
                    _pick = ""
                    if best_top is None:
                        _pick = "no_solid"
                    else:
                        _a_bot = best_top + best_len - 1
                        _ovl = not (_a_bot < _r_top or best_top > (_r_top + _r_len - 1))
                        if (_ovl and _r_nseg >= 3 and _r_len >= 2 * best_len
                                and _r_top < best_top):
                            _pick = "scrap"
                        elif ((not _ovl) and _a_bot < _blim
                                and (_r_nseg >= 2 or best_len <= 2)):
                            # a floating walk run loses to a base-touching ladder when
                            # the ladder shows gap structure OR the run is a 1-2-row
                            # scrap (weaker evidence than a gated >=6-row base block)
                            _pick = "float"
                    if _pick:
                        best_top, best_len = _r_top, _r_len
                        self._dbg_rung = {"why": _pick, "nseg": _r_nseg,
                                          "top": _r_top, "len": _r_len}
            # A cap/jersey fragment is not a measured fill edge merely because
            # a locator supplied this box. The solid/rung selection above must
            # reach the existing meter-base region, or its exact selected top
            # must agree with a gated base-reaching rung block. An unrelated
            # unselected base rung cannot validate a floating stripe above it.
            # This retains the Pill tick that shares the real ladder's top.
            # Keep trusted ruler and width history: rejected pixels do not erase them.
            if (best_top is not None
                    and (_rb is None or _rb[0] != best_top)
                    and best_top + best_len - 1 < base_region):
                self._last_fill_coarse = 0.0
                self._dbg_subpx = dict(ok=0, gate='floating_without_base_ribbon')
                return 0.0, None, -1
            # A slight side-on occlusion can leave several exact meter columns
            # visible while reducing every row below the ordinary 28%-of-box
            # consensus.  Recover only from those bottom-connected columns and
            # only under the current-shot trajectory/lifecycle gates above.
            _direct_edge = best_top is not None
            if best_top is not None:
                _top_occ = self._top_censored_fill_edge(
                    white, box_bgr, bh, x1 - x0, denom,
                    best_top, best_len, ts)
                if _top_occ is False:
                    self._last_fill_coarse = 0.0
                    return 0.0, None, -1
                if _top_occ is not None:
                    best_top, best_len, _ = _top_occ
                    _direct_edge = False
            else:
                _occ = self._partial_occlusion_fill_edge(
                    white, bh, x1 - x0, denom, ts)
                if _occ is not None:
                    best_top, best_len, _ = _occ
            fill = ((denom - float(best_top)) / denom * 100.0) if best_top is not None else 0.0
            # green mask up-front: reused by the sub-pixel edge (green rows are excluded from
            # its empty-plateau estimate so the make-window cap can never bias the fit) and by
            # the green tuple below.
            gmask = ((Hh >= 40) & (Hh <= 85) & (S >= 90) & (V >= 90))
            gfrac = gmask.mean(axis=1)
            # ---- SUB-PIXEL FILL EDGE (see __init__ doc). Always COMPUTED when a coarse edge
            # exists so per-shot latches evolve identically whichever value is emitted (and so
            # a single replay logs both estimators on the same frames); the flag only selects
            # which value is EMITTED. Coarse always ships in fill_coarse/raw_fill_pct.
            self._last_fill_coarse = round(fill, 2)
            _fs = None
            if best_top is not None and not self._det_occ_recovered:
                _fs = self._subpixel_fill_edge(V, gfrac, int(best_top), int(best_len),
                                               bh, y0, y1, ts=ts, box_bgr=box_bgr)
                if _fs is None:
                    _fs = self._bridge_subpixel_from_coarse(
                        int(best_top), bh, y0, ts)
                if _fs is not None and self._subpx_fill:
                    fill = _fs
            else:
                self._dbg_subpx = None
            if best_top is not None:
                if _fs is not None and self._subpx_fill:
                    # Base and base_held share the same latched ruler.  A box
                    # fallback is a different ruler even when D remains latched.
                    _anchor_kind = str((self._dbg_subpx or {}).get("anchor", "box"))
                    _anchor_family = ("base" if _anchor_kind in
                                      ("base", "base_held", "base_bridge", "base_int") else "box")
                    _identity = (
                        _anchor_family,
                        None if self._subpx_D is None else float(self._subpx_D),
                        None if self._subpx_off is None else float(self._subpx_off),
                    )
                    self._stamp_fill_estimator("subpixel", _identity)
                else:
                    self._stamp_fill_estimator(
                        "coarse", self._coarse_fill_ruler_identity(denom))
            # green make-window (the ~96-98% cap): topmost green run, on THE SAME RULER as
            # the fill emitted THIS frame (see [ORION_GREEN_SCALE_UNIFY] below).
            green = None
            # A make window is one compact, aligned component at the capsule tip.
            # Never union its rows/pixel confidence with detached court/player paint.
            gmask = self._connected_meter_cap(gmask, white)
            gfrac = gmask.mean(axis=1)
            gpx = int(gmask.sum())
            if gpx >= 4:      # tiny caps reach here only with independent ribbon support
                # Narrow windows can contain only 2-4 pixels per row in a 26px
                # detector box. Their connected geometry, not box-wide occupancy,
                # proves the cap; retain the two-pixel apex convention below.
                grows = np.flatnonzero(gmask.sum(axis=1) >= 2)
                if grows.size:
                    g_top_row = int(grows[0])
                    g_bot_row = int(grows[-1])
                    # [ORION_GREEN_APEX 2026-08-30] The rendered cap is a TRIANGLE that
                    # narrows toward the meter tip: its top rows carry only 1-4 green px
                    # of the ~24-col box, so the 20%-of-width row gate above clips them
                    # and the emitted band top sat ~1-2pp below the real paint while the
                    # band width read ~2x too narrow. Measured on 2514 plateau frames
                    # across 5 framedump sessions (stateless pixel census, 2026-08-30):
                    # row-gate top median 97.2 vs connected >=2px-paint top 98.1; width
                    # 1.90pp vs 3.77pp. The band top is the aim policy's anchor (the tip
                    # is the invariant point), so it must reflect the paint, not the
                    # gate. Extend the TOP edge only (the census shows the bottom edge is
                    # NOT clipped: row-gate bottom == paint bottom in every session)
                    # along rows that stay green-connected (>=2 px, gap tolerance 1 row),
                    # bounded to 8 rows so a detached fleck can never drag the band up.
                    gcnt = gmask.sum(axis=1)
                    _gap = 0
                    _rr = g_top_row
                    _lim = max(0, g_top_row - 8)
                    while _rr - 1 >= _lim:
                        _rr -= 1
                        if int(gcnt[_rr]) >= 2:
                            g_top_row = _rr
                            _gap = 0
                        else:
                            _gap += 1
                            if _gap > 1:
                                break
                    # [ORION_GREEN_SCALE_UNIFY 2026-08-30] Map the band rows through the
                    # SAME transform that produced this frame's EMITTED fill, so a landing
                    # grade's fill-vs-green comparison is always one ruler:
                    #   * sub-pixel frame -> the base-anchored latched (anchor, D) the
                    #     fill numerator used (box slide/jitter cancels out of the band
                    #     exactly as it cancels out of the fill);
                    #   * coarse frame (gates failed / flag off) -> the box-relative
                    #     (denom, denom) mapping, byte-identical to the shipped one.
                    # Before this, the fill switched rulers per frame (sub-pixel engages
                    # on the rise, fails open to coarse at the plateau where the edge
                    # meets the cap) while the band stayed box-ruled: at the plateau the
                    # two rulers differ by a per-shot latch offset (measured IQR ~±2pp),
                    # which is grade noise between peak_fill and green_start. The engine-
                    # facing FILL scale itself is untouched -- only the band moves onto it.
                    _tr = (self._last_subpx_transform
                           if (self._subpx_fill and _fs is not None) else None)
                    if _tr is not None:
                        _anch, _D = float(_tr[0]), float(_tr[1])
                        g_end = (_anch - float(g_top_row)) / _D * 100.0
                        g_start = (_anch - float(g_bot_row)) / _D * 100.0
                    else:
                        g_end = (denom - float(g_top_row)) / denom * 100.0
                        g_start = (denom - float(g_bot_row)) / denom * 100.0
                    g_end = max(0.0, min(100.0, g_end))
                    g_start = max(0.0, min(100.0, g_start))
                    if g_end < g_start:
                        g_start, g_end = g_end, g_start
                    green = (round(g_start, 2), round(g_end, 2),
                             round(0.5 * (g_start + g_end), 2), round(g_end - g_start, 2),
                             round(min(1.0, gpx / 40.0), 3), gpx)
            if _direct_edge and best_top is not None:
                self._record_direct_occlusion_fill(ts, self._last_fill_coarse)
            return round(fill, 2), green, (int(best_top) if best_top is not None else -1)
        except Exception:
            return 0.0, None, -1

    @staticmethod
    def _connected_meter_cap(gmask, white):
        """Return one observed tip component, never a bounding union of green decor."""
        bh, bw = gmask.shape
        selected = np.zeros_like(gmask, dtype=bool)
        # Estimate the actual ribbon centre from its base region, independent of
        # any floating white stripe higher in the box. Cold/no-fill reads use the
        # box centre but still need the same compact tip geometry.
        base_cols = white[bh - max(8, int(round(.20 * bh))):].sum(axis=0)
        peak = int(base_cols.max()) if base_cols.size else 0
        core = np.flatnonzero(base_cols >= max(2, .5 * peak)) if peak >= 2 else []
        core_left = int(core[0]) if len(core) else max(0, bw // 2 - 2)
        core_right = int(core[-1]) + 1 if len(core) else min(bw, bw // 2 + 3)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(
            gmask.astype(np.uint8), connectivity=8)
        chosen, best_key = 0, None
        for label in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[label])
            if area < 8:
                # Downscaled, strict-colour apex pixels can form a real 2x2 cap.
                # This branch measures GREEN ONLY: it never supplies white fill,
                # acquisition authority, or a fallback box-centre identity.
                # Refuse fragments/decor ambiguity and demand a coherent current
                # bottom ribbon plus a compact, centred apex component.
                if (area < 4 or not (2 <= w <= 4 and 2 <= h <= 4)
                        or area < .5 * w * h or n != 2
                        or y + h > max(10, int(np.ceil(.15 * bh)))
                        or peak < 6):
                    continue
                tiny_core = np.flatnonzero(base_cols >= max(6, .5 * peak))
                if (len(tiny_core) < 5 or len(tiny_core) > .65 * bw
                        or int(tiny_core[-1]) - int(tiny_core[0]) + 1 != len(tiny_core)
                        or abs(x + .5 * w - .5 * (tiny_core[0] + tiny_core[-1] + 1)) > 1.5
                        or min(x + w, int(tiny_core[-1]) + 1)
                           - max(x, int(tiny_core[0])) < 2):
                    continue
                ribbon = white[bh - max(8, int(round(.20 * bh))):, tiny_core]
                solid_rows = np.mean(ribbon, axis=1) >= .8
                run = longest = 0
                for solid in solid_rows:
                    run = run + 1 if solid else 0
                    longest = max(longest, run)
                if longest < 6:
                    continue
            if (w < 2 or h < 2
                    or y > max(8, int(np.ceil(.25 * bh)))
                    or y + h > max(10, int(np.ceil(.30 * bh)))
                    # Filled white can split a real cap into two side fragments.
                    # A fragment still has to overlap the observed ribbon, not
                    # merely touch its edge or sit elsewhere in the crop.
                    or min(x + w, core_right) - max(x, core_left) < 2):
                continue
            # A larger lower component can be a ball/reflection behind the meter.
            # All eligibility gates above remain required. Prefer the highest
            # eligible cap; area only resolves components at the same height.
            key = (y, -area)
            if best_key is None or key < best_key:
                chosen, best_key = label, key
        if chosen:
            selected = labels == chosen
        return selected

    def _rung_fill_block(self, white, bh, bw):
        """Gap-tolerant fill block for LADDER-divided meters (2K27 Pill capsule).
        Returns (top, length, nseg) on box rows, or None when the geometry gates fail
        (caller keeps the solid-ribbon walk's verdict -- fail-closed to shipped).

        MEASURED GEOMETRY (seven 1080p owner clips + 720p INTER_AREA downscales, vs the
        green_window_probe.measure_pill ground truth):
          * glossy fill core = ~6 contiguous columns of the ~30-col detector box @1080p
            (~4 of ~20 @720p), flanked by the capsule's dark shell/track on BOTH sides;
          * fill segments ~11 rows @1080p (~7 @720p) split by 1-3-row dark dividers
            (pitch ~12.7 / ~8.4 rows);
          * the EMPTY track carries 1-row bright rung ticks at the same pitch @1080p
            (they alias away at 720p), so per-row brightness alone cannot separate
            filled from empty -- vertical run STRUCTURE can.
        GATES, each one measured:
          * mass: >=4 white rows in the best column and >=12 white px in the box
            (rejects speck noise; the smallest accepted real onset is ~6 rows);
          * narrow core band, <=45% of box width (Arrow2's solid ribbon spans ~50%
            with colw ~0.42-0.45 across it, so a ribbon can never qualify);
          * >=2 mostly-dark columns flanking the band INSIDE the box (a neighbouring
            meter poking into the box edge fails -- that is the off-box Arrow2 case
            that must keep its measured honest-zero at >=12px box error: without this
            gate 5.6% of censused Arrow2 frames flipped, with it 0.03%);
          * the block must touch the base region (bottom ~20% of the box): fill grows
            from the capsule base, decor blobs float;
          * gap tolerance max(2, 0.025*bh) rows = 4 @1080p / 3 @720p sits between the
            divider width (1-3) and the tick spacing (~8-12), so dividers bridge and
            the empty track's ticks never chain into the block;
          * block >=6 rows (the 5-row speck on session_20260829_123758 idx 2932 -- a
            pan-lagged box beside the meter -- fails this; real onset blocks pass)."""
        try:
            colw = white.mean(axis=0)
            cmax = float(colw.max()) if colw.size else 0.0
            if cmax * bh < 4.0 or int(white.sum()) < 12:
                return None
            core = colw >= max(0.5 * cmax, 2.0 / bh)
            bc0 = bc1 = None; c0 = None; bl = 0
            for c in range(bw + 1):
                on = (c < bw) and bool(core[c])
                if on and c0 is None:
                    c0 = c
                elif not on and c0 is not None:
                    if c - c0 > bl:
                        bl = c - c0; bc0, bc1 = c0, c - 1
                    c0 = None
            if bc0 is None or bl < 3 or bl > 0.45 * bw:
                return None
            if bc0 < 2 or bc1 > bw - 3:
                return None
            if (float(colw[bc0 - 2:bc0].mean()) >= 0.25 * cmax
                    or float(colw[bc1 + 1:bc1 + 3].mean()) >= 0.25 * cmax):
                return None
            bfr = white[:, bc0:bc1 + 1].mean(axis=1)
            runs = []
            rt = None
            for r in range(bh):
                if bfr[r] >= 0.5:
                    if rt is None:
                        rt = r
                else:
                    if rt is not None:
                        runs.append((rt, r - 1)); rt = None
            if rt is not None:
                runs.append((rt, bh - 1))
            if not runs:
                return None
            gap_tol = max(2, int(round(0.025 * bh)))
            base_lim = bh - max(8, int(round(0.20 * bh)))
            i = -1
            for j in range(len(runs) - 1, -1, -1):
                if runs[j][1] >= base_lim:
                    i = j
                    break
            if i < 0:
                return None
            s, e = runs[i]
            nseg = 1
            j = i - 1
            while j >= 0 and (s - runs[j][1] - 1) <= gap_tol:
                s = runs[j][0]; nseg += 1; j -= 1
            if (e - s + 1) < 6:
                return None
            return int(s), int(e - s + 1), int(nseg)
        except Exception:
            return None

    def _notch_fallback(self, q, notch, top, bh, y0, y1, ts):
        """[ORION_METER_SUBPIXEL_NOTCH] Edge gates failed on this frame but the notch was
        measured: place the INTEGER coarse edge on the latched notch ruler (anchor 'base_int',
        'base' identity family). <=0.5 px of edge error on a stable ruler beats a flip to the
        coarse box ruler (a different origin AND denominator, plus a generation bump). Only when
        (D, off) are latched; never seeds or relatches (an integer edge must not teach the ruler)."""
        try:
            if (notch is None or not self._subpx_fill or self._subpx_D is None
                    or self._subpx_off is None):
                return None
            if ts is not None:
                # the notch is a clean anchor measurement: keep the hold history warm
                self._subpx_base_hist.append((float(ts), float(y0 + notch)))
                if len(self._subpx_base_hist) > 6:
                    self._subpx_base_hist.pop(0)
                self._subpx_last_base_rel = float(notch) - float(bh - 1)
                self._subpx_last_base_ts = float(ts)
            scale = float(getattr(self, '_subpx_camera_scale', 1.0))
            D = float(self._subpx_D) * scale
            anchor = float(notch) + float(self._subpx_off) * scale
            fill_sub = (anchor - float(top)) / D * 100.0
            if not (-3.0 <= fill_sub <= 103.0):
                return None
            fill_sub = min(100.0, max(0.0, fill_sub))
            q.update({"ok": 1, "anchor": "base_int", "anchor_src": "notch",
                      "base_sub": round(float(notch), 4), "fill_sub": round(fill_sub, 3),
                      "ruler": self._subpx_ruler_kind if self._subpx_D is not None else "",
                      "ruler_n": len(self._subpx_ruler_hist)})
            self._last_subpx_transform = (float(anchor), D)
            return round(fill_sub, 3)
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None

    @staticmethod
    def _subpixel_notch_anchor(Wn, core, top, bh):
        """[ORION_METER_SUBPIXEL_NOTCH] Sub-pixel row (box-relative) of the chevron-notch apex,
        or None. `Wn` is the box crop's whiteness image, `core` the bar-core column mask, `top`
        the coarse fill-edge row. Fails closed: nothing is inferred from the box."""
        try:
            cols = np.flatnonzero(core)
            if cols.size < 2:
                return None
            cx = 0.5 * (float(cols[0]) + float(cols[-1]))       # ribbon centre (pixel centres)
            # the two columns straddling the centre (cx=11.5 -> 11,12; cx=11.0 -> 10,11)
            n_lo = max(0, min(int(Wn.shape[1]) - 2, int(np.floor(cx + 0.5)) - 1))
            nprof = Wn[:, n_lo:n_lo + 2].astype(np.float64).mean(axis=1)
            nrow = int(nprof.shape[0])
            r = top + 1
            while r < bh and nprof[r] >= 200.0:
                r += 1
            if r - (top + 1) < 3 or r >= bh:
                return None                                       # no white run / runs off the box
            nrb = r                                               # first non-white centre row
            b_lo = max(top, nrb - 5)
            Bn = float(np.median(nprof[b_lo:nrb - 1])) if nrb - 1 - b_lo >= 2 else float(nprof[nrb - 1])
            if Bn < 200.0 or nrb + 2 >= nrow:
                return None
            lvl = 0.8 * Bn
            notch = None
            for rr in range(max(top + 1, nrb - 2), min(nrow - 2, nrb + 3)):
                if nprof[rr] < lvl <= nprof[rr - 1] and nprof[rr + 1] < lvl:
                    notch = (rr - 1) + (nprof[rr - 1] - lvl) / max(1e-6, nprof[rr - 1] - nprof[rr])
                    break
            if notch is None:
                return None
            # the run must end INSIDE the box (a fill that runs off the box bottom is clipped or
            # occluded, not a notch) and within the meter's foot zone: the box bottom is detector
            # regression (6.5-8 rows below the notch on n3, 14 on n4), so the zone is loose
            # (2..24 rows) -- it only refuses a white run cut mid-meter by an occluder
            if not ((bh - 24.0) <= notch <= (bh - 2.0)):
                return None
            return float(notch)
        except Exception:
            return None

    def _subpixel_fill_edge(self, V, gfrac, top, run_len, bh, y0, y1, ts=None, box_bgr=None):
        """Sub-pixel fill measurement inside the detector box. Returns the fill percent on
        the SAME 0-100 box-relative scale as the coarse walk (parity via _subpx_bias), or
        None when quality gates fail (caller keeps the coarse value -- fail-open).

        Method (constants are MEASURED on session_20260828_201813 idx 775/776/786 profiles
        and validated by the synthetic sweep in the report):
          1. BAR-CORE columns only: columns that are white in the rows just BELOW the edge.
             The detector box also spans the meter's outline/shadow columns (measured wfrac
             saturates at 0.50), and a mean over those halves the contrast; the core mask
             is derived strictly below the already-found edge so it cannot bias the edge.
          2. 1-D LUMA profile: mean of V over core columns per row. V only -- capture chroma
             is 4:2:0 (vertically subsampled), the axis being localized.
          3. Two plateaus per frame: empty track A (median over rows above the edge, green
             cap rows excluded) and fill core B (median below). 50% point between them, NOT
             a fixed threshold, so brightness/gamma drift and whatever sits above the edge
             only matter through contrast.
          4. Edge = the rising 50% crossing nearest the coarse edge, linear interpolation.
          5. BASE anchor: falling 0.8*B crossing at the bottom of the white run = the
             meter's own base furniture edge (see inline WHY). fill numerator = base - edge
             (both pure image measurements): per-frame box-edge jitter (the measured
             dominant sigma_y term) cancels; the box only sets per-shot latched constants.
             When one frame's base measurement fails, the anchor is HELD by a short
             constant-velocity extrapolation of recent base positions (<=120ms, the meter
             translates smoothly with the shooter), so mid-run gaps do not fall back to
             the jittery box anchor; the hold is dropped across longer gaps.
             _subpx_D / _subpx_off (median of the first 3 clean frames; re-latched on a
             >15% height jump = a genuine rescale, never on jitter).
          6. Quality gates, all fail-open to coarse: >=5 core columns, contrast >= 40 luma
             (measured real contrast ~117), edge width (25->75%) <= 3.0 rows, crossing
             within +/-3 rows of the coarse edge, base >= 4 rows below the edge with a dark
             tail. The gate verdict is stashed in _dbg_subpx as the per-frame
             quality/outlier signal."""
        try:
            q = dict(ok=0, top_sub=-1.0, base_sub=-1.0, contrast=-1.0, width=-1.0,
                     ncore=0, anchor="", fill_sub=-1.0, gate="")
            self._dbg_subpx = q
            self._last_subpx_transform = None    # set only on a successful measure below
            bw = int(V.shape[1])
            if bw < 10 or run_len < 4 or top + 4 > bh:
                q["gate"] = "geom"
                return None
            # [ORION_METER_SUBPIXEL_NOTCH] whiteness = min(B,G,R). The opaque fill is >=237 in
            # every channel; the semi-transparent track takes the background's colour, so over
            # the red paint V reads 228 (no contrast) while whiteness reads 27.
            _use_notch = bool(self._subpx_notch) and box_bgr is not None
            Wn = box_bgr.min(axis=2) if _use_notch else None
            Lum = Wn if _use_notch else V
            # 1. bar-core columns from rows strictly below the edge
            c_lo = min(bh - 1, top + 2)
            c_hi = min(bh, top + 12, top + run_len)
            if c_hi - c_lo < 2:
                q["gate"] = "core_rows"
                return None
            core = (Lum[c_lo:c_hi] >= 200).mean(axis=0) >= 0.6
            core[0] = False
            core[-1] = False
            ncore = int(core.sum())
            q["ncore"] = ncore
            if ncore < 5:
                q["gate"] = "ncore"
                return None
            prof = Lum[:, core].mean(axis=1)   # float64 whiteness (or luma) profile
            # [ORION_METER_SUBPIXEL_NOTCH] a notch measured on THIS frame lets an edge-gate
            # failure below fall back to the integer edge on the same ruler instead of flipping
            # to the coarse box ruler (see _notch_fallback).
            _notch_now = self._subpixel_notch_anchor(Wn, core, top, bh) if _use_notch else None
            # 3. plateaus. On whiteness the green cap is DARK (min(B,G,R) of green paint), so
            # green rows are a valid empty plateau; on V they read ~200 and must stay excluded.
            _a_all = list(range(max(0, top - 6), top - 1))
            a_rows = _a_all if _use_notch else [r for r in _a_all if gfrac[r] < 0.20]
            _cap_zone = bool(_a_all) and (sum(1 for r in _a_all if gfrac[r] >= 0.20) >= 0.5 * len(_a_all))
            if len(a_rows) < 2:
                q["gate"] = "a_rows"
                return self._notch_fallback(q, _notch_now, top, bh, y0, y1, ts) if _use_notch else None
            A = float(np.median(prof[a_rows]))
            b_lo, b_hi = top + 2, min(bh, top + 5, top + run_len)
            if b_hi - b_lo < 2:
                q["gate"] = "b_rows"
                return None
            B = float(np.median(prof[b_lo:b_hi]))
            contrast = B - A
            q["contrast"] = round(contrast, 1)
            if contrast < 40.0:
                q["gate"] = "contrast"
                return self._notch_fallback(q, _notch_now, top, bh, y0, y1, ts) if _use_notch else None
            mid = 0.5 * (A + B)

            def _rise_cross(level):
                for r in range(max(1, top - 3), min(bh - 1, top + 2) + 1):
                    if prof[r - 1] < level <= prof[r]:
                        return (r - 1) + (level - prof[r - 1]) / max(1e-6, prof[r] - prof[r - 1])
                return None
            # 4. sub-pixel edge + width quality
            ysub = _rise_cross(mid)
            if ysub is None:
                q["gate"] = "no_cross"
                return self._notch_fallback(q, _notch_now, top, bh, y0, y1, ts) if _use_notch else None
            w25 = _rise_cross(A + 0.25 * contrast)
            w75 = _rise_cross(A + 0.75 * contrast)
            width = (w75 - w25) if (w25 is not None and w75 is not None) else -1.0
            q["width"] = round(width, 3)
            # inside the green cap the green->white boundary is chroma-smeared (4:2:0) over
            # ~3-4 rows; the crossing stays unbiased, only the sharpness test needs room
            _w_max = 4.5 if (_use_notch and _cap_zone) else 3.0
            if not (0.0 < width <= _w_max):
                q["gate"] = "width"
                return self._notch_fallback(q, _notch_now, top, bh, y0, y1, ts) if _use_notch else None
            eb = ysub + self._subpx_bias      # parity with the coarse first-white-row scale
            if abs(eb - float(top)) > 2.0:    # sub-pixel must refine the edge, not move it
                q["gate"] = "edge_far"
                return self._notch_fallback(q, _notch_now, top, bh, y0, y1, ts) if _use_notch else None
            q["top_sub"] = round(ysub, 4)
            # 5. base edge: FIRST falling crossing of 0.8x the bar's own local brightness at
            # the bottom of the white run. ONE-SIDED relative threshold on purpose: the
            # falloff below the bar steps 255 -> shelf(~170, meter furniture) -> court, and
            # the court side varies with gameplay, so a two-plateau mid there is unstable
            # (first attempt borrowed the FILL edge's mid level and was measured unstable:
            # base wandered ~1-4px on low-contrast frames, e.g. sub_201813 idx 1593-1596/
            # 1733). 0.8*B sits mid-falloff (255 -> ~204) with BOTH sides meter furniture,
            # decoupled from the court and from the fill edge's plateaus.
            base = None
            if _use_notch:
                # [ORION_METER_SUBPIXEL_NOTCH] chevron-notch apex on the 2 centre columns of the
                # white ribbon: walk the CONTIGUOUS white run down from the edge (the ^ outline
                # stroke 2-3 rows inside the notch is itself >=200, so a mask-based "last white
                # row" would land on the stroke), then the falling 0.8*B crossing, one sustained
                # row (the notch interior is ~2 rows deep before the stroke bounces the profile).
                # Accepted only in the chevron zone, 2..12 rows above the box bottom (measured
                # 6.5-8 rows). The core mean smeared this edge because the ^ arms extend lower
                # on the outer columns; two centre columns see the apex itself.
                base = _notch_now
                if base is not None and (base - ysub) < 4.0:
                    base = None
                q["anchor_src"] = "notch" if base is not None else "none"
            elif run_len >= 6:
                rb = top + run_len                    # coarse run bottom (first non-white row)
                s_lo = max(top + 4, rb - 6)
                s_hi = min(bh - 2, rb + 3)
                b_loc_lo = max(top + 2, s_lo - 5)
                if s_hi - s_lo >= 2:
                    B_loc = (float(np.median(prof[b_loc_lo:s_lo]))
                             if s_lo - b_loc_lo >= 2 else B)
                    lvl = 0.8 * B_loc
                    if B_loc >= 200.0:
                        for r in range(s_lo + 1, s_hi + 1):
                            if prof[r] < lvl <= prof[r - 1]:
                                # sustained falloff, not a one-row specular dip
                                if prof[min(bh - 1, r + 2)] < lvl:
                                    base = ((r - 1) + (prof[r - 1] - lvl)
                                            / max(1e-6, prof[r - 1] - prof[r]))
                                break
                if base is not None and (base - ysub) < 4.0:
                    base = None
            held = False
            if base is not None:
                if ts is not None:
                    self._subpx_base_hist.append((float(ts), float(y0 + base)))
                    if len(self._subpx_base_hist) > 6:
                        self._subpx_base_hist.pop(0)
                    self._subpx_last_base_rel = float(base) - float(bh - 1)
                    self._subpx_last_base_ts = float(ts)
            elif ts is not None and len(self._subpx_base_hist) >= 2:
                # BASE HOLD: extrapolate the anchor along its own recent velocity across a
                # short measurement gap (measured anchor sd 0.4-0.5px vs box-edge 0.74px,
                # so a briefly-held anchor still beats falling back to the box).
                _pred_base_abs = self._predict_subpx_base_abs(ts)
                if _pred_base_abs is not None:
                    base = float(_pred_base_abs) - y0
                    held = True
            if (base is None and self._subpx_base_hold and ts is not None
                    and self._subpx_last_base_rel is not None
                    and 0.0 <= (float(ts) - self._subpx_last_base_ts) <= self._subpx_base_hold_max_s):
                # BOX-RELATIVE BASE HOLD (see __init__): the box tracks the meter, so the last
                # measured base-to-box offset of THIS lock places the base on this frame's box.
                # Same ruler family as a measured base -- no estimator step for native.
                base = float(bh - 1) + float(self._subpx_last_base_rel)
                held = True
                q["hold"] = "box_rel"
            if base is not None:
                q["base_sub"] = round(base, 4)
                camera_scale = self._subpixel_camera_scale(
                    base, gfrac, bh, ts, measured=not held)
                # per-shot latch seeding / relatch (state advances on every clean MEASURED
                # frame, independent of which value is emitted; held frames never latch)
                off_now = float(y1 - 1 - (y0 + base))
                if held:
                    pass
                elif (self._subpx_seed is not None
                      and (self._subpx_D is None or self._subpx_provisional)):
                    # a provisional lock keeps seeding under the session ruler; its own seed
                    # still joins the history and re-adopts the median when complete
                    self._subpx_seed.append((float(bh), off_now))
                    if len(self._subpx_seed) >= 3:
                        _seed_D = max(1.0, float(np.median(
                            [b for b, _ in self._subpx_seed])) - 1.0)
                        _seed_off = float(np.median(
                            [o for _, o in self._subpx_seed]))
                        self._subpx_seed = []
                        if self._subpx_provisional and self._subpx_ruler_lock:
                            # [ORION_METER_SUBPIXEL_RULER_LOCK] keep the provisional session
                            # ruler for this shot; the seed only feeds the history (next shot).
                            self._subpx_provisional = False
                            self._record_session_ruler_seed(_seed_D, _seed_off)
                        else:
                            self._subpx_D = _seed_D
                            self._subpx_off = _seed_off
                            self._subpx_provisional = False
                            # [ORION_METER_SUBPIXEL_SESSION_RULER] the completed per-shot seed
                            # joins the session history; the emitted ruler becomes its median.
                            self._adopt_session_ruler()
                elif self._subpx_D is not None and abs((bh - 1.0) / camera_scale - self._subpx_D) > max(
                        6.0, self._subpx_D * self._fill_denom_relatch):
                    # genuine rescale (camera distance changed): re-seed, never jitter-track
                    self._subpx_D = None; self._subpx_provisional = False
                    self._subpx_off = None
                    self._subpx_seed = [(float(bh), off_now)]
                    self._subpx_base_hist = self._subpx_base_hist[-1:]
                    self._subpx_carry_pending = False
                    self._subpx_ruler_hist = []     # a rescale is a new session ruler too
                    self._subpx_ruler_kind = ""
            camera_scale = self._subpixel_camera_scale(
                base, gfrac, bh, ts, measured=base is not None and not held)
            D = (self._subpx_D * camera_scale if self._subpx_D is not None
                 else float(max(1, bh - 1)))
            if base is not None and (self._subpx_off is not None):
                _anch = base + self._subpx_off * camera_scale
                fill_sub = (_anch - eb) / D * 100.0
                q["anchor"] = "base_held" if held else "base"
            elif base is not None:
                # latch still seeding: base-anchored with this frame's own alignment
                _anch = base + (y1 - 1 - (y0 + base))
                fill_sub = (_anch - eb) / D * 100.0
                q["anchor"] = "box0"
            else:
                _anch = bh - 1.0
                fill_sub = (_anch - eb) / D * 100.0
                q["anchor"] = "box"
            fill_sub = min(100.0, max(0.0, fill_sub))
            q["ok"] = 1
            q["fill_sub"] = round(fill_sub, 3)
            q["ruler"] = self._subpx_ruler_kind if self._subpx_D is not None else ""
            q["ruler_n"] = len(self._subpx_ruler_hist)
            q["camera_scale"] = round(camera_scale, 6)
            # [ORION_GREEN_SCALE_UNIFY] The exact (anchor, D) this frame's emitted fill
            # was computed with, so the green make-window can be mapped onto the SAME
            # ruler (see _measure_fill_in_box). Valid only for the frame this call
            # measured -- the caller pairs it with this call's own return value.
            self._last_subpx_transform = (float(_anch), float(D))
            return round(fill_sub, 3)
        except Exception:
            return None

    def _redmask(self, bgr, bounds=None, hsv=None):
        """The BAR mask (named `_redmask` for history; it masks the CONFIGURED bar colour)."""
        row = self._resolve_red_band(bounds)
        if row[0] == "bgr":
            return cv2.inRange(bgr, np.array(row[1], np.uint8), np.array(row[2], np.uint8))
        return self._hsv_band_mask(bgr, row, hsv=hsv)

    def _greenmask(self, hsv, bounds=None):
        lo, hi = bounds if bounds is not None else (
            self._lock_green if self._lock_green is not None else (self._G[0], self._G[1]))
        return cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))

    # -- STAGE 2a: full BGR-colour->contour->size/aspect gate scan (acquire / re-acquire) --
    def _scan(self, frame, region, hmin, ar_min=None, bounds=None, w_max_extra=0,
              red_frac_min=None):
        if ar_min is None:
            ar_min = self.AR_MIN
        if red_frac_min is None:
            red_frac_min = self.params.red_frac_min
        # B2 scale rebase: armed acquire uses the SEED-UNION-MEASURED gates once a session
        # rebase has landed (cold acquire keeps the measured ratios; pre-rebase = seed).
        w_min, w_max, h_max = self._w_min, self._w_max, self._h_max
        if self._robust and self._dims_rebased:
            if self._armed_now_cached:
                w_min = min(w_min, self._meas_w_min)
                w_max = max(w_max, self._meas_w_max)
                h_max = max(h_max, self._meas_h_max)
            else:
                w_min, w_max, h_max = self._meas_w_min, self._meas_w_max, self._meas_h_max
        # CAMERA-ANGLE scale adaptation (identity unless a confident off-nominal lock has been
        # measured): widen the size gates + relax the aspect floor around the rolling estimate.
        w_min, w_max, h_max, hmin, ar_min = self._scale_gates(w_min, w_max, h_max, hmin, ar_min)
        # A strict, physically-armed court-wide acquire may bootstrap a distant half-court meter
        # below the nominal width floor.  Once seated, its relocate window is already position-
        # bounded; preserve that proven lock's measured scale instead of dropping it on frame two.
        # `_courtwide_lock` is false for every cold/menu/nominal scan, so this cannot widen their
        # acceptance surface.
        court_scale = self._courtwide_track_scale()
        if court_scale < 1.0:
            w_min = min(w_min, max(2, int(round(self._lock_w_ref * 0.70))))
            hmin = min(hmin, max(3, int(round(hmin * court_scale))))
        w_max += int(w_max_extra)
        x0, y0, x1, y1 = [int(v) for v in region]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.W, x1), min(self.H, y1)
        if x1 - x0 < w_min or y1 - y0 < hmin:
            return None, 0.0
        sub = frame[y0:y1, x0:x1]
        if self._perf:
            # S2 (producer): memoize the raw mask under an EXACT (region, resolved-bounds)
            # key so the cold-miss path's byte-identical duplicate inRange
            # (_acquire_structure, same band + same default bounds) is computed once.
            # Key on the RESOLVED TAGGED ROW, not on (lo, hi): two different colours can
            # otherwise collide on a key and hand back the wrong colour's mask.
            eff = self._resolve_red_band(bounds)
            key = (x0, y0, x1, y1, eff)
            if key == self._scan_raw_key:
                raw = self._scan_raw
                self._perf_mask_reuses += 1
            else:
                raw = self._redmask(sub, bounds=eff)
                self._scan_raw_key, self._scan_raw = key, raw
            # S1: EXACT empty/near-empty early-exit BEFORE morphology + contours. A passing
            # contour needs countNonZero(mask[bbox]) >= red_frac_min*w*h >= red_frac_min*
            # w_min*hmin (both gate floors), and mask = close(raw) is a subset of
            # dilate(raw) whose count is <= 15*countNonZero(raw) (5x3 all-ones kernel) --
            # so below this floor NO contour can pass the red_frac gate and (None, 0.0)
            # is exactly what the full morphology+contour path would return.
            if cv2.countNonZero(raw) * 15 < red_frac_min * w_min * hmin:
                self._perf_early_exits += 1
                return None, 0.0
        else:
            raw = self._redmask(sub, bounds=bounds)
        mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best, best_score = None, float("-inf")
        # proximity prior ONLY when a prior lock exists (rescan); on a COLD acquire there is no
        # meaningful reference -> a distance penalty would wrongly reject off-centre (slid) meters.
        # FORWARD-PREDICT the reference by the box velocity so the prior points where the meter is
        # HEADING, not where it WAS -- during fast motion the un-predicted (trailing) reference
        # pulled the pick backward off the moving meter. Its weight also eases with speed. bvx=0
        # (a stationary/pristine meter) -> reference == box centre, weight == 0.15: byte-identical.
        cx_ref = (self.box[0] + self.box[2] * 0.5 + self._bvx) if self.box else None
        prox_w = 0.15 * max(0.2, 1.0 - abs(self._bvx) / 40.0)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (w_min <= w <= w_max) or not (hmin <= h <= h_max):
                continue
            if h / float(max(1, w)) < ar_min:
                continue
            red_frac = cv2.countNonZero(mask[y:y + h, x:x + w]) / float(max(1, w * h))
            if red_frac < red_frac_min:
                continue
            prox = -abs((x0 + x + w * 0.5) - cx_ref) if cx_ref is not None else 0.0
            score = h + red_frac * 20 + prox_w * prox
            if score > best_score:
                best_score, best = score, (x0 + x, y0 + y, w, h)
        if best is None:
            return None, 0.0
        return best, self.CONF_INIT      # a clean gated frame is trusted at full confidence (no warmup)

    # -- STAGE 2b: GREEN-TIP anchor. Find the make-window green chevron (the meter's FIXED STRUCTURE,
    #    present at EVERY fill level) in a region. Returns (x,y,w,h) or None. This is what makes the
    #    lock STICK through cap/deflate: when the red fill turns green (cap) or recedes (deflate) the
    #    red column scan fails, but the green tip is still there -> the box stays glued to the meter. --
    def _find_green_tip(self, frame, region, sv_floor=None):
        x0, y0, x1, y1 = [int(v) for v in region]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.W, x1), min(self.H, y1)
        if x1 - x0 < self._gw_min or y1 - y0 < self._gh_min:
            return None
        sub = frame[y0:y1, x0:x1]
        gb = None
        if sv_floor is not None:
            # B2 blur adaptation (locked relocate window ONLY): relax the S/V floors with the
            # measured horizontal speed so a motion-smeared tip still anchors the lock.
            base_lo, base_hi = (self._lock_green if self._lock_green is not None
                                else (self._G[0], self._G[1]))
            gb = ((base_lo[0], min(base_lo[1], int(sv_floor)), min(base_lo[2], int(sv_floor))),
                  base_hi)
        gm = cv2.morphologyEx(self._greenmask(cv2.cvtColor(sub, cv2.COLOR_BGR2HSV), bounds=gb),
                              cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(gm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # forward-predicted proximity reference (see _scan): bvx=0 -> box centre (unchanged).
        cx_ref = (self.box[0] + self.box[2] * 0.5 + self._bvx) if self.box else None
        g_area_min = self._g_area_min
        if self._robust and self._dims_rebased and self._meas_g_area is not None:
            g_area_min = (min(g_area_min, self._meas_g_area) if self._armed_now_cached
                          else self._meas_g_area)
        # CAMERA-ANGLE scale adaptation of the green-tip gates (identity when inactive).
        gw_min, gw_max, gh_min, gh_max, g_area_min = self._scale_green_gates(
            self._gw_min, self._gw_max, self._gh_min, self._gh_max, g_area_min)
        # Same bootstrap rule as the red relocate gate above.  The green search remains inside
        # the small lock-centred ROI and the component still has to satisfy colour, area and size.
        court_scale = self._courtwide_track_scale()
        if court_scale < 1.0:
            gw_min = min(gw_min, max(2, int(round(self._lock_w_ref * 0.65))))
            gh_min = min(gh_min, max(2, int(round(gh_min * court_scale))))
            g_area_min = min(
                g_area_min,
                max(4, int(round(g_area_min * court_scale * court_scale))),
            )
        best, best_score = None, float("-inf")
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (gw_min <= w <= gw_max) or not (gh_min <= h <= gh_max):
                continue
            area = cv2.countNonZero(gm[y:y + h, x:x + w])
            if area < g_area_min:
                continue
            prox = -abs((x0 + x + w * 0.5) - cx_ref) if cx_ref is not None else 0.0
            score = area + 0.5 * prox
            if score > best_score:
                best_score, best = score, (x0 + x, y0 + y, w, h)
        return best

    # -- STAGE 2a' (FADE POP-IN acquire): structure-corroborated SHORT-STUB scan. On a fade shot
    #    the meter pops in fully-formed only ~60ms before release with a short red stub the normal
    #    height/aspect gates reject (h16-38, ar 0.5-1.6) -- by the time h>=33 AND ar>=1.8 hold, the
    #    release clock has already fired blind (live session_20260706_203024, seq 12/13). Accept a
    #    short stub ONLY when the meter's green TIP DOT sits at the track-height offset above the
    #    stub floor, centred on it: two saturated colours in a fixed geometric relation = the
    #    self-contained decor guard (works COLD -- live the shot-gate arms too late on fades). --
    def _acquire_structure(self, frame, region=None, strict_tip=False,
                           red_bounds=None, green_bounds=None,
                           red_mask=None, green_mask=None,
                           strict_scale=None, stub_h_min=None):
        """Acquire a red stub only when its green tip is in the meter relation.

        ``region=None`` is the long-standing nominal-band pop-in path.  The only
        caller that passes a full-frame region is the physically-armed fallback;
        that caller also sets ``strict_tip=True`` so scattered/unrelated green
        pixels cannot corroborate court decor.

        ``strict_tip`` used to select TWO independent things at once: the strict
        connected-component tip proof, and the half-court SCALE FLOORS (0.45x width and
        stub height, plus the perspective ``local_scale``).  ``strict_scale`` splits them
        so a caller can ask for the STRONGER tip proof while keeping the NOMINAL scale
        gates; ``stub_h_min`` overrides only the stub-height floor.  Both default to the
        historical coupling (``strict_scale=strict_tip``), so every shipped call site --
        the nominal pop-in and the court-wide fallback -- is byte-identical.
        """
        if strict_scale is None:
            strict_scale = strict_tip
        x0, y0, x1, y1 = (self._band_eff() if region is None else region)
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.W, int(x1)), min(self.H, int(y1))
        if x1 <= x0 or y1 <= y0:
            return None
        sub = frame[y0:y1, x0:x1]
        raw = red_mask
        if raw is not None and raw.shape[:2] != sub.shape[:2]:
            return None
        if raw is None and self._perf:
            # S2 (consumer): the cold-miss default-band _scan this same frame just computed
            # this exact mask (identical region + resolved bounds) -- reuse it instead of a
            # second full-band inRange. Any key mismatch falls through to a fresh compute.
            # Same RESOLVED TAGGED ROW key the producer writes (see _scan). Resolving through
            # `red_bounds` also closes a latent hole: the old key ignored red_bounds entirely, so
            # an explicit-band call could have been handed the DEFAULT band's memoized mask. Every
            # live caller of this method passes red_bounds=None (the tip-first variant is the one
            # that takes an explicit band, and it has no memo), so this is inert today.
            eff = self._resolve_red_band(red_bounds)
            key = (x0, y0, x1, y1, eff)
            if key == self._scan_raw_key:
                raw = self._scan_raw
                self._perf_mask_reuses += 1
        if raw is None:
            raw = self._redmask(sub, bounds=red_bounds)
        # The physically-armed court-wide path also covers a half-court Go-To camera, where the
        # world-space meter can be ~0.45x the nominal on-screen width.  Keep the normal pop-in path
        # byte-identical; only strict red+connected-green acquisition may use the smaller floors.
        min_w = (max(5, int(round(self._w_min * 0.45)))
                 if strict_scale else self._w_min)
        min_stub_h = (max(3, int(round(self._stub_h_min * 0.45)))
                      if strict_scale else self._stub_h_min)
        if stub_h_min is not None:
            min_stub_h = max(3, int(stub_h_min))
        # early-out: a pop-in stub is >=~70 strict-red px at nominal scale; idle frames (no red in
        # band) skip close/contour work.  The lower strict threshold is still bounded by the later
        # connected-component and exact red/green relation checks.
        raw_floor = 12 if strict_tip else 30
        if cv2.countNonZero(raw) < max(
                raw_floor, int(0.35 * min_w * min_stub_h)):
            return None
        # wider horizontal CLOSE than _scan: the pop-in stub's chevron notch can split the strict
        # red mask into side-by-side fragments (f01168: w10+w12 with a 6px gap) -> bridge them.
        mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((5, 7), np.uint8))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best, best_score = None, float("-inf")
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (min_w <= w <= self._w_max + 4) or not (min_stub_h <= h <= self._h_max):
                continue
            if cv2.countNonZero(mask[y:y + h, x:x + w]) / float(max(1, w * h)) < self.STUB_RED_FRAC:
                continue
            fx, fy = x0 + x, y0 + y
            floor = fy + h
            cxm = fx + w * 0.5
            # Width is fill-independent and therefore the safest local perspective scale.  At
            # half court the tip-to-floor distance shrinks with it; a fixed 73..127px window at
            # 720p looked above a real 7..12px-wide meter and made acquisition impossible.
            local_scale = 1.0
            if strict_scale:
                seed_w = max(1.0, 0.5 * float(self._w_min + self._w_max))
                local_scale = max(0.45, min(1.25, float(w) / seed_w))
            tip_dx = max(4, int(round(self._tip_dx * local_scale)))
            tip_up_min = max(10, int(round(self._tip_up_min * local_scale)))
            tip_up_max = max(tip_up_min + 5,
                             int(round(self._tip_up_max * local_scale)))
            # Give a strict court-wide component enough context to prove its *whole* width.  The
            # old tight crop could turn an 80px scoreboard stripe into a seemingly valid 16px cap.
            # A component touching an internal crop edge is rejected below; a real frame-edge cap
            # remains admissible because that boundary is observable image truncation.
            search_dx = (tip_dx + max(3, int(np.ceil(0.5 * w)))
                         if strict_scale else tip_dx)
            gx0 = max(0, int(cxm - search_dx)); gx1 = min(self.W, int(cxm + search_dx))
            gy0 = max(0, int(floor - tip_up_max)); gy1 = min(self.H, int(floor - tip_up_min))
            if gx1 - gx0 < 4 or gy1 - gy0 < 4:
                continue
            if green_mask is not None:
                if green_mask.shape[:2] != sub.shape[:2]:
                    continue
                gmask = green_mask[gy0 - y0:gy1 - y0, gx0 - x0:gx1 - x0]
            else:
                gsub = frame[gy0:gy1, gx0:gx1]
                gmask = self._greenmask(
                    cv2.cvtColor(gsub, cv2.COLOR_BGR2HSV), bounds=green_bounds)
            gpx = int(cv2.countNonZero(gmask))
            if gpx < self._tip_px_min:
                continue
            if strict_tip:
                # Court-wide scanning sees scoreboards, shoes and signage that
                # the normal band intentionally excludes.  Aggregate green pixel
                # count is therefore insufficient: require one compact connected
                # component, horizontally centred on the red stub, inside the
                # expected 110..190 px-above-floor window.
                gcnts, _ = cv2.findContours(
                    gmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                strict_gpx = 0
                centre_tol = max(3.0, min(float(tip_dx) * 0.5,
                                          float(w) * 0.75))
                for gc in gcnts:
                    gx, gy, gw, gh = cv2.boundingRect(gc)
                    if ((gx == 0 and gx0 > 0)
                            or (gx + gw == gmask.shape[1] and gx1 < self.W)):
                        # The connected component continues beyond an artificial ROI boundary, so
                        # its true size is unknown.  Never validate a wide decor stripe by cropping.
                        continue
                    area = int(cv2.countNonZero(gmask[gy:gy + gh, gx:gx + gw]))
                    if area < self._tip_px_min or gw < 2 or gh < 2:
                        continue
                    if gw > self._gw_max or gh > self._gh_max:
                        continue
                    # The connected tip must stay commensurate with its red stub.  This matters
                    # only for the global strict path and prevents a tiny red jersey/logo from
                    # borrowing a much larger green scoreboard component at the right height.
                    if gw > max(4, int(round(w * 1.75))):
                        continue
                    if area / float(max(1, gw * gh)) < 0.45:
                        continue
                    gcx = gx0 + gx + 0.5 * gw
                    if abs(gcx - cxm) > centre_tol:
                        continue
                    strict_gpx = max(strict_gpx, area)
                if strict_gpx <= 0:
                    continue
                gpx = strict_gpx
            score = h + gpx
            if score > best_score:
                best_score, best = score, (fx, fy, min(w, self._w_max), h)
        return best

    def _clear_micro_candidate(self) -> None:
        """Forget the untrusted sub-quarter-scale court-wide candidate.

        This state never owns ``self.box`` and never reaches DetectResult.  It is
        intentionally cleared by every physical-shot boundary, disarm, missing
        micro frame, and normal/strict acquisition so pixels cannot accumulate
        corroboration across shots or across a blank/loading frame.
        """
        self._micro_pending_col = None
        self._micro_pending_ts = None
        self._micro_pending_epoch = 0
        self._micro_pending_tier = ""
        self._micro_pending_count = 0

    @staticmethod
    def _same_micro_candidate(previous, current) -> bool:
        """Associate two tiny structure observations without freezing motion.

        A Go-To meter may translate a few pixels per 60 Hz frame and its two- or
        three-pixel red body can quantize by a whole pixel.  Compare centre,
        floor, and width ratio rather than exact rectangles; a distant unrelated
        component elsewhere in the full frame cannot complete the proof.
        """
        if previous is None or current is None:
            return False
        px, py, pw, ph = (float(v) for v in previous)
        cx, cy, cw, ch = (float(v) for v in current)
        if min(pw, ph, cw, ch) <= 0.0:
            return False
        centre_dx = abs((px + 0.5 * pw) - (cx + 0.5 * cw))
        floor_dy = abs((py + ph) - (cy + ch))
        width_ratio = max(pw, cw) / max(1.0, min(pw, cw))
        return (centre_dx <= max(6.0, 2.0 * max(pw, cw))
                and floor_dy <= max(7.0, 0.45 * max(ph, ch))
                and width_ratio <= 2.0)

    def _corroborate_micro_candidate(self, col, ts, tier: str) -> bool:
        """Require two unique, adjacent hardware-shot frames before locking.

        The first micro observation is deliberately invisible and timing-inert.
        A repeated callback for the same capture timestamp cannot increment the
        proof.  Missing geometry clears the candidate in the caller, so the two
        observations are consecutive detector frames rather than sparse colour
        coincidences over a long arm window.
        """
        if not self._shot_armed_hw or col is None:
            self._clear_micro_candidate()
            return False
        try:
            frame_ts = float(ts)
        except (TypeError, ValueError, OverflowError):
            self._clear_micro_candidate()
            return False
        if not np.isfinite(frame_ts):
            self._clear_micro_candidate()
            return False

        epoch = int(self._physical_shot_epoch)
        previous_ts = self._micro_pending_ts
        same_epoch = self._micro_pending_epoch == epoch
        newer_unique_frame = bool(
            previous_ts is not None and frame_ts > float(previous_ts) + 1e-9)
        close_in_time = bool(
            newer_unique_frame and frame_ts - float(previous_ts) <= 0.120)
        same_candidate = self._same_micro_candidate(
            self._micro_pending_col, col)

        if same_epoch and close_in_time and same_candidate:
            self._micro_pending_count += 1
        elif previous_ts is not None and abs(frame_ts - float(previous_ts)) <= 1e-9:
            # Duplicate delivery of one capture frame is not new evidence.  Keep
            # the original observation verbatim so repeated callbacks cannot
            # move a pending candidate toward a different component either.
            return False
        else:
            self._micro_pending_count = 1

        self._micro_pending_col = tuple(int(v) for v in col)
        self._micro_pending_ts = frame_ts
        self._micro_pending_epoch = epoch
        self._micro_pending_tier = str(tier)

        if (self._micro_pending_count < 2
                or not self._shot_armed_hw
                or int(self._physical_shot_epoch) != epoch):
            return False
        self._clear_micro_candidate()
        return True

    def _acquire_structure_tip_first(self, frame, region=None,
                                     red_bounds=None, green_bounds=None,
                                     red_mask=None, green_mask=None,
                                     micro=False):
        """Court-wide acquire for a clean cap whose red body is motion-occluded.

        The ordinary strict path is intentionally red-first.  During a Go-To gather, however,
        the player's hand/body can leave only a narrow vertical slice of red while the fixed green
        cap remains intact.  Deriving perspective from that fragment pulls the expected tip window
        down and the genuine meter is missed.  This physically-armed-only fallback reverses the
        proof order: find one compact green component, then require a strict-red vertical fragment
        below it at the same scale and centre.  It never changes the timing colour masks or accepts
        either colour alone.
        """
        x0, y0, x1, y1 = ((0, 0, self.W, self.H) if region is None else region)
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.W, int(x1)), min(self.H, int(y1))
        if x1 <= x0 or y1 <= y0:
            return None

        sub = frame[y0:y1, x0:x1]
        if green_mask is not None and green_mask.shape[:2] != sub.shape[:2]:
            return None
        if red_mask is not None and red_mask.shape[:2] != sub.shape[:2]:
            return None
        if green_mask is None:
            hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
            green_raw = self._greenmask(hsv, bounds=green_bounds)
        else:
            green_raw = green_mask
        green_px_floor = max(self._tip_px_min, 4) if micro else self._tip_px_min
        if cv2.countNonZero(green_raw) < green_px_floor:
            return None
        green = cv2.morphologyEx(
            green_raw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        green_contours, _ = cv2.findContours(
            green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        red_raw = (red_mask if red_mask is not None
                   else self._redmask(sub, bounds=red_bounds))
        red_px_floor = 6 if micro else 9
        if cv2.countNonZero(red_raw) < red_px_floor:
            return None
        # A small close repairs codec pinholes but deliberately cannot bridge the wide player/body
        # occlusion that made the red-first scale estimate unreliable in the first place.
        red = cv2.morphologyEx(
            red_raw, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
        red_contours, _ = cv2.findContours(
            red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        red_candidates = []
        for contour in red_contours:
            rx, ry, rw, rh = cv2.boundingRect(contour)
            min_red_w = 2 if micro else 3
            if (rw < min_red_w or rh < 3
                    or rw > self._w_max + 4 or rh > self._h_max):
                continue
            rarea = int(cv2.countNonZero(red[ry:ry + rh, rx:rx + rw]))
            if (rarea < red_px_floor
                    or rarea / float(max(1, rw * rh)) < self.STUB_RED_FRAC):
                continue
            red_candidates.append((rx, ry, rw, rh, rarea))
        if not red_candidates:
            return None

        seed_green_w = max(1.0, 0.5 * float(self._gw_min + self._gw_max))
        seed_red_w = max(1.0, 0.5 * float(self._w_min + self._w_max))
        min_green_w = (max(2, int(round(self._gw_min * 0.15)))
                       if micro else max(3, int(round(self._gw_min * 0.35))))
        # The micro tier is a narrow gap-filler, not a second permissive global
        # detector.  Larger caps remain owned by the strict path above; only
        # components that really rasterize below its ~0.45x geometry floor may
        # enter temporal corroboration.
        max_micro_green_w = max(
            min_green_w, int(np.ceil(seed_green_w * self.MICRO_CAP_SCALE)))
        best, best_score = None, float("-inf")
        for contour in green_contours:
            gx, gy, gw, gh = cv2.boundingRect(contour)
            if (gw < min_green_w or gh < 2
                    or gw > self._gw_max or gh > self._gh_max):
                continue
            if micro and gw > max_micro_green_w:
                continue
            garea = int(cv2.countNonZero(green[gy:gy + gh, gx:gx + gw]))
            if (garea < green_px_floor
                    or garea / float(max(1, gw * gh)) < 0.45
                    or gw < 0.65 * gh):
                continue

            local_scale = max(
                0.18 if micro else 0.45,
                min(1.25, float(gw) / seed_green_w))
            tip_up_min = max(
                5 if micro else 10,
                int(round(self._tip_up_min * local_scale)))
            tip_up_max = max(
                tip_up_min + (4 if micro else 5),
                int(round(self._tip_up_max * local_scale)))
            gcx = gx + 0.5 * gw
            gcy = gy + 0.5 * gh
            centre_tol = max(2.0 if micro else 3.0,
                             (0.65 if micro else 0.55) * gw)
            relation_slack = 0.5 * gh + 2.0

            for rx, ry, rw, rh, rarea in red_candidates:
                if rw > max(4 if micro else 5, int(round(1.50 * gw))):
                    continue
                if rw < max(2 if micro else 3, int(np.floor(0.18 * gw))):
                    continue
                rcx = rx + 0.5 * rw
                if abs(rcx - gcx) > centre_tol:
                    continue
                # The red fill begins at (or below) the green cap and ends at the track floor.
                # A small overlap allowance covers anti-aliasing, never a component above the cap.
                cap_overlap = max(
                    1 if micro else 2, int(round(3.0 * local_scale)))
                if ry < gy + gh - cap_overlap:
                    continue
                floor = ry + rh
                tip_up = floor - gcy
                if (tip_up < tip_up_min - relation_slack
                        or tip_up > tip_up_max + relation_slack):
                    continue

                # Reconstruct only enough underlying body width to keep the existing row-density
                # timing read valid.  This prevents a one-sided occlusion fragment from seeding an
                # impossibly narrow tracking scale, while never widening beyond the cap-derived
                # perspective or the production width ceiling.
                inferred_w = int(round(seed_red_w * local_scale))
                row_density_cap = max(
                    rw, int(np.floor(rw / max(1e-6, self.params.red_row_thr))) - 6)
                inferred_w = max(rw, min(self._w_max, inferred_w, row_density_cap))
                preferred_x = int(round(gcx - 0.5 * inferred_w))
                contain_lo = rx + rw - inferred_w
                contain_hi = rx
                inferred_x = max(contain_lo, min(preferred_x, contain_hi))
                inferred_x = max(0, min(x1 - x0 - inferred_w, inferred_x))

                relation_mid = 0.5 * (tip_up_min + tip_up_max)
                score = (rh + 0.20 * rarea + 0.40 * garea
                         - 2.0 * abs(rcx - gcx)
                         - 0.10 * abs(tip_up - relation_mid))
                if score > best_score:
                    best_score = score
                    best = (x0 + inferred_x, y0 + ry, inferred_w, rh)
        return best

    def _micro_redmask(self, frame, hsv=None):
        """B9a: the micro tier's red mask, with a RED-DOMINANCE floor. See __init__.

        `_MICRO_RED` is the BGR box [0,0,110]-[110,110,255]. A box cannot express "red": the
        corner (110,110,110) is NEUTRAL GREY and passes, so the band admits every dim
        grey/brown/stone pixel in the scene. That is what makes the tier's full-frame scan find
        arena architecture. Intersect it with the one thing the box cannot say -- that the red
        channel actually DOMINATES -- and the band means what its name claims.

        The dominance floor is BGR-ONLY, for two independent reasons. (1) It is a REPAIR for a
        hole that only a BGR box has: an HSV row's saturation floor already makes the grey axis
        unrepresentable, which is the exact property dominance was bolted on to recover. (2) It
        would be actively WRONG on a purple bar, whose blue channel legitimately exceeds its red
        channel -- `r - max(g, b)` is negative there, so the floor would erase the whole mask and
        the micro tier would go permanently blind on Purple.
        """
        row = self._bands[_mbc.BAND_MICRO]
        base = self._redmask(frame, bounds=row, hsv=hsv)
        if self._micro_red_dom <= 0 or not _mbc.micro_needs_dominance(row):
            return base
        # All-uint8 cv2 ops (this runs on a full frame inside the 16.7 ms detector budget):
        # cv2.subtract saturates at 0, so a non-red pixel lands at 0 and is thresholded out.
        b, g, r = cv2.split(frame)
        dom = cv2.subtract(r, cv2.max(g, b))
        _, dom = cv2.threshold(dom, int(self._micro_red_dom) - 1, 255, cv2.THRESH_BINARY)
        return cv2.bitwise_and(base, dom)

    def _sess_track_h_is_tight(self) -> bool:
        """[ORION_SESS_TRACK_FALLBACK] Is the session's track-height history consistent enough
        that its median is a safe fill DENOMINATOR for a lock that has no history of its own?

        This is a self-validation, not an assumption. The session twin can hold heights measured
        at different camera distances, and the meter genuinely renders at different sizes (16x108
        and 23x115 were both measured in one batch). A median across mixed scales would be biased,
        and a biased denominator is a biased fill that the bot then times off.

        So: require several samples that agree closely. Measured on a healthy single-distance
        session, accepted track heights sit in ONE tight family -- 106-109 px on 96.9%% of frames,
        i.e. under 3%% spread -- so a real session passes comfortably. The 10%% bound is the same
        shape and the same constant the scale-reset run already uses a few lines above
        (`max(lch) - min(lch) <= 0.10 * max(lch)`), so this introduces no new magic number.

        Failing the test is not a regression: the caller then bails exactly as it did before.
        """
        vals = [float(v) for v in self._sess_track_h[-5:] if float(v) > 0.0]
        if len(vals) < 3:
            return False           # too little evidence to call it a measurement
        hi = max(vals)
        return hi > 0.0 and (hi - min(vals)) <= 0.10 * hi

    def _micro_tier_plausible(self) -> bool:
        """B9: may the sub-quarter-scale MICRO tier run on this capture at all?

        True == run it (the shipped behaviour). False == this capture has already been measured
        and it is not the one the micro tier exists for, so skip the tier entirely.

        The micro tier only looks at green caps up to `MICRO_CAP_SCALE` x nominal, so a micro
        candidate is implicitly a claim that the meter is rendering at roughly that perspective
        scale or smaller. `_sess_track_h` is the reader's own measurement of the same quantity
        -- the FULL track height, green-cap top to floor, seeded only from near-full caps and
        guarded on both sides (A1 low / A4 high), and restricted here to locks the micro tier
        did NOT seat so it can never vouch for itself -- and the nominal track for this capture
        is the mid-point of the scaled tip-above-floor prior. When the measured track is more
        than `_micro_gate_ratio` x the tier's own ceiling, the two claims are incompatible: the
        meter that is actually on screen in this session is several times larger than anything
        this tier can accept, so every candidate it can still produce is something else.

        FAILS OPEN in every ambiguous case -- flag off, no history yet (a cold reader, the micro
        unit fixtures), a degenerate prior. A genuine camera/scale change is not a permanent
        block either: A1's low-side recovery walks `_track_h_hist` down onto the new scale after
        `_scale_reset_m` mutually-consistent frames, and the gate re-opens with it.
        """
        if not self._micro_scale_gate:
            return True
        hist = self._sess_track_h
        if len(hist) < self._micro_gate_min_n:
            return True                                   # no session measurement -> never tighten
        nominal_track = 0.5 * float(self._tip_up_min + self._tip_up_max)
        if nominal_track <= 0.0:
            return True
        measured = float(np.median(hist[-self._micro_gate_min_n:]))
        if measured <= self._micro_gate_ratio * self.MICRO_CAP_SCALE * nominal_track:
            return True
        self._micro_gate_blocks += 1
        return False

    def _acquire_courtwide_structure(self, frame, ts=None):
        """Physically-armed full-frame acquisition with bounded codec tolerance.

        Return ``(column, tier, red_bounds, green_bounds)``.  The caller is the sole hardware-arm
        gate: this method is never reached from an idle/CV-self-arm scan.  Every tier still requires
        one compact red body and one compact green cap in the meter's scale/centre/height relation;
        the tolerant tier changes colour recall only, never geometry or single-colour acceptance.

        Strict production colour wins whenever it exists.  Only after both strict proof orders
        miss do we retry the cap-first proof with the modest codec bands.  Cap-first is used for
        that retry because it derives perspective from the less-occluded fixed cap and therefore
        also covers a fragmented Go-To body without another full-frame red-first pass.
        """
        full = (0, 0, self.W, self.H)
        # Union of the courtwide tier with the nominal tier, so the tolerant band can never be
        # NARROWER than production on any axis. For a BGR row this is the same per-channel
        # min/max it always was; the HSV arm unions the hue window and takes the lower S/V floors
        # (see _merge_bands) -- the per-CHANNEL zip that used to be inline here is meaningless on
        # (hue, sat, val) scalars and would have silently mangled a purple band.
        court_red = self._merge_bands(self._bands[_mbc.BAND_COURTWIDE],
                                      self._bands[_mbc.BAND_NOMINAL])
        court_green = (
            tuple(min(a, b) for a, b in zip(self._COURTWIDE_GREEN[0], self._G[0])),
            tuple(max(a, b) for a, b in zip(self._COURTWIDE_GREEN[1], self._G[1])),
        )
        # The tolerant red mask is a superset of the strict one, but NOT of the micro tier below:
        # sub-quarter-scale chroma averaging may put the only surviving body pixels outside the
        # codec band.  Treat this as a strict/codec feasibility gate, not a method-wide return.
        # The micro scan remains hardware-only at the caller and still needs its independent red
        # mask plus two-frame structural corroboration.
        # An HSV bar colour needs the HSV frame for EVERY bar mask in this method, so convert it
        # once up front in that case and thread it through. A BGR (Red) colour converts nothing
        # here -- `_hsv_pre` stays None and the single conversion below happens exactly where it
        # always did, so Red's op sequence is unchanged.
        _hsv_pre = (cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    if self._bands[_mbc.BAND_NOMINAL][0] == "hsv" else None)
        tolerant_red = self._redmask(frame, bounds=court_red, hsv=_hsv_pre)
        codec_red_possible = cv2.countNonZero(tolerant_red) >= 6
        strict_red = None
        if codec_red_possible:
            # Both strict proof orders consume the same full-frame red mask.  Sharing it avoids an
            # otherwise redundant BGR inRange on every physically-armed first-lock frame.
            strict_red = self._redmask(
                frame, bounds=self._bands[_mbc.BAND_NOMINAL], hsv=_hsv_pre)
            col = self._acquire_structure(
                frame, region=full, strict_tip=True, red_mask=strict_red)
            if col is not None:
                self._clear_micro_candidate()
                return col, "strict_red_first", None, None

        # One HSV conversion feeds both strict and tolerant cap masks.  This matters most on the
        # expected pre-meter frames after the physical arm edge, where the detector still has a
        # 16.7 ms frame budget and all proof orders should fail cheaply.
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV) if _hsv_pre is None else _hsv_pre
        tolerant_green = self._greenmask(hsv, bounds=court_green)
        codec_green_possible = (
            cv2.countNonZero(tolerant_green) >= self._tip_px_min)
        if codec_red_possible and codec_green_possible:
            strict_green = self._greenmask(hsv, bounds=(self._G[0], self._G[1]))
            col = self._acquire_structure_tip_first(
                frame, region=full, red_mask=strict_red, green_mask=strict_green)
            if col is not None:
                self._clear_micro_candidate()
                return col, "strict_cap_first", None, None

            col = self._acquire_structure_tip_first(
                frame,
                region=full,
                red_bounds=court_red,
                green_bounds=court_green,
                red_mask=tolerant_red,
                green_mask=tolerant_green,
            )
            if col is not None:
                self._clear_micro_candidate()
                return (col, "codec_cap_first",
                        court_red, court_green)

        # MICRO HALF-COURT TIER.  A 2-3px body/cap is below the strict path's
        # reliable single-frame geometry.  Retry the same connected cap/body
        # proof with sub-quarter-scale floors, then require a second unique,
        # nearby observation before returning a lock.  This method is reachable
        # only from ``if col is None and hw`` in read(); no cold/CV/menu scan can
        # enter this tier and a pending candidate has no timing authority.
        #
        # B9: ...and only when this capture is one the tier could plausibly be
        # rescuing. The reader's own measured full-track height decides; see
        # _micro_tier_plausible (flag OFF -> always True -> byte-identical).
        # Colour refusal comes BEFORE plausibility: the micro tier is the only
        # full-frame scan in the reader, and for White it is unsafe in principle
        # (no dominance repair for the grey corner, and _MICRO_GREEN's S floor of
        # 45 overlaps white's legal saturation -- a white bar masked AS the cap
        # collapses track_top and reads ~100% every frame). See
        # meter_bar_colors.micro_supported().
        if not _mbc.micro_supported(self._meter_color):
            self._clear_micro_candidate()
            return None, "", None, None
        if not self._micro_tier_plausible():
            self._clear_micro_candidate()
            return None, "", None, None
        micro_tier = "micro_compressed_cap_first"
        # `micro_red` is a BAND CARRIER: the caller latches it into `_lock_red` for the whole life
        # of the resulting lock. It MUST be the tagged row, not a bare BGR pair -- an untagged
        # tuple here resolves as BGR and silently reverts every subsequent read of that lock to a
        # red mask, whatever colour the user configured.
        micro_red, micro_green = self._bands[_mbc.BAND_MICRO], self._MICRO_GREEN
        micro_red_mask = self._micro_redmask(frame, hsv=hsv if _hsv_pre is not None else None)
        micro_green_mask = self._greenmask(hsv, bounds=micro_green)
        col = self._acquire_structure_tip_first(
            frame, region=full, red_mask=micro_red_mask,
            green_mask=micro_green_mask, micro=True)
        if col is None:
            self._clear_micro_candidate()
            return None, "", None, None
        if not self._corroborate_micro_candidate(col, ts, micro_tier):
            return None, "", None, None
        return col, micro_tier, micro_red, micro_green

    # -- Build a locked box anchored on the green tip when the red column is momentarily gone
    #    (cap/deflate). The meter hangs BELOW the tip; reuse the last track height so _read_fill can
    #    still measure any receding red below it. --
    def _box_from_tip(self, tip):
        # N2 (full-track height here) was REVERTED: offline A/B on the committed gate
        # (session_20260717_192510) showed anchoring the green-hold box floor at tip_y + FULL-track
        # height regressed median_shot_peak 93.4 -> 77.6 (below the 82 floor) and added an off-shot
        # false-lock (0 -> 1) -- the taller strip shifts where _read_fill anchors the floor, lowering
        # the peak fill read and reaching stray red below the meter. The synthetic stub-fix was real
        # but net-harmful on live frames, so the shipped behaviour (red-column height) is kept.
        gx, gy, gw, gh = tip
        h = self.box[3] if self.box else max(gh, int(round(120 * self.H / self._REF_H)))
        h = max(gh, int(h))
        # EPOCH-5: CLAMP the box to the frame. This height RATCHETS (max(gh, h) off the last box)
        # and, with VZOOM_DOWN extending the band down to self.H, the resulting FLOOR could sit
        # BELOW the frame bottom. _fill_strip then clips the strip but fillable_h does NOT shrink
        # with it -> the fill divides by a track taller than the pixels actually read (a 20-27pp
        # under-read). Clipping here keeps the two in step.
        h = min(h, max(1, self.H - int(gy)))
        return (gx, gy, gw, h)

    # -- STAGE 2b (locked): bounded, motion-followed COLOR relocate around the last box. Returns
    #    (col, evidence) where evidence is 'red' (a valid red column -> full read, make OR miss) or
    #    'green' (only the green tip -> hold the box glued through cap/deflate). The search is a TIGHT
    #    window around the last box + box velocity so it CANNOT drift onto dÃ©cor, and -- critically --
    #    it only returns a box on real METER COLOUR, never on a bare grayscale template match (that
    #    fallback was the 4632-frame window-mullion false lock). --
    def _relocate_window(self):
        """The motion-followed search window around the last box (shared with the
        compressed reader's luma relocate steps so both search the SAME region).
        The window is CLIPPED to the search band on ALL FOUR sides. The right edge kills the
        static NBA-logo banner at x~1846-1902 (a rightward fade slide would otherwise walk the
        speed-relaxed relocate gates onto it -- the concrete decor-lock path the band trim was
        built to kill, plan B2 [fix]). The TOP/left/bottom clips matter once the coast is long
        (occlusion mitigation): a shot that entered coast with a stale velocity dead-reckons the
        box, and without the clip the widened window drifts ABOVE the band (y<250) onto score/clock
        red digits -- a spurious tiny-red pick that hijacks the lock with a garbage read."""
        x, y, w, h = self.box
        px = int(round(x + self._bvx)); py = int(round(y + self._bvy))
        pad_x = self._pad_x + int(min(160, 2.0 * abs(self._bvx)))
        pad_up = self._pad_up + int(min(120, 1.5 * abs(self._bvy)))
        pad_dn = self._pad_dn + int(min(120, 1.5 * abs(self._bvy)))
        # A court-wide lock still searches only this tight box-centred ROI; the
        # full image merely prevents clipping a legitimate edge/corner meter.
        bx0, by0, bx1, by1 = self._tracking_bounds()
        return (max(bx0, px - pad_x), max(by0, py - pad_up),
                min(bx1, px + w + pad_x), min(by1, py + h + pad_dn))

    def _relocate(self, frame):
        if self.box is None:
            return None, None
        # Motion-follow: shift the search centre by the box velocity and widen the pad with speed so a
        # fast-sliding blurred FADE meter (observed x:1100->11->220 across a shot) stays inside the
        # window. The wide window is SAFE from dÃ©cor because every branch below requires real meter
        # COLOUR to accept -- a grey window mullion carries no red/green and is rejected.
        win = self._relocate_window()
        # B2 blur adaptations, keyed to the MEASURED |bvx| and active ONLY inside this locked,
        # position-bounded, band-clipped relocate window (never at acquire): a fast-sliding fade
        # smears the column, so the gates relax proportionally to the demonstrated speed.
        bvx = abs(self._bvx)
        blur_on = self._robust and bvx > 3.0
        w_extra = int(bvx) if self._robust else 0
        rf_min = max(0.30, 0.45 - 0.02 * bvx) if self._robust else None
        # blur red band = the vetted Arrow2 R>=170 (legacy style band), ONLY at |bvx|>3 and only
        # here -- the window is clipped to the band's right edge so it cannot reach the banner.
        # COLOUR-CORRECT relocate band. This was the literal _COURTWIDE_RED, which on a
        # WHITE meter matches nothing at all -- the blur rescue was dead by construction.
        # Use the CONFIGURED colour's courtwide tier: same "relaxed band" intent, right
        # colour. Red resolves to its own courtwide row, i.e. the identical constants.
        red_b = self._bands[_mbc.BAND_COURTWIDE] if blur_on else None
        # (1) RED column (primary: present for a make AND a miss). ar_min RELAXED: the window is
        #     already position-bounded + colour-gated, and the default 1.8 aspect floor silently
        #     rejected every SHORT tracked column (h<~50 on a w28 meter) -- the early rise right
        #     after a fade pop-in and the deflate tail (H_HOLD=12 was dead code under ar>=1.8),
        #     freezing the fill on the green-tip hold instead of reading it.
        col, _c = self._scan(frame, win, self._h_hold, ar_min=self.params.relocate_ar_min,
                             bounds=red_b, w_max_extra=w_extra, red_frac_min=rf_min)
        if col is not None:
            return col, "red"
        # (1b) STATIONARY-OCCLUSION relaxed-red retry, DECOUPLED from bvx. A player's arm/body
        #      crossing a STATIONARY meter (bvx~0) motion-blurs and partially occludes the strict
        #      Arrow2 red WITHOUT any box motion, so the fast-slide blur gate (blur_on: bvx>3)
        #      never engages and the lock coasts/drops. When occlusion mitigation is on, retry the
        #      SAME position-bounded, band-clipped window with the vetted relaxed Arrow2 red band
        #      (R>=170, the legacy style band) + relaxed geometry regardless of speed. ADDITIVE:
        #      it runs ONLY after the strict scan already missed, so a clean frame (strict hit)
        #      never reaches here -> the pristine read is byte-identical. DÃ©cor-safe: the window is
        #      tight around the last meter box + colour-gated (a grey mullion carries no red).
        if self._occl and red_b is None:
            # Same fix as the blur band above: the hardcoded red literal made this
            # occlusion retry a no-op in White mode, and _occl defaults ON -- so every
            # strict miss fell straight through to coast instead of being rescued.
            col, _c = self._scan(frame, win, self._h_hold, ar_min=self.params.relocate_ar_min,
                                 bounds=self._bands[_mbc.BAND_COURTWIDE],
                                 w_max_extra=max(w_extra, 6), red_frac_min=0.30)
            if col is not None:
                return col, "red"
        # (2) GREEN tip (holds the lock through cap/deflate when the red column is gone).
        #     Blur: S/V floors relax to max(50, 90 - 8|bvx|); a stationary armed occlusion relaxes
        #     the tip S/V to 75 so a blurred/desaturated cap chevron still anchors the lock.
        sv = (max(50, int(round(90 - 8.0 * bvx))) if blur_on
              else (75 if (self._occl and (self._armed_now_cached
                                           or self._occl_wide_live)) else None))
        tip = self._find_green_tip(frame, win, sv_floor=sv)
        if tip is not None:
            return self._box_from_tip(tip), "green"
        # (3) NCC position proposal (fast slide / motion blur fragments the colour contour) -- 2k26.py
        #     grayscale TM_CCOEFF_NORMED, but COLOUR-CONFIRMED: only accepted if the proposed box
        #     actually contains meter red/green, so a persistent grey dÃ©cor edge can never hold it.
        prop = self._ncc_propose(frame, win, blur_px=(int(min(15, bvx)) if blur_on else 0))
        if prop is not None and self._has_meter_colour(frame, prop):
            return prop, "ncc"
        return None, None

    # -- grayscale matchTemplate proposal inside the bounded window (position hint only).
    #    The best score is exposed on self._last_ncc_score every attempt (B1 [fix]: the
    #    staleness NCC EWMA needs the score, not just the accept/reject). --
    def _ncc_propose(self, frame, win, blur_px: int = 0):
        if self.tmpl is None or self.tmpl_wh is None:
            return None
        tw, th = self.tmpl_wh
        x0, y0, x1, y1 = [int(v) for v in win]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.W, x1), min(self.H, y1)
        if x1 - x0 < tw or y1 - y0 < th:
            return None
        gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        tmpl = self.tmpl
        if blur_px >= 2:
            # B2 blur: NCC matches a MOTION-BLURRED template at match time (horizontal kernel
            # sized by |bvx|), so a smeared meter still correlates instead of dropping to coast.
            tmpl = cv2.blur(tmpl, (int(blur_px) | 1, 1))
        # TM_CCOEFF_NORMED is degenerate for a flat (zero-variance) template/window.
        if float(tmpl.std()) < 1e-3 or float(gray.std()) < 1e-3:
            return None
        res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        self._last_ncc_score = float(mx)
        if self._calibrator is not None:
            try:
                self._calibrator.note_ncc(float(mx))
            except Exception:
                pass
        if mx < self.NCC_LOCK:
            return None
        return (x0 + loc[0], y0 + loc[1], tw, th)

    # -- does a candidate box carry real METER colour (red fill or green tip)? the dÃ©cor guard --
    def _has_meter_colour(self, frame, box):
        bx, by, bw, bh = [int(v) for v in box]
        bx, by = max(0, bx), max(0, by)
        x1, y1 = min(self.W, bx + bw), min(self.H, by + bh)
        if x1 - bx < 3 or y1 - by < 3:
            return False
        sub = frame[by:y1, bx:x1]
        red_px = int(cv2.countNonZero(self._redmask(sub)))
        if red_px >= max(20, int(0.08 * sub.shape[0] * sub.shape[1])):
            return True
        grn_px = int(cv2.countNonZero(self._greenmask(cv2.cvtColor(sub, cv2.COLOR_BGR2HSV))))
        return grn_px >= max(20, int(0.5 * self._g_area_min))

    # -- fill-strip geometry (shared with CompressedMeterReader so the two fill paths can
    #    never disagree about WHERE they read; byte-identical to the original inline code) --
    def _fill_strip(self, frame, col):
        cx, cw = col[0], col[2]
        floor = col[1] + col[3]                                  # red bottom = track floor
        x0 = max(0, cx - 3); x1 = min(self.W, cx + cw + 3)
        # EPOCH-6: the 240px look-up window is an @1080p prior like every other geometry constant,
        # so it must be scaled to the live capture size (identity at 1080p). Left unscaled it
        # capped the strip -- and therefore the denominator -- BELOW the true track at 1440p/4K,
        # which makes the fill OVER-read and the bot fire early.
        sy = self.H / self._REF_H if self.H else 1.0
        top_search = max(0, floor - max(40, int(round(240 * sy))))
        strip = frame[top_search:min(self.H, floor + 3), x0:x1]
        return strip, x0, top_search

    # -- green make-window as a track-relative % band on the SAME fill scale (track-top = 100%). --
    def _green_window(self, green_px, cap_top, cap_bot, ph, fillable_h):
        """The green chevron cap sits at the TOP of the track; on the fill scale it occupies the top
        `band`% -> the make window is [100-band, 100]. Emitted only when a real cap run is seen; the
        engine times the tip (100) otherwise. Gives the native greenTracker/fill_kalman a real
        release target on the SAME scale the fill is reported on (no early-fire scale mismatch)."""
        if green_px < 2 or cap_top is None or cap_bot is None or fillable_h <= 0:
            return None
        _band_raw = (cap_bot - cap_top + 1) / fillable_h * 100.0
        band = min(20.0, max(2.0, _band_raw))
        # F3-3 (ORION_READER_GREEN_BAND_DIAG, default ON) -- OBSERVABILITY ONLY.
        # `band` is clamped to [2.0, 20.0], so a `green_start` of 98.00 is AMBIGUOUS: it is
        # either a real 2pp cap or the FLOOR binding on a sub-2pp measurement. The tell is that
        # every unbound value is an exact pixel multiple of 1/fillable_h (2.78 = 3px, 5.56 = 6px
        # on a 108px track) while 2.00pp is 2.16px -- not a whole pixel, so it cannot be a real
        # measurement. Record the PRE-clamp value and which rail bound it. `band` itself is
        # untouched, so the emitted window is byte-identical with the flag on or off; only the
        # two diagnostic attributes below change.
        if self._green_band_diag:
            self._green_band_raw = float(_band_raw)
            self._green_band_bound = ("floor" if _band_raw < 2.0 else
                                      ("ceil" if _band_raw > 20.0 else ""))
        g_end = 100.0
        g_start = max(80.0, g_end - band)
        g_center = (g_start + g_end) * 0.5
        g_conf = min(1.0, green_px / 6.0)
        return (round(g_start, 2), round(g_end, 2), round(g_center, 2),
                round(g_end - g_start, 2), round(g_conf, 3), int(green_px))

    # ------------------------------------------------------------------ #
    #  GREEN-ZONE window + release diagnostic (ORION_GREEN_ZONE_WINDOW /
    #  ORION_GREEN_SELF_GRADE; every call site is flag-guarded -> OFF is
    #  byte-identical). Port of tools/diagnostics/green_zone_grader.py.
    # ------------------------------------------------------------------ #
    def set_green_grade_sink(self, fn) -> None:
        """Optional hook invoked once per detector release-window diagnostic."""
        self._gz_sink = fn

    def _gz_process(self, hsv, red_top, ph, fillable_h, green):
        """Per-frame green-zone step, called from _read_fill ONLY when a gz flag is on.
        (a) Collect the neon band's LOWER edge as a fill-% sample when the band is
            UN-OCCLUDED (red top strictly below the neon bottom -- a real gap), exactly
            the grader's expose rule, on THIS frame's fill scale (same ph/fillable_h
            denominator the fill % uses -> no scale mismatch).
        (b) When ORION_GREEN_ZONE_WINDOW is on and a per-shot estimate exists, OVERRIDE
            the emitted green window with the colour-derived [g_lo, 100]."""
        try:
            neon = cv2.inRange(hsv, np.array(_NEON_LO, np.uint8), np.array(_NEON_HI, np.uint8))
            rows = np.flatnonzero(np.count_nonzero(neon, axis=1) >= 1)
            if rows.size:
                g_bot = int(rows[-1])
                # F3-2 (ORION_READER_GZ_TOP_EDGE, default ON) -- OBSERVABILITY ONLY.
                # `rows[0]` is the neon band's TOP edge. It is the ONLY quantity in the reader
                # that could ever measure `green_end` (the window's upper rail, currently hard-
                # wired to 100.0 everywhere); until now it was computed by the same flatnonzero
                # above and silently discarded, so the batch could not measure whether the cap
                # actually reaches the track top. Sampled under the SAME un-occluded expose rule
                # as the bottom edge and on the SAME fill scale. Attribute-only: `_gz_starts`
                # and every emitted window are untouched, so behaviour is byte-identical.
                if self._gz_top_edge and fillable_h > 0:
                    g_top = int(rows[0])
                    if red_top is not None and int(red_top) > g_bot + self._gz_expose_px:
                        _hi = (ph - g_top) / float(fillable_h) * 100.0
                        if 30.0 <= _hi <= 130.0:
                            self._gz_ends.append(float(_hi))
                            if len(self._gz_ends) > 90:
                                self._gz_ends = self._gz_ends[-90:]
                # un-occluded only: the rising red must sit BELOW the band's lower edge,
                # otherwise red has begun to clip it and the edge reads high (narrow).
                if red_top is not None and int(red_top) > g_bot + self._gz_expose_px \
                        and fillable_h > 0:
                    g_lo = (ph - g_bot) / float(fillable_h) * 100.0
                    if 30.0 <= g_lo <= 100.0:      # sanity: reject reflections far below the track
                        self._gz_starts.append(float(g_lo))
                        if len(self._gz_starts) > 90:
                            self._gz_starts = self._gz_starts[-90:]
        except Exception:
            pass
        if not self._gz_window:
            return green
        est = self._gz_estimate()
        if est is None:
            return green                    # no colour sample yet this shot -> unchanged emission
        g_lo, g_conf = est
        g_px = int(green[5]) if green is not None else 0
        return (round(g_lo, 2), 100.0, round((g_lo + 100.0) * 0.5, 2),
                round(100.0 - g_lo, 2), round(g_conf, 3), g_px)

    def _gz_estimate(self):
        """Per-shot window start = robust LOW edge of the exposed-band samples (widest exposed =
        truest): the 20th percentile, mirroring the offline grader. None until a sample lands."""
        if not self._gz_starts:
            return None
        g_lo = float(np.percentile(self._gz_starts, 20))
        g_lo = max(50.0, min(99.5, g_lo))
        conf = min(1.0, len(self._gz_starts) / 4.0)
        return g_lo, conf

    def _gz_begin_shot(self) -> None:
        self._gz_starts = []
        self._gz_ends = []              # F3-2: same per-shot lifecycle as _gz_starts
        self._gz_fill_at_release = None
        self._gz_release_seq = 0
        self._gz_release_physical_epoch = 0
        self._gz_release_shot_attempt = 0
        self._gz_release_identity_verified = False
        self._gz_release_latched = False
        self._gz_graded = False

    def _gz_end_shot(self, reason: str) -> None:
        """Emit a detector-only release-window diagnostic, then clear per-shot state.

        The label describes where Orion's release command landed relative to its colour-derived
        window.  It is never evidence that NBA 2K made or missed the shot.  A record without both
        a real fill latch and a positive native release id is explicitly a proxy and downstream
        production telemetry rejects it.
        """
        # RELEASE ORACLE (diagnostic): settle the open window BEFORE the record below reads
        # its gap, so `oracle_gap_px` describes THIS release and never the previous one.
        self._ro_flush("shot_end")
        try:
            if self._gz_grade and not self._gz_graded:
                est = self._gz_estimate()
                peak = float(self._peak_fill)
                if est is not None and (peak > 0.0 or self._gz_fill_at_release is not None):
                    g_lo, g_conf = est
                    fill_proxy = self._gz_fill_at_release is None
                    proxy = (fill_proxy or not self._gz_release_latched
                             or self._gz_release_seq <= 0
                             or not self._gz_release_identity_verified)
                    # Identity failure makes the record non-authoritative, but it must not rewrite
                    # the local detector diagnostic from the observed release fill to peak fill.
                    fill_used = peak if fill_proxy else float(self._gz_fill_at_release)
                    if fill_used < g_lo - self._gz_tol:
                        label = "EARLY"
                    elif fill_used > 100.0 + self._gz_tol:
                        label = "OVER"
                    else:
                        label = "GREEN"
                    rec = {"t": round(_time.time(), 3), "g_lo": round(g_lo, 1), "g_hi": 100.0,
                           "win_width_pp": round(100.0 - g_lo, 1),
                           "fill_at_release": round(fill_used, 1), "peak_fill": round(peak, 1),
                           "release_seq": int(self._gz_release_seq),
                           "physical_epoch": int(self._gz_release_physical_epoch),
                           "shot_attempt": int(self._gz_release_shot_attempt),
                           "release_proxy": bool(proxy), "label": label,
                           "window_conf": round(g_conf, 3),
                           "n_exposed_green": len(self._gz_starts), "end_reason": str(reason),
                           # RELEASE ORACLE (see __init__) -- APPEND-ONLY. The settled
                           # white-top-to-green-bottom gap for this release, or -1.0 when the
                           # window never produced a measurable frame. Diagnostic; no consumer
                           # of this record may time anything on it.
                           "oracle_gap_px": round(float(
                               (self.last_release_oracle or {}).get("gap_px", -1.0)), 2)}
                    self._gz_graded = True
                    self.last_green_grade = rec
                    self._gz_emit(rec)
        except Exception:
            pass
        self._gz_starts = []
        self._gz_ends = []              # F3-2: same per-shot lifecycle as _gz_starts
        self._gz_fill_at_release = None
        self._gz_release_seq = 0
        self._gz_release_physical_epoch = 0
        self._gz_release_shot_attempt = 0
        self._gz_release_identity_verified = False
        self._gz_release_latched = False
        # A LOCK DROP is a definitive shot end (the next lock is a NEW shot), so re-open
        # grading. An hw_disarm leaves _gz_graded latched until the trailing lock drop (or
        # the next hw-arm) so post-release deflate frames can never mint a second label.
        if reason == "lock_drop":
            self._gz_graded = False

    def _gz_emit(self, rec: dict) -> None:
        """Fan the release-window diagnostic out without claiming a gameplay outcome."""
        try:
            _gz_logger.info("release_window_diagnostic %s", _json.dumps(rec, sort_keys=True))
        except Exception:
            pass
        try:
            if self._gz_log_path is None:
                root = _os.path.dirname(_os.path.abspath(__file__))
                d = _os.path.join(root, "logs", "diagnostics", "green_zone_labels")
                _os.makedirs(d, exist_ok=True)
                self._gz_log_path = _os.path.join(
                    d, "labels_%s.jsonl" % _datetime.date.today().strftime("%Y%m%d"))
            with open(self._gz_log_path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(rec, sort_keys=True) + "\n")
        except Exception:
            pass
        if self._gz_sink is not None:
            try:
                self._gz_sink(dict(rec))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  SUB-PIXEL fill boundary (ORION_READER_SUBPIX_EDGE, default OFF)
    # ------------------------------------------------------------------ #
    def _subpix_edge_row(self, strip, t, win=8, guard=1):
        """Continuous row of the fill boundary, by 50%-COVERAGE interpolation of the colour
        ramp across the edge.

        The boundary pixel row is PARTIALLY covered by the bar, so under alpha compositing its
        colour is a linear blend of the fill colour and whatever is behind it: coverage is
        recoverable, and the whole-pixel step (1px = 100/track_h pp) is not a floor.

        The blend is measured by PROJECTING each row's mean colour onto the local
        background->fill axis, both ends estimated from THIS frame's own pixels a few rows
        either side of the edge. That self-calibration is the point: a fixed colour threshold
        (what the shipped refinement uses) cuts the ramp at a position that moves with the local
        contrast, and the local contrast is constant within a shot but different between shots
        -- i.e. it is a per-shot correlated bias. A 50%-coverage crossing is a geometric
        feature, so it does not move with contrast, gain, or the codec's chroma handling.

        Returns None -- caller keeps the shipped estimate -- whenever the ramp is not cleanly
        resolvable: too few rows either side, no measurable colour step, or a profile that never
        actually spans the transition (occlusion, a green cap hard against the red, dÃ©cor).
        Cost is bounded to a <=18-row x (pw-8) window regardless of the strip height.
        """
        ph, pw = strip.shape[:2]
        lo = max(0, t - guard - win)
        hi = min(ph, t + guard + win)
        i = t - lo                                    # coarse edge, window-relative
        if i < 4 or (hi - lo) - i < 4:
            return None
        iw = slice(4, pw - 4) if pw > 10 else slice(0, pw)
        seg = strip[lo:hi, iw, :]
        if seg.shape[1] < 2:
            return None
        prof = seg.mean(axis=1, dtype=np.float32)         # (n, 3) row-mean BGR
        # PERF: np.median costs ~15us per call here (a sort plus its dispatch overhead) against a
        # 0.2-0.4ms whole-frame budget. np.partition at the midpoint is the same order statistic
        # for odd counts and the lower median for even ones -- an equally robust centre for a
        # run of near-identical rows -- for ~2us.
        _f, _b = prof[i + guard:], prof[:i - guard]
        fg = np.partition(_f, _f.shape[0] // 2, axis=0)[_f.shape[0] // 2]   # inside the fill
        bg = np.partition(_b, _b.shape[0] // 2, axis=0)[_b.shape[0] // 2]   # above the edge
        dv = fg - bg
        den = float(dv @ dv)
        if den < 64.0:                                    # < 8 grey levels of separation
            return None
        # 0 above the edge, 1 inside the fill. .tolist() once: the crossing walk below indexes
        # this 14-18 element profile several times and Python-scalar indexing is ~10x cheaper
        # than materialising a numpy scalar per access.
        al = ((prof - bg) @ dv / den).tolist()
        n = len(al)
        # the window must genuinely straddle the transition, or the "crossing" is noise
        if not (al[0] + al[1] < 0.60 < 1.40 < al[-1] + al[-2]):
            return None
        k = i
        if al[k] < 0.5:                                   # walk DOWN into the fill
            while k + 1 < n and al[k] < 0.5:
                k += 1
            if al[k] < 0.5:
                return None
        while k - 1 >= 0 and al[k - 1] >= 0.5:            # then UP to the topmost crossing
            k -= 1
        if k < 1:
            return None
        a_lo, a_hi = al[k - 1], al[k]
        if a_hi - a_lo < 1e-6:
            return None
        # crossing in pixel-CENTRE coords; +0.5 puts it on the row-BOUNDARY datum `sub_top`
        # and `red_top_coarse` are expressed in.
        e = lo + (k - 1) + (0.5 - a_lo) / (a_hi - a_lo) + 0.5
        if not (t - 3.0 <= e <= t + 2.0):     # never let it wander off the coarse boundary
            return None
        return float(e)

    # -- STAGE 3: FULL-TRACK-anchored fill. Returns (coarse, subpix, trackbox, top_row, green). --
    def _read_fill(self, frame, col, probe: bool = False):
        # EPOCH-2: `probe` marks a SPECULATIVE read (a candidate that may be REJECTED moments
        # later). A probe reads but never WRITES the long-lived histories -- _track_h_hist,
        # _track_w_hist, _low_cand_hist, _gz_starts -- so a steal that is rejected cannot leave a
        # poisoned denominator (or a green-zone sample harvested off a column we never adopted)
        # behind. _probe_read is the instance-level twin of the kwarg: it lets a SUBCLASS override
        # with the shipped (frame, col) signature inherit the suppression through its super() call.
        probe = bool(probe) or bool(self._probe_read)
        if not probe:
            self._read_bail = None       # EPOCH-3/5: per-frame "do not emit this number" flag
            self._ro_frame = None        # RELEASE ORACLE: per-frame settle measurement
        self._pre_hug_tbox = None        # BOX-HUG: per-call, set only on the full geometry path
        self._hug_stroke = None          # BOX-TIGHT mode 2: per-call twin of the above
        strip, x0, top_search = self._fill_strip(frame, col)
        ph, pw = strip.shape[:2]
        if ph < 8 or pw < 3:
            return 0.0, 0.0, (x0, top_search, pw, ph), -1, None
        red = self._redmask(strip)                                # EXACT BGR red
        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        grn = self._greenmask(hsv)
        red_row = red.mean(axis=1) / 255.0
        grn_row = grn.mean(axis=1) / 255.0
        # SILVER/white chevron cap = bright, low-saturation rows. The Arrow2 arrow-tip apex
        # (the â–² that is the meter's TRUE top) is this silver chevron sitting DIRECTLY above the
        # green make-window; the topmost GREEN row lands ~6px BELOW the apex, so the box must
        # extend up through this silver run to sit OVER the tip (not clip it). Measured over the
        # interior columns so the +-3px pads and the (saturated) blue court can't leak in.
        _iw = slice(3, pw - 3) if pw > 6 else slice(0, pw)
        silver_row = ((hsv[:, _iw, 2] > 140) & (hsv[:, _iw, 1] < 90)).mean(axis=1)
        # === RED FILL: the top of the contiguous red run that reaches the track FLOOR (strip bottom).
        #     Taking the bottom-anchored run (not merely the topmost red row) ignores stray red high
        #     in the window and, on a deflate/release frame, tracks the RECEDING boundary. ===
        ridx = np.flatnonzero(red_row >= self.params.red_row_thr)
        red_top_coarse = None
        if ridx.size:
            rset = set(int(v) for v in ridx.tolist())
            rt = int(ridx[-1])
            while (rt - 1) in rset or (rt - 2) in rset or (rt - 3) in rset:   # bridge <=3px chevron/notch
                rt -= 1
            red_top_coarse = rt
        # === FULL-TRACK TOP: the top of the TOP-MOST plausible GREEN run that lies ABOVE the red
        #     fill. The green make-window cap chevron is present at the track TOP at EVERY fill
        #     level, so its TOP row anchors the FULL track (bottom chevron -> top cap). Anchoring
        #     here (not on the green BOTTOM) is what fixes the false-100%: the old green-bottom
        #     anchor collapsed onto the fill boundary whenever the big "make" region or a court
        #     reflection appeared, shrinking the denominator so a half-full meter read 100%.
        #     Excluding green BELOW the fill drops court reflections/chroma-bleed. Picking the
        #     TOP-MOST run (occl on), not the LONGEST, is what stops a lower/longer make-flash or
        #     reflection green run from beating the faint TOP cap -> otherwise track_top is pulled
        #     DOWN and the box is truncated (short span). A >=2px floor skips the tiny DETACHED
        #     arrow-tip speck that floats above a dark gap; fall back to longest if none clear it. ===
        ref = red_top_coarse if red_top_coarse is not None else ph
        gidx = np.flatnonzero(grn_row >= self.params.green_row_thr)
        gabove = gidx[gidx < ref]
        track_top = cap_top = cap_bot = None
        apex_top = None            # FIX-1: the apex row the chevron walk ACTUALLY detected
        full_cap = False
        if gabove.size:
            runs, run = [], [int(gabove[0])]
            for v in gabove[1:].tolist():
                if v - run[-1] <= 4:                              # bridge <=4px anti-alias gaps
                    run.append(v)
                else:
                    runs.append(run); run = [v]
            runs.append(run)
            # TOP-MOST vs LONGEST only MATTERS when there is genuine ambiguity (a lower/longer
            # make-flash or reflection run competing with the faint TOP cap run). With a SINGLE
            # green run above the fill (the common case) topmost == longest, so this is byte-
            # identical to the shipped longest-run pick -- the pristine read is untouched.
            if self._occl and len(runs) > 1:
                plausible = [rn for rn in runs if len(rn) >= 2]   # gabove ascending -> [0] is topmost
                best = plausible[0] if plausible else max(runs, key=len)
                full_cap = bool(plausible)
            else:
                best = max(runs, key=len)
                # EPOCH-1: the >=2px run-plausibility floor applies to the SINGLE-run branch too.
                # A lone >=green_row_thr row (jersey, logo, MAKE-flash) is not a cap; calling it a
                # full cap made `cand` jump to ~the whole strip, which ALWAYS beat the 0.90*max
                # seed gate below -> the poisoned denominator then under-read every later shot.
                full_cap = len(best) >= 2
            cap_top, cap_bot = best[0], best[-1]
            # --- ARROW-TIP APEX: cap_top is the top of the GREEN make-window, which sits a few px
            #     BELOW the meter's true top (the silver chevron â–²). Walk UP from cap_top through the
            #     silver chevron cap so track_top lands ON the apex -> the box sits fully OVER the
            #     meter and never clips the tip. Bounded (<=12px, the chevron is ~6-8px) and gap-
            #     bridged (the 1px apex point) so it can't run into dÃ©cor/ceiling above the gap; it
            #     stops at the first background row. cap_top/cap_bot stay the GREEN cap (the make-
            #     window band in _green_window is green-only), only the box/denominator top moves. ---
            # EPOCH-4: a 0.03 silver fraction over ~28 interior columns is satisfied by ONE bright
            # low-saturation pixel per row -- background specular/anti-alias noise clears it, so the
            # walk could drag the apex a full 12px into the BACKGROUND. That both misplaces the box
            # top the bot aims at (TIP_TIGHT is default-ON) and feeds those 12px into the long-lived
            # height history. 0.30 demands a real chevron ROW (>=30% of the interior columns lit).
            # A >=2-CONSECUTIVE-ROW requirement was also tried and REVERTED: an arrow tip is
            # genuinely a 1-row feature, so refusing the topmost single row made track_top bimodal
            # frame-to-frame and cost mid_rise_glitches (1, floor 0) on session_20260704_210801
            # under the live flag set. The row-density threshold is the noise guard on its own.
            apex_top = cap_top
            gap = 0
            rr = cap_top
            while rr - 1 >= max(0, cap_top - 12):
                rr -= 1
                if silver_row[rr] >= 0.30 or grn_row[rr] >= self.params.green_row_thr:
                    apex_top = rr; gap = 0
                else:
                    gap += 1
                    if gap > 2:
                        break
            track_top = apex_top
        # smoothed FULL-track height: the denominator + box height must be the FULL track (cap ->
        # floor) at EVERY fill level. The green make-window DESCENDS with the receding red on a
        # deflate tail, so seeding the history every frame lets that shrinking cap drag the median
        # DOWN -> a truncated box + inflated deflate fill. Seed the full-track estimate ONLY from a
        # near-FULL cap (cand within 90% of the running max, occl): a descending deflate green does
        # NOT update it, so the box stays full-span and the deflate fill reads on the true scale.
        # (Shipped behaviour == seed every real-cap frame -> preserved when occl is OFF, and on the
        # synthetic fixed-height-cap frames it is identical since cand never exceeds the max.)
        green_px = int(gabove.size)
        if track_top is not None:
            cand = float(ph - track_top)
            # EPOCH-1: PLAUSIBILITY of the candidate FULL-track height, in the same scaled units
            # every other geometry prior uses. The tip sits TIP_UP_MIN..TIP_UP_MAX above the floor,
            # so a genuine full track lies inside [0.6, 1.6] x TIP_UP_MAX*sy. A candidate outside
            # that is a speck / dÃ©cor / flash artefact: it may still be READ this frame, but it is
            # never allowed into the LONG-LIVED denominator history (nor into the A1 recovery run),
            # so one bad frame in shot N cannot tax shot N+1.
            cand_ok = (0.6 * self._tip_up_max) <= cand <= (1.6 * self._tip_up_max)
            hist = self._track_h_hist
            rebase_seed = False
            if not self._occl:
                seed = full_cap or not hist
            elif not hist:
                seed = True
            else:
                seed = full_cap and cand >= 0.90 * float(max(hist[-8:]))
                # A4 (ORION_READER_TRACK_H_CAP): the HIGH half the gate above never had. A
                # candidate TALLER than the established track is admitted unconditionally today,
                # so one inflated measurement -- the arrow-tip apex walk chaining background rows
                # above the green cap, its budget is 12px -- enters the history, drags the
                # 5-sample median UP, and every fill for the rest of that lock is divided by a
                # track ~10% too tall. That is a CONSTANT ~9pp under-read across the shot: a
                # per-shot correlated, horizon-flat observation bias, invisible to frame
                # averaging. Refuse it, and record it for the recovery run below so a REAL scale
                # change (or a cold history seeded off one short outlier) can still be adopted.
                high_refused = False
                if (self._track_h_cap and seed
                        and cand > (1.0 + self._track_h_cap_tol)
                                   * float(np.median(hist[-5:]))):
                    seed = False
                    high_refused = True
                # A1 (ORION_READER_SCALE_RESET): TWO-SIDED seed gate. The 0.90*max gate above is
                # ONE-WAY -- a single tall outlier (dÃ©cor false-lock / green reflection / momentary
                # zoom-in) poisons max(hist), then every TRUE full-cap height below 90% of it is
                # refused FOREVER and the fill under-reads for the rest of the session ("stops
                # timing"). Recovery: collect the REFUSED full-cap candidates; once M consecutive
                # ones are mutually CONSISTENT (spread <= 10%) AND FLAT (no monotone drift -- a
                # DEFLATE tail descends a few px per frame, a genuinely-shorter meter is stable)
                # AND plausibly full-track (>= 50% of the running max: an end-of-deflate stub
                # hovering near the floor can never qualify), ADMIT the lower candidate into the
                # history (a normal seed, run kept alive) so the 5-sample median steps down
                # GRADUALLY and the stale max ages out of the 8-sample gate window -- no wholesale
                # replacement (a replace-style rebase flapped the median against a re-seeded tall
                # sample -> a >20pp mid-rise fill glitch on the regression gates). A single low
                # outlier can never qualify (needs M consistent frames) and any NORMAL accepted
                # seed clears the run, so nominal jitter never triggers it. (First offline A/B
                # caught the unguarded version rebasing on deflate tails -> false-100 peaks; the
                # flatness + 0.5*max guards are what block that while keeping the recovery.)
                # A4 HIGH-side recovery, mirroring A1's low-side one but deliberately STRICTER
                # (a longer run, a tighter spread): admitting a tall candidate too eagerly
                # re-poisons the very denominator this gate exists to protect. A genuinely
                # taller full track is the meter's fixed structure and is therefore present on
                # EVERY frame of its life, so it produces a long, consistent, FLAT run; the
                # apex-walk artefact measured on session_20260804_032333 lasted 5 and 7 frames.
                if high_refused and cand_ok and not probe:
                    hch = self._high_cand_hist
                    hch.append(cand)
                    if len(hch) > self._track_h_cap_m:
                        del hch[0]
                    if (len(hch) >= self._track_h_cap_m
                            and max(hch) - min(hch) <= 0.05 * max(hch)
                            and abs(hch[-1] - hch[0]) <= max(2.0, 0.02 * max(hch))):
                        seed = True          # the taller track is real -- adopt it
                        rebase_seed = True   # keep the run alive: recovery continues next frame
                # A4: a candidate refused for being too TALL must never be re-admitted through
                # A1's LOW-side recovery (it clears `cand >= 0.50*max` trivially), or the
                # ceiling would be a 4-frame speed bump instead of a gate.
                if (self._scale_reset and full_cap and not seed and cand_ok and not probe
                        and not high_refused
                        and cand >= 0.50 * float(max(hist[-8:]))):
                    lch = self._low_cand_hist
                    lch.append(cand)
                    if len(lch) > self._scale_reset_m:
                        del lch[0]
                    if (len(lch) >= self._scale_reset_m
                            and max(lch) - min(lch) <= 0.10 * max(lch)
                            and abs(lch[-1] - lch[0]) <= max(3.0, 0.03 * max(lch))):
                        seed = True
                        rebase_seed = True   # keep the run alive: recovery continues next frame
            if seed and cand_ok and not probe:
                if self._scale_reset and not rebase_seed:
                    self._low_cand_hist = []
                if self._track_h_cap and not rebase_seed:
                    # A4: any NORMALLY accepted seed proves the current track is the live one,
                    # so the tall-candidate run is stale -- clear it (mirrors the low side).
                    self._high_cand_hist = []
                self._ever_capped = True     # EPOCH-3: a real full track has now been measured
                hist.append(cand)
                if len(hist) > 12:
                    self._track_h_hist = hist = hist[-12:]
                # B9b: the SESSION-scoped twin, written only by locks the micro tier did not
                # seat. `_track_h_hist` is wiped per lock (and a micro decor lock, latching
                # `_MICRO_GREEN` as its cap band, would otherwise seed the very measurement
                # that is supposed to disqualify it).
                if self._courtwide_acquire_tier != "micro_compressed_cap_first":
                    self._sess_track_h.append(cand)
                    if len(self._sess_track_h) > 24:
                        del self._sess_track_h[:-24]
            fillable_h = float(np.median(hist[-5:])) if hist else cand
        elif self._track_h_hist:
            fillable_h = float(np.median(self._track_h_hist[-5:]))
        elif self._sess_track_h and self._sess_track_h_is_tight():
            # [ORION_SESS_TRACK_FALLBACK] The per-lock history is empty but the SESSION-scoped
            # twin holds real measurements. Prefer a measured denominator over refusing the frame.
            #
            # WHY THIS EXISTS. Owner-confirmed second failure mode: "for some the meter detector
            # didnt pick up the meter". Measured on the verification session, `no_cap_anchor` is
            # 875 of 4637 undetected frames -- the reader had a meter in front of it, saw no green
            # cap, found `_track_h_hist` empty, and bailed. The engine cannot time a shot it never
            # sees, so those presses die as silently as the structure-proof case did.
            #
            # WHY IT IS GATED RATHER THAN JUST USED. A wrong denominator is a wrong fill and the
            # bot times off fill, so this must not become a confident guess. `_sess_track_h` is
            # session-scoped, so it can legitimately hold heights from a DIFFERENT camera
            # distance -- and the meter does render at different sizes (measured boxes of 16x108
            # and 23x115 in the same batch). A session median taken across mixed distances would
            # be biased.
            #
            # THE HONEST LIMIT: I could not measure the half-court track height, because
            # half-court shots are precisely the ones that never get detected. So instead of
            # asserting the median is safe, `_sess_track_h_is_tight()` VALIDATES IT AT USE TIME:
            # the fallback is only taken when the session's own measurements agree with each other
            # closely enough that their median cannot be far wrong. A session spanning genuinely
            # different meter scales fails that test and bails exactly as before.
            fillable_h = float(np.median(self._sess_track_h[-5:]))
        else:
            # EPOCH-3: no cap anchor THIS frame and no history to fall back on. The raw strip
            # height is NOT a denominator -- it is the SEARCH WINDOW (~230-243px vs a true track
            # of ~160), so dividing by it under-reads the fill ~30% AND places the emitted box top
            # ~70px above the real tip. Use the GEOMETRIC PRIOR (mid-point of the scaled
            # tip-above-floor range = a nominal full track) and FLAG the frame: read() emits a
            # clean not-detected rather than a confidently-wrong number the bot would time off.
            # Reachable on the first lock after reset_tracking() (live colour/style change).
            fillable_h = 0.5 * float(self._tip_up_min + self._tip_up_max)
            if not probe and self._ever_capped:
                # ...and if this reader HAS measured a real track before (so the empty history is a
                # reset_tracking() wipe, not a cold start), refuse to emit the guess at all. A cold
                # reader keeps the shipped contract: green-absent frames stay FED with the
                # 'green_not_found' sentinels so the engine can still time the tip.
                self._read_bail = "no_cap_anchor"
        # EPOCH-6: the 230px denominator ceiling is an @1080p prior too -- scale it (identity at
        # 1080p) or it caps the track below its true height at 1440p/4K -> the fill over-reads.
        _sy = self.H / self._REF_H if self.H else 1.0
        fillable_h = float(min(max(fillable_h, 20.0),
                               float(min(ph - 2, max(40, int(round(230 * _sy)))))))
        # ph IS `min(self.H, floor + 3) - top_search` -- the strip is cut to exactly that.
        if not probe and float(ph) < fillable_h + 4.0:
            # EPOCH-5: the STRIP is shorter than the track we are dividing by -- the box floor ran
            # past the frame bottom (or the band ceiling), _fill_strip clipped the pixels, and the
            # clamp above just squashed the denominator to fit. The resulting % is measured on a
            # truncated track (a 20-27pp under-read), so it must not be emitted as a fresh read;
            # read() holds the last good value instead.
            self._read_bail = "strip_truncated"
        track_top = max(0, ph - int(round(fillable_h)))
        clamp = lambda v: max(0.0, min(100.0, v))
        # N3: steady the EMITTED box width with a 5-sample median. The raw contour width (pw) is
        # re-measured per frame and jitters; the height is already median-steadied by _track_h_hist.
        # Only the reported box width is smoothed -- pw still drives the masks above and the scale
        # estimate (which reads col[2] directly), so the fill read is byte-identical.
        if self._anchor_n3:
            wh = self._track_w_hist
            if not probe:                              # EPOCH-2: no history write on a probe
                wh.append(float(pw))
                if len(wh) > 12:
                    self._track_w_hist = wh = wh[-12:]
            box_w = int(round(float(np.median(wh[-5:])))) if wh else pw
        else:
            box_w = pw
        trackbox = (x0, top_search + track_top, box_w, int(round(fillable_h)))
        # TIP-CAPTURE GUARANTEE: raise the box top to cover the arrow-tip apex above the green
        # cap (never clip the tip). Decoupled from the fill scale -> fill %% unchanged.
        trackbox = self._apply_tip_lift(trackbox, cap_top, top_search, apex_top)
        # FULL-STRUCTURE PRESENTATION GUARANTEE: the cap/chevron can flare wider than the
        # red column, most visibly on a small half-court Go-To.  Enclose only connected
        # structure in a bounded ROI.  This remains output-only: every fill value above was
        # already computed from the original strip, masks and denominator.
        trackbox = self._enclose_meter_bbox(
            frame, col, trackbox,
            top_search + int(cap_top) if cap_top is not None else None,
            top_search + int(cap_bot) if cap_bot is not None else None)
        # (that call also applies the OUTLINE hug -- silver frame + BOTH arrow caps -- which the
        # connected-component union structurally cannot reach; see _enclose_meter_bbox.)
        # FIX-1 observability (attribute only, never emitted): absolute cap/apex rows vs the
        # emitted box top, for the box_excess_headroom measure + tests.
        self._tip_dbg = (top_search + int(cap_top) if cap_top is not None else -1,
                         top_search + int(apex_top) if apex_top is not None else -1,
                         int(trackbox[1]))
        green = self._green_window(green_px, cap_top, cap_bot, ph, fillable_h)
        # NO red this frame -> fill 0 but a COHERENT full-track box (green-only hold done upstream).
        if red_top_coarse is None:
            if (self._gz_window or self._gz_grade) and not probe:   # EPOCH-2: no _gz_starts write
                # no red -> no un-occluded sample (the grader's expose rule needs the red top),
                # but a window estimated earlier in the shot still overrides the emission.
                green = self._gz_process(hsv, None, ph, fillable_h, green)
            return 0.0, 0.0, trackbox, -1, green
        red_top_coarse = max(track_top, int(red_top_coarse))
        # --- red extent (occlusion-tolerant percentile estimate) ---
        # N5: gated on ROBUST *or* the granular ORION_READER_PCTL_FILL split-out flag, so the
        # occlusion-tolerant percentile READ can ship on its own (default OFF -> byte-identical).
        occl_frac = 0.0
        if self._robust or self._pctl_fill:
            # B2 occlusion-tolerant read: per-column top-of-red with run-length solidity, estimate =
            # the 20th PERCENTILE of column tops (definition-preserving vs the row-mean crossing;
            # survives ~2/3 column occlusion). Interior columns only (the +-3px pads excluded).
            seg = red[track_top:, :] > 0
            if seg.shape[0] >= 4 and pw > 6:
                solid = seg[:-2, :] & seg[1:-1, :] & seg[2:, :]    # 3-row solidity run
                has = solid.any(axis=0)
                tops = solid.argmax(axis=0)
                inter = slice(3, pw - 3)
                has_i = has[inter]
                n_int = int(has_i.size)
                if n_int > 0 and int(has_i.sum()) >= max(2, int(0.20 * n_int)):
                    tops_i = tops[inter][has_i]
                    est = int(round(float(np.percentile(tops_i, 20, method="lower"))))
                    red_top_coarse = track_top + est
                    bad = (~has_i).sum() + int((tops_i > est + 6).sum())
                    occl_frac = float(bad) / float(n_int)
        self._last_occl_frac = occl_frac
        if (self._gz_window or self._gz_grade) and not probe:       # EPOCH-2: no _gz_starts write
            # GREEN-ZONE: harvest the neon band's lower edge (un-occluded frames only) and,
            # when the window flag is on, emit the colour-derived [g_lo, 100] window.
            green = self._gz_process(hsv, int(red_top_coarse), ph, fillable_h, green)
        # RELEASE ORACLE (diagnostic; see __init__): this frame's settled landmark pair.
        if self._ro_on and not probe and self._ro_pending:
            self._ro_frame = self._ro_measure(hsv, ph, pw, track_top, fillable_h, _sy)
        coarse = (ph - red_top_coarse) / fillable_h * 100.0
        # SUB-PIXEL fill = smoothed threshold crossing of the red profile (Orion's genuine add)
        sm = np.convolve(red_row, np.array([0.25, 0.5, 0.25]), mode="same")
        thr = self.params.red_row_thr
        t = int(red_top_coarse)
        if t > 0 and sm[t] > sm[t - 1]:
            frac = (thr - sm[t - 1]) / max(1e-6, sm[t] - sm[t - 1])
            sub_top = t - (1.0 - min(1.0, max(0.0, frac)))
        else:
            sub_top = float(t)
        if self._subpix_edge:
            # ORION_READER_SUBPIX_EDGE: prefer the true 50%-coverage boundary over the
            # threshold crossing above. Falls back to `sub_top` whenever the ramp is not
            # cleanly resolvable, so the flag can only ever REPLACE a measurable edge.
            _e = self._subpix_edge_row(strip, t)
            if _e is not None:
                sub_top = _e
        subpix = (ph - sub_top) / fillable_h * 100.0
        top_row = top_search + red_top_coarse
        # F3-1 (ORION_READER_FILL_OVERFLOW, default ON) -- OBSERVABILITY ONLY.
        # `clamp` bounds the emitted fill to [0, 100], which makes "no shot ever overshot 100"
        # a TAUTOLOGY rather than a measurement: an over-read is indistinguishable from a real
        # pinned-at-tip read once both emit 100.00. The clamp itself is DELIBERATELY KEPT --
        # downstream consumers (native greenTracker / fill_kalman / the QML ring) rely on the
        # bounded range -- so record the PRE-clamp values and an overflow flag alongside it.
        # Attribute-only: the returned tuple is byte-identical with the flag on or off.
        if self._fill_overflow_diag and not probe:
            self._fill_raw = (float(coarse), float(subpix))
            self._fill_overflow = int(coarse > 100.0 or subpix > 100.0)
            self._fill_underflow = int(coarse < 0.0 or subpix < 0.0)
            if self._fill_overflow:
                self._fill_overflow_n += 1
        return round(clamp(coarse), 2), round(clamp(subpix), 2), trackbox, int(top_row), green

    # ------------------------------------------------------------------ #
    #  wall-time velocity (mirrors chain _MotionEstimator: LS slope over a
    #  rolling window of (fill, wall-time) samples, clamp +/-500 %/s).
    # ------------------------------------------------------------------ #
    def _update_velocity(self, fill: float, ts: float):
        self._vel_hist.append((float(ts), float(fill)))
        if len(self._vel_hist) < 2:
            self._velocity = 0.0; self._accel = 0.0
            return
        tm = np.array([s[0] for s in self._vel_hist], dtype=np.float64)
        fp = np.array([s[1] for s in self._vel_hist], dtype=np.float64)
        tm = tm - tm[-1]
        if float(np.ptp(tm)) <= 1e-6:
            self._velocity = 0.0; self._accel = 0.0
            return
        try:
            slope = float(np.polyfit(tm, fp, 1)[0])
        except Exception:
            slope = 0.0
        vel = max(-self.VEL_MAX, min(self.VEL_MAX, slope))
        if self._prev_vel_ts is not None and ts > self._prev_vel_ts:
            self._accel = max(-2000.0, min(2000.0, (vel - self._prev_vel) / (ts - self._prev_vel_ts)))
        else:
            self._accel = 0.0
        self._velocity = vel
        self._prev_vel = vel
        self._prev_vel_ts = ts

    def _rise_state(self, fill: float) -> str:
        self._peak_fill = max(self._peak_fill, fill)
        if self._velocity > 25.0:
            return "rising"
        if fill >= 85.0 and abs(self._velocity) <= 25.0:
            return "peak"
        if self._peak_fill >= 70.0 and fill < self._peak_fill - 8.0:
            return "spent"
        return ""

    def _non_physical_fill(self, fill: float) -> bool:
        """B3 (ORION_READER_VAR_BREAK): does the REPORTED fill history look like something a real
        meter can physically do? Pushes `fill` onto the rolling window and returns True when this
        frame is part of a NON-PHYSICAL run (i.e. it should charge the fake-lock breaker).

        The shipped breaker counted only BYTE-frozen fills (|dfill| < 0.05). That test was written
        against offline framedumps; on a live 60fps H.264 feed a dÃ©cor blob's fill jitters by more
        than 0.05 pp EVERY frame, so the counter was perpetually reset and a MOVING false lock
        (the live "FILL 87% RISING" on the ESRB legal screen -- no meter anywhere on the frame)
        could never trip it, no matter how long it held.

        Two physical invariants replace it, both evaluated over the last `_static_win` reported
        fills (~0.33 s @60fps):
          * BOUNDED -- stddev < `_static_std_pp` (~1.5 pp). The fill wobbles but makes no net
            progress. A real meter is either climbing (a shot rises 0->~100 in 0.5-1.5 s, which
            is tens of pp of spread over this window) or it is gone within ~1-2 s.
          * OSCILLATING -- >= `_static_osc_min` steps UP and >= `_static_osc_min` steps DOWN (each
            >= `_static_osc_pp`) with |net| < half the window's range. A real rising meter never
            oscillates DOWN and a real deflate never oscillates UP, so a two-sided sawtooth with
            no net direction is a box wandering over dÃ©cor, not a fill. Requiring BOTH directions
            is what keeps a genuine monotone rise (down-steps ~0) and a genuine monotone deflate
            (up-steps ~0) out of the breaker.
        Flag OFF -> always False (the byte-frozen test alone, byte-identical to HEAD).
        """
        if not self._var_break:
            return False
        self._rep_fill_hist.append(float(fill))
        n = len(self._rep_fill_hist)
        if n < self._rep_fill_hist.maxlen:
            return False                      # judge only on a FULL window
        v = np.fromiter(self._rep_fill_hist, dtype=np.float64, count=n)
        if float(v.std()) < self._static_std_pp:
            return True                       # bounded: jittering in place, no net progress
        d = np.diff(v)
        ups = int((d >= self._static_osc_pp).sum())
        downs = int((d <= -self._static_osc_pp).sum())
        if ups < self._static_osc_min or downs < self._static_osc_min:
            return False                      # one-sided => a real rise or a real deflate
        rng = float(v.max() - v.min())
        return abs(float(v[-1] - v[0])) < 0.5 * rng   # sawtooth with no net direction

    def _note_arm_edge_reference(self, frame) -> None:
        """B7: capture the arm-edge red reference for THIS hardware epoch (see __init__).

        Taken on the first frame of a new physical epoch, i.e. at the press -- before the shot
        HUD renders (~300-400 ms later). Uses the WIDEST red band so decor is caught whatever
        band later admits it. One 1280x720 uint8 mask (~0.9 MB), computed once per shot.
        """
        if not self._arm_edge_veto:
            return
        epoch = int(self._physical_shot_epoch or 0)
        if not self._shot_armed_hw or epoch <= 0:
            return
        if self._arm_edge_mask is not None and epoch == self._arm_edge_mask_epoch:
            return                                   # already captured for this shot
        try:
            # Through `_redmask` so this honours the CONFIGURED bar colour. It used to call
            # cv2.inRange on `_MICRO_RED` directly, which meant the veto reference was a RED
            # mask no matter what colour the bar was -- it would have vetoed nothing (or the
            # wrong thing) on any non-Red meter. The MICRO row is still the right choice here:
            # it is the widest band, and the point is to catch decor whatever band later admits
            # it. Deliberately NOT `_micro_redmask`: the dominance floor narrows the mask, and
            # this reference wants maximum recall.
            self._arm_edge_mask = self._redmask(
                frame, bounds=self._bands[_mbc.BAND_MICRO])
            self._arm_edge_mask_epoch = epoch
        except Exception:
            self._arm_edge_mask = None
            self._arm_edge_mask_epoch = 0

    def _clipped_at_frame_border(self, col) -> bool:
        """B8: is this candidate CLIPPED by the frame boundary? -> it is not a shot meter.

        The park shot meter is a player-attached HUD element drawn wholly inside the frame.
        Measured over session_20260804_032333 (2513 real in-shot meter boxes across 29 shots):
        min x = 3, min y = 191, and **0.00%** touch any border. The decor locks reproduced in
        the arm-held/no-meter condition sit at x=0 and y=0 with w=6-10 (the real meter is a
        fixed-size w 16-18 / h 106-119 HUD here), i.e. they are truncated slivers of background
        architecture running off the edge of the screen.

        Cheap, stateless, and zero-cost on real meters. 0 -> disabled.
        """
        if not self._border_veto or col is None:
            return False
        try:
            x, y, w, h = (int(col[0]), int(col[1]), int(col[2]), int(col[3]))
        except (TypeError, ValueError):
            return False
        if w <= 0 or h <= 0:
            return False
        clipped = (x <= 0 or y <= 0
                   or (x + w) >= int(self.W) or (y + h) >= int(self.H))
        if not clipped:
            return False
        # A real meter CAN legitimately ride the frame edge (measured min x = 3, and the
        # served-bbox tests pin meters at col_x 3 / 1264 / 1271) -- but it arrives at its FULL
        # nominal width. Only a candidate that is BOTH clipped AND narrower than the nominal
        # width floor is a truncated sliver: the reproduced decor locks are w 6-10 against
        # `_w_min` 13 @720p, while every one of the 2513 real boxes is >= 13.
        if w >= int(self._w_min):
            return False
        self._border_vetoes += 1
        return True

    def _prior_presence_veto(self, col) -> bool:
        """B7: was this candidate's red ALREADY on screen at the press? -> it is decor.

        True == reject. Fails OPEN (False) when the veto is off, when there is no live lock
        context to test, or when no arm-edge reference exists for the current epoch.
        """
        if not self._arm_edge_veto or col is None:
            return False
        ref = self._arm_edge_mask
        if ref is None or self._arm_edge_mask_epoch != int(self._physical_shot_epoch or 0):
            return False                             # no usable reference -> never tighten
        try:
            x, y, w, h = (int(col[0]), int(col[1]), int(col[2]), int(col[3]))
            if w <= 0 or h <= 0:
                return False
            y0, y1 = max(0, y), min(ref.shape[0], y + h)
            x0, x1 = max(0, x), min(ref.shape[1], x + w)
            if y1 - y0 < 2 or x1 - x0 < 2:
                return False
            sub = ref[y0:y1, x0:x1]
            frac = cv2.countNonZero(sub) / float(sub.size)
        except Exception:
            return False
        if frac < self._arm_edge_pp:
            return False
        self._arm_edge_vetoes += 1
        return True

    def _hw_arm_grace_live(self, ts) -> bool:
        """B6: is the PHYSICAL arm still inside its bounded breaker-grace window?

        `_shot_armed_hw` alone used to grant the fake-lock breaker unconditional grace. On the
        shipped wiring that is equivalent to disabling the breaker outright (see the
        `_arm_grace_ms` comment in __init__), because detect() only reaches the breaker while
        `_shot_armed_hw` is True. Bounding the grace to `_arm_grace_ms` from the arm edge keeps
        the A2 within-shot protection (a real release/outcome freeze is a few hundred ms, deep
        inside the budget) while letting a multi-second dead hold run the counter to its cap.

        `_arm_grace_ms == 0` restores the legacy unbounded behaviour byte-for-byte.
        """
        if not self._shot_armed_hw:
            return False
        if self._arm_grace_ms <= 0.0:
            return True                       # legacy: hw arm == unbounded grace
        if ts is None or self._hw_arm_ts is None:
            return True                       # no usable clock -> never tighten
        return (ts - self._hw_arm_ts) * 1000.0 <= self._arm_grace_ms

    def _note_hw_arm_edge(self, ts) -> None:
        """Latch the wall-time of the PHYSICAL arm edge that starts a breaker-grace window.
        A new hardware epoch is a new shot and earns a fresh budget; a disarm clears it."""
        if not self._shot_armed_hw:
            self._hw_arm_ts = None
            self._hw_arm_grace_epoch = 0
            return
        epoch = int(self._physical_shot_epoch or 0)
        if self._hw_arm_ts is None or epoch != getattr(self, "_hw_arm_grace_epoch", 0):
            self._hw_arm_ts = ts if ts is not None else self._hw_arm_ts
            self._hw_arm_grace_epoch = epoch

    def _grace_live(self, ts: float) -> bool:
        """NF-2: is the shot-coast grace currently live? Flag-gated (OFF -> always False ->
        byte-identical). Wall-time mode (_shot_coast_ms > 0, the default) compares the frame ts
        against the latched deadline so the budget is fps-invariant; ms==0 falls back to the legacy
        frame counter for offline A/B. `ts` is always a float inside read() (normalised at entry)."""
        if not self._shot_coast:
            return False
        if self._shot_coast_ms > 0.0:
            return ts is not None and ts < self._grace_until_ts
        return self._rise_recent_n > 0

    # ------------------------------------------------------------------ #
    #  Track B helpers: fingerprint, hw-shot close, fit access, gates
    # ------------------------------------------------------------------ #
    def _hue_fingerprint(self, frame):
        """32-bin hue histogram of the search band (subsampled, ~0.1ms), computed once per
        hw-arm -- the arena-switch scene fingerprint (B1 staleness)."""
        try:
            x0, y0, x1, y1 = self._band
            sub = frame[y0:y1:6, x0:x1:6]
            if sub.size < 300:
                return None
            hsv = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_BGR2HSV)
            h = cv2.calcHist([hsv], [0], None, [32], [0, 180]).ravel()
            s = float(h.sum())
            return (h / s) if s > 0 else None
        except Exception:
            return None

    def _end_hw_shot(self, cal) -> None:
        """Falling hw-arm edge: corroborate (release_marker + calibrator peak >= 70 -- the B3
        proxy, no grade event crosses native->sidecar), maybe rebase the scale gates (B2, once
        per session), then run the calibrator's commit-or-discard."""
        corroborated = False
        try:
            peak = float((cal._stage or {}).get("peak", 0.0)) if cal._stage is not None else 0.0
            corroborated = bool(self._shot_release_seen) and peak >= 70.0
        except Exception:
            pass
        self._maybe_rebase_scale(corroborated)
        cal.end_shot()
        self._apply_baked_bands()

    def _maybe_rebase_scale(self, corroborated: bool) -> None:
        """B2 scale: re-derive the contour gates as RATIOS of the locked meter dims, rebased
        ONCE per session from >=30 locked red frames inside a corroborated shot. Cold acquire
        then uses the measured ratios; ARMED acquire uses the seed-union (never narrower than
        the shipped gates while the bot is provably shooting)."""
        if not self._robust or self._dims_rebased or not corroborated:
            return
        if len(self._dims_samples) < 30:
            return
        try:
            ws = float(np.median([d[0] for d in self._dims_samples]))
            hs = float(np.median([d[1] for d in self._dims_samples]))
            seed_w = 0.5 * (self._w_min + self._w_max)
            ws = min(max(ws, 0.85 * seed_w), 1.15 * seed_w)   # +-15%-around-seed clamp (B3)
            self._meas_w_min = max(3, int(round(0.70 * ws)))
            self._meas_w_max = max(self._meas_w_min + 1, int(round(1.30 * ws)))
            self._meas_h_max = max(self._h_acq + 1, int(round(1.10 * hs)))
            self._meas_g_area = max(20, int(round(7.0 * ws)))
            self._dims_rebased = True
            _cal_logger.warning("scale rebase: w_base=%.1f track_base=%.1f -> w[%d,%d] h_max=%d "
                                "g_area=%d (n=%d)", ws, hs, self._meas_w_min, self._meas_w_max,
                                self._meas_h_max, self._meas_g_area, len(self._dims_samples))
        except Exception:
            pass
        finally:
            self._dims_samples = []

    def _fit_state(self, ts: float) -> Optional[dict]:
        """Latest registration-fit prediction at capture time ts (seconds) via the wired
        provider (RegistrationPredictor.predict_fill, epoch-ms domain). None when absent."""
        if (self._meter_detector is not None
                or self._fill_estimator_identity is not None):
            # This hook belongs to CLASSICAL read(): its contour/green-cap ruler
            # is not the direct-box ruler used to train the registration fit.
            # detect() measures/stamps that direct ruler only AFTER read(), so
            # comparing the previous stamp here cannot prove this frame matches.
            # A detector miss must not leak its old fit into classical coast or
            # trajectory gating either. Keep the boundary for the whole detector
            # epoch, and for any stamped direct ruler until reset_tracking clears
            # it. Classical pixel/rate checks and native direct-box fill remain.
            return None
        if self._fit_provider is None:
            return None
        try:
            return self._fit_provider(float(ts) * 1000.0)
        except Exception:
            return None

    def _trajectory_gate(self, fill: float, ts: float) -> bool:
        """B2: True -> REJECT this fill sample (emitted as 'fill_gated'). Fit-based tau =
        max(3*sigma_unw, 0.75*|v_pred|*16.7ms + 2pp) floored 3.5pp; pre-fit falls back to a
        rate gate measured from the last ACCEPTED sample (dup-preceded double-steps pass:
        the dt doubles with the dup, so the allowance doubles). 6 consecutive rejections ->
        accept + reset (the world moved, the fit was wrong)."""
        last = self._traj_last_acc
        fs = self._fit_state(ts)
        if fs is not None and fs.get("n", 0) >= 6:
            tau = max(3.0 * float(fs.get("sigma_pp", 0.0)),
                      0.75 * abs(float(fs.get("vel_pp_ms", 0.0))) * 16.7 + 2.0,
                      3.5)
            dev = abs(fill - float(fs.get("fill", fill)))
        elif last is not None:
            dt_ms = max(1.0, (ts - last[0]) * 1000.0)
            tau = 0.75 * dt_ms + 3.5          # ~750%/s rate allowance + floor
            dev = abs(fill - last[1])
        else:
            self._traj_last_acc = (ts, fill)
            return False
        if dev > tau:
            self._traj_rejects += 1
            if self._traj_rejects > 6:
                self._traj_rejects = 0
                self._traj_last_acc = (ts, fill)
                return False
            return True
        self._traj_rejects = 0
        self._traj_last_acc = (ts, fill)
        return False

    # ------------------------------------------------------------------ #
    #  THE ENTIRE READER -> rich dict (also drives the head-to-head harness)
    # ------------------------------------------------------------------ #
    def read(self, frame, ts: Optional[float] = None) -> dict:
        if ts is None:
            ts = _time.perf_counter()
        self._prepare_frame_geometry(frame)
        if self._perf:
            # S2: the band-mask memo is strictly per-frame (a stale mask from a previous
            # frame must never be served, e.g. when the probation cooldown skips the cold
            # scans and _acquire_structure runs first) -- clear it before any scan.
            self._scan_raw_key = None
            self._scan_raw = None
        armed = self._armed()
        self._armed_now_cached = armed
        hw = self._shot_armed_hw
        if self._prob_refuse_n > 0:      # N4: decay the post-no_rise cold-acquire refuse cooldown
            self._prob_refuse_n -= 1
        if self._rise_recent_n > 0:      # N6: decay the shot-coast grace (re-latched on a rising read)
            self._rise_recent_n -= 1
        if self._stalebreak and self._fresh_rise_left > 0:
            # A2: decay the "rising" freshness window (re-latched on a FRESH rising RED read)
            self._fresh_rise_left -= 1
        # B1: is the widened in-shot occlusion window live this frame? (computed once; also
        # consumed inside _relocate where ts is not plumbed). Flag OFF -> always False.
        self._occl_wide_live = bool(self._occl_wide and ts < self._occl_wide_until)
        if self._vzoom and self.box is None and self._vz_ref is not None:
            # B2: age the last-lock vertical reference while UN-locked; past the re-acquire
            # window the cold-acquire band snaps back to the nominal prior.
            self._vz_ref_age += 1
        if self._scale_guard and self._scale_last_ts is not None \
                and self._size_base > 0 and self._scale_est != 1.0 \
                and ts - self._scale_last_ts > self._scale_guard_decay_s:
            # A3: no GUARD-qualified fresh sample for a while -> the widened gates must not
            # stay ratcheted open; relax the EMA back toward nominal (per-frame, ~1s constant).
            self._scale_est += (1.0 - self._scale_est) * 0.02
            if abs(self._scale_est - 1.0) < 1e-3:
                self._scale_est = 1.0
        cal = self._calibrator
        # ColorCalibrator shot lifecycle rides the HARDWARE arm edges ONLY (B1 [fix]: the CV
        # self-arm refreshes the merged gate but can never open a training window).
        if cal is not None:
            try:
                if hw and not self._prev_hw_armed:
                    self._shot_release_seen = False
                    self._dims_samples = []
                    cal.begin_shot(fingerprint=self._hue_fingerprint(frame))
                    self._apply_baked_bands()   # arena-switch may have swapped/reverted bands
                elif (not hw) and self._prev_hw_armed:
                    self._end_hw_shot(cal)
            except Exception:
                pass
        # GREEN-ZONE shot lifecycle rides the SAME hardware-arm edges (flag-guarded; the
        # calibrator may be absent -- the gz stack must not depend on ORION_COLOR_CAL).
        if self._gz_window or self._gz_grade:
            if hw and not self._prev_hw_armed:
                self._gz_begin_shot()
            elif (not hw) and self._prev_hw_armed:
                self._gz_end_shot("hw_disarm")
        _hw_arm_edge = bool(hw and not self._prev_hw_armed)
        if _hw_arm_edge:
            # Covers direct/legacy set_shot_state callers that do not use the explicit
            # notify_physical_shot_start hook.
            self._clear_gameplay_structure_proof()
            self._clear_micro_candidate()
            # [ORION_READER_POST_RELEASE_YIELD] a NEW physical press ends the post-release
            # window: its meter must never be yieldable off the previous shot's release.
            self._pr_release_pending = False
            self._pr_frozen_n = 0
            self._pr_prev_fill = None
        _bridge_epoch = bool(_hw_arm_edge and self._can_bridge_arm_epoch(ts))
        if self._shot_shape_lock and _hw_arm_edge:
            self._shot_shape_live = True
            self._shot_shape = ((int(self.last_tbox[2]), int(self.last_tbox[3]))
                                if _bridge_epoch and self.last_tbox and self.last_tbox[2] > 0
                                else None)
        # B6 PER-SHOT EPOCH -- rides the SAME hardware-arm edges (never the merged/CV gate: a
        # CV self-arm is exactly the signal a phantom forges, so letting it open an epoch would
        # hand the phantom the reset button). A shot is a fresh measurement: nothing a PREVIOUS
        # frame believed about WHERE the meter is, HOW FAST it was moving, or WHAT it last read
        # may straddle the boundary, because that is precisely how a dÃ©cor lock (and its frozen
        # tail) survived from one shot into the next and then fired the bot blind. Only the SLOW,
        # two-sided-validated session geometry (_track_h_hist / _track_w_hist / _fillable_hist --
        # the fill DENOMINATOR, healed in place by the A1 two-sided gate and never movable by one
        # frame) persists; the fast per-shot calibration (width baseline / zoom ref / dim rebase)
        # ends with the epoch via reset_session_scale().
        if self._shot_epoch and _bridge_epoch:
            # Same, freshly-measured meter: preserve its box/template/fill/velocity histories.
            # Only reset state whose meaning cannot cross a shot boundary.  In particular, do
            # NOT call reset_session_scale(): this is the same on-screen meter and throwing its
            # measured width/vertical reference away is what made the post-edge box breathe.
            self._epoch_bridge_active = True
            self._consec = 0
            self._peak_fill = float(self.last_fill)
            self._coast_n = 0
            self._held_run = 0
            self._dr_frames = 0
            self._traj_last_acc = None
            self._traj_rejects = 0
            self._bp_dx = self._bp_dy = 0.0
            self._bp_n = 0
            self._static_fill_n = 0
            self._rep_fill_prev = None
            self._rep_fill_hist.clear()
            self._prob_active = False
            self._prob_from_steal = False
            self._low_cand_hist = []
            self._high_cand_hist = []
            self._rise_recent_n = 0
            self._grace_until_ts = 0.0
            # A recent monotone rise plus the PHYSICAL arm is sufficient to seed the existing
            # post-disarm occlusion deadline.  A flat/phantom bridge gets no such grace.
            if self._occl_wide and self._velocity > 25.0 \
                    and self._fresh_up_n >= self._fresh_up_req():
                self._occl_wide_until = ts + self._occl_wide_ms / 1000.0
                self._occl_wide_live = True
            else:
                self._occl_wide_until = 0.0
                self._occl_wide_live = False
        elif self._shot_epoch and _hw_arm_edge:
            # ARM edge ONLY -> force a COLD re-acquire on the new shot's own evidence.
            #
            # The DISARM edge deliberately does NOT drop the lock, even though "no frozen tail
            # straddles the shot boundary" would suggest it. Live, the hardware arm releases AT
            # the shot, i.e. exactly when the shooting motion occludes the meter -- dropping the
            # lock there is the BLIND-FIRE bug the N6 shot-coast grace and the B1 widened
            # occlusion window exist to prevent (measured: session_20260717_231912 shot 1 drops
            # at occlusion frame 5), and the reader's unit suite pins that coast. The tail is
            # already bounded WITHOUT a drop -- by conf decay, the `_armed_coast_max` coast cap,
            # and the wall-time grace deadlines -- and the arm edge below guarantees the part
            # that actually matters: nothing from the previous shot can survive INTO the next.
            self.conf = 0.0
            self.box = None
            self._courtwide_lock = False
            self._courtwide_acquire_tier = ""
            self.tmpl = None
            self.tmpl_wh = None
            self._lock_red = self._lock_green = None
            self._lock_w_ref = 0.0
            self._relock_reject_n = 0
            self._consec = 0
            self._peak_fill = 0.0
            self._coast_n = 0
            self._held_run = 0
            self._dr_frames = 0
            self._traj_last_acc = None
            self._traj_rejects = 0
            self._bvx = self._bvy = 0.0
            self._bp_dx = self._bp_dy = 0.0
            self._bp_n = 0
            self._prev_cx = self._prev_cy = self._prev_floor_y = None
            self.last_fill = 0.0
            self.last_coarse = 0.0
            self.last_tbox = [0, 0, 0, 0]
            self._vel_hist.clear()
            self._velocity = 0.0
            self._accel = 0.0
            self._prev_vel = 0.0
            self._prev_vel_ts = None
            # every "the previous frame vouches for this one" budget ends with the epoch
            self._static_fill_n = 0
            self._rep_fill_prev = None
            self._rep_fill_hist.clear()
            self._fresh_up_n = 0
            self._fresh_prev_fill = None
            self._fresh_lock_ts = None
            self._fresh_lock_streak = 0
            self._epoch_bridge_active = False
            self._fresh_rise_left = 0
            self._lock_ever_rose = False      # [ORION_DEAD_HOLD_GRACE] never carry across a re-lock
            self._rise_recent_n = 0
            self._grace_until_ts = 0.0
            self._occl_wide_until = 0.0
            self._occl_wide_live = False
            self._prob_active = False
            self._prob_from_steal = False
            self._low_cand_hist = []
            self._high_cand_hist = []
            if self._shot_shape_lock:
                # The shot is real, but there was no trustworthy pre-edge lock.  Keep the shape
                # epoch active so the first fresh meter read latches w/h; no stale dimensions cross.
                self._shot_shape_live = True
                self._shot_shape = None
            # the fast per-shot calibration (width baseline / vertical-zoom ref / once-per-shot
            # dim rebase) is exactly what reset_session_scale() ends -- reuse it, don't duplicate.
            if self._shot_epoch_scale:
                self.reset_session_scale()
        self._prev_hw_armed = hw
        locked = self.conf >= self.CONF_MIN and self.box is not None
        if cal is not None and hw:
            try:
                cal.observe_shot_frame(locked)
            except Exception:
                pass
        # STAGE 2b (locked): bounded COLOUR relocate -- red column (make/miss) OR green tip
        # (cap/deflate hold). Returns a box ONLY on real meter colour, never a bare grayscale template
        # match -> a persistent grey dÃ©cor edge (window mullion) can no longer hold the lock.
        col, evidence = (self._relocate(frame) if locked else (None, None))
        # B4 (RELOCK_GEOM): a relocate hit must be GEOMETRICALLY CONSISTENT with the lock it
        # claims to continue. `_relocate` scans with far weaker gates than a cold acquire
        # (h_hold=12 / relocate_ar_min=0.35 vs h_acq=33 / ar_min=1.8) and the accept path below
        # resets conf to CONF_INIT on EVERY hit -- so once a dÃ©cor blob was locked, ANY red
        # fragment inside the window refreshed it and confidence decay was structurally
        # unreachable (the lock was immortal). The meter's COLUMN WIDTH is the fill-invariant
        # identity of the lock: it does not change as the meter fills, caps or deflates, and it
        # survives the fast horizontal fade slide that makes a position test useless (a measured
        # same-meter re-find jumped ~150 px under a camera pan). A hit outside +/-40% of the
        # acquiring width is therefore NOT this meter -- drop it to the ordinary MISS path, where
        # the shipped decay / armed-hold floor / shot-coast grace / coast-steal machinery all
        # apply unchanged. Purely subtractive: `_lock_w_ref` is 0 until a lock has been seated,
        # green-tip and NCC evidence are exempt (their boxes are synthesised, not measured), and
        # a real meter's own width always passes its own reference.
        if (self._relock_geom and col is not None and evidence == "red"
                and self._lock_w_ref > 0.0):
            _rw = float(col[2])
            if not (self._lock_w_ref * (1.0 - self._relock_w_tol) <= _rw
                    <= self._lock_w_ref * (1.0 + self._relock_w_tol)):
                self._relock_reject_n += 1
                col, evidence = None, None
            else:
                self._relock_reject_n = 0
        stage = "track" if evidence == "red" else ("track_green" if evidence == "green" else "track")
        # A2 (ORION_READER_STALEBREAK, lever ii-b -- COAST STEAL): a long coast can be a STALE
        # dead-hold pinning the lock (frozen fill served as meter_memory) while the REAL meter
        # rises somewhere the tight relocate window cannot reach -- the live "fresh=0
        # staleMem=NN" shots. Mid-shot SUPPRESSION cannot fix this without also cutting real
        # occlusion holds (gate A/B: a 7-frame within-shot gap), so instead the coast is made
        # SUPERSEDABLE: once the coast has lasted >= 8 frames, run the SAME cold acquisition
        # scan the unlocked path would run and, if a gate-passing column exists, RE-SEAT the
        # lock on it immediately (fresh evidence beats a frozen echo; per-lock tracking state
        # restarts clean). A real occlusion coast is untouched unless a column that passes the
        # strict acquire gates appears -- the exact acceptance a cold acquire would apply anyway.
        if (self._stalebreak and locked and col is None and self._coast_n >= 8):
            _hmin_s = self._h_acq_armed if armed else self._h_acq
            _armin_s = self.AR_MIN_ARMED if armed else None
            _steal, _c2 = self._scan(frame, self._band_eff(), _hmin_s, ar_min=_armin_s)
            # A2c STEAL-COURTWIDE (see flag comment): a PHYSICALLY-armed shot whose real
            # meter is below the nominal steal floor / outside the band (beyond-half-court
            # Go-To: h=4-9) may supersede a frozen echo through the SAME strict courtwide
            # structure ladder the cold acquire runs -- with the same B7/B8 vetoes. The
            # nominal steal above always wins first; unarmed/off-shot frames never enter.
            _steal_cw_tier = ""
            _steal_cw_red = _steal_cw_green = None
            if _steal is None and self._steal_courtwide and hw:
                _cw, _cw_tier, _cw_red, _cw_green = \
                    self._acquire_courtwide_structure(frame, ts=ts)
                if _cw is not None and self._clipped_at_frame_border(_cw):
                    _cw = None          # B8: clipped sliver -> background scenery
                if _cw is not None and self._prior_presence_veto(_cw):
                    _cw = None          # B7: red already on screen at the press -> decor
                # PERMANENT STEAL FORENSICS: one line on the FIRST courtwide steal search
                # of each epoch and on every seat, floored at 1/s so the native relay
                # throttle (1/s global) cannot eat the seat line behind a search line.
                _cw_now = _time.monotonic()
                if ((_cw is not None
                     or self._steal_cw_log_epoch != int(self._physical_shot_epoch or 0))
                        and _cw_now - self._steal_cw_log_last >= 1.0):
                    self._steal_cw_log_epoch = int(self._physical_shot_epoch or 0)
                    self._steal_cw_log_last = _cw_now
                    try:
                        _acq_logger.warning(
                            "METER STEAL SEARCH: scan=courtwide nominal_floor_px=%d "
                            "held_fill=%.1f coast=%d found=%d tier=%s box=%s epoch=%d",
                            int(_hmin_s), float(self.last_fill), int(self._coast_n),
                            int(_cw is not None), (_cw_tier or "-"),
                            ([int(v) for v in _cw] if _cw is not None else "-"),
                            int(self._physical_shot_epoch or 0))
                    except Exception:
                        pass
                if _cw is not None:
                    _steal = _cw
                    _steal_cw_tier = _cw_tier or "-"
                    _steal_cw_red, _steal_cw_green = _cw_red, _cw_green
            if _steal is not None:
                # RE-SEAT the lock on the stolen column and emit ONE not-detected frame. The
                # seat is a NEW lock; reporting its (lower) fresh fill on the same continuous
                # track as the frozen echo would be a >20pp backward fill step on consecutive
                # detected frames (gate A/B: mid_rise_glitches). One laundering frame resets
                # every consumer's continuity; the very next frame relocates the seated column
                # and reads it fresh with clean per-lock state.
                # SILENT_RESEAT: read the stolen column NOW (before the state reset) so the
                # same-meter test can compare its fresh fill against the coasted hold.
                _fr_sub = _fr_coarse = None; _fr_tbox = None
                if self._silent_reseat:
                    # EPOCH-2: this read is PURELY SPECULATIVE -- the steal it probes may be
                    # rejected two lines below, so it must not write _track_h_hist / _track_w_hist /
                    # _low_cand_hist / _gz_starts. Latched through _probe_read (not the kwarg) so a
                    # subclass override with the shipped (frame, col) signature still inherits the
                    # suppression via its super()._read_fill call.
                    self._probe_read = True
                    try:
                        _fr_coarse, _fr_sub, _fr_tbox, _fr_top, _fr_green = \
                            self._read_fill(frame, tuple(int(v) for v in _steal))
                    finally:
                        self._probe_read = False
                _held_fill, _held_coarse = self.last_fill, self.last_coarse
                _held_tbox = list(self.last_tbox) if self.last_tbox else [0, 0, 0, 0]
                # STEAL_PROBATION (fix 1d): a steal with NO live evidence must PROVE a rise
                # within _steal_prob_frames read frames (N4 machinery, short window) or be
                # dropped as static decor. IN-SHOT (armed/hw) / fresh-rise / grace steals are
                # EXEMPT -- exactly A2's legitimate supersession set. The merged `armed` gate
                # is part of the exempt set because a mid-shot steal is routinely a NEAR-PEAK
                # same-meter re-find (measured: session_20260707_175017 o=587, held 94.1 ->
                # fresh 89.7 capping) that structurally CANNOT rise 4pp -- probing it dropped a
                # real lock into a 14-frame mid-shot gap. A stale echo can no longer keep the
                # CV self-arm alive (A2's fresh-rise gating ships default-ON), so the armed
                # window a decor steal could hide in is bounded (~120 frames past the last
                # real rise); the off-shot falselock gate stays the arbiter. Green appearing
                # during the window exempts inside the N4 consumption block (_prob_green).
                if self._steal_probation and not (armed or hw or self._fresh_rise_left > 0
                                                  or self._grace_live(ts)):
                    self._prob_active = True; self._prob_n = 0
                    self._prob_start = None; self._prob_prev = None
                    self._prob_inc = 0; self._prob_green = False
                    self._prob_limit = self._steal_prob_frames
                    self._prob_from_steal = True
                elif self._steal_probation:
                    self._prob_active = False; self._prob_from_steal = False
                self.box = tuple(int(v) for v in _steal)
                # A nominal-band steal keeps the historical bookkeeping; an A2c courtwide
                # steal seats the same courtwide lock state the cold path would (track
                # scale, tier forensics, per-lock bands below).
                self._courtwide_lock = bool(_steal_cw_tier)
                self._courtwide_acquire_tier = _steal_cw_tier
                self.conf = self.CONF_INIT
                self._lock_w_ref = float(_steal[2])   # B4: the reseat is a NEW lock -> new width
                self._relock_reject_n = 0             #     identity for the relocate gate
                self._bvx = self._bvy = 0.0
                self._bp_dx = self._bp_dy = 0.0; self._bp_n = 0   # FIX-2: new lock, dead reckon over
                self._prev_cx = self._prev_cy = self._prev_floor_y = None
                self._vel_hist.clear(); self._velocity = 0.0; self._accel = 0.0
                self._consec = 0; self._peak_fill = 0.0
                self._coast_n = 0; self._static_fill_n = 0
                self._fresh_lock_streak = 0
                # Per-lock ACTIVE band, mirroring the cold acquire: a codec-tier courtwide
                # steal reads with the tolerant bands for the life of THIS lock; strict
                # tiers / nominal steals keep None (production band). (B7 in the cold path
                # latches exactly these carriers.)
                self._lock_red = _steal_cw_red if _steal_cw_tier else None
                self._lock_green = _steal_cw_green if _steal_cw_tier else None
                sx, sy2, sw, sh = self.box
                cr = frame[max(0, sy2):min(self.H, sy2 + sh), max(0, sx):min(self.W, sx + sw)]
                if cr.shape[0] >= 6 and cr.shape[1] >= 3:
                    self.tmpl = cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY)
                    self.tmpl_wh = (cr.shape[1], cr.shape[0])
                if (self._silent_reseat and _fr_sub is not None
                        and _held_tbox[2] > 0 and _held_fill > 0.5
                        and (_held_fill - _fr_sub) <= 20.0):
                    # SAME METER (fill-continuous: no >20pp backward step -- the exact
                    # mid_rise_glitches boundary): the coasted meter re-found after a
                    # pan/occlusion. Emit the dead-reckoned HELD box as ONE detected:True
                    # frame (reason 'held_reseat') instead of the detected:False launder --
                    # a same-meter reseat never blinks. The held state is KEPT so the next
                    # frame's fresh read continues the same track.
                    self._held_run += 1   # A2xB1: this emission is a HELD fill
                    self.last_fill, self.last_coarse = _held_fill, _held_coarse
                    self.last_tbox = list(_held_tbox)
                    self.last_debug = {"stage": "steal_reseat", "armed": armed,
                                       "held_reseat": 1, "box": list(self.box),
                                       "held": round(_held_fill, 2),
                                       "fresh": round(_fr_sub, 2)}
                    return {"detected": True, "meter_present": True, "fill": _held_fill,
                            "fill_coarse": _held_coarse, "bbox": list(_held_tbox),
                            "stage": "steal_reseat", "confidence": round(self.conf, 3),
                            "velocity_pct_s": 0.0, "rejection_reason": "held_reseat",
                            "rise_state": ""}
                # genuinely DISCONTINUOUS (>20pp backward step) or cold-hold reseat -> the
                # designed ONE-frame launder stays.
                self._held_run = 0   # A2xB1: a re-seat is fresh evidence -> held-echo run over
                if self._silent_reseat and _fr_tbox is not None and int(_fr_tbox[2]) > 0:
                    # seed the stolen column's fresh read so a relocate miss on the very next
                    # frame coasts on a REAL box/fill instead of emitting the detected:True
                    # zero-box ghost (the second ghost frame of the shipped launder).
                    self.last_fill, self.last_coarse = _fr_sub, _fr_coarse
                    self.last_tbox = self._stabilize_shot_tbox(_fr_tbox)
                else:
                    self.last_fill = 0.0; self.last_coarse = 0.0
                    self.last_tbox = [0, 0, 0, 0]
                # [ORION_READER_POST_RELEASE_YIELD] P1(b): an hw-vouched steal FROM our own
                # post-release-frozen lock (release seq relayed, held fill static near-full)
                # is the NEW shot's rise superseding the spent meter. The held-fill display
                # launder exists to keep an unknown discontinuity sampler-stale; here the
                # discontinuity's provenance is KNOWN, so serve the stolen column's FRESH
                # read immediately (the measured cost of the launder was 2 engine-stale
                # frames on the critical back-to-back path). _qualify_gameplay_sample sees
                # the pr_frozen_steal marker and skips the reverify de-authorization for the
                # same reason. FAIL-CLOSED UNCHANGED: the engine still requires its full
                # strict ownership proof (3 unique rising structure-verified frames, first
                # sight <= 40%% fill, >= 3pp rise) before this meter can own anything.
                if (self._pr_yield and hw and self._pr_release_pending
                        and _held_fill >= self._pr_yield_fill_min
                        and _fr_sub is not None and self.last_tbox[2] > 0):
                    self._pr_release_pending = False
                    self._pr_frozen_n = 0
                    self.last_debug = {"stage": "steal_reseat", "armed": armed,
                                       "pr_frozen_steal": 1, "box": list(self.box),
                                       "held": round(_held_fill, 2),
                                       "fresh": round(float(_fr_sub), 2)}
                    return {"detected": True, "meter_present": True, "fill": self.last_fill,
                            "fill_coarse": self.last_coarse, "bbox": list(self.last_tbox),
                            "stage": "steal_reseat", "confidence": round(self.conf, 3),
                            "velocity_pct_s": 0.0,
                            "rejection_reason": "green_not_found", "rise_state": ""}
                if (self._hw_reseat_continuity and hw and _held_tbox[2] > 0
                        and self.last_tbox[2] > 0):
                    # The discontinuity still must NOT feed timing as a fresh sample, but a
                    # PHYSICAL shot should never paint a zero-box frame.  Publish the newly-found
                    # position with the held fill/reason for one sampler-stale frame; the internal
                    # state is already seeded from the fresh column, so the next frame resumes raw.
                    self.last_debug = {"stage": "steal_reseat", "armed": armed,
                                       "hw_reseat": 1, "box": list(self.box),
                                       "held": round(_held_fill, 2),
                                       "fresh": round(float(_fr_sub), 2)}
                    return {"detected": True, "meter_present": True, "fill": _held_fill,
                            "fill_coarse": _held_coarse, "bbox": list(self.last_tbox),
                            "stage": "steal_reseat", "confidence": round(self.conf, 3),
                            "velocity_pct_s": 0.0, "rejection_reason": "held_reseat",
                            "rise_state": ""}
                self.last_debug = {"stage": "steal_reseat", "armed": armed,
                                   "coast_n_was": self._coast_n, "box": list(self.box)}
                return {"detected": False, "meter_present": False, "fill": 0.0,
                        "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": "steal_reseat",
                        "confidence": round(self.conf, 3), "velocity_pct_s": 0.0,
                        "rejection_reason": "steal_reseat", "rise_state": ""}
        if col is None:                                           # STAGE 2a (re)acquire
            # A coast/miss breaks the consecutive-FRESH qualification used by the arm bridge.
            # A same-frame cold acquire below starts a new streak after it is fully accepted.
            self._fresh_lock_streak = 0
            if not locked:
                # Early-rise acquisition: relax the contour-height AND aspect gate when the shot-gate
                # is armed (the genuine misses were the first 3-7 rise frames of a shot).
                hmin_a = self._h_acq_armed if armed else self._h_acq
                armin_a = self.AR_MIN_ARMED if armed else None
                acq_band = None
                structure_acq = False
                courtwide_acq = False
                courtwide_tier = ""
                courtwide_red = courtwide_green = None
                # Acquire forensics: WHICH path seated the lock and the height floor it
                # actually enforced (the observable that settles armed-vs-unarmed floors).
                acq_tier = ""
                acq_floor = 0
                # N4: while a recent no_rise drop's refuse cooldown is active, block a COLD red-scan
                # re-acquire (the same static dÃ©cor would just re-lock) -- but NEVER block a hw-armed
                # shot or the green-corroborated structure pop-in below.
                _cold_ok = not ((self._rise_probation or self._steal_probation)
                                and self._prob_refuse_n > 0 and not hw)
                # a fresh acquire always re-latches its band: stale per-lock bands (e.g. a
                # subclass zeroed the lock without the drop path) must not leak into cold scans
                self._lock_red = self._lock_green = None
                # LEARNED band first (when baked); on a miss retry the SAME FRAME with the
                # default band -- an 8-frame streak is far too slow for fade pop-ins (B1 [fix]).
                if _cold_ok and self._learned_red is not None:
                    col, _c = self._scan(frame, self._band_eff(), hmin_a, ar_min=armin_a,
                                         bounds=self._learned_red)
                    if col is not None:
                        acq_band = "learned"
                        acq_tier, acq_floor = "scan_learned", hmin_a
                        if cal is not None:
                            cal.note_learned_miss(False)
                if _cold_ok and col is None:
                    col, _c = self._scan(frame, self._band_eff(), hmin_a, ar_min=armin_a,
                                         bounds=(self._bands[_mbc.BAND_NOMINAL]
                                                 if self._learned_red is not None else None))
                    if col is not None:
                        acq_band = "default"
                        acq_tier, acq_floor = "scan_default", hmin_a
                        if self._learned_red is not None and cal is not None:
                            cal.note_learned_miss(True)   # learned missed, default saw it
                if col is None:
                    # FADE POP-IN: short stub + green-tip-dot structure corroboration (see
                    # _acquire_structure). Runs cold (no arm required) so it also covers
                    # paths with no physical arm signal (framedump replays, fades where the
                    # meter pops in fully-formed near the release). Pop-ins ride the DEFAULT
                    # band by design (plan B1: fades never contribute qualifying frames; a
                    # learned band earns nothing here).
                    col = self._acquire_structure(frame)
                    if col is not None:
                        acq_band = "default"
                        acq_tier, acq_floor = "popin_stub", self._stub_h_min
                        structure_acq = True   # green-corroborated pop-in -> probation EXEMPT
                if col is None and hw:
                    # The meter is player-attached and can first appear above,
                    # below, or beside the nominal band.  Only a PHYSICAL shot is
                    # authority to pay for a global search, and the global path
                    # accepts only compact, connected red+green meter structure;
                    # the codec tier relaxes colour floors but not geometry.  The
                    # ordinary nominal scans always run first.
                    col, courtwide_tier, courtwide_red, courtwide_green = \
                        self._acquire_courtwide_structure(frame, ts=ts)
                    if col is not None:
                        acq_band = "default"
                        acq_tier = "courtwide:%s" % (courtwide_tier or "-")
                        acq_floor = 0   # tier-specific floor; the accepted h is the datum
                        structure_acq = True
                        courtwide_acq = True
                if col is None and self._early_stub:
                    # N7 EARLY-STUB. The pop-in above floors the red stub at STUB_H_MIN=10px
                    # (~8.5% fill) because its tip test is only ">= _tip_px_min GREEN PIXELS
                    # somewhere in the tip window" -- too weak to trust a 4px stub with. The
                    # court-wide fallback already trusts a 4px stub, but only with the STRICT
                    # connected-tip proof (one compact component, size-commensurate with the
                    # stub, centred on it, inside the 110..190px-above-floor window) AND only
                    # when a PHYSICAL arm has paid for the full-frame search.
                    #
                    # RECORD CORRECTED 2026-08-03: the original justification here claimed the
                    # physical arm "never arrives in time" (inferred from the POSE ARM line
                    # landing 1 ms after ownership) so acquisition ran unarmed. Wrong command:
                    # POSE ARM is the LATER shot-begin path; the t=0 shot_gate_arm was silent
                    # but demonstrably lands (~1-2ms after the edge -- see the class-level N7
                    # comment for the log proof), so `hw`/`armed` ARE true during acquisition.
                    # The floor gap this pass closes is still real: on a nominal rise the
                    # armed scans floor at 15px, the pop-in at 10px, and the sub-10px
                    # court-wide tiers can miss their strict proofs, so the pop-in's 10px
                    # (~8.5% fill, the live first_fill=5.4 seats) wins without this pass.
                    # Measured on a real-pixel ramp (framedump shot frames re-cut to a known
                    # fill using their OWN unfilled-track pixels): pop-in floor 10px vs 2-4px
                    # on the stricter tiers.
                    #
                    # So run the STRICT tip proof COLD, in the NOMINAL BAND, at the low stub
                    # floor. This is not a widened scan: it is a NARROWER search region than
                    # the court-wide path that already accepts these floors, with the SAME
                    # (strict) corroboration, and it keeps the NOMINAL width gates
                    # (strict_scale=False -> w in _w_min.._w_max+4; no half-court 0.45x width
                    # relaxation and no perspective rescaling of the tip relation). A decor
                    # blob must therefore be meter-WIDE, 4-9px tall, >=35% red, and carry ONE
                    # compact green component of commensurate width centred directly above it
                    # at track height -- the same self-contained two-colour geometric proof the
                    # pop-in was built on, only enforced harder.
                    #
                    # Ordered LAST on purpose: every shipped path (learned scan, default scan,
                    # pop-in, and the hw court-wide fallback) is attempted first and wins first,
                    # so an ARMED frame is byte-identical to before and this can only ADD an
                    # acceptance the reader previously had to wait ~6px of fill for. The lock it
                    # seats is an ordinary nominal-band lock (courtwide_acq stays false), and it
                    # still has to earn the epoch-bound structure proof from its own green.
                    col = self._acquire_structure(
                        frame, region=self._band_eff(), strict_tip=True,
                        strict_scale=False, stub_h_min=self._stub_h_min_early)
                    if col is not None:
                        acq_band = "default"
                        acq_tier, acq_floor = "early_stub", self._stub_h_min_early
                        structure_acq = True   # green-corroborated -> probation EXEMPT
                # B7 is deliberately scoped to the COURT-WIDE / RELAXED-BAND tiers only. A cold
                # nominal scan reads the PRODUCTION red band (`_lock_red` is None until a lock
                # seats), and measured over session_20260804_032333 the production band yields
                # only 18 qualifying columns anywhere in x<280 -- every one of them the real
                # meter panning past -- versus 1583 mullion columns under `_MICRO_RED`. So the
                # decor can only enter through a relaxed-band tier, and leaving the nominal
                # path untested keeps every ordinary acquire byte-identical.
                if col is not None and self._clipped_at_frame_border(col):
                    # B8: the candidate runs off the edge of the frame -> background scenery.
                    self.last_debug = {"stage": "border_veto", "tier": acq_tier or "-",
                                       "box": [int(v) for v in col],
                                       "n": int(self._border_vetoes)}
                    col = None
                if (col is not None and courtwide_acq
                        and self._prior_presence_veto(col)):
                    # B7: this column's red was ALREADY on screen at the press -> it is decor
                    # (window mullion / signage), not a shot HUD that pops in with the shot.
                    # Drop the candidate and let the frame fall through to the ordinary miss
                    # path, so the REAL meter can seat when it renders a few frames later.
                    self.last_debug = {"stage": "arm_edge_veto", "tier": acq_tier or "-",
                                       "box": [int(v) for v in col],
                                       "n": int(self._arm_edge_vetoes)}
                    col = None
                if col is not None and self._det_only_seat_veto(col, ts):
                    # [ORION_READER_DETECTOR_ONLY_SEAT 2026-09-11] Under the landmark proposer
                    # the reader's own colour tiers seated wide false locks at the press (w 31-50
                    # px boxes on 14/98 shots in session 120503: jersey numbers, nameplates, a
                    # leftover meter) -- the ownership proof then restarted on those samples and
                    # 4 wide-open shots aborted. The CV proposer searches the whole band every
                    # frame in ~1 ms, so a real meter always has a fresh detector box: a colour
                    # candidate that no fresh detector box overlaps is not a meter.
                    self.last_debug = {"stage": "det_only_veto", "tier": acq_tier or "-",
                                       "box": [int(v) for v in col],
                                       "n": int(self._det_diag.get("det_only_veto", 0))}
                    col = None
                if col is not None:
                    evidence = "red"
                    self._courtwide_lock = bool(courtwide_acq)
                    self._courtwide_acquire_tier = courtwide_tier if courtwide_acq else ""
                    # PERMANENT ACQUIRE FORENSICS: the first accepted sample of a shot IS this
                    # lock seat, and whether it ran armed was previously unobservable (verified
                    # 2026-08-03: the t=0 shot_gate_arm is real but logged nothing anywhere).
                    # One line per armed/hw/epoch-bearing seat (~1-3 per shot: the arm edge
                    # forces a cold re-acquire); unarmed seats (menu/decor churn) are
                    # rate-limited. floor_px=0 -> tier-specific floor (see tier name); h is
                    # the accepted column height that floor admitted.
                    _acq_now = _time.monotonic()
                    if (armed or hw or self._physical_shot_epoch
                            or _acq_now - self._acq_log_last >= 2.0):
                        self._acq_log_last = _acq_now
                        try:
                            _acq_logger.warning(
                                "READER ACQUIRE: tier=%s floor_px=%d h=%d w=%d armed=%d "
                                "hw=%d epoch=%d band=%s",
                                acq_tier or "-", int(acq_floor), int(col[3]), int(col[2]),
                                int(bool(armed)), int(bool(hw)),
                                int(self._physical_shot_epoch), acq_band or "-")
                        except Exception:
                            pass
                    # B4: the ACQUIRING column's width is the lock's geometric identity -- every
                    # later relocate hit is checked against it (see RELOCK_GEOM above). Seeded
                    # here, from a column that passed the structural cold-acquire gates.
                    self._lock_w_ref = float(col[2])
                    self._relock_reject_n = 0
                    # per-lock ACTIVE band: the band that acquired the lock reads it for the
                    # lock's whole life (relocate/fill/green stay self-consistent).
                    self._lock_red = (courtwide_red if courtwide_red is not None else
                                      (self._learned_red if acq_band == "learned" else None))
                    self._lock_green = (courtwide_green if courtwide_green is not None else
                                        (self._learned_green if acq_band == "learned" else None))
                    # N4: arm rise-probation ONLY on a fresh COLD red-scan acquire (not hw-armed,
                    # not a green-corroborated structure pop-in). hw/structure acquires are
                    # latency-critical and already corroborated -> exempt (clear any stale state).
                    if self._rise_probation and not hw and not structure_acq:
                        self._prob_active = True; self._prob_n = 0
                        self._prob_start = None; self._prob_prev = None
                        self._prob_inc = 0; self._prob_green = False
                        self._prob_limit = self._prob_frames   # cold probation window
                        self._prob_from_steal = False
                    else:
                        self._prob_active = False
                stage = "acquire"
            if col is None:                                       # miss -> confidence decays, no warmup
                # N6 shot-coast grace: a lock that was RISING RED within the last _shot_coast_ms of
                # wall-time (NF-2: fps-invariant, not a frame count) stays coast-protected even after
                # the merged gate disarms (the live release occlusion) -- so a shot never fires blind
                # on a vanished box. Self-bounding via the wall-clock deadline; flag-guarded so the
                # default path is byte-identical.
                _grace = self._grace_live(ts) or self._occl_wide_live
                if locked:
                    # Slower decay WHILE ARMED (or in the shot-coast grace) -> a longer coast
                    # survives a fast-fade blur string / the release occlusion.
                    decay = self.CONF_RATE_ARMED if (armed or _grace) else self.CONF_RATE
                    if self._robust and armed and self._velocity > 25.0:
                        # B2: armed slow decay 0.004 -> 0.002, ONLY while rising with a live
                        # registration fit at conf >= 0.5 (the fit vouches the shot is mid-rise).
                        fs = self._fit_state(ts)
                        if fs is not None and fs.get("conf", 0.0) >= 0.5:
                            decay = 0.002
                    self.conf -= decay
                    # NO-DISAPPEAR-DURING-SHOT guarantee: while the shot-gate is ARMED (a shot is
                    # in progress) NEVER let the confidence decay drop the lock -- floor it at
                    # CONF_MIN so the (velocity-extrapolated) box is HELD through the whole shot.
                    # The lock is released only on a real shot END (the orchestrator disarms ->
                    # armed=False -> normal decay resumes) or the wall-clock coast cap
                    # (_armed_coast_max frames) as a stuck-armed safety. Coast-frame-count gated so
                    # the cap is honoured. No effect on present-meter frames -> byte-identical.
                    if (self._armed_hold and (armed or _grace)
                            and self._coast_n < self._armed_coast_max):
                        self.conf = max(self.conf, self.CONF_MIN)
                    # [ORION_READER_POST_RELEASE_YIELD] P1(a) force-unlock: this lock is OUR
                    # OWN released shot's meter, frozen static >=_pr_yield_fill_min for
                    # _pr_yield_frames emissions (release seq relayed -- provenance, not a
                    # heuristic), and its column just vanished. Coasting it as meter_memory
                    # only delays the next attempt's acquisition (measured ~17 fed=0 frames);
                    # drop straight to the canonical lock-drop path below so the next frame
                    # runs the ordinary armed cold acquire on the NEW rise. One yield per
                    # release; a mid-rise meter can never qualify (static + release seen).
                    if (self._pr_yield and self._pr_release_pending
                            and self._pr_frozen_n >= self._pr_yield_frames):
                        self._pr_release_pending = False
                        self._pr_frozen_n = 0
                        self._pr_yield_n += 1
                        self.conf = 0.0
                if self.conf < self.CONF_MIN:
                    # GREEN-ZONE: a real lock drop is the shot end on paths that never hw-arm
                    # (fades / framedump replays). Grade BEFORE the peak fill is zeroed.
                    if self._gz_window or self._gz_grade:
                        self._gz_end_shot("lock_drop")
                    self.conf = 0.0; self.box = None; self.tmpl = None
                    self._courtwide_lock = False
                    self._courtwide_acquire_tier = ""
                    self._consec = 0; self._peak_fill = 0.0
                    self._bvx = self._bvy = 0.0; self._prev_cx = self._prev_cy = None
                    self._bp_dx = self._bp_dy = 0.0; self._bp_n = 0   # FIX-2: lock gone
                    self._lock_w_ref = 0.0; self._relock_reject_n = 0  # B4: identity dies with it
                    self._fresh_up_n = 0; self._fresh_prev_fill = None  # B5: rise run is over
                    self._fresh_lock_ts = None; self._fresh_lock_streak = 0
                    self._epoch_bridge_active = False
                    self._visual_drop_shot_shape(
                        hw, preserve=(not self._require_gameplay_eligibility
                                      or self._gameplay_lock_authorized))
                    self._prev_floor_y = None; self._prob_active = False
                    self._lock_red = self._lock_green = None
                    self._traj_last_acc = None; self._traj_rejects = 0; self._dr_frames = 0
                    self._rise_recent_n = 0                       # N6: grace expired -> shot is over
                    if self._scale_reset:
                        # A1: a confirmed lock DROP also clears the TRANSIENT read state -- the
                        # inline drop above resets conf/box/velocity but the stale last_fill/
                        # last_tbox/_vel_hist (and the two-sided-gate run) otherwise survive into
                        # the next lock. The SCALE histories (_track_h_hist etc.) are deliberately
                        # KEPT: the two-sided seed gate heals a poisoned median in-place, while
                        # clearing them made the next lock's early no-green frames read on the
                        # degenerate ph-clamp denominator (under-read) -- gate A/B measured 4
                        # shots dropping below the finder's MIN_PEAK on session_20260707_175017
                        # plus 2 spurious off-shot decor episodes from the re-segmentation.
                        self._low_cand_hist = []
                        self._high_cand_hist = []
                        self.last_fill = 0.0; self.last_coarse = 0.0
                        self.last_tbox = [0, 0, 0, 0]
                        self._vel_hist.clear()
                    self._update_velocity(0.0, ts)
                    self.last_debug = {"stage": "no_meter", "armed": armed, "conf": 0.0}
                    return {"detected": False, "meter_present": False, "fill": 0.0, "fill_coarse": 0.0,
                            "bbox": [0, 0, 0, 0], "stage": "no_meter", "confidence": 0.0,
                            "velocity_pct_s": self._velocity, "rejection_reason": "roi_not_found",
                            "rise_state": ""}
                # coast (still >= CONF_MIN): HOLD the last good reading (mirrors loc_mem coast).
                # DEAD-RECKON the box along its last velocity so a multi-frame coast during a fast
                # slide keeps the re-lock window centred on where the meter is HEADING, not where it
                # was -- otherwise a blurred fast fade escapes the window and drops.
                self._coast_n += 1
                self._held_run += 1   # A2xB1: every coast emission is a HELD fill
                if self.box is not None and (abs(self._bvx) > 1.0 or abs(self._bvy) > 1.0):
                    bx, by, bw, bh = self.box
                    nx = int(round(bx + self._bvx)); ny = int(round(by + self._bvy))
                    # Clamp the entire dead-reckoned search anchor into this lock's tracking
                    # bounds.  Normal locks remain in the plausible player band; a structurally
                    # proven court-wide lock remains in-frame without shrinking at an edge.
                    _bnd = self._tracking_bounds()
                    nx = max(_bnd[0], min(nx, max(_bnd[0], _bnd[2] - bw)))
                    ny = max(_bnd[1], min(ny, max(_bnd[1], _bnd[3] - bh)))
                    self.box = (nx, ny, bw, bh)
                    # (b) FORWARD-PREDICT the REPORTED box too, so the DRAWN box LEADS with the
                    # meter during a coast instead of freezing at the last read position (the
                    # frozen-box lag). Bounded to the first few coast frames + clipped to the band
                    # so a long coast can't drift the drawn box onto decor; on re-lock the box is
                    # overwritten by the real read. bvx=0 (stationary) -> no shift.
                    if (self._occl and self._coast_n <= 6
                            and self.last_tbox and self.last_tbox[2]):
                        tx, ty, tw, th = self.last_tbox
                        tx = int(round(tx + self._bvx)); ty = int(round(ty + self._bvy))
                        tx = max(_bnd[0], min(tx, max(_bnd[0], _bnd[2] - tw)))
                        ty = max(_bnd[1], min(ty, max(_bnd[1], _bnd[3] - th)))
                        self.last_tbox = [tx, ty, tw, th]
                    # DECAY the coast momentum: a meter that vanished won't keep sliding at its last
                    # speed for the whole (now ~25-frame) armed coast. Bounds total drift to ~6*bvx
                    # and lets the window settle back over the last-known position (occl only).
                    if self._occl:
                        self._bvx *= 0.85; self._bvy *= 0.85
                # B2 dead-reckoned coast: with an ACTIVE registration fit fed >= 8 real samples,
                # emit the template's dead-reckoned fill instead of a flat hold -- hard-capped at
                # 10 frames, KILLED on disarm, and excluded from tip_reg/raw_fed (the sampler tier
                # reason 'dead_reckoned' feeds the engine but can never look fresh downstream).
                if (self._robust and armed and self._dr_frames < 10
                        and self._fit_provider is not None):
                    fs = self._fit_state(ts)
                    if fs is not None and fs.get("n", 0) >= 8:
                        self._dr_frames += 1
                        pred = float(min(100.0, max(self.last_fill, fs.get("fill", self.last_fill))))
                        self.last_debug = {"stage": "coast", "armed": armed,
                                           "conf": round(self.conf, 3), "dead_reckoned": 1,
                                           "dr_frames": self._dr_frames}
                        return {"detected": True, "meter_present": True, "fill": round(pred, 2),
                                "fill_coarse": round(pred, 2), "bbox": list(self.last_tbox),
                                "stage": "coast", "confidence": round(self.conf * 0.5, 3),
                                "velocity_pct_s": self._velocity,
                                "rejection_reason": "dead_reckoned",
                                "rise_state": self._rise_state(pred)}
                self.last_debug = {"stage": "coast", "armed": armed, "conf": round(self.conf, 3)}
                if self._relock_geom and self._relock_reject_n:
                    # B4 observability: this coast frame exists because the relocate hit was
                    # REFUSED as geometrically inconsistent with the acquiring lock -- the live
                    # signature of a dÃ©cor blob being re-found under the weak relocate gates.
                    self.last_debug["relock_rej"] = int(self._relock_reject_n)
                rs_c = self._rise_state(self.last_fill)
                vel_c = self._velocity
                if self._stalebreak and rs_c == "rising" and self._fresh_rise_left <= 0:
                    # A2: a coast frame's velocity is STALE (the vel history froze when the red
                    # column vanished); claiming "rising" off it -- and re-advertising the stale
                    # positive velocity -- is what lets a frozen-fill dead-hold refresh the
                    # orchestrator's CV self-arm forever (fresh=0 staleMem=32/33/40 shots live).
                    # Without a FRESH rising RED read inside the freshness window, a coast frame
                    # claims no rise and advertises no velocity.
                    rs_c = ""
                    vel_c = 0.0
                return {"detected": True, "meter_present": True, "fill": self.last_fill,
                        "fill_coarse": self.last_coarse, "bbox": list(self.last_tbox), "stage": "coast",
                        "confidence": round(self.conf, 3), "velocity_pct_s": vel_c,
                        "rejection_reason": "meter_memory", "rise_state": rs_c}
        # real METER COLOUR this frame (red column or green tip) -> reset confidence to full.
        _coast_was = self._coast_n          # B1: pre-reset coast length (post-occlusion launder)
        self.conf = self.CONF_INIT
        self._dr_frames = 0
        self._coast_n = 0
        # EPOCH-3/5: clear the "do not emit" flag HERE, not only inside _read_fill -- a SUBCLASS
        # override can return without ever reaching the base implementation (CompressedMeterReader's
        # K2 luma path does exactly that), and a stale flag from a previous frame must never gate
        # this one.
        self._read_bail = None
        coarse, subpix, tbox, top_row, green = self._read_fill(frame, col)
        # RELEASE ORACLE (diagnostic, see __init__): fold THIS frame's settle measurement into
        # the open release window. Reads only what _read_fill already measured and writes only
        # its own buffer; no branch below it can see a different number because of this call.
        if self._ro_on and self._ro_pending:
            self._ro_note(ts, float(coarse))
        # GREEN-ONLY HOLD (cap/deflate): the green tip anchors the box but the red column has
        # momentarily vanished -> DON'T report a 0% glitch; hold the last good fill while the box
        # stays glued to the meter. A receding red (real deflation) still reads through naturally.
        # Also covers a DEGENERATE red relocate (occl): a spurious/misaligned red fragment that
        # reads fill==0 while we hold a real prior fill is not a true 0% -- holding the last good
        # value (instead of writing 0 + a garbage track height) stops it corrupting the lock.
        green_hold = False
        if subpix <= 0.5 and (evidence == "green"
                              or (self._occl and locked and self.last_fill > 2.0)):
            subpix, coarse = self.last_fill, self.last_coarse
            tbox = self.last_tbox if self.last_tbox and self.last_tbox[2] else tbox
            green_hold = True
            self._held_run += 1   # A2xB1: a green_hold re-emits the HELD fill -- it EXTENDS
                                  # the held-echo run (the _coast_n reset just above is
                                  # exactly how the seam glitch defeated the launder floor)
        if self._read_bail == "no_cap_anchor" and not green_hold:
            self._fresh_lock_streak = 0
            # EPOCH-3: NEVER emit a fill computed on a synthetic denominator. No green cap was
            # seen this frame AND no full-track height has ever been measured, so `subpix` rests
            # on the geometric prior -- a guess, not a measurement. The POSITION is still trusted
            # (real meter colour), so keep the lock and the box glued and let the template stay
            # fresh; the first frame that shows a real cap reads for real. A green_hold is exempt:
            # that path re-emits a previously MEASURED fill, not a number off this denominator.
            self.box = col
            _nx, _ny, _nw, _nh = col
            _ncr = frame[max(0, _ny):min(self.H, _ny + _nh), max(0, _nx):min(self.W, _nx + _nw)]
            if _ncr.shape[0] >= 6 and _ncr.shape[1] >= 3:
                self.tmpl = cv2.cvtColor(_ncr, cv2.COLOR_BGR2GRAY)
                self.tmpl_wh = (_ncr.shape[1], _ncr.shape[0])
            self.last_debug = {"stage": "no_cap_anchor", "armed": armed,
                               "conf": round(self.conf, 3), "evidence": evidence}
            return {"detected": False, "meter_present": False, "fill": 0.0, "fill_coarse": 0.0,
                    "bbox": [0, 0, 0, 0], "stage": "no_cap_anchor",
                    "confidence": round(self.conf, 3), "velocity_pct_s": 0.0,
                    "rejection_reason": "no_cap_anchor", "rise_state": ""}
        if (self._occl_wide and (_coast_was >= 12 or self._held_run >= 12)
                and evidence == "red" and not green_hold
                and self.last_fill - subpix > 20.0):
            # B1: LAUNDER the first fresh red read after a LONG widened-occlusion coast when it
            # steps >20pp BELOW the coasted held fill. The held value was an echo; the fresh
            # read is a new phase (deflate / next action), and reporting both on one continuous
            # track paints a false backward mid-rise step (gate A/B: mid_rise_glitches). Seat
            # the fresh state and emit ONE not-detected frame; continuity resumes next frame.
            _held = self.last_fill
            _held_coarse = self.last_coarse
            self.box = col
            self._lock_w_ref = float(col[2])      # B4: laundered re-seat = a NEW lock identity
            self._relock_reject_n = 0
            self._fresh_up_n = 0                  # B5: the fresh rise run restarts with it
            self._fresh_prev_fill = None
            self._fresh_lock_streak = 0
            self.last_fill, self.last_coarse = subpix, coarse
            self.last_tbox = self._stabilize_shot_tbox(tbox)
            self._bvx = self._bvy = 0.0
            self._bp_dx = self._bp_dy = 0.0; self._bp_n = 0   # FIX-2: fresh re-seat
            self._prev_cx = self._prev_cy = self._prev_floor_y = None
            self._vel_hist.clear(); self._velocity = 0.0; self._accel = 0.0
            self._consec = 0; self._peak_fill = 0.0
            cx0, cy0, cw0, ch0 = col
            cr = frame[max(0, cy0):min(self.H, cy0 + ch0), max(0, cx0):min(self.W, cx0 + cw0)]
            if cr.shape[0] >= 6 and cr.shape[1] >= 3:
                self.tmpl = cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY)
                self.tmpl_wh = (cr.shape[1], cr.shape[0])
            self._held_run = 0   # A2xB1: laundered -> the held-echo run is consumed
            self.last_debug = {"stage": "occl_relock", "armed": armed,
                               "coast_was": _coast_was, "held": round(_held, 2),
                               "fresh": round(subpix, 2)}
            if self._hw_reseat_continuity and hw and self.last_tbox[2] > 0:
                # Preserve visible meter presence, but keep `occl_relock` as the rejection reason
                # so the discontinuous fill remains sampler-stale for this one transition frame.
                return {"detected": True, "meter_present": True, "fill": _held,
                        "fill_coarse": _held_coarse, "bbox": list(self.last_tbox),
                        "stage": "occl_relock", "confidence": round(self.conf, 3),
                        "velocity_pct_s": 0.0, "rejection_reason": "occl_relock",
                        "rise_state": ""}
            return {"detected": False, "meter_present": False, "fill": 0.0, "fill_coarse": 0.0,
                    "bbox": [0, 0, 0, 0], "stage": "occl_relock",
                    "confidence": round(self.conf, 3), "velocity_pct_s": 0.0,
                    "rejection_reason": "occl_relock", "rise_state": ""}
        if evidence == "red" and not green_hold:
            self._held_run = 0   # A2xB1: a GENUINE red read ends the held-echo run
        # N4 rise-probation: a freshly-acquired COLD lock must PROVE it is a live rising meter within
        # a few frames or be dropped as static dÃ©cor. A green tip appearing exempts it (real cap);
        # a hw-arm mid-probation exempts it (a real shot is confirmed in progress).
        if (self._rise_probation or self._steal_probation) and self._prob_active:
            if hw or self._grace_live(ts):
                self._prob_active = False              # real shot confirmed (hw arm or a recent
                                                       # rising read) -> exempt from the dÃ©cor drop
            else:
                if evidence == "green" or green is not None or green_hold:
                    self._prob_green = True
                if self._prob_start is None:
                    self._prob_start = self._prob_prev = subpix
                else:
                    if subpix > self._prob_prev + 0.5:
                        self._prob_inc += 1
                    self._prob_prev = subpix
                risen = subpix - (self._prob_start if self._prob_start is not None else subpix)
                if self._prob_green or (risen >= self._prob_rise_pp
                                        and self._prob_inc >= self._prob_inc_min):
                    self._prob_active = False           # proved a live rise -> keep the lock
                else:
                    self._prob_n += 1
                    if self._prob_n >= self._prob_limit:
                        # no rise + no green in the probation window -> static dÃ©cor fake-lock. Drop
                        # the lock, refuse a cold re-acquire briefly, and emit a clean not-detected.
                        self._prob_active = False
                        self._prob_refuse_n = self._prob_refuse_frames
                        if self._prob_from_steal:
                            # steal probation (fix 1d): the failed steal also consumes the SPENT
                            # occlusion window + held-echo run -- the wide-grace budget that kept
                            # the coast alive must not immediately fund a re-steal of the same
                            # static decor.
                            self._prob_from_steal = False
                            self._occl_wide_until = 0.0; self._occl_wide_live = False
                            self._held_run = 0
                        self.conf = 0.0; self.box = None; self.tmpl = None
                        self._courtwide_lock = False
                        self._courtwide_acquire_tier = ""
                        self._consec = 0; self._peak_fill = 0.0
                        self._bvx = self._bvy = 0.0
                        self._bp_dx = self._bp_dy = 0.0; self._bp_n = 0   # FIX-2: lock dropped
                        self._lock_w_ref = 0.0; self._relock_reject_n = 0  # B4: identity dies too
                        self._fresh_up_n = 0; self._fresh_prev_fill = None  # B5
                        self._fresh_lock_ts = None; self._fresh_lock_streak = 0
                        self._epoch_bridge_active = False
                        self._visual_drop_shot_shape(
                            hw, preserve=(not self._require_gameplay_eligibility
                                          or self._gameplay_lock_authorized))
                        self._prev_cx = self._prev_cy = self._prev_floor_y = None
                        self._lock_red = self._lock_green = None
                        self._traj_last_acc = None; self._traj_rejects = 0; self._dr_frames = 0
                        self._rise_recent_n = 0
                        self._update_velocity(0.0, ts)
                        self.last_debug = {"stage": "no_rise", "armed": armed,
                                           "prob_n": self._prob_n, "held_fill": round(subpix, 2)}
                        return {"detected": False, "meter_present": False, "fill": 0.0,
                                "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": "no_meter",
                                "confidence": 0.0, "velocity_pct_s": 0.0,
                                "rejection_reason": "no_rise", "rise_state": ""}
        # B2 trajectory gate (robust only): reject a fill that tears away from the registration
        # prediction (occlusion misread), 6-rejection cap then accept+reset. The frame is still
        # emitted (reason 'fill_gated' -> sampler tier: FED to the engine, excluded from raw_fed).
        gated = False
        if self._robust and evidence == "red":
            gated = self._trajectory_gate(subpix, ts)
        # EPOCH-5: the strip could not hold the denominator -- this frame's % was measured on a
        # TRUNCATED track. Reuse the fill-gate path verbatim (position trusted, box glued, template
        # kept fresh, held fill emitted, suspect value kept out of velocity); only the reason
        # differs so the tier is attributable downstream.
        _truncated = (not green_hold) and self._read_bail == "strip_truncated"
        if _truncated:
            gated = True
        # motion-follow: update the box-centre velocity (EMA) for the next _relocate window
        cx, cy, cw, ch = col
        ccx, ccy = cx + cw * 0.5, cy + ch * 0.5
        if self._anchor_n1:
            # N1: anchor _bvy to the fill-INVARIANT floor/notch (cy+ch), not the fill-column centre.
            # On a static-position rising meter the floor is fixed but the centre climbs at ~half the
            # fill rate -> a phantom upward _bvy that walks the coast box + relocate window off the
            # meter (drift / coast walk-off). x is not fill-polluted, so _bvx stays on centre-x.
            fcy = float(cy + ch)
            if self._prev_cx is not None:
                self._bvx = 0.5 * self._bvx + 0.5 * (ccx - self._prev_cx)
            if self._prev_floor_y is not None:
                self._bvy = 0.5 * self._bvy + 0.5 * (fcy - self._prev_floor_y)
            self._prev_cx, self._prev_cy, self._prev_floor_y = ccx, ccy, fcy
        else:
            if self._prev_cx is not None:
                self._bvx = 0.5 * self._bvx + 0.5 * (ccx - self._prev_cx)
                self._bvy = 0.5 * self._bvy + 0.5 * (ccy - self._prev_cy)
            self._prev_cx, self._prev_cy = ccx, ccy
        self.box = col
        if gated:
            self._fresh_lock_streak = 0
            # position trusted (real meter colour), FILL suspect: keep the box glued + template
            # fresh, hold the last accepted fill, and DON'T feed the suspect value to velocity.
            cr = frame[max(0, cy):min(self.H, cy + ch), max(0, cx):min(self.W, cx + cw)]
            if cr.shape[0] >= 6 and cr.shape[1] >= 3:
                self.tmpl = cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY); self.tmpl_wh = (cr.shape[1], cr.shape[0])
            if cal is not None and hw:
                try:
                    cal.observe_feed(True)
                except Exception:
                    pass
            self.last_debug = {"stage": stage, "evidence": evidence, "armed": armed,
                               "conf": round(self.conf, 3), "vel_pct_s": round(self._velocity, 1),
                               "bvx": round(self._bvx, 1), "fill_gated": 1,
                               "traj_rejects": self._traj_rejects,
                               "truncated": int(_truncated),
                               "occl_frac": round(getattr(self, "_last_occl_frac", 0.0), 3)}
            if self.last_tbox and self.last_tbox[2]:
                # FIX-2: a fill-gated frame re-emits the HELD box -- predict it forward too.
                _fg_tbox = self._predict_tbox(self.last_tbox, armed) if self._box_predict \
                    else list(self.last_tbox)
            else:
                _fg_tbox = self._stabilize_shot_tbox(tbox)
            return {"detected": True, "meter_present": True, "fill": self.last_fill,
                    "fill_coarse": self.last_coarse,
                    "bbox": _fg_tbox,
                    "stage": stage, "confidence": round(self.conf, 3),
                    "velocity_pct_s": self._velocity, "top_row": top_row, "green": green,
                    "rejection_reason": "strip_truncated" if _truncated else "fill_gated",
                    "rise_state": self._rise_state(self.last_fill)}
        _served_tbox = self._stabilize_shot_tbox(tbox)
        self.last_fill, self.last_coarse, self.last_tbox = subpix, coarse, _served_tbox
        if evidence == "red" and not green_hold:
            _fresh_contiguous = (stage != "acquire" and _coast_was == 0
                                 and self._fresh_lock_ts is not None
                                 and ts - self._fresh_lock_ts <= self._epoch_bridge_ms / 1000.0)
            self._fresh_lock_streak = (self._fresh_lock_streak + 1) if _fresh_contiguous else 1
            self._fresh_lock_ts = ts
        else:
            self._fresh_lock_streak = 0
        # B4: drift the lock's width identity with the ACCEPTED fresh red column only (slow EMA,
        # ~10-frame constant). A genuine camera zoom/pan walks the meter's on-screen width
        # gradually, so the reference follows it; a one-frame dÃ©cor fragment can never move it
        # (it was already refused by the gate above and never reaches here).
        if self._relock_geom and evidence == "red" and not green_hold and col is not None:
            _cw = float(col[2])
            self._lock_w_ref = _cw if self._lock_w_ref <= 0.0 else \
                (self._lock_w_ref + 0.1 * (_cw - self._lock_w_ref))
        self._last_green_hold = bool(green_hold)   # observability (measure scripts / tests)
        # BOX-TIGHT source (attribute-only, flag-independent so harnesses can measure the
        # raw bar on a baseline run too): THIS frame carries a fresh red-column measurement.
        # Alongside the bar (col) and its measured top row, capture the PRE-LATCH per-frame
        # track geometry (`tbox` here is _read_fill's output BEFORE _stabilize_shot_tbox --
        # the live-measured full-track extent mode 2 needs, never the per-shot latched shape)
        # and the outline stroke `_hug_outline_bbox` measured this frame. A green_hold's col
        # is the green TIP (not the bar) and a fill_gated/coast/held frame returns above this
        # line, so those frames correctly leave the per-frame source unset (detect() clears
        # it at the boundary) and the display box translates instead.
        self._tight_src = ((tuple(int(v) for v in col), int(top_row),
                            (tuple(int(v) for v in tbox)
                             if tbox and len(tbox) >= 4 else None),
                            self._hug_stroke) \
            if (evidence == "red" and not green_hold) else None)
        if self._box_predict and not green_hold:
            # FIX-2: a genuinely FRESH read re-anchors the reported box -> the predicted-run
            # accumulator is consumed (a green_hold stores the FROZEN box, so it keeps it).
            self._bp_dx = self._bp_dy = 0.0
            self._bp_n = 0
        # (c) On a GREEN-HOLD frame subpix is the FROZEN last_fill (the red column vanished); feeding
        # it to the LS-slope velocity flattens the rise to ~0 over a cap/deflate hold. CARRY the last
        # real velocity instead (skip the flat sample) so the reported rise stays non-zero through the
        # hold; a genuine receding red reads through as evidence=='red' and updates velocity normally.
        if not (self._occl and green_hold):
            self._update_velocity(subpix, ts)
        self._consec += 1
        rs = self._rise_state(subpix)
        # B5 (FRESH_RISE): A2 gated stale "rising" on the COAST / meter_memory return only. A
        # phantom that `_relocate` re-finds every frame never takes that path -- it is a FRESH
        # read every frame (confirmed live: meter_memory / steal_reseat occurred 0 times while
        # the ESRB legal screen advertised "FILL 87% RISING"), so it kept feeding "rising" +
        # a large jitter-derived velocity straight into the orchestrator's CV self-arm, which
        # re-armed the shot gate every frame and (B2) suppressed the breaker that would have
        # killed it. `rising` comes from an LS slope over 6 samples, and two-sided jitter on a
        # dÃ©cor blob produces a big slope in EITHER direction -- so the slope alone is not
        # evidence of a rise. Require the fill to have ACTUALLY gone up on N CONSECUTIVE fresh
        # samples first: a real meter racks that up in 3 frames (~50 ms of a 0.5-1.5 s rise,
        # before the 6-sample velocity window is even full), while a phantom's next-frame drop
        # resets the run. EXEMPT once the claim is established (`_fresh_rise_left` live -- so a
        # real shot reports continuously after its first qualified frame) and whenever a
        # PHYSICAL arm vouches that a shot is genuinely in progress.
        _rise_gated = False
        if self._fresh_rise_gate and evidence == "red" and not green_hold:
            if self._fresh_prev_fill is not None:
                if subpix > self._fresh_prev_fill + self._fresh_up_pp:
                    self._fresh_up_n += 1
                elif subpix < self._fresh_prev_fill - self._fresh_up_pp:
                    self._fresh_up_n = 0        # oscillated DOWN -> not a physical rise
            self._fresh_prev_fill = subpix
            if (rs == "rising" and self._fresh_up_n < self._fresh_up_req()
                    and not (hw or self._fresh_rise_left > 0)):
                rs = ""
                _rise_gated = True
        # N6 shot-coast: latch the grace on a genuine RISING RED read (real fill climbing, not a
        # frozen green-hold). A shot in flight re-arms this every rising frame, so the grace covers
        # the release occlusion that begins the instant the rise stops + the arm disarms.
        if self._shot_coast and evidence == "red" and not green_hold and rs == "rising":
            if self._shot_coast_ms > 0.0:
                self._grace_until_ts = ts + self._shot_coast_ms / 1000.0   # NF-2: wall-time budget
            else:
                self._rise_recent_n = self._shot_coast_max                 # legacy frame count
        # B5: an UNPROVEN rise must not advertise its (jitter-derived) velocity either -- the CV
        # self-arm fires on `rise_state == 'rising' OR velocity > 40`, so gating only the label
        # would leave the loop intact.
        vel_out = 0.0 if _rise_gated else self._velocity
        if self._stalebreak:
            if evidence == "red" and not green_hold and rs == "rising":
                # A2: only a FRESH rising RED read refreshes the "rising" freshness window.
                self._fresh_rise_left = self._stale_rise_frames
                # [ORION_DEAD_HOLD_GRACE] ...and latch it for the life of the lock. Same evidence
                # bar as the window above (a fresh rising RED read), so nothing weaker can set it.
                self._lock_ever_rose = True
            elif green_hold and rs == "rising" and self._fresh_rise_left <= 0:
                # A2: a green-hold frame's velocity is CARRIED (frozen), not measured -- without
                # a fresh rising red read inside the window it must not claim "rising" (nor
                # advertise the stale velocity) or a dead-hold keeps re-arming the CV shot-gate.
                rs = ""
                vel_out = 0.0
        if (self._occl_wide and hw and evidence == "red"
                and not green_hold and rs == "rising"):
            # B1: latch the WIDENED in-shot occlusion coast budget only from the PHYSICAL gate.
            # The merged/CV gate is detector-derived and a phantom can forge it by advertising a
            # rising jitter slope; it is therefore not authority for post-disarm memory.
            self._occl_wide_until = ts + self._occl_wide_ms / 1000.0
        # B1 harvest / FROZEN inlier probe (hardware-armed red frames only) + B2 scale-dim samples
        if cal is not None and hw and evidence == "red":
            try:
                cal.observe_feed(False)
                cal.observe_red_frame(frame, col, subpix, self._velocity, self._consec, rs,
                                      geom_gates={"w_min": self._w_min, "w_max": self._w_max,
                                                  "h_acq": self._h_acq, "h_max": self._h_max,
                                                  "ar_min": self.AR_MIN},
                                      green_seen=green is not None)
            except Exception:
                pass
        # `_pre_hug_tbox`: the box BEFORE the presentation-only outline hug (see _read_fill).
        _geo_tbox = self._pre_hug_tbox if self._pre_hug_tbox is not None else tbox
        if (self._robust and hw and evidence == "red" and not self._dims_rebased
                and _geo_tbox and _geo_tbox[3] >= 40):
            self._dims_samples.append((float(col[2]), float(_geo_tbox[3])))
        # CAMERA-ANGLE scale estimate: harvest the meter's on-screen full-track HEIGHT on every
        # confident, NON-gated red lock (no hw-arm required -- camera scale is observable off-line
        # too). The self-referencing ratio drives _scale_gates; a nominal meter keeps the estimate
        # at ~1.0 (inside the deadband) so the gates never move. Only real full-track red frames
        # qualify (a short pop-in stub or a green-hold would bias the baseline).
        if (self._scale_adapt and evidence == "red" and not green_hold
                and _geo_tbox and _geo_tbox[3] >= 40 and col and col[2] > 0):
            # A3 (ORION_READER_SCALE_GUARD): only HW-armed or genuinely-RISING red locks may
            # feed the scale EMA. A confident-but-static dÃ©cor false-lock (never rising, never
            # hw-corroborated) could otherwise open the deadband, widen the gates, admit more
            # dÃ©cor, and ratchet the estimate wider for the rest of the session.
            if not self._scale_guard or hw or rs == "rising":
                self._note_scale_sample(float(col[2]))
                if self._scale_guard:
                    self._scale_last_ts = ts
        if (self._vzoom and evidence == "red" and not green_hold
                and _geo_tbox and _geo_tbox[3] >= 40
                and self._track_h_hist):
            # B2 (ORION_READER_VZOOM): remember the locked track box's vertical extent -- the
            # band-follow anchor (and the re-acquire memory after a zoom-out drop). ONLY a
            # green-cap-anchored track (_track_h_hist non-empty) qualifies: a no-green lock's
            # box height is the degenerate ph-clamp fallback (a synthetic 230px strip whose top
            # can sit far above the band) and letting it seed the ref shifted the band -191px
            # off every later real shot (third offline A/B iteration caught this).
            self._note_vshift_sample(float(_geo_tbox[1]),
                                     float(_geo_tbox[1] + _geo_tbox[3]))
        cr = frame[max(0, cy):min(self.H, cy + ch), max(0, cx):min(self.W, cx + cw)]
        if cr.shape[0] >= 6 and cr.shape[1] >= 3:
            self.tmpl = cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY); self.tmpl_wh = (cr.shape[1], cr.shape[0])
        self.last_debug = {"stage": stage, "evidence": evidence, "armed": armed,
                           "conf": round(self.conf, 3), "vel_pct_s": round(self._velocity, 1),
                           "bvx": round(self._bvx, 1),
                           "courtwide_lock": bool(self._courtwide_lock),
                           "courtwide_tier": self._courtwide_acquire_tier}
        if self._vzoom_down and self._vy_down:
            # 1a observability: the applied arm-dive bottom widen (px below the band bottom)
            self.last_debug["vy_down"] = round(self._vy_down, 1)
        if self._fresh_rise_gate and _rise_gated:
            # B5 observability: this frame's "rising" claim was refused for want of consecutive
            # genuinely-increasing fresh samples (fresh_up = how many it has so far).
            self.last_debug["rise_gated"] = int(self._fresh_up_n)
        if self._robust:
            self.last_debug["occl_frac"] = round(getattr(self, "_last_occl_frac", 0.0), 3)
        _emit_tbox = list(self.last_tbox)
        if self._box_predict and green_hold:
            # FIX-2: a green_hold frame re-emits the FROZEN box while the meter may be
            # sliding (its fresh col updated _bvx above) -- predict the REPORTED box
            # forward so it tracks instead of trailing. x-ONLY (vy=0): the green-tip col's
            # centre-y is an evidence-type teleport, not motion. last_tbox stays frozen.
            _emit_tbox = self._predict_tbox(_emit_tbox, armed, vy=0.0)
        return {"detected": True, "meter_present": True, "fill": subpix, "fill_coarse": coarse,
                "bbox": _emit_tbox, "stage": stage, "confidence": round(self.conf, 3),
                "velocity_pct_s": vel_out, "top_row": top_row, "green": green,
                "rejection_reason": "green_not_found", "rise_state": rs}

    # ------------------------------------------------------------------ #
    #  PRODUCTION ADAPTER: DetectResult identical to MeterDetector.detect()
    # ------------------------------------------------------------------ #
    def detect(self, frame_bgr, ts: Optional[float] = None):
        """Drop-in for MeterDetector.detect(): emit the SAME DetectResult contract so the
        orchestrator/sidecar/native/timing stack consume it unchanged. When the green make-window is
        seen this frame it is emitted as a real release target (start/end/center/width, on the SAME
        fill scale); when it is not, green stays 'not found' (center=-1, conf=0) and the engine times
        the TIP via eta_ms. rejection_reason='green_not_found' keeps a fresh frame FED either way."""
        # Snapshot the controller identity before touching this frame. If notify_physical_shot_start
        # interleaves later, this result remains stamped OLD (and native rejects it) instead of
        # laundering old pixels into the new epoch through the mutable reader global.
        _detect_shot_epoch = int(self._physical_shot_epoch) if self._shot_armed_hw else 0
        # [ORION_READER_FORGET_RATE_LIMIT] the FRAME clock, kept for the control-thread paths
        # (notify_physical_shot_start) that have no ts of their own: the locator's first-sight
        # pairs are stamped in frame time, so asking whether one is still live needs it.
        if ts is not None and ts == ts:
            self._frame_ts_last = float(ts)
        self._prepare_frame_geometry(frame_bgr)
        # BOX-TIGHT: the per-frame fresh-column source must be cleared at the PRODUCTION
        # boundary, not only inside read() -- a subclass read() can return before the base
        # fresh path runs (CompressedMeterReader's stale hold does), and a stale source from
        # a previous frame must never re-shape this frame's held box as if it were fresh.
        self._tight_src = None
        # B6: latch/clear the physical arm edge BEFORE read(), so the breaker's bounded hw grace
        # is measured from the press that opened this window (see _hw_arm_grace_live).
        self._note_hw_arm_edge(ts)
        # [ORION_PLAYER_ANCHOR] publish this press window to the anchor, BEFORE the frame is
        # submitted to the locator: the locator decides where to look from it.
        self._publish_press_window(ts)
        # [ORION_CV_TIPLESS_ARMED] drain the locator's tipless record (a dict read and, at most
        # once per press, one ERROR line) on the reader thread, never on the detector worker.
        self._flush_tipless_line()
        # RELEASE ORACLE (diagnostic, see __init__): run the open window's clock.
        self._ro_tick(ts)
        # B7: capture this shot's arm-edge red reference (once per hardware epoch, at the press).
        self._note_arm_edge_reference(frame_bgr)
        # DETECTOR-DRIVEN LOCATION (see __init__): submit this frame to the async YOLO locator
        # and, when its freshest box disagrees with the colour lock, seat the lock on the
        # detector's box so read()'s tight relocate + _read_fill refine within the CORRECT
        # column. When the detector is fresh and sees NO meter, latch a veto applied to the
        # colour reader's output below. All guarded so a missing detector is inert.
        self._det_no_meter = False
        self._det_region = None       # per-frame: constrains read()'s search to the detector box
        self._det_occ_current_full_negative = False
        self._det_occ_current_source_age = float("inf")
        # Captured identity for the capless detector-fill proof below.  Only a
        # unique, fresh locator result accepted into this exact lifecycle generation
        # may advance its evidence window; repeated async slots are inert.
        _df_nogreen_locator_fresh = False
        _df_nogreen_locator_ts = None
        _df_nogreen_locator_box = None
        _df_nogreen_locator_generation = 0
        # Template cadence belongs to unique accepted detector SOURCE results, not
        # capture callbacks. latest() repeats its slot between worker completions.
        # Reset before lookup so a missing/failed locator cannot replay refresh intent.
        self._det_new_accept = False
        _detector_fault = ""
        if self._meter_detector is not None:
            # A source timestamp is committed only with a successful locator
            # pass. If processing raises after advancing the high-water mark,
            # the same still-fresh async result must be eligible for a retry.
            _det_seen_before = self._det_seen_dts
            try:
                # PRIORITY-ON-ACQUIRE decision (legacy knob name, see __init__): enqueue the
                # freshest frame with a worker-priority wake. Expensive preprocessing/ORT must
                # never execute on this capture callback.
                _pre_now = ts if ts is not None else 0.0
                self._publish_static_rank_zones(_pre_now)
                # Lifecycle mode: "acquiring" == not LOCKED (a pending candidate still needs the
                # next unique worker result so its second corroborating look is not starved).
                _acquiring = ((self._det_state != 'locked') if self._det_lifecycle
                              else (self._det_last_box is None
                                    or (_pre_now - self._det_last_found_ts) > self._det_hold_s))
                _priority_epoch = int(_detect_shot_epoch or 0)
                _new_arm_event = (bool(self._shot_armed_hw)
                                  and self._det_priority_epoch != _priority_epoch)
                _new_pending_event = (
                    self._det_lifecycle and self._det_state == 'pending'
                    and float(self._det_pend_ts)
                    > float(self._det_priority_pending_ts) + 1.0e-6)
                _phase_repeat = (
                    self._det_phased_acquire
                    and (_pre_now - self._det_last_sync_acq)
                    >= self._det_phased_priority_s)
                _priority_event = (self._det_sync_acq_always or _new_arm_event
                                   or _new_pending_event or _phase_repeat)
                _priority_due = (self._det_sync_acquire
                                 and (self._det_sync_acq_always or self._shot_armed_hw)
                                 and _acquiring and _priority_event
                                 and (_pre_now - self._det_last_sync_acq)
                                 >= self._det_acq_interval_s)
                _queued_priority = False
                _scan_region = 'full'
                if _priority_due:
                    if self._det_phased_acquire:
                        _scan_region = self._det_next_acq_scan_region(
                            _detect_shot_epoch)
                    else:
                        self._det_acq_scan_last = 'full'
                    _queued_priority = self._det_submit_priority(
                        frame_bgr, _pre_now, _scan_region)
                    if _queued_priority:
                        self._det_last_sync_acq = _pre_now
                        if _new_arm_event:
                            self._det_priority_epoch = _priority_epoch
                        if _new_pending_event:
                            self._det_priority_pending_ts = float(self._det_pend_ts)
                        self._det_diag['priority_acq'] = (
                            self._det_diag.get('priority_acq', 0) + 1)
                        self._det_diag['scan_' + _scan_region] += 1
                if not _queued_priority:
                    _hot = False
                    if self._det_armed_hot and self._shot_armed_hw:
                        if self._det_hot_epoch != _priority_epoch:
                            self._det_hot_epoch = _priority_epoch
                            self._det_hot_open_ts = _pre_now
                        _hot = (_pre_now - self._det_hot_open_ts) <= self._det_armed_hot_max_s
                    _hot_fn = getattr(self._meter_detector, 'submit_priority', None) if _hot else None
                    if callable(_hot_fn):
                        _hot_fn(frame_bgr, ts if ts is not None else 0.0)
                        self._det_diag['hot_submit'] = self._det_diag.get('hot_submit', 0) + 1
                    else:
                        self._meter_detector.submit(frame_bgr, ts if ts is not None else 0.0)
                # New locators expose scope atomically. Legacy/fake locators retain
                # the four-tuple latest() contract and are necessarily full-frame.
                _latest_details = getattr(self._meter_detector, 'latest_details', None)
                if callable(_latest_details):
                    _dfound, _dbox, _dconf, _dts, _dscope = _latest_details()
                else:
                    _dfound, _dbox, _dconf, _dts = self._meter_detector.latest()
                    _dscope = 'full'
                if _dscope not in ('full', 'left', 'right'):
                    _dscope = 'full'
                try:
                    _dts_f = float(_dts)
                    _dts_valid = bool(np.isfinite(_dts_f) and _dts_f >= 0.0)
                except (TypeError, ValueError, OverflowError):
                    _dts_f = -1.0
                    _dts_valid = False
                try:
                    _dts_age = float(ts) - _dts_f
                    _dts_ordered = bool(
                        ts is not None and np.isfinite(float(ts))
                        and _dts_valid and np.isfinite(_dts_age)
                        and _dts_age >= -1.0e-6)
                except (TypeError, ValueError, OverflowError):
                    _dts_age = float("inf")
                    _dts_ordered = False
                # Freshness is two-sided.  A source frame cannot originate after
                # the callback consuming it; accepting such a stamp both grants an
                # unbounded hold (now-source stays negative) and can poison the
                # monotonic unique-result clock ahead of every legitimate result.
                _dfresh = bool(
                    _dts_ordered and _dts_age <= float(self._det_ttl_s))
                _source_this_arm = self._det_result_this_arm(_dts)
                _dg = self._det_diag
                _dg["calls"] += 1
                self._det_fresh_accept = False   # per-frame: set below on an accepted proposal
                if _dfound:
                    _dg["found"] += 1
                if _dfresh:
                    _dg["fresh"] += 1
                elif _dfound:
                    _dg["stale"] += 1              # a box existed but was older than the TTL
                _now = ts if ts is not None else 0.0
                # UNIQUE-result guard for the lifecycle: in async mode latest() repeats one
                # result across many frames, and streaks/strikes must count detector RESULTS,
                # not frames (a single async box must not fake a 2-frame acquire streak).
                # Do not advance the unique-result high-water mark for a future
                # stamp.  If the producer clock recovers on the next callback, its
                # legitimate source must still be able to acquire immediately.
                _new_res = bool(
                    _dts_ordered and _dts_f > self._det_seen_dts)
                if _new_res:
                    self._det_seen_dts = float(_dts)
                    # A unique result revokes recovery authority until this same
                    # result proves to be a fresh, accepted, current-arm positive.
                    # The exceptional one-frame negative bridge is authorized
                    # separately by its <=45 ms + broad-body gates; this positive-
                    # source flag never grants it implicitly.
                    self._det_occ_locator_source_positive = False
                if not _dfresh or not _dfound:
                    # Repetition does not make an async slot immortal: once its
                    # source timestamp exceeds the detector TTL it loses recovery
                    # authority even though it is not a new result.
                    self._det_occ_locator_source_positive = False
                # A read-rescue result is ordinary timestamped detector evidence.  It
                # cannot bypass lifecycle/teleport/no-meter policy; this flag only says
                # that an accepted result should reset the stale position-serving state.
                _rescue_reseat = False
                _rescue_source_ready = False
                _rrts = float(getattr(self, '_det_rescue_request_ts', -1.0e9))
                if _rrts > -1.0e8:
                    if (_now - _rrts) > max(float(self._det_ttl_s), 0.20):
                        self._det_rescue_request_ts = -1.0e9
                    elif (_new_res and _dts_valid and _dscope == 'full'
                          and _dts_f + 1.0e-6 >= _rrts):
                        _rescue_source_ready = True
                self._note_press_ghost_full_result(
                    found=bool(_dfound), fresh=bool(_dfresh), scope=str(_dscope),
                    new_result=bool(_new_res))
                self._note_static_zone_absence(
                    found=bool(_dfound), fresh=bool(_dfresh), scope=str(_dscope),
                    new_result=bool(_new_res))
                if _dfresh and _dfound and _dbox is not None:
                    _bb = tuple(int(v) for v in _dbox)
                    # LIFECYCLE gate (pass-through when ORION_METER_LIFECYCLE=0): the proposal
                    # may be refused -- a pending candidate still warming up its acquire streak,
                    # or a teleport outlier accruing re-seed strikes -- and then NOTHING moves
                    # this frame (the retired MeterBoxKalman served the coasted prediction on
                    # 'reject', never the raw jump).
                    if self._det_on_found(_bb, float(_dconf or 0.0), _now, _new_res,
                                          result_ts=_dts):
                        self._det_fresh_accept = True
                        # KEEP may accept the same still-fresh async slot repeatedly
                        # for presence/width checks. It must not reseed NCC from every
                        # intervening frame: fill-edge/occluder changes would compound
                        # into template drift instead of tracking one stable reference.
                        self._det_new_accept = bool(_new_res)
                        if _new_res and _source_this_arm:
                            self._det_occ_locator_source_positive = True
                        _rescue_reseat = bool(_rescue_source_ready)
                        if _dfresh and _new_res and _source_this_arm:
                            _df_nogreen_locator_fresh = True
                            _df_nogreen_locator_ts = float(_dts)
                            _df_nogreen_locator_box = _bb
                            _df_nogreen_locator_generation = int(
                                getattr(self, "_det_lock_generation", 0) or 0)
                        self._det_last_box = _bb
                        if _new_res:
                            # Hold authority is evidence-clocked.  latest() repeats
                            # one async slot on every capture callback; consumption
                            # time must not renew a source frame's lifetime forever.
                            self._det_last_found_ts = float(_dts_f)
                        # History is keyed on the FRAME the box was computed from (_dts), not now --
                        # otherwise the velocity is measured against the wrong clock.
                        _bx0, _by0, _bw0, _bh0 = self._det_last_box
                        _hts = float(_dts) if (_dts is not None and _dts >= 0.0) else _now
                        if not self._det_box_hist or _hts > self._det_box_hist[-1][0]:
                            self._det_box_hist.append((_hts, _bx0 + _bw0 * 0.5,
                                                       _by0 + _bh0, _bw0, _bh0))
                        # Re-seed the tracker template from every fresh detection: the meter's
                        # appearance changes as it fills, so a stale template would decay.
                        # CONTINUITY mode owns its own template lifecycle in _det_track_step --
                        # seeding here from the CURRENT frame at the detection's (stale) box is
                        # exactly the bias that pinned the box behind a panning meter.
                        if self._det_track and not self._det_track_cont:
                            self._seed_track_template(frame_bgr, self._det_last_box)
                elif (self._det_lifecycle and _dfresh and not _dfound
                      and _dscope == 'full'):
                    # No-meter STRIKE accounting (locked only). May force-drop the lock, after
                    # which the veto branch below fires on the now-empty hold.
                    self._det_occ_current_full_negative = True
                    self._det_occ_current_source_age = (
                        float(_now) - float(_dts_f))
                    self._det_on_nofound(_now, _new_res)
                if _rescue_source_ready:
                    # One unique full-frame result consumes one rescue request whether
                    # found, refused by lifecycle, or no-find. A continued read failure
                    # may schedule another bounded request after RESCUE_GAP_MS.
                    self._det_rescue_request_ts = -1.0e9
                # HELD box: keep the last-found meter box across brief async found=False gaps
                # (the detector runs on its own thread and can miss a cycle mid-shot; without this
                # the region/seed vanish for a frame and read() re-locks décor).
                _held = None
                _lock_ok = (self._det_state == 'locked') if self._det_lifecycle else True
                _hold_allow = self._det_hold_s
                if self._det_lifecycle and _lock_ok:
                    # EXTENDED COAST (retired peak-hold, _PEAK_HOLD_S=0.6 wall-clock bound): a
                    # lock that PROVED a >=_det_coast_rise_pp rise and still measured a white
                    # ribbon at the tracked spot last frame bridges the locator's cap blindness
                    # -- measured on session_20260830_003937, YOLO returns found=0 for 17-33
                    # CONSECUTIVE frames inside real shot runs, which is what expired the old
                    # 0.35s hold mid-shot and produced the owner's on/off/on churn.
                    _rise_ok = (self._det_lock_fill0 is not None
                                and (self._det_lock_fill_max - self._det_lock_fill0)
                                >= float(self._det_coast_rise_pp))
                    if _rise_ok and self._det_last_presence:
                        _hold_allow = max(_hold_allow, self._det_coast_max_s)
                if self._det_last_box is not None and ts is not None and _lock_ok:
                    if (_now - self._det_last_found_ts) > _hold_allow:
                        if self._det_lifecycle:
                            # Explicit DROP (not a silent fade): arms the warm re-acquire
                            # memory so a find moments later re-latches instantly.
                            self._det_drop_lock(_now, 'coast_expired')
                    else:
                        _held = self._det_last_box
                        # EXTRAPOLATE to now: the newest box was computed from a frame that is
                        # already tens of ms old. Advance it along its measured velocity so it
                        # sits where the meter IS, not where it was. Size is held (the meter does
                        # not resize while it translates); horizontal centre and bottom edge move.
                        # Bottom-edge geometry is load-bearing: detector height jitter must not
                        # manufacture vertical translation. Bounded by
                        # _det_extrap_max_s.
                        if self._det_extrap and len(self._det_box_hist) >= 2:
                            _h0 = self._det_box_hist[0]; _h1 = self._det_box_hist[-1]
                            _span = _h1[0] - _h0[0]
                            _age = _now - _h1[0]
                            if _span > 1e-3 and 0.0 < _age <= self._det_extrap_max_s:
                                # ROBUST velocity (see _det_hist_vel): a Theil-Sen median
                                # over every pairwise slope, so one in-gate acquisition hop
                                # cannot sling the served box off a static meter. Shipped code carried a
                                # bare first-last fit and was only shielded by the NCC
                                # stale-box pin the continuity fix removed. While the
                                # estimate is UNTRUSTED (short history) the total shift
                                # is additionally capped to a few px -- bounded onset
                                # correction on a real fade, bounded onset damage on a
                                # static meter (both failure modes measured; see knobs).
                                _vx, _vy, _spd, _vtr = self._det_hist_vel()
                                if not _vtr:
                                    _sh = float(np.hypot(_vx * _age, _vy * _age))
                                    _shc = float(self._det_early_shift_max)
                                    if _sh > _shc > 0.0:
                                        _vsc = _shc / _sh
                                        _vx *= _vsc; _vy *= _vsc
                                _cx = _h1[1] + _vx * _age
                                _bot = _h1[2] + _vy * _age
                                _w, _h = int(_h1[3]), int(_h1[4])
                                _nx = int(round(_cx - _w * 0.5)); _ny = int(round(_bot - _h))
                                _nx = max(0, min(int(self.W) - _w, _nx))
                                _ny = max(0, min(int(self.H) - _h, _ny))
                                _held = (_nx, _ny, _w, _h)
                if _rescue_reseat and _held is not None:
                    # _held is the accepted source box advanced to this callback's clock,
                    # so a moving meter is not snapped backward to inference-old pixels.
                    self._det_hard_reseat_serving(frame_bgr, _held, _now)
                    self._det_diag['rescue_seat'] = (
                        self._det_diag.get('rescue_seat', 0) + 1)
                self._det_active_box = _held
                if _held is not None:
                    # CONSTRAIN read()'s search to the meter box AND seed the lock there, so read()'s
                    # own machinery (velocity/breakers/state) tracks the right column. The direct-fill
                    # override below is the guarantee; this keeps read() coherent.
                    bx, by, bw, bh = _held
                    _px = max(16, int(bw * 0.75)); _py = max(24, int(bh * 0.25))
                    self._det_region = (max(0, bx - _px), max(0, by - _py),
                                        min(int(self.W), bx + bw + _px),
                                        min(int(self.H), by + bh + _py))
                    _seat = True
                    if self.box is not None and self.conf >= self.CONF_MIN:
                        ax, ay, aw, ah = self.box
                        _ix = max(ax, bx); _iy = max(ay, by)
                        _ix2 = min(ax + aw, bx + bw); _iy2 = min(ay + ah, by + bh)
                        _inter = max(0, _ix2 - _ix) * max(0, _iy2 - _iy)
                        _iou = (_inter / float(aw * ah + bw * bh - _inter + 1e-9)
                                if aw > 0 and bw > 0 else 0.0)
                        if _iou >= self._det_seat_iou:
                            _seat = False
                    if _seat:
                        self.box = (bx, by, bw, bh)
                        self.conf = self.CONF_INIT
                        self._det_seeded = True
                        _dg["seeded"] += 1
                elif _dfresh and not _dfound and _dscope == 'full':
                    # No held box either -> the detector is confident there is no meter -> veto.
                    self._det_no_meter = True
                    self._det_fill_hist.clear()
                    self._det_coarse_fill_hist.clear()
                    self._det_box_hist.clear()   # stale motion must not extrapolate a new lock
                    self._det_tmpl = None        # and no stale template may track a new lock
                    self._det_tmpl_size = None
                    self._det_tmpl_cx_offset = 0.0
                    self._det_tmpl_bottom_offset = 0.0
                    self._subpx_D = None; self._subpx_provisional = False         # and no stale per-shot scale/anchor may
                    #                                measure a new meter
                    self._subpx_off = None
                    self._subpx_seed = []
                    self._subpx_base_hist = []; self._subpx_last_base_rel = None
                    self._subpx_carry_pending = False
                    self._coarse_denom_ref = None  # a later detector lock is a new ruler identity
                    self._subpx_camera_ref = None
                    self._subpx_camera_scale = 1.0
                    self._det_scale_reference = None
                    self._det_scale_pending = None
                    self._det_track_scale_match = None
                    self._det_track_vx = 0.0     # a new lock starts with no inherited motion
                    self._det_track_vy = 0.0
                    self._det_track_last_match = None
                    self._det_track_box = None   # and no stale tracked position either
                    self._det_track_box_ts = None
                    self._det_track_last_match_ts = None
                    self._det_track_sample_ts = None
                    self._det_track_clock_timed = None
                    self._det_track_velocity_dt_s = 1.0 / 60.0
                    self._det_tmpl_ts = -1.0e9
                    self._det_tmpl_pos = None
                    _dg["nofound"] += 1
                # THROTTLED HEALTH (ERROR level -> not throttled by the native relay). One line
                # every ~2s so a live run reveals whether the detector is finding the meter,
                # whether results are fresh (not stale/CPU-starved), and whether it is seeding.
                try:
                    if ts is not None and (ts - self._det_diag_last) >= 2.0:
                        self._det_diag_last = ts
                        # [ORION_READER_HEALTH_FIELD_ORDER 2026-09-16] THE LAYER COUNTERS COME
                        # FIRST. The native relay forwards `trimmed.left(300)` of every sidecar
                        # WARNING/ERROR (RemotePlaySession.cpp), and with a ~45-char log prefix
                        # that cut landed in the middle of `lifecycle(...)`: on the 09-16 blind
                        # run `idle_unpublished=` and `idle_reuse=` were never once visible in the
                        # native log, so two of the six publication layers had NO live counter and
                        # could be neither accused nor cleared. Everything that can WITHHOLD a read
                        # now sits inside the first ~150 chars; the descriptive scan/lifecycle
                        # census stays behind it, where a trim costs nothing.
                        _lst = getattr(getattr(self._meter_detector, "_base", None),
                                       "stats", None) or {}
                        _acq_logger.error(
                            "DETECTOR HEALTH provider=%s"
                            + " loc_forget=" + str(_dg.get("loc_forget", 0))
                            + "/" + str(getattr(self, "_loc_forget_suppressed", 0))
                            + "/" + str(getattr(self, "_loc_forget_deferred", 0))
                            + " detfault=" + str(_dg.get("locator_exception", 0))
                            + "/" + str(_dg.get("fill_exception", 0))
                            + " reseed_refused=" + str(_dg.get("reseed_refused", 0))
                            + " staticq=" + str(len(self._static_zones))
                            + "/" + str(self._static_zone_withheld)
                            + " staticq_repeat=" + str(self._static_zone_withheld_repeat)
                            # [ANCHOR INSTRUMENT 2026-09-21] idle/patch/refused/none/lo/mid/hi.
                            # Sits HERE, not in the proposer's cv={} tail: the relay's 300-char
                            # cap dropped that tail on the very first live line (335 chars).
                            + " anchor=" + "/".join(str(_lst.get(k, 0)) for k in (
                                "idle_hit", "anchor_patch_hit", "refused_outside", "anchor_none",
                                "anchor_conf_lo", "anchor_conf_mid", "anchor_conf_hi"))
                            + " idle_unpublished=" + str(_dg.get("idle_unpublished", 0))
                            + " idle_reuse=" + str(_lst.get("idle_reuse", 0))
                            + " press_fresh_withheld=" + str(
                                getattr(self, "_press_fresh_withheld", 0))
                            + " tipless=" + str(_lst.get("tipless_accept", 0))
                            + "/" + str(_lst.get("tipless_pending", 0))
                            + " infer=%.0fms calls=%d found=%d fresh=%d "
                            "stale=%d seeded=%d nofound_veto=%d last=(found=%s fresh=%s box=%s age=%.2fs)"
                            " lifecycle(state=" + str(self._det_state)
                            + " locks=" + str(_dg["lock"]) + " drops=" + str(_dg["drop"])
                            + " reseeds=" + str(_dg["reseed"])
                            + " outliers=" + str(_dg["outlier"])
                            + " rescues=" + str(_dg.get("rescue", 0))
                            + "/" + str(_dg.get("rescue_seat", 0))
                            + " occl_fill=" + str(_dg.get("occlusion_fill", 0))
                            + " top=" + str(_dg.get("occlusion_top", 0))
                            + " neg=" + str(_dg.get("occlusion_negative", 0))
                            + " reject=" + str(_dg.get("occlusion_reject", 0)) + ")"
                            + " hot_submit=" + str(_dg.get("hot_submit", 0))
                            + " reseat_x=" + str(_dg.get("reseat_x", 0))
                            + " dims_locked=" + str(_dg.get("dims_locked", 0))
                            + " det_only_veto=" + str(_dg.get("det_only_veto", 0))
                            # [ORION_READER_IDLE_PUBLISH_GATE] locks the reader tracked but did NOT
                            # hand to the engine/overlay (no arm, no rise, no continuation) --
                            # moved to the FRONT of this line, where the relay's trim cannot eat it.
                            + " cv=" + str(self._proposer_stats())
                            + " scan(enabled=%s scope=%s last_side=%s"
                            + " full=%d/%d left=%d/%d right=%d/%d partial_miss=%d)",
                            getattr(self._meter_detector, "provider", "?"),
                            float(getattr(self._meter_detector, "infer_ms", 0.0)),
                            _dg["calls"], _dg["found"], _dg["fresh"], _dg["stale"],
                            _dg["seeded"], _dg["nofound"], _dfound, _dfresh,
                            ([int(v) for v in _dbox] if _dbox else None),
                            (float(ts - _dts) if (_dts is not None and _dts >= 0.0) else -1.0),
                            self._det_phased_acquire, _dscope,
                            self._det_acq_last_side or '-',
                            _dg['scan_full_hit'], _dg['scan_full'],
                            _dg['scan_left_hit'], _dg['scan_left'],
                            _dg['scan_right_hit'], _dg['scan_right'],
                            _dg['scan_partial_miss'])
                except Exception:
                    # Telemetry is never detector authority; a broken counter/logger
                    # cannot turn a valid locator result into a missed shot.
                    self._det_diag["health_exception"] = (
                        self._det_diag.get("health_exception", 0) + 1)
            except Exception:
                # A locator exception is not a fresh positive or a fresh negative.
                # In particular it must not reuse _det_active_box from the previous
                # callback as current detector authority; that bypassed the normal
                # source-age/hold checks for as long as submit/latest kept failing.
                self._det_diag["locator_exception"] = (
                    self._det_diag.get("locator_exception", 0) + 1)
                if self._physical_shot_epoch == _detect_shot_epoch:
                    self._det_no_meter = False
                    self._det_seen_dts = _det_seen_before
                    # _det_on_found and other policy helpers may have partially
                    # mutated the lifecycle before the exception. Retry from an
                    # empty lock, never count one source as two observations.
                    self._det_reset_lock_state()
                    self._det_active_box = None
                    self._det_warm_pos = None
                    self._det_warm_ts = -1.0e9
                    self._clear_gameplay_structure_proof()
                    self._gameplay_lock_authorized = False
                _detector_fault = "detector_locator_exception"
        # read()/qualification can latch colour-derived structure before the
        # detector-authoritative fill is measured. Remember only the census
        # history: a fill fault must revoke proof, never resurrect proof that
        # read()/qualification intentionally cleared on an identity change.
        _census_before = getattr(self, "_ep_census", None)
        _census_epoch_before = (_census_before.get("epoch")
                                if isinstance(_census_before, dict) else None)
        _census_latched_before = (_census_before.get("latched")
                                  if isinstance(_census_before, dict) else None)
        if _detector_fault:
            # Do not run read()/qualification on a failed locator frame: even a
            # rejected colour sample could latch structure proof for this shot.
            self.conf = 0.0; self.box = None; self.tmpl = None
            self.last_fill = 0.0; self.last_coarse = 0.0
            self.last_debug = {"stage": _detector_fault}
            s = {"detected": False, "meter_present": False, "fill": 0.0,
                 "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                 "stage": _detector_fault, "confidence": 0.0,
                 "velocity_pct_s": 0.0, "rejection_reason": _detector_fault,
                 "rise_state": ""}
        elif self._require_gameplay_eligibility and not self._shot_armed_hw:
            s = self._close_gameplay_gate()
        else:
            if self._require_gameplay_eligibility:
                self._gameplay_gate_closed = False
            s = self.read(frame_bgr, ts)
            if self._require_gameplay_eligibility:
                s = self._qualify_gameplay_sample(s, _detect_shot_epoch)
        # DETECTOR-AUTHORITATIVE FILL. read()'s white-blind colour acquire wanders onto décor even
        # when the box is seeded (rendered live proof: detector dead-on the meter, reader on the
        # player at 0% fill), AND _read_fill masks the configured colour so it reads 0 on a white
        # meter unless meter_color=='White' (rendered proof: meter filling to 92% while the reader
        # reported 0.00). So whenever the detector holds a meter, measure fill DIRECTLY in its box
        # with a COLOUR-AGNOSTIC white scan and emit THAT -- authoritative regardless of what read()
        # locked or how meter_color is set. It runs after the gate too, so a gameplay-ineligible
        # frame during the fill is no longer dropped: the detector's meter presence is the proof.
        # [ORION_READER_BOX_WIDTH_GATE 2026-09-03] Judge the box BEFORE it is measured, on the
        # detector's RAW candidate width (the smoothed emit ramps a 38 px box in as 29/32/34 and
        # would let two frames through) as well as the served width; wide only (legitimate narrow
        # boxes exist: width 17 after a 23 median in archived sessions). A hit is a REJECTED
        # frame (detected False, coarse 0, explicit reason) -- native treats an empty reason as
        # accepted and strict ownership consumes the coarse fill -- and three hits in a row retire
        # the lock so a persistent false box cannot starve re-acquisition.
        _bw_gate_hit = False
        if (self._det_active_box is not None and self._box_w_gate
                and len(self._box_w_hist) >= self._box_w_min_n):
            try:
                _bw_served = float(self._det_active_box[2])
                _bw_served_h = max(1.0, float(self._det_active_box[3]))
                _bw_raw = _bw_served
                _bw_raw_h = _bw_served_h
                # The raw candidate is the lifecycle-ACCEPTED fresh proposal only: a refused
                # teleport proposal in the async slot must not be able to reject or retire the
                # good lock it was refused against (Sol, review #57).
                if bool(getattr(self, "_det_fresh_accept", False)) and self._det_last_box:
                    _bw_raw = float(self._det_last_box[2])
                    _bw_raw_h = max(1.0, float(self._det_last_box[3]))
                _bw_key = max(_bw_served / _bw_served_h, _bw_raw / _bw_raw_h)
                _bw_med = float(np.median(list(self._box_w_hist)))
                if _bw_med > 0.0 and _bw_key > _bw_med * self._box_w_ratio:
                    _bw_gate_hit = True
            except Exception:
                _bw_gate_hit = False
        if _bw_gate_hit:
            self._box_w_gate_hits += 1
            self._box_w_consec += 1
            self._last_fill_estimator_mode = ""
            self._last_fill_estimator_generation = 0
            self._last_fill_coarse = 0.0
            self.last_fill = 0.0
            self.last_coarse = 0.0
            _bw_retire = (self._box_w_consec % self._box_w_retire_n) == 0
            if ts is not None and (float(ts) - self._box_w_log_ts) >= 1.0:
                self._box_w_log_ts = float(ts)
                _acq_logger.error(
                    "BOX WIDTH IMPLAUSIBLE: served_w=%d raw_w=%d key=%.3f session_median_ratio=%.3f "
                    "limit=%.2f consecutive=%d%s -> frame rejected",
                    int(_bw_served), int(_bw_raw), _bw_key, _bw_med, self._box_w_ratio,
                    int(self._box_w_consec), " (lock retired)" if _bw_retire else "")
            s = {"detected": False, "meter_present": False, "fill": 0.0,
                 "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                 "stage": "box_width_implausible", "confidence": 0.0,
                 "velocity_pct_s": 0.0,
                 "rejection_reason": "box_width_implausible", "rise_state": ""}
            try:
                self.last_debug = {"stage": "box_width_implausible",
                                   "served_w": int(_bw_served), "raw_w": int(_bw_raw),
                                   "median_w": round(_bw_med, 1)}
            except Exception:
                pass
            if _bw_retire:
                self._box_w_retired += 1
                self.conf = 0.0; self.box = None; self.tmpl = None
                try:
                    self._det_reset_lock_state()
                    self._det_warm_pos = None
                    self._det_warm_ts = -1.0e9
                    self._det_active_box = None
                except Exception:
                    pass
        else:
            self._box_w_consec = 0
        if self._det_active_box is not None and not _bw_gate_hit:
            try:
                _ab = tuple(int(v) for v in self._det_active_box)
                # DETECT-THEN-TRACK: the detector only refreshes every ~45ms; track the meter on
                # EVERY frame with a cheap NCC match so the box stays ON it between detections.
                if self._det_track:
                    _ab = tuple(int(v) for v in self._det_track_step(
                        frame_bgr, _ab, ts))
                    self._det_active_box = _ab
                _tracked_ab = _ab
                _ab, _display_ab = self._det_measurement_boxes(frame_bgr, _ab, ts)
                # The active box remains the detector/tracker authority's box;
                # pixel-crop dimensions cannot dilute the original width gate.
                self._det_active_box = _tracked_ab if self._det_measure_raw else _ab
                _sp, _grn, _tr = self._measure_fill_in_box(frame_bgr, _ab, ts=ts)
                if (self._reseat_x and _tr is not None and int(_tr) < 0
                        and not self._det_occ_current_full_negative):
                    # [ORION_READER_RESEAT_X] the served box may simply be beside the meter
                    _dx = self._reseat_x_dx(frame_bgr, _ab)
                    if _dx is not None:
                        _ab2 = (int(_ab[0]) + int(_dx), int(_ab[1]), int(_ab[2]), int(_ab[3]))
                        _sp2, _grn2, _tr2 = self._measure_fill_in_box(frame_bgr, _ab2, ts=ts)
                        if _tr2 is not None and int(_tr2) >= 0 and float(_sp2) > 0.0:
                            _sp, _grn, _tr, _ab = _sp2, _grn2, _tr2, _ab2
                            _display_ab = (int(_display_ab[0]) + int(_dx), int(_display_ab[1]),
                                           int(_display_ab[2]), int(_display_ab[3]))
                            _tracked_ab = (int(_tracked_ab[0]) + int(_dx), int(_tracked_ab[1]),
                                           int(_tracked_ab[2]), int(_tracked_ab[3]))
                            self._det_active_box = _tracked_ab if self._det_measure_raw else _ab
                            if getattr(self, '_det_track_box', None) is not None:
                                _tb = tuple(int(v) for v in self._det_track_box)
                                self._det_track_box = (_tb[0] + int(_dx), _tb[1], _tb[2], _tb[3])
                            self._reseat_x_n += 1
                            self._det_diag['reseat_x'] = self._det_diag.get('reseat_x', 0) + 1
                        else:
                            # a shifted box that still reads nothing teaches the tracker nothing
                            self._measure_fill_in_box(frame_bgr, _ab, ts=ts)
                if self._det_occ_current_full_negative:
                    # One detector-negative callback consumes the budget whether
                    # it recovered, rejected, or was fully hidden.  Only a later
                    # positive *direct* read can replenish it.
                    self._det_occ_negative_bridge_used = True
                if _tr is not None and int(_tr) >= 0 and float(_sp) > 0.0 and int(_ab[3]) > 0:
                    _width_box = _tracked_ab if self._det_measure_raw else _ab
                    self._box_w_hist.append(float(_width_box[2]) / float(_width_box[3]))
                # READ-RESCUE (see the __init__ knob block): a failed held-box read queues
                # one priority/latest-frame worker result.  It never runs YOLO/ORT inline;
                # the accepted result hard-reseats serving on a later callback while this
                # frame remains an honest zero.
                if (self._read_rescue and _tr is not None and int(_tr) < 0
                        and ts is not None
                        and (float(ts) - self._det_last_rescue) >= self._rescue_gap_s):
                    self._det_last_rescue = float(ts)
                    self._det_read_rescue(frame_bgr, float(ts))
                # fill_coarse/raw_fill_pct always carries the COARSE row-walk value, so live
                # telemetry keeps a permanent sub-pixel-vs-coarse A/B breadcrumb per frame.
                _sp = float(_sp); _co = float(getattr(self, "_last_fill_coarse", _sp) or 0.0)
                # A retained box is not a measured white edge. In particular a
                # vanished/occluded ribbon's sentinel zero must not enter a timing
                # slope or create a rollback followed by a fictitious rapid rise.
                _white_read_valid = bool(
                    _tr is not None and int(_tr) >= 0
                    and np.isfinite(_sp) and np.isfinite(_co)
                    and 0.0 <= _sp <= 100.0 and 0.0 <= _co <= 100.0)
                _measurement_rejected = bool(
                    not _white_read_valid or self._det_occ_censored_reject
                    or self._det_pixel_ruler_reject)
                if (self._tracking_meter_style == "pill"
                        and not _measurement_rejected and _sp > 0.0):
                    self._note_pill_pickup(ts, _sp)
                if self._det_lifecycle:
                    # Per-lock fill bookkeeping: the PROVEN-rise latch that earns the extended
                    # coast (retired peak-hold demanded a real rise) + the white-ribbon presence
                    # that stands in for the retired red-presence survival corroboration.
                    self._det_last_presence = not _measurement_rejected
                    self._det_lock_read_n = int(getattr(self, "_det_lock_read_n", 0)) + 1
                    if self._det_last_presence and np.isfinite(_sp) and _sp > 0.0:
                        # A failed read's zero is absence, not the start of a rise.
                        # Before a rise has been proved, a material downward re-seat
                        # starts a new ordered episode. Do not retain its old peak:
                        # a falling leftover is not an upward-growing meter.
                        unproved = (self._det_lock_fill0 is None
                            or self._det_lock_fill_max - self._det_lock_fill0
                            < float(self._static_zone_rise_pp))
                        if (self._det_lock_fill0 is None or (unproved
                                and _sp < self._det_lock_fill0 - 2.5)):
                            self._det_lock_fill0 = _sp
                            self._det_lock_fill_max = _sp
                        elif _sp > self._det_lock_fill_max:
                            self._det_lock_fill_max = _sp
                        # ORDERED rise run (see the static-zone knob block): the peak above
                        # can be one jittery read; this cannot.
                        _pf = self._det_lock_prev_fill
                        if _pf is not None:
                            if _sp > _pf + self._fresh_up_pp:
                                self._det_lock_up_n += 1
                            elif _sp < _pf - self._fresh_up_pp:
                                self._det_lock_up_n = 0
                        self._det_lock_prev_fill = _sp
                    # A lifecycle-approved box becomes a side-priority hint only
                    # after its measured meter fill proves a real rise. A static
                    # one-frame proposal can therefore never steer the next scan;
                    # even a bad hint merely changes order because all three views
                    # remain in the cycle.
                    if (self._shot_armed_hw and self._det_lock_fill0 is not None
                            and (self._det_lock_fill_max - self._det_lock_fill0)
                            >= float(self._det_coast_rise_pp)):
                        _acx = float(_ab[0]) + float(_ab[2]) * 0.5
                        self._det_acq_last_side = (
                            'left' if _acx < float(self.W) * 0.5 else 'right')
                self.box = _ab
                self.conf = self.CONF_INIT
                self.last_fill = _sp
                self.last_coarse = _co
                # Independent detector-fill velocity (read()'s _vel_hist is décor-polluted).
                if ts is not None and not _measurement_rejected:
                    self._det_fill_hist.append((float(ts), _sp))
                    self._det_coarse_fill_hist.append((float(ts), _co))
                    _cut = float(ts) - 0.5
                    while self._det_fill_hist and self._det_fill_hist[0][0] < _cut:
                        self._det_fill_hist.popleft()
                    while (self._det_coarse_fill_hist
                           and self._det_coarse_fill_hist[0][0] < _cut):
                        self._det_coarse_fill_hist.popleft()
                _dvel = 0.0
                if len(self._det_fill_hist) >= 3:
                    _tt = np.array([p[0] for p in self._det_fill_hist], dtype=np.float64)
                    _ff = np.array([p[1] for p in self._det_fill_hist], dtype=np.float64)
                    if _tt[-1] > _tt[0]:
                        _dvel = float(np.polyfit(_tt - _tt[0], _ff, 1)[0])   # pct/s
                _obox = _display_ab
                if self._det_occ_recovered:
                    if self._det_occ_kind == "top_censored_negative_once":
                        _occ_conf = min(0.75, max(
                            0.65, 0.65 + 0.10 * float(self._det_occ_support)))
                    else:
                        _occ_conf = min(0.90, max(
                            0.65, 0.65 + float(self._det_occ_support)))
                else:
                    _occ_conf = 1.0
                s = {"detected": True, "meter_present": True,
                     "fill": _sp, "fill_coarse": _co, "bbox": _obox,
                     "fill_estimator_mode": self._last_fill_estimator_mode,
                     "fill_estimator_generation": self._last_fill_estimator_generation,
                     "confidence": _occ_conf, "velocity_pct_s": _dvel,
                     "top_row": int(_tr) if _tr is not None else -1, "green": _grn,
                     "rejection_reason": "", "rise_state": self._rise_state(_sp),
                     "stage": "detector_fill",
                     "measurement_missing": not _white_read_valid}
                if self._det_occ_censored_reject or self._det_pixel_ruler_reject:
                    _pixel_reject_reason = ("detector_pixel_geometry_missing"
                        if self._det_pixel_ruler_reject else
                        "detector_fill_top_occluded" if self._det_occ_censored_reject
                        else "detector_white_ribbon_missing")
                    # Keep lifecycle/tracker state available for reacquisition,
                    # but publish absence and no velocity/green target. No zero
                    # or rejected edge was appended to either timing history.
                    self.last_fill = 0.0
                    self.last_coarse = 0.0
                    s = {"detected": False, "meter_present": False,
                         "fill": 0.0, "fill_coarse": 0.0,
                         "bbox": [0, 0, 0, 0],
                         "fill_estimator_mode": "",
                         "fill_estimator_generation": 0,
                         "confidence": 0.0, "velocity_pct_s": 0.0,
                         "top_row": -1, "green": None,
                         "rejection_reason": _pixel_reject_reason,
                         "rise_state": "", "stage": _pixel_reject_reason}
                # DETECTOR-FILL STRUCTURE LATCH (see the __init__ knob block: the live
                # silent-shot chain was read()'s COLOUR-masked sample starving
                # _qualify_gameplay_sample under a meter_color drift, while this
                # colour-agnostic sample -- the one actually emitted -- carried the
                # green chevron on 91% of early rise frames). A low-fill frame inside
                # the CURRENT hardware epoch may stamp immediately when it contains
                # both the meter's green chevron and a measured white ribbon. That is
                # the strongest onset signature available and lets native ownership
                # consume the next rising frame instead of losing 2-4 frames while the
                # lifecycle box transitions acquiring -> locked. High/static boxes keep
                # the stricter two-consecutive-LOCKED-frame rule, preserving the green-
                # ball-VFX false-lock defence. Latch-only: publication/authorization
                # remains with the existing qualify and native two-frame rise gates.
                _df_same_epoch = (
                    _detect_shot_epoch > 0 and self._shot_armed_hw
                    and self._physical_shot_epoch == _detect_shot_epoch)
                _df_has_structure = (
                    not _measurement_rejected and _grn is not None
                    and _tr is not None and int(_tr) >= 0)
                _df_low_rise = (
                    np.isfinite(_co) and 0.0 < float(_co) <= self._ghost_press_low_pct)
                _df_locked = not self._det_lifecycle or self._det_state == 'locked'
                if self._detfill_green_latch and self._require_gameplay_eligibility:
                    if _df_same_epoch and _df_has_structure and _df_low_rise:
                        self._df_green_streak = max(1, self._df_green_streak)
                        self._latch_gameplay_structure_proof(_detect_shot_epoch)
                    elif _df_locked:
                        # At high fill, require repeated same-box evidence. A coasting
                        # box crossed once by a green-ball VFX cannot build this streak.
                        if _df_has_structure:
                            self._df_green_streak += 1
                        else:
                            self._df_green_streak = 0
                        if self._df_green_streak >= 2 and _df_same_epoch:
                            self._latch_gameplay_structure_proof(_detect_shot_epoch)
                    else:
                        self._df_green_streak = 0
                else:
                    self._df_green_streak = 0
                # EARLY CAPLESS RISE PROOF.  Difficult/deep meters may not render a
                # green chevron until after the 20/25% ownership rung, even though the
                # detector already owns a real white ribbon rising from zero.  Three
                # distinct fresh locator frames on one lifecycle generation, all in
                # the evidence-backed 0..20% onset band and rising >=4pp end-to-end,
                # may stamp the existing structure bit.  This changes no publication,
                # ownership, confidence, or timing rule.
                if _df_nogreen_locator_fresh:
                    if _grn is None:
                        self._advance_detfill_nogreen_rise(
                            shot_epoch=_detect_shot_epoch,
                            lock_generation=_df_nogreen_locator_generation,
                            source_ts=_df_nogreen_locator_ts,
                            sample_ts=ts,
                            coarse_fill=_co,
                            locator_box=_df_nogreen_locator_box,
                            white_ribbon=bool(not _measurement_rejected
                                              and _tr is not None and int(_tr) >= 0),
                            locator_fresh=True)
                    else:
                        self._reset_detfill_nogreen_evidence()
            except Exception:
                # The white-ribbon measurement is authoritative when a detector
                # box is held. Falling back to read() here can publish a plausible
                # rising decor trace with confidence 1.0 after a measurement fault.
                if self._physical_shot_epoch == _detect_shot_epoch:
                    self._clear_gameplay_structure_proof()
                    self._gameplay_lock_authorized = False
                    if (isinstance(_census_before, dict)
                            and self._ep_census is _census_before
                            and self._ep_census.get("epoch") == _census_epoch_before):
                        self._ep_census["latched"] = _census_latched_before
                    # A late exception can occur after rise/coast or tracker
                    # state was updated. Retire this partial lock, but never
                    # erase a newer arm installed by the command thread.
                    self._det_reset_lock_state()
                    self._det_active_box = None
                    self._det_warm_pos = None
                    self._det_warm_ts = -1.0e9
                    self._det_occ_locator_source_positive = False
                    self.conf = 0.0; self.box = None; self.tmpl = None
                    self.last_fill = 0.0; self.last_coarse = 0.0
                    self.last_debug = {"stage": "detector_fill_exception"}
                self._det_diag["fill_exception"] = (
                    self._det_diag.get("fill_exception", 0) + 1)
                s = {"detected": False, "meter_present": False, "fill": 0.0,
                     "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                     "stage": "detector_fill_exception", "confidence": 0.0,
                     "velocity_pct_s": 0.0,
                     "rejection_reason": "detector_fill_exception", "rise_state": ""}
        else:
            self._det_fill_hist.clear()
            self._det_coarse_fill_hist.clear()
            self._df_green_streak = 0    # the chevron streak is a property of a HELD box
            self._reset_detfill_nogreen_evidence()
        # DETECTOR IS THE SOLE GATE: when the detector is active but holds NO meter (none found
        # within the hold window), suppress ANY colour-reader lock this frame. read()'s white-blind
        # path otherwise flickers onto décor -- the white backboard/goal post, a jersey, a court
        # line -- exactly the "locks onto the goal post" the owner saw. With the detector present it
        # is the only authority: meter held -> the direct-fill override above already emitted it;
        # no meter held -> nothing is a valid lock, so drop read()'s output. (Detector absent ->
        # this is inert and the shipped colour reader stands unchanged.)
        if (self._meter_detector is not None and self._det_active_box is None
                and bool(s.get("detected"))):
            self.conf = 0.0; self.box = None; self.tmpl = None
            s = {"detected": False, "meter_present": False, "fill": 0.0,
                 "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": "detector_no_meter",
                 "confidence": 0.0, "velocity_pct_s": 0.0,
                 "rejection_reason": "detector_no_meter", "rise_state": ""}
        # FAKE-LOCK / DEAD-HOLD BREAKER (path-complete: applied at the production boundary so it
        # covers BOTH a real static-red re-lock AND the coast / meter-memory hold that an
        # intermittent dÃ©cor re-lock keeps alive by resetting the coast cap). A live shot meter's
        # fill CHANGES as it rises and the meter then vanishes within ~1-2 s; a detection that
        # reports a BYTE-IDENTICAL fill frame after frame is static red DÃ‰COR (a UI panel / jersey /
        # logo the colour gate cannot tell from a meter) or a stuck hold -- caught live holding a
        # frozen fill for 120-222 frames (session_20260717_192510: 25.55 for 222f, 50.61 / 46.78 for
        # 181f, 91.61 for 120f), feeding the timing stack a dead, wrong value the whole time. Once
        # the hold is non-physical, SUPPRESS the output; read() keeps the (colour-valid) box warm so
        # a genuine restart re-locks instantly, and normal reporting resumes the instant the fill
        # actually moves (counter resets). Mid-fills (<85%) are never a legit hold -> tight cap; a
        # near-tip hold (>=85%, a real peak / green make-window) gets a looser cap so a real ~0.8 s
        # peak survives. Guarded on fill>0.5 so a legit run of true-zero reads never trips it.
        _bk_det = bool(s.get("detected"))
        # On the detector-fill path the breakers judge the COARSE fill on purpose: the
        # static/fake-lock breaker's identity test is "byte-identical fill frame after
        # frame", which is exactly what static decor produces under the row-quantized walk.
        # The sub-pixel fill carries ~0.05-0.3pp of honest per-frame measurement noise,
        # which would defeat that identity test and quietly disarm the false-lock defense;
        # the coarse value preserves the breaker's shipped semantics bit-for-bit. Every
        # other stage keeps judging "fill" exactly as before (their "fill_coarse" is the
        # raw pre-EMA value, a different quantity).  Every detector-fill press guard below
        # must use this SAME canonical value too.  Mixing the sub-pixel ruler into the 40%
        # ownership boundary can turn one physical sample into 41.5% here while native sees
        # 38.7%: the reader then quarantines a sample the engine is explicitly able to own.
        _bk_fill = float((s.get("fill_coarse") if s.get("stage") == "detector_fill"
                          else s.get("fill", 0.0)) or 0.0)
        # CAPLESS-LOCK BREAKER (WHITE ONLY). The static breaker above catches a
        # BYTE-FROZEN fill; it cannot catch a false lock whose fill wobbles. On a
        # WHITE meter that gap is wide open, because bright neutral pixels are the
        # most abundant thing in an arena and a lock on a jersey or a painted lane
        # line reads a small, drifting fill indefinitely.
        #
        # Measured on session_20260826_204615 (live 2K27, owner shooting):
        #   * 4 phantom locks, the worst holding 15.3s with the box ~stationary and
        #     a mean fill of 5.5% -- the "FILL 6% on bare court" the owner
        #     photographed. `_coast_n` never tripped its cap because the phantom
        #     RE-FINDS its white column every frame, which resets the coast.
        #   * longest run with no green cap: GENUINE shots 56 frames (~1.75s);
        #     PHANTOM locks 141 frames and beyond. The tails separate cleanly.
        #
        # The green apex is the meter's identity and decor does not have one. A
        # brief loss is normal (occlusion, a player's arm), so this bounds the
        # loss rather than forbidding it. Deliberately NOT gated on arm grace: the
        # owner was physically arming throughout, which is exactly what suppresses
        # the static breaker, and a capless lock is invalid whatever the arm says.
        # [ORION_READER_GHOST_PRESS_BREAK] POST-PRESS LEFTOVER-METER BREAKER (see the __init__
        # knob block for the measured mechanism and the fail-closed argument). Runs at the
        # production boundary like the breakers below, but INSIDE the armed press window on
        # purpose -- the grace machinery that shields mid-shot coasted peaks from the static
        # breaker is exactly what let the leftover meter bridge every press. The guard is
        # narrower than any grace: it exists only between a press and that press's OWN first
        # low-fill sighting, only against locks reading at/above the engine's first-sight
        # bound (which the engine can never own), and only when their nonzero reads hold a
        # static spread no real rise can produce. detector_fill-stage only: the colour path's
        # synthetic/test frames never carry this stage, and live serving is detector-owned.
        if (self._ghost_press_break and _bk_det
                and s.get("stage") == "detector_fill"
                and self._shot_armed_hw and ts is not None
                and self._hw_arm_ts is not None
                and int(getattr(self, "_hw_arm_grace_epoch", 0) or 0)
                == int(self._physical_shot_epoch or 0)
                and (float(ts) - float(self._hw_arm_ts)) <= self._ghost_press_window_s
                and not self._press_low_seen):
            _gf = _bk_fill
            _gbb = s.get("bbox") or [0, 0, 0, 0]
            _gcx = float(_gbb[0]) + float(_gbb[2]) * 0.5
            _gcy = float(_gbb[1]) + float(_gbb[3]) * 0.5
            _gr = max(32.0, self._ghost_zone_radius_scale * max(8.0, float(_gbb[2])))
            _in_zone = (self._press_ghost_zone is not None
                        and abs(_gcx - self._press_ghost_zone[0]) <= _gr
                        and abs(_gcy - self._press_ghost_zone[1]) <= _gr * 2.0)
            _evict = False
            _suppress_hold = False
            _plaus_on = bool(self._press_onset_plaus)
            if _in_zone:
                # LEVEL-AWARE zone test (see __init__): only reads near the ghost's own
                # remembered fill level are the ghost; a read far below it is a meter
                # rendering inside the zone (measured epoch 18: real onset read 25.5/31.1
                # under a ~93% leftover -- above any fixed low threshold, far below the
                # level). The band floor never sinks below the low exemption, so a
                # low-level ghost cannot swallow genuine onset reads.
                _gz_level = (float(self._press_ghost_level)
                             if self._press_ghost_level is not None else 100.0)
                _gz_band_lo = max(_gz_level - self._ghost_zone_band_pp,
                                  self._ghost_zone_low_pct + 1.0)
                if (0.0 < _gf < _gz_band_lo
                        and (not _plaus_on
                             or _gf < self._stale_press_drop_pct)):
                    # Real-onset candidate: publish (a single frame is harmless to the
                    # engine); two CONSECUTIVE candidates stand the guard down -- one
                    # blurred partial read of the ghost cannot make two in a row.
                    # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] a sub-band read AT/ABOVE the
                    # engine's 40% anchor bound has no publish value (a first sight >=40
                    # is refused, burning the episode) -- it is the leftover draining
                    # through the band; follow-evict it below instead. And the pair must
                    # RISE like an onset: two sub-band reads of a fading leftover decline
                    # (measured epoch 51: the fade walked 12.7 down through the band and
                    # stood the guard down for a 51.4 re-lock the engine then owned).
                    self._press_ghost_zone_low_n += 1
                    self._press_ghost_zero_n = 0
                    _pair_ok = ((not _plaus_on)
                                or self._press_onset_pair_advance(_gf, float(ts)))
                    if self._press_ghost_zone_low_n >= 2 and _pair_ok:
                        self._press_low_seen = True
                        self._press_ghost_zone = None
                        self._press_ghost_level = None
                        self._press_ghost_zone_low_n = 0
                        self._press_ghost_full_nofind_n = 0
                        self._press_ghost_reads.clear()
                        self._ghost_press_flush_summary("real_onset_in_zone")
                elif _gf > 0.0:
                    # The quarantined leftover meter re-locked (or was still held): follow
                    # its drift and level, suppress the read, evict the lock again.
                    self._press_ghost_zone = (_gcx, _gcy)
                    if self._press_ghost_level is not None:
                        self._press_ghost_level = (self._press_ghost_level * 0.8
                                                   + _gf * 0.2)
                    else:
                        self._press_ghost_level = _gf
                    self._press_ghost_zone_low_n = 0
                    self._press_ghost_zero_n = 0
                    self._press_ghost_full_nofind_n = 0
                    self._press_onset_cand = None   # an in-band read breaks the onset pair
                    _evict = True
                else:
                    # Zero read on the quarantined box: the fading/blurred leftover OR the
                    # real onset's first EMPTY frame rendering in-zone (see the zero-read
                    # hold knob in __init__: 51/336 live evictions were fill=0.0 and the
                    # late cluster sat exactly where the real meter renders). Suppress the
                    # publish either way, but only evict after _ghost_zero_evict_n
                    # CONSECUTIVE zeros -- an empty real meter reads nonzero within a frame
                    # or two and is then classified by the level band above, while a held
                    # zero on the fading ghost still dies, ~2 frames later than before.
                    # The candidate pair is left alone: a blur frame between two genuine
                    # onset reads must not reset the escape.
                    self._press_ghost_zero_n += 1
                    if self._press_ghost_zero_n >= self._ghost_zero_evict_n:
                        _evict = True
                    else:
                        _suppress_hold = True
            elif 0.0 < _gf <= self._ghost_press_low_pct:
                # This press's meter has been seen low outside any quarantine: every later
                # high fill is the real rise (or its settle plateau, which the release
                # grading needs). Guard stands down.
                # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] STRENGTHENED 2026-08-31: a single
                # low read is no longer enough -- all four live authority losses (epochs
                # 32/50/51/58) were one garbage-low read (a pan-blurred/fading leftover, at
                # press ages where NO real meter can render) standing the guard down for
                # the very next high re-lock. The stand-down now needs a RISING pair, the
                # same evidence a real onset always produces; the first low read still
                # publishes unchanged (a single low frame is harmless to the engine).
                if (not _plaus_on) or self._press_onset_pair_advance(_gf, float(ts)):
                    self._press_low_seen = True
                    self._press_ghost_zone = None
                    self._press_ghost_zone_low_n = 0
                    self._press_ghost_full_nofind_n = 0
                    self._press_ghost_reads.clear()
                    self._ghost_press_flush_summary("real_onset_low")
            elif _gf > self._ghost_press_low_pct:
                # Rolling-window identification: _ghost_press_frames nonzero reads whose
                # spread stays inside _ghost_press_spread_pp. A real rise crosses this band
                # at ~6pp/frame (30ms cadence; ~3pp at 60fps) and cannot hold the spread;
                # zero reads (pan blur) neither feed nor reset the window, so the ghost's
                # own flicker cannot shield it.
                self._press_ghost_reads.append(_gf)
                if (len(self._press_ghost_reads) >= self._ghost_press_frames
                        and (max(self._press_ghost_reads) - min(self._press_ghost_reads))
                        <= self._ghost_press_spread_pp):
                    self._press_ghost_zone = (_gcx, _gcy)
                    self._press_ghost_level = (sum(self._press_ghost_reads)
                                               / len(self._press_ghost_reads))
                    self._press_ghost_zone_low_n = 0
                    self._press_ghost_full_nofind_n = 0
                    self._press_ghost_reads.clear()
                    _evict = True
            if _evict or _suppress_hold:
                self._pw_ghost_static += 1
                if _evict:
                    # LOG IDEMPOTENCE (see __init__): the eviction itself stays per-frame
                    # (it is what frees the single locker), but the ERROR line is emitted
                    # only for a NEW ghost (first eviction of the press, or the zone centre
                    # moved >64px -- a different object) or after 0.5s; repeats go to DEBUG
                    # and the per-press summary carries the totals.
                    self._press_ghost_evict_total += 1
                    _glog_new = (self._press_ghost_log_zone is None
                                 or abs(_gcx - self._press_ghost_log_zone[0]) > 64.0
                                 or abs(_gcy - self._press_ghost_log_zone[1]) > 64.0)
                    if _glog_new or (float(ts) - self._press_ghost_log_ts) >= 0.5:
                        _acq_logger.error(
                            "GHOST LOCK DROPPED POST-PRESS: epoch=%d fill=%.1f%% "
                            "zone=(%d,%d) (static/no-low-sighting leftover meter "
                            "quarantined so the real onset can be acquired)",
                            int(self._physical_shot_epoch or 0), _gf,
                            int(_gcx), int(_gcy))
                        self._press_ghost_log_ts = float(ts)
                        self._press_ghost_log_zone = (_gcx, _gcy)
                    else:
                        _acq_logger.debug(
                            "GHOST LOCK DROPPED POST-PRESS (repeat): epoch=%d fill=%.1f%% "
                            "zone=(%d,%d)",
                            int(self._physical_shot_epoch or 0), _gf,
                            int(_gcx), int(_gcy))
                else:
                    self._press_ghost_suppress_total += 1
                    _acq_logger.debug(
                        "GHOST ZERO-READ HELD POST-PRESS: epoch=%d zone=(%d,%d) run=%d",
                        int(self._physical_shot_epoch or 0), int(_gcx), int(_gcy),
                        int(self._press_ghost_zero_n))
                s = {"detected": False, "meter_present": False, "fill": 0.0,
                     "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                     "stage": "ghost_press_suppressed", "confidence": 0.0,
                     "velocity_pct_s": 0.0,
                     "rejection_reason": "ghost_static_press", "rise_state": ""}
                try:
                    self.last_debug = {"stage": "ghost_press_suppressed",
                                       "ghost_fill": round(_gf, 2)}
                except Exception:
                    pass
                if _evict:
                    self.conf = 0.0; self.box = None; self.tmpl = None
                    try:
                        self._det_reset_lock_state()
                        self._det_warm_pos = None
                        self._det_warm_ts = -1.0e9
                        self._det_active_box = None
                    except Exception:
                        pass
                    # [ORION_READER_GHOST_FORGET_LOCATOR] Without this the proposer re-proposes
                    # the very box just evicted -- on co-location alone -- and the reader
                    # re-locks it next frame: the measured 2-11 evictions / +5..+12 locks per
                    # press with drops flat. The quarantine zone already knows WHERE the ghost
                    # is; the proposer must not.
                    self._det_forget_locator_position(
                        'ghost_press_evict',
                        box=(_gbb[0], _gbb[1], _gbb[2], _gbb[3]), ts=ts)
                    self._arm_press_fresh_zone(
                        (_gbb[0], _gbb[1], _gbb[2], _gbb[3]), 'ghost_press_evict')
                _bk_det = False; _bk_fill = 0.0
        # [ORION_READER_COLD_FIRST_READ_VETO] (see the __init__ knob block for the measured
        # live failure). Runs AFTER the ghost breaker on purpose: the breaker must see every
        # read at full cadence (zone-following and eviction cadence unchanged), and a frame
        # the breaker already suppressed never reaches here (_bk_det is False). What is left
        # is exactly the class the breaker does not cover: an OUT-OF-ZONE cold lock whose
        # first 1-2 reads measure garbage-high on the raw proposal box -- the stick-shot
        # first-sight poison and the second-leftover 2-frame pre-identification race. The
        # lock is KEPT (no reset, no warm clear): the very next read publishes if sane.
        # PRESS-SCOPED like the ghost guard (same gate, same fail-closed shape): it exists
        # only between a physical press and that press's own first low-fill sighting,
        # bounded by the same window. Outside a press -- idle relocks, the post-release
        # settle plateau (whose press saw the rise's low fills), mid-shot rescue relocks
        # after the onset published -- it is inert, so established lifecycle behaviour
        # (and its pinned tests) are untouched.
        if (self._cold_first_read_veto and _bk_det
                and s.get("stage") == "detector_fill"
                and self._det_lifecycle
                and self._shot_armed_hw and ts is not None
                and self._hw_arm_ts is not None
                and int(getattr(self, "_hw_arm_grace_epoch", 0) or 0)
                == int(self._physical_shot_epoch or 0)
                and (float(ts) - float(self._hw_arm_ts)) <= self._ghost_press_window_s
                and not self._press_low_seen
                and not bool(getattr(self, "_det_lock_was_warm", False))
                and int(getattr(self, "_det_lock_read_n", 0)) <= self._cold_first_read_n
                and _bk_fill >= self._cold_first_read_pct):
            _vf = _bk_fill
            self._pw_cold_first += 1
            _acq_logger.debug(
                "COLD FIRST-READ VETOED: read#%d fill=%.1f%% >= %.1f%% (unproven cold-lock "
                "read withheld; the engine could never anchor on it)",
                int(getattr(self, "_det_lock_read_n", 0)), _vf, self._cold_first_read_pct)
            s = {"detected": False, "meter_present": False, "fill": 0.0,
                 "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                 "stage": "cold_first_read_veto", "confidence": 0.0,
                 "velocity_pct_s": 0.0,
                 "rejection_reason": "cold_first_read_unproven", "rise_state": ""}
            try:
                self.last_debug = {"stage": "cold_first_read_veto",
                                   "vetoed_fill": round(_vf, 2)}
            except Exception:
                pass
            # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] a withheld read is not evidence:
            # the garbage 72.9 measured on press 15's cold lock sat in _det_fill_hist and
            # made the NEXT press's stale drop kill a genuine 27->34 RISING lock (measured
            # session_20260830_191051 press 16). What the veto refuses to publish, the
            # held-fill judgment must not trust either.
            try:
                if self._det_fill_hist and self._det_fill_hist[-1][0] == float(ts):
                    self._det_fill_hist.pop()
                if (self._det_coarse_fill_hist
                        and self._det_coarse_fill_hist[-1][0] == float(ts)):
                    self._det_coarse_fill_hist.pop()
            except Exception:
                pass
            _bk_det = False; _bk_fill = 0.0
        # [ORION_READER_PRESS_ONSET_PLAUSIBILITY] PRESS-CLOCK SERVE VETO (see the __init__
        # knob block for the live failure and the two invariants). Runs AFTER the ghost
        # breaker and the cold veto on purpose: the breaker keeps full-cadence sight of
        # every read (zone-following, rolling-window identification and eviction cadence
        # unchanged), and what reaches here is exactly the read the engine would otherwise
        # anchor on. Same press-scoped fail-closed gate as the guards above: it exists only
        # between a physical press and that press's own genuine low sighting, and it only
        # ever WITHHOLDS a read no real meter could have produced at this press age / after
        # the last published serve. On a veto the lock is evicted (it is holding the single
        # locker the real onset needs -- measured, the fading leftover holds it for 100s of
        # ms) and the quarantine zone is armed at the read's box, so the same object's next
        # reads are handled by the level-band machinery instead of re-racing this veto.
        if (self._press_onset_plaus and _bk_det
                and s.get("stage") == "detector_fill"
                and self._shot_armed_hw and ts is not None
                and self._hw_arm_ts is not None
                and int(getattr(self, "_hw_arm_grace_epoch", 0) or 0)
                == int(self._physical_shot_epoch or 0)
                and (float(ts) - float(self._hw_arm_ts)) <= self._ghost_press_window_s
                and not self._press_low_seen):
            _pf = _bk_fill
            if _pf > 0.0:
                _age_allow = self._press_fill_allowance(float(ts))
                _step_allow = self._press_step_allowance(float(ts))
                # BOTH rules judge only reads AT/ABOVE the engine's 40% anchor bound: a
                # sub-40 read is never withheld (harmless alone -- the engine cannot
                # complete ownership on it without a plausible rise, and a genuine
                # POP-IN/carried-over serve is often sub-40 at ages/steps the clocks
                # would refuse; clamping those benched real shots in the replay A/B).
                # ONSET CLOCK: no genuine meter can read >=40 before the press clock
                # allows it (the envelope leads any real rise by >=2x).
                _age_veto = (_pf >= self._stale_press_drop_pct and _pf > _age_allow)
                # RISE STEP: a jump landing >=40 faster than the ribbon can rise from the
                # last published serve is a tracker OBJECT SWITCH, never the same meter
                # (the live failures jumped +38.7..+89.2pp in <=85ms, landing 51-94).
                _step_veto = (not _age_veto
                              and _pf >= self._stale_press_drop_pct
                              and _step_allow is not None and _pf > _step_allow)
                if _age_veto or _step_veto:
                    self._press_implausible_n += 1
                    _pbb = s.get("bbox") or [0, 0, 0, 0]
                    _pcx = float(_pbb[0]) + float(_pbb[2]) * 0.5
                    _pcy = float(_pbb[1]) + float(_pbb[3]) * 0.5
                    _page_ms = (float(ts) - float(self._hw_arm_ts)) * 1000.0
                    _pallow = _age_allow if _age_veto else float(_step_allow)
                    _kind = "onset_clock" if _age_veto else "rise_step"
                    if not self._press_implausible_logged:
                        _acq_logger.error(
                            "PRESS-IMPLAUSIBLE SERVE VETOED: epoch=%d kind=%s fill=%.1f%% "
                            "allow=%.1f%% press_age_ms=%d zone=(%d,%d) (no real meter can "
                            "read this here on this press's clock)",
                            int(self._physical_shot_epoch or 0), _kind, _pf, _pallow,
                            int(_page_ms), int(_pcx), int(_pcy))
                        self._press_implausible_logged = True
                    else:
                        _acq_logger.debug(
                            "PRESS-IMPLAUSIBLE SERVE VETOED (repeat): epoch=%d kind=%s "
                            "fill=%.1f%% allow=%.1f%% press_age_ms=%d",
                            int(self._physical_shot_epoch or 0), _kind, _pf, _pallow,
                            int(_page_ms))
                    # A read at/above the engine's anchor bound is CERTAINLY not this
                    # press's onset (the envelope leads any genuine rise): evict the lock
                    # (it is holding the single locker the real onset needs), and on the
                    # onset-clock veto also arm the quarantine at its box so the leftover's
                    # follow-up reads are owned by the level-band machinery.
                    if (_age_veto and self._ghost_press_break
                            and float(_pbb[2]) > 0.0):
                        self._press_ghost_zone = (_pcx, _pcy)
                        self._press_ghost_level = _pf
                        self._press_ghost_zone_low_n = 0
                        self._press_ghost_zero_n = 0
                        self._press_ghost_full_nofind_n = 0
                    self.conf = 0.0; self.box = None; self.tmpl = None
                    try:
                        self._det_reset_lock_state()
                        self._det_warm_pos = None
                        self._det_warm_ts = -1.0e9
                        self._det_active_box = None
                    except Exception:
                        pass
                    # A withheld read is not held-fill evidence for the next press either.
                    try:
                        if (self._det_fill_hist
                                and self._det_fill_hist[-1][0] == float(ts)):
                            self._det_fill_hist.pop()
                        if (self._det_coarse_fill_hist
                                and self._det_coarse_fill_hist[-1][0] == float(ts)):
                            self._det_coarse_fill_hist.pop()
                    except Exception:
                        pass
                    s = {"detected": False, "meter_present": False, "fill": 0.0,
                         "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                         "stage": "press_onset_veto", "confidence": 0.0,
                         "velocity_pct_s": 0.0,
                         "rejection_reason": "press_onset_implausible", "rise_state": ""}
                    try:
                        self.last_debug = {"stage": "press_onset_veto",
                                           "veto_kind": _kind,
                                           "vetoed_fill": round(_pf, 2),
                                           "allow": round(_pallow, 2)}
                    except Exception:
                        pass
                    _bk_det = False; _bk_fill = 0.0
                else:
                    # The serve stands: it becomes the press's step-clamp reference --
                    # and a SUSTAINED plausible rise is onset proof in its own right.
                    # A mid-rise catch may never dip <= the low threshold (measured epoch
                    # 52: first sight 31.4), so the low-pair path alone would leave the
                    # guard up through the settle, where the static-spread identification
                    # would then wrongly evict the shot's own plateau. Three consecutive
                    # published steps rising within the physical cap are something no
                    # fade, drain or static leftover can produce.
                    if (self._press_last_pub_fill is not None
                            and self._press_last_pub_ts is not None
                            and self._press_pair_min_rise_pp
                            <= _pf - float(self._press_last_pub_fill)
                            <= self._press_step_margin_pp
                            + self._press_max_rate_pct_ms
                            * max(0.0, (float(ts) - float(self._press_last_pub_ts))
                                  * 1000.0)):
                        self._press_rise_run += 1
                    else:
                        self._press_rise_run = 0
                    self._press_last_pub_fill = _pf
                    self._press_last_pub_ts = float(ts)
                    if self._press_rise_run >= 3 and not self._press_low_seen:
                        self._press_low_seen = True
                        self._press_ghost_zone = None
                        self._press_ghost_level = None
                        self._press_ghost_zone_low_n = 0
                        self._press_ghost_full_nofind_n = 0
                        self._press_ghost_reads.clear()
                        self._ghost_press_flush_summary("real_onset_rise")
        # [ORION_READER_FRESH_AFTER_GHOST] A ghost that was dropped at the press, evicted
        # post-press or retired after full absence leaves its ZONE under a fresh-onset
        # requirement for the rest of the press. Deliberately AFTER the plausibility block so
        # the mid-rise escape (_press_rise_run, three consecutive plausible rising serves) is
        # still fed and can stand the requirement down for an onset that never reads low --
        # only the publication is withheld, and only for reads at/above the engine's own 40%
        # first-sight bound, which it could not anchor on anyway.
        if (self._fresh_after_ghost and self._ghost_press_break and _bk_det
                and s.get("stage") == "detector_fill"
                and self._press_fresh_zone is not None
                and not self._press_low_seen
                and self._shot_armed_hw and ts is not None
                and self._hw_arm_ts is not None
                and int(getattr(self, "_hw_arm_grace_epoch", 0) or 0)
                == int(self._physical_shot_epoch or 0)
                and (float(ts) - float(self._hw_arm_ts)) <= self._ghost_press_window_s
                and _bk_fill >= self._stale_press_drop_pct):
            _fbb = s.get("bbox") or [0, 0, 0, 0]
            _fcx = float(_fbb[0]) + float(_fbb[2]) * 0.5
            _fcy = float(_fbb[1]) + float(_fbb[3]) * 0.5
            _fr = max(32.0, self._ghost_zone_radius_scale * max(8.0, float(_fbb[2])))
            if (abs(_fcx - self._press_fresh_zone[0]) <= _fr
                    and abs(_fcy - self._press_fresh_zone[1]) <= _fr * 2.0):
                self._press_fresh_withheld += 1
                self._pw_fresh_zone += 1
                if not self._press_fresh_zone_logged:
                    self._press_fresh_zone_logged = True
                    _acq_logger.error(
                        "RESEED REFUSED: epoch=%d fill=%.1f%% zone=(%d,%d) - a retired ghost "
                        "held this zone; a lock here is withheld until this press sights its "
                        "own meter low and rising",
                        int(self._physical_shot_epoch or 0), _bk_fill,
                        int(self._press_fresh_zone[0]), int(self._press_fresh_zone[1]))
                else:
                    _acq_logger.debug(
                        "RESEED REFUSED (repeat): epoch=%d fill=%.1f%%",
                        int(self._physical_shot_epoch or 0), _bk_fill)
                s = {"detected": False, "meter_present": False, "fill": 0.0,
                     "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                     "stage": "ghost_zone_unproven", "confidence": 0.0,
                     "velocity_pct_s": 0.0,
                     "rejection_reason": "ghost_zone_unproven", "rise_state": ""}
                try:
                    self.last_debug = {"stage": "ghost_zone_unproven",
                                       "zone_fill": round(_bk_fill, 2)}
                except Exception:
                    pass
                _bk_det = False; _bk_fill = 0.0
        if self._press_low_seen and self._press_fresh_zone is not None:
            self._press_fresh_zone = None        # this press proved its own onset
            self._press_fresh_zone_logged = False
        # [ORION_READER_STATIC_ZONE_QUARANTINE] Press-INDEPENDENT (the owner's court-line false
        # locks happened with no press within 40s). A position that has produced
        # _static_zone_strikes locks that never rose publishes nothing until a lock there
        # proves a rise; the moment one does, the record is retired and the zone is normal
        # again. A real meter pays at most the frame it takes to gain _static_zone_rise_pp.
        if (self._static_zone_q and _bk_det and ts is not None
                and s.get("stage") == "detector_fill"):
            _zb = s.get("bbox") or [0, 0, 0, 0]
            _zq = self._static_zone_quarantined(_zb, float(ts))
            if _zq is not None:
                _zf0 = getattr(self, "_det_lock_fill0", None)
                _zrise = ((float(getattr(self, "_det_lock_fill_max", -1.0) or -1.0)
                           - float(_zf0)) if _zf0 is not None else -1.0)
                _zordered = int(getattr(self, "_det_lock_up_n", 0) or 0)
                _zarmed = bool(self._shot_armed_hw)
                if (_zrise >= self._static_zone_rise_pp
                        and (_zarmed or _zordered >= self._fresh_up_req())):
                    self._static_zone_release(
                        _zq, 'lock_proved_rise_armed' if _zarmed else 'lock_proved_ordered_rise')
                else:
                    self._static_zone_withheld += 1
                    self._pw_static_zone += 1
                    try:
                        _wc = (float(_zb[0]) + float(_zb[2]) * 0.5,
                               float(_zb[1]) + float(_zb[3]) * 0.5)
                        _wl = self._static_zone_withheld_last
                        if _wl is not None and abs(_wc[0] - _wl[0]) <= 6.0 \
                                and abs(_wc[1] - _wl[1]) <= 6.0:
                            self._static_zone_withheld_repeat += 1
                        self._static_zone_withheld_last = _wc
                    except (TypeError, ValueError, IndexError):
                        pass
                    _acq_logger.debug(
                        "STATIC ZONE WITHHELD: zone=(%d,%d) strikes=%d fill=%.1f%% rise=%.1fpp "
                        "ordered=%d/%d armed=%d",
                        int(_zq['cx']), int(_zq['cy']), int(_zq['n']), _bk_fill, _zrise,
                        _zordered, self._fresh_up_req(), int(_zarmed))
                    s = {"detected": False, "meter_present": False, "fill": 0.0,
                         "fill_coarse": 0.0, "bbox": [0, 0, 0, 0],
                         "stage": "static_zone_quarantined", "confidence": 0.0,
                         "velocity_pct_s": 0.0,
                         "rejection_reason": "static_zone_quarantined", "rise_state": ""}
                    try:
                        self.last_debug = {"stage": "static_zone_quarantined",
                                           "zone_fill": round(_bk_fill, 2)}
                    except Exception:
                        pass
                    _bk_det = False; _bk_fill = 0.0
        # [ORION_READER_BOX_LATCH] Everything above has had its say: what survives here is a
        # detector-fill read this press OWNS (its own onset was sighted). Latch the geometry.
        if (self._box_latch and _bk_det and self._press_low_seen
                and s.get("stage") == "detector_fill"
                and self._shot_armed_hw
                and int(self._physical_shot_epoch or 0) != 0
                and self._det_state == 'locked'):
            self._arm_box_latch(s.get("bbox"), ts)
        if self._meter_color == "White":
            # MEASURE THE CAP DIRECTLY, do not trust `s["green"]`.
            #
            # The reader's own cap search reports green_not_found on 48-95% of
            # detections, yet the green apex is demonstrably present: measured
            # over session_20260826_204615, boxes sitting ON the true meter carry
            # a median of 24 green px inside their top 35%, and only 9% carry
            # fewer than 8. Boxes sitting OFF the meter (court lines, the bright
            # sideline -- a long white stripe looks exactly like a bar) carry a
            # median of 0, with 95% under 8px.
            #
            # So >=8px inside the box top keeps 91% of TRUE locks while admitting
            # 5% of FALSE ones. Trusting `s["green"]` instead made this breaker
            # blind, because a true lock and a false one looked equally capless.
            _cap_seen = False
            try:
                _bb = s.get("bbox") or []
                if len(_bb) == 4 and int(_bb[2]) > 0 and int(_bb[3]) > 0:
                    _bx, _by, _bw, _bh = (int(v) for v in _bb)
                    _y1 = min(frame_bgr.shape[0], _by + max(4, int(_bh * 0.35)))
                    _x0 = max(0, _bx - 2); _x1 = min(frame_bgr.shape[1], _bx + _bw + 2)
                    _y0 = max(0, _by)
                    if _x1 > _x0 and _y1 > _y0:
                        _cap = self._greenmask(
                            cv2.cvtColor(frame_bgr[_y0:_y1, _x0:_x1], cv2.COLOR_BGR2HSV))
                        _cap_seen = int(_cap.sum() // 255) >= self._capless_px_min
            except Exception:
                _cap_seen = True     # fail OPEN: never let the probe blind the reader
            # WINDOWED, not a consecutive streak. The streak version reset on ANY
            # single capped frame, and at 60fps a false lock picks up a stray
            # green pixel often enough to rearm the timer forever: live
            # 2026-08-27 the breaker fired ZERO times against a lock that held
            # the centre-court "27" logo for 222 straight dumped frames with the
            # cap reported on 3% of them. Offline it fired fine at 10fps -- the
            # bug was invisible at the framedump's sample rate.
            #
            # Judge the WINDOW instead: a true meter shows its cap on most
            # frames (measured 91% of on-meter locks carry >=8 green px in the
            # box top), so a lock whose cap-rate sits near zero across a whole
            # window is not the meter, whatever the odd frame says.
            if not _bk_det:
                self._capless_since = None
                self._capless_hist.clear()
            else:
                if ts is not None:
                    self._capless_hist.append((float(ts), bool(_cap_seen)))
                    _cut = float(ts) - self._capless_max_s
                    while self._capless_hist and self._capless_hist[0][0] < _cut:
                        self._capless_hist.popleft()
                if self._capless_since is None:
                    self._capless_since = ts
            _cap_rate = (sum(1 for _, c in self._capless_hist if c)
                         / float(len(self._capless_hist))) if self._capless_hist else 1.0
            if (_bk_det and self._capless_max_s > 0.0 and ts is not None
                    and self._capless_since is not None
                    and (ts - self._capless_since) >= self._capless_max_s
                    and len(self._capless_hist) >= 8
                    and _cap_rate <= self._capless_rate_max):
                s = {"detected": False, "meter_present": False, "fill": 0.0,
                     "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": "capless_suppressed",
                     "confidence": 0.0, "velocity_pct_s": 0.0,
                     "rejection_reason": "capless_fake_lock", "rise_state": ""}
                try:
                    self.last_debug = {"stage": "capless_suppressed",
                                       "capless_s": round(float(ts - self._capless_since), 2)}
                except Exception:
                    pass
                self.conf = 0.0; self.box = None; self.tmpl = None
                self._capless_since = None
                self._capless_hist.clear()
                _bk_det = False; _bk_fill = 0.0
        # [ORION_READER_POST_RELEASE_YIELD] P1 frozen-streak census (flag-gated, attribute-only:
        # nothing below reads these when the flag is off). Counts consecutive STATIC (<=0.75pp
        # step -- far under the ~3pp/frame of a real near-tip rise) emissions at/above the
        # frozen floor while OUR OWN release is pending. Counts across fresh AND held/coast
        # emissions alike (a meter_memory hold re-emits the identical fill), and unlike the
        # fake-lock breaker it is NOT zeroed by arm/coast grace: the release relay is stronger
        # provenance than any grace heuristic. Consumed by the read()-side force-unlock.
        if self._pr_yield:
            if (self._pr_release_pending and _bk_det
                    and _bk_fill >= self._pr_yield_fill_min
                    and self._pr_prev_fill is not None
                    and abs(_bk_fill - self._pr_prev_fill) <= 0.75):
                self._pr_frozen_n += 1
            else:
                self._pr_frozen_n = 0
            self._pr_prev_fill = _bk_fill if _bk_det else None
        # Count at ANY frozen fill level -- a detected box stuck at a constant value (incl. exactly
        # 0.0: a red/green structure whose fill strip reads empty = dÃ©cor / misdetect) for longer
        # than a physical hold is never a live rising meter.
        # NF-2: grace liveness (wall-time) computed once, reused by the counter (NF-5) and the
        # suppression gate below.
        _grace_active = self._grace_live(ts) or self._occl_wide_live   # B1: widened in-shot window
                                                                       # counts as grace here too
        # A2 (STALEBREAK): the hardened breaker must never count/fire INSIDE an armed shot
        # window -- a legit capped/held fill can sit byte-frozen past the caps mid-shot, and
        # breaking there minted a 7-frame within-shot gap on the regression gates (floor <= 5).
        # An armed frame therefore counts as grace for the breaker. The off-shot dead-hold
        # still dies: the fresh-rise gating stops the stale lock re-arming the CV shot-gate,
        # so its armed window expires (~120 frames) and the counter then runs to the cap.
        # (Flag OFF -> unchanged: _grace_active is only consumed by breaker-gated code.)
        #
        # B2 (ARM_GUARD) -- THE POSITIVE-FEEDBACK LOOP. `_armed_now_cached` is the MERGED gate,
        # and the orchestrator refreshes that gate for ~120 frames on `detected && rising` (the
        # CV self-arm). A phantom reports `rising`, self-arms, and the self-arm then hands it
        # breaker grace, which zeroes `_static_fill_n` EVERY FRAME -- so the phantom disables its
        # own killer and the arm never expires. Measured live: ARMED 23:18:06 -> DISARMED
        # 23:19:00 = 54 s continuously armed with ZERO shots, and `static_fake_lock` fired 0
        # times all session.
        #
        # Two arm signals a phantom CANNOT forge open the grace outright: a PHYSICAL (hardware)
        # arm, and a rise the reader itself certified fresh (`_fresh_rise_left`, latched by B5
        # only after N consecutive genuinely-increasing fresh samples).
        #
        # The MERGED gate keeps its grace on HELD frames only (`_held_run > 0`: coast /
        # meter_memory / green_hold / dead_reckoned / held_reseat). That is the case the blanket
        # grace was actually written for -- a shot's release occlusion holding a coasted peak --
        # and it stays BOUNDED by the existing armed-coast cap (`_armed_coast_max`, ~180 frames),
        # so it cannot run forever even when the arm itself is forged. What it no longer covers
        # is the FRESH path: a detection that `_relocate` re-finds every single frame yet whose
        # fill is frozen or non-physical. That is exactly and only the phantom -- confirmed live,
        # where meter_memory / steal_reseat occurred 0 times while the ESRB legal screen
        # advertised "FILL 87% RISING" -- and a fresh read that never moves physically is not a
        # vision sample no matter what the arm claims.
        # B6: the ONLY change is that the hw arm now grants grace inside a BOUNDED window
        # (see _hw_arm_grace_live) instead of unconditionally. The other two clauses are
        # untouched: `_fresh_rise_left` self-expires in `_stale_rise_frames` (30) frames, and
        # measured on the live dead-holds it is live for only 3-8 of 529 frames with 117-253
        # frame gaps, so it cannot sustain the grace on its own.
        # [ORION_DEAD_HOLD_GRACE] The held-frame clause now also requires that this lock was ever
        # OBSERVED RISING. That clause was written for a real shot's release occlusion holding a
        # coasted peak, but as written it handed breaker grace to ANY held run while armed --
        # including a dead décor hold -- bounded only by _armed_coast_max (180 frames, ~3 s). That
        # is the user-visible false lock: measured, a run of 151 consecutive frames with the fill
        # BYTE-FROZEN at 46.7 and rejection_reason=meter_memory, i.e. a box parked on the crowd
        # reading "FILL 47%" for two and a half seconds while no shot was happening.
        #
        # MEASURED SEPARATION, across every archived detframes CSV. Held runs that contain a
        # rising frame (a real occlusion) vs held runs with none (a dead hold):
        #     legit  n=45    median 2f   p90 70f   max 180f
        #     dead   n=2096  median 1f   p90  3f   max 215f
        # LENGTH DOES NOT SEPARATE THEM -- legitimate occlusions reach 180 frames -- which is why
        # simply shrinking the cap would have killed real shots. The RISE does separate them
        # perfectly. And critically, of the 7 legitimate runs longer than the 40-frame breaker
        # cap, ALL SEVEN have their first rising frame at position 0 of the run, so gating on the
        # latch suppresses none of them.
        #
        # Fail-safe direction: failing this test does not suppress anything by itself, it only
        # stops the counter being zeroed, so the ordinary 40/60-frame cap applies exactly as it
        # already does everywhere else.
        if self._stalebreak and (
                (self._hw_arm_grace_live(ts) or self._fresh_rise_left > 0
                 or (self._armed_now_cached and self._held_run > 0
                     and self._lock_ever_rose))
                if self._arm_guard else self._armed_now_cached):
            _grace_active = True
        # NF-5 [fix]: do NOT let grace frames PRE-CHARGE the fake-lock breaker. A shot's release
        # occlusion legitimately holds a frozen (coasted peak) fill; counting those frozen frames
        # would shrink the post-grace budget so a legitimate outcome-freeze right after the shot
        # could be suppressed early. Freeze the counter at 0 while grace is live -> the breaker
        # gets its FULL cap once grace expires. (Flag OFF -> _grace_active False -> byte-identical.)
        if _grace_active:
            self._static_fill_n = 0
            self._rep_fill_hist.clear()   # B3: post-grace budget starts from a clean window too
        elif (self._fakelock_break or self._stalebreak) and _bk_det:
            # legacy test: the REPORTED fill is BYTE-frozen frame to frame.
            _frozen = (self._rep_fill_prev is not None
                       and abs(_bk_fill - self._rep_fill_prev) < 0.05)
            # B3 test (evaluated FIRST so the rolling window sees every detected frame, even
            # the byte-frozen ones): the fill is MOVING but not PHYSICALLY.
            _nonphys = self._non_physical_fill(_bk_fill)
            if _frozen or _nonphys:
                self._static_fill_n += 1
            else:
                self._static_fill_n = 0
        else:
            self._static_fill_n = 0
            self._rep_fill_hist.clear()
        self._rep_fill_prev = _bk_fill if _bk_det else None
        # Caps chosen ABOVE the longest genuine within-shot hold/occlusion (a player's shooting
        # motion crossing the meter is <~0.5 s) but far BELOW the observed fake-locks (2-3.7 s), so
        # the no-disappear coast is preserved while the dead hold dies: 40f (~0.67 s) mid-fill, 60f
        # (~1.0 s) near the tip where a real peak/green make-window legitimately lingers longer.
        # N6: never suppress while the shot-coast grace is live -- a real shot's release occlusion
        # legitimately holds a frozen (coasted peak) fill, and that is a vision sample, not a dÃ©cor
        # dead-hold. The grace is self-bounded (NF-2: <= _shot_coast_ms wall-time, ~0.52 s), so a
        # true dead-hold that outlasts it is still caught once the grace expires.
        if ((self._fakelock_break or self._stalebreak) and _bk_det and not _grace_active
                and self._static_fill_n > (self._fakelock_cap_hi if _bk_fill >= 85.0
                                           else self._fakelock_cap_mid)):
            s = {"detected": False, "meter_present": False, "fill": 0.0, "fill_coarse": 0.0,
                 "bbox": [0, 0, 0, 0], "stage": "static_suppressed", "confidence": 0.0,
                 "velocity_pct_s": 0.0, "rejection_reason": "static_fake_lock", "rise_state": ""}
            try:
                self.last_debug = {"stage": "static_suppressed", "static_n": self._static_fill_n,
                                   "held_fill": round(_bk_fill, 2)}
            except Exception:
                pass
            if self._stalebreak:
                # A2 (hardened): a frozen-fill dead-hold BREAKS the lock (state drop), not just
                # the report. The legacy breaker keeps the (colour-valid) box warm, so the SAME
                # dÃ©cor re-locks next frame and the stale loop -- frozen fill + stale rising
                # velocity -> CV self-arm refresh -> endless armed coast -- sustains itself
                # (fresh=0 staleMem=32/33/40 shots live). Dropping the state forces the COLD
                # re-acquire path to run; a real meter that returns passes the cold gates and
                # re-locks within a frame or two.
                self.conf = 0.0; self.box = None; self.tmpl = None
                self._courtwide_lock = False
                self._courtwide_acquire_tier = ""
                self._consec = 0; self._peak_fill = 0.0
                self._bvx = self._bvy = 0.0
                self._prev_cx = self._prev_cy = self._prev_floor_y = None
                self._lock_red = self._lock_green = None
                self._traj_last_acc = None; self._traj_rejects = 0; self._dr_frames = 0
                self._rise_recent_n = 0; self._grace_until_ts = 0.0
                self._fresh_rise_left = 0; self._occl_wide_until = 0.0
                self._held_run = 0   # A2xB1: hard break drops the lock -> held-echo run over
                self._vel_hist.clear(); self._velocity = 0.0
                self._static_fill_n = 0
                self._rep_fill_hist.clear()                        # B3: window dies with the lock
                self._lock_w_ref = 0.0; self._relock_reject_n = 0  # B4: identity dies with it
                self._fresh_up_n = 0; self._fresh_prev_fill = None  # B5: rise run is over
                self._fresh_lock_ts = None; self._fresh_lock_streak = 0
                self._epoch_bridge_active = False
                # A breaker invalidates this visual identity, but hardware may still own the
                # shot.  Let a later real meter establish fresh dimensions in the same epoch.
                self._visual_drop_shot_shape(self._shot_armed_hw, preserve=False)
                self.last_fill = 0.0; self.last_coarse = 0.0
                self.last_tbox = [0, 0, 0, 0]
                if self._scale_reset:
                    # A1 synergy: clear the two-sided-gate run only; the SCALE histories are
                    # kept (the gradual two-sided gate heals them in place -- see _read_fill).
                    self._low_cand_hist = []
                    self._high_cand_hist = []
        if s.get("measurement_missing", False):
            # Run zero-read/ghost retirement bookkeeping first. Suppressing at
            # the detector-fill construction would starve the ghost's consecutive
            # zero counter and leave a dead candidate monopolizing acquisition.
            s = {"detected": False, "meter_present": False, "fill": 0.0,
                 "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "confidence": 0.0,
                 "velocity_pct_s": 0.0, "top_row": -1, "green": None,
                 "stage": "detector_white_ribbon_missing",
                 "rejection_reason": "detector_white_ribbon_missing",
                 "rise_state": ""}
        # [ORION_PROOF_DETECTOR_BOX 2026-09-19] Stamp the PRE-transform rectangle on EVERY
        # published frame, not only when BOX-TIGHT reshapes one. The orchestrator forwards it
        # to native as `det_bbox`, where the ownership proof's shape gate judges detection
        # geometry on it instead of on the overlay hug (the hug reaches up to
        # ORION_READER_BOX_TIGHT_SIDE/TOP/BOT_REACH = 18 px past each edge and is re-derived
        # from this frame's colour pixels, so a still 26x110 meter draws 26..44 px wide).
        # Unconditional so the field can never be one frame stale: with the flag off it simply
        # equals the published bbox, and the native side then sees no change at all.
        if not isinstance(self.last_debug, dict):
            self.last_debug = {}
        _pre_tight_bbox = s.get("bbox")
        self.last_debug["det_box"] = (
            [int(v) for v in _pre_tight_bbox] if _pre_tight_bbox and len(_pre_tight_bbox) >= 4
            else [0, 0, 0, 0])
        if self._box_tight:
            # BOX-TIGHT: re-shape ONLY the outgoing rectangle (after the breaker, so a
            # suppressed frame's zero box stays zero). fill/velocity/green/top_row and every
            # internal state are already final and untouched.
            #
            # OBSERVABILITY (2026-08-06): publish the PRE-transform rectangle before
            # overwriting it. detframes.csv logs `result.bbox` -- i.e. the DISPLAY box --
            # so with this flag armed (run_orion.local.ps1 sets it to 2) the only recorded
            # stability instrument measured the presentation transform, not detection.
            # Frame-to-frame box variance was therefore UNMEASURABLE on live data exactly
            # when the flag was on. `det_box` is the detector's own rectangle in every
            # mode (it equals bbox when the flag is off), so stability statistics are
            # comparable across modes. Display path is unchanged.
            _trow = s.get("top_row", -1)
            _raw_bbox = s.get("bbox")
            if not isinstance(self.last_debug, dict):
                self.last_debug = {}
            self.last_debug["det_box"] = (
                [int(v) for v in _raw_bbox] if _raw_bbox and len(_raw_bbox) >= 4
                else [0, 0, 0, 0])
            # DETECTOR-FILL frames already serve the meter's full tip-to-base box, so
            # the display transform's vertical extent is capped at 1.6x of it (see the
            # clamp in _tight_display_box: a colour-drift-poisoned _tight_src stretched
            # the engine-facing rectangle to 5x meter height on 227 live frames).
            _hcap = 0
            if (str(s.get("stage", "")) == "detector_fill"
                    and _raw_bbox and len(_raw_bbox) >= 4 and int(_raw_bbox[3]) > 0):
                _hcap = int(round(1.6 * int(_raw_bbox[3])))
            s["bbox"] = self._tight_display_box(
                _raw_bbox, int(_trow) if _trow is not None else -1, h_cap=_hcap)
        style = str(getattr(self._cfg, "meter_style", "Arrow2") or "Arrow2") if self._cfg else "Arrow2"
        color = str(getattr(self._cfg, "meter_color", "Red") or "Red") if self._cfg else "Red"
        detected = bool(s["detected"])
        fill = float(s.get("fill", 0.0) or 0.0)
        vel = float(s.get("velocity_pct_s", 0.0) or 0.0)
        # [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] see _idle_publish_ok. The ONLY thing it
        # changes is what leaves this adapter; every piece of reader/locator state above it has
        # already been updated, so the next press still inherits the full memory.
        bbox_out = tuple(int(v) for v in s.get("bbox", (0, 0, 0, 0)))
        if self._idle_pub_gate and not self._idle_publish_ok(detected, bbox_out, fill, ts):
            detected = False
            bbox_out = (0, 0, 0, 0)
        green = s.get("green")            # (start,end,center,width,conf,px) or None
        # Green release target (the make-window centre) when present; else the tip (100%).
        g_start = g_end = g_center = -1.0
        g_width = g_conf = 0.0
        target_pct = 100.0
        if green is not None:
            g_start, g_end, g_center, g_width, g_conf = (float(green[0]), float(green[1]),
                                                         float(green[2]), float(green[3]), float(green[4]))
            target_pct = g_center
        # eta to the release target from wall-time velocity; -1 when not rising.
        eta_ms = -1.0
        eta_green_ms = -1.0
        if vel > 0.5 and fill < 100.0:
            eta_ms = max(0.0, (100.0 - fill) / vel * 1000.0)
        if green is not None and vel > 0.5 and fill < target_pct:
            eta_green_ms = max(0.0, (target_pct - fill) / vel * 1000.0)
        _structure_verified = bool(
            detected and _detect_shot_epoch > 0 and self._shot_armed_hw
            and self._physical_shot_epoch == _detect_shot_epoch
            and self._gameplay_structure_verified
            and self._gameplay_structure_proof_epoch == _detect_shot_epoch)
        # STRUCTURE NECROPSY bookkeeping (see __init__): what this frame actually
        # EMITTED for the press epoch, the engine-facing half of the census.
        if (detected and _detect_shot_epoch > 0
                and self._ep_census.get("epoch") == _detect_shot_epoch):
            self._ep_census["emit_det"] += 1
        kwargs = dict(
            detected=detected,
            style=style,
            color_name=color,
            bbox=bbox_out,
            fill_pct=fill,
            confidence=float(s.get("confidence", 0.0) or 0.0),
            consecutive_frames=int(self._consec),
            raw_fill_pct=float(s.get("fill_coarse", 0.0) or 0.0),
            smoothed_fill_pct=fill,
            fill_estimator_mode=str(s.get("fill_estimator_mode", "") or ""),
            fill_estimator_generation=int(s.get("fill_estimator_generation", 0) or 0),
            fill_velocity_pct_s=vel,
            fill_acceleration_pct_s2=float(self._accel),
            eta_ms=eta_ms,
            top_pixel_row=int(s.get("top_row", -1)),
            green_window_start_pct=g_start,
            green_window_end_pct=g_end,
            green_window_center_pct=g_center,
            green_window_width_pct=g_width,
            green_window_confidence=g_conf,
            green_cluster_px=int(green[5]) if green is not None else 0,
            eta_to_green_center_ms=eta_green_ms,
            rejection_reason=str(s.get("rejection_reason", "")),
            rise_state=str(s.get("rise_state", "")),
            gameplay_sample_epoch=_detect_shot_epoch,
            gameplay_structure_verified=_structure_verified,
            gameplay_structure_epoch=(
                _detect_shot_epoch if _structure_verified else 0),
        )
        if _HAVE_REAL_RESULT:
            # The real dataclass has more fields (all defaulted); pass only the ones we set.
            return _DetectResult(**kwargs)
        return _DetectResult(**kwargs)

    # ------------------------------------------------------------------ #
    #  MeterDetector-compatible shims (orch calls these via getattr/hasattr)
    # ------------------------------------------------------------------ #
    def set_active_style(self, style):
        style_key = str(style or "").strip().casefold()
        changed = style_key != self._tracking_meter_style
        if self._cfg is not None:
            try:
                self._cfg.meter_style = str(style)
            except Exception:
                pass
        if changed:
            self.reset_tracking()
        self._tracking_meter_style = style_key

    def reload_config(self, cfg=None):
        if cfg is not None:
            self._cfg = cfg
        # A live colour change lands HERE (orchestrator.update_meter -> reload_config). Without
        # this the reader kept masking the OLD colour for the rest of the session while the HUD
        # cheerfully reported the new one -- the picker looked like it worked and detected
        # nothing. Rebuild before reset_tracking so the cleared state is re-seeded consistently.
        _prev = getattr(self, "_meter_color", None)
        self._rebuild_bands()
        if _prev is not None and _prev != self._meter_color:
            _acq_logger.warning("reader bar colour %s -> %s (bands rebuilt)",
                                _prev, self._meter_color)
            # Bands learned for the OLD colour are meaningless for the new one.
            self._learned_red = None
            if self._calibrator is not None:
                try:
                    self._calibrator.set_colour(self._meter_color)
                except Exception:
                    pass
        self.reset_tracking()
