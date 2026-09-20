"""Poll-phase probe: does the game's verdict depend on WHERE INSIDE A CONSOLE FRAME the
release command was issued?

READ-ONLY on every input. Parses Orion native session logs, joins each graded vision release
to (a) the engine clock instant the command was issued, (b) the per-shot game-frame grid the
engine recovered from the meter's fill staircase (`FRAME PHASE:`), (c) the capture cycle
(`CAPTURE PHASE:`), and (d) the game's own verdict (sidecar `BANNER VERDICT:` attributed by
time, plus the engine-joined `RELEASE ORACLE:` gap keyed on the physical epoch).

It then folds the verdicts against the release phase modulo one console frame and reports a
circular effect size with a within-stratum permutation null.

Usage:
    python tools/timing/poll_phase_probe.py --logs "<glob>" --out D:\\NexusVision\\poll_phase

Nothing here touches the engine, the reader, or any running process.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict

import numpy as np

PERIOD_MS = 1000.0 / 60.0            # the engine's console-frame constant
PERIOD_5994_MS = 1000.0 / 59.94      # the real PS5 vsync measured 09-03 (AV clock)

_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z\s+(?P<body>.*)$")
_KV = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def _kv(body: str) -> dict:
    return {k: v for k, v in _KV.findall(body)}


def _f(d: dict, key: str, default=float("nan")) -> float:
    v = d.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _i(d: dict, key: str, default=None):
    v = d.get(key)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _ts_ms(ts: str) -> float:
    # ISO with fractional seconds, no zone suffix by the time we get here.
    t = dt.datetime.strptime(ts[:23], "%Y-%m-%dT%H:%M:%S.%f")
    return t.timestamp() * 1000.0


def parse_log(path: str) -> dict:
    """One session log -> {'shots': [...], 'banners': [...], 'oracles': [...]}"""
    shots: list[dict] = []
    banners: list[dict] = []
    oracles: list[dict] = []
    resv_created: dict[tuple, dict] = {}
    capture: dict[int, dict] = {}
    last_capture: dict | None = None
    open_shot: dict | None = None
    by_seq: dict[int, dict] = {}
    lead_changes: list[tuple] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = _TS.match(raw.rstrip("\n"))
            if not m:
                continue
            body = m.group("body")
            wall = _ts_ms(m.group("ts"))

            if "FRAME PHASE:" in body:
                d = _kv(body[body.index("FRAME PHASE:"):])
                shot = {
                    "wall_frame_phase_ms": wall,
                    "frame_phase_ms": _f(d, "phase_ms"),
                    "frame_phase_sd_ms": _f(d, "sd"),
                    "frame_phase_lock": _i(d, "lock", 0),
                    "frame_period_ms": _f(d, "period_ms", PERIOD_MS),
                    "frame_edges": _i(d, "edges", 0),
                    "frame_skips": _i(d, "skips", 0),
                    "frame_dating_source": d.get("source", ""),
                    "frame_correction_ms": _f(d, "correction_ms"),
                    "frame_step_pct": _f(d, "step_pct"),
                    "anchor_ms": _f(d, "anchor_ms"),
                    "physical_epoch": _i(d, "physical_epoch"),
                    "shot_attempt": _i(d, "shot_attempt"),
                    "session": os.path.basename(path),
                }
                if last_capture is not None:
                    shot.update(last_capture)
                shots.append(shot)
                open_shot = shot
                continue

            if "CAPTURE PHASE:" in body:
                d = _kv(body[body.index("CAPTURE PHASE:"):])
                last_capture = {
                    "capture_cycle_phase_ms": _f(d, "cycle_phase_ms"),
                    "capture_coherence": _f(d, "coherence"),
                    "capture_lock": _i(d, "lock", 0),
                    "capture_deadline_eta_ms": _f(d, "deadline_eta_ms"),
                    "capture_n": _i(d, "n", 0),
                }
                if open_shot is not None and open_shot.get("capture_cycle_phase_ms") is None:
                    open_shot.update(last_capture)
                continue

            if "TIP RESERVATION:" in body:
                d = _kv(body[body.index("TIP RESERVATION:"):])
                if d.get("disposition") == "reservation_created":
                    key = (_i(d, "physical_epoch"), _i(d, "shot_attempt"))
                    rec = {
                        "fire_target": d.get("fire_target", ""),
                        "frame_offset_ms": _f(d, "frame_offset_ms"),
                        "lead_ms": _f(d, "lead_ms"),
                        "lead_kind": d.get("lead_kind", ""),
                        "lead_source": d.get("lead_source", ""),
                        "lead_offset_ms": _f(d, "lead_offset_ms", 0.0),
                        "banner_trim_ms": _f(d, "banner_trim_ms", 0.0),
                        "tip_eta_ms": _f(d, "tip_eta_ms"),
                        "command_eta_ms": _f(d, "command_eta_ms"),
                        "predictor_sigma_ms": _f(d, "predictor_sigma_ms"),
                        "resv_fill_pct": _f(d, "fill_pct"),
                        "frame_age_ms": _f(d, "frame_age_ms"),
                        "tempo": d.get("tempo", ""),
                        "armed_source": d.get("source", ""),
                        "phase_const_ms": _f(d, "phase_const_ms"),
                        "wall_resv_ms": wall,
                    }
                    resv_created.setdefault(key, rec)
                continue

            if body.startswith("Precise dispatch timing:"):
                d = _kv(body)
                seq = _i(d, "seq")
                rec = {
                    "command_issued_ms": _f(d, "command_issued_ms"),
                    "dispatch_token": _i(d, "token"),
                    "wall_dispatch_ms": wall,
                }
                tgt = by_seq.get(seq)
                if tgt is None and open_shot is not None and "command_issued_ms" not in open_shot:
                    tgt = open_shot
                if tgt is not None and "command_issued_ms" not in tgt:
                    tgt.update(rec)
                    tgt["seq"] = seq
                    by_seq.setdefault(seq, tgt)
                continue

            if body.startswith("Release issued:"):
                d = _kv(body)
                seq = _i(d, "seq")
                mm = re.search(r"shot=(.+?)\s*$", body)
                relfill = re.search(r"Release issued: fill ([0-9.]+)%", body)
                rec = {
                    "shot_type": mm.group(1).strip() if mm else "",
                    "release_fill_pct": float(relfill.group(1)) if relfill else float("nan"),
                    "release_age_ms": _f(d, "age".replace("age", "age")) if "age" in d else float("nan"),
                    "wall_release_ms": wall,
                    "release_seq_log": seq,
                }
                agem = re.search(r"age=(\d+)ms", body)
                if agem:
                    rec["release_age_ms"] = float(agem.group(1))
                if open_shot is not None and not open_shot.get("shot_type"):
                    open_shot.update(rec)
                continue

            if body.startswith("Release submit:"):
                d = _kv(body)
                seq = _i(d, "seq")
                rec = {
                    "fire_epoch_ms": _f(d, "fire_epoch_ms"),
                    "submit_ok": _i(d, "ok", 0),
                    "wall_submit_ms": wall,
                }
                tgt = by_seq.get(seq) or open_shot
                if tgt is not None and "fire_epoch_ms" not in tgt:
                    tgt.update(rec)
                    tgt["seq"] = seq
                    by_seq.setdefault(seq, tgt)
                continue

            if "BANNER VERDICT:" in body:
                d = _kv(body[body.index("BANNER VERDICT:"):])
                banners.append({
                    "wall_ms": wall,
                    "timing": d.get("timing", ""),
                    "coverage": d.get("coverage", ""),
                    "ncc": _f(d, "ncc"),
                })
                continue

            if "RELEASE ORACLE: release_seq=" in body:
                d = _kv(body[body.index("RELEASE ORACLE:"):])
                oracles.append({
                    "wall_ms": wall,
                    "release_seq": _i(d, "release_seq"),
                    "gap_px": _f(d, "gap_px"),
                    "gap_pct": _f(d, "gap_pct"),
                    "settled_fill": _f(d, "settled_fill"),
                    "proxy": d.get("proxy", ""),
                })
                continue

            if "BANNER TRIM: reset reason=user_lead_change" in body:
                d = _kv(body[body.index("BANNER TRIM:"):])
                lead_changes.append((wall, _f(d, "user_lead")))
                continue

    # fold in reservation + capture by (epoch, attempt)
    for s in shots:
        key = (s.get("physical_epoch"), s.get("shot_attempt"))
        r = resv_created.get(key)
        if r:
            s.update(r)
    return {"shots": shots, "banners": banners, "oracles": oracles,
            "lead_changes": lead_changes, "path": path}


BANNER_WINDOW = (500.0, 3200.0)   # release -> banner emit, ms (handoff: onset 995-1744, emit +115)


def attribute(parsed: dict) -> list[dict]:
    shots = [s for s in parsed["shots"] if "command_issued_ms" in s and s.get("submit_ok")]
    for s in shots:
        s["wall_fire_ms"] = s.get("wall_dispatch_ms") or s.get("wall_submit_ms") or s.get("wall_release_ms")
    shots.sort(key=lambda s: s["wall_fire_ms"])
    # banner -> nearest preceding release inside the window, unique
    used: set[int] = set()
    for b in parsed["banners"]:
        cands = [i for i, s in enumerate(shots)
                 if BANNER_WINDOW[0] <= b["wall_ms"] - s["wall_fire_ms"] <= BANNER_WINDOW[1]]
        if not cands:
            continue
        i = cands[-1]
        if i in used:
            continue
        # ambiguity: another release also sits inside the window
        shots[i]["banner"] = b["timing"]
        shots[i]["banner_ncc"] = b["ncc"]
        shots[i]["banner_lag_ms"] = b["wall_ms"] - shots[i]["wall_fire_ms"]
        shots[i]["banner_ambiguous"] = 1 if len(cands) > 1 else 0
        used.add(i)
    # oracle -> physical epoch
    oracle_by_epoch = {}
    for o in parsed["oracles"]:
        oracle_by_epoch.setdefault(o["release_seq"], o)
    for s in shots:
        o = oracle_by_epoch.get(s.get("physical_epoch"))
        if o:
            s["oracle_gap_px"] = o["gap_px"]
            s["oracle_settled_fill"] = o["settled_fill"]
            s["oracle_proxy"] = o["proxy"]
    return shots


def add_phases(shots: list[dict]) -> None:
    for s in shots:
        p = s.get("frame_period_ms") or PERIOD_MS
        if not (p and math.isfinite(p) and p > 1.0):
            p = PERIOD_MS
        cmd = s.get("command_issued_ms", float("nan"))
        s["phase_engine_ms"] = cmd % p if math.isfinite(cmd) else float("nan")
        fp = s.get("frame_phase_ms", float("nan"))
        s["phase_frame_ms"] = ((cmd - fp) % p) if (math.isfinite(cmd) and math.isfinite(fp)) else float("nan")
        # centred: signed distance from the frame CENTRE (what the frame-native target aims at)
        s["phase_frame_centred_ms"] = (s["phase_frame_ms"] - p / 2.0) if math.isfinite(s["phase_frame_ms"]) else float("nan")
        cp = s.get("capture_cycle_phase_ms", float("nan"))
        s["phase_capture_ms"] = cp if math.isfinite(cp) else float("nan")
        s["phase_frameage_ms"] = (s.get("frame_age_ms", float("nan")) % p)
        s["phase_wall_ms"] = (s.get("fire_epoch_ms", float("nan")) % p)


def circ_amplitude(theta: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Effect size for 'outcome depends on phase': |mean((y - ybar) * e^{i theta})|, and its angle."""
    yc = y - y.mean()
    z = np.mean(yc * np.exp(1j * theta))
    return float(abs(z)) * 2.0, float(np.angle(z))


