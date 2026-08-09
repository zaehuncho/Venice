"""Auto-label the shot meter for training a YOLO meter detector — WITHOUT manual annotation.

How it works (validated 2026-06-28): green-led (neon make-window over red = 100% precision) anchors
the meter at high fill; from each anchor we TRACK the red column frame-to-frame (motion-following,
contiguous run) backward + forward through the whole shot, so the meter is labeled at EVERY fill —
including the low-fill early-rise frames green-led can't see on its own. A fixed-x column fails on
fades (the meter slides and the column catches the player/jersey/UI); following the motion fixes it.

Output: YOLO-format labels (one class: meter) + a visual validation strip. Run per captured session,
then pool the labels across MANY sessions/shot-types to train a model that generalizes.

  C:/Python314/python.exe tools/diagnostics/meter_autolabel.py <framedump_glob> <out_dir>

NOTE: data is the bottleneck, not the method — one session of one shot type overfits. Capture varied
sessions (all shot types, both meter colours) and pool before training.
"""
import sys, glob, os
import cv2, numpy as np

GLOB = sys.argv[1] if len(sys.argv) > 1 else "logs/diagnostics/framedump/f*_0_raw.png"
OUT = sys.argv[2] if len(sys.argv) > 2 else "datasets/meter_autolabel"
FRAMES = sorted(glob.glob(GLOB))


def red_mask(img, loose=False):
    # COLOR-AGNOSTIC fill mask: the meter fill is RED (H~0/179) on some batches and PURPLE/MAGENTA
    # (H~130-165) on others (the game's meter-colour setting varies). The GREEN make-window anchor
    # (green_x) is colour-invariant and is what makes this meter-specific, so we can widen the hue band
    # to cover red..purple without labelling colour-only distractors — geometry + play-band + the green
    # anchor reject a purple court / UI blob (it has no green window above it).
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV); H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    h, w = img.shape[:2]; yt, yb = int(h * 0.15), int(h * 0.98)
    smin, vmin = (90, 60) if loose else (110, 85)   # loose grabs the dimmer lower fill for full boxes
    warm = ((H >= 160) | (H <= 14))                 # red / magenta-red
    purple = ((H >= 130) & (H < 160))               # purple / violet meter fill
    r = (((warm | purple) & (S >= smin) & (V >= vmin))).astype(np.uint8); r[:yt] = 0; r[yb:] = 0
    return r


def green_x(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV); H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    h, w = img.shape[:2]; yt, yb = int(h * 0.15), int(h * 0.98)
    g = (((H >= 40) & (H <= 75) & (S >= 90) & (V >= 110))).astype(np.uint8); g[:yt] = 0; g[yb:] = 0
    r = red_mask(img)
    gg = cv2.dilate(g * 255, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)
    cnts, _ = cv2.findContours(gg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None; bs = -1
    for cc in cnts:
        gx, gy, gw, gh = cv2.boundingRect(cc)
        if gw > 40 or gh > 40 or gw * gh < 2: continue
        cxg = gx + gw // 2; colr = r[gy:min(h, gy + 170), max(0, cxg - 7):cxg + 8]
        ys = np.flatnonzero(colr.any(axis=1))
        if int(colr.sum()) < 6 or ys.size < 3: continue
        if ys.size > bs: bs = ys.size; best = cxg
    return best


def runs(colbool):
    out = []; s = None
    for y, v in enumerate(colbool):
        if v and s is None: s = y
        elif not v and s is not None: out.append((s, y - 1)); s = None
    if s is not None: out.append((s, len(colbool) - 1))
    # close 2px gaps so a thin AA break doesn't split the bar
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] <= 2: merged[-1] = (merged[-1][0], b)
        else: merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= 5]


