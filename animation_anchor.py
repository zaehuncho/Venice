"""Animation-skeleton release anchor (shadow-first). See docs/ANIMATION_ANCHOR.md.

Computes a player-animation PHASE anchor (a repeatable landmark in the shooting animation) as a
candidate timing reference INDEPENDENT of the shot meter. The point: the meter-appear anchor is noisy
(detection locks on at 34-97% fill -> ~+-73ms appear->tip scatter, larger than the ~23-36ms green
window). A steadier physical landmark could tighten timing.

This module NEVER controls release. It is consumed in SHADOW mode (compute + log) until a live batch
proves the landmark->tip timing is more consistent than the meter-appear anchor (see the live plan in
the doc). Default OFF -> a true no-op.

Design notes:
- Pluggable `AnchorDetector` so a classical detector (default) or a pose backend are interchangeable.
- Default `MotionPeakDetector` is classical (numpy frame-diff motion energy), sub-ms, no NN / no new
  dependency, so it can never add the inference latency that would defeat an autogreener.
- The detector only needs to fire a REPEATABLE event; its constant offset to the meter tip is what the
  live analysis calibrates. So "which animation phase exactly" is decided from data, not assumed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

Region = Optional[Tuple[int, int, int, int]]  # (x, y, w, h) in frame pixels


@dataclass
class AnchorResult:
    found: bool = False        # a phase landmark fired THIS frame
    kind: str = ""             # e.g. "motion_peak"
    confidence: float = 0.0    # 0..1
    ts: float = 0.0            # perf_counter() seconds at this frame
    motion: float = 0.0        # diagnostic: smoothed motion energy this frame


def _to_gray(frame_bgr: np.ndarray, region: Region) -> Optional[np.ndarray]:
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return None
    f = frame_bgr
    if region is not None:
        x, y, w, h = (int(v) for v in region)
        H, W = f.shape[:2]
        x0 = max(0, min(W - 1, x)); y0 = max(0, min(H - 1, y))
        x1 = max(x0 + 1, min(W, x + w)); y1 = max(y0 + 1, min(H, y + h))
        f = f[y0:y1, x0:x1]
    if f.size == 0:
        return None
    # Cheap luma proxy (mean over channels); float32 so the frame-diff doesn't wrap uint8.
    return f.astype(np.float32).mean(axis=2) if f.ndim == 3 else f.astype(np.float32)


class MotionPeakDetector:
    """Fires once per shot at the smoothed motion-energy PEAK (sustained rise -> fall, above a floor).

    A jump shot has a strong motion signature (gather -> explosive jump -> set/release); the smoothed
    frame-diff energy in the player region peaks at a repeatable phase. CANDIDATE heuristic: the live
    analysis measures peak->tip and its variance before this is ever trusted. reset() per shot.
    """

    def __init__(self, *, motion_floor: float = 2.0, smooth: float = 0.5, min_rise_frames: int = 2):
        self.motion_floor = float(motion_floor)
        self.smooth = float(smooth)
        self.min_rise_frames = int(min_rise_frames)
        self.reset()

    def reset(self) -> None:
        self._prev: Optional[np.ndarray] = None
        self._ema = 0.0
        self._last_ema = 0.0
        self._rising = 0
        self._fired = False

    def update(self, frame_bgr: np.ndarray, region: Region = None) -> AnchorResult:
        ts = time.perf_counter()
        gray = _to_gray(frame_bgr, region)
        if gray is None:
            return AnchorResult(ts=ts)
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return AnchorResult(ts=ts)
        motion = float(np.abs(gray - self._prev).mean())
        self._prev = gray
        self._ema = self._ema * (1.0 - self.smooth) + motion * self.smooth

        result = AnchorResult(ts=ts, motion=self._ema)
        if self._ema > self._last_ema:
            self._rising += 1
        else:
            # Transition from a sustained rise to falling = the previous frame was the peak.
            if self._rising >= self.min_rise_frames and self._last_ema >= self.motion_floor and not self._fired:
                conf = max(0.05, min(1.0, self._last_ema / (self.motion_floor * 3.0)))
                result = AnchorResult(found=True, kind="motion_peak", confidence=conf,
                                      ts=ts, motion=self._ema)
                self._fired = True
            self._rising = 0
        self._last_ema = self._ema
        return result


class _DisabledDetector:
    def update(self, frame_bgr: np.ndarray, region: Region = None) -> AnchorResult:
        return AnchorResult()

    def reset(self) -> None:
        pass


class AnimationAnchor:
    """Flag-gated facade. enabled=False -> a true no-op (only the flag check runs)."""

    def __init__(self, enabled: bool = False, detector: Optional[object] = None):
        self.enabled = bool(enabled)
        self._detector = detector if detector is not None else MotionPeakDetector()

    def update(self, frame_bgr: np.ndarray, region: Region = None) -> AnchorResult:
        if not self.enabled:
            return AnchorResult()
        return self._detector.update(frame_bgr, region)

    def reset(self) -> None:
        self._detector.reset()
