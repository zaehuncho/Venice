import json, hmac, hashlib, time, string, secrets, uuid, base64
import urllib.request, urllib.error
from decimal import Decimal

REGION              = "us-east-1"
TOKEN_SECRET_SSM    = "/orion/token_secret"
ADMIN_SECRET_SSM    = "/orion/admin_secret"
ED25519_PRIV_SSM    = "/orion/ed25519_private_key"
ED25519_PUB_SSM     = "/orion/ed25519_public_key"
ED25519_KEY_ID      = "orion-ed25519-v1"

# Manifest signing fields — ALL must be present in signed payload
# allow_rollback added per v0.4.0 requirements
MANIFEST_SIGN_FIELDS = [
    "latest_version", "minimum_supported_version",
    "artifact_url", "sha256", "published_at",
    "mandatory", "allow_rollback", "public_key_id"
]

MAX_DEACT_3OD   = 3
DEACT_COOLDOWN  = 86400
TS_SKEW_LIMIT   = 300
KEY_CHARS       = [c for c in string.ascii_uppercase + string.digits if c not in "OI01"]

# ── bot gateway (Discord bot / Cloudflare Worker) ──────────────────────────────
# HIGH-3: per-consumer bot secrets. Each caller (Discord bot host, the two
# webhooks, the Cloudflare worker) carries its OWN secret in its OWN SSM param so
# a single leak can be rotated independently without disrupting the others.
# require_bot() accepts any one of these; the matched consumer is logged.
BOT_SECRET_SSM       = "/orion/bot_service_secret"      # legacy / Discord-bot host + worker
BOT_SECRET_SSM_NAMES = [
    "/orion/bot_service_secret",     # Discord bot host (orion_bot.py) — legacy default
    "/orion/webhook_bot_secret",     # gumroad + sellhub webhook Lambdas
    "/orion/worker_bot_secret",      # Cloudflare worker
]
TIER_DAYS            = {"day": 1, "week": 7, "month": 30, "lifetime": 36500}
BOT_RESET_COOLDOWN_S = 86400
BOT_RESET_MAX_30D    = 3

# Free trial: one per Discord account (TRIAL#<discord_id> claim marker below)
# AND one per machine (the TRIALMACHINE#<machine_id> guard in handle_activate,
# which survives an hwid-reset). Both gates are required — either alone is
# trivially farmed with alt accounts or a fresh VM.
TRIAL_DAYS = 3

# ── Discord delivery ───────────────────────────────────────────────────────────
DISCORD_API            = "https://discord.com/api/v10"
DISCORD_BOT_TOKEN_SSM  = "/orion/discord_bot_token"
DISCORD_GUILD_ID_SSM   = "/orion/discord_guild_id"
DISCORD_TRIAL_ROLE_SSM = "/orion/discord_trial_role_id"
BRAND_EMBED_COLOR      = 0x2563EB

# ── MED-1: server-side rate limiting on the money endpoints ────────────────────
# Fixed-window counter in DynamoDB (orion-ratelimit, PK "rl_key"). Fail-open on
# infra error so a table/permission gap never takes the money path fully down,
# but a working table enforces the cap.
RATELIMIT_WINDOW_S   = 60
RATELIMIT_MAX        = 30    # per (endpoint, subject) per window

# ── CRIT-2: artifact_url allow-list for the (now offline-signed) update manifest.
# The Lambda no longer signs; it only STORES a pre-signed manifest, and it refuses
# to store one whose artifact_url is not an owned HTTPS origin. Override via env
# ORION_ARTIFACT_ALLOWLIST (comma-separated host or host/prefix entries).
ARTIFACT_URL_ALLOWLIST = [
    "orion-releases.s3.amazonaws.com",
    "orion-releases.s3.us-east-1.amazonaws.com",
    "releases.orion.game",
    "cdn.orion.game",
]

# ── NEW-1: dedicated Ed25519 lease-signing key (NOT the update-manifest key,
# NOT the HMAC token secret). Operator sets the PEM private key in this SSM param.
LEASE_SIGNING_KEY_SSM = "/orion/lease_signing_key"
LEASE_KEY_ID          = "orion-lease-ed25519-v1"

SHARD_ENC_KEY_SSM     = "/orion/shard_encryption_key"
SHARD_RETRIEVE_LIMIT  = 10

# ── lazy-loaded clients ───────────────────────────────────────────────────────
_ssm = None
_ddb = None

def get_ssm():
    global _ssm
    if _ssm is None:
        import boto3
        _ssm = boto3.client("ssm", region_name=REGION)
    return _ssm

def get_ddb():
    global _ddb
    if _ddb is None:
        import boto3
        _ddb = boto3.resource("dynamodb", region_name=REGION)
    return _ddb

def ssm_get(name, decrypt=False):
    r = get_ssm().get_parameter(Name=name, WithDecryption=decrypt)
    return r["Parameter"]["Value"]

# ── Ed25519 signing ───────────────────────────────────────────────────────────
_ed25519_priv = None
_ed25519_pub_b64 = None

def load_ed25519_private_key():
    global _ed25519_priv
    if _ed25519_priv is None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem = ssm_get(ED25519_PRIV_SSM, decrypt=True).encode()
        _ed25519_priv = load_pem_private_key(pem, password=None)
    return _ed25519_priv

def load_ed25519_public_key_b64():
    global _ed25519_pub_b64
    if _ed25519_pub_b64 is None:
        _ed25519_pub_b64 = ssm_get(ED25519_PUB_SSM, decrypt=False)
    return _ed25519_pub_b64

