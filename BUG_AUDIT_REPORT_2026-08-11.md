# Venice / Orion — Repo-Wide Bug Audit Report

**Date:** 2026-08-11 · **Ship (early access):** 2026-08-28 (17 days) · **Branch:** `fix/timing-input-and-remoteplay-blockers`
**Method:** 9 parallel deep-read agents across the priority areas in `BUG_AUDIT_BRIEF.md`, each briefed with the brief's hard-safety rules, verification traps, and known-intentionals. No source was modified; the app was not run. Findings are split CONFIRMED (traced end-to-end / reproduced) vs PLAUSIBLE (read the code, didn't run it). Every agent also returned CLEAN negatives.

---

> ### ⚠️ CORRECTION (2026-08-11, verified against `git` after publication)
> **#47 is NOT an open ship-blocker — it is already fixed and committed on this branch.** Commit `85f0e20` (*"capture: #47 make route revocation retryable, keep cache poisoning permanent"*, 09:26 today) split the flag exactly as §1 recommends: `already_revoked` now survives only as a code comment (`remote_play_orchestrator.py:3127`), the retryable `_capture_route_currently_invalid` flag is wired through ~10 sites, the poisoned-cache-never-reloaded invariant is preserved, and the file is clean in `git status`. The P3 agent that filed §1 almost certainly read a stale `.claude/worktrees/agent-*` snapshot (predating the 09:26 commit); its cited live-log line (16048) is **real historical** evidence from a session *before* the fix landed. **Net: the repo currently has ZERO open ship-blockers.** The only still-needed item from that area is the small `0.0`-epoch velocity guard (§3, "Fail-closed 0.0 epoch"), which is genuinely unapplied. Validate the merged #47 fix in CI via `tests/test_remote_play_frame_pipe.py`. Details in `.audit_raw/CORRECTION_47_already_fixed.md`.

---

## 0. Executive summary

