#!/usr/bin/env python
"""
box_fidelity.py -- OFFLINE overlay-geometry fidelity harness (BOX-TIGHT A/B/C).

Question this answers: does the REPORTED (drawn) meter box hug the meter -- right size,
right position -- on every fresh frame? The engine times off telemetry, so the drawn box
is cosmetic; this harness exists because it must LOOK glued.

Replays the recorded live framedump sessions through the PRODUCTION reader (same coarse
window + unarmed-shot-find + armed measurement pass as tools/regression/replay_gates.py)
once per ORION_READER_BOX_TIGHT mode:

    mode 0  baseline: the shipped presentation envelope
    mode 1  BAR-HUG:  the raw red-column bbox
    mode 2  REFERENCE-HUG (owner screenshot 2026-06-29 193423): bar + 0.625*cw margin
            per side (2.25x rect), live cap-to-chevron track height + 4.5% air per
            side, 5-median size smoothing, never-shrink containment.
            Reference targets (screenshot-measured 2026-08-06): width_ratio ~2.25,
            height_ratio_vs_track ~1.09, |cx_off| <= 1px, containment 1.0.

and measures per fresh frame, against the reader's own raw red-column measurement
(reader._tight_src -- an attribute-only observability twin written on every fresh red
frame REGARDLESS of the mode, so every run measures against identical ground truth):

    width ratio / height ratio vs the BAR; height ratio vs the LIVE TRACK (mode-2 target)
    |centre-x offset| px, top/bottom offset px, IoU vs the bar
    CONTAINMENT: drawn box fully contains the current red bar (owner invariant, mode 2)
    STABILITY: per-frame |dw| / |dh| between consecutive fresh frames (no pumping)
    SCALE-TRACKING: the longest shot span replayed at x1.6 / x0.6 render scale (the same
    machinery as the camera_adapt gate) -- the drawn box must scale proportionally

The harness also asserts DISPLAY-ONLY-ness: across the armed passes the per-frame
`detected` flags, `fill_pct` values and rejection reasons must be byte-identical.

Usage:
  python tools/overlay/box_fidelity.py
  python tools/overlay/box_fidelity.py --sessions session_20260804_202151 --modes 0 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as _time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import cv2  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tools", "regression"))
import replay_gates as rg  # noqa: E402

# session_20260804_032333 is deliberately absent: it interleaves two captures (known bad).
SESSIONS = [
    "session_20260804_193918",
    "session_20260804_201020",
    "session_20260804_202151",
]

FLAG = "ORION_READER_BOX_TIGHT"


def _iou(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _run_pass(frames, W, H, armed_set, mode, scale=None):
    """Armed measurement pass with ORION_READER_BOX_TIGHT forced to `mode` (0/1/2).
    scale != None resizes every frame (the camera_adapt machinery) for the dynamic-sizing
    test. Returns per-frame records: detected/fill/reason + the reported bbox and (on
    fresh red frames) the raw bar bbox, its measured top row and the live track box."""
    os.environ[FLAG] = str(int(mode))
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(W if scale is None else int(round(W * scale)),
                               H if scale is None else int(round(H * scale)))
    assert reader._box_tight == int(mode), "flag did not take (env read at __init__)"
    records = []
    prev_armed = False
    shot_epoch = 0
    for ordi, (idx, flag, path) in enumerate(frames):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        if scale is not None:
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if armed_set is not None:
            armed = ordi in armed_set
            if armed and not prev_armed:
                shot_epoch += 1
                reader.notify_physical_shot_start(shot_epoch)
            reader.set_shot_state(armed, 0.0, armed)
            prev_armed = armed
        res = reader.detect(bgr, ts=ordi / 60.0)
        src = reader._tight_src          # fresh-red observability twin (mode-independent)
        records.append({
            "ord": ordi,
            "det": bool(res.detected),
            "fill": float(res.fill_pct or 0.0),
            "reason": str(getattr(res, "rejection_reason", "") or ""),
            "bbox": tuple(int(v) for v in (res.bbox or (0, 0, 0, 0))),
            "bar": (tuple(int(v) for v in src[0]) if src is not None else None),
            "top_row": (int(src[1]) if src is not None else -1),
            "track": (tuple(int(v) for v in src[2])
                      if src is not None and src[2] is not None else None),
        })
    return records


def _s(a):
    if not a:
        return {"n": 0}
    v = np.asarray(a, dtype=float)
    return {"n": int(v.size), "mean": round(float(v.mean()), 3),
            "p50": round(float(np.median(v)), 3),
            "p90": round(float(np.percentile(v, 90)), 3),
            "min": round(float(v.min()), 3), "max": round(float(v.max()), 3)}


def _fidelity(records):
    """Aggregate reported-vs-bar/track geometry over FRESH frames (bar ground truth
    present, detected, non-zero reported box) + containment + per-frame size stability."""
    wr, hr, hr_track, cxo, topo, boto, ious = [], [], [], [], [], [], []
    contain = 0
    dws, dhs = [], []
    prev_wh = None
    prev_ord = None
    fresh = held = 0
    for r in records:
        bx = r["bbox"]
        if not r["det"] or bx[2] <= 0 or bx[3] <= 0:
            prev_wh = None
            prev_ord = None
            continue
        if r["bar"] is None:
            held += 1
            continue
        fresh += 1
        cx, cy, cw, ch = r["bar"]
        # fold the measured red top into the bar ground truth (the tight contract raises
        # the box top to it) so 'perfect' scores exactly 1.0/0.0
        if 0 <= r["top_row"] < cy:
            ch = cy + ch - r["top_row"]
            cy = r["top_row"]
        x, y, w, h = bx
        wr.append(w / max(1.0, float(cw)))
        hr.append(h / max(1.0, float(ch)))
        if r["track"] is not None and r["track"][3] > 0:
            hr_track.append(h / float(r["track"][3]))
        cxo.append(abs((x + w * 0.5) - (cx + cw * 0.5)))
        topo.append(y - cy)
        boto.append((y + h) - (cy + ch))
        ious.append(_iou(bx, (cx, cy, cw, ch)))
        if x <= cx and y <= cy and x + w >= cx + cw and y + h >= cy + ch:
            contain += 1
        if prev_wh is not None and prev_ord == r["ord"] - 1:
            dws.append(abs(w - prev_wh[0]))
            dhs.append(abs(h - prev_wh[1]))
        prev_wh = (w, h)
        prev_ord = r["ord"]

    return {
        "fresh_frames": fresh,
        "held_frames_with_box": held,
        "width_ratio": _s(wr),
        "height_ratio_vs_bar": _s(hr),
        "height_ratio_vs_track": _s(hr_track),
        "abs_cx_off_px": _s(cxo),
        "top_off_px": _s(topo),
        "bot_off_px": _s(boto),
        "iou_vs_bar": _s(ious),
        "iou_ge_085_frac": (round(float((np.asarray(ious) >= 0.85).mean()), 3)
                            if ious else None),
        "containment_rate": (round(contain / fresh, 4) if fresh else None),
        "stability_abs_dw_px": _s(dws),
        "stability_abs_dh_px": _s(dhs),
    }


def _stability(records, shots, W, H):
    deltas = {k: [] for k in ("x", "y", "w", "h")}
    drop_runs = reacquires = dropped_frames = 0
    resize_events = region_jumps = stable_pairs = 0
    jump_floor = 0.10 * float(np.hypot(W, H))

    for a, b in shots:
        valid = [bool(r["det"] and r["bbox"][2] > 0 and r["bbox"][3] > 0)
                 for r in records[a:b]]
        k = 0
        while k < len(valid):
            if valid[k]:
                k += 1
                continue
            j = k
            while j < len(valid) and not valid[j]:
                j += 1
            if k > 0 and valid[k - 1]:
                drop_runs += 1
                dropped_frames += j - k
                if j < len(valid) and valid[j]:
                    reacquires += 1
            k = j

        for left, right in zip(records[a:b], records[a + 1:b]):
            lb, rb = left["bbox"], right["bbox"]
            lv = bool(left["det"] and lb[2] > 0 and lb[3] > 0)
            rv = bool(right["det"] and rb[2] > 0 and rb[3] > 0)
            if not (lv and rv):
                continue
            vals = {
                "x": float(rb[0] - lb[0]), "y": float(rb[1] - lb[1]),
                "w": float(rb[2] - lb[2]), "h": float(rb[3] - lb[3]),
            }
            for name, value in vals.items():
                deltas[name].append(value)
            lcx, lcy = lb[0] + 0.5 * lb[2], lb[1] + 0.5 * lb[3]
            rcx, rcy = rb[0] + 0.5 * rb[2], rb[1] + 0.5 * rb[3]
            jump_threshold = max(jump_floor, 3.0 * max(lb[2], rb[2]))
            if float(np.hypot(rcx - lcx, rcy - lcy)) > jump_threshold:
                region_jumps += 1
            elif (abs(vals["w"]) / max(1.0, float(lb[2])) > 0.10
                  or abs(vals["h"]) / max(1.0, float(lb[3])) > 0.10):
                resize_events += 1
            else:
                stable_pairs += 1

    def delta_stats(values):
        if not values:
            return {"n": 0}
        arr = np.asarray(values, dtype=float)
        return {
            "n": int(arr.size), "variance_px2": round(float(np.var(arr)), 4),
            "sd_px": round(float(np.std(arr)), 4),
            "abs_p50_px": round(float(np.median(np.abs(arr))), 3),
            "abs_p90_px": round(float(np.percentile(np.abs(arr), 90)), 3),
            "abs_max_px": round(float(np.max(np.abs(arr))), 3),
        }

    unstable = drop_runs + resize_events + region_jumps
    return {
        "shot_count": len(shots), "drop_runs": drop_runs,
        "reacquired_runs": reacquires, "dropped_frames": dropped_frames,
        "resize_events_gt10pct": resize_events,
        "region_jumps": region_jumps,
        "region_jump_threshold_px": round(jump_floor, 3),
        "stable_adjacent_pairs": stable_pairs,
        "failure_mode_share": {
            "drop_reacquire": round(drop_runs / unstable, 4) if unstable else 0.0,
            "held_resize": round(resize_events / unstable, 4) if unstable else 0.0,
            "region_jump": round(region_jumps / unstable, 4) if unstable else 0.0,
        },
        "delta": {name: delta_stats(values) for name, values in deltas.items()},
    }


def _scale_tracking(frames, W, H, shots, mode):
    """Dynamic-sizing acceptance: replay the longest shot span at x1.0/x1.6/x0.6 (the
    camera_adapt machinery) fully armed and compare the MEDIAN drawn box size. A dynamic
    box scales ~proportionally; a fixed-pixel box fails by construction."""
    if not shots:
        return {}
    a, b = max(shots, key=lambda s: s[1] - s[0])
    pad = 8
    span = frames[max(0, a - pad):b + pad]
    out = {}
    ref_w = ref_h = None
    for tag, sc in (("x1.0", None), ("x1.6", 1.6), ("x0.6", 0.6)):
        rec = _run_pass(span, W, H, set(range(len(span))), mode, scale=sc)
        ws = [r["bbox"][2] for r in rec if r["det"] and r["bbox"][2] > 0]
        hs = [r["bbox"][3] for r in rec if r["det"] and r["bbox"][3] > 0]
        med_w = float(np.median(ws)) if ws else 0.0
        med_h = float(np.median(hs)) if hs else 0.0
        ent = {"n_boxed": len(ws), "median_w_px": round(med_w, 1),
               "median_h_px": round(med_h, 1)}
        if tag == "x1.0":
            ref_w, ref_h = med_w, med_h
        elif ref_w and ref_h and med_w and med_h:
            ent["w_scale_vs_x1.0"] = round(med_w / ref_w, 3)
            ent["h_scale_vs_x1.0"] = round(med_h / ref_h, 3)
            ent["expected"] = sc
        out[tag] = ent
    return out


def measure(session, modes, framedump=rg.FRAMEDUMP, window=None):
    session_dir = os.path.join(framedump, session)
    frames = rg.list_frames(session_dir)
    if not frames:
        return {"present": False, "session": session}
    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    H, W = b0.shape[:2]
    if window is None:
        window = rg.GATE_WINDOW
    # window + shot spans found ONCE (mode 0) and reused for every pass, so all armed
    # passes see byte-identical frames + arming.
    os.environ[FLAG] = "0"
    wstart, wend = rg._locate_window(frames, W, H, window)
    frames = frames[wstart:wend]
    unarmed = rg._run_pass(frames, W, H, armed_set=None)
    shots = rg._find_shots(unarmed)
    armed_set = set()
    for a, b in shots:
        armed_set.update(range(a, b))

    passes = {m: _run_pass(frames, W, H, armed_set, m) for m in modes}

    # DISPLAY-ONLY assertion: detection + fill + reason byte-identical across ALL passes.
    mismatch = 0
    base = passes[modes[0]]
    for m in modes[1:]:
        for rb, rt in zip(base, passes[m]):
            if rb["det"] != rt["det"] or repr(rb["fill"]) != repr(rt["fill"]) \
                    or rb["reason"] != rt["reason"]:
                mismatch += 1
    out = {
        "present": True, "session": session, "frames": len(frames),
        "window": [wstart, wend], "W": W, "H": H, "n_shots": len(shots),
        "fill_or_detect_mismatch_frames": mismatch,
    }
    for m in modes:
        out["mode%d" % m] = _fidelity(passes[m])
    # dynamic-sizing test for the housing mode (and baseline for reference)
    if 2 in modes:
        out["scale_tracking_mode2"] = _scale_tracking(frames, W, H, shots, 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=SESSIONS)
    ap.add_argument("--modes", nargs="*", type=int, default=[0, 1, 2])
    ap.add_argument("--window", type=int, default=None)
    args = ap.parse_args()
    out = {}
    for s in args.sessions:
        t0 = _time.perf_counter()
        out[s] = measure(s, list(args.modes), window=args.window)
        out[s]["_wall_s"] = round(_time.perf_counter() - t0, 1)
        sys.stderr.write("done %s in %.1fs\n" % (s, out[s]["_wall_s"]))
        sys.stderr.flush()
    print(json.dumps(out, indent=2))
    ok = all((not m.get("present"))
             or (m.get("fill_or_detect_mismatch_frames", 1) == 0)
             for m in out.values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
