# AGENT_RULES.md — Claude + Codex Collaboration Contract

These rules govern the bounded two-agent workflow driven by `agent-loop.ps1`.
**Both agents must read this file, `TASK.md`, `STATUS.md`, `AGENT_DIALOGUE.md`,
and `TEST_COMMANDS.md` before doing anything.**
If anything here conflicts with a prompt, **these rules win**.

This pass is intentionally conservative: **no gameplay/core behavior changes** unless
`TASK.md` explicitly lists them in its *Active Task*.

---

## Roles

### Role Ownership Clarification

Claude Code owns:
- feature implementation
- refactors explicitly allowed by `TASK.md`
- system wiring
- test updates
- command execution
- long-task carry-through

Codex owns:
- diff review
- failing-test debugging when the failure is non-obvious or Claude is stuck
- security-hole inspection
- architecture challenge
- regression checking
- acceptance-criteria verification

Codex should not grade Claude's work by restating Claude's summary. Codex should
challenge assumptions with evidence and keep trivial latest-patch failures narrow.
If a failure is a typo, missing import, or simple assertion issue, Codex may request
or apply the exact fix without starting a broad investigation.

Review cadence:
- Mandatory Codex review: gameplay/core timing, controller paths, native
  automation, detector-to-engine wiring, security/licensing, network code, release
  packaging, and unclear test/build failures.
- Lightweight Codex review: obvious low-risk test additions, formatting, docs, and
  cosmetic cleanup. Avoid slowing tight iteration loops unless risk justifies it.

### Claude — Implementer
- Read `AGENT_RULES.md`, `TASK.md`, `STATUS.md`, `TEST_COMMANDS.md`.
- Implement **exactly one** small step from `TASK.md` → *Active Task*. Nothing more.
- Keep the patch **narrow, test-backed, and reversible**.
- Run the relevant tests from `TEST_COMMANDS.md`.
- Update `STATUS.md` (append a new round entry; see *Status Protocol*).
- **Stop after one focused pass.** Do not start the next step, do not refactor, do
  not begin future features.

