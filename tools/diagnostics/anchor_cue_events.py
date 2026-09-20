"""anchor_cue_events.py -- which POSE EVENT predicts the release, and how tightly?

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverable 1, "Signal").

CORPUS.  The 1040-clip 60 fps 2K26 corpus lives on E:, which is not mounted, but the
2026-08-09 cue-TCN run left its POSE TRACKS behind:

    D:\\VeniceTraining\\no_meter_prep\\cue_labels_seq_cache.npz
        251 shots, 87 clips, 17 COCO keypoints x (x, y, conf) per sample,
        sampled every 3rd frame of 59.94 fps video  ->  50.05 ms per sample,
        window = [release - ~1.5 s, release + ~3 s], full-frame pixel coords.

So the timing resolution here is 50 ms, NOT 16.7 ms.  Every event time below is therefore
estimated by SUB-SAMPLE interpolation (linear for threshold crossings, parabolic for
extrema), and the tool measures how much of the residual is sampling by re-running the
whole study on the same tracks DECIMATED to 100 ms.  The 50 -> 100 ms degradation is the
empirical handle on what 16.7 ms would buy.

CAMERA.  2K pans and zooms, so no absolute pixel y is usable.  Every signal is
body-relative and scale-normalised:  u = (y_part - y_shoulder) / S,
S = median |y_ankle - y_shoulder| over the pre-shot part of the window.

EVENTS (all times in ms relative to the shot's own pose-release R):
    gather      knee rises (crouch): max of (y_knee - y_hip)/S before the push
    set_point   wrist crosses shoulder height going up  (u_wrist = 0)
    wrist_v0    last zero-crossing of wrist vertical velocity before R (the "set" pause)
    hip_apex    jump apex: parabolic minimum of (y_hip)/S    [camera-relative, see caveat]
    elbow_ext   elbow angle (shoulder-elbow-wrist) crosses 150 deg going open
    R           release proxy: parabolic minimum of y_wrist (highest hand)

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_cue_events.py
Outputs: D:\\NexusVision\\anchor_study\\cue_events.json, cue_events.csv
"""
from __future__ import annotations

import json
import math
import os
import statistics as st

import numpy as np

CACHE = r"D:\VeniceTraining\no_meter_prep\cue_labels_seq_cache.npz"
LABELS = r"D:\VeniceTraining\no_meter_prep\cue_labels.json"
OUT = r"D:\NexusVision\anchor_study"

SH_L, SH_R, EL_L, EL_R, WR_L, WR_R = 5, 6, 7, 8, 9, 10
HIP_L, HIP_R, KN_L, KN_R, AN_L, AN_R = 11, 12, 13, 14, 15, 16
CONF = 0.30


def mid(k, a, b, axis=1):
    """confidence-weighted midpoint of a left/right pair, NaN when both are weak."""
    ca, cb = k[:, a, 2], k[:, b, 2]
    va, vb = k[:, a, axis], k[:, b, axis]
    w = np.stack([np.where(ca >= CONF, 1.0, 0.0), np.where(cb >= CONF, 1.0, 0.0)])
    s = w.sum(0)
    out = np.where(s > 0, (w[0] * va + w[1] * vb) / np.maximum(s, 1), np.nan)
    return out


def best_wrist(k):
    """the SHOOTING wrist: the one whose y travels furthest up in the window."""
    out = {}
    for tag, (wi, ei, si) in (("L", (WR_L, EL_L, SH_L)), ("R", (WR_R, EL_R, SH_R))):
        y = np.where(k[:, wi, 2] >= CONF, k[:, wi, 1], np.nan)
        out[tag] = (y, wi, ei, si)
    trav = {t: (np.nanmax(v[0]) - np.nanmin(v[0])) if np.isfinite(v[0]).sum() > 5
            else -1 for t, v in out.items()}
    return out[max(trav, key=lambda t: trav[t])]


