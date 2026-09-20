"""Gumroad Ping webhook — provisioning, refunds/disputes, and HWID-reset credits.

Event kinds handled (docs/ADMIN_PANEL_V2_CONTRACT.md §3 "Paid credit path", §6):
  purchase      mint a license (or, for the HWID-reset product, grant a paid credit)
  refund        revoke the license          (`refunded=true`)
  dispute       revoke + alert the owner    (`disputed=true`)
  chargeback    revoke + alert the owner    (`chargebacked=true` / `chargeback=true`)
  dispute_won   UN-revoke + alert the owner (`dispute_won=true`)

Owner alerts are a Discord DM sent with the BOT TOKEN from SSM
`/orion/discord_bot_token` (the same token the delivery DM uses — this Lambda has
no gateway connection, so it talks to Discord's REST API directly). The recipient
is env `OWNER_DISCORD_USER_ID`, falling back to SSM `/orion/owner_discord_user_id`.
Contract §4 puts `alerts.owner_discord_user_id` in the Lambda's `orion-config`
table, but no route exposes it to a webhook, so it is configured here (gap listed
in the handoff). Alerts NEVER block the operation; failures print `alert_failed`.

ENV (all optional; defaults in the constants below):
  GUMROAD_HWID_RESET_PRODUCT   permalink slug of the paid "HWID reset" product
  OWNER_DISCORD_USER_ID        owner's Discord user id for alerts
  ORION_API_BASE               API origin (default: the live execute-api origin)
"""
import json
import os
import time
import hmac
import base64
import urllib.request
import urllib.error
import urllib.parse
import boto3
from botocore.exceptions import ClientError

# ── Clients ──────────────────────────────────────────────────────────────────
ssm = boto3.client("ssm", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

_secret_cache: dict = {}

def get_secret(name: str) -> str:
    if name not in _secret_cache:
        resp_ = ssm.get_parameter(Name=name, WithDecryption=True)
        _secret_cache[name] = resp_["Parameter"]["Value"]
    return _secret_cache[name]

# ── Pre-load secrets at cold start to avoid first-request failures ──────────
try:
    get_secret("/orion/gumroad_webhook_token")
    get_secret("/orion/bot_service_secret")
    get_secret("/orion/discord_bot_token")
except Exception as _e:
    print(f"[WARN] Cold-start secret pre-load failed: {_e}")

# ── DynamoDB tables ───────────────────────────────────────────────────────────
events_table   = dynamodb.Table("orion-gumroad-events")   # idempotency, PK event_key
orders_table   = dynamodb.Table("orion-gumroad-orders")   # revoke lookups, PK sale_id
licenses_table = dynamodb.Table("orion-licenses")         # shared with the SellHub path

# ── Account identity + endpoints ──────────────────────────────────────────────
GUMROAD_SELLER_ID = "4-7tKV7OFXGk7mm0h87eFQ=="  # cross-check constant, not a secret
API_BASE      = os.environ.get("ORION_API_BASE", "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com").rstrip("/")
PROVISION_URL = API_BASE + "/api/bot/provision"
# contract §3 "Paid credit path": grants hwid_paid_credits += 1 on the customer's
# newest active key. ASSUMED route — not in the endpoint list (§2); see handoff.
HWID_CREDIT_URL = API_BASE + "/api/bot/hwid-credit"
DISCORD_API   = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://zaeorion.com, 1.0)"
LICENSE_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"

# LEGACY: the paid "HWID reset" product. A sale grants a reset credit instead of
# minting a license, so it is deliberately NOT in PRODUCT_MAP. Owner rule
# 2026-09-15 retired it from the storefront — resets are 3 free then 1 day off the
# subscription — but the handler stays so an in-flight sale still lands, and so
# staff can keep granting credits through /api/bot/hwid-credit. Set
# GUMROAD_HWID_RESET_PRODUCT="" to switch the path off entirely.
HWID_RESET_PRODUCT = os.environ.get("GUMROAD_HWID_RESET_PRODUCT", "orion-hwid-reset").strip()

# Legacy activation-fee support is opt-in only. The live storefront has one
# product: the $25/month membership, with no first-purchase surcharge.
ACTIVATION_PRODUCT = os.environ.get("GUMROAD_ACTIVATION_PRODUCT", "").strip()

# Owner-alert recipient. Env first so it can be changed without a redeploy of SSM.
OWNER_DISCORD_USER_ID_ENV = os.environ.get("OWNER_DISCORD_USER_ID", "").strip()

# Event kinds that revoke, and the revoke_reason each writes. Only a license whose
# revoke_reason is one of these may be un-revoked by a `dispute_won` ping — an
# owner/staff revoke (which writes kill_reason) is never undone from here.
REVOKING_KINDS = {"refund": "refunded", "dispute": "disputed", "chargeback": "chargebacked"}
ALERTING_KINDS = {"dispute", "chargeback", "dispute_won"}   # contract §4 webhook.chargeback

# ── Product map (Gumroad product_permalink → plan metadata) ──────────────────
# `orion-monthly` is the ONLY product sold as of the owner rule 2026-09-15: a
# Gumroad MEMBERSHIP at $25/month whose ping fires on every charge. Its plan is
# `month` so the backend's TIER_DAYS/lifetime logic and the bot's copy all agree.
# Everything below it is legacy and stays ONLY so a refund/dispute ping on a key
# already sold still resolves to a plan. None of them is offered anywhere.
MEMBERSHIP_PRODUCT = "orion-monthly"
LICENSELESS_PRODUCTS = {
    slug.strip()
    for slug in os.environ.get("GUMROAD_LICENSELESS_PRODUCTS", MEMBERSHIP_PRODUCT).split(",")
    if slug.strip()
}
PRODUCT_MAP = {
    "orion-monthly":  {"plan": "month",    "days": 30,   "slug": "ORION_MONTH"},
    # ── legacy, not sold ──
    "orion-beta":     {"plan": "beta",     "days": 30,   "slug": "ORION_BETA"},
    "orion-1day":     {"plan": "1day",     "days": 1,    "slug": "ORION_1D"},
    "orion-3day":     {"plan": "3day",     "days": 3,    "slug": "ORION_3D"},
    "orion-7day":     {"plan": "7day",     "days": 7,    "slug": "ORION_7D"},
    "orion-14day":    {"plan": "14day",    "days": 14,   "slug": "ORION_14D"},
    "orion-30day":    {"plan": "30day",    "days": 30,   "slug": "ORION_30D"},
    "orion-120day":   {"plan": "120day",   "days": 120,  "slug": "ORION_120D"},
    "orion-lifetime": {"plan": "lifetime", "days": None, "slug": "ORION_LIFETIME"},
}

# Plans that renew rather than re-mint when a recurring membership charge lands.
RENEWABLE_PLANS = {"month", "30day"}

DISCORD_ID_ALIASES = {"discord_id", "discord_user_id", "discorduserid"}

# ── Helpers ────────────────────────────────────────────────────────────────────
def resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}

