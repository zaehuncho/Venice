You are Codex, Venice's release-integrity reviewer. This task is a **consistency and reliability review and verification**. The owner is about to launch a paid beta, and his biggest fear is a customer deciding "it's broken" and refunding. The product goal is **fewer earlies and fewer lates than any competitor, session after session, and no moment where Venice looks broken.**

Repo: `C:\Users\aaron\Desktop\NexusVision` (dirty tree; preserve it). Chiaki fork: `C:\Users\aaron\Desktop\chiaki-ng-src` (branch orion, dirty; preserve it). Never use ProjectReplay.

## Read first

1. `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.md` — the master list (M-01..M-41), the verified severity changes, and the progress sections at the bottom (Batch 1 and the reliability batch).
2. `docs/redteam/2026-09-21/RED_TEAM_REPORT.codex.final.md` — your own final gate. Batch 1 targets its CL-001, CL-003, CL-005 and CL-006 items.
3. `docs/variance/SESSION_CENTRE_TRACKING_DESIGN_2026-09-22.md` and `docs/variance/OFFSET_SWEEP_RESULT_2026-09-22.md` — what the timing data says, including what was refuted.

## Part A — verify what was patched (code + tests; approve / needs changes / blocked per item)

**Launcher (native)** — built 2026-09-23, ctest 30/30:
- **M-08 single instance:** `main.cpp` activateRunningInstance.
- **R2 predicate split:** `ShotIntentPolicy.h` / `ControllerRoutingPolicy.h`; test `triggerHeldBlocksOnlyRouteRecovery`.
- **CL-003 `pre` packet:** built from the last confirmed packet plus terminal edges only (`OrionInputClient.cpp`); test `triggerReleaseSplitCarriesOnlyTerminalEdges`.
- **Owned keepalive:** every 100 ms (`kOwnedKeepaliveMs`); test `ownedUnchangedStateSendsUnflaggedKeepalive`.
- **ABANDON notice:** `OrionInputAbandon` 0x08 before a client-side ACK-timeout close.
- **Log sink admission/shutdown bounds:** `OrderedFileLogSink`; test `failingStorageNeverBlocksProducerOrShutdown`.
- **CL-006 silence-aged health values:** `RemotePlaySession.h`. **No event-loop test yet — say whether one is required.**
- **`Input tick health` log line.**
- **Left-fade offset:** default −6 ms, with a one-time migration of a persisted +8 (`AppConfig.*`); test `leftFadeOffsetMigratesOldDefaultOnce`.
- **CL2-P6-004:** feedforward minimum applied shift of 2 ms (`OnsetFeedforward.h`).
- **CL2-P6-003:** banner trim refuses verdicts while the dev offset hook is armed.
- **CL2-P6-006:** `ORION_ONSET_FF_*` env overrides and A/B arms compiled out of production.
- **CL2-P9-001:** a latched `detection_unavailable` gate stops blind backstop releases after 3 no-meter shots; it clears only after 2 owned shots. Test `meterBlindBackstopStandsDownWhileDetectionUnavailable`; new customer text in `meterBlindHint`.
- **Pill (beta) re-withdrawn:** also removes the style-switch sidecar restart that disconnected the session for ~14 s.

**Fork (OrionStream, deployed locally as sha256 `d38dc7c9…`)** — 162/162:
- **CL-001:** bounded soft faults (3 per 10 s, 2 s hold), a checked transition, and owned neutral.
- **CL-005:** terminal sender exits.
- **Dead-man:** neutral after 400 ms of owner silence.
- **ABANDON soft path.**
- **Twin/echo:** survive a soft drop.
- **The Windows pipe I/O paths are not unit-tested.** Assess the risk.

**Python:** `shot_records.py` defaults OFF in the compiled customer sidecar and falls back from D: to LOCALAPPDATA.

**Agents still in flight — review when present:**
- capture-card qualification by measurement, plus plain-language capture errors (CL2-P3-001/002/004/005);
- licence heartbeat retry/backoff, resume/network re-check, and plain licence error text (CL2-P8-002/006/008).

## Part B — consistency (fewer earlies AND lates)

- Baseline from `python tools/timing/scoreboard.py --days 8` (41 sessions, experiments excluded): EXC 64.8 %, EARLY 16.0 %, LATE 19.3 %; median session 65.1 %.
- Shot records: `D:\NexusVision\shot_records\*.jsonl`. Logs: `logs\orion_native.log(.1)`. Chiaki session logs: `%APPDATA%\Chiaki\Chiaki\log\`.

Find anything the **bot itself** does that makes timing less consistent than it has to be:
- a learner moving the wrong way;
- a fence discarding good evidence;
- a per-shot-type or per-range bias;
- a session-to-session step in release timing;
- a gate that fires on stale data.

Rules:
- Separate what is controllable from the online floor. The floor is the PS5→2K leg, which cannot be seen from the client; the network, PC and fire-to-wire legs were measured as ruled out on 09-22.
- Every claim needs a pre-fire mechanism and a test that could refute it.
- Mind two traps: the 09-22 offset-sweep sessions (`165506`, `172143`) contain deliberate displacements, and reader `onset_ms` is contaminated on bridged presses (CL2-P1-002).

## Part C — "looks broken" reports from the owner, 2026-09-23 (evidence, not conclusions)

1. **"Square gets stuck / holding Square doesn't let you shoot."**
   - Native audits show every post-release Square suppression ending at the physical release.
   - The `press_unanswered_no_meter` pass-through presses were delivered.
   - Find any path we missed.
2. **"Pressing Triangle and Square, X gets stuck."**
   - Chiaki log `chiaki_session_2026-09-22_21-08-12-490490.log`: the press at 21:17:48.499 local is btn 12 → 4 → 0 within 72 ms.
   - Cross was pressed 35 times this session. Every release was forwarded within 0.05–0.22 s with `redundant=1 redundant_history=1`.
   - A 13-tap Cross burst at 21:18:05–10 suggests the owner saw X held on the console.
   - Find any path by which the console could keep Cross (or any button) down that these logs would not show — e.g. the keep-alive second producer (CL2-P4-005), history-event ordering, or the new keepalive / split / abandon code.

## Rules

- **Do not** deploy, commit, launch Venice/OrionNative, or edit settings.json.
- **Do not** edit `simple_meter_reader.py` or `meter_locator_cv.py` (Astra's lane).
- You may run the existing test suites; write only your report.

**Report:** `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.codex.reliability.md`
- **Per item:** approved / needs changes / blocked, with file:line evidence and the verification test.
- **Part B:** a ranked list of consistency levers with expected effect and evidence strength.
- **Part C:** findings for each report.
- **End with** the top 5 fixes in priority order.
