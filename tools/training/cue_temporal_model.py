#!/usr/bin/env python3
"""1D TCN cue temporal model for No-Meter Mode: pose sequences -> release timing + cue class.

Two heads on one causal dilated-convolution backbone (docs/NO_METER_MODE_TRAINING.md Stage 5):

  timing head  - scalar FRAMES-UNTIL-RELEASE measured from the window's last frame. This is
                 what the bot USES: it feeds the tip-timing arm decision the same way the
                 meter tip does.
  cue head     - softmax over {set_point, push, jump, flick, release, none} for the window's
                 last frame. Live corroboration with the meter + debugging interpretability.

CAUSAL is the shipping constraint: every convolution is left-padded and right-chomped, so the
prediction at the last timestep sees only frames <= t -- the runtime queries at "now" with
nothing but past frames in its buffer. No future-frame leakage by construction.

Feature contract (the RUNTIME must mirror this EXACTLY or predictions are garbage; the
trainer also records it in cue_temporal_model_meta.json):

  input   (N, 51, T) float32, channels-first; T = window (default 60 frames ~ 1s @ 60fps)
  51      = 17 COCO joints x [x, y, v], joint-major (x0, y0, v0, x1, y1, v1, ...)
  x, y    = (pixel - bbox_center) / bbox_diagonal, where bbox is the tight box over the
            joints with conf >= 0.30 (KP_CONF_FLOOR, the pose_timing.py convention).
            Translation- and scale-invariant; <2 visible joints => frame fully zeroed, v=0.
  v       = raw visibility flag 0/1/2 (2 if conf > 0.5, 1 if conf > 0.1, else 0 -- the
            pose_pseudolabel_dataset.py convention). NEVER silently zero-filled: an occluded
            joint keeps x = y = 0 but the model sees v = 0 and learns to down-weight it.
  mirror  left-handed players are mirrored to right-handed: joints reordered by
            COCO_FLIP_IDX and the (centered) x negated.

This module is import-safe WITHOUT torch: normalization + constants are numpy-only, and
build_model() fails loud with the install hint when torch is missing (never a top-level
hard import of an optional dep).
"""
from __future__ import annotations

import numpy as np

# ---- classes / geometry constants -------------------------------------------------------------
CUE_CLASSES = ("set_point", "push", "jump", "flick", "release", "none")
NUM_CLASSES = len(CUE_CLASSES)
NUM_JOINTS = 17
INPUT_DIM = NUM_JOINTS * 3  # x, y, v per joint = 51

KP_CONF_FLOOR = 0.30  # pose_timing.py convention: below this a joint is "not visible"
# Shooting-arm joints AFTER mirroring (everything is right-handed post-normalization):
ARM_SHOULDER, ARM_ELBOW, ARM_WRIST = 6, 8, 10
# Left/right joint swap for handedness mirroring (pose_pseudolabel_dataset.py convention).
COCO_FLIP_IDX = [0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15]

# ---- default architecture (3060 inference well under 2ms/frame) -------------------------------
DEFAULT_CHANNELS = (64, 128, 128, 128)
DEFAULT_KERNEL = 3
DEFAULT_DILATIONS = (1, 2, 4, 8)
DEFAULT_DROPOUT = 0.1
DEFAULT_WINDOW = 60

TORCH_FIX = (
    "torch is required for the model itself (normalization helpers are numpy-only).\n"
    "Install the CUDA build -- see the preflight in train_cue_temporal_model.py for the\n"
    "exact pip commands (never the +cpu build on the training rig, task #69)."
)

try:  # optional dep: this module must stay importable for numpy-only consumers
    import torch  # noqa: F401
    from torch import nn
except Exception as _exc:  # pragma: no cover - environment dependent
    torch = None
    nn = None
    _TORCH_ERR = _exc
else:
    _TORCH_ERR = None


# ==============================================================================================
# Feature normalization (numpy-only; the runtime mirrors this)
# ==============================================================================================

def visibility_flags(conf):
    """YOLO keypoint confidence -> raw 0/1/2 visibility (pose_pseudolabel_dataset.py rule)."""
    conf = np.asarray(conf, dtype=np.float32)
    return np.where(conf > 0.5, 2.0, np.where(conf > 0.1, 1.0, 0.0)).astype(np.float32)


def normalize_frames(kpts, mirror=False):
    """(T, 17, 3) pixel keypoints [x, y, conf] -> (T, 51) float32 model features.

    Per frame: center on the tight bbox of visible joints (conf >= KP_CONF_FLOOR), scale by
    the bbox diagonal, keep the raw 0/1/2 visibility as its own channel. Occluded joints get
    x = y = 0 WITH v = 0 (the missingness is explicit, never silent). mirror=True converts a
    left-handed pose to right-handed (COCO_FLIP_IDX reorder + x negation).
    """
    kpts = np.asarray(kpts, dtype=np.float32)
    T = kpts.shape[0]
    out = np.zeros((T, INPUT_DIM), dtype=np.float32)
    for f in range(T):
        kp = kpts[f]
        v = visibility_flags(kp[:, 2])
        vis = kp[:, 2] >= KP_CONF_FLOOR
        x = np.zeros(NUM_JOINTS, dtype=np.float32)
        y = np.zeros(NUM_JOINTS, dtype=np.float32)
        if vis.sum() >= 2:
            xs, ys = kp[vis, 0], kp[vis, 1]
            cx = (float(xs.min()) + float(xs.max())) / 2.0
            cy = (float(ys.min()) + float(ys.max())) / 2.0
            diag = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
            if diag > 1e-3:
                x[vis] = (kp[vis, 0] - cx) / diag
                y[vis] = (kp[vis, 1] - cy) / diag
            else:
                v[:] = 0.0  # degenerate cluster: treat the whole frame as missing
        else:
            v[:] = 0.0
        if mirror:
            x = -x[COCO_FLIP_IDX]
            y = y[COCO_FLIP_IDX]
            v = v[COCO_FLIP_IDX]
        out[f, 0::3] = x
        out[f, 1::3] = y
        out[f, 2::3] = v
    return out


