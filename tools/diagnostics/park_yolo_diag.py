#!/usr/bin/env python
"""
park_yolo_diag.py  --  root-cause instrumentation for the "match-finding blackout"
(reject='roi_not_found') on the failing Fade shots of session_20260704_210801.

For each sampled frame it records, WITHOUT modifying meter_detector.py, by driving the
detector's own internals:
  1. YOLO locator box + confidence   (standalone MeterLocator -> same weights/env, so
     it mirrors the main detector's per-frame locate() without perturbing its state).
  2. _ParkTracker RAW candidates BEFORE gates, on the SAME 1280x720 INTER_AREA resize
     (s=1.0) the tracker uses -> for every red contour: bbox + WHICH gate rejected it
     (size / bot_min / outside_band / edge_height / thin_ratio), computed off a FRESH
     pure-red (S>=180) mask (no static-EMA) so the geometry gate is isolated.
  3. The SAME candidate extraction at a RELAXED S>=130 mask -> does a contour appear
     that the S>=180 mask missed? (direct blur-desaturation test).
  4. The real, stateful park emit (dbg_park.update fed in-sequence from idx 0) -> the
     rect + last_confidence it actually produced (accounts for static-EMA + _confirm).
  5. Red-mask pixel stats INSIDE the hand-verified true meter bbox (park's 720p space):
     mean S, mean V, #px passing pure(S>=180,V>=150,H<=2|>=176) vs relaxed(S>=130).
  6. Meter fill mid-rise(short) vs tall, from the true bbox height.

Writes ONLY under logs/diagnostics/. Reuses replay_framedump.py for the session env,
frame listing, wallclock calibration and the independent red-bar finder.
"""
import argparse
import json
import os
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_framedump as R          # sets session env + sys.path(REPO); no clock installed

from meter_detector import _ParkTracker as PT       # noqa: E402
from meter_locator_infer import try_load as load_locator  # noqa: E402

REF_W, REF_H = PT._REF_W, PT._REF_H                  # 1280 x 720
SX = None  # native->720 scale set per frame


def to720(bbox_native, wn, hn):
    sx, sy = REF_W / float(wn), REF_H / float(hn)
    x, y, w, h = bbox_native
    return (int(round(x * sx)), int(round(y * sy)), max(1, int(round(w * sx))), max(1, int(round(h * sy))))


