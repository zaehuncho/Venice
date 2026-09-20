#!/usr/bin/env python3
"""Rescale every METER-TIME constant in learning.json (and the launcher's ORION_METER_TIME_SCALE)
by a measured factor k when the game's jumpshot Release Speed changes.

  python tools/timing/rescale_meter_time.py --measure logs/diagnostics/detframes.csv
      -> prints k = (new 20->90 crossing interval) / 334.5 ms from the 60 fps capture
  python tools/timing/rescale_meter_time.py --apply 1.32
      -> backs up learning.json, scales its meter-time keys by 1.32 (relative to the stored
         meter_time_scale, so re-applying is idempotent), records meter_time_scale=1.32, and
         prints the launcher line to set ORION_METER_TIME_SCALE.

Meter time (scales):   learned/measured_phase_physical_ms, global_appear_to_tip_ms,
                       shot_type_appear_to_tip_ms.*, global_rise_velocity_pct_ms (1/k),
                       ema_fill_per_frame (1/k)
Rig time (never):      learned_latency_ms, actuation_lead_ms, probe_spawn_offset_ms, aim offset
The reference meter is the 2K27 meter measured 09-02/09-03 (20->90 = 334.5 ms, k = 1.0).
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LEARNING = ROOT / "learning.json"
REF_20_90_MS = 334.5
F = [20, 30, 41.5, 50, 60, 70, 80, 90]


def _crossings(t, f):
    fm = np.maximum.accumulate(f)
    cr = {}
    for th in F:
        i = int(np.searchsorted(fm, th))
        if i == 0 or i >= len(fm):
            return None
        cr[th] = t[i - 1] + (th - fm[i - 1]) / max(1e-6, fm[i] - fm[i - 1]) * (t[i] - t[i - 1])
    return cr


def measure(csv_path: Path, min_shots: int = 6) -> float:
    rows = []
    with open(csv_path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append((float(r["wall_ms"]), int(r["detected"]), float(r["fill_pct"]), int(r["h"])))
            except Exception:
                pass
    det = [r for r in rows if r[1] and r[3] > 60]
    shots, cur = [], []
    for r in det:
        if cur and r[0] - cur[-1][0] > 700:
            shots.append(cur)
            cur = []
        cur.append(r)
    if cur:
        shots.append(cur)
    d2090, d2041, rates = [], [], []
    for s in shots:
        t = np.array([r[0] for r in s])
        f = np.array([r[2] for r in s])
        i90 = int(np.argmax(f >= 90)) if (f >= 90).any() else -1
        if i90 <= 0:
            continue
        t, f = t[: i90 + 1], f[: i90 + 1]
        if len(t) < 6 or f[0] > 20:
            continue
        cr = _crossings(t, f)
        if not cr:
            continue
        d2090.append(cr[90] - cr[20])
        d2041.append(cr[41.5] - cr[20])
        rates.append([(b - a) / (cr[b] - cr[a]) for a, b in zip(F[:-1], F[1:])])
    if len(d2090) < min_shots:
        print(f"only {len(d2090)} clean rises in {csv_path.name}; need >= {min_shots}")
        return float("nan")
    mad = lambda v: 1.4826 * np.median(np.abs(np.asarray(v) - np.median(v)))
    k = float(np.median(d2090)) / REF_20_90_MS
    print(f"{csv_path.name}: {len(d2090)} clean rises; 20->90 median {np.median(d2090):.1f} ms "
          f"(rmad {mad(d2090):.1f}), 20->41.5 median {np.median(d2041):.1f} ms; "
          f"local rates {[round(float(np.median([r[i] for r in rates])), 3) for i in range(len(F) - 1)]}")
    print(f"k = {np.median(d2090):.1f} / {REF_20_90_MS} = {k:.3f}")
    return k


def apply(k: float) -> None:
    data = json.loads(LEARNING.read_text(encoding="utf-8-sig"))
    prev = float(data.get("meter_time_scale", 1.0) or 1.0)
    rel = k / prev
    backup = LEARNING.with_name(f"learning.json.bak-meterscale-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(LEARNING, backup)
    changed = {}
    for key in ("learned_phase_physical_ms", "measured_phase_physical_ms", "global_appear_to_tip_ms"):
        if key in data and data[key] is not None:
            changed[key] = (data[key], data[key] * rel)
            data[key] = data[key] * rel
    for key in ("global_rise_velocity_pct_ms", "ema_fill_per_frame"):
        if key in data and data[key]:
            changed[key] = (data[key], data[key] / rel)
            data[key] = data[key] / rel
    if isinstance(data.get("shot_type_appear_to_tip_ms"), dict):
        for t, v in list(data["shot_type_appear_to_tip_ms"].items()):
            data["shot_type_appear_to_tip_ms"][t] = v * rel
        changed["shot_type_appear_to_tip_ms"] = ("x", f"x{rel:.3f}")
    data["meter_time_scale"] = k
    LEARNING.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"backup: {backup.name}")
    for key, (a, b) in changed.items():
        print(f"  {key}: {a} -> {b}")
    print(f"meter_time_scale = {k:.3f} (relative factor applied {rel:.3f})")
    print("Launcher: set  ORION_METER_TIME_SCALE = \"%.3f\"  next to the PCTL/curve flags, then relaunch." % k)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", type=Path, help="60 fps detframes CSV recorded at the NEW speed")
    ap.add_argument("--apply", type=float, help="scale factor k to write into learning.json")
    args = ap.parse_args()
    if args.measure:
        k = measure(args.measure)
        if not np.isfinite(k):
            return 1
    if args.apply is not None:
        if not 0.5 <= args.apply <= 3.0:
            print("k must be within 0.5..3.0")
            return 1
        apply(args.apply)
    if not args.measure and args.apply is None:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
