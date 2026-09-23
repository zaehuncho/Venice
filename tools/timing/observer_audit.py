#!/usr/bin/env python
"""observer_audit.py -- consumer for the Orion timing-observer journals.

    python tools/timing/observer_audit.py --dir <capture dir> [--decision N ...] [--all] [--csv out.csv]

Contract: D:\\NexusVision\\diagnostics\\timing-observer-20260920-190810\\TIMING_OBSERVER_SCHEMA.json
Fixtures: tools/timing/observer_fixtures.py (15 adversarial cases + EXPECTATIONS.json)

THE QUESTION THIS ANSWERS (workstream A): for each canonical decision, what support did the sampler
ACTUALLY have -- accepted samples, their span, and the effective n implied by the weights -- and was
the decision revisable afterwards. Everything else is refused.

WHAT IT WILL NOT DO, each one because the schema forbids it or because a previous consumer got it
wrong on real data:
  * It will not treat `phase_hypothetical_weight` or `registration_hypothetical_weight` as applied.
    Both are telemetry-only candidate weights; neither is evidence the final source fused anything.
  * It will not count `feed=2` as an available frame. That is ownership-seed history replay.
  * It will not equate raw `n` with usable support: n is deque support, and with `wrss = -1` or zero
    weight sums there is NO COMPUTED FIT at all. Effective n = (Sum w)^2 / Sum w^2 is defined only
    for computed nonzero weights, and is a weighting measure, NOT a count of independent samples.
  * It will not join by token, frame, epoch or source_seq alone, and it will not resolve a reused
    id by last-wins or by nearest timestamp. Reused ids with contradictory payload are AMBIGUOUS.
  * It will not infer a missing stage as absence in a lossy or incomplete trace.
  * It will not infer candidate admissibility from a capture-time ETA, and no record here implies
    console or game receipt.
  * `native_frame.raw_fed` is upstream reader feed metadata, NOT native sampler acceptance.
    `detected` is the EVALUATED native sidecarResult.detected AFTER freshness/meter/fill/confidence
    gates -- not a raw payload flag -- and the stage is post-native-dedupe and pre-signal, so it is
    not every delivered telemetry row. Both answer availability, not support.

POST-ARM REVISABILITY, as far as the records actually support it (worker ledger):
  * A per-token ledger joins worker rows on the FULL route scope -- (process identity, engine
    context, token, route generation). A zero/absent context, a zero/absent token or an unknown
    route generation means NO JOIN: the row is held in `unjoinable_worker` and is never dropped
    into the nearest bucket. There is no nearest-time attribution anywhere in this consumer.
  * What the ledger reports is what the worker RETURNED: the arm result, each retarget result, the
    remaining time at claim, and whether dispatch was confirmed LOCALLY. A deadline is read as
    accepted only on result enum 0; on any refusal the deadline field is the REQUESTED value.
  * A worker `retarget_result` carries no proposal_id, so it is attributed to an engine proposal
    only when exactly ONE engine `retarget` row is eligible in the same route scope. More than one
    means UNATTRIBUTED. Zero means UNATTRIBUTED too, because the engine records that stage only for
    proposals reaching the final identity/deadline guard -- earlier returns are NOT asserted absent.
  * A successful retarget is evidence the worker accepted THAT proposal kind at THAT moment
    (RungConsensus / FadeProgressionCatchup). It is NOT evidence that an arbitrary revised slope
    would pass the ordinary one-frame guards; the phase 3 ms route is not a generic API.
  * In a lossy or incomplete capture the ledger is a FLOOR, not a count, and the per-decision
    revisability verdict is UNKNOWN rather than a number. Displaying "retargets: 0" when retargets
    existed but could not be attributed was a real defect found on real traces.

HARD BOUNDARY: analysis tooling only; never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = 1
MISSING = object()

COMPLETE, AMBIGUOUS, UNKNOWN, CONTRADICTS, REJECT_JOIN = (
    "COMPLETE", "AMBIGUOUS", "UNKNOWN", "CONTRADICTS", "REJECT_JOIN")

# input_disposition (schema). 5 and 6 are the only accepting outcomes; 6 follows an internal gap
# reset, which is NOT an extra reset record.
DISPOSITION = {1: "nonfinite", 2: "nonadvancing_timestamp", 3: "near_duplicate_fill",
               4: "backward_fill", 5: "accepted", 6: "accepted_after_gap_reset"}
ACCEPTING = {5, 6}
FEED = {1: "live_detection", 2: "ownership_seed"}
# FALLBACK ONLY. The authoritative maps come from the schema via load_enums() at startup; these
# exist so the tool still runs when the schema file is unavailable. Transcribing enums has already
# caused three defects in this project, most recently by dropping the far/near qualifiers below.
SOURCE = {0: "none_or_unrecognized", 1: "phase", 2: "phase_firstsight", 3: "sampler",
          4: "registration", 5: "fused", 6: "press_anchored", 7: "press_anchored_boot",
          8: "registration+sampler_far", 9: "sampler+registration_near",
          10: "registration_far_disagreement", 11: "sampler_near_disagreement"}

# Stages this consumer does NOT interpret. Named explicitly so the report can say NOT ANALYZED
# rather than silently display a zero -- displaying 0 retargets when retargets exist but carry no
# decision_id was a real defect found on real traces.
NOT_ANALYZED = {
    # Still the honest answer for a decision with no single decision_fit: without one record there
    # is no token and no route generation to join on, so there is nothing to look up.
    "retarget": "no single decision_fit, so the decision names no token or route generation and "
                "cannot be joined to a worker ledger; revisability is NOT ANSWERED for it",
}

# FALLBACK ONLY, same rule as SOURCE above: load_enums() reads these from the schema and that
# copy wins. They exist so the worker ledger still names outcomes when the schema file is absent,
# and they are never used to "correct" a value the schema supplies.
WORKER_EVENT = {1: "arm_result", 2: "claimed", 3: "dispatch_complete", 4: "retarget_result"}
ARM_RESULT = {0: "Armed", 1: "EngineDisarmed", 2: "RouteRejected", 3: "WindowRejected",
              4: "MailboxBusy"}
RETARGET_RESULT = {0: "Retargeted", 1: "EngineDisarmed", 2: "InvalidToken", 3: "WrongToken",
                   4: "NotWaiting", 5: "OutcomePending", 6: "RouteRejected", 7: "WindowRejected",
                   8: "EngineRejected"}
DISPATCH_RESULT = {0: "not_confirmed", 1: "confirmed_local_only"}

NATIVE_HEADER_REQUIRED = ["schema", "trace_kind", "qpc_hz", "sync_qpc", "sync_unix_us",
                          "sync_uncertainty_qpc", "capacity"]
PYTHON_HEADER_REQUIRED = ["schema", "trace_kind", "clock", "clock_hz", "sync_clock",
                          "sync_unix_ns", "sync_uncertainty_ns", "capacity"]


DEFAULT_SCHEMA_PATH = ("D:/NexusVision/diagnostics/timing-observer-20260920-190810/"
                       "TIMING_OBSERVER_SCHEMA.json")


def _i(v):
    """STRICT integer coercion. Returns None for anything that is not an exact integer.

    A permissive int() was the root of six completeness gaps found on real traces: bool coerced to
    1, a fractional 0.5 truncated to 0, and garbage strings slipped through wherever the caller only
    checked for None. Booleans are rejected outright -- `qpc_hz: true` is not a clock.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        t = v.strip()
        t = t[1:] if t.startswith("-") else t
        return int(v) if t.isdigit() else None
    return None


