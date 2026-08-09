from __future__ import annotations

import logging
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from meter_detector import (
    MeterDetector,
    DetectResult,
    DetectorConfig,
    load_detector_config,
)
from chiaki_backend import FrameData

logger = logging.getLogger("RemotePlayCV")


# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

@dataclass
class RemotePlayCVConfig:
    """CV pipeline configuration for Remote Play."""
    # Detection
    meter_color: str = "Purple"
    meter_style: str = "Arrow2"
    confidence_gate: float = 0.32
    min_stable_frames: int = 1
    # Green window
    green_window_start_pct: float = 93.0
    green_window_end_pct: float = 100.0
    green_hsv_low: Optional[List[int]] = None
    green_hsv_high: Optional[List[int]] = None
    green_cluster_min_px: int = 3
    # Adaptive ROI
    roi_enabled: bool = True
    roi_size_px: int = 72
    roi_padding_px: int = 18
    roi_lock_frames: int = 1           # frames of stable detection before locking ROI
    # Ghost engine (meter occlusion handling)
    ghost_enabled: bool = True
    ghost_occlusion_frames: int = 2    # tolerate N frames of occlusion
    ghost_max_extrapolate_ms: float = 200.0
    # Performance
    cuda_enabled: bool = True
    max_process_ms: float = 5.0        # budget per frame
    skip_frames_on_overrun: int = 1    # skip N frames if over budget
    # Super-sampling
    super_sample_window: int = 8


# ---------------------------------------------------------------------------
#  Green Window Analyzer
# ---------------------------------------------------------------------------

class GreenWindowAnalyzer:
    """Dedicated green zone analysis on the meter ROI.

    Uses HSV thresholding to find the green region within the meter,
    returning its position as a fill percentage range.
    """

    # Default green HSV ranges for shot meter green zones
    _DEFAULT_GREEN_HSV = [
        (np.array([40, 80, 80], np.uint8), np.array([80, 255, 255], np.uint8)),
        (np.array([20, 100, 100], np.uint8), np.array([35, 255, 255], np.uint8)),
    ]

    def __init__(self, config: RemotePlayCVConfig) -> None:
        self._config = config
        self._custom_ranges: List[Tuple[np.ndarray, np.ndarray]] = []
        if config.green_hsv_low and config.green_hsv_high:
            self._custom_ranges = [(
                np.array(config.green_hsv_low, np.uint8),
                np.array(config.green_hsv_high, np.uint8),
            )]

    def analyze(
        self,
        frame_bgr: np.ndarray,
        bbox: Tuple[int, int, int, int],
    ) -> Tuple[float, float, float, float, int]:
        """Analyze green window within the meter bounding box.

        Returns:
            (start_pct, end_pct, center_pct, width_pct, green_px_count)
            All values -1 if no green detected.
        """
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return -1.0, -1.0, -1.0, 0.0, 0

        H, W = frame_bgr.shape[:2]
        x1 = max(0, int(x))
        y1 = max(0, int(y))
        x2 = min(W, int(x + w))
        y2 = min(H, int(y + h))
        if x2 <= x1 or y2 <= y1:
            return -1.0, -1.0, -1.0, 0.0, 0

        roi = frame_bgr[y1:y2, x1:x2]
        roi_h, roi_w = roi.shape[:2]
        if roi_h <= 0 or roi_w <= 0:
            return -1.0, -1.0, -1.0, 0.0, 0

        # Convert to HSV
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # Build green mask
        ranges = self._custom_ranges if self._custom_ranges else self._DEFAULT_GREEN_HSV
        mask = np.zeros((roi_h, roi_w), dtype=np.uint8)
        for lo, hi in ranges:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))

        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        green_px = int(cv2.countNonZero(mask))
        if green_px < self._config.green_cluster_min_px:
            return -1.0, -1.0, -1.0, 0.0, green_px

        # Determine if meter is vertical or horizontal
        is_vertical = roi_h >= roi_w * 0.70

        if is_vertical:
            # Green zone rows → fill percentage
            row_counts = np.count_nonzero(mask, axis=1)
            threshold = max(1, roi_w * 0.15)
            green_rows = np.where(row_counts >= threshold)[0]

            if len(green_rows) == 0:
                return -1.0, -1.0, -1.0, 0.0, green_px

            top_row = int(green_rows[0])
            bottom_row = int(green_rows[-1])

            # Convert to fill percentage (bottom = 0%, top = 100%)
            start_pct = (1.0 - bottom_row / float(roi_h)) * 100.0
            end_pct = (1.0 - top_row / float(roi_h)) * 100.0
        else:
            # Horizontal meter
            col_counts = np.count_nonzero(mask, axis=0)
            threshold = max(1, roi_h * 0.15)
            green_cols = np.where(col_counts >= threshold)[0]

            if len(green_cols) == 0:
                return -1.0, -1.0, -1.0, 0.0, green_px

            left_col = int(green_cols[0])
            right_col = int(green_cols[-1])

            start_pct = (left_col / float(roi_w)) * 100.0
            end_pct = (right_col / float(roi_w)) * 100.0

        center_pct = (start_pct + end_pct) / 2.0
        width_pct = end_pct - start_pct

        return (
            round(start_pct, 2),
            round(end_pct, 2),
            round(center_pct, 2),
            round(width_pct, 2),
            green_px,
        )


