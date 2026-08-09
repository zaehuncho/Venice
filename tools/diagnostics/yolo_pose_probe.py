#!/usr/bin/env python3
"""Offline YOLO-pose probe for the animation-anchor track (see docs/ANIMATION_ANCHOR.md).

Runs a YOLOv8/YOLO11 pose model over a 2K gameplay recording to (1) measure how reliably the player
avatar is detected, (2) extract the shooting-wrist height trajectory, and (3) auto-detect shot
RELEASE events (wrist-raise minima). This is the OFFLINE development/validation vehicle for the pose
anchor — it never runs in the live bot (a per-frame pose NN would add latency that defeats an
autogreener; the LIVE shadow detector is the cheap classical motion-peak in animation_anchor.py).

PROVEN 2026-06-20 on `2026-06-06 12-47-20.mp4` (practice gym, 120fps): 94% player detection over 3500
frames, 17 releases in 58s at a clean ~2.3s cadence, release wristY clustered 0.43-0.49 (norm) =
a steady physical landmark. YOLO weights auto-download once (needs internet).

Usage:
  C:\\Python314\\python.exe tools/diagnostics/yolo_pose_probe.py "C:\\Users\\Administrator\\Videos\\<file>.mp4" \
      [--frames 3500] [--model yolov8n-pose.pt] [--out logs/diagnostics/yolo_pose] [--csv]
Next step (correlation): pair these release times with the meter tip + the on-screen TIMING HUD to
test whether the pose anchor's release->tip variance beats the meter-appear anchor's (the gate).
"""
from __future__ import annotations

import argparse
import csv as _csv
import os
import sys

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--frames", type=int, default=3500, help="max frames to PROCESS (after step subsampling)")
    ap.add_argument("--model", default="yolov8n-pose.pt")
    ap.add_argument("--out", default="logs/diagnostics/yolo_pose")
    ap.add_argument("--conf", type=float, default=0.10, help="YOLO confidence floor (the 2K avatar is low-conf)")
    ap.add_argument("--csv", action="store_true", help="write the per-frame wrist trajectory to <out>/wrist_traj.csv")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    try:
        import cv2
        from ultralytics import YOLO
    except Exception as exc:  # pragma: no cover - env probe
        print(f"need opencv + ultralytics: {exc}")
        return 2
    if not os.path.isfile(args.video):
        print(f"no such video: {args.video}")
        return 2
    os.makedirs(args.out, exist_ok=True)
    m = YOLO(args.model)

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"video fps={fps:.0f} frames={total} dur={total / max(fps, 1):.0f}s")
    step = 2 if fps > 80 else 1  # ~60 effective fps

    rows = []  # (frame_idx, t, person_conf, shooting_wrist_y_norm)
    det = proc = i = 0
    while proc < args.frames:
        if not cap.grab():  # cheap sequential advance (no decode)
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            proc += 1
            r = m.predict(fr, verbose=False, conf=args.conf)[0]
            if r.boxes is not None and len(r.boxes):
                det += 1
                b = int(np.argmax(r.boxes.conf.cpu().numpy()))
                kp = r.keypoints.data.cpu().numpy()[b]
                H = fr.shape[0]
                wy = [kp[j, 1] for j in (9, 10) if kp[j, 2] > 0.3]  # COCO 9=L wrist, 10=R wrist
                swy = (min(wy) / H) if wy else float("nan")
                rows.append((i, i / fps, float(r.boxes.conf.cpu().numpy()[b]), swy))
        i += 1
    cap.release()
    print(f"processed={proc} detected={det} ({100 * det / max(proc, 1):.0f}%)  span={i / max(fps, 1):.0f}s")

    valid = [(fi, t, swy) for (fi, t, _c, swy) in rows if not np.isnan(swy)]
    print(f"frames with a shooting-wrist keypoint: {len(valid)}")
    if args.csv and rows:
        with open(os.path.join(args.out, "wrist_traj.csv"), "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["frame", "t_s", "person_conf", "wrist_y_norm"])
            w.writerows(rows)
        print(f"wrote {args.out}/wrist_traj.csv")
    if len(valid) <= 30:
        return 0

    A = np.array(valid, dtype=float)
    wy = A[:, 2]
    rest = float(np.median(wy))
    print(f"wristY(0=top): min={wy.min():.2f} rest(med)={rest:.2f} max={wy.max():.2f} raiseAmp={rest - wy.min():.2f}")
    events = []
    for k in range(2, len(wy) - 2):
        if (wy[k] < rest - 0.10 and wy[k] <= wy[k - 1] and wy[k] <= wy[k + 1]
                and wy[k] < wy[k - 2] and wy[k] < wy[k + 2]):
            if not events or (A[k, 1] - events[-1][1]) > 0.8:  # >0.8s apart = distinct shots
                events.append((int(A[k, 0]), float(A[k, 1]), float(wy[k])))
    print(f"release events (wrist-raise minima >0.8s apart): {len(events)}")
    for e in events[:12]:
        print(f"   t={e[1]:5.1f}s frame={e[0]} wristY={e[2]:.2f}")
    cap = cv2.VideoCapture(args.video)
    for n, (fi, t, _y) in enumerate(events[:4]):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if ok:
            rr = m.predict(fr, verbose=False, conf=args.conf)[0]
            cv2.imwrite(os.path.join(args.out, f"rel_{n}_t{t:.0f}s.png"), rr.plot())
    cap.release()
    print(f"saved {min(4, len(events))} annotated release frames to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
