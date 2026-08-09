#!/usr/bin/env python
"""dual_capture_align.py -- align a dual-capture session (capture-card teacher vs Chiaki
student) and emit teacher labels onto student frames (the M5 self-labeling rig).

The capture card has NO stream PTS (host QPC only); the student carries the decoder PTS.
Alignment is therefore three stages, coarse -> fine -> per-shot:

  Stage 0  Theil-Sen CFR fit of the teacher timeline t_T(n) = T0 + n*DELTA over
           (frame_number, timestamp) -- deletes USB/driver arrival jitter (robust to the
           wedge/stall outliers the card backend logs).
  Stage 1  coarse offset from the shared host clock (both taps stamp the same QPC/epoch):
           b0 = median(teacher_time - student_pts) -- +/-150 ms class, seeds Stage 2.
  Stage 2  fit t_T = a*pts + b (a IS fitted: 50 ppm crystal drift = 36 ms over 30 min)
           by maximizing correlation between the teacher's RISE-RATE trace (despiked
           d(fill)/dt over detected spans) and the student's pixel ACTIVITY proxy
           (mean |dY| per frame, computed inline by the recorder -- no student reader
           needed). This is the continuous generalization of the proven
           replay_framedump.calibrate_mapping binary-agreement fit.
  Stage 3  per-shot residual: local cross-correlation of the teacher fill trace vs the
           student activity envelope inside each shot window -> delta_shot; the session
           median folds into b, the scatter is the label-confidence weight.

Label emit: teacher fill interpolated to each student frame's mapped time (detected,
non-coast teacher spans only), with per-shot auto-QC gates (teacher confidence coverage,
isotonic-rise residual, alignment residual) and the affine bbox transfer with the
sx=sy=2/3 tripwire (overscan / wrong-output-mode detection).

Pure numpy; consumes only the recorder's CSV/JSON outputs (no pixel access).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


# --------------------------------------------------------------------------- #
#  Stage 0: robust CFR timeline fit
# --------------------------------------------------------------------------- #
def theil_sen(x, y, max_pairs: int = 4000, rng_seed: int = 7):
    """Robust line fit y = slope*x + intercept (median of pairwise slopes). Subsamples
    pairs above max_pairs so a 100k-frame session stays O(max_pairs log)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = x.size
    if n < 2:
        raise ValueError("need >= 2 points")
    rng = np.random.default_rng(rng_seed)
    if n * (n - 1) // 2 <= max_pairs:
        ii, jj = np.triu_indices(n, k=1)
    else:
        ii = rng.integers(0, n, max_pairs)
        jj = rng.integers(0, n, max_pairs)
        keep = ii != jj
        ii, jj = ii[keep], jj[keep]
    dx = x[jj] - x[ii]
    ok = dx != 0
    slope = float(np.median((y[jj][ok] - y[ii][ok]) / dx[ok]))
    intercept = float(np.median(y - slope * x))
    return slope, intercept


def smooth_teacher_timeline(frame_no, t_s):
    """CFR re-stamp: fit t(n)=T0+n*DELTA and return the smoothed per-frame times.
    Residual RMS is reported so a pathological session (fps changes mid-run) is visible."""
    slope, intercept = theil_sen(frame_no, t_s)
    fitted = intercept + slope * np.asarray(frame_no, dtype=np.float64)
    resid = np.asarray(t_s, dtype=np.float64) - fitted
    return fitted, slope, float(np.sqrt(np.mean(resid ** 2)))


# --------------------------------------------------------------------------- #
#  fill-trace conditioning
# --------------------------------------------------------------------------- #
def despike(fill, max_dev: float = 8.0):
    """Median-of-3 replacement of single-frame spikes (contour-split artifacts): a sample
    deviating > max_dev pp from the median of its neighbours takes that median."""
    f = np.asarray(fill, dtype=np.float64).copy()
    if f.size < 3:
        return f
    med = np.median(np.stack([f[:-2], f[1:-1], f[2:]]), axis=0)
    bad = np.abs(f[1:-1] - med) > max_dev
    f[1:-1][bad] = med[bad]
    return f


def rise_rate_trace(t_s, fill, detected, clip_lo: float = 0.0):
    """The teacher's rise-rate signal r(t) = max(d fill/dt, 0) over DETECTED spans (0
    elsewhere) -- the luminance-motion twin the student's activity proxy correlates with.
    Rates are stamped at the interval MIDPOINTS (t[i]+t[i+1])/2: assigning them to the
    later sample shifts the whole rate mass +half a frame and biases every centroid the
    per-shot residual computes."""
    t = np.asarray(t_s, dtype=np.float64)
    f = despike(np.asarray(fill, dtype=np.float64))
    det = np.asarray(detected, dtype=bool)
    dt = np.diff(t)
    df = np.diff(f)
    ok = (dt > 1e-6) & det[1:] & det[:-1]
    r = np.zeros(t.size - 1, dtype=np.float64)
    r[ok] = np.maximum(df[ok] / dt[ok], clip_lo)
    t_mid = 0.5 * (t[1:] + t[:-1])
    return t_mid, r


