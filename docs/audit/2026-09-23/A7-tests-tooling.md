# A7 — Tests and tooling

**Decision: needs changes.** The broad offline suite is not clean, but the supplied “~30 pre-existing failures” is not a current defect count. The corrected current run has eight failed tests: six host-disk fixture failures, one previously reported retired-default assertion, and one reproducible diagnostic thread-name race. No source/test/build files were changed. Existing native tests were copied to scratch; no application was launched or repository build run.

## Evidence and interpretation

- Corrected broad run: `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe C:\Users\aaron\AppData\Local\Temp\orion-a7-full2-run.py`. Its exact pytest arguments are `tests --ignore=tests/backend -m "not slow" -q -p no:cacheprovider --basetemp C:\Users\aaron\AppData\Local\Temp\orion-a7-full2-pytest --junitxml C:\Users\aaron\AppData\Local\Temp\orion-a7-full2.xml`; bytecode disabled. **8 failed, 4247 passed, 14 skipped, 6 deselected, 6 xfailed, 4 xpassed, 1 warning in 178.61s; exit 1.** Raw transcript: `C:\Users\aaron\AppData\Local\Temp\orion-a7-full2.txt`. The scratch runner permits local asyncio socketpair traffic but blocks non-loopback socket connect/sendto and product executable launches. It recorded **30 blocked non-loopback operations**; therefore passing tests do not imply fully mocked networking. No off-host connection/send was allowed by this guard.
- The first broad runner blocked loopback too, interfering with Windows asyncio: **35 failed, 4221 passed, 13 skipped, 6 deselected, 6 xfailed, 4 xpassed; exit 1**, retained in `C:\Users\aaron\AppData\Local\Temp\orion-a7-full.txt` and `orion-a7-full.xml`. Its 28 additional asyncio failures are audit-harness artifacts, not product defects. The corrected run above supersedes that count.
- Historical-cache-file rerun: all existing non-backend files referenced by `.pytest_cache/v/cache/lastfailed`, `-m "not slow" -q -p no:cacheprovider`, external basetemp/JUnit, yielded **5 failed, 866 passed, 2 skipped in 33.67s; exit 1**. Exact file list: `C:\Users\aaron\AppData\Local\Temp\orion-a7-python-files.txt`; output `C:\Users\aaron\AppData\Local\Temp\orion-a7-python.txt`. All five failures are the disk fixture issue below.
- Repeat the six disk-dependent failed nodes with **only** `shutil.disk_usage` supplied as an ample-space fixture: **6 passed in 3.51s; exit 0**, `C:\Users\aaron\AppData\Local\Temp\orion-a7-disk-fixture.txt`. This is fixture-isolation evidence, not evidence that the machine gained free disk space or that production's disk guard should be removed.
- Standalone backend tests and six slow regressions are not included in the broad count. A5 separately records mocked backend/website coverage. No StrictSecurity or production release approval is implied.

## Findings

