# Probe schema 2 — the measurement gate contract

Status: **CONTRACT DRAFT, no producer exists.** The active `AutomationEngine.cpp` still matches the
staged observer transaction's *before* hash. Nothing in this document is implemented in the engine;
it is the target the producer must hit and the contract the consumer (`tools/timing/probe_audit.py`)
enforces. Written 2026-09-20 (Opus) from Codex's field-level specification, which is itself the
answer to "does the existing observer already carry what the gate needs".

**It does not.** The observer (schema 1) carries process identity, engine contexts, route
generations, frame provenance, sampler inputs, canonical decision snapshots, requested deadlines,
arm/retarget results, worker command/completion timestamps and loss accounting. It does **not**
carry a cross-process physical-shot identity, the native press/onset latches, *the bucket actually
used*, the operation → accepted-revision → claimed-deadline linkage, or any
treatment / adherence / outcome accounting.

So: **extend the observer, schema 2. Do not build a second logger.** Same bounded, non-blocking
producer; hashes and configuration snapshots go in cold-path metadata, never hot-path string
formatting.

> **THE WIRE SCHEMA IS `PROBE_SCHEMA_2.json`, NOT THIS FILE.** This document is the prose and the
> reasoning; the JSON carries the exact field names, types, units, enum values, probability scale
> and cardinality that a producer targets. `tests/test_probe_schema_conformance.py` binds the JSON,
> the fixtures and the consumer together, so a stage or a threshold cannot be changed in one place
> only. Where the two disagree, **the JSON wins** -- prose is how three transcription defects got
> into this project already.
>
> **Freezes are published under a NEW revision path each time** (`probe-gate-<date>-rN`). Replacing
> the contents of a directory someone has already reviewed by hash defeats exact-hash review.

---

## 1. Identity contract

| identity | scope |
|---|---|
| process | existing `(capture_id, role, pid, instance)` |
| **physical shot** | native process + engine `context` + `physical_epoch` |
| attempt | physical shot + `attempt` |
| **assignment** | one `assignment_id` per physical shot |
| schedule operation | engine-scoped `operation_id`, linked to `assignment_id`, `decision_id`, `plan_id` |
| worker execution | existing process/context/token/route-generation scope **plus accepted `target_revision`** |
| outcome | Python process/orchestrator + shot-record session/sequence + `verdict_seq`, **explicitly linked** to the native physical shot |

**Assignment must survive attempt and token changes within the same physical shot.** The existing
development hook caches by `shot_.armToken`; that is not this contract.

### The record's OWNER is not the journal's EMITTER

A reader onset or a shot verdict is emitted by a **Python** journal but describes a **native-owned**
physical shot. Filing it under the emitting journal's identity creates a separate, Python-owned
shot and the cross-process join fails silently while looking healthy.

So every Python-emitted record carries an explicit native-owner reference -- `native_capture_id`,
`native_pid`, `native_instance` -- and a record without a complete one is **held unattached**, never
filed under its emitter. Fixtures must use genuinely separate native and Python journals; putting
Python-origin records inside the native journal conceals exactly this defect.

### The naming trap, to be removed in schema 2

- `BANNER TRIM release_seq` is currently used as the **physical epoch**.
- native `Release delivery identity release_seq` is a **separate release counter**.

Rename to `physical_epoch` and `native_release_seq`. **Never join them because their values
coincide.** A consumer that does is wrong even when the numbers agree.

---

## 2. Clock contract

Domains are explicit fields, not `_ms` / `_us` suffixes:

| domain | carries |
|---|---|
| engine monotonic µs | physical press, native acceptance/onset, deadlines, worker command and completion |
| Python `perf_counter_ns` | reader-processing and observation times |
| Unix epoch µs | carried capture/press metadata, **labelled as such** |
| journal QPC | enqueue time only — **never the semantic event time** |

Keep the existing header sync records. Add an **engine-clock ↔ QPC bridge** when cross-domain
timestamps will be compared.

### `clock_bridge` (record type, implemented)

Scoped to an **engine context**, not to a shot — it carries no `physical_epoch` and must never be
pushed through the shot-identity resolver. Fields: `sync_id`, `engine_us` and `qpc` (the anchor
pair), `valid_from_engine_us` / `valid_to_engine_us` (the bracket this segment is good for), and
`uncertainty_us`.

The consumer converts only through an **applicable** segment — one whose bracket *contains* the
timestamp. It refuses when:

