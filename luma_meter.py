#!/usr/bin/env python
"""luma_meter.py -- luma-domain (Y-plane) primitives for the compressed-stream meter reader.

H.264/HEVC 4:2:0 destroys chroma (the red/green masks the capture-card reader lives on) but
PROTECTS luma edges: the fill boundary's ~45-60 grey-level step is the most compression-durable
measurement in the whole meter. These are the pure-numpy kernels the CompressedMeterReader
builds on; no cv2, no reader state -- unit-testable in isolation.

Y-profile model of the meter interior (index 0 = TOP of the strip, y grows downward):

    tip sliver (bright, ~145-166)          <- fixed structure at the track top
    empty track (near-black ~25-45)        <- L_dark plateau
    ---- fill boundary (the step K2 fits) ----
    fill body (mid ~76-105)                <- L_fill plateau

K2 = robust 4-parameter logistic fit p(y) = L_dark + (L_fill-L_dark)*sigmoid((y-y0)/s):
IRLS-Huber + Gauss-Newton with an analytic Jacobian. The boundary is the fitted y0 (an
adaptive-midpoint crossing -- stays unbiased as blur/brightness drift, unlike the fixed
0.20 threshold on a binarized chroma profile). The fitted width s absorbs deblocking blur
and doubles as a per-frame quality signal: q = (step/resid_rms)/s.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


def _sigmoid(u: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(u, -30.0, 30.0)))


@dataclass
class PlateauEMA:
    """Per-shot EMA of the profile plateaus. Freezes the fit's L_dark/L_fill when the profile
    is degenerate (near-empty / near-full: one plateau missing) and tracks the tip-sliver
    luminance so the bright-row threshold adapts to the encode's dimming."""
    l_dark: float = 0.0
    l_fill: float = 0.0
    l_tip: float = 150.0        # nominal green-chevron luma; EMA'd when the sliver is seen
    alpha: float = 0.2
    n: int = 0

    @property
    def ready(self) -> bool:
        return self.n > 0

    def update(self, l_dark: float, l_fill: float) -> None:
        if self.n == 0:
            self.l_dark, self.l_fill = float(l_dark), float(l_fill)
        else:
            a = self.alpha
            self.l_dark = (1 - a) * self.l_dark + a * float(l_dark)
            self.l_fill = (1 - a) * self.l_fill + a * float(l_fill)
        self.n += 1

    def update_tip(self, tip_luma: float) -> None:
        self.l_tip = 0.8 * self.l_tip + 0.2 * float(tip_luma)

    def reset(self) -> None:
        self.l_dark = 0.0
        self.l_fill = 0.0
        self.l_tip = 150.0
        self.n = 0


@dataclass
class FillFit:
    y0: float           # fill boundary, strip-row coordinates (float, sub-pixel)
    s: float            # fitted logistic width (px) -- blur estimate
    step: float         # L_fill - L_dark (grey levels)
    l_dark: float
    l_fill: float
    resid_rms: float
    q: float            # (step / resid_rms) / s -- per-frame quality (higher = sharper/cleaner)
    frozen: bool        # plateaus frozen to the EMA (degenerate window)


def _step_score_init(p: np.ndarray, halfwin: int = 6) -> Optional[float]:
    """Matched-filter boundary init: argmax of mean(below) - mean(above) over a +/-halfwin
    row window. O(n) via cumsum; far more noise/blur-robust than a single-pixel derivative
    peak (which noise beats once deblocking spreads the step over many rows)."""
    n = p.size
    if n < 2 * halfwin + 2:
        return None
    c = np.concatenate(([0.0], np.cumsum(p)))
    ys = np.arange(halfwin, n - halfwin)
    below = (c[ys + 1 + halfwin] - c[ys + 1]) / halfwin     # mean p[y+1 .. y+halfwin]
    above = (c[ys] - c[ys - halfwin]) / halfwin             # mean p[y-halfwin .. y-1]
    score = below - above
    k = int(np.argmax(score))
    if score[k] <= 0.0:
        return None
    return float(ys[k]) + 0.5


