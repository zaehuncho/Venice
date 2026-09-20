# Square path audit — 2026-09-19

**Scope.** Every physical Square (shoot button) press in `logs/orion_native.log.1`
(2026-09-18T00:14Z → 18:29Z) and `logs/orion_native.log` (2026-09-18T18:29Z →
2026-09-19T02:48Z): 12 app sessions, **582 Square presses**, 26 h of wall clock.

**Method.** Every claim below is a count over those two files or a line in the tree.
The parser is checked in as `tests/test_square_path_press_census.py`; running it with
`-s` reprints the census. Nothing here is inferred from a mechanism the logs do not
show. Where a finding contradicts a previous diagnosis, the contradiction is stated.

> **Line anchors:** `HEAD e104e88` plus the 2026-09-19 working tree. `AutomationEngine.cpp`,
> `OrionAppController.cpp` and `ShotIntentPolicy.h` are under concurrent edit; every anchor is
> paired with the symbol or quoted text, and the text is authoritative, not the number.

**Headline.** The Square path does **not** lose presses. Every one of the 582 is
accounted for, every one reached the route layer, and the down/up pairing never
inverted. What the audit found is one **proven merge** (two presses reaching the game
as one hold), one **structurally dead safety net** (the meter backstop has fired zero
times and cannot fire in the shipped config), and one **lying diagnostic field** that
would mislead the next diagnosis of exactly that dead net.

---

## 1. The press census

| Terminal | log.1 | log | total | share |
|---|---:|---:|---:|---:|
| **owned by the engine** (`Release delivery identity: physical_epoch=N`) | 126 | 154 | **280** | 48.1 % |
| **unanswered, no meter** (`SHOT NOT OWNED: reason=press_unanswered_no_meter`) | 178 | 103 | **281** | 48.3 % |
| **undeliverable** (`PRESS UNDELIVERABLE`, no live input route) | 3 | 9 | **12** | 2.1 % |
| **ownership proof incomplete** (`SHOT NOT OWNED: reason=ownership_proof_incomplete`) | 6 | 3 | **9** | 1.5 % |
| **unaccounted (no decision of any kind)** | 0 | 0 | **0** | 0 % |
| **total square_edge presses** | 313 | 269 | **582** | |

`ownership_structure_stamp_missing` — the other fail-closed terminal
(`AutomationEngine.cpp:8440`) — fired **zero** times.

### 1.1 The 281 unanswered presses, classified by hold length

The meter cannot physically exist early in a press: on owned shots the reader's first
sight lands **500–850 ms** after the press (`simple_reader: PICKUP: …
first_sight_ms_after_press=`; e.g. 798.5 ms at 18:29:45.135Z, 517.0 ms at
04:21:23.650Z). A press shorter than that *cannot* be answered by a meter, no matter
what the engine does.

| hold | n | share of unanswered | reading |
|---|---:|---:|---|
| < 300 ms | **254** | 90.4 % | taps: pump fakes, defensive Square, menu presses. Correct pass-through. |
| 300–450 ms | 5 | 1.8 % | still below the earliest observed pickup. Borderline taps. |
| **≥ 450 ms** | **22** | 7.8 % | **the only presses that were plausibly real shot holds.** |

Distribution: n=281, min 48.7 ms, p10 75.4, **median 98.9 ms**, p90 278.0, max 1627.2.

**So the honest unanswered-shot rate is 22 / 582 = 3.8 % of Square presses, not 48 %.**
The 48 % figure is an artifact of counting taps as shots.

### 1.2 What happened on the 22 long holds

19 of the 22 carry `PICKUP: … first_sight_fill=-1.0 first_sight_ms_after_press=-1.0` —
**the reader never proposed a candidate at all for the whole hold.** There was nothing
on the screen to own. This corroborates the 2026-09-17 dump conclusion (no-meter
presses are late gathers or no gather) and refutes "the engine discarded a meter it
saw" for these 19.

The other 3 did see something and were still refused:

| when | hold | type | first sight | why it was refused |
|---|---:|---|---|---|
| 09-18T21:36:36.849Z | 1627 ms | Right Fade | fill 15.0 @ 800 ms | `lead_ready=0 backstop=no_lead` — first press of the session, latency not yet authoritative. Correct. |
| 09-18T23:30:58.062Z | 1361 ms | Standstill | **fill 90.0** @ 850 ms | first sight was already at 90 % fill; the ownership proof needs a rising run from a low fill, so it refused. **A real lost shot.** |
| 09-18T04:52:24.255Z | 582 ms | Standstill | fill 46.0 @ **67 ms** | first sight 67 ms after the press at 46 % — the previous shot's meter (a ghost). Refusal is correct. |