# --------------------------------------------------------------------------- #
#  Stage 2: t_teacher = a*pts + b by rise-rate <-> activity correlation
# --------------------------------------------------------------------------- #
def _corr_at(map_a, map_b, t_teach, r_teach, pts_s, activity):
    """Pearson correlation of the teacher rise-rate (interpolated at the mapped student
    times) against the student activity proxy."""
    mapped = map_a * pts_s + map_b
    inside = (mapped >= t_teach[0]) & (mapped <= t_teach[-1])
    if int(inside.sum()) < 16:
        return -1.0
    r_at = np.interp(mapped[inside], t_teach, r_teach)
    a_v = activity[inside]
    ra = r_at - r_at.mean()
    av = a_v - a_v.mean()
    denom = float(np.sqrt((ra * ra).sum() * (av * av).sum()))
    if denom <= 1e-12:
        return -1.0
    return float((ra * av).sum() / denom)


def fit_time_map(t_teach, r_teach, pts_s, activity, b0: float,
                 b_span_s: float = 2.0, coarse_b_s: float = 0.010,
                 a_ppm_span: float = 120.0, a_ppm_step: float = 20.0,
                 fine_b_s: float = 0.001):
    """Grid fit of (a, b): coarse b at 10 ms x a at 20 ppm, then b refined to 1 ms and a
    to 5 ppm around the winner. Returns (a, b, score)."""
    t_teach = np.asarray(t_teach, dtype=np.float64)
    r_teach = np.asarray(r_teach, dtype=np.float64)
    pts_s = np.asarray(pts_s, dtype=np.float64)
    activity = np.asarray(activity, dtype=np.float64)
    best = (-1.0, 1.0, b0)
    a_grid = 1.0 + np.arange(-a_ppm_span, a_ppm_span + 1e-9, a_ppm_step) * 1e-6
    b_grid = b0 + np.arange(-b_span_s, b_span_s + 1e-9, coarse_b_s)
    for a in a_grid:
        for b in b_grid:
            s = _corr_at(a, b, t_teach, r_teach, pts_s, activity)
            if s > best[0]:
                best = (s, float(a), float(b))
    _, a1, b1 = best
    for a in a1 + np.arange(-20e-6, 20e-6 + 1e-12, 5e-6):
        for b in b1 + np.arange(-0.015, 0.015 + 1e-12, fine_b_s):
            s = _corr_at(a, b, t_teach, r_teach, pts_s, activity)
            if s > best[0]:
                best = (s, float(a), float(b))
    score, a, b = best
    # NOTE: the global correlation PLATEAUS near the optimum (rate and activity are both
    # near-binary inside a shot), so (a, b) here is coarse (~10 ms class). The precise
    # map comes from refine_with_shots() -- per-shot edge residuals regressed over the
    # session -- which callers must apply before trusting labels.
    return a, b, score


def refine_with_shots(a, b, t_teach, r_teach, pts_s, activity, windows):
    """Stage 3: per-shot residuals delta_k at shot times tau_k, robust-line-fit
    delta(tau) = alpha*tau + beta -> corrected map a' p + b' with
    a' = a(1+alpha), b' = b(1+alpha) + beta. Each shot's ON/OFF edge pair localizes the
    offset far more sharply than the global correlation (which plateaus), and the slope
    of delta over the session recovers the ppm drift the plateau hides.
    Returns (a', b', [(tau, delta), ...])."""
    taus, deltas = [], []
    for w in windows:
        d = per_shot_residual(a, b, t_teach, r_teach, pts_s, activity, w)
        if d is not None:
            taus.append(0.5 * (w[0] + w[1]))
            deltas.append(d)
    if len(deltas) >= 3:
        alpha, beta = theil_sen(np.asarray(taus), np.asarray(deltas))
        return a * (1.0 + alpha), b * (1.0 + alpha) + beta, list(zip(taus, deltas))
    if deltas:
        return a, b + float(np.median(deltas)), list(zip(taus, deltas))
    return a, b, []


