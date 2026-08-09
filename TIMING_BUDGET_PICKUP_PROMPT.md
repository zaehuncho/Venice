# Orion timing — pickup prompt (state as of 2026-08-05 late)

Read this whole file before touching anything. It carries a **counted live measurement** that
refuted the previous leading theory, so several things you may find in older docs/comments are now
known wrong. Demo is **2026-08-08**.

---

## 1. Where the bot actually is

First counted live batch (50 shots, one spot, one shot type, greens counted by the owner):

```
42 greens
+ 6 missed tip deadlines  -> bot never fired; the human's press passed through and landed LATE
+ 2 genuine bot earlies
= 50   (84% overall)
```

The arithmetic reconciles exactly with the owner's account: *"some lates (which the bot didn't
arm/aborted), a couple of earlies."*

**The bot is ~95% (42/44) on shots it actually drives. Timing quality is NOT the problem.**
**All six lates are missed deadlines.** That is the entire remaining loss.

Verified in-log: each `disposition=rejected_missed` is followed by no release for 3–6 seconds — the
next event is a fresh shot, not a recovery. The missed-deadline shot never fires.

### Do not re-litigate these (measured, this session)

- **Aim geometry / green-window centring is NOT the lever.** The engine aims at `kTipTargetPct=100.0`
  and the reader hardcodes `g_end = 100.0` (`simple_meter_reader.py:4253`), so the aim sits on the
  window's late edge on 130/130 releases. That model predicts a ~50% ceiling. **Measured 84%.** The
  release is already biased early of the tip. A per-shot centring feature was built
  (`RemapConfig::autonomousGreenCenterFrac`, default 0) and is **shelved unshipped** — the only
  genuine timing misses were *earlies*, so shifting earlier would worsen them.
- **`owned_square_detector_unresolved` / `owned_square_trajectory_unresolved` are not aborts.** They
  are per-tick hold states (`action=hold_output_no_blind_release`; 92 lines across 56 seconds). An
  earlier analysis counted log lines and called them 20 lost possessions. They are symptoms of the
  budget running out, not consumers of it.

---

## 2. The one enemy: a 93ms decision budget

```
anchor -> tip animation = 393 ms   (RemapConfig::tipPhaseConstantMs)
required lead           = 300 ms   (user-set; measuredLeadForActuationMs() REPLACES the posterior)
DECISION BUDGET         =  93 ms
```

From the meter crossing `tipPhaseAnchorPct = 30.0`, the engine has 93ms to witness the crossing,
satisfy the ownership proof, and schedule. Miss it and the deadline is already in the past.

Measured `TIP DEADLINE DECISION disposition=rejected_missed`:

```
src=phase                     tip_eta=286.4  lateness=13.6   fill=46.7  frame_age=11.9
src=sampler                   tip_eta=241.4  lateness=58.6   fill=23.1  frame_age=25.4
src=phase                     tip_eta=275.2  lateness=24.8   fill=47.1  frame_age=30.4
src=registration+sampler_far  tip_eta=176.0  lateness=124.0  fill=50.4  frame_age=41.4
src=phase                     tip_eta=269.2  lateness=30.8   fill=46.3  frame_age=31.0
```

`lateness = 300 − tip_eta` exactly. Decisions land at ~46–47% fill ≈ 90ms past the anchor at
0.185 pp/ms — right on the boundary, so jitter tips them over.

Frame age is what tips them:

| | n | mean frame age |
|---|---|---|
| Successful releases | 54 | **10.8ms** (median 9, p90 17) |
| Missed deadlines | 5 | **28.0ms** (min 11.9, max 41.4) |

Frame age is **already folded into `predictedTipMs`** via the capture-aligned clock (see the comment
at `AutomationEngine.cpp:6207`). It is not under-compensated — it genuinely consumes wall-clock
budget. `captureAgeLeadCapMs = 20.0` exists but its use at `AutomationEngine.cpp:6808` sits in
`processHolding()` **after** the early return to `processAutonomousLiveMeterHolding()` at
`:6562-6565`, i.e. dead in production. Do not tune it.

