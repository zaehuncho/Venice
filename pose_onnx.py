"""ONNX Runtime CUDA pose backend for the ROI-crop hot path (v3 pose model).

WHY THIS EXISTS
---------------
The ultralytics ``.pt`` wrapper costs ~15-19 ms per ROI-crop call on this rig — a fixed
per-call tax (pre/post-process, tensor marshalling, Python dispatch) that swamps the
~1.7 GFLOP of actual compute at 256px. ONNX Runtime with the CUDA execution provider
plus CUDA-graph capture collapses that to ~3 ms end to end (letterbox + session + decode).

MEASURED ON THIS RIG (RTX 3060, busy desktop, 2026-08-09)
    ultralytics .pt ROI crop            ~17 ms median
    ORT CUDA session.run (op20 export)   ~8.6 ms  (2 mid-net Resize nodes fall to CPU EP)
    ORT CUDA session.run (op17 export)   ~8.4 ms  (zero Memcpy nodes -> CUDA-graph eligible)
    ORT CUDA + CUDA graph (op17)         ~2.8 ms  <- this module's primary path

The opset-17 export is REQUIRED for the CUDA-graph path: at opset 19/20 ORT's CUDA
kernel registry has no Resize kernel, so the two FPN upsamples fall back to the CPU EP,
adding 6 Memcpy nodes which both cost time and disqualify CUDA-graph capture.
Regenerate with:
    from ultralytics import YOLO
    YOLO("models/orion_pose2k_n_v3.pt").export(format="onnx", imgsz=256, opset=17,
                                               dynamic=False, simplify=True)

OUTPUT LAYOUT (verified empirically on real frames, 2026-08-09)
    output0: (1, 300, 57)  — YOLO26 end-to-end head, NMS-free, rows sorted by score desc
    row = [x1, y1, x2, y2, score, cls] + 17 * [kpt_x, kpt_y, kpt_conf]
    All coordinates are in the 256x256 letterboxed input space; cls is always 0 (person).
    Verified by: (a) box columns bound the keypoint extent (impossible for cxcywh),
    (b) col 4 monotonically descending in [0,1], col 5 identically 0,
    (c) numeric parity vs ultralytics .pt decode on the same crops (see
    tools/diagnostics/pose_onnx_parity.py).

SAFETY
------
- CUDA ONLY. If the CUDA EP is unavailable or fails to actually activate (ORT silently
  falls back to CPU when its CUDA DLLs are missing!), this module raises
  ``OnnxPoseUnavailable`` — it NEVER runs CPU inference. The caller (pose_timing) then
  keeps the ultralytics path and logs why.
- The returned objects mimic the exact slice of the ultralytics Results API the
  consumers use: ``r.boxes`` (None-check, len, .xyxy/.conf/.cls -> .cpu().numpy())
  and ``r.keypoints.data.cpu().numpy()`` -> (N, 17, 3) in input-image pixel coords.
- Not thread-safe (single io-binding buffer set); the detector runs single-threaded.
"""
from __future__ import annotations

import os
import time
import logging

import numpy as np
import cv2

logger = logging.getLogger(__name__)

# Preferred first: the opset-17 export (all-CUDA graph -> CUDA-graph capture works).
# The opset-20 export still runs correctly (bit-identical outputs) but ~3x slower.
DEFAULT_ONNX_CANDIDATES = (
    "models/orion_pose2k_n_v3_256_op17.onnx",
    "models/orion_pose2k_n_v3_256.onnx",
)


