blocked

# Codex reliability lane — frozen rc1, 2026-09-23

**Lane decision:** **blocked** for release promotion, not an overall security verdict. The rc1 package is hash-consistent and several source/fork fault fixtures pass, but capture identity can be falsely certified, displaced releases still enter learners, semantic learning corruption survives load and poisons its backup, and METER patch-day unavailability is not communicated for meterless Square presses. StrictSecurity and compiled-sidecar held-out footage/hardware gates remain open. **No evidence here proves silent blind fire in default METER mode.**

## Evidence boundary and candidate identity

- Frozen source `59129e50b2f1862b104267d52f1f6f00d05bf746`; sheet HEAD `ea8729b178a09ad572e0c3205abeb1c8f3e7fb9a`; fork `67eec7252d423e3d4ec05c1f33cec7a5248d4b9e`. `git diff 59129e5 -- native_orion/src native_orion/backend capture_card_backend.py remote_play_orchestrator.py shot_records.py` returned **0 lines** when checked. Installer/assets, `learning.json`, the sheet, tests and other reports were being modified concurrently; those working-tree versions are **not** rc1 evidence. Claude/Gemini reports supplied leads only.
- Copied `C:\Users\aaron\VeniceRC\rc1-20260923\orion-package` into `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\orion-package`; SHA-256 per-file comparison: literal `package_copy_files=578 hash_mismatches=0`, exit 0. `VeniceSetup-1.0.0.exe` `D0383F12C867936A24040E108B5B8866F1BE7307F8E07BA19F2F8BDC614F8D50`; zip `209BD83F8F4E5A767DBA3F35BD9BD01B6210D3788D81395881CD4C8E167E7567`; update manifest `2652B3D64991DF8B357F70DED3E9C2CA22753F440098C5A7DA7457CB91DEEF5F`. Package `release_manifest.json` `1083EA693F937612D51BA2EE467F2B8A0E0FE240AD08C280B6A03976FE06E950`, `.sig` `63ACBA1C2D676E60FD42FD04BD14EC5AF6888122F7EA4C9960B70BFEB53E524D`, `OrionNative.exe` `40D7AFB49059DD158BA8306D4149011327CB0256B48D7FF77847BE95A1E3711D`, `OrionSidecar.exe` `6E3BF77F58374E22F8683EEF31EE482C320C4A54AFD312819DADE94A5197451E`, `OrionStream.exe` `1F659A46570FACBB12F505BDA8B61AAF65E9A522C06C6DBEA470E5E48C180897`.
- `& .\.venv\Scripts\python.exe -B tools/security_audit.py --root . --package-dir "$env:TEMP\codex-reliability-rc1-20260923\orion-package" --package-only` → literal `[orion-security] OK`, exit 0. Source sidecar `& .\.venv\Scripts\python.exe -B tools/sidecar_bundle_manifest.py --verify --dist build/sidecar/autogreen_sidecar.dist` → `compiled sidecar matches current sources`, exit 0. **Do not** point this verifier at the whole 578-file package; it then reports extra files by design. Packaged sidecar `--build-identity-file` → `ac12502c4806b27ab8009ece1dc47c56acd8acbde1e715427e20d77e4d1c4207`, exit 0, equal to `ORION_SIDECAR_BUILD.json` and current reader/capture/records source manifest. This is build provenance, not behavioral proof.
- Copied packaged `OrionSidecar.exe --detector-smoke-file "$env:TEMP\codex-reliability-rc1-20260923\detector-smoke.json" --root "$env:TEMP\codex-reliability-rc1-20260923\orion-package"` → `{"max_ms": 41.669, "median_ms": 25.69, "model": "orion_meter_detector.onnx", "ok": true, "p90_limit_ms": 35.0, "p90_ms": 29.933, "p99_ms": 33.883, "provider": "DmlExecutionProvider", "runs": 100}`, exit 0. It did not replay video, exercise a live capture handle or prove 60-fps sustained operation.

### Test transcript / exact scope

All commands below used the copied package or source-built fixtures. No launcher was directly run and no console/third-party endpoint was contacted. `Start-Process` uses `-WindowStyle Hidden`; QTest output was directed to the named scratch `.txt` files. The native test binaries link production DLL hashes identical to rc1 (for example `AutomationCore.dll` `D34C62E18801A3C51C78E9EC706F6F7C08609F9715CA7BD01FB59237C42BCA51`), **not** the packaged `OrionNative.exe` process. The fork pipe harness is a source-built test from commit `67eec725`, **not** the packaged `OrionStream.exe` process.