# ---------------------------------------------------------------------------
#  Ghost Engine (Meter Occlusion Handler)
# ---------------------------------------------------------------------------

class GhostEngine:
    """Handles brief meter occlusion (e.g., player model overlapping meter).

    Extrapolates fill percentage using velocity from last known good frames
    for up to N frames of occlusion. Prevents false abort during shots.
    """

    def __init__(self, config: RemotePlayCVConfig) -> None:
        self._config = config
        self._last_good_result: Optional[DetectResult] = None
        self._last_good_ts: float = 0.0
        self._last_velocity: float = 0.0
        self._occlusion_count: int = 0

    def on_detection(self, result: DetectResult, velocity_pct_s: float, ts: float) -> DetectResult:
        """Process a detection result, possibly extrapolating if occluded."""
        if not self._config.ghost_enabled:
            return result

        if result.detected and result.confidence > 0.2:
            # Good frame - update ghost state
            self._last_good_result = result
            self._last_good_ts = ts
            self._last_velocity = velocity_pct_s
            self._occlusion_count = 0
            return result

        # Meter not detected - check if we should extrapolate
        if self._last_good_result is None:
            return result

        self._occlusion_count += 1
        if self._occlusion_count > self._config.ghost_occlusion_frames:
            return result  # Too many frames occluded, give up

        dt_ms = (ts - self._last_good_ts) * 1000.0
        if dt_ms > self._config.ghost_max_extrapolate_ms:
            return result  # Too long since last good frame

        # Extrapolate fill percentage
        delta_pct = self._last_velocity * (dt_ms / 1000.0)
        extrapolated_fill = min(100.0, self._last_good_result.fill_pct + delta_pct)

        # Create ghost result
        ghost = DetectResult(
            detected=True,
            style=self._last_good_result.style,
            color_name=self._last_good_result.color_name,
            bbox=self._last_good_result.bbox,
            fill_pct=extrapolated_fill,
            confidence=self._last_good_result.confidence * 0.8,  # Reduced confidence
            consecutive_frames=self._last_good_result.consecutive_frames,
            velocity=self._last_velocity,
            smoothed_fill_pct=extrapolated_fill,
            green_window_start_pct=self._last_good_result.green_window_start_pct,
            green_window_end_pct=self._last_good_result.green_window_end_pct,
            green_window_center_pct=self._last_good_result.green_window_center_pct,
            green_window_width_pct=self._last_good_result.green_window_width_pct,
        )
        return ghost

    def reset(self) -> None:
        self._last_good_result = None
        self._last_good_ts = 0.0
        self._last_velocity = 0.0
        self._occlusion_count = 0


# ---------------------------------------------------------------------------
#  Adaptive ROI Tracker
# ---------------------------------------------------------------------------

class AdaptiveROITracker:
    """Tracks meter position and locks a tight ROI after stable detection.

    Reduces per-frame CV work from full-frame search to small ROI scan.
    Falls back to full-frame on ROI miss.
    """

    def __init__(self, config: RemotePlayCVConfig) -> None:
        self._config = config
        self._roi: Optional[Tuple[int, int, int, int]] = None  # x, y, w, h
        self._roi_locked = False
        self._stable_count = 0
        self._miss_count = 0
        self._max_miss = 5

    @property
    def locked(self) -> bool:
        return self._roi_locked

    @property
    def roi(self) -> Optional[Tuple[int, int, int, int]]:
        return self._roi

    def update(self, detected: bool, bbox: Tuple[int, int, int, int]) -> Optional[Tuple[int, int, int, int]]:
        """Update ROI tracker with latest detection.

        Returns the ROI to use for next frame (None = full frame).
        """
        if not self._config.roi_enabled:
            return None

        if detected and bbox[2] > 0 and bbox[3] > 0:
            self._miss_count = 0
            self._stable_count += 1

            # Update ROI with padding
            pad = self._config.roi_padding_px
            size = self._config.roi_size_px
            cx = bbox[0] + bbox[2] // 2
            cy = bbox[1] + bbox[3] // 2
            half = max(size, max(bbox[2], bbox[3])) // 2 + pad

            self._roi = (
                max(0, cx - half),
                max(0, cy - half),
                half * 2,
                half * 2,
            )

            if self._stable_count >= self._config.roi_lock_frames:
                self._roi_locked = True
        else:
            self._miss_count += 1
            if self._miss_count > self._max_miss:
                self._roi_locked = False
                self._roi = None
                self._stable_count = 0

        return self._roi if self._roi_locked else None

    def reset(self) -> None:
        self._roi = None
        self._roi_locked = False
        self._stable_count = 0
        self._miss_count = 0


