# Venice / Orion — internal red team: full coverage (wave 1 + wave 2)

You are an independent red-team engineer. Find every way Venice can fail, wedge, mislead the
customer, charge them wrongly, or be bypassed on every surface below, and write findings the owner can
act on. You work READ-ONLY. You are one of three teams (Claude, Codex, Gemini) writing to ONE merged
report, so follow the schema and severity scale exactly.

## 1. What Venice is (engineering codename: Orion — internal slugs keep that name on purpose)

Windows app for NBA 2K27 shot timing on PS5. Repo: `C:\Users\aaron\Desktop\NexusVision` (branch
`fix/timing-input-and-remoteplay-blockers`, a large UNCOMMITTED working tree — always inspect the
working tree, not HEAD). Components:

- **OrionNative.exe** (Qt 6.8 / C++): launcher UI, licence client + lease gate, controller input
  relay (RawInput DualSense → named pipe, ViGEm/XUSB fallback), the automation engine (the bot),
  updater hand-off. Source `native_orion/src`, QML `native_orion/qml`. Customer-facing name: Venice.
- **OrionSidecar.exe** (Python 3.12 compiled with Nuitka): vision — YOLO meter detector
  (ONNX/DirectML), colour fill reader, shot gate, orchestrator. Source
  `native_orion/backend/autogreen_sidecar.py` plus repo-root modules (`remote_play_orchestrator.py`,
  `simple_meter_reader.py`, `meter_locator_cv.py`, `player_anchor.py`, `shot_records.py`,
  `latency_estimator.py`, `xbox_remote_play.py`, `decoder_pipe_identity.py`).
