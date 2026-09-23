#!/usr/bin/env python
"""probe_audit.py -- consumer for the probe schema 2 measurement gate.

    python tools/timing/probe_audit.py --dir <capture dir> [--all] [--csv out.csv]
    exit status 0 = GATE PASSED, 1 = gate withheld, 2 = usage error.

Contract: tools/timing/PROBE_SCHEMA_2.md. Fixtures: tests/test_probe_audit.py.

FOUR verdicts per shot plus ONE capture-level gate result, and they are never collapsed:

    1. MEASUREMENT  -- is the required evidence present, internally consistent, and correctly
                       ordered, for THIS shot's lifecycle?
    2. ADHERENCE    -- did the EXECUTED plan carry the assigned SCHEDULING displacement?
    3. OUTCOME      -- is there an attributed verdict?
    4. CLOCK        -- can reader-side and native-side timestamps be compared? (Separate, because
                       the primary estimand is entirely engine-clock and needs no conversion.)
    +  GATE         -- is the CAPTURE ready to support an experiment at all?

Collapsing them is how a non-adherent assignment quietly leaves the primary comparison, which
would bias the estimate this consumer exists to protect.

WHAT IT REFUSES. Every entry below is a false pass Codex reproduced against an earlier version of
this file, or a defect this project has already shipped once:
  * contradictory or ABSENT execution evidence reading as COMPLETE. A shot reported `released`
    must carry press, onset, plan, an accepted-revision worker chain, a release link and a verdict.
  * a worker chain joined by REVISION NUMBER alone. Revision 4 under a different token, route
    generation or operation is a different execution; matching them is a nearest-join in disguise.
  * an acceptance record that appears AFTER the dispatch it would have authorised.
  * a derived tardiness computed from a lineage the consumer has already called contradictory.
  * SCHEDULING displacement described as delivered-command displacement. They are different
    quantities; `final_requested - zero_offset_shadow` measures only the first, and neither
    supplies a counterfactual command time for an untreated shot.
  * a missing shadow on the EXECUTED plan read as NONADHERENT because some other plan had one.
    Missing evidence is UNVERIFIABLE; only an observed contradiction is NONADHERENT.
  * a learning-freeze mask checked by SUBSTRING (`not_banner_trim` contains `banner_trim`) or
    skipped entirely because it was not a string.
  * a manifest bound that is non-numeric, negative, or above the hook's hard limit.
  * an assignment whose experiment_id does not match the manifest, or whose dose exceeds the bound.
  * MICROSECONDS ADDED TO QPC TICKS. Conversion scales by the owning journal's own qpc_hz.
  * a clock bridge borrowed across processes because two of them chose the same context number.
  * two journals claiming the same process identity, merged.
  * a Python-emitted record filed under the EMITTING journal's identity. A reader onset or verdict
    describes a NATIVE-owned shot and must carry an explicit native-owner reference.
  * a missing grade read as EXCELLENT, or a cancelled shot dropped from the accounting.
  * `release_seq` by that name: `physical_epoch` is the SHOT, `native_release_seq` is a release
    counter, and the two have historically shared that name.
  * exit status 0 on a capture with no manifest, no shots, or only unattached records.

STATUS: THE PRODUCER DOES NOT EXIST. The active AutomationEngine.cpp still matches the staged
observer transaction's `before` hash. This consumer and its fixtures ARE the contract.

HARD BOUNDARY: analysis tooling only; never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.timing.observer_audit import _i, load  # noqa: E402

SCHEMA = 2

COMPLETE, INCOMPLETE = "COMPLETE", "INCOMPLETE"
# INTEGRITY is a SEPARATE axis from completeness, and the distinction is the whole of Blocker 1.
#   INTACT  -- the evidence this shot carries is self-consistent. A shot may be INTACT and still be
#              non-adherent, cancelled or ungraded: those are legitimate experimental OUTCOMES.
#   CORRUPT -- the evidence contradicts itself or violates a declared bound. That withholds
#              readiness for the WHOLE capture.
# The defect this replaces: the gate asked whether ANY shot reached COMPLETE, so one healthy shot
# re-admitted twelve corrupt ones.
INTACT, CORRUPT = "INTACT", "CORRUPT"
ADHERENT, NONADHERENT, UNVERIFIABLE = "ADHERENT", "NONADHERENT", "UNVERIFIABLE"
GRADED, UNGRADED, DISPUTED, UNKNOWN = "GRADED", "UNGRADED", "DISPUTED", "UNKNOWN"
CLOCK_OK, CLOCK_ENGINE_ONLY, CLOCK_REFUSED = "OK", "ENGINE_ONLY", "REFUSED"
GATE_PASS, GATE_WITHHELD = "PASSED", "WITHHELD"

VERDICTS = {"EARLY", "EXCELLENT", "LATE", "UNKNOWN"}
TERMINALS = {"released", "cancelled", "killed", "unanswered", "ungraded"}
# Which lifecycles owe a delivered command. A cancellation BEFORE arming owes no dispatch; a shot
# reported released owes the whole chain. An already-issued command is not erased by a later
# cancellation label, which is why `killed` still owes its execution evidence.
NEEDS_EXECUTION = {"released", "killed"}

W_ARM, W_CLAIMED, W_DISPATCH, W_RETARGET = 1, 2, 3, 4
ACCEPTED = 0            # arm_result Armed / retarget_result Retargeted. Anything else is a refusal.

HOOK_BOUND_US = 25000   # devFireOffsetForShotMs hard bound; a manifest may not exceed it.
# The declared fidelity threshold for SCHEDULING DISPLACEMENT only. It is NOT a clock tolerance and
# NOT a claim that command tardiness must stay under 2 ms -- a large, accurately measured tardiness
# is a finding, not a measurement failure.
DISPLACEMENT_FIDELITY_US = 2000

COLLIDING_NAMES = {
    "physical_epoch": "the physical SHOT identity (formerly BANNER TRIM release_seq)",
    "native_release_seq": "a release counter (native Release delivery identity release_seq)",
}

SHOT_STAGES = {"shot_press": "press", "native_onset": "native_onset",
               "reader_onset": "reader_onset", "probe_assignment": "assignment",
               "schedule_plan": "plans", "worker": "worker", "release_link": "release",
               "shot_verdict": "verdict", "shot_terminal": "terminal"}
# Stages a Python-role journal may emit. They describe a NATIVE-owned shot, so they must carry an
# explicit native-owner reference; the emitting journal's identity is NOT the shot's owner.
PYTHON_STAGES = {"reader_onset", "shot_verdict"}


def _d(rec):
    return rec.get("data", rec)


def _rational(v):
    """Exact "n/d" with 0 < n <= d, parsed as integers. Never a float comparison.

    `"0/0"` and a bare integer both have no defined scale and are refused.
    """
    if not isinstance(v, str) or "/" not in v:
        return None
    n, _, d = v.partition("/")
    if not (n.strip().isdigit() and d.strip().isdigit()):
        return None
    n, d = int(n), int(d)
    return (n, d) if 0 < n <= d else None


def _arm_doses(man):
    """The manifest's declared arm doses, in microseconds."""
    raw = (man or {}).get("arms_us")
    if not isinstance(raw, str):
        return set()
    out = set()
    for tok in raw.split(","):
        v = _i(tok.strip())
        if v is not None:
            out.add(v)
    return out


