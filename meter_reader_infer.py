"""Live inference for the SUB-PIXEL METER READER (models/meter_reader.pt) — the read-jitter fix. Flag-gated
(ORION_METER_READER=1); when off it is NEVER imported, so torch is not loaded here. Given a LOCATED meter
CROP (BGR, the same crop the classical reader scans) it regresses continuous fill + the green make-window
bounds, replacing the ~1-2% row-counting quantization that flows into velocity -> tip prediction -> release
jitter, and it reads the contested "fade" green sliver the row-scan struggles with.

Trained by tools/training/train_meter_reader.py. MeterReaderNet + preprocess() are BIT-IDENTICAL to that
trainer. Never raises: any failure -> None (detection keeps its classical fill%/green read). Model-optional:
if models/meter_reader.pt is absent, try_load() returns None and the detector is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("meter_reader")

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_ROOT, "models", "meter_reader.pt")
_META = os.path.join(_ROOT, "models", "meter_reader.json")


class MeterReader:
    """Wraps the meter reader CNN. read(crop_bgr) -> {fill_pct, green_lo, green_hi} | None."""

    def __init__(self, model_path: str = _MODEL, meta_path: str = _META) -> None:
        self.enabled = False
        self._model = None
        self._input_h = 96
        self._input_w = 32
        try:
            import torch  # local: only when the flag enables this path
            import torch.nn as nn
            import cv2  # noqa: F401  (fail fast here if cv2 is missing, like the classical path)

            class MeterReaderNet(nn.Module):
                """MUST match tools/training/train_meter_reader.py."""

                def __init__(self):
                    super().__init__()
                    self.conv1 = nn.Conv2d(3, 16, 3, padding=1); self.bn1 = nn.BatchNorm2d(16)
                    self.conv2 = nn.Conv2d(16, 32, 3, padding=1); self.bn2 = nn.BatchNorm2d(32)
                    self.conv3 = nn.Conv2d(32, 64, 3, padding=1); self.bn3 = nn.BatchNorm2d(64)
                    self.pool = nn.MaxPool2d(2)
                    self.gap = nn.AdaptiveAvgPool2d(1)
                    self.fc1 = nn.Linear(64, 32); self.fc2 = nn.Linear(32, 3)
                    self.relu = nn.ReLU(); self.drop = nn.Dropout(0.2)

                def forward(self, x):
                    x = self.pool(self.relu(self.bn1(self.conv1(x))))
                    x = self.pool(self.relu(self.bn2(self.conv2(x))))
                    x = self.relu(self.bn3(self.conv3(x)))
                    x = self.gap(x).flatten(1)
                    x = self.drop(self.relu(self.fc1(x)))
                    return torch.sigmoid(self.fc2(x))

            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                self._input_h = int(meta.get("input_h", self._input_h))
                self._input_w = int(meta.get("input_w", self._input_w))
            except OSError:
                pass

            self._torch = torch
            self._model = MeterReaderNet()
            self._model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
            self._model.eval()
            self.enabled = True
            logger.info("MeterReader ready: %s (input=%dx%d)",
                        os.path.basename(model_path), self._input_h, self._input_w)
        except Exception as exc:  # torch/cv2/model missing -> stay disabled, detector uses its classical read
            logger.warning("MeterReader unavailable (%s); disabled", exc)
            self.enabled = False

    def _preprocess(self, crop_bgr):
        """BGR uint8 crop -> CHW float32 in [0,1] at the fixed input size. MUST match train_meter_reader.py."""
        import cv2
        if crop_bgr.shape[0] != self._input_h or crop_bgr.shape[1] != self._input_w:
            crop_bgr = cv2.resize(crop_bgr, (self._input_w, self._input_h), interpolation=cv2.INTER_AREA)
        x = crop_bgr.astype(np.float32) / 255.0
        return np.transpose(x, (2, 0, 1))

    def read(self, crop_bgr) -> Optional[dict]:
        """Regress the located crop. Returns {fill_pct(0..100), green_lo, green_hi} (green_* are fractions of
        crop height, green_lo<=green_hi) or None on any failure / when disabled."""
        if not self.enabled or self._model is None or crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
            return None
        try:
            feats = self._preprocess(crop_bgr)
            with self._torch.no_grad():
                t = self._torch.from_numpy(feats).unsqueeze(0)
                out = self._model(t)[0].cpu().numpy()
            fill = float(np.clip(out[0], 0.0, 1.0)) * 100.0
            lo = float(np.clip(out[1], 0.0, 1.0))
            hi = float(np.clip(out[2], 0.0, 1.0))
            if hi < lo:
                lo, hi = hi, lo
            return {"fill_pct": fill, "green_lo": lo, "green_hi": hi}
        except Exception as exc:
            logger.debug("MeterReader inference skip: %s", exc)
            return None


def try_load() -> Optional[MeterReader]:
    """Return a MeterReader iff ORION_METER_READER=1 and the model exists, else None. Safe to call
    unconditionally (imports torch only when enabled)."""
    if os.environ.get("ORION_METER_READER", "0") != "1":
        return None
    if not os.path.exists(_MODEL):
        logger.warning("ORION_METER_READER=1 but %s missing; classical read stays authoritative", _MODEL)
        return None
    try:
        rd = MeterReader()
        return rd if rd.enabled else None
    except Exception:
        return None
