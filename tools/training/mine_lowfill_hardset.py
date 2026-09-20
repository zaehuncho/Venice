#!/usr/bin/env python
"""Mine LOW-FILL meter frames the shipped 2K27 detector missed -> YOLO hard set.

Problem (measured 2026-09-03): meter2k27_n3_pill does not see the Arrow2 meter during
its first ~200-300 ms of rise (fill 0-30%). The framedump sessions record, per shot, a
run of `detected=0` frames (rejection detector_no_meter / roi_not_found) immediately
before the first `detected=1` frame -- and the meter IS visible on most of them.

Method (per shot = run of detected=1 frames in frames.csv):
  1. Take the first detected frame's box (frames.csv bbox_*; it spans the full outline,
     green cap to bottom chevron, with ~3 px padding).
  2. Build a masked grayscale template of that box. The MASK keeps the outline ring,
     the arrow top + green cap and the bottom chevron, and blanks the interior where
     the white fill level changes -- so the template is fill-invariant.
  3. Walk BACKWARDS frame by frame (the meter follows the shooter, so it moves) with a
     masked NCC search around the previous box; if the template match fails, fall back
     to a green-cap proposer (green blobs in the search window -> hypothesised box ->
     local NCC refine).
  4. VERIFY every propagated box with cv2 on the frame itself: a compact green cap in the
     top of the box, centred, not a stripe crossing it; and either a bright-neutral (white)
     column standing on the base (fill >= 3 px) or -- for 0 % frames, where the white still
     sits inside the bottom chevron -- the two silver outline RAILS (row-wise brighter than
     the track interior at the template's rail columns, searched +-4 px) plus NCC >= NCC_OUTLINE.
     The template box itself is re-cut to the rails (+-4 px) and the cap top (-3 px, 120 px
     tall @720p) so every label shares one tight geometry; if the template frame shows no
     measurable rails (bright parquet/grey floor) only white-column evidence is accepted.
     Stop the walk at the first frame that fails (the meter was not there yet).
     FINDING (2026-09-03): of 450 mined frames the shipped model misses only 32 offline, all
     meters over parquet floors -- the MyCourt misses are async-detector staleness.
  5. Also keep the DETECTED frames with low fill (frames.csv fill_pct < 35, verified the
     same way) as extra low-fill positives -- the box there is the pipeline's own.
  6. Negatives: gameplay frames with detected=0 (detector_no_meter / roi_not_found),
     no detected=1 frame and no "Physical shot epoch" (orion_native.log) within +-1.5 s,
     and a frame-wide green-cap+white-column guard that finds nothing.

Output (YOLO): <out>/images/{train,val,heldout}/, <out>/labels/..., manifest.csv.
Split is BY SHOT for the training sessions (val = model selection only) and the
--heldout sessions are kept out of training entirely (true held-out evaluation).
"""
from __future__ import annotations
import argparse, csv, glob, os, random, re, sys, datetime
import cv2, numpy as np

NO_DET_REJ = {"detector_no_meter", "roi_not_found", "shot_candidate_unverified",
              "no_cap_anchor", "cold_first_read_unproven", "capless_fake_lock"}
NEG_REJ = {"detector_no_meter", "roi_not_found"}

# thresholds (calibrated on session_20260903_151910, see --calib)
NCC_MIN = 0.45          # template match must clear this to be trusted at all
NCC_OUTLINE = 0.50      # outline-only acceptance (0 % fill, no white column yet; cap re-centred)
MAX_BACK = 12           # frames (~1.4 s at the ~9 fps framedump cadence)
SEARCH_X, SEARCH_Y = 120, 80
G_LO, G_HI = np.array((35, 70, 80), np.uint8), np.array((90, 255, 255), np.uint8)
W_LO, W_HI = np.array((0, 0, 220), np.uint8), np.array((179, 40, 255), np.uint8)


# ----------------------------------------------------------------------------- helpers
def read_frames(sess):
    rows = list(csv.DictReader(open(os.path.join(sess, "frames.csv"))))
    for r in rows:
        r["idx"] = int(r["idx"]); r["detected"] = int(r["detected"])
        r["t_wall"] = float(r["t_wall"]); r["fill_pct"] = float(r["fill_pct"])
        r["box"] = tuple(int(r[k]) for k in ("bbox_x", "bbox_y", "bbox_w", "bbox_h"))
        r["path"] = os.path.join(sess, f"f{r['idx']:05d}_{r['detected']}_raw.png")
    return rows