### [AUD-A7-001] medium — Native evidence does not identify the complete tested binary generation
- Kind: test-gap
- Evidence: `C:\Users\aaron\Desktop\NexusVision\scripts\run_orion_qtest.ps1:27-47,54-63` retains paths, timestamps, PID and exit status, but no executable/dependency hashes or source/build identity. `C:\Users\aaron\Desktop\NexusVision\scripts\verify_orion.ps1:117-154` checks CMake checkout/path ownership, not generation coherence. A1's new observed evidence (`C:\Users\aaron\Desktop\NexusVision\docs\audit\2026-09-23\A1-engine.md:24`) is an access violation in `BannerLeadTrim::resolvedKey` using the 01:58:25Z test EXE with the 02:33:21Z AutomationCore DLL, after the relevant 02:19:39Z header/source change. The test EXE was subsequently replaced at 02:52:33Z, SHA256 `E44556BEE85D6D9E5C17B50768882C6FF3E6681A5C278FB04888460A2DE07E58`; current AutomationCore is `2D47566F59F3206E90500F52F2A2C75BAEEC593A4D066FD22D5CDA85EDF8B84F`. Later retained log `C:\Users\aaron\Desktop\NexusVision\native_orion\build\Testing\QtTest\Release\OrionNativeTests\20260923T025234473Z-a6ded2a5a25a42d9a3a11fc63fe1aef1\qtest.txt` reports **1235 passed, 0 failed, 7 skipped**. These different observations must not be merged into one source verdict.
- Customer impact: a stale/mixed unit can falsely block a good change or certify unrelated old code; the final package's confidence is weaker than the green test count suggests.
- Proposed change: add a build-unit manifest (test EXE, project DLLs, Qt/runtime dependencies, configuration, compiler and source snapshot hashes) to each retained run; verify before/after hashes and mark a changing unit invalid. Build/test in an immutable staging directory or serialize publication with testing. Different DLL timestamps alone are not an error: unchanged dependencies can legitimately be older. Keep the current verifier's build-before-test order; do not replace it with timestamp heuristics.
- Verification: use two harmless fixture binary generations and change a dependency during a run; the harness must retain both identities and reject a coherent-pass claim. A clean, unchanged complete unit must preserve the child's original exit code. Rebuild the complete native unit before release verification. This extends the prior reliability report's stale-binary warning with A1's actual mixed-generation failure; it does not report a proven current-source crash.
- Confidence: confirmed

### [AUD-A7-002] medium — Abandoned-publish regression assumes one 3 ms read must observe abandonment
- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\tests\SharedMemoryFrameReaderTests.cpp:634-645` kills/waits for the producer, invokes `readFrame` once, then requires an `abandoned` diagnostic. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SharedMemoryFrameReader.cpp:28,275-277,301-329` has a 3 ms mutex wait and intentionally returns a null frame with an empty diagnostic for ordinary timeout. A frozen copied runtime repeated the exact existing test 20 times: **19 pass, 1 fail**. Run 17: **2 passed, 1 failed, 169ms, exit 1**, line 643, literal failure `'reader.lastError().contains(QStringLiteral("abandoned"), Qt::CaseInsensitive)' returned FALSE. ()`. The partial frame remained null and its output frame number was zero; the later last-generation assertion was not reached. Retained 00:49:57Z, 00:52:52Z and 00:53:47Z repository runs show the same empty-error failure, not a recovered corrupt image.
- Customer impact: a nondeterministic release gate encourages retries that conceal genuine transport regressions; this observation does not establish unsafe SHM publication to the customer.
- Proposed change: preserve null-frame and zero-generation assertions on every attempt; allow a bounded retry only for the explicit contention/no-error state before the first abandonment observation, and fail on any other diagnostic or non-null partial frame. Retain the Win32 wait result and elapsed wait in test diagnostics so timeout versus abandonment is identifiable. Keep the later clean-writer recovery assertions. Do not make production's 3 ms wait unbounded and do not retry the whole failing suite until green.
- Verification: `C:\Users\aaron\AppData\Local\Temp\orion-a7-shm-unit\OrionShmInteropTests.exe abandonedPartialPublishIsDroppedThenCleanWriterRecoversAcrossProcesses -o <run-log>,txt`, `QT_QPA_PLATFORM=offscreen`, explicit test Python. Logs `run-1.txt` through `run-20.txt`, summary `repeats.txt`, and complete seven-file manifest `unit.json` are in that scratch directory. EXE SHA256 `DF8B1F085076576CB525738DD5CDAC29554B5CC08D4F0AACD38C1A9F453341C2`, RemotePlayCore `8EEA05DE06EC90F78AE60403DC47D55E4791B927BA497DCFA160A15D9EE1D909`; all copied hashes unchanged after the repeats. Validate the proposed bounded observation with an injected initial timeout and a bounded no-abandon failure, plus repeated real-process runs. The initial incomplete scratch copy lacked its Qt platform plugin and was stopped before any test output; it contributes no test result.
- Confidence: confirmed

