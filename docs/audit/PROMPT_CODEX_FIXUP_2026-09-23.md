You are Codex, Venice's (internal name Orion) release-security owner. Your full audit (`docs/audit/2026-09-23/SUMMARY.md` and the eight area reports A1–A8) blocked the paid beta. **This task is to fix those findings yourself**, with tests, so the next round can re-gate one coherent build.

Repos (both uncommitted working trees, both yours to edit for this task):
- `C:\Users\aaron\Desktop\NexusVision` (launcher, sidecar, backend, website, installer, tools)
- `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`, the OrionStream fork)

Never use `C:\Users\Administrator\Desktop\ProjectReplay`.

**Claude is not editing either tree while you work** (the freeze your audit asked for). Since your audit, Claude changed only:
- `native_orion/qml/components/ShotLeadCard.qml`: the direction hint now wraps (layout overflow fix);
- a new test, `PreviewPresentationBufferTests::shotLeadCardFitsANarrowPanel`;
- `tools/timing/scoreboard.py`: an offline session is excluded.

The deployed `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` is the fork round-2 build, SHA-256 `397017fa…`. It was play-tested last night:
- 54 shots;
- 0 disconnects;
- `release_resend` lines fired;
- on close it logged `exit_path=orion_bridge_fatal owner_pipe_disconnected`, then `StreamConnection sending Disconnect`, with no final `Orion process_exit` line. On a normal close the launcher kills the child after the pipe drops. Take that into account for AUD-A3-001.

## What to fix, in this order

1. **AUD-A6-001:** updater self-lock and rollback lock. Run the verified helper and its full DLL closure from outside the install tree. Prove a full update, and an injected-failure rollback, on disposable copied install folders only. Also fix **A6-002** (prune retired runtime files using a verified old/new delta; user data is kept) and **A6-003** (stage package + ZIP + manifest, verify all three, then publish atomically).
2. **AUD-A5-002:** a failed entitlement lookup must return retriable "unavailable" without minting a lease. The existing bounded lease then expires safely, and a paying customer is never told they have no subscription. Also fix **A5-003** (persisted role-delivery state; a webhook replay repairs the role without re-minting or a duplicate DM) and **A5-004** (ordinary lease expiry is not labelled a clock failure).
3. **AUD-A2-003:** make ABANDON/cancel completion truly bounded: owned async buffers and reaping, with no `GetOverlappedResult(..., TRUE)` after cancellation. Inject a delayed cancellation and prove bounded caller progress and exactly one terminal result.
4. **AUD-A5-001:** in `website/src/worker.js` (GMC-001), status polling must not consume the pairing-code issue quota. Test a 40–90 s provisioning delay with a stateful quota and exactly one code issued. `worker.test.mjs` must stay green.
5. **AUD-A2-001 and A2-002:**
   - Refuse a live video-source change, or run it as an ordered disarm/stop/requalify.
   - Warm-preview promotion must follow the launch identity actually chosen.
   - Event-loop tests for both.
6. **AUD-A4-001:** index 0 / a generic "USB Video" device is never treated as the customer's explicit choice. Test fresh install, device reorder, explicit pick and webcam.
7. **AUD-A3-001 and A3-002:**
   - Parent-side (launcher/sidecar) record of the kill initiator: PID, creation time, session generation.
   - Classify clean, fault and forced exits in fake-child tests.
   - Pin a bounded abnormal-session log bundle before retries, so it survives more than 6 reconnect logs.
   - Also fix **A3-003** (the `20/3` seconds-vs-ms retry timer) and **A3-004** (suppress embedded-menu key routing in launcher-managed mode).
8. **AUD-A1-001 and A1-002:**
   - Clear queued oracle attribution at a generation/reset boundary.
   - Apply the ≥2 ms feedforward floor to the *final* displacement, after the trim cap.
   - Test paired learner markers.
   - A1-003 only if it is cheap and conditional-safe.
9. **AUD-A2-004:** Copy Log includes the redacted ring with a partial marker whenever a disk drain fails or times out. The fixture must include the newest injected error.
10. **AUD-A7-001, A7-002, A7-003, A7-004:**
    - Record full EXE/DLL/source hashes per native test run, and reject mixed units.
    - Fix the SHM abandoned-publish flake.
    - Make the six disk-dependent tests deterministic.
    - Add a default-deny egress guard to the broad Python suite, and mock the backend and console paths until **zero** non-loopback attempts remain.
11. **A4-002, A4-003:** only if safe. Ambiguous banner attribution becomes no-train/no-tally.

## Hard limits

- **Do not edit** `simple_meter_reader.py` or `meter_locator_cv.py`: that is Astra's lane. If a fix needs them, write the patch as a proposal in your report.
- Do not change shipped timing defaults. This includes:
  - left fade −6 ms;
  - FF gain, clamp and floor policy, except the A1-002 final-floor fix;
  - the Pill withdrawal;
  - capture fps policy.
- **No commits, no deploys** (Worker, Lambda, Discord, API Gateway), and no live Stripe, Discord, AWS or Cloudflare calls. Never run `register_commands.py`.
- **Never launch `OrionNative.exe` directly**, and never force-kill Venice. Never hand-edit or re-sign `%LOCALAPPDATA%\NexusVision\Orion Native\settings.json`.
- Do not replace the deployed OrionStream.exe. Build the fork in its own build dir and report the SHA-256.
- No secrets in output, logs, tests or reports.
- Every security-sensitive change fails closed.

## How to work

- Python: `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe`. The `C:\Python314` in AGENTS.md does not exist. Give pytest a forward-slash `--basetemp` outside the repo.
- Native: `cmake --build native_orion/build --config Release` from PowerShell. Git Bash mangles `/m`.
- Fork builds: prepend `C:\msys64\mingw64\bin` to PATH, or cc1 exits silently.
- For each finding:
  1. Write the failing test first and show that it fails.
  2. Fix it.
  3. Show that it passes.
  4. Where the audit asked for them, add the cross-component regressions.
- At the end, run the full native ctest set, the fork unit suite plus `orion-bridge-pipe-test`, the broad Python suite under the egress guard, `worker.test.mjs`, and the Standard verify:
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe`

  Leave StrictSecurity for the owner.

## Output

Write `docs/audit/2026-09-23/FIXUP_REPORT.md` with the following:
- For every AUD finding: **fixed / partial / deferred (why)**, the files changed, and the tests, marked as failing-before and passing-after.
- Final test totals, with any remaining failures and their cause.
- The builds the release needs: launcher, sidecar and fork, with their hashes. Also what must be rebuilt or repackaged.
- Anything that needs an owner decision or Astra.
- Your verdict on the source: **ready for re-gate / needs changes**. This is not a release approval. The final red-team round gates the rebuilt package.