| condition | refusal |
|---|---|
| the manifest declares no `clock_tolerance_us` | no conversion may be trusted |
| no bridge exists for the context | ENGINE_ONLY (see below) — not a failure |
| the timestamp falls between two segments | **CLOCK DISCONTINUITY**, named as such |
| the timestamp is outside every segment | outside-bracket, a distinct defect from a gap |
| two segments both claim it | overlapping segments |
| `uncertainty_us` is absent, negative, or above the tolerance | uncertainty refusal |

A *nearest* segment is not an applicable segment. Extrapolating past a bracket is exactly the
cross-clock arithmetic this contract forbids, and it is how a clock discontinuity becomes a
confident wrong number.

### ENGINE_ONLY is a separate verdict, and it is not a failure

**Do not require cross-clock conversion for quantities already measurable entirely in the engine
clock.** Native onset, command-after-press, command-after-native-onset, scheduling tardiness and
scheduling displacement are **all engine-clock**. The bridge is needed only for the reader/native
cross-check.

So the clock verdict is reported **separately** from measurement, adherence and outcome, and a
capture with no bridge is `ENGINE_ONLY`: a limitation on the cross-check, not on the experiment.
There are tests asserting that a missing *and* a refused bridge both leave measurement, adherence
and outcome intact — coupling them would discard good treatment evidence over an irrelevant defect.

### Standing correction

`shot_records.py:709-723` computes an **estimate** via `onset + onset_lag_ms`. A constant 36 does
not establish a measured conversion, and reader capture acceptance and native ownership acceptance
are different events. **"reader 544 = native 580" is unverified and must not be used.** Verified
directly: logged native slow = 40 shots, reader onset ≥ 544 = 68, disagreeing on 30 of 203 (14.8 %);
the best reader cut is ~558 and even that is inexact because the buckets' reader-onset ranges
overlap. No reader cut reproduces the native bucket.

---

## 3. Records

### 3.1 `experiment_manifest` — emitted before assignment is enabled

`experiment_id`, `protocol_version`, `build_hash`, `schema_hash`, `config_revision`, configuration
snapshot/hash, arm values, assignment probabilities, randomisation algorithm + seed (or frozen
schedule hash), `max_abs_offset_us`, eligibility definition, stopping rule, `learning_freeze_mask`,
initial learning-state revision, capture-phase-lock settings, banner-trim settings, outcome
grace/timeout settings, clock tolerances.

**The freeze must cover banner trim and oracle adaptation**, not only the learning paths the
development hook already fences.

### 3.2 `shot_press` — when the native physical edge is committed

Physical-shot identity, `press_engine_us`, the exact `press_epoch_us` sent to the sidecar,
`press_source`, initial shot type, and the native-owner identity propagated to and echoed by the
Python shot record.

**A Python message-receipt timestamp must not substitute for the physical edge.**

### 3.3 `native_onset` — when native ownership/onset is actually latched

Instrument **both** ordinary fresh acceptance and the promoted ownership-seed path.

Physical-shot identity, `attempt`, `first_fresh_accept_engine_us`, `first_meter_seen_engine_us`,
`native_onset_us`, `valid`, `ownership_source` (ordinary fresh accept vs promoted seed), original
frame provenance (`native_session`, `processed_seq`, source reference), and an
ownership-generation/reference sufficient to identify the accepted episode.

**Record the original seed provenance, not the latest frame at promotion time.**

**Onset OCCURRENCE is not when eligibility became KNOWN.** A promoted seed's
`first_meter_seen_engine_us` is *backdated* -- it names when the meter was seen, not when the engine
accepted it -- so it can legitimately precede its own acceptance. Ordering is therefore checked only
on `latched_engine_us`, and the required sequence is:

    eligibility known  ->  assignment committed  ->  first treatment-bearing operation
    latched_engine_us  <=  eligibility_fixed_engine_us  <=  assigned_engine_us  <=  plan now_engine_us

A later attempt may carry a later `native_onset` record without invalidating the original,
explicitly referenced eligibility snapshot.

Every decision/plan that consumes onset must reference this record and carry: actual shot
type/range, `computed_tempo`, `effective_tempo` after configuration switches, the **resolved trim
key** including fallback resolution, `banner_trim_us`, effective lead, `config_revision`.

> The resolved key matters because `BannerLeadTrim::trimMsForType()` uses `resolvedKey(...)` — the
> requested tempo is not necessarily the storage key that supplied the value.

### 3.4 `reader_onset` — on a successful `ShotRecordWriter.note_onset`