def perm_p(theta, y, strata, n_perm=4000, seed=12345):
    amp, ang = circ_amplitude(theta, y)
    rng = np.random.default_rng(seed)
    idx_by = defaultdict(list)
    for i, s in enumerate(strata):
        idx_by[s].append(i)
    null = np.empty(n_perm)
    yv = y.copy()
    for k in range(n_perm):
        ys = yv.copy()
        for _, idx in idx_by.items():
            j = np.array(idx)
            ys[j] = yv[rng.permutation(j)]
        null[k] = circ_amplitude(theta, ys)[0]
    p = float((np.sum(null >= amp) + 1) / (n_perm + 1))
    return amp, ang, p, float(np.median(null)), float(np.percentile(null, 95))


def sawtooth_scan(theta, y, strata, period, n_perm=2000, seed=999):
    """Best split of the circle into two arcs (a cliff): max |mean(y|arc A) - mean(y|arc B)|."""
    def stat(yv):
        best = 0.0
        best_cut = 0.0
        for cut in np.linspace(0, 2 * math.pi, 48, endpoint=False):
            rel = (theta - cut) % (2 * math.pi)
            a = rel < math.pi
            if a.sum() < 4 or (~a).sum() < 4:
                continue
            d = abs(yv[a].mean() - yv[~a].mean())
            if d > best:
                best, best_cut = d, cut
        return best, best_cut
    obs, cut = stat(y)
    rng = np.random.default_rng(seed)
    idx_by = defaultdict(list)
    for i, s in enumerate(strata):
        idx_by[s].append(i)
    null = np.empty(n_perm)
    for k in range(n_perm):
        ys = y.copy()
        for _, idx in idx_by.items():
            j = np.array(idx)
            ys[j] = y[rng.permutation(j)]
        null[k] = stat(ys)[0]
    p = float((np.sum(null >= obs) + 1) / (n_perm + 1))
    return obs, cut * period / (2 * math.pi), p, float(np.median(null))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--perms", type=int, default=4000)
    args = ap.parse_args(argv)

    paths: list[str] = []
    for g in args.logs:
        paths.extend(sorted(glob.glob(g)))
    os.makedirs(args.out, exist_ok=True)

    all_shots: list[dict] = []
    per_log = []
    for p in paths:
        parsed = parse_log(p)
        shots = attribute(parsed)
        add_phases(shots)
        for s in shots:
            s["session"] = os.path.basename(p)
        all_shots.extend(shots)
        per_log.append({
            "log": os.path.basename(p),
            "shots": len(shots),
            "banners_seen": len(parsed["banners"]),
            "banners_attributed": sum(1 for s in shots if s.get("banner")),
            "oracles": len(parsed["oracles"]),
            "oracles_joined": sum(1 for s in shots if "oracle_gap_px" in s),
        })

    cols = sorted({k for s in all_shots for k in s.keys()})
    csv_path = os.path.join(args.out, "shots.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for s in all_shots:
            w.writerow(s)

    with open(os.path.join(args.out, "parse_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"per_log": per_log, "total": len(all_shots)}, fh, indent=2)

    print(f"parsed {len(all_shots)} released shots from {len(paths)} logs -> {csv_path}")
    for r in per_log:
        print("  ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