def _arm_label_dose(arm_id):
    """The dose an arm LABEL claims, when the label encodes one (plus25 / minus25 / zero).

    A label that contradicts the dose is a corruption: `arm_id="minus25"` with `+25000` means the
    randomisation record and the treatment record disagree about which arm this shot is in, and
    intention-to-treat accounting would then be built on the wrong label.
    """
    if not isinstance(arm_id, str):
        return None
    a = arm_id.strip().lower()
    if a in ("zero", "control", "plus0", "minus0"):
        return 0
    for pre, sign in (("plus", 1), ("minus", -1), ("p", 1), ("m", -1)):
        if a.startswith(pre) and a[len(pre):].isdigit():
            return sign * int(a[len(pre):]) * 1000
    return None


def _owner(rec, journal):
    """The NATIVE process that owns the shot this record describes.

    For a native journal that is its own identity. For a Python journal it must be an explicit
    reference carried IN the record -- using the emitting journal's identity would file a Python
    verdict as a separate Python-owned shot, which is a cross-process join failing silently.
    """
    if journal.role != "python":
        return journal.identity, ""
    d = _d(rec)
    ref = (d.get("native_capture_id"), "native", _i(d.get("native_pid")),
           d.get("native_instance"))
    if ref[0] in (None, "") or ref[2] is None or ref[3] in (None, ""):
        return None, ("python record carries no complete native-owner reference "
                      "(native_capture_id / native_pid / native_instance)")
    return ref, ""


def _shot_key(rec):
    d = _d(rec)
    ctx = _i(rec.get("context"))
    ep = _i(d.get("physical_epoch"))
    why = []
    if not ctx:
        why.append("engine context " + ("is 0 (null)" if ctx == 0 else "missing"))
    if not ep:
        why.append("physical_epoch " + ("is 0 (unset)" if ep == 0 else "missing"))
    if why:
        return None, "; ".join(why)
    return (ctx, ep), ""


def _wscope(rec):
    """Full worker execution scope. A revision number alone is NOT an identity."""
    d = _d(rec)
    return (_i(d.get("token")), _i(rec.get("generation")), _i(d.get("operation_id")))


