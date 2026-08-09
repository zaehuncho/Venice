#!/usr/bin/env python3
"""Export the cue temporal model to ONNX (opset 17, static shapes) + ORT-CPU parity check.

Matches the sidecar's DirectML-ready shipping pattern: fully static input shape
(1, 51, window), no dynamic axes, opset 17. After export the ONNX is run on 10 val
sequences through onnxruntime-CPU and compared against the torch model; max abs diff must
be < 1e-4 on BOTH heads or the export FAILS (rc 1) -- a silently-diverged export is a
timing bug the bot would ship.

Emits next to the checkpoint: cue_temporal_model.onnx, and stamps an "onnx" section into
cue_temporal_model_meta.json (input/output names, shapes, opset, parity result) -- the
runtime reads the meta for the feature contract.

Usage:
  python tools/training/export_cue_temporal_onnx.py \
      --checkpoint logs/diagnostics/cue_temporal/cue_temporal_model.pt \
      [--out <dir>/cue_temporal_model.onnx] [--opset 17]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cue_temporal_model import INPUT_DIM, build_model  # noqa: E402
from train_cue_temporal_model import (  # noqa: E402
    build_dataset, build_split, cache_path_for, load_cache, load_shots, preflight_cuda_torch,
)

PARITY_TOL = 1e-4
PARITY_N = 10


def _parity_inputs(cfg):
    """10 real val sequences from the trainer's cache; seeded gaussian fallback if absent."""
    labels = cfg["labels"]
    labels = labels if os.path.isabs(labels) else os.path.join(ROOT, labels)
    if os.path.isfile(labels):
        cache = load_cache(cache_path_for(labels))
        if cache:
            shots, _ = load_shots(labels)
            _tr, val_shots, _bad = build_split(shots, cfg["val_frac"], cfg["seed"])
            Xva, _t, _c, _m, _v, _s = build_dataset(
                val_shots, cache, cfg["window"], cfg["per_shot"], cfg["seed"] + 1,
                cfg["handedness"], cfg["cue_tol"], cfg["max_arm_missing"])
            if len(Xva) >= PARITY_N:
                print(f"parity inputs: {PARITY_N} real val sequences from the cache")
                return Xva[:PARITY_N]
    print("parity inputs: cache/labels unavailable -> seeded gaussian stand-ins "
          "(numeric parity only; re-run with the cache present for a real-data check)")
    rng = np.random.default_rng(20260808)
    return rng.standard_normal((PARITY_N, INPUT_DIM, cfg["window"])).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkpoint", default=os.path.join("logs", "diagnostics", "cue_temporal",
                                                         "cue_temporal_model.pt"))
    ap.add_argument("--out", default="", help="output .onnx path (default: next to the checkpoint)")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    # ---- CUDA preflight (task #69: the training rig must never quietly hold a +cpu torch) ----
    if not preflight_cuda_torch():
        return 2
    import torch
    try:
        import onnxruntime as ort
    except Exception as exc:
        print(f"onnxruntime is not importable ({exc}).\n"
              f'FIX:  "{sys.executable}" -m pip install onnxruntime')
        return 2

    ckpt_path = args.checkpoint if os.path.isabs(args.checkpoint) else os.path.join(ROOT, args.checkpoint)
    if not os.path.isfile(ckpt_path):
        print(f"checkpoint not found: {ckpt_path} (run train_cue_temporal_model.py first)")
        return 2
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    cfg = ckpt["config"]
    window = int(cfg["window"])

    # Export + parity both on CPU: deterministic apples-to-apples against ORT-CPU.
    model = build_model(channels=cfg["channels"], kernel=cfg["kernel"],
                        dilations=cfg["dilations"], dropout=cfg["dropout"])
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    onnx_path = args.out or os.path.join(os.path.dirname(ckpt_path), "cue_temporal_model.onnx")
    onnx_path = onnx_path if os.path.isabs(onnx_path) else os.path.join(ROOT, onnx_path)
    dummy = torch.zeros(1, INPUT_DIM, window, dtype=torch.float32)
    torch.onnx.export(
        model, dummy, onnx_path,
        opset_version=args.opset,
        input_names=["pose_window"],
        output_names=["frames_until_release", "cue_logits"],
        do_constant_folding=True,
        dynamic_axes=None,  # fully static (1, 51, window) -- DirectML-friendly
    )
    print(f"exported: {onnx_path} (opset {args.opset}, static shape [1, {INPUT_DIM}, {window}])")

    # ---- ORT-CPU parity check -----------------------------------------------------------------
    X = _parity_inputs(cfg)
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    max_diff = 0.0
    with torch.no_grad():
        for i in range(len(X)):
            xb = X[i:i + 1]
            t_t, t_c = model(torch.from_numpy(xb))
            o_t, o_c = sess.run(None, {"pose_window": xb})
            max_diff = max(max_diff,
                           float(np.max(np.abs(t_t.numpy() - np.asarray(o_t).reshape(-1)))),
                           float(np.max(np.abs(t_c.numpy() - np.asarray(o_c)))))
    ok = max_diff < PARITY_TOL
    print(f"parity: max abs diff {max_diff:.2e} over {len(X)} sequences, both heads "
          f"-> {'PASS' if ok else f'FAIL (tolerance {PARITY_TOL:.0e})'}")

    # ---- stamp the meta json ------------------------------------------------------------------
    meta_path = os.path.join(os.path.dirname(ckpt_path), "cue_temporal_model_meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["onnx"] = {
            "path": os.path.abspath(onnx_path),
            "opset": args.opset,
            "input": {"name": "pose_window", "shape": [1, INPUT_DIM, window], "dtype": "float32"},
            "outputs": {"frames_until_release": [1], "cue_logits": [1, len(cfg["classes"])]},
            "parity_max_abs_diff": max_diff,
            "parity_pass": ok,
            "exported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        print(f"meta updated: {meta_path}")
    else:
        print(f"WARNING: {meta_path} not found -- meta not stamped (trainer writes it)")

    if not ok:
        print("EXPORT FAILED the parity gate; do not ship this ONNX")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
