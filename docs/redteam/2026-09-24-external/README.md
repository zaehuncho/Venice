# External (black-box) AI red team: Venice paid beta

**Preparation snapshot (2026-09-24): prompt files are ready, dispatch is pending.**
The previously frozen candidate still passes its recorded signed-inventory check,
but its installer is not Authenticode-signed and it is not the final protected
deployment unit. Its candidate sheet does not record a StrictSecurity run, an
installed VM canary, or an installed play test. Freeze and verify a new exact
installer/package/update-manifest set before giving any prompt to a tester.
This README is for the owner; do **not** give it or the repository to testers.

For each independent tester, supply only that tester's prompt and this filled
candidate card in the VM/shared channel (never passwords or signing material):

```
INSTALLER_PATH=<path inside disposable VM>
CANDIDATE_SHA256=<SHA-256 of those exact installer bytes>
PACKAGE_SHA256=<SHA-256 of the exact update ZIP, if supplied>
TEST_LICENSE_LABEL=<throwaway test account identifier, not the licence secret>
LIVE_ENDPOINT_WINDOW=<approved UTC window or OFFLINE_ONLY>
REPORT_DROP=<VM shared-folder path>
```

Before dispatch, record the matching final source/build identity privately,
run Standard and StrictSecurity on the final candidate, verify the signed
manifest and complete package inventory, verify installer Authenticode status,
and complete the installed VM/play canary. A changed installer or ZIP hash
requires a new candidate card and affected re-tests. Give each AI a fresh VM
snapshot; serialize any live endpoint work and record its UTC window.

**When:** after the final internal round is patched and the **frozen beta candidate (rc5: package + Venice installer)** exists. Not before. The owner moved packing to the first update (2026-09-23), so the beta candidate is unpacked and the installer is unsigned by decision; testers should still report anything those choices expose.

**What "external" means:** each AI acts as an outside attacker. It has **no repo, no source, no docs from this repo, no memory of the internal findings**. It has only what a customer or cracker would have:
- the installer;
- the installed Venice folder inside the test VM;
- the public website (zaeorion.com);
- the Discord bot in the owner's server;
- the network traffic of its own test install.

**Setup (owner):**
1. Copy the release installer into the VM. Take a **VM snapshot "clean-before-install"** first, so every run starts from a clean machine.
2. Create **one throwaway test licence** / Discord test account for the testers, revocable from the admin panel. Never a real customer's account.
3. Give each AI **only** its prompt below, plus the installer path inside the VM. Codex and Claude sessions for this round should be started **outside the NexusVision repo** (e.g. a scratch folder), so they cannot read source.
4. Keep the admin panel open to revoke the test licence and to watch backend logs while they test.

**Prompts:**
- `PROMPT_EXTERNAL_CODEX.md`: reverse engineering and tamper resistance of the installed product.
- `PROMPT_EXTERNAL_GEMINI.md`: purchase, website, Discord and public-surface attack path.
- `PROMPT_EXTERNAL_CLAUDE.md`: licence/API abuse and runtime tampering.

**Output:** each writes its report into the VM's shared folder (or pastes it back to the owner). Claude then merges the reports into `docs/redteam/2026-09-24-external/EXTERNAL_REPORT.md`, verifies each finding against the source (white-box), and patches. **Any unlock without valid auth, and any way to install a tampered update, is a release blocker.**

**Common ground rules (in every prompt):**
- Only the owner's systems: the VM install, zaeorion.com, the owner's API endpoints and the owner's Discord bot. **Never PSN, 2K, Stripe's own systems, Cloudflare/AWS infrastructure itself, or other users.**
- The throwaway test licence only. No real customer data.
- No load, flooding or DoS: at most a handful of requests to demonstrate a rate limit or replay.
- No persistence, malware or credential theft. Nothing is published or shared. Tooling and patched binaries stay inside the VM and are deleted when the VM is reverted.
- Never print secrets you find. Report *where* they are, not their values.
