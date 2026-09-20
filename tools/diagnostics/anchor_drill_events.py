"""anchor_drill_events.py -- does a POSE EVENT beat the DRAWN METER as the shot's anchor?

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverables 1, 4 and 5).

THE VALUE GATE (stated 2026-06-20, never measured on 2K27 until now): the meter path
already lands the same meter position every shot, yet the game grades 10-25 % LATE/EARLY
because the DRAWN meter's onset lags the press by a variable 0-130 ms.  If a pose event is
a TIGHTER clock than the drawn onset, it is worth fusing.  So for every shot we measure

    press -> pose event      (the animation's own onset)
    press -> meter onset     (what the engine buckets tempo on today)
    release command - pose event
    release command - meter onset

and compare their spreads, per shot type, and split by the game's own banner verdict.

THE POSE EVENT.  On the shooter track, wrist-y normalised inside the person box
(0 = box top, 1 = box bottom) falls from ~0.45 at rest to ~0.05 overhead.  The event is the
LINEARLY INTERPOLATED crossing of rest - f * depth, scanning BACKWARD from the highest-hand
sample so the crossing belongs to THIS release.  f is swept (0.25 / 0.5 / 0.75).

SAMPLING.  Both framedumps are ~8 fps (ORION_FRAMEDUMP_INTERVAL default 0.2), so a crossing
is bracketed by samples ~113 ms apart and the interpolation error dominates.  Every number
below is therefore an UPPER BOUND on a 60 fps anchor's jitter, and the tool reports the
bracket width per shot so the bound is auditable.

Sessions:
    A  session_20260915_185359   29 releases, banner verdicts from _analysis/panel_grade.json
    B  session_20260912_201355  107 presses,  press/release from the hud landmark study

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_drill_events.py --scan-b
    .venv/Scripts/python.exe tools/diagnostics/anchor_drill_events.py --events
Outputs: D:\\NexusVision\\anchor_study\\drill_events.json, drill_events.csv, poseB.npz
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics as st
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = r"D:\NexusVision\anchor_study"
SESS = {
    "A": r"D:\NexusVision\framedump\session_20260915_185359",
    "B": r"D:\NexusVision\framedump\session_20260912_201355",
}
ANA = r"D:\NexusVision\framedump\_analysis"
DETW = os.path.join(ANA, "detframes_window.csv")
SHOTS_B = os.path.join(REPO, "logs", "diagnostics", "hud_landmark_study",
                       "shots_table.csv")
ICON_B = os.path.join(REPO, "logs", "diagnostics", "hud_landmark_study",
                      "icon_shots.csv")
WEIGHTS = os.path.join(REPO, "logs", "diagnostics", "pose_train",
                       "pose2k_n_eff_phase2", "weights", "best.pt")

WR_L, WR_R = 9, 10
HIP_L, HIP_R = 11, 12


def frame_path(sess, idx):
    for d in (0, 1):
        p = os.path.join(SESS[sess], "f%05d_%d_raw.png" % (idx, d))
        if os.path.exists(p):
            return p
    return None


def read_frames(sess):
    rows = []
    with open(os.path.join(SESS[sess], "frames.csv"), newline="",
              encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({"idx": int(r["idx"]), "t_wall": float(r["t_wall"]),
                         "detected": int(r["detected"]), "fill": float(r["fill_pct"]),
                         "bx": int(r["bbox_x"]), "by": int(r["bbox_y"]),
                         "bw": int(r["bbox_w"]), "bh": int(r["bbox_h"])})
    return rows


def shots_a():
    out = []
    v = {}
    with open(os.path.join(ANA, "panel_grade.json"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("seq"):
                v[int(r["seq"])] = r.get("word", "")
    for s in json.load(open(os.path.join(ANA, "shots.json"), encoding="utf-8")):
        if s.get("seq") is None or s.get("press_t") is None or s.get("rel_t") is None:
            continue
        out.append({"id": "A%d" % s["seq"], "press": s["press_t"], "rel": s["rel_t"],
                    "type": s.get("shot") or "?", "verdict": v.get(s["seq"], ""),
                    "hold_ms": s.get("hold_ms")})
    return out


def shots_b():
    icon = {}
    if os.path.exists(ICON_B):
        with open(ICON_B, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    icon[int(r["epoch"])] = float(r["off_mid_rel_ms"])
                except Exception:
                    pass
    out = []
    with open(SHOTS_B, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("release_wall"):
                continue
            try:
                p, rl = float(r["press_wall"]), float(r["release_wall"])
            except Exception:
                continue
            ep = int(r["epoch"])
            out.append({"id": "B%d" % ep, "press": p, "rel": rl,
                        "type": r.get("shot_type") or "?", "verdict": r.get("banner") or "",
                        "hold_ms": float(r["hold_ms"]) if r.get("hold_ms") else None,
                        "icon_off_rel_ms": icon.get(ep)})
    return out


def scan(sess, shots, pre=0.55, post=1.30, imgsz=640, conf=0.20):
    """pose over the frames inside the shot windows only."""
    import cv2
    from ultralytics import YOLO
    m = YOLO(WEIGHTS)
    rows = read_frames(sess)
    want = set()
    for s in shots:
        for r in rows:
            if s["press"] - pre <= r["t_wall"] <= s["rel"] + post:
                want.add(r["idx"])
    want = sorted(want)
    print("session %s: %d frames in %d shot windows" % (sess, len(want), len(shots)))
    store = {}
    batch, bidx = [], []

    def flush():
        if not batch:
            return
        res = m.predict(batch, imgsz=imgsz, device=0, half=True, conf=conf, verbose=False)
        for i, r in zip(bidx, res):
            if r.boxes is None or len(r.boxes) == 0 or r.keypoints is None:
                store[i] = np.zeros((0, 56), np.float32)
            else:
                b = r.boxes.xyxy.cpu().numpy().astype(np.float32)
                c = r.boxes.conf.cpu().numpy().astype(np.float32)[:, None]
                k = r.keypoints.data.cpu().numpy().astype(np.float32)
                store[i] = np.concatenate([b, c, k.reshape(len(k), -1)], 1)
        batch.clear()
        bidx.clear()

    for n, i in enumerate(want):
        p = frame_path(sess, i)
        if p is None:
            continue
        batch.append(cv2.imread(p))
        bidx.append(i)
        if len(batch) == 8:
            flush()
        if n % 300 == 0:
            print("  %d/%d" % (n, len(want)), flush=True)
    flush()
    np.savez_compressed(os.path.join(OUT, "pose%s.npz" % sess),
                        idx=np.array(sorted(store), np.int32),
                        **{"p%d" % i: a for i, a in store.items()})
    print("stored", len(store))


def load(sess):
    z = np.load(os.path.join(OUT, "pose%s.npz" % sess))
    return {int(i): z["p%d" % int(i)] for i in z["idx"]}


def meter_onsets():
    """full-rate (60 fps) first-detection wall time per detframes row, session A."""
    ts, det, fill = [], [], []
    with open(DETW, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts.append(float(r["wall_ms"]) / 1000.0)
                det.append(int(r["detected"]))
                fill.append(float(r["fill_pct"]))
            except Exception:
                continue
    return np.array(ts), np.array(det), np.array(fill)


def track_for(shot, frames, det, pre=0.55, post=1.30):
    """the shooter's normalised wrist-y and hip-y over the shot window."""
    pts = []
    for r in frames:
        t = r["t_wall"]
        if not (shot["press"] - pre <= t <= shot["rel"] + post):
            continue
        a = det.get(r["idx"])
        if a is None or not len(a):
            continue
        if r["detected"] and r["bh"] >= 60:
            cx, cy = r["bx"] + r["bw"] / 2.0, r["by"] + r["bh"] / 2.0
            pc = np.stack([(a[:, 0] + a[:, 2]) / 2, (a[:, 1] + a[:, 3]) / 2], 1)
            j = int(np.argmin(np.hypot(pc[:, 0] - cx, pc[:, 1] - cy)))
        else:
            j = int(np.argmax(a[:, 4]))
        k = a[j, 5:].reshape(17, 3)
        h = max(1.0, a[j, 3] - a[j, 1])
        wr = WR_R if k[WR_R, 2] >= k[WR_L, 2] else WR_L
        pts.append({
            "t": t, "dt": (t - shot["press"]) * 1e3,
            "wr": float((k[wr, 1] - a[j, 1]) / h),
            "hip": float(((k[HIP_L, 1] + k[HIP_R, 1]) / 2 - a[j, 1]) / h),
            "conf": float(a[j, 4]), "h": h,
        })
    pts.sort(key=lambda p: p["t"])
    return pts


