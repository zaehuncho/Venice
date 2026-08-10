"""ONNX-vs-ultralytics pose parity + latency verification on REAL live frames.

Proves the decode in pose_onnx.py is correct by running BOTH backends on the same
player crops from a live framedump session and measuring keypoint agreement in pixels,
then reports the end-to-end per-call latency of each path (letterbox + inference +
decode — the number the 16.7 ms frame budget actually pays).

Crops are built exactly like the runtime does it (PoseTimingDetector._compute_crop_region
padding: 60 px sides / 40 px top-bottom around the detected player box).

Reference note: the .pt reference at imgsz=256 letterboxes to a stride-aligned RECTANGLE
(ultralytics auto=True) while the ONNX export is a fixed 256x256 square, so 1-3 px of
benign geometric disagreement is expected even with a perfect decode. A wrong channel
layout or a wrong un-letterbox produces tens-to-hundreds of px and fails the gate.

Usage:
    .venv\\Scripts\\python.exe tools\\diagnostics\\pose_onnx_parity.py
    (options: --frames-dir --stride --max-crops --conf --kp-conf --tol-med --tol-p95)

Exit code 0 = parity PASS, 1 = FAIL.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import cv2

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)


def crop_region(box, H, W):
    """Same padding as PoseTimingDetector._compute_crop_region."""
    x1, y1, x2, y2 = box[:4]
    return (max(0, int(x1) - 60), max(0, int(y1) - 40),
            min(W, int(x2) + 60), min(H, int(y2) + 40))


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", default=r"D:\VeniceTraining\framedump\session_20260809_115736")
    ap.add_argument("--stride", type=int, default=20, help="use every Nth frame")
    ap.add_argument("--max-crops", type=int, default=150)
    ap.add_argument("--pt", default=os.path.join(ROOT, "models", "orion_pose2k_n_v3.pt"))
    ap.add_argument("--onnx", default=None, help="override ONNX path (default: pose_onnx resolver)")
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--kp-conf", type=float, default=0.30)
    ap.add_argument("--tol-med", type=float, default=2.0, help="median kpt deviation gate (px)")
    ap.add_argument("--tol-p95", type=float, default=6.0, help="p95 kpt deviation gate (px)")
    args = ap.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from pose_onnx import OnnxPoseModel
    from ultralytics import YOLO

    onnx_model = OnnxPoseModel(args.onnx)
    print(f"[onnx] {onnx_model.describe()}  init-measured: "
          f"median {onnx_model.latency_ms_median:.2f} ms  p90 {onnx_model.latency_ms_p90:.2f} ms")
    pt_model = YOLO(args.pt)

    frames = sorted(f for f in os.listdir(args.frames_dir) if f.lower().endswith((".jpg", ".png")))
    if not frames:
        print(f"FAIL: no frames in {args.frames_dir}")
        return 1
    frames = frames[:: max(1, args.stride)]
    print(f"[data] {len(frames)} frames sampled from {args.frames_dir}")

    devs, wrist_devs, box_devs = [], [], []
    t_onnx, t_pt = [], []
    n_crops = n_matched = n_onnx_only = n_pt_only = n_neither = 0

    for fname in frames:
        if n_crops >= args.max_crops:
            break
        frame = cv2.imread(os.path.join(args.frames_dir, fname))
        if frame is None:
            continue
        H, W = frame.shape[:2]

        # Player discovery: full-frame .pt (the runtime's own fallback discovery path)
        rf = pt_model.predict(frame, verbose=False, conf=0.25)[0]
        if rf.boxes is None or len(rf.boxes) == 0:
            continue
        xyxy = rf.boxes.xyxy.cpu().numpy()
        areas = (xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])
        order = np.argsort(-areas)[:2]  # two largest players per frame

        for bi in order:
            if n_crops >= args.max_crops:
                break
            px1, py1, px2, py2 = crop_region(xyxy[bi], H, W)
            crop = frame[py1:py2, px1:px2]
            if crop.shape[0] < 60 or crop.shape[1] < 60:
                continue
            n_crops += 1

            t0 = time.perf_counter()
            ro = onnx_model.predict(crop, verbose=False, conf=args.conf)[0]
            t_onnx.append((time.perf_counter() - t0) * 1000.0)

            t0 = time.perf_counter()
            rp = pt_model.predict(crop, verbose=False, conf=args.conf, imgsz=256)[0]
            t_pt.append((time.perf_counter() - t0) * 1000.0)

            has_o = ro.boxes is not None and len(ro.boxes) > 0
            has_p = rp.boxes is not None and len(rp.boxes) > 0
            if not has_o and not has_p:
                n_neither += 1
                continue
            if has_o != has_p:
                n_onnx_only += int(has_o)
                n_pt_only += int(has_p)
                continue

            ob = ro.boxes.xyxy.cpu().numpy()
            ok = ro.keypoints.data.cpu().numpy()
            pb = rp.boxes.xyxy.cpu().numpy()
            pk = rp.keypoints.data.cpu().numpy()

            # Match every pt detection to its best-IoU onnx detection
            for pj in range(len(pb)):
                ious = [iou(pb[pj], ob[oj]) for oj in range(len(ob))]
                oj = int(np.argmax(ious))
                if ious[oj] < 0.5:
                    continue
                n_matched += 1
                box_devs.append(float(np.abs(pb[pj] - ob[oj]).max()))
                vis = (pk[pj, :, 2] >= args.kp_conf) & (ok[oj, :, 2] >= args.kp_conf)
                if vis.any():
                    d = np.sqrt(((pk[pj, vis, :2] - ok[oj, vis, :2]) ** 2).sum(axis=1))
                    devs.extend(d.tolist())
                    for wk in (9, 10):  # the wrists — the joints the timing reader consumes
                        if vis[wk]:
                            wrist_devs.append(float(np.sqrt(
                                ((pk[pj, wk, :2] - ok[oj, wk, :2]) ** 2).sum())))

    if not devs:
        print("FAIL: no co-detected keypoints to compare")
        return 1

    devs = np.array(devs)
    t_onnx_s, t_pt_s = sorted(t_onnx), sorted(t_pt)
    med = float(np.median(devs))
    p95 = float(np.percentile(devs, 95))

    print(f"\n[parity] crops={n_crops} matched-dets={n_matched} "
          f"(onnx-only={n_onnx_only} pt-only={n_pt_only} neither={n_neither})")
    print(f"[parity] keypoint deviation px (n={len(devs)}): "
          f"median {med:.2f}  p95 {p95:.2f}  max {devs.max():.2f}")
    if wrist_devs:
        wd = np.array(wrist_devs)
        print(f"[parity] WRIST deviation px (n={len(wd)}): "
              f"median {np.median(wd):.2f}  p95 {np.percentile(wd, 95):.2f}  max {wd.max():.2f}")
    if box_devs:
        print(f"[parity] box corner deviation px: median {np.median(box_devs):.2f} "
              f"max {max(box_devs):.2f}")
    print(f"\n[latency] end-to-end per call on real crops (letterbox+infer+decode):")
    print(f"[latency]   onnx        median {t_onnx_s[len(t_onnx_s)//2]:.2f} ms  "
          f"p90 {t_onnx_s[int(len(t_onnx_s)*0.9)]:.2f} ms")
    print(f"[latency]   ultralytics median {t_pt_s[len(t_pt_s)//2]:.2f} ms  "
          f"p90 {t_pt_s[int(len(t_pt_s)*0.9)]:.2f} ms")

    ok_gate = med <= args.tol_med and p95 <= args.tol_p95
    print(f"\n{'PASS' if ok_gate else 'FAIL'}: median {med:.2f} (gate {args.tol_med}) / "
          f"p95 {p95:.2f} (gate {args.tol_p95})")
    return 0 if ok_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
