"""Live inference for the COMPRESSED-STREAM METER READER (models/meter_reader_y.pt) — the 1-channel
LUMA student CNN. Flag-gated (ORION_METER_READER_Y=1); when off it is NEVER imported, so torch is not
loaded here. Given a LOCATED meter crop's Y (luma) plane it regresses continuous fill + the green
make-window bounds, estimates per-read fill uncertainty (aleatoric sigma), AND arbitrates meter
presence — the acquisition-arbiter role for the chroma-collapsed / re-encoded feed where the classical
red/green cues are gone.

Trained by tools/training/train_meter_reader_y.py. MeterReaderNetY + _preprocess() are BIT-IDENTICAL to
that trainer. Never raises: any failure -> None / disabled (detection keeps its classical read).
Model-optional: if models/meter_reader_y.pt is absent, try_load() returns None and the reader is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("meter_reader_y")

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.environ.get("ORION_METER_READER_Y_MODEL", os.path.join(_ROOT, "models", "meter_reader_y.pt"))
_META = os.path.join(os.path.dirname(_MODEL), "meter_reader_y.json")

INPUT_H = 128
INPUT_W = 48


class MeterReaderY:
    """Wraps the luma meter reader CNN.
    read(y_crop)   -> {fill_pct, green_lo, green_hi, present_p, sigma_pp} | None
    verify(y_crop) -> bool (present_p >= 0.5), the acquisition-arbiter signal."""

    def __init__(self, model_path: str = _MODEL, meta_path: str = _META) -> None:
        self.enabled = False
        self._model = None
        self._input_h = INPUT_H
        self._input_w = INPUT_W
        try:
            import torch  # local: only when the flag enables this path
            import torch.nn as nn

            class MeterReaderNetY(nn.Module):
                """MUST match tools/training/train_meter_reader_y.py._make_net."""

                def __init__(self):
                    super().__init__()
                    self.conv1 = nn.Conv2d(1, 16, 3, padding=1); self.bn1 = nn.BatchNorm2d(16)
                    self.conv2 = nn.Conv2d(16, 32, 3, padding=1); self.bn2 = nn.BatchNorm2d(32)
                    self.conv3 = nn.Conv2d(32, 64, 3, padding=1); self.bn3 = nn.BatchNorm2d(64)
                    self.conv4 = nn.Conv2d(64, 96, 3, padding=1); self.bn4 = nn.BatchNorm2d(96)
                    self.pool = nn.MaxPool2d(2)
                    self.gap = nn.AdaptiveAvgPool2d(1)
                    self.fc1 = nn.Linear(96, 48)
                    self.head_reg = nn.Linear(48, 3)
                    self.head_present = nn.Linear(48, 1)
                    self.head_logvar = nn.Linear(48, 1)
                    self.relu = nn.ReLU(); self.drop = nn.Dropout(0.2)

                def forward(self, x):
                    x = self.pool(self.relu(self.bn1(self.conv1(x))))
                    x = self.pool(self.relu(self.bn2(self.conv2(x))))
                    x = self.pool(self.relu(self.bn3(self.conv3(x))))
                    x = self.relu(self.bn4(self.conv4(x)))
                    x = self.gap(x).flatten(1)
                    h = self.drop(self.relu(self.fc1(x)))
                    reg = torch.sigmoid(self.head_reg(h))
                    present_logit = self.head_present(h).squeeze(-1)
                    log_var = torch.clamp(self.head_logvar(h).squeeze(-1), -6.0, 2.0)
                    return reg, present_logit, log_var

            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                # input is fixed "y_128x48"; parse defensively but keep the trained contract
                if meta.get("input") not in (None, "y_128x48"):
                    logger.warning("meter_reader_y meta input=%s != y_128x48", meta.get("input"))
            except OSError:
                pass

            self._torch = torch
            self._model = MeterReaderNetY()
            self._model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
            self._model.eval()
            self.enabled = True
            logger.info("MeterReaderY ready: %s (input=%dx%d)",
                        os.path.basename(model_path), self._input_h, self._input_w)
        except Exception as exc:  # torch/model missing -> stay disabled, classical read stands
            logger.warning("MeterReaderY unavailable (%s); disabled", exc)
            self.enabled = False

    def _preprocess(self, y_crop):
        """uint8 Y crop -> 1xHxW float32 in [0,1]. MUST match train_meter_reader_y.py.preprocess.

        Accepts [H,W] or [H,W,1] uint8. Resizes with cv2.INTER_AREA only when not already HxW."""
        arr = np.asarray(y_crop)
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        if arr.shape[0] != self._input_h or arr.shape[1] != self._input_w:
            import cv2
            arr = cv2.resize(arr, (self._input_w, self._input_h), interpolation=cv2.INTER_AREA)
        x = arr.astype(np.float32) / 255.0
        return x[np.newaxis, :, :]

    def _forward(self, y_crop):
        feats = self._preprocess(y_crop)
        with self._torch.no_grad():
            t = self._torch.from_numpy(feats).unsqueeze(0)
            reg, plogit, lvar = self._model(t)
        reg = reg[0].cpu().numpy()
        p = float(self._torch.sigmoid(plogit)[0].cpu().numpy())
        sigma_pp = float(np.exp(0.5 * lvar[0].cpu().numpy())) * 100.0
        return reg, p, sigma_pp

    def read(self, y_crop) -> Optional[dict]:
        """Regress the located luma crop. Returns
            {fill_pct(0..100), green_lo(0..100), green_hi(0..100), present_p(0..1), sigma_pp}
        (green_lo<=green_hi) or None on any failure / when disabled."""
        if not self.enabled or self._model is None or y_crop is None or getattr(y_crop, "size", 0) == 0:
            return None
        try:
            reg, p, sigma_pp = self._forward(y_crop)
            fill = float(np.clip(reg[0], 0.0, 1.0)) * 100.0
            lo = float(np.clip(reg[1], 0.0, 1.0)) * 100.0
            hi = float(np.clip(reg[2], 0.0, 1.0)) * 100.0
            if hi < lo:
                lo, hi = hi, lo
            return {"fill_pct": fill, "green_lo": lo, "green_hi": hi,
                    "present_p": p, "sigma_pp": sigma_pp}
        except Exception as exc:
            logger.debug("MeterReaderY inference skip: %s", exc)
            return None

    def verify(self, y_crop) -> bool:
        """Acquisition arbiter: True iff the crop looks like a real meter (present_p >= 0.5).
        Never raises; a disabled reader / failure returns False."""
        if not self.enabled or self._model is None or y_crop is None or getattr(y_crop, "size", 0) == 0:
            return False
        try:
            _, p, _ = self._forward(y_crop)
            return p >= 0.5
        except Exception as exc:
            logger.debug("MeterReaderY verify skip: %s", exc)
            return False


def try_load() -> Optional[MeterReaderY]:
    """Return a MeterReaderY iff ORION_METER_READER_Y=1 and the model exists, else None. Safe to call
    unconditionally (imports torch only when enabled)."""
    if os.environ.get("ORION_METER_READER_Y", "0") != "1":
        return None
    if not os.path.exists(_MODEL):
        logger.warning("ORION_METER_READER_Y=1 but %s missing; classical read stays authoritative", _MODEL)
        return None
    try:
        rd = MeterReaderY()
        return rd if rd.enabled else None
    except Exception:
        return None
