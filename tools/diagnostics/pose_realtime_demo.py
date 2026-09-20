"""pose_realtime_demo.py -- run the trained YOLO26n-pose ONNX model as a REAL-TIME loop.

Owner's question (2026-09-17): *"I don't think we've ever got the ONNX model actually
working and running in real time -- it's just been trained and sitting on disk. Can you
run it against frame dumps to see how it performs?"*

This is a **loop with a wall clock**, not a synthetic benchmark. It decodes a real source
at its native rate, runs the pose model on the GPU frame by frame, locks onto the ball
handler, extracts the animation SET POINT (wrist-y velocity zero-crossing) and writes an
annotated MP4 the owner can watch.

Read-only. Nothing here imports, starts or touches the engine, the sidecar, the reader,
the detector, settings.json or learning.json. The only repo module imported is
``player_anchor`` (the nameplate identity), and only its ``PlayerAnchor`` class, on frames
this process decoded itself.

Sources (auto-discovered, in this order):
  * ``E:\\PS5\\CREATE\\Video Clips\\NBA 2K26\\*.mp4``   (the 2K26 60 fps corpus, if mounted)
  * ``D:\\VeniceTraining\\delay_clips\\clip*.mp4``      (59.94 fps 1080p MyCourt fallback)
  * ``D:\\NexusVision\\framedump\\session_*``           (PNG dumps; timing from frames.csv)
  * ``ep<epoch>_f<idx>_*.jpg`` press-window JPEGs inside any session_* dir

Usage:
    .venv/Scripts/python.exe tools/diagnostics/pose_realtime_demo.py --all
    .venv/Scripts/python.exe tools/diagnostics/pose_realtime_demo.py --list
    .venv/Scripts/python.exe tools/diagnostics/pose_realtime_demo.py \
        --source clipA --annotate --imgsz 448

Outputs (all under D:\\NexusVision\\pose_rt\\): rt_<src>.json, rt_<src>_frames.csv,
rt_<src>_shots.csv, annot_<src>.mp4, sheet_<src>.png, pose_rt_summary.json.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import statistics as st
import subprocess
import sys
import threading
import time

import numpy as np

# --------------------------------------------------------------------------- #
# torch MUST be imported before onnxruntime or onnxruntime-gpu silently falls back
# to the CPU EP (docs/ANIMATION_ANCHOR_V2.md 2.4).  torch.__init__ is what puts the
# CUDA/cuDNN DLL directory on the Windows search path.
# --------------------------------------------------------------------------- #
import torch  # noqa: F401  (load-bearing side effect)
import onnxruntime as ort
import cv2

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OUT_ROOT = r"D:\NexusVision\pose_rt"
MODEL_DIR = r"D:\NexusVision\anchor_study"
FRAMEDUMP_ROOT = r"D:\NexusVision\framedump"
ANALYSIS_DIR = r"D:\NexusVision\framedump\_analysis"
CLIP_DIRS = [r"E:\PS5\CREATE\Video Clips\NBA 2K26", r"D:\VeniceTraining\delay_clips"]

KP_NAMES = ["nose", "eye_l", "eye_r", "ear_l", "ear_r", "sh_l", "sh_r", "el_l", "el_r",
            "wr_l", "wr_r", "hip_l", "hip_r", "kn_l", "kn_r", "an_l", "an_r"]
SKEL = [(0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
        (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
        (0, 5), (0, 6)]
WR_L, WR_R = 9, 10

FRAME_60 = 1000.0 / 60.0


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #
def pct(xs, p):
    xs = sorted(x for x in xs if x == x)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarise(ms):
    ms = [m for m in ms if m == m]
    if not ms:
        return {}
    return {"n": len(ms), "p50": round(pct(ms, 50), 2), "p90": round(pct(ms, 90), 2),
            "p99": round(pct(ms, 99), 2), "max": round(max(ms), 2),
            "mean": round(st.fmean(ms), 2)}


def sd(xs):
    xs = [x for x in xs if x == x]
    return float(st.pstdev(xs)) if len(xs) > 1 else float("nan")


def mad_sd(xs):
    """Robust sd (1.4826 * median absolute deviation) -- the archive's usual estimator."""
    xs = [x for x in xs if x == x]
    if len(xs) < 2:
        return float("nan")
    m = st.median(xs)
    return 1.4826 * st.median([abs(x - m) for x in xs])


# --------------------------------------------------------------------------- #
#  1. the model  --  ONNX, CUDA, asserted
# --------------------------------------------------------------------------- #
class PoseRT:
    """The live path: letterbox -> ORT CUDA -> decode.  No ultralytics, no torch forward."""

    def __init__(self, imgsz=448, conf=0.08, max_persons=6):
        path = os.path.join(MODEL_DIR, "pose26n_%d_fp32.onnx" % imgsz)
        if not os.path.exists(path):
            raise SystemExit("missing export: %s (run anchor_pose_bench.py)" % path)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(
            path, so, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        provs = self.sess.get_providers()
        if provs[0] != "CUDAExecutionProvider":
            raise SystemExit(
                "pose model is NOT on the GPU (providers=%s). torch must be imported "
                "before onnxruntime; see ANIMATION_ANCHOR_V2 2.4." % provs)
        self.providers = provs
        self.path = path
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.max_persons = int(max_persons)
        self.iname = self.sess.get_inputs()[0].name
        self.buf = np.empty((1, 3, imgsz, imgsz), np.float32)
        # warm the graph so the first real frame is not a 400 ms outlier
        for _ in range(8):
            self.sess.run(None, {self.iname: self.buf})

    # -- preprocess ------------------------------------------------------- #
    def pre(self, bgr):
        sz = self.imgsz
        h, w = bgr.shape[:2]
        r = min(sz / float(h), sz / float(w))
        nw, nh = int(round(w * r)), int(round(h * r))
        dx, dy = (sz - nw) // 2, (sz - nh) // 2
        canvas = np.full((sz, sz, 3), 114, np.uint8)
        canvas[dy:dy + nh, dx:dx + nw] = cv2.resize(bgr, (nw, nh),
                                                    interpolation=cv2.INTER_LINEAR)
        np.divide(canvas[:, :, ::-1].transpose(2, 0, 1)[None], np.float32(255.0),
                  out=self.buf)
        return self.buf, r, dx, dy

    # -- infer ------------------------------------------------------------ #
    def infer(self, x):
        return self.sess.run(None, {self.iname: x})[0]

    # -- decode ----------------------------------------------------------- #
    def post(self, out, r, dx, dy):
        """YOLO26 end-to-end head, (1,300,57) = [x1,y1,x2,y2,score,cls,17*(x,y,c)].
        NMS-free: the queries are already de-duplicated, so this is a threshold + sort."""
        a = out[0] if out.ndim == 3 else out
        keep = a[:, 4] >= self.conf
        if not keep.any():
            return []
        a = a[keep]
        a = a[np.argsort(-a[:, 4])[:self.max_persons]]
        people = []
        for row in a:
            bx = (row[0:4].copy()).astype(np.float32)
            bx[0] = (bx[0] - dx) / r
            bx[2] = (bx[2] - dx) / r
            bx[1] = (bx[1] - dy) / r
            bx[3] = (bx[3] - dy) / r
            k = row[6:57].reshape(17, 3).copy()
            k[:, 0] = (k[:, 0] - dx) / r
            k[:, 1] = (k[:, 1] - dy) / r
            people.append({"box": bx, "score": float(row[4]), "kpt": k})
        return people


# --------------------------------------------------------------------------- #
#  2. sources
# --------------------------------------------------------------------------- #
class VideoSource:
    kind = "video"

    def __init__(self, name, path):
        self.name = name
        self.path = path
        cap = cv2.VideoCapture(path)
        self.fps = float(cap.get(cv2.CAP_PROP_FPS)) or 60.0
        self.n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        self.cap = None

    def restrict(self, start=0, count=0):
        self.start = int(start)
        if count:
            self.n = min(self.n - self.start, int(count))
        else:
            self.n = self.n - self.start
        return self

    def open(self):
        self.cap = cv2.VideoCapture(self.path)
        if getattr(self, "start", 0):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.start)
        return self

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __len__(self):
        return self.n

    def frames(self):
        """yield (i, t_src_ms, bgr, extra) -- t_src_ms is the source's own clock."""
        i = 0
        while True:
            ok, img = self.cap.read()
            if not ok:
                break
            yield i, i * 1000.0 / self.fps, img, {}
            i += 1


