"""Offline detector scorecard on the framedump — the things that matter:
acquisition timing (detect DURING the rise, not after), per-shot coverage,
precision (on the real meter), and stability (jitter). Re-run after each tune.

  python tools/diagnostics/detector_eval.py [framedump_glob]
"""
import sys, glob
sys.path.insert(0, ".")
import cv2, numpy as np
from meter_detector import MeterDetector, DetectorConfig

GLOB = sys.argv[1] if len(sys.argv) > 1 else "logs/diagnostics/framedump/f*_0_raw.png"
FRAMES = sorted(glob.glob(GLOB))


def red_present(img):
    """Loose ground truth: a tall-thin saturated-red bar is (probably) the meter."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV); H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((((H >= 160) | (H <= 12)) & (S >= 130) & (V >= 100))).astype(np.uint8); red[:110] = 0
    rm = cv2.dilate(red * 255, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 7)), 1)
    cnts, _ = cv2.findContours(rm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None; bs = 0
    for cc in cnts:
        x, y, w, h = cv2.boundingRect(cc)
        if h >= 22 and w <= 20 and h / max(1, w) >= 2.2:
            sc = h * h / max(1, w)
            if sc > bs: bs = sc; best = (x, y, w, h)
    return best


def red_at(img, bb):
    x, y, w, h = [int(v) for v in bb]
    roi = img[max(0, y - 3):y + h + 3, max(0, x - 3):x + w + 3]
    if roi.size == 0: return False
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV); H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return int((((H >= 160) | (H <= 14)) & (S >= 120) & (V >= 90)).sum()) >= 6


def evaluate():
    c = DetectorConfig(); c.meter_color = "Red"; c.auto_meter_color = True
    det = MeterDetector("meter_styles", c)
    try: det.set_active_style("Arrow2")
    except Exception: pass
    rows = []   # dict per frame
    for i, fp in enumerate(FRAMES):
        img = cv2.imread(fp)
        if img is None:
            rows.append(dict(i=i, det=False, bb=None, fill=0.0, gt=None, onred=False)); continue
        gt = red_present(img)
        r = det.detect(img); found = bool(getattr(r, "detected", False))
        bb = getattr(r, "bbox", None) if found else None
        rows.append(dict(i=i, det=found, bb=bb, fill=float(getattr(r, "fill_pct", 0) or 0),
                         gt=gt, onred=(red_at(img, bb) if bb else False)))
    dets = [r for r in rows if r["det"]]
    prec = 100 * sum(1 for r in dets if r["onred"]) / max(1, len(dets))
    bursts = []; cur = []
    for r in rows:
        if r["det"]:
            if cur and r["i"] - cur[-1]["i"] > 10: bursts.append(cur); cur = []
            cur.append(r)
    if cur: bursts.append(cur)
    real = [b for b in bursts if len(b) >= 4]
    early = sum(1 for b in real if b[0]["fill"] < 25)
    tip = sum(1 for b in real if max(x["fill"] for x in b) > 90)
    jit = []
    for b in real:
        prev = None
        for r in b:
            if r["bb"]:
                cx = r["bb"][0] + r["bb"][2] / 2; cy = r["bb"][1] + r["bb"][3] / 2
                if prev: jit.append(abs(cx - prev[0]) + abs(cy - prev[1]))
                prev = (cx, cy)
    js = sorted(jit)
    jmed = js[len(js) // 2] if js else 0; jmax = max(js) if js else 0
    drop = gtin = 0
    for b in real:
        for i in range(b[0]["i"], b[-1]["i"] + 1):
            if rows[i]["gt"] is not None:
                gtin += 1
                if not rows[i]["det"]: drop += 1
    print("=" * 64)
    print(f"detections={len(dets)}  precision(on-red)={prec:.0f}%")
    print(f"shot bursts(>=4)={len(real)}  early-acquire(<25%)={early}/{len(real)}  reach-tip(>90%)={tip}/{len(real)}")
    print(f"jitter within bursts: median={jmed:.1f}px  max={jmax:.0f}px")
    print(f"intra-burst dropouts: {drop}/{gtin} meter-present frames missed inside a shot ({100*drop/max(1,gtin):.0f}%)")
    print("per-burst [first->max fill] len onred:")
    for b in real:
        fl = [x["fill"] for x in b]
        print(f"  f{b[0]['i']:4d}-{b[-1]['i']:4d} [{fl[0]:3.0f}->{max(fl):3.0f}%] len={len(b):2d} onred={sum(1 for x in b if x['onred'])}/{len(b)}")
    return dict(prec=prec, bursts=len(real), early=early, tip=tip, jmed=jmed, jmax=jmax, drop=drop, gtin=gtin)


if __name__ == "__main__":
    evaluate()
