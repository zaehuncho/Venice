#!/usr/bin/env python
"""dual_capture_recorder.py -- M5 dual-capture session recorder (teacher + student).

Runs BOTH video taps off one PS5 simultaneously and archives a self-labeling session:

  TEACHER  capture card @1080p60 -> SimpleMeterReader online (0.6 ms/frame) -> teacher.csv
           (detframes-like rows) + band-strip PNGs during shot windows.
  STUDENT  the chiaki-ng ORFR frame pipe -> RAW NV12 payloads archived LOSSLESSLY (the
           codec artifacts ARE the training signal) + per-frame (seq, pts, epoch, activity)
           index. Activity = mean |dY| over the scaled band region, computed inline so the
           aligner (dual_capture_align.py) never needs pixel access for Stage 2.

Shot-gated writes keep the disk budget sane: both taps hold RAM pre-roll rings; the
teacher's online reader triggers on 2 consecutive rising frames, flushes both pre-rolls,
streams until the shot spends (+ tail), and RETROACTIVELY re-runs the reader over the
teacher pre-roll with the armed gates forced (recovers the first 3-7 early-rise frames,
written to teacher_retro.csv). Off-shot: 1 student frame / 2 s as hard-negative pool.

PREFLIGHT (per session -- the aligner's affine tripwire will quarantine violations):
  * PS5 video output pinned 1080p60, HDR OFF, VRR OFF, RGB range fixed
  * ONE ladder rung per session (pass --rung; never change it mid-session)
  * the ORFR pipe is single-consumer: the live bot must NOT be in decoder mode
  * the capture card must be free (the bot must not hold it either)

Usage:
  python tools/diagnostics/dual_capture_recorder.py --out logs/dual/S1 --rung Balanced \
      [--duration-s 1800] [--card-index 0]

The core logic (ShotGate, DualCaptureRecorder) is source-agnostic and unit-tested with
synthetic taps; only the two thin adapters at the bottom touch real hardware.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Load the PRODUCTION reader by explicit path: `tools/diagnostics/simple_meter_reader.py`
# (the retired prototype) shadows the root module whenever this directory is on sys.path
# first -- the same basename collision validate_simple_reader.py works around.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_smr_production", os.path.join(_REPO, "simple_meter_reader.py"))
_smr = importlib.util.module_from_spec(_spec)
sys.modules["_smr_production"] = _smr    # dataclass annotation resolution needs the registry
_spec.loader.exec_module(_smr)
SimpleMeterReader = _smr.SimpleMeterReader

BAND_1080 = (5, 250, 1815, 770)      # the reader's search band @1080p (strip archive + activity)


class ShotGate:
    """The shot-window state machine: IDLE -> (2 consecutive rising frames) -> RECORDING
    -> (spent OR lock lost, + tail_s) -> IDLE. Pure; feed() returns 'start'/'stop'/None."""

    def __init__(self, tail_s: float = 0.7, rising_frames: int = 2):
        self.tail_s = float(tail_s)
        self.rising_frames = int(rising_frames)
        self.active = False
        self._rising = 0
        self._tail_until: Optional[float] = None

    def feed(self, ts: float, detected: bool, rise_state: str) -> Optional[str]:
        if not self.active:
            self._rising = self._rising + 1 if (detected and rise_state == "rising") else 0
            if self._rising >= self.rising_frames:
                self.active = True
                self._tail_until = None
                return "start"
            return None
        if detected and rise_state != "spent":
            self._tail_until = None
            return None
        if self._tail_until is None:
            self._tail_until = ts + self.tail_s
        if ts >= self._tail_until:
            self.active = False
            self._rising = 0
            self._tail_until = None
            return "stop"
        return None


def band_activity(y_prev: Optional[np.ndarray], y_now: np.ndarray,
                  frame_w: int, frame_h: int) -> float:
    """Student activity proxy: mean |dY| over the (resolution-scaled) band, 8:1 subsampled.
    ~20 us at 720p; the aligner's Stage-2 correlation signal."""
    sx, sy = frame_w / 1920.0, frame_h / 1080.0
    x0, y0, x1, y1 = (int(BAND_1080[0] * sx), int(BAND_1080[1] * sy),
                      int(BAND_1080[2] * sx), int(BAND_1080[3] * sy))
    now = y_now[y0:y1:8, x0:x1:8].astype(np.int16)
    if y_prev is None or y_prev.shape != y_now.shape:
        return 0.0
    prev = y_prev[y0:y1:8, x0:x1:8].astype(np.int16)
    return float(np.mean(np.abs(now - prev)))