### 1.3 Sprint / R2 — the historical mechanism does not reproduce

`sprint_release_on_square = False` in `settings.json`, and **`sprint_released=1` appears
on 0 of 582 press lines** — the fence holds, the mechanism never engaged. Verified, not
assumed.

The `square_press_r2_hold_ms = 50` hold is live and bounded: the only R2 divergences in
26 h are **4 episodes of `OUTPUT DIVERGENCE: field=r2 out=255 phys=0`, max `held_ms=50`**
(`OrionAppController.cpp:11752`). Exactly the configured ceiling, never above it.

Against the "R2 release coincident with the press kills the shot" hypothesis:

| population | n | owned |
|---|---:|---:|
| all deliverable presses | 570 | 280 (49.1 %) |
| presses with an R2 release edge inside the trace window | **38** (6.5 %) | **24 (63.2 %)** |

Presses that *did* carry an R2 release edge were **more** likely to be owned, not less.
Of the 14 unanswered ones, 12 had holds of 86–103 ms (taps). Only 5 of the 38 edges
landed at or before the press (`-13.1, -9.4, -8.3, -5.8, -4.1, 0.0, 0.0` ms).
**In this corpus the R2 mechanism is not producing dead presses.** That is consistent
with `square_press_r2_hold_ms` working; it is *not* a reason to re-open
`sprint_release_on_square`.

Sprint state also does not explain the 22 long holds: 7 of the 19 never-seen ones had
`r2 >= 200` (37 %) against a 39 % baseline on owned presses. **No correlation.**

---

## 2. Was any press swallowed by the app rather than the game?

### 2.1 The route layer never dropped one

* **582 / 582** presses produced a `Square-down delivery identity` naming their own epoch.
* **569** carried `square_bit=1 delivery_stage=local_udp_accepted local_route_ack=1`.
* **12** carried `square_bit=-1 … reason=virtual_disconnected` — these are exactly the
  12 `PRESS UNDELIVERABLE` presses (session Disconnected, `pipe_connected=0`). Loudly
  reported, not swallowed.
* **1** carried `square_bit=0` while the press was real (below).
* Pipe ACK wait: n=582, median **353.5 µs**, p90 599 µs, max **1833 µs** — never within
  an order of magnitude of the 25 ms / 50 ms ACK budgets (`OrionInputClient.cpp:80`, `:83`).
* Input-hook write failures across the whole corpus: `failures` reached 1 and
  `ack_failures` reached 3 on a handful of heartbeats out of 14 649 — no press-time loss.
* `SHOT-GATE ARM RECEIPT`: 452 receipts, **0** `epoch != effective_epoch` mismatches.

### 2.2 The one press the app really did swallow — **PROVEN**

`logs/orion_native.log.1:14947-14962 (grep `epoch=187`)`, 2026-09-18T03:54:13Z, physical epoch 187.

```
13.224Z  Physical shot epoch: epoch=187 intent=square_edge … r2=255
13.226Z  Square-down delivery identity: physical_epoch=187 … square_bit=1 … local_route_ack=1
13.337Z  PRESS ANALOG TRACE: epoch=187 r2=[255,…,-]            <- trace #1, flushed EARLY
13.337Z  Square-up route audit: epoch=187 phase=raw_up  physical_square=0 requested_square=1 pipe_snapshot_square=1
13.513Z  PRESS ANALOG TRACE: epoch=187 r2=[255,…,-]            <- trace #2, 7 filled slots
13.513Z  Square-up route audit: epoch=187 phase=raw_up  physical_square=0 requested_square=1 pipe_snapshot_square=1
13.520Z  SHOT NOT OWNED: reason=press_unanswered_no_meter physical_epoch=187 hold_ms=295.1
13.521Z  Square-up route audit: epoch=187 phase=debounced_up physical_square=0 requested_square=0
```

Read it as a mechanism:

1. The player released at **13.337Z** (`physical_square=0`), 113 ms into the press.
2. A **second** `PRESS ANALOG TRACE` for the *same* epoch appears at 13.513Z with seven
   filled ladder slots. The trace can only be reopened by a physical Square-down edge
   (`AutomationEngine.cpp:4755` `if (squareDownEdge) { … padTracePressEpoch_ =
   physicalShotEpoch_; }`) and it stamps the *current* epoch — which was still 187.
   **A second real press, roughly 160 ms long, got no epoch of its own.**