class Shot:
    def __init__(self, key, owner):
        self.key = key                 # (engine context, physical_epoch)
        self.owner = owner             # native process identity
        for attr in set(SHOT_STAGES.values()):
            setattr(self, attr, [])

    def add(self, stage, rec):
        getattr(self, SHOT_STAGES[stage]).append(rec)

    # ---------------------------------------------------------------- lifecycle
    def terminal_state(self):
        states = {_d(t).get("terminal_state") for t in self.terminal}
        return states.pop() if len(states) == 1 else None

    # ---------------------------------------------------------------- worker lineage
    def lineage(self):
        """operation -> ACCEPTED revision -> claim -> dispatch, on FULL worker scope.

        Returns (scope, accepted_deadline_us, why). A non-empty `why` means no execution claim may
        rest on this shot and every derived quantity is withheld -- including tardiness, which an
        earlier version happily computed from a deadline it had just called contradictory.
        """
        by_ev = defaultdict(list)
        for w in self.worker:
            by_ev[_i(_d(w).get("event"))].append(w)
        why = []
        accepted = {}        # full scope -> (deadline, index of the accepting record)
        for w in by_ev[W_ARM] + by_ev[W_RETARGET]:
            d = _d(w)
            scope = _wscope(w)
            if scope[2] is None:
                why.append("worker result carries no operation_id (required on refusals too)")
            if None in (scope[0], scope[1]):
                why.append("worker result carries no token / route generation")
            if _i(d.get("result")) != ACCEPTED:
                continue                       # a refusal leaves the previous revision standing
            rev = _i(d.get("target_revision"))
            dl = _i(d.get("accepted_deadline_us"))
            if rev is None or dl is None:
                why.append("accepted worker result without target_revision/accepted_deadline_us")
                continue
            full = scope + (rev,)
            if full in accepted and accepted[full][0] != dl:
                why.append(f"scope {full} accepted twice with different deadlines")
            accepted[full] = (dl, self.worker.index(w))
        claims, dispatches = by_ev[W_CLAIMED], by_ev[W_DISPATCH]
        state = self.terminal_state()
        if not dispatches:
            if claims and state not in ("cancelled", "killed", "unanswered"):
                why.append("CLAIM WITHOUT DISPATCH and no cancellation/abandonment terminal")
            if state in NEEDS_EXECUTION:
                why.append(f"terminal={state} but no worker dispatch recorded")
            return None, None, why
        if not claims:
            why.append("DISPATCH WITHOUT CLAIM: the execution lifecycle is incomplete")
        scopes = {_wscope(x) + (_i(_d(x).get("target_revision")),) for x in dispatches}
        if len(scopes) > 1:
            why.append(f"dispatches name several execution scopes {sorted(map(str, scopes))}")
            return None, None, why
        full = scopes.pop()
        if None in full:
            why.append(f"dispatch does not name a full execution scope {full}")
            return None, None, why
        for x in dispatches:
            d = _d(x)
            if _i(d.get("command_us")) is None or _i(d.get("complete_us")) is None:
                why.append("dispatch carries no command_us / complete_us")
            if _i(d.get("result")) == 0:
                why.append("dispatch reports not_confirmed: delivery is not evidenced")
        for c in claims:
            cfull = _wscope(c) + (_i(_d(c).get("target_revision")),)
            if cfull != full:
                why.append(f"claim scope {cfull} does not match the dispatched scope {full}")
        if full not in accepted:
            why.append(f"dispatched scope {full} has NO accepted-deadline lineage "
                       f"(accepted: {sorted(map(str, accepted)) or 'none'})")
            return full, None, why
        dl, acc_idx = accepted[full]
        if acc_idx > min(self.worker.index(x) for x in dispatches):
            why.append("the accepting record appears AFTER the dispatch it would authorise")
        for c in claims:
            cd = _i(_d(c).get("effective_deadline_us"))
            if cd is not None and cd != dl:
                why.append(f"claim effective deadline {cd} != accepted deadline {dl}")
        return full, (None if why else dl), why

    def tardiness_us(self):
        """Local command minus the DISPATCHED revision's deadline. Withheld on any lineage
        contradiction, and never console or game receipt."""
        full, dl, why = self.lineage()
        if dl is None or why:
            return None
        for w in self.worker:
            if _i(_d(w).get("event")) != W_DISPATCH:
                continue
            if _wscope(w) + (_i(_d(w).get("target_revision")),) == full:
                cmd = _i(_d(w).get("command_us"))
                if cmd is not None:
                    return cmd - dl
        return None

    def duplicate_plan_ids(self):
        """operation_ids carried by more than one plan with a CONTRADICTORY payload.

        A reused id resolved by first-wins is how a superseded plan quietly becomes the executed
        one. Identical repeats are fine -- the producer may legitimately re-emit -- but a
        contradictory pair is AMBIGUOUS and must not be silently reduced.
        """
        by_op = defaultdict(set)
        for p in self.plans:
            by_op[_i(_d(p).get("operation_id"))].add(json.dumps(_d(p), sort_keys=True))
        return sorted(str(op) for op, sigs in by_op.items() if len(sigs) > 1)

    def executed_plan(self):
        """The plan the dispatched operation actually came from, or None.

        Returns None when that operation_id is carried by contradictory plans: there is no single
        executed plan then, and picking the first would be first-wins on a reused id.
        """
        full, _dl, _why = self.lineage()
        if full is None:
            return None
        if str(full[2]) in self.duplicate_plan_ids():
            return None
        matches = [p for p in self.plans if _i(_d(p).get("operation_id")) == full[2]]
        return matches[0] if len(matches) == 1 else None

    # ---------------------------------------------------------------- measurement
    def classify(self, capture):
        """Every measurement finding, split into CORRUPT and benign-ABSENT.

        The split is the whole of Blocker 1. A finding is CORRUPT when two records disagree, an
        ordering is impossible, a reference does not resolve, or a declared bound is violated --
        the capture cannot be reconciled and readiness must be withheld for ALL of it. A finding
        is benign when evidence is merely ABSENT for a lifecycle that does not owe it: a cancelled
        shot with no dispatch, a delivered shot with no grade. Those are legitimate experimental
        OUTCOMES and must never withhold the capture, or the gate becomes a selection filter.
        """
        corrupt, absent = [], []
        bad, miss = corrupt.append, absent.append

        by_ev = defaultdict(list)
        for w in self.worker:
            by_ev[_i(_d(w).get("event"))].append(w)
        # BLOCKER 5: obligations follow OBSERVED TRANSITIONS, not the terminal LABEL. A shot that
        # demonstrably issued a command owes its execution evidence whatever the label says; a kill
        # BEFORE any command owes no dispatch; and labelling a terminal `ungraded` erases nothing.
        delivered = bool(by_ev[W_DISPATCH] or self.release)
        state = self.terminal_state()

        if not self.terminal:
            miss("no shot_terminal: the record never closed")
        elif state is None:
            bad("contradictory shot_terminal records")
        if state == "released" and not delivered:
            bad("terminal=released but neither a dispatch nor a release_link exists")
        if delivered and state in ("cancelled", "unanswered"):
            bad(f"terminal={state} but a command was issued: a label does not erase a command")

        # -- press ------------------------------------------------------------------------
        if len(self.press) > 1:
            sigs = {json.dumps(_d(p), sort_keys=True) for p in self.press}
            (bad if len(sigs) > 1 else miss)(
                f"shot_press x{len(self.press)}" + (" with contradictory payloads" if len(sigs) > 1
                                                    else " (identical repeats)"))
        press_us = _i(_d(self.press[0]).get("press_engine_us")) if self.press else None
        owed = bad if delivered else miss          # a delivered shot OWES its timing chain
        if not self.press:
            owed("no shot_press")
        elif press_us is None:
            owed("shot_press carries no press_engine_us")

        # -- onset ------------------------------------------------------------------------
        if not self.native_onset:
            owed("no native_onset: the bucket actually used is unknown")
        seen_attempts = []
        for n in self.native_onset:
            d = _d(n)
            at = _i(d.get("attempt"))
            if at is None:
                miss("native_onset carries no attempt")
            else:
                seen_attempts.append(at)
            if _i(d.get("valid")) != 1:
                bad(f"native_onset valid={d.get('valid')!r}: not an accepted onset")
            on, fm = _i(d.get("native_onset_us")), _i(d.get("first_meter_seen_engine_us"))
            if on is None:
                miss("native_onset carries no native_onset_us")
            elif on < 0:
                bad(f"native_onset_us is negative ({on})")
            elif press_us is not None and fm is not None and on != fm - press_us:
                # The declared duration must equal its own endpoints, or one of the three is wrong.
                bad(f"native_onset_us {on} does not equal first_meter_seen - press "
                    f"({fm} - {press_us} = {fm - press_us})")
            for k in ("native_onset_engine_us", "latched_engine_us"):
                if _i(d.get(k)) is None:
                    owed(f"native_onset carries no {k}")
            if d.get("resolved_trim_key") in (None, "", "unknown"):
                miss("native_onset resolved_trim_key is unknown: the bucket that supplied the "
                     "trim is not observed")
            if d.get("effective_tempo") in (None, "", "unknown"):
                miss("native_onset effective_tempo is unknown")
        if len(set(seen_attempts)) != len(seen_attempts):
            bad("duplicate native_onset for one attempt -- AMBIGUOUS")

        man = _d(capture.manifests[0]) if capture.manifests else {}
        cfg = _i(man.get("config_revision")) if man else None
        for n in self.native_onset:
            got = _i(_d(n).get("config_revision"))
            if cfg is not None and got is not None and got != cfg:
                bad(f"native_onset config_revision {got} != manifest {cfg}")

        # -- assignment -------------------------------------------------------------------
        if not self.assignment:
            miss("no probe_assignment: this shot is outside the experiment accounting")
        aids = {_i(_d(r).get("assignment_id")) for r in self.assignment}
        if len(aids) > 1:
            bad("ASSIGNMENT CHANGED within one physical shot")
        aid = aids.pop() if len(aids) == 1 else None
        arms = _arm_doses(man)
        for a in self.assignment:
            d = _d(a)
            if man and d.get("experiment_id") != man.get("experiment_id"):
                bad(f"assignment experiment_id {d.get('experiment_id')!r} != manifest "
                    f"{man.get('experiment_id')!r}")
            bound = _i(man.get("max_abs_offset_us"))
            dose = _i(d.get("assigned_delta_us"))
            if dose is None:
                miss("assignment carries no assigned_delta_us")
            else:
                if bound is not None and abs(dose) > bound:
                    bad(f"assigned dose {dose} us exceeds the manifest bound {bound} us")
                if arms and dose not in arms:
                    bad(f"assigned dose {dose} us is not one of the manifest arms {sorted(arms)}")
                lbl = _arm_label_dose(d.get("arm_id"))
                if lbl is not None and lbl != dose:
                    bad(f"arm_id {d.get('arm_id')!r} contradicts the dose {dose}")
            pw = _rational(d.get("assignment_probability"))
            if d.get("assignment_probability") is not None and pw is None:
                bad(f"assignment_probability {d.get('assignment_probability')!r} is not an "
                    "exact rational n/d with 0 < n <= d")
            for k in ("eligibility_fixed_engine_us", "assigned_engine_us"):
                if _i(d.get(k)) is None:
                    miss(f"assignment carries no {k}")

        elig = [_i(_d(a).get("eligibility_fixed_engine_us")) for a in self.assignment]
        asg = [_i(_d(a).get("assigned_engine_us")) for a in self.assignment]
        latch = [_i(_d(n).get("latched_engine_us")) for n in self.native_onset]
        if elig and latch and None not in elig and None not in latch and min(elig) < min(latch):
            bad("ELIGIBILITY FIXED BEFORE ONSET WAS LATCHED")
        if elig and asg and None not in elig and None not in asg and min(asg) < max(elig):
            bad("ASSIGNMENT COMMITTED BEFORE ELIGIBILITY WAS FIXED")
        plan_now = [_i(_d(p).get("now_engine_us")) for p in self.plans]
        if asg and plan_now and None not in asg and None not in plan_now \
                and min(plan_now) < max(asg):
            bad("a schedule_plan precedes the assignment it claims to carry")

        # -- plans ------------------------------------------------------------------------
        if not self.plans:
            owed("no schedule_plan: no deadline lineage")
        dupes = self.duplicate_plan_ids()
        if dupes:
            bad(f"REUSED operation_id {dupes} across contradictory schedule_plan records "
                "-- AMBIGUOUS, and never resolved by first-wins")
        for p in self.plans:
            d = _d(p)
            for k in ("baseline_pre_offset_deadline_us", "assigned_delta_us",
                      "post_offset_deadline_us", "post_phase_lock_deadline_us",
                      "final_requested_deadline_us", "clipped_past_deadline",
                      "zero_offset_shadow_deadline_us", "handoff_deadline_us"):
                if _i(d.get(k)) is None:
                    miss(f"schedule_plan missing {k}")
            if _i(d.get("operation_id")) is None:
                miss("schedule_plan carries no operation_id")
            got = _i(d.get("config_revision"))
            if got is None:
                miss("a schedule_plan carries no config_revision: configuration continuity is "
                     "unevidenced, which is not the same as unchanged")
            elif cfg is not None and got != cfg:
                bad(f"plan config_revision {got} != manifest {cfg} with no accounted transition")
            got_a = _i(d.get("assignment_id"))
            if got_a is None:
                miss("schedule_plan carries no assignment_id")
            elif aid is not None and got_a != aid:
                bad(f"schedule_plan references assignment {got_a!r}, not {aid}")

        # -- execution --------------------------------------------------------------------
        if delivered and not self.worker:
            bad("a command was issued but there are NO worker records")
        if delivered and not self.release:
            miss("a command was issued but no release_link was recorded")
        full, _dl, lw = self.lineage()
        for r in self.release:
            d = _d(r)
            if _i(d.get("native_release_seq")) is None:
                miss("release_link carries no native_release_seq")
            if "release_seq" in d:
                bad("release_link uses the AMBIGUOUS name `release_seq`: "
                    + "; ".join(f"{k} is {v}" for k, v in COLLIDING_NAMES.items()))
            got_a = _i(d.get("assignment_id"))
            if got_a is None:
                miss("release_link carries no assignment_id")
            elif aid is not None and got_a != aid:
                bad("release_link references a different assignment")
            lc = _i(d.get("local_command_us"))
            if lc is None:
                miss("release_link carries no local_command_us")
            elif press_us is not None and lc < press_us:
                bad(f"release_link command {lc} precedes the press {press_us}")
            if full and not lw:
                got = (_i(d.get("token")), _i(d.get("route_generation")) if
                       _i(d.get("route_generation")) is not None else _i(r.get("generation")),
                       _i(d.get("operation_id")), _i(d.get("target_revision")))
                if got != full:
                    bad(f"release_link scope {got} contradicts the dispatched scope {full}")
        sigs = {json.dumps(_d(r), sort_keys=True) for r in self.release}
        if len(sigs) > 1:
            bad(f"{len(self.release)} contradictory release_link records")
        for t in self.terminal:
            got_a = _i(_d(t).get("assignment_id"))
            if got_a is None:
                miss("shot_terminal carries no assignment_id")
            elif aid is not None and got_a != aid:
                bad("shot_terminal references a different assignment")

        # -- BLOCKER 2: the executed plan must EXPLAIN the accepted target -----------------
        plan = self.executed_plan()
        if plan is not None and full and not lw:
            handoff = _i(_d(plan).get("handoff_deadline_us"))
            acc = _dl
            if handoff is not None and acc is not None and handoff != acc:
                bad(f"the worker accepted {acc} us but the executed plan handed off {handoff} us "
                    f"({acc - handoff:+d} us unaccounted): the plan does not explain the target")
            if _d(plan).get("disposition") in ("refused", "rejected", "superseded"):
                bad(f"the executed plan's disposition is {_d(plan).get('disposition')!r}: a plan "
                    "that did not arm cannot be the one that executed")

        corrupt.extend(lw)
        if capture.blocking:
            bad("capture is lossy or incomplete: " + ", ".join(capture.blocking))
        return corrupt, absent

    def integrity(self, capture):
        corrupt, _absent = self.classify(capture)
        return (CORRUPT if corrupt else INTACT), corrupt

    def measurement(self, capture):
        corrupt, absent = self.classify(capture)
        why = corrupt + absent
        return (COMPLETE if not why else INCOMPLETE), why


    # ---------------------------------------------------------------- clock
    def clock(self, capture):
        if not self.reader_onset:
            return CLOCK_ENGINE_ONLY, ["no reader_onset: nothing crosses a clock domain here"]
        if not capture.bridges.get((self.owner, self.key[0])):
            return CLOCK_ENGINE_ONLY, [
                f"no clock_bridge for {self.owner} context {self.key[0]}: engine-clock quantities "
                "remain available; the reader/native cross-check does not"]
        why = []
        for n in self.native_onset:
            t = _i(_d(n).get("native_onset_engine_us"))
            if t is None:
                continue
            _q, _u, w = capture.convert(self.owner, self.key[0], t)
            if w:
                why.append(f"native_onset: {w}")
        return (CLOCK_OK if not why else CLOCK_REFUSED), (why or ["conversion available"])

    # ---------------------------------------------------------------- adherence
    def adherence(self, capture):
        """Did the EXECUTED plan carry the assigned SCHEDULING displacement?

        This is a SCHEDULING quantity -- final requested deadline minus the same-plan zero-offset
        shadow. It is NOT delivered-command displacement and it supplies no counterfactual command
        time for an untreated shot; command tardiness is reported separately.

        Only the executed plan is judged. A rejected or superseded plan's violation is a separate
        diagnostic, not evidence about the treatment that actually ran.
        """
        if not self.assignment:
            return UNVERIFIABLE, ["no probe_assignment"]
        doses = {_i(_d(a).get("assigned_delta_us")) for a in self.assignment}
        if len(doses) != 1 or None in doses:
            return UNVERIFIABLE, [f"assigned_delta_us not single-valued: {sorted(map(str, doses))}"]
        want = doses.pop()
        plan = self.executed_plan()
        if plan is None:
            dupes = self.duplicate_plan_ids()
            if dupes:
                return UNVERIFIABLE, [f"operation_id {dupes} is carried by contradictory "
                                      "schedule_plan records: there is no single executed plan"]
            return UNVERIFIABLE, ["no EXECUTED plan: the dispatched operation cannot be tied to a "
                                  "schedule_plan, so effective displacement is unobservable"]
        d = _d(plan)
        shadow = _i(d.get("zero_offset_shadow_deadline_us"))
        final = _i(d.get("final_requested_deadline_us"))
        if shadow is None or final is None:
            return UNVERIFIABLE, ["the executed plan has no zero-offset shadow / final deadline; "
                                  "REQUESTED displacement must not be substituted for EFFECTIVE"]
        why = []
        if _i(d.get("clipped_past_deadline")):
            why.append(f"CLIPPED: the offset targeted the past (op {d.get('operation_id')})")
        got = final - shadow
        if abs(got - want) > DISPLACEMENT_FIDELITY_US:
            why.append(f"effective scheduling displacement {got} us vs assigned {want} us "
                       f"(error {abs(got - want)} us > {DISPLACEMENT_FIDELITY_US} us fidelity "
                       "threshold)")
        note = (f"; {len(self.plans) - 1} superseded plan(s) not judged here"
                if len(self.plans) > 1 else "")
        if why:
            return NONADHERENT, [w + note for w in why]
        return ADHERENT, [f"executed plan displacement {got} us, within "
                          f"{DISPLACEMENT_FIDELITY_US} us of assignment{note}"]

    # ---------------------------------------------------------------- outcome
    def outcome(self):
        if not self.terminal:
            return UNKNOWN, ["no shot_terminal: the record never closed"], None
        states = {_d(t).get("terminal_state") for t in self.terminal}
        bad = states - TERMINALS
        if bad:
            return UNKNOWN, [f"unknown terminal_state {sorted(map(str, bad))}"], None
        if len(states) > 1:
            return DISPUTED, [f"contradictory terminal states {sorted(states)}"], None
        state = states.pop()
        if not self.verdict:
            return UNGRADED, [f"terminal={state}, no shot_verdict"], None
        vs = {_d(v).get("verdict") for v in self.verdict}
        bad = vs - VERDICTS
        if bad:
            return DISPUTED, [f"unknown verdict {sorted(map(str, bad))}"], None
        if len(vs) > 1:
            return DISPUTED, [f"contradictory verdicts {sorted(vs)}"], None
        v = vs.pop()
        att = {_d(x).get("attribution") for x in self.verdict}
        if att != {"attributed"}:
            return DISPUTED, [f"verdict attribution unresolved: {sorted(map(str, att))}"], v
        if state != "released":
            return DISPUTED, [f"graded {v} but terminal_state={state}"], v
        return (GRADED if v != "UNKNOWN" else UNGRADED), [f"terminal={state}"], v


