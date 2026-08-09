#!/usr/bin/env python3
"""Turn the latest live Orion session into a truthful per-release report.

The report joins native release timing with the exact local delivery stage.  A
``local_udp_accepted`` result means Chiaki accepted the feedback transaction at
its local UDP boundary; ``active_vigem_submit`` means the active local virtual
controller accepted it.  Neither result is a console or gameplay
acknowledgement, so the user's observed in-game release remains the final proof.

The detector's post-release window comparison is shown only as a release-window
diagnostic.  It is not a make/miss or green result and is never used to compute
accuracy here.

Examples::

    .venv\\Scripts\\python.exe tools/timing/live_batch_report.py
    .venv\\Scripts\\python.exe tools/timing/live_batch_report.py --since 02:54
"""
from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG = os.path.join(ROOT, "logs", "orion_native.log")
SESSION_MARKER = "Sidecar env keys:"

PATS = {
    "issued": re.compile(
        r"Release issued: fill ([\d.]+)% target ([\d.]+)%.*?"
        r"code=([\w-]+).*?offset=([-\d.]+)ms seq=(\d+) shot=(.+)$"
    ),
    "timing": re.compile(
        r"Release timing: seq=(\d+).*?fillAtRel=([-\d.]+) peakFill=([-\d.]+)"
    ),
    "shadow": re.compile(r"Shadow timing: seq=(\d+).*?latencyMs=([-\d.]+)"),
    "submit": re.compile(r"Release submit: seq=(\d+)\s*(.*)$"),
    "window": re.compile(
        r"Release-window diagnostic \(not game outcome\): seq=(\d+) "
        r"position=(\w+) fill=([-\d.]+) window_start=([-\d.]+) shot=(.+)$"
    ),
    "latency_observation": re.compile(
        r"INFO latency_estimator: latency observation:\s+(.*)$"
    ),
    "abort": re.compile(r"Shot automation aborted: ([A-Za-z0-9_-]+)"),
    "unowned": re.compile(r"SHOT NOT OWNED: reason=([A-Za-z0-9_-]+)"),
    "unresolved": re.compile(r"OWNED-SHOT UNRESOLVED: reason=([A-Za-z0-9_-]+)"),
    "kv": re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)"),
}

LOCAL_ACCEPTED_STAGES = frozenset({"local_udp_accepted", "active_vigem_submit"})


def _session_lines(lines: list[str], since: str | None, latest_session: bool) -> list[str]:
    if since:
        for index, line in enumerate(lines):
            if since in line:
                return lines[index:]
        return []
    if latest_session:
        for index in range(len(lines) - 1, -1, -1):
            if SESSION_MARKER in lines[index]:
                return lines[index:]
    return lines


def _parse_submit(tail: str) -> dict:
    if tail.startswith("NOT_SUBMITTED"):
        reason = tail[len("NOT_SUBMITTED"):].strip() or "unspecified"
        return {
            "submit_ok": False,
            "delivery_stage": "not_submitted",
            "submit_reason": reason,
            "console_ack": False,
        }

    fields = dict(PATS["kv"].findall(tail))
    stage = fields.get("delivery_stage", "legacy_unverified").lower()
    ok = fields.get("ok") == "1"
    # No Orion route currently exposes a per-release console acknowledgement.
    # Missing legacy fields therefore fail closed to false rather than unknown-as-true.
    console_ack = fields.get("console_ack") == "1"
    return {
        "submit_ok": ok,
        "delivery_stage": stage,
        "backend": fields.get("backend", "?"),
        "console_ack": console_ack,
        "local_accepted": ok and stage in LOCAL_ACCEPTED_STAGES,
    }


