"""[ORION_FUSED_FIRE] Shadow A/B referee — the measured-lead / fused-fire FLIP GATE.

Every shot in a session with fused_shadow ON (the default) logs one line:

  FusedShadow: bucket=<b> mu=<ms> sigma=<ms> fireAt=<ms> shadowFire=<ms> actual=<ms>
               delta=<ms> shadowFill=<pct> info=<n> pair=<0|1> code=<releaseReasonCode>

This tool aggregates them per bucket and prints the flip criteria verdict:

  1. coverage:   >= 80% of shots anchored with sigma <= 30ms at release
  2. agreement:  |median delta| <= 25ms vs the (EMA-dialed, historically greening)
                 feedforward fires, per bucket with n >= 5
  3. oracle:     the session's last 'release marker:' line shows sd <= 3.3 and n >= 6

Usage: python tools/timing/fused_shadow_report.py <session-log> [more logs...]
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

SHADOW_RE = re.compile(
    r"FusedShadow: bucket=(\S+) mu=([\-0-9.]+) sigma=([\-0-9.]+) fireAt=([\-0-9.]+) "
    r"shadowFire=([\-0-9.]+) actual=([\-0-9.]+) delta=([\-0-9.]+)ms shadowFill=([\-0-9.]+) "
    r"info=(\d+) pair=(\d) code=(\S+)")
ORACLE_RE = re.compile(r"release marker: .*l_fixed=([\-0-9.]+) sd=([\-0-9.]+)")


def main(paths: list[str]) -> int:
    rows = []
    oracle_sd = None
    oracle_n = 0
    for p in paths:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        for m in SHADOW_RE.finditer(text):
            rows.append({
                "bucket": m.group(1), "mu": float(m.group(2)), "sigma": float(m.group(3)),
                "fireAt": float(m.group(4)), "shadowFire": float(m.group(5)),
                "actual": float(m.group(6)), "delta": float(m.group(7)),
                "fill": float(m.group(8)), "info": int(m.group(9)),
                "pair": int(m.group(10)), "code": m.group(11),
            })
        oracle_n += len(ORACLE_RE.findall(text))
        for m in ORACLE_RE.finditer(text):
            oracle_sd = float(m.group(2))
    if not rows:
        print("no FusedShadow lines found — run a session with fused_shadow ON (the default)")
        return 1

    n = len(rows)
    anchored_ok = [r for r in rows if 0.0 < r["sigma"] <= 30.0]
    fired = [r for r in rows if r["shadowFire"] >= 0.0]
    print(f"shots with fused shadow: {n}; sigma<=30 at release: {len(anchored_ok)} "
          f"({100.0 * len(anchored_ok) / n:.0f}%); shadow fired: {len(fired)}")

    per = defaultdict(list)
    for r in fired:
        per[r["bucket"]].append(r["delta"])
    print(f"\n{'bucket':24s} {'n':>4s} {'median delta':>12s} {'IQR':>8s}  verdict (|med|<=25, n>=5)")
    all_pass = True
    for bucket, deltas in sorted(per.items()):
        arr = np.asarray(deltas, float)
        med = float(np.median(arr))
        iqr = float(np.percentile(arr, 75) - np.percentile(arr, 25))
        ok = len(arr) >= 5 and abs(med) <= 25.0
        all_pass = all_pass and ok
        print(f"{bucket:24s} {len(arr):>4d} {med:>10.1f}ms {iqr:>6.1f}ms  {'PASS' if ok else 'INSUFFICIENT' if len(arr) < 5 else 'FAIL'}")

    cov_ok = len(anchored_ok) >= 0.8 * n
    oracle_ok = oracle_sd is not None and oracle_sd <= 3.3 and oracle_n >= 6
    print(f"\ncoverage {'PASS' if cov_ok else 'FAIL'} | agreement "
          f"{'PASS' if all_pass and per else 'FAIL'} | oracle "
          f"{'PASS' if oracle_ok else 'FAIL'} (sd={oracle_sd}, markers={oracle_n})")
    if cov_ok and all_pass and per and oracle_ok:
        print("\nFLIP GATE: ALL CRITERIA MET — enable settings fused_fire + measured_lead "
              "(or ORION_FUSED_FIRE=1 ORION_MEASURED_LEAD=1) for the next session.")
        return 0
    print("\nFLIP GATE: NOT MET — keep shadow mode; collect more shots / run warmup probes.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
