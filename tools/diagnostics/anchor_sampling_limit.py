"""anchor_sampling_limit.py -- how much of the pose-anchor's jitter is the 8 fps DUMP?

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverables 1 and 5).

anchor_drill_events.py measures the wrist-rise crossing on ~8 fps framedumps and gets a
jitter far above the 8 ms target.  Before blaming the SIGNAL we have to price the SAMPLING.
Three measurements:

  1. DECIMATION.  Re-run the same crossing on the same tracks with every 2nd sample dropped
     (bracket 113 -> 226 ms).  The shift per shot is the interpolation error at 226 ms; by
     the linear-interpolation error law (err ~ curvature * bracket^2 / 8) the same shot at
     16.7 ms costs (16.7/113)^2 = 1/46 of the 113 ms error.  This gives the extrapolation
     to a 60 fps dump WITHOUT one.

  2. TRACK QUALITY.  persons/frame, detection rate, shooter-keypoint confidence, and how
     often the "nearest person to the meter box" rule changes identity inside one shot --
     solo drill (A) vs the 5v5/Theater session (B).

  3. COUPLING.  Does the pose crossing move WITH the drawn meter onset shot to shot
     (Pearson r, session A, where detframes gives the onset at full rate)?  If it does, the
     pose is watching the same animation clock the meter is drawn from, and an earlier pose
     landmark can replace the onset.  If it does not, at this sampling it is noise.

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_sampling_limit.py
Outputs: D:\\NexusVision\\anchor_study\\sampling_limit.json
"""
from __future__ import annotations

import json
import os
import statistics as st

import numpy as np

import anchor_drill_events as E   # same folder

OUT = r"D:\NexusVision\anchor_study"


def spread(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    if len(xs) < 4:
        return {"n": len(xs)}
    med = st.median(xs)
    return {"n": len(xs), "med": round(med, 1), "sd": round(st.pstdev(xs), 1),
            "mad_sd": round(1.4826 * st.median([abs(x - med) for x in xs]), 1)}


def main():
    res = {}
    ts, dd, ff = E.meter_onsets()

    for sess, getter in (("A", E.shots_a), ("B", E.shots_b)):
        det = E.load(sess)
        frames = E.read_frames(sess)
        shots = getter()

        # ---- 2. track quality ------------------------------------------
        npf, conf, kpok, ident = [], [], [], []
        for s in shots:
            last_c = None
            flips = 0
            n = 0
            for r in frames:
                if not (s["press"] - 0.55 <= r["t_wall"] <= s["rel"] + 1.30):
                    continue
                a = det.get(r["idx"])
                if a is None:
                    continue
                npf.append(len(a))
                if not len(a):
                    continue
                if r["detected"] and r["bh"] >= 60:
                    cx, cy = r["bx"] + r["bw"] / 2.0, r["by"] + r["bh"] / 2.0
                    pc = np.stack([(a[:, 0] + a[:, 2]) / 2, (a[:, 1] + a[:, 3]) / 2], 1)
                    j = int(np.argmin(np.hypot(pc[:, 0] - cx, pc[:, 1] - cy)))
                else:
                    j = int(np.argmax(a[:, 4]))
                conf.append(float(a[j, 4]))
                k = a[j, 5:].reshape(17, 3)
                kpok.append(int((k[:, 2] >= 0.5).sum()))
                c = ((a[j, 0] + a[j, 2]) / 2, (a[j, 1] + a[j, 3]) / 2)
                if last_c is not None and np.hypot(c[0] - last_c[0],
                                                   c[1] - last_c[1]) > 120:
                    flips += 1
                last_c = c
                n += 1
            if n >= 3:
                ident.append(flips / float(n))
        res.setdefault(sess, {})["track_quality"] = {
            "frames": len(npf),
            "persons_per_frame_p50": float(np.percentile(npf, 50)) if npf else None,
            "persons_per_frame_p90": float(np.percentile(npf, 90)) if npf else None,
            "persons_per_frame_max": int(max(npf)) if npf else 0,
            "frames_with_no_person_pct": round(100.0 * sum(1 for x in npf if x == 0) /
                                               max(len(npf), 1), 1),
            "shooter_box_conf": spread(conf),
            "keypoints_conf_ge_0.5_of_17": spread(kpok),
            "track_jump_gt_120px_per_frame": spread(ident),
        }

        # ---- 1. decimation ---------------------------------------------
        full, dec = [], []
        for s in shots:
            pts = E.track_for(s, frames, det)
            a = E.crossing(pts, 0.5)
            b = E.crossing(pts[::2], 0.5)
            if a and b:
                full.append(a[0])
                dec.append(b[0])
        shift = [d - f for f, d in zip(full, dec)]
        res[sess]["decimation"] = {
            "n": len(shift),
            "full_bracket_ms": 113 if sess == "A" else 162,
            "shift_abs_med_ms": round(st.median([abs(x) for x in shift]), 1) if shift else None,
            "shift_sd_ms": round(st.pstdev(shift), 1) if len(shift) > 3 else None,
            "x50_full": spread(full), "x50_decimated": spread(dec),
        }

        # ---- 3. coupling (session A only) ------------------------------
        if sess == "A" and len(ts):
            xs, ys = [], []
            for s in shots:
                pts = E.track_for(s, frames, det)
                c = E.crossing(pts, 0.5)
                w = (ts >= s["press"]) & (ts <= s["press"] + 1.6) & (dd == 1) & (ff > 0)
                if c and w.any():
                    xs.append((ts[w][0] - s["press"]) * 1e3)
                    ys.append(c[0])
            if len(xs) >= 6:
                r = float(np.corrcoef(xs, ys)[0, 1])
                sl, ic = np.polyfit(xs, ys, 1)
                resid = np.array(ys) - (sl * np.array(xs) + ic)
                res[sess]["coupling_onset_vs_pose"] = {
                    "n": len(xs), "pearson_r": round(r, 3),
                    "slope": round(float(sl), 3),
                    "resid_sd_ms": round(float(np.std(resid)), 1),
                    "pose_minus_onset_med_ms": round(st.median(
                        [y - x for x, y in zip(xs, ys)]), 1),
                }

    # extrapolation
    a = res.get("A", {}).get("decimation", {})
    if a.get("shift_sd_ms"):
        e226 = a["shift_sd_ms"]            # error introduced going 113 -> 226 ms
        # linear-interp error ~ k*bracket^2 ; sd(err226 - err113) ~ k*(226^2-113^2)
        k = e226 / (226.0 ** 2 - 113.0 ** 2)
        res["extrapolation"] = {
            "note": "linear-interpolation error ~ k * bracket^2, k fitted from the "
                    "113 -> 226 ms decimation shift",
            "k_ms_per_ms2": round(k, 8),
            "interp_err_sd_at_113ms": round(k * 113.0 ** 2, 1),
            "interp_err_sd_at_16.7ms": round(k * 16.7 ** 2, 2),
        }
    with open(os.path.join(OUT, "sampling_limit.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=float)
    print(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
