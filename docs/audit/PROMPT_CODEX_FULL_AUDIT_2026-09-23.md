You are Codex, doing a **full codebase audit of Venice** before its paid beta. You have hours; use them. Be thorough and verify everything against the actual code, tests and logs. The goals, in priority order:

1. **Bugs** that a paying customer could hit (wrong input, stuck buttons, disconnects, silent non-firing, crashes, hangs, data loss, confusing errors).
2. **Reliability and consistency** improvements, especially anything that reduces EARLY/LATE shots that the bot itself causes.
3. **Things that can be removed:** dead code, dormant experiments, unused flags and settings, stale docs and scripts, duplicate logic. The product should be smaller and clearer before launch.
4. **Simplifications** that reduce risk without changing behaviour.
5. **Test gaps**, where a real failure mode has no test.

## Scope and trees

- Repo: `C:\Users\aaron\Desktop\NexusVision` (dirty working tree; preserve it).
- Chiaki fork: `C:\Users\aaron\Desktop\chiaki-ng-src` (branch orion, dirty; preserve it).
- Never use ProjectReplay.

## Read first

- `AGENTS.md`
- `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.md` (master list M-01..M-41 plus progress)
- your own `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.codex.reliability.md`
- `docs/ROADMAP_POST_LAUNCH_2026-09.md`
- the memory of what was already measured: `docs/variance/*.md`

**Do not re-report** items already listed there unless you have new evidence; cite their IDs.

## Areas (write one report per area; work through all of them)

| # | Area | Main paths |
|---|---|---|
| A1 | Native timing engine | `native_orion/src/AutomationEngine.*`, `OnsetFeedforward.h`, `BannerLeadTrim.h`, related policy headers |
| A2 | Native controller, input client, UI | `OrionAppController.*`, `OrionInputClient.*`, `RemotePlaySession.*`, `native_orion/qml/` |
| A3 | Input path end to end, native + fork | `gui/src/orioninputbridge.cpp`, `lib/src/feedbacksender.c`, `lib/src/orioninput.c`, `gui/src/streamsession.cpp`. Include the open question below. |
| A4 | Python sidecar and orchestrator | `native_orion/backend/autogreen_sidecar.py`, `remote_play_orchestrator.py`, `capture_card_backend.py`, `shot_records.py`, `banner_verdict_live.py`. `simple_meter_reader.py` and `meter_locator_cv.py` belong to Astra: **report only, no diffs**. |
| A5 | Licence, backend, website, Discord | `LicenseClient.*`, `LeaseGate.*`, `backend/lambda_function.py`, `website/src/worker.js`, `discord_launch/` |
| A6 | Packaging, installer, updater, build scripts | `tools/package_orion_release.py`, `tools/release_filter_policy.py`, `installer/`, `scripts/`, the updater sources |
| A7 | Tests and tooling | Flaky tests (known: OrionShmInteropTests `abandonedPartialPublish…`), the ~30 pre-existing failing Python tests (list them and say which are stale versus real), tests that assert retired behaviour |
| A8 | Dead weight | Dormant flags and experiments (Pill route, No-Meter, press-anchored predictor, legacy backstop paths, dev hooks), unused settings keys, stale docs and scripts, large files. For each removal candidate: evidence it is unused, risk, and what must go with it. |

**Open question for A3:** a customer disconnect on 2026-09-23 at 02:28:59Z.
- OrionStream exited with code 1 mid-game. Its log (`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_2026-09-22_21-08-12-490490.log`) stops ~0.8 s earlier with no error.
- No Windows crash event was recorded.
- The owner had pressed Options at 02:28:57.3 and 02:28:59.47.
- The PS5 then refused three reconnects with "Remote is already in use" (0x80108b10) before a manual reconnect worked.
- The owner says **the same disconnect happened on earlier builds**.

Find every code path in the fork and the launcher that can end OrionStream with exit code 1 or stop the session without a clean Disconnect, and whether any is reachable from ordinary play: Options, menus, controller shortcuts, Qt key handling (`gui/src/qmlcontroller.cpp` maps controller buttons to keys), window focus, the session-stop dialog. Also say what logging would identify the cause next time (flush-on-exit, exit-reason line).

## Output

Write to `docs/audit/2026-09-23/` (create it):
- one file per area, `A1-engine.md` … `A8-dead-weight.md`;
- a `SUMMARY.md` ranking the top 20 items across all areas by customer impact.

Every item has this form:

```
### [AUD-Ax-NNN] severity (critical/high/medium/low) — title
- Kind: bug | reliability | removal | simplification | test-gap
- Evidence: file:line, log lines or test output
- Customer impact: what a paying user experiences
- Proposed change: minimal, as a unified diff when it is small (DO NOT APPLY IT)
- Verification: the test that proves it
- Confidence: confirmed / probable / speculative
```

## Rules

- **READ-ONLY on both trees.** Propose diffs in the reports; do not apply them. Claude is concurrently editing the fork (release re-send), `RemotePlaySession`, `OrionAppController`, `OrderedFileLogSink` and `OrionInputClient`.
- You may run existing test suites and read logs. Build only in a scratch copy outside the repo if you need a binary.
- **Do not** deploy, commit, launch Venice/OrionNative, or edit settings.json.
- Do not contact live services. No secrets in the reports.
- Re-check your own findings before writing them. A wrong "critical" costs more than a missed low.