def _manifest_canonical_bytes(manifest_dict):
    canonical = {f: manifest_dict[f] for f in MANIFEST_SIGN_FIELDS}
    return json.dumps(canonical, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

def sign_manifest_ed25519(manifest_dict):
    """OFFLINE-ONLY signer (kept for the air-gapped package pipeline / local tools).

    CRIT-2: this is NOT called from any request handler anymore. The Lambda no
    longer loads the Ed25519 PRIVATE key and no longer signs attacker-supplied
    artifact_url/sha256 in-process. Online publishing (handle_post_update) only
    STORES a pre-signed manifest and verifies its signature with the PUBLIC key.
    """
    payload_bytes = _manifest_canonical_bytes(manifest_dict)
    priv = load_ed25519_private_key()
    sig_bytes = priv.sign(payload_bytes)
    return base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode("ascii")

def verify_manifest_signature(manifest_dict, signature_b64url):
    """Verify a detached Ed25519 signature over the canonical manifest bytes using
    the PUBLIC key (SSM /orion/ed25519_public_key, raw 32-byte key, base64)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    pub_b64 = load_ed25519_public_key_b64()
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    pad = "=" * (-len(signature_b64url) % 4)
    sig = base64.urlsafe_b64decode(signature_b64url + pad)
    try:
        pub.verify(sig, _manifest_canonical_bytes(manifest_dict))
        return True
    except InvalidSignature:
        return False

# ── NEW-1: Ed25519 heartbeat-lease signing ─────────────────────────────────────
_lease_priv = None

def load_lease_private_key():
    global _lease_priv
    if _lease_priv is None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem = ssm_get(LEASE_SIGNING_KEY_SSM, decrypt=True).encode()
        _lease_priv = load_pem_private_key(pem, password=None)
    return _lease_priv

def sign_lease(license_key, machine_id, lease_expires_at):
    """Sign the canonical lease string the client verifies:
        f"{license_key}:{machine_id}:{lease_expires_at}"
    (license_key uppercased/trimmed, machine_id trimmed, expires base-10 int).
    Returns base64url (no padding) — matches LeaseGate::verifyLeaseSignature."""
    message = f"{license_key.strip().upper()}:{machine_id.strip()}:{int(lease_expires_at)}".encode("utf-8")
    priv = load_lease_private_key()
    sig = priv.sign(message)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

# ── helpers ───────────────────────────────────────────────────────────────────
def decimal_default(obj):
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    raise TypeError

def ok(body, status=200):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=decimal_default)}

def err(msg, status=400):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": msg})}

def require_admin(event):
    h = (event.get("headers") or {})
    secret = h.get("x-orion-admin-secret") or h.get("X-Orion-Admin-Secret") or ""
    try:
        expected = ssm_get(ADMIN_SECRET_SSM, decrypt=True)
    except Exception:
        return False
    return secrets.compare_digest(secret, expected)

def require_bot(event):
    """HIGH-3: accept ANY one of the per-consumer bot secrets (each in its own SSM
    param) so they can be rotated independently. Returns the matched consumer name
    (truthy) or False. Timing-safe compare against every configured consumer even
    on a miss (no early-out that would leak which consumer matched)."""
    h = (event.get("headers") or {})
    secret = h.get("x-orion-bot-secret") or h.get("X-Orion-Bot-Secret") or ""
    matched = False
    for ssm_name in BOT_SECRET_SSM_NAMES:
        try:
            expected = ssm_get(ssm_name, decrypt=True)
        except Exception:
            continue
        if secret and secrets.compare_digest(secret, expected):
            matched = ssm_name
    return matched

def gen_license_key():
    return "-".join("".join(secrets.choice(KEY_CHARS) for _ in range(4)) for _ in range(4))

def gen_token():
    return secrets.token_urlsafe(32)

def now_ts():
    return int(time.time())

# ── DynamoDB tables ───────────────────────────────────────────────────────────
def licenses_table():
    return get_ddb().Table("orion-licenses")

def tokens_table():
    return get_ddb().Table("orion-tokens")

def audit_table():
    return get_ddb().Table("orion-audit")

def config_table():
    return get_ddb().Table("orion-config")

def update_manifest_table():
    return get_ddb().Table("orion-update-manifest")

def shards_table():
    return get_ddb().Table("orion-shards")

def ratelimit_table():
    return get_ddb().Table("orion-ratelimit")

# ── MED-1: fixed-window rate limiter (atomic ADD + TTL) ────────────────────────
def rate_limit_ok(scope, subject, limit=RATELIMIT_MAX, window=RATELIMIT_WINDOW_S):
    """Return True if under the cap for (scope, subject) in the current window.
    Fail-open on infra error so a missing table/permission never fully blackholes
    the money path, but a working table enforces the cap."""
    if not subject:
        subject = "anon"
    bucket = int(now_ts() // window)
    rl_key = f"{scope}:{subject}:{bucket}"
    try:
        r = ratelimit_table().update_item(
            Key={"rl_key": rl_key},
            UpdateExpression="ADD hits :one SET expires = :e",
            ExpressionAttributeValues={":one": 1, ":e": (bucket + 2) * window},
            ReturnValues="UPDATED_NEW",
        )
        hits = int(r.get("Attributes", {}).get("hits", 1))
        return hits <= limit
    except Exception as e:
        print(f"[RATELIMIT] fail-open ({scope}): {e}")
        return True

def _client_ip(event):
    rc = event.get("requestContext", {}) or {}
    http = rc.get("http", {}) or {}
    return http.get("sourceIp") or (event.get("headers") or {}).get("x-forwarded-for", "") or ""

# ── CRIT-2: alerting hook for privileged endpoints ─────────────────────────────
def admin_alert(kind, event, extra=None):
    """Structured, greppable alert line for /api/admin/* and /api/update POST.
    Wire a CloudWatch metric-filter / SNS subscription to '[ALERT]' in prod."""
    ip = _client_ip(event)
    ua = (event.get("headers") or {}).get("user-agent", "")[:80]
    print(f"[ALERT] kind={kind} ip={ip} ua={ua} extra={extra or {}}")
    audit_log(f"alert_{kind}", extra={"ip": ip})

# ── audit log ─────────────────────────────────────────────────────────────────
def audit_log(event_type, license_key=None, extra=None):
    try:
        item = {
            "event_id": str(uuid.uuid4()),
            "ts": now_ts(),
            "event_type": event_type,
        }
        if license_key:
            item["license_suffix"] = license_key[-4:]
        if extra:
            item.update(extra)
        audit_table().put_item(Item=item)
    except Exception as e:
        print(f"[WARNING] AUDIT log failed: {e}")

# ── kill switch config ────────────────────────────────────────────────────────
def get_global_kill():
    try:
        r = config_table().get_item(Key={"config_key": "global_kill"})
        item = r.get("Item", {})
        return item.get("enabled", False), item.get("reason", "")
    except Exception:
        return False, ""

# ── /api/version ─────────────────────────────────────────────────────────────
def handle_version(event):
    return ok({
        "ok": True,
        "service": "orion-activate",
        "version": "0.4.0",
        "update_signing": "ed25519",
        "public_key_id": ED25519_KEY_ID,
        "ts": now_ts()
    })

# ── /api/activate ─────────────────────────────────────────────────────────────
def handle_activate(event):
    # ── Parse body ───────────────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")

    license_key = (body.get("license_key") or "").strip().upper()
    machine_id  = (body.get("machine_id")  or "").strip()

    if not license_key or not machine_id:
        return err("license_key and machine_id required")
    if len(machine_id) > 256:
        return err("machine_id too long")

    # ── MED-1: server-side rate-limit (per key + per source IP) ────────────────
    if not rate_limit_ok("activate", license_key) or not rate_limit_ok("activate_ip", _client_ip(event)):
        audit_log("activate_rate_limited", license_key)
        return err("rate_limited", 429)

    # ── MED-1: activate replay window (nonce + timestamp). The client sends these
    # in the body AND as X-Orion-Request-* headers (LicenseClient.cpp). Enforce
    # both: reject stale timestamps and replayed nonces.
    h = (event.get("headers") or {})
    nonce = (str(body.get("request_nonce") or h.get("x-orion-request-nonce") or "")).strip()
    ts_raw = body.get("request_timestamp")
    if ts_raw in (None, ""):
        ts_raw = h.get("x-orion-request-timestamp")
    if not nonce or ts_raw in (None, ""):
        return err("replay_fields_required", 400)
    try:
        req_ts = int(ts_raw)
    except (TypeError, ValueError):
        return err("invalid_timestamp", 400)
    if abs(now_ts() - req_ts) > TS_SKEW_LIMIT:
        return err("timestamp_expired", 401)
    if not check_nonce("activate:" + nonce):
        audit_log("activate_replay_detected", license_key)
        return err("replay_detected", 401)

    gkill, greason = get_global_kill()
    if gkill:
        return err("service_disabled: " + (greason or "temporarily disabled"), 503)

    # ── Fetch license record ─────────────────────────────────────────────────
    try:
        r = licenses_table().get_item(Key={"license_key": license_key})
    except Exception as e:
        print(f"[ERROR] DynamoDB GetItem failed: {e}")
        return err("internal_error", 500)

    item = r.get("Item")
    if not item:
        return err("invalid_key", 403)

    # ── Validate record shape (guard against legacy/malformed records) ────────
    try:
        # Normalise expiry: accept numeric epoch (Decimal) OR string ISO / epoch.
        # [FIX 2026-08-07] `or` treats 0 as falsy, so a lifetime license (line 1623
        # sets expiry=0 for plan=="lifetime") would fall through to the legacy
        # `expiry_date` lookup, get None, and be rejected as license_record_invalid.
        # This bricked every real lifetime purchase minted through the Gumroad
        # webhook. Explicit None-check keeps 0 as a valid value.
        expiry_raw = item.get("expiry")
        if expiry_raw is None:
            expiry_raw = item.get("expiry_date")
        if expiry_raw is None:
            print(f"[ERROR] license_record_invalid: no expiry field key=...{license_key[-8:]}")
            return err("license_record_invalid", 400)
        try:
            expiry = int(expiry_raw)          # Decimal or numeric string
        except (TypeError, ValueError):
            import datetime
            # Try ISO date string "2025-12-31"
            try:
                expiry = int(datetime.datetime.fromisoformat(str(expiry_raw)).timestamp())
            except Exception:
                print(f"[ERROR] license_record_invalid: unparseable expiry={expiry_raw!r} key=...{license_key[-8:]}")
                return err("license_record_invalid", 400)
        # Backfill normalised expiry if it was stored under legacy key
        if "expiry" not in item:
            item["expiry"] = expiry
    except Exception as e:
        print(f"[ERROR] license_record_invalid: unexpected schema error {e} key=...{license_key[-8:]}")
        return err("license_record_invalid", 400)

    # ── Revocation / status checks ────────────────────────────────────────────
    if item.get("revoked", False):
        reason = item.get("kill_reason", "revoked")
        audit_log("activate_blocked_revoked", license_key, {"reason": reason})
        print(f"[WARNING] AUDIT activate blocked: key ...{license_key[-4:]} revoked, reason={reason}")
        return err("license_revoked", 403)

    if item.get("status") == "revoked":
        audit_log("activate_blocked_status_revoked", license_key)
        return err("license_revoked", 403)

    if item.get("status") != "active":
        audit_log("activate_bad_status", license_key, {"status": item.get("status")})
        return err("license_invalid_status", 403)

    # ── Expiry check ──────────────────────────────────────────────────────────
    if expiry > 0 and expiry < now_ts():
        return err("license_expired", 403)

    # ── Machine binding ───────────────────────────────────────────────────────
    bound_machine = item.get("machine_id", "")
    activations   = int(item.get("activations", 0))
    max_devices   = int(item.get("max_devices", 1))

    if bound_machine and bound_machine != machine_id:
        if activations >= max_devices:
            audit_log("activate_machine_conflict", license_key)
            return err("device_mismatch", 403)

    # ── Trial-abuse guard ─────────────────────────────────────────────────────
    # A trial-plan license binding to a machine for the first time must claim a
    # per-machine marker. If this machine already claimed one (via this key or
    # any other trial key, including after an hwid-reset), block the bind. Paid
    # plans are never subject to this check.
    if item.get("plan") == "trial" and bound_machine != machine_id:
        marker_key = "TRIALMACHINE#" + machine_id
        try:
            licenses_table().put_item(
                Item={"license_key": marker_key, "status": "trial_machine_claim",
                      "revoked": True, "created_at": now_ts()},
                ConditionExpression="attribute_not_exists(license_key)")
        except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
            audit_log("activate_blocked_trial_used", license_key,
                      {"machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id})
            return err("trial_used", 403)

    # ── NEW-2: atomic device-cap bind (claim BEFORE minting the token) ────────
    # Conditional ADD closes the read-modify-write TOCTOU: two concurrent binds on
    # a fresh max_devices=1 key can no longer both read activations=0 and both win.
    # Allowed iff re-binding the SAME machine, OR there is still device capacity.
    try:
        licenses_table().update_item(
            Key={"license_key": license_key},
            UpdateExpression="ADD activations :one SET machine_id = :m, last_activated = :t",
            ConditionExpression=(
                "machine_id = :m OR attribute_not_exists(machine_id) OR machine_id = :empty "
                "OR activations < :max OR attribute_not_exists(activations)"
            ),
            ExpressionAttributeValues={
                ":one": 1, ":m": machine_id, ":t": now_ts(),
                ":empty": "", ":max": max_devices,
            },
        )
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        audit_log("activate_device_cap", license_key)
        return err("device_limit_reached", 403)
    except Exception as e:
        print(f"[ERROR] license bind update_item failed: {e}")
        return err("internal_error", 500)

    # ── Issue session token (only after the bind is durably claimed) ──────────
    token    = gen_token()
    expires  = now_ts() + 86400 * 30
    token_id = str(uuid.uuid4())

    try:
        token_secret = ssm_get(TOKEN_SECRET_SSM, decrypt=True)
    except Exception as e:
        print(f"[ERROR] SSM token_secret unavailable: {e}")
        return err("internal_error", 500)

    sig = hmac.new(token_secret.encode(), f"{token_id}:{license_key}:{expires}".encode(), hashlib.sha256).hexdigest()

    try:
        tokens_table().put_item(Item={
            "token_id":   token_id,
            "license_key": license_key,
            "machine_id":  machine_id,
            "token_hash":  hashlib.sha256(token.encode()).hexdigest(),
            "expires":     expires,
            "created_at":  now_ts(),
            "sig":         sig,
        })
    except Exception as e:
        print(f"[ERROR] tokens_table PutItem failed: {e}")
        return err("internal_error", 500)

    audit_log("activate_success", license_key, {"machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id})
    print(f"[INFO] AUDIT activate success: key ...{license_key[-4:]}")

    return ok({
        "ok":               True,
        "token":            token,
        "tid":              token_id,
        "expires":          expires,
        "license_key_suffix": license_key[-4:],
    })


def handle_deactivate(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    token    = (body.get("token") or "").strip()
    token_id = (body.get("token_id") or "").strip()
    machine_id = (body.get("machine_id") or "").strip()

    if not token or not token_id:
        return err("token and token_id required")

    r = tokens_table().get_item(Key={"token_id": token_id})
    item = r.get("Item")
    if not item:
        return err("invalid_token", 403)

    stored_hash = item.get("token_hash", "")
    if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored_hash):
        return err("invalid_token", 403)

    if item.get("expires", 0) < now_ts():
        return err("token_expired", 403)

    license_key = item.get("license_key", "")

    tokens_table().delete_item(Key={"token_id": token_id})
    audit_log("deactivate_success", license_key, {"machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id})
    print(f"[INFO] AUDIT deactivate: key ...{license_key[-4:]}")

    return ok({"ok": True, "deactivated": True})

# ── /api/update GET ───────────────────────────────────────────────────────────
def handle_get_update(event):
    try:
        r = update_manifest_table().get_item(Key={"record_id": "current"})
        item = r.get("Item")
        if not item:
            return err("no manifest published", 404)

        pub_key_b64 = load_ed25519_public_key_b64()

        manifest = {
            "ok": True,
            "latest_version":           item.get("latest_version", ""),
            "minimum_supported_version": item.get("minimum_supported_version", ""),
            "artifact_url":             item.get("artifact_url", ""),
            "sha256":                   item.get("sha256", ""),
            "signature_alg":            "ed25519",
            "signature":                item.get("signature", ""),
            "public_key_id":            item.get("public_key_id", ED25519_KEY_ID),
            "public_key_b64":           pub_key_b64,
            "release_notes":            item.get("release_notes", ""),
            "published_at":             item.get("published_at", ""),
            "mandatory":                item.get("mandatory", False),
            "allow_rollback":           item.get("allow_rollback", False),
        }
        return ok(manifest)
    except Exception as e:
        print(f"[ERROR] get_update: {e}")
        return err("internal_error", 500)

# ── CRIT-2: artifact_url allow-list ────────────────────────────────────────────
def _artifact_url_allowed(url):
    """Require HTTPS + a host (optionally host/path-prefix) on the owned allow-list.
    Overridable via env ORION_ARTIFACT_ALLOWLIST (comma-separated)."""
    import urllib.parse as _up
    allow = ARTIFACT_URL_ALLOWLIST
    env = _os.environ.get("ORION_ARTIFACT_ALLOWLIST", "").strip()
    if env:
        allow = [a.strip() for a in env.split(",") if a.strip()]
    try:
        p = _up.urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.netloc:
        return False
    host = p.netloc.lower()
    path = p.path or "/"
    for entry in allow:
        entry = entry.lower().rstrip("/")
        if "/" in entry:
            e_host, e_prefix = entry.split("/", 1)
            if host == e_host and path.lstrip("/").startswith(e_prefix):
                return True
        elif host == entry:
            return True
    return False

# ── /api/update POST ──────────────────────────────────────────────────────────
def handle_post_update(event):
    if not require_admin(event):
        admin_alert("update_forbidden", event)
        return err("forbidden", 403)

    # CRIT-2: throttle + alert on the privileged publish endpoint.
    if not rate_limit_ok("admin_update", _client_ip(event), limit=10, window=60):
        admin_alert("update_rate_limited", event)
        return err("rate_limited", 429)
    admin_alert("update_publish_attempt", event)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    # CRIT-2: the Lambda no longer SIGNS. It stores a manifest that was signed
    # OFFLINE (air-gapped key; the package pipeline emits update_manifest.unsigned
    # .json → signed → posted here). So `signature` is now REQUIRED input, and the
    # attacker-supplied artifact_url must be on the owned allow-list.
    required = ["latest_version", "minimum_supported_version", "artifact_url",
                "sha256", "signature"]
    for f in required:
        if not body.get(f):
            return err(f"missing required field: {f}")

    if not _artifact_url_allowed(body["artifact_url"]):
        admin_alert("update_artifact_url_rejected", event, {"artifact_url": body["artifact_url"][:120]})
        return err("artifact_url_not_allowed", 400)

    published_at  = body.get("published_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    mandatory     = bool(body.get("mandatory", False))
    allow_rollback = bool(body.get("allow_rollback", False))
    release_notes = body.get("release_notes", "")
    signature     = body["signature"]
    public_key_id = body.get("public_key_id", ED25519_KEY_ID)

    # Build the canonical manifest exactly as the offline signer did.
    manifest_dict = {
        "latest_version":            body["latest_version"],
        "minimum_supported_version": body["minimum_supported_version"],
        "artifact_url":              body["artifact_url"],
        "sha256":                    body["sha256"],
        "published_at":              published_at,
        "mandatory":                 mandatory,
        "allow_rollback":            allow_rollback,
        "public_key_id":             public_key_id,
    }

    # Verify the pre-signed signature with the PUBLIC key before storing. The
    # private key is NOT present in the Lambda anymore → no signing oracle.
    try:
        if not verify_manifest_signature(manifest_dict, signature):
            admin_alert("update_bad_signature", event)
            return err("invalid_signature", 400)
    except Exception as e:
        print(f"[ERROR] manifest signature verify failed: {e}")
        return err("signature_verify_error", 500)

    # Store manifest in DynamoDB
    try:
        update_manifest_table().put_item(Item={
            "record_id":               "current",
            "latest_version":            manifest_dict["latest_version"],
            "minimum_supported_version": manifest_dict["minimum_supported_version"],
            "artifact_url":              manifest_dict["artifact_url"],
            "sha256":                    manifest_dict["sha256"],
            "published_at":              manifest_dict["published_at"],
            "mandatory":                 manifest_dict["mandatory"],
            "allow_rollback":            manifest_dict["allow_rollback"],
            "public_key_id":             public_key_id,
            "signature":                 signature,
            "signature_alg":             "ed25519",
            "release_notes":             release_notes,
            "updated_at":                now_ts(),
        })
    except Exception as e:
        print(f"[ERROR] manifest store failed: {e}")
        return err("storage_error", 500)

    print(f"[INFO] AUDIT update manifest published: version={manifest_dict['latest_version']}")

    return ok({
        "ok": True,
        "latest_version":            manifest_dict["latest_version"],
        "minimum_supported_version": manifest_dict["minimum_supported_version"],
        "artifact_url":              manifest_dict["artifact_url"],
        "sha256":                    manifest_dict["sha256"],
        "signature_alg":             "ed25519",
        "signature":                 signature,
        "public_key_id":             public_key_id,
        "published_at":              manifest_dict["published_at"],
        "mandatory":                 manifest_dict["mandatory"],
        "allow_rollback":            manifest_dict["allow_rollback"],
        "release_notes":             release_notes,
    })

# ── /api/admin/provision ──────────────────────────────────────────────────────
def handle_provision(event):
    if not require_admin(event):
        return err("forbidden", 403)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    max_devices  = int(body.get("max_devices", 1))
    notes        = body.get("notes", "")
    license_key  = gen_license_key()
    created_at   = now_ts()

    licenses_table().put_item(Item={
        "license_key":  license_key,
        "status":       "active",
        "revoked":      False,
        "max_devices":  max_devices,
        "activations":  0,
        "machine_id":   "",
        "notes":        notes,
        "created_at":   created_at,
    })

    audit_log("provision", license_key, {"max_devices": max_devices})
    print(f"[INFO] AUDIT provision: key ...{license_key[-4:]}")

    return ok({"ok": True, "license_key": license_key, "max_devices": max_devices}, 201)

# ── /api/admin/kill ───────────────────────────────────────────────────────────
def handle_kill(event):
    if not require_admin(event):
        return err("forbidden", 403)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    target_type = body.get("target_type", "")
    target_id   = body.get("target_id", "")
    reason      = body.get("reason", "killed")

    if target_type == "license_key":
        if not target_id:
            return err("target_id required")
        licenses_table().update_item(
            Key={"license_key": target_id},
            UpdateExpression="SET #s = :s, revoked = :r, kill_reason = :k, killed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "revoked",
                ":r": True,
                ":k": reason,
                ":t": now_ts(),
            }
        )
        audit_log("kill_license", target_id, {"reason": reason})
        print(f"[WARNING] AUDIT kill license: key ...{target_id[-4:]}, reason={reason}")
        return ok({"ok": True, "killed": target_id, "reason": reason})

    elif target_type == "global":
        config_table().put_item(Item={
            "config_key": "global_kill",
            "enabled":    True,
            "reason":     reason,
            "set_at":     now_ts(),
        })
        audit_log("kill_global", extra={"reason": reason})
        print(f"[WARNING] AUDIT global kill activated: reason={reason}")
        return ok({"ok": True, "global_kill": True, "reason": reason})

    else:
        return err("target_type must be 'license_key' or 'global'")

# ── /api/admin/unkill ─────────────────────────────────────────────────────────
def handle_unkill(event):
    if not require_admin(event):
        return err("forbidden", 403)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    target_type = body.get("target_type", "")
    target_id   = body.get("target_id", "")

    if target_type == "license_key":
        if not target_id:
            return err("target_id required")
        licenses_table().update_item(
            Key={"license_key": target_id},
            UpdateExpression="SET #s = :s, revoked = :r REMOVE kill_reason",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "active", ":r": False}
        )
        audit_log("unkill_license", target_id)
        return ok({"ok": True, "unkilled": target_id})

    elif target_type == "global":
        config_table().put_item(Item={
            "config_key": "global_kill",
            "enabled":    False,
            "reason":     "",
            "set_at":     now_ts(),
        })
        audit_log("unkill_global")
        return ok({"ok": True, "global_kill": False})

    else:
        return err("target_type must be 'license_key' or 'global'")

# ── /api/admin/status ─────────────────────────────────────────────────────────
def handle_admin_status(event):
    if not require_admin(event):
        return err("forbidden", 403)

    gkill, greason = get_global_kill()
    return ok({
        "ok": True,
        "global_kill": gkill,
        "kill_reason": greason,
        "service_version": "0.4.0",
        "update_signing": "ed25519",
        "public_key_id": ED25519_KEY_ID,
        "ts": now_ts(),
    })

# ── /api/verify ───────────────────────────────────────────────────────────────
def handle_verify(event):
    """Verify a session token is still valid."""
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    token    = (body.get("token") or "").strip()
    token_id = (body.get("token_id") or "").strip()

    if not token or not token_id:
        return err("token and token_id required")

    r = tokens_table().get_item(Key={"token_id": token_id})
    item = r.get("Item")
    if not item:
        return err("invalid_token", 403)

    stored_hash = item.get("token_hash", "")
    if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored_hash):
        return err("invalid_token", 403)

    if item.get("expires", 0) < now_ts():
        return err("token_expired", 403)

    license_key = item.get("license_key", "")

    # Check if license was killed after token was issued
    lr = licenses_table().get_item(Key={"license_key": license_key})
    lic = lr.get("Item", {})
    if lic.get("revoked", False) or lic.get("status") != "active":
        return err("license_revoked", 403)

    # Check if license expired after token was issued (session tokens live up to
    # 30 days — far longer than short plans/trials). Mirrors handle_activate's
    # guard: expiry == 0 means lifetime; missing/unparseable keeps legacy behaviour.
    try:
        lic_expiry = int(lic.get("expiry") or 0)
    except (TypeError, ValueError):
        lic_expiry = 0
    if 0 < lic_expiry < now_ts():
        return err("license_expired", 403)

    return ok({
        "ok": True,
        "valid": True,
        "license_key_suffix": license_key[-4:],
        "expires": item.get("expires"),
    })


# ══════════════════════════════════════════════════════════════════════════════
# Staff / Admin handlers — added v0.4.1
# ══════════════════════════════════════════════════════════════════════════════

STAFF_TOKEN_SSM   = "/orion/staff_token_secret"
ENROLL_KEY_SSM    = "/orion/staff_enroll_key_hash"   # stored as sha256(key)
STAFF_TOKEN_TTL   = 86400 * 7    # 7 days
ENROLL_NONCE_TTL  = 300          # 5 min replay window

def staff_table():
    return get_ddb().Table("orion-staff")

def staff_audit_table():
    return get_ddb().Table("orion-staff-audit")

def nonces_table():
    return get_ddb().Table("orion-nonces")

# ── Staff auth helpers ────────────────────────────────────────────────────────

def get_staff_token_secret():
    return ssm_get(STAFF_TOKEN_SSM, decrypt=True)

def issue_staff_token(staff_id: str, machine_id: str) -> tuple:
    """Returns (token_str, token_id, expires_ts)."""
    token    = secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())
    expires  = now_ts() + STAFF_TOKEN_TTL
    secret = get_staff_token_secret()
    sig = hmac.new(secret.encode(),
                   f"{token_id}:{staff_id}:{machine_id}:{expires}".encode(),
                   hashlib.sha256).hexdigest()
    tokens_table().put_item(Item={
        "token_id":   token_id,
        "staff_id":   staff_id,
        "machine_id": machine_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expires":    expires,
        "created_at": now_ts(),
        "sig":        sig,
        "kind":       "staff",
    })
    return token, token_id, expires

def require_staff(event) -> tuple:
    """Returns (staff_item, None) or (None, error_response).
    Validates bearer token, expiry, machine_id, disabled flag."""
    h          = (event.get("headers") or {})
    auth       = h.get("authorization") or h.get("Authorization") or ""
    machine_id = (h.get("x-machine-id") or h.get("X-Machine-Id") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None, err("missing_token", 401)
    token = auth[7:].strip()
    if not token:
        return None, err("missing_token", 401)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        r = tokens_table().query(
            IndexName="token_hash-index",
            KeyConditionExpression="token_hash = :h",
            ExpressionAttributeValues={":h": token_hash},
            Limit=1
        )
        items = r.get("Items", [])
    except Exception:
        # Fallback: scan is expensive but safe if GSI not yet set up
        items = []

    if not items:
        # Try direct lookup if token_id is embedded in token (not our format)
        return None, err("invalid_token", 403)

    ti = items[0]
    if ti.get("kind") != "staff":
        return None, err("invalid_token", 403)
    if int(ti.get("expires", 0)) < now_ts():
        return None, err("token_expired", 403)
    # MED-2: bind UNCONDITIONALLY. A machine-bound staff token used WITHOUT the
    # X-Machine-Id header (or with a different one) is rejected — no short-circuit
    # that let a stolen bearer token skip binding by omitting the header.
    bound = ti.get("machine_id", "")
    if bound:
        if not machine_id or not secrets.compare_digest(machine_id, bound):
            return None, err("machine_mismatch", 403)

    staff_id = ti.get("staff_id", "")
    try:
        sr = staff_table().get_item(Key={"staff_id": staff_id})
        staff = sr.get("Item")
    except Exception:
        return None, err("internal_error", 500)

    if not staff:
        return None, err("staff_not_found", 403)
    if staff.get("disabled", False):
        return None, err("staff_disabled", 403)

    return staff, None

def staff_audit(action: str, actor: str, details: dict):
    try:
        staff_audit_table().put_item(Item={
            "audit_id":  str(uuid.uuid4()),
            "action":    action,
            "actor":     actor,
            "details":   json.dumps(details),
            "ts":        now_ts(),
        })
    except Exception as e:
        print(f"[WARN] staff_audit write failed: {e}")

def check_nonce(nonce: str, window: int = ENROLL_NONCE_TTL) -> bool:
    """Returns True if nonce is fresh (not replayed). Writes it to DynamoDB."""
    if not nonce:
        return False
    try:
        nonces_table().put_item(
            Item={"nonce": nonce, "ts": now_ts(), "expires": now_ts() + window},
            ConditionExpression="attribute_not_exists(nonce)"
        )
        return True
    except Exception:
        return False   # already used or DDB error

# ── POST /api/staff/enroll ────────────────────────────────────────────────────

def handle_staff_enroll(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    enroll_key  = (body.get("enroll_key")  or "").strip()
    staff_id    = (body.get("staff_id")    or "").strip()
    display_name = (body.get("display_name") or "").strip()
    machine_id  = (body.get("machine_id")  or "").strip()
    nonce       = (body.get("nonce")       or "").strip()
    ts_s        = body.get("timestamp", 0)

    if not all([enroll_key, staff_id, machine_id, nonce]):
        return err("missing_fields")
    if len(staff_id) > 64 or len(machine_id) > 256:
        return err("field_too_long")

    # Timestamp replay check
    try:
        if abs(now_ts() - int(ts_s)) > ENROLL_NONCE_TTL:
            return err("timestamp_expired", 401)
    except Exception:
        return err("invalid_timestamp", 400)

    # Nonce replay check
    if not check_nonce(nonce):
        return err("replay_detected", 401)

    # Verify enroll key against stored hash
    try:
        stored_hash = ssm_get(ENROLL_KEY_SSM, decrypt=True)
    except Exception:
        return err("enrollment_disabled", 403)
    candidate_hash = hashlib.sha256(enroll_key.encode()).hexdigest()
    if not secrets.compare_digest(candidate_hash, stored_hash):
        return err("invalid_enroll_key", 403)

    # Check staff_id not already taken
    try:
        existing = staff_table().get_item(Key={"staff_id": staff_id}).get("Item")
    except Exception as e:
        print(f"[ERROR] staff enroll DDB check: {e}")
        return err("internal_error", 500)
    if existing:
        return err("staff_id_taken", 409)

    # Create staff record
    try:
        staff_table().put_item(Item={
            "staff_id":     staff_id,
            "display_name": display_name or staff_id,
            "machine_id":   machine_id,
            "role":         "staff",
            "disabled":     False,
            "created_at":   now_ts(),
        })
    except Exception as e:
        print(f"[ERROR] staff enroll create: {e}")
        return err("internal_error", 500)

    token, token_id, expires = issue_staff_token(staff_id, machine_id)
    staff_audit("staff_enrolled", staff_id, {"machine_suffix": machine_id[-4:]})
    print(f"[INFO] Staff enrolled: {staff_id}")
    return ok({"ok": True, "token": token, "tid": token_id, "expires": expires, "role": "staff"})

# ── POST /api/staff/login ─────────────────────────────────────────────────────

def handle_staff_login(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    staff_id   = (body.get("staff_id")   or "").strip()
    machine_id = (body.get("machine_id") or "").strip()
    nonce      = (body.get("nonce")      or "").strip()
    ts_s       = body.get("timestamp", 0)

    if not all([staff_id, machine_id, nonce]):
        return err("missing_fields")
    try:
        if abs(now_ts() - int(ts_s)) > ENROLL_NONCE_TTL:
            return err("timestamp_expired", 401)
    except Exception:
        return err("invalid_timestamp", 400)
    if not check_nonce(nonce):
        return err("replay_detected", 401)

    try:
        sr = staff_table().get_item(Key={"staff_id": staff_id})
        staff = sr.get("Item")
    except Exception:
        return err("internal_error", 500)
    if not staff:
        return err("invalid_credentials", 403)
    if staff.get("disabled", False):
        return err("staff_disabled", 403)
    if staff.get("machine_id") and staff["machine_id"] != machine_id:
        return err("machine_mismatch", 403)

    token, token_id, expires = issue_staff_token(staff_id, machine_id)
    staff_audit("staff_login", staff_id, {"machine_suffix": machine_id[-4:]})
    print(f"[INFO] Staff login: {staff_id}")
    return ok({"ok": True, "token": token, "tid": token_id, "expires": expires,
               "role": staff.get("role", "staff"), "display_name": staff.get("display_name", "")})

# ── GET /api/staff/whoami ─────────────────────────────────────────────────────

def handle_staff_whoami(event):
    staff, err_resp = require_staff(event)
    if err_resp:
        return err_resp
    return ok({"ok": True, "staff_id": staff["staff_id"],
               "display_name": staff.get("display_name", ""),
               "role": staff.get("role", "staff"),
               "disabled": staff.get("disabled", False)})

# ── POST /api/staff/license ───────────────────────────────────────────────────

def handle_staff_license(event):
    staff, err_resp = require_staff(event)
    if err_resp:
        return err_resp
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    action      = (body.get("action") or "").strip().lower()
    license_key = (body.get("license_key") or "").strip().upper()
    if not license_key:
        return err("missing_license_key")

    try:
        r = licenses_table().get_item(Key={"license_key": license_key})
        item = r.get("Item")
    except Exception:
        return err("internal_error", 500)
    if not item:
        return err("invalid_key", 404)

    if action == "lookup":
        safe = {
            "license_key_suffix": license_key[-4:],
            "status": item.get("status"),
            "revoked": item.get("revoked", False),
            "plan": item.get("plan"),
            "expiry": item.get("expiry"),
            "activations": item.get("activations", 0),
            "machine_suffix": (item.get("machine_id") or "")[-4:] or None,
        }
        staff_audit("staff_license_lookup", staff["staff_id"], {"key_suffix": license_key[-4:]})
        return ok({"ok": True, "license": safe})
    else:
        return err("unknown_action")

# ── POST /api/staff/tamper-report ─────────────────────────────────────────────

def handle_staff_tamper_report(event):
    staff, err_resp = require_staff(event)
    if err_resp:
        return err_resp
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    report_type = (body.get("type") or "unknown").strip()[:64]
    details     = body.get("details") or {}
    machine_id  = (body.get("machine_id") or "").strip()
    staff_audit("staff_tamper_report", staff["staff_id"], {
        "type": report_type,
        "machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id,
        "detail_keys": list(details.keys()) if isinstance(details, dict) else [],
    })
    print(f"[WARNING] Staff tamper report from {staff['staff_id']}: type={report_type}")
    return ok({"ok": True, "received": True})

# ── GET /api/admin/search ──────────────────────────────────────────────────────

def handle_admin_search(event):
    if not require_admin(event):
        return err("forbidden", 403)
    qs = (event.get("queryStringParameters") or {})
    q  = (qs.get("q") or "").strip().upper()
    if not q:
        return err("missing_query")
    try:
        r = licenses_table().get_item(Key={"license_key": q})
        item = r.get("Item")
    except Exception:
        return err("internal_error", 500)
    if not item:
        return ok({"ok": True, "found": False})
    safe = {
        "license_key_suffix": q[-4:],
        "status": item.get("status"),
        "revoked": item.get("revoked", False),
        "plan": item.get("plan"),
        "expiry": item.get("expiry"),
        "activations": item.get("activations", 0),
        "email": item.get("email", ""),
        "machine_suffix": (item.get("machine_id") or "")[-4:] or None,
    }
    return ok({"ok": True, "found": True, "license": safe})

# ── GET /api/admin/whoami ──────────────────────────────────────────────────────

def handle_admin_whoami(event):
    if not require_admin(event):
        return err("forbidden", 403)
    return ok({"ok": True, "role": "admin", "service_version": "0.4.0"})

# ── POST /api/admin/tamper-report ─────────────────────────────────────────────

def handle_admin_tamper_report(event):
    if not require_admin(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    report_type = (body.get("type") or "unknown").strip()[:64]
    details     = body.get("details") or {}
    machine_id  = (body.get("machine_id") or "").strip()
    staff_audit("admin_tamper_report", "admin", {
        "type": report_type,
        "machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id,
        "detail_keys": list(details.keys()) if isinstance(details, dict) else [],
    })
    print(f"[WARNING] Admin tamper report: type={report_type}")
    return ok({"ok": True, "received": True})



import os as _os

LEASE_TTL_S = 900  # 15-minute heartbeat window
EDGE_AUTH_SECRET_SSM = "/orion/edge_auth_secret"

_edge_auth_secret_cache = None

def _get_edge_auth_secret():
    global _edge_auth_secret_cache
    if _edge_auth_secret_cache:
        return _edge_auth_secret_cache
    import boto3
    ssm = boto3.client("ssm", region_name="us-east-1")
    try:
        resp = ssm.get_parameter(Name=EDGE_AUTH_SECRET_SSM, WithDecryption=True)
        _edge_auth_secret_cache = resp["Parameter"]["Value"]
        return _edge_auth_secret_cache
    except Exception as e:
        print(f"[EDGE_AUTH] SSM fetch failed: {e}")
        return None

def require_edge_auth(headers):
    """HIGH-4: edge auth is now enforced ALWAYS and fails CLOSED.
    Returns None if OK, or a 403 error response.

    Two former fail-open holes are closed:
      1. Missing/misnamed SSM secret used to pass through -> now DENY.
      2. Enforcement used to require the EDGE_AUTH_ENFORCE env var -> now always on.
    Timing-safe compare on the header."""
    secret = _get_edge_auth_secret()
    if not secret:
        # Fail CLOSED: without a configured edge secret we cannot authenticate the
        # caller, so refuse rather than silently disabling the gate.
        print("[EDGE_AUTH] DENY: SSM secret missing (fail-closed)")
        return err("forbidden", 403)
    incoming = headers.get("x-edge-auth") or headers.get("X-Edge-Auth") or ""
    import hmac as _hmac
    if not _hmac.compare_digest(incoming.strip(), secret.strip()):
        print(f"[EDGE_AUTH] DENY: bad/absent header ua={headers.get('user-agent','')[:60]}")
        return err("forbidden", 403)
    return None

def handle_validate(event):
    """POST /api/license/check - heartbeat/lease check"""
    import time
    # NEW-5: guard against malformed body (every other handler wraps json.loads).
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json", 400)
    license_key = (body.get("license_key") or "").strip().upper()
    machine_id  = (body.get("machine_id")  or "").strip()
    if not license_key or not machine_id:
        return err("license_key and machine_id required", 400)
    # MED-1: rate-limit the heartbeat too (per key + per source IP).
    if not rate_limit_ok("check", license_key) or not rate_limit_ok("check_ip", _client_ip(event)):
        return err("rate_limited", 429)
    gkill, _ = get_global_kill()
    if gkill:
        return err("service disabled", 503)
    try:
        resp = licenses_table().get_item(Key={"license_key": license_key})
    except Exception as e:
        print(f"[check] DDB error: {e}")
        return err("internal error", 500)
    item = resp.get("Item")
    if not item:
        return err("invalid license", 403)
    if item.get("machine_id") != machine_id:
        return err("machine mismatch", 403)
    if item.get("revoked", False) or item.get("status") == "revoked":
        return err("license revoked", 403)
    now = int(time.time())
    # Fail closed on expiry (mirrors handle_activate's guard). expiry == 0 means
    # lifetime; a missing/unparseable expiry keeps legacy behaviour (no check).
    try:
        expiry = int(item.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if 0 < expiry < now:
        return err("license expired", 403)
    lease_expires_at = now + LEASE_TTL_S
    resp_body = {
        "ok": True,
        "license_key": license_key,
        "machine_id": machine_id,
        "lease_expires_at": lease_expires_at,
        "tier": item.get("tier", "standard"),
    }
    # ── NEW-1: sign the lease so the client (LeaseGate::verifyLeaseSignature) can
    # cryptographically trust lease_expires_at. A MITM / cracked client can no
    # longer fabricate a far-future lease without the private key. Fail-soft only
    # if the signing key is not yet provisioned (operator must set SSM before the
    # client embeds the public key and makes the signature mandatory).
    try:
        resp_body["lease_sig"] = sign_lease(license_key, machine_id, lease_expires_at)
        resp_body["public_key_id"] = LEASE_KEY_ID
    except Exception as e:
        print(f"[LEASE] signing unavailable (fail-soft): {e}")
    return ok(resp_body)

# ── bot routes (Discord gateway bot / Cloudflare Worker) ───────────────────────
# Auth: X-Orion-Bot-Secret header, checked against SSM /orion/bot_service_secret
# (require_bot). Delivery (DM + role-grant) happens CLIENT-SIDE in orion_bot.py —
# these handlers only mint/read license state and return a bare license_key.
def _find_license_by_discord(discord_id):
    items = licenses_table().scan(
        FilterExpression="discord_user_id = :d",
        ExpressionAttributeValues={":d": str(discord_id)},
    ).get("Items", [])
    cand = [i for i in items if not i.get("revoked", False) and i.get("status") == "active"]
    cand.sort(key=lambda i: int(i.get("created_at", 0)), reverse=True)
    return cand[0] if cand else None

# ── HIGH-3: order_id spend-once markers (atomic claim, mint-once per order) ─────
def _claim_order(order_id, source):
    """Atomically claim ORDER#<order_id>. Returns None if freshly claimed
    (caller should mint), or the previously-minted license_key (may be "") if the
    order was already spent (caller should NOT mint again)."""
    marker = "ORDER#" + str(order_id)
    try:
        licenses_table().put_item(
            Item={"license_key": marker, "status": "order_claim", "revoked": True,
                  "source": source, "created_at": now_ts()},
            ConditionExpression="attribute_not_exists(license_key)")
        return None
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        existing = licenses_table().get_item(Key={"license_key": marker}).get("Item", {})
        return existing.get("minted_license_key", "")

def _bind_order_license(order_id, license_key):
    try:
        licenses_table().update_item(
            Key={"license_key": "ORDER#" + str(order_id)},
            UpdateExpression="SET minted_license_key = :k",
            ExpressionAttributeValues={":k": license_key})
    except Exception as e:
        print(f"[WARN] order marker bind failed order={order_id}: {e}")

def handle_bot_killswitch(event):
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    action = str(body.get("action", "status")).lower()
    if action == "status":
        gkill, _ = get_global_kill()
        return ok({"ok": True, "enabled": gkill})
    if action in ("on", "enable", "engage"):
        val = True
    elif action in ("off", "disable", "disengage"):
        val = False
    else:
        return err("action must be on, off, or status", 400)
    by = str(body.get("by", ""))
    config_table().put_item(Item={
        "config_key": "global_kill",
        "enabled": val,
        "reason": f"bot:{by}" if val else "",
    })
    audit_log("bot_killswitch", extra={"enabled": val, "by": by})
    print(f"[WARNING] AUDIT bot_killswitch enabled={val} by={by}")
    return ok({"ok": True, "enabled": val,
               "message": f"Launcher killswitch {'ENGAGED' if val else 'released'}."})

def _discord_dm(discord_user_id, embed):
    """Open a DM channel with the user and post an embed. Raises on failure.

    Purchases are DM'd by the Gumroad webhook Lambda's send_discord_dm(); this is
    the same flow for trials. It lives here rather than in the bot because the
    Cloudflare Worker that now serves interactions is a stateless proxy that must
    never see a license key."""
    token = ssm_get(DISCORD_BOT_TOKEN_SSM, decrypt=True)
    req = urllib.request.Request(
        DISCORD_API + "/users/@me/channels",
        data=json.dumps({"recipient_id": str(discord_user_id)}).encode(),
        method="POST")
    req.add_header("Authorization", "Bot " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            channel_id = json.loads(r.read())["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DM channel open failed {e.code}: {e.read().decode()[:200]}")
    msg = urllib.request.Request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        data=json.dumps({"embeds": [embed]}).encode(), method="POST")
    msg.add_header("Authorization", "Bot " + token)
    msg.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(msg, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DM send failed {e.code}: {e.read().decode()[:200]}")


def _discord_add_role(discord_user_id, role_ssm):
    """Best-effort role grant. Never raises: the license is already minted and
    delivered by this point, so a missing role id, an unset guild id or a role
    hierarchy problem must not turn a successful issue into a failure."""
    try:
        role_id = ssm_get(role_ssm, decrypt=False).strip()
        guild_id = ssm_get(DISCORD_GUILD_ID_SSM, decrypt=False).strip()
        if not role_id or not guild_id:
            print(f"[WARN] role grant skipped: {role_ssm} or guild id unset")
            return False
        token = ssm_get(DISCORD_BOT_TOKEN_SSM, decrypt=True)
        req = urllib.request.Request(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
            data=b"", method="PUT")
        req.add_header("Authorization", "Bot " + token)
        req.add_header("Content-Length", "0")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[WARN] role grant failed ({role_ssm}): {e}")
        return False


def handle_bot_status(event):
    """POST /api/bot/status — read-only entitlement lookup for the Discord bot's
    /purchase command (shows "you already own X" instead of a blind buy prompt).

    Deliberately returns NO license_key: this response travels back out through
    the Cloudflare Worker into an interaction reply, and a key has no business on
    that path. /deliver and /provision remain the only mint/return-key routes.

    _find_license_by_discord already filters to non-revoked + status=active and
    returns the newest, but status alone doesn't imply unexpired — check expiry
    here so a lapsed customer is told to renew rather than shown as active."""
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    discord_id = str(body.get("discord_id", "")).strip()
    if not discord_id:
        return err("discord_id is required", 400)
    item = _find_license_by_discord(discord_id)
    if not item:
        return ok({"ok": True, "has_license": False})
    now = now_ts()
    expiry = int(item.get("expiry", 0) or 0)
    lifetime = expiry == 0
    return ok({
        "ok": True,
        "has_license": True,
        "plan": str(item.get("plan", "") or ""),
        "lifetime": lifetime,
        "expiry": expiry,
        "active": lifetime or expiry > now,
        "seconds_left": 0 if lifetime else max(0, expiry - now),
        "bound": bool(item.get("machine_id", "")),
    })

def handle_bot_hwid_reset(event):
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    discord_id = str(body.get("discord_id", "")).strip()
    if not discord_id:
        return err("discord_id is required", 400)
    item = _find_license_by_discord(discord_id)
    if not item:
        return ok({"ok": False, "message": "No active license is linked to your account."})
    key = item["license_key"]
    now = now_ts()
    last = int(item.get("bot_reset_last_at", 0))
    if last and (now - last) < BOT_RESET_COOLDOWN_S:
        rem = BOT_RESET_COOLDOWN_S - (now - last)
        return ok({"ok": False, "message":
                   f"You recently reset. Try again in {rem // 3600}h {(rem % 3600) // 60}m, or open a ticket."})
    cutoff = now - 30 * 86400
    cnt = int(item.get("bot_reset_count_30d", 0))
    window_start = int(item.get("bot_reset_window_start", 0))
    if window_start < cutoff:
        cnt = 0
    if cnt >= BOT_RESET_MAX_30D:
        return ok({"ok": False, "message":
                   f"You've used the max {BOT_RESET_MAX_30D} resets in 30 days. Open a support ticket."})
    licenses_table().update_item(
        Key={"license_key": key},
        UpdateExpression=("SET machine_id = :e, bot_reset_last_at = :n, "
                          "bot_reset_count_30d = :c, bot_reset_window_start = :w"),
        ExpressionAttributeValues={
            ":e": "", ":n": now, ":c": cnt + 1,
            ":w": window_start if window_start >= cutoff else now,
        })
    audit_log("bot_hwid_reset", key, {"discord_id": discord_id})
    left = BOT_RESET_MAX_30D - (cnt + 1)
    return ok({"ok": True, "message": f"HWID reset done — activate on your new PC. ({left} left this month.)"})

def handle_bot_trial(event):
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    discord_id = str(body.get("discord_id", "")).strip()
    if not discord_id:
        return err("discord_id is required", 400)
    now = now_ts()
    claim_key = "TRIAL#" + discord_id
    try:
        licenses_table().put_item(
            Item={"license_key": claim_key, "status": "trial_claim", "revoked": True, "created_at": now},
            ConditionExpression="attribute_not_exists(license_key)")
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return ok({"ok": False, "message": "You've already claimed your free trial."})
    key = gen_license_key()
    expiry = now + TRIAL_DAYS * 86400
    licenses_table().put_item(Item={
        "license_key": key, "status": "active", "revoked": False, "plan": "trial",
        "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
        "created_at": now, "discord_user_id": discord_id, "source": "trial",
    })
    # Deliver from here, not through the Worker. If the DM bounces (closed DMs),
    # roll the claim back so the user can retry after opening them — otherwise a
    # single bounce permanently burns their one trial and creates a support ticket.
    try:
        _discord_dm(discord_id, {
            "title": f"🎁 Your free {TRIAL_DAYS}-day Venice trial",
            "description": (
                f"Your trial is active for **{TRIAL_DAYS} days**. It locks to the "
                "first PC you activate on, so use the machine you actually play on.\n\n"
                "When it runs out, run `/purchase` to keep going."),
            "fields": [
                {"name": "License Key", "value": f"||`{key}`||", "inline": False},
                {"name": "Support", "value": "Open a ticket if you hit any trouble.", "inline": False},
            ],
            "color": BRAND_EMBED_COLOR,
            "footer": {"text": "Venice — do not share your key"},
        })
    except Exception as e:
        print(f"[WARN] trial DM failed discord={discord_id}: {e}")
        try:
            licenses_table().delete_item(Key={"license_key": claim_key})
            licenses_table().update_item(
                Key={"license_key": key},
                UpdateExpression="SET #s = :s, revoked = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "dm_failed", ":r": True})
        except Exception as e2:
            print(f"[ERROR] trial rollback failed discord={discord_id}: {e2}")
        return ok({"ok": False, "message":
                   "I couldn't DM you. Turn on **Privacy Settings → Direct Messages** "
                   "for this server, then run `/claim_trial` again."})
    _discord_add_role(discord_id, DISCORD_TRIAL_ROLE_SSM)
    audit_log("bot_trial_issued", key, {"discord_id": discord_id})
    print(f"[INFO] AUDIT bot_trial_issued key ...{key[-4:]} discord={discord_id}")
    # No license_key in the response: it's already delivered, and this payload
    # travels back out through the stateless Worker into a Discord reply.
    return ok({"ok": True, "plan": "trial", "expiry": expiry,
               "message": f"✅ Your {TRIAL_DAYS}-day trial key is in your DMs!"})

def handle_bot_deliver(event):
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    discord_id = str(body.get("discord_id", "")).strip()
    plan = str(body.get("plan", "")).strip().lower() or "month"
    days = int(body.get("days") or TIER_DAYS.get(plan, 30))
    order_id = str(body.get("order_id", "")).strip()

    # HIGH-3: /deliver is the admin manual-mint path (no order in the general
    # case), but when an order_id IS supplied it is spend-once, just like
    # /provision — a replayed deliver can't double-mint the same order.
    if order_id:
        dup = _claim_order(order_id, "admin_deliver")
        if dup is not None:
            return ok({"ok": True, "license_key": dup, "plan": plan,
                       "duplicate_order": True, "message": "Order already delivered."}, 200)

    now = now_ts()
    key = gen_license_key()
    expiry = 0 if plan == "lifetime" else now + days * 86400
    item = {
        "license_key": key, "status": "active", "revoked": False, "plan": plan,
        "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
        "created_at": now, "source": "admin_deliver",
    }
    if discord_id:
        item["discord_user_id"] = discord_id
    if order_id:
        item["order_id"] = order_id
    licenses_table().put_item(Item=item)
    if order_id:
        _bind_order_license(order_id, key)
    audit_log("bot_deliver", key, {"plan": plan, "discord_id": discord_id, "order_id": order_id})
    print(f"[INFO] AUDIT bot_deliver key ...{key[-4:]} plan={plan} discord={discord_id}")
    return ok({"ok": True, "license_key": key, "plan": plan, "expiry": expiry,
               "message": f"Minted {plan} license."})

def handle_bot_provision(event):
    """POST /api/bot/provision — automated mint for the SellHub webhook Lambda
    (orion-sellhub-webhook). Kept separate from /api/bot/deliver (admin-triggered,
    no order_id) so audit_log entries and future validation don't conflate the two
    call sites."""
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    plan = str(body.get("plan", "")).strip().lower() or "month"
    days = int(body.get("days") or TIER_DAYS.get(plan, 30))
    discord_id = str(body.get("discord_user_id", "")).strip()
    order_id = str(body.get("order_id", "")).strip()

    # ── HIGH-3: scope the mint to a verified-UNSPENT order_id. One backend license
    # per paid order EVER — an attacker replaying a webhook (or a leaked webhook
    # secret firing repeated provisions) can no longer fan out N licenses. The
    # order marker is claimed atomically BEFORE minting.
    if not order_id:
        return err("order_id required", 400)
    dup = _claim_order(order_id, "sellhub")
    if dup is not None:
        return ok({"ok": True, "license_key": dup, "plan": plan,
                   "duplicate_order": True}, 200)

    now = now_ts()
    key = gen_license_key()
    expiry = 0 if plan == "lifetime" else now + days * 86400
    item = {
        "license_key": key, "status": "active", "revoked": False, "plan": plan,
        "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
        "created_at": now, "source": "sellhub", "order_id": order_id,
    }
    if discord_id:
        item["discord_user_id"] = discord_id
    licenses_table().put_item(Item=item)
    _bind_order_license(order_id, key)
    audit_log("bot_provision", key, {"plan": plan, "days": days, "discord_id": discord_id, "order_id": order_id})
    print(f"[INFO] AUDIT bot_provision key ...{key[-4:]} plan={plan} days={days} order={order_id}")
    return ok({"ok": True, "license_key": key, "plan": plan, "expiry": expiry}, 201)

# ══════════════════════════════════════════════════════════════════════════════
# Shard gate — server-held key fragments for the OrionPack PE packer
# ══════════════════════════════════════════════════════════════════════════════

_shard_enc_key_cache = None

def _get_shard_enc_key():
    global _shard_enc_key_cache
    if _shard_enc_key_cache is None:
        _shard_enc_key_cache = bytes.fromhex(ssm_get(SHARD_ENC_KEY_SSM, decrypt=True))
    return _shard_enc_key_cache

def _shard_encrypt(plaintext: bytes) -> tuple:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_shard_enc_key()
    nonce = _os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return ct.hex(), nonce.hex()

def _shard_decrypt(ct_hex: str, nonce_hex: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_shard_enc_key()
    return AESGCM(key).decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None)

def handle_shard_store(event):
    if not require_admin(event):
        admin_alert("shard_store_forbidden", event)
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    build_id = (body.get("build_id") or "").strip()
    shard_hex = (body.get("shard") or "").strip()
    license_id = (body.get("license_id") or "").strip()
    hwid_hash = (body.get("hwid_hash") or "").strip()
    max_activations = int(body.get("max_activations", 0))
    ttl_hours = int(body.get("ttl_hours", 0))
    if not build_id or not shard_hex:
        return err("build_id and shard required")
    try:
        shard_bytes = bytes.fromhex(shard_hex)
    except ValueError:
        return err("shard must be hex-encoded")
    if len(shard_bytes) != 32:
        return err("shard must be exactly 32 bytes")
    try:
        ct_hex, nonce_hex = _shard_encrypt(shard_bytes)
    except Exception as e:
        print(f"[SHARD] encryption failed: {e}")
        return err("internal_error", 500)
    now = now_ts()
    expires_at = (now + ttl_hours * 3600) if ttl_hours > 0 else 0
    try:
        shards_table().put_item(Item={
            "build_id": build_id,
            "encrypted_shard": ct_hex,
            "shard_nonce": nonce_hex,
            "license_id": license_id,
            "hwid_hash": hwid_hash,
            "created_at": now,
            "expires_at": expires_at,
            "max_activations": max_activations,
            "activation_count": 0,
            "revoked": False,
        })
    except Exception as e:
        print(f"[SHARD] store failed: {e}")
        return err("internal_error", 500)
    audit_log("shard_stored", extra={"build_id": build_id[:16]})
    print(f"[INFO] Shard stored: build_id={build_id[:16]}...")
    return ok({"ok": True, "build_id": build_id,
               "expires_at": expires_at or None}, 201)

def handle_shard_retrieve(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    build_id = (body.get("build_id") or "").strip()
    license_key = (body.get("license_key") or "").strip().upper()
    machine_id = (body.get("machine_id") or "").strip()
    if not build_id:
        return err("build_id required")
    if not license_key or not machine_id:
        return err("license_key and machine_id required")
    if not rate_limit_ok("shard_retrieve", build_id, limit=SHARD_RETRIEVE_LIMIT):
        return err("rate_limited", 429)
    try:
        r = shards_table().get_item(Key={"build_id": build_id})
    except Exception as e:
        print(f"[SHARD] retrieve DDB error: {e}")
        return err("internal_error", 500)
    item = r.get("Item")
    if not item:
        return err("not_found", 404)
    if item.get("revoked", False):
        audit_log("shard_retrieve_revoked", extra={"build_id": build_id[:16]})
        return err("revoked", 403)
    expires_at = int(item.get("expires_at", 0))
    if expires_at > 0 and expires_at < now_ts():
        return err("expired", 403)
    max_act = int(item.get("max_activations", 0))
    act_count = int(item.get("activation_count", 0))
    if max_act > 0 and act_count >= max_act:
        return err("max_activations", 403)
    stored_hwid = item.get("hwid_hash", "")
    if stored_hwid:
        incoming_hwid = hashlib.sha256(machine_id.encode()).hexdigest()
        if not secrets.compare_digest(incoming_hwid, stored_hwid):
            audit_log("shard_hwid_mismatch", extra={"build_id": build_id[:16]})
            return err("hwid_mismatch", 403)
    try:
        lr = licenses_table().get_item(Key={"license_key": license_key})
        lic = lr.get("Item")
    except Exception:
        return err("internal_error", 500)
    if not lic:
        return err("invalid_license", 403)
    if lic.get("revoked", False) or lic.get("status") != "active":
        return err("license_revoked", 403)
    if lic.get("machine_id") and lic["machine_id"] != machine_id:
        return err("machine_mismatch", 403)
    try:
        lic_expiry = int(lic.get("expiry") or 0)
    except (TypeError, ValueError):
        lic_expiry = 0
    if 0 < lic_expiry < now_ts():
        return err("license_expired", 403)
    try:
        shard_bytes = _shard_decrypt(item["encrypted_shard"], item["shard_nonce"])
    except Exception as e:
        print(f"[SHARD] decrypt failed: {e}")
        return err("internal_error", 500)
    try:
        shards_table().update_item(
            Key={"build_id": build_id},
            UpdateExpression="ADD activation_count :one SET last_retrieved = :t",
            ExpressionAttributeValues={":one": 1, ":t": now_ts()})
    except Exception:
        pass
    audit_log("shard_retrieved", license_key,
              {"build_id": build_id[:16], "machine_suffix": machine_id[-4:]})
    return ok({"ok": True, "shard": shard_bytes.hex()})

def handle_shard_revoke(event):
    if not require_admin(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    build_id = (body.get("build_id") or "").strip()
    if not build_id:
        return err("build_id required")
    try:
        shards_table().update_item(
            Key={"build_id": build_id},
            UpdateExpression="SET revoked = :r, revoked_at = :t",
            ExpressionAttributeValues={":r": True, ":t": now_ts()})
    except Exception as e:
        print(f"[SHARD] revoke failed: {e}")
        return err("internal_error", 500)
    audit_log("shard_revoked", extra={"build_id": build_id[:16]})
    print(f"[WARNING] Shard revoked: build_id={build_id[:16]}...")
    return ok({"ok": True, "revoked": True})

def handle_shard_status(event):
    if not require_admin(event):
        return err("forbidden", 403)
    qs = (event.get("queryStringParameters") or {})
    build_id = (qs.get("build_id") or "").strip()
    if not build_id:
        return err("build_id required")
    try:
        r = shards_table().get_item(Key={"build_id": build_id})
    except Exception:
        return err("internal_error", 500)
    item = r.get("Item")
    if not item:
        return err("not_found", 404)
    return ok({
        "ok": True,
        "build_id": build_id,
        "license_id": item.get("license_id", ""),
        "hwid_hash": item.get("hwid_hash", ""),
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at", 0),
        "max_activations": item.get("max_activations", 0),
        "activation_count": item.get("activation_count", 0),
        "revoked": item.get("revoked", False),
        "last_retrieved": item.get("last_retrieved"),
    })

# ── router ────────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path   = event.get("rawPath", "") or event.get("path", "")

    print(f"[INFO] {method} {path}")

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    # HIGH-3 + HIGH-4: edge auth now applies to ALL routes including /api/bot/*
    # (the webhooks/worker present the edge secret on their calls). No exemption.
    edge_err = require_edge_auth(headers)
    if edge_err:
        return edge_err

    # CRIT-2 / MED-1: throttle + alert on the privileged admin surface.
    if path.startswith("/api/admin/"):
        if not rate_limit_ok("admin", _client_ip(event), limit=20, window=60):
            admin_alert("admin_rate_limited", event, {"path": path})
            return err("rate_limited", 429)

    if path == "/api/version":
        return handle_version(event)
    elif path == "/api/activate" and method == "POST":
        return handle_activate(event)
    elif path == "/api/license/check" and method == "POST":
        return handle_validate(event)
    elif path == "/api/license/redeem" and method == "POST":
            return handle_activate(event)
    elif path == "/api/deactivate" and method == "POST":
        return handle_deactivate(event)
    elif path == "/api/verify" and method == "POST":
        return handle_verify(event)
    elif path == "/api/update":
        if method == "GET":
            return handle_get_update(event)
        elif method == "POST":
            return handle_post_update(event)
        else:
            return err("method not allowed", 405)
    elif path == "/api/admin/provision" and method == "POST":
        return handle_provision(event)
    elif path == "/api/admin/kill" and method == "POST":
        return handle_kill(event)
    elif path == "/api/admin/unkill" and method == "POST":
        return handle_unkill(event)
    elif path == "/api/admin/status" and method == "GET":
        return handle_admin_status(event)
    # ── bot routes ───────────────────────────────────────────────────────────
    elif path == "/api/bot/killswitch" and method == "POST":
        return handle_bot_killswitch(event)
    elif path == "/api/bot/status" and method == "POST":
        return handle_bot_status(event)
    elif path == "/api/bot/hwid-reset" and method == "POST":
        return handle_bot_hwid_reset(event)
    elif path == "/api/bot/trial" and method == "POST":
        return handle_bot_trial(event)
    elif path == "/api/bot/deliver" and method == "POST":
        return handle_bot_deliver(event)
    elif path == "/api/bot/provision" and method == "POST":
        return handle_bot_provision(event)
    # ── staff routes ─────────────────────────────────────────────────────────
    elif path == "/api/staff/enroll" and method == "POST":
        return handle_staff_enroll(event)
    elif path == "/api/staff/login" and method == "POST":
        return handle_staff_login(event)
    elif path == "/api/staff/whoami" and method == "GET":
        return handle_staff_whoami(event)
    elif path == "/api/staff/license" and method == "POST":
        return handle_staff_license(event)
    elif path == "/api/staff/tamper-report" and method == "POST":
        return handle_staff_tamper_report(event)
    # ── admin staff/search/whoami/tamper routes ───────────────────────────────
    elif path == "/api/admin/search" and method == "GET":
        return handle_admin_search(event)
    elif path == "/api/admin/whoami" and method == "GET":
        return handle_admin_whoami(event)
    elif path == "/api/admin/staff" and method == "GET":
        return handle_admin_staff(event)
    elif path == "/api/admin/staff" and method == "POST":
        return handle_admin_staff(event)
    elif path == "/api/admin/staff/audit" and method == "GET":
        return handle_admin_staff_audit(event)
    elif path == "/api/admin/tamper-report" and method == "POST":
        return handle_admin_tamper_report(event)
    # ── shard routes (OrionPack packer key fragments) ────────────────────────
    elif path == "/api/shard/store" and method == "POST":
        return handle_shard_store(event)
    elif path == "/api/shard/retrieve" and method == "POST":
        return handle_shard_retrieve(event)
    elif path == "/api/admin/shard/revoke" and method == "POST":
        return handle_shard_revoke(event)
    elif path == "/api/admin/shard/status" and method == "GET":
        return handle_shard_status(event)
    else:
        return err("not found", 404)

# ── GET /api/admin/staff (missing handler backfill) ──────────────────────────
def handle_admin_staff(event):
    if not require_admin(event):
        return err("forbidden", 403)
    try:
        result = staff_table().scan(Limit=200)
        members = [{
            "staff_id": s.get("staff_id"),
            "display_name": s.get("display_name", ""),
            "role": s.get("role", "staff"),
            "disabled": s.get("disabled", False),
            "created_at": s.get("created_at")
        } for s in result.get("Items", [])]
        return ok({"ok": True, "staff": members})
    except Exception as e:
        print(f"[ERROR] admin staff scan: {e}")
        return err("internal_error", 500)

# ── GET /api/admin/staff/audit (missing handler backfill) ────────────────────
def handle_admin_staff_audit(event):
    if not require_admin(event):
        return err("forbidden", 403)
    try:
        result = staff_audit_table().scan(Limit=200)
        logs = sorted(result.get("Items", []), key=lambda x: int(x.get("ts", 0)), reverse=True)
        safe = [{"audit_id": l.get("audit_id"), "action": l.get("action"),
                 "actor": l.get("actor"), "ts": l.get("ts")} for l in logs]
        return ok({"ok": True, "audit": safe})
    except Exception as e:
        print(f"[ERROR] admin audit scan: {e}")
        return err("internal_error", 500)
