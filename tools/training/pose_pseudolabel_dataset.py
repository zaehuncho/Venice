#!/usr/bin/env python3
"""Self-training step 1: auto-label 2K recordings into a YOLO-pose dataset (no hand labels).

Runs a strong TEACHER pose model (yolov8x-pose / yolo11x-pose) over the 2K gameplay recordings and
writes the high-confidence single-player detections as YOLO-pose labels. A small STUDENT model
(yolo11n-pose) is then fine-tuned on this dataset (tools/training/pose_finetune.py) to adapt to the
2K avatar domain at nano speed. See docs/ANIMATION_ANCHOR.md.

The student cannot exceed the teacher on the ~4 domain-gap keypoints, but it DOES gain 2K-domain
detection/confidence + speed, and the shooting-arm keypoints we anchor on are reliable.

YOLO-pose label line (normalized 0-1): class cx cy w h  (px py v)*17   (v: 0 absent,1 occluded,2 vis)

Usage:
  C:\\Python314\\python.exe tools/training/pose_pseudolabel_dataset.py \
      [--videos-glob "C:\\Users\\Administrator\\Videos\\2026-06-*.mp4"] [--teacher yolov8x-pose.pt] \
      [--per-video 150] [--min-conf 0.55] [--out logs/diagnostics/pose_ds] [--val-frac 0.2]
"""
from __future__ import annotations

import argparse
import glob
import os
import random

import numpy as np


COCO_FLIP_IDX = [0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos-glob", default=r"C:\Users\Administrator\Videos\2026-06-*.mp4")
    ap.add_argument("--teacher", default="yolov8x-pose.pt")
    ap.add_argument("--per-video", type=int, default=150, help="max labeled frames per recording")
    ap.add_argument("--min-conf", type=float, default=0.55, help="keep a frame only if the teacher is this confident")
    ap.add_argument("--out", default="logs/diagnostics/pose_ds")
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--max-videos", type=int, default=6)
    ap.add_argument("--max-scan", type=int, default=1200,
                    help="stop scanning a clip after this many SAMPLED frames (skips 0-player/menu clips fast)")
    ap.add_argument("--multi", action="store_true",
                    help="label ALL detected players per frame (crowded Park/Rec), not just the top one")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    try:
        import cv2
        from ultralytics import YOLO
    except Exception as exc:
        print(f"need opencv + ultralytics: {exc}")
        return 2

    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        os.makedirs(os.path.join(args.out, sub), exist_ok=True)
    teacher = YOLO(args.teacher)
    vids = sorted(glob.glob(args.videos_glob))[: args.max_videos]
    print(f"teacher={args.teacher}  videos={len(vids)}  per-video<= {args.per_video}  min-conf={args.min_conf}")

    rng = random.Random(1234)
    kept = train = val = 0
    for v in vids:
        cap = cv2.VideoCapture(v)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        if total <= 0:
            cap.release()
            continue
        step = max(1, int(round(fps / 12.0)))  # ~12 sampled fps (decorrelate consecutive frames)
        labeled = 0
        i = 0
        scanned = 0
        base = os.path.splitext(os.path.basename(v))[0].replace(" ", "_")
        while labeled < args.per_video and scanned < args.max_scan:
            if not cap.grab():
                break
            if i % step == 0:
                scanned += 1
                ok, fr = cap.retrieve()
                if not ok:
                    break
                r = teacher.predict(fr, verbose=False, conf=args.min_conf)[0]
                if r.boxes is not None and len(r.boxes):
                    confs = r.boxes.conf.cpu().numpy()
                    idxs = ([p for p in range(len(confs)) if confs[p] >= args.min_conf]
                            if args.multi else [int(np.argmax(confs))])
                    idxs = [p for p in idxs if confs[p] >= args.min_conf]
                    if idxs:
                        xywhn = r.boxes.xywhn.cpu().numpy()
                        kxy_all = r.keypoints.xyn.cpu().numpy()             # (N,17,2)
                        kcf_all = r.keypoints.conf.cpu().numpy()           # (N,17)
                        lines = []
                        for p in idxs:
                            cx, cy, w, h = xywhn[p]
                            parts = [f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"]
                            for j in range(17):
                                kc = kcf_all[p][j]
                                v_flag = 2 if kc > 0.5 else (1 if kc > 0.1 else 0)
                                parts.append(f"{kxy_all[p][j,0]:.6f} {kxy_all[p][j,1]:.6f} {v_flag}")
                            lines.append(" ".join(parts))
                        split = "val" if rng.random() < args.val_frac else "train"
                        stem = f"{base}_{i:06d}"
                        cv2.imwrite(os.path.join(args.out, f"images/{split}/{stem}.jpg"), fr,
                                    [cv2.IMWRITE_JPEG_QUALITY, 90])
                        with open(os.path.join(args.out, f"labels/{split}/{stem}.txt"), "w") as fh:
                            fh.write("\n".join(lines) + "\n")
                        labeled += 1
                        kept += 1
                        train += split == "train"
                        val += split == "val"
            i += 1
        cap.release()
        print(f"  {os.path.basename(v):30} labeled {labeled}")

    yaml = os.path.join(args.out, "pose2k.yaml")
    with open(yaml, "w") as fh:
        fh.write(
            f"path: {os.path.abspath(args.out)}\n"
            "train: images/train\n"
            "val: images/val\n"
            "kpt_shape: [17, 3]\n"
            f"flip_idx: {COCO_FLIP_IDX}\n"
            "names:\n  0: person\n"
        )
    print(f"\nDONE: kept {kept} labeled frames (train {train} / val {val}) -> {args.out}")
    print(f"dataset yaml: {yaml}")
    print("next: tools/training/pose_finetune.py --data " + yaml)
    return 0 if kept > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
