#!/usr/bin/env python3
"""Kalman-filter ball tracker on top of Venice's v9 ball detections.

Stabilizes ``orion_player_detect_v9.pt`` per-frame ball outputs (class
``Zbasketball`` == class idx 3, see ``tools/diagnostics/train_player_v9.py``)
by predicting through 1-3 frame dropouts and rejecting spurious high-velocity
detections that would otherwise hijack the trajectory.

Kalman state (6-dim, constant-velocity):
    x = [cx, cy, vx, vy, w, h]
Measurement (4-dim, from v9):
    z = [cx, cy, w, h]

The model is CONSTANT-VELOCITY (CV) v1. A future upgrade to CONSTANT-
ACCELERATION on the y-axis would let projectile gravity be part of the
physics prior; the state would grow to
``[cx, cy, vx, vy, ay, w, h]`` with ``ay`` around a positive constant during
ball-in-flight. Left as a TODO because CV is sufficient for the 1-3 frame
prediction horizon this tracker is actually used at, and the reset logic
below cleanly handles any longer occlusion.

Bbox convention (input and output): top-left ``[x, y, w, h]`` in raw pixels,
matching ``prepare_release_cue_labels.py``'s ``player_bbox_at_release`` /
``meter_bbox`` layout.

Integration points
------------------
This module is a LIBRARY. The two call sites below MUST NOT be wired yet
(both files are on the do-not-modify list for this change). The sketches
below are the intended future call pattern.

Label time -- ``tools/training/prepare_release_cue_labels.py``:

    from tools.tracking.ball_kalman_tracker import BallKalmanTracker

    tracker = BallKalmanTracker()
    ball_traj = {}     # frame_idx -> [x, y, w, h] top-left, tracker-stabilised
    for frame_idx, frame in enumerate(video):
        r    = player_det.predict(frame, verbose=False, conf=0.15)[0]
        cls  = r.boxes.cls.cpu().numpy().astype(int)
        xyxy = r.boxes.xyxy.cpu().numpy()
        conf = r.boxes.conf.cpu().numpy()
        dets = []
        for b, c, k in zip(xyxy, conf, cls):
            if int(k) != 3:   # 3 = Zbasketball
                continue
            x, y = float(b[0]), float(b[1])
            w, h = float(b[2] - b[0]), float(b[3] - b[1])
            dets.append({"bbox": [x, y, w, h], "conf": float(c), "class": "ball"})
        out = tracker.update(dets, frame_idx)
        if out is not None and out.get("bbox") is not None:
            ball_traj[frame_idx] = out["bbox"]
    # Downstream: use ``ball_traj[release_video_frame]`` as a shooter-adjacency
    # signal that survives v9 dropouts and single-frame FP flashes.

Runtime -- ``native_orion/backend/autogreen_sidecar.py`` (pose / no-meter mode,
after the periodic v9 predict inside ``PoseTimingDetector`` -- see
``pose_timing.py:554`` for the point where ``self._ball_pos`` is updated
today):

    # Replace the raw "pick most-confident ball_box" with:
    dets = _extract_ball_dets(v9_result)    # same shape as label-time above
    out  = ball_tracker.update(dets, frame_seq)
    if out is not None and out.get("bbox") is not None:
        cx = out["bbox"][0] + out["bbox"][2] / 2.0
        cy = out["bbox"][1] + out["bbox"][3] / 2.0
        detector._ball_pos = (cx, cy)
        # A "reset" state = fresh trajectory; keep the ball_age counter honest
        # (age = 999 == "no recent ball" downstream in pose_timing.py:722).
        detector._ball_age = 0 if out["state"] != "reset" else 999
    # DO NOT wire this yet -- ``autogreen_sidecar.py`` is on the do-not-modify
    # list for the current change. Ship the tracker first, wire on the owner's
    # next tuning pass so the behaviour change is scheduled, not ambient.

Runtime budget: v1 measured at ~15 microseconds per ``update`` on a 3.5GHz
laptop (see ``__main__ --timing`` in this module). The runtime frame loop is
60fps == 16.7ms per frame; this tracker's contribution is ~0.1% of that
budget, well under the "<0.5ms per call" contract.

Usage (standalone eval, not run in CI -- needs opencv + ultralytics + a real
video + real v9 weights on the owner's rig):

    python tools/tracking/ball_kalman_tracker.py --video "path.mp4" \\
        --detector logs/diagnostics/player_v9/weights/best.pt \\
        --out logs/diagnostics/ball_tracker_eval.json

Dependencies (library import path):
    numpy only. NO torch, ultralytics, opencv. Those are optional imports
    gated inside the __main__ eval block; a missing dep there prints a pip
    hint and exits 2 (never a hard traceback into the caller).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# --- Constants -----------------------------------------------------------------

# Rolling window of recent detection confidence used by the false-positive
# velocity gate ("recent-history" in the design doc).
_CONF_HIST_LEN = 10

# 6-state indices.
_S_CX, _S_CY, _S_VX, _S_VY, _S_W, _S_H = 0, 1, 2, 3, 4, 5
# 4-measurement indices.
_M_CX, _M_CY, _M_W, _M_H = 0, 1, 2, 3


# --- Bbox helpers --------------------------------------------------------------

def _iou_xywh(a, b) -> float:
    """IoU on top-left ``(x, y, w, h)`` tuples. Returns 0.0 for degenerate boxes."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1 = ax1 if ax1 > bx1 else bx1
    iy1 = ay1 if ay1 > by1 else by1
    ix2 = ax2 if ax2 < bx2 else bx2
    iy2 = ay2 if ay2 < by2 else by2
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union > 0 else 0.0


