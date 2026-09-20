"""anchor_pose_bench_trt.py -- TensorRT / small-input follow-up to anchor_pose_bench.py.

Two questions the first pass left open:
  1. is the ~8 ms ORT-CUDA p50 COMPUTE or OVERHEAD?  (bench 128/192/256/320/448 -- if the
     p50 is flat in input size it is overhead, and a smaller crop buys nothing)
  2. does the TensorRT EP (engine cached on D:) cut the p50/p99 enough to fit a 16.7 ms
     detect tick?

Read-only.  Outputs D:\\NexusVision\\anchor_study\\pose_cost_trt.json
"""
from __future__ import annotations

import json
import os
import shutil
import statistics as st
import time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = r"D:\NexusVision\anchor_study"
CACHE = os.path.join(OUT, "trt_cache")
WEIGHTS = os.path.join(REPO, "logs", "diagnostics", "pose_train",
                       "pose2k_n_eff_phase2", "weights", "best.pt")


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarise(name, ms):
    return {"name": name, "n": len(ms), "p50": round(pct(ms, 50), 3),
            "p90": round(pct(ms, 90), 3), "p99": round(pct(ms, 99), 3),
            "max": round(max(ms), 3), "mean": round(st.fmean(ms), 3)}


def crop(sz):
    rng = np.random.default_rng(7)
    return rng.integers(40, 190, size=(sz, sz, 3), dtype=np.uint16).astype(np.uint8)


def pre(bgr, sz, half):
    import cv2
    r = cv2.resize(bgr, (sz, sz))
    x = r[:, :, ::-1].transpose(2, 0, 1)[None]
    return np.ascontiguousarray(x, dtype=np.float16 if half else np.float32) / (255.0)


def main():
    os.makedirs(CACHE, exist_ok=True)
    import onnxruntime as ort
    from ultralytics import YOLO
    res = {"scaling": [], "trt": [], "iobinding": []}

    # 1. input-size scaling on the CUDA EP (fp32)
    for sz in (128, 192, 256, 320, 448, 640):
        path = os.path.join(OUT, f"pose26n_{sz}_fp32.onnx")
        if not os.path.exists(path):
            m = YOLO(WEIGHTS)
            p = m.export(format="onnx", imgsz=sz, simplify=True, opset=17,
                         device="cpu", verbose=False)
            shutil.move(p, path)
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        sess = ort.InferenceSession(path, so, providers=["CUDAExecutionProvider"])
        n = sess.get_inputs()[0].name
        x = pre(crop(sz), sz, False)
        for _ in range(30):
            sess.run(None, {n: x})
        lat = []
        for _ in range(400):
            t0 = time.perf_counter()
            sess.run(None, {n: x})
            lat.append((time.perf_counter() - t0) * 1e3)
        res["scaling"].append(summarise(f"cuda/fp32/{sz}", lat))
        print(res["scaling"][-1], flush=True)
        del sess

    # 2. IO binding at 320 (removes the per-call H2D copy from the timed path)
    try:
        path = os.path.join(OUT, "pose26n_320_fp32.onnx")
        sess = ort.InferenceSession(path, providers=["CUDAExecutionProvider"])
        n = sess.get_inputs()[0].name
        x = pre(crop(320), 320, False)
        xo = ort.OrtValue.ortvalue_from_numpy(x, "cuda", 0)
        io = sess.io_binding()
        io.bind_ortvalue_input(n, xo)
        for o in sess.get_outputs():
            io.bind_output(o.name, "cuda", 0)
        for _ in range(30):
            sess.run_with_iobinding(io)
        lat = []
        for _ in range(400):
            t0 = time.perf_counter()
            sess.run_with_iobinding(io)
            lat.append((time.perf_counter() - t0) * 1e3)
        res["iobinding"].append(summarise("cuda/fp32/320/iobinding", lat))
        print(res["iobinding"][-1], flush=True)
        del sess
    except Exception as e:
        res["iobinding"].append({"error": str(e)[:300]})

    # 3. TensorRT EP (engine cached; first session build is the cost)
    if "TensorrtExecutionProvider" in ort.get_available_providers():
        for sz, tag in ((320, "fp16"), (320, "fp32")):
            path = os.path.join(OUT, f"pose26n_{sz}_{tag}.onnx")
            opts = {"device_id": 0, "trt_fp16_enable": tag == "fp16",
                    "trt_engine_cache_enable": True, "trt_engine_cache_path": CACHE,
                    "trt_timing_cache_enable": True}
            try:
                t0 = time.perf_counter()
                sess = ort.InferenceSession(
                    path, providers=[("TensorrtExecutionProvider", opts),
                                     "CUDAExecutionProvider"])
                build_s = time.perf_counter() - t0
                n = sess.get_inputs()[0].name
                x = pre(crop(sz), sz, tag == "fp16")
                for _ in range(30):
                    sess.run(None, {n: x})
                lat = []
                for _ in range(400):
                    t0 = time.perf_counter()
                    sess.run(None, {n: x})
                    lat.append((time.perf_counter() - t0) * 1e3)
                s = summarise(f"trt/{sz}/{tag}", lat)
                s["session_build_s"] = round(build_s, 1)
                res["trt"].append(s)
                print(s, flush=True)
                del sess
            except Exception as e:
                res["trt"].append({"name": f"trt/{sz}/{tag}", "error": str(e)[:300]})
                print(res["trt"][-1], flush=True)
    else:
        res["trt"].append({"error": "TensorrtExecutionProvider not available"})

    with open(os.path.join(OUT, "pose_cost_trt.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print("wrote pose_cost_trt.json")


if __name__ == "__main__":
    main()