- **OrionStream.exe**: patched chiaki-ng Remote Play client. Fork `C:\Users\aaron\Desktop\chiaki-ng-src`
  (branch `orion`, uncommitted working tree). Bot state arrives over a named pipe
  (`gui/src/orioninputbridge.cpp`, ACL'd to SYSTEM/Administrators/the interactive user), merges with
  the physical pad before encryption (`lib/src/orioninput.c`, `lib/src/feedbacksender.c`), frames are
  exported to the launcher (`gui/src/orionframeexport.cpp`).
- **OrionUpdater.exe**: installs Ed25519-signed updates (`native_orion/src/updater_main.cpp`,
  `UpdaterArchive.cpp`, pinned public key id `orion-ed25519-v1`, `update_pubkeys.json`,
  `docs/UPDATER_CLIENT.md`). **VeniceNetSvc.exe**: WinDivert packet-bridge service for the deferred
  "meter delay" feature (`native_orion/venicenet_service/`). **OrionOwner.exe / OrionStaff.exe**:
  admin consoles (`native_orion/src/AdminToolController.cpp`, `native_orion/qml/admin/`,
  `docs/ADMIN_TOOL.md`), CLI `tools/admin/orion_admin.py`.
- **Backend**: AWS Lambda `orion-activate` (`backend/lambda_function.py`) behind HTTP API Gateway
  `v348t5hg3i` at `https://api.zaeorion.com` (explicit routes — 5 admin routes were added tonight,
  see `docs/OWNER_TOOLS_GATEWAY_BLOCKER_2026-09-21.md`), DynamoDB tables `orion-licenses`,
  `orion-audit`, `orion-staff`, secrets in SSM. **Website**: Cloudflare Worker `website/src/worker.js`
  + `website/wrangler.jsonc` (Stripe checkout, Discord OAuth, `/buy`, `/connect`, webhook), static
  `website/public/`, verifier `website/tests/verify_site.py`. **Discord**: Triton (`discord_launch/orion_bot.py`,
  licensing: `/claim_trial`, `/status`, `/hwid_reset`, `/deliver`, `/keygen`), Venice Guard
  (security/tickets/verify), Nereus (`discord_launch/nereus_bot.py`, announcements), interactions
  Worker `discord_launch/orion_worker.js`, webhook Lambda `discord_launch/gumroad_webhook/`.
- **Packaging**: `tools/package_orion_release.py`, `tools/security_audit.py`,
  `tools/release_filter_policy.py`, `scripts/verify_orion.ps1` (`-StrictSecurity`), installer
  `installer/orion.iss` + `installer/build_installer.ps1`, dev package under `release/orion-package`
  (`release_manifest.json`, `security_policy.json`). Runbooks: `docs/PACKING_RUNBOOK.md`,
  `docs/LAUNCH_PACKING_CHECKLIST.md`.
- Tiers: PS5 + capture card = supported; Remote-Play-only and Xbox = experimental, gated. Plan:
  free 7-day trial (one per Discord account, one per PC), then $19.99/month via Stripe; access is
  Discord-linked, no licence key for customers; one PC at a time; 3 free HWID resets.

Logs (dev build): `logs/orion_native.log` (8 MB, rotates to `.log.1`), `logs/orion_user.log`,
`logs/sidecar_crash.log`, `logs/sidecar_fault.log`. Production builds log under
`%LOCALAPPDATA%\NexusVision\Orion Native\`. Always `grep -a`; never cat a whole log.

## 2. Ground rules — non-negotiable

- Source, configs and packages are READ-ONLY: no edits, no builds (cmake / msbuild / ninja / nuitka /
  wrangler / iscc), no launching OrionNative, OrionStream, OrionSidecar or the consoles, no git
  commit/stash/checkout/reset, no deploys, no Discord posts, no Stripe changes. Another engineer builds
  in `native_orion/build`; a running launcher is never force-killed (it corrupts `settings.json`).
- Read-only AWS/Cloudflare/Stripe queries are allowed only with credentials already in your
  environment; create nothing. Never print secrets, tokens, private keys, SSM values, licence keys,
  or customer identities — refer to them by path or name.
- Targets are the owner's own systems only. Never touch PSN, NBA 2K servers, Sony, Stripe's or
  Discord's own infrastructure beyond the owner's account objects, or other users' data.
- Evidence over opinion. Every finding cites `file:line`, an endpoint, or a package path, and a log
  line number where relevant. State what would REFUTE your hypothesis, not only what supports it.
- Python-only tests may be run: `pytest -p no:cacheprovider --basetemp <your own temp dir>`. Run
  `tests/backend` and `tests/test_backend_staff_auth.py` in SEPARATE invocations (together they
  produce bogus collection errors).
- Two waves, both yours. WAVE 1 (A–E, the shot pipeline) is also being covered by another team in
  parallel — cover it again independently and do not read their reports first. WAVE 2 (S1–S16) is
  everything else. Report both in the one file, tagged by surface.

## 3. WAVE 1 — the shot pipeline (A–E). Another team is on these in parallel; cover them again, independently

Do not read the other team's reports first. Two teams converging on the same evidence is the point;
two teams copying each other is not. Start from `docs/redteam/2026-09-21/EVIDENCE_PACKET.md` (log
line numbers, incident counts, code anchors) and the two live bugs it documents.

**A. Controller input path, end to end.** Physical pad → launcher (RawInput/DualSense reader,
`OrionInputProtocol.h`, the input-hook pipe writer and its ack watchdog — heartbeat line
`Input hook heartbeat: connected= writes= failures= ack_failures= … ack_seq= expected_seq=`, route
generations/attestation, the ViGEm/XUSB fallback, `Input release-repair duplicate`, the fail-closed
recovery gate that re-seeds state "after physical shot controls were neutral"; anchors
`OrionAppController.cpp:13714`, `:13931`, `:14101`) → named pipe → fork `gui/src/orioninputbridge.cpp`
(pipe server, coalescing/drops ~`:198-214`, `DisconnectNamedPipe` `:65`/`:347`,
`owner_pipe_disconnected` `:339`) → `lib/src/orioninput.c` (queue capacity, ownership merge,
redundancy rules) → `lib/src/feedbacksender.c` (release twin +3 ms and echo +40 ms for PURE BUTTON
releases only) → takion feedback packets. Goals, in order: (1) root-cause the owner's bug — TRIANGLE
+ CIRCLE pressed together makes the direct pipe close and the launcher fall back to ViGEm (the
"disconnect"): the log shows a heartbeat write burst (8934 → 8961 in one second, `ack_seq` stuck at
8939) right before `Controller route changed before automation process: generation=2 attested=1
live=3; scheduled authority revoked` at 03:11:12Z; decide which side closes the pipe and on what
rule, whether a human two-button chord (each button change is a separate DualSense report) can reach
the ack threshold, whether coalescing or queue capacity is involved, and whether the release-repair
duplicate itself writes into a closing pipe and escalates. (2) Root-cause R2 "clamping down": are
analog triggers (l2/r2) covered by any release redundancy; what controller state is re-seeded on
recovery and on fallback; can a stale `r2=255` snapshot be replayed; how does the XUSB fallback map
triggers. (3) Every other way to wedge input: stuck button, lost release, double press, frozen
sticks, ownership mask never lifted, physical release dropped while the bot owns a shot, sequence
wrap, reconnect races, ack timeouts under load.

**B. Automation engine (the bot).** `native_orion/src/AutomationEngine.{h,cpp}` (ownership
acquisition, arm, scheduleFire, release, cooldown, hold band, banner trim, onset feedforward, learner
fences, reset paths), `PreciseFirePolicy.h`, `ShotGateProtocol.h`, `OnsetFeedforward.h`,
`LeadCalibrationPolicy.h`, the automation hooks in `OrionAppController.cpp` (scheduled authority,
release ticks, `Release ownership`, `IDLE-GATE` reasons, `SHOT NOT OWNED`), contracts in
`native_orion/tests/AutomationEngineTests.cpp`. Goals: fire without owning; fire twice for one press;
hold the shot button past the physical release; never release (stuck ownership — the log's
`sqLatch=1 … waiting_for_button_release`); fire on a stale epoch after a route change or pipe
recovery; gate armed across court change or session end; onset feedforward / banner trim poisoning
(trim bound, epoch keying); what happens to a SCHEDULED fire when the direct pipe closes mid-shot
and the route falls back — dropped, replayed on the new route, or held; whether the engine's release
marker or the release-repair duplicate can race a physical release into a press-release-press glitch.

**C. Meter detection and the sidecar shot gate.** `native_orion/backend/autogreen_sidecar.py`,
`remote_play_orchestrator.py` (shot gate, readiness — `Remote Play input session failed readiness`
~`:2824` and whatever sets `_last_error_msg`, SHOT RECORD / DISARM RECEIPT, capture health,
`Detector frame rejected: reason=dark_frame count=60 (feed fails closed)`), `simple_meter_reader.py`
(BOX LATCHED, DETECTOR HEALTH, static zone, quarantine, TTL, anchor), `meter_locator_cv.py`,
`player_anchor.py`, `meter_detector_yolo.py`, `shot_records.py`; native consumers
`ShotGateProtocol.h`, `SharedMemoryFrameReader.*`, the banner/verdict relay in
`RemotePlaySession.cpp`. Goals: is the readiness failure a CAUSE or a CONSEQUENCE of the pipe drop
(trace both directions with file:line and log lines); what "fails closed" stops and how it recovers —
can it leave the gate armed but blind, or the native side waiting for a proposal that never comes;
ownership on stale sidecar evidence (physical epoch vs sidecar epoch, `stale_samples`,
`stamp_epoch_seen`); false locks, ghost meters (the previous shot's feedback meter), static zone
withholding real reads, box latched on the wrong box, quarantine timing — which of these can fire
the bot on a non-shot; robustness (locked temp dirs, DirectML→CPU fallback, cadence stalls, SHM
timestamp rejects, log rotation mid-session). The reader files are owned by another engineer:
report only.

**D. Session lifecycle, decoder/frame path, and the fork's uncommitted thread pinning.**
`RemotePlaySession.{h,cpp}` (connect, disconnect, court change, standby/pre-booted client,
reconnect, executable policy), `RemotePlayExecutablePolicy.h`, the decoder pipe and
`SharedMemoryFrameReader.*`, capture fallback (MSMF vs DSHOW), preview pipeline; fork
`gui/src/streamsession.cpp`, `orionframeexport.cpp`, `orioninputbridge.cpp` lifecycle
(start/stop/join, `fail_transport`), `lib/src/takion.c`, `thread.c`, `time.c`, `videoreceiver.c`,
`senkusha.c`. Goals: any path that ends a session unexpectedly, wedges the decoder, leaks a
handle/thread, or desynchronises "stream is live" between launcher and OrionStream (how is
OrionStream's death detected, what happens to the route and the bot); the uncommitted affinity code
assumes an SMT layout (5800X: LP0+LP1 = core 0) — what happens on 4-core, non-SMT, hybrid P/E-core
Intel, a VM, or an unexpected `GetLogicalProcessorInformationEx` layout (two hot threads on one
core, a nonexistent core, an ignored `SetThreadAffinityMask` failure, a crash); AV-clock CSV
(`ORION_AVCLOCK_PATH`) path validation, truncation, senkusha-vs-session double open; microsecond
clock wrap/monotonicity; whether TRIANGLE + CIRCLE or any chord reaches a chiaki quit/menu/stream
shortcut left over from upstream (yes/no with file:line).

**E. The controller-route recovery state machine across both processes.** Launcher: route
generations and attestation, `Timing cache route proof issued by neutral local delivery … awaiting
exact sidecar echo`, `Timing route acknowledgement crossed a sidecar scope transition … proof
revoked`, `Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback`, the
heartbeat/ack watchdog, `Input route reassert FAILED`, the independent fail-closed gate cleared by
`Direct controller pipe recovered`. Sidecar: `Latency route re-keyed
(controller_delivery_route_transition); restored authority awaits fresh health`, readiness, the
health/echo it sends back. Goals: explain the PERIODIC cycle from 22:22 local (pipe closed →
readiness failed → recovered, every ~10-15 s) as a text sequence diagram with log-line and file:line
citations; which side INITIATES each close; is it a livelock between the launcher's recovery and the
sidecar's health check or the bridge's disconnect; what ends it; the exact trigger of the first
incident (what produces ~27 writes in one second, which watchdog rule turns ack lag into a route
change — cite the constants); the MINIMAL change that stops the flap and the false route change
WITHOUT weakening fail-closed behaviour (the bot must never fire on a route it cannot prove); other
inputs that can start the cycle (stick spam, trigger ramps, USB pad power events, sidecar restarts,
log rotation, court change).

## 4. WAVE 2 — every other path (S1–S16). Cover ALL; rank by what you find

Each entry: what to inspect, what to try, and the prior incidents that make it worth your time.

**S1. Settings and learning persistence.** `native_orion/src/AppConfig.{h,cpp}` (validate/save/load),
`SecurityManager.cpp` (settings signature, `settings.json.sig`), `learning.json`, profile
export/import, first-run tour state, log rotation. Try: kill mid-write (atomic temp-file + rename, or
in-place?), zero-byte or partial files on load, signature mismatch handling (fail closed or silent
reset?), out-of-range values restored from disk, two launchers racing. Prior: a kill mid-write zeroed
`actuation_lead_ms`; `learning.json` was found 0 bytes. Can a customer lose a tuned lead silently?

**S2. Lead / latency seed path.** `models/latency_factory_prior.json` (measured on ONE route),
`latency_estimator.py`, the auto-seed (placeholder → measured estimator + margin, 2 ms/shot limit),
estimator authority, banner-driven trim (streak integrator), the Shot Lead slider (1..100 ↔ 150..400
ms), the guided lead calibration added tonight (`LeadCalibrationPolicy.h`,
`OrionAppController::beginLeadCalibration / reportLeadCalibrationVerdict / cancelLeadCalibration`,
`qml/components/ShotLeadCard.qml`). Try: a seed that never converges; a factory prior on another
route (Bluetooth, RP-only); bisection fed wrong verdicts (early/late swapped, skips); step halving
that never locks; persistence of each step when the customer cancels mid-way; trim and calibration
fighting. Prior: lead = `learned_latency_ms` alone — a 6 ms seed vs ~75 ms real made EVERY shot
late; no customer has used the calibration screen.

**S3. Capture-card tier.** Device enumeration/selection, MSMF vs DSHOW and the fallback between
them, requested vs delivered fps, another app holding the card, DPI / multi-monitor placement of
preview and overlay, the SHM frame ring (`SharedMemoryFrameReader.*`, timestamp rejects,
depth/underflow). Try: card busy, unplugged mid-session, 120 Hz requested, format changes, two cards.
Prior: the capture-API fallback "bricked the bot" (MSMF-not-DSHOW; grep "warm timing revoked"); the
HD60 X accepts 120 Hz but delivers 60 → lates; OBS owning the Elgato → "no live device"; the
capture-phase lock lease bug.

**S4. Controller hardware and network variance.** DualSense over USB vs Bluetooth, USB selective
suspend, hot-unplug/re-plug mid-shot, two pads, DualSense Edge and third-party pads through RawInput,
PS5 rest-mode wake (`ps5_wake.py`, connect/wake path in `RemotePlaySession.cpp`), court IP change
mid-session, ICS topology (PS5 gets DHCP .100; a lab VM has stolen 192.168.137.1), Wi-Fi loss
bursts; the fork's redundancy constants in `lib/include/chiaki/orioninput.h`. Prior: a silent USB pad
(EPM=1) presented as "controller disconnects"; the stuck-button class IS a lost release datagram
(pure button releases now go 3×, analog triggers do not); the CLI stream never wakes a resting PS5.

**S5. Meter-delay service.** `native_orion/venicenet_service/` (VeniceNetSvc install/start, service
account, WinDivert filter needing the court IP, flush loop, delay cap), `MeterDelayIntercept.cpp`, the
launcher's `MeterDelayController` gates (`courtKnown_`), the D-pad-Up bypass hotkey, service
registration in `installer/orion.iss`. Deferred feature, but it ships and runs. Try: service
absent/stopped → loud or silent no-op?; court IP unknown → filter never installs; delay > headroom;
stale filter after a court change; service left after uninstall; the service's privilege and pipe.
Prior: "never engages locally — service down ⇒ silent no-op"; fresh installs started only the old
NexusVisionSvc so the feature was dead; offset > headroom silently killed shots.

**S6. Purchase → unlock, end to end.** Trace one customer: Discord OAuth → `/buy` → Stripe Checkout
(price id in `website/wrangler.jsonc`; $19.99 as of tonight) → webhook (`verifyStripeSignature`,
idempotency, amount checks) → Lambda provision → Discord role → `/connect` one-time code → launcher
unlock (`LicenseClient.cpp`) → heartbeat/lease (grep "lease") → renewal / cancel / refund /
chargeback → revoke. Try: webhook replay; wrong price id; test-mode subscription against live; refund
not revoking; cancel-at-period-end vs immediate; trial claimed twice (one per Discord AND one per PC
— is the PC side enforced server-side?); one-time code reuse or cross-account use; unlock on a second
PC; HWID reset counting (3 free, then deduct); `/deliver` and `/keygen` staff paths. Prior: refunds
once never revoked (a replay guard ate the ping); revoke wrote `status` while activate read the
`revoked` bool; "prod died 900 s after unlock" was a lease-key mismatch; the subscription DM said
$20 until tonight (fixed locally, not deployed).

**S7. Licence/auth client and offline behaviour.** `LicenseClient.cpp`, the entitlement cache
(`license_cache.enc`), machine-id/HWID derivation, nonce + timestamp handling, heartbeat codes
`frozen|blacklisted|version_blocked`, `client_version` on heartbeat, MOTD, the global kill switch
(`config.global_kill` — how often does the launcher read it?), API unreachable, clock skew, DNS
failure, captive portal. Try: unlock without the server (dev-key containment under
`ORION_PRODUCTION_BUILD`), cache tamper, replay of an old activate response, HWID spoof,
expired/revoked/killed key mid-session (does the session end, how loudly?). Any unlock without valid
entitlement is CATASTROPHIC.

**S8. Updater client and release package.** `updater_main.cpp`, `UpdaterArchive.{h,cpp}`,
`update_pubkeys.json`, `release_manifest.json`, `security_policy.json`, `docs/UPDATER_CLIENT.md`,
`tools/security_audit.py`, `tools/package_orion_release.py`, `tools/release_filter_policy.py`. Try:
fake manifest; bad or absent signature; wrong/unknown `public_key_id`; tampered `artifact_url` or
`sha256`; downgrade; non-HTTPS URL; zip path traversal; symlink/junction entries; DLL replacement in
the archive; rollback failure; missing libcrypto or OrionUpdater; `update_pubkeys.json` tamper; a
manifest that misses a runtime file; lab/test artifacts inside the package; unpruned Qt styles
(policy in `release_filter_policy.py`, gates `PACKAGE_QT_STYLE_*`). An unsigned or tampered update
installing is CATASTROPHIC.

**S9. Backend API and admin surface.** `backend/lambda_function.py` (route table near line 4900:
every `/api/admin/*`, `/api/staff/*`, `/api/bot/*`, `/api/activate|verify|update|version|deactivate|shard/*`),
`require_capability` role enforcement, audit rows (actor on every mutation), `orion-audit` GSIs,
TOTP enrol/confirm, admin-secret rotation, staff enrol/login/machine binding, rate limits, the kill
switch via `POST /api/admin/config`. Try: capability bypass by role; staff acting after disable;
destructive action without a reason; audit gaps; replay of signed requests; nonce/timestamp skew;
brute force on `/api/staff/login`; information leaks in error bodies; the five routes added tonight
(`GET /api/admin/metrics`, `GET|POST /api/admin/config`, `GET /api/staff/audit`, `GET /api/admin/audit`)
— new surface, never attacked. Tests: `tests/backend` (360), `tests/test_backend_staff_auth.py` (17)
— run them separately.

**S10. Website Worker and Stripe.** `website/src/worker.js`, `wrangler.jsonc`, `verify_site.py`
(the security-contract assertions). Try: CSP/HSTS gaps; `/buy` without a Discord session;
guild-membership check bypass; `return_to` open redirect; session cookie flags; checkout config
leakage; webhook signature/timestamp window; price/amount assertions; the billing-portal link;
`/connect` code issuance and expiry; the `/discord` profile page.

**S11. Discord bots.** `orion_bot.py` (Triton), `nereus_bot.py`, `orion_worker.js`,
`gumroad_webhook/`, `discord_commands.json`, `register_commands.py` (NEVER run it). Try: command
permission scoping (`/deliver`, `/keygen` ship hidden); role checks; ephemeral vs public replies
leaking codes; `/hwid_reset` abuse; trial-claim races; bot token handling; Nereus's link/ping
sanitiser; Venice Guard's verify-button component surviving a message PATCH.

**S12. Installer and first launch on a clean machine.** `installer/orion.iss`,
`build_installer.ps1`, `INSTALL_NOTICE.txt`, services registered, ViGEm driver dependency, VC++
runtime, DirectML/ONNX provider fallback, Qt plugin paths, per-user data dir creation, permissions
when installed for all users, uninstall leftovers (services, WinDivert driver), the launcher being
MANDATORY (starting OrionNative.exe directly orphans the sidecar and kills timing). Try: install as a
standard user; path with spaces/unicode; install over an existing install; antivirus quarantining
the Nuitka sidecar; no capture device on first run.

**S13. Experimental tiers' gates.** `xbox_remote_play.py`, `decoder_pipe_identity.py`, the
Remote-Play-only decoder pipe path, tier selection and gating in the launcher. The features may be
rough; the GATES must fail closed: can a customer enable Xbox or RP-only without the intended gate,
what does the UI promise, what happens to timing when the decoder pipe delivers late frames.

**S14. Owner/staff consoles.** `AdminToolController.cpp`, `qml/admin/*`, `tools/admin/orion_admin.py`.
Try: one-time secrets shown twice (enroll key, new licence keys, TOTP URI, rotated secret); secrets
persisted anywhere (memory-only is the contract); `--check-startup-security`; release-integrity
verification in production builds; the raw-response viewer leaking tokens; staff caps display vs
server enforcement; kill engage → disengage round-trip through Config.

**S15. Binaries and packages as an attacker sees them.** Strings scan of `release/orion-package` and
`native_orion/build/Release`: URLs, admin endpoints, dev keys, debug env vars that unlock protected
behaviour (`ORION_*`), the deep-link owner check, DLL import/export surface, DLL search-order hijack
from app dir / current dir / PATH, swapping `OrionNative.exe`, `OrionUpdater.exe` or libcrypto,
running from a renamed/moved folder, deleting `security_policy.json` or `release_manifest.json`.
Do not build exploit tooling; document the path.

**S16. Logging and telemetry hygiene.** What launcher and sidecar write to `logs/` and
`%LOCALAPPDATA%`: PII (Discord ids, emails, machine ids, court IPs), secrets, licence keys, one-time
codes; log growth and rotation; what a customer would paste into a support ticket; sidecar WARNING
throttling (per-shot WARNINGs vanish — never infer dead code from a missing WARNING).

## 5. Severity scale — use exactly these five words

- **CATASTROPHIC** — unlock without valid entitlement; unsigned/tampered update installs; secret or
  private-key exposure; a customer charged a different amount than shown; loss of a customer's tuned
  profile with no recovery; remote code execution.
- **CRITICAL** — the bot presses or holds input outside a shot (input hijack); session-wide input
  loss; kill switch written but not enforced; staff destructive action without an audit row; the
  purchase → unlock path broken for every customer; release integrity check bypass.
- **HIGH** — timing silently wrong for a class of customers (seed, lead, capture cadence); detection
  silently degraded; a service failing open when it must fail closed or vice-versa; updater rollback
  failure; first launch failing on a clean machine; PII in logs.
- **MEDIUM** — recoverable failure with a visible symptom; misleading UI or log; robustness gap
  under hardware/network variance; audit row present but incomplete.
- **LOW** — hygiene, dead code, docs drift, cosmetic, non-actionable observations.

## 6. Report format — one file, this exact schema (three teams' files merge by concatenation)

Write to `docs/redteam/2026-09-21/RED_TEAM_REPORT.<team>.md` where `<team>` is `gemini`, `codex`
or `claude`. Finding ids are `<TEAM>-<NNN>` with team prefixes `GM`, `CX`, `CL`.

Header block:

```
# Red team report — <team> — 2026-09-21
Working tree: NexusVision @ <git rev-parse --short HEAD> (+ uncommitted), chiaki-ng-src @ <rev> (+ uncommitted)
Surfaces covered: S1 … S16 (list)
Surfaces NOT covered and why: …
Method: (read-only review; which tests you ran; which logs you read)
```

Then findings grouped under `## CATASTROPHIC`, `## CRITICAL`, `## HIGH`, `## MEDIUM`, `## LOW`,
most severe first, in this shape:

```
### [GM-001] CRITICAL — <one-line title>
- Surface: S6 purchase → unlock
- Exploit / failure path: <how it happens, step by step>
- Affected: <file:line | endpoint | package path>
- Impact: <who is hurt, how badly, how often>
- Reproduction: <numbered, non-destructive steps>
- Evidence: <what you actually observed: log line numbers, code, test output>
- Root-cause hypothesis: <…>
  - Supporting evidence: <…>
  - Evidence that would refute it: <…>
- Required fix: <minimal and specific>
- Verification test: <unit test sketch or manual check that proves it fixed>
- Owner decision needed: no | yes — <what>
- Confidence: high | medium | low
```

Close with three things:

1. **Executive summary** — at most 15 lines: what is actually dangerous, what is noise.
2. **Findings table** — every finding: id · severity · surface · one-liner.
3. **Release verdict for the beta** — one of `blocked` / `needs changes` / `approved`, followed by
   the exact blockers by id. A security-sensitive feature is not approved unless it fails closed.

Rules for writing: one finding per root cause (do not split one bug into five symptoms); no finding
without evidence; if you could not reach a conclusion say so and name what would settle it; never
soften a CATASTROPHIC to keep the list short; never inflate a LOW to look thorough.