| Exact command/input (PowerShell unless noted) | Literal result / exit |
|---|---|
| `& native_orion\build_prod_review\Release\OrionInputProtocolTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionInputProtocolTests.txt,txt"` | `Totals: 54 passed, 0 failed, 0 skipped, 0 blacklisted, 3258ms`; 0. Includes R2-up/L2-down, L2-up/R2-down, R2-up+stick, ACK ordering, missing final ACK, abandon cancellation, unflagged owned keepalive. |
| `& native_orion\build_prod_review\Release\OrionInputRetryTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionInputRetryTests.txt,txt"` | `Totals: 40 passed, 0 failed, 0 skipped, 0 blacklisted, 15ms`; 0. |
| `& native_orion\build_prod_review\Release\OrionOrderedFileLogSinkTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionOrderedFileLogSinkTests.txt,txt"` | `Totals: 6 passed, 0 failed, 0 skipped, 0 blacklisted, 184ms`; 0; includes `failingStorageNeverBlocksProducerOrShutdown`. |
| `& native_orion\build_prod_review\Release\OrionRemotePlayPathTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionRemotePlayPathTests.txt,txt"` | `Totals: 18 passed, 0 failed, 2 skipped, 0 blacklisted, 273ms`; 0; production config-path cases skipped. |
| `& native_orion\build_prod_review\Release\OrionMeterDelaySettingsTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionMeterDelaySettingsTests.txt,txt"` | `Totals: 15 passed, 0 failed, 4 skipped, 0 blacklisted, 10ms`; 0. |
| `& native_orion\build_prod_review\Release\OrionLicenseHeartbeatPolicyTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionLicenseHeartbeatPolicyTests.txt,txt"` | `Totals: 23 passed, 0 failed, 0 skipped, 0 blacklisted, 10ms`; 0. |
| `& native_orion\build_prod_review\Release\OrionShotVerdictTallyTests.exe -o "$env:TEMP\codex-reliability-rc1-20260923\OrionShotVerdictTallyTests.txt,txt"` | `Totals: 59 passed, 0 failed, 0 skipped, 0 blacklisted, 8ms`; 0. |
| `& native_orion\build\Release\OrionNativeTests.exe meterModeNeverAnswersAnUnownedPressBlindly -o "$env:TEMP\codex-reliability-rc1-20260923\engine-meter-dev.txt,txt"` | `Totals: 8 passed, 0 failed, 0 skipped, 0 blacklisted, 14ms`; 0; 6 gesture rows plus setup/cleanup, **dev engine fixture**. |
| `& native_orion\build_prod_review\Release\OrionNativeTests.exe meterModeNeverAnswersAnUnownedPressBlindly -o "$env:TEMP\codex-reliability-rc1-20260923\engine-meter-prod.txt,txt"` | no QTest file; process exit `-1073740791` (0xC0000409) in this isolated environment. No production behavioral pass claimed. |
| `& C:\Users\aaron\Desktop\chiaki-ng-src\build-codex-a3-20260923\test\orion-bridge-pipe-test.exe` (dummy sink, PATH includes its local DLLs) | `11 of 11 (100%) tests successful`; 0; `dead-man neutral after 402 ms of silence`, `terminal after 4 abandon cycles`, `hold expired after 2069 ms`. Full literal output: `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\fork-pipe.txt`. |
| `& .\.venv\Scripts\python.exe -B "$env:TEMP\codex-reliability-rc1-20260923\capture_open_race.py"` (`PYTHONPATH`=repo, `_open` mocked; snapshot HD60 X but opened handle webcam) | `inventory_name=HD60 X opened_handle_name=Integrated Webcam started=True route_basis=configured identity_verified=True`; 0. |
| `& .\.venv\Scripts\python.exe -B "$env:TEMP\codex-reliability-rc1-20260923\capture_pick_fallback.py"` (`PYTHONPATH`=repo; selected stable ID A at index 0, mock A busy, B card opens at index 1) | `selected_id=A opened_id=B probed=[0, 1] started=True route_basis=card_name identity_verified=True`; 0. |
| `& .\.venv\Scripts\python.exe -B "$env:TEMP\codex-reliability-rc1-20260923\ff_join.py"` (read-only historical development log; `seq`, nonzero FF and press-tip within 3 seconds) | `within_3s_nonzero_ff_press_tip_pairs=48` / `accepted_on_displaced=47`; 0. Last 6 joined tuples are in that script's output; not rc1 package runtime proof. |
| `& .\.venv\Scripts\python.exe -B -m pytest tests/test_detection_regression.py -q` (offline corpus test; previously run) | `ssssss [100%]A7_OFFLINE_GUARD_BLOCKS 0` / `6 skipped in 0.20s`; 0. A green exit contains **zero** held-out patch corpus comparisons. |

