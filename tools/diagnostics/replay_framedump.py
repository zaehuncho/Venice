#!/usr/bin/env python
"""
replay_framedump.py  --  deterministic replay / verification harness for the
capture-card framedump sessions (logs/diagnostics/framedump/session_*).

WHY: last night's live session (session_20260704_210801) dumped the RAW frames the
sidecar detector actually saw (fNNNNN_D_raw.png, D = live `detected` flag). Several
Fade shots failed with a persistent reject='roi_not_found' mid-shot. This harness
re-runs meter_detector.MeterDetector over those exact frames -- built EXACTLY as the
live sidecar builds it (remote_play_orchestrator.py lines ~380-408 + the
autogreen_sidecar update_meter('Arrow2') path) -- so a fix can be validated offline,
deterministically, against the live ground truth.

GROUND TRUTH SOURCES (read-only):
  * The framedump filename flag  fNNNNN_D  -> D is the LIVE detector's `detected`
    for that exact frame. => a perfect per-frame replay-vs-live comparison (same
    pixels in, compare `detected`).
  * logs/diagnostics/detframes.csv (the UNROTATED latest = this session) carries a
    `wall_ms` epoch column per processed frame => the frame-index<->wallclock anchor.

This file writes ONLY under tools/diagnostics/ (itself) and logs/diagnostics/.
It does NOT modify meter_detector.py or any existing source/test.

Usage examples:
  py replay_framedump.py --calibrate-only
  py replay_framedump.py --start 0 --end 1326 --step 1
  py replay_framedump.py --window seq12 --step 1 --annotate 12
"""
import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys
import time as _time

# --------------------------------------------------------------------------- #
# 0. Repo + the EXACT session locator env, set BEFORE importing meter_detector.
#    meter_locator_infer / meter_box_kalman read these at construction time;
#    meter_detector module-level flags read at import. Setting here covers both.
#    (Source: live_today.log line 28 "Sidecar env: ...")
# --------------------------------------------------------------------------- #
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)   # meter_detector / meter_locator_infer live at the repo root

_SESSION_ENV = {
    "ORION_METER_LOCATOR": "1",
    "ORION_METER_LOCATOR_CONF": "0.45",
    "ORION_METER_LOCATOR_EVERY": "1",
    "ORION_METER_LOCATOR_IMGSZ": "960",
    "ORION_METER_LOCATOR_MODEL": os.path.join(REPO, "models", "orion_meter_n_v6_mycourt960.pt"),
    "ORION_METER_TRACK": "1",
    # park temporal defaults ON (ORION_PARK_TEMPORAL unset in the session); pin it
    # explicitly so the replay is not sensitive to a stray shell env.
    "ORION_PARK_TEMPORAL": "1",
}
for _k, _v in _SESSION_ENV.items():
    os.environ[_k] = _v          # force the session values (authoritative)
# Never let the replay itself dump frames or write the live detframes csv.
os.environ.pop("ORION_FRAMEDUMP", None)
os.environ["ORION_DETCSV"] = "0"

import numpy as np               # noqa: E402
import cv2                       # noqa: E402

# --------------------------------------------------------------------------- #
# 1. Synthetic per-frame clock.  The detector uses time.perf_counter() for all of
#    its temporal logic (rise-state, warm-reacq 1.2s, box-track hold 3.0s,
#    _RECENT_PROX_S 0.5s). Replaying back-to-back on a CPU-bound YOLO would make the
#    inter-frame wallclock = inference time (~100s of ms), NOT the ~0.033s live
#    spacing -> time-based holds would expire in the wrong number of frames. So we
#    freeze perf_counter to a fake clock that advances by exactly `dt` per frame
#    (one frame == one instant). Deterministic + faithful to the intended spacing.
# --------------------------------------------------------------------------- #
_REAL_PERF = _time.perf_counter          # keep the real clock for our own timing


class _FrameClock:
    def __init__(self, dt, t0=100000.0):
        self._t = float(t0)
        self._dt = float(dt)

    def tick(self):
        self._t += self._dt

    def now(self):
        return self._t


_CLOCK = _FrameClock(dt=0.0334)


def _install_clock():
    _time.perf_counter = _CLOCK.now
    _time.monotonic = _CLOCK.now


