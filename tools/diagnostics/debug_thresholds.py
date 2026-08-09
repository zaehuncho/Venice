"""Debug: print adaptive threshold values during shot frames."""
import os, sys, cv2, numpy as np, time
os.environ.setdefault("YOLO_VERBOSE", "False")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from pose_timing import PoseTimingDetector, EMA_TAU_MS, PUSH_VEL_PERCENTILE, ADAPTIVE_HISTORY_FRAMES
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

    # Print adaptive state every 10 frames or on meter frames
    if n % 10 == 0 or mdet:
        if pose._buf_count >= 30:
            wy, hy, ky, t, fseqs = pose._get_ordered()
            dt_ms = pose._dt_ms
            wy_s = pose._ema_tau(pose._fill_nans(wy), EMA_TAU_MS, dt_ms)
            wy_vel = np.gradient(wy_s, t)
            wy_vel_s = pose._ema_tau(pose._fill_nans(wy_vel), EMA_TAU_MS, dt_ms)
            n_hist = min(ADAPTIVE_HISTORY_FRAMES, len(wy_s))
            vel_hist = wy_vel_s[-n_hist:]
            vel_valid = vel_hist[~np.isnan(vel_hist)]
            if len(vel_valid) >= 20:
                push_vel = float(np.percentile(vel_valid, PUSH_VEL_PERCENTILE))
                push_vel = min(push_vel, -0.04)
                cur_vel = wy_vel_s[-1] if not np.isnan(wy_vel_s[-1]) else 0.0
                cur_wy = wy_s[-1] if not np.isnan(wy_s[-1]) else float('nan')
                rest_w = float(np.nanmedian(wy_s[-n_hist:]))
                flag = " *** METER ***" if mdet else ""
                print(f"f={seq} cur_vel={cur_vel:.4f} push_thr={push_vel:.4f} cur_wy={cur_wy:.3f} rest_wy={rest_w:.3f} push_emitted={pose._push_emitted}{flag}")

cap.release()
print(f"\nLandmarks: {len(lms)}")
for lm in lms:
    print(f"  {lm.kind} f={lm.frame_seq} conf={lm.confidence:.2f}")
