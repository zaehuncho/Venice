"""Production stamina-bar user-lock (meter-independent player identity).

The stamina bar sits under the user's controlled player (only the user has it), so it's the primary
no-meter player-ID. The bar is used to ROI-crop a zoomed region above it and run pose on the crop
(where the player is large) -> the bar solves BOTH the lock and the detection.

This module is the production version of tools/diagnostics/stamina_lock.py, enhanced with:
- Appearance histogram (HSV torso color) for cluster disambiguation + bar-loss coasting
- N-frame confirmation before initial lock acquisition (avoid flicker)
- Anti-switch rule: only switch lock when bar under different player for >=N consecutive frames

API: find_stamina_bar(frame) -> (x,y,w,h)|None
     lock_user_pose(frame, pose) -> dict|None
     StaminaTracker(pose) -> tracker with .update(frame) -> dict|None
"""
from __future__ import annotations

import cv2

_Y_LO, _Y_HI = (15, 80, 120), (40, 255, 255)
_B_LO, _B_HI = (90, 80, 90), (128, 255, 255)

_BAR_MODEL = None
_BAR_TRIED = False


def _bar_model():
    global _BAR_MODEL, _BAR_TRIED
    if not _BAR_TRIED:
        _BAR_TRIED = True
        import os
        if os.path.exists("models/orion_bar_park.pt"):
            try:
                from ultralytics import YOLO
                _BAR_MODEL = YOLO("models/orion_bar_park.pt")
            except Exception:
                _BAR_MODEL = None
        elif os.path.exists("models/orion_bar_n.pt"):
            try:
                from ultralytics import YOLO
                _BAR_MODEL = YOLO("models/orion_bar_n.pt")
            except Exception:
                _BAR_MODEL = None
    return _BAR_MODEL