3. Because no epoch was minted, no `shot_gate_arm` was sent and the engine's
   `squareHoldStartMs_` still pointed at the *first* press — the reported `hold_ms=295.1`
   is the span of two presses, not one.
4. `requested_square=1 pipe_snapshot_square=1` on **both** raw_ups: the console was told
   Square-DOWN continuously from 13.224Z to 13.521Z. **The game saw one 297 ms hold where
   the player made two presses.**

**Root cause, at the line.** `orion::ShotIntentEdgeTracker::updateSquareGesture`,
`native_orion/src/ShotIntentPolicy.h:277-313`. A new epoch requires three consecutive
UP polls (`kReleaseSamples = 3`, `:127`). On any `active` poll the function does
`squareInactiveSamples_ = 0` unconditionally (`:285`) and parks the previous run in
`squareReboundInactiveSamples_`. That rebound run is spendable **only** through
`deliveredSquareReleasePending_` (`:308-312`), which is set by
`noteOwnedSquareReleaseDelivered()` — i.e. **only after an OWNED release was delivered**.
Epoch 187 was never owned, so the rebound could never retire the latch: one spurious or
genuine held poll inside the debounce reset the counter to zero, `squareLatched_` stayed
true, and the mirrored output kept Square down for 184 ms after the physical release.

**Rate: 1 in 582 presses (0.17 %).** The second repeated-`raw_up` in the corpus
(epoch 251, 04:21:24.353/24.366Z) is the benign form — `requested_square=0` already, the
up had been delivered, the bounce cost nothing.

This is the only mechanism in these logs that reproduces the owner's phenotype
("square button is stuck, next press does nothing"). It is real, it is rare, and its
root cause is a single reachable branch.

### 2.3 The one press delivered with the Square bit clear

`logs/orion_native.log.1:341`, 00:16:54.975Z, epoch 46:
`Square-down delivery identity: physical_epoch=46 … square_bit=0` alongside
`IDLE-GATE: reason=stabilizing_tempo_movement_intent` and
`SQUARE SUPPRESSED: gate=engine:Idle/stabilizing_tempo_movement_intent`.

This is the **Tempo remap** path deliberately withholding the Square bit while it
stabilises the movement intent and waits for `waiting_for_tempo_movement_commit_ack`
(8 ms later, at 00:16:54.983Z). It is by design and it is instrumented. It is worth
knowing that Tempo mode shifts the console's view of the press start by ~8–10 ms; it is
**not** a swallow. 1 occurrence in 582.

---

## 3. Can the down/up pairing invert or leak?

**No inversion.** Over 582 presses: `raw_up` 584, `debounced_up` 582, and the phases
alternate except for the two repeated `raw_up`s in §2.2. A `debounced_up` never
preceded its `raw_up`, and no press ended without a `debounced_up`.

**Bounded leak, by design.** `SquareUpAuditTracker` (`PressedOverlayPolicy.h:53-75`)
reports `RawUp` on the 1st up poll and `DebouncedUp` on the 3rd. Between them the engine
still mirrors Square down — 289 of the audits show `phase=raw_up … requested_square=1
pipe_snapshot_square=1`. Measured gap:

```
raw_up -> debounced_up : n=582  min 4 ms  median 8 ms  p90 10 ms  max 47 ms
                         >20 ms: 2    >50 ms: 0    >100 ms: 0
```

So an *unowned* press holds the console's Square bit ~8 ms past the player's release.
That is the debounce, it is bounded, and at 2K's 16.7 ms frame it is under one frame in
the median case. The 47 ms outlier and the 184 ms merge are the same mechanism reaching
further than intended.

**The owned-shot direction is clean.** 281 `Release ownership` summaries:
`phys_held_all=1 out_cleared_all=1 max_out_sq=0` on 280 of them — the engine drove
Square-up to the console while the player still held the button, and never let the
output Square bit rise again during the release window. The single `phys_held_all=0` case
(04:31:38.476Z, seq 33) is a **Go-To stick shot**, where no Square was held at all — not
a Square-path defect.

`SQUARE SUPPRESSED` accounting matches exactly: 280 `Releasing/release_scheduled`,
280 `Cooldown/release_scheduled`, 280 `Idle/Idle`, 281 `Idle/waiting_for_button_release`
— one per owned shot per state, no strays.

