#!/usr/bin/env python3
"""Extract frames from shot-animation windows for v9 player-detector retraining.

Harvests frames across gather->jump->release animations from the known meter-dense
clips.  Also samples rest frames so the model keeps its at-rest performance.
Outputs frames + a YOLO-format label proposal (using v9's own predictions as
pseudo-labels, which the user can then correct in a labeler like labelImg).

Classes (MUST match v9): 0=Player, 1=Stamina, 2=Unplayable, 3=Zbasketball

Usage:
    C:\\Python314\\python.exe tools\\diagnostics\\extract_shot_frames.py [--per_clip 200]
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# Shot-dense windows known from eval runs
CLIPS = [
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4",
        "name": "park_v1",
        "windows": [(3760, 6760)],   # 6 meter shots (Purple)
    },
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4",
        "name": "park_v2",
        "windows": [(1000, 3500)],   # 3 meter shots (Purple)
    },
    # Round 2: Red-meter Park clips (4 new, ~3 min each)
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260624212008.mp4",
        "name": "park_v3",
        "windows": [(150, 3150), (3150, 6150), (6150, 9150)],
    },
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260624212347.mp4",
        "name": "park_v4",
        "windows": [(2880, 5880), (5880, 8880)],
    },
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260624212700.mp4",
        "name": "park_v5",
        "windows": [(0, 3000), (3000, 6240), (6240, 9240), (9240, 10784)],
    },
    {
        "path": r"C:\Users\Administrator\Videos\NBA 2K26_20260624213055.mp4",
        "name": "park_v6",
        "windows": [(7860, 10860)],
    },
]

# Sampling: every N-th frame in shot windows, plus rest frames outside
SHOT_SAMPLE_EVERY = 10   # every 10th frame in shot windows (~60 frames per 10s)
REST_SAMPLE_EVERY = 200  # every 200th frame outside shot windows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/diagnostics/player_shot_label")
    ap.add_argument("--per_clip", type=int, default=300, help="max frames per clip")
    ap.add_argument("--pseudo_label", action="store_true", default=True,
                    help="run v9 on each frame to write pseudo-labels")
    args = ap.parse_args()

    from ultralytics import YOLO

    out_img = os.path.join(args.out, "images", "train")
    out_lbl = os.path.join(args.out, "labels", "train")
    os.makedirs(out_img, exist_ok=True)
    os.makedirs(out_lbl, exist_ok=True)

    v9_path = os.path.join(ROOT, "models", "orion_player_detect_v9.pt")
    v9 = YOLO(v9_path) if os.path.exists(v9_path) else None
    if v9 is None:
        print("WARNING: v9 model not found -- extracting frames without pseudo-labels")
    else:
        print(f"v9 loaded: {v9.names}")

    total = 0
    for clip in CLIPS:
        path = clip["path"]
        name = clip["name"]
        if not os.path.exists(path):
            print(f"  [skip] {path} not found")
            continue

        cap = cv2.VideoCapture(path)
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        print(f"\n  {name}: {n_frames} frames @ {fps:.0f}fps")

        frame_idxs = set()
        for lo, hi in clip["windows"]:
            for fi in range(lo, min(hi, n_frames), SHOT_SAMPLE_EVERY):
                frame_idxs.add(fi)
        for fi in range(0, n_frames, REST_SAMPLE_EVERY):
            in_shot = any(lo <= fi < hi for lo, hi in clip["windows"])
            if not in_shot:
                frame_idxs.add(fi)

        frame_idxs = sorted(frame_idxs)
        if len(frame_idxs) > args.per_clip:
            idxs = np.linspace(0, len(frame_idxs) - 1, args.per_clip).astype(int)
            frame_idxs = [frame_idxs[i] for i in idxs]

        print(f"    sampling {len(frame_idxs)} frames (shot+rest)")

        for fi in frame_idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                continue

            stem = f"{name}_f{fi:06d}"
            img_path = os.path.join(out_img, stem + ".jpg")
            lbl_path = os.path.join(out_lbl, stem + ".txt")
            cv2.imwrite(img_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

            if v9 is not None:
                r = v9.predict(frame, verbose=False, conf=0.15, imgsz=640)[0]
                lines = []
                if r.boxes is not None and len(r.boxes):
                    H, W = frame.shape[:2]
                    xywh = r.boxes.xywh.cpu().numpy()
                    cls = r.boxes.cls.cpu().numpy().astype(int)
                    for i in range(len(cls)):
                        x, y, w, h = xywh[i]
                        lines.append(f"{cls[i]} {x/W:.6f} {y/H:.6f} {w/W:.6f} {h/H:.6f}")
                with open(lbl_path, "w") as f:
                    f.write("\n".join(lines))

            total += 1

        cap.release()

    print(f"\nExtracted {total} frames -> {out_img}")
    print(f"Pseudo-labels -> {out_lbl}")
    print("\nNEXT STEPS:")
    print("  1. Review/correct labels in labelImg or similar tool")
    print("     (focus on shot-animation frames where v9 missed the player)")
    print("  2. Create dataset.yaml (see tools/diagnostics/train_player_v9.py)")
    print("  3. Run: C:\\Python314\\python.exe tools\\diagnostics\\train_player_v9.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
