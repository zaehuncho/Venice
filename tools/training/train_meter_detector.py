#!/usr/bin/env python3
"""Train the meter LOCATOR (YOLO11n, 1 class 'meter') — the reject-random-objects fix.

Full-frame detector that finds the shot meter ANYWHERE (player-attached, variable position) and rejects
red distractors (arcade glow, UI, EA FC indicators) by LEARNED shape — what HSV colour + a fixed play-band
cannot. Two-stage runtime: this locator -> meter bbox -> constrain the colour scan / feed the ROI reader
(models/meter_roi/meter_roi.pt) for precise notch/fill/green.

Data: synthetic full-frame (tools/diagnostics/synth_meter_fullframe.py) + optional REAL (datasets/meter_poc,
88 labeled 1080p frames + the user's fresh park-shooting framedumps once auto-labeled). Validate on REAL.

USAGE:
  # 1) generate synthetic:  python tools/diagnostics/synth_meter_fullframe.py --n 4000
  # 2) train (synthetic):   python tools/training/train_meter_detector.py
  # 2b) train + real:       python tools/training/train_meter_detector.py --with-real
Outputs models/orion_meter_n.pt.
"""
from __future__ import annotations

import argparse
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _abs_image_list(list_or_dir, base):
    """Return absolute image paths from a YOLO train.txt/val.txt or an images dir."""
    paths = []
    if os.path.isfile(list_or_dir):
        for ln in open(list_or_dir, encoding="utf-8"):
            p = ln.strip()
            if not p:
                continue
            paths.append(p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p)))
    elif os.path.isdir(list_or_dir):
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            paths.extend(glob.glob(os.path.join(list_or_dir, ext)))
    return paths


def _ds(name):
    d = os.path.join(ROOT, "datasets", name)
    tr = _abs_image_list(os.path.join(d, "train.txt"), d) or _abs_image_list(os.path.join(d, "images"), d)
    va = _abs_image_list(os.path.join(d, "val.txt"), d)
    return tr, va


def _extra_dataset_images(d):
    """Absolute image paths for one --extra-dataset dir: prefer its train.txt, then images/, then
    the dir itself (the rung re-encode dirs / dual-capture yolo/ dirs)."""
    dd = d if os.path.isabs(d) else os.path.join(ROOT, d)
    return (_abs_image_list(os.path.join(dd, "train.txt"), dd)
            or _abs_image_list(os.path.join(dd, "images"), dd)
            or _abs_image_list(dd, dd))


def _write_combined_yaml(synth_yaml_dir, out_yaml, extra_datasets=None):
    """Combine SYNTHETIC + REAL park clips + meter_poc for training; validate on the REAL data
    (park clips val + meter_poc val). Writes list files.

    extra_datasets (used by --luma): extra dataset dirs (rung re-encodes + dual-capture yolo/)
    whose train images are appended. Absent/empty -> byte-identical to the historical behaviour."""
    synth_train = _abs_image_list(os.path.join(synth_yaml_dir, "train.txt"), synth_yaml_dir)
    poc_tr, poc_va = _ds("meter_poc")
    if not poc_va:
        poc_va = poc_tr
    park_tr, park_va = _ds("meter_real_park")          # auto-labeled from the raw 2K26 park clips
    extra_tr = []
    for d in (extra_datasets or []):
        extra_tr += _extra_dataset_images(d)
    train = synth_train + poc_tr + park_tr + extra_tr
    val = (poc_va + park_va) or poc_tr
    comb_dir = os.path.dirname(out_yaml)
    train_list = os.path.join(comb_dir, "_combined_train.txt")
    val_list = os.path.join(comb_dir, "_combined_val.txt")
    with open(train_list, "w", encoding="utf-8") as f:
        f.write("\n".join(train) + "\n")
    with open(val_list, "w", encoding="utf-8") as f:
        f.write("\n".join(val) + "\n")
    with open(out_yaml, "w", encoding="utf-8") as f:
        f.write(f"train: {train_list}\nval: {val_list}\nnames:\n  0: meter\n")
    extra_note = f" + {len(extra_tr)} extra" if extra_tr else ""
    print(f"combined train: {len(synth_train)} synth + {len(poc_tr)} poc + {len(park_tr)} park-clip{extra_note} "
          f"| val (REAL): {len(val)}")
    return out_yaml


def training_aug_kwargs(luma: bool) -> dict:
    """YOLO augmentation kwargs for m.train(). The DEFAULT set (luma=False) is exactly what this
    trainer has always passed: HUD-appropriate geometry only (no rotate/shear/perspective; a HUD
    meter doesn't rotate/skew in world space) with scale/translate/mosaic so the meter appears at
    varied size/position.

    LUMA mode (Y replicated x3 for the compressed Chiaki path) additionally zeroes HUE and
    SATURATION jitter -- both meaningless on a replicated-gray image -- while keeping the VALUE
    (brightness) jitter, which still helps. Every geometry kwarg is left exactly as above."""
    kw = dict(
        degrees=0, shear=0, perspective=0,     # a HUD meter doesn't rotate/skew in world space
        mosaic=0.5, scale=0.5, translate=0.2,  # scale/translate = meter appears at varied size/pos
    )
    if luma:
        kw.update(hsv_h=0.0, hsv_s=0.0, hsv_v=0.4)   # gray input: no hue/sat aug; keep brightness
    return kw


