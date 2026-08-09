#!/usr/bin/env python
"""green_zone_grader.py -- OCR-free self-grader from the meter's GREEN make-window colour.

READ-ONLY diagnostic. Writes only under logs/diagnostics/. Never touches production.

FINDING that grounds this tool (verified on framedumps, 2026-07-09):
  The NBA-2K shot meter renders the perfect-release "make window" as a SATURATED NEON-GREEN
  band at the TOP of the meter (OpenCV BGR ~[19,254,28], HSV ~[58,224,255]), pinned at the
  100% apex (the arrowhead tip) and extending DOWNWARD by a VARIABLE amount that encodes the
  per-shot window: a thin ~1-4 pp sliver on contested/moving shots, a wide ~10-34 pp solid
  band on open standstill shots (a clear translucent gap separates it from the rising red fill
  until the red covers it). It is trivially colour-segmentable. Reading its LOWER edge per shot
  recovers the make-window that previously only existed in the engine logs -- with NO OCR.
  Validated vs part0: top pinned ~100 (100% agreement), width bimodal ~standstill 12 pp /
  moving 2 pp (matched across sessions 162915 / 041446 / 043919).

What this tool provides:
  (a) per-shot GREEN WINDOW [g_lo, 100] where g_lo = lower edge of the exposed green band.
  (b) a per-shot OCR-free label by comparing fill-at-cap (peak) to that window:
      EARLY  (peak fell short of the window,  peak < g_lo - tol)
      GREEN  (peak landed inside [g_lo, 100+tol])
      OVER   (peak overshot the apex into deflate/overtime, peak > 100 + tol)

Motion-safe scale: the meter is player-attached and MOVES frame-to-frame, so absolute pixel
rows are NOT comparable across frames. Every quantity is converted to a fill% on THAT FRAME's
own scale (0% = red floor = track bottom; 100% = green apex) before aggregating. The window's
lower edge is read only from frames where the green band is still UN-OCCLUDED by the rising red.

Usage:
  C:/Python314/python.exe tools/diagnostics/green_zone_grader.py --session logs/diagnostics/framedump/session_20260706_041446
  (add more --session ... to aggregate; --json to dump per-shot rows)
"""
import argparse, os, sys, glob, re, json
import numpy as np, cv2

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "_smr_grader", os.path.join(REPO, "tools", "diagnostics", "simple_meter_reader.py"))
_smr = importlib.util.module_from_spec(_spec)
sys.modules["_smr_grader"] = _smr
_spec.loader.exec_module(_smr)
SimpleMeterReader = _smr.SimpleMeterReader

# Neon green make-window swatch (shared with meter_detector._GreenWindowScanner).
NEON_LO = np.array([48, 140, 140], np.uint8)
NEON_HI = np.array([70, 255, 255], np.uint8)
# BGR pure red fill (matches the reader's Arrow2 red).
RED_LO = np.array([0, 0, 180], np.uint8)
RED_HI = np.array([80, 80, 255], np.uint8)

_FR = re.compile(r"f(\d{5})_([01])_raw\.png$")


def list_frames(session_dir):
    out = []
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FR.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    out.sort()
    return out


def scan_session(session_dir):
    """Per-frame: reader fill + red floor/top + green tip top/bottom (stable pixel rows)."""
    frames = list_frames(session_dir)
    if not frames:
        return []
    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    if b0 is None:
        return []
    H, W = b0.shape[:2]
    reader = SimpleMeterReader(W, H)
    recs = []
    for idx, flag, path in frames:
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        r = reader.read(bgr)
        rec = dict(idx=idx, det=bool(r["detected"]), fill=float(r["fill"]),
                   g_top=None, g_bot=None, g_h=0, red_floor=None, red_top=None)
        if r["detected"] and r["bbox"] and r["bbox"][2] > 0:
            x, y, w, h = r["bbox"]
            cx0 = max(0, x - 3); cx1 = min(W, x + w + 3)
            ytop = max(0, y - 40); ybot = min(H, y + h + 3)
            col = bgr[ytop:ybot, cx0:cx1]
            if col.size:
                hsv = cv2.cvtColor(col, cv2.COLOR_BGR2HSV)
                neon = cv2.inRange(hsv, NEON_LO, NEON_HI)
                red = cv2.inRange(col, RED_LO, RED_HI)
                cthr = max(1, int(0.15 * col.shape[1]))
                ri = np.flatnonzero(np.count_nonzero(red, axis=1) >= cthr)
                gi = np.flatnonzero(np.count_nonzero(neon, axis=1) >= 1)
                if ri.size:
                    rec["red_top"] = ytop + int(ri[0]); rec["red_floor"] = ytop + int(ri[-1])
                if gi.size:
                    rec["g_top"] = ytop + int(gi[0]); rec["g_bot"] = ytop + int(gi[-1])
                    rec["g_h"] = int(gi[-1] - gi[0] + 1)
        recs.append(rec)
    return recs


