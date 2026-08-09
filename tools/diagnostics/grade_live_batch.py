#!/usr/bin/env python3
"""One-command LIVE-BATCH grading for timing tuning.

Input an `orion_native.log` (+ optionally the OBS .mp4 of the batch) and get a
per-shot-type report: EARLY / LATE / GREEN verdict per shot, the per-type green
rate, the mean release-fill-vs-green-window, the meter-appear-anchor engagement
rate, and a RECOMMENDED per-type offset delta (ms) to dial earlyLateOffsetMs /
the per-type feedforward seeds.

This is a DIAGNOSTIC TOOL ONLY. It never touches the engine / stream / detector
and never writes settings.json / learning.json.

WHAT IT READS (all paired by seq AND nearest timestamp so the per-launch seq
reset that accumulates across sessions in one log file never cross-links them):
  * "Release issued:" — fill N% target N% (green A-B) ... code=.. presence=..
        conf=.. age=..ms offset=..ms seq=N shot=TYPE
        The release fill vs the green window [A,B] gives the PRIMARY verdict:
          fill < A          -> EARLY
          A <= fill <= B    -> GREEN
          fill > B          -> LATE
        green == -1.0--1.0  -> no green window was found; grade from the engine's
        own Shot-outcome verdict and/or the HUD instead.
  * "Release tempo:"     — seq mode/bucket/path/plannedFlickMs/flickDir
  * "Release freshness:" — seq blindFire/staleMs/code (did it fire blind?)
  * "Shot outcome:"      — seq verdict=EXCELLENT/GOOD/EARLY/LATE errorMs
        learnedOffset [ffClockMs meterClockMs anchor=meter_appear/hold_start
        greens/misses]. errorMs is SIGNED: + = LATE, - = EARLY, 0 = on time.

THE KEY ACCURACY SIGNAL (per the brief): does a code=feedforward_target release
ride the GENUINE meter-appear anchor (anchor=meter_appear on the paired outcome,
good) or fall back to the hold-start clock (meter not visible)? Reported as the
per-type meter-appear-anchor engagement rate. The "meter not visible" fallback is
also detectable on the issued line itself (presence=no_sample_ever / the
"(meter not visible)" reason text), so it is reported even with no outcome line.

GROUND TRUTH FROM THE MP4 (optional): the top-center in-game TIMING HUD. Per
memory nexusvision-read-shot-feedback-from-recording: crop=560:150:680:0 at
1080p, sampled at release_time + ~1.2s, classified by colour (green=EXCELLENT,
red=EARLY/LATE). TIMEZONE: the log stamps are UTC (Z); the OBS mp4 filename is
LOCAL America/Chicago CDT (UTC-5), so video_start_utc = local + 5h. Pass
--video-start-utc to override; otherwise it is inferred from the filename.

RECOMMENDED OFFSET DELTA: per type, from the engine's own mean signed errorMs
(authoritative, when Shot-outcome lines exist) and corroborated by the fill-vs-
green-window centre miss. Convention (memory nexusvision-live-timing-breakthrough
"late -> raise / early -> lower"): a LATE batch (errorMs > 0) recommends RAISING
earlyLateOffsetMs; an EARLY batch recommends LOWERING it. The printed delta is the
signed change to ADD to the current per-type offset.

USAGE:
  # log only (no video) — the fast inner-loop read:
  python tools/diagnostics/grade_live_batch.py --log logs/orion_native.log

  # grade only the most recent session's shots:
  python tools/diagnostics/grade_live_batch.py --log logs/orion_native.log --last-session

  # full ground-truth grade against the OBS recording:
  python tools/diagnostics/grade_live_batch.py \
      --log logs/orion_native.log \
      --video "C:/Users/Administrator/Videos/2026-06-24 11-15-00.mp4"
      # (video-start-utc inferred from the filename as local CDT + 5h)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The canonical shot types we tune (printed in this order first); any other type
# found in the log (Back/Front/Post Fade, etc.) is appended after these.
CANONICAL_ORDER = ["Standstill", "Left Fade", "Right Fade", "Go-To", "Tempo"]

DEFAULT_LOG = ROOT / "logs" / "orion_native.log"
# Top-center TIMING-HUD crop at 1080p (ffmpeg W:H:X:Y), per memory
# nexusvision-read-shot-feedback-from-recording.
DEFAULT_HUD_CROP = "560:150:680:0"
DEFAULT_HUD_DELAY_S = 1.2
TZ_LOCAL_TO_UTC_HOURS = 5.0  # dev machine America/Chicago CDT = UTC-5 -> +5

_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z"

# "2026-06-04T07:42:25.588Z  Release issued: fill 57.9% target 93.9% (green 89.5-100.0)
#   plan=.. reason=.. code=green_confirmed presence=accepted src=sidecar-fusion
#   conf=0.85 age=4ms offset=24.1ms seq=N shot=Standstill"
# seq= is optional (older lines omit it) and precedes shot=. shot=<type> may
# contain a space (e.g. "Right Fade") and runs to end-of-line.
_ISSUED = re.compile(
    _TS + r"\s+Release issued: fill (?P<fill>[\d.]+)% target (?P<target>[\d.]+)% "
    r"\(green (?P<gs>-?[\d.]+)-(?P<ge>-?[\d.]+)\).*?"
    r"code=(?P<code>\w+) presence=(?P<presence>\w+) src=(?P<src>[\w-]+) "
    r"conf=(?P<conf>[\d.]+) age=(?P<age>\d+)ms offset=(?P<offset>-?[\d.]+)ms"
    r"(?:\s+seq=(?P<seq>\d+))? shot=(?P<shot>.+?)\s*$"
)
# "Release tempo: seq=1 mode=ButtonShot bucket=Standstill path=feedforward_target
#   plannedFlickMs=663 flickDir=- shot=Standstill"
_TEMPO = re.compile(
    _TS + r"\s+Release tempo: seq=(?P<seq>\d+) mode=(?P<mode>\w+) "
    r"bucket=(?P<bucket>[\w-]+) path=(?P<path>\w+) "
    r"plannedFlickMs=(?P<flickms>-?\d+) flickDir=(?P<flickdir>\S+) shot=(?P<shot>.+?)\s*$"
)
# "Release freshness: seq=1 blindFire=1 code=feedforward_target lastFresh=0 fresh=26
#   staleMs=29 memTrusted=0 shot=Go-To"
_FRESHNESS = re.compile(
    _TS + r"\s+Release freshness: seq=(?P<seq>\d+) blindFire=(?P<blind>[01]) "
    r"code=(?P<code>\w+) lastFresh=(?P<lastfresh>[01]) fresh=(?P<fresh>\d+) "
    r"staleMs=(?P<stale>-?\d+) memTrusted=(?P<memtrust>\d+) shot=(?P<shot>.+?)\s*$"
)
# "Shot outcome: seq=11 verdict=LATE errorMs=28.0 learnedOffset=3.7 [ffClockMs=452
#   meterClockMs=80 anchor=meter_appear phase=lock greens=4 misses=0 learnCount=8]
#   shot=Standstill"  — the bracketed fields appear only in the richer build; parse
#   the always-present prefix strictly, the rest opportunistically.
# 2026-08-08: builds after the lead-conflict surfacing pass replaced the permanently
# inert learnedOffset=/ffClockMs= fields with the ACTIVE lead and its provenance
# ("... errorMs=28.0 leadMs=320.0 lead_source=user meterClockMs=..."). Accept both so
# this tool grades historical and current logs alike; learned_offset/ff_clock are None
# on new-format lines (they were seed echoes, not measurements — see the controller
# comment at the emit site).
_OUTCOME = re.compile(
    _TS + r"\s+Shot outcome: seq=(?P<seq>\d+) verdict=(?P<verdict>\w+) "
    r"errorMs=(?P<errms>-?[\d.]+) "
    r"(?:learnedOffset=(?P<learned>-?[\d.]+)"
    r"|leadMs=(?P<leadms>-?[\d.]+) lead_source=(?P<leadsrc>\w+))"
    r".*?shot=(?P<shot>.+?)\s*$"
)
_OUT_ANCHOR = re.compile(r"anchor=(?P<v>\w+)")
_OUT_FFCLOCK = re.compile(r"ffClockMs=(?P<v>-?\d+)")
_OUT_METERCLOCK = re.compile(r"meterClockMs=(?P<v>-?\d+)")
_OUT_GREENS = re.compile(r"greens=(?P<v>\d+)")
_OUT_MISSES = re.compile(r"misses=(?P<v>\d+)")

# OBS filename "YYYY-MM-DD HH-MM-SS.mp4" (also tolerate '_' separators).
_FNAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_utc(ts: str) -> dt.datetime:
    ts = ts.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(ts, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable UTC timestamp: {ts!r}")


def parse_log(path: Path):
    """Return a list of release dicts, each enriched with its paired tempo /
    freshness / outcome record."""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    releases = []
    tempo_list, freshness_list, outcome_list = [], [], []
    for line in text:
        m = _ISSUED.search(line)
        if m:
            d = m.groupdict()
            gs, ge = float(d["gs"]), float(d["ge"])
            releases.append({
                "utc": parse_utc(d["ts"]),
                "log_ts": d["ts"] + "Z",
                "shot": d["shot"].strip(),
                "code": d["code"],
                "presence": d["presence"],
                "src": d["src"],
                "fill": float(d["fill"]),
                "target": float(d["target"]),
                "green_lo": gs,
                "green_hi": ge,
                "has_green": not (gs <= 0 and ge <= 0),  # -1.0--1.0 => no window
                "conf": float(d["conf"]),
                "age": int(d["age"]),
                "offset": float(d["offset"]),
                "seq": int(d["seq"]) if d.get("seq") else None,
                "meter_not_visible": "meter not visible" in line,
            })
            continue
        m = _TEMPO.search(line)
        if m:
            d = m.groupdict()
            tempo_list.append({
                "utc": parse_utc(d["ts"]), "seq": int(d["seq"]),
                "mode": d["mode"], "bucket": d["bucket"], "path": d["path"],
                "flick_ms": int(d["flickms"]), "flick_dir": d["flickdir"],
            })
            continue
        m = _FRESHNESS.search(line)
        if m:
            d = m.groupdict()
            freshness_list.append({
                "utc": parse_utc(d["ts"]), "seq": int(d["seq"]),
                "blind_fire": int(d["blind"]), "code": d["code"],
                "stale_ms": int(d["stale"]), "mem_trusted": int(d["memtrust"]),
            })
            continue
        m = _OUTCOME.search(line)
        if m:
            d = m.groupdict()
            rec = {
                "utc": parse_utc(d["ts"]), "seq": int(d["seq"]),
                "verdict": d["verdict"], "error_ms": float(d["errms"]),
                # Old-format logs only; None on 2026-08-08+ logs (field was inert).
                "learned_offset": float(d["learned"]) if d.get("learned") else None,
                # New-format logs only: the lead the decision path consumed + provenance.
                "lead_ms": float(d["leadms"]) if d.get("leadms") else None,
                "lead_source": d.get("leadsrc"),
                "anchor": None, "ff_clock": None, "meter_clock": None,
                "greens": None, "misses": None,
            }
            for key, rx in (("anchor", _OUT_ANCHOR), ("ff_clock", _OUT_FFCLOCK),
                            ("meter_clock", _OUT_METERCLOCK),
                            ("greens", _OUT_GREENS), ("misses", _OUT_MISSES)):
                mm = rx.search(line)
                if mm:
                    rec[key] = mm.group("v") if key == "anchor" else int(mm.group("v"))
            outcome_list.append(rec)
            continue

    for r in releases:
        r["tempo"] = _pair_by_seq_ts(r, tempo_list)
        r["freshness"] = _pair_by_seq_ts(r, freshness_list)
        r["outcome"] = _pair_by_seq_ts(r, outcome_list)
    return releases


def _pair_by_seq_ts(rel, candidates, max_dt_s: float = 4.0):
    """Pair a release to its companion line by seq AND nearest timestamp. The
    companion lines are emitted within a few seconds of "Release issued:" for the
    same seq; requiring seq match AND |dt| small disambiguates the cross-session
    seq reuse (seq restarts at 1 each app launch) that a seq-only dict cannot.
    Outcome lines lag the release by ~1-3s, hence the generous default window."""
    if rel["seq"] is None:
        return None
    best, best_dt = None, max_dt_s
    for c in candidates:
        if c["seq"] != rel["seq"]:
            continue
        delta = abs((c["utc"] - rel["utc"]).total_seconds())
        if delta < best_dt:
            best, best_dt = c, delta
    return best


def split_sessions(releases, gap_s: float = 120.0):
    """Split the flat release list into sessions. A new OrionNative launch resets
    seq to 1 and there is a long wall-clock gap between batches, so a seq that
    goes backwards OR a > gap_s time jump starts a new session."""
    sessions, cur = [], []
    for r in releases:
        if cur:
            prev = cur[-1]
            seq_reset = (r["seq"] is not None and prev["seq"] is not None
                         and r["seq"] <= prev["seq"])
            time_jump = (r["utc"] - prev["utc"]).total_seconds() > gap_s
            if seq_reset or time_jump:
                sessions.append(cur)
                cur = []
        cur.append(r)
    if cur:
        sessions.append(cur)
    return sessions


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def verdict_from_fill(rel):
    """PRIMARY verdict from release fill vs the green window [lo,hi].
    Returns one of EARLY / GREEN / LATE / UNKNOWN (no window or no sample)."""
    if not rel["has_green"]:
        return "UNKNOWN"
    # presence=no_sample_ever / fill 0 with no window means the meter was never
    # seen this shot; fill is meaningless then.
    if rel["presence"] == "no_sample_ever":
        return "UNKNOWN"
    fill = rel["fill"]
    if fill < rel["green_lo"]:
        return "EARLY"
    if fill > rel["green_hi"]:
        return "LATE"
    return "GREEN"


# Map the engine Shot-outcome verdict onto our 3 buckets (GOOD counts as a make).
_OUTCOME_TO_BUCKET = {
    "EXCELLENT": "GREEN", "GOOD": "GREEN", "GREEN": "GREEN",
    "EARLY": "EARLY", "LATE": "LATE",
}


def best_verdict(rel):
    """Resolve a single verdict per release, in priority order:
       1. the HUD verdict (set when --video is supplied — true ground truth),
       2. fill-vs-green window (only when a window exists and a sample was taken),
       3. the engine's own Shot-outcome verdict (covers the no-window case),
       4. UNKNOWN.
    Returns (verdict, source)."""
    if rel.get("hud_verdict"):
        return rel["hud_verdict"], "hud"
    fv = verdict_from_fill(rel)
    if fv != "UNKNOWN":
        return fv, "fill_vs_green"
    out = rel.get("outcome")
    if out and out["verdict"] in _OUTCOME_TO_BUCKET:
        return _OUTCOME_TO_BUCKET[out["verdict"]], "shot_outcome"
    return "UNKNOWN", "none"


def fill_vs_green_center(rel):
    """Signed release-fill minus green-window CENTRE, in fill-% points (+ above the
    centre / late-leaning, - below / early-leaning). None when no window / no
    sample. This is the fill-domain corroboration of the time-domain errorMs."""
    if not rel["has_green"] or rel["presence"] == "no_sample_ever":
        return None
    center = 0.5 * (rel["green_lo"] + rel["green_hi"])
    return rel["fill"] - center


# --------------------------------------------------------------------------- #
# HUD ground truth (optional; robust to a missing / short video)
# --------------------------------------------------------------------------- #
def video_start_utc_from_name(video_path: str):
    m = _FNAME.search(os.path.basename(video_path))
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    local = dt.datetime(y, mo, d, hh, mm, ss)
    return (local + dt.timedelta(hours=TZ_LOCAL_TO_UTC_HOURS)).replace(tzinfo=dt.timezone.utc)


def find_ffmpeg(explicit):
    if explicit and os.path.isfile(explicit):
        return explicit
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    for c in (
        r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe",
        r"C:\msys64\mingw64\bin\ffmpeg.exe",
    ):
        if os.path.isfile(c):
            return c
    return None


def classify_hud(png_path):
    """Colour-classify the TIMING-HUD word. Green => GREEN (EXCELLENT), red => a
    miss (EARLY or LATE — colour alone can't tell which; the fill/outcome signals
    disambiguate direction, the HUD only confirms make-vs-miss). Returns
    (verdict, strength) with verdict in {GREEN, MISS, ?}."""
    try:
        import cv2
    except Exception:
        return ("?", 0)
    img = cv2.imread(png_path)
    if img is None:
        return ("?", 0)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (40, 80, 80), (85, 255, 255))
    red = (cv2.inRange(hsv, (0, 90, 80), (12, 255, 255))
           | cv2.inRange(hsv, (168, 90, 80), (180, 255, 255)))
    g, r = int((green > 0).sum()), int((red > 0).sum())
    floor = max(40, int(0.002 * img.shape[0] * img.shape[1]))
    if g > r and g >= floor:
        return ("GREEN", g)
    if r > g and r >= floor:
        return ("MISS", r)
    return ("?", max(g, r))


def grade_from_video(releases, video, vstart, ffmpeg, crop, delay_s, out_dir):
    """Sample the HUD for each in-window release and set rel['hud_verdict'] /
    rel['hud_raw']. Robust: a release whose time falls outside the recording, or a
    frame that can't be read, is left ungraded. Samples a small window around the
    nominal delay and keeps the clearest verdict (the banner appears at a variable
    lag and lingers)."""
    cap_dur = _video_duration_s(video)
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, ".tmp_hud.png")
    offsets = [delay_s + d for d in (-0.1, 0.4, 0.9, 1.4)]
    graded = 0
    for rel in releases:
        vt0 = (rel["utc"] - vstart).total_seconds()
        if vt0 < 0 or (cap_dur is not None and vt0 > cap_dur):
            continue  # release is outside the recording -> leave ungraded
        best, best_at = ("?", -1), None
        for off in offsets:
            vt = vt0 + off
            if vt < 0 or (cap_dur is not None and vt > cap_dur):
                continue
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{vt:.3f}",
                 "-i", video, "-frames:v", "1", "-vf", f"crop={crop}",
                 "-q:v", "2", "-y", tmp],
                check=False,
            )
            if not os.path.isfile(tmp):
                continue
            tv, strength = classify_hud(tmp)
            if tv != "?" and strength > best[1]:
                best, best_at = (tv, strength), vt
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        rel["hud_raw"] = best[0]
        rel["hud_at_s"] = best_at
        # GREEN maps straight through; a red MISS becomes EARLY/LATE using the
        # fill/outcome direction when we have it (HUD colour can't tell direction).
        if best[0] == "GREEN":
            rel["hud_verdict"] = "GREEN"
            graded += 1
        elif best[0] == "MISS":
            rel["hud_verdict"] = _direction_for_miss(rel)
            graded += 1
        else:
            rel["hud_verdict"] = None
    return graded


def _direction_for_miss(rel):
    out = rel.get("outcome")
    if out and out["verdict"] in ("EARLY", "LATE"):
        return out["verdict"]
    fv = verdict_from_fill(rel)
    if fv in ("EARLY", "LATE"):
        return fv
    c = fill_vs_green_center(rel)
    if c is not None:
        return "LATE" if c > 0 else "EARLY"
    return "MISS"  # known miss, direction unknown


def _video_duration_s(video):
    try:
        import cv2
    except Exception:
        return None
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    cap.release()
    return (n / fps) if fps else None


# --------------------------------------------------------------------------- #
# Aggregation + recommendation
# --------------------------------------------------------------------------- #
def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def _ordered_types(types):
    canon = [t for t in CANONICAL_ORDER if t in types]
    rest = sorted(t for t in types if t not in CANONICAL_ORDER)
    return canon + rest


def anchor_class(rel):
    """Classify ONE feedforward_target release into exactly one anchor bucket
    (mutually exclusive, so the per-type counts never overlap / exceed 100%):

      'meter_appear' - the paired Shot-outcome line reports anchor=meter_appear:
                       the per-type feedforward clock is calibrated off the GENUINE
                       meter appearance (the accurate path the brief wants high).
      'hold_start'   - the paired outcome reports anchor=hold_start: the clock fell
                       back to the cross-shot hold-start clock.
      'unknown'      - no outcome line was paired (older build / shot not graded),
                       so the calibration anchor can't be read from this release.

    The authoritative signal is the outcome's anchor= field (it states which clock
    the engine actually used). Returns one of the three strings, or None for a
    non-feedforward release."""
    if rel["code"] != "feedforward_target":
        return None
    out = rel.get("outcome")
    if out and out["anchor"] == "meter_appear":
        return "meter_appear"
    if out and out["anchor"] == "hold_start":
        return "hold_start"
    return "unknown"


def saw_meter_at_release(rel):
    """Did THIS shot's release see the live meter? Distinct from anchor_class
    (which is the per-type CLOCK calibration): a shot can ride a meter_appear-
    anchored clock yet still fire with the meter not visible this rep. True =
    a live sample was used; False = the "(meter not visible)" / presence=
    no_sample_ever blind path."""
    return not (rel["meter_not_visible"] or rel["presence"] == "no_sample_ever")


def recommend_delta(errs, centers):
    """Recommended signed change to ADD to the per-type earlyLateOffsetMs.

    Primary: the engine's own mean signed errorMs (authoritative ground truth from
    the post-release grader). Convention per memory nexusvision-live-timing-
    breakthrough: late (errorMs > 0) -> RAISE the offset (positive delta); early
    (errorMs < 0) -> LOWER it. So delta = +mean(errorMs).

    Fallback (no Shot-outcome lines in the batch): estimate from the mean fill-vs-
    green-CENTER miss converted to ms via a nominal rise velocity (~0.20 %/ms near
    the top, memory nexusvision-shot-meter-mechanic). Flagged as an estimate.

    Returns (delta_ms or None, basis_str)."""
    em = _mean(errs)
    if em is not None and errs:
        return round(em, 1), f"engine errorMs (n={len(errs)})"
    cm = _mean(centers)
    if cm is not None and centers:
        # +fill above center == late == raise offset. ~0.20 %/ms -> ms = pct/0.20.
        delta = cm / 0.20
        return round(delta, 1), f"fill-vs-green estimate (n={len(centers)}, ~0.20%/ms)"
    return None, "no signal"


def build_report(releases):
    by = {}
    for rel in releases:
        v, src = best_verdict(rel)
        rel["_verdict"], rel["_vsrc"] = v, src
        by.setdefault(rel["shot"], []).append(rel)
    return by


def print_per_shot(releases):
    print("\n=== PER-SHOT (chronological) ===")
    hdr = (f"{'seq':>4} {'shot':<11} {'verdict':<8} {'src':<13} {'code':<18} "
           f"{'fill':>6} {'green':>12} {'off':>7} {'tempo':<10} {'anchor':<11} "
           f"{'engine':<9}")
    print(hdr)
    print("-" * len(hdr))
    for rel in releases:
        green = (f"{rel['green_lo']:.0f}-{rel['green_hi']:.0f}"
                 if rel["has_green"] else "(none)")
        tempo = rel["tempo"]["mode"] if rel.get("tempo") else "-"
        out = rel.get("outcome")
        anchor = out["anchor"] if (out and out["anchor"]) else "-"
        eng = out["verdict"] if out else "-"
        fr = rel.get("freshness")
        blind = " *blind" if (fr and fr["blind_fire"]) else ""
        print(f"{(rel['seq'] if rel['seq'] is not None else '-'):>4} "
              f"{rel['shot']:<11} {rel['_verdict']:<8} {rel['_vsrc']:<13} "
              f"{rel['code']:<18} {rel['fill']:>5.1f}% {green:>12} "
              f"{rel['offset']:>6.1f} {tempo:<10} {anchor:<11} {eng:<9}{blind}")


def _fnum(x, fmt, dash="-"):
    """Format a possibly-None / NaN number; x==x is False only for NaN."""
    return (fmt % x) if (x is not None and x == x) else dash


def print_per_type(by):
    print("\n=== PER-SHOT-TYPE SUMMARY ===")
    hdr = (f"{'shot type':<12} {'n':>3} {'GRN':>4} {'ERL':>4} {'LAT':>4} "
           f"{'?':>3} {'green%':>7} {'fill-vs-grn':>12} {'errMs(eng)':>11} "
           f"{'mtr-appr%':>10} {'sawMtr%':>8} {'blind%':>7}")
    print(hdr)
    print("-" * len(hdr))
    recs = {}
    for shot in _ordered_types(by.keys()):
        rels = by[shot]
        n = len(rels)
        g = sum(1 for r in rels if r["_verdict"] == "GREEN")
        e = sum(1 for r in rels if r["_verdict"] == "EARLY")
        la = sum(1 for r in rels if r["_verdict"] == "LATE")
        u = sum(1 for r in rels if r["_verdict"] == "UNKNOWN")
        graded = g + e + la
        green_pct = (100.0 * g / graded) if graded else float("nan")

        centers = [c for c in (fill_vs_green_center(r) for r in rels) if c is not None]
        center_mean = _mean(centers)

        # meter-appear engagement: of the feedforward_target releases whose anchor
        # is KNOWN (a paired outcome), the share riding the genuine meter_appear
        # clock (vs the hold_start fallback). Mutually exclusive via anchor_class.
        ff_classes = [anchor_class(r) for r in rels if r["code"] == "feedforward_target"]
        ff_known = [c for c in ff_classes if c in ("meter_appear", "hold_start")]
        ff_pct = (100.0 * sum(1 for c in ff_known if c == "meter_appear")
                  / len(ff_known)) if ff_known else float("nan")

        # per-shot vision availability (distinct signal): did the release see the
        # live meter this rep?
        ff_rels = [r for r in rels if r["code"] == "feedforward_target"]
        saw_pct = (100.0 * sum(1 for r in ff_rels if saw_meter_at_release(r))
                   / len(ff_rels)) if ff_rels else float("nan")

        errs = [r["outcome"]["error_ms"] for r in rels if r.get("outcome")]
        err_mean = _mean(errs)
        blind = [r for r in rels if r.get("freshness")]
        blind_pct = (100.0 * sum(1 for r in blind if r["freshness"]["blind_fire"])
                     / len(blind)) if blind else float("nan")

        delta, basis = recommend_delta(errs, centers)
        recs[shot] = (delta, basis, err_mean, center_mean, green_pct, n)

        print(f"{shot:<12} {n:>3} {g:>4} {e:>4} {la:>4} {u:>3} "
              f"{_fnum(green_pct, '%.0f%%'):>7} {_fnum(center_mean, '%+.1f'):>12} "
              f"{_fnum(err_mean, '%+.1f'):>11} {_fnum(ff_pct, '%.0f%%'):>10} "
              f"{_fnum(saw_pct, '%.0f%%'):>8} {_fnum(blind_pct, '%.0f%%'):>7}")
    print("  legend: GRN/ERL/LAT = green/early/late; '?' = ungradeable (no window "
          "& no outcome).")
    print("  fill-vs-grn = mean(release_fill - green_window_center), fill-% "
          "(+late / -early leaning).")
    print("  mtr-appr% = feedforward clock anchored to genuine meter-appear (per-type "
          "calibration; want HIGH).")
    print("  sawMtr%   = releases that saw the LIVE meter this rep (vs blind 'meter "
          "not visible').")
    return recs


def print_recommendations(recs):
    print("\n=== RECOMMENDED PER-TYPE OFFSET DELTA (ms) ===")
    print("  Add the delta to that type's earlyLateOffsetMs / feedforward seed.")
    print("  Convention: LATE batch (errMs>0) -> RAISE offset (positive delta);")
    print("              EARLY batch (errMs<0) -> LOWER offset (negative delta).")
    hdr = f"  {'shot type':<12} {'delta ms':>9}  basis"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for shot in _ordered_types(recs.keys()):
        delta, basis, err_mean, center_mean, green_pct, n = recs[shot]
        if delta is None:
            print(f"  {shot:<12} {'--':>9}  {basis}")
            continue
        flag = "  (near-zero; hold)" if abs(delta) < 5 else ""
        print(f"  {shot:<12} {delta:>+9.1f}  {basis}{flag}")
    print("\n  NOTE: the engine's post-release grader can false-LATE genuine greens")
    print("  at the dead top (a made and a late shot both peak ~100 then recede).")
    print("  Trust --video HUD / the user's eyes over errorMs for FADES near the tip")
    print("  (memory nexusvision-contest-window-grader / nexusvision-deadtop).")


def print_anchor_callout(by):
    """The KEY accuracy read (the brief's headline signal). Two DISTINCT questions,
    reported separately so neither is misleading:

      1. CLOCK CALIBRATION: of the feedforward_target releases, what fraction ride a
         meter_appear-anchored clock vs the hold_start fallback (mutually exclusive
         via anchor_class). A high hold_start share => the meter-appear anchor isn't
         engaging => timing rides the cross-shot clock, the #1 fade-late lever
         (memory nexusvision-meter-appear-reanchor).
      2. PER-SHOT VISION: of those releases, what fraction actually SAW the live
         meter this rep vs fired blind ("(meter not visible)"). A clock can be
         meter_appear-anchored yet still fire blind on a given rep, so this is a
         separate axis."""
    ff_all = [r for rels in by.values() for r in rels if r["code"] == "feedforward_target"]
    if not ff_all:
        return
    classes = [anchor_class(r) for r in ff_all]
    n_appear = classes.count("meter_appear")
    n_hold = classes.count("hold_start")
    n_unknown = classes.count("unknown")
    known = n_appear + n_hold
    saw = sum(1 for r in ff_all if saw_meter_at_release(r))
    print("\n=== METER-APPEAR ANCHOR ENGAGEMENT (feedforward_target releases) ===")
    print(f"  feedforward_target releases : {len(ff_all)}")
    print("  -- clock calibration (from the paired Shot-outcome anchor=) --")
    if known:
        print(f"  meter_appear-anchored clock : {n_appear}/{known} "
              f"({100.0*n_appear/known:.0f}% of graded)  <- good (vision-calibrated)")
        print(f"  hold_start fallback clock   : {n_hold}/{known} "
              f"({100.0*n_hold/known:.0f}% of graded)  <- cross-shot clock")
    else:
        print("  (no anchor= field on any paired outcome — older build / unpaired)")
    if n_unknown:
        print(f"  anchor unknown (no outcome) : {n_unknown} (not graded this batch)")
    print("  -- per-shot vision availability (from the issued line) --")
    print(f"  saw the LIVE meter this rep : {saw}/{len(ff_all)} "
          f"({100.0*saw/len(ff_all):.0f}%)   fired blind: {len(ff_all)-saw} "
          f"({100.0*(len(ff_all)-saw)/len(ff_all):.0f}%)")


def print_verdict_disagreements(releases):
    """Where the fill-vs-green-window verdict disagrees with the engine's own
    Shot-outcome verdict. This is the crux of fade tuning: near the dead top a
    genuine green and a slightly-late shot both read fill ~100 then recede, so the
    detector-fill-vs-window and the engine grader can disagree (memory
    nexusvision-grader-recede-inversion / nexusvision-deadtop). A high disagreement
    rate on fades means neither log signal is trustworthy alone -> use --video / the
    user's eyes. Only releases with BOTH a window and a paired outcome qualify."""
    pairs = []
    for r in releases:
        fv = verdict_from_fill(r)
        out = r.get("outcome")
        if fv == "UNKNOWN" or not out or out["verdict"] not in _OUTCOME_TO_BUCKET:
            continue
        ev = _OUTCOME_TO_BUCKET[out["verdict"]]
        if fv != ev:
            pairs.append((r, fv, ev))
    if not pairs:
        return
    print("\n=== VERDICT DISAGREEMENTS (fill-vs-green  vs  engine Shot-outcome) ===")
    print(f"  {len(pairs)} releases where the two log signals disagree "
          f"(dead-top grader ambiguity; confirm with --video):")
    for r, fv, ev in pairs[:15]:
        print(f"    seq={(r['seq'] if r['seq'] is not None else '-'):>3} "
              f"{r['shot']:<11} fill-vs-green={fv:<6} engine={ev:<6} "
              f"(fill {r['fill']:.0f}% green {r['green_lo']:.0f}-{r['green_hi']:.0f}, "
              f"errMs={r['outcome']['error_ms']:+.0f})")
    if len(pairs) > 15:
        print(f"    ... and {len(pairs)-15} more")


def print_hud_summary(releases):
    graded = [r for r in releases if r.get("hud_verdict")]
    if not graded:
        return
    g = sum(1 for r in graded if r["hud_verdict"] == "GREEN")
    mism = [r for r in graded
            if (r["hud_verdict"] == "GREEN") != (verdict_from_fill(r) == "GREEN")
            and verdict_from_fill(r) != "UNKNOWN"]
    print("\n=== HUD GROUND-TRUTH (from --video) ===")
    print(f"  HUD-graded shots : {len(graded)}   HUD green-rate : "
          f"{100.0*g/len(graded):.0f}%")
    if mism:
        print(f"  HUD vs fill-vs-green disagreements: {len(mism)} "
              f"(trust the HUD; the green window or detector fill was off)")
        for r in mism[:12]:
            print(f"    seq={r['seq']} {r['shot']:<11} HUD={r['hud_verdict']:<6} "
                  f"fill-vs-green={verdict_from_fill(r):<7} "
                  f"(fill {r['fill']:.0f}% green {r['green_lo']:.0f}-{r['green_hi']:.0f})")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Grade a live batch (orion_native.log [+ OBS .mp4]) into a "
                    "per-shot-type timing report with recommended offset deltas.",
        epilog="DIAGNOSTIC ONLY -- never writes settings.json / learning.json / "
               "engine code. With --video the in-game TIMING HUD is the ground "
               "truth; without it, the report is log-only (fill-vs-green window + "
               "the engine's own Shot-outcome verdict).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG),
                    help=f"orion_native.log path (default {DEFAULT_LOG})")
    ap.add_argument("--video", default=None,
                    help="OBS .mp4 of the batch (optional; enables HUD ground truth)")
    ap.add_argument("--video-start-utc", default=None,
                    help="UTC instant of video frame 0, e.g. 2026-06-24T16:15:00Z. "
                         "Default: inferred from the OBS filename (LOCAL CDT + 5h).")
    ap.add_argument("--last-session", action="store_true",
                    help="grade only the most recent session (seq resets / >2min gap)")
    ap.add_argument("--shot", default=None,
                    help="restrict to one shot type (e.g. 'Right Fade')")
    ap.add_argument("--no-per-shot", action="store_true",
                    help="omit the per-shot chronological table (summary only)")
    ap.add_argument("--hud-crop", default=DEFAULT_HUD_CROP,
                    help=f"ffmpeg crop W:H:X:Y for the TIMING HUD (default {DEFAULT_HUD_CROP})")
    ap.add_argument("--hud-delay-s", type=float, default=DEFAULT_HUD_DELAY_S,
                    help=f"HUD appears ~this long after release (default {DEFAULT_HUD_DELAY_S})")
    ap.add_argument("--ffmpeg", default=None, help="explicit ffmpeg path")
    ap.add_argument("--hud-out", default=str(ROOT / "logs" / "diagnostics" / "hud_frames"),
                    help="dir for sampled HUD frames (created on demand)")
    args = ap.parse_args(argv)

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"log not found: {log_path}", file=sys.stderr)
        return 2
    releases = parse_log(log_path)
    if not releases:
        print("no 'Release issued:' lines found in the log", file=sys.stderr)
        return 3

    sessions = split_sessions(releases)
    if args.last_session:
        releases = sessions[-1]
        s = releases[0]
        print(f"[--last-session] {len(releases)} releases, "
              f"{s['log_ts']} .. {releases[-1]['log_ts']} "
              f"({len(sessions)} sessions in the log)")
    else:
        print(f"parsed {len(releases)} releases across {len(sessions)} session(s) "
              f"in {log_path}")

    if args.shot:
        releases = [r for r in releases if r["shot"].lower() == args.shot.lower()]
        if not releases:
            print(f"no releases for shot={args.shot!r}", file=sys.stderr)
            return 4

    # Optional HUD ground truth.
    if args.video:
        video = args.video
        if not os.path.isfile(video):
            print(f"[--video] not found, skipping HUD grade: {video}", file=sys.stderr)
        else:
            vstart = (parse_utc(args.video_start_utc)
                      if args.video_start_utc else video_start_utc_from_name(video))
            if vstart is None:
                print("[--video] could not infer video start time from the filename; "
                      "pass --video-start-utc. Skipping HUD grade.", file=sys.stderr)
            else:
                ffmpeg = find_ffmpeg(args.ffmpeg)
                if not ffmpeg:
                    print("[--video] ffmpeg not found (pass --ffmpeg); skipping HUD.",
                          file=sys.stderr)
                else:
                    print(f"[--video] HUD ground truth: start(UTC)={vstart.isoformat()} "
                          f"crop={args.hud_crop} delay~{args.hud_delay_s}s")
                    n = grade_from_video(releases, video, vstart, ffmpeg,
                                         args.hud_crop, args.hud_delay_s, args.hud_out)
                    print(f"[--video] HUD-graded {n}/{len(releases)} releases "
                          f"(others outside the recording or unreadable)")

    by = build_report(releases)
    if not args.no_per_shot:
        print_per_shot(releases)
    recs = print_per_type(by)
    print_anchor_callout(by)
    print_verdict_disagreements(releases)
    print_hud_summary(releases)
    print_recommendations(recs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
