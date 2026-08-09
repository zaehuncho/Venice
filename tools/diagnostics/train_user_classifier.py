#!/usr/bin/env python3
"""Train a user-player classifier to improve lock robustness.

Takes player crops labeled by meter proximity (user vs other) and trains a small CNN
to discriminate the user player from others. Uses leave-one-clip-out cross-validation.

The classifier is integrated into pose_timing.py as an additional lock signal:
when the box tracker needs to re-acquire, it picks the player with the highest
classifier score instead of just the largest/nearest box.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/train_user_classifier.py
"""
from __future__ import annotations

import os, sys, argparse, json, random
from collections import defaultdict

import numpy as np
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DATASET_DIR = os.path.join(ROOT, "tools", "diagnostics", "user_player_dataset")
MODEL_PATH = os.path.join(ROOT, "models", "user_player_classifier.pt")

CROP_SIZE = (96, 96)  # (H, W)
BATCH_SIZE = 64
EPOCHS = 20
LR = 1e-3
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class UserPlayerCNN(nn.Module):
    """Small CNN for user-player classification."""

    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(128, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(), nn.AdaptiveAvgPool2d(1),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(64, 1),
        )

    def forward(self, x):
        x = self.features(x)
        return self.classifier(x).squeeze(-1)


class PlayerCropDataset(Dataset):
    def __init__(self, crops, labels, augment=False):
        self.crops = crops
        self.labels = labels
        self.augment = augment

    def __len__(self):
        return len(self.crops)

    def __getitem__(self, idx):
        crop = self.crops[idx]
        label = self.labels[idx]

        if self.augment:
            # Random horizontal flip
            if random.random() < 0.5:
                crop = cv2.flip(crop, 1)
            # Random brightness jitter
            if random.random() < 0.3:
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                hsv[:, :, 2] = np.clip(hsv[:, :, 2].astype(int) + random.randint(-20, 20), 0, 255).astype(np.uint8)
                crop = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

        # Convert to tensor
        crop = cv2.resize(crop, (CROP_SIZE[1], CROP_SIZE[0]))
        crop = crop[:, :, ::-1].copy()  # BGR -> RGB (copy to fix negative strides)
        tensor = torch.from_numpy(crop).float().permute(2, 0, 1) / 255.0
        return tensor, torch.tensor(label, dtype=torch.float32)


def load_dataset(max_other_per_clip=500):
    """Load crops from disk, grouped by clip."""
    user_dir = os.path.join(DATASET_DIR, "user")
    other_dir = os.path.join(DATASET_DIR, "other")

    if not os.path.exists(user_dir) or not os.path.exists(other_dir):
        print("Dataset not found. Run extract_user_player_dataset.py first.")
        sys.exit(1)

    # Group by clip (filename prefix: V3_f00000_p0_user.png -> V3)
    by_clip = defaultdict(lambda: {"user": [], "other": []})

    for fname in os.listdir(user_dir):
        clip = fname.split("_")[0]
        img = cv2.imread(os.path.join(user_dir, fname))
        if img is not None:
            by_clip[clip]["user"].append(img)

    for fname in os.listdir(other_dir):
        clip = fname.split("_")[0]
        img = cv2.imread(os.path.join(other_dir, fname))
        if img is not None:
            by_clip[clip]["other"].append(img)

    # Subsample "other" to balance
    for clip in by_clip:
        others = by_clip[clip]["other"]
        random.shuffle(others)
        by_clip[clip]["other"] = others[:max_other_per_clip]

    return by_clip


