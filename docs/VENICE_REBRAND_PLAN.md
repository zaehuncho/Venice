# Venice Rebrand Plan — killing "NexusVision"

_Status: PLAN ONLY (2026-08-08). Nothing in this document has been applied. Produced read-only on
branch `fix/timing-input-and-remoteplay-blockers`._

**Scope rule (from the standing directive):** the rebrand is `NexusVision → Venice`,
customer-facing first. It is **not** `Orion → Venice`. Every internal `orion-*` slug, resource,
key id (`orion-ed25519-v1`), exe name (`OrionNative.exe`), protocol (`orion://`), and module name
stays exactly as it is.

**Interlock:** the `NexusVisionSvc` Windows service is being replaced by `VeniceNet.dll` loaded
in-process (sibling workstream, `native_orion/venicenet/` — not yet in the tree). That changes
this plan's shape: most service-side NexusVision strings are **deleted, not renamed**. This plan
assumes one combined release: app with VeniceNet + org rename + data migration + installer that
removes the old service. If VeniceNet slips, do NOT ship a half-rebrand that renames the service —
a renamed-but-still-shipping service doubles the migration surface for zero customer value.

---

## 0. Non-negotiables

1. **The one-shot data migration MUST NOT corrupt settings/learning on failure.** Fail closed,
   keep the old directory untouched until the copy is verified, and show the user a clear error
   with the old path, the new path, and what to do. Never silently fall back to a fresh install:
   a silent reset zeroes `actuation_lead_ms` and the bot fires at the 218.5 ms factory prior —
   a known, hard-to-diagnose timing regression (see memory: force-kill settings corruption).
2. **Existing `NexusVisionSvc` MUST be uninstalled before the new install completes.** Two packet
   paths fighting over WinDivert = broken meter delay for the customer. The removal runs in the
   installer's `PrepareToInstall` (before file copy) and hard-aborts setup if the service cannot
   be stopped+deleted (see §3).
3. **`run_orion.local.ps1` keeps its exact filename.** It is gitignored via the glob `*.local.ps1`
   (`.gitignore:121`) — there is no exact-name entry. Any rename must still match `*.local.ps1`
   or the license key inside it becomes committable. Recommendation: do not rename it at all.
   (It contains one `NexusVisionSvc` comment at line 18; it is untracked — owner's local edit,
   not part of this plan.)
4. **Internal `orion-*` identifiers stay.** `OrionNative.exe`, `OrionUpdater.exe`,
   `OrionSidecar.exe`, `OrionStream.exe`, `orion://`, `orion.ico`, `orion-ed25519-v1`,
   `orion-package/`, `learning.json` schema keys, log filenames `orion_native.log` /
   `orion_user.log` — all untouched. The Inno `AppId` GUID (`orion.iss:57`) is the upgrade
   identity and is already NexusVision-free; it must never change.

---

## 1. Inventory

Grep basis: `NexusVision | nexus_vision | nexus-vision | NexusVisionSvc | NEXUS_VISION`
(plus the display string `"Nexus Vision"`). Whole tree: 61,619 occurrences in 2,039 files, but
almost all are generated build trees. Excluding `build_stub_vm/`, `native_orion/build_*/`, and
`_attic/`: **154 occurrences in 54 source files**. Build trees regenerate; `_attic/` is frozen
history and is deliberately left alone.

Categories: **[CUST]** customer-visible (must rebrand) · **[ENG]** engineer-visible (should
rebrand for consistency) · **[INT]** internal code path / dev-rig convenience (rename optional,
low priority).

### 1.1 Customer-visible [CUST] — 10 touchpoint groups

