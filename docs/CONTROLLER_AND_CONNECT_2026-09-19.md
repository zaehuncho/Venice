# Controller UI isolation, disconnect audit, connect latency — 2026-09-19

Three owner-reported defects, in the owner's words:

1. *"when using the controller you can see it moving on the pc hovering over stuff"*
2. *"no disconnect bugs"*
3. *"instant connect when connecting"*

Everything below is either **MEASURED** (a number from a real log or a test), **PROVEN**
(the offending code is quoted and the mechanism is argued), or **SUSPECTED / UNVERIFIED**
(said so explicitly). Nothing here was confirmed on the live rig — the app was never
launched. Read the *Still needs the rig* section before trusting any of it in production.

Line numbers are from the working tree on 2026-09-19. `OrionAppController.cpp` and
`remote_play_orchestrator.py` were being edited concurrently by other agents, so treat
line numbers there as approximate and grep for the tag instead: every change carries
`[ORION_CONTROLLER_UI_ISOLATION 2026-09-19]`, `[ORION_DISCONNECT_AUDIT 2026-09-19]` or
`[ORION_CONNECT_LATENCY 2026-09-19]`.

---

## 1. Controller input driving the Windows UI

### Root cause

The pad is not being read by Venice's UI. A **pad mapper on the PC** — Steam Input's
desktop configuration, or DS4Windows / DualSenseX — translates the DualSense's HID
reports into synthetic mouse and keyboard input with `SendInput()`, and Windows delivers
that to whatever window is focused, which during a session is Venice.

This was already known and partly defended: `OrionAppController::nativeEventFilter`
(`src/OrionAppController.cpp`, the `windows_generic_MSG` branch) consumed mapped input
while a short guard window was open. **The shipped guard could not possibly cover the
owner's symptom**, for two independent reasons:

* **It never classified pointer motion or the wheel.** `DesktopUiInputKind` had exactly
  three values — `Other`, `NavigationKey`, `MouseButton` — and
  `shouldSuppressControllerMappedUiInput` answered only for the last two. The visible
  symptom the owner describes — the cursor *moving* and *hovering* over controls — is
  `WM_MOUSEMOVE`, which was never a candidate for suppression no matter how wide the
  guard was. `WM_MOUSEWHEEL` likewise: a mapped stick scrolling the wheel over a QML
  `Slider` or `ComboBox` changes its value.
* **The guard was only opened by a press EDGE.** `controllerUiActivityPressEdge` looks at
  buttons, D-pad and triggers only. A mapper drives the pointer from an **analog stick**,
  which produces no edge at all — so the one input class that causes the symptom was also
  the one class that never armed the defence.

A third, narrower hole: the guard is armed by `pollPhysicalController()` on a 4 ms timer,
so a mapped message dispatched before the next poll observed the edge slipped through.

**Second, separate contributor (not fixed, by design):** on the ViGEm delivery route
(direct pipe disabled) Venice's own virtual XUSB pad mirrors gameplay to the desktop
(`OrionAppController.cpp`, the `shouldNeutralizeDesktopVirtualPad(...)` branch). A mapper
sees *that* pad as an Xbox controller and maps it too. It is only neutralised while the
hook owns input, which is correct — neutralising it on the ViGEm route would kill input
delivery. The message filter is the only defence available there.

### Fix

`src/ControllerRoutingPolicy.h`

* `DesktopUiInputKind` gains `PointerMotion` and `WheelScroll` (appended, so the existing
  `AutomationEngineTests.cpp` coverage of the old predicate still compiles and passes).
* New `DesktopUiInputOrigin { Unknown, Hardware, Injected }`.
* `kControllerUiStickDeflection = 24` (line ~372) — the reader's `normalizeHidAxis()`
  already zeroes `|axis| < 8`, so this only has to clear resting jitter; ~19 % deflection
  is deliberate movement.
* `kControllerUiPointerGuardMs = 220` (line ~375) — deliberately longer than the 120 ms
  press guard because a mapped pointer keeps emitting motion after the stick recenters.
* `controllerUiPointerActivity(lx, ly, rx, ry)` (line ~377) — level-triggered stick test.
* `shouldIsolateDesktopUiInput(...)` (line ~417) — the one decision, split by cause:
  * not streaming, or `controllerUiModeActive` → never suppress;
  * `kind == Other` → never suppress;
  * **origin == Injected → suppress every UI-driving kind, with no timing window**;
  * otherwise → the shipped 120 ms press guard for keys/clicks (it literally calls
    `shouldSuppressControllerMappedUiInput`, so the two can never diverge) and the new
    pointer guard for motion/wheel.

