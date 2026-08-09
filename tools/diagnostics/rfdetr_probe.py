#!/usr/bin/env python3
"""Offline RF-DETR probe — measures player detection quality on 2K gameplay video.

Runs RF-DETR (Nano/Small/Medium) over a recording and reports:
  1. Player detection rate (frames with a person bbox)
  2. Confidence distribution
  3. Bbox stability (jitter between frames)
  4. Per-frame inference latency (ms)
  5. Saves annotated frames for visual inspection

Usage:
  python tools/diagnostics/rfdetr_probe.py "C:\\Users\\Administrator\\Videos\\<file>.mp4" \
      [--model nano] [--frames 500] [--conf 0.5] [--out logs/diagnostics/rfdetr_probe]
"""
from __future__ import annotations

import argparse
import csv as _csv
import os
import sys
import time

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--model", default="nano", choices=["nano", "small", "medium", "large"],
                     help="RF-DETR model size")
    ap.add_argument("--frames", type=int, default=500, help="max frames to process")
    ap.add_argument("--conf", type=float, default=0.01, help="confidence threshold")
    ap.add_argument("--out", default="logs/diagnostics/rfdetr_probe")
    ap.add_argument("--step", type=int, default=0, help="frame step (0=auto: 2 if >80fps else 1)")
    ap.add_argument("--csv", action="store_true", help="write per-frame stats to CSV")
    ap.add_argument("--checkpoint", default=None, help="path to fine-tuned .pth checkpoint")
    args = ap.parse_args()

    try:
        import cv2
        from rfdetr import RFDETRNano, RFDETRSmall, RFDETRMedium, RFDETRLarge
    except Exception as exc:
        print(f"need opencv + rfdetr: {exc}")
        return 2

    if not os.path.isfile(args.video):
        print(f"no such video: {args.video}")
        return 2

    os.makedirs(args.out, exist_ok=True)

    model_map = {
        "nano": RFDETRNano,
        "small": RFDETRSmall,
        "medium": RFDETRMedium,
        "large": RFDETRLarge,
    }
    model_cls = model_map[args.model]
    print(f"Loading RF-DETR {args.model}...")
    t0 = time.perf_counter()
    if args.checkpoint:
        model = model_cls.from_checkpoint(args.checkpoint)
        print(f"  loaded checkpoint: {args.checkpoint}")
    else:
        model = model_cls()
    load_ms = (time.perf_counter() - t0) * 1000
    print(f"  model loaded in {load_ms:.0f}ms")

    # GPU optimization for lower latency
    try:
        import torch
        if torch.cuda.is_available():
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
            model.optimize_for_inference(dtype=torch.float16)
            print("  optimized for inference (FP16)")
    except Exception as e:
        print(f"  GPU opt skipped: {e}")

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"video: {w}x{h} fps={fps:.0f} frames={total} dur={total / max(fps, 1):.0f}s")

    step = args.step if args.step > 0 else (2 if fps > 80 else 1)

    rows = []
    det_count = 0
    proc = 0
    i = 0
    latencies = []
    person_bboxes = []

    while proc < args.frames:
        if not cap.grab():
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            proc += 1

            t0 = time.perf_counter()
            detections = model.predict(frame, threshold=args.conf)
            infer_ms = (time.perf_counter() - t0) * 1000
            latencies.append(infer_ms)

            # detections is a sv.Detections object
            has_person = False
            best_conf = 0.0
            best_bbox = None
            all_class_ids = []
            all_confs_list = []

            if hasattr(detections, 'class_id') and detections.class_id is not None and len(detections.class_id) > 0:
                all_class_ids = list(np.array(detections.class_id))
                all_confs_list = list(np.array(detections.confidence))
                # COCO class 0 = person
                person_mask = np.array(detections.class_id) == 0
                if np.any(person_mask):
                    has_person = True
                    person_confs = np.array(detections.confidence)[person_mask]
                    person_xyxy = np.array(detections.xyxy)[person_mask]
                    best_idx = int(np.argmax(person_confs))
                    best_conf = float(person_confs[best_idx])
                    best_bbox = person_xyxy[best_idx]
                    person_bboxes.append(best_bbox)
                else:
                    # Track best detection regardless of class
                    all_confs_arr = np.array(detections.confidence)
                    if len(all_confs_arr) > 0:
                        best_idx = int(np.argmax(all_confs_arr))
                        best_conf = float(all_confs_arr[best_idx])
                        best_bbox = np.array(detections.xyxy)[best_idx]

            if has_person:
                det_count += 1

            rows.append((i, i / fps, infer_ms, int(has_person), best_conf,
                         best_bbox[0] if best_bbox is not None else 0,
                         best_bbox[1] if best_bbox is not None else 0,
                         best_bbox[2] if best_bbox is not None else 0,
                         best_bbox[3] if best_bbox is not None else 0))

            if proc % 50 == 0:
                print(f"  [{proc}/{args.frames}] person_rate={100*det_count/proc:.0f}% any_det={100*len([r for r in rows if r[4]>0])/proc:.0f}% avg_lat={np.mean(latencies[-50:]):.1f}ms classes_seen={sorted(set(all_class_ids))[:10]}")
        i += 1

    cap.release()

    # Summary
    det_rate = 100 * det_count / max(proc, 1)
    avg_lat = np.mean(latencies) if latencies else 0
    p50_lat = np.percentile(latencies, 50) if latencies else 0
    p95_lat = np.percentile(latencies, 95) if latencies else 0
    fps_achievable = 1000 / avg_lat if avg_lat > 0 else 0

    print(f"\n{'='*60}")
    print(f"RF-DETR {args.model} Results")
    print(f"{'='*60}")
    print(f"  Frames processed:  {proc}")
    print(f"  Player detected:   {det_count} ({det_rate:.0f}%)")
    print(f"  Inference latency: avg={avg_lat:.1f}ms  p50={p50_lat:.1f}ms  p95={p95_lat:.1f}ms")
    print(f"  Achievable FPS:    {fps_achievable:.0f}")
    print(f"  Span:              {i / max(fps, 1):.0f}s")

    # Bbox stability (frame-to-frame center jitter)
    if len(person_bboxes) > 10:
        centers = np.array([[(b[0]+b[2])/2, (b[1]+b[3])/2] for b in person_bboxes])
        diffs = np.diff(centers, axis=0)
        jitter = np.sqrt(diffs[:, 0]**2 + diffs[:, 1]**2)
        print(f"  Bbox center jitter: mean={np.mean(jitter):.1f}px  med={np.median(jitter):.1f}px  p95={np.percentile(jitter, 95):.1f}px")
        # Bbox size consistency
        sizes = np.array([abs(b[2]-b[0]) * abs(b[3]-b[1]) for b in person_bboxes])
        print(f"  Bbox area: mean={np.mean(sizes):.0f}px²  std={np.std(sizes):.0f}px²  cv={np.std(sizes)/max(np.mean(sizes),1):.2f}")

    # Confidence distribution
    confs = [r[4] for r in rows if r[4] > 0]
    if confs:
        print(f"  Confidence (any class): mean={np.mean(confs):.2f}  med={np.median(confs):.2f}  min={np.min(confs):.2f}  max={np.max(confs):.2f}")

    # Class distribution
    all_detected_classes = []
    for r in rows:
        if r[4] > 0:
            all_detected_classes.append(int(r[3]))  # has_person flag as proxy
    # Re-scan to get actual class IDs
    class_counts = {}
    for r in rows:
        # We stored has_person in col 3, not class_id. Let's just report person vs non-person
        pass
    print(f"  Person frames: {det_count}  Non-person det frames: {len([r for r in rows if r[4]>0 and r[3]==0])}")

    print(f"{'='*60}")

    # Save annotated frames (any detection: first, midpoint, last)
    det_rows = [r for r in rows if r[4] > 0]  # any detection with conf > 0
    if len(det_rows) > 0:
        sample_indices = []
        if len(det_rows) >= 3:
            sample_indices = [0, len(det_rows)//2, len(det_rows)-1]
        elif len(det_rows) >= 1:
            sample_indices = list(range(min(3, len(det_rows))))

        cap = cv2.VideoCapture(args.video)
        for n, si in enumerate(sample_indices):
            fi = det_rows[si][0]
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if ok:
                dets = model.predict(frame, threshold=args.conf)
                # Draw all detections
                annotated = frame.copy()
                if hasattr(dets, 'xyxy') and dets.xyxy is not None and len(dets.xyxy) > 0:
                    for idx, bbox in enumerate(dets.xyxy):
                        x1, y1, x2, y2 = map(int, bbox)
                        conf = float(np.array(dets.confidence)[idx]) if hasattr(dets, 'confidence') else 0
                        cls_id = int(np.array(dets.class_id)[idx]) if hasattr(dets, 'class_id') and dets.class_id is not None else -1
                        color = (0, 255, 0) if cls_id == 0 else (0, 0, 255)  # green=person, red=other
                        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                        cv2.putText(annotated, f"cls{cls_id} {conf:.2f}", (x1, y1-5),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
                cv2.imwrite(os.path.join(args.out, f"det_{n}_frame{fi}.png"), annotated)
                print(f"  saved annotated frame {fi} -> {args.out}/det_{n}_frame{fi}.png")
        cap.release()

    # CSV
    if args.csv and rows:
        csv_path = os.path.join(args.out, "rfdetr_stats.csv")
        with open(csv_path, "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["frame", "t_s", "infer_ms", "has_person", "conf", "x1", "y1", "x2", "y2"])
            w.writerows(rows)
        print(f"  wrote {csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