def parse(logpath, since=None, latest_session=True):
    shots = defaultdict(dict)
    with open(logpath, encoding="utf-8", errors="replace") as fh:
        lines = _session_lines(fh.readlines(), since, latest_session)

    for line in lines:
        match = PATS["issued"].search(line)
        if match:
            seq = int(match.group(5))
            shots[seq].update(
                fill_issue=float(match.group(1)),
                target=float(match.group(2)),
                code=match.group(3),
                offset=float(match.group(4)),
                shot=match.group(6),
            )

        match = PATS["timing"].search(line)
        if match:
            seq = int(match.group(1))
            shots[seq].update(
                fillAtRel=float(match.group(2)), peakFill=float(match.group(3))
            )

        match = PATS["shadow"].search(line)
        if match:
            seq = int(match.group(1))
            shots[seq]["latencyMs"] = float(match.group(2))

        match = PATS["submit"].search(line)
        if match:
            seq = int(match.group(1))
            shots[seq].update(_parse_submit(match.group(2)))

        match = PATS["window"].search(line)
        if match:
            seq = int(match.group(1))
            shots[seq].update(
                window_position=match.group(2),
                window_fill=float(match.group(3)),
                window_start=float(match.group(4)),
            )
            shots[seq].setdefault("shot", match.group(5))

        match = PATS["latency_observation"].search(line)
        if match:
            fields = dict(PATS["kv"].findall(match.group(1)))
            try:
                seq = int(fields.get("seq", "0"))
            except ValueError:
                seq = 0
            if seq > 0:
                shots[seq].update(
                    latency_observation=True,
                    latency_accepted=fields.get("accepted") == "1",
                    latency_status=fields.get("status", "unknown"),
                    latency_reject=fields.get("reject", "-"),
                    latency_n=fields.get("n", "?"),
                    latency_freeze=fields.get("freeze", "?"),
                    latency_total_ms=fields.get("total_ms", "-"),
                    latency_target_pct=fields.get("target_pct", "-"),
                    latency_stop_pct=fields.get("f_stop", "-"),
                )

    return shots


def parse_ownership_events(logpath, since=None, latest_session=True):
    """Return non-release ownership outcomes from the same selected log window.

    A release-only table is insufficient for launch validation: by definition it
    cannot contain a shot that never reached ``triggerRelease``. Keep terminal
    aborts separate from pre-ownership and owned-but-unresolved diagnostics so a
    stress batch cannot report 100% intervention merely by omitting failures.
    """
    with open(logpath, encoding="utf-8", errors="replace") as fh:
        lines = _session_lines(fh.readlines(), since, latest_session)

    events = {"aborts": [], "unowned": [], "unresolved": []}
    for line in lines:
        for key in events:
            match = PATS[
                "abort" if key == "aborts" else key
            ].search(line)
            if match:
                events[key].append(match.group(1))
    return events