def _urlopen(req, timeout: int = 10):
    """Single egress seam — every outbound HTTP call in this module goes through
    it, so tests stub one name instead of patching urllib globally."""
    return urllib.request.urlopen(req, timeout=timeout)

def _form_flag(form: dict, *names) -> bool:
    """Gumroad sends booleans as the strings "true"/"false"."""
    for n in names:
        if str(form.get(n, "")).strip().lower() == "true":
            return True
    return False

def classify_event(form: dict) -> str:
    """-> purchase | refund | dispute | chargeback | dispute_won.

    Gumroad sends `disputed=true` together with `dispute_won=true` when a dispute
    resolves in the seller's favour, so the resolution is checked FIRST."""
    if _form_flag(form, "dispute_won"):
        return "dispute_won"
    if _form_flag(form, "chargebacked", "chargeback"):
        return "chargeback"
    if _form_flag(form, "disputed"):
        return "dispute"
    if _form_flag(form, "refunded"):
        return "refund"
    return "purchase"

def is_recurring_charge(form: dict) -> bool:
    """True for the 2nd and later charges of a Gumroad membership.

    Gumroad marks a renewal with `is_recurring_charge=true` (the FIRST charge of a
    subscription does not carry it). `recurrence` alone is not enough: it is set on
    the initial charge too, which must still MINT."""
    return _form_flag(form, "is_recurring_charge", "recurring_charge")


def get_path_token(event: dict) -> str:
    params = event.get("pathParameters") or {}
    return params.get("token", "")

def parse_form_body(event: dict) -> dict:
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode()
    return dict(urllib.parse.parse_qsl(raw_body, keep_blank_values=True))

def extract_permalink_slug(raw: str) -> str:
    """Gumroad's Ping `product_permalink` field is the full product URL
    (e.g. https://seller.gumroad.com/l/orion-beta), not the bare slug the
    rest of this file matches against -- normalize it to the trailing slug."""
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        path = urllib.parse.urlparse(raw).path
        return path.rstrip("/").rsplit("/", 1)[-1]
    return raw

def get_discord_user_id_from_form(form: dict) -> str:
    """Gumroad flattens custom checkout fields to top-level keys named after the
    field's exact label -- the required field is labeled 'Discord ID' verbatim."""
    if "Discord ID" in form:
        return form["Discord ID"].strip()
    for k, v in form.items():
        if k.lower().replace("-", "_").replace(" ", "_") in DISCORD_ID_ALIASES:
            return str(v).strip()
    return ""

