"""Pose-based shot timing detector for no-meter mode.

Wraps StaminaTracker + YOLO pose model to extract the locked player's wrist trajectory
and detect PUSH (feedforward anchor) and RELEASE (reactive anchor) landmarks in real time.

Validated parameters from tools/diagnostics/pose_phase_analyzer.py:
- PUSH: strongest upward wrist velocity (< -0.06) in 80-160ms before release
- RELEASE: 7-frame local minimum, depth >= 0.10 below rest, post-release deceleration
- Push-to-release window: 80-160ms
- Near-rest pre-check: wrist within 0.04 of rest in 200-400ms before push
- Min 1.5s between accepted shots
- EMA smoothing: tau=50ms

COCO keypoints: 9/10=wrists, 11/12=hips (Y: 0=top, 1=bottom, up=negative)
"""
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Callable

import numpy as np
import cv2

logger = logging.getLogger(__name__)

KP_WRIST_L, KP_WRIST_R = 9, 10
KP_HIP_L, KP_HIP_R = 11, 12
KP_KNEE_L, KP_KNEE_R = 13, 14
KP_CONF_FLOOR = 0.30

# Multi-keypoint phase detection parameters
PUSH_VEL_PERCENTILE = 5          # adaptive: push fires at this percentile of running wrist velocity
RELEASE_DEPTH_PERCENTILE = 10    # adaptive: release fires below this percentile of running wrist-Y
PUSH_TO_REL_MIN_MS = 80.0        # minimum push-to-release interval
PUSH_TO_REL_MAX_MS = 400.0       # maximum push-to-release interval (widened for noisy data)
PUSH_TIMEOUT_MS = 500.0          # push auto-resets if no release found within this time
NEAR_REST_THRESHOLD = 0.06       # hip within this of rest in pre-push window (was wrist, now hip)
MIN_SHOT_SPACING_S = 1.5         # minimum seconds between accepted shots
POST_RELEASE_DECEL_VEL = 0.02    # wrist velocity must go positive (downward) within 5 frames
EMA_TAU_MS = 30.0                # EMA smoothing time constant (reduced from 50 for faster shot response)
LOCAL_MIN_WINDOW = 3             # half-window for local minimum check (7-frame total)
PRE_PUSH_WINDOW_LO = 0.40        # seconds before push to check near-rest
PRE_PUSH_WINDOW_HI = 0.20        # seconds before push to check near-rest
GATHER_WINDOW_MS = 500.0         # max time before push to look for gather
GATHER_HIP_DROP = 0.015          # hip-Y must rise (crouch) by this much for gather
GATHER_KNEE_DROP = 0.02          # knee-Y must rise (crouch) by this much for gather
GATHER_TO_PUSH_MAX_MS = 600.0    # a push must follow a detected gather within this window (shot-specificity)
VEL_EMA_TAU_MS = 55.0            # heavier EMA for wrist VELOCITY (GLM: 50-60ms) vs 30ms for position
ZEROCROSS_EMA_TAU_MS = 20.0      # GLM-validated: tau=20ms causal EMA for the sub-frame zero-cross release (40-72ms IQR live)
PUSH_VEL_PEAK_THRESHOLD = -0.05  # min (most-negative) smoothed wrist velocity at the peak to fire a push
PUSH_MIN_RISE_PCT = 0.05         # wrist must be >= this above rest at the velocity peak (reject rest jitter)
HIP_APEX_TOLERANCE_FRAMES = 3    # release must be within this many frames of hip-Y local max
ADAPTIVE_HISTORY_FRAMES = 300    # running window for adaptive thresholds (~5s at 60fps)
BOX_TRACKER_MAX_AGE = 60         # max frames to coast without detection (1s at 60fps)
BOX_STALE_POSITION_AGE = 180     # use last known position up to this age (3s)
BOX_EMA_ALPHA = 0.6              # EMA smoothing for box center/size

# --- Appearance (jersey) lock: veto a fallback switch onto a different-coloured player ---
# The locked player's HSV torso histogram is seeded from a stamina-bar-confirmed lock; the
# full-frame fallback paths then refuse to grab a candidate whose jersey clearly differs (a
# contesting defender), coasting instead of switching. Lenient on purpose: it must keep the
# real shooter through arm-up frames, so it only vetoes a CLEAR mismatch. Same-team teammates
# share a jersey, so spatial/velocity continuity (the box tracker) handles those; appearance
# only adds the cross-team veto.
APPEARANCE_MATCH_MIN = 0.35      # min torso-histogram correlation to accept a fallback candidate
APPEARANCE_SIG_ALPHA = 0.25      # EMA blend rate when refreshing the signature from a good lock


class _BoxTracker:
    """Lightweight temporal box tracker with EMA smoothing and constant-velocity coasting."""

    def __init__(self, max_age: int = BOX_TRACKER_MAX_AGE, alpha: float = BOX_EMA_ALPHA):
        self.max_age = max_age
        self.alpha = alpha
        self.cx = None
        self.cy = None
        self.w = None
        self.h = None
        self.vx = 0.0
        self.vy = 0.0
        self.age = 10 ** 9

    def update(self, box_xyxy):
        """Feed a new detection. box_xyxy = [x1, y1, x2, y2]."""
        nx = (box_xyxy[0] + box_xyxy[2]) / 2.0
        ny = (box_xyxy[1] + box_xyxy[3]) / 2.0
        nw = box_xyxy[2] - box_xyxy[0]
        nh = box_xyxy[3] - box_xyxy[1]
        if self.cx is None:
            self.cx, self.cy, self.w, self.h = nx, ny, nw, nh
        else:
            self.vx = (nx - self.cx) * self.alpha + self.vx * (1 - self.alpha)
            self.vy = (ny - self.cy) * self.alpha + self.vy * (1 - self.alpha)
            self.cx = nx * self.alpha + self.cx * (1 - self.alpha)
            self.cy = ny * self.alpha + self.cy * (1 - self.alpha)
            self.w = nw * self.alpha + self.w * (1 - self.alpha)
            self.h = nh * self.alpha + self.h * (1 - self.alpha)
        self.age = 0

    def predict(self):
        """Return predicted box [x1,y1,x2,y2] or None if no track."""
        if self.cx is None:
            return None
        pcx = self.cx + self.vx * self.age
        pcy = self.cy + self.vy * self.age
        return [pcx - self.w / 2, pcy - self.h / 2, pcx + self.w / 2, pcy + self.h / 2]

    def predict_box_raw(self):
        """Return predicted center+size for crop computation."""
        if self.cx is None:
            return None
        pcx = self.cx + self.vx * self.age
        pcy = self.cy + self.vy * self.age
        return (pcx, pcy, self.w, self.h)

    def distance_to(self, box_xyxy):
        """Distance from predicted center to a detection box center."""
        pred = self.predict_box_raw()
        if pred is None:
            return 1e18
        pcx, pcy, _, _ = pred
        bcx = (box_xyxy[0] + box_xyxy[2]) / 2.0
        bcy = (box_xyxy[1] + box_xyxy[3]) / 2.0
        return (pcx - bcx) ** 2 + (pcy - bcy) ** 2

    def miss(self):
        self.age += 1

    def is_valid(self):
        return self.cx is not None and self.age < self.max_age

    def has_last_position(self):
        """True if we have a last known position within BOX_STALE_POSITION_AGE."""
        return self.cx is not None and self.age < BOX_STALE_POSITION_AGE

    def last_position(self):
        """Return last known center (no velocity prediction) for stale fallback."""
        if self.cx is None:
            return None
        return (self.cx, self.cy, self.w, self.h)

    def reset(self):
        self.cx = self.cy = self.w = self.h = None
        self.vx = self.vy = 0.0
        self.age = 10 ** 9


@dataclass
class PoseLandmark:
    kind: str          # "push" or "release"
    frame_seq: int     # sidecar frame serial for staleness rejection
    confidence: float  # 0-1
    timestamp: float   # perf_counter seconds (diagnostic only, not used for scheduling)
    subframe_seq: float = -1.0  # sub-frame-interpolated frame serial (zero-cross release); -1 = use frame_seq
    arm_token: int = 0  # physical-shot identity; zero is invalid/unarmed


