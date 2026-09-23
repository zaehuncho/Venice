# Venice launch red team: MASTER report (internal wave, 2026-09-22)

**Sources**

| Team | Report | Coverage | IDs |
|---|---|---|---|
| Claude | `RED_TEAM_REPORT.claude.md` | P1–P9, 69 findings | CL2-* |
| Codex | `../2026-09-21/RED_TEAM_REPORT.codex.P1-P9.consolidated.md` | P1–P9, 18 findings | CX:… (lane-prefixed) |
| Gemini | `RED_TEAM_REPORT.gemini.md` | L1–L5 + breadth, 26 findings | GM2-* |

Carryover: the Codex final gate `../2026-09-21/RED_TEAM_REPORT.codex.final.md`. **L1–L5 were covered by Gemini only.** Codex's security pass on L1–L5 is still owed before the external round.

**Overall: BLOCKED.**
- No team confirmed a path to an unwanted input in a live game.
- The blockers:
  - a stale release package;
  - input liveness (stall, lockout, unbounded soft faults);
  - log-sink liveness on a full disk;
  - local service/pipe trust;
  - update and patch-day readiness;
  - the carried persistence, kill and audit items.

**Verification done while merging (read-only).** These change several severities.
- **GM2-001** (entitlement bypass, "catastrophic") → **low**. `LeaseGate::enabledFromEnvironment()` returns `true` under `ORION_PRODUCTION_BUILD`, so the lease gate cannot be disabled in customer builds. The residual risk (patching the binary in memory) is inherent to any client.
- **GM2-002** (SYSTEM service binary writable, "catastrophic") → **high, conditional**.
  - The default install is `{autopf}\Venice` (Program Files, admin-only write, P7 verified).
  - `DisableDirPage` is not set, so a customer can pick a folder outside Program Files. There, inherited ACLs may let standard users replace `packet_bridge\VeniceNetSvc.exe`, which the SYSTEM service runs.
- **GM2-003 / CL2-P7-001 / CX P7-03** confirmed. On this PC, `icacls C:\ProgramData\NexusVision` grants `BUILTIN\Users` create-files/folders (`WD,AD,WEA,WA`), and the folder's owner is the standard user.
- **GM2-009** (unmasked key, "critical") → **low / owner policy**. The full key appears only in an **ephemeral** reply to the staff member who ran `/deliver` (`orion_bot.py:823`), not in public. This is the open owner decision GM-005 ("staff-visible keys").
- **GM2-011** (owner TOTP disable) confirmed. `totp_disable` (`lambda_function.py:4632`) sets `owner_totp_required=False` without a fresh TOTP code. Any path that reaches that owner action without TOTP (e.g. the staff-session owner route) can switch 2FA off.
- **GM2-016** (heartbeat replay) → **medium**. The lease is Ed25519-signed and certificate-pinned. The gap is that nothing binds the lease to its request, and the expiry is checked against the wall clock. The fix (a request nonce + a monotonic lease) is agreed.
- **GM2-007 / GM2-008** are partly **stale**. The fork-side 25 ms teardown was fixed on 09-21 (budget 40, soft fault). What remains open: the launcher-side 25 ms kill (CL2-P2-003), the unbounded soft fault (CL-001), and the cross-trigger redundancy (CL-003).

## CRITICAL / release blockers

| # | Finding | Sources | Owner |
|---|---|---|---|
| M-01 | **The release package is not the tested code.** Package `OrionNative`/`OrionStream`/updater hashes ≠ the current builds. | CX P4-04/P0-01 | Claude: rebuild after the patches, then Standard + **StrictSecurity (owner)** on that exact unit |
| M-02 | **A permanent log I/O failure or a full disk blocks the GUI and input control path**, and can hang shutdown. | CX P1/P4-03, CX-015, CL2-P5 (evidence), CL2-P8 | Claude |
| M-03 | **Input ownership after a delivery fault is unbounded.** The soft-fault loop has no deadline, the drop/ACK is unchecked, stale output is not neutralised, and it resends at up to 30 Hz. The launcher-side 25 ms ACK timeout still kills the session. After a soft fault the release loses its copies, and the fork and launcher disagree on ownership. | CL-001, CX P5-02/P8-04, CL2-P2-003, CL2-P4-003/004, GM2-007 (partly stale) | Claude |
| M-04 | **No dead-man switch.** The input tick, receipt and subtick run on the lowest-priority GUI thread (receipt spikes up to 280 ms). A stall leaves the console holding the last input, with the human locked out. | CL2-P4-001, CL2-P5-001 | Claude |

## HIGH

