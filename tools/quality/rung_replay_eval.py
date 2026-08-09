#!/usr/bin/env python
"""rung_replay_eval.py -- M4 rung A/B harness: ADAPTIVE vs FROZEN meter reader per rung.

Replays an existing 1080p framedump session through the REAL RemotePlaySession bandwidth
ladder (reusing tools/diagnostics/reencode_ladder.py's encoder templates + decode loop) and,
for each rung, runs TWO CompressedMeterReader variants over the SAME decoded frames:

  * ADAPTIVE = CompressedMeterReader()                       -- auto quality (q_session-driven
    ReaderParams + luma-tracking gate; the M4 quality layer choosing its own row per frame).
  * FROZEN   = CompressedMeterReader(params=ReaderParams(),  -- the capture-card q=1 row pinned
               luma_tracking=True)                              (quality adaptation disabled, luma
                                                                 branches available but frozen).

Both variants are judged against a PRISTINE baseline (SimpleMeterReader on the original PNGs,
run once and shared across every rung). Per rung we report, adaptive vs frozen ADJACENT with an
(adaptive - frozen) delta column:

  * detection recall (both / pristine-only / rung-only) vs the pristine baseline
  * per-phase recall bucketed by the PRISTINE reader's rise_state (rising / peak / spent) --
    never aggregate-only, because peak recall can hide a dead rising edge
  * fill |delta| stats (mean / median / p90) on frames BOTH detect
  * the ADAPTIVE reader's OBSERVED median q_session (from last_debug) next to the rung name,
    beside the STATIC rung prior it "should" land near, so the q calibration is visible

HONESTY CLAUSES (read before believing numbers), inherited from reencode_ladder:
  * Framedumps are throttled (~15 fps dump cadence, 64.6 ms/idx here), NOT 60 fps -- consecutive
    frames carry ~4x the real motion. dup mode repeats each frame to the rung fps so the per-frame
    BIT BUDGET is realistic, but it UNDERSTATES temporal artifacts (bursty motion, no true 60fps
    inter-frame residual). Truth lies between seq and dup; the dual-capture rig (M5) is the real
    source. Use these numbers for RELATIVE (adaptive vs frozen, rung vs rung) conclusions only.
  * x264 != the PS5 hardware encoder (different RDO/AQ). Augmentation-grade only.
  * detection flag mirrors reencode_ladder: detected AND stage != 'coast' (a memory-coast is not
    a fresh detection). The compressed 'stale' hold is counted as detected -- the reader is still
    asserting the lock -- and any hold lag surfaces in the fill |delta| stats.

Usage (repo root):
  .venv311/Scripts/python.exe tools/quality/rung_replay_eval.py \
      [--session session_20260704_210801] [--rungs Balanced,UltraLow] [--mode dup] \
      [--max-frames 600] [--no-arm]
Outputs: logs/diagnostics/rung_replay_eval/<session>/report.txt + report.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

# --------------------------------------------------------------------------- #
#  sys.path discipline: the repo root MUST be sys.path[0] so the PRODUCTION
#  root simple_meter_reader.py wins over the retired tools/diagnostics twin
#  that shadows it whenever tools/diagnostics lands on the path first.
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
try:
    sys.path.remove(_REPO)
except ValueError:
    pass
sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402

from simple_meter_reader import SimpleMeterReader, ReaderParams  # noqa: E402
from compressed_meter_reader import CompressedMeterReader  # noqa: E402
from stream_quality import rung_prior  # noqa: E402


def _load_reencode_ladder():
    """Load tools/diagnostics/reencode_ladder.py by explicit path (NOT by name, so its
    sibling simple_meter_reader shadow never gets a chance to load). The module is registered
    in sys.modules BEFORE exec_module -- Python 3.14 dataclass machinery resolves the defining
    module via sys.modules[__module__], and reencode_ladder pulls in dataclass-bearing deps."""
    path = os.path.join(_REPO, "tools", "diagnostics", "reencode_ladder.py")
    spec = importlib.util.spec_from_file_location("reencode_ladder_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_RL = _load_reencode_ladder()
RUNGS = _RL.RUNGS
MS_PER_IDX = 64.6  # dump cadence for session_20260704_210801 (detframes fit)

CAVEATS = [
    "dump cadence ~15 fps (64.6 ms/idx), NOT 60 fps: consecutive frames carry ~4x real motion.",
    "dup mode makes the per-frame BIT BUDGET realistic but UNDERSTATES temporal artifacts.",
    "x264 != the PS5 hardware encoder; augmentation-grade. RELATIVE comparisons only.",
    "detection flag = detected AND stage != 'coast' (pristine and rung use the SAME rule);",
    "  'stale' holds count as detected -- any hold lag shows up in the fill |delta| stats.",
]


# --------------------------------------------------------------------------- #
#  reader builders (factored so the construction contract is unit-testable)
# --------------------------------------------------------------------------- #
def build_adaptive_reader() -> CompressedMeterReader:
    """The M4 quality-adaptive reader: default construction -> auto quality (q_session drives
    ReaderParams.for_quality + the luma-tracking gate). _luma_gate_auto is True."""
    return CompressedMeterReader()


def build_frozen_reader() -> CompressedMeterReader:
    """The pinned capture-card q=1 row: explicit ReaderParams() disables quality adaptation;
    luma_tracking=True keeps the luma branches available but at the frozen q=1 thresholds
    (NCC_LOCK stays 0.60, the shipped constant)."""
    return CompressedMeterReader(params=ReaderParams(), luma_tracking=True)


# --------------------------------------------------------------------------- #
#  encode + decode one rung (reuses reencode_ladder.encode_rung + its decode loop)
# --------------------------------------------------------------------------- #
def encode_decode(frames, rung, mode, work_dir):
    """Encode the PNG sequence through one rung (reencode_ladder's encoder templates) and
    decode it back to a list of (frame_index, BGR) at the dump cadence. Mirrors the decode
    loop in reencode_ladder.evaluate_rung; deletes the intermediate .mp4."""
    out_mp4 = os.path.join(work_dir, f"{rung}_{mode}.mp4")
    dup = _RL.encode_rung(frames, rung, mode, out_mp4)
    cap = cv2.VideoCapture(out_mp4)
    decoded = []
    k = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if k % dup == 0 and len(decoded) < len(frames):
                decoded.append((frames[len(decoded)][0], fr))
            k += 1
    finally:
        cap.release()
    try:
        os.unlink(out_mp4)
    except OSError:
        pass
    return decoded, dup


def _reader_rows(reader, decoded, ms_per_idx, arm):
    """Run a reader over decoded (index, BGR) frames at the dump cadence. Returns
    (rows, q_sessions): rows share reencode_ladder.run_reader's shape (i/det/stage/fill/rise/
    green); q_sessions collects last_debug q_session for the observed-q median."""
    if arm:
        reader.set_shot_state(True, 1.0)
    rows, q_sessions = [], []
    for i, frame in decoded:
        s = reader.read(frame, ts=i * ms_per_idx / 1000.0)
        rows.append({
            "i": i,
            "det": bool(s.get("detected")) and s.get("stage") != "coast",
            "stage": s.get("stage"),
            "fill": float(s.get("fill", 0.0) or 0.0),
            "rise": str(s.get("rise_state", "") or ""),
            "green": s.get("green") is not None,
        })
        dbg = getattr(reader, "last_debug", None) or {}
        q = dbg.get("q_session")
        if q is not None:
            q_sessions.append(float(q))
    return rows, q_sessions


# --------------------------------------------------------------------------- #
#  PURE metric computation (testable on fabricated row dicts, no frames needed)
# --------------------------------------------------------------------------- #
_PHASES = ("rising", "peak", "spent")


def compute_variant_metrics(pristine_rows, rung_rows) -> dict:
    """Pure: pristine baseline rows + one variant's rung rows -> metrics dict. Both are lists
    of dicts in reencode_ladder.run_reader shape (i/det/fill/rise). Recall, per-phase recall
    (bucketed by the PRISTINE reader's rise_state), and fill |delta| stats on frames both
    detect. No frames, no readers -- unit-testable."""
    pris_by_i = {r["i"]: r for r in pristine_rows}
    both = ponly = ronly = 0
    fill_d = []
    phase = {ph: [0, 0] for ph in _PHASES}          # rise_state -> [pristine_det, both_det]
    for rr in rung_rows:
        pr = pris_by_i.get(rr["i"])
        if pr is None:
            continue
        if pr["det"]:
            ph = (pr.get("rise") or "")
            if ph in phase:
                phase[ph][0] += 1
            if rr["det"]:
                both += 1
                fill_d.append(abs(float(rr["fill"]) - float(pr["fill"])))
                if ph in phase:
                    phase[ph][1] += 1
            else:
                ponly += 1
        elif rr["det"]:
            ronly += 1
    pris_det = sum(1 for r in pristine_rows if r["det"])
    return {
        "frames": len(rung_rows),
        "pristine_det": pris_det,
        "both": both, "pristine_only": ponly, "rung_only": ronly,
        "recall_vs_pristine_pct": round(_RL._pct(both, pris_det), 1),
        "fill_absdelta": _RL._stats(fill_d),
        "per_phase_recall_pct": {ph: round(_RL._pct(b, p), 1) for ph, (p, b) in phase.items()},
        "per_phase_counts": {ph: {"pristine": p, "both": b} for ph, (p, b) in phase.items()},
    }


def _p90(metrics) -> "float | None":
    return (metrics.get("fill_absdelta") or {}).get("p90")


def compute_deltas(adaptive: dict, frozen: dict) -> dict:
    """Pure: (adaptive - frozen) deltas for recall, fill-p90, and per-phase recall. Missing
    p90 (a variant detected nothing jointly) -> None rather than a fabricated number."""
    a_p90, f_p90 = _p90(adaptive), _p90(frozen)
    fill_p90_delta = (round(a_p90 - f_p90, 2)
                      if a_p90 is not None and f_p90 is not None else None)
    recall_delta = round(adaptive["recall_vs_pristine_pct"]
                         - frozen["recall_vs_pristine_pct"], 1)
    a_ph = adaptive.get("per_phase_recall_pct", {})
    f_ph = frozen.get("per_phase_recall_pct", {})
    phase_delta = {ph: round(a_ph[ph] - f_ph[ph], 1)
                   for ph in _PHASES if ph in a_ph and ph in f_ph}
    return {"recall_delta_pct": recall_delta,
            "fill_p90_delta": fill_p90_delta,
            "per_phase_recall_delta_pct": phase_delta}


# --------------------------------------------------------------------------- #
#  report formatting (pure: metric dicts -> text lines)
# --------------------------------------------------------------------------- #
def _num(v, prec=2, width=8):
    return ("n/a" if v is None else f"{v:.{prec}f}").rjust(width)


def _signed(v, prec=1, width=8):
    if v is None:
        return "n/a".rjust(width)
    return (f"{v:+.{prec}f}").rjust(width)


def format_rung_block(rung: str, adaptive: dict, frozen: dict, deltas: dict,
                      q_obs_median, q_expected) -> list:
    """Pure: one rung's metric dicts -> report text lines with adaptive/frozen adjacent and an
    (adaptive - frozen) delta column for recall and fill-p90."""
    prof = RUNGS.get(rung, {})
    profstr = (f"{prof.get('w')}x{prof.get('h')}@{prof.get('fps')} {prof.get('kbps')}kbps"
               if prof else "?")
    q_obs_s = "n/a" if q_obs_median is None else f"{q_obs_median:.3f}"
    q_exp_s = "n/a" if q_expected is None else f"{q_expected:.3f}"
    a_ap90, f_ap90 = _p90(adaptive), _p90(frozen)
    lines = [
        f"=== {rung}  ({profstr}) ===",
        f"  adaptive q_session observed median = {q_obs_s}   (static rung prior ~ {q_exp_s})",
        f"  frames judged: {adaptive.get('frames')}   pristine detections: {adaptive.get('pristine_det')}",
        "  metric                  adaptive    frozen   d(adp-frz)",
        f"  recall vs pristine %   {_num(adaptive['recall_vs_pristine_pct'],1)} "
        f"{_num(frozen['recall_vs_pristine_pct'],1)} {_signed(deltas['recall_delta_pct'],1)}",
        f"  fill |d| p90 (pp)      {_num(a_ap90,2)} {_num(f_ap90,2)} "
        f"{_signed(deltas['fill_p90_delta'],2)}",
        "  detection both / pristine-only / rung-only:",
        f"      adaptive  both={adaptive['both']} pristine-only={adaptive['pristine_only']} "
        f"rung-only={adaptive['rung_only']}",
        f"      frozen    both={frozen['both']} pristine-only={frozen['pristine_only']} "
        f"rung-only={frozen['rung_only']}",
        "  per-phase recall % (bucketed by pristine rise_state):",
    ]
    a_ph = adaptive.get("per_phase_recall_pct", {})
    f_ph = frozen.get("per_phase_recall_pct", {})
    d_ph = deltas.get("per_phase_recall_delta_pct", {})
    cnts = adaptive.get("per_phase_counts", {})
    for ph in _PHASES:
        n = (cnts.get(ph, {}) or {}).get("pristine", 0)
        lines.append(
            f"      {ph:<7} adp={_num(a_ph.get(ph), 1, 6)} frz={_num(f_ph.get(ph), 1, 6)} "
            f"d={_signed(d_ph.get(ph), 1, 6)}   (pristine n={n})")
    lines.append(f"  fill |d| pp   adaptive: {adaptive['fill_absdelta']}")
    lines.append(f"                frozen:   {frozen['fill_absdelta']}")
    lines.append("")
    return lines


def _median(vals):
    return None if not vals else round(float(np.median(np.asarray(vals, dtype=np.float64))), 3)


# --------------------------------------------------------------------------- #
#  driver
# --------------------------------------------------------------------------- #
def evaluate(session, rung_names, mode, max_frames, arm, out_dir):
    session_dir = os.path.join(_REPO, "logs", "diagnostics", "framedump", session)
    frames = _RL.list_frames(session_dir, max_frames)
    if not frames:
        raise FileNotFoundError(f"no frames in {session_dir}")
    os.makedirs(out_dir, exist_ok=True)

    print(f"[pristine] SimpleMeterReader over {len(frames)} original PNGs ...")
    pristine = _RL.run_reader(((i, cv2.imread(p)) for i, p in frames), MS_PER_IDX, armed=arm)

    rung_reports = []
    for rung in rung_names:
        if rung not in RUNGS:
            raise ValueError(f"unknown rung {rung!r}; known: {list(RUNGS)}")
        print(f"[{rung}/{mode}] encoding + decoding ...")
        decoded, dup = encode_decode(frames, rung, mode, out_dir)
        n = min(len(decoded), len(frames))
        decoded = decoded[:n]
        print(f"[{rung}/{mode}] adaptive reader over {n} frames (dup x{dup}) ...")
        a_rows, a_q = _reader_rows(build_adaptive_reader(), decoded, MS_PER_IDX, arm)
        print(f"[{rung}/{mode}] frozen reader over {n} frames ...")
        f_rows, f_q = _reader_rows(build_frozen_reader(), decoded, MS_PER_IDX, arm)

        adaptive = compute_variant_metrics(pristine, a_rows)
        frozen = compute_variant_metrics(pristine, f_rows)
        deltas = compute_deltas(adaptive, frozen)
        prof = RUNGS[rung]
        q_expected = round(float(rung_prior(prof["w"], prof["h"], prof["fps"], prof["kbps"])), 3)
        q_obs = _median(a_q)
        print(f"  recall adp={adaptive['recall_vs_pristine_pct']}% frz="
              f"{frozen['recall_vs_pristine_pct']}% (d={deltas['recall_delta_pct']})  "
              f"q_obs={q_obs} (prior~{q_expected})")
        rung_reports.append({
            "rung": rung, "mode": mode, "dup": dup, "frames": n,
            "rung_profile": prof,
            "q_session_observed_median": q_obs,
            "q_session_expected_prior": q_expected,
            "adaptive": adaptive, "frozen": frozen, "deltas": deltas,
        })

    report_obj = {
        "session": session, "mode": mode, "ms_per_idx": MS_PER_IDX, "arm": arm,
        "frames": len(frames), "caveats": CAVEATS, "rungs": rung_reports,
    }
    lines = [
        f"rung_replay_eval (M4 A/B) -- {session} ({len(frames)} frames, {MS_PER_IDX} ms/idx, "
        f"mode={mode}, arm={arm})",
        "ADAPTIVE = CompressedMeterReader() [auto quality]   vs   "
        "FROZEN = CompressedMeterReader(params=ReaderParams(), luma_tracking=True) [q=1 pinned]",
        "both judged vs PRISTINE = SimpleMeterReader on the original PNGs (shared).",
        "CAVEATS:",
    ]
    lines += [f"  * {c}" for c in CAVEATS]
    lines.append("")
    for rr in rung_reports:
        lines += format_rung_block(rr["rung"], rr["adaptive"], rr["frozen"], rr["deltas"],
                                   rr["q_session_observed_median"],
                                   rr["q_session_expected_prior"])
    report_txt = "\n".join(lines)

    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report_obj, f, indent=2)
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report_txt)
    return report_obj, report_txt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", default="session_20260704_210801")
    ap.add_argument("--rungs", default=",".join(RUNGS))
    ap.add_argument("--mode", default="dup", choices=["dup", "seq"],
                    help="dup (realistic per-frame bit budget, understates temporal) or seq")
    ap.add_argument("--max-frames", type=int, default=600,
                    help="runtime guard: default 600 keeps a full run under ~10 min")
    ap.add_argument("--arm", dest="arm", action="store_true", default=True,
                    help="set_shot_state(True) like the reencode probe (default on)")
    ap.add_argument("--no-arm", dest="arm", action="store_false")
    args = ap.parse_args(argv)

    rung_names = [r.strip() for r in args.rungs.split(",") if r.strip()]
    out_dir = os.path.join(_REPO, "logs", "diagnostics", "rung_replay_eval", args.session)
    try:
        _, report_txt = evaluate(args.session, rung_names, args.mode,
                                 args.max_frames, args.arm, out_dir)
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        return 2
    print("\n" + report_txt)
    print(f"wrote {out_dir}\\report.json / report.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