def crossing(pts, frac, pre_ms=0.0):
    """interpolated ms-after-press where wrist-y falls through rest - frac*depth.

    Scans BACKWARD from the lowest wrist-y (= highest hand) sample.  Returns
    (ms, bracket_ms, depth) or None.
    """
    if len(pts) < 3:
        return None
    rest_pool = [p["wr"] for p in pts if p["dt"] <= pre_ms + 60]
    if len(rest_pool) < 2:
        rest_pool = [p["wr"] for p in pts[:2]]
    rest = st.median(rest_pool)
    i_min = min(range(len(pts)), key=lambda i: pts[i]["wr"])
    depth = rest - pts[i_min]["wr"]
    if depth < 0.15 or i_min == 0:
        return None
    thr = rest - frac * depth
    for i in range(i_min, 0, -1):
        a, b = pts[i - 1]["wr"], pts[i]["wr"]
        if a >= thr > b:
            f = (a - thr) / max(a - b, 1e-9)
            ms = pts[i - 1]["dt"] + f * (pts[i]["dt"] - pts[i - 1]["dt"])
            return ms, pts[i]["dt"] - pts[i - 1]["dt"], depth
    return None


def spread(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    if len(xs) < 4:
        return {"n": len(xs)}
    med = st.median(xs)
    return {"n": len(xs), "med": round(med, 1), "sd": round(st.pstdev(xs), 1),
            "mad_sd": round(1.4826 * st.median([abs(x - med) for x in xs]), 1),
            "iqr": round(float(np.percentile(xs, 75) - np.percentile(xs, 25)), 1)}


def events():
    res = {"sessions": {}}
    rowsout = []
    ts, dd, ff = meter_onsets()

    for sess, getter in (("A", shots_a), ("B", shots_b)):
        npz = os.path.join(OUT, "pose%s.npz" % sess)
        if not os.path.exists(npz):
            print("skip session", sess, "(no", npz, ")")
            continue
        det = load(sess)
        frames = read_frames(sess)
        shots = getter()
        recs = []
        for s in shots:
            pts = track_for(s, frames, det)
            rec = {"id": s["id"], "type": s["type"], "verdict": s["verdict"],
                   "hold_ms": s["hold_ms"], "n_pts": len(pts),
                   "n_pre": sum(1 for p in pts if p["dt"] <= 0),
                   "icon_off_rel_ms": s.get("icon_off_rel_ms")}
            for f in (0.25, 0.5, 0.75):
                c = crossing(pts, f)
                if c:
                    rec["x%d" % int(f * 100)] = round(c[0], 1)
                    rec["x%d_bracket" % int(f * 100)] = round(c[1], 1)
                    rec["depth"] = round(c[2], 3)
                else:
                    rec["x%d" % int(f * 100)] = None
            # full-rate meter onset (session A only -- detframes covers that session)
            if sess == "A" and len(ts):
                w = (ts >= s["press"]) & (ts <= s["press"] + 1.6) & (dd == 1) & (ff > 0)
                rec["meter_onset_ms"] = (round(float((ts[w][0] - s["press"]) * 1e3), 1)
                                         if w.any() else None)
            else:
                rec["meter_onset_ms"] = None
            hold = s["hold_ms"] if s["hold_ms"] else (s["rel"] - s["press"]) * 1e3
            for f in (25, 50, 75):
                x = rec.get("x%d" % f)
                rec["rel_minus_x%d" % f] = (round(hold - x, 1) if x is not None else None)
            rec["rel_minus_onset"] = (round(hold - rec["meter_onset_ms"], 1)
                                      if rec.get("meter_onset_ms") is not None else None)
            recs.append(rec)
            rowsout.append(rec)

        blk = {"n_shots": len(recs),
               "n_with_x50": sum(1 for r in recs if r.get("x50") is not None),
               "bracket_ms": spread([r.get("x50_bracket") for r in recs]),
               "by_type": {}, "overall": {}}
        for key in ("x25", "x50", "x75", "meter_onset_ms",
                    "rel_minus_x25", "rel_minus_x50", "rel_minus_x75",
                    "rel_minus_onset"):
            blk["overall"][key] = spread([r.get(key) for r in recs])
        types = sorted({r["type"] for r in recs})
        for t in types:
            sub = [r for r in recs if r["type"] == t]
            if len(sub) < 4:
                continue
            blk["by_type"][t] = {k: spread([r.get(k) for r in sub])
                                 for k in ("x50", "meter_onset_ms", "rel_minus_x50",
                                           "rel_minus_onset")}
        # verdict split
        vs = {}
        for v in ("EXCELLENT", "LATE", "EARLY"):
            sub = [r for r in recs if r["verdict"] == v]
            if len(sub) >= 4:
                vs[v] = {k: spread([r.get(k) for r in sub])
                         for k in ("x50", "rel_minus_x50", "meter_onset_ms",
                                   "rel_minus_onset", "hold_ms")}
        blk["by_verdict"] = vs
        res["sessions"][sess] = blk

    with open(os.path.join(OUT, "drill_events.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=float)
    cols = ["id", "type", "verdict", "hold_ms", "n_pts", "n_pre", "depth",
            "x25", "x50", "x75", "x50_bracket", "meter_onset_ms",
            "rel_minus_x25", "rel_minus_x50", "rel_minus_x75", "rel_minus_onset",
            "icon_off_rel_ms"]
    with open(os.path.join(OUT, "drill_events.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rowsout:
            w.writerow([r.get(c) for c in cols])
    print(json.dumps(res, indent=1, default=float))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-a", action="store_true")
    ap.add_argument("--scan-b", action="store_true")
    ap.add_argument("--events", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if a.scan_a:
        scan("A", shots_a())
    if a.scan_b:
        scan("B", shots_b())
    if a.events:
        events()
    return 0


if __name__ == "__main__":
    sys.exit(main())
