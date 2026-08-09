"""Replay dumped raw frames through the real detector with the debug sink, then
draw the search regions + every purple candidate so we can SEE why a visible meter
is missed (roi_not_found). Iterates against the exact failing frames.

    python tools/diagnostics/debug_detect_frame.py f00150_0_raw.png [f00083_0_raw.png ...]
"""
import os
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from meter_detector import MeterDetector, DetectorConfig

FD = os.path.join(ROOT, "logs", "diagnostics", "framedump")
OUT = os.path.join(ROOT, "logs", "diagnostics")


def _make_det():
    cfg = DetectorConfig()
    cfg.meter_color = "Purple"
    cfg.meter_style = "Arrow2"
    det = MeterDetector("meter_styles", cfg)
    # set_active_style is what narrows detection to the Arrow2 profile; setting
    # cfg.meter_style alone is ignored by the detector (it reads self._active).
    det.set_active_style("Arrow2")
    return det


def run(fname):
    path = fname if os.path.isabs(fname) else os.path.join(FD, fname)
    img = cv2.imread(path)
    if img is None:
        print(f"{fname}: could not read")
        return
    H, W = img.shape[:2]
    # Survey pass: a fresh detector, ONE full-frame detect with the debug sink.
    # Unlocked, so every colour candidate's bbox is in frame-absolute coords and
    # we see the whole candidate field (meter vs player/ball blobs) + gate results.
    survey = _make_det()
    survey._debug = {}
    r = survey.detect(img)
    dbg = survey._debug
    # Settled result: feed the frame the way the live pipeline does so detected /
    # rejection_reason reflect the ACQUIRED state (a single detect can't satisfy
    # min_consecutive_valid_frames, so it would read bbox_unstable).
    settled = _make_det()
    rs = r
    for _ in range(6):
        rs = settled.detect(img)

    print(f"\n=== {fname}  {W}x{H} ===")
    print(f"survey : detected={r.detected} bbox={r.bbox} fill={r.fill_pct:.0f} conf={r.confidence:.2f} reject={r.rejection_reason!r}")
    print(f"settled: detected={rs.detected} bbox={rs.bbox} fill={rs.fill_pct:.0f} conf={rs.confidence:.2f} reject={rs.rejection_reason!r}")
    for g in dbg.get("gates", []):
        print(f"  gate[{g['style']}] sx={g['sx']} sy={g['sy']} w=[{g['w_min']},{g['w_max']}] h=[{g['h_min']},{g['h_max']}] ar=[{g['ar_min']},{g['ar_max']}]")
    regions = dbg.get("regions", [])
    print(f"  search regions: {len(regions)}")
    for reg in regions[:6]:
        print(f"    region {reg[4]}: x[{reg[0]}..{reg[2]}] y[{reg[1]}..{reg[3]}]")
    cands = dbg.get("cand", [])
    cands.sort(key=lambda c: -c["area"])
    print(f"  candidates (purple contours found): {len(cands)} -- top by area:")
    for c in cands[:12]:
        print(f"    x={c['x']} y={c['y']} {c['w']}x{c['h']} ar={c['ar']} area={c['area']:.0f} size_ok={c['size_ok']}")

    # draw
    vis = img.copy()
    for reg in regions:
        cv2.rectangle(vis, (reg[0], reg[1]), (reg[2], reg[3]), (255, 120, 0), 1)  # blue = search region
    for c in cands:
        col = (0, 220, 0) if c["size_ok"] else (0, 0, 230)  # green=passed size, red=failed size
        cv2.rectangle(vis, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), col, 1)
    if r.detected and r.bbox[2] > 0:
        x, y, w, h = r.bbox
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 2)  # yellow = accepted
    outp = os.path.join(OUT, "dbg_" + os.path.splitext(os.path.basename(path))[0] + ".png")
    cv2.imwrite(outp, vis)
    print(f"  -> annotated: {outp}")


if __name__ == "__main__":
    files = sys.argv[1:] or ["f00150_0_raw.png", "f00083_0_raw.png"]
    for f in files:
        run(f)