`src/OrionAppController.cpp`

* `currentDesktopUiInputOrigin()` (anonymous namespace, line ~120) resolves
  `GetCurrentInputMessageSource` from `user32.dll` by `GetProcAddress` (so the build does
  not depend on the SDK's `WINVER`) and maps `IMO_INJECTED` → `Injected`,
  `IMO_HARDWARE` → `Hardware`, everything else (`IMO_SYSTEM`, `IMO_UNAVAILABLE`, failure)
  → `Unknown`. **Fails open**: an unavailable API can only fall back to the timing guards.
* `nativeEventFilter` (line ~5203) classifies `WM_MOUSEMOVE` / `WM_NCMOUSEMOVE` /
  `WM_MOUSEHOVER` as `PointerMotion` and `WM_MOUSEWHEEL` / `WM_MOUSEHWHEEL` as
  `WheelScroll`, resolves the origin only for UI-driving kinds, and calls the new decision.
* `pollPhysicalController` (next to the existing press-edge guard) re-arms
  `controllerUiPointerGuardUntilMs_` on every poll a stick is deflected.
* Constructor (line ~2147) reads the two knobs once.
* `OrionAppController.h` — new members `controllerUiPointerGuardUntilMs_`,
  `controllerUiPassthrough_`, `controllerUiInjectedIsolation_`, next to the existing
  `controllerUiGuardUntilMs_`.

### Why the injected-source leg is the real fix

It is an exact discriminator rather than a heuristic: a mapper is a user-mode process
using `SendInput()`, which the OS tags `IMO_INJECTED`; the owner's real mouse and keyboard
are `IMO_HARDWARE`. It needs no timing window, so it also closes the 4 ms poll race. The
timing guards remain as the fallback for the case where the API is unavailable or a mapper
injects at a level that reports as hardware.

### Knobs

| env | default | effect |
|---|---|---|
| `ORION_CONTROLLER_UI_PASSTHROUGH=1` | off | disables the isolation entirely — the explicit "deliberate controller-driven UI" mode, and the way back to pre-2026-09-19 behaviour |
| `ORION_CONTROLLER_UI_INJECTED_ISOLATION=0` | on | keeps the timing guards, stops trusting the injected verdict |

Neither "Fix Controller" (`qml/pages/RemotePlayPage.qml` — a plain button click) nor the
first-run tour (`qml/components/FirstRunTour.qml` — real-keyboard `Keys.onPressed`) is
controller-driven, and the isolation only engages while a session is live, so nothing
deliberate is broken today. `ORION_CONTROLLER_UI_PASSTHROUGH` is the hook a future
configure-controller mode should set.

### UNVERIFIED

* **That the owner's mapper actually injects via `SendInput`.** Highly likely for Steam
  Input desktop configuration, but I could not verify it on the rig. If it turns out to
  report as hardware, the pointer/press guards still cover it — that is why both legs ship.
* Whether the OS cursor still *visibly* moves. It will: the cursor is drawn by the OS.
  Suppressing the message stops Venice reacting (no hover, no click, no wheel), it does
  not freeze the pointer.
* Whether a suppressed motion can leave a *stale* hover lit (hover set by the real mouse,
  then blocked motions). Cosmetic; not handled.

---

## 2. Disconnect / teardown audit

Full path inventory was produced by a dedicated read-only audit; the summary below keeps
what matters. **The native stop/teardown state machine is in good shape.** The three races
one would expect are genuinely closed, each by an independent guard:

* **Stop during recovery read as a spontaneous exit — RULED OUT.** `stop()` sets
  `intentionalSidecarRestart_` before `stopSidecar()`; `shouldRecoverUnexpectedSidecarExit`
  *also* requires `!remotePlayTeardownActive_`, which `disconnectRemotePlay` sets before
  `remotePlay_.stop()` runs; and `stop()` clears `sidecarRestartPending_` and bumps
  `inputRecoveryGeneration_`.
* **Watchdog restarts fighting an intentional stop — RULED OUT.**
  `sidecarRestartPending_` is the cancellation token and `stop()`/`start()` both clear it.
* **The input retry reconnecting after a user stop — RULED OUT.**
  `fireScheduledInputSessionRetry` re-checks attempt, lifecycle generation, intent, safe
  mode, teardown, shutdown, and the live state. `OrionInputClient` has no self-driven
  reconnect at all: `ensureConnected()` is reachable only from `sendDetailed()`, whose call
  sites are Running-gated or ownership-gated, and `reassertLastState()` refuses to connect.

Structural note worth keeping: there is **no `stopRequested_` flag anywhere**. The Sept-18
work added observability only (the two `emit setupMessage` lines in `stop()` and
`stopSidecar()`). Correctness rests entirely on ordering — which is why the fixes below are
about restoring ordering invariants, not about adding new state.

### PROVEN and FIXED

| id | severity | what | where |
|---|---|---|---|
| F1 | HIGH | `shutdown` could not be **read** while a session command was running | `native_orion/backend/autogreen_sidecar.py`, `remote_play_orchestrator.py` |
| F2 | HIGH | the PS5 disconnect handshake ran after up to 3 s of preview joins | `native_orion/backend/autogreen_sidecar.py`, `remote_play_orchestrator.py` |
| F3 | MED | sidecar `finished` had no process-identity guard | `src/RemotePlaySession.cpp` |
| F4 | MED | `disconnectRemotePlay` never revoked the engine's armed gate | `src/OrionAppController.cpp` |
| F5 | MED | `inputRecoveryPending_` survived a generic session error | `src/RemotePlaySession.cpp` |
| F6 | LOW-MED | a capture preview silently cancelled a pending watchdog restart | `src/RemotePlaySession.cpp` |
| F7 | LOW | the deferred handoff timer had no generation token | `src/RemotePlaySession.{h,cpp}` |
| F8 | LOW | `restartSidecarWithWindowContainment` ignored the teardown flag | `src/OrionAppController.cpp` |

**F1 — `shutdown` is undeliverable during a session command.** `recover_input` and
`start_stream` ran *on the stdin reader thread*. `recover_input_link()` paces three
attempts against a 17 s deadline; `start_stream` owns the whole promotion. While either
ran, nothing else could be read from stdin — including `shutdown`. The native writes
`shutdown`, waits `kSidecarGracefulShutdownMs` (5 s, `SidecarWatchdog.h`) and then
`taskkill /F /T`. `QProcess::terminate()` does not help (on Windows Qt posts `WM_CLOSE`,
which a console Python process never turns into SIGTERM, so the sidecar's SIGTERM handler
is dead code) and `closeWriteChannel()` does not help (`_stdin_loop_wrapped` deliberately
refuses to stop on EOF). So pressing Disconnect during input recovery — **exactly when the
owner would press it, because input is dead while video is still live** — force-killed the
sidecar 5 s into a 17 s handler: `chiaki_session_stop()` never ran, so the console reported
the transport vanishing ("LAN cable disconnected"), and `orch.stop()` never ran, so the
Elgato handle stayed held and the 3 s post-disconnect preview resume could land on a
contended device.

*Fix:* both commands are handed to a single-flight bounded worker
(`_deferred_cmd_queue` / `_deferred_command_worker` / `_defer_command`) so the reader stays
free. Order between the two offloaded commands is preserved (one worker, FIFO); `shutdown`
now jumps ahead of them, which is the point. Both were already asynchronous from the
native's point of view (it waits for `{"event": "promote"/"started"/"error"}` and
`input_recovery/ready` against its own deadlines), so nothing downstream gains a new race.
`recover_input_link()` gained a `self._running` check at every attempt boundary so a
teardown aborts it instead of launching a replacement client underneath the close.

