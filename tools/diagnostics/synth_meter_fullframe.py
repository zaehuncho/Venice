#!/usr/bin/env python3
"""Full-FRAME synthetic generator for the meter LOCATOR (YOLO, 1 class). Composites the proven
render_meter() meter onto REAL 1080p framedump backgrounds at varied position/scale/colour, emitting
YOLO bbox labels. HARD NEGATIVES = real distractor backgrounds (park lobby arcade-red, UI banners,
even the mixed-in EA FC frames) with NO meter and an empty label — this is what teaches the locator to
REJECT random red objects (the ship blocker), which HSV colour alone cannot.

WHY a locator: the live classical park detector false-locks on lobby red props (camera pans break the
static-red EMA) and misses the real meter when it's player-attached off to the side (meter_poc shows it
at the extreme edge). A trained locator finds the meter ANYWHERE and rejects distractors by learned shape.

OUTPUT (YOLO layout): datasets/meter_fullframe_synth/images/*.png + labels/*.txt (class 0 = meter,
normalized cx,cy,w,h) + train.txt/val.txt (ABSOLUTE paths, 90/10) + meter.yaml.

DEGRADATION PROFILES (--profile usb|hdmi|mix, default mix): the live rig is a 1080p HDMI capture
card, not the $20 USB card cheap_card_degrade models — that mismatch was a sim-to-real gap driver
(trained locator ~0% live recall). hdmi_degrade adds limited/full range squeeze + TRUE 4:2:0 chroma
subsampling; ~30% of positives also get a horizontal motion blur on the meter region (fades slide
sideways). Meter fill colours are sampled from REAL labeled park crops (datasets/meter_real_park)
with the fixed RED/MAGENTA constants kept as fallback choices.

USAGE:
  C:\\Python314\\python.exe tools/diagnostics/synth_meter_fullframe.py --n 4000 \
      --bg-glob "logs/diagnostics/framedump/f*_raw.png" --neg-frac 0.35 --profile mix
"""
import argparse, glob, os, random
import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys
sys.path.insert(0, os.path.join(ROOT, "tools", "diagnostics"))
from synth_meter_gen import render_meter, composite, RED_BGR, MAGENTA_BGR   # reuse the proven meter render

W, H = 1920, 1080


def load_bg(path):
    im = cv2.imread(path)
    if im is None:
        return None
    if im.shape[1] != W or im.shape[0] != H:
        im = cv2.resize(im, (W, H))
    return im


