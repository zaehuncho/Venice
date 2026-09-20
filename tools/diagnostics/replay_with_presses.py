#!/usr/bin/env python
"""
replay_with_presses.py -- replay a framedump through the REAL SimpleMeterReader +
MeterContourLocator while DRIVING THE PHYSICAL PRESS TIMELINE from the native log.

WHY THIS EXISTS
---------------
`tools/diagnostics/replay_simple_reader.py` never issues a physical press: it only
uses the CV self-arm (a detected RISING meter refreshes the shot-gate deadline).
Every press-gated layer in the reader --

    ORION_READER_GHOST_FORGET_LOCATOR   (evict-at-press forgets the proposer's position)
    ORION_READER_FRESH_AFTER_GHOST      (the retired ghost's zone must re-earn a low sighting)
    ORION_READER_BOX_LATCH              (box identity latched once the press owns a meter)
    ORION_READER_STATIC_ZONE_QUARANTINE (a never-rising position publishes nothing)
    ORION_READER_IDLE_PUBLISH_GATE      (publish only when armed / rising / continuing)
    ORION_LOCATOR_IDLE_REUSE            (locator short-circuits a byte-identical idle frame)
    ORION_CV_TIPLESS_ARMED              (a tip-less column may be proposed inside a press)

-- is therefore DEAD CODE in that tool, which is exactly why the previous replays
reported "0 diff" while the same layers produced a live regression (10 blind releases
in 30 presses on 2026-09-16 14:15-14:21 local).

This harness replays the SAME frames but replays the PRESS TIMELINE with them, calling
the identical hooks `RemotePlayOrchestrator` calls:

    press  (`Physical shot epoch: epoch=N`  +  `shot_gate_arm send: ... shot_type=T`)
        -> reader.notify_physical_shot_start(epoch)
        -> reader.notify_physical_shot_type(epoch, shot_type, rhythm)
        -> reader.set_shot_state(True, 1.0, True)          # _arm_shot_gate
    close  (`shot_gate_release send` / `shot_gate_disarm send`, or the hw-arm timeout)
        -> reader.notify_physical_shot_release(epoch, release_ms, reason)
        -> reader.notify_release(...)                      # release-marker hook
    every frame
        -> reader.set_shot_state(armed, 0.0, armed_hw)     # _processing_loop, BEFORE detect
        -> reader.detect(frame, ts=<frame wall clock>)

"Reached the engine" is decided with the ORCHESTRATOR'S OWN predicate
(`_should_feed_engine`: detected + positive bbox + rejection_reason in
{'', 'green_not_found', 'fill_gated', 'dead_reckoned'}), re-implemented here byte for
byte, so a publication in this report is a publication the native engine would have seen.

READ-ONLY. Touches no detector/reader source, no git state, no live process. Writes only
under --out (and the frame cache under --cache-dir).

USAGE
-----
  # one configuration
  .venv/Scripts/python.exe tools/diagnostics/replay_with_presses.py \
      --session "D:/NexusVision/framedump/session_20260915_185359" \
      --log logs/orion_native.log.1 \
      --out "D:/NexusVision/framedump/_layer_ab" --config baseline

  # the whole A/B matrix (one SUBPROCESS per config: player_anchor.ARM/ANCHOR are
  # process-global singletons, so configs must not share an interpreter)
  .venv/Scripts/python.exe tools/diagnostics/replay_with_presses.py \
      --session "D:/NexusVision/framedump/session_20260915_185359" \
      --log logs/orion_native.log.1 \
      --out "D:/NexusVision/framedump/_layer_ab" --matrix

  # just the press table parsed out of the log (no replay)
  ... --presses-only
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import glob
import json
import os
import re
import subprocess
import sys
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# --------------------------------------------------------------------------- layers
# name -> env var.  Every one of these is a PUBLICATION/ACQUISITION policy layer that
# only a physical press can exercise.
LAYERS = {
    "GHOST_FORGET_LOCATOR": "ORION_READER_GHOST_FORGET_LOCATOR",
    "FRESH_AFTER_GHOST": "ORION_READER_FRESH_AFTER_GHOST",
    "BOX_LATCH": "ORION_READER_BOX_LATCH",
    "STATIC_ZONE_QUARANTINE": "ORION_READER_STATIC_ZONE_QUARANTINE",
    "IDLE_PUBLISH_GATE": "ORION_READER_IDLE_PUBLISH_GATE",
    "LOCATOR_IDLE_REUSE": "ORION_LOCATOR_IDLE_REUSE",
    "TIPLESS_ARMED": "ORION_CV_TIPLESS_ARMED",
}
CONFIGS = ["baseline"] + list(LAYERS) + ["all_on"]

# The NON-layer knobs the 2026-09-15 18:54 sidecar actually launched with, recovered from
# `Sidecar shipped timing profile` / `Sidecar env keys` in logs/orion_native.log.1 and from
# the handoff's launch line (anchor + "relaxation trio").  Pinned so the replay cannot
# inherit a stray shell environment and so every configuration differs ONLY by its layer.
LIVE_ENV = {
    "ORION_SIMPLE_READER": "1",
    "ORION_METER_PROPOSER": "cv",
    "ORION_METER_DETECTOR": "0",       # the locator is attached by hand (see _build_reader)
    "ORION_PLAYER_ANCHOR": "1",
    "ORION_ANCHORED_SEARCH": "1",
    "ORION_ANCHOR_REFUSE_MODE": "1",
    "ORION_EXPECTATION_WINDOW": "1",
    "ORION_CV_SHAPE_MIN_H_ARMED": "8",
    "ORION_CV_COL_W_MIN_ARMED": "6",
    "ORION_CV_OUTLINE_MIN": "0",
    "ORION_READER_ANCHOR": "1",
    "ORION_READER_PCTL_FILL": "1",
    "ORION_READER_TRACK_H_CAP": "1",
    "ORION_METER_SUBPIXEL_SESSION_RULER": "1",
    "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL": "1",
    "ORION_METER_SUBPIXEL_BASE_HOLD": "1",
    "ORION_METER_PARTIAL_OCCLUSION": "1",
    "ORION_METER_PARTIAL_OCCLUSION_MAX_MS": "120",
    "ORION_METER_NEGATIVE_BRIDGE_MAX_MS": "45",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT": "3",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_COLS": "2",
    # [ORION_PROOF_DETECTOR_BOX 2026-09-19] run_orion.local.ps1:575 launches the reader with
    # the display hug at mode 2. It was missing here, so every replay before today published the
    # DETECTOR rectangle while the live product published the hugged one -- which is exactly why
    # offline runs never reproduced the `break_geometry` restarts the live logs are full of.
    "ORION_READER_BOX_TIGHT": "2",
    "ORION_COLOR_CAL": "0",
    "ORION_GREEN_SELF_GRADE": "0",
    "ORION_DETDIAG": "0",
}

# Orchestrator shot-gate constants (remote_play_orchestrator.py ~1558).
GATE_MAX_SECONDS = 20.0
GATE_POST_RELEASE_SECONDS = 3.0

# The blind-release deadline the engine fires its METER BACKSTOP on: law hold + grace.
# Defaults are the owner-stated pair; the log's own METER BACKSTOP lines carry the exact
# law per type for this session and are reported alongside.
DEADLINE_MS = {"standstill": 650.0 + 100.0, "fade": 940.0 + 220.0}
LAW_FROM_LOG = {"Standstill": 650.0 + 100.0,
                "Right_Fade": 950.0 + 100.0,
                "Left_Fade": 933.3 + 100.0}

GHOST_AT_PRESS_PCT = 40.0     # the engine's own first-sight anchor bound

# Reader stages/reasons that mean "a layer (or an older breaker) WITHHELD a read".
WITHHOLD_REASONS = {
    "ghost_static_press": "ghost breaker evicted/suppressed the leftover (pre-existing)",
    "cold_first_read_unproven": "cold first-read veto (pre-existing)",
    "ghost_zone_unproven": "FRESH_AFTER_GHOST refused a re-seed in the retired ghost's zone",
    "static_zone_quarantined": "STATIC_ZONE_QUARANTINE refused a never-risen position",
}

_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z\s")
_PRESS_RE = re.compile(r"Physical shot epoch: epoch=(\d+) intent=(\S+)")
_ARM_RE = re.compile(r"shot_gate_arm send: epoch=(\d+) source=(\S+) sent=(\d+)"
                     r"(?: shot_type=(\S+) rhythm=(\d+))?")
_REL_RE = re.compile(r"shot_gate_release send: epoch=(\d+) release_ms=([\d.]+)")
_DIS_RE = re.compile(r"shot_gate_disarm send: epoch=(\d+)(?: reason=(\S+))?")
_BACKSTOP_RE = re.compile(r"METER BACKSTOP: fired type=(.+?) hold_ms=([\d.]+) "
                          r"law_hold_ms=([\d.]+).*?epoch=(\d+).*?grace_ms=([\d.]+)")


def _iso_to_epoch(s: str) -> float:
    """'2026-09-16T01:37:42.735' -> epoch seconds (the native log stamps UTC).

    Verified against the log's own `release_ms=` field, which is the same clock as
    framedump frames.csv `t_wall` (release_ms=1789522662734.1 on the line stamped
    2026-09-16T01:37:42.735Z; frames.csv idx 0 t_wall=1789522662.602440)."""
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=_dt.timezone.utc).timestamp()


# =========================================================================== the log
def parse_log(log_path, t0, t1, pad_s=30.0):
    """-> (presses, backstops).  presses = [{epoch, press_t, intent, shot_type, rhythm,
    close_t, close_reason, release_ms}] for every press whose press_t is in the padded
    window, ordered by time."""
    presses, by_epoch, backstops = [], {}, {}
    lo, hi = t0 - pad_s, t1 + pad_s
    with open(log_path, "rb") as f:
        for raw in f:
            line = raw.decode("utf-8", "replace")
            m = _TS_RE.match(line)
            if not m:
                continue
            ts = _iso_to_epoch(m.group(1))
            if ts < lo or ts > hi:
                continue
            mp = _PRESS_RE.search(line)
            if mp:
                ep = int(mp.group(1))
                rec = {"epoch": ep, "press_t": ts, "intent": mp.group(2),
                       "shot_type": "", "rhythm": 0, "close_t": None,
                       "close_reason": "", "release_ms": None}
                presses.append(rec)
                by_epoch[ep] = rec
                continue
            ma = _ARM_RE.search(line)
            if ma:
                rec = by_epoch.get(int(ma.group(1)))
                if rec is not None and ma.group(4):
                    rec["shot_type"] = ma.group(4)
                    rec["rhythm"] = int(ma.group(5) or 0)
                continue
            mr = _REL_RE.search(line)
            if mr:
                rec = by_epoch.get(int(mr.group(1)))
                if rec is not None and rec["close_t"] is None:
                    rec["close_t"] = ts
                    rec["close_reason"] = "release"
                    rec["release_ms"] = float(mr.group(2))
                continue
            md = _DIS_RE.search(line)
            if md:
                rec = by_epoch.get(int(md.group(1)))
                if rec is not None and rec["close_t"] is None:
                    rec["close_t"] = ts
                    rec["close_reason"] = md.group(2) or "disarm"
                continue
            mb = _BACKSTOP_RE.search(line)
            if mb:
                backstops[int(mb.group(4))] = {
                    "type": mb.group(1), "hold_ms": float(mb.group(2)),
                    "law_hold_ms": float(mb.group(3)), "grace_ms": float(mb.group(5)),
                    "fired_t": ts}
    presses.sort(key=lambda r: r["press_t"])
    # A press with no close edge expires on the hw-arm timeout.
    for rec in presses:
        if rec["close_t"] is None:
            rec["close_t"] = rec["press_t"] + GATE_MAX_SECONDS
            rec["close_reason"] = "hw_arm_timeout"
    return presses, backstops


# ======================================================================== the frames
def load_frames_csv(session_dir):
    rows = []
    with open(os.path.join(session_dir, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "idx": int(r["idx"]), "t": float(r["t_wall"]),
                "live_det": int(r["detected"]), "live_fill": float(r["fill_pct"]),
                "live_conf": float(r["conf"]), "live_reject": r["rejection"] or "",
                "live_bbox": (int(r["bbox_x"]), int(r["bbox_y"]),
                              int(r["bbox_w"]), int(r["bbox_h"])),
            })
    rows.sort(key=lambda r: r["t"])
    return rows


def _png_for(session_dir, idx):
    # [2026-09-19] The press-window dumper gained a per-epoch prefix and a 6-digit index and
    # writes JPEG (`ep7_f000123_0_raw.jpg`); the original 5-digit PNG naming is still written by
    # the older whole-session dumper. Accept both, newest naming first, or every Sept-17/18
    # session loads as "no decodable frames".
    for pattern in ("ep*_f%06d_?_raw.jpg" % idx, "ep*_f%06d_?_raw.png" % idx,
                    "f%05d_?_raw.png" % idx, "f%05d_?_raw.jpg" % idx):
        hits = sorted(glob.glob(os.path.join(session_dir, pattern)))
        if hits:
            return hits[0]
    return None


class FrameCache:
    """Decoded-frame cache on D:.  The dump's PNGs cost ~82 ms each to decode; a 9-config
    matrix over 824 frames would spend ~10 minutes in libpng alone.  One uint8 memmap
    (N,H,W,3) costs 2.8 MB/frame on disk and ~0 ms to read."""

    def __init__(self, session_dir, cache_dir, rows, rebuild=False):
        import numpy as np
        self._np = np
        name = os.path.basename(session_dir.rstrip("/\\"))
        self.dir = os.path.join(cache_dir, name)
        os.makedirs(self.dir, exist_ok=True)
        self.meta_path = os.path.join(self.dir, "index.json")
        self.npy_path = os.path.join(self.dir, "frames_u8.npy")
        self.rows = rows
        meta = None
        if not rebuild and os.path.exists(self.meta_path) and os.path.exists(self.npy_path):
            try:
                meta = json.load(open(self.meta_path))
            except Exception:
                meta = None
        if meta and meta.get("n") == len(rows) and meta.get("idx") == [r["idx"] for r in rows]:
            self.shape = tuple(meta["shape"])
            self.arr = np.lib.format.open_memmap(self.npy_path, mode="r")
            self.miss = set(meta.get("missing", []))
            return
        self.miss = self._build(session_dir, rows)
        self.arr = np.lib.format.open_memmap(self.npy_path, mode="r")

    def _build(self, session_dir, rows):
        import cv2
        np = self._np
        shape = None
        for r in rows:
            p = _png_for(session_dir, r["idx"])
            if p:
                im = cv2.imread(p, cv2.IMREAD_COLOR)
                if im is not None:
                    shape = im.shape
                    break
        if shape is None:
            raise SystemExit("no decodable frames in %s" % session_dir)
        self.shape = shape
        arr = np.lib.format.open_memmap(
            self.npy_path, mode="w+", dtype=np.uint8,
            shape=(len(rows), shape[0], shape[1], shape[2]))
        missing, t0 = [], _time.perf_counter()
        for i, r in enumerate(rows):
            p = _png_for(session_dir, r["idx"])
            im = cv2.imread(p, cv2.IMREAD_COLOR) if p else None
            if im is None or im.shape != shape:
                missing.append(r["idx"])
                arr[i] = 0
            else:
                arr[i] = im
            if (i + 1) % 100 == 0:
                sys.stderr.write("  cache %d/%d (%.0fs)\n" % (
                    i + 1, len(rows), _time.perf_counter() - t0))
        arr.flush()
        del arr
        json.dump({"n": len(rows), "shape": list(shape),
                   "idx": [r["idx"] for r in rows], "missing": missing},
                  open(self.meta_path, "w"))
        return set(missing)

    def get(self, i):
        return self._np.asarray(self.arr[i])


# ========================================================================= the replay
class _Cfg:
    """Live det_config for this session: `CV detector = SimpleMeterReader ...
    meter_color=White style=Arrow2` (logs/orion_native.log.1, 2026-09-15T23:54:05Z)."""
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32
    park_temporal_enabled = False


class _Gate:
    """The orchestrator's dual-bound shot gate, wall-clock half only (the frame-sequence
    bound is 2400 frames = never binding at this dump's rate)."""

    def __init__(self):
        self.deadline = -1.0
        self.hw_deadline = -1.0

    def arm(self, t):
        self.deadline = t + GATE_MAX_SECONDS
        self.hw_deadline = t + GATE_MAX_SECONDS

    def settle(self, t):
        # _settle_shot_gate("release"): trim both deadlines to now + the post-release tail.
        if self.deadline >= 0.0:
            self.deadline = min(self.deadline, t + GATE_POST_RELEASE_SECONDS)
        if self.hw_deadline >= 0.0:
            self.hw_deadline = min(self.hw_deadline, t + GATE_POST_RELEASE_SECONDS)

    def state(self, t):
        return (t <= self.deadline, t <= self.hw_deadline)


