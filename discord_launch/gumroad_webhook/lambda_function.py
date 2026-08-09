import json
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
PROVISION_URL = "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/bot/provision"
DISCORD_API   = "https://discord.com/api/v10"
LICENSE_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"

# ── Product map (Gumroad product_permalink → plan metadata) ──────────────────
PRODUCT_MAP = {
    "orion-beta":     {"plan": "beta",     "days": 30,   "slug": "ORION_BETA"},
    "orion-1day":     {"plan": "1day",     "days": 1,    "slug": "ORION_1D"},
    "orion-3day":     {"plan": "3day",     "days": 3,    "slug": "ORION_3D"},
    "orion-7day":     {"plan": "7day",     "days": 7,    "slug": "ORION_7D"},
    "orion-14day":    {"plan": "14day",    "days": 14,   "slug": "ORION_14D"},
    "orion-30day":    {"plan": "30day",    "days": 30,   "slug": "ORION_30D"},
    "orion-120day":   {"plan": "120day",   "days": 120,  "slug": "ORION_120D"},
    "orion-lifetime": {"plan": "lifetime", "days": None, "slug": "ORION_LIFETIME"},
}

DISCORD_ID_ALIASES = {"discord_id", "discord_user_id", "discorduserid"}

# ── Helpers ────────────────────────────────────────────────────────────────────
def resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}

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

