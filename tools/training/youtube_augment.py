#!/usr/bin/env python3
"""YouTube augmentation shim for the No-Meter pose dataset. DEFAULT DISABLED.

DEFERRED / SECONDARY DATA SOURCE. The owner's own recordings are the primary
corpus; this exists only to add JUMP-SHOT ANIMATION VARIETY from builds/animations
the owner doesn't play. It is NOT for temporal-head (cue) training: YouTube footage
has no controlled meter setup, so it can never produce release-cue ground truth --
it feeds the POSE student only, always as train data, never validation
(split_train_val.py --val-source-allowlist owner enforces this downstream).

What it does when explicitly enabled with --enable:
  1. Downloads each URL at highest quality via yt-dlp.
  2. Samples frames like pose_pseudolabel_dataset.py (~12 sampled fps, per-video
     cap, early scan abort) -- same YOLO-pose txt line format, same dataset layout.
  3. Applies a HARDER teacher confidence threshold: 0.75 (vs 0.55 for owner data),
     because compression artifacts + overlays make the teacher less trustworthy.
  4. Blacks out the TOP-RIGHT QUADRANT of every frame BEFORE labeling and saving
     (the usual streamer face-cam location) so the teacher can never label the
     streamer, and the saved image matches its label.
  5. Writes frames into images/train + labels/train ONLY, stems prefixed "yt_",
     and tags them source=youtube in <dataset>/sources.json.

Without --enable the script prints this policy and exits 0 (safe to wire into
automation now, activate later).

Usage:
  python tools/training/youtube_augment.py --enable \
      --urls "https://youtube.com/watch?v=..." [more urls] \
      [--urls-file yt_urls.txt] [--out logs/diagnostics/no_meter_prep/pose_ds] \
      [--teacher yolov8x-pose.pt] [--min-conf 0.75] [--per-video 150]
"""
from __future__ import annotations

import argparse
import json
import os
import re

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

YT_MIN_CONF_DEFAULT = 0.75   # harder than the 0.55 owner-data default; do not soften
OWNER_MIN_CONF = 0.55        # documented for contrast only


def blackout_facecam(frame):
    """Zero the top-right quadrant in place (streamer face-cam location) and return it."""
    H, W = frame.shape[:2]
    frame[0:H // 2, W // 2:W] = 0
    return frame


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name).replace(" ", "_")


def update_sources_json(ds_dir, stem_to_source):
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


