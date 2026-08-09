"""[ORION_PROBE] Offline validation of the probe -> label -> cache -> restore chain.

WHAT THIS PROVES (and what it cannot). It replays REAL recorded meter-rise frames from a
detframes CSV through the production LatencyEstimator with a synthetic probe PRESS marker
placed a fixed interval before each rise, and a supplied probe_spawn_offset_ms. That
exercises, on real frame data: the in-band rise collector (fill floor / 45.0 ceiling /
monotone step), the template back-extrapolation and its 6ms consistency cap, the label
formula (raw - spawn), the [lo, hi] range gate, the posterior outlier gates against the
factory prior, the [ORION_PROBE_PERSIST] cache write at >= 3 probe labels, and the
probe-validated restore as a factory-kind seed.

It CANNOT validate D_spawn itself (the press placement here is synthetic by construction) or
the injection side (Cross/Square timing, possession). Those need the live rig — see
tools/timing/calibrate_probe_spawn.py and the owner checklist.

Success criterion mirrors the live one: >= 3 ACCEPTED probe labels and a persisted cache
that RESTORES as a probe prior in a fresh estimator.

Usage:
    python tools/timing/validate_probe_chain.py logs/diagnostics/detframes_YYYY..csv
        [--spawn-offset 180.9] [--press-lead 400] [--max-episodes 8]

Uses a scratch cache path; never touches %LOCALAPPDATA%\\Orion or any live state.
The environment flag is irrelevant here: probe persistence is enabled via the estimator's
constructor parameter, exactly as the sidecar's wire key does it.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from latency_estimator import LatencyEstimator  # noqa: E402


def load_episodes(path: str, max_episodes: int) -> list:
    """Contiguous detected+fed runs whose fill rises from <25 through the invertible band
    (>=3 samples inside 22.8-45.0) to >50 — i.e. real shot rises, not decor flickers."""
    episodes: list = []
    cur: list = []
    last_wall = None

    def flush() -> None:
        nonlocal cur
        if cur:
            fills = [f for _, f in cur]
            n_band = sum(1 for f in fills if 22.8 < f < 45.0)
            if min(fills) < 25.0 and max(fills) > 50.0 and n_band >= 3:
                episodes.append(cur)
        cur = []

    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row["detected"] == "1" and row["fed"] == "1":
                wall = float(row["wall_ms"])
                if last_wall is not None and wall - last_wall > 250.0 and cur:
                    flush()
                cur.append((wall, float(row["fill_pct"])))
                last_wall = wall
            else:
                flush()
    flush()
    return episodes[:max_episodes]


def build_estimator(cache_path: str, restore: bool) -> LatencyEstimator:
    # Factory-prior-equivalent construction (the shipped prior: total 218.5, sd 28.7). The
    # probe-persist switch is the constructor parameter, as wired from the sidecar env key.
    return LatencyEstimator(
        boot_prior_ms=218.5,
        hi_ms=500.0,
        mu_prior_ms=218.5 - 8.3,
        sd_prior_ms=28.7,
        route_scope="probe-chain-validation",
        cache_path=cache_path,
        restore_cache=restore,
        factory_prior_source="validation_fixture",
        factory_prior_version="v0",
        factory_prior_sd_ms=28.7,
        probe_persist_enabled=True,
        probe_onboard_enabled=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="detframes CSV with real shot rises")
    ap.add_argument("--spawn-offset", type=float, default=180.9,
                    help="probe_spawn_offset_ms under test (default: the 2026-08-06 "
                         "calibrated 180.9)")
    ap.add_argument("--press-lead", type=float, default=400.0,
                    help="synthetic press -> first-detected-frame interval (ms); the "
                         "measured raw median is ~406, and raw - spawn must land inside "
                         "the factory acceptance envelope")
    ap.add_argument("--max-episodes", type=int, default=8)
    args = ap.parse_args()

    episodes = load_episodes(args.csv, args.max_episodes)
    if len(episodes) < 3:
        print(f"FAIL: only {len(episodes)} qualifying rise episodes in {args.csv} — need >=3")
        return 1
    print(f"replaying {len(episodes)} real rise episodes from {args.csv}")

    scratch = Path(tempfile.mkdtemp(prefix="orion_probe_chain_")) / "probe_cache.bin"
    est = build_estimator(str(scratch), restore=False)
    if est._reg is None:  # noqa: SLF001 — validation needs the same template production uses
        print("FAIL: registration template unavailable (models/tip_registration.json)")
        return 1

    closes = 0
    for seq, ep in enumerate(episodes, start=1):
        press_wall = ep[0][0] - args.press_lead
        assert est.mark_probe(press_wall, seq, rtt_ms=None,
                              spawn_offset_ms=args.spawn_offset)
        for wall, fill in ep:
            est.update(wall, fill, True, True)
        raw = est.last_probe_raw_ms
        closed = est._pending_probe_ms is None and raw > 0  # noqa: SLF001
        closes += 1 if closed else 0
        print(f"  episode {seq}: frames={len(ep)} raw={raw:.1f}ms "
              f"status={est.last_status} labels={est.probe_labels}")

    labels = est.probe_labels
    persisted = scratch.is_file()
    print(f"closes={closes}/{len(episodes)} accepted_probe_labels={labels} "
          f"cache_persisted={persisted} measured={est.value_ms():.1f}ms "
          f"sd={est.authority_sd_ms:.1f}")

    ok = labels >= 3 and persisted
    if ok:
        est2 = build_estimator(str(scratch), restore=True)
        restored = bool(est2.probe_prior_restored)
        print(f"fresh-estimator restore: probe_prior_restored={restored} "
              f"seed_total={est2.authority_value_ms:.1f}ms "
              f"kind={est2.authority_kind} source={est2.factory_prior_source}")
        ok = ok and restored and est2.factory_prior_source == "probe_cache:self_measured"

    # The uncalibrated control leg: spawn 0 must still produce ZERO labels (raw-only), or a
    # rig could mint labels containing the un-stripped game constant.
    est0 = build_estimator(str(scratch) + ".uncal", restore=False)
    ep = episodes[0]
    est0.mark_probe(ep[0][0] - args.press_lead, 1, rtt_ms=None, spawn_offset_ms=0.0)
    for wall, fill in ep:
        est0.update(wall, fill, True, True)
    uncal_ok = (est0.probe_labels == 0 and est0.last_status == "probe_uncalibrated"
                and est0.last_probe_raw_ms > 0)
    print(f"uncalibrated control: labels={est0.probe_labels} "
          f"status={est0.last_status} raw={est0.last_probe_raw_ms:.1f} -> "
          f"{'ok' if uncal_ok else 'VIOLATION'}")

    ok = ok and uncal_ok
    print("PASS" if ok else "FAIL")
    try:
        scratch.unlink(missing_ok=True)
        Path(str(scratch) + ".uncal").unlink(missing_ok=True)
        scratch.parent.rmdir()
    except OSError:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