# --------------------------------------------------------------------------- #
# 2. Build the detector EXACTLY as the live sidecar does.
# --------------------------------------------------------------------------- #
def build_detector(style="Arrow2", confidence_gate=0.32):
    """Mirror remote_play_orchestrator._build (lines 380-408) + update_meter(style)."""
    from meter_detector import MeterDetector, DetectorConfig

    cfg = DetectorConfig()
    cfg.meter_color = "Red"               # park pins Red (orch 398 + detect() 4108)
    cfg.confidence_threshold = confidence_gate   # = OrchestratorConfig.confidence_gate (0.32)
    cfg.park_temporal_enabled = True      # ORION_PARK_TEMPORAL default '1'
    cfg.auto_meter_color = False          # orch 399
    styles_dir = os.path.join(REPO, "meter_styles")
    det = MeterDetector(styles_dir, cfg)
    # Live path reaches set_active_style('Arrow2') via autogreen_sidecar ->
    # orch.update_meter(meter_style='Arrow2'); every diagnostic harness does the same.
    det.set_active_style(style)

    loc = getattr(det, "_locator", None)
    info = {
        "locator_loaded": bool(loc is not None and getattr(loc, "enabled", True)),
        "locator_device": str(getattr(loc, "_device", "-")),
        "locator_imgsz": getattr(loc, "_imgsz", "-"),
        "locator_conf": getattr(loc, "_conf", "-"),
        "locator_every": getattr(loc, "_every", "-"),
        "box_track": getattr(det, "_box_track", None) is not None,
        "reader": getattr(det, "_reader", None) is not None,
        "park_temporal": cfg.park_temporal_enabled,
        "meter_color": cfg.meter_color,
        "style": style,
    }
    return det, info


# --------------------------------------------------------------------------- #
# 3. Framedump discovery + filename flag parsing.
# --------------------------------------------------------------------------- #
_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")


def list_frames(session_dir):
    """Return dict idx -> (path, live_detected_flag) for *_raw.png."""
    out = {}
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out[int(m.group(1))] = (p, int(m.group(2)))
    return out


# --------------------------------------------------------------------------- #
# 4. detframes.csv (live per-frame log w/ wall_ms) -> wallclock anchor.
# --------------------------------------------------------------------------- #
def load_detframes(csv_path):
    import csv
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "wall_ms": float(r["wall_ms"]),
                    "detected": int(r["detected"]),
                    "fill": float(r["fill_pct"]),
                    "reject": r.get("rejection_reason", ""),
                    "x": int(r["x"]), "y": int(r["y"]), "w": int(r["w"]), "h": int(r["h"]),
                    "frame_no": int(r.get("frame_no", -1)),
                })
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda d: d["wall_ms"])
    return rows


def calibrate_mapping(frame_flags, detframes):
    """
    Fit  wall_ms(idx) = T0 + idx*DELTA  by maximizing agreement between the framedump
    filename `detected` flag and the detframes `detected` flag sampled at wall_ms(idx).
    Both are ~30fps samplings of the SAME live detection stream, so the (on/off) run
    structure locks the fit. Returns (T0_ms, delta_ms, agreement, n).
    """
    idxs = np.array(sorted(frame_flags), dtype=np.float64)
    fflag = np.array([frame_flags[int(i)][1] for i in idxs], dtype=np.int8)
    dwall = np.array([d["wall_ms"] for d in detframes], dtype=np.float64)
    ddet = np.array([d["detected"] for d in detframes], dtype=np.int8)

    w0, w1 = dwall[0], dwall[-1]
    span = w1 - w0
    # DELTA search: 25ms (40fps) .. 90ms (11fps); the live pipeline was CPU-bound so
    # the effective dump rate is unknown a priori -> search wide.
    best = None
    for delta in np.arange(25.0, 90.01, 0.5):
        # T0 search: idx0 lands within [w0-2s, w0 + (span - N*delta) + 2s]
        n = len(idxs)
        t0_hi = w1 - (n - 1) * delta + 2000.0
        t0_lo = w0 - 2000.0
        if t0_hi < t0_lo:
            t0_hi = t0_lo + 4000.0
        for t0 in np.arange(t0_lo, t0_hi + 1.0, 100.0):
            wall = t0 + idxs * delta
            pos = np.searchsorted(dwall, wall)
            pos = np.clip(pos, 1, len(dwall) - 1)
            left = pos - 1
            take_left = (wall - dwall[left]) <= (dwall[pos] - wall)
            nn = np.where(take_left, left, pos)
            agree = float(np.mean(ddet[nn] == fflag))
            if best is None or agree > best[2]:
                best = (t0, delta, agree, n)
    # refine DELTA around the winner at 0.05ms and T0 at 10ms
    t0b, db, _, n = best
    for delta in np.arange(db - 1.0, db + 1.01, 0.05):
        for t0 in np.arange(t0b - 200.0, t0b + 200.1, 10.0):
            wall = t0 + idxs * delta
            pos = np.clip(np.searchsorted(dwall, wall), 1, len(dwall) - 1)
            left = pos - 1
            take_left = (wall - dwall[left]) <= (dwall[pos] - wall)
            nn = np.where(take_left, left, pos)
            agree = float(np.mean(ddet[nn] == fflag))
            if agree > best[2]:
                best = (t0, delta, agree, n)
    return best


