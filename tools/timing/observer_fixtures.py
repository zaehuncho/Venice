#!/usr/bin/env python
"""observer_fixtures.py -- adversarial fixtures for the Orion timing-observer consumer.

    python tools/timing/observer_fixtures.py --out <dir>      # write the fixture corpus
    python tools/timing/observer_fixtures.py --list           # print the case table

Schema under review: D:\\NexusVision\\diagnostics\\timing-observer-20260920-190810\\
TIMING_OBSERVER_SCHEMA.json (DRAFT). The emitter is owned by the input/transport lane; this file is
the CONSUMER side's test corpus and is written BEFORE the consumer so the consumer is built against
cases rather than against optimism.

WHY THIS EXISTS. The previous trace consumer passed 39 synthetic unit tests and still shipped two
defects that only a real two-process capture exposed: it merged records across fork contexts that
legitimately reused an id, and it compared record ids ACROSS PROCESSES, flagging every healthy
transaction as out of order. Both were identity bugs, not field bugs. So every case below attacks
IDENTITY or COMPLETENESS, which is where that consumer actually failed -- not the field list, which
is where it did not.

Each case pairs a generated trace with the verdict a correct consumer MUST produce. The expectation
is the point: a consumer that reports a confident finding where the data cannot support one is
wrong even if it never crashes.

HARD BOUNDARY: analysis tooling only; never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA = 1

# Expectation vocabulary the consumer must be able to express.
COMPLETE = "COMPLETE"            # the chain is observed and internally consistent
AMBIGUOUS = "AMBIGUOUS"          # identity does not resolve; MUST NOT be silently merged
UNKNOWN = "UNKNOWN"              # evidence could be missing; absence proves nothing
CONTRADICTS = "CONTRADICTS"      # two observed records disagree
REJECT_JOIN = "REJECT_JOIN"      # a join was attempted across incompatible generations


def _hdr(role, pid, capture_id="cap-1", qpc_hz=10_000_000, instance="1"):
    """Container header per the SETTLED schema. native and python have DIFFERENT required keys.

    An earlier draft of these fixtures omitted sync_uncertainty and capacity, which made every
    control case read as incomplete. That was caught by running the consumer against the corpus --
    which is the entire reason the corpus exists.
    """
    h = {"type": "header", "schema": SCHEMA, "trace_kind": "timing", "role": role,
         "capture_id": capture_id, "pid": pid, "instance": instance, "capacity": 4096}
    if role == "python":
        h.update({"clock": "perf_counter_ns", "clock_hz": 1_000_000_000,
                  "sync_clock": "500000000", "sync_unix_ns": "1", "sync_uncertainty_ns": "1000"})
    else:
        h.update({"qpc_hz": qpc_hz, "sync_qpc": "1000", "sync_unix_us": "1",
                  "sync_uncertainty_qpc": "10"})
    return h


def _foot(reason="shutdown", dropped=0, accepted=None, written=None, pending=0):
    f = {"type": "footer", "now_us": "999999", "reason": reason, "pending": pending,
         "dropped_full": str(dropped), "dropped_contention": "0", "dropped_clock": "0"}
    if accepted is not None:
        f["accepted"] = str(accepted)
    if written is not None:
        f["written"] = str(written)
    return f


def _ev(stage, **data):
    d = {"type": "event", "stage": stage}
    d.update(data)
    return d


def _sampler_input(attempt, input_id, frame, fill, disposition, feed=1, n_before=0, n_after=1,
                   sampler_generation=1, ruler=1):
    return _ev("sampler_input", attempt=attempt, input_id=input_id, feed=feed, frame=frame,
               measurement_epoch_us=str(1_000_000 + frame * 16667),
               capture_engine_us=str(1_000_000 + frame * 16667),
               fill_micropct=int(fill * 1_000_000), ruler=ruler, ruler_mode=1,
               disposition=disposition, ruler_reset=0, n_before=n_before, n_after=n_after,
               tail_before_us="0", tail_after_us="33000", sampler_generation=sampler_generation)


def _decision_fit(attempt, decision_id, n, span_us, weight_sum=3.0, weight_sq=2.0,
                  source=1, phase_primary=1, sampler_authoritative=0, sampler_generation=1):
    return _ev("decision_fit", attempt=attempt, token=attempt, decision_id=decision_id,
               input_id=n, frame=n, now_us="1100000", selected_tip_us="1500000",
               selected_sigma_us="13000", combined_sigma_us="13000", selected_source=source,
               valid=1, phase_primary=phase_primary, sampler_authoritative=sampler_authoritative,
               n=n, span_us=str(span_us), first_sample_us="1000000",
               last_sample_us=str(1_000_000 + 20 * 16667),
               weight_sum_q9=int(weight_sum * 1_000_000_000),
               weight_sq_sum_q9=int(weight_sq * 1_000_000_000),
               wrss_q9=0, lambda_q9=500_000_000, slope_q9=185_000_000, used_quad=0,
               peak_fallback=0, crossing_us="1500000", sampler_sigma_us="20000",
               confidence_q6=1_000_000, frame_age_us="18500",
               sampler_generation=sampler_generation, imminent_us="80000", phase_solo=1,
               curve_alpha_q9=0, ruler=1)


def _decision_members(attempt, decision_id, hypothetical_weight=0.18):
    return _ev("decision_members", attempt=attempt, token=attempt, decision_id=decision_id,
               input_id=3, phase_anchor_us="1000000", anchor_micropct=20_000_000,
               phase_tip_us="1500000", phase_sigma_us="13000",
               phase_hypothetical_weight_q9=int(hypothetical_weight * 1_000_000_000),
               phase_stretch_q9=1_000_000_000, stretch_latched=0,
               phase_constant_us="403300", phase_solo=1, curve_enabled=0,
               reference_clock_enabled=0, registration_tip_us=None,
               registration_sigma_us=None, registration_applied_weight_q9=0,
               sampler_crossing_us="1500000", horizon_debias_us="0", phase_primary=1,
               sampler_sole_slope_refused=1)


def _native_frame(frame, detected=1, raw_fed=1, integrity_generation=1):
    return _ev("native_frame", frame=frame, processed_seq=frame,
               capture_epoch_us=str(1_000_000 + frame * 16667),
               measurement_epoch_us=str(1_000_100 + frame * 16667),
               detector_done_epoch_us=str(1_005_000 + frame * 16667),
               queued_epoch_us=str(1_006_000 + frame * 16667),
               emit_epoch_us=str(1_007_000 + frame * 16667),
               receipt_epoch_us=str(1_010_000 + frame * 16667),
               effective_age_us="18500", detected=detected, raw_fed=raw_fed, ruler=1,
               integrity_generation=integrity_generation)


def _py(stage, frame, source_generation=1, source_identity="src-A", context=7, **kw):
    d = {"type": "event", "stage": stage, "frame": frame, "context": context,
         "source_generation": source_generation, "source_identity": source_identity,
         "capture_ns": str(500_000_000 + frame * 16_667_000),
         "publication_ns": str(501_000_000 + frame * 16_667_000)}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------------------------
# The cases. Each returns (files, expectation, why).
# ---------------------------------------------------------------------------------------------

def case_clean():
    native = [_hdr("native", 100)]
    for f in range(1, 7):
        native.append(_native_frame(f))
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, n_before=f - 1, n_after=f))
    native += [_decision_fit(1, 1, n=6, span_us=100_000),
               _decision_members(1, 1),
               _foot(accepted=len(native), written=len(native))]
    return ({"native.jsonl": native}, COMPLETE,
            "six accepted live inputs, one decision, clean footer -- the control case")


def case_seed_replay_not_availability():
    """feed=2 is ownership-seed REPLAY. Counting it as an available frame inflates support."""
    native = [_hdr("native", 100)]
    for f in range(1, 4):
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, feed=2, n_after=f))
    for f in range(4, 6):
        native.append(_native_frame(f))
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, feed=1, n_after=f))
    native += [_decision_fit(1, 1, n=5, span_us=80_000), _decision_members(1, 1),
               _foot(accepted=9, written=9)]
    return ({"native.jsonl": native}, COMPLETE,
            "n=5 but only TWO live frames; a consumer reporting 5 available frames is WRONG")


def case_rejected_inputs_are_recorded():
    """Rejections must reduce support without disappearing from the record."""
    native = [_hdr("native", 100)]
    disp = [5, 3, 5, 4, 5, 2]     # accepted, near_duplicate, accepted, backward, accepted, nonadvancing
    n = 0
    for f, d in enumerate(disp, 1):
        native.append(_native_frame(f))
        if d == 5:
            n += 1
        native.append(_sampler_input(1, f, f, 20 + f, disposition=d, n_before=n - (1 if d == 5 else 0),
                                     n_after=n))
    native += [_decision_fit(1, 1, n=3, span_us=66_000), _decision_members(1, 1),
               _foot(accepted=13, written=13)]
    return ({"native.jsonl": native}, COMPLETE,
            "6 frames offered, 3 accepted -- availability 6 and support 3 must both be reportable")


def case_reused_decision_id():
    native = [_hdr("native", 100)]
    native += [_decision_fit(1, 7, n=4, span_us=66_000), _decision_members(1, 7),
               _decision_fit(1, 7, n=6, span_us=99_000), _decision_members(1, 7),
               _foot(accepted=4, written=4)]
    return ({"native.jsonl": native}, AMBIGUOUS,
            "decision_id 7 twice in one attempt -- MUST NOT be merged or silently last-wins")


def case_generation_mismatch():
    native = [_hdr("native", 100)]
    native += [_native_frame(5, integrity_generation=1),
               _sampler_input(1, 5, 5, 25, disposition=5, sampler_generation=2),
               _decision_fit(1, 1, n=3, span_us=50_000, sampler_generation=1),
               _foot(accepted=3, written=3)]
    return ({"native.jsonl": native}, REJECT_JOIN,
            "input carries sampler_generation 2, the fit claims 1 -- support is NOT this input's")


def case_frame_underspecified_identity():
    """CORRECTED after source review: frame 5 in two DISTINCT source_generations is NORMAL --
    the scoped identities differ and nothing is ambiguous. The real trap is an UNDERSPECIFIED
    identity, where the generation or context is missing and the join cannot be resolved at all."""
    py = [_hdr("python", 200)]
    py += [_py("capture_publish", 5, source_generation=1, read_start_ns="1", read_return_ns="2",
               isolate_done_ns="3", hw_pts_valid=1, pll_enabled=0),
           _py("capture_publish", 5, source_generation=2, read_start_ns="4", read_return_ns="5",
               isolate_done_ns="6", hw_pts_valid=1, pll_enabled=0),
           _py("capture_publish", 5, source_generation=None, context=None, read_start_ns="7",
               read_return_ns="8", isolate_done_ns="9", hw_pts_valid=1, pll_enabled=0),
           _foot(accepted=3, written=3)]
    return ({"python.jsonl": py}, AMBIGUOUS,
            "two distinct generations are FINE; the third row has source_generation=null, so its "
            "identity is underspecified and the join is unresolvable")


def case_missing_footer():
    native = [_hdr("native", 100)]
    for f in range(1, 4):
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, n_after=f))
    native.append(_decision_fit(1, 1, n=3, span_us=50_000))
    return ({"native.jsonl": native}, UNKNOWN,
            "no footer: the journal may be truncated, so a low support count proves nothing")


def case_dropped_records():
    native = [_hdr("native", 100)]
    for f in range(1, 4):
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, n_after=f))
    native += [_decision_fit(1, 1, n=3, span_us=50_000), _foot(dropped=4, accepted=4, written=4)]
    return ({"native.jsonl": native}, UNKNOWN,
            "4 dropped records: n=3 may be an artifact of loss, not the sampler's actual support")


def case_python_journal_no_loss_accounting():
    py = [_hdr("python", 200)]
    py += [_py("capture_publish", 1, read_start_ns="1", read_return_ns="2", isolate_done_ns="3",
               hw_pts_valid=1, pll_enabled=0)]
    return ({"python.jsonl": py}, UNKNOWN,
            "python side with no footer/loss counters -- a missing row is indistinguishable from "
            "a frame that was never published (this is the asymmetry raised in schema review)")


def case_sampler_reset_without_clock():
    """A reset with no time cannot be placed relative to the decision it may have invalidated."""
    native = [_hdr("native", 100)]
    native += [_sampler_input(1, 1, 1, 21, disposition=5, n_after=1),
               _sampler_input(1, 2, 2, 22, disposition=5, n_after=2),
               _ev("sampler_reset", attempt=1, reason=2, sampler_generation=2, n_before=2, n_after=0),
               _sampler_input(1, 3, 3, 23, disposition=6, sampler_generation=2, n_after=1),
               _decision_fit(1, 1, n=1, span_us=0, sampler_generation=2),
               _foot(accepted=5, written=5)]
    return ({"native.jsonl": native}, UNKNOWN,
            "ruler reset discarded 2 samples; without a reset clock the consumer cannot say whether "
            "it preceded or followed the decision")


def case_hypothetical_weight_must_not_be_applied():
    native = [_hdr("native", 100)]
    native += [_decision_fit(1, 1, n=3, span_us=50_000, source=1, phase_primary=1,
                             sampler_authoritative=0),
               _decision_members(1, 1, hypothetical_weight=0.18),
               _foot(accepted=2, written=2)]
    return ({"native.jsonl": native}, COMPLETE,
            "phase_hypothetical_weight 0.18 is COUNTERFACTUAL. A consumer reporting a fused "
            "sampler contribution has repeated the exact error this schema field warns about")


def case_effective_n_below_raw_n():
    """Weighted fits have an effective n lower than their raw n -- report the effective one."""
    native = [_hdr("native", 100)]
    native += [_decision_fit(1, 1, n=8, span_us=120_000, weight_sum=3.9, weight_sq=2.6),
               _decision_members(1, 1), _foot(accepted=1, written=1)]
    return ({"native.jsonl": native}, COMPLETE,
            "raw n=8 but (sum w)^2/sum w^2 = 5.85 -- quoting 8 overstates the achievable precision")


def case_unknown_enum_value():
    native = [_hdr("native", 100)]
    native += [_sampler_input(1, 1, 1, 21, disposition=99, n_after=1),
               _decision_fit(1, 1, n=1, span_us=0, source=42),
               _foot(accepted=2, written=2)]
    return ({"native.jsonl": native}, UNKNOWN,
            "disposition 99 and source 42 are unmapped -- preserve verbatim, never guess, and do "
            "not count an unmapped disposition as accepted")


def case_detected_but_not_fed():
    """Availability and acceptance are different questions at a different layer."""
    native = [_hdr("native", 100)]
    for f in range(1, 7):
        native.append(_native_frame(f, detected=1, raw_fed=1 if f % 2 else 0))
    native += [_decision_fit(1, 1, n=3, span_us=50_000), _decision_members(1, 1),
               _foot(accepted=7, written=7)]
    return ({"native.jsonl": native}, COMPLETE,
            "6 frames detected, 3 fed to the sampler -- the gap is a call-site question, not loss")


def case_accepted_count_mismatch():
    native = [_hdr("native", 100)]
    for f in range(1, 4):
        native.append(_sampler_input(1, f, f, 20 + f, disposition=5, n_after=f))
    foot = _foot(accepted=9, written=3)
    foot["_keep_counts"] = True          # the mismatch IS the trap; the writer must not repair it
    native += [foot]
    return ({"native.jsonl": native}, UNKNOWN,
            "accepted 9 != written 3 != parsed 3 -- the journal did not record what it accepted")


CASES = [
    ("clean", case_clean),
    ("seed_replay_not_availability", case_seed_replay_not_availability),
    ("rejected_inputs_are_recorded", case_rejected_inputs_are_recorded),
    ("reused_decision_id", case_reused_decision_id),
    ("generation_mismatch", case_generation_mismatch),
    ("frame_underspecified_identity", case_frame_underspecified_identity),
    ("missing_footer", case_missing_footer),
    ("dropped_records", case_dropped_records),
    ("python_journal_no_loss_accounting", case_python_journal_no_loss_accounting),
    ("sampler_reset_without_clock", case_sampler_reset_without_clock),
    ("hypothetical_weight_must_not_be_applied", case_hypothetical_weight_must_not_be_applied),
    ("effective_n_below_raw_n", case_effective_n_below_raw_n),
    ("unknown_enum_value", case_unknown_enum_value),
    ("detected_but_not_fed", case_detected_but_not_fed),
    ("accepted_count_mismatch", case_accepted_count_mismatch),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write the fixture corpus here")
    ap.add_argument("--list", action="store_true", help="print the case table and exit")
    args = ap.parse_args(argv)

    if args.list or not args.out:
        print(f"{'case':<42}{'expects':<12}why")
        for name, fn in CASES:
            _, exp, why = fn()
            print(f"{name:<42}{exp:<12}{why}")
        print(f"\n{len(CASES)} cases. Expectations: COMPLETE / AMBIGUOUS / UNKNOWN / "
              "CONTRADICTS / REJECT_JOIN.")
        print("\nThe expectation is the test. A consumer that reports a confident finding where "
              "the\ndata cannot support one is wrong even when it does not crash.")
        return 0

    out = Path(args.out)
    manifest = []
    for name, fn in CASES:
        files, exp, why = fn()
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        for fname, records in files.items():
            # Contiguous event ids 1..N are part of the container completeness contract, so the
            # fixtures must satisfy it or every case reads as incomplete for the wrong reason.
            nid = 0
            for r in records:
                if r.get("type") == "event":
                    nid += 1
                    r.setdefault("id", str(nid))
                    r.setdefault("qpc", str(1_000_000 + nid * 16667))
            # accepted/written count EVENTS, not records. Counting the header too made the control
            # cases read as COUNT_MISMATCH -- caught by running the consumer against the corpus.
            for r in records:
                if r.get("type") == "footer":
                    if r.pop("_keep_counts", False):
                        continue          # this case's mismatch IS the trap -- do not repair it
                    if "accepted" in r:
                        r["accepted"] = str(nid)
                    if "written" in r:
                        r["written"] = str(nid)
            with open(d / fname, "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r) + "\n")
        manifest.append({"case": name, "expects": exp, "why": why,
                         "files": sorted(files)})
    with open(out / "EXPECTATIONS.json", "w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "cases": manifest}, fh, indent=2)
    print(f"wrote {len(CASES)} cases to {out}")
    print(f"expectations: {out / 'EXPECTATIONS.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