class PoseTimingDetector:
    """Real-time pose-based shot timing detector.

    Uses StaminaTracker to lock the user's player, extracts the shooting wrist Y trajectory,
    and detects PUSH (feedforward) and RELEASE (reactive) landmarks.

    The detector runs frame-by-frame. PUSH is emitted as soon as detected (feedforward).
    RELEASE is emitted after validation (reactive — will be late due to pipeline latency).
    """

    def __init__(self, pose_model_path: str = "models/orion_pose2k_n_v2.pt",
                 bar_model_path: str = "models/orion_bar_park.pt",
                 player_model_path: str = "models/orion_player_detect_v9.pt",
                 pose_conf: float = 0.10,
                 handedness: str = "Right",
                 on_landmark: Optional[Callable[[PoseLandmark], None]] = None):
        self.pose_conf = pose_conf
        self.on_landmark = on_landmark
        # Shooting wrist: right-handed shooters release off the RIGHT wrist (kp 10), lefties the
        # LEFT (kp 9). We prefer that wrist's trajectory; fall back to the other only if occluded.
        self._shooting_wrist = KP_WRIST_L if str(handedness).strip().lower().startswith("l") else KP_WRIST_R

        # Load pose model
        from ultralytics import YOLO
        if os.path.exists(pose_model_path):
            self.pose = YOLO(pose_model_path)
        else:
            logger.warning("Pose model %s not found, falling back to yolov8n-pose.pt", pose_model_path)
            self.pose = YOLO("yolov8n-pose.pt")

        # Load optional v9 player detection model (Player + Stamina + Basketball)
        self.player_det = None
        if player_model_path and os.path.exists(player_model_path):
            try:
                self.player_det = YOLO(player_model_path)
                logger.info("Player detection model loaded: %s", player_model_path)
            except Exception as e:
                logger.warning("Failed to load player detection model: %s", e)

        # Create stamina tracker (imports from production stamina_bar module)
        from stamina_bar import StaminaTracker, _compute_appearance_hist
        self.tracker = StaminaTracker(self.pose)
        self._appearance_hist_fn = _compute_appearance_hist

        # Box tracker for temporal player lock continuity
        self._box_tracker = _BoxTracker()

        # Locked player's jersey signature (HSV torso histogram), seeded from a stamina-bar-
        # confirmed lock. Used to veto a full-frame fallback switch onto a different-jersey
        # player. None until first confident seed -> while None, lock behaviour is unchanged.
        self._appearance_sig = None
        self._v9_continuity = False   # True when the last v9 pick followed the existing track
        # Appearance veto: proven inert on both validation clips (redundant with the temporal-jump
        # guard; brittle mid-animation) -> default OFF. Set ORION_POSE_APPEARANCE_VETO=1 to seed +
        # gate by jersey (kept for a future heavy cross-team-contest clip).
        self._appearance_veto_enabled = os.environ.get("ORION_POSE_APPEARANCE_VETO", "0") != "0"

        # User-player classifier: a small CNN trained on meter-confirmed user crops vs other players.
        # Used as a SOFT signal during bootstrap (boost candidates that look like the user player).
        # Cross-clip generalization is imperfect (F1=0.50 avg) — used as a tiebreaker, not a hard gate.
        self._user_classifier = None
        self._user_classifier_crop_size = (96, 96)
        classifier_path = os.path.join(os.path.dirname(__file__), "models", "user_player_classifier.pt")
        if os.path.exists(classifier_path):
            try:
                import torch
                import torch.nn as nn
                class _UserPlayerCNN(nn.Module):
                    def __init__(self):
                        super().__init__()
                        self.features = nn.Sequential(
                            nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
                            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
                            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2),
                            nn.Conv2d(128, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(), nn.AdaptiveAvgPool2d(1),
                        )
                        self.classifier = nn.Sequential(
                            nn.Flatten(), nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.3), nn.Linear(64, 1),
                        )
                    def forward(self, x):
                        return self.classifier(self.features(x)).squeeze(-1)
                ckpt = torch.load(classifier_path, map_location="cpu", weights_only=False)
                self._user_classifier = _UserPlayerCNN()
                self._user_classifier.load_state_dict(ckpt["model_state"])
                self._user_classifier.eval()
                self._user_classifier_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                self._user_classifier.to(self._user_classifier_device)
                if "crop_size" in ckpt:
                    self._user_classifier_crop_size = ckpt["crop_size"]
                logger.info("User-player classifier loaded: %s", classifier_path)
            except Exception as e:
                logger.warning("Failed to load user-player classifier: %s", e)

        # Trajectory buffers (ring buffer)
        self._max_buf = 300  # 5s at 60fps
        self._wrist_y = np.full(self._max_buf, np.nan)
        self._hip_y = np.full(self._max_buf, np.nan)
        self._knee_y = np.full(self._max_buf, np.nan)
        self._t = np.zeros(self._max_buf)
        self._frame_seqs = np.full(self._max_buf, -1, dtype=int)
        self._buf_idx = 0
        self._buf_count = 0

        # Detection state
        self._last_shot_time = -1e9
        self._push_emitted = False
        self._push_idx = -1
        self._push_time = 0.0
        self._push_cooldown_until = -1e9  # time before which push cannot fire (set after timeout)
        self._rest_wrist = np.nan
        self._rest_hip = np.nan
        self._gather_time = -1e9  # timestamp of last detected gather
        # Require a gather (hip/knee crouch) shortly before a push -> shot-specific push.
        # Default OFF (committed baseline) until the time-base fix lets it be tuned without
        # dropping real shots; set ORION_POSE_REQUIRE_GATHER=1 to A/B it. Matched eval A/B
        # 2026-06-24: cut raw pushes 75->55 but cost a real shot (5/6->4/6).
        self._require_gather = os.environ.get("ORION_POSE_REQUIRE_GATHER", "0") != "0"
        # Velocity-PEAK push (vs height-threshold-crossing): anchor the push at the instant of MAX
        # upward wrist velocity -- a consistent biomechanical moment, so far less STD than a crossing
        # whose point varies with camera angle / shot type. Default OFF; ORION_POSE_VEL_PEAK_PUSH=1
        # to A/B on the now-deterministic eval. Also switches to heavier velocity smoothing.
        self._vel_peak_push = os.environ.get("ORION_POSE_VEL_PEAK_PUSH", "0") != "0"
        # Apex release: anchor RELEASE at the wrist-Y velocity zero-crossing (rising->falling -- the
        # ball leaving the hand) instead of the deepest-Y point, which a single noise spike can
        # hijack. A derivative zero-crossing is far more temporally consistent -> tighter release-STD.
        self._apex_release = os.environ.get("ORION_POSE_APEX_RELEASE", "0") != "0"
        # GLM-validated sub-frame velocity ZERO-CROSSING release (40-72ms IQR live, passes the 80ms gate):
        # causal EMA tau=20ms -> backward-diff velocity -> first neg->pos crossing -> LINEAR sub-frame
        # interpolation between the two bracketing frames. The sub-frame interp is the precision win the
        # integer-frame apex_release lacked (~200ms -> ~72ms). 1-frame (16ms) detect delay; needs the
        # input hook OR the temporal-model lookahead to cover latency live.
        self._zerocross_release = os.environ.get("ORION_POSE_ZEROCROSS", "0") != "0"
        self._dt_ms = 16.67  # default 60fps; updated from frame timing

        # Controller-state arming: when the SQUARE button rising edge is detected
        # by the remap engine, it calls notify_shot_start(). This arms the
        # zero-crossing search directly — 100% arm rate, 60ms IQR (validated).
        # Replaces the legacy PUSH detector (44% arm rate, +578ms offset).
        self._controller_arm_frame = -1
        self._controller_arm_time = -1e9
        self._controller_armed = False
        self._controller_arm_token = 0

        # Shot-at-arm anchor: on notify_shot_start, snapshot all players' poses,
        # then over the next ~10 frames identify the ONE whose wrist Y rises
        # (jumpshot signature). Re-anchor _box_tracker to that player.
        self._arm_anchor_active = False
        self._arm_anchor_frame = -1
        self._arm_anchor_candidates = {}  # {player_id: [(wrist_y_norm, frame_seq), ...]}
        self._arm_anchor_next_id = 0

        # --- Multi-signal fusion state ---
        # Ball position from v9 Zbasketball class (class 3)
        self._ball_pos = None  # (cx, cy, frame_seq)
        self._ball_age = 999
        # Stamina bar position from v9 (class 1) — strong user-player anchor
        self._stamina_pos = None  # (cx, cy)
        self._stamina_age = 999

        # Online appearance: always-on HSV torso histogram with Bhattacharyya matching
        # Updated every frame when lock confidence is high; used as a continuous score
        self._online_sig = None  # float32 HSV histogram
        self._online_sig_alpha = 0.05  # slow EMA blend (5% per frame)
        self._online_sig_frames = 0  # how many frames contributed

        # Failure detection: track pose confidence + size for anomaly detection
        self._last_pose_conf = 0.0
        self._last_box_area = 0.0
        self._lock_failures = 0  # consecutive failure frames
        self._lock_fail_threshold = 5  # consecutive failures before re-acquire
        self._wrist_continuity_buf = []  # recent wrist-Y values for discontinuity check

        # FPS detection
        self._last_frame_time = 0.0

        # External player hint (e.g. from meter detector bbox — meter appears at user's shooter)
        self._player_hint = None  # (cx, cy, w, h, timestamp)
        self._player_hint_timeout = 2.0  # hint is valid for 2 seconds

        # ROI crop mode: when tracker is locked, run pose on a small crop around
        # the locked player instead of the full frame. Skips v9 detection on most
        # frames → ~10-30× less per-frame work → ≥30fps in crowded parks.
        # v9 re-validation runs every _v9_interval frames to update ball/stamina
        # positions and re-confirm the lock.
        self.last_overlay = None  # {"kpts": [[x,y,conf]×17], "box": [x1,y1,x2,y2]} full-frame coords
        self._kpts_ema = None     # overlay-only smoothed keypoint positions (17×2); cosmetic, not timing
        self._v9_skip_counter = 0
        self._v9_interval = int(os.environ.get("ORION_POSE_V9_INTERVAL", "15"))
        # Camera-anchor lock: make the under-player marker + camera-center-bottom prior dominate
        # (vs flaky ball/hint/highest-wrist). Default OFF for clean A/B; ON in the launcher + eval.
        self._camera_anchor = os.environ.get("ORION_POSE_CAMERA_ANCHOR", "0") != "0"

    def _append(self, wrist_y, hip_y, knee_y, frame_seq, t):
        i = self._buf_idx
        self._wrist_y[i] = wrist_y
        self._hip_y[i] = hip_y
        self._knee_y[i] = knee_y
        self._t[i] = t
        self._frame_seqs[i] = frame_seq
        self._buf_idx = (i + 1) % self._max_buf
        self._buf_count = min(self._buf_count + 1, self._max_buf)

    def _compute_crop_region(self, box, H, W):
        """Compute crop region with fixed padding matching the original pre-crop values.

        60px on sides, 40px on top/bottom. These values are matched to the
        original inline padding that gave 70% overall accuracy.
        """
        x1, y1, x2, y2 = box[:4]
        px1 = max(0, int(x1) - 60)
        py1 = max(0, int(y1) - 40)
        px2 = min(W, int(x2) + 60)
        py2 = min(H, int(y2) + 40)
        return px1, py1, px2, py2

    def _get_ordered(self):
        """Return trajectory arrays in chronological order."""
        if self._buf_count < self._max_buf:
            return (self._wrist_y[:self._buf_count],
                    self._hip_y[:self._buf_count],
                    self._knee_y[:self._buf_count],
                    self._t[:self._buf_count],
                    self._frame_seqs[:self._buf_count])
        n = self._max_buf
        idx = np.roll(np.arange(n), -(self._buf_idx))
        return (self._wrist_y[idx], self._hip_y[idx], self._knee_y[idx],
                self._t[idx], self._frame_seqs[idx])

    @staticmethod
    def _ema_tau(arr, tau_ms, dt_ms):
        a = 1.0 - np.exp(-dt_ms / max(tau_ms, 1.0))
        out = np.copy(arr)
        for i in range(1, len(out)):
            if not np.isnan(out[i]) and not np.isnan(out[i - 1]):
                out[i] = out[i - 1] * (1 - a) + out[i] * a
        for i in range(len(out) - 2, -1, -1):
            if not np.isnan(out[i]) and not np.isnan(out[i + 1]):
                out[i] = out[i + 1] * (1 - a) + out[i] * a
        return out

    @staticmethod
    def _fill_nans(arr):
        out = np.copy(arr); n = len(out); i = 0
        while i < n:
            if np.isnan(out[i]):
                j = i
                while j < n and np.isnan(out[j]): j += 1
                if i > 0 and j < n:
                    lo, hi = out[i - 1], out[j]
                    for k in range(i, j): out[k] = lo + (hi - lo) * (k - i + 1) / (j - i + 1)
                elif i == 0 and j < n: out[:j] = out[j]
                elif j >= n and i > 0: out[i:] = out[i - 1]
                i = j
            else: i += 1
        return out

    def _extract_wrist_y(self, kpts, H):
        """Get the shooting wrist Y (normalized 0-1). Prefers the handedness-selected wrist;
        falls back to the other wrist only when the shooting wrist is occluded/low-confidence."""
        if kpts is None:
            return float("nan")
        lw = kpts[KP_WRIST_L]  # (x, y, conf)
        rw = kpts[KP_WRIST_R]
        lc, rc = lw[2], rw[2]
        if lc < KP_CONF_FLOOR and rc < KP_CONF_FLOOR:
            return float("nan")
        shoot = lw if self._shooting_wrist == KP_WRIST_L else rw
        other = rw if self._shooting_wrist == KP_WRIST_L else lw
        # Prefer the shooting wrist; the both-below-floor case already returned NaN above, so if
        # the shooting wrist is below the floor the other wrist is necessarily usable.
        if shoot[2] >= KP_CONF_FLOOR:
            return float(shoot[1]) / H
        return float(other[1]) / H

    def _extract_hip_y(self, kpts, H):
        """Get hip Y (normalized 0-1) — average of both hips if both visible."""
        if kpts is None:
            return float("nan")
        lh, rh = kpts[KP_HIP_L], kpts[KP_HIP_R]
        lc, rc = lh[2], rh[2]
        if lc < KP_CONF_FLOOR and rc < KP_CONF_FLOOR:
            return float("nan")
        vals = []
        if lc >= KP_CONF_FLOOR: vals.append(lh[1] / H)
        if rc >= KP_CONF_FLOOR: vals.append(rh[1] / H)
        return float(np.mean(vals)) if vals else float("nan")

    def _extract_knee_y(self, kpts, H):
        """Get knee Y (normalized 0-1) — average of both knees if both visible."""
        if kpts is None:
            return float("nan")
        lk, rk = kpts[KP_KNEE_L], kpts[KP_KNEE_R]
        lc, rc = lk[2], rk[2]
        if lc < KP_CONF_FLOOR and rc < KP_CONF_FLOOR:
            return float("nan")
        vals = []
        if lc >= KP_CONF_FLOOR: vals.append(lk[1] / H)
        if rc >= KP_CONF_FLOOR: vals.append(rk[1] / H)
        return float(np.mean(vals)) if vals else float("nan")

    def set_player_hint(self, bbox_xyxy):
        """Provide an external hint for the user's player position (e.g. from meter detector).
        bbox_xyxy = [x1, y1, x2, y2] in frame pixel coordinates. Used to bootstrap/re-acquire
        player lock when v9 and stamina bar detection fail."""
        if bbox_xyxy is not None and len(bbox_xyxy) >= 4:
            cx = (bbox_xyxy[0] + bbox_xyxy[2]) / 2.0
            cy = (bbox_xyxy[1] + bbox_xyxy[3]) / 2.0
            w = bbox_xyxy[2] - bbox_xyxy[0]
            h = bbox_xyxy[3] - bbox_xyxy[1]
            self._player_hint = (cx, cy, w, h, time.perf_counter())

    def _score_user_candidate(self, frame, box_xyxy):
        """Run the user-player classifier on a candidate player crop. Returns 0..1 score."""
        if self._user_classifier is None:
            return 0.0
        try:
            import torch
            x1, y1, x2, y2 = map(int, box_xyxy[:4])
            H, W = frame.shape[:2]
            x1 = max(0, x1); y1 = max(0, y1); x2 = min(W, x2); y2 = min(H, y2)
            if x2 - x1 < 10 or y2 - y1 < 10:
                return 0.0
            crop = frame[y1:y2, x1:x2]
            crop = cv2.resize(crop, (self._user_classifier_crop_size[1], self._user_classifier_crop_size[0]))
            crop = crop[:, :, ::-1].copy()  # BGR -> RGB
            tensor = torch.from_numpy(crop).float().permute(2, 0, 1).unsqueeze(0) / 255.0
            tensor = tensor.to(self._user_classifier_device)
            with torch.no_grad():
                logit = self._user_classifier(tensor)
                return float(torch.sigmoid(logit).item())
        except Exception:
            return 0.0

    def _hint_valid(self):
        """Check if player hint is still valid (within timeout)."""
        if self._player_hint is None:
            return False
        age = time.perf_counter() - self._player_hint[4]
        return age < self._player_hint_timeout

    def _appearance_sim(self, frame, kpts):
        """Correlation of a candidate's torso (jersey) histogram with the locked signature.
        Returns None when there is no signature yet or the candidate's torso can't be measured
        (low-confidence shoulders) -- callers treat None as 'no appearance opinion -> allow', so
        a missing measurement never drops the real player; only a measured mismatch vetoes."""
        if self._appearance_sig is None:
            return None
        h = self._appearance_hist_fn(frame, None, kpts)
        if h is None:
            return None
        return float(cv2.compareHist(self._appearance_sig, h, cv2.HISTCMP_CORREL))

    def _refresh_signature(self, frame, kpts):
        """EMA-update the locked-player jersey signature from a confident (stamina-confirmed)
        lock. Slow blend so one off frame can't poison the lock; float32 for cv2.compareHist."""
        h = self._appearance_hist_fn(frame, None, kpts)
        if h is None:
            return
        if self._appearance_sig is None:
            self._appearance_sig = h.astype(np.float32)
        else:
            blended = (1.0 - APPEARANCE_SIG_ALPHA) * self._appearance_sig + APPEARANCE_SIG_ALPHA * h
            self._appearance_sig = blended.astype(np.float32)

    def _detect_player_v9(self, frame):
        """Use v9 detection model to find my_player box with multi-signal fusion.

        Scoring combines:
        - Tracker proximity (temporal continuity)
        - Ball proximity (ball-centric anchor — shooter is near the ball)
        - Appearance match (online HSV torso histogram)
        - Size plausibility (area ratio vs tracker)
        - Wrist height (higher hand = more likely shooting)

        Also detects lock failure (sudden size jump, confidence drop) and triggers
        fast re-acquisition.

        Returns (box_xyxy, stamina_box) or None. Updates self._box_tracker."""
        if self.player_det is None:
            return None
        self._v9_continuity = False

        r = self.player_det.predict(frame, verbose=False, conf=0.15, imgsz=640)[0]
        cls = r.boxes.cls.cpu().numpy().astype(int) if r.boxes is not None and len(r.boxes) else np.array([], dtype=int)
        xyxy = r.boxes.xyxy.cpu().numpy() if r.boxes is not None and len(r.boxes) else np.array([]).reshape(0, 4)
        confs = r.boxes.conf.cpu().numpy() if r.boxes is not None and len(r.boxes) else np.array([])

        player_boxes = xyxy[cls == 0] if len(cls) > 0 else np.array([]).reshape(0, 4)
        player_confs = confs[cls == 0] if len(cls) > 0 else np.array([])
        stamina_boxes = xyxy[cls == 1] if len(cls) > 0 else np.array([]).reshape(0, 4)
        ball_boxes = xyxy[cls == 3] if len(cls) > 0 else np.array([]).reshape(0, 4)
        stamina_box = stamina_boxes[0] if len(stamina_boxes) > 0 else None

        # --- Layer 1: Update ball + stamina positions ---
        if len(ball_boxes) > 0:
            # Pick the most confident ball
            ball_confs = confs[cls == 3]
            best_ball = ball_boxes[int(np.argmax(ball_confs))]
            bcx = (best_ball[0] + best_ball[2]) / 2.0
            bcy = (best_ball[1] + best_ball[3]) / 2.0
            self._ball_pos = (bcx, bcy)
            self._ball_age = 0
        else:
            self._ball_age += 1

        if stamina_box is not None:
            s_cx = (stamina_box[0] + stamina_box[2]) / 2.0
            s_cy = (stamina_box[1] + stamina_box[3]) / 2.0
            self._stamina_pos = (s_cx, s_cy)
            self._stamina_age = 0
        else:
            self._stamina_age += 1

        # --- Layer 4: Failure detection ---
        # Check if current lock has gone wrong
        force_reacquire = False
        if self._box_tracker.is_valid() and self._last_box_area > 0:
            pred = self._box_tracker.predict()
            if pred is not None:
                pred_area = (pred[2] - pred[0]) * (pred[3] - pred[1])
                # Sudden size change > 2x or < 0.4x
                if pred_area > 0 and self._last_box_area > 0:
                    ratio = pred_area / self._last_box_area
                    if ratio > 2.5 or ratio < 0.35:
                        self._lock_failures += 1
                    else:
                        self._lock_failures = max(0, self._lock_failures - 1)
                # Pose confidence drop
                if self._last_pose_conf < 0.15 and self._lock_failures > 0:
                    self._lock_failures += 1
            if self._lock_failures >= self._lock_fail_threshold:
                force_reacquire = True
                self._lock_failures = 0

        # --- Case 1: Tracker valid + detections available — multi-signal fusion ---
        if self._box_tracker.is_valid() and len(player_boxes) > 0 and not force_reacquire:
            best_score, best_b = -1e18, None
            tracker_pred = self._box_tracker.predict()

            for pb in player_boxes:
                score = self._score_candidate(pb, frame, tracker_pred, stamina_box, confs)
                if score > best_score:
                    best_score = score
                    best_b = pb

            if best_b is not None:
                self._box_tracker.update(best_b)
                self._v9_continuity = True
                # Update online appearance signature from confident lock
                self._update_online_sig(frame, best_b)
                return (best_b, stamina_box)

        # --- Case 2: Tracker valid but no detections — coast ---
        if self._box_tracker.is_valid() and len(player_boxes) == 0 and not force_reacquire:
            self._box_tracker.miss()
            pred = self._box_tracker.predict()
            if pred is not None:
                return (pred, stamina_box)

        # --- Case 3: Re-acquire from scratch (tracker stale, first frame, or failure) ---
        if force_reacquire:
            logger.debug("Lock failure detected — forcing re-acquire")

        if len(player_boxes) == 0:
            self._box_tracker.miss()
            return None

        # If stamina detected, use it as a strong anchor
        if stamina_box is not None:
            s_cx = (stamina_box[0] + stamina_box[2]) / 2.0
            s_cy = (stamina_box[1] + stamina_box[3]) / 2.0
            best_d, best_b = 1e18, None
            for pb in player_boxes:
                p_cx = (pb[0] + pb[2]) / 2.0
                p_cy = (pb[1] + pb[3]) / 2.0
                d = (p_cx - s_cx) ** 2 + (p_cy - s_cy) ** 2
                if d < best_d:
                    best_d, best_b = d, pb
            self._box_tracker.update(best_b)
            self._update_online_sig(frame, best_b)
            return (best_b, stamina_box)

        # No stamina — use multi-signal fusion for bootstrap
        best_score, best_b, best_ci = -1e18, None, -1
        for pi, pb in enumerate(player_boxes):
            score = self._score_candidate_bootstrap(pb, frame, confs)
            if score > best_score:
                best_score, best_b, best_ci = score, pb, pi

        # Random-object guard: v9 runs at a low conf=0.25, so transient false "players" (crowd,
        # court props, UI) can appear. For a NEW lock (no tracker continuity vouching for it)
        # require a CONFIDENT detection; otherwise take no lock and wait, rather than snapping onto
        # a random object. Tracking (Case 1) keeps the low floor — temporal + appearance scoring
        # already vouch for the known player there. Env-tunable (ORION_POSE_REACQUIRE_MIN_CONF).
        reacq_min = float(os.environ.get("ORION_POSE_REACQUIRE_MIN_CONF", "0.40"))
        if best_b is not None and 0 <= best_ci < len(player_confs) and float(player_confs[best_ci]) < reacq_min:
            self._box_tracker.miss()
            return None

        if best_b is not None:
            self._box_tracker.update(best_b)
            self._update_online_sig(frame, best_b)
        return (best_b, stamina_box)

    def _camera_pos_prior(self, pcx, pcy, W, H):
        """Camera-anchor positional prior (0..1): in 2K the camera locks onto the user's player,
        so it sits near screen-center-X and lower-center-Y. Anisotropic Gaussian (wider X)."""
        ax, ay = 0.50 * W, 0.62 * H
        sx, sy = 0.20 * W, 0.22 * H
        return float(np.exp(-0.5 * ((pcx - ax) / sx) ** 2 - 0.5 * ((pcy - ay) / sy) ** 2))

    def _indicator_adjacency(self, pb, mcx, mcy):
        """(passes, dist) for the under-player marker vs a candidate box. The marker sits at/under
        the feet, so reject a marker above the head; dist = marker center to nearest box point."""
        x1, y1, x2, y2 = pb[0], pb[1], pb[2], pb[3]
        if mcy < y1 - 0.25 * (y2 - y1):
            return (False, 1e18)
        dx = max(x1 - mcx, 0.0, mcx - x2)
        dy = max(y1 - mcy, 0.0, mcy - y2)
        return (True, float(np.sqrt(dx * dx + dy * dy)))

    def _indicator_score(self, pb, stamina_box=None):
        """0..1 score for how well a candidate sits OVER the under-player marker (the game's user
        indicator). Uses an explicit stamina_box if given, else the recently tracked marker."""
        mc = None
        if stamina_box is not None:
            mc = ((stamina_box[0] + stamina_box[2]) / 2.0, (stamina_box[1] + stamina_box[3]) / 2.0)
        elif self._stamina_pos is not None and self._stamina_age < 8:
            mc = (self._stamina_pos[0], self._stamina_pos[1])
        if mc is None:
            return 0.4  # neutral when no marker this frame
        passes, idist = self._indicator_adjacency(pb, mc[0], mc[1])
        return float(np.exp(-idist / 80.0)) if passes else 0.0

    def _score_candidate(self, pb, frame, tracker_pred, stamina_box, all_confs=None):
        """Multi-signal fusion score for a candidate player box during tracking.

        Weighted combination of:
        - Tracker proximity (0.35): exp decay from predicted position
        - Ball proximity (0.25): exp decay from ball position (if recent)
        - Appearance match (0.15): Bhattacharyya correlation with online signature
        - Size plausibility (0.10): area ratio close to 1.0
        - Stamina proximity (0.10): distance to stamina bar
        - Confidence (0.05): detection confidence
        """
        pcx = (pb[0] + pb[2]) / 2.0
        pcy = (pb[1] + pb[3]) / 2.0
        parea = (pb[2] - pb[0]) * (pb[3] - pb[1])

        scores = {}

        # Tracker proximity
        if tracker_pred is not None:
            tcx = (tracker_pred[0] + tracker_pred[2]) / 2.0
            tcy = (tracker_pred[1] + tracker_pred[3]) / 2.0
            tdist = np.sqrt((pcx - tcx) ** 2 + (pcy - tcy) ** 2)
            scores['tracker'] = np.exp(-tdist / 120.0)
        else:
            scores['tracker'] = 0.5

        # Ball proximity
        if self._ball_age < 10 and self._ball_pos is not None:
            bdist = np.sqrt((pcx - self._ball_pos[0]) ** 2 + (pcy - self._ball_pos[1]) ** 2)
            scores['ball'] = np.exp(-bdist / 100.0)
        else:
            scores['ball'] = 0.3  # neutral when no ball

        # Appearance match (online signature)
        if self._online_sig is not None:
            h = self._compute_torso_hist(frame, pb)
            if h is not None:
                scores['appearance'] = float(cv2.compareHist(self._online_sig, h, cv2.HISTCMP_CORREL))
            else:
                scores['appearance'] = 0.5
        else:
            scores['appearance'] = 0.5  # neutral until signature built

        # Size plausibility
        if self._last_box_area > 0 and parea > 0:
            ratio = parea / self._last_box_area
            scores['size'] = np.exp(-abs(ratio - 1.0) * 3.0)  # peak at ratio=1.0
        else:
            scores['size'] = 0.5

        # Stamina proximity
        if stamina_box is not None:
            s_cx = (stamina_box[0] + stamina_box[2]) / 2.0
            s_cy = (stamina_box[1] + stamina_box[3]) / 2.0
            sdist = np.sqrt((pcx - s_cx) ** 2 + (pcy - s_cy) ** 2)
            scores['stamina'] = np.exp(-sdist / 150.0)
        else:
            scores['stamina'] = 0.3

        # Detection confidence
        scores['conf'] = 0.5  # default; overridden if we have per-box conf

        # Meter hint X proximity: meter appears above the user's player at a similar X
        if self._hint_valid():
            hdist_x = abs(pcx - self._player_hint[0])
            scores['hint'] = np.exp(-hdist_x / 200.0)
        else:
            scores['hint'] = 0.5

        # Camera-center-bottom positional prior + under-player marker adjacency.
        H_frame, W_frame = frame.shape[:2]
        scores['pos'] = self._camera_pos_prior(pcx, pcy, W_frame, H_frame)
        scores['indicator'] = self._indicator_score(pb, stamina_box)

        if self._camera_anchor:
            weights = {'tracker': 0.34, 'pos': 0.22, 'indicator': 0.18, 'size': 0.12,
                       'appearance': 0.08, 'ball': 0.03, 'hint': 0.03}
        else:
            weights = {'tracker': 0.35, 'ball': 0.15, 'appearance': 0.10, 'size': 0.05,
                       'stamina': 0.05, 'conf': 0.05, 'hint': 0.25}
        return sum(weights[k] * max(0.0, min(1.0, scores.get(k, 0.0))) for k in weights)

    def _score_candidate_bootstrap(self, pb, frame, all_confs=None):
        """Multi-signal fusion score for bootstrap (no tracker history).

        Weights: ball proximity (0.40), wrist height (0.25), center proximity (0.15),
        appearance (0.10), confidence (0.10)
        """
        pcx = (pb[0] + pb[2]) / 2.0
        pcy = (pb[1] + pb[3]) / 2.0
        parea = (pb[2] - pb[0]) * (pb[3] - pb[1])
        H_frame, W_frame = frame.shape[:2]

        scores = {}

        # Ball proximity — dominant signal for bootstrap
        if self._ball_age < 10 and self._ball_pos is not None:
            bdist = np.sqrt((pcx - self._ball_pos[0]) ** 2 + (pcy - self._ball_pos[1]) ** 2)
            scores['ball'] = np.exp(-bdist / 100.0)
        else:
            scores['ball'] = 0.2

        # Wrist height — run quick pose on crop to check
        # (deferred: use box position as proxy — higher box = taller player = more likely standing)
        # Use box top Y as proxy: lower top-Y = taller player
        scores['wrist'] = max(0, 0.5 - pb[1] / H_frame) * 2.0  # higher box top = higher score

        # Center proximity (user's player tends to be near screen center)
        center_dist = abs(pcx - W_frame / 2) / W_frame + abs(pcy - H_frame * 0.5) / H_frame
        scores['center'] = 1.0 / (1.0 + center_dist * 3.0)

        # Appearance
        if self._online_sig is not None:
            h = self._compute_torso_hist(frame, pb)
            if h is not None:
                scores['appearance'] = float(cv2.compareHist(self._online_sig, h, cv2.HISTCMP_CORREL))
            else:
                scores['appearance'] = 0.5
        else:
            scores['appearance'] = 0.5

        # Area — prefer larger players (closer to camera)
        scores['area'] = min(1.0, parea / 80000.0)

        # Meter hint X proximity — strongest external signal when available
        if self._hint_valid():
            hdist_x = abs(pcx - self._player_hint[0])
            scores['hint'] = np.exp(-hdist_x / 200.0)
        else:
            scores['hint'] = 0.5

        # Camera-center-bottom prior + under-player marker adjacency for cold-start.
        scores['pos'] = self._camera_pos_prior(pcx, pcy, W_frame, H_frame)
        scores['indicator'] = self._indicator_score(pb, None)

        if self._camera_anchor:
            weights = {'pos': 0.34, 'indicator': 0.24, 'area': 0.20, 'appearance': 0.12,
                       'ball': 0.05, 'hint': 0.05}
        else:
            weights = {'hint': 0.25, 'ball': 0.30, 'wrist': 0.15, 'center': 0.10,
                       'appearance': 0.10, 'area': 0.10}
        return sum(weights[k] * max(0.0, min(1.0, scores.get(k, 0.0))) for k in weights)

    def _compute_torso_hist(self, frame, box_xyxy):
        """Compute HSV torso histogram from a player box.
        Uses the central region of the box as torso proxy (no keypoints needed)."""
        try:
            x1, y1, x2, y2 = map(int, box_xyxy[:4])
            H, W = frame.shape[:2]
            x1 = max(0, x1); y1 = max(0, y1); x2 = min(W, x2); y2 = min(H, y2)
            if x2 - x1 < 20 or y2 - y1 < 30:
                return None
            # Torso region: middle 60% width, 30-70% height
            tx1 = int(x1 + (x2 - x1) * 0.2)
            tx2 = int(x1 + (x2 - x1) * 0.8)
            ty1 = int(y1 + (y2 - y1) * 0.3)
            ty2 = int(y1 + (y2 - y1) * 0.7)
            torso = frame[ty1:ty2, tx1:tx2]
            if torso.size < 100:
                return None
            hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
            return hist.astype(np.float32)
        except Exception:
            return None

    def _update_online_sig(self, frame, box_xyxy):
        """EMA-update the online appearance signature from a confident lock."""
        h = self._compute_torso_hist(frame, box_xyxy)
        if h is None:
            return
        if self._online_sig is None:
            self._online_sig = h
        else:
            alpha = self._online_sig_alpha
            self._online_sig = ((1.0 - alpha) * self._online_sig + alpha * h).astype(np.float32)
        self._online_sig_frames += 1

    def _detect_gather(self, hy_s, ky_s, t, t_now):
        """Check if a gather (crouch) happened in the GATHER_WINDOW_MS before now.
        Returns True if hip-Y and/or knee-Y rose (player crouched) recently."""
        gather_lo = t_now - GATHER_WINDOW_MS / 1000.0
        # Find hip-Y and knee-Y in the gather window
        hip_vals = [(t[j], hy_s[j]) for j in range(len(t))
                     if gather_lo <= t[j] <= t_now and not np.isnan(hy_s[j])]
        knee_vals = [(t[j], ky_s[j]) for j in range(len(t))
                      if gather_lo <= t[j] <= t_now and not np.isnan(ky_s[j])]
        if len(hip_vals) < 4:
            return False
        # Gather = hip-Y was lower (higher position) earlier, then dropped (crouched)
        # In normalized Y: crouch = hip-Y INCREASES (moves down on screen)
        hip_early = np.median([v for _, v in hip_vals[:max(3, len(hip_vals)//3)]])
        hip_late = np.median([v for _, v in hip_vals[-max(3, len(hip_vals)//3):]])
        hip_drop = hip_late - hip_early  # positive = moved down = crouched

        knee_drop = 0.0
        if len(knee_vals) >= 4:
            knee_early = np.median([v for _, v in knee_vals[:max(3, len(knee_vals)//3)]])
            knee_late = np.median([v for _, v in knee_vals[-max(3, len(knee_vals)//3):]])
            knee_drop = knee_late - knee_early

        # Either hip or knee must show a crouch
        if hip_drop >= GATHER_HIP_DROP:
            return True
        if knee_drop >= GATHER_KNEE_DROP:
            return True
        return False

    def _find_hip_apex(self, hy_s, t, t_release, tolerance_frames=HIP_APEX_TOLERANCE_FRAMES):
        """Check if hip-Y has a local minimum (player at jump apex) near t_release.
        Hip-Y minimum = highest point = apex of jump."""
        valid = ~np.isnan(hy_s)
        if valid.sum() < 10:
            return False
        idx = np.where(valid)[0]
        # Find the index closest to t_release
        rel_k = np.argmin(np.abs(t[idx] - t_release))
        rel_k = idx[rel_k]
        # Check if hip-Y is at a local minimum within tolerance
        lo = max(0, rel_k - tolerance_frames)
        hi = min(len(hy_s), rel_k + tolerance_frames + 1)
        window_vals = [hy_s[j] for j in range(lo, hi) if not np.isnan(hy_s[j])]
        if not window_vals:
            return False
        min_val = min(window_vals)
        # The release frame should be near the hip minimum (apex)
        # Check that hip-Y rises (player descends) after the minimum
        after_min = [hy_s[j] for j in range(rel_k, min(len(hy_s), rel_k + 8))
                     if not np.isnan(hy_s[j])]
        if len(after_min) < 2:
            return False
        # At least some values after release should be higher than the minimum
        rises_after = sum(1 for v in after_min[1:] if v > min_val + 0.005)
        return rises_after >= 2

    def _overlay_smooth_lead(self, kpts, box):
        """Cosmetic overlay polish — does NOT affect shot timing (that path uses the RAW kpts).
        (1) Light per-joint EMA removes the frame-to-frame flicker that made limbs look jumpy.
        (2) A velocity LEAD shifts the drawn skeleton/box forward by ~the inference+pipe latency
        (the box's smoothed translation × lead frames, capped) so it stops trailing a moving player.
        Tunable: ORION_POSE_OVERLAY_EMA (0..1, higher=snappier), ORION_POSE_LEAD_FRAMES."""
        disp = kpts.copy()
        a = float(os.environ.get("ORION_POSE_OVERLAY_EMA", "0.7"))
        if (self._kpts_ema is None) or (self._kpts_ema.shape[0] != kpts.shape[0]):
            self._kpts_ema = kpts[:, :2].astype(np.float64).copy()
        for i in range(kpts.shape[0]):
            if kpts[i, 2] >= KP_CONF_FLOOR:
                self._kpts_ema[i, 0] = a * kpts[i, 0] + (1.0 - a) * self._kpts_ema[i, 0]
                self._kpts_ema[i, 1] = a * kpts[i, 1] + (1.0 - a) * self._kpts_ema[i, 1]
                disp[i, 0] = self._kpts_ema[i, 0]
                disp[i, 1] = self._kpts_ema[i, 1]
            else:
                self._kpts_ema[i, 0] = kpts[i, 0]
                self._kpts_ema[i, 1] = kpts[i, 1]
        lead = float(os.environ.get("ORION_POSE_LEAD_FRAMES", "1.2"))
        lx = max(-40.0, min(40.0, float(self._box_tracker.vx) * lead))
        ly = max(-40.0, min(40.0, float(self._box_tracker.vy) * lead))
        disp[:, 0] += lx
        disp[:, 1] += ly
        dbox = [box[0] + lx, box[1] + ly, box[2] + lx, box[3] + ly]
        return disp, dbox

    def update(self, frame, frame_seq: int, frame_time: Optional[float] = None) -> Optional[PoseLandmark]:
        """Process one frame. Returns a PoseLandmark if a push or release was detected this frame.

        frame_time: video-time of this frame in seconds. LIVE passes nothing -> real-time
        perf_counter (frames arrive in real time). OFFLINE replay passes frame_index/fps so the
        velocities/EMA are computed in true video-time (not CPU-processing time) -> deterministic
        AND matched to live, instead of distorted by however fast the eval happens to run."""
        H, W = frame.shape[:2]
        now = frame_time if frame_time is not None else time.perf_counter()

        # Update FPS estimate
        if self._last_frame_time > 0:
            dt = now - self._last_frame_time
            if 0.005 < dt < 0.1:
                self._dt_ms = self._dt_ms * 0.9 + dt * 1000.0 * 0.1
        self._last_frame_time = now

        # Reset overlay — will be set if we get a valid track this frame
        self.last_overlay = None

        # Shot-at-arm anchor: collect wrist-Y from all players during anchor window
        self._arm_anchor_just_fired = False
        track = None
        if self._arm_anchor_active:
            self._arm_anchor_update(frame, frame_seq, H, W)

        # If the arm anchor just fired and locked to a player, use its box directly
        # instead of letting _detect_player_v9 override the tracker on this frame.
        if self._arm_anchor_just_fired and self._box_tracker.cx is not None:
            bt = self._box_tracker
            pbox = [bt.cx - bt.w / 2, bt.cy - bt.h / 2, bt.cx + bt.w / 2, bt.cy + bt.h / 2]
            px1, py1, px2, py2 = self._compute_crop_region(pbox, H, W)
            crop = frame[py1:py2, px1:px2]
            if crop.shape[0] >= 16 and crop.shape[1] >= 16:
                try:
                    r = self.pose.predict(crop, verbose=False, conf=self.pose_conf)[0]
                    if r.boxes is not None and len(r.boxes) > 0:
                        if len(r.boxes) > 1:
                            areas = (r.boxes.xyxy.cpu().numpy()[:, 2] - r.boxes.xyxy.cpu().numpy()[:, 0]) * \
                                    (r.boxes.xyxy.cpu().numpy()[:, 3] - r.boxes.xyxy.cpu().numpy()[:, 1])
                            bi = int(np.argmax(areas))
                        else:
                            bi = 0
                        kpts = r.keypoints.data.cpu().numpy()[bi].copy()
                        kpts[:, 0] += px1
                        kpts[:, 1] += py1
                        track = {"box": pbox, "kpts": kpts}
                except Exception:
                    pass

        # ROI CROP fast path: when tracker is locked, skip v9 and run pose on a
        # small crop around the tracker prediction. ~10-30× less per-frame work.
        # Every _v9_interval frames, fall through to v9 for re-validation +
        # ball/stamina update. Disabled during arm anchor (needs all players).
        if track is None and self._box_tracker.is_valid() and not self._arm_anchor_active:
            self._v9_skip_counter += 1
            if self._v9_skip_counter < self._v9_interval:
                pred = self._box_tracker.predict()
                if pred is not None:
                    px1, py1, px2, py2 = self._compute_crop_region(pred, H, W)
                    crop = frame[py1:py2, px1:px2]
                    if crop.shape[0] >= 16 and crop.shape[1] >= 16:
                        try:
                            r = self.pose.predict(crop, verbose=False, conf=self.pose_conf)[0]
                            if r.boxes is not None and len(r.boxes):
                                xyxy_crop = r.boxes.xyxy.cpu().numpy()
                                # Pick person closest to tracker predicted center
                                # (not largest — a nearby larger player would steal the lock)
                                tcx = self._box_tracker.cx
                                tcy = self._box_tracker.cy
                                best_d, bi = 1e18, 0
                                for p in range(len(xyxy_crop)):
                                    pcx = (xyxy_crop[p, 0] + xyxy_crop[p, 2]) / 2.0 + px1
                                    pcy = (xyxy_crop[p, 1] + xyxy_crop[p, 3]) / 2.0 + py1
                                    d = (pcx - tcx) ** 2 + (pcy - tcy) ** 2
                                    if d < best_d:
                                        best_d = d
                                        bi = p
                                kp = r.keypoints.data.cpu().numpy()[bi].copy()
                                kp[:, 0] += px1
                                kp[:, 1] += py1
                                bb = r.boxes.xyxy.cpu().numpy()[bi].copy()
                                bb[0] += px1; bb[2] += px1; bb[1] += py1; bb[3] += py1
                                # Size sanity: reject person switch (very different area)
                                tracker_area = self._box_tracker.w * self._box_tracker.h
                                det_area = (bb[2] - bb[0]) * (bb[3] - bb[1])
                                if tracker_area > 0 and (det_area / tracker_area > 3.0 or det_area / tracker_area < 0.33):
                                    pass  # suspected switch — fall through to v9
                                else:
                                    self._box_tracker.update(bb)
                                    self._v9_continuity = True
                                    self._update_online_sig(frame, bb)
                                    track = {"box": bb, "kpts": kp, "bar": None, "src": "crop"}
                        except Exception:
                            pass
            else:
                # Re-validation frame — reset counter, fall through to v9
                self._v9_skip_counter = 0

        # Get locked player — try v9 detection first, fall back to stamina tracker
        if track is None:
            self._v9_skip_counter = 0
            det = self._detect_player_v9(frame)
            if det is not None:
                pbox, sbox = det
                px1, py1, px2, py2 = self._compute_crop_region(pbox, H, W)
                crop = frame[py1:py2, px1:px2]
                # Require a real crop: a box that has drifted to the frame edge (a coasting
                # tracker prediction) can yield a 1px-thin slice that passes size>0 but crashes
                # cv2.resize inside pose.predict. Skip it -> the full-frame fallback handles it.
                if crop.shape[0] >= 16 and crop.shape[1] >= 16:
                    r = self.pose.predict(crop, verbose=False, conf=self.pose_conf)[0]
                    if r.boxes is not None and len(r.boxes):
                        # Pick the largest person in the crop (most likely our player)
                        if len(r.boxes) > 1:
                            areas = (r.boxes.xyxy.cpu().numpy()[:, 2] - r.boxes.xyxy.cpu().numpy()[:, 0]) * \
                                    (r.boxes.xyxy.cpu().numpy()[:, 3] - r.boxes.xyxy.cpu().numpy()[:, 1])
                            bi = int(np.argmax(areas))
                        else:
                            bi = 0
                        kp = r.keypoints.data.cpu().numpy()[bi].copy()
                        kp[:, 0] += px1
                        kp[:, 1] += py1
                        bb = r.boxes.xyxy.cpu().numpy()[bi].copy()
                        bb[0] += px1; bb[2] += px1; bb[1] += py1; bb[3] += py1
                        track = {"box": bb, "kpts": kp, "bar": sbox, "src": "v9"}
                    else:
                        # Pose failed on crop — try full frame as fallback
                        r = self.pose.predict(frame, verbose=False, conf=self.pose_conf)[0]
                        if r.boxes is not None and len(r.boxes):
                            # Pick person closest to v9 box center
                            v9_cx = (pbox[0] + pbox[2]) / 2.0
                            v9_cy = (pbox[1] + pbox[3]) / 2.0
                            xyxy = r.boxes.xyxy.cpu().numpy()
                            dists = [((xyxy[p, 0] + xyxy[p, 2]) / 2 - v9_cx) ** 2 +
                                     ((xyxy[p, 1] + xyxy[p, 3]) / 2 - v9_cy) ** 2
                                     for p in range(len(xyxy))]
                            bi = int(np.argmin(dists))
                            kp = r.keypoints.data.cpu().numpy()[bi].copy()
                            bb = xyxy[bi].copy()
                            track = {"box": bb, "kpts": kp, "bar": sbox, "src": "v9_full"}

        # If v9 failed entirely but box tracker has a prediction, use it to crop for pose
        if track is None and self._box_tracker.is_valid():
            pred = self._box_tracker.predict()
            if pred is not None:
                px1, py1, px2, py2 = self._compute_crop_region(pred, H, W)
                crop = frame[py1:py2, px1:px2]
                # Require a real crop: a box that has drifted to the frame edge (a coasting
                # tracker prediction) can yield a 1px-thin slice that passes size>0 but crashes
                # cv2.resize inside pose.predict. Skip it -> the full-frame fallback handles it.
                if crop.shape[0] >= 16 and crop.shape[1] >= 16:
                    r = self.pose.predict(crop, verbose=False, conf=self.pose_conf)[0]
                    if r.boxes is not None and len(r.boxes):
                        if len(r.boxes) > 1:
                            areas = (r.boxes.xyxy.cpu().numpy()[:, 2] - r.boxes.xyxy.cpu().numpy()[:, 0]) * \
                                    (r.boxes.xyxy.cpu().numpy()[:, 3] - r.boxes.xyxy.cpu().numpy()[:, 1])
                            bi = int(np.argmax(areas))
                        else:
                            bi = 0
                        kp = r.keypoints.data.cpu().numpy()[bi].copy()
                        kp[:, 0] += px1
                        kp[:, 1] += py1
                        bb = r.boxes.xyxy.cpu().numpy()[bi].copy()
                        bb[0] += px1; bb[2] += px1; bb[1] += py1; bb[3] += py1
                        track = {"box": bb, "kpts": kp, "bar": None, "src": "track"}

        # If still no track, try full-frame pose and pick person closest to tracker prediction
        if track is None and self._box_tracker.is_valid():
            r = self.pose.predict(frame, verbose=False, conf=self.pose_conf)[0]
            if r.boxes is not None and len(r.boxes):
                pred_raw = self._box_tracker.predict_box_raw()
                if pred_raw is not None:
                    tcx, tcy, _, _ = pred_raw
                    xyxy = r.boxes.xyxy.cpu().numpy()
                    dists = [((xyxy[p, 0] + xyxy[p, 2]) / 2 - tcx) ** 2 +
                             ((xyxy[p, 1] + xyxy[p, 3]) / 2 - tcy) ** 2
                             for p in range(len(xyxy))]
                    bi = int(np.argmin(dists))
                    kp_bi = r.keypoints.data.cpu().numpy()[bi]
                    sim = self._appearance_sim(frame, kp_bi)
                    if sim is None or sim >= APPEARANCE_MATCH_MIN:
                        kp = kp_bi.copy()
                        bb = xyxy[bi].copy()
                        track = {"box": bb, "kpts": kp, "bar": None, "src": "full_track"}
                    # else: closest person is a different jersey -> coast (leave track None)

        # If v9 failed but box tracker has position, skip StaminaTracker
        # (it picks wrong players in crowded scenes) and go to filtered full-frame fallback
        if track is None and not self._box_tracker.has_last_position():
            track = self.tracker.update(frame)

        # Last resort: if still no track, run pose on full frame and pick by heuristic
        if track is None or track.get("kpts") is None:
            r = self.pose.predict(frame, verbose=False, conf=self.pose_conf)[0]
            if r.boxes is not None and len(r.boxes):
                xyxy = r.boxes.xyxy.cpu().numpy()
                H_frame, W_frame = frame.shape[:2]
                confs = r.boxes.conf.cpu().numpy()
                kps_all = r.keypoints.data.cpu().numpy()
                # If box tracker has last known position, use distance + size filter
                # BUT re-bootstrap every 60 frames to allow switching to a new shooter
                if self._box_tracker.has_last_position() and self._box_tracker.age < 60:
                    lp = self._box_tracker.last_position()
                    tcx, tcy, tw, th = lp
                    max_dist = 1.5 * np.sqrt(tw ** 2 + th ** 2)
                    best_d, best_bi = 1e18, -1
                    for p in range(len(xyxy)):
                        bcx = (xyxy[p, 0] + xyxy[p, 2]) / 2.0
                        bcy = (xyxy[p, 1] + xyxy[p, 3]) / 2.0
                        d = np.sqrt((bcx - tcx) ** 2 + (bcy - tcy) ** 2)
                        bw = xyxy[p, 2] - xyxy[p, 0]
                        bh = xyxy[p, 3] - xyxy[p, 1]
                        area_ratio = (bw * bh) / max(tw * th, 1)
                        if area_ratio < 0.33 or area_ratio > 3.0:
                            continue
                        sim = self._appearance_sim(frame, kps_all[p])
                        if sim is not None and sim < APPEARANCE_MATCH_MIN:
                            continue  # nearby & same-size, but wrong jersey -> not our player
                        if d < max_dist and d < best_d:
                            best_d, best_bi = d, p
                    if best_bi >= 0:
                        bi = best_bi
                        kp = kps_all[bi].copy()
                        bb = xyxy[bi].copy()
                        track = {"box": bb, "kpts": kp, "bar": None, "src": "full_frame"}
                        self._box_tracker.update(bb)
                else:
                    # Bootstrap: pick the person whose wrist is highest (lowest Y)
                    # = most likely to be in a shooting motion.
                    # Wrist at rest is ~0.55-0.65 of frame height; during shot
                    # the shooting hand rises to ~0.35-0.45.
                    # User-player classifier: SOFT boost (0.5x weight) — imperfect cross-clip
                    # generalization (F1=0.50 avg), so it's a tiebreaker not a gate.
                    best_score, best_bi = -1.0, -1
                    for p in range(len(xyxy)):
                        kp = kps_all[p]
                        # Appearance veto: with a locked signature, never bootstrap onto a
                        # different-jersey player -- a contesting defender with a hand up is
                        # THE false-push source. Unmeasurable torso -> None -> allowed.
                        sim = self._appearance_sim(frame, kp)
                        if sim is not None and sim < APPEARANCE_MATCH_MIN:
                            continue
                        rw = kp[10]  # right wrist
                        lw = kp[9]   # left wrist
                        # Use the higher-confidence wrist
                        wrist_y = min(rw[1], lw[1]) if rw[2] >= 0.3 and lw[2] >= 0.3 else \
                                  (rw[1] if rw[2] >= 0.3 else (lw[1] if lw[2] >= 0.3 else H_frame * 0.7))
                        wrist_y_norm = wrist_y / H_frame
                        # Score: lower wrist-Y (higher hand) = more likely shooting
                        # Combined with confidence and center proximity
                        cx = (xyxy[p, 0] + xyxy[p, 2]) / 2.0
                        cy = (xyxy[p, 1] + xyxy[p, 3]) / 2.0
                        area = (xyxy[p, 2] - xyxy[p, 0]) * (xyxy[p, 3] - xyxy[p, 1])
                        center_dist = abs(cx - W_frame / 2) / W_frame + abs(cy - H_frame * 0.5) / H_frame
                        # Higher score for lower wrist-Y (higher hand), larger area, higher conf
                        hand_up_score = max(0, 0.65 - wrist_y_norm) * 10  # 0 if wrist below 0.65
                        score = (hand_up_score + area / 50000.0) * confs[p] / (1 + center_dist * 2)
                        # User-player classifier soft boost
                        if self._user_classifier is not None:
                            cls_score = self._score_user_candidate(frame, xyxy[p])
                            score *= (1.0 + 0.5 * cls_score)  # up to 1.5x boost for high user score
                        if score > best_score:
                            best_score, best_bi = score, p
                    if best_bi >= 0:
                        bi = best_bi
                        kp = kps_all[bi].copy()
                        bb = xyxy[bi].copy()
                        track = {"box": bb, "kpts": kp, "bar": None, "src": "full_frame"}
                        self._box_tracker.update(bb)

        if track is None or track["kpts"] is None:
            self._append(float("nan"), float("nan"), float("nan"), frame_seq, now)
            self._last_pose_conf = 0.0
            return None

        # Update failure detection state
        bb = track["box"]
        self._last_box_area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        # Estimate pose confidence from keypoint visibility
        kpts = track["kpts"]
        visible_kpts = np.sum(kpts[:, 2] >= 0.3)
        self._last_pose_conf = visible_kpts / 17.0

        # Expose overlay data (full-frame pixel coords) for skeleton visualization.
        # Expand the box to encompass visible keypoints so the overlay shows the
        # full player (v9 detection box often cuts off feet/ankles).
        vis_mask = kpts[:, 2] >= 0.3
        if np.any(vis_mask):
            vis_kpts = kpts[vis_mask]
            overlay_box = [
                float(min(bb[0], vis_kpts[:, 0].min())),
                float(min(bb[1], vis_kpts[:, 1].min())),
                float(max(bb[2], vis_kpts[:, 0].max())),
                float(max(bb[3], vis_kpts[:, 1].max())),
            ]
        else:
            overlay_box = [float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])]
        fh, fw = frame.shape[:2]
        disp_kpts, disp_box = self._overlay_smooth_lead(kpts, overlay_box)
        ob_cx = (disp_box[0] + disp_box[2]) * 0.5
        ob_cy = (disp_box[1] + disp_box[3]) * 0.5
        indic = ([float(self._stamina_pos[0]), float(self._stamina_pos[1])]
                 if (self._stamina_pos is not None and self._stamina_age < 8) else None)
        self.last_overlay = {
            "kpts": disp_kpts.tolist(),
            "box": disp_box,
            # Camera-anchor viz (bottom-line): a fixed bottom-center anchor point,
            # the locked player's box center, and the under-player marker point when fresh.
            "anchor": [float(fw) * 0.5, float(fh)],
            "lock_center": [float(ob_cx), float(ob_cy)],
            "indicator": indic,
            "lock_src": track.get("src"),
            "lock_conf": float(self._last_pose_conf),
        }

        # Update online appearance signature from the pose-confirmed track
        self._update_online_sig(frame, bb)

        wrist_y = self._extract_wrist_y(track["kpts"], H)
        hip_y = self._extract_hip_y(track["kpts"], H)
        knee_y = self._extract_knee_y(track["kpts"], H)

        # Temporal consistency check: reject sudden jumps in wrist-Y or hip-Y
        # that indicate a person switch (not real motion — real wrist motion
        # is at most ~0.10/frame at 60fps). Use recent median as reference.
        if self._buf_count >= 15 and not np.isnan(wrist_y):
            recent_wy = self._wrist_y[np.arange(self._buf_idx - 15, self._buf_idx - 1) % self._max_buf]
            valid_wy = recent_wy[~np.isnan(recent_wy)]
            if len(valid_wy) >= 5:
                med_wy = float(np.median(valid_wy))
                if abs(wrist_y - med_wy) > 0.25:
                    # Person switch — reject and coast
                    self._append(float("nan"), float("nan"), float("nan"), frame_seq, now)
                    return None
        if self._buf_count >= 15 and not np.isnan(hip_y):
            recent_hy = self._hip_y[np.arange(self._buf_idx - 15, self._buf_idx - 1) % self._max_buf]
            valid_hy = recent_hy[~np.isnan(recent_hy)]
            if len(valid_hy) >= 5:
                med_hy = float(np.median(valid_hy))
                if abs(hip_y - med_hy) > 0.20:
                    self._append(float("nan"), float("nan"), float("nan"), frame_seq, now)
                    return None

        self._append(wrist_y, hip_y, knee_y, frame_seq, now)

        # Seed/refresh the jersey signature from a stamina-bar-confirmed lock, between shots
        # only (at rest the torso is unoccluded and the bar marks the user's player). This is
        # what later lets the fallback paths veto a different-jersey switch. Trust two lock
        # sources: a stamina-bar-confirmed frame (strongest), OR a v9 pick that FOLLOWED the
        # existing track (stable continuity -> very likely still the user, and far more frequent
        # than a per-frame stamina hit in busy multi-player scenes). A fresh/uncertain v9
        # re-acquire (largest box) is deliberately NOT trusted as a seed.
        if self._appearance_veto_enabled and not self._push_emitted and (
                track.get("bar") is not None
                or (track.get("src") in ("v9", "v9_full") and self._v9_continuity)):
            self._refresh_signature(frame, track["kpts"])

        # Need enough history
        if self._buf_count < 30:
            return None

        # Get ordered trajectory
        wy, hy, ky, t, fseqs = self._get_ordered()
        dt_ms = self._dt_ms

        # Smooth all trajectories
        wy_s = self._ema_tau(self._fill_nans(wy), EMA_TAU_MS, dt_ms)
        hy_s = self._ema_tau(self._fill_nans(hy), EMA_TAU_MS, dt_ms)
        ky_s = self._ema_tau(self._fill_nans(ky), EMA_TAU_MS, dt_ms)
        wy_vel = np.gradient(wy_s, t)
        wy_vel_s = self._ema_tau(self._fill_nans(wy_vel),
                                 VEL_EMA_TAU_MS if self._vel_peak_push else EMA_TAU_MS, dt_ms)

        # GLM zero-cross release signal: tau=20ms causal EMA on wrist-Y + backward-difference (causal)
        # velocity. Kept SEPARATE from wy_s/wy_vel_s (tau=30/55ms) -- the tighter tau + causal diff is
        # what reproduces the 72ms; np.gradient (centered) would leak one future frame.
        wy_zc = vel_zc = None
        if self._zerocross_release:
            wy_zc = self._ema_tau(self._fill_nans(wy), ZEROCROSS_EMA_TAU_MS, dt_ms)
            vel_zc = np.empty_like(wy_zc); vel_zc[0] = 0.0
            vel_zc[1:] = wy_zc[1:] - wy_zc[:-1]

        # Update rest positions (running median over adaptive window)
        valid_w = ~np.isnan(wy_s)
        valid_h = ~np.isnan(hy_s)
        if valid_w.sum() < 20 or valid_h.sum() < 20:
            return None
        # Use recent window for adaptive thresholds
        n_hist = min(ADAPTIVE_HISTORY_FRAMES, len(wy_s))
        # Rest position = 75th percentile (excludes shot frames where hand is up = lower Y)
        # Median was too low because shot frames pulled it down
        self._rest_wrist = float(np.nanpercentile(wy_s[-n_hist:], 75))
        self._rest_hip = float(np.nanpercentile(hy_s[-n_hist:], 50))

        rest_w = self._rest_wrist
        rest_h = self._rest_hip
        if np.isnan(rest_w) or np.isnan(rest_h):
            return None

        # Adaptive release depth from running wrist-Y distribution
        wy_hist = wy_s[-n_hist:]
        wy_valid = wy_hist[~np.isnan(wy_hist)]
        adaptive_rel_depth = float(np.percentile(wy_valid, RELEASE_DEPTH_PERCENTILE))
        # Release must be below this (lower Y = higher hand) and below rest - 0.05 minimum
        adaptive_rel_threshold = min(adaptive_rel_depth, rest_w - 0.05)

        # --- GATHER detection (arms the push detector) ---
        t_now = t[-1]
        if self._detect_gather(hy_s, ky_s, t, t_now):
            self._gather_time = t_now

        # --- PUSH detection ---
        if not self._push_emitted and t_now >= self._push_cooldown_until:
            # Shot-specificity gate: a real jumpshot is preceded by a GATHER (hip/knee crouch).
            # A bare wrist-rise (dribble / pass / pump-fake) has no recent gather -> reject it.
            # _gather_time is refreshed every frame a crouch is seen in the trailing window.
            gather_recent = (t_now - self._gather_time) <= GATHER_TO_PUSH_MAX_MS / 1000.0
            gate_ok = (gather_recent or not self._require_gather) and \
                      (t_now - self._last_shot_time >= MIN_SHOT_SPACING_S)
            if self._vel_peak_push:
                # Anchor at the MAX upward wrist velocity (a consistent biomechanical instant),
                # not an arm-height crossing. The peak is known one frame late: wy_vel_s[-2] is a
                # local minimum (most negative) when it sits at/below both neighbours. Require a
                # real upward burst AND the wrist genuinely raised above rest at that peak.
                if gate_ok and len(wy_vel_s) >= 3:
                    v2, v1, v0 = wy_vel_s[-3], wy_vel_s[-2], wy_vel_s[-1]
                    peak_wy = wy_s[-2]
                    if (v1 <= v2 and v1 <= v0 and v1 < PUSH_VEL_PEAK_THRESHOLD
                            and not np.isnan(peak_wy) and peak_wy < rest_w - PUSH_MIN_RISE_PCT):
                        self._push_emitted = True
                        self._push_idx = len(wy_s) - 2
                        self._push_time = t[-2]
                        landmark = PoseLandmark(kind="push", frame_seq=int(fseqs[-2]),
                                                confidence=min(1.0, abs(v1) / 0.15), timestamp=now,
                                                arm_token=self._controller_arm_token)
                        if self.on_landmark:
                            self.on_landmark(landmark)
                        return landmark
            else:
                # Legacy: height-threshold-crossing push (wide STD; kept for A/B against vel-peak).
                cur_wy = wy_s[-1] if not np.isnan(wy_s[-1]) else rest_w
                cur_vel = wy_vel_s[-1] if not np.isnan(wy_vel_s[-1]) else 0.0
                push_threshold = rest_w - 0.08
                if cur_wy < push_threshold and cur_vel < -0.02 and gate_ok:
                    recent_wy = wy_s[-5:] if len(wy_s) >= 5 else wy_s
                    below_count = sum(1 for v in recent_wy if not np.isnan(v) and v < push_threshold)
                    if below_count >= 3:
                        self._push_emitted = True
                        self._push_idx = len(wy_s) - 1
                        self._push_time = t_now
                        push_score = min(1.0, abs(cur_vel) / 0.15)
                        conf = 0.5 * push_score + 0.5 * min(1.0, (rest_w - cur_wy) / 0.15)
                        landmark = PoseLandmark(kind="push", frame_seq=frame_seq,
                                                confidence=conf, timestamp=now,
                                                arm_token=self._controller_arm_token)
                        if self.on_landmark:
                            self.on_landmark(landmark)
                        return landmark

        # --- CONTROLLER-STATE ARMED RELEASE (primary path when armed) ---
        # When the SQUARE button arms the search, look for the first zero-crossing
        # in [arm_frame, arm_frame + 60 frames]. This is the validated 60ms IQR path.
        if self._controller_armed and self._zerocross_release and self._buf_count >= 30:
            arm_idx = -1
            for fi in range(len(fseqs) - 1, -1, -1):
                if fseqs[fi] <= self._controller_arm_frame:
                    arm_idx = fi
                    break
            if arm_idx >= 0 and vel_zc is not None:
                # Search from arm to arm + 60 frames for first neg→pos crossing
                search_hi = min(arm_idx + 60, len(vel_zc) - 1)
                for j in range(max(arm_idx + 1, 1), search_hi + 1):
                    if np.isnan(vel_zc[j]) or np.isnan(vel_zc[j - 1]) or np.isnan(wy_zc[j]):
                        continue
                    if vel_zc[j - 1] < 0.0 <= vel_zc[j]:
                        denom = vel_zc[j] - vel_zc[j - 1]
                        frac = (-vel_zc[j - 1] / denom) if abs(denom) > 1e-12 else 0.0
                        frac = min(max(frac, 0.0), 1.0)
                        release_subframe_seq = float(fseqs[j - 1]) + frac

                        self._last_shot_time = t[j]
                        landmark_arm_token = self._controller_arm_token
                        self._controller_armed = False
                        self._controller_arm_frame = -1
                        self._controller_arm_token = 0

                        confidence = 0.9  # high confidence — controller-armed
                        landmark = PoseLandmark(
                            kind="release",
                            frame_seq=int(fseqs[j]),
                            confidence=confidence,
                            timestamp=now,
                            subframe_seq=release_subframe_seq,
                            arm_token=landmark_arm_token,
                        )
                        if self.on_landmark:
                            self.on_landmark(landmark)
                        return landmark
                # Timeout: if 60 frames passed with no crossing, disarm
                frames_since_arm = len(fseqs) - 1 - arm_idx
                if frames_since_arm > 60:
                    self._controller_armed = False
                    self._controller_arm_frame = -1
                    self._controller_arm_token = 0
                    logger.debug("Controller-state ARM timed out (no crossing in 60 frames)")

        # --- RELEASE detection (reactive, hip-apex confirmed) ---
        if self._push_emitted and self._push_idx >= 0:
            t_push = self._push_time
            push_to_now = (t_now - t_push) * 1000.0

            # Push timeout: if no release found within PUSH_TIMEOUT_MS, reset
            if push_to_now > PUSH_TIMEOUT_MS:
                self._push_emitted = False
                self._push_idx = -1
                self._push_cooldown_until = t_now + 0.2  # cooldown 200ms after timeout
            elif push_to_now >= PUSH_TO_REL_MIN_MS:
                # Search window: 80-250ms after push (widened for fadeaways)
                rel_lo_t = t_push + PUSH_TO_REL_MIN_MS / 1000.0
                rel_hi_t = t_push + PUSH_TO_REL_MAX_MS / 1000.0

                # Locate the RELEASE point in the push-to-release window.
                best_k = -1
                best_y = 1e9
                release_subframe_seq = -1.0
                if self._zerocross_release:
                    # GLM-validated sub-frame velocity zero-crossing (40-72ms IQR live): first neg->pos
                    # crossing (rising->falling = ball release), threshold-FREE on velocity -- the smooth
                    # apex eases velocity to ~0 right before it flips, so a magnitude gate would reject
                    # the real release (that exact bug cost us a day). LINEAR sub-frame interpolation
                    # between the two bracketing frames is the precision win the integer-frame path lacked.
                    for j in range(max(self._push_idx + 1, 1), len(wy_zc)):
                        if t[j] < rel_lo_t: continue
                        if t[j] > rel_hi_t: break
                        if np.isnan(vel_zc[j]) or np.isnan(vel_zc[j - 1]) or np.isnan(wy_zc[j]):
                            continue
                        # Light height gate only (wrist genuinely raised above rest): the PUSH already
                        # confirmed a shot, so the strict adaptive percentile that strangled the rate
                        # (2/87) is unnecessary here -- the first clean crossing after a real push IS
                        # the apex (matches GLM's threshold-free probe that found all 87).
                        if vel_zc[j - 1] < 0.0 <= vel_zc[j] and wy_zc[j] < rest_w - 0.04:
                            denom = vel_zc[j] - vel_zc[j - 1]
                            frac = (-vel_zc[j - 1] / denom) if abs(denom) > 1e-12 else 0.0
                            frac = min(max(frac, 0.0), 1.0)
                            best_k = j
                            best_y = wy_zc[j]
                            release_subframe_seq = float(fseqs[j - 1]) + frac
                            break
                elif self._apex_release:
                    # APEX = wrist-Y velocity zero-crossing (rising -> falling): the ball leaving the
                    # hand. The FIRST crossing where the wrist is genuinely raised (below the release
                    # threshold) is the release -- a derivative signal, robust to the lone-deep-frame
                    # noise that scatters the deepest-point method.
                    for j in range(max(self._push_idx + 1, 1), len(wy_s)):
                        if t[j] < rel_lo_t: continue
                        if t[j] > rel_hi_t: break
                        if np.isnan(wy_vel_s[j]) or np.isnan(wy_vel_s[j - 1]) or np.isnan(wy_s[j]):
                            continue
                        if wy_vel_s[j - 1] < 0.0 <= wy_vel_s[j] and wy_s[j] < adaptive_rel_threshold:
                            best_k = j
                            best_y = wy_s[j]
                            break
                else:
                    # Legacy: deepest wrist-Y (highest hand) in the window (noise-sensitive).
                    for j in range(self._push_idx, len(wy_s)):
                        if t[j] < rel_lo_t: continue
                        if t[j] > rel_hi_t: break
                        if np.isnan(wy_s[j]): continue
                        if wy_s[j] < best_y:
                            best_y = wy_s[j]
                            best_k = j

                # Use adaptive depth threshold instead of fixed RELEASE_DEPTH
                # Zero-cross already height-gated inside its loop; the legacy paths still re-check the
                # strict adaptive threshold here (this outer re-check is what overrode the relaxed gate).
                if best_k >= 0 and (self._zerocross_release or best_y < adaptive_rel_threshold):
                    push_to_rel = (t[best_k] - t_push) * 1000.0
                    if PUSH_TO_REL_MIN_MS <= push_to_rel <= PUSH_TO_REL_MAX_MS:
                        # Hip apex confirmation (optional — soft score only)
                        hip_apex = self._find_hip_apex(hy_s, t, t[best_k])

                        # Confidence score: blend wrist + depth + hip apex + timing
                        push_score = min(1.0, abs(wy_vel_s[self._push_idx]) / 0.15)
                        rel_depth = max(0.0, rest_w - wy_s[best_k])
                        depth_score = min(1.0, rel_depth / 0.15)
                        apex_score = 0.8 if hip_apex else 0.4
                        if 100 <= push_to_rel <= 250:
                            timing_score = 1.0
                        elif 80 <= push_to_rel <= 400:
                            timing_score = 0.7
                        else:
                            timing_score = 0.3
                        confidence = (0.35 * push_score + 0.25 * depth_score +
                                      0.25 * apex_score + 0.15 * timing_score)

                        self._last_shot_time = t[best_k]
                        landmark_arm_token = self._controller_arm_token
                        self._push_emitted = False
                        self._push_idx = -1
                        self._controller_armed = False
                        self._controller_arm_frame = -1
                        self._controller_arm_token = 0

                        landmark = PoseLandmark(
                            kind="release",
                            frame_seq=int(fseqs[best_k]),
                            confidence=confidence,
                            timestamp=now,
                            subframe_seq=release_subframe_seq,
                            arm_token=landmark_arm_token,
                        )
                        if self.on_landmark:
                            self.on_landmark(landmark)
                        return landmark

        return None

    def notify_shot_start(self, frame_seq: int, timestamp: float = 0.0,
                          arm_token: int = 0):
        """Called by the orchestrator when the SQUARE button rising edge is detected.

        This is the controller-state arming signal — the ground truth "shot is
        starting NOW" that replaces the legacy vision-based PUSH detector.
        Validated: 100% arm rate, 60ms IQR across V3-V7 (vs 44% / +578ms for push).

        Arms the zero-crossing release search to scan [arm_frame, arm_frame + 60]
        for the first neg→pos velocity crossing (the ball release).

        Also activates the shot-at-arm anchor: snapshots all detected players'
        poses to identify the user's player by jumpshot wrist-rise signature.
        """
        self._controller_arm_frame = frame_seq
        self._controller_arm_time = timestamp if timestamp > 0 else time.perf_counter()
        self._controller_armed = True
        self._controller_arm_token = int(arm_token) if int(arm_token) > 0 else 0
        self._push_emitted = False
        self._push_idx = -1

        # Activate shot-at-arm anchor
        self._arm_anchor_active = True
        self._arm_anchor_frame = frame_seq
        self._arm_anchor_candidates = {}
        self._arm_anchor_next_id = 0

        # Snapshot anchor positions at shot start for use in evaluation 12 frames later
        self._arm_anchor_stamina = (self._stamina_pos, self._stamina_age) if self._stamina_age < 30 else (None, 999)
        self._arm_anchor_hint = (self._player_hint[0], self._player_hint[1]) if self._hint_valid() else (None, None)
        self._arm_anchor_ball = (self._ball_pos, self._ball_age) if self._ball_age < 10 else (None, 999)

        logger.debug("Controller-state ARM at frame %d (anchor active)", frame_seq)

    def reset(self):
        """Reset shot state (call between shots or on shot abort)."""
        self._push_emitted = False
        self._push_idx = -1
        self._push_time = 0.0
        self._push_cooldown_until = -1e9
        self._gather_time = -1e9
        self._controller_arm_frame = -1
        self._controller_arm_time = -1e9
        self._controller_armed = False
        self._controller_arm_token = 0
        self._arm_anchor_active = False
        self._arm_anchor_candidates = {}
        self.last_overlay = None
        self._v9_skip_counter = 0

    def _arm_anchor_update(self, frame, frame_seq, H, W):
        """Collect wrist-Y samples from all detected players during the arm anchor window.
        After ~10 frames, pick the player with the strongest wrist-rise signature and
        re-anchor _box_tracker to them.

        Uses v9 player detection (Player class only) to avoid refs/crowd, then runs
        pose on each player crop for wrist keypoints."""
        if not self._arm_anchor_active:
            return

        frames_since_arm = frame_seq - self._arm_anchor_frame
        if frames_since_arm > 12:
            # Window expired — evaluate and pick winner
            self._arm_anchor_evaluate(frame, H, W)
            return

        # Use v9 player detection to get player boxes (filters out refs/crowd)
        try:
            if self.player_det is None:
                # Fallback: use pose model if v9 not available
                r = self.pose.predict(frame, verbose=False, conf=self.pose_conf)[0]
                if r.boxes is None or len(r.boxes) == 0:
                    return
                xyxy = r.boxes.xyxy.cpu().numpy()
                kps_all = r.keypoints.data.cpu().numpy()
                player_indices = range(len(xyxy))
            else:
                rv = self.player_det.predict(frame, verbose=False, conf=0.15, imgsz=640)[0]
                if rv.boxes is None or len(rv.boxes) == 0:
                    return
                cls_v = rv.boxes.cls.cpu().numpy().astype(int)
                xyxy_v = rv.boxes.xyxy.cpu().numpy()
                player_boxes_v = xyxy_v[cls_v == 0]
                if len(player_boxes_v) == 0:
                    return

                # Run pose on each player crop to get wrist keypoints
                xyxy = []
                kps_all = []
                for pb in player_boxes_v:
                    px1, py1, px2, py2 = self._compute_crop_region(pb, H, W)
                    crop = frame[py1:py2, px1:px2]
                    if crop.shape[0] < 16 or crop.shape[1] < 16:
                        continue
                    rp = self.pose.predict(crop, verbose=False, conf=self.pose_conf)[0]
                    if rp.boxes is None or len(rp.boxes) == 0:
                        continue
                    # Pick largest person in crop
                    if len(rp.boxes) > 1:
                        areas = (rp.boxes.xyxy.cpu().numpy()[:, 2] - rp.boxes.xyxy.cpu().numpy()[:, 0]) * \
                                (rp.boxes.xyxy.cpu().numpy()[:, 3] - rp.boxes.xyxy.cpu().numpy()[:, 1])
                        bi = int(np.argmax(areas))
                    else:
                        bi = 0
                    kp = rp.keypoints.data.cpu().numpy()[bi].copy()
                    kp[:, 0] += px1
                    kp[:, 1] += py1
                    xyxy.append(pb)
                    kps_all.append(kp)

                if not xyxy:
                    return
                xyxy = np.array(xyxy)
                kps_all = np.array(kps_all)
                player_indices = range(len(xyxy))

            # Match detections to existing candidates by box center distance
            for bi in player_indices:
                bcx = (xyxy[bi, 0] + xyxy[bi, 2]) / 2.0
                bcy = (xyxy[bi, 1] + xyxy[bi, 3]) / 2.0

                # Find matching candidate
                matched_id = None
                best_dist = 1e18
                for cid, samples in self._arm_anchor_candidates.items():
                    if not samples:
                        continue
                    last_box = samples[-1]
                    last_cx = (last_box[2] + last_box[4]) / 2.0
                    last_cy = (last_box[3] + last_box[5]) / 2.0
                    d = (bcx - last_cx) ** 2 + (bcy - last_cy) ** 2
                    if d < best_dist:
                        best_dist = d
                        matched_id = cid

                # If no match or too far, create new candidate
                if matched_id is None or best_dist > 10000:  # 100px threshold
                    matched_id = self._arm_anchor_next_id
                    self._arm_anchor_next_id += 1

                # Record wrist Y (normalized) + box
                kp = kps_all[bi]
                rw = kp[10]  # right wrist
                lw = kp[9]   # left wrist
                # Use higher-confidence wrist
                if rw[2] >= 0.3 and lw[2] >= 0.3:
                    wrist_y_norm = min(rw[1], lw[1]) / H  # higher hand = lower Y
                elif rw[2] >= 0.3:
                    wrist_y_norm = rw[1] / H
                elif lw[2] >= 0.3:
                    wrist_y_norm = lw[1] / H
                else:
                    wrist_y_norm = float('nan')

                if matched_id not in self._arm_anchor_candidates:
                    self._arm_anchor_candidates[matched_id] = []
                self._arm_anchor_candidates[matched_id].append(
                    (wrist_y_norm, frame_seq, xyxy[bi][0], xyxy[bi][1], xyxy[bi][2], xyxy[bi][3])
                )
        except Exception as e:
            logger.debug("Arm anchor update error: %s", e)

    def _arm_anchor_evaluate(self, frame, H, W):
        """Pick the user's player using wrist-rise + proximity bias.

        Score = wrist_rise * proximity_factor
        - wrist_rise: how much the wrist Y decreased (hand went up = jumpshot)
        - proximity_factor: 1.0 if no prior position, else decays with distance
          from the existing box tracker position and/or meter hint.
        """
        self._arm_anchor_active = False

        if not self._arm_anchor_candidates:
            logger.debug("Arm anchor: no candidates collected")
            return

        # Get prior position for proximity bias
        prior_cx, prior_cy = None, None
        if self._box_tracker.has_last_position():
            prior_cx, prior_cy = self._box_tracker.cx, self._box_tracker.cy
        elif self._hint_valid():
            prior_cx, prior_cy = self._player_hint[0], self._player_hint[1]

        # Also use meter hint as a secondary prior (stronger when available).
        # Only X: the meter shares the player's X but sits above them, so Y is unreliable.
        hint_cx = None
        if self._hint_valid():
            hint_cx = self._player_hint[0]

        # Stamina bar position — strongest user-player anchor when available
        # Use snapshotted position from shot start (may be stale by evaluation time)
        stamina_cx, stamina_cy = None, None
        snap_stamina, snap_stamina_age = getattr(self, '_arm_anchor_stamina', (None, 999))
        if snap_stamina is not None and snap_stamina_age < 30:
            stamina_cx, stamina_cy = snap_stamina

        # Use snapshotted hint from shot start
        snap_hint_cx, snap_hint_cy = getattr(self, '_arm_anchor_hint', (None, None))
        if snap_hint_cx is not None:
            hint_cx = snap_hint_cx        # Y intentionally unused: meter hint is only reliable in X

        # Determine the primary anchor position (strongest available)
        # Stamina bar: both X and Y are reliable (appears under player)
        # Meter hint: only X is reliable (meter appears above player, shares X)
        anchor_cx, anchor_cy = None, None
        if stamina_cx is not None:
            anchor_cx, anchor_cy = stamina_cx, stamina_cy
        elif hint_cx is not None:
            anchor_cx = hint_cx  # X only — meter is above player but shares X

        best_id = None
        best_score = -1e9
        best_box = None

        for cid, samples in self._arm_anchor_candidates.items():
            if len(samples) < 3:
                continue
            wrist_ys = [s[0] for s in samples if not np.isnan(s[0])]
            if len(wrist_ys) < 3:
                continue

            # Wrist rise = wrist Y decreased (hand went up)
            n = len(wrist_ys)
            early = np.median(wrist_ys[:max(1, n // 3)])
            late = np.median(wrist_ys[-max(1, n // 3):])
            rise = early - late  # positive = hand rose (Y decreased)

            if rise < 0.005:  # lowered threshold: barely any wrist rise needed
                continue

            # Most recent box for this candidate
            last_sample = samples[-1]
            cand_box = [last_sample[2], last_sample[3], last_sample[4], last_sample[5]]
            cand_cx = (cand_box[0] + cand_box[2]) / 2.0
            cand_cy = (cand_box[1] + cand_box[3]) / 2.0

            # --- Scoring: wrist-rise is primary, proximity is secondary tiebreaker ---
            # Don't let a wrong tracker position veto a correct wrist-rise candidate.
            prox_factor = 1.0
            if prior_cx is not None:
                dist = np.sqrt((cand_cx - prior_cx) ** 2 + (cand_cy - prior_cy) ** 2)
                # Soft proximity: only 0.7..1.0 range, never fully rejects
                prox_factor = 0.7 + 0.3 * np.exp(-dist / 200.0)

            # Ball proximity factor: shooter is near the ball
            ball_factor = 1.0
            if self._ball_age < 15 and self._ball_pos is not None:
                bdist = np.sqrt((cand_cx - self._ball_pos[0]) ** 2 + (cand_cy - self._ball_pos[1]) ** 2)
                ball_factor = 0.5 + 0.5 * np.exp(-bdist / 120.0)

            # Appearance factor: match with online signature
            appear_factor = 1.0
            if self._online_sig is not None and self._online_sig_frames > 5:
                h = self._compute_torso_hist(frame, cand_box)
                if h is not None:
                    sim = float(cv2.compareHist(self._online_sig, h, cv2.HISTCMP_CORREL))
                    appear_factor = 0.7 + 0.3 * max(0.0, sim)

            # Marker-adjacency boost: if stamina bar is present, use nearest-point
            # distance (from stamina_lock.py adjacency rule) to boost the player
            # whose box the marker sits adjacent to. This is more precise than
            # center-to-center distance — the marker sits below/beside the player,
            # not at their center.
            marker_factor = 1.0
            if stamina_cx is not None:
                X1, Y1, X2, Y2 = cand_box
                # Reject marker above player head
                if stamina_cy < Y1 - 0.25 * (Y2 - Y1):
                    marker_factor = 0.5  # marker is above this player — unlikely theirs
                else:
                    qx = min(max(stamina_cx, X1), X2)
                    qy = min(max(stamina_cy, Y1), Y2)
                    mdist = np.sqrt((stamina_cx - qx) ** 2 + (stamina_cy - qy) ** 2)
                    sw = abs(snap_stamina[0] - stamina_cx) * 2 if snap_stamina is not None else 40
                    cap = (max(sw, 40) * 2.5) ** 2
                    if mdist ** 2 < cap:
                        # Strong boost: marker is adjacent to this player's box
                        marker_factor = 1.0 + 0.5 * np.exp(-mdist / 80.0)
                    else:
                        marker_factor = 0.8  # marker is far from this player

            # Wrist rise is primary (0.5..1.5 range modifier), proximity is secondary
            rise_mod = 0.5 + min(1.0, rise * 5.0)
            score = rise_mod * prox_factor * ball_factor * appear_factor * marker_factor

            if score > best_score:
                best_score = score
                best_id = cid
                best_box = cand_box

        if best_id is not None and best_box is not None and best_score > 0.005:
            # Only re-anchor if the tracker is stale/invalid or the anchor's pick
            # is close to the tracker's current position (confirming same player).
            # Don't let a weak wrist-rise signal override a working tracker.
            tracker_valid = self._box_tracker.is_valid()
            if not tracker_valid:
                self._box_tracker.update(best_box)
                self._arm_anchor_just_fired = True
                logger.info("Arm anchor: locked to player %d (score=%.4f, box=%s) [tracker was stale]",
                            best_id, best_score, best_box)
            else:
                # Tracker is valid — check if anchor's pick is near tracker
                bt_cx, bt_cy = self._box_tracker.cx, self._box_tracker.cy
                anchor_cx = (best_box[0] + best_box[2]) / 2.0
                anchor_cy = (best_box[1] + best_box[3]) / 2.0
                dist = np.sqrt((anchor_cx - bt_cx) ** 2 + (anchor_cy - bt_cy) ** 2)
                if dist < 150:
                    # Near tracker — confirm/re-anchor to the anchor's pick
                    self._box_tracker.update(best_box)
                    self._arm_anchor_just_fired = True
                    logger.info("Arm anchor: confirmed player %d (score=%.4f, dist=%.0f)",
                                best_id, best_score, dist)
                else:
                    # Far from tracker — don't override a working tracker
                    logger.debug("Arm anchor: candidate %d far from tracker (dist=%.0f), keeping tracker",
                                 best_id, dist)
        else:
            logger.debug("Arm anchor: no clear winner (best_score=%.4f)", best_score)
