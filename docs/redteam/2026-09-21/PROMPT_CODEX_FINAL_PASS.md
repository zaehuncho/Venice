# Codex — final gate on the 2026-09-22 red-team patch (Claude lane)

You are Orion's release-integrity approver (AGENTS.md). Three red-team reports and one merged lane
tracker exist in `docs/redteam/2026-09-21/` (`RED_TEAM_REPORT.{claude,codex,gemini}.md`,
`RED_TEAM_LANES.md`). Claude has PATCHED its lane; the exact table of what changed, why, and which
tests prove it is `RED_TEAM_REPORT.claude.md` § "PATCH APPLIED". Your job is not to re-survey the
product; it is to decide whether this patch is safe to hand to the owner's rig test and then to the
external red team. Verdict: approved / needs changes / blocked, per finding id.

## Trees and identity
- NexusVision @ 0eca7d2 + uncommitted. Patch files: backend/lambda_function.py, website/src/worker.js,
  discord_launch/orion_worker.js, native_orion/src/{AppConfig.h,AppConfig.cpp,OrionAppController.h,
  OrionAppController.cpp,OrionInputClient.h,OrionInputClient.cpp,ShotIntentPolicy.h,SidecarWatchdog.h},
  native_orion/tests/{AutomationEngineTests.cpp,OrionInputProtocolTests.cpp}. Grep the markers
  `[2026-09-22 RED TEAM` / `[2026-09-22 GM-` / `[CX-0` to find every hunk.
- chiaki-ng-src @ c7515213 + uncommitted (Astra's affinity/clock/expedite/chord-tests AND Claude's
  bridge/drop/echo-shed). Claude's hunks carry `[2026-09-22 RED TEAM CL-00x]`.
- Deployed OrionStream.exe sha256 7786742a9260650a == build-orion-optimized-ffmpeg7/gui/OrionStream.exe.
  Prove that yourself before reading anything into a log; the previous incident was a source fix
  that never reached the binary. Launcher, Lambda and Workers are built/edited but NOT deployed.

## Re-run everything yourself (do not trust the numbers in the report)
fork: build `build-orion-unit-ffmpeg7` target chiaki-unit with PATH=/c/msys64/mingw64/bin first;
expect 147/147. native: `ctest --test-dir native_orion/build -C Release` expect 30/30 (do NOT rebuild
if a launcher is running). backend: `pytest tests/backend` (360) and `pytest
tests/test_backend_staff_auth.py` (17) in SEPARATE invocations, `-p no:cacheprovider`. website:
`python tests/verify_site.py public/index.html` and `node --test tests/worker.test.mjs` from
website/ (38). CLI: `pytest tests/test_orion_admin.py` (76).

## Adversarial questions — answer each with file:line and a test or a refutation
1. `chiaki_session_drop_orion_transport` + the bridge's soft-fault path: is there ANY sequence where
   physical passthrough is unmasked over a stale bot command (owning_ latch, `orion_ownership_active`,
   the launcher's forced-seed `own` flag)? Can a soft fault loop (timeout → Failed ACK → reconnect →
   timeout) starve the human of input longer than the old session kill did?
2. CANCELED vs TIMEOUT: confirm a dead/aborted sender still ends the session (fatal path) and that a
   `wait_orion_delivery` timeout can never be misreported as a mismatch or vice versa.
3. The trigger-release split in `OrionInputClient::sendDetailed`: two MustDeliver transactions per
   tick — can the second be lost/reordered, can `last_` desync from OrionStream if the first succeeds
   and the second fails, does the fork's `is_pure_button_release` actually copy the first packet?
4. `shouldAutoRecoverInputFromTelemetry` now ignores MISSING evidence: if the sidecar goes silent
   entirely (no telemetry at all), what other liveness path recovers the input link? If none, say so.
5. Kill switch: with `_LAST_GLOBAL_KILL` per container, can a stale "disabled" survive across an
   owner's kill + a DynamoDB outage? Is fail-closed on a cold container acceptable for /api/activate
   and heartbeat (lockout blast radius), and what grace does the owner need to decide?
6. `<action>.attempt` audit rows: does anything downstream (metrics, audit UI, tests, the owner
   tool's audit page) mis-count or mis-render them? Is `audit_attempt_or_refuse` on every destructive
   path (licence actions, config.set, kill) and on none of the read paths?
7. Worker refund/dispute: idempotency on replay, partial-refund handling, identity resolution
   (charge → invoice → subscription → discord id) failure modes, and that `kind: "disputed"` maps to a
   revoke the backend accepts. Confirm no test-mode event can revoke a live entitlement.
8. learning.json last-good: `.bak` promotion only from a VALID file; quarantine naming collisions;
   profile-specific learning files; the note surfacing once. Can a corrupt `.bak` ever be restored?
9. Anything in the patch that widens surface, weakens a fail-closed gate, or contradicts
   AGENTS.md's release blockers. Anything Astra's uncommitted fork changes and Claude's now conflict on.

## Output
`docs/redteam/2026-09-21/RED_TEAM_REPORT.codex.final.md` in the shared schema: per-id verdict
(approved / needs changes / blocked) with the exact reason, then an overall verdict for handing the
build to the owner's rig test, then what the EXTERNAL red team should attack first on the repackaged
build. Do not edit source. Do not deploy. Do not print secrets.