The `RouteTransitionIntegrationTests.cpp` fixture's fake sidecar is protocol-only, but its production run from a scratch copy crashed `-1073740791`; another copied production layout hung and was stopped as our own test process. I did **not** reproduce the claimed 3/5 prod versus 5/5 dev result. Its in-place test stages a model file under the build directory, outside this lane's write boundary, so it was not rerun in place. F2's result is an actionable **unverified lead** for StrictSecurity, not my test result. `scripts/verify_orion.ps1 -StrictSecurity` was not run; the sheet also records not run on rc1. No broad pytest was run after the destructive stale-sweep hazard was identified. The 309-pass focused Python output from before that warning is not promoted here because its exact allowlist invocation was not retained.

## Findings, ranked LOW → CRITICAL

### LOW — unsupported style ingress remains wider than the UI

`MeterConfigPanel.qml:61` offers only `Arrow2`. The frozen package has **zero** `pill.json` files and has `Arrow2.json`; Pill is re-withdrawn after the incomplete 20-shot/no-framedump beta result. `AppConfig.h:72-86`, however, still accepts `Arrow`, `Dial`, `Straight`, `Sword`; all four corresponding JSON files are in the package. **Path:** a hand-edited/persisted style or profile passes native normalization even though the UI never offers it. **Impact:** false expectation of supported detection; not a demonstrated Pill re-enable. **Fix/test:** normalize all ingress to Arrow2 or explicitly label/archive other profiles and exclude unsupported model data; test each stored/profile value through the packaged configuration route. Tier: source + package inventory; package UI not launched.

### MEDIUM — M-12 semantic `learning.json` and last-good rotation

`AppConfig.cpp:2192` accepts `shot_type_learned_offset_ms.Standstill=9999999.0`, while the no-meter hold validator rejects its `median_ms=9999999.0`. A fresh **real AppConfig C++ source build** loaded the poisoned offset with an empty load note and `saveLearning` rotated the bad primary into `.bak`. Input/command/output/exit and tested rollback are recorded in `VERIFICATION.txt` below; untouched source and live `learning.json` were not modified. **Impact:** a bad offset can persist and replace the last good copy, biasing future timing. **Fix:** schema-wide finite/range/type validation before accepting *or rotating* a generation; preserve last-good on semantic failure. **Closure:** valid control loads, every out-of-band map/legacy field is rejected, bad primary falls back to known-good backup, backup hash remains unchanged. Tier: source-built fixture, not rc1 launcher.

### MEDIUM — M-13 / P9 warning latch is unreachable for default METER meterless Square presses; F8's silent-fire inference does not follow

Independent control-flow trace: `OrionAppController.cpp:13013-13025` **does** issue a controller-origin `physicalShotEpoch` on Square down; F4's phrase “never produce physicalShotEpoch” must mean the **ShotContext field**, not the parent counter. `observeMeterBlindness` receives `shot_.physicalShotEpoch` at `:2995`, and `AutomationEngine.cpp:7123` assigns that ShotContext field only at `beginShot`. A wholly undetected METER Square stays pending and ends `SHOT NOT OWNED ... output=pass_through backstop=green_window_priority` (`AutomationEngine.cpp:8620-8640`), so the advisory never sees consecutive shot epochs. `AutomationEngine::applyConfig` hard-sets `greenWindowPriority=true` (`:2129`); `maybeFireMeterBlindBackstop` may pass its preliminary block-reason check, **but** `:21932-21946` returns false at the actual fire branch and logs `blind_release_suppressed=1`. The dev six-row meterless fixture confirms 0 synthetic releases. Therefore **releases=0 / no explicit unavailable state** is the supported claim, not blind timer fire.

