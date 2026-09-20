"""Track the 3PT nameplate icon ("3" disc), the Pill meter, and the feedback banner
across a gameplay clip, frame by frame, with everything anchored to a TRACKED
nameplate (the plate translates AND SCALES with the player, so fixed ROIs lie).

WHY: the competitor's "Icon" no-meter mode reportedly times off the player's 3PT
icon. Before deciding whether that icon is a predictive anchor (gather) or a
ground-truth release marker (ball-leaves-hand), we need per-frame badge state
pinned against the meter and the release. This probe produces that table.

Notes discovered on the 2026-08-30 park clips:
  - OTHER players' nameplates carry the SAME "3" badge (e.g. K--TUNED), so any
    badge detector MUST anchor to the tracked gamertag text, not "a black disc".
  - The park 3PT line is GREEN, so a green-dome finder needs a pill-body check
    below the dome or it locks the court line.
  - The nameplate scales with player depth (~0.6-1.1x), so template matching is
    multi-scale.

Usage:
  python tools/diagnostics/icon_badge_probe.py <video> <out_csv>
      [--badge-template f.png] [--text-template f.png] [--no-yolo] [--start N] [--end N]

Templates default to auto-cropping from hardcoded frames of the first 2026-08-30
park clip (only valid for that clip); pass files for other clips.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import cv2
import numpy as np

TEMPLATE_SPEC = {
    "badge": (63, 193, 231, 836, 874),
    "text": (90, 288, 412, 828, 866),
}

# Badge disc position relative to the TEXT top-left at scale 1.0 (measured):
# centre ~ (-59, +18); search window generous, scaled with plate scale.
BADGE_SEARCH = (-125, -20, -16, 46)   # dx0, dx1, dy0, dy1

# Feedback banner (fixed HUD element, 1080p).
BANNER_BOX = (692, 868, 28, 72)
BANNER_VALUE = (700, 865, 44, 70)

SCALES_FULL = [0.55, 0.65, 0.75, 0.85, 0.95, 1.05, 1.15]


def grab_template(cap, spec):
    fr_idx, x0, x1, y0, y1 = spec
    cap.set(cv2.CAP_PROP_POS_FRAMES, fr_idx)
    ok, fr = cap.read()
    if not ok:
        raise RuntimeError("cannot read template frame %d" % fr_idx)
    return fr[y0:y1, x0:x1].copy()


def to_gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def match_in(gray, tmpl_gray, x0, y0, x1, y1):
    H, W = gray.shape[:2]
    th, tw = tmpl_gray.shape[:2]
    x0 = max(0, min(W - tw - 1, int(x0))); x1 = max(x0 + tw + 1, min(W, int(x1)))
    y0 = max(0, min(H - th - 1, int(y0))); y1 = max(y0 + th + 1, min(H, int(y1)))
    win = gray[y0:y1, x0:x1]
    if win.shape[0] <= th or win.shape[1] <= tw:
        return (-1.0, -1, -1)
    res = cv2.matchTemplate(win, tmpl_gray, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    return (float(mx), x0 + ml[0], y0 + ml[1])


class ScaledTemplates:
    """Grayscale template pyramid cache."""

    def __init__(self, base_gray):
        self.base = base_gray
        self.cache = {}

    def at(self, s):
        key = round(s, 3)
        t = self.cache.get(key)
        if t is None:
            h, w = self.base.shape[:2]
            nw, nh = max(8, int(round(w * s))), max(6, int(round(h * s)))
            t = cv2.resize(self.base, (nw, nh), interpolation=cv2.INTER_AREA)
            self.cache[key] = t
        return t


def badge_dark_frac(frame, bx, by, bw, bh):
    roi = frame[by:by + bh, bx:bx + bw]
    if roi.size == 0:
        return 0.0
    v = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2]
    return float(np.mean(v < 70))


def _noncourt_frac(frame, x0, y0, x1, y1):
    """Fraction of pixels that look like meter shell (dark rung / white / green),
    NOT tan court wood. Used to validate a dome candidate has a pill below it."""
    H, W = frame.shape[:2]
    x0 = max(0, int(x0)); x1 = min(W, int(x1)); y0 = max(0, int(y0)); y1 = min(H, int(y1))
    if x1 - x0 < 2 or y1 - y0 < 4:
        return 0.0
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    shell = (v < 95) | ((v > 190) & (s < 90)) | ((h >= 40) & (h <= 85) & (s >= 70) & (v >= 90))
    return float(np.mean(shell))


def find_green_dome(frame, x0, x1, y0, y1):
    """Green-dome finder with a pill-body validation below the candidate.
    Returns (x, y, w, body_frac) of the best candidate or None."""
    H, W = frame.shape[:2]
    x0 = max(0, int(x0)); x1 = min(W, int(x1)); y0 = max(0, int(y0)); y1 = min(H, int(y1))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    win = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(win, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((h >= 40) & (h <= 85) & (s >= 70) & (v >= 90)).astype(np.uint8)
    if int(mask.sum()) < 6:
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, n):
        x, y, w, hgt, area = stats[i]
        if area < 6 or w < 4 or w > 40 or hgt > 40 or hgt < 2:
            continue
        # pill-body check: a strip below the dome must be shell-like, not court
        gx, gy = x0 + x, y0 + y
        bf = _noncourt_frac(frame, gx, gy + hgt + 2, gx + w, gy + hgt + 2 + max(20, 4 * w))
        if bf < 0.35:
            continue
        score = bf * area
        if best is None or score > best[3] * best[4]:
            best = (gx, gy, int(w), bf, area)
    if best is None:
        return None
    return (best[0], best[1], best[2], best[3])


def measure_meter(frame, box):
    x, y, w, h = [int(v) for v in box]
    H, W = frame.shape[:2]
    x = max(0, x); y = max(0, y)
    w = min(w, W - x); h = min(h, H - y)
    if w < 6 or h < 20:
        return None
    roi = frame[y:y + h, x:x + w]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    cx0 = max(1, int(w * 0.2)); cx1 = max(cx0 + 1, int(w * 0.8))
    green = ((hh >= 40) & (hh <= 85) & (ss >= 70) & (vv >= 90))[:, cx0:cx1]
    white = ((vv >= 200) & (ss <= 80))[:, cx0:cx1]
    ncols = green.shape[1]
    g_rows = np.where(green.sum(axis=1) >= max(2, ncols // 3))[0]
    w_rows = np.where(white.sum(axis=1) >= max(2, ncols // 3))[0]
    body_h = float(h)
    out = {"body_h": body_h, "green_top_frac": -1.0, "green_bot_frac": -1.0,
           "green_h_frac": 0.0, "fill_pct": -1.0}
    g_bot = None
    if g_rows.size:
        g_top, g_bot = int(g_rows[0]), int(g_rows[-1])
        out["green_top_frac"] = 1.0 - g_top / body_h
        out["green_bot_frac"] = 1.0 - g_bot / body_h
        out["green_h_frac"] = (g_bot - g_top + 1) / body_h
    if w_rows.size:
        cand = w_rows[w_rows > g_bot] if g_bot is not None else w_rows
        if cand.size:
            w_top = int(cand[0])
            out["fill_pct"] = 100.0 * (1.0 - w_top / body_h)
    return out


def banner_state(frame):
    x0, x1, y0, y1 = BANNER_BOX
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    dark = float(np.mean(v < 80))
    if dark < 0.30:
        return (0, "none")
    vx0, vx1, vy0, vy1 = BANNER_VALUE
    val = frame[vy0:vy1, vx0:vx1]
    hsv2 = cv2.cvtColor(val, cv2.COLOR_BGR2HSV)
    h2, s2, v2 = hsv2[:, :, 0], hsv2[:, :, 1], hsv2[:, :, 2]
    live = (s2 > 90) & (v2 > 90)
    red = int(np.sum(live & ((h2 <= 10) | (h2 >= 170))))
    grn = int(np.sum(live & (h2 >= 40) & (h2 <= 85)))
    yel = int(np.sum(live & (h2 > 10) & (h2 < 40)))
    best = max(red, grn, yel)
    if best < 25:
        return (1, "none")
    kind = "red" if best == red else ("green" if best == grn else "yellow")
    return (1, kind)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out_csv")
    ap.add_argument("--badge-template")
    ap.add_argument("--text-template")
    ap.add_argument("--no-yolo", action="store_true")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    ap.add_argument("--plate-thresh", type=float, default=0.55,
                    help="min TM_CCOEFF_NORMED score to accept the nameplate text "
                         "(lower to ~0.42 for dark/edge-of-frame sections where the "
                         "plate is visibly present but scores low)")
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print("cannot open", args.video); sys.exit(2)
    fps = cap.get(cv2.CAP_PROP_FPS) or 59.94

    badge_img = cv2.imread(args.badge_template) if args.badge_template else grab_template(cap, TEMPLATE_SPEC["badge"])
    text_img = cv2.imread(args.text_template) if args.text_template else grab_template(cap, TEMPLATE_SPEC["text"])
    badge_pyr = ScaledTemplates(to_gray(badge_img))
    text_pyr = ScaledTemplates(to_gray(text_img))

    loc = None
    loc_relaxed = None
    if not args.no_yolo:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        try:
            from meter_detector_yolo import get_locator, MeterYoloLocator
            loc = get_locator()   # PRODUCT configuration -- what ships
            print("yolo locator:", "ok provider=%s imgsz=%d" % (loc.provider, loc.imgsz) if loc else "FAILED TO LOAD")
            # DIAGNOSTIC configuration: relaxed size gate + low conf, to separate
            # "model cannot see the Pill meter" from "the plausibility gate vetoes it".
            os.environ["ORION_METER_W_MIN_FRAC"] = "0.004"
            os.environ["ORION_METER_H_MIN_FRAC"] = "0.04"
            loc_relaxed = MeterYoloLocator(conf_thres=0.10)
            os.environ.pop("ORION_METER_W_MIN_FRAC", None)
            os.environ.pop("ORION_METER_H_MIN_FRAC", None)
            if not loc_relaxed.ok:
                loc_relaxed = None
        except Exception as e:
            print("yolo import failed:", e)

    cap.set(cv2.CAP_PROP_POS_FRAMES, args.start)
    rows = []
    idx = args.start
    last_plate = None       # (x, y, scale)
    t0 = time.time()
    while True:
        if 0 <= args.end <= idx:
            break
        ok, frame = cap.read()
        if not ok:
            break
        gray = to_gray(frame)
        H, W = gray.shape[:2]

        # --- nameplate text: multi-scale; local search around last hit, else sweep ---
        best = (-1.0, -1, -1, 1.0)   # score, x, y, scale
        if last_plate is not None:
            px, py, ps = last_plate
            for s in (ps * 0.94, ps, ps * 1.06):
                if not (0.45 <= s <= 1.3):
                    continue
                t = text_pyr.at(s)
                sc, tx, ty = match_in(gray, t, px - 90, py - 70, px + 90 + t.shape[1], py + 70 + t.shape[0])
                if sc > best[0]:
                    best = (sc, tx, ty, s)
        if best[0] < args.plate_thresh:
            for s in SCALES_FULL:
                t = text_pyr.at(s)
                sc, tx, ty = match_in(gray, t, 0, int(H * 0.30), W, H)
                if sc > best[0]:
                    best = (sc, tx, ty, s)
        score, tx, ty, pscale = best
        plate_found = 1 if score >= args.plate_thresh else 0
        last_plate = (tx, ty, pscale) if plate_found else None

        # --- badge: search left of the text, at the plate's scale ---
        b_score, bx, by = (-1.0, -1, -1)
        b_dark = 0.0
        bt = badge_pyr.at(pscale)
        bth, btw = bt.shape[:2]
        if plate_found:
            dx0, dx1, dy0, dy1 = [d * pscale for d in BADGE_SEARCH]
            b_score, bx, by = match_in(gray, bt, tx + dx0, ty + dy0, tx + dx1 + btw, ty + dy1 + bth)
            if b_score > 0:
                b_dark = badge_dark_frac(frame, bx, by, btw, bth)
        badge_present = 1 if (b_score >= 0.50 and b_dark >= 0.32) else 0

        # --- YOLO meter: product config + relaxed-diagnostic config ---
        yolo_found, yx, yy, yw, yh, yconf = 0, -1, -1, -1, -1, 0.0
        if loc is not None:
            det = loc.detect_box(frame)
            if det is not None:
                yx, yy, yw, yh, yconf = det
                yolo_found = 1
        r_found, rx, ry, rw, rh, rconf = 0, -1, -1, -1, -1, 0.0
        if loc_relaxed is not None:
            det = loc_relaxed.detect_box(frame)
            if det is not None:
                rx, ry, rw, rh, rconf = det
                r_found = 1

        # --- green dome (colour, independent of YOLO), pill-body validated ---
        dome = None
        if plate_found:
            span = 430 * pscale
            dome = find_green_dome(frame, tx - 250 * pscale, tx + 330 * pscale, ty - span, ty - 60 * pscale)
        if dome is None and r_found:
            dome = find_green_dome(frame, rx - 8, rx + rw + 8, ry - 8, ry + max(20, rh // 2))
        dome_found = 1 if dome else 0
        dmx, dmy, dmw = (dome[0], dome[1], dome[2]) if dome else (-1, -1, -1)

        # --- fill measurement: prefer the relaxed YOLO box; else build from dome ---
        meter_box = None
        if r_found:
            meter_box = (rx, ry, rw, rh)
        elif dome:
            body = int(round(11.7 * max(8, dmw)))
            meter_box = (dmx - 3, dmy - 4, dmw + 6, min(230, body))
        mm = measure_meter(frame, meter_box) if meter_box else None

        bp, bk = banner_state(frame)

        rows.append({
            "frame": idx, "time_s": round(idx / fps, 4),
            "plate_found": plate_found, "plate_x": tx, "plate_y": ty,
            "plate_score": round(score, 4), "plate_scale": round(pscale, 3),
            "badge_present": badge_present, "badge_score": round(b_score, 4),
            "badge_dark_frac": round(b_dark, 4), "badge_x": bx, "badge_y": by,
            "yolo_found": yolo_found, "yolo_x": yx, "yolo_y": yy,
            "yolo_w": yw, "yolo_h": yh, "yolo_conf": round(float(yconf), 4),
            "yrelax_found": r_found, "yrelax_x": rx, "yrelax_y": ry,
            "yrelax_w": rw, "yrelax_h": rh, "yrelax_conf": round(float(rconf), 4),
            "dome_found": dome_found, "dome_x": dmx, "dome_y": dmy, "dome_w": dmw,
            "fill_pct": round(mm["fill_pct"], 2) if mm else -1.0,
            "green_top_frac": round(mm["green_top_frac"], 4) if mm else -1.0,
            "green_bot_frac": round(mm["green_bot_frac"], 4) if mm else -1.0,
            "green_h_frac": round(mm["green_h_frac"], 4) if mm else 0.0,
            "body_h": mm["body_h"] if mm else -1,
            "banner_present": bp, "banner_kind": bk,
        })
        idx += 1

    cap.release()
    if rows:
        with open(args.out_csv, "w", newline="") as f:
            wcsv = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wcsv.writeheader()
            wcsv.writerows(rows)
    dt = time.time() - t0
    print("wrote %d rows -> %s  (%.1fs, %.1f fps)" % (len(rows), args.out_csv, dt, len(rows) / max(dt, 1e-9)))

    def runs(pred):
        out = []
        cur = None
        for r in rows:
            v = pred(r)
            if v and cur is None:
                cur = r["frame"]
            elif not v and cur is not None:
                out.append((cur, r["frame"] - 1)); cur = None
        if cur is not None:
            out.append((cur, rows[-1]["frame"]))
        return out

    by_frame = {r["frame"]: r for r in rows}
    print("plate runs:", runs(lambda r: r["plate_found"] == 1))
    print("badge runs:", runs(lambda r: r["badge_present"] == 1))
    print("yolo runs:", runs(lambda r: r["yolo_found"] == 1))
    print("yrelax runs:", runs(lambda r: r["yrelax_found"] == 1))
    print("dome runs:", runs(lambda r: r["dome_found"] == 1))
    print("banner runs:", [(a, b, by_frame[a]["banner_kind"]) for a, b in runs(lambda r: r["banner_present"] == 1)])


if __name__ == "__main__":
    main()