def load_enums(path=None):
    """Read the enum maps FROM THE SCHEMA rather than transcribing them.

    Transcription has now produced three separate defects in this project -- an ACK Failed value,
    and the far/near qualifiers on sources 8-11. Reading the contract removes the whole class.
    Returns None if the schema is unavailable, in which case the fallback maps below are used.
    """
    try:
        with open(path or DEFAULT_SCHEMA_PATH, "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    out = {}
    for key in ("source", "input_disposition", "feed", "reservation_disposition", "reset_reason",
                "worker_event", "retarget_kind", "route", "delivery_stage", "ruler_mode"):
        v = d.get(key)
        if isinstance(v, dict):
            out[key] = {int(k): s for k, s in v.items() if str(k).lstrip("-").isdigit()}
    wr = d.get("worker_results")
    if isinstance(wr, dict):
        for k, v in wr.items():
            if isinstance(v, dict):
                out[f"worker_results.{k}"] = {int(a): b for a, b in v.items()
                                              if str(a).lstrip("-").isdigit()}
    return out


def enum_text(table, v):
    """Unknown values are preserved verbatim and never guessed."""
    if v is None:
        return "unknown"
    name = table.get(v)
    return f"{v}({name})" if name else f"{v}(unmapped)"


class Journal:
    """One file == one process. Identity is (capture_id, role, pid, instance) -- never capture_id."""

    def __init__(self, path):
        self.path = Path(path)
        self.header = None
        self.footer = None
        self.events = []
        self.stats = []
        self.malformed = []
        self.duplicate_headers = 0
        self.duplicate_footers = 0
        self.events_after_footer = 0
        self.records_after_footer = 0
        self.header_first = True
        self.missing_event_clock = 0

    role = property(lambda s: (s.header or {}).get("role"))
    pid = property(lambda s: (s.header or {}).get("pid"))
    instance = property(lambda s: (s.header or {}).get("instance"))
    capture_id = property(lambda s: (s.header or {}).get("capture_id"))

    @property
    def identity(self):
        return (self.capture_id, self.role, self.pid, self.instance)

    @property
    def label(self):
        return f"{self.role}/pid{self.pid}/inst{self.instance}"

    def completeness(self):
        """Schema 'complete': exactly one header and a shutdown footer, accepted==written==count,
        contiguous ids 1..N, pending 0, every drop counter 0. Anything else is incomplete or lossy.
        An empty/disabled file is NOT a negative observation."""
        f = []
        required = PYTHON_HEADER_REQUIRED if self.role == "python" else NATIVE_HEADER_REQUIRED
        if self.header is None:
            f.append("NO_HEADER")
        else:
            # Process identity is (capture_id, role, pid, instance). A journal that cannot name
            # itself cannot be joined to anything, so a missing element is fatal, not cosmetic.
            for k in ("capture_id", "role", "pid", "instance"):
                if self.header.get(k) in (None, ""):
                    f.append(f"IDENTITY_MISSING:{k}")
            if _i(self.header.get("pid")) is None:
                f.append(f"IDENTITY_BAD:pid={self.header.get('pid')!r}")
            # sync uncertainty is a bracket width: negative is meaningless.
            for k in ("sync_uncertainty_qpc", "sync_uncertainty_ns"):
                if k in self.header:
                    u = _i(self.header[k])
                    if u is None or u < 0:
                        f.append(f"HEADER_BAD:{k}={self.header[k]!r}")
            for k in ("sync_qpc", "sync_unix_us", "sync_clock", "sync_unix_ns"):
                if k in self.header and _i(self.header[k]) is None:
                    f.append(f"HEADER_BAD:{k}={self.header[k]!r}")
        if not self.header_first:
            f.append("HEADER_NOT_FIRST")
        if self.records_after_footer:
            # A footer that is not the last record is not a footer.
            f.append(f"RECORDS_AFTER_FOOTER:{self.records_after_footer}")
        if self.header is not None:
            # Typed validation, not key presence: a key holding null or a non-integer clock is not
            # a valid header, and key-presence checking passed such files.
            for k in required:
                if k not in self.header:
                    f.append(f"HEADER_MISSING:{k}")
                elif self.header[k] is None:
                    f.append(f"HEADER_NULL:{k}")
            for k in ("qpc_hz", "clock_hz", "capacity"):
                if k in self.header and (_i(self.header[k]) is None or _i(self.header[k]) <= 0):
                    f.append(f"HEADER_BAD:{k}={self.header[k]}")
            if self.header.get("schema") != SCHEMA:
                f.append(f"FOREIGN_SCHEMA:{self.header.get('schema')}")
            if self.header.get("trace_kind") != "timing":
                f.append(f"TRACE_KIND:{self.header.get('trace_kind')}")
        if self.duplicate_headers:
            f.append("DUPLICATE_HEADER")
        if self.footer is None:
            f.append("NO_FOOTER")
        else:
            if self.footer.get("reason") != "shutdown":
                f.append(f"FOOTER_REASON:{self.footer.get('reason')}")
            pending = _i(self.footer.get("pending"))
            if pending is None or pending != 0:
                # A NEGATIVE pending is not "no backlog"; it is a corrupt counter.
                f.append(f"PENDING:{self.footer.get('pending')}")
            n = len(self.events)
            acc, wri = _i(self.footer.get("accepted")), _i(self.footer.get("written"))
            # `written` is REQUIRED, not optional. Treating it as optional meant a footer with the
            # field removed still read as complete.
            if acc is None or wri is None or acc != n or wri != n:
                f.append(f"COUNT_MISMATCH:accepted={acc},written={wri},parsed={n}")
            drop_keys = ("dropped_full", "dropped_contention",
                         "dropped_clock" if self.role != "python" else "dropped_invalid")
            # A footer cannot report FEWER drops than a stats snapshot already reported. Counters
            # are monotonic; a footer that "resets" them is not a clean run.
            for k in drop_keys:
                fv = _i(self.footer.get(k))
                hi = max([_i(st.get(k)) or 0 for st in self.stats] or [0])
                if fv is not None and hi > fv:
                    f.append(f"COUNTER_REGRESSION:{k} stats={hi} > footer={fv}")
            for k in drop_keys:
                if k not in self.footer:
                    f.append(f"DROPS_UNKNOWN:{k}_absent")
                    continue
                d = _i(self.footer.get(k))
                if d is None or d < 0:
                    f.append(f"DROPS_UNKNOWN:{k}={self.footer.get(k)}")
                elif d > 0:
                    f.append(f"LOSSY:{k}={d}")
        if self.duplicate_footers:
            f.append("DUPLICATE_FOOTER")
        if self.events_after_footer:
            f.append("EVENTS_AFTER_FOOTER")
        if self.missing_event_clock:
            f.append(f"EVENT_CLOCK_MISSING:{self.missing_event_clock}")
        if self.malformed:
            f.append(f"MALFORMED:{len(self.malformed)}")
        # Contiguity must hold IN FILE ORDER. Sorting first made a reordered journal read as
        # contiguous, which hid exactly the corruption the check exists to catch.
        ids = [_i(e.get("id")) for e in self.events]
        if ids and all(i is not None for i in ids):
            if ids != list(range(1, len(ids) + 1)):
                f.append("ID_NOT_CONTIGUOUS" if sorted(ids) != list(range(1, len(ids) + 1))
                         else "ID_OUT_OF_ORDER")
        elif ids:
            f.append("ID_MISSING")
        return f

    @property
    def lossy_or_incomplete(self):
        return bool(self.completeness())


def load(path):
    j = Journal(path)
    seen_footer = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except ValueError as exc:
                j.malformed.append((n, str(exc)))
                continue
            if not isinstance(o, dict):
                j.malformed.append((n, "not an object"))
                continue
            t = o.get("type")
            if t == "header":
                if j.header is None:
                    j.header = o
                else:
                    j.duplicate_headers += 1
            elif t == "footer":
                if seen_footer:
                    j.duplicate_footers += 1
                else:
                    j.footer = o
                seen_footer = True
            elif t == "event":
                if seen_footer:
                    j.events_after_footer += 1
                    j.records_after_footer += 1
                if j.header is None:
                    j.header_first = False
                clock_key = "clock_ns" if o.get("clock_ns") is not None else "qpc"
                if _i(o.get(clock_key)) is None:
                    j.missing_event_clock += 1
                o["_line"] = n
                j.events.append(o)
            elif t == "stats":
                # Periodic stats snapshots are a VALID record type. Rejecting them as malformed
                # made healthy journals read as corrupt. But a stats record AFTER the footer means
                # the footer was not final.
                if seen_footer:
                    j.records_after_footer += 1
                if j.header is None:
                    j.header_first = False
                j.stats.append(o)
            else:
                j.malformed.append((n, f"unknown record type {t!r}"))
    return j


class Decision:
    """One canonical decision snapshot: decision_fit + decision_members + its sampler provenance."""

    def __init__(self, key):
        self.key = key                 # (identity, context, epoch, decision_id)
        self.fit = []
        self.members = []
        self.reservations = []
        self.retargets = []
        self.flags = []
        self.notes = []
        self.pair_incomplete = None

    # -- support reconstruction -------------------------------------------
    def fit_record(self):
        return self.fit[0] if len(self.fit) == 1 else None

    def computed_fit(self):
        """Schema: n<3 or already-at-target can RETURN BEFORE weighted fitting. wrss=-1 and zero
        weight sums mean there is NO COMPUTED FIT -- not a fit with poor support."""
        f = self.fit_record()
        if f is None:
            return None
        d = f.get("data", f)
        wrss = _i(d.get("wrss_q9"))
        ws = _i(d.get("weight_sum_q9"))
        wq = _i(d.get("weight_sq_sum_q9"))
        if wrss is None or ws is None or wq is None:
            return None
        if wrss < 0 or ws == 0 or wq == 0:
            return False
        return True

    def effective_n(self):
        """(Sum w)^2 / Sum w^2 -- a WEIGHTING measure, not a count of independent error samples.
        Defined only when a fit was actually computed."""
        if self.computed_fit() is not True:
            return None
        d = self.fit_record().get("data", self.fit_record())
        ws = _i(d.get("weight_sum_q9")) / 1e9
        wq = _i(d.get("weight_sq_sum_q9")) / 1e9
        return (ws * ws / wq) if wq else None

    def raw_n(self):
        f = self.fit_record()
        if f is None:
            return None
        return _i(f.get("data", f).get("n"))

    def hypothetical_only(self):
        """Report candidate weights explicitly AS telemetry so no reader mistakes them for fusion."""
        out = []
        for m in self.members:
            d = m.get("data", m)
            for k in ("phase_hypothetical_weight_q9", "registration_hypothetical_weight_q9"):
                v = _i(d.get(k))
                if v:
                    out.append(f"{k.replace('_q9','')}={v/1e9:.3f} (TELEMETRY, not applied)")
        return out


class TokenLedger:
    """Bounded per-token worker ledger.

    Key: (native process identity, engine context, token, route generation). A token of 0, or an
    unknown context or route generation, means NO JOIN -- those rows are held unattributed rather
    than guessed into a bucket. There is NO nearest-time attribution anywhere in this class.

    What it answers: what the worker actually returned for an arm and for each retarget, what the
    remaining time was at claim, and whether a dispatch was locally confirmed. What it does NOT
    answer is whether any particular engine proposal caused a given worker retarget -- see
    `attribution`.
    """

    def __init__(self, key):
        self.key = key
        self.arm = []
        self.claims = []
        self.retargets = []
        self.dispatches = []
        self.unknown_events = []
        # Set by Capture.build(). "not computed" is deliberately NOT "unattributed": a ledger that
        # never reached attribution must not read as one that reached it and found nothing.
        self.attribution = "not computed"

    def add(self, rec, enums):
        d = rec.get("data", rec)
        ev = _i(d.get("event"))
        name = (enums.get("worker_event") or WORKER_EVENT).get(ev)
        {"arm_result": self.arm, "claimed": self.claims,
         "retarget_result": self.retargets, "dispatch_complete": self.dispatches
         }.get(name, self.unknown_events).append(rec)

    @staticmethod
    def _result(rec):
        return _i(rec.get("data", rec).get("result"))

    def arm_outcome(self, enums):
        if not self.arm:
            return None, ("no arm_result row in this capture for this token -- NOT a claim "
                          "that the worker was never armed")
        r = self._result(self.arm[0])
        tbl = enums.get("worker_results.arm_result") or ARM_RESULT
        return r, enum_text(tbl, r)

    def accepted_deadline(self):
        """Deadlines are accepted ONLY on result enum 0 (Armed / Retargeted). Any other return is
        a refusal and its deadline field must not be read as an accepted target."""
        out = []
        for rec in self.arm + self.retargets:
            if self._result(rec) == 0:
                d = rec.get("data", rec)
                out.append(_i(d.get("deadline_us")))
        return [x for x in out if x is not None]

    def retarget_outcomes(self, enums):
        tbl = enums.get("worker_results.retarget_result") or RETARGET_RESULT
        return [enum_text(tbl, self._result(r)) for r in self.retargets]

    def revisability(self, enums):
        """Observed revisability for this token, stated as what the worker RETURNED.

        A successful retarget is evidence the worker accepted one, for that proposal kind, at that
        moment. It is NOT evidence that an arbitrary revised slope would pass ordinary one-frame
        guards -- the validated retarget path is specialised (RungConsensus / FadeProgressionCatchup)
        and the phase 3 ms route is not a generic API.
        """
        if not self.retargets:
            return ("no worker retarget_result row in this capture for this token -- this "
                    "consumer does not infer that none was attempted")
        outs = self.retarget_outcomes(enums)
        good = sum(1 for o in outs if o.startswith("0("))
        claim_remaining = None
        for c in self.claims:
            claim_remaining = _i(c.get("data", c).get("remaining_us"))
            break
        s = f"{good}/{len(outs)} retarget(s) returned success: {', '.join(outs)}"
        if claim_remaining is not None:
            s += f"; remaining at claim {claim_remaining} us"
        return s + (" -- specialised retarget path only; NOT generalisable to an arbitrary "
                    "revised slope")

    def dispatch_outcomes(self, enums):
        """Dispatch confirmation is LOCAL ONLY. Schema: no event here implies console/game receipt,
        so `confirmed_local_only` means the local send path completed -- nothing further."""
        tbl = enums.get("worker_results.dispatch_complete") or DISPATCH_RESULT
        return [enum_text(tbl, self._result(d)) for d in self.dispatches]

    def unknown_event_codes(self, enums):
        tbl = enums.get("worker_event") or WORKER_EVENT
        return sorted({enum_text(tbl, _i(r.get("data", r).get("event")))
                       for r in self.unknown_events})


class Capture:
    def __init__(self, journals, enums=None):
        self.journals = journals
        self.decisions = {}
        self.inputs = defaultdict(list)      # (identity, context) -> sampler_input events
        self.frames = defaultdict(list)
        self.resets = defaultdict(list)
        # Worker side. Keyed on the FULL route scope -- (process identity, engine context, token,
        # route generation) -- because the schema forbids joining by token alone across engines or
        # processes. Rows that cannot name that scope go to unjoinable_worker and stay there.
        self.enums = enums if enums is not None else (load_enums() or {})
        self.ledgers = {}
        self.proposals = defaultdict(list)   # same key -> engine `retarget` rows (proposal side)
        self.unjoinable_worker = []          # (event, why) -- never guessed into a bucket
        self.unjoinable_proposals = []

    def capture_level_flags(self):
        """Flags about the CAPTURE as a whole, not about any single journal."""
        f = []
        ids = Counter(j.identity for j in self.journals)
        for k, c in ids.items():
            if c > 1:
                f.append(f"DUPLICATE_PROCESS_IDENTITY:{k}")
        roles = Counter(j.role for j in self.journals)
        for r in ("native", "python"):
            if roles.get(r, 0) == 0:
                f.append(f"NO_{r.upper()}_JOURNAL")
        caps = {j.capture_id for j in self.journals if j.capture_id}
        if len(caps) > 1:
            f.append("MIXED_CAPTURE_IDS")
        return f

    def flags(self):
        f = []
        for j in self.journals:
            f.extend(f"{j.label}:{x}" for x in j.completeness())
        f.extend(self.capture_level_flags())
        return f

    # Capture-level observations that are NOT journal defects and do not block absence inference.
    # This is an ALLOWLIST of exceptions. Everything Journal.completeness() reports blocks BY
    # CONSTRUCTION -- a blocklist of blocking names leaked the moment a new flag was added without
    # registering it (ID_OUT_OF_ORDER was emitted and silently non-blocking).
    CAPTURE_ADVISORY = ("NO_PYTHON_JOURNAL",)

    @property
    def blocking(self):
        """Every condition under which a missing stage must NOT be read as absence.

        Journal defects are blocking by construction. Only CAPTURE_ADVISORY entries are exempt.
        """
        out = []
        for j in self.journals:
            out.extend(f"{j.label}:{x}" for x in j.completeness())
        for x in self.capture_level_flags():
            if not x.startswith(self.CAPTURE_ADVISORY):
                out.append(x)
        return out

    def python_frame_conflicts(self):
        """`frame` is scoped to (python process, backend context, source_generation). The same
        number in two generations is a DIFFERENT frame, and must never be merged."""
        # ONLY the capture stages are scoped by (backend context, source_generation, frame).
        # bundle_publish / processing_start / processing_done legitimately carry NEITHER -- they are
        # scoped by orchestrator source_identity and processed_seq instead. Treating their absent
        # fields as an underspecified identity flagged every healthy real capture as AMBIGUOUS.
        CAPTURE_SCOPED = {"capture_publish", "capture_consume"}
        seen = defaultdict(list)
        for j in self.journals:
            if j.role != "python":
                continue
            for e in j.events:
                if e.get("stage") not in CAPTURE_SCOPED:
                    continue
                d = e.get("data", e)
                f = d.get("frame")
                if f is None:
                    continue
                seen[(j.identity, d.get("context"), d.get("source_generation"), f)].append(e)
        # A frame number repeating across DISTINCT, fully-scoped generations is NORMAL -- the
        # scoped identities differ, so nothing is ambiguous. Only an UNDERSPECIFIED identity (no
        # context or no source_generation) makes the join unresolvable.
        bad = {}
        for (ident, ctx, gen, f), evs in seen.items():
            if ctx is None or gen is None:
                bad[(ident, ctx, gen, f)] = evs
        return bad

    def capture_verdict(self):
        """Verdict for the capture as a whole -- used when there are no decisions to judge.

        A definite identity conflict outranks a possible completeness gap: the first is a
        contradiction in the data, the second is only an absence of evidence.
        """
        conflicts = self.python_frame_conflicts()
        if conflicts:
            k = next(iter(conflicts))
            return AMBIGUOUS, (f"frame {k[3]} carries an underspecified identity "
                               f"(context={k[1]}, source_generation={k[2]}); the key is "
                               "(process, context, source_generation, frame) and it cannot be "
                               "resolved without all four")
        if self.blocking:
            return UNKNOWN, "capture incomplete or lossy: " + ", ".join(self.blocking)
        return COMPLETE, ""

    def build(self):
        for j in self.journals:
            if j.role == "python":
                continue
            ident = j.identity
            for e in j.events:
                st = e.get("stage")
                d = e.get("data", e)
                ctx = e.get("context")
                if st == "sampler_input":
                    self.inputs[(ident, ctx)].append(e)
                elif st == "sampler_reset":
                    self.resets[(ident, ctx)].append(e)
                elif st == "native_frame":
                    self.frames[(ident, ctx)].append(e)
                elif st == "worker":
                    key, why = self.route_key(ident, e)
                    if key is None:
                        self.unjoinable_worker.append((e, why))
                    else:
                        led = self.ledgers.get(key)
                        if led is None:
                            led = self.ledgers[key] = TokenLedger(key)
                        led.add(e, self.enums)
                elif st in ("decision_fit", "decision_members", "reservation", "retarget"):
                    did = d.get("decision_id")
                    if did is None and st == "retarget":
                        # A real `retarget` row carries proposal_id, NOT decision_id -- that is why
                        # this consumer previously reported revisability as NOT ANALYZED. It is the
                        # ENGINE GUARD OBSERVATION, so preserve it by full route scope. It is
                        # NOT a worker-call binding: a commit-before-worker emits a guard with no
                        # worker return, and an early worker refusal emits a return with no guard.
                        pkey, pwhy = self.route_key(ident, e)
                        if pkey is None:
                            self.unjoinable_proposals.append((e, pwhy))
                        else:
                            self.proposals[pkey].append(e)
                        continue
                    key = (ident, ctx, e.get("epoch"), did)
                    dec = self.decisions.get(key)
                    if dec is None:
                        dec = self.decisions[key] = Decision(key)
                    {"decision_fit": dec.fit, "decision_members": dec.members,
                     "reservation": dec.reservations, "retarget": dec.retargets}[st].append(e)

        # decision_id uniqueness is per-engine, NOT per-epoch. Keying on epoch let the same id
        # under two epochs split into separate entries and escape the reuse check entirely.
        per_engine = defaultdict(set)
        for (ident, ctx, epoch, did) in self.decisions:
            per_engine[(ident, ctx, did)].add(epoch)
        for key, dec in self.decisions.items():
            epochs = per_engine[(key[0], key[1], key[3])]
            if len(epochs) > 1:
                dec.flags.append(
                    f"REUSED_DECISION_ID: decision_id {key[3]} appears under epochs "
                    f"{sorted(str(e) for e in epochs)} in one engine; uniqueness is per-engine")
            # Reused ids with contradictory payload are AMBIGUOUS -- never last-wins.
            if len(dec.fit) > 1:
                sigs = {json.dumps(x.get("data", x), sort_keys=True) for x in dec.fit}
                dec.flags.append(
                    f"REUSED_DECISION_ID: {len(dec.fit)} decision_fit records"
                    + (" with contradictory payload" if len(sigs) > 1 else " (identical payload)"))
            if len(dec.members) > 1:
                dec.flags.append(f"REUSED_DECISION_ID: {len(dec.members)} decision_members")
            if dec.fit and not dec.members:
                # The PAIRED RECORD is incomplete: fit and members are a required emitted pair, so
                # the decision must not be called COMPLETE. The fit-support sub-result remains
                # reportable and is still printed -- an incomplete pair is not an unusable fit.
                dec.pair_incomplete = ("decision_members absent; fit and members are a required "
                                       "emitted pair, so this decision record is not COMPLETE "
                                       "(the fit-support sub-result below is still usable)")
            # Support must come from the same sampler generation the fit claims.
            f = dec.fit_record()
            if f is not None:
                fg = _i(f.get("data", f).get("sampler_generation"))
                ident, ctx = key[0], key[1]
                gens = {_i(i.get("data", i).get("sampler_generation"))
                        for i in self.inputs.get((ident, ctx), [])}
                if fg is not None and gens and fg not in gens:
                    dec.flags.append(
                        f"GENERATION_MISMATCH: fit sampler_generation {fg} not among inputs {sorted(g for g in gens if g is not None)}")

        # Preserve engine guard observations beside the worker ledger WITHOUT inventing a causal
        # binding. Worker returns carry no proposal_id, and ONE OBSERVED GUARD IS NOT ONE ELIGIBLE
        # WORKER CALL: commit-before-worker emits a guard with no worker return; an early worker
        # refusal emits a return with no guard. Even one row each with equal deadlines does not
        # establish a shared call identity. No nearest-time tiebreak is permitted.
        # (Defect found by Codex against f978424f: the "sole eligible" branch below asserted a
        # binding across multiple worker returns, pre-arm commits and contradictory deadlines.)
        for key, led in self.ledgers.items():
            props = self.proposals.get(key, [])
            ids = sorted(x for x in {_i(p.get("data", p).get("proposal_id")) for p in props}
                         if x is not None)
            if not led.retargets:
                led.attribution = (
                    "no worker retarget_result row for this token"
                    + (f"; {len(props)} engine proposal row(s) reached the final guard and no "
                       "worker return was recorded -- observed, not inferred absent" if props
                       else ""))
            elif len(ids) == 1 and len(props) == 1:
                led.attribution = (
                    f"UNATTRIBUTED: proposal_id {ids[0]} is the sole OBSERVED engine guard row "
                    f"beside {len(led.retargets)} worker retarget_result row(s) in this route "
                    "scope, not a proven binding. Worker rows carry no proposal_id; pre-arm "
                    "commits and early worker returns need not emit paired rows")
            elif ids:
                led.attribution = (
                    f"UNATTRIBUTED: {len(props)} engine guard row(s) {ids} are observed in this "
                    "route scope, not proven eligible bindings; worker rows carry no proposal_id")
            else:
                led.attribution = (
                    "UNATTRIBUTED: no engine `retarget` row in this route scope. The engine records "
                    "that stage only for proposals reaching the final identity/deadline guard, so "
                    "earlier engine returns are NOT asserted absent")
        return self

    @staticmethod
    def route_key(ident, e):
        """Full route scope for a worker/proposal row, or None plus the reason it cannot be joined.

        Schema, `identity_scopes`: a zero root context is NULL, a zero token is UNSET, and the
        worker joins on the COPIED token and route generation rather than mutable shot state. Any
        one of those missing means there is no join -- the row is held unattributed, never dropped
        into the nearest bucket.
        """
        d = e.get("data", e)
        ctx, gen, tok = _i(e.get("context")), _i(e.get("generation")), _i(d.get("token"))
        why = []
        if not ctx:
            why.append("root context " + ("is 0 (null)" if ctx == 0 else "missing"))
        if gen is None:
            why.append("route generation unknown")
        if not tok:
            why.append("token " + ("is 0 (unset)" if tok == 0 else "missing"))
        return (None, "; ".join(why)) if why else ((ident, ctx, tok, gen), "")

    def revisability_for(self, dec):
        """Post-arm revisability for one decision, as (verdict, detail).

        The join runs decision -> its own token and route generation -> worker ledger. It is
        attempted ONLY from the decision's own record, never from a neighbouring one, and it is
        refused outright when the capture is lossy or incomplete: there, an absent worker row is
        not evidence that the worker did nothing.
        """
        f = dec.fit_record()
        if f is None:
            return "NOT ANALYZED", NOT_ANALYZED["retarget"]
        key, why = self.route_key(dec.key[0], f)
        if key is None:
            return "NOT ANALYZED", f"decision carries no joinable route scope: {why}"
        led = self.ledgers.get(key)
        if led is None:
            if self.blocking:
                return "UNKNOWN", ("no worker record in this route scope, and the capture is lossy "
                                   "or incomplete -- absence proves nothing here")
            return "NOT ANALYZED", ("no worker record in this token's full route scope; this "
                                    "consumer does not infer that none occurred")
        if self.blocking:
            return "UNKNOWN", (f"capture is lossy or incomplete; observed only -- "
                               f"{led.revisability(self.enums)}")
        return led.revisability(self.enums), led.attribution

    def support_for(self, dec):
        """Composition of the fit's support, reconstructed from sampler provenance.

        THE FIT'S OWN SNAPSHOT IS AUTHORITATIVE FOR SUPPORT SIZE. `n`, `first_sample_us`,
        `last_sample_us` and the weight sums are what the engine actually fitted. Provenance is used
        only to EXPLAIN the composition of that support, never to recount it.

        A previous version of this method counted every sampler input in the context that shared the
        fit's generation -- including inputs that arrived AFTER the decision. On a real trace it
        reported 11 accepted where the fit had 3. Two fences prevent that:
          1. `input_id <= fit.input_id` -- the fit's input_id is its ledger position, so a later
             ledger position cannot have contributed to it.
          2. the fit's own [first_sample_us, last_sample_us] window, which is the rolling eviction
             the sampler actually applied.
        When provenance and the fit disagree on size, the composition is reported as UNRECONSTRUCTED
        rather than substituted for the fit's number.
        """
        ident, ctx = dec.key[0], dec.key[1]
        f = dec.fit_record()
        fd = f.get("data", f) if f else {}
        fg = _i(fd.get("sampler_generation"))
        ledger = _i(fd.get("input_id"))
        first_us, last_us = _i(fd.get("first_sample_us")), _i(fd.get("last_sample_us"))
        authoritative_n = _i(fd.get("n"))

        live_avail = accepted = seed_accepted = rejected = unknown_feed = 0
        fence_bypassed = duplicate_ids = 0
        seen_ids = set()
        rej = Counter()
        for i in self.inputs.get((ident, ctx), []):
            d = i.get("data", i)
            if fg is not None and _i(d.get("sampler_generation")) != fg:
                continue
            iid = _i(d.get("input_id"))
            t = _i(d.get("capture_engine_us"))
            # A fence that cannot be APPLIED is not a fence that passed. An input missing input_id
            # or capture_engine_us slips past both tests, so its membership is unknown and the
            # reconstruction must be void rather than quietly inclusive.
            if iid is None or (ledger is not None and iid > ledger):
                if iid is None:
                    fence_bypassed += 1
                    continue
                continue                      # FUTURE input: cannot have contributed to this fit
            if first_us is not None and last_us is not None:
                if t is None:
                    fence_bypassed += 1
                    continue
                if not (first_us <= t <= last_us):
                    continue                  # outside the fit's own retained window
            if iid in seen_ids:
                duplicate_ids += 1
            seen_ids.add(iid)
            disp, feed = _i(d.get("disposition")), _i(d.get("feed"))
            if disp in ACCEPTING:
                if feed == 2:
                    seed_accepted += 1
                elif feed == 1:
                    accepted += 1
                else:
                    unknown_feed += 1         # NEVER silently counted as live
            elif disp is not None:
                rejected += 1
                rej[enum_text(DISPOSITION, disp)] += 1
            if feed == 1:
                live_avail += 1

        contributing = accepted + seed_accepted + unknown_feed
        # Matching n is NECESSARY but not SUFFICIENT: a count can coincide while provenance is
        # missing or duplicated. Both must be clean for the composition to be trusted.
        reconstructed = (authoritative_n is not None and contributing == authoritative_n
                         and fence_bypassed == 0 and duplicate_ids == 0)
        why = []
        if fence_bypassed:
            why.append(f"{fence_bypassed} input(s) missing input_id/capture_engine_us, so the "
                       "fences could not be applied")
        if duplicate_ids:
            why.append(f"{duplicate_ids} duplicate input_id(s) in provenance")
        if authoritative_n is not None and contributing != authoritative_n:
            why.append(f"provenance sums to {contributing}, fit reports n={authoritative_n}")
        return {"authoritative_n": authoritative_n,
                "reconstructed": reconstructed,
                "unreconstructed_why": "; ".join(why),
                "input_calls_live": live_avail, "accepted_live": accepted,
                "accepted_seed_replay": seed_accepted, "accepted_unknown_feed": unknown_feed,
                "rejected": rejected, "rejected_reasons": dict(rej),
                "resets": len(self.resets.get((ident, ctx), []))}

    def verdict(self, dec):
        if dec.flags:
            kind = REJECT_JOIN if any("GENERATION_MISMATCH" in x for x in dec.flags) else AMBIGUOUS
            return kind, "; ".join(dec.flags)
        blocking = self.blocking
        f = dec.fit_record()
        if f is None:
            if blocking:
                return UNKNOWN, "no single decision_fit; capture incomplete: " + ", ".join(blocking)
            return CONTRADICTS, "no decision_fit for a referenced decision_id"
        d = f.get("data", f)
        unmapped = []
        if _i(d.get("selected_source")) not in SOURCE:
            unmapped.append(f"selected_source={d.get('selected_source')}")
        for i in self.inputs.get((dec.key[0], dec.key[1]), []):
            di = _i(i.get("data", i).get("disposition"))
            if di is not None and di not in DISPOSITION:
                unmapped.append(f"disposition={di}")
                break
        if unmapped:
            return UNKNOWN, "unmapped enum value(s), preserved not guessed: " + ", ".join(unmapped)
        if blocking:
            return UNKNOWN, "capture incomplete or lossy: " + ", ".join(blocking)
        if dec.pair_incomplete:
            return UNKNOWN, dec.pair_incomplete
        for r in self.resets.get((dec.key[0], dec.key[1]), []):
            # A reset must be placed on the ENGINE clock to be ordered against a decision, whose
            # now/tip/crossing are engine microseconds. The journal QPC stamp is a DIFFERENT clock
            # domain (schema clock_domains: "Native journal QPC stamps do not equal engine
            # clocks"), so its presence does not make the reset placeable.
            if r.get("data", r).get("now_us") is None:
                return UNKNOWN, ("sampler_reset carries no engine clock (now_us), so it cannot be "
                                 "ordered against this decision; the journal QPC stamp is a "
                                 "different clock domain and does not substitute")
        return COMPLETE, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir")
    ap.add_argument("--file", nargs="*", default=[])
    ap.add_argument("--decision", type=int, nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--csv")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.file]
    if args.dir:
        paths.extend(sorted(Path(args.dir).rglob("*.jsonl")))
    if not paths:
        ap.error("no journals: pass --dir or --file")
    cap = Capture([load(p) for p in paths]).build()
    flags = cap.flags()

    print("== capture ==")
    for j in cap.journals:
        print(f"  {j.path.name}: {j.label or '(no header)'} events={len(j.events)} "
              f"flags={','.join(j.completeness()) or 'none'}")
    print(f"flags        : {', '.join(flags) or 'none'}")
    print(f"blocking     : {', '.join(cap.blocking) or 'none'}")
    print("authority    : observer fields NEVER supply detection, ownership, release timing or "
          "capture authority; no record implies console/game receipt")

    rows = []
    for key in sorted(cap.decisions, key=lambda k: str(k)):
        dec = cap.decisions[key]
        did = key[3]
        if args.decision and did not in args.decision:
            continue
        verdict, detail = cap.verdict(dec)
        sup = cap.support_for(dec)
        eff = dec.effective_n()
        computed = dec.computed_fit()
        rv, rvd = cap.revisability_for(dec)
        rows.append({
            "decision_id": did, "epoch": key[2], "context": key[1],
            "verdict": verdict, "detail": detail,
            "raw_n": sup["authoritative_n"],
            "reconstructed": sup["reconstructed"],
            "unreconstructed_why": sup["unreconstructed_why"],
            "input_calls_live": sup["input_calls_live"],
            "accepted_unknown_feed": sup["accepted_unknown_feed"],
            "computed_fit": {True: "yes", False: "NO (returned before weighted fitting)",
                             None: "unknown"}[computed],
            "effective_n": f"{eff:.2f}" if eff is not None else "n/a",
            "accepted_live": sup["accepted_live"],
            "accepted_seed_replay": sup["accepted_seed_replay"],
            "rejected": sup["rejected"], "rejected_reasons": json.dumps(sup["rejected_reasons"]),
            "resets": sup["resets"],
            "telemetry_weights": "; ".join(dec.hypothetical_only()),
            "revisability": rv, "revisability_detail": rvd,
            "notes": "; ".join(dec.notes),
        })

    cap_verdict, cap_detail = cap.capture_verdict()
    print(f"capture      : [{cap_verdict}]" + (f"  {cap_detail}" if cap_detail else ""))

    shown = rows if args.all else [r for r in rows if r["verdict"] != COMPLETE]
    print(f"\n== decisions ({len(shown)} shown of {len(rows)}) ==")
    if not rows:
        print("  no canonical decisions in this capture; the capture-level verdict above stands.")
    for r in shown:
        print(f"\ndecision {r['decision_id']} (epoch {r['epoch']}, ctx {r['context']})  "
              f"[{r['verdict']}]")
        print(f"  fit        : n={r['raw_n']} (AUTHORITATIVE) computed={r['computed_fit']} "
              f"effective_n={r['effective_n']}")
        print(f"  composition: accepted_live={r['accepted_live']} "
              f"seed_replay={r['accepted_seed_replay']} "
              f"unknown_feed={r['accepted_unknown_feed']} rejected={r['rejected']} "
              f"{r['rejected_reasons']}"
              + ("" if r["reconstructed"] else f"  [UNRECONSTRUCTED: {r['unreconstructed_why']}]"))
        print(f"  input calls: {r['input_calls_live']} live sampler calls in the fit window "
              "(calls, NOT distinct available frames)")
        print(f"  revisability: {r['revisability']} -- {r['revisability_detail']}")
        for k in ("detail", "telemetry_weights", "notes"):
            if r[k]:
                print(f"  {k:<11}: {r[k]}")
        if r["resets"]:
            print(f"  resets     : {r['resets']} in this context")

    print(f"\n== worker ledger ({len(cap.ledgers)} token(s)) ==")
    if cap.blocking:
        print("  [UNKNOWN] the capture is lossy or incomplete: an absent worker row is NOT "
              "evidence of an absent event, so every line below is a floor, not a count.")
    if not cap.ledgers:
        print("  no joinable worker records in this capture.")
    for key in sorted(cap.ledgers, key=lambda k: str(k)):
        led = cap.ledgers[key]
        _code, arm_text = led.arm_outcome(cap.enums)
        dl = led.accepted_deadline()
        print(f"\ntoken {key[2]} (ctx {key[1]}, route gen {key[3]})")
        print(f"  arm        : {arm_text}")
        print(f"  deadlines  : accepted {dl if dl else 'none'}"
              f"{' us' if dl else ''} -- result enum 0 only; a refusal's deadline field is the "
              "REQUESTED value, not an accepted target")
        print(f"  claims     : {len(led.claims)} (remaining-time snapshot, not an engine deadline)")
        disp = led.dispatch_outcomes(cap.enums)
        print(f"  dispatch   : {', '.join(disp) if disp else 'none recorded'} -- LOCAL ONLY; no "
              "record here implies console or game receipt")
        print(f"  revisability: {led.revisability(cap.enums)}")
        print(f"  attribution: {led.attribution}")
        if led.unknown_events:
            print(f"  UNMAPPED   : {len(led.unknown_events)} worker event(s) "
                  f"{led.unknown_event_codes(cap.enums)} -- preserved, not guessed")
    if cap.unjoinable_worker:
        print(f"\n  {len(cap.unjoinable_worker)} worker row(s) HELD UNJOINABLE (no bucket guessed):")
        for _e, why in cap.unjoinable_worker[:10]:
            print(f"    - {why}")
    if cap.unjoinable_proposals:
        print(f"  {len(cap.unjoinable_proposals)} engine proposal row(s) held unjoinable:")
        for _e, why in cap.unjoinable_proposals[:10]:
            print(f"    - {why}")
    print("  NOTE: a successful retarget shows the worker accepted THAT proposal kind at THAT "
          "moment\n  (RungConsensus / FadeProgressionCatchup). It is NOT evidence that an "
          "arbitrary revised slope\n  would pass the ordinary one-frame guards.")

    print("\n== summary ==")
    for k, v in sorted(Counter(r["verdict"] for r in rows).items()):
        print(f"  {k:<12} {v}")
    print("\nCOMPLETE means only that the observed LOCAL records are internally consistent. "
          "Effective n is a\nweighting measure, not a count of independent error samples, and "
          "candidate weights are telemetry,\nnot evidence of applied fusion. A missing stage in a "
          "lossy or incomplete capture is UNKNOWN.")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