F8 independently noticed a real second bug: `OrionAppController.cpp:11732-11744` clears the blind streak on `genuineRawDetection` **before** checking zero/idle `physicalShotEpoch` at `:11749`. An idle false sighting can reset a streak **where one exists**; it is not what causes the default Square case to stay below three, because that case creates no ShotContext epochs. The frequency of real idle false locks on rc1 is unmeasured here. In **NO METER**, `inputTimedEnabled` returns early and clears the streak by design (`:11721-11729`), and timed releases are intentional; they are not a METER patch-day blind-backstop fire. Any proposed silent-fire path must show a concrete different METER branch defeating `greenWindowPriority`; none was shown. **Impact:** wrong style/patch can leave the bot inactive without an explicit customer-facing “detection unavailable” state. **Fix:** count completed controller-origin unanswered Square epochs, preserve genuine in-epoch visibility distinction, and recover only after two genuinely vision-owned shots. **Closure:** controller-level three-blind-press test, idle raw negative control, genuine-owned recovery, NO METER mode control, exact package held-out footage. Tier: source + dev engine fixture; hardware/package replay unverified.

### MEDIUM — M-25 FF-displaced releases enter other learners

`AutomationEngine.cpp:13249-13313` captures nonzero applied onset FF per release. Some fences exist (`releaseMarker` at `:13251`, phase sample at `:22801-22822`, trim/oracle at `:23113`), but `recordNoMeterHoldObservation` (`:22305-22309`), `PRESS-TIP OBSERVATION accepted` (`:22372-22395`), per-type clock seed (`:13364-13398`) and velocity prior (`:13404-13413`) test `devFireOffsetArmed_`, not the FF displacement. Independent `ff_join.py` joined historical dev log lines within 3 seconds: **47/48 nonzero-FF press-tip observations accepted**, e.g. seq 24 at 17:15:08Z (−10 ms, accepted 1). The prior F4 6/6 was a narrower subset; this broader log join is not package runtime proof. **Impact:** biased persisted no-meter hold / press-tip priors and possible cross-shot drift, not demonstrated same-day make-rate loss. **Fix:** carry applied onset-FF into every release-coupled learner and reject only truly displaced samples (retain sub-floor zero controls). **Closure:** displaced vs undisplaced fixture compares emitted learner updates and persisted maps. Tier: source + dev log; package DLL hash/source match only.

### MEDIUM — M-26 Cancel mutates Auto lead provenance

`OrionAppController.cpp:9381` stores only the numeric lead. For Auto/zero at entry and ≥1 graded step, `cancelLeadCalibration` at `:9401-9402` falls back to the *current stepped lead*, and `setActuationLeadMs` at `:9449-9456` clamps to [150,800] and forces `actuationLeadUserSet=true`; it also resets the tally. An Auto/zero install can thus become a persistent manual lead after Cancel, even though the action claims restoration. A measured-but-not-user-set lead similarly flips provenance. **Impact:** a potentially wrong timing lead remains pinned rather than auto-seeding. **Fix:** snapshot/restore the full `{value,userSet,per-source stash}` tuple, including zero, on Cancel; update copy only after successful restoration. **Closure:** constructed-controller Auto→GOOD/LATE→Cancel, measured→Cancel and manually-set control; assert exact settings tuple and UI. Tier: source-confirmed, not controller/package-executed here.

### MEDIUM — P4 soft-fault lifetime budget can age out indefinitely

Fork `lib/src/orioninput.c:212-253` enforces **3 faults per rolling 10 s**, increments `soft_faults_total` only for diagnostics, and does not enforce that aggregate. Its own unit test at `test/orioninput.c:242-253` explicitly accepts nine faults spaced `10000/3+1` ms apart. Thus reconnect/delivery recovery does **not** reset the rolling window, but low-rate repeated faults can continue for the life of the session. Dummy pipe proved four rapid abandons terminate and 2-second un-recovered hold terminates; it did **not** run nine spread-out cycles. **Impact:** repeated neutral pauses/reconnect churn with no lifetime terminal; not a stuck button or silent handback on present evidence. **Fix/test:** add a documented lifetime/session aggregate or rate-of-recovery budget and a timed nine-cycle dummy-pipe test. Tier: fork source/unit fixture; package executable not run.

### HIGH — explicit capture pick and opened-handle identity can be overridden