**F2 — the handshake ran too late to fit the budget.** `orch.stop()` has always put the
graceful Chiaki close first *within itself*, with an explicit comment about the 5 s budget.
That reasoning was defeated one level up: `autogreen_sidecar.py`'s `finally:` joined the
preview worker (2.0 s) and the preview-stats worker (1.0 s) **before** calling `stop()`,
so on a slow join the `WM_CLOSE` went out at t≈3 s and its own 3 s wait ran past the
force-kill.

*Fix:* new `RemotePlayOrchestrator.close_remote_play_client()` — the handshake alone,
idempotent, and it publishes `self._running = False` / clears `_input_link_ready` first so
anything on the session-command worker sees the stopping intent. The sidecar's `finally:`
calls it before the preview joins. This is a **reorder, not new work**: `orch.stop()`'s own
call is now a no-op. Worst-case total teardown is unchanged (~16.5 s); what changed is that
the one step with an external, non-recoverable consequence now runs first.

**F3 — retired-generation sidecar exit.** The `QProcess::finished` lambda was the only one
of the three `proc` lambdas with no identity check (`errorOccurred` and `started` both have
one), and it nulls `sidecarProcess_`, latches `rejectLateSidecarStarted_` and emits
`sidecarExited`. The normal teardown is *not* affected — `stopSidecar()` nulls the pointer
before it waits, so an in-call `finished` sees `nullptr` and must still run the full path.
The exposure is a force-killed process (exactly F1's shape) whose exit lands after the
600/3000 ms respawn installed a **new** sidecar: every later `sendSidecarCommand` silently
fails, the new sidecar's `started` is discarded, and a spurious exit is emitted at the new
session. *Fix:* early return, expressed as
`sidecarExitBelongsToRetiredGeneration(haveLive, liveIsThisOne)`.

**F4 — armed across the deferred teardown.** `disconnectRemotePlay()` called
`disarmPreciseFire()` + `automation_.reset()` but never `automation_.setArmed(false)`, and
there is no `syncEngineArmed()` in it. Every sibling teardown does both
(`releaseFailedRemoteInputRoute`, `tripWatchdog`, the `sidecarExited` handler,
`prepareForApplicationExit`). On the deferred path — the user's own Disconnect click —
`remotePlay_.stop()` is one `singleShot(0)` away, so for one event-loop turn the 4 ms input
poll still runs with `state == Running`, the pipe connected and ViGEm plugged, and a Square
held at the instant of the click could start a fresh owned shot after the user asked to
disconnect. *Fix:* `automation_.setArmed(false)` first, like everyone else.

**F5 — stuck `inputRecoveryPending_`.** The `input_recovery`/`error` branch clears the
recovery pair; the **generic** `error` branch never did. The session leaves Running with
the flag set, and because `inputRecoveryDeadlineApplies()` requires a Running session the
20 s deadline no-ops, so nothing clears it until the next `start()`/`stop()`/exit. While
stuck, `recoverInputLink()` refuses at its own guard **and**
`inputLinkRecoveryAction`'s `inputOnlyRecoveryPending` short-circuit suppresses the whole
escalation ladder. *Fix:* clear the pair (and bump the generation) in the generic branch.

**F6 — preview steals a pending restart.** `startCapturePreview()` did not clear
`sidecarRestartPending_`. After `restartSidecar()` → `stopSidecar()` the process is gone
and the state is Disconnected, so the preview's guards all pass; it spawns a *preview*
sidecar, and the restart timer then finds a running process and logs "Sidecar start
skipped" — the recovery is silently downgraded. *Fix:* clear it and say so in the log. This
one was rated SUSPECTED by the audit (I could not construct a guaranteed trigger); the fix
is strictly safer than the previous behaviour either way.

**F7 — unguarded deferred handoff timer.** The only lifecycle timer in
`RemotePlaySession.cpp` with no generation/intent token — `state_ == Connecting` is a
level, not an identity. No live failure was found; it is the guard that has to exist the
moment a third Connecting-producing path appears. *Fix:* new `sessionIntentGeneration_`,
bumped by `start()` and `stop()` **only** — deliberately not `streamPromoteGeneration_` or
`inputRecoveryGeneration_`, which `QProcess::finished` also bumps and would therefore
cancel a handoff that is legitimately waiting for exactly that process to die.

**F8 — restart vs teardown, defence in depth.** All six call sites are independently gated
today, so this is not a live bug — but this is the function that re-arms the embed watchdog
`disconnectRemotePlay()` just stopped and respawns the sidecar, i.e. the shape of "I pressed
Disconnect and it came back". *Fix:*
`sidecarRestartAllowedDuringLifecycle(shutdownActive, teardownActive)`.

### SUSPECTED, NOT FIXED

* **F9 — the post-disconnect preview resume is not generation-guarded**
  (`QTimer::singleShot(kSidecarRestartDelayMsCaptureCard, …startCapturePreview())`). Safe
  today only because both `startCapturePreview()` implementations re-check state. A Connect
  that fails to Error with a dead sidecar inside the 3 s window opens a preview underneath
  the retry machinery — probably intended, but nothing expresses that.
* **F10 — the ViGEm pad is not unplugged on an unexpected sidecar exit.** The
  `sidecarExited` handler neutrals the route but never calls
  `controller_.disconnectController()`. Correct while a restart is expected to succeed;
  if the bounded restarts exhaust, the pad stays plugged with the session reading
  Disconnected. **Deliberately not changed** — unplugging on every unexpected exit would
  break the restart path, and the right fix is to unplug only on *exhaustion*, which needs
  a live-rig decision.
* **F11 — forensics gap.** After `taskkill /F` no `atexit` runs, so the sidecar's
  `_EXIT_REASON` marker is never written: the most common abnormal exit is the one with the
  least evidence. F1/F2 reduce how often it happens; they do not close the gap.
* **`delayedAutoReconnectStillCurrent` does not take `remotePlayTeardownActive_`.**
  Unreachable today because the only setter of that flag also bumps the generation — an
  invariant held by convention, not by the predicate.

### Not a bug (checked, ruled out)

* A stop is **never** misclassified as a crash: a clean commanded shutdown returns 0, and a
  force-kill is still covered by `intentionalSidecarRestart_` on both deliberate paths.
* `inputRecoveryPending_` and `sidecarRestartPending_` can both be true transiently after a
  force-kill, but neither clears the other's work and there is no deadlock.
* The lifecycle generation **is** bumped on every manual Connect and Disconnect — even a
  *refused* Connect bumps, which is conservative and correct.
* The precise-fire worker re-validates the live route under `submitMutex_`, so it cannot
  press after the session leaves Running.

---

## 3. Connect latency

### Measured, from the real logs

Corpus: `logs/orion_native.log` + `.log.1` (2026-09-18 00:14 → 09-19 02:48) and the
August `orion_native_TIP_*` rotations. 37 user-initiated connects, 28 reached Running;
**12 on the current build, 10 reached Running**. `%LOCALAPPDATA%\NexusVision\Orion Native\`
holds nothing from this era.

**The rig is capture-card mode, so there are two independent sequences and they must not be
added.** The capture preview starts automatically at app boot, before any Connect; Chiaki
carries **input only**. Therefore "Running → first frame" is ~0 by construction — when the
Connect press lands the preview pipeline is already thousands of frames in.

**Path B — Connect press → Running (current build, n=10, ms):**

| stage | median | min | max |
|---|---|---|---|
| native prep (press → sidecar handoff) | **264** | 242 | 328 |
|  · settings saved → streaming preset | 15 | 8 | 26 |
|  · streaming preset → window containment | **152** | 146 | — |
|  · containment → Connecting | 23 | 14 | 78 |
|  · Connecting → promotion started | 70 | 63 | 92 |
| console wake (`wake_check`) | 1 | 1 | 11 |
| host resolve (`host_resolve`) | **0** | 0 | **2440** |
| standby claim | 150 | 2 | 334 |
| standby promote (`open <host>`) | 304 | 195 | 424 |
| client boot | 22 | 13 | 52 |
| **console handshake** | **494** | 281 | 569 |
| sidecar `start_stream` total | 1073 | 839 | 3266 |
| **TOTAL press → Running** | **1324** | **1067** | **3519** |

Distribution: `1067, 1074, 1269, 1291, 1293, 1354, 1583, 1632, 3370, 3519`.
Excluding the two `host_resolve` outliers (n=8): min 1067, **median 1292**, max 1632.

**The ~1.2 s figure reproduces — nothing regressed on the press path.** The August build
(n=18) had a median of 1690 ms and carried a hard ~2585 ms stall between the click and a
second `Connecting`; that stall is gone.

**Rest mode: zero samples.** Every one of the 37 attempts found the console awake
(`wake_check` 1 ms median). The rest-mode wake line never appears in any log.
`standby=1` in `start_stream timing` means the **pre-booted standby Chiaki client** was
promoted — it does **not** mean the PS5 was in standby. **The wake path was therefore not
measured and was not touched.**

**Path A — app launch → first presented frame (n=11): 7163 ms median**, of which
**4071 ms (median) is the capture-card open** — the window between `SHM preview reader
opened` and the first frame reaching the detector, in which not one log line is emitted by
the capture path. Unchanged from the August build. This is the biggest single number in the
whole connect experience and it is **completely uninstrumented**; sub-dividing it needs
stamps inside `capture_card_backend.py`, which was out of scope here.

### Fixed

**The `CONSOLE ADDRESS DRIFT` prewarm TTL race — worth ~2.4 s on ~20 % of connects.**
`remote_play_client.py`. `prewarm_console_host()` short-circuited on "the entry is still
fresh", so a refresh could only **start** once the entry had already expired. The
orchestrator ticks it every 30 s against a 120 s TTL, which leaves the cache empty from
expiry until the next tick finishes its ~2.4 s lookup — up to ~32 s of every 120 s, ~27 %
of the wall clock. A Connect landing there pays `_console_host_lookup()` synchronously.

Live proof:

```
2026-09-19T02:33:23.578Z  CONSOLE ADDRESS DRIFT: … console at 192.168.137.126   <- prewarm
2026-09-19T02:35:34.181Z  Chiaki Remote Play settings saved.                    <- click, +130.6 s
2026-09-19T02:35:37.550Z  start_stream timing: total=3103ms … host_resolve=2440ms
2026-09-18T18:28:06.028Z  …  ->  18:28:09.546Z  total=3266ms host_resolve=2422ms
```

*Fix:* `_CONSOLE_HOST_PREWARM_REFRESH_AFTER_S = 45.0` plus `_console_host_prewarm_age()`.
The prewarm now re-resolves while the answer is still **valid**, so the cache never empties
between ticks. 45 s sits above the 30 s tick (so a tick is not a refresh every time) and far
enough below the 120 s TTL that a ~2.4 s lookup always lands before expiry (worst-case
refresh start 75 s, fresh again by ~78 s). **The served answer is never older than the TTL** —
only the refresh *start* moved. `prewarm_console_host()` now returns `"refreshing"` when an
entry is still servable and `"started"` only when the cache was genuinely cold.

*Before → after:* 2 of 10 measured connects paid 2422/2440 ms; both would now have been
served from cache. Expected effect on the distribution: the 3370/3519 ms tail collapses to
~1.0–1.1 s, taking the mean from 1745 ms to ~1300 ms with the median unchanged at ~1.3 s.

**Overlap: window containment now runs after the promotion IPC.** `OrionAppController.cpp`,
`connectRemotePlay()`. `setChiakiEmbedVisible(true)` was the single most expensive thing on
the native half — **152 ms median (min 146, n=11)** between `Streaming preset:` and
`Capture-card input window contained …`. It is Win32 surgery on another process's window
(`findChiakiWindow()` sweeps up to 8 times, `SetParent()` blocks on the stream client's
message loop) and **nothing in it is an input to the console handshake**, while
`remotePlay_.start()` only writes the promotion command to the live sidecar's stdin.
Swapping the two overlaps ~175 ms of local work with the ~1073 ms already in flight.

Nothing regresses on containment: `chiakiEmbedWatchdog_` starts immediately afterwards and
re-runs the identical containment every tick for the whole session (35 s of fast ticks),
and the deferred call still happens on the same event-loop turn — before any sidecar reply
can be processed. In capture-card mode `chiakiEmbedStatus_` is never `"Embedded"`, so no
Connecting-path code reads a value this reorder could change (checked every consumer).

**Expected combined effect: median ~1324 ms → ~1150 ms, and the 3.4–3.5 s tail removed.
UNVERIFIED — this is arithmetic on measured stage times, not a new measurement.**

### Instrumentation added (so the next measurement is direct)

Stage (a), "click → handler entry", was previously unmeasurable: the first line any connect
produced was `Chiaki Remote Play settings saved.` from *inside* the handler.

* `RemotePlaySession::beginConnectStopwatch()` (`RemotePlaySession.h`) + `connectStopwatch_`
  / `connectStopwatchArmed_` / `sessionIntentGeneration_` members.
* `OrionAppController::connectRemotePlay()` logs **`Connect pressed.`** and starts the
  stopwatch immediately after the re-entrancy guard — this is t=0.
* `RemotePlaySession::start()` logs `Connect stage: native_prep=<n>ms (click -> session start)`.
* The promotion ack logs `… (handoff=<n>ms)` — the boundary where our half ends and the
  sidecar's own `start_stream timing:` line begins, so the two halves become addable.
* `RemotePlaySession::setState()` logs `Connect stage: total=<n>ms click -> Running|Error|Disconnected`
  on the first terminal transition out of Connecting. Diagnostic only; nothing branches on it.

### Deliberately NOT changed

* **The PS5 rest-mode wake path.** No log in the corpus contains a rest-mode wake, so it
  could not be measured, and the wake-first budget was left exactly as it is.
* The console handshake (494 ms median) — that is the PS5's own response time.
* `standby_promote` (304 ms) and `standby_claim` (150 ms) — real IPC to the pre-booted
  client; reducing them needs a design change, not a reorder.
* The 4071 ms capture-card open — biggest number available, but it is Path A (app boot, not
  Connect) and instrumenting it means editing the capture backend.

---

## Tests

| suite | command | result |
|---|---|---|
| `OrionInputRetryTests` (native) | `ctest -C Debug -R OrionInputRetryTests` | **30 passed, 0 failed** (10 new slots) |
| `OrionRemotePlayPathTests` (native) | `ctest -C Debug -R OrionRemotePlayPathTests` | passed |
| `tests/test_remote_play_teardown_ordering.py` (new) | `pytest -q` | **9 passed** |
| `test_remote_play_client_stale_sweep.py` | `pytest -q` | **14 passed** (3 new) |
| connect/lifecycle/diagnostics python set | `pytest -q` | **83 passed** |

New native tests live in `native_orion/tests/InputSessionRetryPolicyTests.cpp` (it already
included `ControllerRoutingPolicy.h`, and the target links Qt Core only):

* `pointerMotionWasInvisibleToTheShippedPressGuard` — the regression anchor for defect 1.
* `stickDeflectionOpensThePointerGuardAndPressEdgesDoNot`
* `pointerGuardSuppressesMotionAndWheelWhileStreaming`
* `injectedOriginSuppressesEveryUiKindWithNoTimingWindow`
* `injectedIsolationKnobOffLeavesOnlyTheTimingGuards`
* `isolationNeverEngagesOutsideASessionOrInPassthroughMode`
* `pointerGuardOutlivesTheStickByTheMapperSmoothingTail`
* `aRetiredSidecarExitIsIgnoredOnlyWhenANewerOneIsLive` (F3)
* `deferredHandoffNeedsBothTheIntentAndTheConnectingState` (F7)
* `aSidecarRestartMayNeverRaceATeardownOrShutdown` (F8)

Build: throwaway `C:/Users/aaron/obld_u` mirroring `native_orion/build/CMakeCache.txt`
(VS 17 2022 / x64, Qt 6.8.0 msvc2022_64, `ORION_BUILD_TESTS=ON`, `ORION_PRODUCTION=OFF`,
`ORION_REQUIRE_OPENCV=OFF`). `OrionNative` and the test targets build clean.

**Known unrelated failure:** `tests/test_remote_play_frame_pipe.py::test_decoder_capture_loop_waits_once_and_has_no_poll_pacing`
fails in the working tree. It exercises `_capture_loop`, which none of these changes touch;
`git diff` shows that function was modified by concurrent work (stall attributor, Y-plane
skip). Flagged, not investigated.

---

## Still needs the live rig

1. **Defect 1.** That a pad-mapped pointer/click/wheel no longer reaches Venice's controls
   during a session, and that the owner's real mouse is untouched. Watch for the one-shot
   line `Controller UI isolation active: PS5 gameplay input cannot activate Venice controls.`
   If the symptom persists, the mapper is not reporting as injected: check whether the line
   appears at all, then rely on the pointer guard (and tell me the mapper's name).
2. **Defect 2, F1/F2.** Press Disconnect **during** an input recovery and confirm the
   console does *not* report a cable/LAN error and the capture preview resumes without
   "no live device". The sidecar should log the teardown reaching `{"event":"stopped"}`
   rather than dying to `taskkill`.
3. **Defect 2, F4.** Hold Square and press Disconnect in the same instant; no shot should be
   owned after the click.
4. **Defect 3.** One connect with the new lines in place gives the whole breakdown directly:
   `Connect pressed.` → `Connect stage: native_prep=` → `… (handoff=)` →
   `Sidecar: start_stream timing:` → `Connect stage: total=`. Expect native_prep ≈ 110 ms
   (was 264 ms) and total ≈ 1.15 s, with no 3.4 s outliers over a dozen connects spread
   across several minutes (the TTL race needed a click ≥120 s after a prewarm).
5. **Rest mode.** Nothing here was validated against a resting console because no log
   contains one. Please do one rest-mode connect and confirm the wake budget is intact.
