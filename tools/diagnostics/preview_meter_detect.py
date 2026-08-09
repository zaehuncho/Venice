#!/usr/bin/env python3
"""Annotated meter-detection preview + scorecard on a real gameplay video.

Runs the SHIPPING detector (meter_detector.MeterDetector with load_detector_config(),
so settings.json — template anchor, auto colour, thresholds — is honoured exactly like
live) over a video IN ORDER (the detector is stateful) and renders an annotated mp4 plus
a per-frame CSV. It also draws an INDEPENDENT high-precision ground-truth (GT) box for the
real red meter so a false lock is visually obvious: GT (bright green) vs the detector's
lock (coloured by which code path won).

This is the acceptance harness for the detection rebuild: the detector box must ride the
GT box through every shot, with no idle / floor / scatter locks.

  python tools/diagnostics/preview_meter_detect.py --video "C:/.../clip.mp4" \
      --out out.mp4 --csv out.csv [--downscale 1280x720] [--no-anchor] \
      [--start 0 --dur 60 --stride 2 --color Red]
"""
from __future__ import annotations
import argparse
import csv
import os
import sys
from collections import Counter

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from meter_detector import MeterDetector, load_detector_config  # noqa: E402

_ZONE_BGR = {
    "anchor": (255, 255, 0),       # cyan
    "green_first": (255, 0, 255),  # magenta
    "wide": (0, 0, 255),           # red
    "crop": (0, 165, 255),         # orange
    "locked_crop": (0, 165, 255),  # orange
}


