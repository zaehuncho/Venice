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
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import cv2

import meter_bar_colors as _mbc

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

    def __init__(self, frame_w: Optional[int] = None, frame_h: Optional[int] = None,
                 cfg=None, shot_gate=None, params: Optional[ReaderParams] = None,
                 calibrator: Optional[ColorCalibrator] = None,
                 require_gameplay_eligibility: bool = False):
        self.W = int(frame_w) if frame_w else 0
        self.H = int(frame_h) if frame_h else 0
        self._cfg = cfg                          # DetectorConfig (orch update_meter reads det._cfg)
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
        self._gameplay_verify_frames = 0
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
        """Drop stale tracking (orch update_meter calls this on a colour change)."""
        self._clear_gameplay_structure_proof()
        self._reset_state()
        # A colour/style change is a NEW METER, not a re-acquire of the same one -> end the epoch.
        # Deliberately NOT called from the confidence-decay lock-drop path, which keeps the scale
        # (same meter, same size, it will be back).
        self.reset_session_scale()
        # B9b: ...and a DIFFERENT meter invalidates the measured scale the micro gate reads.
        self._sess_track_h = []

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

    def _tight_display_box(self, bbox, top_row=-1):
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
                self._tight_wh_hist.append((right - left, bot - top))
                ws = sorted(p[0] for p in self._tight_wh_hist)
                hs = sorted(p[1] for p in self._tight_wh_hist)
                w_d, h_d = ws[len(ws) // 2], hs[len(hs) // 2]
                ctr = cx + 0.5 * cw
                tight = [int(round(ctr - 0.5 * w_d)), int(round(bot - h_d)),
                         int(w_d), int(h_d)]
                l2 = min(tight[0], cx)
                t2 = min(tight[1], bar_top)
                r2 = max(tight[0] + tight[2], cx + cw)
                b2 = max(tight[1] + tight[3], bar_bot)
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
        else:
            return out
        if tight[2] <= 0 or tight[3] <= 0:
            return out
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
                self._clear_gameplay_structure_proof()
                self._physical_shot_epoch = 0
                self._clear_micro_candidate()
            self._shot_armed_hw = next_hw
        elif not armed:
            self._shot_armed_hw = False   # a full disarm always closes the hw window too
            self._clear_gameplay_structure_proof()
            self._physical_shot_epoch = 0
            self._clear_micro_candidate()

    def _clear_gameplay_structure_proof(self) -> None:
        self._gameplay_structure_verified = False
        self._gameplay_structure_proof_epoch = 0

    def _latch_gameplay_structure_proof(self, shot_epoch) -> None:
        """Attach structural evidence to its captured epoch, never the mutable current epoch."""
        try:
            parsed_epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            parsed_epoch = 0
        self._gameplay_structure_proof_epoch = (
            parsed_epoch if 0 < parsed_epoch <= 0xFFFFFFFFFFFFFFFF else 0)
        self._gameplay_structure_verified = self._gameplay_structure_proof_epoch != 0

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
        self._clear_gameplay_structure_proof()
        self._clear_micro_candidate()
        # [ORION_READER_POST_RELEASE_YIELD] an explicit new shot identity ends the
        # post-release window exactly like the read()-side arm edge does.
        self._pr_release_pending = False
        self._pr_frozen_n = 0
        self._pr_prev_fill = None
        try:
            parsed_epoch = int(shot_epoch)
        except (TypeError, ValueError, OverflowError):
            parsed_epoch = 0
        _prev_epoch = int(getattr(self, "_physical_shot_epoch", 0) or 0)
        self._physical_shot_epoch = (
            parsed_epoch if 0 < parsed_epoch <= 0xFFFFFFFFFFFFFFFF else 0)
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
        if (self._stale_press_drop_pct > 0.0
                and self._physical_shot_epoch != 0
                and self._physical_shot_epoch != _prev_epoch
                and self.box is not None
                and float(getattr(self, "last_fill", 0.0) or 0.0)
                >= self._stale_press_drop_pct):
            _stale_fill = float(getattr(self, "last_fill", 0.0) or 0.0)
            self.conf = 0.0
            self.box = None
            self.tmpl = None
            self._consec = 0
            self._peak_fill = 0.0
            self._coast_n = 0
            self._capless_since = None
            # Name it in the log. Offline replay could NOT reproduce the stale
            # state this fixes (the harness's synthetic presses do not align with
            # real shot cycles), so the live log is the only arbiter of whether
            # this fires and whether it helps. ERROR level for the same reason as
            # _scope_reject: the native relay throttles sidecar WARNINGs.
            _acq_logger.error(
                "STALE LOCK DROPPED AT PRESS: epoch=%d held_fill=%.1f%% (>= %.1f%%) "
                "- the engine would have refused to own this shot",
                self._physical_shot_epoch, _stale_fill, self._stale_press_drop_pct)
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
                           "n_exposed_green": len(self._gz_starts), "end_reason": str(reason)}
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
        if not self.W or not self.H:
            self.H, self.W = int(frame.shape[0]), int(frame.shape[1])
            self._recompute_scale()
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
        # BOX-TIGHT: the per-frame fresh-column source must be cleared at the PRODUCTION
        # boundary, not only inside read() -- a subclass read() can return before the base
        # fresh path runs (CompressedMeterReader's stale hold does), and a stale source from
        # a previous frame must never re-shape this frame's held box as if it were fresh.
        self._tight_src = None
        # B6: latch/clear the physical arm edge BEFORE read(), so the breaker's bounded hw grace
        # is measured from the press that opened this window (see _hw_arm_grace_live).
        self._note_hw_arm_edge(ts)
        # B7: capture this shot's arm-edge red reference (once per hardware epoch, at the press).
        self._note_arm_edge_reference(frame_bgr)
        if self._require_gameplay_eligibility and not self._shot_armed_hw:
            s = self._close_gameplay_gate()
        else:
            if self._require_gameplay_eligibility:
                self._gameplay_gate_closed = False
            s = self.read(frame_bgr, ts)
            if self._require_gameplay_eligibility:
                s = self._qualify_gameplay_sample(s, _detect_shot_epoch)
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
        _bk_fill = float(s.get("fill", 0.0) or 0.0)
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
            s["bbox"] = self._tight_display_box(
                _raw_bbox, int(_trow) if _trow is not None else -1)
        style = str(getattr(self._cfg, "meter_style", "Arrow2") or "Arrow2") if self._cfg else "Arrow2"
        color = str(getattr(self._cfg, "meter_color", "Red") or "Red") if self._cfg else "Red"
        detected = bool(s["detected"])
        fill = float(s.get("fill", 0.0) or 0.0)
        vel = float(s.get("velocity_pct_s", 0.0) or 0.0)
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
        kwargs = dict(
            detected=detected,
            style=style,
            color_name=color,
            bbox=tuple(int(v) for v in s.get("bbox", (0, 0, 0, 0))),
            fill_pct=fill,
            confidence=float(s.get("confidence", 0.0) or 0.0),
            consecutive_frames=int(self._consec),
            raw_fill_pct=float(s.get("fill_coarse", 0.0) or 0.0),
            smoothed_fill_pct=fill,
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
        if self._cfg is not None:
            try:
                self._cfg.meter_style = str(style)
            except Exception:
                pass

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
