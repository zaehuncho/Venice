# (Body = the exact text to paste into browser-Claude.)

You are wiring SellHub purchases into Orion's existing AWS Lambda license backend so a PAID order mints a license key, the Discord bot DMs it to the buyer and grants the Customer role, and a REFUND/chargeback auto-revokes it. You work across AWS (Lambda + SSM Parameter Store), the Discord Developer Portal + the Orion server, and SellHub. First ask the owner for: BASE_URL (the API base — e.g. https://api.zaeorion.com or the API Gateway invoke URL).

Backend facts: Python Lambda behind API Gateway; DynamoDB table `orion-licenses`; secrets in SSM SecureString under `/orion/*`; existing routes `/api/activate`, `/api/provision`, etc. You're adding `POST /api/sellhub/webhook`. Keys are minted server-side, HWID-locked on activation, revocable, and expire by tier — this flow preserves all of that.

## STEP 1 — Deploy the Lambda code (AWS Lambda console → the Orion function → Code)
Make THREE edits to `lambda_function.py`, then Deploy:
1. In the top imports, add: `import urllib.request, urllib.error`
2. Immediately BEFORE the line `def lambda_handler(event, context):`, paste the whole `=== BLOCK ===` at the bottom of this prompt.
3. In `lambda_handler`, right after:
   ```
           elif path == "/api/version":
               return handle_version()
   ```
   add:
   ```
           elif path == "/api/sellhub/webhook":
               return handle_sellhub_webhook(event.get("body") or "", body, headers)
   ```
   Deploy. Sanity check: `GET BASE_URL/api/version` still returns OK.

## STEP 2 — Create the Discord delivery bot
- Discord Developer Portal → New Application "Orion License Bot" → Bot → Reset Token → copy the BOT TOKEN (secret).
- No privileged intents needed (it DMs users who share the server + manages roles).
- OAuth2 → URL Generator → scope `bot` → permissions **Manage Roles, Send Messages** → open the URL → add to the Orion server.
- Server Settings → Roles → drag the bot's role ABOVE 💎 Customer and ⭐ Lifetime (a bot can only assign roles below itself).

## STEP 3 — Collect IDs (Discord → Settings → Advanced → Developer Mode ON)
- GUILD_ID = right-click the server → Copy Server ID.
- CUSTOMER_ROLE_ID = Server Settings → Roles → right-click 💎 Customer → Copy Role ID.
- LIFETIME_ROLE_ID = same for ⭐ Lifetime.

## STEP 4 — Store secrets in SSM (AWS → Systems Manager → Parameter Store → Create, type SecureString)
- `/orion/discord_bot_token` = bot token (Step 2)
- `/orion/discord_guild_id` = GUILD_ID
- `/orion/discord_customer_role_id` = CUSTOMER_ROLE_ID
- `/orion/discord_lifetime_role_id` = LIFETIME_ROLE_ID
- `/orion/sellhub_webhook_secret` = (filled in Step 5)
The Lambda role already reads `/orion/*` SecureStrings; if logs show SSM AccessDenied, add `ssm:GetParameter` on `/orion/*` to the role.

## STEP 5 — Configure SellHub
- **Discord ID capture (critical):** the buyer's Discord ID MUST reach the webhook or the bot can't DM. Easiest: sell via SellHub's `/purchase` bot in #purchase (it knows the buyer's Discord ID) and/or require Discord login at checkout. Verify an order carries the Discord user ID.
- **Webhook:** SellHub → Developer/Webhooks → add endpoint = `BASE_URL/api/sellhub/webhook`; subscribe to **order paid/completed** AND **refunded/chargeback/cancelled**.
- Copy SellHub's webhook **signing secret** → save as `/orion/sellhub_webhook_secret`.
- **Field/header check:** the Lambda verifies the signature from header `X-Sellhub-Signature` / `X-Signature` / `X-Webhook-Signature` (HMAC-SHA256 of the raw body) and reads order id, product name, discord id, email, event/status from common field names. Open a test webhook delivery and confirm the signature header name + that the payload has an order id, product name, and the buyer's discord id. If any differ, give the owner the exact header + field names so they tweak `verify_sellhub_signature()` and `handle_sellhub_webhook()`.

