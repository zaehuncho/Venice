# Venice launch stack: read-only live audit (2026-09-19)

Scope: a read-only audit of what is live right now, compared with the repo working tree on branch
`fix/timing-input-and-remoteplay-blockers`. Nothing live was changed: there were no Lambda updates,
no SSM writes, no Discord writes (GET only), no wrangler deploys or secret changes, and no EC2
actions. The only local writes are this file and scratch files under `D:\NexusVision\live_audit\`.
No secret values appear here. Secrets were read in-process only; the Triton token is identified by
its length (72) and a 6-character hash prefix (`cf25c2`).

Access used: AWS as `venice-automation` (Lambda get-function and config, CloudWatch filter-log-events,
SSM describe-parameters, EC2/CloudFormation/SSM-instance describe). Cloudflare through the wrangler
OAuth token (`wrangler deployments/versions/secret list`, plus read-only `GET` calls to the API for
script content, script settings and the workers.dev subdomain). Discord REST as Triton (GET only).
Denied or not attempted: IAM reads, DynamoDB list, zone rulesets (403), Lambda function-URL config,
`ssm:SendCommand` (outside the brief), and the guild member list, integrations, webhooks, automod
and invites (403; Triton has no Manage Guild and no GUILD_MEMBERS intent).

---

## 0. Headline findings

1. **No real-money purchase path is live.** The storefront Worker runs Stripe **test mode**:
   `GET https://zaeorion.com/api/checkout/config` returns `publishableKey: "pk_test_51UGADh…"`.
   The live version is bound to the test price `price_1UGAOmGniZwGqtXLa9iDiEtk`. The live webhook
   code also ignores every event unless both keys are live, so even a test purchase provisions nothing.
2. **The production client will probably stop firing about 15 minutes after activation.** The live
   license Lambda cannot sign heartbeat leases, for two reasons. The package ships only
   `lambda_function.py`, so `cryptography` is missing; the logs show
   `[LEASE] signing unavailable (fail-soft): No module named 'cryptography'`. And SSM
   `/orion/lease_signing_key` does not exist. Production builds force lease-gated fire on
   (`LeaseGate::enabledFromEnvironment` returns `true` under `ORION_PRODUCTION_BUILD`) and embed a
   lease verification key, so a heartbeat without a signature is not refreshed. `fireAllowed()` then
   turns false once the 900 s activation lease goes stale. This comes from reading the code and logs.
   It has not been observed on a production build, so confirm it on one before relying on it.
3. **Renewals will fail at day 30.** The Worker requires `invoice.paid === true` for
   `subscription_cycle` invoices. Stripe removed `Invoice.paid` in API version `2025-03-31.basil`
   ([changelog](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices)).
   The Worker already reads basil-only fields (`parent.subscription_details`), so every renewal will
   throw, return 500, and never extend the license.
4. **Both Lambdas are byte-identical to the repo.** Redeploying the repo files changes nothing. The
   live code exists only in the uncommitted working tree, and `website/` is untracked in git.
5. **The orion-activate IAM role is missing grants**, so rate limiting fails open on activate,
   pair-issue and admin, and every config read falls back to its default
   (`owner_totp_required=false`, `owner_ip_allowlist=[]`, `min_client_version=""`, and so on).
   This was still happening on 09-17.
6. **The activation fee is not implemented** on any live path, and no customer-facing copy mentions it.

---

## 1. Lambdas

### 1a. `orion-activate` (the license backend)

| Field | Live value |
|---|---|
| Runtime / handler / architecture | `python3.12` / `lambda_function.lambda_handler` / x86_64, 128 MB, 15 s timeout |
| LastModified | `2026-09-17T04:25:08Z` |
| CodeSha256 | `Pjk+8xr6AQ8KzgpeL/cQUZk2wC4FfMFZszyAhOJoD3Q=` (zip hex `3e393ef31afa010f…`) |
| Package contents | `lambda_function.py` **only**. No layers, no vendored dependencies. |
| Published versions | `$LATEST` only (no aliases; rollback depends on the local zips) |
| Role | `orion-activate-lambda-role` |
| Env var **names** | `ADMIN_SECRET_SSM`, `EDGE_AUTH_ENFORCE`, `EDGE_AUTH_SECRET_SSM`, `TOKEN_SECRET_SSM`. The code reads **none** of these; the only env it reads is `ORION_ARTIFACT_ALLOWLIST`, which is unset. `EDGE_AUTH_ENFORCE` is dead because edge auth is always enforced (HIGH-4). |

**Diff against `backend/lambda_function.py`: identical.** Both are 4711 lines with the same SHA-256
(`7a4e015b6e07…`). There are **no behavioural differences**. The live zip is byte-identical to
`.codex_artifacts/discord-account-purchase-20260916/LAMBDA_CANDIDATE.zip`, the 09-16/17 deploy. The
repo file has **3094 insertions** relative to git `HEAD` (7d19686), so the live code is not committed.

**Tests:** `cd tests/backend && ../../.venv/Scripts/python.exe -m pytest -q` gives **271 passed**.
Running `tests/discord tests/backend` together fails at collection (17 errors,
`ModuleNotFoundError: No module named 'conftest'`) because `tests/backend` has its own
`pytest.ini`/`conftest` import scheme. Run the two directories separately.

