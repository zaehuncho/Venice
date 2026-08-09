"""Per-shot RELEASE-ACTION TIMELINE — diagnostic-only (no behavior changes).

Answers: for each released shot, is the bot's release command landing EARLY, LATE, or
JITTERING; is the on-screen meter's recede-to-~52% post-shot feedback or a missed/late
release; and is the green_center<->meter_full target swing shifting the aim point.

It does NOT modify the engine/detector. It reuses correlate_releases.py for log parsing
and detector replay, adds a `Shot state` parser, and per release seq emits a timeline:

  T_arm (Idle->Holding) -> T_issued (Release issued) -> T_submit (Release submit/ViGEm)
  F_release (detector fill the engine acted on)  vs  F_peak (real on-screen meter peak)
  fill-based inferred round-trip = (F_peak - F_release) / vel    [clock-offset-free]
  recede: post-peak settle value + whether the ROI persists or vanishes
  per-shot frame strip (release / peak / settle) for visual outcome labeling

Usage:
  python tools/diagnostics/release_action_timeline.py \
      --video "C:/Users/Administrator/Videos/2026-06-05 03-35-02.mp4" \
      --video-start-utc 2026-06-05T08:35:02Z
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import correlate_releases as cr  # noqa: E402  (reuse parse_log/replay/crop/parse_utc)

_SHOTSTATE = re.compile(
    cr._TS + r"\s+Shot state: (?P<frm>\w+) -> (?P<to>\w+) mode=(?P<mode>\w+) seq=(?P<seq>\d+)")
# Strict submit parse (cr._SUBMIT's optional seq is unreliable — its leading .*? eats "seq=").
_SUBMIT_SEQ = re.compile(cr._TS + r"\s+Release submit: seq=(?P<seq>\d+) ok=(?P<ok>[01])")


def parse_submits(log_path: Path):
    """Return a time-ordered [(utc, seq)] for 'Release submit:' lines. seq restarts each
    session, so callers must pair by seq AND nearest timestamp (not a seq-keyed dict)."""
    out = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SUBMIT_SEQ.search(line)
        if m:
            out.append((cr.parse_utc(m.group("ts")), int(m.group("seq"))))
    return out


def pair_submit_utc(rel, submits):
    """Submit utc for this release: matching seq AND nearest timestamp (within 2s) — the
    submit is logged ~ms after issued, so this disambiguates cross-session seq reuse."""
    best, bestdt = None, 2.0
    for utc, seq in submits:
        if seq != rel["seq"]:
            continue
        dt = abs((utc - rel["utc"]).total_seconds())
        if dt < bestdt:
            best, bestdt = utc, dt
    return best

DEFAULT_DET = ROOT / "logs" / "diagnostics" / "detframes.csv"
DEFAULT_FRAMES = ROOT / "logs" / "diagnostics" / "frames_033502"


def parse_shot_states(log_path: Path):
    """Return (arms, releasing_seq). arms = time-ordered [(utc, mode)] for Idle->Holding;
    releasing_seq = {seq: utc} for the Holding->Releasing transition."""
    arms, releasing_seq = [], {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SHOTSTATE.search(line)
        if not m:
            continue
        d = m.groupdict()
        utc = cr.parse_utc(d["ts"])
        if d["frm"] == "Idle" and d["to"] == "Holding":
            arms.append((utc, d["mode"]))
        elif d["to"] == "Releasing":
            releasing_seq[int(d["seq"])] = utc
    return arms, releasing_seq


def pair_arm(rel, arms):
    """The arm for a release = the last Idle->Holding strictly before its issued time."""
    best = None
    for utc, mode in arms:
        if utc <= rel["utc"]:
            best = (utc, mode)
        else:
            break
    return best


def peak_settle_vanish(traj):
    """From a [(rel_ms, fill, conf, rej)] trajectory return
    (peak_fill, peak_rel_ms, settle_fill, vanish_rel_ms)."""
    pts = [(t, f) for (t, f, _c, _r) in traj if f is not None]
    if not pts:
        return None, None, None, None
    peak_rel, peak_fill = max(pts, key=lambda p: p[1])  # (rel_ms, fill) -> max by fill
    # settle = median fill in [peak+150ms, peak+550ms] (the recede plateau)
    after = [f for (t, f) in pts if peak_rel + 150 <= t <= peak_rel + 550]
    settle = sorted(after)[len(after) // 2] if after else None
    # vanish = first roi_not_found AFTER the peak
    vanish = None
    for (t, f, _c, rej) in traj:
        if t > peak_rel and rej == "roi_not_found":
            vanish = t
            break
    return round(peak_fill, 1), round(peak_rel, 0), (round(settle, 1) if settle is not None else None), vanish


def grab_crop(cv2, cap, t_sec, crop, path):
    x, y, w, h = crop
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_sec * 1000.0))
    ok, fr = cap.read()
    if ok:
        cv2.imwrite(str(path), fr[y:y + h, x:x + w])
        return True
    return False


def investigate_false_lock(det_path: Path):
    """Quantify the between-shots false-lock: stable mid-fill samples with a green band and
    no real rising shot. Heuristic: detected, 45<=fill<=60, green present, conf>=0.7."""
    if not det_path.exists():
        return None
    try:
        csv.field_size_limit(10 ** 7)
    except Exception:
        pass
    try:
        rows = list(csv.DictReader(det_path.open(encoding="utf-8", errors="replace")))
    except Exception as e:
        return {"error": str(e)}

    def F(r, k):
        try:
            return float(r[k])
        except Exception:
            return 0.0
    det = [r for r in rows if r.get("rejection_reason") != "roi_not_found" and F(r, "fill_pct") > 0]
    suspicious = [r for r in det if 45.0 <= F(r, "fill_pct") <= 60.0
                  and F(r, "green_confidence") > 0.0 and F(r, "confidence") >= 0.7]
    # longest contiguous run of suspicious frames (by t_ms gaps < 200ms)
    longest = cur = 0
    last_t = None
    for r in suspicious:
        t = F(r, "t_ms")
        if last_t is not None and (t - last_t) < 200:
            cur += 1
        else:
            cur = 1
        longest = max(longest, cur)
        last_t = t
    return {
        "total_frames": len(rows), "detected": len(det),
        "suspicious_mid_green": len(suspicious),
        "pct_of_detected": round(100.0 * len(suspicious) / max(1, len(det)), 1),
        "longest_run_frames": longest,
        "sample": [(r["t_ms"], r["fill_pct"], r["confidence"], r["green_center_pct"],
                    r["green_confidence"]) for r in suspicious[:6]],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--video-start-utc", required=True)
    ap.add_argument("--log", default=str(cr.DEFAULT_LOG))
    ap.add_argument("--detframes", default=str(DEFAULT_DET))
    ap.add_argument("--crop", default=",".join(map(str, cr.DEFAULT_CROP)))
    ap.add_argument("--style", default="Arrow2")
    ap.add_argument("--color", default="Purple")
    ap.add_argument("--window-pre", type=float, default=1.2)
    ap.add_argument("--window-post", type=float, default=1.0)
    ap.add_argument("--frames-dir", default=str(DEFAULT_FRAMES))
    ap.add_argument("--out", default=str(ROOT / "logs" / "diagnostics" / "release_timeline.csv"))
    args = ap.parse_args()

    log_path = Path(args.log)
    releases, submits_seq, submits_list, ownership_seq, attribution_seq, attribution_list = cr.parse_log(log_path)
    arms, releasing_seq = parse_shot_states(log_path)
    submits = parse_submits(log_path)
    vstart = cr.parse_utc(args.video_start_utc)
    crop = tuple(int(v) for v in args.crop.split(","))

    import cv2
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"could not open video: {args.video}", file=sys.stderr)
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0) / fps if fps else 0.0
    frames_dir = Path(args.frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    fields = ["seq", "shot", "code", "target_mode", "green_confirmed",
              "t_issued", "arm_to_issued_ms", "issued_to_submit_ms",
              "f_release", "target", "vel_pct_ms", "crossing_eta_ms", "within_reach",
              "f_peak", "peak_rel_ms", "settle_fill", "vanish_rel_ms",
              "delta_fill", "inferred_round_trip_ms", "early_late",
              "raw_green", "attr_green_confirm_fill"]
    rows = []
    print(f"=== Per-shot release-action timeline ({len(releases)} releases parsed; "
          f"video {duration:.0f}s) ===")
    for rel in releases:
        seq = rel["seq"]
        video_t = (rel["utc"] - vstart).total_seconds()
        if not (0.0 <= video_t <= duration):
            continue  # only shots inside this recording
        arm = pair_arm(rel, arms)
        sub_utc = pair_submit_utc(rel, submits)
        attr = cr.pair_attribution(rel, attribution_list)
        arm_to_issued = round((rel["utc"] - arm[0]).total_seconds() * 1000.0, 0) if arm else None
        issued_to_submit = round((sub_utc - rel["utc"]).total_seconds() * 1000.0, 1) if sub_utc else None

        det = cr.make_detector(ROOT, args.style, args.color)
        _f, _g, _pk, _rej, traj = cr.video_fill_trajectory(
            cv2, cap, fps, det, crop, video_t, args.window_pre, args.window_post)
        f_peak, peak_rel, settle, vanish = peak_settle_vanish(traj)

        vel = attr["vel"] if attr else 0.0
        f_release = rel["fill"]
        delta_fill = round(f_peak - f_release, 1) if f_peak is not None else None
        rtt = round(delta_fill / vel, 0) if (delta_fill is not None and vel and vel > 0.001) else None
        # outcome proxy from where the meter peaked vs the green band (raw green from the release line)
        early_late = ""
        if f_peak is not None:
            if f_peak >= 99.0:
                early_late = "overshoot/late?"
            elif f_peak >= 90.0:
                early_late = "near-top"
            else:
                early_late = "below-top"

        # frame strip: release / peak / settle
        sd = frames_dir / f"seq{seq:02d}_{rel['shot'].replace(' ', '')}"
        sd.mkdir(parents=True, exist_ok=True)
        grab_crop(cv2, cap, video_t, crop, sd / "1_release.png")
        if peak_rel is not None:
            grab_crop(cv2, cap, video_t + peak_rel / 1000.0, crop, sd / "2_peak.png")
        if settle is not None:
            grab_crop(cv2, cap, video_t + (peak_rel + 350) / 1000.0, crop, sd / "3_settle.png")

        row = {
            "seq": seq, "shot": rel["shot"], "code": rel["code"],
            "target_mode": attr["target_mode"] if attr else "",
            "green_confirmed": attr["green_confirmed"] if attr else "",
            "t_issued": rel["log_ts"], "arm_to_issued_ms": arm_to_issued,
            "issued_to_submit_ms": issued_to_submit, "f_release": f_release, "target": rel["target"],
            "vel_pct_ms": round(vel, 4), "crossing_eta_ms": attr["crossing_eta"] if attr else "",
            "within_reach": attr["within_reach"] if attr else "",
            "f_peak": f_peak, "peak_rel_ms": peak_rel, "settle_fill": settle, "vanish_rel_ms": vanish,
            "delta_fill": delta_fill, "inferred_round_trip_ms": rtt, "early_late": early_late,
            "raw_green": rel["green"], "attr_green_confirm_fill": attr["green_confirm_fill"] if attr else "",
        }
        rows.append(row)
        print(f"  seq={seq:<2} {rel['shot']:<10} {rel['code']:<16} "
              f"arm->iss={str(arm_to_issued):>5}ms iss->sub={str(issued_to_submit):>4}ms "
              f"F_rel={f_release:5.1f} F_peak={str(f_peak):>5} settle={str(settle):>5} "
              f"dFill={str(delta_fill):>5} rtt~={str(rtt):>5}ms vel={vel:.3f} "
              f"tmode={(attr['target_mode'] if attr else '-'):<11} {early_late}")

    cap.release()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # Summary: issued->submit sanity, and the f_peak distribution
    iss_sub = [r["issued_to_submit_ms"] for r in rows if r["issued_to_submit_ms"] is not None]
    if iss_sub:
        print(f"\nissued->submit ms: min={min(iss_sub)} max={max(iss_sub)} "
              f"(expect ~1-2ms — confirms the virtual write happened at the logged release)")
    peaks = [r["f_peak"] for r in rows if r["f_peak"] is not None]
    if peaks:
        print(f"F_peak across shots: min={min(peaks)} max={max(peaks)} "
              f"mean={sum(peaks)/len(peaks):.1f}  (does the meter always peak ~100 then recede?)")

    # ---- Separate investigation 1: detector false-lock between shots ----
    print("\n=== Detector false-lock between shots (~52% fill / green ~94) ===")
    fl = investigate_false_lock(Path(args.detframes))
    if fl and "error" in fl:
        print(f"  (detframes read skipped: {fl['error']})")
    elif fl:
        print(f"  detframes: {fl['detected']}/{fl['total_frames']} detected; "
              f"suspicious mid-fill(45-60)+green = {fl['suspicious_mid_green']} "
              f"({fl['pct_of_detected']}% of detected); longest contiguous run = "
              f"{fl['longest_run_frames']} frames")
        for s in fl["sample"]:
            print(f"    t={s[0]}ms fill={s[1]} conf={s[2]} green_c={s[3]} green_conf={s[4]}")
    else:
        print("  (detframes.csv not found)")

    # ---- Separate investigation 2: greenConfirmed vs green=-1--1 attribution mismatch ----
    print("\n=== greenConfirmed=1 while Release issued shows green=-1--1 ===")
    mism = []
    for rel in releases:
        attr = cr.pair_attribution(rel, attribution_list)
        if attr and attr["green_confirmed"] == 1 and rel["green"].startswith("-1"):
            mism.append((rel["seq"], rel["shot"], rel["code"], rel["green"], attr["target_mode"]))
    print(f"  mismatched releases: {len(mism)}")
    for m in mism[:12]:
        print(f"    seq={m[0]} {m[1]:<10} {m[2]:<16} raw_green={m[3]} attr_targetMode={m[4]}")
    print("  (mechanism: the issued line logs the LATEST raw sample's green (can be -1);"
          " greenConfirmed/targetMode reflect greenTracker_.confirmed() from prior frames.)")

    print(f"\nwrote {out}  ({len(rows)} in-window shots)  frames -> {frames_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
