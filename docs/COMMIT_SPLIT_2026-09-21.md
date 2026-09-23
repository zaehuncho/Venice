# Commit split — 2026-09-21 (prepared by Claude, owner decides)

Astra's beta release gate starts with **clean commits in BOTH repos** (NexusVision and the sibling
`chiaki-ng-src`, branch `orion`). This is the split of everything uncommitted on top of
`0eca7d2 launch checkpoint`, grouped by intent so each commit is reviewable on its own. Nothing here
has been committed; every unit below was verified as described. `learning.json` is runtime data and
is **excluded** everywhere.

Two files need hunk-level staging (`git add -p`) because they carry two units at once:
- `native_orion/CMakeLists.txt` — U1 owns only the `add_library(UpdaterCore …)` source-list hunk
  (adds `src/UpdaterTrust.cpp/.h`); every other hunk (broker targets, tests, definitions) is U2.
- `backend/lambda_function.py` — U3 owns the two price-copy hunks (`@@ -110` "$25 → $20/month" and
  `@@ -2577`); the activation hunks (`@@ -551` `session_only`, `@@ -802` conditional removal of
  license material / profile) are U2. **Never deploy the Lambda from a tree that mixes them** —
  see the "Lambda TRAP" in memory/beta-release-gate.

## U5 — launcher UX: first-run gate, profile card, Xbox ack, customer error copy, Pill removed
Verified: native ctest 30/30 on the relinked exe; pytest contracts reconciled (40/40 in the sweep).
```
git add native_orion/qml/components/MeterConfigPanel.qml native_orion/qml/components/Sidebar.qml \
  native_orion/qml/components/StreamSetupForm.qml native_orion/qml/pages/RemotePlayPage.qml \
  native_orion/qml/pages/StreamSetupGate.qml native_orion/src/AppConfig.cpp native_orion/src/AppConfig.h \
  native_orion/src/OrionAppController.cpp native_orion/src/OrionAppController.h \
  native_orion/src/RemotePlaySession.cpp native_orion/src/RemotePlaySession.h \
  native_orion/tests/RemotePlayExecutablePolicyTests.cpp native_orion/tests/XboxRemotePlayPolicyTests.cpp \
  tests/test_venice_ui_contract.py tests/test_non_live_production_surface_contract.py \
  tests/test_controller_route_lifecycle_contract.py tests/test_decoder_ordering_reliability.py \
  scripts/verify_orion.ps1 chiaki_backend.py native_orion/backend/autogreen_sidecar.py
```
(`chiaki_backend.py` and `autogreen_sidecar.py` carry small remote-play-path diffs with no other
unit's keywords — eyeball `git diff` on both before staging.)

## U8 — installer: branded wizard, driver install checks, graceful close, fail-closed service
Verified: ISCC stub compile exit 0; build_installer.ps1 throws without a signing command; 06:46 a real
dev package (2939 files, tonight's binaries) passed the package security audit except the expected
missing .sig, and the installer was compiled against it with -AllowUnsigned (see memory
packaging-pipeline-dry-run-20260921).
```
git add installer/orion.iss installer/build_installer.ps1 installer/INSTALL_NOTICE.txt installer/assets/ \n  tests/test_installer_uninstall_path.py    # contracts reconciled to the graceful close + numeric SCM poll
```

## U4 — website: BETA tag + honest beta line, $20, constellation starfield, /connect outage copy
Verified: `website/tests/verify_site.py` VERIFY_OK; worker tests 38/38; starfield harness 119/120.
```
git add website/public/index.html website/public/styles.css website/public/app.js \
  website/src/worker.js website/tests/verify_site.py website/VERIFICATION.txt website/REDESIGN_2026-09-19.md
```

## U3 — pricing + "the Venice bot" copy: Discord embeds, bot, Lambda price lines only
Verified: embeds parse; tests/discord 40/40. Lambda: stage ONLY the two price hunks.
```
git add discord_launch/launch_embeds/launch_announcement.json discord_launch/launch_embeds/pricing.json \
  discord_launch/launch_embeds/purchase_command.json discord_launch/launch_embeds/setup_guide.json \
  discord_launch/launch_embeds/welcome_terms_patch.json discord_launch/orion_bot.py \
  tests/discord/test_orion_bot_admin_v2.py tests/discord/test_venice_guard_refresh.py
git add -p backend/lambda_function.py      # accept @@ -110 and @@ -2577 only
```

## U7 — meter detection (now Astra's lane; the diff is Claude's 09-21 work, verified 834/834)
Static-zone ordered-rise release + repeat-offender TTL + anchor/idle instrument + health-line order.
```
git add simple_meter_reader.py meter_locator_cv.py player_anchor.py \
  tests/test_reader_fresh_onset_lock.py tests/test_meter_locator_cv.py tests/test_player_anchor.py \
  tests/test_simple_reader_sidecar_wiring.py tests/test_green_cap_association.py tests/fixtures/meter/ \
  docs/HANDOFF_METER_DETECTION_2026-09-21.md
```

## U6 — timing observers: probe schema 2, observer/input-trace audits, shot-record clocks, PS5 net study
Verified: pytest (part of the 2018-pass standard run).
```
git add native_orion/src/AutomationEngine.cpp native_orion/src/AutomationEngine.h \
  native_orion/src/ShotGateProtocol.h native_orion/tests/AutomationEngineTests.cpp \
  shot_records.py tests/test_shot_records.py tests/test_shot_records_wiring.py tests/test_shot_record_clocks.py \
  remote_play_orchestrator.py tools/timing/ tests/test_probe_audit.py tests/test_probe_audit_counterexamples.py \
  tests/test_probe_audit_exhaustive.py tests/test_probe_gate_aggregation.py tests/test_probe_schema_conformance.py \
  tests/test_observer_worker_ledger.py tests/test_input_trace_audit.py tests/test_pipeline_clock_trace.py \
  lossless_frame_archive.py tests/test_lossless_frame_archive.py
```

## U1 — updater trust root + reparse-point refusal (SECURITY — commit after Codex verdict + Strict)
Verified: OrionUpdaterTests 59/59 (26 new; one privilege-gated file-symlink case skips on an unelevated account) in the dev tree (production tree re-verified per round),
configured tree (native_orion/build_prod_review); in-repo harnesses updater_e2e_dev 9/9 and
updater_e2e_prod 7/7 on the real binaries; full ctest 30/30. Six Codex rounds: every original finding APPROVED; the verdict stays
'blocked' only on owner gates (Strict with the signing key, a staging manifest, one elevated ctest run
for the privilege-gated file-symlink case). UNC install roots are refused outright. LicenseClient.cpp: update-check
logs the server's JSON envelope reason on HTTP errors (fail-soft unchanged).
```
git add native_orion/src/UpdaterTrust.h native_orion/src/UpdaterTrust.cpp \n  native_orion/src/UpdaterArchive.h native_orion/src/UpdaterArchive.cpp native_orion/src/updater_main.cpp \n  native_orion/tests/OrionUpdaterTests.cpp docs/UPDATER_CLIENT.md \n  native_orion/src/LicenseClient.cpp tools/security/updater_e2e/ tests/test_packager_updater_attestation.py \n  tests/test_packager_signing_key_passphrase.py
git add -p native_orion/CMakeLists.txt     # accept ONLY the UpdaterCore source-list hunk AND the
                                           # ORION_UPDATE_ED25519_PUBKEY2 rotation-key hunk
git add -p tools/package_orion_release.py  # accept the updater-attestation hunks AND the encrypted-key passphrase hunk
                                           # (@@ -956 function, @@ -1039 call); the rest is U2
```

## U2 — activation broker / server-shard packaging (SECURITY — Astra's; BLOCKED per her gate)
Everything broker/shard: sources, tests, packing tools + docs, security_core CMake, the remaining
CMake hunks, the Lambda activation hunks. Commit only on Astra's word; it is not deployable yet.
```
git add native_orion/broker/ native_orion/src/BrokerInstallTrust.cpp native_orion/src/BrokerInstallTrust.h \
  native_orion/src/MachineIdentity.cpp native_orion/src/MachineIdentity.h native_orion/src/PinnedSpki.h \
  native_orion/src/main.cpp native_orion/src/NetworkSecurity.cpp native_orion/src/SecurityManager.cpp \
  native_orion/security_core/CMakeLists.txt \
  native_orion/tests/ActivateBrokerContractTests.cpp native_orion/tests/BrokerInstallTrustTests.cpp \
  native_orion/tests/BrokerSelfTrustGateTests.cpp native_orion/tests/BrokerSessionStoreTests.cpp \
  native_orion/tests/BrokerSpkiPinTests.cpp native_orion/tests/MachineIdParityTests.cpp \
  tools/package_orion_release.py tools/security_audit.py tools/release_filter_policy.py tools/security/pack_lethe_release.py \
  tests/test_packer_hardening.py tests/test_security_audit.py tests/test_release_filter_hygiene.py \
  tests/test_server_shard_release_gate.py tests/test_packing_workflow.py tests/backend/test_pairing.py \
  docs/ACTIVATION_BROKER_2026-09-19.md docs/SERVER_SHARD_2026-09-19.md docs/PACKING_RUNBOOK.md docs/LAUNCH_PACKING_CHECKLIST.md
git add -p native_orion/CMakeLists.txt     # the remaining hunks
git add -p tools/package_orion_release.py  # the remaining hunks (everything but U1's attestation)
git add -p backend/lambda_function.py      # @@ -551 and @@ -802
```

## Excluded
`learning.json` (runtime state; already in .gitignore's spirit — do not stage).

## Sibling repo: chiaki-ng-src (branch orion)
Two layers, both uncommitted:
1. **Pre-09-02 transport/decoder work** (streamsession.cpp, takion.{c,h}, thread.{c,h}, time.{c,h},
   videoreceiver.{c,h}, orionavclock.{c,h}, lib/CMakeLists.txt, test/*) — this IS the deployed
   OrionStream (every file predates the 09-02 23:22 build). Astra: "one checkout loses it; never
   run patch_tier1.py". Commit as `orion: transport + av-clock (as deployed 2026-09-02)`.
2. **09-21 stuck-button fix** — `lib/include/chiaki/orioninput.h lib/src/orioninput.c
   lib/src/feedbacksender.c test/feedbacksender_orion.c` (chiaki-unit 124/124). Commit separately.
The ~50 untracked helper scripts / logs in that tree (`patch_*.py`, `*.log`, `chiaki-ng-Win/`) are
build scratch, not source — leave them untracked or add to .gitignore; do not commit.

## Suggested order
U5 → U8 → U4 → U3 → U7 → U6 (all verified, customer-facing first), then U1 and U2 once their
Codex verdicts are in, so each security commit message can cite the verdict and the Strict run.

## U9 - onset feedforward (engine) + court RTT stamp (sidecar) + grader  [added 14:40]

Owner: "onset forward anyway" / "call it and build it". Timing, not security; Standard verify.

- native_orion/src/OnsetFeedforward.h (NEW, header-only policy)
- native_orion/src/AppConfig.h / AppConfig.cpp (onset_ff_* keys: fields, validate, save, load)
- native_orion/src/AutomationEngine.h (include, RemapConfig fields, members, decl)
- native_orion/src/AutomationEngine.cpp (env overrides, ring feed, choke-point displacement,
  base recovery x5, lease bounds x3, clear/carry, Release onsetff line, PHASE SAMPLE field)
- native_orion/CMakeLists.txt (header listed) - HUNK: keep separate from the U1 updater hunks
- native_orion/tests/AutomationEngineTests.cpp (3 tests + 5 keys in the round-trip test)
- run_orion.local.ps1 (scrub list)
- shot_records.py + remote_play_orchestrator.py + tests/test_shot_records.py (court RTT stamp)
- tools/timing/onset_ff_grade.py (NEW), docs/ONSET_FEEDFORWARD_2026-09-21.md (NEW)

## U8 addendum - Qt Quick style prune  [added 14:40]

- tools/package_orion_release.py: prune_unused_quick_styles + QUICK_STYLE_ROOTS (HUNK: separate
  from the updater-attestation hunks already assigned to U1)
- tests/test_packager_quick_style_prune.py (NEW), tests/test_release_packaging.py (spy test)
- docs/PACKING_RUNBOOK.md section "Qt Quick style prune"

Evidence 14:35: native 30/30 (OrionNativeTests 1227/0/7 skipped); packing suites 61 + shot
records 117 green; package 668 files, both apps launched + rendered from it.
