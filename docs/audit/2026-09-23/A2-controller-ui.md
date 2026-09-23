needs changes

# A2 — Native controller, input client and UI

Read-only source audit, rechecked 2026-09-23 02:48 UTC. No source/settings edits, builds, deployment, live-service calls or application launch. Existing test executables only were run. The source tree was already dirty and Claude's concurrent revisions were retained. This report proposes changes; it does not apply them or approve a release.

Reviewed the audit prompt, AGENTS.md, master M-01..M-41 report, prior Codex reliability report, roadmap and variance documents. Healthy fire-to-wire measurements do not establish failure-path liveness or correctness during configuration transitions. No accuracy improvement percentage is claimed here.

### [AUD-A2-001] high — Changing video source during a session changes its timing profile without changing the video producer
- Kind: bug
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\qml\components\StreamSetupForm.qml:232-250` leaves `videoSourceCombo` enabled during Running/Connecting; its setter is also unguarded at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:10710-10737`. `switchActuationLeadVideoSource` immediately swaps the saved source-specific lead; `syncBackendConfig` applies it to the engine at `:15290-15293`. In contrast, `C:\Users\aaron\Desktop\NexusVision\native_orion\src\RemotePlaySession.cpp:779-840` only pushes audio/remap changes. The source is encoded at sidecar launch (`:1985`, `:2495-2509`), not in `update_remap`; its handler at `C:\Users\aaron\Desktop\NexusVision\native_orion\backend\autogreen_sidecar.py:3170-3228` changes remap/meter configuration, not the capture backend. The Setup page has no enclosing active-session disable (`C:\Users\aaron\Desktop\NexusVision\native_orion\qml\pages\DashboardPage.qml:60-74`). This is a new transition defect, not M-15's device qualification or M-33's persisted-index issue.
- Customer impact: A user can select Remote Play while HDMI continues supplying the frames, or vice versa, and instantly run that existing feed against the other route's Shot Lead. The UI advertises the new route although the running producer has not changed. Wrong timing, a disabled timing lane, or confusing health/recovery behavior can follow; an actual game miss was not measured in this audit.
- Proposed change: Treat source changes as a route transaction. For the smallest beta fix, reject them natively while Connecting/Running/Disconnecting and disable the selector with an explanation to disconnect first. Preserve the active lead until the old route stops. In preview-only mode, invalidate/retire any affected preview before publishing the new source; do not treat logical Disconnected as proof that no sidecar exists. Avoid a UI-only fix because other setter callers remain possible.
- Verification: Add a controller/QML event-loop test with a fake sidecar and different source-specific leads. Attempt both source directions while Running, Connecting and Disconnecting; assert either an explicit refusal with unchanged engine/feed identity, or an ordered disarm -> stop -> source/lead commit -> new feed qualification. Include warm preview and an in-flight shot; assert no frame from the old source receives the new source's timing profile. The 52 passing static UI tests below do not cover this transaction.
- Confidence: confirmed

