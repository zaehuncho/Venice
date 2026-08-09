#!/usr/bin/env python
"""
aim_estimator.py -- OFFLINE estimator of the optimal tip-phase AIM from banner
verdicts, with the sample-size arithmetic that says which moves a batch can
actually justify, the aim-vs-decision-budget frontier, and the exact
learning.json value to write.

WHY THIS EXISTS
  The aim (effectiveTipPhaseConstantMs = learnedPhysical + aim-preservation
  offset) is the single number that decides early-vs-late, and it has been a
  guess that wanders: the in-session learner medians a 20-sample window of the
  bot's OWN landings, so it chases its own noise (measured walk 439.2 -> 431.0
  across one 70-release batch). tip_phase_aim_frozen now holds it still -- but a
  frozen WRONG value is as bad as a drifting one. This tool turns graded banner
  batches into a measured optimum with a confidence interval.

THE PRINCIPLE
  The game's TIMING banner is the only valid outcome instrument. The signal is
  the EARLY/LATE BALANCE, not the green count: model the landing error of shot i
  as Normal(A_i - A*, sigma) where A_i is the effective aim constant the shot
  was fired under (read from the log, because the learner walks mid-session) and
  A* is the aim at which the landing distribution is centred in the green
  window. Verdict probabilities with half-width h:
      P(EARLY) = Phi((-h - mu)/sigma),  P(LATE) = Phi((mu - h)/sigma),
      P(GREEN) = 1 - P(EARLY) - P(LATE),      mu = A_i - A*.
  Maximum likelihood over (A*, h) with sigma supplied; profile-likelihood CI on
  A*. Batches fired at DIFFERENT aims (the walk is, for once, useful) pin A* in
  real milliseconds without leaning on the sigma scale; a single-aim batch pins
  it only through sigma, so the report shows A* across a sigma grid and is
  blunt when the answer depends on it.

HARD BOUNDARY (owner directive)
  Banner-driven CLOSED-LOOP grading is FORBIDDEN. This file is analysis tooling
  only: it reads recorded artifacts (framedump PNGs, orion_native.log,
  banner_reader CSVs) and prints numbers for a HUMAN to apply. It is never
  imported by the engine/orchestrator/sidecar. Do not add any code path by
  which its output can reach the engine without a human typing it.

UNITS / TRANSLATIONS (all verified against AutomationEngine.cpp 2026-08-06)
  effective aim   = learnedPhysical_inmem + AIM_OFFSET(74.0)
  learnedPhysical_inmem = learning.json learned_phase_physical_ms
                          + (58.3 if tip_phase_anchor_base20 else 0)
  =>  learning.json value = effective_aim - 74.0 - (58.3 if base20)
  decision budget = effective aim - actuation_lead_ms (300)
  The 74.0 offset is constant - seed in BOTH regimes (393-319 = 451.3-377.3);
  it is also cross-checked against the aim_offset_ms field of the log's
  "Tip phase measurement" lines when a log is supplied.

USAGE
  # ONE COMMAND from a fresh counted batch (framedump session(s) + engine log):
  python tools/timing/aim_estimator.py pipeline \
      --session logs/diagnostics/framedump/session_20260806_164915 \
      --log logs/orion_native.log --workdir logs/diagnostics/banner_grading \
      --label frozen_batch

  # estimator only, from already-joined CSVs (banner_reader.py join output):
  python tools/timing/aim_estimator.py recommend \
      --joined out/joined_a.csv out/joined_b.csv --log logs/orion_native.log

  # how many graded shots does a move of X ms need?
  python tools/timing/aim_estimator.py power --sigma 9.3 --green-rate 0.92

  # aim vs decision-budget frontier from the log's promotion/reject records:
  python tools/timing/aim_estimator.py frontier --log logs/orion_native.log

  # internal checks (synthetic-recovery, translation round-trip):
  python tools/timing/aim_estimator.py selftest
"""
import argparse
import bisect
import csv
import datetime
import json
import math
import os
import re
import sys

import numpy as np
from scipy.stats import norm, binomtest
from scipy.optimize import minimize

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

# ---- engine constants (mirrored, never imported; verified against source) ----
AIM_OFFSET_MS = 74.0          # tipPhaseConstantMs - tipPhaseSeedPhysicalMs, both regimes
BASE20_SHIFT_MS = 58.3        # kAnchorBase20ShiftMs (AutomationEngine.cpp:50)
DEFAULT_LEAD_MS = 300.0       # settings actuation_lead_ms (user-set)
LEARN_BAND_PHYS = (240.0, 430.0)   # base-30 physical accept band (shifted +58.3 on base-20)

# landing-spread scale. Raw robust SD of accepted PHASE SAMPLE landings measured
# 9.3 ms (n=70, 2026-08-06 counted batch); the stop-dating instrument alone
# contributes ~7.7 ms, so the spread the GAME grades may be as low as
# sqrt(9.3^2 - 7.7^2) ~ 5.2. A* barely moves with sigma near balance, but the
# SIZE of a recommended move scales with it -- so the report shows a grid.
SIGMA_DEFAULT = 9.3
SIGMA_GRID = (5.2, 7.0, 9.3, 12.0)

EARLY_WORDS = ("EARLY", "SLIGHTLY_EARLY", "VERY_EARLY")
LATE_WORDS = ("LATE", "SLIGHTLY_LATE", "VERY_LATE")
GREEN_WORDS = ("EXCELLENT",)


