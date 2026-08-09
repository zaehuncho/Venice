"""Evaluate the shot-at-arm anchor on V3-V7 offline clips.

For each clip:
1. Run meter detection to find shot rising edges (ground truth shot starts)
2. At each shot edge, call notify_shot_start() to activate the anchor
3. Run pose detection on all players during the anchor window (~12 frames)
4. Check if the anchor picked the SAME player that meter proximity says is the user

Reports per-clip:
- Number of shots detected
- Anchor evaluations (how many times the anchor ran to completion)
- Correct locks (anchor picked the meter-confirmed user player)
- Lock accuracy %

Usage:
    C:\\Python314\\python.exe tools/diagnostics/eval_arm_anchor.py
"""
import os
os.environ.setdefault("YOLO_VERBOSE", "False")
import sys
import time
import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config
from ultralytics import YOLO

VIDEO_DIR = r"C:\Users\Administrator\Videos"
CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Red", "V7"),
]
METER_PROXIMITY_THRESHOLD = 400
DEBOUNCE = 18  # min frames between distinct shot rising edges


def get_meter_user_player(frame, meter, player_model):
    """Run meter + player detection, return the bbox of the player closest to meter (ground truth user)."""
    r = meter.detect(frame)
    if not r.detected or not hasattr(r, 'bbox') or not r.bbox or r.bbox[2] <= 0:
        return None, None
    bx, by, bw, bh = r.bbox
    meter_cx = bx + bw / 2.0
    meter_cy = by + bh / 2.0

    det = player_model.predict(frame, verbose=False, conf=0.25, imgsz=640)[0]
    if det.boxes is None or len(det.boxes) == 0:
        return None, r.bbox
    cls = det.boxes.cls.cpu().numpy().astype(int)
    xyxy = det.boxes.xyxy.cpu().numpy()
    player_boxes = xyxy[cls == 0]
    if len(player_boxes) == 0:
        return None, r.bbox

    best_d, best_box = 1e18, None
    for pb in player_boxes:
        pcx = (pb[0] + pb[2]) / 2.0
        pcy = (pb[1] + pb[3]) / 2.0
        d = np.sqrt((pcx - meter_cx) ** 2 + (pcy - meter_cy) ** 2)
        if d < best_d:
            best_d = d
            best_box = pb
    if best_d > METER_PROXIMITY_THRESHOLD:
        return None, r.bbox
    return best_box, r.bbox


def box_iou(box_a, box_b):
    """Compute IoU between two xyxy boxes."""
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / max(union, 1e-6)


def box_center_dist(box_a, box_b):
    """Distance between box centers."""
    acx = (box_a[0] + box_a[2]) / 2.0
    acy = (box_a[1] + box_a[3]) / 2.0
    bcx = (box_b[0] + box_b[2]) / 2.0
    bcy = (box_b[1] + box_b[3]) / 2.0
    return np.sqrt((acx - bcx) ** 2 + (acy - bcy) ** 2)


