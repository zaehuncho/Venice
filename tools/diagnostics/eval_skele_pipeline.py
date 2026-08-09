"""Score the SKELE (no-meter pose) pipeline against shot ground truth, offline.

Runs the full pose pipeline (player-lock -> pose -> shooting-wrist -> push/release
landmarks) AND the shot METER over the same frames, then correlates: for every real
shot (a meter rising edge) it checks whether a push/release landmark fired nearby and
measures the landmark->meter-appearance OFFSET. The offset STD across shots is the
make-or-break metric — a no-meter clock only greens if one calibrated offset lands the
shot, so a wide std (the historical ~326ms "precision wall") means the landmark signal
is too noisy regardless of how often it fires.

Reports the three success criteria:
  1. player-lock % on shot frames
  2. push / release / pair fire-rate per real shot
  3. landmark->meter offset mean +/- STD (ms)   <- the precision target

Usage:
    C:\\Python314\\python.exe tools/diagnostics/eval_skele_pipeline.py VIDEO [count] [start] [handedness]
First locate a shot-dense window with find_shot_windows.py. See memory
nexusvision-nometer-pose-mode (the GLM skele track).
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

VIDEO = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
HANDED = sys.argv[4] if len(sys.argv) > 4 else "Right"
METER_COLOR = os.environ.get("METER_COLOR", "Purple")
SHOT_MATCH_FRAMES = 30   # a landmark counts for a shot if within +/- this many frames
DEBOUNCE = 18            # min frames between distinct shot rising-edges

lms = []
pose = PoseTimingDetector(handedness=HANDED, on_landmark=lambda lm: lms.append(lm))

mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
mcfg.meter_style = "Arrow2"; mcfg.meter_color = METER_COLOR; mcfg.auto_meter_color = False
meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg); meter.set_active_style("Arrow2")

cap = cv2.VideoCapture(VIDEO)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
START = int(sys.argv[3]) if len(sys.argv) > 3 else int(total * 0.30)
cap.set(cv2.CAP_PROP_POS_FRAMES, START)
print(f"{os.path.basename(VIDEO)} | {fps:.0f}fps | window {START}..{START+COUNT} | handedness={HANDED}")

shot_appears, wrist_ok, n = [], 0, 0
shot_frame_lock, shot_frame_total = 0, 0
prev_meter = False
last_shot = -10 ** 9
t0 = time.time()
while n < COUNT:
    ok, frame = cap.read()
    if not ok:
        break
    seq = START + n
    r = meter.detect(frame)
    fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
    # Pass meter bbox as player hint BEFORE pose update (meter appears at user's shooter)
    if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
        bx, by, bw, bh = r.bbox
        pose.set_player_hint((bx, by, bx + bw, by + bh))
    try:
        pose.update(frame, seq, frame_time=seq / fps)
        if pose._buf_count > 0 and not np.isnan(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]):
            wrist_ok += 1
    except Exception:
        pass
    if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
        shot_appears.append(seq)
        last_shot = seq
    prev_meter = fed
    # Track lock specifically on shot frames
    if fed:
        shot_frame_lock += 1 if pose._buf_count > 0 and not np.isnan(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]) else 0
        shot_frame_total += 1
    n += 1
cap.release()

pushes = [l.frame_seq for l in lms if getattr(l, "kind", "") == "push"]
releases = [l.frame_seq for l in lms if getattr(l, "kind", "") == "release"]


def nearest(seqs, target):
    cand = [s for s in seqs if abs(s - target) <= SHOT_MATCH_FRAMES]
    return min(cand, key=lambda s: abs(s - target)) if cand else None


push_off, rel_off, with_push, with_rel, with_both = [], [], 0, 0, 0
for s in shot_appears:
    p, rl = nearest(pushes, s), nearest(releases, s)
    if p is not None:
        with_push += 1; push_off.append((s - p) * 1000.0 / fps)
    if rl is not None:
        with_rel += 1; rel_off.append((s - rl) * 1000.0 / fps)
    if p is not None and rl is not None:
        with_both += 1

ns = max(len(shot_appears), 1)
print(f"\nprocessed {n} frames in {time.time()-t0:.0f}s")
print(f"[1] player-lock (usable wrist): {wrist_ok}/{n} ({100*wrist_ok/max(n,1):.0f}%)")
if shot_frame_total > 0:
    print(f"[1] player-lock ON SHOT FRAMES: {shot_frame_lock}/{shot_frame_total} ({100*shot_frame_lock/shot_frame_total:.0f}%)")
print(f"[2] real shots (meter edges): {len(shot_appears)} | push {with_push}/{ns} "
      f"({100*with_push/ns:.0f}%) | release {with_rel}/{ns} ({100*with_rel/ns:.0f}%) | "
      f"pair {with_both}/{ns} ({100*with_both/ns:.0f}%)")
if push_off:
    print(f"[3] push->meter offset: mean {np.mean(push_off):+.0f}ms  STD {np.std(push_off):.0f}ms "
          f"(target: STD well under the ~30-40ms green window)")
if rel_off:
    print(f"[3] release->meter offset: mean {np.mean(rel_off):+.0f}ms  STD {np.std(rel_off):.0f}ms")
print(f"raw landmarks: {len(lms)} (push {len(pushes)}, release {len(releases)})")
