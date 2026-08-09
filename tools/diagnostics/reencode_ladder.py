#!/usr/bin/env python
"""reencode_ladder.py -- M1 feasibility probe: what does each Remote Play rung do to the meter?

Sequence-encodes an existing 1080p framedump session through the REAL RemotePlaySession
bandwidth ladder (x264/x265 zerolatency CBR, no-scenecut, infinite GOP + intra-refresh,
~2-frame VBV -- the closest ffmpeg proxy to the PS5's low-latency hardware encoder), decodes
it back at the rung's native resolution, runs the PRODUCTION SimpleMeterReader on the decoded
frames, and reports per rung exactly what dies:

  * detection vs the pristine baseline (both / pristine-only / rung-only), per rise_state phase
  * fill |delta| on frames both detect (mean / median / p90)
  * RED inRange + GREEN tip mask pixel SURVIVAL inside the pristine reader's box (the direct
    measurement of "compression kills chroma acquisition")

HONESTY CLAUSES (read before believing numbers):
  * Framedumps are throttled (~60-75 ms/frame, NOT 60fps), so consecutive frames carry ~4x the
    real 60fps motion. Two bracketing modes are run per rung:
      - seq: dump frames 1:1, declared at the rung fps  -> temporal artifacts OVERSTATED (~4x motion)
      - dup: each frame repeated to the rung fps        -> per-frame bit budget realistic, motion bursty
    Truth lies between; the dual-capture rig (M5) is the real source. This probe is for RELATIVE
    (rung vs rung, chroma vs luma) conclusions and for building the M2 test corpus.
  * x264/x265 != the PS5 hardware encoder (different RDO/AQ). Augmentation-grade only.

Usage (repo root, .venv311):
  python tools/diagnostics/reencode_ladder.py --session session_20260704_210801 \
      [--rungs Performance,Balanced] [--modes seq,dup] [--max-frames N] [--keep-video]
Outputs: logs/diagnostics/reencode_ladder/<session>/report.txt + report.json (+ .mp4 with --keep-video)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
import cv2

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from simple_meter_reader import SimpleMeterReader  # noqa: E402  (production reader)

# The REAL ladder, mirrored from native_orion/src/RemotePlaySession.cpp streamPresetForMode().
RUNGS = {
    "Quality":      dict(w=1920, h=1080, fps=60, codec="libx264", kbps=12000),
    "Performance":  dict(w=1280, h=720,  fps=60, codec="libx264", kbps=12000),
    "Balanced":     dict(w=1280, h=720,  fps=60, codec="libx264", kbps=4000),
    "LowBandwidth": dict(w=960,  h=540,  fps=60, codec="libx264", kbps=2500),
    "UltraLow":     dict(w=640,  h=360,  fps=30, codec="libx264", kbps=1200),
}

_FRAME_RE = re.compile(r"f(\d+)_([01])_raw\.png$")


def list_frames(session_dir: str, max_frames: int = 0):
    out = []
    for p in sorted(glob.glob(os.path.join(session_dir, "f*_raw.png"))):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), p))
    out.sort()
    if max_frames:
        out = out[:max_frames]
    return out


def encode_rung(frames, rung, mode, out_mp4):
    """Encode the PNG sequence through one rung. mode='seq' (1:1) or 'dup' (repeat each frame
    to the rung fps so the per-frame bit budget matches reality)."""
    r = RUNGS[rung]
    # dup factor: dump cadence (~15fps) -> rung fps. seq mode = 1.
    dup = max(1, round(r["fps"] / 15.0)) if mode == "dup" else 1
    vbv_buf = max(2 * r["kbps"] // r["fps"], 40)   # ~2-frame VBV, floor for sanity
    # threads=1: multithreaded x264/x265 rate control is NON-DETERMINISTIC, which made
    # consecutive A/B harness runs incomparable (frozen-reader recall swung 96->50% at
    # the UltraLow rung with identical code). Single-threaded encode is bit-reproducible;
    # the ~2-3x slower encode is irrelevant at probe scale.
    if r["codec"] == "libx264":
        cparams = (f"nal-hrd=cbr:force-cfr=1:scenecut=0:keyint=infinite:intra-refresh=1:"
                   f"bitrate={r['kbps']}:vbv-maxrate={r['kbps']}:vbv-bufsize={vbv_buf}:"
                   f"ref=1:bframes=0:rc-lookahead=0:slices=4:aq-mode=1:threads=1")
        cargs = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-x264-params", cparams]
    else:
        cparams = (f"keyint=-1:intra-refresh=1:bframes=0:rc-lookahead=0:scenecut=0:"
                   f"bitrate={r['kbps']}:vbv-maxrate={r['kbps']}:vbv-bufsize={vbv_buf}:"
                   f"pools=1:frame-threads=1")
        cargs = ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-x265-params", cparams]
    # concat demuxer: lets us express per-frame duplication without touching the PNGs
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=os.path.dirname(out_mp4)) as lf:
        dur = 1.0 / r["fps"]
        for _, p in frames:
            for _ in range(dup):
                lf.write(f"file '{os.path.abspath(p)}'\nduration {dur:.6f}\n")
        lf.write(f"file '{os.path.abspath(frames[-1][1])}'\n")   # concat quirk: repeat last
        listfile = lf.name
    try:
        # NO colorspace tags: swscale converts RGB->YUV with bt601 and cv2.VideoCapture
        # decodes bt601 by default -- tagging bt709 makes the decoder use a DIFFERENT
        # matrix than the encoder actually applied, hue-shifting pure red out of the
        # narrow inRange (measured: 3054 red px -> 0 at 12 Mbps with tags, -> 2963
        # without). A self-consistent round trip isolates pure COMPRESSION damage.
        # (Separately: the LIVE chiaki path decodes a bt709 stream with bt601 constants
        # in chiaki_backend._to_bgr -- a real hue shift the NV12/M6 work must fix.)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-vf", f"scale={r['w']}:{r['h']}:flags=bicubic,format=yuv420p",
               "-r", str(r["fps"]), *cargs, out_mp4]
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(listfile)
    return dup


def run_reader(frames_iter, ms_per_frame: float, armed: bool = True):
    """Run the production reader over an iterable of BGR frames; returns per-frame dicts."""
    reader = SimpleMeterReader()
    reader.set_shot_state(armed, 1.0)
    out = []
    for i, frame in frames_iter:
        s = reader.read(frame, ts=i * ms_per_frame / 1000.0)
        out.append({
            "i": i, "det": bool(s["detected"]) and s["stage"] != "coast",
            "stage": s["stage"], "fill": float(s.get("fill", 0.0) or 0.0),
            "bbox": list(s.get("bbox") or (0, 0, 0, 0)),
            "green": s.get("green") is not None,
            "rise": str(s.get("rise_state", "") or ""),
        })
    return out


def mask_survival(pristine_png, rung_frame, pristine_row, red_lo, red_hi, g_lo, g_hi):
    """RED/GREEN mask pixel survival inside the pristine trackbox, pristine vs rung frame."""
    bx, by, bw, bh = pristine_row["bbox"]
    if bw <= 0 or bh <= 0:
        return None
    pris = cv2.imread(pristine_png)
    if pris is None:
        return None
    ph, pw = pris.shape[:2]
    rh, rw = rung_frame.shape[:2]
    sx, sy = rw / pw, rh / ph
    x0, y0 = max(0, bx), max(0, by)
    x1, y1 = min(pw, bx + bw), min(ph, by + bh)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    sub_p = pris[y0:y1, x0:x1]
    rx0, ry0 = int(x0 * sx), int(y0 * sy)
    rx1, ry1 = max(rx0 + 1, int(x1 * sx)), max(ry0 + 1, int(y1 * sy))
    sub_r = rung_frame[ry0:min(rh, ry1), rx0:min(rw, rx1)]
    if sub_r.size == 0:
        return None
    area_ratio = (sub_r.shape[0] * sub_r.shape[1]) / float(sub_p.shape[0] * sub_p.shape[1])
    red_p = cv2.countNonZero(cv2.inRange(sub_p, red_lo, red_hi))
    red_r = cv2.countNonZero(cv2.inRange(sub_r, red_lo, red_hi))
    hsv_p = cv2.cvtColor(sub_p, cv2.COLOR_BGR2HSV)
    hsv_r = cv2.cvtColor(sub_r, cv2.COLOR_BGR2HSV)
    grn_p = cv2.countNonZero(cv2.inRange(hsv_p, g_lo, g_hi))
    grn_r = cv2.countNonZero(cv2.inRange(hsv_r, g_lo, g_hi))
    return {
        "red_surv": (red_r / max(1.0, red_p * area_ratio)) if red_p >= 30 else None,
        "grn_surv": (grn_r / max(1.0, grn_p * area_ratio)) if grn_p >= 20 else None,
    }


def _pct(a, b):
    return 100.0 * a / max(1, b)


def _stats(vals):
    if not vals:
        return dict(n=0)
    v = np.asarray(vals, dtype=np.float64)
    return dict(n=int(v.size), mean=round(float(v.mean()), 2),
                median=round(float(np.median(v)), 2), p90=round(float(np.percentile(v, 90)), 2))


def evaluate_rung(frames, pristine, rung, mode, work_dir, ms_per_frame, keep_video):
    out_mp4 = os.path.join(work_dir, f"{rung}_{mode}.mp4")
    dup = encode_rung(frames, rung, mode, out_mp4)
    cap = cv2.VideoCapture(out_mp4)
    decoded = []
    k = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if k % dup == 0 and len(decoded) < len(frames):
                decoded.append((frames[len(decoded)][0], fr))
            k += 1
    finally:
        cap.release()
    n = min(len(decoded), len(frames))
    rung_rows = run_reader(iter(decoded[:n]), ms_per_frame)
    pris_by_i = {r["i"]: r for r in pristine}
    reader = SimpleMeterReader()   # mask constants (post-ReaderParams they live on the instance)
    red_lo = np.array(reader._RED_LO, np.uint8); red_hi = np.array(reader._RED_HI, np.uint8)
    g_lo = np.array(reader._G[0], np.uint8);     g_hi = np.array(reader._G[1], np.uint8)

    both = ponly = ronly = 0
    fill_d, red_s, grn_s = [], [], []
    phase = {ph: [0, 0] for ph in ("rising", "peak", "spent", "")}   # [pristine_det, both_det]
    surv_every = max(1, n // 120)                                    # cap imread cost
    for idx, rr in enumerate(rung_rows):
        pr = pris_by_i.get(rr["i"])
        if pr is None:
            continue
        if pr["det"]:
            ph = phase.setdefault(pr["rise"], [0, 0])
            ph[0] += 1
            if rr["det"]:
                ph[1] += 1
                both += 1
                fill_d.append(abs(rr["fill"] - pr["fill"]))
            else:
                ponly += 1
            if idx % surv_every == 0:
                s = mask_survival(dict(frames)[rr["i"]], decoded[idx][1], pr, red_lo, red_hi, g_lo, g_hi)
                if s:
                    if s["red_surv"] is not None:
                        red_s.append(s["red_surv"])
                    if s["grn_surv"] is not None:
                        grn_s.append(s["grn_surv"])
        elif rr["det"]:
            ronly += 1
    pris_det = sum(1 for r in pristine if r["det"])
    rep = {
        "rung": rung, "mode": mode, "frames": n, "dup": dup,
        "pristine_det": pris_det, "both": both, "pristine_only": ponly, "rung_only": ronly,
        "recall_vs_pristine_pct": round(_pct(both, pris_det), 1),
        "fill_absdelta": _stats(fill_d),
        "red_mask_survival": _stats([round(min(v, 3.0), 3) for v in red_s]),
        "green_mask_survival": _stats([round(min(v, 3.0), 3) for v in grn_s]),
        "green_window_frames": {"pristine": sum(1 for r in pristine if r["green"]),
                                "rung": sum(1 for r in rung_rows if r["green"])},
        "per_phase_recall_pct": {ph or "none": round(_pct(b, p), 1)
                                 for ph, (p, b) in phase.items() if p},
    }
    if not keep_video:
        try:
            os.unlink(out_mp4)
        except OSError:
            pass
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", default="session_20260704_210801")
    ap.add_argument("--rungs", default=",".join(RUNGS))
    ap.add_argument("--modes", default="seq,dup")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--ms-per-idx", type=float, default=64.6,
                    help="dump cadence (detframes fit; 64.6 for session_20260704_210801)")
    ap.add_argument("--keep-video", action="store_true")
    args = ap.parse_args()

    session_dir = os.path.join(_REPO, "logs", "diagnostics", "framedump", args.session)
    frames = list_frames(session_dir, args.max_frames)
    if not frames:
        print(f"no frames in {session_dir}")
        return 2
    out_dir = os.path.join(_REPO, "logs", "diagnostics", "reencode_ladder", args.session)
    os.makedirs(out_dir, exist_ok=True)

    print(f"[pristine] running reader over {len(frames)} frames ...")
    pristine = run_reader(((i, cv2.imread(p)) for i, p in frames), args.ms_per_idx)

    reports = []
    for rung in [r.strip() for r in args.rungs.split(",") if r.strip()]:
        if rung not in RUNGS:
            print(f"unknown rung {rung!r}; known: {list(RUNGS)}")
            return 2
        for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
            print(f"[{rung}/{mode}] encoding + evaluating ...")
            rep = evaluate_rung(frames, pristine, rung, mode, out_dir, args.ms_per_idx, args.keep_video)
            reports.append(rep)
            print(f"  recall={rep['recall_vs_pristine_pct']}%  fill|d| {rep['fill_absdelta']}  "
                  f"red_surv {rep['red_mask_survival']}  grn_surv {rep['green_mask_survival']}")

    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"session": args.session, "ms_per_idx": args.ms_per_idx,
                   "frames": len(frames), "reports": reports}, f, indent=2)
    lines = [f"reencode_ladder probe -- {args.session} ({len(frames)} frames, "
             f"{args.ms_per_idx} ms/idx dump cadence)",
             "CAVEAT: seq mode overstates temporal artifacts (~4x motion); dup mode understates them.",
             ""]
    for rep in reports:
        lines += [f"=== {rep['rung']} / {rep['mode']} (dup x{rep['dup']}) ===",
                  f"  detection vs pristine: both={rep['both']} pristine-only={rep['pristine_only']} "
                  f"rung-only={rep['rung_only']}  recall={rep['recall_vs_pristine_pct']}%",
                  f"  per-phase recall: {rep['per_phase_recall_pct']}",
                  f"  fill |delta| pp: {rep['fill_absdelta']}",
                  f"  RED mask survival (ratio, 1.0=pristine): {rep['red_mask_survival']}",
                  f"  GREEN tip survival (ratio): {rep['green_mask_survival']}",
                  f"  green-window frames: {rep['green_window_frames']}", ""]
    report_txt = "\n".join(lines)
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report_txt)
    print("\n" + report_txt)
    print(f"wrote {out_dir}\\report.json / report.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