**What the tests do not cover:** the venv has `cryptography` installed and conftest seeds the lease
key into moto SSM, so the suite cannot catch the live packaging and SSM gaps:

- Every `cryptography` import fails in the live Lambda: lease signing (`sign_lease`), update-manifest
  signing and verification (`/api/update` POST), and shard store and retrieve (AES-GCM,
  `/api/shard/*`). The error was last seen in the logs on 08-07 and 08-08, which were the last live
  heartbeats. `POST /api/license/check` does not appear anywhere in 45 days of logs after that.
- SSM parameters the code reads that **do not exist**: `/orion/lease_signing_key`,
  `/orion/owner_totp_secret`, `/orion/staff_token_secret`, `/orion/staff_enroll_key_hash` and
  `/orion/webhook_bot_secret`. The last one is optional because `require_bot` tries each name.
- IAM denials seen in CloudWatch over 09-15 to 09-17, still occurring on 09-17:

  | Count | Denial |
  |---|---|
  | 69 / 68 / 65 / 33 / 31 / 22 / 7 / 4 | `orion-config GetItem` for the keys `min_client_version`, `blocked_versions`, `motd`, `owner_ip_allowlist`, `owner_totp_required`, `alerts`, `reset_policy_defaults`, `fraud_thresholds`. Each falls back to `CONFIG_DEFAULTS`. |
  | 33 / 8 / 8 / 4 | `orion-ratelimit UpdateItem`, which logs `[RATELIMIT] fail-open` for `admin`, `activate`, `activate_ip` and `pair_issue` |
  | 2 / 2 | `orion-audit Query` on `day-ts-index` and `Scan`, so `/api/admin/audit` returns nothing |

**Verdict: IDENTICAL.** Redeploying the repo file does nothing. The package is still deficient: a
redeploy has to **add** `cryptography` (a manylinux x86_64 cp312 wheel plus `cffi`) to the zip. That
is allowed by `lambda:UpdateFunctionCode`, but it is a live change that needs the owner's OK. The
lease key also has to exist in SSM. Once both are in place, a new deploy is safe and is needed.

### 1b. `orion-gumroad-webhook` (legacy Gumroad path)

| Field | Live value |
|---|---|
| Runtime | `python3.13`, 128 MB, 30 s timeout |
| LastModified | `2026-09-15T23:22:13Z` |
| CodeSha256 | `KDBVqmrOpEInDZs/xIT10VbDLrwvT1MFKlnYBXuyef0=` (zip hex `283055aa6acea442…`, the same as `.codex_artifacts/venice-site-20260915/deploy/webhook-20260915182201.zip`) |
| Role | `service-role/orion-gumroad-webhook-role-w62tpsau` |
| Env var names | **none**. So `GUMROAD_ACTIVATION_PRODUCT` is empty (the activation-fee path is **off**), `GUMROAD_HWID_RESET_PRODUCT` defaults to `orion-hwid-reset` (on), and `OWNER_DISCORD_USER_ID` is empty. SSM `/orion/owner_discord_user_id` does not exist either, so **owner dispute and chargeback alerts are disabled**. |

**Diff against `discord_launch/gumroad_webhook/lambda_function.py`: identical.** Both are 859 lines
with SHA-256 `6497c7e04688…`. It is uncommitted: 701 insertions relative to HEAD.

Tests: `tests/discord/test_gumroad_membership.py`, `test_gumroad_webhook_admin_v2.py` and
`test_discord_user_agent.py` all pass. The whole `tests/discord` suite gives
**127 passed, 1 failed**. The failure is
`test_venice_guard_refresh.py::test_status_prefers_discord_id_and_fail_closed_legacy_lookup`, which
still expects `/status(interaction, key)`. The repo and live bot dropped the `key` option on purpose
on 09-16 ("Triton /status has no key option"), so the **test is stale**, not the bot.

Log history: on 09-15 at 22:13Z there were `AccessDenied` errors on `GetParameter` (the edge secret)
and on `DeleteItem` (claim release). After that, synthetic pings from 23:17Z to 01:45Z provisioned
and DMed successfully. Nothing has arrived since. Whether the role's DeleteItem grant has been fixed
is unknown and is an owner check.

**Verdict: IDENTICAL.** Redeploying is a no-op. The webhook is only relevant if the Gumroad product
stays on sale (see section 2e).

---

## 2. Storefront Worker `venice-site-production` (zaeorion.com)

### 2a. Deployment state

- Current version at 100%: **`fc33d689-caf3-48c4-a694-141f5a98fd5f`**, created
  `2026-09-17T04:49:10Z` (the "Discord account page" deploy). Earlier production versions:
  `bda4dbf0` ("Discord OAuth launcher pairing"), `6c637b48` (secret change at 03:40:25Z, matching SSM
  `/orion/website_pair_secret` modified 03:40:23Z), `5786b826` ("Isolate Stripe test webhooks from
  production entitlements"), `4a1da9ad`, `9a138b69` ("dedicated Worker credential", matching SSM
  `/orion/worker_bot_secret` modified 21:48Z on 09-16), and `84fc3f4e`.
