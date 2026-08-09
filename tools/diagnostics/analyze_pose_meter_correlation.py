#!/usr/bin/env python3
"""Step 2 value-gate: does the pose anchor fire at a CONSISTENT point in the meter rise?

Runs BOTH the pose model (release = shooting-wrist raise peak) and the meter detector over a 2K
recording, aligns them per frame, and for each detected shot measures the **meter fill at the
pose-release moment**. If that fill clusters tightly across shots (low std), the pose landmark is a
steady phase reference -> a fixed pose-release->tip offset times the shot, beating the noisy
meter-appear anchor (which scatters because detection locks on at 34-97% fill). See
docs/ANIMATION_ANCHOR.md.

Usage:
  C:\\Python314\\python.exe tools/diagnostics/analyze_pose_meter_correlation.py "C:\\...\\<rec>.mp4" \
      [--pose models/orion_pose2k_n.pt] [--frames 4000] [--meter-color Purple]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# meter_detector.py lives at the repo root; add it to sys.path so this works when invoked as
# `python tools/diagnostics/analyze_pose_meter_correlation.py` (script-dir, not cwd, is on the path).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--pose", default="models/orion_pose2k_n.pt")
    ap.add_argument("--frames", type=int, default=4000)
    ap.add_argument("--meter-color", default="Purple")
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--embed-crop", action="store_true",
                    help="crop the Orion app embed (game sub-region) first, for live-batch recordings")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    try:
        import cv2
        from ultralytics import YOLO
        from meter_detector import MeterDetector, DetectorConfig
    except Exception as exc:
        print(f"need opencv + ultralytics + meter_detector: {exc}")
        return 2
    pose_model = args.pose if os.path.isfile(args.pose) else "yolov8x-pose.pt"
    print(f"pose={pose_model}  meter_color={args.meter_color}")
    pose = YOLO(pose_model)
    cfg = DetectorConfig()
    cfg.meter_color = args.meter_color
    styles = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "meter_styles")
    det = MeterDetector(styles, cfg)

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = 2 if fps > 80 else 1
    emb = None
    if args.embed_crop:
        ok0, f0 = cap.read()
        if ok0:
            g = cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY)
            mk = (g > 55).astype("uint8"); mk[:int(f0.shape[0] * 0.06), :] = 0; mk[int(f0.shape[0] * 0.94):, :] = 0
            c = max(cv2.findContours(mk, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea)
            emb = cv2.boundingRect(c)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        print(f"embed-crop: {emb}")
    print(f"fps={fps:.0f} frames={total} (step={step})")

    rows = []  # (t, wristY_norm, meter_fill or nan, meter_detected)
    proc = i = 0
    while proc < args.frames:
        if not cap.grab():
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            if emb is not None:
                ex, ey, ew, eh = emb
                fr = fr[ey:ey + eh, ex:ex + ew]
            proc += 1
            H = fr.shape[0]
            # METER FIRST -- it localizes the USER's player: 2K draws the shot meter only at the
            # user's shooter, so the user's player is whichever detected person is nearest the meter
            # (robust in real games with 9 other players; no on-screen calibration needed).
            mres = det.detect(fr)
            mdet = bool(mres and getattr(mres, "detected", False))
            mfill = float(getattr(mres, "fill_pct", 0.0) or 0.0) if mdet else 0.0
            mbb = (getattr(mres, "bbox", None) if mdet else None)
            # pose: pick the person NEAREST the meter (= the user's player), else highest-conf.
            r = pose.predict(fr, verbose=False, conf=args.conf)[0]
            swy = np.nan
            if r.boxes is not None and len(r.boxes):
                xywh = r.boxes.xywh.cpu().numpy()
                if mbb and len(mbb) >= 4 and mbb[2] > 0:
                    mcx, mcy = mbb[0] + mbb[2] / 2.0, mbb[1] + mbb[3] / 2.0
                    b = int(np.argmin([(xywh[p, 0] - mcx) ** 2 + (xywh[p, 1] - mcy) ** 2
                                       for p in range(len(xywh))]))
                else:
                    b = int(np.argmax(r.boxes.conf.cpu().numpy()))
                kp = r.keypoints.data.cpu().numpy()[b]
                wy = [kp[j, 1] for j in (9, 10) if kp[j, 2] > 0.3]
                if wy:
                    swy = min(wy) / H
            rows.append((i / fps, swy, mfill if mdet else np.nan, mdet))
        i += 1
    cap.release()

    A = rows
    mdet_rate = 100 * sum(1 for _ in A if _[3]) / max(len(A), 1)
    wvalid = [r for r in A if not np.isnan(r[1])]
    print(f"processed={len(A)}  pose-wrist frames={len(wvalid)}  meter-detected={mdet_rate:.0f}%")
    if len(wvalid) < 30:
        print("too few pose frames to analyze")
        return 1

    t = np.array([r[0] for r in A])
    wy = np.array([r[1] for r in A])
    mf = np.array([r[2] for r in A])
    # pose-release events: wrist-Y minima (hand highest), >0.8s apart
    valid = ~np.isnan(wy)
    rest = np.nanmedian(wy)
    events = []
    idx = np.where(valid)[0]
    for p in range(2, len(idx) - 2):
        k = idx[p]
        if (wy[k] < rest - 0.10 and wy[k] <= wy[idx[p - 1]] and wy[k] <= wy[idx[p + 1]]):
            if not events or (t[k] - t[events[-1]]) > 0.8:
                events.append(k)
    print(f"pose-release events: {len(events)}")

    fills_at_release = []
    for k in events:
        # nearest meter fill within +-0.15s of the pose release
        lo, hi = t[k] - 0.15, t[k] + 0.15
        win = [mf[j] for j in range(len(t)) if lo <= t[j] <= hi and not np.isnan(mf[j])]
        if win:
            fills_at_release.append(float(np.median(win)))
    print(f"shots with a meter fill at the pose release: {len(fills_at_release)}")
    if len(fills_at_release) >= 5:
        arr = np.array(fills_at_release)
        print(f"\n=== VALUE GATE: meter fill % at the pose-release landmark ===")
        print(f"  n={len(arr)}  mean={arr.mean():.1f}%  std={arr.std():.1f}%  "
              f"min={arr.min():.0f}  max={arr.max():.0f}")
        print(f"  (LOW std => the pose landmark fires at a consistent meter phase => usable anchor.")
        print(f"   Compare to the meter-appear anchor: detection locks on at 34-97% fill = ~huge std.)")
        verdict = ("STRONG (tight) -- pose anchor likely beats meter-appear" if arr.std() < 8 else
                   "MODERATE -- promising but tune detector/region" if arr.std() < 15 else
                   "WEAK -- pose-release fill scatters; not a better anchor as-is")
        print(f"  VERDICT: {verdict}")
    else:
        print("not enough paired shots (meter may not detect well on this recording's geometry)")

    # BETTER metric for no-meter mode: TIME from the jump-start landmark to the METER PEAK (tip).
    # Avoids the post-release deflation that confounds fill-at-release. A steady offset (low ms std)
    # => fire release at jumpStart + offset to hit the tip (green).
    offs = []
    for k in events:
        pk_pos = int(np.searchsorted(idx, k))
        js = k
        for q in range(pk_pos, -1, -1):                 # walk back to the start of the wrist raise
            if wy[idx[q]] >= rest - 0.05:
                js = idx[q]; break
        lo, hi = t[k] - 0.30, t[k] + 0.20            # TIGHT window so we don't grab a neighbor shot's meter
        cand = [(mf[j], t[j]) for j in range(len(t)) if lo <= t[j] <= hi and not np.isnan(mf[j])]
        if not cand:
            continue
        peak_fill, peak_t = max(cand, key=lambda c: c[0])
        if peak_fill < 85:                              # require a REAL tip (not partial/deflated)
            continue
        off = (peak_t - t[js]) * 1000.0
        if not (250 <= off <= 1100):                    # plausible jumpshot duration; reject mispairs
            continue
        offs.append(off)
    if len(offs) >= 5:
        a = np.array(offs)
        print(f"\n=== NO-METER OFFSET: jump-start -> meter-tip (ms) ===")
        print(f"  n={len(a)}  mean={a.mean():.0f}ms  std={a.std():.0f}ms  [{a.min():.0f},{a.max():.0f}]")
        print(f"  (no-meter mode would fire release at jumpStart + ~{a.mean():.0f}ms; std vs the ~30ms green window is the gate)")
        v = ("STRONG (std < 20ms) -- no-meter timing viable" if a.std() < 20 else
             "MODERATE (std 20-35ms) -- viable with a slower jumpshot / refinement" if a.std() < 35 else
             "WEAK (std > 35ms) -- landmark or meter-peak too noisy")
        print(f"  VERDICT: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
