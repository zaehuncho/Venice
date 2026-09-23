You are Codex, Venice's (internal name Orion) release-security owner and authorized internal tester. **Run this only after `docs/audit/2026-09-23/FIXUP_REPORT.md` exists and Claude has rebuilt one coherent unit**: launcher + sidecar + fork, packaged. The owner will tell you the package path.

This is the **final internal gate before the paid beta**, a defensive review of systems the owner owns and runs:
- the Venice launcher, updater, installer and release package;
- the licence backend (Lambda / API Gateway);
- the website Worker;
- the Discord bot;
- the local Chiaki fork.

Test against a **copy** of the release package, never the dev tree or the owner's real install. Do not contact PSN, 2K, or any third-party service.

Repos:
- `C:\Users\aaron\Desktop\NexusVision`
- `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`)

Never use ProjectReplay.

## Scope

1. **Re-verify every fix in FIXUP_REPORT.md** against the *packaged* bytes, not the source:
   - hash the binaries in the package and match them to the tested build;
   - re-run each finding's closure test;
   - try each finding's original failure path again.
2. **Re-check the open master items:** M-01 (package = revised unit), M-14 (elevated update path, end to end) and anything else still open in `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.md`.
3. **Release integrity:**
   - release_manifest covers every runtime file;
   - no lab/test/tool artifacts or secrets in the package;
   - Ed25519-only update verification;
   - SHA-256 match;
   - no downgrade;
   - safe ZIP paths;
   - rollback works on a disposable install;
   - the launcher starts from a clean, renamed folder.
4. **Licence/auth fail-closed:**
   - tampered cache and settings;
   - nonce/timestamp replay;
   - revoked, expired or killed keys;
   - global kill switch;
   - device mismatch;
   - entitlement-lookup outage: must not kick a paying user, and must not unlock anyone either.
5. **Input path under stress** (real pipe harness plus the packaged fork):
   - release re-send;
   - ABANDON bounded;
   - dead-man neutral;
   - clean Disconnect on close;
   - exit classification.
6. **Customer failure states:** each one must show a clear message and never leave a stuck button:
   - capture device missing, or the wrong one;
   - Remote Play busy;
   - licence reconnecting;
   - detection unavailable.

## Limits

- No commits, no deploys, and no live Stripe/Discord/AWS/Cloudflare changes. Read-only calls only, and only if the owner agrees.
- Never run `register_commands.py`.
- Never launch `OrionNative.exe` directly, never force-kill Venice, and never touch the owner's settings.json.
- Do not edit product source. Findings go in the report; Claude patches them.
- No secrets in output. Do not commit or leave any test artifacts in the repo or the package.

## Output

Write `docs/redteam/2026-09-23-final/RED_TEAM_REPORT.codex.final.md`.

**Verdict: approved / needs changes / blocked.** Then, for every finding:
- ID and severity;
- path;
- affected file/endpoint/package;
- impact;
- high-level reproduction;
- required fix;
- verification test.

Give a clear list of **release blockers**, kept separate from post-launch items.