def bg_has_real_meter(im):
    """True if a background frame appears to contain a REAL in-game meter. The framedumps are LIVE
    captures and include mid-shot frames — pasting synthetic meters onto those leaves the real
    meter UNLABELED, training the locator to IGNORE exactly the object it must find (found in the
    v2 contact-sheet audit; a sim-to-real recall killer).
    Signature: a tall-thin solid saturated red/magenta column, FREESTANDING (not a frame-edge wall
    sliver), with the neon-green make-window chevron near/above the column top. The green
    corroboration keeps the red arcade walls / banner panels from mass-excluding backgrounds."""
    Wf = im.shape[1]
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    Hc, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = (((Hc >= 170) | (Hc <= 6) | ((Hc >= 140) & (Hc <= 166))) & (S >= 170) & (V >= 120)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
    green = (((Hc >= 40) & (Hc <= 80)) & (S >= 120) & (V >= 120))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i][:5])
        if not (h >= 40 and 5 <= w <= 70 and h >= 2.2 * w and area >= 0.45 * w * h):
            continue
        if x <= 3 or x + w >= Wf - 3:
            continue                                          # frame-edge wall/banner sliver
        gx0 = max(0, x - 12); gx1 = min(Wf, x + w + 12)
        gy0 = max(0, y - int(1.5 * h)); gy1 = y + max(8, h // 3)
        if int(green[gy0:gy1, gx0:gx1].sum()) >= 10:          # chevron cap present
            return True
    # LOW-FILL meters: the red run is too short for the column test, so anchor on the neon-green
    # chevron itself (small freestanding blob) and require red fill pixels in the narrow column
    # directly below it. Post-shot 'TIMING' banner text can look chevron-like — those frames are
    # mid/post-shot captures anyway, so over-excluding them is harmless.
    gm = cv2.morphologyEx(green.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    ng, _, gstats, _ = cv2.connectedComponentsWithStats(gm, 8)
    for i in range(1, ng):
        gx, gy, gw, gh, garea = (int(v) for v in gstats[i][:5])
        if not (5 <= gw <= 40 and 2 <= gh <= 26 and 10 <= garea <= 500):
            continue
        if gx <= 3 or gx + gw >= Wf - 3:
            continue
        x0 = max(0, gx - 8); x1 = min(Wf, gx + gw + 8)
        y1 = min(im.shape[0], gy + gh + 130)
        if int(m[gy + gh:y1, x0:x1].sum()) >= 30:
            return True
    return False


def paste_meter(bg, rng, palette=None, meter_blur_frac=0.0):
    """Render a meter into a small crop and alpha-paste it onto bg at a varied position/scale.
    Returns the YOLO bbox (cx,cy,w,h normalized) of the meter, or None on a degenerate render.
    palette: list of REAL sampled fill colours (BGR) — preferred over the fixed constants.
    meter_blur_frac: probability of a horizontal motion blur on the meter region (fades slide)."""
    # crop size drives meter size. Real meter ~22x96px @1080p; vary widely for zoom/scale robustness.
    cw = rng.randint(48, 150)          # -> track width ~8-50px
    ch = rng.randint(90, 320)          # -> track height ~40-260px
    # Colour: prefer the REAL sampled meter palette (sim-to-real), keeping the fixed RED + MAGENTA
    # constants as fallback + choices so the locator still learns SHAPE, not one hue.
    # [[nexusvision-meter-color-diversity]]
    if palette and rng.random() < 0.55:
        fill_bgr = palette[rng.randrange(len(palette))]
    else:
        fill_bgr = RED_BGR if rng.random() < 0.55 else MAGENTA_BGR
    layer, alpha, lab = render_meter(ch, cw, rng, fill_bgr=fill_bgr)
    # meter's tight bbox within the crop (render_meter bbox = [x0n,y0n,x1n,y1n] normalized to crop)
    bx0, by0, bx1, by1 = lab["bbox"]
    mw = (bx1 - bx0) * cw
    mh = (by1 - by0) * ch
    if mw < 4 or mh < 20:
        return None
    # paste location: anywhere, incl. the extreme sides (player-attached) but avoid the very bottom HUD.
    px = rng.randint(2, max(3, W - cw - 2))
    py = rng.randint(int(H * 0.03), max(int(H * 0.04), int(H * 0.80) - ch))
    reg = bg[py:py + ch, px:px + cw]
    if reg.shape[:2] != (ch, cw):
        return None
    bg[py:py + ch, px:px + cw] = composite(reg, layer, alpha)
    # full-frame meter bbox -> YOLO
    x0 = px + bx0 * cw; y0 = py + by0 * ch
    # HORIZONTAL motion blur on the METER region only (~meter_blur_frac of positives): a sliding
    # fade smears the bar sideways while the background stays sharp. Kernel 3-9px, tight bbox +4px.
    if rng.random() < meter_blur_frac:
        kx = rng.randint(3, 9)
        mx0 = max(0, int(x0) - 4); my0 = max(0, int(y0) - 4)
        mx1 = min(W, int(x0 + mw) + 5); my1 = min(H, int(y0 + mh) + 5)
        if mx1 - mx0 > kx and my1 - my0 > 2:
            bg[my0:my1, mx0:mx1] = cv2.blur(bg[my0:my1, mx0:mx1], (kx, 1))
    cx = (x0 + mw / 2.0) / W; cy = (y0 + mh / 2.0) / H
    return (cx, cy, mw / W, mh / H)


def cheap_card_degrade(img, rng):
    """Simulate a CHEAP USB capture card so the locator survives $20 hardware: MJPEG blocking, resolution
    loss (downscale->upscale), chroma subsampling / colour bleed, white-balance cast, mild blur + sensor
    noise. Applied to the WHOLE composed frame (meter included) -- a cheap card degrades everything the
    same way. Randomised per-frame so the training set spans clean..cheap. [[cheap-card-support]]"""
    h, w = img.shape[:2]
    out = img
    # 1) resolution loss: downscale to 540p-810p then back up (a cheap card captures below source res)
    if rng.random() < 0.7:
        sh = rng.randint(int(h * 0.5), int(h * 0.85))
        sw = int(w * sh / h)
        out = cv2.resize(cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA), (w, h),
                         interpolation=cv2.INTER_LINEAR)
    # 2) chroma bleed: blur only the colour channels (4:2:0-ish subsampling artefact)
    if rng.random() < 0.6:
        ycc = cv2.cvtColor(out, cv2.COLOR_BGR2YCrCb)
        ycc[:, :, 1] = cv2.blur(ycc[:, :, 1], (3, 3))
        ycc[:, :, 2] = cv2.blur(ycc[:, :, 2], (3, 3))
        out = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)
    # 3) white-balance / colour cast (cheap cards shift channels)
    if rng.random() < 0.5:
        gains = np.array([rng.uniform(0.85, 1.15) for _ in range(3)], np.float32)
        out = np.clip(out.astype(np.float32) * gains, 0, 255).astype(np.uint8)
    # 4) mild blur + sensor noise
    if rng.random() < 0.4:
        out = cv2.GaussianBlur(out, (3, 3), 0)
    if rng.random() < 0.5:
        out = np.clip(out.astype(np.int16) + rng.randint(2, 8) * np.random.randn(h, w, 3).astype(np.int16),
                      0, 255).astype(np.uint8)
    # 5) hard MJPEG re-compression (the dominant cheap-card artefact: 8x8 blocking)
    q = rng.randint(28, 60)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    if ok:
        out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out


