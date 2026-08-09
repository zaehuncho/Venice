#!/usr/bin/env python3
"""Multi-keypoint jumpshot phase analyzer for skeleton timing.

Extracts full-body keypoint trajectories from 2K recordings and detects animation PHASES:
  gather -> jump-start -> set-point -> push -> release

Pairs each landmark with the meter to measure:
  1. meter-fill-% at each landmark (consistency gate)
  2. landmark -> meter-tip offset in ms (no-meter calibration)

Supports multiple recordings in one run. Uses meter-nearest player lock.

Usage:
  C:\\Python314\\python.exe tools\\diagnostics\\pose_phase_analyzer.py \\
      "C:\\Users\\Administrator\\Videos\\vid1.mp4" \\
      "C:\\Users\\Administrator\\Videos\\vid2.mp4" \\
      [--model models/orion_pose2k_n.pt] [--meter-color Purple] \\
      [--frames 4000] [--csv] [--out logs/diagnostics/phase_analysis]

COCO: 5/6=shoulders 7/8=elbows 9/10=wrists 11/12=hips 13/14=knees 15/16=ankles
  Y: 0=top, 1=bottom (up = negative Y)
"""
from __future__ import annotations
import argparse, csv as _csv, os, sys
import numpy as np

KP_HIP_L, KP_HIP_R = 11, 12
KP_KNEE_L, KP_KNEE_R = 13, 14
KP_SHOULDER_L, KP_SHOULDER_R = 5, 6
KP_WRIST_L, KP_WRIST_R = 9, 10
KP_CONF_FLOOR = 0.30


def _safe_y(kp, idx, H):
    if kp is None or idx >= len(kp): return float("nan")
    x, y, c = kp[idx]
    return float(y) / H if c >= KP_CONF_FLOOR else float("nan")


def _ema(arr, a=0.4):
    out = np.copy(arr)
    for i in range(1, len(out)):
        if not np.isnan(out[i]) and not np.isnan(out[i-1]):
            out[i] = out[i-1]*(1-a) + out[i]*a
    for i in range(len(out)-2, -1, -1):
        if not np.isnan(out[i]) and not np.isnan(out[i+1]):
            out[i] = out[i+1]*(1-a) + out[i]*a
    return out


def _ema_tau(arr, tau_ms: float, dt_ms: float):
    """Framerate-aware EMA. tau_ms = desired time constant, dt_ms = frame interval."""
    a = 1.0 - np.exp(-dt_ms / max(tau_ms, 1.0))
    return _ema(arr, float(a))


def _fill_nans(arr):
    out = np.copy(arr); n = len(out); i = 0
    while i < n:
        if np.isnan(out[i]):
            j = i
            while j < n and np.isnan(out[j]): j += 1
            if i > 0 and j < n:
                lo, hi = out[i-1], out[j]
                for k in range(i, j): out[k] = lo + (hi-lo)*(k-i+1)/(j-i+1)
            elif i == 0 and j < n: out[:j] = out[j]
            elif j >= n and i > 0: out[i:] = out[i-1]
            i = j
        else: i += 1
    return out


