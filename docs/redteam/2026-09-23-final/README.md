# Final launch round: order of work (2026-09-23)

**Current final internal dispatch:** `FINAL_INTERNAL_REDTEAM_PROMPTS.md` supersedes the
three shorter prompt drafts below. Copy one self-contained agent section per task;
four independent reviews (two Codex, Gemini, Claude) must share one frozen-candidate sheet.

1. **Codex fix-up.** Send `docs/audit/PROMPT_CODEX_FIXUP_2026-09-23.md`. Claude does not edit during it. Output: `docs/audit/2026-09-23/FIXUP_REPORT.md`.
2. **Claude rebuilds one unit:**
   - launcher;
   - compiled sidecar;
   - fork, deployed locally with a backup;
   - release package.
3. **Final internal red team.** Four read-only runs, in parallel, using
   `FINAL_INTERNAL_REDTEAM_PROMPTS.md` and copies of the same frozen candidate:
   - Codex Security → `RED_TEAM_REPORT.codex.security.md`;
   - Codex Reliability → `RED_TEAM_REPORT.codex.reliability.md`;
   - Gemini → `RED_TEAM_REPORT.gemini.internal.md`;
   - Claude (F1–F8) → `RED_TEAM_REPORT.claude.internal.md`.
   The older individual prompt drafts remain as history, not the current dispatch.
4. **Claude merges and patches.** The merged report is `RED_TEAM_REPORT.md`. Release blockers are patched first.
5. **Owner play test** on the patched build, graded with the scoreboard.
6. **Launcher UI upgrade.** Based on Gemini's brief plus the owner's picks; Claude builds it.
7. **Venice-branded installer.** Wizard art, pages and copy.
8. **Pack and protect:**
   - package;
   - Lethe;
   - StrictSecurity (owner);
   - VM install/update/rollback canary;
   - Codex final sign-off on the exact package.
9. **Launch, on the owner's word:**
   - deploy the website Worker, Lambda and Discord embeds;
   - post the timing note;
   - Stripe live.
