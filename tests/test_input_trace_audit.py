"""Adversarial fixtures for tools/timing/input_trace_audit.py (input trace v1 analyser).

These exist to prove the analyser refuses to over-read a capture, in BOTH directions. The two
failures that matter are (a) confidently reporting "the press stopped here" when the capture merely
lost the record, reused an identifier, or never wrote the field, and (b) flagging a perfectly
healthy trace as broken. Both have happened; each has fixtures below.

Covered: drops, missing/duplicate/unterminated footer, events after footer, accepted/written
accounting, sequence reuse across fork contexts, multiple primary completions, ambiguous
cross-process joins, retries and redundant events, non-Square history obligations, numeric ack
stages, failed-then-successful history attempts, earliest-contradiction ordering, malformed QPC,
absent-field-is-not-zero, state_send never satisfying the Square step, and orphan records.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.timing.input_trace_audit import (  # noqa: E402
    Capture, blocking_reasons, enum_text, load_file,
)

QPC_HZ = 10000000
HISTORY_OK = {0}          # stand-in for the real enum; the tool defaults to "unknown"


def _header(role, pid, instance, capture_id="cap-1", qpc_hz=QPC_HZ):
    return {"type": "header", "schema": 1, "role": role, "capture_id": capture_id, "pid": pid,
            "instance": str(instance), "qpc_hz": qpc_hz, "sync_qpc": "1000", "sync_unix_us": "1",
            "capacity": 4096, "console_observed": False}


def _event(eid, qpc, stage, seq, data, context="1", generation=None):
    return {"type": "event", "id": str(eid), "qpc": str(qpc), "stage": stage, "context": context,
            "generation": generation, "epoch": None, "source_seq": seq, "data": data,
            "dropped_total": "0", "console_observed": False}


def _footer(reason="shutdown", dropped_full=0, dropped_contention=0, dropped_clock=0,
            accepted=None, written=None, pending=0):
    f = {"type": "footer", "qpc": "9999", "dropped_full": str(dropped_full),
         "dropped_contention": str(dropped_contention), "dropped_clock": str(dropped_clock),
         "pending": pending, "reason": reason, "console_observed": False}
    if accepted is not None:
        f["accepted"] = str(accepted)
    if written is not None:
        f["written"] = str(written)
    return f


def _write(path, records, raw_tail=None):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        if raw_tail is not None:
            fh.write(raw_tail)
    return path


def _native_events(seq=7, ack_stage=None, result=3, ack_seq=None):
    term = {"phase": 1, "result": result, "ack_source_seq": seq if ack_seq is None else ack_seq}
    if ack_stage is not None:
        term["ack_stage"] = ack_stage
    return [
        _event(1, 100, "native_policy", seq, {"raw_square": 1, "desired_square": 1, "engine_state": 2}),
        _event(2, 110, "native_send", seq, {"phase": 0, "square": 1, "own": 1}),
        _event(9, 200, "native_send", seq, term),
    ]


def _fork_events(seq=7, prior_history_square=0, requested=1, applied=1, needs_history=1,
                 newest_square=1, history_result=0, status=1, context="1", parse_complete=1):
    ev = [
        _event(3, 120, "fork_enqueue", seq, {"square": requested, "queue_depth": 1,
                                             "redundant": 0, "redundant_history": 0}, context),
        _event(4, 130, "fork_dequeue", seq,
               {"prior_square": 0, "prior_history_square": prior_history_square,
                "requested_square": requested, "applied_square": applied, "needs_state": 1,
                "needs_history": needs_history, "redundant": 0, "redundant_history": 0}, context),
    ]
    if needs_history:
        ev.append(_event(5, 140, "history_send", seq,
                         {"feedback_seq": 33, "result": history_result, "parse_complete": parse_complete,
                          "square_down_count": 1, "square_up_count": 0,
                          "newest_square": newest_square}, context))
    ev.append(_event(6, 150, "local_complete", seq,
                     {"status": status, "applied_square": applied, "redundant": 0,
                      "redundant_history": 0}, context))
    return ev


def _mk(tmp, role, events, footer="auto", pid=None, instance=1, name=None, capture_id="cap-1",
        qpc_hz=QPC_HZ):
    pid = pid or (100 if role == "native" else 200)
    name = name or f"{role}.jsonl"
    if footer == "auto":
        footer = _footer(accepted=len(events), written=len(events))
    recs = [_header(role, pid, instance, capture_id, qpc_hz)] + events + ([footer] if footer else [])
    return _write(tmp / name, recs)


def _audit(*paths):
    return Capture([load_file(p) for p in paths]).build()


def _txn(cap, seq, context=None):
    hits = [t for (s, lane), t in cap.transactions.items()
            if s == seq and (context is None or lane[3] == context)]
    assert hits, f"no transaction for seq {seq}"
    return hits[0]


def _verdict(cap, seq, history_ok=HISTORY_OK, context=None):
    return _txn(cap, seq, context).verdict(cap.flags(), history_ok)


# -- baseline -------------------------------------------------------------------------------

def test_complete_chain_reports_complete(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", _fork_events()))
    assert _verdict(cap, 7)[0] == "COMPLETE"


def test_complete_is_unknown_until_the_history_enum_is_published(tmp_path):
    """Until history_send success can be decided, COMPLETE must not be claimed."""
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", _fork_events()))
    verdict, detail = _verdict(cap, 7, history_ok=None)
    assert verdict == "UNKNOWN:history_send"
    assert "--history-ok" in detail


# -- GAP 1: needs_history is not "a Square edge is owed" -------------------------------------

def test_history_owed_only_when_a_square_delta_was_recorded(tmp_path):
    """A full-state transaction can owe history because Cross or a trigger changed, with Square
    untouched. Requiring a Square event there would flag valid non-Square traffic."""
    fork = _fork_events(prior_history_square=1, requested=1, applied=1, newest_square=-1)
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    txn = _txn(cap, 7)
    assert txn.square_edge_owed() is False
    verdict, detail = _verdict(cap, 7)
    assert verdict == "COMPLETE", detail          # newest_square=-1 is NOT a contradiction here


def test_history_without_square_event_is_a_contradiction_only_when_a_delta_existed(tmp_path):
    fork = _fork_events(prior_history_square=0, requested=1, applied=1, newest_square=-1)
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _txn(cap, 7).square_edge_owed() is True
    verdict, detail = _verdict(cap, 7)
    assert verdict == "CONTRADICTS:history_send"
    assert "a Square edge was owed" in detail


def test_redundant_history_entry_does_not_create_an_obligation(tmp_path):
    """A redundant_history entry replays saved history; it is not a fresh obligation."""
    fork = _fork_events(prior_history_square=1, requested=1, applied=1)
    fork[1]["data"]["redundant_history"] = 1
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _txn(cap, 7).square_edge_owed() is None   # nothing decidable was recorded


def test_missing_prior_history_square_is_unknown_not_false(tmp_path):
    fork = _fork_events()
    del fork[1]["data"]["prior_history_square"]
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "UNKNOWN:history_send"
    assert "prior_history_square" in detail


# -- GAP 2: success must be established, and retries may succeed later -----------------------

def test_numeric_enqueued_ack_is_not_local_completion(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events(ack_stage=1)),
                 _mk(tmp_path, "fork", _fork_events()))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "CONTRADICTS:native_send/terminal"
    assert "Enqueued-only" in detail


def test_legacy_string_enqueued_ack_still_recognised(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events(ack_stage="ENQUEUED")),
                 _mk(tmp_path, "fork", _fork_events()))
    assert _verdict(cap, 7)[0] == "CONTRADICTS:native_send/terminal"


def test_failed_terminal_result_is_not_completion(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events(result=0)),
                 _mk(tmp_path, "fork", _fork_events()))
    assert _verdict(cap, 7)[0].startswith("CONTRADICTS:")


def test_unchanged_result_is_a_dedup_call_not_a_successful_send(tmp_path):
    """InputRouteWriteResult.Unchanged=1 creates no send, so it cannot complete the chain."""
    cap = _audit(_mk(tmp_path, "native", _native_events(result=1)),
                 _mk(tmp_path, "fork", _fork_events()))
    assert _verdict(cap, 7)[0] == "CONTRADICTS:native_send/terminal"


def test_a_retry_that_eventually_succeeds_completes(tmp_path):
    """Inspect the matching SUCCESSFUL attempt, not merely the first one."""
    fork = _fork_events(history_result=9)                      # first attempt fails
    fork.insert(3, _event(51, 145, "history_send", 7,
                          {"feedback_seq": 34, "result": 0, "parse_complete": 1,
                           "newest_square": 1}))                # retry succeeds
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _verdict(cap, 7)[0] == "COMPLETE"


def test_every_history_attempt_failing_is_a_contradiction(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", _fork_events(history_result=9)))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "CONTRADICTS:history_send"
    assert "every recorded history attempt failed" in detail


def test_local_complete_without_a_success_status_is_a_contradiction(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", _fork_events(status=7)))
    assert _verdict(cap, 7)[0] == "CONTRADICTS:local_complete"


# -- GAP 3: reuse across contexts, and multiple primary completions --------------------------

def test_source_seq_reused_across_fork_contexts_stays_ambiguous(tmp_path):
    """Seen in the real producer replay: one file, contexts 1/2/3, seq reused in each."""
    fork = _fork_events(context="1") + _fork_events(context="2") + _fork_events(context="3")
    for i, e in enumerate(fork):
        e["id"] = str(100 + i)
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    lanes = [lane for (s, lane) in cap.transactions if s == 7]
    assert len({l[3] for l in lanes}) == 3
    for ctx in ("1", "2", "3"):
        verdict, detail = _verdict(cap, 7, context=ctx)
        assert verdict == "AMBIGUOUS"
        assert "SEQ_REUSE" in detail


def test_multiple_primary_completions_are_caught_even_when_a_redundant_exists(tmp_path):
    """The old check bailed out if ANY redundant record was present, hiding real reuse."""
    fork = _fork_events()
    fork.append(_event(60, 160, "local_complete", 7,
                       {"status": 1, "redundant": 1, "redundant_history": 1}))
    fork.append(_event(61, 170, "local_complete", 7, {"status": 1, "redundant": 0}))
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "AMBIGUOUS"
    assert "MULTIPLE_PRIMARY_LOCAL_COMPLETE" in detail


def test_redundant_repeats_alone_do_not_break_a_chain(tmp_path):
    fork = _fork_events()
    fork.append(_event(60, 160, "local_complete", 7, {"status": 1, "redundant": 1}))
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _verdict(cap, 7)[0] == "COMPLETE"
    assert "redundant" in _txn(cap, 7).redundancy_note()


def test_two_fork_lifetimes_are_blocking(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", _fork_events(), pid=200, instance=2, name="fork_a.jsonl"),
                 _mk(tmp_path, "fork", _fork_events(), pid=201, instance=3, name="fork_b.jsonl"))
    assert any("MULTIPLE_FORK_LIFETIMES" in f for f in cap.flags())
    assert blocking_reasons(cap.flags())


# -- GAP 4: cross-domain joins are blocking --------------------------------------------------

def test_mixed_qpc_domain_is_blocking(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", _fork_events(), qpc_hz=3000000))
    assert "MIXED_QPC_DOMAIN" in cap.flags()
    assert any("MIXED_QPC_DOMAIN" in f for f in blocking_reasons(cap.flags()))


def test_mixed_capture_ids_are_blocking(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", _fork_events(), capture_id="other"))
    assert "MIXED_CAPTURE_IDS" in blocking_reasons(cap.flags())


def test_malformed_qpc_is_flagged_and_blocking(tmp_path):
    fork = _fork_events()
    fork[0]["qpc"] = "not-a-number"
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert any("BAD_QPC" in f for f in blocking_reasons(cap.flags()))


# -- GAP 5: footer validity ------------------------------------------------------------------

def test_running_footer_is_not_an_orderly_end(tmp_path):
    ev = _fork_events()
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", ev, footer=_footer(reason="running", accepted=len(ev),
                                                          written=len(ev))))
    assert any("UNTERMINATED_FOOTER" in f for f in blocking_reasons(cap.flags()))


def test_pending_backlog_at_footer_is_blocking(tmp_path):
    ev = _fork_events()
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", ev, footer=_footer(accepted=len(ev), written=len(ev),
                                                          pending=4)))
    assert any("PENDING_AT_FOOTER" in f for f in blocking_reasons(cap.flags()))


def test_accepted_written_mismatch_is_blocking(tmp_path):
    ev = _fork_events()
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", ev, footer=_footer(accepted=len(ev), written=len(ev) - 1)))
    assert any("ACCEPTED_COUNT_MISMATCH" in f for f in blocking_reasons(cap.flags()))


def test_duplicate_footer_is_flagged(tmp_path):
    ev = _fork_events()
    recs = [_header("fork", 200, 1)] + ev + [_footer(accepted=len(ev), written=len(ev)),
                                             _footer(accepted=len(ev), written=len(ev))]
    path = _write(tmp_path / "fork.jsonl", recs)
    cap = _audit(_mk(tmp_path, "native", _native_events()), path)
    assert any("DUPLICATE_FOOTER" in f for f in blocking_reasons(cap.flags()))


def test_events_after_footer_are_flagged(tmp_path):
    ev = _fork_events()
    recs = ([_header("fork", 200, 1)] + ev + [_footer(accepted=len(ev), written=len(ev))]
            + [_event(99, 999, "local_complete", 7, {"status": 1})])
    path = _write(tmp_path / "fork.jsonl", recs)
    cap = _audit(_mk(tmp_path, "native", _native_events()), path)
    assert any("EVENTS_AFTER_FOOTER" in f for f in blocking_reasons(cap.flags()))


def test_a_clean_footer_cannot_erase_earlier_reported_loss(tmp_path):
    """Drops reported in a mid-run stats record still count even if the footer ends at zero."""
    ev = _fork_events()
    recs = ([_header("fork", 200, 1)] + ev
            + [{"type": "stats", "qpc": "500", "accepted": "3", "dropped_full": "5",
                "dropped_contention": "0", "dropped_clock": "0", "pending": 0, "reason": "running",
                "console_observed": False}]
            + [_footer(accepted=len(ev), written=len(ev))])
    path = _write(tmp_path / "fork.jsonl", recs)
    cap = _audit(_mk(tmp_path, "native", _native_events()), path)
    assert any("STATS_REPORTED_DROPS" in f for f in blocking_reasons(cap.flags()))


def test_dropped_clock_counts_toward_loss(tmp_path):
    ev = _fork_events()
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", ev, footer=_footer(dropped_clock=2, accepted=len(ev),
                                                          written=len(ev))))
    assert any("DROPS:2" in f for f in blocking_reasons(cap.flags()))


# -- drops / no footer turn findings into UNKNOWN --------------------------------------------

def test_missing_stage_in_clean_capture_stops_at_that_stage(tmp_path):
    fork = [e for e in _fork_events() if e["stage"] != "history_send"]
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _verdict(cap, 7)[0] == "STOPS_AT:history_send"


def test_same_gap_with_drops_is_unknown_not_a_finding(tmp_path):
    fork = [e for e in _fork_events() if e["stage"] != "history_send"]
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", fork,
                     footer=_footer(dropped_full=3, accepted=len(fork), written=len(fork))))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "UNKNOWN:history_send"
    assert "DROPS:3" in detail


def test_same_gap_with_no_footer_is_unknown(tmp_path):
    fork = [e for e in _fork_events() if e["stage"] != "history_send"]
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork, footer=None))
    assert _verdict(cap, 7)[0] == "UNKNOWN:history_send"


def test_size_limit_footer_is_treated_as_incomplete(tmp_path):
    fork = [e for e in _fork_events() if e["stage"] != "history_send"]
    cap = _audit(_mk(tmp_path, "native", _native_events()),
                 _mk(tmp_path, "fork", fork,
                     footer=_footer(reason="size_limit", accepted=len(fork), written=len(fork))))
    assert _verdict(cap, 7)[0] == "UNKNOWN:history_send"


# -- GAP 6: earliest contradiction, not the last seen stage ----------------------------------

def test_contradicts_names_the_earliest_conflicting_stage(tmp_path):
    """A dequeue conflict and a terminal conflict together must report the DEQUEUE."""
    fork = _fork_events(applied=0)                       # conflicts at fork_dequeue
    cap = _audit(_mk(tmp_path, "native", _native_events(ack_seq=99)),  # also conflicts at terminal
                 _mk(tmp_path, "fork", fork))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "CONTRADICTS:fork_dequeue"
    assert "ack_source_seq" in detail                    # the later conflict is still reported


# -- contract invariants ---------------------------------------------------------------------

def test_state_send_does_not_substitute_for_history_send(tmp_path):
    fork = [e for e in _fork_events() if e["stage"] != "history_send"]
    fork.insert(2, _event(5, 140, "state_send", 7, {"feedback_seq": 33, "result": 0}))
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert _verdict(cap, 7)[0] == "STOPS_AT:history_send"


def test_transaction_without_a_policy_record_is_still_complete(tmp_path):
    nat = [e for e in _native_events() if e["stage"] != "native_policy"]
    cap = _audit(_mk(tmp_path, "native", nat), _mk(tmp_path, "fork", _fork_events()))
    assert _verdict(cap, 7)[0] == "COMPLETE"


def test_policy_desired_square_is_never_compared_to_the_mapped_packet(tmp_path):
    nat = _native_events()
    nat[0]["data"]["desired_square"] = 0
    cap = _audit(_mk(tmp_path, "native", nat), _mk(tmp_path, "fork", _fork_events()))
    verdict, detail = _verdict(cap, 7)
    assert verdict == "COMPLETE"
    assert "desired_square" not in detail


def test_orphan_records_are_not_counted_as_shots(tmp_path):
    fork = _fork_events()
    fork.append(_event(70, 170, "local_complete", None, {"status": 1}))
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", fork))
    assert len(cap.orphans) == 1
    assert {s for (s, _) in cap.transactions} == {7}


def test_truncated_tail_is_flagged_not_fatal(tmp_path):
    recs = [_header("fork", 200, 1)] + _fork_events()
    path = _write(tmp_path / "fork.jsonl", recs, raw_tail='{"type":"event","id":"8')
    tr = load_file(path)
    assert tr.malformed
    flags = tr.completeness_flags()
    assert any("MALFORMED_LINES" in f for f in flags) and "NO_FOOTER" in flags


def test_unknown_enum_values_are_preserved_verbatim():
    assert enum_text("native_send", "result", 3) == "3(LocalUdpAccepted)"
    assert enum_text("native_send", "result", 4242) == "4242(unmapped)"
    assert enum_text("native_policy", "engine_state", 4) == "4(Releasing)"
    assert enum_text("native_send", "engine_state", 4) == "4(unmapped)"   # enums are per-stage
    assert enum_text("native_send", "result", None) == "unknown"


# -- regressions found by a REAL two-process paired capture, not by these fixtures ------------

def test_unambiguous_join_survives_the_display_path(tmp_path, capsys):
    """An unambiguously joined transaction is keyed (seq, None); the display lane lives on the
    transaction. Reading lane[3] off the KEY crashed the CLI on every real paired capture while
    every unit test passed, because the units never exercised main()."""
    import tools.timing.input_trace_audit as mod
    n = _mk(tmp_path, "native", _native_events())
    f = _mk(tmp_path, "fork", _fork_events())
    rc = mod.main(["--file", str(n), str(f), "--history-ok", "0", "--all",
                   "--csv", str(tmp_path / "out.csv")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMPLETE" in out
    assert (tmp_path / "out.csv").exists()


def test_order_check_uses_qpc_not_per_process_record_ids(tmp_path):
    """Record ids are PER PROCESS. Comparing a native id against a fork id is meaningless and
    flagged every healthy paired capture as out of order. qpc is the shared clock domain."""
    nat = _native_events()
    fork = _fork_events()
    for e in fork:                      # fork ids far below native's, but qpc order is correct
        e["id"] = str(int(e["id"]) - 100 if int(e["id"]) > 100 else int(e["id"]))
    for e in nat:
        e["id"] = str(int(e["id"]) + 9000)
    cap = _audit(_mk(tmp_path, "native", nat), _mk(tmp_path, "fork", fork))
    txn = _txn(cap, 7)
    assert txn.order_anomaly() == "", txn.order_anomaly()
    assert _verdict(cap, 7)[0] == "COMPLETE"


def test_every_row_keeps_a_raw_record_reference(tmp_path):
    cap = _audit(_mk(tmp_path, "native", _native_events()), _mk(tmp_path, "fork", _fork_events()))
    refs = [r.ref for r in _txn(cap, 7).records]
    assert refs and all(":" in ref for ref in refs)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