def word_dir(word):
    w = (word or "").strip().upper().replace(" ", "_")
    if w in GREEN_WORDS:
        return "G"
    if w in EARLY_WORDS:
        return "E"
    if w in LATE_WORDS:
        return "L"
    return None


# ---------------------------------------------------------------- log parsing
RE_TPM = re.compile(
    r"^(\S+)\s+Tip phase measurement: physical_median_ms=([\d.]+) n=(\d+) "
    r"sample_ms=([\d.]+) anchor_pct=([\d.]+) aim_offset_ms=([\d.]+) "
    r"effective_const_ms=([\d.]+)")
# NOTE deliberately NOT used for the aim timeline: reservation lines carry
# phase_const_ms for the LADDER RUNG the shot was dated at (effective const
# minus the per-rung secant offset, e.g. 439.2 -> 352.98 at rung 35), so they
# are NOT the aim. Only "Tip phase measurement" lines log the base effective
# constant. (Bug found 2026-08-06: mixing them in polluted the per-shot aims.)
RE_PROM = re.compile(
    r"^(\S+)\s+TIP RESERVATION: disposition=reservation_promoted source=(\S+) "
    r"command_eta_ms=([\d.-]+).*?lead_ms=([\d.]+)")
RE_MISS = re.compile(
    r"^(\S+)\s+TIP DEADLINE DECISION: disposition=rejected_missed source=(\S+) "
    r"tip_eta_ms=(-?[\d.]+).*?lateness_ms=(-?[\d.]+).*?lead_ms=([\d.]+)")
RE_PHASE_SAMPLE = re.compile(
    r"^(\S+)\s+PHASE SAMPLE: raw_ms=([\d.-]+) normalized_ms=([\d.-]+) "
    r"anchor_pct=([\d.]+) accepted=(\d)")


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


class LogFacts(object):
    """Everything the estimator needs from orion_native.log."""

    def __init__(self):
        self.aim_timeline = []      # [(iso_utc, effective_const_ms)] sorted
        self.aim_offsets = []       # aim_offset_ms values seen (cross-check 74.0)
        self.promotions = []        # [(iso, source, command_eta_ms, lead_ms)]
        self.rejects = []           # [(iso, source, lateness_ms, lead_ms)]
        self.landings = []          # [(iso, normalized_ms, accepted)]

    def aim_at(self, iso_utc, stale_s=600.0):
        """Effective aim constant in force at a wall time: the last entry
        BEFORE the query, unless it is stale (previous session -- the learner
        state was re-restored at launch, e.g. a frozen aim), in which case the
        first FRESH entry after is used (the aim moves <1 ms per landing, so a
        near-future entry is the correct session's value). None if nothing is
        within stale_s on either side."""
        if not self.aim_timeline:
            return None
        keys = [t for t, _ in self.aim_timeline]
        tq = _ts(iso_utc)
        i = bisect.bisect_right(keys, iso_utc) - 1
        if i >= 0 and (tq - _ts(keys[i])).total_seconds() <= stale_s:
            return self.aim_timeline[i][1]
        j = i + 1
        if j < len(keys) and (_ts(keys[j]) - tq).total_seconds() <= stale_s:
            return self.aim_timeline[j][1]
        if i >= 0:
            return self.aim_timeline[i][1]   # stale but better than nothing
        return None


def parse_log(paths):
    lf = LogFacts()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_TPM.match(line)
                if m:
                    lf.aim_timeline.append((m.group(1), float(m.group(7))))
                    lf.aim_offsets.append(float(m.group(6)))
                    continue
                m = RE_PROM.match(line)
                if m:
                    lf.promotions.append((m.group(1), m.group(2),
                                          float(m.group(3)), float(m.group(4))))
                    continue
                m = RE_MISS.match(line)
                if m:
                    lf.rejects.append((m.group(1), m.group(2),
                                       float(m.group(4)), float(m.group(5))))
                    continue
                m = RE_PHASE_SAMPLE.match(line)
                if m:
                    lf.landings.append((m.group(1), float(m.group(3)),
                                        int(m.group(5))))
    lf.aim_timeline.sort()
    return lf


# ---------------------------------------------------------------- config reads
def read_current_state(settings_path, learning_path):
    """Current aim state from settings.json + learning.json (READ-ONLY)."""
    st = {}
    try:
        with open(settings_path) as f:
            s = json.load(f)
        st["base20"] = bool(s.get("tip_phase_anchor_base20", False))
        st["frozen"] = bool(s.get("tip_phase_aim_frozen", False))
        st["lead_ms"] = float(s.get("actuation_lead_ms", DEFAULT_LEAD_MS))
    except OSError:
        st["base20"], st["frozen"], st["lead_ms"] = True, False, DEFAULT_LEAD_MS
        st["settings_missing"] = True
    try:
        with open(learning_path) as f:
            lj = json.load(f)
        st["file_phys"] = float(lj.get("learned_phase_physical_ms"))
    except (OSError, TypeError, ValueError):
        st["file_phys"] = None
    if st["file_phys"] is not None:
        shift = BASE20_SHIFT_MS if st["base20"] else 0.0
        st["inmem_phys"] = st["file_phys"] + shift
        st["effective_aim"] = st["inmem_phys"] + AIM_OFFSET_MS
    return st


