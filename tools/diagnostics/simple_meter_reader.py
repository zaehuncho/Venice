#!/usr/bin/env python
"""
simple_meter_reader.py -- READ-ONLY candidate meter reader + head-to-head harness.

HYPOTHESIS (to prove/disprove): because the bot KNOWS when a shot is happening (it holds
Square) and the meter is player-attached, a SHOT-GATED, REGION-ANCHORED, SIMPLE reader can
match Orion's multi-stage serving chain (YOLO locator -> loc_mem coast -> _ParkTracker
rise-confirm -> wide color scan -> _StabilityValidator corroboration -> sub-pixel reader ->
MeterBoxKalman) on accuracy at a fraction of the complexity and cost.

The candidate is modelled on TWO SHIPPING 2K tools (grounded, not invented):
  * 2k_Vision (docs/reference/2kvision_reverse_engineered.md): HSV/BGR inRange -> findContours
    -> per-style size/aspect gate + CONFIDENCE-DECAY lock (no warmup: trust a strong frame
    immediately, decay per frame). Arrow2 = tall thin bar. Desktop/2kMETER.py: fill via a
    coloured contour anchored by a full-track cap detection -> fill = h/total.
  * Starzen/StarVision (Starzen RE/STARZEN_RE_REPORT.md): colour->contour->shape-gate->classify,
    PLUS a cv2.matchTemplate FAST-PATH in a small window around the last-known meter location
    (the efficiency trick), PLUS pose-based SHOT-GATING (only detect/fire when actually shooting).

Candidate architecture (this file), 3 stages vs the chain's ~8:
  1. SHOT-GATE  : a tall thin red column exists in the plausible player band? (offline proxy for
                  the bot's arm/hold state; LIVE this gates on the held-trigger + the v9 pose model).
                  No column -> "(no meter)". This is the entire false-lock guard.
  2. LOCALISE   : cv2.matchTemplate NCC fast-path around the last box when LOCKED (cheap, ~sub-ms);
                  full colour->contour->size/aspect gate scan in the band only on (RE)ACQUIRE.
                  A CONFIDENCE-DECAY lock (no N-frame warmup) coasts a brief miss, drops on decay.
  3. READ       : full track anchored by the GREEN make-window cap (the 2kMETER gray-cap idea,
                  adapted -- our empty track is translucent-blue, its "cap" is the green tip) and
                  the red floor. fill% = red_extent / (floor - green). TWO fill estimates are
                  emitted every frame: COARSE contour-height (2kMETER/Starzen style) and SUB-PIXEL
                  edge crossing (Orion's genuine add) -- so we can MEASURE whether sub-pixel
                  materially beats coarse contour-height fill.

Writes ONLY under tools/diagnostics/ and logs/diagnostics/. Never touches meter_detector.py.
Reuses replay_framedump.build_detector to run the CURRENT chain on the exact same frames
(cached to logs/diagnostics/simple_vs_chain/chaincache_*.jsonl so a rerun is cheap).

Usage:
  .venv311/Scripts/python.exe tools/diagnostics/simple_meter_reader.py \
      --session logs/diagnostics/framedump/session_20260704_210801
"""
import argparse
import glob
import json
import os
import re
import sys
import time as _time
from collections import Counter