### [AUD-A7-003] medium — Six frame/archive tests depend on host free-space percentage
- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\tests\test_framedump_press_window.py:26-74` sets `_framedump_min_free_bytes=1` but retains `_framedump_min_free_pct=0.5`. `C:\Users\aaron\Desktop\NexusVision\remote_play_orchestrator.py:6571-6589` correctly takes the maximum of the absolute and percentage floors. The broad run logs **free=2.84 GiB required=4.65 GiB**, disables diagnostic framedump, and fails five window/crop tests plus `C:\Users\aaron\Desktop\NexusVision\tests\test_shot_records_wiring.py:177-201`, which reuses that helper. All six pass when only disk capacity is an ample-space fixture.
- Customer impact: unrelated workstation disk pressure creates six apparent capture regressions, making the release gate noisy. No evidence here contradicts the intended production low-disk cutoff.
- Proposed change: make positive frame-window tests use a deterministic `disk_usage` fixture; keep dedicated low-disk/query-failure tests exercising the real policy. The existing census helper in `C:\Users\aaron\Desktop\NexusVision\tests\test_framedump_census_reliability.py:40-41` already isolates disk availability. A minimal helper diff is:
```diff
--- a/tests/test_framedump_press_window.py
+++ b/tests/test_framedump_press_window.py
@@ -27,2 +27,4 @@
 def _press_orchestrator(tmp_path, monkeypatch, **env):
     """A framedump-only orchestrator in press-window mode, with a REAL writer thread."""
