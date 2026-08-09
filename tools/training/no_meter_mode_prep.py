#!/usr/bin/env python3
"""No-Meter Mode corpus orchestrator: owner recordings -> training-ready dataset + manifest.

Runs the four-stage No-Meter Mode data pipeline over the owner's gameplay drive
(see docs/NO_METER_MODE_TRAINING.md):

  0. CUDA torch preflight     - FAILS LOUD if torch.cuda.is_available() is False. The 2026-07-06
                                0/28 batch root cause was silent 8fps CPU inference; never again.
  1. Menu/junk clip filter    - cheap heuristics + optional player-detect gate; skips menus,
                                lobbies, loading screens, still photos before any GPU labeling.
  2. Teacher pseudo-labeling  - delegates to tools/training/pose_pseudolabel_dataset.py
                                (UNMODIFIED, one subprocess per kept clip) -> YOLO-pose txt lines.
  3. Release-cue label prep   - delegates to tools/training/prepare_release_cue_labels.py
                                (meter-anchored weak supervision for the five cues).
  4. Manifest                 - no_meter_mode_manifest.json: per-video kept/rejected/labeled
                                counts + artifact paths, the single input for training scripts.

Every frame is tagged with a --source ("owner" | "youtube") via <dataset>/sources.json
(keyed by video-stem prefix; the YOLO txt format itself stays untouched). The validation
split is enforced owner-only by tools/training/split_train_val.py.

This module is also the DEDICATED MENU FILTER: `classify_frame` / `scan_video` are importable
from other tools and depend only on cv2 + numpy (no torch). They extend the --max-scan
early-abort idea from pose_pseudolabel_dataset.py:41-42 into a decision made BEFORE labeling.

Usage:
  python tools/training/no_meter_mode_prep.py --videos-root "D:\\2k_recordings" \
      [--source owner] [--out logs/diagnostics/no_meter_prep] [--per-video 150] \
      [--min-conf 0.55] [--max-videos 0] [--sample-frames 9] [--no-player-gate] \
      [--skip-pseudolabel] [--skip-cue-labels] [--meter-color Purple] [--handedness Right]

WRITE-CODE-ONLY SCAFFOLD NOTE: nothing here trains a model; it prepares data + a manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".heic")

# --- Menu-filter thresholds (cheap wins > perfect classification; tune here) -------------------
DOMINANT_FRAC_MENU = 0.60   # one coarse HSV bin owning >60% of pixels = flat menu/loading art
VAL_MEAN_DARK = 28.0        # near-black frame = loading screen / fade
HUE_SPREAD_MIN = 8.0        # sat-weighted hue std below this = monochrome UI, not a court scene
MIN_GAMEPLAY_FRACTION = 0.34  # < 1/3 of sampled frames gameplay-like -> reject the clip
MIN_CLIP_FRAMES = 90        # < 1.5s @60fps carries no usable shot


# ==============================================================================================
# Menu / junk frame filter  (importable; cv2 + numpy only)
# ==============================================================================================

def classify_frame(frame_bgr):
    """Cheap gameplay-vs-menu heuristic for a single BGR frame.

    Returns (is_gameplay: bool, metrics: dict). Menus / loading screens / replay
    wipe cards are dominated by flat color regions, darkness, or monochrome UI;
    live court scenes have diverse hues and no single dominant color bin.
    """
    import cv2
    import numpy as np

    small = cv2.resize(frame_bgr, (160, 90), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(np.float32)
    s = hsv[:, :, 1].astype(np.float32)
    v = hsv[:, :, 2].astype(np.float32)

    val_mean = float(v.mean())

    # Dominant coarse-color fraction: 16 hue x 4 sat x 4 val bins.
    bins = (np.minimum(h / 180.0 * 16, 15).astype(np.int32) * 16
            + np.minimum(s / 256.0 * 4, 3).astype(np.int32) * 4
            + np.minimum(v / 256.0 * 4, 3).astype(np.int32))
    counts = np.bincount(bins.ravel(), minlength=256)
    dominant_frac = float(counts.max()) / float(bins.size)

    # Saturation-weighted hue spread: courts + jerseys + crowd = wide; flat UI = narrow.
    w = s / (s.sum() + 1e-6)
    hue_mean = float((h * w).sum())
    hue_spread = float(np.sqrt(((h - hue_mean) ** 2 * w).sum()))

    metrics = {"dominant_frac": round(dominant_frac, 3),
               "val_mean": round(val_mean, 1),
               "hue_spread": round(hue_spread, 1)}
    if val_mean < VAL_MEAN_DARK:
        return False, metrics
    if dominant_frac > DOMINANT_FRAC_MENU:
        return False, metrics
    if hue_spread < HUE_SPREAD_MIN:
        return False, metrics
    return True, metrics


def scan_video(path, sample_frames=9, player_check=None, player_gate_frames=3):
    """Clip-level keep/reject decision without labeling anything.

    Samples `sample_frames` frames evenly across the clip and votes with
    classify_frame. If `player_check` (callable frame->bool, e.g. a v9 Player-class
    gate) is given, the first `player_gate_frames` gameplay-like samples must
    contain at least one detected player or the clip is rejected -- the
    "no player in the first 3 sampled frames -> skip clip entirely" rule.

    Returns a verdict dict: {status: kept|rejected, reason, sampled, gameplay_frames, metrics}.
    """
    import cv2

    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not cap.isOpened() or total <= 0:
        cap.release()
        return {"status": "rejected", "reason": "unreadable", "sampled": 0, "gameplay_frames": 0}
    if total < MIN_CLIP_FRAMES:
        cap.release()
        return {"status": "rejected", "reason": "too_short", "sampled": 0, "gameplay_frames": 0,
                "total_frames": total}

    idxs = [int(total * (k + 0.5) / sample_frames) for k in range(sample_frames)]
    gameplay = 0
    sampled = 0
    checked_players = 0
    player_seen = False
    last_metrics = {}
    for fi in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok:
            continue
        sampled += 1
        is_game, last_metrics = classify_frame(fr)
        if is_game:
            gameplay += 1
            if player_check is not None and checked_players < player_gate_frames:
                checked_players += 1
                try:
                    if player_check(fr):
                        player_seen = True
                except Exception:
                    player_seen = True  # gate error must never eat a clip; fail open
    cap.release()

    if sampled == 0:
        return {"status": "rejected", "reason": "unreadable", "sampled": 0, "gameplay_frames": 0}
    if gameplay < max(1, int(round(sampled * MIN_GAMEPLAY_FRACTION))):
        return {"status": "rejected", "reason": "menu_like", "sampled": sampled,
                "gameplay_frames": gameplay, "metrics": last_metrics}
    if player_check is not None and checked_players > 0 and not player_seen:
        return {"status": "rejected", "reason": "no_player_found", "sampled": sampled,
                "gameplay_frames": gameplay}
    return {"status": "kept", "reason": "", "sampled": sampled, "gameplay_frames": gameplay}


def make_player_gate(model_path=None, conf=0.35):
    """Build a frame->bool Player-class gate from the v9 detector (torch path).

    Returns None if the model cannot be loaded, so callers degrade to heuristics-only.
    v9 classes: 0=Player 1=Stamina 2=Unplayable 3=Zbasketball (train_player_v9.py).
    """
    mp = model_path or os.path.join(ROOT, "models", "orion_player_detect_v9.pt")
    if not os.path.isfile(mp):
        return None
    try:
        from ultralytics import YOLO
    except Exception:
        return None
    model = YOLO(mp)

    def _gate(frame):
        r = model.predict(frame, verbose=False, conf=conf)[0]
        if r.boxes is None or not len(r.boxes):
            return False
        cls = r.boxes.cls.cpu().numpy().astype(int)
        return bool((cls == 0).any())

    return _gate


# ==============================================================================================
# CUDA preflight (task #69: fail EARLY and LOUD, never silently 8fps)
# ==============================================================================================

def preflight_cuda_torch():
    """Abort with the exact fix command unless CUDA torch + ultralytics are importable."""
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
    name = torch.cuda.get_device_name(0)
    print(f"preflight OK: torch {torch.__version__} CUDA={torch.version.cuda} device={name}")
    return True


# ==============================================================================================
# Orchestration
# ==============================================================================================

def _enumerate_files(videos_root):
    vids, images, other = [], [], []
    for dirpath, _dirnames, filenames in os.walk(videos_root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext in VIDEO_EXTS:
                vids.append(p)
            elif ext in IMAGE_EXTS:
                images.append(p)
            else:
                other.append(p)
    return sorted(vids), sorted(images), sorted(other)


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0].replace(" ", "_")


def _count_labels_for(ds_dir, base):
    """Count YOLO label files written for a given video stem (train + val)."""
    import glob as _glob
    n = 0
    for split in ("train", "val"):
        n += len(_glob.glob(os.path.join(ds_dir, "labels", split, _glob.escape(base) + "_*.txt")))
    return n


def _run_pseudolabel(video_path, ds_dir, args):
    """One pose_pseudolabel_dataset.py subprocess for one kept clip (script UNMODIFIED)."""
    import glob as _glob
    script = os.path.join(ROOT, "tools", "training", "pose_pseudolabel_dataset.py")
    cmd = [sys.executable, script,
           "--videos-glob", _glob.escape(video_path),
           "--max-videos", "1",
           "--per-video", str(args.per_video),
           "--min-conf", str(args.min_conf),
           "--out", ds_dir,
           "--val-frac", str(args.val_frac),
           "--teacher", args.teacher]
    if args.multi:
        cmd.append("--multi")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode not in (0, 1):  # 1 = "kept 0 frames", legitimate for a dud clip
        tail = (r.stdout or "").strip().splitlines()[-3:] + (r.stderr or "").strip().splitlines()[-3:]
        print(f"    pseudolabel subprocess rc={r.returncode}: {' | '.join(tail)}")
    return r.returncode


def _update_sources_json(ds_dir, stem_to_source):
    """Merge stem-prefix -> source tags into <dataset>/sources.json (split/val enforcement input)."""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos-root", required=True, help="root folder of the owner's gameplay drive")
    ap.add_argument("--source", default="owner", choices=["owner", "youtube"],
                    help="provenance tag stamped on every frame (validation is owner-only)")
    ap.add_argument("--out", default=os.path.join("logs", "diagnostics", "no_meter_prep"))
    ap.add_argument("--dataset-dir", default="", help="pose dataset dir (default <out>/pose_ds)")
    ap.add_argument("--teacher", default="yolov8x-pose.pt")
    ap.add_argument("--per-video", type=int, default=150)
    ap.add_argument("--min-conf", type=float, default=0.55)
    ap.add_argument("--val-frac", type=float, default=0.2,
                    help="passed through to pseudo-labeling; re-balanced later by split_train_val.py")
    ap.add_argument("--max-videos", type=int, default=0, help="0 = all")
    ap.add_argument("--sample-frames", type=int, default=9, help="menu-filter samples per clip")
    ap.add_argument("--no-player-gate", action="store_true",
                    help="menu filter: heuristics only, skip the v9 player-detect confirmation")
    ap.add_argument("--multi", action="store_true", help="label all players per frame (Park/Rec)")
    ap.add_argument("--fast", action="store_true",
                    help="Stage 2 via tools/training/fast_pseudolabel.py: one teacher load, "
                         "batched FP16 predict (imgsz 960, batch 16) instead of one subprocess "
                         "per clip. Identical output contract; the subprocess path stays the "
                         "fallback without this flag. See docs Stage 1b.")
    ap.add_argument("--meter-color", default="Purple")
    ap.add_argument("--handedness", default="Right")
    ap.add_argument("--skip-menu-filter", action="store_true")
    ap.add_argument("--skip-pseudolabel", action="store_true")
    ap.add_argument("--skip-cue-labels", action="store_true")
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

    vids, images, other = _enumerate_files(args.videos_root)
    if args.max_videos > 0:
        vids = vids[: args.max_videos]
    print(f"corpus: {len(vids)} videos, {len(images)} still images (skipped), "
          f"{len(other)} other files (ignored) under {args.videos_root}")

    per_video = []
    for p in images:
        per_video.append({"path": p, "status": "rejected", "reject_reason": "still_image",
                          "frames_labeled": 0})

    # Duplicate-stem guard: pose_pseudolabel_dataset stems are basename-derived; a duplicate
    # basename in another subfolder would silently overwrite frames.
    seen_stems = {}

    # ---- Stage 1: menu/junk filter ------------------------------------------------------------
    gate = None
    if not args.skip_menu_filter and not args.no_player_gate:
        gate = make_player_gate()
        print(f"player-detect gate: {'ON (orion_player_detect_v9)' if gate else 'unavailable -> heuristics only'}")

    kept = []
    t0 = time.time()
    for i, v in enumerate(vids):
        stem = _stem(v)
        if stem in seen_stems:
            per_video.append({"path": v, "status": "rejected",
                              "reject_reason": f"duplicate_basename_of:{seen_stems[stem]}",
                              "frames_labeled": 0})
            continue
        seen_stems[stem] = v
        if args.skip_menu_filter:
            verdict = {"status": "kept", "reason": "menu_filter_skipped"}
        else:
            verdict = scan_video(v, sample_frames=args.sample_frames, player_check=gate)
        row = {"path": v, "stem": stem, "status": verdict["status"],
               "reject_reason": verdict.get("reason", ""), "frames_labeled": 0}
        per_video.append(row)
        if verdict["status"] == "kept":
            kept.append(row)
        if (i + 1) % 50 == 0:
            print(f"  menu-filter {i + 1}/{len(vids)} "
                  f"(kept {len(kept)}, {time.time() - t0:.0f}s)")
    n_rej = sum(1 for r in per_video if r["status"] == "rejected")
    print(f"menu filter: kept {len(kept)}/{len(vids)} videos ({n_rej} rejected incl. stills)")

    # ---- Stage 2: teacher pseudo-labeling (delegated, script unmodified) ----------------------
    if not args.skip_pseudolabel:
        print(f"\npseudo-labeling {len(kept)} kept clips -> {ds_dir} "
              f"(teacher={args.teacher}, min-conf={args.min_conf}, per-video<={args.per_video}"
              f"{', FAST batched' if args.fast else ''})")
        if args.fast:
            # Single-process batched path (import-only; docs Stage 1b). Same outputs.
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            from fast_pseudolabel import pseudolabel_clips
            counts = pseudolabel_clips([row["path"] for row in kept], ds_dir,
                                       teacher=args.teacher, per_video=args.per_video,
                                       min_conf=args.min_conf, val_frac=args.val_frac,
                                       multi=args.multi)
            for row in kept:
                row["frames_labeled"] = counts.get(row["path"], 0)
        else:
            for i, row in enumerate(kept):
                before = _count_labels_for(ds_dir, row["stem"])
                _run_pseudolabel(row["path"], ds_dir, args)
                row["frames_labeled"] = _count_labels_for(ds_dir, row["stem"]) - before
                print(f"  [{i + 1}/{len(kept)}] {os.path.basename(row['path']):40.40} "
                      f"labeled {row['frames_labeled']}")
        _update_sources_json(ds_dir, {row["stem"]: args.source for row in kept})
    else:
        print("\npseudo-labeling skipped (--skip-pseudolabel)")

    # ---- Stage 3: release-cue label prep (delegated) ------------------------------------------
    cue_json = os.path.join(out_dir, "release_cue_labels.json")
    if not args.skip_cue_labels:
        list_path = os.path.join(out_dir, "kept_videos.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(row["path"] for row in kept) + "\n")
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
                for row in kept:
                    row["shots_cue_labeled"] = by_vid.get(os.path.basename(row["path"]), 0)
            except Exception:
                pass
    else:
        print("\ncue-label prep skipped (--skip-cue-labels)")

    # ---- Stage 4: manifest --------------------------------------------------------------------
    manifest = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "videos_root": os.path.abspath(args.videos_root),
        "source": args.source,
        "teacher": args.teacher,
        "min_conf": args.min_conf,
        "totals": {
            "videos_found": len(vids),
            "still_images_skipped": len(images),
            "videos_kept": len(kept),
            "videos_rejected": sum(1 for r in per_video if r["status"] == "rejected"),
            "frames_labeled": sum(r.get("frames_labeled", 0) for r in per_video),
            "shots_cue_labeled": sum(r.get("shots_cue_labeled", 0) for r in per_video),
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
        "per_video": per_video,
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