def gt_meter(frame_bgr, color="Red"):
    """Independent, GREEN-TIP-ANCHORED ground truth for the real shot meter, robust at
    ALL fills. The neon-GREEN chevron marks the top of the track whenever the meter is
    up (even at low fill); the meter is the thin PURE-saturated red column directly
    below it, inside the central play band. Anchoring on the green tip (not the red bar)
    avoids the low-fill blind spot where the red sits far below the tip. Conjunctive
    (green tip AND a thin red column below) so it almost never fires on jerseys / logos /
    floor / post-shot empty space. Returns the meter (x, y, w, h) spanning tip->fill, or
    None. Independent of the detector under test — marks where the meter ACTUALLY is."""
    H, W = frame_bgr.shape[:2]
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    y0, y1 = int(H * 0.10), int(H * 0.88)
    if color == "Purple":
        red = ((Hh >= 138) & (Hh <= 162) & (Ss >= 140) & (Vv >= 85)).astype(np.uint8)
    else:
        red = (((Hh >= 164) | (Hh <= 11)) & (Ss >= 150) & (Vv >= 85)).astype(np.uint8)
    green = ((Hh >= 40) & (Hh <= 85) & (Ss >= 70) & (Vv >= 105)).astype(np.uint8)
    red[:y0] = 0; red[y1:] = 0
    green[:y0] = 0; green[y1:] = 0
    if int(green.sum()) < 2 or int(red.sum()) < 5:
        return None
    gd = cv2.dilate(green * 255, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)
    cnts, _ = cv2.findContours(gd, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    wmax = max(6, int(W * 0.022))           # the meter column is THIN
    csz = max(30, int(W * 0.05))            # the chevron is small/compact
    track = max(120, int(H * 0.42))         # max track height below the tip
    xr = max(8, int(W * 0.014))
    best, bscore = None, -1.0
    for c in cnts:
        gx, gy, gw, gh = cv2.boundingRect(c)
        if gw > csz or gh > csz or gw * gh < 2:
            continue
        cx = gx + gw // 2
        x0, x1 = max(0, cx - xr), min(W, cx + xr + 1)
        col = red[gy:min(H, gy + track), x0:x1]
        ys = np.flatnonzero(col.any(axis=1))
        if int(col.sum()) < 6 or ys.size < 4:
            continue
        rtop = gy + int(ys[0]); rbot = gy + int(ys[-1]); rmid = (rtop + rbot) // 2
        rw = int(np.count_nonzero(red[rmid, max(0, cx - 30):cx + 31]))
        if not (2 <= rw <= wmax):
            continue
        score = float(ys.size)
        if score > bscore:
            bscore = score
            top = min(gy, rtop)
            best = (max(0, cx - max(rw, 6) // 2), top, max(6, rw), max(8, rbot - top + 1))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--downscale", default="", help="WxH, e.g. 1280x720")
    ap.add_argument("--start", type=float, default=0.0, help="start seconds")
    ap.add_argument("--dur", type=float, default=0.0, help="duration seconds (0=all)")
    ap.add_argument("--stride", type=int, default=2, help="process every Nth frame")
    ap.add_argument("--color", default="", help="override meter_color")
    ap.add_argument("--no-anchor", action="store_true", help="disable template anchor")
    ap.add_argument("--no-gt", action="store_true", help="don't draw the ground-truth box (cleaner viewing)")
    ap.add_argument("--max-frames", type=int, default=0)
    args = ap.parse_args()

    cfg = load_detector_config()
    if args.color:
        cfg.meter_color = args.color
    if args.no_anchor:
        cfg.template_anchor_enabled = False
    det = MeterDetector(os.path.join(_REPO, "meter_styles"), cfg)
    gt_color = cfg.meter_color

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print("cannot open", args.video); return 2
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    dw = dh = None
    if args.downscale:
        dw, dh = (int(v) for v in args.downscale.lower().split("x"))
    if args.start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, args.start * 1000.0)
    end_ms = (args.start + args.dur) * 1000.0 if args.dur > 0 else 0.0

    writer = None
    out_fps = max(1.0, src_fps / max(1, args.stride))
    rows = []
    zones = Counter()
    det_centers = []         # (cx, cy) on detected frames
    gt_hits = det_hits = on_gt = idle_locks = 0
    fi = -1
    proc = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        fi += 1
        if end_ms and cap.get(cv2.CAP_PROP_POS_MSEC) > end_ms:
            break
        if fi % max(1, args.stride):
            continue
        if dw:
            frame = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)
        H, W = frame.shape[:2]
        r = det.detect(frame)
        dbg = det.last_debug or {}
        gt = gt_meter(frame, gt_color)

        zone = str(dbg.get("zone", "") or "")
        zones[zone] += 1
        bx, by, bw, bh = r.bbox
        if r.detected and bw > 0:
            det_hits += 1
            dcx, dcy = bx + bw / 2.0, by + bh / 2.0
            det_centers.append((dcx, dcy))
        if gt is not None:
            gt_hits += 1
        # On-GT: detector box centre within the GT box (correct lock).
        ongt = False
        if r.detected and gt is not None:
            gx, gy, gw, gh = gt
            ongt = (gx - 6 <= bx + bw / 2.0 <= gx + gw + 6) and (gy - 10 <= by + bh / 2.0 <= gy + gh + 10)
            if ongt:
                on_gt += 1
        # Idle lock proxy: detector locked but NO ground-truth meter on screen.
        if r.detected and gt is None and r.rejection_reason in ("", "green_not_found"):
            idle_locks += 1

        rows.append(dict(
            f=fi, det=int(r.detected), zone=zone, fill=round(r.fill_pct, 1),
            conf=round(r.confidence, 3), rej=r.rejection_reason,
            x=bx, y=by, w=bw, h=bh, anchor_found=dbg.get("anchor_found", 0),
            anchor_score=round(float(dbg.get("anchor_score", -1.0)), 3),
            med_h=dbg.get("med_h", -1.0), med_s=dbg.get("med_s", -1.0),
            gt_x=(gt[0] if gt else -1), gt_y=(gt[1] if gt else -1),
            gt_w=(gt[2] if gt else -1), gt_h=(gt[3] if gt else -1), on_gt=int(ongt)))

        if args.out:
            ann = frame.copy()
            if gt is not None and not args.no_gt:
                gx, gy, gw, gh = gt
                cv2.rectangle(ann, (gx - 2, gy - 2), (gx + gw + 2, gy + gh + 2), (0, 255, 0), 2)
                cv2.putText(ann, "GT", (gx + gw + 4, gy + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
            if r.detected and bw > 0:
                col = _ZONE_BGR.get(zone, (255, 255, 255))
                cv2.rectangle(ann, (bx, by), (bx + bw, by + bh), col, 2)
                cv2.putText(ann, f"{zone} {r.fill_pct:.0f}% c{r.confidence:.2f}",
                            (bx, max(12, by - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
            af = int(dbg.get("anchor_found", 0) or 0)
            hdr = f"f{fi} det={int(r.detected)} {zone} rej={r.rejection_reason} anc={af}/{float(dbg.get('anchor_score',-1)):.2f}"
            cv2.putText(ann, hdr, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            if writer is None:
                writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (W, H))
            writer.write(ann)

        proc += 1
        if args.max_frames and proc >= args.max_frames:
            break
        if proc % 500 == 0:
            print(f"  ..{proc} frames  det={det_hits} gt={gt_hits} onGT={on_gt} idle={idle_locks}")

    cap.release()
    if writer is not None:
        writer.release()
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)

    n = max(1, len(rows))
    xs = [c[0] for c in det_centers]
    ys = [c[1] for c in det_centers]
    print("=" * 70)
    print(f"frames processed: {len(rows)}  ({args.video})")
    print(f"detected: {det_hits} ({100*det_hits/n:.1f}%)   GT-present: {gt_hits} ({100*gt_hits/n:.1f}%)")
    print(f"on-GT (correct lock / all detections): {on_gt}/{det_hits} "
          f"({100*on_gt/max(1,det_hits):.1f}%)  <- PRECISION")
    print(f"GT-recall (on-GT / GT-present frames):  {on_gt}/{gt_hits} "
          f"({100*on_gt/max(1,gt_hits):.1f}%)  <- RECALL")
    print(f"IDLE FALSE-LOCKS (locked, no GT meter):  {idle_locks} ({100*idle_locks/n:.1f}%)  <- want ~0")
    if xs:
        print(f"detection centre X: min {min(xs):.0f} max {max(xs):.0f} std {np.std(xs):.0f}  (scatter)")
        print(f"detection centre Y: min {min(ys):.0f} max {max(ys):.0f} std {np.std(ys):.0f}")
    print("zones:", dict(zones.most_common()))
    if args.out:
        print("annotated:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
