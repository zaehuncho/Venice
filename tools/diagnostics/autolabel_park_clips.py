#!/usr/bin/env python3
"""Auto-label the shot meter across the real PARK CLIPS (and live-batch recordings) -> pooled YOLO dataset
for the meter LOCATOR. Reuses the proven meter_autolabel.py (green make-window anchor + red-column track =
100% precision, labels every fill) by extracting each clip's frames and running it per clip, then pooling.

This is the REAL training data the synthetic set can't fully cover (true colour/lighting/motion of the live
red park meter). Combine with datasets/meter_fullframe_synth + datasets/meter_poc to train.

USAGE:
  C:\\Python314\\python.exe tools/diagnostics/autolabel_park_clips.py [--step 2] [--out datasets/meter_real_park]
"""
import argparse, glob, os, shutil, subprocess, sys, tempfile
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VIDEOS = r"C:\Users\Administrator\Videos"
AUTOLABEL = os.path.join(ROOT, "tools", "diagnostics", "meter_autolabel.py")


def find_clips():
    # RAW gameplay clips only (no Orion neon overlay burned in — that would pollute the green/red
    # auto-label). The RED_PARK_*.mp4 are Orion-overlay recordings; add them only after confirming the
    # overlay doesn't sit on the meter. The NBA 2K26_* are clean raw 5v5-park OBS recordings.
    pats = ["NBA 2K26_*.mp4"]
    clips = []
    for p in pats:
        clips += glob.glob(os.path.join(VIDEOS, p))
    # Skip huge multi-minute session recordings (the 426MB Mar clip is ~30min = a black hole); the
    # focused Jun-24 shot-timing clips (~85-125MB, shot-dense) give plenty of labeled meters fast.
    # Include the big full-frame sessions (Jun-18 166MB, May 172MB, Mar 406MB) that were previously
    # skipped -- they hold the most shots + the most shot-type diversity. extract()'s --max-frames cap
    # bounds each clip's I/O so a long session can't black-hole (the earlier 30-min-clip failure).
    clips = [c for c in clips if os.path.getsize(c) < 500 * 1024 * 1024]
    seen = set(); out = []                                       # de-dup, keep order
    for c in sorted(clips, key=os.path.getsize):                # smallest first (fast feedback)
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def extract(clip, tmp, step, max_frames=0, native=False):
    """Extract every Nth frame, DOWNSCALED to 720p (5x less I/O; the meter is clearly visible and the
    auto-labeler + red/green masks are resolution-relative). Labels come out in 720p coords -> we save the
    720p frame, so they stay consistent (a 720p training frame is fine for a YOLO locator at imgsz 960).
    native=True keeps the clip's NATIVE resolution (1080p for the 2K26 clips) — more I/O per frame but
    the thin meter keeps its full pixel width; labels are normalized so they stay resolution-independent.
    max_frames>0 STOPS after that many kept frames -> bounds decode+I/O so a 30-min session can't black-hole."""
    cap = cv2.VideoCapture(clip)
    if not cap.isOpened():
        return 0
    i = n = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            if not native and (fr.shape[1] != 1280 or fr.shape[0] != 720):
                fr = cv2.resize(fr, (1280, 720))
            cv2.imwrite(os.path.join(tmp, f"f{n:06d}.jpg"), fr, [cv2.IMWRITE_JPEG_QUALITY, 90])
            n += 1
            if max_frames and n >= max_frames:
                break
        i += 1
    cap.release()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=2, help="use every Nth frame (2 = 30fps; tracking stays contiguous)")
    ap.add_argument("--out", default=os.path.join("datasets", "meter_real_park"))
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--max-frames", type=int, default=10000,
                    help="cap kept frames per clip (0=unbounded) -> bounds I/O so a long session can't black-hole")
    ap.add_argument("--native", action="store_true",
                    help="extract at the clip's NATIVE resolution (e.g. 1080p) instead of the 720p downscale; "
                         "labels stay normalized so they remain resolution-independent")
    args = ap.parse_args()

    out = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    img_dir = os.path.join(out, "images"); lab_dir = os.path.join(out, "labels")
    os.makedirs(img_dir, exist_ok=True); os.makedirs(lab_dir, exist_ok=True)

    clips = find_clips()
    print(f"clips: {len(clips)}")
    total = 0
    for ci, clip in enumerate(clips):
        base = os.path.splitext(os.path.basename(clip))[0].replace(" ", "_")
        tmp = tempfile.mkdtemp(prefix="clipframes_")
        tmp_out = tempfile.mkdtemp(prefix="cliplabels_")
        try:
            nf = extract(clip, tmp, args.step, args.max_frames, native=args.native)
            if nf < 10:
                print(f"  [{ci}] {base}: only {nf} frames, skip"); continue
            # run the proven auto-labeler on this clip's frames
            subprocess.run([sys.executable, AUTOLABEL, os.path.join(tmp, "f*.jpg"), tmp_out],
                           cwd=ROOT, capture_output=True, text=True)
            labs = glob.glob(os.path.join(tmp_out, "labels", "*.txt"))
            k = 0
            for lf in labs:
                if os.path.getsize(lf) == 0:
                    continue
                stem = os.path.splitext(os.path.basename(lf))[0]
                src_img = os.path.join(tmp_out, "images", stem + ".png")
                if not os.path.exists(src_img):
                    continue
                name = f"{base}_{stem}"
                shutil.copy(src_img, os.path.join(img_dir, name + ".png"))
                shutil.copy(lf, os.path.join(lab_dir, name + ".txt"))
                k += 1
            total += k
            print(f"  [{ci}] {base}: {nf} frames -> {k} labeled")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(tmp_out, ignore_errors=True)

    # split + yaml
    imgs = sorted(glob.glob(os.path.join(img_dir, "*.png")))
    import random
    random.Random(0).shuffle(imgs)
    nv = int(len(imgs) * args.val_frac)
    val, tr = imgs[:nv], imgs[nv:]
    with open(os.path.join(out, "train.txt"), "w") as f:
        f.write("\n".join(tr) + "\n")
    with open(os.path.join(out, "val.txt"), "w") as f:
        f.write("\n".join(val) + "\n")
    with open(os.path.join(out, "meter.yaml"), "w") as f:
        f.write(f"path: {out}\ntrain: train.txt\nval: val.txt\nnames:\n  0: meter\n")
    print(f"\nTOTAL real labeled: {total}  (train {len(tr)} / val {len(val)}) -> {out}")


if __name__ == "__main__":
    main()
