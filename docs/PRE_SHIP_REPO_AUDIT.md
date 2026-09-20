# Pre-Ship Repository Audit

**Date:** 2026-09-17
**Branch:** `fix/timing-input-and-remoteplay-blockers`
**Scope:** READ-ONLY audit ahead of release wrap. No git mutations, nothing deleted, nothing modified except this report.
**Working-tree counts at audit time:** 183 untracked (`??`), 146 modified (` M`), 83 deletions (` D`).

> **No secret values are printed in this report — names and paths only.**

---

## 0. Executive summary — the two things that must happen before this repo/package leaves the machine

1. **PURGE 25 tracked reverse-engineering / auth-bypass / model-theft scripts from the repo root** (they target the "Helios" / "InputSense" / `inputsense.com` competitor stack). All are git-tracked, so they travel with any clone/push/backup. **None are imported by product code, tests, the sidecar bundle manifest, CMake, or the build scripts** — safe to remove. See §1 and the **PURGE LIST** in §6.
2. **Delete `codesigning/venice_update_signing.pem` from the box.** It is **present on disk (119 bytes)** — this violates the standing rule that it must never be on this machine. It is **not** git-tracked (covered by `.gitignore` `/codesigning/` and `*.pem`), so it will not be committed, but it must be removed from disk by the owner (I cannot delete).

