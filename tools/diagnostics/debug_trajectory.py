"""Dump wrist-Y, hip-Y, knee-Y trajectory on a shot window to diagnose phase detection."""
import os, sys, cv2, numpy as np
os.environ.setdefault("YOLO_VERBOSE", "False")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

VIDEO = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4"
START = int(sys.argv[2]) if len(sys.argv) > 2 else 3760
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 500

lms = []
pose = PoseTimingDetector(handedness="Right", on_landmark=lambda lm: lms.append(lm))
mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
mcfg.meter_style = "Arrow2"; mcfg.meter_color = "Purple"; mcfg.auto_meter_color = False
meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg); meter.set_active_style("Arrow2")

cap = cv2.VideoCapture(VIDEO)
cap.set(cv2.CAP_PROP_POS_FRAMES, START)
fps = cap.get(cv2.CAP_PROP_FPS) or 60.0

for n in range(COUNT):
    ok, frame = cap.read()
    if not ok: break
    seq = START + n
    mr = meter.detect(frame)
    mdet = bool(mr.detected and mr.rejection_reason in ("", "green_not_found"))
    if mdet and hasattr(mr, 'bbox') and mr.bbox and len(mr.bbox) >= 4 and mr.bbox[2] > 0:
        bx, by, bw, bh = mr.bbox
        pose.set_player_hint((bx, by, bx + bw, by + bh))
    pose.update(frame, seq)

    # Get current trajectory values
    if pose._buf_count > 0:
        idx = (pose._buf_idx - 1) % pose._max_buf
        wy = pose._wrist_y[idx]
        hy = pose._hip_y[idx]
        ky = pose._knee_y[idx]
        wy_s = f"{wy:.3f}" if not np.isnan(wy) else "  nan"
        hy_s = f"{hy:.3f}" if not np.isnan(hy) else "  nan"
        ky_s = f"{ky:.3f}" if not np.isnan(ky) else "  nan"
    else:
        wy_s = hy_s = ky_s = "  ---"
    flag = " *** METER ***" if mdet else ""
    print(f"f={seq} wy={wy_s} hy={hy_s} ky={ky_s}{flag}")

cap.release()
print(f"\nLandmarks: {len(lms)}")
for lm in lms:
    print(f"  {lm.kind} f={lm.frame_seq} conf={lm.confidence:.2f}")
