# External red team: white-box triage (Claude)

Candidate: rc5. Installer `5f1e946d…`, package `6e3c10e8…`, source `c17d764`.

Each external finding is checked against the source here. The reports themselves stay in `C:\RedTeam\<tester>\`.

## Codex (on-disk phase, 2026-09-24 09:07–09:11Z)

**Lane verdict: blocked.** The only reason is that the VM-phase controls have not run. No blocker-class failure was observed.

| ID | Codex | Triage | Action |
|---|---|---|---|
| (fixture) `allow_local_dev_bypass` false→true in a copied `security_policy.json` | detected by the signed manifest (hash mismatch) | **Not exploitable, for two independent reasons.** (1) The signed release manifest covers the file, and the edit is caught (Codex's own control). (2) The native runtime never reads that field. In a production build `automationSecurityAllowed()` hard-codes `localDevBypass = false` under `#ifdef ORION_PRODUCTION_BUILD` (`native_orion/src/OrionAppController.cpp:11587`). The field is consumed only by the packaging and audit tools, which refuse `true` in a customer package (`tools/security_audit.py:602`, `tests/test_packer_hardening.py:661`). | none. The edited file stays in Codex's fixture folder only. |
| L-01 PDB paths and exported C++ symbols | LOW, observed | **Confirmed.** It eases reverse engineering but is not a bypass. The strip/obfuscation work is scheduled with packing in the first update. | post-launch: `/PDBALTPATH` or stripped debug directory; minimise exports |
| M-01 Installer and PEs not Authenticode-signed | MEDIUM, observed | **Confirmed; accepted by the owner for the beta** (decision 2026-09-23). Update integrity does not depend on it, because updates need the Ed25519-signed manifest and the SHA-256. The risk is manual-download substitution. | launch: publish the installer SHA-256 in #downloads and serve only from `https://venice-releases.s3.amazonaws.com`. Post-launch: code signing. |
| ACL note: tester folder `Authenticated Users: Modify` | informational | Applies to the tester's copied folder only. The installed-path ACL is a VM-phase item (P-B RT-HIGH rows). | VM phase |

Note: Codex read the candidate card before the `$t` path fix and resolved it correctly to `C:\RedTeam\codex`; the hashes matched.

## Gemini, Claude (5.5) and claude2 (on-disk phase; claude2 also ran a keyed live pass)

| Theme | Reported as | White-box verdict | Action |
|---|---|---|---|
| Packet-bridge service (`VeniceNetSvc.exe` LocalSystem + WinDivert driver + meter-delay IPC verbs) | Claude B-0 CRITICAL, H-1, H-3, M-2, M-6, M-9; claude2 D1 HIGH | **Real surface, shelved feature.** Meter delay is compiled off in the app (`kMeterDelayShelved`), yet the installer still registers a SYSTEM service plus kernel driver, and the service still carries the meter-delay verbs. The DLL loads by full path from `{app}\packet_bridge` (Program Files: admin-writable only), so it is not a user→SYSTEM escalation by itself. Token-file ACL and fallbacks are unverified. | **Proposed: remove the packet bridge from the beta package and installer** (service, WinDivert pair, service registration). The app loads `VeniceNet.dll` dynamically, so it can be absent. Owner decision. |
| Analysis-tool lock uses substring match; includes `magnify`/`magnifier` | Gemini GEM-03 MEDIUM | **Confirmed and worse than reported.** `SecurityManager.cpp:299-330` uses `name.contains(tool)`, and customer packages set `lock_automation_on_analysis_tool: true`. Windows Magnifier, AIDA64 (`ida`), and any process name containing `r2`/`ida`/`hxd` lock automation, with the message "Release integrity verification failed". | **Fix for launch:** exact executable-name matching; drop magnify/magnifier. |
| Lease gate "off by default" (kill switch not enforced) | claude2 BC-2 | **False.** `LeaseGate::enabledFromEnvironment()` returns `true` under `ORION_PRODUCTION_BUILD` (`LeaseGate.cpp:35-38`); `build_prod_review` has `ORION_PRODUCTION=ON` → `ORION_PRODUCTION_BUILD=1`. | none |
| Installer `asInvoker` → service can't install | Gemini GEM-05 HIGH | **False positive.** The Inno bootstrapper's manifest is always asInvoker; `PrivilegesRequired=admin` (`orion.iss:99`) self-elevates. | none (moot if the bridge is removed) |
| Updater dev flags present in production binary | Claude M-1; claude2 F-BUILDPROFILE | **Guarded at compile time** (`updater_main.cpp:89 #if defined(ORION_PRODUCTION_BUILD)`). The strings remain; behaviour is refused. | post-launch: strip the strings |
| PDB paths leak developer username and path | all four | **Confirmed**, LOW. | **Fix for launch** if rebuilding anyway: `/PDBALTPATH:%_PDB%` |
| Dev-tree fallbacks (`~/Desktop/NexusVision/...`) in `RemotePlaySession::helperPath/sidecarScriptPath`; sidecar settings fallback | Claude M-5, L-2; Gemini GEM-04 | **Confirmed**, LOW-MED (same-user only). The app-directory candidate wins on a real install. | **Fix for launch:** compile the fallbacks out under `ORION_PRODUCTION_BUILD` |
| `/api/license/check`: `invalid_key` vs `device_mismatch` distinguishable; key echoed in response | claude2 F-5 MED, F-6 LOW | **Confirmed.** The key space is 36^16, so enumeration is impractical, but uniform denial, a rate limit on check, and suffix-only echo are cheap. | backend patch; owner deploys |
| `min_client_version` empty; no HTTP→HTTPS redirect; CORS `*` | claude2 F-1, F-4, F-3 | Confirmed; configuration. | launch checklist: set `min_client_version=1.0.0`; Cloudflare "Always Use HTTPS" (owner) |
| `/api/update` 404 and S3 object 404 | claude2 F-2 | Expected: not published until launch. | launch step |
| Unsigned installer and PEs | all four | Accepted for beta by the owner. | publish the SHA-256 in #downloads |
| chiaki-ng fork licensing; game-specific tailoring | Claude M-7, M-8 | Legal and business questions, not code. | owner/legal |
| Discord ID as identity; privacy policy | Claude L-5 | Check that privacy.html names the Discord ID. | copy check |
| Named pipes same-user; ViGEmClient unsigned; duplicate OpenSSL/Qt; SecurityCore single key | Claude H-2, H-5, H-4, L-3, L-4 | Same-user or local-admin threat model, or footprint. No privilege boundary crossed. | post-launch hardening list |