class DualCaptureRecorder:
    """Source-agnostic core. Sources are duck-typed: .get() -> FrameData-like (attrs:
    frame [BGR ndarray, teacher], y_plane/payload/fmt [student], pts, timestamp_ns,
    epoch_ns, frame_number) or None."""

    def __init__(self, out_dir: str, teacher_src, student_src, *, reader=None,
                 preroll: int = 60, neg_every_s: float = 2.0, meta: Optional[dict] = None):
        self.out = out_dir
        self.teacher_src = teacher_src
        self.student_src = student_src
        self.reader = reader or SimpleMeterReader()
        self.gate = ShotGate()
        self.preroll_n = int(preroll)
        self.neg_every_s = float(neg_every_s)
        self.meta = dict(meta or {})
        self._stop = threading.Event()
        # shared recording window (epoch seconds) -- the student thread gates on this
        self._rec_lock = threading.Lock()
        self._rec_since: Optional[float] = None
        self._shot_id = -1
        # teacher state
        self._t_rows = []
        self._t_retro_rows = []
        self._t_preroll = deque(maxlen=self.preroll_n)   # (epoch_s, band_strip BGR, frame_no)
        self._t_frame_no = 0
        self._strip_count = 0
        # student state
        self._s_rows = []
        self._s_preroll = deque(maxlen=self.preroll_n)   # (meta_row, payload)
        self._s_prev_y: Optional[np.ndarray] = None
        self._s_last_neg = 0.0
        self._s_shotfile = None
        self._s_shotfile_off = 0
        os.makedirs(os.path.join(self.out, "shots"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "teacher_strips"), exist_ok=True)

    # ---------------- teacher side ---------------- #
    def _band_crop(self, frame):
        h, w = frame.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        x0, y0, x1, y1 = (int(BAND_1080[0] * sx), int(BAND_1080[1] * sy),
                          int(BAND_1080[2] * sx), int(BAND_1080[3] * sy))
        return frame[y0:y1, x0:x1].copy(), (x0, y0)

    def teacher_tick(self, fd) -> Optional[str]:
        """One teacher frame: run the reader, log the row, drive the shot gate.
        Returns the gate event for testability."""
        frame = fd.frame
        epoch_s = fd.epoch_ns / 1e9
        self._t_frame_no += 1
        out = self.reader.read(frame, ts=epoch_s)
        bbox = out.get("bbox") or (0, 0, 0, 0)
        h, w = frame.shape[:2]
        self._t_rows.append((epoch_s * 1000.0, fd.timestamp_ns, self._t_frame_no,
                             1 if out.get("detected") else 0,
                             float(out.get("fill", 0.0) or 0.0),
                             float(out.get("confidence", 0.0) or 0.0),
                             int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]),
                             w, h, str(out.get("rise_state", "") or "")))
        strip, _ = self._band_crop(frame)
        self._t_preroll.append((epoch_s, strip, self._t_frame_no))
        event = self.gate.feed(epoch_s, bool(out.get("detected")), str(out.get("rise_state", "")))
        if event == "start":
            self._on_shot_start(epoch_s, w, h)
        elif event == "stop":
            self._on_shot_stop()
        if self.gate.active:
            self._write_strip(self._t_frame_no, strip)
        return event

    def _write_strip(self, frame_no, strip):
        try:
            import cv2
            cv2.imwrite(os.path.join(self.out, "teacher_strips", f"f{frame_no:06d}.png"), strip)
            self._strip_count += 1
        except Exception:
            pass

    def _on_shot_start(self, epoch_s, frame_w, frame_h):
        self._shot_id += 1
        with self._rec_lock:
            self._rec_since = epoch_s - self.preroll_n / 60.0
        # flush the teacher pre-roll strips + RETRO armed re-run over them: a fresh reader
        # with the shot-gate forced re-reads the pre-roll so the first 3-7 early-rise
        # frames (which the cold gates reject) get labeled with hindsight.
        retro = SimpleMeterReader()
        retro.set_shot_state(True, 1.0)
        sx, sy = frame_w / 1920.0, frame_h / 1080.0
        x0, y0 = int(BAND_1080[0] * sx), int(BAND_1080[1] * sy)
        for ts, strip, fno in list(self._t_preroll):
            self._write_strip(fno, strip)
            canvas = np.zeros((frame_h, frame_w, 3), np.uint8)
            canvas[y0:y0 + strip.shape[0], x0:x0 + strip.shape[1]] = strip
            r = retro.read(canvas, ts=ts)
            bb = r.get("bbox") or (0, 0, 0, 0)
            self._t_retro_rows.append((ts * 1000.0, fno, self._shot_id,
                                       1 if r.get("detected") else 0,
                                       float(r.get("fill", 0.0) or 0.0),
                                       float(r.get("confidence", 0.0) or 0.0),
                                       int(bb[0]), int(bb[1]), int(bb[2]), int(bb[3]),
                                       str(r.get("rise_state", "") or "")))

    def _on_shot_stop(self):
        with self._rec_lock:
            self._rec_since = None

    # ---------------- student side ---------------- #
    def student_tick(self, fd) -> Optional[str]:
        """One student frame: activity proxy, pre-roll ring, shot-gated archive.
        Returns 'shot'/'neg'/None for testability."""
        if fd.payload is None or fd.y_plane is None:
            return None
        epoch_s = fd.epoch_ns / 1e9
        h, w = fd.y_plane.shape
        act = band_activity(self._s_prev_y, fd.y_plane, w, h)
        self._s_prev_y = np.array(fd.y_plane)          # own copy (payload buffers rotate)
        row = [fd.frame_number, fd.pts, fd.timestamp_ns, fd.epoch_ns, w, h, fd.fmt, act]
        with self._rec_lock:
            rec_since = self._rec_since
            shot_id = self._shot_id
        if rec_since is not None:
            self._drain_student_preroll(shot_id)
            self._archive_student(row, fd.payload, "shot", shot_id)
            return "shot"
        self._s_preroll.append((row, fd.payload))
        if epoch_s - self._s_last_neg >= self.neg_every_s:
            self._s_last_neg = epoch_s
            self._archive_student(row, fd.payload, "neg", -1)
            return "neg"
        return None

    def _drain_student_preroll(self, shot_id):
        while self._s_preroll:
            row, payload = self._s_preroll.popleft()
            self._archive_student(row, payload, "shot", shot_id)

    def _archive_student(self, row, payload, kind, shot_id):
        fname = f"shots/shot_{shot_id:03d}.nv12" if kind == "shot" else "shots/negatives.nv12"
        path = os.path.join(self.out, fname)
        with open(path, "ab") as fh:
            off = fh.tell()
            fh.write(payload)
        self._s_rows.append((*row, kind, shot_id, fname, off, len(payload)))

    # ---------------- run / flush ---------------- #
    def run(self, duration_s: float):
        for src in (self.teacher_src, self.student_src):
            if hasattr(src, "start") and not src.start():
                raise RuntimeError(f"source failed to start: {src}")
        t_thr = threading.Thread(target=self._pump, args=(self.teacher_src, self.teacher_tick),
                                 name="dc-teacher", daemon=True)
        s_thr = threading.Thread(target=self._pump, args=(self.student_src, self.student_tick),
                                 name="dc-student", daemon=True)
        t_thr.start()
        s_thr.start()
        try:
            end = time.monotonic() + duration_s
            while time.monotonic() < end and not self._stop.is_set():
                time.sleep(0.25)
        finally:
            self._stop.set()
            t_thr.join(timeout=2.0)
            s_thr.join(timeout=2.0)
            for src in (self.teacher_src, self.student_src):
                if hasattr(src, "stop"):
                    try:
                        src.stop()
                    except Exception:
                        pass
            self.flush()

    def _pump(self, src, tick):
        last_no = -1
        while not self._stop.is_set():
            fd = src.get()
            if fd is None:
                time.sleep(0.002)
                continue
            if fd.frame_number == last_no:       # latest-wins ring: dedupe
                time.sleep(0.002)
                continue
            last_no = fd.frame_number
            try:
                tick(fd)
            except Exception as exc:              # a tick error must not kill the tap
                print(f"tick error ({tick.__name__}): {exc}", file=sys.stderr)

    def flush(self):
        def _w(path, header, rows):
            with open(os.path.join(self.out, path), "w", encoding="utf-8") as fh:
                fh.write(header + "\n")
                for r in rows:
                    fh.write(",".join(str(v) for v in r) + "\n")
        _w("teacher.csv",
           "wall_ms,timestamp_ns,frame_number,detected,fill,conf,x,y,w,h,frame_w,frame_h,rise_state",
           self._t_rows)
        _w("teacher_retro.csv",
           "wall_ms,frame_number,shot_id,detected,fill,conf,x,y,w,h,rise_state",
           self._t_retro_rows)
        _w("student_index.csv",
           "seq,pts_us,timestamp_ns,epoch_ns,w,h,fmt,activity,kind,shot_id,file,offset,length",
           self._s_rows)
        self.meta.update({"shots": self._shot_id + 1,
                          "teacher_frames": len(self._t_rows),
                          "student_frames": len(self._s_rows),
                          "teacher_strips": self._strip_count})
        with open(os.path.join(self.out, "session_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(self.meta, fh, indent=2)


# --------------------------------------------------------------------------- #
#  hardware adapters (thin; everything above is unit-tested without them)
# --------------------------------------------------------------------------- #
class CardSource:
    def __init__(self, index: int = 0):
        from capture_card_backend import CaptureCardBackend
        self._be = CaptureCardBackend(device_index=index, width=1920, height=1080, fps=60)

    def start(self):
        return self._be.start()

    def get(self):
        return self._be.get_frame_nonblocking()

    def stop(self):
        self._be.stop()


class PipeSource:
    def __init__(self):
        from chiaki_backend import OrionFramePipeBackend
        self._be = OrionFramePipeBackend(keep_payload=True)

    def start(self):
        return self._be.start()

    def get(self):
        return self._be.get_frame_nonblocking()

    def stop(self):
        self._be.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--rung", required=True,
                    help="the ONE ladder rung this session runs (Quality/Performance/"
                         "Balanced/LowBandwidth/UltraLow) -- recorded to session_meta")
    ap.add_argument("--duration-s", type=float, default=1800.0)
    ap.add_argument("--card-index", type=int, default=0)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    print("PREFLIGHT: PS5 output 1080p60 / HDR off / VRR off; bot NOT in decoder mode; "
          "capture card free. Rung locked to:", args.rung)
    rec = DualCaptureRecorder(args.out, CardSource(args.card_index), PipeSource(),
                              meta={"rung": args.rung, "started_epoch": time.time()})
    rec.run(args.duration_s)
    print(f"session written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
