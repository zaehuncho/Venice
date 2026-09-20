#!/usr/bin/env python
"""compressed_meter_reader.py -- the Chiaki/Remote-Play (compressed H.264 4:2:0) meter reader.

CompressedMeterReader extends the production SimpleMeterReader with the LUMA-domain machinery
the compressed stream needs (chroma is what 4:2:0 + heavy encode destroys; luma edges survive):

  * K2 fill read  : when the chroma fill profile is too weak to trust, read the fill boundary
                    from the interior Y-column profile with a robust 4-param logistic fit
                    (luma_meter.fit_fill_boundary) and anchor the fillable height on the
                    BRIGHT TIP SLIVER (the luma twin of the green-cap anchor).
  * stale gate    : encoder skip-MBs repeat the meter pixels on a "new" frame. When the ROI is
                    pixel-frozen while the filter believes the fill is moving, the frame is
                    MISSING DATA: hold everything (no measurement, no confidence decay, no
                    velocity append) instead of feeding a duplicate fill sample.

CHROMA-FIRST COLLAPSE RULE: every chroma gate runs first, unchanged -- on a pristine source
(capture card, high-bitrate) the chroma paths win everywhere, the luma branches are dead code,
and this class behaves byte-identically to SimpleMeterReader (asserted by the collapse
regression in tests/test_compressed_meter_reader.py). Acquisition is still chroma-only until
the K1 rail-pair scan lands (M3).

Selected by ORION_COMPRESSED_READER=1 in the orchestrator (decoder/Chiaki path); the
capture-card path keeps SimpleMeterReader.
"""
from __future__ import annotations

import time as _time
from collections import deque
from typing import Optional

import numpy as np
import cv2

from simple_meter_reader import SimpleMeterReader, ReaderParams
from luma_meter import (PlateauEMA, fit_fill_boundary, find_tip_sliver, rail_pair_scan,
                        roi_sad)
from stream_quality import QualityEstimator


