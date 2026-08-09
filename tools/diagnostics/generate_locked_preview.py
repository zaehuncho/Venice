#!/usr/bin/env python3
"""Generate annotated preview using lock_user_pose + StaminaTracker for correct player locking."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from ultralytics import YOLO
from stamina_lock import find_stamina_bar, StaminaTracker, lock_user_pose

POSE_MODEL = "models/orion_pose2k_n.pt"

KP_NAMES = {0:"nose",5:"l_sh",6:"r_sh",7:"l_el",8:"r_el",9:"l_wr",10:"r_wr",
            11:"l_hip",12:"r_hip",13:"l_kn",14:"r_kn",15:"l_an",16:"r_an"}
KP_COLORS = {9:(0,255,255), 10:(0,255,255),
             11:(255,0,0), 12:(255,0,0),
             13:(0,255,0), 14:(0,255,0)}

SKELETON = [(5,7),(7,9),(6,8),(8,10),(11,13),(13,15),(12,14),(14,16),(5,6),(11,12)]

def annotate(frame, result):
    ann = frame.copy()
    H, W = ann.shape[:2]

    if result is None:
        cv2.putText(ann, "NO LOCK", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)
        return ann

    bar = result.get("bar")
    src = result.get("src", "?")
    box = result.get("box")
    kpts = result.get("kpts")

    # Draw bar
    if bar:
        x, y, w, h = bar
        cv2.rectangle(ann, (x, y), (x+w, y+h), (0, 255, 0), 3)
        cv2.putText(ann, "BAR", (x, max(15, y-5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 2)

    # Draw player box
    if box is not None:
        x1, y1, x2, y2 = [int(v) for v in box]
        color = (0, 200, 0) if src == "bar" else (0, 180, 255)  # green=bar lock, orange=coasting
        cv2.rectangle(ann, (x1, y1), (x2, y2), color, 2)
        label = f"USER ({src})"
        cv2.putText(ann, label, (x1, max(15, y1-8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    # Draw skeleton
    if kpts is not None:
        for a, b in SKELETON:
            if kpts[a][2] > 0.3 and kpts[b][2] > 0.3:
                pa = kpts[a][:2].astype(int)
                pb = kpts[b][:2].astype(int)
                cv2.line(ann, pa, pb, (200, 200, 200), 2)
        for idx in range(len(kpts)):
            if kpts[idx][2] < 0.3:
                continue
            px, py = int(kpts[idx][0]), int(kpts[idx][1])
            color = KP_COLORS.get(idx, (255, 255, 255))
            cv2.circle(ann, (px, py), 5, color, -1)
            name = KP_NAMES.get(idx, str(idx))
            cv2.putText(ann, name, (px+6, py-4), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
        # Highlight wrists
        for idx in [9, 10]:
            if kpts[idx][2] > 0.3:
                px, py = int(kpts[idx][0]), int(kpts[idx][1])
                cv2.circle(ann, (px, py), 10, (0, 255, 255), 2)

    return ann

def main():
    pose = YOLO(POSE_MODEL)
    tracker = StaminaTracker(pose, max_age=12)

    videos = [
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4", "park_5min_b"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4", "park_5min_a"),
    ]

    out_dir = r"C:\Users\Administrator\Videos\annotated"
    os.makedirs(out_dir, exist_ok=True)

    for vpath, prefix in videos:
        if not os.path.exists(vpath):
            continue
        cap = cv2.VideoCapture(vpath)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        out_path = os.path.join(out_dir, f"{prefix}_locked.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(out_path, fourcc, fps, (W, H))

        # Process every 5th frame from 10% to 70% of video
        frame_indices = list(range(total//10, total*7//10, 5))
        bar_hits = 0
        track_hits = 0
        misses = 0

        for i, fi in enumerate(frame_indices):
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, fr = cap.read()
            if not ok:
                continue
            result = tracker.update(fr)
            if result:
                if result.get("src") == "bar":
                    bar_hits += 1
                else:
                    track_hits += 1
            else:
                misses += 1
            ann = annotate(fr, result)
            cv2.putText(ann, f"f{fi}", (W-100, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
            out.write(ann)

            if i % 100 == 0:
                print(f"  {prefix}: {i}/{len(frame_indices)} bar={bar_hits} track={track_hits} miss={misses}")

        out.release()
        cap.release()
        total_frames = bar_hits + track_hits + misses
        print(f"  {prefix}: bar_lock={bar_hits} ({100*bar_hits//max(total_frames,1)}%) "
              f"track={track_hits} ({100*track_hits//max(total_frames,1)}%) "
              f"miss={misses} ({100*misses//max(total_frames,1)}%)")
        print(f"  Saved: {out_path}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
