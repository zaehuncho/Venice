"""Characterize fade/Go-To shots from an analyze_orion_recording.py CSV.

Consumes the per-frame CSV (frame,time_ms,detected,bbox,fill_pct,...,green_*,
rejection_reason) and, per detected shot segment, reports the data the native
timing + calibration loop needs grounded in REAL footage:

  * D1 tracking quality:  detected-frame coverage, rejection mix, how often the
    bbox slides (a fade) vs stays put (standstill) during the RISE.
  * Rise-to-peak timing:  first-meter -> peak-fill duration (ms) -> the per-type
    feedforward-clock prior (RemapConfig::shotType*Ms).
  * Post-peak SETTLE:  scans the trailing frames for a STATIC-bbox + stable-fill
    run (the frozen marker after the player lands) and prints the bbox motion
    (px/frame) of the slide vs the settle -> grounds the native settle gates
    (meterSettleMaxMovePx / meterSettleFillTolPct / meterMinSettledFrames). bbox
    motion is also reported as %% of frame width and scaled to the ~1550px live
    GDI capture, since the engine threshold is in capture px, not 1080p px.

Usage:
    python tools/diagnostics/analyze_shot_settle.py logs/diagnostics/v0607_2108.csv
    python tools/diagnostics/analyze_shot_settle.py <csv> --gap-frames 16 --live-capture-w 1550
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path


def _parse_bbox(s):
    if not s or s == "-":
        return None
    try:
        x, y, w, h = (int(float(v)) for v in s.split(","))
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)
    except Exception:
        return None


def _center(b):
    return (b[0] + b[2] * 0.5, b[1] + b[3] * 0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-shot rise/settle characterization from a detector CSV")
    ap.add_argument("csv", help="CSV from analyze_orion_recording.py")
    ap.add_argument("--gap-frames", type=int, default=14, help="non-detected frames that end a shot")
    ap.add_argument("--frame-w", type=int, default=1920, help="recording width (for pct-of-width)")
    ap.add_argument("--live-capture-w", type=int, default=1550, help="live GDI capture width (engine px scale)")
    ap.add_argument("--settle-move-frac", type=float, default=0.008,
                    help="bbox center move (fraction of frame width) below which a frame is 'static'")
    ap.add_argument("--settle-fill-tol", type=float, default=8.0, help="fill %% band for the settled tail")
    ap.add_argument("--min-settle", type=int, default=3, help="min consecutive static+stable frames = settled")
    args = ap.parse_args()

    rows = []
    with open(args.csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    if not rows:
        print("no rows")
        return 1

    # Segment detected runs into shots.
    segs, cur, miss = [], None, 0
    for r in rows:
        det = r.get("detected") in ("1", "True", "true")
        if det:
            if cur is None:
                cur = []
            cur.append(r)
            miss = 0
        elif cur is not None:
            miss += 1
            if miss >= args.gap_frames:
                segs.append(cur)
                cur, miss = None, 0
    if cur:
        segs.append(cur)

    move_px_gate = args.settle_move_frac * args.frame_w
    scale = args.live_capture_w / float(args.frame_w)
    print(f"file={args.csv}")
    print(f"segments={len(segs)}  static-gate={move_px_gate:.1f}px(1080p) "
          f"= {move_px_gate*scale:.1f}px(@{args.live_capture_w})  fill_tol={args.settle_fill_tol}")

    settle_moves_all, slide_moves_all, rise_ms_all = [], [], []
    graded_settle = 0
    for i, seg in enumerate(segs, 1):
        fills = [float(r["fill_pct"]) for r in seg]
        t = [float(r["time_ms"]) for r in seg]
        cens = [(_center(_parse_bbox(r["bbox"])) if _parse_bbox(r["bbox"]) else None) for r in seg]
        rejs = {}
        for r in seg:
            rj = r.get("rejection_reason") or "ok"
            rejs[rj] = rejs.get(rj, 0) + 1
        peak_i = max(range(len(fills)), key=lambda k: fills[k])
        rise_ms = t[peak_i] - t[0]
        # per-frame bbox center motion
        moves = []
        for a, b in zip(cens, cens[1:]):
            moves.append((((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5) if (a and b) else float("nan"))
        rise_moves = [m for m in moves[:peak_i] if m == m]
        post_moves = [m for m in moves[peak_i:] if m == m]
        slide_moves_all.extend(rise_moves)
        # find the longest trailing STATIC + fill-stable run (the settled frozen marker)
        best = 0
        j = len(seg) - 1
        last_fill = fills[-1]
        run = 0
        for k in range(len(seg) - 1, 0, -1):
            mv = moves[k - 1]
            if mv == mv and mv <= move_px_gate and abs(fills[k] - last_fill) <= args.settle_fill_tol:
                run += 1
                settle_moves_all.append(mv)
            else:
                break
        best = run + 1 if run else 0
        rise_ms_all.append(rise_ms)
        settled = best >= args.min_settle
        if settled:
            graded_settle += 1
        tail_fill = statistics.median(fills[-best:]) if best else float("nan")
        print(
            f"  Shot {i:2d}: f{seg[0]['frame']}-{seg[-1]['frame']} det={len(seg)} "
            f"fill {min(fills):.0f}->{max(fills):.0f} peak@{t[peak_i]-t[0]:.0f}ms "
            f"rise_move~{(statistics.median(rise_moves) if rise_moves else 0):.0f}px "
            f"settle_run={best}{'(SETTLED)' if settled else ''} "
            f"tail_fill={tail_fill:.0f} "
            f"rej={rejs}"
        )

    def _stat(name, xs):
        if not xs:
            print(f"  {name}: (none)")
            return
        xs = sorted(xs)
        print(f"  {name}: n={len(xs)} med={statistics.median(xs):.1f} "
              f"p10={xs[int(0.1*len(xs))]:.1f} p90={xs[min(len(xs)-1,int(0.9*len(xs)))]:.1f} max={xs[-1]:.1f}")

    print(f"\n=== AGGREGATE (settled {graded_settle}/{len(segs)} shots) ===")
    _stat("rise->peak ms", rise_ms_all)
    _stat("RISE bbox move px/frame (1080p)", slide_moves_all)
    _stat("SETTLE bbox move px/frame (1080p)", settle_moves_all)
    if slide_moves_all and settle_moves_all:
        print(f"  scaled to @{args.live_capture_w}: "
              f"settle med={statistics.median(settle_moves_all)*scale:.1f}px "
              f"rise med={statistics.median(slide_moves_all)*scale:.1f}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
