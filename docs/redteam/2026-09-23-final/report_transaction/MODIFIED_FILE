# Venice rc1 — final internal red-team merge (2026-09-23)

## Decision

**BLOCKED for the frozen rc1 release unit.** The signed package inventory is coherent, but focused fixtures found backend kill-state fail-open, unaudited destructive mutation, owner TOTP step-up bypass, an unmanifested-file update gap, and update/install recovery defects. Standard, StrictSecurity, VM and packaged-play gates are also open. This is an internal test result, not an approval to publish. No product source, frozen package, live backend, PSN, or 2K system was changed during this coordinator audit.

**Exact object:** C:\Users\aaron\VeniceRC\rc1-20260923. Product source commit 59129e5; fork 67eec725; docs-only HEAD at freeze ea8729b. Package has 578 files, of which 576 are covered by the signed release manifest. Sorted package path/hash digest: d5c1d2aabbcf9430bce4e05bc3f62fe5ffdabe2402710ce68245ea56330e32c6.

| Frozen item | SHA-256 |
|---|---|
| VeniceSetup-1.0.0.exe | D0383F12C867936A24040E108B5B8866F1BE7307F8E07BA19F2F8BDC614F8D50 |
| orion-package-1.0.0.zip | 209BD83F8F4E5A767DBA3F35BD9BD01B6210D3788D81395881CD4C8E167E7567 |
| update_manifest.json | 2652B3D64991DF8B357F70DED3E9C2CA22753F440098C5A7DA7457CB91DEEF5F |
| orion-package/release_manifest.json | 1083EA693F937612D51BA2EE467F2B8A0E0FE240AD08C280B6A03976FE06E950 |
| orion-package/release_manifest.sig | 63ACBA1C2D676E60FD42FD04BD14EC5AF6888122F7EA4C9960B70BFEB53E524D |
| orion-package/OrionNative.exe | 40D7AFB49059DD158BA8306D4149011327CB0256B48D7FF77847BE95A1E3711D |
| orion-package/OrionUpdater.exe | 838CACE7FF739D37F377F2B27701560AED4A0CF334C7CD1D0B958C036F1E6E1B |
| orion-package/OrionSidecar.exe | 6E3BF77F58374E22F8683EEF31EE482C320C4A54AFD312819DADE94A5197451E |
| orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe | 1F659A46570FACBB12F505BDA8B61AAF65E9A522C06C6DBEA470E5E48C180897 |

**Evidence classes:** PKG = exact frozen bytes or package verification; FIX = disposable fixture using the same source path (not a production-key/live update); SRC = source at 59129e5; DEV = owner dev-launcher session, not rc1; HYP = unproven field consequence. Findings below retain those boundaries. Scratch transcripts are in C:\Users\aaron\AppData\Local\Temp\venice-internal-redteam-rc1-20260923-130647 and in the Claude lane scratch directories listed by its report.

## Findings — LOW to CRITICAL

### LOW

| ID / evidence | Affected surface and path | Impact | Required fix and closure test |
|---|---|---|---|
| RT-LOW-01 / PKG+SRC | OrionNative.exe contains customer-visible “Feed paused: Orion is minimized” (GM-INT-03). | Internal codename in the activity feed, not an auth leak. | Change customer copy to Venice; minimize the packaged app and inspect the feed. |
| RT-LOW-02 / PKG+SRC | Release includes DEPLOY_LOG.txt, developer comments/build-path strings and a dead interpreter-discovery helper (CL3-F1-001/005). About 69 other ORION timing/fire-policy environment knobs remain production-active (CL3-F1-004). | Information hygiene and local configuration variability; no unlock or blind fire was demonstrated. | Remove non-runtime material; inventory every production env override, fence or document it, then re-scan the rebuilt package. |
| RT-LOW-03 / DEV+FIX | Normal capture-card closes are labelled child faults and pinned; four ordinary closes can evict a true incident bundle (CL3-F3-002). | Support loses useful evidence. | Mark parent-initiated close before pipe teardown; preserve abnormal bundles separately; simulate five ordinary closes around one fault. |
| RT-LOW-04 / SRC+FIX | Stripe event-id replay lacks explicit dedup, while entitlement transitions remain idempotent (CL3-F6-002). | Duplicate alert/audit noise, not duplicate entitlement. | Store processed event IDs and test replayed webhook deliveries. |
| RT-LOW-05 / SRC | Customer copy is inconsistent across website, Discord bot, installer and app about Remote Play-only, Xbox, trial and signed-download policy (CL3-F8-005/011). | Setup and purchase confusion. | Owner supplies one fact table; regenerate and compare all channels before publication. |
| RT-LOW-06 / SRC+PKG | The UI offers Arrow2 only and Pill is excluded, but native normalization still accepts packaged Arrow, Dial, Straight and Sword profiles (Codex reliability P1). | A hand-edited stored style can select an unsupported-looking route; no Pill re-enable or misfire demonstrated. | Normalize all supported ingress to the offered set or document/test each profile against packaged reader footage. |
| RT-LOW-07 / SRC | A SecurityManager first-run signature branch requires settings to be absent and present in nested checks, so is unreachable (CSEC-02). | Misleading recovery reasoning, not proven first-launch lock; controller has a separate sign-on-start path. | Remove dead branch and test fresh profile separately from existing profile with missing signature. |

