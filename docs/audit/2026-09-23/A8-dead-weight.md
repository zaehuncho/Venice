# A8 — Dead weight and removal boundaries

Status: **needs changes**, but these are cleanup candidates, not release blockers. This inventory uses tracked-file references and current customer ingress paths, not file size alone. Do not remove dormant timing paths simply because their customer switch is off: the post-launch roadmap explicitly reserves Pill and No-Meter work, and the meter blind backstop still uses parts of the timer machinery.

### [AUD-A8-001] low — retired press-anchored fallback remains a persisted settings field
- Kind: removal
- Evidence: `native_orion/src/AppConfig.h:1657-1663` declares `pressAnchoredFallbackEnabled` solely for load/round-trip; `native_orion/src/AppConfig.cpp:985,2036-2037` writes/loads `press_anchored_fallback_enabled`; repository-wide source search found no runtime consumer of the field. `native_orion/src/AutomationEngine.cpp:21447-21449` states the firing rule, override and block reason were deleted. `native_orion/tests/AutomationEngineTests.cpp:15985-15988` still pins key introduction, not behavior. The nearby **meter blind backstop is active** and is not part of this candidate.
- Customer impact: The signed settings schema continues to expose a switch that cannot affect a shot, complicating support and migration checks. Removing it incorrectly, however, could upset existing settings round-trip/signature expectations.
- Proposed change: Deprecate only this key: tolerate it on old signed files, stop generating it for new settings after a schema migration, then delete the data member and old-key round-trip assertion once compatible-load tests pass. Update the dated handoff (`docs/HANDOFF_2026-09-14_UI_POLISH.md:132,212,315,454`) to point at the active blind backstop instead. Do **not** remove `meterBlindBackstopBlockReason()` or blind timer tests. This is a multi-file format migration, not a safe isolated deletion diff.
- Verification: Load a valid old signed settings file with the true key and prove the engine still never consults it; save/reload under the new schema; verify signature and all unrelated settings; run settings-compatibility and backstop tests.
- Confidence: confirmed

### [AUD-A8-002] low — withdrawn Pill route still serializes an unreachable kill switch
- Kind: removal
- Evidence: `native_orion/src/AppConfig.h:66-86` normalizes persisted/requested `pill` to `Arrow2`; `native_orion/src/AppConfig.cpp:1362-1367` applies that load rule, while `:685,1381-1384` still writes/reads `pill_yolo_route` (default true at `AppConfig.h:239`). `native_orion/src/RemotePlaySession.cpp:2365-2373` still calls `applyPillYoloRoute`, whose only meaningful branch requires Pill (`SidecarReaderProfile.h:292-301`). The settings key and branch are thus unreachable through the normal shipped style ingress. However, `docs/ROADMAP_POST_LAUNCH_2026-09.md:9` plans more styles including Pill, and direct helper tests still cover the route.
- Customer impact: Every new settings file carries an inert toggle and the launcher keeps a branch whose production behavior is currently never exercised. Removing the branch prematurely would make planned Pill re-enable riskier.
- Proposed change: Before beta, remove or omit the *serialized* `pill_yolo_route` from fresh customer settings while keeping tolerant old-file parsing. Keep the routing helper/tests behind the future-style work until Pill is either formally reinstated or abandoned; only then delete helper, call site, env key, fixture tests and stale explanatory comments together. This is a staged schema/feature decision, not a safe one-line deletion.
- Verification: Golden settings fixtures must migrate old Pill installs to Arrow2 without changing other values; regular Arrow2 launch environment must remain byte-for-byte equivalent; a dedicated lab Pill fixture must still set YOLO until the feature decision is final.
- Confidence: confirmed

### [AUD-A8-003] low — sidecar emits the same processed-sequence key twice
- Kind: simplification
- Evidence: `native_orion/backend/autogreen_sidecar.py:2685-2686` has two adjacent identical `"processed_seq"` entries in one Python dictionary literal. The latter overwrites the former; both compute the same value. `native_orion/src/RemotePlaySession.cpp:4967-4971` consumes the single JSON key. This is independent of the active sequence-provenance design.
- Customer impact: No current behavioral difference, but duplicate keys obscure which value wins during future edits and make provenance review harder.
- Proposed change: Minimal source proposal, **not applied**:

  ```diff
  --- a/native_orion/backend/autogreen_sidecar.py
  +++ b/native_orion/backend/autogreen_sidecar.py
  @@
                       "processed_seq": int(_processed["seq"]),
  -                    "processed_seq": int(_processed["seq"]),
                       "measurement_capture_ts_ms": _measurement_epoch_ms,
  ```

- Verification: Sidecar protocol serialization fixture should assert exactly one `processed_seq` source entry and unchanged emitted numeric value; run `tests/test_detcsv_authority.py` and frame-provenance tests.
- Confidence: confirmed

## Explicit retain decisions / size census

- **No-Meter:** `AppConfig.cpp:2165-2168`, `OrionAppController.cpp:11054-11068` and `AutomationEngine.cpp:1431-1437` fence pose-only customer authority; `RemotePlayPage.qml:1801-1827` unmounts its card. `docs/ROADMAP_POST_LAUNCH_2026-09.md:14,29-43` explicitly plans No-Meter v2. Do not delete its engine or `NoMeterCard.qml` merely because the current build cannot select it; preserve the future test harness. DebugPage's old toggle prose (`DebugPage.qml:65-76`) should instead say production forces the toggle off.
- **Dev offset/FF/sprint experiments:** The offset sweep and FF A/B have pre-registered measurement rules; engine production guards exclude the dev hooks. `AppConfig.cpp:39-56` intentionally retains the sprint-release A/B door after a live refutation. Do not remove these before the measurement decision; remove them as a *coherent experimental unit* if the ceiling is confirmed.
- **Large tracked files:** The 5.46 MB ONNX model is referenced by `meter_detector_yolo.py` and the sidecar build, and the 2.2–2.4 MB meter PNG fixtures are named by reader tests. `native_orion/tests/AutomationEngineTests.cpp` is 2.65 MB but active. Size alone did not establish a safe large-file deletion. A future split/compression needs detector and test-performance evidence, not this audit's guess.
