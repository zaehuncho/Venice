"""Is the wrist FLICK localizable? Tests sharper release events than the smooth peak/apex:
  - accel_peak: max |wrist-Y acceleration| in the shot window (the snap)
  - snap_down: max DOWNWARD wrist velocity after the apex (follow-through)
  - apex: wrist-Y minimum (re-confirm)
Measures each event's offset vs the meter edge; tight IQR => localizable (closed-loop viable).
Captures the locked-player wrist-Y from PoseTimingDetector per frame (no detector change).
Usage: python flick_localize_probe.py "<video>" [count] [start] [handed]  (env METER_COLOR)
"""
import sys, os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ.setdefault("YOLO_VERBOSE", "False")
import numpy as np, cv2
from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

VIDEO = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "Videos" / "NBA 2K26_20260521032018.mp4")
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
START = int(sys.argv[3]) if len(sys.argv) > 3 else 11940
HANDED = sys.argv[4] if len(sys.argv) > 4 else "Right"

pose = PoseTimingDetector(handedness=HANDED)
mcfg = load_detector_config(os.path.join(".", "settings.json"))
mcfg.meter_style = "Arrow2"; mcfg.meter_color = os.environ.get("METER_COLOR", "Red"); mcfg.auto_meter_color = False
meter = MeterDetector("meter_styles", mcfg); meter.set_active_style("Arrow2")

cap = cv2.VideoCapture(VIDEO)
if START:
    cap.set(cv2.CAP_PROP_POS_FRAMES, START)
fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
wy = []          # per-frame locked wrist-Y (NaN if none)
shot_edges = []
prev = False; last = -10 ** 9; DEBOUNCE = 18; n = 0
while n < COUNT:
    ok, fr = cap.read()
    if not ok:
        break
    seq = START + n
    r = meter.detect(fr)
    fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
    if fed and getattr(r, "bbox", None) and len(r.bbox) >= 4 and r.bbox[2] > 0:
        bx, by, bw, bh = r.bbox
        pose.set_player_hint((bx, by, bx + bw, by + bh))
    try:
        pose.update(fr, seq, frame_time=seq / fps)
    except Exception:
        pass
    wy.append(float(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]))
    if fed and not prev and (seq - last) > DEBOUNCE:
        shot_edges.append(n); last = seq
    prev = fed; n += 1
cap.release()

wy = np.array(wy)
# fill + light smooth
mask = ~np.isnan(wy)
if mask.sum() > 10:
    wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])
k = np.array([0.25, 0.5, 0.25])
wy = np.convolve(wy, k, mode="same")
vel = np.gradient(wy)
acc = np.gradient(vel)
ms = 1000.0 / fps

events = {"apex": [], "snap_down": [], "accel_peak": []}
W = 30
for e in shot_edges:
    lo, hi = e, min(len(wy), e + W)   # event is at/after the edge (during the rise)
    if hi - lo < 6:
        continue
    seg_wy, seg_vel, seg_acc = wy[lo:hi], vel[lo:hi], acc[lo:hi]
    apex = lo + int(np.argmin(seg_wy))                       # wrist highest
    accel_peak = lo + int(np.argmax(np.abs(seg_acc)))        # sharpest snap
    a_rel = apex - lo
    snap = apex + int(np.argmax(seg_vel[a_rel:])) if a_rel < len(seg_vel) - 1 else apex  # max downward vel after apex
    events["apex"].append((apex - e) * ms)
    events["snap_down"].append((snap - e) * ms)
    events["accel_peak"].append((accel_peak - e) * ms)

print(f"VIDEO {os.path.basename(VIDEO)} {START}..{START+n}  shots={len(shot_edges)}")
for name, offs in events.items():
    if not offs:
        continue
    a = np.array(sorted(offs))
    med = np.median(a)
    inwin = sum(1 for x in offs if abs(x - med) <= 40)
    print(f"[{name:11s}] n={len(offs)} median={med:+.0f}ms IQR={np.percentile(a,75)-np.percentile(a,25):.0f}ms "
          f"±40ms={inwin}/{len(offs)} ({100*inwin//max(len(offs),1)}%)")