+    monkeypatch.setattr(rpo.shutil, "disk_usage", lambda _path:
+                        SimpleNamespace(total=100 * _GIB, used=_GIB, free=99 * _GIB))
```
- Verification: run all six listed failed nodes on a low-space host, then retain separate boundary tests just below/at/above the configured floor and disk-query failure; production must still stop only diagnostic writes. Scratch fixture result: **6 passed, exit 0**.
- Confidence: confirmed

### [AUD-A7-004] medium — Passing “offline” tests attempt real backend and console network operations
- Kind: test-gap
- Evidence: the corrected broad run's guard recorded **19 external HTTPS connection attempts** during `C:\Users\aaron\Desktop\NexusVision\tests\test_backend_staff_auth.py` and **11 TCP/UDP operations** during `C:\Users\aaron\Desktop\NexusVision\tests\test_remote_play_client_lifecycle.py` and `C:\Users\aaron\Desktop\NexusVision\tests\test_start_stream_warm_promote.py`. All were blocked before connect/send. The staff fixture (`test_backend_staff_auth.py:129-140`) mocks the legacy `staff_audit_table` but not the unified `audit_table`; `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:1372-1379` writes both, and unified audit catches its failure at `:447-452`. Standby fixtures (`test_remote_play_client_lifecycle.py:603-620`) fake process/pipe operations but leave console reachability paths live; disabling the wake flag alone is insufficient. Exact triggering nodeids are retained in `C:\Users\aaron\AppData\Local\Temp\orion-a7-full2.txt` after `A7_OFFLINE_GUARD_BLOCKS 30`.
- Customer impact: routine CI/developer tests can depend on network/credentials, take unexpected time, or perform unrelated external operations; caught errors allow green tests to overlook missing audit coverage.
- Proposed change: mock both audit tables with their actual keys and assert both intended audit writes, fail any unhandled AWS transport boundary, and stub console reachability/wake helpers in lifecycle fixtures. Add a process-wide default-deny non-loopback egress fixture with an explicit loopback exception for Windows asyncio; count blocked attempts as test failures even if application code catches the exception. Avoid relying on placeholder IP addresses or exception-catching as isolation.
- Verification: repeat the same broad suite with the guard: **zero attempted non-loopback operations** is the required result, independent of pass count. Add fixture tests demonstrating an unmocked transport fails the test rather than disappearing into best-effort logging. Do not retest by permitting live destinations.
- Confidence: confirmed

### [AUD-A7-005] low — Diagnostic and benchmark tests conflate deterministic behavior with scheduling accidents
- Kind: reliability
- Evidence: the corrected broad run fails `C:\Users\aaron\Desktop\NexusVision\tests\test_stall_attributor.py:76-105`: literal output `STALL: gap_ms=59.8 thread=tid-14152 where=test_stall_attributor.py:_hog_the_gil:72 ... cause=in_process`. Five isolated fresh-process repeats yield **4 pass / 1 fail**; failed run 4 retains `gap_ms=60.0 thread=tid-34792` with the correct hog stack in `C:\Users\aaron\AppData\Local\Temp\orion-a7-stall-4.txt`. The sampler snapshots thread stacks separately from the watchdog's live-thread-name enumeration (`C:\Users\aaron\Desktop\NexusVision\stall_attributor.py:258-290,313-337,379-381`); a short-lived worker can die before its name is cached. Separately, `C:\Users\aaron\Desktop\NexusVision\tests\test_meter_locator_cv.py:697-711` times the two variants in separate sequential 21-sample batches and hard-fails a +1.5 ms difference. The reader agent observed 5.08 versus 3.05 ms on its first run, followed by 3/3 isolated passes and 240/240 full-suite passes; that is cross-agent evidence, not an A7-reproduced locator failure. A7's broad run passed the locator test.
- Customer impact: the optional, default-off stall diagnostic can omit a useful short-lived thread name; timing noise also creates false release failures. Neither observation proves a new live shot-timing defect.
- Proposed change: test deterministic stack/cause attribution from explicit samples and add a named-worker lifecycle synchronization fixture; retain a separate real-thread stress test whose honest fallback is a numeric thread ID. If naming every sampled short-lived worker is required, capture bounded identity information with the sample/registration lifecycle rather than assuming the watchdog ran. Preserve the locator's deterministic candidate-count equality; put wall-time budgets in an isolated, interleaved/paired performance run with retained distributions rather than a sequential single-run unit threshold.
- Verification: deterministic tests must pass regardless of watchdog scheduling and must preserve stack identity after thread exit; stress runs retain all failures, not only a final pass. A focused command is `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe -m pytest tests/test_stall_attributor.py::test_a_synthetic_60ms_stall_names_the_thread_and_the_function -q -p no:cacheprovider --basetemp <external-temp>`; current five outputs are `C:\Users\aaron\AppData\Local\Temp\orion-a7-stall-1.txt` through `orion-a7-stall-5.txt`, each exit 0 except run 4 exit 1.
- Confidence: confirmed

### [AUD-A7-006] medium — Component-green counts omit the state transitions where fresh defects occur
- Kind: test-gap
- Evidence: A2's four UI/contract suites pass **52/52** while AUD-A2-001/002 identify live source-setting changes and capture-device warm reuse that those textual contracts do not exercise. A5's **42/42** worker tests and **48/48** selected backend tests pass separately while AUD-A5-001 identifies their coupled retry/limiter mismatch; the native heartbeat suite passes **21/21** while AUD-A5-004 demonstrates the stale success-state classification. See `C:\Users\aaron\Desktop\NexusVision\docs\audit\2026-09-23\A2-controller-ui.md` and `C:\Users\aaron\Desktop\NexusVision\docs\audit\2026-09-23\A5-license-backend.md`. The gate in `C:\Users\aaron\Desktop\NexusVision\scripts\verify_orion.ps1:270-362` is an explicit file list, not all default root tests, so a broad local pass and a release-gate pass are different claims.
- Customer impact: convincing aggregate pass counts miss customer-visible transitions spanning otherwise tested components.
- Proposed change: add behavioral tests for source/device changes across stopped/preview/running states; a shared worker/backend provisioning-pending-to-ready trace with a real in-memory quota model; and native lease-success/sleep/expiry/retry transitions. Add the resulting files to the release allowlist explicitly. These are test fixes for the cited items, not duplicate new product findings.
- Verification: each regression must fail against the currently reported transition and pass the smallest proposed production correction. Assert actual active capture identity and heartbeat/limiter state, not just UI strings or mocked success responses. Retain separate identities for the A3 fork's changing pipe-test generations; its different test populations must not be pooled into a retry-pass claim.
- Confidence: confirmed

## Current eight failed Python nodes: stale versus real

| Exact node (repository-relative pytest nodeid) | Classification |
|---|---|
| `tests/test_framedump_press_window.py::test_thirty_seconds_ten_windows_no_drops_inside_no_frames_outside` | Real test-fixture defect; host disk cutoff, AUD-A7-003. |
| `tests/test_framedump_press_window.py::test_frames_carry_the_pre_roll_before_the_press` | Same disk fixture. |
| `tests/test_framedump_press_window.py::test_window_has_a_hard_cap_for_an_unanswered_press` | Same disk fixture. |
| `tests/test_framedump_press_window.py::test_shooter_crop_writes_the_patch_not_the_frame` | Same disk fixture. |
| `tests/test_framedump_press_window.py::test_one_window_of_real_720p_frames_still_drops_nothing` | Same disk fixture. |
| `tests/test_shot_records_wiring.py::test_press_window_files_join_the_jsonl_by_epoch` | Same shared disk fixture. |
| `tests/test_ship_defaults.py::test_the_rest_of_the_native_ship_list_is_where_the_launch_line_left_it` | Stale retired-default assertion, already in the prior Codex reliability report; not a new product finding. |
| `tests/test_stall_attributor.py::test_a_synthetic_60ms_stall_names_the_thread_and_the_function` | Real diagnostic name-capture limitation plus schedule-dependent assertion, AUD-A7-005; not a capture/fire defect. |

The stale shipping assertion is at `C:\Users\aaron\Desktop\NexusVision\tests\test_ship_defaults.py:434`, expecting `squarePressR2HoldMs=50.0`. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.h:776-781` explicitly documents why faithful pass-through changed the default to **0.0** after the owner hold report. Update the test to the accepted product policy; do not revive a retired 50 ms hold to satisfy it. This remains the same issue already identified in `C:\Users\aaron\Desktop\NexusVision\docs\redteam\2026-09-22-launch\RED_TEAM_REPORT.codex.reliability.md:10`.