def _should_feed_engine(res) -> bool:
    """`RemotePlayOrchestrator._should_feed_engine`, verbatim."""
    if not res or not getattr(res, "detected", False):
        return False
    bbox = getattr(res, "bbox", None)
    if not bbox or bbox[2] <= 0 or bbox[3] <= 0:
        return False
    return getattr(res, "rejection_reason", "") in (
        "", "green_not_found", "fill_gated", "dead_reckoned")


def _is_raw_accepted(res) -> bool:
    """`RemotePlayOrchestrator._is_raw_accepted`, verbatim."""
    if not res or not getattr(res, "detected", False):
        return False
    bbox = getattr(res, "bbox", None)
    if not bbox or bbox[2] <= 0 or bbox[3] <= 0:
        return False
    return getattr(res, "rejection_reason", "") in ("", "green_not_found")


def _apply_env(config):
    for k, v in LIVE_ENV.items():
        os.environ[k] = v
    for name, key in LAYERS.items():
        if config == "all_on":
            os.environ[key] = "1"
        elif config == name:
            os.environ[key] = "1"
        else:
            os.environ[key] = "0"


def _build_reader():
    """The orchestrator's wiring (remote_play_orchestrator.py ~1710) with the CV proposer
    attached by hand, exactly as tests/test_reader_fresh_onset_lock.py does."""
    import meter_locator_cv as mlc
    import player_anchor as pa
    from meter_detector_yolo import AsyncMeterLocator
    from simple_meter_reader import SimpleMeterReader
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)
    r = SimpleMeterReader(cfg=_Cfg(), require_gameplay_eligibility=True)
    r._meter_detector = AsyncMeterLocator(base=mlc.MeterContourLocator(), sync=True)
    try:
        r.set_active_style("Arrow2")
    except Exception:
        pass
    return r


