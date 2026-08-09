"""Quick one-off: per-frame detframes timeline around one shot window (Round 33 triage)."""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# seq=33 Go-To: release at 2026-06-12T19:23:55.012Z, learned clock 2099ms -> hold start ~19:23:52.91
rel = datetime(2026, 6, 12, 19, 23, 55, 12000, tzinfo=timezone.utc).timestamp() * 1000
start = rel - 2200

repo = Path(__file__).resolve().parents[2]
with open(repo / "logs" / "diagnostics" / "detframes.csv") as f:
    rows = [r for r in csv.DictReader(f) if start <= float(r['wall_ms']) <= rel + 300]

print('frames in window:', len(rows))
for r in rows:
    if r['detected'] != '1':
        continue
    t = (float(r['wall_ms']) - rel) / 1000.0
    print(f"{t:+.2f}s fill={r['fill_pct']:>6} conf={r['confidence']:>5} "
          f"bbox=({r['x']},{r['y']},{r['w']},{r['h']}) frame={r['frame_w']}x{r['frame_h']} "
          f"med_h={r['med_h']:>5} med_s={r['med_s']:>5} zone={r['zone']}")
