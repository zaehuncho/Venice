#!/usr/bin/env python3
"""Active-learning RANK + EXPORT for the No-Meter Mode pose student (NO retraining here).

Runs the trained STUDENT over the FULL corpus (not just the labeled subset), ranks frames
by model uncertainty, and exports a review queue the owner can flip through fast
(docs/NO_METER_MODE_TRAINING.md Stage 4b):

  uncertainty = mean(1 - conf) over detections
              + 0.25 * count(detections with 0.3 < conf < 0.6)     # the "hesitation zone"
  gameplay-like frames with NO detection at all score 1.0 (worst case: the student missed
  the player outright). Higher = more uncertain = more valuable to hand-label.

Menu/junk frames are skipped via the importable no_meter_mode_prep.classify_frame heuristic
(uncertainty on menu art is junk, not signal); --no-menu-filter disables that.

Outputs (under --dataset, default logs/diagnostics/pose_ds):
  active_learning_queue.json       top-K frames sorted by uncertainty: video path, frame
                                   index, score components, current-model prediction
  active_learning_review/          qNNNN_*.jpg overlays (predictions drawn) -- eyeball these
  active_learning_review/clean/    al_<stem>_f<idx>.jpg CLEAN frames + prefilled YOLO-pose
                                   .txt (same line format as pose_pseudolabel_dataset.py,
                                   dets >= 0.30 conf) -- correct the labels in your tool of
                                   choice, then copy each pair into the dataset's
                                   images/train + labels/train

The al_ stem prefix is exactly what pose_finetune_efficient.py --boost-weight N matches
(hand-labeled additions weighted N x by oversampling). Exported stems are also merged into
<dataset>/sources.json as --source (default owner) so the split_train_val.py provenance
invariant holds. This script never trains: hand-fix the top-K labels, then re-run
  python tools/training/pose_finetune_efficient.py --boost-weight 3

Usage:
  python tools/training/pose_active_learning.py \
      --model logs/diagnostics/pose_train/pose2k_n_eff_phase2/weights/best.pt \
      [--videos-root "E:\\PS5\\CREATE\\Video Clips\\NBA 2K26"] \
      [--dataset logs/diagnostics/pose_ds] [--top-k 200] [--min-uncertainty 0.3] \
      [--per-video 60] [--max-videos 0] [--conf-floor 0.05] [--no-menu-filter]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts")
DEFAULT_CORPUS = r"E:\PS5\CREATE\Video Clips\NBA 2K26"

HESITATION_LO = 0.3     # dets in (LO, HI) are the model "hesitating" -- neither sure nor junk
HESITATION_HI = 0.6
HESITATION_WEIGHT = 0.25
NO_DETECTION_SCORE = 1.0  # gameplay frame, zero detections: max mean-deficit, worst case
PREFILL_MIN_CONF = 0.30   # dets below this stay out of the prefilled label txt (junk-in-junk-out)


# ==============================================================================================
# CUDA preflight (task #69: fail EARLY and LOUD, never silently 8fps / CPU-crawl)
# ==============================================================================================

def preflight_cuda_torch():
    """Abort with the exact fix commands unless CUDA torch + ultralytics are importable."""
    py = sys.executable
    fix = (
        "\n"
        "FIX (run in the venv/python you will train with):\n"
        f'  "{py}" -m pip uninstall -y torch torchvision\n'
        f'  "{py}" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126\n'
        f'  "{py}" -m pip install ultralytics\n'
        "\n"
        "Never run this pipeline on CPU torch: CPU inference was the 2026-07-06 0/28 batch\n"
        "root cause (8fps detector). This rig's torch may be the +cpu build -- 'import torch\n"
        "works' is NOT the test; torch.cuda.is_available() is.\n"
    )
    try:
        import torch  # noqa: F401
    except Exception as exc:
        print(f"PREFLIGHT FAILED: torch is not importable ({exc}){fix}")
        return False
    import torch
    if not torch.cuda.is_available():
        built = getattr(torch.version, "cuda", None)
        print(f"PREFLIGHT FAILED: torch {torch.__version__} has no CUDA "
              f"(torch.cuda.is_available()=False, built-against-cuda={built}).{fix}")
        return False
    try:
        from ultralytics import YOLO  # noqa: F401
    except Exception as exc:
        print(f"PREFLIGHT FAILED: ultralytics is not importable ({exc}){fix}")
        return False
    print(f"preflight OK: torch {torch.__version__} CUDA={torch.version.cuda} "
          f"device={torch.cuda.get_device_name(0)}")
    return True


# ==============================================================================================
# Uncertainty + label prefill
# ==============================================================================================

def frame_uncertainty(confs):
    """(score, mean_conf_deficit, hesitation_count) for one frame's detection confidences."""
    confs = [float(c) for c in confs]
    if not confs:
        return NO_DETECTION_SCORE, NO_DETECTION_SCORE, 0
    deficit = sum(1.0 - c for c in confs) / len(confs)
    hes = sum(1 for c in confs if HESITATION_LO < c < HESITATION_HI)
    return deficit + HESITATION_WEIGHT * hes, deficit, hes