def evaluate_clip(video_name, count, start, meter_color, clip_name):
    """Evaluate shot-at-arm anchor on one clip."""
    video_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.exists(video_path):
        print(f"  {clip_name}: VIDEO NOT FOUND: {video_path}")
        return

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    # Load models
    player_model = YOLO(os.path.join(ROOT, "models", "orion_player_detect_v9.pt"))
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"; mcfg.meter_color = meter_color; mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    lms = []
    pose = PoseTimingDetector(handedness="Right", on_landmark=lambda lm: lms.append(lm))

    prev_meter = False
    last_shot = -10 ** 9
    shot_count = 0
    anchor_evals = 0
    anchor_correct = 0
    anchor_no_winner = 0
    anchor_no_ground_truth = 0
    anchor_details = []

    n = 0
    t0 = time.time()
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n

        # Meter detection
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))

        # Pass meter bbox as player hint (meter appears at user's shooter)
        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            pose.set_player_hint((bx, by, bx + bw, by + bh))

        # Detect shot rising edge -> simulate notify_shot_start
        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            last_shot = seq
            shot_count += 1
            # Get ground truth user player from meter proximity
            gt_box, meter_bbox = get_meter_user_player(frame, meter, player_model)
            # Arm the anchor
            pose.notify_shot_start(seq, timestamp=seq / fps)
            # Store ground truth for this shot
            anchor_details.append({
                "shot_idx": shot_count,
                "frame": seq,
                "gt_box": gt_box,
                "meter_bbox": meter_bbox,
                "anchor_box": None,
                "result": "pending",
            })

        # Run pose update (this also runs _arm_anchor_update if active)
        try:
            pose.update(frame, seq, frame_time=seq / fps)
        except Exception:
            pass

        # Check if anchor just evaluated (anchor_active went from True to False)
        if anchor_details and anchor_details[-1]["result"] == "pending":
            if not pose._arm_anchor_active:
                anchor_evals += 1
                detail = anchor_details[-1]
                # Get the anchor's pick from box_tracker
                if pose._box_tracker.cx is not None:
                    bt = pose._box_tracker
                    anchor_box = [bt.cx - bt.w / 2, bt.cy - bt.h / 2,
                                  bt.cx + bt.w / 2, bt.cy + bt.h / 2]
                    detail["anchor_box"] = anchor_box

                    if detail["gt_box"] is not None:
                        # Compare anchor pick to ground truth
                        iou = box_iou(anchor_box, detail["gt_box"])
                        dist = box_center_dist(anchor_box, detail["gt_box"])
                        # Correct if IoU > 0.3 or center distance < 80px
                        if iou > 0.3 or dist < 80:
                            anchor_correct += 1
                            detail["result"] = f"correct (IoU={iou:.2f}, dist={dist:.0f})"
                        else:
                            detail["result"] = f"WRONG (IoU={iou:.2f}, dist={dist:.0f})"
                    else:
                        anchor_no_ground_truth += 1
                        detail["result"] = "no_gt"
                else:
                    anchor_no_winner += 1
                    detail["result"] = "no_winner"

        prev_meter = fed
        n += 1

    cap.release()
    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(f"{clip_name} ({video_name})")
    print(f"  Frames: {n} in {elapsed:.0f}s | FPS: {fps:.0f}")
    print(f"  Shots detected (meter edges): {shot_count}")
    print(f"  Anchor evaluations: {anchor_evals}")
    print(f"  Correct locks: {anchor_correct}")
    print(f"  No winner (wrist rise < threshold): {anchor_no_winner}")
    print(f"  No ground truth (meter too far from players): {anchor_no_ground_truth}")
    if anchor_evals > 0:
        evaluable = anchor_evals - anchor_no_ground_truth
        if evaluable > 0:
            print(f"  LOCK ACCURACY: {anchor_correct}/{evaluable} = {100*anchor_correct/evaluable:.0f}%")
        else:
            print(f"  LOCK ACCURACY: N/A (no ground truth available)")

    for d in anchor_details:
        if d["result"] != "pending" and d["result"] != "no_gt":
            print(f"    Shot {d['shot_idx']} @ frame {d['frame']}: {d['result']}")

    return {
        "clip": clip_name,
        "shots": shot_count,
        "anchor_evals": anchor_evals,
        "correct": anchor_correct,
        "no_winner": anchor_no_winner,
        "no_gt": anchor_no_ground_truth,
    }


if __name__ == "__main__":
    print("Shot-at-Arm Anchor Evaluation on V3-V7")
    print("="*60)
    results = []
    for video_name, count, start, meter_color, clip_name in CLIPS:
        r = evaluate_clip(video_name, count, start, meter_color, clip_name)
        if r:
            results.append(r)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total_shots = sum(r["shots"] for r in results)
    total_evals = sum(r["anchor_evals"] for r in results)
    total_correct = sum(r["correct"] for r in results)
    total_no_winner = sum(r["no_winner"] for r in results)
    total_no_gt = sum(r["no_gt"] for r in results)
    print(f"Total shots: {total_shots}")
    print(f"Total anchor evals: {total_evals}")
    print(f"Total correct: {total_correct}")
    print(f"Total no winner: {total_no_winner}")
    print(f"Total no ground truth: {total_no_gt}")
    evaluable = total_evals - total_no_gt
    if evaluable > 0:
        print(f"OVERALL LOCK ACCURACY: {total_correct}/{evaluable} = {100*total_correct/evaluable:.0f}%")