def verify_license_key(license_key: str, product_permalink: str, product_id: str) -> bool:
    # HIGH-2: increment_uses_count="true" so Gumroad's own counter advances on
    # every verify. Combined with the uses>1 reject below and the license_key
    # backend idempotency, a replayed ping cannot re-mint on one purchased key.
    payload = {"license_key": license_key, "increment_uses_count": "true"}
    if product_id:
        payload["product_id"] = product_id
    elif product_permalink:
        payload["product_permalink"] = product_permalink
    else:
        return False
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(LICENSE_VERIFY_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("success"):
                return False
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

def send_discord_dm(discord_user_id: str, license_key: str, plan: str, days):
    token = get_secret("/orion/discord_bot_token")
    dm_url = f"{DISCORD_API}/users/@me/channels"
    dm_payload = json.dumps({"recipient_id": discord_user_id}).encode()
    dm_req = urllib.request.Request(dm_url, data=dm_payload, method="POST")
    dm_req.add_header("Authorization", f"Bot {token}")
    dm_req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(dm_req, timeout=10) as r:
            channel = json.loads(r.read())
            channel_id = channel["id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"DM channel open failed {e.code}: {body}")

    duration_str = "Lifetime" if days is None else f"{days} day{'s' if days != 1 else ''}"
    # Customer-facing copy uses the Venice brand (rebrand 2026-08-04). The
    # orion-* product slugs in PRODUCT_MAP stay as-is — they're the join key
    # Gumroad pings match on, and renaming them breaks delivery.
    embed = {
        "title": "🎮 Venice License Activated",
        "description": f"Your **{plan}** license ({duration_str}) is ready.",
        "fields": [
            {"name": "License Key", "value": f"||`{license_key}`||", "inline": False},
            {"name": "Support", "value": "DM a staff member or open a ticket if you have issues.", "inline": False}
        ],
        "color": 0x2563EB,
        "footer": {"text": "Venice — do not share your key"}
    }
    msg_url = f"{DISCORD_API}/channels/{channel_id}/messages"
    msg_payload = json.dumps({"embeds": [embed]}).encode()
    msg_req = urllib.request.Request(msg_url, data=msg_payload, method="POST")
    msg_req.add_header("Authorization", f"Bot {token}")
    msg_req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(msg_req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"DM send failed {e.code}: {body}")

def _webhook_bot_secret() -> str:
    # HIGH-3: per-consumer secret. Prefer the webhook-scoped param; fall back to
    # the legacy shared secret so this keeps working until the new param is set.
    try:
        return get_secret("/orion/webhook_bot_secret")
    except Exception:
        return get_secret("/orion/bot_service_secret")

def provision_license(plan: str, days, discord_user_id: str, order_id: str) -> str:
    bot_secret = _webhook_bot_secret()
    payload = json.dumps({
        "plan": plan,
        "days": days,
        "discord_user_id": discord_user_id,
        "order_id": order_id,
    }).encode()
    req = urllib.request.Request(PROVISION_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Orion-Bot-Secret", bot_secret)
    # HIGH-4: /api/bot/* is now behind edge auth — present the edge secret so this
    # server-to-server call is accepted.
    try:
        req.add_header("X-Edge-Auth", get_secret("/orion/edge_auth_secret"))
    except Exception as _e:
        print(f"[WARN] edge secret unavailable for provision call: {_e}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data["license_key"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"provision HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"provision error: {e}")

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
    refunded = form.get("refunded", "").lower() == "true"
    is_test = form.get("test", "").lower() == "true"
    product_permalink = extract_permalink_slug(form.get("product_permalink") or form.get("permalink", ""))
    product_id = form.get("product_id", "")
    license_key = form.get("license_key", "")
    email = form.get("email", "")

    print(f"[INFO] sale_id={sale_id} product_permalink={product_permalink} refunded={refunded} test={is_test} email={email}")

    if not sale_id:
        return resp(400, {"error": "missing_sale_id"})

    # ── Layer 2: seller_id cross-check ──────────────────────────────────────
    if seller_id != GUMROAD_SELLER_ID:
        print(f"[WARN] seller_id_mismatch: got {seller_id!r}")
        return resp(401, {"error": "seller_id_mismatch"})

    # ── Layer 3: license_key verify (every product has key-gen enabled) ─────
    if not license_key:
        return resp(401, {"error": "missing_license_key"})
    if not verify_license_key(license_key, product_permalink, product_id):
        print(f"[WARN] license_verify_failed sale_id={sale_id}")
        return resp(401, {"error": "license_verify_failed"})

    # Purchase and refund pings re-use the SAME sale_id, so idempotency must be
    # keyed by (sale_id, event type) -- otherwise the refund re-POST would be
    # silently swallowed as a "duplicate" of the original purchase event.
    event_key = f"{sale_id}:{'refund' if refunded else 'purchase'}"
    try:
        events_table.put_item(
            Item={"event_key": event_key, "sale_id": sale_id, "ts": int(time.time()), "status": "processing"},
            ConditionExpression="attribute_not_exists(event_key)"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return resp(200, {"skipped": True, "reason": "duplicate_event"})
        raise

    # ── Refund path ──────────────────────────────────────────────────────────
    if refunded:
        try:
            existing = orders_table.get_item(Key={"sale_id": sale_id}).get("Item")
            if not existing:
                return resp(200, {"skipped": "no_existing_order_to_revoke"})
            existing_license_key = existing.get("license_key", "")
            if existing_license_key:
                licenses_table.update_item(
                    Key={"license_key": existing_license_key},
                    UpdateExpression="SET #s = :revoked, revoked_at = :ts, revoke_reason = :reason",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":revoked": "revoked", ":ts": int(time.time()), ":reason": "refunded"}
                )
            orders_table.update_item(
                Key={"sale_id": sale_id},
                UpdateExpression="SET #s = :revoked, revoked_at = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":revoked": "revoked", ":ts": int(time.time())}
            )
            print(f"[INFO] AUDIT gumroad_revoke_ok sale_id={sale_id} license=...{existing_license_key[-8:] if existing_license_key else ''}")
        except Exception as e:
            print(f"[ERROR] Revoke failed: {e}")
            return resp(500, {"error": "revoke_failed"})
        return resp(200, {"revoked": True, "sale_id": sale_id})

    # ── Purchase path ──────────────────────────────────────────────────────────
    plan_meta = PRODUCT_MAP.get(product_permalink)
    if not plan_meta:
        print(f"[WARN] Unknown product_permalink={product_permalink!r} product_id={product_id!r}")
        return resp(422, {"error": f"unknown product: {product_permalink or product_id}"})

    discord_user_id = get_discord_user_id_from_form(form)
    if not discord_user_id:
        return resp(422, {"error": "missing discord_user_id"})

    # ── HIGH-2: idempotency keyed on the genuine Gumroad license_key (NOT the
    # attacker-chosen sale_id). One backend license per Gumroad key EVER — a forged
    # ping that replays a real license_key with a fresh sale_id cannot re-mint.
    lickey_event = f"lickey:{license_key}"
    try:
        events_table.put_item(
            Item={"event_key": lickey_event, "sale_id": sale_id, "license_suffix": license_key[-6:],
                  "ts": int(time.time()), "status": "minted"},
            ConditionExpression="attribute_not_exists(event_key)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[WARN] license_key already minted; skipping re-mint ...{license_key[-6:]}")
            return resp(200, {"skipped": True, "reason": "license_key_already_minted"})
        raise

    try:
        issued_license_key = provision_license(plan_meta["plan"], plan_meta["days"], discord_user_id, sale_id)
    except Exception as e:
        # NEW-6: log detail server-side, return a generic body (no internal text).
        print(f"[ERROR] Provision failed: {e}")
        # V3 MED: the lickey idempotency marker was claimed BEFORE this mint. A transient
        # provision failure would otherwise ORPHAN A PAID KEY — Gumroad's retry ping would hit
        # the existing marker and return "already minted" without ever issuing a key. Release the
        # claim so the retry re-attempts the mint. (Idempotency against a genuine duplicate is
        # preserved: a real re-ping after a SUCCESSFUL mint still finds the marker.)
        try:
            events_table.delete_item(Key={"event_key": lickey_event})
        except Exception as del_e:
            print(f"[ERROR] failed to release lickey claim after provision failure: {del_e}")
        return resp(500, {"error": "provision_failed"})

    print(f"[INFO] Provisioned license ...{issued_license_key[-8:]} plan={plan_meta['slug']} sale_id={sale_id}")

    dm_status = "delivered"
    try:
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
        "status": "active",
        "test": is_test,
        "created_at": int(time.time()),
    })

    print(f"[INFO] AUDIT gumroad_issue_ok sale_id={sale_id} plan={plan_meta['slug']} dm_status={dm_status}")

    return resp(200, {
        "success": True,
        "sale_id": sale_id,
        "plan": plan_meta["plan"],
        "dm_status": dm_status,
        "license_suffix": issued_license_key[-8:],
    })