## STEP 6 — Test end-to-end
- Create a 100%-off TEST coupon. From a test Discord account (in the server, DMs-from-members allowed), buy "Orion — Day" via `/purchase`.
- PASS = bot DMs the key embed + 💎 Customer role added + CloudWatch shows `AUDIT sellhub_issue_ok`.
- Idempotency: redeliver the same webhook → CloudWatch shows `sellhub_duplicate`, no 2nd key.
- Refund: refund the test order → CloudWatch shows `AUDIT sellhub_revoke_ok`; that key now fails activation as "revoked".
- No DM? confirm the webhook carried the Discord ID, the account allows server-member DMs, and the bot role sits above Customer. `sellhub_sig_fail` = wrong secret/header (Step 5). `ignored event=…` = paid-event detection missed; give the owner the event/status string.
- Disable the TEST coupon.

SECURITY: `/api/sellhub/webhook` mints keys ONLY on a valid SellHub HMAC signature. Never call it unsigned; keep the bot token + signing secret in SSM only.

---

## === BLOCK === (paste before `def lambda_handler`)
```python
# ====================================================================
# SellHub purchase webhook -> mint license + DM key via the Discord bot.
# Signature-verified, idempotent per order, auto-revoke on refund.
# Required SSM SecureString params:
#   /orion/sellhub_webhook_secret /orion/discord_bot_token
#   /orion/discord_guild_id /orion/discord_customer_role_id /orion/discord_lifetime_role_id
# ====================================================================
SELLHUB_SECRET_SSM        = "/orion/sellhub_webhook_secret"
DISCORD_BOT_TOKEN_SSM     = "/orion/discord_bot_token"
DISCORD_GUILD_SSM         = "/orion/discord_guild_id"
DISCORD_CUSTOMER_ROLE_SSM = "/orion/discord_customer_role_id"
DISCORD_LIFETIME_ROLE_SSM = "/orion/discord_lifetime_role_id"
TIER_DAYS = {"day": 1, "week": 7, "month": 30, "lifetime": 36500}

_ssm_secret_cache = {}
def get_ssm_secret(name):
    if name in _ssm_secret_cache:
        return _ssm_secret_cache[name]
    val = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    _ssm_secret_cache[name] = val
    return val

def tier_from_product(name):
    n = (name or "").lower()
    for t in ("lifetime", "month", "week", "day"):
        if t in n:
            return t
    return ""

def _is_paid(ev, status):
    s = (str(ev) + " " + str(status)).lower()
    return (any(w in s for w in ("paid", "complete", "success", "fulfilled"))
            and not any(w in s for w in ("refund", "charge", "dispute", "cancel", "fail")))

def _is_refund(ev, status):
    s = (str(ev) + " " + str(status)).lower()
    return any(w in s for w in ("refund", "chargeback", "charge_back", "dispute", "cancel"))

def _discord_api(method, path, body=None):
    url = "https://discord.com/api/v10" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bot " + get_ssm_secret(DISCORD_BOT_TOKEN_SSM),
        "Content-Type":  "application/json",
        "User-Agent":    "OrionLicenseBot (https://zaeorion.com, 1.0)",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:
        return 0, {"error": str(e)}

def discord_dm(discord_id, embed):
    st, ch = _discord_api("POST", "/users/@me/channels", {"recipient_id": str(discord_id)})
    if st not in (200, 201) or "id" not in ch:
        print(f"[ERROR] discord dm-open failed status={st}")
        return False
    st2, _ = _discord_api("POST", f"/channels/{ch['id']}/messages", {"embeds": [embed]})
    if st2 not in (200, 201):
        print(f"[ERROR] discord dm-send failed status={st2}")
        return False
    return True

def discord_set_role(discord_id, role_ssm, add=True):
    try:
        guild = get_ssm_secret(DISCORD_GUILD_SSM)
        role  = get_ssm_secret(role_ssm)
    except Exception as e:
        print(f"[ERROR] discord role config missing: {e}")
        return False
    st, _ = _discord_api("PUT" if add else "DELETE",
                         f"/guilds/{guild}/members/{discord_id}/roles/{role}")
    return st in (200, 204)

def verify_sellhub_signature(raw_body, headers):
    try:
        secret = get_ssm_secret(SELLHUB_SECRET_SSM)
    except Exception as e:
        print(f"[ERROR] sellhub secret missing: {e}")
        return False
    sig = (headers.get("x-sellhub-signature") or headers.get("x-signature")
           or headers.get("x-webhook-signature") or "")
    if not sig:
        return False
    if sig.startswith("sha256="):
        sig = sig.split("=", 1)[1]
    raw = raw_body.encode() if isinstance(raw_body, str) else (raw_body or b"")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.lower(), expected.lower())

def license_key_embed(key, tier, expiry):
    exp = "Lifetime — never expires" if tier == "lifetime" else f"<t:{expiry}:F>"
    return {
        "title": "🔑 Your Orion License",
        "description": ("Thanks for your purchase! Here is your license key:\n\n"
                        f"```\n{key}\n```\n"
                        "**Activate**\n1. Open the Orion app.\n2. Paste this key into the license field and click activate.\n"
                        "3. It binds to this PC. To move it, open a support ticket.\n\nKeep this key private — sharing or reselling voids it."),
        "color": 2450411,
        "fields": [
            {"name": "Tier",    "value": (tier.capitalize() or "—"), "inline": True},
            {"name": "Expires", "value": exp,                        "inline": True},
        ],
        "footer": {"text": "Orion • Precision Shot-Timing"},
    }

def sellhub_issue(order_id, product, discord_id, email):
    now    = int(time.time())
    tier   = tier_from_product(product) or "day"
    days   = TIER_DAYS[tier]
    expiry = now + days * 86400
    pointer_pk = "ORDER#" + order_id
    new_key = gen_license_key()
    try:
        licenses_table.put_item(
            Item={"license_key": pointer_pk, "kind": "order_pointer", "revoked": True,
                  "order_id": order_id, "issued_key": new_key,
                  "discord_user_id": discord_id, "tier": tier, "created_at": now},
            ConditionExpression="attribute_not_exists(license_key)")
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        prev = licenses_table.get_item(Key={"license_key": pointer_pk}).get("Item", {})
        print(f"[INFO] AUDIT sellhub_duplicate order={order_id} key_suffix={key_suffix(prev.get('issued_key',''))}")
        return ok_res({"ok": True, "duplicate": True})
    item = {
        "license_key": new_key, "email": email or "", "plan": tier, "status": "active",
        "revoked": False, "machine_id": "", "expiry": expiry, "created_at": now,
        "activation_count": 0, "deactivation_count_30d": 0,
        "order_id": order_id, "source": "sellhub",
    }
    if discord_id:
        item["discord_user_id"] = discord_id
    licenses_table.put_item(Item=item)
    print(f"[INFO] AUDIT sellhub_issue_ok order={order_id} key_suffix={key_suffix(new_key)} tier={tier} discord={discord_id or '-'}")
    delivered = False
    if discord_id and str(discord_id).isdigit():
        delivered = discord_dm(discord_id, license_key_embed(new_key, tier, expiry))
        discord_set_role(discord_id, DISCORD_CUSTOMER_ROLE_SSM, add=True)
        if tier == "lifetime":
            discord_set_role(discord_id, DISCORD_LIFETIME_ROLE_SSM, add=True)
    if not delivered:
        print(f"[WARNING] sellhub_issue undelivered order={order_id} (no/invalid discord_id or DMs blocked) — key minted, deliver manually")
    return ok_res({"ok": True, "delivered": delivered, "tier": tier})

def sellhub_revoke(order_id):
    ptr = licenses_table.get_item(Key={"license_key": "ORDER#" + order_id}).get("Item")
    if not ptr or not ptr.get("issued_key"):
        print(f"[WARNING] sellhub_revoke no-pointer order={order_id}")
        return ok_res({"ok": True, "note": "no license for order"})
    key = ptr["issued_key"]
    licenses_table.update_item(
        Key={"license_key": key},
        UpdateExpression="SET revoked = :t, #st = :r, machine_id = :m",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":t": True, ":r": "revoked", ":m": ""})
    print(f"[INFO] AUDIT sellhub_revoke_ok order={order_id} key_suffix={key_suffix(key)}")
    did = ptr.get("discord_user_id", "")
    if did and str(did).isdigit():
        discord_dm(did, {"title": "⚠ Orion License Revoked",
                         "description": "The Orion license from a refunded/disputed order has been revoked. If this is a mistake, open a support ticket.",
                         "color": 2450411, "footer": {"text": "Orion"}})
        discord_set_role(did, DISCORD_CUSTOMER_ROLE_SSM, add=False)
    return ok_res({"ok": True, "revoked": True})

def handle_sellhub_webhook(raw_body, body, headers):
    if not verify_sellhub_signature(raw_body, headers):
        print("[WARNING] AUDIT sellhub_sig_fail")
        return err_res(403, "bad_signature", "Invalid webhook signature.")
    data       = body.get("data") if isinstance(body.get("data"), dict) else body
    event      = body.get("event") or body.get("type") or ""
    status     = data.get("status") or ""
    order_id   = str(data.get("order_id") or data.get("invoice_id") or data.get("id") or "").strip()
    product    = data.get("product_name") or data.get("product") or data.get("title") or ""
    cust       = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    discord_id = str(data.get("discord_id") or data.get("discord_user_id")
                     or cust.get("discord_id") or cust.get("discord_user_id") or "").strip()
    email      = data.get("email") or cust.get("email") or ""
    if not order_id:
        return err_res(400, "missing_order", "order_id missing from webhook payload.")
    if _is_refund(event, status):
        return sellhub_revoke(order_id)
    if _is_paid(event, status):
        return sellhub_issue(order_id, product, discord_id, email)
    print(f"[INFO] sellhub_webhook ignored event={event} status={status} order={order_id}")
    return ok_res({"ok": True, "ignored": str(event or status)})
```

