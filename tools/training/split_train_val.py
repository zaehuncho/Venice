#!/usr/bin/env python3
"""Rebuild the train/val split of the No-Meter pose dataset with a source allowlist.

CRITICAL INVARIANT: validation frames must be the OWNER'S OWN GAMEPLAY. YouTube
augmentation frames (source=youtube) are train-only -- a val set contaminated with
YouTube footage would grade the student on streamer overlays, face-cams, and builds
the owner never plays. This script enforces that with --val-source-allowlist
(default: owner). Frames whose source cannot be established are treated as
"unknown" and are ALSO kept out of val (fail closed).

Provenance comes from <dataset>/sources.json (written by no_meter_mode_prep.py and
youtube_augment.py): {"<video_stem>": "owner" | "youtube", ...}. Frame stems are
"<video_stem>_<frameidx:06d>" (pose_pseudolabel_dataset.py format, unchanged).

The split is made at VIDEO granularity by default (--group-by video): frames of one
clip are heavily correlated, so a frame-level split would leak train footage into
val and overstate the student. Files are MOVED between images/{train,val} +
labels/{train,val}; the YOLO txt contents and pose2k.yaml are untouched.

Usage:
  python tools/training/split_train_val.py --dataset logs/diagnostics/no_meter_prep/pose_ds \
      [--val-frac 0.2] [--val-source-allowlist owner] [--group-by video|frame] \
      [--seed 1234] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_sources(dataset_dir):
    path = os.path.join(dataset_dir, "sources.json")
    if not os.path.isfile(path):
        print(f"WARNING: {path} not found -- every frame will be treated as source=unknown\n"
              "         (unknown frames are excluded from val; run no_meter_mode_prep.py to tag them)")
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def collect_frames(dataset_dir):
    """[(stem, split, img_path, lbl_path)] across both splits."""
    frames = []
    for split in ("train", "val"):
        for img in sorted(glob.glob(os.path.join(dataset_dir, "images", split, "*.jpg"))):
            stem = os.path.splitext(os.path.basename(img))[0]
            lbl = os.path.join(dataset_dir, "labels", split, stem + ".txt")
            frames.append((stem, split, img, lbl if os.path.isfile(lbl) else None))
    return frames


def video_key(stem):
    """'<video_stem>_<frameidx:06d>' -> '<video_stem>' (frame idx is the last _-token)."""
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def source_of(stem, sources):
    key = video_key(stem)
    if key in sources:
        return sources[key]
    # tolerate stems whose video name itself ends in digits: try longest matching prefix
    best = ""
    for k in sources:
        if stem.startswith(k + "_") and len(k) > len(best):
            best = k
    return sources[best] if best else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True,
                    help="dataset root containing images/{train,val} + labels/{train,val}")
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--val-source-allowlist", nargs="+", default=["owner"],
                    help="only frames from these sources may enter val (default: owner)")
    ap.add_argument("--group-by", choices=["video", "frame"], default="video",
                    help="video (default) avoids same-clip train/val leakage")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--dry-run", action="store_true", help="report the moves, touch nothing")
    args = ap.parse_args()

    ds = args.dataset if os.path.isabs(args.dataset) else os.path.join(ROOT, args.dataset)
    if not os.path.isdir(os.path.join(ds, "images", "train")):
        print(f"not a pose dataset dir (no images/train): {ds}")
        return 2

    sources = load_sources(ds)
    frames = collect_frames(ds)
    if not frames:
        print("no frames found")
        return 2

    allow = set(args.val_source_allowlist)
    per_frame_src = {stem: source_of(stem, sources) for stem, _s, _i, _l in frames}

    # ---- pick the val membership ---------------------------------------------------------------
    rng = random.Random(args.seed)
    total = len(frames)
    target_val = int(round(total * args.val_frac))
    val_stems = set()

    if args.group_by == "video":
        groups = {}
        for stem, _split, _img, _lbl in frames:
            groups.setdefault(video_key(stem), []).append(stem)
        eligible = [k for k in groups if per_frame_src[groups[k][0]] in allow]
        rng.shuffle(eligible)
        acc = 0
        for k in eligible:
            if acc >= target_val:
                break
            val_stems.update(groups[k])
            acc += len(groups[k])
        blocked_groups = len(groups) - len(eligible)
    else:
        eligible = [stem for stem, _s, _i, _l in frames if per_frame_src[stem] in allow]
        rng.shuffle(eligible)
        val_stems = set(eligible[:target_val])
        blocked_groups = 0

    # ---- apply ---------------------------------------------------------------------------------
    moved = evicted = 0
    counts = {"train": {}, "val": {}}
    for stem, split, img, lbl in frames:
        want = "val" if stem in val_stems else "train"
        src = per_frame_src[stem]
        if want == "val" and src not in allow:  # belt & braces: never let a non-allowlisted in
            want = "train"
        if split == "val" and want == "train" and src not in allow:
            evicted += 1
        counts[want][src] = counts[want].get(src, 0) + 1
        if want != split:
            moved += 1
            if not args.dry_run:
                os.replace(img, os.path.join(ds, "images", want, os.path.basename(img)))
                if lbl:
                    os.replace(lbl, os.path.join(ds, "labels", want, os.path.basename(lbl)))

    n_val = sum(counts["val"].values())
    n_train = sum(counts["train"].values())
    tag = "(dry-run, nothing moved)" if args.dry_run else ""
    print(f"split: {n_train} train / {n_val} val of {total} frames "
          f"(target val {target_val}); {moved} files moved {tag}")
    for split in ("train", "val"):
        by = ", ".join(f"{k}={v}" for k, v in sorted(counts[split].items())) or "-"
        print(f"  {split:5}: {by}")
    if evicted:
        print(f"  evicted {evicted} non-allowlisted frames that were sitting in val")
    if blocked_groups:
        print(f"  {blocked_groups} video group(s) barred from val by allowlist {sorted(allow)}")

    bad_val = [k for k in counts["val"] if k not in allow]
    if bad_val:
        print(f"ERROR: val still contains disallowed sources {bad_val} -- this is a bug, aborting")
        return 3
    if n_val == 0:
        print("WARNING: val is EMPTY -- no allowlisted (owner) frames available. "
              "Pseudo-label owner footage before training; do NOT relax the allowlist to youtube.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
