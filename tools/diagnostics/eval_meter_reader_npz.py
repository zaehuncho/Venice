#!/usr/bin/env python3
"""Score a trained meter_reader model's FILL head against a pre-extracted crop npz.

Used for the CROSS-SESSION held-out check: train on one session's real crops (+ synthetic),
evaluate on the OTHER session's real crops (never seen). No YOLO needed (crops are pre-cut).
Reports fill MAE / bias / median + a per-fill-bin breakdown.

USAGE:
  py tools/diagnostics/eval_meter_reader_npz.py --npz datasets/meter_reader_real_210801/meter_reader_real.npz
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "training"))
import train_meter_reader as T  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "meter_reader.pt"))
    ap.add_argument("--label", default="")
    ap.add_argument("--split", default="all", choices=["all", "val"])
    args = ap.parse_args()

    import torch
    d = np.load(args.npz)
    images, labels = d["images"], d["labels"]
    ih = int(d["img_h"]) if "img_h" in d else 96
    iw = int(d["img_w"]) if "img_w" in d else 32
    if args.split == "val" and "split" in d:
        m = d["split"] == 1
        images, labels = images[m], labels[m]
    X = np.stack([T.preprocess(im, ih, iw) for im in images]).astype(np.float32)
    Y = labels[:, 0].astype(np.float32)

    net = T._make_net(torch)
    net.load_state_dict(torch.load(args.model, map_location="cpu", weights_only=True))
    net.eval()
    with torch.no_grad():
        P = net(torch.from_numpy(X)).numpy()[:, 0]

    err = (P - Y) * 100.0
    ae = np.abs(err)
    tag = args.label or os.path.basename(args.npz)
    print(f"[{tag}] N={len(Y)}  fill MAE={ae.mean():.2f}%  median={np.median(ae):.2f}%  "
          f"bias(model-label)={err.mean():+.2f}%  p90={np.percentile(ae,90):.2f}%")
    ft = Y * 100.0
    for lo, hi in [(0, 40), (40, 60), (60, 80), (80, 95), (95, 101)]:
        mm = (ft >= lo) & (ft < hi)
        if mm.any():
            print(f"    fill[{lo:3d}-{hi:3d}]%  n={int(mm.sum()):4d}  MAE={ae[mm].mean():.2f}%  bias={err[mm].mean():+.2f}%")


if __name__ == "__main__":
    main()