### MEDIUM

| ID / evidence | Affected surface and path | Impact | Required fix and closure test |
|---|---|---|---|
| RT-MED-01 / SRC+model | Calibration Cancel calls setActuationLeadMs and converts Auto/zero into user-set 150 ms (CL3-F4-009; OrionAppController.cpp:9346-9410). | Persistent late timing on a fresh customer profile. | Restore the exact lead, provenance and user-set tuple; test Cancel on Auto/zero and manually-set profiles. |
| RT-MED-02 / DEV+SRC | Feedforward-displaced releases still train press-to-tip, no-meter hold, velocity and clock priors; six of six DEV FF epochs were accepted (CL3-F4-008). | Gradual learner drift; this is not package make-rate proof. | Fence every learner on applied displacement and replay matched-profile controls before/after. |
| RT-MED-03 / SRC | Syntactically valid but out-of-range learning.json values load and rotate into last-good (CL3-F4-006). | A corrupt file can bias timing rather than quarantine. | Validate semantic bounds before applying/rotating; test finite extreme values and backup recovery. |
| RT-MED-04 / SRC+DEV | Controller-origin physicalShotEpoch advances on Square down, but the latch receives shot_.physicalShotEpoch, which is assigned only by beginShot; wholly undetected METER presses never populate that ShotContext field. Idle raw sightings can also reset an existing streak before the epoch check (CL3-F4-007 / M-13). | On a meter-breaking patch, timing stops but no explicit unavailable state appears. The wrong-style control had 80 presses and zero bot releases; **silent fire was not observed**. | Drive the warning from completed unanswered controller-origin presses, clear only after in-epoch vision-owned detections, then replay altered/missing/false-lock corpora through the compiled sidecar. |
| RT-MED-05 / FIX+SRC | A 30 Hz capture request self-qualifies although timing was validated at 60 Hz (CL3-F5-006). | An unvalidated frame rate can acquire timing authority; no hardware misfire proved. | Require validated fps or preview-only operation; rig-test 30/60 Hz. The distinct wrong-device identity finding is RT-HIGH-06. |
| RT-MED-07 / FIX+SRC with conflicting fixture setup | Claude observed production route-transition fixture 3/5 and dev 5/5 with a missing staged model; Codex's separately copied production run crashed with 0xC0000409, so neither is a valid passing production gate (CL3-F2-001/F3-004). | StrictSecurity status is unknown and likely blocked by the fixture/environment; product refuses missing-model input correctly in the observed route. | Stage the model or explicit fixture seam in an isolated build, retain a missing-model negative, reproduce 5/5, then run exact-candidate StrictSecurity. |
| RT-MED-08 / FIX+SRC | Dev-profile update accepts a signed ZIP with extra unmanifested runtime files (C20), and trusts supplied --current-version for older signed manifest rejection (C25). The Python release verifier accepted an unknown key ID with an in-manifest self-supplied public key, although native trust rejected unknown IDs (CSEC-03/04; CL3-F7-004/005). | A signing/pipeline mistake can install unlisted files; the Python gate can falsely approve an unknown key; an older signed artifact plus invocation control can downgrade. No forged production Ed25519 signature worked. | Exact signed inventory at stage/post-apply, pin the Python verifier to the production keyring, derive installed version from verified manifest and pin production manifest origin; repeat C20/C25 and unknown-ID negatives. |
| RT-MED-09 / SRC | Settings and signature are separate writes; a mismatch locks automation, but QML lacks a visible repair path (GM-INT-02 / CL3-F8-006). Deleting the signature can cause next-start re-sign of current settings; DebugPage also exposes “Sign Settings” (CSEC-05). | Customer recovery is poor, while local config tamper may be laundered through bootstrap. Authentication and lease/fire gates still apply; no licence bypass. Gemini’s “permanent critical lock” is refuted. | Atomic save/sign, one-time bootstrap marker, visible narrowly scoped repair and no silent re-sign after tamper; test mismatch, missing signature and crash between writes. |
| RT-MED-10 / SRC+FIX | Sleep/resume is treated as a UI-freeze SAFE MODE, and kill-switch activation can be shown as an internet problem (CL3-F8-007/008). | Fail-closed but misleading and support-heavy field behavior. | Steady-clock suspend handling and distinct service-disabled error mapping; VM sleep/resume and kill fixture. |
| RT-MED-11 / SRC | The frozen signed update manifest has an empty artifact_url. A publishable URL requires changing and re-signing it (CL3-F1-002). | Final customer manifest is not the audited frozen object. | Sign final URL, freeze its hash, re-verify trust/ZIP and do a real TLS update canary. |
| RT-MED-12 / SRC; runtime replay untested | Heartbeat lease signs key, machine and expiry, but not a request nonce/account-session epoch (CSEC-06). | An old same-key/same-machine response may be replayable before expiry or across a backend state change; different key/device is not thereby authorized. | Bind signed response to request/session epoch and monotonic freshness; replay after revoke/sleep/clock jump against the actual fire gate. |
| RT-MED-13 / SRC+unit | Input soft-fault budget is three per rolling ten seconds, but the aggregate total is diagnostic only; the fork unit explicitly allows nine spaced faults (Codex reliability P4). | Repeated neutral/reconnect churn can persist for a session, although no stuck button or silent handback was shown. | Add a documented lifetime/recovery budget and a nine-cycle dummy-pipe test, then validate human restoration after full stop. |