def normalization_meta():
    """The exact contract the runtime must reproduce; embedded in cue_temporal_model_meta.json."""
    return {
        "kp_conf_floor": KP_CONF_FLOOR,
        "center": "midpoint of the tight bbox over joints with conf >= kp_conf_floor",
        "scale": "bbox diagonal in pixels: x,y = (pixel - center) / diagonal",
        "visibility": "raw 0/1/2 channel: 2 if conf > 0.5, 1 if conf > 0.1, else 0",
        "missing_joints": "x = y = 0 with v = 0 (v channel carries missingness, never silent)",
        "degenerate_frame": "<2 visible joints or diagonal <= 1e-3 => all channels 0, all v 0",
        "handedness": "left-handed mirrored to right: reorder joints by coco_flip_idx, negate x",
        "coco_flip_idx": list(COCO_FLIP_IDX),
        "feature_layout": "joint-major [x, y, v] x 17 COCO joints = 51 channels, (N, 51, T)",
    }


# ==============================================================================================
# Model (torch-gated)
# ==============================================================================================

if nn is not None:

    class _CausalBlock(nn.Module):
        """Two dilated causal convs + residual: left-pad, right-chomp => no future leakage."""

        def __init__(self, c_in, c_out, kernel, dilation, dropout):
            super().__init__()
            self.pad = (kernel - 1) * dilation
            self.conv1 = nn.Conv1d(c_in, c_out, kernel, padding=self.pad, dilation=dilation)
            self.conv2 = nn.Conv1d(c_out, c_out, kernel, padding=self.pad, dilation=dilation)
            self.relu = nn.ReLU()
            self.drop = nn.Dropout(dropout)
            self.down = nn.Conv1d(c_in, c_out, 1) if c_in != c_out else None

        def forward(self, x):
            y = self.drop(self.relu(self.conv1(x)[:, :, :-self.pad]))
            y = self.drop(self.relu(self.conv2(y)[:, :, :-self.pad]))
            res = x if self.down is None else self.down(x)
            return self.relu(y + res)

    class CueTemporalModel(nn.Module):
        """1D TCN backbone + timing-regression head + cue-classification head.

        forward(x: (N, 51, T)) -> (frames_until_release: (N,), cue_logits: (N, 6)).
        Both heads read the LAST timestep's features (causal => "now").
        """

        def __init__(self, input_dim=INPUT_DIM, channels=DEFAULT_CHANNELS, kernel=DEFAULT_KERNEL,
                     dilations=DEFAULT_DILATIONS, dropout=DEFAULT_DROPOUT, num_classes=NUM_CLASSES):
            super().__init__()
            if len(channels) != len(dilations):
                raise ValueError(f"channels {channels} vs dilations {dilations} length mismatch")
            blocks = []
            c_prev = input_dim
            for c, d in zip(channels, dilations):
                blocks.append(_CausalBlock(c_prev, int(c), int(kernel), int(d), float(dropout)))
                c_prev = int(c)
            self.backbone = nn.Sequential(*blocks)
            self.timing_head = nn.Sequential(nn.Linear(c_prev, 64), nn.ReLU(), nn.Linear(64, 1))
            self.cue_head = nn.Sequential(nn.Linear(c_prev, 64), nn.ReLU(), nn.Linear(64, num_classes))

        def forward(self, x):
            h = self.backbone(x)[:, :, -1]
            return self.timing_head(h).squeeze(-1), self.cue_head(h)


def build_model(**kwargs):
    """Construct a CueTemporalModel, failing loud (not at import) when torch is absent."""
    if nn is None:
        raise RuntimeError(f"cannot build CueTemporalModel: torch import failed ({_TORCH_ERR}).\n{TORCH_FIX}")
    return CueTemporalModel(**kwargs)


def count_params(model):
    return int(sum(p.numel() for p in model.parameters()))


def arch_dict(channels=DEFAULT_CHANNELS, kernel=DEFAULT_KERNEL, dilations=DEFAULT_DILATIONS,
              dropout=DEFAULT_DROPOUT):
    return {"input_dim": INPUT_DIM, "channels": list(channels), "kernel": int(kernel),
            "dilations": list(dilations), "dropout": float(dropout),
            "num_classes": NUM_CLASSES, "classes": list(CUE_CLASSES)}
