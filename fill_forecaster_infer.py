"""Live inference for the fill-trajectory forecaster (model #3). Flag-gated (ORION_FILL_FORECAST=1); when off,
NEVER imported, so torch is not loaded in the sidecar. Given the running meter fill curve it predicts
`ms_to_tip` (the green window at the top). The engine subtracts the measured total latency to time the release.

Trained by tools/diagnostics/fill_forecaster.py (frames-to-tip, self-supervised on the recorded fill curves).
Keep FillForecastNet / build_features BIT-IDENTICAL to that trainer.
"""
from __future__ import annotations

import json
import os
from collections import deque

import numpy as np

WINDOW = 12
FPS = 60.0
MS_PER_FRAME = 1000.0 / FPS
_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_ROOT, "models", "fill_forecaster", "fill_forecaster.pt")
_META = os.path.join(_ROOT, "models", "fill_forecaster", "fill_forecaster.json")

# --- reset heuristics: a new shot starts when the meter reappears after a gap or the fill drops sharply -------
_GAP_MS = 200.0        # inter-sample gap that ends a shot
_DROP_PCT = 25.0       # a fill drop this large = a recede / new shot -> reset the history


def build_features(fill, green, i, window=WINDOW):
    """MUST match tools/diagnostics/fill_forecaster.py.build_features."""
    seg_f = fill[i - window:i] / 100.0
    seg_g = green[i - window:i] / 100.0
    vel = np.gradient(seg_f) * 10.0
    acc = np.gradient(vel) * 10.0
    gap = np.clip(seg_g - seg_f, -0.2, 1.0)
    return np.stack([seg_f, vel, acc, gap], axis=0).astype(np.float32)


class FillForecaster:
    """Rolling-history forecaster. Feed each detected meter frame via update(); read predict() for ms-to-tip.

    Cheap: one tiny 1D-CNN forward on the last WINDOW resampled samples. Returns None until it has enough
    history / when the meter isn't cleanly rising."""

    def __init__(self, model_path=_MODEL, meta_path=_META):
        import torch  # local: only when the flag enables this path
        import torch.nn as nn

        class FillForecastNet(nn.Module):
            def __init__(self, window=WINDOW, n_features=4):
                super().__init__()
                self.conv1 = nn.Conv1d(n_features, 32, 5, padding=2); self.bn1 = nn.BatchNorm1d(32)
                self.conv2 = nn.Conv1d(32, 64, 3, padding=1); self.bn2 = nn.BatchNorm1d(64)
                self.conv3 = nn.Conv1d(64, 64, 3, padding=1); self.bn3 = nn.BatchNorm1d(64)
                self.gap = nn.AdaptiveAvgPool1d(1)
                self.fc1 = nn.Linear(64, 32); self.fc2 = nn.Linear(32, 1)
                self.relu = nn.ReLU(); self.drop = nn.Dropout(0.2)

            def forward(self, x):
                x = self.relu(self.bn1(self.conv1(x)))
                x = self.relu(self.bn2(self.conv2(x)))
                x = self.relu(self.bn3(self.conv3(x)))
                x = self.gap(x).squeeze(-1)
                x = self.drop(x)
                x = self.relu(self.fc1(x))
                return self.fc2(x).squeeze(-1)

        self._torch = torch
        self.bias_frames = 0.0
        try:
            with open(meta_path, encoding="utf-8") as f:
                self.bias_frames = float(json.load(f).get("bias_frames", 0.0))
        except OSError:
            pass
        self.net = FillForecastNet()
        self.net.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
        self.net.eval()
        self.available = True
        self._t = deque(maxlen=64)
        self._f = deque(maxlen=64)
        self._g = deque(maxlen=64)

    def reset(self):
        self._t.clear(); self._f.clear(); self._g.clear()

    def update(self, t_ms, fill_pct, green_center_pct, detected):
        """Push a frame. Resets the shot history on a not-detected frame, a time gap, or a sharp fill drop."""
        if not detected:
            self.reset()
            return
        if self._t:
            if (t_ms - self._t[-1]) > _GAP_MS or (self._f[-1] - fill_pct) > _DROP_PCT:
                self.reset()
        self._t.append(float(t_ms))
        self._f.append(float(fill_pct))
        self._g.append(float(green_center_pct) if green_center_pct is not None and green_center_pct >= 0 else 96.0)

    def predict(self):
        """Bias-corrected ms until the fill peaks (the tip). None if history is short or the meter is already at
        the top / receding (nothing to forecast)."""
        if len(self._t) < WINDOW + 1:
            return None
        t = np.asarray(self._t, float)
        f = np.asarray(self._f, float)
        g = np.asarray(self._g, float)
        # resample onto a uniform 60fps grid ending at the latest sample (matches the trainer)
        t0, t1 = t[0], t[-1]
        if t1 - t0 < WINDOW * MS_PER_FRAME * 0.5:
            return None
        grid = np.arange(t0, t1 + 1e-3, MS_PER_FRAME)
        if len(grid) < WINDOW:
            return None
        fg = np.interp(grid, t, f)
        gg = np.interp(grid, t, g)
        feats = build_features(fg, gg, len(grid))          # window ending at the latest grid sample
        with self._torch.no_grad():
            x = self._torch.from_numpy(feats).unsqueeze(0)
            pf = float(self._torch.expm1(self._torch.clamp(self.net(x), min=0)).item())
        pf -= self.bias_frames
        return max(0.0, pf) * MS_PER_FRAME


def try_load():
    """Return a FillForecaster if the flag is on and the model loads, else None. Safe to call unconditionally."""
    if os.environ.get("ORION_FILL_FORECAST", "0") != "1":
        return None
    if not os.path.exists(_MODEL):
        return None
    try:
        return FillForecaster()
    except Exception:
        return None
