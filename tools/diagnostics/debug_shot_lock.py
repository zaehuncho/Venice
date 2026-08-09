"""Debug why player lock fails on shot frames specifically.
Runs v9 detector + pose model on frames around a known shot window and reports
per-frame: v9 detection, pose detection, wrist visibility."""
import os, sys, cv2, numpy as np
os.environ.setdefault("YOLO_VERBOSE", "False")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ultralytics import YOLO
from meter_detector import MeterDetector, load_detector_config

VIDEO = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4"
START = int(sys.argv[2]) if len(sys.argv) > 2 else 3760
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 500

pose = YOLO("models/orion_pose2k_n_v2.pt")
pdet = YOLO("models/orion_player_detect_v9.pt")
mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
mcfg.meter_style = "Arrow2"; mcfg.meter_color = "Purple"; mcfg.auto_meter_color = False
meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg); meter.set_active_style("Arrow2")

cap = cv2.VideoCapture(VIDEO)
cap.set(cv2.CAP_PROP_POS_FRAMES, START)
print(f"Debugging {os.path.basename(VIDEO)} frames {START}..{START+COUNT}")

for n in range(COUNT):
    ok, frame = cap.read()
    if not ok: break
    seq = START + n
    H, W = frame.shape[:2]

    # Meter
    mr = meter.detect(frame)
    mdet = bool(mr.detected and mr.rejection_reason in ("", "green_not_found"))

    # v9
    pr = pdet.predict(frame, verbose=False, conf=0.25, imgsz=640)[0]
    pcls = pr.boxes.cls.cpu().numpy().astype(int) if pr.boxes is not None and len(pr.boxes) else np.array([], dtype=int)
    pxyxy = pr.boxes.xyxy.cpu().numpy() if pr.boxes is not None and len(pr.boxes) else np.array([]).reshape(0, 4)
    n_player = int((pcls == 0).sum())
    n_stamina = int((pcls == 1).sum())

    # Pose on full frame
    rr = pose.predict(frame, verbose=False, conf=0.10)[0]
    n_person = int(len(rr.boxes)) if rr.boxes is not None else 0

    # Wrist visibility
    wrist_vis = False
    if n_person > 0:
        kp = rr.keypoints.data.cpu().numpy()[0]
        rw = kp[10]  # right wrist
        lw = kp[9]   # left wrist
        wrist_vis = rw[2] >= 0.30 or lw[2] >= 0.30

    flag = " *** SHOT ***" if mdet else ""
    print(f"f={seq} meter={int(mdet)} v9_player={n_player} v9_stamina={n_stamina} "
          f"pose_persons={n_person} wrist_vis={int(wrist_vis)}{flag}")

cap.release()