Two independent all-mocked `CaptureCardBackend` fixtures reached `identity_verified=True` in wrong-device cases: (1) frozen inventory says selected HD60 X at index 0 but `_open(0)` returns an actual webcam after reorder; the backend never re-attests the opened handle. (2) selected stable ID A is busy, B is another card; `_candidate_indices` at `capture_card_backend.py:1215-1222` walks B and `start()` labels it `card_name` (`:1259-1262`), while `identity_verified()` at `:891-892` permits that basis without matching selected ID A. `RemotePlaySession.cpp:2540-2544` passes a one-time environment inventory at sidecar spawn; `:2765-2795` refreshes for a **new sidecar launch**, not each backend reopen within a live sidecar. The capture route can therefore misattribute device timing to the wrong card. **Fix:** bind verified opened handle's stable moniker/identity to the explicit pick; if not verifiable, no timing/fire authority; refresh/revalidate after reopen/hotplug and test two-card, webcam/reorder, OBS contention, generic-index-zero cases. Tier: source + dummy open; **no hardware assertion**. This is broader than the sheet's accepted same-second A4 risk.

### HIGH — patch-day detection and production-route gate are not release-proven

`LicenseClient.cpp:313-323` sends key/machine/version only; `backend/lambda_function.py` has no detector/armed-epoch telemetry field or fleet aggregation. There is no demonstrated model-mismatch sentinel, detection-only remote off policy, owner drill, or signed emergency update/rollback timing. `tests/test_detection_regression.py` skipped all six cases because its held-out corpus is absent, while the compiled sidecar was tested only by model smoke. The candidate sheet explicitly records **StrictSecurity not run** and owner VM/hardware canary not run. Independent scratch execution of the production route-transition fixture crashed instead of yielding a usable verdict; F2's 3/5 production failure must be reproduced in a fully isolated, correctly staged build before promotion. At frozen source `59129e5`, `tests/test_remote_play_client_stale_sweep.py:145-157` calls machine-wide `terminate_chiaki_processes()` **before** its `pytest.skip` when a stream is found (`git show 59129e5:tests/test_remote_play_client_stale_sweep.py`, read-only). The working-tree test now pre-skips, but that post-freeze edit is not rc1. A broad pytest run must not be used near a live stream until the fix is frozen and a process-isolated test proves it. **Fix/test:** privacy-minimal denominator counters and fleet alert, local explicit unavailable state/mismatch sentinel, detection-only signed policy, a held-out altered/missing/false-lock corpus through the **copied compiled sidecar**, staged production fixture 5/5 and StrictSecurity in an isolated VM, signed no-op emergency update/rollback drill. Tier: source/package-smoke and gate absence; live/customer effect unverified.

**CRITICAL:** none demonstrated in this reliability lane. A direct default-METER blind-fire claim would overstate the evidence.

## Input ownership and neutral-state fault ledger (dummy sink, not console receipt)

| Fault / observed fixture | Launcher/fork result | Ownership / human-control state |
|---|---|---|
| R2-up+L2-down+Square, reverse trigger, release+stick | `triggerReleaseSplitCarriesOnlyTerminalEdges`, 54-test suite pass; last confirmed packet plus terminal edges then next state | Owned while running; no human handback requested. Exact 7 packet test assertion, dummy—not console. |
| Simultaneous other digital presses; lost/missing/duplicate/late ACK | protocol QTests pass including ordered seq and `missingFinalAckClosesAmbiguousRouteWithoutReplay`; exact mixed Square+Triangle→Cross not separately exercised | Ambiguous route is closed; no claimed console receipt or human restoration. |
| Failed final/Failed-ACK or queue-drop path | fork dummy `delivery_timeout_failed_ack_soft_path`: `soft_fault cause=local_delivery_timeout`; applied buttons 0, session kept, pipe reconnect seeded | `owning=1`, latch=1, keepalive off during recovery; **human not restored** in same active session. |
| ABANDON write/read cancellation | launcher `blockedAbandonWriteCancelsWithoutBlockingOrDoubleReporting`; fork `enqueue_stall_abandon_soft_path`, delayed read/write cancel reapers pass | Soft neutral; latch=1, reconnect needed, no automatic human handback. |
| No ABANDON / plain disconnect while held / failed ENQUEUED ACK write | fork `plain_disconnect_while_owning_is_fatal` and `enqueue_stall_without_abandon_is_fatal`; exactly one stop request | Latch=1 until session teardown; stop requested. Actual console-human restoration not observed. |
| GUI/input writer silence with owned stick+trigger | fork `owner_silence_neutralizes`: neutral after **402 ms**, one episode, no stop; unflagged keepalive test passes | `owning=1`, latch=1, keepalive off while silent; human locked out until explicit release/session teardown. |
| Four rapid ABANDONs; soft hold without reconnect | `soft_fault_budget_exhausted` after 4; `soft_hold_expired` at **2069 ms**; exactly one stop | Neutral+latched, terminal stop requested; human restoration after real teardown unverified. |
| Sender-thread exit without client | fork `thread_exit_without_client` pass | No owned controls in that row; teardown bounded. Owned sender-thread-exit console behavior remains unverified. |
| Nine faults spaced >3.33 s apart | unit source explicitly permits all; not rerun through dummy pipe | Rolling window ages out; each soft period neutral+latched, no lifetime cap, no asserted human handback. |

