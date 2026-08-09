"""Measure per-frame detection rate + false-positive rate of the v9 class-1
under-player marker (stamina bar) on V3-V7 clips.

For each clip:
1. Run v9 detection on every Nth frame (stride=5 for speed)
2. Count frames where class-1 (stamina) is detected
3. For each detection, check if a player box (class-0) is adjacent
   (nearest-point distance < cap, marker NOT above player head)
4. Report:
   - Detection rate: % frames with a stamina detection
   - Adjacency rate: % stamina detections with an adjacent player
   - Mean confidence of stamina detections
   - Per-clip distribution of stamina positions (X/Y spread)

This determines whether TIER-A (marker-adjacency hard lock) can be a
hard gate or must degrade to a soft scoring term.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/measure_marker_reliability.py
"""
import os
os.environ.setdefault("YOLO_VERBOSE", "False")
import sys
import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from ultralytics import YOLO

VIDEO_DIR = r"C:\Users\Administrator\Videos"
CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "V7"),
]
STRIDE = 5  # sample every 5th frame for speed
MODEL_PATH = os.path.join(ROOT, "models", "orion_player_detect_v9.pt")
ADJACENCY_CAP_MULT = 2.5  # cap = (max(marker_w, 40) * MULT)^2


def measure_clip(path, start_frame, max_frames, model):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"  ERROR: cannot open {path}")
        return None

    total_sampled = 0
    stamina_detected = 0
    stamina_with_adjacent_player = 0
    stamina_confs = []
    stamina_positions = []  # (cx, cy) normalized to 0-1
    player_counts_when_stamina = []
    marker_above_head_count = 0

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    for fi in range(max_frames):
        if fi % STRIDE != 0:
            cap.grab()
            continue
        ret, frame = cap.read()
        if not ret:
            break
        total_sampled += 1

        r = model.predict(frame, verbose=False, conf=0.25, imgsz=640)[0]
        if r.boxes is None or len(r.boxes) == 0:
            continue

        cls = r.boxes.cls.cpu().numpy().astype(int)
        xyxy = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()

        player_boxes = xyxy[cls == 0]
        stamina_boxes = xyxy[cls == 1]
        stamina_confs_slice = confs[cls == 1]

        if len(stamina_boxes) == 0:
            continue

        stamina_detected += 1
        player_counts_when_stamina.append(len(player_boxes))

        # Pick first stamina (highest conf)
        best_si = int(np.argmax(stamina_confs_slice))
        sbox = stamina_boxes[best_si]
        s_conf = float(stamina_confs_slice[best_si])
        stamina_confs.append(s_conf)

        H, W = frame.shape[:2]
        s_cx = (sbox[0] + sbox[2]) / 2.0
        s_cy = (sbox[1] + sbox[3]) / 2.0
        sw = sbox[2] - sbox[0]
        sh = sbox[3] - sbox[1]
        stamina_positions.append((s_cx / W, s_cy / H))

        if len(player_boxes) == 0:
            continue

        # Adjacency check (from stamina_lock.py)
        cap_dist = (max(sw, 40) * ADJACENCY_CAP_MULT) ** 2
        best_d = 1e18
        found_adjacent = False
        for pb in player_boxes:
            X1, Y1, X2, Y2 = pb
            # Reject marker above player head
            if s_cy < Y1 - 0.25 * (Y2 - Y1):
                continue
            qx = min(max(s_cx, X1), X2)
            qy = min(max(s_cy, Y1), Y2)
            d = (s_cx - qx) ** 2 + (s_cy - qy) ** 2
            if d < best_d:
                best_d = d
            if d < cap_dist:
                found_adjacent = True

        if found_adjacent:
            stamina_with_adjacent_player += 1
        else:
            # Check if it was because marker was above all heads
            all_above = True
            for pb in player_boxes:
                X1, Y1, X2, Y2 = pb
                if s_cy >= Y1 - 0.25 * (Y2 - Y1):
                    all_above = False
                    break
            if all_above:
                marker_above_head_count += 1

    cap.release()

    if total_sampled == 0:
        return None

    det_rate = stamina_detected / total_sampled
    adj_rate = stamina_with_adjacent_player / stamina_detected if stamina_detected > 0 else 0
    mean_conf = np.mean(stamina_confs) if stamina_confs else 0
    pos_arr = np.array(stamina_positions) if stamina_positions else np.array([[0.5, 0.9]])

    return {
        "total_sampled": total_sampled,
        "stamina_detected": stamina_detected,
        "detection_rate": det_rate,
        "adjacent_rate": adj_rate,
        "mean_conf": mean_conf,
        "pos_x_mean": float(np.mean(pos_arr[:, 0])),
        "pos_x_std": float(np.std(pos_arr[:, 0])),
        "pos_y_mean": float(np.mean(pos_arr[:, 1])),
        "pos_y_std": float(np.std(pos_arr[:, 1])),
        "mean_players_when_stamina": float(np.mean(player_counts_when_stamina)) if player_counts_when_stamina else 0,
        "marker_above_head": marker_above_head_count,
        "no_adjacent": stamina_detected - stamina_with_adjacent_player - marker_above_head_count,
    }


