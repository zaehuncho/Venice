"""[ORION_PROBE] Offline D_spawn calibration — run ONCE, then persist the result.

The warmup probe (call-for-ball then shoot) measures press -> meter-appear = the full loop PLUS
the constant game-side spawn lead-in D_spawn (animation time from the press to the first meter
pixel). The passive frozen-meter oracle measures the same loop WITHOUT D_spawn. So from ONE
session that contains BOTH (a warmup probe run + passive oracle labels from real shots):

    D_spawn = median(probe raw)  -  L_oracle(total, SAME session)

Usage:
    python tools/timing/calibrate_probe_spawn.py <session-log> [<more logs...>]

where <session-log> is any log that captured the sidecar's stderr/log stream (e.g.
logs/orion_native.log for a native run).

WHY THE JOIN IS SESSION-AWARE (v2). The original tool took median(all probe raws in the file)
minus the LAST release marker's measured_latency_ms in the file. Both halves were wrong on a
real rotated log:
  * orion_native.log spans many sidecar sessions on different capture routes. Measured
    2026-08-06: a capture-card session's probes (raw median 406.0) sat in the same file as a
    later decoder-route session whose converged oracle read 134.0 — the file-global join
    computed D_spawn = 269.9 vs the true same-session 180.9, a ~90ms error that nothing
    downstream could detect.
  * release markers echo the CURRENT posterior, which on an n=0 session is the packaged
    factory prior — a plausible-looking number that measured nothing on this rig.

The v2 join therefore:
  1. PREFERS the estimator's own in-memory join: the greppable
     "probe spawn estimate: d_spawn=..." line (latency_estimator._log_probe_spawn_estimate),
     which is same-session/same-estimator by construction. The line with the most closed
     probes in the newest cluster wins.
  2. Falls back to an offline join that reconstructs the SAME quantity the estimator
     publishes (measured_ms = posterior mu + 8.3 tick expectation; see _recompute): probe
     closes are grouped into runs, and each run is joined ONLY against accepted
     "latency observation" lines from the same sidecar session (bounded by
     "New sidecar process generation" lines), taking the last accepted observation's
     posterior fixed_ms + 8.3 with n >= MIN_ORACLE_LABELS.
  3. Cross-checks 1 vs 2 when both exist and refuses to print a recommendation when they
     disagree by more than CROSS_CHECK_MAX_DELTA_MS — a disagreement means the session
     structure was not what this tool assumed, and shipping a wrong constant here silently
     mislabels every probe on every rig.

Prints the recommended learning.json value:  "probe_spawn_offset_ms": <D>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

# Same constant as latency_estimator.TICK_WAIT_EXPECT_MS (measured_ms = mu + rtt + this).
TICK_WAIT_EXPECT_MS = 8.3
# A probe run's closes arrive seconds apart; >5 min of silence separates runs/sessions.
RUN_GAP_S = 300.0
# Minimum accepted passive labels behind the oracle value before it is trusted for the join.
MIN_ORACLE_LABELS = 3
# Estimator-join vs offline-join disagreement beyond this refuses a recommendation.
CROSS_CHECK_MAX_DELTA_MS = 20.0

PROBE_RE = re.compile(r"probe closed: raw=([0-9.]+)ms")
ESTIMATE_RE = re.compile(
    r"probe spawn estimate: d_spawn=([0-9.]+)ms probes=(\d+) n_labels=(\d+) "
    r"measured_total_ms=([0-9.]+)")
# Accepted passive observation: total_ms is THIS label's measured total, n the posterior count.
# The join uses the median of the session's accepted totals rather than the posterior mean
# (fixed_ms): measured 2026-08-06, one 189.1ms outlier label dragged the conjugate mean from
# 210.1 to 186.0 inside 90 seconds while the median of the same six labels held at 225.1 —
# a 25ms swing in the shipped constant depending on which minute the log was cut.
OBSERVATION_RE = re.compile(
    r"latency observation: .*accepted=1 .*total_ms=([0-9.]+) .*\bn=(\d+)")
SESSION_RE = re.compile(r"New sidecar process generation")
# Native line prefix (UTC) or the sidecar's own stamp (local); one file uses one flavour for
# ordering, so mixed offsets cannot reorder events within a file.
TS_NATIVE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z")
TS_SIDECAR_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _line_ts(line: str) -> float | None:
    m = TS_NATIVE_RE.match(line) or TS_SIDECAR_RE.search(line)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = (int(g) for g in m.groups())
    # Ordinal seconds are enough: the join needs ordering and gaps, never absolute wall time.
    day_ordinal = y * 372 + mo * 31 + d
    return day_ordinal * 86400.0 + h * 3600.0 + mi * 60.0 + s + ms / 1000.0


class _Events:
    """Parsed, time-stamped log events (plain class: this file is loaded by path in tests,
    and a @dataclass in a module absent from sys.modules breaks under Python 3.12)."""

    def __init__(self) -> None:
        self.probes: list = []         # (t, raw_ms)
        self.estimates: list = []      # (t, d_spawn, n_probes, n_labels)
        self.observations: list = []   # (t, total_ms, n)
        self.session_starts: list = []


def _parse(paths: list[str]) -> _Events:
    ev = _Events()
    for p in paths:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            t = None
            m = PROBE_RE.search(line)
            if m:
                t = _line_ts(line)
                if t is not None:
                    ev.probes.append((t, float(m.group(1))))
                continue
            m = ESTIMATE_RE.search(line)
            if m:
                t = _line_ts(line)
                if t is not None:
                    ev.estimates.append((t, float(m.group(1)),
                                         int(m.group(2)), int(m.group(3))))
                continue
            m = OBSERVATION_RE.search(line)
            if m:
                t = _line_ts(line)
                if t is not None:
                    ev.observations.append((t, float(m.group(1)), int(m.group(2))))
                continue
            if SESSION_RE.search(line):
                t = _line_ts(line)
                if t is not None:
                    ev.session_starts.append(t)
    for lst in (ev.probes, ev.estimates, ev.observations, ev.session_starts):
        lst.sort(key=lambda item: item[0] if isinstance(item, tuple) else item)
    return ev


def _session_index(t: float, session_starts: list[float]) -> int:
    """Index of the sidecar session containing t (0 = before the first recorded boundary)."""
    idx = 0
    for i, start in enumerate(session_starts, start=1):
        if t >= start:
            idx = i
        else:
            break
    return idx


def _group_runs(probes: list) -> list[list]:
    runs: list[list] = []
    for t, raw in probes:
        if runs and (t - runs[-1][-1][0]) <= RUN_GAP_S:
            runs[-1].append((t, raw))
        else:
            runs.append([(t, raw)])
    return runs


def _estimator_join(ev: _Events) -> tuple[float, str] | None:
    """Preference 1: the estimator's own same-session estimate; newest cluster, most probes."""
    if not ev.estimates:
        return None
    cluster = [ev.estimates[-1]]
    for item in reversed(ev.estimates[:-1]):
        if cluster[0][0] - item[0] <= RUN_GAP_S:
            cluster.insert(0, item)
        else:
            break
    best = max(cluster, key=lambda item: (item[2], item[0]))
    t, d_spawn, n_probes, n_labels = best
    return d_spawn, (f"estimator online join: d_spawn={d_spawn:.1f}ms over "
                     f"{n_probes} closed probes vs an n={n_labels} passive posterior")


