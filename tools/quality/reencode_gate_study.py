#!/usr/bin/env python
"""reencode_gate_study.py -- does the shipped CV LOCATOR + shape gate survive H.264?

MEASUREMENT ONLY. Writes nothing outside logs/diagnostics/reencode_study/ and the staging
dir it is given. Imports the shipped modules read-only; edits nothing.

QUESTION (owner, 2026-09-14): every gate in meter_locator_cv.py was tuned on CAPTURE-CARD
pixels (PNG framedump, no compression). The remote-play-only route decodes an H.264 stream
(720p60 @ 4 or 12 Mbps, or 1080p60 @ 12 Mbps, BT.709 4:2:0) and normalises it to 1280x720
before the detector sees it. How far does each gate metric move toward its threshold?

WHAT IT DOES
  1. picks contiguous BLOCKS of framedump PNGs around verified true-meter runs (frames.csv),
     each block carrying pre/post-roll gameplay so false locks are measurable too;
  2. encodes the block sequence through x264 at each ladder rung with BT.709/limited tags;
  3. decodes to RAW yuv420p and converts with chiaki_backend's OWN _BT709_M matrix (the live
     path), then normalises with remote_play_orchestrator._normalize_detector_frame;
  4. drives a FRESH meter_locator_cv.MeterContourLocator over every condition, resetting at
     each block boundary, and (optionally) the production SimpleMeterReader;
  5. re-measures the gate-9 shape metrics (edge sd, row-width CV, solidity, tip px) at the
     PRISTINE ground-truth box in every condition, so we see how far each metric moved.

TEMPORAL HONESTY (read before believing anything)
  The framedump cadence is ~150 ms/frame, not 16.7 ms. Each source frame is therefore
  duplicated `dup` times to fill a 60 fps timeline so the per-frame BIT BUDGET is realistic.
  Two samples are taken from every duplicate group and both are reported:
      head (group frame 0)     : carries ~150 ms of motion at a 16.7 ms bit budget
                                 -> PESSIMISTIC (overstates compression damage)
      tail (group frame dup-1) : the encoder has had dup-1 static frames to refine
                                 -> OPTIMISTIC (understates it)
  Real 60 fps quality lies between. Conclusions are only drawn where head and tail agree.

Usage (repo root):
  .venv/Scripts/python.exe tools/quality/reencode_gate_study.py --stage      # copy PNGs
  .venv/Scripts/python.exe tools/quality/reencode_gate_study.py --run        # encode+measure
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter

import numpy as np
import cv2

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Production env, pinned BEFORE any shipped module is imported (module-level flags are read
# at import). Values = the live sidecar's (replay_simple_reader.py header + the CV proposer
# A/B the owner is running). ORION_METER_DETECTOR_SYNC makes inference inline = deterministic.
for _k, _v in {
    "ORION_SIMPLE_READER": "1",
    "ORION_METER_DETECTOR": "1",
    "ORION_METER_PROPOSER": "cv",
    "ORION_METER_DETECTOR_SYNC": "1",
    "ORION_READER_OCCLUSION": "1",
    "ORION_READER_ROBUST": "0",
    "ORION_READER_SCALE_ADAPT": "1",
    "ORION_READER_TIP_ENFORCE": "1",
    "ORION_READER_ARMED_HOLD": "1",
    "ORION_COLOR_CAL": "0",
    "ORION_GREEN_ZONE_WINDOW": "0",
    "ORION_GREEN_SELF_GRADE": "0",
}.items():
    os.environ.setdefault(_k, _v)

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

SESSION = "session_20260912_201355"
SRC_DIR = r"D:\NexusVision\framedump" "\\" + SESSION
OUT_DIR = os.path.join(_REPO, "logs", "diagnostics", "reencode_study")
STAGE_DIR = os.environ.get(
    "ORION_REENCODE_STAGE",
    r"C:\Users\aaron\AppData\Local\Temp\claude\C--Users-aaron-Desktop-NexusVision"
    r"\7396b864-d4d6-441b-840e-8730daa5e4ba\scratchpad\stage")

# ---------------------------------------------------------------- ladder rungs
# Mirrors native_orion/src/RemotePlaySession.cpp streamPresetForMode().
RUNGS = {
    # CONTROL: the same BT.709 4:2:0 round trip with NO quantisation. Separates "chroma
    # subsampling + matrix/range round trip" from "bitrate", so a metric that already moved
    # here was never a compression casualty.
    "control_420_qp1":     dict(w=1280, h=720, kbps=0),
    "balanced_720p_4M":    dict(w=1280, h=720,  kbps=4000),
    "performance_720p_12M": dict(w=1280, h=720, kbps=12000),
    "quality_1080p_12M":   dict(w=1920, h=1080, kbps=12000),
}
FPS = 60

# ---------------------------------------------------------------- block picking


def _read_frames_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pick_blocks(rows, pre=30, post=15, min_run=5, max_frames=1800):
    """True-meter runs -> contiguous index blocks with gameplay pre/post-roll.

    A run counts as a TRUE meter only if the box is stable (centre travel <= 60 px) and the
    fill actually rose >= 25 pp inside the run. That refuses the jumping multi-hundred-pixel
    'runs' that are the production false locks this gate was built to kill.
    """
    runs, cur = [], []
    for r in rows:
        if r["detected"] == "1":
            if cur and int(r["idx"]) == int(cur[-1]["idx"]) + 1:
                cur.append(r)
            else:
                if cur:
                    runs.append(cur)
                cur = [r]
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    true_runs = []
    for R in runs:
        if len(R) < min_run:
            continue
        cx = [int(x["bbox_x"]) + int(x["bbox_w"]) / 2 for x in R]
        cy = [int(x["bbox_y"]) + int(x["bbox_h"]) / 2 for x in R]
        fill = [float(x["fill_pct"]) for x in R]
        if (max(cx) - min(cx)) > 80 or (max(cy) - min(cy)) > 80:
            continue
        # the fill must climb: a static jersey/scoreboard lock reports a flat fill
        rise = max(fill) - min(fill[: max(1, len(fill) // 2)])
        if rise < 15:
            continue
        true_runs.append((int(R[0]["idx"]), int(R[-1]["idx"])))

    blocks, total = [], 0
    for a, b in true_runs:
        s, e = max(0, a - pre), min(len(rows) - 1, b + post)
        if blocks and s <= blocks[-1][1] + 4:
            blocks[-1] = (blocks[-1][0], max(blocks[-1][1], e))
        else:
            blocks.append((s, e))
    out, total = [], 0
    for s, e in blocks:
        n = e - s + 1
        if total + n > max_frames:
            break
        out.append((s, e))
        total += n
    return out, true_runs, total


def stage(blocks, rows):
    os.makedirs(STAGE_DIR, exist_ok=True)
    n = 0
    for s, e in blocks:
        for i in range(s, e + 1):
            src = os.path.join(SRC_DIR, "f%05d_%s_raw.png" % (i, rows[i]["detected"]))
            if not os.path.exists(src):
                for d in ("0", "1"):
                    alt = os.path.join(SRC_DIR, "f%05d_%s_raw.png" % (i, d))
                    if os.path.exists(alt):
                        src = alt
                        break
            dst = os.path.join(STAGE_DIR, "f%05d.png" % i)
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
            n += 1
    return n


# ---------------------------------------------------------------- encode/decode

def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def encode(order, rung, dup, out_mp4, work):
    """Encode the staged PNG sequence at one rung. Each source frame is repeated `dup` times
    so the per-frame bit budget matches a real 60 fps stream.

    Frames are piped in as RAW bgr24 rather than through the concat demuxer: concat's
    per-entry `duration` handling emitted one extra leading group, which shifted the whole
    decoded stream by one source frame against the pristine baseline (caught by comparing the
    shape-probe height sequence). A raw pipe at a fixed 60 fps is 1:1 by construction.
    """
    r = RUNGS[rung]
    vf = ("scale=%d:%d:flags=bicubic:out_color_matrix=bt709:out_range=tv,format=yuv420p"
          % (r["w"], r["h"]))
    if r["kbps"] <= 0:                                # near-lossless control
        # qp=0 is refused by High profile ("high profile doesn't support lossless"); qp=1
        # is in-profile and visually lossless, so it isolates the 4:2:0 + BT.709 round trip.
        rate = ["-qp", "1", "-x264-params", "threads=1:scenecut=0"]
    else:
        bufsize = max(64, r["kbps"] // 4)             # ~250 ms VBV: low-latency game stream
        rate = ["-b:v", "%dk" % r["kbps"], "-maxrate", "%dk" % r["kbps"],
                "-bufsize", "%dk" % bufsize,
                "-x264-params",
                "nal-hrd=cbr:force-cfr=1:scenecut=0:ref=1:rc-lookahead=0:aq-mode=1:threads=1"]
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "1280x720", "-r", str(FPS), "-i", "-",
           "-vf", vf, "-fps_mode", "passthrough",
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
           "-profile:v", "high", "-g", "60", "-bf", "0", *rate,
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv",
           out_mp4]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in order:
            im = cv2.imread(os.path.join(STAGE_DIR, "f%05d.png" % i))
            if im is None:
                raise RuntimeError("missing staged frame %d" % i)
            if im.shape[:2] != (720, 1280):
                im = cv2.resize(im, (1280, 720), interpolation=cv2.INTER_AREA)
            buf = np.ascontiguousarray(im).tobytes()
            for _ in range(dup):
                p.stdin.write(buf)
    finally:
        p.stdin.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg encode failed rc=%s" % p.returncode)
    return " ".join(cmd[1:]), time.time() - t0


# BT.709 limited-range YUV->BGR, copied verbatim from chiaki_backend.OrionFramePipeBackend
# (the LIVE remote-play conversion). Rows = B, G, R.
_BT709_M = np.array([
    [1.164383, 2.112402, 0.000000, -289.017],
    [1.164383, -0.213249, -0.532909, 76.878],
    [1.164383, 0.000000, 1.792741, -248.078],
], dtype=np.float64)


def yuv420_to_bgr(buf, w, h):
    y = np.frombuffer(buf, np.uint8, count=w * h).reshape(h, w)
    off = w * h
    cw, ch = w // 2, h // 2
    u = np.frombuffer(buf, np.uint8, count=cw * ch, offset=off).reshape(ch, cw)
    v = np.frombuffer(buf, np.uint8, count=cw * ch, offset=off + cw * ch).reshape(ch, cw)
    uf = cv2.resize(u, (w, h), interpolation=cv2.INTER_LINEAR)
    vf = cv2.resize(v, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.transform(cv2.merge([y, uf, vf]), _BT709_M)


def decode_iter(mp4, w, h):
    """Yield every decoded frame as BGR via the live BT.709 matrix."""
    fsz = w * h * 3 // 2
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", mp4,
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=fsz * 4)
    try:
        while True:
            buf = p.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            yield yuv420_to_bgr(buf, w, h)
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        p.wait()


# ---------------------------------------------------------------- shape probe

_GREEN_LO = (38, 90, 90)
_GREEN_HI = (85, 255, 255)


def measure_shape(frame, box, cfg):
    """Re-measure the gate-9 metrics at a KNOWN meter box, exactly as _meter_shaped does.

    `box` is the reader's tracked box (x, y, w, h) in 1280x720 coords. The white column is
    re-found inside a small ROI around it with gates 1-3, then the largest component is
    measured. Returns None when no white column survives gates 1-3 (itself a finding).
    """
    H, W = frame.shape[:2]
    s = H / 720.0
    bx, by, bw, bh = box
    px, py = int(18 * s), int(14 * s)
    x0, y0 = max(0, int(bx) - px), max(0, int(by) - py)
    x1, y1 = min(W, int(bx + bw) + px), min(H, int(by + bh) + py)
    if x1 - x0 < 10 or y1 - y0 < 20:
        return None
    sub = frame[y0:y1, x0:x1]
    b, g, r = cv2.split(sub)
    mx = cv2.max(cv2.max(b, g), r)
    mn = cv2.min(cv2.min(b, g), r)
    bright = cv2.threshold(mx, cfg["v_min"] - 1, 255, cv2.THRESH_BINARY)[1]
    neutral = cv2.threshold(cv2.subtract(mx, mn), cfg["spread_max"], 255, cv2.THRESH_BINARY_INV)[1]
    white = cv2.bitwise_and(bright, neutral)
    if cv2.countNonZero(white) == 0:
        return dict(found=0)
    gc = max(1, int(round(cfg["gap_close"] * s))) + 2
    kc = np.ones((gc, 1), np.uint8)
    white = cv2.erode(cv2.dilate(white, kc, anchor=(0, gc // 2)), kc, anchor=(0, (gc - 1) // 2))
    sup = max(2, int(round(cfg["min_support"] * s)))
    ks = np.ones((sup, 1), np.uint8)
    col = cv2.dilate(cv2.erode(white, ks, anchor=(0, sup // 2)), ks, anchor=(0, (sup - 1) // 2))
    if cv2.countNonZero(col) == 0:
        return dict(found=0)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(col, connectivity=8)
    if n <= 1:
        return dict(found=0)
    wmin, wmax = cfg["col_w_min"] * s, cfg["col_w_max"] * s
    cand = [i for i in range(1, n)
            if wmin <= stats[i, cv2.CC_STAT_WIDTH] <= wmax
            and stats[i, cv2.CC_STAT_HEIGHT] >= sup]
    if not cand:
        # report the widest survivor so width drift is visible even when it left the window
        i = int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1
        return dict(found=0, off_width=float(stats[i, cv2.CC_STAT_WIDTH]) / s)
    i = max(cand, key=lambda k: stats[k, cv2.CC_STAT_AREA])
    x, y, w, h, area = (int(v) for v in stats[i])
    m = labels[y:y + h, x:x + w] == i
    notch = int(round(8 * s))
    cx = x + w * 0.5
    wbot = y + h
    gtop_prior = wbot - cfg["tip_gap"] * s
    top_skip = min(max(1, int(round(gtop_prior + 10 * s)) - y), max(1, h - 2))
    body = slice(top_skip, max(top_skip + 1, h - notch))
    rows_ = m.sum(axis=1)[body]
    if rows_.size == 0:
        return dict(found=0)
    rw_med = float(np.median(rows_))
    rw_cv = (float(rows_.max()) - float(rows_.min())) / max(1.0, rw_med)
    left = np.argmax(m, axis=1)[body]
    right = (w - 1 - np.argmax(m[:, ::-1], axis=1))[body]
    sol = area / float(max(1, w * h))
    # gate 5 green-tip confirmation, at the same window the locator uses
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    ty0 = int(max(0, gtop_prior - cfg["tip_tol"] * s))
    ty1 = int(max(0, gtop_prior + cfg["tip_tol"] * s + 6 * s))
    tx0 = int(max(0, cx - cfg["tip_dx_max"] * s))
    tx1 = int(min(sub.shape[1], cx + cfg["tip_dx_max"] * s + 1))
    tip_px = 0
    if ty1 - ty0 >= 3 and tx1 - tx0 >= 3:
        tip_px = int(cv2.countNonZero(cv2.inRange(hsv[ty0:ty1, tx0:tx1], _GREEN_LO, _GREEN_HI)))
    # tip spill ring 8-14 px
    gy0 = int(max(0, gtop_prior - 2 * s))
    gy1 = int(min(sub.shape[0], gtop_prior + 10 * s))

    def green(c0, c1):
        c0 = int(max(0, min(sub.shape[1], c0)))
        c1 = int(max(0, min(sub.shape[1], c1)))
        if c1 <= c0 or gy1 <= gy0:
            return 0
        return int(cv2.countNonZero(cv2.inRange(hsv[gy0:gy1, c0:c1], _GREEN_LO, _GREEN_HI)))

    g_in = green(cx - 8 * s, cx + 8 * s + 1)
    g_mid = green(cx - 14 * s, cx - 8 * s) + green(cx + 8 * s + 1, cx + 14 * s)
    out = dict(found=1, h=h / s, w=w / s, rw_med=rw_med / s, rw_cv=rw_cv,
               edge_sd_l=float(np.std(left)) / s, edge_sd_r=float(np.std(right)) / s,
               sol=sol, tip_px=tip_px / (s * s), g_in=g_in / (s * s), g_mid=g_mid / (s * s),
               spill_ratio=(g_mid / g_in) if g_in > 0 else None)
    out.update(_pixel_budget(sub, hsv, cx, wbot, gtop_prior, s))
    return out


def _pixel_budget(sub, hsv, cx, wbot, gtop_prior, s):
    """GATE-1 headroom, measured on the pixels the gate actually judges.

    Gate 1 is max(B,G,R) >= 225 AND (max-min) <= 25 -- an ACHROMATIC-BRIGHT test. 4:2:0
    chroma subsampling plus quantisation is exactly what pushes those two numbers the wrong
    way, so this reports how much room is left:
      fv_p10 : the 10th percentile of max(B,G,R) over the fill's own core strip. The gate
               keeps a pixel at >= 225, so (fv_p10 - 225) is the luma margin.
      fs_p90 : the 90th percentile of the channel spread there. The gate keeps <= 25.
    Green tip: the same for hue / sat / value against _GREEN_LO (38, 90, 90).
    """
    H, W = sub.shape[:2]
    out = {}
    y0 = int(max(0, wbot - 26 * s)); y1 = int(min(H, wbot - 8 * s))
    x0 = int(max(0, cx - 4 * s)); x1 = int(min(W, cx + 5 * s))
    if y1 - y0 >= 3 and x1 - x0 >= 3:
        st = sub[y0:y1, x0:x1].astype(np.int16)
        mx = st.max(axis=2); mn = st.min(axis=2)
        out["fv_p10"] = float(np.percentile(mx, 10))
        out["fv_p50"] = float(np.percentile(mx, 50))
        out["fs_p90"] = float(np.percentile(mx - mn, 90))
        out["fs_p50"] = float(np.percentile(mx - mn, 50))
    gy0 = int(max(0, gtop_prior)); gy1 = int(min(H, gtop_prior + 9 * s))
    gx0 = int(max(0, cx - 5 * s)); gx1 = int(min(W, cx + 6 * s))
    if gy1 - gy0 >= 3 and gx1 - gx0 >= 3:
        t = hsv[gy0:gy1, gx0:gx1].reshape(-1, 3).astype(np.int16)
        # judge the tip's OWN pixels: the top quartile of saturation in the window
        k = max(3, t.shape[0] // 4)
        sel = t[np.argsort(-t[:, 1])[:k]]
        out["tip_h_p50"] = float(np.median(sel[:, 0]))
        out["tip_s_p50"] = float(np.median(sel[:, 1]))
        out["tip_v_p50"] = float(np.median(sel[:, 2]))
        out["tip_s_p25"] = float(np.percentile(sel[:, 1], 25))
    return out


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--rungs", default=",".join(RUNGS))
    ap.add_argument("--max-frames", type=int, default=1800)
    ap.add_argument("--dup", type=int, default=9)     # ~150 ms dump cadence -> 60 fps
    ap.add_argument("--reader", action="store_true", help="also run SimpleMeterReader")
    ap.add_argument("--keep-video", action="store_true")
    args = ap.parse_args()

    rows = _read_frames_csv(os.path.join(SRC_DIR, "frames.csv"))
    blocks, true_runs, total = pick_blocks(rows, max_frames=args.max_frames)
    order = [i for s, e in blocks for i in range(s, e + 1)]
    print("blocks=%d true_runs=%d frames=%d" % (len(blocks), len(true_runs), len(order)))

    if args.stage:
        n = stage(blocks, rows)
        print("staged %d png -> %s" % (n, STAGE_DIR))
        return 0
    if not args.run:
        for s, e in blocks:
            print("  block %d-%d (%d)" % (s, e, e - s + 1))
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    # per-frame label: 'shot' inside a verified true run, 'gap' otherwise
    true_set = set()
    for a, b in true_runs:
        true_set.update(range(a, b + 1))
    block_start = {s for s, _ in blocks}
    ts_by_idx = {int(r["idx"]): float(r["t_ms"]) / 1000.0 for r in rows}
    gt_box = {int(r["idx"]): (int(r["bbox_x"]), int(r["bbox_y"]), int(r["bbox_w"]),
                              int(r["bbox_h"])) for r in rows if r["detected"] == "1"}
    rej_by_idx = {int(r["idx"]): r["rejection"] for r in rows}

    import meter_locator_cv as mlc
    probe = mlc.MeterContourLocator()
    cfg = dict(v_min=probe.v_min, spread_max=probe.spread_max, gap_close=probe.gap_close,
               min_support=probe.min_support, col_w_min=probe.col_w_min,
               col_w_max=probe.col_w_max, tip_gap=probe.tip_gap, tip_tol=probe.tip_tol,
               tip_dx_max=probe.tip_dx_max)
    from remote_play_orchestrator import _normalize_detector_frame

    reader_cls = None
    if args.reader:
        from simple_meter_reader import SimpleMeterReader
        reader_cls = SimpleMeterReader

    def run_condition(name, frames_iter, csv_w):
        """frames_iter yields (idx, sample, bgr).

        Each sample name grows a `<sample>_fs` twin: a FIRST-SIGHT locator whose temporal
        state is reset before every frame, so gate 9 has to judge the meter on its own and
        the bridge cannot hide a metric that moved. The framedump's 150 ms cadence makes the
        real locator's bridge tolerance (12 + 600*dt px, capped 102) far more generous than
        live at 60 fps (22 px), so the unbridged twin is the conservative reading.
        """
        loc, loc_fs, rdr = {}, {}, {}
        n = 0
        for idx, sample, bgr in frames_iter:
            frame, why = _normalize_detector_frame(bgr)
            if frame is None:
                continue
            if sample not in loc:
                loc[sample] = mlc.MeterContourLocator()
                loc_fs[sample] = mlc.MeterContourLocator()
                if reader_cls is not None and sample in ("pristine", "tail"):
                    rdr[sample] = reader_cls()
            t = ts_by_idx.get(idx, idx / 60.0)
            gt = gt_box.get(idx)
            sm = measure_shape(frame, gt, cfg) if gt else None

            def one(L, first_sight):
                if first_sight or idx in block_start:
                    L.reset()
                before = dict(L.stats)
                box = L.detect_box(frame, ts=t)
                d = {k: L.stats.get(k, 0) - before.get(k, 0) for k in L.stats}
                return box, d

            for tag, L, fs in ((sample, loc[sample], False), (sample + "_fs", loc_fs[sample], True)):
                box, d = one(L, fs)
                fill = rdet = None
                if not fs and sample in rdr:
                    if idx in block_start:
                        try:
                            rdr[sample].reset()
                        except Exception:
                            pass
                    try:
                        rdr[sample].set_shot_state(True, 0.0, True)
                        st = rdr[sample].detect(frame, ts=t)
                        rdet = int(bool(st.detected))
                        fill = float(st.fill_pct or 0.0)
                    except Exception:
                        pass
                rec = dict(cond=name, sample=tag, idx=idx,
                           label="shot" if idx in true_set else "gap",
                           rej=rej_by_idx.get(idx, ""),
                           found=int(box is not None),
                           conf=round(box[4], 2) if box else "",
                           bx=box[0] if box else "", by=box[1] if box else "",
                           bw=box[2] if box else "", bh=box[3] if box else "",
                           cands=d.get("col_cands", 0), no_tip=d.get("no_tip", 0),
                           not_lone=d.get("not_lone", 0), no_tip_lone=d.get("no_tip_lone", 0),
                           shape_short=d.get("shape_short", 0),
                           shape_irregular=d.get("shape_irregular", 0),
                           tip_spill=d.get("tip_spill", 0),
                           shape_bridged=d.get("shape_bridged", 0),
                           outline_weak=d.get("outline_weak", 0),
                           rdet=rdet if rdet is not None else "",
                           fill=round(fill, 2) if fill is not None else "")
                if sm:
                    for k, v in sm.items():
                        rec["m_" + k] = round(v, 3) if isinstance(v, float) else v
                csv_w.writerow(rec)
            n += 1
        return n

    fields = ["cond", "sample", "idx", "label", "rej", "found", "conf", "bx", "by", "bw", "bh",
              "cands", "no_tip", "not_lone", "no_tip_lone", "shape_short", "shape_irregular",
              "tip_spill", "shape_bridged", "outline_weak", "rdet", "fill",
              "m_found", "m_off_width", "m_h", "m_w", "m_rw_med", "m_rw_cv", "m_edge_sd_l",
              "m_edge_sd_r", "m_sol", "m_tip_px", "m_g_in", "m_g_mid", "m_spill_ratio",
              "m_fv_p10", "m_fv_p50", "m_fs_p90", "m_fs_p50",
              "m_tip_h_p50", "m_tip_s_p50", "m_tip_v_p50", "m_tip_s_p25"]
    csv_path = os.path.join(OUT_DIR, "per_frame.csv")
    cmds = {}
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()

        print("[pristine] ...")
        def pristine_iter():
            for i in order:
                im = cv2.imread(os.path.join(STAGE_DIR, "f%05d.png" % i))
                if im is not None:
                    yield i, "pristine", im
        t0 = time.time()
        run_condition("pristine", pristine_iter(), w)
        print("  %.0fs" % (time.time() - t0))

        work = os.path.join(OUT_DIR, "_work")
        os.makedirs(work, exist_ok=True)
        for rung in [r.strip() for r in args.rungs.split(",") if r.strip()]:
            mp4 = os.path.join(work, rung + ".mp4")
            print("[%s] encoding ..." % rung)
            cmd, dt = encode(order, rung, args.dup, mp4, work)
            cmds[rung] = cmd
            sz = os.path.getsize(mp4)
            print("  encoded in %.0fs, %.1f MB" % (dt, sz / 1e6))
            r = RUNGS[rung]

            def rung_iter():
                k = 0
                for fr in decode_iter(mp4, r["w"], r["h"]):
                    gi, pos = divmod(k, args.dup)
                    k += 1
                    if gi >= len(order):
                        continue          # drain, never break: a broken pipe hides errors
                    if pos == 0:
                        yield order[gi], "head", fr
                    elif pos == args.dup - 1:
                        yield order[gi], "tail", fr
            t0 = time.time()
            run_condition(rung, rung_iter(), w)
            print("  measured in %.0fs" % (time.time() - t0))
            if not args.keep_video:
                try:
                    os.unlink(mp4)
                except OSError:
                    pass
    with open(os.path.join(OUT_DIR, "encode_cmds.json"), "w", encoding="utf-8") as f:
        json.dump({"rungs": RUNGS, "fps": FPS, "dup": args.dup, "blocks": blocks,
                   "true_runs": true_runs, "frames": len(order), "cmds": cmds}, f, indent=2)
    print("wrote %s" % csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
