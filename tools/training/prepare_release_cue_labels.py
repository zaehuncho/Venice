#!/usr/bin/env python3
"""Weak-supervision label prep for the five No-Meter release cues.

Builds frame-level ground truth for: SET POINT, JUMP, FLICK, PUSH, RELEASE.

Supervision source (the prepare_e2e_data.py pattern, that script left untouched):
meter-confirmed shots. The meter is ON in the training recordings, so its fill
trajectory is the oracle -- RELEASE is fixed at the meter-fill edge (the frame
where fill peaks/stops), then the other four cues are auto-labeled by walking
BACKWARDS temporally through the pose trajectories:

  release   = meter-fill edge (max fill in the shot span, fill >= --min-tip-fill)
  flick     = wrist-velocity peak in the ascent (fastest upward wrist motion)
  push      = ascent onset (last upward-velocity threshold crossing before the flick)
  set point = wrist-y minimum before the ascent (image coords: min y = highest raise)
  jump      = feet-y minimum (mean ankle-y minimum = feet at their highest)

Cues are ANIMATION-LEVEL invariants -- the same recipe applies to every shot type,
so there is deliberately no per-shot-type branching here (per owner correction).

Pose comes from YOLO pose directly with a meter-nearest player lock (the
pose_phase_analyzer.py pattern) because the cue set needs ANKLES, which the
PoseTimingDetector ring buffer does not carry. COCO: 9/10=wrists 11/12=hips
15/16=ankles; Y=0 is top, so "minimum y" = highest point on screen.

Output: JSON (--out) with one record per accepted shot: cue frame indices (`frame` =
sampled-trajectory index, `video_frame` = raw video frame = frame * step), times,
ms-before-release offsets, per-cue confidence, ordering check, per-shot `handedness`,
`release_frame`/`release_video_frame`, the locked `player_bbox_at_release` and
`meter_bbox` (both [x, y, w, h] raw image-space pixels), plus a corpus summary
(median/IQR per cue) printed at the end. A jump apex that lands after release is
CLAMPED to the release frame (flagged `clamped_from_post_release`) -- the cue count
stays honest and the label stays inside valid training windows.

Usage:
  python tools/training/prepare_release_cue_labels.py "<video1>" ["<video2>" ...] \
      [--videos-list kept_videos.txt] [--out logs/diagnostics/release_cue_labels.json] \
      [--model models/orion_pose2k_n_v3.pt] [--meter-color Purple] [--handedness Right] \
      [--frames 0] [--min-tip-fill 70] [--source owner]

--videos-list lines are "path" or "path<TAB>handedness": the optional second column is a
per-clip handedness override (the prepare_e2e_data.py CLIPS-tuple pattern).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KP_WRIST_L, KP_WRIST_R = 9, 10
KP_HIP_L, KP_HIP_R = 11, 12
KP_ANKLE_L, KP_ANKLE_R = 15, 16
KP_CONF_FLOOR = 0.30

DEBOUNCE_S = 0.30          # min gap between accepted meter-appear edges
SHOT_SPAN_MAX_S = 2.5      # release searched within this long after the meter appears
WIN_BEFORE_S = 1.3         # cue search window before release
JUMP_LO_S, JUMP_HI_S = -0.40, 0.15   # jump apex window around release
FLICK_LO_S = -0.30         # flick searched in [release-300ms, release]
PUSH_VEL_THRESH = -0.05    # smoothed upward wrist velocity (norm-units/s) that defines "ascent"
MIN_WINDOW_COVERAGE = 0.5  # fraction of non-NaN wrist samples required in the cue window

CUE_NAMES = ("set_point", "jump", "flick", "push", "release")

_NO_BOX = (float("nan"),) * 4


def _norm_handed(h):
    """Normalize any handedness spelling to the canonical lowercase 'left' / 'right'."""
    return "left" if str(h).strip().lower().startswith("l") else "right"


def _safe_y(kp, idx, H):
    if kp is None or idx >= len(kp):
        return float("nan")
    x, y, c = kp[idx]
    return float(y) / H if c >= KP_CONF_FLOOR else float("nan")


def _mean_y(kp, idxs, H):
    vals = [_safe_y(kp, j, H) for j in idxs]
    vals = [v for v in vals if not np.isnan(v)]
    return sum(vals) / len(vals) if vals else float("nan")


def _fill_nans(arr):
    out = np.copy(arr)
    mask = ~np.isnan(out)
    if mask.sum() < 2:
        return out
    out = np.interp(np.arange(len(out)), np.where(mask)[0], out[mask])
    return out


def _ema_tau(arr, tau_ms, dt_ms):
    a = 1.0 - np.exp(-dt_ms / max(tau_ms, 1.0))
    out = np.copy(arr)
    for i in range(1, len(out)):
        out[i] = out[i - 1] * (1 - a) + out[i] * a
    for i in range(len(out) - 2, -1, -1):
        out[i] = out[i + 1] * (1 - a) + out[i] * a
    return out


def extract_trajectories(video_path, pose, mdet, handedness, frames_cap, step, pose_batch=16,
                         min_tip_fill=0.0):
    """Per-sampled-frame arrays: t, wrist_y, hip_y, ankle_y, meter fill, meter detected,
    plus the locked-player and meter bboxes ([x, y, w, h] raw pixels; NaN when absent).

    Returns (arrs, anchors, n_collapsed) -- release anchors are found internally now.

    TWO-PASS (2026-08-09 speedup): pass A decodes once running ONLY the meter detector
    (strictly per-frame in temporal order -- detector state unchanged); the release anchors
    are computed from it; pass B decodes again and runs BATCHED pose only on the sampled
    frames inside the cue windows of anchors that pass min_tip_fill ([release-1.45s,
    release+0.25s], a margin around WIN_BEFORE_S and JUMP_HI_S). Everything downstream
    (label_cues_for_shot, coverage) reads only window slices, so labels are identical to
    the old full-frame pose pass while skipping ~90% of pose work (junk anchors -- median
    25% fill -- skip pose entirely). Pose frames outside every window stay NaN.
    """
    import cv2

    shooting = KP_WRIST_L if str(handedness).strip().lower().startswith("l") else KP_WRIST_R
    other = KP_WRIST_R if shooting == KP_WRIST_L else KP_WRIST_L

    # ---- pass A: meter only ------------------------------------------------------------------
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    if step <= 0:
        step = 2 if fps > 80 else 1
    rows = []
    mboxes = []  # meter bbox per sampled frame, top-left [x, y, w, h]
    i = proc = 0
    while frames_cap <= 0 or proc < frames_cap:
        if not cap.grab():
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            proc += 1
            mres = mdet.detect(fr)
            mdet_b = bool(mres and getattr(mres, "detected", False))
            mfill = float(getattr(mres, "fill_pct", 0) or 0) if mdet_b else float("nan")
            mbb = getattr(mres, "bbox", None) if mdet_b else None
            rows.append([i / fps, float("nan"), float("nan"), float("nan"), mfill, int(mdet_b)])
            mboxes.append(tuple(float(v) for v in mbb[:4])
                          if mbb is not None and len(mbb) >= 4 else _NO_BOX)
        i += 1
    cap.release()
    if not rows:
        return None, [], 0

    n = len(rows)
    pboxes = [_NO_BOX] * n
    t_arr = np.array([r[0] for r in rows])
    mf = np.array([r[4] for r in rows])
    md = np.array([r[5] for r in rows])
    anchors, n_collapsed = find_release_anchors(t_arr, mf, md)

    # sampled indices needing pose: cue windows of anchors that survive the tip-fill gate
    needed = set()
    for rel_idx, fill in anchors:
        if fill < min_tip_fill:
            continue
        lo = rel_idx
        while lo > 0 and t_arr[rel_idx] - t_arr[lo] < WIN_BEFORE_S + 0.15:
            lo -= 1
        hi = rel_idx
        while hi < n - 1 and t_arr[hi] - t_arr[rel_idx] < JUMP_HI_S + 0.10:
            hi += 1
        needed.update(range(lo, hi + 1))

    # ---- pass B: batched pose on needed sampled frames only ----------------------------------
    def _flush(chunk):
        """chunk: [(sample_idx, frame, mbb)]; batch the pose, meter-nearest player select."""
        if not chunk:
            return
        results = pose.predict([c[1] for c in chunk], verbose=False, conf=0.10)
        for (idx, fr, mbb), r in zip(chunk, results):
            H = fr.shape[0]
            wy = hy = ay = float("nan")
            pbb = _NO_BOX
            if r.boxes is not None and len(r.boxes):
                xywh = r.boxes.xywh.cpu().numpy()
                if mbb and len(mbb) >= 4 and mbb[2] > 0:
                    mcx, mcy = mbb[0] + mbb[2] / 2, mbb[1] + mbb[3] / 2
                    b = int(np.argmin([(xywh[p, 0] - mcx) ** 2 + (xywh[p, 1] - mcy) ** 2
                                       for p in range(len(xywh))]))
                else:
                    b = int(np.argmax(r.boxes.conf.cpu().numpy()))
                kp = r.keypoints.data.cpu().numpy()[b]
                wy = _safe_y(kp, shooting, H)
                if np.isnan(wy):
                    wy = _safe_y(kp, other, H)
                hy = _mean_y(kp, (KP_HIP_L, KP_HIP_R), H)
                ay = _mean_y(kp, (KP_ANKLE_L, KP_ANKLE_R), H)
                # YOLO xywh is center-based -> store top-left pixel convention
                pbb = (float(xywh[b, 0] - xywh[b, 2] / 2), float(xywh[b, 1] - xywh[b, 3] / 2),
                       float(xywh[b, 2]), float(xywh[b, 3]))
            rows[idx][1:4] = [wy, hy, ay]
            pboxes[idx] = pbb

    if needed:
        last_needed_raw = max(needed) * step
        cap = cv2.VideoCapture(video_path)
        chunk = []
        i = 0
        while i <= last_needed_raw:
            if not cap.grab():
                break
            if i % step == 0 and (i // step) in needed:
                ok, fr = cap.retrieve()
                if not ok:
                    break
                si = i // step
                mbb = mboxes[si] if not np.any(np.isnan(mboxes[si])) else None
                chunk.append((si, fr, mbb))
                if len(chunk) >= max(pose_batch, 1):
                    _flush(chunk)
                    chunk = []
            i += 1
        _flush(chunk)
        cap.release()

    arr = {k: np.array([r[j] for r in rows]) for j, k in
           enumerate(("t", "wy", "hy", "ay", "mf", "md"))}
    arr["pbb"] = np.array(pboxes)
    arr["mbox"] = np.array(mboxes)
    arr["fps"] = fps
    arr["step"] = step
    return arr, anchors, n_collapsed


def find_release_anchors(t, mf, md):
    """Meter-appear edges (debounced) -> [(release_idx, tip_fill)] at the meter-fill edge.

    Returns (anchors, n_collapsed). Meter flicker inside ONE shot creates several appear
    edges whose spans resolve to the same fill peak -- the 2026-08-09 audit found 19/78 and
    66/251 duplicated (video, release_frame) labels. Anchors whose release times are within
    0.2s are collapsed to the highest-fill one; the census reports the collapsed count.
    """
    anchors = []
    prev = False
    last_t = -1e9
    n = len(t)
    for j in range(n):
        cur = bool(md[j])
        if cur and not prev and (t[j] - last_t) > DEBOUNCE_S:
            hi = j
            while hi < n and t[hi] - t[j] <= SHOT_SPAN_MAX_S:
                hi += 1
            span = mf[j:hi]
            if span.size and not np.all(np.isnan(span)):
                rel = j + int(np.nanargmax(span))
                anchors.append((rel, float(mf[rel])))
                last_t = t[j]
        prev = cur
    # collapse same-shot duplicates: releases within 0.2s -> keep the highest-fill anchor
    merged = []
    for rel, fill in sorted(anchors):
        if merged and abs(t[rel] - t[merged[-1][0]]) <= 0.2:
            if fill > merged[-1][1]:
                merged[-1] = (rel, fill)
        else:
            merged.append((rel, fill))
    return merged, len(anchors) - len(merged)


def label_cues_for_shot(arrs, rel_idx, dt_ms, min_coverage=MIN_WINDOW_COVERAGE):
    """Walk backwards from the release anchor and label the other four cues.

    Returns (result, reject_reason, detail): result is (cues dict, coverage, ordering_ok)
    on success (reason/detail None); on rejection result is None and reason is one of
    "short_window" | "low_coverage" | "short_flick_window" with a numeric detail
    (window frames / coverage fraction / flick-window frames) for the census.
    """
    t = arrs["t"]
    n = len(t)
    lo = rel_idx
    while lo > 0 and t[rel_idx] - t[lo] < WIN_BEFORE_S:
        lo -= 1
    if rel_idx - lo < 12:
        return None, "short_window", rel_idx - lo

    wy_raw = arrs["wy"][lo:rel_idx + 1]
    coverage = float(np.mean(~np.isnan(wy_raw)))
    if coverage < min_coverage:
        return None, "low_coverage", coverage

    wy = _ema_tau(_fill_nans(np.copy(wy_raw)), 50.0, dt_ms)
    vel = np.gradient(wy, t[lo:rel_idx + 1])          # + = downward, - = upward
    m = rel_idx - lo                                   # local index of release

    # FLICK: fastest upward wrist motion in [release+FLICK_LO_S, release].
    f_lo = m
    while f_lo > 0 and t[rel_idx] - t[lo + f_lo] < -FLICK_LO_S:
        f_lo -= 1
    if m - f_lo < 2:
        return None, "short_flick_window", m - f_lo
    flick = f_lo + int(np.argmin(vel[f_lo:m + 1]))
    flick_vel = float(vel[flick])

    # PUSH: ascent onset -- walk back from the flick to the last frame before the
    # upward velocity first crossed PUSH_VEL_THRESH (sustained rise begins here).
    push = flick
    while push > 0 and vel[push - 1] < PUSH_VEL_THRESH:
        push -= 1

    # SET POINT: wrist-y minimum before the ascent (min y = highest raise pre-push).
    set_pt = int(np.argmin(wy[: push + 1])) if push >= 1 else 0

    # JUMP: mean-ankle-y minimum (feet highest) in [release+JUMP_LO_S, release+JUMP_HI_S].
    ay = arrs["ay"]
    j_lo = rel_idx
    while j_lo > 0 and t[rel_idx] - t[j_lo] < -JUMP_LO_S:
        j_lo -= 1
    j_hi = rel_idx
    while j_hi < n - 1 and t[j_hi] - t[rel_idx] < JUMP_HI_S:
        j_hi += 1
    jump = None
    ay_win = ay[j_lo:j_hi + 1]
    if ay_win.size and np.mean(~np.isnan(ay_win)) >= 0.4:
        jump = j_lo + int(np.nanargmin(ay_win))

    # Confidence: window coverage x cue-strength heuristics (0-1, weak labels stay honest).
    flick_conf = min(1.0, abs(flick_vel) / 0.15) * coverage
    push_conf = flick_conf * (0.9 if push < flick else 0.4)
    set_conf = coverage * (1.0 if set_pt < push else 0.3)
    jump_conf = 0.0 if jump is None else float(np.mean(~np.isnan(ay_win))) * 0.9
    ordering_ok = bool(set_pt <= push <= flick <= m)

    # A post-release jump apex can never sit inside a training window (windows end at
    # release); CLAMP it to the release frame rather than dropping it, so the labeled cue
    # count stays honest without inflating coverage with untrainable cues.
    jump_clamped = False
    if jump is not None and jump > rel_idx:
        jump = rel_idx
        jump_clamped = True

    step = int(arrs.get("step", 1)) or 1

    def _cue(local_idx, conf, absolute=False):
        gi = local_idx if absolute else lo + local_idx
        return {"frame": int(gi), "video_frame": int(gi) * step, "t": float(t[gi]),
                "ms_before_release": float((t[rel_idx] - t[gi]) * 1000.0),
                "confidence": round(float(conf), 3)}

    cues = {
        "release": _cue(rel_idx, min(1.0, coverage + 0.3), absolute=True),
        "flick": _cue(flick, flick_conf),
        "push": _cue(push, push_conf),
        "set_point": _cue(set_pt, set_conf),
    }
    if jump is not None:
        cues["jump"] = _cue(jump, jump_conf, absolute=True)
        if jump_clamped:
            cues["jump"]["clamped_from_post_release"] = True
    return (cues, coverage, ordering_ok), None, None


def summarize(shots):
    full = [s for s in shots if s.get("tier", "AB") == "AB"]
    n_a = len(shots) - len(full)
    if n_a:
        print(f"\ntiers: {len(full)} full-cue (AB) + {n_a} timing-only (A)")
    print(f"\n{'cue':<12} {'n':>5} {'median ms-before-rel':>20} {'IQR':>8}")
    print("-" * 50)
    for name in CUE_NAMES:
        offs = [s["cues"][name]["ms_before_release"] for s in full if name in s["cues"]]
        if not offs:
            print(f"{name:<12} {0:>5}")
            continue
        a = np.array(sorted(offs))
        print(f"{name:<12} {len(a):>5} {np.median(a):>19.0f} "
              f"{np.percentile(a, 75) - np.percentile(a, 25):>7.0f}")
    ok = sum(1 for s in full if s["ordering_ok"])
    print(f"ordering set<=push<=flick<=release: {ok}/{len(full)} (full-cue tier)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="*", help="video paths (or use --videos-list)")
    ap.add_argument("--videos-list", default="", help="text file, one video path per line")
    ap.add_argument("--out", default=os.path.join("logs", "diagnostics", "release_cue_labels.json"))
    # v3 default per the 2026-08-09 A/B: conversion yield is IDENTICAL to v2 (78/78 shots on
    # the same 24 clips) but v3's wrist tracking is measurably better (4.8 vs 7.0px jitter,
    # catches raised follow-through arms), and pose-derived cue placements differ between the
    # models by 58-167ms median -- so use the better tracker for the trajectory-derived cues.
    ap.add_argument("--model", default=os.path.join("models", "orion_pose2k_n_v3.pt"))
    ap.add_argument("--meter-color", default="Purple")
    ap.add_argument("--handedness", default="Right")
    ap.add_argument("--frames", type=int, default=0, help="cap sampled frames per video (0 = all)")
    ap.add_argument("--step", type=int, default=0, help="frame step (0 = auto: 2 if >80fps)")
    ap.add_argument("--pose-batch", type=int, default=16,
                    help="frames per batched pose call (1 = old frame-serial behavior)")
    ap.add_argument("--min-tip-fill", type=float, default=70.0,
                    help="reject shots whose meter never fills this far (weak anchor)")
    ap.add_argument("--min-coverage", type=float, default=MIN_WINDOW_COVERAGE,
                    help="min fraction of non-NaN wrist samples in the cue window")
    ap.add_argument("--source", default="owner", choices=["owner", "youtube"],
                    help="provenance tag; cue labels should stay owner-only (see docs)")
    args = ap.parse_args()

    # (path, per-clip handedness override or None) -- the prepare_e2e_data.py CLIPS pattern:
    # a --videos-list line may carry "path<TAB>handedness"; TAB is illegal in Windows paths.
    videos = [(v, None) for v in args.videos]
    if args.videos_list:
        with open(args.videos_list, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                if "\t" in ln:
                    p, h = ln.split("\t", 1)
                    videos.append((p.strip(), h.strip() or None))
                else:
                    videos.append((ln, None))
    if not videos:
        print("no videos given (positional paths or --videos-list)")
        return 2

    os.environ.setdefault("YOLO_VERBOSE", "False")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        import cv2  # noqa: F401
        from ultralytics import YOLO
        from meter_detector import MeterDetector, DetectorConfig
    except Exception as exc:
        print(f"need opencv + ultralytics + meter_detector: {exc}")
        print("run tools/training/no_meter_mode_prep.py first -- its CUDA preflight prints the fix")
        return 2

    mp = args.model if os.path.isfile(args.model) else "yolov8x-pose.pt"
    print(f"pose model: {mp}   meter color: {args.meter_color}   handedness: {args.handedness}")
    pose = YOLO(mp)
    cfg = DetectorConfig()
    cfg.meter_color = args.meter_color
    mdet = MeterDetector(os.path.join(ROOT, "meter_styles"), cfg)

    all_shots = []
    # Incremental progress + resume (2026-08-09): one JSON line per finished clip next to
    # --out. A killed run loses at most the clip in flight; a rerun with the same --out
    # skips clips already in the progress file and seeds its state from them.
    prog_path = (args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)) \
        + ".progress.jsonl"
    done_videos = set()
    # Rejection census: every meter-detected shot that does NOT become a cue label is
    # counted by reason (previously they vanished silently -- the 23% conversion mystery).
    # Two-tier labels (2026-08-09): the TIMING head only needs the meter-anchored release,
    # the CUE head needs the full wrist-derived cue set. A shot whose cue derivation fails
    # (low coverage / short window) but whose meter anchor is clean is emitted as tier "A"
    # (release cue only) instead of being dropped; full-cue shots are tier "AB". The trainer
    # masks the cue-CE loss to tier-AB windows -- tier-A rows must never fabricate cue
    # targets. The tip-fill gate stays for BOTH tiers: a low-fill anchor (median 25% in the
    # census) is a pump-fake/tap/flicker artifact, not a trustworthy release.
    census = {
        "video_not_found": 0, "video_no_frames": 0, "duplicate_anchor": 0,
        "low_tip_fill": 0, "short_window": 0, "low_coverage": 0, "short_flick_window": 0,
        "accepted_timing_only": 0, "accepted_full": 0,
    }
    rej_tip_fills = []    # tip_fill of shots rejected by --min-tip-fill
    rej_coverages = []    # coverage of shots rejected by --min-coverage
    per_video = []        # (video, n_anchors, n_accepted) conversion table
    if os.path.isfile(prog_path):
        with open(prog_path, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue  # torn tail line from a kill mid-write
                done_videos.add(rec["video"])
                all_shots.extend(rec["shots"])
                for ck, cv in rec["census"].items():
                    census[ck] = census.get(ck, 0) + cv
                rej_tip_fills.extend(rec.get("rej_tip_fills", []))
                rej_coverages.extend(rec.get("rej_coverages", []))
                if rec.get("per_video"):
                    per_video.append(rec["per_video"])
        if done_videos:
            print(f"resume: {len(done_videos)} clips already in {os.path.basename(prog_path)}, "
                  f"{len(all_shots)} shots seeded")
    for vp, handed_over in videos:
        if os.path.basename(vp) in done_videos:
            continue
        if not os.path.isfile(vp):
            print(f"skip (not found): {vp}")
            census["video_not_found"] += 1
            continue
        handed = handed_over or args.handedness
        hnorm = _norm_handed(handed)
        vname = os.path.basename(vp)
        tag = f" [{hnorm} override]" if handed_over else ""
        print(f"[{vname[:40]}]{tag} extracting ...", end=" ", flush=True)
        c0 = dict(census)
        s0, rt0, rc0 = len(all_shots), len(rej_tip_fills), len(rej_coverages)

        def _progress_write(pv, _v=vname, _c0=c0, _s0=s0, _rt0=rt0, _rc0=rc0):
            rec = {"video": _v, "shots": all_shots[_s0:],
                   "census": {k: census[k] - _c0.get(k, 0) for k in census
                              if census[k] != _c0.get(k, 0)},
                   "rej_tip_fills": rej_tip_fills[_rt0:],
                   "rej_coverages": rej_coverages[_rc0:],
                   "per_video": pv}
            with open(prog_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")

        arrs, anchors, n_collapsed = extract_trajectories(
            vp, pose, mdet, handed, args.frames, args.step,
            pose_batch=args.pose_batch, min_tip_fill=args.min_tip_fill)
        if arrs is None:
            print("no frames")
            census["video_no_frames"] += 1
            _progress_write(None)
            continue
        step = int(arrs["step"])
        dt_ms = 1000.0 * step / max(arrs["fps"], 1.0)
        census["duplicate_anchor"] += n_collapsed
        n_ok = n_any = 0
        for k, (rel_idx, tip_fill) in enumerate(anchors):
            if tip_fill < args.min_tip_fill:
                census["low_tip_fill"] += 1
                rej_tip_fills.append(round(float(tip_fill), 1))
                continue
            res, reason, detail = label_cues_for_shot(arrs, rel_idx, dt_ms,
                                                      min_coverage=args.min_coverage)
            if res is None:
                census[reason] += 1
                if reason == "low_coverage":
                    rej_coverages.append(round(float(detail), 3))
                # tier A: timing supervision survives -- the meter anchor is clean even
                # though the wrist-derived cues are not derivable
                tier = "A"
                coverage = float(detail) if reason == "low_coverage" else 0.0
                ordering_ok = None
                cues = {"release": {
                    "frame": int(rel_idx), "video_frame": int(rel_idx) * step,
                    "t": float(arrs["t"][rel_idx]), "ms_before_release": 0.0,
                    "confidence": 1.0}}
                census["accepted_timing_only"] += 1
            else:
                cues, coverage, ordering_ok = res
                tier = "AB"
                census["accepted_full"] += 1
            shot = {
                "video": vname, "path": os.path.abspath(vp), "source": args.source,
                "shot_idx": k, "fps": float(arrs["fps"]), "step": step,
                "handedness": hnorm, "tier": tier,
                "release_frame": int(rel_idx),
                "release_video_frame": int(rel_idx) * step,
                "tip_fill": round(tip_fill, 1), "coverage": round(coverage, 3),
                "ordering_ok": ordering_ok, "cues": cues,
            }
            pbb = arrs["pbb"][rel_idx]
            if not np.any(np.isnan(pbb)):
                shot["player_bbox_at_release"] = [round(float(v), 1) for v in pbb]
            mbx = arrs["mbox"][rel_idx]
            if not np.any(np.isnan(mbx)):
                shot["meter_bbox"] = [round(float(v), 1) for v in mbx]
            all_shots.append(shot)
            if tier == "AB":
                n_ok += 1
            n_any += 1
        print(f"{len(anchors)} meter shots, {n_ok} cue-labeled, {n_any - n_ok} timing-only")
        pv = {"video": vname, "meter_shots": len(anchors),
              "cue_labeled": n_ok, "timing_only": n_any - n_ok}
        per_video.append(pv)
        _progress_write(pv)

    out = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cue_names": list(CUE_NAMES),
            "params": {"min_tip_fill": args.min_tip_fill, "win_before_s": WIN_BEFORE_S,
                       "push_vel_thresh": PUSH_VEL_THRESH, "flick_lo_s": FLICK_LO_S,
                       "jump_window_s": [JUMP_LO_S, JUMP_HI_S],
                       "min_window_coverage": args.min_coverage,
                       "pose_model": os.path.basename(mp),
                       # invocation default; per-shot "handedness" is authoritative
                       # (per-clip --videos-list overrides can differ from this)
                       "handedness": _norm_handed(args.handedness)},
            "n_shots": len(all_shots),
            "rejection_census": census,
            "rejected_tip_fills": sorted(rej_tip_fills),
            "rejected_coverages": sorted(rej_coverages),
            "per_video": per_video,
            "shots": all_shots,
        }, fh, indent=2)
    print(f"\nsaved {len(all_shots)} cue-labeled shots -> {out}")
    total_anchors = sum(v["meter_shots"] for v in per_video)
    print(f"rejection census (of {total_anchors} meter-detected shots):")
    for k, v in census.items():
        print(f"  {k:<20} {v}")
    if rej_tip_fills:
        a = np.array(rej_tip_fills)
        print(f"  rejected tip_fill: median {np.median(a):.0f}  p25 {np.percentile(a, 25):.0f}  "
              f"p75 {np.percentile(a, 75):.0f}  (gate {args.min_tip_fill})")
    if rej_coverages:
        a = np.array(rej_coverages)
        print(f"  rejected coverage: median {np.median(a):.2f}  p25 {np.percentile(a, 25):.2f}  "
              f"p75 {np.percentile(a, 75):.2f}  (gate {args.min_coverage})")
    if all_shots:
        summarize(all_shots)
    return 0 if all_shots else 1


if __name__ == "__main__":
    raise SystemExit(main())