def med(values):
    values = sorted(values)
    return values[len(values) // 2] if values else float("nan")


def _stage_label(value: dict) -> str:
    stage = value.get("delivery_stage", "missing")
    labels = {
        "local_udp_accepted": "UDP_ACCEPT",
        "active_vigem_submit": "VIGEM_ACCEPT",
        "not_submitted": "NOT_SENT",
        "legacy_unverified": "LEGACY_?",
        "missing": "NO_RECORD",
    }
    return labels.get(stage, stage.upper()[:12])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=LOG)
    parser.add_argument(
        "--since", default="", help="only releases after a log token (for example 02:54)"
    )
    parser.add_argument(
        "--all-sessions",
        action="store_true",
        help="parse the full cumulative log (sequence numbers may repeat across launches)",
    )
    args = parser.parse_args()

    shots = parse(
        args.log,
        args.since or None,
        latest_session=not args.all_sessions and not bool(args.since),
    )
    ownership = parse_ownership_events(
        args.log,
        args.since or None,
        latest_session=not args.all_sessions and not bool(args.since),
    )
    seqs = sorted(
        seq for seq, value in shots.items()
        if "fillAtRel" in value or "fill_issue" in value or "delivery_stage" in value
    )
    if not seqs and not any(ownership.values()):
        print("No shot outcomes parsed (the latest session may not have fired yet).")
        return

    accepted = [seq for seq in seqs if shots[seq].get("local_accepted", False)]
    failed = [
        seq for seq in seqs
        if shots[seq].get("delivery_stage") == "not_submitted"
        or shots[seq].get("submit_ok") is False
    ]

    print("=" * 112)
    print(
        f"LIVE RELEASE REPORT | {len(seqs)} releases | "
        f"{len(accepted)} locally accepted | {len(failed)} failed/not submitted | "
        f"{len(ownership['aborts'])} aborted | {len(ownership['unowned'])} not owned | "
        f"{len(ownership['unresolved'])} unresolved transitions"
    )
    print("=" * 112)
    print(
        f"  {'seq':>4} {'shot':>12} {'path':>17} {'local stage':>12} "
        f"{'fill@rel':>8} {'peak':>6} {'latMs':>6} {'lead':>6} {'window diag':>13}"
    )
    print("  " + "-" * 104)
    for seq in seqs:
        value = shots[seq]
        print(
            f"  {seq:>4} {value.get('shot', '?'):>12} {value.get('code', '?'):>17} "
            f"{_stage_label(value):>12} "
            f"{value.get('fillAtRel', float('nan')):>7.1f}% "
            f"{value.get('peakFill', float('nan')):>5.0f} "
            f"{value.get('latencyMs', float('nan')):>6.0f} "
            f"{value.get('offset', float('nan')):>+6.0f} "
            f"{value.get('window_position', '-'):>13}"
        )

    latency = [
        shots[seq]["latencyMs"] for seq in seqs
        if shots[seq].get("latencyMs", -1.0) > 0.0
    ]
    leads = [shots[seq]["offset"] for seq in seqs if "offset" in shots[seq]]
    release_fill = [
        shots[seq]["fillAtRel"] for seq in seqs
        if shots[seq].get("fillAtRel", -1.0) >= 0.0
    ]
    paths = defaultdict(int)
    stages = defaultdict(int)
    for seq in seqs:
        paths[shots[seq].get("code", "?")] += 1
        stages[shots[seq].get("delivery_stage", "missing")] += 1

    print("  " + "-" * 104)
    print("  AGGREGATE:")
    if latency:
        print(
            f"    observed path latency: median {med(latency):.0f} ms "
            f"(min {min(latency):.0f} / max {max(latency):.0f})"
        )
    if release_fill:
        print(f"    detector fill at command: median {med(release_fill):.0f}%")
    if leads:
        print(
            f"    release lead used: start {leads[0]:+.0f} ms -> "
            f"end {leads[-1]:+.0f} ms"
        )
    print(
        "    release paths: "
        + ", ".join(f"{name}={count}" for name, count in sorted(paths.items()))
    )
    print(
        "    local stages: "
        + ", ".join(f"{name}={count}" for name, count in sorted(stages.items()))
    )
    observed_calibration = [
        seq for seq in seqs if shots[seq].get("latency_observation", False)
    ]
    if observed_calibration:
        print("    latency observations:")
        for seq in observed_calibration:
            value = shots[seq]
            verdict = "ACCEPT" if value.get("latency_accepted") else "REJECT"
            print(
                f"      seq={seq} {verdict} status={value.get('latency_status', '?')} "
                f"reject={value.get('latency_reject', '-')} "
                f"total_ms={value.get('latency_total_ms', '-')} "
                f"n={value.get('latency_n', '?')} freeze={value.get('latency_freeze', '?')} "
                f"target={value.get('latency_target_pct', '-')}% "
                f"stop={value.get('latency_stop_pct', '-')}%"
            )
    for label, key in (
        ("abort reasons", "aborts"),
        ("not-owned reasons", "unowned"),
        ("unresolved reasons", "unresolved"),
    ):
        counts = defaultdict(int)
        for reason in ownership[key]:
            counts[reason] += 1
        if counts:
            print(
                f"    {label}: "
                + ", ".join(
                    f"{name}={count}" for name, count in sorted(counts.items())
                )
            )
    print("=" * 112)
    print("  LOCAL acceptance is not a PS5/game acknowledgement (console_ack=0).")
    print("  TRUE intervention and make/green results must be confirmed from the live game.")
    print("  Detector/authority aborts and not-owned/unresolved entries are launch failures even when every issued release was accepted.")
    print("  Window diagnostics describe detector geometry only; they are not shot outcomes.")


if __name__ == "__main__":
    main()