### HIGH

| ID / evidence | Affected surface and path | Impact | Required fix and closure test |
|---|---|---|---|
| RT-HIGH-01 / SRC+ACL; VM pending | Installer permits a custom destination, creates LocalSystem VeniceNetSvc from that destination, grants Interactive Users service start/stop, and does not harden install-folder ACLs (GM-INT-01 / CL3-F7-003; installer/orion.iss). | Conditional local standard-user to SYSTEM path when an admin chooses a user-writable install tree. This is a privilege-boundary release blocker; actual installed DACL needs VM proof. | Restrict to protected Program Files or enforce protected DACL before service registration; prove icacls, sc qc, denied file replacement/start in a two-user VM. Check uninstall never deletes unrelated chosen-folder data. |
| RT-HIGH-02 / FIX+PKG | OrionAppController.cpp:5874-5903 uses QFileInfo::isWritable to decide whether to request UAC. A Qt 6.8.0 probe of the same expression on C:\Program Files\Venice returned installDirWritable=1, elevationRequested=0 although Users has RX only (CL3-F7-001 / M-14). | Default-install updater starts unelevated and cannot stage; mandatory/emergency update cannot reach customers. It fails closed rather than installing tampered bytes. | Use an actual write check or reliable Windows ACL test/requireAdministrator; standard-user and admin VM updates must prompt and end at exact new hashes; UAC decline leaves exact old hashes. |
| RT-HIGH-03 / FIX, dev-profile updater | A held retired DLL caused “Update failed AND rollback failed” and left Qt6Core.dll, UpdaterCore.dll and release manifest/signature NEW with other files OLD (CL3-F7-002, C27). | Mixed install fails integrity and needs repair; violates exact-old-or-new rollback invariant. | Transactional rename-aside/retry/recovery journal; repeat C27/C28 with lock held and released, comparing the full inventory to exact old/new units. |
| RT-HIGH-04 / SRC+missing drill | No fleet meter-lock signal, detection-only off switch, patch-day runbook/support macros, or rehearsed signed emergency update; global kill disables the whole product (CL3-F8-010 / P9). | A game-meter patch can fail every customer before detection or recovery catches up. | Implement privacy-minimal denominated counters, alert threshold, detection-only off, customer status template and a timed signed no-op update/rollback drill. |
| RT-HIGH-05 / gate record | Standard and StrictSecurity have **not** run on rc1; no standard-user VM install/UAC/service ACL/update/rollback canary, no packaged-unit owner play session, no final signed URL manifest. Installer Authenticode status is NotSigned while installer/build_installer.ps1 and the Discord post gate forbid an unsigned public installer. | The required release proof and distribution unit are incomplete. | Fix above, freeze rc2, run Standard and StrictSecurity on that exact unit, record literal transcripts and hashes; Authenticode-sign or obtain an explicit policy decision and make publication controls consistent. |
| RT-HIGH-06 / FIX+SRC | Two all-mocked capture opens gave identity_verified=True with the wrong device: selected A busy but card B opened, and a cached HD60 X index reopened as a webcam (Codex reliability P3 / CL3-F5-007/008). Startup inventory can remain frozen for the sidecar lifetime. | Selected/opened-handle mismatch grants fire authority to another source; no hardware shot misfire was proved. | Bind selected stable ID to the opened handle, refresh on reopen/hotplug, deny authority on mismatch; rig-test two cards, webcam/reorder, OBS contention and generic index zero. |
| RT-HIGH-07 / FIX+SRC | Mocked audit-table failure let /api/admin/kill return 200 and mutate global kill without a durable audit row; destructive route updates state before non-required audit (CSEC-10, also revoke ordering). | Destructive staff/admin action lacks required audit trail, an explicit release blocker. | Required pre-mutation attempt audit and idempotent completion audit; failure fixture must return 503 with state unchanged. |