**Two `.gitignore` gaps that could leak secrets on a careless `git add -A`:** the sensitive `settings.json.bak-*`, `learning.json.bak-*`, and `models/tip_registration.json.bak-*` backups are **NOT ignored** (they contain the owner's console IP + dev licence key / dev learner state). See §4.

---

## 1. Repo-root RE / auth-bypass / model-theft tooling — CLASSIFY

All target the competitor "Helios2.exe" / `CvPython.dll` / `InferenceCore.dll` / `ch.dll` / `inputsense.com` / `_2k_Vision`. **Every one is git-tracked.** Load-bearing check (§5) proves nothing in the product imports any of them.

### 1a. The 12 named candidates

| File | What it does | Tracked | Referenced by product? | Verdict |
|---|---|---|---|---|
| `hook_helios_load.py` | Frida-attaches to Helios2.exe, hooks `SetEnvironmentVariableW` to learn its env before it loads `ch.dll` | yes | no | **PURGE** |
| `monitor_helios.py` | Frida monitor of Helios DLL/model loading (`ch.dll`, `InferenceCore.dll`, `infcore_load_model`) | yes | no | **PURGE** |
| `rip_helios.py` | "Aggressive Helios.exe patcher — bypass all auth checks"; patches competitor PE | yes | no | **PURGE** |
| `runtime_auth_bypass.py` | Frida hook of `isUserAuthenticated` → always true | yes | no | **PURGE** |
| `capture_model.py` | Frida-hooks `infcore_load_model` / `infcore_set_session` to steal model data + auth token; writes to `Desktop\EXPLOITS\helios_bypass\models` | yes | no | **PURGE** |
| `hook_infcore.py` | Frida hook of `InferenceCore.dll` to intercept model loading + capture decryption | yes | no | **PURGE** |
| `hook_python_load.py` | Frida hook of Helios Python loading path | yes | no | **PURGE** |
| `network_intercept.py` | Intercepts the model-decryption-key request to `inputsense.com` (Frida/WinHTTP) to extract the key | yes | no | **PURGE** |
| `nexus_core.py` | Standalone ONNX inference wrapper ("no auth, no network"); runner for the **extracted** competitor models | yes | no (self only) | **PURGE** (built on ripped models; not product) |
| `nexus_loader.py` | Standalone loader that "runs extracted ONNX models directly without Helios/InputSense auth stack"; "compatible with Helios model output format" | yes | no (self only) | **PURGE** (built on ripped models; not product) |
| `onnx_dumper.py` | Attaches to Helios, dumps decrypted ONNX models from process memory | yes | no | **PURGE** |
| `patch_auth_funcs.py` | Patches `isUserAuthenticated` in `CvPython.dll` to always return true | yes | no | **PURGE** |

> Note on `nexus_core.py` / `nexus_loader.py`: docstrings frame them as "what you integrate into your NexusVision product," **but** they explicitly run *extracted* competitor models and reference the Helios/InputSense format. The actual shipping product (native_orion sidecar, `simple_meter_reader.py`, `meter_detector*.py`, the OrionSidecar bundle) does **not** import them (§5). They are the standalone runners for the ripped models — **PURGE**. If the owner believes either contains original first-party code worth keeping, extract that code into a product module first; do not ship these files.

### 1b. Additional RE tooling found by content sweep (not in the original 12) — all tracked, all repo-root

| File | What it does | Tracked | Verdict |
|---|---|---|---|
| `surgical_patch.py` | Finds/patches the specific `CvPython.dll` auth check that hides the Start button | yes | **PURGE** |
| `rip_infcore.py` | "Aggressive InferenceCore.dll patcher — bypass model auth" | yes | **PURGE** |
| `rip_cvpython.py` | "Aggressive CvPython.dll patcher — enable Start button without auth" | yes | **PURGE** |
| `runtime_hook.py` | Frida hook of Helios to extract model data at load | yes | **PURGE** |
| `run_direct.py` | Runs competitor `_2k_Vision` script directly using patched `ch.dll` | yes | **PURGE** |
| `test_init.py` | Loads/tests the patched `ch_v2.dll` (RE scratch, not a pytest) | yes | **PURGE** |
| `patch_init_call.py` | Patches the init call in `ch.dll` `DllMain` to succeed | yes | **PURGE** |
| `test_patched2.py` | Loads/tests patched `ch_patched.dll` (RE scratch) | yes | **PURGE** |
| `patch_dllmain.py` | Patches `ch.dll` `DllMain` to return TRUE immediately | yes | **PURGE** |
| `test_patched_dll.py` | Loads/tests patched `ch.dll` (RE scratch) | yes | **PURGE** |
| `patch_ch_dll.py` | `ch.dll` auth-bypass patcher (stub CheckAuth/GetAuthStatus/IsLicensed) | yes | **PURGE** |
| `quick_extract.py` | Frida ONNX extractor — hooks model load and dumps | yes | **PURGE** |
| `infcore_shim.cpp` | C++ `InferenceCore.dll` auth-bypass shim (forward-to-real DLL) | yes | **PURGE** |

**Total RE/bypass files to purge: 25** (24 `.py` + 1 `.cpp`), all git-tracked, all repo-root.

### 1c. Product files that legitimately mention RE terms — KEEP

| File | Why it matched | Verdict |
|---|---|---|
| `native_orion/src/SecurityManager.cpp` | Lists `"frida"`, `"radare2"`, `"r2"` as blacklisted analysis-tool process names (product anti-tamper detection) | **KEEP (product)** |
| `tools/security/pack_lethe_release.py` | Owner's OWN Lethe release-packer wrapper (defensive, first-party) | **KEEP (build tooling)** — see §2c |
| `tools/security_audit.py`, `tools/release_filter_policy.py`, `tools/package_orion_release.py`, backend/tests with "license"/"bypass" strings | First-party licensing / release-hygiene code and tests | **KEEP (product)** |

---

## 2. Untracked inventory (183 `??`) — buckets

### 2a. MUST ADD — build / test / product inputs (verified not `.gitignore`-matched; verified load-bearing)

**Product Python modules (repo root)** — imported by product and/or covered by an added test:
- `async_diagnostic_csv.py`, `banner_verdict_live.py`, `meter_locator_cv.py`, `player_anchor.py`, `shot_records.py`, `stall_attributor.py`
  - e.g. `simple_meter_reader.py` imports `meter_locator_cv`; `banner_verdict_live.py` is a sidecar-referenced grader; each has a matching `tests/test_*.py`.

**Build-gated grader inputs (required by `tools/sidecar_bundle_manifest.py`)** — the sidecar `source_files()` raises `FileNotFoundError` if these are absent:
- `tools/timing/panel_grade.py`  (READER_SOURCE_INPUTS — compiled into the sidecar as `orion_panel_grade`)
- `tools/timing/panel_templates.npz`  (READER_DATA_INPUTS — 13.5 KB template library; small, safe to commit)

**Native C++ headers (`#include`d by tracked `.cpp/.h`; omitting them breaks the build):**
- `native_orion/src/BannerLeadTrim.h` (AppConfig, AutomationEngine, OrionAppController)
- `native_orion/src/FireEpochClock.h` (OrionAppController)
- `native_orion/src/GameFramePhase.h` (AutomationEngine)
- `native_orion/src/InputSessionRetryPolicy.h` (OrionAppController, RemotePlaySession)
- `native_orion/src/ManualShotTally.h` (OrionAppController)
- `native_orion/src/ShotGateProtocol.h` (RemotePlaySession; named in CMakeLists.txt)
- `native_orion/src/ShotVerdictTally.h` (OrionAppController)
- `native_orion/src/SquareOutputWatchdog.h` (OrionAppController)

**Native C++ tests:**
- `native_orion/tests/ActivityFeedPolicyTests.cpp`, `FireEpochClockTests.cpp`, `InputSessionRetryPolicyTests.cpp`, `SharedMemoryFramePumpNotificationTests.cpp`, `ShotVerdictTallyTests.cpp`

**QML — Admin Panel V2 (referenced by the modified `native_orion/qml/admin/AdminMain.qml`):**
- `native_orion/qml/admin/` (18 files): `AdminAccountPage.qml`, `AdminAuditPage.qml`, `AdminButton.qml`, `AdminCard.qml`, `AdminCombo.qml`, `AdminConfigPage.qml`, `AdminDashboardPage.qml`, `AdminField.qml`, `AdminJsonView.qml`, `AdminKeyValue.qml`, `AdminLicensesPage.qml`, `AdminLoginPane.qml`, `AdminPill.qml`, `AdminReasonField.qml`, `AdminSecretReveal.qml`, `AdminStaffPage.qml`, `AdminStatTile.qml`, `AdminToggle.qml`
- `native_orion/qml/components/NoMeterCard.qml`, `native_orion/qml/components/ShotVerdictTally.qml`

**Python tests (`tests/`)** — ~50 files; each imports a product/tool module that must also be present (see interdependency note below):
- `tests/backend/`: `test_admin_v2.py`, `test_discord_subscription_entitlement.py`, `test_pairing.py`, `test_plan_surface.py`, `test_profile.py`
- `tests/discord/` (new directory)
- `tests/test_*.py` (~45): including `test_banner_verdict_live.py`, `test_meter_locator_cv.py`, `test_player_anchor.py`, `test_shot_records.py`, `test_shot_records_wiring.py`, `test_stall_attributor.py`, `test_async_diagnostic_csv.py`, `test_epoch_table.py`, `test_poll_phase_fold.py`, `test_timing_motion_audit.py`, `test_consistency_bench.py`, `test_manual_shot_tally.py`, `test_native_qtest_diagnostics.py`, `test_release_oracle.py`, and the capture/decoder/framedump suite.

**Scripts:** `scripts/run_orion_qtest.ps1`

> **Interdependency note:** several new tests import new tool modules. If you add the test but not its module, pytest collection fails. Pairs to keep together: `test_epoch_table.py`↔`tools/timing/epoch_table.py`; `test_poll_phase_fold.py`↔`tools/timing/poll_phase_fold.py`; `test_timing_motion_audit.py`↔`tools/timing/timing_motion_audit.py`; `test_consistency_bench.py`↔`tools/quality/consistency_bench.py`; plus the root-module pairs above. Easiest safe path: add the whole `tools/timing/`, `tools/quality/` new files together with the tests.

### 2b. MUST NEVER COMMIT — secrets / scratch / backups

| Path(s) | Why | `.gitignore` status |
|---|---|---|
| `settings.json.bak-*` (12 files: `-2k27-…`, `-ip-204531`, `-ip138-…`, `-lead275/285/290/293/298/300-…`, `-leftfade6-…`, `-pretrim-…`, `-rightfade-…`) | Dev settings backups — contain owner **console IP + dev licence key** (confirmed marker match in `settings.json.bak-ip-204531`) | **NOT IGNORED — GAP** |
| `learning.json.bak-2k27-20260826_200402` | Dev learner state backup | **NOT IGNORED — GAP** |
| `models/tip_registration.json.bak-2k26-20260903` | Dev calibration backup (2K26-era) | **NOT IGNORED — GAP** |
| `probe.txt` | 46 KB QtTest console log (scratch: "Start testing of AutomationEngineTests") | **NOT IGNORED — GAP** |

> `.gitignore` has `*.bak` and `*.bak_*`, but these files use the extension `.bak-<tag>` (hyphen), which **neither pattern matches**. Add `settings.json.bak-*`, `learning.json.bak-*`, `*.json.bak-*`, and `probe.txt` (or `/probe.txt`) to `.gitignore`, or delete the backups. The release **package** filter (`tools/release_filter_policy.py`) also will not catch them: its `.bak` suffix rule matches `.endswith('.bak')`, not `.bak-…`.

### 2c. UNCLEAR — owner discretion (no secrets found; add or leave per intent)

- **Working/scratch docs at root** (ephemeral session notes; safe to leave untracked or delete): `BOT_MAXOUT_PLAN_2026-08-11.md`, `BUG_AUDIT_BRIEF.md`, `BUG_AUDIT_REPORT_2026-08-11.md`, `METER_DELAY_ASK_2026-08-11.md`, `METER_DELAY_PLAN_2026-08-12.md`, `REVIEW_ASK_2026-08-11.md`, `REVIEW_RESULT_2026-08-11.md`, `REVIEW_RESULT_2026-08-13.md`, `RIG_BATCH_2026-08-11.md`, `TIP_TIMING_CONSISTENCY_2026-08-12.md`
- **`docs/` (add — real documentation):** `ADMIN_PANEL_V2_CONTRACT.md`, `ANIMATION_ANCHOR_V2.md`, `DETECTOR_LOWFILL_FINETUNE_2026-09-03.md`, `DISCORD_SERVER_SETUP_PROMPT.md`, `HANDOFF_2026-09-13_MAKE_RATE_CEILING.md`, `HANDOFF_2026-09-14_ADMIN_PANEL_V2.md`, `HANDOFF_2026-09-14_UI_POLISH.md`, `NO_METER_V2_DESIGN.md`, `PROMPT_external_review_timing_residual.md`
- **`discord_launch/` Nereus bot infra (no hardcoded tokens found; add if this is the deployed bot):** `NEREUS_RUNBOOK.md`, `discord_commands.json`, `nereus.service`, `nereus_bot.py`, `register_commands.py`, `run_nereus.sh`, `venice_guard.py`
- **`tools/security/pack_lethe_release.py`** — owner's Lethe release-packer wrapper. Add as build tooling (first-party).
- **`tools/timing/` diagnostics (add the test-paired ones; the rest are dev tooling):** `banner_join.py`, `epoch_table.py`, `meter_trajectory.py`, `nometer_prior_audit.py`, `poll_phase_fold.py`, `poll_phase_fold_v2.py`, `poll_phase_probe.py`, `rescale_meter_time.py`, `timing_motion_audit.py`
- **`tools/quality/` (add `consistency_bench.py`; rest dev):** `consistency_bench.py`, `green_window_probe.py`, `reencode_gate_report.py`, `reencode_gate_study.py`, `reencode_gate_sweep.py`
- **`tools/diagnostics/` (~21 dev diagnostics — optional):** anchor_*.py, farshot_*.py, hud_*.py, icon_badge_probe.py, audit_lowfill_evaluation.py, pose_realtime_demo.py, replay_with_presses.py, `fix_dualsense_usb_power.ps1`
- **`tools/training/` (~11 training scripts — dev/optional):** eval_meter_*.py, finetune_meter_*.py, label_*.py, mine_lowfill_hardset.py, train_meter_2k27.py
- **`website/`** — 44 files (no `.env`/`.pem` found). Owner decision whether the marketing site belongs in this repo.

---

## 3. Modified inventory (146 ` M`) — grouped, with flags

| Area | Representative files | Flag |
|---|---|---|
| Native C++ engine/src | `AutomationEngine.*`, `OrionAppController.*`, `RemotePlaySession.*`, `OrionInputClient.*`, `AppConfig.*`, `LicenseClient.*`, `SharedMemoryFramePump.cpp`, `main.cpp`, many `*Policy.h` | OK (commit) |
| Native C++ tests | `AutomationEngineTests.cpp`, `OrionInputProtocolTests.cpp`, `SharedMemoryFrameReaderTests.cpp`, etc. | OK |
| QML UI | `Theme.qml`, `AppShell.qml`, `admin/AdminMain.qml`, dashboard/components/pages/* | OK |
| Readers/detectors (Python, crown-jewel) | `simple_meter_reader.py`, `compressed_meter_reader.py`, `meter_detector.py`, `meter_detector_yolo.py`, `latency_estimator.py`, `tip_registration_infer.py` | OK |
| Backend / Discord / worker | `backend/lambda_function.py`, `discord_launch/*`, `orion_worker.js`, `wrangler.toml` | OK (no secrets found in diffed files) |
| Build / manifest / scripts | `native_orion/CMakeLists.txt`, `tools/sidecar_bundle_manifest.py`, `scripts/build_orion_sidecar.ps1`, `requirements-build.txt`, `pytest.ini` | OK |
| Docs | `docs/*.md`, `PICKUP_PROMPT_*.md`, `VENICE_EXTERNAL_AI_PROMPT.md` | OK |
| **`learning.json`** | Dev learner state, **tracked + modified** | **REVIEW** — committing dev-tuned learner state to the repo. It IS stripped from the customer *package* by `release_filter_policy.py` (listed there), so it will not ship to customers; commit only if this is the intended shipping baseline for the repo. |
| **`models/tip_registration.json`** | Meter tip-registration model, tracked + modified | **KEEP** — it is a required `MODEL_INPUT` in `sidecar_bundle_manifest.py` and an `APPROVED_MODEL_FILES` entry; must stay tracked. |
| `.gitattributes`, `.gitignore` | Config | OK — but see §4 gaps (the `.gitignore` change should also close the `.bak-*` gap). |
| `.claude/scheduled_tasks.lock` (` D`) | Lock-file deletion | OK |

**Deletions (83 ` D`) — expected refactor:** almost the entire ` D` set is `tools/security/packer/**` (the old "orion" packer: stub C, VM `.vasm`, GUI, tests, docs) plus `tools/security/pack_orion_release.py` and `tests/test_orionpack_container.py` and `native_orion/qml/pages/GeneralPage.qml`. This matches the migration to the owner's Lethe packer (`tools/security/pack_lethe_release.py`, untracked). These deletions look intentional; no action beyond confirming the Lethe path is the replacement.

---

## 4. Secrets scan (names / paths only)

- **`codesigning/venice_update_signing.pem` — PRESENT ON DISK (119 bytes).** Not git-tracked (ignored by `/codesigning/` and `*.pem`). **Standing rule violation: it must not be on this box.** Owner must delete it from disk (I cannot). It is safe from being committed.
- **Sensitive backups NOT covered by `.gitignore`** (see §2b): `settings.json.bak-*` (owner console IP + dev licence key), `learning.json.bak-*`, `models/tip_registration.json.bak-*`. **Highest-priority gap** — one `git add -A` would stage the dev licence key + console IP.
- **Root `settings.json`** (dev settings, on disk, 9 KB): **untracked and ignored** (`.gitignore` `/settings.json`). Also stripped from the package by `release_filter_policy.py`. OK — will not ship via git; do not `git add` it.
- **`ORION-…` licence-key pattern in tracked files:** all matches are **placeholders / format strings, not real keys.** The only two non-test hits are benign: `latency_estimator.py:168` `_CACHE_MAGIC = b"ORION-…\0"` (a cache-file magic constant) and `native_orion/src/Diagnostics.cpp:11` (a comment documenting the key format for redaction). The rest are backend/admin test fixtures. The real dev key lives only in the untracked `settings.json` (+ its `.bak-*` backups).
- **Discord/AWS/Cloudflare/Gumroad tokens:** no hardcoded token patterns found in the untracked Nereus bot files (`nereus_bot.py`, `venice_guard.py`, `register_commands.py`, `discord_commands.json`, `nereus.service`, `run_nereus.sh`).
- **Test-key material** (`*.pem`, `*.pfx`, `*.p12`, `*.key`) exists only under gitignored scratch trees (`.pytest_codex_strict*/`, `Usersaaron*pytest*/`, `.codex_artifacts/`) — all covered by `.gitignore`. OK.

**`.gitignore` coverage — good:** `*.pem *.key *.crt *.p12 *.pfx`, `/codesigning/`, `/settings.json`, `/settings.json.sig`, `credentials.json`, `auth_tokens.json`, `.env*`, `*.local.*`, `.vault/`, `.secrets/`, `/Starzen RE/`, `/redteam*/`, `.tokensave/`, all pytest/codex scratch trees, models/large binaries.
**`.gitignore` coverage — MISSES:** (1) the `*.json.bak-<tag>` sensitive backups (§2b); (2) `probe.txt` scratch; (3) the 25 tracked RE scripts are not ignored **and cannot be fixed by `.gitignore`** because they are already tracked — they must be `git rm`'d (§6).

---

## 5. Load-bearing proof for the PURGE list

For all 25 RE files, verified none are referenced outside themselves:
- **Product/imports:** a repo-wide grep for each module name across `*.py/*.ps1/*.cpp/*.h/*.js/*.toml/*.cmake/*.json/*.md` returns only self-references and docstring usage strings (plus one untracked audit doc, `BUG_AUDIT_REPORT_2026-08-11.md`). No product module, no `tests/` file imports any of them.
- **Sidecar bundle manifest** (`tools/sidecar_bundle_manifest.py`): `READER_SOURCE_INPUTS` = `tools/timing/panel_grade.py`; `READER_DATA_INPUTS` = `tools/timing/panel_templates.npz`; `MODEL_INPUTS` = the three approved models. `source_files()` globs repo-root `*.py` **at build time** — so leaving these RE `.py` at root would actually pull them into the sidecar source-identity glob; another reason to purge them. None are named as required inputs.
- **CMake** (`native_orion/CMakeLists.txt`): no reference to any RE file (`infcore_shim.cpp` is not in any target).
- **Build/launch scripts** (`scripts/build_orion_sidecar.ps1`): no reference to any RE file.
- **Anti-tamper** (`SecurityManager.cpp`) only lists `frida`/`radare2`/`r2` as *strings to detect*, not imports.

Conclusion: purging all 25 is safe for build, launch, tests, and the shipped sidecar.

---

## 6. Proposed action lists for owner approval (paths only)

### PURGE from git — `git rm` (25 tracked RE/bypass/model-theft files)
```
capture_model.py
hook_helios_load.py
hook_infcore.py
hook_python_load.py
infcore_shim.cpp
monitor_helios.py
network_intercept.py
nexus_core.py
nexus_loader.py
onnx_dumper.py
patch_auth_funcs.py
patch_ch_dll.py
patch_dllmain.py
patch_init_call.py
quick_extract.py
rip_cvpython.py
rip_helios.py
rip_infcore.py
run_direct.py
runtime_auth_bypass.py
runtime_hook.py
surgical_patch.py
test_init.py
test_patched2.py
test_patched_dll.py
```
(25 files. These should be removed from the working tree AND from git history before the repo leaves the machine if it has ever been pushed; a `git rm` alone leaves them in history.)

### DELETE from disk (owner action; not git) — standing-rule violation
```
codesigning/venice_update_signing.pem      # must never be on this box
```

### DO NOT COMMIT — add to `.gitignore` (or delete) — currently a leak gap
```
settings.json.bak-*        (12 files — owner console IP + dev licence key)
learning.json.bak-*        (1 file — dev learner state)
models/tip_registration.json.bak-2k26-20260903   (dev calibration backup)  -> ignore /*.json.bak-*
probe.txt                  (46 KB QtTest scratch log)
```

### MUST ADD — `git add` (build/test/product; verified not ignored, verified load-bearing)
```
# root product modules
async_diagnostic_csv.py banner_verdict_live.py meter_locator_cv.py player_anchor.py shot_records.py stall_attributor.py
# build-gated grader inputs (required by sidecar manifest)
tools/timing/panel_grade.py tools/timing/panel_templates.npz
# native headers (#included by tracked .cpp/.h)
native_orion/src/BannerLeadTrim.h native_orion/src/FireEpochClock.h native_orion/src/GameFramePhase.h
native_orion/src/InputSessionRetryPolicy.h native_orion/src/ManualShotTally.h native_orion/src/ShotGateProtocol.h
native_orion/src/ShotVerdictTally.h native_orion/src/SquareOutputWatchdog.h
# native tests
native_orion/tests/ActivityFeedPolicyTests.cpp native_orion/tests/FireEpochClockTests.cpp
native_orion/tests/InputSessionRetryPolicyTests.cpp native_orion/tests/SharedMemoryFramePumpNotificationTests.cpp
native_orion/tests/ShotVerdictTallyTests.cpp
# QML admin panel V2 + components
native_orion/qml/admin/*.qml
native_orion/qml/components/NoMeterCard.qml native_orion/qml/components/ShotVerdictTally.qml
# python tests (add tool modules they import together — see interdependency note)
tests/backend/test_admin_v2.py tests/backend/test_discord_subscription_entitlement.py tests/backend/test_pairing.py
tests/backend/test_plan_surface.py tests/backend/test_profile.py
tests/discord/ tests/test_*.py   (~45 new files)
# scripts
scripts/run_orion_qtest.ps1
```

### ADD — recommended (owner discretion; no secrets found)
```
docs/ADMIN_PANEL_V2_CONTRACT.md docs/ANIMATION_ANCHOR_V2.md docs/DETECTOR_LOWFILL_FINETUNE_2026-09-03.md
docs/DISCORD_SERVER_SETUP_PROMPT.md docs/HANDOFF_2026-09-13_MAKE_RATE_CEILING.md
docs/HANDOFF_2026-09-14_ADMIN_PANEL_V2.md docs/HANDOFF_2026-09-14_UI_POLISH.md docs/NO_METER_V2_DESIGN.md
docs/PROMPT_external_review_timing_residual.md
discord_launch/{NEREUS_RUNBOOK.md,discord_commands.json,nereus.service,nereus_bot.py,register_commands.py,run_nereus.sh,venice_guard.py}
tools/security/pack_lethe_release.py
tools/timing/{banner_join.py,epoch_table.py,meter_trajectory.py,nometer_prior_audit.py,poll_phase_fold.py,poll_phase_fold_v2.py,poll_phase_probe.py,rescale_meter_time.py,timing_motion_audit.py}
tools/quality/{consistency_bench.py,green_window_probe.py,reencode_gate_report.py,reencode_gate_study.py,reencode_gate_sweep.py}
```

### LEAVE (do not `git add`) — scratch / owner decision
```
BOT_MAXOUT_PLAN_2026-08-11.md BUG_AUDIT_BRIEF.md BUG_AUDIT_REPORT_2026-08-11.md METER_DELAY_ASK_2026-08-11.md
METER_DELAY_PLAN_2026-08-12.md REVIEW_ASK_2026-08-11.md REVIEW_RESULT_2026-08-11.md REVIEW_RESULT_2026-08-13.md
RIG_BATCH_2026-08-11.md TIP_TIMING_CONSISTENCY_2026-08-12.md
tools/diagnostics/*  tools/training/*   website/   (owner decision)
```

---

## 7. Surprises / callouts

1. **The RE surface is twice the size named in the brief:** 25 tracked files, not 12. The extra 13 (`rip_infcore.py`, `rip_cvpython.py`, `surgical_patch.py`, `runtime_hook.py`, `run_direct.py`, `quick_extract.py`, `patch_ch_dll.py`, `patch_dllmain.py`, `patch_init_call.py`, `test_init.py`, `test_patched2.py`, `test_patched_dll.py`, `infcore_shim.cpp`) are the same auth-bypass / model-theft campaign and are all git-tracked.
2. **`codesigning/venice_update_signing.pem` is physically on the box** — the single hardest standing-rule violation. Not in git, but must be deleted from disk.
3. **`.gitignore` misses the `*.json.bak-<tag>` backups**, which hold the owner's console IP + dev licence key. Neither the ignore rules nor the package `.bak` suffix filter catch them.
4. `learning.json` is committed (dev learner state) — safe for the *customer package* (stripped by `release_filter_policy.py`), but confirm it is the intended repo baseline.
5. The `tools/security/packer/**` deletions (83 files) are the retirement of the old orion packer in favor of Lethe (`pack_lethe_release.py`) — looks intentional.
6. The three tracked ORION-key matches outside tests are all benign (a cache magic constant and a format comment); no real licence key is in tracked source.
