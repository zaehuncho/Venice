import json
import os
import hmac
import hashlib
import time
import boto3
import urllib.request
import urllib.error
from botocore.exceptions import ClientError

# ── SSM client ──────────────────────────────────────────────────────────────
ssm = boto3.client("ssm", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

_secret_cache: dict = {}

def get_secret(name: str) -> str:
    if name not in _secret_cache:
        resp = ssm.get_parameter(Name=name, WithDecryption=True)
        _secret_cache[name] = resp["Parameter"]["Value"]
    return _secret_cache[name]

# ── Pre-load secrets at cold start to avoid first-request failures ──────────
try:
    get_secret("/orion/sellhub_webhook_secret")
    get_secret("/orion/sellhub_webhook_secret_refunded")
    get_secret("/orion/discord_bot_token")
except Exception as _e:
    print(f"[WARN] Cold-start secret pre-load failed: {_e}")

# ── DynamoDB tables ─────────────────────────────────────────────────────────
events_table  = dynamodb.Table("orion-webhook-events")
orders_table  = dynamodb.Table("orion-sellhub-orders")
licenses_table = dynamodb.Table("orion-licenses")

# ── Product map (Sellhub product UUID → plan metadata) ──────────────────────
PRODUCT_MAP = {
    "5d84289a-8345-4db4-9edb-16d48dcc8ef0": {"plan": "beta",     "days": 30,   "beta": True,  "slug": "ORION_BETA"},
    "732c9fd6-1beb-4001-8946-3181d2e7d17c": {"plan": "1day",     "days": 1,    "beta": False, "slug": "ORION_1D"},
    "374e7d91-3357-4bf4-b879-709fb0ac1c15": {"plan": "3day",     "days": 3,    "beta": False, "slug": "ORION_3D"},
    "f9b1c725-f326-47a0-9776-fc9473a9b7ff": {"plan": "7day",     "days": 7,    "beta": False, "slug": "ORION_7D"},
    "5ec643de-e2c8-4b12-9754-021308a38bc7": {"plan": "14day",    "days": 14,   "beta": False, "slug": "ORION_14D"},
    "2577936c-b403-46f8-ae8e-36a65b424f02": {"plan": "30day",    "days": 30,   "beta": False, "slug": "ORION_30D"},
    "22dd36b2-2ea3-476a-80c5-04060da9b9ae": {"plan": "120day",   "days": 120,  "beta": False, "slug": "ORION_120D"},
    "738ec36d-48fd-4a0d-b7ba-559281efe883": {"plan": "lifetime", "days": None, "beta": False, "slug": "ORION_LIFETIME"},
}

PAID_STATUSES   = {"paid", "completed", "fulfilled"}
REVOKE_STATUSES = {"refunded", "chargeback", "cancelled", "disputed"}

PROVISION_URL = "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/bot/provision"
DISCORD_API   = "https://discord.com/api/v10"

REPLAY_WINDOW = 300  # 5 minutes

# ── Helpers ──────────────────────────────────────────────────────────────────
def resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}