class DumpSource:
    """A framedump directory: PNG frames plus frames.csv (t_wall + the meter box)."""
    kind = "dump"

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.rows = []
        csvp = os.path.join(path, "frames.csv")
        if os.path.exists(csvp):
            with open(csvp, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        self.rows.append({
                            "idx": int(r["idx"]), "t_wall": float(r["t_wall"]),
                            "detected": int(r.get("detected", 0) or 0),
                            "bx": int(r.get("bbox_x", -1) or -1),
                            "by": int(r.get("bbox_y", -1) or -1),
                            "bw": int(r.get("bbox_w", 0) or 0),
                            "bh": int(r.get("bbox_h", 0) or 0),
                            "fill": float(r.get("fill_pct", -1) or -1)})
                    except Exception:
                        continue
        self.rows.sort(key=lambda r: r["idx"])
        self.files = {}
        for p in glob.glob(os.path.join(path, "f*_raw.png")):
            m = re.match(r"f(\d+)_\d+_raw\.png$", os.path.basename(p))
            if m:
                self.files[int(m.group(1))] = p
        if not self.rows:
            self.rows = [{"idx": i, "t_wall": i * 0.1, "detected": 0, "bx": -1,
                          "by": -1, "bw": 0, "bh": 0, "fill": -1}
                         for i in sorted(self.files)]
        self.rows = [r for r in self.rows if r["idx"] in self.files]
        t0 = self.rows[0]["t_wall"] if self.rows else 0.0
        for r in self.rows:
            r["t_ms"] = (r["t_wall"] - t0) * 1000.0
        self.t0_wall = t0
        gaps = [self.rows[i + 1]["t_ms"] - self.rows[i]["t_ms"]
                for i in range(len(self.rows) - 1)]
        self.gap_p50 = pct(gaps, 50) if gaps else 0.0
        self.gap_p90 = pct(gaps, 90) if gaps else 0.0
        self.fps = 1000.0 / self.gap_p50 if self.gap_p50 > 0 else 0.0
        img = cv2.imread(self.rows[0]["idx"] and self.files[self.rows[0]["idx"]]
                         or self.files[self.rows[0]["idx"]]) if self.rows else None
        self.h, self.w = (img.shape[:2] if img is not None else (720, 1280))
        self.n = len(self.rows)
        self.all_rows = list(self.rows)

    def restrict(self, start=0, count=0):
        self.rows = self.all_rows[int(start): (int(start) + int(count)) if count else None]
        self.n = len(self.rows)
        return self

    def restrict_to_windows(self, times_wall, pre_ms=900.0, post_ms=900.0):
        """Keep only the frames inside a press/release window -- the duty the sidecar
        would actually run (ANIMATION_ANCHOR_V2 2.3: press windows only)."""
        keep = []
        for r in self.all_rows:
            tw = r["t_wall"]
            for tt in times_wall:
                if tt - pre_ms / 1000.0 <= tw <= tt + post_ms / 1000.0:
                    keep.append(r)
                    break
        self.rows = keep
        self.n = len(keep)
        return self

    def open(self):
        return self

    def close(self):
        pass

    def __len__(self):
        return self.n

    def frames(self):
        for i, r in enumerate(self.rows):
            img = cv2.imread(self.files[r["idx"]])
            if img is None:
                continue
            yield i, r["t_ms"], img, r


class JpgSource:
    """Press-window JPEGs from the new collector: ep<epoch>_f<idx>_*.jpg."""
    kind = "jpgdump"

    def __init__(self, name, path, files):
        self.name = name
        self.path = path
        rec = []
        for p in files:
            m = re.match(r"ep(\d+)_f(\d+)_", os.path.basename(p))
            if m:
                rec.append((int(m.group(1)), int(m.group(2)), p))
        rec.sort()
        self.rec = rec
        self.n = len(rec)
        self.fps = 60.0
        img = cv2.imread(rec[0][2]) if rec else None
        self.h, self.w = (img.shape[:2] if img is not None else (720, 1280))

    def restrict(self, start=0, count=0):
        self.rec = self.rec[int(start): (int(start) + int(count)) if count else None]
        self.n = len(self.rec)
        return self

    def open(self):
        return self

    def close(self):
        pass

    def __len__(self):
        return self.n

    def frames(self):
        for i, (ep, fi, p) in enumerate(self.rec):
            img = cv2.imread(p)
            if img is None:
                continue
            yield i, fi * FRAME_60, img, {"epoch": ep, "fidx": fi}


def discover():
    srcs = []
    clip_dir = None
    for d in CLIP_DIRS:
        if os.path.isdir(d):
            clip_dir = d
            break
    if clip_dir:
        vids = sorted(glob.glob(os.path.join(clip_dir, "*.mp4")))
        # prefer 60 fps, landscape, >= 8 s -- the shape a jump-shot clip has
        good = []
        for p in vids:
            c = cv2.VideoCapture(p)
            fps = c.get(cv2.CAP_PROP_FPS)
            n = c.get(cv2.CAP_PROP_FRAME_COUNT)
            w = c.get(cv2.CAP_PROP_FRAME_WIDTH)
            h = c.get(cv2.CAP_PROP_FRAME_HEIGHT)
            c.release()
            if fps >= 50 and w > h and n / max(fps, 1) >= 8:
                good.append((p, fps, n))
        for p, fps, n in good:
            srcs.append(VideoSource(os.path.splitext(os.path.basename(p))[0], p))
    for d in sorted(glob.glob(os.path.join(FRAMEDUMP_ROOT, "session_*"))):
        jpgs = glob.glob(os.path.join(d, "ep*_f*_*.jpg"))
        if jpgs:
            srcs.append(JpgSource("jpg_" + os.path.basename(d)[8:], d, jpgs))
        pngs = glob.glob(os.path.join(d, "f*_raw.png"))
        if len(pngs) >= 50:
            srcs.append(DumpSource(os.path.basename(d)[8:], d))
    return srcs


# --------------------------------------------------------------------------- #
#  3. tracking + the player lock
# --------------------------------------------------------------------------- #
class Tracker:
    """Greedy nearest-centre association with a motion gate.  Gives every person a
    persistent id so a LOCK FLIP is observable (the id under the lock changed), which
    is the quantity ANIMATION_ANCHOR_V2 6.1 says a record without is unfilterable."""

    def __init__(self, gate_px=120.0, max_gap=30):
        self.gate = gate_px
        self.max_gap = int(max_gap)      # a person the model lost for a few frames is
        self.tracks = {}                 # still the same person -- coasting the track
        self.next_id = 1                 # keeps a DETECTION gap from reading as a FLIP

    def step(self, people, i, scale=1.0):
        cent = [((p["box"][0] + p["box"][2]) * 0.5, (p["box"][1] + p["box"][3]) * 0.5,
                 p["box"][3] - p["box"][1]) for p in people]
        used, out = set(), [0] * len(people)
        order = sorted(range(len(people)), key=lambda j: -people[j]["score"])
        for j in order:
            cx, cy, h = cent[j]
            best, bd, bgap = None, 1e18, 0
            for tid, (tx, ty, th, li) in self.tracks.items():
                if tid in used or i - li > self.max_gap:
                    continue
                d = math.hypot(cx - tx, cy - ty)
                if d < bd:
                    best, bd, bgap = tid, d, i - li
            gate = self.gate * scale * (1.0 + 0.12 * max(0, bgap - 1))
            if best is not None and bd <= gate:
                out[j] = best
                used.add(best)
            else:
                out[j] = self.next_id
                self.next_id += 1
        for j, tid in enumerate(out):
            cx, cy, h = cent[j]
            self.tracks[tid] = (cx, cy, h, i)
        return out


def lock_by_plate(people, tids, anchor, scale):
    """Nameplate anchor: the PS-disc rides at the owner's FEET, so the shooter is the
    person whose box BOTTOM-CENTRE sits nearest the plate."""
    if anchor is None:
        return None, None, 0.0
    best, bd = None, 1e18
    for j, p in enumerate(people):
        bx = p["box"]
        cx = (bx[0] + bx[2]) * 0.5
        bot = bx[3]
        d = math.hypot(cx - anchor.icon_x, bot - (anchor.icon_y - 4.0 * scale))
        if d < bd:
            best, bd = j, d
    if best is None or bd > 190.0 * scale:
        return None, None, float(bd)
    return best, tids[best], float(bd)


def lock_by_box(people, tids, mbox, scale):
    """Fallback: nearest person centre to the meter box centre (ANIMATION_ANCHOR_V2 3.2
    measures the offset as +58 px x / +53 px y at 720p; this is the rule the doc measured
    flipping on 20 % of 5v5 frames)."""
    if mbox is None:
        return None, None, 0.0
    cx, cy = mbox[0] + mbox[2] * 0.5 + 58.0 * scale, mbox[1] + mbox[3] * 0.5 + 53.0 * scale
    best, bd = None, 1e18
    for j, p in enumerate(people):
        bx = p["box"]
        d = math.hypot((bx[0] + bx[2]) * 0.5 - cx, (bx[1] + bx[3]) * 0.5 - cy)
        if d < bd:
            best, bd = j, d
    if best is None or bd > 320.0 * scale:
        return None, None, float(bd)
    return best, tids[best], float(bd)


# --------------------------------------------------------------------------- #
#  4. box / cpu / gpu telemetry
# --------------------------------------------------------------------------- #
class Telemetry(threading.Thread):
    def __init__(self, period=0.4):
        super().__init__(daemon=True)
        self.period = period
        self.stop_evt = threading.Event()
        self.gpu, self.gmem, self.cpu, self.vram = [], [], [], []
        self._nvml = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = (pynvml, pynvml.nvmlDeviceGetHandleByIndex(0))
        except Exception:
            self._nvml = None
        try:
            import psutil
            self._ps = psutil.Process()
            self._ps.cpu_percent(None)
            self._psutil = psutil
            psutil.cpu_percent(None)
        except Exception:
            self._ps = self._psutil = None

    def _gpu_sample(self):
        if self._nvml is not None:
            pynvml, h = self._nvml
            try:
                u = pynvml.nvmlDeviceGetUtilizationRates(h)
                m = pynvml.nvmlDeviceGetMemoryInfo(h)
                return float(u.gpu), float(u.memory), m.used / 1048576.0
            except Exception:
                return None
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.memory,memory.used",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
            a = [float(x) for x in r.stdout.strip().split(",")]
            return a[0], a[1], a[2]
        except Exception:
            return None

    def run(self):
        while not self.stop_evt.wait(self.period):
            g = self._gpu_sample()
            if g:
                self.gpu.append(g[0])
                self.gmem.append(g[1])
                self.vram.append(g[2])
            if self._psutil is not None:
                self.cpu.append(self._psutil.cpu_percent(None))

    def result(self):
        self.stop_evt.set()
        out = {}
        if self.gpu:
            out["gpu_util_pct"] = {"p50": round(pct(self.gpu, 50), 1),
                                   "p90": round(pct(self.gpu, 90), 1),
                                   "max": round(max(self.gpu), 1), "n": len(self.gpu)}
            out["gpu_mem_ctrl_pct"] = {"p50": round(pct(self.gmem, 50), 1)}
            out["vram_used_mb"] = {"p50": round(pct(self.vram, 50), 0),
                                   "max": round(max(self.vram), 0)}
        if self.cpu:
            out["cpu_util_pct_box"] = {"p50": round(pct(self.cpu, 50), 1),
                                       "p90": round(pct(self.cpu, 90), 1),
                                       "max": round(max(self.cpu), 1)}
        return out


# --------------------------------------------------------------------------- #
#  5. events -- the SET POINT
# --------------------------------------------------------------------------- #
def smooth(y, k=5):
    y = np.asarray(y, np.float64)
    if len(y) < k or k < 3:
        return y.copy()
    w = np.ones(k) / k
    pad = k // 2
    yp = np.concatenate([np.full(pad, y[0]), y, np.full(pad, y[-1])])
    return np.convolve(yp, w, "valid")[:len(y)]


def find_shots(t_ms, wr_y, min_depth=0.16, min_gap_ms=700.0, look_ms=1100.0,
               rise_lo_ms=90.0, rise_hi_ms=800.0, onset_frac=0.12, smooth_k=5):
    """A shot = the wrist sweeping UP to an apex.

    ``wr_y`` is normalised inside the person box (0 = box top, 1 = box bottom), so a
    rising hand is a FALLING wr_y.  The apex is a local minimum with a real rise behind
    it; the SET POINT is the last zero-crossing of the wrist-y velocity before the peak
    upward velocity of that rise -- i.e. the instant the hand stops dropping/holding and
    starts the shooting motion.  It is EARLIER than the half-rise the V2 doc measured
    landing 89 ms after the drawn meter onset, which is the whole point.
    """
    t = np.asarray(t_ms, np.float64)
    y = np.asarray(wr_y, np.float64)
    ok = np.isfinite(y)
    if ok.sum() < 8:
        return []
    ys = y.copy()
    # fill short gaps so the velocity is defined, but remember where the data was real
    idx = np.arange(len(y))
    ys[~ok] = np.interp(idx[~ok], idx[ok], y[ok])
    ys = smooth(ys, smooth_k)
    v = np.gradient(ys, t)          # units / ms ; negative = hand rising

    # the apex must be the lowest point (highest hand) in +-350 ms, with a real rise
    # behind it -- that is what separates a shot from a dribble bob.
    half = 350.0
    apexes = []
    for i in range(2, len(ys) - 2):
        w0 = int(np.searchsorted(t, t[i] - half))
        w1 = int(np.searchsorted(t, t[i] + half))
        if ys[i] > ys[max(0, w0):max(w1, i + 1)].min() + 1e-9:
            continue
        back = (t >= (t[i] - look_ms)) & (t <= t[i])
        seg = ys[back]
        if len(seg) < 4:
            continue
        depth = float(seg.max() - ys[i])
        if depth < min_depth:
            continue
        apexes.append((i, depth))
    # de-duplicate: keep the deepest apex inside each min_gap window
    apexes.sort(key=lambda a: t[a[0]])
    kept = []
    for i, d in apexes:
        if kept and t[i] - t[kept[-1][0]] < min_gap_ms:
            if d > kept[-1][1]:
                kept[-1] = (i, d)
            continue
        kept.append((i, d))

    shots = []
    for i, depth in kept:
        lo = max(0, int(np.searchsorted(t, t[i] - look_ms)))
        if i - lo < 3:
            continue
        p = int(np.argmin(v[lo:i + 1])) + lo          # fastest RISE
        if p <= lo or v[p] >= 0:
            continue

        def back_cross(level):
            """last time before the peak at which the rise rate was slower than `level`
            (levels are negative; 0.0 is the literal velocity zero-crossing)."""
            for q in range(p, lo, -1):
                if v[q - 1] >= level > v[q]:
                    a, b = v[q - 1] - level, v[q] - level
                    f = a / (a - b) if (a - b) != 0 else 0.5
                    return q, float(t[q - 1] + f * (t[q] - t[q - 1]))
            return None, float("nan")

        # The literal zero-crossing is what the brief asks for, but on a real trace the
        # hand wobbles at rest, so the LAST zero-crossing can land on a noise wiggle
        # hundreds of ms early.  The stable form of the same event is the crossing of a
        # small FRACTION of the peak rise rate; both are reported.
        z_i, z_t = back_cross(0.0)
        o_i, o_t = back_cross(onset_frac * v[p])
        sp_i, sp_t = (o_i, o_t) if o_i is not None else (z_i, z_t)
        if sp_i is None:
            continue
        rise = float(t[i] - sp_t)
        if not (rise_lo_ms <= rise <= rise_hi_ms):
            continue
        # The apex is a PLATEAU: the hand hangs overhead through the follow-through, so
        # argmin over it is noise.  The sharp end-of-rise event is the FIRST crossing of
        # 90 % of the rise, scanned forward from the peak velocity.
        lvl = ys[i] + 0.10 * depth
        top_i, top_t = i, float(t[i])
        for q in range(p, i + 1):
            if ys[q] <= lvl:
                a, b = ys[q - 1] - lvl, ys[q] - lvl
                f = a / (a - b) if (a - b) != 0 and q > 0 else 0.0
                top_i = q
                top_t = float(t[q - 1] + f * (t[q] - t[q - 1])) if q > 0 else float(t[q])
                break
        gap_in_rise = 0
        run = 0
        for q in range(sp_i, i + 1):
            run = 0 if ok[q] else run + 1
            gap_in_rise = max(gap_in_rise, run)
        shots.append({
            "apex_i": int(i), "apex_t": float(t[i]), "apex_y": float(ys[i]),
            "depth": float(depth), "peak_vel_i": int(p), "peak_vel": float(v[p]),
            "set_i": int(sp_i), "set_t": sp_t, "set_y": float(ys[sp_i]),
            "zero_i": int(z_i) if z_i is not None else -1,
            "zero_t": float(z_t), "zero_rise_ms": float(t[i] - z_t) if z_t == z_t else
            float("nan"),
            "pv_t": float(t[p]), "set_to_pv_ms": float(t[p] - sp_t),
            "top_i": int(top_i), "top_t": top_t, "set_to_top_ms": float(top_t - sp_t),
            "rise_ms": rise,
            "n_real": int(ok[sp_i:i + 1].sum()), "n_win": int(i + 1 - sp_i),
            "max_gap_frames_in_rise": int(gap_in_rise),
        })
    return shots


# --------------------------------------------------------------------------- #
#  6. drawing
# --------------------------------------------------------------------------- #
def draw_pose(img, p, colour=(0, 235, 255), thin=False):
    k = p["kpt"]
    bx = p["box"]
    cv2.rectangle(img, (int(bx[0]), int(bx[1])), (int(bx[2]), int(bx[3])),
                  colour, 1 if thin else 2)
    for a, b in SKEL:
        if k[a, 2] < 0.35 or k[b, 2] < 0.35:
            continue
        cv2.line(img, (int(k[a, 0]), int(k[a, 1])), (int(k[b, 0]), int(k[b, 1])),
                 colour, 1 if thin else 2, cv2.LINE_AA)
    for i in range(17):
        if k[i, 2] < 0.35:
            continue
        c = (60, 60, 255) if i in (WR_L, WR_R) else (255, 255, 255)
        cv2.circle(img, (int(k[i, 0]), int(k[i, 1])), 4 if i in (WR_L, WR_R) else 2,
                   c, -1, cv2.LINE_AA)


def draw_trace(img, t_ms, wr_y, now_i, events, x0, y0, w, h, span_ms=2500.0):
    """The wrist-y inset: a scrolling plot with the set point and apex marked."""
    cv2.rectangle(img, (x0, y0), (x0 + w, y0 + h), (18, 18, 18), -1)
    cv2.rectangle(img, (x0, y0), (x0 + w, y0 + h), (90, 90, 90), 1)
    t_now = t_ms[now_i]
    lo = t_now - span_ms

    def px(t):
        return int(x0 + 4 + (w - 8) * max(0.0, min(1.0, (t - lo) / span_ms)))

    def py(v):
        return int(y0 + 4 + (h - 8) * max(0.0, min(1.0, (v - 0.0) / 0.75)))

    for frac, lab in ((0.05, "up"), (0.45, "rest")):
        yy = py(frac)
        cv2.line(img, (x0 + 4, yy), (x0 + w - 4, yy), (55, 55, 55), 1)
        cv2.putText(img, lab, (x0 + 6, yy - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.32,
                    (110, 110, 110), 1, cv2.LINE_AA)
    prev = None
    for i in range(now_i + 1):
        if t_ms[i] < lo:
            prev = None
            continue
        v = wr_y[i]
        if not np.isfinite(v):
            prev = None
            continue
        pt = (px(t_ms[i]), py(v))
        if prev is not None:
            cv2.line(img, prev, pt, (0, 235, 255), 1, cv2.LINE_AA)
        prev = pt
    for ev in events:
        for key, col, lab in (("set_t", (60, 255, 60), "SET"),
                              ("apex_t", (255, 180, 60), "APEX"),
                              ("rel_t", (60, 60, 255), "REL")):
            tv = ev.get(key)
            if tv is None or not np.isfinite(tv) or tv < lo or tv > t_now:
                continue
            xx = px(tv)
            cv2.line(img, (xx, y0 + 3), (xx, y0 + h - 3), col, 1, cv2.LINE_AA)
            cv2.putText(img, lab, (xx + 2, y0 + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.34,
                        col, 1, cv2.LINE_AA)
    cv2.putText(img, "wrist-y (in person box)", (x0 + 5, y0 + h - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (170, 170, 170), 1, cv2.LINE_AA)


def hud(img, lines, x=10, y=24, col=(255, 255, 255)):
    for i, s in enumerate(lines):
        cv2.putText(img, s, (x + 1, y + 20 * i + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, s, (x, y + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.52, col, 1,
                    cv2.LINE_AA)


# --------------------------------------------------------------------------- #
#  7. the real-time loop
# --------------------------------------------------------------------------- #
def drop_sim(pipe_ms, budget_ms=FRAME_60):
    """If this loop were pinned to a 60 fps camera with latest-frame-wins, how many
    frames would it never see?  Frames arrive every 16.67 ms; a frame that arrives while
    the loop is busy is dropped."""
    busy_until = 0.0
    dropped = 0
    for i, dt in enumerate(pipe_ms):
        arrive = i * budget_ms
        if arrive < busy_until - 1e-9:
            dropped += 1
            continue
        busy_until = max(arrive, busy_until) + dt
    return dropped


def run_source(src, pose, mode="native", lock_mode="auto", limit=0, quiet=False):
    """One real-time pass.  Returns the per-frame record and the timing summary."""
    import player_anchor as pa
    anch = pa.PlayerAnchor()

    tele = Telemetry()
    tele.start()
    trk = Tracker(gate_px=120.0)
    scale = src.h / 720.0

    rec = []
    t_dec, t_pre, t_inf, t_post, t_lock, t_pipe = [], [], [], [], [], []
    src.open()
    it = src.frames()
    t_wall0 = time.perf_counter()
    t_src0 = None
    skipped_gap_s = 0.0
    lag_ms = []
    n = 0
    try:
        while True:
            td0 = time.perf_counter()
            try:
                item = next(it)
            except StopIteration:
                break
            i, t_src, img, extra = item
            if t_src0 is None:
                t_src0 = t_src
            dec = (time.perf_counter() - td0) * 1000.0

            tp0 = time.perf_counter()
            x, r, dx, dy = pose.pre(img)
            pre = (time.perf_counter() - tp0) * 1000.0

            ti0 = time.perf_counter()
            out = pose.infer(x)
            inf = (time.perf_counter() - ti0) * 1000.0

            tq0 = time.perf_counter()
            people = pose.post(out, r, dx, dy)
            post = (time.perf_counter() - tq0) * 1000.0

            tl0 = time.perf_counter()
            tids = trk.step(people, i, scale)
            anchor = None
            if lock_mode in ("auto", "plate"):
                anchor = anch.update(img, ts=t_src / 1000.0, armed=True)
            # -- every rule is evaluated on EVERY frame so the two can be compared --
            jp, tp, dp = lock_by_plate(people, tids, anchor, scale)
            mb = None
            if extra and extra.get("detected") and extra.get("bh", 0) >= 60:
                mb = (extra["bx"], extra["by"], extra["bw"], extra["bh"])
            jb, tb, db = lock_by_box(people, tids, mb, scale)
            jt = int(np.argmax([p["score"] for p in people])) if people else None
            tt = tids[jt] if jt is not None else None
            if lock_mode == "plate":
                j, tid, dist, lock_src = jp, tp, dp, "plate"
            elif lock_mode == "box":
                j, tid, dist, lock_src = jb, tb, db, "box"
            elif lock_mode == "top":
                j, tid, dist, lock_src = jt, tt, float("nan"), "top"
            else:                                   # auto: plate -> box -> top
                if jp is not None:
                    j, tid, dist, lock_src = jp, tp, dp, "plate"
                elif jb is not None:
                    j, tid, dist, lock_src = jb, tb, db, "box"
                else:
                    j, tid, dist, lock_src = jt, tt, float("nan"), "top"
            if j is None:
                lock_src = "none"
            lok = (time.perf_counter() - tl0) * 1000.0

            pipe = dec + pre + inf + post + lok
            t_dec.append(dec)
            t_pre.append(pre)
            t_inf.append(inf)
            t_post.append(post)
            t_lock.append(lok)
            t_pipe.append(pipe)

            row = {"i": i, "t_src": t_src, "n_people": len(people),
                   "lock_src": lock_src, "track_id": tid if tid is not None else -1,
                   "tid_plate": tp if tp is not None else -1,
                   "tid_box": tb if tb is not None else -1,
                   "tid_top": tt if tt is not None else -1,
                   **{("c%s_%s" % (ax, nm)):
                      (float((people[jj]["box"][0 + ax] + people[jj]["box"][2 + ax]) * 0.5)
                       if jj is not None else float("nan"))
                      for nm, jj in (("plate", jp), ("box", jb), ("top", jt))
                      for ax in (0, 1)},
                   "lock_dist": dist, "t_dec": dec, "t_pre": pre, "t_inf": inf,
                   "t_post": post, "t_lock": lok, "t_pipe": pipe,
                   "anchor_conf": float(anchor.conf) if anchor is not None else float("nan")}
            if j is not None:
                p = people[j]
                bx = p["box"]
                h = max(1.0, bx[3] - bx[1])
                k = p["kpt"]
                wr = WR_R if k[WR_R, 2] >= k[WR_L, 2] else WR_L
                sh_y = float(k[5:7, 1].mean())
                hip_y = float(k[11:13, 1].mean())
                torso = max(12.0, hip_y - sh_y)
                row.update({
                    "score": p["score"], "box_x": float(bx[0]), "box_y": float(bx[1]),
                    "box_w": float(bx[2] - bx[0]), "box_h": float(h),
                    # doc-comparable: 0 = box top, 1 = box bottom (ANIMATION_ANCHOR_V2 3.2)
                    "wr_y": float((k[wr, 1] - bx[1]) / h),
                    # body-relative hand height: 0 = wrist at the shoulders, 1 = one torso
                    # ABOVE them.  Immune to the person box growing when the arm goes up.
                    "hand_h": float((sh_y - k[wr, 1]) / torso),
                    "torso_px": float(torso),
                    "wr_x": float((k[wr, 0] - bx[0]) / max(1.0, bx[2] - bx[0])),
                    "wr_c": float(k[wr, 2]),
                    "kpt_conf_mean": float(k[:, 2].mean()),
                    "kpt_ge50": int((k[:, 2] >= 0.5).sum()),
                    "sh_y": float((sh_y - bx[1]) / h),
                    "kp": k.copy(), "bx": bx.copy(),
                })
            else:
                row.update({"score": float("nan"), "wr_y": float("nan"),
                            "hand_h": float("nan"), "torso_px": float("nan"),
                            "wr_c": float("nan"), "kpt_conf_mean": float("nan"),
                            "kpt_ge50": 0, "box_h": float("nan"), "kp": None,
                            "bx": None, "box_x": float("nan"), "box_y": float("nan"),
                            "box_w": float("nan"), "wr_x": float("nan"),
                            "sh_y": float("nan")})
            if extra:
                row["dump"] = {kk: extra.get(kk) for kk in
                               ("idx", "t_wall", "detected", "bx", "by", "bw", "bh", "fill")
                               if kk in extra}
            rec.append(row)
            n += 1

            # ---- the wall clock: pace to the source's native rate ----------
            if mode == "native":
                target = t_wall0 + (t_src - t_src0) / 1000.0
                now = time.perf_counter()
                slack = target - now
                if slack > 0.30:
                    # a gap between press windows (the sidecar would be idle here) --
                    # jump the clock instead of sleeping through dead time
                    t_wall0 -= slack
                    skipped_gap_s += slack
                    slack = 0.0
                lag_ms.append(-slack * 1000.0)
                if slack > 0.0015:
                    time.sleep(slack - 0.0005)
            if limit and n >= limit:
                break
    finally:
        src.close()

    wall = time.perf_counter() - t_wall0 - skipped_gap_s
    tel = tele.result()
    dropped60 = drop_sim(t_pipe)
    out = {
        "source": src.name, "kind": src.kind, "path": getattr(src, "path", ""),
        "mode": mode, "w": src.w, "h": src.h,
        "native_fps": round(float(src.fps), 3), "frames": n,
        "wall_s": round(wall, 3),
        "achieved_fps": round(n / wall, 2) if wall > 0 else 0.0,
        "capacity_fps": round(1000.0 / pct(t_pipe, 50), 1) if t_pipe else 0.0,
        "stage_ms": {"decode": summarise(t_dec), "pre": summarise(t_pre),
                     "infer": summarise(t_inf), "post": summarise(t_post),
                     "lock": summarise(t_lock), "pipeline": summarise(t_pipe)},
        "pipeline_over_16_67ms": sum(1 for v in t_pipe if v > FRAME_60),
        "dropped_if_pinned_60fps": dropped60,
        "dropped_pct_60fps": round(100.0 * dropped60 / max(1, n), 2),
        "pace_lag_ms": summarise([v for v in lag_ms if v > 0]) if lag_ms else {},
        "telemetry": tel,
        "no_person_frames": sum(1 for r in rec if r["n_people"] == 0),
        "lock_src_counts": {k: sum(1 for r in rec if r["lock_src"] == k)
                            for k in ("plate", "box", "top", "none")},
        "anchor_stats": dict(anch.stats),
    }
    if not quiet:
        print("  [%s/%s] %d frames in %.2fs = %.1f fps (native %.2f) | pipe p50 %.2f "
              "p99 %.2f ms | drop@60 %d (%.1f%%) | lock %s"
              % (src.name, mode, n, wall, out["achieved_fps"], src.fps,
                 out["stage_ms"]["pipeline"].get("p50", 0),
                 out["stage_ms"]["pipeline"].get("p99", 0), dropped60,
                 out["dropped_pct_60fps"], out["lock_src_counts"]), flush=True)
    return rec, out


# --------------------------------------------------------------------------- #
#  8. per-shot analysis
# --------------------------------------------------------------------------- #
def _drill_shots_json():
    p = os.path.join(ANALYSIS_DIR, "shots.json")
    if not os.path.exists(p):
        return []
    try:
        shots = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    verd = {}
    g = os.path.join(ANALYSIS_DIR, "panel_grade.json")
    if os.path.exists(g):
        try:
            with open(g, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("seq"):
                        verd[int(r["seq"])] = r.get("word", "")
        except Exception:
            pass
    return [{"seq": s.get("seq"), "press_t": s.get("press_t"), "rel_t": s.get("rel_t"),
             "shot": s.get("shot", ""), "verdict": verd.get(s.get("seq"), "")}
            for s in shots if s.get("rel_t")]


def _hud_study_shots():
    p = os.path.join(REPO, "logs", "diagnostics", "hud_landmark_study", "shots_table.csv")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rel = float(r.get("release_wall") or "nan")
            except Exception:
                continue
            if not (rel == rel):
                continue
            try:
                pr = float(r.get("press_wall") or "nan")
            except Exception:
                pr = float("nan")
            out.append({"seq": int(r.get("epoch") or 0), "press_t": pr, "rel_t": rel,
                        "shot": r.get("shot_type", ""), "verdict": r.get("banner", "")})
    return out


def session_releases(src):
    """Every known TRUE release that lands inside this dump's own wall-clock span.
    Two archives carry them: _analysis/shots.json (the drill) and the HUD landmark
    study's shots_table.csv (the Theater session).  The span filter is what makes it
    safe to consult both without a session tag."""
    rows = getattr(src, "all_rows", None) or getattr(src, "rows", [])
    if not rows:
        return []
    lo = min(r["t_wall"] for r in rows) - 2.0
    hi = max(r["t_wall"] for r in rows) + 2.0
    out = []
    for s in (_drill_shots_json() + _hud_study_shots()):
        if lo <= s["rel_t"] <= hi:
            out.append(s)
    out.sort(key=lambda s: s["rel_t"])
    return out


def release_times_wall(src):
    return [s["rel_t"] for s in session_releases(src)]


def jitter_probe(t_ms, sig, shots, min_depth):
    """Split the observed shot-to-shot spread into INSTRUMENT and ANIMATION.

    Two perturbations of the same tracks, each re-running the whole extractor:
      * **sampling** -- decimate 60 Hz to 30 / 20 Hz at every phase.  How far does the
        event move?  This is the ANIMATION_ANCHOR_V2 3.4 interpolation term, measured
        instead of extrapolated.
      * **estimator** -- vary the onset fraction (0.08/0.12/0.18) and the smoothing
        width (3/5/7).  How far does the event move?  This is the definition's own noise.

    If both are small next to the spread across shots, the spread is the ANIMATION
    differing from shot to shot -- which is the signal an anchor would key on, not noise.
    """
    t = np.asarray(t_ms, float)
    s = np.asarray(sig, float)
    base = {round(sh["apex_t"]): sh["set_t"] for sh in shots}
    if not base:
        return {}

    def match(sl):
        d = []
        for sh in sl:
            for at, bt in base.items():
                if abs(sh["apex_t"] - at) <= 220.0:
                    d.append(sh["set_t"] - bt)
                    break
        return d

    samp = []
    for step, phases in ((2, 2), (3, 3)):
        for ph in range(phases):
            sub = slice(ph, None, step)
            samp += match(find_shots(t[sub], s[sub], min_depth=min_depth))
    est = []
    for of in (0.08, 0.12, 0.18):
        for k in (3, 5, 7):
            if of == 0.12 and k == 5:
                continue
            est += match(find_shots(t, s, min_depth=min_depth, onset_frac=of,
                                    smooth_k=k))
    return {
        "sampling_30_20hz": {"n": len(samp), "sd": round(sd(samp), 1) if samp else None,
                             "mad_sd": round(mad_sd(samp), 1) if samp else None,
                             "p90_abs": round(pct([abs(x) for x in samp], 90), 1)
                             if samp else None},
        "estimator_knobs": {"n": len(est), "sd": round(sd(est), 1) if est else None,
                            "mad_sd": round(mad_sd(est), 1) if est else None,
                            "p90_abs": round(pct([abs(x) for x in est], 90), 1)
                            if est else None},
    }


def lock_rule_stats(rec, key, name=None, scale=1.0):
    """How often does this rule change the identity it is holding, on a frame where it
    holds one at all?  This is the number ANIMATION_ANCHOR_V2 6.1 calls the top risk.

    Two forms, because they answer different questions:
      * ``flips_per_frame`` -- the tracker id changed.  Sensitive to the SAMPLE RATE (a
        6.6 fps dump gives the tracker no chance to associate across 150 ms of motion).
      * ``jump_gt120px_per_frame`` -- the chosen person's box centre moved > 120 px
        between consecutive held frames.  This is the exact quantity the V2 doc quotes
        (0.20 / frame for nearest-to-box in 5v5), so the two are comparable.
    """
    ids = [r.get(key, -1) for r in rec]
    held = [v for v in ids if v > 0]
    flips = 0
    prev = None
    for v in ids:
        if v <= 0:
            continue
        if prev is not None and v != prev:
            flips += 1
        prev = v
    out = {"frames_held": len(held), "frames_total": len(ids),
           "hold_pct": round(100.0 * len(held) / max(1, len(ids)), 1),
           "flips": flips,
           "flips_per_frame": round(flips / max(1, len(held)), 4),
           "distinct_ids": len(set(held))}
    if name:
        jumps, pairs, prev_c = 0, 0, None
        for r in rec:
            cx = r.get("c0_%s" % name, float("nan"))
            cy = r.get("c1_%s" % name, float("nan"))
            if not (cx == cx and cy == cy):
                prev_c = None
                continue
            if prev_c is not None:
                pairs += 1
                if math.hypot(cx - prev_c[0], cy - prev_c[1]) > 120.0 * scale:
                    jumps += 1
            prev_c = (cx, cy)
        out["jump_gt120px"] = jumps
        out["consecutive_pairs"] = pairs
        out["jump_gt120px_per_frame"] = round(jumps / max(1, pairs), 4)
    return out


def analyse(rec, src, out):
    t = [r["t_src"] for r in rec]
    wy = [r["wr_y"] for r in rec]
    hh = [-(r.get("hand_h", float("nan"))) for r in rec]   # negate: apex = minimum

    cand = {
        "wr_y_boxnorm": find_shots(t, wy, min_depth=0.22),
        "hand_h_torso": find_shots(t, hh, min_depth=1.05),
    }
    def iv(sl, key):
        xs = [s[key] for s in sl if s[key] == s[key]]
        if not xs:
            return None
        return {"n": len(xs), "median": round(st.median(xs), 1),
                "sd": round(sd(xs), 1) if len(xs) > 1 else None,
                "mad_sd": round(mad_sd(xs), 1) if len(xs) > 1 else None}

    out["feature_compare"] = {}
    for name, sl in cand.items():
        out["feature_compare"][name] = {
            "n_shots": len(sl),
            # the three candidate END references, weakest last
            "set_to_peakvel": iv(sl, "set_to_pv_ms"),
            "set_to_top90": iv(sl, "set_to_top_ms"),
            "set_to_apex": iv(sl, "rise_ms"),
            "zerocross_to_apex": iv(sl, "zero_rise_ms")}
    # the body-relative feature is the physical one (the person box grows when the arm
    # goes up, which couples wr_y to the very motion it is measuring); keep it unless it
    # found nothing.
    out["feature_used"] = ("hand_h_torso" if len(cand["hand_h_torso"]) >=
                           max(1, len(cand["wr_y_boxnorm"])) else "wr_y_boxnorm")
    shots = cand[out["feature_used"]]
    out["jitter_probe"] = jitter_probe(
        t, hh if out["feature_used"] == "hand_h_torso" else wy, shots,
        1.05 if out["feature_used"] == "hand_h_torso" else 0.22)

    sc = src.h / 720.0
    out["lock_rules"] = {"plate": lock_rule_stats(rec, "tid_plate", "plate", sc),
                         "box": lock_rule_stats(rec, "tid_box", "box", sc),
                         "top_conf": lock_rule_stats(rec, "tid_top", "top", sc)}
    ag = [(r.get("tid_plate", -1), r.get("tid_box", -1)) for r in rec]
    both = [(a, b) for a, b in ag if a > 0 and b > 0]
    out["lock_rules"]["plate_vs_box_agree_pct"] = (
        round(100.0 * sum(1 for a, b in both if a == b) / len(both), 1) if both else None)

    # attach true releases where they exist (drill dump only)
    rel_map = []
    if src.kind == "dump" and getattr(src, "t0_wall", None):
        for s in session_releases(src):
            if s["rel_t"]:
                rel_map.append((s, (s["rel_t"] - src.t0_wall) * 1000.0,
                                (s["press_t"] - src.t0_wall) * 1000.0 if s["press_t"] else None))

    rows = []
    for si, sh in enumerate(shots):
        lo = sh["set_i"]
        hi = sh["apex_i"]
        w = rec[max(0, lo - 6): min(len(rec), hi + 12)]
        confs = [r["kpt_conf_mean"] for r in w if np.isfinite(r["kpt_conf_mean"])]
        ge50 = [r["kpt_ge50"] for r in w if r["kpt_ge50"]]
        lost = sum(1 for r in w if not np.isfinite(r["wr_y"]))
        ids = [r["track_id"] for r in w if r["track_id"] > 0]
        flips = sum(1 for a, b in zip(ids, ids[1:]) if a != b)
        locks = [r["lock_src"] for r in w]
        rel_t = float("nan")
        seq = verd = shot_type = ""
        press_t = float("nan")
        for s, rt, pt in rel_map:
            if abs(rt - sh["apex_t"]) < 700.0:
                rel_t, seq, verd, shot_type = rt, s["seq"], s["verdict"], s["shot"]
                press_t = pt if pt is not None else float("nan")
                break
        rows.append({
            "source": src.name, "shot": si + 1, "seq": seq, "shot_type": shot_type,
            "verdict": verd,
            "set_t_ms": round(sh["set_t"], 1), "apex_t_ms": round(sh["apex_t"], 1),
            "peakvel_t_ms": round(sh["pv_t"], 1), "top90_t_ms": round(sh["top_t"], 1),
            "set_to_peakvel_ms": round(sh["set_to_pv_ms"], 1),
            "set_to_top90_ms": round(sh["set_to_top_ms"], 1),
            "rise_ms": round(sh["rise_ms"], 1),
            "zero_rise_ms": (round(sh["zero_rise_ms"], 1)
                             if sh["zero_rise_ms"] == sh["zero_rise_ms"] else ""),
            "depth": round(sh["depth"], 3),
            "max_gap_frames_in_rise": sh["max_gap_frames_in_rise"],
            "press_t_ms": round(press_t, 1) if np.isfinite(press_t) else "",
            "rel_t_ms": round(rel_t, 1) if np.isfinite(rel_t) else "",
            "set_minus_rel_ms": round(sh["set_t"] - rel_t, 1) if np.isfinite(rel_t) else "",
            "apex_minus_rel_ms": round(sh["apex_t"] - rel_t, 1) if np.isfinite(rel_t) else "",
            "kpt_conf_mean": round(st.fmean(confs), 3) if confs else "",
            "kpt_ge50_median": int(st.median(ge50)) if ge50 else 0,
            "frames_lost": lost, "frames_in_window": len(w),
            "lock_flips": flips,
            "lock_plate_frames": locks.count("plate"),
            "lock_box_frames": locks.count("box"),
            "lock_top_frames": locks.count("top"),
            "samples_in_rise": sh["n_real"],
        })
    out["shots"] = rows
    out["n_shots"] = len(rows)

    def stats(vals):
        vals = [v for v in vals if isinstance(v, (int, float)) and v == v]
        if not vals:
            return None
        return {"n": len(vals), "median": round(st.median(vals), 1),
                "sd": round(sd(vals), 1) if len(vals) > 1 else None,
                "mad_sd": round(mad_sd(vals), 1) if len(vals) > 1 else None,
                "p10": round(pct(vals, 10), 1), "p90": round(pct(vals, 90), 1)}

    out["set_to_apex_ms"] = stats([r["rise_ms"] for r in rows]) or {}
    out["set_to_peakvel_ms"] = stats([r["set_to_peakvel_ms"] for r in rows])
    out["set_to_top90_ms"] = stats([r["set_to_top90_ms"] for r in rows])
    smr = [r["set_minus_rel_ms"] for r in rows if r["set_minus_rel_ms"] != ""]
    if smr:
        out["set_to_release_ms"] = stats(smr)
        out["apex_to_release_ms"] = stats([r["apex_minus_rel_ms"] for r in rows
                                           if r["apex_minus_rel_ms"] != ""])
    out["lock_flips_total"] = sum(r["lock_flips"] for r in rows)
    out["lock_flips_per_shot"] = (round(out["lock_flips_total"] / len(rows), 2)
                                  if rows else None)
    return shots


# --------------------------------------------------------------------------- #
#  9. annotated output
# --------------------------------------------------------------------------- #
def write_video(src, rec, shots, out_path, max_w=1280, fps=None, events_rel=None):
    n = len(rec)
    if n == 0:
        return None
    t = [r["t_src"] for r in rec]
    wy = [r["wr_y"] for r in rec]
    evs = []
    for si, sh in enumerate(shots):
        e = {"set_t": sh["set_t"], "apex_t": sh["apex_t"], "n": si + 1}
        if events_rel:
            for rt in events_rel:
                if abs(rt - sh["apex_t"]) < 700.0:
                    e["rel_t"] = rt
        evs.append(e)

    scale = min(1.0, max_w / float(src.w))
    ow, oh = int(src.w * scale), int(src.h * scale)
    fps = fps or max(1.0, float(src.fps))
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (ow, oh))
    if not vw.isOpened():
        return None
    # a short, SLOW replay of the best shot -- the one the owner actually watches
    best = max(range(len(shots)), key=lambda k: shots[k]["depth"]) if shots else None
    bw, blo, bhi, bpath = None, -1, -2, None
    if best is not None:
        pre = int(round(0.60 * fps))
        post = int(round(0.70 * fps))
        blo = max(0, shots[best]["set_i"] - pre)
        bhi = min(n - 1, shots[best]["apex_i"] + post)
        bpath = out_path.replace("annot_", "best_")
        bw = cv2.VideoWriter(bpath, cv2.VideoWriter_fourcc(*"mp4v"),
                             max(4.0, fps / 4.0), (ow, oh))
        if not bw.isOpened():
            bw = None
    src.open()
    draw_ms = []
    try:
        for i, t_src, img, extra in src.frames():
            if i >= n:
                break
            t0 = time.perf_counter()
            r = rec[i]
            if r.get("kp") is not None:
                draw_pose(img, {"kpt": r["kp"], "box": r["bx"]})
            a = max(0.0, r.get("anchor_conf", float("nan")))
            lines = ["%s  t=%.2fs  f%d" % (src.name, t_src / 1000.0, i),
                     "pose %d person(s)  lock=%s  conf=%.2f"
                     % (r["n_people"], r["lock_src"],
                        r["score"] if np.isfinite(r["score"]) else 0.0),
                     "pipe %.1f ms  (infer %.1f)" % (r["t_pipe"], r["t_inf"])]
            near = [e for e in evs if e["set_t"] - 250 <= t_src <= e["apex_t"] + 700]
            for e in near:
                if abs(t_src - e["set_t"]) <= 40:
                    lines.append(">>> SET POINT (shot %d)" % e["n"])
                if abs(t_src - e["apex_t"]) <= 40:
                    lines.append(">>> APEX / release (shot %d)" % e["n"])
                if "rel_t" in e and abs(t_src - e["rel_t"]) <= 40:
                    lines.append(">>> ENGINE RELEASE (shot %d)" % e["n"])
            img = cv2.resize(img, (ow, oh), interpolation=cv2.INTER_AREA)
            hud(img, lines)
            draw_trace(img, t, wy, i, evs, ow - 330, oh - 150, 320, 140)
            for e in near:
                if abs(t_src - e["set_t"]) <= 90 and r.get("kp") is not None:
                    k = r["kp"]
                    wr = WR_R if k[WR_R, 2] >= k[WR_L, 2] else WR_L
                    cv2.circle(img, (int(k[wr, 0] * scale), int(k[wr, 1] * scale)),
                               18, (60, 255, 60), 3, cv2.LINE_AA)
            vw.write(img)
            if bw is not None and blo <= i <= bhi:
                bw.write(img)
            draw_ms.append((time.perf_counter() - t0) * 1000.0)
    finally:
        src.close()
        vw.release()
        if bw is not None:
            bw.release()
    return {"path": out_path, "w": ow, "h": oh, "fps": round(fps, 2),
            "best_shot_clip": bpath,
            "best_shot_frames": [int(blo), int(bhi)],
            "draw_encode_ms": summarise(draw_ms)}


def write_sheet(src, rec, shots, out_path, pick=None, span=6, anchor_t=None,
                anchor_label=""):
    """A 12-frame contact sheet around one release.

    ``anchor_t`` (a TRUE release, in source-clock ms) wins when it is known; otherwise
    the sheet spans the deepest detected shot from its set point to its follow-through.
    """
    if anchor_t is not None and rec:
        ts = [r["t_src"] for r in rec]
        c = int(np.argmin([abs(v - anchor_t) for v in ts]))
        lo = max(0, c - 7)
        hi = min(len(rec) - 1, c + 4)
        sh = {"set_i": -99, "apex_i": c, "apex_t": anchor_t, "rise_ms": float("nan"),
              "depth": 0.0}
        pick = -1
    else:
        if not shots:
            return None
        if pick is None:
            pick = max(range(len(shots)), key=lambda i: shots[i]["depth"])
        sh = shots[pick]
        # span the whole shooting motion: a few frames before the SET POINT through the
        # follow-through, so both marked events are on the sheet.
        lo = max(0, sh["set_i"] - 3)
        hi = min(len(rec) - 1, sh["apex_i"] + 6)
    step = max(1, int(round((hi - lo) / 11.0)))
    idxs = [lo + k * step for k in range(12)]
    idxs = [i for i in idxs if 0 <= i < len(rec)]
    want = set(idxs)
    tiles = {}
    src.open()
    try:
        for i, t_src, img, extra in src.frames():
            if i in want:
                r = rec[i]
                if r.get("kp") is not None:
                    draw_pose(img, {"kpt": r["kp"], "box": r["bx"]})
                bx = r.get("bx")
                if bx is not None:
                    cx = (bx[0] + bx[2]) * 0.5
                    cy = (bx[1] + bx[3]) * 0.5
                    hh = max(140.0, (bx[3] - bx[1]) * 1.15)
                    ww = hh * 4.0 / 3.0
                    x0 = int(max(0, min(img.shape[1] - ww, cx - ww / 2)))
                    y0 = int(max(0, min(img.shape[0] - hh, cy - hh / 2)))
                    crop = img[y0:y0 + int(hh), x0:x0 + int(ww)]
                else:
                    crop = img
                tile = cv2.resize(crop, (320, 240), interpolation=cv2.INTER_AREA)
                lab = "f%d  %+dms" % (i, round(t_src - sh["apex_t"]))
                col = (255, 255, 255)
                if abs(i - sh["set_i"]) <= step // 2:
                    lab += "  SET"
                    col = (60, 255, 60)
                if abs(i - sh["apex_i"]) <= step // 2:
                    lab += "  " + (anchor_label or "APEX")
                    col = (255, 180, 60)
                cv2.rectangle(tile, (0, 0), (319, 239), col, 2)
                cv2.putText(tile, lab, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                            (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(tile, lab, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.46, col, 1,
                            cv2.LINE_AA)
                tiles[i] = tile
                want.discard(i)
            if not want:
                break
    finally:
        src.close()
    if not tiles:
        return None
    order = [tiles[i] for i in idxs if i in tiles]
    while len(order) < 12:
        order.append(np.zeros((240, 320, 3), np.uint8))
    grid = np.vstack([np.hstack(order[0:4]), np.hstack(order[4:8]),
                      np.hstack(order[8:12])])
    head = np.zeros((44, grid.shape[1], 3), np.uint8)
    title = ("%s  %s  (%s)" % (src.name, anchor_label, "%.1f fps" % src.fps)
             if anchor_t is not None else
             "%s  shot %d  set->apex %.0f ms  (%s)"
             % (src.name, pick + 1, sh["rise_ms"],
                "60 fps" if src.fps > 50 else "%.1f fps" % src.fps))
    cv2.putText(head, title,
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.imwrite(out_path, np.vstack([head, grid]))
    return {"path": out_path, "shot": pick + 1}


# --------------------------------------------------------------------------- #
#  10. main
# --------------------------------------------------------------------------- #
def duty_sweep(pose, frame, out_path, seconds=8.0):
    """THE deployment question: a shadow thread that runs 'press windows only, 30 Hz'
    leaves the GPU idle between calls.  A 3060 drops its SM clock when idle, so the same
    graph costs several times more at a low duty than flat out.  Measured here directly:
    the same frame, the same session, at a range of call rates."""
    x, r, dx, dy = pose.pre(frame)
    res = []
    for hz in (0, 5, 10, 15, 30, 60):
        period = 0.0 if hz == 0 else 1.0 / hz
        n = int(seconds / max(period, 1.0 / 240.0))
        n = max(40, min(n, 600))
        ts = []
        t_end = time.perf_counter() + seconds
        nxt = time.perf_counter()
        while time.perf_counter() < t_end and len(ts) < n:
            if period:
                nxt += period
                s = nxt - time.perf_counter()
                if s > 0.0015:
                    time.sleep(s - 0.0005)
            t0 = time.perf_counter()
            pose.infer(x)
            ts.append((time.perf_counter() - t0) * 1000.0)
        warm = ts[3:]
        res.append({"hz": hz if hz else "max", "n": len(warm),
                    "first3_ms": [round(v, 2) for v in ts[:3]], **summarise(warm)})
        print("   duty %5s Hz : p50 %6.2f  p90 %6.2f  p99 %6.2f  (cold first 3: %s)"
              % (res[-1]["hz"], res[-1].get("p50", 0), res[-1].get("p90", 0),
                 res[-1].get("p99", 0), res[-1]["first3_ms"]), flush=True)
    # and the cold-start cost of a WINDOW: 700 ms idle, then a 30 Hz burst of 20
    bursts = []
    for _ in range(12):
        time.sleep(0.7)
        b = []
        for k in range(20):
            t0 = time.perf_counter()
            pose.infer(x)
            b.append((time.perf_counter() - t0) * 1000.0)
            s = 1.0 / 30.0 - (time.perf_counter() - t0)
            if s > 0.0015:
                time.sleep(s)
        bursts.append(b)
    per_pos = [[b[k] for b in bursts] for k in range(20)]
    res_burst = [{"sample": k, "p50": round(pct(per_pos[k], 50), 2),
                  "p90": round(pct(per_pos[k], 90), 2)} for k in range(20)]
    print("   press-window burst (700 ms idle -> 20 calls @30 Hz), p50 per sample:")
    print("   " + " ".join("%.0f" % d["p50"] for d in res_burst))
    doc = {"rates": res, "press_window_burst_30hz_after_700ms_idle": res_burst}
    json.dump(doc, open(out_path, "w"), indent=1)
    return doc


def write_frames_csv(path, rec):
    cols = ["i", "t_src", "n_people", "lock_src", "track_id", "tid_plate", "tid_box",
            "tid_top", "lock_dist", "anchor_conf",
            "score", "box_x", "box_y", "box_w", "box_h", "wr_x", "wr_y", "hand_h",
            "torso_px", "wr_c", "sh_y",
            "kpt_conf_mean", "kpt_ge50", "t_dec", "t_pre", "t_inf", "t_post", "t_lock",
            "t_pipe"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in rec:
            w.writerow({c: (round(r[c], 4) if isinstance(r.get(c), float) else r.get(c))
                        for c in cols})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", default=[],
                    help="source name (repeatable); default = every discovered source")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--imgsz", type=int, default=448)
    ap.add_argument("--conf", type=float, default=0.08)
    ap.add_argument("--lock", default="auto", choices=["auto", "plate", "box", "top"])
    ap.add_argument("--annotate", action="store_true", default=True)
    ap.add_argument("--no-annotate", dest="annotate", action="store_false")
    ap.add_argument("--max-pass", dest="maxpass", action="store_true", default=True,
                    help="also run an unpaced max-throughput pass")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--windows", action="store_true",
                    help="dump sources: keep only press/release windows (the live duty)")
    ap.add_argument("--win-pre-ms", type=float, default=900.0)
    ap.add_argument("--win-post-ms", type=float, default=900.0)
    ap.add_argument("--duty-sweep", dest="duty", action="store_true",
                    help="measure inference latency vs call rate (the 30 Hz shadow-thread"
                         " question) and exit")
    ap.add_argument("--out", default=OUT_ROOT)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    srcs = discover()
    if args.list:
        for s in srcs:
            print("%-28s %-8s %dx%d  %.2f fps  %d frames" %
                  (s.name, s.kind, s.w, s.h, s.fps, len(s)))
        return
    if args.source:
        want = set(args.source)
        srcs = [s for s in srcs if s.name in want]
    if not srcs:
        raise SystemExit("no sources (use --list)")

    pose = PoseRT(imgsz=args.imgsz, conf=args.conf)
    print("model  : %s" % pose.path)
    print("provider: %s" % pose.providers[0])
    print("sources: %s" % ", ".join(s.name for s in srcs))

    if args.duty:
        s0 = srcs[0].open()
        frame = next(s0.frames())[2]
        s0.close()
        duty_sweep(pose, frame, os.path.join(args.out, "duty_sweep.json"))
        return

    summary = {"model": pose.path, "imgsz": args.imgsz, "conf": args.conf,
               "providers": pose.providers,
               "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
               "ort": ort.__version__, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
               "sources": []}

    for s in srcs:
        if args.windows and s.kind == "dump":
            times = release_times_wall(s)
            if times:
                s.restrict_to_windows(times, args.win_pre_ms, args.win_post_ms)
                print("   windows: %d releases -> %d frames" % (len(times), len(s)))
        if args.start or (args.limit and s.kind != "video"):
            s.restrict(args.start, args.limit)
        print("== %s (%s, %dx%d, %.2f fps, %d frames)" %
              (s.name, s.kind, s.w, s.h, s.fps, len(s)), flush=True)
        rec, out = run_source(s, pose, mode="native", lock_mode=args.lock,
                              limit=args.limit)
        if args.maxpass:
            _, outmax = run_source(s, pose, mode="max", lock_mode=args.lock,
                                   limit=args.limit)
            out["max_pass"] = {k: outmax[k] for k in
                               ("frames", "wall_s", "achieved_fps", "stage_ms",
                                "dropped_if_pinned_60fps", "dropped_pct_60fps",
                                "telemetry")}
        shots = analyse(rec, s, out)
        write_frames_csv(os.path.join(args.out, "rt_%s_frames.csv" % s.name), rec)
        if out["shots"]:
            cols = list(out["shots"][0].keys())
            with open(os.path.join(args.out, "rt_%s_shots.csv" % s.name), "w",
                      newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, cols)
                w.writeheader()
                w.writerows(out["shots"])
        if args.annotate:
            rel = None
            if s.kind == "dump" and getattr(s, "t0_wall", None):
                rel = [(x["rel_t"] - s.t0_wall) * 1000.0
                       for x in session_releases(s) if x["rel_t"]]
            v = write_video(s, rec, shots,
                            os.path.join(args.out, "annot_%s.mp4" % s.name),
                            events_rel=rel)
            out["video"] = v
            at, lab = None, ""
            if s.kind == "dump" and getattr(s, "t0_wall", None):
                # a TRUE release beats a detected apex -- prefer a graded one
                ts = [r["t_src"] for r in rec]
                cands = []
                for x in session_releases(s):
                    rt = (x["rel_t"] - s.t0_wall) * 1000.0
                    dens = sum(1 for v in ts if rt - 700.0 <= v <= rt + 500.0)
                    cands.append((dens, x.get("verdict", "") == "EXCELLENT", rt, x))
                cands.sort(key=lambda c: (-c[0], not c[1]))
                if cands and cands[0][0] >= 6:
                    at = cands[0][2]
                    lab = "RELEASE seq %s %s" % (cands[0][3]["seq"],
                                                 cands[0][3].get("verdict", ""))
            sheet = write_sheet(s, rec, shots,
                                os.path.join(args.out, "sheet_%s.png" % s.name),
                                anchor_t=at, anchor_label=lab)
            out["sheet"] = sheet
            if v:
                print("  video : %s" % v["path"])
            if sheet:
                print("  sheet : %s" % sheet["path"])
        for lab, key in (("set->peakvel", "set_to_peakvel_ms"),
                         ("set->top90  ", "set_to_top90_ms"),
                         ("set->apex   ", "set_to_apex_ms")):
            d = out.get(key) or {}
            print("  %s n=%s median %s ms  sd %s  mad_sd %s"
                  % (lab, d.get("n"), d.get("median"), d.get("sd"), d.get("mad_sd")))
        print("  shots : %d   flips/shot %s" % (out["n_shots"],
                                                out["lock_flips_per_shot"]), flush=True)
        if "set_to_release_ms" in out:
            print("  set->release: n=%d median %s ms  sd %s  mad_sd %s"
                  % (out["set_to_release_ms"]["n"], out["set_to_release_ms"]["median"],
                     out["set_to_release_ms"]["sd"], out["set_to_release_ms"]["mad_sd"]))
        json.dump(out, open(os.path.join(args.out, "rt_%s.json" % s.name), "w"),
                  indent=1, default=str)
        summary["sources"].append(out)

    # merge with whatever earlier invocations left, keyed on the source name, so the
    # summary is the whole study and not just this command line
    sp = os.path.join(args.out, "pose_rt_summary.json")
    if os.path.exists(sp):
        try:
            old = json.load(open(sp))
            names = {o["source"] for o in summary["sources"]}
            summary["sources"] = ([o for o in old.get("sources", [])
                                   if o.get("source") not in names]
                                  + summary["sources"])
            summary["sources"].sort(key=lambda o: o.get("source", ""))
        except Exception:
            pass
    json.dump(summary, open(sp, "w"), indent=1, default=str)
    print("\nwrote %s" % os.path.join(args.out, "pose_rt_summary.json"))


if __name__ == "__main__":
    main()