## Historical failure ledger (not a current failure total)

The supplied prompt gives no canonical 30-node file. The actual cache `C:\Users\aaron\Desktop\NexusVision\.pytest_cache\v\cache\lastfailed`, last written **2026-09-22 01:21:39Z**, has **52 non-backend root nodes** plus 16 standalone backend collection-file entries. Of those 52: **44 stale nodeids** (36 renamed/removed functions, five nodes from a retired file, three retired parameter cases), **three now passing**, **five current disk-fixture failures**. Every old node is listed below rather than treating absent tests as passing. The 16 backend file entries are historical collection failures under a different suite/root configuration, not 16 demonstrated backend defects.

- Geometry gate off-default pins were replaced with explicit on-default tests.
- The nine old staff tests describe a replaced staff-token/enrollment design; the file's current header documents the rewrite.
- Latency single-label-authority pins were replaced with telemetry-only/validated authority contracts.
- The old `[White]` unsupported-color case is absent because current supported-color policy differs; old delay cap `[301.0-300.0]` / `[100000.0-300.0]` cases were replaced with the current bounded range.
- Old Pill customer-selector assertions are absent; do not restore the withdrawn product route solely to recreate old test names.

| Historical nodeid | Current disposition |
|---|---|
| `tests/test_meter_detector_geometry_gate.py::test_geometry_gate_off_by_default` | Stale: renamed/removed function |
| `tests/test_meter_detector_geometry_gate.py::test_loaded_config_geometry_gate_off` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_staff_enrollment_key_is_hashed_with_salt` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_staff_role_matrix_is_server_side_source_of_truth` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_router_exposes_staff_routes_without_auth_as_401` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_admin_staff_create_returns_one_time_key_and_no_hash` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_route_exceptions_return_json_not_empty_500` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_version_advertises_staff_features` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_staff_role_comes_from_server_not_token_payload` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_staff_enrollment_key_is_single_use` | Stale: renamed/removed function |
| `tests/test_backend_staff_auth.py::test_staff_enrollment_expired_key_is_rejected` | Stale: renamed/removed function |
| `tests/test_luma_fit.py::test_degenerate_near_full_freezes_to_ema` | Stale: renamed/removed function |
| `tests/test_compressed_meter_reader.py::test_collapse_parity_on_real_framedumps` | Stale: renamed/removed function |
| `tests/test_dual_capture.py::test_stage2_recovers_time_map_within_tolerance` | Stale: renamed/removed function |
| `tests/test_simple_reader_sidecar_wiring.py::test_flag_absent_defaults_to_chain` | Stale: renamed/removed function |
| `tests/test_release_marker_relay.py::test_green_grade_carries_native_release_identity_with_and_without_estimator` | Stale: renamed/removed function |
| `tests/test_latency_estimator.py::test_one_clean_controlled_label_grants_only_bounded_provisional_authority` | Stale: renamed/removed function |
| `tests/test_latency_estimator.py::test_controlled_warm_start_keeps_refining_from_ordinary_live_labels` | Stale: renamed/removed function |
| `tests/test_venice_ui_contract.py::test_performance_defaults_to_results_and_hides_detector_tuning` | Stale: renamed/removed function |
| `tests/test_latency_calibration_ui_contract.py::test_latency_calibration_qml_uses_controller_contract_and_correct_instructions` | Stale: renamed/removed function |
| `tests/test_venice_live_page_cleanup_contract.py::test_meter_truth_hud_is_one_slim_line_of_live_values` | Stale: renamed/removed function |
| `tests/test_passive_court_flow_contract.py::test_network_page_labels_passive_endpoint_as_unverified` | Stale: renamed/removed function |
| `tests/test_latency_estimator.py::test_factory_prior_refines_without_a_fixed_warmup_batch` | Stale: renamed/removed function |
| `tests/test_reader_seat_physics_and_hold_cap.py::test_seat_veto_catches_a_position_teleport_that_barely_moves_the_fill` | Stale: retired file |
| `tests/test_reader_seat_physics_and_hold_cap.py::test_seat_veto_refuses_the_measured_9x77_phantom_step` | Stale: retired file |
| `tests/test_reader_seat_physics_and_hold_cap.py::test_seat_veto_fires_while_hardware_armed` | Stale: retired file |
| `tests/test_reader_seat_physics_and_hold_cap.py::test_long_green_hold_stops_claiming_freshness` | Stale: retired file |
| `tests/test_reader_seat_physics_and_hold_cap.py::test_hold_cap_counts_coast_frames_as_age` | Stale: retired file |
| `tests/test_simple_meter_reader.py::test_track_h_cap_default_off_reproduces_the_shipped_poisoning` | Stale: renamed/removed function |
| `tests/test_nexus_svc_delay.py::test_delay_is_clamped_to_the_hard_safety_range[301.0-300.0]` | Stale: retired parameter case |
| `tests/test_nexus_svc_delay.py::test_delay_is_clamped_to_the_hard_safety_range[100000.0-300.0]` | Stale: retired parameter case |
| `tests/test_simple_meter_reader.py::test_unsupported_colours_fall_back_to_red_and_keep_reading[White]` | Stale: retired parameter case |
| `tests/test_simple_reader_tight_box_clamp.py::test_translated_tall_shape_is_clamped_bottom_anchored` | Stale: renamed/removed function |
| `tests/test_simple_reader_tight_box_clamp.py::test_top_row_raise_is_clamped` | Stale: renamed/removed function |
| `tests/test_simple_reader_tight_box_clamp.py::test_colour_path_h_cap_zero_is_untouched` | Stale: renamed/removed function |
| `tests/test_simple_reader_lock_lifecycle.py::test_pre_arm_latest_does_not_starve_current_frame_sync_acquire` | Stale: renamed/removed function |
| `tests/test_simple_reader_lock_lifecycle.py::test_prior_frame_pending_result_does_not_starve_current_sync_acquire` | Stale: renamed/removed function |
| `tests/test_simple_reader_lock_lifecycle.py::test_phased_sync_acquire_rotates_one_region_per_opportunity` | Stale: renamed/removed function |
| `tests/test_manual_shot_tally.py::test_manual_results_are_visible_outside_the_native_video_surface` | Stale: renamed/removed function |
| `tests/test_meter_locator_cv.py::test_meter_above_the_scan_top_is_refused_by_default[45]` | Stale: renamed/removed function |
| `tests/test_venice_ui_contract.py::test_timing_mode_switch_owns_the_live_side_panel` | Stale: renamed/removed function |
| `tests/test_player_anchor_acquire.py::test_per_strip_zero_restores_the_global_pick` | Stale: renamed/removed function |
| `tests/test_non_live_production_surface_contract.py::test_setup_keeps_backend_contracts_and_has_no_customer_performance_label` | Now passes |
| `tests/test_non_live_production_surface_contract.py::test_sidebar_and_tour_only_name_current_customer_surfaces` | Now passes |
| `tests/test_controller_route_lifecycle_contract.py::test_direct_input_pipe_cache_is_reset_at_every_process_generation_boundary` | Now passes |
| `tests/test_framedump_press_window.py::test_thirty_seconds_ten_windows_no_drops_inside_no_frames_outside` | Current disk-fixture failure |
| `tests/test_framedump_press_window.py::test_frames_carry_the_pre_roll_before_the_press` | Current disk-fixture failure |
| `tests/test_framedump_press_window.py::test_window_has_a_hard_cap_for_an_unanswered_press` | Current disk-fixture failure |
| `tests/test_framedump_press_window.py::test_shooter_crop_writes_the_patch_not_the_frame` | Current disk-fixture failure |
| `tests/test_framedump_press_window.py::test_one_window_of_real_720p_frames_still_drops_nothing` | Current disk-fixture failure |
| `tests/test_probe_audit.py::test_eligibility_decided_before_onset_is_refused` | Stale: renamed/removed function |
| `tests/test_venice_ui_contract.py::test_meter_style_combo_offers_pill_and_routes_it_to_the_packaged_detector` | Stale: renamed/removed function |

