# PICKUP PROMPT — Gumroad migration, paste this after /clear

Continue the Gumroad storefront migration (replacing SellHub). The full approved plan is at
`C:\Users\Administrator\.claude\plans\mellow-conjuring-lampson.md` — read it first for the
complete rationale, security model, and sequence. Key docs for the wider (storefront-agnostic)
context: `discord_launch/SELLHUB_DISCORD_LINK_PROMPT_CURRENT.md`,
`discord_launch/LAMBDA_RECONCILIATION.md`.

## Binding process rule
Do NOT delegate this workstream to the Agent tool (Plan/Explore/general-purpose/etc.) — the user
explicitly rejected that earlier and asked me to do all design + implementation myself, directly.
This still applies.

## What's already done (sequence steps 1–4 of the plan, all confirmed complete)
- `discord_launch/gumroad_webhook/lambda_function.py` is written and verified intact on disk
  (279 lines, mirrors the live `orion-sellhub-webhook`/`lambda_v6.py` pattern almost line-for-line).
- All 8 Gumroad products created (Lifetime/120/30/14/7/3/1-day + Beta) matching `PRODUCT_MAP` in
  that file.
- SSM `/orion/gumroad_webhook_token` = `<removed>` created (SecureString; rotate before launch).
- DynamoDB `orion-gumroad-events` (PK `event_key`) and `orion-gumroad-orders` (PK `sale_id`)
  created, both Active, on-demand.
- **Owner completed IAM + Lambda + API Gateway wiring** (I cannot do IAM/access-control work —
  hard system boundary, not just a style preference). Verified live via read-only browser
  inspection:
  - IAM role `orion-gumroad-webhook-role-w62tpsau` has the inline least-privilege policy +
    `AWSLambdaBasicExecutionRole` attached.
  - Lambda function `orion-gumroad-webhook` exists with a genuine "API Gateway" trigger wired in
    its Function overview diagram.
  - API Gateway route `ANY /api/gumroad/webhook/{token}` (route `kd1oa8e`) integrated
    (integration `gfiqge1`) on `orion-license-api` (`v348t5hg3i`), `$default` stage has
    **Automatic Deployment: Enabled** — confirmed live, deployment `wgy83l`. No manual "Deploy"
    click needed for this API.

## Exactly where I stopped
The Lambda function `orion-gumroad-webhook`'s Code tab in the AWS Console **still shows only the
default stub** (`import json` / `# TODO implement`) — the real source has NOT been pasted in yet.
I was mid-way through pasting it: I'd staged the full source as a JS string
(`window.__gumroadLambdaSrc`) in that browser tab's page context via `javascript_tool`, about to
write it to the clipboard and paste it into the Monaco editor. **That staged JS variable is almost
certainly gone now** (new session / tab may have reloaded) — redo the staging step from the file
on disk rather than assuming it's still there.

Chrome tabs should still be open from before (call `tabs_context_mcp` to get fresh IDs — don't
reuse old numeric tab IDs from a prior session). Tabs to expect: Discord Dev Portal, Discord
`#verification` channel, DynamoDB tables list, Cloudflare Worker `orion-discord-bot` settings,
Gumroad Products page, API Gateway Stages page, IAM role permissions page, and the
`orion-gumroad-webhook` Lambda Code tab.

## Immediate next steps
1. Read `discord_launch/gumroad_webhook/lambda_function.py` fresh from disk (it's confirmed intact
   — plain `Read` works, no need to route around a compressed/garbled render).
2. On the `orion-gumroad-webhook` Lambda's Code tab: stage that source as a JS string in the page
   context, `navigator.clipboard.writeText(...)` it, click into the Monaco editor, Ctrl+A, Ctrl+V
   — avoid simulated keystroke typing (risks corruption from auto-closing brackets/indentation on
   ~279 lines).
3. Screenshot to confirm the paste is clean and complete (matches file start/end), then find and
   click the save control (likely "Deploy" once the editor is dirtied, or the "Update" button) to
   persist it.
4. Run a test invoke from the console (synthetic `application/x-www-form-urlencoded` test event)
   to sanity-check the handler before touching live Gumroad traffic.
5. **Ask the user before** setting the Gumroad Ping endpoint URL (Settings → Advanced) to
   `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/gumroad/webhook/3e0f1525e29f4086366f81c4ddadf0bc`
   — this is the live cutover point and requires explicit permission under the standing rules
   (webhook/endpoint URL changes always do, even under broad authorization).
6. Run the test-purchase/refund verification from the plan (Sequence step 6): `test=true` Gumroad
   purchase → expect Discord DM + 💎 Customer role + CloudWatch `AUDIT gumroad_issue_ok`; refund →
   expect CloudWatch `AUDIT gumroad_revoke_ok`, role removed, license revoked; watch for
   `seller_id_mismatch` / `license_verify_failed` / `invalid_token` as failure signals.
7. Double-check "Generate a unique license key" is toggled on for all 8 Gumroad products (flagged
   earlier, not yet independently re-verified).
8. Deliver the final report in the plan's A–H shape, adapted for Gumroad.

## Open items to flag, not decide
- Whether SellHub stays live in parallel during cutover or gets taken down once Gumroad is
  verified.
- SellHub 0-stock bump — still nominally approved, low priority given the migration.
- Unrelated, separate thread: Discord role-ID extraction for
  `/orion/discord_customer_role_id` / `/orion/discord_lifetime_role_id` SSM params — no progress
  yet, pick up independently if the user raises it.
- Noted but unresolved, not urgent: Cloudflare Worker tab shows a Worker named
  `orion-discord-bot`, but the docs reference `license-redeem-proxy` — not yet reconciled, don't
  touch without asking (Worker/webhook config changes need explicit permission).

## Standing constraints that still apply
- Never modify IAM/access-control/permissions myself — hard system boundary, survives any user
  authorization; state the rule and hand it to the owner.
- Never touch a live webhook/endpoint URL (Gumroad Ping, Discord Interactions URL, etc.) without
  asking first, even with broad standing authorization.
- No real paid purchases; additive-only changes to shared infra.
- `discord_launch/gumroad_webhook/lambda_function.py` is already-reviewed source I wrote — pasting
  it into the Lambda console the owner already created/wired is in scope for me (this is not the
  same as unreviewed changes to an existing production Lambda).