def hdmi_degrade(img, rng):
    """Simulate the LIVE 1080p HDMI capture card (the actual rig — closing the sim-to-real gap the
    USB-only degrade left open). An HDMI card is sharp (no MJPEG mush) but: (1) video levels get
    range-crushed full->limited (16..235) — or occasionally the inverse expand when a limited-range
    signal is mis-tagged and double-converted; (2) the signal is TRUE 4:2:0 chroma-subsampled:
    Cr/Cb at half resolution, upsampled blocky (NEAREST). Both hit thin saturated edges — exactly
    the ~10-20px meter bar."""
    out = img
    # 1) limited/full range squeeze (randomized slightly), or the inverse expand ~20% of the time
    r = rng.random()
    if r < 0.70:
        lo = rng.uniform(14.0, 18.0); hi = rng.uniform(233.0, 237.0)
        out = np.clip(lo + out.astype(np.float32) * (hi - lo) / 255.0, 0, 255).astype(np.uint8)
    elif r < 0.90:
        lo = rng.uniform(14.0, 18.0); hi = rng.uniform(233.0, 237.0)
        out = np.clip((out.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)
    # 2) TRUE 4:2:0 chroma subsampling: Cr/Cb half-res (INTER_AREA), back up with INTER_NEAREST
    ycc = cv2.cvtColor(out, cv2.COLOR_BGR2YCrCb)
    h, w = ycc.shape[:2]
    for c in (1, 2):
        half = cv2.resize(ycc[:, :, c], (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA)
        ycc[:, :, c] = cv2.resize(half, (w, h), interpolation=cv2.INTER_NEAREST)
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)


def sample_real_palette(rng, n_crops=200, k=4):
    """Sample ACTUAL meter fill colours (BGR) from the real labeled park crops
    (datasets/meter_real_park images+labels): load ~n_crops random labeled frames, crop each bbox,
    keep saturated red/magenta pixels, k-means the pooled pixels and return the dominant cluster
    centres (largest first). The rendered fill then uses REAL colours instead of only the fixed
    RED_BGR constant — the render-colour half of the sim-to-real gap. Returns [] if no real data.
    Prefers the manually CLEANED split (datasets/meter_val_manual/clean_train.txt, READ-only) when
    present: the raw pseudo-labels include red-wall/UI-banner false positives that would pollute
    the colour clusters."""
    lab_dir = os.path.join(ROOT, "datasets", "meter_real_park", "labels")
    img_dir = os.path.join(ROOT, "datasets", "meter_real_park", "images")
    clean = os.path.join(ROOT, "datasets", "meter_val_manual", "clean_train.txt")
    labs = []
    if os.path.isfile(clean):
        for ln in open(clean, encoding="utf-8"):
            stem = os.path.splitext(os.path.basename(ln.strip()))[0]
            if stem:
                lp = os.path.join(lab_dir, stem + ".txt")
                if os.path.isfile(lp) and os.path.getsize(lp) > 0:
                    labs.append(lp)
    if not labs:
        labs = [p for p in glob.glob(os.path.join(lab_dir, "*.txt")) if os.path.getsize(p) > 0]
    if not labs:
        return []
    rng.shuffle(labs)
    chunks = []
    for lp in labs[:n_crops]:
        stem = os.path.splitext(os.path.basename(lp))[0]
        ip = os.path.join(img_dir, stem + ".png")
        if not os.path.exists(ip):
            ip = os.path.join(img_dir, stem + ".jpg")
            if not os.path.exists(ip):
                continue
        img = cv2.imread(ip)
        if img is None:
            continue
        try:
            parts = open(lp).readline().split()
            ncx, ncy, nbw, nbh = (float(v) for v in parts[1:5])
        except Exception:
            continue
        Hh, Ww = img.shape[:2]
        # pad the box a touch sideways (older labels carry an approximate width); the saturation
        # mask below isolates the fill pixels regardless.
        x0 = max(0, int((ncx - nbw) * Ww)); x1 = min(Ww, int((ncx + nbw) * Ww) + 1)
        y0 = max(0, int((ncy - nbh / 2) * Hh)); y1 = min(Hh, int((ncy + nbh / 2) * Hh) + 1)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        Hc, Sc, Vc = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        m = (((Hc >= 160) | (Hc <= 14) | ((Hc >= 130) & (Hc < 160))) & (Sc >= 150) & (Vc >= 90))
        sel = crop[m]
        if len(sel) == 0:
            continue
        if len(sel) > 400:                                   # cap per-crop so one frame can't dominate
            sel = sel[np.linspace(0, len(sel) - 1, 400).astype(int)]
        chunks.append(sel)
    if not chunks:
        return []
    pts = np.concatenate(chunks).reshape(-1, 3).astype(np.float32)
    if len(pts) < 50:
        return []
    K = min(k, max(1, len(pts) // 200))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, lbl, centers = cv2.kmeans(pts, K, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(lbl.ravel(), minlength=K)
    order = np.argsort(-counts)
    pal = []
    for o in order:
        if counts[o] < 0.05 * len(pts):
            continue
        c = tuple(int(v) for v in centers[o])
        # keep only TRUE meter hues (red-wrap or purple); residual junk labels (orange court
        # lines, rim) can survive the pixel mask into their own cluster — reject those centres.
        hue = int(cv2.cvtColor(np.full((1, 1, 3), c, np.uint8), cv2.COLOR_BGR2HSV)[0, 0, 0])
        if hue >= 160 or hue <= 8 or (130 <= hue < 160):
            pal.append(c)
    return pal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4000)
    ap.add_argument("--profile", choices=("usb", "hdmi", "mix"), default="mix",
                    help="capture-degradation profile: usb=cheap_card_degrade, hdmi=hdmi_degrade "
                         "(the LIVE 1080p HDMI rig), mix=per-frame ~60/40 hdmi/usb (default)")
    ap.add_argument("--cheap-card-frac", type=float, default=0.5,
                    help="fraction of frames degraded (which degrade is picked by --profile)")
    ap.add_argument("--meter-blur-frac", type=float, default=0.3,
                    help="fraction of positives with horizontal motion blur on the meter region")
    ap.add_argument("--jpg-q", type=int, default=90, help="output JPEG quality")
    ap.add_argument("--out", default=os.path.join("datasets", "meter_fullframe_synth"))
    ap.add_argument("--bg-glob", default=os.path.join("logs", "diagnostics", "framedump", "f*_raw.png"))
    ap.add_argument("--neg-frac", type=float, default=0.35, help="fraction of hard-negative (no-meter) frames")
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep-meter-bgs", action="store_true",
                    help="skip the real-meter background exclusion scan (debug/back-compat only; "
                         "leaving real meters unlabeled in backgrounds poisons the locator)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    bgs = glob.glob(os.path.join(ROOT, args.bg_glob)) if not os.path.isabs(args.bg_glob) else glob.glob(args.bg_glob)
    bgs = [b for b in bgs if os.path.getsize(b) > 5000]      # skip partial/corrupt dumps
    if not bgs:
        raise SystemExit(f"no backgrounds matched {args.bg_glob}")
    print(f"backgrounds: {len(bgs)}")

    if not args.keep_meter_bgs:
        # One-time sequential scan (read-only I/O): drop live-capture frames that were dumped
        # MID-SHOT and therefore contain the real meter — they must never be 'background'.
        kept, dropped = [], []
        for b in bgs:
            im = load_bg(b)
            if im is None:
                continue
            (dropped if bg_has_real_meter(im) else kept).append(b)
        print(f"backgrounds containing a REAL meter excluded: {len(dropped)}/{len(bgs)} -> {len(kept)} usable")
        if not kept:
            raise SystemExit("all backgrounds excluded by the real-meter scan — check bg_has_real_meter")
        bgs = kept

    palette = sample_real_palette(rng)
    print(f"real meter palette (BGR, dominant first): {palette if palette else 'NONE -> fixed constants only'}")

    out = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    img_dir = os.path.join(out, "images"); lab_dir = os.path.join(out, "labels")
    os.makedirs(img_dir, exist_ok=True); os.makedirs(lab_dir, exist_ok=True)

    written = []
    made = 0
    tries = 0
    while made < args.n and tries < args.n * 3:
        tries += 1
        bg = load_bg(rng.choice(bgs))
        if bg is None:
            continue
        boxes = []
        if rng.random() >= args.neg_frac:                    # POSITIVE: 1-2 meters (usually 1)
            k = 1 if rng.random() < 0.9 else 2
            for _ in range(k):
                bb = paste_meter(bg, rng, palette=palette, meter_blur_frac=args.meter_blur_frac)
                if bb is not None:
                    boxes.append(bb)
        # Capture degradation on a fraction (applied AFTER the meter is composited, so the meter is
        # degraded exactly as the real card would degrade it). Clean frames keep full fidelity.
        if rng.random() < args.cheap_card_frac:
            if args.profile == "usb":
                bg = cheap_card_degrade(bg, rng)
            elif args.profile == "hdmi":
                bg = hdmi_degrade(bg, rng)
            else:                                            # mix: the live rig is HDMI-first
                bg = hdmi_degrade(bg, rng) if rng.random() < 0.6 else cheap_card_degrade(bg, rng)
        name = f"m{made:06d}"
        cv2.imwrite(os.path.join(img_dir, name + ".jpg"), bg, [cv2.IMWRITE_JPEG_QUALITY, args.jpg_q])
        with open(os.path.join(lab_dir, name + ".txt"), "w") as f:      # empty file = hard negative
            for (cx, cy, w, h) in boxes:
                f.write(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
        written.append(os.path.join(img_dir, name + ".jpg"))
        made += 1

    rng.shuffle(written)
    nv = int(len(written) * args.val_frac)
    val, tr = written[:nv], written[nv:]
    with open(os.path.join(out, "train.txt"), "w") as f:
        f.write("\n".join(tr) + "\n")
    with open(os.path.join(out, "val.txt"), "w") as f:
        f.write("\n".join(val) + "\n")
    with open(os.path.join(out, "meter.yaml"), "w") as f:
        f.write(f"path: {out}\ntrain: train.txt\nval: val.txt\nnames:\n  0: meter\n")
    npos = sum(1 for p in written if os.path.getsize(os.path.join(lab_dir, os.path.basename(p)[:-4] + ".txt")) > 0)
    print(f"wrote {made} frames ({npos} positive / {made - npos} hard-negative) -> {out}")
    print(f"train={len(tr)} val={len(val)}  yaml: {os.path.join(out, 'meter.yaml')}")


if __name__ == "__main__":
    main()
