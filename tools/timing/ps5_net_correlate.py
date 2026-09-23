#!/usr/bin/env python
"""ps5_net_correlate.py -- join PS5<->game-server packet timing to per-shot banner verdicts.

    python tools/timing/ps5_net_correlate.py --capture <session>.txt --meta <session>.meta.json \\
        --records "D:\\NexusVision\\shot_records\\session_20260920_*.jsonl" [--csv out.csv]

WHY THIS EXISTS. The owner shoots wide-open shots in ONLINE MyCourt, so there is no contest and
every shot is the same shot -- yet LATE verdicts cluster (two selection-free sessions, exact runs
tests p=0.014 each, Fisher p=0.0018; lag-1 P(LATE|prev LATE) 0.64 vs 0.12). Roughly 95 engine-side
covariates were tested and none survived correction. Every one of them measures OUR side. A server
sits between the press and the outcome, and network state is the one candidate with natural serial
memory that none of those covariates could see. The console is behind ICS on this PC, so that path
is observable here without touching the console.

WHAT THIS TOOL ANSWERS, in order of how much it would mean:
  1. Does network state differ on LATE shots?  (weakest: a difference could be coincidence)
  2. Does the network metric ITSELF cluster on the same timescale the verdicts do?
  3. Does the late dependence SURVIVE conditioning on it?  (decisive, and the same mediation test
     that killed the green-window hypothesis -- if the dependence survives at matched network
     state, the network is a bystander too.)

WHAT IT WILL NOT DO
  * It will not call a correlation a cause. A server-side or network explanation that fits is still
    circumstantial, and a fix may not exist on our side.
  * It will not treat a null as exoneration without reporting the effect size it could have seen.
  * It reports every metric tested, so a reader can price the multiple comparisons themselves.

HARD BOUNDARY: analysis tooling only; never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import random
import re
import statistics as st
import sys
from collections import Counter, defaultdict

# pktmon etl2txt layout, verified against a real capture on this machine 2026-09-20:
#   * the file is UTF-16, not UTF-8
#   * each packet is TWO lines -- a header line, then a tab-indented detail line
#       [00] 0004.0A5C::2026-09-20 16:28:45.188842000 [Microsoft-Windows-PktMon] PktGroupId 334,
#            PktNumber 1, Appearance 0, Direction Rx, Type Ethernet, Component 11, Edge 1, ...
#            <tab>MAC > MAC, ethertype IPv4 (0x0800), length 126: 192.168.137.126.56783 >
#                 3.227.237.86.30004: UDP, length 84
#   * CRITICAL: the SAME packet is logged at every component it traverses (8 of them here), so the
#     raw line count overstates traffic several-fold. Metrics are computed on ONE component only,
#     otherwise inter-packet gaps and jitter are fiction.
HEADER_PAT = re.compile(
    r"::(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d+).*?"
    r"Direction (\w+).*?Component (\d+),\s*Edge (\d+)")
DETAIL_PAT = re.compile(
    r"length \d+:\s+(\d{1,3}(?:\.\d{1,3}){3})\.(\d+)\s+>\s+(\d{1,3}(?:\.\d{1,3}){3})\.(\d+):"
    r"\s+(\w+),\s+length\s+(\d+)")

WINDOW_BEFORE_MS = 2000   # the press is the anchor; network memory should show up before it
WINDOW_AFTER_MS = 500


def _read_text(path):
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = open(path, "r", encoding=enc, errors="strict").read()
            if "PktMon" in text or "ethertype" in text:
                return text, enc
        except (UnicodeError, UnicodeDecodeError):
            continue
    return open(path, "r", encoding="utf-8", errors="replace").read(), "utf-8(replace)"


def parse_capture(path, component=None, peer=None):
    """-> (rows, meta). rows are (unix_ms, src, dst, nbytes, direction), one entry per packet.

    Deduplicated to a single component. With component=None the busiest one is chosen and reported.
    """
    import time as _time
    text, enc = _read_text(path)
    lines = text.splitlines()
    by_component = defaultdict(list)
    hdr = None
    unparsed, detail_lines = [], 0

    for ln in lines:
        h = HEADER_PAT.search(ln)
        if h:
            y, mo, d, hh, mm, ss, frac, direction, comp, edge = h.groups()
            # pktmon stamps are LOCAL wall-clock, not UTC. mktime interprets them as local and
            # handles DST, which a timegm() here would silently offset by the UTC delta.
            epoch_ms = int(_time.mktime((int(y), int(mo), int(d), int(hh), int(mm), int(ss),
                                         0, 0, -1))) * 1000 + int(frac[:3].ljust(3, "0"))
            hdr = (epoch_ms, direction, comp, edge)
            continue
        dt = DETAIL_PAT.search(ln)
        if dt:
            detail_lines += 1
            if hdr is None:
                continue
            src, _sp, dst, dp, proto, nbytes = dt.groups()
            epoch_ms, direction, comp, edge = hdr
            if peer and peer not in (src, dst):
                continue
            # (component, edge) is the physical observation point. The same packet appears at
            # several components AND at both edges of one component, so both must key the dedup.
            by_component[f"{comp}/{edge}"].append((epoch_ms, src, dst, int(nbytes), direction))
            hdr = None
        elif ln.strip() and len(unparsed) < 5 and "ethertype" in ln:
            unparsed.append(ln.strip()[:160])

    if not by_component:
        raise SystemExit(
            f"no packets parsed from {path} (encoding {enc}, {detail_lines} detail lines seen). "
            + ("Sample unparsed:\n  " + "\n  ".join(unparsed) if unparsed else
               "No 'ethertype' lines at all -- is this a pktmon etl2txt file?"))

    # Prefer an observation point that sees BOTH directions -- a one-way vantage cannot show a
    # stall in the return path, which is exactly the thing we are looking for.
    def score(c):
        rows_ = by_component[c]
        return (len({r[4] for r in rows_}), len(rows_))

    chosen = component or max(by_component, key=score)
    if chosen not in by_component:
        raise SystemExit(f"component {chosen} not in capture; present: "
                         f"{sorted(by_component, key=lambda c: -len(by_component[c]))}")
    rows = sorted(by_component[chosen])
    meta = {"encoding": enc, "detail_lines": detail_lines, "component": chosen,
            "components": {c: len(v) for c, v in by_component.items()},
            "dedup_ratio": round(detail_lines / max(len(rows), 1), 2)}
    return rows, meta


def load_records(patterns):
    shots = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            session = path.replace("\\", "/").rsplit("/", 1)[-1]
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    verdict = ((rec.get("banner") or {}).get("timing") or "").upper()
                    press = rec.get("press_ts_ms") or rec.get("press_ts")
                    if verdict in ("EXCELLENT", "LATE", "EARLY") and press:
                        shots.append({"session": session, "seq": rec.get("seq"),
                                      "press_ms": int(press), "verdict": verdict})
    shots.sort(key=lambda s: (s["session"], s["seq"] if s["seq"] is not None else s["press_ms"]))
    return shots


def window_metrics(rows, t0, t1, console_ip):
    lo = _bisect(rows, t0)
    pkts = []
    while lo < len(rows) and rows[lo][0] <= t1:
        pkts.append(rows[lo])
        lo += 1
    if len(pkts) < 2:
        return None
    ts = [p[0] for p in pkts]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    up = sum(1 for p in pkts if p[1] == console_ip)
    return {
        "pkts": len(pkts),
        "bytes": sum(p[3] for p in pkts),
        "max_gap_ms": max(gaps),
        "p90_gap_ms": sorted(gaps)[int(0.9 * (len(gaps) - 1))],
        "median_gap_ms": st.median(gaps),
        "jitter_mad_ms": st.median([abs(g - st.median(gaps)) for g in gaps]),
        "up_frac": up / len(pkts),
    }


def _bisect(rows, t):
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def perm_diff(groups_a, groups_b, n=20000, seed=7):
    """Two-sided permutation test on the mean difference. Returns (diff, p) or (None, None)."""
    if len(groups_a) < 2 or len(groups_b) < 2:
        return None, None
    obs = st.mean(groups_a) - st.mean(groups_b)
    pool = list(groups_a) + list(groups_b)
    k = len(groups_a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(st.mean(pool[:k]) - st.mean(pool[k:])) >= abs(obs) - 1e-12:
            hits += 1
    return obs, (hits + 1) / (n + 1)


def lag1_autocorr(series, n=20000, seed=11):
    """Permutation p for lag-1 autocorrelation -- does the METRIC itself have memory?"""
    if len(series) < 6:
        return None, None
    def r(x):
        m = st.mean(x)
        num = sum((a - m) * (b - m) for a, b in zip(x, x[1:]))
        den = sum((v - m) ** 2 for v in x)
        return num / den if den else 0.0
    obs = r(series)
    rng = random.Random(seed)
    pool = list(series)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if r(pool) >= obs:
            hits += 1
    return obs, (hits + 1) / (n + 1)


def stratified_lag1(shots, strata_key, n=20000, seed=13):
    """Does LATE-follows-LATE survive when verdicts are permuted WITHIN network-state strata?

    This is the decisive test, and the same one that retired the green-window hypothesis.
    """
    pairs = [(a, b) for a, b in zip(shots, shots[1:]) if a["session"] == b["session"]]
    if len(pairs) < 8:
        return None
    def delta(_unused=None):
        idx = {id(s): i for i, s in enumerate(shots)}
        late = [s["verdict"] == "LATE" for s in shots]
        pl = [late[idx[id(b)]] for (a, b) in pairs if late[idx[id(a)]]]
        pn = [late[idx[id(b)]] for (a, b) in pairs if not late[idx[id(a)]]]
        if not pl or not pn:
            return None
        return sum(pl) / len(pl) - sum(pn) / len(pn)
    obs = delta(None)
    if obs is None:
        return None
    buckets = defaultdict(list)
    for s in shots:
        buckets[(s["session"], s.get(strata_key))].append(s)
    rng = random.Random(seed)
    hits = 0
    original = [s["verdict"] for s in shots]
    try:
        for _ in range(n):
            for group in buckets.values():
                vs = [g["verdict"] for g in group]
                rng.shuffle(vs)
                for g, v in zip(group, vs):
                    g["verdict"] = v
            d = delta(None)
            if d is not None and d >= obs:
                hits += 1
    finally:
        for s, v in zip(shots, original):
            s["verdict"] = v
    return obs, (hits + 1) / (n + 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--capture", required=True, help="pktmon etl2txt output")
    ap.add_argument("--meta", help="<session>.meta.json written by ps5_net_capture.ps1")
    ap.add_argument("--records", nargs="+", required=True, help="shot_records jsonl glob(s)")
    ap.add_argument("--console-ip", help="override the console address")
    ap.add_argument("--peer", help="game-server address; only packets touching it are kept")
    ap.add_argument("--component", help="pktmon 'component/edge' vantage point (default: the one "
                                        "seeing both directions, else the busiest)")
    ap.add_argument("--before-ms", type=int, default=WINDOW_BEFORE_MS)
    ap.add_argument("--after-ms", type=int, default=WINDOW_AFTER_MS)
    ap.add_argument("--csv")
    args = ap.parse_args(argv)

    console_ip = args.console_ip
    if args.meta:
        m = json.load(open(args.meta, encoding="utf-8-sig"))
        console_ip = console_ip or m.get("ps5")
    if not console_ip:
        ap.error("need --console-ip or --meta")

    rows, cap_meta = parse_capture(args.capture, component=args.component, peer=args.peer)
    shots = load_records(args.records)
    print(f"capture         : {args.capture}")
    print(f"  encoding      : {cap_meta['encoding']}")
    print(f"  detail lines  : {cap_meta['detail_lines']}  (each packet is logged at several "
          f"components; raw lines overstate traffic)")
    print(f"  vantage point : component/edge {cap_meta['component']}  "
          f"(dedup ratio {cap_meta['dedup_ratio']}x)")
    print(f"capture packets : {len(rows)}")
    print(f"graded shots    : {len(shots)}  " + str(Counter(s['verdict'] for s in shots)))
    if not rows or not shots:
        print("nothing to correlate")
        return 1

    covered = 0
    for s in shots:
        m = window_metrics(rows, s["press_ms"] - args.before_ms, s["press_ms"] + args.after_ms,
                           console_ip)
        s["net"] = m
        covered += bool(m)
    print(f"shots with net  : {covered} of {len(shots)}"
          + ("" if covered == len(shots) else "   <-- uncovered shots are EXCLUDED, not zero-filled"))
    usable = [s for s in shots if s["net"]]
    if len(usable) < 8:
        print("\ntoo few shots fall inside the capture window to say anything. Not a null result -- "
              "a coverage failure. Check that the capture spans the session and that --meta's clock "
              "anchor is right.")
        return 1

    metrics = ["pkts", "bytes", "max_gap_ms", "p90_gap_ms", "median_gap_ms", "jitter_mad_ms",
               "up_frac"]

    print("\n== 1. does network state differ on LATE shots? ==")
    print(f"{'metric':<16}{'LATE med':>12}{'other med':>12}{'diff':>12}{'perm p':>10}")
    for k in metrics:
        a = [s["net"][k] for s in usable if s["verdict"] == "LATE"]
        b = [s["net"][k] for s in usable if s["verdict"] != "LATE"]
        if len(a) < 2 or len(b) < 2:
            continue
        diff, p = perm_diff(a, b)
        print(f"{k:<16}{st.median(a):>12.2f}{st.median(b):>12.2f}{diff:>12.2f}{p:>10.3f}")
    print(f"{len(metrics)} metrics tested; at alpha=0.05 expect ~{0.05*len(metrics):.1f} hits by "
          "chance. Do not read a single small p as a finding.")

    print("\n== 2. does the network metric itself CLUSTER? ==")
    print("(if the verdicts have memory and the network has none, the network is not the source)")
    for k in metrics:
        per_session = defaultdict(list)
        for s in usable:
            per_session[s["session"]].append(s["net"][k])
        for sess, series in sorted(per_session.items()):
            r, p = lag1_autocorr(series)
            if r is not None:
                print(f"  {k:<16} {sess:<34} lag-1 r={r:+.2f}  p={p:.3f}  n={len(series)}")

    print("\n== 3. DECISIVE: does LATE-follows-LATE survive at matched network state? ==")
    for k in ("max_gap_ms", "jitter_mad_ms", "pkts"):
        vals = sorted(s["net"][k] for s in usable)
        cut = vals[len(vals) // 2]
        for s in usable:
            s["_stratum"] = "hi" if s["net"][k] > cut else "lo"
        res = stratified_lag1(usable, "_stratum")
        if res is None:
            print(f"  {k:<16} too few within-session consecutive pairs to test")
            continue
        obs, p = res
        print(f"  {k:<16} delta P(LATE|prev LATE) - P(LATE|prev other) = {obs:+.2f}   "
              f"permuted WITHIN {k} strata: p={p:.4f}")
    print("\n  A dependence that survives here is NOT explained by that metric -- the same way the"
          "\n  green window was retired. A dependence that collapses is a lead, not a proof.")

    if args.csv and usable:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["session", "seq", "press_ms", "verdict"] + metrics)
            for s in usable:
                w.writerow([s["session"], s["seq"], s["press_ms"], s["verdict"]]
                           + [s["net"][k] for k in metrics])
        print(f"\nwrote {args.csv}")

    print("\nCorrelation is not cause. If network state tracks the lates this is circumstantial and "
          "a fix may not exist on our side; if it does not, the memory is still unlocated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