class CompressedMeterReader(SimpleMeterReader):
    """SimpleMeterReader + luma fill read (K2) + encoder-skip stale gate."""

    # stale gate: ROI SAD below max(floor_abs, floor_mult * rolling-quiet-floor) while the
    # fill velocity says the meter should be moving => encoder skip => missing data.
    STALE_SAD_FLOOR = 0.5           # absolute SAD floor (grey levels/px, 4:1 subsampled)
    STALE_FLOOR_MULT = 1.3          # multiple of the rolling quiet-frame noise floor
    STALE_VEL_MIN = 40.0            # %/s the filter must believe before "frozen" is suspicious
    STALE_QUIET_VEL = 20.0          # %/s below this = quiet frame -> feeds the noise floor
    # Deadlock valve: velocity is deliberately FROZEN while stale, so a genuine scene change
    # (capture freeze / cut) would otherwise stay "stale" forever and hold the fill -- the
    # frozen-echo failure class. No real encoder-skip run while RISING lasts this long
    # (~200 ms @60fps); past it, frames fall through to the honest decay/drop cascade.
    STALE_RUN_MAX = 12
    # chroma fill health: this many rows of >=45%-red coverage below the cap = trust chroma
    CHROMA_ROWS_MIN = 2
    # UNARMED luma acquire (design 2a): BOTH gates must hold -- q_session at/below this
    # (bottom two Schmitt bands: the rungs where chroma acquisition is measurably blind)
    # AND this many consecutive reads without a live chroma detection. Pristine sources
    # never satisfy the first gate, so the collapse row is untouched by construction.
    UNARMED_ACQ_Q_MAX = 0.5
    UNARMED_CHROMA_DEAD_MIN = 30           # ~0.5 s @60fps of total chroma silence
    # luma green/tip anchor stabilizer (ladder eval: sliver-only green over-fires on
    # low-bitrate blocking, non-monotonic across rungs). Green is EMITTED only when the
    # chroma green mask corroborates the sliver rows OR the sliver has held a stable row
    # for TIP_PERSIST_MIN consecutive luma reads. The sliver still anchors fillable_h
    # geometry either way -- only the release-target emission is gated.
    TIP_PERSIST_MIN = 3
    TIP_ROW_TOL = 3.0                      # px: sliver-bottom row stability tolerance
    # temporal tip-lock: once the sliver row has persisted TIP_LOCK_MIN frames, refine it
    # in a +/-TIP_LOCK_WIN row window around the locked span (warm start, like prev_y0);
    # a windowed miss falls back to the cold full-profile search -- a lock, not a cage.
    TIP_LOCK_MIN = 5
    TIP_LOCK_WIN = 8
    # velocity-aware edge de-lag (design 1a): the logistic midpoint lags the leading edge
    # on a fast rise. Advance y0 along the motion by k*min(s, |vel|*dt*px/pct), gated on
    # LOW q_frame + confidently-moving velocity so a pristine/slow read is never touched.
    DELAG_K = 0.5
    DELAG_Q_MAX = 0.75

    def __init__(self, frame_w: Optional[int] = None, frame_h: Optional[int] = None,
                 cfg=None, shot_gate=None, params: Optional[ReaderParams] = None,
                 luma_tracking: Optional[bool] = None,
                 require_gameplay_eligibility: bool = False):
        super().__init__(frame_w, frame_h, cfg=cfg, shot_gate=shot_gate, params=params,
                         require_gameplay_eligibility=require_gameplay_eligibility)
        # Master gate for every LUMA branch (K2 fill fallback, rails relocate/acquire,
        # luma evidence). OFF -> byte-identical to SimpleMeterReader (the collapse row;
        # asserted by tests). ON (default) -> the compressed-stream behavior. The M4
        # quality layer will drive this from q_session so a pristine source collapses
        # automatically; until then ORION_LUMA_TRACKING=0 forces the collapse row.
        _arg = luma_tracking
        if _arg is None:
            import os as _os
            env = _os.environ.get("ORION_LUMA_TRACKING", "").strip().lower()
            if env in ("0", "false", "no", "off"):
                _arg = False
            elif env in ("1", "true", "yes", "on"):
                _arg = True
        # Explicit constructor arg / env value STICKS; unset -> quality-managed (auto):
        # the pristine Schmitt band gates the luma branches OFF (the formal collapse),
        # any lower band gates them ON.
        self._luma_gate_auto = _arg is None
        self._luma_tracking = True if _arg is None else bool(_arg)
        # quality layer: q_frame -> R (temporal estimators); q_session (Schmitt bands) ->
        # ReaderParams.for_quality + the luma-tracking gate
        self._qe = QualityEstimator()
        self._q_applied: Optional[float] = None
        self._params_fixed = params is not None
        self._last_chroma_ts: Optional[float] = None
        self._chroma_q_tick = 0
        self._plateaus = PlateauEMA()
        self._prev_y0_strip: Optional[float] = None
        self._luma_diag: dict = {}
        # stale-gate state
        self._stale_prev_roi: Optional[np.ndarray] = None
        self._stale_prev_key = None            # (x0,y0,x1,y1) of the stored ROI
        self._stale_quiet_sads = deque(maxlen=60)
        self._last_read_ts: Optional[float] = None
        self._last_roi_sad = -1.0
        self._stale_run = 0
        # K1 luma acquisition state
        self._acq_hint = None                  # (box, conf, ts) pushed by a locator (YOLO)
        self._decor_registry = []              # pre-arm rail-pair registry: dicts x/spacing/ts
        self._registry_frame = 0
        self._luma_pending = 0                 # E4 confirm countdown after a luma-evidence lock
        self._luma_lock_fill = 0.0
        self._chroma_dead_run = 0              # consecutive reads without a live chroma read
        self._cur_ts: Optional[float] = None   # this read()'s ts (de-lag dt inside _read_fill)
        # luma tip anchor state (persistence + temporal tip-lock), strip-row coordinates
        self._tip_row: Optional[int] = None    # last sliver BOTTOM row (the fillable anchor)
        self._tip_top: Optional[int] = None    # last sliver TOP row (tip-lock window seed)
        self._tip_persist = 0                  # consecutive stable-row sliver frames
        # native decoder Y plane for THIS frame (one-shot: cleared at the end of read();
        # the orchestrator re-sets it before every detect on the pipe path)
        self._native_y = None
        # bug #2 (design doc): the trained Y-student (models/meter_reader_y.pt) was wired
        # to NOTHING. Load it here -- flag-gated inside try_load (ORION_METER_READER_Y=1;
        # returns None when off / model absent / torch missing) -- and use verify() as a
        # CONFIRMER only in _luma_acquire (relax-never-suppress, the acquire-hint contract).
        try:
            from meter_reader_y_infer import try_load as _try_load_reader_y
            self._reader_y = _try_load_reader_y()
        except Exception:
            self._reader_y = None

    def _reset_state(self):
        super()._reset_state()
        # subclass attrs may not exist yet on the first (super().__init__) call
        if hasattr(self, "_plateaus"):
            self._plateaus.reset()
            self._prev_y0_strip = None
            self._stale_prev_roi = None
            self._stale_prev_key = None
            self._stale_quiet_sads.clear()
            self._last_read_ts = None
            self._last_roi_sad = -1.0
            self._stale_run = 0
            self._luma_pending = 0
            self._luma_lock_fill = 0.0
            self._chroma_dead_run = 0
            self._cur_ts = None
            self._tip_row = None
            self._tip_top = None
            self._tip_persist = 0
            self._native_y = None

    def reset_tracking(self):
        """Retire pixel-coordinate acquisition evidence on source/profile changes.

        Ordinary lock/shot resets deliberately keep the same scene's hint and
        rejected rail registry; a new source or profile must not inherit them.
        """
        super().reset_tracking()
        self._acq_hint = None
        self._decor_registry = []
        self._registry_frame = 0

    # ------------------------------------------------------------------ #
    #  quality layer: q_session -> discrete tunables + the luma gate
    # ------------------------------------------------------------------ #
    def set_stream_profile(self, width: int, height: int, fps: float = 60.0,
                           bitrate_kbps: Optional[float] = None) -> None:
        """Static rung prior (the orchestrator knows the configured resolution/bitrate)."""
        self._qe.set_stream_profile(width, height, fps, bitrate_kbps)

    def _apply_quality(self, q: float) -> None:
        """Re-shadow the behavioural tunables from the adaptation law. Only in auto mode
        and only when q_session actually moved (band changes are already hysteretic)."""
        if self._params_fixed:
            return
        if self._q_applied is not None and abs(q - self._q_applied) < 1e-6:
            return
        self._q_applied = q
        p = ReaderParams.for_quality(q)
        self.params = p
        self.CONF_RATE, self.CONF_RATE_ARMED = p.conf_rate, p.conf_rate_armed
        self.NCC_LOCK = p.ncc_lock
        self.G_AREA_MIN = p.g_area_min
        self._G = ((self._G[0][0], p.green_s_floor, p.green_v_floor), self._G[1])
        if p.vel_win != self.VEL_WIN:
            self.VEL_WIN = p.vel_win
            old = list(self._vel_hist)
            self._vel_hist = deque(old[-p.vel_win:], maxlen=p.vel_win)
        if self.W and self.H:
            self._recompute_scale()          # G_AREA_MIN feeds the scaled _g_area_min

    # ------------------------------------------------------------------ #
    #  K1 luma acquisition (rails + evidence bits + décor registry + hint)
    # ------------------------------------------------------------------ #
    def set_acquire_hint(self, box, conf: float = 0.0, ts: Optional[float] = None):
        """Locator (YOLO-on-luma) hint: shrinks the K1 scan region and relaxes rail
        persistence one notch. A hint can NEVER create a lock alone -- the rail/evidence
        gates must still pass (same relax-never-suppress contract as the shot gate)."""
        self._acq_hint = (tuple(int(v) for v in box), float(conf),
                          float(ts) if ts is not None else _time.perf_counter())

    # ------------------------------------------------------------------ #
    #  native Y pass-through (Tier-1 #5): the decoder's own luma, no BGR round trip
    # ------------------------------------------------------------------ #
    def set_native_y(self, y) -> None:
        """Decoder Y-plane for the NEXT read() (one-shot; cleared when that read ends).
        Used by every LUMA consumer in place of BGR2GRAY when its shape matches the
        frame; the chroma paths never touch it, so the collapse row is unaffected."""
        self._native_y = y if (y is not None and getattr(y, "ndim", 0) == 2) else None

    def _y_view(self, frame, x0: int, y0: int, x1: int, y1: int):
        """Grayscale view of frame[y0:y1, x0:x1]: the native decoder Y plane when one was
        passed for this frame (zero-copy slice), else cv2 BGR2GRAY of the crop."""
        ny = self._native_y
        if (ny is not None and ny.shape[0] == frame.shape[0]
                and ny.shape[1] == frame.shape[1]):
            return ny[y0:y1, x0:x1]
        return cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)

    def _band_y(self, frame, region=None):
        x0, y0, x1, y1 = region if region is not None else self._band
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.W, int(x1)), min(self.H, int(y1))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None, 0, 0
        return self._y_view(frame, x0, y0, x1, y1), x0, y0

    def _y_verify(self, y_band, cand_box) -> Optional[bool]:
        """MeterReaderY.verify() on a rail candidate's Y crop (bug #2 wiring). Returns
        None when the student is not wired (flag off / model absent), True/False from
        present_p otherwise. CONFIRMER ONLY: the caller may use True to shrink the scan
        (acquire-hint contract) but must never let any value create or veto a lock."""
        if self._reader_y is None:
            return None
        try:
            x, ry, w, h = cand_box
            y0 = max(0, ry - 8)
            y1 = min(y_band.shape[0], ry + h + 4)
            x0 = max(0, x - 2)
            x1 = min(y_band.shape[1], x + w + 3)
            if x1 - x0 < 4 or y1 - y0 < 10:
                return None
            return bool(self._reader_y.verify(y_band[y0:y1, x0:x1]))
        except Exception:
            return None

    def _registry_match(self, x_frame: float, spacing: int, now: float) -> str:
        """'' = unknown pair; 'soft' = was standing pre-arm (needs full structure);
        'hard' = E4-refuted this shot (provably non-rising -- refuse for its TTL)."""
        self._decor_registry = [e for e in self._decor_registry
                                if now - e["ts"] <= (2.0 if e.get("hard") else 5.0)]
        verdict = ""
        for e in self._decor_registry:
            if abs(e["x"] - x_frame) <= 6 and abs(e["spacing"] - spacing) <= 3:
                if e.get("hard"):
                    return "hard"
                verdict = "soft"
        return verdict

    def _update_registry(self, frame, now: float):
        """While UNARMED and unlocked, remember stable rail pairs (~2 Hz): the meter APPEARS
        at shot start, so anything already standing there is décor. An armed acquire that
        matches a registry entry needs the full structural evidence (sliver AND fill step)."""
        y, bx, by = self._band_y(frame)
        if y is None:
            return
        for _, (x, ry, w, h) in rail_pair_scan(
                y, spacing_lo=max(4, self._w_min - 2), spacing_hi=self._w_max + 4,
                min_extent=self._h_acq, max_extent=self._h_max)[:6]:
            xf = bx + x + w * 0.5
            for e in self._decor_registry:
                if abs(e["x"] - xf) <= 6 and abs(e["spacing"] - w) <= 3:
                    e["ts"] = now
                    break
            else:
                self._decor_registry.append({"x": xf, "spacing": int(w), "ts": now})

    def _candidate_evidence(self, y_band, cand_box):
        """E2 (tip sliver) / E3 (single dark->mid fill step) on a rail-scan candidate,
        computed from its interior Y profile in band coordinates."""
        x, ry, w, h = cand_box
        y0 = max(0, ry - 6)
        y1 = min(y_band.shape[0], ry + h + 4)
        il, ir = x + 2, x + w - 1
        if ir - il < 2 or y1 - y0 < 10:
            return False, False
        prof = y_band[y0:y1, il:ir].mean(axis=1).astype(np.float64)
        sliver = find_tip_sliver(prof, self._plateaus)
        fit = fit_fill_boundary(prof, plateaus=self._plateaus)
        e3 = fit is not None and 45.0 <= fit.l_fill <= 140.0
        return bool(sliver.size >= 2), e3

    def _unarmed_acquire_ok(self) -> bool:
        """Quality gate for the UNARMED luma acquire (design 2a): at the low rungs the
        chroma acquire is blind (red desaturates first under motion) so the early rise is
        lost before the shot gate arms. Enable the unarmed rail acquire ONLY when
        q_session sits in the degraded bands AND chroma has been dead for a sustained
        run -- a pristine source (q_session=1) can never satisfy the first clause, so
        the collapse row keeps its armed-only behaviour byte-identically."""
        return (self._qe.q_session <= self.UNARMED_ACQ_Q_MAX
                and self._chroma_dead_run >= self.UNARMED_CHROMA_DEAD_MIN)

    def _luma_acquire(self, frame):
        """K1 luma acquisition -- rails + evidence, then the E4 rise confirm (read()
        zeroes the lock if the fill never rises). ARMED: rails + (sliver OR fill-step),
        registry pairs need full structure. UNARMED (quality-gated, design 2a): permitted
        only via _unarmed_acquire_ok() and then at the STRICTEST bar -- e2 AND e3 AND
        h >= _h_acq regardless of registry verdict -- because on real pristine frames
        even rails+sliver+step occurs in décor (measured: framedump parity broke on it);
        E4 makes the residual risk non-catastrophic (a non-rising lock is zeroed and
        hard-registered within 4 frames)."""
        armed = self._shot_armed
        if not self._luma_tracking:
            return None
        if not armed and not self._unarmed_acquire_ok():
            return None
        now = self._last_read_ts if self._last_read_ts is not None else _time.perf_counter()
        region = None
        persist = 0.6
        if self._acq_hint is not None:
            hbox, hconf, hts = self._acq_hint
            if now - hts <= 0.5:
                hx, hy, hw, hh = hbox
                region = (hx - 2 * hw, hy - hh, hx + 3 * hw, hy + 2 * hh)
                persist = 0.5
            else:
                self._acq_hint = None
        y, bx, by = self._band_y(frame, region)
        if y is None:
            return None
        min_ext = self._h_acq_armed if armed else self._h_acq
        cands = rail_pair_scan(y, spacing_lo=max(4, self._w_min - 2),
                               spacing_hi=self._w_max + 4,
                               min_extent=min_ext, max_extent=self._h_max,
                               persist=persist,
                               prior_x=(self.box[0] + self.box[2] * 0.5 - bx)
                               if self.box else None)
        y_hinted = False
        for _, cand in cands[:4]:
            x, ry, w, h = cand
            verdict = self._registry_match(bx + x + w * 0.5, w, now)
            if verdict == "hard":
                continue                           # E4-refuted this shot: not the meter
            e2, e3 = self._candidate_evidence(y, cand)
            if not armed:
                # UNARMED (quality-gated): full structure at the COLD height, always.
                ok = e2 and e3 and h >= self._h_acq
            elif verdict == "":
                ok = e2 or e3
            else:                                  # armed but over standing pre-arm décor
                ok = e2 and e3 and h >= self._h_acq
            if not ok:
                # bug #2 wiring: MeterReaderY.verify() as a CONFIRMER only. Under total
                # chroma collapse a rail pair whose classical structure is blurred away
                # can still be CONFIRMED by the Y-student -> feed the acquire hint so the
                # next scans SHRINK onto it (relax-never-suppress: the rail/evidence
                # gates above must still pass to lock -- verify can never create one,
                # and a refusal changes nothing). At most one CNN call per frame.
                if not y_hinted and self._reader_y is not None:
                    y_hinted = True                # bound the cost: top candidate only
                    if self._y_verify(y, cand) is True:
                        self.set_acquire_hint((bx + x, by + ry, w, h), 0.5, ts=now)
                        self._luma_diag = dict(self._luma_diag or {})
                        self._luma_diag["y_verify_hint"] = 1
                continue
            # E4 arm: the lock must show a rising fill within 4 frames or read() zeroes it.
            # Baseline fill is captured on the FIRST read of the new lock (sentinel -1).
            # The velocity history is CLEARED: it holds pre-lock zeros, so the fill jump
            # at lock reads as a huge LS slope -> rise_state 'rising' -> E4 rubber-stamps
            # a STATIC pair (the exact décor class E4 exists to refute). Post-lock samples
            # rebuild a genuine rise within 2-3 frames.
            self._vel_hist.clear()
            self._luma_pending = 4
            self._luma_lock_fill = -1.0
            diag = {"acquire_path": "luma_rails", "e2": int(e2), "e3": int(e3),
                    "acq_armed": int(armed)}
            yv = self._y_verify(y, cand)
            if yv is not None:
                diag["y_verify"] = int(yv)         # diagnostic only, NEVER a gate
            self._luma_diag = diag
            return (bx + x, by + ry, w, h)
        return None

    # base read() calls this when the chroma scan found nothing (both armed and cold)
    def _acquire_structure(self, frame, **kw):
        col = super()._acquire_structure(frame, **kw)
        if col is not None:
            return col
        # The LUMA fallback is the nominal-band pop-in's partner: it has its own band, its
        # own rails/sliver structure proof and no notion of the caller's region or stub
        # floor. Only the plain pop-in call may reach it. Every other base call site (the
        # court-wide full-frame fallback and the N7 early-stub band pass) passes kwargs and
        # must fall through unchanged, exactly as it did before those callers existed.
        if kw:
            return None
        return self._luma_acquire(frame)

    # -- luma-aware décor guard: base _relocate's NCC branch calls this polymorphically --
    def _has_meter_colour(self, frame, box):
        if super()._has_meter_colour(frame, box):
            return True
        if not self._luma_tracking:
            return False
        bx, by, bw, bh = [int(v) for v in box]
        y0 = max(0, by - 8)
        y1 = min(self.H, by + bh + 4)
        x0, x1 = max(0, bx), min(self.W, bx + bw)
        if x1 - x0 < 4 or y1 - y0 < 10:
            return False
        sub = self._y_view(frame, x0, y0, x1, y1).astype(np.float64)
        m = 2 if sub.shape[1] >= 8 else 1
        prof = sub[:, m:-m].mean(axis=1) if sub.shape[1] - 2 * m >= 2 else sub.mean(axis=1)
        if find_tip_sliver(prof, self._plateaus).size >= 2:      # E2
            return True
        fit = fit_fill_boundary(prof, plateaus=self._plateaus)   # E3
        return fit is not None and 45.0 <= fit.l_fill <= 140.0

    # -- relocate: chroma steps first (super), then luma rails in the SAME window --
    def _relocate(self, frame):
        col, ev = super()._relocate(frame)
        if col is not None or self.box is None or not self._luma_tracking:
            return col, ev
        win = self._relocate_window()
        y, bx, by = self._band_y(frame, win)
        if y is None:
            return None, None
        cands = rail_pair_scan(y, spacing_lo=max(4, self._w_min - 2),
                               spacing_hi=self._w_max + 4,
                               min_extent=self._h_hold, max_extent=self._h_max,
                               prior_x=self.box[0] + self.box[2] * 0.5 - bx)
        if cands:
            _, (x, ry, w, h) = cands[0]
            return (bx + x, by + ry, w, h), "rails"
        return None, None

    # ------------------------------------------------------------------ #
    #  stale gate (encoder-skip duplicate frames are MISSING DATA)
    # ------------------------------------------------------------------ #
    def _stale_roi(self):
        """The region whose pixel-freshness proves the frame carries new meter evidence:
        the last trackbox when we have one (covers the fillable track), else the lock box."""
        box = self.last_tbox if (self.last_tbox and self.last_tbox[2] > 0) else self.box
        if not box:
            return None
        x, y, w, h = [int(v) for v in box]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.W, x + w), min(self.H, y + h)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return x0, y0, x1, y1

    def _check_stale(self, frame, ts: float) -> bool:
        """True iff this frame's meter ROI is a pixel-frozen repeat while the filter believes
        the fill is moving. A genuinely static meter (pre-shot hold / plateau) is NOT stale --
        it is a valid zero-motion observation. Updates the stored ROI/noise floor either way."""
        if self.box is None or not self.W:
            return False
        key = self._stale_roi()
        if key is None:
            return False
        x0, y0, x1, y1 = key
        roi = self._y_view(frame, x0, y0, x1, y1)
        sad = roi_sad(roi, self._stale_prev_roi) if key == self._stale_prev_key else -1.0
        self._stale_prev_roi = roi
        self._stale_prev_key = key
        self._last_roi_sad = sad
        if sad < 0.0:
            return False                        # box moved / first frame: no comparison
        # duplicate timestamp = duplicate frame, unconditionally stale while locked+moving
        dup_ts = (self._last_read_ts is not None and ts <= self._last_read_ts)
        if abs(self._velocity) < self.STALE_QUIET_VEL and not dup_ts:
            self._stale_quiet_sads.append(sad)  # quiet frame -> calibrates the noise floor
            return False
        if abs(self._velocity) < self.STALE_VEL_MIN and not dup_ts:
            return False                        # not confidently moving -> don't gate
        floor = (float(np.percentile(np.asarray(self._stale_quiet_sads), 20))
                 if len(self._stale_quiet_sads) >= 8 else 0.0)
        thr = max(self.STALE_SAD_FLOOR, self.STALE_FLOOR_MULT * floor)
        return bool(sad < thr)

    # ------------------------------------------------------------------ #
    #  READ: stale gate wraps the whole cascade; luma diag flows to last_debug
    # ------------------------------------------------------------------ #
    def read(self, frame, ts: Optional[float] = None) -> dict:
        if ts is None:
            ts = _time.perf_counter()
        # Direct read() callers must retire old geometry BEFORE the stale fast
        # return. detect() already prepares it; this same-size call is idempotent.
        # Reset precedes _cur_ts because _reset_state clears the de-lag timestamp.
        self._prepare_frame_geometry(frame)
        self._cur_ts = ts                      # de-lag dt inside _read_fill (state only)
        locked = self.conf >= self.CONF_MIN and self.box is not None
        stale_now = locked and self._check_stale(frame, ts)
        if stale_now and self._stale_run < self.STALE_RUN_MAX:
            # MISSING DATA: hold fill/box/confidence/velocity; nothing decays, nothing advances.
            # rejection 'stale_frame' keeps the feed gate closed (same class as 'meter_memory').
            self._stale_run += 1
            self._qe.update(ts, stale=True)
            self.last_debug = {"stage": "stale", "conf": round(self.conf, 3),
                               "stale": 1, "roi_sad": round(self._last_roi_sad, 2),
                               "stale_run": self._stale_run,
                               "q_frame": round(self._qe.q_frame, 3),
                               "q_session": round(self._qe.q_session, 3)}
            self._native_y = None              # one-shot: never reuse across frames
            return {"detected": True, "meter_present": True, "fill": self.last_fill,
                    "fill_coarse": self.last_coarse, "bbox": list(self.last_tbox),
                    "stage": "stale", "confidence": round(self.conf, 3),
                    "velocity_pct_s": self._velocity, "rejection_reason": "stale_frame",
                    "rise_state": self._rise_state(self.last_fill)}
        # reset the run ONLY on genuinely non-stale frames: past the cap, still-frozen
        # frames must keep falling through (decay every frame -> honest drop), not buy
        # another 12-frame hold each time one decay tick lands (the 1-second echo).
        if not stale_now:
            self._stale_run = 0
        self._luma_diag = {}
        # pre-arm décor registry: while unarmed + unlocked, remember standing rail pairs
        # (~2 Hz at 60fps) so an armed acquire can tell "appeared with the shot" from
        # "was always there".
        self._registry_frame += 1
        if not locked and not self._shot_armed and self._registry_frame % 30 == 0:
            try:
                self._update_registry(frame, ts)
            except Exception:
                pass
        out = super().read(frame, ts)
        self._last_read_ts = ts
        # E4 confirm: a lock created from LUMA structure must show a rising fill within 4
        # frames or it is a standing décor pair -- zero it (chroma locks are exempt: the
        # colour itself is the evidence).
        if self._luma_pending > 0:
            if self.box is None:
                self._luma_pending = 0
            elif self._luma_lock_fill < 0.0:
                # first read of the new lock = the rise baseline (not a confirm frame)
                self._luma_lock_fill = float(out.get("fill", 0.0) or 0.0)
            elif (out.get("rise_state") == "rising"
                    or out.get("fill", 0.0) > self._luma_lock_fill + 3.0):
                self._luma_pending = 0                     # confirmed: it rose
            else:
                self._luma_pending -= 1
                if self._luma_pending == 0:
                    # provably non-rising structure while armed: HARD-register the pair so
                    # it cannot immediately re-acquire, then zero the lock.
                    bx0, by0, bw0, bh0 = self.box
                    self._decor_registry.append({"x": bx0 + bw0 * 0.5, "spacing": int(bw0),
                                                 "ts": ts, "hard": True})
                    self.conf = 0.0
                    self.box = None
                    self.tmpl = None
                    self._consec = 0
                    self.last_debug = dict(self.last_debug or {})
                    self.last_debug["luma_e4_reject"] = 1
        if self._luma_diag:
            self.last_debug.update(self._luma_diag)
        self.last_debug.setdefault("stale", 0)
        if self._last_roi_sad >= 0.0:
            self.last_debug.setdefault("roi_sad", round(self._last_roi_sad, 2))
        # ---- quality layer: feed per-frame evidence, drive R / params / the luma gate ----
        diag = self._luma_diag or {}
        fresh = bool(out.get("detected")) and out.get("rejection_reason") in ("", "green_not_found")
        chroma_alive = fresh and diag.get("fill_path", "chroma") == "chroma"
        if chroma_alive:
            self._last_chroma_ts = ts
            self._chroma_dead_run = 0
        else:
            # sustained chroma silence is one of the two UNARMED-acquire gates (2a);
            # counted on real (non-stale) reads only -- stale frames return above.
            self._chroma_dead_run = min(self._chroma_dead_run + 1, 1 << 20)
        q_frame, q_session = self._qe.update(
            ts, fit_q=diag.get("q_pix"), chroma_alive=chroma_alive)
        if self._luma_gate_auto:
            # pristine band collapses the luma branches -- but FAIL OPEN: only while
            # chroma is actually delivering (a stream that degrades after going quiet
            # must not stay luma-blind on stale quality history).
            chroma_recent = (self._last_chroma_ts is not None
                             and ts - self._last_chroma_ts <= 1.0)
            self._luma_tracking = not (self._qe.pristine and chroma_recent)
            self._apply_quality(q_session)
        self.last_debug["q_frame"] = round(q_frame, 3)
        self.last_debug["q_session"] = round(q_session, 3)
        self.last_debug["R_used"] = round(QualityEstimator.r_for(q_frame), 4)
        self._native_y = None                  # one-shot: never reuse across frames
        return out

    # ------------------------------------------------------------------ #
    #  STAGE 3 (compressed): chroma-first fill read with the K2 luma fallback
    # ------------------------------------------------------------------ #
    def _read_fill(self, frame, col):
        strip, x0, top_search = self._fill_strip(frame, col)
        ph, pw = strip.shape[:2]
        if ph < 8 or pw < 3:
            return super()._read_fill(frame, col)
        red_row = self._redmask(strip).mean(axis=1) / 255.0
        # COLLAPSE RULE: if the base chroma read would find ANY red rows (>= its own
        # red_row_thr), delegate byte-identically -- the luma path may only replace a read
        # the chroma path would MISS outright, never re-interpret a weak-but-present one.
        red_rows = np.flatnonzero(red_row >= self.params.red_row_thr)
        if not self._luma_tracking or red_rows.size > 0:
            self._luma_diag = {"fill_path": "chroma"}
            # QUALITY SENSING (outputs untouched): chroma succeeding says nothing about
            # encode blur -- a flat "alive" score saturated q_session to the pristine band
            # on a 4 Mbps stream and gated the luma branches off (rung-harness finding).
            # Every 8th frame, measure the K2 edge statistic at the chroma boundary so
            # q_p reflects the ACTUAL sharpness of the edge being timed.
            self._chroma_q_tick = (self._chroma_q_tick + 1) % 8
            if self._chroma_q_tick == 0 and red_rows.size > 0:
                try:
                    ph_, pw_ = strip.shape[:2]
                    m_ = 4 if pw_ >= 12 else max(1, pw_ // 4)
                    y_ = self._y_view(frame, x0, top_search,
                                      x0 + pw_, top_search + ph_).astype(np.float64)
                    prof_ = (y_[:, m_:pw_ - m_].mean(axis=1)
                             if pw_ - 2 * m_ >= 2 else y_.mean(axis=1))
                    fit_ = fit_fill_boundary(prof_, prev_y0=float(red_rows[0]),
                                             plateaus=self._plateaus)
                    if fit_ is not None:
                        self._luma_diag["q_pix"] = round(fit_.q, 3)
                        self._luma_diag["sig_width"] = round(fit_.s, 2)
                except Exception:
                    pass
            return super()._read_fill(frame, col)
        # ---- K2: luma fill read ----
        y = self._y_view(frame, x0, top_search, x0 + pw, top_search + ph).astype(np.float64)
        m = 4 if pw >= 12 else max(1, pw // 4)   # exclude the bevel rails from the row mean
        prof = y[:, m:pw - m].mean(axis=1) if pw - 2 * m >= 2 else y.mean(axis=1)
        # tip sliver = the luma green-cap: same fillable-height semantics as the chroma path.
        # TEMPORAL TIP-LOCK (design 1c): the tip row is a per-shot invariant -- once the
        # sliver has held a stable row for TIP_LOCK_MIN frames, refine it in a small window
        # around the locked span (warm start, like prev_y0) instead of scanning the whole
        # profile cold; a windowed miss falls straight back to the cold search.
        sliver = np.empty(0, dtype=np.int64)
        if (self._tip_row is not None and self._tip_top is not None
                and self._tip_persist >= self.TIP_LOCK_MIN):
            w0 = int(max(0, self._tip_top - self.TIP_LOCK_WIN))
            w1 = int(min(ph, self._tip_row + self.TIP_LOCK_WIN + 1))
            if w1 - w0 >= 4:
                sliver = find_tip_sliver(prof[w0:w1], self._plateaus)
                if sliver.size:
                    sliver = sliver + w0
        if sliver.size == 0:
            sliver = find_tip_sliver(prof, self._plateaus)
        # sliver-row persistence (feeds both the tip-lock above and the green anchor gate)
        if sliver.size:
            _bot, _top = int(sliver[-1]), int(sliver[0])
            if self._tip_row is not None and abs(_bot - self._tip_row) <= self.TIP_ROW_TOL:
                self._tip_persist += 1
            else:
                self._tip_persist = 1
            self._tip_row, self._tip_top = _bot, _top
        else:
            self._tip_persist = 0
        green_px = int(sliver.size)
        green_band_px = int(sliver[-1] - sliver[0] + 1) if sliver.size else 0
        if sliver.size:
            green_bottom = int(sliver[-1])
            fillable_h = float(ph - green_bottom)
            self._fillable_hist.append(fillable_h)
            if len(self._fillable_hist) > 60:
                self._fillable_hist = self._fillable_hist[-60:]
            self._plateaus.update_tip(float(prof[sliver].mean()))
        elif self._fillable_hist:
            fillable_h = float(np.median(self._fillable_hist))
            green_bottom = max(0, ph - int(fillable_h))
        else:
            green_bottom, fillable_h = 0, float(ph)
        fillable_h = float(min(max(fillable_h, 6.0), 185.0))       # same clamps as chroma path
        green_bottom = max(0, ph - int(fillable_h))
        if fillable_h < 6:
            return 0.0, 0.0, (x0, top_search, pw, ph), -1, None
        fit = fit_fill_boundary(prof[green_bottom:], prev_y0=self._prev_y0_strip,
                                plateaus=self._plateaus)
        # the fill plateau must look like RED-in-Y (mid-luma ~76-105 nominal): a capped meter
        # (interior gone bright green ~150) or a washed-out strip must NOT read as fill --
        # the base behavior there (red-miss -> green-only hold upstream) is correct.
        if fit is not None and not (45.0 <= fit.l_fill <= 140.0):
            fit = None
        if fit is None:
            self._prev_y0_strip = None
            self._luma_diag = {"fill_path": "luma_reject"}
            if self.last_tbox and self.last_tbox[2] > 0 and self.last_fill > 0.5:
                # chroma dead AND no credible luma step (cap flash / washout / blur) while
                # the box is still evidence-anchored: HOLD the last good read -- emitting a
                # 0-fill glitch mid-shot is exactly the green-only-hold lesson, luma edition.
                return self.last_coarse, self.last_fill, tuple(self.last_tbox), -1, None
            return 0.0, 0.0, (x0, top_search, pw, ph), -1, None
        if not fit.frozen:
            self._plateaus.update(fit.l_dark, fit.l_fill)
        # PER-COLUMN ROBUST BOUNDARY (design 1b): the full-width row mean lets ONE ringing
        # macroblock column bias y0; refit on 3 vertical-third column sub-means and take
        # the median y0. Falls back to the full-mean fit unless ALL THREE thirds see a
        # credible red-in-Y step (a partial agreement is how an artifact wins a vote).
        y0_meas = fit.y0
        interior = y[green_bottom:, m:pw - m] if pw - 2 * m >= 6 else None
        if interior is not None and interior.shape[0] >= 8:
            sub_y0 = []
            for third in np.array_split(interior, 3, axis=1):
                f3 = fit_fill_boundary(third.mean(axis=1), prev_y0=fit.y0,
                                       plateaus=self._plateaus)
                if f3 is not None and 45.0 <= f3.l_fill <= 140.0:
                    sub_y0.append(f3.y0)
            if len(sub_y0) == 3:
                y0_meas = float(np.median(sub_y0))
        self._prev_y0_strip = y0_meas             # warm start = the MEASURED boundary
        # VELOCITY-AWARE EDGE DE-LAG (design 1a): on a fast rise the logistic midpoint
        # lags the instantaneous leading edge (fill p90 tail 14-35pp). Advance y0 along
        # the motion by k*min(s, |vel|*dt*px/pct). Gated on LOW q_frame AND a confidently
        # moving velocity, so the pristine/slow read is byte-identical.
        y0_use = y0_meas
        vel = self._velocity
        n_fit = prof[green_bottom:].size
        if (abs(vel) > self.STALE_VEL_MIN and self._qe.q_frame < self.DELAG_Q_MAX
                and self._cur_ts is not None and self._last_read_ts is not None):
            dt = min(0.05, max(0.0, self._cur_ts - self._last_read_ts))
            delag = self.DELAG_K * min(fit.s, abs(vel) * dt * fillable_h / 100.0)
            if delag > 0.0:
                y0_use = float(np.clip(y0_meas - np.sign(vel) * delag, 0.0,
                                       max(0.0, n_fit - 1.0)))
        y0_abs = green_bottom + y0_use                              # strip-row coordinates
        clamp = lambda v: max(0.0, min(100.0, v))
        coarse = clamp((ph - round(y0_abs)) / fillable_h * 100.0)
        subpix = clamp((ph - y0_abs) / fillable_h * 100.0)
        trackbox = (x0, top_search + green_bottom, pw, int(fillable_h))
        # Match the primary reader's served-bbox contract without changing this luma
        # boundary or denominator: the display/sidecar box encloses a connected wide cap
        # and silver apex, while timing continues to use the narrow rail strip above.
        trackbox = self._enclose_meter_bbox(
            frame, col, trackbox,
            top_search + int(sliver[0]) if sliver.size else None,
            top_search + int(sliver[-1]) if sliver.size else None)
        top_row = top_search + int(round(y0_abs))
        green = None
        if green_px >= 2 and green_band_px > 0:
            # GREEN/TIP ANCHOR STABILIZER (ladder eval): the raw luma sliver over-fires on
            # low-bitrate blocking (195-753 anchors vs 200 clean) and under-fires on
            # high-bitrate softening. Emit the green release window ONLY when the chroma
            # green mask corroborates the sliver rows OR the sliver has held a stable row
            # for TIP_PERSIST_MIN consecutive reads. The sliver still anchors fillable_h
            # above either way -- this gates the RELEASE TARGET, not the geometry; with no
            # green emitted downstream times the tip (100%), the honest fallback.
            corroborated = False
            try:
                cap_bgr = strip[int(sliver[0]):int(sliver[-1]) + 1]
                if cap_bgr.size:
                    gmask = self._greenmask(cv2.cvtColor(cap_bgr, cv2.COLOR_BGR2HSV))
                    corroborated = int(cv2.countNonZero(gmask)) >= 4
            except Exception:
                corroborated = False
            if corroborated or self._tip_persist >= self.TIP_PERSIST_MIN:
                band = min(12.0, max(2.0, green_band_px / fillable_h * 100.0))
                g_end = 100.0
                g_start = max(80.0, g_end - band)
                green = (round(g_start, 2), round(g_end, 2),
                         round((g_start + g_end) * 0.5, 2), round(g_end - g_start, 2),
                         round(min(1.0, green_px / 6.0), 3), green_px)
        self._luma_diag = {"fill_path": "luma", "sig_width": round(fit.s, 2),
                           "edge_curv": round(fit.step / max(fit.resid_rms, 1e-3), 4),
                           "q_pix": round(fit.q, 3),
                           "tip_persist": self._tip_persist}
        if y0_use != y0_meas:
            self._luma_diag["delag_px"] = round(y0_meas - y0_use, 2)
        return round(coarse, 2), round(subpix, 2), trackbox, int(top_row), green