## M / P closure register

“Partial” means a source or fixture pass, **not** a package/hardware closure.

| Item | This lane |
|---|---|
| M-02 log I/O | Partial: production QTest failure case passes; current `OrderedFileLogSink.cpp:75-132` uses one admission deadline and bounded `drain(timeout)`. Disk-full GUI Copy/Shutdown not VM-tested. |
| M-03/04/10 input soft fault, dead-man, trigger split | Partial: prod-linked native tests + fork dummy pipe pass; actual console/sender and full launch integration unverified. M-03 lifetime budget residual above. |
| M-08/09 second instance and lease/sleep | Source/unit policy only; two-process, sleep/internet and customer state unverified. |
| M-11 telemetry silence | Source ageing/watchdog exists; sidecar-stays-alive JSONL silence not exercised. |
| M-12 | Open: semantic-fault C++ fixture reproduced. |
| M-13 | Open: default METER warning/unavailable gap, no demonstrated blind fire; P9 operations missing. |
| M-15/33 capture | Open: wrong-device certification fixtures; 30-fps/low-fps and hardware not exercised. |
| M-16 Go-To late meter | Source tests only; held-out Go-To footage missing. |
| M-17 RP-only evidence | Hardware field run missing. |
| M-25 | Open: FF learner leak reproduced in source and dev log; rc1 runtime unverified. |
| M-26 | Open: source branch wrong on Cancel; controller fixture owed. |
| M-27 | 150-ms floor remains; fast RP rig data missing. |
| M-28/31/32 fork producer/teardown | Partial dummy-pipe and source tests; real producer/session teardown unverified. |
| M-29 shot records | Focused source tests previously passed root fallback but exact rerun/VM without D: still owed; no packaged assertion. |
| M-30 performance | Smoke 100 DML runs only; sustained 60-fps CPU/DML, memory, log rotation/UI unverified. |
| M-34 PS5 address propagation | Source only; no DHCP surrogate tested. |
| M-40 shot evidence | Source schema inspected; cross-build/settings scoreboard join not certified. |
| M-41 regression corpus | Open: six skipped; held-out patch footage absent from test path. |
| P1 | Partial: default-METER no-blind-fire dev fixture; compiled sidecar held-out footage/hardware unverified. |
| P2 | Partial: fork dummy-pipe and native policy fixtures; production route transition and full decoder session unverified. |
| P3 | Open: two capture identity failures; hardware cases owed. |
| P4 | Partial: dummy pipe neutral/ownership above; aggregate and real receipt/human restoration owed. |
| P5 | Partial: bounded log-sink fixture and DML smoke; disk/no-D:/sustained load owed. |
| P6 | Open: M-12/M-25/M-26; scoreboard only with matched identity, no live make-rate inference. |
| P7 | Not re-audited in this reliability lane; refer to dedicated IPC/security lane, no closure claim. |
| P8 | Not executed: sleep, Wi-Fi, coexistence, dual-launch, reboot/update require disposable owner rig/VM. |
| P9 | Blocked: METER warning gap, missing fleet/drill/held-out corpus. Pill correctly excluded, but other unsupported styles persist. |

## Owner-run closure, in priority order