def find_shots(recs, min_len=4, min_peak=60.0):
    out = []; i = 0; n = len(recs)
    while i < n:
        if not recs[i]["det"]:
            i += 1; continue
        j = i
        while j < n and recs[j]["det"]:
            j += 1
        seg = recs[i:j]
        if j - i >= min_len and max(s["fill"] for s in seg) >= min_peak:
            out.append((i, j))
        i = j
    return out


def grade_shot(seg, tol=2.0, expose_margin_px=3):
    """Motion-safe per-frame measurement. The meter is player-attached and MOVES
    frame-to-frame, so absolute pixel rows are NOT comparable across frames -- every
    quantity is turned into a fill% on THAT FRAME's own scale (0% = red floor = track
    bottom; 100% = green apex = arrowhead tip) before aggregating.

    The full green make-window band is only visible while the rising red has not yet
    covered it, so the window START (its lower edge) is taken from frames where the
    green band sits strictly ABOVE the red top (a real gap). fill-at-cap is the peak
    red top over the shot."""
    green_starts = []      # window bottom edge as fill%, from UN-OCCLUDED green frames
    green_widths = []
    fills = []
    for s in seg:
        if s["red_floor"] is None or s["red_top"] is None:
            continue
        floor = float(s["red_floor"])
        # frame-local apex: prefer this frame's green top; fall back skipped if absent
        if s["g_top"] is not None:
            span = max(1.0, floor - float(s["g_top"]))
        else:
            continue
        to_pct = lambda row: (floor - float(row)) / span * 100.0
        fills.append(to_pct(s["red_top"]))
        if s["g_bot"] is not None:
            # green fully exposed when red top sits BELOW green bottom (larger row = lower)
            if s["red_top"] > s["g_bot"] + expose_margin_px:
                gs = to_pct(s["g_bot"])
                green_starts.append(gs)
                green_widths.append(100.0 - gs)
    if not fills or not green_starts:
        return None
    # window START = robust LOW edge of the exposed green band (widest exposed = truest);
    # use the 20th pct of measured starts to reject frames where red has begun to clip it.
    win_start = float(np.percentile(green_starts, 20))
    win_width = 100.0 - win_start
    peak_fill = float(np.max(fills))
    if peak_fill < win_start - tol:
        label = "EARLY"
    elif peak_fill > 100.0 + tol:
        label = "OVER"
    else:
        label = "GREEN"
    return dict(g_lo=round(win_start, 1), g_hi=100.0, win_width_pp=round(win_width, 1),
                peak_fill=round(peak_fill, 1), label=label,
                n_exposed_green=len(green_starts), n_frames=len(seg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", required=True,
                    help="framedump session dir (repeatable)")
    ap.add_argument("--tol", type=float, default=2.0)
    ap.add_argument("--json", default="", help="optional path to dump per-shot rows")
    args = ap.parse_args()

    all_rows = []
    for sess in args.session:
        sdir = sess if os.path.isabs(sess) else os.path.join(REPO, sess)
        name = os.path.basename(sdir.rstrip("/\\"))
        recs = scan_session(sdir)
        shots = find_shots(recs)
        print("\n== %s : %d frames, %d shots ==" % (name, len(recs), len(shots)))
        for si, (a, b) in enumerate(shots):
            g = grade_shot(recs[a:b], tol=args.tol)
            if g is None:
                print("  shot%-2d  (no green frames -- ungradable)" % si)
                continue
            g.update(session=name, shot=si)
            all_rows.append(g)
            print("  shot%-2d  win=[%5.1f,100]  width=%4.1fpp  peak=%5.1f  -> %-5s  (exposed-green frames %d/%d)"
                  % (si, g["g_lo"], g["win_width_pp"],
                     g["peak_fill"], g["label"], g["n_exposed_green"], g["n_frames"]))

    if all_rows:
        print("\n================ AGGREGATE (%d gradable shots) ================" % len(all_rows))
        his = [r["g_hi"] for r in all_rows]
        wids = [r["win_width_pp"] for r in all_rows]
        print("green-window TOP  g_hi : median=%.1f  p10=%.1f  (part0: top pinned ~100)"
              % (np.median(his), np.percentile(his, 10)))
        print("green-window WIDTH pp  : median=%.1f  p90=%.1f  max=%.1f  (part0: standstill~12, moving~2)"
              % (np.median(wids), np.percentile(wids, 90), max(wids)))
        top_pin = 100.0 * np.mean([abs(h - 100.0) <= 4.0 for h in his])
        print("top-pin agreement (g_hi within 4pp of 100): %.0f%%" % top_pin)
        from collections import Counter
        c = Counter(r["label"] for r in all_rows)
        print("labels:", dict(c))
    if args.json and all_rows:
        with open(args.json, "w", encoding="utf-8") as f:
            for r in all_rows:
                f.write(json.dumps(r) + "\n")
        print("wrote", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
