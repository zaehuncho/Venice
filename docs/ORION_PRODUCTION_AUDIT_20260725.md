# Orion — Production Audit & Fix Batch (2026-07-25)

Six parallel Opus-5 read-only audits of the live path, then a four-agent fix batch.
Baseline before the batch: **51/51 detection gates, 221 native tests / 0 failed.**

---

## THE HEADLINE FINDING — why the bot drove shots that never went in

**`effectiveLatency` under the shipped config was ~6 ms when the real invisible loop is ~60-110 ms.**

`AutomationEngine.cpp` composes the fire lead as `fire = predictedTip - effectiveLatency`. Under
`autonomous_vision=true` (the shipped config) that branch deliberately uses **one** self-measured
number — `learnedLatencyMs` + a per-type residual — and explicitly drops the modelled
`controllerChainMs + remotePlayPipelineMs` constants. Two native tests
(`autonomousGlobalLeadConvergesFromZero`, `measuredLeadUsesMeasuredLatencyAsLeadBase`) enforce that
design, so it is intentional.

The design assumed the post-release grader would dial `learnedLatencyMs` from its 0 seed up to the
true latency. **That never happens in production:**

- the launcher forces `ORION_FREEZE_CAL`, so `calibrationFrozen` is true; and
- the grader's only signal was the meter self-grade, retired as degenerate (the phantom
  `LATE 66ms` / `EARLY 90ms`, `greens=0`).

So `learned_latency_ms` sat at its seed (**6.0** in `learning.json`) permanently. The engine fired
~6 ms before the predicted tip while the press needs the capture pipeline **plus** the
ViGEm→chiaki→LAN→PS5 path to land — a constant, every-shot, every-type **late** bias of tens of ms.
That is exactly what a 0-green record looks like.

**Fix applied:** seeded `learned_latency_ms = 75.0` (was 6.0). The code was left alone — the
"one self-measured lead, no hardcodes" contract is correct; only its seed was wrong.

## UPDATE 2026-07-26 — THE SEED WAS NEVER THE WHOLE STORY: the fire floor caps the lead at ~46 ms

The first live batch on the seeded lead came back `0/0 green`. Root cause, confirmed by arithmetic against
the live log:

`AutomationEngine.h:267` — `tipFireMinFillPct = 88.0`. Under `globalApplies` this becomes
`minPredictiveFillFloor`, and `abovePredictiveFloor` is ANDed into `withinReach`
(`AutomationEngine.cpp:3380-3385`), so **no predictive release may fire below 88% fill**.

Expressing a lead of L ms at velocity v requires firing at `target - v*L`. With the live
`target=99.6`, `v≈0.25 %/ms`:

| lead | required fire fill | allowed? |
|---|---|---|
| 46 ms | 88.1% | just barely |
| **75 ms (seeded)** | **80.9%** | **BLOCKED by the 88 floor** |
| 100 ms | 74.6% | blocked |

So the maximum lead the engine can physically express is `(99.6 - 88) / 0.25 ≈ **46 ms**`, no matter what
`learned_latency_ms` holds. **Seeding 75 changed nothing.** Live proof: vision releases cluster at
88.9 / 90.1 / 90.7 / 91.3 / 91.9 — pinned against the floor with machine-like consistency, not spread as
sampling granularity would produce.

Consequence: the bot fires ~40 ms late on EVERY vision shot of EVERY type — precisely a 0-green record
with detection working perfectly. **The floor and the lead are in direct conflict and must be reconciled
together**; raising the lead alone is inert, and lowering the floor alone re-opens the early-fire risk the
floor was built to prevent (crossing IQR widens well below the tip).

