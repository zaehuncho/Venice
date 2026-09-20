"""anchor_drill_pose.py -- run the fine-tuned YOLO26n-pose over the 2K27 drill framedump.

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverables 1 and 5).

Stage 1 (--scan): pose over every dumped frame of
D:\\NexusVision\\framedump\\session_20260915_185359 (824 frames, 1280x720, ~8 fps),
storing every person detection (box + 17 COCO keypoints) to an npz.

Stage 2 (--join): join the detections to
  * frames.csv          (meter box + t_wall per dumped frame)
  * _analysis/shots.json (press_t / rel_t / shot type / landing per release)
  * _analysis/panel_grade.json (the game's own banner verdict per release)
and answer:
  a) WHICH person is the shooter -- the offset of the meter box from each person's box,
     so a live crop rule can be written (and how often the nearest-person rule is stable);
  b) what the pose says at the press / command frames, and whether ANY pose quantity
     separates the LATE releases from the EXCELLENT ones where no meter quantity could.

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_drill_pose.py --scan --join
Outputs: D:\\NexusVision\\anchor_study\\drill_pose.npz, drill_pose_join.json,
         drill_pose_shots.csv
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
DUMP = r"D:\NexusVision\framedump\session_20260915_185359"
ANA = r"D:\NexusVision\framedump\_analysis"
OUT = r"D:\NexusVision\anchor_study"
WEIGHTS = os.path.join(REPO, "logs", "diagnostics", "pose_train",
                       "pose2k_n_eff_phase2", "weights", "best.pt")

KP = {0: "nose", 5: "sh_l", 6: "sh_r", 7: "el_l", 8: "el_r", 9: "wr_l", 10: "wr_r",
      11: "hip_l", 12: "hip_r", 13: "kn_l", 14: "kn_r", 15: "an_l", 16: "an_r"}


def frame_path(idx):
    for d in (0, 1):
        p = os.path.join(DUMP, "f%05d_%d_raw.png" % (idx, d))
        if os.path.exists(p):
            return p
    return None


def read_frames_csv():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "idx": int(r["idx"]), "t_ms": float(r["t_ms"]),
                "t_wall": float(r["t_wall"]), "detected": int(r["detected"]),
                "fill": float(r["fill_pct"]),
                "bx": int(r["bbox_x"]), "by": int(r["bbox_y"]),
                "bw": int(r["bbox_w"]), "bh": int(r["bbox_h"]),
            })
    return rows


def scan(conf=0.20, imgsz=640):
    from ultralytics import YOLO
    m = YOLO(WEIGHTS)
    rows = read_frames_csv()
    idxs, persons = [], []
    batch, bidx = [], []

    def flush():
        if not batch:
            return
        res = m.predict(batch, imgsz=imgsz, device=0, half=True, conf=conf,
                        verbose=False)
        for i, r in zip(bidx, res):
            if r.keypoints is None or r.boxes is None or len(r.boxes) == 0:
                persons.append(np.zeros((0, 4 + 1 + 51), np.float32))
            else:
                b = r.boxes.xyxy.cpu().numpy().astype(np.float32)
                c = r.boxes.conf.cpu().numpy().astype(np.float32)[:, None]
                k = r.keypoints.data.cpu().numpy().astype(np.float32)
                k = k.reshape(k.shape[0], -1)
                persons.append(np.concatenate([b, c, k], 1))
            idxs.append(i)
        batch.clear()
        bidx.clear()

    import cv2
    for n, r in enumerate(rows):
        p = frame_path(r["idx"])
        if p is None:
            continue
        batch.append(cv2.imread(p))
        bidx.append(r["idx"])
        if len(batch) == 8:
            flush()
        if n % 200 == 0:
            print("  scan %d/%d" % (n, len(rows)), flush=True)
    flush()
    os.makedirs(OUT, exist_ok=True)
    np.savez_compressed(os.path.join(OUT, "drill_pose.npz"),
                        idx=np.array(idxs, np.int32),
                        **{"p%d" % i: a for i, a in zip(idxs, persons)})
    print("scanned %d frames, %d with >=1 person" %
          (len(idxs), sum(1 for a in persons if len(a))))


def load_scan():
    z = np.load(os.path.join(OUT, "drill_pose.npz"))
    return {int(i): z["p%d" % int(i)] for i in z["idx"]}


def read_verdicts():
    v = {}
    with open(os.path.join(ANA, "panel_grade.json"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("seq"):
                v[int(r["seq"])] = r.get("word", "")
    return v


def join():
    det = load_scan()
    rows = read_frames_csv()
    by_idx = {r["idx"]: r for r in rows}
    order = sorted(by_idx)
    twall = np.array([by_idx[i]["t_wall"] for i in order])
    shots = json.load(open(os.path.join(ANA, "shots.json"), encoding="utf-8"))
    verd = read_verdicts()

    out = {"crop_rule": {}, "shots": [], "late_vs_exc": {}}

    # ---- (a) where is the shooter relative to the meter box? -------------
    dxs, dys, hs, nper = [], [], [], []
    for r in rows:
        if not r["detected"] or r["bh"] < 60:
            continue
        a = det.get(r["idx"])
        if a is None or not len(a):
            continue
        cx = r["bx"] + r["bw"] / 2.0
        cy = r["by"] + r["bh"] / 2.0
        # nearest person centre to the meter box centre
        pc = np.stack([(a[:, 0] + a[:, 2]) / 2, (a[:, 1] + a[:, 3]) / 2], 1)
        d = np.hypot(pc[:, 0] - cx, pc[:, 1] - cy)
        j = int(np.argmin(d))
        dxs.append(pc[j, 0] - cx)
        dys.append(pc[j, 1] - cy)
        hs.append(a[j, 3] - a[j, 1])
        nper.append(len(a))

    def q(v, p):
        return round(float(np.percentile(v, p)), 1) if len(v) else None

    out["crop_rule"] = {
        "n_pairs": len(dxs),
        "person_cx_minus_box_cx": {"p10": q(dxs, 10), "p50": q(dxs, 50), "p90": q(dxs, 90)},
        "person_cy_minus_box_cy": {"p10": q(dys, 10), "p50": q(dys, 50), "p90": q(dys, 90)},
        "person_box_h": {"p10": q(hs, 10), "p50": q(hs, 50), "p90": q(hs, 90)},
        "persons_per_frame": {"p50": q(nper, 50), "p90": q(nper, 90),
                              "max": int(max(nper)) if nper else 0},
    }

    # ---- (b) per-shot pose at the press / command ------------------------
    recs = []
    for s in shots:
        seq = s["seq"]
        press = s["press_t"]
        rel = s["rel_t"]
        w = [i for i in order if press - 0.45 <= by_idx[i]["t_wall"] <= rel + 0.70]
        track = []
        for i in w:
            a = det.get(i)
            r = by_idx[i]
            if a is None or not len(a):
                track.append({"idx": i, "dt_press": round((r["t_wall"] - press) * 1e3, 1),
                              "n": 0})
                continue
            if r["detected"] and r["bh"] >= 60:
                cx, cy = r["bx"] + r["bw"] / 2.0, r["by"] + r["bh"] / 2.0
            else:
                cx, cy = None, None
            if cx is None:
                j = int(np.argmax(a[:, 4]))
            else:
                pc = np.stack([(a[:, 0] + a[:, 2]) / 2, (a[:, 1] + a[:, 3]) / 2], 1)
                j = int(np.argmin(np.hypot(pc[:, 0] - cx, pc[:, 1] - cy)))
            k = a[j, 5:].reshape(17, 3)
            h = max(1.0, a[j, 3] - a[j, 1])
            # normalised (0 = top of the person box, 1 = bottom): scale-free
            def ky(i_):
                return float((k[i_, 1] - a[j, 1]) / h)
            wr = 10 if k[10, 2] >= k[9, 2] else 9      # the more confident wrist
            track.append({
                "idx": i, "dt_press": round((by_idx[i]["t_wall"] - press) * 1e3, 1),
                "n": int(len(a)), "conf": round(float(a[j, 4]), 3),
                "box_h": round(h, 1),
                "wr_y": round(ky(wr), 4), "wr_c": round(float(k[wr, 2]), 3),
                "hip_y": round((ky(11) + ky(12)) / 2, 4),
                "kn_y": round((ky(13) + ky(14)) / 2, 4),
                "an_y": round((ky(15) + ky(16)) / 2, 4),
                "sh_y": round((ky(5) + ky(6)) / 2, 4),
                "box_top_abs": round(float(a[j, 1]), 1),
                "hip_abs": round(float((k[11, 1] + k[12, 1]) / 2), 1),
                "wr_abs": round(float(k[wr, 1]), 1),
                "kp_ok": int((k[:, 2] >= 0.5).sum()),
            })
        recs.append({
            "seq": seq, "shot": s.get("shot"), "verdict": verd.get(seq, ""),
            "hold_ms": round(s.get("hold_ms", 0.0), 1),
            "fill_at_issue": s.get("fill_at_issue"),
            "settled_fill": (s.get("landing") or {}).get("settled_fill"),
            "n_frames": len(track), "track": track,
        })
    out["shots"] = recs

    # ---- LATE vs EXCELLENT on every pose quantity we have ----------------
    def near(track, dt):
        cand = [t for t in track if t.get("n")]
        if not cand:
            return None
        return min(cand, key=lambda t: abs(t["dt_press"] - dt))

    groups = {"EXCELLENT": [], "LATE": []}
    for r in recs:
        if r["verdict"] in groups:
            groups[r["verdict"]].append(r)
    stats = {}
    for field in ("wr_y", "hip_y", "kn_y", "sh_y", "box_h", "conf", "kp_ok"):
        for dt, tag in ((0.0, "at_press"), (300.0, "press+300"), (600.0, "press+600")):
            vals = {}
            for g, rs in groups.items():
                xs = []
                for r in rs:
                    t = near(r["track"], dt)
                    if t and t.get(field) is not None and abs(t["dt_press"] - dt) < 130:
                        xs.append(float(t[field]))
                vals[g] = xs
            if len(vals["EXCELLENT"]) >= 4 and len(vals["LATE"]) >= 4:
                e, l = vals["EXCELLENT"], vals["LATE"]
                stats["%s@%s" % (field, tag)] = {
                    "EXC_n": len(e), "EXC_med": round(st.median(e), 4),
                    "EXC_sd": round(st.pstdev(e), 4),
                    "LATE_n": len(l), "LATE_med": round(st.median(l), 4),
                    "LATE_sd": round(st.pstdev(l), 4),
                    "delta": round(st.median(l) - st.median(e), 4),
                }
    out["late_vs_exc"] = stats

    # sampling reality check
    dts = np.diff(twall) * 1e3
    out["dump_sampling"] = {
        "n_frames": len(order), "median_gap_ms": round(float(np.median(dts)), 1),
        "p10_gap_ms": round(float(np.percentile(dts, 10)), 1),
        "p90_gap_ms": round(float(np.percentile(dts, 90)), 1),
        "effective_fps": round(1000.0 / float(np.median(dts)), 2),
        "frames_in_press_to_release_window_median": round(float(np.median(
            [sum(1 for t in r["track"] if 0 <= t["dt_press"] <=
                 (r["hold_ms"] or 700)) for r in recs])), 1),
    }

    with open(os.path.join(OUT, "drill_pose_join.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=float)
    with open(os.path.join(OUT, "drill_pose_shots.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["seq", "shot", "verdict", "hold_ms", "dt_press_ms", "n_person",
                    "conf", "box_h", "wr_y", "hip_y", "kn_y", "sh_y", "kp_ok"])
        for r in recs:
            for t in r["track"]:
                w.writerow([r["seq"], r["shot"], r["verdict"], r["hold_ms"],
                            t["dt_press"], t.get("n", 0), t.get("conf", ""),
                            t.get("box_h", ""), t.get("wr_y", ""), t.get("hip_y", ""),
                            t.get("kn_y", ""), t.get("sh_y", ""), t.get("kp_ok", "")])
    print(json.dumps({k: out[k] for k in ("crop_rule", "dump_sampling")}, indent=1))
    print("LATE vs EXC:")
    for k, v in out["late_vs_exc"].items():
        print(" ", k, v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--join", action="store_true")
    a = ap.parse_args()
    if a.scan:
        scan()
    if a.join:
        join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
