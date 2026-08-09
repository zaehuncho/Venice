# PICKUP PROMPT — orion-activate edge-auth 403 fix + redeploy, paste this after /clear

Continuing the Gumroad storefront migration. Full context, in order of authority:
`C:\Users\Administrator\.claude\plans\mellow-conjuring-lampson.md` (approved plan),
`discord_launch/GUMROAD_MIGRATION_PICKUP_PROMPT.md` (prior phase — webhook Lambda build/deploy,
now complete), `discord_launch/LAMBDA_RECONCILIATION.md` (why `backend/lambda_function.py` looks
like a huge diff vs. the last commit — it was intentionally replaced with the verbatim deployed
bundle as baseline, then patched).

## Binding process rule
Do NOT delegate this workstream to the Agent tool (Plan/Explore/general-purpose/etc.) — the user
explicitly rejected that earlier and asked me to do all design + implementation myself, directly.
This still applies.

## Where things stand
Sequence steps 1–5 of the plan are done: webhook Lambda built, deployed, wired; all 8 Gumroad
products created; Ping endpoint URL cutover already done (confirmed — a real Gumroad webhook has
already fired against production). Step 6 (test-purchase verification) uncovered a live bug:

**The bug**: `orion-activate`'s router calls `require_edge_auth(headers)` unconditionally before
path dispatch. Neither the SellHub webhook Lambda nor the new Gumroad webhook Lambda sends the
`X-Edge-Auth` header they only send `X-Orion-Bot-Secret`, which is checked by a separate,
already-adequate `require_bot()` gate inside each `/api/bot/*` handler. Result: a real `test=true`
Gumroad purchase's webhook passes its own security checks, calls `/api/bot/provision`, and gets a
403 from the edge-auth gate before ever reaching `require_bot()`.

