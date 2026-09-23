"""Adversarial fixtures for the WORKER LEDGER in tools/timing/observer_audit.py.

The rest of that consumer answers workstream A's support question and is already covered by
tools/timing/observer_fixtures.py. This file covers only the part that was previously reported as
NOT ANALYZED: what the worker returned after the arm, and whether any engine proposal can honestly
be said to have caused a given worker retarget.

Every case here attacks one of the two ways this analysis goes wrong:

  (a) a FALSE JOIN -- attaching a worker row to a token/context/route generation it does not name,
      or attributing a worker retarget to a proposal when more than one was eligible, or reading a
      refused arm's `deadline_us` as an accepted target; and
  (b) a FALSE ABSENCE -- reporting "0 retargets" or "not armed" when the capture is lossy, or when
      the engine simply does not record that stage for proposals that returned before the final
      guard.

Schema under test: D:\\NexusVision\\diagnostics\\timing-observer-20260920-190810\\
TIMING_OBSERVER_SCHEMA.json (`identity_scopes`, `worker_results`, `semantics`, `join_rules`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.timing.observer_audit import Capture, load  # noqa: E402

QPC_HZ = 10_000_000
ARM, CLAIMED, DISPATCH, RETARGET = 1, 2, 3, 4          # worker_event
ARMED, WINDOW_REJECTED = 0, 3                          # worker_results.arm_result
RETARGETED, NOT_WAITING = 0, 4                         # worker_results.retarget_result
CONFIRMED_LOCAL = 1                                    # worker_results.dispatch_complete

# The consumer reads enums from the schema when it is present. Pinning them here means these tests
# describe the contract rather than whichever schema file happens to be on this machine.
ENUMS = {
    "worker_event": {1: "arm_result", 2: "claimed", 3: "dispatch_complete", 4: "retarget_result"},
    "worker_results.arm_result": {0: "Armed", 1: "EngineDisarmed", 2: "RouteRejected",
                                  3: "WindowRejected", 4: "MailboxBusy"},
    "worker_results.retarget_result": {0: "Retargeted", 1: "EngineDisarmed", 2: "InvalidToken",
                                       3: "WrongToken", 4: "NotWaiting", 5: "OutcomePending",
                                       6: "RouteRejected", 7: "WindowRejected", 8: "EngineRejected"},
    "worker_results.dispatch_complete": {0: "not_confirmed", 1: "confirmed_local_only"},
    "retarget_kind": {1: "RungConsensus", 2: "FadeProgressionCatchup"},
}


# -------------------------------------------------------------------------------------------
# builders
# -------------------------------------------------------------------------------------------
def _hdr(pid=100, instance="1", capture_id="cap-1"):
    return {"type": "header", "schema": 1, "trace_kind": "timing", "role": "native",
            "capture_id": capture_id, "pid": pid, "instance": instance, "capacity": 4096,
            "qpc_hz": QPC_HZ, "sync_qpc": "1000", "sync_unix_us": "1",
            "sync_uncertainty_qpc": "10"}


def _foot(dropped_full=0, pending=0):
    return {"type": "footer", "now_us": "999999", "reason": "shutdown", "pending": pending,
            "dropped_full": str(dropped_full), "dropped_contention": "0", "dropped_clock": "0"}


def _ev(stage, data, context="1", generation="9", epoch="7"):
    return {"type": "event", "stage": stage, "context": context, "generation": generation,
            "epoch": epoch, "source_seq": None, "data": dict(data), "dropped_total": "0"}


def _worker(event, token, context="1", generation="9", **data):
    # Worker records carry a NULL epoch per identity_scopes; the join is context+token+generation.
    return _ev("worker", dict(data, token=token, event=event),
               context=context, generation=generation, epoch=None)


def _engine_retarget(token, proposal_id, kind=1, result=0, context="1", generation="9"):
    return _ev("retarget", {"attempt": 5, "token": token, "proposal_id": proposal_id,
                            "kind": kind, "result": result, "old_deadline_us": "1500000",
                            "new_deadline_us": "1503000", "identity_valid": 1,
                            "deadline_valid": 1, "route": 1, "frame": 20, "ruler": 1},
               context=context, generation=generation)


def _fit(decision_id, token, n=4, context="1", generation="9"):
    return _ev("decision_fit",
               {"attempt": 5, "token": token, "decision_id": decision_id, "input_id": n,
                "frame": n, "now_us": "1100000", "selected_tip_us": "1500000",
                "selected_sigma_us": "13000", "combined_sigma_us": "13000", "selected_source": 1,
                "valid": 1, "phase_primary": 1, "sampler_authoritative": 0, "n": n,
                "span_us": "66000", "first_sample_us": "1000000", "last_sample_us": "1100000",
                "weight_sum_q9": 3_000_000_000, "weight_sq_sum_q9": 2_000_000_000, "wrss_q9": 0,
                "lambda_q9": 500_000_000, "slope_q9": 185_000_000, "used_quad": 0,
                "peak_fallback": 0, "crossing_us": "1500000", "sampler_sigma_us": "20000",
                "confidence_q6": 1_000_000, "frame_age_us": "18500", "sampler_generation": 1,
                "imminent_us": "80000", "phase_solo": 1, "curve_alpha_q9": 0, "ruler": 1},
               context=context, generation=generation)


def _members(decision_id, token, context="1", generation="9"):
    return _ev("decision_members",
               {"attempt": 5, "token": token, "decision_id": decision_id, "input_id": 3,
                "phase_anchor_us": "1000000", "anchor_micropct": 20_000_000,
                "phase_tip_us": "1500000", "phase_sigma_us": "13000",
                "phase_hypothetical_weight_q9": 180_000_000, "phase_stretch_q9": 1_000_000_000,
                "stretch_latched": 0, "phase_constant_us": "403300", "phase_solo": 1,
                "curve_enabled": 0, "reference_clock_enabled": 0, "sampler_crossing_us": "1500000",
                "horizon_debias_us": "0", "phase_primary": 1, "sampler_sole_slope_refused": 1},
               context=context, generation=generation)


def _input(input_id, frame, fill, context="1", generation="9"):
    return _ev("sampler_input",
               {"attempt": 5, "input_id": input_id, "feed": 1, "frame": frame,
                "measurement_epoch_us": str(1_000_000 + frame * 16667),
                "capture_engine_us": str(1_000_000 + frame * 16667),
                "fill_micropct": int(fill * 1_000_000), "ruler": 1, "ruler_mode": 1,
                "disposition": 5, "ruler_reset": 0, "n_before": input_id - 1, "n_after": input_id,
                "tail_before_us": "0", "tail_after_us": "33000", "sampler_generation": 1},
               context=context, generation=generation)


def _paired_decision(token=6, decision_id=1, n=4, context="1", generation="9"):
    """A COMPLETE paired decision: four accepted inputs, a fit and its members."""
    # frame i stamps capture_engine_us at 1_000_000 + i*16667, which must land inside the fit's
    # own [first_sample_us, last_sample_us] window or the support fence correctly excludes it.
    rows = [_input(i, i, 20 + i * 4, context=context, generation=generation)
            for i in range(1, n + 1)]
    rows.append(_fit(decision_id, token, n=n, context=context, generation=generation))
    rows.append(_members(decision_id, token, context=context, generation=generation))
    return rows


def _write(tmp_path, name, records, repair_counts=True):
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
        if r.get("type") == "footer" and repair_counts:
            r["accepted"] = str(nid)
            r["written"] = str(nid)
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    return p


def _cap(tmp_path, records, name="native.jsonl", repair_counts=True):
    return Capture([load(_write(tmp_path, name, records, repair_counts))], enums=ENUMS).build()


def _only(cap):
    assert len(cap.ledgers) == 1, sorted(cap.ledgers)
    return next(iter(cap.ledgers.values()))


IDENT = ("cap-1", "native", 100, "1")


# -------------------------------------------------------------------------------------------
# the join: a worker row belongs to the scope it NAMES, or to nothing
# -------------------------------------------------------------------------------------------
def test_worker_row_joins_on_the_full_route_scope(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED, deadline_us="1500000"), _foot()])
    assert list(cap.ledgers) == [(IDENT, 1, 6, 9)]
    assert cap.unjoinable_worker == []


@pytest.mark.parametrize("kw,fragment", [
    ({"token": 0}, "token is 0 (unset)"),
    ({"context": None}, "root context missing"),
    ({"context": "0"}, "root context is 0 (null)"),
    ({"generation": None}, "route generation unknown"),
])
def test_an_unnameable_scope_is_held_not_guessed(tmp_path, kw, fragment):
    """A zero token, a null root context or an unknown route generation means NO JOIN.

    The failure this prevents is the tempting one: there is exactly one live token in the capture,
    so a nearest-match consumer would happily attach the row to it and report a confident outcome.
    """
    row = _worker(ARM, **dict({"token": 6}, **kw), result=ARMED)
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED), row, _foot()])
    assert list(cap.ledgers) == [(IDENT, 1, 6, 9)], "the unnameable row was joined to a real token"
    assert len(cap.unjoinable_worker) == 1
    assert fragment in cap.unjoinable_worker[0][1]
    # and it must not have been counted into the real token's ledger either
    assert len(_only(cap).arm) == 1


def test_same_token_in_two_contexts_is_two_ledgers(tmp_path):
    """identity_scopes: never equate two contexts because token/generation match."""
    cap = _cap(tmp_path, [_hdr()]
               + _paired_decision(token=6, decision_id=1, context="1")
               + _paired_decision(token=6, decision_id=2, context="2")
               + [_worker(ARM, 6, context="1", result=ARMED),
                  _worker(ARM, 6, context="2", result=WINDOW_REJECTED), _foot()])
    assert sorted(cap.ledgers) == [(IDENT, 1, 6, 9), (IDENT, 2, 6, 9)]
    assert cap.ledgers[(IDENT, 1, 6, 9)].arm_outcome(ENUMS)[0] == ARMED
    assert cap.ledgers[(IDENT, 2, 6, 9)].arm_outcome(ENUMS)[0] == WINDOW_REJECTED


def test_same_token_in_two_route_generations_is_two_ledgers(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, generation="9", result=ARMED),
                  _worker(ARM, 6, generation="10", result=WINDOW_REJECTED), _foot()])
    assert sorted(cap.ledgers) == [(IDENT, 1, 6, 9), (IDENT, 1, 6, 10)]


def test_two_processes_sharing_a_capture_id_do_not_merge(tmp_path):
    """capture_id is a run LABEL, not a process identity."""
    a = load(_write(tmp_path, "a.jsonl",
                    [_hdr(pid=100, instance="1")] + _paired_decision()
                    + [_worker(ARM, 6, result=ARMED), _foot()]))
    b = load(_write(tmp_path, "b.jsonl",
                    [_hdr(pid=200, instance="2")] + _paired_decision()
                    + [_worker(ARM, 6, result=WINDOW_REJECTED), _foot()]))
    cap = Capture([a, b], enums=ENUMS).build()
    assert len(cap.ledgers) == 2
    assert {k[0] for k in cap.ledgers} == {("cap-1", "native", 100, "1"),
                                           ("cap-1", "native", 200, "2")}


# -------------------------------------------------------------------------------------------
# what the worker RETURNED -- refusals are not outcomes
# -------------------------------------------------------------------------------------------
def test_a_refused_arm_deadline_is_not_an_accepted_deadline(tmp_path):
    """`worker arm/retarget deadline/authority are actual REQUESTED values`.

    WindowRejected still carries the deadline the engine asked for. Reading it as the target the
    worker adopted is the single most inviting misread in this stage.
    """
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=WINDOW_REJECTED, deadline_us="1500000"), _foot()])
    led = _only(cap)
    assert led.arm_outcome(ENUMS)[1] == "3(WindowRejected)"
    assert led.accepted_deadline() == []


def test_an_accepted_arm_deadline_is_reported(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED, deadline_us="1500000"), _foot()])
    assert _only(cap).accepted_deadline() == [1500000]


def test_a_failed_retarget_deadline_is_not_accepted_either(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED, deadline_us="1500000"),
                  _worker(RETARGET, 6, result=NOT_WAITING, deadline_us="1503000"), _foot()])
    led = _only(cap)
    assert led.accepted_deadline() == [1500000]
    assert led.retarget_outcomes(ENUMS) == ["4(NotWaiting)"]
    assert "0/1" in led.revisability(ENUMS)


def test_dispatch_confirmation_is_local_only(tmp_path):
    """`No event implies console/game receipt.` The wording must carry that, not just the enum."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED, deadline_us="1500000"),
                  _worker(DISPATCH, 6, result=CONFIRMED_LOCAL, complete_us="1500400"), _foot()])
    assert _only(cap).dispatch_outcomes(ENUMS) == ["1(confirmed_local_only)"]