**One true SHIP BLOCKER survives, and it is the one the brief already suspected (#47).** Everything else is either HIGH-value-but-recoverable, a latency/accuracy improvement, or a refutation of something the brief feared.

Scoreboard on the brief's open items:

| Item | Brief's belief | This audit |
|---|---|---|
| **#47** capture-API one-way revocation | ship blocker, real fix open | **ALREADY FIXED on-branch** (commit `85f0e20`) — see correction banner above; P3 read a stale worktree |
| **#87** service thread/handle leak | believed real | **CONFIRMED** — unauthenticated local **DoS** on a LocalSystem service |
| #87 latched `DriverState::Error` | "may never clear / bricks meter-delay" | latch **CONFIRMED**, "**bricks**" **REFUTED** — latent/cosmetic as wired |
| #87 port mismatch 30099 vs 30020 | real, not a typo | **CONFIRMED** — causes a **silent meter-delay no-op**; narrow-detector is the safe fix |
| **#88** Square tap swallowed | open | **CONFIRMED**, and found a **second** hardcode site |
| **#42** D-Pad Up double-booked | open | **CONFIRMED but latent** (Defense Mode is unwired today) |
| **#64** service-name mismatch | believed addressed | **CONFIRMED addressed** — every call site tries both names |
| **#89** connect ~3.1–3.4 s | fallback cosmetic | **CONFIRMED** — cost is Chiaki sidecar bring-up; Senkusha is compressible |
| Meter-delay toggle "inoperative" | reported broken | **REFUTED** — correctly wired; owner saw the honest "backend disarmed" banner |
| **−57 ms systematic offset** | "cause unknown, high value" | **SOLVED — not a timing offset at all** (self-grade fill→ms artifact) |
| `noMeterBaseOffsetMs` default-outside-clamp class | pattern to hunt | **CLEAN** — the known instance is already fixed; 0 other violations in 50 settings |

**The #1 ABSOLUTE-PRIORITY symptom from the tip-timing pickup — `live_tip_deadline_missed` drift-to-abort — is now explained** (§3.1). Its "obvious" fix (`phase_veto_directional`) was **deliberately pulled on 2026-08-08** because it introduces an *unbounded* no-release regression; do not flip it. The real fix is at the root.

---

## 1. SHIP BLOCKERS (must fix before Aug 28)

> **UPDATE:** This section is retained as the evidence trail, but #47 was verified **already fixed on-branch** (commit `85f0e20`) after publication — see the correction banner at the top. The description below is the *pre-fix* state the P3 agent read from a stale worktree. **No action required beyond CI verification.**

### [~~SHIP BLOCKER~~ → FIXED on-branch `85f0e20`] #47 — Capture warm-timing revocation is a permanent one-way latch; only a full restart recovers
`remote_play_orchestrator.py:3074-3089` (latch), set at `:3130`, init at `:831-832`
**WHAT:** `_guard_capture_latency_route()` reads `already_revoked = _capture_warm_cache_revoked`; when true it takes the branch at `:3074` and `return False` at `:3089` **unconditionally**, before ever evaluating `if matches:` at `:3091`. The only `return True` in the guarded section (`:3123`) lives inside that now-unreachable block. The flag is set `False` **only** in `__init__` (`:832`) and `True` **only** on mismatch (`:3130`) — no other production assignment exists.
**TRIGGER:** `matches` requires `actual_api=='DSHOW'` AND index AND mode AND geometry. A **transient DSHOW→MSMF fallback during stall recovery** — explicitly documented for the shipping HD60X card (`capture_card_backend.py:514-531`: *"on some cards (HD60X) DSHOW is the one that wedges… MSMF rides through it"*) — flips `actual_api` to MSMF and latches permanently, even after the card returns to the exact configured DSHOW route.
**IMPACT:** Once latched, all three consumer gates pin timing to neutral: `latency_estimator_attestation_snapshot()` returns generation 0 (`:3184-3185`), `attest_controller_latency_route()` returns False forever (`:2008-2011`), and `capture_invalid = revoked OR not verified` forces `generation=0, route=''` (`:3200-3204`). The sidecar emits **generation 0** for the rest of the process, so the C++ brain rejects the sidecar's measured latency and falls back to its own model (~13 ms modeled vs 40–100 ms real). A reconnect does not help — the flag lives on the long-lived orchestrator `self`.
**EVIDENCE (live log, real occurrence):** `logs/orion_native.log` line 16048 — *"Capture-card warm timing revoked: actual route is not configured DirectShow index 0 (actual_api=MSMF actual_index=0 mode=UNVERIFIED)"*. `actual_index` **matched**; the revocation fired purely on `actual_api=MSMF`. Clean re-attestation (`capture_negotiated_mode_attested`, line 16158) only occurred in a **brand-new sidecar process** after teardown (line 16054). Demonstrates "cannot recover without a full restart."
**CONFIDENCE: CONFIRMED** (control flow + flag lifecycle + live-log trigger).
**FIX (behaviour-changing — intended):** Split the one conflated flag:
- `_capture_warm_cache_poisoned` — permanent, one-way; gates **only** `restore_cache` (never resurrect the stale on-disk posterior scoped to the old route identity).
- `_capture_route_currently_invalid` — retryable; recomputed every guard call from `matches`. Remove the unconditional early-out at `:3074-3089`; on a valid DSHOW/index/mode match set `_capture_warm_cache_verified = True`, return True, and allow fresh **cold, empty-scope** attestation (non-zero generation); on mismatch suppress (gen 0) but stay retryable.
This restores attested *cold* timing in-process when the card flips back to DSHOW, while never reloading the poisoned persisted cache.

---

## 2. HIGH

### [HIGH] #87 — Per-connection client thread + kernel HANDLE leak on the LocalSystem service = unauthenticated local DoS
`native_orion/venicenet_service/IpcServer.cpp:540-546` (accumulation), cleanup only at `:505-514` (`stop()`)
**WHAT:** `acceptLoop()` spawns one `std::thread` per connection into `clientThreads_` and reaps with `remove_if([](std::thread& t){ return !t.joinable(); })`. The predicate is inverted — a `std::thread` stays `joinable()==true` after its function returns until `join()`/`detach()` is called, and `handleClient` threads are never joined/detached except at final `stop()`. So the reap removes **nothing**; every connection permanently adds a thread object (each holding an open Win32 thread HANDLE) freed only at service shutdown.
**TRIGGER:** Any local process connects to `127.0.0.1:47291` and disconnects, repeatedly. **No authentication is required** — the leak happens at accept, before the token check. There is no live-connection cap (`listen(srv, 8)` is only the backlog).
**IMPACT:** Unbounded kernel-handle + heap growth inside a **LocalSystem** process. An unprivileged connect/close loop exhausts the service handle table and grows `clientThreads_` until the service degrades/dies — a local DoS killing IPC + meter-delay for all clients.
**CONFIDENCE: CONFIRMED.** No test covers it.
**FIX (not behaviour-changing):** Per-client done-flag, `join()` flagged threads in the reap step; or `detach()` the client thread and drop `clientThreads_` entirely; add a max live-connection count.

### [HIGH · KNOWN/STAGED] #3.1 — Progressive drift-to-abort (`live_tip_deadline_missed`, release_seq=0) is explained; do NOT flip `phase_veto_directional`
`AutomationEngine.cpp:13757`, `13843-13913` (learner); `10986-11037` (symmetric veto); aborts at `7035, 7060`
**WHAT:** Two coupled, in-code-documented mechanisms reproduce the exact reported symptom.
1. **The phase learner rides its own release.** `learnedPhasePhysicalMs_` is a session-persistent animation constant fed by `observedMs = stopForMeasurementMs − meterCapPhaseAnchorMs_` (`:13757`), where *"the stop rides the release (the meter freezes when the game registers the press)"* (`:13778`). It is not reset per shot (by design — it belongs to the equipped jumpshot, persisted to `learning.json`). Because the observation partly echoes the fire it produced, a sustained bias walks the constant monotonically — the authors' own measured note: **aim walked 439.2→431.0 ms over one 70-release batch** (`:13918-13922`).
2. **A symmetric live-meter veto.** `disagreementMs = samplerFit.crossingMs − phaseTipAbsMs`; `contradictedByLiveMeter = disagreementMs² > k²·combinedVar` (`:10992-10997`). When the sampler (documented **+61–92 ms EARLY** bias, `:11006`) disagrees, the healthy dated phase tip is demoted to the sampler crossing, whose deadline already reads as past → abort at `reservation_age_ms≈0`, `release_seq=0`. The code documents **16 real aborts** (Go-To 5, No-Dip 8, Standstill 3), each carrying a schedulable phase deadline (+12…+132 ms) demoted on the tick (`:11009-11014`).
**IMPACT:** Exactly the reported failure — "first ~2 shots perfect, then progressive lates, then aborts entirely." Core selling point.
**CONFIDENCE: CONFIRMED** mechanism (present, session-persistent, produces reservation-age-0 aborts, per the code's own measured comments). PLAUSIBLE on exact drift *direction* (depends on game-meter freeze physics not in these files).
**FIX — CRITICAL CAVEAT:** The obvious remedy `phase_veto_directional` is a **deliberately-pulled staged flag** (`AppConfig.cpp:197-222`): drafted, evidenced against these same 16 aborts, and **pulled 2026-08-08** because with it ON, 12 `AutomationEngineTests` pins fail (shots stuck `waiting_for_live_tip_deadline`, `deadline=-1`, never releasing) — a potential **unbounded no-release** regression, judged worse than the bounded 16+3. **Do not flip it as a quick fix.** The real fix is at the root: ground the phase learner in world-only truth (or hard-rate-limit its per-session movement) **and/or** debias the sampler crossing the veto trusts. Both existing mitigations (`phase_veto_directional`, `tip_phase_aim_frozen`) are default-OFF but reachable via settings/UI; `tip_phase_aim_frozen` is also latched by the Tip-Timing "lock" (`OrionAppController.cpp:8088`).

### [HIGH · latency] Chiaki feedback **sender** thread runs at NORMAL priority — largest single input-jitter source
`chiaki-ng-src` (branch `orion`) — `lib/src/feedbacksender.c:744-747`, affinity cb `gui/src/streamsession.cpp:57-92`
**WHAT:** The actual encrypt-and-UDP-send of every shot packet runs on `feedback_sender_thread_func` (→ `chiaki_takion_send_feedback_state` → `takion_send_feedback_packet`: gkcrypt + `send()`). The Orion affinity callback sets `SetThreadAffinityMask` **only** — pins it to core 1 but leaves priority NORMAL with no MMCSS. Affinity pins the thread to a core; it does not reserve the core.
**TRIGGER:** Any other runnable thread the scheduler places on core 1 can preempt the sender between the input-bridge wake-signal and the send.
**IMPACT:** Up to one Windows scheduler quantum (~1–15 ms) of jitter injected on the exact product-critical shot-send. The asymmetry is stark: the takion **recv** thread gets MMCSS `"Pro Audio"`, the input bridge gets `THREAD_PRIORITY_HIGHEST`, but the thread doing the real send gets neither. A superseded experiment (`patch_affinity.py:59`) had the boost; the shipping callback dropped the priority line.
**CONFIDENCE: CONFIRMED** (no priority/MMCSS is set). PLAUSIBLE on the ms magnitude.
**FIX (behaviour-changing):** Give `CHIAKI_THREAD_NAME_FEEDBACK` MMCSS `"Pro Audio"` (mirror the takion block) or `THREAD_PRIORITY_HIGHEST`. **Not** `TIME_CRITICAL` — `orioninputbridge.cpp:83-93` documents that TIME_CRITICAL there preempted the MMCSS takion thread and caused shot-correlated lag. FEEDBACK (core 1) and TAKION (core 0) are on separate cores, so a matching MMCSS class is low-risk.

### [HIGH] #88 — Square TAP is swallowed with tempo remap on; the `passthrough` escape hatch is dead code
`controller_remap.py:818-819, 859-873` + `RemotePlaySession.cpp:742` **and `:1878`**
**WHAT:** In the shipped mode (`shot_trigger_mode=='button'`), any physical Square tap — steal on defense, menu navigation — never reaches the console. `_process_idle` strips the first frame into WARMUP (`:818-819`); `_process_warmup` strips Square every frame until `min_press_hold_ms` (~75 ms); a tap released early hits the cancel path (`:863-870`) which rebuilds IDLE and **never re-injects** the swallowed press. There is no re-injection buffer.
**TRIGGER:** Any Square tap shorter than ~75 ms while the remap is engaged. (Codified as intended in `tests/test_controller_remap.py:433`.)
**IMPACT:** User cannot steal, pass-fake into a menu, or navigate menus while armed.
**Why `passthrough` can't save it:** the only non-arming forward path (`_process_idle:813-814`) is unreachable — `shot_trigger_mode` is hardcoded to `"button"` in **two** C++ emitters (`RemotePlaySession.cpp:742` live-update *and* `:1878` initial config), and `load_remap_config()` (which could set `passthrough`) has **zero callers**.
**CONFIDENCE: CONFIRMED** (both halves).
**FIX (behaviour-changing):** Plumb `shot_trigger_mode` from a real setting so `passthrough` is reachable, OR add deferred-forward: if WARMUP cancels before the hold threshold, emit the swallowed Square down/up. Must be gated + tested.

---

## 3. MEDIUM

### [MEDIUM→HIGH under a flaky sidecar] Sidecar `QProcess` leaked on every unsolicited exit
`native_orion/src/RemotePlaySession.cpp:2312-2422` (null-out at `:2388`, missing `deleteLater`)
The `QProcess::finished` handler sets `sidecarProcess_ = nullptr` but never calls `proc->deleteLater()`. The deliberate-teardown siblings delete correctly (`:2437`, `:2572-2574`) — only the **self-exit** path orphans the object. Every sidecar self-exit (Python crash, chiaki death, capture-card loss) + watchdog restart — the exact recovery loop this product runs for hours — orphans a `QProcess` + its capturing lambdas + pipe read buffers. **CONFIDENCE: CONFIRMED.** FIX: append `proc->deleteLater();` as the last statement of the `finished` lambda (not behaviour-changing).

### [MEDIUM] #87 port-range mismatch → meter-delay **silently no-ops** while reporting "active"
`CourtIpDetector.h:37` (`kCourtPortMax=30099`) vs `MeterDelayIntercept.h:75` (`kPortMax=30020`)
The detector qualifies/reports a court endpoint on any UDP port **30000-30099**; the intercept's WinDivert filter only matches **30000-30020**. For any real court flow on a port in **30021-30099**, detection succeeds and the intercept reports `active=true`, yet the filter matches **zero** packets (`intercepted==0`, `bufferDepth==0`) → meter-delay silently no-ops, invisible to the user. **CONFIDENCE: CONFIRMED** (ranges provably differ; silent-no-op path traced). FIX (assess, don't reflexively "fix"): the behaviour-preserving alignment is to **narrow `kCourtPortMax` to 30020** (matches the intercept + `nexus_svc.py` + the test that pins `udp.DstPort<=30020` as the load-bearing gameplay boundary). Widening the intercept to 30099 is behaviour-changing and needs real NBA 2K26 port evidence.

### [MEDIUM · PLAUSIBLE] `OrderedFileLogSink` retry loops are shutdown-blind → clean-exit hangs forever on a persistent disk fault
`native_orion/src/OrderedFileLogSink.cpp:208` (also `:80, :128`)
`writeLosslessly()`'s mkpath/open/flush retry loops sleep 25 ms and retry forever without checking `stopping_`; the `enqueue()` capacity-wait predicate (`:80`) also omits it. On a persistent write failure (log dir quarantined by AV, ACL revoked, volume removed, disk full that doesn't clear), the worker spins forever and `stopAndDrain()`/destructor `worker_.join()` never returns → the process hangs on exit while holding `enqueueOrderMutex_`. FIX (behaviour-changing): make the retry/capacity loops observe `stopping_` (drop the un-writable tail on shutdown only), or give the destructor a bounded/detached join. (Rotation + 16 MB/8 MB caps themselves are fine.)

