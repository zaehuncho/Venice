# Venice launch red team: INTERNAL wave (2026-09-22)

The owner decided to launch at the current online make rate (~70%). This wave attacks everything that touches **money, access and trust** before the external pass. The shot/input pipeline was covered on 09-21 (`docs/redteam/2026-09-21/`). Its open blockers carry into this wave and are listed below so nobody re-reports them as new.

## Who runs what

| Team | Report file | ID prefix | Emphasis |
|---|---|---|---|
| Claude (5 Opus agents, one per lane) | `L1-*.md` … `L5-*.md`, consolidated in `RED_TEAM_REPORT.claude.md` | `CL2-` | Deep read of each lane |
| Codex | `RED_TEAM_REPORT.codex.md` | `CX2-` | Exploit verification on a COPIED package, fixture-driven backend/Worker attacks, and re-verification of the 09-22 final-gate blockers |
| Gemini | `RED_TEAM_REPORT.gemini.md` | `GM2-` | Breadth: cloud/Cloudflare/AWS configuration, web and API surface, docs and runbooks, blue-team readiness (logging, recovery, rotation, rollback) |

All reports live in `C:\Users\aaron\Desktop\NexusVision\docs\redteam\2026-09-22-launch\`. The owner folds them into ONE report, ranked catastrophic to low.

## Lanes

| Lane | Surface | Start here |
|---|---|---|
| **L1 Licence and access** | Unlocking Venice without a valid entitlement. The activation broker, paired-account sign-in (zaeorion.com/connect one-time code), nonce/replay, timestamp skew, offline behaviour, device binding and HWID spoofing, revoked/expired/killed keys, lease key, dev-key containment | `native_orion/src/{LicenseClient,SecurityManager,LeaseGate,MachineIdentity,NetworkSecurity,BrokerInstallTrust,NetworkBridge}.*`, the OrionActivate broker, `backend/lambda_function.py` (activate/validate/lease/pairing handlers), `website/src/worker.js` (connect flow) |
| **L2 Money** | Stripe checkout, webhooks, refund and dispute revocation, the 7-day trial and its abuse (repeat trials, Discord alts), the $19.99 price identity, subscription lifecycle (cancel, past_due, renewal), order-claim recovery, idempotency | `website/src/worker.js`, `discord_launch/orion_worker.js`, `discord_launch/orion_bot.py` (`/claim_trial`, `/purchase`), backend provision/revoke/chargeback routes, `website/wrangler*.toml` (names only, never values) |
| **L3 Update, package, installer** | Unsigned or tampered updates, wrong or unknown `public_key_id`, downgrade, non-HTTPS artifact, zip traversal and links, DLL replacement, rollback failure, `update_pubkeys.json` tamper, unmanifested or lab files in the package, installer ACLs and install path, DLL search order and preload | `native_orion/src/{updater_main,UpdaterTrust,UpdaterArchive,UpdateManifest,ReleaseManifestTrust,UpdateGatePolicy}.*`, `tools/{package_orion_release,release_filter_policy,security_audit,verify_release_integrity,verify_native_imports}.py`, `installer/orion.iss`, `installer/build_installer.ps1` |
| **L4 Owner, staff, backend admin** | Role bypass (owner/admin/support), disabled staff still acting, Discord ID binding, destructive actions without reason or audit, key masking, kill switch (engage/disengage/caching), config writes, API Gateway routes, AWS and Cloudflare least privilege | `native_orion/src/{AdminToolController,admin_tool_main}.*`, `native_orion/qml/admin/`, `tools/admin/orion_admin.py`, `backend/lambda_function.py` admin/staff/config/audit handlers, `tools/admin/create_gateway_routes.py`, `docs/ADMIN_TOOL.md`, `docs/OWNER_TOOLS_GATEWAY_BLOCKER_2026-09-21.md` |
| **L5 Local tamper, secrets, runtime** | settings.json and its signature, `learning.json` and its backup/quarantine, the licence cache, release_manifest / security_policy removal, swapping OrionStream/OrionUpdater/libcrypto, running from a moved folder, debug env vars that unlock anything (every `ORION_*` knob; the dev hooks `ORION_DEV_FIRE_OFFSET_SWEEP` and the new `ORION_DEV_LATE_CARRY_AB` must be dead in a production build), strings/secrets in binaries, repo and logs, customer identifiers in diagnostics, the sidecar's shipped form, the Lethe protector's interaction | `native_orion/src/{AppConfig,SecurityManager,OrionAppController}.*`, `native_orion/CMakeLists.txt` (`ORION_PRODUCTION_BUILD`), `native_orion/backend/autogreen_sidecar.py`, `scripts/build_orion_sidecar.ps1`, `run_orion.local.ps1` (dev only, never shipped), `tools/security_audit.py` |

## Already known: do NOT re-report as new (cite the ID if you find more, or prove one fixed)

From `docs/redteam/2026-09-21/RED_TEAM_REPORT.codex.final.md` (Codex final gate, 09-22 03:14, verdict **blocked**):

| ID | State | Gist |
|---|---|---|
| CL-001 / CX-004 / GM-003 | blocked | Fork soft-fault reconnect has no bound, unchecked drop/ACK, no neutralisation |
| CL-003 / GM-004 | needs changes | Cross-trigger `pre` packet may copy new edges |
| CL-005 | needs changes | Two sender-thread exits are invisible |
| CL-006 | needs changes | Total telemetry silence has a stale-health gap |
| GM-002 / CX-001 / CX-008 | needs changes / blocked | learning.json "valid" backup is not semantically validated; copy/rename not atomic or checked |
| GM-006 / CX-007 / CX-020 | needs changes | Calibration Cancel does not restore zero/auto provenance |
| CX-013 | blocked | Warm "kill disabled" cache defeats a later kill during a selective outage |
| CX-014 | blocked | Audit attempt guard covers only some destructive routes |
| CX-002 | blocked | Stripe refund/dispute Charge mapping acknowledges without revoking; replay not idempotent |
| CX-015 | blocked | Unbounded log-sink wait |
| CX-016 | needs changes | Monthly entitlement modelled as fixed 30 days |
| CX-017 | blocked | Interrupted order claims can stay pending forever |
| CX-018 / GM-012 | blocked | DLL preload/fallback trust gate incomplete |
| CX-019 / GM-018 | needs changes | Customer/network identifiers in diagnostics and logs |
| CL-008 | blocked | Owner policy: No-Meter / input-timed epoch join |

## Hard rules (all teams)

- **Read-only on the repo and the fork.** Do not edit source, settings, or git state. Write only your own report and scratch files outside the repo.
- **Systems we own only.** This means the Venice launcher, updater, package, backend/API (`api.zaeorion.com`, Lambda `orion-activate`, API Gateway `v348t5hg3i`), the Cloudflare Workers and site, the staff/owner tools, and local builds.
- **Never target** PSN, NBA 2K servers, Stripe's own infrastructure, Discord's infrastructure, or any third party.
- **Live endpoints** get only low-rate, non-destructive, unauthenticated probes:
  - No brute force and no load.
  - No creating, altering or revoking real licences, users, charges or subscriptions.
  - No kill-switch toggles.
  - Prefer fixtures and local copies. Stripe only in test mode, and only if already configured.
- **Package attacks** only against a COPY of a package directory in your own scratch folder. Never against the dev tree or the installed app.
- **Never:**
  - launch `OrionNative.exe` directly; the owner runs Venice through `run_orion.local.ps1`;
  - run `register_commands.py`;
  - touch `C:\Users\Administrator\Desktop\ProjectReplay`;
  - deploy;
  - run `verify_orion.ps1 -StrictSecurity` (it builds and deploys; that is the owner's step).
- **No secrets in reports.** Name a secret's location and type, never its value. Mask licence keys and Discord IDs.
- **No exploit artifacts left in the repo.** No persistence, malware or credential theft.

## Finding format (every finding)

```
### [PREFIX-NNN] <severity> — <one-line title>
- Lane: L1..L5
- Exploit / failure path: step by step, with file:line evidence
- Affected: files / endpoint / package path
- Impact: what an attacker or a failure gains; who is hurt (owner, customer, revenue)
- Reproduction (high level): enough for the patcher to confirm it
- Required fix: concrete, minimal
- Verification test: what proves the fix
- Confidence: confirmed (reproduced or code-proven) / probable / speculative
```

**Severity:**

| Severity | Meaning |
|---|---|
| **catastrophic** | Unlock without a valid entitlement at scale, install of an unsigned or tampered update, a secret leak that grants control, or revenue bypass |
| **high** | A single-user bypass, a destructive admin action without audit, a kill switch that does not fail closed, or revocation that silently fails |
| **medium** | Hard-to-exploit or bounded-impact weaknesses, missing monitoring, or a recovery gap |
| **low** | Hardening, hygiene, docs |

End every report with a verdict line: **approved / needs changes / blocked**. Then list the top 5 fixes in priority order.

## Product pipeline lanes (reliability and performance, added at the owner's request)

These are **quality** lanes: bugs, stalls, lost or stuck inputs, wrong reads, crashes, wasted CPU/GPU, and slow startup. Same report format; use the severity scale by customer impact:
- **catastrophic:** the bot is unusable or makes a damaging wrong input;
- **high:** frequent missed or stuck shots, or a crash;
- **medium:** occasional misreads or slowdowns;
- **low:** polish.

| Lane | Surface | Start here |
|---|---|---|
| **P1 Meter detection** | Locator, lock/ghost/stale handling, the fail-closed exception paths (Codex patch 09-22), quick re-shot after a previous meter (09-22 epoch-10 miss), long range, Arrow2/White, fill accuracy | `simple_meter_reader.py` (Astra's lane: report only), `meter_locator_cv.py`, `native_orion/backend/autogreen_sidecar.py` |
| **P2 Decoder and stream path** | Chiaki fork session, decoder pipe, reconnects, Remote Play-only mode, frame export | `C:\Users\aaron\Desktop\chiaki-ng-src` (`gui/src/orioninputbridge.cpp`, `lib/src/session.c`, `lib/src/feedbacksender.c`), `native_orion/src/RemotePlaySession.*`, `remote_play_orchestrator.py` |
| **P3 Capture card** | Device selection, OBS holding the card, 60 Hz vs 120 requests, MSMF/DSHOW fallback, frame pacing, dropped/duplicate frames | `capture_card_backend.py`, orchestrator capture code, the `Capture health` logs |
| **P4 Button presses / controller input** | Press/release delivery, stuck buttons, trigger releases, Triangle+Circle combos, ownership hand-back, ViGEm vs pipe, disconnect recovery | `native_orion/src/{OrionInputClient,OrionAppController,ShotIntentPolicy,SidecarWatchdog}.*`, the fork bridge |
| **P5 Launcher performance** | Startup time, CPU/GPU load per frame, QML render cost, the detector's per-frame cost, log volume and disk I/O, memory growth over a long session, low-disk behaviour | `native_orion/src/`, `native_orion/qml/`, the sidecar, `logs/orion_native.log` (rotation), the preview/activity feed |

## More lanes (added 09-22 evening)

| Lane | Surface | Start here |
|---|---|---|
| **P6 Timing engine and learners** | Everything new since the 09-21 wave: the onset feedforward and its A/B, the late carry (`ORION_DEV_LATE_CARRY_AB`), the dev offset hook, calibration cancel/restore, learning.json recovery, the banner trim, and the learner fences for displaced shots. Silent drift of every customer's timing counts as high. | `native_orion/src/{AutomationEngine,OnsetFeedforward,BannerLeadTrim,AppConfig}.*`, `docs/variance/*.md` |
| **P7 Local IPC and services** | The input named pipe, shared-memory preview, the sidecar stdin/stdout protocol, NexusVisionSvc and VeniceNetSvc. Check the ACLs, who can connect, whether another local process can inject inputs or read state, whether the services' binary paths and permissions can be abused, and the quoting of their start commands. | `native_orion/src/{OrionInputClient,RemotePlaySession}.*`, the fork's `gui/src/orioninputbridge.cpp` (pipe creation), the service sources and installers (`scripts/build_nexus_service.ps1`, `installer/orion.iss`) |
| **P8 Real-world robustness** | Sleep/hibernate and resume mid-session, a network drop, PS5 rest mode, a system clock change or DST, two Venice instances, Venice with the official Remote Play app or OBS, one licence on two PCs, a Windows update or reboot mid-session, disk full, a long (4 h+) session | the launcher's lifecycle, timers and clocks (`nowMs`, monotonic vs wall), the licence lease and offline grace, the sidecar watchdog |
| **P9 2K patch response** | What happens when 2K changes the meter: how fast we detect it (lock-rate telemetry?), what customers see (a clear "detection unavailable" vs silent misfires), whether a detection fix can ship through the updater without a full reinstall, and whether a regression suite exists (the replay framedump harness) | the reader/detector, `tools/regression`, the updater, the status and health UI |
| **P10 Fresh install (VM)** | Run on the owner's VM with a clean Windows snapshot. **Not a code-reading lane.** Checklist below. | the installer from `installer/Output` |

**P10 checklist** (take a snapshot first; record every prompt, error and minute spent):

1. **SmartScreen and antivirus.** Does Defender or SmartScreen flag the installer or the protected exe? Record the exact wording.
2. **Prerequisites.** Visual C++ runtime, ViGEm and WinDivert driver installs, and the services. Do they install silently, need a reboot, or fail?
3. **First run.** Account pairing (zaeorion.com/connect code) and the licence unlock on a machine that has never seen Venice.
4. **Hard environments.** A username with a space and non-ASCII characters; a non-English Windows display language; 125–150% DPI scaling; two monitors.
5. **Standard user.** Run from a non-admin account: elevation prompts, and whether anything silently needs admin.
6. **Without hardware.** Without a capture card or a PS5: does it fail clearly, with a message a customer can act on?
7. **Lifecycle.** Uninstall, then reinstall. Check for leftover services, drivers, files and registry keys, whether settings survive, and whether the licence re-binds.
8. **Update.** Install the previous package, then update through the updater to the current one.
