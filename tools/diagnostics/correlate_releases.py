"""Correlate engine "Release issued" events with the REAL on-screen meter in a
screen recording.

Non-gameplay diagnostic. It answers the central question from the 10:28 batch:
the engine logged `green_confirmed` standstill releases at 65-76% fill, yet the
shots landed late / no-interaction. Does the engine's logged fill match the REAL
meter at that instant (capture/detect lag), and did the release reach the pad?

Pipeline:
  1. Parse `logs/orion_native.log` "Release issued:" lines (UTC `...Z` timestamp +
     fill/target/green/code/conf/age/offset/shot). Also parse the paired
     "Release submit:" lines if present (added by the ViGEm-verification change);
     pair by `seq=` when available, else by nearest timestamp (and say so).
  2. Map each release to a video time: `video_t = log_utc - video_start_utc`
     (+ optional --clock-offset-ms for sub-second recording-start slack). On this
     machine recordings are local (CDT/UTC-5) and the log is UTC, so pass e.g.
     `--video-start-utc 2026-06-04T15:28:09Z` (= local 10:28:09).
  3. The recording is the full 1920x1080 Orion desktop; the GAME stream is a 16:9
     sub-rectangle. CROP to it (--crop x,y,w,h, default the 10:28 layout) so the
     detector sees the meter, not the whole desktop. A FRESH MeterDetector is built
     per release so the stability latch acquires cleanly on each shot (avoids the
     stale-anchor cross-shot bug).
  4. For each release, replay a lead-in window [t-pre, t+post], crop+detect each
     frame, and record the REAL fill trajectory; `video_fill_pct` is the sample
     nearest video_t.
  5. Emit logs/diagnostics/release_correlation.csv and print a per-shot-type
     log-vs-video fill comparison.

    python tools/diagnostics/correlate_releases.py \
        --video "C:/Users/Administrator/Videos/2026-06-04 10-28-09.mp4" \
        --video-start-utc 2026-06-04T15:28:09Z
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Default game-stream crop within the 1920x1080 recording (10:28 batch layout,
# verified: full-frame meter bbox minus this origin == cropped bbox; 1169:658 = 16:9).
DEFAULT_CROP = (452, 232, 1169, 658)
DEFAULT_VIDEO = r"C:\Users\Administrator\Videos\2026-06-04 10-28-09.mp4"
DEFAULT_LOG = ROOT / "logs" / "orion_native.log"
DEFAULT_OUT = ROOT / "logs" / "diagnostics" / "release_correlation.csv"

_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)"

_ISSUED = re.compile(
    _TS + r"\s+Release issued: fill (?P<fill>[\d.]+)% target (?P<target>[\d.]+)% "
    r"\(green (?P<gs>-?[\d.]+)-(?P<ge>-?[\d.]+)\).*?code=(?P<code>\w+) "
    r"presence=(?P<presence>\w+) src=(?P<src>[\w-]+) conf=(?P<conf>[\d.]+) "
    r"age=(?P<age>\d+)ms offset=(?P<offset>-?[\d.]+)ms(?: seq=\d+)? shot=(?P<shot>.+?)\s*$"
)
# Paired submit line (emitted by the local-delivery verification path). Parse the
# body as independent key/value fields so ordering is genuinely irrelevant. A
# square_bit of -1 is an intentional "exact packet unavailable" sentinel; keep
# the release and classify that field as unknown instead of dropping the line.
_SUBMIT = re.compile(_TS + r"\s+Release submit:(?P<body>.*)$")
_SUBMIT_SEQ = re.compile(r"(?:^|\s)seq=(?P<value>\d+)(?:\s|$)")
_SUBMIT_OK = re.compile(r"(?:^|\s)ok=(?P<value>[01])(?:\s|$)")
_SUBMIT_SQUARE = re.compile(r"(?:^|\s)square_bit=(?P<value>-1|[01])(?:\s|$)")
_ISSUED_SEQ = re.compile(r"seq=(?P<seq>\d+)")
# Ownership summary (emitted by OrionAppController::flushReleaseOwnershipTrace) — proves
# whether Orion's virtual release held the shot across the WHOLE Releasing+Cooldown window.
# All values are single space-free tokens. Paired to a release by seq.
_OWNERSHIP = re.compile(
    _TS + r"\s+Release ownership: seq=(?P<seq>\d+) phys_held_all=(?P<phys_held>[01]) "
    r"out_cleared_all=(?P<out_cleared>[01]) max_out_sq=(?P<max_out>[01]) "
    r"phys_release_t_ms=(?P<phys_rel>-?\d+) ticks=(?P<ticks>\d+) dur_ms=(?P<dur>\d+)"
)
# Release-path attribution (emitted alongside each "Release issued:" line, paired by seq).
# Resolves the Go-To bimodal split: greenConfirmed=1/targetMode=green_tip => the green block
# rode the tip (late ~100); greenConfirmed=0/targetMode=meter_full => the velocity block fired
# at the reachability floor (early ~70). greenConfirmFill/Ms expose the confirmation race. All
# tokens space-free; shot=<type> is last (may contain spaces).
_ATTRIBUTION = re.compile(
    _TS + r"\s+Release attribution: seq=(?P<seq>\d+) greenConfirmed=(?P<gc>[01]) "
    r"targetMode=(?P<tmode>\w+) greenWidth=(?P<gw>[\d.]+) "
    r"greenConfirmFill=(?P<gcf>-?[\d.]+) greenConfirmMs=(?P<gcm>-?\d+) "
    r"vel=(?P<vel>-?[\d.]+) crossingEta=(?P<ceta>-?\d+) "
    r"expectedRise=(?P<erise>[\d.]+) withinReach=(?P<wr>[01]) shot=(?P<shot>.+?)\s*$"
)
# Per-release freshness verdict (emitted alongside "Release issued:", paired by seq). blindFire=1
# => fired on the feedforward clock (vision could not time it); staleMs = ms since the last FRESH
# accept at the instant of release (-1 = vision never went fresh this shot); memTrusted = memory
# echoes promoted to fresh-equivalent this shot (0 unless the memory_trust_enabled A/B is ON).
# All tokens space-free; shot=<type> is last (may contain spaces).
_FRESHNESS = re.compile(
    _TS + r"\s+Release freshness: seq=(?P<seq>\d+) blindFire=(?P<blind>[01]) "
    r"code=(?P<code>\w+) lastFresh=(?P<lastfresh>[01]) fresh=(?P<fresh>\d+) "
    r"staleMs=(?P<stale>-?\d+) memTrusted=(?P<memtrust>\d+) shot=(?P<shot>.+?)\s*$"
)


def classify_ownership(o):
    """Single ownership label per release. Deliberately does NOT use the video — a
    routing leak can only be *suspected* here (clean clear while the user still held);
    confirming it needs the recording, so we label, not claim.
      engine_override_bug   - Orion failed to keep output Square cleared (a real bug).
      possible_routing_leak - output cleared the whole window AND the user kept holding
                              physical Square: if the shot didn't fire in-game the leak
                              is downstream (Chiaki reading the physical DualSense).
      user_released_physical- the user let go of physical Square during the pulse.
      clean_owned           - output cleared cleanly, nothing else flagged."""
    if not o:
        return ""
    if o["out_cleared_all"] == 0:
        return "engine_override_bug"
    if o["phys_held_all"] == 1:
        return "possible_routing_leak"
    if o["phys_release_t_ms"] >= 0:
        return "user_released_physical"
    return "clean_owned"


def parse_utc(ts: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable UTC timestamp: {ts!r}")


def parse_log(path: Path):
    """Return (releases, submits_seq, submits_list, ownership_seq, attribution_seq). releases is a
    list of dicts; submits maps seq->dict and also keeps a time-ordered list for nearest-timestamp
    fallback; ownership_seq maps seq->dict for the "Release ownership:" summary; attribution_seq maps
    seq->dict for the "Release attribution:" path-attribution line."""
    releases, submits_seq, submits_list, ownership_seq, attribution_seq = [], {}, [], {}, {}
    # seq resets to 1 every OrionNative launch, and this log accumulates across sessions, so a
    # seq-keyed dict collides across sessions (last-wins). Keep a time-ordered LIST too and pair
    # attribution to a release by seq AND nearest timestamp (they share a near-identical stamp).
    attribution_list = []
    freshness_list = []
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in text:
        m = _ISSUED.search(line)
        if m:
            d = m.groupdict()
            seqm = _ISSUED_SEQ.search(line)
            releases.append({
                "utc": parse_utc(d["ts"]),
                "log_ts": d["ts"],
                "shot": d["shot"].strip(),
                "code": d["code"],
                "fill": float(d["fill"]),
                "target": float(d["target"]),
                "green": f"{d['gs']}-{d['ge']}",
                "conf": float(d["conf"]),
                "age": int(d["age"]),
                "offset": float(d["offset"]),
                "seq": int(seqm.group("seq")) if seqm else None,
            })
            continue
        m = _SUBMIT.search(line)
        if m:
            d = m.groupdict()
            body = d["body"]
            seq_match = _SUBMIT_SEQ.search(body)
            ok_match = _SUBMIT_OK.search(body)
            square_match = _SUBMIT_SQUARE.search(body)
            if not ok_match or not square_match:
                continue
            square_raw = square_match.group("value")
            rec = {"utc": parse_utc(d["ts"]),
                   "ok": ok_match.group("value"),
                   "square": "unknown" if square_raw == "-1" else square_raw,
                   "seq": int(seq_match.group("value")) if seq_match else None}
            submits_list.append(rec)
            if rec["seq"] is not None:
                submits_seq[rec["seq"]] = rec
            continue
        m = _OWNERSHIP.search(line)
        if m:
            d = m.groupdict()
            ownership_seq[int(d["seq"])] = {
                "phys_held_all": int(d["phys_held"]),
                "out_cleared_all": int(d["out_cleared"]),
                "max_out_sq": int(d["max_out"]),
                "phys_release_t_ms": int(d["phys_rel"]),
                "ticks": int(d["ticks"]),
                "dur_ms": int(d["dur"]),
            }
            continue
        m = _ATTRIBUTION.search(line)
        if m:
            d = m.groupdict()
            rec = {
                "green_confirmed": int(d["gc"]),
                "target_mode": d["tmode"],
                "green_width": float(d["gw"]),
                "green_confirm_fill": float(d["gcf"]),
                "green_confirm_ms": int(d["gcm"]),
                "vel": float(d["vel"]),
                "crossing_eta": int(d["ceta"]),
                "expected_rise": float(d["erise"]),
                "within_reach": int(d["wr"]),
            }
            seq = int(d["seq"])
            attribution_seq[seq] = rec  # last-wins (kept for single-session callers/tests)
            attribution_list.append({"utc": parse_utc(d["ts"]), "seq": seq, **rec})
            continue
        m = _FRESHNESS.search(line)
        if m:
            d = m.groupdict()
            freshness_list.append({
                "utc": parse_utc(d["ts"]),
                "seq": int(d["seq"]),
                "blind_fire": int(d["blind"]),
                "code": d["code"],
                "last_fresh": int(d["lastfresh"]),
                "fresh": int(d["fresh"]),
                "stale_ms": int(d["stale"]),
                "mem_trusted": int(d["memtrust"]),
            })
    # Attach the freshness verdict to each release (seq + nearest timestamp, cross-session safe).
    for r in releases:
        r["freshness"] = pair_freshness(r, freshness_list)
    return releases, submits_seq, submits_list, ownership_seq, attribution_seq, attribution_list


def pair_submit(rel, submits_seq, submits_list):
    """Return (text, paired_by). Deterministic via seq when both sides have it,
    else nearest timestamp within 250ms (flagged as approximate)."""
    if not submits_seq and not submits_list:
        return "", ""  # no submit logging in this batch (pre-fix)
    if rel["seq"] is not None and rel["seq"] in submits_seq:
        s = submits_seq[rel["seq"]]
        return f"ok={s['ok']} sq={s['square']}", "seq"
    best, bestdt = None, 0.251
    for s in submits_list:
        dt = abs((s["utc"] - rel["utc"]).total_seconds())
        if dt < bestdt:
            best, bestdt = s, dt
    if best is not None:
        return f"ok={best['ok']} sq={best['square']}~", "nearest_ts(approx)"
    return "unpaired", ""


def pair_attribution(rel, attribution_list):
    """Return the attribution dict for this release, matched by seq AND nearest timestamp.
    The "Release issued" and "Release attribution" lines for one release share a near-identical
    stamp (emitted back-to-back), so requiring seq==rel.seq AND |dt|<0.5s disambiguates the
    cross-session seq reuse that a seq-only dict cannot (seq restarts at 1 each app launch)."""
    if rel["seq"] is None:
        return None
    best, bestdt = None, 0.5
    for a in attribution_list:
        if a["seq"] != rel["seq"]:
            continue
        dt = abs((a["utc"] - rel["utc"]).total_seconds())
        if dt < bestdt:
            best, bestdt = a, dt
    return best


def pair_freshness(rel, freshness_list):
    """Return the freshness-verdict dict for this release, matched by seq AND nearest timestamp
    (same cross-session seq-reuse disambiguation as pair_attribution: the freshness line is
    emitted back-to-back with "Release issued:" so |dt|<0.5s pins it to the right session)."""
    if rel["seq"] is None:
        return None
    best, bestdt = None, 0.5
    for f in freshness_list:
        if f["seq"] != rel["seq"]:
            continue
        dt = abs((f["utc"] - rel["utc"]).total_seconds())
        if dt < bestdt:
            best, bestdt = f, dt
    return best


def summarize_freshness(releases):
    """Log-only aggregate of the "Release freshness:" verdicts (no video needed). The primary
    read for the memory-trust A/B and the blind-fire question: how many releases fired on the
    feedforward clock with stale vision (blindFire=1), how stale vision was when they did
    (staleMs), and how many memory echoes the trust promoted (memTrusted, 0 unless the
    memory_trust_enabled A/B is ON). Returns a list of printable lines, or [] when the log
    predates the freshness build (no lines to summarize)."""
    fr = [r["freshness"] for r in releases if r.get("freshness")]
    if not fr:
        return []
    n = len(fr)
    blind = [f for f in fr if f["blind_fire"] == 1]
    stale_vals = sorted(f["stale_ms"] for f in blind if f["stale_ms"] >= 0)
    mem_total = sum(f["mem_trusted"] for f in fr)
    mem_releases = sum(1 for f in fr if f["mem_trusted"] > 0)
    bpct = 100.0 * len(blind) / n
    lines = [f"\n=== FRESHNESS / BLIND-FIRE SUMMARY ({n} releases with a verdict) ==="]
    if stale_vals:
        med = stale_vals[len(stale_vals) // 2]
        lines.append(f"  blind fires (feedforward, vision stale): {len(blind)}/{n} ({bpct:.0f}%)  "
                     f"staleMs min/med/max = {stale_vals[0]}/{med}/{stale_vals[-1]}")
    else:
        lines.append(f"  blind fires (feedforward, vision stale): {len(blind)}/{n} ({bpct:.0f}%)")
    lines.append(f"  memory-trust promotions: total={mem_total} across {mem_releases}/{n} releases"
                 f"   ({'A/B ON' if mem_total else 'A/B off or no echoes trusted'})")
    by_code = {}
    for f in fr:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    lines.append("  by code: " + ", ".join(f"{k}={v}" for k, v in
                                           sorted(by_code.items(), key=lambda x: -x[1])))
    return lines


def make_detector(root: Path, style: str, color: str):
    from meter_detector import MeterDetector, load_detector_config
    cfg = load_detector_config(str(root / "settings.json"))
    cfg.meter_style = style
    cfg.meter_color = color
    det = MeterDetector(str(root / "meter_styles"), cfg)
    det.set_active_style(style)
    return det


def _reject_label(r):
    """Detector outcome for one frame: 'accepted' (clean lock), the rejection_reason
    (e.g. roi_not_found / meter_memory / bbox_unstable), or 'no_meter' when undetected
    with no specific reason."""
    rej = getattr(r, "rejection_reason", "") or ""
    if r.detected:
        return rej if rej else "accepted"
    return rej if rej else "no_meter"


def video_fill_trajectory(cv2, cap, fps, det, crop, t_center, pre, post):
    """Replay [t_center-pre, t_center+post], crop+detect, return (fill_at_detected,
    green_at_detected, peak_fill, reject_at_center, trajectory[(rel_ms, fill, conf, rej)]).

    fill/green are from the nearest DETECTED frame; reject_at_center is the detector
    outcome of the frame nearest the release instant (any frame) — so a release that
    landed on a detector miss shows e.g. 'roi_not_found' instead of a blank.

    NOTE: the nearest-center fill is sensitive to the recording-start clock offset,
    which on the 10:28 file is ~-0.3..-0.5s and drifts (120fps frame-time vs wall
    clock). peak_fill (did the REAL meter reach the top anywhere in the window?) is
    offset-robust and is the reliable dropout signal: a low log fill with a high
    peak_fill == detection dropout, not a weak meter."""
    x, y, w, h = crop
    start_ms = max(0.0, (t_center - pre) * 1000.0)
    cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)
    traj = []
    fill_c = green_c = None
    peak = None
    reject_c = None
    best_det_dt = 1e9   # nearest DETECTED frame -> fill/green
    best_any_dt = 1e9   # nearest frame of any kind -> reject at the release instant
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t > t_center + post:
            break
        sub = frame[y:y + h, x:x + w]
        r = det.detect(sub)
        rel_ms = (t - t_center) * 1000.0
        label = _reject_label(r)
        fill = float(r.fill_pct) if r.detected else None
        traj.append((round(rel_ms, 1), fill,
                     round(float(r.confidence), 2) if r.detected else None, label))
        if abs(rel_ms) < best_any_dt:
            best_any_dt = abs(rel_ms)
            reject_c = label
        if r.detected:
            if peak is None or r.fill_pct > peak:
                peak = float(r.fill_pct)
            if abs(rel_ms) < best_det_dt:
                best_det_dt = abs(rel_ms)
                fill_c = round(float(r.fill_pct), 1)
                green_c = round(float(r.green_window_center_pct), 1)
    return (fill_c, green_c, (round(peak, 1) if peak is not None else None),
            reject_c, traj)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--video-start-utc", required=True,
                    help="UTC instant of video frame 0, e.g. 2026-06-04T15:28:09Z")
    ap.add_argument("--clock-offset-ms", type=float, default=0.0,
                    help="added to every video_t to absorb sub-second recording-start slack")
    ap.add_argument("--crop", default=",".join(map(str, DEFAULT_CROP)),
                    help="game-stream crop x,y,w,h within the recording")
    ap.add_argument("--style", default="Arrow2")
    ap.add_argument("--color", default="Purple")
    ap.add_argument("--window-pre", type=float, default=1.5)
    ap.add_argument("--window-post", type=float, default=0.4)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--in-window-only", action="store_true",
                    help="drop releases whose video_t falls outside the recording")
    ap.add_argument("--no-video", action="store_true",
                    help="parse log only; skip detector replay. Writes to a separate "
                         "*.parseonly.csv so it never clobbers the real correlation CSV.")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"log not found: {log_path}", file=sys.stderr)
        return 2
    releases, submits_seq, submits_list, ownership_seq, attribution_seq, attribution_list = parse_log(log_path)
    if not releases:
        print("no 'Release issued' lines found", file=sys.stderr)
        return 3

    # Log-only freshness/blind-fire summary (prints with or without a video — it is the read
    # for the memory-trust A/B and "did this batch fire blind").
    for _line in summarize_freshness(releases):
        print(_line)

    vstart = parse_utc(args.video_start_utc)
    crop = tuple(int(v) for v in args.crop.split(","))

    cap = fps = cv2 = None
    duration_s = None
    if not args.no_video:
        video = Path(args.video)
        if not video.exists():
            print(f"video not found: {video}", file=sys.stderr)
            return 4
        try:
            import cv2 as _cv2
            cv2 = _cv2
        except Exception as exc:
            print(f"opencv import failed: {exc}", file=sys.stderr)
            return 5
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            print(f"could not open video: {video}", file=sys.stderr)
            return 6
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration_s = (frame_count / fps) if fps else None

    out_path = Path(args.out)
    # --no-video must NOT overwrite the real correlation CSV (it would blank every video
    # column). Redirect a default-path parse-only run to a clearly distinct file.
    if args.no_video and out_path == DEFAULT_OUT:
        out_path = DEFAULT_OUT.with_name("release_correlation.parseonly.csv")
        print(f"[--no-video] writing parse-only CSV to {out_path} (real CSV untouched)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # `seq` is parsed from "Release issued:" but was never written; the new attribution joins by
    # seq, so expose it as the FIRST column to audit every join.
    fields = ["seq", "log_ts", "video_t_s", "in_window", "shot_type", "release_code",
              "fill_pct", "target_pct", "confidence", "green_window", "video_fill_pct",
              "video_peak_fill", "video_green_c", "video_reject", "fill_delta",
              "virtual_submit", "submit_pairing",
              "ownership_phys_held_all", "ownership_out_cleared_all",
              "ownership_phys_release_t_ms", "ownership_ticks", "ownership_dur_ms",
              "ownership_class",
              "attr_green_confirmed", "attr_target_mode", "attr_green_width",
              "attr_green_confirm_fill", "attr_vel", "attr_crossing_eta", "attr_within_reach",
              "user_result"]
    rows = []
    n_out = 0
    for rel in releases:
        video_t = (rel["utc"] - vstart).total_seconds() + args.clock_offset_ms / 1000.0
        # In-window = the release time falls inside the recording. When the video isn't
        # loaded (--no-video) we only know t>=0; with the video we also bound by duration.
        if duration_s is not None:
            in_window = 0.0 <= video_t <= duration_s
        else:
            in_window = video_t >= 0.0
        if not in_window:
            n_out += 1
        if args.in_window_only and not in_window:
            continue
        vfill = vgreen = vpeak = vreject = ""
        traj = []
        if cap is not None and in_window:
            det = make_detector(ROOT, args.style, args.color)  # fresh per shot
            f, g, pk, rj, traj = video_fill_trajectory(cv2, cap, fps, det, crop, video_t,
                                                       args.window_pre, args.window_post)
            vfill = "" if f is None else f
            vgreen = "" if g is None else g
            vpeak = "" if pk is None else pk
            vreject = "" if rj is None else rj
        submit_text, pairing = pair_submit(rel, submits_seq, submits_list)
        delta = "" if vfill == "" else round(rel["fill"] - float(vfill), 1)
        own = ownership_seq.get(rel["seq"]) if rel["seq"] is not None else None
        own_class = classify_ownership(own)
        attr = pair_attribution(rel, attribution_list)
        rows.append({
            "seq": "" if rel["seq"] is None else rel["seq"],
            "log_ts": rel["log_ts"], "video_t_s": round(video_t, 3),
            "in_window": "yes" if in_window else "no",
            "shot_type": rel["shot"], "release_code": rel["code"],
            "fill_pct": rel["fill"], "target_pct": rel["target"],
            "confidence": rel["conf"], "green_window": rel["green"],
            "video_fill_pct": vfill, "video_peak_fill": vpeak, "video_green_c": vgreen,
            "video_reject": vreject, "fill_delta": delta, "virtual_submit": submit_text,
            "submit_pairing": pairing,
            "ownership_phys_held_all": "" if own is None else own["phys_held_all"],
            "ownership_out_cleared_all": "" if own is None else own["out_cleared_all"],
            "ownership_phys_release_t_ms": "" if own is None else own["phys_release_t_ms"],
            "ownership_ticks": "" if own is None else own["ticks"],
            "ownership_dur_ms": "" if own is None else own["dur_ms"],
            "ownership_class": own_class,
            "attr_green_confirmed": "" if attr is None else attr["green_confirmed"],
            "attr_target_mode": "" if attr is None else attr["target_mode"],
            "attr_green_width": "" if attr is None else attr["green_width"],
            "attr_green_confirm_fill": "" if attr is None else attr["green_confirm_fill"],
            "attr_vel": "" if attr is None else attr["vel"],
            "attr_crossing_eta": "" if attr is None else attr["crossing_eta"],
            "attr_within_reach": "" if attr is None else attr["within_reach"],
            "user_result": "",
        })
        rng = ""
        if traj:
            fills = [t[1] for t in traj if t[1] is not None]
            if fills:
                rng = f" trajfill[{min(fills):.0f}->{max(fills):.0f}%]"
        peak_s = f" peak={vpeak}" if vpeak != "" else ""
        rej_s = f" rej={vreject}" if vreject not in ("", "accepted") else ""
        flag = "" if in_window else "  [OUT-OF-WINDOW]"
        own_s = f" own={own_class}" if own_class else ""
        print(f"  {rel['log_ts']}  t={video_t:8.3f}s {rel['shot']:<10} "
              f"{rel['code']:<16} log_fill={rel['fill']:5.1f}% "
              f"video_fill={vfill if vfill!='' else '   -':>5}{peak_s}{rej_s}{own_s}{rng}{flag}")

    if cap is not None:
        cap.release()
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n_in = len(rows) - n_out if not args.in_window_only else len(rows)
    print(f"\n=== releases: {len(rows)} written | in-window={n_in} out-of-window={n_out}"
          + (" (dropped)" if args.in_window_only else " (kept, marked in_window=no)") + " ===")

    # Per-shot-type log-vs-video fill comparison (in-window rows with a resolved fill).
    print(f"=== log_fill vs video_fill by shot type ===")
    by = {}
    for r in rows:
        if r["video_fill_pct"] == "":
            continue
        by.setdefault(r["shot_type"], []).append(
            (r["fill_pct"], float(r["video_fill_pct"]), float(r["fill_delta"])))
    for shot, vals in sorted(by.items()):
        n = len(vals)
        md = sum(v[2] for v in vals) / n
        print(f"  {shot:<12} n={n:<3} mean log_fill={sum(v[0] for v in vals)/n:5.1f}% "
              f"mean video_fill={sum(v[1] for v in vals)/n:5.1f}% "
              f"mean delta(log-video)={md:+5.1f}%")
    if not by and not args.no_video:
        print("  (no video fills resolved — check --video-start-utc / --crop)")

    # Dropout signal: a low logged fill with a high REAL peak fill == the engine froze
    # on a stale/dropped detection while the meter actually climbed (fade timeout_fallback
    # / go-to max_hold_safety), NOT a genuinely weak meter. Offset-robust.
    drop = [r for r in rows if r["video_peak_fill"] != ""
            and r["release_code"] in ("timeout_fallback", "max_hold_safety")
            and float(r["video_peak_fill"]) - r["fill_pct"] >= 25.0]
    if drop:
        print("\n=== DROPOUT suspects (low log fill, high real peak) ===")
        for r in drop:
            print(f"  {r['log_ts']} {r['shot_type']:<10} {r['release_code']:<16} "
                  f"log_fill={r['fill_pct']:5.1f}%  real_peak={r['video_peak_fill']}%  "
                  f"reject@release={r['video_reject'] or '-'}")

    # Go-To release-path attribution (from the "Release attribution:" lines). THE read for the
    # bimodal Go-To split: greenConfirmed=1/targetMode=green_tip => the green block rode the tip
    # (typically late ~100); greenConfirmed=0/targetMode=meter_full => the velocity block fired at
    # the reachability floor (typically early ~70). greenConfirmFill shows how late the tracker
    # locked. Confirms (or refutes) the green-confirmation-race hypothesis straight off the run.
    goto_attr = [r for r in rows if r["shot_type"] == "Go-To" and r["attr_target_mode"] != ""]
    if goto_attr:
        print("\n=== Go-To release-path attribution ===")
        for r in goto_attr:
            gcf = r["attr_green_confirm_fill"]
            gcf_s = "never" if gcf == "" or float(gcf) < 0 else f"{float(gcf):.0f}%"
            print(f"  seq={r['seq'] or '-':<4} {r['release_code']:<16} "
                  f"greenConfirmed={r['attr_green_confirmed']} "
                  f"targetMode={r['attr_target_mode']:<11} fill@confirm={gcf_s:<6} "
                  f"release_fill={r['fill_pct']:5.1f}%  vel={r['attr_vel']} "
                  f"withinReach={r['attr_within_reach']}")
        gc_high = [r for r in goto_attr if str(r["attr_green_confirmed"]) == "1"]
        gc_low = [r for r in goto_attr if str(r["attr_green_confirmed"]) == "0"]
        def _mean_fill(rs):
            return (sum(x["fill_pct"] for x in rs) / len(rs)) if rs else float("nan")
        print(f"  -- greenConfirmed=1 (green block): n={len(gc_high)} "
              f"mean release_fill={_mean_fill(gc_high):5.1f}%   "
              f"greenConfirmed=0 (velocity block): n={len(gc_low)} "
              f"mean release_fill={_mean_fill(gc_low):5.1f}%")
    elif any(r["shot_type"] == "Go-To" for r in rows):
        print("\n=== Go-To release-path attribution ===")
        print("  (no 'Release attribution:' lines - log predates the attribution build)")

    # Ownership class tally (from the "Release ownership:" summary lines). This is the
    # primary read for the ownership/routing pass: how many shots Orion owned cleanly vs.
    # left output Square set (engine bug) vs. cleared-but-user-still-holding (a routing
    # leak the video must confirm) vs. the user releasing physically mid-pulse.
    own_rows = [r for r in rows if r["ownership_class"]]
    if own_rows:
        tally = {}
        for r in own_rows:
            tally[r["ownership_class"]] = tally.get(r["ownership_class"], 0) + 1
        print("\n=== ownership class tally ===")
        for cls, n in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"  {cls:<24} {n}")
        leaks = [r for r in own_rows if r["ownership_class"] == "possible_routing_leak"]
        if leaks:
            print("  NOTE: possible_routing_leak = Orion cleared output Square for the whole "
                  "pulse while\n        the user kept holding physical Square. Confirm against "
                  "the video whether the\n        in-game shot actually released; if not, "
                  "Chiaki is reading the physical pad.")
    else:
        print("\n=== ownership class tally ===")
        print("  (no 'Release ownership:' lines - log predates the ownership-trace build)")
    print("\nNOTE: video_fill_pct is the nearest-to-center sample and is sensitive to the\n"
          "recording-start clock offset (~-0.3..-0.5s, drifting on this 120fps file).\n"
          "For absolute timing use --clock-offset-ms; for dropout evidence use peak vs log.")
    print(f"\nwrote {out_path}  ({len(rows)} releases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