def _offline_join(ev: _Events) -> tuple[float, str] | None:
    """Preference 2: newest probe run joined against same-session accepted observations."""
    if not ev.probes:
        return None
    for run in reversed(_group_runs(ev.probes)):
        run_end = run[-1][0]
        session = _session_index(run_end, ev.session_starts)
        eligible = [total for (t, total, n) in ev.observations
                    if _session_index(t, ev.session_starts) == session]
        if len(eligible) < MIN_ORACLE_LABELS:
            continue
        l_oracle = float(np.median(np.asarray(eligible, float)))
        raws = np.asarray([raw for _, raw in run], float)
        raw_med = float(np.median(raws))
        raw_iqr = float(np.percentile(raws, 75) - np.percentile(raws, 25))
        d_spawn = raw_med - l_oracle
        detail = (f"offline session join: run of {len(run)} closes, median raw="
                  f"{raw_med:.1f}ms IQR={raw_iqr:.1f}ms vs oracle {l_oracle:.1f}ms "
                  f"(median of {len(eligible)} accepted passive labels, same sidecar "
                  f"session)")
        if raw_iqr > 25.0:
            detail += "\nWARNING: probe raw IQR is wide (>25ms) — noisy appear detection"
        return d_spawn, detail
    return None


def main(paths: list[str]) -> int:
    ev = _parse(paths)
    if not ev.probes and not ev.estimates:
        print("no 'probe closed' / 'probe spawn estimate' lines found — run the warmup "
              "probes first (Run Timing Probes with probe_spawn_offset_ms still 0)")
        return 1
    est = _estimator_join(ev)
    off = _offline_join(ev)
    if est:
        print(est[1])
    if off:
        print(off[1])
    if not est and not off:
        print("probe closes exist but no same-session passive oracle was found — take "
              f">={MIN_ORACLE_LABELS + 2} real shots (misses included: the passive oracle "
              "only labels sub-95% freezes) in the SAME session as the probe run")
        return 1
    if est and off and abs(est[0] - off[0]) > CROSS_CHECK_MAX_DELTA_MS:
        print(f"REFUSING a recommendation: the estimator join ({est[0]:.1f}ms) and the "
              f"offline session join ({off[0]:.1f}ms) disagree by "
              f"{abs(est[0] - off[0]):.1f}ms (> {CROSS_CHECK_MAX_DELTA_MS}). The session "
              "structure is not what this tool assumed — re-run a single clean session "
              "(probe run + real shots) and calibrate from that log alone.")
        return 1
    # The offline join carries the more converged oracle when both agree; prefer it.
    d_spawn = off[0] if off else est[0]
    if d_spawn <= 0.0 or d_spawn > 400.0:
        print(f"WARNING: implausible D_spawn ({d_spawn:.1f}ms) — check that probes and "
              "shots are from the same session/venue and the oracle had converged")
        return 1
    print(f'\nlearning.json:  "probe_spawn_offset_ms": {d_spawn:.1f}')
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
