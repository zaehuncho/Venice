"""Blocker 1: one healthy shot must not conceal a corrupt one.

Codex reproduced this exactly. Each of the twelve original mutations, ALONE, withheld the gate --
12/12. The same twelve, accompanied by one healthy separately-identified shot, PASSED -- 12/12.
The bad rows stayed visible in the report, but the capture was approved.

The cause was a one-line rule: the gate asked whether ANY shot reached COMPLETE. It never asked
whether the capture could be RECONCILED.

The fix is an integrity axis separate from completeness, and the hard part is what must NOT
withhold. A non-adherent shot, a cancelled shot and an ungraded shot are legitimate experimental
OUTCOMES. If the gate demanded that every shot be adherent and graded it would become a selection
filter -- it would approve only captures whose outcomes happened to look good, which biases the
very estimate the gate exists to protect. So this file tests BOTH directions:

  * corruption anywhere withholds, in either ordering;
  * legitimate outcomes beside a healthy shot do NOT withhold.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_probe_audit as T                                          # noqa: E402
import test_probe_audit_counterexamples as CX                         # noqa: E402
from tools.timing.observer_audit import load                          # noqa: E402
from tools.timing.probe_audit import (                                # noqa: E402
    ADHERENT, COMPLETE, CORRUPT, GATE_PASS, GATE_WITHHELD, GRADED, INTACT, ProbeCapture)

HEALTHY_EPOCH = 7001


def _healthy():
    """A second, separately identified, entirely clean shot."""
    return T._full(epoch=HEALTHY_EPOCH)


def _cap(tmp_path, recs, name):
    p = T._write(tmp_path, [T._hdr(), T._manifest()] + recs + [T._foot()], name + ".jsonl")
    return ProbeCapture([load(p)]).build()


@pytest.mark.parametrize("name,build", CX.MUTATIONS, ids=[n for n, _ in CX.MUTATIONS])
def test_a_healthy_shot_does_not_rescue_a_corrupt_one(tmp_path, name, build):
    cap = _cap(tmp_path, build() + _healthy(), f"after_{name}")
    gate, why = cap.gate()
    assert gate == GATE_WITHHELD, (
        f"{name} was concealed by a healthy shot: {why}")
    assert any("UNRESOLVED EVIDENCE CORRUPTION" in w for w in why), why


@pytest.mark.parametrize("name,build", CX.MUTATIONS, ids=[n for n, _ in CX.MUTATIONS])
def test_order_does_not_matter(tmp_path, name, build):
    """The healthy shot FIRST must be no more forgiving than the healthy shot last."""
    cap = _cap(tmp_path, _healthy() + build(), f"before_{name}")
    assert cap.gate()[0] == GATE_WITHHELD


@pytest.mark.parametrize("name,build", CX.MUTATIONS, ids=[n for n, _ in CX.MUTATIONS])
def test_the_healthy_shot_is_still_reported_as_healthy(tmp_path, name, build):
    """Withholding readiness must not smear corruption onto the clean rows. Every row is
    preserved and judged on its own evidence."""
    cap = _cap(tmp_path, build() + _healthy(), f"rows_{name}")
    healthy = [s for (_o, k), s in cap.shots.items() if k[1] == HEALTHY_EPOCH]
    assert len(healthy) == 1
    s = healthy[0]
    assert s.integrity(cap)[0] == INTACT
    assert s.measurement(cap)[0] == COMPLETE
    assert s.adherence(cap)[0] == ADHERENT
    assert s.outcome()[0] == GRADED


# =========================================================================================
# the other direction: legitimate OUTCOMES must not withhold
# =========================================================================================
def _nonadherent(epoch=9001):
    """Fully measured and internally consistent; only the displacement is wrong."""
    bad = 1_900_000 + 5000
    recs = [T._press(epoch), T._native_onset(epoch), T._reader_onset(epoch),
            T._assignment(epoch, 25000), T._plan(epoch, final=bad),
            T._worker(epoch, deadline=bad), T._claim(epoch, deadline=bad),
            T._dispatch(epoch, command_us=bad + 400),
            T._release(epoch, **{"local_command_us": bad + 400}),
            T._verdict(epoch), T._terminal(epoch)]
    return recs


def _cancelled_before_arming(epoch=9002):
    return [T._press(epoch), T._native_onset(epoch), T._reader_onset(epoch),
            T._assignment(epoch, 0), T._plan(epoch, 0),
            T._terminal(epoch, state="cancelled")]


def _delivered_but_ungraded(epoch=9003):
    """Codex's own correction: a delivered shot with no grade can be completely MEASURED."""
    return [r for r in T._full(epoch) if r["stage"] != "shot_verdict"]


LEGITIMATE = [("nonadherent", _nonadherent),
              ("cancelled_before_arming", _cancelled_before_arming),
              ("delivered_but_ungraded", _delivered_but_ungraded)]


@pytest.mark.parametrize("name,build", LEGITIMATE, ids=[n for n, _ in LEGITIMATE])
def test_a_legitimate_outcome_does_not_withhold_the_gate(tmp_path, name, build):
    """If these withheld, the gate would approve only captures whose outcomes looked good --
    a selection filter, and a biased one."""
    cap = _cap(tmp_path, build() + _healthy(), f"legit_{name}")
    gate, why = cap.gate()
    assert gate == GATE_PASS, f"{name} wrongly withheld the gate: {why}"


@pytest.mark.parametrize("name,build", LEGITIMATE, ids=[n for n, _ in LEGITIMATE])
def test_a_legitimate_outcome_is_INTACT(tmp_path, name, build):
    cap = _cap(tmp_path, build() + _healthy(), f"intact_{name}")
    for (_o, k), s in cap.shots.items():
        assert s.integrity(cap)[0] == INTACT, (name, k, s.integrity(cap)[1])


def test_a_nonadherent_shot_keeps_its_arm_and_dose_in_the_report(tmp_path):
    """Intention-to-treat needs the label, so a non-adherent row must never lose it."""
    cap = _cap(tmp_path, _nonadherent() + _healthy(), "itt")
    row = next(r for r in cap.report() if r["physical_epoch"] == 9001)
    assert row["adherence"] != ADHERENT
    assert row["arm_id"] == "plus25" and row["assigned_delta_us"] == 25000
    assert row["block_id"] == 10 and row["stratum_id"] == 2


def test_a_capture_of_only_legitimate_outcomes_still_passes(tmp_path):
    cap = _cap(tmp_path, _nonadherent() + _cancelled_before_arming()
               + _delivered_but_ungraded() + _healthy(), "all_legit")
    gate, why = cap.gate()
    assert gate == GATE_PASS, why


def test_one_corrupt_shot_among_many_legitimate_ones_still_withholds(tmp_path):
    cap = _cap(tmp_path, _nonadherent() + _cancelled_before_arming()
               + _delivered_but_ungraded() + _healthy()
               + CX._referential_integrity(), "mixed")
    gate, why = cap.gate()
    assert gate == GATE_WITHHELD
    assert any("UNRESOLVED EVIDENCE CORRUPTION" in w for w in why)
    # and the legitimate rows are still there, still judged on their own evidence
    # cap.shots is keyed (owner, (context, physical_epoch)), so the epoch is k[1][1].
    epochs = {k[1][1] for k in cap.shots}
    assert {9001, 9002, 9003, HEALTHY_EPOCH} <= epochs