def shot_epochs(log_path):
    """UTC epoch seconds of every 'Physical shot epoch' line in orion_native.log."""
    out = []
    if not os.path.exists(log_path):
        return out
    pat = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z\s+Physical shot epoch")
    with open(log_path, "rb") as fh:
        for line in fh:
            m = pat.match(line.decode("utf-8", "replace"))
            if m:
                dt = datetime.datetime.fromisoformat(m.group(1)).replace(tzinfo=datetime.timezone.utc)
                out.append(dt.timestamp())
    return out


def make_template(gray, box, margin=2):
    x, y, w, h = box
    H, W = gray.shape
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
    tpl = gray[y0:y1, x0:x1].copy()
    th, tw = tpl.shape
    mask = np.full((th, tw), 255, np.uint8)
    # blank the interior where the fill level changes (keep ring, cap, chevron)
    mask[int(0.26 * th):int(0.86 * th), int(0.28 * tw):int(0.72 * tw)] = 0
    return tpl, mask, (x0 - x, y0 - y)


def ncc_at(gray, tpl, mask, origin, radius):
    """Best masked NCC in a window around a template origin; returns (score, origin)."""
    th, tw = tpl.shape
    H, W = gray.shape
    ox, oy = origin
    X0, Y0 = max(0, ox - radius[0]), max(0, oy - radius[1])
    X1, Y1 = min(W, ox + tw + radius[0]), min(H, oy + th + radius[1])
    reg = gray[Y0:Y1, X0:X1]
    if reg.shape[0] < th or reg.shape[1] < tw:
        return -1.0, origin
    res = cv2.matchTemplate(reg, tpl, cv2.TM_CCOEFF_NORMED, mask=mask)
    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return float(mx), (X0 + loc[0], Y0 + loc[1])


def _rail_rows(box, fill_top_frac):
    """Rows for the rail test: 0.28-0.62 h, but never below the top of the white fill."""
    x, y, w, h = box
    y0, y1 = y + int(0.28 * h), y + int(0.62 * h)
    if fill_top_frac is not None:
        y1 = min(y1, y + int(fill_top_frac * h) - 2)
    return y0, y1


def find_rails(gray, box, cap_cx=None, fill_top_frac=None, reach=8):
    """Column offsets (rel. to box x; may be < 0 or >= w) of the two silver outline rails
    from a frame where the box is known-good. Per-column MEDIAN over the rows (a white
    court line crossing the box only touches a few rows per column), searched in a band
    reaching `reach` px beyond the box (the pipeline box is often offset / over-wide by up
    to 10 px, so the rails are NOT tied to the box edges). A rail is a local PEAK (>= 8
    over the columns 2 px either side) and the pair must beat the track interior between
    them by >= 12 (a translucent track over a red court leaves only ~15-20 of contrast; a
    flat bright floor gives none, and no peak pair -> None -> white-fill-only evidence)."""
    x, y, w, h = box
    H, W = gray.shape
    y0, y1 = _rail_rows(box, fill_top_frac)
    if y1 - y0 < 8:
        return None
    X0, X1 = max(0, x - reach), min(W, x + w + reach)
    col = np.median(gray[y0:y1, X0:X1].astype(np.float32), axis=0)
    o = x - X0                                   # index of box column 0 inside `col`
    n = col.size
    sep_lo, sep_hi = int(round(14 * H / 720.0)), int(round(30 * H / 720.0))
    axis = None if cap_cx is None or cap_cx < 0 else o + float(cap_cx)
    peak = np.full(n, -1e9, np.float32)
    for c in range(2, n - 2):
        peak[c] = col[c] - max(col[c - 2], col[c + 2])
    best = None
    for l in range(2, n - sep_lo - 2):
        if peak[l] < 8:
            continue
        for r in range(l + sep_lo, min(n - 2, l + sep_hi + 1)):
            if peak[r] < 8 or (axis is not None and not (l < axis < r)):
                continue
            contrast = min(col[l], col[r]) - float(col[l + 3:r - 2].mean())
            if contrast < 12:
                continue
            sc = contrast + 0.5 * min(peak[l], peak[r])
            if best is None or sc > best[0]:
                best = (sc, l - o, r - o)
    if best is None:
        return None
    return best[1], best[2]


METER_H_720 = 120       # HUD meter box height @720p: pipeline boxes mode 119-121 across 5 sessions
CAP_TOP_PAD = 3         # rows above the first green cap pixel (labeller convention: apex - 2..3)


def normalize_box(box, rails, cap_top, H, pad=4):
    """Re-cut the box to the meter's own geometry: horizontally rails +- pad (outline plus a
    few px, the labeller convention), vertically from CAP_TOP_PAD above the green cap's first
    row with the constant HUD height. Returns (box, rails relative to the new box)."""
    x, y, w, h = box
    l, r = rails
    nh = int(round(METER_H_720 * H / 720.0))
    ny = y + int(cap_top) - CAP_TOP_PAD if cap_top is not None and cap_top >= 0 else y
    return (x + l - pad, ny, (r - l) + 2 * pad, nh), (pad, (r - l) + pad)