**Caveat: n=5 on missed deadlines.** Mechanism is deterministic, effect is large, but any predicted
win size rests on five events.

---

## 3. The two candidate fixes

### Candidate A — shrink what the budget must cover (IMPLEMENTED 2026-08-05, default OFF)

Reduce the strict ownership proof from 3 frames to 2. **Shipped behind
`ownership_proof_two_frame` (settings.json, default false)**, plumbed AppConfig →
RemapConfig → `AutomationEngine.cpp` (`strictRequiredSamples`). Two native tests pin both
directions (`ownershipProofTwoFrameFlagRecoversOneFrame` — the default-OFF leg fails if the
default ever silently flips — and `ownershipProofTwoFrameStillRefusesStaticLookalike`).
Awaiting its counted 50-shot batch before any default flip.

**OFFLINE-VALIDATED 2026-08-05** (`tools/timing/validate_ownership_proof.py`, replaying the
engine's episode state machine over 369 real shot episodes in `detframes_20260804/05_*.csv`;
16 files, ~40k intra-episode frames). The original arithmetic above was HALF right and the
suggested compensation was WRONG:

- Cadence confirmed 60fps (median intra-episode gap 16.7ms), so the 3rd frame costs ~17ms.
- **But frame 2 is only conditionally dead**: the rise proof (`anchorRiseMinPct` 3.0pp) must
  ALSO pass at sample 2, and the median 1-frame rise is **2.81pp — the rise gate, not the
  sample count, binds the median shot.** 2f/3.0pp grants at sample 2 on 42% of all shots
  (58% of budget-critical shots first-seen at 30–40 fill, where the meter runs faster per
  frame). Measured saving on budget-critical shots: **median 13.8ms, mean 10.0ms** — not a
  flat 17ms.
- **DO NOT pair with `anchorRiseMinPct` 3.0 → 4.0.** Measured: it collapses the grant-at-2
  rate to 19–33% and the median saving to **0ms** — it silently re-creates the 3rd-frame
  wait — while buying nothing on false locks (3 vs 4 admissions, both ≈ baseline). The
  compensation defeats the fix.
- **False-lock cost of dropping the 3rd frame: zero measured.** The 2-frame and 3-frame
  proofs admit the IDENTICAL episode set (4 non-shot grants each, all meter-shaped 16–22px ×
  108–116px conf-1.00 truncated real rises, no décor). The 3rd frame added no discrimination
  anywhere in ~5h of frames. Caveat: offline replay cannot model press/epoch gating (which
  only removes admissions) or menu-context lookalikes that never occur mid-gameplay-capture.
- Implication for the lates: 13.8ms median recovery rescues the 13.6ms lateness, is
  borderline for 24.8/30.8, and does nothing for 58.6/124. **Candidate A alone probably
  converts ~1–2 of the 6 lates; expect to still need Candidate B** (with its 58.3ms shift
  and ladder-count blocker) if the counted batch confirms.

### Candidate B — widen the budget (IMPLEMENTED 2026-08-05, default OFF)

**Shipped behind `tip_phase_anchor_base20` (settings.json, default false).** ONE flag moves the
whole constellation on a regime transition in `applyConfig()` (AutomationEngine.cpp,
[ORION_ANCHOR_BASE20]): anchor 20, constant 451.3, seed 377.3, learn band 298.3/488.3, ladder
count 4. Dating switches to MEASURED per-rung secant offsets {25: 29.42, 30: 58.30, 35: 86.22,
40: 112.58} — the single −5.235 slope would date rung 40 ~7.9ms LATE from base 20 (the meter
accelerates with fill; no single constant fits). `learning.json` stays **canonically base-30**
(translated +58.3 on restore / −58.3 on persist via `phasePriorShiftMs()`), so flipping the flag
in either direction can never corrupt the learned prior. Three native tests pin the
constellation coupling, the rung offsets, and the canonical-storage round-trip. The flag-OFF
path is bit-identical (applyConfig touches nothing without a transition).

