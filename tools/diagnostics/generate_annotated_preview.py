#!/usr/bin/env python3
"""Generate annotated preview video showing stamina bar + pose detections on park gameplay."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from stamina_lock import find_stamina_bar, _bar_model
from ultralytics import YOLO

POSE_MODEL = "models/orion_pose2k_n.pt"
BAR_MODEL = "models/orion_bar_park.pt"

KP_NAMES = {0:"nose",5:"l_sh",6:"r_sh",7:"l_el",8:"r_el",9:"l_wr",10:"r_wr",
            11:"l_hip",12:"r_hip",13:"l_kn",14:"r_kn",15:"l_an",16:"r_an"}
KP_COLORS = {9:(0,255,255), 10:(0,255,255),   # wrists - yellow
             11:(255,0,0), 12:(255,0,0),       # hips - blue
             13:(0,255,0), 14:(0,255,0)}        # knees - green

def annotate_frame(frame, pose_model, bar_model_obj):
    annotated = frame.copy()
    H, W = frame.shape[:2]

    # --- Stamina bar ---
    bar = find_stamina_bar(frame)
    if bar:
        x, y, w, h = bar
        cv2.rectangle(annotated, (x, y), (x+w, y+h), (0, 255, 0), 3)
        cv2.putText(annotated, "STAMINA BAR", (x, max(20, y-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:
        cv2.putText(annotated, "NO BAR", (20, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    # --- Pose ---
    r = pose_model.predict(frame, verbose=False, conf=0.3)[0]
    if r.keypoints is not None and len(r.keypoints):
        kpts = r.keypoints.xy.cpu().numpy()[0]  # [17, 2]
        confs = r.keypoints.conf.cpu().numpy()[0]  # [17]

        # Draw skeleton lines
        skeleton = [(5,7),(7,9),(6,8),(8,10),(11,13),(13,15),(12,14),(14,16),
                    (5,6),(11,12)]
        for a, b in skeleton:
            if confs[a] > 0.3 and confs[b] > 0.3:
                pa = kpts[a].astype(int)
                pb = kpts[b].astype(int)
                cv2.line(annotated, pa, pb, (200,200,200), 2)

        # Draw keypoints
        for idx, (px, py) in enumerate(kpts):
            if confs[idx] < 0.3:
                continue
            px, py = int(px), int(py)
            color = KP_COLORS.get(idx, (255,255,255))
            cv2.circle(annotated, (px, py), 5, color, -1)
            name = KP_NAMES.get(idx, str(idx))
            cv2.putText(annotated, name, (px+6, py-4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        # Highlight wrists (release detection landmark)
        for idx in [9, 10]:
            if confs[idx] > 0.3:
                px, py = int(kpts[idx][0]), int(kpts[idx][1])
                cv2.circle(annotated, (px, py), 10, (0, 255, 255), 2)

    return annotated

def main():
    pose = YOLO(POSE_MODEL)

    videos = [
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4", "park_5min_b"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4", "park_5min_a"),
    ]

    out_dir = "C:\\Users\\Administrator\\Videos\\annotated"
    os.makedirs(out_dir, exist_ok=True)

    for vpath, prefix in videos:
        if not os.path.exists(vpath):
            continue
        cap = cv2.VideoCapture(vpath)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Sample 200 frames from gameplay section, write as preview video
        out_path = os.path.join(out_dir, f"{prefix}_annotated.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(out_path, fourcc, fps, (W, H))

        # Process every 10th frame from 10% to 60% of video
        frame_indices = list(range(total//10, total*6//10, 10))
        for i, fi in enumerate(frame_indices):
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, fr = cap.read()
            if not ok:
                continue
            ann = annotate_frame(fr, pose, _bar_model())
            # Add frame number
            cv2.putText(ann, f"f{fi}", (W-120, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
            out.write(ann)

            if i % 50 == 0:
                print(f"  {prefix}: {i}/{len(frame_indices)}")

        out.release()
        cap.release()
        print(f"  Saved: {out_path}")

    print(f"\nAnnotated videos in: {out_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