def _fit_window(p, y0_init, plateaus, window, min_step, s_init, s_lo, s_hi,
                huber_delta, iters):
    """One damped GN attempt in a +/-window around y0_init. Returns (FillFit|None, pinned)
    where pinned=True means the boundary ran into an INTERIOR window edge (the boundary
    probably left the window -> caller retries with a cold init)."""
    n = p.size
    lo = int(max(0, np.floor(y0_init) - window))
    hi = int(min(n, np.floor(y0_init) + window + 1))
    w = p[lo:hi]
    if w.size < 8:
        return None, False
    ys = np.arange(lo, hi, dtype=np.float64)
    third = max(2, w.size // 3)
    l_dark = float(np.median(w[:third]))
    l_fill = float(np.median(w[-third:]))
    # degenerate window: a plateau is missing (median step too small, or the boundary sits
    # at the profile end so one side has no rows) -> freeze plateaus to the EMA if we have
    # one, else this attempt cannot say anything credible.
    near_end = (y0_init - lo < 4 and lo == 0) or (hi - 1 - y0_init < 4 and hi == n)
    frozen = False
    if l_fill - l_dark < min_step or near_end:
        if plateaus is not None and plateaus.ready and \
                (plateaus.l_fill - plateaus.l_dark) >= min_step:
            l_dark, l_fill = plateaus.l_dark, plateaus.l_fill
            frozen = True
        else:
            return None, False
    s = float(s_init)
    y0 = float(np.clip(y0_init, lo, hi - 1))
    for _ in range(int(iters)):
        u = (ys - y0) / s
        sig = _sigmoid(u)
        amp = l_fill - l_dark
        r = w - (l_dark + amp * sig)
        aw = np.abs(r)
        wt = np.where(aw <= huber_delta, 1.0, huber_delta / np.maximum(aw, 1e-9))
        dsig = sig * (1.0 - sig)
        j_y0 = -amp * dsig / s
        j_s = -amp * dsig * u / s
        cols = [j_y0, j_s] if frozen else [j_y0, j_s, 1.0 - sig, sig]
        J = np.stack(cols, axis=1)
        JW = J * wt[:, None]
        JtJ = J.T @ JW
        # Levenberg damping + tiny ridge: keeps the near-collinear plateau columns from
        # blowing the step up when the window is mostly one plateau.
        JtJ = JtJ + 1e-3 * np.diag(np.diag(JtJ)) + 1e-6 * np.eye(J.shape[1])
        g = J.T @ (wt * r)
        try:
            step_v = np.linalg.solve(JtJ, g)
        except np.linalg.LinAlgError:
            return None, False
        y0 = float(np.clip(y0 + step_v[0], lo, hi - 1))
        s = float(np.clip(s + step_v[1], s_lo, s_hi))
        if not frozen:
            l_dark = float(np.clip(l_dark + step_v[2], 0.0, 255.0))
            l_fill = float(np.clip(l_fill + step_v[3], 0.0, 255.0))
            if l_fill - l_dark < 1.0:
                return None, False
    amp = l_fill - l_dark
    if amp < min_step:
        return None, False
    # pinned at an INTERIOR window edge = the true boundary likely left the window
    pinned = (y0 - lo < 0.75 and lo > 0) or (hi - 1 - y0 < 0.75 and hi < n)
    resid = w - (l_dark + amp * _sigmoid((ys - y0) / s))
    # Huber-capped RMS so a single ringing spike can't tank the quality signal
    rms = float(np.sqrt(np.mean(np.minimum(resid ** 2, (3.0 * huber_delta) ** 2))))
    q = (amp / max(rms, 1e-3)) / s
    return FillFit(y0=y0, s=s, step=amp, l_dark=l_dark, l_fill=l_fill,
                   resid_rms=rms, q=q, frozen=frozen), pinned


def fit_fill_boundary(profile, prev_y0: Optional[float] = None,
                      plateaus: Optional[PlateauEMA] = None, *,
                      window: int = 12, min_step: float = 15.0,
                      s_init: float = 1.5, s_lo: float = 0.7, s_hi: float = 6.0,
                      huber_delta: float = 8.0, iters: int = 4) -> Optional[FillFit]:
    """Fit the dark->fill luma step on a 1-D interior-column profile. Warm-starts from
    prev_y0 when given; if that window misses the boundary (fit rejected or pinned at a
    window edge -- e.g. the fill jumped further than +/-window between samples), retries
    once from a cold matched-filter init over the full profile. Returns None when no
    credible step exists -- callers treat that exactly like a chroma red-miss."""
    p = np.asarray(profile, dtype=np.float64)
    n = p.size
    if n < 8:
        return None
    inits = []
    if prev_y0 is not None and 0.0 <= float(prev_y0) < n:
        inits.append(float(prev_y0))
    cold = _step_score_init(p)
    if cold is not None and (not inits or abs(cold - inits[0]) > 1.5):
        inits.append(cold)
    best_pinned = None
    for y0i in inits:
        fit, pinned = _fit_window(p, y0i, plateaus, window, min_step,
                                  s_init, s_lo, s_hi, huber_delta, iters)
        if fit is not None and not pinned:
            return fit
        if fit is not None and best_pinned is None:
            best_pinned = fit
    # every attempt pinned: better a boundary at the window edge than nothing? No --
    # a pinned fit is exactly how a flat window fabricates a fill. Reject.
    return None


def find_tip_sliver(profile, plateaus: PlateauEMA) -> np.ndarray:
    """Row indices of the bright tip sliver in a Y profile -- the luma twin of the green-mask
    row test (grn_row >= thr). Threshold adapts between the fill plateau and the EMA'd tip
    luminance so encode dimming doesn't kill it. Returns a (possibly empty) index array with
    the same semantics as the chroma path's `gidx`."""
    p = np.asarray(profile, dtype=np.float64)
    if p.size < 4:
        return np.empty(0, dtype=np.int64)
    l_fill = plateaus.l_fill if plateaus.ready else 100.0
    thr = l_fill + 0.5 * (plateaus.l_tip - l_fill)
    if thr <= l_fill + 5.0:      # tip EMA collapsed onto the fill level -> nothing separable
        return np.empty(0, dtype=np.int64)
    idx = np.flatnonzero(p >= thr)
    if idx.size == 0:
        return idx
    # keep only the run nearest the TOP (the sliver lives at the track top; a capped meter
    # lights the whole interior -- that is the t_cap EVENT, not an anchor)
    breaks = np.flatnonzero(np.diff(idx) > 2)
    end = idx.size if breaks.size == 0 else breaks[0] + 1
    run = idx[:end]
    if run[0] > 0.45 * p.size:   # bright region nowhere near the top -> not the sliver
        return np.empty(0, dtype=np.int64)
    return run


def rail_pair_scan(y_band, *, spacing_lo: int, spacing_hi: int,
                   min_extent: int, max_extent: int,
                   h_win: int = 22, persist: float = 0.6, ridge_d: int = 3,
                   prior_x: Optional[float] = None) -> list:
    """K1 acquisition: find 'a dark flat valley flanked by two thin bright vertical rails'
    (the Arrow2 meter's compression-stable luma signature) in a Y-plane band crop.

    Chain: 2:1 row decimation -> bright-ridge map min(Y-Y_left, Y-Y_right) -> adaptive
    threshold max(8, 0.22*(P90-P50 local)) -> windowed column persistence (>= persist of an
    h_win-row window; deblocking breaks rails at macroblock rows, so continuity is NOT
    required) -> rail pairing at meter spacing -> INTERIOR VETO (darker than the rails,
    row-flat, mid-luma -- this is what kills fences/posts/mullions/jersey stripes) ->
    vertical growth across windows.

    Returns [(score, (x, y, w, h)), ...] in BAND coordinates, best first. Pure numpy.
    All px arguments are in band pixels (caller scales for resolution)."""
    y = np.asarray(y_band)
    if y.ndim != 2 or y.shape[0] < h_win or y.shape[1] < spacing_hi + 2 * ridge_d + 2:
        return []
    yb = y[::2].astype(np.float32)                     # rails are vertical: rows decimate free
    hw = max(3, h_win // 2)                            # window size in decimated rows
    d = ridge_d
    # bright thin vertical ridge: brighter than BOTH lateral neighbours at offset d
    center = yb[:, d:-d]
    ridge = np.minimum(center - yb[:, : -2 * d], center - yb[:, 2 * d:])
    # local contrast (P90-P50 of Y) on a coarse tile grid, upsampled to the ridge grid;
    # windows smaller than one tile (the relocate window) fall back to a global value
    th, tw = 32, 64
    if yb.shape[0] >= th and yb.shape[1] >= tw:
        gh, gw = yb.shape[0] // th, yb.shape[1] // tw
        tiles = yb[: gh * th, : gw * tw].reshape(gh, th, gw, tw)
        contrast = (np.percentile(tiles, 90, axis=(1, 3))
                    - np.percentile(tiles, 50, axis=(1, 3)))
        cmap = np.repeat(np.repeat(contrast, th, axis=0), tw, axis=1)
        full = np.empty_like(yb)
        r0, c0 = cmap.shape
        full[:r0, :c0] = cmap
        if r0 < full.shape[0]:
            full[r0:, :c0] = cmap[-1:, :]
        if c0 < full.shape[1]:
            full[:, c0:] = full[:, c0 - 1: c0]
    else:
        full = np.full_like(yb, float(np.percentile(yb, 90) - np.percentile(yb, 50)))
    thr = np.maximum(8.0, 0.22 * full[:, d:-d])
    B = (ridge > thr).astype(np.float32)
    # windowed column persistence via row cumsum; windows stride hw//2
    csum = np.cumsum(B, axis=0)
    csum = np.vstack([np.zeros((1, B.shape[1]), np.float32), csum])
    out = {}
    n_rows = B.shape[0]
    step = max(1, hw // 2)
    for y0 in range(0, max(1, n_rows - hw + 1), step):
        C = csum[y0 + hw] - csum[y0]                   # ridge-rows per column in this window
        ok = C >= persist * hw
        cols = np.flatnonzero(ok)
        if cols.size < 2:
            continue
        # local maxima only (a blurred rail spans 2-3 columns; keep the crest)
        keep = []
        for x in cols:
            l = C[x - 1] if x > 0 else -1
            r = C[x + 1] if x < C.size - 1 else -1
            if C[x] >= l and C[x] >= r:
                keep.append(x)
        rows = slice(y0, y0 + hw)
        for i, xl in enumerate(keep):
            for xr in keep[i + 1:]:
                sp = xr - xl
                if sp < spacing_lo:
                    continue
                if sp > spacing_hi:
                    break
                # interior veto (indices back in yb coords: ridge x + d)
                il, ir = xl + d + 2, xr + d - 1
                if ir - il < 3:
                    continue
                interior = yb[rows, il:ir]
                rail_luma = 0.5 * (yb[rows, xl + d].mean() + yb[rows, xr + d].mean())
                im = float(interior.mean())
                if im > rail_luma - 12.0:
                    continue                            # not a dark valley (bright gap/décor)
                if float(np.median(interior.std(axis=1))) > 20.0:
                    continue                            # textured (crowd/jersey), not a flat track
                if not (10.0 <= im <= 135.0):
                    continue                            # neither dark track nor red-in-Y fill
                key = (int(round(xl / 3.0)), int(round(sp / 3.0)))
                score_w = float(min(C[xl], C[xr]))
                ent = out.setdefault(key, {"xl": xl + d, "xr": xr + d, "rows": [],
                                           "persist": 0.0})
                ent["rows"].append((y0, y0 + hw))
                ent["persist"] = max(ent["persist"], score_w / hw)
    cands = []
    for ent in out.values():
        r0 = min(a for a, _ in ent["rows"]) * 2        # undo decimation
        r1 = max(b for _, b in ent["rows"]) * 2
        extent = r1 - r0
        if not (min_extent <= extent <= max_extent):
            continue
        x = ent["xl"]
        w = ent["xr"] - ent["xl"] + 1
        prox = -abs((x + w * 0.5) - prior_x) if prior_x is not None else 0.0
        score = extent + 20.0 * ent["persist"] + 0.15 * prox
        cands.append((float(score), (int(x), int(r0), int(w), int(extent))))
    cands.sort(key=lambda t: -t[0])
    return cands


def roi_sad(a, b) -> float:
    """Mean |diff| between two equally-shaped uint8 Y ROIs, 4:1 subsampled both dims.
    ~10us on a track-sized ROI; the stale-frame (encoder skip) detector."""
    if a is None or b is None or a.shape != b.shape or a.size == 0:
        return -1.0
    aa = a[::4, ::4].astype(np.int16)
    bb = b[::4, ::4].astype(np.int16)
    return float(np.mean(np.abs(aa - bb)))