def extra_dataset_error(extra_datasets, root):
    """Return a clear multi-line error listing any --extra-dataset dirs that do not exist (the
    rung re-encodes / dual-capture yolo/ dirs, which may not be built yet), or None if all are
    present. Relative paths resolve against `root`."""
    missing = [d for d in (extra_datasets or [])
               if not os.path.isdir(d if os.path.isabs(d) else os.path.join(root, d))]
    if not missing:
        return None
    lines = ["ERROR: --extra-dataset dir(s) not found "
             "(build the rung re-encodes / dual-capture yolo/ dirs first):"]
    lines += [f"  missing: {d}" for d in missing]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join("datasets", "meter_fullframe_synth", "meter.yaml"))
    ap.add_argument("--with-real", action="store_true", help="combine synthetic + datasets/meter_poc; val on real")
    ap.add_argument("--base", default="yolo11n.pt")
    ap.add_argument("--epochs", type=int, default=60)
    # imgsz 768 balances thin-meter recall vs GPU time on the RTX 3060 (960 -> ~8h for 60 ep on the full
    # combined set; 768 -> ~half). A LOCATOR only needs a rough box; the ROI reader reads precisely.
    # imgsz default resolves at runtime: 768 normally, 960 for --luma (the LUMA retrain recipe),
    # unless the user passes an explicit --imgsz. None here keeps the no-flag path byte-identical.
    ap.add_argument("--imgsz", type=int, default=None, help="larger -> thin meter resolves better, slower")
    ap.add_argument("--name", default="meter_n")
    # workers>0 cuts wall time ~2-3x (main-thread decode of 1080p frames is the bottleneck); if the
    # Windows DataLoader spawn crashes at startup, relaunch with --workers 0 (loses ~1 min).
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--patience", type=int, default=15, help="epochs of no val improvement before early-stop")
    # --- LUMA-domain retrain for the compressed Chiaki path (Y replicated x3) ---
    ap.add_argument("--luma", action="store_true",
                    help="LUMA retrain (Y x3): zero hue/sat aug, fold in --extra-dataset rung dirs, "
                         "imgsz 960, deploy models/orion_meter_luma_v1.pt")
    ap.add_argument("--extra-dataset", action="append", default=None, metavar="DIR",
                    help="repeatable: extra dataset dir (rung re-encodes / dual-capture yolo/) folded "
                         "into the --luma combined training set")
    ap.add_argument("--out", default=None,
                    help="deploy path for best.pt (default models/orion_meter_n.pt; "
                         "models/orion_meter_luma_v1.pt with --luma)")
    args = ap.parse_args()

    data = args.data if os.path.isabs(args.data) else os.path.join(ROOT, args.data)
    if not os.path.exists(data):
        print(f"data yaml not found: {data}\n  run: python tools/diagnostics/synth_meter_fullframe.py --n 4000")
        return 1
    if args.luma:
        err = extra_dataset_error(args.extra_dataset, ROOT)   # fail fast: rung/yolo dirs may not exist yet
        if err:
            print(err)
            return 1
    if args.with_real or args.luma:
        data = _write_combined_yaml(os.path.dirname(data),
                                    os.path.join(os.path.dirname(data), "meter_combined.yaml"),
                                    extra_datasets=(args.extra_dataset if args.luma else None))
    imgsz = args.imgsz if args.imgsz is not None else (960 if args.luma else 768)

    os.environ.setdefault("YOLO_VERBOSE", "False")
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")   # opt out of mlflow file-store crash
    from ultralytics import YOLO
    try:  # disable auto-enabled logging integrations (mlflow file-store is in maintenance mode -> crashes)
        from ultralytics import settings as _ul_settings
        _ul_settings.update({"mlflow": False, "clearml": False, "comet": False, "dvc": False, "wandb": False})
    except Exception:
        pass

    m = YOLO(args.base)
    # ABSOLUTE project dir: a relative one gets prefixed with ultralytics' runs_dir (runs/detect/...),
    # which is how earlier monitoring polled a nonexistent results.csv.
    project = os.path.join(ROOT, "logs", "diagnostics", "meter_train")
    m.train(data=data, epochs=args.epochs, imgsz=imgsz, batch=16, device=0,
            workers=args.workers,
            project=project, name=args.name, patience=args.patience,   # early-stop when val plateaus
            **training_aug_kwargs(args.luma))   # geometry aug (+ luma hue/sat zeroing) — see function
    # Deploy target: explicit --out wins, else models/orion_meter_luma_v1.pt for --luma, else the
    # historical models/orion_meter_n.pt default (forward-slash literal keeps the DONE line stable).
    if args.out:
        out_rel = args.out
    elif args.luma:
        out_rel = "models/orion_meter_luma_v1.pt"
    else:
        out_rel = "models/orion_meter_n.pt"
    out_path = out_rel if os.path.isabs(out_rel) else os.path.join(ROOT, out_rel)
    # Copy best.pt from the trainer's ACTUAL save dir (auto-increments to <name>2 etc. when the dir
    # exists) — a hardcoded path silently ships NO model on any rerun.
    best = getattr(getattr(m, "trainer", None), "best", None)
    if best and os.path.exists(best):
        import shutil
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        shutil.copy(best, out_path)
        print(f"DONE {best} -> {out_rel}")
    else:
        print(f"WARNING: best.pt not found (trainer.best={best}); {out_rel} NOT updated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