---

## 4. Races between the press epoch, the shot gate and the release

### 4.1 Refuted — no epoch race

* `shot_gate_arm send: epoch=N` → `SHOT-GATE ARM RECEIPT: epoch=N effective_epoch=N`:
  **452 receipts, 0 mismatches.** The sidecar never armed on a stale epoch.
* `Release delivery identity: physical_epoch=N` never named an epoch with no press in the
  same session (the one apparent orphan, `log.1:775`, is a Go-To stick release).
* `SHOT NOT OWNED` and `Release delivery identity` never both fired for one epoch
  (invariant I1 in the test; 0 doubled terminals).
* `RAPID SQUARE REARM` — the two-poll fast path (`ShotIntentPolicy.h:133`) — fired **0
  times**, so it contributed nothing here either way.

### 4.2 Proven — the backstop is unreachable, and the abort line hides it

`AutomationEngine::maybeFireMeterBlindBackstop` arms a deadline on every meter-path
Square press and then, at `native_orion/src/AutomationEngine.cpp:21401`:

```cpp
    if (config_.greenWindowPriority) {
        if (now >= meterBackstop_.deadlineMs && meterBackstop_.missLoggedDeadlineMs < 0.0) {
            meterBackstop_.missLoggedDeadlineMs = meterBackstop_.deadlineMs;
            emit engineDiagnostic(QStringLiteral(
                "METER VISION WAIT: epoch=%1 type=%2 reason=no_owned_tip "
                "blind_release_suppressed=1 output=physical_or_configured_gather") …);
        }
        return false;
    }
```

`settings.json` ships `green_window_priority = true` **and** `meter_blind_backstop = true`.
The consequence, measured:

* **`METER BACKSTOP: fired` — 0 occurrences in 582 presses / 26 h.** The only
  `METER BACKSTOP` lines in the corpus are 8 `reclassified Standstill -> …` type upgrades.
* **`METER VISION WAIT … blind_release_suppressed=1` — 26 occurrences** (11 + 15).
  13 of them belong to the 22 long-hold presses of §1.2.

So the net that exists specifically to answer a meterless press has never caught one.
`meter_backstop_grace_ms = 100`, `meter_backstop_grace_fade_ms = 220` and
`meter_backstop_never_seen_probe_ms = 0` are all tuning a code path that cannot execute.

**And the abort line says the opposite.** `meterBlindBackstopBlockReason`
(`AutomationEngine.cpp:20937-20918`) tests exactly four things — `meterBlindBackstop`,
`autonomousLiveMeterTimingEnabled()`, `blindReleaseTypeAllowed()`,
`measuredLeadAuthoritative()` — and **does not test `config_.greenWindowPriority`.** An
empty reason is then printed as the word `user_released` (`AutomationEngine.cpp:8481`).
Result: **280 of the 281** unanswered presses report `backstop=user_released`, including
presses that held **1361 ms** and **1039 ms** and whose own `METER VISION WAIT` line, one
tick earlier, proves the deadline passed and was suppressed:

```
22:45:12.846Z  METER VISION WAIT: epoch=4 type=Standstill reason=no_owned_tip blind_release_suppressed=1 …
22:45:13.133Z  SHOT NOT OWNED: reason=press_unanswered_no_meter physical_epoch=4 hold_ms=1039.4 … backstop=user_released
```

Two lines, same epoch, 287 ms apart, flatly contradicting each other. This is a
diagnostic-integrity defect, not a gameplay defect — but it is precisely the field the
next "why didn't it shoot?" investigation will read first, and it currently says
"the player let go", which is false.

---

## 5. Ranked fixes

Ranked by **evidence strength first**, then by cost. Every patch below touches
`AutomationEngine.{h,cpp}` or `ShotIntentPolicy.h`; per the coordination rules for this
task they are written here as snippets, not applied.

---

### FIX 1 — make `backstop=` tell the truth (evidence: 13 contradicted presses; risk: none)

**Why first.** It changes no behaviour, it costs one comparison, and until it lands every
number about the backstop in a returned log is unreliable. Nothing else in this list can
be validated from logs while this field lies.

**File:** `native_orion/src/AutomationEngine.cpp` — **BLOCKED FILE, patch not applied.**