def effective_to_file(effective_aim_ms, base20):
    """The learning.json learned_phase_physical_ms that produces this aim."""
    shift = BASE20_SHIFT_MS if base20 else 0.0
    return effective_aim_ms - AIM_OFFSET_MS - shift


# ---------------------------------------------------------------- verdict model
def class_probs(mu, h, sigma):
    """(pE, pG, pL) for landing-error mean mu, window half-width h, spread sigma."""
    pe = norm.cdf((-h - mu) / sigma)
    pl = norm.cdf((mu - h) / sigma)
    pg = np.clip(1.0 - pe - pl, 1e-12, 1.0)
    return np.clip(pe, 1e-12, 1.0), pg, np.clip(pl, 1e-12, 1.0)


_CLS_IDX = {"E": 0, "G": 1, "L": 2}


def _shot_arrays(shots):
    aims = np.array([a for a, _ in shots], dtype=float)
    cls = np.array([_CLS_IDX[d] for _, d in shots], dtype=int)
    return aims, cls


def nll(shots, a_star, h, sigma):
    """shots: list of (aim_ms, 'E'|'G'|'L') OR (aims_array, cls_array)."""
    if isinstance(shots, tuple):
        aims, cls = shots
    else:
        aims, cls = _shot_arrays(shots)
    mu = aims - a_star
    pe = np.clip(norm.cdf((-h - mu) / sigma), 1e-12, 1.0)
    pl = np.clip(norm.cdf((mu - h) / sigma), 1e-12, 1.0)
    pg = np.clip(1.0 - pe - pl, 1e-12, 1.0)
    p = np.where(cls == 0, pe, np.where(cls == 2, pl, pg))
    return float(-np.log(p).sum())


def fit_mle(shots, sigma, h_bounds=(2.0, 60.0)):
    """MLE of (A*, h) at fixed sigma. Returns (a_star, h, nll_min)."""
    arr = _shot_arrays(shots) if not isinstance(shots, tuple) else shots
    aims = arr[0]
    a_lo, a_hi = aims.min() - 60.0, aims.max() + 60.0

    def obj(x):
        return nll(arr, x[0], x[1], sigma)

    best = None
    for a0 in np.linspace(aims.min() - 20, aims.max() + 20, 5):
        for h0 in (8.0, 15.0, 25.0):
            r = minimize(obj, x0=[a0, h0], method="Nelder-Mead",
                         options={"xatol": 1e-3, "fatol": 1e-6, "maxiter": 2000})
            x = [min(max(r.x[0], a_lo), a_hi),
                 min(max(r.x[1], h_bounds[0]), h_bounds[1])]
            v = obj(x)
            if best is None or v < best[2]:
                best = (x[0], x[1], v)
    return best


def profile_ci(shots, sigma, a_hat, h_hat, nll_min, level_chi2=3.841):
    """Profile-likelihood CI on A* (min over h), chi2_1 at 95% by default."""
    arr = _shot_arrays(shots) if not isinstance(shots, tuple) else shots
    thresh = nll_min + level_chi2 / 2.0

    def prof(a):
        r = minimize(lambda h: nll(arr, a, h[0], sigma), x0=[h_hat],
                     method="Nelder-Mead",
                     options={"xatol": 1e-3, "fatol": 1e-6})
        return r.fun

    def scan(direction):
        step, a = 0.5, a_hat
        limit = a_hat + direction * 80.0
        while (a - limit) * direction < 0:
            nxt = a + direction * step
            if prof(nxt) > thresh:
                # bisect between a and nxt
                lo, hi = a, nxt
                for _ in range(25):
                    mid = 0.5 * (lo + hi)
                    if prof(mid) > thresh:
                        hi = mid
                    else:
                        lo = mid
                return 0.5 * (lo + hi)
            a = nxt
        return limit   # unbounded within scan range

    return scan(-1.0), scan(+1.0)


# ---------------------------------------------------------------- power maths
def sign_test_power(q, m, alpha=0.05):
    """Exact power of the two-sided sign test on m misses when the true
    late-fraction among misses is q."""
    # smallest k with 2*P(Bin(m,1/2) >= k) <= alpha
    null_p = [math.comb(m, j) / 2.0 ** m for j in range(m + 1)]
    cum = 0.0
    k_crit = None
    for k in range(m, -1, -1):
        cum += null_p[k]
        if 2.0 * cum > alpha:
            k_crit = k + 1
            break
    if k_crit is None or k_crit > m:
        return 0.0
    # power: P(#late >= k_crit) + P(#late <= m-k_crit) under Bin(m, q)
    hi = sum(math.comb(m, j) * q ** j * (1 - q) ** (m - j)
             for j in range(k_crit, m + 1))
    lo = sum(math.comb(m, j) * q ** j * (1 - q) ** (m - j)
             for j in range(0, m - k_crit + 1))
    return hi + lo


def shots_for_sign_test(delta, h, sigma, power=0.8, alpha=0.05, max_misses=4000):
    """(misses_needed, shots_needed) for the sign test to detect an aim that is
    `delta` ms off optimum. Shots = misses / missrate(delta)."""
    pe, _, pl = class_probs(delta, h, sigma)
    missrate = pe + pl
    q = pl / missrate
    for m in range(5, max_misses):
        if sign_test_power(q, m, alpha) >= power:
            return m, int(math.ceil(m / missrate))
    return None, None