| # | Finding | Sources | Owner |
|---|---|---|---|
| M-05 | The input pipe has a fixed name, SID-only authentication, no first-instance flag and no peer process/image check. The DACL is already user-only. | CX P1-02/P7-02, CL2-P7-003, GM2-004 | Claude (fork + native) |
| M-06 | The service token is readable by Interactive Users. `C:\ProgramData\NexusVision` is user-creatable/owned, and the SYSTEM service writes into it. Token/file race. | CX P7-03, CL2-P7-001/004, GM2-003, GM2-017, GM-007 | Claude + installer |
| M-07 | The SYSTEM service binary is writable if the customer picks a custom install folder. | GM2-002 (conditional) | Claude: explicit ACL on `{app}\packet_bridge` or force Program Files |
| M-08 | A second Venice instance runs; its Connect kills the first one's stream. | CX P8-03, CL2-P8-001, GM2-005 | Claude |
| M-09 | The fire lease lapses silently after sleep or an internet drop: no retry until the next 5-minute tick, and the UI says Ready. | CL2-P8-002, GM2-021, CX P8-01 (part) | Claude |
| M-10 | Terminal trigger redundancy on a simultaneous opposite-trigger change. Plus **my 09-22 R2 regression** (the shared neutral predicate now includes the triggers). | CX P1-01, CL-003, CL2-P4-002, GM2-008 (partly stale) | Claude |
| M-11 | Total sidecar telemetry silence keeps a fresh-looking health snapshot. | CL-006, CX P3/P5-01 | Claude |
| M-12 | learning.json: "last good" accepts semantic garbage and rotates before a verified replacement. Legacy maps bypass the plausibility checks. | GM-002/CX-001/CX-008, CX P2-02/P6-03, CX P2-03 | Claude |
| M-13 | **2K patch safety.** The bot shoots blind when the meter is gone. A changed meter gives silently wrong timing. There is no fleet telemetry and no remote pause. | CL2-P9-001..004, CX P9-01, GM2-015 | Claude (+ owner telemetry/privacy decision) |
| M-14 | **Updates.** The unelevated updater cannot write the Program Files install. Emergency turnaround is unproven, and there is no canary. | CX P9-02, CL2-P9-005 | Claude + owner |
| M-15 | **Capture.** A non-card device can become the source and is never retried. Cards without a hint word never get timing authority, and nothing tells the customer why. | CL2-P3-001/002 | Claude |
| M-16 | Go-To meters appear ~2 s after the stick push; the windows assume ≤ 1.6 s. | CL2-P1-001 | Astra (reader) + Claude (engine) |
| M-17 | Remote-Play-only mode has no retained field evidence. The owner reports it works. | CL2-P2-001 | Owner: logged retest |
| M-18 | Owner TOTP can be disabled without a fresh code. | GM2-011 (verified) | Claude: step-up code on disable |
| M-19 | DLL search order / preload trust. | GM2-012, CX-018, GM-012 | Claude |
| M-20 | Trial farming: one trial per Discord account plus a client-supplied machine ID, so alts + spoofed IDs repeat trials. | GM2-010 | Claude + owner policy (account age, phone-verified) |
| M-21 | A warm "kill disabled" cache defeats a later owner kill during a selective outage. | CX-013 | Claude |
| M-22 | The audit attempt guard covers only some destructive routes. | CX-014 | Claude |
| M-23 | **Stripe lifecycle.** The modern Charge → Invoice mapping acknowledges without revoking, and replay is not idempotent. No dispute-won handler. Interrupted order claims stay pending. Monthly is modelled as 30 days. | CX-002, GM2-022, CX-017, CX-016/GM2-018 | Claude |

## MEDIUM

