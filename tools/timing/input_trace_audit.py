#!/usr/bin/env python
"""input_trace_audit.py -- read an Orion input trace v1 capture and report, per transaction, the
observed LOCAL stage chain and the EARLIEST missing or contradictory LOCAL observation.

    python tools/timing/input_trace_audit.py --dir <capture dir> [--seq N ...] [--all] [--csv out.csv]
        [--history-ok 0]   # once the history_send result enum is published

Schema: D:\\NexusVision\\diagnostics\\input-trace-20260920-154747\\TRACE_SCHEMA.md (v1), owned by the
input/transport lane. This is the ANALYSER side only: it never writes a trace, never runs in the
engine, and imposes no contract on the producer.

WHAT THIS TOOL WILL NOT DO (schema "Analyzer contract"):
  * It does not name a root cause, and a missing stage never implies an upstream-only cause -- not
    even when no drops were counted.
  * It never infers console receipt or game eligibility. console_observed is false everywhere.
  * COMPLETE means ONLY that the observed local contract was satisfied. It is not proof of a fresh
    button edge, of a shot, or of anything the console did: a STATE-only reassertion can complete
    without carrying a new Square event.
  * It never silently joins records across process instances, fork CONTEXTS or generations. A
    source_seq that repeats across them is AMBIGUOUS.
  * An absent field is UNKNOWN, never zero. Unknown enum values are preserved, never guessed.
  * An orphan terminal result is not counted as a joined shot.

HARD BOUNDARY: analysis tooling only; never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA_VERSION = 1

# Producer's intended transaction chain (transport lane, 2026-09-20).
CHAIN = [
    "native_send/submit",
    "fork_enqueue",
    "fork_dequeue",
    "history_send",          # only when a Square edge was actually owed -- see square_edge_owed()
    "local_complete",
    "native_send/terminal",
]
CHAIN_INDEX = {s: i for i, s in enumerate(CHAIN)}

# Context, never chain steps. native_policy is an on-change + ~1 Hz sample of pre-submit app-thread
# state: the precise-fire thread can release between snapshots and watchdog/ownership-release
# transactions carry no policy record, so requiring one would flag healthy traces. policy fields are
# never compared against a later mapped packet -- no same-decision identity exists.
SIDE_STAGES = {"route", "state_send", "native_policy"}

MISSING = object()

# Enum maps from the frozen sources. Scoped per stage: a field name alone is not an enum identity.
ENUMS = {
    ("native_send", "result"): {0: "Failed", 1: "Unchanged", 2: "Written", 3: "LocalUdpAccepted",
                                4: "OwnershipReleased", 5: "WrittenUnconfirmed"},
    ("native_policy", "engine_state"): {0: "Idle", 1: "Armed", 2: "Holding", 3: "GreenWindow",
                                        4: "Releasing", 5: "PumpFake", 6: "Cooldown"},
    ("native_policy", "up_audit_phase"): {0: "None", 1: "RawUp", 2: "DebouncedUp"},
    # Verified against OrionInputClient.h:49-54. NOTE Failed is 0x80, not 4.
    ("native_send", "ack_stage"): {1: "Enqueued", 2: "LocalUdpAccepted", 3: "OwnershipReleased",
                                   128: "Failed"},
    ("fork_dequeue", "kind"): {0: "STATE", 1: "OWNERSHIP_RELEASE"},
    ("fork_enqueue", "kind"): {0: "STATE", 1: "OWNERSHIP_RELEASE"},
    ("local_complete", "kind"): {0: "STATE", 1: "OWNERSHIP_RELEASE"},
    ("local_complete", "status"): {0: "NONE", 1: "UDP_ACCEPTED", 2: "OWNERSHIP_RELEASED"},
    # history_send / state_send result is a ChiakiErrorCode. CHIAKI_ERR_SUCCESS = 0.
    # IMPORTANT: 0 means the LOCAL callback returned success. It is not remote receipt, and nothing
    # downstream of this process is observed by it.
    ("history_send", "result"): {0: "CHIAKI_ERR_SUCCESS (local callback only)"},
    ("state_send", "result"): {0: "CHIAKI_ERR_SUCCESS (local callback only)"},
}
# Confirmed from the local formatter/send contract; other ChiakiErrorCode values are failures.
DEFAULT_HISTORY_OK = {0}
# Packet flag bits, verified with the enums above.
PACKET_FLAGS = {1: "MustDeliver", 2: "ShotRelease", 4: "RedundantFlick"}
ACK_FAILED = 128
# Terminal native results that represent a successful local completion.
NATIVE_OK_RESULTS = {2, 3, 4, 5}          # Written / LocalUdpAccepted / OwnershipReleased / Unconfirmed
NATIVE_FAIL_RESULTS = {0}                  # Failed
NATIVE_NOOP_RESULTS = {1}                  # Unchanged: dedup call, creates no send
# schema: "Fork status: 1=local UDP accepted, 2=ownership released"
FORK_OK_STATUS = {1, 2}
# ack_stage values that are NOT local completion. Enqueued only, numeric or legacy string.
ACK_ENQUEUED = {1, "ENQUEUED"}

BLOCKING_FLAGS = ("NO_HEADER", "NO_FOOTER", "DROPS_UNKNOWN", "SIZE_LIMIT", "NO_NATIVE_TRACE",
                  "NO_FORK_TRACE", "MIXED_QPC_DOMAIN", "MIXED_CAPTURE_IDS", "EVENTS_AFTER_FOOTER",
                  "DUPLICATE_FOOTER", "PENDING_AT_FOOTER", "UNTERMINATED_FOOTER", "BAD_QPC",
                  "ACCEPTED_COUNT_MISMATCH", "STATS_REPORTED_DROPS", "ID_ORDER_ANOMALY")
BLOCKING_PREFIXES = ("DROPS:", "MALFORMED_LINES:", "FOREIGN_SCHEMA:", "FOOTER_REASON:",
                     "MULTIPLE_", "BAD_QPC:", "ACCEPTED_COUNT_MISMATCH:", "PENDING_AT_FOOTER:",
                     "STATS_REPORTED_DROPS:")


def _get(data, key):
    if not isinstance(data, dict) or key not in data:
        return MISSING
    return data[key]


def _known(v):
    return v is not MISSING and v is not None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def strip_label(flag):
    head, sep, tail = flag.partition(":")
    return tail if sep and "/" in head else flag


def blocking_reasons(capture_flags):
    out = []
    for flag in capture_flags:
        raw = strip_label(flag)
        if raw in BLOCKING_FLAGS or raw.startswith(BLOCKING_PREFIXES):
            out.append(flag)
    return out


def enum_text(stage, field, value):
    if not _known(value):
        return "unknown"
    name = ENUMS.get((stage, field), {}).get(value)
    return f"{value}({name})" if name else f"{value}(unmapped)"


class Record:
    __slots__ = ("path", "line", "obj", "role", "instance", "pid")

    def __init__(self, path, line, obj):
        self.path, self.line, self.obj = path, line, obj
        self.role = self.instance = self.pid = None

    @property
    def ref(self):
        return f"{Path(self.path).name}:{self.line}"

    @property
    def raw_stage(self):
        return self.obj.get("stage")

    @property
    def stage(self):
        st = self.obj.get("stage")
        if st != "native_send":
            return st
        phase = _get(self.obj.get("data"), "phase")
        if phase == 0:
            return "native_send/submit"
        if phase == 1:
            return "native_send/terminal"
        return "native_send/unknown-phase"

    @property
    def context(self):
        return self.obj.get("context")

    @property
    def generation(self):
        return self.obj.get("generation")

    @property
    def qpc(self):
        return _int(self.obj.get("qpc"))

    @property
    def rec_id(self):
        return _int(self.obj.get("id"))

    # The join key. context and generation are NOT decoration: one file can carry several fork
    # contexts that legitimately reuse source_seq values.
    @property
    def lane(self):
        return (self.role, self.pid, self.instance, self.context, self.generation)

    def d(self, key):
        return _get(self.obj.get("data"), key)

    def di(self, key):
        return _int(self.d(key))


class ProcessTrace:
    def __init__(self, path):
        self.path = Path(path)
        self.header = None
        self.footer = None
        self.duplicate_footers = 0
        self.events_after_footer = 0
        self.stats = []
        self.events = []
        self.malformed = []
        self.foreign_schema = []
        self.bad_qpc = 0
        self.id_order_anomalies = 0

    role = property(lambda self: (self.header or {}).get("role"))
    pid = property(lambda self: (self.header or {}).get("pid"))
    instance = property(lambda self: (self.header or {}).get("instance"))
    capture_id = property(lambda self: (self.header or {}).get("capture_id"))
    qpc_hz = property(lambda self: (self.header or {}).get("qpc_hz"))

    @property
    def label(self):
        return f"{self.role}/pid{self.pid}/inst{self.instance}"

    def _counter_total(self, rec):
        """dropped_full + dropped_contention + dropped_clock (QPC sample failure, v1 additive).

        A missing counter yields None -> DROPS_UNKNOWN, never a silent zero.
        """
        total = 0
        for k in ("dropped_full", "dropped_contention", "dropped_clock"):
            if k not in rec:
                if k == "dropped_clock":
                    continue          # pre-amendment captures simply lack it
                return None
            v = _int(rec.get(k))
            if v is None:
                return None
            total += v
        return total

    @property
    def dropped(self):
        if self.footer:
            return self._counter_total(self.footer)
        if self.stats:
            return self._counter_total(self.stats[-1])
        return None

    def completeness_flags(self):
        f = []
        if self.header is None:
            f.append("NO_HEADER")
        if self.footer is None:
            f.append("NO_FOOTER")
        else:
            reason = self.footer.get("reason")
            if reason in ("running", None):
                # A "running" record is a stats snapshot, not an orderly end.
                f.append("UNTERMINATED_FOOTER")
            elif reason == "size_limit":
                f.append("SIZE_LIMIT")
            elif reason != "shutdown":
                f.append(f"FOOTER_REASON:{reason}")
            pending = _int(self.footer.get("pending"))
            if pending is None or pending > 0:
                f.append(f"PENDING_AT_FOOTER:{self.footer.get('pending')}")
            # v1 additive: a clean shutdown requires accepted == written == parsed event count.
            accepted = _int(self.footer.get("accepted"))
            written = _int(self.footer.get("written")) if "written" in self.footer else None
            parsed = len(self.events)
            if accepted is None:
                f.append("ACCEPTED_COUNT_MISMATCH:unknown")
            elif accepted != parsed or (written is not None and written != parsed):
                f.append(f"ACCEPTED_COUNT_MISMATCH:accepted={accepted},"
                         f"written={'?' if written is None else written},parsed={parsed}")
        if self.duplicate_footers:
            f.append("DUPLICATE_FOOTER")
        if self.events_after_footer:
            f.append("EVENTS_AFTER_FOOTER")
        # A clean footer cannot erase loss reported earlier in the run.
        stats_drops = max([self._counter_total(s) or 0 for s in self.stats], default=0)
        drops = self.dropped
        if drops is None:
            f.append("DROPS_UNKNOWN")
        elif drops > 0:
            f.append(f"DROPS:{drops}")
        elif stats_drops > 0:
            f.append(f"STATS_REPORTED_DROPS:{stats_drops}")
        if self.malformed:
            f.append(f"MALFORMED_LINES:{len(self.malformed)}")
        if self.foreign_schema:
            f.append(f"FOREIGN_SCHEMA:{len(self.foreign_schema)}")
        if self.bad_qpc:
            f.append(f"BAD_QPC:{self.bad_qpc}")
        if self.id_order_anomalies:
            f.append("ID_ORDER_ANOMALY")
        return f


def load_file(path):
    tr = ProcessTrace(path)
    seen_footer = False
    last_id = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                tr.malformed.append((n, str(exc)))
                continue
            if not isinstance(obj, dict):
                tr.malformed.append((n, "not an object"))
                continue
            kind = obj.get("type")
            if kind == "header":
                if obj.get("schema") != SCHEMA_VERSION:
                    tr.foreign_schema.append((n, obj.get("schema")))
                if tr.header is None:
                    tr.header = obj
                else:
                    tr.malformed.append((n, "second header in one file"))
            elif kind == "event":
                rec = Record(path, n, obj)
                if rec.qpc is None:
                    tr.bad_qpc += 1
                rid = rec.rec_id
                if rid is not None and last_id is not None and rid < last_id:
                    tr.id_order_anomalies += 1
                if rid is not None:
                    last_id = rid
                if seen_footer:
                    tr.events_after_footer += 1
                tr.events.append(rec)
            elif kind == "stats":
                tr.stats.append(obj)
            elif kind == "footer":
                if seen_footer:
                    tr.duplicate_footers += 1
                else:
                    tr.footer = obj
                seen_footer = True
            else:
                tr.malformed.append((n, f"unknown record type {kind!r}"))
    for rec in tr.events:
        rec.role, rec.instance, rec.pid = tr.role, tr.instance, tr.pid
    return tr


class Transaction:
    """Records sharing (source_seq, lane). A lane is role+pid+instance+context+generation."""

    def __init__(self, seq, lane):
        self.seq, self.lane = seq, lane
        self.by_stage = defaultdict(list)
        self.side = defaultdict(list)
        self.flags = []

    def add(self, rec):
        (self.side if rec.stage in SIDE_STAGES else self.by_stage)[rec.stage].append(rec)

    @property
    def records(self):
        out = []
        for bucket in (self.by_stage, self.side):
            for recs in bucket.values():
                out.extend(recs)
        return sorted(out, key=lambda r: (r.rec_id if r.rec_id is not None else 0, r.line))

    def first(self, stage):
        recs = self.by_stage.get(stage)
        return recs[0] if recs else None

    def primary(self, stage):
        """Records not marked redundant -- the actual attempts, not the replays."""
        return [r for r in self.by_stage.get(stage, []) if (r.di("redundant") or 0) == 0]

    # -- was a Square edge actually owed? ----------------------------------
    def square_edge_owed(self):
        """True only when a dequeue recorded a real prior->requested/applied Square DELTA on an
        ordinary (non-redundant-history) entry.

        needs_history=1 alone is NOT equivalent: a full-state transaction can owe history because
        Cross or a trigger changed, with Square untouched. Treating those as owing a Square event
        would flag perfectly valid non-Square traffic.
        Returns True / False / None(unknown).
        """
        examined = 0
        undecidable = False
        for rec in self.by_stage.get("fork_dequeue", []):
            if (rec.di("redundant_history") or 0) == 1:
                continue                      # a replay of saved history, not a fresh obligation
            prior = rec.di("prior_history_square")
            req = rec.di("requested_square")
            app = rec.di("applied_square")
            target = app if app is not None else req
            if prior is None or target is None:
                undecidable = True
                continue
            examined += 1
            if prior != target:
                return True
        # No ordinary dequeue was decidable -- either none existed, or every one was a
        # redundant-history replay, or the fields were absent. That is no evidence, not evidence
        # of absence.
        if examined == 0 or undecidable:
            return None
        return False

    def expects_history(self):
        """A history_send step is only REQUIRED when a Square edge was owed."""
        return self.square_edge_owed()

    def successful_history(self, history_ok):
        """The matching SUCCESSFUL attempt, not merely the first. Retries may eventually succeed.

        Returns (record|None, resolved: bool). resolved=False means the result enum is not known,
        so success cannot be decided and the transaction must stay UNKNOWN.
        """
        recs = self.by_stage.get("history_send", [])
        if not recs:
            return None, True
        if history_ok is None:
            return None, False
        for rec in recs:
            r = rec.di("result")
            if r is not None and r in history_ok:
                return rec, True
        return None, True

    def successful_local_complete(self):
        for rec in self.by_stage.get("local_complete", []):
            s = rec.di("status")
            if s is not None and s in FORK_OK_STATUS:
                return rec
        return None

    def successful_terminal(self):
        """A terminal native record whose result is a success AND which is neither Enqueued-only
        nor an explicitly Failed ack stage."""
        for rec in self.by_stage.get("native_send/terminal", []):
            ack = rec.di("ack_stage")
            if ack in ACK_ENQUEUED or rec.d("ack_stage") in ACK_ENQUEUED or ack == ACK_FAILED:
                continue
            r = rec.di("result")
            if r is not None and r in NATIVE_OK_RESULTS:
                return rec
        return None

    # -- contradictions, each tagged with the stage where it is first observable --
    def contradictions(self):
        out = []
        sub, enq = self.first("native_send/submit"), self.first("fork_enqueue")
        deq = self.first("fork_dequeue")

        if sub is not None and deq is not None:
            s_sq, a_sq = sub.di("square"), deq.di("applied_square")
            if s_sq is not None and a_sq is not None and s_sq != a_sq:
                out.append(("fork_dequeue",
                            f"native_send.square={s_sq} but fork_dequeue.applied_square={a_sq}"))
        if deq is not None:
            req, app = deq.di("requested_square"), deq.di("applied_square")
            if req is not None and app is not None and req != app:
                out.append(("fork_dequeue",
                            f"fork_dequeue.requested_square={req} but applied_square={app}"))
        if enq is not None and deq is not None:
            e_sq, d_req = enq.di("square"), deq.di("requested_square")
            if e_sq is not None and d_req is not None and e_sq != d_req:
                out.append(("fork_dequeue",
                            f"fork_enqueue.square={e_sq} but fork_dequeue.requested_square={d_req}"))

        if self.square_edge_owed():
            for hist in self.by_stage.get("history_send", []):
                newest, parsed = hist.di("newest_square"), hist.di("parse_complete")
                if newest is None or newest != -1:
                    continue
                if parsed == 1:
                    out.append(("history_send",
                                "a Square edge was owed (prior_history_square differs from the "
                                "applied Square) but this history attempt carries no Square event "
                                "(newest_square=-1, parse_complete=1)"))
                else:
                    out.append(("history_send",
                                "history_send.newest_square=-1 with parse_complete!=1 -- absence "
                                "NOT concludable"))

        for term in self.by_stage.get("native_send/terminal", []):
            ack_seq = term.di("ack_source_seq")
            if ack_seq is not None and self.seq is not None and ack_seq != self.seq:
                out.append(("native_send/terminal",
                            f"ack_source_seq={ack_seq} does not identify source_seq={self.seq}"))
            r = term.di("result")
            if r is not None and r in NATIVE_FAIL_RESULTS and self.successful_terminal() is None:
                out.append(("native_send/terminal",
                            f"terminal result={enum_text('native_send', 'result', r)} and no "
                            "successful attempt was recorded"))
        return out

    def order_anomaly(self):
        """Do the stages first appear in chain order? Reported as context, never a verdict.

        Only the FIRST PRIMARY record of each stage is considered: a redundant replay legitimately
        re-runs dequeue/history/complete, so including repeats would flag every healthy retry.
        """
        # Order MUST be judged on qpc, not on record id: id counters are PER PROCESS, so comparing
        # a native id against a fork id is meaningless and fired on every healthy paired capture.
        # qpc is a shared clock domain (same machine, verified by MIXED_QPC_DOMAIN).
        firsts = []
        for stage in CHAIN:
            prim = [r for r in self.primary(stage) if r.qpc is not None]
            if prim:
                firsts.append((CHAIN_INDEX[stage], min(r.qpc for r in prim)))
        firsts.sort(key=lambda x: x[1])
        idx = [i for i, _ in firsts]
        return "chain stages first appear out of chronological order" if idx != sorted(idx) else ""

    def expected_chain(self):
        exp = list(CHAIN)
        if self.expects_history() is False:
            exp.remove("history_send")
        return exp

    def verdict(self, capture_flags, history_ok):
        if self.flags:
            return "AMBIGUOUS", "; ".join(self.flags)

        contra = self.contradictions()
        if contra:
            earliest = min(contra, key=lambda c: CHAIN_INDEX.get(c[0], 99))
            return f"CONTRADICTS:{earliest[0]}", "; ".join(d for _, d in contra)

        blocking = blocking_reasons(capture_flags)
        exp = self.expected_chain()
        for stage in exp:
            present = self.by_stage.get(stage)
            if stage == "history_send" and self.expects_history() is None:
                return ("UNKNOWN:history_send",
                        "cannot tell whether a Square edge was owed: prior_history_square or the "
                        "applied Square was not recorded")
            if not present:
                if blocking:
                    return f"UNKNOWN:{stage}", "capture incomplete: " + ", ".join(blocking)
                return (f"STOPS_AT:{stage}",
                        "no local record of this stage in an otherwise complete capture")
            # Present is not enough: the step must have SUCCEEDED, via any attempt.
            if stage == "history_send":
                rec, resolved = self.successful_history(history_ok)
                if not resolved:
                    return ("UNKNOWN:history_send",
                            "history_send.result enum is not published, so a successful attempt "
                            "cannot be identified -- pass --history-ok once it is known")
                if rec is None:
                    return (f"CONTRADICTS:{stage}",
                            "every recorded history attempt failed; a failed attempt cannot satisfy "
                            "a successful local send")
            elif stage == "local_complete" and self.successful_local_complete() is None:
                if blocking:
                    return f"UNKNOWN:{stage}", "capture incomplete: " + ", ".join(blocking)
                return f"CONTRADICTS:{stage}", "no local_complete with a success status was recorded"
            elif stage == "native_send/terminal" and self.successful_terminal() is None:
                if blocking:
                    return f"UNKNOWN:{stage}", "capture incomplete: " + ", ".join(blocking)
                return (f"CONTRADICTS:{stage}",
                        "no terminal record shows a successful local result (an Enqueued-only ack "
                        "is not local completion)")
        return "COMPLETE", ""

    def chain_text(self):
        parts = []
        for stage in CHAIN:
            recs = self.by_stage.get(stage)
            short = stage.split("/")[-1]
            parts.append(f"-{short}" if not recs else
                         (f"{short}x{len(recs)}" if len(recs) > 1 else short))
        return " > ".join(parts)

    def context_note(self):
        bits = [f"lane ctx={self.lane[3]} gen={self.lane[4]}"]
        owed = self.square_edge_owed()
        bits.append("square_edge_owed=" + {True: "yes", False: "no", None: "unknown"}[owed])
        term = self.first("native_send/terminal")
        if term is not None:
            bits.append("result=" + enum_text("native_send", "result", term.di("result")))
        for rec in self.side.get("native_policy", []):
            st = rec.di("engine_state")
            if st is not None:
                bits.append("policy.engine_state=" + enum_text("native_policy", "engine_state", st))
            break
        if self.side.get("state_send"):
            bits.append(f"state_send x{len(self.side['state_send'])} (carries no digital Square)")
        anomaly = self.order_anomaly()
        if anomaly:
            bits.append(anomaly)
        return "; ".join(bits)

    def redundancy_note(self):
        notes = []
        for stage in ("fork_enqueue", "fork_dequeue", "local_complete", "history_send"):
            prim = len(self.primary(stage))
            total = len(self.by_stage.get(stage, []))
            if total > prim:
                notes.append(f"{stage}: {prim} primary + {total - prim} redundant")
            elif total > 1:
                notes.append(f"{stage}: {total} attempts")
        return "; ".join(notes)


class Capture:
    def __init__(self, traces):
        self.traces = traces
        self.transactions = {}
        self.orphans = []

    @property
    def capture_ids(self):
        return sorted({t.capture_id for t in self.traces if t.capture_id})

    def flags(self):
        flags = []
        for t in self.traces:
            flags.extend(f"{t.label}:{f}" for f in t.completeness_flags())
        if len(self.capture_ids) > 1:
            flags.append("MIXED_CAPTURE_IDS")
        roles = Counter(t.role for t in self.traces)
        for role in ("native", "fork"):
            if roles.get(role, 0) > 1:
                flags.append(f"MULTIPLE_{role.upper()}_LIFETIMES:{roles[role]}")
            elif roles.get(role, 0) == 0:
                flags.append(f"NO_{role.upper()}_TRACE")
        if len({t.qpc_hz for t in self.traces if t.header}) > 1:
            flags.append("MIXED_QPC_DOMAIN")
        return flags

    def build(self):
        """Join by source_seq, but ONLY when the pairing is unambiguous.

        A lane is scoped within a role -- (role, pid, instance, context, generation). A healthy
        transaction is at most one native lane paired with at most one fork lane. If either side
        contributes MORE than one lane for the same source_seq, the identifier was reused (the real
        producer replay does exactly this across fork contexts 1/2/3) and the lanes are kept apart
        and marked AMBIGUOUS rather than silently combined.
        """
        by_seq = defaultdict(lambda: defaultdict(list))
        for t in self.traces:
            for rec in t.events:
                seq = rec.obj.get("source_seq")
                if seq is None:
                    self.orphans.append(rec)
                    continue
                by_seq[seq][rec.lane].append(rec)

        for seq, lanes in by_seq.items():
            per_role = defaultdict(list)
            for lane in lanes:
                per_role[lane[0]].append(lane)
            ambiguous = any(len(v) > 1 for v in per_role.values())

            if not ambiguous:
                # One lane per role at most: a single transaction spanning the processes.
                fork_lane = next((l for l in lanes if l[0] == "fork"), None)
                display = fork_lane or next(iter(lanes))
                txn = Transaction(seq, display)
                for lane, recs in lanes.items():
                    for rec in recs:
                        txn.add(rec)
                self.transactions[(seq, None)] = txn
            else:
                detail = "; ".join(
                    f"{role}: contexts " + ",".join(sorted({str(l[3]) for l in ls}))
                    + " generations " + ",".join(sorted({str(l[4]) for l in ls}))
                    for role, ls in sorted(per_role.items()) if len(ls) > 1)
                for lane, recs in lanes.items():
                    txn = Transaction(seq, lane)
                    for rec in recs:
                        txn.add(rec)
                    txn.flags.append(
                        f"SEQ_REUSE: source_seq {seq} appears in {len(lanes)} lanes "
                        f"({detail}) -- not combined")
                    self.transactions[(seq, lane)] = txn

        # Several PRIMARY completions inside ONE transaction is reuse too, regardless of any
        # redundant records also present. The old check bailed out whenever a redundant existed.
        for txn in self.transactions.values():
            for stage in ("local_complete", "native_send/terminal"):
                n = len(txn.primary(stage))
                if n > 1:
                    txn.flags.append(
                        f"MULTIPLE_PRIMARY_{stage.split('/')[-1].upper()}:{n}")
        return self


def in_window(rec, since, until):
    q = rec.qpc
    if q is None:
        return True
    if since is not None and q < since:
        return False
    if until is not None and q > until:
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir")
    ap.add_argument("--file", nargs="*", default=[])
    ap.add_argument("--capture-id")
    ap.add_argument("--seq", type=int, nargs="*")
    ap.add_argument("--since-qpc", type=int)
    ap.add_argument("--until-qpc", type=int)
    ap.add_argument("--history-ok", type=int, nargs="*", default=None,
                    help="history_send.result value(s) meaning SUCCESS. Until the enum artifact is "
                         "published this is unknown, and those transactions stay UNKNOWN.")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--csv")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.file]
    if args.dir:
        paths.extend(sorted(Path(args.dir).rglob("*.jsonl")))
    if not paths:
        ap.error("no trace files: pass --dir or --file")

    traces = []
    for p in paths:
        tr = load_file(p)
        if args.capture_id and tr.capture_id != args.capture_id:
            continue
        traces.append(tr)
    if not traces:
        print("no trace files matched", file=sys.stderr)
        return 2
    for tr in traces:
        tr.events = [r for r in tr.events if in_window(r, args.since_qpc, args.until_qpc)]

    cap = Capture(traces).build()
    cap_flags = cap.flags()
    history_ok = set(args.history_ok) if args.history_ok is not None else None

    print("== capture ==")
    for t in traces:
        print(f"  {t.path.name}: {t.label or '(no header)'} events={len(t.events)} "
              f"flags={','.join(t.completeness_flags()) or 'none'}")
    print(f"capture_id   : {','.join(cap.capture_ids) or '(none)'}")
    print(f"flags        : {', '.join(cap_flags) or 'none'}")
    print(f"blocking     : {', '.join(blocking_reasons(cap_flags)) or 'none'}")
    print("console      : never observed (schema: console_observed=false in every record)")
    if history_ok is None:
        print("history_ok   : UNSET -- history_send success cannot be decided; those transactions "
              "stay UNKNOWN. Pass --history-ok once the enum is published.")
    if cap.orphans:
        print(f"orphans      : {len(cap.orphans)} record(s) with source_seq=null -- not joined")

    rows = []
    for (seq, lane) in sorted(cap.transactions, key=lambda k: (k[0], str(k[1]))):
        if args.seq and seq not in args.seq:
            continue
        txn = cap.transactions[(seq, lane)]
        verdict, detail = txn.verdict(cap_flags, history_ok)
        # The KEY's lane is None for an unambiguously joined transaction (one native lane paired
        # with one fork lane). The display lane lives on the transaction itself, always populated.
        dl = txn.lane
        rows.append({"source_seq": seq, "lane_ctx": dl[3], "lane_gen": dl[4],
                     "verdict": verdict, "detail": detail, "chain": txn.chain_text(),
                     "context": txn.context_note(), "redundancy": txn.redundancy_note(),
                     "flags": "; ".join(txn.flags),
                     "records": " ".join(r.ref for r in txn.records)})

    shown = rows if args.all else [r for r in rows if r["verdict"] != "COMPLETE"]
    print(f"\n== transactions ({len(shown)} shown of {len(rows)}) ==")
    for r in shown:
        print(f"\nsource_seq {r['source_seq']} (ctx {r['lane_ctx']})  [{r['verdict']}]")
        print(f"  chain      : {r['chain']}")
        for k in ("detail", "context", "redundancy", "flags"):
            if r[k]:
                print(f"  {k:<11}: {r[k]}")
        print(f"  records    : {r['records']}")

    counts = Counter(r["verdict"].split(":")[0] for r in rows)
    print("\n== summary ==")
    for k in sorted(counts):
        print(f"  {k:<12} {counts[k]}")
    print("\nCOMPLETE means only that the observed LOCAL contract was satisfied -- not proof of a "
          "fresh button edge, of a shot, or of anything the console did.")
    print("This report names the EARLIEST missing or contradictory LOCAL observation and nothing "
          "further. A missing stage never implies an upstream-only cause, not even with no drops.")

    if args.csv:
        cols = list(rows[0].keys()) if rows else ["source_seq", "verdict"]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