def mle_se_per_shot(h, sigma, mu=0.0):
    """Fisher information of one graded shot about A* (3-class model);
    returns the per-shot standard error scale: SE(n) = this / sqrt(n)."""
    d = 1e-4
    info = 0.0
    p0 = class_probs(mu, h, sigma)
    p1 = class_probs(mu + d, h, sigma)
    for a, b in zip(p0, p1):
        deriv = (b - a) / d
        info += deriv * deriv / a
    return 1.0 / math.sqrt(info) if info > 0 else float("inf")


# ---------------------------------------------------------------- frontier
def frontier_rows(lf, aim_candidates, era_min_aim=415.0):
    """Empirical missed-deadline rate vs aim, from base-20-era phase-sourced
    scheduling decisions. Each decision's headroom is normalised to the aim it
    was actually made under, then shifted to each candidate aim (the phase tip
    estimate moves 1:1 with the aim constant; sampler/registration decisions do
    not move and are reported as background)."""
    decisions = []          # (headroom_ms_at_own_aim, own_aim)
    bg_rejects = 0
    n_bg = 0
    for t, src, eta, lead in lf.promotions:
        aim = lf.aim_at(t)
        if aim is None or aim < era_min_aim:
            continue
        if src == "phase":
            decisions.append((eta, aim))
        else:
            n_bg += 1
    for t, src, lateness, lead in lf.rejects:
        aim = lf.aim_at(t)
        if aim is None or aim < era_min_aim:
            continue
        if src == "phase":
            decisions.append((-lateness, aim))
        else:
            n_bg += 1
            bg_rejects += 1
    rows = []
    for a in aim_candidates:
        n_miss = sum(1 for hr, own in decisions if hr + (a - own) < 0.0)
        rows.append((a, n_miss, len(decisions)))
    return rows, decisions, n_bg, bg_rejects