def replay(session_dir, log_path, config, cache_dir, limit=None, windows=None):
    rows = load_frames_csv(session_dir)
    if limit:
        rows = rows[:limit]
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    presses, backstops = parse_log(log_path, t0, t1)
    if windows:
        keep = set()
        for p in presses:
            for i, r in enumerate(rows):
                if p["press_t"] - 0.6 <= r["t"] <= p["close_t"] + 1.5:
                    keep.add(i)
        rows = [r for i, r in enumerate(rows) if i in keep]

    cache = FrameCache(session_dir, cache_dir, rows)
    reader = _build_reader()

    # press events, in time order, as (t, kind, press_record)
    events = []
    for p in presses:
        events.append((p["press_t"], "press", p))
        events.append((p["close_t"], "close", p))
    events.sort(key=lambda e: e[0])
    ei = 0

    gate = _Gate()
    per_frame = []
    prev_t = rows[0]["t"] - 1.0
    idle_unpub_prev = 0
    t_detect = 0.0

    for i, r in enumerate(rows):
        ts = r["t"]
        while ei < len(events) and events[ei][0] <= ts:
            et, kind, p = events[ei]
            ei += 1
            if et <= prev_t:
                continue
            if kind == "press":
                # _arm_shot_gate(source, epoch, notify_reader_start=True)
                try:
                    reader.notify_physical_shot_start(int(p["epoch"]))
                except Exception:
                    pass
                try:
                    reader.notify_physical_shot_type(
                        int(p["epoch"]), str(p["shot_type"] or ""), bool(p["rhythm"]))
                except Exception:
                    pass
                gate.arm(et)
                reader.set_shot_state(True, 1.0, True)
            else:
                try:
                    reader.notify_physical_shot_release(
                        int(p["epoch"]),
                        p["release_ms"] if p["release_ms"] is not None else None,
                        p["close_reason"])
                except Exception:
                    pass
                if p["close_reason"] == "release":
                    try:
                        reader.notify_release(i, physical_epoch=int(p["epoch"]),
                                              shot_attempt=int(p["epoch"]),
                                              identity_verified=True)
                    except Exception:
                        pass
                    gate.settle(et)
        armed, armed_hw = gate.state(ts)
        reader.set_shot_state(armed, 0.0, armed_hw)
        img = cache.get(i)
        _c0 = _time.perf_counter()
        res = reader.detect(img, ts=ts)
        t_detect += _time.perf_counter() - _c0

        dbg = getattr(reader, "last_debug", None) or {}
        try:
            loc_found = bool(reader._meter_detector.latest()[0])
        except Exception:
            loc_found = False
        idle_unpub = int((getattr(reader, "_det_diag", None) or {}).get("idle_unpublished", 0))
        per_frame.append({
            "i": i, "idx": r["idx"], "t": ts,
            "armed": int(armed), "armed_hw": int(armed_hw),
            "det": int(bool(getattr(res, "detected", False))),
            "fed": int(_should_feed_engine(res)),
            "raw": int(_is_raw_accepted(res)),
            "fill": round(float(getattr(res, "fill_pct", 0.0) or 0.0), 2),
            "vel": round(float(getattr(res, "fill_velocity_pct_s", 0.0) or 0.0), 1),
            "bbox": [int(v) for v in (getattr(res, "bbox", (0, 0, 0, 0)) or (0, 0, 0, 0))],
            # [ORION_PROOF_DETECTOR_BOX] the same frame's rectangle BEFORE the display hug --
            # what native now judges ownership geometry on (`det_bbox` on the wire).
            "det_box": [int(v) for v in ((dbg.get("det_box")
                                          or getattr(res, "bbox", (0, 0, 0, 0))
                                          or (0, 0, 0, 0))[:4])],
            "reason": str(getattr(res, "rejection_reason", "") or ""),
            "stage": str(dbg.get("stage", "") or ""),
            "loc_found": int(loc_found),
            "idle_gated": int(idle_unpub > idle_unpub_prev),
            "live_det": r["live_det"], "live_fill": r["live_fill"],
            "live_reject": r["live_reject"],
        })
        idle_unpub_prev = idle_unpub
        prev_t = ts

    health = {}
    try:
        dg = dict(getattr(reader, "_det_diag", {}) or {})
        health = {k: dg.get(k, 0) for k in
                  ("calls", "found", "fresh", "seeded", "nofound", "lock", "drop",
                   "reseed", "reseed_refused", "loc_forget", "idle_unpublished",
                   "outlier", "rescue")}
        health["static_zone_withheld"] = int(getattr(reader, "_static_zone_withheld", 0))
        health["static_zones"] = len(getattr(reader, "_static_zones", []) or [])
        health["box_latch_refused"] = int(getattr(reader, "_box_latch_refused", 0))
        health["press_fresh_withheld"] = int(getattr(reader, "_press_fresh_withheld", 0))
        health["loc_forget_n"] = int(getattr(reader, "_loc_forget_n", 0))
        try:    # the locator's own cv={...} census (hit / no_tip / idle_reuse / ...)
            health["cv"] = str(reader._proposer_stats())
        except Exception:
            pass
    except Exception:
        pass

    shots = score(per_frame, presses, backstops)
    return {"config": config,
            "session": os.path.basename(session_dir.rstrip("/\\")),
            "frames": len(per_frame), "presses": len(shots),
            "detect_ms_mean": round(t_detect / max(1, len(per_frame)) * 1000.0, 2),
            "health": health, "shots": shots,
            "summary": summarize(shots, per_frame)}, per_frame