def main():
    if not os.path.exists(MODEL_PATH):
        print(f"ERROR: model not found at {MODEL_PATH}")
        sys.exit(1)

    print("Loading v9 model...")
    model = YOLO(MODEL_PATH)
    print(f"Model loaded: {MODEL_PATH}\n")

    print("=" * 80)
    print("Under-Player Marker (v9 class-1 / stamina bar) Reliability Report")
    print("=" * 80)
    print(f"Sampling stride: every {STRIDE}th frame")
    print()

    all_results = []
    for fname, max_frames, start, label in CLIPS:
        path = os.path.join(VIDEO_DIR, fname)
        if not os.path.exists(path):
            print(f"{label}: FILE NOT FOUND ({path})")
            continue
        print(f"{label} ({fname})")
        result = measure_clip(path, start, max_frames, model)
        if result is None:
            print("  FAILED")
            continue
        all_results.append((label, result))

        print(f"  Frames sampled: {result['total_sampled']}")
        print(f"  Stamina detected: {result['stamina_detected']} ({result['detection_rate']*100:.1f}%)")
        print(f"  Adjacent player found: {result['adjacent_rate']*100:.1f}% of detections")
        print(f"  Mean confidence: {result['mean_conf']:.3f}")
        print(f"  Position X: {result['pos_x_mean']:.3f} ± {result['pos_x_std']:.3f} (normalized)")
        print(f"  Position Y: {result['pos_y_mean']:.3f} ± {result['pos_y_std']:.3f} (normalized)")
        print(f"  Mean players visible: {result['mean_players_when_stamina']:.1f}")
        print(f"  Marker above head (rejected): {result['marker_above_head']}")
        print(f"  No adjacent (other): {result['no_adjacent']}")
        print()

    if not all_results:
        print("No results collected.")
        return

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'Clip':<6} {'Det%':>6} {'Adj%':>6} {'Conf':>6} {'PosX':>14} {'PosY':>14} {'Players':>8}")
    for label, r in all_results:
        det_pct = f"{r['detection_rate']*100:.1f}%"
        adj_pct = f"{r['adjacent_rate']*100:.1f}%"
        posx = f"{r['pos_x_mean']:.3f}±{r['pos_x_std']:.3f}"
        posy = f"{r['pos_y_mean']:.3f}±{r['pos_y_std']:.3f}"
        print(f"{label:<6} {det_pct:>6} {adj_pct:>6} {r['mean_conf']:>6.3f} {posx:>14} {posy:>14} {r['mean_players_when_stamina']:>8.1f}")

    avg_det = np.mean([r["detection_rate"] for _, r in all_results])
    avg_adj = np.mean([r["adjacent_rate"] for _, r in all_results])
    avg_conf = np.mean([r["mean_conf"] for _, r in all_results])
    avg_det_pct = f"{avg_det*100:.1f}%"
    avg_adj_pct = f"{avg_adj*100:.1f}%"
    print(f"\n{'AVG':<6} {avg_det_pct:>6} {avg_adj_pct:>6} {avg_conf:>6.3f}")

    print()
    print("TIER-A RECOMMENDATION:")
    if avg_det > 0.7 and avg_adj > 0.85:
        print(f"  HARD LOCK — detection rate {avg_det*100:.0f}% + adjacency {avg_adj*100:.0f}% is reliable enough")
        print("  for a near-hard gate. Use TIER-A as primary selection when marker is present.")
    elif avg_det > 0.5:
        print(f"  SOFT BOOST — detection rate {avg_det*100:.0f}% is moderate. Use as a strong scoring term")
        print(f"  (weight 0.18-0.24) but don't hard-lock. Fallback to TIER-B camera-stable weighted score.")
    else:
        print(f"  WEAK SIGNAL — detection rate {avg_det*100:.0f}% is too low for TIER-A. Rely on TIER-B")
        print("  camera-stable priors (center-bottom + size + temporal continuity).")


if __name__ == "__main__":
    main()