1. Freeze a next candidate and re-hash source/build/package; rerun **StrictSecurity only in a disposable VM with no owner stream** after reviewing every process-sweep test. Reproduce the production route-transition fixture 5/5 with its fake sidecar and no writes outside its isolated tree.
2. On an isolated capture rig, run a filmed Arrow2 normal/missing/altered/false-lock corpus through the **copied rc1 compiled sidecar**, including the first affected Square press and explicit METER/NO METER controls. Record detector eligibility, raw/owned epochs, warning, release count, and one genuine recovery. Do not infer from source or model smoke.
3. Run selected-card A/B, hotplug reorder, webcam, generic card, OBS contention and 30/60-fps degradation. Compare the opened handle stable identity with the selected ID at every reopen. Refuse timing authority on mismatch.
4. Complete dummy packet-sink matrix for mixed Square+Triangle→Cross, loss/out-of-order ACKs, nine slow soft faults, sender exit while owned, queue-drop and Failed-ACK write; then controlled rig confirm neutral/human state after full session stop. No real console endpoint in this audit.
5. Use a disposable VM without D:, faulted full disk and sleep/network/clock/reboot surrogates to time GUI input, shutdown, record retention, 60-fps CPU fallback and visible recovery. Run a signed no-op emergency update plus rollback drill with fleet alert/MOTD evidence.

## Transaction record for the semantic-learning fault fixture

Changed branch/field: `AppConfig::reloadLearning/saveLearning`, `learning.json/shot_type_learned_offset_ms.Standstill`. The **only** mutated input is a copy of the 10-ms valid fixture, changed to 9999999 ms; live `learning.json` remains untouched. Four reopened artifacts in my TEMP directory:

- `MODIFIED_FILE`: `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\MODIFIED_FILE` (SHA256 `A3C943B85424FB7426FF3F75DDE3B741C01A482FB0CB8848596175333131BE80`)
- `DIFF_FILE`: `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\DIFF_FILE`
- `VERIFICATION.txt`: `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\VERIFICATION.txt`
- executable `ROLLBACK.sh`: `C:\Users\aaron\AppData\Local\Temp\codex-reliability-rc1-20260923\ROLLBACK.sh`

With `$t="$env:TEMP\codex-reliability-rc1-20260923"` and PATH including scratch `build\orion\Release` and Qt, exact command/input/output/exit:

```text
BASELINE: Copy-Item -LiteralPath "$t\BASELINE_FILE" -Destination "$t\baseline-run\learning.json" -Force; Copy-Item -LiteralPath "$t\BASELINE_FILE" -Destination "$t\baseline-run\learning.json.bak" -Force; & "$t\build\Release\appconfig_probe.exe" "$t\baseline-run" baseline
OUTPUT: mode=baseline loaded_offset=10 loaded_hold_present=1 load_note_empty=1 backup_before=10 save_ok=1 backup_after=10
EXIT: 0
MODIFIED: Copy-Item -LiteralPath "$t\MODIFIED_FILE" -Destination "$t\modified-run\learning.json" -Force; Copy-Item -LiteralPath "$t\BASELINE_FILE" -Destination "$t\modified-run\learning.json.bak" -Force; & "$t\build\Release\appconfig_probe.exe" "$t\modified-run" modified
OUTPUT: mode=modified loaded_offset=1e+07 loaded_hold_present=0 load_note_empty=1 backup_before=10 save_ok=1 backup_after=1e+07
EXIT: 0
ROLLBACK: Copy-Item -LiteralPath "$t\MODIFIED_FILE" -Destination "$t\rollback-copy\learning.json" -Force; & 'C:\Program Files\Git\bin\bash.exe' "$t\ROLLBACK.sh" "$t\rollback-copy\learning.json"
OUTPUT: ROLLBACK_COPY_OK=baseline_sha256_restored
EXIT: 0
RESTORED COPY SHA256: 3F12AB61FBB0176C36E01A38828021D9121C4C96A76A661F90D65FC753CDAA4E
MODIFIED_FILE_UNCHANGED: True
RESTORED BEHAVIOR: Copy-Item -LiteralPath "$t\rollback-copy\learning.json" -Destination "$t\rollback-copy\learning.json.bak" -Force; & "$t\build\Release\appconfig_probe.exe" "$t\rollback-copy" baseline
OUTPUT: mode=baseline loaded_offset=10 loaded_hold_present=1 load_note_empty=1 backup_before=10 save_ok=1 backup_after=10
EXIT: 0
```

The original valid input hash was `3F12AB61FBB0176C36E01A38828021D9121C4C96A76A661F90D65FC753CDAA4E`. `DIFF_FILE` contains only the two changed learning fields. Rollback was tested on **another copy** and left `MODIFIED_FILE` changed. This transcript is a source-built parser fixture, not execution of `OrionNative.exe`.