def test_an_unmapped_worker_event_is_preserved_not_bucketed(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(99, 6, result=0), _foot()])
    led = _only(cap)
    assert led.arm == [] and led.retargets == [] and led.claims == []
    assert led.unknown_event_codes(ENUMS) == ["99(unmapped)"]


def test_a_worker_row_with_no_event_field_is_not_an_arm(tmp_path):
    """Real emitter traces carry rows with the event field omitted. Omitted is not zero."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_ev("worker", {"token": 6}, epoch=None), _foot()])
    led = _only(cap)
    assert led.unknown_event_codes(ENUMS) == ["unknown"]
    assert led.arm_outcome(ENUMS)[0] is None


# -------------------------------------------------------------------------------------------
# attribution -- a worker retarget names no proposal
# -------------------------------------------------------------------------------------------
def test_one_observed_guard_is_still_UNATTRIBUTED(tmp_path):
    """CORRECTED after Codex reproduced a false attribution against consumer f978424f.

    This test previously asserted that one engine guard row beside one worker return WAS an
    attribution. It is not. A commit-before-worker emits a guard with no worker return; an early
    worker refusal emits a return with no guard. So "exactly one of each" is not a binding -- it
    is two observations that happen to be alone, and treating that as proof was the defect.

    Refusing to bind is NOT refusing to report: the observed proposal id must still appear.
    """
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_engine_retarget(6, proposal_id=11),
                  _worker(ARM, 6, result=ARMED, deadline_us="1500000"),
                  _worker(RETARGET, 6, result=RETARGETED, deadline_us="1503000"), _foot()])
    att = _only(cap).attribution
    assert att.startswith("UNATTRIBUTED"), att
    assert "proposal_id 11" in att, "the observed guard id must still be reported"
    assert "not a proven binding" in att


def test_two_eligible_proposals_are_unattributed(tmp_path):
    """The worker row carries no proposal_id and there is no nearest-time tiebreak."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_engine_retarget(6, proposal_id=11), _engine_retarget(6, proposal_id=12),
                  _worker(ARM, 6, result=ARMED),
                  _worker(RETARGET, 6, result=RETARGETED), _foot()])
    att = _only(cap).attribution
    assert att.startswith("UNATTRIBUTED")
    assert "11" in att and "12" in att


