#!/usr/bin/env python3
"""Go-To vs other-type shot census over orion_native logs (2026-08-04..06).

Reads logs/orion_native.log{,.1} and answers, per shot type and per anchor
regime (base30 band 240..430 vs base20 band 298..488):

  * attempts vs releases vs aborts (deadline-missed / ownership faults, with
    the ownership faults split into sub-attempt stick flicks vs real waits)
  * PHASE SAMPLE normalized_ms distribution (n / median / mean / sd)
  * ladder-rung dating distribution (PHASE SAMPLE anchor_pct)
  * release attribution targetMode (phase vs sampler chain)
  * first-FED-fill proxy (Release detsummary minFreshFill) + acquisition lag
    (firstMeterMs - firstFreshMs is not directly logged; we report both)
  * deadline-miss joins: each live_tip_deadline_missed abort joined backward
    to its TIP DEADLINE DECISION line (source / lateness / fill / frame age /
    reservation_first_fill)

Read-only. No engine state is touched. The hand-counted TIMING banner remains
the only outcome instrument; everything here is mechanism telemetry.
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2}\.\d{3})Z\s+(.*)$")

def parse_ts(date: str, tod: str) -> float:
    return datetime.strptime(f"{date}T{tod}", "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp()

def kv(line: str, key: str, cast=float, default=None):
    m = re.search(rf"{re.escape(key)}=([^\s]+)", line)
    if not m:
        return default
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return default

def shot_tail(line: str, key: str):
    m = re.search(rf"{re.escape(key)}=(.+?)\s*$", line)
    if not m:
        return None
    return m.group(1).strip().replace(" ", "_")

def med(xs):
    return statistics.median(xs) if xs else float("nan")

def sd(xs):
    return statistics.stdev(xs) if len(xs) >= 2 else float("nan")

def pctl(xs, q):
    if not xs:
        return float("nan")
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(math.ceil(q * len(ys)) - 1)))
    return ys[idx]

class Census:
    def __init__(self, t0: float, t1: float):
        self.t0, self.t1 = t0, t1
        self.releases = []          # dicts
        self.deadline_aborts = []   # dicts (joined)
        self.own_aborts = []        # dicts
        self.other_aborts = []
        self.phase_samples = []     # dicts
        self.last_decision = None   # last rejected_missed TIP DEADLINE DECISION
        self.last_not_owned = None
        self.last_reservation = None
        self.pending_rel = {}       # seq -> release dict (same log stretch)
        self.regime = "base30"
        self.imminent_holds = []

    def feed(self, ts: float, body: str):
        if not (self.t0 <= ts <= self.t1):
            return
        if body.startswith("PHASE SAMPLE:"):
            band = re.search(r"band_ms=(\d+)\.\.(\d+)", body)
            regime = "base20" if band and band.group(1) == "298" else "base30"
            self.regime = regime
            self.phase_samples.append(dict(
                ts=ts, regime=regime,
                raw=kv(body, "raw_ms"), norm=kv(body, "normalized_ms"),
                anchor=kv(body, "anchor_pct"), accepted=kv(body, "accepted", int),
                shot=shot_tail(body, "shot_type")))
        elif body.startswith("TIP DEADLINE DECISION:") and "rejected_missed" in body:
            self.last_decision = dict(
                ts=ts, source=kv(body, "source", str),
                lateness=kv(body, "lateness_ms"), fill=kv(body, "fill_pct"),
                frame_age=kv(body, "frame_age_ms"),
                first_fill=kv(body, "reservation_first_fill"),
                first_eta=kv(body, "reservation_first_tip_eta_ms"),
                res_age=kv(body, "reservation_age_ms"),
                ever_armable=kv(body, "reservation_ever_armable", int))
        elif body.startswith("TIP RESERVATION:") and "source=" in body:
            self.last_reservation = dict(
                ts=ts, source=kv(body, "source", str),
                tip_eta=kv(body, "tip_eta_ms"),
                phase_tip_eta=kv(body, "phase_tip_eta_ms"),
                phase_primary=kv(body, "phase_primary", int),
                phase_anchor_pct=kv(body, "phase_anchor_pct"),
                smp_tip_eta=kv(body, "smp_tip_eta_ms"),
                smp_sigma=kv(body, "smp_sigma_ms"),
                lead=kv(body, "lead_ms"),
                fill=kv(body, "fill_pct"))
        elif body.startswith("SHOT NOT OWNED:"):
            self.last_not_owned = dict(
                ts=ts, reason=kv(body, "reason", str),
                samples=kv(body, "samples", int),
                first_fill=kv(body, "first_fill"),
                last_fill=kv(body, "last_fill"),
                wait=kv(body, "wait_ms"))
        elif body.startswith("TIP PHASE IMMINENT HOLD:"):
            self.imminent_holds.append(dict(ts=ts, fill=kv(body, "fill_pct"),
                                            anchor=kv(body, "anchor_pct")))
        elif body.startswith("Release issued:"):
            seq = kv(body, "seq", int)
            m = re.search(r"fill ([0-9.]+)% ", body)
            rel = dict(ts=ts, seq=seq, shot=shot_tail(body, "shot"),
                       fill=float(m.group(1)) if m else None,
                       age=kv(body, "age", lambda s: float(s.rstrip("ms"))),
                       reason=kv(body, "reason", str),
                       code=kv(body, "code", str), regime=self.regime,
                       target_mode=None, min_fresh_fill=None,
                       first_fresh_ms=None, first_meter_ms=None,
                       appear_to_rel=None, anchor_appear=None,
                       fresh=None, stale_mem=None, nodet=None)
            self.releases.append(rel)
            if seq is not None:
                self.pending_rel[seq] = rel
        elif body.startswith("Release attribution:"):
            rel = self.pending_rel.get(kv(body, "seq", int))
            if rel and abs(rel["ts"] - ts) < 5:
                rel["target_mode"] = kv(body, "targetMode", str)
        elif body.startswith("Release detsummary:"):
            rel = self.pending_rel.get(kv(body, "seq", int))
            if rel and abs(rel["ts"] - ts) < 5:
                rel["min_fresh_fill"] = kv(body, "minFreshFill")
                rel["first_fresh_ms"] = kv(body, "firstFreshMs")
                rel["first_meter_ms"] = kv(body, "firstMeterMs")
                rel["fresh"] = kv(body, "fresh", int)
                rel["stale_mem"] = kv(body, "staleMem", int)
                rel["nodet"] = kv(body, "nodet", int)
        elif body.startswith("Release timing:"):
            rel = self.pending_rel.get(kv(body, "seq", int))
            if rel and abs(rel["ts"] - ts) < 5:
                rel["appear_to_rel"] = kv(body, "appearToRelMs")
                rel["anchor_appear"] = kv(body, "anchorAppearMs")
        elif body.startswith("Shot abort identity:"):
            reason = kv(body, "reason", str)
            shot = shot_tail(body, "shot_type")
            rec = dict(ts=ts, reason=reason, shot=shot,
                       site=kv(body, "site", str), regime=self.regime)
            if reason == "live_tip_deadline_missed":
                d = self.last_decision
                if d and ts - d["ts"] < 5.0:
                    rec.update(d)
                    rec["joined"] = True
                else:
                    rec["joined"] = False
                r = self.last_reservation
                if r and ts - r["ts"] < 1.5:
                    rec["res_dt_ms"] = (ts - r["ts"]) * 1000.0
                    rec["res_source"] = r["source"]
                    rec["phase_tip_eta"] = r["phase_tip_eta"]
                    rec["phase_anchor_pct"] = r["phase_anchor_pct"]
                    rec["phase_primary"] = r["phase_primary"]
                    rec["smp_tip_eta"] = r["smp_tip_eta"]
                    rec["lead"] = r["lead"]
                self.deadline_aborts.append(rec)
            elif reason in ("ownership_proof_incomplete",
                            "ownership_blocked_stale_meter"):
                n = self.last_not_owned
                if n and ts - n["ts"] < 2.0:
                    rec.update({k: n[k] for k in
                                ("samples", "first_fill", "last_fill", "wait")})
                self.own_aborts.append(rec)
            else:
                self.other_aborts.append(rec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", nargs="*", default=None)
    ap.add_argument("--since", default="2026-08-04T00:00:00.000")
    ap.add_argument("--until", default="2026-08-07T00:00:00.000")
    ap.add_argument("--sessions", action="store_true",
                    help="print per-session Go-To/No_Dip detail")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    logs = args.logs or [root / "logs" / "orion_native.log.1",
                         root / "logs" / "orion_native.log"]
    t0 = datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp()
    t1 = datetime.strptime(args.until, "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp()

    c = Census(t0, t1)
    for path in logs:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = TS_RE.match(line)
                if not m:
                    continue
                c.feed(parse_ts(m.group(1), m.group(2)), m.group(3))

    types = sorted({r["shot"] for r in c.releases if r["shot"]}
                   | {a["shot"] for a in c.deadline_aborts if a["shot"]}
                   | {a["shot"] for a in c.own_aborts if a["shot"]})

    print(f"window: {args.since}Z .. {args.until}Z   logs: "
          f"{', '.join(str(p) for p in logs)}")
    print()
    hdr = (f"{'type':<12} {'rel':>4} {'ddl_miss':>8} {'own_flick':>9} "
           f"{'own_real':>8} {'other':>5} {'abort%':>7}")
    print(hdr)
    print("-" * len(hdr))
    FLICK_MS = 200.0
    for t in types:
        rel = [r for r in c.releases if r["shot"] == t]
        ddl = [a for a in c.deadline_aborts if a["shot"] == t]
        own = [a for a in c.own_aborts if a["shot"] == t]
        flick = [a for a in own if (a.get("wait") or 0) < FLICK_MS
                 and (a.get("samples") or 0) == 0]
        real = [a for a in own if a not in flick]
        other = [a for a in c.other_aborts if a["shot"] == t]
        attempts = len(rel) + len(ddl) + len(real) + len(other)
        ab = len(ddl) + len(real) + len(other)
        pct = 100.0 * ab / attempts if attempts else float("nan")
        print(f"{t:<12} {len(rel):>4} {len(ddl):>8} {len(flick):>9} "
              f"{len(real):>8} {len(other):>5} {pct:>6.1f}%")
    print("\n(attempts = releases + deadline-missed + real ownership faults + "
          f"other; stick flicks < {FLICK_MS:.0f}ms with 0 samples excluded)")

    print("\n=== PHASE SAMPLE normalized_ms by type and regime ===")
    for regime in ("base30", "base20"):
        for t in types:
            xs = [p["norm"] for p in c.phase_samples
                  if p["shot"] == t and p["regime"] == regime
                  and p["accepted"] == 1]
            if not xs:
                continue
            print(f"{regime}  {t:<12} n={len(xs):>3}  med={med(xs):7.1f}  "
                  f"mean={statistics.mean(xs):7.1f}  sd={sd(xs):5.1f}")

    print("\n=== ladder rung (PHASE SAMPLE anchor_pct) by type/regime ===")
    for regime in ("base30", "base20"):
        for t in types:
            rungs = Counter(p["anchor"] for p in c.phase_samples
                            if p["shot"] == t and p["regime"] == regime)
            if rungs:
                s = "  ".join(f"{k:.0f}:{v}" for k, v in sorted(rungs.items()))
                print(f"{regime}  {t:<12} {s}")

    print("\n=== release attribution targetMode by type ===")
    for t in types:
        modes = Counter(r["target_mode"] or "?" for r in c.releases
                        if r["shot"] == t)
        print(f"{t:<12} " + "  ".join(f"{k}:{v}" for k, v in
                                      sorted(modes.items())))

    print("\n=== release without a PHASE SAMPLE within 6s (undated or "
          "no confirmed stop) ===")
    for t in types:
        rel = [r for r in c.releases if r["shot"] == t]
        n_miss = 0
        for r in rel:
            if not any(p["shot"] == t and 0 <= p["ts"] - r["ts"] < 6.0
                       for p in c.phase_samples):
                n_miss += 1
        if rel:
            print(f"{t:<12} {n_miss}/{len(rel)}")

    print("\n=== first-FED fill proxy (Release detsummary minFreshFill) ===")
    for t in types:
        xs = [r["min_fresh_fill"] for r in c.releases
              if r["shot"] == t and r["min_fresh_fill"] is not None]
        if xs:
            print(f"{t:<12} n={len(xs):>3}  med={med(xs):5.1f}  "
                  f"p90={pctl(xs, 0.9):5.1f}  max={max(xs):5.1f}")

    print("\n=== appear->release / detection census on releases ===")
    for t in types:
        ar = [r["appear_to_rel"] for r in c.releases
              if r["shot"] == t and r["appear_to_rel"] is not None]
        fresh = [r["fresh"] for r in c.releases
                 if r["shot"] == t and r["fresh"] is not None]
        nodet = [r["nodet"] for r in c.releases
                 if r["shot"] == t and r["nodet"] is not None]
        if ar:
            print(f"{t:<12} appearToRel med={med(ar):6.1f}  "
                  f"fresh med={med(fresh):4.0f}  nodet med={med(nodet):4.0f}")

    print("\n=== deadline-missed aborts, joined decision detail ===")
    for t in types:
        ddl = [a for a in c.deadline_aborts if a["shot"] == t]
        if not ddl:
            continue
        srcs = Counter(a.get("source") or "unjoined" for a in ddl)
        lat = [a["lateness"] for a in ddl if a.get("lateness") is not None]
        ff = [a["first_fill"] for a in ddl if a.get("first_fill") is not None]
        fa = [a["frame_age"] for a in ddl if a.get("frame_age") is not None]
        print(f"{t:<12} n={len(ddl)}  src={dict(srcs)}")
        if lat:
            print(f"{'':<12} lateness med={med(lat):6.1f} "
                  f"min={min(lat):6.1f} max={max(lat):6.1f}   "
                  f"res_first_fill med={med(ff):5.1f}   "
                  f"frame_age med={med(fa):5.1f}")

    print("\n=== deadline-miss anatomy: was a phase estimate present and "
          "schedulable? ===")
    print("classes: PHASE_SCHEDULABLE = phase member dated + tip_eta-lead > 0 "
          "(veto/selection handed the shot to the sampler);")
    print("         PHASE_LATE = phase dated but its own eta was past;  "
          "UNDATED = no phase anchor (phase_tip_eta=-1);  NOJOIN = no "
          "reservation line")
    for t in types:
        ddl = [a for a in c.deadline_aborts if a["shot"] == t]
        if not ddl:
            continue
        cls = Counter()
        det = []
        for a in ddl:
            pte = a.get("phase_tip_eta")
            lead = a.get("lead") or 300.0
            if pte is None:
                cls["NOJOIN"] += 1
            elif pte < 0:
                cls["UNDATED"] += 1
            elif pte - lead > 0:
                cls["PHASE_SCHEDULABLE"] += 1
                det.append((a["ts"], a.get("source"), a.get("lateness"),
                            pte - lead, a.get("phase_anchor_pct"),
                            a.get("regime"), a.get("res_dt_ms")))
            else:
                cls["PHASE_LATE"] += 1
        print(f"{t:<12} {dict(cls)}")
        for ts_, src, late, margin, rung, regime, res_dt in det:
            stamp = datetime.fromtimestamp(ts_, tz=timezone.utc)
            fresh = "SAME-TICK" if res_dt is not None and res_dt < 60 else \
                f"stale({res_dt:.0f}ms)" if res_dt is not None else "?"
            print(f"    {stamp:%m-%d %H:%M:%S} {regime} src={src} "
                  f"lateness={late} phase_cmd_eta=+{margin:.1f}ms "
                  f"rung={rung} res={fresh}")

    print("\n=== ownership faults (real, wait >= 200ms or samples > 0) ===")
    for t in types:
        own = [a for a in c.own_aborts if a["shot"] == t
               and not ((a.get("wait") or 0) < FLICK_MS
                        and (a.get("samples") or 0) == 0)]
        if not own:
            continue
        ff = [a["first_fill"] for a in own if a.get("first_fill") is not None]
        w = [a["wait"] for a in own if a.get("wait") is not None]
        s = [a.get("samples") or 0 for a in own]
        print(f"{t:<12} n={len(own)}  first_fill med={med(ff):5.1f} "
              f"wait med={med(w):7.1f}  samples med={med(s):3.1f}")

    if args.sessions:
        print("\n=== per-event Go-To / No_Dip detail ===")
        evs = []
        for r in c.releases:
            if r["shot"] in ("Go-To", "No_Dip"):
                evs.append((r["ts"], "REL", r))
        for a in c.deadline_aborts + c.own_aborts + c.other_aborts:
            if a["shot"] in ("Go-To", "No_Dip"):
                evs.append((a["ts"], "ABORT", a))
        for ts, kind, e in sorted(evs):
            stamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            if kind == "REL":
                print(f"{stamp:%m-%d %H:%M:%S} REL   {e['shot']:<7} "
                      f"{e['regime']:<6} tm={e['target_mode'] or '?':<28} "
                      f"minFF={e['min_fresh_fill'] if e['min_fresh_fill'] is not None else '?':>6} "
                      f"a2r={e['appear_to_rel'] if e['appear_to_rel'] is not None else '?'}")
            else:
                print(f"{stamp:%m-%d %H:%M:%S} ABORT {e['shot']:<7} "
                      f"{e['regime']:<6} {e['reason']:<28} "
                      f"src={e.get('source') or '-':<10} "
                      f"late={e.get('lateness') if e.get('lateness') is not None else '-':>6} "
                      f"ff={e.get('first_fill') if e.get('first_fill') is not None else '-':>6} "
                      f"wait={e.get('wait') if e.get('wait') is not None else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
