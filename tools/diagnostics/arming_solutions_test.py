#!/usr/bin/env python3
"""Self-validating zero-crossing detector — solves the ARMING problem.

Instead of needing a separate "shot is starting" signal to arm the crossing search,
this detector runs the crossing detector CONTINUOUSLY and only accepts a crossing
as a release if the TRAILING window carries the full shot signature:

1. GATHER: hip-Y rose (crouch) then fell (extend) — player dipped then exploded
2. SUSTAINED RISE: wrist-Y rose monotonically for 10+ frames before crossing
3. APEX HEIGHT: wrist reached significant height above rest

A dribble/pass has no gather+launch signature → crossing is rejected.
Fully causal — no early signal needed, validation looks backward only.

Also tests:
- Approach 2: Learned shot-start classifier (1D-CNN binary)
- Approach 3: Multi-joint motion-onset energy (coordinated launch burst)

Usage:
    C:\\Python314\\python.exe tools/diagnostics/self_validating_zerocross.py
"""
from __future__ import annotations

import os
import sys
import argparse

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
FPS = 60.0
MS_PER_FRAME = 1000.0 / FPS
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]
VIDEOS = r"C:\Users\Administrator\Videos"


def extract_all_trajectories(video, count, start, handed, meter_color):
    """Extract wrist-Y, hip-Y, knee-Y trajectories and meter shot edges."""
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

    wy_raw, hy_raw, ky_raw = [], [], []
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print(f"  Extracting {os.path.basename(video)}...", end=" ", flush=True)
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
        hy_raw.append(float(pose._hip_y[j]))
        ky_raw.append(float(pose._knee_y[j]))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()

    def fill(arr):
        a = np.array(arr, dtype=float)
        mask = ~np.isnan(a)
        if mask.sum() < 10:
            return None
        return np.interp(np.arange(len(a)), np.where(mask)[0], a[mask])

    wy = fill(wy_raw)
    hy = fill(hy_raw)
    ky = fill(ky_raw)
    if wy is None:
        print("FAILED")
        return None

    print(f"{len(wy)} frames, {len(shot_edges)} shots")
    return wy, hy, ky, shot_edges


def causal_ema(signal, tau_ms=20.0, dt_ms=16.67):
    alpha = 1.0 - np.exp(-dt_ms / max(tau_ms, 1.0))
    out = np.zeros_like(signal)
    out[0] = signal[0]
    for i in range(1, len(signal)):
        out[i] = alpha * signal[i] + (1 - alpha) * out[i - 1]
    return out


def find_all_crossings(wy_ema, vel):
    """Find all neg→pos velocity crossings in the entire trajectory."""
    crossings = []
    for i in range(len(vel) - 1):
        if vel[i] < 0 and vel[i + 1] >= 0:
            frac = -vel[i] / (vel[i + 1] - vel[i] + 1e-10)
            cross_frame = i + frac
            crossings.append(cross_frame)
    return crossings


# ─── APPROACH 1: Self-validating crossing ───────────────────────────────────