def _deadline_for(shot_type):
    st = (shot_type or "").strip()
    if st in LAW_FROM_LOG:
        log_ms = LAW_FROM_LOG[st]
    else:
        log_ms = None
    if st.lower().startswith("standstill"):
        return DEADLINE_MS["standstill"], log_ms
    if "fade" in st.lower():
        return DEADLINE_MS["fade"], log_ms
    return None, log_ms          # Go-To / unclassified: no comparable blind law


def score(per_frame, presses, backstops):
    """Per press: first publication, deadline verdict, ghost-at-press, withhold events."""
    out = []
    n = len(per_frame)
    for p in presses:
        pt, ct = p["press_t"], p["close_t"]
        # frames strictly inside [press, close + 1.5s]
        win = [f for f in per_frame if pt <= f["t"] <= ct + 1.5]
        if not win:
            continue
        before = [f for f in per_frame if f["t"] < pt]
        ghost_live = 0.0
        ghost_cfg = 0.0
        for f in reversed(before[-6:]):
            if pt - f["t"] > 0.6:
                break
            if f["live_det"] and f["live_fill"] > ghost_live:
                ghost_live = f["live_fill"]
            if f["fed"] and f["fill"] > ghost_cfg:
                ghost_cfg = f["fill"]
        first_fed = next((f for f in win if f["fed"]), None)
        first_raw = next((f for f in win if f["raw"]), None)
        first_live = next((f for f in win if f["live_det"]), None)
        dl, dl_log = _deadline_for(p["shot_type"])
        fed_ms = ((first_fed["t"] - pt) * 1000.0) if first_fed else None
        raw_ms = ((first_raw["t"] - pt) * 1000.0) if first_raw else None
        live_ms = ((first_live["t"] - pt) * 1000.0) if first_live else None
        withholds = []
        for f in win:
            if f["fed"]:
                continue
            why = ""
            if f["reason"] in WITHHOLD_REASONS:
                why = f["reason"]
            elif f["idle_gated"]:
                why = "idle_publish_gate"
            elif f["loc_found"] and not f["det"]:
                why = "locator_found_reader_silent:" + (f["reason"] or f["stage"] or "?")
            if why:
                withholds.append({"idx": f["idx"], "ms": round((f["t"] - pt) * 1000.0, 1),
                                  "why": why, "live_fill": f["live_fill"],
                                  "live_det": f["live_det"]})
        out.append({
            "epoch": p["epoch"], "shot_type": p["shot_type"] or "unclassified",
            "intent": p["intent"], "press_t": pt,
            "close_reason": p["close_reason"],
            "hold_ms": round((ct - pt) * 1000.0, 1),
            "frames_in_window": len(win),
            "ghost_at_press_live": round(ghost_live, 1),
            "ghost_at_press_cfg": round(ghost_cfg, 1),
            "ghost_at_press": bool(ghost_live >= GHOST_AT_PRESS_PCT),
            "first_fed_ms": round(fed_ms, 1) if fed_ms is not None else None,
            "first_fed_fill": first_fed["fill"] if first_fed else None,
            "first_raw_ms": round(raw_ms, 1) if raw_ms is not None else None,
            "first_live_ms": round(live_ms, 1) if live_ms is not None else None,
            "deadline_ms": dl, "deadline_ms_log": dl_log,
            "in_time": (None if dl is None else
                        bool(fed_ms is not None and fed_ms <= dl)),
            "in_time_log": (None if dl_log is None else
                            bool(fed_ms is not None and fed_ms <= dl_log)),
            "live_backstop": p["epoch"] in backstops,
            "withholds": withholds,
        })
    return out


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0, 1)