def find_stamina_bar(frame, exclude_hud: bool = True, conf: float = 0.05):
    """Return the user's stamina-bar bbox (x,y,w,h) or None."""
    m = _bar_model()
    if m is not None:
        r = m.predict(frame, verbose=False, conf=conf)[0]
        if r.boxes is not None and len(r.boxes):
            H, W = frame.shape[:2]
            candidates = []
            for bi in range(len(r.boxes)):
                x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                bw, bh = x2 - x1, y2 - y1
                if bw < 10 or bw > 250 or bh < 10 or bh > 250:
                    continue
                ar = max(bw, bh) / max(min(bw, bh), 1)
                if ar < 1.5:
                    continue
                c = float(r.boxes.conf.cpu().numpy()[bi])
                candidates.append((c, bi, x1, y1, bw, bh))
            if candidates:
                candidates.sort(key=lambda c: -c[0])
                _, _, x1, y1, bw, bh = candidates[0]
                return (int(x1), int(y1), int(bw), int(bh))
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    ym = cv2.inRange(hsv, _Y_LO, _Y_HI)
    bm = cv2.inRange(hsv, _B_LO, _B_HI)
    comb = cv2.dilate(((ym | bm) > 0).astype("uint8") * 255,
                      cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    cnts = cv2.findContours(comb, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    best, best_score = None, 0
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        horiz = w >= 18 and 2 <= h <= 32 and w >= 2.0 * h
        vert = h >= 18 and 2 <= w <= 32 and h >= 2.0 * w
        if not (horiz or vert):
            continue
        if exclude_hud:
            if y < H * 0.15 or y > H * 0.92:
                continue
            if x > W * 0.74 and y > H * 0.74:
                continue
        ysum = int(ym[y:y + h, x:x + w].sum())
        bsum = int(bm[y:y + h, x:x + w].sum())
        if ysum == 0 or bsum == 0:
            continue
        score = min(ysum, bsum)
        if score > best_score:
            best_score, best = score, (x, y, w, h)
    return best


def player_roi(frame, bar):
    """Crop a region centered horizontally on the bar, tall enough for the whole player."""
    H, W = frame.shape[:2]
    x, y, w, h = bar
    cx = x + w / 2.0
    cw = int(max(420, w * 6.0))
    ch = int(max(360, w * 5.0))
    x0 = int(max(0, cx - cw / 2)); x1 = int(min(W, cx + cw / 2))
    y1 = int(min(H, y + h + 0.5 * w))
    y0 = int(max(0, y1 - ch))
    return frame[y0:y1, x0:x1], x0, y0


def lock_user_pose(frame, pose, conf: float = 0.10):
    """Find the bar, crop above it, run pose on the crop. Returns dict(bar, kpts[17,3], box) or None."""
    bar = find_stamina_bar(frame)
    if bar is None:
        return None
    crop, x0, y0 = player_roi(frame, bar)
    if crop.size == 0:
        return None
    r = pose.predict(crop, verbose=False, conf=conf)[0]
    if r.boxes is None or not len(r.boxes):
        return {"bar": bar, "kpts": None, "box": None}
    xyxy = r.boxes.xyxy.cpu().numpy()
    bx, by, bw, bh = bar
    bar_cx, bar_cy = bx + bw / 2.0, by + bh / 2.0
    cap = (max(bw, 40) * 2.5) ** 2
    b, best_d = -1, 1e18
    for p in range(len(xyxy)):
        X1, Y1, X2, Y2 = xyxy[p, 0] + x0, xyxy[p, 1] + y0, xyxy[p, 2] + x0, xyxy[p, 3] + y0
        if bar_cy < Y1 - 0.25 * (Y2 - Y1):
            continue
        qx = min(max(bar_cx, X1), X2); qy = min(max(bar_cy, Y1), Y2)
        d = (bar_cx - qx) ** 2 + (bar_cy - qy) ** 2
        if d < best_d and d < cap:
            best_d, b = d, p
    if b < 0:
        return {"bar": bar, "kpts": None, "box": None}
    kp = r.keypoints.data.cpu().numpy()[b].copy()
    kp[:, 0] += x0; kp[:, 1] += y0
    bb = r.boxes.xyxy.cpu().numpy()[b].copy()
    bb[0] += x0; bb[2] += x0; bb[1] += y0; bb[3] += y0
    return {"bar": bar, "kpts": kp, "box": bb}


def _center(b):
    if len(b) == 4:
        return (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _compute_appearance_hist(frame, box, kpts):
    """HSV color histogram of the player's torso region (between shoulders, above hips).
    Used for disambiguation when players overlap and for re-acquisition after bar loss."""
    if kpts is None:
        return None
    H, W = frame.shape[:2]
    ls = kpts[5]   # left shoulder (x, y, conf)
    rs = kpts[6]   # right shoulder
    lhip = kpts[11]  # left hip
    rhip = kpts[12]  # right hip
    if ls[2] < 0.3 or rs[2] < 0.3:
        return None
    sx = int(min(ls[0], rs[0]))
    ex = int(max(ls[0], rs[0]))
    sy = int(min(ls[1], rs[1]))
    ey = int(max(lhip[1], rhip[1])) if lhip[2] > 0.3 and rhip[2] > 0.3 else int(sy + (ex - sx) * 1.5)
    sx = max(0, sx - 5); ex = min(W, ex + 5)
    sy = max(0, sy); ey = min(H, ey)
    if ex - sx < 10 or ey - sy < 10:
        return None
    torso = frame[sy:ey, sx:ex]
    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist


def _compare_appearance(hist_a, hist_b):
    """Histogram intersection score (0-1). Returns 0 if either is None."""
    if hist_a is None or hist_b is None:
        return 0.0
    return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_INTERSECT))


class StaminaTracker:
    """Temporal lock with appearance histogram, N-frame confirmation, and anti-switch.

    Acquire: lock via bar + pose, require N consecutive confirming frames before committing.
    Stick: follow by box continuity + appearance histogram through bar-loss.
    Anti-switch: only switch lock when bar under different player for >=switch_confirm frames,
                 or lock lost for > max_age frames.
    """

    def __init__(self, pose, max_age: int = 12, gate_frac: float = 0.16,
                 acquire_confirm: int = 3, switch_confirm: int = 5,
                 appearance_threshold: float = 0.35):
        self.pose = pose
        self.max_age = max_age
        self.gate = gate_frac
        self.acquire_confirm = acquire_confirm
        self.switch_confirm = switch_confirm
        self.appearance_threshold = appearance_threshold
        self.box = None
        self.kpts = None
        self.age = 10 ** 9
        self._appearance = None
        self._pending_box = None
        self._pending_kpts = None
        self._pending_bar = None
        self._pending_count = 0
        self._switch_candidate = None
        self._switch_count = 0

    def _consistent(self, b, W):
        if self.box is None:
            return False
        (cx, cy), (px, py) = _center(b), _center(self.box)
        return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 < self.gate * W

    def _update_appearance(self, frame, box, kpts):
        hist = _compute_appearance_hist(frame, box, kpts)
        if hist is not None:
            self._appearance = hist

    def update(self, frame):
        H, W = frame.shape[:2]
        meas = lock_user_pose(frame, self.pose)
        if meas is not None and meas["kpts"] is not None:
            if self.box is None:
                # Acquisition phase: require N consecutive confirming frames
                if self._pending_box is not None and self._consistent(meas["box"], W):
                    self._pending_count += 1
                else:
                    self._pending_box = meas["box"]
                    self._pending_kpts = meas["kpts"]
                    self._pending_bar = meas["bar"]
                    self._pending_count = 1
                if self._pending_count >= self.acquire_confirm:
                    self.box = self._pending_box
                    self.kpts = self._pending_kpts
                    self.age = 0
                    self._update_appearance(frame, self.box, self.kpts)
                    self._pending_box = None
                    self._pending_count = 0
                    return {"box": self.box, "kpts": self.kpts, "bar": meas["bar"], "src": "acquire"}
                return None
            elif self.age > self.max_age:
                # Track stale -> re-acquire immediately
                self.box = meas["box"]
                self.kpts = meas["kpts"]
                self.age = 0
                self._update_appearance(frame, self.box, self.kpts)
                self._switch_candidate = None
                self._switch_count = 0
                return {"box": self.box, "kpts": self.kpts, "bar": meas["bar"], "src": " reacquire"}
            elif self._consistent(meas["box"], W):
                # Consistent with track -> accept and reset age
                self.box = meas["box"]
                self.kpts = meas["kpts"]
                self.age = 0
                self._update_appearance(frame, self.box, self.kpts)
                self._switch_candidate = None
                self._switch_count = 0
                return {"box": self.box, "kpts": self.kpts, "bar": meas["bar"], "src": "bar"}
            else:
                # Inconsistent with track -> potential switch candidate
                if self._switch_candidate is not None and self._consistent(meas["box"], W):
                    pass  # same candidate region
                self._switch_candidate = meas["box"]
                self._switch_count += 1
                if self._switch_count >= self.switch_confirm:
                    self.box = meas["box"]
                    self.kpts = meas["kpts"]
                    self.age = 0
                    self._update_appearance(frame, self.box, self.kpts)
                    self._switch_candidate = None
                    self._switch_count = 0
                    return {"box": self.box, "kpts": self.kpts, "bar": meas["bar"], "src": "switch"}
                # Not enough confirmations -> ignore, keep current track
        else:
            self._switch_count = max(0, self._switch_count - 1)

        # No usable bar this frame -> coast: follow nearest person to last box via ROI crop
        if self.box is not None and self.age < self.max_age:
            cx, cy = _center(self.box)
            bw, bh = self.box[2] - self.box[0], self.box[3] - self.box[1]
            cw, ch = int(max(260, bw * 2.2)), int(max(360, bh * 2.0))
            x0 = int(max(0, cx - cw / 2)); x1 = int(min(W, cx + cw / 2))
            y0 = int(max(0, cy - ch / 2)); y1 = int(min(H, cy + ch / 2))
            r = self.pose.predict(frame[y0:y1, x0:x1], verbose=False, conf=0.10)[0]
            if r.boxes is not None and len(r.boxes):
                xyxy = r.boxes.xyxy.cpu().numpy()
                cand = []
                for p in range(len(xyxy)):
                    bb = xyxy[p].copy(); bb[0] += x0; bb[2] += x0; bb[1] += y0; bb[3] += y0
                    cand.append(bb)
                # Score by distance + appearance match
                best_p, best_score = -1, 1e18
                for p in range(len(cand)):
                    dist = (_center(cand[p])[0] - cx) ** 2 + (_center(cand[p])[1] - cy) ** 2
                    if dist < best_score and self._consistent(cand[p], W):
                        best_score, best_p = dist, p
                if best_p >= 0:
                    bi = best_p
                    kp = r.keypoints.data.cpu().numpy()[bi].copy()
                    kp[:, 0] += x0; kp[:, 1] += y0
                    self.box, self.kpts, self.age = cand[bi], kp, self.age + 1
                    return {"box": self.box, "kpts": self.kpts, "bar": None, "src": "track"}
        self.age += 1
        return None
