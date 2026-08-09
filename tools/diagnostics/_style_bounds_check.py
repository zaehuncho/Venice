"""Print each meter style's Purple BGR box and the HSV mask bounds it produces."""
import glob
import json
import sys
from pathlib import Path

import numpy as np

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))
from meter_detector import _style_bgr_bounds_to_hsv  # noqa: E402

for p in glob.glob(str(repo / "meter_styles" / "*.json")):
    st = json.load(open(p, encoding="utf-8"))
    c = st.get("colors", {}).get("Purple")
    if not c:
        print(p, "no Purple")
        continue
    lo = np.array(c["low"], np.uint8)
    hi = np.array(c["high"], np.uint8)
    hlo, hhi = _style_bgr_bounds_to_hsv(lo, hi)
    print(f"{p}\n  BGR {c['low']}..{c['high']} -> HSV lo {hlo.tolist()} hi {hhi.tolist()}")