def validate_shot_signature(wy, hy, ky, cross_idx, lookback=45):
    """Check if the trailing window before a crossing has the shot signature.

    Returns (is_valid, confidence) where confidence is 0-1.
    """
    lo = max(0, int(cross_idx) - lookback)
    hi = int(cross_idx)
    if hi - lo < 15:
        return False, 0.0

    wy_seg = wy[lo:hi]
    hy_seg = hy[lo:hi] if hy is not None else np.zeros_like(wy_seg)
    ky_seg = ky[lo:hi] if ky is not None else np.zeros_like(wy_seg)

    # --- Check 1: Sustained wrist rise before crossing ---
    # The wrist should be rising (Y decreasing = moving up) for most of the window
    wy_diff = wy_seg[-1] - wy_seg[0]  # negative = moved up
    if wy_diff >= 0:
        return False, 0.0  # wrist didn't rise → not a shot

    # Sustained rise: at least 60% of frames should show upward motion
    wy_vel = np.diff(wy_seg)
    upward_frac = np.mean(wy_vel < 0)
    if upward_frac < 0.4:
        return False, 0.0

    # --- Check 2: Gather (hip crouch then extend) ---
    # Hip should dip down (Y increases = crouch) then rise (Y decreases = extend)
    gather_score = 0.0
    if hy is not None and len(hy_seg) > 10:
        hy_ema = causal_ema(hy_seg, tau_ms=50)
        hy_min = np.min(hy_ema)  # highest point
        hy_max = np.max(hy_ema)  # lowest point (crouch)
        hy_range = hy_max - hy_min
        if hy_range > 0.005:  # significant hip movement
            # Check if the crouch happens before the extend
            crouch_idx = np.argmax(hy_ema)  # lowest point (max Y = crouch)
            extend_idx = np.argmin(hy_ema)  # highest point (min Y = extend)
            if crouch_idx < extend_idx:
                gather_score = min(1.0, hy_range / 0.03)
            else:
                gather_score = 0.3 * min(1.0, hy_range / 0.03)  # some movement but wrong order

    # --- Check 3: Knee crouch (similar to hip) ---
    knee_score = 0.0
    if ky is not None and len(ky_seg) > 10:
        ky_ema = causal_ema(ky_seg, tau_ms=50)
        ky_range = np.max(ky_ema) - np.min(ky_ema)
        if ky_range > 0.005:
            knee_score = min(1.0, ky_range / 0.04)

    # --- Check 4: Wrist apex height above rest ---
    # The wrist should have reached a significant height (low Y) by the crossing
    rest_wy = np.median(wy[max(0, lo-60):lo]) if lo > 60 else np.median(wy[:lo+1])
    apex_height = rest_wy - np.min(wy_seg)  # positive = rose above rest
    height_score = min(1.0, apex_height / 0.05) if apex_height > 0 else 0.0

    # --- Check 5: Coordinated launch (hip + wrist moving together) ---
    coord_score = 0.0
    if hy is not None and len(hy_seg) > 10:
        hy_vel = np.diff(causal_ema(hy_seg, tau_ms=30))
        wy_vel_s = np.diff(causal_ema(wy_seg, tau_ms=30))
        # In the launch phase (last 15 frames before crossing), both should move up (negative vel)
        launch_hy = hy_vel[-15:] if len(hy_vel) >= 15 else hy_vel
        launch_wy = wy_vel_s[-15:] if len(wy_vel_s) >= 15 else wy_vel_s
        hy_up = np.mean(launch_hy < 0)
        wy_up = np.mean(launch_wy < 0)
        coord_score = hy_up * wy_up

    # Combined confidence
    confidence = (0.30 * height_score +
                  0.30 * gather_score +
                  0.15 * knee_score +
                  0.15 * coord_score +
                  0.10 * upward_frac)

    # Stricter threshold: need strong gather + significant height + coordinated launch
    is_valid = (height_score > 0.3 and
                gather_score > 0.2 and
                upward_frac > 0.5 and
                confidence > 0.35)

    return is_valid, confidence