def test_a_worker_retarget_with_no_engine_row_is_unattributed_not_absent(tmp_path):
    """`retarget stage records the final engine identity/deadline guard ONLY for proposals reaching
    that branch. Earlier engine returns are not asserted absent.`"""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED),
                  _worker(RETARGET, 6, result=RETARGETED), _foot()])
    att = _only(cap).attribution
    assert att.startswith("UNATTRIBUTED")
    assert "NOT asserted absent" in att


def test_a_proposal_in_another_route_generation_is_not_eligible(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_engine_retarget(6, proposal_id=11, generation="10"),
                  _worker(ARM, 6, result=ARMED),
                  _worker(RETARGET, 6, result=RETARGETED), _foot()])
    assert "no engine `retarget` row in this route scope" in _only(cap).attribution


def test_an_engine_proposal_that_cannot_name_its_scope_is_held(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_ev("retarget", {"attempt": 5, "proposal_id": 11}),
                  _worker(ARM, 6, result=ARMED), _foot()])
    assert len(cap.unjoinable_proposals) == 1
    assert "token missing" in cap.unjoinable_proposals[0][1]
    assert cap.proposals == {}


def test_no_worker_retarget_does_not_claim_none_was_attempted(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED), _foot()])
    led = _only(cap)
    assert "does not infer that none was attempted" in led.revisability(ENUMS)
    assert "no worker retarget_result row" in led.attribution