# ---------------------------------------------------------------- joined input
def load_graded(joined_paths, lf, strict=True, aim_override=None):
    """Graded shots from banner_reader join CSVs.
    Returns (shots [(aim, dir)], detail rows, coverage counters)."""
    shots, detail = [], []
    cov = {"matched": 0, "graded": 0, "dropped_quality": 0, "unreadable": 0,
           "no_release": 0, "no_fire_missed_deadline": 0,
           "release_without_banner": 0}
    for path in joined_paths:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                js = r.get("join_status", "")
                if js in ("no_release", "no_fire_missed_deadline",
                          "release_without_banner"):
                    cov[js] += 1
                    continue
                if js != "matched":
                    continue
                cov["matched"] += 1
                d = word_dir(r.get("word", ""))
                if d is None:
                    cov["unreadable"] += 1
                    continue
                try:
                    score = float(r.get("word_score") or 0.0)
                    nfr = int(r.get("n_frames") or 0)
                    delta = float(r.get("delta_banner_s") or 0.0)
                except ValueError:
                    cov["dropped_quality"] += 1
                    continue
                if strict and not (score >= 0.9 and nfr >= 5 and 0.3 <= delta <= 3.2):
                    cov["dropped_quality"] += 1
                    continue
                aim = aim_override
                if aim is None and lf is not None and r.get("release_utc"):
                    aim = lf.aim_at(_ts(r["release_utc"]).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
                if aim is None:
                    cov["dropped_quality"] += 1
                    continue
                landing = None
                if r.get("phase_normalized_ms") not in ("", None):
                    try:
                        landing = float(r["phase_normalized_ms"])
                    except ValueError:
                        landing = None
                shots.append((aim, d))
                detail.append({"path": os.path.basename(path),
                               "t": r.get("release_utc", ""), "word": r["word"],
                               "color": r.get("color", ""), "dir": d,
                               "aim": aim, "landing": landing,
                               "shot": (r.get("shot") or "").strip(),
                               "seq": r.get("rel_seq", "")})
                cov["graded"] += 1
    return shots, detail, cov


# ---------------------------------------------------------------- reporting
def fmt_tally(detail):
    from collections import Counter
    c = Counter(d["dir"] for d in detail)
    sev = Counter(f"{d['word']}({d['color']})" for d in detail if d["dir"] != "G")
    return c.get("E", 0), c.get("G", 0), c.get("L", 0), dict(sev)


def landing_report(detail):
    by = {"E": [], "G": [], "L": []}
    for d in detail:
        if d["landing"] is not None:
            by[d["dir"]].append(d["landing"])
    lines = []
    for k, name in (("E", "EARLY"), ("G", "GREEN"), ("L", "LATE")):
        v = sorted(by[k])
        if v:
            med = v[len(v) // 2]
            lines.append(f"    {name:6s} n={len(v):3d} landing med={med:7.1f}"
                         f"  range=[{v[0]:.1f}, {v[-1]:.1f}]")
    return lines, by


def do_recommend(args):
    lf = parse_log(args.log) if args.log else None
    state = read_current_state(args.settings, args.learning)
    shots, detail, cov = load_graded(args.joined, lf, strict=not args.no_strict,
                                     aim_override=args.aim)
    print("=" * 78)
    print("AIM ESTIMATE FROM BANNER VERDICTS (offline; banner never feeds the engine)")
    print("=" * 78)
    print(f"joined rows: matched={cov['matched']} graded={cov['graded']} "
          f"(dropped: quality={cov['dropped_quality']} unreadable={cov['unreadable']})")
    print(f"context rows: no_release={cov['no_release']} "
          f"abort/passthrough={cov['no_fire_missed_deadline']} "
          f"release_without_banner={cov['release_without_banner']}")
    if lf and lf.aim_offsets:
        med_off = float(np.median(lf.aim_offsets))
        if abs(med_off - AIM_OFFSET_MS) > 0.05:
            print(f"WARNING: log aim_offset_ms={med_off:.1f} != assumed "
                  f"{AIM_OFFSET_MS}; translations below would be wrong -- fix "
                  f"AIM_OFFSET_MS before trusting the file value.")
    nE, nG, nL, sev = fmt_tally(detail)
    n = nE + nG + nL
    if n == 0:
        print("\nNO graded shots -- nothing to estimate. Check the join.")
        return 1
    aims = sorted(set(round(a, 1) for a, _ in shots))
    print(f"\ngraded shots: n={n}  EARLY={nE}  GREEN={nG}  LATE={nL}   "
          f"(severity mix of misses: {sev})")
    print(f"aims represented (from log, per shot): {aims[0]} .. {aims[-1]} "
          f"({len(aims)} distinct)")
    # per-batch (per joined file) breakdown, aims from the log
    from collections import defaultdict
    byfile = defaultdict(list)
    for d in detail:
        byfile[d["path"]].append(d)
    print("\nper-batch tallies:")
    for p, ds in sorted(byfile.items()):
        e = sum(1 for d in ds if d["dir"] == "E")
        g = sum(1 for d in ds if d["dir"] == "G")
        l = sum(1 for d in ds if d["dir"] == "L")
        amin = min(d["aim"] for d in ds)
        amax = max(d["aim"] for d in ds)
        print(f"  {p:34s} n={len(ds):3d}  E={e:2d} G={g:3d} L={l:2d}  "
              f"aim {amin:.1f}..{amax:.1f}")
    lines, _ = landing_report(detail)
    if lines:
        print("  engine landing (PHASE SAMPLE normalized_ms) by verdict "
              "[corroboration only -- the stop-dater SATURATES on late shots"
              "\n   (a late press stops a meter already at cap), so LATE "
              "landings read low]:")
        print("\n".join(lines))

    # sign test on the misses
    if nE + nL > 0:
        bt = binomtest(nL, nE + nL, 0.5)
        direction = ("LATE surplus -> aim EARLIER" if nL > nE
                     else ("EARLY surplus -> aim LATER" if nE > nL else "balanced"))
        print(f"\nsign test on misses: {nL} late vs {nE} early  "
              f"p={bt.pvalue:.4f}  ({direction})")

    # MLE across the sigma grid
    print(f"\nMLE of the optimum A* (window half-width h fitted; "
          f"sigma = landing spread, supplied):")
    print(f"  {'sigma':>6s} {'A*':>8s} {'95% CI':>18s} {'h':>6s} "
          f"{'->file value':>12s}")
    results = {}
    sigmas = [args.sigma] if args.sigma else list(SIGMA_GRID)
    for sg in sigmas:
        a_hat, h_hat, v = fit_mle(shots, sg)
        lo, hi = profile_ci(shots, sg, a_hat, h_hat, v)
        results[sg] = (a_hat, h_hat, lo, hi)
        fv = effective_to_file(a_hat, state["base20"])
        print(f"  {sg:6.1f} {a_hat:8.1f} [{lo:7.1f}, {hi:7.1f}] {h_hat:6.1f} "
              f"{fv:12.1f}")
    mid_sigma = sigmas[len(sigmas) // 2] if not args.sigma else args.sigma
    a_hat, h_hat, lo, hi = results[mid_sigma]

    a_spread = max(r[0] for r in results.values()) - min(r[0] for r in results.values())
    print(f"  A* spread across the sigma grid: {a_spread:.1f} ms "
          f"({'robust -- the data sit near balance' if a_spread <= 3.0 else 'sigma-sensitive -- treat the magnitude as provisional'})")

    # shot-type robustness: the phase constant is per-ANIMATION, and a mixed
    # batch folds per-type biases into one number. Report the dominant type.
    from collections import Counter
    tc = Counter(d["shot"] for d in detail)
    if len(tc) > 1:
        dom, ndom = tc.most_common(1)[0]
        print("  per-type verdicts (the phase constant is per-ANIMATION; a "
              "type with a one-sided miss profile has a DIFFERENT optimum "
              "than the global knob can express):")
        for ty, cnt in tc.most_common():
            e = sum(1 for d in detail if d["shot"] == ty and d["dir"] == "E")
            g = sum(1 for d in detail if d["shot"] == ty and d["dir"] == "G")
            l = sum(1 for d in detail if d["shot"] == ty and d["dir"] == "L")
            flag = ""
            if cnt >= 8 and (e + l) >= 3 and (e == 0 or l == 0):
                flag = "   <-- one-sided: per-type bias, not the global aim"
            print(f"    {ty:12s} n={cnt:3d}  E={e} G={g} L={l}{flag}")
        if ndom >= 20:
            sub = [(d["aim"], d["dir"]) for d in detail if d["shot"] == dom]
            a_d, h_d, v_d = fit_mle(sub, mid_sigma)
            lo_d, hi_d = profile_ci(sub, mid_sigma, a_d, h_d, v_d)
            print(f"  {dom}-only (n={ndom}, sigma={mid_sigma}): "
                  f"A*={a_d:.1f} [{lo_d:.1f}, {hi_d:.1f}] "
                  f"({'consistent' if abs(a_d - a_hat) <= 3.0 else 'DIVERGES -- per-type aims differ, do not pool blindly'})")

    # current state + the number to write
    print("\n" + "-" * 78)
    if state.get("file_phys") is not None:
        print(f"current learning.json learned_phase_physical_ms = "
              f"{state['file_phys']:.1f}"
              f"  -> effective aim {state['effective_aim']:.1f} "
              f"(base20={'ON' if state['base20'] else 'OFF'}, "
              f"aim_frozen={'ON' if state['frozen'] else 'OFF'})")
        delta = a_hat - state["effective_aim"]
        print(f"recommended effective aim A* = {a_hat:.1f}  "
              f"(move {delta:+.1f} ms from current)")
    else:
        print(f"recommended effective aim A* = {a_hat:.1f} "
              f"(learning.json unreadable -- no delta shown)")
    fv = effective_to_file(a_hat, state["base20"])
    print(f"==> WRITE learned_phase_physical_ms = {fv:.1f}   "
          f"(this is the number that goes in learning.json)")
    print(f"    95% CI on the effective aim: [{lo:.1f}, {hi:.1f}]  "
          f"=> file-value CI [{effective_to_file(lo, state['base20']):.1f}, "
          f"{effective_to_file(hi, state['base20']):.1f}]")
    band_lo = LEARN_BAND_PHYS[0] + (BASE20_SHIFT_MS if state["base20"] else 0.0)
    band_hi = LEARN_BAND_PHYS[1] + (BASE20_SHIFT_MS if state["base20"] else 0.0)
    inmem = fv + (BASE20_SHIFT_MS if state["base20"] else 0.0)
    if not (band_lo <= inmem <= band_hi):
        print(f"    WARNING: implied physical {inmem:.1f} is outside the "
              f"learner accept band [{band_lo:.1f}, {band_hi:.1f}]")
    print(f"    decision budget at A*: {a_hat:.1f} - {state['lead_ms']:.0f} lead "
          f"= {a_hat - state['lead_ms']:.1f} ms "
          f"(was {state.get('effective_aim', float('nan')) - state['lead_ms']:.1f})")
    if state.get("frozen"):
        print("    aim_frozen is ON: the value takes effect at the NEXT launch "
              "(frozen value is read from learning.json at startup).")

    # resolution bluntness
    print("\n" + "-" * 78)
    print("WHAT THIS BATCH CAN RESOLVE (see `power` for the full table):")
    se1 = mle_se_per_shot(h_hat, mid_sigma)
    se_n = se1 / math.sqrt(max(n, 1))
    print(f"  model-based SE of A* at n={n}: {se_n:.1f} ms "
          f"(95% ~ +/-{1.96 * se_n:.1f} ms) -- trusts Normal tails + sigma={mid_sigma}")
    print(f"  profile-likelihood CI width (fewer assumptions): "
          f"{hi - lo:.1f} ms")
    for x in (3.0, 5.0, 8.0):
        m, s = shots_for_sign_test(x, h_hat, mid_sigma)
        if s:
            print(f"  sign test alone: a {x:.0f} ms offset needs ~{m} misses "
                  f"~= {s} graded shots for 80% power")
    if hi - lo > 2 * 8.0:
        print("  BLUNT: this batch cannot justify moves smaller than "
              f"~{(hi - lo) / 2:.0f} ms on its own. Do not chase small deltas.")

    # frontier if a log is present
    if lf:
        print()
        if hi - lo > 20.0:
            print("NOTE: the A* CI above is too wide for the frontier's banner "
                  "columns (pE/pG/pL, net green) to mean anything -- read only "
                  "the budget/miss%% columns below, and get more graded shots.")
        do_frontier_report(lf, state, a_hat, h_hat, mid_sigma,
                           n_graded=n)
    return 0


def do_frontier_report(lf, state, a_hat=None, h_hat=None, sigma=None,
                       n_graded=None, aim_lo=None, aim_hi=None):
    cur = state.get("effective_aim")
    center = a_hat if a_hat is not None else (cur if cur else 435.0)
    lo = aim_lo if aim_lo is not None else center - 12.0
    hi = aim_hi if aim_hi is not None else center + 12.0
    cands = [round(a, 1) for a in np.arange(lo, hi + 0.001, 2.0)]
    rows, decisions, n_bg, bg_rej = frontier_rows(lf, cands)
    print("=" * 78)
    print("AIM vs DECISION-BUDGET FRONTIER "
          "(phase-sourced scheduling decisions, base-20 era)")
    print("=" * 78)
    if not decisions:
        print("no base-20-era phase decisions in the log -- no frontier.")
        return
    n_dec = rows[0][2] if rows else 0
    print(f"decision sample: {n_dec} phase decisions "
          f"(promotions + rejects); {n_bg} non-phase decisions "
          f"({bg_rej} rejected) are aim-INSENSITIVE background")
    hdr = f"  {'aim':>6s} {'budget':>7s} {'pred miss%':>10s} {'n_miss':>6s}"
    have_model = a_hat is not None and h_hat is not None and sigma is not None
    if have_model:
        hdr += f" {'pE%':>6s} {'pG%':>6s} {'pL%':>6s} {'net green/50':>12s} {'owner-lates/50':>14s}"
    print(hdr)
    best = None
    for a, n_miss, n_tot in rows:
        miss_rate = n_miss / n_tot
        line = (f"  {a:6.1f} {a - state['lead_ms']:7.1f} "
                f"{100.0 * miss_rate:9.1f}% {n_miss:6d}")
        if have_model:
            pe, pg, pl = class_probs(a - a_hat, h_hat, sigma)
            fired = 50.0 * (1 - miss_rate)
            net_green = fired * pg
            owner_lates = fired * pl + 50.0 * miss_rate  # aborts pass through late
            line += (f" {100 * pe:5.1f}% {100 * pg:5.1f}% {100 * pl:5.1f}% "
                     f"{net_green:12.1f} {owner_lates:14.1f}")
            if best is None or net_green > best[1]:
                best = (a, net_green)
        marks = []
        if cur is not None and abs(a - cur) < 1.0:
            marks.append("<= current")
        if a_hat is not None and abs(a - a_hat) < 1.0:
            marks.append("<= banner optimum")
        if marks:
            line += "   " + " ".join(marks)
        print(line)
    if best:
        print(f"\n  joint optimum (max net green/50, aborts counted as lates): "
              f"aim {best[0]:.1f}")
    print("  reading: 'pred miss%' is the fraction of historical phase decisions"
          "\n  that would have missed the deadline had the aim been at that value"
          "\n  (headroom shifts 1:1 with the aim constant). Non-phase decisions do"
          "\n  not move with the aim and are excluded from the % but still exist.")


def do_frontier(args):
    lf = parse_log(args.log)
    state = read_current_state(args.settings, args.learning)
    lo, hi = None, None
    if args.aims:
        m = re.match(r"^([\d.]+):([\d.]+)$", args.aims)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
    do_frontier_report(lf, state, aim_lo=lo, aim_hi=hi)
    return 0


def do_power(args):
    sigma = args.sigma
    if args.window_ms:
        h = args.window_ms / 2.0
    else:
        # derive h from the observed green rate at balance
        h = sigma * norm.ppf(0.5 + args.green_rate / 2.0)
    print(f"POWER / RESOLUTION (sigma={sigma}, window half-width h={h:.1f} ms, "
          f"green rate at optimum={100 * (2 * norm.cdf(h / sigma) - 1):.0f}%)")
    print(f"  {'offset X':>9s} {'late-share q':>12s} {'miss rate':>9s} "
          f"{'misses@80%':>10s} {'shots@80%':>9s} {'MLE shots (model)':>17s}")
    se1 = mle_se_per_shot(h, sigma)
    for x in (2.0, 3.0, 5.0, 8.0, 10.0, 15.0):
        pe, _, pl = class_probs(x, h, sigma)
        q = pl / (pe + pl)
        m, s = shots_for_sign_test(x, h, sigma)
        n_mle = int(math.ceil((1.96 * se1 / x) ** 2))
        print(f"  {x:6.0f}ms {q:12.3f} {pe + pl:9.3f} "
              f"{m if m else '>4k':>10} {s if s else '-':>9} {n_mle:17d}")
    print("\n  'shots@80%' = graded shots for the SIGN TEST (verdict counts only)")
    print("  'MLE shots' = graded shots for the 3-class model CI to exclude X")
    print("  (model-trusting: Normal tails + the sigma above). A 50-shot batch")
    m50 = None
    for x in np.arange(1.0, 30.0, 0.5):
        m, s = shots_for_sign_test(x, h, sigma)
        if s and s <= 50:
            m50 = x
            break
    if m50:
        print(f"  resolves ~{m50:.0f} ms by sign test alone; anything smaller "
              f"needs the model or more shots.")
    return 0


# ---------------------------------------------------------------- pipeline
def do_pipeline(args):
    sys.path.insert(0, HERE)
    import banner_reader as br
    os.makedirs(args.workdir, exist_ok=True)
    ev_paths = []
    for sess in args.session:
        name = os.path.basename(os.path.normpath(sess))
        out = os.path.join(args.workdir, f"ev_{args.label}_{name}.csv")
        if os.path.exists(out) and os.path.getsize(out) > 60 and not args.rescan:
            print(f"[pipeline] scan exists, skipping: {out}")
        else:
            ns = argparse.Namespace(session=sess, video=None, stride=2, out=out,
                                    crops=os.path.join(args.workdir,
                                                       f"crops_{args.label}"),
                                    min_frames=2, auto_scale=False)
            br.cmd_scan(ns)
        ev_paths.append(out)
    joined = os.path.join(args.workdir, f"joined_{args.label}.csv")
    ns = argparse.Namespace(events=ev_paths, log=args.log,
                            calib_session=args.session, out=joined)
    br.cmd_join(ns)
    args.joined = [joined]
    args.aim = None
    return do_recommend(args)


# ---------------------------------------------------------------- selftest
def do_selftest(args):
    rng = np.random.default_rng(20260806)
    failures = []

    # 1. translation round trip
    for base20 in (True, False):
        for eff in (431.0, 439.2, 451.3):
            fv = effective_to_file(eff, base20)
            back = fv + (BASE20_SHIFT_MS if base20 else 0.0) + AIM_OFFSET_MS
            if abs(back - eff) > 1e-9:
                failures.append(f"translation round-trip {eff} base20={base20}")
    if abs(effective_to_file(431.0, True) - 298.7) > 0.05:
        failures.append("431.0 must map to file value 298.7 on base-20")
    if abs(effective_to_file(439.2, True) - 306.9) > 0.05:
        failures.append("439.2 must map to file value 306.9 on base-20")

    # 2. MLE recovery on synthetic data (two aims, known truth)
    a_true, h_true, sg = 435.0, 16.0, 9.3
    shots = []
    for aim, count in ((431.0, 400), (439.2, 400)):
        pe, pg, pl = class_probs(aim - a_true, h_true, sg)
        draws = rng.multinomial(count, [pe, pg, pl])
        shots += [(aim, "E")] * draws[0] + [(aim, "G")] * draws[1] + [(aim, "L")] * draws[2]
    a_hat, h_hat, v = fit_mle(shots, sg)
    if abs(a_hat - a_true) > 2.0:
        failures.append(f"MLE recovery: a_hat={a_hat:.1f} vs true {a_true}")
    if abs(h_hat - h_true) > 4.0:
        failures.append(f"MLE recovery: h_hat={h_hat:.1f} vs true {h_true}")
    lo, hi = profile_ci(shots, sg, a_hat, h_hat, v)
    if not (lo <= a_true <= hi):
        failures.append(f"profile CI [{lo:.1f},{hi:.1f}] misses truth {a_true}")

    # 3. sign-test power monotonicity
    prev = None
    for x in (3.0, 5.0, 8.0, 10.0):
        m, s = shots_for_sign_test(x, 16.0, 9.3)
        if s is None:
            failures.append(f"power: no solution at {x}")
            break
        if prev is not None and s > prev:
            failures.append("power: shots not decreasing with offset size")
        prev = s

    # 4. frontier arithmetic on synthetic decisions
    lf = LogFacts()
    lf.aim_timeline = [("2026-08-06T00:00:00.000Z", 439.2)]
    lf.promotions = [("2026-08-06T00:00:01.000Z", "phase", eta, 300.0)
                     for eta in (5.0, 10.0, 50.0, 100.0)]
    lf.rejects = [("2026-08-06T00:00:02.000Z", "phase", 4.0, 300.0)]
    rows, dec, _, _ = frontier_rows(lf, [439.2, 431.2, 445.2])
    got = {a: nm for a, nm, nt in rows}
    #  at own aim: only the reject (headroom -4) is negative -> 1
    #  8 ms earlier: promotions 5 (5-8<0) join, and 10-8>0 stays -> 2? (5-8=-3<0, 10-8=2>0) => reject+5 => 2
    #  6 ms later: reject rescued (−4+6=2>0) -> 0
    if got[439.2] != 1 or got[431.2] != 2 or got[445.2] != 0:
        failures.append(f"frontier arithmetic wrong: {got}")

    # 5. class_probs sanity: mu>0 must raise late prob
    pe0, _, pl0 = class_probs(0.0, 16.0, 9.3)
    peL, _, plL = class_probs(6.0, 16.0, 9.3)
    if not (plL > pl0 and peL < pe0 and abs(pe0 - pl0) < 1e-12):
        failures.append("class_probs direction wrong")

    if failures:
        print("SELFTEST FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("selftest OK (translation, MLE recovery, CI coverage, power "
          "monotonicity, frontier arithmetic, model direction)")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(s):
        s.add_argument("--settings", default=os.path.join(REPO, "settings.json"))
        s.add_argument("--learning", default=os.path.join(REPO, "learning.json"))

    s = sub.add_parser("recommend", help="estimate A* from joined banner CSVs")
    s.add_argument("--joined", nargs="+", required=True,
                   help="banner_reader.py join output CSV(s)")
    s.add_argument("--log", nargs="*", default=[],
                   help="orion_native.log path(s): per-shot aims + frontier")
    s.add_argument("--sigma", type=float, default=None,
                   help="landing spread ms (default: report the whole grid)")
    s.add_argument("--aim", type=float, default=None,
                   help="override: single aim for ALL shots (no log)")
    s.add_argument("--no-strict", action="store_true",
                   help="accept low-confidence banner reads (score<0.9)")
    common(s)
    s.set_defaults(fn=do_recommend)

    s = sub.add_parser("pipeline",
                       help="ONE COMMAND: scan framedumps -> join -> recommend")
    s.add_argument("--session", nargs="+", required=True,
                   help="framedump session dir(s)")
    s.add_argument("--log", nargs="+", required=True)
    s.add_argument("--workdir", required=True,
                   help="where scan/join CSVs land")
    s.add_argument("--label", default="batch")
    s.add_argument("--rescan", action="store_true")
    s.add_argument("--sigma", type=float, default=None)
    s.add_argument("--no-strict", action="store_true")
    common(s)
    s.set_defaults(fn=do_pipeline)

    s = sub.add_parser("power", help="shots needed to justify a move of X ms")
    s.add_argument("--sigma", type=float, default=SIGMA_DEFAULT)
    s.add_argument("--green-rate", type=float, default=0.92,
                   help="green rate at the optimum (sets h when no --window-ms)")
    s.add_argument("--window-ms", type=float, default=None,
                   help="full green-window width in ms (overrides --green-rate)")
    s.set_defaults(fn=do_power)

    s = sub.add_parser("frontier", help="aim vs missed-deadline frontier from log")
    s.add_argument("--log", nargs="+", required=True)
    s.add_argument("--aims", default=None, help="lo:hi effective-aim scan range")
    common(s)
    s.set_defaults(fn=do_frontier)

    s = sub.add_parser("selftest", help="internal consistency checks")
    s.set_defaults(fn=do_selftest)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
