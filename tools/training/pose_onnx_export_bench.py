"""Export the fine-tuned pose model to ONNX and benchmark the REAL inference path.

WHY THIS EXISTS
---------------
pose_export_benchmark.py measures the .pt model through the ultralytics Python
wrapper. On this rig that produced a nonsense ordering:

    full 640px FP32   13.92 ms
    ROI  256px FP32   15.91 ms      <- a 6x smaller input, SLOWER

A smaller input cannot cost more compute. That ordering is the signature of a
fixed per-call tax (ultralytics pre/post-process, NMS-ish decode, tensor
marshalling, Python dispatch) swamping the convolutions. Shrinking imgsz cannot
help when the constant dominates.

ONNX Runtime removes that wrapper entirely: one session.run() on a pre-shaped
NCHW float array. This script measures whether that actually collapses the
constant, which is the ONLY question that decides if No-Meter mode can run live
inside a 16.7 ms frame budget.

Reports raw session.run() time. Real deployment adds letterbox + decode, so
treat these as the floor, not the delivered number.

    python tools/training/pose_onnx_export_bench.py --model <best.pt>
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

import numpy as np


def bench(sess, inp_name: str, shape, runs: int, warmup: int = 15):
    x = np.random.rand(*shape).astype(np.float32)
    for _ in range(warmup):
        sess.run(None, {inp_name: x})
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        sess.run(None, {inp_name: x})
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return {
        "mean": statistics.mean(times),
        "median": times[len(times) // 2],
        "p90": times[int(len(times) * 0.90)],
        "min": times[0],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="path to the .pt weights")
    ap.add_argument("--sizes", default="256,320,640",
                    help="comma-separated square imgsz values to export+bench")
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--outdir", default="models/onnx")
    ap.add_argument("--half", action="store_true", help="also export FP16 ONNX")
    args = ap.parse_args()

    try:
        import onnxruntime as ort
    except ImportError:
        sys.exit("onnxruntime not installed in this interpreter")
    from ultralytics import YOLO

    providers = ort.get_available_providers()
    use = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in providers]
    if "CUDAExecutionProvider" not in use:
        print("WARNING: CUDA provider unavailable -- numbers below are CPU and meaningless "
              "for the live budget.")

    os.makedirs(args.outdir, exist_ok=True)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    print(f"model     : {args.model}")
    print(f"providers : {use}")
    print(f"runs      : {args.runs}\n")
    print(f"{'config':<34}{'median':>9}{'mean':>9}{'p90':>9}{'min':>9}")
    print("-" * 70)

    for sz in sizes:
        for half in ([False, True] if args.half else [False]):
            tag = f"onnx {sz}px {'FP16' if half else 'FP32'}"
            try:
                # Ultralytics writes <weights_stem>.onnx next to the weights.
                model = YOLO(args.model)
                path = model.export(format="onnx", imgsz=sz, half=half,
                                    dynamic=False, simplify=True, verbose=False)
                if not isinstance(path, str):
                    path = str(path)
                dest = os.path.join(args.outdir, f"pose_{sz}{'_fp16' if half else ''}.onnx")
                if os.path.abspath(path) != os.path.abspath(dest):
                    try:
                        if os.path.exists(dest):
                            os.remove(dest)
                        os.replace(path, dest)
                    except OSError:
                        dest = path
            except Exception as exc:
                print(f"{tag:<34}  EXPORT FAILED: {exc}")
                continue

            try:
                so = ort.SessionOptions()
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                sess = ort.InferenceSession(dest, so, providers=use)
                iname = sess.get_inputs()[0].name
                r = bench(sess, iname, (1, 3, sz, sz), args.runs)
                print(f"{tag:<34}{r['median']:>8.2f}{r['mean']:>9.2f}"
                      f"{r['p90']:>9.2f}{r['min']:>9.2f}")
            except Exception as exc:
                print(f"{tag:<34}  BENCH FAILED: {exc}")

    print("\n  Budget: one 60fps frame = 16.67 ms. The pose anchor must also leave room for "
          "ball detection,\n  the user classifier, the meter read and bot logic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
