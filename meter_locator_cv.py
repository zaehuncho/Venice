"""meter_locator_cv.py -- pure-CV meter PROPOSER (A/B stand-in for the YOLO locator).

[ORION_METER_PROPOSER=cv] Owner request 2026-09-10: rule the YOLO path in or out for the random
earlies/lates by swapping ONLY the box proposer. This class has the MeterYoloLocator contract
(detect_box / detect_box_tile -> (x, y, w, h, conf) in full-frame pixels, or None) so
meter_detector_yolo.AsyncMeterLocator wraps it unchanged and simple_meter_reader measures fill
inside the box through the same code path it uses for the YOLO box. Nothing downstream changes.

The meter (2K27 Arrow2/White, 720p) is a translucent dark track ~26 px wide, ~120 px tall, with a
green triangle at the top, a white fill column rising from the bottom and a chevron notch at the
base. Measured on session_20260909_200332 (149 reader boxes, fill 15-95): white column 11 px wide
centred in the box (7 px left margin, 8 right), green tip top = box top + 8, white bottom = box
bottom - 8 (the reader's TRACKED box; the raw landmark measurement read 11 before the even-kernel
row shift was removed), green top -> white bottom = 100 px (rMAD 1.5) at every fill. So the box is built from
two landmarks that do not move with the fill: the white column's bottom and the green tip's top.

Gates (generic CV, in the order they run):
  1. neutral-bright mask: max(B,G,R) >= 225 and max-min <= 25 (the fill is achromatic white;
     court wood, jerseys and skin carry chroma);
  2. vertical gap close (<= 3 rows) so compression breaks do not split the column;
  3. minimum vertical support per column (default 6 px): court lines and lettering are thin
     horizontally-long structures and drop out; the fill column survives from ~6 % fill;
  4. connected component of plausible width (8..22 px @720p) and solidity;
  5. green tip: HSV-green confirmed in a small window a FIXED 100 px (@720p) above the column's
     bottom, centred on it. A white column WITHOUT a tip there is not a meter (a jersey number,
     a sock, a court line end); a hot-streak green cloud cannot move the box (prior geometry).
  6. roaming ROI: after a hit the search collapses to a margin around the last box for a short
     hold; a miss there re-opens the full band.
  7. (opt-in, ORION_CV_OUTLINE_MIN) the track's own light-grey outline -- unreliable: the outline
     is translucent and takes the colour of the court under it (0.00 median on a wood floor).
  8. a LONE column: a scoreboard digit or jersey number always has a white twin beside it.
  9. [ORION_CV_SHAPE_GATE 2026-09-12] the meter's OPAQUE parts have a fixed shape: the white fill
     is a straight rectangle of constant width and the green tip is a compact triangle confined
     to the capsule. A lit white jersey (the owner's own), shorts, socks, the scoreboard and the
     shot chart all pass gates 1-8 with a green head, court line or chart as the "tip"; none of
     them has a straight constant-width fill with a compact tip. Judged on FIRST sight only; a
     box accepted a moment ago is bridged so tracking survives a full or occluded fill.
 10. [ORION_ANCHORED_SEARCH 2026-09-15, default OFF] WHERE and WHEN, from player_anchor.py.
     Gates 1-9 all ask "does this look like a meter?", and the answer at low fill is "cannot
     tell" -- which is why gate 9 DEFERS anything shorter than 14 px and why the first
     accepted fill is ~18 % on the owner's court and ~22 % elsewhere. The ball handler's
     nameplate answers a different question: relative to it the meter box sits at a nearly
     fixed offset (centre-x - 30 px, bottom + 150 px @720p), so one plate sighting predicts a
     ~170 x 270 px patch -- 4 % of the band. That patch is searched FIRST, with a relaxed
     floor (ORION_CV_SHAPE_MIN_H_ARMED), and while the anchor is CONFIDENT (it has learned
     the owner's own gamertag) a candidate outside it is refused. A sub-floor fill still has
     to be seen rising on two consecutive frames inside the press's onset window
     (ORION_EXPECTATION_WINDOW) -- a jersey number does not grow.
Region/size plausibility is the same fraction-of-frame gate the YOLO locator applies.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np

try:                                   # [ORION_PLAYER_ANCHOR] optional, inert when absent
    import player_anchor as _pa
except Exception:                      # pragma: no cover - a missing module must never
    _pa = None                         # break the shipped locator


def _fenv(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else float(default)
    except Exception:
        return float(default)


class MeterContourLocator:
    """Pure-CV meter proposer with the MeterYoloLocator interface."""

    def __init__(self):
        self.ok = True
        self.provider = "cv-contour"
        self.model_path = "cv:contour(white-column+green-tip)"
        self._prep_sess = None
        self._prep_disabled = "n/a (pure CV)"
        self._infer_lock = threading.Lock()
        # Same fraction-of-frame plausibility gate as the YOLO locator (region + size).
        self._band_top = _fenv("ORION_METER_BAND_TOP", 0.20)
        self._band_bot = _fenv("ORION_METER_BAND_BOTTOM", 0.95)
        # [ORION_METER_GATE_TOP 2026-09-12] The ACCEPTANCE gate's top, separate from the scan
        # band. The scan has always started at band_top - 0.12 (= 0.08), but _plausible refused
        # any box whose centre sat above band_top (0.20): measured on 7,509 detected frames the
        # highest meter centre was 0.216, i.e. meters were being clipped and the live symptom
        # was press_unanswered_no_meter on jumping fades and far shots (the player is higher on
        # screen, the meter rides with him). Widening band_top itself was tried and reverted the
        # same day: it also moved the scan to 0.00 and detector infer went 8-12 -> 16-19 ms.
        # This knob moves ONLY the gate, to the scan's own top: no new pixels are scanned. The
        # scoreboard that lives up there is refused by gate 9 (shape), not by the band.
        self._gate_top = _fenv("ORION_METER_GATE_TOP", max(0.0, self._band_top - 0.12))
        self._w_min_f = _fenv("ORION_METER_W_MIN_FRAC", 0.013)
        self._w_max_f = _fenv("ORION_METER_W_MAX_FRAC", 0.035)
        self._h_min_f = _fenv("ORION_METER_H_MIN_FRAC", 0.07)
        self._h_max_f = _fenv("ORION_METER_H_MAX_FRAC", 0.25)
        # Gate constants @720p (scaled by frame height / 720).
        self.v_min = int(_fenv("ORION_CV_WHITE_V_MIN", 225))
        self.spread_max = int(_fenv("ORION_CV_CHANNEL_SPREAD_MAX", 25))
        self.min_support = int(_fenv("ORION_CV_MIN_SUPPORT", 6))
        self.gap_close = int(_fenv("ORION_CV_GAP_CLOSE", 3))
        self.col_w_min = _fenv("ORION_CV_COL_W_MIN", 8.0)
        self.col_w_max = _fenv("ORION_CV_COL_W_MAX", 22.0)
        self.tip_gap = _fenv("ORION_CV_TIP_GAP", 100.0)          # green top above white bottom (measured)
        self.tip_tol = _fenv("ORION_CV_TIP_TOL", 5.0)             # +- rows the tip may sit from the prior
        self.tip_dx_max = _fenv("ORION_CV_TIP_DX_MAX", 6.0)
        self.tip_px_min = int(_fenv("ORION_CV_TIP_PX_MIN", 3))
        _green_s_min = int(max(1, min(255, _fenv("ORION_CV_GREEN_S_MIN", 90))))
        self._GREEN_LO = (38, _green_s_min, 90)   # instance override of the class constant
        self.lone_gap = _fenv("ORION_CV_LONE_GAP_PX", 20.0)      # gate 8: no white twin within this gap
        # No "green above the tip" guard: the hot-streak cloud sits right above a REAL tip
        # (session_20260909_202502, fill ~79 frames) and such a guard hands the frame to the
        # next candidate. The gate values above (V>=225, support 6, width 8-22) are what keep
        # the nameplate-under-a-ball false lock out (offbox 0 on three sessions).
        # Box geometry from the landmarks (measured, see module docstring).
        # [2026-09-11] pads 4/3 put the CV box on the ruler the engine's rung table and tip constant
        # were learned on (the YOLO tracked box, D ~108 px): replay of session_20260909_202502 with
        # 8/10 read the landing at 87.6 and fit the rise at RMS 8.0 ms; 4/3 reads 90.1 at RMS 3.2 ms
        # (YOLO 93.6). The remaining ~3.5 pp is the zero point, under investigation.
        self.box_pad_top = _fenv("ORION_CV_BOX_PAD_TOP", 4.0)
        self.box_pad_bot = _fenv("ORION_CV_BOX_PAD_BOT", 3.0)
        self.box_w = _fenv("ORION_CV_BOX_W", 26.0)
        # Roaming ROI after a hit.
        self.roi_hold_s = _fenv("ORION_CV_ROI_HOLD_S", 0.6)
        self.roi_pad_x = _fenv("ORION_CV_ROI_PAD_X", 48.0)
        self.roi_pad_y = _fenv("ORION_CV_ROI_PAD_Y", 64.0)
        # Gate 7 (2026-09-10): the meter's own OUTLINE. The track has two thin light-grey
        # vertical edges ~5 px outside the white column; a nameplate, menu text or a player's
        # sock has no such pair. Measured on 1,671 real detections: best-edge support median
        # 0.98; the six false locks of the night (202502:420/433, 200332:315/762, 194153:818)
        # scored <= 0.36. A real meter whose outline is hidden (arm across it, court line) is
        # still accepted when the same box was accepted a moment ago (temporal fallback), so
        # the gate costs first sight nothing and only refuses one-frame strangers.
        # DEFAULT OFF (opt-in, 0.45 is the measured operating point). On the replay of
        # session_20260909_202502 the gate + one-frame confirmation moved the reader's first
        # read from fill 15 to 40 (p90 68) because the framedump is sparse (one frame per
        # 0.1-0.8 s) and a refused first sight waits a whole interval; live at 60 fps the cost is
        # one frame, but that is unproven, and a first read past the 40 % anchor gate means no
        # fire. The five single-frame strangers it catches (0.3 % of no-meter frames) are left to
        # the reader's own corroboration until a live batch with cv={no_outline=..} says
        # otherwise. Enable with ORION_CV_OUTLINE_MIN=0.45.
        self.outline_min = _fenv("ORION_CV_OUTLINE_MIN", 0.0)
        self.outline_recent_s = _fenv("ORION_CV_OUTLINE_RECENT_S", 0.20)
        self.outline_recent_px = _fenv("ORION_CV_OUTLINE_RECENT_PX", 12.0)
        # [ORION_CV_TIP_CORROBORATE 2026-09-11] The conf-0.75 acceptance below ("a column as tall
        # as the meter's own fill run IS the meter") takes a white bar on GEOMETRY ALONE, with no
        # green tip to confirm it. Owner reports the visible symptom: the box snaps onto the COURT
        # for a split second. A court line at the right camera angle is exactly an achromatic
        # 8-22 x ~90 px bar, so the assumption does not hold on every court.
        #
        # A real meter is never a one-frame stranger: it rises for ~35 frames at 60 fps and its
        # green tip is confirmable (conf 0.90) for nearly all of them, so by the time the white
        # washes the tip out past ~88 % fill there is always a fresh, co-located prior. A court
        # line has no such history. So an UNCONFIRMED candidate must be corroborated by a sighting
        # at the same place a moment earlier; the first sighting is remembered, not accepted.
        # Cost on a genuine meter first seen already washed out: ONE frame (~17 ms). Confirmed
        # (green-tip) candidates are completely unaffected -- this never touches the 0.90 path.
        self.tip_corroborate = _fenv("ORION_CV_TIP_CORROBORATE", 1.0) > 0.0
        self.tip_recent_s = _fenv("ORION_CV_TIP_RECENT_S", 0.20)
        # [ORION_CV_SHAPE_GATE 2026-09-12] Gate 9: the shape of the meter's OPAQUE parts. The track
        # and outline are translucent and look like whatever court is under them (gate 7 read 0.00
        # on the owner's wood floor), so only the white fill and the green tip are the meter's own.
        # The fill is a straight 11-12 px rectangle: row widths constant (spread <= 0.35 of the
        # median above the chevron notch), both edges straight (sd <= 1.2 px), solidity >= 0.75.
        # The tip is confined to the capsule: nothing green in the 8-14 px ring beside it (real
        # meters: 0 at p50, 7 px at p90 even with a green 3-pt line passing behind the track).
        # Measured on session_20260912_201355 (owner's court, 6,000 frames): production took 222
        # white columns OUTSIDE any shot -- the owner's own white FEVER jersey with the green alien
        # head as the tip, the scoreboard, the shot chart, socks, shorts -- and at the press those
        # false locks sat on the shooter, so the real meter was found late (deadline missed) or the
        # box re-seated mid-rise (geometry break, ownership_proof_incomplete). The gate refuses
        # 95 % of them and no real meter (both2 rule, every "real" it lost was a jersey the sidecar
        # itself had locked). Fills shorter than 14 px are DEFERRED, not judged: the notch makes
        # them irregular, a real fill grows ~4 px per 60 fps frame so the cost is one frame, and
        # the static 8 x 10-13 px jersey bits that pass everything else never grow. Judged on first
        # sight only: a candidate co-located with the box accepted within tip_recent_s is bridged,
        # so a fill crossed by an arm or washed to the arrow top keeps tracking.
        self.shape_gate = _fenv("ORION_CV_SHAPE_GATE", 1.0) > 0.0
        self.shape_min_h = _fenv("ORION_CV_SHAPE_MIN_H", 14.0)
        self.shape_sol_min = _fenv("ORION_CV_SHAPE_SOL_MIN", 0.75)
        self.shape_rw_cv_max = _fenv("ORION_CV_SHAPE_RW_CV_MAX", 0.35)
        self.shape_edge_sd_max = _fenv("ORION_CV_SHAPE_EDGE_SD_MAX", 1.2)
        self.tip_spill_frac = _fenv("ORION_CV_TIP_SPILL_FRAC", 0.25)
        self.tip_spill_px = _fenv("ORION_CV_TIP_SPILL_PX", 4.0)
        # [ORION_METER_TOP_STRIP 2026-09-14] The 09-12 gate fix above moved the ACCEPTANCE
        # floor to 0.08 H but left the SCAN starting at row 0.08*H = 57 @720p, and the scan is
        # what actually binds. Gate 5 looks for the green tip a fixed 100 px ABOVE the white
        # column's bottom; for a meter whose tip sits above row 57 that window falls outside
        # the scanned sub-image, gets clamped to its first row, finds no green there and the
        # candidate dies as `no_tip` -- the column itself is still in the band, only its head
        # is not. Measured on 245,821 detected boxes over 18 detframes sessions the box-top
        # histogram is CENSORED exactly there: 743 boxes at y 100-109, 628 at 90-99, 328 at
        # 80-89, 223 at 70-79, then 4 at 60-69 and 0 at 50-59 (the two at 40-49 came in on the
        # roaming ROI, which may already look above the band). The acquisition floor works out
        # at box top y >= 58, i.e. centre 0.155 H -- twice the gate's own 0.08. That floor is
        # the far-shot symptom the owner reports ("can't see from beyond half court"): the
        # further the shooter, the higher on screen the meter rides with him.
        #
        # This knob does NOT scan a single extra pixel (widening the band to 0.00 was tried on
        # 09-12 and reverted: detector infer went 8-12 -> 16-19 ms). It only lets the two SMALL
        # windows that read ABOVE the white column -- gate 5's tip window (~13 x 16 px) and
        # gate 9's tip-spill ring (~29 x 12 px) -- be taken from the parent frame instead of
        # being clamped to the sub-image's first row. Cost when a candidate's tip prior is
        # inside the sub-image (every candidate in the normal population): one float compare.
        # DEFAULT OFF so the concurrent re-encode replay keeps the shipped behaviour; flip to 1
        # once both studies are in.
        # Default OFF (2026-09-14 evening): the offline validation (0/12,000 court frames changed,
        # 61/80 real meters recovered at box top 8..40 px, cost-neutral) is real, but it went live
        # in the same launches as two other regressions and could not be cleared by a controlled
        # live A/B before the ship. Re-enable with ORION_METER_TOP_STRIP=1 after a live A/B on a
        # Rec court (compare ownership `break_geometry` counts and abort rate against the clamp).
        self.top_strip = _fenv("ORION_METER_TOP_STRIP", 0.0) > 0.0
        # [ORION_ANCHORED_SEARCH / ORION_EXPECTATION_WINDOW 2026-09-15] "know WHERE and
        # WHEN to look". Gate 9's 14 px floor (above) is what makes the first accepted fill
        # ~18 % on the owner's court and ~22 % elsewhere: below that the chevron notch makes
        # the column irregular and a static 8-13 px jersey/scoreboard/sock bit is
        # indistinguishable from a meter ON SHAPE ALONE. player_anchor.py supplies the
        # missing coordinate -- the ball handler's nameplate predicts a ~170 x 250 px patch
        # that must contain the meter -- so a short fill can be judged on WHERE it is
        # instead of on how straight it is. Three separate knobs (SHIP defaults below):
        #   ORION_ANCHORED_SEARCH   search the anchor's patch FIRST, with the relaxed floor,
        #                           and refuse candidates outside it while the anchor is
        #                           confident (the false-lock guarantee);
        #   ORION_CV_SHAPE_MIN_H_ARMED  the floor that applies inside the patch while a press
        #                           is armed (SHIP 8, against gate 9's unarmed 14);
        #   ORION_EXPECTATION_WINDOW  inside the patch AND inside the press's onset window,
        #                           a sub-floor fill may be accepted once it is seen RISING
        #                           on two consecutive frames -- the rise pair replaces the
        #                           shape test that a 8 px column cannot pass.
        # [SHIP CONFIG 2026-09-17] All four now default to the graded ship values rather than
        # to "today's behaviour". Every session from 09-15 on -- including the 09-16 20:26
        # session the owner called perfect -- ran with ANCHORED_SEARCH=1 EXPECTATION_WINDOW=1
        # CV_SHAPE_MIN_H_ARMED=8 CV_COL_W_MIN_ARMED=6 on the dev launch line, and a packaged
        # install inherits no environment at all. Setting any of these to 0 (or to the old
        # 14/8 floors) restores the pre-ship behaviour exactly.
        self.anchored_search = _fenv("ORION_ANCHORED_SEARCH", 1.0) > 0.0
        self.expect_window = _fenv("ORION_EXPECTATION_WINDOW", 1.0) > 0.0
        # `min(...)` rather than a flat 8: the armed floor is a RELAXATION of gate 9, so it must
        # never end up ABOVE the unarmed floor. 8 is the ship value against the shipped 14; a
        # dev A/B that lowers ORION_CV_SHAPE_MIN_H still gets an armed floor that follows it.
        self.shape_min_h_armed = _fenv("ORION_CV_SHAPE_MIN_H_ARMED",
                                       min(8.0, self.shape_min_h))
        # Gate 4's width floor inside the patch. The fill column is 11 px wide at a full
        # meter but the FIRST few percent do not render at full width: measured on the same
        # 120 low-fill frames the achromatic run is 7 px on 25 of them and 8-9 px on 69, so
        # the shipped 8 px floor alone loses a fifth of the low-fill population. SHIP 6, and
        # only inside the patch -- the unarmed gate keeps its 8.
        # Same shape as shape_min_h_armed above: 6 against the shipped 8, and never above the
        # unarmed floor if a dev A/B moves that one.
        self.col_w_min_armed = _fenv("ORION_CV_COL_W_MIN_ARMED", min(6.0, self.col_w_min))
        # 0 = a confident anchor SKIPS the band scan entirely (cheapest); 1 = the band is
        # still scanned and every out-of-patch candidate is refused and COUNTED, so the
        # guarantee is observable in the logs. 1 by default: the owner has to be able to
        # see what the anchor refused before he trusts it to refuse silently.
        self.refuse_mode = int(_fenv("ORION_ANCHOR_REFUSE_MODE", 1.0))
        # [ORION_CV_TIPLESS_ARMED 2026-09-15] Gate 5 (the green tip) is the ONE gate a real
        # meter can fail outright. Measured on session_20260915_185359, epochs 8 (Right Fade)
        # and 21 (Left Fade): the meter was on screen, unoccluded, mid-frame, its white column
        # rising 19 -> 42 -> 63 -> 95 px (ep8) and 26 -> 49 -> 72 -> 104 px (ep21) over the
        # 320 ms before the blind deadline, and the strict-green count in the apex window was
        # 0-4 px for the WHOLE rise (every locked shot in the same session reads 12-36). Both
        # presses died `no_tip` and the engine fired blind. Neither the 0.75 meter-tall escape
        # (it needs h within 0.85-1.08 of tip_gap, i.e. a nearly full meter) nor the green S
        # floor (S>=60 read 1-5 px, same refusal) recovers them.
        #
        # So: a white column may be proposed WITHOUT a tip, at a lower confidence, but only
        # when everything else the locator knows says a meter is due --
        #   * a press is ARMED and this frame is inside that press's own onset window
        #     (player_anchor.onset_window_ms: fades 575-1075 ms, Standstill 50-550 ms);
        #   * the column passed gates 1-4 and 8 (achromatic, supported, plausible width,
        #     lone) like every other candidate;
        #   * it is TALL ENOUGH FOR GATE 9 TO ACTUALLY JUDGE IT (>= shape_min_h, no abstain):
        #     the shape test is the only evidence left once the tip is gone, so the tipless
        #     path may never inherit gate 9's low-fill abstention;
        #   * it has RISEN on two consecutive frames in the same column -- a court line, a
        #     jersey number, a sock and the scoreboard do not grow.
        # Outside an armed onset window NOTHING changes: idle frames never reach this path,
        # which is what keeps the court-line false-lock class closed.
        self.tipless_armed = _fenv("ORION_CV_TIPLESS_ARMED", 1.0) > 0.0
        # Capped below the 0.75 meter-tall escape where it is used, so a tip-less candidate can
        # never out-rank a candidate that showed the locator an actual tip.
        self.tipless_conf = _fenv("ORION_CV_TIPLESS_CONF", 0.70)
        self.tipless_pair_s = _fenv("ORION_CV_TIPLESS_PAIR_MS", 200.0) / 1000.0
        self.tipless_tol_px = _fenv("ORION_CV_TIPLESS_TOL_PX", 14.0)
        # [ORION_READER_GHOST_FORGET_LOCATOR box= 2026-09-16] The co-location tolerance the
        # SURGICAL forget uses to decide whether a remembered first-sight pair is the ghost's
        # own column or somebody else's. It is deliberately the WIDEST of the locator's three
        # existing pair tolerances (outline/tip 12 px, tip-less 14 px, expectation 16 px), so
        # a pair the locator itself would have matched to the forgotten box is always cleared:
        # the surgery may only ever keep pairs the locator could NOT have bridged anyway.
        self._forget_tol_px = max(self.outline_recent_px, self.tipless_tol_px,
                                  _fenv("ORION_EXPECT_PAIR_TOL_PX", 16.0),
                                  _fenv("ORION_CV_FORGET_TOL_PX", 0.0))
        self._scale = 1.0                    # last frame's 720p scale (set by _detect_scan)
        self._last_box = None
        self._last_ts = -1.0e9
        self._tip_pending = None             # (cx, y, t) of an unconfirmed first sight
        self._pending = None                 # (cx, y, t) of an outline-less first sight
        self._anchor = None                  # last player_anchor.Anchor (or None)
        self._anchor_ts = None               # frame ts the anchor above was computed on
        self._expect = None                  # (cx, wbot, col_h, ts) of a sub-floor first sight
        self._tipless = None                 # (cx, wbot, col_h, ts, n) of a tip-less sighting
        self._find_col = None                # (col_h, wbot, cx) of the winning candidate
        self._find_tipless = False           # the winning candidate had NO green tip
        self._pickup_epoch = -1
        self.last_ms = 0.0
        self.anchor_ms = 0.0
        # Per-shot pickup forensics; the reader reads this and emits one PICKUP: line.
        self.pickup = self._blank_pickup()
        # Per-press tipless forensics; the reader reads this and emits one TIPLESS LOCK: line.
        self.tipless = self._blank_tipless()
        self.stats = {"calls": 0, "roi": 0, "full": 0, "hit": 0, "col_cands": 0, "no_tip": 0,
                      "no_outline": 0, "outline_bridged": 0, "outline_weak": 0, "not_lone": 0,
                      "no_tip_lone": 0, "shape_short": 0, "shape_irregular": 0, "tip_spill": 0,
                      "shape_bridged": 0, "shape_error": 0, "top_strip": 0, "error": 0,
                      "shape_abstain": 0,
                      "anchor_hit": 0, "anchor_patch_hit": 0, "refused_outside": 0,
                      "expect_pending": 0, "expect_accept": 0,
                      "tipless_pending": 0, "tipless_accept": 0, "tipless_track": 0,
                      "idle_reuse": 0}
        # ------------------------------------------------------------------ #
        # [ORION_LOCATOR_IDLE_REUSE 2026-09-15] THE IDLE SCAN OF A FROZEN FRAME.
        #
        # The stall attributor named `meter-yolo` inside `_find` on 1080p frames that had
        # not changed a byte, while the game sat in a menu or a loading screen. Measured
        # here on a static 1080p menu frame the full band scan is 32.5 ms (p95 41, max 60):
        # it runs the neutral-bright mask, the gap close, the component pass and gate 9 over
        # ~800k pixels to answer a question whose answer cannot have changed, because the
        # PIXELS did not change.
        #
        # So: when NO press is armed and the sub-image is byte-identical to the one this
        # same scope was handed last time, the previous RESULT is served instead of
        # re-running the scan. Two bounds keep it honest:
        #   * armed frames are NEVER reused -- a shot always pays for a real scan, so this
        #     cannot cost a single frame of a real meter's first sight;
        #   * a reused answer expires after ORION_LOCATOR_IDLE_REUSE_MS (250), so a wedged
        #     signature (a capture card re-serving one buffer) can hide a real scan for at
        #     most a quarter second.
        # The scan's own temporal state is advanced exactly as the re-run would advance it
        # (`_last_box`/`_last_ts`, `hit`), so the roaming ROI and the gate-9 bridge see the
        # same history they would have seen -- reuse is a cost optimisation, not a policy.
        # [SHIP CONFIG 2026-09-17] DEFAULT OFF. The optimisation is sound and its tests stay,
        # but it was OFF in every graded ship session (the 09-16 launch line lists it under
        # OFF), so the packaged install must match: a reused answer is a cost win on idle
        # frames and a behavioural risk nobody has live hours on. ORION_LOCATOR_IDLE_REUSE=1
        # re-arms it unchanged.
        self._idle_reuse = _fenv("ORION_LOCATOR_IDLE_REUSE", 0.0) > 0.0
        self._idle_reuse_s = max(0.0, _fenv("ORION_LOCATOR_IDLE_REUSE_MS", 250.0)) / 1000.0
        # Row stride for the static signature. 4, not 24: the signature has to be finer than
        # the smallest change that can matter, and a meter grows ~4 px per 60 fps frame, so
        # every 4th row is the coarsest stride that is GUARANTEED to sample a change spanning
        # 4 consecutive rows. A 24-row stride measured 0.048 ms and silently declared a
        # 5-px-taller fill "static" (it fell between two sampled rows) -- the exact failure
        # the locator's own one-frame confirmation tests caught. Measured cost of stride 4:
        # 0.88 ms at 1080p, 0.12 ms at 720p, against the 32 ms scan it replaces.
        self._idle_sig_stride = max(1, int(_fenv("ORION_LOCATOR_IDLE_SIG_STRIDE", 4.0)))
        self._idle_cache = {}       # scope key -> (signature, t_computed, result)

    @staticmethod
    def _blank_pickup():
        return {"epoch": 0, "first_sight_fill": -1.0, "first_sight_ms_after_press": -1.0,
                "anchor_used": 0, "anchor_conf": 0.0, "refused_outside_patch": 0,
                "expect_accept": 0, "anchor_ms": 0.0, "logged": 0}

    @staticmethod
    def _blank_tipless():
        """[ORION_CV_TIPLESS_ARMED] One record per press; `epoch` 0 = the path never fired."""
        return {"epoch": 0, "fill": -1.0, "rise_frames": 0, "conf": 0.0, "logged": 0}

    # [ORION_CV_GREEN_S_MIN 2026-09-14] The green-tip HSV floor. The SATURATION floor (90) is the
    # single binding gate on H.264 decoder frames: 4:2:0 chroma subsampling averages the small green
    # triangle with the white fill beside it (measured on 283 true-meter frames re-encoded through the
    # Chiaki rungs, tools/quality/reencode_gate_study.py: every gate-9 metric keeps 3-10x margin, the
    # tip S floor alone loses 4-43 frames; S>=60 recovers 6-10 pp of first-sight locks with 0 false
    # locks on 1,213 adversarial frames and is exactly neutral on capture-card pixels). The knob is
    # set per ROUTE by the sidecar (decoder -> 60); the capture-card path stays at 90, byte-identical.
    _GREEN_LO = (38, 90, 90)
    _GREEN_HI = (85, 255, 255)

    # ------------------------------------------------------------------ interface
    # The async wrapper passes the FRAME timestamp (seconds) when this is True, so the
    # ROI hold and the one-frame confirmation run on frame time in live AND in replay.
    accepts_ts = True

    def reset(self) -> None:
        """Forget the temporal state (source change / clock restart); the wrapper calls it."""
        with self._infer_lock:
            self._last_box = None
            self._last_ts = -1.0e9
            self._tip_pending = None
            self._pending = None
            self._anchor = None
            self._anchor_ts = None
            self._expect = None
            self._tipless = None
            self._idle_cache.clear()   # a new source's pixels are not the old source's answer
            if _pa is not None:
                # keep the learned gamertag/offsets: a source restart is not a new player
                _pa.ANCHOR.reset(keep_identity=True)

    # ------------------------------------------------------------------ pair memory
    # The four TWO-FRAME PROMOTION PAIRS. Each one is "I saw something here once; if the same
    # column shows me the same thing again (grown, where required) I will accept it". They are
    # the only way a first sight can ever be promoted, which is why they are enumerated in one
    # place: whatever forgets them decides whether a real meter can be acquired at all.
    #   _tip_pending  (cx, y, t)                 gate 5, the tip-corroborate first sight
    #   _pending      (cx, y, t)                 gate 7, the outline-less first sight
    #   _expect       (cx, wbot, col_h, ts)      [ORION_EXPECTATION_WINDOW] sub-floor rise pair
    #   _tipless      (cx, wbot, col_h, ts, n)   [ORION_CV_TIPLESS_ARMED] tip-less rise pair
    _PAIR_FIELDS = ("_tip_pending", "_pending", "_expect", "_tipless")
    _PAIR_TS_INDEX = {"_tip_pending": 2, "_pending": 2, "_expect": 3, "_tipless": 3}

    def _pair_window_s(self, name: str) -> float:
        if name == "_tip_pending":
            return float(self.tip_recent_s)
        if name == "_pending":
            return float(self.outline_recent_s)
        if name == "_expect":
            return max(0.0, _fenv("ORION_EXPECT_PAIR_MS", 120.0)) / 1000.0
        return float(self.tipless_pair_s)

    def pending_pairs(self, now: Optional[float] = None) -> Tuple[str, ...]:
        """Which first-sight pairs are STILL LIVE (inside their own promotion window).

        [ORION_READER_FORGET_RATE_LIMIT 2026-09-16] The reader asks before it evicts: a pair
        that is one frame from deciding must be allowed to decide.  Names are returned without
        their leading underscore ('pending', 'expect', 'tipless', 'tip_pending').  Never raises.
        """
        try:
            t = float(now) if (now is not None and now == now) else time.monotonic()
        except (TypeError, ValueError, OverflowError):
            t = time.monotonic()
        out = []
        try:
            with self._infer_lock:
                for name in self._PAIR_FIELDS:
                    mem = getattr(self, name, None)
                    if mem is None:
                        continue
                    try:
                        age = t - float(mem[self._PAIR_TS_INDEX[name]])
                    except (TypeError, ValueError, IndexError, OverflowError):
                        continue
                    if 0.0 <= age <= self._pair_window_s(name):
                        out.append(name.lstrip("_"))
        except Exception:
            return tuple(out)
        return tuple(out)

    def forget_position(self, box=None) -> Tuple[str, ...]:
        """Forget WHERE the last meter was, keeping every learned identity/parameter.

        [ORION_READER_GHOST_FORGET_LOCATOR 2026-09-15] ``_last_box``/``_last_ts`` are the
        locator's own co-location memory, and THREE acceptance paths lean on it: the roaming
        ROI (``roi_hold_s``), the gate-9 bridge, and -- the load-bearing one -- the
        tip-corroborate and outline-support escapes, which accept a candidate that has NO
        confirmed green tip (conf < 0.90) or NO outline support purely because "something was
        accepted at this spot a moment ago".  After the reader evicts or retires a leftover
        (ghost) meter at a press, that spot IS the ghost, so those escapes hand the ghost's own
        column straight back as the next proposal; the reader then re-locks it, evicts it again
        and re-derives its per-lock ruler each cycle (live 2026-09-15 22:37Z: 2-11 evictions and
        +5..+12 locks per press with drops flat).  Forgetting the position costs a real meter at
        most one confirmation frame -- it still has its own tip/outline -- and costs the ghost
        its free pass.  Deliberately NOT ``reset()``: the anchor, the learned identity, the
        pending inference and the published result all survive.

        [ORION_READER_GHOST_FORGET_LOCATOR box= 2026-09-16] ...BUT THE FIRST VERSION ALSO
        FORGOT THE PAIRS, AND THAT IS WHAT BLINDED THE READER.  Live 2026-09-16 14:20:46-
        14:21:11 (epochs 22-27, six consecutive blind presses, all backstopped): with the
        previous shot's meter still on screen the reader evicted it ~10 times a second
        (`loc_forget` 0->148 in lockstep with `locks`, `drops` flat: 39 locks / 42 seeds /
        39 forgets in 4 s), and every one of those calls wiped ``_pending`` / ``_expect`` /
        ``_tipless`` -- the TWO-FRAME PROMOTION PAIRS the relaxation trio needs to accept a
        12-14 %% onset and the tip-less path needs to accept a washed-tip rise.  The real
        meter's pair could never complete, so first publication landed 100-170 ms late
        (`BOX LATCHED` 810-912 ms after the press against a 750 ms deadline).

        So the forget is now SURGICAL.  ``box`` is the ghost the caller is evicting:

          * ``_last_box``/``_last_ts`` always go -- that IS the positional memory, and it is
            the thing that hands the ghost back;
          * a pair whose column is CO-LOCATED with ``box`` (the locator's own co-location
            tolerance) is the ghost's own first sight and goes with it;
          * a pair on a DIFFERENT column belongs to something else -- on a rapid re-press that
            something else is this press's real meter -- and is KEPT, so it can still promote.

        ``box`` may be a 4-tuple (x, y, w, h) or a 2-tuple centre (cx, cy).  ``box=None`` means
        "the caller does not know which object this was" and keeps the original, total amnesia.

        Returns the names of the pairs that were KEPT (for the caller's log line).
        """
        with self._infer_lock:
            cx = None
            if box is not None:
                try:
                    if len(box) >= 4 and float(box[2]) > 0.0:
                        cx = float(box[0]) + float(box[2]) * 0.5
                    elif len(box) >= 2:
                        cx = float(box[0])
                except (TypeError, ValueError, IndexError, OverflowError):
                    cx = None
            tol = self._forget_tol_px * max(0.5, float(self._scale))
            kept = []
            for name in self._PAIR_FIELDS:
                mem = getattr(self, name, None)
                if mem is None:
                    continue
                same = True                       # unknown column -> treat as the ghost's
                if cx is not None:
                    try:
                        same = abs(float(mem[0]) - cx) <= tol
                    except (TypeError, ValueError, IndexError, OverflowError):
                        same = True
                if same:
                    setattr(self, name, None)
                else:
                    kept.append(name.lstrip("_"))
            self._last_box = None
            self._last_ts = -1.0e9
            # [ORION_LOCATOR_IDLE_REUSE] the cached answer was computed WITH the position
            # memory this call is throwing away; serving it again would hand the ghost back.
            self._idle_cache.clear()
            return tuple(kept)

    def detect_box(self, frame_bgr, ts: Optional[float] = None) -> Optional[Tuple[int, int, int, int, float]]:
        if not self.ok or frame_bgr is None:
            return None
        with self._infer_lock:
            return self._detect_locked(frame_bgr, (0, 0), None, ts, frame_bgr)

    def detect_box_tile(self, frame_bgr, side: str, fraction: float = 0.55, ts: Optional[float] = None
                        ) -> Optional[Tuple[int, int, int, int, float]]:
        if not self.ok or frame_bgr is None:
            return None
        try:
            H, W = frame_bgr.shape[:2]
            _side = str(side or "").strip().lower()
            if _side not in ("left", "right") or H <= 0 or W <= 0:
                return None
            cw = max(1, min(W, int(round(W * min(1.0, max(0.05, float(fraction)))))))
            x0 = 0 if _side == "left" else W - cw
        except Exception:
            return None
        with self._infer_lock:
            return self._detect_locked(frame_bgr[:, x0:x0 + cw], (x0, 0), (W, H), ts, frame_bgr)

    def _plausible(self, x: int, y: int, w: int, h: int, W: int, H: int) -> bool:
        try:
            if W <= 0 or H <= 0:
                return True
            cy = (y + h * 0.5) / float(H)
            if not (self._gate_top <= cy <= self._band_bot):
                return False
            wf, hf = w / float(W), h / float(H)
            return (self._w_min_f <= wf <= self._w_max_f) and (self._h_min_f <= hf <= self._h_max_f)
        except Exception:
            return True

    # ------------------------------------------------------------------ core
    # ------------------------------------------------------------------ anchor
    def _press_state(self, now):
        """-> (armed, ms_since_press, shot_type). All zeros when nothing is published.

        The engine's shot gate can stay armed for tens of seconds (and for a 3 s tail after
        the release), but a meter's whole life is under 1.5 s. ORION_ANCHOR_ARM_S bounds the
        anchor to the window in which a meter can actually exist, so a stuck-armed gate can
        never turn into a permanent per-frame cost.
        """
        if _pa is None:
            return (False, -1.0, "")
        try:
            # Indexed, not unpacked: ArmState.state() is APPEND-ONLY (it grew a `rhythm`
            # field with [ORION_SHOT_GATE_TYPE]), and a locator that unpacks it exactly
            # would turn every future field into a per-frame ValueError swallowed by the
            # except below -- i.e. the anchor would silently stop arming.
            _st = _pa.ARM.state()
            epoch, press_ts, shot_type, armed = _st[0], _st[1], _st[2], _st[3]
            if not armed or press_ts <= -1.0e8:
                return (False, -1.0, "")
            dt = (now - press_ts) * 1000.0
            if not (-1.0 <= dt <= _fenv("ORION_ANCHOR_ARM_S", 2.5) * 1000.0):
                return (False, dt, str(shot_type))
            return (True, float(dt), str(shot_type))
        except Exception:
            return (False, -1.0, "")

    def _update_anchor(self, frame_bgr, now, armed):
        """Run the nameplate anchor for this frame. Only while a press is armed: outside a
        shot the meter cannot exist, so the anchor would be pure cost.

        The same frame can reach the locator twice (the reader's phased acquire submits a
        full scan and a left/right tile), so the result is memoised on the frame timestamp:
        one plate search per frame, never two.
        """
        if _pa is None or not _pa.enabled() or not armed:
            self._anchor = None
            self.anchor_ms = 0.0
            self._anchor_ts = None
            return None
        if self._anchor_ts is not None and self._anchor_ts == now:
            return self._anchor
        a = _pa.ANCHOR.update(frame_bgr, ts=now, armed=True)
        self.anchor_ms = float(getattr(_pa.ANCHOR, "last_ms", 0.0))
        self._anchor = a
        self._anchor_ts = now
        if a is not None:
            self.stats["anchor_hit"] += 1
        return a

    def open_pickup_record(self, epoch) -> None:
        """Open a fresh per-press forensics record for `epoch` (idempotent per epoch).

        [ORION_PICKUP_PER_PRESS 2026-09-15] THE RECORD IS OPENED BY THE PRESS, NOT BY THE
        FIRST FRAME THAT HAPPENS TO LOOK FOR A METER. It used to be opened only inside
        _detect_locked, which runs on the LOCATOR's own path: a press whose meter the locator
        never went looking for (the shot-gated reader skipping it, a detector proposal taken
        elsewhere, a press that simply never produced a candidate) kept the PREVIOUS press's
        record -- already `logged=1` -- so its release flushed nothing. 37 presses in the
        2026-09-15 session produced TWO PICKUP lines for exactly that reason, and the presses
        it silently dropped are the misses: an `anchor_used=0` line is the evidence, not the
        absence of a line.

        Detection is untouched: this writes only the forensics dict.
        """
        try:
            ep = int(epoch or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if ep <= 0 or int(self.pickup.get("epoch", 0)) == ep:
            return
        rec = self._blank_pickup()
        rec["epoch"] = ep
        self.pickup = rec               # one rebind: the reader thread never sees a half-record

    def open_tipless_record(self, epoch) -> None:
        """Fresh per-press TIPLESS record for `epoch` (idempotent per epoch).

        [ORION_CV_TIPLESS_ARMED] Deliberately NOT folded into open_pickup_record: that one is
        the ANCHOR's forensics and only runs while ORION_PLAYER_ANCHOR is on, while the tipless
        path needs only WHEN (the press window) and ships on by default. One rebind, so the
        reader thread never reads a half-written record.
        """
        try:
            ep = int(epoch or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if ep <= 0 or int(self.tipless.get("epoch", 0)) == ep:
            return
        rec = self._blank_tipless()
        rec["epoch"] = ep
        self.tipless = rec

    def _note_pickup(self, epoch, press_dt_ms, col_h, anch, s):
        """First accepted sight of this press: fill, lateness, and what found it. The record
        itself is opened when the press is published (open_pickup_record) and re-checked here
        in _detect_locked, so a refusal counted before the first sight is not thrown away by
        the sight that follows it."""
        p = self.pickup
        if p["first_sight_fill"] < 0.0 and col_h is not None:
            p["first_sight_fill"] = round(100.0 * float(col_h) / max(1.0, self.tip_gap * s), 1)
            p["first_sight_ms_after_press"] = round(float(press_dt_ms), 1)
            p["anchor_used"] = int(anch is not None)
            p["anchor_conf"] = round(float(anch.conf), 3) if anch is not None else 0.0
            p["anchor_ms"] = round(float(self.anchor_ms), 3)

    def _detect_locked(self, img, offset, plaus_size, ts=None, full_img=None):
        """[ORION_LOCATOR_IDLE_REUSE] Static-frame short circuit around the real scan.

        See __init__: between shots the band scan re-derives the same answer from the same
        pixels 60 times a second. Reuse is refused outright while a press is armed, so the
        scan a real shot depends on is never skipped, never delayed and never stale.
        """
        if not self._idle_reuse:
            return self._detect_scan(img, offset, plaus_size, ts, full_img)
        t0 = time.perf_counter()
        now = float(ts) if (ts is not None and ts == ts) else time.monotonic()
        key = sig = None
        if self._pending is not None or self._tip_pending is not None:
            # A candidate awaiting its one-frame confirmation (gate 7's outline fallback,
            # gate 5's tip corroboration) is MID-DECISION: the next look at the same pixels is
            # what DECIDES it, not a repeat of the same question. Reuse is refused, and the
            # cache is dropped -- the cached answer for these pixels was computed before the
            # sighting existed, so it is about to be wrong.
            self._idle_cache.clear()
        elif self._press_state(now)[0]:
            # Armed: a shot always pays for a real scan, and nothing cached from before the
            # press may outlive it.
            self._idle_cache.clear()
        else:
            try:
                key = (offset, img.shape)
                sig = hash(img[::self._idle_sig_stride].tobytes())
            except Exception:
                key = sig = None
        if key is not None:
            prev = self._idle_cache.get(key)
            if (prev is not None and prev[0] == sig
                    and 0.0 <= (now - prev[1]) <= self._idle_reuse_s):
                res = prev[2]
                if res is not None and now >= self._last_ts:
                    # The re-run this replaces would have re-found the same box on the same
                    # pixels and re-stamped its position memory; the roaming ROI and the
                    # gate-9 bridge must not decay just because the scan was skipped.
                    self._last_box = (int(res[0]), int(res[1]), int(res[2]), int(res[3]))
                    self._last_ts = now
                    self.stats["hit"] += 1
                self.stats["calls"] += 1
                self.stats["idle_reuse"] += 1
                self.last_ms = (time.perf_counter() - t0) * 1000.0
                return res
        res = self._detect_scan(img, offset, plaus_size, ts, full_img)
        if key is not None:
            if len(self._idle_cache) > 8:
                self._idle_cache.clear()   # scope/geometry churn: never an unbounded map
            self._idle_cache[key] = (sig, now, res)
        return res

    def _detect_scan(self, img, offset, plaus_size, ts=None, full_img=None):
        t0 = time.perf_counter()
        self.stats["calls"] += 1
        try:
            H, W = img.shape[:2]
            fullW, fullH = plaus_size if plaus_size else (W, H)
            s = fullH / 720.0
            # The surgical forget runs OUTSIDE a scan and still has to speak in the same
            # pixels the pair tolerances were measured in.
            self._scale = s
            now = float(ts) if (ts is not None and ts == ts) else time.monotonic()
            # ------------------------------------------------ where and when to look
            armed, press_dt_ms, shot_type = self._press_state(now)
            if armed and _pa is not None:
                # One forensics record per press. The reader opens it when the press is
                # published (open_pickup_record); this is the same call for the paths that
                # reach the locator first, and it is idempotent per epoch.
                # [ORION_CV_TIPLESS_ARMED] The ARM is now published whenever the tipless path
                # is on, so the anchor's own record must stay behind the anchor's switch or a
                # tipless-only install would start emitting PICKUP: lines it cannot fill in.
                if _pa.enabled():
                    self.open_pickup_record(int(_pa.ARM.epoch))
                self.open_tipless_record(int(_pa.ARM.epoch))
            # [ORION_CV_TIPLESS_ARMED] WHEN a tip-less column may be proposed at all: inside
            # an armed press's own onset window and nowhere else.
            tipless = bool(self.tipless_armed and armed and self._in_onset(press_dt_ms, shot_type))
            anch = self._update_anchor(full_img if full_img is not None else img, now, armed)
            # Roaming ROI (gate 6): a margin around the last hit for a short hold.
            roi = None
            if self._last_box is not None and 0.0 <= (now - self._last_ts) <= self.roi_hold_s and not plaus_size:
                lx, ly, lw, lh = self._last_box
                rx0 = int(max(0, lx - self.roi_pad_x * s)); ry0 = int(max(0, ly - self.roi_pad_y * s))
                rx1 = int(min(W, lx + lw + self.roi_pad_x * s)); ry1 = int(min(H, ly + lh + self.roi_pad_y * s))
                if rx1 - rx0 > 8 and ry1 - ry0 > 8:
                    roi = (rx0, ry0, rx1, ry1)
            by0 = int(max(0, (self._band_top - 0.12) * H)); by1 = int(min(H, (self._band_bot + 0.03) * H))
            used_roi = roi is not None
            if roi is None:
                roi = (0, by0, W, by1)
                self.stats["full"] += 1
            else:
                self.stats["roi"] += 1
            rx0, ry0, rx1, ry1 = roi
            # gate 9 bridge: the box accepted a moment ago, in the sub-image's coordinates
            bridge = None
            if self._last_box is not None and 0.0 <= (now - self._last_ts) <= self.tip_recent_s:
                btol = (self.outline_recent_px + min(90.0, 600.0 * (now - self._last_ts))) * s
                bridge = (self._last_box[0] + self._last_box[2] * 0.5 - offset[0],
                          self._last_box[1] - offset[1], btol)
            sub_bridge = lambda ox, oy: (bridge[0] - ox, bridge[1] - oy, bridge[2]) if bridge else None
            # [ORION_METER_TOP_STRIP] parent pixels for the two windows that read ABOVE the
            # white column; None keeps the shipped clamp-to-the-sub-image behaviour exactly.
            parent = lambda ox, oy: (img, ox, oy) if self.top_strip else None
            # ------------------------------------------------ [ORION_ANCHORED_SEARCH]
            # The anchor's patch is searched FIRST and with the relaxed floor. The patch is
            # ~4 % of the band, so this costs ~0.4 ms; it wins the shot when the real meter
            # is shorter than gate 9's floor or when a bigger white impostor would otherwise
            # out-rank it on area (the loop below ranks by (conf, area), and the owner's own
            # white jersey is always the larger blob).
            best = None
            expect_hit = False
            patch = None
            if self.anchored_search and anch is not None and anch.valid():
                # The patch straddles the shooter's torso (the plate is at his feet and the
                # meter rides 150-260 px above it), so his JERSEY NUMBER is inside it. Gate 8
                # -- "a meter has no white twin beside it" -- can only see the components in
                # the sub-image it is handed, so the SCAN rect is padded sideways by more than
                # lone_gap; the winner is then still required to land inside the true patch.
                # Without the pad the patch would quietly weaken the one gate that refuses
                # scoreboard digits and jersey numbers.
                pad = _fenv("ORION_ANCHOR_PATCH_PAD_X", 40.0) * s
                px0 = int(max(0, anch.x0 - pad - offset[0]))
                px1 = int(min(W, anch.x1 + pad - offset[0]))
                py0 = int(max(0, anch.y0 - offset[1])); py1 = int(min(H, anch.y1 - offset[1]))
                if px1 - px0 > 12 and py1 - py0 > 24:
                    patch = (px0, py0, px1, py1)
                    # `parent` is passed UNCONDITIONALLY here (not under TOP_STRIP): the patch
                    # is our own artificial crop, so clamping gate 5's tip window to its first
                    # row would invent a blind strip that does not exist in the band scan.
                    floor = self.shape_min_h_armed if armed else self.shape_min_h
                    wfloor = self.col_w_min_armed if armed else self.col_w_min
                    best = self._find(img[py0:py1, px0:px1], s, sub_bridge(px0, py0),
                                      (img, px0, py0), min_h=floor, w_min=wfloor,
                                      tipless=tipless)
                    if best is not None and not anch.contains_box(
                            best[0] + px0 + offset[0], best[1] + py0 + offset[1],
                            best[2], best[3]):
                        best = None            # it won inside the PAD, not inside the patch
                    if best is not None:
                        rx0, ry0, used_roi = px0, py0, True
                        self.stats["anchor_patch_hit"] += 1
                        expect_hit = self._expectation_ok(
                            now, press_dt_ms, shot_type, armed, s,
                            px0 + offset[0], py0 + offset[1])
                        if not expect_hit:
                            best = None
            if best is None and patch is None:
                best = self._find(img[ry0:ry1, rx0:rx1], s, sub_bridge(rx0, ry0), parent(rx0, ry0),
                                  tipless=tipless,
                                  crop_edges=((rx0 > 0, ry0 > 0, rx1 < W, ry1 < H)
                                              if used_roi else None))
                if best is None and used_roi:
                    # miss inside the ROI: re-open the band once
                    rx0, ry0 = 0, by0
                    best = self._find(img[by0:by1, 0:W], s, sub_bridge(0, by0), parent(0, by0),
                                      tipless=tipless)
                    self.stats["full"] += 1
            elif best is None:
                # The patch missed. A CONFIDENT anchor now owns the frame: either the band is
                # not scanned at all (refuse_mode 0, cheapest) or it is scanned and every
                # candidate the anchor does not contain is refused and counted (refuse_mode 1,
                # the default -- the guarantee has to be visible before it is trusted).
                may_refuse = (_pa is not None and _pa.PlayerAnchor.refuse_ok(anch))
                if not (may_refuse and self.refuse_mode == 0):
                    rx0, ry0 = 0, by0
                    best = self._find(img[by0:by1, 0:W], s, sub_bridge(0, by0), parent(0, by0),
                                      tipless=tipless)
                    self.stats["full"] += 1
                    if best is not None and may_refuse:
                        _bx = best[0] + rx0 + offset[0]; _by = best[1] + ry0 + offset[1]
                        if not anch.contains_box(_bx, _by, best[2], best[3]):
                            self.stats["refused_outside"] += 1
                            self.pickup["refused_outside_patch"] = \
                                int(self.pickup.get("refused_outside_patch", 0)) + 1
                            best = None
            if best is None:
                if not (0.0 <= (now - self._last_ts) <= self.roi_hold_s):
                    self._last_box = None
                self._pending = None            # a stranger seen once, then gone, is forgotten
                return None
            x, y, w, h, conf = best
            x += rx0; y += ry0
            if not self._plausible(x + offset[0], y + offset[1], w, h, fullW, fullH):
                return None
            # [ORION_CV_TIPLESS_ARMED] The winner has no green tip at all. It has already
            # earned WHERE (gates 1-4, 8, 9 -- judged, never abstained) and WHEN (armed +
            # inside the onset window); what is left is the RISE PAIR, which needs the two
            # sightings' full-frame coordinates and therefore cannot live inside _find.
            tipless = bool(self._find_tipless)
            if tipless and not self._tipless_ok(now, s, rx0 + offset[0], ry0 + offset[1],
                                                press_dt_ms, conf):
                return None
            # [ORION_CV_TIP_CORROBORATE 2026-09-11] See __init__. Only the tip-UNCONFIRMED path.
            # A tipless candidate is EXEMPT: its own rise pair is the same evidence this gate
            # asks for (the same column, a moment earlier) plus growth, which a court line at
            # the right camera angle -- the false lock this gate was written for -- cannot fake.
            if self.tip_corroborate and conf < 0.90 and not tipless:
                cxf = x + w * 0.5 + offset[0]
                yyf = y + offset[1]
                seen = None
                if self._last_box is not None and 0.0 <= (now - self._last_ts) <= self.tip_recent_s:
                    seen = (self._last_box[0] + self._last_box[2] * 0.5, self._last_box[1],
                            now - self._last_ts)
                elif (self._tip_pending is not None
                      and 0.0 <= (now - self._tip_pending[2]) <= self.tip_recent_s):
                    # a PENDING sighting was never accepted, so its tolerance may not grow past
                    # two frames or a stranger far away would count as "the same place"
                    seen = (self._tip_pending[0], self._tip_pending[1],
                            min(now - self._tip_pending[2], 2.0 / 60.0))
                # a moving meter (fades) travels up to ~0.6 px/ms: tolerance grows with the gap
                tol = (self.outline_recent_px + min(90.0, 600.0 * seen[2])) * s if seen else 0.0
                if not (seen is not None
                        and abs(seen[0] - cxf) <= tol
                        and abs(seen[1] - yyf) <= tol):
                    self._tip_pending = (cxf, yyf, now)
                    self.stats["no_tip_lone"] = self.stats.get("no_tip_lone", 0) + 1
                    return None
                self._tip_pending = None
            sup = self._outline_support(img, x, y, w, h, s)
            if sup < 0.45:
                self.stats["outline_weak"] = self.stats.get("outline_weak", 0) + 1   # priced, not enforced
            if self.outline_min > 0.0:
                if sup < self.outline_min:
                    # No outline: accept only a candidate seen at the same place on the previous
                    # call (accepted OR pending). A one-frame stranger never gets through; a real
                    # meter on a court where the outline is dim costs ONE frame at first sight.
                    cx = x + w * 0.5 + offset[0]
                    yy = y + offset[1]
                    seen = None
                    if self._last_box is not None and 0.0 <= (now - self._last_ts) <= self.outline_recent_s:
                        seen = (self._last_box[0] + self._last_box[2] * 0.5, self._last_box[1], now - self._last_ts)
                    elif self._pending is not None and 0.0 <= (now - self._pending[2]) <= self.outline_recent_s:
                        # a PENDING sighting was never accepted: its tolerance may not grow past
                        # two frames, or a stranger 100 px away would count as "the same place"
                        seen = (self._pending[0], self._pending[1], min(now - self._pending[2], 2.0 / 60.0))
                    # a moving meter (fades) travels up to ~0.6 px/ms: the tolerance grows with the gap
                    tol = (self.outline_recent_px + min(90.0, 600.0 * seen[2])) * s if seen else 0.0
                    same = (seen is not None
                            and abs(seen[0] - cx) <= tol
                            and abs(seen[1] - yy) <= tol)
                    if not same:
                        self._pending = (cx, yy, now)
                        self.stats["no_outline"] += 1
                        return None
                    self.stats["outline_bridged"] += 1
                    conf = min(conf, 0.7)
                self._pending = None
            if now >= self._last_ts:                # an older frame landing late may not regress the clock
                self._last_box = (x + offset[0], y + offset[1], w, h); self._last_ts = now
            self.stats["hit"] += 1
            _box = (int(x + offset[0]), int(y + offset[1]), int(w), int(h))
            if _pa is not None and _pa.enabled():
                # per-shot pickup forensics (the reader emits one PICKUP: line per press)
                if armed:
                    self._note_pickup(_pa.ARM.epoch, press_dt_ms,
                                      self._find_col[0] if self._find_col else None, anch, s)
                # IDENTITY. This is the bootstrap, and it runs WITHOUT an anchor on purpose:
                # a cold plate search ranks five nameplates on disc correlation alone and picks
                # the wrong one (measured: 4 % of its patches contain the real meter), so the
                # anchor cannot learn who the owner is by looking. A meter that the shipped
                # gates accepted with a confirmed green tip and a full-height fill CAN tell it:
                # the plate is 30 px left of that box's centre and 150 px below its bottom.
                # See player_anchor.note_meter -- forward when an anchor already agrees,
                # reverse (windowed disc search + gamertag cut) when there is nothing yet.
                _full_judgement = (self._find_col is not None
                                   and self._find_col[0] >= self.shape_min_h * s)
                _pa.ANCHOR.note_meter(full_img if full_img is not None else img,
                                      _box, anch, conf >= 0.90 and _full_judgement)
            return (_box[0], _box[1], _box[2], _box[3], float(conf))
        except Exception:
            self.stats["error"] += 1
            return None
        finally:
            self.last_ms = (time.perf_counter() - t0) * 1000.0

    @staticmethod
    def _in_onset(press_dt_ms, shot_type) -> bool:
        """Is `press_dt_ms` inside this shot type's own meter-onset window?

        Shared by the expectation window and the tipless path so the two can never disagree
        about WHEN a meter is due. Fails CLOSED: no player_anchor, no window, no relaxation.
        """
        if _pa is None or press_dt_ms is None or press_dt_ms < 0.0:
            return False
        try:
            lo, hi = _pa.onset_window_ms(shot_type)
        except Exception:
            lo, hi = 50.0, 1100.0
        return bool(lo <= float(press_dt_ms) <= hi)

    def _tipless_ok(self, now, s, ox, oy, press_dt_ms, conf) -> bool:
        """[ORION_CV_TIPLESS_ARMED] May this tip-less winner be proposed?

        Two ways in, and only two:
          * TRACKING -- it sits where a box was accepted a moment ago (the same co-location
            rule gate 9 bridges on). Without it the proposal would die the instant the meter
            TOPS OUT and stops growing, i.e. exactly at the fire;
          * FIRST SIGHT -- it is the second of two consecutive sightings in the same column
            whose fill GREW. The tolerance grows with the frame gap because a fade slides the
            meter up to ~0.6 px/ms sideways (ep8 moved 18-20 px between 110 ms dump frames).
        Anything else is remembered as a pending first sight and refused. One frame of cost to
        a real tip-less meter, and no way in at all for a static white bar.
        """
        col = self._find_col
        if col is None:
            return False
        col_h, wbot, cx = col
        cxf = cx + ox
        botf = wbot + oy
        dtl = now - self._last_ts
        if self._last_box is not None and 0.0 <= dtl <= self.tip_recent_s:
            lb = self._last_box
            tol = (self.outline_recent_px + min(90.0, 600.0 * dtl)) * s
            if (abs(lb[0] + lb[2] * 0.5 - cxf) <= tol
                    and abs(lb[1] + lb[3] - self.box_pad_bot * s - botf) <= tol):
                n = int(self._tipless[4]) if self._tipless else 1
                self._tipless = (cxf, botf, col_h, now, n)
                self.stats["tipless_track"] += 1
                return True
        prev = self._tipless
        if prev is not None:
            dt = now - prev[3]
            tol = (self.tipless_tol_px + min(90.0, 600.0 * max(0.0, dt))) * s
            if (0.0 <= dt <= self.tipless_pair_s and abs(prev[0] - cxf) <= tol
                    and abs(prev[1] - botf) <= tol and col_h > prev[2]):
                n = int(prev[4]) + 1
                self._tipless = (cxf, botf, col_h, now, n)
                self.stats["tipless_accept"] += 1
                self._note_tipless(col_h, n, conf, s)
                return True
        self._tipless = (cxf, botf, col_h, now, 1)
        self.stats["tipless_pending"] += 1
        return False

    def _note_tipless(self, col_h, rise_frames, conf, s) -> None:
        """First tipless acceptance of this press -- the reader logs one TIPLESS LOCK: line."""
        rec = self.tipless
        if rec.get("epoch", 0) and float(rec.get("fill", -1.0)) < 0.0:
            rec["fill"] = round(100.0 * float(col_h) / max(1.0, self.tip_gap * s), 1)
            rec["rise_frames"] = int(rise_frames)
            rec["conf"] = round(float(conf), 2)

    def _expectation_ok(self, now, press_dt_ms, shot_type, armed, s, ox, oy) -> bool:
        """[ORION_EXPECTATION_WINDOW] May the candidate `_find` just chose be accepted?

        A candidate at or above gate 9's own floor is unaffected -- it already passed the
        shape test and this returns True without touching anything. The rule exists only
        for a SUB-FLOOR fill (8..14 px), which no shape test can judge: such a candidate
        must (a) sit inside the anchor's patch -- guaranteed, it was found there -- (b) fall
        inside the press's onset window, and (c) be seen RISING on two consecutive frames.
        A real meter grows ~4 px per 60 fps frame; the static jersey/scoreboard/sock bits
        that the 14 px floor was invented to refuse never grow. Cost to a real meter: one
        frame (~17 ms), against the ~10 pp of fill the relaxed floor buys back.
        """
        col = self._find_col
        if col is None:
            return True
        col_h, wbot, cx = col
        if col_h >= self.shape_min_h * s:
            self._expect = None
            return True                       # ordinary candidate: gate 9 already judged it
        if not self.expect_window:
            return True                       # relaxed floor alone (knob-separable)
        if not armed or press_dt_ms < 0.0:
            return False                      # no press -> a sub-floor fill is never a meter
        if not self._in_onset(press_dt_ms, shot_type):
            return False                      # outside the window: today's confirmation
        cxf = cx + ox
        botf = wbot + oy
        pair_s = _fenv("ORION_EXPECT_PAIR_MS", 120.0) / 1000.0
        tol = _fenv("ORION_EXPECT_PAIR_TOL_PX", 16.0) * s
        prev = self._expect
        if (prev is not None and 0.0 <= (now - prev[3]) <= pair_s
                and abs(prev[0] - cxf) <= tol and abs(prev[1] - botf) <= tol
                and col_h > prev[2]):
            self._expect = None
            self.stats["expect_accept"] += 1
            self.pickup["expect_accept"] = int(self.pickup.get("expect_accept", 0)) + 1
            return True
        self._expect = (cxf, botf, col_h, now)
        self.stats["expect_pending"] += 1
        return False

    def _outline_support(self, img, x, y, w, h, s):
        """Fraction of the track's rows (below the tip, above the notch) that show a light,
        near-grey pixel on the BEST of the two outline bands; nan-safe, 0.0 on any failure."""
        try:
            H, W = img.shape[:2]
            y0 = max(0, int(y + 14 * s)); y1 = min(H, int(y + h - 10 * s))
            if y1 - y0 < 20 * s:
                return 1.0                       # too short to judge: do not refuse
            best = 0.0
            for c0, c1 in ((x + 1 * s, x + 6 * s), (x + w - 6 * s, x + w - 1 * s)):
                c0 = max(0, int(c0)); c1 = min(W, int(c1))
                if c1 <= c0:
                    continue
                strip = img[y0:y1, c0:c1]
                mx = strip.max(axis=2).astype(np.int16); mn = strip.min(axis=2).astype(np.int16)
                light = (mx >= 150) & ((mx - mn) <= 45)
                best = max(best, float((light.sum(axis=1) > 0).mean()))
            return best
        except Exception:
            return 1.0

    def _meter_shaped(self, labels, i, x, y, w, h, area, hsv, cx, gtop, conf, s, parent=None,
                      min_h=None, check_tip=True) -> bool:
        """Gate 9: does this white run + tip have the shape of the meter's opaque parts?

        `min_h` (ORION_CV_SHAPE_MIN_H_ARMED) lowers the floor for the anchored patch only.
        A lower floor must NOT mean "judge a shorter column" -- that was measured and it is
        the wrong reading of the gate. On 120 low-fill frames of session_20260912_201355
        (live fill 1-15 %, meter demonstrably on screen) the real fill run is 10-12 px tall
        on 53 of them, and gate 9's straightness test cannot see it: with h = 11 the body
        slice above the chevron notch is TWO rows, so row-width spread and edge sd are
        measured on two samples and the column is called irregular. Lowering the floor
        alone therefore just moves 53 frames from `shape_short` to `shape_irregular` --
        which is exactly what the first offline A/B showed (recall unchanged, 0/45 and
        1/147 in the two lowest buckets, both arms).

        So between `min_h` and the shipped floor the gate ABSTAINS: it is not that the
        column looks right, it is that shape carries no information there. The caller's
        expectation window then has to earn the acceptance with WHERE (inside the anchor's
        patch) and WHEN (the press's onset window, rising on two consecutive frames) --
        evidence that a jersey number or a scoreboard digit cannot produce.
        """
        try:
            if check_tip and not self._compact_tip(hsv, cx, gtop, conf, s, parent):
                return False
            floor = self.shape_min_h if min_h is None else float(min_h)
            if h < floor * s:
                self.stats["shape_short"] += 1
                return False
            if h < self.shape_min_h * s:
                self.stats["shape_abstain"] = self.stats.get("shape_abstain", 0) + 1
                return True
            m = labels[y:y + h, x:x + w] == i
            notch = int(round(8 * s))
            # judge the straight part only: above the chevron notch and below the arrow head the
            # fill takes on past ~90 % (the top ~10 px of the capsule narrow to the apex)
            top_skip = min(max(1, int(round(gtop + 10 * s)) - y), max(1, h - 2))
            body = slice(top_skip, max(top_skip + 1, h - notch))
            rows = m.sum(axis=1)[body]
            rw_med = float(np.median(rows))
            rw_cv = (float(rows.max()) - float(rows.min())) / max(1.0, rw_med)
            left = np.argmax(m, axis=1)[body]
            right = (w - 1 - np.argmax(m[:, ::-1], axis=1))[body]
            edge_max = self.shape_edge_sd_max * s
            if (area < self.shape_sol_min * w * h or rw_cv > self.shape_rw_cv_max
                    or float(np.std(left)) > edge_max or float(np.std(right)) > edge_max):
                self.stats["shape_irregular"] += 1
                return False
            return True
        except Exception:
            self.stats["shape_error"] += 1
            return False   # a failed measurement is not positive meter evidence

    def _compact_tip(self, hsv, cx, gtop, conf, s, parent=None) -> bool:
        """Current compact-green evidence; past box proximity cannot replace it.

        A bridge may tolerate the white column being crossed by an arm. It
        cannot turn a broad green head/HUD band into a meter tip. Tipless paths
        retain their independent press/rise proof and do not use this test.
        """
        if conf < 0.90:
            return True
        try:
            # a confirmed tip must be COMPACT: green in the ring 8-14 px beside the capsule's
            # centre line is a head, a court line or a chart, never the tip
            H, W = hsv.shape[:2]
            # [ORION_METER_TOP_STRIP] the ring sits beside the tip, so it leaves the
            # sub-image with it; read the same rows from the parent frame instead of
            # silently measuring row 0 (which would price a real meter's ring at 0).
            src, sx, gy0, gy1 = hsv, 0, int(max(0, gtop - 2 * s)), int(min(H, gtop + 10 * s))
            if parent is not None and gtop - 2 * s < 0.0:
                pimg, pox, poy = parent
                py0 = int(max(0, poy + gtop - 2 * s))
                py1 = int(min(pimg.shape[0], poy + gtop + 10 * s))
                px0 = int(max(0, pox + cx - 15 * s))
                px1 = int(min(pimg.shape[1], pox + cx + 15 * s + 1))
                if py1 <= py0 or px1 <= px0:
                    return False
                src = cv2.cvtColor(pimg[py0:py1, px0:px1], cv2.COLOR_BGR2HSV)
                sx = px0 - pox                 # sub-x of the strip's first column
                gy0, gy1 = 0, src.shape[0]
                W = sx + src.shape[1]

            def green(c0, c1):
                c0 = int(max(sx, min(W, c0))) - sx; c1 = int(max(sx, min(W, c1))) - sx
                if c1 <= c0 or gy1 <= gy0:
                    return 0
                return int(cv2.countNonZero(
                    cv2.inRange(src[gy0:gy1, c0:c1], self._GREEN_LO, self._GREEN_HI)))
            g_in = green(cx - 8 * s, cx + 8 * s + 1)
            g_mid = green(cx - 14 * s, cx - 8 * s) + green(cx + 8 * s + 1, cx + 14 * s)
            if g_in <= 0:
                return False  # missing pixels are not evidence of a compact green tip
            if g_mid > self.tip_spill_frac * g_in + self.tip_spill_px * s * s:
                self.stats["tip_spill"] += 1
                return False
            return True
        except Exception:
            self.stats["shape_error"] += 1
            return False

    def _find(self, sub, s, bridge=None, parent=None, min_h=None, w_min=None, tipless=False,
              crop_edges=None):
        """`parent` is (image, ox, oy) -- the pixels `sub` was sliced out of and its origin in
        them -- or None. It is used ONLY by [ORION_METER_TOP_STRIP], and only for the tip
        window / tip-spill ring of a candidate whose head sits above `sub`'s first row.
        `min_h` is gate 9's deferral floor and `w_min` gate 4's width floor for this call
        ([ORION_CV_SHAPE_MIN_H_ARMED] / [ORION_CV_COL_W_MIN_ARMED]); both default to the
        shipped values, so a call that passes neither is byte-identical to the old one.
        `tipless` ([ORION_CV_TIPLESS_ARMED]) lets a column with NO green tip reach the
        ranking at the tipless confidence; False (the default) is the shipped behaviour.
        `crop_edges` marks artificial left/top/right/bottom roaming boundaries.
        An ambiguous component there abstains so the caller scans the full band
        on this SAME frame; crop geometry is never evidence of meter geometry."""
        self._find_col = None
        self._find_tipless = False
        if sub.size == 0:
            return None
        b, g, r = cv2.split(sub)
        mx = cv2.max(cv2.max(b, g), r)
        mn = cv2.min(cv2.min(b, g), r)
        # gate 1: achromatic bright (uint8 SIMD ops; the full band costs ~3 ms @720p)
        bright = cv2.threshold(mx, self.v_min - 1, 255, cv2.THRESH_BINARY)[1]
        spread = cv2.subtract(mx, mn)
        neutral = cv2.threshold(spread, self.spread_max, 255, cv2.THRESH_BINARY_INV)[1]
        white = cv2.bitwise_and(bright, neutral)
        if cv2.countNonZero(white) == 0:
            return None
        # gate 2: vertical gap close
        # Mirrored anchors: OpenCV's default anchor on an EVEN kernel walks every run one row
        # down (CLOSE and OPEN alike), which moved the white-bottom landmark by 1-2 px depending
        # on the resolution (review 2026-09-10 #1). erode/dilate with (0, k//2) then (0, (k-1)//2)
        # keeps the run where it is for any k.
        gc = max(1, int(round(self.gap_close * s))) + 2
        kc = np.ones((gc, 1), np.uint8)
        white = cv2.erode(cv2.dilate(white, kc, anchor=(0, gc // 2)), kc, anchor=(0, (gc - 1) // 2))
        # gate 3: vertical support -- an opening with a tall thin kernel keeps only runs >= min_support
        sup = max(2, int(round(self.min_support * s)))
        ks = np.ones((sup, 1), np.uint8)
        col = cv2.dilate(cv2.erode(white, ks, anchor=(0, sup // 2)), ks, anchor=(0, (sup - 1) // 2))
        if cv2.countNonZero(col) == 0:
            return None
        n, labels, stats, cents = cv2.connectedComponentsWithStats(col, connectivity=8)
        if n <= 1:
            return None
        hsv = None
        wmin = (self.col_w_min if w_min is None else float(w_min)) * s
        wmax = self.col_w_max * s
        tip_gap = self.tip_gap * s
        best = None
        best_col = None
        best_tipless = False
        # [2026-09-13] Component table as arrays. The old per-candidate Python loop over every
        # component (the lone-column check) cost 12 ms on a crowd frame with 306 components and
        # pushed the sidecar past the 16.7 ms frame budget (cv_fps 51, detect 21 ms) -- a run of
        # late-dated anchors = a run of LATE shots. Same rules, vectorized; identical results.
        cx_all = stats[:, cv2.CC_STAT_LEFT]; cy_all = stats[:, cv2.CC_STAT_TOP]
        cw_all = stats[:, cv2.CC_STAT_WIDTH]; ch_all = stats[:, cv2.CC_STAT_HEIGHT]
        ca_all = stats[:, cv2.CC_STAT_AREA]
        keep_mask = (cw_all >= 3) & (ch_all >= sup)                # twins may be any width
        keep_mask[0] = False
        keep_idx = np.flatnonzero(keep_mask)
        cand_idx = keep_idx[(cw_all[keep_idx] >= wmin) & (cw_all[keep_idx] <= wmax)]
        order = cand_idx[np.argsort(-ca_all[cand_idx], kind="stable")][:12]
        kx, ky, kw, kh = cx_all[keep_idx], cy_all[keep_idx], cw_all[keep_idx], ch_all[keep_idx]
        for i in order:
            i = int(i)
            x, y, w, h, area = (int(v) for v in stats[i])
            self.stats["col_cands"] += 1
            if crop_edges is not None:
                # A local crop can remove the adjacent half of a scoreboard/jersey
                # number, turning it into a false "lone" column. Preserve enough
                # side context for even the smallest qualifying twin. Likewise,
                # morphology at a cropped row can invent a white bottom landmark
                # (and thus change the fill ruler). Do not accept that partial
                # component merely because remembered shape bridges the crop.
                left, top, right, bottom = crop_edges
                side_context = self.lone_gap * s + max(3.0, 4.0 * s)
                row_context = max(gc, sup)
                if ((left and x <= side_context)
                        or (right and sub.shape[1] - (x + w) <= side_context)
                        or (top and y <= row_context)
                        or (bottom and sub.shape[0] - (y + h) <= row_context)):
                    self.stats["crop_context_missing"] = self.stats.get("crop_context_missing", 0) + 1
                    continue
            # gate 8 (2026-09-11): a LONE column. The meter's white fill has nothing white beside
            # it (dark track, grey outline); a scoreboard digit ("02", "15") or a jersey number
            # always has another white glyph of similar height within a few pixels -- three
            # wide-open shots in one game were aborted on the "02" of the score panel with the
            # 3-point line playing the green tip. Another achromatic component that overlaps
            # this one vertically by >= 60 % and sits within lone_gap px edge-to-edge kills it.
            lone_gap = self.lone_gap * s
            # a twin GLYPH is about the candidate's own size; a court line beside the meter
            # is far taller (it spans the frame) and must not count
            gap = np.maximum(kx - (x + w), x - (kx + kw))
            ov = np.minimum(y + h, ky + kh) - np.maximum(y, ky)
            twin = ((keep_idx != i) & (gap <= lone_gap)
                    & (ov >= 0.6 * np.minimum(h, kh))
                    & (kh >= 0.5 * h) & (kh <= 1.6 * h) & (kw >= 4 * s))
            neighbour = bool(twin.any())
            if neighbour:
                self.stats["not_lone"] = self.stats.get("not_lone", 0) + 1
                continue
            # the chevron notch hollows the bottom of a short column: solidity is low at low fill
            if area < 0.35 * w * h:
                continue
            # gate 5: the green tip sits a FIXED distance above the white column's bottom
            # (100 px @720p, rMAD 1.5 over 149 boxes; HUD elements do not scale with the camera).
            # Only a narrow window at that spot is inspected, so a hot-streak green cloud around
            # the meter can neither pull the centre nor lift the top: the prior sets the geometry
            # and the window only has to CONFIRM green there.
            cx = x + w * 0.5
            wbot = y + h            # white bottom row (exclusive)
            gtop_prior = wbot - tip_gap
            tx0 = int(max(0, cx - self.tip_dx_max * s)); tx1 = int(min(sub.shape[1], cx + self.tip_dx_max * s + 1))
            if tx1 - tx0 < 3:
                continue
            # [ORION_METER_TOP_STRIP] the tip prior may sit ABOVE this sub-image (a far shot
            # rides the meter up the screen). Shipped: the window is clamped to row 0 of the
            # sub, lands on the wrong pixels and the meter dies as `no_tip`. With the knob on
            # the same tiny window is read from the parent frame instead -- no extra scanning.
            gm = None
            if parent is not None and gtop_prior - self.tip_tol * s < 0.0:
                pimg, pox, poy = parent
                py0 = int(max(0, poy + gtop_prior - self.tip_tol * s))
                py1 = int(min(pimg.shape[0], poy + gtop_prior + self.tip_tol * s + 6 * s))
                px0 = int(max(0, pox + tx0)); px1 = int(min(pimg.shape[1], pox + tx1))
                if py1 - py0 < 3 or px1 - px0 < 3:
                    continue
                ty0 = py0 - poy                 # sub-row of the window's first row (may be < 0)
                gm = cv2.inRange(cv2.cvtColor(pimg[py0:py1, px0:px1], cv2.COLOR_BGR2HSV),
                                 self._GREEN_LO, self._GREEN_HI)
                self.stats["top_strip"] = self.stats.get("top_strip", 0) + 1
                if hsv is None:                 # gate 9 reads it below
                    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
            tip_clamped = False
            if gm is None:
                # The tip prior may sit ABOVE this sub-image (the ROI is a tight box around the
                # last hit, and a rising meter's head leaves it through the top). Without
                # [ORION_METER_TOP_STRIP] the window is clamped to row 0 and reads the WRONG
                # pixels, so "no green here" is not a measurement -- remembered below.
                tip_clamped = bool(gtop_prior - self.tip_tol * s < 0.0)
                ty0 = int(max(0, gtop_prior - self.tip_tol * s)); ty1 = int(max(0, gtop_prior + self.tip_tol * s + 6 * s))
                if ty1 - ty0 < 3:
                    continue
                if hsv is None:
                    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
                gm = cv2.inRange(hsv[ty0:ty1, tx0:tx1], self._GREEN_LO, self._GREEN_HI)
            conf = 0.90
            cand_tipless = False
            if cv2.countNonZero(gm) >= self.tip_px_min:
                gy = np.flatnonzero(gm.sum(axis=1) > 0)
                gtop = ty0 + int(gy.min())
                if abs(gtop - gtop_prior) > self.tip_tol * s:
                    gtop = int(round(gtop_prior))
            elif 0.85 * tip_gap <= h <= 1.08 * tip_gap:
                # Past ~88 % fill the white overlays the tip and washes its green out (S < 35 on
                # session_20260909_200332 #739/#746). A column as tall as the meter's own fill run
                # is the meter itself -- nothing else on a court is an achromatic 11 x 90 px bar --
                # so the prior sets the tip and the confidence says the tip was not confirmed.
                gtop = int(round(gtop_prior))
                conf = 0.75
            elif tipless and not tip_clamped and self.shape_gate and h >= self.shape_min_h * s:
                # [ORION_CV_TIPLESS_ARMED] No tip and not meter-tall either: a rising column
                # inside an armed press's onset window. The prior sets the tip exactly as the
                # 0.75 escape does, and the confidence says the tip was never there. The
                # shape_gate/shape_min_h requirement is deliberate -- gate 9 ABSTAINS below
                # its own floor, and an abstention is not evidence, so the one gate still
                # standing must be a gate that actually ran. The rise pair is checked by the
                # caller (_tipless_ok), which is where full-frame coordinates exist.
                gtop = int(round(gtop_prior))
                conf = min(0.74, self.tipless_conf)
                cand_tipless = True
                # `not tip_clamped` above is load-bearing, not caution. Measured on
                # session_20260915_185359 frame 335 with the path armed for every frame: the
                # roaming ROI cut the candidate's tip window off, the shipped code answered
                # `no_tip`, the caller RE-OPENED THE BAND and found the same meter with its
                # real tip at conf 0.90. Letting a clamped window mint a tipless candidate
                # instead ENDS that search at 0.70 -- and the rise pair then refused it, so a
                # real 0.90 proposal was lost. A window we did not actually read is not
                # evidence that the tip is missing.
            else:
                self.stats["no_tip"] += 1
                continue
            # box from the two landmarks
            bw = int(round(self.box_w * s))
            bx = int(round(cx - bw * 0.5))
            by = int(round(gtop - self.box_pad_top * s))
            bh = int(round((wbot - gtop) + self.box_pad_top * s + self.box_pad_bot * s))
            # gate 9 (2026-09-12): first sight must have the meter's shape; a candidate at the
            # place of the box accepted a moment ago is tracking, not first sight. Refusing here,
            # inside the loop, is what lets the real meter win when a jersey out-ranks it on area.
            if self.shape_gate:
                if (bridge is not None and abs(bridge[0] - cx) <= bridge[2]
                        and abs(bridge[1] - by) <= bridge[2]):
                    if not self._compact_tip(hsv, cx, gtop, conf, s, parent):
                        # Green success particles bloom beside a genuine full meter.
                        # At that point the CURRENT full-height straight white body
                        # can prove the shape independently (the same height band as
                        # the washed-tip fallback above). Position alone never can:
                        # short strips and irregular clothing still fail this gate.
                        # A sparkle can sit above the white apex, so exclude the
                        # first 10px of THAT component's head, not the sparkle's.
                        full_body = (0.85 * tip_gap <= h <= 1.08 * tip_gap
                                     and self._meter_shaped(
                                         labels, i, x, y, w, h, area, hsv, cx, max(gtop, y),
                                         conf, s, parent, check_tip=False))
                        if not full_body:
                            self.stats["shape_bridge_tip_refused"] = self.stats.get(
                                "shape_bridge_tip_refused", 0) + 1
                            continue
                        self.stats["shape_full_body_bridged"] = self.stats.get(
                            "shape_full_body_bridged", 0) + 1
                    self.stats["shape_bridged"] += 1
                elif not self._meter_shaped(labels, i, x, y, w, h, area, hsv, cx, gtop, conf, s,
                                            parent, min_h):
                    continue
            # a confirmed tip (0.90) out-ranks the meter-tall fallback (0.75); area breaks ties
            if best is None or (conf, area) > (best[4], best[-1]):
                best = (bx, by, bw, bh, conf, area)
                # the WHITE run behind the winning box, in `sub` coordinates: the expectation
                # window judges the fill's own height, not the landmark box's
                best_col = (int(h), int(wbot), float(cx))
                best_tipless = bool(cand_tipless)
        self._find_col = best_col if best else None
        self._find_tipless = bool(best_tipless) if best else False
        return best[:5] if best else None
