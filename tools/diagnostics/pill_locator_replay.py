"""Run the SHIPPED meter proposer over real 2K27 PILL (park/rec) frames and report,
per frame, whether it proposed a box and which gate refused it.

READ-ONLY.  Source = datasets/meter2k27_pill_park (774 full 1920x1080 frames from seven
owner park/rec clips, 661 with a ground-truth meter box in audit.csv, labelled 2026-08-30
by the green-dome colour probe and visually verified for the Pill detector retrain).

Proposers
  --proposer cv    meter_locator_cv.MeterContourLocator  (shipped, ORION_METER_PROPOSER=cv)
  --proposer yolo  meter_detector_yolo.MeterYoloLocator  (legacy ONNX, the 08-30 n3_pill net)
Both are driven through the shipped AsyncMeterLocator(sync=True) wrapper, exactly as the
reader drives them live, so the roaming ROI / gate-9 bridge / pair memories see real
frame-to-frame history.

  --scale 0.6667   run at 720p, the resolution the capture path actually delivers
  --armed          publish a press on player_anchor.ARM at each shot's onset, so the
                   anchored search, the onset window and the tip-less path are live
The per-frame refusal reason is the DELTA of the locator's own stats dict.

Usage:
  python tools/diagnostics/pill_locator_replay.py --proposer cv  --scale 0.6667
  python tools/diagnostics/pill_locator_replay.py --proposer yolo --scale 1.0
"""
from __future__ import annotations
import argparse, collections, csv, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, REPO)

import cv2                                      # noqa: E402
import numpy as np                              # noqa: E402

DS = os.path.join(REPO, "datasets", "meter2k27_pill_park")
OUT = os.path.join("D:", os.sep, "NexusVision", "pill_check")
FPS = 60.0

GATES = ("col_cands", "not_lone", "no_tip", "no_tip_lone", "shape_short", "shape_abstain",
         "shape_irregular", "tip_spill", "shape_bridged", "no_outline", "refused_outside",
         "expect_pending", "tipless_pending", "tipless_accept", "tipless_track",
         "anchor_hit", "anchor_patch_hit", "top_strip", "error", "hit")

SOFT = ("no_tip", "no_tip_lone", "shape_short", "shape_irregular", "tip_spill", "not_lone",
        "no_outline", "refused_outside", "expect_pending", "tipless_pending", "error")


def load_rows():
    with open(os.path.join(DS, "audit.csv"), newline="") as f:
        return list(csv.DictReader(f))


def shots(rows, cls="pos"):
    """Contiguous runs of frames of one class inside one clip = one meter appearance.

    `cls='neg'` walks the hard negatives (no meter within +/-30 frames of any dome
    evidence: park decor, white clothing, court lines) -- a proposal there is a FALSE LOCK.
    """
    byclip = collections.defaultdict(list)
    for r in rows:
        if r["cls"] == cls:
            byclip[r["clip"]].append(r)
    out = []
    for clip, rs in sorted(byclip.items()):
        rs.sort(key=lambda r: int(r["idx"]))
        run = [rs[0]]
        for r in rs[1:]:
            if int(r["idx"]) - int(run[-1]["idx"]) <= 3:
                run.append(r)
            else:
                out.append((clip, run))
                run = [r]
        out.append((clip, run))
    return out


def iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


