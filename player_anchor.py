"""player_anchor.py -- "know WHERE and WHEN to look" for the 2K27 shot meter.

[ORION_PLAYER_ANCHOR 2026-09-15] Owner's goal: "when I'm holding Square there is always a
meter on screen -- read it 100 % of the time, no false locks, no late reads."

Today (meter_locator_cv.MeterContourLocator) the meter has to ANNOUNCE itself: a white
column must already be >= ORION_CV_SHAPE_MIN_H (14 px) tall before gate 9 will even judge
it, so the first accepted fill is ~18 % on the owner's court and ~22 % elsewhere, and on a
far shot the candidate sometimes never exists at all (DETDIAG reject='roi_not_found' for a
whole shot). The floor is not arbitrary: at low fill the chevron notch makes the column
irregular, and the court is full of static 8-13 px white bits (jersey numbers, socks, the
scoreboard, the shot chart) that pass every other gate. The floor buys FALSE-LOCK SAFETY
with LATENESS.

This module buys that safety somewhere else, so the floor can come down: it says WHERE the
meter must be before the meter is visible at all.

THE LANDMARK (measured 2026-09-14, tools/diagnostics/hud_3pt_icon.py +
logs/diagnostics/hud_landmark_study/): the ball handler's nameplate at his feet is
``[3-icon][PS disc][gamertag]``. The PS disc is a constant, high-contrast light disc with a
dark glyph -- it does not change with court, jersey, lighting or shot type -- and it
template-matches at >= 0.60 (TM_CCOEFF_NORMED, multi-scale) on ~72 % of shot-window frames.
Relative to the meter box the plate is nearly rigid: over 472 paired frames on
session_20260912_201355 with a plausible meter box,

    icon_x - box_centre_x :  p10 -85   med -20   p90 +56
    icon_y - box_bottom   :  p10 -35   med +147  p90 +173      (px @720p)

so ONE plate sighting predicts a ~170 x 270 px patch that contains the meter. That patch is
~4 % of the scanned band, which is what makes a relaxed fill floor affordable: a candidate
that is too short to judge on shape alone is still judged on WHERE it is. Conditional on
both being visible the patch contains the box 87-88 % of the time (455 pairs; the shipped
DX/DY/tolerances are within 1.5 pp of the best of a full grid search, with a smaller patch).

THE TRAP (the reason for the gamertag lock): every player on the floor has a nameplate, and
taking the single best PS-disc peak puts the anchor on the WRONG plate for whole shots --
19 of 60 shots in the study had every paired frame outside a patch built that way. ps/tx
scores do NOT separate the two populations (medians 0.70/0.69 either side). What separates
them is IDENTITY and CONTINUITY: the owner's own gamertag crop, learned at runtime from the
first shot whose meter is green-tip confirmed inside the patch, plus a track that moves like
a player and not like a teleport. Until that identity exists the anchor may HELP (search its
patch first) but may never REFUSE (see ``refuse_ok``): an anchor on the wrong player that is
allowed to refuse turns a late read into no read at all.

COVERAGE (measured 2026-09-15, session_20260912_201355, and this is the honest ceiling):
the plate is at the offset a detected meter box predicts on

    60-73 %  of frames inside a REAL press window (fill > 15 %), and
     1-9 %  of frames with no press within 2.5 s

so the anchor can act on roughly two thirds of a genuine shot's frames, and almost never on
the out-of-shot white columns production used to lock onto. That ~10x separation is the
FALSE-LOCK result; it is not a "read it 100 % of the time" result, because on a third of a
real shot's frames there is no readable plate at all and the anchor correctly stands down.
Below 15 % fill the corpus has only 18 frames inside a real press, so the first-sight claim
is NOT measured offline -- it needs a live 60 fps A/B (see the PICKUP: line).

COST. The anchor runs only while a press is armed. [ORION_SHOT_GATE_RELEASE 2026-09-15] The
engine now ENDS the press explicitly (shot_gate_release / shot_gate_disarm -> ARM.note_release)
on every path -- a vision release, a blind NO METER release, the METER BACKSTOP, and a manual
cancel -- so the window closes at the instant the shot left the hand instead of being carried
for the whole ~1 s tail. ORION_ANCHOR_ARM_S is now the BACKSTOP for that, not the ordinary
close: it still bounds a press whose engine never spoke (an old engine, a dropped pipe).
Tracking is one full-res correlation in a small window around the last plate; acquisition is
a quarter-resolution pass over the nameplate band plus a full-res refine of the top peaks,
rate-limited by ORION_ANCHOR_ACQ_MS. Measured over 900 real frames: p50 0.21 ms at live
frame spacing (0.58 ms when the corpus is sparse and acquisition runs more often), p99
2.97 ms, max 4.1 ms -- inside the detect path's 16.7 ms budget, but the MAX is the one
number above target; splitting acquisition over two frames (coarse, then refine) is the
obvious next step if it ever matters.

KNOBS (SHIP defaults as of 2026-09-17 -- ORION_PLAYER_ANCHOR=0 makes this module inert;
see docs/SHIP_CONFIG.md for the whole table and the env override for each):
    ORION_PLAYER_ANCHOR        1   master switch for the anchor itself  [SHIP: was 0]
    ORION_ANCHOR_DX          -30   icon_x - box_centre_x prior  (px @720p)
    ORION_ANCHOR_DY          150   icon_y - box_bottom prior    (px @720p)
    ORION_ANCHOR_TOL_X        85   half-width of the patch about the x prior
    ORION_ANCHOR_TOL_Y        70   half-height of the box-BOTTOM band about the y prior
    ORION_ANCHOR_BOX_H       130   meter height allowance added above the bottom band
    ORION_ANCHOR_PS_MIN     0.58   PS-disc correlation floor
    ORION_ANCHOR_TX_MIN     0.35   learned-gamertag correlation floor
    ORION_ANCHOR_REFUSE_CONF 0.75  confidence at which the anchor may REFUSE outside its patch
    ORION_ANCHOR_ACQ_MS      120   min ms between two full acquisition passes while lost
    ORION_ANCHOR_HOLD_MS     250   how long a plate sighting keeps predicting after it is lost
    ORION_ANCHOR_COARSE_MIN 0.34   coarse peak floor in the acquisition pass
    ORION_ANCHOR_LEARN         1   learn the owner's gamertag + the per-session offsets

ACQUISITION KNOBS [ORION_ANCHOR_ACQUIRE 2026-09-17] -- each one is a separate kill switch,
and setting them all back to their pre-09-17 values restores the shipped behaviour exactly:
    ORION_ANCHOR_CORE_PX      19   match on the disc's inscribed square, not the whole
                                   27 px crop  (0 or >= 27 = the shipped template)
    ORION_ANCHOR_SCALE_WIDE    1   sweep SCALES_WIDE instead of SCALES_ACQ
    ORION_ANCHOR_COARSE_DIV    2   coarse-pass downscale  (4 = the shipped quarter res)
    ORION_ANCHOR_COARSE_TOPK   8   coarse peaks the refine pass looks at  (3 = shipped)
    ORION_ANCHOR_PS_ADAPT      1   let a CONFIRMED identity lower its own PS floor
    ORION_ANCHOR_PS_FLOOR_MIN 0.42 ...but never below this, ever
    ORION_ANCHOR_DY_SCALED     1   the icon->box y offset scales with the plate's own scale
    ORION_ANCHOR_BOOT_TOL_Y  130   y tolerance of the note_meter reverse bootstrap search
    ORION_ANCHOR_OFFSET_IDENT  1   an identity-confirmed plate teaches the offsets even
                                   when today's (wrong) patch does not contain the box
    ORION_ANCHOR_OFFSET_TX   0.55  ...but only on a STRONGER gamertag match than ranking
    ORION_ANCHOR_OFFSET_SANE   1   reject an offset outside the measured population band
    ORION_ANCHOR_PLATE_GATE    1   a disc with no gamertag block beside it is not a plate
    ORION_ANCHOR_PLATE_GRAD   30   ...mean |dI/dx| floor of that block   (true p10: 153)
    ORION_ANCHOR_PLATE_BRIGHT 0.04 ...fraction over 200 floor           (true p10: 0.106)
    ORION_ANCHOR_PLATE_STD    20   ...std floor                          (true p10: 54)
    ORION_ANCHOR_PLATE_W    0.45   weight of that block in the ranking
    ORION_ANCHOR_FULL_W      0.5   weight of the FULL 27x27 re-score in the ranking
    ORION_ANCHOR_COARSE_FALLBACK 1 second coarse pass, flat size, when the first finds none
    ORION_ANCHOR_BOOT_NEAR   0.20  how strongly the bootstrap prefers the predicted spot

ACQUISITION KNOBS [2026-09-19] -- same rule: each one is its own kill switch, and setting
both back restores the 09-17 behaviour exactly (see docs/PLAYER_ANCHOR_ACQUISITION_2026-09-19.md):
    ORION_ANCHOR_COARSE_PER_STRIP 1  take the coarse peaks PER y-STRIP, not globally
    ORION_ANCHOR_TRACK_ID_MISS    0  tracked frames the gamertag may deny before the
                                     track is dropped and re-acquired  (0 = never)

WHY (measured 2026-09-19 on the five 09-17/09-18 press-window dumps, 145 presses,
tools/diagnostics/anchor_acquire_study.py):

  * A GLOBAL TOP-K CANNOT RANK ACROSS THE BAND.  _coarse_map already sizes its template by
    the ROW, so the top strip's template is about half the bottom strip's -- and a small
    template's TM_CCOEFF_NORMED is high on anything.  On the six presses the 09-19 baseline
    lost to `not_in_topk` the crowd, the stands, the scoreboard and every FAR player's
    (small) nameplate score 0.62-0.82 while the owner's real plate at the foot of the court
    scores 0.46-0.61: the true plate is not in the GLOBAL top-16 on most of those frames,
    and it is in its OWN strip's top-3 on every frame the oracle sees it.  Splitting the
    SAME budget across the strips costs no extra refine work and took those six presses to
    zero (locked 131/145 -> 137/145; of the presses whose plate is on screen, 94.2 -> 98.6 %).
  * A TRACK THE IDENTITY DENIES IS NOT A TRACK.  _track re-locks on disc correlation alone,
    so an acquisition that opened on a teammate's plate is carried for the whole press by
    the cheap path: on session_20260918_135725 seq 14 the anchor locked a neighbour 15 ms
    after the press and tracked it for 157 of 167 frames.  A RUN of gamertag refusals now
    drops the track so the next acquisition can re-rank with identity.

WHY (measured 2026-09-17, tools/diagnostics/anchor_acquire_study.py, on the owner's own
sessions 030402 / 030758 and the 09-12 reference dump):

  * THE PLATE IS NOT A FIXED-SIZE BILLBOARD.  The docstring above claimed 0.95..1.05 from
    2,160 sightings; that was ONE session on ONE court at one camera distance.  Across the
    three dumps the disc's own scale runs 0.45..1.20 (p10/p50/p90 = 0.60/0.80/0.90 on
    030758), because the nameplate rides at the player's feet and the player is sometimes
    in the far corner.  SCALES_ACQ = (0.92, 1.0, 1.08) cannot reach a 12 px disc at all.
  * THE TEMPLATE CARRIED THE 09-12 COURT.  The 27 x 27 crop has ~4 px of BLOND WOOD in
    every corner.  On the 09-17 dark-blue practice court the same plate scores 0.62 with
    that border left in and 0.85 with it cropped to the inscribed square -- so a 0.58 floor
    that is generous on wood is a refusal on blue.
  * THE QUARTER-RES COARSE PASS CANNOT RANK A SMALL DISC.  At scale 0.6 the disc is ~11 px,
    which is 2.8 px at quarter resolution; the owner's plate then never reaches the top-3
    the refine pass looks at.
  * THE ICON->BOX Y OFFSET IS NOT A CONSTANT.  The meter is a fixed-size HUD panel and the
    plate is not, so the gap between them scales with the plate: dy = 150 px fits 09-12
    (|residual| p50 28 px) and is 75 px wrong at the median on 030758.  dy = k x scale with
    a per-session k cuts that to 11-15 px p50.
  * THE OFFSETS COULD NOT LEARN.  note_meter only kept an offset when the CURRENT patch
    already contained the box (forward) or when no gamertag was known yet (reverse), so a
    session whose prior was wrong could never correct it.  An identity-confirmed plate now
    teaches the offsets whatever the old patch thinks.
    ORION_EXPECT_PRE_MS      100   how far BEFORE the expected onset the window opens
    ORION_EXPECT_POST_MS     400   how far after it closes

and, read by meter_locator_cv (they belong to the search, not to the plate):
    ORION_ANCHORED_SEARCH      1   search the patch first / refuse outside it  [SHIP: was 0]
    ORION_EXPECTATION_WINDOW   1   the rise-pair rule for a sub-floor fill     [SHIP: was 0]
    ORION_CV_SHAPE_MIN_H_ARMED 8   gate 9's deferral floor inside the patch    [SHIP: was 14]
    ORION_CV_COL_W_MIN_ARMED   6   gate 4's width floor inside the patch       [SHIP: was 8]
    ORION_ANCHOR_ARM_S       2.5   seconds after the press the anchor may run at all
    ORION_ANCHOR_PATCH_PAD_X  40   sideways scan pad so gate 8 still sees a jersey's twin
    ORION_ANCHOR_REFUSE_MODE   1   1 = scan the band and COUNT the refusals; 0 = skip it
    ORION_EXPECT_PAIR_MS      60   max gap between the two frames of a rise pair
    ORION_EXPECT_PAIR_TOL_PX  16   how far the pair's two sightings may be apart

Nothing in here writes to disk, reads settings or touches %LOCALAPPDATA%.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np

# --------------------------------------------------------------------------- #
# The PS-disc template: 27 x 27 grayscale, cut from the owner's own nameplate on
# session_20260912_201355 f01105 at (940, 546) -- the same reference crop the
# 09-14 landmark study measured with. It is the PlayStation platform badge, not
# anything owner-specific, so it carries no identity: identity is the GAMERTAG,
# which is learned at runtime and never persisted (see PlayerAnchor.note_meter).
# Embedded rather than shipped as an asset so the module has no packaging tail.
# --------------------------------------------------------------------------- #
_PS_TEMPLATE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABsAAAAbCAAAAACov6uJAAACZklEQVQoFT3BW0iTYRwH4N/v"
    "+6bzlMKKJSILgw5QUheWRZS0CKKsi4LEKGFiB6KLoNNdmXZWCFmzE7lCKjQ1i4hVFkRQWlAX"
    "pjPKyQTTGUQR5ta+9/23VfY8DNIAIESSAREqIkFIDlg2oQgMJJBKm2IAoEA4GAOFAAn+iimY"
    "aSkaIEXIkIqaJAAz3N39UxkprjUL7NogBAwhqmyEvGsLF66en6WH+wMTG90OUMAQrEmboTrb"
    "15Tl2hVAfA/6UDNDA/wAWnGeGz3jUphitTQfKQLYlQ9OtPZW5wv+o258dDFbWFqdaQaabjk0"
    "EgwR/NXYeyyTzvol2L2lTBFA9FO+AxoJnKjwlBDlNU/verOQFPGEF29yOwWA0XHXR8xpPVW8"
    "XSNp3DO4o3VpYwoShvc2ECnN9decgqRxz2jjwdCNdQqAtX8lwdMtHXb8Eakc9R0K1e3QAHjl"
    "G4EDQ5csJBljnlHfoc83V2gA5p0eAoc/XrYA2H71Bf2RywNfy6anW4B5p4ew1fs77eDAkwfx"
    "9V3j2wKZuSNFO/PAK9+ImdeO+p1iVAV2LZr0R/ZNS38eKKibh/j+VUTpyZrl2zXGXn34Ungv"
    "XNURX7FueapgeI+XmdUbnt33TgMZF/X6hSvVleXKBoy29kt01+aoPVvLNf4wBdHvGRnkj4rK"
    "EjYVg4+vN+VpTKEA4u07nsGgBuK3356YpfEf5cLDqznCfk0t6uxQQ4HCP4w1txxeJmBQqBWk"
    "s31tWa5dA4Z8fX8+rdapAfYTomjoN20jhe65OdI3+HBys9sBQNhritAAwFDPy5gl9tnuhWlI"
    "EPkNttsRgMlAGm4AAAAASUVORK5CYII="
)


def _fenv(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else float(default)
    except Exception:
        return float(default)


def enabled() -> bool:
    """Master switch. Read per call so a live toggle takes effect without a restart.

    [SHIP CONFIG 2026-09-17] DEFAULT ON. The anchor was opt-in while it was being graded;
    every session from 09-15 onward -- including the 09-16 20:26 session the owner called
    perfect -- ran ORION_PLAYER_ANCHOR=1 from the dev launch line, which a packaged install
    never sees. ORION_PLAYER_ANCHOR=0 still makes this module inert.
    """
    return _fenv("ORION_PLAYER_ANCHOR", 1.0) > 0.0


# --------------------------------------------------------------------------- #
#  Arm state -- published by the reader on the FRAME clock
# --------------------------------------------------------------------------- #
class ArmState:
    """The press window, in the locator's own (frame) time base.

    The reader owns the press: it is the object the orchestrator calls
    ``notify_physical_shot_start`` / ``set_shot_state`` on, and it is the only place
    where a press epoch and a frame timestamp are known together. It publishes here;
    the locator (which runs on the detector worker thread and never sees an epoch)
    reads. A plain tuple swap is the whole synchronisation: one writer, one reader,
    no torn state.
    """

    __slots__ = ("_s",)

    def __init__(self):
        # (epoch, press_ts, shot_type, armed, rhythm, release_ts)
        self._s = (0, -1.0e9, "", False, False, -1.0e9)

    def note_press(self, epoch: int, ts: float, shot_type: str = "",
                   rhythm: bool = False) -> None:
        try:
            self._s = (int(epoch), float(ts), str(shot_type or ""), True,
                       bool(rhythm), -1.0e9)
        except Exception:
            pass

    def note_shot_type(self, epoch: int, shot_type: str, rhythm: bool = False) -> bool:
        """Re-type a press that is ALREADY armed, WITHOUT moving its timestamp.

        [ORION_SHOT_GATE_TYPE 2026-09-15] The engine's blind 200 ms grace can upgrade a
        Standstill into a fade mid-hold (the player's lean lands a few frames after the
        button), and it re-sends the arm on the same epoch when it does. The press did not
        move -- only the engine's belief about which animation is coming -- so this changes
        the type and nothing else. A type for a DIFFERENT epoch is refused: a late message
        for a retired press must never re-aim the live one.
        """
        e, p, _k, armed, _r, rel = self._s
        try:
            incoming = int(epoch)
        except Exception:
            return False
        if not armed or incoming != e or incoming == 0:
            return False
        self._s = (e, p, str(shot_type or ""), True, bool(rhythm), rel)
        return True

    def note_release(self, epoch: int = 0, ts: float = 0.0) -> bool:
        """End the press window NOW. -> True when this call is what closed it.

        [ORION_SHOT_GATE_RELEASE 2026-09-15] `epoch` is the press the marker belongs to; 0
        means "whatever is armed" (the reader's own frame-clock republish, which owns the
        arm outright). A marker for a DIFFERENT epoch is refused for the same reason
        note_shot_type refuses one: the next press may already be armed, and a retired
        shot's release must not close it.
        """
        e, p, k, armed, r, _rel = self._s
        try:
            incoming = int(epoch)
        except Exception:
            incoming = 0
        if incoming and e and incoming != e:
            return False
        try:
            rel = float(ts)
        except (TypeError, ValueError):
            rel = -1.0e9          # a release with no usable clock still ENDS the window
        self._s = (e, p, k, False, r, rel)
        return bool(armed)

    def reset(self) -> None:
        self._s = (0, -1.0e9, "", False, False, -1.0e9)

    @property
    def armed(self) -> bool:
        return bool(self._s[3])

    @property
    def epoch(self) -> int:
        return int(self._s[0])

    @property
    def press_ts(self) -> float:
        return float(self._s[1])

    @property
    def shot_type(self) -> str:
        return str(self._s[2])

    @property
    def rhythm(self) -> bool:
        return bool(self._s[4])

    @property
    def release_ts(self) -> float:
        """When the press window was closed, in the publisher's clock. <= -1e8 = never."""
        return float(self._s[5])

    def state(self):
        """APPEND-ONLY tuple. Consumers index it; they must never unpack it exactly."""
        return self._s