def train_and_evaluate(by_clip, test_clip):
    """Leave-one-clip-out: train on all clips except test_clip, evaluate on test_clip."""
    train_crops = []
    train_labels = []
    test_crops = []
    test_labels = []

    for clip, data in by_clip.items():
        if clip == test_clip:
            test_crops.extend(data["user"])
            test_labels.extend([1] * len(data["user"]))
            test_crops.extend(data["other"])
            test_labels.extend([0] * len(data["other"]))
        else:
            train_crops.extend(data["user"])
            train_labels.extend([1] * len(data["user"]))
            train_crops.extend(data["other"])
            train_labels.extend([0] * len(data["other"]))

    # Class weights for imbalanced training
    n_user = sum(train_labels)
    n_other = len(train_labels) - n_user
    pos_weight = torch.tensor([n_other / max(n_user, 1)], device=DEVICE)

    train_ds = PlayerCropDataset(train_crops, train_labels, augment=True)
    test_ds = PlayerCropDataset(test_crops, test_labels, augment=False)
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, num_workers=0)
    test_loader = DataLoader(test_ds, batch_size=BATCH_SIZE, shuffle=False, num_workers=0)

    model = UserPlayerCNN().to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-4)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    best_acc = 0
    for epoch in range(EPOCHS):
        model.train()
        total_loss = 0
        for batch_x, batch_y in train_loader:
            batch_x, batch_y = batch_x.to(DEVICE), batch_y.to(DEVICE)
            optimizer.zero_grad()
            logits = model(batch_x)
            loss = criterion(logits, batch_y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        # Evaluate
        model.eval()
        correct = 0
        total = 0
        user_correct = 0
        user_total = 0
        other_correct = 0
        other_total = 0
        with torch.no_grad():
            for batch_x, batch_y in test_loader:
                batch_x, batch_y = batch_x.to(DEVICE), batch_y.to(DEVICE)
                logits = model(batch_x)
                preds = (torch.sigmoid(logits) > 0.5).float()
                correct += (preds == batch_y).sum().item()
                total += len(batch_y)
                user_mask = batch_y == 1
                user_correct += (preds[user_mask] == 1).sum().item()
                user_total += user_mask.sum().item()
                other_mask = batch_y == 0
                other_correct += (preds[other_mask] == 0).sum().item()
                other_total += other_mask.sum().item()

        acc = correct / total
        user_acc = user_correct / max(user_total, 1)
        other_acc = other_correct / max(other_total, 1)
        if acc > best_acc:
            best_acc = acc
        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{EPOCHS}: loss={total_loss/len(train_loader):.3f} "
                  f"acc={acc:.1%} user_acc={user_acc:.1%} other_acc={other_acc:.1%}")

    # Final evaluation
    model.eval()
    all_probs = []
    all_labels = []
    with torch.no_grad():
        for batch_x, batch_y in test_loader:
            batch_x = batch_x.to(DEVICE)
            logits = model(batch_x)
            probs = torch.sigmoid(logits).cpu().numpy()
            all_probs.extend(probs.tolist())
            all_labels.extend(batch_y.numpy().tolist())

    all_probs = np.array(all_probs)
    all_labels = np.array(all_labels)

    # Metrics at various thresholds
    user_probs = all_probs[all_labels == 1]
    other_probs = all_probs[all_labels == 0]

    print(f"\n  {test_clip}: best_acc={best_acc:.1%}")
    print(f"    User score: mean={user_probs.mean():.3f} median={np.median(user_probs):.3f} "
          f"min={user_probs.min():.3f} max={user_probs.max():.3f}")
    print(f"    Other score: mean={other_probs.mean():.3f} median={np.median(other_probs):.3f} "
          f"min={other_probs.min():.3f} max={other_probs.max():.3f}")

    # At threshold 0.5
    tp = ((all_probs > 0.5) & (all_labels == 1)).sum()
    fp = ((all_probs > 0.5) & (all_labels == 0)).sum()
    fn = ((all_probs <= 0.5) & (all_labels == 1)).sum()
    tn = ((all_probs <= 0.5) & (all_labels == 0)).sum()
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-6)
    print(f"    @0.5: precision={precision:.1%} recall={recall:.1%} F1={f1:.3f} "
          f"(TP={tp} FP={fp} FN={fn} TN={tn})")

    return best_acc, model, {"precision": precision, "recall": recall, "f1": f1,
                              "user_mean": float(user_probs.mean()),
                              "other_mean": float(other_probs.mean())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max_other", type=int, default=500, help="Max 'other' samples per clip")
    ap.add_argument("--save_model", action="store_true", help="Save final model")
    args = ap.parse_args()

    print("USER-PLAYER CLASSIFIER TRAINING")
    print(f"Device: {DEVICE}")
    print(f"{'='*80}")

    by_clip = load_dataset(max_other_per_clip=args.max_other)
    for clip, data in by_clip.items():
        print(f"  {clip}: {len(data['user'])} user, {len(data['other'])} other")

    # Leave-one-clip-out cross-validation
    results = {}
    best_model = None
    best_acc = 0
    best_clip = None

    for test_clip in sorted(by_clip.keys()):
        print(f"\n--- Leave-one-out: test on {test_clip} ---")
        acc, model, metrics = train_and_evaluate(by_clip, test_clip)
        results[test_clip] = metrics
        if acc > best_acc:
            best_acc = acc
            best_model = model
            best_clip = test_clip

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY — Leave-one-clip-out cross-validation")
    print(f"{'='*80}")
    for clip, m in results.items():
        print(f"  {clip}: precision={m['precision']:.1%} recall={m['recall']:.1%} F1={m['f1']:.3f} "
              f"user_mean={m['user_mean']:.3f} other_mean={m['other_mean']:.3f}")

    avg_f1 = np.mean([m["f1"] for m in results.values()])
    avg_precision = np.mean([m["precision"] for m in results.values()])
    avg_recall = np.mean([m["recall"] for m in results.values()])
    print(f"\n  AVERAGE: precision={avg_precision:.1%} recall={avg_recall:.1%} F1={avg_f1:.3f}")

    # Save model
    if args.save_model and best_model is not None:
        # Train final model on ALL data
        print(f"\nTraining final model on all data...")
        all_crops = []
        all_labels = []
        for clip, data in by_clip.items():
            all_crops.extend(data["user"])
            all_labels.extend([1] * len(data["user"]))
            all_crops.extend(data["other"])
            all_labels.extend([0] * len(data["other"]))

        n_user = sum(all_labels)
        n_other = len(all_labels) - n_user
        pos_weight = torch.tensor([n_other / max(n_user, 1)], device=DEVICE)

        ds = PlayerCropDataset(all_crops, all_labels, augment=True)
        loader = DataLoader(ds, batch_size=BATCH_SIZE, shuffle=True, num_workers=0)
        model = UserPlayerCNN().to(DEVICE)
        optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-4)
        criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        for epoch in range(EPOCHS):
            model.train()
            for batch_x, batch_y in loader:
                batch_x, batch_y = batch_x.to(DEVICE), batch_y.to(DEVICE)
                optimizer.zero_grad()
                loss = criterion(model(batch_x), batch_y)
                loss.backward()
                optimizer.step()

        torch.save({
            "model_state": model.state_dict(),
            "crop_size": CROP_SIZE,
            "architecture": "UserPlayerCNN",
        }, MODEL_PATH)
        print(f"Saved final model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