- Routes: `zaeorion.com/*` and `www.zaeorion.com/*`. workers.dev is **disabled**.
- Live bindings for `fc33d689`: `CHECKOUT_PROVIDER="stripe"`,
  **`STRIPE_PUBLISHABLE_KEY="pk_test_51UGADh…"`**, **`STRIPE_PRICE_ID="price_1UGAOm…"` (the test
  price)**, `DISCORD_CLIENT_ID="1549481446782017668"` (the Triton app), `ORION_API_BASE`=execute-api,
  and `BUY_URL`=Gumroad. **There is no `STRIPE_MODE` variable.**
- Secret names, live against the repo's `secrets.required`: `DISCORD_CLIENT_SECRET`,
  `ORION_BOT_SECRET`, `ORION_EDGE_AUTH`, `PAIR_ISSUER_SECRET`, `SESSION_SECRET`, `STRIPE_SECRET_KEY`
  and `STRIPE_WEBHOOK_SECRET`. **All 7 names are present.** Their values cannot be read. The Stripe
  pair is still the test pair: `.codex_artifacts/stripe-restricted-live-20260917/VERIFICATION.txt`
  says "Production checkout is not yet switched from the test key because its live secrets are not
  installed", and no secret change has happened since 09-17 03:40Z.

### 2b. Live code compared with the repo (`website/src/worker.js`)