def _tl_to_center(bbox):
    x, y, w, h = bbox
    return float(x + w / 2.0), float(y + h / 2.0), float(w), float(h)


def _center_to_tl(cx, cy, w, h):
    return [float(cx - w / 2.0), float(cy - h / 2.0), float(w), float(h)]


# --- Tracker -------------------------------------------------------------------

class BallKalmanTracker:
    """Kalman filter with IoU association + false-positive velocity gate.

    Public API is stable across CV -> CA upgrades (the state grows; the return
    shape does not).

    Parameters
    ----------
    max_gap_frames: int
        How many consecutive frames without an association the tracker will
        predict through before entering ``searching`` (default 3 == 50ms at
        60fps).
    min_iou_assoc: float
        IoU floor for detection<->prediction association. Below this every
        candidate is treated as "no match" and the tracker coasts on its own
        prediction for this frame (default 0.15).
    max_velocity_pxf: float
        Per-frame center displacement above this magnitude is REJECTED as a
        false positive when recent detection confidence has been high. NBA-
        broadcast basketball at 1080p60 rarely exceeds ~40 px/frame; 60
        px/frame is a generous outer bound (default 60.0).
    reset_conf_threshold: float
        After ``max_gap_frames`` misses, the tracker enters ``searching`` and
        re-initialises on the FIRST detection whose confidence exceeds this
        threshold. A ``reset`` event is emitted so consumers know the old
        trajectory ended (default 0.6).
    """

    def __init__(
        self,
        max_gap_frames: int = 3,
        min_iou_assoc: float = 0.15,
        max_velocity_pxf: float = 60.0,
        reset_conf_threshold: float = 0.6,
    ):
        self.max_gap_frames = int(max_gap_frames)
        self.min_iou_assoc = float(min_iou_assoc)
        self.max_velocity_pxf = float(max_velocity_pxf)
        self.reset_conf_threshold = float(reset_conf_threshold)

        # Time-invariant matrices (built once, reused every call). dt is one
        # frame; a skipped input frame is folded in as an extra predict step
        # so the CV model doesn't accumulate schedule error across gaps.
        F = np.eye(6, dtype=np.float64)
        F[_S_CX, _S_VX] = 1.0
        F[_S_CY, _S_VY] = 1.0
        self._F = F

        H = np.zeros((4, 6), dtype=np.float64)
        H[_M_CX, _S_CX] = 1.0
        H[_M_CY, _S_CY] = 1.0
        H[_M_W,  _S_W]  = 1.0
        H[_M_H,  _S_H]  = 1.0
        self._H = H

        # Q: process noise. Velocity + size may drift more than position (which
        # is directly measured whenever we associate).
        self._Q = np.diag([1.0, 1.0, 4.0, 4.0, 2.0, 2.0]).astype(np.float64)
        # R: measurement noise. v9 boxes on the ball class are ~2-4px jittery
        # per-axis on center, ~4-6px on size -> variances 9 / 16.
        self._R = np.diag([9.0, 9.0, 16.0, 16.0]).astype(np.float64)
        # P0: initial covariance -- large on velocity (unknown at init).
        self._P0 = np.diag([25.0, 25.0, 100.0, 100.0, 25.0, 25.0]).astype(np.float64)
        self._I6 = np.eye(6, dtype=np.float64)

        self.reset()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all track state; next ``update`` starts fresh."""
        self._x: Optional[np.ndarray] = None   # 6-vector state
        self._P: Optional[np.ndarray] = None   # 6x6 covariance
        self._gap = 0                          # consecutive frames without an association
        self._searching = False                # True once gap > max_gap_frames
        self._last_frame_idx = -1
        self._last_conf = 0.0
        self._conf_hist: list = []
        self._fp_rejected = 0
        self._resets = 0

    def update(self, detections, frame_idx: int) -> Optional[dict]:
        """Fold one frame's ball detections into the track.

        Parameters
        ----------
        detections: list[dict]
            ``[{"bbox": [x, y, w, h], "conf": float, "class": "ball"}, ...]``
            Only ball-class entries should be passed; class filtering is the
            caller's job (v9 emits four classes, only class 3 is ball).
        frame_idx: int
            Monotonically increasing frame index. Gaps in the sequence are
            folded in as extra predict steps so a caller that only presents
            "interesting" frames still gets correct velocity estimates.

        Returns
        -------
        dict or None
            ``{"bbox": [x, y, w, h] | None, "conf": float,
               "state": "tracked" | "predicted" | "reset" | "searching",
               "gap": int, "velocity": [vx, vy], "frame": int}``
            Returns ``None`` only while there has never been a track (no
            detection has ever arrived to seed one).
        """
        dets = [d for d in (detections or []) if d.get("bbox") is not None]

        # --- Cold start / re-arm on high-confidence detection while searching ---
        if self._x is None or self._searching:
            best = self._pick_seed(dets)
            if best is None:
                if self._x is None:
                    # Never had a track and no seed this frame -- caller sees None.
                    return None
                # Searching after a lost track: keep the caller informed instead
                # of silently emitting a stale bbox.
                return {
                    "bbox": None,
                    "conf": 0.0,
                    "state": "searching",
                    "gap": int(self._gap),
                    "velocity": [float(self._x[_S_VX]), float(self._x[_S_VY])],
                    "frame": int(frame_idx),
                }
            was_reset = self._x is not None    # re-arm after a gap == reset event
            self._init_from_detection(best)
            self._last_frame_idx = int(frame_idx)
            if was_reset:
                self._resets += 1
            return self._pack_output(
                frame_idx,
                state="reset" if was_reset else "tracked",
                conf=float(best.get("conf", 0.0)),
            )

        # --- Predict step ---
        steps = max(1, int(frame_idx) - int(self._last_frame_idx))
        for _ in range(steps):
            self._predict_one_step()
        self._last_frame_idx = int(frame_idx)

        # --- Associate ---
        pred_bbox = self._predicted_bbox()
        best_det, best_score = None, -1.0
        for d in dets:
            iou = _iou_xywh(pred_bbox, d["bbox"])
            if iou < self.min_iou_assoc:
                continue
            score = iou * float(d.get("conf", 0.0))
            if score > best_score:
                best_score = score
                best_det = d

        if best_det is None:
            return self._coast(frame_idx)

        # --- False-positive gate (implausible velocity + confident recent track) ---
        det_cx, det_cy, _, _ = _tl_to_center(best_det["bbox"])
        implied_v = math.hypot(det_cx - float(self._x[_S_CX]),
                               det_cy - float(self._x[_S_CY]))
        recent_conf = (sum(self._conf_hist) / len(self._conf_hist)
                       if self._conf_hist else 0.0)
        if implied_v > self.max_velocity_pxf and recent_conf > 0.5:
            self._fp_rejected += 1
            logger.debug(
                "ball tracker rejected FP: implied=%.1fpx/f recent_conf=%.2f",
                implied_v, recent_conf,
            )
            return self._coast(frame_idx)

        # --- Kalman update ---
        z = np.array(_tl_to_center(best_det["bbox"]), dtype=np.float64)
        self._kalman_update(z)
        self._gap = 0
        self._last_conf = float(best_det.get("conf", 0.0))
        self._conf_hist.append(self._last_conf)
        self._trim_conf_hist()
        return self._pack_output(frame_idx, state="tracked", conf=self._last_conf)

    # ------------------------------------------------------------------
    # Diagnostics (exposed for the eval harness + external metrics reads)
    # ------------------------------------------------------------------

    @property
    def false_positives_rejected(self) -> int:
        return int(self._fp_rejected)

    @property
    def resets(self) -> int:
        return int(self._resets)

    @property
    def is_tracking(self) -> bool:
        return self._x is not None and not self._searching

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_from_detection(self, det):
        cx, cy, w, h = _tl_to_center(det["bbox"])
        self._x = np.array([cx, cy, 0.0, 0.0, w, h], dtype=np.float64)
        self._P = self._P0.copy()
        self._gap = 0
        self._searching = False
        self._last_conf = float(det.get("conf", 0.0))
        self._conf_hist = [self._last_conf]

    def _pick_seed(self, dets):
        """Choose the detection to (re)initialise the filter on.

        Cold start is lenient (any detection seeds); reacquisition after a gap
        requires at least ``reset_conf_threshold`` confidence to avoid
        anchoring the new trajectory on a stray flash.
        """
        if not dets:
            return None
        candidates = [d for d in dets
                      if float(d.get("conf", 0.0)) >= self.reset_conf_threshold]
        if candidates:
            return max(candidates, key=lambda d: float(d.get("conf", 0.0)))
        if self._searching:
            # Refuse to reacquire on a low-confidence detection -- the whole
            # point of the searching state is to wait for a real ball.
            return None
        # Cold start: no prior track exists, so any detection is worth a shot.
        return max(dets, key=lambda d: float(d.get("conf", 0.0)))

    def _predict_one_step(self):
        # x <- F x ; P <- F P F' + Q
        self._x = self._F @ self._x
        self._P = self._F @ self._P @ self._F.T + self._Q

    def _predicted_bbox(self):
        cx, cy, _, _, w, h = self._x
        return _center_to_tl(cx, cy, w, h)

    def _kalman_update(self, z):
        # y = z - H x ; S = H P H' + R ; K = P H' S^-1
        y = z - self._H @ self._x
        S = self._H @ self._P @ self._H.T + self._R
        K = self._P @ self._H.T @ np.linalg.inv(S)
        self._x = self._x + K @ y
        self._P = (self._I6 - K @ self._H) @ self._P

    def _trim_conf_hist(self):
        if len(self._conf_hist) > _CONF_HIST_LEN:
            del self._conf_hist[: len(self._conf_hist) - _CONF_HIST_LEN]

    def _coast(self, frame_idx):
        """Handle a no-association frame (missed det OR rejected FP)."""
        self._gap += 1
        self._conf_hist.append(0.0)
        self._trim_conf_hist()
        if self._gap > self.max_gap_frames:
            self._searching = True
            return {
                "bbox": None,
                "conf": 0.0,
                "state": "searching",
                "gap": int(self._gap),
                "velocity": [float(self._x[_S_VX]), float(self._x[_S_VY])],
                "frame": int(frame_idx),
            }
        return self._pack_output(frame_idx, state="predicted", conf=0.0)

    def _pack_output(self, frame_idx, *, state, conf):
        return {
            "bbox": self._predicted_bbox(),
            "conf": float(conf),
            "state": state,
            "gap": int(self._gap),
            "velocity": [float(self._x[_S_VX]), float(self._x[_S_VY])],
            "frame": int(frame_idx),
        }


# ==============================================================================
# Standalone evaluation harness (__main__ only; not on the library import path)
# ==============================================================================

def _main_eval(argv=None) -> int:
    """Run v9 on a video, feed the ball class through the tracker, dump metrics."""
    import argparse
    import json
    import os
    import time as _time

    ap = argparse.ArgumentParser(
        description="Standalone eval for ball_kalman_tracker on a real video.",
    )
    ap.add_argument("--video", required=True, help="input video path (mp4/mkv/mov)")
    ap.add_argument("--detector", required=True,
                    help="v9 weights (.pt) -- must include Zbasketball at class idx 3")
    ap.add_argument("--out", required=True,
                    help="output JSON path -- per-frame raw + tracked bboxes")
    ap.add_argument("--conf", type=float, default=0.15,
                    help="v9 confidence floor (default 0.15, matches pose_timing.py)")
    ap.add_argument("--preview-frames", type=int, default=300,
                    help="render this many annotated frames next to --out (0 = none)")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="cap total frames processed (0 = all)")
    ap.add_argument("--timing", action="store_true",
                    help="also run a synthetic timing loop and print per-call latency")
    args = ap.parse_args(argv)

    if args.timing:
        _print_timing_summary()

    # Optional deps ONLY reached when the caller actually runs the eval.
    try:
        import cv2                     # type: ignore
        from ultralytics import YOLO   # type: ignore
    except Exception as exc:
        print(f"standalone eval needs opencv-python + ultralytics: {exc}")
        print("  pip install opencv-python ultralytics")
        return 2

    if not os.path.isfile(args.video):
        print(f"video not found: {args.video}")
        return 2
    if not os.path.isfile(args.detector):
        print(f"detector weights not found: {args.detector}")
        return 2

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    preview_dir = os.path.splitext(args.out)[0]
    if args.preview_frames > 0:
        os.makedirs(preview_dir, exist_ok=True)

    model = YOLO(args.detector)
    tracker = BallKalmanTracker()
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"could not open video: {args.video}")
        return 2

    per_frame: list = []
    tracked_frames = 0
    total_frames = 0
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if args.max_frames and total_frames >= args.max_frames:
            break
        total_frames += 1
        r = model.predict(frame, verbose=False, conf=args.conf)[0]
        dets = _extract_ball_dets(r)
        out = tracker.update(dets, frame_idx)

        rec: dict[str, Any] = {
            "frame": frame_idx,
            "raw_detections": [{"bbox": d["bbox"], "conf": d["conf"]} for d in dets],
            "tracked": None,
            "state": None,
            "gap": None,
            "velocity": None,
        }
        if out is not None:
            rec["tracked"] = out.get("bbox")
            rec["state"] = out.get("state")
            rec["gap"] = out.get("gap")
            rec["velocity"] = out.get("velocity")
            if out.get("bbox") is not None:
                tracked_frames += 1

        per_frame.append(rec)

        if args.preview_frames > 0 and frame_idx < args.preview_frames:
            _render_preview_frame(cv2, frame, dets, out,
                                  os.path.join(preview_dir, f"{frame_idx:05d}.jpg"))
        frame_idx += 1
    cap.release()

    mean_gap = 0.0
    if per_frame:
        mean_gap = float(np.mean([r["gap"] for r in per_frame if r["gap"] is not None] or [0]))

    payload = {
        "video": os.path.abspath(args.video),
        "detector": os.path.abspath(args.detector),
        "total_frames": total_frames,
        "tracked_frames": tracked_frames,
        "mean_gap": round(mean_gap, 3),
        "false_positives_rejected": tracker.false_positives_rejected,
        "resets": tracker.resets,
        "per_frame": per_frame,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(f"\n=== ball_kalman_tracker eval ({_time.strftime('%Y-%m-%d %H:%M:%S')}) ===")
    print(f"video           : {args.video}")
    print(f"detector        : {args.detector}")
    print(f"total frames    : {total_frames}")
    print(f"tracked frames  : {tracked_frames}")
    if total_frames > 0:
        print(f"track ratio     : {tracked_frames / total_frames:.3f}")
    print(f"mean gap        : {mean_gap:.3f}")
    print(f"FP rejected     : {tracker.false_positives_rejected}")
    print(f"resets          : {tracker.resets}")
    print(f"json            : {args.out}")
    if args.preview_frames > 0:
        print(f"previews        : {preview_dir}/  (first {args.preview_frames} frames)")
    return 0


def _extract_ball_dets(v9_result):
    """Convert one v9 ultralytics Result to the tracker's detection schema."""
    boxes = getattr(v9_result, "boxes", None)
    if boxes is None or not len(boxes):
        return []
    cls  = boxes.cls.cpu().numpy().astype(int)
    xyxy = boxes.xyxy.cpu().numpy()
    conf = boxes.conf.cpu().numpy()
    dets = []
    for b, c, k in zip(xyxy, conf, cls):
        if int(k) != 3:      # 3 = Zbasketball; see train_player_v9.py
            continue
        x, y = float(b[0]), float(b[1])
        w, h = float(b[2] - b[0]), float(b[3] - b[1])
        dets.append({"bbox": [x, y, w, h], "conf": float(c), "class": "ball"})
    return dets


