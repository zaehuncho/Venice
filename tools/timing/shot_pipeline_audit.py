"""Session-scoped, identity-joined shot timeline; no control or tuning side effects.

Never infer milliseconds from a categorical banner or add overlapping stage
variances. Local socket acceptance is not console receipt. Repeated release IDs
in a rotated/appended log must not leak into another session's measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

KV = re.compile(r"\b([A-Za-z_]\w*)=([^\s]+)")
# "Release issued:" carries the engine's green window as prose, not as a key=value pair:
#   Release issued: fill 42.0% target 100.0% (green 92.0-96.0) seq=1
# Parsed here so the window travels with the event's own fields and stays hashable for
# the per-stage dedupe below. Never reconstruct it from a later line: a window read off
# a different release is a different shot's ruler.
GREEN_WINDOW = re.compile(r"\(green\s+([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)\)")
STAGES = {
    "Release issued:": "decision",
    "Release attribution:": "prediction",
    "Release timing:": "release_timing",
    "Scheduled fire:": "schedule",
    "Release submit:": "submit",
    "Precise dispatch timing:": "dispatch",
    "Release delivery identity:": "identity",
    "Release detsummary:": "detector_census",
    "Release freshness:": "freshness",
    "Square-down delivery identity:": "square_down",
    "Square-up route audit:": "square_up",
    "SHOT NOT OWNED:": "unowned",
    "TIP DEADLINE DECISION:": "late_decision",
    "TIP RESERVATION:": "reservation",
    "TIP SAMPLER RULER RESET:": "ruler_reset",
    "TIP PHASE RATE STRETCH:": "rate_stretch",
    "Release onsetff:": "onset_ff",
    "Release devoffset:": "dev_offset",
}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def identity(value):
    # Do not round uint64 identities through IEEE754, even in offline tooling.
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        return None
    value = int(value)
    return value if 0 < value < 2**64 else None


def count(value):
    # A census count, not an identity: zero is a real observation and must survive.
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        return None
    return int(value)


def distribution(values):
    ordered = sorted(v for value in values if (v := number(value)) is not None)
    if not ordered:
        return {"n": 0, **dict.fromkeys(("mean", "median", "stddev", "p90", "p95", "p99", "max"))}

    def quantile(p):
        at = (len(ordered) - 1) * p
        lo = math.floor(at)
        hi = math.ceil(at)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)

    return dict(n=len(ordered), mean=statistics.mean(ordered), median=statistics.median(ordered),
                stddev=statistics.pstdev(ordered), p90=quantile(.90), p95=quantile(.95),
                p99=quantile(.99), max=max(ordered))


def log_events(lines):
    for line in lines:
        if "Sidecar tail:" in line:
            continue
        try:
            timestamp = datetime.fromisoformat(line[:24].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                continue
            wall_ms = timestamp.timestamp() * 1000
        except ValueError:
            continue
        for tag, stage in STAGES.items():
            if tag in line:
                fields = dict(KV.findall(line))
                if stage == "decision" and (window := GREEN_WINDOW.search(line)):
                    fields["green_window_pct"] = f"{window.group(1)}-{window.group(2)}"
                yield dict(stage=stage, wall_ms=wall_ms, fields=fields)
                break


def audit(records, lines):
    if not records or len({r.get("session") for r in records}) != 1:
        raise ValueError("Exactly one nonempty shot-record session is required")
    rows = {}
    for record in records:
        epoch = identity(record.get("epoch"))
        start, end = number(record.get("press_ts_ms")), number(record.get("closed_ts_ms"))
        if epoch is None or epoch in rows or start is None or end is None or end < start:
            raise ValueError("Invalid/duplicate shot identity or observation interval")
        banner = record.get("banner") or {}
        rows[epoch] = dict(epoch=epoch, shot_type=record.get("shot_type_upgraded") or record.get("shot_type"),
                           press_ms=start, closed_ms=end, outcome=record.get("outcome"),
                           abort_reason=record.get("disarm_reason"), banner=banner.get("timing"),
                           coverage=banner.get("coverage"), onset_ms=number(record.get("onset_ms")),
                           oracle_gap_pct=number((record.get("oracle") or {}).get("gap_pct")),
                           release_seq=None, events=[], measurements={})
    events = list(log_events(lines))
    identities = {}
    ambiguous = set()
    for event in events:
        if event["stage"] != "identity":
            continue
        fields = event["fields"]
        epoch, seq = identity(fields.get("physical_epoch")), identity(fields.get("release_seq"))
        row = rows.get(epoch)
        if seq is None or row is None or not row["press_ms"] <= event["wall_ms"] <= row["closed_ms"]:
            continue
        # A conflicting in-session identity is missing evidence, never last-write-wins.
        if seq in identities and identities[seq] != epoch:
            ambiguous.add(seq)
        identities[seq] = epoch
    for seq in ambiguous:
        del identities[seq]
    by_epoch = Counter(identities.values())
    for seq, epoch in list(identities.items()):
        if by_epoch[epoch] > 1:
            ambiguous.add(seq)
            del identities[seq]
    seen = set()
    for event in events:
        fields, stage = event["fields"], event["stage"]
        if stage in ("square_down", "square_up", "unowned", "late_decision",
                     "reservation", "ruler_reset", "rate_stretch"):
            epoch = identity(fields.get("physical_epoch", fields.get("latest_physical_epoch")))
        else:
            seq = identity(fields.get("release_seq") if stage == "identity" else fields.get("seq"))
            epoch = identities.get(seq)
        row = rows.get(epoch)
        if row is None or not row["press_ms"] <= event["wall_ms"] <= row["closed_ms"]:
            continue
        if (stage == "onset_ff" and fields.get("physical_epoch") is not None
                and identity(fields.get("physical_epoch")) != epoch):
            continue
        key = (epoch, stage, event["wall_ms"], tuple(sorted(fields.items())))
        if key in seen:
            continue
        seen.add(key)
        row["events"].append(event)
        if stage == "identity":
            row["release_seq"] = identity(fields.get("release_seq"))
    streak = 0
    for row in sorted(rows.values(), key=lambda r: r["press_ms"]):
        row["preceding_excellent_releases"] = streak
        if row["outcome"] == "released":
            streak = streak + 1 if row["banner"] == "EXCELLENT" else 0
        by_stage = {}
        for event in row["events"]:
            by_stage.setdefault(event["stage"], []).append(event["fields"])
        # Repeated/conflicting stage records are not silently averaged per shot.
        single = {stage: values[0] for stage, values in by_stage.items() if len(values) == 1}
        metrics = row["measurements"]
        for stage, source, dest in (
            ("schedule", "deltaMs", "scheduler_error_ms_rounded"),
            ("dispatch", "active_route_wait_ms", "local_route_wait_ms"),
            ("dispatch", "pipe_write_us", "pipe_write_us"),
            ("dispatch", "pipe_ack_wait_us", "pipe_ack_wait_us"),
            ("unowned", "hold_ms", "unowned_hold_ms"),
            ("late_decision", "lateness_ms", "late_decision_ms"),
            ("onset_ff", "applied_ms", "reported_onset_ff_ms"),
            ("dev_offset", "applied_ms", "reported_dev_offset_ms"),
        ):
            fields = single.get(stage, {})
            if dest in ("pipe_write_us", "pipe_ack_wait_us") and fields.get(source + "_valid") != "1":
                continue
            value = number(fields.get(source))
            if value is not None and (source in ("deltaMs", "applied_ms") or value >= 0):
                metrics[dest] = value
        command = number(single.get("dispatch", {}).get("command_issued_ms"))
        deadline = number(single.get("schedule", {}).get("aligned_ms"))
        if command is not None and deadline is not None:
            # aligned_ms is the vision/base deadline, BEFORE scheduleFire's
            # applied onset/dev offsets. Its difference is not scheduler jitter.
            # The release's reported deltaMs already uses the actual deadline.
            metrics["command_minus_base_deadline_ms"] = command - deadline
        age = single.get("decision", {}).get("age", "")
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?ms", age):
            metrics["reported_frame_age_at_release_ms"] = number(age[:-2])
        # WHEN the engine first held a usable answer -- not the meter's onset, and not the
        # late-fire decision that may follow it. Two valid reservations sharing the earliest
        # instant are conflicting evidence, not a tie to break.
        valid = [event for event in row["events"]
                 if event["stage"] == "reservation" and event["fields"].get("valid") == "1"]
        row["first_valid_tip_evaluation"] = None
        if valid:
            earliest = min(event["wall_ms"] for event in valid)
            first = [event for event in valid if event["wall_ms"] == earliest]
            if len(first) == 1:
                fields = first[0]["fields"]
                eta = number(fields.get("command_eta_ms"))
                after_press = earliest - row["press_ms"]
                onset = row["onset_ms"]
                row["first_valid_tip_evaluation"] = dict(
                    after_press_ms=after_press,
                    after_onset_ms=after_press - onset if onset is not None else None,
                    command_eta_ms=eta,
                    # A positive command ETA is runway left; it is not a claim about the game.
                    ahead_of_deadline=eta > 0 if eta is not None else None,
                    fill_pct=number(fields.get("fill_pct")), lead_ms=number(fields.get("lead_ms")),
                    frame_age_ms=number(fields.get("frame_age_ms")),
                    source=fields.get("source"), disposition=fields.get("disposition"))
        decision = single.get("late_decision")
        row["late_decision"] = dict(
            disposition=decision.get("disposition"),
            lateness_ms=number(decision.get("lateness_ms"))) if decision else None
        # The refusal's own census, reported only when exactly one refusal joined this epoch.
        proof = single.get("unowned")
        row["ownership_proof"] = dict(
            reason=proof.get("reason"), samples=count(proof.get("samples")),
            first_fill_pct=number(proof.get("first_fill")),
            last_fill_pct=number(proof.get("last_fill")),
            restarts=count(proof.get("restarts")), break_geometry=count(proof.get("break_geometry")),
            break_anchor=count(proof.get("break_anchor")), unstamped=count(proof.get("unstamped")),
            stamp_epoch_seen=count(proof.get("stamp_epoch_seen"))) if proof else None
        # What the ENGINE believed about its own release, kept strictly separate from the
        # game's verdict. Absent or repeated evidence is None, never False: "no attribution
        # line joined to this epoch" and "the engine confirmed no green" are different facts.
        confirmed = single.get("prediction", {}).get("greenConfirmed")
        row["release_green_confirmed"] = confirmed == "1" if confirmed in ("0", "1") else None
        bounds = [number(v) for v in
                  (single.get("decision", {}).get("green_window_pct") or "").split("-")]
        row["release_green_window_pct"] = (
            bounds if len(bounds) == 2 and None not in bounds and bounds[0] <= bounds[1] else None)
        row["ambiguous_stages"] = sorted(stage for stage, values in by_stage.items() if len(values) > 1
                                         and stage not in ("square_up", "reservation", "ruler_reset", "rate_stretch"))
        row["ruler_resets"] = len(by_stage.get("ruler_reset", []))
        row["prediction_revisions"] = len(by_stage.get("reservation", []))
    ordered = sorted(rows.values(), key=lambda r: r["press_ms"])
    names = sorted({name for row in ordered for name in row["measurements"]})
    return dict(schema="shot_pipeline_audit/2", session=records[0]["session"],
                counts=dict(records=len(ordered), outcomes=dict(Counter(r["outcome"] for r in ordered)),
                            disarm_reasons=dict(Counter(r["abort_reason"] for r in ordered if r["outcome"] == "disarmed")),
                            release_banners=dict(Counter(r["banner"] or "unknown" for r in ordered if r["outcome"] == "released")),
                            joined_releases=sum(r["release_seq"] is not None for r in ordered),
                            # The open-shot disagreement census: the engine confirmed it released
                            # inside the green window, the panel showed an uncontested shot, and the
                            # game still graded it LATE. Requires a joined release identity -- the
                            # attribution text alone never names a shot.
                            reported_green_open_late_epochs=sorted(
                                r["epoch"] for r in ordered
                                if r["outcome"] == "released" and r["banner"] == "LATE"
                                and str(r["coverage"] or "").strip().upper() in ("OPEN", "WIDE OPEN")
                                and r["release_green_confirmed"] is True),
                            ambiguous_release_sequences=sorted(ambiguous)),
                distributions={name: distribution(r["measurements"].get(name) for r in ordered) for name in names},
                unavailable=["true signed/absolute game release error in milliseconds", "console receipt time",
                             "open-shot denominator", "causal variance decomposition", "ground-truth false-lock rate"],
                interpretation=["Local route wait includes write and ACK wait; do not add their variances.",
                                "Command-minus-base-deadline includes intentional onset/dev offsets, NOT scheduler jitter.",
                                "scheduler_error_ms_rounded is the engine's actual-deadline error at 0.01ms precision.",
                                "Missing offset evidence is UNKNOWN; offset-stage variances are not additive.",
                                "Oracle gap is a pixel/progress proxy, not milliseconds or a make/miss label.",
                                "Excellent streaks count released records; intervening taps are not outcomes.",
                                "Reported-green open lates are an engine/game DISAGREEMENT census: the "
                                "engine's own green flag and window, not a measured landing error."],
                shots=ordered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    records = [json.loads(line) for line in args.records.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    result = audit(records, args.log.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    result["sources"] = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.records, args.log)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "distributions": result["distributions"]}, indent=2))


if __name__ == "__main__":
    main()
