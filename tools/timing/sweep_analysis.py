#!/usr/bin/env python
"""Offline, identity-joined offset-sweep analysis. Never controls or launches Orion.

Python 3.10+, NumPy, SciPy. Missing assignments are UNKNOWN, never arm 0 / offset 0.
Run --dry-run on legacy records; a failed fidelity gate suppresses the primary fit.
See docs/variance/PREREGISTRATION_OFFSET_SWEEP.md for the frozen estimands.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import numpy as np
from scipy import optimize, special, stats

VERSION = "2026-09-22-predata-1"
SEED = 20260922
OFFLINE_SESSION = "session_20260920_170027"
GRADES = {"EARLY": 0, "EXCELLENT": 1, "LATE": 2}
OPEN = {"OPEN", "WIDE OPEN"}
CURVE_FILL = np.array([20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100.])
CURVE_MS = np.array([0, 29.42, 58.30, 86.22, 112.58, 163.7, 211.4, 257.4, 302.4, 344.4, 385.5])
TOP_RATE = 10 / (385.5 - 344.4)  # 90..100 is extrapolated in the source, not observed per shot.
KV = re.compile(r"\b([A-Za-z_]\w*)=([^\s]+)")
TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z)\s+(.*)$")
GREEN = re.compile(r"\(green\s+([\d.]+)-([\d.]+)\)")
TAGS = {
    "Physical shot epoch:": "press", "Release issued:": "issued",
    "Release delivery identity:": "identity", "Release onsetff:": "release_ff",
    "Release devoffset:": "release_dev", "Release attribution:": "attribution",
    "Release timing:": "timing", "Scheduled fire:": "schedule",
    "Release landing:": "landing", "DEV FIRE OFFSET DRAW:": "draw",
    "DEV FIRE OFFSET APPLIED:": "dev_refinement", "ONSET FF: reason=": "ff",
    "TIP RESERVATION:": "reservation", "Shot abort identity:": "abort",
    "DEV FIRE OFFSET SWEEP ARMED:": "hook", "TIP PHASE RATE STRETCH:": "stretch",
}


def number(x):
    if isinstance(x,bool):
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def ident(x):
    # Identity must never travel through a float (uint64 physical epochs).
    if isinstance(x, bool) or not re.fullmatch(r"[0-9]+", str(x)):
        return None
    v = int(x)
    return v if 0 < v < 2**64 else None


def utc_ms(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("UTC/offset-aware timestamp required")
    return d.timestamp() * 1000


def distribution(values):
    a = np.array([v for x in values if (v := number(x)) is not None])
    if not len(a):
        return {"n": 0}
    return dict(n=len(a), mean=float(a.mean()), sd=float(a.std(ddof=1)) if len(a)>1 else None,
                **{k: float(np.quantile(a, q)) for k, q in
                   [("min", 0), ("p10", .1), ("p25", .25), ("median", .5),
                    ("p75", .75), ("p90", .9), ("p95", .95), ("max", 1)]})


def coverage_state(x):
    s = str(x or "").upper().replace("_", " ").strip()
    if s in OPEN:
        return "open"
    if any(w in s for w in ("CONTEST", "COVERED", "SMOTHER", "TIGHT", "GUARD")):
        return "contested"
    return "unknown"


def curve_time(f):
    if not math.isfinite(f):
        return float("nan")
    if f < 20:
        return (f-20)/.158
    if f > 100:
        return 385.5+(f-100)/TOP_RATE
    return float(np.interp(f, CURVE_FILL, CURVE_MS))


def curve_window(start, end, scale=1.):
    if start is None or end is None or not (0 <= start < end <= 100):
        return None
    return scale*(curve_time(end)-curve_time(start))


def load_records(paths):
    records, conflicting, counts = {}, set(), Counter()
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            counts["lines"] += 1
            try:
                r = json.loads(line)
            except ValueError:
                counts["invalid_json"] += 1
                continue
            if not isinstance(r,dict):
                counts["invalid_json"] += 1
                continue
            epoch = ident(r.get("epoch")); session = r.get("session")
            start, end = number(r.get("press_ts_ms")), number(r.get("closed_ts_ms"))
            if epoch is None or not isinstance(session, str) or not session or start is None or end is None or start < 0 or end < start:
                counts["invalid_identity_or_interval"] += 1
                continue
            key = (session, epoch)
            if key in records:
                if records[key] != r:
                    conflicting.add(key); counts["conflicting_duplicates"] += 1
                else:
                    counts["identical_duplicates"] += 1
            else:
                records[key] = r
    for k in conflicting:
        records.pop(k, None)
    return sorted(records.values(), key=lambda r: (r["press_ts_ms"], r["session"], r["epoch"])), dict(counts)


def load_events(paths):
    events, seen, counts = [], set(), Counter()
    for p in paths:
        for line_no, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "Sidecar tail:" in line:
                continue
            m = TS.match(line)
            if not m:
                continue
            body = m.group(2)
            stage = next((v for k, v in TAGS.items() if body.startswith(k)), None)
            if stage is None:
                continue
            key = (m.group(1), body)
            if key in seen:
                counts["identical_forwarded_lines"] += 1
                continue
            seen.add(key)
            fields = dict(KV.findall(body))
            if stage == "issued" and (g := GREEN.search(body)):
                fields.update(issued_green_start=g.group(1), issued_green_end=g.group(2))
            events.append(dict(t=utc_ms(m.group(1)), stage=stage, f=fields,
                               file=str(p.resolve()), line=line_no, order=len(events)))
    events.sort(key=lambda e: (e["t"], e["order"]))
    # Reset boundaries come from physical epoch/release-counter resets, not 600 s idle gaps.
    boundaries=[]; last_ep=None; last_seq=None; last_press=None; last_release=None
    for e in events:
        if e["stage"] == "press":
            ep=ident(e["f"].get("epoch"))
            if ep is not None and last_ep is not None and ep <= last_ep:
                boundaries.append(e["t"]); last_seq=None
            last_ep=ep; last_press=e["t"]
        elif e["stage"] == "issued":
            seq=ident(e["f"].get("seq"))
            if seq is not None and last_seq is not None and seq <= last_seq:
                boundaries.append(last_press if last_press is not None and last_press > last_release else e["t"]-150)
            last_seq=seq; last_release=e["t"]
    boundaries=sorted(set(boundaries))
    for e in events:
        e["process"] = bisect_right(boundaries, e["t"])
    counts.update(Counter(e["stage"] for e in events))
    counts["process_partitions"]=len(boundaries)+1
    return events, dict(counts)


def unique_fields(events):
    unique={tuple(sorted(e["f"].items())) for e in events}
    return dict(next(iter(unique))) if len(unique)==1 else None


def join_shots(records, events, scale=1.):
    rows=[]; by_epoch=defaultdict(list); stages=defaultdict(list)
    for e in events:
        stages[e["stage"]].append(e)
    for r in records:
        b=r.get("banner") or {}; typ=r.get("shot_type_upgraded") or r.get("shot_type")
        row=dict(session=r["session"], epoch=int(r["epoch"]), record_seq=r.get("seq"),
                 press_ms=number(r["press_ts_ms"]), closed_ms=number(r["closed_ts_ms"]),
                 release_ms=number(r.get("release_ms")), hold_ms=number(r.get("release_after_press_ms")),
                 onset_ms=number(r.get("onset_ms")), onset_source=r.get("onset_source"),
                 shot_type=typ, outcome=r.get("outcome"), grade=b.get("timing"),
                 coverage=b.get("coverage"), coverage_state=coverage_state(b.get("coverage")),
                 distance_ft=number(b.get("distance_ft")), online=r["session"]!=OFFLINE_SESSION,
                 release_seq=None, attempt=None, process=None, join_source=None, join_conflicts=[],
                 A=None, Z_first=None, Z_final=None, D=None, F=None, scheduled=None,
                 ff_n=None, onset_dev=0., reference_unavailable=1, draw_count=0,
                 arm_conflict=False, pre_width_pp=None, pre_width_ms=None, landing_width_pp=None,
                 landing_width_ms=None, confirm_lead_ms=None, pre_confirmed=False,
                 identity_candidates=[], ff_events=[], attempt_events=[])
        row["clock_failure"]=False
        if row["outcome"]=="released":
            t=row["release_ms"];h=row["hold_ms"]
            row["clock_failure"]=(t is None or h is None or not (row["press_ms"]<=t<=row["closed_ms"]) or
                                   h<0 or abs(t-row["press_ms"]-h)>1.)
        rows.append(row); by_epoch[row["epoch"]].append(row)
    # Both native identities must agree. Primary needs release_ff; legacy delivery identity
    # is allowed ONLY for historical/descriptive width joins, never fabricated A/F.
    for stage in ("release_ff", "identity", "ff", "reservation", "abort", "stretch"):
        for e in stages[stage]:
            ep=ident(e["f"].get("physical_epoch"))
            candidates=[r for r in by_epoch.get(ep,[]) if r["press_ms"] <= e["t"] <= r["closed_ms"]]
            if stage in ("release_ff", "identity"):
                candidates=[r for r in candidates if r["release_ms"] is not None and abs(r["release_ms"]-e["t"]) <= 150]
            if len(candidates)!=1:
                continue
            r=candidates[0]
            if stage in ("release_ff", "identity"):
                seq=ident(e["f"].get("seq" if stage=="release_ff" else "release_seq"))
                at=ident(e["f"].get("shot_attempt"))
                if seq is not None and at is not None:
                    r["identity_candidates"].append((e["process"],seq,at,stage))
            if stage=="ff":
                r["ff_events"].append(e)
            if ident(e["f"].get("shot_attempt")):
                r["attempt_events"].append(e)
    event_by_seq=defaultdict(list); draw_by_attempt=defaultdict(list)
    for e in events:
        if (seq:=ident(e["f"].get("seq"))) is not None:
            event_by_seq[(e["process"],seq,e["stage"])].append(e)
        if e["stage"]=="draw" and (at:=ident(e["f"].get("shot_attempt"))) is not None:
            draw_by_attempt[(e["process"],at)].append(e)
    for r in rows:
        keys={x[:3] for x in r["identity_candidates"]}
        if len(keys)>1:
            r["join_conflicts"].append("release_identity")
        elif len(keys)==1:
            r["process"],r["release_seq"],r["attempt"]=next(iter(keys))
            r["join_source"]="release_ff" if any(x[3]=="release_ff" for x in r["identity_candidates"]) else "delivery_legacy"
        attempts={(e["process"],ident(e["f"].get("shot_attempt"))) for e in r["attempt_events"]}
        if r["attempt"] is not None:
            attempts.add((r["process"],r["attempt"]))
        draws=[e for k in attempts for e in draw_by_attempt[k] if r["press_ms"]<=e["t"]<=r["closed_ms"]]
        draws.sort(key=lambda e:e["t"])
        by_attempt=defaultdict(set)
        for e in draws:
            by_attempt[(e["process"],ident(e["f"].get("shot_attempt")))].add(number(e["f"].get("offset_ms")))
        r["draw_count"]=len(by_attempt)
        r["draw_audit"]=[dict(attempt=ident(e["f"].get("shot_attempt")),Z=number(e["f"].get("offset_ms")),t=e["t"],source=f"{e['file']}:{e['line']}") for e in draws]
        refinements=[e for e in stages["dev_refinement"] if (e["process"],ident(e["f"].get("shot_attempt"))) in attempts and r["press_ms"]<=e["t"]<=r["closed_ms"]]
        r["dev_refinement_audit"]=[dict(fields=e["f"],t=e["t"],source=f"{e['file']}:{e['line']}") for e in refinements]
        r["ff_assignment_sources"]=[f"{e['file']}:{e['line']}" for e in r["ff_events"]]
        if any(len(v)!=1 or None in v for v in by_attempt.values()):
            r["join_conflicts"].append("draw_conflict")
        elif draws:
            r["Z_first"]=number(draws[0]["f"].get("offset_ms"))
            vals=by_attempt.get((r["process"],r["attempt"]))
            if vals:
                r["Z_final"]=next(iter(vals))
        arms={e["f"].get("ab_arm") for e in r["ff_events"] if "ab_arm" in e["f"]}
        r["arm_conflict"]=len(arms)>1 or bool(arms-{"0","1"})
        if len(arms)==1 and not r["arm_conflict"]:
            r["A"]=int(next(iter(arms)))
        ff=sorted(r["ff_events"],key=lambda e:e["t"])
        # No future log message is used as a pre-command covariate.
        ff=[e for e in ff if r["release_ms"] is None or e["t"]<=r["release_ms"]]
        if ff:
            f=ff[-1]["f"]; r["ff_n"]=number(f.get("n"))
            r["ff_gain"]=number(f.get("gain"));r["ff_clamp_ms"]=number(f.get("clamp_ms"))
            r["one_sided"]=number(f.get("one_sided"));r["ff_reason"]=f.get("reason")
            if number(f.get("ref_ms")) is not None and number(f["ref_ms"])>=0 and (r["ff_n"] or 0)>=4:
                d=number(f.get("dev_ms"))
                if d is not None:
                    r["onset_dev"]=d;r["reference_unavailable"]=0
        measured={}
        if r["release_seq"] is not None:
            for stage in ("issued","release_ff","release_dev","attribution","timing","schedule","landing"):
                ev=event_by_seq[(r["process"],r["release_seq"],stage)]
                if stage=="landing":
                    ev=[e for e in ev if 0<=e["t"]-r["release_ms"]<=5000]
                else:
                    ev=[e for e in ev if abs(e["t"]-r["release_ms"])<=150 and r["press_ms"]<=e["t"]<=r["closed_ms"]]
                f=unique_fields(ev)
                if ev and f is None:
                    r["join_conflicts"].append(stage)
                elif f:
                    measured[stage]=f
                    r[stage+"_source"]=";".join(f"{e['file']}:{e['line']}" for e in ev)
        dev=measured.get("release_dev",{});relff=measured.get("release_ff",{});sc=measured.get("schedule",{})
        sched={f.get("scheduled") for f in (dev,relff,sc) if f.get("scheduled") is not None}
        if len(sched)==1 and sched<= {"0","1"}:
            r["scheduled"]=int(next(iter(sched)))
        elif sched:
            r["join_conflicts"].append("scheduled_conflict")
        for f,key in ((dev,"D"),(relff,"F")):
            if f and ident(f.get("shot_attempt"))==r["attempt"]:
                r[key]=number(f.get("applied_ms"))
            elif f:
                r["join_conflicts"].append(key+"_attempt_mismatch")
        if sc.get("shot_attempt") is not None and ident(sc["shot_attempt"])!=r["attempt"]:
            r["join_conflicts"].append("schedule_attempt_mismatch")
        if r["scheduled"]==0 and any(r[k] not in (None,0.) for k in ("D","F")):
            r["join_conflicts"].append("nonzero_in_tick_displacement")
        r["scheduler_delta_ms"]=number(sc.get("deltaMs"))
        a=measured.get("attribution",{});t=measured.get("timing",{});l=measured.get("landing",{});i=measured.get("issued",{})
        w=number(a.get("greenWidth"));gc=number(a.get("greenConfirmMs"));elapsed=number(t.get("appearToRelMs"))
        r["pre_width_pp"]=w if w is not None and w>0 else None
        # Width-only tracker has no endpoints; price it on the extrapolated 90..100 segment.
        r["pre_width_ms"]=w/TOP_RATE*scale if w is not None and w>0 else None
        r["confirm_lead_ms"]=elapsed-gc if elapsed is not None and gc is not None and gc>=0 else None
        r["pre_confirmed"]=a.get("greenConfirmed")=="1" and r["pre_width_pp"] is not None and r["confirm_lead_ms"] is not None and r["confirm_lead_ms"]>=20
        r["release_velocity_pp_ms"]=number(a.get("vel"))
        r["issued_window_ms"]=curve_window(number(i.get("issued_green_start")),number(i.get("issued_green_end")),scale)
        gs,ge=number(l.get("green_start")),number(l.get("green_end"))
        if l.get("graded")=="1" and (number(l.get("green_obs_n")) or 0)>=3 and gs is not None and ge is not None and 0<gs<ge<=100:
            r["landing_start_pp"],r["landing_end_pp"]=gs,ge
            r["landing_width_pp"]=ge-gs;r["landing_width_ms"]=curve_window(gs,ge,scale)
            v=number(l.get("vel_at_rel"))
            r["landing_width_ms_release_velocity_WRONG_GLOBAL_RULER"]=(ge-gs)/v if v is not None and v>0 else None
        r["primary_join"]=r["join_source"]=="release_ff" and not r["join_conflicts"] and all(r[k] is not None for k in ("D","F","Z_final","A","scheduled")) and any(ident(e["f"].get("shot_attempt"))==r["attempt"] for e in r["ff_events"])
        r["H"]=r["hold_ms"]-r["onset_ms"] if r["hold_ms"] is not None and r["onset_ms"] is not None else None
        for k in ("identity_candidates","ff_events","attempt_events"):
            del r[k]
    return rows


def historical_selection(rows):
    groups=defaultdict(list)
    for r in rows:
        if r["online"] and r["shot_type"]=="Standstill" and r["outcome"]=="released" and r["grade"] in GRADES and r["onset_source"]=="detect_loop" and r["H"] is not None:
            groups[r["session"]].append(r)
    out=[]
    for rr in groups.values():
        if len(rr)<12:
            continue
        om=np.median([r["onset_ms"] for r in rr]);hm=np.median([r["hold_ms"] for r in rr])
        for r in rr:
            o=r["onset_ms"]-om;m=r["hold_ms"]-hm-o
            if abs(o)<=200 and abs(m)<=60:
                out.append(dict(r, onset_c=float(o), meter_c=float(m)))
    return out


def window_summary(rows):
    def summarise(rr):
        pre=[r for r in rr if r["pre_confirmed"] and .5<=r["pre_width_pp"]<=5]
        land=[r for r in rr if r["landing_width_pp"] is not None and .5<=r["landing_width_pp"]<=5]
        paired=[r for r in pre if r["landing_width_pp"] is not None and .5<=r["landing_width_pp"]<=5]
        return dict(shots=len(rr),sessions=len({r["session"] for r in rr}),
                    attribution_any=distribution(r["pre_width_pp"] for r in rr),
                    confirmed_plausible_pp=distribution(r["pre_width_pp"] for r in pre),
                    confirmed_plausible_ms=distribution(r["pre_width_ms"] for r in pre),
                    landing_any_pp=distribution(r["landing_width_pp"] for r in rr),
                    landing_any_ms=distribution(r["landing_width_ms"] for r in rr),
                    pre_plausible_sessions=len({r["session"] for r in pre}),
                    landing_plausible_sessions=len({r["session"] for r in land}),
                    landing_plausible_pp=distribution(r["landing_width_pp"] for r in land),
                    landing_plausible_ms=distribution(r["landing_width_ms"] for r in land),
                    paired_width_difference_pp=distribution(r["pre_width_pp"]-r["landing_width_pp"] for r in paired),
                    confirmation_lead_ms=distribution(r["confirm_lead_ms"] for r in pre),
                    issued_window_ms=distribution(r.get("issued_window_ms") for r in rr),
                    wrong_release_velocity_conversion_ms=distribution(r.get("landing_width_ms_release_velocity_WRONG_GLOBAL_RULER") for r in land),
                    paired_n=len(paired))
    hist=historical_selection(rows)
    return {"all_online_graded_standstills":summarise([r for r in rows if r["online"] and r["shot_type"]=="Standstill" and r["grade"] in GRADES and r["outcome"]=="released"]),
            "historical_816_selection":summarise(hist),
            "historical_by_session":{s:summarise([r for r in hist if r["session"]==s]) for s in sorted({r["session"] for r in hist})}}


def ordinal_probs(D, A, c, W, sigma, delta=0., slope=1., wc=None, eta=0., kappa=0., gamma=0., extra_mu=0.):
    D=np.asarray(D,dtype=float);A=np.asarray(A,dtype=float)
    wc=np.zeros_like(D) if wc is None else np.asarray(wc,dtype=float)
    center=c+eta*wc; width=W+kappa*wc
    mu=(slope+gamma*wc)*D+delta*A+extra_mu
    zlo=(center-width/2-mu)/sigma;zhi=(center+width/2-mu)/sigma
    return np.column_stack((special.ndtr(zlo),special.ndtr(zhi)-special.ndtr(zlo),special.ndtr(-zhi)))


def _log_grade(zlo,zhi,y):
    # Use survival differences in the positive tail to avoid 1-1 cancellation.
    a=np.where(zlo>0,special.log_ndtr(-zlo),special.log_ndtr(zhi))
    b=np.where(zlo>0,special.log_ndtr(-zhi),special.log_ndtr(zlo))
    mid=a+np.log(np.maximum(-np.expm1(np.minimum(b-a,0)),1e-300))
    return np.where(y==0,special.log_ndtr(zlo),np.where(y==2,special.log_ndtr(-zhi),mid))


def fit_ordinal(D,A,y, *, width=None, extra=None, fixed=None, slope=1., start=None, multistart=True):
    """MLE with delivered-offset coefficient FIXED. A free coefficient destroys ms identity.

    q=(c/25, W/25, log(sigma), delta/25, [eta/25,kappa/25,gamma], extra/25).
    Width alternative is exactly the preregistered linear c(w), W(w), D*width model.
    """
    D=np.asarray(D,float);A=np.asarray(A,float);y=np.asarray(y,int)
    if len(D)<4 or len(np.unique(y))<2 or not np.isfinite(D).all() or not np.isfinite(A).all():
        return {"success":False,"reason":"insufficient_or_invalid_rows"}
    wc=None if width is None else np.asarray(width,float)-np.median(width)
    X=np.empty((len(D),0)) if extra is None else np.asarray(extra,float).reshape(len(D),-1)
    if not np.isfinite(X).all() or (wc is not None and not np.isfinite(wc).all()):
        return {"success":False,"reason":"nonfinite_covariate"}
    names=["c","W","sigma","delta"]+(["eta","kappa","gamma"] if wc is not None else [])+[f"extra_{i}" for i in range(X.shape[1])]
    fixed=dict(fixed or {})
    if np.ptp(A)==0:
        fixed["delta"]=0.
    npar=len(names); inds={n:i for i,n in enumerate(names)}
    def natural(q):
        out={k:(math.exp(q[i]) if k=="sigma" else q[i] if k=="gamma" else 25*q[i]) for i,k in enumerate(names)}
        out.update(fixed)
        if "pmax" in fixed:
            out["W"]=2*out["sigma"]*stats.norm.ppf((fixed["pmax"]+1)/2)
        return out
    def encode(p):
        return np.array([math.log(p[k]) if k=="sigma" else p[k] if k=="gamma" else p[k]/25 for k in names])
    held=set(fixed)|({"W"} if "pmax" in fixed else set())
    free=[i for i,k in enumerate(names) if k not in held]
    def full(x,q0):
        q=q0.copy();q[free]=x;return q
    def objective(q):
        p=natural(q); w=p["W"]+(p.get("kappa",0)*wc if wc is not None else 0)
        if np.any(np.asarray(w)<=.001):
            return 1e30
        center=p["c"]+(p.get("eta",0)*wc if wc is not None else 0)
        mu=(slope+(p.get("gamma",0)*wc if wc is not None else 0))*D+p["delta"]*A
        if X.shape[1]:
            mu=mu+X@np.array([p[f"extra_{i}"] for i in range(X.shape[1])])
        zlo=(center-w/2-mu)/p["sigma"];zhi=(center+w/2-mu)/p["sigma"]
        return float(-_log_grade(zlo,zhi,y).sum())
    seeds=[dict(c=-7.4,W=94.,sigma=44.,delta=0.),dict(c=0.,W=16.,sigma=8.,delta=0.)]
    if start is not None:
        seeds.insert(0,start)
    if not multistart:
        seeds=seeds[:1]
    bounds=[(-80,80),(.00008,400),(-4,9),(-80,80)]+([(-80,80),(-80,80),(-5,5)] if wc is not None else [])+[(-80,80)]*X.shape[1]
    results=[]
    for seed in seeds:
        seed={**{k:0. for k in names},**seed};q0=encode(seed)
        fun=lambda x:objective(full(x,q0))
        cons=[]
        if wc is not None:
            def positive(x):
                p=natural(full(x,q0))
                return p["W"]+p["kappa"]*wc-.001
            cons=[{"type":"ineq","fun":positive}]
        result=optimize.minimize(fun,q0[free],method="SLSQP" if cons else "L-BFGS-B",
                                 bounds=[bounds[i] for i in free],constraints=cons,
                                 options={"maxiter":1800,"ftol":1e-10})
        results.append((result,full(result.x,q0)))
    candidates=[x for x in results if x[0].success and math.isfinite(x[0].fun) and x[0].fun<1e25]
    if not candidates:
        return {"success":False,"reason":"MLE_not_converged","messages":[str(r.message) for r,q in results]}
    res,q=min(candidates,key=lambda x:x[0].fun);p=natural(q)
    sigma=p["sigma"];W=p["W"]
    edge=any(abs(q[i]-bounds[i][j])<1e-4 for i in free for j in (0,1))
    probs=ordinal_probs(D,A,p["c"],W,sigma,p["delta"],slope,wc,p.get("eta",0),p.get("kappa",0),p.get("gamma",0),
                        X@np.array([p[f"extra_{i}"] for i in range(X.shape[1])]) if X.shape[1] else 0.)
    return {"success":True,"n":len(D),"nll":float(res.fun),"params":p,"pmax":float(2*special.ndtr(W/(2*sigma))-1),
            "boundary":edge,"width_center_pp":float(np.median(width)) if width is not None else None,
            "slope_fixed":slope,"probs":probs.tolist()}


def profile_intervals(D,A,y,fit, *, width=None, extra=None, parameters=("c","W","sigma","pmax")):
    if not fit.get("success"):
        return {}
    cutoff=fit["nll"]+stats.chi2.ppf(.95,1)/2
    result={}
    for name in parameters:
        value=fit["pmax"] if name=="pmax" else fit["params"][name]
        if name=="pmax":
            limits=(.00001,.99999);step=.03
        elif name in ("W","sigma"):
            limits=(.002,9900.);step=max(value*.2,1.)
        elif name=="gamma":
            limits=(-4.99,4.99);step=.02
        else:
            limits=(-1900.,1900.);step=max(abs(value)*.2,5.)
        def f(v):
            out=fit_ordinal(D,A,y,width=width,extra=extra,fixed={name:v},start=fit["params"],multistart=False)
            return out["nll"]-cutoff if out.get("success") else None
        ends=[];flags=[]
        for sign in (-1,1):
            prev=value;v=value;dv=step;bracket=None;failure=False
            for _ in range(22):
                v=max(limits[0],min(limits[1],value+sign*dv));fv=f(v)
                if fv is None:
                    failure=True;break
                if fv>=0:
                    bracket=sorted((prev,v));break
                prev=v
                if v==limits[0] or v==limits[1]:
                    break
                dv*=1.8
            if bracket:
                try:
                    endpoint=optimize.brentq(lambda v:f(v),*bracket,xtol=1e-4)
                    ends.append(float(endpoint));flags.append("finite")
                except (TypeError,ValueError):
                    ends.append(None);flags.append("profile_failed")
            else:
                ends.append(None);flags.append("profile_failed" if failure else "open_at_numerical_search_limit")
        result[name]={"estimate":value,"lower95":ends[0],"upper95":ends[1],"endpoint_status":flags}
    return result


def gof_bootstrap(D,A,y,Z,fit,reps=2000,seed=SEED):
    if not fit.get("success"):
        return {"status":"not_fitted"}
    D=np.asarray(D);A=np.asarray(A);y=np.asarray(y);Z=np.asarray(Z)
    bins=np.clip(np.searchsorted([-15,-5,5,15],Z,side="right"),0,4)
    def statistic(y,p):
        cells=[];total=0.
        for k in range(5):
            ix=bins==k;expected=p[ix].sum(axis=0);observed=np.bincount(y[ix],minlength=3)
            ok=expected>0;total+=float(np.sum((observed[ok]-expected[ok])**2/expected[ok]))
            cells.append(dict(bin=k,n=int(ix.sum()),observed=observed.tolist(),expected=expected.tolist()))
        return total,cells
    p=np.asarray(fit["probs"]);obs,cells=statistic(y,p);rng=np.random.default_rng(seed);null=[]
    for _ in range(reps):
        yy=(rng.random(len(D))[:,None]>np.cumsum(p,axis=1)[:,:2]).sum(axis=1)
        ff=fit_ordinal(D,A,yy,start=fit["params"],multistart=False)
        if ff.get("success"):
            null.append(statistic(yy,np.asarray(ff["probs"]))[0])
    valid=len(null)>=.95*reps and reps>0
    return dict(statistic=obs,cells=cells,requested=reps,successful=len(null),
                p=(1+sum(v>=obs for v in null))/(1+len(null)) if valid else None,
                status="complete" if valid else "bootstrap_incomplete")


def ols_hc3(y,x,nuisance=None):
    y=np.asarray(y,float);x=np.asarray(x,float)
    X=np.column_stack((np.ones(len(y)),x))
    if nuisance is not None:
        N=np.asarray(nuisance,float).reshape(len(y),-1)
        # Drop only CONSTANT nuisance columns. Nonconstant collinearity is a failed gate.
        N=N[:,np.ptp(N,axis=0)>1e-10];X=np.column_stack((X,N))
    n,p=X.shape
    if n<=p+2 or not np.isfinite(X).all() or np.ptp(x)==0 or np.linalg.matrix_rank(X)<p:
        return {"status":"not_identifiable","n":n}
    bread=np.linalg.inv(X.T@X);b=bread@X.T@y;res=y-X@b;h=np.sum((X@bread)*X,axis=1)
    if np.any(h>=1-1e-9):
        return {"status":"unit_leverage","n":n}
    cov=bread@(X.T*((res/(1-h))**2))@X@bread;se=math.sqrt(max(cov[1,1],0))
    critical=stats.t.ppf(.95,n-p)
    lo,hi=b[1]-critical*se,b[1]+critical*se
    return dict(status="estimated",n=n,rank=p,slope=float(b[1]),se_hc3=se,lower90=float(lo),upper90=float(hi))


def fidelity(rows, *, reps=2000, seed=SEED):
    target=[r for r in rows if r["shot_type"]=="Standstill"]
    graded=[r for r in target if r["outcome"]=="released" and r["grade"] in GRADES and r["coverage_state"]!="contested" and all(r.get(k) is not None for k in ("release_ms","hold_ms","press_ms"))]
    sched=[r for r in graded if r["scheduled"]==1]
    joined=[r for r in sched if r["primary_join"]]
    assigned=[r for r in target if r["A"] is not None and not r["arm_conflict"]]
    scheduled_all=[r for r in target if r["outcome"]=="released" and r["scheduled"]==1]
    scheduled_joined=[r for r in scheduled_all if r["primary_join"]]
    agreement=[r["D"] is not None and r["Z_final"] is not None and abs(r["D"]-r["Z_final"])<=.25 for r in scheduled_all]
    multicount=sum(r["draw_count"]>1 for r in rows)
    availability={}
    for field in ("Z_first","A"):
        groups=[[r for r in target if r[field] is not None and (int(r[field]>=0) if field=="Z_first" else r[field])==v] for v in (0,1)]
        rates=[sum(r["outcome"]=="released" and r["grade"] in GRADES for r in rr)/len(rr) if rr else None for rr in groups]
        availability[field]=dict(n=[len(rr) for rr in groups],grade_rates=rates,difference=abs(rates[1]-rates[0]) if None not in rates else None,
                                 in_tick_rates=[sum(r["scheduled"]==0 for r in rr)/len(rr) if rr else None for rr in groups],
                                 abort_rates=[sum(r["outcome"]!="released" for r in rr)/len(rr) if rr else None for rr in groups],
                                 missing_banner_rates=[sum(r["outcome"]=="released" and r["grade"] not in GRADES for r in rr)/len(rr) if rr else None for rr in groups])
    local=[r for r in scheduled_joined if r["H"] is not None]
    local.sort(key=lambda r:r["press_ms"])
    nuisance=np.array([[r["A"],r["onset_dev"],r["reference_unavailable"],i/max(1,len(local)-1)] for i,r in enumerate(local)])
    fit=ols_hc3([r["H"] for r in local],[r["Z_final"] for r in local],nuisance) if local else {"status":"no_rows","n":0}
    against_d=ols_hc3([r["H"] for r in local],[r["D"] for r in local],nuisance) if local else {"status":"no_rows","n":0}
    blocks=[]
    for session in sorted({r["session"] for r in local}):
        ix=[i for i,r in enumerate(local) if r["session"]==session]
        blocks.extend(np.array(ix[j:j+20]) for j in range(0,len(ix),20))
    boot=[];rng=np.random.default_rng(seed)
    if fit.get("status")=="estimated" and len(blocks)>=2:
        for _ in range(reps):
            ix=np.concatenate([blocks[j] for j in rng.integers(0,len(blocks),len(blocks))])
            b=ols_hc3([local[i]["H"] for i in ix],[local[i]["Z_final"] for i in ix],nuisance[ix])
            if b["status"]=="estimated":
                boot.append(b["slope"])
    counts=Counter(r["grade"] for r in joined)
    def ratio(a,b):return a/b if b else None
    conditions={
        "graded_release_identity_coverage_ge90":(ratio(sum(r["release_seq"] is not None for r in graded),len(graded)) or 0)>=.9,
        "graded_schedule_known_ge90":(ratio(sum(r["scheduled"] is not None for r in graded),len(graded)) or 0)>=.9,
        "scheduled_primary_join_coverage_ge90":(ratio(len(joined),len(sched)) or 0)>=.9,
        "local_clock_coverage_ge90":(ratio(len(local),len(scheduled_joined)) or 0)>=.9,
        "record_release_clocks_valid":not any(r.get("clock_failure",False) for r in rows),
        "all_attempt_arm_assignment_ge80":(ratio(len(assigned),len(rows)) or 0)>=.8,
        "multiple_dev_draw_epochs_le2pct":(ratio(multicount,len(rows)) or 0)<=.02,
        "ff_arm_consistent":not any(r["arm_conflict"] for r in rows),
        "carried_vs_draw_agreement_ge95":bool((np.mean(agreement) if agreement else 0)>=.95),
        "both_offset_signs":any(r["D"]<0 for r in joined) and any(r["D"]>0 for r in joined),
        "both_arms":{r["A"] for r in joined}=={0,1},
        "draws_within_uniform_support":all(-25<=r[k]<=25 for r in rows for k in ("Z_first","Z_final") if r[k] is not None),
        "eight_early_and_late":counts["EARLY"]>=8 and counts["LATE"]>=8,
        "grade_availability_difference_le10pp":all(v["difference"] is not None and v["difference"]<=.10 for v in availability.values()),
        "local_first_stage_equivalent":fit.get("status")=="estimated" and fit["lower90"]>=.8 and fit["upper90"]<=1.2,
    }
    return dict(pass_all=all(conditions.values()),conditions=conditions,failed=[k for k,v in conditions.items() if not v],
                funnel=dict(presses=len(rows),target_standstills=len(target),assigned=len(assigned),graded=len(graded),
                            scheduled_all=len(scheduled_all),scheduled_graded=len(sched),joined_scheduled_graded=len(joined),local_clock_rows=len(local),
                            in_tick=sum(r["scheduled"]==0 for r in target),unknown_schedule=sum(r["scheduled"] is None for r in target),
                            multiple_draw_epochs=multicount),
                delivery_agreement=ratio(sum(agreement),len(agreement)),availability=availability,
                D_distribution=distribution(r["D"] for r in scheduled_all),Z_distribution=distribution(r["Z_first"] for r in rows),
                F_by_arm={str(a):distribution(r["F"] for r in assigned if r["A"]==a) for a in (0,1)},
                first_stage_Z=fit,first_stage_D=against_d,
                block20_bootstrap=dict(blocks=len(blocks),successful=len(boot),requested=reps,
                                       lower90=float(np.quantile(boot,.05)) if boot else None,
                                       upper90=float(np.quantile(boot,.95)) if boot else None),
                scheduler_absolute_delta_ms=distribution(abs(r["scheduler_delta_ms"]) for r in joined if r["scheduler_delta_ms"] is not None),
                grade_counts=dict(counts))


def wilson(k,n):
    if not n:
        return None
    z=stats.norm.ppf(.975);p=k/n;den=1+z*z/n
    mid=(p+z*z/(2*n))/den;h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return mid-h,mid+h


def risk_difference(k1,n1,k0,n0):
    if not n1 or not n0:
        return {"status":"missing_arm","counts":[k1,n1,k0,n0]}
    p1=k1/n1;p0=k0/n0;l1,u1=wilson(k1,n1);l0,u0=wilson(k0,n0);rd=p1-p0
    N=n1+n0;S=k1+k0;ks=np.arange(max(0,n1-(N-S)),min(n1,S)+1)
    pmf=stats.hypergeom.pmf(ks,N,S,n1);diff=ks/n1-(S-ks)/n0
    return dict(status="estimated",counts=[k1,n1,k0,n0],arm1_rate=p1,arm0_rate=p0,difference=rd,
                lower95=rd-math.sqrt((p1-l1)**2+(u0-p0)**2),
                upper95=rd+math.sqrt((u1-p1)**2+(p0-l0)**2),
                permutation_p_two_sided=float(pmf[np.abs(diff)>=abs(rd)-1e-12].sum()),
                permutation_p_arm1_better=float(pmf[diff>=rd-1e-12].sum()))


def logistic_fit(X,y):
    X=np.asarray(X,float);y=np.asarray(y,float)
    if len(np.unique(y))<2 or np.linalg.matrix_rank(X)<X.shape[1]:return None
    def fg(b):
        z=X@b
        return np.logaddexp(0,z).sum()-y@z,X.T@(special.expit(z)-y)
    res=optimize.minimize(fg,np.zeros(X.shape[1]),jac=True,method="BFGS",options={"gtol":1e-6,"maxiter":500})
    if not np.isfinite(res.fun) or np.max(np.abs(res.x))>30 or np.linalg.norm(fg(res.x)[1],np.inf)>1e-4:
        return None
    return res.x,float(res.fun)


def ff_contrast(rows, *, reps=10000,seed=SEED):
    rr=[r for r in rows if r["shot_type"]=="Standstill" and r["A"] is not None and not r["arm_conflict"]]
    def contrast(sub,success):
        ns=[sum(r["A"]==a for r in sub) for a in (0,1)]
        ks=[sum(r["A"]==a and success(r) for r in sub) for a in (0,1)]
        return risk_difference(ks[1],ns[1],ks[0],ns[0])
    result=dict(assigned_n=len(rr),policy_success=contrast(rr,lambda r:r["outcome"]=="released" and r["grade"]=="EXCELLENT"),
                graded_only=contrast([r for r in rr if r["outcome"]=="released" and r["grade"] in GRADES],lambda r:r["grade"]=="EXCELLENT"),
                abort_or_missing=contrast(rr,lambda r:r["outcome"]!="released" or r["grade"] not in GRADES),
                early_all_assigned=contrast(rr,lambda r:r["outcome"]=="released" and r["grade"]=="EARLY"),
                warm_reference=contrast([r for r in rr if (r["ff_n"] or 0)>=4],lambda r:r["outcome"]=="released" and r["grade"]=="EXCELLENT"),
                actual_F_by_arm={str(a):distribution(r["F"] for r in rr if r["A"]==a) for a in (0,1)})
    ready=[r for r in rr if r["Z_first"] is not None]
    if len(ready)<20 or len({r["A"] for r in ready})<2:
        result["adjusted"]={"status":"insufficient_covariates_or_arms"};return result
    X=np.array([[1,r["A"],r["Z_first"]/25,(r["Z_first"]/25)**2,r["onset_dev"]/50,r["reference_unavailable"],i/max(1,len(ready)-1)-.5] for i,r in enumerate(ready)])
    keep=[0,1]+[j for j in range(2,X.shape[1]) if np.ptp(X[:,j])>1e-10];X=X[:,keep]
    y=np.array([r["outcome"]=="released" and r["grade"]=="EXCELLENT" for r in ready],float)
    def gcomp(X,y):
        f=logistic_fit(X,y)
        if f is None:return None
        x1=X.copy();x0=X.copy();x1[:,1]=1;x0[:,1]=0
        return float(np.mean(special.expit(x1@f[0])-special.expit(x0@f[0])))
    estimate=gcomp(X,y);rng=np.random.default_rng(seed);boot=[]
    if estimate is not None:
        for _ in range(reps):
            ix=rng.integers(0,len(y),len(y));v=gcomp(X[ix],y[ix])
            if v is not None:boot.append(v)
    result["adjusted"]=dict(status="estimated" if estimate is not None else "rank_or_separation_failure",n=len(ready),difference=estimate,
                            bootstrap_requested=reps,bootstrap_successful=len(boot),
                            lower95=float(np.quantile(boot,.025)) if reps and len(boot)>=.95*reps else None,
                            upper95=float(np.quantile(boot,.975)) if reps and len(boot)>=.95*reps else None)
    base=logistic_fit(X,y);interaction=np.array([r["A"]*r["onset_dev"]/50 for r in ready]);alt=logistic_fit(np.column_stack((X,interaction)),y)
    result["arm_onset_interaction"]={"lr":float(max(0,2*(base[1]-alt[1]))),"p":float(stats.chi2.sf(max(0,2*(base[1]-alt[1])),1))} if base and alt else {"status":"not_identifiable"}
    return result


def compact_fit(fit):
    return {k:v for k,v in fit.items() if k!="probs"}


def historical_fit(rows):
    """Observational diagnostic, NEVER a randomized primary fit."""
    selected=historical_selection(rows)
    subsets={"all":selected,
             "confirmed_visible": [r for r in selected if r["pre_confirmed"] and .5<=r["pre_width_pp"]<=5],
             "landing_visible": [r for r in selected if r["landing_width_pp"] is not None and .5<=r["landing_width_pp"]<=5]}
    result={}
    for label,rr in subsets.items():
        if len(rr)<20:
            result[label]={"success":False,"n":len(rr)};continue
        # Reuse the ORIGINAL session medians, not medians recalculated on the subset.
        f=fit_ordinal([r["meter_c"] for r in rr],np.zeros(len(rr)),[GRADES[r["grade"]] for r in rr],
                      extra=np.array([r["onset_c"]/25 for r in rr])[:,None])
        f=compact_fit(f);f["sessions"]=len({r["session"] for r in rr})
        f["grade_counts"]=dict(Counter(r["grade"] for r in rr))
        if f.get("success"):
            f["beta"]=f["params"]["extra_0"]/25
            f["L"]=f["params"]["c"]-f["params"]["W"]/2
            f["U"]=f["params"]["c"]+f["params"]["W"]/2
        result[label]=f
    return result


def hypothesis_predictions():
    W=94.39;sigma=43.54;c=-7.415
    pmax=2*special.ndtr(W/(2*sigma))-1
    d=np.array([-25,-12.5,0,12.5,25.]);grid=np.linspace(-25,25,10001)
    out={"convention":"arm 0; positive D is later; probabilities, not percentages", "hypotheses":{}}
    for name,w in (("broad94",W),("visible12",12.),("visible16",16.),("visible20",20.)):
        s=w/(2*special.ndtri((1+pmax)/2))
        raw=ordinal_probs(d,np.zeros(len(d)),c,w,s)
        centered=ordinal_probs(d,np.zeros(len(d)),0,w,s)
        pg=ordinal_probs(grid,np.zeros(len(grid)),c,w,s)
        out["hypotheses"][name]=dict(W=w,sigma=s,c=c,pmax=float(pmax),
            raw_D=d.tolist(),raw_probabilities=raw.tolist(),
            centered_offsets=d.tolist(),centered_probabilities=centered.tolist(),
            uniform_sweep_EXCELLENT=float(np.trapezoid(pg[:,1],grid)/50),
            area_bound=min(1.,w/50))
    out["area_identity"]="Integral over all offsets of P(EXCELLENT|D=d) dd = E[W]; independent uniform +/-25 implies sweep success <= E[W]/50."
    out["assumptions"]=["one-to-one independent additive offset", "width/center/noise unaffected by assignment", "no outcome-dependent selection"]
    out["narrow20_with_broad_sigma_max"]=float(2*special.ndtr(20/(2*sigma))-1)
    return out


def interval_bootstrap(D,A,y,fit, *, reps=2000,seed=SEED):
    p=np.asarray(fit["probs"]);rng=np.random.default_rng(seed);boot=[]
    for _ in range(reps):
        yy=(rng.random(len(y))[:,None]>np.cumsum(p,axis=1)[:,:2]).sum(axis=1)
        f=fit_ordinal(D,A,yy,start=fit["params"],multistart=False)
        if f.get("success") and not f["boundary"]:
            boot.append([f["params"][k] for k in ("c","W","sigma")]+[f["pmax"]])
    valid=reps>0 and len(boot)>=.95*reps
    return dict(requested=reps,successful=len(boot),status="complete" if valid else "bootstrap_incomplete",
                intervals={k:dict(lower95=float(np.quantile(np.array(boot)[:,j],.025)),
                                   upper95=float(np.quantile(np.array(boot)[:,j],.975)))
                           for j,k in enumerate(("c","W","sigma","pmax"))} if valid else {},
                note="A bootstrap interval does not close an open likelihood profile or repair a boundary fit.")


def width_interaction(rows, audit=None):
    rr=[r for r in rows if r["pre_confirmed"] and r["pre_width_pp"] is not None and .5<=r["pre_width_pp"]<=5]
    w=np.array([r["pre_width_pp"] for r in rr]);D=np.array([r["D"] for r in rr]);A=np.array([r["A"] for r in rr]);y=np.array([GRADES[r["grade"]] for r in rr])
    q25,q75=np.quantile(w,[.25,.75]) if len(w) else (float("nan"),float("nan"))
    groups=[np.flatnonzero(w<=q25),np.flatnonzero(w>=q75)]
    temporal=ols_hc3(w,[r["Z_final"] for r in rr]) if len(rr) else {"status":"no_rows"}
    # This audit must be independently made before seeing grades. Its keys are exact.
    # Required columns: session,epoch,first_confirmed_width_pp,release_width_pp.
    # Required companion JSON: blinded=true, selected_before_labels=true.
    audit_result={"status":"no_independent_frame_audit"};audit_pass=False
    if audit:
        entries=audit.get("rows",[]);lookup={(r["session"],r["epoch"]):r for r in rr};seen=set();diff=[];strata=Counter();valid=True
        zquartiles=np.quantile([r["Z_first"] for r in rr],[.25,.5,.75]) if rr else []
        expected=[]
        for arm in (0,1):
            for quartile in range(4):
                sub=sorted([r for r in rr if r["A"]==arm and np.searchsorted(zquartiles,r["Z_first"],side="right")==quartile],key=lambda r:r["press_ms"])
                expected.extend((r["session"],r["epoch"]) for r in sub[:5])
        for v in entries:
            key=(v.get("session"),ident(v.get("epoch")));r=lookup.get(key)
            a=number(v.get("first_confirmed_width_pp"));b=number(v.get("release_width_pp"))
            if key in seen or r is None or a is None or b is None or not (.5<=a<=5 and .5<=b<=5) or abs(b-r["pre_width_pp"])>.1:
                valid=False;continue
            seen.add(key);diff.append(abs(a-b));strata[(r["A"],int(np.searchsorted(zquartiles,r["Z_first"],side="right")))]+=1
        audit_pass=bool(valid and audit.get("blinded") is True and audit.get("selected_before_labels") is True and
                    len(entries)==40 and len(expected)==40 and seen==set(expected) and len(diff)==40 and np.median(diff)<=.2 and np.quantile(diff,.95)<=.5)
        audit_result=dict(status="passed" if audit_pass else "failed",n=len(diff),absolute_difference_pp=distribution(diff),
                          selection="first five chronological eligible shots in each A x Z-quartile stratum; exact 40 keys",strata={str(k):v for k,v in strata.items()})
    conditions=dict(at_least_60=len(rr)>=60,iqr_ge_1pp=bool(q75-q25>=1),twenty_each_outer_quartile=all(len(g)>=20 for g in groups),
                    independent_frame_audit=audit_pass,
                    width_not_shifted_by_Z=temporal.get("status")=="estimated" and temporal["lower90"]>=-.02 and temporal["upper90"]<=.02)
    result=dict(n=len(rr),width_pp=distribution(w),conditions=conditions,confirmatory_eligible=all(conditions.values()),
                frame_audit=audit_result,width_on_Z=temporal,local_first_stage_by_width={})
    for name,g in zip(("narrow","wide"),groups):
        sub=[rr[i] for i in g if rr[i]["H"] is not None]
        result["local_first_stage_by_width"][name]=ols_hc3([r["H"] for r in sub],[r["Z_final"] for r in sub],
                   [[r["A"],r["onset_dev"],r["reference_unavailable"],i/max(1,len(sub)-1)] for i,r in enumerate(sub)]) if sub else {"status":"no_rows"}
    result["quartile_delivery_unresolved"]=any(v.get("status")!="estimated" or v["lower90"]<.8 or v["upper90"]>1.2 for v in result["local_first_stage_by_width"].values())
    full=[r for r in rows if r["pre_confirmed"] and r["pre_width_pp"] is not None and r["pre_width_pp"]>0]
    result["full_range_sensitivity"]={"n":len(full),"status":"insufficient_rows"}
    if len(full)>=60 and len({r["pre_width_pp"] for r in full})>=4:
        result["full_range_sensitivity"]={"n":len(full),"status":"exploratory_only",
            "base":compact_fit(fit_ordinal([r["D"] for r in full],[r["A"] for r in full],[GRADES[r["grade"]] for r in full])),
            "alternative":compact_fit(fit_ordinal([r["D"] for r in full],[r["A"] for r in full],[GRADES[r["grade"]] for r in full],width=[r["pre_width_pp"] for r in full]))}
    if len(rr)<60 or len(np.unique(w))<4 or len(np.unique(y))<3:
        result["status"]="insufficient_for_width_model";return result
    base=fit_ordinal(D,A,y);alt=fit_ordinal(D,A,y,width=w)
    result.update(base=compact_fit(base),alternative=compact_fit(alt),status="confirmatory" if result["confirmatory_eligible"] else "exploratory_only")
    if not base.get("success") or not alt.get("success"):
        result["status"]="fit_failed";return result
    lr=max(0.,2*(base["nll"]-alt["nll"]));result["LRT3"]=dict(statistic=lr,p=float(stats.chi2.sf(lr,3)))
    result["profiles"]=profile_intervals(D,A,y,alt,width=w,parameters=("eta","kappa","gamma"))
    p=alt["params"];predictions=[]
    for width in (q25,q75):
        wc=width-alt["width_center_pp"];slope=1+p["gamma"]*wc
        center=p["c"]+p["eta"]*wc;width_ms=p["W"]+p["kappa"]*wc
        for arm in (0,1):
            for d in (-25.,0.,25.):
                lo=(center-width_ms/2-slope*d-p["delta"]*arm)/p["sigma"]
                hi=(center+width_ms/2-slope*d-p["delta"]*arm)/p["sigma"]
                # Units: change in probability per ms of delivered D.
                deriv=[-slope/p["sigma"]*stats.norm.pdf(lo),slope/p["sigma"]*(stats.norm.pdf(lo)-stats.norm.pdf(hi)),slope/p["sigma"]*stats.norm.pdf(hi)]
                predictions.append(dict(width_pp=float(width),D=d,A=arm,probabilities=ordinal_probs([d],[arm],p["c"],p["W"],p["sigma"],p["delta"],wc=[wc],eta=p["eta"],kappa=p["kappa"],gamma=p["gamma"])[0].tolist(),derivative=deriv))
    result["predictions"]=predictions
    result["band_center_IQR_shift_ms"]=float(p["eta"]*(q75-q25))
    def optimum(q,a):
        wc=q-alt["width_center_pp"];slope=1+p["gamma"]*wc
        return (p["c"]+p["eta"]*wc-p["delta"]*a)/slope if abs(slope)>1e-8 else None
    result["optimal_D_by_arm_and_width"]={str(a):[optimum(q,a) for q in (q25,q75)] for a in (0,1)}
    result["build_authorized"]=False
    result["build_reason"]="Later independent sessions and held-out adaptive policy gain remain required; an interaction alone is not a shipping result."
    return result


def trial_analysis(rows, *, protocol_pass=False, width_audit=None, reps_gof=2000, reps_ff=10000, reps_fidelity=2000):
    gate=fidelity(rows,reps=reps_fidelity)
    result=dict(fidelity=gate,FF=ff_contrast(rows,reps=reps_ff),protocol_pass=bool(protocol_pass),
                network={"status":"not_confirmatory_in_single_session","C1_C2":"Requires separate >=5-session held-out extension; not fitted by this script."},
                shipping_decision="No sweep-only shipping or ceiling-confirmed decision.")
    target=[r for r in rows if r["shot_type"]=="Standstill"]
    coverage=Counter(r["coverage_state"] for r in target)
    result["population"]=dict(coverage=dict(coverage),distance_ft=distribution(r.get("distance_ft") for r in target),
          open_generalisation=bool(target) and coverage["contested"]/len(target)<=.2 and coverage["unknown"]/len(target)<=.5)
    result["arm_offset_bins"]=[]
    for arm in (0,1):
        for b in range(5):
            sub=[r for r in target if r["A"]==arm and r["Z_first"] is not None and np.searchsorted([-15,-5,5,15],r["Z_first"],side="right")==b]
            result["arm_offset_bins"].append(dict(A=arm,bin=b,n=len(sub),grades=dict(Counter(r["grade"] or "MISSING" for r in sub))))
    result["FF"]["causal_interpretation_eligible"]=bool(protocol_pass and gate["pass_all"])
    if not protocol_pass or not gate["pass_all"]:
        result.update(status="PRIMARY_SUPPRESSED",primary={"status":"not_fitted","reason":"protocol_or_fidelity_gate_failed"},width={"status":"not_fitted_after_failed_gate"})
        return result
    rr=[r for r in rows if r["shot_type"]=="Standstill" and r["outcome"]=="released" and r["grade"] in GRADES and
        r["coverage_state"]!="contested" and r["scheduled"]==1 and r["primary_join"] and r["hold_ms"] is not None]
    D=np.array([r["D"] for r in rr]);A=np.array([r["A"] for r in rr]);y=np.array([GRADES[r["grade"]] for r in rr]);Z=np.array([r["Z_first"] for r in rr])
    fit=fit_ordinal(D,A,y);result.update(status="ANALYSED",primary=compact_fit(fit))
    if not fit.get("success"):
        result["status"]="PRIMARY_FIT_FAILED";return result
    profiles=profile_intervals(D,A,y,fit);result["primary"]["profiles"]=profiles
    if fit["boundary"] or any(v is None for item in profiles.values() for v in (item["lower95"],item["upper95"])):
        result["primary"]["bootstrap_fallback"]=interval_bootstrap(D,A,y,fit,reps=reps_gof)
    result["primary"]["goodness_of_fit"]=gof_bootstrap(D,A,y,Z,fit,reps=reps_gof)
    result["primary"]["fixed_scale_sensitivity"]={str(s):compact_fit(fit_ordinal(D,A,y,slope=s)) for s in (.8,1.2)}
    o=np.array([r["onset_ms"] if r["onset_ms"] is not None else np.nan for r in rr]);o-=np.nanmedian(o)
    keep=np.isfinite(o);order=np.arange(len(rr))/max(1,len(rr)-1)-.5
    extra=np.column_stack((o[keep]/50,order[keep],A[keep]*o[keep]/50))
    if len(extra):extra=extra[:,np.ptp(extra,axis=0)>1e-10]
    sensitive=fit_ordinal(D[keep],A[keep],y[keep],extra=extra)
    same_cohort_base=fit_ordinal(D[keep],A[keep],y[keep])
    shifts={k:sensitive["params"][k]/same_cohort_base["params"][k]-1 for k in ("W","sigma")} if sensitive.get("success") and same_cohort_base.get("success") else {}
    result["context_sensitivity"]=dict(fit=compact_fit(sensitive),same_cohort_base=compact_fit(same_cohort_base),fractional_shifts=shifts,context_dependent=any(abs(v)>.2 for v in shifts.values()),missing_onset=int((~keep).sum()))
    explicit=[r for r in rr if r["coverage_state"]=="open"]
    result["explicit_open_sensitivity"]=compact_fit(fit_ordinal([r["D"] for r in explicit],[r["A"] for r in explicit],[GRADES[r["grade"]] for r in explicit]))
    result["width"]=width_interaction(rr,width_audit)
    return result


def cohort_from_presses(rows,events,start,sessions,block):
    presses=[e for e in events if e["stage"]=="press" and e["t"]>=start][:block]
    chosen=[];missing=[];duplicates=[];used=set()
    for e in presses:
        ep=ident(e["f"].get("epoch"))
        matches=[r for r in rows if r["epoch"]==ep and abs(r["press_ms"]-e["t"])<=150 and r["session"] in sessions]
        if len(matches)!=1:
            missing.append(dict(epoch=ep,press_ms=e["t"],matches=len(matches)));continue
        r=matches[0];key=(r["session"],r["epoch"])
        if key in used:duplicates.append(key);continue
        used.add(key);chosen.append(r)
    return chosen,dict(requested=block,physical_presses=len(presses),records_matched=len(chosen),missing_or_ambiguous=missing,duplicate_keys=duplicates,
                       complete=len(presses)==block and not missing and not duplicates)


def capture_check(path,rows):
    if path is None:return {"pass":False,"reason":"capture_not_supplied"}
    try:
        meta=json.loads((path/"meta.json").read_text(encoding="utf-8-sig"))
        times=[];invalid=0
        with (path/"packets.csv").open(encoding="utf-8-sig",newline="") as f:
            for r in csv.DictReader(f):
                t=number(r.get("wall_ms"))
                if t is not None and r.get("dir") in ("in","out") and r.get("peer") and number(r.get("len")) is not None:
                    times.append(t)
                else:invalid+=1
        overlap=bool(times and rows and min(times)<=min(r["press_ms"] for r in rows) and max(times)>=max(r["closed_ms"] for r in rows))
        return dict(pass_=bool(overlap and not invalid and meta.get("snaplen")==42),rows=len(times),invalid=invalid,covers_block=overlap,
                    snaplen=meta.get("snaplen"),packet_identity="unverified; no capture delivery-fidelity claim",pcap_header_audit="manual independent prerequisite for network analysis")
    except (OSError,ValueError) as e:
        return {"pass_":False,"reason":str(e)}


def protocol_check(protocol,events,rows,cohort,capture,record_counts):
    required={"same_spot":True,"online_open_standstills":True,"offset_spec":"uniform:-25:25","ff_spec":"0.2:10:1,0.45:40:0"}
    fields={k:protocol.get(k)==v for k,v in required.items()}
    for k in ("build_sha256","settings_sha256"):
        fields[k]=bool(re.fullmatch(r"[0-9a-fA-F]{64}",str(protocol.get(k,""))))
    processes={r["process"] for r in rows if r["process"] is not None}
    hooks={e["process"] for e in events if e["stage"]=="hook" and e["f"].get("spec")=="uniform:-25:25" and rows and e["t"]<=max(r["closed_ms"] for r in rows)}
    fields["hook_in_each_process"]=bool(processes) and processes<=hooks
    fields["complete_press_ledger"]=cohort.get("complete",False)
    fields["capture_collecting"]=capture.get("pass_",False)
    fields["no_corrupt_input_identity"]=not any(record_counts.get(k,0) for k in ("invalid_json","invalid_identity_or_interval","conflicting_duplicates"))
    # Verify logged arm settings, not just a declaration or nonzero displacement.
    assigned=[r for r in rows if r["A"] in (0,1)]
    fields["logged_arm_parameters"]=bool(assigned) and all(r.get("ff_gain")==(.2 if r["A"]==0 else .45) and
         r.get("ff_clamp_ms")== (10 if r["A"]==0 else 40) and r.get("one_sided")== (1 if r["A"]==0 else 0) for r in assigned)
    return {"pass_all":all(fields.values()),"conditions":fields,"capture":capture,"declaration":protocol}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_outputs(out,rows,report):
    out.mkdir(parents=True,exist_ok=False)  # Freeze results; never overwrite a prior run.
    fields=sorted({k for r in rows for k in r})
    with (out/"shots.csv").open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for r in rows:
            writer.writerow({k:json.dumps(v,separators=(",",":")) if isinstance(v,(dict,list)) else v for k,v in r.items()})
    (out/"summary.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
    lines=[f"sweep_analysis {VERSION}",f"mode={report['mode']}",f"records={len(rows)}",
           f"status={report['trial']['status']}",f"assigned={report['trial']['fidelity']['funnel']['assigned']}",
           f"fidelity_pass={report['trial']['fidelity']['pass_all']}",
           "failed="+",".join(report['trial']['fidelity']['failed']),
           "primary_fit="+str(report['trial']['primary'].get('success',False)),
           "No application launch, build, capture, network connection, or source modification performed."]
    text="\n".join(lines)+"\n";(out/"RUN.txt").write_text(text,encoding="utf-8");return text


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--records-dir",type=Path,required=True)
    ap.add_argument("--logs",type=Path,nargs="+",required=True)
    ap.add_argument("--out-dir",type=Path,required=True)
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--session",action="append",default=[])
    ap.add_argument("--start-utc")
    ap.add_argument("--block",type=int,choices=(100,200),default=100)
    ap.add_argument("--protocol-json",type=Path)
    ap.add_argument("--capture-dir",type=Path)
    ap.add_argument("--width-audit",type=Path,help="JSON with blinded, selected_before_labels and exact-key rows; see preregistration")
    args=ap.parse_args(argv)
    if args.out_dir.exists():ap.error("output directory already exists; use a new path")
    if not args.dry_run and (not args.session or not args.start_utc or not args.protocol_json):
        ap.error("real analysis requires --session, --start-utc and --protocol-json")
    paths=sorted(args.records_dir.glob("*.jsonl"))
    if not paths:ap.error("no shot-record JSONL files")
    plan_path=Path(__file__).resolve().parents[2]/"docs"/"variance"/"PREREGISTRATION_OFFSET_SWEEP.md"
    inputs=paths+args.logs+[Path(__file__).resolve()]+[p for p in (args.protocol_json,args.width_audit) if p]
    if plan_path.is_file():inputs.append(plan_path)
    if args.capture_dir:
        inputs += [args.capture_dir/name for name in ("meta.json","packets.csv","pings.csv") if (args.capture_dir/name).is_file()]
    before={str(p.resolve()):sha256(p) for p in inputs}
    records,rc=load_records(paths);events,ec=load_events(args.logs);rows=join_shots(records,events)
    corpus=hashlib.sha256()
    for p in paths:corpus.update(p.name.encode()+b"\0"+bytes.fromhex(before[str(p.resolve())]))
    audit=json.loads(args.width_audit.read_text(encoding="utf-8-sig")) if args.width_audit else None
    windows=window_summary(rows);legacy=historical_fit(rows) if args.dry_run else {"status":"not_run_on_new_trial"}
    if args.dry_run:
        cohort={"status":"legacy_all_records_not_a_trial"};protocol={"pass_all":False,"reason":"dry_run_never_a_preregistered_trial"}
    else:
        rows,cohort=cohort_from_presses(rows,events,utc_ms(args.start_utc),set(args.session),args.block)
        p=json.loads(args.protocol_json.read_text(encoding="utf-8-sig"))
        protocol=protocol_check(p,events,rows,cohort,capture_check(args.capture_dir,rows),rc)
    trial=trial_analysis(rows,protocol_pass=protocol["pass_all"],width_audit=audit)
    if any(sha256(p)!=before[str(p.resolve())] for p in inputs):
        raise RuntimeError("input changed during analysis; discard this result and rerun on frozen inputs")
    report=dict(version=VERSION,mode="legacy_dry_run" if args.dry_run else "preregistered_trial",argv=sys.argv[1:] if argv is None else argv,
                created_utc=datetime.now(timezone.utc).isoformat(),numpy=np.__version__,scipy=__import__('scipy').__version__,
                input_sha256=before,corpus_digest=corpus.hexdigest(),record_parse=rc,event_parse=ec,
                join_counts=dict(Counter(r["join_source"] or "none" for r in rows)),
                conflicts=[{"session":r["session"],"epoch":r["epoch"],"reasons":r["join_conflicts"]} for r in rows if r["join_conflicts"]],
                cohort=cohort,protocol=protocol,trial=trial,visible_windows=windows,legacy_observational_fit=legacy,
                hypothesis_predictions=hypothesis_predictions())
    print(write_outputs(args.out_dir,rows,report),end="")
    return 0 if args.dry_run or trial["status"]=="ANALYSED" else 2


if __name__=="__main__":
    raise SystemExit(main())
