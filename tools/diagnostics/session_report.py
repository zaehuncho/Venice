"""session_report.py -- one-command post-session verdict for a live Orion run.

Parses the LAST session in logs/orion_native.log (a session = everything after the final
sidecar-environment marker, i.e. the last Connect) and prints:

  * a per-shot table built from the Release-family telemetry lines
    (seq, shot type, release code, hold->release ms, fill@release, fresh/stale sample split,
     acquisition latency, blind-fire + anchor flags, tip-gate defer),
  * session aggregates (blind-fire rate, fresh-serve health, acquisition latency stats,
    DETDIAG rejection histogram, capture-health anomalies, watchdog/restart events),
  * a PASS / ATTENTION verdict with the specific reasons.

Notes:
  * 'Shot outcome' verdicts are reported but EXCLUDED from the pass criteria when the session
    logged 'Calibration grader FROZEN' -- the frozen self-grade emits a constant deflate-LATE
    (~66ms) that is not a timing measurement.
  * Stdlib-only on purpose: runs under both C:\\Python314\\python.exe and .venv311.

Usage:
  python tools\\diagnostics\\session_report.py                 # last session, default log
  python tools\\diagnostics\\session_report.py --log PATH      # explicit log
  python tools\\diagnostics\\session_report.py --json OUT.json # also dump machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG = os.path.join(REPO, "logs", "orion_native.log")

_TS = r"^(?P<ts>\S+)\s+"


def _kv_all(line):
    """token=value pairs (values are space-free by log convention, shot=<type> last)."""
    d = dict(re.findall(r"(\w+)=([^\s]+)", line))
    m = re.search(r"shot=(.+)$", line)
    if m:
        d["shot"] = m.group(1).strip()
    return d


def _is_session_marker(line):
    """Accept both the legacy value-bearing marker and the redacted key-only marker."""
    return "Sidecar env:" in line or "Sidecar env keys:" in line


def parse_last_session(path):
    """Return (session_lines, start_index) for the last Connect block; fall back to
    the whole file when no marker exists."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start = 0
    for i, ln in enumerate(lines):
        if _is_session_marker(ln):
            start = i
    return lines[start:], start


