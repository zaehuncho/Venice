#!/usr/bin/env python3
"""Efficiency wrapper around the pose student fine-tune: backbone freeze + progressive
resolution + AMP/optimizer pins (docs/NO_METER_MODE_TRAINING.md Stage 4b).

pose_finetune.py is load-bearing and stays BYTE-IDENTICAL (the owner uses it elsewhere).
Its CLI cannot express freeze/amp/lr, so this wrapper replicates its exact Ultralytics
train() surface (pose_finetune.py:34-38 -- device 0, project logs/diagnostics/pose_train,
patience 15, verbose/plots off, exist_ok) and extends it with three wins:

  1. Backbone freeze (default freeze=10): layers 0..9 are the COCO-pretrained pose
     backbone (conv stem + C2f/C3k2 blocks + SPPF) -- generic person-pose features that
     2K avatars do not invalidate. Only the head needs domain adaptation, so freezing the
     backbone cuts the backward pass ~40% and reduces overfitting risk on the 1040-clip
     corpus. NOTE: the default student yolo11n-pose carries an extra C2PSA block at index
     10; --freeze 11 freezes the full YOLO11 backbone. --verbose-freeze prints the exact
     per-layer table; --no-freeze is the escape hatch.
  2. Progressive resolution (default --phases 320:20,640:10): 20 epochs at imgsz 320,
     then 10 at 640 with the phase-2 model INITIALIZED FROM the phase-1 best.pt.
     (Deliberately NOT Ultralytics resume=True: resume restores the checkpoint's OWN
     imgsz/epochs and refuses a changed config -- loading best.pt as the next phase's
     starting weights IS the cross-resolution weight carry.) --single-phase bypasses
     (then --imgsz/--epochs behave exactly like pose_finetune.py).
  3. AMP + optimizer pins: amp=True is passed EXPLICITLY every phase (the Ultralytics
     default is True, but a stray cfg override would silently cost the ~1.8x -- pin it,
     log it); cos_lr=True + warmup_epochs=3; freeze mode pins AdamW lr0=1e-3 (a frozen
     backbone will not diverge at the higher lr). A training_config.json lands next to
     the final weights so eval knows exactly what produced them.

Active-learning boost (tools/training/pose_active_learning.py closes the loop):
--boost-weight N oversamples hand-labeled train frames N x by file duplication (stems
listed in <dataset>/hand_labeled.txt if present, else the al_ stem prefix the
active-learning export writes). Ultralytics exposes no per-sample loss-weight hook;
N x duplication == N x loss weight in expectation. __boost copies are cleaned and
re-made on every run (idempotent, safe to change N).

Usage:
  python tools/training/pose_finetune_efficient.py [--data logs/diagnostics/pose_ds/pose2k.yaml] \
      [--student yolo11n-pose.pt] [--phases 320:20,640:10] [--freeze 10] [--batch 16] \
      [--name pose2k_n_eff] [--boost-weight 1] [--single-phase] [--imgsz 640] [--epochs 40] \
      [--no-freeze] [--verbose-freeze] [--lr0 0.001] [--warmup-epochs 3] [--patience 15]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The base run layout, mirrored from pose_finetune.py:34-38 (UNMODIFIED base script).
PROJECT_REL = os.path.join("logs", "diagnostics", "pose_train")
FREEZE_DEFAULT = 10  # yolov8*-pose backbone = layers 0..9 (C2f blocks + SPPF); head stays live


# ==============================================================================================
# CUDA preflight (task #69: fail EARLY and LOUD, never silently 8fps / CPU-crawl)
# ==============================================================================================

def preflight_cuda_torch():
    """Abort with the exact fix commands unless CUDA torch + ultralytics are importable."""
    py = sys.executable
    fix = (
        "\n"
        "FIX (run in the venv/python you will train with):\n"
        f'  "{py}" -m pip uninstall -y torch torchvision\n'
        f'  "{py}" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126\n'
        f'  "{py}" -m pip install ultralytics\n'
        "\n"
        "Never train on CPU torch: CPU inference was the 2026-07-06 0/28 batch root cause\n"
        "(8fps detector). This rig's system torch may be the +cpu build -- 'import torch\n"
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
    print(f"preflight OK: torch {torch.__version__} CUDA={torch.version.cuda} "
          f"device={torch.cuda.get_device_name(0)}")
    return True


# ==============================================================================================
# Base-surface drift guard (we replicate pose_finetune.py's kwargs; warn if it changes)
# ==============================================================================================

def warn_if_base_drifted():
    """The wrapper mirrors pose_finetune.py's train() kwargs verbatim. If the base script's
    config surface changes, this warns instead of silently training with stale settings."""
    base = os.path.join(ROOT, "tools", "training", "pose_finetune.py")
    tokens = ('project="logs/diagnostics/pose_train"', "patience=15", "device=0",
              "plots=False", "exist_ok=True")
    try:
        with open(base, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return
    missing = [t for t in tokens if t not in src]
    if missing:
        print(f"WARNING: pose_finetune.py config surface drifted (missing {missing}) -- "
              "re-sync the replicated train() kwargs in pose_finetune_efficient.py")


# ==============================================================================================
# Phase spec + boost oversampling
# ==============================================================================================

def parse_phases(spec):
    """'320:20,640:10' -> [(320, 20), (640, 10)] with validation."""
    phases = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            imgsz_s, epochs_s = part.split(":")
            imgsz, epochs = int(imgsz_s), int(epochs_s)
        except ValueError:
            print(f'bad --phases entry "{part}" (want IMGSZ:EPOCHS, e.g. 320:20)')
            raise SystemExit(2)
        if imgsz <= 0 or imgsz % 32 or epochs <= 0:
            print(f'bad --phases entry "{part}" (imgsz must be a positive multiple of 32, '
                  "epochs positive)")
            raise SystemExit(2)
        phases.append((imgsz, epochs))
    if not phases:
        print(f'--phases "{spec}" parsed to nothing')
        raise SystemExit(2)
    return phases


def dataset_dir_from_yaml(data_yaml):
    """Resolve the dataset root: the yaml's 'path:' line if present, else its directory."""
    try:
        with open(data_yaml, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("path:"):
                    p = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if p:
                        return p if os.path.isabs(p) else os.path.join(ROOT, p)
    except OSError:
        pass
    return os.path.dirname(os.path.abspath(data_yaml))


def apply_boost(ds_dir, weight):
    """Oversample hand-labeled train frames weight-x by duplication (== loss weight in
    expectation; Ultralytics has no per-sample weight hook). Stems come from
    <dataset>/hand_labeled.txt when present, else any train stem prefixed al_ (the
    pose_active_learning.py export convention). Idempotent: stale __boost copies from
    earlier runs are always removed first, so changing --boost-weight is safe."""
    imgs = os.path.join(ds_dir, "images", "train")
    lbls = os.path.join(ds_dir, "labels", "train")
    removed = 0
    for d in (imgs, lbls):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if "__boost" in fn:
                os.remove(os.path.join(d, fn))
                removed += 1
    if weight <= 1:
        if removed:
            print(f"boost: cleaned {removed} stale __boost copies (boost off)")
        return 0

    stems, src = set(), "al_ stem prefix"
    listing = os.path.join(ds_dir, "hand_labeled.txt")
    if os.path.isfile(listing):
        with open(listing, "r", encoding="utf-8") as fh:
            stems = {ln.strip() for ln in fh if ln.strip()}
        src = "hand_labeled.txt"
    elif os.path.isdir(lbls):
        stems = {fn[:-4] for fn in os.listdir(lbls)
                 if fn.startswith("al_") and fn.endswith(".txt")}
    if not stems:
        print(f"boost: --boost-weight {weight} requested but no hand-labeled train frames "
              f"found ({src}; add labels via pose_active_learning.py first) -- continuing unboosted")
        return 0

    boosted = 0
    for stem in sorted(stems):
        lp = os.path.join(lbls, stem + ".txt")
        ip = ""
        for ext in (".jpg", ".jpeg", ".png"):
            cand = os.path.join(imgs, stem + ext)
            if os.path.isfile(cand):
                ip = cand
                break
        if not os.path.isfile(lp) or not ip:
            print(f"  boost: skip {stem} (image or label missing from the train split)")
            continue
        for k in range(2, weight + 1):
            shutil.copy2(ip, os.path.join(imgs, f"{stem}__boost{k}{os.path.splitext(ip)[1]}"))
            shutil.copy2(lp, os.path.join(lbls, f"{stem}__boost{k}.txt"))
        boosted += 1
    print(f"boost: {boosted} hand-labeled train frames x{weight} via duplication ({src}"
          f"{'; cleaned ' + str(removed) + ' stale copies' if removed else ''})")
    return boosted


# ==============================================================================================
# Freeze plan logging
# ==============================================================================================

def log_freeze_plan(model, freeze_n, verbose):
    """Preview which layers Ultralytics freeze=<n> will lock (it flips requires_grad at
    train() time for indices 0..n-1). Summary always; per-layer table with --verbose-freeze."""
    try:
        layers = list(model.model.model)
    except Exception:
        print("freeze: could not enumerate model layers (unexpected model structure)")
        return
    frozen_p = trainable_p = 0
    rows = []
    for i, layer in enumerate(layers):
        n_p = sum(int(t.numel()) for t in layer.parameters())
        frozen = i < freeze_n
        frozen_p += n_p * frozen
        trainable_p += n_p * (not frozen)
        rows.append((i, getattr(layer, "type", type(layer).__name__), n_p, frozen))
    total = max(frozen_p + trainable_p, 1)
    print(f"freeze plan: {freeze_n} layers (0..{freeze_n - 1}) locked = "
          f"{frozen_p:,} params ({100.0 * frozen_p / total:.1f}%); "
          f"{trainable_p:,} trainable in layers {freeze_n}..{len(layers) - 1}")
    if verbose:
        for i, tname, n_p, frozen in rows:
            print(f"  layer {i:2d}  {'FROZEN   ' if frozen else 'trainable'}  "
                  f"{n_p:>10,} params  {tname}")


# ==============================================================================================
# Main
# ==============================================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join("logs", "diagnostics", "pose_ds", "pose2k.yaml"))
    ap.add_argument("--student", default="yolo11n-pose.pt")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--name", default="pose2k_n_eff")
    # 2026-08-09 host-throughput wins. This pipeline is DATALOADER-bound, not GPU-bound
    # (stage-1 extraction ran at 9% GPU; a nano student at batch 16 underfeeds a 3060).
    ap.add_argument("--workers", type=int, default=8,
                    help="dataloader workers (Ultralytics default 8). This rig is 8C/16T; "
                         "beyond ~12 the physical cores thrash and it gets slower")
    ap.add_argument("--cache", default="none", choices=["disk", "ram", "none"],
                    help="MEASURED 2026-08-09, default deliberately 'none'. 'disk' pre-decodes "
                         "to .npy, but the cache is ~10x the JPEG corpus (46.8 GB of .npy vs "
                         "4.9 GB of jpgs for 40k frames) and training then does SHUFFLED RANDOM "
                         "reads across all of it every epoch -- catastrophic unless the cache "
                         "lives on NVMe. This rig's D: is a 5400 RPM HDD; caching there was far "
                         "slower than just decoding. 'ram' needs the same ~46 GB resident (won't "
                         "fit 32 GB). Keep the dataset on the NVMe and decode on the fly with "
                         "--workers 12; an 8-core Ryzen keeps a 3060 fed comfortably.")
    ap.add_argument("--freeze", type=int, default=FREEZE_DEFAULT,
                    help="freeze the first N layers (default 10 = the pose backbone; "
                         "yolo11n-pose has C2PSA at index 10 -- use 11 to lock it too)")
    ap.add_argument("--no-freeze", action="store_true", help="escape hatch: train all layers")
    ap.add_argument("--verbose-freeze", action="store_true",
                    help="print the per-layer frozen/trainable table before training")
    ap.add_argument("--phases", default="320:20,640:10",
                    help='progressive-resolution schedule "IMGSZ:EPOCHS,..." '
                         "(weights carried across phases via each phase's best.pt)")
    ap.add_argument("--single-phase", action="store_true",
                    help="bypass progressive resolution; train once at --imgsz for --epochs")
    ap.add_argument("--imgsz", type=int, default=640, help="only used with --single-phase")
    ap.add_argument("--epochs", type=int, default=40, help="only used with --single-phase")
    ap.add_argument("--lr0", type=float, default=None,
                    help="base lr; default 1e-3 in freeze mode (frozen backbone won't "
                         "diverge), Ultralytics auto otherwise")
    ap.add_argument("--warmup-epochs", type=float, default=3.0)
    ap.add_argument("--patience", type=int, default=15, help="mirrors pose_finetune.py")
    ap.add_argument("--boost-weight", type=int, default=1,
                    help="oversample hand-labeled (al_* / hand_labeled.txt) train frames N x")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")

    # ---- Stage 0: CUDA preflight (ALWAYS first; task #69) ------------------------------------
    if not preflight_cuda_torch():
        return 2
    warn_if_base_drifted()

    data = args.data if os.path.isabs(args.data) else os.path.join(ROOT, args.data)
    if not os.path.isfile(data):
        print(f"no dataset yaml: {data} (run pose_pseudolabel_dataset.py / no_meter_mode_prep.py first)")
        return 2
    project = os.path.join(ROOT, PROJECT_REL)

    freeze_n = 0 if args.no_freeze else max(0, args.freeze)
    phases = [(args.imgsz, args.epochs)] if args.single_phase else parse_phases(args.phases)

    # ---- Win 3: AMP pinned + optimizer defaults (logged up front) ----------------------------
    print("AMP: pinned ON (amp=True passed explicitly every phase; the Ultralytics default is "
          "True but a silent cfg override would cost the ~1.8x -- never trust the default)")
    opt_kwargs = {"cos_lr": True, "warmup_epochs": args.warmup_epochs}
    if freeze_n > 0:
        opt_kwargs.update(optimizer="AdamW", lr0=args.lr0 if args.lr0 is not None else 1e-3)
    elif args.lr0 is not None:
        opt_kwargs.update(optimizer="AdamW", lr0=args.lr0)
    # else: Ultralytics 'auto' optimizer picks its own lr (full fine-tune, conservative)
    print(f"optimizer: {opt_kwargs.get('optimizer', 'auto')}"
          f"{' lr0=' + str(opt_kwargs['lr0']) if 'lr0' in opt_kwargs else ''} "
          f"cos_lr=True warmup_epochs={args.warmup_epochs}")

    # ---- Boost (active-learning loop) --------------------------------------------------------
    n_boosted = apply_boost(dataset_dir_from_yaml(data), args.boost_weight)

    from ultralytics import YOLO

    # ---- Win 1 + 2: freeze + progressive-resolution phases -----------------------------------
    weights = args.student
    phase_logs = []
    t_all = time.time()
    for pi, (imgsz, epochs) in enumerate(phases, start=1):
        run_name = args.name if len(phases) == 1 else f"{args.name}_phase{pi}"
        m = YOLO(weights)
        if pi == 1:
            if freeze_n > 0:
                log_freeze_plan(m, freeze_n, args.verbose_freeze)
                print("freeze applies to every phase (the head keeps adapting; the backbone stays COCO)")
            else:
                print("freeze: OFF (--no-freeze) -- full fine-tune, expect ~1.6x the step time")
        print(f"\nphase {pi}/{len(phases)}: imgsz={imgsz} epochs={epochs} "
              f"start-weights={weights} -> run {run_name}")
        t0 = time.time()
        train_kwargs = dict(
            # mirrored from pose_finetune.py:34-38 (base script UNMODIFIED)
            data=data, epochs=epochs, imgsz=imgsz, batch=args.batch,
            device=0, project=project, name=run_name, exist_ok=True,
            patience=args.patience, verbose=False, plots=False,
            # Stage 4b extensions
            amp=True, **opt_kwargs,
        )
        # Host-throughput: the bottleneck here is feeding the GPU, not the GPU itself.
        train_kwargs["workers"] = args.workers
        if args.cache != "none":
            # Ultralytics wants cache=True for RAM, "disk" for the .npy path.
            train_kwargs["cache"] = True if args.cache == "ram" else "disk"
        if freeze_n > 0:
            train_kwargs["freeze"] = freeze_n
        m.train(**train_kwargs)
        dt = time.time() - t0
        best = os.path.join(project, run_name, "weights", "best.pt")
        if not os.path.isfile(best):
            print(f"ERROR: phase {pi} produced no best.pt at {best}")
            return 1
        phase_logs.append({"phase": pi, "imgsz": imgsz, "epochs": epochs,
                           "start_weights": weights, "best": best,
                           "wall_clock_s": round(dt, 1)})
        print(f"phase {pi} done in {dt / 60.0:.1f} min -> {best}")
        weights = best  # NOT resume=True: best.pt as next-phase init IS the weight carry

    final_best = weights
    total_s = time.time() - t_all

    # ---- training_config.json (eval must know what produced the weights) ---------------------
    import torch
    import ultralytics
    config = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wrapper": "tools/training/pose_finetune_efficient.py",
        "base_surface": "pose_finetune.py:34-38 (unmodified base; kwargs replicated here)",
        "student": args.student,
        "data": os.path.abspath(data),
        "batch": args.batch,
        "workers": args.workers,
        "cache": args.cache,
        "patience": args.patience,
        "freeze": freeze_n,
        "freeze_note": ("layers 0..%d frozen (COCO pose backbone); head trainable" % (freeze_n - 1))
                       if freeze_n else "no layers frozen (--no-freeze)",
        "amp": True,
        "cos_lr": True,
        "warmup_epochs": args.warmup_epochs,
        "optimizer": opt_kwargs.get("optimizer", "auto"),
        "lr0": opt_kwargs.get("lr0"),
        "boost_weight": args.boost_weight,
        "boosted_frames": n_boosted,
        "phases": phase_logs,
        "total_wall_clock_s": round(total_s, 1),
        "final_weights": final_best,
        "versions": {"torch": torch.__version__, "ultralytics": ultralytics.__version__,
                     "cuda": torch.version.cuda, "device": torch.cuda.get_device_name(0)},
    }
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(final_best)), "training_config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    print(f"\nDONE in {total_s / 60.0:.1f} min. efficient fine-tuned student weights: {final_best}")
    print(f"training config: {cfg_path}")
    print("next:")
    print(f'  python tools/training/pose_eval_compare.py  (against {os.path.basename(final_best)})')
    print(f'  python tools/training/pose_active_learning.py --model "{final_best}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