### CRITICAL

| ID / evidence | Affected surface and path | Impact | Required fix and closure test |
|---|---|---|---|
| RT-CRIT-01 / FIX+SRC | Owner-role staff bearer bypassed fresh TOTP for totp_disable and rotate_admin_secret: mocked enrolled owner, machine binding and required TOTP received HTTP 200 for both actions without a code (CSEC-12; backend/lambda_function.py:3481-3513, 4819-4838). Staff login uses submitted staff/machine identifiers, timestamp and nonce without an independent possession factor. | MFA removal and break-glass secret rotation from a bearer obtained through the weaker path; fixture-proved, not a live-account exploit. | Independent login possession proof plus action-specific fresh TOTP or equivalent for *every* owner path; deny and audit before mutation; test stolen bearer, wrong machine, expired code, disabled owner and audit outage. |
| RT-CRIT-02 / FIX+SRC | A warm cached “kill off” state is reused when the global-kill backing-store read fails selectively; a mocked /api/license/check returned 200 with a newly signed lease under that failure (CSEC-13; backend/lambda_function.py:494-506). Cold-state failure denies. | The kill switch can fail open for fresh/renewed authority precisely during an outage after owner intervention. | Deny new/renewed lease on unknown kill state or use a bounded versioned fail-closed protocol; test warm off→owner kill→selective read failure, actual lease/fire denial, then recovery. |

No unauthenticated licence unlock, forged-update install, service-to-SYSTEM VM exploit, stuck input, or blind fire was verified on rc1. Gemini labelled its custom-path and settings findings CRITICAL; custom-path escalation remains HIGH conditional on installed VM ACLs, and the settings issue is MEDIUM recovery/tamper handling. The two CRITICAL backend rows above are independently reproduced offline fixtures, not live production attacks.

## Coverage matrix