def verify_signature(raw_body: bytes, sig_header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # NEW-4: strip the "sha256=" PREFIX, not a character set. lstrip("sha256=")
    # ate leading hex digits in {2,5,6,a,3,e,s,h,=}, intermittently rejecting
    # legitimate webhooks whose digest started with those.
    sig = sig_header.lower()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    return hmac.compare_digest(expected, sig)

def get_discord_user_id(custom_fields) -> str:
    """Support both dict and list formats; multiple possible key names."""
    aliases = {"discord user id", "discord_user_id", "discorduserid", "discord id", "discord_id"}
    if isinstance(custom_fields, dict):
        for k, v in custom_fields.items():
            if k.lower().replace("-", "_").replace(" ", "_") in {"discord_user_id", "discord_id"} or k.lower().replace(" ", "_") in aliases:
                return str(v).strip()
        # normalize keys
        norm = {"_".join(k.lower().split()): v for k, v in custom_fields.items()}
        for alias in aliases:
            key = "_".join(alias.split())
            if key in norm:
                return str(norm[key]).strip()
    elif isinstance(custom_fields, list):
        for field in custom_fields:
            name = field.get("name", "")
            if "_".join(name.lower().split()) in aliases:
                return str(field.get("value", "")).strip()
    return ""

def send_discord_dm(discord_user_id: str, license_key: str, plan: str, days):
    token = get_secret("/orion/discord_bot_token")
    # Open DM channel
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

    # Send message
    duration_str = "Lifetime" if days is None else f"{days} day{'s' if days != 1 else ''}"
    embed = {
        "title": "🎮 Orion License Activated",
        "description": f"Your **{plan}** license ({duration_str}) is ready.",
        "fields": [
            {"name": "License Key", "value": f"||`{license_key}`||", "inline": False},
            {"name": "Support", "value": "DM a staff member or open a ticket if you have issues.", "inline": False}
        ],
        "color": 0x5865F2,
        "footer": {"text": "Orion Game — do not share your key"}
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
    # HIGH-3: per-consumer secret; fall back to the legacy shared secret.
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
    # HIGH-4: /api/bot/* is behind edge auth — present the edge secret.
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

# ── Main handler ─────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    raw_body = (event.get("body") or "").encode()
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(event["body"])

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig_header = headers.get("x-sellhub-signature", "")
    ts_header  = headers.get("x-sellhub-timestamp", "")

    # ── Timestamp replay protection ─────────────────────────────────────────
    if ts_header:
        try:
            ts = int(ts_header)
            if abs(time.time() - ts) > REPLAY_WINDOW:
                return resp(401, {"error": "timestamp_expired"})
        except ValueError:
            return resp(401, {"error": "invalid_timestamp"})

    # ── Signature verification (try completed secret then refunded secret) ──
    secret_completed = get_secret("/orion/sellhub_webhook_secret")
    secret_refunded  = get_secret("/orion/sellhub_webhook_secret_refunded")

    sig_valid = False
    active_secret = None
    for secret in [secret_completed, secret_refunded]:
        if verify_signature(raw_body, sig_header, secret):
            sig_valid = True
            active_secret = secret
            break

    if not sig_valid:
        return resp(401, {"error": "invalid_signature"})

    # ── Parse body ──────────────────────────────────────────────────────────
    try:
        body = json.loads(raw_body)
    except Exception:
        return resp(400, {"error": "invalid_json"})

    # Support optional "order" wrapper
    order = body.get("order", body)

    event_id  = body.get("event_id", "")
    order_id  = order.get("id", "")
    status    = order.get("status", "").lower()
    line_items = order.get("line_items", [])
    custom_fields = order.get("custom_fields", {})

    print(f"[INFO] event_id={event_id} order_id={order_id} status={status}")

    # ── Layer 1 idempotency: event_id ───────────────────────────────────────
    if event_id:
        try:
            events_table.put_item(
                Item={"event_id": event_id, "order_id": order_id, "ts": int(time.time()), "status": "processing"},
                ConditionExpression="attribute_not_exists(event_id)"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return resp(200, {"skipped": True, "reason": "duplicate_event_id"})
            raise

    # ── Refund / chargeback path ────────────────────────────────────────────
    if status in REVOKE_STATUSES:
        try:
            existing = orders_table.get_item(Key={"order_id": order_id}).get("Item")
            if not existing:
                return resp(200, {"skipped": "no_existing_order_to_revoke"})
            license_key = existing.get("license_key", "")
            # Deactivate in licenses table
            if license_key:
                licenses_table.update_item(
                    Key={"license_key": license_key},
                    UpdateExpression="SET #s = :revoked, revoked_at = :ts, revoke_reason = :reason",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":revoked": "revoked", ":ts": int(time.time()), ":reason": status}
                )
            # Mark order revoked
            orders_table.update_item(
                Key={"order_id": order_id},
                UpdateExpression="SET #s = :revoked, revoked_at = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":revoked": "revoked", ":ts": int(time.time())}
            )
            print(f"[INFO] Revoked license ...{license_key[-8:]} for order {order_id} reason={status}")
        except Exception as e:
            print(f"[ERROR] Revoke failed: {e}")
            return resp(500, {"error": "revoke_failed"})
        return resp(200, {"revoked": True, "order_id": order_id})

    # ── Skip non-paid statuses ──────────────────────────────────────────────
    if status not in PAID_STATUSES:
        return resp(200, {"skipped": True, "status": status})

    # ── Layer 2 idempotency: order_id ───────────────────────────────────────
    existing_order = orders_table.get_item(Key={"order_id": order_id}).get("Item")
    if existing_order:
        return resp(200, {"skipped": True, "reason": "duplicate_order_id", "order_id": order_id})

    # ── Validate product ────────────────────────────────────────────────────
    product_id = ""
    for item in line_items:
        pid = item.get("product_id") or item.get("productId", "")
        if pid in PRODUCT_MAP:
            product_id = pid
            break

    if not product_id:
        raw_ids = [item.get("product_id") or item.get("productId", "") for item in line_items]
        print(f"[WARN] Unknown product_ids: {raw_ids}")
        return resp(422, {"error": f"unknown product_id: {raw_ids}"})

    plan_meta = PRODUCT_MAP[product_id]

    # ── Require discord_user_id ─────────────────────────────────────────────
    discord_user_id = get_discord_user_id(custom_fields)
    if not discord_user_id:
        return resp(422, {"error": "missing discord_user_id"})

    # ── Provision license ───────────────────────────────────────────────────
    try:
        license_key = provision_license(plan_meta["plan"], plan_meta["days"], discord_user_id, order_id)
    except Exception as e:
        print(f"[ERROR] Provision failed: {e}")
        # V3 MED: the Layer-1 event_id marker was claimed BEFORE this mint. A transient provision
        # failure would orphan a PAID key — a retry with the same event_id hits the marker and
        # returns duplicate_event_id without ever minting. Release the claim so the retry
        # re-attempts. (Layer-2 order_id is written only AFTER a successful mint, so it can't orphan.)
        if event_id:
            try:
                events_table.delete_item(Key={"event_id": event_id})
            except Exception as del_e:
                print(f"[ERROR] failed to release event_id claim after provision failure: {del_e}")
        return resp(500, {"error": "provision_failed"})

    print(f"[INFO] Provisioned license ...{license_key[-8:]} plan={plan_meta['slug']} order={order_id}")

    # ── Send Discord DM ─────────────────────────────────────────────────────
    dm_status = "delivered"
    try:
        send_discord_dm(discord_user_id, license_key, plan_meta["plan"], plan_meta["days"])
        print(f"[INFO] Discord DM delivered to {discord_user_id}")
    except Exception as e:
        dm_status = "delivery_failed"
        print(f"[WARN] Discord DM failed (will log for retry): {e}")

    # ── Write order record ──────────────────────────────────────────────────
    orders_table.put_item(Item={
        "order_id":        order_id,
        "event_id":        event_id,
        "discord_user_id": discord_user_id,
        "product_id":      product_id,
        "plan":            plan_meta["plan"],
        "slug":            plan_meta["slug"],
        "license_key":     license_key,
        "license_suffix":  license_key[-8:],
        "dm_status":       dm_status,
        "status":          "active",
        "created_at":      int(time.time()),
    })

    # ── Mark event done ─────────────────────────────────────────────────────
    if event_id:
        events_table.update_item(
            Key={"event_id": event_id},
            UpdateExpression="SET #s = :done",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":done": "done"}
        )

    return resp(200, {
        "success": True,
        "order_id": order_id,
        "plan": plan_meta["slug"],
        "dm_status": dm_status,
        "license_suffix": license_key[-8:]
    })