### [MEDIUM · PLAUSIBLE] Fail-closed `0.0` measurement epoch injected into the velocity clock as a real timestamp
`remote_play_orchestrator.py:4729, :4795`; `_frame_measurement_epoch_ms` `:5571-5576`
`_frame_measurement_epoch_ms()` deliberately returns `0.0` when the source epoch is missing/non-finite/≤0; `:4795` then calls `detect(frame, ts=0.0)`. In `SimpleMeterReader.read()` `ts=0.0` is **not** `None`, so it doesn't fall back to `perf_counter()` — the literal `0.0` flows into `_update_velocity()` and the wall-clock gates. A `_vel_hist` window mixing `0.0` with real epoch-seconds (~1.7e9) yields a garbage least-squares slope; an all-`0.0` window forces velocity to 0. Drives `eta_ms`/`rise_state` and gates the CV self-arm. Primarily a session-start/PTS-not-yet-converged and degraded-path issue (steady-state capture-card frames carry a real epoch). FIX (minor): when `_frame_wall_ms<=0.0`, pass `ts=None` (or skip the velocity/wall update for that frame).

### [MEDIUM] Injected WinHTTP monitor hook leaks per-request state unboundedly *(RE/instrumentation tooling — weight accordingly)*
`network_intercept.py:45` (also `:59-66, :137-144`)
`pendingRequests` (keyed by request handle) is never pruned — no close-handle hook, no delete — and per-entry `responseBody += data` grows with no cap. Left attached over a long run, memory in the target process grows steadily. **CONFIDENCE: CONFIRMED.** FIX: hook the close-handle call to `delete pendingRequests[...]`, cap `responseBody`. Note this is analysis tooling, not necessarily the shipping runtime.

