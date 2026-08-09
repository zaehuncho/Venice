# Meter Delay — install-to-working end-to-end trace

Date: 2026-08-08. Status: current as of the `fix/timing-input-and-remoteplay-blockers`
working tree. Companion to `docs/ADAPTIVE_DELAY_PLAN.md` (design history) and the
`MeterDelayController.h` header notes (actuator semantics).

This is the full path from "customer runs VeniceSetup" to "the shot meter is actually
being delayed", with every silent-no-op point named and the truth-surface state it now
reports. The product rule this document audits: **the UI can never claim Meter Delay is
armed when it is not.**

---

## 1. What must exist on disk (installer output)

| Artifact | Put there by | Purpose |
|---|---|---|
| `{app}\OrionNative.exe` + Qt/DLLs | `installer/orion.iss` `[Files]` from the signed package | The app |
| `{app}\packet_bridge\NexusVisionSvc.exe` (+ its Nuitka dep tree) | `tools/package_orion_release.py copy_compiled_service()` — packaging **fails loudly** if the compiled bridge is missing | The elevated WinDivert packet bridge, Nuitka-compiled from `nexus_svc.py` by `scripts/build_nexus_service.ps1` |
| `{app}\packet_bridge\WinDivert64.dll` / `WinDivert64.sys` | same packager step (pydivert bundle, `vendor\windivert` fallback) | Kernel driver pair, loaded **on demand** by the service, never at boot |
| `%LOCALAPPDATA%\NexusVision\Orion Native\settings.json` | first app run | Carries `meter_delay_enabled` (default **true**), `meter_delay_ms` (250, clamped 200–300), `meter_delay_bypass_on_defense` (true) |

A loose `nexus_svc.py` / `.venv311` interpreter never ships
(`tools/release_filter_policy.py` bans them); the compiled exe is the only customer
host. Dev rigs run `nexus_svc.py debug` instead — the client treats both identically.

## 2. What must be registered (installer post-step)

`installer/orion.iss` → `CurStepChanged(ssPostInstall)` → `RegisterPacketBridgeService()`:

- `sc create NexusVisionSvc binPath= "\"{app}\packet_bridge\NexusVisionSvc.exe\" --arm-meter-delay" start= demand obj= LocalSystem`
- **`--arm-meter-delay` in binPath is what arms the intercept.** `nexus_svc` ships
  DISARMED by design (arm gate, `nexus_svc.py:451-484`); the installer registering the
  service *is* the deliberate operator arm action. A service registered without the
  flag comes up disarmed and says so (see state 3 below).
- `sc sdset` grants Interactive Users START/STOP so the unelevated app can demand-start
  it. Driving it is still bearer-token gated (per-session token file, HMAC compare).
- Failure is **fail-soft with a MsgBox**: setup completes, Meter Delay reports
  unavailable in-app (state 1). Uninstall runs `sc stop` + `sc delete`
  (`orion.iss:153-154`).

SCM-start routing inside the exe: the SCM invokes the ImagePath with no verb, so
`_is_service_run_invocation()` (`nexus_svc.py:487-500`) must route that (arm flag
aside) to the service dispatcher, not the CLI parser — pinned by
`tests/test_nexus_svc_delay.py::test_scm_launch_routes_to_the_service_dispatcher` and
`test_installer_armed_imagepath_both_routes_and_arms`.

## 3. What must come up at runtime (app side)

1. **Bridge link decision** — `OrionAppController::packetBridgeLinkConfigured()`:
   the loopback client + service auto-start run when
   `network_enabled || meter_delay_enabled` (both default ON).
   *2026-08-08 fix:* `meter_delay_enabled` joined this predicate. Before, the bridge
   ran only on the packet-capture diagnostics opt-in — whose only UI toggle was
   removed 2026-08-06 — so any install with the opt-in persisted `false` (the
   pre-2026-08-04 default) had a Meter Delay toggle that saved, logged and did
   **nothing**. Enabling Meter Delay now brings up its own prerequisites, at launch
   and live at the moment of the toggle (`setMeterDelayEnabled`).
   *2026-08-08 VeniceNet wave 1:* the passive-sniffing opt-in flag
   (`network_packet_capture_opt_in`) was **deleted entirely** — everything
   network-side ships on by default and the predicate lost its middle term. A legacy
   settings.json carrying the old key is ignored on load.
2. **Availability probe** (once per launch, cached):
   `packetBridgeBackendPresent()` = NexusVisionSvc registered (sc query) OR
   `nexus_svc.py` beside root/app dir. Feeds `meterDelayBackendAvailable` (the card
   banner) and the install guard that suppresses pointless 15 s sc.exe retries.
3. **Service start**: `ensurePacketBridgeRunning()` → `sc start NexusVisionSvc`
   (hidden window), 12 s grace, then debug-script fallback (dev rigs only).
4. **Handshake** (`NetworkBridge` worker, 127.0.0.1:47291):
   - service greets `{"event":"hello","version":3,"features":[...]}` —
     `features` contains `"meter_delay"` **only if the intercept was armed at start**;
   - client authenticates with the freshest live bearer token
     (`%PROGRAMDATA%`/`%LOCALAPPDATA%\NexusVision\nexus_bridge.token`);
   - per-verb refusals: a disarmed service answers every delay verb with
     `meter_delay_disarmed` (loud, never silent).