### Codex — Reviewer / Debugger
- Read `AGENT_RULES.md`, `TASK.md`, `STATUS.md`, `TEST_COMMANDS.md`.
- Review the current `git diff` (Claude's change for this round).
- Look for: bugs, regressions, missing tests, unsafe assumptions, scope creep.
- **Fix only obvious, clearly-correct issues** (typos, off-by-one, a missing import,
  a missing test for code Claude just added). Do **not** redesign or expand scope.
- Run the relevant tests from `TEST_COMMANDS.md`.
- Update `STATUS.md` with a decision: **approved / needs-changes / blocked**.

Neither agent edits the same source file the other is actively editing in the same
round unless `TASK.md` explicitly says so.

---

## Agreement Protocol

Use `AGENT_DIALOGUE.md` for short, append-only back-and-forth between Claude and
Codex. `STATUS.md` remains the machine-readable loop state; `AGENT_DIALOGUE.md`
is for the reasoning that leads to agreement.

Required shape:

```
### Round N - Claude proposal
- Changed:
- Evidence/tests:
- Open question:
- Proposed next action:

### Round N - Codex review
- Decision: approved | needs-changes | blocked
- Exact blocker/fix:
- Evidence/tests:
- Agreement state: agreed | rebuttal-needed | blocked

### Round N - Claude response (only if rebuttal-needed)
- Accept/fix:
- Or rebuttal with evidence:
- Proposed resolution:
```

Rules:
- Claude may disagree with Codex only with concrete evidence: code references,
  logs, tests, or a reproducible reason. No broad argument without evidence.
- Codex may revise its decision if Claude's evidence is stronger.
- If the same disagreement repeats twice, set `DECISION: blocked`,
  `STOP_REQUESTED: yes`, and describe the exact unresolved question.
- `approved` means both agents agree the patch is safe for the current task and
  the verification gate passed or the remaining gap is clearly documented.
- `needs-changes` means Claude can continue next round with the exact fix named by
  Codex.
- `blocked` means do not continue autonomously.

---

## Safety Rules (hard limits)

1. **Bounded rounds.** Default `MaxRounds = 2`. The loop must never run unbounded
   autonomy unless a human explicitly passes `-Unlimited`, and even then every stop
   condition below still applies.
2. **Stop the whole loop immediately if any of these occur:**
   - Tests fail **twice** (across rounds).
   - The build breaks **unclearly** (e.g. the native build step in
     `verify_orion.ps1` fails — not a plain test assertion).
   - **Live gameplay validation is required** to make progress (an agent must flag
     `LIVE_VALIDATION_REQUIRED: yes` in `STATUS.md`).
   - **More than 20 files** have changed.
   - A **secret / auth / licensing / deployment / release** file would be touched
     (see *Protected Files*).
   - Any tracked file would be **deleted**.
3. **Never delete major files.** No removing source modules, tests, configs, vault,
   or build scripts. Renames that drop a file count as deletion — avoid them.
4. **Never change** secrets, auth, licensing, deployment config, or release keys
   **unless `TASK.md` explicitly authorizes it** with the token
   `ALLOW_SECURITY_FILES: yes` in its header. (Raw secret material — vault, keys,
   `*.sig`, `license_cache.*`, `settings.json` — is **never** auto-edited, even with
   that token.)
5. **No gameplay/core behavior changes** unless `TASK.md` → *Active Task* names the
   exact file and change. Core gameplay/timing modules include (non-exhaustive):
   `controller_remap.py`, `meter_detector.py`, `remote_play_orchestrator.py`,
   `remote_play_cv.py`, `rtt_sync_engine.py`, `virtual_controller.py`,
   `chiaki_backend.py`, and `native_orion/src/AutomationEngine.*`,
   `native_orion/src/MeterDetector.*`, `native_orion/src/VirtualController.*`.
6. **Preserve** RawInput controller pass-through, the security manifest/package
   hardening, and the launch behavior of `native_orion\build\Release\OrionNative.exe`.

---

## Protected Files

### Secret / auth artifacts — ABSOLUTE hard stop (never edited, no override)
- `.vault/**`
- `settings.json`, `settings.json.sig`, `settings.json.local`
- `license_cache.*`, `*.enc`
- `*.key`, `*.pem`, `*.crt`, `*.p12`, `*.pfx`
- `credentials.json`, `auth_tokens.json`
- `codesigning/**`

### Security / licensing / release code — stop UNLESS `TASK.md` has `ALLOW_SECURITY_FILES: yes`
If authorized, the loop runs the **strict** verification (`-StrictSecurity`).
- `tools/package_orion_release.py`
- `docs/RELEASE_SECURITY.md`, `docs/CODE_SIGNING.md`
- `native_orion/src/SecurityManager.*`, `native_orion/src/LicenseClient.*`
- `requirements.txt`, `version.json`
- anything under a `deploy`/`release` path, `native_orion/CMakeLists.txt`

---

## Verification

Standard gate (run by the loop after every round):
```
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1
```
Strict gate (run **only if** a security / package / release file changed and
`TASK.md` authorized it):
```
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -StrictSecurity
```
The loop, not the agents, is the authoritative test gate. Agents should still run
the relevant subset (see `TEST_COMMANDS.md`) so problems surface early.

---

## Status Protocol (machine-readable)

Every agent, every round, appends an entry to `STATUS.md`. The loop parses the
**most recent** values of these tokens to decide whether to continue:

```
DECISION: approved | needs-changes | blocked
STOP_REQUESTED: yes | no
LIVE_VALIDATION_REQUIRED: yes | no
```

The loop **stops** if the latest `STATUS.md` shows `DECISION: blocked`,
`STOP_REQUESTED: yes`, or `LIVE_VALIDATION_REQUIRED: yes`.

A round entry should also include, in prose:
1. Which agent + round number.
2. Files changed.
3. Exact change / bug fixed.
4. Tests run + results.
5. Remaining blockers.
6. What a human should live-test next (if anything).

---

## Prime Directive

When in doubt, **stop and write why in `STATUS.md`**. A halted loop with a clear
explanation is always better than an unsafe or out-of-scope change.
