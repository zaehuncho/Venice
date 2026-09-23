# External (black-box) AI red team: Venice paid beta

**When:** after the final internal round is patched and the **real Lethe-protected package + Venice installer** exists. Not before.

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