```diff
@@ native_orion/src/AutomationEngine.cpp:20937  QString AutomationEngine::meterBlindBackstopBlockReason(double now) const
 QString AutomationEngine::meterBlindBackstopBlockReason(double now) const
 {
     if (!config_.meterBlindBackstop) {
         return QStringLiteral("off");
     }
+    // [SQUARE PATH AUDIT 2026-09-19] THE SUPPRESSOR THAT ACTUALLY DECIDES. greenWindowPriority
+    // short-circuits maybeFireMeterBlindBackstop() at AutomationEngine.cpp:21401 and returns
+    // false before the deadline is ever consulted, so with it on the backstop is unreachable —
+    // 0 `METER BACKSTOP: fired` lines against 26 `METER VISION WAIT … blind_release_suppressed=1`
+    // in logs/orion_native.log{,.1} (582 presses, 2026-09-18/19). Without this clause the abort
+    // line below reported `user_released` for presses held 1039 ms and 1361 ms whose own
+    // METER VISION WAIT line proves the deadline had passed. The word must name the suppressor.
+    if (config_.greenWindowPriority) {
+        return QStringLiteral("green_window_priority");
+    }
     // The backstop belongs to the strict live meter path. NO METER mode and pose timing have
     // their own release clocks and must never grow a second one.
     if (!autonomousLiveMeterTimingEnabled()) {
         return QStringLiteral("not_live_meter");
     }
```

Order matters: `green_window_priority` must be reported **above** `no_lead`, because it
binds regardless of lead authority. Placing it directly after the `off` test keeps the
existing token order otherwise intact (append-only for every existing grep).

`tests/test_square_path_press_census.py::test_i5_backstop_token_does_not_contradict_the_vision_wait`
is checked in as an `xfail` against this exact defect; it turns green the moment the
patch lands, and can then be flipped to `strict=True`.

---

### FIX 2 — decide what `meter_blind_backstop` means (evidence: 0 fires in 582 presses; risk: medium if armed)

Right now two shipped settings disagree with each other and the UI implies a net that
does not exist. There are two honest resolutions; **this audit recommends 2a**, because
2b changes live release behaviour and the ship config (memory: `SHIP CONFIG 09-16`)
deliberately put vision in front.

**2a (recommended, zero gameplay risk).** Treat the backstop as retired under the shipped
config: after FIX 1, surface the suppression where the owner can see it and stop
presenting three tuning knobs for dead code. No engine change beyond FIX 1 — a one-line
status string plus removing the knobs from the settings surface. Evidence supporting
"retired is fine": on the 22 long holds, **19 had no meter candidate at all**, and on the
one session where blind releases were graded they went 3 EXCELLENT / 3 EARLY against
vision's 15 EXCELLENT / 8 EARLY / 0 LATE (the measurement already recorded in
`AutomationEngine.cpp:21325-21335`). A coin-flip release on a meterless press buys ~3 % of
presses a 50 % shot; it also re-introduces a release path with no vision evidence.

**2b (only with an owner-run drill).** Let the backstop fire on the *provably meterless*
press only — the narrow case `greenWindowPriority` was never aimed at. The engine already
computes the predicate.

**File:** `native_orion/src/AutomationEngine.cpp` — **BLOCKED FILE, patch not applied.**

```diff
@@ native_orion/src/AutomationEngine.cpp:21401  maybeFireMeterBlindBackstop()
-    if (config_.greenWindowPriority) {
+    // [SQUARE PATH AUDIT 2026-09-19] GREEN-WINDOW PRIORITY MEANS "VISION OWNS A PRESS VISION
+    // CAN SEE". It was never meant to cover the press where the reader proposed NOTHING for the
+    // whole hold: 19 of the 22 shot-length (>=450 ms) unanswered presses in the 2026-09-18/19
+    // corpus carry `PICKUP first_sight_fill=-1` — there was no candidate for vision to be given
+    // priority over. Keep the suppression for every press that saw something; let the bare-law
+    // deadline answer the press that saw nothing. `meterBackstopCandidateEverSeen()` is the same
+    // latch the NEVER_SEEN collapse already uses, so no new evidence is introduced here.
+    const bool everSeen = meterBackstopCandidateEverSeen(pendingSquarePhysicalEpoch_);
+    if (config_.greenWindowPriority && everSeen) {
         if (now >= meterBackstop_.deadlineMs && meterBackstop_.missLoggedDeadlineMs < 0.0) {
             meterBackstop_.missLoggedDeadlineMs = meterBackstop_.deadlineMs;
             emit engineDiagnostic(QStringLiteral(
                 "METER VISION WAIT: epoch=%1 type=%2 reason=no_owned_tip "
                 "blind_release_suppressed=1 output=physical_or_configured_gather")
                                       .arg(pendingSquarePhysicalEpoch_)
                                       .arg(meterBackstop_.shotType));
         }
         return false;
     }
```

