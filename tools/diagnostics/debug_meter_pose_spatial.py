"""Debug: show meter bbox position vs pose detections to understand spatial relationship."""
import os, sys, cv2, numpy as np
os.environ.setdefault("YOLO_VERBOSE", "False")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ultralytics import YOLO
from meter_detector import MeterDetector, load_detector_config

VIDEO = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4"
START = int(sys.argv[2]) if len(sys.argv) > 2 else 4192
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 20

pose = YOLO("models/orion_pose2k_n_v2.pt")
mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
mcfg.meter_style = "Arrow2"; mcfg.meter_color = "Purple"; mcfg.auto_meter_color = False
meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg); meter.set_active_style("Arrow2")

cap = cv2.VideoCapture(VIDEO)
cap.set(cv2.CAP_PROP_POS_FRAMES, START)

for n in range(COUNT):
    ok, frame = cap.read()
    if not ok: break
    seq = START + n
    H, W = frame.shape[:2]

    mr = meter.detect(frame)
    mdet = bool(mr.detected and mr.rejection_reason in ("", "green_not_found"))
    mbbox = mr.bbox if mdet and mr.bbox else None

    rr = pose.predict(frame, verbose=False, conf=0.10)[0]
    persons = []
    if rr.boxes is not None and len(rr.boxes):
        xyxy = rr.boxes.xyxy.cpu().numpy()
        confs = rr.boxes.conf.cpu().numpy()
        kps = rr.keypoints.data.cpu().numpy()
        for p in range(len(xyxy)):
            cx = (xyxy[p, 0] + xyxy[p, 2]) / 2.0
            cy = (xyxy[p, 1] + xyxy[p, 3]) / 2.0
            rw = kps[p][10]  # right wrist
            lw = kps[p][9]   # left wrist
            rwc = f"{rw[2]:.2f}" if rw[2] >= 0.3 else "--"
            lwc = f"{lw[2]:.2f}" if lw[2] >= 0.3 else "--"
            rwy = f"{rw[1]/H:.3f}" if rw[2] >= 0.3 else "nan"
            lwy = f"{lw[1]/H:.3f}" if lw[2] >= 0.3 else "nan"
            persons.append((cx, cy, confs[p], rwy, lwy, rwc, lwc))

    mstr = f"meter=({mbbox[0]:.0f},{mbbox[1]:.0f},{mbbox[2]:.0f},{mbbox[3]:.0f})" if mbbox else "meter=NONE"
    print(f"\nf={seq} {mstr}")
    for i, (cx, cy, conf, rwy, lwy, rwc, lwc) in enumerate(persons):
        d = ""
        if mbbox:
            mcx = mbbox[0] + mbbox[2] / 2.0
            mcy = mbbox[1] + mbbox[3] / 2.0
            d = f" dist={np.sqrt((cx-mcx)**2+(cy-mcy)**2):.0f}"
        print(f"  p{i}: cx={cx:.0f} cy={cy:.0f} conf={conf:.2f} rwy={rwy}({rwc}) lwy={lwy}({lwc}){d}")

cap.release()
