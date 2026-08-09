#!/usr/bin/env python3
"""Codec-augmented SYNTHETIC LUMA dataset for the compressed-stream meter reader (MeterReaderNet-Y).

WHY: the capture-card (RGB) meter path is solved. The COMPRESSED path (Chiaki H.264 4:2:0, Remote
Play) needs a 1-channel LUMA student CNN, because chroma is subsampled and mangled by the low-latency
encoder. The prior feasibility probe (tools/diagnostics/reencode_ladder.py, logs/diagnostics/
reencode_ladder/*/report.txt) established two hard facts this tool is built around:

  1. Re-encoding MUST be SEQUENCE-based. The damage that matters is TEMPORAL (P-frame residual
     quantization, intra-refresh smear, motion-vector drift). Single-frame JPEG / blur proxies were
     shown NOT to transfer -- so every sample here is a real 24-frame rising sequence pushed through
     ffmpeg and decoded back, and the training crop is harvested from a decoded P-FRAME.
  2. The ffmpeg round trip must NOT tag bt709. swscale converts RGB->YUV with bt601 and
     cv2.VideoCapture decodes bt601 by default; tagging bt709 makes decode use a different matrix
     than encode applied and destroys pure red. Colorspace is left UNTAGGED so the round trip is
     self-consistent and we isolate pure COMPRESSION damage. (We only keep LUMA anyway, but a
     self-consistent round trip still matters for the fill/track contrast.)

APPEARANCE MODEL: reused from tools/diagnostics/gen_meter_reader_synth_v2.py -- dark near-black
track interior, 2-3px light-grey beveled frame, red fill body rising from a chevron notch, and a thin
(2-10% of track) neon-green tip sliver at the top. The colour constants and the composite() alpha
blend are IMPORTED from that module (single source of truth); the per-frame drawing is a faithful
replica of render_meter_v2's steps 1-5 (cited inline) -- reimplemented ONLY because render_meter_v2
randomizes geometry every call and exposes no fill parameter, whereas a rising SEQUENCE needs the
geometry/colour FIXED across 24 frames while the red fill ramps.

ENCODER: the x264/x265 zerolatency-CBR templates are reused from reencode_ladder.py encode_rung
(nal-hrd=cbr, force-cfr, scenecut=0, keyint=infinite, intra-refresh, ~2-frame VBV, no colorspace
tags). Bitrate is SCALED to the 256x256 canvas so the per-pixel bit density matches the chosen rung:
kbps * (256*256)/(rung_w*rung_h). We encode AT the canvas size (no rescale) -- artifact DENSITY is
what must match the rung, not the frame dimensions.

OUTPUT npz (default datasets/meter_reader_synth_y/meter_reader_y_data.npz):
  images   uint8   [N,128,48,1]  single-channel Y crop (H=128, W=48)
  labels   float32 [N,4]         = [fill_frac(0..1), green_top_frac, green_bottom_frac, present(0/1)]
  m_fill   float32 [N]           1 for positives, 0 for negatives (fill loss mask)
  m_green  float32 [N]           1 for positives, 0 for negatives (green-window loss mask)
  rung     int8    [N]           ladder-rung index (see RUNG_NAMES) used for the crop's sequence
  split    uint8   [N]           0=train, 1=val (~10% val)
  fill_raw float32 [N]           raw fill fraction (can exceed 1.0 for the 1.06 overshoot renders)

USAGE:
  python tools/diagnostics/gen_meter_reader_synth_y.py --n-sequences 500
  python tools/diagnostics/gen_meter_reader_synth_y.py --smoke     # 8 sequences (test/dev)
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import random
import shutil
import subprocess
import sys
import tempfile

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
# NOTE: tools/diagnostics is put on sys.path so the v2 module's sibling imports
# (synth_meter_gen / synth_meter_reader_data) resolve. This tool imports NO repo-root module,
# so it does NOT trip the retired tools/diagnostics/simple_meter_reader.py shadow.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_FRAMEDUMP_DIR = os.path.join(
    _REPO, "logs", "diagnostics", "framedump", "session_20260704_210801")
DEFAULT_OUT = os.path.join(_REPO, "datasets", "meter_reader_synth_y", "meter_reader_y_data.npz")

CANVAS = 256          # square render/encode canvas (background patch size)
N_FRAMES = 24         # frames per rising sequence
SKIP_INTRA = 8        # skip the leading intra-refresh region before harvesting P-frames
STORE_H, STORE_W = 128, 48   # crop store size (tall vertical bar)
WIDTH_PAD = 0.30      # 0.30 each side => x1.6 total track-box width
HEIGHT_PAD = 0.12     # +12% of track height top and bottom


# ---------------------------------------------------------------------------------------------
# Appearance model: import colour constants + composite() from gen_meter_reader_synth_v2 (single
# source of truth); fall back to a faithful replica if the import fails. Constants are cited so the
# replica stays in sync with tools/diagnostics/gen_meter_reader_synth_v2.py.
# ---------------------------------------------------------------------------------------------
def _load_v2():
    name = "gen_meter_reader_synth_v2"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_HERE, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    _V2 = _load_v2()
    RED_BGR = _V2.RED_BGR
    GREEN_BGR = _V2.GREEN_BGR
    DARK_BGR = _V2.DARK_BGR
    FRAME_BGR = _V2.FRAME_BGR
    LEG_BGR = _V2.LEG_BGR
    composite = _V2.composite
    APPEARANCE_SRC = "imported from gen_meter_reader_synth_v2"
except Exception as _e:  # pragma: no cover - defensive fallback
    # Faithful replica of gen_meter_reader_synth_v2.py appearance constants (cited).
    RED_BGR = (28, 18, 222)       # bright pure red fill body
    GREEN_BGR = (40, 245, 60)     # neon make-window tip sliver (~#31FF1F)
    DARK_BGR = (26, 24, 24)       # real empty track: dark / near-black
    FRAME_BGR = (188, 188, 190)   # light-grey beveled frame
    LEG_BGR = (150, 150, 150)     # grey chevron legs under the bar

    def composite(bg, layer, alpha):  # replica of synth_meter_gen.composite
        a = alpha[..., None]
        return (bg.astype(np.float32) * (1 - a) + layer.astype(np.float32) * a).astype(np.uint8)

    APPEARANCE_SRC = f"replicated (v2 import failed: {_e})"


# ---------------------------------------------------------------------------------------------
# Encoder templates: reused from tools/diagnostics/reencode_ladder.py RUNGS / encode_rung.
# The real RemotePlay bandwidth ladder, mirrored from RemotePlaySession.cpp streamPresetForMode().
# ---------------------------------------------------------------------------------------------
RUNGS = {
    "Quality":      dict(w=1920, h=1080, fps=60, codec="libx264", kbps=12000),
    "Performance":  dict(w=1280, h=720,  fps=60, codec="libx264", kbps=12000),
    "Balanced":     dict(w=1280, h=720,  fps=60, codec="libx264", kbps=4000),
    "LowBandwidth": dict(w=960,  h=540,  fps=60, codec="libx264", kbps=2500),
    "UltraLow":     dict(w=640,  h=360,  fps=30, codec="libx264", kbps=1200),
}
RUNG_NAMES = list(RUNGS)
# Weighted toward the Balanced 720p rung (the density the reader must be most robust to).
RUNG_WEIGHTS = {"Quality": 1, "Performance": 2, "Balanced": 5, "LowBandwidth": 2, "UltraLow": 1}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def scaled_bitrate_kbps(rung, canvas_w=CANVAS, canvas_h=CANVAS):
    """Scale the rung bitrate to the canvas so per-pixel bit density matches (spec point 3)."""
    r = RUNGS[rung]
    return max(1, int(round(r["kbps"] * (canvas_w * canvas_h) / (r["w"] * r["h"]))))


def encode_sequence(frames_bgr, rung, work_dir, fps=None):
    """Encode a list of BGR frames through ONE ladder rung at the canvas size (no rescale).

    Replicates reencode_ladder.encode_rung's x264/x265 zerolatency-CBR params and the concat-demuxer
    per-frame-duration feed, with two deliberate changes for this tool: (a) bitrate is scaled to the
    canvas, (b) there is NO scale filter (encode at canvas size). NO colorspace tags -- see module
    docstring. Returns (out_mp4_path, kbps_used).
    """
    r = RUNGS[rung]
    fps = fps or r["fps"]
    h, w = frames_bgr[0].shape[:2]
    kbps = scaled_bitrate_kbps(rung, w, h)
    vbv_buf = max(2 * kbps // fps, 40)   # ~2-frame VBV, floor for sanity (reencode_ladder)

    png_paths = []
    for i, fr in enumerate(frames_bgr):
        p = os.path.join(work_dir, f"src_{i:03d}.png")
        cv2.imwrite(p, fr)
        png_paths.append(p)

    # concat demuxer: express the per-frame duration explicitly (mirrors reencode_ladder, incl. the
    # "repeat last frame" concat quirk). Forward slashes so ffmpeg is happy on Windows.
    listfile = os.path.join(work_dir, "concat.txt")
    dur = 1.0 / fps
    with open(listfile, "w", encoding="utf-8") as lf:
        for p in png_paths:
            ap = os.path.abspath(p).replace("\\", "/")
            lf.write(f"file '{ap}'\nduration {dur:.6f}\n")
        lf.write(f"file '{os.path.abspath(png_paths[-1]).replace(chr(92), '/')}'\n")

    if r["codec"] == "libx264":
        cparams = (f"nal-hrd=cbr:force-cfr=1:scenecut=0:keyint=infinite:intra-refresh=1:"
                   f"bitrate={kbps}:vbv-maxrate={kbps}:vbv-bufsize={vbv_buf}:"
                   f"ref=1:bframes=0:rc-lookahead=0:slices=4:aq-mode=1")
        cargs = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-x264-params", cparams]
    else:
        cparams = (f"keyint=-1:intra-refresh=1:bframes=0:rc-lookahead=0:scenecut=0:"
                   f"bitrate={kbps}:vbv-maxrate={kbps}:vbv-bufsize={vbv_buf}")
        cargs = ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-x265-params", cparams]

    out_mp4 = os.path.join(work_dir, f"seq_{rung}.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", listfile,
           "-vf", "format=yuv420p", "-r", str(fps), *cargs, out_mp4]
    subprocess.run(cmd, check=True)

    for p in png_paths:
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        os.unlink(listfile)
    except OSError:
        pass
    return out_mp4, kbps


def decode_frames(mp4_path):
    """Decode an mp4 back to a list of BGR frames via cv2.VideoCapture."""
    cap = cv2.VideoCapture(mp4_path)
    out = []
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            out.append(fr)
    finally:
        cap.release()
    return out


def weighted_rung(rng):
    names = list(RUNG_WEIGHTS)
    return rng.choices(names, weights=[RUNG_WEIGHTS[n] for n in names], k=1)[0]


# ---------------------------------------------------------------------------------------------
# Rendering: fixed-geometry rising SEQUENCE. Steps 1-5 mirror render_meter_v2 in
# gen_meter_reader_synth_v2.py; geometry + colours are fixed once per sequence, only the red fill
# height ramps, so the encoder sees a real rising meter over a real (panned) background.
# ---------------------------------------------------------------------------------------------
def _jit(rng, c, k):
    return tuple(int(_clamp(v + rng.randint(-k, k), 0, 255)) for v in c)


def _draw_meter(bg, geom, style, fill_pct):
    """Composite the meter (at fixed geometry/colour) with a given fill onto bg. Replica of
    render_meter_v2 steps 1-5 with fixed style."""
    ch, cw = bg.shape[:2]
    layer = np.zeros((ch, cw, 3), np.uint8)
    alpha = np.zeros((ch, cw), np.float32)
    tx, ty, tw, th = geom["track_box"]
    bottom = ty + th
    green_top, green_bottom = geom["green_top"], geom["green_bottom"]
    fw = style["fw"]

    fill_h = int(round(_clamp(fill_pct, 0.0, 1.0) * th))
    fill_top = bottom - fill_h

    # 1) DARK empty track interior + light-grey beveled frame
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), style["dark"], -1)
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), style["frame"], fw)
    alpha[max(0, ty - fw):bottom + fw, max(0, tx - fw):tx + tw + fw] = style["alpha_val"]
    # 2) thin neon green make-window at the very top
    cv2.rectangle(layer, (tx + fw, green_top), (tx + tw - fw, green_bottom), style["green"], -1)
    # 3) red fill from the notch up to fill_top (never over the green cap)
    rf_top = max(fill_top, green_bottom)
    if rf_top < bottom:
        cv2.rectangle(layer, (tx + fw, rf_top), (tx + tw - fw, bottom), style["red"], -1)
    # 4) chevron notch at the bottom + grey legs beneath
    cxm = tx + tw // 2
    vh = max(3, tw // 3)
    notch = np.array([[tx + fw, bottom], [cxm, bottom - vh], [tx + tw - fw, bottom]], np.int32)
    cv2.fillPoly(layer, [notch], style["dark"])
    cv2.polylines(layer, [notch], False, style["frame"], 1)
    leg_h = style["leg_h"]
    cv2.line(layer, (tx + fw, bottom), (cxm, bottom + leg_h), LEG_BGR, 1)
    cv2.line(layer, (tx + tw - fw, bottom), (cxm, bottom + leg_h), LEG_BGR, 1)
    yb = _clamp(bottom + leg_h + 1, 0, ch)
    alpha[bottom:yb, tx:tx + tw] = np.maximum(alpha[bottom:yb, tx:tx + tw], 0.5)
    # 5) soften a touch (real meter is anti-aliased over the bg)
    k = style["blur_k"]
    if k > 1:
        layer = cv2.GaussianBlur(layer, (k, k), 0)
        alpha = cv2.GaussianBlur(alpha, (k, k), 0)
    return composite(bg, layer, alpha)


def render_meter_sequence(bg_source, rng, n_frames=N_FRAMES, canvas=CANVAS):
    """Render an n_frames rising sequence of a fixed meter over a panned real background.

    Returns (frames, fills, geom):
      frames : list[np.uint8 (canvas,canvas,3)]  composited BGR frames
      fills  : list[float]                        raw fill fraction per frame (may exceed 1.0)
      geom   : dict(track_box=(tx,ty,tw,th), green_top, green_bottom)
    """
    H, W = bg_source.shape[:2]
    if H < canvas or W < canvas:
        bg_source = cv2.resize(bg_source, (max(canvas, W), max(canvas, H)))
        H, W = bg_source.shape[:2]

    # --- fixed geometry (leave margin for the x1.6 / +12% crop pad + jitter to stay in canvas) ---
    tw = rng.randint(14, 28)
    th = rng.randint(90, 150)
    padx = int(WIDTH_PAD * tw) + 10
    pady = int(HEIGHT_PAD * th) + 10
    tx = rng.randint(padx, max(padx + 1, canvas - tw - padx))
    ty = rng.randint(pady, max(pady + 1, canvas - th - pady))
    green_frac = rng.uniform(0.02, 0.10)              # thin neon sliver (real windows are thin)
    green_h = max(2, int(round(th * green_frac)))
    geom = dict(track_box=(tx, ty, tw, th), green_top=ty, green_bottom=ty + green_h)

    # --- fixed colour/style (jittered ONCE so the meter does not flicker frame to frame) ---
    style = dict(
        dark=_jit(rng, DARK_BGR, 10), frame=_jit(rng, FRAME_BGR, 22),
        green=_jit(rng, GREEN_BGR, 22), red=_jit(rng, RED_BGR, 16),
        fw=rng.choice([2, 2, 3]), leg_h=rng.randint(4, 12),
        alpha_val=rng.uniform(0.85, 0.98), blur_k=rng.choice([1, 3]),
    )

    # --- rising fill ramp: start 0..0.5 -> end up to 1.06, roughly linear ---
    start = rng.uniform(0.0, 0.5)
    end = rng.uniform(max(start + 0.15, 0.5), 1.06)

    # --- global pan: shift the background crop window 1-3 px/frame (real motion for the encoder) ---
    # Pick a start that lets the FULL pan stay in bounds when the slack allows (always true for the
    # 1920x1080 framedump); fall back to any start + per-frame clamp for small backgrounds.
    n1 = n_frames - 1
    maxx, maxy = W - canvas, H - canvas
    dx = rng.choice([-3, -2, -1, 1, 2, 3])
    dy = rng.choice([-3, -2, -1, 0, 1, 2, 3])

    def _pan_start(mx, d):
        lo, hi = max(0, -d * n1), min(mx, mx - d * n1)
        return rng.randint(lo, hi) if lo <= hi else rng.randint(0, mx)

    x0 = _pan_start(maxx, dx)
    y0 = _pan_start(maxy, dy)

    frames, fills = [], []
    for f in range(n_frames):
        cx0 = min(max(x0 + dx * f, 0), maxx)   # clamp: safety net for small backgrounds
        cy0 = min(max(y0 + dy * f, 0), maxy)
        bg = bg_source[cy0:cy0 + canvas, cx0:cx0 + canvas].copy()
        fill_pct = start + (end - start) * (f / n1)
        frames.append(_draw_meter(bg, geom, style, fill_pct))
        fills.append(fill_pct)
    return frames, fills, geom


# ---------------------------------------------------------------------------------------------
# Crop + label geometry
# ---------------------------------------------------------------------------------------------
def pad_track_box(track_box):
    """Track box -> crop box (float x0,y0,x1,y1): x1.6 total width, +12% height top/bottom."""
    tx, ty, tw, th = track_box
    px = WIDTH_PAD * tw
    py = HEIGHT_PAD * th
    return (tx - px, ty - py, tx + tw + px, ty + th + py)


def green_fracs_for_crop(green_top, green_bottom, crop_y0, crop_h):
    """Pure label math: green-window canvas rows -> fractions of the crop height."""
    return ((green_top - crop_y0) / crop_h, (green_bottom - crop_y0) / crop_h)


def _resize_gray(crop_bgr):
    r = cv2.resize(crop_bgr, (STORE_W, STORE_H), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY)
    return g[..., None]


def crop_meter(frame, track_box, green_top, green_bottom, rng):
    """Cut the meter crop with +-3px / +-5% scale jitter, resize to 128x48, convert to single-channel
    Y. Returns (gray[128,48,1] uint8, green_top_frac, green_bottom_frac)."""
    H, W = frame.shape[:2]
    x0f, y0f, x1f, y1f = pad_track_box(track_box)
    s = rng.uniform(0.95, 1.05)                        # +-5% scale jitter
    cx = (x0f + x1f) / 2 + rng.uniform(-3, 3)          # +-3px translation jitter
    cy = (y0f + y1f) / 2 + rng.uniform(-3, 3)
    bw = (x1f - x0f) * s
    bh = (y1f - y0f) * s
    ix0 = int(round(cx - bw / 2)); iy0 = int(round(cy - bh / 2))
    ix1 = int(round(cx + bw / 2)); iy1 = int(round(cy + bh / 2))
    ix0 = max(0, min(ix0, W - 2)); iy0 = max(0, min(iy0, H - 2))
    ix1 = max(ix0 + 2, min(ix1, W)); iy1 = max(iy0 + 2, min(iy1, H))
    crop = frame[iy0:iy1, ix0:ix1]
    gtf, gbf = green_fracs_for_crop(green_top, green_bottom, iy0, iy1 - iy0)
    return _resize_gray(crop), float(gtf), float(gbf)


def _sample_negative_box(canvas, avoid_box, rng, ref_w, ref_h):
    """A meter-FREE crop box (x,y,w,h) that does not overlap the meter's padded region."""
    ax0, ay0, ax1, ay1 = avoid_box
    for _ in range(40):
        w = int(_clamp(ref_w * rng.uniform(0.85, 1.15), 8, canvas - 2))
        h = int(_clamp(ref_h * rng.uniform(0.85, 1.15), 8, canvas - 2))
        x0 = rng.randint(0, canvas - w)
        y0 = rng.randint(0, canvas - h)
        if x0 + w < ax0 or x0 > ax1 or y0 + h < ay0 or y0 > ay1:
            return (x0, y0, w, h)
    # fallback: top-left corner box (worst case, small overlap risk)
    w = int(_clamp(ref_w, 8, canvas // 3))
    h = int(_clamp(ref_h, 8, canvas // 2))
    return (0, 0, w, h)


# ---------------------------------------------------------------------------------------------
# Dataset generation
# ---------------------------------------------------------------------------------------------
def generate(n_sequences, out_path, seed=0, framedump_dir=DEFAULT_FRAMEDUMP_DIR,
             neg_frac=0.15, val_frac=0.10, keep_mp4=False, verbose=True):
    rng = random.Random(seed)
    bg_files = sorted(glob.glob(os.path.join(framedump_dir, "f*_raw.png")))
    if not bg_files:
        raise SystemExit(f"no framedump backgrounds matched {framedump_dir}/f*_raw.png")

    tmp_root = tempfile.mkdtemp(prefix="synth_y_")
    imgs, labels, m_fill, m_green, rung_idx, fill_raw = [], [], [], [], [], []
    n_encoded = n_skipped = 0
    try:
        for si in range(n_sequences):
            bg_source = cv2.imread(rng.choice(bg_files))
            if bg_source is None:
                n_skipped += 1
                continue
            rung = weighted_rung(rng)
            frames, fills, geom = render_meter_sequence(bg_source, rng)
            work = os.path.join(tmp_root, f"seq{si:05d}")
            os.makedirs(work, exist_ok=True)
            try:
                mp4, _ = encode_sequence(frames, rung, work)
                decoded = decode_frames(mp4)
            except (subprocess.CalledProcessError, cv2.error):
                shutil.rmtree(work, ignore_errors=True)
                n_skipped += 1
                continue
            if len(decoded) < 2:
                shutil.rmtree(work, ignore_errors=True)
                n_skipped += 1
                continue
            n_encoded += 1

            avail = [i for i in range(SKIP_INTRA, len(decoded))] or list(range(len(decoded)))
            k = min(rng.randint(2, 4), len(avail))
            picks = sorted(rng.sample(avail, k))

            tx, ty, tw, th = geom["track_box"]
            gt, gb = geom["green_top"], geom["green_bottom"]
            padded = pad_track_box(geom["track_box"])
            ridx = RUNG_NAMES.index(rung)
            for fi in picks:
                frame = decoded[fi]
                fill_pct = fills[min(fi, len(fills) - 1)]
                if rng.random() < neg_frac:
                    nb = _sample_negative_box(CANVAS, padded, rng, 1.6 * tw, 1.24 * th)
                    x, y, w, h = nb
                    gray = _resize_gray(frame[y:y + h, x:x + w])
                    imgs.append(gray)
                    labels.append([0.0, 0.0, 0.0, 0.0])
                    m_fill.append(0.0); m_green.append(0.0)
                    fill_raw.append(0.0)
                else:
                    gray, gtf, gbf = crop_meter(frame, geom["track_box"], gt, gb, rng)
                    imgs.append(gray)
                    labels.append([float(_clamp(fill_pct, 0.0, 1.0)),
                                   float(_clamp(gtf, 0.0, 1.0)),
                                   float(_clamp(gbf, 0.0, 1.0)), 1.0])
                    m_fill.append(1.0); m_green.append(1.0)
                    fill_raw.append(float(fill_pct))
                rung_idx.append(ridx)
            if not keep_mp4:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        if not keep_mp4:
            shutil.rmtree(tmp_root, ignore_errors=True)

    N = len(imgs)
    if N == 0:
        raise SystemExit("no crops harvested (all sequences failed to encode/decode)")
    images = np.asarray(imgs, np.uint8).reshape(N, STORE_H, STORE_W, 1)
    labels_a = np.asarray(labels, np.float32).reshape(N, 4)
    m_fill_a = np.asarray(m_fill, np.float32)
    m_green_a = np.asarray(m_green, np.float32)
    rung_a = np.asarray(rung_idx, np.int8)
    fill_raw_a = np.asarray(fill_raw, np.float32)

    split = np.zeros((N,), np.uint8)
    n_val = int(round(N * val_frac))
    idx = list(range(N))
    rng.shuffle(idx)
    for j in idx[:n_val]:
        split[j] = 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    np.savez_compressed(out_path, images=images, labels=labels_a, m_fill=m_fill_a,
                        m_green=m_green_a, rung=rung_a, split=split, fill_raw=fill_raw_a)

    if verbose:
        pos = int((labels_a[:, 3] > 0.5).sum())
        print(f"wrote {out_path}")
        print(f"  appearance: {APPEARANCE_SRC}")
        print(f"  sequences: requested={n_sequences} encoded={n_encoded} skipped={n_skipped}")
        print(f"  crops N={N} positives={pos} negatives={N - pos} "
              f"({100.0 * (N - pos) / N:.1f}% neg)  val={n_val} ({100.0 * n_val / N:.1f}%)")
        fp = labels_a[labels_a[:, 3] > 0.5][:, 0] if pos else np.array([0.0])
        print(f"  fill(pos) min={fp.min():.3f} max={fp.max():.3f} mean={fp.mean():.3f}  "
              f"fill_raw max={fill_raw_a.max():.3f}")
        rung_hist = {RUNG_NAMES[i]: int((rung_a == i).sum()) for i in range(len(RUNG_NAMES))}
        print(f"  rung histogram: {rung_hist}")
    return out_path, N


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-sequences", type=int, default=500)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--framedump-dir", default=DEFAULT_FRAMEDUMP_DIR)
    ap.add_argument("--neg-frac", type=float, default=0.15)
    ap.add_argument("--val-frac", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--smoke", action="store_true", help="8-sequence smoke run (test/dev)")
    ap.add_argument("--keep-mp4", action="store_true", help="keep the temp mp4s (debug)")
    args = ap.parse_args()

    n = 8 if args.smoke else args.n_sequences
    generate(n, args.out, seed=args.seed, framedump_dir=args.framedump_dir,
             neg_frac=args.neg_frac, val_frac=args.val_frac, keep_mp4=args.keep_mp4)


if __name__ == "__main__":
    main()
