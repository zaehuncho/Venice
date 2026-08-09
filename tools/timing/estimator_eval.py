"""Q3: is median-of-20 (with n/window shrinkage) the right phase-constant
estimator?  Replays every alternative estimator over the REAL accepted PHASE
SAMPLE sequences from the logs, session by session, and scores the aim error
each would have produced.  Read-only.

    python tools/timing/estimator_eval.py

Truth model: the constant genuinely drifts within/between sessions, so "truth"
for shot i is the centered local median (window 15) of artifact-cleaned samples
-- an oracle smoother that tracks real drift but averages out noise.  A second
scoring against the global segment median is printed for contrast (assumes no
real drift; penalises any tracking).

Artifact cleaning: stop-reopen artifacts are one-sided (+30..+70ms re-dates).
`stop_reopen_corroborate` is armed going forward, so the FUTURE input stream is
the cleaned one; estimators are scored on both raw (past) and cleaned (future)
streams.  Cleaning rule: sample > running-median + 25ms -> artifact (mirrors
what the corroboration fix removes); slow-meter earlies (the defer guard, also
in flight) are approximated by dropping samples < running-median - 25ms in the
'both guards' variant.
"""

import math
import os
import re
import sys
from datetime import datetime, timezone

import numpy as np

LOGS = ["logs/orion_native.log.1", "logs/orion_native.log"]

RE_PHASE = re.compile(
    r"^(\S+?)Z\s+PHASE SAMPLE: raw_ms=([\d.]+) normalized_ms=([\d.]+) "
    r"anchor_pct=([\d.]+) accepted=(\d).*?shipped_const_ms=([\d.]+) "
    r"effective_const_ms=([\d.]+) shot_type=(.+)$")
RE_TIPMEAS = re.compile(
    r"^(\S+?)Z\s+Tip phase measurement: physical_median_ms=([\d.]+) n=(\d+) "
    r"sample_ms=([\d.]+)")