ARM = ArmState()


# --------------------------------------------------------------------------- #
#  Expectation window -- WHEN the meter is due
# --------------------------------------------------------------------------- #
# Onset after the press, measured: ~150 ms standing, ~650-700 ms on a fade (the
# gather is longer). [ORION_SHOT_GATE_TYPE 2026-09-15] The engine now ships the shot
# TYPE with the arm (shot_gate_arm shot_type=...), so an ordinary Square press gets
# its own ~0.5 s window. The UNION (a 1.05 s slice of a shot, rather than "any
# time") remains the answer for an unknown/empty type: a stick or probe edge, or an
# OLD engine paired with this sidecar. Never require the type to be present.
_ONSET = {
    "standstill": 150.0,
    "static": 150.0,
    "shot": 150.0,
    "gotoshot": 320.0,
    "goto": 320.0,
    "stepback": 520.0,
    "fade": 675.0,
    "leftfade": 675.0,
    "rightfade": 675.0,
    "hopfade": 675.0,
}
_ONSET_MIN_DEFAULT = 150.0
_ONSET_MAX_DEFAULT = 700.0


def onset_window_ms(shot_type: str = "") -> Tuple[float, float]:
    """-> (lo, hi) ms after the press in which the meter's onset is expected."""
    pre = _fenv("ORION_EXPECT_PRE_MS", 100.0)
    post = _fenv("ORION_EXPECT_POST_MS", 400.0)
    k = "".join(ch for ch in str(shot_type or "").lower() if ch.isalnum())
    on = _ONSET.get(k)
    if on is None:
        lo, hi = _ONSET_MIN_DEFAULT, _ONSET_MAX_DEFAULT
    else:
        lo = hi = on
    return (max(0.0, lo - pre), hi + post)