If 2b is taken, FIX 1's reason token must become conditional on the same predicate, or
it will report `green_window_priority` for a press the backstop went on to answer.

**Validation gate before shipping 2b:** one owner drill, banner-graded, of at least 30
Square presses in which the backstop fires ≥ 5 times. Ship only if those blind releases
do not grade worse than the session's vision releases. Do **not** ship 2b on this audit
alone — the audit proves the net is dead, not that arming it is an improvement.

---

### FIX 3 — stop the merged press (evidence: 1 proven event in 582; risk: low, needs care)

**File:** `native_orion/src/ShotIntentPolicy.h` — not on the blocked list, but left
unapplied with the rest of this audit's changes so the whole set lands as one reviewed
batch.

The defect is that an UNOWNED press can never spend its rebound inactive run
(`ShotIntentPolicy.h:308-312` gates it on `deliveredSquareReleasePending_`, which only an
*owned* delivered release sets). Two candidate patches; **3a is the smaller claim.**

**3a — let an unowned press spend its rebound run.**

```diff
@@ native_orion/src/ShotIntentPolicy.h:277  updateSquareGesture()
     [[nodiscard]] bool updateSquareGesture(bool active) noexcept
     {
         if (active) {
             // Retain only the immediately preceding inactive run.  This lets a
             // delivery acknowledgement later in THIS controller tick recover a
             // two-report rapid release, while an old mid-hold dropout cannot be
             // spent at some unrelated future release.
             squareReboundInactiveSamples_ = squareInactiveSamples_;
             squareInactiveSamples_ = 0;
+            // [SQUARE PATH AUDIT 2026-09-19] THE MERGED PRESS. An UNOWNED press has no
+            // `noteOwnedSquareReleaseDelivered()` to arm deliveredSquareReleasePending_, so its
+            // rebound run was unspendable and one held poll inside the three-poll debounce reset
+            // the release outright. logs/orion_native.log.1:14947-14962 (grep `epoch=187`) (epoch 187, 03:54:13Z):
+            // the player released at +113 ms, pressed again for ~160 ms, got NO epoch, and the
+            // console was told Square-down continuously for 297 ms — two presses, one hold.
+            // An unowned latch grants no fire authority and owns no ShotContext, so retiring it
+            // on the same two-report evidence the delivered path already trusts cannot
+            // pre-empt a shot; it can only let the SECOND press mint its own epoch.
+            if (!unownedSquareLatchRetirable_
+                && squareLatched_
+                && squareReboundInactiveSamples_ >= kDeliveredSquareReleaseSamples) {
+                squareLatched_ = false;
+            }
             if (!squareLatched_) {
```

This needs a new caller-set bit — `unownedSquareLatchRetirable_`, cleared whenever the
engine owns the press (`AutomationEngine` leaves `HoldState::Idle`) and set otherwise —
wired from the same site that already calls `noteOwnedSquareReleaseDeliveredForEpoch`
(`OrionAppController.cpp`, near the `Release ownership` flush). Without that bit the
patch would weaken the debounce for owned shots too, which is the regression
`ShotIntentPolicy.h:116-121` exists to prevent.

**3b — cap the unowned output hold instead (blunter, strictly bounded).** Rather than
change edge minting, forbid the *output* from holding Square more than N polls past the
first `RawUp` while the engine is unowned. The invariant is easy to state and easy to
test: *an unowned press may not hold the console's Square bit for more than
`kReleaseSamples` polls after the physical release.* The measured need is 184 ms against a
median of 8 ms, so a 20 ms cap would have cost the corpus nothing and closed the merge.
This is the safer fix if 3a's ownership bit proves awkward to thread.

**Do not ship 3a and 3b together.** They both retire the same latch and would race.

---

### Not recommended (stated so they are not re-proposed)

* **Re-opening `sprint_release_on_square`.** §1.3: presses with an R2 release edge were
  owned at 63 % vs a 49 % baseline. The mechanism is not producing dead presses under
  `square_press_r2_hold_ms = 50`.
