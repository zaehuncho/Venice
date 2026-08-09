#!/usr/bin/env python3
"""Evaluate a trained cue temporal model on the owner-only val split, with per-clip breakdown.

Rebuilds the EXACT val split and window set the trainer used (split seed, val-frac, window,
jitter seed, cue tolerance all come from the checkpoint's stored config, overridable) and
reports: timing MAE (ms), green-window hit rate (|error| <= 30ms), per-class cue F1, cue
confusion matrix, and a per-clip table -- the per-clip view is what exposes "one bad video"
problems (right-fade-style clusters) that corpus averages hide.

CPU is fine here (the model is ~0.3M params); no CUDA gate. Requires the sequence cache the
trainer wrote next to the labels JSON -- run train_cue_temporal_model.py first.

Labels must carry the 2026-08-08 schema (per-cue `video_frame`, per-shot `handedness` +
`player_bbox_at_release`); older JSONs fail loud via the trainer's shared loader with a
"regenerate labels" message. Handedness resolves per shot: labels field -> the trainer
config's --handedness -> 'right' (DEBUG-logged, --debug to see it).

Usage:
  python tools/training/eval_cue_temporal_model.py \
      --checkpoint logs/diagnostics/cue_temporal/cue_temporal_model.pt \
      [--labels <override>] [--device cpu] [--debug]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cue_temporal_model import CUE_CLASSES, build_model  # noqa: E402
from train_cue_temporal_model import (  # noqa: E402
    build_dataset, build_split, cache_path_for, compute_metrics, load_cache, load_shots,
    predict_in_batches,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkpoint", default=os.path.join("logs", "diagnostics", "cue_temporal",
                                                         "cue_temporal_model.pt"))
    ap.add_argument("--labels", default="", help="override the labels JSON stored in the checkpoint")
    ap.add_argument("--device", default="cpu", help="cpu (default) or cuda")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--debug", action="store_true",
                    help="DEBUG logging (per-shot handedness/player-lock fallback provenance)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s")

    try:
        import torch
    except Exception as exc:
        print(f"torch is not importable ({exc}).\n"
              "Eval runs fine on CPU torch; on the training rig prefer the CUDA build "
              "(see train_cue_temporal_model.py preflight for the pip commands).")
        return 2

    ckpt_path = args.checkpoint if os.path.isabs(args.checkpoint) else os.path.join(ROOT, args.checkpoint)
    if not os.path.isfile(ckpt_path):
        print(f"checkpoint not found: {ckpt_path} (run train_cue_temporal_model.py first)")
        return 2
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    cfg = ckpt["config"]

    labels = args.labels or cfg["labels"]
    labels = labels if os.path.isabs(labels) else os.path.join(ROOT, labels)
    if not os.path.isfile(labels):
        print(f"labels JSON not found: {labels}")
        return 2
    cache_file = cache_path_for(labels)
    cache = load_cache(cache_file)
    if not cache:
        print(f"sequence cache not found/empty: {cache_file}\n"
              "run train_cue_temporal_model.py first -- it extracts and caches the sequences "
              "(eval never re-reads video, so it stays CPU-cheap)")
        return 2

    shots, _ = load_shots(labels)
    _train_shots, val_shots, contaminated = build_split(shots, cfg["val_frac"], cfg["seed"])
    if contaminated:
        print(f"ERROR: val contains {len(contaminated)} non-owner shots -- aborting")
        return 3
    if not val_shots:
        print("ERROR: val split is empty")
        return 1
    Xva, tva, cva, mva, vva, stats = build_dataset(
        val_shots, cache, cfg["window"], cfg["per_shot"], cfg["seed"] + 1,
        cfg["handedness"], cfg["cue_tol"], cfg["max_arm_missing"])
    if not len(Xva):
        print(f"no val windows built (stats: {stats})")
        return 1

    device = torch.device(args.device)
    model = build_model(channels=cfg["channels"], kernel=cfg["kernel"],
                        dilations=cfg["dilations"], dropout=cfg["dropout"]).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    pt, pc = predict_in_batches(model, torch, Xva, device, batch=args.batch)
    m = compute_metrics(pt, tva, mva, pc, cva)

    print(f"checkpoint: {ckpt_path} (best epoch {ckpt.get('epoch', '?')})")
    print(f"val: {len(Xva)} windows from {len(set(vva))} clips / {len(val_shots)} shots  "
          f"(rejects: {stats})")
    print(f"\ntiming MAE: {m['timing_mae_ms']} ms")
    print(f"green-window hit rate (+-30ms): {100 * m['green_hit_rate']:.1f}%")
    print(f"cue accuracy: {100 * m['cue_acc']:.1f}%")
    print("\ncue F1 per class:")
    for name in CUE_CLASSES:
        print(f"  {name:<10} {m['cue_f1'][name]:.3f}")

    # ---- confusion matrix ---------------------------------------------------------------------
    ncls = len(CUE_CLASSES)
    conf = np.zeros((ncls, ncls), dtype=int)
    for t, p in zip(cva, pc):
        conf[t, p] += 1
    hdr = " ".join(f"{n[:6]:>7}" for n in CUE_CLASSES)
    print(f"\nconfusion (rows=true, cols=pred):\n{'':<11}{hdr}")
    for i, name in enumerate(CUE_CLASSES):
        print(f"{name:<11}" + " ".join(f"{conf[i, j]:>7}" for j in range(ncls)))

    # ---- per-clip breakdown -------------------------------------------------------------------
    err_ms = (pt - tva) * mva
    print(f"\n{'clip':<42} {'n':>5} {'MAE ms':>8} {'hit%':>6} {'cue-acc%':>9}")
    print("-" * 74)
    vids = np.asarray(vva)
    for vid in sorted(set(vva)):
        sel = vids == vid
        mae = float(np.mean(np.abs(err_ms[sel])))
        hit = 100.0 * float(np.mean(np.abs(err_ms[sel]) <= 30.0))
        acc = 100.0 * float(np.mean(pc[sel] == cva[sel]))
        print(f"{vid[:42]:<42} {int(sel.sum()):>5} {mae:>8.1f} {hit:>6.1f} {acc:>9.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
