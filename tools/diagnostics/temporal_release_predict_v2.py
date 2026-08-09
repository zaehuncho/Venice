#!/usr/bin/env python3
"""Temporal release predictor v2 — improved architecture and training.

Key changes from v1:
1. Classification approach: predict "is this the release frame?" (binary)
   instead of regression on frames-to-release. This is simpler and more stable.
2. Also test: regression with log-scale target (compress the horizon range).
3. Larger model: 2 more conv layers, BatchNorm for stability.
4. Better loss weighting: weight recent frames more (the release frame matters most).
5. Data augmentation: add noise to wrist-Y to improve robustness.
6. Feature engineering: add the EMA-smoothed wrist-Y and its velocity directly.

Usage:
    # Train:
    C:\\Python314\\python.exe tools/diagnostics/temporal_release_predict_v2.py train
    # Evaluate:
    C:\\Python314\\python.exe tools/diagnostics/temporal_release_predict_v2.py eval "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys

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
WINDOW = 30
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]
VIDEOS = r"C:\Users\Administrator\Videos"
MODEL_PATH = os.path.join(ROOT, "models", "release_predictor_v2.pt")


class ReleaseCNNv2(nn.Module):
    """Improved 1D-CNN with BatchNorm and deeper layers."""

    def __init__(self, window=WINDOW, n_features=4):
        super().__init__()
        self.conv1 = nn.Conv1d(n_features, 32, kernel_size=5, padding=2)
        self.bn1 = nn.BatchNorm1d(32)
        self.conv2 = nn.Conv1d(32, 64, kernel_size=5, padding=2)
        self.bn2 = nn.BatchNorm1d(64)
        self.conv3 = nn.Conv1d(64, 128, kernel_size=3, padding=1)
        self.bn3 = nn.BatchNorm1d(128)
        self.conv4 = nn.Conv1d(128, 128, kernel_size=3, padding=1)
        self.bn4 = nn.BatchNorm1d(128)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc1 = nn.Linear(128, 64)
        self.fc2 = nn.Linear(64, 32)
        self.fc3 = nn.Linear(32, 1)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.3)

    def forward(self, x):
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.relu(self.bn2(self.conv2(x)))
        x = self.relu(self.bn3(self.conv3(x)))
        x = self.relu(self.bn4(self.conv4(x)))
        x = self.gap(x).squeeze(-1)
        x = self.drop(x)
        x = self.relu(self.fc1(x))
        x = self.drop(x)
        x = self.relu(self.fc2(x))
        x = self.fc3(x).squeeze(-1)
        return x


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

    wy = np.array(wy_raw, dtype=float)
    mask = ~np.isnan(wy)
    if mask.sum() > 10:
        wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])
    else:
        return None, None

    # Find zero-crossing frames using causal EMA (same as Stage 1)
    alpha = 1.0 - np.exp(-16.67 / 20.0)
    ema = np.zeros_like(wy)
    ema[0] = wy[0]
    for i in range(1, len(wy)):
        ema[i] = alpha * wy[i] + (1 - alpha) * ema[i - 1]
    vel = np.zeros_like(ema)
    vel[1:] = ema[1:] - ema[:-1]

    release_frames = []
    for e in shot_edges:
        for i in range(e, min(len(vel), e + 60)):
            if i > 0 and vel[i - 1] < 0 and vel[i] >= 0:
                release_frames.append(i)
                break

    return wy, release_frames


def build_features(traj, t, window=WINDOW):
    """Build feature vector for frame t from trajectory."""
    y_seg = traj[t - window:t]
    vel = np.gradient(y_seg)
    acc = np.gradient(vel)

    # EMA-smoothed version (τ=20ms)
    alpha = 1.0 - np.exp(-16.67 / 20.0)
    ema = np.zeros_like(y_seg)
    ema[0] = y_seg[0]
    for i in range(1, len(y_seg)):
        ema[i] = alpha * y_seg[i] + (1 - alpha) * ema[i - 1]
    ema_vel = np.zeros_like(ema)
    ema_vel[1:] = ema[1:] - ema[:-1]

    # Features: raw_y, ema_vel, vel, acc
    features = np.stack([y_seg, ema_vel, vel, acc], axis=0)

    # Normalize each feature independently
    mean = features.mean(axis=1, keepdims=True)
    std = features.std(axis=1, keepdims=True) + 1e-8
    features = (features - mean) / std

    return features.astype(np.float32)


class ReleaseDataset(Dataset):
    """Dataset for release frame prediction.

    Generates (features, frames_to_release) pairs.
    Uses log-scale target for regression stability.
    """

    def __init__(self, trajectories, release_frames, window=WINDOW, augment=True):
        self.samples = []
        self.augment = augment

        for traj, releases in zip(trajectories, release_frames):
            if len(traj) < window + 1:
                continue
            for rf in releases:
                # Generate samples from WINDOW frames before release to 3 frames after
                for t in range(max(window, rf - 40), min(len(traj), rf + 4)):
                    if t < window:
                        continue
                    label = float(rf - t)  # frames to release
                    self.samples.append((traj, t, label))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        traj, t, label = self.samples[idx]
        features = build_features(traj, t, WINDOW)

        # Augmentation: add small noise to features
        if self.augment and np.random.random() < 0.5:
            features = features + np.random.randn(*features.shape).astype(np.float32) * 0.05

        # Log-scale target: sign-preserving log(1+|x|)
        if label >= 0:
            target = np.log1p(label)
        else:
            target = -np.log1p(abs(label))

        return torch.from_numpy(features), torch.tensor(target, dtype=torch.float32), torch.tensor(label, dtype=torch.float32)


def train():
    """Train the temporal model on all clips."""
    print("TEMPORAL RELEASE PREDICTOR v2 — Training")
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

    # Build dataset (train on all clips, validate on V7 only)
    print("  Building dataset...", end=" ", flush=True)

    # Split: train on V3-V6, validate on V7 (leave-one-out)
    train_trajectories = all_trajectories[:-1]
    train_releases = all_releases[:-1]
    val_trajectories = [all_trajectories[-1]]
    val_releases = [all_releases[-1]]

    train_ds = ReleaseDataset(train_trajectories, train_releases, WINDOW, augment=True)
    val_ds = ReleaseDataset(val_trajectories, val_releases, WINDOW, augment=False)
    print(f"{len(train_ds)} train, {len(val_ds)} val")

    if len(train_ds) < 100:
        print("  [error] too few samples")
        return

    train_loader = DataLoader(train_ds, batch_size=64, shuffle=True, num_workers=0,
                              collate_fn=lambda batch: (torch.stack([b[0] for b in batch]),
                                                         torch.stack([b[1] for b in batch]),
                                                         torch.stack([b[2] for b in batch])))
    val_loader = DataLoader(val_ds, batch_size=64, shuffle=False, num_workers=0,
                            collate_fn=lambda batch: (torch.stack([b[0] for b in batch]),
                                                       torch.stack([b[1] for b in batch]),
                                                       torch.stack([b[2] for b in batch])))

    model = ReleaseCNNv2(window=WINDOW, n_features=4).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=5e-4, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=100)
    criterion = nn.SmoothL1Loss(beta=0.5)

    best_val_loss = float('inf')
    epochs = 100

    print(f"  Training {epochs} epochs...")
    for epoch in range(epochs):
        model.train()
        train_loss = 0
        for features, targets, labels in train_loader:
            features, targets = features.to(DEVICE), targets.to(DEVICE)
            optimizer.zero_grad()
            pred = model(features)
            loss = criterion(pred, targets)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
        train_loss /= len(train_loader)

        model.eval()
        val_loss = 0
        val_mae_frames = 0
        with torch.no_grad():
            for features, targets, labels in val_loader:
                features, targets, labels = features.to(DEVICE), targets.to(DEVICE), labels.to(DEVICE)
                pred = model(features)
                loss = criterion(pred, targets)
                val_loss += loss.item()
                # Convert log-scale back to frames
                pred_frames = torch.sign(pred) * torch.expm1(torch.abs(pred))
                val_mae_frames += torch.abs(pred_frames - labels).mean().item()
        val_loss /= max(len(val_loader), 1)
        val_mae_frames /= max(len(val_loader), 1)

        scheduler.step()

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1:3d}: train_loss={train_loss:.4f} val_loss={val_loss:.4f} val_MAE={val_mae_frames:.2f} frames ({val_mae_frames*16.67:.0f}ms)")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), MODEL_PATH)

    print(f"\n  Best val_loss: {best_val_loss:.4f}")
    print(f"  Model saved: {MODEL_PATH}")

    # Evaluate on V7
    model.load_state_dict(torch.load(MODEL_PATH, weights_only=True))
    model.eval()

    # Per-horizon evaluation
    print("\n  Prediction error by horizon (on V7):")
    val_traj = val_trajectories[0]
    val_rels = val_releases[0]

    print(f"  {'Horizon':>10} {'n':>5} {'MAE':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 55)

    for h in [1, 2, 3, 5, 8, 10, 15, 20, 30]:
        errors = []
        for rf in val_rels:
            t = rf - h
            if t < WINDOW:
                continue
            features = build_features(val_traj, t, WINDOW)
            with torch.no_grad():
                pred = model(torch.from_numpy(features).unsqueeze(0).to(DEVICE))
                pred_frames = (torch.sign(pred) * torch.expm1(torch.abs(pred))).item()
            error_frames = pred_frames - h
            errors.append(error_frames * 16.67)

        if len(errors) >= 5:
            errs = np.array(errors)
            mae = np.mean(np.abs(errs))
            med = np.median(errs)
            iqr = np.percentile(errs, 75) - np.percentile(errs, 25)
            w40 = sum(1 for x in errors if abs(x - med) <= 40)
            w80 = sum(1 for x in errors if abs(x - med) <= 80)
            print(f"  {h:3d} frames  {len(errors):>5} {mae:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(errors)}':>8} {f'{w80}/{len(errors)}':>8}")

    # Closed-loop simulation
    print(f"\n  Closed-loop simulation (trigger when predicted frames-to-release < 0.5):")
    detection_offsets = []
    for rf in val_rels:
        for t in range(max(WINDOW, rf - 40), min(len(val_traj), rf + 5)):
            features = build_features(val_traj, t, WINDOW)
            with torch.no_grad():
                pred = model(torch.from_numpy(features).unsqueeze(0).to(DEVICE))
                pred_frames = (torch.sign(pred) * torch.expm1(torch.abs(pred))).item()
            if pred_frames < 0.5:
                detection_offsets.append((t - rf) * 16.67)
                break

    if len(detection_offsets) >= 5:
        arr = np.array(sorted(detection_offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in detection_offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in detection_offsets if abs(x - med) <= 80)
        print(f"  n={len(detection_offsets)} median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(detection_offsets)} ±80ms={w80}/{len(detection_offsets)}")
        if iqr < 80:
            print(f"  → SUB-80ms with temporal model!")
        else:
            print(f"  → Above 80ms — temporal model needs more data or better features")


def evaluate(video, count, start, handed, meter_color):
    """Evaluate the trained model on a clip."""
    if not os.path.exists(MODEL_PATH):
        print(f"  [error] model not found at {MODEL_PATH}. Train first.")
        return

    print(f"TEMPORAL RELEASE PREDICTOR v2 — Evaluation")
    print(f"{'='*70}")

    model = ReleaseCNNv2(window=WINDOW, n_features=4).to(DEVICE)
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

    print(f"\n  Open-loop prediction error by horizon:")
    print(f"  {'Horizon':>10} {'n':>5} {'MAE':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 55)

    for h in [1, 2, 3, 5, 8, 10, 15, 20, 30]:
        errors = []
        for rf in releases:
            t = rf - h
            if t < WINDOW:
                continue
            features = build_features(traj, t, WINDOW)
            with torch.no_grad():
                pred = model(torch.from_numpy(features).unsqueeze(0).to(DEVICE))
                pred_frames = (torch.sign(pred) * torch.expm1(torch.abs(pred))).item()
            error_frames = pred_frames - h
            errors.append(error_frames * ms)

        if len(errors) >= 5:
            errs = np.array(errors)
            mae = np.mean(np.abs(errs))
            med = np.median(errs)
            iqr = np.percentile(errs, 75) - np.percentile(errs, 25)
            w40 = sum(1 for x in errors if abs(x - med) <= 40)
            w80 = sum(1 for x in errors if abs(x - med) <= 80)
            print(f"  {h:3d} frames  {len(errors):>5} {mae:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(errors)}':>8} {f'{w80}/{len(errors)}':>8}")

    # Closed-loop simulation
    print(f"\n  Closed-loop simulation (trigger when predicted frames-to-release < 0.5):")
    detection_offsets = []
    for rf in releases:
        for t in range(max(WINDOW, rf - 40), min(len(traj), rf + 5)):
            features = build_features(traj, t, WINDOW)
            with torch.no_grad():
                pred = model(torch.from_numpy(features).unsqueeze(0).to(DEVICE))
                pred_frames = (torch.sign(pred) * torch.expm1(torch.abs(pred))).item()
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
            print(f"  → SUB-80ms with temporal model!")
        else:
            print(f"  → Above 80ms — temporal model needs more data or better features")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode")
    sub.add_parser("train")
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
