"""anchor_thread_budget.py -- can a pose model run BESIDE the detect thread without stalling it?

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverable 2, "Cost").

The live rule is "nothing may stall the detect thread" (the 2026-09-14 GIL regression).  The
raw p50 of the pose model is not the question -- the question is what it does to the 60 Hz
tick of the thread that reads the meter.  So this simulates the real shape:

  * a DETECT thread that wakes every 16.67 ms and does ~3 ms of numpy work (the meter
    locator's measured 2-8 ms), and records how LATE each wake actually was;
  * a POSE thread that runs the exported ONNX model through onnxruntime in a loop, at a
    chosen duty (every frame / every other frame / press-window only);
  * the same detect thread measured ALONE as the control.

Reported: detect-tick lateness p50/p99/max with and without the pose thread, and the pose
thread's own achieved rate.  onnxruntime releases the GIL inside Run(), so the interesting
number is how much of the 7-9 ms leaks back as Python-side and GPU-contention stall.

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_thread_budget.py
Outputs: D:\\NexusVision\\anchor_study\\thread_budget.json
"""
from __future__ import annotations

import json
import os
import statistics as st
import threading
import time

import numpy as np

OUT = r"D:\NexusVision\anchor_study"
MODEL = os.path.join(OUT, "pose26n_320_fp32.onnx")
TICK = 1.0 / 60.0
SECONDS = 12.0


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def detect_work(buf):
    """~3 ms of the kind of work the meter locator does (HSV-ish masking + column sums)."""
    m = (buf[:, :, 0] > 120) & (buf[:, :, 1] < 90)
    c = m.sum(0)
    return int(c.argmax())


def run(with_pose, duty=1, seconds=SECONDS):
    stop = threading.Event()
    late = []
    pose_lat = []
    prov = []

    def detect():
        rng = np.random.default_rng(3)
        buf = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        nxt = time.perf_counter() + TICK
        while not stop.is_set():
            now = time.perf_counter()
            if now < nxt:
                time.sleep(max(0.0, min(nxt - now, 0.004)))
                continue
            late.append((now - nxt) * 1e3)
            detect_work(buf)
            nxt += TICK
            if nxt < time.perf_counter() - 0.05:
                nxt = time.perf_counter() + TICK

    def pose():
        # onnxruntime-gpu in this venv only finds the CUDA/cuDNN DLLs once torch has
        # loaded them into the process -- without this the session silently falls back
        # to CPU (see thread_budget.json "provider").
        try:
            import torch  # noqa: F401
        except Exception:
            pass
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        sess = ort.InferenceSession(MODEL, so, providers=["CUDAExecutionProvider"])
        prov.append(sess.get_providers()[0])
        n = sess.get_inputs()[0].name
        x = np.ascontiguousarray(
            np.random.default_rng(1).random((1, 3, 320, 320)), dtype=np.float32)
        for _ in range(20):
            sess.run(None, {n: x})
        k = 0
        nxt = time.perf_counter() + TICK
        while not stop.is_set():
            now = time.perf_counter()
            if now < nxt:
                time.sleep(max(0.0, min(nxt - now, 0.003)))
                continue
            nxt += TICK
            k += 1
            if k % duty:
                continue
            t0 = time.perf_counter()
            sess.run(None, {n: x})
            pose_lat.append((time.perf_counter() - t0) * 1e3)

    td = threading.Thread(target=detect, daemon=True)
    tp = threading.Thread(target=pose, daemon=True) if with_pose else None
    td.start()
    if tp:
        tp.start()
    time.sleep(seconds)
    stop.set()
    td.join(2)
    if tp:
        tp.join(5)
    late = late[60:]
    out = {"ticks": len(late),
           "tick_late_p50_ms": round(pct(late, 50), 2),
           "tick_late_p90_ms": round(pct(late, 90), 2),
           "tick_late_p99_ms": round(pct(late, 99), 2),
           "tick_late_max_ms": round(max(late), 2),
           "ticks_over_16.7ms_late_pct": round(
               100.0 * sum(1 for x in late if x > 16.7) / len(late), 2)}
    if prov:
        out["provider"] = prov[0]
    if pose_lat:
        out["pose_infer_p50_ms"] = round(pct(pose_lat, 50), 2)
        out["pose_infer_p99_ms"] = round(pct(pose_lat, 99), 2)
        out["pose_rate_hz"] = round(len(pose_lat) / seconds, 1)
    return out


def main():
    try:
        import torch  # noqa: F401  (loads the CUDA DLLs for onnxruntime)
    except Exception:
        pass
    res = {"model": MODEL, "tick_hz": 60, "seconds": SECONDS, "cases": {}}
    res["cases"]["detect_alone"] = run(False)
    print("detect_alone", res["cases"]["detect_alone"], flush=True)
    for duty, tag in ((1, "pose_every_frame"), (2, "pose_every_2nd"),
                      (4, "pose_every_4th")):
        res["cases"][tag] = run(True, duty)
        print(tag, res["cases"][tag], flush=True)
    with open(os.path.join(OUT, "thread_budget.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print("wrote thread_budget.json")


if __name__ == "__main__":
    main()