### [MEDIUM · timing-accuracy hardening — from the −57 ms investigation]
These aren't runtime bugs; they are latent hazards + a fresh-install risk surfaced while solving the mystery (see §5.2 for detail): the **self-grade footgun** (`AutomationEngine.cpp:14265-14304`, already misled a prior tuning session), the **fixed 5.5 ms/pp EARLY magnitude mis-train hazard** (`:14302`, masked only by the current freeze), the **latency-label outlier acceptance** of a 303.8 ms spike (`latency_estimator.py:1877`, matters off user-lead), and the **41 ms seed-vs-converged phase portability gap** for fresh installs (`learning.json`=278 vs seed 319).

---

## 4. LOW / POLISH

- **[LOW-MED] `showSkeleton` toggle doesn't round-trip** — `OrionAppController.cpp:9167-9175`: setter saves, but `AppConfig::save()`/`load()` never (de)serialize `show_skeleton`; the OFF choice reverts to `true` every restart. Adjacent sibling `showLiveMeterMetrics` *is* round-tripped. CONFIRMED. (Debug overlay, no timing effect.)
- **[LOW-MED · PLAUSIBLE] Elevated update relaunches the app with an inherited admin token** — `OrionAppController.cpp:4875` (`runas`) → `updater_main.cpp:333` (`startDetached`, no de-elevation). Post-update the app runs as Administrator until the next manual restart; files it then writes can become Admin-owned (UAC ownership footgun). May be partly desired (ViGEm/WinDivert). Only on the `!installDirWritable` branch.
- **[LOW · PLAUSIBLE] Python `migrate_flat_keys` fights the C++ writer + writes settings.json non-atomically** — `orion_config_io.py:40-63` deletes flat keys that `AppConfig::save()` always re-adds (persistent churn), and its plain `open('w')` can race the C++ `QSaveFile` atomic write → possible lost update. FIX: temp+rename, or stop rewriting when nested exists.
- **[LOW] `confirmScheduledFire()` drop path is a dev-documented double-fire smell — but mitigated** — `AutomationEngine.cpp:9701-9737`. A confirm whose token matches but whose route-generation drifted is hard-dropped even though the worker physically submitted the edge. **Not constructible** as a live double-fire because `setControllerDeliveryRouteAttestation()` fences (`invalidateUnconfirmedSchedule`) *before* publishing G+1 and that fence is a verified **blocking** `Qt::DirectConnection`. **Action:** the OrionAppController owner should confirm `confirmScheduledFire` is only ever invoked synchronously inside that fence, never from a queued marshal that could land a G-generation confirm on a G+1 tick.
- **[LOW] LicenseClient emits `activationFinished` twice on a TLS error** — `LicenseClient.cpp:257-297`: the `sslErrors` slot emits, then `abort()` triggers `finished` which emits again with the misleading "timed out" branch. FIX: drop the emit from `sslErrors`.
- **[LOW] PS5 helper `QProcess` leaked + `helperResult` never emitted on FailedToStart** — `RemotePlaySession.cpp:1616-1686`: only `finished` is connected, not `errorOccurred`; a launch failure leaks the proc and hangs the setup UI with no result. FIX: add an `errorOccurred` handler.
- **[LOW] Frame-gap EMAs never reset per shot** — `AutomationEngine.cpp:2918-2927`: hygiene only; they only *widen* the arming horizon (clamped) and inter-shot gaps are gated ≤100 ms, so they cannot cause the abort. Optional reset in `beginShot()`.
- **[LOW · PLAUSIBLE] Synthetic-hold Square leak** — `controller_remap.py:827-840`: holding Square across a full autogreen cycle forwards the still-held real Square on return to IDLE without re-arming → a bot-unmanaged second meter. Edge case.
- **[LOW] Per-packet heap allocation on the service capture hot path** — `MeterDelayIntercept.cpp:784` + broadcast serialisation; allocator churn under game-rate flow (bounded queues, no correctness issue). FIX: pool/reuse buffers.
- **[LOW] Per-shot synchronous `LOGI` on the Chiaki inject/send hot path** — `orioninputbridge.cpp:224,275,309` + `feedbacksender.c`; `CHIAKI_LOGI` isn't NDEBUG-gated, so each flagged shot does 5–8 `vsnprintf`+stdout writes at the shot instant. FIX (observability-only): demote to `CHIAKI_LOGV` or rate-limit.
- **[LOW] `TCP_NODELAY` set on a UDP socket** — `takion.c:282` vs `:356` (SOCK_DGRAM): a no-op on a false premise; remove to avoid false confidence.
- **[LOW · PLAUSIBLE] `nexus_core.py:104` per-frame error `print()`** — unthrottled; a persistent inference fault spams stdout and can stall the capture loop if the pipe isn't drained. FIX: rate-limited logger.
- **[LOW] Frida session/script never detached** — `network_intercept.py:284` (RE tooling; benign on process exit).
- **[LOW ~NOT A BUG] `LeaseGate` first-call `QElapsedTimer.start()` race** — `LeaseGate.cpp:17`; benign (same QPC base, monotonic, no lease bypass). FIX: init via IIFE.
- **[LOW] Dead config fields** — `greenConfirmFastPath`, `noMeterShotTypeOffsets` (`AppConfig.h:380,687`): never persisted/reachable, sit at compile defaults. Wire or delete.
- **[LOW] Stale comments/docstrings** — `OrionAppController.cpp:12725` (says installer registers `NexusVisionSvc`; it creates `VeniceNetSvc`); `nexus_svc.py:1030` (says clamp `[0,300]`; actual `600`); `orion_build_optimized.bat` (`-march=x86-64-v2` claims AVX2/FMA/BMI2 — those are v3). Doc-only.
- **[LOW] Stale-patch footgun** — `chiaki-ng-src/patch_affinity.py` conflicts with the shipping affinity callback (feedback→core 3, all `TIME_CRITICAL`); not run by the build, but re-running it silently installs the exact TIME_CRITICAL policy the bridge comment says caused shot lag. Quarantine/delete superseded patch scripts.

