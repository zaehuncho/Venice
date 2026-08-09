"""Does FUSING multiple pose cues tighten the release estimate below any single cue's ~200ms IQR?
Captures wrist/hip/knee from PoseTimingDetector, computes several cues per shot, calibrates each to
its median offset, and fuses (median + mean of calibrated cues). If fused IQR < best single cue,
fusion helps and we take it.
Usage: python cue_fusion_probe.py "<video>" [count] [start] [handed]  (env METER_COLOR)
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
WY, HY, KY = [], [], []
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
    j = (pose._buf_idx - 1) % pose._max_buf
    WY.append(float(pose._wrist_y[j])); HY.append(float(pose._hip_y[j])); KY.append(float(pose._knee_y[j]))
    if fed and not prev and (seq - last) > DEBOUNCE:
        shot_edges.append(n); last = seq
    prev = fed; n += 1
cap.release()


def prep(a):
    a = np.array(a); m = ~np.isnan(a)
    if m.sum() > 10:
        a = np.interp(np.arange(len(a)), np.where(m)[0], a[m])
    a = np.convolve(a, [0.25, 0.5, 0.25], mode="same")
    return a, np.gradient(a), np.gradient(np.gradient(a))


wy, wv, wa = prep(WY); hy, hv, ha = prep(HY)
ms = 1000.0 / fps
cues = {"w_velpeak": [], "w_apex": [], "w_accelpeak": [], "w_snap": [], "hip_apex": []}
W = 30
for e in shot_edges:
    lo, hi = e, min(len(wy), e + W)
    if hi - lo < 6:
        continue
    apex = lo + int(np.argmin(wy[lo:hi]))
    snap = apex + (int(np.argmax(wv[apex:hi])) if apex < hi - 1 else 0)
    cues["w_velpeak"].append((lo + int(np.argmin(wv[lo:hi])) - e) * ms)
    cues["w_apex"].append((apex - e) * ms)
    cues["w_accelpeak"].append((lo + int(np.argmax(np.abs(wa[lo:hi]))) - e) * ms)
    cues["w_snap"].append((snap - e) * ms)
    cues["hip_apex"].append((lo + int(np.argmin(hy[lo:hi])) - e) * ms)


def iqr(a):
    a = np.array(sorted(a)); return np.percentile(a, 75) - np.percentile(a, 25)


names = list(cues)
nrec = len(cues["w_apex"])
print(f"VIDEO {os.path.basename(VIDEO)} shots={nrec}")
meds = {}
for nm in names:
    a = np.array(cues[nm]); meds[nm] = np.median(a)
    print(f"  [{nm:12s}] median={np.median(a):+5.0f}ms IQR={iqr(a):4.0f}ms ±40={sum(1 for x in a if abs(x-np.median(a))<=40)}/{nrec}")
cal = np.array([[cues[nm][i] - meds[nm] for nm in names] for i in range(nrec)])
for label, fused in (("median", np.median(cal, axis=1)), ("mean", np.mean(cal, axis=1))):
    print(f"  [FUSED {label:6s}] IQR={iqr(fused):4.0f}ms ±40={sum(1 for x in fused if abs(x)<=40)}/{nrec} ({100*sum(1 for x in fused if abs(x)<=40)//max(nrec,1)}%)")
