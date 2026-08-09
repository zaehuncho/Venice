#!/usr/bin/env python3
"""Fine-tune RF-DETR on the 2K player detection dataset.

Restructures the existing YOLO dataset to RF-DETR's expected format,
then fine-tunes RFDETRNano (or Small) with the 4-class player labels:
  0=Player, 1=Stamina, 2=Unplayable, 3=Zbasketball

Usage:
  python tools/diagnostics/train_rfdetr.py [--model nano] [--epochs 50] [--val-split 0.15]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import glob
import random

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Prefer expanded dataset if it exists, fall back to original
_EXPANDED = os.path.join(ROOT, "logs", "diagnostics", "rfdetr_expanded_dataset")
SRC_DATASET = _EXPANDED if os.path.exists(os.path.join(_EXPANDED, "data.yaml")) else os.path.join(ROOT, "logs", "diagnostics", "player_shot_label")
OUT_DATASET = _EXPANDED if os.path.exists(os.path.join(_EXPANDED, "data.yaml")) else os.path.join(ROOT, "logs", "diagnostics", "rfdetr_player_dataset")


def restructure_dataset(src: str, dst: str, val_split: float = 0.15) -> str:
    """Restructure YOLO dataset from images/train/ to train/images/ format.

    RF-DETR expects:
      dst/
        data.yaml
        train/images/*.jpg
        train/labels/*.txt
        valid/images/*.jpg
        valid/labels/*.txt
    """
    src_images = os.path.join(src, "images", "train")
    src_labels = os.path.join(src, "labels", "train")
    if not os.path.isdir(src_images):
        print(f"ERROR: {src_images} not found")
        return ""

    images = sorted(glob.glob(os.path.join(src_images, "*.jpg")))
    if not images:
        images = sorted(glob.glob(os.path.join(src_images, "*.png")))
    if len(images) < 30:
        print(f"ERROR: only {len(images)} images found, need 30+")
        return ""

    # Clean output
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.join(dst, "train", "images"))
    os.makedirs(os.path.join(dst, "train", "labels"))
    os.makedirs(os.path.join(dst, "valid", "images"))
    os.makedirs(os.path.join(dst, "valid", "labels"))

    # Shuffle and split
    random.seed(42)
    random.shuffle(images)
    n_val = max(1, int(len(images) * val_split))
    val_images = images[:n_val]
    train_images = images[n_val:]

    def copy_pair(img_path, split):
        label_path = img_path.replace("images", "labels").replace(
            os.path.splitext(img_path)[1], ".txt"
        )
        dst_img = os.path.join(dst, split, "images", os.path.basename(img_path))
        dst_lbl = os.path.join(dst, split, "labels", os.path.basename(label_path))
        shutil.copy2(img_path, dst_img)
        if os.path.exists(label_path):
            shutil.copy2(label_path, dst_lbl)
        else:
            # Create empty label file for negative samples
            open(dst_lbl, "w").close()

    for img in train_images:
        copy_pair(img, "train")
    for img in val_images:
        copy_pair(img, "valid")

    # Write data.yaml
    yaml_path = os.path.join(dst, "data.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"""path: {os.path.abspath(dst)}
train: train/images
val: valid/images

nc: 4
names:
  0: Player
  1: Stamina
  2: Unplayable
  3: Zbasketball
""")

    n_train = len(train_images)
    n_val = len(val_images)
    print(f"Dataset restructured: {n_train} train, {n_val} val -> {dst}")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="nano", choices=["nano", "small", "medium"],
                     help="RF-DETR model size")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--reuse", action="store_true", default=True, help="reuse existing restructured dataset (skip rebuild)")
    ap.add_argument("--rebuild", action="store_true", default=False, help="force rebuild dataset from source")
    ap.add_argument("--resume", default=None, help="resume from checkpoint path")
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4, help="gradient accumulation steps")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--val-split", type=float, default=0.15, help="fraction of data for validation")
    ap.add_argument("--src", default=SRC_DATASET, help="source dataset directory")
    ap.add_argument("--dst", default=OUT_DATASET, help="output dataset directory")
    ap.add_argument("--out", default=None, help="model output directory (default: models/rfdetr_player_{model})")
    args = ap.parse_args()

    if args.out is None:
        args.out = os.path.join(ROOT, "models", f"rfdetr_player_{args.model}")

    # Step 1: Restructure dataset (or reuse existing)
    print("=" * 60)
    print("Step 1: Prepare dataset for RF-DETR")
    print("=" * 60)
    data_yaml = os.path.join(args.dst, "data.yaml")
    if args.rebuild or not os.path.exists(data_yaml):
        dataset_dir = restructure_dataset(args.src, args.dst, args.val_split)
        if not dataset_dir:
            return 1
    else:
        dataset_dir = args.dst
        n_train = len(glob.glob(os.path.join(dataset_dir, "train", "images", "*.jpg")))
        n_val = len(glob.glob(os.path.join(dataset_dir, "valid", "images", "*.jpg")))
        print(f"Reusing existing dataset: {n_train} train, {n_val} val -> {dataset_dir}")

    # Step 2: Load model and configure for 4 classes
    print(f"\n{'='*60}")
    print(f"Step 2: Load RF-DETR {args.model} (4 classes)")
    print(f"{'='*60}")
    try:
        from rfdetr import RFDETRNano, RFDETRSmall, RFDETRMedium
    except Exception as exc:
        print(f"ERROR: need rfdetr: {exc}")
        return 2

    model_map = {
        "nano": RFDETRNano,
        "small": RFDETRSmall,
        "medium": RFDETRMedium,
    }
    model_cls = model_map[args.model]
    model = model_cls()  # num_classes auto-detected from data.yaml

    print(f"  model: RF-DETR {args.model}, num_classes auto-detected from data.yaml")
    print(f"  epochs: {args.epochs}, batch_size: {args.batch_size}, grad_accum: {args.grad_accum}")
    print(f"  lr: {args.lr}")
    if args.resume:
        print(f"  resume from: {args.resume}")

    # Step 3: Train
    print(f"\n{'='*60}")
    print("Step 3: Fine-tuning...")
    print(f"{'='*60}")

    output_dir = args.out
    os.makedirs(output_dir, exist_ok=True)

    train_kwargs = dict(
        dataset_dir=dataset_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        grad_accum_steps=args.grad_accum,
        lr=args.lr,
        output_dir=output_dir,
        amp_dtype="fp16",
    )
    if args.resume:
        train_kwargs["resume"] = args.resume

    model.train(**train_kwargs)

    # Step 4: Check output
    print(f"\n{'='*60}")
    print("Step 4: Training complete")
    print(f"{'='*60}")

    # RF-DETR saves checkpoints in output_dir
    ckpts = glob.glob(os.path.join(output_dir, "*.pth"))
    if ckpts:
        for ck in sorted(ckpts):
            sz = os.path.getsize(ck) / (1024 * 1024)
            print(f"  checkpoint: {ck} ({sz:.1f} MB)")
    else:
        print(f"  WARNING: no .pth files found in {output_dir}")
        # Check subdirectories
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                if f.endswith(".pth"):
                    print(f"  found: {os.path.join(root, f)}")

    print(f"\nTo evaluate, run:")
    print(f'  python tools/diagnostics/rfdetr_probe.py "<video>" --model nano --frames 300 --conf 0.5')
    print(f"  (update rfdetr_probe.py to load the fine-tuned model)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