def resolve_default_onnx() -> str:
    """Pick the best available default ONNX export (repo-root relative, like the .pt loads)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in DEFAULT_ONNX_CANDIDATES:
        for base in (here, os.getcwd()):
            cand = os.path.join(base, rel)
            if os.path.isfile(cand):
                return cand
    # Nothing found — return the preferred relative path so the error names it.
    return DEFAULT_ONNX_CANDIDATES[0]


class OnnxPoseUnavailable(RuntimeError):
    """Raised when the ONNX CUDA path cannot be used. Carries the human-readable reason."""


class _NpTensor:
    """Minimal shim so numpy arrays satisfy the ``.cpu().numpy()`` call chain."""

    __slots__ = ("_a",)

    def __init__(self, a):
        self._a = a

    def cpu(self):
        return self

    def numpy(self):
        return self._a

    def __len__(self):
        return len(self._a)


class _OnnxBoxes:
    __slots__ = ("xyxy", "conf", "cls", "_n")

    def __init__(self, xyxy, conf, cls):
        self.xyxy = _NpTensor(xyxy)
        self.conf = _NpTensor(conf)
        self.cls = _NpTensor(cls)
        self._n = len(xyxy)

    def __len__(self):
        return self._n


class _OnnxKeypoints:
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = _NpTensor(data)


class _OnnxPoseResult:
    __slots__ = ("boxes", "keypoints")

    def __init__(self, xyxy, conf, cls, kpts):
        self.boxes = _OnnxBoxes(xyxy, conf, cls)
        self.keypoints = _OnnxKeypoints(kpts)


class OnnxPoseModel:
    """Drop-in ``predict()`` replacement for the ultralytics pose model on ROI crops.

    predict(img_bgr, verbose=..., conf=...) -> [result] where result mimics the
    ultralytics Results attributes the codebase consumes. Coordinates are returned in
    the pixel space of the image passed in (the crop), exactly like ultralytics, so
    callers keep adding the crop origin themselves.
    """

    def __init__(self, onnx_path: str | None = None, imgsz: int = 256,
                 warmup_runs: int = 12, bench_runs: int = 30):
        self.onnx_path = onnx_path or resolve_default_onnx()
        self.imgsz = int(imgsz)
        self.cuda_graph = False
        self.latency_ms_median = float("nan")

        if not os.path.isfile(self.onnx_path):
            raise OnnxPoseUnavailable(f"onnx file not found: {self.onnx_path}")

        # torch first: registers torch\lib as a DLL directory so ORT's CUDA EP can
        # resolve cublas64_12/cudnn64_9 (torch's bundled CUDA runtime). Also the
        # authoritative CUDA-presence check — CPU inference is banned project-wide.
        try:
            import torch
        except Exception as exc:  # pragma: no cover
            raise OnnxPoseUnavailable(f"torch import failed: {exc}")
        if not torch.cuda.is_available():
            raise OnnxPoseUnavailable("torch reports no CUDA device")

        try:
            import onnxruntime as ort
        except Exception as exc:
            raise OnnxPoseUnavailable(f"onnxruntime import failed: {exc}")
        self._ort = ort

        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise OnnxPoseUnavailable(
                f"CUDAExecutionProvider not in available providers ({ort.get_available_providers()})")

        # Best-effort: newer ORT can preload CUDA/cuDNN DLLs from torch / nvidia wheels.
        try:  # pragma: no cover
            ort.preload_dlls()
        except Exception:
            pass
        ort.set_default_logger_severity(3)

        # Try the CUDA-graph session first; fall back to a plain CUDA session.
        sess = None
        try:
            sess = self._build_session(use_cuda_graph=True)
            self._setup_iobinding(sess)
            self._verify_session(graph_mode=True)
            self.cuda_graph = True
        except OnnxPoseUnavailable:
            raise
        except Exception as exc:
            logger.info("pose-onnx: cuda_graph capture unusable (%s) — plain CUDA session", exc)
            sess = None
        if sess is None:
            self._iob = None
            self._x_dev = None
            self._y_dev = None
            sess = self._build_session(use_cuda_graph=False)
            self._session = sess
            self._verify_session(graph_mode=False)

        # Warm up + measure the full wired path (letterbox + infer + decode) on a
        # realistic ROI-sized dummy so the init log carries an auditable number.
        dummy = np.random.randint(0, 255, (300, 260, 3), dtype=np.uint8)
        for _ in range(max(1, warmup_runs)):
            self.predict(dummy, verbose=False, conf=0.10)
        times = []
        for _ in range(max(1, bench_runs)):
            t0 = time.perf_counter()
            self.predict(dummy, verbose=False, conf=0.10)
            times.append((time.perf_counter() - t0) * 1000.0)
        times.sort()
        self.latency_ms_median = times[len(times) // 2]
        self.latency_ms_p90 = times[int(len(times) * 0.9)]

    # ------------------------------------------------------------------ session
    def _build_session(self, use_cuda_graph: bool):
        ort = self._ort
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3
        cuda_opts = {"cudnn_conv_algo_search": "HEURISTIC"}
        if use_cuda_graph:
            cuda_opts["enable_cuda_graph"] = "1"
        sess = ort.InferenceSession(self.onnx_path, so,
                                    providers=[("CUDAExecutionProvider", cuda_opts),
                                               "CPUExecutionProvider"])
        # THE anti-silent-CPU guard: ORT lists CUDA as "available" even when its DLLs
        # cannot load, then quietly creates a CPU session. Refuse that.
        if "CUDAExecutionProvider" not in sess.get_providers():
            raise OnnxPoseUnavailable(
                "CUDA EP failed to initialize (session fell back to CPU — CUDA runtime "
                "DLLs missing? torch must bundle the matching CUDA version)")
        self._input_name = sess.get_inputs()[0].name
        self._output_name = sess.get_outputs()[0].name
        out_shape = tuple(sess.get_outputs()[0].shape)
        if len(out_shape) != 3 or out_shape[2] != 57:
            raise OnnxPoseUnavailable(f"unexpected output shape {out_shape}, want (1, N, 57)")
        self._out_shape = out_shape
        return sess

    def _setup_iobinding(self, sess):
        """Fixed device buffers required for CUDA-graph capture."""
        ort = self._ort
        self._session = sess
        self._x_host = np.zeros((1, 3, self.imgsz, self.imgsz), dtype=np.float32)
        self._x_dev = ort.OrtValue.ortvalue_from_numpy(self._x_host, "cuda", 0)
        self._y_dev = ort.OrtValue.ortvalue_from_shape_and_type(self._out_shape, np.float32, "cuda", 0)
        iob = sess.io_binding()
        iob.bind_ortvalue_input(self._input_name, self._x_dev)
        iob.bind_ortvalue_output(self._output_name, self._y_dev)
        self._iob = iob

    def _verify_session(self, graph_mode: bool):
        """Two distinct random inputs must give distinct, finite outputs.

        Guards the classic CUDA-graph failure where a stale capture replays a constant
        output regardless of input.
        """
        a = np.ascontiguousarray(np.random.rand(1, 3, self.imgsz, self.imgsz).astype(np.float32))
        b = np.ascontiguousarray(np.random.rand(1, 3, self.imgsz, self.imgsz).astype(np.float32))
        ya = self._infer(a).copy()
        yb = self._infer(b).copy()
        if not (np.isfinite(ya).all() and np.isfinite(yb).all()):
            raise RuntimeError("non-finite output from ONNX session")
        if np.allclose(ya, yb):
            raise RuntimeError("output did not track input (stale CUDA-graph capture?)")

    def _infer(self, blob: np.ndarray) -> np.ndarray:
        """(1,3,S,S) float32 -> (N,57) float32."""
        if getattr(self, "_iob", None) is not None:
            self._x_dev.update_inplace(np.ascontiguousarray(blob))
            self._session.run_with_iobinding(self._iob)
            return self._y_dev.numpy()[0]
        out = self._session.run([self._output_name], {self._input_name: blob})
        return out[0][0]

    # ------------------------------------------------------------------ predict
    def predict(self, img, verbose: bool = False, conf: float = 0.10, imgsz=None, **_ignored):
        """Ultralytics-shaped predict on a BGR uint8 image. Returns [result]."""
        h, w = img.shape[:2]
        s = self.imgsz
        # Letterbox (ultralytics convention: keep aspect, pad 114, centered w/ 0.1 rounding)
        r = min(s / h, s / w)
        nw, nh = round(w * r), round(h * r)
        if (nw, nh) != (w, h):
            resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        else:
            resized = img
        dw, dh = (s - nw) / 2.0, (s - nh) / 2.0
        top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
        left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
        padded = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                    cv2.BORDER_CONSTANT, value=(114, 114, 114))
        blob = np.ascontiguousarray(
            padded[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255.0

        out = self._infer(blob)  # (N, 57), score-desc

        keep = out[:, 4] >= float(conf)
        rows = out[keep]
        n = len(rows)
        if n == 0:
            e = np.empty((0,), dtype=np.float32)
            return [_OnnxPoseResult(np.empty((0, 4), np.float32), e, e,
                                    np.empty((0, 17, 3), np.float32))]

        boxes = rows[:, 0:4].copy()
        scores = rows[:, 4].copy()
        cls = rows[:, 5].copy()
        kpts = rows[:, 6:57].reshape(n, 17, 3).copy()

        # Un-letterbox back to input-image pixels (subtract pad, divide by ratio, clip)
        inv = 1.0 / r
        boxes[:, [0, 2]] = np.clip((boxes[:, [0, 2]] - left) * inv, 0, w)
        boxes[:, [1, 3]] = np.clip((boxes[:, [1, 3]] - top) * inv, 0, h)
        kpts[:, :, 0] = np.clip((kpts[:, :, 0] - left) * inv, 0, w)
        kpts[:, :, 1] = np.clip((kpts[:, :, 1] - top) * inv, 0, h)

        return [_OnnxPoseResult(boxes, scores, cls, kpts)]

    def describe(self) -> str:
        return (f"onnxruntime-cuda {os.path.basename(self.onnx_path)} imgsz={self.imgsz} "
                f"cuda_graph={'on' if self.cuda_graph else 'off'}")