def _detect_phases(t, hip_y, wrist_y, knee_y, dt_ms=16.7):
    """Detect release and push phases from wrist trajectory.

    RELEASE = wrist-Y local minimum (hand at highest point = ball release).
    PUSH = frame with strongest upward wrist velocity in the 70-180ms
    window before each release. A real release must be preceded by a push;
    this validates the release and rejects follow-through dips.

    Tight validation:
    - Release must be deep (rest-0.08) and a 7-frame local minimum.
    - Push velocity must be strongly upward (< -0.05).
    - Push-to-release timing must be 70-180ms (hard reject outside).
    - Wrist must have been near rest (within 0.05) in the 200-400ms before
      the push — rejects follow-through dips where wrist was already high.
    - Minimum 1.2s between accepted releases.

    Returns list of shot dicts with release_k, push_k, confidence.
    """
    wrist_vel = np.gradient(wrist_y, t)
    wrist_vel_s = _ema_tau(_fill_nans(wrist_vel), 50.0, dt_ms)
    rest_wrist = float(np.nanmedian(wrist_y))
    valid_w = ~np.isnan(wrist_y)
    idx_w = np.where(valid_w)[0]

    # --- Find all wrist-Y local minima (release candidates) ---
    releases = []
    for p in range(3, len(idx_w) - 3):
        k = idx_w[p]
        # Must be deep below rest (hand significantly raised)
        if wrist_y[k] >= rest_wrist - 0.10:
            continue
        # Local minimum in 7-frame window (robust to noise)
        is_min = True
        for q in range(max(0, p-3), min(len(idx_w), p+4)):
            if idx_w[q] != k and wrist_y[idx_w[q]] < wrist_y[k]:
                is_min = False
                break
        if is_min:
            releases.append(k)

    # --- For each release, find the PUSH and validate ---
    shots = []
    last_rel_time = -1e9
    for rel_k in releases:
        t_rel = t[rel_k]
        # Must be >1.5s from previous accepted release
        if t_rel - last_rel_time < 1.5:
            continue

        # PUSH: strongest upward wrist velocity in 80-160ms before release.
        push_lo_t = t_rel - 0.16
        push_hi_t = t_rel - 0.08
        push_k = None
        best_push_vel = 0.0  # most negative = strongest upward
        for p in range(len(idx_w)):
            k = idx_w[p]
            if t[k] < push_lo_t:
                continue
            if t[k] > push_hi_t:
                break
            if np.isnan(wrist_vel_s[k]):
                continue
            if wrist_vel_s[k] < best_push_vel:
                best_push_vel = wrist_vel_s[k]
                push_k = k

        # Reject if no push found or push velocity too weak
        if push_k is None or best_push_vel > -0.06:
            continue

        t_push = t[push_k]
        push_to_rel = (t_rel - t_push) * 1000.0

        # Hard reject implausible push-to-release timing
        if push_to_rel < 80 or push_to_rel > 160:
            continue

        # Wrist must have been near rest (within 0.05) in the 200-400ms
        # before the push. This rejects follow-through dips where the wrist
        # was already high (e.g. after a previous shot's release).
        pre_push_lo_t = t_push - 0.40
        pre_push_hi_t = t_push - 0.20
        was_near_rest = False
        for p in range(len(idx_w)):
            k = idx_w[p]
            if t[k] < pre_push_lo_t:
                continue
            if t[k] > pre_push_hi_t:
                break
            if not np.isnan(wrist_y[k]) and abs(wrist_y[k] - rest_wrist) < 0.04:
                was_near_rest = True
                break
        if not was_near_rest:
            continue

        # Post-release deceleration: wrist velocity must become positive
        # (downward) within 5 frames after release. A real release is
        # followed by the hand dropping (follow-through). A false release
        # (e.g. mid-shot pause) won't have this deceleration.
        has_decel = False
        rel_p = None
        for pi, pk in enumerate(idx_w):
            if pk == rel_k:
                rel_p = pi
                break
        if rel_p is not None:
            for qi in range(rel_p + 1, min(len(idx_w), rel_p + 6)):
                kq = idx_w[qi]
                if not np.isnan(wrist_vel_s[kq]) and wrist_vel_s[kq] > 0.02:
                    has_decel = True
                    break
        if not has_decel:
            continue

        # --- Confidence score (0-1) ---
        push_score = min(1.0, abs(best_push_vel) / 0.15)
        rel_depth = max(0.0, rest_wrist - wrist_y[rel_k])
        depth_score = min(1.0, rel_depth / 0.15)
        if 90 <= push_to_rel <= 150:
            timing_score = 1.0
        elif 80 <= push_to_rel <= 160:
            timing_score = 0.7
        else:
            timing_score = 0.3

        confidence = 0.45 * push_score + 0.35 * depth_score + 0.20 * timing_score

        shots.append({
            "release_k": rel_k, "push_k": push_k,
            "confidence": round(confidence, 3),
            "t_release": float(t[rel_k]),
            "t_push": float(t[push_k]),
            "t_jump_start": float("nan"),
            "t_set_point": float("nan"),
            "t_gather": float("nan"),
        })
        last_rel_time = t_rel

    return shots


