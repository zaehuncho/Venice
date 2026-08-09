#!/usr/bin/env python3
"""Retrain v9 player detector with shot-animation frames added.

Fine-tunes from the existing v9 weights (transfer learning) so the model
keeps its at-rest performance while learning to hold through shot animations.

Classes (MUST match v9): 0=Player, 1=Stamina, 2=Unplayable, 3=Zbasketball

Usage:
    C:\\Python314\\python.exe tools\\diagnostics\\train_player_v9.py [--epochs 80]

Prerequisites:
    1. Run extract_shot_frames.py to harvest frames + pseudo-labels
    2. Correct labels in labelImg (especially missed Player boxes during shots)
    3. Ensure dataset.yaml exists at logs/diagnostics/player_shot_label/dataset.yaml
"""
from __future__ import annotations

import argparse
import os
import sys
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def write_dataset_yaml(out_dir: str) -> str:
    """Write a YOLO dataset.yaml for the player shot-label set."""
    yaml_path = os.path.join(out_dir, "dataset.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"""path: {os.path.abspath(out_dir)}
train: images/train
val: images/train

nc: 4
names:
  0: Player
  1: Stamina
  2: Unplayable
  3: Zbasketball
""")
    return yaml_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="logs/diagnostics/player_shot_label")
    ap.add_argument("--base", default="models/orion_player_detect_v9.pt",
                    help="fine-tune from existing v9 weights (transfer learning)")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--name", default="player_v9_shot")
    args = ap.parse_args()

    from ultralytics import YOLO

    data_dir = os.path.join(ROOT, args.data)
    yaml_path = write_dataset_yaml(data_dir)

    # Check we have labeled frames
    lbl_dir = os.path.join(data_dir, "labels", "train")
    if not os.path.isdir(lbl_dir):
        print(f"ERROR: {lbl_dir} not found. Run extract_shot_frames.py first.")
        return 1

    import glob
    labels = glob.glob(os.path.join(lbl_dir, "*.txt"))
    pos = sum(1 for f in labels if os.path.getsize(f) > 0)
    print(f"labeled files: {len(labels)} ({pos} with at least one box)")
    if pos < 30:
        print(f"only {pos} positive labels -- need at least 30+ for fine-tuning")
        print("run: C:\\Python314\\python.exe tools\\diagnostics\\extract_shot_frames.py")
        return 1

    base_path = os.path.join(ROOT, args.base)
    if not os.path.exists(base_path):
        print(f"ERROR: base model {base_path} not found")
        return 1

    print(f"\nFine-tuning from {args.base} for {args.epochs} epochs...")
    m = YOLO(base_path)
    m.train(
        data=yaml_path,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=16,
        device=0,
        workers=0,          # Windows: avoid DataLoader worker crashes
        project="logs/diagnostics/player_v9_train",
        name=args.name,
        patience=20,
        # Augmentation: keep it mild for fine-tuning
        degrees=0,
        shear=0,
        perspective=0,
        mosaic=0.3,
        scale=0.2,
        flipud=0.0,         # don't flip vertically (HUD/stamina bar orientation matters)
        fliplr=0.5,         # horizontal flip is fine
    )

    best = f"logs/diagnostics/player_v9_train/{args.name}/weights/best.pt"
    if os.path.exists(best):
        # Backup old model, deploy new one
        backup = "models/orion_player_detect_v9_backup.pt"
        if os.path.exists(base_path) and not os.path.exists(backup):
            shutil.copy(base_path, backup)
            print(f"backed up old v9 -> {backup}")
        shutil.copy(best, base_path)
        print(f"DONE -> {base_path}")
        print("\nVerify with:")
        print(f'  C:\\Python314\\python.exe tools\\diagnostics\\eval_skele_pipeline.py '
              f'"C:\\Users\\Administrator\\Videos\\NBA 2K26_20260324195410.mp4" 3000 3760 Right')
    else:
        print(f"ERROR: best.pt not found at {best}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
