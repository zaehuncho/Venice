#!/usr/bin/env python3
"""Fast pseudo-labeling for the No-Meter corpus: ONE teacher load, batched FP16 inference.

Drop-in replacement for Stage 2's per-clip subprocess fan-out
(no_meter_mode_prep.py -> pose_pseudolabel_dataset.py --max-videos 1). The fan-out
reloads the 62.7M-param teacher once per clip -- on the 1040-clip corpus that is
~1040 model loads (~50 min of pure load + CUDA-context overhead) and one-frame-at-
a-time predicts (a measured run cleared only ~101 clips in 30 min). This module
loads the teacher ONCE, filters and labels every clip in a single process, and
predicts in batches, with the IDENTICAL output contract:

  images/{train,val}/<stem>.jpg  labels/{train,val}/<stem>.txt  pose2k.yaml
  stem       = <video_basename_spaces_to_underscores>_<rawframeidx:06d>
  label line = "0 cx cy w h (px py v)*17"  normalized 0-1, 6 decimals, v in {0,1,2}
  <dataset>/sources.json    stem-prefix -> source sidecar (owner|youtube)
  <out>/no_meter_mode_manifest.json   same schema as no_meter_mode_prep.py
  train/val picked per written frame by a per-clip random.Random(1234), matching the
  one-subprocess-per-clip behavior exactly (rebalanced later by split_train_val.py).

Speed levers vs the fan-out (measured numbers in docs/NO_METER_MODE_TRAINING.md Stage 1b):
  - single teacher load; default teacher yolo26x-pose.pt (faster AND +7.2 AP over
    yolov8x-pose on COCO pose; both present in the repo root)
  - batched predict (--batch 16): ultralytics runs a python-list source as one
    forward pass of len(list) images (measured: +1.24x under FP16 on the 3060)
  - FP16 (--no-half to disable): measured 1.83x at imgsz 960 batch 16, parity vs
    fp32 = 0.15 px mean keypoint delta on the top player (92 vs 93 dets / 16 frames).
    TRAP (ultralytics 8.4): `half=` is a DEPRECATED NO-OP -- FP16 is requested via
    `quantize=16`, and the predictor freezes its precision at the FIRST predict()
    call of a YOLO instance, so every predict here passes the same kwargs. A warmup
    predict without quantize would silently lock the teacher to fp32 forever.
  - --imgsz 960 letterbox (players are large in a 1080p 2K frame; full 1920 is
    wasted compute). Labels are NORMALIZED against the RAW frame -- ultralytics
    divides xywhn / keypoints.xyn by Results.orig_shape, not the letterboxed
    tensor -- so imgsz never touches the label coordinate space. That assumption
    is ASSERTED at runtime (_verify_normalized), fail-loud if ultralytics changes.
  - --per-video 60 (fan-out default was 150): frames within a clip are highly
    correlated, so the marginal value of frames 61-150 is low; 60 x 1040 ~= 62k
    frames is still generous for a pose fine-tune and keeps Stage 4 training sane.
  - cheap decode: cap.grab() every frame, cap.retrieve() only on sampled frames
    (~12 fps equivalent), the pose_pseudolabel_dataset.py idiom.

Disk levers (the fan-out saved raw 1080p JPEGs: measured ~691 KB/frame = ~41 GB for
the full corpus, and C: has ~12 GB free):
  - default --out is D:\\VeniceTraining\\no_meter_prep (D: has ~892 GB free); --out /
    --dataset-dir stay overridable.
  - --save-imgsz 960 (default): the SAVED training jpg is downscaled so its longest
    side is <= this (we fine-tune at imgsz <= 640, so 1080p on disk is pure waste).
    SAFE because labels are normalized against the ORIGINAL frame the teacher saw and
    the resize is uniform on both axes -- normalized coords are resize-invariant.
    0 = keep native resolution. --jpeg-quality tunes the tradeoff further.
  - free-space preflight: before the teacher is loaded, the planned frame count is
    costed (~700 KB/frame at 1080p, scaled by (save_imgsz/1920)^2) + 30% headroom and
    the run FAILS LOUD if the target drive cannot hold it.

Menu filtering is IMPORTED from no_meter_mode_prep (classify_frame / scan_video /
make_player_gate) -- one implementation, two callers, never forked.

Resumable: every finished clip (kept or rejected) is appended to <out>/progress.jsonl;
a re-run with --resume (default ON) skips clips already recorded and folds their rows
into the final manifest. --no-resume ignores and overwrites the progress file.

Usage:
  python tools/training/fast_pseudolabel.py --videos-root "E:\\PS5\\CREATE\\Video Clips\\NBA 2K26" \
      [--teacher yolo26x-pose.pt] [--batch 16] [--imgsz 960] [--no-half] [--per-video 60] \
      [--out D:\\VeniceTraining\\no_meter_prep] [--save-imgsz 960] [--jpeg-quality 90] \
      [--max-videos 0] [--no-resume] [--cue-labels]

no_meter_mode_prep.py --fast routes its Stage 2 through pseudolabel_clips() below;
the old subprocess fan-out stays available as the fallback path.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Menu filter + preflight + sidecar helpers: IMPORTED, not forked (single implementation).
from no_meter_mode_prep import (  # noqa: E402  (cv2/numpy imported lazily inside)
    classify_frame,               # noqa: F401  (re-exported for API parity)
    scan_video,
    make_player_gate,
    preflight_cuda_torch,
    _enumerate_files,
    _stem,
    _update_sources_json,
)
# Keep pose2k.yaml byte-identical to the fan-out by reusing its flip index.
from pose_pseudolabel_dataset import COCO_FLIP_IDX  # noqa: E402

DEFAULT_TEACHER = "yolo26x-pose.pt"
DEFAULT_BATCH = 16
DEFAULT_IMGSZ = 960
DEFAULT_PER_VIDEO = 60
DEFAULT_OUT = r"D:\VeniceTraining\no_meter_prep"  # C: has ~12 GB free; the corpus needs more
DEFAULT_SAVE_IMGSZ = 960      # longest side of the SAVED jpg; labels are resize-invariant
DEFAULT_JPEG_QUALITY = 90     # the fan-out's cv2.IMWRITE_JPEG_QUALITY value
SAMPLE_FPS = 12.0  # ~12 sampled fps, decorrelates consecutive frames (fan-out idiom)

# Measured 2026-08-09 on a partial fan-out run: 9.99 GB / ~15,150 frames at native
# 1080p q90 => ~691 KB/frame. Scaled by saved-image area for the preflight estimate.
NATIVE_FRAME_KB = 700.0
NATIVE_LONG_SIDE = 1920.0
DISK_HEADROOM = 1.3

_NORM_VERIFIED = False


# ==============================================================================================
# Teacher + dataset scaffolding
# ==============================================================================================

def load_teacher(weights):
    """Load the pose teacher exactly once. CUDA is asserted HERE as well as in the
    preflight: this function is the last gate before GPU work starts."""
    import torch
    from ultralytics import YOLO
    assert torch.cuda.is_available(), (
        "torch.cuda.is_available() is False -- never run the teacher on CPU "
        "(2026-07-06 0/28 batch root cause). Run no_meter_mode_prep.py for the fix text.")
    return YOLO(weights)


def ensure_dataset_dirs(ds_dir):
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        os.makedirs(os.path.join(ds_dir, sub), exist_ok=True)


def estimate_frame_kb(save_imgsz):
    """~700 KB/frame measured at native 1080p q90, scaled by saved-image area."""
    kb = NATIVE_FRAME_KB
    if save_imgsz and save_imgsz < NATIVE_LONG_SIDE:
        kb *= (save_imgsz / NATIVE_LONG_SIDE) ** 2
    return kb


def preflight_disk_space(ds_dir, frames_planned, save_imgsz):
    """Fail loud BEFORE the teacher loads if the dataset drive cannot hold the run.

    The 1080p fan-out output measured 691 KB/frame -> ~41 GB for 62,400 frames, and
    C: had ~12 GB free: an overnight run would have died partway. Cost the plan up
    front (+30% headroom for labels/variance) against the target drive instead.
    """
    import shutil
    need = frames_planned * estimate_frame_kb(save_imgsz) * 1024.0 * DISK_HEADROOM
    free = shutil.disk_usage(os.path.abspath(ds_dir)).free
    line = (f"disk preflight: ~{need / 2**30:.1f} GiB needed for <= {frames_planned} frames "
            f"at save-imgsz {save_imgsz or 'native'} (+{int((DISK_HEADROOM - 1) * 100)}% headroom), "
            f"{free / 2**30:.1f} GiB free on {os.path.splitdrive(os.path.abspath(ds_dir))[0]}\\")
    if free < need:
        print("PREFLIGHT FAILED: " + line)
        print("  fix: point --out/--dataset-dir at a bigger drive (D: has the space), or "
              "lower --per-video / --save-imgsz (e.g. --save-imgsz 640 ~= x0.44 the bytes).")
        return False
    print(line + " -- OK")
    return True


def write_dataset_yaml(ds_dir):
    """Identical content to pose_pseudolabel_dataset.py's yaml (written early so a
    crash mid-run still leaves a loadable dataset)."""
    yaml = os.path.join(ds_dir, "pose2k.yaml")
    with open(yaml, "w") as fh:
        fh.write(
            f"path: {os.path.abspath(ds_dir)}\n"
            "train: images/train\n"
            "val: images/val\n"
            "kpt_shape: [17, 3]\n"
            f"flip_idx: {COCO_FLIP_IDX}\n"
            "names:\n  0: person\n"
        )
    return yaml


def _verify_normalized(result, frame):
    """Prove the imgsz-independence assumption once per run, fail-loud otherwise.

    xywhn / keypoints.xyn are normalized against Results.orig_shape (the raw decoded
    frame), NOT the letterboxed --imgsz inference tensor. If that ever stops holding
    (ultralytics behavior change), labels would silently land in the wrong coordinate
    space -- so the first labeled frame of every run re-checks it.
    """
    global _NORM_VERIFIED
    if _NORM_VERIFIED:
        return
    oh, ow = int(result.orig_shape[0]), int(result.orig_shape[1])
    fh, fw = frame.shape[:2]
    if (oh, ow) != (fh, fw):
        raise AssertionError(
            f"ultralytics orig_shape {(oh, ow)} != decoded frame {(fh, fw)}: normalized "
            "labels would be in the wrong coordinate space. Do not trust this dataset.")
    b = result.boxes.xywhn.cpu().numpy()
    if b.size and (float(b.min()) < -1e-3 or float(b.max()) > 1.0 + 1e-3):
        raise AssertionError(
            f"xywhn outside [0,1] (min={float(b.min()):.4f} max={float(b.max()):.4f}): "
            "not normalized against the raw frame. Do not trust this dataset.")
    _NORM_VERIFIED = True


# ==============================================================================================
# Batched per-clip labeler (port of pose_pseudolabel_dataset.py's loop, same outputs)
# ==============================================================================================

def label_clip(teacher, video_path, ds_dir, *, per_video=DEFAULT_PER_VIDEO, min_conf=0.55,
               val_frac=0.2, multi=False, batch=DEFAULT_BATCH, imgsz=DEFAULT_IMGSZ,
               half=True, max_scan=1200, save_imgsz=DEFAULT_SAVE_IMGSZ,
               jpeg_quality=DEFAULT_JPEG_QUALITY):
    """Label one clip with the already-loaded teacher; returns frames labeled.

    Output-identical to one pose_pseudolabel_dataset.py subprocess run on the same
    detections: same stems (raw frame index), same 6-decimal normalized label lines,
    same per-frame train/val draw from a fresh Random(1234). Differences by design:
    predict() runs on up to `batch` sampled frames at once (frames past the per-video
    cap inside the final batch are dropped unwritten -- the first `per_video`
    qualifying frames in scan order are written either way), and the SAVED jpg is
    downscaled to `save_imgsz` longest-side (labels are normalized against the
    original frame and the resize is uniform, so the coordinates are unaffected).
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    if total <= 0:
        cap.release()
        return 0
    step = max(1, int(round(fps / SAMPLE_FPS)))
    base = os.path.splitext(os.path.basename(video_path))[0].replace(" ", "_")
    rng = random.Random(1234)  # fresh per clip == one-subprocess-per-clip behavior
    labeled = 0
    scanned = 0
    i = 0
    buf_frames, buf_idx = [], []

    # ultralytics 8.4: FP16 = quantize=16 (half= is a deprecated no-op). Precision is
    # frozen at the FIRST predict() of the instance -- keep kwargs identical every call.
    predict_kw = dict(verbose=False, conf=min_conf, imgsz=imgsz, device=0)
    if half:
        predict_kw["quantize"] = 16

    def flush():
        nonlocal labeled
        if not buf_frames:
            return
        results = teacher.predict(buf_frames, **predict_kw)
        for fr, fi, r in zip(buf_frames, buf_idx, results):
            if labeled >= per_video:
                break
            if r.boxes is None or not len(r.boxes):
                continue
            _verify_normalized(r, fr)
            confs = r.boxes.conf.cpu().numpy()
            picks = ([p for p in range(len(confs)) if confs[p] >= min_conf]
                     if multi else [int(np.argmax(confs))])
            picks = [p for p in picks if confs[p] >= min_conf]
            if not picks:
                continue
            xywhn = r.boxes.xywhn.cpu().numpy()
            kxy_all = r.keypoints.xyn.cpu().numpy()           # (N,17,2)
            kcf_all = r.keypoints.conf.cpu().numpy()          # (N,17)
            lines = []
            for p in picks:
                cx, cy, w, h = xywhn[p]
                parts = [f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"]
                for j in range(17):
                    kc = kcf_all[p][j]
                    v_flag = 2 if kc > 0.5 else (1 if kc > 0.1 else 0)
                    parts.append(f"{kxy_all[p][j, 0]:.6f} {kxy_all[p][j, 1]:.6f} {v_flag}")
                lines.append(" ".join(parts))
            split = "val" if rng.random() < val_frac else "train"
            stem = f"{base}_{fi:06d}"
            # Downscale the SAVED image only (labels stay normalized to the original
            # frame; a uniform resize of both axes leaves normalized coords valid).
            out_fr = fr
            long_side = max(fr.shape[:2])
            if save_imgsz and long_side > save_imgsz:
                sc = save_imgsz / float(long_side)
                out_fr = cv2.resize(fr, (max(1, int(round(fr.shape[1] * sc))),
                                         max(1, int(round(fr.shape[0] * sc)))),
                                    interpolation=cv2.INTER_AREA)
            cv2.imwrite(os.path.join(ds_dir, "images", split, stem + ".jpg"), out_fr,
                        [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            with open(os.path.join(ds_dir, "labels", split, stem + ".txt"), "w") as fh:
                fh.write("\n".join(lines) + "\n")
            labeled += 1
        buf_frames.clear()
        buf_idx.clear()

    while labeled < per_video and scanned < max_scan:
        if not cap.grab():          # cheap: decode headers only, no frame conversion
            break
        if i % step == 0:
            ok, fr = cap.retrieve()  # full decode only on sampled frames
            if not ok:
                break
            scanned += 1
            buf_frames.append(fr)
            buf_idx.append(i)
            if len(buf_frames) >= batch:
                flush()
        i += 1
    flush()
    cap.release()
    return labeled


def _fmt_hms(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pseudolabel_clips(paths, ds_dir, *, teacher=DEFAULT_TEACHER, per_video=DEFAULT_PER_VIDEO,
                      min_conf=0.55, val_frac=0.2, multi=False, batch=DEFAULT_BATCH,
                      imgsz=DEFAULT_IMGSZ, half=True, max_scan=1200, progress_every=10,
                      save_imgsz=DEFAULT_SAVE_IMGSZ, jpeg_quality=DEFAULT_JPEG_QUALITY):
    """Single-teacher-load batched replacement for the per-clip subprocess fan-out.

    This is the entry point no_meter_mode_prep.py --fast routes through: takes the
    already-menu-filtered kept paths, returns {video_path: frames_labeled}.
    """
    ensure_dataset_dirs(ds_dir)
    write_dataset_yaml(ds_dir)
    if not preflight_disk_space(ds_dir, len(paths) * per_video, save_imgsz):
        raise SystemExit(2)
    model = load_teacher(teacher)
    counts = {}
    frames_total = 0
    t0 = time.time()
    for n, p in enumerate(paths, 1):
        counts[p] = label_clip(model, p, ds_dir, per_video=per_video, min_conf=min_conf,
                               val_frac=val_frac, multi=multi, batch=batch, imgsz=imgsz,
                               half=half, max_scan=max_scan, save_imgsz=save_imgsz,
                               jpeg_quality=jpeg_quality)
        frames_total += counts[p]
        if n % progress_every == 0 or n == len(paths):
            el = time.time() - t0
            rate = n / el * 60.0 if el > 0 else 0.0
            eta = (len(paths) - n) / (n / el) if n else 0.0
            print(f"  [{n}/{len(paths)}] frames {frames_total} | {rate:.1f} clips/min "
                  f"| elapsed {_fmt_hms(el)} | eta {_fmt_hms(eta)}", flush=True)
    return counts


# ==============================================================================================
# Standalone orchestration (menu filter -> batched labeling -> manifest), resumable
# ==============================================================================================

def _load_progress(path):
    """{abspath: row} from progress.jsonl (tolerates a torn final line from a crash)."""
    done = {}
    if not os.path.isfile(path):
        return done
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue  # torn tail line from a mid-write crash
            if row.get("path"):
                done[os.path.abspath(row["path"])] = row
    return done


def _manifest_row(row):
    """Strip bookkeeping keys so per_video rows match no_meter_mode_prep.py exactly."""
    keep = ("path", "stem", "status", "reject_reason", "frames_labeled", "shots_cue_labeled")
    return {k: row[k] for k in keep if k in row}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos-root", required=True, help="root folder of the owner's gameplay drive")
    ap.add_argument("--source", default="owner", choices=["owner", "youtube"],
                    help="provenance tag stamped on every frame (validation is owner-only)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="output root (default on D:; C: cannot hold the corpus)")
    ap.add_argument("--dataset-dir", default="", help="pose dataset dir (default <out>/pose_ds)")
    ap.add_argument("--save-imgsz", type=int, default=DEFAULT_SAVE_IMGSZ,
                    help="longest side of the SAVED training jpg (0 = native). Labels are "
                         "normalized to the original frame, so this never skews coordinates")
    ap.add_argument("--jpeg-quality", type=int, default=DEFAULT_JPEG_QUALITY)
    ap.add_argument("--teacher", default=DEFAULT_TEACHER,
                    help="pose teacher weights (yolo26x-pose: faster + more accurate than yolov8x)")
    ap.add_argument("--per-video", type=int, default=DEFAULT_PER_VIDEO,
                    help="max labeled frames per clip (60: in-clip frames are correlated; "
                         "60 x 1040 ~= 62k frames is generous for a pose fine-tune)")
    ap.add_argument("--min-conf", type=float, default=0.55)
    ap.add_argument("--val-frac", type=float, default=0.2,
                    help="passed to the per-frame split draw; re-balanced by split_train_val.py")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help="frames per teacher.predict() call (the single biggest win)")
    ap.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ,
                    help="teacher letterbox size; labels stay normalized to the raw frame")
    ap.add_argument("--no-half", action="store_true", help="disable FP16 inference")
    ap.add_argument("--max-scan", type=int, default=1200,
                    help="stop scanning a clip after this many SAMPLED frames")
    ap.add_argument("--max-videos", type=int, default=0, help="0 = all")
    ap.add_argument("--sample-frames", type=int, default=9, help="menu-filter samples per clip")
    ap.add_argument("--no-player-gate", action="store_true",
                    help="menu filter: heuristics only, skip the v9 player-detect confirmation")
    ap.add_argument("--skip-menu-filter", action="store_true")
    ap.add_argument("--multi", action="store_true", help="label all players per frame (Park/Rec)")
    ap.add_argument("--resume", dest="resume", action="store_true", default=True,
                    help="skip clips already recorded in <out>/progress.jsonl (default)")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="ignore + overwrite the progress file, redo everything")
    ap.add_argument("--progress-every", type=int, default=10,
                    help="print the live progress/ETA line every N clips")
    ap.add_argument("--cue-labels", action="store_true",
                    help="also run Stage 3 (prepare_release_cue_labels.py) on the kept clips; "
                         "OFF by default -- it is a separate, slower pass")
    ap.add_argument("--meter-color", default="Purple")
    ap.add_argument("--handedness", default="Right")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")

    # ---- Stage 0: CUDA preflight (ALWAYS first; task #69) ------------------------------------
    if not preflight_cuda_torch():
        return 2
    try:
        import cv2  # noqa: F401
    except Exception as exc:
        print(f"PREFLIGHT FAILED: opencv is not importable ({exc}). pip install opencv-python")
        return 2
    if not os.path.isdir(args.videos_root):
        print(f"--videos-root does not exist: {args.videos_root}")
        return 2

    out_dir = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    ds_dir = args.dataset_dir or os.path.join(out_dir, "pose_ds")
    os.makedirs(out_dir, exist_ok=True)
    ensure_dataset_dirs(ds_dir)
    write_dataset_yaml(ds_dir)  # early: a crash mid-run still leaves a loadable dataset

    vids, images, other = _enumerate_files(args.videos_root)
    if args.max_videos > 0:
        vids = vids[: args.max_videos]
    print(f"corpus: {len(vids)} videos, {len(images)} still images (skipped), "
          f"{len(other)} other files (ignored) under {args.videos_root}")

    progress_path = os.path.join(out_dir, "progress.jsonl")
    done = _load_progress(progress_path) if args.resume else {}
    if done:
        print(f"resume: {len(done)} clips already in {progress_path} will be skipped")

    # Disk preflight BEFORE any model load: cost the remaining clips against the drive.
    frames_planned = max(0, len(vids) - len(done)) * args.per_video
    if not preflight_disk_space(ds_dir, frames_planned, args.save_imgsz):
        return 2
    prog_fh = open(progress_path, "a" if args.resume else "w", encoding="utf-8")

    per_video = []
    for p in images:
        per_video.append({"path": p, "status": "rejected", "reject_reason": "still_image",
                          "frames_labeled": 0})

    gate = None
    if not args.skip_menu_filter and not args.no_player_gate:
        gate = make_player_gate()
        print(f"player-detect gate: {'ON (orion_player_detect_v9)' if gate else 'unavailable -> heuristics only'}")

    # ---- load the teacher ONCE (this is the whole point) -------------------------------------
    t_load = time.time()
    teacher = load_teacher(args.teacher)
    print(f"teacher={args.teacher} loaded once in {time.time() - t_load:.1f}s "
          f"(batch={args.batch}, imgsz={args.imgsz}, half={not args.no_half}, "
          f"per-video<={args.per_video}, min-conf={args.min_conf})")

    # ---- per-clip loop: menu filter -> batched labeling, progress + resume -------------------
    seen_stems = {}
    kept_rows = []
    n_frames = 0
    n_done_prev = 0
    t0 = time.time()
    for i, v in enumerate(vids):
        row = done.get(os.path.abspath(v))
        if row is not None:                      # resumed from a previous run
            stem = row.get("stem") or _stem(v)
            seen_stems.setdefault(stem, v)
            per_video.append(row)
            if row.get("status") == "kept":
                kept_rows.append(row)
                n_frames += row.get("frames_labeled", 0)
            n_done_prev += 1
            continue

        stem = _stem(v)
        if stem in seen_stems:
            row = {"path": v, "status": "rejected",
                   "reject_reason": f"duplicate_basename_of:{seen_stems[stem]}",
                   "frames_labeled": 0}
        else:
            seen_stems[stem] = v
            if args.skip_menu_filter:
                verdict = {"status": "kept", "reason": "menu_filter_skipped"}
            else:
                verdict = scan_video(v, sample_frames=args.sample_frames, player_check=gate)
            row = {"path": v, "stem": stem, "status": verdict["status"],
                   "reject_reason": verdict.get("reason", ""), "frames_labeled": 0}
            if row["status"] == "kept":
                row["frames_labeled"] = label_clip(
                    teacher, v, ds_dir, per_video=args.per_video, min_conf=args.min_conf,
                    val_frac=args.val_frac, multi=args.multi, batch=args.batch,
                    imgsz=args.imgsz, half=not args.no_half, max_scan=args.max_scan,
                    save_imgsz=args.save_imgsz, jpeg_quality=args.jpeg_quality)
                _update_sources_json(ds_dir, {stem: args.source})
        per_video.append(row)
        if row["status"] == "kept":
            kept_rows.append(row)
            n_frames += row["frames_labeled"]
        prog_fh.write(json.dumps(_manifest_row(row)) + "\n")
        prog_fh.flush()

        n_new = i + 1 - n_done_prev              # clips actually processed this session
        if n_new > 0 and (n_new % args.progress_every == 0 or i + 1 == len(vids)):
            el = time.time() - t0
            rate = n_new / el * 60.0 if el > 0 else 0.0
            remaining = len(vids) - (i + 1)
            eta = remaining / (n_new / el) if n_new and el > 0 else 0.0
            print(f"  [{i + 1}/{len(vids)}] kept {len(kept_rows)} | frames {n_frames} "
                  f"| {rate:.1f} clips/min | elapsed {_fmt_hms(el)} | eta {_fmt_hms(eta)}",
                  flush=True)
    prog_fh.close()

    n_rej = sum(1 for r in per_video if r.get("status") == "rejected")
    print(f"labeling done: kept {len(kept_rows)}/{len(vids)} videos ({n_rej} rejected incl. "
          f"stills), {n_frames} frames labeled in {_fmt_hms(time.time() - t0)}")
    _update_sources_json(ds_dir, {r.get("stem") or _stem(r["path"]): args.source
                                  for r in kept_rows})

    # ---- Stage 3 (optional): release-cue label prep, delegated unchanged ---------------------
    cue_json = os.path.join(out_dir, "release_cue_labels.json")
    list_path = os.path.join(out_dir, "kept_videos.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(r["path"] for r in kept_rows) + "\n")
    if args.cue_labels:
        script = os.path.join(ROOT, "tools", "training", "prepare_release_cue_labels.py")
        cmd = [sys.executable, script, "--videos-list", list_path, "--out", cue_json,
               "--source", args.source, "--meter-color", args.meter_color,
               "--handedness", args.handedness]
        print(f"\ncue-label prep: {' '.join(cmd[2:])}")
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print(f"cue-label prep exited rc={r.returncode} (manifest still written)")
        if os.path.isfile(cue_json):
            try:
                with open(cue_json, "r", encoding="utf-8") as fh:
                    cue_data = json.load(fh)
                by_vid = {}
                for s in cue_data.get("shots", []):
                    by_vid[s.get("video", "")] = by_vid.get(s.get("video", ""), 0) + 1
                for row in kept_rows:
                    row["shots_cue_labeled"] = by_vid.get(os.path.basename(row["path"]), 0)
            except Exception:
                pass
    else:
        print(f"\ncue-label prep skipped (opt in with --cue-labels; kept list: {list_path})")

    # ---- manifest (same schema + path as no_meter_mode_prep.py) ------------------------------
    per_video_out = [_manifest_row(r) for r in per_video]
    manifest = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "videos_root": os.path.abspath(args.videos_root),
        "source": args.source,
        "teacher": args.teacher,
        "min_conf": args.min_conf,
        "totals": {
            "videos_found": len(vids),
            "still_images_skipped": len(images),
            "videos_kept": len(kept_rows),
            "videos_rejected": sum(1 for r in per_video_out if r["status"] == "rejected"),
            "frames_labeled": sum(r.get("frames_labeled", 0) for r in per_video_out),
            "shots_cue_labeled": sum(r.get("shots_cue_labeled", 0) for r in per_video_out),
        },
        "artifacts": {
            "pose_dataset_dir": os.path.abspath(ds_dir),
            "pose_dataset_yaml": os.path.abspath(os.path.join(ds_dir, "pose2k.yaml")),
            "sources_json": os.path.abspath(os.path.join(ds_dir, "sources.json")),
            "cue_labels_json": os.path.abspath(cue_json),
        },
        "next_steps": [
            "python tools/training/split_train_val.py --dataset " + os.path.abspath(ds_dir)
            + " --val-source-allowlist owner",
            "python tools/training/pose_finetune.py --data "
            + os.path.abspath(os.path.join(ds_dir, "pose2k.yaml")),
        ],
        "per_video": per_video_out,
    }
    mpath = os.path.join(out_dir, "no_meter_mode_manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    t = manifest["totals"]
    print(f"\nDONE: kept {t['videos_kept']}/{t['videos_found']} videos, "
          f"{t['frames_labeled']} frames pseudo-labeled, {t['shots_cue_labeled']} shots cue-labeled")
    print(f"manifest: {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