def summarize(shots, per_frame):
    gradable = [s for s in shots if s["deadline_ms"] is not None]
    ghosts = [s for s in shots if s["ghost_at_press"]]
    return {
        "presses": len(shots),
        "gradable": len(gradable),
        "published_at_all": sum(1 for s in shots if s["first_fed_ms"] is not None),
        "blind": sum(1 for s in shots if s["first_fed_ms"] is None),
        "in_time": sum(1 for s in gradable if s["in_time"]),
        "in_time_log_law": sum(1 for s in gradable if s["in_time_log"]),
        "median_first_fed_ms": _median([s["first_fed_ms"] for s in shots]),
        "median_first_raw_ms": _median([s["first_raw_ms"] for s in shots]),
        "ghost_at_press": len(ghosts),
        "ghost_at_press_in_time": sum(1 for s in ghosts
                                      if s["deadline_ms"] is not None and s["in_time"]),
        "ghost_at_press_blind": sum(1 for s in ghosts if s["first_fed_ms"] is None),
        "withhold_frames": sum(len(s["withholds"]) for s in shots),
        "frames_locator_found_reader_silent": sum(
            1 for f in per_frame if f["loc_found"] and not f["fed"]),
        "live_agreement": round(
            sum(1 for f in per_frame if f["fed"] == f["live_det"]) / max(1, len(per_frame)), 4),
    }