I downloaded the live bundle through the Cloudflare API (`/workers/scripts/…/content/v2`, saved to
`D:\NexusVision\live_audit\worker\`), normalised the esbuild output and diffed it against the repo.
**The repo is newer than live.** The behavioural differences:

| # | Area | Live (fc33d689) | Repo |
|---|---|---|---|
| 1 | `checkoutConfigured` | Stripe is "configured" if any publishable key, secret key, price and webhook secret are set. Test and live keys can be mixed. | Requires `STRIPE_MODE` ∈ {test, live}, `pk_<mode>_` and `sk_<mode>_`\|`rk_<mode>_`. |
| 2 | Live-event gate | `liveKeys` accepts only `sk_live_` | Accepts `sk_live_` **or `rk_live_`** (restricted key) |
| 3 | `/health`, `/api/checkout/config` | A misconfiguration reports `"fallback"` / `provider:"gumroad"` | Reports `"unavailable"` when `CHECKOUT_PROVIDER=stripe` but misconfigured |
| 4 | `/buy` when not configured | **Falls back to the Gumroad `BUY_URL`** (302) | Returns **503 "Checkout is being prepared."** and never falls back to Gumroad |

Repo production variables (not live): `STRIPE_MODE="live"`, `pk_live_51UGADh…`,
`STRIPE_PRICE_ID="price_1UGaaRGniZwGqtXLwQV7OLRY"` (the live "Venice Monthly" at $25/month, product
`prod_VH8sGbabpgoIuZ` per the 09-17 record). `npm run check` in the repo gives
**22/22 pass**. I did not run a wrangler dry-run because the harness classified it as a production
deploy; the 09-17 record shows a dry-run with `env.STRIPE_MODE ("live")` and
`env.STRIPE_PRICE_ID ("price_1UGaaR…")`.

### 2c. Static assets

All 16 files in `website/public/` match live. `app.js`, the CSS files, icons, `robots.txt` and
`sitemap.xml` are byte-identical. The HTML pages (`/`, privacy, refunds, terms, 404) are
**byte-identical once one injected block is removed**. The index hash was never stable (7353f10d vs
936652dd), and the reason is that **Cloudflare's zone-level Bot/JS detection injects an inline
`<script>` into every HTML response**. That script loads `/cdn-cgi/challenge-platform/scripts/jsd/main.js`
and contains a per-request ray id and timestamp, so the live hash changes on every fetch (I saw
`bc28f765` and then `13543d4d`). With the block stripped, the page hashes to `936652dd` = repo.
Side effect: the site's CSP (`script-src 'self' https://js.stripe.com`, no `unsafe-inline`) blocks
that injected script. Visitors get a CSP violation in the console and JS detections never run. This
is low severity, and fixing it is an owner task in the Cloudflare dashboard.

### 2d. Purchase flow as implemented (repo and live are the same except for 2b)

1. The site's "Subscribe" button (`data-checkout`) makes `app.js` fetch `/api/checkout/config`. An
   unauthenticated visitor goes to `/auth/discord?return_to=/#pricing`. `/buy` takes the same route
   (302 to OAuth or to `/#pricing`). `www` is 308-redirected to the apex.
2. **Discord OAuth** uses scope `identify` and client `1549481446782017668` (Triton). The redirect
   `https://zaeorion.com/auth/discord/callback` is registered on the app. The callback sets
   `venice_session`, an HMAC token signed with `SESSION_SECRET` and valid for 1 hour.
3. `POST /api/checkout/session` creates a Stripe Checkout Session with `mode=subscription`,
   `ui_mode=embedded_page`, one line item `STRIPE_PRICE_ID` ×1, `client_reference_id` and
   `metadata.discord_user_id`, subscription metadata, and **`automatic_tax[enabled]=true`**.
   **There is no trial, and there is no activation-fee line.**
4. **The Stripe webhook** is implemented at **`POST /api/stripe/webhook`**. It checks the
   `Stripe-Signature` v1 HMAC with a 300 s tolerance and a 1 MB body cap. The events it acts on:
   - `checkout.session.completed` with `payment_status=paid`, mode subscription and
     `amount_total>0`. It re-fetches the subscription (livemode, `active`, exactly one item equal to
     `STRIPE_PRICE_ID` with quantity 1, matching Discord id), then calls
     `ORION_API_BASE/api/bot/provision` with `{plan: month, days: 30, notify: true, subscription_id}`.
   - `invoice.paid`: `subscription_create` is ignored. `subscription_cycle` triggers a renew
     provision. **This is broken, see blocker B3.**
   - `customer.subscription.deleted` and `customer.subscription.updated`
     (`status` = `canceled` or `unpaid`) call `/api/bot/chargeback`, which revokes the key and removes
     the Customer role.
   - `invoice.payment_failed` is only logged.
5. **The backend** (`orion-activate`) accepts Stripe provisions only from the consumer
   `/orion/worker_bot_secret`. It mints a license with `stripe_subscription_id` and
   `oauth_pair_required=true`, grants the Customer role, and has Triton DM a subscription
   confirmation and connect link. **The DM does not contain the key.**
6. The return URL `/checkout/complete?session_id=` redirects to `/discord?purchase=complete`. The
   customer then uses "Connect launcher", which goes through `/connect`,
   `/api/bot/pair-issue` (`PAIR_ISSUER_SECRET` ↔ `/orion/website_pair_secret`), a five-minute one-use
   `PAIR-` code, and `orion://activate?key=PAIR-…`. The launcher redeems it through
   `/api/license/redeem` (on `api.zaeorion.com`) or `/api/activate`.
7. Discord `/purchase` shows one link button to `https://zaeorion.com/#pricing`. `/claim_trial`
   calls `/api/bot/trial`, which grants Trial and DMs the connect link.

**Stripe dashboard configuration the owner has to confirm (I cannot see Stripe):**

- The live endpoint `we_1UGaudGniZwGqtXLPy81NNQ1` points at `https://zaeorion.com/api/stripe/webhook`
  and is Active, per the 09-17 record, with events `checkout.session.completed`,
  `customer.subscription.deleted`, `customer.subscription.updated`, `invoice.paid` and
  `invoice.payment_failed`. That list matches what the Worker needs. **Its signing secret must be
  rolled before use**, because the 09-17 record says it was exposed in browser tool output. Put the
  new secret into `STRIPE_WEBHOOK_SECRET`.
- Note the endpoint's **API version**. If it is basil or later, B3 applies.
- Finish **account verification** and create the live **restricted key** (Checkout Sessions: Write,
  Subscriptions: Read), or use `sk_live_`. The 09-17 record says Stripe was showing a
  "Verification required" prompt.
- **Stripe Tax must be active in live mode**, because the Worker always sends
  `automatic_tax[enabled]=true`. If Tax is not set up, live session creation fails and the customer
  sees "Secure checkout is temporarily unavailable".
- **Payment methods** should be cards and wallets only. `checkout.session.async_payment_succeeded`
  is not handled, so a delayed-notification method would never provision.
- The **customer portal** or some cancellation route, since the copy says "cancel any time" and the
  Worker has no billing-portal link.
- The live price `price_1UGaaR…` must be active and recurring monthly at $25.00 USD, with the
  statement descriptor and branding set.

### 2e. Which payment path is live end-to-end

- **Stripe:** test mode only. The live Worker serves the `pk_test_` key and test price, and every
  webhook returns `non_live_ignored` because its `liveKeys` check needs both `sk_live_` and
  `pk_live_`. So **neither test nor real payments provision anything** through the live Worker
  today. The last full provision through the Worker was the 09-16 06:38Z test-mode run, before the
  09-17 isolation deploy.
- **Gumroad:** the product "Venice — Monthly" at $25 is **still publicly purchasable** at
  `https://jacksonia66.gumroad.com/l/orion-monthly` (HTTP 200). The webhook Lambda is deployed and
  proved itself with synthetic pings on 09-15 and 09-16. However, nothing on the site or in Discord
  links to it any more: `/buy` goes to OAuth and then Stripe, and `_redirects`' `/buy → Gumroad` rule
  is dead because the Worker runs first. Whether Gumroad's ping URL still targets the Lambda is an
  owner check.
- **Activation fee: not implemented anywhere live.** The Stripe checkout has a single subscription
  line. The Gumroad `_handle_activation_fee` code exists but is off (the env var is unset, and
  `/l/orion-activation` returns 404). The webhook source says "no first-purchase surcharge". The
  customer-facing copy (site: "Continue for $25 per month"; `#pricing`: "One plan, billed monthly")
  does not mention a fee. Only `docs/DISCORD_SERVER_SETUP_PROMPT.md` still describes one. This is an
  **owner decision**. If the fee stays, add a one-time `price` as `line_items[1]` in
  `createCheckoutSession`. `subscriptionMatchesPlan` still holds, because one-time items land on the
  first invoice, not on the subscription. The copy would also need updating.

---

## 3. Legacy Discord Worker and the Triton host

**Workers on the account** (`GET /accounts/…/workers/scripts`):

| Worker | Route | workers.dev | Status |
|---|---|---|---|
| `venice-site-production` | `zaeorion.com/*`, `www.zaeorion.com/*` | off | the storefront (above) |
| `orion-discord-bot` | **none** | **on**: `orion-discord-bot.ialijacks1961.workers.dev` answers "Orion License Bot" (200) | Legacy HTTP-interactions bot, last modified 2026-07-03. Secrets: `DISCORD_APP_ID`, `DISCORD_PUBLIC_KEY`, `ORION_BOT_SECRET`. About 130 lines; **not** the 581-line repo `discord_launch/orion_worker.js`, which was never deployed. |
| `license-redeem-proxy` | `api.zaeorion.com/api/license/redeem` | on | Injects `X-Edge-Auth` and forwards to `api.zaeorion.com`. It is redundant: `GET https://api.zaeorion.com/api/version` already returns 200 with no header, so a zone rule injects the edge secret for every path. The direct execute-api URL returns 403 `forbidden`. Keep it, because it serves the launcher's redeem route. |
| `orion-shard-gate` | `api.zaeorion.com/api/shard/*` | off | Shard gate. Note that the backend shard crypto is broken (B2). |

**Does anything still route to `orion-discord-bot`?** Not from Triton. `GET /applications/@me`
returns `interactions_endpoint_url: null`, so Triton's interactions arrive over the gateway. The old
Worker's `DISCORD_APP_ID` is a secret, so I cannot tell which application it served or whether that
application still has its endpoint set. There is no managed role for any app other than Triton and
Venice Guard in the guild. **`discord_launch/wrangler.toml` (`name="orion-license-bot"`),
`orion_worker.js`, `discord_commands.json` and `register_commands.py` are superseded by Triton and
should not be deployed or run.** See section 4d.

**Triton host** `i-0ba45335427155bb7`: `running`, t4g.micro, launched 2026-09-15T21:08Z, us-east-1a,
IMDSv2 `required`, instance profile `venice-discord-bots-BotInstanceProfile-…`. The SSM agent is
**Online** (last ping 2026-09-19T20:58Z, agent 3.3.4624.0). CloudFormation stack
`venice-discord-bots` is **`UPDATE_ROLLBACK_COMPLETE`**: the 09-15 21:56Z update failed because
`venice-automation is not authorized to perform: ec2:StopInstances`. Any future stack update that
touches `BotInstance` needs admin credentials.

**Comparing the live bot code:** this is not possible read-only with my access. It needs `ssm:SendCommand` (for
example `sha256sum /opt/venice-bots/app/triton_gateway.py`), which executes on the host and is
outside this brief. There are two pieces of indirect evidence:

- The 09-16 deploy record gives the new live source
  `/opt/venice-bots/app/triton_gateway.py` SHA-256 **`2cb8d000c96be261…`**, which **equals** the repo
  `discord_launch/orion_bot.py` (`2cb8d000c96be261…`).
- The live guild command tree matches the repo tree exactly. The `/status` and `/faq` command
  versions carry the snowflake time 2026-09-17T04:29:59Z, right after the 04:25Z Lambda deploy.

The same 09-16 records show `venice-guard.service` active (`venice_guard.py` `4dbd7486…` = repo) and
`nereus.service` staged but **inactive**. The Nereus bot user is in the guild with the Bots role.

---

## 4. Discord guild "Venice" (`1483836452776316970`)

### 4a. Guild settings

- Owner: `893902727845920840` (holds the Owner role).
- `mfa_level=1` (2FA required for moderation), `verification_level=3` (High),
  `explicit_content_filter=2` (all members), and notifications set to mentions only. These match
  target section 6.
- Features: COMMUNITY and NEWS. `rules_channel` is `#📋-rules`, `public_updates_channel` is
  `#staff-updates`, `safety_alerts` go to `#mod-log`, and onboarding is disabled.
- 5 members (4 online). The ones I could identify are the Owner, Triton, Venice Guard and Nereus.
  **The fifth member, and who holds Admin or Co-Owner, cannot be read** (the member list returned 403).

### 4b. Roles (top to bottom) and dangerous permissions

| Pos | Role | ID | Dangerous permissions | Notes |
|---|---|---|---|---|
| 20 | Owner | 1484025290224046182 | **ADMINISTRATOR** | ok |
| 19 | **Bots** | 1549503472079212675 | none (perms 0) | **Sits above Co-Owner and Admin.** Held by Triton, Venice Guard and Nereus. Triton and Guard have Manage Roles, so they can assign Co-Owner or Admin (both Administrator). A leaked bot token becomes Administrator. |
| 18 | Co-Owner | 1485348366824247447 | **ADMINISTRATOR** | not in the target doc; the owner said to delete it on 09-19 (memory) |
| 17 | Admin | 1485348271638450449 | **ADMINISTRATOR** | Target: Manage Server/Roles/Channels, Kick, Ban and Manage Messages without Administrator. The owner said to keep it (09-19). Reported only. |
| 16 | Support | 1485348123562741810 | none | orphan: 0 permissions and no overwrites; not in the target |
| 15 | Venice Guard (managed) | 1549498158801879050 | Manage Roles, Manage Channels, Kick, Ban, Manage Messages, Moderate, View Audit Log | verification and moderation bot |
| 11 | Triton (managed) | 1549489526634844275 | Manage Roles, Manage Channels | above Customer and Trial (ok) |
| 10 | Verified | 1485354805680541977 | none | |
| 6 | Unverified | 1485350863957393490 | none | not in the target |
| 5 | **Staff** | 1520815228697448528 | **Manage Roles, Manage Channels**, Create Expressions | Target: Manage Messages, Kick, Timeout. Sits **below** Verified and the bots. With Manage Roles it can hand out Customer, Trial and Muted, which unlocks the customer channels but not a license, because entitlement is checked server-side. |
| 3 | Customer | 1549488662192857188 | none | ok |
| 2 | Trial | 1549488662935380101 | none | ok |
| 1 | Muted | 1549488663736483882 | none | ok |
| 0 | @everyone | — | none dangerous. It has View, History, Reactions, Use App Commands and **Use External Apps**; it has no Send at guild level, no Create Invite and no Mention Everyone. | |

The **Lifetime** role `1549488660049829908` has been deleted, which matches the revised target. SSM
`/orion/discord_lifetime_role_id` (v3) still points at it; only Triton's optional
`LIFETIME_ROLE_ID` could use it.

### 4c. Channels (live) compared with the target

| Live category | Channels (live) | Target | Diff |
|---|---|---|---|
| 📌 START HERE (@everyone view, no send) | `📋-rules`: a Venice Guard combined welcome, rules, terms, trial and verify panel, message `1549685816404746273` | 📋 INFORMATION: `#verify`, `#welcome`, `#announcements`, `#faq`, `#how-to-buy` | **Collapsed on purpose** on 09-16 (the owner asked for ONE welcome message). `#announcements` lives under COMMUNITY. `#faq` and `#how-to-buy` are missing, but `/faq` covers the FAQ. |
| 🛒 STORE | `pricing` (Triton embed, **says "Checkout status: test mode"**), `reviews` (empty) | `#pricing`, `#purchase`, `#reviews`, `#vouch-format` | `#purchase` and `#vouch-format` are missing. Commands work in `#pricing` because @everyone keeps Use App Commands. |
| 🎫 SUPPORT | `create-ticket` (Venice Guard panel; @everyone can use app commands) | `#create-ticket`, `#support-info`, `#hwid-reset` | `#support-info` and `#hwid-reset` are missing. There is no separate ticket bot. |
| 💬 COMMUNITY (@everyone hidden, Verified view and send) | voice `🎙 -General 1`, voice `🎙 -General 2`, `general`, `clips`, `announcements` (@everyone read-only) | `#general`, `#2k-discussion`, `#clips`, `#off-topic`, `#bot-commands` | `#2k-discussion`, `#off-topic` and `#bot-commands` are missing. **`General 1` has no Verified allow**, so Verified members cannot see it. **`General 2` is unsynced**: @everyone can view it, Unverified is denied, and there is no Muted deny. `announcements` is empty. |
| ⭐ CUSTOMER (Customer and Trial only) | `customer-lounge` (empty), **`downloads` (EMPTY)**, `setup-guide` (Triton embed, **says "activate with the key delivered by DM"**, which no longer matches the paired flow) | `#customer-lounge`, `#downloads`, `#setup-guide`, `#changelog`, `#priority-support` | `#changelog` and `#priority-support` are missing. **`#downloads` has no installer.** |
| 🔒 STAFF (@everyone hidden; Staff, Admin, Owner and bots allowed) | `staff-chat` `1549499814834610331`, `mod-log` `…818362015785`, `ticket-log` `…821939757156`, `sales-log` `…825190600875`, `audit-log` `…828390727732`, `staff-updates` `1512153191335198910` (admins only) | the same 5 required names | All 5 exact names are present. **No staff channel is exposed** to @everyone, Verified, Customer or Trial. Nereus has a member overwrite on `#audit-log` (+View, +Send, −History); needs review. |

The Muted deny (Send, Reactions, Speak) is present on every channel except `General 2`.

### 4d. Registered commands compared with the repo

- Global commands: **0**. Guild commands: **9**: `/purchase`, `/claim_trial`, `/hwid_reset`,
  `/status`, `/setup`, `/faq` (topic choices: capture_card, remote_play, no_greens, new_pc,
  missing_key, quick_answers; `public` option), `/deliver` (plan choices `[month]`,
  `default_member_permissions=0`), `/keygen` (plan choices `[month, lifetime]`,
  `default_member_permissions=0`), and `/lookup` (user, key). These are **identical to the repo
  `orion_bot.py` tree**, including the 09-15 plan changes.
- Command permissions: `/deliver` allows Admin, Staff and `#staff-chat`, and denies @everyone and all
  channels. `/keygen` allows Admin and `#staff-chat`, and denies @everyone and all channels. Both
  **match target section 4**.
- `discord_launch/discord_commands.json` is **stale**. It has 6 commands, no `/status`, `/setup` or
  `/faq`, and different descriptions. **Do NOT run `register_commands.py`.** It bulk-overwrites the
  guild commands, so it would delete `/status`, `/setup` and `/faq`, and it says itself not to run
  while the gateway bot is deployed. Triton syncs its own tree on boot. The 09-15 note "re-run
  register_commands.py" is superseded, because the new plan choices are already live.
- Triton application: `bot_public=true` (anyone can invite it), and the user-install integration type
  is enabled. Its only redirect URI is the storefront callback.

### 4e. Discord tidy plan (ordered)

**SAFE** (additive or reversible edits of the bots' own content):

1. After Stripe live is verified (B1), PATCH Triton's `#pricing` message `1549501172875010240` to drop
   the "Checkout status: test mode" line.
2. Change the `/purchase` copy in `orion_bot.py:671` ("currently in **test mode**"), then redeploy
   Triton. The restart re-syncs the same 9 commands. Do this together with 1.
3. PATCH Triton's `#setup-guide` message `1549501209650536460`. Field 1 should read "sign in at
   zaeorion.com/connect with Discord (or enter the PAIR- code)" instead of "key delivered by DM".
4. Post the `#downloads` content (signed installer link and connect steps) once B5 is resolved.
5. Sync `🎙 -General 1` with the COMMUNITY category so Verified members can see it.
6. Sync `🎙 -General 2` with COMMUNITY: hide it from @everyone, allow Verified, deny Muted.
7. Create `#support-info` and `#hwid-reset` under SUPPORT and post the target section 5 embeds. The
   HWID copy is "3 free, then 1 day off".
8. Create `#changelog` and `#priority-support` under CUSTOMER, inheriting the category permissions.
9. Optionally create `#2k-discussion`, `#off-topic` and `#bot-commands` (COMMUNITY) and
   `#vouch-format` and `#purchase` (STORE), inheriting the category permissions.
10. Optionally move `#announcements` into 📌 START HERE. Its overwrites are explicit, so access does
    not change.
11. Post a first launch announcement in `#announcements`.
12. In the Developer Portal, set Triton's **Public Bot** off and disable User Install. The owner does
    this in the portal.
13. Repo only: update `discord_commands.json` to mirror the live 9-command tree or mark it legacy,
    and fix the stale test in `tests/discord/test_venice_guard_refresh.py`.
14. Repo only: rewrite `docs/DISCORD_SERVER_SETUP_PROMPT.md` for Stripe (not Gumroad), the collapsed
    START HERE, and whatever the owner decides about the activation fee.

**DESTRUCTIVE or OWNER-DECISION:**

1. **Delete the Co-Owner role** (Administrator). The owner said to delete it on 09-19 (memory).
   First confirm who holds it; the member list is not readable with Triton's intents.
2. **Admin keeps Administrator.** The owner decided this on 09-19. It is reported here and no action
   is needed.
3. **Move the Bots role** (position 19) below Admin, Co-Owner and Support, to about position 12.
   Venice Guard's managed role at 15 still sits above Verified and Unverified, and Triton's at 11
   still sits above Customer and Trial. This removes the path from a bot token to Administrator.
   The position change is reversible but it changes the role layout.
4. **Staff role:** remove Manage Roles, Manage Channels and Create Expressions; add Manage Messages,
   Kick and Moderate Members; move it above Bots, Customer and Trial as in the target order.
5. **Delete the orphan Support role**, or give it a purpose.
6. **Remove Nereus's Send overwrite on `#audit-log`**, unless Nereus is meant to post audit lines.
7. **Delete or disable the `orion-discord-bot` Worker.** The reversible step is turning off its
   workers.dev subdomain. Check the old application's Interactions URL first.
8. **Unpublish the Gumroad "Venice — Monthly" product**, or keep it as a fallback. It is still on
   sale.
9. **Activation fee:** implement it in Stripe or drop it from the docs.
10. Delete the stale SSM `/orion/discord_lifetime_role_id` and the unused orion-activate env vars.
    These are live changes, but of low value.

**Counts:** 14 SAFE items (11 in Discord, 3 in the repo or portal) and 10 DESTRUCTIVE/OWNER-DECISION
items (6 in Discord, 4 elsewhere).

---

## 5. Launch blockers, ranked

| # | Severity | Blocker | Exact fix | Who |
|---|---|---|---|---|
| **B1** | CRITICAL | **Checkout is Stripe TEST mode, and the live webhook provisions nothing.** Live `fc33d689` has `pk_test_` and `price_1UGAOm…`; `/api/checkout/config` shows `pk_test_…`; the webhook returns `non_live_ignored` for every event. | **Owner, in the Stripe dashboard:** finish account verification; create the live restricted key; **roll** the `we_1UGaud…` signing secret; confirm live Tax, card-only payment methods, the portal and the price (section 2d). **Coordinator:** apply the B3 fix, run `npm run check`, then `npx wrangler versions upload --env production` (repo code plus live variables), `npx wrangler versions secret put STRIPE_SECRET_KEY --env production` and `… STRIPE_WEBHOOK_SECRET --env production` (the owner pastes the values into the prompt), then `npx wrangler versions deploy <id>@100% --env production`. Verify that `/api/checkout/config` shows `pk_live_` and `/health` shows `checkout:"stripe"`, then run a real $25 purchase, refund it, and check provision, DM and `/connect`. **Do not run `wrangler secret put` against the current live code.** It deploys immediately on top of the `pk_test` variables and old code, which would mix modes and still ignore live events. | Owner (Stripe) + coordinator (Worker) |
| **B2** | CRITICAL | **Lease signing cannot work in the live Lambda.** `cryptography` is missing from the zip (log: `No module named 'cryptography'`) and SSM `/orion/lease_signing_key` is missing. Production clients force lease-gated fire on and require a valid Ed25519 `lease_sig` to refresh, so automation should stop about 15 minutes after activation (`kFallbackLeaseTtlS=900`, `kDefaultMaxStalenessMs=15 min`). Manifest signing and shard AES-GCM are broken for the same reason. | Build the orion-activate zip with the `cryptography` and `cffi` wheels (`pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 -t pkg cryptography`, then add `lambda_function.py`) and deploy with `aws lambda update-function-code` after the owner OKs it. **Find the private half of the 07-17 lease keypair** whose public key is embedded in `LeaseGate.cpp` (`nKx5ximQ…`) and put it at `/orion/lease_signing_key` as a SecureString. If it is lost, generate a new keypair and rebuild the client with the new public key. Verify that a production build keeps firing for more than 20 minutes and that the heartbeat returns `lease_sig`. | Coordinator (zip and deploy with OK); owner (key custody, SSM value) |
| **B3** | HIGH (fails at day 30) | **Renewals are rejected.** `worker.js:437` requires `object.paid !== true` to be false, but `paid` does not exist on basil-or-later invoices. The test fixture (`worker.test.mjs:96`) uses an impossible payload that has both `paid:true` and `parent`. | Change the condition to `object.status !== "paid"`, drop `paid` from the fixture, and add a basil-shaped fixture. Ship with B1. The owner confirms the webhook endpoint's API version. | Coordinator |
| **B4** | HIGH | **IAM gaps on `orion-activate-lambda-role`**: rate limits fail open (activate, pair-issue, admin); config reads default (`owner_totp_required=false`, `owner_ip_allowlist=[]`, `min/blocked_versions`); the admin audit cannot be read. | In the IAM console, grant `dynamodb:GetItem` and `PutItem` on `orion-config`, `UpdateItem` on `orion-ratelimit`, and `Query` on `orion-audit/index/day-ts-index` (plus `Scan` on `orion-audit` if the fallback is kept). Then check that the CloudWatch lines `[RATELIMIT] fail-open` and `[CONFIG] read failed` stop. | Owner (AWS admin) |
| **B5** | HIGH | **Nothing to download.** `#downloads` is empty, and the release gate did not pass on 09-17: StrictSecurity was missing the trusted signing PEM, SSM `/orion/manifest_signing_key` does not match the release public key, and one `OrionNativeTests` assertion failed. | Owner supplies the trusted signing key; coordinator builds, signs and publishes the installer and posts it in `#downloads`. | Owner + coordinator |
| **B6** | MEDIUM | **Customer copy is stale:** the `/purchase` embed and the `#pricing` message say "test mode"; `#setup-guide` says "key delivered by DM". | Discord tidy items 1 to 3, after B1. | Coordinator |
| **B7** | MEDIUM | **The legacy Gumroad product is still on sale.** It is a second sales channel with no activation-fee handling and alerts to nobody (no owner id configured). | Unpublish it in Gumroad, or keep it and set `OWNER_DISCORD_USER_ID` and the ping URL. | Owner (Gumroad) |
| **B8** | MEDIUM (security) | **Discord role hierarchy:** the Bots role sits above the Administrator roles; Co-Owner and Admin hold Administrator; Staff has Manage Roles. | DESTRUCTIVE items 1 to 5. | Owner decision, then coordinator |
| **B9** | MEDIUM (ops) | **Live code is uncommitted:** backend, webhook and bot are modified against HEAD, and `website/` is untracked. After the 08-09 repo loss, this is the only copy apart from `.codex_artifacts`. | Commit on the current branch, excluding secrets and `node_modules`. | Coordinator |
| B10 | LOW | Missing SSM parameters: `owner_totp_secret` (Admin V2 TOTP cannot be enabled, and the config write is also blocked by B4), `staff_token_secret` and `staff_enroll_key_hash` (the staff-login path; 1 `POST /api/staff/login` appeared in 5 days, with the outcome not verified), and `owner_discord_user_id`. | Create them when the matching feature is launched. | Owner/coordinator |
| B11 | LOW | The legacy `orion-discord-bot` Worker is live on workers.dev, and `bot_public=true`. | DESTRUCTIVE item 7 and SAFE item 12. | Owner |
| B12 | LOW | The Cloudflare JS-detection injection is blocked by the CSP (console error, detections do not run). | In the Cloudflare dashboard, turn off JS detections for the zone or allow them through the CSP. | Owner (Cloudflare) |
| B13 | LOW | `tests/discord` has 1 stale failure (`/status key`), and running `tests/discord` with `tests/backend` in one call errors at collection. | Update the test; document running the two directories separately. | Coordinator |

---

## 6. Evidence files (local scratch)

`D:\NexusVision\live_audit\` contains:

- `live_orion-activate.zip` and `live_orion-gumroad-webhook.zip`, plus their unzipped copies
- `cfg_*.json` (the Lambda configurations; the env values are only SSM parameter names and `EDGE_AUTH_ENFORCE=1`, with no secrets)
- `worker\*.js` (the live Worker bundles)
- `site\` (the live HTML and assets)
- `discord\*.json` (guild, roles, channels, commands, permissions and message snapshots, with any
  `token` fields redacted)
- `channel_tree.txt`, `pytest_out.txt`
- the helper scripts `cf_get.py`, `discord_get.py`, `discord_member.py` and `perms.py`, which read
  credentials in-process and never print them