Standalone backend collection-file entries (not rerun as part of the root suite):

- `tests/backend/test_activate.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_admin_bot_auth.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_admin_v2.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_crit2_update_signing.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_edge_auth.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_high3_order_scope.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_med1_activate_replay.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_med2_staff_binding.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_new1_lease_sig.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_new2_device_cap.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_staff_rbac.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_trial.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_validate.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_plan_surface.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_discord_subscription_entitlement.py` — historical standalone-suite collection entry; no current failure inference.
- `tests/backend/test_pairing.py` — historical standalone-suite collection entry; no current failure inference.

## Recheck anchors

Rechecked source hashes immediately before report publication:
- `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SharedMemoryFrameReader.cpp`: `32F5F27B7B65B3E2C45500CDFE0C15E4D8440E802F7BF712FE39641EFD90DDD6`.
- `C:\Users\aaron\Desktop\NexusVision\native_orion\tests\SharedMemoryFrameReaderTests.cpp`: `48A291FF83887E5BE7BC631829F1ADB88AC28E1DEDD5E86F9C8A5C6F997C520E`.
- `C:\Users\aaron\Desktop\NexusVision\scripts\run_orion_qtest.ps1`: `9BF905544384348396388BBD461D14EBCF8FDEDDD9BE07C5DE15100BF24E652A`.
- `C:\Users\aaron\Desktop\NexusVision\tests\test_framedump_press_window.py`: `E33676DE500949A46BF146D0C9E81F979C47F638C1A2BA459BD4DA8218D9C876`.
- `C:\Users\aaron\Desktop\NexusVision\tests\test_ship_defaults.py`: `109629CEBD13F5017C95794DD0F9C6165325D0E8D0A64CCC648A6D93273E01D0`.
- `C:\Users\aaron\Desktop\NexusVision\stall_attributor.py`: `1DEC83F0ABA81F38B576562B7637A6ED64D3D6476002E39349AA68E2E65DF744`.
- `C:\Users\aaron\Desktop\NexusVision\tests\test_backend_staff_auth.py`: `C3D3540F1ED5FB0F4B03B19B43A785AA1CED49BC97E151C38C18EBB1DE1DCE00`.