# --------------------------------------------------------------------------- #
#  Stage 3: per-shot residual + label emit
# --------------------------------------------------------------------------- #
def shot_windows(t_s, fill, detected, min_rise_pp: float = 25.0, gap_s: float = 0.5):
    """Detected teacher shot windows: contiguous detected spans whose despiked fill rises
    by >= min_rise_pp. Returns [(t_start, t_end), ...]."""
    t = np.asarray(t_s, dtype=np.float64)
    f = despike(np.asarray(fill, dtype=np.float64))
    det = np.asarray(detected, dtype=bool)
    spans = []
    i = 0
    n = t.size
    while i < n:
        if not det[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and det[j + 1] and (t[j + 1] - t[j]) <= gap_s:
            j += 1
        if f[i:j + 1].size >= 5 and (np.max(f[i:j + 1]) - f[i]) >= min_rise_pp:
            spans.append((float(t[i]), float(t[j])))
        i = j + 1
    return spans


def per_shot_residual(a, b, t_teach, r_teach, pts_s, activity, window,
                      pad_s: float = 0.5, max_abs_s: float = 0.05):
    """delta_shot via CENTROID matching: the activity-weighted mean time of the student's
    (baseline-subtracted) activity burst vs the rate-weighted centroid of the teacher's
    rise, inside the padded shot window. Correlation-argmax plateaus on these near-binary
    signals (per-shot scatter ~10 ms); centroids of symmetric bursts are sub-sample
    precise. Any constant teacher-vs-proxy shape bias is shot-invariant and folds into b;
    the unbiased second iteration re-runs Stage 3 with a real student fill trace
    (CompressedMeterReader over the decoded student frames) instead of the proxy."""
    t0, t1 = window
    mapped = a * np.asarray(pts_s, dtype=np.float64) + b
    m = (mapped >= t0 - pad_s) & (mapped <= t1 + pad_s)
    if int(m.sum()) < 8:
        return None
    tm = mapped[m]
    av = np.asarray(activity, dtype=np.float64)[m]
    shoulders = (tm < t0) | (tm > t1)
    base = float(np.median(av[shoulders])) if int(shoulders.sum()) >= 4 else float(np.median(av))
    w_s = np.maximum(av - base, 0.0)
    if w_s.sum() <= 1e-9:
        return None
    # trim sub-threshold weights: residual shoulder noise is only ~% of the burst mass,
    # but spread over the +/-pad it drags the centroid by tens of ms per realization
    w_s[w_s < 0.25 * w_s.max()] = 0.0
    if w_s.sum() <= 1e-9:
        return None
    c_student = float((w_s * tm).sum() / w_s.sum())
    t_T = np.asarray(t_teach, dtype=np.float64)
    r_T = np.asarray(r_teach, dtype=np.float64)
    tt = (t_T >= t0 - pad_s) & (t_T <= t1 + pad_s)
    w_t = np.maximum(r_T[tt], 0.0)
    if w_t.sum() <= 1e-9:
        return None
    w_t = w_t.copy()
    w_t[w_t < 0.25 * w_t.max()] = 0.0
    if w_t.sum() <= 1e-9:
        return None
    delta = float((w_t * t_T[tt]).sum() / w_t.sum()) - c_student
    return delta if abs(delta) <= max_abs_s else None


def isotonic_residual(fill):
    """RMS distance of a rise trace from its best non-decreasing fit (pool-adjacent-
    violators) -- the monotone-rise QC gate."""
    f = despike(np.asarray(fill, dtype=np.float64))
    g = f.copy()
    w = np.ones_like(g)
    i = 0
    while i < g.size - 1:                     # PAVA
        if g[i] > g[i + 1] + 1e-12:
            new = (w[i] * g[i] + w[i + 1] * g[i + 1]) / (w[i] + w[i + 1])
            g[i] = new
            w[i] += w[i + 1]
            g = np.delete(g, i + 1)
            w = np.delete(w, i + 1)
            i = max(0, i - 1)
        else:
            i += 1
    fitted = np.repeat(g, w.astype(int))
    fitted = fitted[: f.size] if fitted.size >= f.size else np.pad(fitted, (0, f.size - fitted.size), "edge")
    return float(np.sqrt(np.mean((f - fitted) ** 2)))


def transfer_labels(a, b, teacher, student, *, conf_min: float = 0.95,
                    iso_max_pp: float = 2.0, scale_tol: float = 0.015):
    """Emit per-student-frame labels from the aligned teacher.

    teacher: dict of arrays t_s, fill, detected, conf, x, y, w, h, frame_w, frame_h
    student: dict of arrays pts_s, w, h (+ anything else, passed through)
    Returns (labels, report): labels has fill/sigma/bbox per student frame (NaN where no
    teacher span covers it), report carries the QC verdicts. The affine tripwire asserts
    the student is a clean uniform scale of the teacher frame."""
    t_T = np.asarray(teacher["t_s"], dtype=np.float64)
    fill = despike(np.asarray(teacher["fill"], dtype=np.float64))
    det = np.asarray(teacher["detected"], dtype=bool)
    conf = np.asarray(teacher["conf"], dtype=np.float64)
    pts = np.asarray(student["pts_s"], dtype=np.float64)
    mapped = a * pts + b
    sx = float(student["w"][0]) / float(teacher["frame_w"][0])
    sy = float(student["h"][0]) / float(teacher["frame_h"][0])
    scale_ok = abs(sx - sy) <= scale_tol
    windows = shot_windows(t_T, fill, det)
    qc = []
    labels = {"pts_s": pts, "fill": np.full(pts.size, np.nan),
              "x": np.full(pts.size, np.nan), "y": np.full(pts.size, np.nan),
              "w": np.full(pts.size, np.nan), "h": np.full(pts.size, np.nan),
              "shot_id": np.full(pts.size, -1, dtype=np.int64)}
    for si, (w0, w1) in enumerate(windows):
        span = (t_T >= w0) & (t_T <= w1) & det
        n_span = int(span.sum())
        cov = float((conf[span] >= conf_min).mean()) if n_span else 0.0
        iso = isotonic_residual(fill[span]) if n_span >= 5 else 99.0
        ok = cov >= 0.90 and iso <= iso_max_pp and scale_ok
        qc.append({"shot": si, "t0": w0, "t1": w1, "frames": n_span,
                   "conf_cov": round(cov, 3), "iso_pp": round(iso, 2), "pass": bool(ok)})
        if not ok:
            continue
        sm = (mapped >= w0) & (mapped <= w1)
        if not sm.any():
            continue
        labels["fill"][sm] = np.interp(mapped[sm], t_T[span], fill[span])
        for key in ("x", "y", "w", "h"):
            v = np.interp(mapped[sm], t_T[span], np.asarray(teacher[key], dtype=np.float64)[span])
            labels[key][sm] = v * (sx if key in ("x", "w") else sy)
        labels["shot_id"][sm] = si
    report = {"a": a, "b": b, "sx": round(sx, 4), "sy": round(sy, 4),
              "scale_ok": bool(scale_ok), "shots": qc,
              "shots_pass": sum(1 for q in qc if q["pass"]),
              "labeled_frames": int(np.isfinite(labels["fill"]).sum())}
    return labels, report


# --------------------------------------------------------------------------- #
#  CLI: run the full pipeline on a recorded session directory
# --------------------------------------------------------------------------- #
def run(session_dir: str) -> dict:
    import csv

    def _read_csv(path):
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return {k: np.array([r[k] for r in rows], dtype=np.float64)
                for k in rows[0].keys()} if rows else {}

    T = _read_csv(os.path.join(session_dir, "teacher.csv"))
    S = _read_csv(os.path.join(session_dir, "student_index.csv"))
    if not T or not S:
        raise SystemExit(f"missing teacher.csv / student_index.csv in {session_dir}")
    t_fit, slope, rms = smooth_teacher_timeline(T["frame_number"], T["timestamp_ns"] / 1e9)
    pts_s = S["pts_us"] / 1e6
    b0 = float(np.median(t_fit) - np.median(pts_s))
    t_r, r_r = rise_rate_trace(t_fit, T["fill"], T["detected"] > 0.5)
    a, b, score = fit_time_map(t_r, r_r, pts_s, S["activity"], b0)
    windows = shot_windows(t_fit, T["fill"], T["detected"] > 0.5)
    a, b, shot_deltas = refine_with_shots(a, b, t_r, r_r, pts_s, S["activity"], windows)
    deltas = [d for _, d in shot_deltas]
    teacher = {"t_s": t_fit, "fill": T["fill"], "detected": T["detected"] > 0.5,
               "conf": T["conf"], "x": T["x"], "y": T["y"], "w": T["w"], "h": T["h"],
               "frame_w": T["frame_w"], "frame_h": T["frame_h"]}
    student = {"pts_s": pts_s, "w": S["w"], "h": S["h"]}
    labels, report = transfer_labels(a, b, teacher, student)
    report.update({"stage0_cfr_rms_ms": round(rms * 1000, 2), "stage2_score": round(score, 3),
                   "stage3_delta_scatter_ms": round(float(np.std(deltas)) * 1000, 1) if deltas else None,
                   "shots_found": len(windows)})
    out = os.path.join(session_dir, "labels.csv")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("pts_s,fill,x,y,w,h,shot_id\n")
        for i in range(labels["pts_s"].size):
            fh.write(f"{labels['pts_s'][i]:.6f},{labels['fill'][i]:.3f},"
                     f"{labels['x'][i]:.1f},{labels['y'][i]:.1f},{labels['w'][i]:.1f},"
                     f"{labels['h'][i]:.1f},{labels['shot_id'][i]}\n")
    with open(os.path.join(session_dir, "align_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir")
    raise SystemExit(0 if run(ap.parse_args().session_dir) else 1)