# =========================================================================== reporting
def print_report(res):
    s = res["summary"]
    print("\n=== %s / %s ===" % (res["session"], res["config"]))
    print(json.dumps(s, indent=2))
    print("%-6s %-14s %6s %7s %8s %8s %6s %s" % (
        "epoch", "type", "ghost", "firstFed", "deadline", "inTime", "wh", "backstop"))
    for sh in res["shots"]:
        print("%-6d %-14s %6.1f %7s %8s %8s %6d %s" % (
            sh["epoch"], sh["shot_type"][:14], sh["ghost_at_press_live"],
            ("-" if sh["first_fed_ms"] is None else "%.0f" % sh["first_fed_ms"]),
            ("-" if sh["deadline_ms"] is None else "%.0f" % sh["deadline_ms"]),
            ("n/a" if sh["in_time"] is None else ("YES" if sh["in_time"] else "NO")),
            len(sh["withholds"]), "BACKSTOP" if sh["live_backstop"] else ""))


def matrix(args):
    rows = []
    for cfg in CONFIGS:
        out_json = os.path.join(args.out, "%s__%s.json" % (
            os.path.basename(args.session.rstrip("/\\")), cfg))
        cmd = [sys.executable, os.path.abspath(__file__),
               "--session", args.session, "--log", args.log, "--out", args.out,
               "--cache-dir", args.cache_dir, "--config", cfg, "--quiet"]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        if args.windows_only:
            cmd += ["--windows-only"]
        sys.stderr.write("[matrix] %s\n" % cfg)
        # The reader logs its DETECTOR HEALTH / PICKUP / ORACLE lines to stdout; capture
        # them into the out dir instead of flooding the matrix report.
        with open(os.path.join(args.out, "%s.log" % cfg), "wb") as lf:
            r = subprocess.run(cmd, cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            sys.stderr.write("[matrix] %s FAILED rc=%d (see %s.log)\n" % (
                cfg, r.returncode, cfg))
            continue
        rows.append(json.load(open(out_json)))
    print("\n================= A/B MATRIX =================")
    hdr = ("%-24s %5s %6s %6s %7s %9s %9s %7s %7s %7s" % (
        "config", "press", "pubbed", "blind", "inTime", "medFedMs", "medRawMs",
        "ghost", "ghBlind", "withh"))
    print(hdr)
    for r in rows:
        s = r["summary"]
        print("%-24s %5d %6d %6d %7s %9s %9s %7d %7d %7d" % (
            r["config"], s["presses"], s["published_at_all"], s["blind"],
            "%d/%d" % (s["in_time"], s["gradable"]),
            s["median_first_fed_ms"], s["median_first_raw_ms"],
            s["ghost_at_press"], s["ghost_at_press_blind"], s["withhold_frames"]))
    base = next((r for r in rows if r["config"] == "baseline"), None)
    if base:
        bl = {s["epoch"]: s for s in base["shots"]}
        print("\n--- per-press DELTA vs baseline (first-publication ms; +late / -early) ---")
        for r in rows:
            if r["config"] == "baseline":
                continue
            diffs, lost, gained = [], [], []
            for sh in r["shots"]:
                b = bl.get(sh["epoch"])
                if not b:
                    continue
                if b["first_fed_ms"] is not None and sh["first_fed_ms"] is None:
                    lost.append(sh["epoch"])
                elif b["first_fed_ms"] is None and sh["first_fed_ms"] is not None:
                    gained.append(sh["epoch"])
                elif b["first_fed_ms"] is not None and sh["first_fed_ms"] is not None:
                    d = sh["first_fed_ms"] - b["first_fed_ms"]
                    if abs(d) > 0.5:
                        diffs.append((sh["epoch"], round(d, 1)))
            print("%-24s lost=%s gained=%s shifted=%s" % (
                r["config"], lost or "-", gained or "-", diffs or "-"))
    json.dump([{k: v for k, v in r.items() if k != "shots"} for r in rows],
              open(os.path.join(args.out, "matrix_summary.json"), "w"), indent=2)
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--config", default="baseline", choices=CONFIGS)
    ap.add_argument("--matrix", action="store_true", help="run every config in a subprocess")
    ap.add_argument("--presses-only", action="store_true")
    ap.add_argument("--windows-only", action="store_true",
                    help="replay only frames inside press windows (big dumps)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    a.cache_dir = a.cache_dir or os.path.join(a.out, "cache")
    os.makedirs(a.cache_dir, exist_ok=True)

    if a.presses_only:
        rows = load_frames_csv(a.session)
        presses, backstops = parse_log(a.log, rows[0]["t"], rows[-1]["t"])
        print(json.dumps({"frames": len(rows),
                          "frame_window": [rows[0]["t"], rows[-1]["t"]],
                          "frame_window_utc": [
                              _dt.datetime.fromtimestamp(rows[0]["t"], _dt.timezone.utc).isoformat(),
                              _dt.datetime.fromtimestamp(rows[-1]["t"], _dt.timezone.utc).isoformat()],
                          "presses": presses, "backstops": backstops}, indent=2))
        return

    if a.matrix:
        matrix(a)
        return

    _apply_env(a.config)
    res, per_frame = replay(a.session, a.log, a.config, a.cache_dir,
                            limit=a.limit, windows=a.windows_only)
    stem = "%s__%s" % (os.path.basename(a.session.rstrip("/\\")), a.config)
    json.dump(res, open(os.path.join(a.out, stem + ".json"), "w"), indent=2)
    with open(os.path.join(a.out, stem + ".frames.jsonl"), "w") as f:
        for r in per_frame:
            f.write(json.dumps(r) + "\n")
    if not a.quiet:
        print_report(res)
    else:
        print(json.dumps(res["summary"]))


if __name__ == "__main__":
    main()
