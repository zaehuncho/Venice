"""Compute the within-shot engine FEED rate straight from a live DETDIAG log.

Run a measurement batch with dense DETDIAG:
    $env:ORION_DETDIAG="1"; $env:ORION_DETDIAG_INTERVAL="0.04"   (~25 logged/sec)
then play a varied batch (Orion UN-minimized) and point this at the log:
    python tools/diagnostics/analyze_detdiag_log.py logs/orion_native.log

Feeds the engine when reject in {'', 'green_not_found'} (matches
RemotePlayOrchestrator._should_feed_engine). Reports per-shot fed-rate + worst blind
gap (consecutive unfed logged samples) — the thing that governs release timing.
"""
import re
import sys

FED = {"", "green_not_found"}
GAP_SAMPLES = 8   # >=N consecutive non-detected samples ends a shot
MIN_SHOT = 5

LINE = re.compile(
    r"DETDIAG\s+detected=(?P<det>\w+)\s+fill=(?P<fill>[\d.]+|None)\s+conf=(?P<conf>[\d.]+|None).*?"
    r"reject=(?P<rej>'[^']*'|\"[^\"]*\"|\w+)\s+frame=(?P<w>\d+)x(?P<h>\d+)"
)


def _samples(path):
    out = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = LINE.search(line)
        if not m:
            continue
        rej = m.group("rej").strip("'\"")
        out.append({
            "det": m.group("det").lower() == "true",
            "fill": float(m.group("fill")) if m.group("fill") != "None" else 0.0,
            "conf": float(m.group("conf")) if m.group("conf") != "None" else 0.0,
            "rej": rej,
            "wh": (int(m.group("w")), int(m.group("h"))),
        })
    return out


def main(path):
    sm = _samples(path)
    if not sm:
        print(f"No DETDIAG samples in {path}. Run with ORION_DETDIAG=1 ORION_DETDIAG_INTERVAL=0.04.")
        return
    frames = {s["wh"] for s in sm}
    print(f"file: {path}")
    print(f"DETDIAG samples: {len(sm)}   capture sizes seen: {sorted(frames, reverse=True)}")
    conf_det = [s["conf"] for s in sm if s["det"]]
    if conf_det:
        conf_det.sort()
        print(f"detected conf: min={conf_det[0]:.2f} median={conf_det[len(conf_det)//2]:.2f} max={conf_det[-1]:.2f}")

    shots, cur, miss = [], [], 0
    for s in sm:
        if s["det"]:
            if miss >= GAP_SAMPLES and cur:
                shots.append(cur)
                cur = []
            miss = 0
            cur.append(s)
        else:
            miss += 1
    if cur:
        shots.append(cur)
    real = [s for s in shots if len(s) >= MIN_SHOT]
    print(f"\nreal shots (>= {MIN_SHOT} detected samples): {len(real)}")
    print(f"{'shot':>4} {'smpl':>4} {'fed%':>5} {'maxgap':>6} {'mem':>4} {'unstab':>6} {'lowconf':>7}  fill")
    tot_fed = tot = well = 0
    for i, s in enumerate(real, 1):
        flags = [x["rej"] in FED for x in s]
        fed = sum(flags)
        maxgap = g = 0
        for f in flags:
            g = 0 if f else g + 1
            maxgap = max(maxgap, g)
        mem = sum(1 for x in s if x["rej"] == "meter_memory")
        uns = sum(1 for x in s if x["rej"] == "bbox_unstable")
        low = sum(1 for x in s if x["rej"] == "low_confidence")
        fills = [x["fill"] for x in s]
        tot_fed += fed
        tot += len(s)
        ok = (fed / len(s) >= 0.60) and (maxgap <= 3)
        well += ok
        print(f"{i:>4} {len(s):>4} {100*fed/len(s):>4.0f}% {maxgap:>6} {mem:>4} {uns:>6} {low:>7}  "
              f"{min(fills):.0f}->{max(fills):.0f}%{'' if ok else '  <-- sparse/gappy'}")
    print(f"\nWITHIN-SHOT fed-rate: {100*tot_fed/max(1,tot):.0f}%  ({tot_fed}/{tot})")
    print(f"timing-able shots (>=60% fed, gap<=3): {well}/{len(real)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/orion_native.log")