# ---------------------------------------------------------------------------
#  Frame Statistics
# ---------------------------------------------------------------------------

@dataclass
class CVFrameStats:
    """Per-frame CV processing stats."""
    frame_number: int = 0
    process_time_ms: float = 0.0
    meter_detected: bool = False
    fill_pct: float = 0.0
    confidence: float = 0.0
    velocity_pct_s: float = 0.0
    green_start_pct: float = -1.0
    green_end_pct: float = -1.0
    green_center_pct: float = -1.0
    green_width_pct: float = 0.0
    eta_to_green_ms: float = -1.0
    roi_locked: bool = False
    ghost_active: bool = False
    skipped: bool = False


# ---------------------------------------------------------------------------
#  Remote Play CV Pipeline
# ---------------------------------------------------------------------------

class RemotePlayCV:
    """Main CV pipeline for Remote Play frame processing.

    Usage:
        cv_pipe = RemotePlayCV(config, remap_engine)
        cv_pipe.start()
        ...
        # In frame loop:
        stats = cv_pipe.process_frame(frame_data)
        ...
        cv_pipe.stop()
    """

    def __init__(
        self,
        config: Optional[RemotePlayCVConfig] = None,
        remap_engine=None,
        detector_config: Optional[DetectorConfig] = None,
    ) -> None:
        self._config = config or RemotePlayCVConfig()
        self._remap_engine = remap_engine
        self._detector_config = detector_config or load_detector_config()

        # Override detector config from our CV config
        self._detector_config.meter_color = self._config.meter_color
        self._detector_config.cuda_enabled = self._config.cuda_enabled

        # Sub-components
        self._detector = MeterDetector(self._detector_config)
        self._green_analyzer = GreenWindowAnalyzer(self._config)
        self._ghost = GhostEngine(self._config)
        self._roi_tracker = AdaptiveROITracker(self._config)

        # State
        self._frame_count = 0
        self._skip_counter = 0
        self._last_result: Optional[DetectResult] = None
        self._velocity_pct_s: float = 0.0
        self._lock = threading.Lock()

        # Performance tracking
        self._process_times: deque = deque(maxlen=120)
        self._stats = {
            "frames_processed": 0,
            "frames_skipped": 0,
            "avg_process_ms": 0.0,
            "p95_process_ms": 0.0,
            "detections": 0,
            "green_windows_found": 0,
            "ghost_extrapolations": 0,
            "roi_lock_rate": 0.0,
        }
        self._detection_count = 0
        self._roi_lock_count = 0

    def process_frame(self, frame_data: FrameData) -> CVFrameStats:
        """Process a single frame through the CV pipeline.

        Returns per-frame statistics. Also feeds data to the remap engine.
        """
        with self._lock:
            self._frame_count += 1
            stats = CVFrameStats(frame_number=frame_data.frame_number)

            # Skip frames if over budget
            if self._skip_counter > 0:
                self._skip_counter -= 1
                stats.skipped = True
                self._stats["frames_skipped"] += 1
                return stats

            t0 = time.perf_counter()

            # Get frame
            frame = frame_data.frame
            if frame is None or frame.size == 0:
                return stats

            # Apply ROI if locked
            roi = self._roi_tracker.roi
            process_frame = frame
            roi_offset_x, roi_offset_y = 0, 0
            if roi and self._roi_tracker.locked:
                rx, ry, rw, rh = roi
                H, W = frame.shape[:2]
                rx = max(0, min(W - 1, rx))
                ry = max(0, min(H - 1, ry))
                rw = min(rw, W - rx)
                rh = min(rh, H - ry)
                if rw > 20 and rh > 20:
                    process_frame = frame[ry:ry + rh, rx:rx + rw]
                    roi_offset_x, roi_offset_y = rx, ry

            # Run meter detection
            result = self._detector.detect(process_frame)

            # Adjust bbox if ROI was used
            if roi_offset_x > 0 or roi_offset_y > 0:
                bx, by, bw, bh = result.bbox
                result = DetectResult(
                    detected=result.detected,
                    style=result.style,
                    color_name=result.color_name,
                    bbox=(bx + roi_offset_x, by + roi_offset_y, bw, bh),
                    fill_pct=result.fill_pct,
                    confidence=result.confidence,
                    consecutive_frames=result.consecutive_frames,
                    raw_fill_pct=result.raw_fill_pct,
                    smoothed_fill_pct=result.smoothed_fill_pct,
                    velocity=result.velocity,
                    fill_velocity_pct_s=result.fill_velocity_pct_s,
                    green_window_start_pct=result.green_window_start_pct,
                    green_window_end_pct=result.green_window_end_pct,
                    green_window_center_pct=result.green_window_center_pct,
                    green_window_width_pct=result.green_window_width_pct,
                    green_window_confidence=result.green_window_confidence,
                )

            # Ghost engine: handle occlusion
            ts = time.perf_counter()
            velocity = result.fill_velocity_pct_s if result.detected else self._velocity_pct_s
            result = self._ghost.on_detection(result, velocity, ts)
            ghost_active = not result.detected or (result.confidence < 0.3 and self._last_result is not None)

            # Update ROI tracker
            self._roi_tracker.update(result.detected, result.bbox)

            # Green window analysis
            green_start, green_end, green_center, green_width, green_px = -1.0, -1.0, -1.0, 0.0, 0
            if result.detected and result.bbox[2] > 0 and result.bbox[3] > 0:
                green_start, green_end, green_center, green_width, green_px = \
                    self._green_analyzer.analyze(frame, result.bbox)

                # Use detector's built-in green window if our analyzer found nothing
                if green_start < 0 and result.green_window_start_pct >= 0:
                    green_start = result.green_window_start_pct
                    green_end = result.green_window_end_pct
                    green_center = result.green_window_center_pct
                    green_width = result.green_window_width_pct

            # ETA to green window
            eta_green = -1.0
            if green_center > 0 and result.fill_pct < green_center:
                vel = result.fill_velocity_pct_s
                if vel > 0.5:
                    remaining = green_center - result.fill_pct
                    eta_green = (remaining / vel) * 1000.0  # ms

            # Update velocity tracking
            if result.detected:
                self._velocity_pct_s = result.fill_velocity_pct_s
                self._detection_count += 1
            if self._roi_tracker.locked:
                self._roi_lock_count += 1

            self._last_result = result

            # Feed remap engine
            if self._remap_engine is not None:
                try:
                    self._remap_engine.update_cv_data(
                        fill_pct=result.fill_pct,
                        confidence=result.confidence,
                        velocity_pct_s=result.fill_velocity_pct_s,
                        green_start_pct=green_start,
                        green_end_pct=green_end,
                        green_center_pct=green_center,
                        eta_to_green_ms=eta_green,
                        consecutive_valid_frames=result.consecutive_frames,
                        meter_detected=result.detected,
                    )
                except Exception:
                    pass

            # Timing
            process_ms = (time.perf_counter() - t0) * 1000.0
            self._process_times.append(process_ms)

            # Check if over budget → skip next frame(s)
            if process_ms > self._config.max_process_ms:
                self._skip_counter = self._config.skip_frames_on_overrun

            # Update stats
            self._stats["frames_processed"] += 1
            if result.detected:
                self._stats["detections"] += 1
            if green_start >= 0:
                self._stats["green_windows_found"] += 1
            if ghost_active:
                self._stats["ghost_extrapolations"] += 1

            if len(self._process_times) >= 10:
                times = sorted(self._process_times)
                self._stats["avg_process_ms"] = round(sum(times) / len(times), 2)
                p95_idx = int(len(times) * 0.95)
                self._stats["p95_process_ms"] = round(times[min(p95_idx, len(times) - 1)], 2)
            if self._frame_count > 0:
                self._stats["roi_lock_rate"] = round(self._roi_lock_count / self._frame_count, 3)

            # Build stats
            stats.process_time_ms = round(process_ms, 2)
            stats.meter_detected = result.detected
            stats.fill_pct = result.fill_pct
            stats.confidence = result.confidence
            stats.velocity_pct_s = result.fill_velocity_pct_s
            stats.green_start_pct = green_start
            stats.green_end_pct = green_end
            stats.green_center_pct = green_center
            stats.green_width_pct = green_width
            stats.eta_to_green_ms = eta_green
            stats.roi_locked = self._roi_tracker.locked
            stats.ghost_active = ghost_active

            return stats

    def get_last_result(self) -> Optional[DetectResult]:
        with self._lock:
            return self._last_result

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def reset(self) -> None:
        """Reset all tracking state (e.g., between possessions)."""
        with self._lock:
            self._detector.reset()
            self._ghost.reset()
            self._roi_tracker.reset()
            self._last_result = None
            self._velocity_pct_s = 0.0
            self._skip_counter = 0

    def reload_config(self, config: RemotePlayCVConfig) -> None:
        with self._lock:
            self._config = config
            self._green_analyzer = GreenWindowAnalyzer(config)
            self._ghost = GhostEngine(config)
            self._roi_tracker = AdaptiveROITracker(config)
