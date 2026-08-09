#!/usr/bin/env python3
"""Scan video frames for the under-player CIRCLE/RING indicator.

NBA 2K shows a colored circle beneath the user's player (when the setting is enabled).
This script scans frames to:
1. Detect circular markers near player feet using HSV color filtering + Hough circles
2. Characterize: color, size, position relative to player box, persistence
3. Save annotated frames for visual verification

Usage:
    C:\\Python314\\python.exe tools/diagnostics/ring_indicator_scan.py
"""
from __future__ import annotations

import os
import sys
import argparse
import json

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

VIDEOS = r"C:\Users\Administrator\Videos"

CLIPS = [
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "V7"),
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "V3"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "V6"),
]

OUTPUT_DIR = os.path.join(ROOT, "tools", "diagnostics", "ring_scan_output")


def detect_circles_in_region(frame, region_mask=None):
    """Detect circles using multiple color spaces and Hough transform."""
    results = []
    h, w = frame.shape[:2]

    # Convert to HSV for color-based detection
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    # Try several color ranges that NBA 2K uses for player indicators
    # The ring is typically a bright/saturated color (blue, red, green, yellow)
    color_ranges = {
        "blue":   ([100, 150, 100], [130, 255, 255]),
        "red":    ([0, 150, 100], [10, 255, 255]),
        "red2":   ([170, 150, 100], [180, 255, 255]),
        "green":  ([50, 150, 100], [80, 255, 255]),
        "yellow": ([25, 150, 100], [35, 255, 255]),
        "cyan":   ([85, 150, 100], [100, 255, 255]),
        "magenta":([140, 150, 100], [170, 255, 255]),
        "orange": ([15, 150, 100], [25, 255, 255]),
    }

    for color_name, (lower, upper) in color_ranges.items():
        lower = np.array(lower, np.uint8)
        upper = np.array(upper, np.uint8)
        mask = cv2.inRange(hsv, lower, upper)

        if region_mask is not None:
            mask = cv2.bitwise_and(mask, region_mask)

        # Clean up
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        # Find contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 20 or area > 2000:
                continue
            # Check circularity
            perimeter = cv2.arcLength(cnt, True)
            if perimeter < 10:
                continue
            circularity = 4 * np.pi * area / (perimeter * perimeter)
            if circularity < 0.5:
                continue
            # Get center and radius
            (cx, cy), radius = cv2.minEnclosingCircle(cnt)
            if radius < 3 or radius > 30:
                continue
            results.append({
                "color": color_name,
                "cx": int(cx),
                "cy": int(cy),
                "radius": int(radius),
                "area": float(area),
                "circularity": float(circularity),
            })

    # Also try Hough circles on grayscale
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if region_mask is not None:
        gray = cv2.bitwise_and(gray, gray, mask=region_mask)

    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1, minDist=20,
        param1=50, param2=15, minRadius=3, maxRadius=25
    )
    if circles is not None:
        for c in circles[0]:
            cx, cy, r = int(c[0]), int(c[1]), int(c[2])
            # Check if this circle overlaps with any color-detected one
            overlaps = any(
                abs(cx - d["cx"]) < 10 and abs(cy - d["cy"]) < 10
                for d in results
            )
            if not overlaps:
                # Sample color at center
                if 0 <= cy < h and 0 <= cx < w:
                    pixel_hsv = hsv[cy, cx]
                    results.append({
                        "color": f"hsv({pixel_hsv[0]},{pixel_hsv[1]},{pixel_hsv[2]})",
                        "cx": cx, "cy": cy, "radius": r,
                        "area": float(np.pi * r * r),
                        "circularity": 0.0,
                        "source": "hough",
                    })

    return results


def scan_clip(video_path, count, start, clip_name, sample_interval=30):
    """Scan a clip for ring indicators at regular intervals."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_detections = []
    n = 0
    frames_scanned = 0

    while n < count:
        ok, frame = cap.read()
        if not ok:
            break

        if n % sample_interval == 0:
            h, w = frame.shape[:2]
            # Focus on lower third of frame (where player feet/rings are)
            lower_mask = np.zeros((h, w), np.uint8)
            lower_mask[int(h * 0.4):, :] = 255

            circles = detect_circles_in_region(frame, lower_mask)

            if circles:
                # Save annotated frame
                annotated = frame.copy()
                for c in circles:
                    cv2.circle(annotated, (c["cx"], c["cy"]), c["radius"], (0, 255, 0), 2)
                    cv2.putText(annotated, c["color"], (c["cx"] + 5, c["cy"] - 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
                fname = f"{clip_name}_frame{n:05d}.png"
                cv2.imwrite(os.path.join(OUTPUT_DIR, fname), annotated)

                for c in circles:
                    c["frame"] = n
                    c["frame_seq"] = start + n
                    c["norm_x"] = c["cx"] / w
                    c["norm_y"] = c["cy"] / h
                    all_detections.append(c)

            frames_scanned += 1

        n += 1
    cap.release()

    print(f"  {clip_name}: scanned {frames_scanned} frames, found {len(all_detections)} circles")

    # Analyze detections
    if all_detections:
        # Group by color
        by_color = {}
        for d in all_detections:
            color = d["color"]
            by_color.setdefault(color, []).append(d)

        print(f"  By color:")
        for color, dets in sorted(by_color.items(), key=lambda x: -len(x[1])):
            radii = [d["radius"] for d in dets]
            norm_ys = [d["norm_y"] for d in dets]
            norm_xs = [d["norm_x"] for d in dets]
            print(f"    {color}: {len(dets)} detections, "
                  f"radius {np.median(radii):.0f}px (range {min(radii)}-{max(radii)}), "
                  f"norm_y {np.median(norm_ys):.2f}, norm_x {np.median(norm_xs):.2f}")

    return all_detections


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=str, default="all")
    ap.add_argument("--interval", type=int, default=30, help="Sample every N frames")
    args = ap.parse_args()

    print("RING/CIRCLE INDICATOR SCAN")
    print(f"Looking for under-player colored circles in video frames")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*80}")

    all_detections = []
    for clip_file, count, start, clip_name in CLIPS:
        if args.clips != "all" and clip_name not in args.clips.split(","):
            continue
        video = os.path.join(VIDEOS, clip_file)
        if not os.path.exists(video):
            print(f"  [skip] {clip_file} not found")
            continue
        dets = scan_clip(video, count, start, clip_name, args.interval)
        all_detections.extend(dets)

    # Save full results
    results_path = os.path.join(OUTPUT_DIR, "ring_scan_results.json")
    with open(results_path, "w") as f:
        json.dump(all_detections, f, indent=2)
    print(f"\nResults saved to {results_path}")

    # Summary
    if all_detections:
        print(f"\n{'='*80}")
        print("SUMMARY")
        print(f"Total circles detected: {len(all_detections)}")
        by_color = {}
        for d in all_detections:
            by_color.setdefault(d["color"], []).append(d)
        for color, dets in sorted(by_color.items(), key=lambda x: -len(x[1])):
            print(f"  {color}: {len(dets)} detections")
    else:
        print("\nNo circles detected — try adjusting color ranges or sample interval")


if __name__ == "__main__":
    main()