| # | Finding | Sources | Owner |
|---|---|---|---|
| M-24 | The onset-FF env knobs, including `ORION_ONSET_FF_AB`, are live in production builds. Trivial `#ifndef` fix. | GM2-006 (rated critical), CX P2-01/P6-02, CL2-P6-006 | Claude |
| M-25 | **Learner fences.** Displaced shots still train 4 learners. The fences drop with FF gain 0. The sweep is not fenced from the trim. There is no size floor, so ~45% of evidence is discarded. Late-phase recovery is suppressed under FF. | CL2-P6-001..004, GM2-013 | Claude |
| M-26 | Calibration Cancel does not restore the zero/auto provenance. | GM-006/CX-007/CX-020, CX P6-01 | Claude |
| M-27 | The calibration's 150 ms lead floor may block fast Remote Play setups. | GM2-014 | Claude (verify with RP data) |
| M-28 | Sender-thread exits lack terminal publication. | CL-005, CX P4-01 | Claude (fork) |
| M-29 | **Shot records are hard-coded to `D:\NexusVision`**, ON by default, fsync every shot, and have no cap. | CL2-P5-003 | Claude (**launch-critical**: most customers have no D:) |
| M-30 | **Performance.** Synchronous user log on the GUI thread with no rotation; a full security evaluation every second plus ~90 property notifies; log volume and retention. | CL2-P5-002/004/005 | Claude |
| M-31 | Fork teardown race on console end; per-frame logging on the input lock (RP-only). | CL2-P2-002/004 | Claude (fork) |
| M-32 | The keep-alive is a second producer; SHOT_RELEASE bypasses the no-copy rule. | CL2-P4-005/006 | Claude (fork) |
| M-33 | **Capture.** Bare-index storage; a second card adopted by name; a degraded fps passes health; no plain errors; untested MJPG/4K/HDR/30 Hz. | CL2-P3-003..007 | Claude + owner (cheap dongle) |
| M-34 | A PS5 DHCP move is not propagated to Meter Delay / the RTT probe. | CL2-P8-005, GM2-020 | Claude |
| M-35 | **Clocks.** Heartbeat replay / wall-clock lease; resume latches SAFE MODE; a backward step blinds vision; raw `timestamp_expired`. | GM2-016, CL2-P8-003/004/006, GM2-023, CX P8-01 | Claude (nonce + monotonic lease fix agreed) |
| M-36 | A crash between the settings save and its signature locks production with an opaque reason. | CL2-P8-007 | Claude |
| M-37 | The admin metrics endpoint does a full-table scan. | GM2-019 | Claude |
| M-38 | Multi-device licences are a single mutable binding. | CX P8-02 | Only if multi-device is sold |
| M-39 | The frame pipe feeds unverified frames to detection. | CL2-P7-002 | Claude |
| M-40 | **Forensics.** Shot-record onset/pickup contamination (live unaffected; the late-carry claim re-verified). The range reader is dead (1,608/1,608 unknown). | CL2-P1-002/003 | Astra |
| M-41 | The regression corpus is missing or stale. **Framedumps: 12 sessions / ~23 GB on D: with no backup.** The banner reader is template-bound. | CL2-P9-006/007 | **Owner: back up**; Claude |

## LOW

| Lane | IDs |
|---|---|
| Claude | CL2-P1-004; P2-005/P4-008 (normal stop logged fatal); P2-006; P3-008..010; P4-007, 009; P5-006..010; P6-005, 007; P7-005; P8-008..010; P9-008 |
| Codex | CX P7-01 (preview shm ACL implicit) |
| Gemini | GM2-001 (downgraded, see above); GM2-009 (downgraded, owner policy GM-005); GM2-024 / CX-019 (IPs and identifiers in logs); GM2-025 / GM-031 (the settings signature is locally forgeable); GM2-026 (manifest fragility) |

## Owner decisions and actions

1. **Back up the framedumps** (M-41).
2. The Remote-Play-only launch claim, plus the logged retest (M-17).
3. **Policy calls:**
   - staff-visible keys (GM-005 / GM2-009);
   - the trial-abuse policy (M-20);
   - telemetry and privacy wording (M-13);
   - whether multi-device licences are sold (M-38);
   - No-Meter (CL-008);
   - partial refunds.
4. StrictSecurity on the rebuilt unit (M-01), then deploys on your word.
5. The VM install checklist (P10), after the rebuild.

## Patch order

| Batch | IDs | Theme |
|---|---|---|
| **1** | M-03, M-04, M-10, M-11, M-28, M-08, M-02 | Input and liveness |
| **2** | M-13, M-09, M-15, M-29, M-24 | Silent failures + quick wins |
| **3** | M-05, M-06, M-07, M-39 | Local trust |
| **3** | M-12, M-26, M-25, M-35 | Persistence and timing |
| **3** | M-18, M-19, M-21, M-22, M-23, M-20 | Backend and money |
| **4** | M-16, M-40 | With Astra |
| **Rebuild** | M-01, M-14 | Package + update path |

Then: Codex security pass on L1–L5 + re-gate → StrictSecurity → deploy → external round.

Status legend (update as fixes land): open / patched / verified (Codex).

## Batch 1 progress (2026-09-22 evening)

**Status: launcher (native) half PATCHED.**
- Build: 19:24 local, OrionNative sha256 `9A1314F52644B62E…`.
- ctest: 30/30. OrionInputProtocolTests: 51/51. OrderedFileLogSinkTests: 6/6.
- Not deployed, not committed. Awaiting Codex verification.

