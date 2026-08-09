#!/usr/bin/env python3
"""Auto-label extracted bar training frames using current model + HSV fallback.
Saves YOLO-format labels (class 0 = stamina_bar) alongside images.
Also extracts miss frames (where model fails but HSV finds a bar) for hard negatives.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from ultralytics import YOLO

def label_frames(bar_model, img_dir, label_dir):
    os.makedirs(label_dir, exist_ok=True)
    images = sorted(f for f in os.listdir(img_dir) if f.endswith(".png"))
    labeled = 0
    skipped = 0
    
    for img_name in images:
        img_path = os.path.join(img_dir, img_name)
        fr = cv2.imread(img_path)
        if fr is None:
            continue
        H, W = fr.shape[:2]
        
        # Run model at low conf
        r = bar_model.predict(fr, verbose=False, conf=0.01)[0]
        best_box = None
        best_conf = 0
        
        if r.boxes is not None and len(r.boxes):
            for bi in range(len(r.boxes)):
                x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                bw, bh = x2 - x1, y2 - y1
                if 10 <= bw <= 250 and 10 <= bh <= 250:
                    ar = max(bw, bh) / max(min(bw, bh), 1)
                    if ar >= 1.5:
                        c = float(r.boxes.conf.cpu().numpy()[bi])
                        if c > best_conf:
                            best_conf = c
                            best_box = (x1, y1, x2, y2)
        
        # If model didn't find anything, try HSV
        if best_box is None:
            hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
            ym = cv2.inRange(hsv, (15, 80, 120), (40, 255, 255))
            bm = cv2.inRange(hsv, (90, 80, 90), (128, 255, 255))
            comb = cv2.dilate(((ym | bm) > 0).astype("uint8") * 255,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
            cnts = cv2.findContours(comb, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
            best_score = 0
            for c in cnts:
                x, y, w, h = cv2.boundingRect(c)
                horiz = w >= 18 and 2 <= h <= 32 and w >= 2.0 * h
                vert = h >= 18 and 2 <= w <= 32 and h >= 2.0 * w
                if not (horiz or vert):
                    continue
                ysum = int(ym[y:y+h, x:x+w].sum())
                bsum = int(bm[y:y+h, x:x+w].sum())
                if ysum == 0 or bsum == 0:
                    continue
                score = min(ysum, bsum)
                if score > best_score:
                    best_score = score
                    best_box = (x, y, x+w, y+h)
        
        if best_box is not None:
            x1, y1, x2, y2 = best_box
            # YOLO format: class cx cy w h (normalized)
            cx = (x1 + x2) / 2.0 / W
            cy = (y1 + y2) / 2.0 / H
            bw = (x2 - x1) / W
            bh = (y2 - y1) / H
            label_name = img_name.replace(".png", ".txt")
            with open(os.path.join(label_dir, label_name), "w") as f:
                f.write(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
            labeled += 1
        else:
            skipped += 1
    
    print(f"  Labeled: {labeled}, Skipped (no bar found): {skipped}")
    return labeled

def main():
    bar = YOLO("models/orion_bar_n.pt")
    img_dir = "logs/diagnostics/bar_training_frames"
    label_dir = "logs/diagnostics/bar_training_labels"
    
    print("Auto-labeling training frames...")
    labeled = label_frames(bar, img_dir, label_dir)
    
    # Create YOLO dataset structure
    dataset_dir = "datasets/bar_park"
    for split in ["train"]:
        os.makedirs(os.path.join(dataset_dir, "images", split), exist_ok=True)
        os.makedirs(os.path.join(dataset_dir, "labels", split), exist_ok=True)
    
    # Copy labeled images and labels
    import shutil
    count = 0
    for img_name in sorted(os.listdir(img_dir)):
        if not img_name.endswith(".png"):
            continue
        label_name = img_name.replace(".png", ".txt")
        label_path = os.path.join(label_dir, label_name)
        if os.path.exists(label_path):
            shutil.copy(os.path.join(img_dir, img_name),
                       os.path.join(dataset_dir, "images", "train", img_name))
            shutil.copy(label_path,
                       os.path.join(dataset_dir, "labels", "train", label_name))
            count += 1
    
    print(f"\nDataset created: {dataset_dir} ({count} images)")
    
    # Also copy existing training data if available
    existing = "datasets/bar_dataset"
    if os.path.exists(existing):
        for split in ["train", "val"]:
            src_img = os.path.join(existing, "images", split)
            src_lbl = os.path.join(existing, "labels", split)
            if os.path.isdir(src_img):
                for f in os.listdir(src_img):
                    shutil.copy(os.path.join(src_img, f),
                               os.path.join(dataset_dir, "images", "train", f"old_{f}"))
                    count += 1
            if os.path.isdir(src_lbl):
                for f in os.listdir(src_lbl):
                    shutil.copy(os.path.join(src_lbl, f),
                               os.path.join(dataset_dir, "labels", "train", f"old_{f}"))
        print(f"Including existing data: {count} total images")
    
    # Write dataset.yaml
    yaml_path = os.path.join(dataset_dir, "dataset.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"path: {os.path.abspath(dataset_dir)}\n")
        f.write("train: images/train\n")
        f.write("val: images/train\n")
        f.write("nc: 1\n")
        f.write("names: ['stamina_bar']\n")
    
    print(f"\nReady to retrain with: {yaml_path}")
    print(f"Run: yolo train model=yolov8n.pt data={yaml_path} epochs=50 imgsz=640")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
