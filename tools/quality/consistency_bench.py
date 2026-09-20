#!/usr/bin/env python
"""Per-condition CONSISTENCY BENCHMARK for the meter detection stack.

Purpose
-------
Turn "we think we're more consistent than the competitor" into per-condition
numbers, and expose which conditions we cannot measure yet.  Scores every
usable framedump session on two instruments:

  * REPLAY  -- the CURRENT reader (simple_meter_reader + YOLO detector), run
               offline over the dumped raw frames with native physical-shot
               epochs replayed (exact production env: see _REPLAY_ENV). Measures what the
               code as it stands today would see on those pixels.
  * LIVE    -- the per-frame verdicts the session's own build recorded in
               frames.csv.  Measures what actually flew that day (including
               arm-gating, so LIVE detection is bounded by the arm windows).

Per shot-run (a physical Square press joined from orion_native.log) it scores:
  detection      : meter found at all; first-detected fill (engine cannot fire
                   above ~33% first sight) and press->first-detection latency
  lock stability : on/off transitions per detected frame (churn), longest
                   unbroken lock, frame-to-frame box-centre motion |dcx|
                   (raw + detrended jitter, because fades move the meter)
  false locks    : boxes that are not meter-shaped (~23x107 @720p) and any
                   lock in the scoreboard band (top of frame)
  fill quality   : per-frame fill noise sigma_y about a within-run linear fit
                   on the rising segment (in % of scale), for BOTH the
                   sub-pixel fill (fill_pct) and the coarse instrument
                   (raw_fill_pct / fill_coarse), plus fitted slope %/ms
  outcome (log)  : fired vs aborted BY REASON vs silent; landings graded on
                   peak_fill against [green_start, green_end] from
                   `Release landing:` lines.  settled_fill is a documented
                   INVALID instrument on this rig and is never used.

Joining framedumps to the log
-----------------------------
frames.csv `t_wall` is stamped at WRITE time and can lag capture by seconds,
while `t_ms` is the capture-relative axis (perf_counter at capture).  The
session anchor is therefore the LOWER ENVELOPE  anchor = min(t_wall - t_ms/1e3)
(write lag is always >= 0), and every log event is mapped onto the t_ms axis
through that anchor.  Join quality is REPORTED per session as the distribution
of press -> first-rise-onset deltas; treat per-run rows with |delta| outside
[-0.3s, +1.5s] with suspicion.  frames.csv restarts idx/t_ms when the sidecar
restarts mid-session; each generation gets its own anchor, and PNGs for
overlapping indices belong to the LATEST generation (the restart overwrote
them).

Usage
-----
  .venv/Scripts/python.exe tools/quality/consistency_bench.py all
      [--sessions S1,S2,...] [--log logs/orion_native.log]
      [--outdir logs/diagnostics/consistency_bench/<ts>]
  subcommands:
    replay  -- run the current reader over the dumps (slow, cached per session)
    score   -- parse log, join, compute metrics, write report (fast)
    all     -- replay (reusing cache) then score

Replay caches live beside the report (replay_<session>.csv) and are reused only
when their profile/model identity matches; pass --force-replay to recompute.
Every future
session: run `all` again and diff report.txt against the previous run to catch
per-condition regressions.
"""
from __future__ import annotations

import argparse
import csv
import glob as _glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRAMEDUMP_DIR = os.path.join(ROOT, "logs", "diagnostics", "framedump")
DEFAULT_LOG = os.path.join(ROOT, "logs", "orion_native.log")
BENCH_DIR = os.path.join(ROOT, "logs", "diagnostics", "consistency_bench")

# Exact production reader profile. Keep this in sync with
# native_orion/src/SidecarReaderProfile.h; a contract test enforces the shared
# keys. These are forced for the benchmark so an inherited developer shell
# cannot silently change the instrument being compared.
_REPLAY_ENV = {
    "ORION_READER_OCCLUSION": "1", "ORION_READER_ROBUST": "0",
    "ORION_READER_SCALE_ADAPT": "1", "ORION_READER_TIP_ENFORCE": "1",
    "ORION_READER_ARMED_HOLD": "1", "ORION_COLOR_CAL": "0",
    "ORION_GREEN_ZONE_WINDOW": "0", "ORION_GREEN_SELF_GRADE": "0",
    "ORION_READER_ANCHOR": "1", "ORION_READER_PCTL_FILL": "1",
    "ORION_READER_TRACK_H_CAP": "1",
    "ORION_METER_SUBPIXEL_SESSION_RULER": "1",
    "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL": "1",
    "ORION_METER_SUBPIXEL_BASE_HOLD": "1",
    "ORION_METER_DETECTOR": "1",
    # Mode 1 is the shipped armed-only priority path. Mode 2 used by the old
    # benchmark continuously prioritized acquisition and was not a live replay.
    "ORION_METER_DETECTOR_SYNC_ACQUIRE": "1",
    "ORION_LATENCY_REGIME_REOPEN_SOFT": "1",
    "ORION_METER_PARTIAL_OCCLUSION": "1",
    "ORION_METER_PARTIAL_OCCLUSION_MAX_MS": "120",
    "ORION_METER_NEGATIVE_BRIDGE_MAX_MS": "45",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT": "3",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_COLS": "2",
    "ORION_METER_DETECTOR_CONF": "0.35",
    "ORION_METER_PROVIDER_PRIORITY": "dml,cpu",
    "ORION_TIMING_PROFILE_ID": "2k27-2026-09-04-v2",
}
SHIPPED_METER_MODEL = os.path.join(ROOT, "models", "orion_meter_detector.onnx")
REPLAY_CODE_INPUTS = (
    os.path.join(ROOT, "simple_meter_reader.py"),
    os.path.join(ROOT, "meter_detector_yolo.py"),
    os.path.abspath(__file__),
)

# Meter geometry prior @720p (measured live: bbox ~23-24 x 106-108).
SHAPE_W = (15, 36)          # px @720p-scale
SHAPE_H = (55, 165)
SCOREBOARD_BAND_FRAC = 0.18  # top fraction of frame height = scoreboard/shot-clock band
RUN_WINDOW_MS = 3000.0       # press window length (capped by the next press)
RUN_GUARD_MS = 4500.0        # frames within press+guard are never counted as no-shot FPs
RISE_CAP_MS = 1500.0         # rise metrics restricted to press+this
FIRST_FILL_GATE = 33.0       # engine cannot fire above ~this first-seen fill


def resolve_replay_profile(model_path=None):
    """Return the complete deterministic production child environment."""

    profile = dict(_REPLAY_ENV)
    profile["ORION_METER_MODEL"] = os.path.abspath(
        model_path or SHIPPED_METER_MODEL
    )
    return profile