# --------------------------------------------------------------------------- #
#  The anchor
# --------------------------------------------------------------------------- #
class Anchor:
    """One plate sighting and the meter patch it predicts (full-frame pixels)."""

    __slots__ = ("icon_x", "icon_y", "scale", "conf", "ps", "tx", "track_n",
                 "x0", "y0", "x1", "y1", "bot_lo", "bot_hi", "cx_lo", "cx_hi", "ts")

    def __init__(self, icon_x, icon_y, scale, conf, ps, tx, track_n, ts,
                 dx, dy, tol_x, tol_y, box_h, W, H):
        self.icon_x = float(icon_x)
        self.icon_y = float(icon_y)
        self.scale = float(scale)
        self.conf = float(conf)
        self.ps = float(ps)
        self.tx = float(tx)
        self.track_n = int(track_n)
        self.ts = float(ts)
        cx = self.icon_x - dx                 # predicted meter box centre-x
        bot = self.icon_y - dy                # predicted meter box BOTTOM row
        self.cx_lo = cx - tol_x
        self.cx_hi = cx + tol_x
        self.bot_lo = bot - tol_y
        self.bot_hi = bot + tol_y
        # The SEARCH patch has to hold the whole box, so it reaches box_h above the
        # highest plausible bottom and a little below the lowest (the chevron notch).
        self.x0 = int(max(0, np.floor(self.cx_lo - 18)))
        self.x1 = int(min(W, np.ceil(self.cx_hi + 18)))
        self.y0 = int(max(0, np.floor(self.bot_lo - box_h)))
        self.y1 = int(min(H, np.ceil(self.bot_hi + 6)))

    # -- acceptance ------------------------------------------------------- #
    def contains_box(self, bx: float, by: float, bw: float, bh: float) -> bool:
        """Is this meter box the one this plate predicts? Judged on the two landmarks
        the box is BUILT from (centre-x and bottom row), never on the whole rectangle:
        a partly-filled meter's box is the same box, only shorter."""
        cx = bx + bw * 0.5
        bot = by + bh
        return (self.cx_lo <= cx <= self.cx_hi) and (self.bot_lo <= bot <= self.bot_hi)

    def rect(self):
        return (self.x0, self.y0, self.x1, self.y1)

    def valid(self) -> bool:
        return self.x1 - self.x0 > 12 and self.y1 - self.y0 > 24