| Item | Change | Test |
|---|---|---|
| M-08 single instance | `main.cpp`: every second launch now activates the verified running "Venice" window and exits. It shows a message if the window is not found; previously this happened only for deep links. | manual (no harness) |
| M-10 R2 regression (CL2-P4-002) | `shotInputControlsNeutral` is back to Square + right stick. The trigger requirement moves ONLY into `routeRecoveryShotInputsNeutral` (ControllerRoutingPolicy.h). | `triggerHeldBlocksOnlyRouteRecovery` |
| M-10 CL-003 | The split `pre` is now built from the last CONFIRMED packet plus terminal edges only (trigger zero-crossings, digital releases). The split runs whenever `p` would not be copyable (a new press OR a trigger increase). A release carrying only stick motion is not split. | `triggerReleaseSplitCarriesOnlyTerminalEdges` (R2-up+L2-rise+Square, L2-up+R2-rise, R2-up+stick; inspects the wire packets) |
| M-04 dead-man, launcher half | An owned, unchanged state re-sends the confirmed state UNFLAGGED every ≥100 ms (`kOwnedKeepaliveMs`) from the input tick itself. `Input tick health: gap_max_ms=… owned_keepalives=… abandons=… split_trigger_releases=…` is logged once per heartbeat. | `ownedUnchangedStateSendsUnflaggedKeepalive` |
| M-03 launcher 25 ms kill (CL2-P2-003) | Before closing on its own ACK timeout, the client sends one unacknowledged `OrionInputAbandon` (0x08) packet: seq = the abandoned packet's, body = the last confirmed state, so an older fork sees an equal-state re-assert. | covered with the fork half |
| M-02 log sink (CX-015) | Admission waits ≤ `admissionWaitMs` (250) while storage is healthy and not at all while the writer is failing (it drops and counts). The writer abandons retries `shutdownGiveUpMs` (2000) after stop. New stats: `storageFault`, `storageFaultEpisodes`, `dropped{Batches,Lines,Bytes}`. | `failingStorageNeverBlocksProducerOrShutdown` |
| M-11 CL-006 | `RemotePlaySession` ages `frameAgeMs` / `pixelAgeMs` / `transportAgeMs` to at least the telemetry silence once it exceeds 1000 ms. Telemetry runs at ≥ 60 Hz. Detection fails closed; the existing transport watchdog restarts at 8 s. Missing-before-first-record ages nothing. | no event-loop fixture yet (**owed**) |

**Owed:**
- **The fork half** (agent): CL-001 bounded soft fault + checked transitions + neutral; CL-005 sender exits; the dead-man 400 ms neutralise; the ABANDON soft path.
- A CL-006 event-loop test.
- The thread-priority A/B (CL2-P5-001), after the gap telemetry has data.

**Fork half PATCHED (agent; re-verified by Claude).**
- Tests: fork unit 162/162 (147 + 15 new), re-run independently, same result.
- Deployed locally at 2026-09-23T00:35Z: `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` sha256 `d38dc7c9…`. Backup `OrionStream.exe.bak-*-7786742a…`; DEPLOY_LOG entry written.
- **CL-001**
  - On timeout: drop the unsent work, queue ONE owned neutral, write the Failed ACK. Any failure in that transition escalates to neutral, then the existing teardown.
  - Budget: `SOFT_FAULT_MAX` 3 per 10 s, `SOFT_HOLD_MAX_MS` 2000, then the terminal action.
  - The keep-alive resend is off during a fault.
  - The release's twin and echo survive a soft drop (CL2-P4-003).
- **CL-005:** every sender exit publishes should_stop/dead and wakes waiters with CANCELED.
- **CL2-P4-001:** 400 ms of owner silence produces one owned neutral and one `owner_silent_neutralized` line. The pipe is overlapped with 50 ms slices.
- **CL2-P2-003:** an ABANDON packet is not enqueued or ACKed and logs `launcher_abandon`. The next disconnect is soft; without an abandon it stays fatal.
- **Untested:** the Windows pipe I/O itself (only the shared policy is unit-tested). A live check of the soft, dead-man and abandon paths is owed: owner rig + Codex.

## Reliability batch (2026-09-23 early, owner goal: fewer earlies AND lates than any competitor)

- **Scoreboard:** `tools/timing/scoreboard.py`, read-only.
  - Baseline over 41 sessions (7 days, experiments excluded): pooled EXC 64.8 %, EARLY 16.0 %, LATE 19.3 %; median session 65.1 %.
- **CL2-P6-004 PATCHED:** `OnsetFeedforward` applies nothing below 2 ms (`kMinAppliedMs`, was 0.05). Those shots fly undisplaced and train the latency marker, trim, oracle and phase. Unit case added.
- **CL2-P6-003 PATCHED:** with the dev offset hook armed, banner verdicts are refused (`BANNER TRIM: ignored reason=dev_offset`).
- **Left fade −6 ms** (`ORION_LEFT_FADE_LATER`): default and a one-time migration of the persisted +8; `leftFadeOffsetMigratesOldDefaultOnce`.
- **Pill style switch mid-session:** restarts detection.
- **Build/test:** pending the owner's session end (the exe is in use).
- **Pill (beta) RE-WITHDRAWN (2026-09-23, owner: "performed terribly in a real game").**
  - The re-enable is fully reverted.
  - The mid-session style-switch restart is removed: it disconnected Venice for ~14 s.
  - A persisted "Pill" loads as Arrow2.