def build(proposer, extra_env):
    for k, v in extra_env.items():
        os.environ[k] = str(v)
    os.environ["ORION_METER_PROPOSER"] = proposer
    import importlib
    import meter_detector_yolo as mdy
    importlib.reload(mdy)
    if proposer == "cv":
        import meter_locator_cv as mlc
        importlib.reload(mlc)
        base = mlc.MeterContourLocator()
    else:
        conf = float(os.environ.get("ORION_METER_DETECTOR_CONF", "0.35"))
        base = mdy.MeterYoloLocator(conf_thres=conf)
    loc = mdy.AsyncMeterLocator(base=base, sync=True)
    return base, loc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposer", default="cv", choices=("cv", "yolo"))
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--armed", action="store_true")
    ap.add_argument("--shots", type=int, default=0, help="limit to N shots (0 = all)")
    ap.add_argument("--sheet", default="", help="contact-sheet filename under the out dir")
    ap.add_argument("--env", default="", help="extra KEY=VAL,KEY=VAL for the locator")
    ap.add_argument("--csv", default="", help="per-frame csv name under the out dir")
    ap.add_argument("--cls", default="pos", choices=("pos", "neg"),
                    help="pos = frames with a GT meter (recall); neg = hard negatives "
                         "(any proposal is a FALSE LOCK)")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    extra = {}
    for kv in a.env.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            extra[k.strip()] = v.strip()
    base, loc = build(a.proposer, extra)
    have_stats = hasattr(base, "stats")
    import player_anchor as pa

    rows = load_rows()
    sh = shots(rows, a.cls)
    if a.shots:
        sh = sh[: a.shots]
    tot = hits = 0
    reasons = collections.Counter()
    ious = []
    per_shot = []
    sheet_tiles = []
    csv_rows = []
    for si, (clip, run) in enumerate(sh):
        if hasattr(base, "reset"):
            base.reset()
        try:
            pa.ANCHOR.reset()
        except Exception:
            pass
        first_ts = int(run[0]["idx"]) / FPS
        if a.armed:
            pa.ARM.note_press(si + 1, first_ts - 0.25, "Standstill")
        prev = dict(base.stats) if have_stats else {}
        n_hit = 0
        first_hit_i = None
        for r in run:
            idx = int(r["idx"])
            p = os.path.join(DS, "images", r["split"], "%s_%05d.png" % (clip, idx))
            im = cv2.imread(p)
            if im is None:
                continue
            gt = ([float(r[k]) for k in ("x0", "y0", "x1", "y1")]
                  if r.get("x0") else [0.0, 0.0, 0.0, 0.0])
            if abs(a.scale - 1.0) > 1e-6:
                im = cv2.resize(im, None, fx=a.scale, fy=a.scale, interpolation=cv2.INTER_AREA)
                gt = [v * a.scale for v in gt]
            ts = idx / FPS
            loc.submit(im, ts)
            found, box, conf, _ = loc.latest()
            d = {}
            if have_stats:
                d = {k: base.stats[k] - prev.get(k, 0) for k in base.stats
                     if base.stats[k] - prev.get(k, 0)}
                prev = dict(base.stats)
            why = ",".join("%s=%s" % (k, v) for k, v in sorted(d.items()) if k in GATES) or "-"
            tot += 1
            ov = 0.0
            if found and box:
                bx, by, bw, bh = box[:4]
                ov = iou((bx, by, bx + bw, by + bh), tuple(gt))
                ious.append(ov)
                hits += 1
                n_hit += 1
                if first_hit_i is None:
                    first_hit_i = idx
            else:
                for k in d:
                    if k in SOFT:
                        reasons[k] += 1
                if not d or all(k in ("calls", "full", "roi", "col_cands", "idle_reuse")
                                for k in d):
                    reasons["no_candidate(gates 1-4)"] += 1
            csv_rows.append({"clip": clip, "idx": idx, "shot": si + 1,
                             "found": int(bool(found)), "conf": round(float(conf or 0), 3),
                             "iou": round(ov, 3), "why": why})
            if a.sheet and len(sheet_tiles) < 14 and (idx - int(run[0]["idx"])) % 14 == 0:
                vis = im.copy()
                cv2.rectangle(vis, (int(gt[0]), int(gt[1])), (int(gt[2]), int(gt[3])),
                              (0, 255, 255), 1)
                if found and box:
                    bx, by, bw, bh = [int(v) for v in box[:4]]
                    cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), (0, 0, 255), 1)
                cx = int((gt[0] + gt[2]) / 2)
                cy = int((gt[1] + gt[3]) / 2)
                hw, hh = int(90 * a.scale), int(130 * a.scale)
                x0 = max(0, cx - hw)
                y0 = max(0, cy - hh)
                t = vis[y0:cy + hh, x0:cx + hw]
                if t.size:
                    t = cv2.resize(t, (180, 260), interpolation=cv2.INTER_NEAREST)
                    cv2.putText(t, "%s:%d" % (clip[:6], idx), (3, 13), 0, 0.38, (0, 255, 255), 1)
                    cv2.putText(t, ("HIT" if found else "miss"), (3, 253), 0, 0.42,
                                (0, 255, 0) if found else (0, 0, 255), 1)
                    sheet_tiles.append(t)
        per_shot.append((clip, int(run[0]["idx"]), len(run), n_hit, first_hit_i))

    print("")
    print("=== proposer=%s scale=%s armed=%s env=%s" % (a.proposer, a.scale, a.armed, extra or "-"))
    label = ("frames with a GT meter" if a.cls == "pos"
             else "HARD NEGATIVE frames (no meter)")
    print("%s: %d   PROPOSED: %d (%.1f%%)%s"
          % (label, tot, hits, 100.0 * hits / max(1, tot),
             "  <-- FALSE LOCKS" if a.cls == "neg" and hits else ""))
    if ious:
        print("IoU vs GT box: p50=%.2f p10=%.2f min=%.2f" %
              (np.median(ious), np.percentile(ious, 10), min(ious)))
    print("refusals (per frame, may be several per frame):")
    for k, v in reasons.most_common():
        print("   %-28s %d" % (k, v))
    if have_stats:
        print("locator stats totals:", {k: v for k, v in base.stats.items() if v})
    print("%-10s %6s %6s %5s  first_hit" % ("clip", "start", "frames", "hits"))
    for clip, st, n, nh, fh in per_shot:
        print("%-10s %6d %6d %5d  %s" % (clip, st, n, nh, fh if fh is not None else "-"))
    if a.csv and csv_rows:
        with open(os.path.join(OUT, a.csv), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)
        print("csv ->", os.path.join(OUT, a.csv))
    if a.sheet and sheet_tiles:
        cv2.imwrite(os.path.join(OUT, a.sheet), np.hstack(sheet_tiles))
        print("sheet ->", os.path.join(OUT, a.sheet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