### [AUD-A2-002] medium — Selecting another capture device before Connect can promote the old preview device instead
- Kind: bug
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\qml\components\StreamSetupForm.qml:271-285` permits choosing a new device while a warm preview exists. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:10740-10748` saves the index but never retires/reopens that preview. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\RemotePlaySession.cpp:996-1018` decides warm reuse from capture-card *category* only. Its `start_stream` command contains console information, not a new device index (`:2054-2071`); `ORION_CAPTURE_CARD_INDEX` is injected only on process launch (`:2504`). `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SidecarWatchdog.h:259-262` has no device/FPS/config identity in the reuse predicate. FPS edits have the same reuse limitation, although their setter does at least log that they apply on the next sidecar launch (`OrionAppController.cpp:10765-10767`). New evidence beyond M-33: even an intentional, correctly enumerated selection can fail to take effect on the next Connect because the old sidecar is reused.
- Customer impact: On a two-device PC, a user corrects the selection in Setup and presses Connect, but Venice keeps detecting the previous device. The selected device and actual feed disagree; choosing the right card appears ineffective. Repeated warm promotions preserve the mismatch until the process is actually restarted.
- Proposed change: Snapshot the active preview's launch identity (at minimum device index/identity and requested FPS) and permit warm promotion only when it matches the requested configuration. On mismatch, follow the existing asynchronous teardown plus capture-handle-release delay and cold start. Mark pending versus active settings truthfully until that completes. Keep same-identity promotion fast.
- Verification: Fake two capture devices. Open preview A, select B while logically Disconnected, then Connect; assert A is closed, one B producer is launched after the release delay, and no A frames are accepted into B's timing namespace. Repeat an FPS change and unchanged-configuration promotion; only the unchanged case may reuse the existing process. This does not require a live console.
- Confidence: confirmed

### [AUD-A2-003] high — ABANDON revision still has an unbounded cancellation-completion wait
- Kind: reliability
- Evidence: New evidence on M-03 / CL2-P2-003 and the prior reliability report's “Client ABANDON notice”: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionInputClient.cpp:662-668` now replaces `WaitForSingleObject(..., INFINITE)` with `GetOverlappedResult(pipe_, &ov, &written, TRUE)` after `CancelIoEx`. A TRUE wait has no timeout parameter and the pipe close at `:634` occurs only after this function returns. Thus the comment claiming a bounded fire path is not implemented. Existing read/write cancellation branches also retain INFINITE waits (`:317-322`, `:592-599`). The revised counter at `:671-673` correctly counts only a fully completed ABANDON write; the earlier unconditional-counter defect is no longer reported.
- Customer impact: Under stalled cancellation completion, the input/precise-fire caller can remain blocked past its advertised 3/25/65 ms limits. A fork dead-man may neutralize input, but it does not restore the blocked launcher or make its shutdown deadline finite. This is a confirmed missing software deadline, not evidence that such a kernel delay occurred in the reported customer disconnect.
- Proposed change: Move pending operation buffers/OVERLAPPED/event lifetime to an owned asynchronous I/O object or dedicated I/O worker. On deadline, revoke route authority and return a terminal pending/fault result without freeing still-referenced memory; retire and reap the operation separately. Apply the same ownership/deadline design to read, normal write and ABANDON paths. Merely replacing TRUE with FALSE or returning after a bounded wait while stack storage is still referenced would introduce a memory-lifetime defect.
- Verification: A local Windows pipe/cancellation test must delay cancellation completion beyond the caller deadline and exercise normal completion, cancellation, completion-winning-the-race and peer exit. Assert bounded caller/GUI responsiveness, no stack-buffer use after return, one terminal result and truthful ABANDON count. Rebuild the native fixture first: the existing 51-test binary predates this revision. No exploit or remote-target test is required.
- Confidence: confirmed

### [AUD-A2-004] medium — Copy Activity Log discards the new timeout result and can omit the current failure silently
- Kind: reliability
- Evidence: New follow-on to M-02 after its bounded-drain revision: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:10633-10648` casts the 500 ms drain result to void, then prefers any nonempty disk tail. The in-memory ring is used only when that tail is empty (`:10650-10655`). `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrderedFileLogSink.cpp:129-136` explicitly returns false when queued bytes have not reached disk. Therefore an older nonempty log suppresses the available current ring precisely when recent writes fail. The older unbounded-drain/per-chunk-admission findings are not repeated: current source has a bounded default drain and a single per-batch admission deadline.
- Customer impact: A customer clicks Copy immediately after an error on full/failed storage, receives a success message, and sends support a log ending before the error. Diagnosis of the very storage/input failure that prompted copying becomes unreliable.
- Proposed change: Retain the drain result. On timeout/storage fault, copy the sharing-redacted in-memory ring with an explicit “disk log incomplete; recent session events follow” marker, optionally alongside a separately labelled older disk tail. Preserve the existing redaction on both sources. Report partial status instead of unconditional disk-tail success. Also inspect the sink's dropped counters because an empty queue alone cannot prove that all recent batches were retained.
  Minimal proposed implementation (report only):

  ```diff
  --- a/native_orion/src/OrionAppController.cpp
  +++ b/native_orion/src/OrionAppController.cpp
  @@
  -    (void)appLogSink_.drain(std::chrono::milliseconds(500));   // [M-02 r2] bounded on a dead disk
  +    const bool drained = appLogSink_.drain(std::chrono::milliseconds(500));
  +    const auto logStats = appLogSink_.stats();
  +    if (!drained || logStats.storageFault || logStats.droppedBatches != 0) {
  +        clipboard->setText(
  +            QStringLiteral("Disk log incomplete; recent session events follow.\n")
  +            + ui_notifications::serializeActivityLogForSharing(logs_));
  +        appendLog(QStringLiteral("Partial activity log copied from the session ring; disk logging is incomplete."));
  +        return;
  +    }
  ```
- Verification: A fixture starts with an old nonempty disk log, injects write failure, appends a new distinctive customer error and invokes Copy. Assert clipboard contains the new error, a partial-log marker and no secret-bearing raw values, with a bounded operation. A second case must cover dropped batches followed by an otherwise successful drain. No clipboard interaction with the user's active application was performed here.
- Confidence: confirmed

## Concurrent repair rechecks — not duplicate findings

- **M-11 / CL-006:** source now arms a first-record clock at sidecar creation (`RemotePlaySession.cpp:2276-2280`) and `SidecarWatchdog.h:44-59` ages never-reported health after a 20 s cold-start grace. `OrionInputProtocolTests.cpp:1505-1523` adds a pure-policy test. The earlier “ages nothing forever” claim is obsolete for this source. Event-loop startup/restart/preview lifecycle proof remains required; an old binary's pass is not that proof.
- **M-13 / CL2-P9-001:** `OrionAppController.cpp:11663-11673,11707-11717,11728-11742` now retains the warning while the gate is latched and clears it with owned-shot recovery. The prior raw-blip warning disappearance is addressed in source. A controller-level three-blind-shots -> raw-blip -> two-distinct-owned-shots UI test remains the decisive regression.
- **M-02:** `OrderedFileLogSink` now uses one admission deadline and bounded drain. The new copy-completeness issue is AUD-A2-004, not a claim that the old drain still waits forever.
- **M-01:** existing binaries remain older than the A2 source revisions. Their passing runs below are baseline checks only. This report does not approve the package or substitute for rebuilt Standard/StrictSecurity gates.

## Tests actually run

CWD: `C:\Users\aaron\Desktop\NexusVision`. Python bytecode/cache writes disabled; test outputs placed in TEMP. No test source was edited.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
& .\.venv\Scripts\python.exe -m pytest tests/test_venice_ui_contract.py tests/test_preview_qml_contract.py tests/test_non_live_production_surface_contract.py tests/test_gui_cadence_contract.py -q -p no:cacheprovider --basetemp "$env:TEMP\orion-a2-audit-pytest"
```

