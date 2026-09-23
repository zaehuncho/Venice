# P5: Launcher performance (Claude, internal wave 2026-09-22)

Lane: P5 Launcher performance. ID prefix `CL2-P5-`. This was a read-only review: no source, settings or git changes, no builds and no launches.

## Evidence base

- `logs/orion_native.log.1`: 16.78 MB, 63,740 lines, 2026-09-21 18:56Z to 2026-09-22 22:31Z. It covers 5 app sessions and 536 releases (424 of them in the 22:00-22:31Z drill on 09-22). `logs/orion_native.log` is the 23 KB continuation after rotation.
- `logs/orion_user.log` (3.8 MB), `D:\NexusVision\shot_records\` (4.5 MB) and `logs/diagnostics\` (1.6 GB, dev-only opt-in CSVs).
- Code read: `OrderedFileLogSink.*`, `OrionAppController.cpp` (appendLog/flushPendingLogs, timers, the 4 ms poll, the detection handler, the security refresh), `RemotePlaySession.cpp` (sidecar relay, preview pipeline), `SecurityManager.cpp` (periodic evaluation), `AutomationEngine.cpp` (UserFacingLog), QML pages and components, `remote_play_orchestrator.py`, `shot_records.py`, `stall_attributor.py`, `autogreen_sidecar.py` (priority) and `scripts/build_orion_sidecar.ps1`.
- Disk: C: shows 5.8 GB free (`df`), with 925 of 931 GB used.

### Measured baseline (owner rig, all sessions in `.log.1`)

| Signal | p50 | p90 | p99 | max | Notes |
|---|---|---|---|---|---|
| Sidecar detect_ms (Capture health, 5 s EMA) | 0.7 | 3.7 | 11.4 | 18.4 | 60 fps budget is 16.7 ms |
| DETECTOR HEALTH infer= (ms) | mode 7 | | | 159 | >16 ms in about 0.6 % of lines |
| Capture raw_gap_max_ms (per 5 s) | 20.7 | 24.0 | 70.9 | 207 | 61 windows >40 ms, clustered 09-21 23:08-23:15 while idle (source side, P3) |
| Sidecar callback_gap max (per 5 s) | 21.9 | 26.1 | 81.0 | 753 | same cluster |
| Sidecar emit to native receipt, per-minute max (`Telemetry stage split`) | 19.9 | 60.6 | 186.8 | 280 | per-sample p50 about 2 ms, p90 about 4 ms |
| QML render_tick_gap_max_ms (preview_pipeline) | 25.8 | 41.5 | 161.8 | 657 | biased toward unhealthy windows (5 s cadence when unhealthy, 60 s when healthy) |
| frame_age_ms at TIP RESERVATION (n=549) | 20.7 | 25.3 | 30.5 | 41.4 | what the engine actually decided on |
| Input pipe ack_wait_us (heartbeat) | 342 | | 1,191 | 25,602 | |
| Release codes | 533 live_meter_tip / 3 live_tip_fired_late | | | | none of the 3 falls in a >40 ms receipt window |

Bottom line: on the owner's rig, the load is not measurably costing shots today. The frame age at decision has p99 30.5 ms, and none of the 3 late fires coincides with a receipt spike. The risk is structural. Every piece of timing-critical, non-fire work shares one thread that runs at the lowest priority of the three timing processes, and that is what a weaker customer PC will expose. The findings below are ranked by customer impact.

---

### [CL2-P5-001] medium — The GUI thread carries passthrough input, detection receipt and the subtick, and runs at the lowest priority of the three timing processes
- Lane: P5
- Exploit / failure path:
  1. `inputPollTimer_` (4 ms, `Qt::PreciseTimer`) runs `pollPhysicalController()` on the GUI thread (`OrionAppController.cpp:4412-4418`). That function submits every passthrough output: the user's own Square press, the sticks, and the pipe/ViGEm writes (`OrionAppController.cpp:13480-13614`).
  2. Sidecar detections arrive by `QProcess` stdout. `RemotePlaySession::onSidecarStdout` (`RemotePlaySession.cpp:3831`) parses them and then calls `sidecarDetectionReady`, then `automation_.updateDetection()` and the subtick reschedule (`OrionAppController.cpp:2805-2860`), all on the GUI thread. The same thread runs QML's FrameAnimation preview clock, every `appendLog()` format and filter, the 200 ms log flush and the 1 s security status apply.
  3. Priorities: Chiaki is forced to `HIGH_PRIORITY_CLASS` (`OrionAppController.cpp:802`), and the sidecar raises itself to `ABOVE_NORMAL` (`autogreen_sidecar.py:1716-1729`). Venice sets nothing for itself. Only the precise-fire thread is raised (`THREAD_PRIORITY_TIME_CRITICAL`, `:1532`). Under CPU contention the Venice GUI thread (base 8) therefore loses to every sidecar thread (10) and every Chiaki thread (13).
  4. Measured stalls on this thread and its inputs: the per-minute max of sidecar emit to native receipt has p90 60.6 ms, p99 186.8 ms and max 280 ms. The render tick gap has p99 162 ms and max 657 ms. Five of the 27 minutes with receipt spikes >50 ms are the first minute after connect.
- Affected: `native_orion/src/OrionAppController.cpp`, `RemotePlaySession.cpp`, `main.cpp` (no priority set), the sidecar priority policy
- Impact: A stall on this thread delays the customer's own passthrough input, including the Square-down that starts the shot, and it also delays the detection samples that renew the fire lease. The owner's many-core rig absorbs it: the frame age at decision peaks at 41 ms. A 4-6 core customer PC running Discord or a browser, with the sidecar busy, is the case this ordering loses. Expect onset jitter and occasional late or declined fires there.
- Reproduction (high level): On a 4-core machine, or with CPU affinity limited to 4 cores, run a drill with a CPU-heavy background process. Compare the `Telemetry stage split` max and the `frame_age_ms` at TIP RESERVATION against this baseline.
- Required fix (low-risk first):
  1. Add a GUI-loop lag counter: the max and p99 gap between `pollPhysicalController` calls, appended to the existing heartbeat line.
  2. Raise the GUI thread to `THREAD_PRIORITY_ABOVE_NORMAL`. That is still below the sidecar's detect threads, so detection cannot be starved. A/B it with (1).
  3. Longer term, move the 4 ms poll and detection dispatch onto a dedicated automation thread. QML would then only read snapshots.
- Verification test: poll-gap p99 ≤ 6 ms and receipt per-minute max p99 < 40 ms on a 4-core affinity drill, with no drop in sidecar `cv_fps`.
- Confidence: confirmed (thread and priority layout are code-proven, stalls measured); probable (impact on customer hardware)

### [CX-015, known] — new performance evidence: a full disk turns into a controller freeze
- Lane: P5 (this adds evidence to CX-015; it is not a new ID)
- Failure path:
  - `OrderedFileLogSink::writeLosslessly` retries `open`/`flush` forever (`OrderedFileLogSink.cpp:205-247`).
  - `enqueue()` then blocks once 8 MB is outstanding (`:78-83`, `maxOutstandingBytes`).
  - `flushPendingLogs()` calls `enqueue()` on the GUI thread (`OrionAppController.cpp:15002`). The GUI thread blocks, so both the 4 ms passthrough and detection receipt stop.
  - After 6 s the freeze watchdog disarms and neutralises (`:4909-4930`), and the app goes to safe mode. Exit hangs in `stopAndDrain()`.
- Time to freeze at measured volume: about 21 min drilling (22.7 MB/h), about 1.5 h in normal play (about 5 MB/h), about 3 h idle-connected (2.5 MB/h).
- Other disk writers also fail badly on a full disk: `orion_user.log` keeps its unbounded pending list in memory (CL2-P5-002), and shot records log one error per shot (CL2-P5-003). The owner rig has 5.8 GB free on C:.
- Fix: never block `enqueue()`. Drop periodic telemetry first, keep audit lines within a bounded reserve, count the drops, and add a free-space probe at startup that warns the customer below about 1 GB.

### [CL2-P5-002] medium — `orion_user.log` does synchronous file I/O on the GUI thread every 200 ms and never rotates
- Lane: P5
- Exploit / failure path: `flushPendingLogs()` (GUI thread, every 200 ms) calls `userLog_.flushTo()` (`OrionAppController.cpp:15012`). Whenever lines are pending, that runs `QDir().mkpath`, `QFile::open(Append|Text)`, the writes and `close` synchronously (`AutomationEngine.cpp:269-285`). That happens around every shot (meter detected, meter gone, release accepted). A file close on Windows can trigger an antivirus scan, and this one blocks the thread that carries passthrough input and detection receipt. There is no size cap: the file is 3.8 MB and 53,265 lines since 2026-08-09, and it only grows. If `open` fails (disk full, ACL), `pending_` is kept and grows without bound (`:275-277`).
- Affected: `native_orion/src/AutomationEngine.cpp` (UserFacingLog), `OrionAppController.cpp:15012`, `%LOCALAPPDATA%\...\logs\orion_user.log`
- Impact: Periodic GUI-thread I/O jitter around shots, an unbounded file on the customer's C:, and unbounded memory when the disk is full.
- Reproduction (high level): Put a slow filter on the logs directory (or AV scan-on-close) and watch the GUI-loop lag counter from CL2-P5-001 around releases. Or fill the disk and watch process private bytes.
- Required fix: Give the user log its own `OrderedFileLogSink` instance with `maxFileBytes` of about 1-2 MB (that reuses the worker thread and rotation), and cap `pending_` (drop oldest).
- Verification test: A unit test shows `flushTo` does no filesystem call on the calling thread, and the file rotates at the cap. A disk-full test shows bounded `pending_`.
- Confidence: confirmed (code and file evidence); probable (the size of the jitter)

### [CL2-P5-003] medium — Shot records are ON by default, write to a hardcoded `D:\NexusVision\shot_records`, fsync every shot, and have no retention
- Lane: P5 (the privacy and data angle belongs to L5)
- Exploit / failure path: `ShotRecorder.create()` treats a missing `ORION_SHOT_RECORDS` as `'1'` (`shot_records.py:219-222`). The default root is `D:\NexusVision\shot_records` (`:233-236`). The writer thread appends and calls `os.fsync` on every record (`:853-868`). No native, installer or packaging code sets `ORION_SHOT_RECORDS` or its root (grep of `native_orion/`, `installer/`, `tools/package_orion_release.py` and `tools/release_filter_policy.py` is empty), and `remote_play_orchestrator.py:1578` imports it statically, so the compiled sidecar should include it. Evidence on this rig: 572 `SHOT RECORD` lines in `.log.1`, and 4.5 MB of JSONL on D:.
- Affected: `shot_records.py`, `remote_play_orchestrator.py:1570-1586`, the customer's D: drive
- Impact: On a customer PC with a D: drive (data disk, USB stick, optical or network drive), Venice creates a folder there without asking and grows it forever, with one fsync per shot. Removable or slow media add I/O stalls; the writer is off the detect thread, but it adds GIL and disk pressure. On PCs with no D:, the feature silently disables itself. Behaviour therefore differs by machine for no reason.
- Reproduction (high level): Run the packaged sidecar on a machine with a D: drive and no env override, take a shot, and inspect `D:\NexusVision\shot_records`.
- Required fix: Default the recorder to OFF in the production build (only the dev launcher sets it). If it must ship, root it under `%LOCALAPPDATA%\NexusVision\Orion Native\shot_records`, drop the per-record fsync (flush each record, fsync on close), and cap total size.
- Verification test: In a packaged-sidecar smoke run with no env, no `shot_records` directory is created anywhere, and `SHOT RECORDS armed` is absent from the log.
- Confidence: confirmed (code and dev-rig output); probable (that the shipped sidecar does the same)

### [CL2-P5-004] medium — The full security evaluation runs every second (Authenticode, manifest signature, per-file probes), then re-notifies about 90 QML properties unconditionally
- Lane: P5
- Exploit / failure path:
  1. `refreshTimer_` fires every 1 s (`OrionAppController.cpp:4382`) and calls `periodicSecurityEvaluator_.request()`, which runs `SecurityManager::evaluate()` on the security worker (`SecurityManager.cpp:354+`). Each run does all of the following:
     - `WinVerifyTrust` on the running exe (`:727-748`), which hashes the whole PE (3.6 MB);
     - reads, parses and Ed25519-verifies the release manifest (`:479+`);
     - `canonicalFilePath` plus `stat` plus a `CreateFileW`/`GetFileInformationByHandleEx` per manifest entry (the hash cache avoids re-hashing but not the per-file opens; `:68-110`);
     - takes a process snapshot, runs the VM and sandbox registry probes, and decrypts the entitlement.
  2. On completion, `applySecurityStatus()` always emits `statusChanged()` (`OrionAppController.cpp`, end of `applySecurityStatus`), which re-evaluates every QML binding on the 90 `NOTIFY statusChanged` properties on the GUI thread every second, even when nothing changed.
- Affected: `SecurityManager.cpp` (`evaluate`, `authenticodeSignedButInvalid`, `verifyReleaseIntegrity`), `OrionAppController.cpp` (`refreshSecurity`, `applySecurityStatus`)
- Impact: A steady CPU and I/O tax in the same process as the timing thread, plus a 1 Hz burst of binding re-evaluation on the GUI thread. The cost has not been measured. It is paid every second for the whole session, including during shots. Re-running Authenticode on the running image every second adds no security, because Windows refuses write opens on a mapped running image.
- Reproduction (high level): Time `evaluate()` (a QElapsedTimer around it, logged every 60 s) on a packaged build with the real manifest.
- Required fix:
  1. Run Authenticode once at startup, plus on explicit request.
  2. Cache the manifest parse and signature result by the manifest's (size, mtime, ChangeTime).
  3. Keep the 1 Hz check for cheap signals only (debugger, process list) and move per-file probes to about every 10 s.
  4. Emit `statusChanged()` only when a field actually changed.
- Verification test: A log shows `evaluate()` duration p99 under a few ms at steady state, and a QML test counts one `statusChanged` per real change. The kill/lock fail-closed tests (StrictSecurity) must still pass.
- Confidence: confirmed (cadence and calls are code-proven); speculative (cost magnitude)

### [CL2-P5-005] low — Log volume and retention: a 1 Hz heartbeat is 22 % of the log, and only about 1.5-3 h of play survives rotation
- Lane: P5
- Failure path, by emitter (share of bytes in `.log.1`):

  | Emitter | Share | Detail |
  |---|---|---|
  | `Input hook heartbeat` | 22 % (3.72 MB, 11,441 lines) | every 1 s while the hook is enabled, even idle (`OrionAppController.cpp:13965-13975`). Its `max_us` is a lifetime max (598 on every line of a session), so it carries no per-window information |
  | `DETECTOR HEALTH` | 9 % (1.54 MB) | every 2 s at ERROR level, idle included |
  | `preview_stats` | 5 % | |
  | Capture health | 4.7 % | |
  | per-shot lines | remainder | about 15 KB per shot, about 70 lines |

- Rates:
  - idle-connected: 42.5 KB/min (2.5 MB/h)
  - drill: 378 KB/min (22.7 MB/h)
  - normal play (about 3 shots/min, projected from the idle and per-shot figures): about 5 MB/h
- Retention: rotation keeps only `.log.1` (`OrderedFileLogSink.cpp:189-198` deletes `.2`), so at most 32 MB, which is about 85 min of drilling. The owner already has to "read .log.1".
- Affected: `OrionAppController.cpp` heartbeat, `simple_meter_reader.py:13280` DETECTOR HEALTH cadence, `OrderedFileLogSink` rotation
- Impact: Disk writes, GUI-thread formatting (every line goes through `appendLog`, `simplified()` and the ring filters on the GUI thread), and support evidence that is lost after a long session.
- Required fix:
  - Heartbeat: emit every 30 s, plus immediately when `failures`, `ack_failures`, `connected` or `ack_winerr` change; make `max_us` windowed.
  - DETECTOR HEALTH: every 10 s while no press is active.
  - Rotation: keep 3 generations (rename `.1` to `.2` to `.3`).
- Verification test: Idle-connected volume under 0.5 MB/h. A rotation unit test keeps 3 generations. The heartbeat still appears within 1 s of a pipe failure.
- Confidence: confirmed

### [CL2-P5-006] low — The 300-character relay cap hides the performance fields that would explain stalls
- Lane: P5
- Failure path: `onSidecarStderr` forwards `trimmed.left(300)` (`RemotePlaySession.cpp:2875+`). In `.log.1`, `raw_stage` (capture mode), `source_skip`, `raw_gap_event_ms`, `preview_dup_refresh` and `core_black_run` appear 0 times, and so does DETECTOR HEALTH's `lifecycle(state=`. The comments at `remote_play_orchestrator.py:4103-4110` already record this.
- Impact: The fields that attribute a capture stall to a stage never reach the log, so the 09-21 23:08-23:15 stall cluster (raw gap up to 207 ms, callback gap up to 753 ms) cannot be explained after the fact.
- Required fix: Raise the cap for `Capture health` and `DETECTOR HEALTH` to about 640, or split each into two lines.
- Verification test: `grep -c raw_gap_event_ms` > 0 after a session.
- Confidence: confirmed

### [CL2-P5-007] low — Capture health prints `gc2=0 gcms=0 stall=0/0` while the stall attributor is off, so "no data" reads as "healthy"
- Lane: P5
- Failure path: `ORION_STALL_ATTRIB` defaults to 0 (`stall_attributor.py:38-52`). The Capture health line still prints zeros from an empty dict (`remote_play_orchestrator.py:4119-4127`). There are 0 `GC:` lines in `.log.1`.
- Impact: A gen-2 GC hitch looks exactly like a GC-free session. Whoever triages a stall is misled.
- Required fix: Print `gc2=off` when the instrument is off. Keep the near-free `gc.callbacks` timing always on (it does no sampling and no stack walk), and leave only the 5 ms sampler opt-in.
- Verification test: A shipped-config log shows real `gc2` counts, or `off`.
- Confidence: confirmed

### [CL2-P5-008] low — No memory telemetry, so growth over a long session cannot be verified
- Lane: P5
- Failure path: No RSS, working-set or private-bytes figure is logged by native or by the sidecar (grep shows none). The bounded structures that were checked are fine: `logs_` and `customerLogs_` hold 1000 lines each, the hash cache 4096 entries, and `meterBoxRing_` is bounded. The unbounded ones are `UserFacingLog::pending_` on failure (CL2-P5-002) and the sidecar, which was not audited.
- Required fix: Add `priv_mb` and `ws_mb` to the 60 s `preview_pipeline` line (`GetProcessMemoryInfo`), and the same to the sidecar's Capture health line (ctypes; no psutil).
- Verification test: A 3 h soak shows flat `priv_mb` after warm-up.
- Confidence: confirmed (absence)

### [CL2-P5-009] low — The Debug page's LogViewer rebuilds the whole raw ring on every 200 ms log beat while the bot runs
- Lane: P5
- Failure path: `DebugPage.qml:350` binds `text: orion.logText`, so every `logsChanged` triggers a C++ join of the 1000-line raw ring. `LogViewer.qml` then splits it and rebuilds `shownLines`, and the `ListView` model is a JS array, so each change fully resets the model. The raw ring changes many times per shot. RemotePlayPage was throttled on 09-21 (1.5 s, text compare); the Debug page was not.
- Impact: Only while a customer has Debug open during play, but then it is multi-ms GUI-thread work every 200 ms on the timing thread.
- Required fix: Bind `text` only when `filterMode === "all"`, and apply the same 1.5 s throttle and text compare as `syncCaptureLogModel`.
- Verification test: With Debug open during a drill, GUI-loop lag stays equal to the RemotePlayPage baseline.
- Confidence: confirmed (code); speculative (magnitude)

### [CL2-P5-010] low — Startup: about 6.6 s from controller construction to the first preview frame, 4.1 s of it opening and attesting the capture card
- Lane: P5
- Evidence (session 09-21 20:51):

  | Time (s) | Event |
  |---|---|
  | 37.452 | first controller line |
  | 38.220 | sidecar spawned |
  | 39.886 | detector loaded |
  | 44.040 | capture mode attested, first frame |
  | 44.073 | first shm preview frame |
  | 49.05 | preview at 58-60 fps |

  The time from process start to the first controller line is not logged. The first minute after connect also has most of the receipt spikes (CL2-P5-001).
- Required fix: Log a process-start timestamp (`GetProcessTimes`) with the first controller line. Show a "Opening capture card..." stage in the UI during the 4 s gap. The card-open profile itself belongs to P3.
- Confidence: confirmed

## Checked and cleared (negative results)

- **`TIP TOKEN KILL site=subtick_fire_at_past` (10 events, overdue 8.6-18.9 ms)** is not a GUI stall. In each checked case, `shot_gate_release release_ms` equals the arm time plus the ETA, so the precise-fire thread fired on time. The GUI thread only noticed on the next sidecar payload, which is at most one frame plus processing. The oracle proxy was green on the shots checked (epochs 88, 19, 136, 48).
- **The detector is within budget:** detect_ms p99 11.4 ms, infer mode 7 ms, and gen-2 GC impact is unknown (see CL2-P5-007). Preview `encode_ms` is 0 (the shm transport works). `shm_write_ms` has a mean of 0.26 ms.
- **The `orion_native.log` disk I/O is off the GUI thread** (`OrderedFileLogSink` worker). The GUI thread only formats lines and runs the `appendLog` filters (about 26 case-insensitive `contains` per line at about 10-30 lines/s).
- **QML:**
  - `StarBackdrop` (Canvas at 30 fps) is not instantiated anywhere, and `VeniceBackdrop` is static.
  - `meterMetricsChanged` is cadence-throttled.
  - The Chiaki embed timer is off in QML-render mode.
  - The activity feed is throttled to 1.5 s while live.
  - The infinite pulse animations only run while the stream is live, when the scene graph renders every frame anyway.
- **The sidecar stdout path** coalesces stale preview lines, so there is no per-line memmove.
- **Framedump and detframes CSVs** are opt-in (`ORION_FRAMEDUMP`, `_detcsv_enabled`). The 1.6 GB in `logs/diagnostics` is dev output, not a shipped behaviour.
- **The capture stall cluster on 09-21 23:08-23:15** was idle, with `raw_gap_max_ms` also high. It is source- or machine-side (lane P3), not Venice render load.

## Verdict

**needs changes.** Nothing blocks launch. Today's load does not measurably cost shots on the owner's rig (frame age at decision p99 30.5 ms, 3 of 536 fired late, none tied to a stall). But the timing-critical GUI thread is the lowest-priority timing thread on the machine, and several avoidable costs sit on it or next to it.

Top 5 fixes, in priority order:
1. **CL2-P5-001:** add the GUI-loop lag counter to the heartbeat, then A/B `THREAD_PRIORITY_ABOVE_NORMAL` on the GUI thread (a 4-core affinity drill decides it).
2. **CX-015 (known):** make log `enqueue()` non-blocking on a full disk, and add a free-space warning at startup. This is the only path here that turns into a controller freeze.
3. **CL2-P5-003:** default shot records OFF in production, or root them under `%LOCALAPPDATA%` with a cap and no per-record fsync.
4. **CL2-P5-002:** move `orion_user.log` onto an `OrderedFileLogSink` worker with rotation, and bound `pending_`.
5. **CL2-P5-004:** run Authenticode once, cache manifest verification, move per-file probes to about every 10 s, and emit `statusChanged` only on change.
