"""Expand RF-DETR training dataset with diverse video frames + copy-paste augmentation.

Steps:
  1. Extract frames from diverse videos (different dates, courts, angles)
  2. Auto-label extracted frames using YOLO orion_player_detect_v9.pt
  3. Merge all existing labeled datasets (player_shot_label, v5boost, v5balanced)
  4. Generate copy-paste augmented images using 30K+ player crops
  5. Restructure into RF-DETR YOLO format with train/val split
"""
import argparse
import glob
import os
import random
import shutil
import cv2
import numpy as np
from pathlib import Path


# ── Existing labeled datasets (same class mapping: 0=Player, 1=Stamina, 2=Unplayable, 3=Zbasketball)
EXISTING_DATASETS = [
    "logs/diagnostics/player_shot_label",
    "logs/diagnostics/player_shot_label_v5boost",
    "logs/diagnostics/player_shot_label_v5balanced",
]

# Player crop directories
CROP_DIRS = [
    "tools/diagnostics/user_player_dataset/user",
    "tools/diagnostics/user_player_dataset/other",
]

# Output
OUT_DIR = "logs/diagnostics/rfdetr_expanded_dataset"
FRAME_OUT = "logs/diagnostics/rfdetr_new_frames"  # temp storage for extracted frames

# Video directory
VIDEO_DIR = r"C:\Users\Administrator\Videos"

# Diverse video selection — different dates, sizes, sessions
# We'll pick ~25 videos spanning Feb through Jun
VIDEO_PICKS = [
    "2026-02-08 22-02-58.mp4",       # early, large file
    "2026-03-22 18-28-44.mp4",
    "2026-04-14 19-56-13.mp4",
    "2026-04-16 14-57-23.mp4",       # large, 4 min
    "2026-04-17 10-28-12.mp4",       # very large, 6 min
    "2026-04-19 22-19-13.mp4",
    "2026-04-20 13-36-55.mp4",
    "2026-04-26 20-47-00.mp4",
    "2026-04-26 20-48-59.mp4",
    "2026-05-19 13-49-36.mp4",
    "2026-06-01 11-06-46.mp4",
    "2026-06-03 20-56-48.mp4",
    "2026-06-04 10-28-09.mp4",       # very large, 5 min
    "2026-06-04 11-58-46.mp4",
    "2026-06-05 03-35-02.mp4",
    "2026-06-06 12-47-20.mp4",       # large, 4 min
    "2026-06-06 20-57-58.mp4",       # large
    "2026-06-07 21-08-10.mp4",       # large
    "2026-06-08 14-25-08.mp4",       # large
    "2026-06-12 13-09-29.mp4",       # large, 4 min
    "2026-06-15 20-35-16.mp4",       # large
    "2026-06-18 05-49-28.mp4",
    "2026-06-22 16-22-44.mp4",
    "2026-06-24 09-35-26.mp4",       # large
    "2026-06-25 21-49-04.mp4",       # large
    "2026-06-26 21-20-24.mp4",
    "2026-06-27 16-58-31.mp4",
    "2026-06-28 00-45-13.mp4",
    "2026-06-28 20-22-32.mp4",
    "NBA 2K26_20260324195410.mp4",   # named capture
    "NBA 2K26_20260521032018.mp4",
    "NBA 2K26_20260618064057.mp4",
    "RED_DETECTION_PARK.mp4",
    "RED_DETECTION_TEST.mp4",
    "V3_DETECTION_PROOF.mp4",
]