import numpy as np
import cv2

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# =========================================================================== #
#  THE CANDIDATE READER  (this is the whole thing -- count the lines)
# =========================================================================== #
class SimpleMeterReader:
    """Shot-gated, matchTemplate-tracked, confidence-decay, green-cap-anchored fill reader
    for the Red/Arrow2 park meter. Grounded on 2k_Vision + Starzen (see module docstring)."""

    # --- meter geometry priors: EXACT 2k_Vision Arrow2.json params (@1080p) ---
    # search band 5/250/1915/770 -> right trimmed to 1815 to drop the STATIC NBA-logo banner
    # (x~1846-1902) which yields a width-30 red contour that the shape gate alone can't reject.
    BAND = (5, 250, 1815, 770)        # x0,y0,x1,y1 px
    W_MIN, W_MAX = 20, 34             # Arrow2 contour width 23-30 (+/- capture slack)
    H_ACQ = 33                        # Arrow2 contour height min 33
    H_MAX = 165                       # Arrow2 contour height max 165
    H_HOLD = 12                       # relaxed min to HOLD a tracked lock (deflate tail)
    AR_MIN = 1.8
    # confidence-decay lock: EXACT Arrow2 model {initial 1.0, minimum 0.95, rate 0.01}. No warmup.
    CONF_INIT, CONF_MIN, CONF_RATE = 1.0, 0.95, 0.01
    NCC_LOCK = 0.60                   # grayscale matchTemplate TM_CCOEFF_NORMED (2k26.py uses 0.8)
    ROI_PAD_X, ROI_PAD_UP, ROI_PAD_DN = 30, 100, 22
    # RED fill: EXACT Arrow2 BGR [0,0,220]-[60,60,255] (validated: gives the meter at width ~24-30)
    _RED_LO, _RED_HI = (0, 0, 220), (60, 60, 255)
    _G = ((38, 90, 90), (85, 255, 255))        # green make-window cap (2k26 green ~[35,100,100]-[85,255,255])

    def __init__(self, frame_w, frame_h):
        self.W, self.H = frame_w, frame_h
        self.conf = 0.0
        self.box = None            # last red-column bbox (x,y,w,h) full-frame
        self.tmpl = None           # last meter GRAYSCALE crop (matchTemplate fast-path template)
        self.tmpl_wh = None
        self._fillable_hist = []   # rolling green-cap fillable heights (fallback anchor)
        self.last_fill = 0.0       # last good reading (held during a coast, like the chain)
        self.last_coarse = 0.0
        self.last_tbox = [0, 0, 0, 0]

    # -- masks (EXACT 2k_Vision BGR red; HSV green) --
    def _redmask(self, bgr):
        return cv2.inRange(bgr, np.array(self._RED_LO, np.uint8), np.array(self._RED_HI, np.uint8))

    def _greenmask(self, hsv):
        return cv2.inRange(hsv, np.array(self._G[0], np.uint8), np.array(self._G[1], np.uint8))

    # -- STAGE 2a: full BGR-colour->contour->size/aspect gate scan (acquire / re-acquire) --
    def _scan(self, frame, region, hmin):
        x0, y0, x1, y1 = [int(v) for v in region]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.W, x1), min(self.H, y1)
        if x1 - x0 < self.W_MIN or y1 - y0 < hmin:
            return None, 0.0
        sub = frame[y0:y1, x0:x1]
        mask = cv2.morphologyEx(self._redmask(sub), cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best, best_score = None, float("-inf")
        # proximity prior ONLY when a prior lock exists (rescan); on a COLD acquire there is no
        # meaningful reference -> a distance penalty would wrongly reject off-centre (slid) meters.
        cx_ref = (self.box[0] + self.box[2] * 0.5) if self.box else None
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (self.W_MIN <= w <= self.W_MAX) or not (hmin <= h <= self.H_MAX):
                continue
            if h / float(max(1, w)) < self.AR_MIN:
                continue
            red_frac = cv2.countNonZero(mask[y:y + h, x:x + w]) / float(max(1, w * h))
            if red_frac < 0.45:
                continue
            prox = -abs((x0 + x + w * 0.5) - cx_ref) if cx_ref is not None else 0.0
            score = h + red_frac * 20 + 0.15 * prox
            if score > best_score:
                best_score, best = score, (x0 + x, y0 + y, w, h)
        if best is None:
            return None, 0.0
        return best, self.CONF_INIT      # a clean gated frame is trusted at full confidence (no warmup)

    # -- STAGE 2b: GRAYSCALE matchTemplate NCC fast-path around last box (locked) --
    def _track(self, frame):
        if self.tmpl is None or self.box is None:
            return None
        tw, th = self.tmpl_wh
        x, y, w, h = self.box
        x0 = max(0, x - self.ROI_PAD_X); y0 = max(0, y - self.ROI_PAD_UP)
        x1 = min(self.W, x + w + self.ROI_PAD_X); y1 = min(self.H, y + h + self.ROI_PAD_DN)
        win = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if win.shape[0] < th or win.shape[1] < tw:
            return None
        res = cv2.matchTemplate(win, self.tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx < self.NCC_LOCK:
            return None
        nx, ny = x0 + loc[0], y0 + loc[1]
        # re-measure the red-column extent at the tracked spot (fill grows/shrinks each frame)
        col, _c = self._scan(frame, (nx - 6, ny - self.ROI_PAD_UP, nx + tw + 6, ny + th + 6), self.H_HOLD)
        return col if col is not None else (nx, ny, tw, th)

    # -- STAGE 3: green-cap-anchored full-track fill. Returns (coarse, subpix, trackbox) --
    def _read_fill(self, frame, col):
        cx, cw = col[0], col[2]
        floor = col[1] + col[3]                                  # red bottom = track floor
        x0 = max(0, cx - 3); x1 = min(self.W, cx + cw + 3)
        top_search = max(0, floor - 240)                          # look up to 240px above the floor
        strip = frame[top_search:min(self.H, floor + 3), x0:x1]
        ph, pw = strip.shape[:2]
        if ph < 8 or pw < 3:
            return 0.0, 0.0, (x0, top_search, x1 - x0, ph)
        red = self._redmask(strip)                                # EXACT BGR red
        grn = self._greenmask(cv2.cvtColor(strip, cv2.COLOR_BGR2HSV))
        red_row = red.mean(axis=1) / 255.0
        grn_row = grn.mean(axis=1) / 255.0
        # --- full-track cap: the GREEN make-window row is the top anchor (the 2kMETER gray-cap idea,
        #     adapted: our empty track is translucent-blue and the whole arena is mid-gray so the
        #     literal gray-cap [80-120] anchors the COURT not the meter -> total ~900px -> ~10%% fill
        #     when true fill is ~80%%; the green tip is present at every fill level -> reliable cap). ---
        gidx = np.flatnonzero(grn_row >= 0.15)
        if gidx.size:
            green_bottom = int(gidx[-1])                          # bottom of green tip = top of fillable area
            fillable_h = float(ph - green_bottom)
            self._fillable_hist.append(fillable_h)
            if len(self._fillable_hist) > 60:
                self._fillable_hist = self._fillable_hist[-60:]
        elif self._fillable_hist:                                 # green briefly missing -> rolling median
            fillable_h = float(np.median(self._fillable_hist))
            green_bottom = max(0, ph - int(fillable_h))
        else:
            green_bottom, fillable_h = 0, float(ph)
        fillable_h = float(min(max(fillable_h, 6.0), 185.0))       # Arrow2 track ~130-165px; guard outliers
        green_bottom = max(0, ph - int(fillable_h))
        if fillable_h < 6:
            return 0.0, 0.0, (x0, top_search, pw, ph)
        # --- red extent (only BELOW the green cap) ---
        ridx = np.flatnonzero(red_row[green_bottom:] >= 0.20)
        if ridx.size == 0:
            return 0.0, 0.0, (x0, top_search, pw, ph)
        red_top_coarse = green_bottom + int(ridx[0])              # integer top edge (contour-height style)
        # COARSE fill = red contour height / fillable track height (2kMETER / Starzen style)
        coarse = (ph - red_top_coarse) / fillable_h * 100.0
        # SUB-PIXEL fill = smoothed threshold crossing of the red profile (Orion's add)
        sm = np.convolve(red_row, np.array([0.25, 0.5, 0.25]), mode="same")
        thr = 0.20
        t = red_top_coarse
        if t > 0 and sm[t] > sm[t - 1]:
            frac = (thr - sm[t - 1]) / max(1e-6, sm[t] - sm[t - 1])
            sub_top = t - (1.0 - min(1.0, max(0.0, frac)))
        else:
            sub_top = float(t)
        subpix = (ph - sub_top) / fillable_h * 100.0
        clamp = lambda v: max(0.0, min(100.0, v))
        trackbox = (x0, top_search + green_bottom, pw, int(fillable_h))
        return round(clamp(coarse), 2), round(clamp(subpix), 2), trackbox

    # -- the ENTIRE reader --
    def read(self, frame):
        locked = self.conf >= self.CONF_MIN and self.box is not None
        col = self._track(frame) if locked else None              # STAGE 2b fast-path when locked
        stage = "track"
        if col is None:                                           # STAGE 2a (re)acquire: colour->contour->gate
            if locked:
                x, y, w, h = self.box
                region = (x - self.ROI_PAD_X, y - self.ROI_PAD_UP, x + w + self.ROI_PAD_X, y + h + self.ROI_PAD_DN)
                col, _c = self._scan(frame, region, self.H_HOLD)
                stage = "rescan"
            else:
                col, _c = self._scan(frame, self.BAND, self.H_ACQ)
                stage = "acquire"
            if col is None:                                       # miss -> confidence decays (rate 0.01), no warmup
                if locked:
                    self.conf -= self.CONF_RATE
                if self.conf < self.CONF_MIN:
                    self.conf = 0.0; self.box = None; self.tmpl = None
                    return {"detected": False, "fill": 0.0, "fill_coarse": 0.0, "bbox": [0, 0, 0, 0], "stage": "no_meter"}
                # coast (still >= CONF_MIN): HOLD the last good reading (the column vanished this
                # frame; re-reading a missing column would read 0). Mirrors the chain's loc_mem coast.
                return {"detected": True, "fill": self.last_fill, "fill_coarse": self.last_coarse,
                        "bbox": list(self.last_tbox), "stage": "coast"}
        # a clean gated/tracked frame -> reset confidence to full (trust a strong frame immediately)
        self.conf = self.CONF_INIT
        coarse, subpix, tbox = self._read_fill(frame, col)
        self.box = col
        self.last_fill, self.last_coarse, self.last_tbox = subpix, coarse, [int(v) for v in tbox]
        cx, cy, cw, ch = col
        cr = frame[max(0, cy):min(self.H, cy + ch), max(0, cx):min(self.W, cx + cw)]
        if cr.shape[0] >= 6 and cr.shape[1] >= 3:
            self.tmpl = cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY); self.tmpl_wh = (cr.shape[1], cr.shape[0])
        return {"detected": True, "fill": subpix, "fill_coarse": coarse,
                "bbox": [int(v) for v in tbox], "stage": stage}


# =========================================================================== #
#  HEAD-TO-HEAD HARNESS
# =========================================================================== #
_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")


def list_frames(session_dir):
    out = []
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def load_chain_cache(path):
    if not os.path.exists(path):
        return None
    cache = {}
    for line in open(path):
        r = json.loads(line)
        cache[(r["idx"], r["flag"])] = r
    return cache


def _pava(y):
    """Pool-adjacent-violators: least-squares non-decreasing fit."""
    y = np.asarray(y, float)
    vals, wts = list(y), [1.0] * len(y)
    out_v, out_w = [], []
    for v, w in zip(vals, wts):
        while out_v and out_v[-1] > v:
            pv, pw = out_v.pop(), out_w.pop()
            v = (pv * pw + v * w) / (pw + w); w = pw + w
        out_v.append(v); out_w.append(w)
    res = []
    for v, w in zip(out_v, out_w):
        res.extend([v] * int(w))
    return np.array(res[:len(y)])


def true_shots(records, min_core=5, max_gap=3, min_peak=70.0):
    """TRUE shots by CONSENSUS so chain-only decor locks / simple-only blips never count as shots.
    core = run of >=min_core frames where BOTH detect; expanded over the surrounding EITHER-detect
    span (rise/deflate edges) up to a gap of max_gap; requires consensus peak >= min_peak."""
    n = len(records)
    de = [bool(r["chain_det"] or r["simple_det"]) for r in records]
    db = [bool(r["chain_det"] and r["simple_det"]) for r in records]
    shots = []
    i = 0
    while i < n:
        if not db[i]:
            i += 1; continue
        j = i
        while j < n and db[j]:
            j += 1
        if j - i >= min_core:
            a, b = i, j
            gap = 0
            while a > 0 and (de[a - 1] or gap < max_gap):
                a -= 1; gap = 0 if de[a] else gap + 1
            gap = 0
            while b < n and (de[b] or gap < max_gap):
                gap = 0 if de[b] else gap + 1; b += 1
            peak = max(max(r["chain_fill"] if r["chain_det"] else 0.0,
                           r["simple_fill"] if r["simple_det"] else 0.0) for r in records[a:b])
            if peak >= min_peak:
                if shots and a <= shots[-1][1]:
                    shots[-1] = (shots[-1][0], b)
                else:
                    shots.append((a, b))
            i = max(j, b)
        else:
            i = j
    return shots


def build_gt(records):
    """Per-shot GT = isotonic (monotone non-decreasing) fit of the per-frame CONSENSUS fill on the
    rise (start..peak), over TRUE shots only. Built symmetrically -> favours neither reader."""
    gt = {}
    for i, j in true_shots(records):
        seg = records[i:j]
        cons = []
        for r in seg:
            v = []
            if r["chain_det"]:
                v.append(r["chain_fill"])
            if r["simple_det"]:
                v.append(r["simple_fill"])
            cons.append(float(np.mean(v)) if v else np.nan)
        cons = np.array(cons)
        if np.all(np.isnan(cons)):
            continue
        peak_k = int(np.nanargmax(cons))
        rise = cons[:peak_k + 1].copy()
        good = ~np.isnan(rise)
        if good.sum() < 3:
            continue
        iso = _pava(np.where(good, rise, np.interp(np.arange(len(rise)), np.flatnonzero(good), rise[good])))
        for k in range(peak_k + 1):
            gt[seg[k]["idx_ord"]] = (float(iso[k]), "peak" if k == peak_k else "rise")
    return gt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=os.path.join(
        REPO, "logs", "diagnostics", "framedump", "session_20260704_210801"))
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "diagnostics", "simple_vs_chain"))
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--limit", type=int, default=-1)
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--use-cache", action="store_true", help="load chain results from chaincache_*.jsonl (fast)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    frames = list_frames(args.session)
    if args.step > 1:
        frames = frames[::args.step]
    if args.limit > 0:
        frames = frames[:args.limit]
    sess = os.path.basename(args.session)
    print("session=%s  frames=%d" % (sess, len(frames)))

    cache = load_chain_cache(os.path.join(args.out, "chaincache_%s.jsonl" % sess)) if args.use_cache else None
    det = RF = None
    if not args.no_chain and cache is None:
        import replay_framedump as RF
        RF._install_clock()
        det, info = RF.build_detector()
        print("chain build:", {k: info[k] for k in ("locator_loaded", "style", "meter_color")})
    elif cache is not None:
        print("using chain CACHE (%d rows)" % len(cache))

    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    H, W = b0.shape[:2]
    simple = SimpleMeterReader(W, H)

    records = []
    chain_ms = simple_ms = 0.0
    perf = (RF._REAL_PERF if (RF is not None and hasattr(RF, "_REAL_PERF")) else _time.perf_counter)
    stage_ms = Counter(); stage_n = Counter()
    have_chain = (det is not None) or (cache is not None)
    chain_cache_out = []
    for ordi, (idx, flag, path) in enumerate(frames):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        c = {"chain_det": False, "chain_fill": 0.0, "chain_bbox": [0, 0, 0, 0],
             "chain_rise": "", "chain_conf": 0.0, "chain_rej": ""}
        if cache is not None:
            hit = cache.get((idx, flag))
            if hit:
                c = {k: hit[k] for k in c}
        elif det is not None:
            RF._CLOCK.tick()
            t0 = perf(); res = det.detect(bgr); chain_ms += (perf() - t0) * 1000.0
            c = {"chain_det": bool(res.detected), "chain_fill": round(float(res.fill_pct), 2),
                 "chain_bbox": [int(v) for v in (res.bbox or [0, 0, 0, 0])],
                 "chain_rise": res.rise_state or "", "chain_conf": round(float(res.confidence), 3),
                 "chain_rej": res.rejection_reason or ""}
            chain_cache_out.append({"idx": idx, "flag": flag, **c})
        t0 = perf(); s = simple.read(bgr); dms = (perf() - t0) * 1000.0
        simple_ms += dms; stage_ms[s["stage"]] += dms; stage_n[s["stage"]] += 1
        records.append({"idx_ord": ordi, "idx": idx, "flag": flag, **c,
                        "simple_det": s["detected"], "simple_fill": s["fill"],
                        "simple_coarse": s["fill_coarse"], "simple_bbox": s["bbox"],
                        "simple_stage": s["stage"], "simple_ms": round(dms, 3)})

    jp = os.path.join(args.out, "hh_%s.jsonl" % sess)
    with open(jp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    if chain_cache_out:
        with open(os.path.join(args.out, "chaincache_%s.jsonl" % sess), "w") as f:
            for r in chain_cache_out:
                f.write(json.dumps(r) + "\n")

    print("\nsimple stage cost:", {k: "%.3fms x%d" % (stage_ms[k] / max(1, stage_n[k]), stage_n[k]) for k in stage_n})
    _report(records, sess, chain_ms, simple_ms, have_chain, jp)
    return 0


def _report(records, sess, chain_ms, simple_ms, have_chain, jp):
    n = len(records)
    print("\n================= HEAD-TO-HEAD: %s (%d frames) =================" % (sess, n))
    print("wrote", jp)
    if not have_chain:
        sd = sum(r["simple_det"] for r in records)
        print("simple-only: detected %d/%d (%.1f%%)  %.3f ms/frame" % (sd, n, 100.0 * sd / n, simple_ms / n))
        return
    lk = [r["simple_ms"] for r in records if r["simple_stage"] in ("track", "rescan")]
    print("\n[SPEED]  chain %.2f ms/frame(CPU)   simple %.3f ms/frame(all)   simple %.3f ms(locked, n=%d)" % (
        chain_ms / n if chain_ms else float("nan"), simple_ms / n,
        (np.mean(lk) if lk else float("nan")), len(lk)))

    cd = sum(r["chain_det"] for r in records); sd = sum(r["simple_det"] for r in records)
    both = sum(r["chain_det"] and r["simple_det"] for r in records)
    neither = sum((not r["chain_det"]) and (not r["simple_det"]) for r in records)
    print("\n[DETECTION]  chain=%d  simple=%d  (of %d)" % (cd, sd, n))
    print("  both=%d neither=%d chain-only=%d simple-only=%d  frame-agreement=%.1f%%" % (
        both, neither, sum(r["chain_det"] and not r["simple_det"] for r in records),
        sum(r["simple_det"] and not r["chain_det"] for r in records), 100.0 * (both + neither) / n))

    for tag, key in (("SUB-PIXEL", "simple_fill"), ("COARSE contour-height", "simple_coarse")):
        diffs = np.array([abs(r["chain_fill"] - r[key]) for r in records if r["chain_det"] and r["simple_det"]])
        if diffs.size:
            print("\n[FILL AGREEMENT vs chain -- %s] (both detect, n=%d)" % (tag, diffs.size))
            print("  |simple-chain| mean=%.1f median=%.1f p90=%.1f  <=3pp:%.0f%% <=5pp:%.0f%% <=10pp:%.0f%%" % (
                diffs.mean(), np.median(diffs), np.percentile(diffs, 90),
                100 * (diffs <= 3).mean(), 100 * (diffs <= 5).mean(), 100 * (diffs <= 10).mean()))

    gt = build_gt(records)
    by = {r["idx_ord"]: r for r in records}
    for label, want in (("RISE", {"rise"}), ("PEAK", {"peak"}), ("RISE+PEAK", {"rise", "peak"})):
        ce, se, se2 = [], [], []
        for oid, (g, ph) in gt.items():
            if ph not in want:
                continue
            r = by[oid]
            if r["chain_det"]:
                ce.append(abs(r["chain_fill"] - g))
            if r["simple_det"]:
                se.append(abs(r["simple_fill"] - g)); se2.append(abs(r["simple_coarse"] - g))
        ng = sum(1 for _, (_, p) in gt.items() if p in want)
        if ce or se:
            f = lambda a: (np.mean(a), np.sqrt(np.mean(np.square(a))), len(a)) if a else (float("nan"),) * 3
            print("\n[vs GROUND-TRUTH %s]  (monotone-rise consensus fit, %d GT frames)" % (label, ng))
            print("  chain          MAE=%.1fpp RMSE=%.1f (n=%d)" % f(ce))
            print("  simple subpix  MAE=%.1fpp RMSE=%.1f (n=%d)" % f(se))
            print("  simple coarse  MAE=%.1fpp RMSE=%.1f (n=%d)" % f(se2))
    _robustness(records)


def _robustness(records):
    n = len(records)
    shots = true_shots(records)
    print("\n[ROBUSTNESS]  %d TRUE (consensus) shots" % len(shots))
    c_drop = s_drop = tot = c_mg = s_mg = 0
    for a, b in shots:
        cg = sg = 0
        for k in range(a, b):
            tot += 1
            if not records[k]["chain_det"]:
                c_drop += 1; cg += 1; c_mg = max(c_mg, cg)
            else:
                cg = 0
            if not records[k]["simple_det"]:
                s_drop += 1; sg += 1; s_mg = max(s_mg, sg)
            else:
                sg = 0
    if tot:
        print("  within-shot dropout: chain %d/%d (%.1f%%) maxgap=%d | simple %d/%d (%.1f%%) maxgap=%d" % (
            c_drop, tot, 100.0 * c_drop / tot, c_mg, s_drop, tot, 100.0 * s_drop / tot, s_mg))
    inshot = np.zeros(n, bool)
    for a, b in shots:
        inshot[a:b] = True
    off = int((~inshot).sum())
    c_fl = sum(1 for k in range(n) if not inshot[k] and records[k]["chain_det"])
    s_fl = sum(1 for k in range(n) if not inshot[k] and records[k]["simple_det"])

    def episodes(key):
        e = 0; run = False
        for k in range(n):
            on = (not inshot[k]) and records[k][key]
            if on and not run:
                e += 1
            run = on
        return e
    print("  off-shot false-lock FRAMES:    chain %d/%d (%.2f%%) | simple %d/%d (%.2f%%)" % (
        c_fl, off, 100.0 * c_fl / max(1, off), s_fl, off, 100.0 * s_fl / max(1, off)))
    print("  off-shot false-lock EPISODES:  chain %d | simple %d  (spot-checked chain locks = red decor: City kiosk/2K logo/banners)" % (
        episodes("chain_det"), episodes("simple_det")))

    def glitches(det_key, fill_key):
        g = 0
        for a, b in shots:
            prev = None; peaked = False
            pk = max([records[k][fill_key] for k in range(a, b) if records[k][det_key]] or [0])
            for k in range(a, b):
                if not records[k][det_key]:
                    prev = None; continue
                f = records[k][fill_key]
                if f >= pk - 5:
                    peaked = True
                if prev is not None and not peaked and (prev - f) > 20:
                    g += 1
                prev = f
        return g
    print("  mid-rise glitches (>20pp backward drop): chain %d | simple %d" % (
        glitches("chain_det", "chain_fill"), glitches("simple_det", "simple_fill")))


if __name__ == "__main__":
    sys.exit(main())