Original analysis (still the evidence base):

Move `tipPhaseAnchorPct` 30 → 20, buying **54ms** (93 → ~147ms).

**The slope has been measured — do not use the shipped constant.** n=21 shots with a witnessed 30%
crossing, from recorded per-frame fill (`logs/diagnostics/detframes*.csv`). Method:
`slope(L→30) = −(t_30 − t_L)/(30−L)`, no tip estimate needed.

```
level 20:  −5.830 ms/pp   (rSD 0.44)
level 25:  −5.776         (rSD 0.45)
level 35:  −5.583         (rSD 0.56)
level 40:  −5.428         (rSD 0.33)
```

The relationship is smooth across 20–40 (extrapolation is fine), but the shipped
`tipPhaseConstantSlopeMsPerPct = −5.235` is wrong at the low end. **The true 20→30 slope is −5.830,
so the correct shift is 58.3ms, not 52.35ms.** Using the shipped constant would leave the bot firing
**~6ms early on every shot**. The shipped slope is also mildly wrong at the existing ladder rungs
(35, 40), costing ~1.7ms / ~1.9ms on No-Dip today.

Four values must move together by **58.3ms**:

```
tipPhaseConstantMs         393.0  -> 451.3
tipPhaseSeedPhysicalMs     319.0  -> 377.3
learner accept band   [240, 430]  -> [298, 488]
learning.json learned_phase_physical_ms (currently ~318.6, DRIFTS) -> rescaled
```

**BLOCKER — ladder coverage.** With base 20 and `tipPhaseAnchorLadderCount = 2`,
`tipPhaseAnchorLadderStepPct = 5.0`, the ladder becomes `{25, 30}`. No-Dip shots are first seen at
32.4–37.3% fill, so they would miss **every** rung and fall off the phase ladder entirely onto the
biased sampler chain (+61–92ms bias) that the phase member exists to replace. **If you do Candidate
B you must also raise `tipPhaseAnchorLadderCount` to 4** so base 20 yields `{25, 30, 35, 40}`.

Also verified: `anchorMaxFirstFillPct = 40.0` is a separate constant and does **not** move with the
phase anchor — no interaction there.

**Per-shot variance floor:** rSD 0.44 ms/pp at level 20 → the shift itself varies ±4.4ms shot to
shot over a 10pp move. A single constant is only right on average.

---

## 4. Uncommitted work in the tree (nothing committed this session)

Branch `fix/timing-input-and-remoteplay-blockers`, last commit `1dbc86ba`.
**445 native tests + 67 python tests pass.** Relevant modified files:

- `latency_estimator.py` — probe collector now gathers inside the invertible band
  (`_probe_fill_floor()`, `fmin + 2`); all four silent drop paths in `_close_probe()` now log
  `probe DROPPED: reason=…`; per-frame probe trace decimated out to ~3.5s.
- `native_orion/src/RemotePlaySession.cpp` — throttle bypass for `probe DROPPED` and for the
  authority-death lines (`warm timing revoked`, `Latency authority reset`, `capture_route_mismatch`).
- `native_orion/src/AutomationEngine.{h,cpp}` — green-centring feature (default OFF, shelved);
  `tipPhaseConstantMs` **reverted 392 → 393** (two prior adjustments were made against refuted
  fill-derived metrics; 393 is what the tests encode and derive).
- `native_orion/src/AppConfig.{h,cpp}` — `autonomous_green_center_frac` / `_max_ms` plumbing.
- `native_orion/tests/AutomationEngineTests.cpp` — probe test now pins its own probe timings instead
  of inheriting shipped defaults.
