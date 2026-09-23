"""Adversarial fixtures for tools/timing/probe_audit.py -- the probe schema 2 measurement gate.

Contract: tools/timing/PROBE_SCHEMA_2.md. Every case below is one of the failures Codex named when
he specified the gate, plus the ones this project has already committed once:

  * a join made on a nearest timestamp, on a token alone, or across the two DIFFERENT counters both
    historically called `release_seq`;
  * an assignment that does not survive an attempt or token change inside one physical shot;
  * eligibility or onset determined AFTER treatment;
  * REQUESTED displacement substituted for EFFECTIVE displacement, when `scheduleFire` phase-locks
    after the hook and can clip an offset targeting the past;
  * a missing grade read as EXCELLENT;
  * a non-adherent assignment quietly dropped from the accounting.

The last one is the reason the consumer reports three verdicts instead of one. A single "valid shot"
filter is exactly how a biased estimate gets built out of a correct-looking pipeline, so there are
tests here that assert the three stay SEPARATE.

THE PRODUCER DOES NOT EXIST YET. These fixtures are the contract the producer must satisfy; they
exist so the gate cannot be declared passed by assertion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.timing.observer_audit import load  # noqa: E402
from tools.timing.probe_audit import (  # noqa: E402
    ADHERENT, CLOCK_ENGINE_ONLY, CLOCK_OK, CLOCK_REFUSED, COMPLETE, DISPUTED, GATE_PASS,
    GATE_WITHHELD, GRADED, INCOMPLETE, NONADHERENT, ProbeCapture, UNGRADED, UNKNOWN,
    UNVERIFIABLE,
)

QPC_HZ = 10_000_000
CTX = "7"
EPOCH = 42


def _hdr(pid=100, instance="1", capture_id="probe-1"):
    return {"type": "header", "schema": 2, "trace_kind": "timing", "role": "native",
            "capture_id": capture_id, "pid": pid, "instance": instance, "capacity": 4096,
            "qpc_hz": QPC_HZ, "sync_qpc": "1000", "sync_unix_us": "1",
            "sync_uncertainty_qpc": "10"}


def _foot(dropped_full=0):
    return {"type": "footer", "now_us": "999999", "reason": "shutdown", "pending": 0,
            "dropped_full": str(dropped_full), "dropped_contention": "0", "dropped_clock": "0"}


def _ev(stage, data, context=CTX):
    return {"type": "event", "stage": stage, "context": context, "generation": "9",
            "epoch": None, "source_seq": None, "data": dict(data), "dropped_total": "0"}


def _manifest(**over):
    d = {"experiment_id": "probe-gate-1", "protocol_version": 1, "build_hash": "abc123",
         "schema_hash": "def456", "config_revision": 11, "max_abs_offset_us": 25000,
         "randomisation_seed": 20260920, "arms_us": "-25000,0,25000",
         "learning_freeze_mask": "aim,banner_trim,oracle,latency",
         "eligibility": "standstill_online", "stopping_rule": "fixed_180",
         "clock_tolerance_us": 1000, "physical_epoch": 0}
    d.update(over)
    return _ev("experiment_manifest", d)


# ---------------------------------------------------------------------------------------
# One consistent timeline. Every builder derives from it, so a fixture cannot quietly hold a
# plan deadline that disagrees with the worker records -- which is exactly how the previous
# _full(delta=0) expected ADHERENT while its plan said 1_900_000 and its worker said 1_925_000.
#
#   press 1_000_000 -> onset latched 1_600_000 -> eligibility fixed 1_620_000
#   -> assignment 1_650_000 -> plan now 1_700_000
#   -> shadow deadline 1_900_000, final = shadow + dose
#   -> worker accepts `final`, claims `final`, dispatches at final + 400
# ---------------------------------------------------------------------------------------
PRESS_US, LATCH_US, ELIG_US, ASSIGN_US, PLAN_NOW_US = (
    1_000_000, 1_600_000, 1_620_000, 1_650_000, 1_700_000)
SHADOW_US = 1_900_000
COMMAND_LAG_US = 400


def _deadline(delta=25000):
    return SHADOW_US + delta


def _press(epoch=EPOCH, **over):
    d = {"physical_epoch": epoch, "press_engine_us": PRESS_US,
         "press_epoch_us": 1_789_940_000_000, "press_source": "square_edge",
         "shot_type": "Standstill", "native_owner": "nat-1"}
    d.update(over)
    return _ev("shot_press", d)


def _native_onset(epoch=EPOCH, onset_us=LATCH_US, **over):
    d = {"physical_epoch": epoch, "attempt": 1,
         "first_fresh_accept_engine_us": onset_us, "first_meter_seen_engine_us": onset_us,
         "native_onset_engine_us": onset_us, "latched_engine_us": onset_us,
         "native_onset_us": onset_us - PRESS_US,
         "valid": 1, "ownership_source": "fresh_accept", "native_session": 3,
         "processed_seq": 1201, "computed_tempo": "slow", "effective_tempo": "slow",
         "resolved_trim_key": "Standstill/slow", "banner_trim_us": 6000,
         "effective_lead_us": 280_000, "config_revision": 11}
    d.update(over)
    return _ev("native_onset", d)


def _reader_onset(epoch=EPOCH, **over):
    """Emitted by a PYTHON journal in real life, so it carries the native-owner reference."""
    d = {"physical_epoch": epoch, "shot_record_seq": 7, "press_epoch_us": 1_789_940_000_000,
         "accepted_capture_epoch_us": 1_789_940_540_000, "reader_onset_us": 540_000,
         "reader_processing_ns": "500000000", "onset_clock": "capture_wall",
         "structure_verified": 1, "frame": 1201, "source_generation": 1,
         "processed_seq": 1201, "acceptance_version": 2,
         "native_capture_id": "probe-1", "native_pid": 100, "native_instance": "1"}
    d.update(over)
    return _ev("reader_onset", d)


def _arm_for(delta):
    """The arm LABEL must agree with the dose. A fixture whose label contradicts its own
    treatment is the randomisation record and the treatment record disagreeing -- exactly the
    corruption the consumer now rejects, so the fixture must not carry it by accident."""
    if delta == 0:
        return "zero"
    return ("plus" if delta > 0 else "minus") + str(abs(delta) // 1000)


def _assignment(epoch=EPOCH, delta=25000, assigned_at=ASSIGN_US, **over):
    d = {"physical_epoch": epoch, "assignment_id": 501, "experiment_id": "probe-gate-1",
         "pre_treatment_native_onset_us": LATCH_US - PRESS_US, "effective_bucket": "slow",
         "eligible": 1, "eligibility_reason": "ok", "stratum_id": 2, "block_id": 10,
         "block_position": 3, "arm_id": _arm_for(delta), "assignment_probability": "1/3",
         "assigned_delta_us": delta, "assigned_engine_us": assigned_at,
         "eligibility_fixed_engine_us": ELIG_US}
    d.update(over)
    return _ev("probe_assignment", d)


def _plan(epoch=EPOCH, delta=25000, shadow=SHADOW_US, final=None, clipped=0, op=9001, **over):
    final = (shadow + delta) if final is None else final
    d = {"physical_epoch": epoch, "assignment_id": 501, "attempt": 1, "plan_id": 77,
         "operation_id": op, "decision_id": 55, "now_engine_us": PLAN_NOW_US,
         "baseline_pre_offset_deadline_us": shadow, "assigned_delta_us": delta,
         "post_offset_deadline_us": shadow + delta, "clipped_past_deadline": clipped,
         "post_phase_lock_deadline_us": final, "final_requested_deadline_us": final,
         "zero_offset_shadow_deadline_us": shadow, "handoff_deadline_us": final,
         "authority_us": 2_500_000, "config_revision": 11, "disposition": "armed"}
    d.update(over)
    return _ev("schedule_plan", d)


def _worker(epoch=EPOCH, event=1, result=0, op=9001, deadline=None, delta=25000, **over):
    deadline = _deadline(delta) if deadline is None else deadline
    d = {"physical_epoch": epoch, "token": 6, "event": event, "result": result,
         "operation_id": op, "plan_id": 77, "requested_deadline_us": deadline,
         "accepted_deadline_us": deadline, "target_revision": 4}
    d.update(over)
    return _ev("worker", d)


def _claim(epoch=EPOCH, rev=4, deadline=None, delta=25000, op=9001, **over):
    deadline = _deadline(delta) if deadline is None else deadline
    d = {"physical_epoch": epoch, "token": 6, "event": 2, "operation_id": op, "plan_id": 77,
         "target_revision": rev, "effective_deadline_us": deadline, "remaining_us": 3200}
    d.update(over)
    return _ev("worker", d)


def _dispatch(epoch=EPOCH, rev=4, command_us=None, delta=25000, op=9001, **over):
    command_us = (_deadline(delta) + COMMAND_LAG_US) if command_us is None else command_us
    d = {"physical_epoch": epoch, "token": 6, "event": 3, "result": 1, "operation_id": op,
         "plan_id": 77, "target_revision": rev, "command_us": command_us,
         "complete_us": command_us + 200, "route": 1, "delivery_stage": 1}
    d.update(over)
    return _ev("worker", d)


def _retarget(epoch=EPOCH, result=0, rev=5, deadline=1_928_000, op=9002, **over):
    d = {"physical_epoch": epoch, "token": 6, "event": 4, "result": result, "operation_id": op,
         "plan_id": 77, "target_revision": rev, "accepted_deadline_us": deadline,
         "requested_deadline_us": deadline}
    d.update(over)
    return _ev("worker", d)


def _release(epoch=EPOCH, delta=25000, **over):
    d = {"physical_epoch": epoch, "attempt": 1, "assignment_id": 501, "token": 6,
         "route_generation": 9, "target_revision": 4, "operation_id": 9001,
         "native_release_seq": 118, "local_command_us": _deadline(delta) + COMMAND_LAG_US,
         "reported_release_epoch_us": 1_789_940_925_400, "delivery_mode": "scheduled_worker",
         "delivery_result": "ok"}
    d.update(over)
    return _ev("release_link", d)


def _verdict(epoch=EPOCH, verdict="LATE", **over):
    """Emitted by a PYTHON journal in real life, so it carries the native-owner reference."""
    d = {"physical_epoch": epoch, "shot_record_seq": 7, "verdict_seq": 7, "verdict": verdict,
         "attribution": "attributed", "coverage": "absent", "frame": 1330,
         "observed_ns": "501000000", "emitted_ns": "501100000",
         "native_capture_id": "probe-1", "native_pid": 100, "native_instance": "1"}
    d.update(over)
    return _ev("shot_verdict", d)


def _terminal(epoch=EPOCH, state="released", **over):
    d = {"physical_epoch": epoch, "assignment_id": 501, "terminal_state": state,
         "close_reason": "complete", "terminal_engine_us": 2_500_000,
         "adherence_state": "as_assigned"}
    d.update(over)
    return _ev("shot_terminal", d)


def _full(epoch=EPOCH, delta=25000, verdict="LATE", state="released"):
    """A clean, complete, adherent, graded shot with a full, INTERNALLY CONSISTENT chain.

    Worker deadlines are derived from the plan, so changing the dose cannot leave the fixture
    contradicting itself -- the previous version's _full(delta=0) did exactly that and still
    asserted ADHERENT.
    """
    return [_press(epoch), _native_onset(epoch), _reader_onset(epoch),
            _assignment(epoch, delta), _plan(epoch, delta),
            _worker(epoch, delta=delta), _claim(epoch, delta=delta), _dispatch(epoch, delta=delta),
            _release(epoch, delta=delta), _verdict(epoch, verdict), _terminal(epoch, state)]


def _write(tmp_path, records, name="native.jsonl"):
    nid = 0
    out = []
    for r in records:
        r = dict(r)
        if r.get("type") == "event":
            nid += 1
            r.setdefault("id", str(nid))
            r.setdefault("qpc", str(1_000_000 + nid * 16667))
        out.append(r)
    for r in out:
        if r.get("type") == "footer":
            r["accepted"] = str(nid)
            r["written"] = str(nid)
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    return p


def _cap(tmp_path, records, name="native.jsonl"):
    return ProbeCapture([load(_write(tmp_path, records, name))]).build()


def _one(cap):
    assert len(cap.shots) == 1, sorted(cap.shots)
    return next(iter(cap.shots.values()))


# ===========================================================================================
# the control
# ===========================================================================================
def test_a_clean_shot_is_complete_adherent_and_graded(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_foot()])
    s = _one(cap)
    assert cap.manifest_flags() == []
    assert s.measurement(cap) == (COMPLETE, [])
    assert s.adherence(cap)[0] == ADHERENT
    assert s.outcome()[0] == GRADED
    assert s.outcome()[2] == "LATE"


def test_the_three_verdicts_are_reported_separately(tmp_path):
    """A non-adherent shot must still be MEASURED and GRADED, not filtered out of existence.

    The shot below is fully measured and internally consistent -- the worker chain follows the
    plan's actual final deadline. ONLY the scheduling displacement is wrong: the plan delivered
    +5000 us where +25000 us was assigned. That is the case that must survive in the report.
    """
    bad = 1_900_000 + 5000
    recs = [_press(), _native_onset(), _reader_onset(), _assignment(delta=25000),
            _plan(final=bad), _worker(deadline=bad), _claim(deadline=bad),
            _dispatch(command_us=bad + 400), _release(), _verdict(), _terminal()]
    recs = [r for r in recs if r["stage"] != "release_link"]
    recs.append(_release(**{"local_command_us": bad + 400}))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    assert s.measurement(cap) == (COMPLETE, [])
    assert s.adherence(cap)[0] == NONADHERENT
    assert s.outcome()[0] == GRADED, "a non-adherent shot still has an outcome"
    row = cap.report()[0]
    assert (row["measurement"], row["adherence"], row["outcome"]) == (COMPLETE, NONADHERENT, GRADED)
    assert row["arm_id"] == "plus25" and row["assigned_delta_us"] == 25000, (
        "a non-adherent row without its treatment label is useless for intention-to-treat")


# ===========================================================================================
# identity
# ===========================================================================================
@pytest.mark.parametrize("over,fragment", [
    ({"physical_epoch": 0}, "physical_epoch is 0 (unset)"),
    ({"physical_epoch": None}, "physical_epoch missing"),
])
def test_a_record_with_no_shot_identity_is_held_not_guessed(tmp_path, over, fragment):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_verdict(**over)] + [_foot()])
    assert len(cap.shots) == 1, "the unidentifiable record was attached to the live shot"
    assert len(cap.unattached) == 1
    assert fragment in cap.unattached[0][2]


def test_a_null_engine_context_is_not_an_identity(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full()
               + [_verdict(verdict="EARLY")] + [_foot()])
    # rewrite the extra verdict's context to 0 by rebuilding
    recs = [_hdr(), _manifest()] + _full() + [_ev("shot_verdict", _verdict()["data"], context="0")]
    cap = _cap(tmp_path, recs + [_foot()], name="b.jsonl")
    assert len(cap.shots) == 1
    assert any("engine context is 0 (null)" in w for _s, _e, w in cap.unattached)


def test_the_same_epoch_in_two_processes_is_two_shots(tmp_path):
    a = load(_write(tmp_path, [_hdr(pid=100, instance="1"), _manifest()] + _full() + [_foot()],
                    "a.jsonl"))
    b = load(_write(tmp_path, [_hdr(pid=200, instance="2"), _manifest()] + _full() + [_foot()],
                    "b.jsonl"))
    cap = ProbeCapture([a, b]).build()
    assert len(cap.shots) == 2, "two processes reusing physical_epoch 42 were merged"


def test_release_seq_by_that_name_is_refused_even_when_the_value_is_right(tmp_path):
    """`physical_epoch` and `native_release_seq` have both been called `release_seq`."""
    rel = _release()
    rel["data"].pop("native_release_seq")
    rel["data"]["release_seq"] = 118
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full()[:-3] + [rel, _verdict(), _terminal()]
               + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("AMBIGUOUS name `release_seq`" in w for w in why)
    assert any("physical_epoch" in w and "native_release_seq" in w for w in why)


# ===========================================================================================
# assignment must survive attempt / token changes
# ===========================================================================================
def test_an_assignment_that_changes_within_one_shot_is_refused(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full()
               + [_assignment(assignment_id=502, delta=-25000)] + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("ASSIGNMENT CHANGED within one physical shot" in w for w in why)


def test_a_second_attempt_keeps_the_same_assignment(tmp_path):
    """Re-arming is normal. The assignment must be the SAME one, not a redraw."""
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full()
               + [_native_onset(onset_us=1_650_000, attempt=2),
                  _plan(op=9002, attempt=2)] + [_foot()])
    s = _one(cap)
    assert s.measurement(cap)[0] == COMPLETE
    assert s.adherence(cap)[0] == ADHERENT


def test_duplicate_onset_for_one_attempt_is_ambiguous(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_native_onset()] + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("duplicate native_onset" in w for w in why)


# ===========================================================================================
# eligibility must precede treatment
# ===========================================================================================
def test_eligibility_fixed_before_onset_was_latched_is_refused(tmp_path):
    """Eligibility known before the onset that defines it is eligibility decided after the fact.

    Onset OCCURRENCE is not when eligibility became known -- a promoted seed's first-meter
    timestamp is backdated -- so `latched_engine_us` is the one that may be compared.
    """
    recs = [r for r in _full() if r["stage"] != "probe_assignment"]
    recs.append(_assignment(**{"eligibility_fixed_engine_us": LATCH_US - 50_000}))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("ELIGIBILITY FIXED BEFORE ONSET WAS LATCHED" in w for w in why)


def test_an_assignment_committed_before_eligibility_is_refused(tmp_path):
    recs = [r for r in _full() if r["stage"] != "probe_assignment"]
    recs.append(_assignment(assigned_at=ELIG_US - 10_000))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("ASSIGNMENT COMMITTED BEFORE ELIGIBILITY" in w
               for w in _one(cap).measurement(cap)[1])


def test_a_plan_before_its_assignment_is_refused(tmp_path):
    recs = [r for r in _full() if r["stage"] != "schedule_plan"]
    recs.append(_plan(**{"now_engine_us": ASSIGN_US - 10_000}))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("precedes the assignment" in w for w in _one(cap).measurement(cap)[1])


def test_a_shot_with_no_native_onset_cannot_name_its_bucket(tmp_path):
    recs = [r for r in _full() if r["stage"] != "native_onset"]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("the bucket actually used is unknown" in w for w in why)


# ===========================================================================================
# requested displacement is NOT effective displacement
# ===========================================================================================
def test_without_a_shadow_deadline_adherence_is_unverifiable(tmp_path):
    p = _plan()
    p["data"].pop("zero_offset_shadow_deadline_us")
    recs = [r for r in _full() if r["stage"] != "schedule_plan"] + [p]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).adherence(cap)
    assert v == UNVERIFIABLE
    assert any("REQUESTED" in w and "EFFECTIVE" in w for w in why)


def test_a_clipped_offset_is_nonadherent_even_if_the_numbers_agree(tmp_path):
    """scheduleFire can clip an offset targeting the past. Clipping is a treatment failure."""
    recs = [r for r in _full() if r["stage"] != "schedule_plan"] + [_plan(clipped=1)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).adherence(cap)
    assert v == NONADHERENT
    assert any("CLIPPED" in w for w in why)


def test_phase_lock_moving_the_deadline_shows_up_as_nonadherence(tmp_path):
    """Phase lock pushed the deadline 9 ms beyond the assigned dose. The execution evidence is
    consistent with that; only the treatment fidelity fails."""
    moved = 1_900_000 + 25000 + 9000
    recs = [r for r in _full() if r["stage"] not in ("schedule_plan", "worker", "release_link")]
    recs += [_plan(final=moved), _worker(deadline=moved), _claim(deadline=moved),
             _dispatch(command_us=moved + 400), _release(**{"local_command_us": moved + 400})]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    assert s.measurement(cap)[0] == COMPLETE
    v, why = s.adherence(cap)
    assert v == NONADHERENT
    assert any("effective scheduling displacement" in w for w in why)


def test_a_two_millisecond_error_is_still_adherent(tmp_path):
    recs = [r for r in _full() if r["stage"] != "schedule_plan"]
    recs.append(_plan(final=1_900_000 + 25000 + 2000))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert _one(cap).adherence(cap)[0] == ADHERENT


def test_a_zero_offset_assignment_is_still_an_assignment(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full(delta=0) + [_foot()])
    s = _one(cap)
    assert s.measurement(cap)[0] == COMPLETE
    assert s.adherence(cap)[0] == ADHERENT, "zero-offset control arms must be accounted for"


def test_a_shot_with_no_assignment_is_outside_the_accounting(tmp_path):
    recs = [r for r in _full() if r["stage"] != "probe_assignment"]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    assert s.measurement(cap)[0] == INCOMPLETE
    assert s.adherence(cap)[0] == UNVERIFIABLE


# ===========================================================================================
# outcomes -- a missing grade is not EXCELLENT
# ===========================================================================================
def test_a_missing_grade_is_ungraded_not_excellent(tmp_path):
    recs = [r for r in _full() if r["stage"] != "shot_verdict"]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why, grade = _one(cap).outcome()
    assert v == UNGRADED
    assert grade is None
    assert "no shot_verdict" in why[0]


def test_a_cancelled_shot_stays_in_the_accounting(tmp_path):
    recs = [r for r in _full() if r["stage"] not in ("shot_verdict", "shot_terminal")]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_terminal(state="cancelled")] + [_foot()])
    v, why, _ = _one(cap).outcome()
    assert v == UNGRADED
    assert "terminal=cancelled" in why[0]
    assert len(cap.report()) == 1, "the cancelled shot vanished from the report"


def test_two_contradictory_verdicts_are_disputed(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_verdict(verdict="EARLY")] + [_foot()])
    v, why, _ = _one(cap).outcome()
    assert v == DISPUTED
    assert "contradictory verdicts" in why[0]


def test_an_unattributed_verdict_is_disputed(tmp_path):
    recs = [r for r in _full() if r["stage"] != "shot_verdict"]
    recs.append(_verdict(attribution="unresolved"))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why, _ = _one(cap).outcome()
    assert v == DISPUTED
    assert "attribution unresolved" in why[0]


def test_a_grade_on_a_cancelled_shot_is_disputed(tmp_path):
    recs = [r for r in _full() if r["stage"] != "shot_terminal"]
    recs.append(_terminal(state="cancelled"))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why, _ = _one(cap).outcome()
    assert v == DISPUTED
    assert "terminal_state=cancelled" in why[0]


def test_a_record_that_never_closed_is_unknown(tmp_path):
    recs = [r for r in _full() if r["stage"] != "shot_terminal"]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why, _ = _one(cap).outcome()
    assert v == UNKNOWN
    assert "never closed" in why[0]


def test_an_unknown_terminal_state_is_not_silently_accepted(tmp_path):
    recs = [r for r in _full() if r["stage"] != "shot_terminal"]
    recs.append(_terminal(state="probably_fine"))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert _one(cap).outcome()[0] == UNKNOWN


# ===========================================================================================
# the manifest anchors everything
# ===========================================================================================
def test_no_manifest_is_a_capture_level_refusal(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _full() + [_foot()])
    assert cap.manifest_flags() == ["NO_EXPERIMENT_MANIFEST"]


@pytest.mark.parametrize("missing", ["experiment_id", "build_hash", "randomisation_seed",
                                     "learning_freeze_mask", "stopping_rule"])
def test_a_manifest_missing_a_required_field_is_flagged(tmp_path, missing):
    cap = _cap(tmp_path, [_hdr(), _manifest(**{missing: None})] + _full() + [_foot()])
    assert f"MANIFEST_MISSING:{missing}" in cap.manifest_flags()


@pytest.mark.parametrize("mask,missing", [
    ("aim,latency", "banner_trim"),
    ("aim,banner_trim,latency", "oracle"),
])
def test_the_freeze_must_cover_banner_trim_and_oracle(tmp_path, mask, missing):
    """Freezing only the paths the dev hook already fenced is not freezing the loop under test."""
    cap = _cap(tmp_path, [_hdr(), _manifest(learning_freeze_mask=mask)] + _full() + [_foot()])
    assert f"LEARNING_NOT_FROZEN:{missing}" in cap.manifest_flags()


def test_a_bound_above_the_hook_limit_is_refused(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest(max_abs_offset_us=40000)] + _full() + [_foot()])
    assert any("BOUND_EXCEEDS_HOOK_LIMIT" in f for f in cap.manifest_flags())


def test_contradictory_manifests_are_flagged(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest(), _manifest(config_revision=12)]
               + _full() + [_foot()])
    assert "CONTRADICTORY_MANIFESTS" in cap.manifest_flags()


def test_identical_repeated_manifests_are_fine(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest(), _manifest()] + _full() + [_foot()])
    assert "CONTRADICTORY_MANIFESTS" not in cap.manifest_flags()


# ===========================================================================================
# a lossy capture cannot pass the gate
# ===========================================================================================
def test_a_lossy_capture_blocks_measurement(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_foot(dropped_full=3)])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("lossy or incomplete" in w for w in why)


def test_a_lossy_capture_does_not_erase_the_outcome(tmp_path):
    """Refusing a measurement claim is not refusing the evidence that was recorded."""
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_foot(dropped_full=3)])
    assert _one(cap).outcome()[0] == GRADED


def test_a_plan_missing_a_deadline_field_is_incomplete(tmp_path):
    for field in ("baseline_pre_offset_deadline_us", "post_phase_lock_deadline_us",
                  "final_requested_deadline_us", "clipped_past_deadline"):
        p = _plan()
        p["data"].pop(field)
        recs = [r for r in _full() if r["stage"] != "schedule_plan"] + [p]
        cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()], name=f"{field}.jsonl")
        v, why = _one(cap).measurement(cap)
        assert v == INCOMPLETE, field
        assert any("schedule_plan missing" in w for w in why), field


def test_a_plan_with_no_operation_id_has_no_lineage(tmp_path):
    p = _plan()
    p["data"].pop("operation_id")
    recs = [r for r in _full() if r["stage"] != "schedule_plan"] + [p]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("no operation_id" in w for w in why)


# ===========================================================================================
# worker lineage: operation -> ACCEPTED revision -> claim -> dispatch
# ===========================================================================================
def test_the_control_has_a_complete_lineage(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest()] + _full() + [_foot()])
    scope, deadline, why = _one(cap).lineage()
    assert (scope, deadline, why) == ((6, 9, 9001, 4), 1_925_000, [])
    assert _one(cap).tardiness_us() == 400


def test_a_dispatch_with_no_accepted_revision_has_no_lineage(tmp_path):
    """Matching a dispatch to "the latest arm we saw" is a nearest-in-time join in disguise."""
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _claim(rev=9), _dispatch(rev=9)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    scope, deadline, why = _one(cap).lineage()
    assert scope == (6, 9, 9001, 9) and deadline is None
    assert any("NO accepted-deadline lineage" in w for w in why)
    assert _one(cap).measurement(cap)[0] == INCOMPLETE


def test_a_refused_retarget_does_not_become_the_lineage(tmp_path):
    """A refusal leaves the PREVIOUSLY accepted revision authoritative. Revision 5 was refused."""
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _retarget(result=4, rev=5), _claim(rev=4), _dispatch(rev=4)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    scope, deadline, why = _one(cap).lineage()
    assert (scope, deadline) == ((6, 9, 9001, 4), 1_925_000), "revision 5 leaked into the lineage"
    assert why == []


def test_a_refusal_must_still_carry_its_operation_id(tmp_path):
    r = _retarget(result=4, rev=5)
    r["data"].pop("operation_id")
    recs = [x for x in _full() if x["stage"] != "worker"]
    recs += [_worker(), r, _claim(), _dispatch()]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("no operation_id" in w for w in _one(cap).lineage()[2])


def test_a_successful_retarget_supplies_its_own_accepted_deadline(tmp_path):
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _retarget(result=0, rev=5, deadline=1_928_000, op=9002),
             _claim(rev=5, deadline=1_928_000, op=9002),
             _dispatch(rev=5, command_us=1_928_300, op=9002)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert _one(cap).lineage()[:2] == ((6, 9, 9002, 5), 1_928_000)
    assert _one(cap).tardiness_us() == 300


def test_two_dispatches_naming_different_revisions_are_ambiguous(tmp_path):
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _retarget(result=0, rev=5), _claim(), _dispatch(rev=4), _dispatch(rev=5)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("several execution scopes" in w for w in _one(cap).lineage()[2])


def test_the_same_revision_under_another_token_is_a_different_execution(tmp_path):
    """Revision 4 under token 6 and token 7 are not the same execution. Matching them by
    revision number alone is a nearest-join wearing a different hat."""
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _claim(token=7), _dispatch(token=7)]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    scope, deadline, why = s.lineage()
    # The claim and dispatch agree with each other; it is the ACCEPT that sits under token 6.
    assert scope == (7, 9, 9001, 4) and deadline is None
    assert any("NO accepted-deadline lineage" in w for w in why)
    assert s.tardiness_us() is None
    assert s.measurement(cap)[0] == INCOMPLETE


def test_one_revision_accepted_twice_with_different_deadlines_is_refused(tmp_path):
    recs = [r for r in _full() if r["stage"] != "worker"]
    recs += [_worker(), _retarget(result=0, rev=4, deadline=1_930_000, op=9001),
             _claim(), _dispatch()]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    assert any("accepted twice with different deadlines" in w for w in s.lineage()[2])
    assert s.tardiness_us() is None, "a contradictory lineage must withhold every derived value"


def test_a_claim_with_no_dispatch_needs_an_explicit_cancellation(tmp_path):
    recs = [r for r in _full() if r["stage"] not in ("worker", "shot_terminal")]
    recs += [_worker(), _claim(), _terminal(state="released")]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("CLAIM WITHOUT DISPATCH" in w for w in _one(cap).lineage()[2])


def test_cancel_after_claim_is_explainable(tmp_path):
    recs = [r for r in _full() if r["stage"] not in ("worker", "shot_verdict", "shot_terminal")]
    recs += [_worker(), _claim(), _terminal(state="cancelled")]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    s = _one(cap)
    assert s.lineage()[2] == []
    assert s.outcome()[0] == UNGRADED, "a cancelled shot stays in the accounting"


# ===========================================================================================
# the bucket that actually supplied the trim
# ===========================================================================================
@pytest.mark.parametrize("key", [None, "", "unknown"])
def test_an_unknown_resolved_trim_key_is_refused(tmp_path, key):
    """resolvedKey(...) can fall back to a different storage key than the requested tempo."""
    recs = [r for r in _full() if r["stage"] != "native_onset"]
    recs.append(_native_onset(resolved_trim_key=key))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()], name="k.jsonl")
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("resolved_trim_key" in w for w in why)


def test_an_unknown_effective_tempo_is_refused(tmp_path):
    recs = [r for r in _full() if r["stage"] != "native_onset"]
    recs.append(_native_onset(effective_tempo="unknown"))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert any("effective_tempo is unknown" in w for w in _one(cap).measurement(cap)[1])


def test_the_requested_tempo_is_not_the_resolved_key(tmp_path):
    """A fallback resolution is NORMAL and must not be an error -- only an UNKNOWN one is."""
    recs = [r for r in _full() if r["stage"] != "native_onset"]
    recs.append(_native_onset(computed_tempo="slow", effective_tempo="normal",
                              resolved_trim_key="Standstill/normal"))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert _one(cap).measurement(cap)[0] == COMPLETE


# ===========================================================================================
# reader and native onset are DIFFERENT events
# ===========================================================================================
def test_reader_and_native_first_frames_may_differ(tmp_path):
    """Different acceptance events. Divergence is expected, not a defect -- and the consumer
    must never derive one from the other."""
    recs = [r for r in _full() if r["stage"] != "reader_onset"]
    recs.append(_reader_onset(processed_seq=1207, frame=1207))
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    assert _one(cap).measurement(cap)[0] == COMPLETE


def test_a_reader_onset_alone_cannot_stand_in_for_native_onset(tmp_path):
    recs = [r for r in _full() if r["stage"] != "native_onset"]
    cap = _cap(tmp_path, [_hdr(), _manifest()] + recs + [_foot()])
    v, why = _one(cap).measurement(cap)
    assert v == INCOMPLETE
    assert any("bucket actually used is unknown" in w for w in why)


# ===========================================================================================
# clock domains: required for the cross-check, NOT for the estimand
# ===========================================================================================
def _bridge(sync_id=1, engine_us=1_500_000, qpc=15_000_000, lo=1_000_000, hi=2_500_000,
            uncertainty=300, **over):
    d = {"sync_id": sync_id, "engine_us": engine_us, "qpc": qpc,
         "valid_from_engine_us": lo, "valid_to_engine_us": hi, "uncertainty_us": uncertainty}
    d.update(over)
    return _ev("clock_bridge", d)


OWNER = ("probe-1", "native", 100, "1")


def _mf(**over):
    return _manifest(**over)


def test_a_missing_bridge_is_engine_only_not_a_failure(tmp_path):
    """The primary estimand is entirely engine-clock. Demanding a conversion it never needed
    would fail a perfectly good capture."""
    cap = _cap(tmp_path, [_hdr(), _mf()] + _full() + [_foot()])
    s = _one(cap)
    v, why = s.clock(cap)
    assert v == CLOCK_ENGINE_ONLY
    assert "engine-clock quantities remain available" in why[0]
    assert s.measurement(cap)[0] == COMPLETE, "a missing bridge must not block measurement"
    assert s.adherence(cap)[0] == ADHERENT


def test_a_valid_bridge_permits_the_cross_check(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _mf(), _bridge()] + _full() + [_foot()])
    assert _one(cap).clock(cap)[0] == CLOCK_OK
    qpc, unc, why = cap.convert(OWNER, 7, 1_600_000)
    assert why == "" and unc == 300
    # MICROSECONDS ARE NOT TICKS: 100_000 us at 10 MHz is 1_000_000 ticks, not 100_000.
    assert qpc == 15_000_000 + 1_000_000


def test_conversion_scales_by_the_owning_journals_qpc_hz(tmp_path):
    """The defect this pins: microseconds were added straight to a tick count."""
    cap = _cap(tmp_path, [_hdr(), _mf(),
                          _bridge(engine_us=1_000_000, qpc=20_000_000, lo=0, hi=9_000_000)]
               + _full() + [_foot()])
    qpc, _unc, why = cap.convert(OWNER, 7, 1_600_000)
    assert why == ""
    assert qpc == 26_000_000, "0.6 s at 10 MHz is 6_000_000 ticks"


def test_a_bridge_is_not_borrowed_across_processes(tmp_path):
    """Two processes may both choose engine context 7. One's sync segment is not the other's."""
    a = load(_write(tmp_path, [_hdr(pid=100, instance="1"), _mf(), _bridge()]
                    + _full() + [_foot()], "a.jsonl"))
    b = load(_write(tmp_path, [_hdr(pid=200, instance="2")] + _full() + [_foot()], "b.jsonl"))
    cap = ProbeCapture([a, b]).build()
    other = ("probe-1", "native", 200, "2")
    shot = cap.shots[(other, (7, EPOCH))]
    v, why = shot.clock(cap)
    assert v == CLOCK_ENGINE_ONLY
    assert "no clock_bridge" in why[0]
    assert cap.convert(other, 7, 1_600_000)[2].startswith("no clock_bridge")


def test_a_timestamp_outside_every_segment_is_refused(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _mf(), _bridge(lo=1_000_000, hi=1_500_000)]
               + _full() + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_REFUSED
    assert any("outside every sync segment" in w for w in why)


def test_a_gap_between_segments_is_named_as_a_discontinuity(tmp_path):
    """A timestamp between two segments is a DISCONTINUITY, a different defect from no bridge."""
    cap = _cap(tmp_path, [_hdr(), _mf(),
                          _bridge(sync_id=1, lo=1_000_000, hi=1_550_000),
                          _bridge(sync_id=2, engine_us=1_800_000, qpc=18_000_000,
                                  lo=1_650_000, hi=2_500_000)]
               + _full() + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_REFUSED
    assert any("CLOCK DISCONTINUITY" in w for w in why)


def test_overlapping_segments_are_refused(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _mf(), _bridge(sync_id=1), _bridge(sync_id=2, qpc=15_500_000)]
               + _full() + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_REFUSED
    assert any("overlapping sync segments" in w for w in why)


def test_uncertainty_above_the_declared_tolerance_is_refused(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _mf(), _bridge(uncertainty=4000)] + _full() + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_REFUSED
    assert any("exceeds the manifest tolerance" in w for w in why)


def test_no_declared_tolerance_means_no_conversion_is_trusted(tmp_path):
    cap = _cap(tmp_path, [_hdr(), _manifest(clock_tolerance_us=None), _bridge()]
               + _full() + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_REFUSED
    assert any("no clock_tolerance_us" in w for w in why)


def test_a_segment_without_an_uncertainty_is_refused(tmp_path):
    b = _bridge()
    b["data"].pop("uncertainty_us")
    cap = _cap(tmp_path, [_hdr(), _mf(), b] + _full() + [_foot()])
    assert _one(cap).clock(cap)[0] == CLOCK_REFUSED


def test_a_bridge_with_no_engine_context_is_held(tmp_path):
    b = _ev("clock_bridge", _bridge()["data"], context="0")
    cap = _cap(tmp_path, [_hdr(), _mf(), b] + _full() + [_foot()])
    assert any(st == "clock_bridge" for st, _e, _w in cap.unattached)
    assert cap.bridges == {}


def test_a_shot_with_no_reader_onset_needs_no_bridge(tmp_path):
    recs = [r for r in _full() if r["stage"] != "reader_onset"]
    cap = _cap(tmp_path, [_hdr(), _mf()] + recs + [_foot()])
    v, why = _one(cap).clock(cap)
    assert v == CLOCK_ENGINE_ONLY
    assert "nothing crosses a clock domain here" in why[0]


def test_a_refused_clock_does_not_block_adherence(tmp_path):
    """Adherence is final-deadline minus shadow -- both engine-clock. A broken bridge is
    irrelevant to it, and coupling them would discard good treatment evidence."""
    cap = _cap(tmp_path, [_hdr(), _mf(), _bridge(uncertainty=9000)] + _full() + [_foot()])
    s = _one(cap)
    assert s.clock(cap)[0] == CLOCK_REFUSED
    assert s.adherence(cap)[0] == ADHERENT
    assert s.outcome()[0] == GRADED
