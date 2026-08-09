#!/usr/bin/env python3
"""User-player lock via the under-player STAMINA BAR (yellow-left / blue-right), meter-independent.

The stamina bar sits under the user's controlled player (only the user has it), so it's the primary
no-meter player-ID. Because the pose model misses small players in dark Park/Rec scenes, we use the
bar to ROI-CROP a zoomed region above it and run pose on the crop (where the player is large) -> the
bar solves BOTH the lock and the detection. See docs/ANIMATION_ANCHOR.md.

Reusable API: find_stamina_bar(frame) -> (x,y,w,h)|None ; lock_user_pose(frame, pose) -> dict|None.
"""
from __future__ import annotations

import cv2
import numpy as np

# yellow (left) and blue/cyan (right) HSV bands for the bar
_Y_LO, _Y_HI = (15, 80, 120), (40, 255, 255)
_B_LO, _B_HI = (90, 80, 90), (128, 255, 255)

# optional learned bar detector (models/orion_bar_n.pt); falls back to HSV when absent
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
    """Return the user's stamina-bar bbox (x,y,w,h) or None. Uses the learned detector when available
    (more robust on wood floors / vertical bars / motion), else the HSV yellow+blue heuristic."""
    m = _bar_model()
    if m is not None:
        r = m.predict(frame, verbose=False, conf=conf)[0]
        if r.boxes is not None and len(r.boxes):
            # Filter by plausible bar size and pick highest-confidence valid box
            H, W = frame.shape[:2]
            candidates = []
            for bi in range(len(r.boxes)):
                x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                bw, bh = x2 - x1, y2 - y1
                # Reject implausible sizes (too large = UI element, too small = noise)
                if bw < 10 or bw > 250 or bh < 10 or bh > 250:
                    continue
                ar = max(bw, bh) / max(min(bw, bh), 1)
                if ar < 1.5:  # bar must be elongated (not square)
                    continue
                c = float(r.boxes.conf.cpu().numpy()[bi])
                candidates.append((c, bi, x1, y1, bw, bh))
            if candidates:
                candidates.sort(key=lambda c: -c[0])  # highest confidence first
                _, _, x1, y1, bw, bh = candidates[0]
                return (int(x1), int(y1), int(bw), int(bh))
        # Model found nothing valid — fall through to HSV fallback below
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    ym = cv2.inRange(hsv, _Y_LO, _Y_HI)
    bm = cv2.inRange(hsv, _B_LO, _B_HI)
    # symmetric dilation connects the yellow+blue segments whether the bar is HORIZONTAL
    # (yellow-left/blue-right, under feet) or VERTICAL (yellow-bottom/blue-top, beside the player).
    comb = cv2.dilate(((ym | bm) > 0).astype("uint8") * 255,
                      cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    cnts = cv2.findContours(comb, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    best, best_score = None, 0
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        horiz = w >= 18 and 2 <= h <= 32 and w >= 2.0 * h      # wide + thin
        vert = h >= 18 and 2 <= w <= 32 and h >= 2.0 * w       # tall + thin
        if not (horiz or vert):
            continue
        if exclude_hud:
            if y < H * 0.15 or y > H * 0.92:                   # top scoreboard + bottom stats HUD
                continue
            if x > W * 0.74 and y > H * 0.74:                   # bottom-right scoreboard
                continue
        ysum = int(ym[y:y + h, x:x + w].sum())
        bsum = int(bm[y:y + h, x:x + w].sum())
        if ysum == 0 or bsum == 0:                             # MUST contain both colors
            continue
        score = min(ysum, bsum)
        if score > best_score:
            best_score, best = score, (x, y, w, h)
    return best


def player_roi(frame, bar):
    """Crop a region centered horizontally on the bar (the user can be ABOVE, LEFT, or RIGHT of it),
    tall enough to capture the WHOLE player. The adjacency rule then picks the correct nearby player.
    (crop, x0, y0)."""
    H, W = frame.shape[:2]
    x, y, w, h = bar
    cx = x + w / 2.0
    cw = int(max(420, w * 6.0))         # SYMMETRIC: left AND right of the bar
    ch = int(max(360, w * 5.0))         # tall enough for the full player above the bar
    x0 = int(max(0, cx - cw / 2)); x1 = int(min(W, cx + cw / 2))
    y1 = int(min(H, y + h + 0.5 * w))   # a bit below the bar
    y0 = int(max(0, y1 - ch))           # extend UP for the full body
    return frame[y0:y1, x0:x1], x0, y0


def lock_user_pose(frame, pose, conf: float = 0.10):
    """Find the bar, crop above it, run pose on the crop. Returns dict(bar, kpts[17,3] full-frame,
    box) or None. kpts/box are mapped back to full-frame coords."""
    bar = find_stamina_bar(frame)
    if bar is None:
        return None
    crop, x0, y0 = player_roi(frame, bar)
    if crop.size == 0:
        return None
    r = pose.predict(crop, verbose=False, conf=conf)[0]
    if r.boxes is None or not len(r.boxes):
        return {"bar": bar, "kpts": None, "box": None}
    # the user = the player whose box the bar sits ADJACENT to (just BELOW the feet, or just past the
    # RIGHT edge). The bar is a separate UI element next to the body, not inside it -> use the distance
    # from the bar to the NEAREST POINT of each player's box, with a cap so far-off crowd players are
    # rejected. Nearest within the cap wins.
    xyxy = r.boxes.xyxy.cpu().numpy()
    bx, by, bw, bh = bar
    bar_cx, bar_cy = bx + bw / 2.0, by + bh / 2.0
    cap = (max(bw, 40) * 2.5) ** 2
    b, best_d = -1, 1e18
    for p in range(len(xyxy)):
        X1, Y1, X2, Y2 = xyxy[p, 0] + x0, xyxy[p, 1] + y0, xyxy[p, 2] + x0, xyxy[p, 3] + y0
        if bar_cy < Y1 - 0.25 * (Y2 - Y1):                  # bar above the head = not this player's
            continue
        qx = min(max(bar_cx, X1), X2); qy = min(max(bar_cy, Y1), Y2)   # nearest box point
        d = (bar_cx - qx) ** 2 + (bar_cy - qy) ** 2
        if d < best_d and d < cap:
            best_d, b = d, p
    if b < 0:
        return {"bar": bar, "kpts": None, "box": None}     # no player adjacent to this bar -> no clean lock
    kp = r.keypoints.data.cpu().numpy()[b].copy()           # (17,3) x,y,conf in crop coords
    kp[:, 0] += x0; kp[:, 1] += y0
    bb = r.boxes.xyxy.cpu().numpy()[b].copy()
    bb[0] += x0; bb[2] += x0; bb[1] += y0; bb[3] += y0
    return {"bar": bar, "kpts": kp, "box": bb}


def _center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


class StaminaTracker:
    """Temporal lock: acquire the user via the stamina bar, then FOLLOW that player by pose-box
    continuity through frames where the bar drops (e.g. mid-jump on a wood floor). Also rejects
    transient WRONG-bar picks (a measurement inconsistent with the established track is ignored while
    the track is fresh). Built for the no-meter calibration so a whole shot stays locked end-to-end."""

    def __init__(self, pose, max_age: int = 12, gate_frac: float = 0.16):
        self.pose = pose
        self.max_age = max_age      # frames to coast on continuity before declaring lost
        self.gate = gate_frac       # max center jump (fraction of frame width) to accept a match
        self.box = None
        self.kpts = None
        self.age = 10 ** 9

    def _consistent(self, b, W):
        if self.box is None:
            return False
        (cx, cy), (px, py) = _center(b), _center(self.box)
        return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 < self.gate * W

    def update(self, frame):
        H, W = frame.shape[:2]
        meas = lock_user_pose(frame, self.pose)
        if meas is not None and meas["kpts"] is not None:
            # accept the bar measurement if it's a fresh acquire, consistent with the track, or the
            # track has gone stale; otherwise it's a likely wrong-bar pick -> ignore it.
            if self.box is None or self.age > self.max_age or self._consistent(meas["box"], W):
                self.box, self.kpts, self.age = meas["box"], meas["kpts"], 0
                return {"box": self.box, "kpts": self.kpts, "bar": meas["bar"], "src": "bar"}
        # no usable bar this frame -> coast: follow the nearest person to the last box via an ROI crop
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
                bi = min(range(len(cand)),
                         key=lambda p: (_center(cand[p])[0] - cx) ** 2 + (_center(cand[p])[1] - cy) ** 2)
                if self._consistent(cand[bi], W):
                    kp = r.keypoints.data.cpu().numpy()[bi].copy()
                    kp[:, 0] += x0; kp[:, 1] += y0
                    self.box, self.kpts, self.age = cand[bi], kp, self.age + 1
                    return {"box": self.box, "kpts": self.kpts, "bar": None, "src": "track"}
        self.age += 1
        return None
