#!/usr/bin/env python3
"""Train the LUMA meter LOCATOR for the compressed (Chiaki, capture-card-less) path.

The primary capture-card locator (models/orion_meter_n.pt) trains on COLOR frames and leans on
red HSV cues that DIE under compression (RED mask survival 0.13-0.37 under motion, per the ladder
eval). This trainer builds a GRAY-domain (Y replicated x3) dataset — exactly what
CompressedMeterReader feeds — so the model must localize the meter by SHAPE, not red colour:

  * synth_luma_twin  — luma copy of datasets/meter_fullframe_synth (full-frame synthetic meter +
                       hard-negative park/menu backgrounds; the shape/false-positive spine)
  * park_luma_twin   — luma copy of datasets/meter_real_park (real 2K park frames, auto-labeled)
  * <session>__<rung>_dup — REAL live framedumps re-encoded through the Remote Play ladder
                       (Balanced 4M / UltraLow 1.2M) then converted to luma = the true compressed
                       domain (built by build_rung_luma_dataset.py)

VAL = a held-out slice of the REAL luma dirs only (park twin + rung dirs), never synthetic, so
early-stop tracks real-domain recall.

Usage (repo root):
  C:/Python314/python.exe tools/training/train_meter_locator_luma.py \
      --root datasets/meter_rung_luma_v1 --epochs 60 --imgsz 960 \
      --out models/orion_meter_luma_v1.pt
Do NOT commit the model.
"""
from __future__ import annotations

import argparse
import glob
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _imgs(d):
    out = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        out += glob.glob(os.path.join(d, "images", ext))
        out += glob.glob(os.path.join(d, "frames", ext))
    return out


def collect(root, val_frac, seed=0, cap=0):
    """Return (train_list, val_list). REAL dirs (park twin + rung crops) are split val_frac into
    val; synthetic (synth twin) is train-only."""
    root = root if os.path.isabs(root) else os.path.join(ROOT, root)
    subdirs = [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]
    train, val = [], []
    rng = random.Random(seed)
    stats = {}
    for d in sorted(subdirs):
        name = os.path.basename(d)
        imgs = _imgs(d)
        if not imgs:
            continue
        if cap and len(imgs) > cap:
            imgs = imgs[::max(1, len(imgs) // cap)][:cap]
        is_synth = "synth" in name.lower()
        if is_synth:
            train += imgs
            stats[name] = (len(imgs), 0)
        else:
            rng.shuffle(imgs)
            k = int(len(imgs) * val_frac)
            val += imgs[:k]
            train += imgs[k:]
            stats[name] = (len(imgs) - k, k)
    return train, val, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join("datasets", "meter_rung_luma_v1"))
    ap.add_argument("--base", default="yolo11n.pt")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--cap", type=int, default=0, help="max images per dir (subsample for a fast run)")
    ap.add_argument("--name", default="meter_luma")
    ap.add_argument("--out", default=os.path.join("models", "orion_meter_luma_v1.pt"))
    args = ap.parse_args()

    train, val, stats = collect(args.root, args.val_frac, cap=args.cap)
    if not train:
        print(f"no training images under {args.root} (build the luma dirs first)")
        return 1
    if not val:                                   # degenerate: no real dir -> hold out from train
        random.Random(0).shuffle(train)
        k = max(1, int(len(train) * args.val_frac))
        val, train = train[:k], train[k:]

    root = args.root if os.path.isabs(args.root) else os.path.join(ROOT, args.root)
    train_list = os.path.join(root, "_luma_train.txt")
    val_list = os.path.join(root, "_luma_val.txt")
    with open(train_list, "w", encoding="utf-8") as f:
        f.write("\n".join(train) + "\n")
    with open(val_list, "w", encoding="utf-8") as f:
        f.write("\n".join(val) + "\n")
    yaml_path = os.path.join(root, "meter_luma.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(f"train: {train_list}\nval: {val_list}\nnames:\n  0: meter\n")
    print(f"LUMA locator dataset: {len(train)} train / {len(val)} val (real-only)")
    for n, (tr, va) in stats.items():
        print(f"    {n:<40} train={tr} val={va}")

    os.environ.setdefault("YOLO_VERBOSE", "False")
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    from ultralytics import YOLO
    try:
        from ultralytics import settings as _s
        _s.update({"mlflow": False, "clearml": False, "comet": False, "dvc": False, "wandb": False})
    except Exception:
        pass

    m = YOLO(args.base)
    project = os.path.join(ROOT, "logs", "diagnostics", "meter_train")
    # gray-replicated input: zero hue/sat aug (meaningless on gray), keep brightness + HUD geometry
    m.train(data=yaml_path, epochs=args.epochs, imgsz=args.imgsz, batch=args.batch, device=0,
            workers=args.workers, project=project, name=args.name, patience=args.patience,
            degrees=0, shear=0, perspective=0, mosaic=0.5, scale=0.5, translate=0.2,
            hsv_h=0.0, hsv_s=0.0, hsv_v=0.4)
    out_path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    best = getattr(getattr(m, "trainer", None), "best", None)
    if best and os.path.exists(best):
        import shutil
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        shutil.copy(best, out_path)
        print(f"DONE {best} -> {args.out}")
        return 0
    print(f"WARNING: best.pt not found (trainer.best={best}); {args.out} NOT written")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