def interp_nan(a):
    a = np.asarray(a, float).copy()
    ok = np.isfinite(a)
    if ok.sum() < 4:
        return None
    idx = np.arange(len(a))
    a[~ok] = np.interp(idx[~ok], idx[ok], a[ok])
    return a


def smooth(a, w=3):
    if w <= 1:
        return a
    k = np.ones(w) / w
    return np.convolve(a, k, mode="same")


def parabolic_extreme(y, i, want_min=True):
    """sub-sample vertex of the parabola through (i-1, i, i+1); returns fractional index."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    a, b, c = y[i - 1], y[i], y[i + 1]
    den = (a - 2 * b + c)
    if abs(den) < 1e-9:
        return float(i)
    d = 0.5 * (a - c) / den
    if not np.isfinite(d) or abs(d) > 1.0:
        return float(i)
    return float(i) + float(d)


def cross(y, thr, lo, hi, rising):
    """LAST fractional index in [lo, hi) where y crosses thr in the given direction.

    Scanning backwards from the release is the causal definition: the wrist passed
    through this height on its way to THIS release, not on some earlier dribble.
    """
    for i in range(min(len(y), hi) - 1, max(1, lo) - 1, -1):
        a, b = y[i - 1], y[i]
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        if (rising and a >= thr > b) or ((not rising) and a <= thr < b):
            if abs(b - a) < 1e-9:
                return float(i)
            return float(i - 1) + float((a - thr) / (a - b))
    return None


def angle(ax, ay, bx, by, cx, cy):
    v1 = np.stack([ax - bx, ay - by], -1)
    v2 = np.stack([cx - bx, cy - by], -1)
    n1 = np.linalg.norm(v1, axis=-1)
    n2 = np.linalg.norm(v2, axis=-1)
    cosv = (v1 * v2).sum(-1) / np.maximum(n1 * n2, 1e-6)
    return np.degrees(np.arccos(np.clip(cosv, -1, 1)))


REJ = {}


def _rej(why):
    REJ[why] = REJ.get(why, 0) + 1
    return None


def events_for(k, dt_ms, rel_idx):
    """k: (T,17,3) full-frame px.  Returns {event: ms relative to R} + diagnostics."""
    T = len(k)
    if T < 12:
        return _rej("window_too_short")
    ywr, wi, ei, si = best_wrist(k)
    ysh = mid(k, SH_L, SH_R)
    yhip = mid(k, HIP_L, HIP_R)
    ykn = mid(k, KN_L, KN_R)
    yan = mid(k, AN_L, AN_R)
    ywr_i, ysh_i, yhip_i, ykn_i, yan_i = (interp_nan(a) for a in
                                          (ywr, ysh, yhip, ykn, yan))
    if any(a is None for a in (ywr_i, ysh_i, yhip_i, ykn_i)):
        return _rej("keypoints_too_sparse")
    scale = float(np.nanmedian(np.abs(yan_i - ysh_i))) if yan_i is not None else np.nan
    if not np.isfinite(scale) or scale < 20:
        scale = float(np.nanmedian(np.abs(yhip_i - ysh_i))) * 2.2
    if not np.isfinite(scale) or scale < 15:
        return _rej("no_scale")

    u_wr = smooth((ywr_i - ysh_i) / scale)        # 0 = wrist at shoulder height, + = below
    u_kn = smooth((ykn_i - yhip_i) / scale)
    u_hip = smooth((yhip_i - ysh_i) / scale)
    y_hip_abs = smooth(yhip_i / scale)            # camera-relative jump apex

    # R = highest hand (min of wrist y in body-relative units) near the labelled release
    lo = max(2, rel_idx - 8)
    hi = min(T - 2, rel_idx + 8)
    if hi <= lo:
        return _rej("release_at_window_edge")
    i_r = int(lo + int(np.argmin(u_wr[lo:hi])))
    fr_R = parabolic_extreme(u_wr, i_r)
    rest = float(np.nanmedian(u_wr[:max(4, i_r - 12)])) if i_r > 8 else float(u_wr[0])
    depth = rest - float(u_wr[i_r])
    if not np.isfinite(depth) or depth < 0.15:
        return _rej("raise_depth_lt_0.15")         # no real raise -> not a jumper

    ev = {"R": fr_R, "depth": depth, "rest": rest, "scale": scale}

    # set_point: wrist crosses shoulder height (u = 0) on the way up, before R
    ev["set_point"] = cross(u_wr, 0.0, max(1, i_r - 30), i_r + 1, rising=True)
    # half_rise: wrist crosses half of its own travel (shot-adaptive, robust to reach)
    ev["half_rise"] = cross(u_wr, rest - 0.5 * depth, max(1, i_r - 30), i_r + 1, True)
    ev["q_rise"] = cross(u_wr, rest - 0.25 * depth, max(1, i_r - 30), i_r + 1, True)
    ev["three_q_rise"] = cross(u_wr, rest - 0.75 * depth, max(1, i_r - 30), i_r + 1, True)

    # wrist_v0: last zero crossing of wrist velocity before the final push
    v = np.gradient(u_wr)
    j = None
    for i in range(i_r - 1, max(1, i_r - 20), -1):
        if v[i] < 0 <= v[i - 1] or (v[i] < 0 and v[i - 1] >= 0):
            j = i
            break
    if j is not None and abs(v[j] - v[j - 1]) > 1e-9:
        ev["wrist_v0"] = float(j - 1) + float(v[j - 1] / (v[j - 1] - v[j]))
    else:
        ev["wrist_v0"] = None

    # gather: deepest crouch = max of (knee - hip) before the rise
    g_hi = int(ev["half_rise"]) if ev["half_rise"] else i_r - 4
    g_lo = max(1, g_hi - 20)
    if g_hi - g_lo >= 3:
        i_g = g_lo + int(np.argmax(u_kn[g_lo:g_hi]))
        ev["gather"] = parabolic_extreme(-u_kn, i_g)
    else:
        ev["gather"] = None

    # hip_apex: camera-relative highest hip (jump apex)
    a_lo, a_hi = max(1, i_r - 10), min(T - 2, i_r + 10)
    i_a = a_lo + int(np.argmin(y_hip_abs[a_lo:a_hi]))
    ev["hip_apex"] = parabolic_extreme(y_hip_abs, i_a)

    # elbow_ext: elbow angle opening through 150 deg before R
    ang = smooth(angle(k[:, si, 0], k[:, si, 1], k[:, ei, 0], k[:, ei, 1],
                       k[:, wi, 0], k[:, wi, 1]))
    ev["elbow150"] = cross(-ang, -150.0, max(1, i_r - 25), i_r + 1, rising=True)
    ev["elbow_min"] = None
    e_lo, e_hi = max(1, i_r - 20), i_r + 1
    if e_hi - e_lo >= 3:
        i_e = e_lo + int(np.argmin(ang[e_lo:e_hi]))
        ev["elbow_min"] = parabolic_extreme(ang, i_e)

    out = {"depth": depth, "scale": scale, "T": T, "i_r": i_r}
    for kname in ("set_point", "q_rise", "half_rise", "three_q_rise", "wrist_v0",
                  "gather", "hip_apex", "elbow150", "elbow_min"):
        f = ev.get(kname)
        out[kname + "_ms"] = None if f is None else round((f - fr_R) * dt_ms, 2)
    out["R_frac"] = fr_R
    # causal tempo covariate: how long the gather -> quarter-rise leg already took
    if out.get("q_rise_ms") is not None and out.get("gather_ms") is not None:
        out["_rise_span"] = round(out["q_rise_ms"] - out["gather_ms"], 2)
    else:
        out["_rise_span"] = None
    return out


def robust(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    if len(xs) < 5:
        return {"n": len(xs)}
    med = st.median(xs)
    mad = st.median([abs(x - med) for x in xs])
    return {"n": len(xs), "med": round(med, 1), "sd": round(st.pstdev(xs), 1),
            "mad_sd": round(1.4826 * mad, 1),
            "p10": round(float(np.percentile(xs, 10)), 1),
            "p90": round(float(np.percentile(xs, 90)), 1),
            "iqr": round(float(np.percentile(xs, 75) - np.percentile(xs, 25)), 1)}


def fit_residual(rows, ev, covs):
    """least-squares fit of (R - event) on covariates; returns residual sd (ms)."""
    y, X = [], []
    for r in rows:
        v = r.get(ev + "_ms")
        if v is None:
            continue
        c = [r.get(c_) for c_ in covs]
        if any(x is None or not np.isfinite(x) for x in c):
            continue
        y.append(-v)                       # R - event, positive = event leads release
        X.append([1.0] + [float(x) for x in c])
    if len(y) < 20:
        return {"n": len(y)}
    A = np.array(X, float)
    b = np.array(y, float)
    coef, *_ = np.linalg.lstsq(A, b, rcond=None)
    res = b - A @ coef
    return {"n": len(y), "raw_sd": round(float(np.std(b)), 1),
            "resid_sd": round(float(np.std(res)), 1),
            "resid_mad_sd": round(1.4826 * float(np.median(np.abs(res - np.median(res)))), 1),
            "r2": round(float(1 - np.var(res) / max(np.var(b), 1e-9)), 3),
            "coef": [round(float(c), 3) for c in coef]}


def run(decimate=1):
    REJ.clear()
    z = np.load(CACHE, allow_pickle=True)
    meta = json.loads(str(z["meta"]))
    rows = []
    for m in meta:
        k = z[m["arr"]]
        step = int(m["step"]) * decimate
        dt = 1000.0 * step / float(m["fps"])
        rel_i = (int(m["rel"]) - int(m["start"])) / float(m["step"])
        kk = k[::decimate]
        e = events_for(kk, dt, int(round(rel_i / decimate)))
        if e is None:
            continue
        e["key"] = m["key"]
        e["clip"] = os.path.basename(m["path"])
        rows.append(e)
    return rows, dict(REJ)


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"corpus": {"cache": CACHE, "sample_ms": round(1000 * 3 / 59.94, 2)}}
    lab = json.load(open(LABELS, encoding="utf-8"))
    res["corpus"]["n_shots_labelled"] = lab["n_shots"]

    for dec, tag in ((1, "sample_50ms"), (2, "sample_100ms")):
        rows, rej = run(dec)
        blk = {"n_shots_with_events": len(rows), "rejected": rej,
               "events": {}, "fits": {}}
        for ev in ("set_point", "q_rise", "half_rise", "three_q_rise", "wrist_v0",
                   "gather", "hip_apex", "elbow150", "elbow_min"):
            lead = [(-r[ev + "_ms"]) if r.get(ev + "_ms") is not None else None
                    for r in rows]
            blk["events"][ev] = robust(lead)
        # per-shot tempo covariates available AT the event (causal): how long the
        # rise has already taken, and how deep the raise is.
        for ev in ("q_rise", "half_rise", "set_point", "wrist_v0", "gather"):
            blk["fits"][ev + "|const"] = fit_residual(rows, ev, [])
            blk["fits"][ev + "|tempo"] = fit_residual(
                rows, ev, ["_rise_span", "depth"])
        res[tag] = blk
        if dec == 1:
            import csv as _csv
            with open(os.path.join(OUT, "cue_events.csv"), "w", newline="",
                      encoding="utf-8") as f:
                w = _csv.writer(f)
                cols = ["key", "clip", "depth", "scale", "_rise_span"] + [
                    e + "_ms" for e in ("set_point", "q_rise", "half_rise",
                                        "three_q_rise", "wrist_v0", "gather",
                                        "hip_apex", "elbow150", "elbow_min")]
                w.writerow(cols)
                for r in rows:
                    w.writerow([r.get(c) for c in cols])
    with open(os.path.join(OUT, "cue_events.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=float)
    print(json.dumps(res, indent=1, default=float)[:6000])


if __name__ == "__main__":
    main()