---

## 5. NOT A BUG / REFUTED — reported loudly (the brief asked for this)

### 5.1 Refutations of the brief's own fears
- **Meter-delay toggle "inoperative" → REFUTED.** `MeterConfigPanel.qml:364-367` is a correctly-wired custom `DashboardToggle` (an `Item`, not a `Switch`), with a complete QML→`setMeterDelayEnabled`→`applyMeterDelayRuntimeConfig`→controller→DLL path and a `settingsChanged` re-eval. The suspected `onMoved: if(!pressed)`-on-a-Switch bug **does not exist** (that idiom is only on real `ThemedSlider`s). The owner's "no effect" is the honest **backend-disarmed** banner (`:325-349`), not broken wiring.
- **#64 service-name mismatch → CONFIRMED ADDRESSED.** Every app start/query site resolves through `candidateServiceNames() = {"VeniceNetSvc","NexusVisionSvc"}` (one `sc start` site, one `sc query` site, both name-agnostic). No hardcoded single-name start remains.
- **The −57 ms "systematic offset" → SOLVED; it is NOT a timing offset.** It's the post-release **self-grade** computing `errorMs = (settled_fill − green_center) × 5.5` (`AutomationEngine.cpp:14302`, `meterMsPerPct=5.5`), clamped ±90 — reproduced to 0.1 ms for all 7 EARLY shots from their logged fill numbers. The two LATE shots are both exactly `+66.0 = meterRecedeLatePct(12.0) × 5.5`. The "4 ms band" is the fixed 5.5 constant acting on similar fill deficits, **not jitter and not a scheduled offset**. All 7 EARLY shots had `peak_fill` below `green_start` — the meters never filled into their window (contested/low-peaking fades), not an early release. The `settled_fill` "corroboration" is the *same quantity* the error is derived from, not independent.
- **"5 of 7 EARLY from the `phase` tip source" → REFUTED (base-rate).** Phase armed 55/61 shots (90%), so "most EARLY are phase" is inevitable. Per-source EARLY rate: phase **9.1%** (5/55) — the **lowest** of any source; registration+sampler_far 50% (2/4, n<5, directional only). No phase-specific early bias.
- **`noMeterBaseOffsetMs` default-outside-clamp → already FIXED.** Clamp widened to `[-200,200]` on both paths (`AppConfig.cpp:1177, 1256`), consistent across C++/QML/Python. A systematic check of **all 50 clamped settings** found **zero** other default-outside-clamp violations.
- **#87 latched `DriverState::Error` "bricks meter-delay" → latch CONFIRMED, "bricks" REFUTED.** The error state never clears at runtime (`clearError()` never called; `onProbe()` runs once at boot; `onHandleOpened()` returns false from `Error` without clearing), **but** `driver_state` is never emitted on the wire and `start_meter_intercept` doesn't gate on it — so meter-delay keeps working after a transient failure. It becomes a real stuck-error only if a client ever honours the documented authoritative-driver_state contract. Latent/cosmetic as wired; still worth wiring `clearError()` on open-success.
- **Latency-estimator poisoning → NOT reproduced in shipping config.** With a Shot Lead set and in-band, `measuredLeadForActuationMs()` returns the user lead and **ignores** `measuredLatencyAuthorityMs_` entirely (`:10294-10298`, deliberate). `l_fixed` was noisy/non-monotonic (not a one-way ramp) and meter delay was off. Residual risk only *without* a Shot Lead or under an applied meter delay (see §5.2 #3).
- **`learnedOffset=0.0` on EXCELLENT shots → correct by design.** The learners are frozen in this config (`ORION_FREEZE_CAL`, `tip_phase_aim_frozen`, autonomous-vision freeze gates), and a green self-grade carries `errorMs=0.0` — a no-op for any learner ("green = no correction"). The learner is declining to learn from greens and correctly frozen against the degenerate self-grade; it is not silently failing.

### 5.2 Timing-accuracy hardening opportunities (from the −57 ms investigation)
1. **Self-grade is a documented tuning footgun** (`AutomationEngine.cpp:14265-14304`). The signed `EARLY errorMs=-57`-style lines read like real timing error but are `Δfill×5.5`; the code records a prior incident where a user acted on them and "floored Tip Timing and raised Shot Lead into the unschedulable regime." **Improve:** emit contested shots (`peak < green_start`) as `fill_gap_pp`, not synthetic `ms`.
2. **The fixed 5.5 ms/pp EARLY magnitude is a latent mis-train hazard** (`:14302`). The EARLY branch trusts the magnitude (unlike the LATE branch, which treats magnitude as uninformative). 5.5 is the near-anchor slope; near the tip the meter has decelerated further, so contested low-peak shots yield inflated "−57 ms". If the freeze is ever cleared (the intended demo/tuning path), these feed `learnFromOutcome` and walk the aim. **Improve:** convert the EARLY fill gap with the shot's own measured near-tip velocity, and gate EARLY-magnitude learning behind `peak_reached_green==false ⇒ difficulty, not timing`.
3. **Latency outlier acceptance** (`latency_estimator.py:1877`): a confident 303.8 ms label (≈+90 ms over the ~210 baseline, sd 6.0) was accepted and held 5 shots. A single large jump should require corroboration before becoming authority — de-risks users without a Shot Lead and the meter-delay regime.
4. **Seed-vs-converged 41 ms portability gap:** `learning.json.learned_phase_physical_ms=278` vs `tipPhaseSeedPhysicalMs=319`; a fresh install (no `learning.json`) runs the seed and starts **41 ms** off this rig's tuned aim. Re-derive the shipped seed from pooled `PHASE SAMPLE effective_const_ms` medians across installs before GA.

---

## 6. Max-the-bot — latency / accuracy improvements, ranked

| # | Improvement | Where | Expected win | Risk |
|---|---|---|---|---|
| 1 | **In-process cold re-attestation** (the #47 split-flag fix) — stop dropping to the ~13 ms native model for a whole session after a transient MSMF blip | `remote_play_orchestrator.py:3074` | Restores measured-latency timing without a restart; **biggest accuracy win** | Med (behaviour-changing, but that's the point) |
| 2 | **MMCSS-boost the feedback sender thread** to `"Pro Audio"` | `feedbacksender.c` / `streamsession.cpp:57` | Removes ~1–15 ms jitter on the exact shot-send; **biggest input-jitter win** | Low (separate core from takion) |
| 3 | **Root-fix the phase-learner drift** (world-only truth or hard per-session rate-limit) + **debias the sampler** the veto trusts | `AutomationEngine.cpp:13757, 10992` | Kills the drift-to-abort without the pulled flag's no-release risk | Med-High (aim path) |
| 4 | **Compress Senkusha on the known bot LAN** (force MTU fallback after one RTT ping / build with it off) + drop the fixed 10 ms `session.c:575` sleep | `chiaki-ng-src/lib/src/session.c:698` | Cuts a few hundred ms off the ~3.1–3.4 s connect (#89) | Med (MTU on non-1454 paths; safe on controlled LAN) |
| 5 | **Per-style detector threshold profiles** instead of one Arrow2/Red-tuned global set | `simple_meter_reader.py` (`_derive_bands`, `_hsv_band_mask`) | Hardens fill accuracy across 2K26 meter skins | Low-Med |
| 6 | **Frame-export readback onto a dedicated staging surface** (keep pack off the decode thread) — or verify the fps cap isn't forcing recv-thread readbacks the bot doesn't need | `orionframeexport.cpp:151` | −1–5 ms/frame recv jitter | Med |
| 7 | **Allow an MSMF route to earn a *cold* attestation** (non-zero generation) rather than zero authority — the pixels are valid, only the persisted cache is route-bound | `remote_play_orchestrator.py:3067` | Keeps measured timing alive on MSMF | Med |
| 8 | **Demote per-shot `LOGI` → `LOGV`** on the Chiaki hot path | `orioninputbridge.cpp`, `feedbacksender.c` | Removes formatting/I-O jitter at the shot instant | Low (observability-only) |
| 9 | **Re-derive the shipped phase seed** from pooled install medians (§5.2 #4) | `AutomationEngine.h:1435` | Fresh installs start on-aim, not 41 ms off | Low |
| 10 | **Corroboration gate on latency-authority jumps** (§5.2 #3) | `latency_estimator.py:1877` | Fewer mistimed shots off user-lead / under meter delay | Low |

Already-good, verified: Chiaki `SO_RCVBUF` 1 MiB, congestion cadence 200→100 ms, decoder `LOW_DELAY|FLAG2_FAST`, event-driven sender (`FEEDBACK_STATE_TIMEOUT_MAX` 200→50 ms), the ref-frame-corruption fix, the streaminfo-readiness window move, d3d11va hw-decode fixes.

---

## 7. Examined and found CLEAN (negative results have value)

- **AutomationEngine epoch/generation/token spine** — no async/worker consumer forgot the identity check; `scheduleFire`/`clearScheduledFire` single choke-point stamps/resets all identity fields; abort fences the worker between exactly-once consume checks; feedforward routes mutually exclusive; cross-thread state is all `std::atomic` with a seqlock double-check and an immutable value copy to the worker; worker joined before teardown. **Exceptionally well-defended.**
- **venicenet_service IPC** — `Json.h` recursion depth-capped (32, tested both sides), number parse overflow-safe, `\u` bounds-checked, tree bounded by 16 KiB line cap; the prior double-free is fixed; auth is fail-closed + constant-time; writer-socket + double-close lifetimes correct; IPv4 parse bounds-checked; buffer overflow policy release-oldest/order-preserved.
- **Detector** — fill-% math (no off-by-one, clamp deliberate + recorded), velocity key mapping, ts-to-capture-epoch wiring, read-bail (holds suspects out of velocity), capture-card EXCLUSIVE mode (no silent WGC/decoder fallback), dark-frame handling, frame-skip accounting.
- **Settings/updater** — migration infra (degrade-not-wipe, `*_user_set` veto, idempotent), the `meterDelayLeadOffsetMs` sum-envelope fail-close, force-pinned production keys, QML slider bands vs C++ clamps; updater is HTTPS-only + Ed25519 + SHA-256 + zip-slip/symlink guards + rollback.
- **Input** — DS4 button-mask packing + stick centering (no off-by-one), XInput→DS4 mapping, edge/hysteresis latches, `feedbacksender.c` edge semantics, remap locking.
- **Resource/lifetime hub** — `OrionAppController` threads joined before teardown, singleShot timers context-bound, handles closed on every branch, activity ring FIFO-capped; `VeniceNetClient`/`VirtualController`/`Ed25519`/`UpdaterArchive`/`SecurityManager`/`NetworkBridge` + the SHM/pipe/decoder layers all disciplined (reverse-order frees, joined threads, bounded mailboxes, atomic double-free guards).
- **Meter-delay controller** — no double-apply (NetworkBridge verbs not connected), keepalive under the service starvation watchdog, slew `static_assert`-pinned to the service cap, `readyForArm` has valid accept paths, single-threaded arm/disarm (no race).
- **Chiaki correctness** — ref-frame MRU fix, streaminfo readiness ordering, timedwait boundary re-check, d3d11va lifetime, Orion transport lock-order + wake-loss race closed, fail-closed queue never drops a terminal RELEASE.
- **pose_timing.py** — not in the shipping meter path (`no_meter.enabled=false`, no dev flag).

---

## 8. Trap / safety compliance

No reasoning from mtimes. Log analysis namespaced to the single 2026-08-11 session (`seq`/`physical_epoch` restart per session; ~27 sessions in the append-only log). Grep patterns anchored (no `abort`⊂`aborted=0`). Statistical claims cite n (headline session n=61 graded: 7 EARLY / 2 LATE / 52 EXCELLENT; non-phase per-source counts n<5, flagged directional). Nothing was modified; the app and service were not started/stopped; `run_orion.local.ps1`, `settings.json`, `learning.json` were read-only. Deployed Chiaki audited at the sibling `chiaki-ng-src` (branch `orion`), not the vendored copy.

---

## Appendix — raw per-agent findings
Full unabridged agent outputs are stashed in `.audit_raw/` (P1a, P1b, P2, P3, P4, P5, chiaki_fork, meter_delay_wiring, crosscutting_leaks) for anyone who wants the complete evidence trails behind each line above.
