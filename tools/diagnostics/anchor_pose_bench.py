"""anchor_pose_bench.py -- what does an ANIMATION-ANCHOR pose model cost on this box?

Read-only diagnostic for docs/ANIMATION_ANCHOR_V2.md (deliverable 2, "Cost").

Measures, for the 2026-08-09 fine-tuned YOLO26n-pose
(logs/diagnostics/pose_train/pose2k_n_eff_phase2/weights/best.pt):

  * ONNX export wall time (fp32 + fp16) at 256 / 320 / 384 input,
  * per-frame inference latency p50/p90/p99/max for
      - onnxruntime CUDAExecutionProvider  (fp32 and fp16 weights)
      - onnxruntime CPUExecutionProvider   (the fallback if CUDA is busy)
      - onnxruntime DmlExecutionProvider   (if the DirectML build is installed)
      - ultralytics .predict()             (the 2026-06-20 "~12 ms" reference)
      - raw torch forward (no wrapper)     (the floor the wrapper hides)
  * the PRE- and POST-processing that a live detect thread would also pay
    (crop -> letterbox -> CHW -> normalise; and decode best person + 17 kpts).

Nothing here touches the engine, the sidecar, settings or learning.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/anchor_pose_bench.py
Outputs: D:\\NexusVision\\anchor_study\\pose_cost.json  (+ the exported .onnx files)
"""
from __future__ import annotations

import json
import os
import shutil
import statistics as st
import sys
import time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = r"D:\NexusVision\anchor_study"
WEIGHTS = os.path.join(REPO, "logs", "diagnostics", "pose_train",
                       "pose2k_n_eff_phase2", "weights", "best.pt")
ITERS = 300
WARMUP = 30


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarise(name, ms):
    return {
        "name": name, "n": len(ms),
        "p50": round(pct(ms, 50), 3), "p90": round(pct(ms, 90), 3),
        "p99": round(pct(ms, 99), 3), "max": round(max(ms), 3),
        "mean": round(st.fmean(ms), 3), "sd": round(st.pstdev(ms), 3),
    }