def lock_box(gray, hsv, box, rails, cap_top_ref, dx):
    """Snap a candidate box onto the meter: dx from the rail search, dy from the cap top."""
    x, y, w, h = box
    nb = (x + dx, y, w, h)
    v = verify(hsv, nb)
    if v and v["green_ok"] and v["cap_top"] >= 0 and cap_top_ref is not None:
        dy = int(v["cap_top"]) - int(cap_top_ref)
        if 0 < abs(dy) <= 6:
            nb2 = (nb[0], nb[1] + dy, w, h)
            v2 = verify(hsv, nb2)
            if v2 and v2["green_ok"]:
                nb, v = nb2, v2
    return nb, v


RAIL_MODE = os.environ.get("LOWFILL_RAIL_MODE", "one")     # inner | one | both
RAIL_MIN_FRAC = float(os.environ.get("LOWFILL_RAIL_MIN_FRAC", "0.5"))


def _rail_frac(gray, box, rails, y0, y1):
    x, y, w, h = box
    rl, rr = rails
    H, W = gray.shape
    xl0, xl1 = x + rl - 1, x + rl + 2
    xr0, xr1 = x + rr - 1, x + rr + 2
    if xl0 < 0 or xr1 > W or y0 < 0 or y1 > H or (xr0 - 2) - (xl1 + 2) < 3:
        return 0.0, 0.0, 0.0
    g = gray[y0:y1].astype(np.float32)
    rail_l = g[:, xl0:xl1].max(axis=1); rail_r = g[:, xr0:xr1].max(axis=1)
    inner = g[:, xl1 + 2:xr0 - 2].mean(axis=1)
    in_ok = (rail_l >= inner + 12) & (rail_r >= inner + 12)
    ol = (rail_l >= g[:, max(0, xl0 - 4):max(1, xl0 - 1)].mean(axis=1) + 8) if xl0 - 4 >= 0 else in_ok
    orr = (rail_r >= g[:, xr1 + 1:min(W, xr1 + 4)].mean(axis=1) + 8) if xr1 + 4 <= W else in_ok
    return float(in_ok.mean()), float((in_ok & (ol | orr)).mean()), float((in_ok & ol & orr).mean())


def rails_ok(gray, box, rails, fill_top_frac=None, min_frac=None, search=4):
    """Row-wise evidence of the two silver outline rails (rows 0.28-0.62 h, above the white
    fill), searched over horizontal shifts of +-`search` px (the cap re-centring is only
    good to a few px). Per row: both rails brighter than the track interior (+12)
    ("inner"), and brighter than the exterior (+8) on one ("one") / both ("both") sides.
    A green streak, a ball or HUD text has no rail pair at the template's separation; the
    empty Arrow2 outline does -- this is the 0 %-fill evidence.
    Returns (ok, frac_used, f_inner, f_one, f_both, dx); ok is None when too few rows sit
    above the fill (then the white column is the evidence)."""
    if rails is None:
        return False, 0.0, 0.0, 0.0, 0.0, 0
    min_frac = RAIL_MIN_FRAC if min_frac is None else min_frac
    y0, y1 = _rail_rows(box, fill_top_frac)
    if y1 - y0 < 8:
        return None, 1.0, 1.0, 1.0, 1.0, 0
    best = None
    for dx in sorted(range(-search, search + 1), key=abs):
        f_in, f_one, f_both = _rail_frac(gray, (box[0] + dx, box[1], box[2], box[3]), rails, y0, y1)
        frac = {"inner": f_in, "one": f_one, "both": f_both}[RAIL_MODE]
        if best is None or frac > best[1] + 1e-6:
            best = (dx, frac, f_in, f_one, f_both)
    dx, frac, f_in, f_one, f_both = best
    return frac >= min_frac, frac, f_in, f_one, f_both, dx


def fill_top(v):
    """Top of the white column as a fraction of the box height (None when no column)."""
    if not v or v["fill_h"] < 3 or v["fill_bottom"] < 0:
        return None
    return v["fill_bottom"] - v["fill_h"] / float(v["_h"])