- `tests/test_latency_estimator.py` — regression test for the sub-`fmin` probe collector bug.
- `run_orion.local.ps1` — **reaping a python sidecar now arms the 2.5s capture-driver settle wait.**
- **2026-08-05 (this session):** `AppConfig.{h,cpp}`, `AutomationEngine.{h,cpp}` — Candidate A
  `ownership_proof_two_frame` flag (default OFF, see §3); `AutomationEngineTests.cpp` — two
  [ORION_DECISION_BUDGET] tests; `tools/timing/validate_ownership_proof.py` — the offline
  validator (run it before touching `requiredSamples` or `anchorRiseMinPct`). Verified:
  445 native + 1330 python tests green. NOTE: `run_gates.py` without `--sessions` fails
  honestly — its default July framedumps are gone from disk; pass the on-disk Aug-04
  sessions explicitly. Against those, 27/30 detection gates pass; the 3 failures are
  pre-existing reader-side/floor-drift items (two dumps below the n_shots>=2 floor, and
  offshot_falselock_episodes=2 in session_20260804_202151), untouched by the engine change.

---

## 5. Launch and verification procedure (follow exactly)

**Always launch via the launcher, never `OrionNative.exe` directly.** A direct launch omits ~19 env
keys including every reader flag (`ORION_READER_TRACK_H_CAP`, `TRACK_H_ROBUST_GATE`, `ANCHOR`,
`PCTL_FILL`, `FAKELOCK_BREAK`, `METER_TRACK`, `LOC_ROI`, `GOTO_METER_WAIT`) and the pinned sidecar
interpreter, and it does not elevate (WinDivert needs admin or the packet bridge dies).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File run_orion.local.ps1
# accept the UAC prompt
```

**Before counting any batch, verify the engine is actually alive** — a dead session produces zero
releases and reads as 50 catastrophic misses:

```
capture_route_mismatch      -> must be 0
route_scope_rejected        -> must be 0
"acknowledged directly by sidecar" -> must be >= 1
waiting_for_latency_calibration    -> must be 0
"Release issued"            -> must track your press count
```

**Known trap:** a single `capture_route_mismatch` sets `_capture_warm_cache_revoked` **one-way for
the sidecar's life**; every route proof then returns `route_scope_rejected`, the engine never gains
measured-lead authority, and every press falls through. One session logged 73 rejects / 14
`waiting_for_latency_calibration` / **zero releases**. Root cause was the launcher reaping an orphan
sidecar without waiting for the Elgato to release — **fixed** in `run_orion.local.ps1`. If it recurs,
the cause line now survives the log throttle:
`Capture-card warm timing revoked: actual route is not configured DirectShow index N (actual_api=… actual_index=… mode=…)`

**Never force-kill Orion** (a kill mid-write zeroes `actuation_lead_ms`; the bot then runs the 218.5
factory prior and looks like a timing regression). Close with the window X. It runs elevated, so an
unelevated shell cannot `CloseMainWindow()` it — ask the owner to close it before rebuilding.

---

## 6. Confirmed-real bugs, unfixed (safe, none touch timing)

1. **The estimator cache never persists.** `_persist_cache()` requires `_has_controlled_anchor` and
   `_controlled_validation_ready`, set only when `calibration`/`controlled_validation` is true — and
   **135/135 live release markers carry `calibration=0`**. `%LOCALAPPDATA%\Orion\timing\` has never
   existed. Every session cold-starts at the factory prior `measured=218.5 sd=28.7`. Value is
   limited: `measuredLeadForActuationMs()` returns the user's 300 whenever in band, so the posterior
   never drives the lead — this buys fewer cold-start aborts and an honest `lead_sd`, not σ.
2. **`release_enabled` latent landmine.** `pushRemapUpdate()` never sends it; the handler at
   `autogreen_sidecar.py:2734` rebuilds `RemapConfig(...)` without it, so it reverts to the
   `True` default (`controller_remap.py:66`) on every config change. **Inert today** — the Python
   engine's output requires `self._virtual_controller` (`remote_play_orchestrator.py:5579`) and
   `virtual_controller=False` every session. Becomes a real dual-release the moment anyone enables a
   virtual controller. One-line fix either side.
3. **Tick snap can never engage.** `conf = clip(1 − rms/6.0, 0, 1)` keys to per-sample residual
   (≈18.8), pinned at 0 against `fusedTickSnapMinConf = 0.5`; pair buffer hard-capped at 16 so
   `sd = rms/√n` cannot reach the 1.2ms gate. Worth ~+1.5pp at current σ — fix it as a dead feature,
   not as a lever.
4. **`phase_source_verified` hardcoded `False`** (`rtt_sync_engine.py:798`) gates native
   `tickPhaseReady` at `RemotePlaySession.cpp:3990`. Also inert for a second reason: `courtRttReady`
   needs a verified public court IP and the RTT ticks log `court=-`.

**Pattern worth a standing sweep:** this codebase repeatedly ships gates whose thresholds cannot be
met on real data, on features that are therefore off forever, while unit tests pass because the
synthetic fixture is cleaner than reality. Four confirmed instances (the probe collector, the cache,
the tick snap, `phase_source_verified`). Checking every `clip(1 - x/N)` confidence and every
`MinConf`/`MaxSd` engage gate against the measured real-data distribution is cheap and has paid four
times.

---

## 7. Refuted — do not re-propose

Aim/green centring (measured 84%). ViGEm jitter (path is a named pipe; **0.110ms** robust SD over
1009+1242 submits). Command-path jitter as a term (0.110ms — a prior claim of 28–36ms was a misread
predictor-sigma field). Capture-card latency as dominant (**227 vs 234ms** end-to-end between card
and decoder paths). The freeze oracle (causally falsified — this also undermines
`tools/timing/oracle_join.py`). `settled_fill`/`peak`/`f_stop`/`travel_pp` as instruments (circular
or non-responsive — **do not tune against any log-derived fill metric**). The 28.7ms actuation prior
(refuted twice). `lead_sd = 28.7` poisoning the validity gate (cap is 75.0; phase path computes
31.9ms — not binding). Tick alignment as a large win (~+1.5pp). The registration/template predictor
as an accuracy improvement. Banner-driven closed-loop grading of the learner (owner directive;
offline banner *measurement* is wanted, control input is not). Dual-engine release as a live cause
(real defect, inert).

---

## 8. Constraints

- **Fail closed.** No release without valid authority. Never fabricate a release, never emit a
  guaranteed-overtime command, never restore a "late cadence recovery" path.
- Preserve the controller reconnect fence, RawInput pass-through, and absolute max-hold safety.
- Default-OFF and revertible until graded on a counted batch.
- **Do not ship a safety-gate change (Candidate A) without a confirming 50-shot batch.** Demo is
  2026-08-08; shipping unvalidated is worse than shipping the current 84%/95%-driven behaviour.
- Live batches are the scarce resource. Prefer offline grading:
  `tools/regression/run_gates.py`, `tools/timing/tip_predictor_eval.py`, the engine-exact PHASE
  SAMPLE replay, framedump replay.

---

## 9. DO NOT REGRESS — things that are cheap to break and expensive to find

### 9.1 The launcher fix is NOT in git

`run_orion.local.ps1` is gitignored for key hygiene, so the fix that cured the intermittent dead
sessions exists **only as a live file on this machine**. If that file is lost, regenerated, or the
repo is cloned elsewhere, the sidecar-reap race returns and timing dies at random again. Reapply it
by hand — in the reap block, the python sidecar loop must arm the same settle wait as OrionNative:

```powershell
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'autogreen|orchestrator|sidecar|nexus' } |
    ForEach-Object {
        $killedCapture = $true          # <-- THIS LINE. The sidecar is what holds the Elgato.
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
```

Without `$killedCapture = $true` there, the 2.5s capture-driver wait below never arms when only a
sidecar was reaped — which is the common case, because closing Orion with the window X exits the
native process cleanly and orphans the sidecar.

### 9.2 Never tune the anchor with the shipped slope

Run `python tools/timing/measure_anchor_slope.py logs/diagnostics/detframes*.csv` first. The shipped
`tipPhaseConstantSlopeMsPerPct = -5.235` is wrong at the low end (true 20->30 is **-5.830**). Using
it makes the bot fire ~6ms early on every shot, and nothing surfaces that until a counted batch.

### 9.3 Do not enable `autonomous_green_center_frac`

It is 0.0 for a reason (see §1). The only genuine bot timing misses in the counted batch were
**earlies**; centring shifts earlier and would worsen them. Do not flip it without a new counted
batch that shows lates dominating.

### 9.4 Do not tune against any log-derived fill metric

`settled_fill`, `peak`, `f_stop`, `travel_pp` are all circular or non-responsive — refuted four
times. `settled_fill` moved 0.28pp for 18ms of lead change (12x damped vs velocity). The only
outcome signal that has ever tracked reality is the game's own TIMING banner, counted by hand.

### 9.5 Read the EFFECTIVE aim, never the shipped constant

`effectiveTipPhaseConstantMs() = learnedPhysicalMs + (tipPhaseConstantMs - tipPhaseSeedPhysicalMs)`.
`learned_phase_physical_ms` in `learning.json` drifts within a session — it moved 319.216 -> 318.594
during one evening. Quoting the constant as "the aim" has already invalidated a whole tuning walk.
Also: **never commit `learning.json`** — it is live learner state.

### 9.6 When a test fails after a change, decide which side is wrong

Three tests broke tonight because constants had been changed hours earlier without re-running the
suite. The right resolution was to **revert the constant** (it had been changed against a refuted
metric), not to edit six assertions to match it. Editing the test to agree with the code is how an
unevidenced change becomes permanent.

### 9.7 A passing suite is not evidence a feature works

The probe collector was 100% broken on every rig forever while 66 tests passed, because the
synthetic driver started its rise at exactly the one value real data never produces. When a fixture
constructs the input, check what values it starts from.

---

## 10. Suggested first moves

1. ~~Verify the Candidate A arithmetic~~ **DONE 2026-08-05** — frames are 16.7ms apart but frame 2
   is only conditionally dead; the rise gate binds the median shot (see §3).
2. ~~Offline-validate the 2-frame proof~~ **DONE** — `tools/timing/validate_ownership_proof.py`:
   median 13.8ms recovered on budget-critical shots, zero extra false-lock admissions, and the
   3.0→4.0 rise "compensation" refuted (saves 0ms).
3. ~~Implement Candidate B~~ **DONE 2026-08-05** — `tip_phase_anchor_base20`, see §3. Budget
   93 → ~151ms; per-rung measured dating; learning.json canonical at base 30.
4. **NEXT: the counted 50-shot batch.** Both flags are ALREADY SET in settings.json
   (backup: `settings.json.pre_batch_20260805`), `native_orion/build/Release/OrionNative.exe`
   (the launcher's target) is rebuilt with both, and the launcher enables the display-only
   box predictor (`ORION_READER_BOX_PREDICT=1`, `CAP_PX=20`) for the overlay-sag complaint.
   Launch via `run_orion.local.ps1`, verify liveness (§5), count 50. Compare **lates vs the
   6/50 baseline** and watch earlies (per-rung dating removes a ~1.7–1.9ms systematic late at
   rungs 35/40, so earlies should not worsen; the 2026-08-04/05 log census: deadline misses
   were 127 of 191 no-fire presses, ownership-proof 49 — A+B attack 92% of the abort mass).
5. If lates persist after A+B, the remaining suspects in measured order: frame-age jitter
   (mean 28ms on misses vs 10.8 on successes — capture pipeline), the
   `subtick_fire_at_past` token-kill tail (~40 episodes/2 days, overdue ~12ms vs 8ms grace —
   fail-closed by design, do NOT convert to fire-late), and the biased sampler chain on shots
   that still miss every rung.

Expected payoff if the lates are removed: those ~6 shots become driven shots at the measured ~95%
driven rate, i.e. **84% → ~95%** overall, and lates specifically go away — which is what the owner
asked for ("a few earlies is fine but no lates").