> **This 75 ms is the single most important number in the product and it is an estimate.**
> Credible range 60-110 ms (modelled `latency_compensation_ms 45 + remotePlayPipelineMs 55 = 100`;
> the sidecar's own end-to-end oracle previously measured ~70). Trim it live: **shots landing late
> → raise it; early → lower it.** The permanent fix is the release→peak self-measurement (below).

### The keystone still to build
Fire the release, then watch when the meter **peaks**. That delay *is* the true loop latency, needs
no banner/green reading, and would let `learnedLatencyMs` self-calibrate — restoring the original
design intent without the retired grader. The engine already has `holdStartMs` / `firstMeterSeenMs`
and the reader already detects the peak.

---

## ROOT CAUSE — PS5 "LAN cable disconnected"

**There is no code path anywhere in the product where Chiaki exits gracefully.** Every termination is
`TerminateProcess`:
- `remote_play_client.py:419-444` — `proc.terminate()` on Windows **is** `TerminateProcess`, then
  `taskkill /F /T`, then a 6 s `terminate_chiaki_processes()` sweep.
- `RemotePlaySession.cpp:144/163/1962`, `OrionAppController.cpp:4154-4157/2602-2609/3586-3589`.
- `RemotePlaySession.cpp:598` — `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; closing the job handle in the
  destructor TerminateProcess-es the whole tree regardless.

`chiaki_session_stop()` therefore never runs, no Takion/ctrl disconnect is sent, and the PS5 sees the
transport vanish mid-session. **It also fires mid-game** on every watchdog `restartSidecar()`.

**Fix:** post `WM_CLOSE` to the tracked stream-window HWND and wait, escalating to the old kill path
only on timeout; move `_client_manager.stop()` to the top of the orchestrator's `stop()`; raise
`kSidecarGracefulShutdownMs` 2000→5000; make the taskkill sweeps conditional on the graceful path
having failed.

Note this also **exonerates WinDivert**, which was blamed in 2026-07-02 and is why passive sniffing
(live court IP / RTT / jitter) was defaulted off. Its other premise — "sees ZERO packets on a
non-routing gaming PC" — is also false here: this PC **routes** the PS5 via ICS
(`192.168.137.1`, PS5 at `.100`), so `NETWORK_FORWARD` is exactly where that traffic flows.
Telemetry is therefore being defaulted **on**.

---

## DETECTION — the reader is not the problem; its *state* is

Measured across 45 shots / 5 sessions (incl. fades): within-shot detect rate **0.959-0.982**.
**80% of the remaining "misses" are frames where 2K has not drawn the meter yet** (literally 0 red
pixels in the search band) — a metric artifact of the gate's 3-frame span padding, not a detector
failure. True rate excluding those: **0.994**. Detection is shot-type agnostic and already good.

What is actually broken is **cross-shot state carry-over** — shot N's garbage taxes every later shot:

| # | Defect | Consequence |
|---|---|---|
| 1 | `:2328-2334` a **single 1-row green speck** is accepted as a "full cap" (the ≥2px run floor only applies when `len(runs)>1`) | fill denominator ratchets ~160→~243; **a full meter reports ~66%**, permanently |
| 2 | `:2733-2735` the SILENT_RESEAT **speculative** read mutates `_track_h_hist`/`_track_w_hist`/`_low_cand_hist` | a *rejected* steal still poisons the denominator |
| 3 | `:2409-2414` no-green fallback uses the raw strip height (~230) as the denominator | fill under-reads ~30% **and** the box top sits ~70px above the real tip |
| 4 | `:2287,:2346-2354` apex walk's silver threshold ≈ **one pixel** | tip dragged up to 12px into the background — and those px feed the ratchet |
| 5 | `:1982-1991` `_box_from_tip` unclipped + ratcheting height | strip truncates but denominator doesn't → 20-27pp under-read |
| 6 | `:2130,:2413` the 240/230 constants are **unscaled** @1080p | at 1440p/4K fill over-reads → fires early |
| 7 | `:1509-1514` `_scale_est` is an **α=0.5** EMA | one blurred width moves it ~30% in ONE frame; gates stay widened ≥8 s |
| 8 | `_reset_state()` never clears `_size_base`/`_scale_est`/`_vz_ref`/`_meas_*` | a live colour change divides the NEW meter by the OLD meter's baseline |

Plus: off-shot `meter_memory` re-emits a **byte-frozen** fill for 27-44 frames past shot end, so the
previous shot's dead lock is still being served when the next shot starts — the user-visible
"box sits wrong → blinks → relocks". `ORION_READER_FAKELOCK_BREAK=1` bounds it (A/B'd 51/51).

**Design directive:** make each shot an **epoch** — per-shot tracking state is cleared on arm and the
lock is re-acquired cold; only a slow, two-sided-validated session calibration persists. Today those
are the *same variables*, which is the root design flaw.

> **Also found:** `ORION_READER_TRACK_H_ROBUST_GATE` (set in the rig) **does not exist** in the
> reader — 0 references, a no-op. Same for `ABSENT_CONCEDE`, `VZOOM_DOWN_COLD`, `SPENT_DROP`.
> `ANCHOR`, `PCTL_FILL`, `FAKELOCK_BREAK` are real. Note `ANCHOR`/`PCTL_FILL` are set live but are
> **default-OFF in code**, so the live config is not what the 51/51 gate validates (re-run gates with
> the rig env as a second baseline).
> **Rejected by measurement:** `SHOT_COAST=1` → **46/51** (ghost frames on all 5 sessions).

---

## TIMING — further biases beyond the lead

- **Decel correction has the wrong SIGN** (`:3415-3420`, dup at `:3462`, `:4576`): it *shortens*
  time-to-tip exactly where the meter decelerates and linear extrapolation is already early-biased.
  Fires on every predictive release (`decelKneePct == tipFireMinFillPct == 88`).
- **`predictive_target` extrapolates from `now` using a fill captured up to 180 ms ago** (`:3411`,
  `:3429`) — nothing subtracts `now - lastDetectionMs`. Pure late bias.
- **Staleness siblings**: `:3378`, `:3441`, `:3453` and `reevaluateScheduleOnFreshSample:4467` gate
  only on `meterFresh`, and memory extrapolation advances `fillPct` on echo frames — so a fire can
  be armed on fill the detector never saw. (One block was already guarded; the siblings were not.)
- **Target divergence**: `processHolding` has a fade→green-entry branch; the reschedule mirror has
  **no fade branch**, so the scheduled fire aims a different point than the immediate fire.
- **`std::abs(networkOffsetMs)`** inverts a negative manual sync trim (settings already hold `-50`).
- **Actuation budget** is small and healthy: console 60 Hz tick quantisation dominates
  (≈ +8 ms mean, σ ≈ 5 ms); precise-fire thread wake < 0.5 ms.

---

## TEMPO vs BUTTON

The clocks are **already identical** (both read `globalHoldToReleaseMs`; fades share
`shot_type_feedforward_ms` via the D3 fallback). The entire divergence is the **anchor**:

- button arms at press+190 ms and the game's shot starts at the **physical press**;
- tempo arms at press+24 ms and the shot starts at `beginShot`;
- so tempo's `holdStartMs` is **190 ms early animation-relative**, pulling every hold-anchored clock
  (blind-fire net, commitMin, minHold, tempo fallback, no-meter abort) early.
- tempo also has an up-to-650 ms `Armed` state with **no release path at all** — a blackout button
  mode does not have.

**Fix:** route tempo direct-to-`Holding` and stamp `holdStartMs = now + (squareHoldArmMs -
tempoSquareHoldArmMs)`. Plus data hygiene: the `|tempo` RTT baselines had **diverged** from their
plain-type twins (Standstill 13.59 vs 7.87), which alone shifted tempo's lead ±7.5 ms — those keys
were deleted from `learning.json`.

---

## CONCURRENCY

No lock-order inversion (every nesting is the documented `submitMutex_` → `m_`). The real damage:

- **Four `VirtualController` mutation sites take no lock at all** — `VirtualController` has no
  internal lock, so serialization is entirely caller-side. Teardown, the physical-pad-lost path, and
  the RawInput `GIDC_REMOVAL` handler can all submit/free the pad concurrently with the precise-fire
  thread's release write → corrupted or dropped release on the shot in flight.
- **Destructor joins the fire thread LAST**, after `disconnectRemotePlay()` already freed the ViGEm
  target it dereferences — use-after-free on exit with a shot armed.
- **A normal Disconnect can self-inflict SAFE MODE**: teardown blocks the GUI thread past the 6 s
  freeze-watchdog threshold with no heartbeat bump, and safe mode is a manual-recovery latch.
- **Frozen-feed watchdog is unreachable**: the `pixelAge > 12000` term is nested inside a
  `frameAge > 20000` gate, but a byte-frozen card keeps delivering duplicates so `frameAge` stays ~0.
- **`feed_healthy` is a dead wire** — emitted by the sidecar, parsed by nothing.
- **Capture stall-reopen can never succeed**: it opens the new handle *before* releasing the old one,
  on an exclusive-access device. The whole recovery path is a no-op.

---

## PRODUCTION / DEBLOAT

- **≈110 GB safe to reclaim** (~69% of the tree): 50.6 GB non-gate framedumps, 29.2 GB `datasets/`,
  12.6 GB orphaned loose framedump files, ~11 GB retired-chain training corpora, 2.16 GB redteam,
  1.78 GB write-only `archive/local-runtime/`, 936 MB stale crashdump, plus build/release output.
- **DO NOT TOUCH: 43.6 GB of gate framedumps** — the 5 session dirs hardcoded in
  `replay_gates.py:444-449` **are** the 51/51 gate, and they are untracked + gitignored. Back them up
  before any `logs/` cleanup. Also load-bearing: `.venv311/` (first sidecar interpreter),
  `MeterDetector.cpp` (retired but must stay in the CMake source list), `nexus_svc.py` and the
  backend scripts (spawned by **string path**, never imported).
- **Retired chains are correctly retired** — torch/ultralytics are not imported on the shipped path.
  Only residue: `from meter_detector import DetectorConfig` (`remote_play_orchestrator.py:579`),
  a 300 KB module parse every session; it is torch-free, so cost is startup only.
- **Flag census**: 249 env-flag-shaped names, **199 live, 50 dead** (prose-only). 28 are default-ON
  (now permanent behaviour). All 35 flags the rig sets are live readers.
- **`AppConfig.h` defaults have drifted from the values the rig actually runs** —
  `meterColor` Purple vs live Red, `autonomousVision` false vs true, `tipGateCapMs` 100 vs 300,
  `hardwareDecode` false vs true, `streamBandwidthMode` Performance vs Balanced. **A fresh install
  does not reproduce the tuned configuration** — highest-leverage config fix before ship.
- `updateChannel` defaults to `"beta"`; `cudaEnabled` defaults true though torch is excluded from the
  bundle; **"No Meter" mode is a shipped UI toggle whose dependencies (torch + 3 weight files) are
  deliberately not packaged** — it fails silently into a swallowed `except`.
- **Unbounded main log** — `flushPendingLogs()` appends forever, no rotation (currently 43 MB).
- `ORION_DETCSV` defaults **ON**: a line-buffered per-frame CSV write = ~60 disk flushes/sec on the
  capture thread, for every customer.
- `settings.json` carries another machine's absolute `chiaki_path`
  (`C:\Users\Administrator\...`); mitigated only by the packager excluding the file.

---

## VERIFICATION CONTRACT

- `python tools/regression/run_gates.py` → **51/51 detection + 221 native / 0 failed**.
- **The gate runner does NOT rebuild the test exe.** Always
  `cmake --build native_orion/build --config Release --target OrionNativeTests` first, or you are
  grading a stale binary (this silently hid 3 failures earlier in the session).
- `OrionNativeTests.exe` stdout does not survive shell redirection here — use its **exit code**
  (= number of failures).
- Offline green is necessary, not sufficient: a flag set once passed 51/51 offline and still cost
  −8% live. Every behavioural change needs one live batch.