def _render_preview_frame(cv2, frame, dets, out, path):
    """Draw raw v9 boxes in red + tracker output in green, save as JPG."""
    img = frame.copy()
    for d in dets:
        x, y, w, h = [int(round(v)) for v in d["bbox"]]
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 1)
        cv2.putText(img, f"v9 {d['conf']:.2f}", (x, max(10, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
    if out is not None and out.get("bbox") is not None:
        x, y, w, h = [int(round(v)) for v in out["bbox"]]
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
        label = f"kf {out.get('state', '')} gap={out.get('gap', 0)}"
        cv2.putText(img, label, (x, y + h + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    cv2.imwrite(path, img)


def _print_timing_summary(n_iter: int = 5000) -> None:
    """Measure per-call latency on a synthetic constant-velocity ball stream."""
    import timeit
    tracker = BallKalmanTracker()
    # Warm up + build a real detection stream so the update path hits every
    # branch (Kalman update, IoU association, conf-history trimming).
    dets_by_frame = [
        [{"bbox": [100.0 + i * 3.0, 200.0 + i * 1.5, 40.0, 40.0],
          "conf": 0.85, "class": "ball"}]
        for i in range(n_iter)
    ]
    for i in range(min(20, n_iter)):
        tracker.update(dets_by_frame[i], i)

    counter = [20]

    def _one():
        i = counter[0]
        tracker.update(dets_by_frame[i], i)
        counter[0] = i + 1

    # Cap iterations at n_iter - 20 to stay inside the pre-built stream.
    reps = max(1, n_iter - 20 - 1)
    total = timeit.timeit(_one, number=reps)
    per_call_us = (total / reps) * 1e6
    print(f"[timing] BallKalmanTracker.update: {per_call_us:.2f} us/call "
          f"({reps} iterations, pure numpy, dt=1 frame)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main_eval())