def test_revisability_refuses_to_generalise(tmp_path):
    """A successful retarget is evidence about THAT proposal kind at THAT moment only."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_engine_retarget(6, proposal_id=11, kind=2),
                  _worker(ARM, 6, result=ARMED),
                  _worker(CLAIMED, 6, remaining_us="3200", target_revision=2),
                  _worker(RETARGET, 6, result=RETARGETED), _foot()])
    text = _only(cap).revisability(ENUMS)
    assert "1/1 retarget(s) returned success" in text
    assert "remaining at claim 3200 us" in text
    assert "NOT generalisable" in text


# -------------------------------------------------------------------------------------------
# paired decision -> ledger
# -------------------------------------------------------------------------------------------
def _decision(cap, decision_id=1):
    return next(d for d in cap.decisions.values() if d.key[3] == decision_id)


def test_a_paired_decision_resolves_its_own_revisability(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_engine_retarget(6, proposal_id=11),
                  _worker(ARM, 6, result=ARMED),
                  _worker(RETARGET, 6, result=RETARGETED), _foot()])
    assert cap.blocking == []
    verdict, detail = cap.revisability_for(_decision(cap))
    assert "1/1 retarget(s) returned success" in verdict
    assert "proposal_id 11" in detail


def test_a_decision_with_no_worker_row_is_not_reported_as_zero(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision() + [_foot()])
    verdict, detail = cap.revisability_for(_decision(cap))
    assert verdict == "NOT ANALYZED"
    assert "does not infer that none occurred" in detail


def test_a_decision_whose_worker_rows_are_in_another_scope_does_not_borrow_them(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision(token=6)
               + [_worker(ARM, 7, result=ARMED),
                  _worker(RETARGET, 7, result=RETARGETED), _foot()])
    verdict, _ = cap.revisability_for(_decision(cap))
    assert verdict == "NOT ANALYZED"


def test_a_decision_with_two_fits_has_no_token_to_join_on(tmp_path):
    """A reused decision_id with two fit records is AMBIGUOUS; it names no single token."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_fit(1, 6, n=6), _worker(ARM, 6, result=ARMED), _foot()])
    verdict, detail = cap.revisability_for(_decision(cap))
    assert verdict == "NOT ANALYZED"
    assert "no single decision_fit" in detail