Python shot-record identity **plus an explicit native physical-shot reference**, the exact carried
press epoch, the accepted sample/capture epoch, `reader_onset_us`, reader-processing monotonic
timestamp, `onset_clock`, `structure_verified`, exact frame / source-generation / processed-sequence
provenance, acceptance-definition version.

Emit only when `note_onset` accepts the first observation. **Do not reconstruct from a later
`native_frame` row and do not substitute `onset_engine_estimate_ms`.**

### 3.5 `probe_assignment` — after eligibility is fixed, before any displaced arm

Physical-shot identity, `assignment_id`, manifest reference, the referenced **pre-treatment** native
onset and effective bucket, `eligible` plus an explicit eligibility/rejection reason, `stratum_id`,
`block_id`, position in block, `arm_id`, assignment probability, `assigned_delta_us`, assignment
time in the engine clock.

**Emit zero-offset assignments too, and emit eligibility/accounting records for excluded shots.**

**SIGN CONVENTION:** positive `assigned_delta_us` = **later** command. Positive banner trim =
**earlier** command. They have opposite signs.

### 3.6 `schedule_plan` — inside `AutomationEngine::scheduleFire`

The hook near `17258-17287` is followed by capture-phase locking, and it can clip an offset that
targets the past. **`DEV FIRE OFFSET APPLIED` is therefore not sufficient.**

For **every** arm / re-arm / retarget operation: physical-shot and assignment identity, `attempt`,
`plan_id`, `operation_id`, `decision_id`, `now_engine_us`, `baseline_pre_offset_deadline_us`,
`assigned_delta_us`, `post_offset_deadline_us`, **past-deadline clipping status**,
`post_phase_lock_deadline_us`, `final_requested_deadline_us`, authority/lease identity and its
deadline, transformation/configuration revision, disposition.

Where a downstream transformation is non-linear, also record a **pure, same-input zero-offset shadow
deadline**, or enough immutable inputs to reproduce it exactly offline.

> That shadow is a counterfactual for **this scheduling transformation only** — not proof of the
> trajectory the shot would have followed untreated. It must never be obtained by re-running
> mutating controller logic.

### 3.7 `worker` — extended, at mailbox operations, claim and dispatch

Add `operation_id`, `plan_id`, requested deadline, accepted deadline **only on acceptance**,
accepted `target_revision` on successful arm/retarget; at claim, the copied revision and its actual
effective deadline; at dispatch, the same copied identifiers plus existing `command_us`,
`complete_us`, route, delivery stage, result.

Return the operation identifier **on refusals too** — a refused retarget leaves the previously
accepted revision authoritative. Copy identifiers from the immutable operation / claimed target,
never from mutable current shot state. Add explicit cancellation / abandonment terminal records so a
claim without a dispatch stays explainable.

### 3.8 `release_link`, `shot_verdict`, `shot_terminal`

**`release_link`**, at actual release reporting: physical epoch, attempt, assignment, worker
token/route generation/revision/operation, `native_release_seq`, exact local command timestamp,
reported release epoch timestamp, delivery mode/result — distinguishing scheduled worker execution
from any other path.

**`shot_verdict`**, when the reader accepts and attributes the banner: Python shot-record identity,
native physical-shot reference, `verdict_seq`, an EARLY/EXCELLENT/LATE/UNKNOWN enum, attribution
status, coverage status (**absent vs unreadable, distinctly**), banner observation/frame provenance,
observation and emission timestamps.

**`shot_terminal`**, when the record closes: assignment identity;
released / cancelled / killed / unanswered / ungraded status; existing close reason; terminal time;
explicit treatment-adherence status.

**A missing grade is not EXCELLENT.** Cancelled or clipped assignments stay in the experimental
accounting.

---

## 4. Consumer acceptance rules

Produce **four shot-level conclusions plus one capture-level gate result**. Never collapse them
into a single "valid shot" filter:

| verdict | question |
|---|---|
| MEASUREMENT | is the required evidence present, consistent and correctly ordered **for this shot's lifecycle**? |
| ADHERENCE | did the **executed** plan carry the assigned scheduling displacement? |
| OUTCOME | is there an attributed verdict? |
| CLOCK | can reader-side and native-side timestamps be compared? |
| **GATE** | is the **capture** ready to support an experiment at all? |

### Scheduling displacement is NOT delivered-command displacement

`final_requested_deadline - zero_offset_shadow_deadline` measures a **scheduling transformation**.
To associate it with execution you must additionally prove the **dispatched revision accepted that
plan's deadline** -- which is what the worker lineage is for. Command **tardiness**
(`command_us - the dispatched revision's accepted deadline`) is a *separate* quantity and is
reported separately.