def resolve_reader_config(root=ROOT, meter_style=None, meter_color=None):
    """Resolve the same user-visible meter settings the live sidecar receives."""

    settings = {}
    try:
        with open(os.path.join(root, "settings.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
            if isinstance(payload, dict):
                settings = payload
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        confidence = float(settings.get("detection_confidence_percent", 32)) / 100.0
    except (TypeError, ValueError):
        confidence = 0.32
    return {
        "meter_style": str(meter_style or settings.get("meter_style") or "Arrow2"),
        "meter_color": str(meter_color or settings.get("meter_color") or "White"),
        "confidence_threshold": min(1.0, max(0.0, confidence)),
    }


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def replay_profile_id(profile, reader_config, runs_by_generation):
    """Content identity for cache invalidation without trusting a model path."""

    model = profile["ORION_METER_MODEL"]
    model_hash = ""
    if os.path.isfile(model):
        model_hash = file_sha256(model)
    material = {
        "env": {k: v for k, v in profile.items() if k != "ORION_METER_MODEL"},
        "model_sha256": model_hash,
        "reader": reader_config,
        "code_sha256": {
            os.path.relpath(path, ROOT).replace("\\", "/"): file_sha256(path)
            for path in REPLAY_CODE_INPUTS
            if os.path.isfile(path)
        },
        "epochs": {
            str(gen): [
                [r["epoch"], round(r["press_ms"], 3), round(r["arm_end_ms"], 3)]
                for r in runs
            ]
            for gen, runs in sorted(runs_by_generation.items())
        },
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def replay_cache_matches(path, profile_id):
    try:
        with open(path, newline="") as fh:
            first = next(csv.DictReader(fh), None)
        return bool(first and first.get("profile_id") == profile_id)
    except (OSError, StopIteration, csv.Error):
        return False


# --------------------------------------------------------------------------- #
#  frames.csv loading (generation-aware)
# --------------------------------------------------------------------------- #

def load_session(session_dir):
    """Return list of generations: {rows, anchor, png_owner:set(idx), name, gen}."""
    fcsv = os.path.join(session_dir, "frames.csv")
    gens, cur = [], []
    with open(fcsv, newline="") as fh:
        last_idx = -1
        for r in csv.DictReader(fh):
            i = int(r["idx"])
            if i < last_idx:
                gens.append(cur)
                cur = []
            last_idx = i
            cur.append(r)
    if cur:
        gens.append(cur)
    pngs = {}
    for p in _glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = re.match(r"f(\d+)_[01]_raw\.png$", os.path.basename(p))
        if m:
            pngs[int(m.group(1))] = p
    out = []
    for gi, rows in enumerate(gens):
        anchor = min(float(r["t_wall"]) - float(r["t_ms"]) / 1000.0 for r in rows)
        out.append({
            "name": os.path.basename(session_dir), "dir": session_dir, "gen": gi,
            "rows": rows, "anchor": anchor,
            "t_max_ms": max(float(r["t_ms"]) for r in rows),
            "idx_max": max(int(r["idx"]) for r in rows),
        })
    # PNG ownership: the LATEST generation that wrote an idx owns the file.
    for idx, path in pngs.items():
        owner = None
        for g in out:
            if idx <= g["idx_max"]:
                owner = g          # later generations overwrite earlier ones
        if owner is not None:
            owner.setdefault("png", {})[idx] = path
    for g in out:
        g.setdefault("png", {})
    return out


# --------------------------------------------------------------------------- #
#  log parsing
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z\s")


def _ts_epoch(line):
    m = _TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp()


def _kv(line, key, cast=str):
    m = re.search(re.escape(key) + r"=([^\s]+)", line)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return None


def _shot_name(line, key):
    """Shot-type fields: `shot=Right Fade` (space-separated, runs to end of line on
    landing/timing lines) vs `shot_type=Right_Fade`.  Normalize to underscores."""
    m = re.search(re.escape(key) + r"=([A-Za-z][A-Za-z_ ]*?)\s*$", line.rstrip())
    if not m:
        m = re.search(re.escape(key) + r"=([A-Za-z_]+)", line)
    if not m:
        return None
    return m.group(1).strip().replace(" ", "_")


def parse_log(paths):
    ev = {"press": [], "abort": [], "presstip": [], "landing": [],
          "rel_timing": [], "not_owned": [], "cap_health": []}
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                t = None
                if "Physical shot epoch:" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["press"].append({"t": t, "epoch": _kv(line, "epoch", int),
                                            "intent": _kv(line, "intent")})
                elif "Shot abort identity:" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["abort"].append({
                            "t": t, "epoch": _kv(line, "physical_epoch", int),
                            "reason": _kv(line, "reason"), "site": _kv(line, "site"),
                            "shot_type": _kv(line, "shot_type"),
                            "meter_x": _kv(line, "meter_x", float)})
                elif "PRESS-TIP OBSERVATION:" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["presstip"].append({
                            "t": t, "epoch": _kv(line, "physical_epoch", int),
                            "shot_type": _kv(line, "shot_type"),
                            "seq": _kv(line, "seq", int),
                            "press_to_tip_ms": _kv(line, "press_to_tip_ms", float)})
                elif "Release landing:" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["landing"].append({
                            "t": t, "seq": _kv(line, "seq", int),
                            "peak_fill": _kv(line, "peak_fill", float),
                            "green_start": _kv(line, "green_start", float),
                            "green_end": _kv(line, "green_end", float),
                            "meter_x": _kv(line, "meter_x", float),
                            "shot_type": _shot_name(line, "shot")})
                elif "Release timing:" in line and "seq=" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["rel_timing"].append({
                            "t": t, "seq": _kv(line, "seq", int),
                            "shot_type": _shot_name(line, "shot")})
                elif "Capture health: cap_mode=" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["cap_health"].append({"t": t, "mode": _kv(line, "cap_mode")})
                elif "SHOT NOT OWNED:" in line:
                    t = _ts_epoch(line)
                    if t:
                        ev["not_owned"].append({
                            "t": t,
                            "epoch": _kv(line, "physical_epoch", int),
                            "reason": _kv(line, "reason"),
                            "shot_type": _kv(line, "shot_type"),
                        })
    return ev


# --------------------------------------------------------------------------- #
#  press-run join
# --------------------------------------------------------------------------- #

def build_runs(gen, ev):
    """Press windows on this generation's t_ms axis, with shot metadata."""
    a = gen["anchor"]
    lo, hi = a - 2.0, a + gen["t_max_ms"] / 1000.0 + 2.0
    presses = sorted((p for p in ev["press"] if lo <= p["t"] <= hi and p["epoch"] is not None),
                     key=lambda p: p["t"])
    presses = list({(p["t"], p["epoch"], p.get("intent")): p for p in presses}.values())
    # seq -> fire wall time (Release timing logged at the fire)
    seq_fire = {}
    for rt in ev["rel_timing"]:
        if lo <= rt["t"] <= hi and rt["seq"] is not None:
            seq_fire[rt["seq"]] = rt
    runs = []
    for i, p in enumerate(presses):
        t_ms = (p["t"] - a) * 1000.0
        end = t_ms + RUN_WINDOW_MS
        if i + 1 < len(presses):
            end = min(end, (presses[i + 1]["t"] - a) * 1000.0)
        run = {"epoch": p["epoch"], "press_utc": p["t"], "press_ms": t_ms,
               "end_ms": end, "shot_type": None, "aborts": [], "landing": None,
               "fired": False, "silent": True, "terminal_ms": None,
               "release_seq": None}
        runs.append(run)
    by_epoch = {r["epoch"]: r for r in runs}
    for ab in ev["abort"]:
        r = by_epoch.get(ab["epoch"])
        if r is not None and 0.0 <= ab["t"] - r["press_utc"] < 30.0:
            r["aborts"].append(ab["reason"])
            r["silent"] = False
            r["shot_type"] = r["shot_type"] or ab["shot_type"]
            terminal_ms = (ab["t"] - a) * 1000.0
            if terminal_ms >= r["press_ms"]:
                current = r["terminal_ms"]
                r["terminal_ms"] = (
                    terminal_ms if current is None else min(current, terminal_ms)
                )
    for no in ev["not_owned"]:
        r = by_epoch.get(no["epoch"])
        if r is not None and 0.0 <= no["t"] - r["press_utc"] < 30.0:
            r["aborts"].append("not_owned:" + str(no["reason"] or "unknown"))
            r["silent"] = False
            r["shot_type"] = r["shot_type"] or no["shot_type"]
            terminal_ms = (no["t"] - a) * 1000.0
            if terminal_ms >= r["press_ms"]:
                current = r["terminal_ms"]
                r["terminal_ms"] = (
                    terminal_ms if current is None else min(current, terminal_ms)
                )
    seq_epoch = {}
    for pt in ev["presstip"]:
        r = by_epoch.get(pt["epoch"])
        if r is not None and 0.0 <= pt["t"] - r["press_utc"] < 30.0:
            r["shot_type"] = r["shot_type"] or pt["shot_type"]
            if pt["seq"] is not None:
                seq_epoch[pt["seq"]] = pt["epoch"]
    for ld in ev["landing"]:
        if not (lo - 10 <= ld["t"] <= hi + 10):
            continue
        ep = seq_epoch.get(ld["seq"])
        r = by_epoch.get(ep) if ep is not None else None
        if r is not None and not 0.0 <= ld["t"] - r["press_utc"] < 30.0:
            continue
        if ld["seq"] in seq_fire and not 0.0 <= ld["t"] - seq_fire[ld["seq"]]["t"] < 30.0:
            continue
        if r is not None and ld["seq"] in seq_fire and not (
                0.0 <= seq_fire[ld["seq"]]["t"] - r["press_utc"] < 30.0):
            continue
        if r is None and ld["seq"] in seq_fire:
            ft = seq_fire[ld["seq"]]["t"]
            cands = [x for x in runs if 0.0 <= ft - x["press_utc"] <= 6.0]
            r = cands[-1] if cands else None
        if r is not None:
            r["landing"] = ld
            r["fired"] = True
            r["silent"] = False
            r["shot_type"] = r["shot_type"] or ld["shot_type"]
    for sq, rt in seq_fire.items():
        ep = seq_epoch.get(sq)
        if ep is not None:
            # A later physical press is not the owner of an earlier shot's
            # release just because it is nearer in time. Use the same explicit
            # identity as the landing; never count one release on two presses.
            r = by_epoch.get(ep)
            if r is None or not 0.0 <= rt["t"] - r["press_utc"] < 30.0:
                continue
        else:
            cands = [x for x in runs if 0.0 <= rt["t"] - x["press_utc"] <= 6.0]
            r = cands[-1] if cands else None
        if r is not None:
            r["release_seq"] = sq
            terminal_ms = (rt["t"] - a) * 1000.0
            if terminal_ms >= r["press_ms"]:
                current = r["terminal_ms"]
                r["terminal_ms"] = (
                    terminal_ms if current is None else min(current, terminal_ms)
                )
            if not r["landing"]:
                r["fired"] = True
                r["silent"] = False
                r["shot_type"] = r["shot_type"] or rt["shot_type"]
    for r in runs:
        r["arm_end_ms"] = min(
            r["end_ms"],
            r["terminal_ms"] if r["terminal_ms"] is not None else r["end_ms"],
        )
    return runs


# --------------------------------------------------------------------------- #
#  replay
# --------------------------------------------------------------------------- #

REPLAY_FIELDS = ["profile_id", "gen", "idx", "t_ms", "shot_epoch", "shot_armed",
                 "det", "fill", "coarse", "conf",
                 "bx", "by", "bw", "bh", "rej", "g_start", "g_end", "g_conf",
                 "rise_state", "frame_w", "frame_h"]


def replay_session(
    session_dir, out_csv, ev, profile, reader_config, *, gens=None,
    runs_by_generation=None, profile_id=None,
):
    """Replay frames with the production env and the recorded hardware epochs."""

    gens = gens or load_session(session_dir)
    runs_by_generation = runs_by_generation or {
        g["gen"]: build_runs(g, ev) for g in gens
    }
    profile_id = profile_id or replay_profile_id(
        profile, reader_config, runs_by_generation
    )
    model_path = profile["ORION_METER_MODEL"]
    if not os.path.isfile(model_path):
        raise SystemExit(
            "production meter detector model is missing: %s" % model_path
        )

    # This is a reusable Python module as well as a CLI. Do not leak the forced
    # production profile into a caller's test process.
    old_env = {key: os.environ.get(key) for key in profile}
    os.environ.update(profile)
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import cv2
        from simple_meter_reader import SimpleMeterReader

        ReplayConfig = type("ReplayConfig", (), dict(reader_config))
        t0 = time.perf_counter()
        n_fed = n_missing = 0
        with open(out_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(REPLAY_FIELDS)
            for g in gens:
                # Fresh reader per generation (a restart got a fresh reader live too).
                rdr = SimpleMeterReader(
                    cfg=ReplayConfig(), require_gameplay_eligibility=True
                )
                runs = runs_by_generation.get(g["gen"], [])
                last_epoch = None
                release_notified = set()
                for row in g["rows"]:
                    idx = int(row["idx"])
                    path = g["png"].get(idx)
                    if not path:
                        n_missing += 1
                        continue
                    fr = cv2.imread(path)
                    if fr is None:
                        n_missing += 1
                        continue
                    frame_ms = float(row["t_ms"])
                    ts = frame_ms / 1000.0
                    for run in runs:
                        if (
                            run["fired"]
                            and run["release_seq"] is not None
                            and run["terminal_ms"] is not None
                            and run["terminal_ms"] <= frame_ms
                            and run["epoch"] not in release_notified
                        ):
                            rdr.notify_release(
                                int(run["release_seq"]),
                                physical_epoch=int(run["epoch"]),
                                identity_verified=False,
                            )
                            release_notified.add(run["epoch"])
                    active = next(
                        (
                            run for run in runs
                            if run["press_ms"] <= frame_ms <= run["arm_end_ms"]
                        ),
                        None,
                    )
                    epoch = int(active["epoch"]) if active is not None else 0
                    if active is not None and epoch != last_epoch:
                        rdr.notify_physical_shot_start(epoch)
                        last_epoch = epoch
                    armed = active is not None
                    rdr.set_shot_state(armed, 1.0 if armed else 0.0, armed)
                    res = rdr.detect(fr, ts=ts)
                    bx, by, bw_, bh = (
                        int(v) for v in (res.bbox or (0, 0, 0, 0))
                    )
                    w.writerow([
                        profile_id, g["gen"], idx, "%.2f" % frame_ms,
                        epoch, int(armed), int(bool(res.detected)),
                        "%.2f" % res.fill_pct,
                        "%.2f" % getattr(res, "raw_fill_pct", 0.0),
                        "%.3f" % res.confidence, bx, by, bw_, bh,
                        (res.rejection_reason or "").replace(",", ";"),
                        "%.2f" % getattr(res, "green_window_start_pct", -1.0),
                        "%.2f" % getattr(res, "green_window_end_pct", -1.0),
                        "%.2f" % getattr(res, "green_window_confidence", 0.0),
                        getattr(res, "rise_state", ""),
                        fr.shape[1], fr.shape[0],
                    ])
                    n_fed += 1
        print(
            "replayed %s: %d frames fed, %d missing pngs, profile=%s, %.1fs"
            % (
                os.path.basename(session_dir), n_fed, n_missing, profile_id,
                time.perf_counter() - t0,
            )
        )
        return profile_id
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_replay(out_csv):
    frames = defaultdict(list)
    with open(out_csv, newline="") as fh:
        for r in csv.DictReader(fh):
            frames[int(r["gen"])].append(r)
    return frames


# --------------------------------------------------------------------------- #
#  metrics
# --------------------------------------------------------------------------- #

def _med(xs):
    return statistics.median(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(math.ceil(0.9 * len(s))) - 1)]


def _linfit(ts, ys):
    """Least-squares y = a + b*t; returns (b_slope, sigma_residual) or (None, None)."""
    n = len(ts)
    if n < 5:
        return None, None
    mt = sum(ts) / n
    my = sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return None, None
    b = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / sxx
    a = my - b * mt
    res = [y - (a + b * t) for t, y in zip(ts, ys)]
    dof = max(1, n - 2)
    sigma = math.sqrt(sum(r * r for r in res) / dof)
    return b, sigma


def _shape_flags(fr, w_key="bw", h_key="bh"):
    """Not-meter-shaped / scoreboard-band flags for one detected frame row."""
    try:
        bw, bh = int(fr[w_key]), int(fr[h_key])
        by = int(fr["by"])
        H = int(fr.get("frame_h") or 720)
    except (TypeError, ValueError, KeyError):
        return False, False
    s = H / 720.0
    bad_shape = not (SHAPE_W[0] * s <= bw <= SHAPE_W[1] * s
                     and SHAPE_H[0] * s <= bh <= SHAPE_H[1] * s)
    cy = by + bh / 2.0
    in_scoreboard = cy < SCOREBOARD_BAND_FRAC * H
    return bad_shape, in_scoreboard


def score_run(run, frames, fill_key="fill", coarse_key="coarse"):
    """frames: list of frame dicts for the run's generation, sorted by t_ms.

    The window starts AT the mapped press (no pre-roll): the anchor's write-lag
    bias maps log events EARLY relative to t_ms, so scanning from the mapped
    press never misses the onset, while a pre-roll would count the previous
    shot's still-live lock as this run's first detection."""
    w = [f for f in frames if run["press_ms"] <= float(f["t_ms"]) <= run["end_ms"]]
    det = [f for f in w if f["det"] == "1"]
    m = {"n_frames": len(w), "n_det": len(det), "found": bool(det),
         "window_ms": run["end_ms"] - run["press_ms"]}
    if not w:
        m["no_frames"] = True
        return m
    # first detection
    if det:
        f0 = det[0]
        m["first_fill"] = float(f0[fill_key])
        m["first_fill_coarse"] = float(f0.get(coarse_key) or 0.0)
        m["first_latency_ms"] = float(f0["t_ms"]) - run["press_ms"]
        m["first_fill_le_gate"] = m["first_fill"] <= FIRST_FILL_GATE
    # churn / longest lock
    trans = 0
    prev = None
    streak = best_streak = 0
    best_ms = cur_start = None
    for f in w:
        d = f["det"] == "1"
        if prev is not None and d != prev:
            trans += 1
        if d:
            if streak == 0:
                cur_start = float(f["t_ms"])
            streak += 1
            if streak > best_streak:
                best_streak = streak
                best_ms = float(f["t_ms"]) - cur_start
        else:
            streak = 0
        prev = d
    m["transitions"] = trans
    m["churn"] = trans / max(1, len(det))
    m["longest_lock_frames"] = best_streak
    m["longest_lock_ms"] = best_ms
    # box motion (consecutive detected frames, gap <= 100ms)
    dcx = []
    for f1, f2 in zip(det, det[1:]):
        if float(f2["t_ms"]) - float(f1["t_ms"]) <= 100.0:
            c1 = int(f1["bx"]) + int(f1["bw"]) / 2.0
            c2 = int(f2["bx"]) + int(f2["bw"]) / 2.0
            dcx.append(c2 - c1)
    m["dcx_med"] = _med([abs(d) for d in dcx])
    m["dcx_p90"] = _p90([abs(d) for d in dcx])
    # detrended jitter: subtract rolling median motion (fades move the meter for real)
    if len(dcx) >= 5:
        k = 2
        jit = []
        for i in range(len(dcx)):
            loc = dcx[max(0, i - k):i + k + 1]
            jit.append(abs(dcx[i] - statistics.median(loc)))
        m["jitter_med"] = _med(jit)
        m["jitter_p90"] = _p90(jit)
    # shape / scoreboard flags
    bad = sb = 0
    for f in det:
        b, s = _shape_flags(f)
        bad += b
        sb += s
    m["bad_shape_frames"] = bad
    m["scoreboard_frames"] = sb
    # rise segment fit (both instruments)
    # sigma_y must measure per-frame NOISE, not the early-rise curvature: restrict
    # the linear fit to the constant-rate regime (fill 25..93; the measured 2K27
    # rate is ~0.196 %/ms there).  Fall back to 5..93 when the run is too sparse.
    rise = [f for f in det
            if float(f["t_ms"]) <= run["press_ms"] + RISE_CAP_MS
            and 25.0 <= float(f[fill_key]) <= 93.0]
    if len(rise) < 5:
        rise = [f for f in det
                if float(f["t_ms"]) <= run["press_ms"] + RISE_CAP_MS
                and 5.0 <= float(f[fill_key]) <= 93.0]
        if len(rise) >= 5:
            m["rise_fit_fallback"] = True
    # keep only the monotone-ish leading rise: stop at the first frame after the max
    if rise:
        fills = [float(f[fill_key]) for f in rise]
        peak_i = fills.index(max(fills))
        rise = rise[:peak_i + 1]
    if len(rise) >= 5:
        ts = [float(f["t_ms"]) for f in rise]
        b, s = _linfit(ts, [float(f[fill_key]) for f in rise])
        m["slope_pct_ms"], m["sigma_y"] = b, s
        cb, cs = _linfit(ts, [float(f.get(coarse_key) or 0.0) for f in rise])
        m["slope_coarse"], m["sigma_y_coarse"] = cb, cs
        m["rise_points"] = len(rise)
    peak = max((float(f[fill_key]) for f in det), default=None)
    m["peak_fill_seen"] = peak
    # FILL DROPOUT during the rise: detected frames whose fill reads ~0 between the
    # first real fill sample and the rise peak.  This is the measured fade failure
    # mode (box tracks the panning meter, the fill read comes back 0) -- the engine
    # starves and aborts detector_authority_lost even though the lock never broke.
    rise_win = [f for f in det if float(f["t_ms"]) <= run["press_ms"] + RISE_CAP_MS]
    nz = [i for i, f in enumerate(rise_win) if float(f[fill_key]) > 0.5]
    if nz:
        lo = nz[0]
        peak_i = max(nz, key=lambda i: float(rise_win[i][fill_key]))
        span = rise_win[lo:peak_i + 1]
        if len(span) >= 3:
            drops = sum(1 for f in span if float(f[fill_key]) <= 0.5)
            m["fill_dropout_rate"] = drops / len(span)
            m["fill_dropout_n"] = len(span)
    return m


def live_frames_for_gen(gen):
    out = []
    for r in gen["rows"]:
        out.append({"gen": str(gen["gen"]), "idx": r["idx"], "t_ms": r["t_ms"],
                    "det": r["detected"], "fill": r["fill_pct"], "coarse": r["fill_pct"],
                    "conf": r["conf"], "bx": r["bbox_x"], "by": r["bbox_y"],
                    "bw": r["bbox_w"], "bh": r["bbox_h"], "rej": r["rejection"],
                    "frame_w": "1280", "frame_h": "720"})
    return out


def no_shot_fp_stats(frames, runs):
    """Detected frames OUTSIDE any press guard window = candidate false positives."""
    guards = [(r["press_ms"] - 300.0, r["press_ms"] + RUN_GUARD_MS) for r in runs]
    out = {"frames_outside": 0, "det_outside": 0, "bad_shape": 0, "scoreboard": 0,
           "examples": []}
    for f in frames:
        t = float(f["t_ms"])
        if any(a <= t <= b for a, b in guards):
            continue
        out["frames_outside"] += 1
        if f["det"] == "1":
            out["det_outside"] += 1
            b, s = _shape_flags(f)
            out["bad_shape"] += b
            out["scoreboard"] += s
            if len(out["examples"]) < 12:
                out["examples"].append({"gen": f["gen"], "idx": f["idx"],
                                        "t_ms": f["t_ms"], "fill": f["fill"],
                                        "bbox": [f["bx"], f["by"], f["bw"], f["bh"]]})
    return out


def grade_landing(ld):
    if not ld:
        return None
    pf, gs, ge = (ld.get(key) for key in ("peak_fill", "green_start", "green_end"))
    if any(value is None or not math.isfinite(value) for value in (pf, gs, ge)):
        return None
    if gs > ge:
        return None
    if gs <= pf <= ge:
        return "green"
    return "early" if pf < gs else "late"


# --------------------------------------------------------------------------- #
#  drift / post-outcome analysis (within-session temporal axis)
# --------------------------------------------------------------------------- #
# The competitor's reported failure mode is TEMPORAL ("consistent for 4-5
# shots, then downhill after a miss"), which per-condition averages would hide
# entirely.  These sections measure our own within-session drift and
# post-outcome conditioning directly.

_T975 = {3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26,
         10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04}


def _tcrit(df):
    if df <= 2:
        return 4.30
    for k in sorted(_T975):
        if df <= k:
            return _T975[k]
    return 1.96


def _ols_ci(xs, ys):
    """slope, lo95, hi95 of y ~ x; None if degenerate."""
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    s2 = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / (n - 2)
    se = math.sqrt(s2 / sxx)
    t = _tcrit(n - 2)
    return b, b - t * se, b + t * se


DRIFT_METRICS = [("first_fill", "%.2f"), ("sigma_y", "%.2f"),
                 ("fill_dropout_rate", "%.3f"), ("churn", "%.4f"),
                 ("slope_pct_ms", "%.4f"), ("dcx_p90", "%.2f")]


def outcome_class(run):
    """Collapse a run's outcome for conditioning: green / offtarget / abort / silent."""
    if run["landing"]:
        g = grade_landing(run["landing"])
        if g is None:
            return "fired_ungraded"
        return "green" if g == "green" else "fire_offtarget"
    if run["aborts"]:
        return "abort"
    if run["fired"]:
        return "fired_ungraded"
    return "silent"


def drift_section(rows_inst, inst):
    """Per-session: metric-vs-shot-index OLS slope + first/last-third means."""
    out = []
    by_sess = defaultdict(list)
    for r in rows_inst:
        if not r["metrics"].get("no_frames"):
            by_sess[r["session"]].append(r)
    lines = ["WITHIN-SESSION DRIFT  [%s]  (metric vs shot index; + = worsening for "
             "first_fill/sigma/dropout/churn)" % inst.upper()]
    for sess, rs in sorted(by_sess.items()):
        rs = sorted(rs, key=lambda r: (r["gen"], r["press_ms"]))
        n = len(rs)
        lines.append("  %s  (n=%d shots)%s" % (sess, n,
                     "  [UNPOWERED: n<10 -- treat trends as anecdote]" if n < 10 else ""))
        for key, spec in DRIFT_METRICS:
            pts = [(i + 1, r["metrics"][key]) for i, r in enumerate(rs)
                   if r["metrics"].get(key) is not None]
            if len(pts) < 4:
                lines.append("    %-18s n=%d (too sparse to fit)" % (key, len(pts)))
                continue
            xs, ys = zip(*pts)
            fit = _ols_ci(xs, ys)
            third = max(1, len(pts) // 3)
            first3 = statistics.mean(ys[:third])
            last3 = statistics.mean(ys[-third:])
            if fit:
                b, lo, hi = fit
                sig = "" if lo <= 0 <= hi else "  <-- CI excludes 0"
                lines.append(("    %-18s n=%2d slope/shot=%+.4f [%.4f,%.4f]"
                              "  first3rd=" + spec + " last3rd=" + spec + "%s")
                             % (key, len(pts), b, lo, hi, first3, last3, sig))
        # outcome drift where the log gives it: abort/silent in first vs last half
        half = n // 2
        if half >= 3:
            def _rate(sub, cls):
                return sum(1 for r in sub if outcome_class(r) == cls) / len(sub)
            lines.append("    outcome: abort rate first-half=%.2f last-half=%.2f | "
                         "silent %.2f -> %.2f | green %.2f -> %.2f"
                         % (_rate(rs[:half], "abort"), _rate(rs[half:], "abort"),
                            _rate(rs[:half], "silent"), _rate(rs[half:], "silent"),
                            _rate(rs[:half], "green"), _rate(rs[half:], "green")))
        out.append((sess, rs))
    return "\n".join(lines) + "\n", out


def post_outcome_section(rows_inst, inst):
    """Metrics of shot k+1 conditioned on the outcome of shot k (same session/gen)."""
    by_sg = defaultdict(list)
    for r in rows_inst:
        if not r["metrics"].get("no_frames"):
            by_sg[(r["session"], r["gen"])].append(r)
    groups = defaultdict(list)
    for _, rs in by_sg.items():
        rs = sorted(rs, key=lambda r: r["press_ms"])
        for prev, cur in zip(rs, rs[1:]):
            # only condition on consecutive shots (< 30 s apart)
            if cur["press_ms"] - prev["press_ms"] > 30000:
                continue
            groups[outcome_class(prev)].append(cur)
    lines = ["POST-OUTCOME CONDITIONING  [%s]  (shot k+1 metrics given shot k outcome)"
             % inst.upper()]
    for cls, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        sig = _agg([r["metrics"] for r in rs], "sigma_y")
        drp = _agg([r["metrics"] for r in rs], "fill_dropout_rate")
        ff = _agg([r["metrics"] for r in rs], "first_fill")
        ab = sum(1 for r in rs if outcome_class(r) == "abort") / n
        gr = sum(1 for r in rs if outcome_class(r) == "green") / n
        det = sum(1 for r in rs if r["metrics"].get("found")) / n
        lines.append("  after %-15s n=%2d%s: found=%.2f next_abort=%.2f next_green=%.2f "
                     "sigma_med=%s dropout_med=%s first_fill_med=%s"
                     % (cls, n, " (UNPOWERED)" if n < 10 else "", det, ab, gr,
                        fmt_cell(_med(sig)), fmt_cell(_med(drp)), fmt_cell(_med(ff))))
    return "\n".join(lines) + "\n"


def log_outcome_drift(ev):
    """LOG-ONLY outcome drift over EVERY session in the log (not just framedumped
    ones): split at >10 min gaps OR physical-epoch resets. Use the same causal
    per-press join as framedump scoring, including both abort families and
    releases without landings. These are engine proxies, not human outcomes."""
    presses = sorted((p for p in ev["press"] if p["epoch"] is not None),
                     key=lambda p: p["t"])
    # --extra-log may overlap the main log. An exact repeated record is not
    # a process restart, and must not split every shot into a one-row session.
    presses = list({(p["t"], p["epoch"], p.get("intent")): p for p in presses}.values())
    if not presses:
        return "LOG-WIDE OUTCOME DRIFT: no presses in log\n"
    sessions, cur = [], [presses[0]]
    for p in presses[1:]:
        if p["t"] - cur[-1]["t"] > 600 or p["epoch"] <= cur[-1]["epoch"]:
            sessions.append(cur)
            cur = []
        cur.append(p)
    sessions.append(cur)
    lines = ["LOG-WIDE OUTCOME DRIFT (engine peak_fill proxy; NOT human-banner grading)",
             "  Denominator: all logged presses, including unverified no-meter presses; "
             "abort families are deduplicated per press."]
    for si, ses in enumerate(sessions):
        lo, hi = ses[0]["t"], ses[-1]["t"] + 12
        if si + 1 < len(sessions):
            hi = min(hi, sessions[si + 1][0]["t"])
        # Clip at the NEXT epoch reset, not a symmetric time allowance that
        # can lend a previous process's seq/epoch to the following process.
        scoped = {key: [event for event in values if lo <= event["t"] < hi]
                  for key, values in ev.items()}
        scoped["press"] = ses
        runs = build_runs({"anchor": lo, "t_max_ms": (hi - lo) * 1000.0}, scoped)
        seqd = []
        for run in runs:
            e = {"t": run["press_utc"], "outcome": outcome_class(run), "err": None}
            ld = run["landing"]
            if grade_landing(ld) is not None and ld["green_end"] - ld["green_start"] <= 6.0:
                e["err"] = ld["peak_fill"] - (ld["green_start"] + ld["green_end"]) / 2.0
            seqd.append(e)
        n = len(seqd)
        if n < 6:
            continue
        start = datetime.fromtimestamp(ses[0]["t"], timezone.utc).strftime("%m-%d %H:%MZ")
        half = n // 2
        def _r(sub, o):
            return sum(1 for e in sub if e["outcome"] == o) / len(sub)
        errs = [(i + 1, abs(e["err"])) for i, e in enumerate(seqd) if e["err"] is not None]
        efit = _ols_ci([x for x, _ in errs], [y for _, y in errs]) if len(errs) >= 4 else None
        lines.append("  session %s n=%d presses: abort %.2f->%.2f  green %.2f->%.2f  "
                     "silent %.2f->%.2f  fired_ungraded %.2f->%.2f  (first->last half)%s"
                     % (start, n, _r(seqd[:half], "abort"), _r(seqd[half:], "abort"),
                        _r(seqd[:half], "green"), _r(seqd[half:], "green"),
                        _r(seqd[:half], "silent"), _r(seqd[half:], "silent"),
                        _r(seqd[:half], "fired_ungraded"), _r(seqd[half:], "fired_ungraded"),
                        "  [UNPOWERED n<12]" if n < 12 else ""))
        if efit:
            b, l, h = efit
            lines.append("      landing |err| vs index: n=%d slope=%+.3fpp/shot "
                         "[%.3f,%.3f]%s" % (len(errs), b, l, h,
                          "  <-- CI excludes 0" if not (l <= 0 <= h) else ""))
        # post-outcome on the log stream
        after = defaultdict(list)
        for prev, curq in zip(seqd, seqd[1:]):
            after[prev["outcome"]].append(curq["outcome"])
        for cls, nxt in sorted(after.items(), key=lambda kv: -len(kv[1])):
            if len(nxt) >= 3:
                lines.append("      after %-14s n=%2d -> next abort=%.2f green=%.2f "
                             "silent=%.2f" % (cls, len(nxt),
                              sum(1 for o in nxt if o == "abort") / len(nxt),
                              sum(1 for o in nxt if o == "green") / len(nxt),
                              sum(1 for o in nxt if o == "silent") / len(nxt)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  aggregation / report
# --------------------------------------------------------------------------- #

def _agg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return vals


def condition_table(run_rows, label_fn):
    groups = defaultdict(list)
    for r in run_rows:
        if r["metrics"].get("no_frames"):
            continue        # press fell where the dump has no frames (missing pngs)
        groups[label_fn(r)].append(r)
    table = {}
    for k, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        # A press whose window was truncated under ~1.4s by the NEXT press (double
        # tap / cancel) cannot contain its own full meter; a not-found there is a
        # press artifact, not a detection miss.  Excluded from the found-rate
        # denominator, counted in short_miss.
        short_miss = [r for r in rs
                      if not r["metrics"].get("found")
                      and r["metrics"].get("window_ms", RUN_WINDOW_MS) < 1400.0]
        rs = [r for r in rs if r not in short_miss]
        if not rs:
            continue
        n = len(rs)
        found = sum(1 for r in rs if r["metrics"].get("found"))
        ff = _agg([r["metrics"] for r in rs], "first_fill")
        gate = [r["metrics"].get("first_fill_le_gate") for r in rs
                if r["metrics"].get("first_fill_le_gate") is not None]
        table[k] = {
            "n": n, "found": found, "found_rate": found / n if n else None,
            "first_fill_med": _med(ff), "first_fill_p90": _p90(ff),
            "first_fill_le_gate_rate": (sum(gate) / len(gate)) if gate else None,
            "first_latency_med_ms": _med(_agg([r["metrics"] for r in rs], "first_latency_ms")),
            "churn_med": _med(_agg([r["metrics"] for r in rs], "churn")),
            "longest_lock_med_ms": _med(_agg([r["metrics"] for r in rs], "longest_lock_ms")),
            "dcx_med": _med(_agg([r["metrics"] for r in rs], "dcx_med")),
            "dcx_p90": _p90(_agg([r["metrics"] for r in rs], "dcx_p90")),
            "jitter_med": _med(_agg([r["metrics"] for r in rs], "jitter_med")),
            "dropout_med": _med(_agg([r["metrics"] for r in rs], "fill_dropout_rate")),
            "dropout_max": (max(_agg([r["metrics"] for r in rs], "fill_dropout_rate"))
                            if _agg([r["metrics"] for r in rs], "fill_dropout_rate")
                            else None),
            "sigma_y_med": _med(_agg([r["metrics"] for r in rs], "sigma_y")),
            "sigma_y_coarse_med": _med(_agg([r["metrics"] for r in rs], "sigma_y_coarse")),
            "slope_mean": (statistics.mean(_agg([r["metrics"] for r in rs], "slope_pct_ms"))
                           if _agg([r["metrics"] for r in rs], "slope_pct_ms") else None),
            "slope_sd": (statistics.stdev(_agg([r["metrics"] for r in rs], "slope_pct_ms"))
                         if len(_agg([r["metrics"] for r in rs], "slope_pct_ms")) >= 2 else None),
            "bad_shape_frames": sum(r["metrics"].get("bad_shape_frames", 0) for r in rs),
            "scoreboard_frames": sum(r["metrics"].get("scoreboard_frames", 0) for r in rs),
            "fired": sum(1 for r in rs if r["fired"]),
            "silent": sum(1 for r in rs if r["silent"]),
            "aborts": dict(Counter(a for r in rs for a in r["aborts"])),
            "landing_grades": dict(Counter(g for g in (grade_landing(r["landing"]) for r in rs)
                                           if g)),
            "unpowered": n < 8,
            "short_miss_excluded": len(short_miss),
        }
    return table


def fmt_cell(v, spec="%.2f"):
    if v is None:
        return "-"
    if isinstance(v, float):
        return spec % v
    return str(v)


def render_table(title, table):
    cols = [("n", "n", "%d"), ("found", "found", "%d"),
            ("found_rate", "found%", "%.2f"),
            ("first_fill_med", "ff_med", "%.1f"), ("first_fill_p90", "ff_p90", "%.1f"),
            ("first_fill_le_gate_rate", "ff<=33%", "%.2f"),
            ("first_latency_med_ms", "lat_ms", "%.0f"),
            ("churn_med", "churn", "%.3f"),
            ("longest_lock_med_ms", "lock_ms", "%.0f"),
            ("dcx_med", "dcx", "%.1f"), ("jitter_med", "jit", "%.2f"),
            ("dropout_med", "drop_md", "%.2f"), ("dropout_max", "drop_mx", "%.2f"),
            ("sigma_y_med", "sig_sub", "%.2f"), ("sigma_y_coarse_med", "sig_crs", "%.2f"),
            ("slope_mean", "slope", "%.4f"), ("slope_sd", "slope_sd", "%.4f"),
            ("fired", "fired", "%d"), ("silent", "silent", "%d")]
    lines = [title]
    hdr = "%-28s" % "condition" + "".join("%9s" % c[1] for c in cols) + "  flags"
    lines.append(hdr)
    for k, v in table.items():
        row = "%-28s" % (str(k)[:28])
        for key, _, spec in cols:
            row += "%9s" % fmt_cell(v.get(key), spec)
        flags = []
        if v.get("unpowered"):
            flags.append("UNPOWERED(n<8)")
        if v.get("short_miss_excluded"):
            flags.append("shortmiss=%d" % v["short_miss_excluded"])
        if v.get("bad_shape_frames"):
            flags.append("badshape=%d" % v["bad_shape_frames"])
        if v.get("scoreboard_frames"):
            flags.append("scoreboard=%d" % v["scoreboard_frames"])
        if v.get("landing_grades"):
            flags.append("grades=" + json.dumps(v["landing_grades"]))
        if v.get("aborts"):
            flags.append("aborts=" + json.dumps(v["aborts"]))
        row += "  " + " ".join(flags)
        lines.append(row)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def discover_sessions():
    out = []
    for d in sorted(_glob.glob(os.path.join(FRAMEDUMP_DIR, "session_*"))):
        if os.path.exists(os.path.join(d, "frames.csv")) and \
                _glob.glob(os.path.join(d, "f*_raw.png")):
            out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["replay", "score", "all"])
    ap.add_argument("--sessions", default="",
                    help="comma-separated session dir names (default: all with pngs)")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--extra-log", default="", help="optional older rotated log")
    ap.add_argument("--outdir", default="")
    ap.add_argument("--replay-cache", default="",
                    help="dir holding replay_<session>.csv to reuse (default: outdir)")
    ap.add_argument("--force-replay", action="store_true")
    ap.add_argument("--meter-model", default="",
                    help="production detector ONNX (default: models/orion_meter_detector.onnx)")
    ap.add_argument("--meter-style", default="",
                    help="override settings.json meter_style")
    ap.add_argument("--meter-color", default="",
                    help="override settings.json meter_color")
    args = ap.parse_args()

    outdir = args.outdir or os.path.join(BENCH_DIR, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)
    cache = args.replay_cache or outdir

    if args.sessions:
        sessions = [os.path.join(FRAMEDUMP_DIR, s.strip())
                    for s in args.sessions.split(",") if s.strip()]
    else:
        sessions = discover_sessions()

    logs = [args.log] + ([args.extra_log] if args.extra_log else [])
    ev = parse_log([p for p in logs if os.path.exists(p)])
    replay_profile = resolve_replay_profile(args.meter_model or None)
    reader_config = resolve_reader_config(
        meter_style=args.meter_style or None,
        meter_color=args.meter_color or None,
    )
    replay_id_by_session = {}

    if args.cmd in ("replay", "all"):
        os.makedirs(cache, exist_ok=True)
        for s in sessions:
            out_csv = os.path.join(cache, "replay_%s.csv" % os.path.basename(s))
            gens = load_session(s)
            runs_by_generation = {
                g["gen"]: build_runs(g, ev) for g in gens
            }
            profile_id = replay_profile_id(
                replay_profile, reader_config, runs_by_generation
            )
            replay_id_by_session[os.path.basename(s)] = profile_id
            if (
                os.path.exists(out_csv)
                and not args.force_replay
                and replay_cache_matches(out_csv, profile_id)
            ):
                print("cache hit:", out_csv)
                continue
            if os.path.exists(out_csv) and not args.force_replay:
                print("cache invalidated (profile/model/epochs changed):", out_csv)
            replay_session(
                s, out_csv, ev, replay_profile, reader_config,
                gens=gens, runs_by_generation=runs_by_generation,
                profile_id=profile_id,
            )
    if args.cmd == "replay":
        return

    all_rows = {"replay": [], "live": []}
    session_reports = {}
    for s in sessions:
        name = os.path.basename(s)
        gens = load_session(s)
        rep_csv = os.path.join(cache, "replay_%s.csv" % name)
        expected_runs = {g["gen"]: build_runs(g, ev) for g in gens}
        expected_profile_id = replay_profile_id(
            replay_profile, reader_config, expected_runs
        )
        replay_id_by_session[name] = expected_profile_id
        if os.path.exists(rep_csv) and not replay_cache_matches(
            rep_csv, expected_profile_id
        ):
            raise SystemExit(
                "replay cache does not match the production profile/model/epochs: "
                + rep_csv
                + " (run the replay or all command)"
            )
        rep_frames = load_replay(rep_csv) if os.path.exists(rep_csv) else {}
        srep = {"generations": len(gens), "gen_detail": [], "fp_control": {},
                "join": {}}
        for g in gens:
            runs = build_runs(g, ev)
            live = live_frames_for_gen(g)
            rep = sorted(rep_frames.get(g["gen"], []), key=lambda f: float(f["t_ms"]))
            gd = {"gen": g["gen"], "rows": len(g["rows"]), "pngs": len(g["png"]),
                  "presses": len(runs),
                  "anchor_utc": datetime.fromtimestamp(g["anchor"], timezone.utc)
                                        .isoformat()}
            # join-quality: press -> first rise onset (replay first det after press)
            deltas = []
            for r in runs:
                cand = [float(f["t_ms"]) for f in rep
                        if f["det"] == "1"
                        and r["press_ms"] - 300 <= float(f["t_ms"]) <= r["press_ms"] + 1500]
                if cand:
                    deltas.append(cand[0] - r["press_ms"])
            gd["press_to_onset_med_ms"] = _med(deltas)
            gd["press_to_onset_n"] = len(deltas)
            srep["gen_detail"].append(gd)
            for inst, frames in (("replay", rep), ("live", live)):
                if not frames:
                    continue
                for r in runs:
                    mr = score_run(r, frames)
                    all_rows[inst].append({
                        "session": name, "gen": g["gen"], "epoch": r["epoch"],
                        "shot_type": r["shot_type"] or "UNKNOWN",
                        "press_ms": r["press_ms"], "fired": r["fired"],
                        "silent": r["silent"], "aborts": r["aborts"],
                        "landing": r["landing"], "metrics": mr,
                        "meter_x": (r["landing"] or {}).get("meter_x")})
                srep.setdefault("fp_%s" % inst, {})[g["gen"]] = \
                    no_shot_fp_stats(frames, runs)
        session_reports[name] = srep

    # ---------------- report ----------------
    rpt = []
    rpt.append("CONSISTENCY BENCHMARK  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    rpt.append("sessions: %s" % ", ".join(os.path.basename(s) for s in sessions))
    rpt.append("log: %s" % args.log)
    model_hash = (
        file_sha256(replay_profile["ORION_METER_MODEL"])
        if os.path.isfile(replay_profile["ORION_METER_MODEL"])
        else "MISSING"
    )
    rpt.append(
        "reader profile: style=%s color=%s confidence_gate=%.2f "
        "detector_conf=%s timing_profile=%s"
        % (
            reader_config["meter_style"], reader_config["meter_color"],
            reader_config["confidence_threshold"],
            replay_profile["ORION_METER_DETECTOR_CONF"],
            replay_profile["ORION_TIMING_PROFILE_ID"],
        )
    )
    rpt.append(
        "meter model: %s sha256=%s"
        % (os.path.basename(replay_profile["ORION_METER_MODEL"]), model_hash)
    )
    if replay_id_by_session:
        rpt.append(
            "replay identities: "
            + " ".join(
                "%s=%s" % (name, identity)
                for name, identity in sorted(replay_id_by_session.items())
            )
        )
    rpt.append(
        "forced production env: "
        + " ".join(
            "%s=%s" % (key, replay_profile[key])
            for key in sorted(replay_profile)
            if key != "ORION_METER_MODEL"
        )
    )
    rpt.append("")
    for name, sr in session_reports.items():
        rpt.append("SESSION %s  generations=%d" % (name, sr["generations"]))
        for gd in sr["gen_detail"]:
            rpt.append("  gen%d rows=%d pngs=%d presses=%d anchor=%s "
                       "press->onset med=%s ms (n=%d)"
                       % (gd["gen"], gd["rows"], gd["pngs"], gd["presses"],
                          gd["anchor_utc"], fmt_cell(gd["press_to_onset_med_ms"], "%.0f"),
                          gd["press_to_onset_n"]))
        for inst in ("replay", "live"):
            fps = sr.get("fp_%s" % inst) or {}
            for gen, fp in fps.items():
                rpt.append("  FP(no-shot) [%s gen%s]: frames=%d det=%d badshape=%d "
                           "scoreboard=%d" % (inst, gen, fp["frames_outside"],
                                              fp["det_outside"], fp["bad_shape"],
                                              fp["scoreboard"]))
                for ex in fp["examples"]:
                    rpt.append("      ex idx=%s t=%sms fill=%s bbox=%s"
                               % (ex["idx"], ex["t_ms"], ex["fill"], ex["bbox"]))
        rpt.append("")
    for inst in ("replay", "live"):
        rows = all_rows[inst]
        if not rows:
            continue
        rpt.append("=" * 100)
        rpt.append("INSTRUMENT: %s   (runs=%d)" % (inst.upper(), len(rows)))
        rpt.append(render_table("--- by shot_type ---",
                                condition_table(rows, lambda r: r["shot_type"])))
        rpt.append(render_table("--- by session ---",
                                condition_table(rows, lambda r: r["session"])))
        rpt.append(render_table(
            "--- fade vs standstill vs other ---",
            condition_table(rows, lambda r: (
                "fade" if "Fade" in (r["shot_type"] or "") else
                "standstill" if r["shot_type"] == "Standstill" else "other"))))

        def _mx_band(r):
            mx = r.get("meter_x")
            if mx is None:
                return "mx_unknown"
            return "mx_left(<0.4)" if mx < 0.4 else ("mx_right(>0.6)" if mx > 0.6
                                                     else "mx_center")
        rpt.append(render_table("--- by meter screen-x (landing lines only) ---",
                                condition_table(rows, _mx_band)))
        drift_txt, _ = drift_section(rows, inst)
        rpt.append(drift_txt)
        rpt.append(post_outcome_section(rows, inst))
    rpt.append("=" * 100)
    rpt.append(log_outcome_drift(ev))
    text = "\n".join(rpt)
    with open(os.path.join(outdir, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)

    # machine-readable dump
    def _san(o):
        if isinstance(o, dict):
            return {k: _san(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_san(v) for v in o]
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        return o
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(_san({"sessions": session_reports, "runs": all_rows}),
                  fh, indent=1, default=str)
    print(text)
    print("\nreport written to", outdir)


if __name__ == "__main__":
    main()