# -------------------------------------------------------------------------------------------
# a lossy capture is a FLOOR, never a count
# -------------------------------------------------------------------------------------------
def test_a_lossy_capture_makes_revisability_unknown(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED), _foot(dropped_full=3)])
    assert cap.blocking
    verdict, detail = cap.revisability_for(_decision(cap))
    assert verdict == "UNKNOWN"
    assert "lossy or incomplete" in detail


def test_a_lossy_capture_with_no_worker_row_is_unknown_not_not_analyzed(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision() + [_foot(dropped_full=3)])
    verdict, detail = cap.revisability_for(_decision(cap))
    assert verdict == "UNKNOWN"
    assert "absence proves nothing" in detail


def test_an_incomplete_capture_still_reports_what_it_observed(tmp_path):
    """Refusing a verdict is not refusing the evidence: the observed rows still print."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision()
               + [_worker(ARM, 6, result=ARMED),
                  _worker(RETARGET, 6, result=RETARGETED)], repair_counts=False)
    assert "NO_FOOTER" in cap.blocking[0] or any("NO_FOOTER" in b for b in cap.blocking)
    verdict, detail = cap.revisability_for(_decision(cap))
    assert verdict == "UNKNOWN"
    assert "1/1 retarget(s) returned success" in detail


def test_absence_wording_never_claims_the_event_did_not_happen(tmp_path):
    """Belt and braces on the strings themselves -- this is where the previous consumer lied."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision() + [_worker(CLAIMED, 6, remaining_us="1000"),
                                                          _foot()])
    led = _only(cap)
    assert "NOT a claim" in led.arm_outcome(ENUMS)[1]
    assert "does not infer" in led.revisability(ENUMS)


# -------------------------------------------------------------------------------------------
# the ledger must not disturb the support analysis it sits beside
# -------------------------------------------------------------------------------------------
def test_worker_rows_do_not_leak_into_sampler_support(tmp_path):
    cap = _cap(tmp_path, [_hdr()] + _paired_decision(n=4)
               + [_worker(ARM, 6, result=ARMED), _worker(RETARGET, 6, result=RETARGETED),
                  _foot()])
    sup = cap.support_for(_decision(cap))
    assert sup["authoritative_n"] == 4
    assert sup["accepted_live"] == 4
    assert sup["reconstructed"] is True


def test_the_ledger_is_bounded_by_what_the_capture_contains(tmp_path):
    """No ledger is created for a token that never appears in a worker row."""
    cap = _cap(tmp_path, [_hdr()] + _paired_decision(token=6)
               + [_engine_retarget(6, proposal_id=11), _foot()])
    assert cap.ledgers == {}
    assert list(cap.proposals) == [(IDENT, 1, 6, 9)]
