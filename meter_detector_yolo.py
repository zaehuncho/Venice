"""Trained YOLO meter locator (2K27 white meter) as a corroboration source.

WHY THIS EXISTS: the colour reader (simple_meter_reader) finds a meter by scanning
for bright/white columns, and an arena is full of bright neutral décor -- a white
hoodie, a painted lane line, the sideline, centre-court text. That produces the
"FILL 6% on bare court" phantom locks the owner photographed. A single-class
YOLO11n trained on 2079 labelled 2K27 frames + 693 non-gameplay negatives locates
the meter directly and is used to VETO any colour-reader box it cannot corroborate.

Runtime contract: ONNX via onnxruntime (CUDA, then DirectML, then CPU). NEVER raises to
the caller -- every failure returns None so the reader falls back to its shipped
behaviour. Import is lazy and guarded so a missing model / missing onnxruntime
simply disables the feature.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover - cv2 is always present in the reader venv
    cv2 = None


def _ensure_cuda_dlls() -> None:
    """Put torch's bundled CUDA runtime on the DLL search path.

    onnxruntime-gpu needs cublasLt64_12.dll / cudnn64_9.dll; a stock machine does
    not have them, but the torch wheel in this venv ships them under torch/lib.
    Without this, ORT silently falls back to CPU (~174 ms/frame vs ~14 ms on GPU).
    Best-effort and idempotent -- any failure just leaves ORT on CPU.
    """
    try:
        import pathlib
        import torch  # noqa: F401 - only needed for its bundled libs
        tlib = str(pathlib.Path(torch.__file__).parent / "lib")
        if os.path.isdir(tlib):
            try:
                os.add_dll_directory(tlib)   # py3.8+ Windows
            except Exception:
                pass
            if tlib not in os.environ.get("PATH", ""):
                os.environ["PATH"] = tlib + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


_ensure_cuda_dlls()


def _resolve_default_model(base_dir: Optional[str] = None) -> str:
    """Return the canonical packaged model, with development-only fallbacks.

    ``models/orion_meter_detector.onnx`` is the sole stable runtime identity and
    is the path a production profile pins.  The run-tree candidates keep source
    checkouts usable while a candidate is being promoted, but are never preferred
    over the canonical artifact and do not leak a training-directory dependency
    into an installed package.
    """
    root = os.path.abspath(base_dir or os.path.dirname(os.path.abspath(__file__)))
    canonical = os.path.join(root, "models", "orion_meter_detector.onnx")
    dev_root = os.path.join(
        root, "runs", "detect", "logs", "diagnostics", "meter_train")
    candidates = (
        canonical,
        # n3_pill is the shipped detector (99.7% on the Arrow2/Pill validation) and the
        # geometry every meter-time constant, the session ruler and tip_registration.json
        # were calibrated against: box h 107 px, within-shot h sd ~0.65 px at 720p.
        os.path.join(dev_root, "meter2k27_n3_pill", "weights", "best.onnx"),
        # n4_lowfill (9 epochs) was promoted to canonical on 2026-09-04 without a
        # geometry check: same frames, box h 121 px (+14), h sd 2-5x n3 -> ruler shift
        # ~13%, rate stretch on every shot, command-fill spread x2-3 (see
        # docs/HANDOFF_2026-09-01_TIMING_LANE.md, 2026-09-08). A candidate is promotable
        # only after box-geometry parity with n3 on the same frames.
        os.path.join(dev_root, "meter2k27_n4_lowfill", "weights", "best.onnx"),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    # Preserve the stable production-facing failure path when no asset exists.
    return canonical


_DEFAULT_MODEL = _resolve_default_model()


_PROVIDER_ALIASES = {
    "cuda": "CUDAExecutionProvider",
    "cudaexecutionprovider": "CUDAExecutionProvider",
    "dml": "DmlExecutionProvider",
    "directml": "DmlExecutionProvider",
    "dmlexecutionprovider": "DmlExecutionProvider",
    "cpu": "CPUExecutionProvider",
    "cpuexecutionprovider": "CPUExecutionProvider",
}


def _provider_priority(available, preference: Optional[str] = None):
    """Resolve the installed ONNX providers in deterministic priority order.

    CUDA remains first on the calibrated NVIDIA rig.  DirectML is preferred to
    CPU when it is already installed, giving non-CUDA Windows GPUs a viable
    accelerated path without making the DirectML package a dependency.  The
    optional ``ORION_METER_PROVIDER_PRIORITY`` value accepts comma-separated
    aliases (``cuda,dml,cpu``) or exact provider names; unavailable/unknown
    entries are ignored and CPU is retained as a final fallback when present.
    """
    avail = [str(p) for p in (available or [])]
    aset = set(avail)
    raw = (os.environ.get("ORION_METER_PROVIDER_PRIORITY", "")
           if preference is None else str(preference or ""))
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    requested = []
    for token in tokens:
        provider = _PROVIDER_ALIASES.get(token.lower(), token)
        if provider in aset and provider not in requested:
            requested.append(provider)
    if not requested:
        requested = [p for p in (
            "CUDAExecutionProvider", "DmlExecutionProvider",
            "CPUExecutionProvider") if p in aset]
    if "CPUExecutionProvider" in aset and "CPUExecutionProvider" not in requested:
        requested.append("CPUExecutionProvider")
    return requested or (["CPUExecutionProvider"]
                         if "CPUExecutionProvider" in aset else avail[:1])


def _session_options(ort, providers):
    """Keep inference's CPU workers bounded, including GPU fallback operators.

    ORT's automatic pool uses every physical core and spins between work items.
    This sidecar shares the host with decode, tracking and controller work, so
    sleeping workers and a small explicit pool avoid consuming that capacity
    while waiting for another detector frame. No input size or cadence changes.
    """
    try:
        requested_threads = int(os.environ.get("ORION_METER_CPU_THREADS", "") or 2)
    except (TypeError, ValueError):
        requested_threads = 2
    cpu_threads = max(1, min(requested_threads, 4, os.cpu_count() or 1))
    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.intra_op_num_threads = cpu_threads
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    if "DmlExecutionProvider" in providers:
        # Required even when DML is a secondary provider in the requested list.
        options.enable_mem_pattern = False
    return options


class MeterYoloLocator:
    """Loads the ONNX detector once and returns the single best meter box per frame.

    detect_box(frame_bgr) -> (x, y, w, h, conf) in FULL-FRAME pixels, or None.
    """

    def __init__(self, model_path: Optional[str] = None, imgsz: int = 1280,
                 conf_thres: float = 0.35, iou_thres: float = 0.45):
        self.imgsz = int(imgsz)
        self.conf_thres = float(conf_thres)
        self.iou_thres = float(iou_thres)
        self.ok = False
        self._sess = None
        self._inp = None
        self._in_dtype = np.float32
        self._canvas = None      # reused letterbox buffer (see _letterbox)
        self._canvas_geom = None # (height, width, top, left) currently painted into it
        self._prep_sess = None   # [ORION_METER_DETECTOR_GPU_PREP] on-device blob graph, or None
        self._prep_ov = None
        self._prep_disabled = "not attempted"
        # Serialize detect_box: the async worker thread and a synchronous acquire call (detect_now)
        # can both invoke it, and they share the single reused _canvas buffer + ORT session, which a
        # concurrent call would corrupt. Contention is rare (acquire skips the async submit), so the
        # lock is essentially free but makes the two paths safe to coexist.
        self._infer_lock = threading.Lock()
        self.provider = "none"
        # Region/size plausibility gate (see _plausible). The old bottom=0.85 came from the
        # retired detector's 1080p search strip, not the learned model's evidence. Archived deep
        # shots place genuine 0.919-0.934-confidence meters at center-y 0.881-0.922; 0.95 admits
        # all four while adding zero detections across 806 no-meter negatives and zero new locks
        # across 558 runtime no-det frames. Top/size/confidence gates remain unchanged.
        def _f(name, default):
            try:
                return float(os.environ.get(name, "") or default)
            except Exception:
                return default
        self._band_top = _f("ORION_METER_BAND_TOP", 0.20)
        self._band_bot = _f("ORION_METER_BAND_BOTTOM", 0.95)
        self._w_min_f = _f("ORION_METER_W_MIN_FRAC", 0.013)
        self._w_max_f = _f("ORION_METER_W_MAX_FRAC", 0.035)
        self._h_min_f = _f("ORION_METER_H_MIN_FRAC", 0.07)
        self._h_max_f = _f("ORION_METER_H_MAX_FRAC", 0.25)
        path = model_path or os.environ.get("ORION_METER_MODEL", "") or _DEFAULT_MODEL
        self.model_path = path
        if cv2 is None or not os.path.isfile(path):
            return
        try:
            import onnxruntime as ort
            provs = ort.get_available_providers()
            use = _provider_priority(provs)
            so = _session_options(ort, use)
            self._sess = ort.InferenceSession(path, sess_options=so, providers=use)
            _in = self._sess.get_inputs()[0]
            self._inp = _in.name
            # Take imgsz FROM THE MODEL so the letterbox can never disagree with the export
            # (a mismatch silently yields zero detections). 960 ships as the default: measured
            # 29ms vs 48ms at 1280 with IDENTICAL detection (140/349 both) -> ~40% faster
            # cadence, which is less box staleness to extrapolate and an earlier first lock.
            try:
                _shape = getattr(_in, "shape", None)
                if _shape and len(_shape) == 4:
                    _sz = _shape[2] if isinstance(_shape[2], int) else _shape[3]
                    if isinstance(_sz, int) and _sz >= 128:
                        self.imgsz = int(_sz)
            except Exception:
                pass
            # FP16 export (best.onnx, ~13 ms vs ~44 ms FP32 -> fresher box, less lock lag)
            # takes a float16 input; feed the wrong dtype and ORT returns zero detections.
            self._in_dtype = np.float16 if "float16" in (_in.type or "") else np.float32
            self.provider = self._sess.get_providers()[0]
            self.ok = True
            # [ORION_METER_DETECTOR_GPU_PREP 2026-09-08] blob construction on the GPU. The
            # numpy blob (f32 blobFromImage + f16 cast, ~19 ms under live load) cost twice the
            # 9.7 ms model run and held the GIL; a Cast/Mul/Cast/Transpose graph on the CUDA
            # provider bound straight into the detector's input takes the end-to-end call from
            # ~24 ms to ~11 ms with bit-identical outputs. Fail-closed to the CPU path.
            self._prep_sess = None
            self._prep_ov = None
            self._prep_disabled = ""
            if os.environ.get("ORION_METER_DETECTOR_GPU_PREP", "1").strip() not in ("0", "false", "no"):
                self._build_gpu_prep(ort)
            else:
                self._prep_disabled = "ORION_METER_DETECTOR_GPU_PREP=0"
        except Exception:
            self.ok = False
            self._sess = None

    # -- GPU preprocessing (optional, CUDA only) ------------------------------------------
    def _build_gpu_prep(self, ort) -> None:
        """Create the on-device blob graph and verify it against the CPU path once."""
        try:
            if self.provider != "CUDAExecutionProvider" or self._in_dtype is not np.float16:
                self._prep_disabled = "provider=%s dtype=%s" % (self.provider, self._in_dtype.__name__)
                return
            import onnx
            from onnx import helper, TensorProto, numpy_helper
            S = int(self.imgsz)
            nodes = [
                helper.make_node("Cast", ["u8"], ["f32"], to=TensorProto.FLOAT),
                helper.make_node("Mul", ["f32", "scale"], ["scaled"]),
                helper.make_node("Cast", ["scaled"], ["f16"], to=TensorProto.FLOAT16),
                helper.make_node("Transpose", ["f16"], ["nchw_bgr"], perm=[0, 3, 1, 2]),
                helper.make_node("Split", ["nchw_bgr", "splits"], ["b", "g", "r"], axis=1),
                helper.make_node("Concat", ["r", "g", "b"], ["images"], axis=1),
            ]
            graph = helper.make_graph(
                nodes, "orion_meter_prep",
                [helper.make_tensor_value_info("u8", TensorProto.UINT8, [1, S, S, 3])],
                [helper.make_tensor_value_info("images", TensorProto.FLOAT16, [1, 3, S, S])],
                initializer=[
                    numpy_helper.from_array(np.array(1.0 / 255.0, dtype=np.float32), "scale"),
                    numpy_helper.from_array(np.array([1, 1, 1], dtype=np.int64), "splits"),
                ])
            model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
            model.ir_version = 9
            prep_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            # Same bounded, non-spinning CPU pool (and mem-pattern setting) as the detector
            # session: the prep graph's CPU fallback operators must never grow the sidecar's
            # worker footprint.
            so = _session_options(ort, list(self._sess.get_providers()))
            prep = ort.InferenceSession(model.SerializeToString(), sess_options=so,
                                        providers=prep_providers)
            if prep.get_providers()[0] != "CUDAExecutionProvider":
                self._prep_disabled = "prep session not on CUDA"
                return
            ov = ort.OrtValue.ortvalue_from_shape_and_type([1, 3, S, S], np.float16, "cuda", 0)
            self._prep_sess = prep
            self._prep_ov = ov
            self._prep_io = prep.io_binding()
            self._prep_io.bind_ortvalue_output("images", ov)
            self._det_io = self._sess.io_binding()
            self._det_io.bind_ortvalue_input(self._inp, ov)
            self._det_out_name = self._sess.get_outputs()[0].name
            # Self-test on a synthetic frame: the two paths must agree bit for bit, otherwise the
            # detector geometry could move without any model change. Refuse on mismatch.
            rng = np.random.default_rng(7)
            canvas = rng.integers(0, 256, size=(S, S, 3), dtype=np.uint8)
            ref = self._run_model_cpu(canvas)
            got = self._run_model_gpu(canvas)
            if ref.shape != got.shape or not np.array_equal(
                    ref.astype(np.float32), got.astype(np.float32)):
                self._prep_disabled = "self-test mismatch"
                self._prep_sess = None
                self._prep_ov = None
        except Exception as exc:  # pragma: no cover - environment dependent
            self._prep_disabled = "%s: %s" % (type(exc).__name__, exc)
            self._prep_sess = None
            self._prep_ov = None

    def _run_model_cpu(self, canvas):
        # OpenCV performs channel swap + NCHW packing + normalization in native code.
        # The former NumPy chain created a ~20 MB FP32 intermediate, divided it, then
        # allocated again for FP16; on the live 1280-square model that conversion was
        # slower than CUDA inference itself.  Real-frame A/B is detection-identical.
        blob = cv2.dnn.blobFromImage(
            canvas, scalefactor=1.0 / 255.0, swapRB=True, crop=False)
        if blob.dtype != self._in_dtype:
            blob = blob.astype(self._in_dtype, copy=False)
        return self._sess.run(None, {self._inp: blob})[0]  # (1,5,N)

    def _run_model_gpu(self, canvas):
        u8 = canvas[None]
        if not u8.flags["C_CONTIGUOUS"]:
            u8 = np.ascontiguousarray(u8)
        self._prep_io.bind_cpu_input("u8", u8)
        self._prep_sess.run_with_iobinding(self._prep_io)
        self._det_io.bind_output(self._det_out_name, "cpu")
        self._sess.run_with_iobinding(self._det_io)
        return self._det_io.copy_outputs_to_cpu()[0]

    def _run_model(self, canvas):
        """(1,5,N) raw model output for a letterboxed canvas; GPU prep when available."""
        if self._prep_sess is not None:
            try:
                return self._run_model_gpu(canvas)
            except Exception as exc:  # fall back for the rest of the process, never mid-shot again
                self._prep_disabled = "runtime %s: %s" % (type(exc).__name__, exc)
                self._prep_sess = None
                self._prep_ov = None
        return self._run_model_cpu(canvas)

    # -- preprocessing: letterbox to a square imgsz, matching ultralytics export --
    def _letterbox(self, img):
        h, w = img.shape[:2]
        r = min(self.imgsz / h, self.imgsz / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        top = (self.imgsz - nh) // 2
        left = (self.imgsz - nw) // 2
        # REUSE the 1280² canvas across calls -- allocating+filling ~5 MB every frame was a
        # big chunk of the ~31 ms preprocessing overhead (the real lock-lag source, since FP16
        # already cut inference to ~13 ms). The worker is single-threaded, so reuse is safe; the
        # padding border is filled with 114 ONCE and only the image region is overwritten each call
        # (input geometry is fixed, so the border never needs re-clearing).
        cv = self._canvas
        _geom = (nh, nw, top, left)
        if cv is None or cv.shape[0] != self.imgsz or cv.shape[1] != self.imgsz:
            cv = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
            self._canvas = cv
            self._canvas_geom = _geom
        elif self._canvas_geom != _geom:
            # Full-frame inference (16:9, top/bottom padding) and the opt-in edge
            # acquisition tiles (near-square, left/right padding) share this buffer.
            # The old fixed-geometry optimization filled the border only once; after
            # switching geometry, pixels from the previous image remained in what is
            # now padding and became a synthetic HUD/court strip seen by the model.
            # Geometry changes are acquisition-only, so one sub-millisecond fill at
            # each phase boundary keeps the steady tracking path allocation-free.
            cv.fill(114)
            self._canvas_geom = _geom
        cv[top:top + nh, left:left + nw] = resized
        return cv, r, left, top

    def detect_box(self, frame_bgr) -> Optional[Tuple[int, int, int, int, float]]:
        """Return the highest-confidence meter box (x, y, w, h, conf), or None."""
        if not self.ok or frame_bgr is None:
            return None
        with self._infer_lock:
            return self._detect_box_locked(frame_bgr)

    def detect_box_tile(self, frame_bgr, side: str, fraction: float = 0.55
                        ) -> Optional[Tuple[int, int, int, int, float]]:
        """Run one inference on a full-height overlapping edge tile.

        This is an acquisition-only resolution boost for tiny far/moving meters.  It
        does *not* change confidence or geometry policy: the returned crop box is
        mapped back to the source frame and checked against the ordinary full-frame
        plausibility gate before it can leave this method.

        ``side='left'`` is ``[0, .55W]`` and ``side='right'`` is
        ``[.45W, W]``.  The 10% overlap means a three-phase full/left/right scan has
        no horizontal blind seam while performing exactly one inference per call.
        """
        if not self.ok or frame_bgr is None:
            return None
        try:
            H, W = frame_bgr.shape[:2]
            if H <= 0 or W <= 0:
                return None
            _side = str(side or "").strip().lower()
            if _side not in ("left", "right"):
                return None
            _frac = min(1.0, max(0.05, float(fraction)))
            cw = max(1, min(W, int(round(W * _frac))))
            x0 = 0 if _side == "left" else W - cw
            crop = frame_bgr[:, x0:x0 + cw]
        except Exception:
            return None
        with self._infer_lock:
            return self._detect_box_locked(
                crop, output_offset=(x0, 0), plausibility_size=(W, H))

    def _detect_box_locked(self, frame_bgr, output_offset=(0, 0),
                           plausibility_size=None):
        try:
            H, W = frame_bgr.shape[:2]
            canvas, r, padx, pady = self._letterbox(frame_bgr)
            out = self._run_model(canvas)                      # (1,5,N)
            pred = out[0]                                      # (5,N)
            if pred.shape[0] == 5 and pred.shape[1] != 5:
                pred = pred.T                                  # (N,5): cx,cy,w,h,conf
            conf = pred[:, 4]
            keep = (conf >= self.conf_thres) & np.isfinite(pred).all(axis=1)
            if not np.any(keep):
                return None
            pred = pred[keep]
            cx, cy, bw, bh, sc = (pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3], pred[:, 4])
            x1 = cx - bw / 2.0; y1 = cy - bh / 2.0
            x2 = cx + bw / 2.0; y2 = cy + bh / 2.0
            boxes = np.stack([x1, y1, x2, y2], axis=1)
            # Apply the UNCHANGED full-frame geometry gate before selection.
            # An oversized/high-confidence proposal can overlap a real meter
            # enough to suppress it, then fail plausibility itself. Selecting a
            # plausible winner only AFTER NMS cannot recover that lost box.
            # This API returns ONE box: NMS over plausible candidates always
            # retains their top-scoring member, so it cannot change that answer.
            # Walk score order and stop on the first plausible box instead of
            # paying for suppression or mapping every duplicate model anchor.
            for best in np.argsort(sc)[::-1]:
                bx1, by1, bx2, by2 = boxes[best]
                # undo letterbox -> full-frame pixels
                bx1 = (bx1 - padx) / r; bx2 = (bx2 - padx) / r
                by1 = (by1 - pady) / r; by2 = (by2 - pady) / r
                bx1 = max(0.0, min(W - 1.0, bx1)); bx2 = max(0.0, min(W - 1.0, bx2))
                by1 = max(0.0, min(H - 1.0, by1)); by2 = max(0.0, min(H - 1.0, by2))
                x, y = int(round(bx1)), int(round(by1))
                w, h = int(round(bx2 - bx1)), int(round(by2 - by1))
                if w <= 0 or h <= 0:
                    continue
                # A tile proposal is expressed in crop pixels above. Map it to the
                # original frame *before* applying geometry. Applying the normal width
                # fractions to the 55%-wide crop would silently loosen min-width and
                # tighten max-width, which is exactly the kind of gate drift this path
                # is forbidden to introduce.
                ox, oy = output_offset
                out_x, out_y = x + int(ox), y + int(oy)
                pW, pH = plausibility_size if plausibility_size is not None else (W, H)
                if not self._plausible(out_x, out_y, w, h, int(pW), int(pH)):
                    continue
                return (out_x, out_y, w, h, float(sc[best]))
            return None

        except Exception:
            return None

    def _plausible(self, x: int, y: int, w: int, h: int, W: int, H: int) -> bool:
        """Reject proposals that cannot be the shot meter, by REGION and by SIZE.

        The retired colour detector carried both gates in its per-style config
        (meter_styles/*.json: `search` = left 5 / top 216 / right 1915 / bottom 920 @1080p, and
        `contour` w_min/w_max/h_min/h_max) and they are a large part of why its locks were stable.
        Moving to YOLO dropped them, and the model -- trained only on real meters -- happily
        proposes meter-ish bright verticals elsewhere.

        MEASURED on session_20260830_003937 (2019 live detections): 200 were not meter-shaped and
        131 of those sat in the TOP band, i.e. the SCOREBOARD -- exactly the owner-reported "locks
        onto the scoreboard / side of the court". Sizes among them included 28x345 and 40x507
        against a real meter of ~23x107 @720p.

        The meter is anchored to the SHOOTING PLAYER on the court, so it can never be in the
        scoreboard strip nor be several times its own height. Fractions of the frame (not pixels)
        so the gate holds at any capture resolution; env-tunable, and any parse failure disables
        the gate rather than rejecting a real meter."""
        try:
            if W <= 0 or H <= 0:
                return True
            cy = (y + h * 0.5) / float(H)
            if not (self._band_top <= cy <= self._band_bot):
                return False
            wf = w / float(W)
            hf = h / float(H)
            if not (self._w_min_f <= wf <= self._w_max_f):
                return False
            if not (self._h_min_f <= hf <= self._h_max_f):
                return False
            return True
        except Exception:
            return True          # never let the gate itself reject a real meter

    @staticmethod
    def _nms(boxes, scores, iou_thres):
        if len(boxes) == 0:
            return []
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(int(i))
            if order.size == 1:
                break
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1); h = np.maximum(0.0, yy2 - yy1)
            inter = w * h
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou <= iou_thres]
        return keep


class AsyncMeterLocator:
    """Runs MeterYoloLocator on a background thread so detector latency (even ~14 ms
    on GPU, ~174 ms on CPU) NEVER blocks the timing loop.

    The reader calls submit(frame, ts) each detect() (non-blocking; always keeps only
    the freshest frame) and latest() to read the most recent result:
        (found: bool, box: (x,y,w,h) or None, conf: float, frame_ts: float)
    frame_ts is the ts of the frame the box was computed from, so the caller can
    reject a stale result. Purely a wrapper -- if the underlying locator failed to
    load, ok is False and latest() always reports not-found.
    """

    def __init__(self, base: Optional["MeterYoloLocator"] = None, sync: Optional[bool] = None):
        self._base = base if base is not None else MeterYoloLocator()
        self.ok = bool(self._base and self._base.ok)
        self.provider = self._base.provider if self._base else "none"
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        # (frame, ts, scope, priority), always the freshest unprocessed frame.
        # ``priority`` is a scheduling hint only: it bypasses the inter-inference
        # breathing gap, but inference still happens on this worker.  Keeping that
        # distinction is load-bearing -- running ORT inline on the capture callback
        # caused 80-170ms source gaps and collapsed the live preview into the 30s.
        self._pending = None
        self._result = (False, None, 0.0, -1.0)
        # Scope of the pixels inspected for _result. A tile miss is NOT evidence
        # that the other 45% of the frame has no meter; consumers that implement a
        # global no-meter veto can read this atomically through latest_details().
        self._result_scope = "full"
        # Results are pixel-coordinate evidence for one source geometry/lifecycle.
        # Generation checks also cover in-flight work: clearing _result alone would
        # let the old inference republish after a resolution switch or reset.
        self._generation = 0
        self._frame_shape = None
        self._stop = False
        self._thread = None
        self.infer_ms = 0.0
        # Min gap between inferences. For pure-CV (provider=="cv-contour"), inference takes ~0.5ms
        # with zero GIL stall, so min interval defaults to 0ms (zero artificial lag). For YOLO,
        # it defaults to 15ms.
        _default_interval = "0" if getattr(self._base, "provider", "") == "cv-contour" else "15"
        self._min_interval_s = max(0.0, float(
            os.environ.get("ORION_METER_DETECTOR_MIN_INTERVAL_MS", _default_interval)) / 1000.0)
        self._last_infer_end = 0.0
        # SYNC mode (offline replay/eval only): submit() runs inference inline so latest()
        # always reflects the frame just submitted -- decouples the seed/veto CORRECTNESS test
        # from wall-clock pacing. Live always uses the async thread so detector latency never
        # blocks timing. Default from ORION_METER_DETECTOR_SYNC.
        if sync is None:
            sync = os.environ.get("ORION_METER_DETECTOR_SYNC", "").strip() in ("1", "true", "True", "yes")
        self._sync = bool(sync)
        if self.ok and not self._sync:
            self._thread = threading.Thread(target=self._loop, name="meter-yolo", daemon=True)
            self._thread.start()

    def submit(self, frame, ts: float) -> None:
        """Queue the freshest full-frame sample at ordinary detector cadence."""
        self._submit(frame, ts, scope="full", priority=False)

    def submit_priority(self, frame, ts: float) -> None:
        """Queue a freshest-frame acquisition/rescue sample without blocking.

        Priority wakes the worker out of its normal inter-inference gap.  It never
        runs preprocessing or ORT on the caller, so capture/presentation cadence is
        independent of detector latency.
        """
        self._submit(frame, ts, scope="full", priority=True)

    def submit_priority_region(self, frame, ts: float, region: str) -> None:
        """Priority variant for the optional one-region-per-opportunity scan."""
        scope = str(region or "full").strip().lower()
        if scope not in ("full", "left", "right"):
            raise ValueError("region must be 'full', 'left', or 'right'")
        self._submit(frame, ts, scope=scope, priority=True)

    def _submit(self, frame, ts: float, *, scope: str, priority: bool) -> None:
        if not self.ok or frame is None:
            return
        if self._sync:
            # Explicit offline/eval mode only.  Live constructs the threaded locator.
            self._detect_now_scoped(frame, ts, scope)
            return
        with self._lock:
            if self._stop:
                return
            generation = self._generation
        # SNAPSHOT the frame. The live capture backend reuses its frame buffer, so a
        # bare reference handed to the worker thread can be overwritten before the worker
        # reads it -> the detector processes garbage and finds nothing (the live 2K27
        # failure: reader locked décor at ~0% fill while the offline replay, which hands
        # imread's fresh arrays, hit 96%). A ~2.7MB copy (~0.3ms) buys a stable snapshot.
        try:
            snap = np.ascontiguousarray(frame)
            if snap is frame:          # ascontiguousarray may return the SAME array
                snap = frame.copy()
        except Exception:
            return
        with self._cv:
            # reset()/stop() may run while the caller snapshots a reused buffer.
            # That pre-reset frame must not become the new generation's first work.
            if self._stop or generation != self._generation:
                return
            # A normal next-frame submit must not erase a priority wake that the worker
            # has not claimed yet.  It may replace its pixels (newer is better), but the
            # bypass survives so the freshest frame runs immediately.
            carried_priority = bool(self._pending is not None and self._pending[3])
            self._set_frame_shape_locked(snap)
            self._pending = (snap, float(ts), scope,
                             bool(priority or carried_priority))
            self._cv.notify_all()

    def latest(self) -> Tuple[bool, Optional[Tuple[int, int, int, int]], float, float]:
        with self._lock:
            return self._result

    def latest_details(self):
        """Return ``latest()`` plus its inference scope atomically.

        Kept separate from latest() so every existing four-tuple caller remains
        binary/source compatible. Scope is ``full``, ``left`` or ``right``.
        """
        with self._lock:
            return (*self._result, self._result_scope)

    def detect_now(self, frame, ts: float) -> Tuple[bool, Optional[Tuple[int, int, int, int]], float, float]:
        """SYNCHRONOUS detection on THIS frame -- blocks the caller ~inference time and returns the
        box immediately, so the caller has a lock on the SAME frame instead of ~4-5 frames later once
        the async worker's result propagates. Intended ONLY for the acquisition phase (no meter locked
        yet), where the ~30ms block costs nothing time-critical (no shot is in flight) but eliminates
        the single-worker onset lag (worker busy on the prior frame + inference + read round-trip that
        puts the async first lock at ~25% fill instead of ~5%). Also publishes into _result so a
        subsequent latest() agrees. Returns (found, box, conf, ts)."""
        return self._detect_now_scoped(frame, ts, "full")

    def detect_now_region(self, frame, ts: float, region: str
                          ) -> Tuple[bool, Optional[Tuple[int, int, int, int]], float, float]:
        """Synchronous one-inference acquisition over full/left/right pixels.

        This intentionally is a distinct opt-in API rather than changing detect_now's
        contract. Existing tracking/read-rescue callers remain full-frame.
        """
        _region = str(region or "full").strip().lower()
        if _region not in ("full", "left", "right"):
            raise ValueError("region must be 'full', 'left', or 'right'")
        return self._detect_now_scoped(frame, ts, _region)

    def _base_detect(self, frame, ts):
        # [ORION_METER_PROPOSER=cv] the CV locator keeps per-frame temporal state (ROI hold,
        # one-frame confirmation) on the FRAME clock; the ONNX locator is stateless.
        if getattr(self._base, "accepts_ts", False):
            return self._base.detect_box(frame, ts=ts)
        return self._base.detect_box(frame)

    def _base_detect_tile(self, frame, side, ts):
        if getattr(self._base, "accepts_ts", False):
            return self._base.detect_box_tile(frame, side, 0.55, ts=ts)
        return self._base.detect_box_tile(frame, side, 0.55)

    def _detect_now_scoped(self, frame, ts: float, region: str):
        if not self.ok or frame is None:
            return (False, None, 0.0, float(ts))
        with self._lock:
            if self._stop:
                return (False, None, 0.0, -1.0)
            self._set_frame_shape_locked(frame)
            generation = self._generation
        t0 = time.perf_counter()
        if region == "full":
            b = self._base_detect(frame, ts)
        else:
            b = self._base_detect_tile(frame, region, ts)
        dt = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            if self._stop or generation != self._generation:
                return (False, None, 0.0, -1.0)
            self.infer_ms = dt
            self._result_scope = region
            if b is None:
                res = (False, None, 0.0, float(ts))
            else:
                x, y, w, h, c = b
                res = (True, (x, y, w, h), float(c), float(ts))
            self._result = res
            self._last_infer_end = time.perf_counter()
        return res

    def _invalidate_locked(self) -> None:
        _reset = getattr(self._base, "reset", None)
        if callable(_reset):
            try:
                _reset()                      # the CV locator keeps temporal state on the frame clock
            except Exception:
                pass
        """Drop old evidence/work while holding _lock; never wait for inference."""
        self._generation += 1
        self._pending = None
        self._result = (False, None, 0.0, -1.0)
        self._result_scope = "full"
        self._frame_shape = None
        self.infer_ms = 0.0
        self._last_infer_end = 0.0

    def _set_frame_shape_locked(self, frame) -> None:
        shape = tuple(frame.shape)
        if self._frame_shape is not None and shape != self._frame_shape:
            self._invalidate_locked()
        self._frame_shape = shape

    def reset(self) -> None:
        """Invalidate source-session evidence without blocking on the worker.

        Call for a source/session reset even when dimensions stay unchanged.
        Geometry changes are detected automatically on submit. The sentinel's
        timestamp -1 means 'no result', not a fresh full-frame no-meter veto.
        Reset does not restart a stopped locator.
        """
        with self._cv:
            self._invalidate_locked()
            self._cv.notify_all()

    def forget_position(self, box=None):
        """Ask the base locator to forget WHERE it last saw a meter (nothing else).

        [ORION_READER_GHOST_FORGET_LOCATOR 2026-09-15] The reader calls this when it evicts or
        retires a leftover meter, so the retired object's position can no longer bridge the
        base locator's own acceptance gates.  Unlike ``reset()`` this does NOT bump the
        generation, drop the pending frame or clear the published result: a legitimately
        in-flight inference of the REAL meter must survive.  Inert for a stateless proposer.

        [box= 2026-09-16] ``box`` names the GHOST being evicted so the base locator can keep
        the first-sight pairs that belong to a different column (see
        ``MeterContourLocator.forget_position``).  Forwarded positionally only when given, so
        a base locator predating the argument still works.  Returns the kept pair names.
        """
        fn = getattr(self._base, "forget_position", None)
        if not callable(fn):
            return ()
        try:
            return fn(box) if box is not None else fn()
        except TypeError:
            try:                       # a base that never learned the argument
                return fn()
            except Exception:
                return ()
        except Exception:
            return ()

    def pending_pairs(self, now=None):
        """[ORION_READER_FORGET_RATE_LIMIT] Forward the base locator's live first-sight pairs."""
        fn = getattr(self._base, "pending_pairs", None)
        if not callable(fn):
            return ()
        try:
            return tuple(fn(now) or ())
        except Exception:
            return ()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._invalidate_locked()
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            # THROTTLE FIRST, THEN GRAB THE FRESHEST FRAME. The wait spaces out inferences so the
            # detector's CPU-heavy preprocessing (which holds the Python GIL ~20ms/cycle) does not
            # starve the preview/reader threads -> the capture-card stutter the owner reported. The
            # condition wait RELEASES the GIL, handing the preview thread a clean window each cycle.
            # Unlike time.sleep(), it can be interrupted by an acquisition/read-rescue priority
            # request, without ever moving inference back onto the capture callback. The meter
            # moves slowly and the reader's detector-box HOLD bridges the wider cadence, so tracking
            # is unaffected. Tunable via ORION_METER_DETECTOR_MIN_INTERVAL_MS (0 disables). 15ms
            # (was 40): the GIL RELEASE is what cures the stutter, not the length of the sleep -- and
            # 40ms added ~40ms of box staleness on top of ~50ms inference, the 'detection lags' report.
            #
            # ORDER MATTERS (first-lock latency): the sleep runs BEFORE the frame grab, not after.
            # Grabbing _pending first and sleeping afterwards meant inferring a frame up to
            # _min_interval_s stale, and -- measured on session_20260828_201813 -- at a shot's onset
            # it meant the worker had already captured the PRE-meter frame when the meter appeared
            # during the sleep, so the meter frame that arrived mid-sleep waited a whole extra cycle
            # (~1 frame / ~5% of fill on the steep initial ramp). Sleeping first lets us grab the
            # freshest pending frame AFTER the GIL-release window, cutting that lag with no cost to
            # the stutter fix (the release still happens every cycle).
            with self._cv:
                while True:
                    while self._pending is None and not self._stop:
                        self._cv.wait()
                    if self._stop:
                        return
                    priority = bool(self._pending[3])
                    gap = (self._min_interval_s
                           - (time.perf_counter() - self._last_infer_end))
                    if priority or gap <= 0.0:
                        frame, ts, scope, _priority = self._pending
                        generation = self._generation
                        self._pending = None
                        break
                    # Wake early if a priority/newer request arrives.  Because submit()
                    # carries the priority bit forward, a following ordinary frame cannot
                    # accidentally put the worker back to sleep.
                    self._cv.wait(timeout=gap)
            t0 = time.perf_counter()
            if scope == "full":
                b = self._base_detect(frame, ts)
            else:
                b = self._base_detect_tile(frame, scope, ts)
            dt = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                if self._stop or generation != self._generation:
                    continue
                self._last_infer_end = time.perf_counter()
                self.infer_ms = dt
                self._result_scope = scope
                if b is None:
                    self._result = (False, None, 0.0, ts)
                else:
                    x, y, w, h, c = b
                    self._result = (True, (x, y, w, h), float(c), ts)


_SINGLETON: Optional[MeterYoloLocator] = None
_ASYNC: Optional[AsyncMeterLocator] = None


def get_locator() -> Optional[MeterYoloLocator]:
    """Process-wide synchronous singleton; None if the detector could not load."""
    global _SINGLETON
    if _SINGLETON is None:
        # [ORION_METER_DETECTOR_CONF 2026-09-03] live confidence threshold knob (default 0.35).
        # Offline on session_20260903_151910 the model returned NOTHING at 0.05 on a visible
        # low-fill meter (e89 +851/+952 ms) and 0.17 on e77 +609 ms: the early-fill blind spot
        # is a training gap first and a threshold second; the knob exists so the replay
        # harness can price a lower threshold against false positives.
        _conf = 0.35
        try:
            _env = os.environ.get("ORION_METER_DETECTOR_CONF", "").strip()
            if _env:
                _v = float(_env)
                if 0.05 <= _v <= 0.95:
                    _conf = _v
        except Exception:
            _conf = 0.35
        # [ORION_METER_PROPOSER=cv] owner A/B 2026-09-10: swap ONLY the box proposer for the
        # pure-CV landmark locator (meter_locator_cv.py); the async wrapper and the reader's
        # in-box fill measurement are unchanged. Unset / "yolo" = the shipped ONNX detector.
        # Explicit on purpose: the dev launcher pins it; the shipped profile does not.
        _which = os.environ.get("ORION_METER_PROPOSER", "").strip().lower()
        if _which == "cv":
            import meter_locator_cv as _mlc
            _SINGLETON = _mlc.MeterContourLocator()
        else:
            _SINGLETON = MeterYoloLocator(conf_thres=_conf)
    return _SINGLETON if _SINGLETON.ok else None


def get_async_locator() -> Optional["AsyncMeterLocator"]:
    """Process-wide async singleton; None if the detector could not load."""
    global _ASYNC
    if _ASYNC is None:
        _ASYNC = AsyncMeterLocator(get_locator())
    return _ASYNC if _ASYNC.ok else None