**Neither supplies a counterfactual command time for an untreated shot.** Any phrasing like "did the
assigned displacement reach the command?" is too strong and must not be used.

### The 2000 us threshold has exactly one job

It is a declared **scheduling-displacement fidelity threshold**. It is **not** a clock tolerance,
and **not** a requirement that command tardiness stay under 2 ms. A large, accurately measured
tardiness is a **finding**, not a measurement failure. The clock tolerance is a separate manifest
field (`clock_tolerance_us`).

### Lifecycle-dependent completeness

Required records depend on how the shot ended:

| terminal state | owes |
|---|---|
| `released` | press, onset, plan, an accepted-revision worker chain, release link, verdict |
| `killed` | the same execution evidence -- **an already-issued command is not erased by a later cancellation label** |
| `cancelled` before arming | no dispatch; a claim without a dispatch is explained by the cancellation |
| `unanswered` / `ungraded` | remain in the accounting; absence of a grade is reported, not inferred |

### Observed contradiction is not missing evidence

- A **missing** shadow on the executed plan is **UNVERIFIABLE**, not NONADHERENT -- and certainly
  not NONADHERENT merely because some *other* plan had one.
- Only the **executed** plan is judged. A rejected or superseded plan's violation is a separately
  identified diagnostic, not evidence about the treatment that actually ran.
- A contradictory lineage **withholds every derived value**, including tardiness. Computing a number
  from a deadline the consumer has just called contradictory is worse than reporting nothing.

### Withhold a complete measurement or gain claim when

- a required identity is missing, ambiguous or contradicted;
- a join would need nearest-time matching, a token alone, a revision number alone, or interchangeable
  `release_seq` meanings;
- assignment changes within a physical shot;
- eligibility or onset was reconstructed **after** treatment;
- the claimed or dispatched revision has no explicit accepted-deadline lineage;
- requested displacement is substituted for effective displacement;
- clock conversion is missing, crosses a discontinuity, or exceeds its declared uncertainty;
- a required trace is lossy or incomplete;
- configuration or supposedly frozen learning state changes without an accounted transition;
- outcome attribution is unresolved.

**Keep those rows and their reasons.** Non-adherent assignments are reported **with their arm, dose,
block and stratum** -- intention-to-treat accounting needs the label, and silently excluding them
would bias the estimate this consumer exists to protect. Report missing outcomes by arm with
sensitivity bounds.

### The capture-level gate

Three shot verdicts do not establish experiment readiness. The gate is **WITHHELD** on a missing or
contradictory manifest, a lossy capture, no identified shots, records with no usable identity, no
shot reaching COMPLETE, or no shot under experimental accounting. **Every row is preserved; only
readiness is withheld.**

`main()` exits **0 for GATE PASSED and 1 for withheld**. Zero must not mean "the report rendered".

### Derived measurements the consumer may compute

| quantity | definition |
|---|---|
| native onset | first native meter time - native physical press |
| command after press | local command - native physical press |
| command after native onset | local command - first native meter time |
| scheduling tardiness | local command - the **dispatched revision's** accepted deadline |
| scheduling displacement | final requested deadline - the **same plan's** zero-offset shadow |

All five are **engine-clock only** and need no conversion, so a missing clock bridge does not block
them. **None implies console or game receipt.**

## 5. The gate, before the main experiment

1. **Fixtures**: reused epochs across processes; attempt changes within one shot; zero-offset
   assignment; refused retarget; multiple accepted revisions; cancel-after-claim; phase-lock /
   clipping changes; reader and native first frames differing; clock discontinuity; duplicate and
   missing verdict; lossy footer; unknown resolved bucket.
2. **End-to-end zero-offset capture**: at least **20 consecutive eligible shots across two capture
   lifecycles**, every assignment accounted for, every delivered shot linked through its actual
   revision, ungraded shots explicitly ungraded.
3. **The small non-zero pilot**: assignment produces a measurable, **correctly signed** scheduling
   displacement, and every clipping/refusal case is accounted for.
4. **Observer perturbation check**: bounded producer behaviour, and an observer-on/off timing
   comparison. **Instrumentation is not assumed free.**

These are engineering smoke-test thresholds, **not statistical validation**.

A large measured scheduling delay is a **finding**, not automatically a measurement failure.
Precise-looking numbers with an unresolved clock or identity join **fail** the gate.

The observer stays observational: journal loss must never become a new release-authority dependency.
A failed audit stops progression to the main probe; the existing controller keeps ownership of
cancellation and kill behaviour.