def extract_frames(videos, out_dir, frames_per_video=100, conf_threshold=0.4):
    """Extract frames from diverse videos and auto-label with YOLO."""
    from ultralytics import YOLO

    os.makedirs(out_dir, exist_ok=True)
    img_dir = os.path.join(out_dir, "images")
    lbl_dir = os.path.join(out_dir, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    model = YOLO("models/orion_player_detect_v9.pt")
    total_extracted = 0
    total_labeled = 0

    for vid_name in videos:
        vid_path = os.path.join(VIDEO_DIR, vid_name)
        if not os.path.exists(vid_path):
            print(f"  SKIP (not found): {vid_name}")
            continue

        cap = cv2.VideoCapture(vid_path)
        if not cap.isOpened():
            print(f"  SKIP (cannot open): {vid_name}")
            continue

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / max(fps, 1)

        # Sample frames evenly across the video, skipping first/last 5%
        start = int(total_frames * 0.05)
        end = int(total_frames * 0.95)
        step = max(1, (end - start) // frames_per_video)

        vid_prefix = Path(vid_name).stem.replace(" ", "_").replace("-", "")
        count = 0
        labeled = 0

        for idx in range(start, end, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            fname = f"{vid_prefix}_f{idx:06d}.jpg"
            fpath = os.path.join(img_dir, fname)
            cv2.imwrite(fpath, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            count += 1

            # Auto-label with YOLO
            results = model.predict(fpath, conf=conf_threshold, verbose=False, imgsz=640)
            lines = []
            if results and results[0].boxes is not None:
                boxes = results[0].boxes
                for i in range(len(boxes)):
                    cls = int(boxes.cls[i].item())
                    conf = float(boxes.conf[i].item())
                    x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy()
                    # Convert to YOLO format (normalized cx, cy, w, h)
                    h, w = frame.shape[:2]
                    cx = (x1 + x2) / 2 / w
                    cy = (y1 + y2) / 2 / h
                    bw = (x2 - x1) / w
                    bh = (y2 - y1) / h
                    lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")

            if lines:
                lbl_path = os.path.join(lbl_dir, fname.replace(".jpg", ".txt"))
                with open(lbl_path, "w") as f:
                    f.write("\n".join(lines) + "\n")
                labeled += 1
            else:
                # Write empty label file (frame with no detections)
                lbl_path = os.path.join(lbl_dir, fname.replace(".jpg", ".txt"))
                open(lbl_path, "w").close()

            total_extracted += 1
            if count % 20 == 0:
                print(f"  [{vid_name}] extracted {count}/{frames_per_video}, labeled {labeled}")

        cap.release()
        print(f"  {vid_name}: {count} frames, {labeled} labeled (dur={duration:.0f}s)")

    print(f"\nTotal: {total_extracted} frames extracted, {total_labeled} auto-labeled")
    return total_extracted


def load_crop_paths(crop_dirs, max_crops=5000):
    """Load player crop image paths."""
    crops = []
    for d in crop_dirs:
        if os.path.exists(d):
            crops.extend(glob.glob(os.path.join(d, "*.jpg")))
            crops.extend(glob.glob(os.path.join(d, "*.png")))
    random.shuffle(crops)
    return crops[:max_crops]


def copy_paste_augment(src_img_dir, src_lbl_dir, crop_paths, out_dir, n_aug=2000):
    """Generate copy-paste augmented images by pasting player crops onto backgrounds."""
    aug_img_dir = os.path.join(out_dir, "images")
    aug_lbl_dir = os.path.join(out_dir, "labels")
    os.makedirs(aug_img_dir, exist_ok=True)
    os.makedirs(aug_lbl_dir, exist_ok=True)

    # Get background images (frames that have existing labels)
    bg_images = sorted(glob.glob(os.path.join(src_img_dir, "*.jpg")))
    if not bg_images:
        print("  No background images found for augmentation")
        return 0

    count = 0
    for i in range(n_aug):
        bg_path = random.choice(bg_images)
        bg = cv2.imread(bg_path)
        if bg is None:
            continue
        h, w = bg.shape[:2]

        # Load existing labels for this background
        lbl_path = os.path.join(src_lbl_dir, os.path.basename(bg_path).replace(".jpg", ".txt"))
        existing_lines = []
        if os.path.exists(lbl_path):
            with open(lbl_path) as f:
                existing_lines = [l.strip() for l in f if l.strip()]

        # Paste 1-3 random crops
        n_paste = random.randint(1, 3)
        new_lines = list(existing_lines)  # keep existing labels

        for _ in range(n_paste):
            if not crop_paths:
                break
            crop_path = random.choice(crop_paths)
            crop = cv2.imread(crop_path)
            if crop is None:
                continue

            ch, cw = crop.shape[:2]
            # Scale crop to reasonable player size (3-8% of frame width)
            target_w = random.randint(int(w * 0.03), int(w * 0.08))
            scale = target_w / max(cw, 1)
            target_h = int(ch * scale)
            if target_h < 10 or target_h > h * 0.5:
                continue
            resized = cv2.resize(crop, (target_w, target_h))

            # Random position (avoid edges)
            max_x = w - target_w - 10
            max_y = h - target_h - 10
            if max_x < 10 or max_y < 10:
                continue
            px = random.randint(10, max_x)
            py = random.randint(10, max_y)

            # Alpha blend edges for smoother paste
            blend_border = min(5, target_w // 4, target_h // 4)
            if blend_border > 0:
                # Create a mask with feathered edges
                mask = np.ones((target_h, target_w), dtype=np.float32)
                mask[:blend_border] *= np.linspace(0, 1, blend_border)[:, None]
                mask[-blend_border:] *= np.linspace(1, 0, blend_border)[:, None]
                mask[:, :blend_border] *= np.linspace(0, 1, blend_border)[None, :]
                mask[:, -blend_border:] *= np.linspace(1, 0, blend_border)[None, :]
                mask = mask[:, :, None]
                bg[py:py+target_h, px:px+target_w] = (
                    resized * mask + bg[py:py+target_h, px:px+target_w] * (1 - mask)
                ).astype(np.uint8)
            else:
                bg[py:py+target_h, px:px+target_w] = resized

            # YOLO label for pasted crop (class 0 = Player)
            cx = (px + target_w / 2) / w
            cy = (py + target_h / 2) / h
            bw = target_w / w
            bh = target_h / h
            new_lines.append(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")

        # Save augmented image
        out_name = f"aug_{i:05d}.jpg"
        out_img = os.path.join(aug_img_dir, out_name)
        cv2.imwrite(out_img, bg, [cv2.IMWRITE_JPEG_QUALITY, 92])

        out_lbl = os.path.join(aug_lbl_dir, out_name.replace(".jpg", ".txt"))
        with open(out_lbl, "w") as f:
            f.write("\n".join(new_lines) + "\n" if new_lines else "")

        count += 1
        if count % 200 == 0:
            print(f"  Copy-paste augmentation: {count}/{n_aug}")

    print(f"  Copy-paste augmentation complete: {count} images")
    return count


def merge_and_split(src_datasets, new_frames_dir, aug_dir, out_dir, val_split=0.12):
    """Merge all datasets and create train/val split for RF-DETR."""
    train_img = os.path.join(out_dir, "train", "images")
    train_lbl = os.path.join(out_dir, "train", "labels")
    val_img = os.path.join(out_dir, "valid", "images")
    val_lbl = os.path.join(out_dir, "valid", "labels")
    for d in [train_img, train_lbl, val_img, val_lbl]:
        os.makedirs(d, exist_ok=True)

    # Collect all (image, label) pairs
    pairs = []  # (img_path, lbl_path, prefix)

    # Existing datasets
    for ds in src_datasets:
        for sub in ["train", "val", "valid"]:
            img_d = os.path.join(ds, "images", sub)
            lbl_d = os.path.join(ds, "labels", sub)
            if not os.path.isdir(img_d):
                # Try flat structure
                img_d = os.path.join(ds, "images")
                lbl_d = os.path.join(ds, "labels")
            if not os.path.isdir(img_d):
                continue
            prefix = Path(ds).name
            for img in glob.glob(os.path.join(img_d, "*.jpg")):
                lbl = os.path.join(lbl_d, os.path.basename(img).replace(".jpg", ".txt"))
                if os.path.exists(lbl):
                    pairs.append((img, lbl, prefix))

    # New auto-labeled frames
    if os.path.isdir(os.path.join(new_frames_dir, "images")):
        for img in glob.glob(os.path.join(new_frames_dir, "images", "*.jpg")):
            lbl = os.path.join(new_frames_dir, "labels", os.path.basename(img).replace(".jpg", ".txt"))
            if os.path.exists(lbl):
                pairs.append((img, lbl, "new"))

    # Augmented images
    if os.path.isdir(os.path.join(aug_dir, "images")):
        for img in glob.glob(os.path.join(aug_dir, "images", "*.jpg")):
            lbl = os.path.join(aug_dir, "labels", os.path.basename(img).replace(".jpg", ".txt"))
            if os.path.exists(lbl):
                pairs.append((img, lbl, "aug"))

    print(f"  Total pairs to merge: {len(pairs)}")

    # Deduplicate by image hash (simple: filename + filesize)
    seen = set()
    unique = []
    for img, lbl, prefix in pairs:
        key = (os.path.basename(img), os.path.getsize(img))
        if key not in seen:
            seen.add(key)
            unique.append((img, lbl, prefix))

    print(f"  After dedup: {len(unique)}")

    # Shuffle and split
    random.shuffle(unique)
    n_val = max(50, int(len(unique) * val_split))
    val_pairs = unique[:n_val]
    train_pairs = unique[n_val:]

    # Copy files
    for img, lbl, prefix in train_pairs:
        fname = f"{prefix}_{os.path.basename(img)}"
        shutil.copy2(img, os.path.join(train_img, fname))
        shutil.copy2(lbl, os.path.join(train_lbl, fname.replace(".jpg", ".txt")))

    for img, lbl, prefix in val_pairs:
        fname = f"{prefix}_{os.path.basename(img)}"
        shutil.copy2(img, os.path.join(val_img, fname))
        shutil.copy2(lbl, os.path.join(val_lbl, fname.replace(".jpg", ".txt")))

    print(f"  Train: {len(train_pairs)}, Val: {len(val_pairs)}")

    # Write data.yaml
    yaml_path = os.path.join(out_dir, "data.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"path: {os.path.abspath(out_dir)}\n")
        f.write("train: train/images\n")
        f.write("val: valid/images\n")
        f.write("test: valid/images\n")
        f.write("names:\n")
        f.write("  0: Player\n")
        f.write("  1: Stamina\n")
        f.write("  2: Unplayable\n")
        f.write("  3: Zbasketball\n")

    return len(train_pairs), len(val_pairs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames-per-video", type=int, default=100, help="frames to extract per video")
    ap.add_argument("--n-aug", type=int, default=2000, help="copy-paste augmented images to generate")
    ap.add_argument("--max-crops", type=int, default=5000, help="max crop images to load for augmentation")
    ap.add_argument("--val-split", type=float, default=0.12)
    ap.add_argument("--skip-extract", action="store_true", help="skip video frame extraction")
    ap.add_argument("--skip-aug", action="store_true", help="skip copy-paste augmentation")
    ap.add_argument("--skip-merge", action="store_true", help="skip merge step")
    args = ap.parse_args()

    random.seed(42)

    # Step 1: Extract frames from diverse videos + auto-label
    if not args.skip_extract:
        print("=" * 60)
        print("Step 1: Extract frames from diverse videos + auto-label")
        print("=" * 60)
        n = extract_frames(VIDEO_PICKS, FRAME_OUT, args.frames_per_video)
        print(f"  Extracted {n} frames\n")
    else:
        print("  Skipping frame extraction\n")

    # Step 2: Copy-paste augmentation with 30K crops
    if not args.skip_aug:
        print("=" * 60)
        print("Step 2: Copy-paste augmentation with player crops")
        print("=" * 60)
        crop_paths = load_crop_paths(CROP_DIRS, args.max_crops)
        print(f"  Loaded {len(crop_paths)} crop images")

        # Use existing labeled frames as backgrounds
        bg_img_dir = os.path.join("logs/diagnostics/player_shot_label", "images", "train")
        bg_lbl_dir = os.path.join("logs/diagnostics/player_shot_label", "labels", "train")
        if not os.path.isdir(bg_img_dir):
            bg_img_dir = os.path.join("logs/diagnostics/player_shot_label", "images")
            bg_lbl_dir = os.path.join("logs/diagnostics/player_shot_label", "labels")

        aug_dir = os.path.join(FRAME_OUT, "augmented")
        n = copy_paste_augment(bg_img_dir, bg_lbl_dir, crop_paths, aug_dir, args.n_aug)
        print(f"  Generated {n} augmented images\n")
    else:
        print("  Skipping copy-paste augmentation\n")

    # Step 3: Merge all datasets + split
    if not args.skip_merge:
        print("=" * 60)
        print("Step 3: Merge all datasets + train/val split")
        print("=" * 60)
        aug_dir = os.path.join(FRAME_OUT, "augmented")
        n_train, n_val = merge_and_split(
            EXISTING_DATASETS, FRAME_OUT, aug_dir, OUT_DIR, args.val_split
        )
        print(f"\n  Final dataset: {n_train} train, {n_val} val")
        print(f"  Output: {OUT_DIR}")
    else:
        print("  Skipping merge\n")

    print("\nDone! Ready for training:")
    print(f"  python tools/diagnostics/train_rfdetr.py --model small --epochs 100 --src {OUT_DIR} --dst {OUT_DIR} --out models/rfdetr_player_small")


if __name__ == "__main__":
    raise SystemExit(main())