Literal result: `52 passed in 0.41s`; exit 0.

```powershell
$d=Join-Path $env:TEMP 'orion-a2-audit-native'
foreach($t in @('OrionInputProtocolTests','OrionInputRetryTests','OrionRemotePlayPathTests','OrionPreviewPresentationTests')) {
  $out=Join-Path $d "$t.txt"
  & (Join-Path '.\native_orion\build\Release' "$t.exe") -o "$out,txt"
}
```

All four exit statuses were 0. Retained text outputs at `C:\Users\aaron\AppData\Local\Temp\orion-a2-audit-native\`:

- `OrionInputProtocolTests.txt`: `Totals: 51 passed, 0 failed, 0 skipped, 0 blacklisted, 1895ms`.
- `OrionInputRetryTests.txt`: `Totals: 40 passed, 0 failed, 0 skipped, 0 blacklisted, 10ms`.
- `OrionRemotePlayPathTests.txt`: `Totals: 20 passed, 0 failed, 0 skipped, 0 blacklisted, 157ms`.
- `OrionPreviewPresentationTests.txt`: `Totals: 56 passed, 0 failed, 0 skipped, 0 blacklisted, 4337ms`.

The input binary is dated 01:57:26 UTC and the path binary 01:57:27 UTC; rechecked source revisions were 02:42:46 (`OrionInputClient.cpp`), 02:44:11 (`RemotePlaySession.cpp`), 02:44:00 (`SidecarWatchdog.h`) and 02:46:19 (`OrionAppController.cpp`). No freshly compiled production behavior is claimed.

## Recheck identities

SHA-256 read immediately before report creation (source may continue changing after this snapshot):

| File under `C:\Users\aaron\Desktop\NexusVision` | SHA-256 |
|---|---|
| `native_orion/src/OrionAppController.cpp` | `568FB02F001C4DC91831E099DDB67EBE203667618035B59E69FEA43D8C1E6C72` |
| `native_orion/src/RemotePlaySession.cpp` | `36037EC2590DBC58D82D21F5F4E89AE25FF3D08D9811E7B6B93796AC2B5E04C8` |
| `native_orion/src/OrionInputClient.cpp` | `E24D3BE9F4FE579186267DCBD06B440E3114F74F2AECD59141AA497A8CD81DCF` |
| `native_orion/qml/components/StreamSetupForm.qml` | `CAEE7ED1511D5790433D8BFECF565CD83DA0F656F1013A1B0B8C4F7383E1711D` |
| `native_orion/src/OrderedFileLogSink.cpp` | `359500FE9F1835C9A33E930ADF2CF9E9115A098F2CD69C6D430B9DFEB2E71687` |

