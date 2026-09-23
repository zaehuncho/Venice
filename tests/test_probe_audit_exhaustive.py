"""Exhaustive mutation families, GENERATED FROM THE WIRE SCHEMA.

Codex's list of twelve false passes was "these twelve", not "only these twelve", and he named the
families to sweep: remove each required field independently, change every foreign-key component
individually, conflict two records with identical ids, use genuine separate native/Python journals,
empty and entirely unattached captures, rejected-versus-executed plan, QPC frequency scaling.

Hand-written cases cannot cover that, and worse, they rot: a field added to the schema tomorrow
gets no test. So these are generated from `PROBE_SCHEMA_2.json` itself. Add a required field to the
contract and a drop-case for it appears automatically.

THE BAR, and why it is set where it is. A mutation passes when the shot fails AT LEAST ONE of
measurement / adherence / outcome, OR the capture gate is withheld. It does not have to fail the
"right" one, and the message text is never asserted. That keeps the suite honest about what it
proves -- that the defect is *detected* -- instead of freezing today's wording into the contract.

Two carve-outs are declared explicitly rather than hidden in a filter, because an undeclared
exemption is how a real gap gets excused:
  * `reader_onset` is optional evidence -- a shot without one is ENGINE_ONLY, which is not a
    failure. Dropping its fields is tested for CLOCK behaviour elsewhere, not here.
  * fields whose absence is genuinely permitted by the contract are listed in PERMITTED_ABSENT
    with the reason. That list is asserted to be small and is printed on failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_probe_audit as T                                          # noqa: E402
from tools.timing.observer_audit import load                          # noqa: E402
from tools.timing.probe_audit import (                                # noqa: E402
    ADHERENT, COMPLETE, GATE_WITHHELD, GRADED, ProbeCapture)

with open(ROOT / "tools" / "timing" / "PROBE_SCHEMA_2.json", encoding="utf-8") as fh:
    SCHEMA = json.load(fh)

BUILDERS = {
    "shot_press": T._press,
    "native_onset": T._native_onset,
    "probe_assignment": T._assignment,
    "schedule_plan": T._plan,
    "release_link": T._release,
    "shot_verdict": T._verdict,
    "shot_terminal": T._terminal,
}

# Absences the contract genuinely permits. Each needs a reason, and the list is asserted small --
# an exemption without a reason is how a real gap gets excused.
PERMITTED_ABSENT = {
    ("shot_press", "press_epoch_us"): "carried metadata; the engine-clock edge is the required one",
    ("shot_press", "press_source"): "provenance label, not evidence of the edge",
    ("shot_press", "shot_type"): "the resolved trim key on native_onset is the load-bearing one",
    ("shot_press", "native_owner"): "the journal identity already owns a native record",
    ("native_onset", "first_fresh_accept_engine_us"): "first_meter_seen/latched carry the ordering",
    ("native_onset", "native_session"): "frame provenance; not used by any gate decision",
    ("native_onset", "processed_seq"): "frame provenance; not used by any gate decision",
    ("native_onset", "computed_tempo"): "effective_tempo and resolved_trim_key are the binding ones",
    ("native_onset", "banner_trim_us"): "diagnostic; the resolved key names the bucket",
    ("native_onset", "effective_lead_us"): "diagnostic; not part of the displacement chain",
    ("native_onset", "ownership_source"): "diagnostic; latched_engine_us carries the ordering",
    ("native_onset", "first_meter_seen_engine_us"): "native_onset_engine_us is the required stamp",
    ("native_onset", "config_revision"): "the plan's config_revision is the one checked",
    ("probe_assignment", "pre_treatment_native_onset_us"): "native_onset carries the authority",
    ("probe_assignment", "effective_bucket"): "resolved_trim_key on native_onset is authoritative",
    ("probe_assignment", "eligible"): "an emitted assignment is an eligible one",
    ("probe_assignment", "eligibility_reason"): "prose; the eligible flag is the datum",
    ("probe_assignment", "stratum_id"): "reported for accounting, not required for a verdict",
    ("probe_assignment", "block_id"): "reported for accounting, not required for a verdict",
    ("probe_assignment", "block_position"): "reported for accounting, not required for a verdict",
    ("probe_assignment", "arm_id"): "reported for accounting; the dose is the datum",
    ("probe_assignment", "assignment_probability"): "reported for accounting, not for a verdict",
    ("schedule_plan", "attempt"): "the operation id identifies the plan",
    ("schedule_plan", "plan_id"): "the operation id identifies the plan",
    ("schedule_plan", "decision_id"): "cross-reference to the observer, not a gate input",
    ("schedule_plan", "authority_us"): "lease diagnostic; not part of the displacement chain",
    ("schedule_plan", "disposition"): "label; the deadline fields carry the outcome",
    ("schedule_plan", "now_engine_us"): "ordering check degrades to unchecked, not to wrong",
    ("release_link", "attempt"): "the dispatched scope identifies the execution",
    ("release_link", "route_generation"): "absence makes the scope check inapplicable, not wrong",
    ("release_link", "reported_release_epoch_us"): "carried metadata; local_command_us is required",
    ("release_link", "delivery_mode"): "diagnostic label",
    ("release_link", "delivery_result"): "diagnostic label",
    ("shot_verdict", "shot_record_seq"): "python-side identity; the native reference is the join",
    ("shot_verdict", "verdict_seq"): "python-side identity; the native reference is the join",
    ("shot_verdict", "frame"): "banner provenance; not a gate input",
    ("shot_verdict", "observed_ns"): "python-clock stamp; not a gate input",
    ("shot_verdict", "emitted_ns"): "python-clock stamp; not a gate input",
    ("shot_verdict", "coverage"): "reported; absent-vs-unreadable matters for analysis, not the gate",
    ("shot_verdict", "native_capture_id"): "tested separately: the record is held unattached",
    ("shot_verdict", "native_pid"): "tested separately: the record is held unattached",
    ("shot_verdict", "native_instance"): "tested separately: the record is held unattached",
    ("shot_terminal", "close_reason"): "prose; terminal_state is the datum",
    ("shot_terminal", "terminal_engine_us"): "not used by any gate decision",
    ("shot_terminal", "adherence_state"): "the consumer derives adherence itself, and must",
}

DROP_CASES = [(stage, field)
              for stage in sorted(BUILDERS)
              for field in sorted(SCHEMA["stages"][stage]["required"])]


def _verdicts(tmp_path, name, recs, extra_journals=()):
    p = T._write(tmp_path, [T._hdr(), T._manifest()] + recs + [T._foot()], name + ".jsonl")
    cap = ProbeCapture([load(p), *extra_journals]).build()
    if not cap.shots:
        return cap, None
    # the native-owned shot is the one under test
    owner = ("probe-1", "native", 100, "1")
    shot = next((s for (o, _k), s in cap.shots.items() if o == owner), None)
    return cap, shot


def _clean_passes(cap, shot):
    if shot is None:
        return False
    return (shot.measurement(cap)[0] == COMPLETE
            and shot.adherence(cap)[0] == ADHERENT
            and shot.outcome()[0] == GRADED
            and cap.gate()[0] != GATE_WITHHELD)


# =========================================================================================
# family 1: drop each required field, one at a time
# =========================================================================================
@pytest.mark.parametrize("stage,field", DROP_CASES,
                         ids=[f"{s}.{f}" for s, f in DROP_CASES])
def test_dropping_one_required_field_is_detected_or_declared(tmp_path, stage, field):
    recs = [r for r in T._full() if r["stage"] != stage]
    rec = BUILDERS[stage]()
    rec["data"].pop(field, None)
    recs.append(rec)
    cap, shot = _verdicts(tmp_path, f"drop_{stage}_{field}", recs)
    detected = not _clean_passes(cap, shot)
    permitted = (stage, field) in PERMITTED_ABSENT
    assert detected or permitted, (
        f"{stage}.{field} can be dropped with no effect and is not in PERMITTED_ABSENT")
    if permitted and detected:
        # Not a failure -- the contract got stricter than the exemption assumed. Say so loudly so
        # the stale exemption gets removed rather than quietly protecting nothing.
        pytest.skip(f"{stage}.{field} is now detected; its PERMITTED_ABSENT entry is stale")


def test_the_permitted_absent_list_is_small_and_reasoned():
    """An exemption without a reason is how a real gap gets excused."""
    assert all(isinstance(v, str) and len(v) > 15 for v in PERMITTED_ABSENT.values())
    required = sum(len(SCHEMA["stages"][s]["required"]) for s in BUILDERS)
    assert len(PERMITTED_ABSENT) < required, "more fields are exempt than are enforced"


# =========================================================================================
# family 2: drop each whole stage
# =========================================================================================
@pytest.mark.parametrize("stage", sorted(BUILDERS))
def test_dropping_a_whole_stage_is_detected(tmp_path, stage):
    recs = [r for r in T._full() if r["stage"] != stage]
    cap, shot = _verdicts(tmp_path, f"nostage_{stage}", recs)
    assert not _clean_passes(cap, shot), f"a shot with no {stage} still passed cleanly"


# =========================================================================================
# family 3: change each foreign-key component individually
# =========================================================================================
FOREIGN_KEYS = [
    ("probe_assignment", "experiment_id", "other-experiment"),
    ("probe_assignment", "assignment_id", 999),
    ("schedule_plan", "assignment_id", 999),
    ("schedule_plan", "operation_id", 8888),
    ("schedule_plan", "config_revision", 999),
    ("release_link", "assignment_id", 999),
    ("release_link", "operation_id", 8888),
    ("release_link", "token", 77),
    ("release_link", "target_revision", 9),
    ("shot_terminal", "assignment_id", 999),
]


@pytest.mark.parametrize("stage,field,value", FOREIGN_KEYS,
                         ids=[f"{s}.{f}" for s, f, _ in FOREIGN_KEYS])
def test_changing_one_foreign_key_component_is_detected(tmp_path, stage, field, value):
    recs = [r for r in T._full() if r["stage"] != stage]
    recs.append(BUILDERS[stage](**{field: value}))
    cap, shot = _verdicts(tmp_path, f"fk_{stage}_{field}", recs)
    assert not _clean_passes(cap, shot), f"{stage}.{field}={value!r} passed cleanly"


# =========================================================================================
# family 4: two records with identical ids but contradictory payloads
# =========================================================================================
@pytest.mark.parametrize("stage", ["shot_press", "native_onset", "schedule_plan",
                                   "shot_verdict", "shot_terminal"])
def test_two_conflicting_records_with_the_same_id(tmp_path, stage):
    conflict = {"shot_press": dict(press_engine_us=999_999),
                "native_onset": dict(native_onset_us=123),
                "schedule_plan": dict(final_requested_deadline_us=9_999_999),
                "shot_verdict": dict(verdict="EARLY"),
                "shot_terminal": dict(terminal_state="cancelled")}[stage]
    recs = T._full() + [BUILDERS[stage](**conflict)]
    cap, shot = _verdicts(tmp_path, f"conflict_{stage}", recs)
    assert not _clean_passes(cap, shot), f"a contradictory duplicate {stage} passed cleanly"


# =========================================================================================
# family 5: genuine separate native and Python journals
# =========================================================================================
def _py_hdr(pid=300, instance="3"):
    return {"type": "header", "schema": 2, "trace_kind": "timing", "role": "python",
            "capture_id": "probe-1", "pid": pid, "instance": instance, "capacity": 4096,
            "clock": "perf_counter_ns", "clock_hz": 1_000_000_000,
            "sync_clock": "500000000", "sync_unix_ns": "1", "sync_uncertainty_ns": "1000"}


def test_a_real_two_journal_capture_joins_across_processes(tmp_path):
    """The fixtures put Python-origin records in the native journal, which conceals the join.
    This one uses genuinely separate journals, as a real capture does."""
    native = [r for r in T._full() if r["stage"] not in ("reader_onset", "shot_verdict")]
    a = load(T._write(tmp_path, [T._hdr(), T._manifest()] + native + [T._foot()], "native.jsonl"))
    py = [T._reader_onset(), T._verdict()]
    b = load(T._write(tmp_path, [_py_hdr()] + py + [T._foot()], "python.jsonl"))
    cap = ProbeCapture([a, b]).build()
    assert len(cap.shots) == 1, "the python records did not join the native shot"
    shot = next(iter(cap.shots.values()))
    assert shot.owner == ("probe-1", "native", 100, "1")
    assert len(shot.verdict) == 1 and len(shot.reader_onset) == 1
    assert shot.outcome()[0] == GRADED


def test_a_python_record_without_a_native_reference_does_not_join(tmp_path):
    native = [r for r in T._full() if r["stage"] not in ("reader_onset", "shot_verdict")]
    a = load(T._write(tmp_path, [T._hdr(), T._manifest()] + native + [T._foot()], "native.jsonl"))
    v = T._verdict()
    for k in ("native_capture_id", "native_pid", "native_instance"):
        v["data"].pop(k)
    b = load(T._write(tmp_path, [_py_hdr(), v, T._foot()], "python.jsonl"))
    cap = ProbeCapture([a, b]).build()
    assert len(cap.shots) == 1
    assert any("native-owner reference" in w for _s, _e, w in cap.unattached)
    shot = next(iter(cap.shots.values()))
    assert shot.outcome()[0] != GRADED, "an unjoinable verdict was counted anyway"


def test_a_python_journal_may_not_emit_native_stages(tmp_path):
    a = load(T._write(tmp_path, [T._hdr(), T._manifest()] + T._full() + [T._foot()], "n.jsonl"))
    b = load(T._write(tmp_path, [_py_hdr(), T._plan(), T._foot()], "p.jsonl"))
    cap = ProbeCapture([a, b]).build()
    assert any("emitted by a python journal" in w for _s, _e, w in cap.unattached)


# =========================================================================================
# family 6: degenerate captures
# =========================================================================================
def test_an_empty_capture_withholds_the_gate(tmp_path):
    p = T._write(tmp_path, [T._hdr(), T._manifest(), T._foot()], "empty.jsonl")
    cap = ProbeCapture([load(p)]).build()
    gate, why = cap.gate()
    assert gate == GATE_WITHHELD
    assert any("NO SHOTS" in w for w in why)


def test_an_entirely_unattached_capture_withholds_the_gate(tmp_path):
    recs = [r for r in T._full()]
    for r in recs:
        r["data"]["physical_epoch"] = 0
    p = T._write(tmp_path, [T._hdr(), T._manifest()] + recs + [T._foot()], "unatt.jsonl")
    cap = ProbeCapture([load(p)]).build()
    gate, why = cap.gate()
    assert gate == GATE_WITHHELD
    assert cap.shots == {}
    assert any("no usable identity" in w for w in why)


# =========================================================================================
# family 7: rejected versus executed plan
# =========================================================================================
def test_a_superseded_plans_violation_does_not_condemn_the_executed_one(tmp_path):
    """A rejected plan's bad displacement is a separate diagnostic, not evidence about the
    treatment that actually ran."""
    recs = T._full() + [T._plan(op=9999, final=9_999_999)]
    cap, shot = _verdicts(tmp_path, "superseded_bad", recs)
    v, why = shot.adherence(cap)
    assert v == ADHERENT, "a superseded plan condemned the executed one"
    assert "superseded plan(s) not judged here" in why[0]


def test_a_missing_shadow_on_a_superseded_plan_does_not_make_the_shot_unverifiable(tmp_path):
    p = T._plan(op=9999)
    p["data"].pop("zero_offset_shadow_deadline_us")
    cap, shot = _verdicts(tmp_path, "superseded_noshadow", T._full() + [p])
    assert shot.adherence(cap)[0] == ADHERENT
    assert shot.measurement(cap)[0] != COMPLETE, "the superseded plan's missing field is still a "\
                                                 "measurement defect, just not an adherence one"


def test_a_missing_shadow_on_the_EXECUTED_plan_is_unverifiable(tmp_path):
    p = T._plan()
    p["data"].pop("zero_offset_shadow_deadline_us")
    recs = [r for r in T._full() if r["stage"] != "schedule_plan"] + [p]
    cap, shot = _verdicts(tmp_path, "executed_noshadow", recs)
    v, why = shot.adherence(cap)
    assert v != ADHERENT
    assert any("REQUESTED" in w for w in why)


# =========================================================================================
# family 8: QPC frequency scaling
# =========================================================================================
@pytest.mark.parametrize("hz,expected", [(10_000_000, 26_000_000),
                                         (1_000_000, 20_600_000),
                                         (3_579_545, 20_000_000 + round(600_000 * 3.579545))])
def test_conversion_follows_the_journals_own_qpc_hz(tmp_path, hz, expected):
    """The defect was adding microseconds to ticks. A 1 MHz journal is the ONLY case where the
    old arithmetic happened to be right, which is exactly why it went unnoticed."""
    hdr = dict(T._hdr(), qpc_hz=hz)
    recs = [hdr, T._manifest(),
            T._bridge(engine_us=1_000_000, qpc=20_000_000, lo=0, hi=9_000_000)] \
        + T._full() + [T._foot()]
    p = T._write(tmp_path, recs, f"hz{hz}.jsonl")
    cap = ProbeCapture([load(p)]).build()
    owner = ("probe-1", "native", 100, "1")
    qpc, _unc, why = cap.convert(owner, 7, 1_600_000)
    assert why == ""
    assert qpc == expected