def analyze(lines):
    shots = defaultdict(dict)          # seq -> merged fields
    rejects = Counter()
    detdiag_n = 0
    uniqfps_zero_runs = 0
    events = []                        # (ts, kind, detail)
    grader_frozen = False
    env_line = ""
    timing_mode = ""
    outcome = {}

    for ln in lines:
        if _is_session_marker(ln):
            env_line = ln.strip()
        elif "Timing mode:" in ln:
            timing_mode = ln.split("Timing mode:", 1)[1].strip()
        elif "Calibration grader FROZEN" in ln:
            grader_frozen = True
        elif "DETDIAG" in ln:
            detdiag_n += 1
            m = re.search(r"reject='([a-z_]*)'", ln)
            if m:
                rejects[m.group(1) or "(accepted)"] += 1
            if "uniqfps=0" in ln:
                uniqfps_zero_runs += 1
        elif "Release issued:" in ln:
            d = _kv_all(ln)
            seq = d.get("seq", "?")
            m = re.search(r"fill ([\d.]+)% target ([\d.]+)%", ln)
            shots[seq].update({
                "shot": d.get("shot", "?"), "code": d.get("code", "?"),
                "conf": d.get("conf", ""), "offset_ms": d.get("offset", ""),
                "fill_at_issue": (m.group(1) if m else ""), "target": (m.group(2) if m else ""),
            })
        elif "Release timing:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({
                "holdToRelMs": d.get("holdToRelMs", ""), "appearToRelMs": d.get("appearToRelMs", ""),
                "fillAtRel": d.get("fillAtRel", ""), "peakFill": d.get("peakFill", ""),
                "plannedClockMs": d.get("plannedClockMs", ""),
            })
        elif "Release detsummary:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({
                "samples": d.get("samples", ""), "fresh": d.get("fresh", ""),
                "staleMem": d.get("staleMem", ""), "nodet": d.get("nodet", ""),
                "firstFreshMs": d.get("firstFreshMs", ""), "minFreshFill": d.get("minFreshFill", ""),
            })
        elif "Release freshness:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({"blindFire": d.get("blindFire", "")})
        elif "Release vision:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({
                "anchorValid": d.get("anchorValid", ""), "tipGateDeferMs": d.get("tipGateDeferMs", ""),
                "visionFreshAtRel": d.get("visionFreshAtRel", ""),
            })
        elif "Release tempo:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({"mode": d.get("mode", ""), "bucket": d.get("bucket", "")})
        elif "Shot outcome:" in ln:
            d = _kv_all(ln)
            shots[d.get("seq", "?")].update({"verdict": d.get("verdict", ""), "errorMs": d.get("errorMs", "")})
            outcome[d.get("seq", "?")] = d
        elif "Watchdog trip" in ln or "Frame feed stalled" in ln:
            events.append((ln[:24], "WATCHDOG", ln.strip()[:150]))
        elif "Sidecar restart scheduled" in ln or "restarting detection sidecar" in ln:
            events.append((ln[:24], "RESTART", ln.strip()[:150]))
        elif "Capture degraded" in ln:
            events.append((ln[:24], "DEGRADED", ln.strip()[:150]))
        elif "SAFE MODE" in ln:
            events.append((ln[:24], "SAFEMODE", ln.strip()[:150]))

    return {
        "env": env_line, "timing_mode": timing_mode, "grader_frozen": grader_frozen,
        "shots": dict(shots), "rejects": dict(rejects), "detdiag_lines": detdiag_n,
        "uniqfps_zero": uniqfps_zero_runs, "events": events,
    }


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def report(a, json_out=None):
    shots = a["shots"]
    seqs = sorted(shots, key=lambda s: _num(s, 1e9) or 1e9)
    print("=" * 100)
    print("ORION SESSION REPORT")
    print("=" * 100)
    if a["timing_mode"]:
        print(f"timing : {a['timing_mode']}")
    if a["env"]:
        safe_keys = {"ORION_METER_LOCATOR", "ORION_LOC_ROI", "ORION_CAPTURE_CARD", "LOCATOR_MODEL"}
        bits = [t for t in a["env"].split() if t.split("=", 1)[0] in safe_keys]
        print(f"env    : {' '.join(bits) if bits else '(see log)'}")
    print(f"grader : {'FROZEN (verdicts are NOT timing measurements)' if a['grader_frozen'] else 'active'}")
    print()

    hdr = (f"{'seq':>4} {'shot':<12} {'mode':<10} {'code':<19} {'hold_ms':>7} {'fill@rel':>8} "
           f"{'fresh':>5} {'stale':>5} {'1stFresh':>8} {'blind':>5} {'anchOK':>6} {'tipDefer':>8}")
    print(hdr)
    print("-" * len(hdr))
    blind = 0
    fresh_tot, first_fresh = [], []
    for s in seqs:
        d = shots[s]
        if "code" not in d and "fresh" not in d:
            continue
        b = d.get("blindFire", "")
        blind += 1 if b == "1" else 0
        f = _num(d.get("fresh"))
        if f is not None:
            fresh_tot.append(f)
        ff = _num(d.get("firstFreshMs"))
        if ff is not None and ff >= 0:
            first_fresh.append(ff)
        print(f"{s:>4} {d.get('shot','?')[:12]:<12} {d.get('mode','')[:10]:<10} {d.get('code','?')[:19]:<19} "
              f"{d.get('holdToRelMs',''):>7} {d.get('fillAtRel',''):>8} {d.get('fresh',''):>5} "
              f"{d.get('staleMem',''):>5} {d.get('firstFreshMs',''):>8} {b:>5} "
              f"{d.get('anchorValid',''):>6} {d.get('tipGateDeferMs',''):>8}")

    n = sum(1 for s in seqs if "code" in shots[s] or "fresh" in shots[s])
    print()
    print(f"shots={n}  blindFire={blind} ({(100.0*blind/max(1,n)):.0f}%)  "
          f"avg fresh/shot={(sum(fresh_tot)/max(1,len(fresh_tot))):.1f}  "
          f"firstFresh ms: min={min(first_fresh) if first_fresh else '-'} "
          f"median={sorted(first_fresh)[len(first_fresh)//2] if first_fresh else '-'} "
          f"max={max(first_fresh) if first_fresh else '-'}")
    print(f"DETDIAG lines={a['detdiag_lines']}  rejects={a['rejects']}")
    if a["grader_frozen"]:
        verd = Counter(shots[s].get("verdict", "") for s in seqs if shots[s].get("verdict"))
        if verd:
            print(f"verdicts (UNGRADED -- frozen self-grade, ignore magnitudes): {dict(verd)}")
    if a["events"]:
        print("\nEVENTS:")
        for ts, kind, detail in a["events"]:
            print(f"  [{kind}] {detail}")

    problems = []
    if blind > 0:
        problems.append(f"{blind} blind-fire shot(s) -- detection lost during those holds")
    if any(k == "WATCHDOG" or k == "RESTART" or k == "SAFEMODE" for _, k, _ in a["events"]):
        problems.append("capture watchdog/restart event(s) occurred")
    slow_acq = [v for v in first_fresh if v > 350]
    if slow_acq:
        problems.append(f"{len(slow_acq)} shot(s) acquired later than 350ms after hold start")
    print()
    if problems:
        print("VERDICT: ATTENTION")
        for p in problems:
            print(f"  - {p}")
        print("  -> dump frames next run (Launch Orion (capture).local.bat) and replay-diagnose:")
        print("     .venv311\\Scripts\\python.exe tools\\diagnostics\\replay_framedump.py --session <dump dir>")
    else:
        print("VERDICT: PASS -- no blind fires, no capture events, acquisition prompt on every shot")

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(a, f, indent=1, default=str)
        print(f"\njson -> {json_out}")
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if not os.path.exists(args.log):
        print(f"log not found: {args.log}", file=sys.stderr)
        return 2
    lines, start = parse_last_session(args.log)
    a = analyze(lines)
    return report(a, args.json or None)


if __name__ == "__main__":
    sys.exit(main())