def verify_license_key(license_key: str, product_permalink: str, product_id: str,
                       increment: bool = True, enforce_uses: bool = True) -> bool:
    # HIGH-2: increment_uses_count="true" so Gumroad's own counter advances on
    # every verify. Combined with the uses>1 reject below and the license_key
    # backend idempotency, a replayed ping cannot re-mint on one purchased key.
    #
    # BUT that guard is purchase-only. A refund/dispute ping re-uses the SAME
    # license_key, so with increment+enforce on it would read uses=2 and 401 —
    # which is why refunds never actually revoked before. Revoke-type events
    # therefore verify WITHOUT incrementing and WITHOUT the uses ceiling: they
    # only need to prove the key is genuinely Gumroad's, and they mint nothing.
    payload = {"license_key": license_key}
    if increment:
        payload["increment_uses_count"] = "true"
    if product_id:
        payload["product_id"] = product_id
    elif product_permalink:
        payload["product_permalink"] = product_permalink
    else:
        return False
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(LICENSE_VERIFY_URL, data=data, method="POST")
    try:
        with _urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("success"):
                return False
            if not enforce_uses:
                return True
            # Reject a key that has already been consumed (uses>1 == replay).
            uses = result.get("uses")
            if uses is None:
                uses = (result.get("purchase") or {}).get("uses")
            try:
                if uses is not None and int(uses) > 1:
                    print(f"[WARN] license_key uses>1 (replay) uses={uses}")
                    return False
            except (TypeError, ValueError):
                pass
            return True
    except urllib.error.HTTPError as e:
        print(f"[WARN] License verify HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"[WARN] License verify error: {e}")
        return False

def discord_dm_embed(discord_user_id: str, embed: dict):
    """Open a DM channel with the bot token and post one embed. Raises on failure;
    every caller decides whether that is fatal (delivery) or not (alerts)."""
    token = get_secret("/orion/discord_bot_token")
    dm_url = f"{DISCORD_API}/users/@me/channels"
    dm_payload = json.dumps({"recipient_id": str(discord_user_id)}).encode()
    dm_req = urllib.request.Request(dm_url, data=dm_payload, method="POST")
    dm_req.add_header("Authorization", f"Bot {token}")
    dm_req.add_header("Content-Type", "application/json")
    dm_req.add_header("User-Agent", DISCORD_USER_AGENT)
    try:
        with _urlopen(dm_req, timeout=10) as r:
            channel = json.loads(r.read())
            channel_id = channel["id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"DM channel open failed {e.code}: {body}")

    msg_url = f"{DISCORD_API}/channels/{channel_id}/messages"
    msg_payload = json.dumps({"embeds": [embed]}).encode()
    msg_req = urllib.request.Request(msg_url, data=msg_payload, method="POST")
    msg_req.add_header("Authorization", f"Bot {token}")
    msg_req.add_header("Content-Type", "application/json")
    msg_req.add_header("User-Agent", DISCORD_USER_AGENT)
    try:
        with _urlopen(msg_req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"DM send failed {e.code}: {body}")


def set_customer_role(discord_user_id: str, add: bool) -> bool:
    """Best-effort paid-role synchronization for purchase/revoke events."""
    if not discord_user_id:
        return False
    try:
        token = get_secret("/orion/discord_bot_token")
        guild_id = get_secret("/orion/discord_guild_id").strip()
        role_id = get_secret("/orion/discord_customer_role_id").strip()
        if not guild_id or not role_id:
            raise RuntimeError("Discord guild/customer role parameter is empty")
        req = urllib.request.Request(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
            data=b"" if add else None,
            method="PUT" if add else "DELETE")
        req.add_header("Authorization", "Bot " + token)
        req.add_header("User-Agent", DISCORD_USER_AGENT)
        if add:
            req.add_header("Content-Length", "0")
        with _urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[WARN] customer role {'grant' if add else 'remove'} failed: {e}")
        return False


def send_discord_dm(discord_user_id: str, license_key: str, plan: str, days):
    duration_str = "Lifetime" if days is None else f"{days} day{'s' if days != 1 else ''}"
    # Customer-facing copy uses the Venice brand (rebrand 2026-08-04). The
    # orion-* product slugs in PRODUCT_MAP stay as-is — they're the join key
    # Gumroad pings match on, and renaming them breaks delivery.
    return discord_dm_embed(discord_user_id, {
        "title": "🎮 Venice License Activated",
        "description": f"Your **{plan}** license ({duration_str}) is ready.",
        "fields": [
            {"name": "License Key", "value": f"||`{license_key}`||", "inline": False},
            {"name": "Support", "value": "DM a staff member or open a ticket if you have issues.", "inline": False}
        ],
        "color": 0x2563EB,
        "footer": {"text": "Venice — do not share your key"}
    })


def send_renewal_dm(discord_user_id: str, days):
    """A recurring charge extends the key the customer already has — so the DM must
    NOT resend a key (there is no new one) and must not read like a fresh purchase."""
    return discord_dm_embed(discord_user_id, {
        "title": "🔁 Venice subscription renewed",
        "description": (f"Thanks — another **{days} days** have been added to the key you "
                        "already have. Nothing to install, nothing to re-paste.\n\n"
                        "Run **`/lookup`** in the server to see your new expiry."),
        "color": 0x2563EB,
        "footer": {"text": "Venice — do not share your key"}
    })


def send_activation_fee_dm(discord_user_id: str):
    """The activation fee mints nothing, so tell the buyer what is still outstanding
    rather than leaving them waiting for a key that is never coming."""
    return discord_dm_embed(discord_user_id, {
        "title": "✅ Activation fee received",
        "description": ("Your one-time activation fee is recorded.\n\n"
                        "The **monthly subscription** is a separate checkout — once that "
                        "payment clears your license key is DM'd here automatically. "
                        "Run **`/purchase`** in the server for the link."),
        "color": 0x2563EB,
        "footer": {"text": "Venice — activation fee, no key issued for this item"}
    })


def send_hwid_credit_dm(discord_user_id: str):
    return discord_dm_embed(discord_user_id, {
        "title": "🔓 HWID reset credit added",
        "description": ("Your paid HWID reset is on your account.\n\n"
                        "Run **`/hwid_reset`** in the Discord server and it will use the credit — "
                        "no time is deducted from your subscription."),
        "color": 0x2563EB,
        "footer": {"text": "Venice — one credit, one reset"}
    })


def owner_discord_user_id() -> str:
    """env OWNER_DISCORD_USER_ID, else SSM /orion/owner_discord_user_id, else ''.
    Contract §4 keeps `alerts.owner_discord_user_id` in orion-config, but no route
    exposes it to a webhook — see the handoff's gap list."""
    if OWNER_DISCORD_USER_ID_ENV:
        return OWNER_DISCORD_USER_ID_ENV
    try:
        return get_secret("/orion/owner_discord_user_id").strip()
    except Exception:
        return ""


def alert_owner(title: str, description: str, fields=None, level: str = "warn"):
    """Contract §4: owner alerts NEVER block the operation. Any failure — no
    recipient configured, closed DMs, Discord 5xx — is printed as `alert_failed`
    and swallowed. Returns True when the DM went out."""
    owner_id = owner_discord_user_id()
    if not owner_id:
        print("[WARN] AUDIT alert_failed reason=no_owner_discord_user_id "
              "(set env OWNER_DISCORD_USER_ID or SSM /orion/owner_discord_user_id)")
        return False
    try:
        discord_dm_embed(owner_id, {
            "title": title,
            "description": description,
            "fields": fields or [],
            "color": 0xDC2626 if level == "warn" else 0x2563EB,
            "footer": {"text": "Venice — Gumroad webhook alert"},
        })
        return True
    except Exception as e:
        print(f"[WARN] AUDIT alert_failed title={title!r}: {e}")
        return False

def _webhook_bot_secret() -> str:
    # HIGH-3: per-consumer secret. Prefer the webhook-scoped param; fall back to
    # the legacy shared secret so this keeps working until the new param is set.
    try:
        return get_secret("/orion/webhook_bot_secret")
    except Exception:
        return get_secret("/orion/bot_service_secret")

def _bot_request(url: str, payload: dict, what: str):
    """POST a bot-secret + edge-auth server-to-server call. Returns parsed JSON;
    raises RuntimeError with `what` in the message on any failure."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Orion-Bot-Secret", _webhook_bot_secret())
    # HIGH-4: /api/bot/* is now behind edge auth — present the edge secret so this
    # server-to-server call is accepted.
    try:
        req.add_header("X-Edge-Auth", get_secret("/orion/edge_auth_secret"))
    except Exception as _e:
        print(f"[WARN] edge secret unavailable for {what} call: {_e}")
    try:
        with _urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"{what} HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"{what} error: {e}")


def provision_license(plan: str, days, discord_user_id: str, order_id: str,
                      renew: bool = False) -> dict:
    """Mint (or, with renew=True, EXTEND) the customer's key.

    `renew` is what makes the monthly membership recurring: the backend extends the
    customer's existing key by `days` instead of minting a second one, so the key
    in their delivery DM keeps working month after month. Returns the whole reply
    so the caller can see `renewed`."""
    payload = {
        "plan": plan,
        "days": days,
        "discord_user_id": discord_user_id,
        "order_id": order_id,
    }
    if renew:
        payload["renew"] = True
    data = _bot_request(PROVISION_URL, payload, "provision")
    if not data.get("license_key"):
        raise RuntimeError("provision returned no license_key")
    return data


def grant_hwid_credit(discord_user_id: str, order_id: str) -> dict:
    """Contract §3 "Paid credit path": +1 hwid_paid_credits on the customer's
    newest active key. `order_id` makes it spend-once server-side, the same way
    /api/bot/provision claims an order."""
    data = _bot_request(HWID_CREDIT_URL, {
        "discord_user_id": discord_user_id,
        "order_id": order_id,
    }, "hwid_credit")
    if not data.get("ok", True):
        raise RuntimeError(f"hwid_credit refused: {data.get('error') or data}")
    return data

# ── Event handlers ─────────────────────────────────────────────────────────────
# Contract §6 defines the OUTCOME (status=revoked + revoke_reason + audit
# webhook.chargeback + owner alert) but §2 defines no backend route for it, so
# these write `orion-licenses` directly, exactly as the refund path always has.
# Swap `_revoke_license` / `_unrevoke_license` for a bot route the day one exists.

def _revoke_license(license_key: str, reason: str):
    licenses_table.update_item(
        Key={"license_key": license_key},
        UpdateExpression="SET #s = :revoked, revoked = :r, revoked_at = :ts, revoke_reason = :reason",
        ExpressionAttributeNames={"#s": "status"},
        # Both `status` AND the `revoked` boolean: backend/lambda_function.py
        # checks them separately (activate reads `revoked`, validate reads both),
        # and the old status-only write left `revoked` false on a refunded key.
        ExpressionAttributeValues={":revoked": "revoked", ":r": True,
                                   ":ts": int(time.time()), ":reason": reason})


def _unrevoke_license(license_key: str):
    licenses_table.update_item(
        Key={"license_key": license_key},
        UpdateExpression=("SET #s = :active, revoked = :r, unrevoked_at = :ts "
                          "REMOVE revoked_at, revoke_reason"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":active": "active", ":r": False, ":ts": int(time.time())})


def _handle_revoke(kind: str, sale_id: str, email: str):
    reason = REVOKING_KINDS[kind]
    try:
        existing = orders_table.get_item(Key={"sale_id": sale_id}).get("Item")
        if not existing:
            # A dispute on a sale we never provisioned is still owner news.
            if kind in ALERTING_KINDS:
                alert_owner(f"⚠️ Gumroad {kind} — no order on file",
                            f"A **{kind}** arrived for a sale this webhook never provisioned. "
                            "Nothing was revoked; check Gumroad manually.",
                            [{"name": "sale_id", "value": f"`{sale_id}`", "inline": True},
                             {"name": "email", "value": email or "unknown", "inline": True}])
            print(f"[INFO] AUDIT webhook.{kind} sale_id={sale_id} result=no_existing_order_to_revoke")
            return resp(200, {"skipped": "no_existing_order_to_revoke", "kind": kind})

        existing_license_key = existing.get("license_key", "")
        if existing_license_key:
            _revoke_license(existing_license_key, reason)
            set_customer_role(str(existing.get("discord_user_id") or ""), False)
        elif existing.get("plan") == "hwid_reset":
            # A refunded/disputed HWID-reset CREDIT: there is no license to
            # revoke and no route to decrement hwid_paid_credits (contract §3
            # defines the grant, not the claw-back). Mark the order and tell the
            # owner; a credit already spent is a reset already taken anyway.
            print(f"[WARN] AUDIT webhook.{kind} hwid_credit_not_clawed_back sale_id={sale_id}")
        elif existing.get("plan") == "activation_fee":
            # The activation fee never minted a key, so there is nothing to
            # revoke. The membership is a separate sale with its own ping.
            print(f"[INFO] AUDIT webhook.{kind} activation_fee_no_license sale_id={sale_id}")
        orders_table.update_item(
            Key={"sale_id": sale_id},
            UpdateExpression="SET #s = :revoked, revoked_at = :ts, revoke_reason = :reason",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":revoked": "revoked", ":ts": int(time.time()), ":reason": reason})
        suffix = existing_license_key[-8:] if existing_license_key else ""
        print(f"[INFO] AUDIT webhook.{kind} gumroad_revoke_ok sale_id={sale_id} license=...{suffix}")
    except Exception as e:
        print(f"[ERROR] Revoke failed ({kind}): {e}")
        return resp(500, {"error": "revoke_failed"})

    if kind in ALERTING_KINDS:
        alert_owner(f"💳 Gumroad {kind} — license revoked",
                    f"A **{kind}** was received and the license has been revoked automatically.",
                    [{"name": "sale_id", "value": f"`{sale_id}`", "inline": True},
                     {"name": "license", "value": f"…{suffix}" if suffix else "none", "inline": True},
                     {"name": "discord", "value": str(existing.get("discord_user_id") or "unknown"), "inline": True},
                     {"name": "email", "value": email or "unknown", "inline": False}])
    return resp(200, {"revoked": True, "sale_id": sale_id, "kind": kind, "reason": reason})


def _handle_dispute_won(sale_id: str, email: str):
    """Seller won the dispute -> put the customer back in business. Only undoes a
    revoke THIS webhook made (revoke_reason in REVOKING_KINDS.values()); an owner
    or staff revoke writes kill_reason instead and is never touched from here."""
    try:
        existing = orders_table.get_item(Key={"sale_id": sale_id}).get("Item")
        if not existing:
            print(f"[INFO] AUDIT webhook.dispute_won sale_id={sale_id} result=no_existing_order")
            return resp(200, {"skipped": "no_existing_order", "kind": "dispute_won"})
        license_key = existing.get("license_key", "")
        lic = licenses_table.get_item(Key={"license_key": license_key}).get("Item") if license_key else None
        prior = str((lic or {}).get("revoke_reason") or "")
        if not lic or prior not in set(REVOKING_KINDS.values()):
            alert_owner("⚖️ Gumroad dispute won — NOT un-revoked",
                        ("The dispute resolved in your favour, but the license was not revoked by this "
                         f"webhook (revoke_reason={prior or 'none'}), so it was left alone. "
                         "Restore it from OrionOwner.exe if that's wrong."),
                        [{"name": "sale_id", "value": f"`{sale_id}`", "inline": True},
                         {"name": "license", "value": f"…{license_key[-8:]}" if license_key else "none", "inline": True}])
            print(f"[INFO] AUDIT webhook.dispute_won sale_id={sale_id} result=left_alone prior={prior or 'none'}")
            return resp(200, {"unrevoked": False, "reason": "not_webhook_revoked", "sale_id": sale_id})

        _unrevoke_license(license_key)
        set_customer_role(str(existing.get("discord_user_id") or ""), True)
        orders_table.update_item(
            Key={"sale_id": sale_id},
            UpdateExpression="SET #s = :active, unrevoked_at = :ts REMOVE revoked_at, revoke_reason",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":active": "active", ":ts": int(time.time())})
        print(f"[INFO] AUDIT webhook.dispute_won gumroad_unrevoke_ok sale_id={sale_id} license=...{license_key[-8:]}")
    except Exception as e:
        print(f"[ERROR] Un-revoke failed: {e}")
        return resp(500, {"error": "unrevoke_failed"})

    alert_owner("✅ Gumroad dispute won — license restored",
                "The dispute resolved in your favour; the license was un-revoked automatically.",
                [{"name": "sale_id", "value": f"`{sale_id}`", "inline": True},
                 {"name": "license", "value": f"…{license_key[-8:]}", "inline": True},
                 {"name": "email", "value": email or "unknown", "inline": False}],
                level="info")
    return resp(200, {"unrevoked": True, "sale_id": sale_id, "kind": "dispute_won"})


def _handle_hwid_reset_purchase(form: dict, sale_id: str, license_key: str, is_test: bool):
    """Contract §3 "Paid credit path" — a sale of GUMROAD_HWID_RESET_PRODUCT grants
    one paid reset credit instead of minting a license."""
    discord_user_id = get_discord_user_id_from_form(form)
    if not discord_user_id:
        return resp(422, {"error": "missing discord_user_id"})

    # Same HIGH-2 idempotency as the mint path, keyed on the genuine Gumroad key:
    # one credit per purchased key, ever, whatever sale_id a replay invents.
    lickey_event = f"lickey:{license_key}"
    try:
        events_table.put_item(
            Item={"event_key": lickey_event, "sale_id": sale_id, "kind": "hwid_credit",
                  "license_suffix": license_key[-6:], "ts": int(time.time()), "status": "credited"},
            ConditionExpression="attribute_not_exists(event_key)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[WARN] hwid credit already granted for ...{license_key[-6:]}")
            return resp(200, {"skipped": True, "reason": "credit_already_granted"})
        raise

    try:
        grant_hwid_credit(discord_user_id, sale_id)
    except Exception as e:
        print(f"[ERROR] hwid credit failed: {e}")
        # Release the claim so Gumroad's retry re-attempts (same rule as V3 MED
        # on the mint path: never orphan something the customer paid for).
        try:
            events_table.delete_item(Key={"event_key": lickey_event})
        except Exception as del_e:
            print(f"[ERROR] failed to release hwid credit claim: {del_e}")
        return resp(500, {"error": "hwid_credit_failed"})

    dm_status = "delivered"
    try:
        send_hwid_credit_dm(discord_user_id)
    except Exception as e:
        dm_status = "failed"
        print(f"[ERROR] hwid credit DM failed: {e}")

    orders_table.put_item(Item={
        "sale_id": sale_id,
        "discord_user_id": discord_user_id,
        "license_key": "",              # no license minted — this is a credit
        "plan": "hwid_reset",
        "status": "credited",
        "test": is_test,
        "created_at": int(time.time()),
    })
    print(f"[INFO] AUDIT gumroad_hwid_credit_ok sale_id={sale_id} discord={discord_user_id} dm_status={dm_status}")
    return resp(200, {"success": True, "sale_id": sale_id, "plan": "hwid_reset",
                      "hwid_credit": True, "dm_status": dm_status})


def _handle_activation_fee(form: dict, sale_id: str, license_key: str, is_test: bool):
    """The one-time activation fee. It mints NOTHING: the membership ping is the
    only thing that creates or renews a key. Recorded so refunds and support have a
    row to look at, and DM'd so the buyer knows a key is not coming from this item."""
    discord_user_id = get_discord_user_id_from_form(form)

    # Same HIGH-2 idempotency as every other path, keyed on the genuine Gumroad key.
    lickey_event = f"lickey:{license_key}"
    try:
        events_table.put_item(
            Item={"event_key": lickey_event, "sale_id": sale_id, "kind": "activation_fee",
                  "license_suffix": license_key[-6:], "ts": int(time.time()), "status": "recorded"},
            ConditionExpression="attribute_not_exists(event_key)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[WARN] activation fee already recorded for ...{license_key[-6:]}")
            return resp(200, {"skipped": True, "reason": "activation_fee_already_recorded"})
        raise

    dm_status = "skipped"
    if discord_user_id:
        dm_status = "delivered"
        try:
            send_activation_fee_dm(discord_user_id)
        except Exception as e:
            dm_status = "failed"
            print(f"[ERROR] activation fee DM failed: {e}")

    orders_table.put_item(Item={
        "sale_id": sale_id,
        "discord_user_id": discord_user_id,
        "license_key": "",              # no license minted — this is a fee
        "plan": "activation_fee",
        "status": "paid",
        "test": is_test,
        "created_at": int(time.time()),
    })
    print(f"[INFO] AUDIT gumroad_activation_fee_ok sale_id={sale_id} discord={discord_user_id} dm_status={dm_status}")
    return resp(200, {"success": True, "sale_id": sale_id, "plan": "activation_fee",
                      "license_issued": False, "dm_status": dm_status})


# ── Main handler ───────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    # ── Layer 1: URL-embedded token (the Ping URL path itself is the secret) ──
    # NEW-3: timing-safe compare (was `!=`, a remote timing oracle on the only
    # auth factor gating the HIGH-2 exploit).
    token = get_path_token(event)
    expected_token = get_secret("/orion/gumroad_webhook_token")
    if not token or not hmac.compare_digest(token, expected_token):
        return resp(403, {"error": "invalid_token"})

    form = parse_form_body(event)

    sale_id = form.get("sale_id", "")
    seller_id = form.get("seller_id", "")
    kind = classify_event(form)                      # purchase|refund|dispute|chargeback|dispute_won
    is_test = form.get("test", "").lower() == "true"
    product_permalink = extract_permalink_slug(form.get("product_permalink") or form.get("permalink", ""))
    product_id = form.get("product_id", "")
    license_key = form.get("license_key", "")
    email = form.get("email", "")

    print(f"[INFO] sale_id={sale_id} product_permalink={product_permalink} kind={kind} test={is_test} email={email}")

    if not sale_id:
        return resp(400, {"error": "missing_sale_id"})

    # ── Layer 2: seller_id cross-check ──────────────────────────────────────
    if seller_id != GUMROAD_SELLER_ID:
        print(f"[WARN] seller_id_mismatch: got {seller_id!r}")
        return resp(401, {"error": "seller_id_mismatch"})

    # ── Layer 3: Gumroad proof ──────────────────────────────────────────────
    # Only a PURCHASE spends a use. See verify_license_key: incrementing on a
    # refund/dispute ping would push uses to 2 and 401 the very event that is
    # supposed to revoke.
    #
    # A RECURRING membership charge re-uses the same license_key as the first
    # charge, whose verify already pushed uses to 1. Incrementing + enforcing on a
    # renewal would read uses=2 and 401 the very ping that keeps the subscription
    # alive — the same trap refunds fell into. Renewals therefore verify like a
    # revoke ping: prove the key is Gumroad's, mint nothing new. Replay protection
    # for them is the URL token + seller_id + the (sale_id, kind) event marker.
    is_purchase = kind == "purchase"
    recurring = is_purchase and is_recurring_charge(form)
    verify_strict = is_purchase and not recurring
    if license_key:
        if not verify_license_key(license_key, product_permalink, product_id,
                                  increment=verify_strict, enforce_uses=verify_strict):
            print(f"[WARN] license_verify_failed sale_id={sale_id} kind={kind}")
            return resp(401, {"error": "license_verify_failed"})
    elif product_permalink not in LICENSELESS_PRODUCTS:
        # Gumroad's current Membership editor does not expose the legacy
        # generate-license-key switch. Only explicitly allow-listed slugs may
        # use the high-entropy webhook-path token + seller-id proof instead.
        # One-off and legacy products still fail closed without Gumroad's key.
        print(f"[WARN] missing_license_key sale_id={sale_id} product={product_permalink!r}")
        return resp(401, {"error": "missing_license_key"})
    else:
        print(f"[INFO] AUDIT license_less_membership_verified sale_id={sale_id} "
              f"product={product_permalink} kind={kind}")

    # Purchase, refund and dispute pings re-use the SAME sale_id, so idempotency
    # must be keyed by (sale_id, event kind) -- otherwise the refund re-POST would
    # be silently swallowed as a "duplicate" of the original purchase event.
    event_key = f"{sale_id}:{kind}"
    try:
        events_table.put_item(
            Item={"event_key": event_key, "sale_id": sale_id, "kind": kind,
                  "ts": int(time.time()), "status": "processing"},
            ConditionExpression="attribute_not_exists(event_key)"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return resp(200, {"skipped": True, "reason": "duplicate_event"})
        raise

    # ── Revoke path: refund / dispute / chargeback (contract §6) ─────────────
    if kind in REVOKING_KINDS:
        return _handle_revoke(kind, sale_id, email)

    # ── Un-revoke path: the seller won the dispute ───────────────────────────
    if kind == "dispute_won":
        return _handle_dispute_won(sale_id, email)

    # ── Activation fee: a one-time charge that mints NOTHING (owner rule 2026-09-15)
    if ACTIVATION_PRODUCT and product_permalink == ACTIVATION_PRODUCT:
        return _handle_activation_fee(form, sale_id, license_key, is_test)

    # ── HWID-reset product: a paid reset credit, not a license (contract §3) ──
    if HWID_RESET_PRODUCT and product_permalink == HWID_RESET_PRODUCT:
        return _handle_hwid_reset_purchase(form, sale_id, license_key, is_test)

    # ── Purchase path ──────────────────────────────────────────────────────────
    plan_meta = PRODUCT_MAP.get(product_permalink)
    if not plan_meta:
        print(f"[WARN] Unknown product_permalink={product_permalink!r} product_id={product_id!r}")
        return resp(422, {"error": f"unknown product: {product_permalink or product_id}"})

    discord_user_id = get_discord_user_id_from_form(form)
    if not discord_user_id:
        return resp(422, {"error": "missing discord_user_id"})

    # A recurring membership charge RENEWS the key the customer already has. The
    # lickey marker below deliberately does NOT apply to it: that marker is
    # "one key per Gumroad license_key EVER", and a membership re-sends the same
    # license_key every month, so honouring it here would silently swallow every
    # renewal and let the subscription lapse. (sale_id, kind) is the idempotency
    # for renewals — Gumroad issues a fresh sale_id per charge.
    renewing = recurring and plan_meta["plan"] in RENEWABLE_PLANS

    # Current Gumroad Membership products may be license-less. In that case the
    # genuine sale_id is the stable purchase identity; the secret URL token and
    # seller_id authenticate the sender and the event marker above is spend-once.
    lickey_event = f"lickey:{license_key}" if license_key else f"saleauth:{sale_id}"
    if not renewing:
        # ── HIGH-2: idempotency keyed on the genuine Gumroad license_key (NOT the
        # attacker-chosen sale_id). One backend license per Gumroad key EVER — a forged
        # ping that replays a real license_key with a fresh sale_id cannot re-mint.
        try:
            events_table.put_item(
                Item={"event_key": lickey_event, "sale_id": sale_id,
                      "license_suffix": license_key[-6:] if license_key else "",
                      "ts": int(time.time()), "status": "minted"},
                ConditionExpression="attribute_not_exists(event_key)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                reason = "license_key_already_minted" if license_key else "sale_already_minted"
                suffix = f" ...{license_key[-6:]}" if license_key else f" sale_id={sale_id}"
                print(f"[WARN] {reason}; skipping re-mint{suffix}")
                return resp(200, {"skipped": True, "reason": reason})
            raise

    try:
        result = provision_license(plan_meta["plan"], plan_meta["days"], discord_user_id,
                                   sale_id, renew=renewing)
    except Exception as e:
        # NEW-6: log detail server-side, return a generic body (no internal text).
        print(f"[ERROR] Provision failed: {e}")
        # V3 MED: the lickey idempotency marker was claimed BEFORE this mint. A transient
        # provision failure would otherwise ORPHAN A PAID KEY — Gumroad's retry ping would hit
        # the existing marker and return "already minted" without ever issuing a key. Release the
        # claim so the retry re-attempts the mint. (Idempotency against a genuine duplicate is
        # preserved: a real re-ping after a SUCCESSFUL mint still finds the marker.)
        # Both markers were claimed before provisioning. Releasing only the
        # license/sale-auth marker is insufficient because a retry with the same
        # sale_id would still be swallowed by the outer event marker.
        retry_claims = [event_key]
        if not renewing:
            retry_claims.append(lickey_event)
        for retry_key in retry_claims:
            try:
                events_table.delete_item(Key={"event_key": retry_key})
            except Exception as del_e:
                print(f"[ERROR] failed to release retry claim after provision failure: {del_e}")
        return resp(500, {"error": "provision_failed"})

    issued_license_key = result["license_key"]
    # The backend decides whether it actually renewed (it declines when the
    # customer has no extendable key) — trust its answer, not our guess.
    renewed = bool(result.get("renewed"))
    print(f"[INFO] {'Renewed' if renewed else 'Provisioned'} license ...{issued_license_key[-8:]} "
          f"plan={plan_meta['slug']} sale_id={sale_id}")

    dm_status = "delivered"
    try:
        if renewed:
            send_renewal_dm(discord_user_id, plan_meta["days"])
        else:
            send_discord_dm(discord_user_id, issued_license_key, plan_meta["plan"], plan_meta["days"])
        print(f"[INFO] Discord DM delivered to {discord_user_id}")
    except Exception as e:
        dm_status = "failed"
        print(f"[ERROR] Discord DM failed: {e}")

    orders_table.put_item(Item={
        "sale_id": sale_id,
        "discord_user_id": discord_user_id,
        "license_key": issued_license_key,
        "plan": plan_meta["plan"],
        "status": "renewed" if renewed else "active",
        "test": is_test,
        "created_at": int(time.time()),
    })

    print(f"[INFO] AUDIT gumroad_issue_ok sale_id={sale_id} plan={plan_meta['slug']} "
          f"renewed={renewed} dm_status={dm_status}")

    return resp(200, {
        "success": True,
        "sale_id": sale_id,
        "plan": plan_meta["plan"],
        "renewed": renewed,
        "dm_status": dm_status,
        "license_suffix": issued_license_key[-8:],
    })