class ProbeCapture:
    def __init__(self, journals):
        self.journals = journals
        self.shots = {}
        self.manifests = []
        self.bridges = defaultdict(list)   # (owner identity, context) -> clock_bridge rows
        self.unattached = []

    @property
    def blocking(self):
        """Container completeness on THIS schema.

        `Journal.completeness()` is schema 1's, so it reports FOREIGN_SCHEMA for every schema 2
        header. That flag is RE-DECIDED here, never filtered by a substring allowlist -- an
        allowlist is how ID_OUT_OF_ORDER once leaked past this project's blocking list.
        """
        out = []
        seen = {}
        for j in self.journals:
            name = j.label or j.path.name
            got = _i((j.header or {}).get("schema"))
            for f in j.completeness():
                if not f.startswith("FOREIGN_SCHEMA:"):
                    out.append(f"{name}:{f}")
            if got != SCHEMA:
                out.append(f"{name}:FOREIGN_SCHEMA:{got!r} (probe schema {SCHEMA} required)")
            if j.identity in seen:
                out.append(f"{name}:DUPLICATE_PROCESS_IDENTITY {j.identity}")
            seen[j.identity] = j
        return out

    def build(self):
        for j in self.journals:
            for e in j.events:
                st = e.get("stage")
                if st == "experiment_manifest":
                    self.manifests.append(e)
                    continue
                if st == "clock_bridge":
                    ctx = _i(e.get("context"))
                    if ctx:
                        self.bridges[(j.identity, ctx)].append(e)
                    else:
                        self.unattached.append((st, e, "clock_bridge has no engine context"))
                    continue
                if st not in SHOT_STAGES:
                    continue
                if j.role == "python" and st not in PYTHON_STAGES:
                    self.unattached.append((st, e, f"stage {st} emitted by a python journal"))
                    continue
                owner, ow = _owner(e, j)
                if owner is None:
                    self.unattached.append((st, e, ow))
                    continue
                key, why = _shot_key(e)
                if key is None:
                    self.unattached.append((st, e, why))
                    continue
                s = self.shots.get((owner, key))
                if s is None:
                    s = self.shots[(owner, key)] = Shot(key, owner)
                s.add(st, e)
        for k in self.bridges:
            self.bridges[k].sort(key=lambda r: _i(_d(r).get("engine_us")) or 0)
        return self

    def _qpc_hz(self, owner):
        for j in self.journals:
            if j.identity == owner:
                return _i((j.header or {}).get("qpc_hz"))
        return None

    def clock_tolerance_us(self):
        if not self.manifests:
            return None
        return _i(_d(self.manifests[0]).get("clock_tolerance_us"))

    def convert(self, owner, ctx, engine_us):
        """Engine microseconds -> QPC ticks for ONE process, or a refusal.

        Two defects this exists not to repeat: microseconds were once added straight to tick
        counts, and bridges were once keyed on the context NUMBER, so a second process that also
        chose context 7 borrowed the first one's segment. Conversion now scales by the owning
        journal's own qpc_hz and the bridge is scoped to (process identity, context).
        """
        tol = self.clock_tolerance_us()
        if tol is None:
            return None, None, "manifest declares no clock_tolerance_us"
        hz = self._qpc_hz(owner)
        if not hz or hz <= 0:
            return None, None, f"owning journal {owner} declares no usable qpc_hz"
        segs = self.bridges.get((owner, ctx)) or []
        if not segs:
            return None, None, f"no clock_bridge for {owner} context {ctx}"
        applicable, spans = [], []
        for b in segs:
            d = _d(b)
            lo, hi = _i(d.get("valid_from_engine_us")), _i(d.get("valid_to_engine_us"))
            if lo is None or hi is None:
                continue
            spans.append((lo, hi))
            if lo <= engine_us <= hi:
                applicable.append(b)
        if not applicable:
            if any(hi < engine_us for _lo, hi in spans) and any(lo > engine_us for lo, _h in spans):
                return None, None, (f"engine time {engine_us} falls in a CLOCK DISCONTINUITY "
                                    f"between sync segments {spans}")
            return None, None, f"engine time {engine_us} is outside every sync segment {spans}"
        if len(applicable) > 1:
            ids = sorted(str(_d(b).get("sync_id")) for b in applicable)
            return None, None, f"overlapping sync segments {ids} both claim this timestamp"
        d = _d(applicable[0])
        unc = _i(d.get("uncertainty_us"))
        if unc is None or unc < 0:
            return None, None, f"sync segment {d.get('sync_id')} declares no valid uncertainty"
        if unc > tol:
            return None, None, f"sync uncertainty {unc} us exceeds the manifest tolerance {tol} us"
        eng0, qpc0 = _i(d.get("engine_us")), _i(d.get("qpc"))
        if eng0 is None or qpc0 is None:
            return None, None, f"sync segment {d.get('sync_id')} has no anchor pair"
        # MICROSECONDS ARE NOT TICKS. Scale by the owning process's own qpc_hz.
        return qpc0 + round((engine_us - eng0) * hz / 1_000_000), unc, ""

    def manifest_flags(self):
        f = []
        if not self.manifests:
            return ["NO_EXPERIMENT_MANIFEST"]
        if len({json.dumps(_d(m), sort_keys=True) for m in self.manifests}) > 1:
            f.append("CONTRADICTORY_MANIFESTS")
        d = _d(self.manifests[0])
        for k in ("experiment_id", "protocol_version", "build_hash", "schema_hash",
                  "config_revision", "max_abs_offset_us", "randomisation_seed",
                  "learning_freeze_mask", "eligibility", "stopping_rule", "arms_us",
                  "clock_tolerance_us"):
            if d.get(k) in (None, ""):
                f.append(f"MANIFEST_MISSING:{k}")
        # TOKEN membership, not substring: "not_banner_trim" CONTAINS "banner_trim".
        mask = d.get("learning_freeze_mask")
        if mask in (None, ""):
            pass                                   # already flagged as missing
        elif isinstance(mask, (str, list, tuple)):
            tokens = ({t.strip() for t in mask.split(",") if t.strip()}
                      if isinstance(mask, str) else {str(t).strip() for t in mask})
            for need in ("banner_trim", "oracle"):
                if need not in tokens:
                    f.append(f"LEARNING_NOT_FROZEN:{need}")
        else:
            f.append(f"MANIFEST_BAD:learning_freeze_mask is {type(mask).__name__}, "
                     "not a token list")
        raw = d.get("max_abs_offset_us")
        if raw not in (None, ""):
            cap = _i(raw)
            if cap is None:
                f.append(f"MANIFEST_BAD:max_abs_offset_us={raw!r}")
            elif cap <= 0:
                f.append(f"MANIFEST_BAD:max_abs_offset_us={cap} (must be positive)")
            elif cap > HOOK_BOUND_US:
                f.append(f"BOUND_EXCEEDS_HOOK_LIMIT:{cap}us "
                         f"(devFireOffsetForShotMs caps at {HOOK_BOUND_US})")
        return f

    def gate(self):
        """Is this CAPTURE ready to support an experiment? Separate from every shot verdict.

        Rows are always preserved; readiness is what gets withheld.
        """
        why = list(self.manifest_flags())
        if self.blocking:
            why.append("capture lossy/incomplete: " + ", ".join(self.blocking))
        if not self.shots:
            why.append("NO SHOTS: the capture contains no identified physical shot")
        if self.unattached:
            why.append(f"{len(self.unattached)} record(s) carry no usable identity")

        # BLOCKER 1. The old rule asked whether ANY shot reached COMPLETE, so ONE healthy shot
        # re-admitted every corrupt one beside it -- all twelve original mutations passed again
        # when accompanied by a clean shot. Readiness is withheld for UNRESOLVED CORRUPTION
        # ANYWHERE in the capture.
        #
        # What it deliberately does NOT do is require every shot to be adherent and graded. A
        # non-adherent, cancelled or ungraded shot is a legitimate experimental OUTCOME; demanding
        # those pass would turn the gate into a selection filter and bias the very estimate it
        # exists to protect.
        corrupt = []
        for (owner, key), shot in sorted(self.shots.items(), key=lambda kv: str(kv[0])):
            verdict, reasons = shot.integrity(self)
            if verdict == CORRUPT:
                corrupt.append((key, reasons))
        if corrupt:
            why.append(f"{len(corrupt)} of {len(self.shots)} shot(s) carry UNRESOLVED EVIDENCE "
                       "CORRUPTION; the capture cannot be reconciled")
            for key, reasons in corrupt[:5]:
                why.append(f"    ctx {key[0]} epoch {key[1]}: {reasons[0]}")
            if len(corrupt) > 5:
                why.append(f"    ... and {len(corrupt) - 5} more")

        rows = self.report()
        if rows and not [r for r in rows if r["assignment_id"] is not None]:
            why.append("no shot carries an assignment: nothing is under experimental accounting")
        return (GATE_PASS if not why else GATE_WITHHELD), why

    def report(self):
        rows = []
        for (owner, key), s in sorted(self.shots.items(), key=lambda kv: str(kv[0])):
            m, mw = s.measurement(self)
            integ, _ireasons = s.integrity(self)
            a, aw = s.adherence(self)
            o, ow, v = s.outcome()
            ck, ckw = s.clock(self)
            full, dl, _lw = s.lineage()
            ad = _d(s.assignment[0]) if s.assignment else {}
            rows.append(dict(
                owner=str(owner), context=key[0], physical_epoch=key[1],
                assignment_id=_i(ad.get("assignment_id")), arm_id=ad.get("arm_id"),
                assigned_delta_us=_i(ad.get("assigned_delta_us")),
                block_id=_i(ad.get("block_id")), stratum_id=_i(ad.get("stratum_id")),
                assignment_probability=ad.get("assignment_probability"),
                eligible=_i(ad.get("eligible")),
                measurement=m, integrity=integ, adherence=a, outcome=o, clock=ck, verdict=v,
                dispatched_scope=str(full) if full else None, accepted_deadline_us=dl,
                tardiness_us=s.tardiness_us(),
                measurement_why="; ".join(mw), adherence_why="; ".join(aw),
                outcome_why="; ".join(ow), clock_why="; ".join(ckw)))
        return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir")
    ap.add_argument("--file", nargs="*", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--csv")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.file]
    if args.dir:
        paths.extend(sorted(Path(args.dir).rglob("*.jsonl")))
    if not paths:
        ap.error("no journals: pass --dir or --file")
    cap = ProbeCapture([load(p) for p in paths]).build()
    rows = cap.report()
    gate, gate_why = cap.gate()

    print("== capture ==")
    for j in cap.journals:
        flags = [f for f in j.completeness() if not f.startswith("FOREIGN_SCHEMA:")]
        got = _i((j.header or {}).get("schema"))
        if got != SCHEMA:
            flags.append(f"FOREIGN_SCHEMA:{got!r}")
        print(f"  {j.path.name}: {j.label or '(no header)'} events={len(j.events)} "
              f"schema={got} flags={','.join(flags) or 'none'}")
    if cap.unattached:
        print(f"UNATTACHED   : {len(cap.unattached)} record(s) carry no usable identity, HELD")
        for st, _e, why in cap.unattached[:8]:
            print(f"    - {st}: {why}")

    print(f"\n== shots ({len(rows)}) ==")
    shown = rows if args.all else [r for r in rows
                                   if not (r["measurement"] == COMPLETE
                                           and r["adherence"] == ADHERENT
                                           and r["outcome"] == GRADED)]
    for r in shown:
        print(f"\nshot ctx {r['context']} epoch {r['physical_epoch']} "
              f"assignment={r['assignment_id']} arm={r['arm_id']} "
              f"dose={r['assigned_delta_us']} block={r['block_id']}")
        print(f"  measurement : [{r['measurement']}] {r['measurement_why']}")
        if r["integrity"] == CORRUPT:
            print(f"  integrity   : [CORRUPT] this shot withholds the WHOLE capture's readiness")
        print(f"  adherence   : [{r['adherence']}] {r['adherence_why']}")
        print(f"  outcome     : [{r['outcome']}] {r['outcome_why']}"
              + (f"  verdict={r['verdict']}" if r["verdict"] else ""))
        if r["clock"] != CLOCK_OK:
            print(f"  clock       : [{r['clock']}] {r['clock_why']}")
        if r["tardiness_us"] is not None:
            print(f"  tardiness   : {r['tardiness_us']} us after the dispatched deadline "
                  "(NOT console or game receipt)")

    print("\n== accounting, reported SEPARATELY (never collapsed into one filter) ==")
    for field in ("measurement", "integrity", "adherence", "outcome", "clock"):
        c = Counter(r[field] for r in rows)
        print(f"  {field:<12} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print("  by arm       " + "  ".join(
        f"{k}={v}" for k, v in sorted(Counter(str(r["arm_id"]) for r in rows).items())))

    print(f"\n== GATE: {gate} ==")
    for w in gate_why:
        print(f"  - {w}")
    print("\nScheduling displacement is a SCHEDULING quantity; command tardiness is reported\n"
          "separately and neither supplies an untreated counterfactual. ENGINE_ONLY is not a\n"
          "failure. Non-adherent assignments are REPORTED with their arm and dose, never dropped:\n"
          "excluding them would bias the estimate this consumer exists to protect. A missing grade\n"
          "is not EXCELLENT. No record here implies console or game receipt.")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv}")
    return 0 if gate == GATE_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