| # | File:Line | Current text | Proposed replacement |
|---|---|---|---|
| C1 | `native_orion/src/main.cpp:249` | `QApplication::setOrganizationName(QStringLiteral("NexusVision"))` | `"Venice"` — this single string is what moves the data dir (§2). `main.cpp:248` app name `"Orion Native"` stays. |
| C2 | `native_orion/src/updater_main.cpp:481` | `setOrganizationName(QStringLiteral("NexusVision"))` (OrionUpdater) | `"Venice"`. Updater currently logs to `installDir/orion_updater.log` (`updater_main.cpp:180`), so no data-dir migration needed for it — but flip it in the same commit so no future QStandardPaths use resurrects the old org tree. |
| C3 | `installer/orion.iss:305,310,315,316,321,333,340` + `:153-154` (`[UninstallRun]`) | `sc create/stop/delete/description/sdset NexusVisionSvc`, `packet_bridge\NexusVisionSvc.exe` | **Delete** `RegisterPacketBridgeService` wholesale (service is replaced by VeniceNet.dll); replace with (a) legacy-service *removal* in `PrepareToInstall` (§3) and (b) WinDivert driver registration + `sdset` (§4). `[UninstallRun]` lines become the WinDivert cleanup instead. |
| C4 | `installer/orion.iss:322` | `DisplayName= "Nexus Vision Packet Bridge"` (visible in services.msc) | Gone with C3 (no replacement service). |
| C5 | `nexus_svc.py:92-97` | `SVC_NAME = "NexusVisionSvc"`, `SVC_DISPLAY_NAME = "Nexus Vision Packet Bridge"`, description "…for Nexus Vision…" | File **retired** when VeniceNet lands (it is the service source). Until then: no rename (non-negotiable interlock above). Post-VeniceNet: move to `_attic/` or delete; keep `git log` as the history. |
| C6 | `nexus_svc.py:120-148`, `native_orion/src/NetworkBridge.cpp:51`, `NetworkBridge.h:31-32` | Bearer-token paths `%PROGRAMDATA%\NexusVision\nexus_bridge.token`, `%LOCALAPPDATA%\NexusVision\nexus_bridge.token` | Token machinery **disappears** with the in-process DLL (no cross-process auth needed). Installer deletes leftover `%PROGRAMDATA%\NexusVision\` at upgrade (§3 step 7). `%LOCALAPPDATA%\NexusVision\nexus_bridge.token` sits *beside* (not inside) the `Orion Native` data dir — the §2 migration must NOT sweep it; the installer/first-run cleanup removes it. |
| C7 | `tools/package_orion_release.py:118` (`NEXUS_SERVICE_EXE = "NexusVisionSvc.exe"`), `:110-116`, `:445`, and `tools/release_filter_policy.py:137` (`"packet_bridge/NexusVisionSvc.exe"`) | Shipped exe name in `C:\Program Files\Venice\packet_bridge\` (file explorer) + the packaging ban list | Packaging stops shipping the service exe entirely; `copy_compiled_service()` is replaced by the VeniceNet.dll packaging step. `release_filter_policy.py:137` must be updated **in lockstep** or packaging fails its own policy check. |
| C8 | `tools/package_orion_release.py:420` | `_THIRD_PARTY_NOTICE` header "Third-party components bundled with the **Nexus Vision** packet bridge" — ships as a text file in the package | "Third-party components bundled with Venice" (WinDivert LGPL notice must survive — the DLL/driver still ships for VeniceNet). |
| C9 | `native_orion/src/OrionAppController.cpp:3960, 8474, 12354, 12367, 12378, 12395, 12442` | User-facing log lines & `sc` calls: "no NexusVisionSvc service…", "starting NexusVisionSvc", `sc start NexusVisionSvc`, `sc query NexusVisionSvc` — visible in the in-app Log Viewer | These live in `ensurePacketBridgeRunning()` / `packetBridgeBackendPresent()` which the VeniceNet integration rewrites anyway. Replacement strings say "VeniceNet". **Coordinate with the sibling agent** — same functions. |
| C10 | `nexus_svc.py:348` | `nexus_svc.log` written beside the exe → `C:\Program Files\Venice\packet_bridge\nexus_svc.log` on installs | Gone with the service. `[UninstallDelete] Type: filesandordirs; Name: "{app}"` (`orion.iss:166`) already removes strays on uninstall; upgrade removes `{app}\packet_bridge\` (§3 step 7). |

Also customer-*adjacent*: the data directory itself, `%LOCALAPPDATA%\NexusVision\Orion Native\`
(visible in Explorer) — the whole of §2.

### 1.2 Engineer-visible [ENG] — rename for consistency (mechanical pass)

Comments/docs only; zero runtime behavior. Do these in one sweep commit after the functional work,
so the diff is reviewable as "strings only".

| File:Line(s) | What | Proposed |
|---|---|---|
| `native_orion/src/OrionAppController.h:613, 2332` | Comments describing the compiled bridge | Rewrite for VeniceNet |
| `native_orion/src/OrionAppController.cpp:12324-12326, 12460` | Comments | Rewrite for VeniceNet |
| `native_orion/src/main.cpp:295` | Comment "~/Desktop/NexusVision/settings.json would repoint…" | Keep meaning; wording can say "the dev checkout" (the *path constant* is [INT], see 1.3) |
| `native_orion/qml/components/MeterConfigPanel.qml:283` | Comment `packet_bridge\NexusVisionSvc.exe -> NexusVisionSvc` | Rewrite for VeniceNet (banner logic itself is being reworked by the VeniceNet change) |
| `native_orion/tests/MeterDelaySettingsPropertyTests.cpp:158-160, 229` | Comments + assertion message naming NexusVisionSvc | Rewrite alongside the VeniceNet availability rework |
| `native_orion/tests/AutomationEngineTests.cpp:8346` | **Asserts the data path ends with `"/NexusVision/Orion Native"`** | MUST flip to `"/Venice/Orion Native"` in the same commit as C1 or the suite goes red. File is in the sibling agent's territory — **explicit coordination item**. Also `:5825, 26919, 26947-27053` (token-path comments/tests → deleted with C6). |
| `tests/test_nexus_svc_delay.py:1154-1183`, `tests/test_nexus_svc_auth.py:176-177`, `tests/test_release_packaging.py:250, 277` | Bridge/packaging tests | Retired/rewritten with the service (C5/C7). `test_release_packaging.py` gains the inverse assertion: the package must NOT contain `packet_bridge/NexusVisionSvc.exe`. |
| `scripts/build_nexus_service.ps1` (filename + `:10,15,61,101,104,107`) | Nuitka build of the service exe | Retired with the service. Do not rename mid-flight. |
| `install_nexus_service.bat` (`:3,5,34,47`) | Dev-rig service installer | Retired with the service. |
| `installer/README.md:14,18` | Packaging docs for the bridge bundle | Rewrite for VeniceNet + WinDivert registration |
| `docs/METER_DELAY_END_TO_END.md` (6×) | The bridge chain doc | Rewrite for VeniceNet (this doc is the one most worth updating properly, not sed'ing) |
| `docs/CODE_SIGNING.md:5` | "All NexusVision release binaries…" | "All Venice release binaries…" |
| Historical dossiers/prompts: `docs/ORION_*` (A2_B1, CHIAKI_INJECT, MASTER_*, PERF, REVIEW_REQUEST, DETECTION/RTT dossiers), `docs/TIER2_PROMPT_*`, `docs/MODELS_BUILD_PROMPT_*`, `docs/COMPRESSED_READER_PICKUP_PROMPT.md`, `docs/WORKFLOW_AUDIT_FINDINGS_20260804.md` | Mostly repo-path references (`C:\Users\…\Desktop\NexusVision`) in frozen evidence docs | **Leave as-is** (they are dated records; rewriting them falsifies history). Optionally add a one-line banner "product now branded Venice" — not required. |
| Root docs/tooling: `AGENTS.md:1,3,6`, `PROJECT_STRUCTURE.md:1,6`, `TEST_COMMANDS.md:3`, `PICKUP_PROMPT_TIP_TIMING_METER_DELAY.md:10,16`, `VENICE_EXTERNAL_AI_PROMPT.md:5`, `agent-loop.ps1:4,214,240`, `scripts/run_agent_loop.ps1:7`, `.agents/AGENTS.md`, `.claude/agents/orion-primary-implementer.md` | "NexusVision/Orion repo" phrasing + repo path | Update phrasing to "Venice (repo dir NexusVision)" — the *path* stays truthful as long as the checkout dir keeps its name (see 1.3/§7). |
| `scripts/verify_orion.ps1` | **Clean** — no NexusVision/service references found | No change |
| `assets/` | Only `orion.ico` / `orion.png` — nothing NexusVision-named | No change |
| `installer/OrionSetup/` (Qt installer UI) | No NexusVision references found in `src/` or `qml/` | No change |

### 1.3 Internal code paths [INT] — dev-rig fallbacks, low priority

These hardcode the *checkout directory* `~/Desktop/NexusVision`, which is the folder name of this
repo, not customer-facing brand. They are compiled out or inert in production
(`ORION_PRODUCTION_BUILD` gates the root fallback; `rootDir_` is always set on installs).

| File:Line | What |
|---|---|
| `native_orion/src/main.cpp:289` | `desktopNexus` dev-root fallback (prod: compiled out, `main.cpp:297`) |
| `native_orion/src/admin_tool_main.cpp:35` | Same pattern (owner/staff tool, never shipped) |
| `native_orion/src/RemotePlaySession.cpp:1639, 1692, 1698, 1754, 1769, 2122` | Fallbacks when `rootDir_` is empty: helper root, `.venv311` python, sidecar paths |
| `chiaki_backend.py:1916`, `meter_detector.py:398, 2351`, `orion_config_io.py:16, 21`, `rtt_sync_engine.py:299`, `native_orion/backend/autogreen_sidecar.py:13, 43, 1088, 1491`, `native_orion/backend/ps5_remoteplay_helper.py:306` | Python-side equivalents / `--root` help text |
| `settings.json` + `settings.json.bak` / `.pre_*` backups | Local dev state; `chiaki_path` embeds the checkout path — never committed intent, do not sed |
| `chiaki_backend.py:215` | Error message "the NexusVision/vendor/chiaki/ directory" |

**Recommendation: do NOT rename the checkout directory** as part of this rebrand. Renaming
`Desktop\NexusVision` → `Desktop\Venice` would break: the dev-root fallback constants above, local
`settings.json` `chiaki_path`, the `.claude` project memory keyed on the path, the deployed-fork
sibling relationship (`..\chiaki-ng-src`), and every dated evidence doc. It buys nothing a
customer can see. If the owner wants it eventually, it is its own change with its own checklist.

### 1.4 Explicitly out of scope for edits

- `_attic/**` (72 hits in `ORION_CLEANUP_MANIFEST.json` alone) — frozen history.
- `build_stub_vm/`, `native_orion/build_phaseveto/`, `native_orion/build_meterdelay/` — generated;
  they clean themselves on the next configure. Do not sed generated trees.
- `native_orion/venicenet/`, `native_orion/src/AutomationEngine.{h,cpp}` + its tests — sibling
  agent's territory (coordination items are flagged in 1.2).

---

## 2. Data migration design

### 2.1 Target path: `%LOCALAPPDATA%\Venice\Orion Native\`

The path is produced by Qt, not hardcoded: `QStandardPaths::AppLocalDataLocation` =
`%LOCALAPPDATA%/<OrganizationName>/<ApplicationName>` (`native_orion/src/OrionPaths.h:35-56`,
prod-only; dev builds resolve to the checkout). So the *only* code change that moves the
directory is `main.cpp:249` org name `NexusVision → Venice`.

**Chosen: `Venice\Orion Native`** (change org only, keep app name), rejected alternatives:

- `Venice\Venice` — requires also changing `setApplicationName` (`main.cpp:248`). That renames an
  *internal* identity ("Orion Native") the standing directive says to keep; it breaks the
  production-log tooling conventions (memory: prod logs live under `…\Orion Native\`, in-app ring
  is 160 lines — every runbook greps that path); and it doubles the strings that can drift.
  "Orion" is sanctioned; only "NexusVision" is the dead brand.
- Fully custom path (not via QStandardPaths) — throws away the tested `orionDataDir()` machinery
  and its fallbacks for no benefit.

Same-volume guarantee: old and new live under the same `%LOCALAPPDATA%`, so directory renames are
atomic (`MoveFileEx` same-volume) — the migration exploits this.

### 2.2 What actually lives in the old dir (complete inventory, from code)

| Item | Producer | Migrate? |
|---|---|---|
| `settings.json` + `settings.json.sig` | `AppConfig.cpp:142`, `SecurityManager.cpp:978/983` | YES (the crown jewels) |
| `learning.json` + per-profile `learning.<slug>.json` | `AppConfig.cpp:161-175` | YES (glob `learning*.json`, not just the default) |
| `.vault/orion_entitlement.cache` | `SecurityManager.cpp:988` (DPAPI-wrapped offline entitlement) | YES — losing it silently forces online re-verification |
| `logs/orion_native.log`, `logs/orion_user.log` | `OrionAppController.cpp:1671, 11901` | YES (support/diagnosis value) |
| `status.json` | `OrionAppController.cpp:12640` | YES (cheap) |
| `update_attempt.txt` | `OrionAppController.cpp:4710-4735` | YES (cheap) |
| `calibration/` | `OrionAppController.cpp:7049-7050` | YES |
| `cache/qmlcache/` | Qt QML disk cache (`CacheLocation` under the same tree) | **NO — exclude.** Machine+build specific, regenerated on first launch; copying stale caches is pure risk. |
| NOT in this dir: `%LOCALAPPDATA%\NexusVision\nexus_bridge.token` | sits in the *parent* `NexusVision\` dir (`nexus_svc.py:144-148`) | NO — dies with the service; installer cleanup (§3 step 7) |

### 2.3 Signatures survive the move — verified, no re-sign

`SecurityManager::settingsDigest()` (`SecurityManager.cpp:939-951`) hashes
`"orion-settings-v1|" + machineId() + "|" + <settings.json bytes>`; `machineId()`
(`SecurityManager.cpp:367-376`) is hostname + `machineUniqueId` + registry MachineGuid.
**No path is part of the digest.** A byte-exact copy of `settings.json` + `settings.json.sig`
on the same machine verifies identically at the new path. Therefore:

- Do **NOT** re-sign after the move. Re-signing would mask real tampering that happened before
  migration and would turn the migration into a signing oracle.
- The copy must be byte-exact (`QFile::copy` is; no re-serialization, no JSON round-trip).
- `.vault/orion_entitlement.cache` is DPAPI per-user, also path-independent — plain copy.
- Corollary: if the signature was *already* invalid pre-move, it is invalid post-move and the
  existing security-lock UX fires exactly as before. Migration neither hides nor creates locks.

### 2.4 Algorithm (one-shot, on app startup, production builds only)

Runs in `main.cpp` immediately after `setOrganizationName`/`setApplicationName` and the
single-instance mutex (`main.cpp:267`), **before** the QML engine, before `OrionAppController`
construction (which opens the log sink in the new dir), and before `SecurityManager::evaluate()`
(whose first-run bootstrap would otherwise sign fresh defaults — see failure surface). If
`alreadyRunning` is true, skip migration entirely (the primary instance owns it).

```
OLD  = %LOCALAPPDATA%/NexusVision/Orion Native        (built explicitly from
       QStandardPaths::GenericDataLocation — never by temporarily flipping the org name)
NEW  = orionDataDir(rootDir)                          (= %LOCALAPPDATA%/Venice/Orion Native)
MARK = NEW/.migration_in_progress                     (JSON: {src, started_utc, app_version})

primary_state(dir) := exists(dir/settings.json) OR glob(dir/learning*.json) non-empty

1. If MARK exists:                      // previous attempt died mid-copy
     wipe NEW contents EXCEPT MARK      // NEW is known-partial; OLD was never touched
     continue at step 3                 // idempotent retry
2. If primary_state(NEW):               skip (migration already done — the normal case forever after)
   If NOT exists(OLD) or NOT primary_state(OLD):  skip (fresh machine — nothing to migrate)
3. mkpath(NEW); write MARK (QSaveFile, flushed)
4. Copy OLD → NEW recursively, EXCLUDING cache/ :
     order: everything else → logs/ → .vault/ → learning*.json → settings.json.sig → settings.json LAST
     (settings.json last means primary_state(NEW) only becomes true when everything else already landed,
      so the step-2 guard can never see a "complete-looking" half-copy)
5. Verify: byte-size + SHA-256 compare for settings.json, settings.json.sig, every learning*.json,
     .vault/orion_entitlement.cache (if present). Any mismatch → FAIL path.
6. Delete MARK (this is the commit point).
7. Rename OLD → %LOCALAPPDATA%/NexusVision/Orion Native.migrated-to-venice   // atomic, same volume
     and write OLD-parent breadcrumb NexusVision/MOVED_TO_VENICE.txt ("Your data moved to …").
     Rename failure here is NON-FATAL (log + continue): NEW is complete and step-2 makes reruns no-ops.
8. Log the whole event (see 2.6).
```

**Copy-then-rename, never move-file-by-file:** a per-file move that dies midway leaves *both*
directories half-populated with no way to tell which half is authoritative. With copy-first, OLD
remains 100% intact until step 6; every crash before step 6 is retried from a clean slate by
step 1; every crash after step 6 is already committed.

**Why rename OLD at all (step 7):** (a) it makes the end state unambiguous in Explorer — the
customer sees one `Venice` tree and one clearly-marked backup; (b) it stops any stale component
(old exe run from an old shortcut, orphaned sidecar) from silently continuing to write the old
tree; (c) it is the instant rollback: rename back, done. The backup is small (logs dominate) and
is deliberately **never auto-deleted** by the app. The uninstaller MAY offer to remove it.

**Idempotency proof-sketch:** the only state transitions are (no NEW) → (NEW partial + MARK) →
(NEW complete, no MARK) → (OLD renamed). Every crash point lands in exactly one of these, each of
which step 1/2 classifies deterministically. The "both dirs have primary state, no MARK" case
(e.g. a downgrade ran the old exe after migration) resolves as **NEW wins, log a warning, never
overwrite** — the guard in step 2 already does this.

### 2.5 Failure surface — fail closed, loudly

On any failure in steps 3-5 (mkpath denied, disk full, copy error, hash mismatch):

- Delete nothing. OLD is untouched by construction; leave NEW + MARK for the retry.
- Show a **blocking native dialog before the QML engine loads** (QMessageBox is available —
  QApplication already exists):
  > *"Venice couldn't move your settings and learned timing from the old data folder.*
  > *Nothing has been lost — your data is still at `<OLD>`.*
  > *Error: `<detail>`. Free up disk space or fix folder permissions at `<NEW>`, then start
  > Venice again. If this keeps happening, send `%TEMP%\venice_migration_failure.log` to support."*
  Buttons: **Retry** (loop the algorithm) / **Quit**.
- On Quit, **exit the process** — do not continue into a defaults run. Continuing would (a) let
  `AppConfig::save()` + the first-run bootstrap (`SecurityManager.cpp:396-420`) write and *sign*
  a fresh default `settings.json` in NEW, permanently making NEW look "already migrated" to
  step 2, and (b) run the bot on factory-prior timing. Both are exactly the silent-reset failure
  the non-negotiable forbids.

### 2.6 Logging when the log location is itself moving

The app log sink opens at `NEW/logs/orion_native.log` (`OrionAppController.cpp:1671`) — which
does not exist until the migration has run. Solution, three sinks in order of durability:

1. **In-memory journal** during the migration (vector of timestamped lines).
2. On success: flush the journal into the migrated `NEW/logs/orion_native.log` via the normal
   `appendLog` path once the controller is up (so the event appears in the in-app Log Viewer and
   the 160-line ring), AND write `NEW/migration_report.txt` with the full journal + per-file hash
   results (evidence that survives log rotation).
3. On failure: write the journal to `%TEMP%\venice_migration_failure.log` (guaranteed-writable),
   which the error dialog names explicitly.

### 2.7 Trickiest edge cases (call-outs for review)

- **Interrupted first attempt** — covered by MARK + wipe-and-retry (step 1). The subtle part is
  that `settings.json` is copied last so a torn run can never satisfy `primary_state(NEW)`.
- **Old version runs after migration** (stale shortcut, downgrade): OLD was renamed, so the old
  exe recreates defaults in a fresh OLD — visibly unconfigured rather than silently divergent;
  NEW-wins guard protects the migrated data either way.
- **Second instance during migration**: gated by the existing `Local\OrionNativeLauncher` mutex;
  non-primary instances skip.
- **`ORION_PRODUCTION_BUILD` gating**: `orionDataDir()` returns the checkout in dev, so migration
  must be a no-op in dev builds (OLD==NEW-style guard) — but implement it path-injected
  (`migrateVeniceDataDir(oldDir, newDir, journal)`) so unit tests can drive it with temp dirs.

---

## 3. Service uninstallation for existing users (installer)

Current registration reference: `installer/orion.iss:281-344` — `RunSc()` (`:282-286`) wraps
`Exec(sc.exe …)` and surfaces sc's own exit code; `RegisterPacketBridgeService` (`:300-344`)
does stop → delete → `Sleep(600)` → create → description → sdset, from `CurStepChanged(ssPostInstall)`
(`:346-356`). The uninstaller has stop/delete at `:153-154`.

### 3.1 Where it runs: `PrepareToInstall`, not `ssPostInstall`

Two reasons the removal must happen **before file copy**:

1. The old `NexusVisionSvc.exe` lives under `{app}\packet_bridge\` — if the service is running,
   the file is locked and Inno's copy/overwrite of `{app}` fails mid-install.
2. `PrepareToInstall` can **abort setup cleanly** by returning a non-empty string; `ssPostInstall`
   cannot. Non-negotiable #2 requires abort-on-failure, so this is the only correct hook.

### 3.2 Sequence (Pascal-level design)

```
function RemoveLegacyNexusService(out Err: string): Boolean;

1. DETECT     RunSc('query NexusVisionSvc') → exit code 1060 (does not exist) → Result:=True, done.
2. STOP       RunSc('stop NexusVisionSvc') → accept 0, 1062 (not started), 1060.
              Anything else → fall through to the poll; sc stop on a STOP_PENDING service
              returns 1051/1061 variants — all handled by polling, not by trusting the code.
3. POLL       Loop up to 30 s (500 ms steps): RunScCapture('query NexusVisionSvc') → parse STATE.
              STOPPED → proceed. STOP_PENDING → keep polling (nexus_svc closes its WinDivert
              handle in its SERVICE_STOP handler; WinDivert unloads on last-handle close, so a
              held handle resolves itself once the process dies).
4. KILL       Still not STOPPED after 30 s (hung STOP_PENDING / wedged WinDivert wait):
              'sc queryex NexusVisionSvc' → PID → Exec('taskkill /f /pid <pid>'), poll 10 s more.
              Killing the process force-closes the WinDivert handle; the on-demand driver
              refcounts down on its own. Still alive → Err := 'service could not be stopped';
              Result := False.
5. DELETE     RunSc('delete NexusVisionSvc'):
                0 or 1060                    → OK.
                1072 (marked for delete)     → OK-with-warning: something (services.msc, another
                  SCM handle) pins the record; it purges on close/reboot. SAFE here because the
                  new install registers NO service under this name (VeniceNet is a DLL) — there
                  is no name-reuse race. A marked-for-delete service also cannot be started, so
                  it cannot intercept packets. Log it; continue.
                5 (access denied) or other   → Err := 'sc delete failed (code N)'; Result := False.
                  (Installer runs elevated — PrivilegesRequired=admin — so code 5 means something
                  is genuinely wrong, e.g. a tampered service SD. Do not paper over it.)
6. ON FALSE   PrepareToInstall returns:
              'Venice could not remove the old NexusVision background service (<Err>).
               Please reboot Windows and run this installer again. Nothing has been changed yet.'
              → Setup aborts BEFORE any file was copied. Old install keeps working. This is the
              fail-loud path demanded by non-negotiable #2: an install that completed with the
              old service still alive would give the customer two packet paths and a broken
              meter delay.
7. CLEANUP    (best-effort, non-fatal, after DELETE succeeds):
              - DelTree('{app}\packet_bridge') stale dir (new layout re-creates what it needs)
              - DelTree('%PROGRAMDATA%\NexusVision') — the bridge token remnant (C6)
              - The old {app}\packet_bridge\nexus_svc.log goes with its dir
```

`RunScCapture` = `RunSc` + output capture (Inno: redirect via `cmd /c sc query … > tmpfile` and
read the file, since `Exec` has no stdout capture). Parse `STATE` numerically (`4 RUNNING`,
`3 STOP_PENDING`, `1 STOPPED`) — never the localized text.

### 3.3 Uninstaller changes (`orion.iss:146-154`)

Replace the two `NexusVisionSvc` `[UninstallRun]` entries with the equivalent cleanup for whatever
§4 registers (the WinDivert driver service, only if *we* created it — see §4.3), keeping the
`RunOnceId` discipline. Keep the legacy stop/delete lines for **one release** as belt-and-braces
for users who upgrade and later uninstall (they no-op at 1060, which is already non-fatal).
Also fix the stale comment at `orion.iss:163` ("`%LOCALAPPDATA%\Orion`" → the real
`%LOCALAPPDATA%\Venice\Orion Native` path) while in the file.

---

## 4. WinDivert DACL setup for non-admin runtime open

New world: no LocalSystem bridge; the unelevated app (VeniceNet.dll in-process) must open a
WinDivert handle itself. WinDivert's documented default is that `WinDivertOpen` requires
Administrator; the install-time job is to pre-register the driver service (elevated, once) and
loosen who may use it.

### 4.1 Install-time sequence (elevated, in `ssPostInstall` after files are copied)

Order matters — each step needs the previous one's object to exist:

```
1. Files first: {app}\packet_bridge\WinDivert64.sys + WinDivert.dll land with [Files]
   (they already ship — tools/package_orion_release.py WINDIVERT_FILES).
2. sc create WinDivert type= kernel start= demand binPath= "{app}\packet_bridge\WinDivert64.sys"
   - Name MUST be exactly "WinDivert": the DLL locates/starts the driver by that service name.
   - If it already exists (exit 1073): another product (or our own previous run) registered it.
     DO NOT delete-and-recreate blindly — compare binPath via `sc qc WinDivert`; if it points at
     a foreign path, reuse it as-is and only apply step 3 (log the foreign path). Record in the
     registry (HKLM\Software\Venice\WinDivertServiceOwned = 0/1) whether WE created it, so the
     uninstaller only deletes what we own.
3. Splice, don't overwrite: capture `sc sdshow WinDivert` (current SDDL), and only if it lacks
   an ;;;AU) ACE, insert ours before the S: (SACL) portion if present:
       (A;;CCLCSWRPLOCRRC;;;AU)          ← service-object rights: query config/status,
                                            interrogate, START (RP), read SD. No stop (WP), no
                                            change-config (DC) — non-admins must not be able to
                                            stop the driver out from under another session or
                                            repoint its binary.
   then `sc sdset WinDivert <spliced SDDL>`.
   A full replacement default, if sdshow is unusable, mirrors the stock service SD plus AU:
       D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)
         (A;;CCLCSWLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)(A;;CCLCSWRPLOCRRC;;;AU)
4. VERIFY, fail loud (the malformed-SDDL requirement):
   - sc sdset exits non-zero on malformed SDDL (ERROR_INVALID_PARAMETER 87) or wrong service
     (1060). RunSc already surfaces the code: any non-zero → MsgBox naming the exact sc command
     and code, and the install is marked failed for the network feature — NOT a silent skip.
     (Contrast with today's sdset failure path, orion.iss:340-343, which is informational-only;
     that was acceptable when an elevated service existed as the actual opener. Here the DACL is
     load-bearing: without it the flagship meter delay silently never works for a non-admin.)
   - Then `sc sdshow WinDivert` and assert the output contains ";;;AU)" — catches the
     "sdset succeeded but wrote something else" class.
5. RUNTIME PROOF (post-install smoke): run the app's own probe unelevated —
     OrionNative.exe --verify-windivert-open   (VeniceNet self-test: open SNIFF-only handle on a
     never-matching filter, close, exit 0/nonzero)
   executed via `runasoriginaluser` from the Finished page (Inno runs elevated; the probe must
   run as the real user). Non-zero → MsgBox: "Venice installed, but the network feature failed
   its permission check — the meter delay will not work until this is fixed. <code>".
```

### 4.2 Why the runtime proof is mandatory, not optional

`sc sdset` edits the **service object's** security descriptor (SCM operations: start/query). The
WinDivert **device object's** DACL is established by the driver itself at create time; the two
are related but not the same gate, and WinDivert's own docs treat non-admin use as unsupported
territory. This has the exact shape of the "gates unreachable on real data" trap: everything
green at install time, feature dead at runtime. So the DACL design above ships **only** with the
step-5 proof wired in, and the plan carries a fallback:

- **Fallback A (preferred):** a minimal elevated open-broker — a one-shot elevated helper
  (UAC prompt on first enable, like the old service install was) opens the WinDivert handle and
  duplicates it into the app process (`DuplicateHandle`), then exits. Handle inheritance across
  privilege levels is the vendor-sanctioned pattern.
- **Fallback B:** keep a (renamed, Venice-branded) tiny service after all — explicitly the
  outcome we are trying to avoid; listed only so the decision is recorded if A fails too.

The go/no-go between "sdset works on the rig" and Fallback A is a **live-rig gate before the
rebrand release is cut** (memory: verify claimed fixes independently — an offline pass here
proves nothing).

### 4.3 Uninstall

If `HKLM\Software\Venice\WinDivertServiceOwned = 1`: `sc stop WinDivert` (accept 1062) then
`sc delete WinDivert` in `[UninstallRun]`, before `{app}` removal deletes the `.sys` out from
under a registered service. If owned=0 (foreign registration), leave the service alone.

---

## 5. Test matrix — all four required for signoff

Common post-conditions for every row (call this **P-BASE**):
`sc query NexusVisionSvc` → 1060 (absent) or, acceptable-with-log, stopped+marked-for-delete;
app launches unelevated; Meter Delay card reports backend available and armed truthfully;
`%LOCALAPPDATA%\Venice\Orion Native\settings.json` + `.sig` verify (no security lock);
no process ever writes `%LOCALAPPDATA%\NexusVision\**` again.

| # | Path | Setup | Must be true post-install | Acceptable | Broken (= migration bug) |
|---|---|---|---|---|---|
| T1 | **Fresh machine** (never had NexusVision) | Clean VM/profile: no old dir, no service, no WinDivert svc | P-BASE; NO migration ran (journal absent); first-run bootstrap signs fresh defaults exactly once (`settings_signature_first_run_bootstrap` in log); WinDivert probe passes | `Orion Native.migrated-to-venice` absent (nothing to migrate) | Migration dialog appearing at all; app writing to a `NexusVision\` tree; probe failing silently |
| T2 | **Upgrade over a working NexusVisionSvc install** | Old installer's layout: service registered demand-start, old data dir populated with real settings/learning/logs, service currently STOPPED | P-BASE; old service gone; `Venice\Orion Native\` holds byte-identical `settings.json` (+ valid sig, NOT re-signed — file hash unchanged), all `learning*.json`, logs, `.vault\`; old dir renamed `*.migrated-to-venice`; `MOVED_TO_VENICE.txt` breadcrumb; `migration_report.txt` written; `actuation_lead_ms` value survives EXACTLY (this is the single most important byte in the file) | `%PROGRAMDATA%\NexusVision` cleanup best-effort-failed with a log line; OLD rename failed but NEW complete (logged) | Fresh-default settings in NEW while OLD had real data; sig invalid post-move when it was valid pre-move; learning profile files dropped; both dirs "live" |
| T3 | **Upgrade while NexusVisionSvc is RUNNING with an open WinDivert handle** | Start the service, drive it so the intercept is armed and a WinDivert handle is held, then run the installer | Installer's stop→poll→(kill if needed)→delete succeeds; file copy does NOT fail on a locked `NexusVisionSvc.exe`; then everything in T2 | Stop takes up to the 30 s poll; taskkill path used (logged); reboot-required abort message if truly wedged — with the OLD install still functional (PrepareToInstall aborted before file copy) | Install "succeeds" with the service still running/registered; copy error mid-install leaving a half-upgraded `{app}`; WinDivert driver left permanently loaded with no owner |
| T4 | **Upgrade over a broken registration** (service exists but disabled, or its binPath exe was deleted / start fails 1053) | `sc config NexusVisionSvc start= disabled` and/or delete the old exe by hand | Removal treats it identically: stop returns 1062, delete succeeds, install proceeds; then T2's data assertions (data dir state is independent of service health) | 1072 marked-for-delete warning path | Installer aborting on 1062/1060-class codes (over-strict), or looping forever polling a service that can never reach STOPPED because it never started |

Additional signoff sweeps (not full rows): T2 repeated with a **per-profile** `learning.<slug>.json`
present; T2 with the migration **interrupted by kill** at three points (mid-copy / after copy
before MARK delete / after MARK delete before OLD rename) then relaunched — final state must
equal uninterrupted T2; T1/T2 uninstall afterwards — `Venice\Orion Native` survives uninstall
(matching today's keep-settings semantics, `orion.iss:156-166`).

---

## 6. Execution order (suggested commits)

1. **Installer legacy-service removal + WinDivert registration/sdset + probe** (§3, §4) — can
   land behind the installer build, independent of the app.
2. **App: org rename + data migration** (§2) in one commit: `main.cpp:249`, `updater_main.cpp:481`,
   the migration unit (new small file beside `OrionPaths.h`), and the
   `AutomationEngineTests.cpp:8346` expectation flip (**coordinate — sibling agent's file**).
3. **VeniceNet integration replaces the bridge client path** (sibling agent):
   `OrionAppController` C9 strings, `MeterConfigPanel` banner, `NetworkBridge` token code
   deletion (C6), retirement of `nexus_svc.py` / `build_nexus_service.ps1` /
   `install_nexus_service.bat` / bridge tests (C5), packaging swap (C7, C8) + policy list
   (`release_filter_policy.py:137`) + `test_release_packaging.py` inversion.
4. **Docs/comments sweep** (§1.2 ENG rows) — strings only, one reviewable diff.
5. **Live-rig gate** (§4.2) + full §5 matrix → release.

## 7. Findings that change expectations (summary)

- **No cloud contamination.** `backend/lambda_function.py`, `discord_launch/` (bot, worker,
  gumroad webhook, wrangler.toml) contain zero NexusVision identifiers — no customer cloud
  state, bundle ID, or signed manifest carries the dead brand persistently. The release
  manifest lists `packet_bridge/NexusVisionSvc.exe` per-release but is regenerated and re-signed
  every release, so it self-heals when packaging changes.
- **Upgrade identity is safe.** Inno `AppId` (`orion.iss:57`) never contained NexusVision.
- **The rebrand is *smaller* than the grep suggests** (61k hits → 154 real ones), and the
  VeniceNet transition converts most CUST items from renames into deletions.
- **The two genuinely risky pieces** are the data migration (§2 — one shot, on the owner's and
  every customer's live timing state) and the non-admin WinDivert open (§4 — vendor-unsupported
  territory; must be proven on the rig, with Fallback A ready).
- **One coordination hazard:** `AutomationEngineTests.cpp:8346` hard-asserts the old
  `/NexusVision/Orion Native` path and sits in the sibling agent's files; the org-rename commit
  goes red without a synchronized edit there.
- **Do not rename the checkout dir** `Desktop\NexusVision` in this pass (§1.3) — high blast
  radius, zero customer visibility.
