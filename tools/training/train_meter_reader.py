#!/usr/bin/env python3
"""Train the SUB-PIXEL METER READER (models/meter_reader.pt) — a tiny CNN that regresses continuous fill +
the green make-window bounds from a LOCATED meter crop.

Input : BGR crop resized to a fixed size (default 96x32 HxW; the meter is a tall vertical bar so height >>
        width to preserve sub-pixel vertical resolution).
Output: 3 sigmoid values -> [fill_frac(0..1), green_top_frac(0..1), green_bottom_frac(0..1)].
Loss  : MSE. Data: tools/diagnostics/synth_meter_reader_data.py (npz of images+labels+split).

The MeterReaderNet class and the preprocess() below MUST stay BIT-IDENTICAL to meter_reader_infer.py.

USAGE:
  # 1) data:   python tools/diagnostics/synth_meter_reader_data.py --n 8000
  # 2) train:  python tools/training/train_meter_reader.py --epochs 40
  # smoke:     python tools/training/train_meter_reader.py --epochs 1 --device cpu --limit 200
Outputs models/meter_reader.pt + models/meter_reader.json.
"""
from __future__ import annotations

import argparse
import json
import os

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA = os.path.join(ROOT, "datasets", "meter_reader_synth", "meter_reader_data.npz")
OUT_PT = os.path.join(ROOT, "models", "meter_reader.pt")
OUT_JSON = os.path.join(ROOT, "models", "meter_reader.json")


def _make_net(torch):
    import torch.nn as nn

    class MeterReaderNet(nn.Module):
        """3 conv blocks -> global avg pool -> FC -> 3 sigmoid outputs. MUST match meter_reader_infer.py."""

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

    return MeterReaderNet()


def preprocess(crop_bgr, input_h, input_w):
    """BGR uint8 crop -> CHW float32 in [0,1] at the fixed input size. MUST match meter_reader_infer.py."""
    if crop_bgr.shape[0] != input_h or crop_bgr.shape[1] != input_w:
        crop_bgr = cv2.resize(crop_bgr, (input_w, input_h), interpolation=cv2.INTER_AREA)
    x = crop_bgr.astype(np.float32) / 255.0
    return np.transpose(x, (2, 0, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--device", default="auto", help="auto|cpu|cuda|0")
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--input-h", type=int, default=96)
    ap.add_argument("--input-w", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0, help="cap total samples (for a fast CPU smoke); 0=all")
    args = ap.parse_args()

    import torch
    from torch.utils.data import DataLoader, TensorDataset

    if args.device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = args.device

    d = np.load(args.data)
    images, labels, split = d["images"], d["labels"], d["split"]
    input_h = int(d["img_h"]) if "img_h" in d else args.input_h
    input_w = int(d["img_w"]) if "img_w" in d else args.input_w
    if args.limit and args.limit < len(images):
        images, labels, split = images[:args.limit], labels[:args.limit], split[:args.limit]

    X = np.stack([preprocess(im, input_h, input_w) for im in images]).astype(np.float32)
    Y = labels.astype(np.float32)
    tr_mask = split == 0
    va_mask = split == 1
    if not va_mask.any():  # tiny smoke subset may have no val rows -> borrow a few
        va_mask = np.zeros_like(tr_mask); va_mask[: max(1, len(tr_mask) // 10)] = True

    xt = torch.from_numpy(X[tr_mask]); yt = torch.from_numpy(Y[tr_mask])
    xv = torch.from_numpy(X[va_mask]); yv = torch.from_numpy(Y[va_mask])
    tr_loader = DataLoader(TensorDataset(xt, yt), batch_size=args.batch, shuffle=True)
    va_loader = DataLoader(TensorDataset(xv, yv), batch_size=args.batch, shuffle=False)

    net = _make_net(torch).to(device)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    lossf = torch.nn.MSELoss()

    os.makedirs(os.path.dirname(OUT_PT), exist_ok=True)
    best = float("inf")
    for ep in range(args.epochs):
        net.train()
        tr_loss = 0.0
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = lossf(net(xb), yb)
            loss.backward()
            opt.step()
            tr_loss += loss.item() * len(xb)
        tr_loss /= max(1, len(xt))

        net.eval()
        va_loss = 0.0
        with torch.no_grad():
            for xb, yb in va_loader:
                xb, yb = xb.to(device), yb.to(device)
                va_loss += lossf(net(xb), yb).item() * len(xb)
        va_loss /= max(1, len(xv))
        print(f"epoch {ep + 1}/{args.epochs}  train_mse={tr_loss:.5f}  val_mse={va_loss:.5f}")

        if va_loss <= best:
            best = va_loss
            torch.save(net.state_dict(), OUT_PT)

    if not os.path.exists(OUT_PT):  # degenerate (0 val samples etc.) -> always save the final weights
        torch.save(net.state_dict(), OUT_PT)

    meta = {
        "input_h": int(input_h),
        "input_w": int(input_w),
        "normalize": "div255",
        "outputs": ["fill_frac", "green_top_frac", "green_bottom_frac"],
        "arch": "meter_reader_cnn_v1",
        "best_val_mse": float(best),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"saved {OUT_PT} (best_val_mse={best:.5f}) + {OUT_JSON}")


if __name__ == "__main__":
    main()
