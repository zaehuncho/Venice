#!/usr/bin/env python
"""Join a net_probe capture to the shot records and split the online grading variance.

Companion to tools/timing/net_probe.py (2026-09-22 variance hunt). For every banner-graded shot
inside the capture window it measures, on the Windows wall clock both sides share:

  rp_press / rp_release   first PC->PS5 Remote Play datagram at/after the press / the release
  send_lag_*              PS5 -> court: first outbound packet after that datagram (the PS5's own
                          send-tick phase + processing, i.e. how long the input sat in the console)
  send_lag_delta          send_lag_release - send_lag_press  (what the hold GAINS inside the PS5)
  rtt_court_* / rtt_gw_*  ICMP RTT nearest the press / release (court and home gateway)
  rtt_court_delta         release minus press (uplink drift across the hold)
  srv_period / srv_phase  server->PS5 inbound tick period (mode of inter-arrivals) and the phase of
                          the release's outbound packet inside that tick

Then it refits the ordered-probit grade model from docs/variance/ONLINE_GRADING_CLOCK_2026-09-22.md
(grade = hold - (1-beta)*onset + noise) with each network feature added, and reports how much of the
unexplained sigma each one removes. A feature that removes a lot is the jackpot lever.

    python tools/timing/net_join.py                      # newest capture, all overlapping records
    python tools/timing/net_join.py --capture D:\\NexusVision\\netcap\\20260922_190000
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

NETCAP = r"D:\NexusVision\netcap"
RECORDS = r"D:\NexusVision\shot_records"
PC_ICS = "192.168.137.1"


def load_capture(capdir: Path):
    meta = json.loads((capdir / "meta.json").read_text())
    ps5 = meta.get("ps5", "192.168.137.81")
    court = meta.get("court") or ""
    rp_to_ps5, ps5_out, srv_in = [], [], []
    with open(capdir / "packets.csv", newline="") as f:
        for r in csv.DictReader(f):
            t = float(r["wall_ms"])
            if r["dir"] == "in" and r["peer"] == PC_ICS:
                rp_to_ps5.append(t)
            elif court and r["dir"] == "out" and r["peer"] == court:
                ps5_out.append((t, int(r["len"])))
            elif court and r["dir"] == "in" and r["peer"] == court:
                srv_in.append(t)
    pings = defaultdict(list)
    with open(capdir / "pings.csv", newline="") as f:
        for r in csv.DictReader(f):
            if r["rtt_ms"]:
                pings[r["target"]].append((float(r["wall_ms"]), float(r["rtt_ms"])))
    for k in pings:
        pings[k].sort()
    return meta, court, sorted(rp_to_ps5), sorted(ps5_out), sorted(srv_in), pings


def cadence_report(label, ts, sizes=None):
    """How a stream is clocked: modal inter-arrival (its tick), how regular it is, and packet sizes.
    The PS5 sends to the server on its own cadence, so 'first outbound packet after the input' only
    measures the phase of that cadence; the input-bearing packet has to be told apart some other way
    (size is the first candidate - verified on a synthetic session, 2026-09-22)."""
    gaps = [b - a for a, b in zip(ts, ts[1:]) if 0.2 < b - a < 250.0]
    if len(gaps) < 20:
        print(f"  {label}: too few packets")
        return None
    mode = Counter(round(g) for g in gaps).most_common(1)[0][0]
    on_tick = sum(1 for g in gaps if abs(g - mode) <= 2) / len(gaps)
    line = f"  {label}: modal gap {mode} ms ({1000 / max(mode, 1):.0f} Hz), {on_tick:.0%} of gaps on it, p90 gap {sorted(gaps)[int(.9 * len(gaps))]:.1f} ms"
    if sizes:
        top = Counter(sizes).most_common(4)
        line += f"; sizes {top}"
    print(line)
    return Counter(sizes).most_common(1)[0][0] if sizes else None


def first_at_or_after(xs, t, within):
    i = bisect.bisect_left(xs, t)
    return xs[i] if i < len(xs) and xs[i] - t <= within else None


def nearest_ping(series, t, within=150.0):
    if not series:
        return None
    ts = [p[0] for p in series]
    i = bisect.bisect_left(ts, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(series) and abs(series[j][0] - t) <= within:
            if best is None or abs(series[j][0] - t) < abs(best[0] - t):
                best = series[j]
    return best[1] if best else None


def server_tick(srv_in):
    gaps = [b - a for a, b in zip(srv_in, srv_in[1:]) if 1.0 < b - a < 200.0]
    if len(gaps) < 50:
        return None
    hist = Counter(round(g) for g in gaps)
    mode, _ = hist.most_common(1)[0]
    near = [g for g in gaps if abs(g - mode) <= 3]
    return st.median(near) if near else float(mode)


def load_records(t0, t1, records_dir=RECORDS):
    out = []
    for f in glob.glob(str(Path(records_dir) / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            b = d.get("banner") or {}
            p = d.get("press_ts_ms")
            if not p or not (t0 <= p <= t1) or b.get("timing") not in ("EARLY", "EXCELLENT", "LATE"):
                continue
            hold = d.get("release_after_press_ms")
            onset = d.get("onset_ms")
            if hold is None or onset is None:
                continue
            out.append(dict(session=d["session"], shot_type=d.get("shot_type"), press=p,
                            release=p + hold, hold=hold, onset=onset, v=b["timing"]))
    return out


def first_sized_after(out, t, within, modal):
    i = bisect.bisect_left(out, (t, -1))
    while i < len(out) and out[i][0] - t <= within:
        if modal is None or out[i][1] != modal:
            return out[i][0]
        i += 1
    return None


def features(rec, rp, out, srv_in, pings, period, modal=None):
    f = {}
    out_t = [x[0] for x in out]
    rpp = first_at_or_after(rp, rec["press"] - 2.0, 40.0)
    rpr = first_at_or_after(rp, rec["release"] - 2.0, 40.0)
    if rpp is not None:
        o = first_at_or_after(out_t, rpp, 120.0)
        f["send_phase_press"] = o - rpp if o is not None else None
        o2 = first_sized_after(out, rpp, 120.0, modal)
        f["send_lag_press"] = o2 - rpp if o2 is not None else None
    if rpr is not None:
        o = first_at_or_after(out_t, rpr, 120.0)
        f["send_phase_release"] = o - rpr if o is not None else None
        o2 = first_sized_after(out, rpr, 120.0, modal)
        f["send_lag_release"] = o2 - rpr if o2 is not None else None
        if o is not None and period:
            j = bisect.bisect_right(srv_in, o) - 1
            if j >= 0:
                f["srv_phase"] = ((o - srv_in[j]) % period) / period
    if f.get("send_lag_press") is not None and f.get("send_lag_release") is not None:
        f["send_lag_delta"] = f["send_lag_release"] - f["send_lag_press"]
    for tgt in ("court", "gateway"):
        a = nearest_ping(pings.get(tgt, []), rec["press"])
        b = nearest_ping(pings.get(tgt, []), rec["release"])
        f[f"rtt_{tgt}_press"], f[f"rtt_{tgt}_release"] = a, b
        if a is not None and b is not None:
            f[f"rtt_{tgt}_delta"] = b - a
    return f


def fit(rows, extra):
    """Ordered probit: latent = bh*hold + bo*onset + sum(bx*x). Returns (sigma_ms, beta, coefs, nll)."""
    import numpy as np
    from scipy.optimize import minimize
    from scipy.stats import norm
    h = np.array([r["hold_c"] for r in rows]); o = np.array([r["onset_c"] for r in rows])
    X = np.array([[r[k] for k in extra] for r in rows]) if extra else np.zeros((len(rows), 0))
    y = np.array([{"EARLY": 0, "EXCELLENT": 1, "LATE": 2}[r["v"]] for r in rows])

    def nll(p):
        bh, bo, c0, d1 = p[:4]; bx = p[4:]
        z = bh * h + bo * o + (X @ bx if len(bx) else 0.0)
        c1 = c0 + math.exp(d1)
        p0 = norm.cdf(c0 - z); p2 = 1 - norm.cdf(c1 - z); p1 = np.clip(1 - p0 - p2, 1e-9, 1)
        pr = np.where(y == 0, np.clip(p0, 1e-9, 1), np.where(y == 2, np.clip(p2, 1e-9, 1), p1))
        return -np.log(pr).sum()
    x0 = [0.02, -0.01, -1.0, 0.5] + [0.0] * len(extra)
    r = minimize(nll, x0, method="Nelder-Mead", options=dict(maxiter=40000, xatol=1e-8, fatol=1e-8))
    bh, bo = r.x[0], r.x[1]
    coefs = {k: r.x[4 + i] / bh for i, k in enumerate(extra)} if bh > 0 else {}
    return (1 / bh if bh > 0 else float("nan")), (1 + bo / bh if bh > 0 else float("nan")), coefs, r.fun


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", default="")
    ap.add_argument("--records", default=RECORDS)
    ap.add_argument("--standstill-only", action="store_true", default=True)
    args = ap.parse_args()
    capdir = Path(args.capture) if args.capture else max(Path(NETCAP).glob("*/"), key=lambda p: p.name)
    meta, court, rp, out, srv_in, pings = load_capture(capdir)
    print(f"capture {capdir.name}: court={court or '?'}  rp->ps5={len(rp)}  ps5->court={len(out)}  "
          f"court->ps5={len(srv_in)}  pings court={len(pings.get('court', []))} gw={len(pings.get('gateway', []))}")
    if not rp:
        print("no Remote Play datagrams captured: wrong interface or the PS5 was not streaming")
        return 1
    print("stream cadence:")
    cadence_report("PC -> PS5 (Remote Play input)", rp)
    modal = cadence_report("PS5 -> court", [x[0] for x in out], [x[1] for x in out])
    cadence_report("court -> PS5", srv_in)
    period = server_tick(srv_in)
    print(f"server inbound tick: {period:.2f} ms ({1000 / period:.1f} Hz)" if period else "server tick: not enough inbound packets")
    recs = load_records(rp[0], rp[-1], args.records)
    if args.standstill_only:
        recs = [r for r in recs if r["shot_type"] == "Standstill"]
    rows = []
    for r in recs:
        r.update(features(r, rp, out, srv_in, pings, period, modal))
        rows.append(r)
    print(f"graded standstills in window: {len(rows)}")
    if len(rows) < 30:
        print("need >= 30 graded standstills for a fit; per-shot features written anyway")
    by = defaultdict(list)
    for r in rows:
        by[r["session"]].append(r)
    for s, g in by.items():
        mo = st.median(x["onset"] for x in g); mh = st.median(x["hold"] for x in g)
        for x in g:
            x["onset_c"], x["hold_c"] = x["onset"] - mo, x["hold"] - mh
    outcsv = capdir / "joined.csv"
    keys = ["session", "press", "hold", "onset", "v", "send_phase_press", "send_phase_release",
            "send_lag_press", "send_lag_release",
            "send_lag_delta", "srv_phase", "rtt_court_press", "rtt_court_release", "rtt_court_delta",
            "rtt_gateway_press", "rtt_gateway_release", "rtt_gateway_delta"]
    with open(outcsv, "w", newline="") as f:
        w = csv.writer(f); w.writerow(keys)
        for r in rows:
            w.writerow([r.get(k) for k in keys])
    print(f"per-shot features -> {outcsv}")
    for k in keys[5:]:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if vals:
            print(f"  {k:22s} n={len(vals):4d}  median {st.median(vals):8.2f}  sd {st.pstdev(vals):7.2f}")
    if len(rows) < 30:
        return 0
    base = [r for r in rows if all(r.get(k) is not None for k in ("send_lag_delta", "rtt_court_delta"))] or rows
    sig0, beta0, _, nll0 = fit(base, [])
    print(f"\nbaseline (n={len(base)}): sigma {sig0:.1f} ms, beta {beta0:.2f}")
    for feat in ("send_lag_delta", "send_lag_release", "rtt_court_delta", "rtt_gateway_delta", "srv_phase"):
        sub = [r for r in base if r.get(feat) is not None]
        if len(sub) < 30:
            continue
        s_b, _, _, n_b = fit(sub, [])
        s_f, b_f, coef, n_f = fit(sub, [feat])
        lr = 2 * (n_b - n_f)
        print(f"  + {feat:18s} n={len(sub):4d}  sigma {s_b:5.1f} -> {s_f:5.1f} ms  coef {coef.get(feat, float('nan')):+.2f} ms/unit"
              f"  LR chi2 {lr:6.2f} (p<0.05 if >3.84)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
