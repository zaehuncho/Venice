"""Audit detframes.csv: which accepted detections fed the engine, and when did
out-of-band masked-median winners (med_h outside the mask's own 132-167 hue band)
appear? med_h > 159 winners should be impossible under the default purity gate."""
import csv
from datetime import datetime, timezone
from pathlib import Path


def ts(ms):
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).strftime("%m-%dT%H:%M:%SZ")


repo = Path(__file__).resolve().parents[2]
rows = list(csv.DictReader(open(repo / "logs" / "diagnostics" / "detframes.csv")))
det = [r for r in rows if r.get("detected") == "1" and r.get("med_h") not in (None, "")]
print(f"total rows {len(rows)}, detected {len(det)}")

in_band = [r for r in det if 132.0 <= float(r["med_h"]) <= 167.0]
out_band = [r for r in det if float(r["med_h"]) >= 0.0 and not (132.0 <= float(r["med_h"]) <= 167.0)]
nomed = [r for r in det if float(r["med_h"]) < 0.0]
print(f"in-band med_h [132,167]: {len(in_band)}   out-of-band: {len(out_band)}   no-median(-1): {len(nomed)}")

if in_band:
    print("\nin-band winners (real meter?) first/last:")
    for r in (in_band[0], in_band[-1]):
        print(f"  {ts(r['wall_ms'])} fill={r['fill_pct']} bbox=({r['x']},{r['y']},{r['w']},{r['h']}) "
              f"med_h={r['med_h']} med_s={r['med_s']} frame={r['frame_w']}x{r['frame_h']}")

over159 = [r for r in det if float(r["med_h"]) > 159.0]
print(f"\nwinners with med_h > 159 (should be purity-impossible): {len(over159)}")
for r in over159[:10]:
    print(f"  {ts(r['wall_ms'])} fill={r['fill_pct']} bbox=({r['x']},{r['y']},{r['w']},{r['h']}) "
          f"med_h={r['med_h']} med_s={r['med_s']} zone={r['zone']}")

fed_noise = [r for r in det if r.get("fed") == "1" and float(r["med_h"]) >= 0.0 and float(r["med_h"]) < 100.0]
fed_all = [r for r in det if r.get("fed") == "1"]
print(f"\nfed rows: {len(fed_all)}; fed with red-ish winner med_h in [0,100): {len(fed_noise)}")
if fed_all:
    print("fed med_h breakdown:")
    from collections import Counter
    print(" ", Counter(r["med_h"] for r in fed_all).most_common(8))
