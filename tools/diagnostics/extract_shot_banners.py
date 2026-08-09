#!/usr/bin/env python3
"""Extract the in-game TIMING-banner crop for every bot release in a recorded batch.

Banner-calibrate workflow (2026-06-09): the post-release shot METER can't tell green from late at
the dead-top (a made shot and a late shot both peak ~100 then recede), so the bot's self-grade is
unreliable there. The reliable ground truth is the on-screen TIMING banner (EXCELLENT / LATE / EARLY).
This tool pairs each `Release issued:` log line to its frame in the OBS recording and crops the
banner, named by seq + shot type + the bot's self-grade, so the true verdicts can be read in bulk
and the per-type release offset dialed from ground truth (the bot's meter self-learning is frozen via
ORION_FREEZE_CAL / settings freeze_calibration; only the offset/clock dialed here ships).

Usage:
  python tools/diagnostics/extract_shot_banners.py \
      --video "C:/Users/Administrator/Videos/2026-06-08 22-24-27.mp4" \
      --log logs/orion_native.log --shot "Right Fade" --out logs/banners

The recording START time is taken from the OBS filename ("YYYY-MM-DD HH-MM-SS.mp4", LOCAL time);
--tz-offset-hours converts it to the log's UTC (dev machine = America/Chicago CDT = UTC-5 -> +5).
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys

# "2026-06-09T03:24:40.236Z  Release issued: fill .. seq=11 shot=Standstill"
REL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z\s+Release issued:.*?\bseq=(?P<seq>\d+)\b.*?\bshot=(?P<shot>[\w-]+)"
)
# "... Shot outcome: seq=11 verdict=LATE ... shot=Standstill"
OUT_RE = re.compile(r"Shot outcome:\s*seq=(?P<seq>\d+)\s+verdict=(?P<verdict>\w+).*?\bshot=(?P<shot>[\w-]+)")

FNAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")


def parse_log(log_path):
    """Return {seq: {ts: datetime(UTC), shot: str, verdict: str|None}} for releases."""
    releases = {}
    outcomes = {}
    with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = REL_RE.search(line)
            if m:
                ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
                # last write per seq wins (a re-armed seq re-fires); keep the latest
                releases[int(m.group("seq"))] = {"ts": ts, "shot": m.group("shot")}
                continue
            mo = OUT_RE.search(line)
            if mo:
                outcomes[int(mo.group("seq"))] = mo.group("verdict")
    for seq, rel in releases.items():
        rel["verdict"] = outcomes.get(seq)
    return releases


def video_start_utc(video_path, tz_offset_hours):
    m = FNAME_RE.search(os.path.basename(video_path))
    if not m:
        raise SystemExit(f"could not parse a start time from filename: {os.path.basename(video_path)}")
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    local = dt.datetime(y, mo, d, hh, mm, ss)
    return (local + dt.timedelta(hours=tz_offset_hours)).replace(tzinfo=dt.timezone.utc)


def classify_banner(png_path):
    """Colour-classify the TIMING verdict word: green = ON-TARGET (EXCELLENT), red = OFF (LATE/EARLY).
    Returns (verdict, strength) where verdict in {'ON-TARGET','OFF','?'} and strength = matched pixels
    (used to pick the best frame across a sampling window). Samples a TIGHT sub-region over the verdict
    word only (the white 'TIMING' label is above it; the red shot-clock digits are center-lower) so the
    clock can't masquerade as a red 'LATE'."""
    try:
        import cv2
    except Exception:
        return ("?", 0)
    img = cv2.imread(png_path)
    if img is None:
        return ("?", 0)
    h, w = img.shape[:2]
    # verdict word only: left band x ~20-46%, mid height y ~28-55% (below the "TIMING" label,
    # above/left of the center shot-clock).
    sub = img[int(0.28 * h):int(0.55 * h), int(0.20 * w):int(0.46 * w)]
    if sub.size == 0:
        return ("?", 0)
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (40, 80, 80), (85, 255, 255))
    red = cv2.inRange(hsv, (0, 90, 80), (12, 255, 255)) | cv2.inRange(hsv, (168, 90, 80), (180, 255, 255))
    g, r = int((green > 0).sum()), int((red > 0).sum())
    floor = max(40, int(0.003 * sub.shape[0] * sub.shape[1]))
    if g > r and g >= floor:
        return ("ON-TARGET", g)
    if r > g and r >= floor:
        return ("OFF", r)
    return ("?", max(g, r))


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
    raise SystemExit("ffmpeg not found; pass --ffmpeg <path>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--log", default="logs/orion_native.log")
    ap.add_argument("--shot", default=None, help="only this shot type (e.g. 'Right Fade'); default all")
    ap.add_argument("--out", default="logs/banners")
    ap.add_argument("--tz-offset-hours", type=float, default=5.0, help="local->UTC (CDT=UTC-5 -> 5)")
    ap.add_argument("--banner-delay-s", type=float, default=1.3, help="banner appears ~this long after release")
    ap.add_argument("--crop", default="760:170:580:0", help="ffmpeg crop W:H:X:Y for the TIMING band (1080p)")
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    start = video_start_utc(args.video, args.tz_offset_hours)
    releases = parse_log(args.log)
    os.makedirs(args.out, exist_ok=True)

    rows = []
    # The banner appears at a VARIABLE delay (~1.2-2.7s) after release and lingers, so sample a window
    # and keep the frame with the clearest verdict (most matched pixels). A fixed single offset missed
    # banners that appeared late (validated: seq4 of the Standstill batch was blank at +1.3s, EXCELLENT
    # at +1.9s). Requires the user to PAUSE ~2-3s between shots so the prior banner has faded.
    offsets = [args.banner_delay_s + d for d in (-0.1, 0.4, 0.9, 1.4)]
    tmp = os.path.join(args.out, ".tmp_banner.png")
    for seq in sorted(releases):
        rel = releases[seq]
        if args.shot and rel["shot"].lower() != args.shot.lower():
            continue
        base = (rel["ts"] - start).total_seconds()
        verdict = rel.get("verdict") or "none"
        name = f"banner_seq{seq:02d}_{rel['shot']}_bot-{verdict}.png".replace(" ", "")
        outp = os.path.join(args.out, name)
        best, best_at = ("?", -1), None
        for off in offsets:
            vt = base + off
            if vt < 0:
                continue
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{vt:.3f}", "-i", args.video,
                 "-frames:v", "1", "-vf", f"crop={args.crop}", "-q:v", "2", "-y", tmp],
                check=False,
            )
            if not os.path.isfile(tmp):
                continue
            tv, strength = classify_banner(tmp)
            if tv != "?" and strength > best[1]:
                best, best_at = (tv, strength), vt
                shutil.copyfile(tmp, outp)  # keep the clearest verdict frame
        if os.path.isfile(tmp):
            os.remove(tmp)
        ok = os.path.isfile(outp)
        rows.append((seq, rel["shot"], verdict, best[0], f"{best_at:.2f}s" if best_at else "n/a",
                     name if ok else "NO-BANNER"))

    if not rows:
        print("No matching releases found (check --shot / --log / time window).")
        return
    print(f"video start (UTC): {start.isoformat()}   extracted {len(rows)} banners -> {args.out}")
    print(f"{'seq':>4}  {'banner':<10} {'bot':<10} {'shot':<12} {'vid_t':>8}  file")
    on = off = unk = 0
    for seq, shot, botv, true, vt, name in rows:
        flag = "  <-- bot MISLABELED a green shot" if (true == "ON-TARGET" and botv in ("LATE", "EARLY")) else ""
        print(f"{seq:>4}  {true:<10} {botv:<10} {shot:<12} {vt:>8}  {name}{flag}")
        on += true == "ON-TARGET"; off += true == "OFF"; unk += true == "?"
    print(f"\nBANNER TRUTH: {on} ON-TARGET (green) / {off} OFF (late|early) / {unk} unclear  of {len(rows)}"
          f"   -> green-rate {100.0 * on / max(1, on + off):.0f}%")


if __name__ == "__main__":
    main()