5. **Session gates** (`MeterDelayController`): enabled && Remote Play `Running` &&
   court IP known (passive court-flow classifier — needs the bridge running, which
   the link fix guarantees). Topology prerequisite: both the sniff loop and the
   intercept open WinDivert at the **NETWORK_FORWARD** layer, so the console's
   traffic must route *through* this PC (the ICS setup from
   `docs/ORION_REMOTE_PLAY_SETUP.md`) — on any other topology there is nothing to
   observe or hold, court discovery never qualifies, and the card honestly stays at
   "Waiting for a live game session (Court IP unknown)". Gates open → session
   `Ready` → policy `AlwaysOn` engages, ramps at
   100 ms/s to the configured 200–300 ms, keepalives every 150 ms (service watchdog
   zeroes the delay after 500 ms of starvation — a crashed app can never leave the
   console delayed; last-client-disconnect dead-man does the same).
6. **Echo** (restored 2026-08-08): the service's own applied-delay snapshot
   (`meter_delay_active` / `meter_delay_ms` / `meter_buffer_depth`) rides its
   `{"event":"meter_delay"}` state broadcast and every delay-verb ack;
   `NetworkBridge::meterDelayServiceEcho` feeds the card so "Active" quotes the
   number the service reports, not only the controller's book-keeping. Display-only —
   never a timing input (`PacketBridgeAuthority.h`).

## 4. The truthful states (Meter Delay card, `meterDelayStatusText`)

Precedence: backend absent > user OFF > service link/arm state > actuator ramp.
All pinned by `MeterDelaySettingsPropertyTests::meterDelayStatusLineIsHonestAcrossArmStates`.

| # | Condition | Card says |
|---|---|---|
| 1 | No backend on this machine (probe) | "Not available on this install — the network-delay service is missing…" + warning banner with the re-run-installer remedy |
| 2 | Toggle off | "Off." |
| 3 | Bridge connected, service reports DISARMED (hello features / `meter_delay_disarmed`) | "Delay service is connected but DISARMED — no delay is being applied…" + banner |
| 4 | Link configured, not yet connected | "Waiting for the delay service connection — no delay is being applied yet." |
| 4b | Link not configured while enabled — **unreachable live since the 2026-08-08 link fix** (enabling the toggle now configures the link by definition); kept as defense-in-depth so a predicate regression reports truthfully instead of waiting forever | "Delay service link is off (packet capture is disabled)…" |
| 5 | Connected to a pre-features (v<3) service | "Connected to an older delay service that does not report arming — cannot confirm…" |
| 6 | Armed, session gates not open | "Waiting for a live game session (reason)." / "Checking connection quality…" |
| 7 | Armed + engaged | "Engaging — X of Y ms." → "Active — holding Y ms (service reports Z ms applied)." → "Releasing — X ms." |

"Armed"/"Active" is reachable **only** via `MeterDelayServiceState::Armed`, which
comes solely from the connected service's own hello/echo on the current TCP
generation and resets to Unknown on every disconnect (`NetworkBridge.cpp` worker
loop) — a dead or restarted bridge can never leave a stale "Armed" on screen.

## 5. Silent no-op audit

| Failure point | Before | Now |
|---|---|---|
| Capture opt-in persisted off (no UI to fix it) | toggle saved + logged, nothing ran — **the trap** | `meter_delay_enabled` alone configures the link (constructor, `ensurePacketBridgeRunning`, `setMeterDelayEnabled`); predicate pinned by `meterDelayAloneConfiguresTheBridgeLink` |
| No service registered (old install / sc-create failure) | silence | state 1 banner + one-shot log line naming the remedy; installer MsgBox at install time |
| Service registered but disarmed (registered without the flag) | client book-keeping showed "Active" | state 3; disarmed round-trip pinned on both sides (`test_disarmed_bridge_answers_loudly…`, `meterDelayStatusLineIsHonestAcrossArmStates`) |
| Service too old to report arming | optimistic verbs vanished | state 5, "cannot confirm" |
| Bridge dies mid-session | stale UI | arm state + echo die with the connection; service dead-man + 500 ms watchdog zero the delay console-side |
| App killed mid-session | console left delayed | service-side watchdog/dead-man (pytest-pinned) |
| Controller claims a delay the service isn't applying | possible | service echo quoted in the "Active" line; log census gets `Meter delay service echo:` transitions |

## 6. Remaining honest gap

- If `sc start` fails persistently (AV blocks WinDivert, driver policy), the card
  stays in state 4 ("Waiting for the delay service connection") indefinitely while
  the app retries every 15 s and logs each attempt. That is truthful ("no delay is
  being applied yet") but not self-diagnosing; promoting repeated start failures to
  their own state would need new plumbing and was deliberately left out.
- The end-to-end SCM path (`sc create/start` with the armed ImagePath on a clean
  machine) can only be proven by running the installer on a machine where loading
  the WinDivert driver is acceptable — not on the dev/daily-driver rig. The unit
  seams on both sides of that boundary are covered
  (`_is_service_run_invocation`, arm-gate tests, installer-ImagePath routing test,
  hello/echo/disarmed client tests).
