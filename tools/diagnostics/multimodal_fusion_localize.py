#!/usr/bin/env python3
"""Multi-modal fusion release detector — combines ALL signals (pose, ball,
optical flow, audio) into a single fused release estimate.

Claude's 5-cue pose fusion got 135ms IQR (offline). Different modalities
have independent noise → fusing across modalities may tighten further.

Signals fused:
  - wrist-Y apex (pose)
  - wrist-Y accel-peak (pose flick)
  - forearm angle accel-peak (pose)
  - ball-wrist distance (HSV ball)
  - optical flow upward magnitude
  - audio spectrogram cross-correlation

Usage:
    C:\\Python314\\python.exe tools/diagnostics/multimodal_fusion_localize.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import subprocess
import tempfile

import numpy as np
import cv2
from scipy import signal as scipy_signal
from scipy.io import wavfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config
from ultralytics import YOLO

KP_WRIST_L, KP_WRIST_R = 9, 10
KP_ELBOW_L, KP_ELBOW_R = 7, 8
KP_SHOULDER_L, KP_SHOULDER_R = 5, 6
KP_CONF_FLOOR = 0.25
DEBOUNCE = 18
SEARCH_BEFORE = 15
SEARCH_AFTER = 45

ORANGE_LOW = np.array([5, 80, 80])
ORANGE_HIGH = np.array([25, 255, 255])


def angle_between(x1, y1, x2, y2):
    return math.degrees(math.atan2(-(y2 - y1), x2 - x1))


def detect_ball_hsv(frame, player_box=None):
    if player_box is not None:
        px, py, pw, ph = player_box
        pad = max(pw, ph) // 2
        x1 = max(0, px - pad)
        y1 = max(0, py - pad)
        x2 = min(frame.shape[1], px + pw + pad)
        y2 = min(frame.shape[0], py + ph + pad)
        roi = frame[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1
    else:
        roi = frame
        offset_x, offset_y = 0, 0

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, ORANGE_LOW, ORANGE_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = 0
    for c in contours:
        area = cv2.contourArea(c)
        if area < 50 or area > 50000:
            continue
        perimeter = cv2.arcLength(c, True)
        if perimeter < 1:
            continue
        circularity = 4 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.3:
            continue
        x, y, w, h = cv2.boundingRect(c)
        aspect = float(w) / max(h, 1)
        if aspect < 0.5 or aspect > 2.0:
            continue
        score = area * circularity
        if score > best_score:
            best_score = score
            best = (x + w / 2 + offset_x, y + h / 2 + offset_y, area)
    return best


def extract_audio(video_path, start_frame, count, fps):
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=os.environ.get("TEMP", "/tmp"))
    tmp_wav.close()
    start_time = start_frame / fps
    duration = count / fps
    cmd = ["ffmpeg", "-y", "-ss", f"{start_time:.3f}", "-t", f"{duration:.3f}",
           "-i", video_path, "-vn", "-ac", "1", "-ar", "48000", "-f", "wav", tmp_wav.name]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        sr, audio = wavfile.read(tmp_wav.name)
        os.unlink(tmp_wav.name)
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        return audio, sr
    except Exception:
        if os.path.exists(tmp_wav.name):
            os.unlink(tmp_wav.name)
        return None, None


def process_clip(video, count, start, handed, meter_color):
    pose_model_path = os.path.join(ROOT, "models", "orion_pose2k_n.pt")
    if not os.path.exists(pose_model_path):
        pose_model_path = os.path.join(ROOT, "models", "yolo11n-pose.pt")
    pose_yolo = YOLO(pose_model_path)
    player_model = YOLO(os.path.join(ROOT, "models", "orion_player_detect_v9.pt"))

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    is_right = str(handed).strip().lower().startswith("r")
    wr_kp = KP_WRIST_R if is_right else KP_WRIST_L
    el_kp = KP_ELBOW_R if is_right else KP_ELBOW_L

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count}")

    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0
    player_box = None
    prev_crop = None

    # Per-frame signals
    wrist_y = []
    forearm_angle = []
    ball_wrist_dist = []
    flow_up = []
    audio_energy = []

    print("  Processing video...", end=" ", flush=True)
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n

        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))

        # Player detection
        det = player_model.predict(frame, conf=0.25, verbose=False)
        player_box = None
        if det and det[0].boxes is not None and len(det[0].boxes) > 0:
            cls = det[0].boxes.cls.cpu().numpy().astype(int)
            xyxy = det[0].boxes.xyxy.cpu().numpy()
            players = xyxy[cls == 0] if len(cls) > 0 else np.array([]).reshape(0, 4)
            if len(players) > 0:
                if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
                    bx, by, bw, bh = r.bbox
                    mx, my = bx + bw / 2, by + bh / 2
                    dists = [abs((b[0]+b[2])/2 - mx) + abs((b[1]+b[3])/2 - my) for b in players]
                    best = np.argmin(dists)
                else:
                    areas = [(b[2]-b[0]) * (b[3]-b[1]) for b in players]
                    best = np.argmax(areas)
                b = players[best]
                player_box = (int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1]))

        # Pose
        fa = float('nan')
        wy = float('nan')
        wx = float('nan')
        if player_box is not None:
            px, py, pw, ph = player_box
            pad = max(pw, ph) // 4
            x1 = max(0, px - pad)
            y1 = max(0, py - pad)
            x2 = min(frame.shape[1], px + pw + pad)
            y2 = min(frame.shape[0], py + ph + pad)
            crop = frame[y1:y2, x1:x2]
            pose_result = pose_yolo.predict(crop, verbose=False, conf=0.1)
        else:
            pose_result = pose_yolo.predict(frame, verbose=False, conf=0.1)
            x1, y1 = 0, 0
            crop = frame

        if pose_result and pose_result[0].keypoints is not None:
            kpts = pose_result[0].keypoints.data.cpu().numpy()
            if len(kpts) > 0:
                confs = kpts[..., 2].mean(axis=-1)
                best_person = np.argmax(confs)
                kp = kpts[best_person]
                if kp[wr_kp, 2] >= KP_CONF_FLOOR:
                    wy = float(kp[wr_kp, 1] + y1) / frame.shape[0]  # normalized
                    wx = float(kp[wr_kp, 0] + x1)
                if kp[el_kp, 2] >= KP_CONF_FLOOR and kp[wr_kp, 2] >= KP_CONF_FLOOR:
                    fa = angle_between(kp[el_kp, 0] + x1, kp[el_kp, 1] + y1,
                                       kp[wr_kp, 0] + x1, kp[wr_kp, 1] + y1)

        wrist_y.append(wy)
        forearm_angle.append(fa)

        # Ball
        ball = detect_ball_hsv(frame, player_box)
        bd = float('nan')
        if ball is not None and not np.isnan(wx):
            bd = math.sqrt((ball[0] - wx)**2 + (ball[1] - (wy * frame.shape[0]))**2)
        ball_wrist_dist.append(bd)

        # Optical flow
        fu = float('nan')
        if player_box is not None:
            px, py, pw, ph = player_box
            cx1 = max(0, px - 20)
            cy1 = max(0, py - 20)
            cx2 = min(frame.shape[1], px + pw + 20)
            cy2 = min(frame.shape[0], py + int(ph * 0.7) + 20)
            gray_crop = cv2.cvtColor(frame[cy1:cy2, cx1:cx2], cv2.COLOR_BGR2GRAY)
            gray_crop = cv2.resize(gray_crop, (160, 160))
            if prev_crop is not None and prev_crop.shape == gray_crop.shape:
                flow = cv2.calcOpticalFlowFarneback(prev_crop, gray_crop, None,
                    0.5, 3, 15, 3, 5, 1.2, 0)
                fu = float(np.percentile(-flow[:80, :, 1], 95))
            prev_crop = gray_crop
        else:
            prev_crop = None
        flow_up.append(fu)

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    # Audio
    print("  Extracting audio...", end=" ", flush=True)
    audio, sr = extract_audio(video, start, count, fps)
    if audio is not None:
        hop = int(sr * 0.001)
        for i in range(0, len(audio) - hop, hop):
            e = math.sqrt(np.mean(audio[i:i+hop]**2))
            audio_energy.append(e)
        # Resample audio energy to match frame count
        audio_times = np.arange(len(audio_energy)) * hop / sr
        frame_times = np.arange(n) / fps
        audio_energy_interp = np.interp(frame_times, audio_times, audio_energy)
        print(f"done | {len(audio)} samples")
    else:
        audio_energy_interp = np.full(n, np.nan)
        print("FAILED")

    # Prepare all signals
    def prep(arr):
        a = np.array(arr, dtype=float)
        m = ~np.isnan(a)
        if m.sum() > 10:
            a = np.interp(np.arange(len(a)), np.where(m)[0], a[m])
        else:
            return None, None, None
        a = np.convolve(a, [0.25, 0.5, 0.25], mode="same")
        return a, np.gradient(a), np.gradient(np.gradient(a))

    ms = 1000.0 / fps

    # Compute per-shot cue offsets for each signal
    cue_names = ["wrist_y", "forearm_angle", "ball_dist", "flow_up", "audio_energy"]
    raw_signals = [wrist_y, forearm_angle, ball_wrist_dist, flow_up, audio_energy_interp.tolist()]

    all_cues = {}  # cue_name -> list of offsets (ms from meter edge)

    for cname, raw in zip(cue_names, raw_signals):
        val, vel, acc = prep(raw)
        if val is None:
            continue
        offsets = []
        for e in shot_edges:
            lo = max(0, e - SEARCH_BEFORE)
            hi = min(len(val), e + SEARCH_AFTER)
            if hi - lo < 6:
                continue
            seg_val = val[lo:hi]
            seg_acc = acc[lo:hi] if acc is not None else np.zeros(hi - lo)

            if cname in ("wrist_y", "audio_energy"):
                # For wrist-Y: release = min Y (highest point)
                # For audio: release = max energy
                apex = lo + int(np.argmin(seg_val)) if cname == "wrist_y" else lo + int(np.argmax(seg_val))
            elif cname == "ball_dist":
                apex = lo + int(np.argmax(seg_val))  # max distance = separation
            else:
                apex = lo + int(np.argmax(seg_val))  # max = peak

            # Use accel-peak for all (Claude found it's the sharpest)
            accelpeak = lo + int(np.argmax(np.abs(seg_acc))) if acc is not None else apex
            offsets.append((accelpeak - e) * ms)

        if len(offsets) >= 5:
            all_cues[cname] = offsets

    # Print individual cues
    print(f"\n  INDIVIDUAL CUES (accel-peak):")
    print(f"  {'Cue':<20} {'n':>4} {'Median':>8} {'IQR':>8} {'±40ms':>8}")
    print("  " + "-" * 55)

    cue_medians = {}
    for cname, offs in all_cues.items():
        a = np.array(sorted(offs))
        med = np.median(a)
        iqr = np.percentile(a, 75) - np.percentile(a, 25)
        w40 = sum(1 for x in offs if abs(x - med) <= 40)
        cue_medians[cname] = med
        print(f"  {cname:<20} {len(offs):>4} {med:>+7.0f}ms {iqr:>7.0f}ms {f'{w40}/{len(offs)}':>8}")

    # Fusion: calibrate each cue to its median, then fuse
    # Only use shots that have ALL cues
    if len(all_cues) < 2:
        print("\n  [warn] not enough cues for fusion")
        return {}

    # Find shots present in all cues (by index)
    min_n = min(len(offs) for offs in all_cues.values())
    cal_cues = []
    for cname, offs in all_cues.items():
        # Trim to min_n (they should all be same length since same shots)
        cal = np.array(offs[:min_n]) - cue_medians[cname]
        cal_cues.append(cal)

    cal_arr = np.array(cal_cues).T  # (n_shots, n_cues)

    # Fusion methods
    print(f"\n  FUSION ({len(all_cues)} cues, {min_n} shots):")
    print(f"  {'Method':<20} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 50)

    results = {}
    for label, fused in (("mean", np.mean(cal_arr, axis=1)),
                         ("median", np.median(cal_arr, axis=1)),
                         ("trimmed_mean", np.array([np.mean(np.sort(c)[1:-1]) if len(c) > 2 else np.mean(c) for c in cal_arr]))):
        a = np.array(sorted(fused))
        med = np.median(a)
        iqr = np.percentile(a, 75) - np.percentile(a, 25)
        w40 = sum(1 for x in fused if abs(x) <= 40)
        w80 = sum(1 for x in fused if abs(x) <= 80)
        print(f"  {label:<20} {iqr:>7.0f}ms {f'{w40}/{min_n}':>8} {f'{w80}/{min_n}':>8}")
        results[f"fusion_{label}"] = {"n": min_n, "iqr": iqr, "within_40": w40, "within_80": w80}

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"MULTI-MODAL FUSION LOCALIZABILITY TEST")
    print(f"Target: <80ms IQR (decision gate for input-hook build)")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color)

    print(f"\n{'='*70}")
    print("VERDICT:")
    best_iqr = min((r["iqr"] for r in results.values()), default=9999)
    if best_iqr < 80:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 80ms → ESCALATE to input-hook build!")
    elif best_iqr < 135:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 135ms → beats Claude's pose-only fusion!")
    elif best_iqr < 200:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 200ms → marginal improvement")
    else:
        print(f"  BEST IQR = {best_iqr:.0f}ms ≥ 200ms → multi-modal fusion also floored")


if __name__ == "__main__":
    main()