def park_candidates(work, s_min):
    """Replicate _ParkTracker._find's contour+gate ladder (s=1.0, FRESH mask, no EMA).
    Returns every contour with the FIRST gate that rejects it (or 'PASS')."""
    H, W = work.shape[:2]
    cy0 = max(0, int(H * PT._Y_TOP)); cy1 = min(H, int(H * PT._Y_BOT))
    cx0 = max(0, int(W * PT._X_LEFT_EDGE)); cx1 = min(W, int(W * PT._X_RIGHT_EDGE))
    crop = work[cy0:cy1, cx0:cx1]
    red = PT._red_mask(crop, PT._HUE_LO, PT._HUE_HI, s_min, PT._VAL_FLOOR)
    red = cv2.morphologyEx(red * 255, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    s = 1.0
    wmin = max(2, int(round(PT._W_MIN * s))); wmax = int(round(PT._W_MAX * s)); hmin = max(2, int(round(PT._H_MIN * s)))
    thin_h = int(round(9 * s)); bot_min = int(H * PT._BOT_MIN)
    x_le = int(W * PT._X_LEFT_EDGE); x_re = int(W * PT._X_RIGHT_EDGE)
    x_l = int(W * PT._X_LEFT); x_r = int(W * PT._X_RIGHT); edge_h = int(H * PT._X_EDGE_MIN_H)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        x += cx0; y += cy0
        cx = x + w // 2
        ar = h / float(max(1, w))
        if w < wmin or w > wmax or h < hmin:
            gate = "size(w=%d h=%d need w[%d..%d] h>=%d)" % (w, h, wmin, wmax, hmin)
        elif (y + h) < bot_min:
            gate = "bot_min(bottom=%d<%d)" % (y + h, bot_min)
        elif cx < x_le or cx > x_re:
            gate = "outside_band(cx=%d)" % cx
        elif (cx < x_l or cx > x_r) and h < edge_h:
            gate = "edge_height(h=%d<%d, cx=%d in edge)" % (h, edge_h, cx)
        elif h >= thin_h and ar < 0.8:
            gate = "thin_ratio(ar=%.2f)" % ar
        else:
            gate = "PASS"
        out.append({"cx": cx, "x": x, "y": y, "w": w, "h": h, "bottom": y + h, "ar": round(ar, 2), "gate": gate})
    return out


def redmask_stats(work, bbox720):
    """mean S/V + pure(S>=180) vs relaxed(S>=130) red-px counts inside the true bbox (720p space)."""
    x, y, w, h = bbox720
    x = max(0, x); y = max(0, y)
    sub = work[y:y + h, x:x + w]
    if sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    hue_red = (Hh <= PT._HUE_LO) | (Hh >= PT._HUE_HI)
    val_ok = Vv >= PT._VAL_FLOOR
    pure = hue_red & (Ss >= 180) & val_ok
    relaxed = hue_red & (Ss >= 130) & val_ok
    loose = hue_red & (Ss >= 90) & val_ok
    return {
        "px": int(sub.shape[0] * sub.shape[1]),
        "mean_S": round(float(Ss.mean()), 1),
        "mean_V": round(float(Vv.mean()), 1),
        "red_S180_V150": int(pure.sum()),
        "red_S130_V150": int(relaxed.sum()),
        "red_S90_V150": int(loose.sum()),
        "hue_red_px": int(hue_red.sum()),
    }


def find_true_bbox(native, s_floor=80, play_band=(0.20, 0.72), hint_cx=None):
    """Detector-free tallest red vertical bar in the play band (hand-verify via saved crop)."""
    H, W = native.shape[:2]
    hsv = cv2.cvtColor(native, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, s_floor, 90), (10, 255, 255))
    m2 = cv2.inRange(hsv, (170, s_floor, 90), (180, 255, 255))
    mask = cv2.morphologyEx(m1 | m2, cv2.MORPH_CLOSE, np.ones((7, 3), np.uint8))
    y0, y1 = int(H * play_band[0]), int(H * play_band[1])
    mask[:y0] = 0; mask[y1:] = 0
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < 10 or h < 25:
            continue
        ar = h / float(max(1, w))
        # reject non-meter reds: the NBA-logo banner / left-edge bars (screen edges) and the
        # tall (h>240) logo/banner blobs; the real meter is a thin bar (w~30, h<=~170).
        if ar < 1.6 or w > 90 or h > 240 or x < 55 or (x + w) > 1865:
            continue
        score = h + (200 if 18 <= w <= 60 else 0) - (abs(x + w // 2 - hint_cx) * 0.3 if hint_cx else 0)
        cands.append((score, [x, y, w, h], round(ar, 2)))
    if not cands:
        return None, []
    cands.sort(key=lambda t: t[0], reverse=True)
    return cands[0][1], [(c[1], c[2]) for c in cands[:4]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=os.path.join(
        R.REPO, "logs", "diagnostics", "framedump", "session_20260704_210801"))
    ap.add_argument("--out", default=os.path.join(R.REPO, "logs", "diagnostics", "replay_analysis"))
    ap.add_argument("--idx", default="", help="explicit comma idx list to sample")
    ap.add_argument("--window", default="seq12,seq13,seq14,blind1,blind2",
                    help="WINDOWS names -> sample center +/- pad")
    ap.add_argument("--pad", type=int, default=4)
    ap.add_argument("--warm-from", type=int, default=560, help="feed park from this idx to warm its EMA/tracks")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    frames = R.list_frames(args.session)
    detframes = R.load_detframes(os.path.join(R.REPO, "logs", "diagnostics", "detframes.csv"))
    T0, DELTA, agree, _ = R.calibrate_mapping(frames, detframes)
    mapping = (T0, DELTA)

    # sample idx set
    if args.idx:
        sample = sorted(int(x) for x in args.idx.split(",") if x.strip())
    else:
        sample = set()
        for name in args.window.split(","):
            name = name.strip()
            if name not in R.WINDOWS:
                continue
            ms = R.utc_to_ms(R.WINDOWS[name][0])
            c = int(round((ms - T0) / DELTA))
            for j in range(c - args.pad, c + args.pad + 1):
                if j in frames:
                    sample.add(j)
        sample = sorted(sample)

    # build standalone YOLO + park; warm park in-sequence
    dbg_loc = load_locator()
    dbg_park = PT()
    park_emit = {}
    lo = min(args.warm_from, min(sample) if sample else args.warm_from)
    hi = max(sample) if sample else lo
    for i in range(lo, hi + 1):
        if i not in frames:
            continue
        bgr = cv2.imread(frames[i][0], cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        rect = dbg_park.update(bgr)
        park_emit[i] = (rect, float(dbg_park.last_confidence),
                        len(dbg_park._tracks), sum(1 for t in dbg_park._tracks if t.locked))

    jsonl = os.path.join(args.out, "park_yolo_diag.jsonl")
    table = []
    with open(jsonl, "w", encoding="utf-8") as jf:
        for i in sample:
            bgr = cv2.imread(frames[i][0], cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            Hn, Wn = bgr.shape[:2]
            work = cv2.resize(bgr, (REF_W, REF_H), interpolation=cv2.INTER_AREA)
            wall = R.ms_to_utc(T0 + i * DELTA)

            # YOLO
            box = dbg_loc.locate(bgr) if dbg_loc is not None else None
            yconf = float(dbg_loc.last_conf) if dbg_loc is not None else -1.0

            # true bbox (hand-verify via crop). Right Fade slides RIGHT (hint high x), Left Fade LEFT.
            tb_native, tb_cands = find_true_bbox(bgr)
            tb720 = to720(tb_native, Wn, Hn) if tb_native else None
            stats = redmask_stats(work, tb720) if tb720 else None

            # park raw candidates (pure S>=180 and relaxed S>=130) + gate attribution
            cand180 = park_candidates(work, PT._S_MIN)
            cand130 = park_candidates(work, 130)

            # which candidates are NEAR the true meter (720 space)?
            def near(c):
                if not tb720:
                    return False
                tcx = tb720[0] + tb720[2] // 2
                return abs(c["cx"] - tcx) <= 40 and abs((c["bottom"]) - (tb720[1] + tb720[3])) <= 40
            near180 = [c for c in cand180 if near(c)]
            near130 = [c for c in cand130 if near(c)]

            rect, pconf, ntr, nlk = park_emit.get(i, (None, 0.0, 0, 0))
            th_native = tb_native[3] if tb_native else -1
            fill_class = "n/a" if th_native < 0 else ("tall" if th_native >= 120 else "mid-rise/short")

            rec = {
                "idx": i, "wall_utc": wall,
                "live_detected": frames[i][1],
                "true_bbox_native": tb_native, "true_bbox_720": tb720,
                "fill_class": fill_class, "true_h_native": th_native,
                "yolo_box": list(box) if box else None, "yolo_conf": round(yconf, 3),
                "yolo_dead_band": bool(0.45 <= yconf < 0.55),
                "park_emit_rect": list(rect) if rect else None, "park_conf": round(pconf, 3),
                "park_tracks": ntr, "park_locked": nlk,
                "redstats": stats,
                "cand180_total": len(cand180), "cand180_pass": sum(c["gate"] == "PASS" for c in cand180),
                "cand130_total": len(cand130), "cand130_pass": sum(c["gate"] == "PASS" for c in cand130),
                "near180": near180, "near130": near130,
                "true_cands_dbg": tb_cands,
            }
            jf.write(json.dumps(rec, default=str) + "\n")

            # decisive columns for the table
            if near180:
                gate180 = "PASS" if any(c["gate"] == "PASS" for c in near180) else near180[0]["gate"]
            elif near130:
                gate180 = "NO-S180-contour; S130:" + ("PASS" if any(c["gate"] == "PASS" for c in near130) else near130[0]["gate"])
            else:
                gate180 = "no red contour (even S130)" if not near130 else "?"
            table.append({
                "idx": i, "utc": wall, "true": tb_native, "fill": fill_class,
                "yconf": round(yconf, 3), "park_rect": rect is not None, "park_conf": round(pconf, 3),
                "gate": gate180,
                "S180": stats["red_S180_V150"] if stats else -1,
                "S130": stats["red_S130_V150"] if stats else -1,
                "meanS": stats["mean_S"] if stats else -1,
                "meanV": stats["mean_V"] if stats else -1,
            })

            # save an evidence crop for hand-verification
            if tb_native:
                x, y, w, h = tb_native
                pad = 34
                crop = bgr[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
                cv2.imwrite(os.path.join(args.out, "truebar_f%05d.png" % i), crop)

    # print table
    print("wrote", jsonl)
    print("calib: wall_ms(idx)=%.0f + idx*%.3f  (agreement %.1f%%)" % (T0, DELTA, agree * 100))
    print("\nidx   utc            true_bbox_native        fill          yConf park pConf | S>=180 S>=130 meanS meanV | park gate(near true meter)")
    for r in table:
        tb = "%-22s" % (str(r["true"]))
        print("%-5d %s %s %-13s %.3f %s  %.2f | %5d  %5d  %5.1f %5.1f | %s" % (
            r["idx"], r["utc"], tb, r["fill"], r["yconf"],
            "Y" if r["park_rect"] else "n", r["park_conf"],
            r["S180"], r["S130"], r["meanS"], r["meanV"], r["gate"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