def best_col(r, x0, prev, xr=13):
    best = None; bsc = -1e9
    for x in range(max(5, x0 - xr), min(r.shape[1] - 5, x0 + xr + 1)):
        col = (r[:, x - 2:x + 3].sum(axis=1) >= 2)
        for a, b in runs(col):
            if prev:
                ov = max(0, min(b, prev[1]) - max(a, prev[0]))
                sc = ov * 2 + (b - a) - 0.5 * abs(((a + b) // 2) - ((prev[0] + prev[1]) // 2))
            else:
                sc = b - a
            if sc > bsc: bsc = sc; best = (x, a, b)
    return best


def red_width(r, x, top, bot, floor=6, max_scan=48):
    """Measure the ACTUAL horizontal extent of the red bar around the tracked column x by walking
    contiguous red-mask columns at the bar's vertical mid. Replaces the old height-derived fake
    width (w = h//8+8 — never measured). Returns (x_left, x_right, width_px) with width >= floor.
    Bridges 1px anti-alias gaps; caps runaway bleed (a jersey/UI blob touching the bar) at the bar
    height, since the meter is always taller than wide."""
    h, w = r.shape[:2]
    x = int(max(0, min(w - 1, x)))
    ym = (top + bot) // 2
    band = r[max(top, ym - 3):min(bot + 1, ym + 4), :]          # ~7-row band at mid-bar
    if band.size == 0:
        return x, x, floor
    colhas = band.sum(axis=0) >= 2                               # column 'red' if >=2 band px lit
    x0 = x
    if not colhas[x0]:                                           # tracker can sit on the AA edge
        for d in (1, -1, 2, -2):
            if 0 <= x0 + d < w and colhas[x0 + d]:
                x0 = x0 + d
                break
        else:
            return x, x, floor                                   # off the mask -> floor
    L = R = x0
    gap = 0
    while L - 1 >= max(0, x0 - max_scan):
        if colhas[L - 1]:
            L -= 1; gap = 0
        elif gap < 1 and L - 2 >= 0 and colhas[L - 2]:           # bridge a single AA gap column
            L -= 2; gap += 1
        else:
            break
    gap = 0
    while R + 1 <= min(w - 1, x0 + max_scan):
        if colhas[R + 1]:
            R += 1; gap = 0
        elif gap < 1 and R + 2 <= w - 1 and colhas[R + 2]:
            R += 2; gap += 1
        else:
            break
    cap = max(floor, bot - top + 1)                              # never wider than tall
    if R - L + 1 > cap:
        L = max(L, x0 - cap // 2); R = min(R, L + cap - 1)
    return L, R, max(floor, R - L + 1)


def main():
    gx = []
    for fp in FRAMES:
        im = cv2.imread(fp); gx.append(green_x(im) if im is not None else None)
    hit = [i for i, v in enumerate(gx) if v is not None]
    bursts = []; cur = []
    for i in hit:
        if cur and i - cur[-1] > 12: bursts.append(cur); cur = []
        cur.append(i)
    if cur: bursts.append(cur)
    bursts = [b for b in bursts if len(b) >= 3]

    labels = {}   # frame_idx -> (cx, cy, w, h) px, on the LOOSE (full) mask; w = MEASURED red extent
    for b in bursts:
        anchor = b[len(b) // 2]; ax = gx[anchor]
        seed_rm = red_mask(cv2.imread(FRAMES[anchor]), loose=True)
        seed = best_col(seed_rm, ax, None)
        if seed is None: continue
        for direction in (1, -1):
            x, top, bot = seed; i = anchor
            while True:
                i += direction
                if i < b[0] - 6 or i > b[-1] + 6 or i < 0 or i >= len(FRAMES): break
                img = cv2.imread(FRAMES[i])
                if img is None: break
                rm = red_mask(img, loose=True)
                nb = best_col(rm, x, (top, bot))
                if nb is None or abs(nb[0] - x) > 16 or (nb[2] - nb[1]) < 6: break
                x, top, bot = nb
                lft, rgt, wpx = red_width(rm, x, top, bot)
                labels[i] = ((lft + rgt) // 2, (top + bot) // 2, wpx, bot - top + 1)
        x, top, bot = seed
        lft, rgt, wpx = red_width(seed_rm, x, top, bot)
        labels[anchor] = ((lft + rgt) // 2, (top + bot) // 2, wpx, bot - top + 1)

    os.makedirs(os.path.join(OUT, "images"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "labels"), exist_ok=True)
    n = 0
    for i, (cx, cy, w, h) in labels.items():
        img = cv2.imread(FRAMES[i]); H, W = img.shape[:2]
        stem = os.path.splitext(os.path.basename(FRAMES[i]))[0]
        cv2.imwrite(os.path.join(OUT, "images", stem + ".png"), img)
        with open(os.path.join(OUT, "labels", stem + ".txt"), "w") as f:
            f.write(f"0 {cx/W:.6f} {cy/H:.6f} {w/W:.6f} {h/H:.6f}\n")
        n += 1
    print(f"bursts={len(bursts)} auto-labeled={n} frames -> {OUT}")
    print("pool MANY sessions here, then: yolo detect train model=yolov8n.pt data=<yaml> epochs=100 imgsz=1280")


if __name__ == "__main__":
    main()