def test_self_validating(wy, hy, ky, shot_edges, clip_name):
    """Test the self-validating crossing detector."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_all_crossings(wy_ema, vel)

    # For each crossing, validate the shot signature
    accepted = []
    rejected = []
    for c in all_crossings:
        valid, conf = validate_shot_signature(wy, hy, ky, c)
        if valid:
            accepted.append((c, conf))
        else:
            rejected.append((c, conf))

    # Match accepted crossings to shot edges — ONE crossing per edge (first after edge)
    tp = []
    fp = []
    matched_edges = set()
    matched_crossings = set()

    # Sort crossings by frame
    accepted_sorted = sorted(accepted, key=lambda x: x[0])

    # For each shot edge, find the FIRST accepted crossing within 60 frames after it
    for edge in shot_edges:
        best_cross = None
        best_conf = 0
        for c, conf in accepted_sorted:
            if c < edge:
                continue  # crossing before edge — skip
            if c > edge + 60:
                break  # too far after edge
            if c not in matched_crossings:
                if best_cross is None:
                    best_cross = c
                    best_conf = conf
                    break  # take FIRST crossing after edge
        if best_cross is not None:
            tp.append((best_cross, edge, best_conf))
            matched_edges.add(edge)
            matched_crossings.add(best_cross)

    # False positives: accepted crossings not matched to any edge
    for c, conf in accepted:
        if c not in matched_crossings:
            # Check if ANY shot edge is within 60 frames
            if shot_edges:
                nearest = min(shot_edges, key=lambda e: abs(c - e))
                if abs(c - nearest) > 60:
                    fp.append((c, conf))
            else:
                fp.append((c, conf))

    # Unmatched shot edges = MISSED shots
    missed = [e for e in shot_edges if e not in matched_edges]

    # Calculate timing offsets for TPs
    offsets = []
    for c, edge, conf in tp:
        offset_ms = (c - edge) * MS_PER_FRAME
        offsets.append(offset_ms)

    # Report
    arm_rate = len(matched_edges) / max(len(shot_edges), 1)
    false_arm_rate = len(fp) / max(len(accepted), 1)

    print(f"\n  {clip_name}: Self-validating crossing")
    print(f"    Total crossings: {len(all_crossings)}")
    print(f"    Accepted: {len(accepted)} | Rejected: {len(rejected)}")
    print(f"    TRUE POSITIVES: {len(tp)} | FALSE POSITIVES: {len(fp)}")
    print(f"    MISSED shots: {len(missed)}/{len(shot_edges)}")
    print(f"    ARM RATE: {arm_rate:.1%} | FALSE ARM RATE: {false_arm_rate:.1%}")

    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": len(fp),
                "n_tp": len(tp), "n_shots": len(shot_edges), "median": med, "iqr": iqr,
                "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no true positives to measure")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": len(fp),
                "n_tp": len(tp), "n_shots": len(shot_edges), "median": 0, "iqr": 9999,
                "w40": 0, "w80": 0, "offsets": []}


# ─── APPROACH 2: Learned shot-start classifier ──────────────────────────────

class ShotStartCNN(nn.Module):
    """Binary 1D-CNN: is a shot starting in this window?"""

    def __init__(self, window=30, n_features=6):
        super().__init__()
        self.conv1 = nn.Conv1d(n_features, 32, kernel_size=5, padding=2)
        self.bn1 = nn.BatchNorm1d(32)
        self.conv2 = nn.Conv1d(32, 64, kernel_size=5, padding=2)
        self.bn2 = nn.BatchNorm1d(64)
        self.conv3 = nn.Conv1d(64, 128, kernel_size=3, padding=1)
        self.bn3 = nn.BatchNorm1d(128)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc1 = nn.Linear(128, 32)
        self.fc2 = nn.Linear(32, 1)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.3)

    def forward(self, x):
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.relu(self.bn2(self.conv2(x)))
        x = self.relu(self.bn3(self.conv3(x)))
        x = self.gap(x).squeeze(-1)
        x = self.drop(x)
        x = self.relu(self.fc1(x))
        return self.fc2(x).squeeze(-1)


def build_multijoint_features(wy, hy, ky, t, window=30):
    """Build 6-feature window: wrist_y, hip_y, knee_y, wrist_vel, hip_vel, knee_vel."""
    if t < window:
        return None
    wy_s = wy[t-window:t]
    hy_s = hy[t-window:t] if hy is not None else np.zeros_like(wy_s)
    ky_s = ky[t-window:t] if ky is not None else np.zeros_like(wy_s)

    wy_v = np.gradient(wy_s)
    hy_v = np.gradient(hy_s)
    ky_v = np.gradient(ky_s)

    features = np.stack([wy_s, hy_s, ky_s, wy_v, hy_v, ky_v], axis=0)
    mean = features.mean(axis=1, keepdims=True)
    std = features.std(axis=1, keepdims=True) + 1e-8
    features = (features - mean) / std
    return features.astype(np.float32)


class ShotStartDataset(Dataset):
    """Binary dataset: shot-starting (1) vs not (0).

    Positive labels: frames 5-15 before each shot edge (150-250ms lead).
    Negative labels: random frames not near any shot edge.
    """

    def __init__(self, trajectories, shot_edges_list, window=30, positive_radius=15):
        self.samples = []
        for (wy, hy, ky), edges in zip(trajectories, shot_edges_list):
            if len(wy) < window + 1:
                continue
            edge_set = set()
            for e in edges:
                for dt in range(3, positive_radius):
                    t = e - dt
                    if t >= window and t < len(wy):
                        feat = build_multijoint_features(wy, hy, ky, t, window)
                        if feat is not None:
                            self.samples.append((feat, 1.0))
                        edge_set.add(t)

            # Negative samples: random frames not near any edge
            n_neg = len(edges) * 3
            for _ in range(n_neg):
                t = np.random.randint(window, len(wy))
                if t not in edge_set and all(abs(t - e) > positive_radius + 10 for e in edges):
                    feat = build_multijoint_features(wy, hy, ky, t, window)
                    if feat is not None:
                        self.samples.append((feat, 0.0))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        feat, label = self.samples[idx]
        return torch.from_numpy(feat), torch.tensor(label, dtype=torch.float32)


def train_shot_start_classifier(all_data):
    """Train the binary shot-start classifier."""
    print("\n  Training shot-start classifier...")
    trajectories = [(wy, hy, ky) for wy, hy, ky, _, _ in all_data]
    shot_edges_list = [edges for _, _, _, edges, _ in all_data]

    # Leave-one-out: train on all but last, validate on last
    train_traj = trajectories[:-1]
    train_edges = shot_edges_list[:-1]
    val_traj = [trajectories[-1]]
    val_edges = [shot_edges_list[-1]]

    train_ds = ShotStartDataset(train_traj, train_edges, window=30, positive_radius=15)
    val_ds = ShotStartDataset(val_traj, val_edges, window=30, positive_radius=15)

    print(f"    Train: {len(train_ds)} samples ({sum(1 for _,l in train_ds.samples if l > 0.5)} pos)")
    print(f"    Val: {len(val_ds)} samples ({sum(1 for _,l in val_ds.samples if l > 0.5)} pos)")

    if len(train_ds) < 50:
        print("    [error] too few samples")
        return None

    train_loader = DataLoader(train_ds, batch_size=64, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=64, shuffle=False, num_workers=0)

    model = ShotStartCNN(window=30, n_features=6).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=5e-4, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=50)
    pos_weight = torch.tensor([3.0]).to(DEVICE)  # weight positive class more
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    best_val_loss = float('inf')
    model_path = os.path.join(ROOT, "models", "shot_start_classifier.pt")

    for epoch in range(50):
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
        val_acc = 0
        with torch.no_grad():
            for features, labels in val_loader:
                features, labels = features.to(DEVICE), labels.to(DEVICE)
                pred = model(features)
                val_loss += criterion(pred, labels).item()
                val_acc += ((pred > 0).float() == labels).float().mean().item()
        val_loss /= max(len(val_loader), 1)
        val_acc /= max(len(val_loader), 1)
        scheduler.step()

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1:3d}: train_loss={train_loss:.4f} val_loss={val_loss:.4f} val_acc={val_acc:.1%}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), model_path)

    print(f"    Best val_loss: {best_val_loss:.4f}, saved to {model_path}")
    return model_path


def test_learned_classifier(model_path, wy, hy, ky, shot_edges, clip_name):
    """Test the learned classifier as an arming signal."""
    if model_path is None or not os.path.exists(model_path):
        print(f"\n  {clip_name}: Learned classifier — SKIP (no model)")
        return None

    model = ShotStartCNN(window=30, n_features=6).to(DEVICE)
    model.load_state_dict(torch.load(model_path, weights_only=True))
    model.eval()

    # Run classifier on every frame
    arm_scores = np.zeros(len(wy))
    for t in range(30, len(wy)):
        feat = build_multijoint_features(wy, hy, ky, t, 30)
        if feat is None:
            continue
        with torch.no_grad():
            score = torch.sigmoid(model(torch.from_numpy(feat).unsqueeze(0).to(DEVICE))).item()
        arm_scores[t] = score

    # Find arm frames: score > 0.5, with debounce
    arm_frames = []
    last_arm = -100
    for t in range(len(arm_scores)):
        if arm_scores[t] > 0.5 and (t - last_arm) > 30:
            arm_frames.append(t)
            last_arm = t

    # For each arm, find the first zero-crossing within 60 frames
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    offsets = []
    matched_edges = set()
    false_arms = 0

    for arm_t in arm_frames:
        # Search for first neg→pos crossing in [arm_t, arm_t+60]
        found_cross = None
        for i in range(arm_t, min(arm_t + 60, len(vel) - 1)):
            if vel[i] < 0 and vel[i + 1] >= 0:
                frac = -vel[i] / (vel[i + 1] - vel[i] + 1e-10)
                found_cross = i + frac
                break

        if found_cross is None:
            continue

        # Match to nearest shot edge
        if not shot_edges:
            false_arms += 1
            continue
        nearest_edge = min(shot_edges, key=lambda e: abs(found_cross - e))
        dist = abs(found_cross - nearest_edge)
        if dist < 60 and nearest_edge not in matched_edges:
            offsets.append((found_cross - nearest_edge) * MS_PER_FRAME)
            matched_edges.add(nearest_edge)
        elif dist >= 60:
            false_arms += 1

    arm_rate = len(matched_edges) / max(len(shot_edges), 1)

    print(f"\n  {clip_name}: Learned classifier arming")
    print(f"    Arm frames: {len(arm_frames)} | False arms: {false_arms}")
    print(f"    ARM RATE: {arm_rate:.1%}")

    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": len(offsets), "n_shots": len(shot_edges), "median": med, "iqr": iqr,
                "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no true positives")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": 0, "n_shots": len(shot_edges), "median": 0, "iqr": 9999,
                "w40": 0, "w80": 0, "offsets": []}


# ─── APPROACH 3: Multi-joint motion-onset energy ────────────────────────────

def test_motion_onset_arming(wy, hy, ky, shot_edges, clip_name):
    """Detect the coordinated launch burst (legs+arms+torso moving up together)."""
    # Compute EMA-smoothed velocities for all joints
    wy_ema = causal_ema(wy, tau_ms=30)
    hy_ema = causal_ema(hy, tau_ms=30) if hy is not None else np.zeros_like(wy)
    ky_ema = causal_ema(ky, tau_ms=30) if ky is not None else np.zeros_like(wy)

    wy_vel = np.zeros_like(wy_ema)
    wy_vel[1:] = wy_ema[1:] - wy_ema[:-1]
    hy_vel = np.zeros_like(hy_ema)
    hy_vel[1:] = hy_ema[1:] - hy_ema[:-1]
    ky_vel = np.zeros_like(ky_ema)
    ky_vel[1:] = ky_ema[1:] - ky_ema[:-1]

    # Motion-onset energy: product of upward velocities (all joints moving up = negative vel)
    # Use a windowed sum of the coordinated upward motion
    window = 10
    energy = np.zeros(len(wy))
    for t in range(window, len(wy)):
        # All three should be negative (upward) simultaneously
        wy_up = wy_vel[t-window:t] < -0.0005
        hy_up = hy_vel[t-window:t] < -0.0003
        ky_up = ky_vel[t-window:t] < -0.0005
        coord = wy_up & hy_up  # wrist + hip coordinated (knee optional)
        energy[t] = np.sum(coord) / window

    # Detect arm frames: energy > 0.5 (50% of window has coordinated upward motion)
    arm_frames = []
    last_arm = -100
    for t in range(len(energy)):
        if energy[t] > 0.5 and (t - last_arm) > 25:
            arm_frames.append(t)
            last_arm = t

    # For each arm, find first zero-crossing within 60 frames
    wy_ema20 = causal_ema(wy, tau_ms=20)
    vel20 = np.zeros_like(wy_ema20)
    vel20[1:] = wy_ema20[1:] - wy_ema20[:-1]

    offsets = []
    matched_edges = set()
    false_arms = 0

    for arm_t in arm_frames:
        found_cross = None
        for i in range(arm_t, min(arm_t + 60, len(vel20) - 1)):
            if vel20[i] < 0 and vel20[i + 1] >= 0:
                frac = -vel20[i] / (vel20[i + 1] - vel20[i] + 1e-10)
                found_cross = i + frac
                break

        if found_cross is None:
            continue

        if not shot_edges:
            false_arms += 1
            continue
        nearest_edge = min(shot_edges, key=lambda e: abs(found_cross - e))
        dist = abs(found_cross - nearest_edge)
        if dist < 60 and nearest_edge not in matched_edges:
            offsets.append((found_cross - nearest_edge) * MS_PER_FRAME)
            matched_edges.add(nearest_edge)
        elif dist >= 60:
            false_arms += 1

    arm_rate = len(matched_edges) / max(len(shot_edges), 1)

    print(f"\n  {clip_name}: Motion-onset energy arming")
    print(f"    Arm frames: {len(arm_frames)} | False arms: {false_arms}")
    print(f"    ARM RATE: {arm_rate:.1%}")

    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": len(offsets), "n_shots": len(shot_edges), "median": med, "iqr": iqr,
                "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no true positives")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": 0, "n_shots": len(shot_edges), "median": 0, "iqr": 9999,
                "w40": 0, "w80": 0, "offsets": []}


# ─── APPROACH 4: Gather-anchored arming ─────────────────────────────────────

def test_gather_anchored_arming(wy, hy, ky, shot_edges, clip_name):
    """Use the hip crouch→extend transition as the arming signal."""
    if hy is None:
        print(f"\n  {clip_name}: Gather-anchored — SKIP (no hip data)")
        return None

    hy_ema = causal_ema(hy, tau_ms=50)
    hy_vel = np.zeros_like(hy_ema)
    hy_vel[1:] = hy_ema[1:] - hy_ema[:-1]

    # Find gather events: hip goes from rising (crouch, vel>0) to falling (extend, vel<0)
    # i.e., hip velocity crosses from positive to negative
    arm_frames = []
    last_arm = -100
    for t in range(len(hy_vel) - 1):
        if hy_vel[t] > 0.0003 and hy_vel[t + 1] <= 0.0003:
            # Hip was going down (crouch) and now going up (extend) — gather complete
            # Only arm if there's significant hip movement
            lookback = min(20, t)
            hip_range = np.max(hy_ema[t-lookback:t+1]) - np.min(hy_ema[t-lookback:t+1])
            if hip_range > 0.008 and (t - last_arm) > 25:
                arm_frames.append(t)
                last_arm = t

    # For each arm, find first zero-crossing within 80 frames (wider window for gather)
    wy_ema20 = causal_ema(wy, tau_ms=20)
    vel20 = np.zeros_like(wy_ema20)
    vel20[1:] = wy_ema20[1:] - wy_ema20[:-1]

    offsets = []
    matched_edges = set()
    false_arms = 0

    for arm_t in arm_frames:
        found_cross = None
        for i in range(arm_t, min(arm_t + 80, len(vel20) - 1)):
            if vel20[i] < 0 and vel20[i + 1] >= 0:
                frac = -vel20[i] / (vel20[i + 1] - vel20[i] + 1e-10)
                found_cross = i + frac
                break

        if found_cross is None:
            continue

        if not shot_edges:
            false_arms += 1
            continue
        nearest_edge = min(shot_edges, key=lambda e: abs(found_cross - e))
        dist = abs(found_cross - nearest_edge)
        if dist < 60 and nearest_edge not in matched_edges:
            offsets.append((found_cross - nearest_edge) * MS_PER_FRAME)
            matched_edges.add(nearest_edge)
        elif dist >= 60:
            false_arms += 1

    arm_rate = len(matched_edges) / max(len(shot_edges), 1)

    print(f"\n  {clip_name}: Gather-anchored arming")
    print(f"    Arm frames: {len(arm_frames)} | False arms: {false_arms}")
    print(f"    ARM RATE: {arm_rate:.1%}")

    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": len(offsets), "n_shots": len(shot_edges), "median": med, "iqr": iqr,
                "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no true positives")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": false_arms,
                "n_tp": 0, "n_shots": len(shot_edges), "median": 0, "iqr": 9999,
                "w40": 0, "w80": 0, "offsets": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip_train", action="store_true", help="Skip training the classifier")
    args = ap.parse_args()

    print(f"ARMING SOLUTION TESTS — Solving the shot-start detection problem")
    print(f"Device: {DEVICE}")
    print(f"{'='*80}")

    # Extract all data first
    all_data = []
    for clip_name, count, start, handed, mcolor, label in CLIPS:
        video = os.path.join(VIDEOS, clip_name)
        if not os.path.exists(video):
            print(f"  [skip] {clip_name} not found")
            continue
        result = extract_all_trajectories(video, count, start, handed, mcolor)
        if result is not None:
            all_data.append((*result, label))

    if not all_data:
        print("  [error] no data extracted")
        return

    # Run all approaches
    print(f"\n{'='*80}")
    print("APPROACH 1: Self-validating crossing (no arming needed)")
    print(f"{'='*80}")
    sv_results = []
    for wy, hy, ky, edges, label in all_data:
        r = test_self_validating(wy, hy, ky, edges, label)
        if r:
            sv_results.append(r)

    print(f"\n{'='*80}")
    print("APPROACH 2: Learned shot-start classifier (1D-CNN)")
    print(f"{'='*80}")
    model_path = None
    if not args.skip_train:
        model_path = train_shot_start_classifier(all_data)
    lc_results = []
    for wy, hy, ky, edges, label in all_data:
        r = test_learned_classifier(model_path, wy, hy, ky, edges, label)
        if r:
            lc_results.append(r)

    print(f"\n{'='*80}")
    print("APPROACH 3: Multi-joint motion-onset energy")
    print(f"{'='*80}")
    mo_results = []
    for wy, hy, ky, edges, label in all_data:
        r = test_motion_onset_arming(wy, hy, ky, edges, label)
        if r:
            mo_results.append(r)

    print(f"\n{'='*80}")
    print("APPROACH 4: Gather-anchored arming (hip crouch→extend)")
    print(f"{'='*80}")
    ga_results = []
    for wy, hy, ky, edges, label in all_data:
        r = test_gather_anchored_arming(wy, hy, ky, edges, label)
        if r:
            ga_results.append(r)

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY — All approaches compared")
    print(f"{'='*80}")
    print(f"\n  {'Approach':<35} {'Arm%':>6} {'False':>6} {'IQR':>8} {'±80ms':>8}")
    print("  " + "-" * 70)

    for name, results in [("Self-validating crossing", sv_results),
                           ("Learned classifier", lc_results),
                           ("Motion-onset energy", mo_results),
                           ("Gather-anchored", ga_results)]:
        if not results:
            print(f"  {name:<35} {'N/A':>6} {'N/A':>6} {'N/A':>8} {'N/A':>8}")
            continue
        avg_arm = np.mean([r["arm_rate"] for r in results])
        total_false = sum(r["false_arms"] for r in results)
        all_offsets = []
        for r in results:
            all_offsets.extend(r["offsets"])
        if all_offsets:
            arr = np.array(sorted(all_offsets))
            med = np.median(arr)
            iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
            w80 = sum(1 for x in all_offsets if abs(x - med) <= 80)
            print(f"  {name:<35} {avg_arm:>5.0%} {total_false:>6} {iqr:>7.1f}ms {f'{w80}/{len(all_offsets)}':>8}")
        else:
            print(f"  {name:<35} {avg_arm:>5.0%} {total_false:>6} {'N/A':>8} {'N/A':>8}")

    # Best approach
    print(f"\n  BEST APPROACH for Claude's engine:")
    best_approach = None
    best_score = -1
    for name, results in [("Self-validating crossing", sv_results),
                           ("Learned classifier", lc_results),
                           ("Motion-onset energy", mo_results),
                           ("Gather-anchored", ga_results)]:
        if not results:
            continue
        avg_arm = np.mean([r["arm_rate"] for r in results])
        total_false = sum(r["false_arms"] for r in results)
        all_offsets = []
        for r in results:
            all_offsets.extend(r["offsets"])
        if all_offsets:
            arr = np.array(sorted(all_offsets))
            iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
            # Score: high arm rate, low false arms, low IQR
            score = avg_arm * 100 - total_false * 5 - iqr
            if score > best_score:
                best_score = score
                best_approach = name

    if best_approach:
        print(f"  → {best_approach} (score={best_score:.1f})")


if __name__ == "__main__":
    main()