def _meter_tip(t, mf, t_event, lo=-0.35, hi=0.25, min_fill=80.0):
    cand = [(mf[j], t[j]) for j in range(len(t))
            if t_event+lo <= t[j] <= t_event+hi and not np.isnan(mf[j])]
    if not cand: return None, None
    pf, pt = max(cand, key=lambda c: c[0])
    return (pf, pt) if pf >= min_fill else (None, None)


def _meter_appear(t, mf, t_event, lo=-0.6, hi=0.1):
    for j in range(len(t)):
        if t_event+lo <= t[j] <= t_event+hi and not np.isnan(mf[j]) and mf[j] > 5:
            return float(mf[j]), float(t[j])
    return None, None


def process_video(vpath, pose, mdet, args):
    import cv2
    cap = cv2.VideoCapture(vpath)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = args.step if args.step > 0 else (2 if fps > 80 else 1)
    print(f"\n{'='*60}\nVideo: {os.path.basename(vpath)}")
    print(f"  fps={fps:.0f} frames={total} step={step} dur={total/max(fps,1):.0f}s")

    emb = None
    if args.embed_crop:
        ok0, f0 = cap.read()
        if ok0:
            g = cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY)
            mk = (g > 55).astype("uint8")
            mk[:int(f0.shape[0]*0.06), :] = 0; mk[int(f0.shape[0]*0.94):, :] = 0
            cnts = cv2.findContours(mk, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
            if cnts: emb = cv2.boundingRect(max(cnts, key=cv2.contourArea))
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    rows = []; proc = i = 0
    while proc < args.frames:
        if not cap.grab(): break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if not ok: break
            if emb: fr = fr[emb[1]:emb[1]+emb[3], emb[0]:emb[0]+emb[2]]
            proc += 1; H = fr.shape[0]
            mres = mdet.detect(fr)
            mdet_b = bool(mres and getattr(mres, "detected", False))
            mfill = float(getattr(mres, "fill_pct", 0) or 0) if mdet_b else float("nan")
            mbb = getattr(mres, "bbox", None) if mdet_b else None
            r = pose.predict(fr, verbose=False, conf=args.conf)[0]
            hy = wy = ky = float("nan")
            if r.boxes is not None and len(r.boxes):
                xywh = r.boxes.xywh.cpu().numpy()
                if mbb and len(mbb) >= 4 and mbb[2] > 0:
                    mcx, mcy = mbb[0]+mbb[2]/2, mbb[1]+mbb[3]/2
                    b = int(np.argmin([(xywh[p,0]-mcx)**2+(xywh[p,1]-mcy)**2
                                       for p in range(len(xywh))]))
                else:
                    b = int(np.argmax(r.boxes.conf.cpu().numpy()))
                kp = r.keypoints.data.cpu().numpy()[b]
                hv = [_safe_y(kp, j, H) for j in (KP_HIP_L, KP_HIP_R)]
                hv = [v for v in hv if not np.isnan(v)]
                hy = sum(hv)/len(hv) if hv else float("nan")
                wv = [_safe_y(kp, j, H) for j in (KP_WRIST_L, KP_WRIST_R)]
                wv = [v for v in wv if not np.isnan(v)]
                wy = min(wv) if wv else float("nan")
                kv = [_safe_y(kp, j, H) for j in (KP_KNEE_L, KP_KNEE_R)]
                kv = [v for v in kv if not np.isnan(v)]
                ky = sum(kv)/len(kv) if kv else float("nan")
            rows.append((i/fps, hy, wy, ky, mfill, int(mdet_b), i))
        i += 1
    cap.release()
    if not rows: return {"shots": [], "n_rows": 0}

    t = np.array([r[0] for r in rows])
    dt_ms = 1000.0 / max(fps / step, 1.0)
    hip_s = _ema_tau(_fill_nans(np.array([r[1] for r in rows])), 80.0, dt_ms)
    wrist_s = _ema_tau(_fill_nans(np.array([r[2] for r in rows])), 80.0, dt_ms)
    knee_s = _ema_tau(_fill_nans(np.array([r[3] for r in rows])), 80.0, dt_ms)
    mf = np.array([r[4] for r in rows])
    mdet_arr = np.array([r[5] for r in rows])
    pose_n = sum(1 for r in rows if not np.isnan(r[1]))
    print(f"  processed={len(rows)} pose={pose_n} meter={100*mdet_arr.sum()/max(len(rows),1):.0f}%")

    shots = _detect_phases(t, hip_s, wrist_s, knee_s, dt_ms=dt_ms)
    print(f"  shots={len(shots)}")
    vname = os.path.basename(vpath)
    for s in shots:
        s["video"] = vname
        tf, tt = _meter_tip(t, mf, s["t_release"])
        s["meter_tip_t"] = tt; s["meter_tip_fill"] = tf
        af, at = _meter_appear(t, mf, s["t_release"])
        s["meter_appear_t"] = at
        if tt is not None:
            for lk, tk in [("push","t_push"),("release","t_release")]:
                pt = s[tk]
                s[f"offset_{lk}_ms"] = (tt-pt)*1000 if not np.isnan(pt) else float("nan")
            s["offset_meter_appear_ms"] = (tt-at)*1000 if at else float("nan")
        else:
            for lk in ("push","release","meter_appear"):
                s[f"offset_{lk}_ms"] = float("nan")
        for pk, tk in [("push","t_push"),("release","t_release")]:
            pt = s[tk]
            if np.isnan(pt): s[f"fill_at_{pk}"] = float("nan"); continue
            lo, hi = pt-0.10, pt+0.10
            win = [mf[j] for j in range(len(t)) if lo<=t[j]<=hi and not np.isnan(mf[j])]
            s[f"fill_at_{pk}"] = float(np.median(win)) if win else float("nan")
    return {"shots": shots, "n_rows": len(rows)}


def report(all_shots, csv_path=None):
    print(f"\n{'='*60}\nCALIBRATION — {len(all_shots)} shots\n{'='*60}")
    cal = [s for s in all_shots if s.get("meter_tip_t") is not None]
    print(f"Calibrated (meter tip >= 80): {len(cal)}")
    if len(cal) < 5: print("Need >= 5."); return

    lms = [("push","offset_push_ms","Push",50,1200),
           ("release","offset_release_ms","Release",-200,200),
           ("meter_appear","offset_meter_appear_ms","Meter-Appear",-100,800)]

    print(f"\n--- Landmark -> Meter-Tip offset (ms) ---")
    print(f"{'Landmark':<16} {'n':>4} {'mean':>8} {'std':>8} {'min':>8} {'max':>8}  verdict     {'n*':>4} {'std*':>8}  verdict*    {'conf>=0.7':>10} {'std_c':>8}")
    print("-"*105)
    best_l = None; best_s = 1e9
    # Confidence-filtered shots
    cal_conf = [s for s in cal if s.get("confidence", 0) >= 0.7]
    for key, ok, label, lo, hi in lms:
        vals = [s[ok] for s in cal if not np.isnan(s[ok]) and lo <= s[ok] <= hi]
        if len(vals) < 5: print(f"{label:<16} {len(vals):>4}  --- insufficient ---"); continue
        a = np.array(vals)
        v = "STRONG" if a.std()<20 else "MODERATE" if a.std()<35 else "WEAK"
        # MAD-based outlier rejection
        med = float(np.median(a))
        mad = float(np.median(np.abs(a - med))) * 1.4826
        if mad > 0:
            inliers = a[np.abs(a - med) <= 2.0 * mad]
        else:
            inliers = a
        if len(inliers) < 5:
            inliers = a
        v2 = "STRONG" if inliers.std()<20 else "MODERATE" if inliers.std()<35 else "WEAK"
        # Confidence-filtered
        cvals = [s[ok] for s in cal_conf if not np.isnan(s[ok]) and lo <= s[ok] <= hi]
        if len(cvals) >= 5:
            ca = np.array(cvals)
            v3 = "STRONG" if ca.std()<20 else "MODERATE" if ca.std()<35 else "WEAK"
            conf_str = f"{len(ca):>4} {ca.std():>8.0f}  {v3}"
        else:
            conf_str = f"{len(cvals):>4} {'---':>8}  ---"
        print(f"{label:<16} {len(a):>4} {a.mean():>8.0f} {a.std():>8.0f} {a.min():>8.0f} {a.max():>8.0f}  {v:<10} {len(inliers):>4} {inliers.std():>8.0f}  {v2:<10}  {conf_str}")
        if inliers.std() < best_s and key != "meter_appear": best_s = inliers.std(); best_l = label

    if best_l:
        print(f"\n>>> BEST ANCHOR: {best_l} (std*={best_s:.0f}ms, MAD-cleaned)")
        print(f"    Green window ~30ms -> {'VIABLE' if best_s<30 else 'TOO NOISY'}")
        print(f"    Confidence>=0.7 shots: {len(cal_conf)}/{len(cal)}")

    print(f"\n--- Meter fill % at each landmark ---")
    print(f"{'Landmark':<16} {'n':>4} {'mean':>8} {'std':>8}")
    print("-"*40)
    for key, _, label, _, _ in lms:
        vals = [s.get(f"fill_at_{key}",float("nan")) for s in cal]
        vals = [v for v in vals if not np.isnan(v)]
        if len(vals) < 5: print(f"{label:<16} {len(vals):>4}  ---"); continue
        a = np.array(vals)
        print(f"{label:<16} {len(a):>4} {a.mean():>8.1f} {a.std():>8.1f}")

    print(f"\n--- Per-shot detail ---")
    print(f"{'#':>3} {'video':<24} {'Push':>7} {'Rel':>7} {'MAp':>7} {'conf':>6}")
    print("-"*60)
    for n, s in enumerate(cal):
        print(f"{n+1:>3} {s.get('video','?')[:24]:<24} "
              f"{s.get('offset_push_ms',float('nan')):>7.0f} "
              f"{s.get('offset_release_ms',float('nan')):>7.0f} "
              f"{s.get('offset_meter_appear_ms',float('nan')):>7.0f} "
              f"{s.get('confidence',0):>6.2f}")

    if csv_path and cal:
        with open(csv_path, "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["video","shot","t_push","t_release",
                        "tip_t","tip_fill","off_push","off_rel","off_map",
                        "fill_push","fill_rel","confidence"])
            for n, s in enumerate(cal):
                row = [s.get("video",""), n+1, f"{s['t_push']:.3f}", f"{s['t_release']:.3f}"]
                row.append(f"{s['meter_tip_t']:.3f}" if s['meter_tip_t'] else "")
                row.append(f"{s['meter_tip_fill']:.1f}" if s['meter_tip_fill'] else "")
                for k in ("offset_push_ms","offset_release_ms","offset_meter_appear_ms"):
                    v = s.get(k, float("nan"))
                    row.append(f"{v:.1f}" if not np.isnan(v) else "")
                for k in ("fill_at_push","fill_at_release"):
                    v = s.get(k, float("nan"))
                    row.append(f"{v:.1f}" if not np.isnan(v) else "")
                row.append(f"{s.get('confidence',0):.3f}")
                w.writerow(row)
        print(f"\nCSV: {csv_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--model", default="models/orion_pose2k_n.pt")
    ap.add_argument("--meter-color", default="Purple")
    ap.add_argument("--frames", type=int, default=4000)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--out", default="logs/diagnostics/phase_analysis")
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--embed-crop", action="store_true")
    ap.add_argument("--step", type=int, default=0, help="frame step (0=auto: 2 if >80fps else 1)")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _root not in sys.path: sys.path.insert(0, _root)
    try:
        import cv2
        from ultralytics import YOLO
        from meter_detector import MeterDetector, DetectorConfig
    except Exception as exc:
        print(f"need opencv + ultralytics + meter_detector: {exc}"); return 2

    pmp = args.model if os.path.isfile(args.model) else "yolov8x-pose.pt"
    print(f"Pose: {pmp}")
    pose = YOLO(pmp)
    cfg = DetectorConfig(); cfg.meter_color = args.meter_color
    sd = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "meter_styles")
    mdet = MeterDetector(sd, cfg)

    os.makedirs(args.out, exist_ok=True)
    all_shots = []
    for vp in args.videos:
        if not os.path.isfile(vp): print(f"skip (not found): {vp}"); continue
        res = process_video(vp, pose, mdet, args)
        all_shots.extend(res["shots"])

    csv_path = os.path.join(args.out, "phase_calibration.csv") if args.csv else None
    report(all_shots, csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