---

## /hwid-reset — self-service HWID reset (additional setup)
The code is already in `backend/lambda_function.py` (deploy it with the SellHub changes — it adds routes `/api/discord/interactions` + `/api/discord/register-commands` and an embedded Ed25519 verifier; **no new Python deps / no Lambda layer**). Then:

1. **SSM:** add a SecureString `/orion/discord_public_key` = the app's **Public Key** (Discord Dev Portal → your app → General Information → Public Key — a 64-char hex string). *(The bot token + guild id from the main setup are reused.)*
2. **Interactions Endpoint URL:** Dev Portal → General Information → "Interactions Endpoint URL" = `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/discord/interactions` → Save. Discord sends a validation PING; it should save with a green check. *(Fails = wrong public key in SSM, or the Lambda isn't deployed yet.)*
3. **Register the command (the OWNER runs this once — needs the admin secret; keep it private):**
   ```
   curl -X POST "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/discord/register-commands" \
        -H "X-Orion-Admin-Secret: <ADMIN_SECRET>"
   ```
   Expect `{"ok": true, "message": "/hwid-reset registered for the guild."}` (guild command = instant).
4. **Test** in the server: `/hwid-reset key:<a license bound to your Discord>` → ephemeral "✅ Done — your license is unbound…". Verify: someone else's key → "isn't linked to your account"; immediate re-run → "try again in 24h"; after 3 in 30 days → "open a ticket". CloudWatch logs `AUDIT hwid_reset_ok`.

Behavior: ownership-checked, rate-limited (24h cooldown, max 3/30 days in DynamoDB), ephemeral, no role needed.