def yolo_pose_lines(result, min_conf=PREFILL_MIN_CONF):
    """Prefilled YOLO-pose label lines from one Results object -- byte-format-identical to
    pose_pseudolabel_dataset.py: class cx cy w h (px py v)*17, v = 2/1/0 at conf .5/.1."""
    lines = []
    if result.boxes is None or not len(result.boxes) or result.keypoints is None:
        return lines
    confs = result.boxes.conf.cpu().numpy()
    xywhn = result.boxes.xywhn.cpu().numpy()
    kxy = result.keypoints.xyn.cpu().numpy()
    kcf = result.keypoints.conf.cpu().numpy() if result.keypoints.conf is not None else None
    for p in range(len(confs)):
        if float(confs[p]) < min_conf:
            continue
        cx, cy, w, h = xywhn[p]
        parts = [f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"]
        for j in range(17):
            kc = float(kcf[p][j]) if kcf is not None else 0.0
            v_flag = 2 if kc > 0.5 else (1 if kc > 0.1 else 0)
            parts.append(f"{kxy[p][j, 0]:.6f} {kxy[p][j, 1]:.6f} {v_flag}")
        lines.append(" ".join(parts))
    return lines


def detections_summary(result):
    """Compact per-detection record for the queue JSON (owner eyeballs, doesn't parse)."""
    out = []
    if result.boxes is None or not len(result.boxes):
        return out
    confs = result.boxes.conf.cpu().numpy()
    xywhn = result.boxes.xywhn.cpu().numpy()
    kcf = (result.keypoints.conf.cpu().numpy()
           if result.keypoints is not None and result.keypoints.conf is not None else None)
    for p in range(len(confs)):
        out.append({"conf": round(float(confs[p]), 3),
                    "xywhn": [round(float(v), 4) for v in xywhn[p]],
                    "kpt_mean_conf": round(float(kcf[p].mean()), 3) if kcf is not None else None})
    return out


# ==============================================================================================
# Corpus walk + sources merge
# ==============================================================================================

def enumerate_videos(videos_root):
    vids = []
    for dirpath, _dirnames, filenames in os.walk(videos_root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                vids.append(os.path.join(dirpath, fn))
    return sorted(vids)


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0].replace(" ", "_")


def merge_sources(ds_dir, stem_to_source):
    """Merge stem -> source tags into <dataset>/sources.json (split_train_val.py input)."""
    path = os.path.join(ds_dir, "sources.json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    data.update(stem_to_source)
    os.makedirs(ds_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return path


# ==============================================================================================
# Main
# ==============================================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True,
                    help="trained student .pt (e.g. logs/diagnostics/pose_train/"
                         "pose2k_n_eff_phase2/weights/best.pt)")
    ap.add_argument("--videos-root", default=DEFAULT_CORPUS,
                    help="FULL corpus root (default the owner's PS5 capture drive)")
    ap.add_argument("--dataset", default=os.path.join("logs", "diagnostics", "pose_ds"),
                    help="pose dataset dir; queue + review land under it")
    ap.add_argument("--top-k", type=int, default=200, help="frames exported for review")
    ap.add_argument("--min-uncertainty", type=float, default=0.3,
                    help="queue floor; frames scoring below this are never candidates")
    ap.add_argument("--per-video", type=int, default=60,
                    help="frames sampled evenly across each clip")
    ap.add_argument("--max-videos", type=int, default=0, help="0 = all")
    ap.add_argument("--conf-floor", type=float, default=0.05,
                    help="inference conf threshold (low on purpose: the hesitation zone "
                         "and near-misses must be visible to the ranker)")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--source", default="owner",
                    help="provenance tag merged into sources.json for exported stems")
    ap.add_argument("--no-menu-filter", action="store_true",
                    help="score every sampled frame, including menu/loading art")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")

    # ---- Stage 0: CUDA preflight (ALWAYS first; task #69) ------------------------------------
    if not preflight_cuda_torch():
        return 2
    try:
        import cv2
    except Exception as exc:
        print(f"PREFLIGHT FAILED: opencv is not importable ({exc}). pip install opencv-python")
        return 2

    model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
    if not os.path.isfile(model_path):
        print(f"student model not found: {model_path} (train one with "
              "tools/training/pose_finetune_efficient.py first)")
        return 2
    if not os.path.isdir(args.videos_root):
        print(f"--videos-root does not exist: {args.videos_root}")
        return 2
    ds_dir = args.dataset if os.path.isabs(args.dataset) else os.path.join(ROOT, args.dataset)
    os.makedirs(ds_dir, exist_ok=True)

    classify_frame = None
    if not args.no_menu_filter:
        try:
            from no_meter_mode_prep import classify_frame  # cv2+numpy only, no torch
        except Exception as exc:
            print(f"menu filter unavailable ({exc}) -- scoring all sampled frames")

    from ultralytics import YOLO
    model = YOLO(model_path)

    vids = enumerate_videos(args.videos_root)
    if args.max_videos > 0:
        vids = vids[: args.max_videos]
    print(f"corpus: {len(vids)} videos under {args.videos_root}")
    print(f"sampling {args.per_video}/clip, conf-floor {args.conf_floor}, "
          f"menu filter {'OFF' if classify_frame is None else 'ON'}")

    # ---- pass 1: score every sampled gameplay frame ------------------------------------------
    cands = []
    scanned = menu_skipped = no_det = 0
    t0 = time.time()
    for vi, v in enumerate(vids):
        cap = cv2.VideoCapture(v)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        if not cap.isOpened() or total <= 0:
            cap.release()
            continue
        idxs = sorted({int(total * (k + 0.5) / args.per_video) for k in range(args.per_video)})
        for fi in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, fr = cap.read()
            if not ok:
                continue
            scanned += 1
            if classify_frame is not None:
                is_game, _metrics = classify_frame(fr)
                if not is_game:
                    menu_skipped += 1
                    continue
            r = model.predict(fr, verbose=False, conf=args.conf_floor, imgsz=args.imgsz)[0]
            confs = (r.boxes.conf.cpu().numpy().tolist()
                     if r.boxes is not None and len(r.boxes) else [])
            score, deficit, hes = frame_uncertainty(confs)
            no_det += not confs
            if score >= args.min_uncertainty:
                cands.append({"video": v, "frame_index": fi,
                              "time_s": round(fi / fps, 2),
                              "uncertainty": round(score, 4),
                              "mean_conf_deficit": round(deficit, 4),
                              "hesitation_count": hes,
                              "n_detections": len(confs)})
        cap.release()
        if (vi + 1) % 25 == 0:
            el = time.time() - t0
            print(f"  scan {vi + 1}/{len(vids)} videos  candidates {len(cands)}  "
                  f"({el:.0f}s, ~{el / (vi + 1) * (len(vids) - vi - 1):.0f}s left)")

    cands.sort(key=lambda c: -c["uncertainty"])
    top = cands[: args.top_k]
    print(f"scored {scanned} frames ({menu_skipped} menu-skipped, {no_det} zero-detection); "
          f"{len(cands)} above --min-uncertainty {args.min_uncertainty}; exporting top {len(top)}")

    # ---- pass 2: export overlays + clean frames + prefilled labels for the top-K -------------
    review_dir = os.path.join(ds_dir, "active_learning_review")
    clean_dir = os.path.join(review_dir, "clean")
    os.makedirs(review_dir, exist_ok=True)
    os.makedirs(clean_dir, exist_ok=True)

    rank_of = {(e["video"], e["frame_index"]): i + 1 for i, e in enumerate(top)}
    by_video = {}
    for e in top:
        by_video.setdefault(e["video"], []).append(e)
    sources_patch = {}
    for v, entries in by_video.items():
        cap = cv2.VideoCapture(v)
        for e in sorted(entries, key=lambda x: x["frame_index"]):
            cap.set(cv2.CAP_PROP_POS_FRAMES, e["frame_index"])
            ok, fr = cap.read()
            if not ok:
                e["export_error"] = "reread_failed"
                continue
            r = model.predict(fr, verbose=False, conf=args.conf_floor, imgsz=args.imgsz)[0]
            rank = rank_of[(v, e["frame_index"])]
            stem = f"{_stem(v)}_f{e['frame_index']:06d}"
            overlay = os.path.join(review_dir, f"q{rank:04d}_{stem}.jpg")
            try:
                annotated = r.plot()
            except Exception:
                annotated = fr
            cv2.imwrite(overlay, annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
            clean_stem = f"al_{stem}"
            clean_jpg = os.path.join(clean_dir, clean_stem + ".jpg")
            clean_txt = os.path.join(clean_dir, clean_stem + ".txt")
            cv2.imwrite(clean_jpg, fr, [cv2.IMWRITE_JPEG_QUALITY, 90])
            with open(clean_txt, "w", encoding="utf-8") as fh:
                lines = yolo_pose_lines(r)
                fh.write("\n".join(lines) + ("\n" if lines else ""))
            e.update(rank=rank, detections=detections_summary(r),
                     review_jpg=os.path.abspath(overlay),
                     clean_jpg=os.path.abspath(clean_jpg),
                     prefilled_label=os.path.abspath(clean_txt))
            sources_patch[clean_stem] = args.source
        cap.release()

    sources_path = merge_sources(ds_dir, sources_patch) if sources_patch else ""

    # ---- queue JSON ---------------------------------------------------------------------------
    queue_path = os.path.join(ds_dir, "active_learning_queue.json")
    queue = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": os.path.abspath(model_path),
        "videos_root": os.path.abspath(args.videos_root),
        "params": {"top_k": args.top_k, "min_uncertainty": args.min_uncertainty,
                   "per_video": args.per_video, "conf_floor": args.conf_floor,
                   "imgsz": args.imgsz, "menu_filter": classify_frame is not None,
                   "hesitation_zone": [HESITATION_LO, HESITATION_HI],
                   "hesitation_weight": HESITATION_WEIGHT,
                   "no_detection_score": NO_DETECTION_SCORE,
                   "prefill_min_conf": PREFILL_MIN_CONF},
        "totals": {"videos_scanned": len(vids), "frames_scored": scanned,
                   "frames_menu_skipped": menu_skipped, "frames_zero_detection": no_det,
                   "candidates": len(cands), "exported": len(top)},
        "queue": sorted(top, key=lambda e: e.get("rank", 10 ** 9)),
    }
    with open(queue_path, "w", encoding="utf-8") as fh:
        json.dump(queue, fh, indent=2)

    print(f"\nDONE in {(time.time() - t0) / 60.0:.1f} min")
    print(f"queue:   {queue_path}")
    print(f"review:  {review_dir}  (flip through q0001..q{len(top):04d} overlays)")
    print(f"clean:   {clean_dir}  (al_* jpg + prefilled txt, ready to hand-correct)")
    if sources_path:
        print(f"sources: {sources_path}  ({len(sources_patch)} al_ stems tagged '{args.source}')")
    print("next (NO training happened here):")
    print("  1. hand-correct the top-K labels in active_learning_review/clean/")
    print(f"  2. copy each al_*.jpg -> {os.path.join(ds_dir, 'images', 'train')}")
    print(f"     and each al_*.txt  -> {os.path.join(ds_dir, 'labels', 'train')}")
    print("  3. python tools/training/pose_finetune_efficient.py --boost-weight 3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