| Lane | Observed rc1/source/fixture evidence | Open proof or failure |
|---|---|---|
| P1 meter/reader | METER-mode blind backstop is suppressed by greenWindowPriority; Arrow2 only offered. Pill is withdrawn and pill.json is excluded; no live Pill beta acceptance carried forward. | No compiled-sidecar altered-meter/framedump corpus: regression test returned 6 skipped. Unavailable warning fails as RT-MED-04; first affected shot and carry not package-proved. |
| P2 decoder/session | Production input/route components pass focused fixtures; recovery revokes fire authority until a fresh route. | Claude production route fixture 3/5 and Codex scratch production run crashed; no valid passing StrictSecurity, composed packaged decoder/console receipt or soak. |
| P3 capture | Sidecar model smoke completed 100 DirectML inferences; sidecar manifest matches 62 files. | RT-HIGH-06 and RT-MED-05; no rig hotplug/two-card/OBS test or 60-fps sustained timing proof. |
| P4 input | Production InputProtocol 54/54, Retry 40/40, fork pipe harness 11/11; neutralization and retransmit fixtures pass. | RT-MED-13 lifetime budget; no constructed launcher↔real bridge↔console composition or Square+Triangle→Cross fixture; process-killing stale-sweep test is a hazardous post-freeze test issue. |
| P5 liveness/performance | Failing log-storage producer/shutdown fixture 6/6; shot records compiled OFF; LOCALAPPDATA fallback; one sidecar DML smoke. | No 60-fps/long-soak packaged run, GUI/input-stall or VM no-D: drill. |
| P6 timing/learners | FF, late-carry, sweep and env fences reviewed; production LEAD_FLOOR/BIAS/ONSET_FF env reads compiled out. | RT-MED-01/02/03; dev 80% EXC session ran a different launcher and changed source mid-session, not rc1 proof. |
| P7 local IPC/services | Service source, SCM SDDL, pipe and shared-memory code inspected; production IPC fixture paths pass. | RT-HIGH-01; fixed-name input pipe lacks first-instance/peer-process proof; no two-user VM pipe/SHM/token/loopback and installed service ACL proof. |
| P8 field robustness | Lease/heartbeat fixtures fail closed; two-instance mutex source reviewed. | Sleep, Wi-Fi, clock steps, two PCs, official Remote Play coexistence, reboot/update mid-session untested on the rig/VM; RT-MED-09/10. |
| P9 patch response | Wrong-style DEV control: 80 presses, zero releases. Signed package/update manifest verifies. | RT-MED-04 and RT-HIGH-04; broken default update RT-HIGH-02; no signed emergency drill. |
| L1 licence/auth | Mock invalid/revoked/offline/device/nonce/skew gates and package lease gate show no ordinary unlock; package-only audit reports no suspicious files. | RT-CRIT-02 kill-state fail-open and RT-MED-12 heartbeat freshness; no two-PC live sold-policy test or fresh installed-profile tamper matrix. A fixture with max_devices=2 silently replaced device A with B, but the current sold one-PC policy makes this a future-policy defect rather than an rc1 two-device blocker. |
| L2 money/trial | Fixture webhook/device cap is idempotent; Worker tests pass. | Event-id audit duplication; live Stripe/Discord settlement not tested. |
| L3 package/update | 576/576 signed files verify; ZIP 578/578 bytes match, 0 unsafe/duplicate/case-colliding/symlink members; dev updater negative-signature/hash/key/HTTPS matrix fails closed. | RT-HIGH-02/03 and RT-MED-08/11; production-key TLS update/rollback, exact staged inventory and final installer signing absent. |
| L4 staff/admin | Focused role fixtures pass on ordinary routes. | RT-HIGH-07 and RT-CRIT-01/02 show destructive audit, owner step-up and kill-state failures; owner IAM/WAF and all routes not production-proved. |
| L5 runtime/local trust | Frozen EXEs/DLLs match recorded build outputs; mismatched settings signatures disarm. | RT-HIGH-01, RT-MED-09; pipe/SHM DACL, token file, DLL search order and cross-user checks need VM evidence. |

## Adjudicated disagreements and nonfindings

- **F8 “blind timer keeps firing” is rejected for METER mode.** AutomationEngine.cpp:2129 sets greenWindowPriority, and maybeFireMeterBlindBackstop returns false at 21936 before firing. The DEV wrong-style control gave 80 presses/0 bot releases. The missing unavailable warning remains real.
- **GM-INT-05’s “Discord claims signed” premise is false.** The Discord download embed explicitly says the installer is not code-signed and its blocked_by field forbids posting. Get-AuthenticodeSignature on frozen installer returns NotSigned. That is a separate publication gate, not a misleading “signed” Discord claim. Website/public/index.html contains no “signed installer” phrase in the checked source.
- **GM-INT-04 small-screen high-severity claim is not verified.** Fixed 1400×820 design scaling is source-visible, but no packaged 1280×720/150%-DPI render or legibility measurement supports 5–7 px text or misaligned popups; the Claude offscreen render reported no overflow. Keep a customer UI test, not a confirmed HIGH.
- **Gemini's unconditional “unrecoverable settings lock” is rejected.** Existing signature mismatch fails closed, but source exposes a DebugPage Sign Settings action and missing-signature startup can re-sign the current file. That repair path is itself too permissive and not a customer-ready recovery. RT-MED-09 keeps both concerns without claiming an auth unlock.
- **No reported local pipe “press buttons” path is accepted without emitted-packet and principal evidence.** Cross-user DACL/peer checks are still a VM gate. Likewise no admin shell, revoked-entitlement unlock, production-key fake manifest acceptance, or PSN/2K service impact is claimed.
- **A test could have killed the owner’s OrionStream.** At commit 59129e5 tests/test_remote_play_client_stale_sweep.py ran terminate_chiaki_processes before its live-process skip, time-correlated within 40 ms to a dev disconnect. This is a strong hypothesis, not an observed initiator. The active working tree has a post-freeze correction; no broad pytest was run here.

