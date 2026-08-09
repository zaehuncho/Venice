"""Quick comparison probe: YOLO vs RF-DETR on the same video."""
import cv2, time, numpy as np
from ultralytics import YOLO

VIDEO = r"C:\Users\Administrator\Videos\2026-06-28 23-02-38.mp4"
CONF = 0.15
N_FRAMES = 600

def probe_yolo(model_path, conf, n_frames, label):
    m = YOLO(model_path)
    cap = cv2.VideoCapture(VIDEO)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"\n=== {label} (conf={conf}) ===")
    print(f"  video: {total} frames @ {fps:.0f}fps")

    lats = []
    dets = 0
    processed = 0
    confs = []
    step = max(1, total // n_frames)

    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue
        t0 = time.time()
        r = m.predict(frame, verbose=False, conf=conf, imgsz=640)[0]
        dt = (time.time() - t0) * 1000
        lats.append(dt)
        processed += 1
        if r.boxes is not None and len(r.boxes) > 0:
            cls = r.boxes.cls.cpu().numpy().astype(int)
            if 0 in cls:
                dets += 1
            confs.extend(r.boxes.conf.cpu().numpy().tolist())
        if processed % 50 == 0:
            print(f"  [{processed}/{n_frames}] player_rate={100*dets/processed:.0f}% avg_lat={np.mean(lats):.1f}ms")

    cap.release()
    lats = np.array(lats)
    print(f"\n  Frames processed:  {processed}")
    print(f"  Player detected:   {dets} ({100*dets/processed:.0f}%)")
    print(f"  Inference latency: avg={np.mean(lats):.1f}ms  p50={np.percentile(lats,50):.1f}ms  p95={np.percentile(lats,95):.1f}ms")
    print(f"  Achievable FPS:    {1000/np.mean(lats):.0f}")
    if confs:
        print(f"  Confidence: mean={np.mean(confs):.2f}  med={np.median(confs):.2f}  min={np.min(confs):.2f}  max={np.max(confs):.2f}")
    return dets, processed, np.mean(lats)

if __name__ == "__main__":
    probe_yolo("models/orion_player_detect_v9.pt", CONF, N_FRAMES, "YOLO orion_player_detect_v9.pt")
