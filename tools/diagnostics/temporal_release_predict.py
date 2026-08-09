#!/usr/bin/env python3
"""Temporal release predictor — trains a 1D-CNN to predict the time-to-release
from the wrist-Y trajectory, enabling OPEN-LOOP timing (predict before release).

The zero-crossing detector (Stage 1) is closed-loop: it detects the release
AT the moment it happens, with 1-frame delay. A temporal model could predict
the release BEFORE it happens, eliminating the delay entirely.

Architecture: 1D-CNN on the last N frames of wrist-Y (normalized).
  Input: (N, 3) — wrist-Y, velocity, acceleration
  Output: scalar = predicted frames-to-release (0 = release frame)

Labels: the zero-crossing frame (from the causal detector) relative to each
input frame. The model learns to predict how many frames until the crossing.

Training data: all V3-V7 clips, wrist-Y trajectories with meter-detected
shot edges as ground truth.

Usage:
    # Train:
    C:\\Python314\\python.exe tools/diagnostics/temporal_release_predict.py train
    # Evaluate:
    C:\\Python314\\python.exe tools/diagnostics/temporal_release_predict.py eval "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys
import pickle

import numpy as np
import cv2
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
WINDOW = 30  # frames of history the model sees
HORIZON = 45  # max frames ahead to predict
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]
VIDEOS = r"C:\Users\Administrator\Videos"
MODEL_PATH = os.path.join(ROOT, "models", "release_predictor.pt")


class ReleaseCNN(nn.Module):
    """1D-CNN that predicts frames-to-release from wrist trajectory."""

    def __init__(self, window=WINDOW, n_features=3):
        super().__init__()
        self.conv1 = nn.Conv1d(n_features, 32, kernel_size=5, padding=2)
        self.conv2 = nn.Conv1d(32, 64, kernel_size=5, padding=2)
        self.conv3 = nn.Conv1d(64, 64, kernel_size=3, padding=1)
        self.pool = nn.MaxPool1d(2)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc1 = nn.Linear(64, 32)
        self.fc2 = nn.Linear(32, 1)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.2)

    def forward(self, x):
        # x: (batch, features, window)
        x = self.relu(self.conv1(x))
        x = self.pool(x)
        x = self.relu(self.conv2(x))
        x = self.pool(x)
        x = self.relu(self.conv3(x))
        x = self.gap(x).squeeze(-1)  # (batch, 64)
        x = self.drop(x)
        x = self.relu(self.fc1(x))
        x = self.fc2(x).squeeze(-1)  # (batch,)
        return x


class TrajectoryDataset(Dataset):
    """Generates (window, frames_to_release) pairs from trajectory data."""

    def __init__(self, trajectories, release_frames, window=WINDOW, horizon=HORIZON):
        self.samples = []
        for traj, releases in zip(trajectories, release_frames):
            if len(traj) < window + 1:
                continue
            for release_frame in releases:
                for t in range(max(window, release_frame - horizon), min(len(traj), release_frame + 5)):
                    if t < window:
                        continue
                    # Input: last `window` frames of (y, vel, acc)
                    y_seg = traj[t - window:t]
                    vel = np.gradient(y_seg)
                    acc = np.gradient(vel)
                    features = np.stack([y_seg, vel, acc], axis=0)  # (3, window)

                    # Normalize
                    mean = features.mean(axis=1, keepdims=True)
                    std = features.std(axis=1, keepdims=True) + 1e-8
                    features = (features - mean) / std

                    # Label: frames to release (0 = release frame, negative = after release)
                    label = float(release_frame - t)

                    # Only keep samples within the prediction horizon
                    if -5 <= label <= horizon:
                        self.samples.append((features.astype(np.float32), label))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        features, label = self.samples[idx]
        return torch.from_numpy(features), torch.tensor(label, dtype=torch.float32)


def extract_trajectories(video, count, start, handed, meter_color):
    """Extract wrist-Y trajectory and shot edges from a clip."""
    pose = PoseTimingDetector(handedness=handed)
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    wy_raw = []
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            pose.set_player_hint((bx, by, bx + bw, by + bh))
        try:
            pose.update(frame, seq, frame_time=seq / fps)
        except Exception:
            pass
        j = (pose._buf_idx - 1) % pose._max_buf
        wy_raw.append(float(pose._wrist_y[j]))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()

    # Forward-fill NaNs
    wy = np.array(wy_raw, dtype=float)
    mask = ~np.isnan(wy)
    if mask.sum() > 10:
        wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])
    else:
        return None, None

    # Find zero-crossing frames (the release) using causal EMA
    alpha = 1.0 - np.exp(-16.67 / 20.0)  # τ=20ms
    ema = np.zeros_like(wy)
    ema[0] = wy[0]
    for i in range(1, len(wy)):
        ema[i] = alpha * wy[i] + (1 - alpha) * ema[i - 1]
    vel = np.zeros_like(ema)
    vel[1:] = ema[1:] - ema[:-1]

    release_frames = []
    for e in shot_edges:
        lo = e
        hi = min(len(vel), e + 60)
        for i in range(lo, hi - 1):
            if vel[i] < 0 and vel[i + 1] >= 0:
                release_frames.append(i)
                break

    return wy, release_frames


def train():
    """Train the temporal model on all clips."""
    print("TEMPORAL RELEASE PREDICTOR — Training")
    print(f"Device: {DEVICE}")
    print(f"{'='*70}")

    all_trajectories = []
    all_releases = []

    for clip_name, count, start, handed, mcolor, label in CLIPS:
        video = os.path.join(VIDEOS, clip_name)
        if not os.path.exists(video):
            print(f"  [skip] {clip_name} not found")
            continue
        print(f"  Extracting {label}...", end=" ", flush=True)
        traj, releases = extract_trajectories(video, count, start, handed, mcolor)
        if traj is None:
            print("FAILED")
            continue
        print(f"{len(traj)} frames, {len(releases)} releases")
        all_trajectories.append(traj)
        all_releases.append(releases)

    if not all_trajectories:
        print("  [error] no data extracted")
        return

    # Build dataset
    print("  Building dataset...", end=" ", flush=True)
    dataset = TrajectoryDataset(all_trajectories, all_releases, WINDOW, HORIZON)
    print(f"{len(dataset)} samples")

    if len(dataset) < 100:
        print("  [error] too few samples")
        return

    # Split train/val (80/20 by clip)
    n_val = max(1, len(dataset) // 5)
    n_train = len(dataset) - n_val
    train_ds, val_ds = torch.utils.data.random_split(dataset, [n_train, n_val])

    train_loader = DataLoader(train_ds, batch_size=64, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=64, shuffle=False, num_workers=0)

    # Model
    model = ReleaseCNN(window=WINDOW, n_features=3).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=50)
    criterion = nn.SmoothL1Loss(beta=1.0)

    best_val_loss = float('inf')
    epochs = 50

    print(f"  Training {epochs} epochs...")
    for epoch in range(epochs):
        model.train()
        train_loss = 0
        for features, labels in train_loader:
            features, labels = features.to(DEVICE), labels.to(DEVICE)
            optimizer.zero_grad()
            pred = model(features)
            loss = criterion(pred, labels)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
        train_loss /= len(train_loader)

        model.eval()
        val_loss = 0
        val_mae = 0
        with torch.no_grad():
            for features, labels in val_loader:
                features, labels = features.to(DEVICE), labels.to(DEVICE)
                pred = model(features)
                loss = criterion(pred, labels)
                val_loss += loss.item()
                val_mae += torch.abs(pred - labels).mean().item()
        val_loss /= max(len(val_loader), 1)
        val_mae /= max(len(val_loader), 1)

        scheduler.step()

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1:3d}: train_loss={train_loss:.4f} val_loss={val_loss:.4f} val_MAE={val_mae:.2f} frames")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), MODEL_PATH)

    print(f"\n  Best val_loss: {best_val_loss:.4f}")
    print(f"  Model saved: {MODEL_PATH}")

    # Quick evaluation: prediction error at different horizons
    model.load_state_dict(torch.load(MODEL_PATH, weights_only=True))
    model.eval()
    print("\n  Prediction error by horizon:")
    with torch.no_grad():
        for h in [1, 3, 5, 10, 15, 20, 30]:
            h_mask = []
            h_errors = []
            for features, labels in val_loader:
                features, labels = features.to(DEVICE), labels.to(DEVICE)
                pred = model(features)
                mask = (labels >= h - 0.5) & (labels < h + 0.5)
                if mask.any():
                    h_errors.extend((pred[mask] - labels[mask]).cpu().numpy().tolist())
            if h_errors:
                errs = np.array(h_errors)
                mae = np.mean(np.abs(errs))
                iqr = np.percentile(errs, 75) - np.percentile(errs, 25)
                print(f"    h={h:2d} frames: n={len(errs):4d} MAE={mae:.2f} IQR={iqr:.2f} frames ({iqr*16.67:.0f}ms)")


def evaluate(video, count, start, handed, meter_color):
    """Evaluate the trained model on a clip."""
    if not os.path.exists(MODEL_PATH):
        print(f"  [error] model not found at {MODEL_PATH}. Train first.")
        return

    print(f"TEMPORAL RELEASE PREDICTOR — Evaluation")
    print(f"{'='*70}")

    model = ReleaseCNN(window=WINDOW, n_features=3).to(DEVICE)
    model.load_state_dict(torch.load(MODEL_PATH, weights_only=True))
    model.eval()

    print(f"  Extracting {os.path.basename(video)}...", end=" ", flush=True)
    traj, releases = extract_trajectories(video, count, start, handed, meter_color)
    if traj is None:
        print("FAILED")
        return
    print(f"{len(traj)} frames, {len(releases)} releases")

    fps = 60.0
    ms = 1000.0 / fps

    # For each release, predict at different horizons and measure error
    print(f"\n  Open-loop prediction error (predicting release BEFORE it happens):")
    print(f"  {'Horizon':>10} {'n':>5} {'MAE':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 55)

    for h in [1, 3, 5, 8, 10, 15, 20]:
        errors = []
        for rf in releases:
            t = rf - h  # predict h frames before release
            if t < WINDOW:
                continue
            y_seg = traj[t - WINDOW:t]
            vel = np.gradient(y_seg)
            acc = np.gradient(vel)
            features = np.stack([y_seg, vel, acc], axis=0)
            mean = features.mean(axis=1, keepdims=True)
            std = features.std(axis=1, keepdims=True) + 1e-8
            features = (features - mean) / std

            with torch.no_grad():
                pred = model(torch.from_numpy(features.astype(np.float32)).unsqueeze(0).to(DEVICE))
                pred_frames = pred.item()

            # Error: predicted frames-to-release vs actual (h)
            error_frames = pred_frames - h  # positive = predicted later than actual
            errors.append(error_frames * ms)

        if len(errors) >= 5:
            errs = np.array(errors)
            mae = np.mean(np.abs(errs))
            med = np.median(errs)
            iqr = np.percentile(errs, 75) - np.percentile(errs, 25)
            w40 = sum(1 for x in errors if abs(x - med) <= 40)
            w80 = sum(1 for x in errors if abs(x - med) <= 80)
            print(f"  {h:3d} frames  {len(errors):>5} {mae:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(errors)}':>8} {f'{w80}/{len(errors)}':>8}")

    # Also evaluate: closed-loop (predict at each frame, trigger when pred < 0.5)
    print(f"\n  Closed-loop simulation (trigger when predicted frames-to-release < 0.5):")
    detection_offsets = []
    for rf in releases:
        for t in range(max(WINDOW, rf - HORIZON), min(len(traj), rf + 5)):
            y_seg = traj[t - WINDOW:t]
            vel = np.gradient(y_seg)
            acc = np.gradient(vel)
            features = np.stack([y_seg, vel, acc], axis=0)
            mean = features.mean(axis=1, keepdims=True)
            std = features.std(axis=1, keepdims=True) + 1e-8
            features = (features - mean) / std

            with torch.no_grad():
                pred = model(torch.from_numpy(features.astype(np.float32)).unsqueeze(0).to(DEVICE))
                pred_frames = pred.item()

            if pred_frames < 0.5:
                detection_offsets.append((t - rf) * ms)
                break

    if len(detection_offsets) >= 5:
        arr = np.array(sorted(detection_offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in detection_offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in detection_offsets if abs(x - med) <= 80)
        print(f"  n={len(detection_offsets)} median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(detection_offsets)} ±80ms={w80}/{len(detection_offsets)}")
        if iqr < 80:
            print(f"  → SUB-80ms with temporal model! Open-loop prediction viable!")
        elif iqr < 72:
            print(f"  → Matches causal zero-crossing (72ms) — but with prediction capability!")
        else:
            print(f"  → Worse than causal zero-crossing — temporal model needs more data/training")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode")
    sub_train = sub.add_parser("train")
    sub_eval = sub.add_parser("eval")
    sub_eval.add_argument("video")
    sub_eval.add_argument("count", type=int, nargs="?", default=3000)
    sub_eval.add_argument("start", type=int, nargs="?", default=11940)
    sub_eval.add_argument("handed", nargs="?", default="Right")
    sub_eval.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    if args.mode == "train":
        train()
    elif args.mode == "eval":
        evaluate(args.video, args.count, args.start, args.handed, args.meter_color)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