class PlayerAnchor:
    """Tracks the owner's nameplate and predicts where his meter will appear."""

    # Nameplate band. Plates ride at the players' FEET, so they never reach the top of
    # the frame nor the very bottom: over 2,160 confirmed plate sightings on
    # session_20260912_201355 the disc centre ran 328..644 px @720p (p01 360, p99 617).
    # The band is the single biggest term in the acquisition cost, so it is cut to that
    # measured range plus a generous margin rather than to "the lower three quarters".
    # [ORION_ANCHOR_ACQUIRE 2026-09-17] The bottom moves 0.97 -> 1.00: on the 09-17 practice
    # court a close-range shot puts the plate at y = 708 px @720p, i.e. BELOW the old band,
    # and on the very closest shots it is off the bottom of the frame entirely (which is a
    # real `no_plate`, not a search failure).
    BAND_Y0_F = 0.35
    BAND_Y1_F = 1.00
    # PERSPECTIVE. The plate's apparent size is a function of its ROW: over 101 oracle-
    # confirmed plates spanning all three dumps, corr(y, scale) = 0.81 and
    # scale ~= SCALE_A + SCALE_B * y_at_720p (residual |p50| 0.054, |p90| 0.13). That is
    # what lets the coarse pass use ONE template size per y-strip instead of guessing a
    # single size for a 2.5x range -- the change that takes the true plate's coarse rank
    # from top-3 47 % to top-3 94 %.
    SCALE_A = -0.012
    SCALE_B = 0.00167
    # The pre-09-17 ladder. It assumed the plate's apparent size barely moves (0.95..1.05
    # over 2,160 sightings) -- true of session_20260912_201355 and of nothing else: see the
    # ACQUISITION KNOBS block above. Kept as the ORION_ANCHOR_SCALE_WIDE=0 kill switch.
    SCALES_ACQ = (0.92, 1.0, 1.08)
    # The measured range is 0.45..1.20; this ladder steps it at ~1.19, so the worst scale
    # mismatch is +-9 %, which costs a few hundredths of correlation and no lock.
    SCALES_WIDE = (0.50, 0.60, 0.72, 0.86, 1.00, 1.15)

    def __init__(self):
        self._lock = threading.Lock()
        self._tmpl = None                # PS disc, gray uint8, 27 x 27 @ scale 1.0
        self._core = None                # ...cropped to its inscribed square (see _match_tmpl)
        self._core_px = -1
        self._ps_seen = []               # PS peaks of IDENTITY-CONFIRMED plates
        self._scale_seen = []            # ...and their scales, for the session's own ladder
        self._tag = None                 # learned gamertag crop, gray uint8 @ scale 1.0
        self._tag_shape = (26, 92)
        self._last = None                # (icon_x, icon_y, scale, ts)
        self._track_n = 0
        self._last_ps = 0.0
        self._last_tx = -9.0
        self._last_acq_ts = -1.0e9
        self._id_miss = 0                # consecutive tracked frames the identity denies
        self._off_dx = []                # learned per-session offsets
        self._off_dy = []
        self.last_ms = 0.0
        self.stats = {"calls": 0, "track": 0, "acq": 0, "hit": 0, "miss": 0,
                      "tag_learned": 0, "bootstrap": 0, "skip_rate": 0, "error": 0,
                      "id_drop": 0}

    # -- lifecycle -------------------------------------------------------- #
    def reset(self, keep_identity: bool = True) -> None:
        with self._lock:
            self._last = None
            self._track_n = 0
            self._last_ps = 0.0
            self._last_tx = -9.0
            self._last_acq_ts = -1.0e9
            self._id_miss = 0
            if not keep_identity:
                self._tag = None
                self._off_dx = []
                self._off_dy = []
                self._ps_seen = []
                self._scale_seen = []

    def _template(self):
        if self._tmpl is None:
            try:
                raw = np.frombuffer(base64.b64decode(_PS_TEMPLATE_B64), np.uint8)
                img = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
                if img is None or img.size == 0:
                    raise ValueError("ps template decode failed")
                self._tmpl = img                      # uint8: see _gray()
            except Exception:
                self.stats["error"] += 1
                self._tmpl = np.zeros((0, 0), np.uint8)
        return self._tmpl if self._tmpl.size else None

    def _match_tmpl(self):
        """The template correlation actually runs against.

        [ORION_ANCHOR_ACQUIRE 2026-09-17] The shipped 27 x 27 keeps ~4 px of the 09-12
        court's blond wood in every corner, and TM_CCOEFF_NORMED does not forgive that on a
        different floor: the SAME plate scores 0.62 with the border and 0.85 without it.
        Cropping to the disc's inscribed square is court-agnostic and costs nothing -- the
        crop is centred, so every (icon_x, icon_y) and every `27.0 * sc` tag offset in this
        module stays exactly where it was.  ORION_ANCHOR_CORE_PX=0 restores the 27 x 27.
        """
        t = self._template()
        if t is None:
            return None
        px = int(_fenv("ORION_ANCHOR_CORE_PX", 19.0))
        if px <= 0 or px >= t.shape[0]:
            return t
        if self._core is None or self._core_px != px:
            o = (t.shape[0] - px) // 2
            self._core = t[o:o + px, o:o + px].copy()
            self._core_px = px
        return self._core

    # -- acquisition policy ----------------------------------------------- #
    def _scales(self):
        """The scale ladder the acquisition sweeps.

        It is NOT narrowed by what the session has seen so far.  That was tried and it is
        wrong: the plate's size is a function of the ROW it is on, not of the session, so a
        session median collapses the ladder onto wherever the owner happened to stand for
        the last few shots and then cannot find him anywhere else (measured: the ladder
        narrowed to 0.67..0.90 and every plate at 0.95+ became an `identity` failure).  The
        per-candidate narrowing that IS sound lives in _refine_scales, which uses the
        candidate's own row.
        """
        if _fenv("ORION_ANCHOR_SCALE_WIDE", 1.0) <= 0.0:
            return self.SCALES_ACQ
        return self.SCALES_WIDE

    def _ps_floor(self):
        """The PS-disc correlation floor for THIS session.

        ORION_ANCHOR_PS_MIN is the cold floor and stays the floor until the owner's own
        gamertag is known.  After that, plates this session has confirmed BY IDENTITY say
        what a real plate scores on this court, and the floor follows them down -- never
        below ORION_ANCHOR_PS_FLOOR_MIN, and never on a candidate the identity does not
        vouch for (see _acquire: a sub-floor peak is kept only when tx >= TX_MIN).
        """
        base = _fenv("ORION_ANCHOR_PS_MIN", 0.58)
        if (_fenv("ORION_ANCHOR_PS_ADAPT", 1.0) <= 0.0 or self._tag is None
                or len(self._ps_seen) < 3):
            return base
        lo = float(np.percentile(self._ps_seen[-40:], 20)) - 0.06
        return max(_fenv("ORION_ANCHOR_PS_FLOOR_MIN", 0.42), min(base, lo))

    def _note_confirmed(self, ps, scale):
        """Remember what a plate the OWNER's identity vouched for looks like."""
        try:
            self._ps_seen.append(float(ps))
            self._scale_seen.append(float(scale))
            if len(self._ps_seen) > 200:
                self._ps_seen = self._ps_seen[-120:]
                self._scale_seen = self._scale_seen[-120:]
        except (TypeError, ValueError):
            pass

    # -- geometry priors -------------------------------------------------- #
    def _priors(self):
        """-> (dx, dy) in @720p px. dy is per unit of PLATE SCALE when DY_SCALED is on."""
        dx = _fenv("ORION_ANCHOR_DX", -30.0)
        dy = _fenv("ORION_ANCHOR_DY", 150.0)
        if _fenv("ORION_ANCHOR_LEARN", 1.0) > 0.0 and len(self._off_dx) >= 5:
            # The per-session offsets are TIGHTER than the population prior (the camera
            # and the owner's plate size are fixed for a session), so a learned median
            # shrinks the patch without shrinking its tolerance.
            dx = float(np.median(self._off_dx[-40:]))
            dy = float(np.median(self._off_dy[-40:]))
        return dx, dy

    @staticmethod
    def _dy_scaled() -> bool:
        return _fenv("ORION_ANCHOR_DY_SCALED", 1.0) > 0.0

    # -- the main entry point --------------------------------------------- #
    def update(self, frame_bgr, ts: Optional[float] = None,
               armed: bool = True) -> Optional[Anchor]:
        """Locate the owner's plate on this frame and return the patch it predicts.

        Cheap by construction: a tracked plate costs one small full-res correlation;
        a lost plate costs one rate-limited coarse pass over the nameplate band.
        Returns None when no plate is credible (the caller then keeps today's
        full-band behaviour -- this module never fails closed).
        """
        t0 = time.perf_counter()
        try:
            if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
                return None
            tmpl = self._match_tmpl()
            if tmpl is None:
                return None
            H, W = frame_bgr.shape[:2]
            if H < 200 or W < 320:
                return None
            now = float(ts) if (ts is not None and ts == ts) else time.monotonic()
            s = H / 720.0
            with self._lock:
                self.stats["calls"] += 1
                hold_s = _fenv("ORION_ANCHOR_HOLD_MS", 250.0) / 1000.0
                hit = None
                # 1) TRACK: a window around the last sighting. A player walks; a plate
                #    does not teleport. This is the branch that runs on nearly every
                #    armed frame and it is the one that has to be free.
                if self._last is not None and 0.0 <= (now - self._last[3]) <= hold_s:
                    hit = self._track(frame_bgr, tmpl, s, now)
                    if hit is not None:
                        self.stats["track"] += 1
                        # [ORION_ANCHOR_TRACK_ID_MISS 2026-09-19] A TRACK THE IDENTITY
                        # DENIES IS NOT A TRACK. The track re-locks on disc correlation
                        # alone, so an acquisition that opened on a TEAMMATE's nameplate is
                        # then carried for the whole press by the cheap path: measured on
                        # session_20260918_135725 seq 14 (a far Left Fade, plate scale
                        # 0.65, the owner's plate at coarse rank 5) the anchor locked a
                        # neighbouring plate 15 ms after the press and tracked it for 157
                        # of 167 frames -- `identity` in the failure histogram. Once the
                        # owner's gamertag IS known, a run of tracked frames that the tag
                        # refuses drops the track and lets the next acquisition re-rank
                        # with identity, which is the term that picks the owner out of five
                        # nameplates. A RUN, not one frame: the ball, a crossing player or
                        # a scale wobble costs a single frame's correlation on the owner's
                        # own plate too. ORION_ANCHOR_TRACK_ID_MISS=0 removes it.
                        # DEFAULT 0 = OFF. Measured 2026-09-19 on all five press dumps:
                        # this bought ZERO extra locks (every one of the +6 came from
                        # ORION_ANCHOR_COARSE_PER_STRIP), it did not close either of the
                        # two real `identity` presses -- on a real court an impostor's
                        # gamertag still correlates 0.4-0.6 against the owner's learned
                        # crop, i.e. above the 0.35 ranking floor -- and it costs
                        # acquisitions (163 -> 243 on session_20260917_050219, anchor max
                        # 12.6 -> 16.0 ms). Shipping it on would be a gain nobody measured.
                        # The mechanism, the knob and the tests stay: set it to 3 with a
                        # TX_MIN that actually separates two plates and re-run the refusal
                        # study before turning it on.
                        id_miss_n = int(_fenv("ORION_ANCHOR_TRACK_ID_MISS", 0.0))
                        if (id_miss_n > 0 and self._tag is not None
                                and hit[4] < _fenv("ORION_ANCHOR_TX_MIN", 0.35)):
                            self._id_miss += 1
                            if self._id_miss >= id_miss_n:
                                self._id_miss = 0
                                self._last = None
                                self._track_n = 0
                                hit = None
                                self.stats["id_drop"] += 1
                        else:
                            self._id_miss = 0
                # 2) ACQUIRE: rate-limited full-band coarse-to-fine search.
                if hit is None:
                    acq_gap = _fenv("ORION_ANCHOR_ACQ_MS", 120.0) / 1000.0
                    if (now - self._last_acq_ts) >= acq_gap:
                        self._last_acq_ts = now
                        hit = self._acquire(frame_bgr, tmpl, s)
                        self.stats["acq"] += 1
                        if hit is not None:
                            self._track_n = 0
                    else:
                        self.stats["skip_rate"] += 1
                if hit is None:
                    self.stats["miss"] += 1
                    # A plate that was solid a moment ago still predicts: the player has
                    # not moved 200 px in 200 ms, and the meter's whole life is ~600 ms.
                    if (self._last is not None
                            and 0.0 <= (now - self._last[3]) <= hold_s
                            and self._track_n >= 2):
                        return self._anchor_from(self._last[0], self._last[1],
                                                 self._last[2], self._last_ps,
                                                 self._last_tx,
                                                 max(0, self._track_n - 1), now, W, H,
                                                 stale_s=now - self._last[3])
                    return None
                ix, iy, sc, ps, tx = hit
                if self._tag is not None and tx >= _fenv("ORION_ANCHOR_TX_MIN", 0.35):
                    self._note_confirmed(ps, sc)
                moved = 1.0e9
                if self._last is not None and 0.0 <= (now - self._last[3]) <= hold_s:
                    moved = float(np.hypot(ix - self._last[0], iy - self._last[1]))
                self._track_n = (self._track_n + 1) if moved <= 120.0 * s else 1
                self._last = (ix, iy, sc, now)
                self._last_ps = ps
                self._last_tx = tx
                self.stats["hit"] += 1
                return self._anchor_from(ix, iy, sc, ps, tx, self._track_n, now, W, H)
        except Exception:
            self.stats["error"] += 1
            return None
        finally:
            self.last_ms = (time.perf_counter() - t0) * 1000.0

    # -- confidence ------------------------------------------------------- #
    def _anchor_from(self, ix, iy, sc, ps, tx, track_n, now, W, H, stale_s=0.0):
        dx, dy = self._priors()
        s = H / 720.0
        tol_x = _fenv("ORION_ANCHOR_TOL_X", 85.0) * s
        tol_y = _fenv("ORION_ANCHOR_TOL_Y", 70.0) * s
        box_h = _fenv("ORION_ANCHOR_BOX_H", 130.0) * s
        if self._dy_scaled():
            # The meter is a fixed-size HUD panel; the plate is a perspective element at
            # the player's feet. The gap between them therefore scales with the PLATE, not
            # with the frame: measured |residual| p50 drops 28->11 px (030402), 76->16
            # (030758) and 28->24 (09-12) against a per-session k x scale.
            k = max(0.25, min(2.5, float(sc)))
            dy = dy * k
            tol_y = tol_y * k
        # a coasted prediction widens with its own age (a player moves ~0.3 px/ms)
        if stale_s > 0.0:
            grow = min(60.0, 300.0 * stale_s) * s
            tol_x += grow
            tol_y += grow
        conf = self._confidence(ps, tx, track_n, stale_s)
        return Anchor(ix, iy, sc, conf, ps, tx, track_n, now,
                      dx * s, dy * s, tol_x, tol_y, box_h, W, H)

    def _confidence(self, ps, tx, track_n, stale_s):
        """0..1. IDENTITY is what earns the right to refuse (see the module docstring):
        without a learned gamertag the score is capped below ORION_ANCHOR_REFUSE_CONF,
        so a first-shot anchor can help the search but can never veto the real meter."""
        ps_min = self._ps_floor()
        c = 0.30 + 0.30 * min(1.0, max(0.0, (ps - ps_min) / max(1e-6, 0.95 - ps_min)))
        c += 0.10 * min(1.0, track_n / 4.0)
        if self._tag is not None:
            tx_min = _fenv("ORION_ANCHOR_TX_MIN", 0.35)
            if tx >= tx_min:
                c += 0.15 + 0.15 * min(1.0, max(0.0, (tx - tx_min) / 0.45))
            else:
                c = min(c, 0.35)          # the plate is NOT the owner's -> do not trust it
        else:
            c = min(c, 0.70)              # no identity yet -> may help, may not refuse
        if stale_s > 0.0:
            c *= max(0.0, 1.0 - stale_s / max(1e-6, _fenv("ORION_ANCHOR_HOLD_MS", 250.0) / 1000.0))
        return float(max(0.0, min(1.0, c)))

    @staticmethod
    def refuse_ok(anchor: Optional[Anchor]) -> bool:
        """May this anchor REFUSE a candidate that sits outside its patch?"""
        return (anchor is not None and anchor.valid()
                and anchor.conf >= _fenv("ORION_ANCHOR_REFUSE_CONF", 0.75))

    # -- learning --------------------------------------------------------- #
    def note_meter(self, frame_bgr, box, anchor: Optional[Anchor], confirmed: bool) -> None:
        """Feed a CONFIRMED meter back. THE METER TEACHES THE ANCHOR, not the other way round.

        Measured 2026-09-15 on session_20260912_201355: a cold acquisition that ranks the PS
        disc on correlation alone finds a plate on 29 % of low-fill frames and the patch it
        builds contains the real meter on 4 % of those -- it is on somebody else's nameplate.
        Identity (the owner's own gamertag) is what fixes that, and identity cannot be
        bootstrapped from the plate search, because the plate search is the thing that is
        wrong. It has to come from the one signal that is already reliable: a meter the
        shipped gates accepted with a confirmed green tip.

        So this runs in two directions:
          FORWARD  -- an anchor already exists and contains this box: keep the per-session
                      icon->box offsets (they tighten the patch) and, once, learn the tag;
          REVERSE  -- no anchor, or the anchor does not contain this box: look for a PS disc
                      exactly where THIS box says the plate must be (box centre - 30, box
                      bottom + 150, +-the patch tolerance), and learn the tag from there.
        The reverse direction is the bootstrap: today's locator is dependable from ~25 % fill,
        so the first ordinary shot of a session hands the anchor the owner's identity, and the
        anchor repays it by finding the NEXT shot's meter earlier.
        """
        if not confirmed or box is None or frame_bgr is None:
            return
        if _fenv("ORION_ANCHOR_LEARN", 1.0) <= 0.0:
            return
        try:
            bx, by, bw, bh = (float(v) for v in box[:4])
            H, W = frame_bgr.shape[:2]
            s = H / 720.0
            tx_min = _fenv("ORION_ANCHOR_TX_MIN", 0.35)
            with self._lock:
                ps_min = self._ps_floor()
                # [ORION_ANCHOR_ACQUIRE 2026-09-17] IDENTITY, not today's geometry, decides
                # whether this (plate, box) pair may teach the offsets. The old rule
                # ("only when the CURRENT patch already contains the box") is circular: a
                # session whose dy prior is wrong can never collect the samples that would
                # correct it, which is exactly what happened on 030758 (dy prior 150,
                # measured median 74). A plate the owner's own gamertag vouches for is a
                # legitimate sample whatever the stale patch thinks.
                # A STRONGER identity than ranking needs: ORION_ANCHOR_TX_MIN (0.35) is the
                # bar for "this plate outranks that one", which is not the same claim as
                # "this plate is the owner's, so move the geometry to it".
                ident_tx = max(tx_min, _fenv("ORION_ANCHOR_OFFSET_TX", 0.55))
                ident = (_fenv("ORION_ANCHOR_OFFSET_IDENT", 1.0) > 0.0
                         and anchor is not None and self._tag is not None
                         and anchor.tx >= ident_tx)
                fwd = (anchor is not None and anchor.ps >= ps_min
                       and (anchor.contains_box(bx, by, bw, bh) or ident))
                if fwd:
                    self._note_offset(anchor.icon_x, anchor.icon_y, anchor.scale,
                                      bx, by, bw, bh, s)
                    if ident:
                        self._note_confirmed(anchor.ps, anchor.scale)
                    if self._tag is not None:
                        return
                    ix, iy, sc = anchor.icon_x, anchor.icon_y, anchor.scale
                else:
                    if self._tag is not None:
                        return
                    found = self._disc_near(frame_bgr, bx + bw * 0.5, by + bh, s)
                    if found is None:
                        return
                    ix, iy, sc = found
                    self._note_offset(ix, iy, sc, bx, by, bw, bh, s)
                if not self._learn_tag(frame_bgr, ix, iy, sc * s):
                    return
                self.stats["tag_learned"] += 1
                self._note_confirmed(_fenv("ORION_ANCHOR_PS_MIN", 0.58), sc)
                if not fwd:
                    self.stats["bootstrap"] = self.stats.get("bootstrap", 0) + 1
        except Exception:
            self.stats["error"] += 1

    def _note_offset(self, ix, iy, sc, bx, by, bw, bh, s):
        """Keep one icon->box offset sample for the per-session prior.

        The x offset is stored in @720p px (it does not track the plate's scale: measured
        corr(scale, dx) = -0.25 / 0.02 / 0.33 on the three dumps).  The y offset is stored
        PER UNIT OF PLATE SCALE when ORION_ANCHOR_DY_SCALED is on, because it does:
        corr(scale, dy) = 0.67 / 0.57 / 0.38, and a per-session k x scale cuts the median
        residual from 28/76/28 px to 11/16/24 px.
        """
        k = max(0.25, min(2.5, float(sc))) if self._dy_scaled() else 1.0
        odx = (ix - (bx + bw * 0.5)) / max(1e-6, s)
        ody = (iy - (by + bh)) / max(1e-6, s * k)
        # PLAUSIBILITY BAND. The offsets are a MEDIAN, and a median only survives a
        # minority of bad samples. `ident` lets a plate the gamertag vouches for teach the
        # prior even when today's patch disagrees, and the gamertag floor that is right for
        # RANKING two nameplates (0.35) is not strong enough evidence to move the geometry:
        # one plate on the wrong player, taken 40 times in a shot, walks the prior off the
        # court. The band is the measured population over all three dumps (dx -98..65,
        # dy/scale 3..289 @720p) with a wide margin. ORION_ANCHOR_OFFSET_SANE=0 disables it.
        if _fenv("ORION_ANCHOR_OFFSET_SANE", 1.0) > 0.0:
            if not (-170.0 <= odx <= 120.0) or not (10.0 <= ody <= 320.0):
                return
        self._off_dx.append(odx)
        self._off_dy.append(ody)
        if len(self._off_dx) > 200:
            self._off_dx = self._off_dx[-120:]
            self._off_dy = self._off_dy[-120:]

    def _disc_near(self, frame_bgr, box_cx, box_bot, s):
        """Find the PS disc where a CONFIRMED meter box says the owner's plate must be.
        -> (icon_x, icon_y, scale) or None. Costs one small multi-scale match, once.

        [ORION_ANCHOR_ACQUIRE 2026-09-17] This is the ONLY road to identity, so it may not
        inherit the search's blind spots: it sweeps the wide ladder and it opens the y
        window to ORION_ANCHOR_BOOT_TOL_Y, because the very prior it is trying to correct
        (dy = 150) is the thing that was 75 px wrong on 030758.  It still refuses anything
        the disc correlation does not support, and the caller still has to cut a gamertag
        with real contrast out of the result before any identity exists.
        """
        tmpl = self._match_tmpl()
        if tmpl is None:
            return None
        dx, dy = self._priors()
        tol_x = _fenv("ORION_ANCHOR_TOL_X", 85.0) * s
        tol_y = max(_fenv("ORION_ANCHOR_TOL_Y", 70.0),
                    _fenv("ORION_ANCHOR_BOOT_TOL_Y", 130.0)) * s
        cx = box_cx + dx * s
        cy = box_bot + dy * s
        ps_min = self._ps_floor()
        tol_y0 = _fenv("ORION_ANCHOR_TOL_Y", 70.0) * s
        best = None
        for m in self._scales():
            t = self._scaled(tmpl, m * s)
            if t is None:
                continue
            th, tw = t.shape[:2]
            reg, rx0, ry0 = self._gray(frame_bgr, cx - tol_x - tw, cy - tol_y - th,
                                       cx + tol_x + tw, cy + tol_y + th)
            if reg is None or reg.shape[0] <= th or reg.shape[1] <= tw:
                continue
            _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))
            if mx < ps_min:
                continue
            ix = rx0 + loc[0] + tw * 0.5
            iy = ry0 + loc[1] + th * 0.5
            if abs(ix - cx) > tol_x or abs(iy - cy) > tol_y:
                continue
            st, st_ok = self._tag_stats(frame_bgr, ix, iy, m * s)
            if _fenv("ORION_ANCHOR_PLATE_GATE", 1.0) > 0.0 and not st_ok:
                continue
            # NEAREST THE PREDICTION, not strongest. Widening the boot window (which is
            # what lets a session with a wrong dy prior bootstrap at all) also lets a
            # TEAMMATE'S plate into it, and this window is the only road to identity -- a
            # wrong tag here poisons every ranking, every offset and every refusal after
            # it. So a plate at the predicted spot outranks a stronger one 100 px away.
            pen = (abs(iy - cy) / max(1.0, tol_y0)) * _fenv("ORION_ANCHOR_BOOT_NEAR", 0.20)
            rank = mx - pen + _fenv("ORION_ANCHOR_PLATE_W", 0.45) * st
            if best is None or rank > best[0]:
                best = (rank, ix, iy, m)
        return None if best is None else (best[1], best[2], best[3])

    def _learn_tag(self, frame_bgr, ix, iy, sc) -> bool:
        """Cut the gamertag immediately right of a disc at (ix, iy) and keep it as this
        session's identity. Never persisted, never leaves the process."""
        H, W = frame_bgr.shape[:2]
        th, tw = self._tag_shape
        sc = max(0.5, sc)
        x0 = int(round(ix + 27.0 * sc * 0.5 + 1))
        y0 = int(round(iy - (th * 0.5) * sc))
        x1 = int(round(x0 + tw * sc))
        y1 = int(round(y0 + th * sc))
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H or x1 - x0 < 16 or y1 - y0 < 8:
            return False
        crop = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if float(crop.std()) < 12.0:
            return False                         # flat wood, not text: do not learn it
        self._tag = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
        return True

    # -- matching --------------------------------------------------------- #
    @staticmethod
    def _gray(frame_bgr, x0, y0, x1, y1):
        """Gray uint8 of ONE small window. Never converts the whole frame: a full-frame
        BGR2GRAY + float32 copy is ~1.5 ms, which is the entire anchor budget."""
        H, W = frame_bgr.shape[:2]
        x0 = int(max(0, min(W, x0))); x1 = int(max(0, min(W, x1)))
        y0 = int(max(0, min(H, y0))); y1 = int(max(0, min(H, y1)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None, 0, 0
        return cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), x0, y0

    def _full_ps(self, frame_bgr, ix, iy, sc):
        """Re-score the winner with the FULL 27 x 27 template.

        The 27 x 27 carries the 09-12 court in its corners -- which is why the search now
        matches on the inscribed disc -- but those same border pixels also carry the
        plate's own CONTEXT (the "3" cell's edge on the left, the gamertag's on the right),
        and that is discrimination the crop threw away.  Measured over 69 true plates and
        26 impostors: full-template p50 0.59 true vs 0.41 impostor.  So the crop decides
        what is FOUND and the full template gets a vote on what is CHOSEN.
        ORION_ANCHOR_FULL_W=0 drops the vote.
        """
        if _fenv("ORION_ANCHOR_FULL_W", 0.5) <= 0.0:
            return 0.0
        t = self._template()
        if t is None or t is self._match_tmpl():
            return 0.0
        t = self._scaled(t, sc)
        if t is None:
            return 0.0
        th, tw = t.shape[:2]
        reg, _, _ = self._gray(frame_bgr, ix - tw * 0.5 - 5, iy - th * 0.5 - 5,
                               ix + tw * 0.5 + 5, iy + th * 0.5 + 5)
        if reg is None or reg.shape[0] <= th or reg.shape[1] <= tw:
            return 0.0
        return max(0.0, float(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED).max()))

    def _tag_stats(self, frame_bgr, ix, iy, sc):
        """Is there a GAMERTAG right of this disc? -> (score 0..1, ok) or (0.0, False).

        [ORION_ANCHOR_ACQUIRE 2026-09-17] Widening the ladder and cropping the template
        buys sensitivity, and sensitivity costs discrimination: on the 09-12 park court the
        wide search ranked a crowd/scoreboard blob above the owner's plate on most frames
        (measured: the anchor's pick was >45 px from the oracle plate, and the real meter
        box then sat inside its patch on 1.6 % of armed frames against 57.8 % for the
        shipped search).  What an impostor does NOT have is a nameplate's text block.

        Measured over 69 oracle-confirmed plates and 26 of the search's own impostors, on
        the ROI the gamertag occupies (p10 of the TRUE population / p90 of the impostors):

            mean |dI/dx|   true p10 153   impostor (09-17) p90   2.8
            fraction >200  true p10 0.106 impostor (09-17) p90   0.0
            std            true p10 54    impostor (09-17) p90   5.2

        so the floors below sit ~5x under the true p10: this refuses a blank patch of
        court, not a dim gamertag.  On the park the populations overlap, which is why the
        gate is a FLOOR and the separation is carried by the score it feeds into the
        ranking.  ORION_ANCHOR_PLATE_GATE=0 removes it entirely.
        """
        H, W = frame_bgr.shape[:2]
        th, tw = self._tag_shape
        x0 = int(round(ix + 27.0 * sc * 0.5 + 1))
        y0 = int(round(iy - (th * 0.5) * sc))
        x1 = int(round(x0 + tw * sc))
        y1 = int(round(y0 + th * sc))
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H or x1 - x0 < 12 or y1 - y0 < 6:
            return 0.0, False
        g = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        std = float(g.std())
        bright = float(cv2.countNonZero(cv2.inRange(g, 200, 255))) / max(1.0, g.size)
        grad = float(np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)).mean())
        ok = (grad >= _fenv("ORION_ANCHOR_PLATE_GRAD", 30.0)
              and bright >= _fenv("ORION_ANCHOR_PLATE_BRIGHT", 0.04)
              and std >= _fenv("ORION_ANCHOR_PLATE_STD", 20.0))
        score = min(1.0, grad / 160.0) * 0.5 + min(1.0, bright / 0.10) * 0.5
        return score, ok

    def _score_tag(self, frame_bgr, ix, iy, sc):
        """Correlate the learned gamertag immediately right of a disc peak. -9 = no
        template yet (unknown, not a refusal)."""
        if self._tag is None:
            return -9.0
        try:
            t = self._tag if abs(sc - 1.0) < 1e-3 else cv2.resize(
                self._tag, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            th2, tw2 = t.shape[:2]
            if th2 < 6 or tw2 < 12:
                return -9.0
            x0 = int(round(ix + 27.0 * sc * 0.5 + 1)) - 8
            y0 = int(round(iy - th2 * 0.5)) - 6
            reg, _, _ = self._gray(frame_bgr, x0, y0, x0 + tw2 + 18, y0 + th2 + 14)
            if reg is None or reg.shape[0] <= th2 or reg.shape[1] <= tw2:
                return -9.0
            return float(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED).max())
        except Exception:
            return -9.0

    def _track(self, frame_bgr, tmpl, s, now):
        """Full-res correlation in a small window around the last plate.

        ONE scale on the hot path (a plate's size does not change between two frames);
        the neighbouring scales are tried only when that one drops below the floor, so
        the ordinary armed frame pays a single ~0.09 ms correlation.
        """
        try:
            lx, ly, lsc, lts = self._last
            H, W = frame_bgr.shape[:2]
            # the window grows with the gap since the last sighting, but only up to the
            # point where a full acquisition would be cheaper anyway
            dt = min(0.10, max(0.0, now - lts))
            pad = (34.0 + 420.0 * dt) * s
            gray, x0, y0 = self._gray(frame_bgr,
                                      lx - pad - 20 * s, ly - pad * 0.7 - 18 * s,
                                      lx + pad + 20 * s, ly + pad * 0.7 + 18 * s)
            if gray is None:
                return None
            ps_min = self._ps_floor()
            best = None
            for m in (1.0, 0.92, 1.09):
                t = self._scaled(tmpl, lsc * m * s)
                if t is None or t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1]:
                    continue
                r = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(r)
                if best is None or mx > best[0]:
                    best = (mx, x0 + loc[0] + t.shape[1] * 0.5,
                            y0 + loc[1] + t.shape[0] * 0.5, lsc * m)
                if mx >= ps_min:
                    break                      # the hot path stops after ONE correlation
            if best is None or best[0] < ps_min:
                return None
            ps, ix, iy, sc = best
            # [ORION_ANCHOR_ACQUIRE 2026-09-17] CLAMP THE TRACKED SCALE. The track re-scales
            # by 0.92/1.09 every frame it re-locks, so over a 60-frame hold the scale random-
            # walks: measured 0.39 on a session whose ladder bottoms out at 0.50, which then
            # resizes the learned gamertag wrongly (identity stops matching) and, with
            # DY_SCALED on, shrinks the patch the plate predicts. The ladder's own range is
            # the clamp, widened a little so a genuinely smaller plate is not pinned.
            if _fenv("ORION_ANCHOR_SCALE_WIDE", 1.0) > 0.0:
                lo = min(self.SCALES_WIDE) * 0.9
                hi = max(self.SCALES_WIDE) * 1.1
                sc = max(lo, min(hi, sc))
            return (ix, iy, sc, ps, self._score_tag(frame_bgr, ix, iy, sc * s))
        except Exception:
            self.stats["error"] += 1
            return None

    def _refine_scales(self, scales, y720, k=3):
        """The `k` ladder rungs nearest the size this ROW implies (see SCALE_A/SCALE_B)."""
        if len(scales) <= k or _fenv("ORION_ANCHOR_SCALE_ROW", 1.0) <= 0.0:
            return scales
        a = _fenv("ORION_ANCHOR_SCALE_A", self.SCALE_A)
        b = _fenv("ORION_ANCHOR_SCALE_B", self.SCALE_B)
        want = a + b * float(y720)
        return tuple(sorted(scales, key=lambda m: abs(m - want))[:k])

    def _coarse_map(self, gs, tmpl, s, fx, fy, by0, scales, flat_scale=None):
        """The band's coarse response map, ONE template size per y-strip.

        [ORION_ANCHOR_ACQUIRE 2026-09-17] A single coarse size cannot rank a disc that runs
        11..30 px, and sweeping three sizes over the whole band costs 3x.  The plate's size
        is a function of its ROW (SCALE_A/SCALE_B above), so each strip gets the size its
        own rows imply and the total work is one full-band pass.  Measured on 101
        oracle-confirmed plates: the true plate's coarse rank is top-3 on 94 % / top-8 on
        97 % of frames, against top-3 36 % / top-8 47 % for the shipped quarter-res pass.
        ORION_ANCHOR_COARSE_STRIPS=0 restores the single-size pass.
        """
        rows = gs.shape[0]
        n = 1 if flat_scale is not None else int(_fenv("ORION_ANCHOR_COARSE_STRIPS", 3.0))
        if n <= 1:
            # the pre-09-17 pass (and the second-pass fallback): ONE size for the band
            m = scales[len(scales) // 2] if flat_scale is None else float(flat_scale)
            t = self._scaled(tmpl, m * s * fx)
            if t is None or t.shape[0] >= rows or t.shape[1] >= gs.shape[1]:
                return None
            r = cv2.matchTemplate(gs, t, cv2.TM_CCOEFF_NORMED)
            flat = np.full(gs.shape, -9.0, np.float32)
            flat[t.shape[0] // 2:t.shape[0] // 2 + r.shape[0],
                 t.shape[1] // 2:t.shape[1] // 2 + r.shape[1]] = r
            return flat
        a = _fenv("ORION_ANCHOR_SCALE_A", self.SCALE_A)
        b = _fenv("ORION_ANCHOR_SCALE_B", self.SCALE_B)
        lo_s, hi_s = min(scales), max(scales)
        flat = np.full(gs.shape, -9.0, np.float32)
        used = False
        for k in range(n):
            r0 = int(rows * k / n)
            r1 = int(rows * (k + 1) / n)
            y720 = (by0 + ((r0 + r1) * 0.5) / max(1e-6, fy)) / max(1e-6, s)
            m = max(lo_s, min(hi_s, a + b * y720))
            t = self._scaled(tmpl, m * s * fx)
            if t is None:
                continue
            th, tw = t.shape[:2]
            p0 = max(0, r0 - th)
            p1 = min(rows, r1 + th)
            sub = gs[p0:p1]
            if sub.shape[0] <= th or sub.shape[1] <= tw:
                continue
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
            tgt = flat[p0 + th // 2:p0 + th // 2 + r.shape[0],
                       tw // 2:tw // 2 + r.shape[1]]
            np.maximum(tgt, r[:tgt.shape[0], :tgt.shape[1]], out=tgt)
            used = True
        return flat if used else None

    def _acquire(self, frame_bgr, tmpl, s):
        """Coarse-to-fine full-band search. Coarse is the band's gray downscaled by an exact
        integer box filter (INTER_AREA to a NON-integer ratio costs 1 ms and buys nothing);
        the top peaks are then refined at full resolution, with the learned gamertag -- not
        the disc correlation -- as the ranking term: identity is what picks the OWNER's
        plate out of five nameplates.

        [ORION_ANCHOR_ACQUIRE 2026-09-17] Two things changed here and both are measured in
        the ACQUISITION KNOBS block above.  (1) The downscale is 1/2, not 1/4: at the
        measured low end of the scale range the disc is ~11 px, i.e. 2.8 px at quarter
        resolution, and the owner's plate simply never reached the top-3.  (2) The coarse
        pass sizes its template BY THE ROW (_coarse_map), because a single coarse size
        cannot rank a disc that is half of it.

        SECOND PASS.  The row->scale fit is a cost optimisation, and a camera it does not
        describe must cost time, never a lock.  So when the first pass yields nothing, ONE
        more coarse pass runs with a flat template size taken from the ladder in rotation:
        successive failed acquisitions therefore sweep the whole ladder, and an unfamiliar
        camera is found within a few acquisition periods instead of never.
        """
        try:
            H, W = frame_bgr.shape[:2]
            by0 = int(self.BAND_Y0_F * H)
            by1 = int(self.BAND_Y1_F * H)
            band = frame_bgr[by0:by1]
            if band.shape[0] < 64:
                return None
            scales = self._scales()
            div = max(1, int(_fenv("ORION_ANCHOR_COARSE_DIV", 2.0)))
            gb = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            gs = (gb if div == 1 else
                  cv2.resize(gb, (gb.shape[1] // div, gb.shape[0] // div),
                             interpolation=cv2.INTER_AREA))
            fx = gs.shape[1] / float(W)
            fy = gs.shape[0] / float(band.shape[0])
            passes = [None]
            # The second pass is THROTTLED, not free: it doubles the acquisition's cost and
            # the acquisition is the module's whole p99. Measured on the 09-12 dump, running
            # it on every failed acquisition took the anchor's p99/max from 5.1/10.5 ms to
            # 12.5/16.5 -- at the frame budget. Every Nth failure keeps the recovery (an
            # unfamiliar camera is still found inside a few acquisition periods) and pays
            # for it once in N. ORION_ANCHOR_COARSE_FALLBACK=0 removes it entirely.
            every = max(1, int(_fenv("ORION_ANCHOR_COARSE_FALLBACK_EVERY", 3.0)))
            self._acq_n = int(getattr(self, "_acq_n", 0)) + 1
            if (_fenv("ORION_ANCHOR_COARSE_FALLBACK", 1.0) > 0.0
                    and int(_fenv("ORION_ANCHOR_COARSE_STRIPS", 3.0)) > 1
                    and self._acq_n % every == 0):
                self._fallback_i = (int(getattr(self, "_fallback_i", -1)) + 1) % len(scales)
                passes.append(scales[self._fallback_i])
            ps_min = self._ps_floor()
            tx_min = _fenv("ORION_ANCHOR_TX_MIN", 0.35)
            best = None
            for flat_scale in passes:
                best = self._acquire_pass(frame_bgr, tmpl, s, gs, fx, fy, by0,
                                          scales, ps_min, flat_scale)
                if best is not None:
                    break
            if best is None:
                return None
            if self._tag is not None and best[5] >= tx_min:
                self._note_confirmed(best[4], best[3])
            return (best[1], best[2], best[3], best[4], best[5])
        except Exception:
            self.stats["error"] += 1
            return None

    def _acquire_pass(self, frame_bgr, tmpl, s, gs, fx, fy, by0, scales, ps_min,
                      flat_scale=None):
        """One coarse pass + refine. flat_scale=None uses the per-row coarse sizes."""
        try:
            flat = self._coarse_map(gs, tmpl, s, fx, fy, by0, scales,
                                    flat_scale=flat_scale)
            if flat is None:
                return None
            peaks = []
            # With an identity to rank by, look at more of them: the owner's plate is often
            # not the STRONGEST disc on the floor, only the one with his gamertag beside it.
            topk = int(_fenv("ORION_ANCHOR_COARSE_TOPK", 8.0))
            sup_x = max(4, int(round(24.0 * s * fx)))
            sup_y = max(4, int(round(20.0 * s * fx)))
            want = max(1, topk if self._tag is not None else max(3, topk // 2))
            coarse_min = _fenv("ORION_ANCHOR_COARSE_MIN", 0.34)
            # [ORION_ANCHOR_COARSE_PER_STRIP 2026-09-19] TAKE THE PEAKS PER STRIP, NOT
            # GLOBALLY. _coarse_map already sizes the template by the row, and that is
            # exactly why a GLOBAL top-k cannot rank across the band: the top strip's
            # template is half the size of the bottom strip's, and a 5 px template
            # correlates with anything, so TM_CCOEFF_NORMED there runs 0.62-0.82 on crowd,
            # stands and scoreboard texture while the owner's real plate at the foot of the
            # court scores 0.46-0.61. Measured on the six presses the 09-19 baseline lost
            # to `not_in_topk` (050219 seq 9/18/23, 135725 seq 2/15, 152024 seq 8): the
            # true plate is NOT in the global top-16 on most of their frames, and it is in
            # its OWN strip's top-3 on every frame where the oracle sees it. Splitting the
            # same budget across the strips therefore costs no extra refine work -- the
            # candidates simply stop all coming from the crowd.
            # ORION_ANCHOR_COARSE_PER_STRIP=0 restores the global pick exactly.
            n_strip = (int(_fenv("ORION_ANCHOR_COARSE_STRIPS", 3.0))
                       if flat_scale is None else 1)
            if (_fenv("ORION_ANCHOR_COARSE_PER_STRIP", 1.0) <= 0.0 or n_strip <= 1
                    or flat.shape[0] < 3 * n_strip):
                bands = [(0, flat.shape[0], want)]
            else:
                per = max(1, (want + n_strip - 1) // n_strip)
                rows = flat.shape[0]
                bands = [(int(rows * k / n_strip), int(rows * (k + 1) / n_strip), per)
                         for k in range(n_strip)]
            for r0, r1, n_take in bands:
                sub = flat[r0:r1]
                if sub.shape[0] < 3:
                    continue
                for _ in range(n_take):
                    _, mx, _, loc = cv2.minMaxLoc(sub)
                    if mx < coarse_min:
                        break
                    peaks.append((float(mx), loc[0] / fx, (loc[1] + r0) / fy + by0))
                    # suppress in the PARENT map so a peak on a strip seam is not taken
                    # twice by two neighbouring strips
                    gy = loc[1] + r0
                    cv2.rectangle(flat, (loc[0] - sup_x, gy - sup_y),
                                  (loc[0] + sup_x, gy + sup_y), -9.0, -1)
            if not peaks:
                return None
            gate = _fenv("ORION_ANCHOR_PLATE_GATE", 1.0) > 0.0
            plate_w = _fenv("ORION_ANCHOR_PLATE_W", 0.45)
            full_w = _fenv("ORION_ANCHOR_FULL_W", 0.5)
            best = None
            for _c, cx, cy in peaks:
                # Refine the ladder rungs the candidate's OWN ROW allows first: the same
                # perspective fit the coarse pass uses, so a 6-rung ladder usually costs 3
                # matches per peak, not 6. If NONE of them clears the floor the rest of the
                # ladder is tried anyway -- the fit is a cost optimisation, never a veto, so
                # a camera it does not describe costs time and not a lock.
                row = self._refine_scales(scales, cy / max(1e-6, s))
                rest = tuple(m for m in scales if m not in row)
                got = False
                for group in (row, rest):
                    for m in group:
                        t = self._scaled(tmpl, m * s)
                        if t is None:
                            continue
                        tw, th = t.shape[1], t.shape[0]
                        reg, rx0, ry0 = self._gray(
                            frame_bgr, cx - tw * 0.5 - 10 * s, cy - th * 0.5 - 8 * s,
                            cx + tw * 0.5 + 10 * s, cy + th * 0.5 + 8 * s)
                        if reg is None or reg.shape[0] <= th or reg.shape[1] <= tw:
                            continue
                        _, mx, _, loc = cv2.minMaxLoc(
                            cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))
                        if mx < ps_min:
                            continue
                        ix = rx0 + loc[0] + tw * 0.5
                        iy = ry0 + loc[1] + th * 0.5
                        st, st_ok = self._tag_stats(frame_bgr, ix, iy, m * s)
                        if gate and not st_ok:
                            continue          # a disc with no nameplate beside it
                        got = True
                        tx = self._score_tag(frame_bgr, ix, iy, m * s)
                        # identity first, structure second, correlation third: with a
                        # learned gamertag the owner's plate outranks a stronger disc on
                        # somebody else's plate; without one, a disc that CARRIES a
                        # nameplate outranks a brighter blob that does not.
                        rank = (mx + (0.0 if tx <= -8.0 else 1.5 * tx)
                                + plate_w * st + full_w * self._full_ps(frame_bgr, ix, iy,
                                                                       m * s))
                        if best is None or rank > best[0]:
                            best = (rank, ix, iy, m, mx, tx)
                    if got or not rest:
                        break
            return best
        except Exception:
            self.stats["error"] += 1
            return None

    @staticmethod
    def _scaled(tmpl, sc):
        if sc <= 0.0:
            return None
        if abs(sc - 1.0) < 1e-3:
            return tmpl
        out = cv2.resize(tmpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        if out.shape[0] < 5 or out.shape[1] < 5:
            return None
        return out


ANCHOR = PlayerAnchor()


def reset_all(keep_identity: bool = True) -> None:
    ANCHOR.reset(keep_identity=keep_identity)
    ARM.reset()