**The fix** (user-authorized: "Deploy all three together (Recommended)"): exempt `/api/bot/*`
paths from the router-level edge-auth check. This ships bundled with two other already-authorized
changes to the same file (from `LAMBDA_RECONCILIATION.md`'s reconciliation): a trial-abuse guard
(blocks a trial license rebinding to a second machine) and a fail-closed `issue_staff_token()` (no
more silent fallback to a hardcoded signing secret on an SSM read failure). All three are already
correct in **the local file on disk**, `backend/lambda_function.py` — confirmed by direct read,
1355 lines / 57606 bytes. Do not re-derive or re-diff this; it's settled. The user's authorization
to deploy all three together is standing and current — no new permission needed to redeploy.

**Deploy status — partial**: an earlier paste-into-Monaco-and-Deploy action on the live
`orion-activate` Lambda only partially persisted. Confirmed via the AWS Console Monaco editor's
Find widget: the trial-abuse guard (search `TRIALMACHINE`) and the fail-closed staff secret
(search `fallback-staff-secret-change-me` → correctly returns **no results** live) both persisted.
The edge-auth exemption did NOT persist — searching `startswith("/api/bot/")` returns **no
results** live, even though it's present in the local file. Function overview "Last modified" is
stale (reads as if from the earlier partial deploy, not a fresh one).

## Why this needs staged chunk files instead of a direct clipboard write
The file is ~57.6KB / 1355 lines — too large for one `javascript_tool` clipboard write to embed
as a literal JS string. A first attempt at JSON-string-escaping (`lambda_json.txt`, a 68045-char
single line) could not be fully re-read into context (the Read tool truncates around 25000 tokens
and can't paginate a single-line file). A first mitigation — splitting that JSON-encoded string
into 5 fixed-size chunk files — was abandoned before use: a chunk boundary could land mid-escape
(e.g. splitting `\n` into a trailing `\` + leading `n`), which would corrupt a naive per-chunk
`"..."`-quoted JS reconstruction (an unescaped trailing backslash would escape the closing quote).

**Adopted fix**: re-encode via **base64** instead. Base64's alphabet (`[A-Za-z0-9+/=]`) needs zero
escaping and is safe to split at any arbitrary byte boundary. Already done — 6 chunk files exist on
disk at
`C:\Users\ADMINI~1\AppData\Local\Temp\claude\C--Users-Administrator-Desktop-NexusVision\42ff92f1-c9d4-402b-b9b8-0f80ae929fe2\scratchpad\`:
`lambda_b64_0.txt` through `lambda_b64_5.txt` (each ≤15000 ASCII chars, 76808 total, encoding the
57606 raw bytes of the local `backend/lambda_function.py`). **If this scratchpad path no longer
exists (new session tempdir), redo the encode from disk**:
```python
import base64
with open(r'C:\Users\Administrator\Desktop\NexusVision\backend\lambda_function.py', 'rb') as f:
    raw = f.read()
b64 = base64.b64encode(raw).decode('ascii')
chunk_size = 15000
chunks = [b64[i:i+chunk_size] for i in range(0, len(b64), chunk_size)]
# write each chunk to its own file in the current session's scratchpad dir
```

## Immediate next steps
1. Call `tabs_context_mcp` fresh — do not reuse any numeric tab ID from a prior session. Find or
   reopen the `orion-activate` Lambda Console Code tab (signed in as "Isaiah (987622176566)").
2. Read each `lambda_b64_N.txt` chunk (0 through 5) from the scratchpad path above.
3. Via sequential `javascript_tool` calls on that tab, accumulate them into a page variable:
   `window.__lambdaB64 = (window.__lambdaB64 || "") + "<chunk>"`, one call per chunk.
4. Final call — decode and stage the clipboard (use `TextDecoder`, not the deprecated
   `escape`/`unescape` trick, since the file has literal non-ASCII box-drawing characters):
   ```js
   const bytes = Uint8Array.from(atob(window.__lambdaB64), c => c.charCodeAt(0));
   const text = new TextDecoder('utf-8').decode(bytes);
   navigator.clipboard.writeText(text);
   ```
5. Click directly into the Monaco editor body first (to deselect any page-wide selection), Ctrl+A,
   Ctrl+V to paste.
6. Screenshot to confirm a clean, complete paste (matches the local file's known start/end).
7. Click "Deploy (Ctrl+Shift+U)" to persist. Covered by the standing "Deploy all three together"
   authorization — no new permission needed.

## After redeploying — reverify from scratch
Use the reliable Find-widget pattern: click into the editor body first, then triple-click directly
on the Find input's existing text (not just near it) to select-all within that input, type the new
term, press Return; if a screenshot looks stale, `zoom` on the find-widget region.
- Search `startswith("/api/bot/")` — should now return a match (previously: none).
- Search `TRIALMACHINE` — should still return 1 match (regression check).
- Search `fallback-staff-secret-change-me` — should still return no results (regression check).
- Function overview "Last modified" should show a genuinely fresh timestamp.
- Fallback/independent corroboration: Versions tab for a new `$LATEST` revision timestamp.

## Then — resume the test/verification sequence toward "bot online"
1. Re-run the Gumroad test-purchase verification: confirm CloudWatch shows `AUDIT
   gumroad_issue_ok` (no more 403), Discord DM with license embed arrives, 💎 Customer role added.
2. Refund that test sale from the Gumroad dashboard → re-fires the Ping with `refunded=true` →
   expect CloudWatch `AUDIT gumroad_revoke_ok`, role removed, license revoked.
3. Reconcile: Gumroad sales dashboard shows 0 sales despite the test purchase (working theory,
   unconfirmed — `test=true` sales may not count toward dashboard tallies; just confirm, don't fix).
4. Investigate: only 6 of the planned 8 product tiers show on the Gumroad Products dashboard
   listing (3-Day and 14-Day are missing) — root cause not yet looked into.
5. Ask the user (don't act unilaterally) whether to add a log line on the un-logged
   `duplicate_event`/idempotency-skip branch in the Gumroad webhook Lambda — this is a Lambda code
   change and must be shown before any live redeploy, per standing process.
6. Double check "Generate a unique license key per sale" is toggled on for all 8 Gumroad products
   (flagged earlier, not yet independently re-verified this phase).
7. Ask the user whether Beta Access should be re-unpublished (optional, not mandatory).
8. Deliver the final report in the plan's A–H shape (same format as
   `discord_launch/SELLHUB_DISCORD_LINK_PROMPT_CURRENT.md`), adapted for Gumroad — include the
   `/api/bot/provision` 403 finding, full diagnostic narrative, the three-part fix, and
   confirmation once live and reverified. This report is the signal that the bot is genuinely
   online end-to-end.

## Open items to flag, not decide
- SellHub parallel-vs-takedown decision.
- SellHub 0-stock bump (still nominally approved, low priority).
- Discord role-ID extraction — separate, unrelated thread.
- Cloudflare Worker naming mismatch (`orion-discord-bot` vs. docs' `license-redeem-proxy`) — not
  yet reconciled, don't touch without asking.

## Standing constraints that still apply
- Never modify IAM/access-control/permissions myself — hard system boundary, survives any user
  authorization; state the rule and hand it to the owner.
- Never touch a live webhook/endpoint URL without asking first, even under broad standing
  authorization (this Lambda code-body redeploy is different — it's on an already-reviewed,
  already-authorized diff, not a new endpoint/URL change).
- No real paid purchases; additive-only changes to shared infra.
