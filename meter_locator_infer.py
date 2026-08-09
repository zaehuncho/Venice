"""Live inference for the trained meter LOCATOR (YOLO11n, models/orion_meter_n.pt) — the reject-random-
objects fix. Flag-gated (ORION_METER_LOCATOR=1); when off, NEVER imported, so torch/ultralytics aren't
loaded here. Given a full frame it returns the highest-confidence meter bbox (x,y,w,h) in native-frame
pixels, or None. The detector then CONSTRAINS the colour scan to that box, so a red distractor (jersey /
UI / court logo) elsewhere is never considered, and it finds the player-attached meter ANYWHERE (variable
position) — what the classical colour+play-band cannot.

Trained by tools/training/train_meter_detector.py on synthetic full-frame + auto-labeled real park clips.
Never raises: any failure -> None (detection falls back to the classical park path). imgsz/conf/skip are
env-tunable so it can be throttled if it costs too much GPU at 60fps.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("meter_locator")

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_ROOT, "models", "orion_meter_n.pt")


class MeterLocator:
    """Wraps the YOLO meter detector. locate(frame_bgr) -> (x,y,w,h) native px | None."""

    def __init__(self, model_path: str = _MODEL, imgsz: int = 640, conf: float = 0.35, every: int = 1) -> None:
        self.enabled = False
        self._model = None
        self._imgsz = int(imgsz)
        self._conf = float(conf)
        self._every = max(1, int(every))
        self._n = 0
        self._last: Optional[Tuple[int, int, int, int]] = None
        self._last_conf = 0.0
        self._device = "cpu"
        self._half = False
        _cuda_avail = False
        try:
            import torch  # local: only when the flag enables this path
            _cuda_avail = bool(torch.cuda.is_available())
            from ultralytics import YOLO
            # ORION_METER_LOCATOR_DEVICE forces cpu/cuda (used to gate candidate weights on CPU while a
            # training job owns the GPU); default = cuda when available.
            _dev = os.environ.get("ORION_METER_LOCATOR_DEVICE", "").strip().lower()
            if _dev in ("cpu", "cuda", "0"):
                self._device = "cpu" if _dev == "cpu" else 0
            else:
                self._device = 0 if _cuda_avail else "cpu"
            # FP16 on GPU: measured ~20% faster with byte-identical boxes at the live imgsz (IoU 1.000,
            # same detections). Opt out with ORION_METER_LOCATOR_HALF=0. No effect on CPU.
            self._half = (os.environ.get("ORION_METER_LOCATOR_HALF", "1") == "1") and self._device != "cpu"
            self._model = YOLO(model_path)
            # one warmup pass so the first real frame isn't a stall
            self._model.predict(np.zeros((32, 32, 3), np.uint8), imgsz=self._imgsz, conf=self._conf,
                                device=self._device, half=self._half, verbose=False)
            self.enabled = True
            # LOUD, PARSEABLE init verdict (WARNING so it reaches orion_native.log via the stderr relay).
            # RC-2 root-cause aid: if the locator silently runs on CPU (device=cpu) or fails to load, the
            # detector falls back to the ~110ms classical CV path -> cv_fps~8 -> stale meter. This line makes
            # the load OUTCOME (device + cuda availability + which weights) unambiguous in the live log.
            _devstr = ("cuda:%s" % self._device) if self._device != "cpu" else "cpu"
            logger.warning("METER_LOCATOR verdict=LOADED device=%s half=%s torch_cuda=%s model=%s "
                           "imgsz=%d conf=%.2f every=%d",
                           _devstr, self._half, _cuda_avail, os.path.basename(model_path),
                           self._imgsz, self._conf, self._every)
            if self._device == "cpu":
                logger.warning("METER_LOCATOR WARNING: running on CPU (torch_cuda=%s) -> full-frame YOLO is "
                               "SLOW; expect detect_ms high / cv_fps low. Check the CUDA torch build.",
                               _cuda_avail)
        except Exception as exc:  # torch/ultralytics/model missing -> stay disabled, detector uses classical
            logger.warning("METER_LOCATOR verdict=FAILED torch_cuda=%s model=%s exc=%r -> disabled "
                           "(classical CV path is authoritative)", _cuda_avail, os.path.basename(model_path), exc)
            self.enabled = False

    def locate(self, frame_bgr, imgsz: Optional[int] = None) -> Optional[Tuple[int, int, int, int]]:
        """imgsz overrides the configured inference size for THIS call only (T6 lock-ROI fast path:
        the detector passes a scale-preserving smaller imgsz for a crop around the tracked meter, so
        the meter keeps its trained pixel scale at a fraction of the full-frame cost). NOTE: with
        every>1 the reused self._last box is in the caller's LAST coordinate frame — the frame-skip
        reuse is only coherent for a fixed-geometry caller (the production every=1 path is exact)."""
        if not self.enabled or self._model is None or frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return None
        # Optional frame-skip: reuse the last box for (every-1) frames (the meter moves slowly within a
        # shot) to cap GPU cost. every=1 (default) runs every frame.
        self._n += 1
        if self._every > 1 and (self._n % self._every) != 0:
            return self._last
        try:
            res = self._model.predict(frame_bgr, imgsz=int(imgsz) if imgsz else self._imgsz, conf=self._conf,
                                      device=self._device, half=self._half, verbose=False)
            r = res[0] if res else None
            boxes = getattr(r, "boxes", None) if r is not None else None
            if boxes is None or len(boxes) == 0:
                self._last = None
                self._last_conf = 0.0
                return None
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy()
            i = int(np.argmax(confs))                       # highest-confidence meter
            x1, y1, x2, y2 = xyxy[i]
            box = (int(round(x1)), int(round(y1)), int(round(max(1.0, x2 - x1))), int(round(max(1.0, y2 - y1))))
            self._last = box
            # Keep the REAL YOLO score (2026-07-02): locate() used to discard it, so the detector had
            # no way to gate a weak/false box — every located box read as fully trusted downstream.
            self._last_conf = float(confs[i])
            return box
        except Exception as exc:
            logger.debug("MeterLocator inference skip: %s", exc)
            return self._last

    @property
    def last_conf(self) -> float:
        """YOLO confidence of the box last returned by locate() (frame-skip reuses carry it forward)."""
        return self._last_conf


def try_load() -> Optional[MeterLocator]:
    """Return a MeterLocator iff ORION_METER_LOCATOR=1 and the model exists, else None. Safe to call
    unconditionally (imports torch only when enabled)."""
    if os.environ.get("ORION_METER_LOCATOR", "0") != "1":
        logger.info("METER_LOCATOR verdict=disabled why=ORION_METER_LOCATOR!=1")
        return None
    # ORION_METER_LOCATOR_MODEL overrides the weights path (A/B a candidate without clobbering the
    # shipped models/orion_meter_n.pt); default = the production path.
    model_path = os.environ.get("ORION_METER_LOCATOR_MODEL", "").strip() or _MODEL
    if not os.path.exists(model_path):
        logger.warning("METER_LOCATOR verdict=disabled why=model_missing path=%s "
                       "(classical detection stays authoritative)", model_path)
        return None
    try:
        loc = MeterLocator(
            model_path=model_path,
            # default matches TRAINING imgsz (tools/training/train_meter_detector.py --imgsz): the meter is
            # ~8px wide at 768 — inferring smaller both shrinks it and mismatches the trained scale.
            imgsz=int(os.environ.get("ORION_METER_LOCATOR_IMGSZ", "768")),
            conf=float(os.environ.get("ORION_METER_LOCATOR_CONF", "0.35")),
            every=int(os.environ.get("ORION_METER_LOCATOR_EVERY", "1")),
        )
        return loc if loc.enabled else None
    except Exception:
        return None
