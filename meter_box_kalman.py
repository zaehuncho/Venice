"""Causal constant-velocity Kalman tracker over the served meter GEOMETRY — the "no drift, no shrink,
no flicker" lever.

The served meter bbox (and the fill denominator behind it) was raw per-frame contour geometry: width
swings 7<->36px on slivers/blur (spuriously wiping the fill-denominator memory -> mistimed releases),
height was a max-hold that could only GROW, and position jitter was handled by DROPPING frames
(bbox_unstable -> fed=0) instead of smoothing them. This filter tracks the meter's STABLE anchor —
centre-x and the NOTCH (bottom edge; the fill-rect top moves with fill, the notch doesn't) — plus a
slow-adapting width and full-track height, so the emitted geometry stays glued to the meter through
pans/zoom and a one-frame outlier can neither yank nor collapse it.

Four independent 2-state [p, v] constant-velocity channels (cx, cb, w, h_full): position tracks a fast
pan, size adapts slowly (zoom is gradual). Per-channel outlier gates: a position jump beyond a
VELOCITY-AWARE gate (tight ~40px when the track is near-static — where post-release green VFX erupts —
opening with tracked speed up to the caller's acquisition gate) REJECTS the sample; three mutually
CONSISTENT far strikes (co-located, or one constant-velocity line) re-seed — it's a real new meter —
while scattered VFX strikes never adopt the distractor. A sliver width is rejected for the size channel
while position still updates. predict() is PURE (no state mutation), so a miss can be bridged without
corrupting the next real update's dt.

Pure numpy, causal, never raises. Flag-gated by try_load (ORION_METER_TRACK=1); when off this module
is never imported and the detector's classical path is byte-identical.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import numpy as np


class _Cv1D:
    """2-state [p, v] constant-velocity scalar Kalman (position-only measurement)."""

    def __init__(self, q: float, r: float) -> None:
        self._q = float(q)      # white-noise-acceleration spectral density (px^2/s^3)
        self._r = float(r)      # measurement variance (px^2)
        self._x: Optional[np.ndarray] = None
        self._P: Optional[np.ndarray] = None
        self._n = 0

    def seed(self, p: float, v: float = 0.0) -> None:
        self._x = np.array([float(p), float(v)], dtype=float)
        # generous velocity uncertainty so the first real motion is trusted quickly
        self._P = np.diag([self._r, 1.0e6])
        self._n = 1

    def advance_update(self, z: float, dt: float) -> None:
        if self._x is None:
            self.seed(z)
            return
        F = np.array([[1.0, dt], [0.0, 1.0]], dtype=float)
        dt2, dt3 = dt * dt, dt ** 3
        Q = self._q * np.array([[dt3 / 3.0, dt2 / 2.0], [dt2 / 2.0, dt]], dtype=float)
        x = F @ self._x
        P = F @ self._P @ F.T + Q
        y = float(z) - x[0]
        S = P[0, 0] + self._r
        if S <= 0.0:
            self._x, self._P = x, P
            return
        K = P[:, 0] / S
        self._x = x + K * y
        self._P = P - np.outer(K, P[0, :])
        self._n += 1

    def extrapolate(self, dt: float) -> float:
        return float(self._x[0] + self._x[1] * dt) if self._x is not None else 0.0

    @property
    def p(self) -> float:
        return float(self._x[0]) if self._x is not None else 0.0

    @property
    def v(self) -> float:
        return float(self._x[1]) if self._x is not None else 0.0

    @property
    def n(self) -> int:
        return self._n


class MeterBoxKalman:
    """update_pose(cx, cb, w, t_s, jump_gate_px) per detection; update_height(h_full, t_s) on a REAL
    green sighting only (the authoritative full-height measurement — anything else is circular).
    predict_pose(t_s) bridges a miss without mutating state."""

    def __init__(self, q_pos: float = 6.0e4, r_pos: float = 4.0,
                 q_size: float = 4.0e2, r_size: float = 6.0,
                 q_h: float = 8.0e2, r_h: float = 25.0,
                 max_dt_s: float = 0.6, coast_s: float = 0.7) -> None:
        self._cx = _Cv1D(q_pos, r_pos)
        self._cb = _Cv1D(q_pos, r_pos)
        self._w = _Cv1D(q_size, r_size)
        self._h = _Cv1D(q_h, r_h)
        self._max_dt = float(max_dt_s)
        self._coast = float(coast_s)
        self._t: Optional[float] = None      # pose-channel measurement clock
        self._t_h: Optional[float] = None    # height-channel clock (green sightings are sparse)
        self._outliers = 0
        # recent outlier positions: a re-seed needs 3 MUTUALLY CONSISTENT far measurements (a real
        # new meter reports the same place thrice); scattered outliers (VFX chaos bouncing between
        # a distractor and the meter) must never adopt the distractor.
        self._outlier_pts: list = []
        self._reseed_undo = None             # pre-strike-reseed state for rollback_reseed()
        self.last_event = ""                 # "seed" | "accept" | "reject" | "reseed" | ""

    # -- lifecycle -------------------------------------------------------------------
    def reset(self) -> None:
        for ch in (self._cx, self._cb, self._w, self._h):
            ch._x = None
            ch._P = None
            ch._n = 0
        self._t = None
        self._t_h = None
        self._outliers = 0
        self._outlier_pts = []
        self._reseed_undo = None
        self.last_event = ""

    def rollback_reseed(self) -> bool:
        """Undo the most recent STRIKE re-seed (caller pixel-vetoed the adopted spot: no coherent
        bar there -> it was a distractor, keep tracking the old meter). Strikes stay cleared so a
        persistent distractor re-earns its 3 strikes (and re-veto) rather than looping per frame.
        Returns True if a rollback was applied."""
        if self._reseed_undo is None:
            return False
        (xcx, pcx_, ncx), (xcb, pcb_, ncb), (xw, pw_, nw), t = self._reseed_undo
        self._cx._x, self._cx._P, self._cx._n = xcx, pcx_, ncx
        self._cb._x, self._cb._P, self._cb._n = xcb, pcb_, ncb
        self._w._x, self._w._P, self._w._n = xw, pw_, nw
        self._t = t
        self._reseed_undo = None
        self._outliers = 0
        self._outlier_pts = []
        self.last_event = "reject"
        return True

    def _seed_pose(self, cx: float, cb: float, w: float, t: float,
                   vcx: float = 0.0, vcb: float = 0.0) -> None:
        self._cx.seed(cx, vcx)
        self._cb.seed(cb, vcb)
        self._w.seed(w)
        self._t = t
        self._outliers = 0
        self._outlier_pts = []

    # -- pose (cx, cb, w) ------------------------------------------------------------
    def update_pose(self, cx: float, cb: float, w: float, t_s: float,
                    jump_gate_px: float, confirm_feed: bool = False) -> str:
        """Returns "accept" | "reject" | "seed"/"reseed". On "reject" the caller should serve
        predict_pose() — the filter believes this frame is a VFX/distractor outlier and the meter is
        still where the track says (post-release green flames yanked the raw box off the bar for
        8-12 frames live). A GENUINE relocation is not starved: three mutually-consistent strikes
        (same place, or one constant-velocity line) re-seed the track onto it, velocity included.

        confirm_feed=True marks a measurement the caller obtained by re-inspecting the PREDICTED
        spot (pixel confirm) rather than from the raw match: it glues the track exactly the same,
        but does NOT clear the outlier strikes — otherwise a spent-carryover bar lingering at the
        old spot would confirm forever and STARVE the strike re-seed while the real next meter
        rises elsewhere (live: 2K keeps the spent meter on screen 1-2s into the next shot).
        A strike re-seed snapshots the pre-seed state; the caller pixel-verifies the adopted spot
        and calls rollback_reseed() if there is no coherent bar there (VFX immune, cuts adopt)."""
        try:
            cx = float(cx); cb = float(cb); w = float(w); t = float(t_s)
            gate = max(8.0, float(jump_gate_px))
        except Exception:
            self.last_event = "reject"
            return "reject"
        if not (np.isfinite(cx) and np.isfinite(cb) and np.isfinite(w) and np.isfinite(t)):
            # NaN/inf converts fine but would poison the state through every later blend — reject it
            # here so "never raises" also means "never corrupts".
            self.last_event = "reject"
            return "reject"
        # a rollback snapshot is only valid for the caller's IMMEDIATE post-reseed check — a stale
        # one must never let a later dt-gap reseed be "rolled back" onto ancient state.
        self._reseed_undo = None
        if self._t is None or self._cx._x is None:
            self._seed_pose(cx, cb, w, t)
            self.last_event = "seed"
            return "seed"
        dt = t - self._t
        if dt <= 0.0 or dt > self._max_dt:
            # out-of-order or a gap too large to integrate (dropout / shot boundary) -> re-seed
            self._seed_pose(cx, cb, w, t)
            self.last_event = "reseed"
            return "reseed"
        # position outlier gate vs the PREDICTED centre (a pan is not an outlier; a teleport is).
        # The gate is VELOCITY-AWARE, not flat: a distractor 200px away is a plausible "pan" only
        # while the meter is actually moving fast (live pans reach ~54px/frame). When the track is
        # near-static (post-release, where the green perfect-release VFX erupts), the effective
        # gate tightens to ~40px so a stands-banner/VFX blob can never be accepted as motion.
        pcx = self._cx.extrapolate(dt)
        pcb = self._cb.extrapolate(dt)
        speed = float(np.hypot(self._cx.v, self._cb.v))          # px/s from the filter state
        eff_gate = min(gate, max(40.0, 3.5 * speed * dt + 25.0))
        if float(np.hypot(cx - pcx, cb - pcb)) > eff_gate:
            self._outliers += 1
            self._outlier_pts.append((cx, cb, w, t))
            if len(self._outlier_pts) > 3:
                self._outlier_pts.pop(0)
            if self._outliers >= 3 and len(self._outlier_pts) >= 3:
                # a REAL different meter tells a CONSISTENT story three times: the same place
                # (static) or a straight constant-velocity path (a panning meter — mean-spread
                # alone would blacklist any relocation moving >45px/frame forever) AND a
                # meter-scale width (a relocation keeps the meter's size; the green-flame VFX
                # blob measures a w~12 sliver against a w~25 track and must never be adopted).
                # VFX/distractor chaos does neither.
                pts = np.asarray([(p[0], p[1]) for p in self._outlier_pts[-3:]], dtype=float)
                tts = np.asarray([p[3] for p in self._outlier_pts[-3:]], dtype=float)
                med_w = float(np.median([p[2] for p in self._outlier_pts[-3:]]))
                sw = self._w.p
                w_ok = sw <= 0.0 or (0.55 * sw) <= med_w <= (1.8 * sw)
                spread = float(np.max(np.linalg.norm(pts - pts.mean(axis=0), axis=1)))
                span = float(tts[2] - tts[0])
                vfit = (pts[2] - pts[0]) / span if span > 1e-6 else np.zeros(2)
                resid = float(np.linalg.norm(pts[1] - (pts[0] + vfit * (tts[1] - tts[0]))))
                if w_ok and (spread <= 45.0 or resid <= 25.0):
                    # snapshot for rollback_reseed(): the caller pixel-verifies the adopted spot
                    # (a stable VFX blob passes every geometric test; only "is there a coherent
                    # bottom-anchored bar THERE" separates it from a genuinely relocated meter).
                    self._reseed_undo = (
                        (None if self._cx._x is None else self._cx._x.copy(),
                         None if self._cx._P is None else self._cx._P.copy(), self._cx._n),
                        (None if self._cb._x is None else self._cb._x.copy(),
                         None if self._cb._P is None else self._cb._P.copy(), self._cb._n),
                        (None if self._w._x is None else self._w._x.copy(),
                         None if self._w._P is None else self._w._P.copy(), self._w._n),
                        self._t)
                    # seed WITH the fitted velocity (adopting a moving meter at v=0 would put the
                    # next prediction behind it and re-enter reject cycling immediately) and the
                    # strike-median width (robust against a single sliver frame).
                    self._seed_pose(cx, cb, med_w, t,
                                    vcx=float(np.clip(vfit[0], -2500.0, 2500.0)),
                                    vcb=float(np.clip(vfit[1], -2500.0, 2500.0)))
                    self.last_event = "reseed"
                    return "reseed"
                self._outliers = 0
                self._outlier_pts = []
            self.last_event = "reject"
            return "reject"
        if not confirm_feed:
            # a RAW in-gate measurement clears the strikes; a confirm-feed accept must NOT — the
            # spent-carryover bar can keep confirming at the old spot while the real next meter
            # rises elsewhere, and only the surviving strikes let the track adopt it in ~3 frames.
            self._outliers = 0
            self._outlier_pts = []
        self._t = t
        self._cx.advance_update(cx, dt)
        self._cb.advance_update(cb, dt)
        # size outlier: a sliver/merge (contour caught a fragment or swallowed a neighbour) must not
        # collapse/balloon the width — position still updated above, width holds.
        sw = self._w.p
        if sw <= 0.0 or (0.45 * sw) <= w <= (2.2 * sw):
            self._w.advance_update(w, dt)
        self.last_event = "accept"
        return "accept"

    def predict_pose(self, t_s: float) -> Optional[Tuple[float, float, float]]:
        """(cx, cb, w) extrapolated to t_s, or None when not ready / beyond the coast horizon.
        PURE: does not mutate state, so a following update() integrates the true full dt."""
        if not self.ready or self._t is None:
            return None
        dt = float(t_s) - self._t
        if dt < 0.0 or dt > self._coast:
            return None
        return (self._cx.extrapolate(dt), self._cb.extrapolate(dt), max(1.0, self._w.p))

    # -- full-track height (green-sighting-fed) ---------------------------------------
    def update_height(self, h_full: float, t_s: float) -> None:
        try:
            h = float(h_full); t = float(t_s)
        except Exception:
            return
        if not (np.isfinite(h) and np.isfinite(t)) or h <= 4.0:
            return
        if self._t_h is None or self._h._x is None:
            self._h.seed(h)
            self._t_h = t
            return
        dt = t - self._t_h
        if dt <= 0.0:
            return
        if dt > 4.0:
            # green sightings are sparse; a very old height is a different episode/scale -> re-seed
            self._h.seed(h)
            self._t_h = t
            return
        self._t_h = t
        # sliver-guard mirrors the width channel
        sh = self._h.p
        if sh <= 0.0 or (0.5 * sh) <= h <= (2.0 * sh):
            self._h.advance_update(h, dt)

    # -- accessors ---------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._cx._x is not None and self._cx.n >= 3

    @property
    def ready_h(self) -> bool:
        return self._h._x is not None and self._h.n >= 2

    def cx(self) -> float:
        return self._cx.p

    def cb(self) -> float:
        return self._cb.p

    def width(self) -> float:
        return max(1.0, self._w.p)

    def height(self) -> float:
        return max(1.0, self._h.p)


def try_load() -> Optional["MeterBoxKalman"]:
    """Return a MeterBoxKalman iff ORION_METER_TRACK=1, else None (inert unless enabled)."""
    if os.environ.get("ORION_METER_TRACK", "0") != "1":
        return None
    try:
        return MeterBoxKalman(
            q_pos=float(os.environ.get("ORION_METER_TRACK_QPOS", "60000")),
            r_pos=float(os.environ.get("ORION_METER_TRACK_RPOS", "4.0")),
            q_size=float(os.environ.get("ORION_METER_TRACK_QSIZE", "400")),
            r_size=float(os.environ.get("ORION_METER_TRACK_RSIZE", "6.0")),
            max_dt_s=float(os.environ.get("ORION_METER_TRACK_MAXDT", "0.6")),
            coast_s=float(os.environ.get("ORION_METER_TRACK_COAST", "0.7")),
        )
    except Exception:
        return None
