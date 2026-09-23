"""Codex's twelve reproduced FALSE PASSES, pinned as regression tests.

Every mutation below produced the literal result COMPLETE / ADHERENT / GRADED against probe_audit
revision `c670f78b`. Each is a way for contradictory or absent execution evidence to be reported as
a clean, usable shot -- which is the single worst thing this consumer can do, because a biased
estimate built from a correct-looking pipeline is indistinguishable from a real result.

The bar is deliberately weak and therefore hard to game: a mutation passes only if it fails AT
LEAST ONE of measurement / adherence / outcome. It does not have to fail the "right" one. That
keeps the test honest about what it proves -- that the defect is detected somewhere -- rather than
encoding today's message text as the contract.

The CONTROL is part of the suite. A consumer that refuses everything would pass all twelve and be
useless, so the clean shot must still come back COMPLETE / ADHERENT / GRADED.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_probe_audit as T                                          # noqa: E402
from tools.timing.observer_audit import load                          # noqa: E402
from tools.timing.probe_audit import (                                # noqa: E402
    ADHERENT, COMPLETE, GATE_PASS, GATE_WITHHELD, GRADED, ProbeCapture)


def _run(tmp_path, name, recs):
    p = T._write(tmp_path, [T._hdr(), T._manifest()] + recs + [T._foot()], name + ".jsonl")
    cap = ProbeCapture([load(p)]).build()
    if not cap.shots:
        return cap, None
    return cap, next(iter(cap.shots.values()))


def _drop(stages):
    return [r for r in T._full() if r["stage"] not in stages]


def _no_execution_evidence():
    return _drop({"worker", "reader_onset", "release_link"})


def _assignment_after_command():
    recs = _drop({"probe_assignment"})
    recs.append(T._assignment(assigned_at=3_000_000,
                              **{"eligibility_fixed_engine_us": 2_900_000}))
    return recs


def _no_press_or_onset_times():
    recs = _drop({"shot_press", "native_onset"})
    p = T._press()
    p["data"].pop("press_engine_us")
    n = T._native_onset()
    n["data"].pop("native_onset_engine_us")
    n["data"].pop("latched_engine_us")
    return recs + [p, n]


def _invalid_negative_onset():
    return _drop({"native_onset"}) + [T._native_onset(valid=0, native_onset_us=-5000)]


def _referential_integrity():
    recs = _drop({"probe_assignment", "schedule_plan", "release_link"})
    return recs + [T._assignment(experiment_id="other-experiment"),
                   T._plan(assignment_id=999), T._release(assignment_id=999)]


def _config_revision_drift():
    return _drop({"schedule_plan"}) + [T._plan(config_revision=999)]


def _dose_exceeds_bound():
    return _drop({"probe_assignment"}) + [T._assignment(delta=50000)]


def _dispatch_without_claim():
    return _drop({"worker"}) + [T._worker(), T._dispatch()]


def _full_scope_changed_revision_kept():
    """Revision 4 under another token, route generation, operation and plan is a DIFFERENT
    execution. Matching on the revision number alone is a nearest-join wearing a different hat."""
    c, d = T._claim(token=77, op=8888), T._dispatch(token=77, op=8888)
    c["generation"] = d["generation"] = "99"
    return _drop({"worker"}) + [T._worker(), c, d]


def _claim_disagrees_with_dispatch():
    return _drop({"worker"}) + [T._worker(), T._claim(rev=9, deadline=1_999_999), T._dispatch()]


def _dispatch_unconfirmed_no_times():
    dd = T._dispatch(result=0)
    dd["data"].pop("command_us")
    dd["data"].pop("complete_us")
    return _drop({"worker"}) + [T._worker(), T._claim(), dd]


def _release_contradicts_dispatch():
    return _drop({"release_link"}) + [T._release(token=77, operation_id=8888, target_revision=9)]


MUTATIONS = [
    ("no_execution_evidence", _no_execution_evidence),
    ("assignment_after_command", _assignment_after_command),
    ("no_press_or_onset_times", _no_press_or_onset_times),
    ("invalid_negative_onset", _invalid_negative_onset),
    ("referential_integrity", _referential_integrity),
    ("config_revision_drift", _config_revision_drift),
    ("dose_exceeds_bound", _dose_exceeds_bound),
    ("dispatch_without_claim", _dispatch_without_claim),
    ("full_scope_changed_revision_kept", _full_scope_changed_revision_kept),
    ("claim_disagrees_with_dispatch", _claim_disagrees_with_dispatch),
    ("dispatch_unconfirmed_no_times", _dispatch_unconfirmed_no_times),
    ("release_contradicts_dispatch", _release_contradicts_dispatch),
]


@pytest.mark.parametrize("name,build", MUTATIONS, ids=[n for n, _ in MUTATIONS])
def test_a_reproduced_false_pass_is_refused(tmp_path, name, build):
    cap, shot = _run(tmp_path, name, build())
    if shot is None:
        return                                    # no shot at all is a refusal by construction
    m = shot.measurement(cap)[0]
    a = shot.adherence(cap)[0]
    o = shot.outcome()[0]
    assert not (m == COMPLETE and a == ADHERENT and o == GRADED), (
        f"{name} is still a false pass: {m}/{a}/{o}")


@pytest.mark.parametrize("name,build", MUTATIONS, ids=[n for n, _ in MUTATIONS])
def test_a_reproduced_false_pass_also_withholds_the_gate(tmp_path, name, build):
    cap, _shot = _run(tmp_path, name, build())
    assert cap.gate()[0] == GATE_WITHHELD, f"{name} left the capture gate open"


def test_the_control_still_passes(tmp_path):
    """A consumer that refused everything would pass all twelve tests above and be useless."""
    cap, shot = _run(tmp_path, "control", T._full())
    assert shot is not None
    assert shot.measurement(cap) == (COMPLETE, [])
    assert shot.adherence(cap)[0] == ADHERENT
    assert shot.outcome()[0] == GRADED
    assert cap.gate() == (GATE_PASS, [])


def test_the_control_gate_passes_only_with_everything_in_place(tmp_path):
    """The gate is capture-level and separate from any shot verdict."""
    cap, _ = _run(tmp_path, "nomanifest", T._full())
    assert cap.gate()[0] == GATE_PASS
    p = T._write(tmp_path, [T._hdr()] + T._full() + [T._foot()], "bare.jsonl")
    bare = ProbeCapture([load(p)]).build()
    gate, why = bare.gate()
    assert gate == GATE_WITHHELD
    assert "NO_EXPERIMENT_MANIFEST" in why
