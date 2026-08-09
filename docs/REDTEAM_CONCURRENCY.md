# Red-team: concurrency / threading race audit (native + Python)

Read-only adversarial audit of the whole app's threading model. Scope: native C++/Qt
(`native_orion/src/`) and the Python sidecar (`remote_play_orchestrator.py`,
`autogreen_sidecar.py`, `capture_card_backend.py`, `simple_meter_reader.py`,
`controller_remap.py`). A race here = corrupted JSONL IPC, a lost/duplicated meter
sample, or a mistimed/double fire for a paying customer.

Date: 2026-07-17. Method: full read of the Python capture/detector/emit threads +
two focused native sub-audits (RemotePlaySession/watchdog, AutomationEngine/fire-path).
Each finding labelled **SOFT** (re-testable / assumption-dependent) vs **HARD** (definite),
with the concrete interleaving and a specific fix. Known-fixed items (#2 frame_count gate,
#4 unlocked stdout writers, #7 stop() cross-thread cv2 release) are confirmed, not re-reported.

---

## Threading model (established before hunting)

**Native (single serialized engine thread + one fire thread):**
- `AutomationEngine automation_` and `RemotePlaySession remotePlay_` are value members of
  `OrionAppController` (a GUI-thread QObject). **No `moveToThread` anywhere** in `native_orion/src`.
  The sidecar `QProcess` is `new QProcess(this)` — lives on the GUI thread; `readyReadStandardOutput`
  → `onSidecarStdout` → `handleSidecarMessage` run on the **GUI event loop**. There is **no stdout
  reader thread**. So the meter-update path (`updateDetection` / `reevaluateScheduleOnFreshSample`)
  and the fire-decision path (`process()` on the 4 ms `inputPollTimer_`) are the **same thread**,
  serialized by the event loop. `AutomationEngine` contains zero mutex/atomic — consistent with
  single-thread ownership.
- **`OrionPreciseFireThread`** (`OrionAppController.cpp:841-975`), `TIME_CRITICAL` — the only thread
  that writes the release to the pad. Coordinates via its own `std::mutex m_` and `owner_->submitMutex_`.
  Touches the engine only via `automation_.engineNowMs()`.
- **`guiFreezeThread_`** (`:2378-2395`) — GUI-freeze watchdog; on a >6 s stall calls
  `automation_.setArmed(false)` + a neutral pad submit under `submitMutex_`.
- `OrionRawInputWorker` — HID pump; own `QMutex`, posts to GUI via `Qt::QueuedConnection`.

**Python (four+ threads over shared orchestrator attributes, GIL-serialized bytecode):**
- **capture thread** (`_capture_loop`) — writes `_last_frame`, `_last_frame_hash`, `_last_frame_ts`,
  `_last_frame_epoch_ms`, `_last_y_plane`, `_last_pts`, `_last_frame_pts`, `_frame_seq`, `_frame_count`,
  PTS offsets.
- **processing/detector thread** (`_processing_loop`) — reads the frame tuple, runs `detect()`, writes
  `_last_meter_present`, `_last_raw_fed`, `_last_meter_track`, `_last_meter_bbox`, `_last_green_window`,
  `_shot_gate_deadline_seq`.
- **input-router thread** (`_input_router_loop`) — reads pad, writes `_shot_gate_*`, calls
  `_arm_shot_gate` / `pose_timing.notify_shot_start`, drives `_remap_engine.process`.
- **telemetry thread** (sidecar `_telemetry_loop`) — reads all `_last_*` + `_remap_engine._shot`, emits JSONL.
- **stdin thread** (sidecar `_stdin_loop`) — `update_meter`/`update_remap`/`release_marker`/`calibrate_meter`,
  mutating the detector and remap engine.
- **preview-encoder thread** — reads `_preview` under `_preview_lock` (correctly).

Because native collapses update-vs-fire onto one thread, the classic native races are mostly
**negatives**; the real native findings are narrow and centre on the GUI↔fire-thread handoff.
The Python side has genuine cross-thread shared-state reads that the GIL makes *non-crashing* but
not *consistent*.

---

## Findings (ranked)

### P1 — MED / HARD — Torn (frame, timestamp) snapshot → frame_age underestimated → early fire
`remote_play_orchestrator.py`: `frame = self._last_frame` snapshotted at **:2078**, but
`_frame_age_ms = max(0.0, (time.perf_counter() - self._last_frame_ts) * 1000.0)` is computed at
**:2527**, *after* `detect()` (which the code itself measures at ~6–40 ms, cv_fps 24–57).

Interleaving: processing thread snapshots `frame` (frame N) at :2078 and enters `detect()`.
During the 6–40 ms detect, the **capture thread** commits a fresh unique frame N+1 and advances
`self._last_frame_ts` (`:1778`). The processing thread then reads the *live* `self._last_frame_ts`
at :2527 — the **N+1** capture time — while the sample it is feeding is from frame N. Result:
`frame_age_ms` is computed too small (can approach 0), so `update_cv_data(frame_age_ms=…)` (:2544)
and `_rtt_engine.observe_frame_age_ms` (:2532) under-report staleness. The engine folds a too-small
staleness into its release lead → it believes the meter sample is fresher than it is → fires slightly
**early**. The same non-atomic snapshot affects `_last_y_plane` (read :2134) and the epoch/pts used by
`_frame_measurement_epoch_ms()` (:2124) — those windows are small (pre-detect) but on the compressed-reader
path a skewed `_last_y_plane` pairs frame N's BGR with frame N+1's luma for one frame.

Note the capture thread's own comment (`:1774-1777`, "committed together so they can never skew")
guarantees consistency *within the capture thread* only; the **processing thread reads these fields at
different instants**, so they skew relative to the snapshotted `frame`.

- **Severity MED. HARD** (definite interleaving; probability rises with detect latency — exactly when
  the CPU is loaded during a shot, i.e. the worst time).
- **Fix:** at the top of the processing iteration, snapshot the whole frame tuple atomically —
  `frame, frame_ts, y_plane, frame_pts, frame_epoch = (self._last_frame, self._last_frame_ts,
  self._last_y_plane, self._last_frame_pts, self._last_frame_epoch_ms)` (a short GIL-atomic read burst,
  ideally under a small `_frame_lock` the capture thread also holds during its commit block at
  :1771-1798) — then compute `_frame_age_ms` and `_frame_measurement_epoch_ms` from the **snapshot**,
  never the live attributes.

### N1 — MED / SOFT — Double-fire: `reevaluateScheduleOnFreshSample` re-arms a shot the fire thread already released (tick-lock nudge)
`AutomationEngine.cpp:4219-4224, 4376-4392` + `OrionAppController.cpp:6042-6048`.

The GUI tick arms the fire thread with `armDeadlineMs = tickAlignedFireDeadlineMs(...)` (`:6043`),
bounded to ±½ console tick (~±8.3 ms) off the engine's stored `schedFireDeadlineMs_`; the engine keeps
the **un-nudged** deadline. When tick-lock pulls the armed deadline earlier, the fire thread presses at
`t_fire < schedFireDeadlineMs_`, sets `fired_=true`/`armed_=false`, but `confirmScheduledFire` only runs
on the *next* GUI tick's `takeFired()` (`:6002-6003`). In that gap a fresh meter sample lands →
`reevaluateScheduleOnFreshSample`:
- the grace guard at `:4221` returns only if `confirmed` (not yet) or `now >= deadline + graceMs` — but
  `now ≈ t_fire < schedFireDeadlineMs_`, so it does **not** return;
- the earlier-only clamp at `:4376` (`fireAtMs >= schedFireDeadlineMs_`) doesn't block because a fresh
  crossing near the tip yields `now < fireAtMs < schedFireDeadlineMs_`;
- → `clearScheduledFire()` + `scheduleFire()` mints a **new token** → the next GUI tick sees a new
  `schedToken` (`:6026`) → `fireThread_->arm(...)` again → **second physical press for the same shot**
  (pump-fake / double shot). The first fire's confirm is dropped (`:4133`).

Without tick-lock the fire always lands at `t_fire >= schedFireDeadlineMs_`, so `:4376` blocks the
reschedule — safe. `tickLockEnabled` is documented **default-OFF** (`OrionAppController.cpp:6036`).

- **Severity MED. SOFT** (armed only when the tick-lock flag is enabled; re-testable by enabling it).
- **Fix:** make the engine's stored deadline equal what was actually armed — either add a setter called
  right after `arm()` to write the nudged deadline back into `schedFireDeadlineMs_`, or move the
  `tickAlignedFireDeadlineMs` nudge *inside* `scheduleFire` so engine and fire thread agree. Then the
  grace guard and earlier-only clamp reflect the true fire instant. Defense-in-depth: gate the reschedule
  on `fireThread_->firedUnconsumed()`.

### N2 — MED/LOW — SOFT — TOCTOU: last-moment disarm ignored during the ≤1.2 ms fire spin; re-arm can double-press
`OrionAppController.cpp:932-970`.

The fire loop re-checks `armed_ && token_==token` under `m_` only until it **claims** the fire at
`:943` (`armed_ = false`), then unlocks and **spins with no lock for up to ~1.2 ms** (`:947-953`) before
`controller_.submit` (`:957`). After the spin there is **no re-check** of `armed_`/`token_`/`quit_`.

- Missed abort: GUI thread calls `fireThread_->disarm(token)` (meter vanished → `clearScheduledFire`,
  `:6050`) after `:943`. `disarm()` sees `armed_==false` → no-op. The shot fires anyway ~1.2 ms after
  the engine tried to abort. `quit_` at shutdown is likewise ignored mid-spin.
- Double: a GUI re-arm of a *new* token during the spin sets `armed_=true`; the thread submits the stale
  claimed `out`, loops, and fires the new token too — feeds N1.

- **Severity MED/LOW. SOFT.**
- **Fix:** after the spin, re-acquire `m_` and re-verify `!quit_ && token_==token` (and not-aborted)
  *before* `controller_.submit`; abort if superseded. The spin itself is unchanged, so sub-ms precision
  is preserved.

### P2 — MED / SOFT — Unlocked detector mutation while `detect()` runs
`remote_play_orchestrator.py` `update_meter` (:853) and stdin `update_remap` (:924-930), on the **stdin
thread**, call `det.reload_config` / `det.set_active_style` / `det.reset_tracking` on the live detector.
The **processing thread** is concurrently inside `self._meter_detector.detect(frame, …)` (:2137). The
detector (`SimpleMeterReader` / `MeterDetector`) has **no internal lock**.

`reload_config` → `reset_tracking` → `_reset_state` (`simple_meter_reader.py:1138-1163`) reassigns ~20
fields non-atomically, including `_vel_hist = deque(...)`, `_fillable_hist = []`, `box=None`, `tmpl=None`.
`detect()` reads these across many bytecodes; a concurrent reset yields a **half-reset read** (e.g. new
empty `_vel_hist` with an old `box`/`tmpl`), producing a garbage fill/velocity for that frame — which can
drive a mistimed release — or a raised exception that the processing loop's `try/except` swallows,
**dropping that frame's meter sample**. `update_meter` also nulls `meter_hsv_low/high` (:874-875) while
detect may read them.

- **Severity MED. SOFT** (only on a live meter color/style change from the Meter tab mid-session; rare and
  user-initiated; blast radius ≈ one to a few frames).
- **Fix:** route config changes through a one-shot flag the processing thread applies at the top of its
  own loop (before snapshotting the frame), or guard `detect()` and all `reload_config/set_active_style/
  reset_tracking` with a shared `_detector_lock`.

### N3 / P-native — LOW / SOFT — Non-atomic `bool armed_` written cross-thread by the freeze watchdog
`AutomationEngine.h:1539` (`bool armed_ = true;`), written by `guiFreezeThread_` via `setArmed(false)`
(`OrionAppController.cpp:2387`), read by `process()` (`:1529`) and `reevaluateScheduleOnFreshSample`
(`:4187`). A plain `bool` written by the watchdog thread and read by the GUI thread is a formal data
race (UB by the C++ memory model). Benign in practice: the write only fires after a >6 s GUI freeze (so
`process()` isn't concurrently live), the write is a single retired store on x86, and the watchdog's own
coordination flags (`guiHeartbeatMs_`, `guiFreezeTripped_`, `watchdogThreadStop_`) are already `std::atomic`.

- **Severity LOW. SOFT.**
- **Fix:** make `armed_` `std::atomic<bool>` (relaxed ordering), matching the atomics beside it. Zero
  behavior change on x86; removes the UB and documents the single-writer intent.

### P3 — LOW / SOFT — Torn telemetry snapshot mixing frame N / N+1 fields
`autogreen_sidecar.py` `_telemetry_loop` reads, unlocked, `orch._remap_engine._shot.fill_pct` (:562, via
`shot`), `orch._last_meter_present` (:616), `orch._frame_count` (:602), `orch._last_meter_bbox` (:672),
`green`, etc. — while the processing thread writes `_last_meter_present`/`_last_meter_bbox` (:2429-2435)
and `_remap_engine.update_cv_data` writes `_shot` (under the remap `_lock`, but the telemetry read does
**not** take that lock). Individual field reads are GIL-atomic, but the assembled payload can carry
`frame_count` from frame N+1 with `bbox`/`fill` from frame N. `frame_count` is the native dedup key, so
the engine may accept a "fresh" frame_count paired with a slightly stale fill/bbox.

- **Severity LOW. SOFT** (fill changes slowly across one 16 ms frame; overlay bbox join is cosmetic; the
  fire-relevant fill/green are close-consistent). Worth noting because it undermines the frame-id join
  guarantees the comments claim.
- **Fix:** have the processing loop publish a single immutable snapshot dict/namedtuple per frame that the
  telemetry loop reads atomically (one attribute read), instead of the telemetry loop scraping ~20 live
  attributes.

### P4 — LOW / SOFT — `notify_release` mutates calibrator + reads `last_fill` concurrently with `detect()`
`mark_release` (orchestrator :2604) runs on the **stdin thread** (native `release_marker` cmd) and calls
`det.notify_release` (:2638). `SimpleMeterReader.notify_release` (:1320-1334) calls
`self._calibrator.note_release()` and reads `self.last_fill` / writes `_gz_fill_at_release` — while the
**processing thread** is inside `detect()`, which also mutates the calibrator and `last_fill`. Concurrent
mutation of `ColorCalibrator` state across two threads. Guarded by `try/except`, once-per-release, and only
live with `ORION_GREEN_SELF_GRADE` / an active calibrator.

- **Severity LOW. SOFT.**
- **Fix:** fold the release relay into the same one-shot-flag/`_detector_lock` mechanism proposed in P2 so
  all detector mutation happens on the processing thread.

### P5 / N4 — LOW / SOFT — Multi-writer coordination flags (benign, note for hardening)
- Python: `_shot_gate_deadline_seq` / `_shot_gate_hw_deadline_seq` written by input-router (`_arm_shot_gate`),
  stdin (`arm_pose`), and processing (CV self-arm, :2159-2161); `set_shot_state` / `pose_timing.notify_shot_start`
  called from those same threads while the processing thread runs `detect()` / `pose_timing.update()`. These
  are simple atomic attribute writes under the GIL (no corruption), but `pose_timing.update()` vs
  `notify_shot_start()` is genuine concurrent object mutation — same class as P2, lower blast radius.
- Native: `fired_`/`firedToken_`/`firedActualMs_` are a **one-deep** handoff (`OrionAppController.cpp:880-898,
  967-969`); two fire-thread arms between two GUI ticks overwrite the first confirm. Physically two real
  shots <4 ms apart is impossible, so this only manifests as a *symptom* of the N1 double-arm.
- **Fix:** subsumed by P2 (Python) and N1 (native). If defense-in-depth is wanted, make the fired handoff a
  small queue / assert `!fired_` at write.

---

## Confirmed already-fixed (not re-reported)

- **#2 frame_count stall-emission gate** — confirmed. The RC-2b stall watchdog synthesizes a no-meter state
  on both sides: orchestrator `_processing_loop` idle branch (`:2054-2070`) and sidecar `_apply_stall_override`
  (`autogreen_sidecar.py:80-99`), keyed off `pixel_age_ms` (last **unique** frame), so a frozen echo can't
  keep serving a held fill.
- **#4 unlocked stdout writers** — confirmed. One process-wide `STDOUT_EMIT_LOCK`
  (`remote_play_orchestrator.py:24`); `emit_stdout_jsonl` (:27-31) wraps every orchestrator-side writer
  (pose_landmark, pose_overlay, calibrate_meter_status), and the sidecar adopts the same lock via
  `_adopt_shared_stdout_lock` (`autogreen_sidecar.py:43-59`) before any orchestrator writer can run
  (called in `main()` after import, before `orch.start()`).
- **#7 stop() cross-thread cv2 release** — confirmed. `CaptureCardBackend._run`'s `finally` owns
  `cap.release()` (`capture_card_backend.py:547-559`); `stop()` (`:669-696`) never releases a wedged handle
  from the non-reader thread — it abandons it into `_wedged_cap` and lets the reader/process-exit reclaim it.

---

## Explicit negatives (investigated, not races)

- **Native meter-update vs fire-decision** — both `updateDetection`/`reevaluateScheduleOnFreshSample` and
  `process()` run on the GUI thread via `AutoConnection` slots, serialized by the event loop.
  `AutomationEngine` has no locks because it is single-thread-owned. NEGATIVE (except the narrow fire-thread
  confirm gap in N1).
- **RemotePlaySession telemetry fields** — every writer is `handleSidecarMessage` on the GUI thread; every
  reader is on the GUI thread. The multi-field telemetry is bundled into one by-value `DetectionResult` and
  emitted once (`RemotePlaySession.cpp:2110-2202`), so no reader sees fill from N with frame_count from N+1.
  NEGATIVE. (Caveat: the `QString` getters `captureTier()`/`telemetry()` copy a COW refcount non-atomically —
  a latent heap-corruption trap **iff** anyone ever reads them off-thread; safe as wired today.)
- **Fire thread reading `engineNowMs()`** (`:965`) — reads only `clock_` (a `QElapsedTimer` started once,
  never restarted) + test-only `testClockMs_`; touches no field `process()` mutates. NEGATIVE.
- **`measuredLatencyMs_` torn read** — `AutomationEngine.h:1508`, read (:4271, `processHolding`) and written
  (`updateDetection`/learning) all on the GUI thread; the fire thread never reads it. NEGATIVE.
- **In-tick fire vs scheduled fire double-press** — mutually exclusive via `schedFireDeadlineMs_`
  (`AutomationEngine.cpp:2157-2162`); on grace-expiry both write the *same* idempotent release and the GUI
  `disarm(token)` retires the lagging arm. NEGATIVE.
- **Pump-fake re-press guard** — fire thread sets `fired_=true` while holding `submitMutex_` (`:955-970`);
  GUI tick re-checks `firedUnconsumed()` under `submitMutex_` (`:6080-6088`) and coalesces the hook write via
  atomic `lastFireHookWriteUs_`. Correct handshake. NEGATIVE.
- **Watchdog restart / window creation** — `restartSidecar` → synchronous `stopSidecar` →
  `QTimer::singleShot(startSidecar)` is a deferred GUI-thread timer, not a concurrent thread; cannot overlap
  `onSidecarStdout`. `runBoundedEnumeration` (`SidecarWatchdog.h:119-148`) owns its worker state via a
  `shared_ptr` that outlives both parties. `stopSidecar` nulls `sidecarProcess_` before `waitForFinished`
  and `onSidecarStdout` guards on it. NEGATIVE.
- **Python RemapEngine** — `process`, `update_config`/`set_config`, `update_cv_data`, `update_network_offset`
  all take `self._lock` (`controller_remap.py:522-567`), so the stdin `update_remap` vs input-router
  `process` mutation is properly serialized. NEGATIVE.
- **Python green-grade bridge** — `_on_green_grade` (processing) and `pop_green_grade` (telemetry) both hold
  `_green_grade_lock` (`remote_play_orchestrator.py:790-810`). NEGATIVE.
- **Python `_frame_ready_evt` edge-consume** — `_telemetry_wait` waits then clears; telemetry reads
  latest-wins, so a coalesced/cleared signal at worst costs one extra identical-schema emit, never a lost
  meter sample. NEGATIVE.

---

## Priority

1. **P1** (MED/HARD) — snapshot the frame tuple in `_processing_loop`; the post-`detect()` `frame_age`
   read against the live `_last_frame_ts` biases release lead early. Only Python-side finding that is a
   definite interleaving on the default path.
2. **N1** (MED/SOFT) — reconcile the engine's stored deadline with the tick-lock-nudged armed deadline to
   close the double-fire; guarded today only by `tickLockEnabled` default-off.
3. **N2** (MED/LOW/SOFT) — re-verify token/quit under `m_` after the fire spin so a last-moment disarm
   (meter-vanish / shutdown) is honored.
4. **P2** (MED/SOFT) — serialize detector mutation (`update_meter`/`reload_config`) against `detect()`.
5. **N3, P3, P4, P5/N4** (LOW) — atomic `armed_`; single-snapshot telemetry; fold `notify_release` onto
   the processing thread; hardening of one-deep `fired_` handoff.
