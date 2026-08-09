"""Quantify RELEASE-TIMING error from a dense per-frame detection log.

Reads logs/diagnostics/detframes.csv (columns: t_ms, detected, fill_pct,
confidence, x, y, w, h, rejection_reason, green_center_pct, green_confidence,
frame_w, frame_h) and, per detected shot segment, measures the real meter
dynamics the native AutomationEngine has to time against:

  * the rise velocity near the top (%/ms) and how it changes (decel?),
  * the green-window center (where green_confidence is real),
  * the TIME the meter spends inside the last ~10% (the timing tolerance the
    release has to hit), and
  * a simulation of the engine's predictive release (paths 1/2) so we can see
    at what fill it WOULD fire and how far below the green that lands.

The point: show whether the release is firing too early (extrapolating a big
gap) and how tight the real green-arrival window is, so the lead / reachability
gate can be tuned with numbers instead of guesses.

    python tools/diagnostics/analyze_release_timing.py [csv_path]
"""
import csv
import statistics
import sys

FED = {"", "green_not_found"}
GAP_FRAMES = 8     # >=N consecutive non-detected frames ends a shot
MIN_SHOT = 5
GREEN_CONF_MIN = 0.12   # green_center_pct is only meaningful above this


def velocity_pct_per_ms(seg, lo=None, hi=None):
    """EWLS slope over fed samples, optionally restricted to a fill band [lo,hi]
    (so we measure the RISE, not the top plateau)."""
    pts = [(float(r["t_ms"]), float(r["fill_pct"])) for r in seg
           if r["rejection_reason"] in FED and float(r["fill_pct"]) > 0
           and (lo is None or float(r["fill_pct"]) >= lo)
           and (hi is None or float(r["fill_pct"]) <= hi)]
    if lo is None and hi is None:
        pts = pts[-8:]
    if len(pts) < 2:
        return 0.0
    lam = 0.5
    n = len(pts)
    sw = tm = fm = 0.0
    for i, (t, f) in enumerate(pts):
        w = pow(2.718281828, -lam * (n - 1 - i))
        sw += w; tm += w * t; fm += w * f
    tm /= sw; fm /= sw
    num = den = 0.0
    for i, (t, f) in enumerate(pts):
        w = pow(2.718281828, -lam * (n - 1 - i))
        num += w * (t - tm) * (f - fm)
        den += w * (t - tm) * (t - tm)
    return num / den if abs(den) > 1e-9 else 0.0


def main(path):
    rows = list(csv.DictReader(open(path)))
    shots, cur, miss = [], [], 0
    for r in rows:
        if r["detected"] == "1":
            if miss >= GAP_FRAMES and cur:
                shots.append(cur); cur = []
            miss = 0
            cur.append(r)
        else:
            miss += 1
    if cur:
        shots.append(cur)
    # A GENUINE shot rises from low to the top. The static ~17-20% blobs and the
    # 30-69% contested dropouts are not useful for timing calibration, so keep only
    # shots that actually reached the top band.
    real = [s for s in shots if len(s) >= MIN_SHOT
            and max(float(r["fill_pct"]) for r in s) >= 90
            and (max(float(r["fill_pct"]) for r in s)
                 - min(float(r["fill_pct"]) for r in s)) >= 40]

    print(f"file: {path}")
    print(f"genuine rising shots (reach >=90%, span >=40%): {len(real)}\n")
    hdr = (f"{'#':>3} {'frm':>3} {'fill_rng':>9} {'gcen':>5} {'rise_v':>7} "
           f"{'80-100':>6} {'green_ms':>8} {'sim_rel':>7} {'lands':>6}")
    print(hdr)
    print("-" * len(hdr))

    rel_gaps, tol80, greenms, lands = [], [], [], []
    LEAD = 110.0   # current effective lead ~ chain+pipeline+offset (see .h)
    for i, s in enumerate(real, 1):
        fills = [float(r["fill_pct"]) for r in s]
        ts = [float(r["t_ms"]) for r in s]
        greens = [float(r["green_center_pct"]) for r in s
                  if float(r["green_confidence"]) >= GREEN_CONF_MIN
                  and float(r["green_center_pct"]) > 0]
        gcen = statistics.median(greens) if greens else None
        fmin, fmax = min(fills), max(fills)
        risev = velocity_pct_per_ms(s, lo=40, hi=92)   # %/ms during the rise

        def t_at(thr):
            return next((ts[k] for k in range(len(fills)) if fills[k] >= thr), None)
        t80, t97, t100 = t_at(80), t_at(97), t_at(99.0)
        dwell80 = (t100 - t80) if (t80 is not None and t100 is not None) else None
        # real time the meter is inside a 3%-wide green band ending at 100
        gwin = (t100 - t97) if (t97 is not None and t100 is not None) else None

        # simulate engine predictive release (path 2) at LEAD ms, then project
        # where the shot LANDS = fill at (release_time + true_latency). We don't
        # know the true latency; assume it equals LEAD (the engine's own model) to
        # see the engine's INTENDED landing, then also the gap below target.
        target = (gcen if gcen is not None else 99.0)
        sim_fill = sim_t = None
        for k in range(3, len(s)):
            v = velocity_pct_per_ms(s[max(0, k - 7):k + 1])
            f = fills[k]
            if v <= 0.001 or f >= target:
                continue
            if (target - f) <= (min(max(v * LEAD, 0.0), 45.0) + 12.0) and (target - f) / v <= LEAD:
                sim_fill, sim_t = f, ts[k]
                break
        gap = (target - sim_fill) if sim_fill is not None else None
        # where it lands if true latency == LEAD: fill at sim_t + LEAD
        land = None
        if sim_t is not None:
            land = next((fills[k] for k in range(len(ts)) if ts[k] >= sim_t + LEAD),
                        fmax)
        if gap is not None:
            rel_gaps.append(gap)
        if dwell80 is not None:
            tol80.append(dwell80)
        if gwin is not None:
            greenms.append(gwin)
        if land is not None and gcen is not None:
            lands.append(land - gcen)   # +late / -early vs green center

        print(f"{i:>3} {len(s):>3} {fmin:>4.0f}-{fmax:>3.0f}% "
              f"{(f'{gcen:.0f}' if gcen is not None else '  -'):>5} "
              f"{risev*1000:>5.0f}/s "
              f"{(f'{dwell80:.0f}' if dwell80 is not None else '-'):>6} "
              f"{(f'{gwin:.0f}ms' if gwin is not None else '-'):>8} "
              f"{(f'{sim_fill:.0f}%' if sim_fill is not None else '-'):>7} "
              f"{(f'{land:.0f}%' if land is not None else '-'):>6}")

    def stat(name, xs, unit=""):
        if xs:
            print(f"{name}: median {statistics.median(xs):.0f}{unit}  "
                  f"mean {statistics.mean(xs):.0f}{unit}  "
                  f"min {min(xs):.0f}{unit}  max {max(xs):.0f}{unit}  (n={len(xs)})")

    print()
    stat("rise velocity 40-92%        ", [v for v in [velocity_pct_per_ms(s, 40, 92) * 1000 for s in real] if v > 0], "/s")
    stat("80->100% dwell              ", tol80, "ms")
    stat("green-band (97-100) dwell   ", greenms, "ms")
    print("  ^ THIS is the timing tolerance. A lead error bigger than this misses green.")
    stat("sim release gap below target", rel_gaps, "%")
    stat("sim landing vs green center ", lands, "%")
    print("  ^ at lead==true-latency the engine intends to land ~here vs green center (- = early).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/diagnostics/detframes.csv")
