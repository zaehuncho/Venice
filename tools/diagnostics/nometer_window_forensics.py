"""READ-ONLY forensics for press windows whose meter was never sighted.

Answers, per epoch, from the 2026-09-17 press-window frame dump:
  1. was a meter DRAWN on screen anywhere in the window?
  2. if drawn, why did MeterContourLocator not propose it?
  3. if not drawn, what animation was it?

Nothing here writes to the repo or touches the running app: it reads the dump,
the shot records and frames.csv only, and writes to D:/NexusVision/nometer_check.
"""
from __future__ import annotations
import csv, json, os, re, sys
from collections import Counter

DUMP = r"D:\NexusVision\framedump"
OUT  = r"D:/NexusVision/nometer_check"
REC  = r"D:\NexusVision\shot_records\session_20260917_030758.jsonl"

os.makedirs(OUT, exist_ok=True)


def load_records(path=REC):
    return {r["epoch"]: r for r in
            (json.loads(l) for l in open(path, encoding="utf-8-sig") if l.strip())}


def load_frames_csv(recs):
    """frames.csv is APPENDED across sessions and idx restarts, so rows are keyed by
    wall clock and clipped to this session's own press span."""
    rows = list(csv.DictReader(open(os.path.join(DUMP, "frames.csv"))))
    lo = min(r["press_ts_ms"] for r in recs.values()) / 1000.0 - 5.0
    hi = max(r["press_ts_ms"] for r in recs.values()) / 1000.0 + 10.0
    out = {}
    for r in rows:
        try:
            tw = float(r["t_wall"])
        except Exception:
            continue
        if lo <= tw <= hi:
            out[int(r["idx"])] = r
    return out


def frame_paths(epoch, lo, hi, kind="raw"):
    """The dump dir is SHARED across sessions (epoch numbers restart), so a file only
    belongs to this window if its frame index is inside the window's own idx span."""
    pat = re.compile(r"^ep%d_f(\d{6})_\d+_%s\.jpg$" % (epoch, kind))
    got = []
    for name in os.listdir(DUMP):
        m = pat.match(name)
        if m:
            i = int(m.group(1))
            if lo <= i <= hi:
                got.append((i, os.path.join(DUMP, name)))
    return sorted(got)


if __name__ == "__main__":
    recs = load_records()
    fr = load_frames_csv(recs)
    targets = [int(a) for a in sys.argv[1:]] or [7, 35, 50]
    for ep in targets:
        r = recs[ep]
        fd = r["framedump"]
        lo, hi = fd["first_idx"], fd["last_idx"]
        files = frame_paths(ep, lo, hi)
        press = r["press_ts_ms"] / 1000.0
        rel = r.get("release_after_press_ms")
        print(f"\n=== epoch {ep}  {r['shot_type']}  hold={rel}  onset_ms={r.get('onset_ms')} "
              f"onset_fill={r.get('onset_fill')}  outcome={r.get('outcome')}")
        print(f"    idx {lo}..{hi} declared frames={fd['frames']} dropped={fd['dropped']} "
              f"files_on_disk={len(files)}")
        rej = Counter()
        prev_t = None
        gaps = []
        for i, _p in files:
            row = fr.get(i)
            if row is None:
                rej["<missing_from_frames.csv>"] += 1
                continue
            t = (float(row["t_wall"]) - press) * 1000.0
            if prev_t is not None and t - prev_t > 20.0:
                gaps.append((prev_t, t))
            prev_t = t
            rej[row["rejection"] or "<accepted>"] += 1
        print("    rejection census:", dict(rej))
        if gaps:
            print("    gaps >20ms:", [f"{a:.0f}->{b:.0f}" for a, b in gaps])
        det = [(i, fr[i]) for i, _ in files if i in fr and fr[i]["detected"] == "1"]
        if det:
            i0, row0 = det[0]
            t0 = (float(row0["t_wall"]) - press) * 1000.0
            print(f"    first detected frame idx={i0} t=+{t0:.0f}ms fill={row0['fill_pct']} "
                  f"box=({row0['bbox_x']},{row0['bbox_y']},{row0['bbox_w']},{row0['bbox_h']})")
            print(f"    detected frames: {len(det)} of {len(files)}")
        else:
            print("    NO frame in this window was ever detected by the live reader")