## Final round: Codex in Windows Sandbox, rc6 (installer c787ea2e…), 2026-09-24 18:19–18:57Z

No exploit reproduced. The lane verdicts were blocked on untested authenticated flows and on the unsigned build.

| ID | Codex | White-box verdict | Action |
|---|---|---|---|
| VEN-EXT-006 updater exits 2 on the pristine signed fixture | MEDIUM | **Expected, by design.** The production updater refuses `--manifest-file` ("a local manifest file is not permitted in production builds", `updater_main.cpp:854`) and ignores `--current-version`. It accepts only the pinned `/api/update`. The fault is the silent exit: it writes to its log file, not stdout. | Post-launch: print the refusal reason to stderr. Before the first real update, rehearse it end-to-end via `/api/update` on a staging install. |
| VEN-EXT-005 `ok`/`release_notes` outside the update signature | LOW | Confirmed. `release_notes` is parsed as `notes` (`UpdateManifest.cpp:175`). Altering it requires breaking the SPKI-pinned TLS to `api.zaeorion.com`. | Post-launch: sign all consumed fields. |
| VEN-EXT-008 unsigned installer and PEs | HIGH | Accepted for the beta by the owner. | Publish the SHA-256 in #downloads; code-sign post-launch. |
| VEN-EXT-003 build inventory, exports, test hooks in the package | LOW | Confirmed. Reverse-engineering cost only; no bypass. | First update, with packing. |
| VEN-EXT-001 `/S` did not run silently | LOW | Tester error: Inno uses `/SILENT` or `/VERYSILENT`. The small-screen clipping is worth checking. | Post-launch UI pass. |
| VEN-EXT-002 "Online" shown while the process was firewalled | LOW | Cosmetic; the gate stayed locked. | Post-launch. |
| VEN-EXT-004 / 007 recovery text names Discord channels; no in-app privacy/refund links | LOW / MEDIUM | Confirmed. The site has the pages; the app does not link them. | First update: add links next to "I Agree" and in the recovery messages. |
| Tampered `AutomationCore.dll` still launches to the locked gate | control | Expected: the integrity lock applies to automation after sign-in. The post-auth assertion is covered by the security tests, and a live check is below. | Owner live check |

**Gaps that remain:** the authenticated lifecycle was covered live by claude2 in the on-disk round (replay, clock skew, device binding, 15-minute lease). What no tester has done is revoke a licence on a running client. That is a release blocker if it fails ("kill switch written but not enforced"), so the owner runs it: see the launch checks.