def iso_ms(s):
    dt = datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def parse():
    samples, meas = [], []
    seen = set()
    for lg in LOGS:
        if not os.path.exists(lg):
            continue
        with open(lg, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RE_PHASE.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    key = (round(ts), m.group(2))
                    if key in seen:
                        continue
                    seen.add(key)
                    samples.append(dict(
                        ts=ts, raw=float(m.group(2)), y=float(m.group(3)),
                        rung=float(m.group(4)), acc=int(m.group(5)),
                        const=float(m.group(6)), eff=float(m.group(7)),
                        type=m.group(8).strip()))
                    continue
                m = RE_TIPMEAS.match(line)
                if m:
                    meas.append(dict(ts=iso_ms(m.group(1)), med=float(m.group(2)),
                                     n=int(m.group(3)), samp=float(m.group(4))))
    samples.sort(key=lambda s: s["ts"])
    meas.sort(key=lambda s: s["ts"])
    return samples, meas


def segments(samples, gap_min=20.0):
    segs, cur = [], []
    for s in samples:
        if cur and (s["ts"] - cur[-1]["ts"] > gap_min * 60000.0
                    or s["const"] != cur[-1]["const"]):
            segs.append(cur)
            cur = []
        cur.append(s)
    if cur:
        segs.append(cur)
    return [g for g in segs if sum(x["acc"] for x in g) >= 25]


def clean_stream(ys, hi=25.0, lo=None):
    """One-sided (or two-sided) artifact filter vs the running median-of-20."""
    out, kept = [], []
    for v in ys:
        if len(kept) >= 5:
            med = float(np.median(kept[-20:]))
            if v - med > hi:
                out.append((v, False))
                continue
            if lo is not None and med - v > lo:
                out.append((v, False))
                continue
        kept.append(v)
        out.append((v, True))
    return out


class MedianK:
    def __init__(self, k, prior):
        self.k, self.buf, self.prior = k, [], prior

    def update(self, v):
        self.buf.append(v)
        if len(self.buf) > self.k:
            self.buf.pop(0)
        n = len(self.buf)
        med = float(np.median(self.buf))
        if n < self.k:
            return self.prior + (med - self.prior) * n / self.k
        return med


class TrimmedMeanK:
    def __init__(self, k, prior, trim=0.2):
        self.k, self.buf, self.prior, self.trim = k, [], prior, trim

    def update(self, v):
        self.buf.append(v)
        if len(self.buf) > self.k:
            self.buf.pop(0)
        n = len(self.buf)
        arr = np.sort(np.array(self.buf))
        t = int(self.trim * n)
        est = float(arr[t:n - t].mean()) if n - 2 * t >= 1 else float(arr.mean())
        if n < self.k:
            return self.prior + (est - self.prior) * n / self.k
        return est


class MeanK(TrimmedMeanK):
    def __init__(self, k, prior):
        super().__init__(k, prior, trim=0.0)


class EWMA:
    def __init__(self, alpha, prior):
        self.a, self.est = alpha, prior

    def update(self, v):
        self.est += self.a * (v - self.est)
        return self.est


class HuberEWMA:
    """EWMA on innovations clipped at +-c ms: tracks drift, shrugs artifacts."""

    def __init__(self, alpha, prior, c=10.0):
        self.a, self.est, self.c = alpha, prior, c

    def update(self, v):
        inn = max(-self.c, min(self.c, v - self.est))
        self.est += self.a * inn
        return self.est


def oracle_truth(ys_clean_vals, idx_map, n_total, win=15):
    """Centered local median over clean values, mapped back to every index."""
    truth = np.full(n_total, np.nan)
    vals = np.array(ys_clean_vals)
    for j, i in enumerate(idx_map):
        lo = max(0, j - win // 2)
        hi = min(len(vals), j + win // 2 + 1)
        truth[i] = float(np.median(vals[lo:hi]))
    # fill gaps (artifact positions) with nearest clean truth
    last = np.nan
    for i in range(n_total):
        if np.isnan(truth[i]):
            truth[i] = last
        else:
            last = truth[i]
    return truth


def run_segment(seg, label, validate_meas=None):
    ys = [s["y"] for s in seg if s["acc"] == 1]
    ts = [s["ts"] for s in seg if s["acc"] == 1]
    n = len(ys)
    flags = clean_stream(ys)
    clean_idx = [i for i, (v, ok) in enumerate(flags) if ok]
    clean_vals = [ys[i] for i in clean_idx]
    n_art = n - len(clean_idx)
    truth = oracle_truth(clean_vals, clean_idx, n)
    gmed = float(np.median(clean_vals))
    t0 = datetime.fromtimestamp(ts[0] / 1000, timezone.utc).strftime("%m-%dT%H:%M")
    dur = (ts[-1] - ts[0]) / 60000.0
    # drift diagnostics on the oracle
    tr_rng = float(np.nanmax(truth) - np.nanmin(truth))
    x = (np.array(ts) - ts[0]) / 60000.0
    cvals = np.array(clean_vals)
    cx = x[clean_idx]
    if len(cvals) > 10 and cx.std() > 0:
        A = np.column_stack([np.ones(len(cx)), cx])
        coef, *_ = np.linalg.lstsq(A, cvals, rcond=None)
        resid = cvals - A @ coef
        se = math.sqrt(np.sum(resid ** 2) / (len(cx) - 2) / np.sum((cx - cx.mean()) ** 2))
        drift = "%+.2f+-%.2f ms/min" % (coef[1], se)
        resid_sd = resid.std(ddof=1)
    else:
        drift, resid_sd = "-", float("nan")
    print("\n== segment %s  n_acc=%d (artifacts %d)  dur=%.0f min  const=%s ==" %
          (t0, n, n_art, dur, seg[0]["const"]))
    print("clean med=%.1f  sd=%.2f rSD=%.2f  drift=%s  detrended sd=%.2f  "
          "oracle range=%.1f" %
          (gmed, np.std(cvals, ddof=1), 1.4826 * np.median(np.abs(cvals - gmed)),
           drift, resid_sd, tr_rng))

    prior = truth[0] if np.isfinite(truth[0]) else gmed
    # deliberately offset prior: the engine restores the previous session's
    # value; measure both a matched and a +15ms-wrong prior
    scenarios = {"prior=truth0": prior, "prior+15": prior + 15.0}
    ests = {
        "shipped med20": lambda p: MedianK(20, p),
        "med10": lambda p: MedianK(10, p),
        "med5": lambda p: MedianK(5, p),
        "med40": lambda p: MedianK(40, p),
        "trim20%%mean20": lambda p: TrimmedMeanK(20, p),
        "mean20": lambda p: MeanK(20, p),
        "EWMA.05": lambda p: EWMA(0.05, p),
        "EWMA.10": lambda p: EWMA(0.10, p),
        "EWMA.20": lambda p: EWMA(0.20, p),
        "Huber-EWMA.10": lambda p: HuberEWMA(0.10, p),
        "Huber-EWMA.15": lambda p: HuberEWMA(0.15, p),
    }
    streams = {
        "raw (past)": [v for v, ok in flags],
        "corroborated (future)": [v for v, ok in flags if ok or v < np.median(clean_vals)],
    }
    # 'corroborated' drops only the HIGH artifacts (the fix is one-sided)
    for sname, stream in streams.items():
        # index mapping stream position -> original accepted index for truth
        if sname.startswith("raw"):
            idxs = list(range(n))
        else:
            idxs = [i for i, (v, ok) in enumerate(flags)
                    if ok or v < np.median(clean_vals)]
        print("  -- stream: %s (n=%d) --" % (sname, len(stream)))
        hdr = "  %-15s" % "estimator"
        for sc in scenarios:
            hdr += "  %-12s" % sc
        print(hdr + "   (RMS / p90 |aim err| ms vs oracle)")
        for ename, mk in ests.items():
            row = "  %-15s" % (ename % () if "%" in ename else ename)
            for sc, pr in scenarios.items():
                est = mk(pr)
                errs = []
                cur = pr
                for j, v in enumerate(stream):
                    i = idxs[j]
                    if np.isfinite(truth[i]) and j >= 3:
                        errs.append(cur - truth[i])   # aim used BEFORE sample j
                    cur = est.update(v)
                errs = np.array(errs)
                row += "  %5.2f/%5.2f " % (
                    math.sqrt(np.mean(errs ** 2)),
                    np.percentile(np.abs(errs), 90))
            print(row)
    # validate simulator vs engine's own published medians
    if validate_meas:
        mism = 0
        sim = MedianK(20, float("nan"))
        # engine shrink base is the restored prior, unknown here; compare only
        # full-window (n>=20) publications where shrinkage is inactive
        buf = []
        pubs = [m for m in validate_meas if ts[0] - 1000 <= m["ts"] <= ts[-1] + 1000]
        k = 0
        for v in ys:
            buf.append(v)
            if len(buf) > 20:
                buf.pop(0)
            if k < len(pubs) and abs(pubs[k]["samp"] - v) < 0.05:
                if pubs[k]["n"] >= 20:
                    med = float(np.median(buf))
                    if abs(med - pubs[k]["med"]) > 0.11:
                        mism += 1
                k += 1
        print("  simulator-vs-engine full-window median mismatches: %d (of %d pubs)"
              % (mism, len(pubs)))


def main():
    np.seterr(all="ignore")
    samples, meas = parse()
    print("total PHASE SAMPLE: %d  (accepted %d)  Tip-phase publications: %d"
          % (len(samples), sum(s["acc"] for s in samples), len(meas)))
    segs = segments(samples)
    for seg in segs:
        t0 = datetime.fromtimestamp(seg[0]["ts"] / 1000, timezone.utc)
        label = t0.strftime("%m-%dT%H:%M")
        run_segment(seg, label, validate_meas=meas)


if __name__ == "__main__":
    main()