def download(url, dl_dir):
    """Fetch one URL at highest quality; returns the local file path or None."""
    import yt_dlp
    opts = {
        "format": "bestvideo[ext=mp4]/bestvideo/best",
        "outtmpl": os.path.join(dl_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def label_video(video_path, teacher, ds_dir, per_video, min_conf, max_scan):
    """pose_pseudolabel_dataset.py sampling loop + face-cam blackout; train split only."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    if total <= 0:
        cap.release()
        return 0
    step = max(1, int(round(fps / 12.0)))  # ~12 sampled fps (decorrelate)
    base = "yt_" + sanitize(os.path.splitext(os.path.basename(video_path))[0])
    labeled = i = scanned = 0
    while labeled < per_video and scanned < max_scan:
        if not cap.grab():
            break
        if i % step == 0:
            scanned += 1
            ok, fr = cap.retrieve()
            if not ok:
                break
            fr = blackout_facecam(fr)
            r = teacher.predict(fr, verbose=False, conf=min_conf)[0]
            if r.boxes is not None and len(r.boxes):
                confs = r.boxes.conf.cpu().numpy()
                p = int(np.argmax(confs))
                if confs[p] >= min_conf:
                    cx, cy, w, h = r.boxes.xywhn.cpu().numpy()[p]
                    kxy = r.keypoints.xyn.cpu().numpy()[p]      # (17,2)
                    kcf = r.keypoints.conf.cpu().numpy()[p]     # (17,)
                    parts = [f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"]
                    for j in range(17):
                        v_flag = 2 if kcf[j] > 0.5 else (1 if kcf[j] > 0.1 else 0)
                        parts.append(f"{kxy[j, 0]:.6f} {kxy[j, 1]:.6f} {v_flag}")
                    stem = f"{base}_{i:06d}"
                    cv2.imwrite(os.path.join(ds_dir, "images", "train", stem + ".jpg"), fr,
                                [cv2.IMWRITE_JPEG_QUALITY, 90])
                    with open(os.path.join(ds_dir, "labels", "train", stem + ".txt"),
                              "w", encoding="utf-8") as fh:
                        fh.write(" ".join(parts) + "\n")
                    labeled += 1
        i += 1
    cap.release()
    return labeled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enable", action="store_true",
                    help="REQUIRED to do anything; this shim is deferred and off by default")
    ap.add_argument("--urls", nargs="*", default=[])
    ap.add_argument("--urls-file", default="", help="text file, one YouTube URL per line")
    ap.add_argument("--out", default=os.path.join("logs", "diagnostics", "no_meter_prep", "pose_ds"),
                    help="pose dataset root (same layout as pose_pseudolabel_dataset.py --out)")
    ap.add_argument("--download-dir", default="",
                    help="where the raw downloads land (default <out>/yt_downloads)")
    ap.add_argument("--teacher", default="yolov8x-pose.pt")
    ap.add_argument("--min-conf", type=float, default=YT_MIN_CONF_DEFAULT,
                    help=f"teacher confidence floor (default {YT_MIN_CONF_DEFAULT}, "
                         f"harder than the {OWNER_MIN_CONF} owner-data default)")
    ap.add_argument("--per-video", type=int, default=150)
    ap.add_argument("--max-scan", type=int, default=1200)
    ap.add_argument("--keep-downloads", action="store_true",
                    help="keep the raw .mp4 downloads (default: delete after labeling)")
    args = ap.parse_args()

    if not args.enable:
        print("youtube_augment is DEFERRED and DISABLED by default (no --enable given).\n"
              "Policy: YouTube frames are a secondary source for jump-shot ANIMATION VARIETY\n"
              "only (builds the owner doesn't play). They are labeled at a harder teacher\n"
              "threshold (0.75), face-cam quadrant blacked out, tagged source=youtube, and are\n"
              "train-only -- validation stays owner-only. Not usable for cue/temporal training.\n"
              "Nothing was downloaded. Re-run with --enable when the owner signs off.")
        return 0

    urls = list(args.urls)
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as fh:
            urls += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not urls:
        print("no URLs given (--urls / --urls-file)")
        return 2

    os.environ.setdefault("YOLO_VERBOSE", "False")
    try:
        import cv2  # noqa: F401
        from ultralytics import YOLO
    except Exception as exc:
        print(f"need opencv + ultralytics: {exc}\n"
              "run tools/training/no_meter_mode_prep.py first -- its CUDA preflight prints the fix")
        return 2
    try:
        import yt_dlp  # noqa: F401
    except Exception as exc:
        print(f"need yt-dlp: {exc}\n  pip install yt-dlp")
        return 2
    import torch
    if not torch.cuda.is_available():
        print("CUDA torch unavailable -- refusing to label on CPU (8fps trap). "
              "See no_meter_mode_prep.py preflight for the install command.")
        return 2

    ds_dir = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    for sub in (os.path.join("images", "train"), os.path.join("labels", "train")):
        os.makedirs(os.path.join(ds_dir, sub), exist_ok=True)
    dl_dir = args.download_dir or os.path.join(ds_dir, "yt_downloads")
    os.makedirs(dl_dir, exist_ok=True)

    teacher = YOLO(args.teacher)
    print(f"teacher={args.teacher}  min-conf={args.min_conf} (owner data uses {OWNER_MIN_CONF})  "
          f"urls={len(urls)}  -> {ds_dir} [train split only]")

    total_labeled = 0
    stems = {}
    for u in urls:
        try:
            vp = download(u, dl_dir)
        except Exception as exc:
            print(f"  download failed: {u} ({exc})")
            continue
        if not vp or not os.path.isfile(vp):
            print(f"  download produced no file: {u}")
            continue
        n = label_video(vp, teacher, ds_dir, args.per_video, args.min_conf, args.max_scan)
        base = "yt_" + sanitize(os.path.splitext(os.path.basename(vp))[0])
        stems[base] = "youtube"
        total_labeled += n
        print(f"  {os.path.basename(vp):40.40} labeled {n} (face-cam quadrant blacked)")
        if not args.keep_downloads:
            try:
                os.remove(vp)
            except OSError:
                pass

    if stems:
        update_sources_json(ds_dir, stems)
    print(f"\nDONE: {total_labeled} youtube frames -> train split only "
          f"(sources.json updated; keep val owner-only via split_train_val.py)")
    return 0 if total_labeled > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
