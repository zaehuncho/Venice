"""Player-anchor feasibility probe: is the shot meter at a CONSISTENT offset from the
controlled player (v9 'Player'/'Stamina' boxes)? If yes, anchoring the meter search to the
player kills the false-lock problem. Ground-truths the meter with green-led (100% precision).

  C:/Python314/python.exe tools/diagnostics/player_anchor_probe.py
"""
import sys, glob
import cv2, numpy as np
from ultralytics import YOLO

FRAMES = sorted(glob.glob("logs/diagnostics/framedump/f*_0_raw.png"))
M = YOLO("models/orion_player_detect_v9.pt")


def green_led(img):
    """100%-precision meter locator: neon-green make-window with red bar directly below."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV); H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    h, w = img.shape[:2]
    yt, yb = int(h * 0.15), int(h * 0.98)
    green = (((H >= 40) & (H <= 75) & (S >= 90) & (V >= 110))).astype(np.uint8); green[:yt] = 0; green[yb:] = 0
    if int(green.sum()) < 2: return None
    red = ((((H >= 160) | (H <= 14)) & (S >= 110) & (V >= 85))).astype(np.uint8); red[:yt] = 0; red[yb:] = 0
    g = cv2.dilate(green * 255, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)
    cnts, _ = cv2.findContours(g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None; bs = -1
    for cc in cnts:
        gx, gy, gw, gh = cv2.boundingRect(cc)
        if gw > 40 or gh > 40 or gw * gh < 2: continue
        cxg = gx + gw // 2
        colr = red[gy:min(h, gy + 170), max(0, cxg - 7):cxg + 8]
        ys = np.flatnonzero(colr.any(axis=1))
        if int(colr.sum()) < 6 or ys.size < 3: continue
        if ys.size > bs: bs = ys.size; best = (cxg, gy + int(ys[0]), gy + int(ys[-1]))
    return best


def stats(name, arr):
    if not arr:
        print(f"  {name}: n=0"); return
    a = np.array(arr, float)
    print(f"  {name}: n={len(arr)}  dx mean={a[:,0].mean():6.1f} std={a[:,0].std():5.1f}  "
          f"dy mean={a[:,1].mean():6.1f} std={a[:,1].std():5.1f}  (low std = consistent = anchorable)")


off_player, off_stam = [], []
n_meter = n_player = n_stam = 0
for fp in FRAMES:
    img = cv2.imread(fp)
    if img is None: continue
    gl = green_led(img)
    if gl is None: continue
    n_meter += 1
    mx, mtop, mbot = gl
    r = M(fp, verbose=False, conf=0.30)[0]
    players = [b for b in r.boxes if int(b.cls) == 0]
    stams = [b for b in r.boxes if int(b.cls) == 1]
    if stams:
        n_stam += 1
        b = max(stams, key=lambda b: float(b.conf))
        sx1, sy1, sx2, sy2 = b.xyxy[0].tolist()
        off_stam.append((mx - (sx1 + sx2) / 2.0, mtop - (sy1 + sy2) / 2.0))
        # shooter = the Player box nearest the (controlled) stamina bar
        if players:
            scx, scy = (sx1 + sx2) / 2.0, (sy1 + sy2) / 2.0
            p = min(players, key=lambda b: abs((b.xyxy[0][0] + b.xyxy[0][2]) / 2 - scx))
            px1, py1, px2, py2 = p.xyxy[0].tolist()
            off_player.append((mx - (px1 + px2) / 2.0, mtop - (py1 + py2) / 2.0))
            n_player += 1
    elif players:
        # no stamina this frame: fall back to highest-conf player
        p = max(players, key=lambda b: float(b.conf))
        px1, py1, px2, py2 = p.xyxy[0].tolist()
        off_player.append((mx - (px1 + px2) / 2.0, mtop - (py1 + py2) / 2.0)); n_player += 1

print(f"meter frames (green-led GT)={n_meter}  with Stamina box={n_stam}  with Player box={n_player}")
print("Offset of meter-top from the controlled-player anchors:")
stats("meter vs STAMINA box-center ", off_stam)
stats("meter vs PLAYER  box-center ", off_player)
