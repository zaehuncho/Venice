You are Gemini, reviewing Venice (a paid NBA 2K27 shot-timing tool for PS5) before its beta launch. This is a **customer-experience reliability review**. The owner's biggest risk is a customer deciding "it's broken" and refunding. Your job: find every moment where a paying customer could reasonably conclude that, and say what they would see and what would fix it. Codex is separately verifying code fixes and timing consistency; do not duplicate that.

Repo: `C:\Users\aaron\Desktop\NexusVision` (read-only for you; write only your report). Never use ProjectReplay.

## Read first

- `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.md` — the master list and what has been patched (progress sections at the bottom).
- `docs/redteam/2026-09-22-launch/RED_TEAM_LANES.md` — rules.
- `discord_launch/launch_embeds/timing_expectations.json` (draft customer note), `discord_launch/launch_embeds/setup_guide.json`, `website/public/index.html`.

## Part A — correct your own report

`RED_TEAM_REPORT.gemini.md` (GM2-*) carried several items forward without re-checking the current code. Re-verify each GM2 finding against today's files and mark it **still open / fixed / downgraded**, with file:line. The merge already found:

| Finding | Correction |
|---|---|
| GM2-001 | The lease gate is forced ON in production (`LeaseGate::enabledFromEnvironment`) |
| GM2-002 | Conditional on a custom install folder |
| GM2-007 / 008 | Partly fixed on 09-21/22 |
| GM2-009 | Ephemeral staff-only reply |

Confirm or refute each.

## Part B — the customer journey, step by step

For each step below, list what can go wrong, **what the customer actually sees** (quote the exact UI or Discord text from the code), whether that text tells them what to do, and the fix:

1. **Buy / trial:** the website, Stripe, `/claim_trial` and `/purchase` in Discord. What happens if payment succeeds but the licence is delayed?
2. **Install:** the installer (`installer/orion.iss`); SmartScreen / antivirus; drivers (ViGEm, WinDivert); non-admin users.
3. **First run:** account pairing (the zaeorion.com/connect one-time code); the licence unlock and every licence error string.
4. **Setup:** the capture card or Remote-Play-only path (`native_orion/qml/pages/StreamSetupGate.qml`, `components/StreamSetupForm.qml`); PS5 discovery and wake; the controller.
5. **Playing:** meter detection states, the "no meter / detection unavailable" message, the Shot Lead calibration, what a customer sees when shots come back LATE or EARLY in streaks, and the timing expectations they have been given.
6. **Things going wrong mid-session:** a Wi-Fi drop, PC sleep, the PS5 changing IP, a second copy of Venice, a full disk, a 2K patch.
7. **Getting help:** is there a clear support path? Do the customer diagnostics or log exports contain what support needs, without exposing keys or personal data?

Prioritise by **refund risk**: silent failure > confusing message > cosmetic.

## Part C — launch-day readiness (blue team)

Check that a short runbook exists, and if not, draft its outline, for:
- a broken update (rollback);
- a meter-breaking 2K patch (pause and messaging);
- a licence server outage;
- a Stripe webhook failure;
- a leaked licence key or admin secret (rotation);
- a flood of "it doesn't work" tickets in the first hour (triage checklist).

Point to existing docs where they exist (`docs/`).

## Rules

- **Read-only.** Do not build, deploy, launch Venice, commit, or edit any file other than your report.
- No secrets in the report.
- **Do not** interact with PSN, 2K servers, Stripe, Discord or any third party.

**Report:** `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.gemini.customer.md`
- **Part A:** a table (GM2 id, current status, evidence).
- **Part B:** findings as `[GMC-NNN] severity — title` with journey step, what the customer sees, refund risk, fix, and verification.
- **Part C:** runbook status.
- **End with** the top 10 fixes ranked by refund risk.