def make_crop(size):
    """A realistic shooter crop: BGR uint8, mid-luma, some structure."""
    rng = np.random.default_rng(7)
    img = (rng.integers(40, 190, size=(size, size, 3), dtype=np.uint16)).astype(np.uint8)
    img[size // 3: size // 2, size // 3: 2 * size // 3] = 230  # a jersey-ish bright block
    return img


def preprocess(bgr, sz, half):
    import cv2
    r = cv2.resize(bgr, (sz, sz), interpolation=cv2.INTER_LINEAR)
    x = r[:, :, ::-1].transpose(2, 0, 1)[None]
    x = np.ascontiguousarray(x, dtype=np.float16 if half else np.float32) / (
        np.float16(255) if half else np.float32(255))
    return x


def decode(out, conf=0.25):
    """YOLO26 end-to-end pose head: (1, 300, 57) = [x1,y1,x2,y2,score,cls, 17*(x,y,c)]."""
    a = out[0]
    if a.ndim == 3:
        a = a[0]
    if a.shape[0] in (56, 57) and a.shape[1] > a.shape[0]:
        a = a.T
    scores = a[:, 4]
    i = int(np.argmax(scores))
    if scores[i] < conf:
        return None
    return a[i, 0:4], a[i, 6:57].reshape(17, 3)


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"box": {}, "export": {}, "infer": [], "pre_post": {}}

    import torch
    import cv2
    res["box"] = {
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "torch": torch.__version__, "cuda": torch.version.cuda,
        "cv2": cv2.__version__, "numpy": np.__version__,
    }
    import onnxruntime as ort
    res["box"]["onnxruntime"] = ort.__version__
    res["box"]["providers"] = ort.get_available_providers()

    from ultralytics import YOLO

    # ---- export --------------------------------------------------------
    onnx_paths = {}
    for sz in (256, 320, 384):
        for half in (False, True):
            tag = "fp16" if half else "fp32"
            dst = os.path.join(OUT, f"pose26n_{sz}_{tag}.onnx")
            m = YOLO(WEIGHTS)
            t0 = time.perf_counter()
            p = m.export(format="onnx", imgsz=sz, half=half, simplify=True,
                         opset=17, device=0 if half else "cpu", verbose=False)
            dt = time.perf_counter() - t0
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(p, dst)      # cross-volume C: -> D:
            except Exception as _e:
                print('move failed', _e)
                dst = p
            onnx_paths[(sz, tag)] = dst
            res["export"][f"{sz}_{tag}"] = {
                "seconds": round(dt, 2), "path": dst,
                "bytes": os.path.getsize(dst) if os.path.exists(dst) else -1}
            print(f"export {sz} {tag}: {dt:.1f}s -> {dst}", flush=True)

    # ---- pre/post ------------------------------------------------------
    crop = make_crop(320)
    full = make_crop(720)
    ms = []
    for _ in range(500):
        t0 = time.perf_counter()
        sub = full[200:520, 300:620]
        preprocess(sub, 320, True)
        ms.append((time.perf_counter() - t0) * 1e3)
    res["pre_post"]["crop320_letterbox_fp16"] = summarise("preprocess", ms[20:])

    # ---- onnxruntime ---------------------------------------------------
    def bench_ort(sz, tag, provider, opts=None):
        path = onnx_paths[(sz, tag)]
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2          # a detect thread must not eat the box
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            sess = ort.InferenceSession(path, so, providers=[provider] if opts is None
                                        else [(provider, opts)])
        except Exception as e:
            return {"name": f"ort/{provider}/{sz}/{tag}", "error": str(e)[:200]}
        iname = sess.get_inputs()[0].name
        x = preprocess(make_crop(sz), sz, tag == "fp16")
        t_load0 = time.perf_counter()
        for _ in range(WARMUP):
            sess.run(None, {iname: x})
        warm = time.perf_counter() - t_load0
        lat, dec = [], []
        for _ in range(ITERS):
            t0 = time.perf_counter()
            out = sess.run(None, {iname: x})
            t1 = time.perf_counter()
            decode([o.astype(np.float32) for o in out])
            t2 = time.perf_counter()
            lat.append((t1 - t0) * 1e3)
            dec.append((t2 - t1) * 1e3)
        s = summarise(f"ort/{provider}/{sz}/{tag}", lat)
        s["decode_p50_ms"] = round(pct(dec, 50), 3)
        s["warmup_s"] = round(warm, 2)
        del sess
        return s

    avail = ort.get_available_providers()
    for sz in (256, 320, 384):
        for tag in ("fp32", "fp16"):
            if "CUDAExecutionProvider" in avail:
                res["infer"].append(bench_ort(sz, tag, "CUDAExecutionProvider"))
                print(res["infer"][-1], flush=True)
    for tag in ("fp32",):
        res["infer"].append(bench_ort(320, tag, "CPUExecutionProvider"))
        print(res["infer"][-1], flush=True)
    if "DmlExecutionProvider" in avail:
        for sz in (256, 320):
            for tag in ("fp32", "fp16"):
                res["infer"].append(bench_ort(sz, tag, "DmlExecutionProvider"))
                print(res["infer"][-1], flush=True)
    else:
        res["infer"].append({"name": "ort/DmlExecutionProvider",
                             "error": "onnxruntime-directml not installed in .venv "
                                      "(providers: %s)" % ",".join(avail)})

    # ---- raw torch forward + ultralytics predict ------------------------
    for sz in (256, 320):
        for half in (True, False):
            m = YOLO(WEIGHTS)
            net = m.model.to("cuda").eval()
            if half:
                net = net.half()
            x = torch.from_numpy(preprocess(make_crop(sz), sz, half)).cuda()
            with torch.inference_mode():
                for _ in range(WARMUP):
                    net(x)
                torch.cuda.synchronize()
                lat = []
                for _ in range(ITERS):
                    t0 = time.perf_counter()
                    y = net(x)
                    torch.cuda.synchronize()
                    lat.append((time.perf_counter() - t0) * 1e3)
            res["infer"].append(summarise(
                f"torch.forward/cuda/{sz}/{'fp16' if half else 'fp32'}", lat))
            print(res["infer"][-1], flush=True)
            del net, m
            torch.cuda.empty_cache()

    m = YOLO(WEIGHTS)
    im = make_crop(320)
    for _ in range(WARMUP):
        m.predict(im, imgsz=320, device=0, half=True, verbose=False)
    lat = []
    for _ in range(150):
        t0 = time.perf_counter()
        m.predict(im, imgsz=320, device=0, half=True, verbose=False)
        lat.append((time.perf_counter() - t0) * 1e3)
    res["infer"].append(summarise("ultralytics.predict/cuda/320/fp16", lat))
    print(res["infer"][-1], flush=True)

    with open(os.path.join(OUT, "pose_cost.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print("wrote", os.path.join(OUT, "pose_cost.json"))


if __name__ == "__main__":
    sys.exit(main())