* **Loosening the ownership proof to catch the 23:30:58Z late pickup (fill 90 @ 850 ms).**
  n = 1. The proof's rising-run requirement is what keeps jerseys and the shot chart from
  becoming meters (memory: *Shape gate: false locks WERE the abort blocker*). One lost
  shot is not evidence to relax it.
* **Anything about `press_unanswered_no_meter` volume.** 90 % of those presses are taps
  under 300 ms. The terminal is correct; only the `backstop=` word on it is wrong.

---

## 6. What is now pinned in CI

`tests/test_square_path_press_census.py` runs against a checked-in fixture
(`tests/fixtures/square_path_census.logtxt`) and, when present, against the live
`logs/orion_native.log*`. It asserts:

| id | invariant | status on this corpus |
|---|---|---|
| I1 | every square press has exactly one terminal | **pass** (0 unaccounted, 0 doubled) |
| I2 | every square press reaches `Square-down delivery identity` | **pass** (582/582) |
| I3 | `raw_up → debounced_up` alternate; merged-press rate ≤ 1 % | **pass** (0.17 %) |
| I4 | at most one substantive analog trace per epoch; swallow rate ≤ 1 % | **pass** (0.17 %) |
| I5 | `backstop=user_released` never contradicts `METER VISION WAIT` | **xfail — FIX 1** |

Plus `test_detectors_fire_on_the_known_merged_press`, which replays the epoch-187 window
verbatim (`tests/fixtures/square_path_merged_press.logtxt`) and asserts both detectors fire
on it, so I3/I4 cannot rot into always-true.

Run: `python -m pytest tests/test_square_path_press_census.py -s --basetemp=D:/NexusVision/pytest_tmp/xbox`

---

## ADDENDUM 2026-09-19 (coordinator) — FIX 3a was attempted and REVERTED. Read this before retrying.

I implemented 3a exactly as specified (an `unownedSquareLatchRetirable_`-style bit fed from
`shot_.state != HoldState::Idle && != HoldState::Cooldown`, re-asserted each controller tick before
`ShotIntentEdgeTracker::update`, relaxing the latch retire to `kDeliveredSquareReleaseSamples`) and
then reverted it. **The ownership bit is false during exactly the window the debounce protects.**

`ShotIntentPolicy.h:116-121` states the debounce's purpose: a false-UP poll followed by a still-held
sample "minted a second physical-shot epoch, reset the reader, **and erased otherwise-valid ownership
proof**". Ownership proof ACCUMULATES BEFORE the engine owns the shot — that is the entire point of
the proof. So while proof is accumulating, `shot_.state` is still `Idle`, the proposed bit reads
"unowned", and 3a relaxes the debounce **precisely when a spurious epoch would destroy the evidence
the engine is in the middle of gathering**. That is the pathology the pickup-stall work
(`docs/PICKUP_STALL_2026-09-19.md`) just spent a day removing; 3a would reintroduce a second route to
it from the input side.

Concrete proof that the relaxation is load-bearing in the wrong direction: the existing, deliberate
regression `AutomationEngineTests.cpp:~30860` ("Ordinary input keeps the established three-UP
lifetime: two reports can never turn a still-held/noisy Square into another physical-shot epoch")
**fails** under 3a, because an unowned tracker now mints on the two-report rebound. That test is not
incidental coverage — it encodes the protection.

**Cost/benefit as measured:** the merged press is **1 proven event in 582 presses (0.17 %)**. The
regression risk is a spurious shot epoch during proof accumulation, whose measured cost when it
happens is a late or lost shot (see the pickup-stall census: `first_fill` p50 30.1 % vs 17.6 %).
Trading a 0.17 % bug for a new route into a 8.5 % one is not a good trade on this evidence.

**Therefore:** do not ship 3a as specified. Two paths remain, for the auditor/owner to choose:
1. **3b (cap the unowned OUTPUT hold).** It never touches edge minting or the debounce, so it cannot
   manufacture an epoch. Measured need: 184 ms against a median of 8 ms; a 20 ms cap would have cost
   the corpus nothing. This is now the RECOMMENDED path.
2. **A better-gated 3a**, if a predicate for "no ownership proof is currently accumulating" can be
   threaded (the engine's `pendingMeterOwnership*` state, not `HoldState`). That predicate does not
   exist today and designing it is not a drive-by change.

Verified after revert: in-place Release build clean, `ctest -C Release` **22/22 targets pass**.