## Verification and remaining release sequence

| Check on exact object or noted fixture | Literal result | Exit |
|---|---|---|
| tools/verify_release_integrity.py --package frozen/orion-package --update-manifest frozen/update_manifest.json --artifact frozen/orion-package-1.0.0.zip --strict-warnings --json scratch/package_integrity.json | [verify] files checked: 576; [verify] RESULT: PASS — integrity chain intact (576 files verified) | 0 |
| tools/security_audit.py --package-only --package-dir frozen/orion-package --json | [] | 0 |
| Independent package/ZIP inventory and SHA-256 | 578 package files; 576 signed; 62 sidecar files matched; 578 ZIP members matched; unsafe/duplicate/case-colliding/symlink/extra/missing = 0 | 0 |
| tools/sidecar_bundle_manifest.py --verify --root . --dist build/sidecar/autogreen_sidecar.dist | compiled sidecar matches current sources | 0 |
| Focused customer contract pytest (3 files; offline guard) | 43 passed in 0.50s; A7_OFFLINE_GUARD_BLOCKS 0 | 0 |
| Qt 6.8.0 updater-elevation probe (scratch copy) | C:\Program Files\Venice installDirWritable=1 elevationRequested=0; ACL Users:(RX) | 0 |
| Installer Authenticode | NotSigned; SHA-256 D0383F12C867936A24040E108B5B8866F1BE7307F8E07BA19F2F8BDC614F8D50 | 0 |
| Security reviewer focused Python package/backend/fixture suites | 96 passed; 79 passed; 5 passed; offline guard blocks 0 | 0 each |
| Security reviewer Worker and selected production-linked native tests | Worker 47/47; updater/lease/activate/broker/input/SHM/clock suites 61/23/18/11/5/54/28/12 passing | 0 each |
| Reliability reviewer compiled packaged sidecar detector smoke | 100 DirectML runs; median 25.69 ms, p90 29.933 ms, max 41.669 ms; model smoke only | 0 |
| Reliability reviewer altered-meter regression fixture | 6 skipped in 0.20s; no held-out footage corpus | 0, but zero behavioral coverage |
| Reliability reviewer isolated production engine/route test attempts | 0xC0000409 process exit; no passing result. Claude separately saw 3/5 production route tests | -1073740791 / separate nonpassing fixture |
| Standard on frozen rc1 | NOT RUN | — |
| StrictSecurity on frozen rc1 | NOT RUN | — |
| Standard-user VM install/update/rollback and owner packaged play | NOT RUN | — |

**Closure order:** (1) repair owner step-up, fail-closed kill-state reads, and required destructive-action audit; (2) repair service-install privilege boundary, exact staged inventory, updater elevation and transactional rollback; (3) repair missing warning, timing learner/Cancel and capture identity issues; (4) rebuild a coherent rc2 including any post-freeze edits and freeze the final signed URL manifest, signed installer and runtime inventory; (5) rerun the affected negative fixtures, Standard and StrictSecurity; (6) execute the two-user/standard-user VM service/IPC/update canary and owner packaged-unit rig session; (7) rehearse patch-day detection and signed rollback; only then make a new approval decision.

The post-freeze working tree includes installer appearance changes, edited timing/route tests, and a corrected stale-sweep test. None changes the frozen rc1 hashes or counts as a fix here. No known-good prior package was supplied, so rollback-to-prior-release readiness remains unproven.

## Reviewer evidence

- Codex security: docs/redteam/2026-09-23-final/RED_TEAM_REPORT.codex.security.md (merged; CSEC-01 through CSEC-13).
- Codex reliability: docs/redteam/2026-09-23-final/RED_TEAM_REPORT.codex.reliability.md (merged; P1–P9).
- Gemini: docs/redteam/2026-09-23-final/RED_TEAM_REPORT.gemini.internal.md (file supplied during audit; CLI provenance not independently verified).
- Claude integration: docs/redteam/2026-09-23-final/RED_TEAM_REPORT.claude.internal.md and claude_lanes/F1.md through F8.md.
- Frozen candidate record: docs/redteam/2026-09-23-final/CANDIDATE_SHEET_rc1.md. Its post-freeze edits and “clean except learning.json” statement are not current working-tree evidence.