def ms_to_utc(ms):
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def utc_to_ms(hhmmss, base_date=(2026, 7, 5)):
    h, m, s = hhmmss.split(":")
    sec, _, frac = s.partition(".")
    micros = int((frac + "000000")[:6]) if frac else 0
    d = _dt.datetime(base_date[0], base_date[1], base_date[2],
                     int(h), int(m), int(sec), micros, tzinfo=_dt.timezone.utc)
    return d.timestamp() * 1000.0


# Live shot events (UTC) from live_today.log; used to locate frame-index windows.
WINDOWS = {
    # name        center UTC        note
    "seq12":  ("02:10:25.930", "Right Fade release, roi_not_found persisting mid-shot"),
    "seq13":  ("02:10:29.700", "Left Fade"),
    "seq14":  ("02:10:31.400", "Left Fade release, firstFreshMs=645"),
    "blind1": ("02:10:36.000", "blindFire"),
    "blind2": ("02:10:40.700", "blindFire"),
    "blind3": ("02:10:52.500", "blindFire"),
    # sanity: working shots (blindFire=0, code=predictive)
    "seq5":   ("02:10:00.279", "WORKING shot (predictive)"),
    "seq7":   ("02:10:06.439", "WORKING shot (predictive)"),
}


# --------------------------------------------------------------------------- #
# 5. Independent (detector-free) red-bar scan -> "is the meter visible & where".
#    The park meter is a thin vertical bar ~34x165: red fill inside a gray frame.
# --------------------------------------------------------------------------- #
def scan_red_bar(bgr):
    """Return (bbox|None, candidates) where each candidate is (x,y,w,h,red_frac,ar)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 90, 90), (10, 255, 255))
    m2 = cv2.inRange(hsv, (170, 90, 90), (180, 255, 255))
    mask = cv2.morphologyEx(m1 | m2, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < 8 or h < 30:
            continue
        ar = h / float(max(1, w))
        if ar < 1.8:            # want a TALL bar
            continue
        red_frac = float(cv2.countNonZero(mask[y:y + h, x:x + w])) / float(max(1, w * h))
        cands.append((x, y, w, h, round(red_frac, 3), round(ar, 2)))
    # prefer bars whose width is meter-like (~20-55) and tallest
    cands.sort(key=lambda t: (18 <= t[2] <= 60, t[3]), reverse=True)
    best = cands[0][:4] if cands else None
    return best, cands


# --------------------------------------------------------------------------- #
# 6. Annotation.
# --------------------------------------------------------------------------- #
def annotate(bgr, replay_bbox, detected, dbg, out_path, caption=""):
    img = bgr.copy()
    # search regions (blue)
    for reg in (dbg or {}).get("regions", []):
        try:
            rx1, ry1, rx2, ry2, label = reg
            cv2.rectangle(img, (int(rx1), int(ry1)), (int(rx2), int(ry2)), (255, 128, 0), 1)
            cv2.putText(img, str(label), (int(rx1) + 2, int(ry1) + 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 128, 0), 1)
        except Exception:
            pass
    # candidates considered (cyan) + reject reasons
    for c in (dbg or {}).get("cand", []):
        cv2.rectangle(img, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (255, 255, 0), 1)
    for c in (dbg or {}).get("val_reject", []):
        cv2.rectangle(img, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (0, 140, 255), 2)
        cv2.putText(img, "val v=%s" % c.get("v"), (c["x"], c["y"] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 140, 255), 1)
    for c in (dbg or {}).get("purity_reject", []):
        cv2.rectangle(img, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (0, 0, 255), 2)
        cv2.putText(img, "purity", (c["x"], c["y"] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
    for c in (dbg or {}).get("circ_reject", []):
        cv2.rectangle(img, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (0, 0, 200), 1)
    # independent red-bar scan (magenta) -> ground-truth "meter is here"
    rb, _ = scan_red_bar(bgr)
    if rb:
        x, y, w, h = rb
        cv2.rectangle(img, (x, y), (x + w, y + h), (255, 0, 255), 2)
        cv2.putText(img, "redbar(%d,%d,%d,%d)" % (x, y, w, h), (x, y + h + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
    # replay emitted bbox (green if detected, else gray)
    if replay_bbox and replay_bbox[2] > 0:
        x, y, w, h = replay_bbox
        col = (0, 255, 0) if detected else (160, 160, 160)
        cv2.rectangle(img, (x, y), (x + w, y + h), col, 2)
    cv2.putText(img, caption, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.imwrite(out_path, img)


# --------------------------------------------------------------------------- #
# 7. Replay loop.
# --------------------------------------------------------------------------- #
def replay(det, frames, idx_list, dt, jsonl_path, annotate_idx=None, out_dir=None,
           mapping=None, stalls=None):
    # stalls: {idx -> duration_ms}. After processing frame `idx`, re-inject the FROZEN frame
    # (duplicate) for duration_ms of ts-advance, simulating a live capture STALL (the production
    # framedump stores only UNIQUE frames, so a stall is otherwise invisible offline -- RC-4). The
    # detector is fed the same pixels with the ORIGINAL-cadence timestamps advancing, so its wall-clock
    # holds (post-peak coast etc.) see the real time gap and can decay a frozen latch.
    stalls = stalls or {}
    annotate_idx = set(annotate_idx or [])
    _CLOCK._dt = dt
    results = {}
    n_match = 0
    n_total = 0
    infer_ms_sum = 0.0
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for i in idx_list:
            if i not in frames:
                continue
            path, live_flag = frames[i]
            bgr = cv2.imread(path, cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            det._debug = {}                     # arm the diagnostic sink
            _CLOCK.tick()                        # advance one frame instant
            t0 = _REAL_PERF()
            res = det.detect(bgr, ts=_CLOCK.now())   # EXPLICIT original timestamp (RC-4)
            infer_ms = (_REAL_PERF() - t0) * 1000.0
            infer_ms_sum += infer_ms
            dbg = det._debug
            bbox = list(res.bbox) if res.bbox else [0, 0, 0, 0]
            rb, rb_cands = scan_red_bar(bgr)
            wall = None
            if mapping:
                wall = mapping[0] + i * mapping[1]
            rec = {
                "idx": i,
                "wall_ms": wall,
                "wall_utc": ms_to_utc(wall) if wall else None,
                "live_detected": live_flag,
                "detected": bool(res.detected),
                "match": bool(int(res.detected) == live_flag),
                "fill": round(float(res.fill_pct), 2),
                "conf": round(float(res.confidence), 3),
                "bbox": [int(v) for v in bbox],
                "reject": res.rejection_reason,
                "rise_state": res.rise_state,
                "last_event": det.last_debug.get("stab_event", ""),
                "zone": det.last_debug.get("zone", ""),
                "roi_miss": det.last_debug.get("roi_miss", -1),
                "cand_n": det.last_debug.get("cand_n", -1),
                "park": det.last_debug.get("park", False),
                "anchor_found": det.last_debug.get("anchor_found", 0),
                "mem_left": det.last_debug.get("mem_left", 0),
                "stab_streak": det.last_debug.get("stab_streak", -1),
                "stab_tracking": det.last_debug.get("stab_tracking", False),
                "n_regions": len(dbg.get("regions", [])),
                "n_cand": len(dbg.get("cand", [])),
                "n_val_reject": len(dbg.get("val_reject", [])),
                "n_purity_reject": len(dbg.get("purity_reject", [])),
                "redbar_bbox": list(rb) if rb else None,
                "redbar_n": len(rb_cands),
                "infer_ms": round(infer_ms, 1),
            }
            jf.write(json.dumps(rec) + "\n")
            results[i] = rec
            n_total += 1
            n_match += int(rec["match"])
            if i in annotate_idx and out_dir:
                cap = "f%05d %s live_det=%d replay_det=%d rej=%s fill=%.0f" % (
                    i, rec["wall_utc"] or "", live_flag, int(res.detected),
                    res.rejection_reason or "-", res.fill_pct)
                annotate(bgr, bbox, res.detected, dbg,
                         os.path.join(out_dir, "ann_f%05d.png" % i), cap)
            # STALL INJECTION: re-feed the FROZEN frame (duplicate) with advancing timestamps for the
            # requested duration, so the stall is visible offline and the detector's wall-clock holds
            # decay exactly as they would live. Records carry dup=1 + the ms elapsed into the stall.
            dur_ms = float(stalls.get(i, 0.0))
            if dur_ms > 0.0:
                elapsed = 0.0
                while elapsed < dur_ms:
                    _CLOCK.tick()
                    elapsed += dt * 1000.0
                    dres = det.detect(bgr, ts=_CLOCK.now())
                    drec = {
                        "idx": i, "dup": 1, "stall_ms": round(elapsed, 1),
                        "detected": bool(dres.detected),
                        "fill": round(float(dres.fill_pct), 2),
                        "reject": dres.rejection_reason,
                        "rise_state": dres.rise_state,
                        "zone": det.last_debug.get("zone", ""),
                        "mem_left": det.last_debug.get("mem_left", 0),
                    }
                    jf.write(json.dumps(drec) + "\n")
    return results, n_match, n_total, (infer_ms_sum / max(1, n_total))


# --------------------------------------------------------------------------- #
# 8. main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.path.join(
        REPO, "logs", "diagnostics", "framedump", "session_20260704_210801"))
    ap.add_argument("--detframes", default=os.path.join(
        REPO, "logs", "diagnostics", "detframes.csv"))
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "diagnostics", "replay_analysis"))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--dt", type=float, default=0.0334, help="synthetic per-frame clock step (s)")
    ap.add_argument("--window", default="", help="comma list of WINDOWS names to focus (overrides start/end)")
    ap.add_argument("--pad", type=int, default=25, help="+/- frames around a window center")
    ap.add_argument("--annotate", type=int, default=8, help="max frames to annotate per run")
    ap.add_argument("--annotate-idx", default="", help="explicit comma idx list to annotate")
    ap.add_argument("--manual-scan", default="", help="comma idx list -> crop + red-bar evidence PNGs")
    ap.add_argument("--calibrate-only", action="store_true")
    ap.add_argument("--stall", default="", help="inject capture stalls: comma list of IDX:DUR_MS "
                    "(re-feed the frozen frame after IDX for DUR_MS of advancing ts) -- makes a live "
                    "stall visible offline so the staleness/HOLD fixes can be validated")
    ap.add_argument("--tag", default="", help="suffix for output filenames")
    args = ap.parse_args()

    stalls = {}
    for spec in (s.strip() for s in args.stall.split(",") if s.strip()):
        try:
            _si, _sd = spec.split(":")
            stalls[int(_si)] = float(_sd)
        except Exception:
            print("bad --stall spec %r (want IDX:DUR_MS)" % spec)

    os.makedirs(args.out, exist_ok=True)
    frames = list_frames(args.session)
    if not frames:
        print("NO frames found in", args.session)
        return 2
    max_idx = max(frames)
    print("frames: %d  (idx 0..%d)  session=%s" % (len(frames), max_idx, os.path.basename(args.session)))

    # --- calibrate wallclock mapping from detframes.csv ---
    mapping = None
    if os.path.exists(args.detframes):
        detframes = load_detframes(args.detframes)
        T0, DELTA, agree, n = calibrate_mapping(frames, detframes)
        mapping = (T0, DELTA)
        print("\n=== FRAME<->WALLCLOCK CALIBRATION (vs detframes.csv, %d live rows) ===" % len(detframes))
        print("  wall_ms(idx) = %.1f + idx * %.3f ms" % (T0, DELTA))
        print("  T0 (idx=0)   = %s UTC   DELTA = %.3f ms (%.2f fps effective)" % (
            ms_to_utc(T0), DELTA, 1000.0 / DELTA))
        print("  idx=%d       = %s UTC" % (max_idx, ms_to_utc(T0 + max_idx * DELTA)))
        print("  det-flag agreement (live filename vs live detframes): %.1f%% over %d frames" % (
            agree * 100.0, n))
        print("  detframes span: %s .. %s UTC" % (ms_to_utc(detframes[0]["wall_ms"]),
                                                  ms_to_utc(detframes[-1]["wall_ms"])))
    else:
        print("detframes.csv NOT found -> no wallclock mapping")

    if args.calibrate_only:
        if mapping:
            print("\n=== window -> frame idx (via calibration) ===")
            for name, (utc, note) in WINDOWS.items():
                ms = utc_to_ms(utc)
                idx = (ms - mapping[0]) / mapping[1]
                print("  %-7s %s  -> idx ~%.0f   (%s)" % (name, utc, idx, note))
        return 0

    # --- build detector (faithful) ---
    _install_clock()
    det, info = build_detector()
    print("\n=== DETECTOR BUILD ===")
    for k, v in info.items():
        print("  %-16s %s" % (k, v))

    # --- decide idx range ---
    if args.window:
        want = [w.strip() for w in args.window.split(",") if w.strip()]
        idx_set = set()
        for name in want:
            if name not in WINDOWS or not mapping:
                continue
            ms = utc_to_ms(WINDOWS[name][0])
            c = int(round((ms - mapping[0]) / mapping[1]))
            for j in range(c - args.pad, c + args.pad + 1):
                if 0 <= j <= max_idx:
                    idx_set.add(j)
        idx_list = sorted(idx_set)
        rng_tag = "win_" + "_".join(want)
    else:
        end = max_idx if args.end < 0 else min(args.end, max_idx)
        idx_list = list(range(args.start, end + 1, args.step))
        rng_tag = "%d_%d_s%d" % (args.start, end, args.step)

    tag = (args.tag + "_" if args.tag else "") + rng_tag
    jsonl_path = os.path.join(args.out, "replay_%s.jsonl" % tag)

    # choose annotate idx: explicit, else spread + interesting (transitions / x>1500 / high fill)
    if args.annotate_idx:
        ann_idx = [int(x) for x in args.annotate_idx.split(",") if x.strip()]
    else:
        ann_idx = idx_list[:: max(1, len(idx_list) // max(1, args.annotate))][: args.annotate]

    print("\n=== REPLAY  %d frames  dt=%.4fs ===" % (len(idx_list), args.dt))
    t_wall0 = _REAL_PERF()
    results, n_match, n_total, avg_ms = replay(
        det, frames, idx_list, args.dt, jsonl_path,
        annotate_idx=ann_idx, out_dir=args.out, mapping=mapping, stalls=stalls)
    elapsed = _REAL_PERF() - t_wall0
    print("  wrote %s  (%d recs)" % (jsonl_path, n_total))
    print("  replay-vs-live(filename) detected match: %d/%d = %.1f%%" % (
        n_match, n_total, 100.0 * n_match / max(1, n_total)))
    print("  avg detect() = %.1f ms/frame   (%.1fs total, %.1f fps)" % (
        avg_ms, elapsed, n_total / max(1e-6, elapsed)))

    # reject breakdown
    from collections import Counter
    rej = Counter(r["reject"] or "(accepted)" for r in results.values())
    print("  reject breakdown:", dict(rej))

    # --- window summary table ---
    if mapping:
        print("\n=== WINDOW SUMMARY (replay vs live) ===")
        print("  %-7s %-13s %-5s | live_det replay_det | replay reject(mode)      redbar_seen" % (
            "name", "utc", "idx"))
        for name, (utc, note) in WINDOWS.items():
            ms = utc_to_ms(utc)
            c = int(round((ms - mapping[0]) / mapping[1]))
            sub = [results[j] for j in range(c - 8, c + 9) if j in results]
            if not sub:
                continue
            live_det = sum(r["live_detected"] for r in sub)
            rep_det = sum(r["detected"] for r in sub)
            rmode = Counter(r["reject"] or "(acc)" for r in sub).most_common(1)[0][0]
            redbar = sum(1 for r in sub if r["redbar_bbox"])
            print("  %-7s %-13s %-5d | %3d/%-3d  %3d/%-3d  | %-22s %d/%d" % (
                name, utc, c, live_det, len(sub), rep_det, len(sub), rmode, redbar, len(sub)))

    # --- manual red-bar evidence crops (detector-free) ---
    if args.manual_scan:
        scan_idx = [int(x) for x in args.manual_scan.split(",") if x.strip()]
        print("\n=== MANUAL RED-BAR SCAN (detector-free) ===")
        for i in scan_idx:
            if i not in frames:
                continue
            bgr = cv2.imread(frames[i][0], cv2.IMREAD_COLOR)
            rb, cands = scan_red_bar(bgr)
            wall = ms_to_utc(mapping[0] + i * mapping[1]) if mapping else "-"
            print("  f%05d %s  redbar=%s  n_cand=%d  top3=%s" % (
                i, wall, rb, len(cands), cands[:3]))
            if rb:
                x, y, w, h = rb
                pad = 30
                crop = bgr[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
                cv2.imwrite(os.path.join(args.out, "meter_crop_f%05d.png" % i), crop)
                annotate(bgr, [0, 0, 0, 0], False, {},
                         os.path.join(args.out, "meterscan_f%05d.png" % i),
                         "f%05d %s redbar(%d,%d,%d,%d)" % (i, wall, x, y, w, h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