def verify(hsv, box):
    """cv2 evidence that `box` holds an Arrow2 meter: green cap on top, white fill column."""
    x, y, w, h = box
    H, W = hsv.shape[:2]
    if w < 12 or h < 40 or x < 0 or y < 0 or x + w > W or y + h > H:
        return None
    crop = hsv[y:y + h, x:x + w]
    cx0, cx1 = int(0.15 * w), int(0.85 * w) + 1
    # --- green cap: top 30 % of the box, horizontally centred
    cap = cv2.inRange(crop[:int(0.30 * h), cx0:cx1], G_LO, G_HI)
    g_px = int(cap.sum() // 255)
    g_off = 99.0
    if g_px:
        m = cv2.moments(cap, binaryImage=True)
        g_off = abs((cx0 + m["m10"] / m["m00"]) - w / 2.0)
    green_ok = g_px >= 5 and g_off <= 0.30 * w
    g_cy = -1.0
    cap_top = -1
    if g_px:
        g_cy = m["m01"] / m["m00"]
        ys, xs = np.nonzero(cap)
        cap_top = int(ys.min())
        # a cap is a compact blob INSIDE the box: a background stripe / court decor crosses it
        if xs.min() == 0 and xs.max() == cap.shape[1] - 1:
            green_ok = False
        if (ys.max() - ys.min() + 1) > 0.28 * h:
            green_ok = False
    if green_ok:
        side = max(6, int(0.5 * w))
        L = hsv[y:y + int(0.30 * h), max(0, x - side):x]
        R = hsv[y:y + int(0.30 * h), x + w:min(W, x + w + side)]
        gl = cv2.inRange(L, G_LO, G_HI).mean() / 255.0 if L.size else 0.0
        gr = cv2.inRange(R, G_LO, G_HI).mean() / 255.0 if R.size else 0.0
        if gl > 0.12 and gr > 0.12:          # green on BOTH sides of the box = a stripe, not a cap
            green_ok = False
    # --- white fill column: rows in [0.22h, 0.98h) whose white width >= 30 % of the box
    fy0, fy1 = int(0.22 * h), int(0.98 * h)
    wm = cv2.inRange(crop[fy0:fy1, cx0:cx1], W_LO, W_HI)
    rw = (wm > 0).sum(axis=1)
    fill_rows = rw >= max(4, int(0.30 * w))
    fill_h, fill_bottom = 0, -1
    if fill_rows.any():
        # longest contiguous run
        best = (0, 0, 0); cur = 0; start = 0
        for i, f in enumerate(fill_rows):
            if f:
                if cur == 0:
                    start = i
                cur += 1
                if cur > best[0]:
                    best = (cur, start, i)
            else:
                cur = 0
        fill_h, _, last = best
        fill_bottom = (fy0 + last) / float(h)
    white_ok = fill_h >= 3 and fill_bottom >= 0.70
    # a drawn column has near-constant row width
    if white_ok:
        widths = rw[fill_rows]
        if widths.size >= 4 and float(np.std(widths) / max(np.mean(widths), 1e-6)) > 0.45:
            white_ok = False
    return dict(green_px=g_px, green_off=round(g_off, 1), fill_h=int(fill_h),
                fill_bottom=round(fill_bottom, 2), green_ok=green_ok, white_ok=white_ok,
                cap_cx=(cx0 + m["m10"] / m["m00"]) if g_px else -1.0, cap_cy=g_cy, _h=h,
                cap_top=cap_top)


def recenter_on_cap(hsv, gray, tpl, mask, off, box, cap_off, v):
    """Shift `box` so its green-cap centroid sits where it sat in the template frame
    (the cap is the meter's axis, so this tightens the label to ~1 px), then re-score."""
    if not v or not v["green_ok"] or v["cap_cx"] < 0:
        return box, None, v
    dx = int(round(v["cap_cx"] - cap_off[0]))
    dy = int(round(v["cap_cy"] - cap_off[1]))
    if abs(dx) > 12 or abs(dy) > 12 or (dx == 0 and dy == 0):
        return box, None, v
    nb = (box[0] + dx, box[1] + dy, box[2], box[3])
    nv = verify(hsv, nb)
    if not nv or not nv["green_ok"]:
        return box, None, v
    sc, o = ncc_at(gray, tpl, mask, (nb[0] + off[0], nb[1] + off[1]), (1, 1))
    fb = (o[0] - off[0], o[1] - off[1], box[2], box[3])
    return fb, sc, (verify(hsv, fb) or nv)


def green_cap_proposals(hsv, prev_box, radius):
    """Green blobs in the search window -> hypothesised boxes with the known geometry."""
    x, y, w, h = prev_box
    H, W = hsv.shape[:2]
    X0, Y0 = max(0, x - radius[0]), max(0, y - radius[1])
    X1, Y1 = min(W, x + w + radius[0]), min(H, y + h + radius[1])
    g = cv2.inRange(hsv[Y0:Y1, X0:X1], G_LO, G_HI)
    n, _, st, cen = cv2.connectedComponentsWithStats(g, 8)
    out = []
    for k in range(1, n):
        bx, by, bw, bh, a = st[k]
        if not (4 <= a <= 200 and 3 <= bw <= 22 and 2 <= bh <= 14):
            continue
        cx, cy = X0 + cen[k][0], Y0 + cen[k][1]
        # the cap centroid sits ~11 % down the box, centred
        out.append((int(round(cx - w / 2.0)), int(round(cy - 0.11 * h)), w, h))
    return out


def frame_has_meter_like(hsv, size):
    """Frame-wide guard for negatives: any green cap with a white column under it."""
    w, h = size
    H, W = hsv.shape[:2]
    g = cv2.inRange(hsv, G_LO, G_HI)
    n, _, st, cen = cv2.connectedComponentsWithStats(g, 8)
    for k in range(1, n):
        bx, by, bw, bh, a = st[k]
        if not (4 <= a <= 200 and 3 <= bw <= 22 and 2 <= bh <= 14):
            continue
        cx, cy = cen[k]
        box = (int(round(cx - w / 2.0)), int(round(cy - 0.11 * h)), w, h)
        box = (min(max(0, box[0]), W - w), min(max(0, box[1]), H - h), w, h)
        v = verify(hsv, box)
        if v and v["green_ok"] and v["white_ok"]:
            return True
    return False


def accept(v, score, r_ok, rails, cap_off=None):
    """Acceptance rule for a propagated box.
    always      : green cap, NCC >= NCC_MIN, and the cap sits where it sat in the template
                  box (<= 4 px) -- an offset box is a wrong label even when the meter is there
    rails known : rails present (or n/a because the white column is tall -> white needed)
                  + (white column or NCC >= NCC_OUTLINE)
    rails unknown: the white column is the only evidence accepted (a textured floor reaches
                  NCC 0.70 on its own; measured on session_20260903_151910)"""
    if v is None or score < NCC_MIN or not v["green_ok"]:
        return False
    if cap_off is not None and (abs(v["cap_cx"] - cap_off[0]) > 4 or abs(v["cap_cy"] - cap_off[1]) > 4):
        return False
    if rails is None or r_ok is None:
        return v["white_ok"]
    return r_ok and (v["white_ok"] or score >= NCC_OUTLINE)


# ----------------------------------------------------------------------------- mining
def mine_session(sess, epochs, stats, calib_rows=None, verbose=True, calib_fill=None):
    rows = read_frames(sess)
    tag = "s" + os.path.basename(sess).split("_")[-1]
    by_idx = {r["idx"]: r for r in rows}
    n = len(rows)
    # shots = runs of detected=1 (>= 2 frames)
    shots = []
    i = 0
    while i < n:
        if rows[i]["detected"] == 1:
            j = i
            while j < n and rows[j]["detected"] == 1:
                j += 1
            if j - i >= 2:
                shots.append((i, j))
            i = j
        else:
            i += 1
    pos, back_counts = [], []
    for si, (s, e) in enumerate(shots):
        first = rows[s]
        img = cv2.imread(first["path"])
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv0 = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        box0 = first["box"]
        v0 = verify(hsv0, box0)
        if not v0 or not v0["green_ok"]:
            stats["shot_template_rejected"] += 1
            continue
        rails = find_rails(gray, box0, v0["cap_cx"], fill_top(v0))
        if rails is not None:
            nb, nr = normalize_box(box0, rails, v0["cap_top"], img.shape[0])
            nv = verify(hsv0, nb)
            if nv and nv["green_ok"] and 12 <= nb[2] <= 60:
                box0, rails, v0 = nb, nr, nv
            else:
                rails = None
        # template + cap offset from the (normalised) box -- everything downstream is relative to it
        tpl, mask, off = make_template(gray, box0)
        cap_off = (v0["cap_cx"], v0["cap_cy"])      # cap centroid relative to the box origin
        cap_top0 = v0["cap_top"]
        r_ok0, r_frac0, fi0, fo0, fb0, _ = rails_ok(gray, box0, rails, fill_top(v0), search=0)
        if calib_rows is not None:
            calib_rows.append(dict(sess=tag, idx=first["idx"], k=0, ncc=1.0,
                                   accepted=2 if r_ok0 else (3 if r_ok0 is None else -1),
                                   x=box0[0], y=box0[1], w=box0[2], h=box0[3], path=first["path"],
                                   rail_frac=round(r_frac0, 2), f_in=round(fi0, 2), f_one=round(fo0, 2),
                                   f_both=round(fb0, 2), **v0))
        if r_ok0 is False:
            stats["template_rails_not_found"] += 1
            rails = None                            # walk-back must then show white fill or NCC >= 0.70
        prev_box = box0
        k = 0
        for f in range(s - 1, max(-1, s - 1 - MAX_BACK), -1):
            r = rows[f]
            if r["detected"] != 0 or r["rejection"] not in NO_DET_REJ:
                break
            im = cv2.imread(r["path"])
            if im is None:
                break
            g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            hs = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
            origin = (prev_box[0] + off[0], prev_box[1] + off[1])
            score, o = ncc_at(g, tpl, mask, origin, (SEARCH_X, SEARCH_Y))
            cand = (o[0] - off[0], o[1] - off[1], box0[2], box0[3])
            v = verify(hs, cand)
            cand, sc2, v = recenter_on_cap(hs, g, tpl, mask, off, cand, cap_off, v)
            if sc2 is not None:
                score = max(score, sc2)
            mode = "template"
            r_ok, r_frac, f_in, f_one, f_both, dx = rails_ok(g, cand, rails, fill_top(v))
            if r_ok and (dx != 0 or (v and v["cap_top"] >= 0 and v["cap_top"] != cap_top0)):
                cand, v = lock_box(g, hs, cand, rails, cap_top0, dx)
                r_ok, r_frac, f_in, f_one, f_both, _ = rails_ok(g, cand, rails, fill_top(v), search=0)
            ok = accept(v, score, r_ok, rails, cap_off)
            if not ok:
                # fall back: green-cap proposals, each refined by a local NCC search
                best = None
                for pb in green_cap_proposals(hs, prev_box, (SEARCH_X, SEARCH_Y)):
                    sc, oo = ncc_at(g, tpl, mask, (pb[0] + off[0], pb[1] + off[1]), (8, 8))
                    cb = (oo[0] - off[0], oo[1] - off[1], box0[2], box0[3])
                    vv = verify(hs, cb)
                    cb, sc2, vv = recenter_on_cap(hs, g, tpl, mask, off, cb, cap_off, vv)
                    if sc2 is not None:
                        sc = max(sc, sc2)
                    rk, rf, fi, fo, fb, ddx = rails_ok(g, cb, rails, fill_top(vv))
                    if rk and (ddx != 0 or (vv and vv["cap_top"] >= 0 and vv["cap_top"] != cap_top0)):
                        cb, vv = lock_box(g, hs, cb, rails, cap_top0, ddx)
                        rk, rf, fi, fo, fb, _ = rails_ok(g, cb, rails, fill_top(vv), search=0)
                    if accept(vv, sc, rk, rails, cap_off):
                        if best is None or sc > best[0]:
                            best = (sc, cb, vv, rf, fi, fo, fb)
                if best is None:
                    if calib_rows is not None:
                        calib_rows.append(dict(sess=tag, idx=r["idx"], k=k + 1, ncc=round(score, 3),
                                               accepted=0, x=cand[0], y=cand[1], w=cand[2], h=cand[3],
                                               path=r["path"], rail_frac=round(r_frac, 2), f_in=round(f_in, 2),
                                               f_one=round(f_one, 2), f_both=round(f_both, 2), **(v or {})))
                    break
                score, cand, v, r_frac, f_in, f_one, f_both = best
                mode = "greencap"
            k += 1
            pos.append(dict(session=tag, idx=r["idx"], path=r["path"], shot=f"{tag}_{si:03d}",
                            k_before=k, x=cand[0], y=cand[1], w=cand[2], h=cand[3],
                            ncc=round(score, 3), mode=mode, source="propagated", rail_frac=round(r_frac, 2),
                            old_detected=0, csv_fill=r["fill_pct"], **v))
            if calib_rows is not None:
                calib_rows.append(dict(sess=tag, idx=r["idx"], k=k, ncc=round(score, 3), accepted=1,
                                       x=cand[0], y=cand[1], w=cand[2], h=cand[3], path=r["path"],
                                       rail_frac=round(r_frac, 2), f_in=round(f_in, 2), f_one=round(f_one, 2),
                                       f_both=round(f_both, 2), **v))
            prev_box = cand
        back_counts.append(k)
        # detected low-fill frames of the same shot (the pipeline's own box, verified)
        for f in range(s, e):
            r = rows[f]
            if r["fill_pct"] <= 0.0:
                continue
            if r["fill_pct"] >= 35.0:
                if calib_fill is not None and (f - s) % 5 == 0:
                    im = cv2.imread(r["path"])
                    if im is not None:
                        v = verify(cv2.cvtColor(im, cv2.COLOR_BGR2HSV), r["box"])
                        if v and v["green_ok"] and v["white_ok"]:
                            calib_fill.append((v["fill_h"] / float(r["box"][3]), r["fill_pct"]))
                continue
            im = cv2.imread(r["path"])
            if im is None:
                continue
            hs = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
            v = verify(hs, r["box"])
            if v and v["green_ok"] and v["white_ok"]:
                pos.append(dict(session=tag, idx=r["idx"], path=r["path"], shot=f"{tag}_{si:03d}",
                                k_before=0, x=r["box"][0], y=r["box"][1], w=r["box"][2], h=r["box"][3],
                                ncc=1.0, mode="pipeline", source="detected_lowfill", rail_frac=-1.0,
                                old_detected=1, csv_fill=r["fill_pct"], **v))
    # negatives
    det_t = np.array([r["t_wall"] for r in rows if r["detected"] == 1])
    ep_t = np.array([t for t in epochs if rows[0]["t_wall"] - 5 <= t <= rows[-1]["t_wall"] + 5])
    pos_idx = {p["idx"] for p in pos}
    sizes = [p["w"] for p in pos] or [28]
    sz = (int(np.median(sizes)), int(np.median([p["h"] for p in pos] or [120])))
    negs, guard_dropped = [], 0
    for r in rows:
        if r["detected"] != 0 or r["rejection"] not in NEG_REJ or r["idx"] in pos_idx:
            continue
        t = r["t_wall"]
        if det_t.size and np.min(np.abs(det_t - t)) <= 1.5:
            continue
        if ep_t.size and np.min(np.abs(ep_t - t)) <= 1.5:
            continue
        im = cv2.imread(r["path"])
        if im is None:
            continue
        if frame_has_meter_like(cv2.cvtColor(im, cv2.COLOR_BGR2HSV), sz):
            guard_dropped += 1
            continue
        negs.append(dict(session=tag, idx=r["idx"], path=r["path"], shot="", k_before=-1,
                         x=0, y=0, w=0, h=0, ncc=0.0, mode="", source="negative", rail_frac=-1.0,
                         old_detected=0, csv_fill=0.0, green_px=0, green_off=0, fill_h=0,
                         fill_bottom=0, green_ok=False, white_ok=False))
    if verbose:
        bc = np.array(back_counts) if back_counts else np.array([0])
        print(f"{tag}: shots={len(shots)} propagated={sum(1 for p in pos if p['source']=='propagated')} "
              f"(per shot mean {bc.mean():.2f}, max {bc.max()}) detected_lowfill="
              f"{sum(1 for p in pos if p['source']=='detected_lowfill')} negatives={len(negs)} "
              f"(guard dropped {guard_dropped}, epochs in span {ep_t.size})", flush=True)
    return pos, negs


def calibrate_fill(all_pos, extra):
    """Map cv2 fill_h/h -> frames.csv fill_pct with a linear fit on DETECTED frames of ALL fills
    (the low-fill frames alone are range-restricted: below ~12 % the white sits inside the
    bottom chevron and fill_h is 0)."""
    pts = [(p["fill_h"] / float(p["h"]), p["csv_fill"]) for p in all_pos
           if p["source"] == "detected_lowfill" and p["fill_h"] >= 3] + list(extra)
    xs = np.array([q[0] for q in pts]); ys = np.array([q[1] for q in pts])
    if xs.size < 10:
        return 100.0 / 0.72, 0.0
    a, b = np.polyfit(xs, ys, 1)
    resid = ys - (a * xs + b)
    print(f"fill calibration: fill_pct = {a:.1f} * fill_h/h + {b:.1f}   (n={xs.size}, "
          f"resid median abs {np.median(np.abs(resid)):.2f} pp)", flush=True)
    return float(a), float(b)


def write_split(items, out, split, manifest, a, b):
    im_dir = os.path.join(out, "images", split); lb_dir = os.path.join(out, "labels", split)
    os.makedirs(im_dir, exist_ok=True); os.makedirs(lb_dir, exist_ok=True)
    for p in items:
        stem = f"{p['session']}_{p['idx']:05d}"
        img = cv2.imread(p["path"])
        H, W = img.shape[:2]
        cv2.imwrite(os.path.join(im_dir, stem + ".png"), img)
        with open(os.path.join(lb_dir, stem + ".txt"), "w") as fh:
            if p["source"] != "negative":
                cx, cy = (p["x"] + p["w"] / 2.0) / W, (p["y"] + p["h"] / 2.0) / H
                fh.write(f"0 {cx:.6f} {cy:.6f} {p['w'] / W:.6f} {p['h'] / H:.6f}\n")
        fill_est = max(0.0, a * p["fill_h"] / float(p["h"]) + b) if p["source"] != "negative" else -1.0
        manifest.append(dict(split=split, image=stem + ".png", session=p["session"], idx=p["idx"],
                             source_frame=p["path"], shot=p["shot"], source=p["source"],
                             k_before_first_det=p["k_before"], x=p["x"], y=p["y"], w=p["w"], h=p["h"],
                             fill_est=round(fill_est, 1), csv_fill=p["csv_fill"], ncc=p["ncc"],
                             rail_frac=p["rail_frac"], mode=p["mode"], green_px=p["green_px"],
                             fill_h=p["fill_h"], old_detected=p["old_detected"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-sessions", nargs="+", default=[
        "logs/diagnostics/framedump/session_20260903_133124",
        "logs/diagnostics/framedump/session_20260903_145810",
        "logs/diagnostics/framedump/session_20260903_151910"])
    ap.add_argument("--heldout-sessions", nargs="+", default=[
        "logs/diagnostics/framedump/session_20260903_100538"])
    ap.add_argument("--log", default="logs/orion_native.log")
    ap.add_argument("--out", default="runs/detect/logs/diagnostics/meter_train/lowfill_hardset")
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--neg-ratio", type=float, default=0.5, help="train negatives per positive")
    ap.add_argument("--calib", action="store_true", help="print NCC/verify stats, write nothing")
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    random.seed(a.seed)
    epochs = shot_epochs(a.log)
    print(f"shot epochs in log: {len(epochs)}", flush=True)
    stats = {"shot_template_rejected": 0, "template_rails_not_found": 0}
    calib = [] if a.calib else None
    calib_fill = []
    train_pos, train_neg, held_pos, held_neg = [], [], [], []
    for s in a.train_sessions:
        p, ng = mine_session(s, epochs, stats, calib, calib_fill=calib_fill)
        train_pos += p; train_neg += ng
    for s in a.heldout_sessions:
        p, ng = mine_session(s, epochs, stats, calib, calib_fill=calib_fill)
        held_pos += p; held_neg += ng
    print("stats:", stats, flush=True)
    if a.calib:
        keys = []
        for c in calib:
            for k in c:
                if k not in keys:
                    keys.append(k)
        with open(os.path.join(os.path.dirname(a.out), "lowfill_calib.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, restval=""); w.writeheader(); w.writerows(calib)
        acc = [c for c in calib if c["accepted"] == 1]; rej = [c for c in calib if c["accepted"] == 0]
        print(f"accepted {len(acc)} ncc p10/p50 {np.percentile([c['ncc'] for c in acc],10):.2f}/"
              f"{np.median([c['ncc'] for c in acc]):.2f}; rejected {len(rej)} ncc p50 "
              f"{np.median([c['ncc'] for c in rej]) if rej else 0:.2f}")
        return 0
    fa, fb = calibrate_fill(train_pos + held_pos, calib_fill)
    # by-shot split of the training sessions
    shots = sorted({p["shot"] for p in train_pos})
    random.shuffle(shots)
    n_val = max(1, int(round(len(shots) * a.val_frac)))
    val_shots = set(shots[:n_val])
    val_pos = [p for p in train_pos if p["shot"] in val_shots]
    tr_pos = [p for p in train_pos if p["shot"] not in val_shots]
    random.shuffle(train_neg)
    n_tr_neg = int(len(tr_pos) * a.neg_ratio)
    n_va_neg = int(len(val_pos) * a.neg_ratio)
    tr_neg, va_neg = train_neg[:n_tr_neg], train_neg[n_tr_neg:n_tr_neg + n_va_neg]
    heldout_negs_from_train = train_neg[n_tr_neg + n_va_neg:]   # never trained on -> FP eval pool
    manifest = []
    write_split(tr_pos + tr_neg, a.out, "train", manifest, fa, fb)
    write_split(val_pos + va_neg, a.out, "val", manifest, fa, fb)
    write_split(held_pos + held_neg, a.out, "heldout", manifest, fa, fb)
    write_split(heldout_negs_from_train, a.out, "heldout_neg", manifest, fa, fb)
    with open(os.path.join(a.out, "manifest.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(manifest[0].keys())); w.writeheader(); w.writerows(manifest)
    for split in ("train", "val", "heldout", "heldout_neg"):
        ms = [m for m in manifest if m["split"] == split]
        npos = sum(1 for m in ms if m["source"] != "negative")
        print(f"{split}: {len(ms)} images, {npos} positives "
              f"({sum(1 for m in ms if m['source']=='propagated')} propagated), "
              f"{len(ms) - npos} negatives", flush=True)
    fills = np.array([m["fill_est"] for m in manifest if m["source"] == "propagated"])
    if fills.size:
        edges = [0, 10, 20, 30, 50, 101]
        hist = np.histogram(fills, bins=edges)[0]
        print("propagated fill_est distribution:", {f"{edges[i]}-{edges[i+1]}": int(hist[i]) for i in range(len(hist))})
    print("dataset ready:", os.path.abspath(a.out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
