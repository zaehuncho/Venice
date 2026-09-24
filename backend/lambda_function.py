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
PAIR_ISSUER_SECRET_SSM = "/orion/website_pair_secret"
BOT_SECRET_SSM_NAMES = [
    "/orion/bot_service_secret",     # Discord bot host (orion_bot.py) — legacy default
    "/orion/webhook_bot_secret",     # gumroad + sellhub webhook Lambdas
    "/orion/worker_bot_secret",      # Cloudflare worker
]
TIER_DAYS            = {"day": 1, "week": 7, "month": 30, "lifetime": 36500}
# Owner rule 2026-09-15: the ONLY sellable plan is the recurring 30-day `month`,
# preceded by the free 7-day `trial`. `day`/`week`/`lifetime` stay in TIER_DAYS so
# keys already sold keep resolving and staff can still mint a comp — they are not
# offered on any customer-facing surface (worker /purchase, bot embeds, Gumroad).
SELLABLE_PLAN        = "month"
LEGACY_PLANS         = ("day", "week", "lifetime")
BOT_RESET_COOLDOWN_S = 86400
BOT_RESET_MAX_30D    = 3          # legacy cap — superseded by the §3 free/paid/deduct policy

# ── Admin Panel V2 (owner-ratified 2026-09-13/14) ─────────────────────────────
# Owner TOTP (RFC 6238, HMAC-SHA1, 30 s, +/-1 step). Base32 secret in SSM.
OWNER_TOTP_SSM       = "/orion/owner_totp_secret"
TOTP_STEP_S          = 30
TOTP_DIGITS          = 6
TOTP_WINDOW_STEPS    = 1
TOTP_ISSUER          = "Venice"

# Reason string on every mutation (contract: <= 200 chars).
REASON_MAX           = 200

# §3 HWID reset policy defaults (overridable via config reset_policy_defaults).
# Owner rule 2026-09-15: 3 free resets per key, then every further reset costs ONE
# day off the subscription. There is no customer-buyable reset any more — the paid
# credit path survives only for staff goodwill (/api/bot/hwid-credit).
RESET_FREE_DEFAULT   = 3
RESET_PENALTY_DAYS   = 1          # aka `deduct_days` in the contract
RESET_COOLDOWN_S     = 86400

# §6 fraud signal defaults.
FRAUD_MACHINES_30D   = 3
FRAUD_RESETS_30D     = 4
MACHINE_HISTORY_MAX  = 10
RESET_HISTORY_MAX    = 20

# §2 caps: per-staff daily budgets applied when the staff row omits them.
DEFAULT_CAPS         = {"keys_per_day": 10, "resets_per_day": 10, "extend_max_days": 30}
LICENSE_CREATE_MAX   = 25         # count <= 25 per license.create call
AUDIT_PAGE_MAX       = 200
AUDIT_DAY_SCAN_MAX   = 62         # how many day-partitions a paged read walks back
METRICS_RATE_MAX     = 6          # owner metrics: 6/min

# GSIs this code uses (falls back to scan when absent — see lookup_mode).
LICENSE_DISCORD_GSI  = "discord_user_id-index"
AUDIT_DAY_GSI        = "day-ts-index"
STAFF_DISCORD_GSI    = "discord_user_id-index"

# §4 owner-alert event defaults.
ALERT_EVENTS_DEFAULT = [
    "staff.create", "staff.disable", "license.revoke", "license.create",
    "license.reset_machine", "config.set", "config.rotate_admin_secret",
    "blacklist.add", "blacklist.remove", "fraud.flag", "webhook.chargeback",
]

# Free trial: one per Discord account (TRIAL#<discord_id> claim marker below)
# AND one per machine (the TRIALMACHINE#<machine_id> guard in handle_activate,
# which survives an hwid-reset). Both gates are required — either alone is
# trivially farmed with alt accounts or a fresh VM.
TRIAL_DAYS = 7
# Per-account claim throttle. The website made /api/bot/trial scriptable with a
# session cookie; with DMs closed every attempt costs two Discord API calls that
# 403, and Discord bans a bot's IP after 10k invalid requests in 10 minutes. Five
# attempts per 10 minutes is far above any honest retry pattern.
TRIAL_CLAIM_RATE_MAX = 5
TRIAL_CLAIM_RATE_WINDOW_S = 600
PAIR_CODE_TTL_S = 300
PAIR_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# ── Discord delivery ───────────────────────────────────────────────────────────
DISCORD_API            = "https://discord.com/api/v10"
DISCORD_BOT_TOKEN_SSM  = "/orion/discord_bot_token"
DISCORD_GUILD_ID_SSM   = "/orion/discord_guild_id"
DISCORD_TRIAL_ROLE_SSM = "/orion/discord_trial_role_id"
DISCORD_CUSTOMER_ROLE_SSM = "/orion/discord_customer_role_id"
BRAND_EMBED_COLOR      = 0x2563EB
# Stripe customer portal (public by design): where a paying customer manages or
# cancels the $19.99/month plan. Linked from the paid provisioning DMs.
BILLING_PORTAL_URL     = "https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00"
# Website Worker only (zaeorion.com). The Discord interactions Worker and the bot
# host carry /orion/bot_service_secret, the webhook Lambdas /orion/webhook_bot_secret.
WORKER_BOT_CONSUMER    = "/orion/worker_bot_secret"
GUILD_ROUTE_BODY_MAX   = 4096      # bytes; {discord_id, access_token} is ~120
GUILD_JOIN_RATE_MAX    = 5         # per Discord account per window
GUILD_JOIN_RATE_WINDOW_S = 600
GUILD_MEMBER_RATE_MAX  = 30        # the site checks on every config fetch
GUILD_MEMBER_RATE_WINDOW_S = 60
# 10007 Unknown Member, 10013 Unknown User. 10004 Unknown Guild is a
# misconfiguration and must surface as an outage, not as "join the server".
DISCORD_NOT_MEMBER_CODES = (10007, 10013)

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
SHARD_RETRIEVE_LIMIT  = 12

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

# [2026-09-23 rc1 RT-MED-12 / CSEC-06] v2 lease: binds the heartbeat response to the
# client's per-request nonce and the server issue time. Distinct, newline-separated,
# domain-tagged message so a v2 signature can never verify as a v1 lease (v1 messages
# start with an UPPER-CASE key, never "orion-lease-v2").
LEASE_NONCE_CHARS = frozenset(string.ascii_letters + string.digits + "-_")

def valid_lease_nonce(nonce):
    return (isinstance(nonce, str) and 16 <= len(nonce) <= 128
            and all(c in LEASE_NONCE_CHARS for c in nonce))

def lease_v2_message(license_key, machine_id, lease_expires_at, issued_at, nonce):
    return (f"orion-lease-v2\n{license_key.strip().upper()}\n{machine_id.strip()}\n"
            f"{int(lease_expires_at)}\n{int(issued_at)}\n{nonce}").encode("utf-8")

def sign_lease_v2(license_key, machine_id, lease_expires_at, issued_at, nonce):
    if not valid_lease_nonce(nonce) or "\n" in machine_id:
        raise ValueError("invalid lease v2 input")
    sig = load_lease_private_key().sign(
        lease_v2_message(license_key, machine_id, lease_expires_at, issued_at, nonce))
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

def err(msg, status=400, message=None, **extra):
    """`error` is always a machine-readable CODE. Human prose belongs in `message`
    (contract §5 revoke-fast: the launcher switches on the code, the UI shows the
    message). Extra kwargs are merged into the body (retry_at, penalty_days, ...)."""
    body = {"ok": False, "error": msg}
    if message is not None:
        body["message"] = message
    if extra:
        body.update(extra)
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=decimal_default)}

def require_admin(event):
    """Break-glass owner secret. §5: when config `owner_totp_required` is true the
    request must ALSO carry a valid X-Orion-Admin-TOTP. Routes under /api/admin/*
    go through resolve_owner() (which also honours the IP allowlist and the
    owner-role staff token); this stays for the non-/api/admin/ privileged routes
    (/api/update POST, /api/shard/store)."""
    if not admin_secret_ok(event):
        return False
    totp_ok, _ = owner_totp_ok(event)
    return totp_ok

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

def require_pair_issuer(event):
    """Only the OAuth website may create launcher pairing codes."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = str(headers.get("x-orion-pair-secret") or "")
    if not supplied:
        return False
    try:
        expected = ssm_get(PAIR_ISSUER_SECRET_SSM, decrypt=True)
    except Exception:
        return False
    return secrets.compare_digest(supplied, expected)

def gen_license_key():
    return "-".join("".join(secrets.choice(KEY_CHARS) for _ in range(4)) for _ in range(4))

def gen_pair_code():
    # This is a short-lived proof of a completed Discord OAuth session, never a
    # Discord ID or a reusable license key. 32 characters give >150 bits.
    return "PAIR-" + "".join(secrets.choice(PAIR_CODE_CHARS) for _ in range(32))

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
def rate_limit_ok(scope, subject, limit=RATELIMIT_MAX,
                  window=RATELIMIT_WINDOW_S, fail_open=True):
    """Return True if under the cap for (scope, subject) in the current window.
    Fail-open on infra error so a missing table/permission never fully blackholes
    the money path, but a working table enforces the cap. ``fail_open=None``
    preserves an unavailable result for callers that must not mint or fabricate
    a truthful quota deadline when the limiter table itself is down."""
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
        posture = "unavailable" if fail_open is None else (
            "fail-open" if fail_open else "fail-closed")
        print(f"[RATELIMIT] {posture} ({scope}): {e}")
        if fail_open is None:
            return None
        return bool(fail_open)

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

# ── §4 audit (single table, actor on every row) ───────────────────────────────
# Every legacy call site (audit_log / staff_audit) routes through audit(). The
# live `orion-audit` table is keyed on `event_id`, which DynamoDB cannot change
# in place, so the ULID is written to BOTH `event_id` (the partition key) and
# `audit_id` (the contract's name). `day` + `ts` back the day-ts-index GSI.
_ULID_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def gen_ulid(ts_ms=None):
    """Crockford base32 ULID: 48-bit millisecond timestamp + 80 random bits.
    Lexicographically sortable, so `audit_id` orders the same way `ts` does."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    val = ((int(ts_ms) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    return "".join(_ULID_B32[(val >> (5 * (25 - i))) & 31] for i in range(26))

def day_str(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else now_ts()))

SYSTEM_ACTOR = {"type": "system", "id": "lambda", "role": "system"}

def make_actor(actor_type, actor_id="", role="", ip=""):
    return {"type": actor_type, "id": str(actor_id or ""), "role": role or actor_type, "ip": ip}

class AuditUnavailable(Exception):
    """Raised only by audit(..., required=True): the audit table refused the row."""

def audit(actor, action, target=None, target_type=None, reason="",
          result="ok", details=None, ip="", flat=None, required=False, event_id=None):
    """The one audit writer. By default never raises — an audit failure must not
    fail the operation, but it IS printed so CloudWatch keeps a copy.
    [2026-09-22 RED TEAM CX-014] With required=True (used BEFORE every destructive
    mutation) a failed write raises AuditUnavailable so the mutation is refused:
    a revoke, kill or config change without a durable row is a release blocker.
    `event_id` pins the row id (idempotent completion rows, see audit_complete)."""
    actor = actor or SYSTEM_ACTOR
    ts = now_ts()
    aid = str(event_id) if event_id else gen_ulid(ts * 1000)
    item = {
        "event_id":    aid,               # live table partition key
        "audit_id":    aid,               # contract field name
        "day":         day_str(ts),
        "ts":          ts,
        "actor_type":  str(actor.get("type", "system")),
        "actor_id":    str(actor.get("id", "")),
        "role":        str(actor.get("role", "")),
        "action":      str(action),
        "event_type":  str(action),       # legacy readers (old audit_log rows)
        "target_type": str(target_type or ""),
        "target":      str(target or ""),
        "reason":      str(reason or "")[:REASON_MAX],
        "result":      str(result or "ok"),
        "ip":          str(ip or actor.get("ip", "") or ""),
    }
    if details:
        try:
            item["details"] = json.loads(json.dumps(details, default=decimal_default))
        except Exception:
            item["details"] = {"repr": str(details)[:400]}
    if flat:
        for k, v in flat.items():
            if k not in item:
                item[k] = v
    try:
        audit_table().put_item(Item=item)
    except Exception as e:
        print(f"[WARNING] AUDIT write failed action={action}: {e}")
        if required:
            raise AuditUnavailable(str(e))
    return aid


def audit_attempt_or_refuse(actor, action, target, target_type, reason, ip="", details=None):
    """Durable '<action>.attempt' row BEFORE a destructive mutation (actor, target,
    reason, ip). The '<action>' row with result=ok follows the mutation as before;
    if that later write fails, this row is the recoverable record. Returns an error
    response (to be returned by the handler) if the row cannot be written."""
    try:
        audit(actor, action, target=target, target_type=target_type, reason=reason,
              result="attempt", details=details, ip=ip, required=True)
    except AuditUnavailable as e:
        print(f"[ERROR] destructive mutation refused, audit unavailable action={action}: {e}")
        return err("audit_unavailable", 503,
                   message="The audit log is unavailable; destructive actions are refused until it recovers.")
    return None


# [2026-09-23 rc1 RT-HIGH-07 / CSEC-10] Every destructive staff/admin mutation is
# bracketed: audit_attempt() BEFORE the write (REQUIRED - raises AuditUnavailable,
# which the router turns into 503 audit_unavailable with nothing mutated), then
# audit_complete() AFTER it. The completion row's id is derived from the attempt
# id, so a retried completion write REPLACES the row instead of duplicating it.
AUDIT_UNAVAILABLE_MESSAGE = ("The audit log is unavailable; destructive actions are "
                             "refused until it recovers.")

def audit_attempt(actor, action, target=None, target_type=None, reason="", ip="", details=None):
    """Durable '<action>.attempt' row before a destructive mutation. Raises
    AuditUnavailable when the row cannot be written. Returns the attempt id."""
    return audit(actor, f"{action}.attempt", target=target, target_type=target_type,
                 reason=reason, result="attempt", details=details, ip=ip, required=True)

def audit_complete(attempt_id, actor, action, target=None, target_type=None, reason="",
                   ip="", details=None, result="ok"):
    """Idempotent completion row `<attempt_id>-<result>`. Never raises: the mutation
    has already happened and the attempt row is the durable record of it."""
    eid = f"{attempt_id}-{result}" if attempt_id else None
    for _ in range(2):
        try:
            return audit(actor, action, target=target, target_type=target_type,
                         reason=reason, result=result, details=details, ip=ip,
                         required=True, event_id=eid)
        except AuditUnavailable:
            continue
    print(f"[ERROR] completion audit failed action={action} attempt={attempt_id}; "
          f"the attempt row is the durable record")
    return eid

def audit_log(event_type, license_key=None, extra=None):
    """Legacy shim: unauthenticated/system-originated events. Keeps the old
    `license_suffix` attribute so existing readers/dashboards keep working."""
    extra = dict(extra or {})
    ip = str(extra.pop("ip", "") or "")
    flat = {"license_suffix": str(license_key)[-4:]} if license_key else None
    return audit(SYSTEM_ACTOR, event_type,
                 target=(str(license_key)[-4:] if license_key else None),
                 target_type="license" if license_key else "global",
                 details=extra or None, ip=ip, flat=flat)

# ── kill switch config ────────────────────────────────────────────────────────
# [2026-09-22 RED TEAM CX-013] A config-table read failure used to return (False, "") - the
# owner's emergency kill silently OFF for the duration of a DynamoDB blip.
# [2026-09-23 rc1 RT-CRIT-02 / CSEC-13] The CX-013 fix then reused this container's LAST
# successful read on a failure. A warm "kill off" therefore kept minting fresh signed leases
# after the owner engaged the kill, for as long as the read kept failing. There is no cache any
# more: every read is a strongly consistent GetItem, and ANY read failure means "unknown", which
# denies new/renewed authority. Callers report it as the retriable code
# `kill_state_unavailable` (503) - deliberately NOT `service_disabled`, which the launcher treats
# as a kill verdict and signs out on (LicenseClient isLicenseKillCode). A launcher that gets the
# retriable code keeps only its already-signed, bounded lease and fails closed when it expires.
KILL_STATE_UNAVAILABLE = "kill_state_unavailable"
_LAST_GLOBAL_KILL = None   # diagnostics only (last successful read); NEVER used for a decision

def get_global_kill():
    """(enabled, reason). A read failure returns (True, KILL_STATE_UNAVAILABLE)."""
    global _LAST_GLOBAL_KILL
    try:
        r = config_table().get_item(Key={"config_key": "global_kill"}, ConsistentRead=True)
        item = r.get("Item", {})
        state = (bool(item.get("enabled", False)), item.get("reason", ""))
        _LAST_GLOBAL_KILL = state
        return state
    except Exception as e:
        print(f"[ERROR] global_kill read failed (state unknown -> deny new authority): {e}")
        return True, KILL_STATE_UNAVAILABLE

def kill_denial(greason, message=None):
    """The response for an engaged OR unknown global kill on a customer route."""
    if greason == KILL_STATE_UNAVAILABLE:
        return err(KILL_STATE_UNAVAILABLE, 503,
                   message="Service status could not be confirmed just now. Retry shortly.",
                   retryable=True)
    return err("service_disabled", 503,
               message=message or greason or "The service is temporarily disabled.")

# ── /api/version ─────────────────────────────────────────────────────────────
def handle_version(event):
    """§5: also serves the version gate and the MOTD so a launcher can learn both
    before it even holds a licence."""
    qs = (event.get("queryStringParameters") or {})
    client_version = str(qs.get("client_version") or "").strip()
    if not client_version:
        try:
            client_version = str((json.loads(event.get("body") or "{}")
                                  ).get("client_version") or "").strip()
        except Exception:
            client_version = ""
    blocked, vinfo = version_gate(client_version)
    body = {
        "ok": True,
        "service": "orion-activate",
        "version": "0.4.0",
        "update_signing": "ed25519",
        "public_key_id": ED25519_KEY_ID,
        "min_client_version": vinfo.get("min_client_version", ""),
        "blocked_versions": config_get("blocked_versions", []) or [],
        "ts": now_ts()
    }
    motd = get_motd()
    if motd:
        body["motd"] = motd
    if blocked:
        body["ok"] = False
        body["error"] = "version_blocked"
        body["message"] = "This build is no longer supported — update required."
    return ok(body)

# ── customer profile block (launcher Profile page) ────────────────────────────
def license_profile(item):
    """The customer-visible slice of a license row, returned on BOTH /api/activate
    and /api/license/check as `profile`.

    Read-only projection: it introduces no new DynamoDB attribute and no schema
    change — every field is already on the row (mint paths) or derived from the
    existing reset-state helper. The launcher's Profile page renders exactly this.

    `expiry` == 0 means LIFETIME (that is how the mint paths store it:
    `expiry = 0 if plan == "lifetime" else now + days * 86400`), so the client
    treats 0 as "no expiry" rather than "expired".

    `activated_at` is the last successful bind (`last_activated`, written by the
    atomic device-cap bind in handle_activate); legacy rows may carry
    `activated_at` instead, and a never-activated key reports 0.

    `discord_username` is best-effort: no mint path stores one today, so it is
    normally "" and the client falls back to showing the ID."""
    def _int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    st = reset_state(item)
    used = _int(st.get("resets_used"), 0)
    free_total = _int(st.get("free_resets"), RESET_FREE_DEFAULT)
    activated_at = item.get("last_activated")
    if activated_at is None:
        activated_at = item.get("activated_at")
    return {
        "discord_user_id": str(item.get("discord_user_id", "") or ""),
        "discord_username": str(item.get("discord_username", "") or ""),
        "plan": str(item.get("plan", "") or ""),
        "expiry": _int(item.get("expiry"), 0),
        "activated_at": max(0, _int(activated_at, 0)),
        "hwid_resets": {
            "used": max(0, used),
            "free_total": max(0, free_total),
            "free_remaining": max(0, free_total - used),
            "paid_credits": max(0, _int(st.get("paid_credits"), 0)),
        },
    }

# ── /api/activate ─────────────────────────────────────────────────────────────
DISCORD_SIGNIN_MESSAGE = ("Connect your Discord account at zaeorion.com/connect, "
                          "then unlock with the one-time code.")

def handle_activate(event):
    # ── Parse body ───────────────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")

    license_key = (body.get("license_key") or "").strip().upper()
    machine_id  = (body.get("machine_id")  or "").strip()
    # [SERVER-SHARD blocker #5, Codex finding #1] Broker/session-only mode. When the
    # unpacked activation broker sets this, the response withholds ALL license
    # material (canonical_license_key, license_key_suffix) and the customer PII
    # profile — the broker only needs the session token/token_id. The DEFAULT
    # in-app response (flag absent/false) is byte-unchanged.
    session_only = bool(body.get("session_only"))

    if not license_key or not machine_id:
        return err("license_key and machine_id required")
    if len(machine_id) > 256:
        return err("machine_id too long")

    # ── MED-1: server-side rate-limit (per key + per source IP) ────────────────
    if not rate_limit_ok("activate", hashlib.sha256(license_key.encode()).hexdigest()) or not rate_limit_ok("activate_ip", _client_ip(event)):
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
        if greason == KILL_STATE_UNAVAILABLE:
            return kill_denial(greason)
        return err("service_disabled: " + (greason or "temporarily disabled"), 503)

    # ── §5 version gate ──────────────────────────────────────────────────────
    blocked, vinfo = version_gate(body.get("client_version"))
    if blocked:
        audit_log("activate_version_blocked", license_key,
                  {"client_version": str(body.get("client_version") or "")})
        return err("version_blocked", 403,
                   message="This build is no longer supported — update required.",
                   min_client_version=vinfo.get("min_client_version", ""))

    # ── §2 blacklist (machine) ───────────────────────────────────────────────
    if is_blacklisted(machine_id=machine_id):
        audit_log("activate_blacklisted", license_key, {"kind": "machine"})
        return err("blacklisted", 403, message="This device is blocked.")

    # A one-use code was issued only after Discord OAuth on the website. It is
    # never treated as a license itself: atomically consume it, resolve the
    # verified account's current entitlement, then run every normal check/bind.
    paired = license_key.startswith("PAIR-")
    if paired:
        try:
            paired_item = consume_pair_code(license_key)
        except EntitlementLookupUnavailable:
            return err("entitlement_unavailable", 503,
                       message="Account access could not be checked just now. Retry shortly.")
        if not paired_item:
            return err("pair_invalid", 403,
                       message="This sign-in link expired or was used. Connect Discord again.")
        license_key = paired_item["license_key"]

    # ── Fetch license record ─────────────────────────────────────────────────
    try:
        r = licenses_table().get_item(Key={"license_key": license_key})
    except Exception as e:
        print(f"[ERROR] DynamoDB GetItem failed: {e}")
        if paired:
            restore_pair_code_after_lookup_outage(body.get("license_key", ""))
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

    # §2 blacklist (the buyer's Discord account, not just this machine)
    if is_blacklisted(discord_id=item.get("discord_user_id")):
        audit_log("activate_blacklisted", license_key, {"kind": "discord"})
        return err("blacklisted", 403, message="This account is blocked.")

    if item.get("status") == "frozen":
        audit_log("activate_frozen", license_key)
        return err("frozen", 403,
                   message="This licence is frozen — the clock is paused. Open a ticket to resume.")

    if item.get("status") != "active":
        audit_log("activate_bad_status", license_key, {"status": item.get("status")})
        return err("license_invalid_status", 403)

    # ── Expiry check ──────────────────────────────────────────────────────────
    if expiry > 0 and expiry < now_ts():
        return err("license_expired", 403)

    # The key is only a credential for locating the Discord account. Paid
    # authority comes from a verified, still-live purchase on that account; a
    # manually minted or copied key must not unlock the launcher by itself.
    entitled, entitlement_reason, lookup_mode = discord_access_entitlement(item)
    if not entitled:
        if lookup_mode == "unavailable":
            if paired:
                restore_pair_code_after_lookup_outage(body.get("license_key", ""))
            return err("entitlement_unavailable", 503,
                       message="Account access could not be checked just now. Retry shortly.")
        audit_log("activate_subscription_required", license_key,
                  {"reason": entitlement_reason, "lookup_mode": lookup_mode})
        return err("subscription_required", 403,
                   message="An active Venice subscription tied to this Discord account is required. Run /purchase or open a ticket.")

    # New trials never authorize by the private database key alone. The person
    # must connect the owning Discord account and redeem its one-use code.
    # Older trial rows lack this flag and remain usable until their short expiry.
    # ── Machine binding ───────────────────────────────────────────────────────
    bound_machine = item.get("machine_id", "")
    activations   = int(item.get("activations", 0))
    max_devices   = int(item.get("max_devices", 1))

    # [2026-09-23 rc1 P-H, owner-approved] Remembered sign-in: an oauth_pair_required
    # key may re-activate WITHOUT a PAIR- exchange only on the machine it is ALREADY
    # bound to. /api/license/check already accepts key + bound machine unpaired every
    # heartbeat, so this grants no new capability. It never binds or re-binds: a new
    # bind, another machine, an unbound key or a post-HWID-reset key still needs the
    # Discord pairing. Every check above (replay, rate limit, kill, version,
    # blacklist, revoke, frozen, expiry, entitlement) has already run.
    bound_resume = False
    if item.get("oauth_pair_required") and not paired:
        if not (bound_machine and secrets.compare_digest(str(bound_machine), machine_id)):
            audit_log("activate_discord_signin_required", license_key)
            return err("discord_signin_required", 403, message=DISCORD_SIGNIN_MESSAGE)
        bound_resume = True

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
    activated_at = now_ts()
    if bound_resume:
        # Binding unchanged: no machine_id write, no activation count. The condition
        # re-checks the binding atomically, so an HWID reset racing this request
        # fails closed to the pairing requirement instead of re-binding.
        try:
            licenses_table().update_item(
                Key={"license_key": license_key},
                UpdateExpression="SET last_activated = :t, last_resume_at = :t",
                ConditionExpression="machine_id = :m",
                ExpressionAttributeValues={":m": machine_id, ":t": activated_at})
        except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
            audit_log("activate_discord_signin_required", license_key,
                      {"reason": "binding_changed_during_resume"})
            return err("discord_signin_required", 403, message=DISCORD_SIGNIN_MESSAGE)
        except Exception as e:
            print(f"[ERROR] license resume update_item failed: {e}")
            return err("internal_error", 500)
    else:
        try:
            licenses_table().update_item(
                Key={"license_key": license_key},
                UpdateExpression="ADD activations :one SET machine_id = :m, last_activated = :t",
                ConditionExpression=(
                    "machine_id = :m OR attribute_not_exists(machine_id) OR machine_id = :empty "
                    "OR activations < :max OR attribute_not_exists(activations)"
                ),
                ExpressionAttributeValues={
                    ":one": 1, ":m": machine_id, ":t": activated_at,
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

    # ── §6 fraud signals: machine_history (last 10 distinct) + flag, never kill ─
    try:
        hist = record_machine_history(item, machine_id)
        licenses_table().update_item(
            Key={"license_key": license_key},
            UpdateExpression="SET machine_history = :h, client_version = :v",
            ExpressionAttributeValues={":h": hist,
                                       ":v": str(body.get("client_version") or
                                                 item.get("client_version", ""))})
        suspect, signals = evaluate_fraud(item, hist)
        if suspect:
            flag_fraud(item, signals)
    except Exception as e:
        print(f"[FRAUD] signal pass failed key=...{license_key[-4:]}: {e}")

    audit_log("activate_success", license_key,
              {"machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id,
               "reason": "bound_machine_resume" if bound_resume
                         else ("paired" if paired else "key")})
    print(f"[INFO] AUDIT activate success: key ...{license_key[-4:]}")

    resp = {
        "ok":               True,
        "token":            token,
        "tid":              token_id,
        "expires":          expires,
    }
    if not session_only:
        # DEFAULT (in-app) response: unchanged fields, in the original order.
        resp["license_key_suffix"] = license_key[-4:]
        if paired:
            # The desktop client stores this private key for signed heartbeats;
            # never send it to a public channel or to the website.
            resp["canonical_license_key"] = license_key
        # Customer profile block (launcher Profile page). `item` is the pre-bind
        # read, so stamp the bind we just wrote rather than the previous session.
        resp["profile"] = license_profile(item)
        resp["profile"]["activated_at"] = activated_at
        motd = get_motd()
        if motd:
            resp["motd"] = motd
    # session_only (broker): return ONLY {ok, token, tid, expires} — no license
    # material, no PII. The broker writes {token, token_id, machine_id} to DPAPI.
    return ok(resp)


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

    # [rc1 RT-HIGH-07] publishing an update manifest is a fleet-wide mutation.
    attempt_id = audit_attempt(make_actor("owner", "break-glass", ROLE_OWNER, _client_ip(event)),
                               "update.publish", target=str(manifest_dict["latest_version"])[:40],
                               target_type="update", reason="publish update manifest",
                               ip=_client_ip(event),
                               details={"sha256": str(manifest_dict["sha256"])[:64],
                                        "mandatory": mandatory})
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

    audit_complete(attempt_id, make_actor("owner", "break-glass", ROLE_OWNER, _client_ip(event)),
                   "update.publish", target=str(manifest_dict["latest_version"])[:40],
                   target_type="update", reason="publish update manifest", ip=_client_ip(event))
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
    """§2 license.create. FIXED: the pre-V2 version minted rows with NO plan and NO
    expiry, which handle_activate then rejected as `license_record_invalid`. Every
    row now carries plan + expiry (0 = lifetime) and the reset-policy counters."""
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "license.create")
    if e:
        return e

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    max_devices = int(body.get("max_devices", 1))
    plan = str(body.get("plan", "") or "month").strip().lower()
    days = body.get("days")
    try:
        days = None if days in (None, "") else int(days)
    except (TypeError, ValueError):
        return err("invalid_days", 400)
    try:
        count = int(body.get("count", 1) or 1)
    except (TypeError, ValueError):
        return err("invalid_count", 400)
    if count < 1 or count > LICENSE_CREATE_MAX:
        return err("invalid_count", 400, message=f"count must be 1..{LICENSE_CREATE_MAX}.")
    note = body.get("note", body.get("notes", ""))
    reason = str(body.get("reason") or "manual provision")[:REASON_MAX]
    audit_attempt(actor, "license.create", target_type="license", reason=reason,
                  ip=_client_ip(event), details={"plan": plan, "days": days, "count": count,
                                                 "route": "provision"})

    keys = []
    for _ in range(count):
        row = mint_license(plan, days, actor,
                           discord_user_id=body.get("discord_user_id", ""),
                           email=body.get("email", ""), note=note,
                           max_devices=max_devices, source="admin_provision")
        keys.append({"license_key": row["license_key"], "expiry": row["expiry"]})
        audit(actor, "license.create", target=row["license_key"][-4:],
              target_type="license", reason=reason, ip=_client_ip(event),
              details={"plan": plan, "days": days, "max_devices": max_devices,
                       "route": "provision"})
        print(f"[INFO] AUDIT provision: key ...{row['license_key'][-4:]} plan={plan}")
    if count > 5:
        owner_alert("license.create", actor, "Licenses minted (provision)",
                    {"count": count, "plan": plan, "reason": reason})

    return ok({"ok": True, "license_key": keys[0]["license_key"],
               "expiry": keys[0]["expiry"], "plan": plan,
               "max_devices": max_devices, "keys": keys, "count": len(keys)}, 201)

# ── /api/admin/kill ───────────────────────────────────────────────────────────
def handle_kill(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    target_type = body.get("target_type", "")
    target_id   = body.get("target_id", "")
    reason      = body.get("reason", "killed")

    ip = _client_ip(event)
    if target_type == "license_key":
        if not target_id:
            return err("target_id required")
        # [rc1 RT-HIGH-07] REQUIRED attempt row first; an audit outage -> 503, no revoke.
        attempt_id = audit_attempt(actor, "license.revoke", target=target_id[-4:],
                                   target_type="license", reason=reason, ip=ip,
                                   details={"route": "kill"})
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
        audit_complete(attempt_id, actor, "license.revoke", target=target_id[-4:],
                       target_type="license", reason=reason, ip=ip, details={"route": "kill"})
        owner_alert("license.revoke", actor, "License killed",
                    {"key": "..." + target_id[-4:], "reason": reason})
        print(f"[WARNING] AUDIT kill license: key ...{target_id[-4:]}, reason={reason}")
        return ok({"ok": True, "killed": target_id, "reason": reason})

    elif target_type == "global":
        attempt_id = audit_attempt(actor, "config.set", target="global_kill",
                                   target_type="config", reason=reason, ip=ip,
                                   details={"enabled": True, "route": "kill"})
        config_table().put_item(Item={
            "config_key": "global_kill",
            "enabled":    True,
            "reason":     reason,
            "set_at":     now_ts(),
        })
        audit_complete(attempt_id, actor, "config.set", target="global_kill",
                       target_type="config", reason=reason, ip=ip,
                       details={"enabled": True, "route": "kill"})
        owner_alert("config.set", actor, "GLOBAL KILL ENGAGED", {"reason": reason})
        print(f"[WARNING] AUDIT global kill activated: reason={reason}")
        return ok({"ok": True, "global_kill": True, "reason": reason})

    else:
        return err("target_type must be 'license_key' or 'global'")

# ── /api/admin/unkill ─────────────────────────────────────────────────────────
def handle_unkill(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")

    target_type = body.get("target_type", "")
    target_id   = body.get("target_id", "")
    ureason     = str(body.get("reason", "unkill") or "unkill")[:REASON_MAX]
    ip          = _client_ip(event)

    if target_type == "license_key":
        if not target_id:
            return err("target_id required")
        attempt_id = audit_attempt(actor, "license.unrevoke", target=target_id[-4:],
                                   target_type="license", reason=ureason, ip=ip,
                                   details={"route": "unkill"})
        licenses_table().update_item(
            Key={"license_key": target_id},
            UpdateExpression="SET #s = :s, revoked = :r REMOVE kill_reason",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "active", ":r": False}
        )
        audit_complete(attempt_id, actor, "license.unrevoke", target=target_id[-4:],
                       target_type="license", reason=ureason, ip=ip,
                       details={"route": "unkill"})
        return ok({"ok": True, "unkilled": target_id})

    elif target_type == "global":
        # [rc1 RT-CRIT-01] RELEASING the kill re-enables every customer: owner step-up
        # (fresh single-use TOTP; break-glass-only while TOTP is not enrolled).
        attempt_id, denied = owner_step_up(event, actor, "config.set", body=body,
                                           target="global_kill", target_type="config",
                                           reason=ureason,
                                           details={"enabled": False, "route": "unkill"})
        if denied:
            return denied
        config_table().put_item(Item={
            "config_key": "global_kill",
            "enabled":    False,
            "reason":     "",
            "set_at":     now_ts(),
        })
        audit_complete(attempt_id, actor, "config.set", target="global_kill",
                       target_type="config", reason=ureason, ip=ip,
                       details={"enabled": False, "route": "unkill"})
        owner_alert("config.set", actor, "Global kill released", {})
        return ok({"ok": True, "global_kill": False})

    else:
        return err("target_type must be 'license_key' or 'global'")

# ── /api/admin/status ─────────────────────────────────────────────────────────
def handle_admin_status(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
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

def staff_audit(action: str, actor: str, details: dict, role: str = "", ip: str = ""):
    """§4: routes through the unified audit() AND keeps writing the legacy
    orion-staff-audit row, because GET /api/admin/staff/audit (and the shipped
    OrionOwner build) still read that table."""
    aid = audit(make_actor("staff", actor, role or "staff", ip), action,
                target=actor, target_type="staff", details=details)
    try:
        staff_audit_table().put_item(Item={
            "audit_id":  aid,
            "action":    action,
            "actor":     actor,
            "details":   json.dumps(details, default=decimal_default),
            "ts":        now_ts(),
        })
    except Exception as e:
        print(f"[WARN] staff_audit write failed: {e}")
    return aid

def check_nonce(nonce: str, window: int = ENROLL_NONCE_TTL) -> bool:
    """Returns True if nonce is fresh (not replayed). Writes it to DynamoDB."""
    if not nonce:
        return False
    try:
        # `ttl` is the attribute docs/SERVER_HANDOFF.md names as the table's TTL key;
        # `expires` is kept for existing readers.
        nonces_table().put_item(
            Item={"nonce": nonce, "ts": now_ts(), "expires": now_ts() + window,
                  "ttl": now_ts() + window},
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

    # Look the staff row up first: §2 moves enrolment from ONE global key to a
    # per-staff, one-time, salted key minted by staff.create.
    try:
        existing = staff_table().get_item(Key={"staff_id": staff_id}).get("Item")
    except Exception as e:
        print(f"[ERROR] staff enroll DDB check: {e}")
        return err("internal_error", 500)

    if existing and existing.get("enroll_key_hash") and not existing.get("enrolled_at"):
        # ── V2 path: bind a pending row created by POST /api/admin/staff create ──
        if existing.get("disabled", False):
            return err("staff_disabled", 403)
        salt = str(existing.get("enroll_salt", "") or "")
        if not secrets.compare_digest(_hash_enroll_key(salt, enroll_key),
                                      str(existing["enroll_key_hash"])):
            audit(make_actor("staff", staff_id, normalize_role(existing.get("role"))),
                  "staff.enroll", target=staff_id, target_type="staff",
                  result="invalid_enroll_key")
            return err("invalid_enroll_key", 403)
        role = normalize_role(existing.get("role"))
        try:
            staff_table().update_item(
                Key={"staff_id": staff_id},
                UpdateExpression=("SET machine_id = :m, enrolled_at = :t, display_name = :n "
                                  "REMOVE enroll_key_hash, enroll_salt"),
                ExpressionAttributeValues={
                    ":m": machine_id, ":t": now_ts(),
                    ":n": display_name or existing.get("display_name", staff_id)})
        except Exception as e:
            print(f"[ERROR] staff enroll bind: {e}")
            return err("internal_error", 500)
        token, token_id, expires = issue_staff_token(staff_id, machine_id)
        staff_audit("staff.enroll", staff_id, {"machine_suffix": machine_id[-4:]}, role=role)
        print(f"[INFO] Staff enrolled (per-staff key): {staff_id} role={role}")
        return ok({"ok": True, "token": token, "tid": token_id, "expires": expires,
                   "role": role, "caps": staff_caps(existing)})

    if existing:
        return err("staff_id_taken", 409)

    # ── Legacy path: the single global enroll key in SSM. Kept so existing tokens
    # and the shipped OrionStaff build keep enrolling until the owner cuts over.
    try:
        stored_hash = ssm_get(ENROLL_KEY_SSM, decrypt=True)
    except Exception:
        return err("enrollment_disabled", 403)
    candidate_hash = hashlib.sha256(enroll_key.encode()).hexdigest()
    if not secrets.compare_digest(candidate_hash, stored_hash):
        return err("invalid_enroll_key", 403)

    # Create staff record
    try:
        staff_table().put_item(Item={
            "staff_id":     staff_id,
            "display_name": display_name or staff_id,
            "machine_id":   machine_id,
            "role":         "staff",           # normalize_role() -> support
            "disabled":     False,
            "caps":         dict(DEFAULT_CAPS),
            "usage":        {"day": day_str(), "keys": 0, "resets": 0},
            "enrolled_at":  now_ts(),
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

    if staff.get("enroll_key_hash") and not staff.get("enrolled_at"):
        # Created by staff.create but never enrolled — no token until the one-time
        # enroll key is redeemed.
        return err("enrollment_pending", 403,
                   message="Redeem your one-time enrolment key first.")
    # [rc1 RT-CRIT-01] staff_id + a CLAIMED machine_id is not a possession factor.
    # An OWNER-role login therefore also needs a fresh, single-use owner TOTP code
    # whenever owner TOTP is required (body `totp_code` or X-Orion-Admin-TOTP).
    role_n = normalize_role(staff.get("role"))
    if role_n == ROLE_OWNER and config_get_strict("owner_totp_required"):
        ip = _client_ip(event)
        login_actor = make_actor("staff", staff_id, role_n, ip)
        secret = owner_totp_secret()
        code = str(body.get("totp_code") or _headers(event).get("x-orion-admin-totp") or "").strip()
        denial = None
        counter = None
        if not secret:
            denial = "totp_not_provisioned"
        elif not code:
            denial = "totp_required"
        else:
            counter = totp_match_counter(secret, code)
            if counter is None:
                denial = "invalid_totp"
        if denial is None:
            try:
                if not totp_consume(counter, secret):
                    denial = "totp_replayed"
            except Exception as e:
                print(f"[STAFF] owner login replay store unavailable: {e}")
                return err("step_up_unavailable", 503)
        if denial:
            audit(login_actor, "staff.login_denied", target=staff_id, target_type="staff",
                  result=denial, ip=ip)
            return err(denial, 403, message="Owner sign-in needs a fresh owner TOTP code.")
    token, token_id, expires = issue_staff_token(staff_id, machine_id)
    try:
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET last_login_at = :t, machine_id = :m",
            ExpressionAttributeValues={":t": now_ts(), ":m": machine_id})
    except Exception as e:
        print(f"[WARN] last_login_at write failed {staff_id}: {e}")
    staff_audit("staff_login", staff_id, {"machine_suffix": machine_id[-4:]},
                role=normalize_role(staff.get("role")))
    print(f"[INFO] Staff login: {staff_id}")
    return ok({"ok": True, "token": token, "tid": token_id, "expires": expires,
               "role": staff.get("role", "staff"),
               "caps": staff_caps(staff), "usage": staff_usage_today(staff),
               "display_name": staff.get("display_name", "")})

# ── GET /api/staff/whoami ─────────────────────────────────────────────────────

def handle_staff_whoami(event):
    staff, err_resp = require_staff(event)
    if err_resp:
        return err_resp
    return ok({"ok": True, "staff_id": staff["staff_id"],
               "display_name": staff.get("display_name", ""),
               "role": staff.get("role", "staff"),
               "effective_role": normalize_role(staff.get("role")),
               "caps": staff_caps(staff), "usage": staff_usage_today(staff),
               "capabilities": sorted(c for c in CAPABILITIES
                                      if normalize_role(staff.get("role")) in CAPABILITIES[c]),
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
        safe = license_view(item, staff.get("role"))
        staff_audit("staff_license_lookup", staff["staff_id"], {"key_suffix": license_key[-4:]},
                    role=normalize_role(staff.get("role")))
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
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
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
    audit(actor, "license.lookup", target=q[-4:], target_type="license",
          ip=_client_ip(event), details={"route": "admin_search"})
    return ok({"ok": True, "found": True, "license": safe,
               "detail": license_view(item, actor.get("role"))})

# ── GET /api/admin/whoami ──────────────────────────────────────────────────────

def handle_admin_whoami(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    # `role` stays "admin" for the shipped OrionOwner/CLI build (they string-match
    # it); `effective_role` carries the V2 truth.
    return ok({"ok": True, "role": "admin", "service_version": "0.4.0",
               "effective_role": normalize_role(actor.get("role")),
               "actor_type": actor.get("type"), "actor_id": actor.get("id"),
               "capabilities": sorted(c for c in CAPABILITIES
                                      if normalize_role(actor.get("role")) in CAPABILITIES[c]),
               "owner_totp_required": bool(config_get("owner_totp_required", False))})

# ── POST /api/admin/tamper-report ─────────────────────────────────────────────

def handle_admin_tamper_report(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    report_type = (body.get("type") or "unknown").strip()[:64]
    details     = body.get("details") or {}
    machine_id  = (body.get("machine_id") or "").strip()
    # staff_audit() writes the unified orion-audit row AND the legacy
    # orion-staff-audit row the shipped owner tool reads. Only the machine SUFFIX
    # is recorded — never the full id, never a header value.
    staff_audit("admin_tamper_report", str(actor.get("id") or "admin"), {
        "type": report_type,
        "machine_suffix": machine_id[-4:] if len(machine_id) >= 4 else machine_id,
        "detail_keys": list(details.keys()) if isinstance(details, dict) else [],
    }, role=normalize_role(actor.get("role")), ip=_client_ip(event))
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
    """POST /api/license/check - heartbeat/lease check.

    §5 revoke-fast (BREAKING): `error` is now always one of the machine-readable
    CODES the launcher already kills on —
        service_disabled | revoked | expired | device_mismatch | invalid_key |
        inactive | subscription_required (+ frozen | blacklisted | version_blocked)
    — and the human prose moved to `message`. The launcher switches on the code;
    nothing downstream should ever string-match the prose again."""
    import time
    # NEW-5: guard against malformed body (every other handler wraps json.loads).
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json", 400)
    license_key = (body.get("license_key") or "").strip().upper()
    machine_id  = (body.get("machine_id")  or "").strip()
    client_version = str(body.get("client_version") or "").strip()
    if not license_key or not machine_id:
        return err("license_key and machine_id required", 400)
    # [rc1 RT-MED-12] optional per-request nonce (v2 lease). Absent = v1 client.
    lease_nonce = body.get("lease_nonce")
    if lease_nonce is not None and not valid_lease_nonce(lease_nonce):
        return err("invalid_lease_nonce", 400,
                   message="lease_nonce must be 16-128 characters of [A-Za-z0-9_-].")
    # MED-1: rate-limit the heartbeat too (per key + per source IP).
    if not rate_limit_ok("check", license_key) or not rate_limit_ok("check_ip", _client_ip(event)):
        return err("rate_limited", 429)
    gkill, greason = get_global_kill()
    if gkill:
        return kill_denial(greason)
    blocked, vinfo = version_gate(client_version)
    if blocked:
        return err("version_blocked", 403,
                   message="This build is no longer supported — update required.",
                   min_client_version=vinfo.get("min_client_version", ""))
    try:
        resp = licenses_table().get_item(Key={"license_key": license_key})
    except Exception as e:
        print(f"[check] DDB error: {e}")
        return err("internal_error", 500, message="Internal error.")
    item = resp.get("Item")
    if not item:
        return err("invalid_key", 403, message="That licence key is not recognised.")
    bl = is_blacklisted(machine_id=machine_id, discord_id=item.get("discord_user_id"))
    if bl:
        audit_log("check_blacklisted", license_key, {"kind": bl})
        return err("blacklisted", 403, message="This device or account is blocked.")
    if item.get("machine_id") != machine_id:
        return err("device_mismatch", 403,
                   message="This licence is bound to a different PC.")
    if item.get("revoked", False) or item.get("status") == "revoked":
        return err("revoked", 403, message="This licence has been revoked.")
    if item.get("status") == "frozen":
        return err("frozen", 403,
                   message="This licence is frozen — the clock is paused. Open a ticket to resume.")
    if item.get("status") != "active":
        return err("inactive", 403, message="This licence is not active.")
    now = int(time.time())
    # Fail closed on expiry (mirrors handle_activate's guard). expiry == 0 means
    # lifetime; a missing/unparseable expiry keeps legacy behaviour (no check).
    try:
        expiry = int(item.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if 0 < expiry < now:
        return err("expired", 403, message="This licence has expired.")
    entitled, entitlement_reason, lookup_mode = discord_access_entitlement(item, now=now)
    if not entitled:
        if lookup_mode == "unavailable":
            audit_log("check_entitlement_unavailable", license_key,
                      {"reason": entitlement_reason})
            return err("entitlement_unavailable", 503,
                       message="Account access could not be checked just now. Retry shortly.")
        audit_log("check_subscription_required", license_key,
                  {"reason": entitlement_reason, "lookup_mode": lookup_mode})
        return err("subscription_required", 403,
                   message="Your Discord account does not have an active Venice subscription. Run /purchase or open a ticket.")
    # §6: heartbeat records last_check_at + client_version on the row (metrics:
    # online_now, versions{}). Best-effort — a write failure must not fail a check.
    try:
        upd = "SET last_check_at = :t"
        vals = {":t": now}
        if client_version:
            upd += ", client_version = :v"
            vals[":v"] = client_version
        licenses_table().update_item(Key={"license_key": license_key},
                                     UpdateExpression=upd,
                                     ExpressionAttributeValues=vals)
    except Exception as e:
        print(f"[check] heartbeat stamp failed: {e}")
    lease_expires_at = now + LEASE_TTL_S
    resp_body = {
        "ok": True,
        "license_key": license_key,
        "machine_id": machine_id,
        "lease_expires_at": lease_expires_at,
        "tier": item.get("tier", "standard"),
        "plan": item.get("plan", ""),
        "expiry": expiry,
    }
    # Customer profile block — the heartbeat is what keeps the launcher's Profile
    # page fresh (days left, HWID reset allowance) without a second round trip.
    # `expiry` was normalised above (0 == lifetime); reuse it so the two agree.
    resp_body["profile"] = license_profile(item)
    resp_body["profile"]["expiry"] = expiry
    motd = get_motd()
    if motd:
        resp_body["motd"] = motd
    # ── NEW-1: sign the lease so the client (LeaseGate::verifyLeaseSignature) can
    # cryptographically trust lease_expires_at. A MITM / cracked client can no
    # longer fabricate a far-future lease without the private key. Fail-soft only
    # if the signing key is not yet provisioned (operator must set SSM before the
    # client embeds the public key and makes the signature mandatory).
    try:
        resp_body["lease_sig"] = sign_lease(license_key, machine_id, lease_expires_at)
        resp_body["public_key_id"] = LEASE_KEY_ID
        if lease_nonce is not None:
            # v2: the signature also covers the caller's nonce and the server issue
            # time, so a captured response cannot be replayed to a later request.
            resp_body["lease_nonce"] = lease_nonce
            resp_body["lease_issued_at"] = now
            resp_body["lease_sig_v2"] = sign_lease_v2(license_key, machine_id,
                                                      lease_expires_at, now, lease_nonce)
            resp_body["lease_sig_version"] = 2
    except Exception as e:
        print(f"[LEASE] signing unavailable (fail-soft): {e}")
    return ok(resp_body)

# ── bot routes (Discord gateway bot / Cloudflare Worker) ───────────────────────
# Auth: X-Orion-Bot-Secret header, checked against SSM /orion/bot_service_secret
# (require_bot). Delivery (DM + role-grant) happens CLIENT-SIDE in orion_bot.py —
# these handlers only mint/read license state and return a bare license_key.
def _find_license_by_discord(discord_id):
    """(item|None, lookup_mode). Uses the discord_user_id GSI when it exists and
    falls back to a full scan when it does not — the caller surfaces lookup_mode so
    the owner can see the index is still missing."""
    rows, mode = find_licenses_by_discord(discord_id)
    if mode == "unavailable":
        return None, mode
    return _newest_active(rows), mode

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
        gkill, greason = get_global_kill()
        return ok({"ok": True, "enabled": gkill, "reason": greason})
    # §2 BREAKING: the bot gateway lost its WRITE actions. The global kill is the
    # single most destructive control in the product and it is now owner-only —
    # a leaked bot secret can no longer take every customer offline.
    if action in ("on", "enable", "engage", "off", "disable", "disengage"):
        audit(make_actor("webhook", require_bot(event) or "bot", "webhook",
                         _client_ip(event)),
              "config.killswitch_write_denied", target="global_kill",
              target_type="config", result="write_removed",
              details={"action": action, "by": str(body.get("by", ""))})
        return err("write_removed", 403,
                   message="The kill switch is owner-only now — use the owner panel "
                           "(POST /api/admin/config or /api/admin/kill).")
    return err("action must be status", 400,
               message="This endpoint is read-only; the only action is 'status'.")

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
    req.add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")
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
    msg.add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")
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
        # These identifiers are stored as SecureString parameters in production.
        # Without decryption SSM returns ciphertext, producing a syntactically
        # valid but nonexistent Discord route (HTTP 404) and silently skipping
        # the paid role grant.
        role_id = ssm_get(role_ssm, decrypt=True).strip()
        guild_id = ssm_get(DISCORD_GUILD_ID_SSM, decrypt=True).strip()
        if not role_id or not guild_id:
            print(f"[WARN] role grant skipped: {role_ssm} or guild id unset")
            return False
        token = ssm_get(DISCORD_BOT_TOKEN_SSM, decrypt=True)
        req = urllib.request.Request(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
            data=b"", method="PUT")
        req.add_header("Authorization", "Bot " + token)
        req.add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")
        req.add_header("Content-Length", "0")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[WARN] role grant failed ({role_ssm}): {e}")
        return False


def _discord_remove_role(discord_user_id, role_ssm):
    """Best-effort paid-role removal after the last paid entitlement ends."""
    try:
        role_id = ssm_get(role_ssm, decrypt=True).strip()
        guild_id = ssm_get(DISCORD_GUILD_ID_SSM, decrypt=True).strip()
        token = ssm_get(DISCORD_BOT_TOKEN_SSM, decrypt=True)
        if not role_id or not guild_id:
            return False
        request = urllib.request.Request(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
            method="DELETE")
        request.add_header("Authorization", "Bot " + token)
        request.add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")
        with urllib.request.urlopen(request, timeout=10):
            return True
    except Exception as exc:
        print(f"[WARN] paid role removal failed: {type(exc).__name__}")
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
    actor, aerr = require_self_or_staff(event, body, discord_id)
    if aerr:
        return aerr
    item, lookup_mode = _find_license_by_discord(discord_id)
    if lookup_mode == "unavailable":
        return err("entitlement_unavailable", 503)
    if not item:
        return ok({"ok": True, "has_license": False, "lookup_mode": lookup_mode})
    now = now_ts()
    expiry = int(item.get("expiry", 0) or 0)
    lifetime = expiry == 0
    st = reset_state(item)
    return ok({
        "ok": True,
        "has_license": True,
        "plan": str(item.get("plan", "") or ""),
        "lifetime": lifetime,
        "expiry": expiry,
        "active": lifetime or expiry > now,
        "seconds_left": 0 if lifetime else max(0, expiry - now),
        "bound": bool(item.get("machine_id", "")),
        "lookup_mode": lookup_mode,
        "resets": {"free_remaining": max(0, st["free_resets"] - st["resets_used"]),
                   "paid_credits": st["paid_credits"], "locked": st["locked"]},
    })

def handle_bot_hwid_reset(event):
    """§3 self-service HWID reset — the owner's rule, as a state machine:

        cooldown -> locked -> free (used < free_resets)
                 -> paid  (paid_credits > 0 — STAFF-GRANTED goodwill only)
                 -> deduct (mode="deduct", costs penalty_days off the expiry)
                 -> payment_required(=confirm) / trial_no_deduct
                    / insufficient_time / paid_only(no expiry)

    Owner rule 2026-09-15: three free resets per key, then one day off the
    subscription per reset. Customers can no longer BUY a reset, so no reply here
    carries a store link; `paid_credits` is still honoured because staff grant
    them through /api/bot/hwid-credit as goodwill.

    WIRE NOTE: the "you must confirm" code is still `payment_required` — both
    Discord front-ends and the live worker switch on that string — but the reply
    now also carries `confirm_required: true` so a future rename has a hook."""
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    discord_id = str(body.get("discord_id") or body.get("discord_user_id") or "").strip()
    if not discord_id:
        return err("discord_id is required", 400)
    mode_req = str(body.get("mode", "") or "").strip().lower()
    reason = str(body.get("reason") or "self-service hwid reset")[:REASON_MAX]
    # Self-service is open; resetting SOMEONE ELSE's key needs a staff row.
    actor, aerr = require_self_or_staff(event, body, discord_id)
    if aerr:
        return aerr

    item, lookup_mode = _find_license_by_discord(discord_id)
    if lookup_mode == "unavailable":
        return err("entitlement_unavailable", 503)
    if not item:
        return ok({"ok": False, "error": "no_license", "lookup_mode": lookup_mode,
                   "message": "No active license is linked to your account."})
    key = item["license_key"]
    now = now_ts()
    st = reset_state(item)

    if not st["self_service"]:
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="self_service_disabled")
        return ok({"ok": False, "error": "self_service_disabled", "lookup_mode": lookup_mode,
                   "message": "Self-service resets are turned off right now — open a ticket."})

    # 1. cooldown
    if st["last_reset_at"] and (now - st["last_reset_at"]) < st["cooldown_s"]:
        retry_at = st["last_reset_at"] + st["cooldown_s"]
        rem = retry_at - now
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="cooldown")
        return ok({"ok": False, "error": "cooldown", "retry_at": retry_at,
                   "lookup_mode": lookup_mode,
                   "message": f"You recently reset. Try again in {rem // 3600}h "
                              f"{(rem % 3600) // 60}m, or open a ticket."})

    # 2. locked
    if st["locked"]:
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="locked")
        return ok({"ok": False, "error": "locked", "lookup_mode": lookup_mode,
                   "message": "Resets are locked on this license — open a support ticket."})

    def _done(new_item, mode, message):
        """The exact success shape the Discord front-ends render."""
        st2 = reset_state(new_item)
        try:
            new_expiry = int(new_item.get("expiry", 0) or 0)
        except (TypeError, ValueError):
            new_expiry = 0
        return ok({
            "ok": True, "mode": mode, "message": message,
            "lookup_mode": lookup_mode,
            "hwid_free_resets": st2["free_resets"],
            "hwid_resets_used": st2["resets_used"],
            "hwid_paid_credits": st2["paid_credits"],
            "free_remaining": max(0, st2["free_resets"] - st2["resets_used"]),
            "penalty_days": st2["penalty_days"],
            "deduct_days": st2["penalty_days"],
            "expiry": new_expiry,
            "lifetime": new_expiry == 0,
        })

    # 3. free allowance
    if st["resets_used"] < st["free_resets"]:
        new_item = apply_hwid_reset(item, actor, "free", reason, consume="free")
        left = max(0, st["free_resets"] - (st["resets_used"] + 1))
        return _done(new_item, "free",
                     f"Reset done — {left} free reset{'s' if left != 1 else ''} left. "
                     f"Activate on your new PC with the same key.")

    # 4. credit granted by staff (goodwill). No customer-facing way to buy one.
    if st["paid_credits"] > 0:
        new_item = apply_hwid_reset(item, actor, "paid", reason, consume="paid")
        left = max(0, st["paid_credits"] - 1)
        return _done(new_item, "paid",
                     f"Reset done — a staff reset credit was used, no days deducted. "
                     f"({left} credit{'s' if left != 1 else ''} left.)")

    # 5. deduct time — explicit confirmation required
    penalty_days = st["penalty_days"]
    day_word = "day" if penalty_days == 1 else "days"
    try:
        expiry = int(item.get("expiry", 0) or 0)
    except (TypeError, ValueError):
        expiry = 0

    # 5a. A trial has no subscription to take a day from. apply_hwid_reset floors
    #     the expiry at `now`, so without this guard a trial would quietly get an
    #     unlimited supply of 4th resets. Refuse, and say why.
    if st["plan"] == "trial":
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="trial_no_deduct")
        return ok({"ok": False, "error": "trial_no_deduct",
                   "penalty_days": penalty_days, "deduct_days": penalty_days,
                   "lookup_mode": lookup_mode,
                   "message": (f"Your free trial includes {st['free_resets']} PC resets and "
                               f"you've used them all. A trial has no subscription to take a "
                               f"{day_word} from — subscribe to keep resetting, or open a ticket.")})

    if mode_req != "deduct":
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="payment_required")
        return ok({"ok": False, "error": "payment_required", "confirm_required": True,
                   "penalty_days": penalty_days, "deduct_days": penalty_days,
                   "lookup_mode": lookup_mode,
                   "message": (f"You've used all {st['free_resets']} free PC resets. The next "
                               f"reset takes {penalty_days} {day_word} off your subscription — "
                               f"confirm below and nothing happens until you do.")})
    if expiry == 0:
        # A key with no clock (staff lifetime comp) has nothing to take days from.
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="paid_only")
        return ok({"ok": False, "error": "paid_only",
                   "lookup_mode": lookup_mode,
                   "message": "This key has no expiry to take days from — open a ticket "
                              "and staff will reset it for you."})
    if (expiry - now) < penalty_days * 86400:
        audit(actor, "license.reset_machine", target=key[-4:], target_type="license",
              reason=reason, result="insufficient_time")
        return ok({"ok": False, "error": "insufficient_time",
                   "penalty_days": penalty_days, "deduct_days": penalty_days,
                   "lookup_mode": lookup_mode,
                   "message": f"You have less than {penalty_days} {day_word} left on your "
                              f"subscription — renew it, or open a ticket."})
    new_item = apply_hwid_reset(item, actor, "deduct", reason,
                                expiry_delta=-penalty_days * 86400)
    new_expiry = int(new_item.get("expiry", expiry - penalty_days * 86400) or 0)
    return _done(new_item, "deduct",
                 f"Reset done — {penalty_days} {day_word} "
                 f"{'was' if penalty_days == 1 else 'were'} deducted from your subscription "
                 f"(expires {time.strftime('%Y-%m-%d', time.gmtime(new_expiry))}).")

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
    # `code` on every outcome lets the website map results to its own copy; the
    # Discord front-ends keep reading `ok` + `message` unchanged.
    if not rate_limit_ok("trial_claim", discord_id, limit=TRIAL_CLAIM_RATE_MAX,
                         window=TRIAL_CLAIM_RATE_WINDOW_S):
        audit_log("bot_trial_rate_limited", extra={"discord_id": discord_id})
        return err("rate_limited", 429, code="rate_limited",
                   message="Too many trial attempts. Wait a few minutes and try again.")
    # §2: a blacklisted account cannot farm trials.
    if is_blacklisted(discord_id=discord_id):
        audit_log("bot_trial_blacklisted", extra={"discord_id": discord_id})
        return err("blacklisted", 403, code="blacklisted", message="This account is blocked.")
    now = now_ts()
    claim_key = "TRIAL#" + discord_id
    try:
        licenses_table().put_item(
            Item={"license_key": claim_key, "status": "trial_claim", "revoked": True, "created_at": now},
            ConditionExpression="attribute_not_exists(license_key)")
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return ok({"ok": False, "code": "already_claimed",
                   "message": "You've already claimed your free trial."})
    key = gen_license_key()
    expiry = now + TRIAL_DAYS * 86400
    licenses_table().put_item(Item={
        "license_key": key, "status": "active", "revoked": False, "plan": "trial",
        "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
        "created_at": now, "discord_user_id": discord_id, "source": "trial",
        "entitlement_type": "discord_trial", "subscription_status": "trial",
        "oauth_pair_required": True,
    })
    # Deliver from here, not through the Worker. If the DM bounces (closed DMs),
    # roll the claim back so the user can retry after opening them — otherwise a
    # single bounce permanently burns their one trial and creates a support ticket.
    try:
        _discord_dm(discord_id, {
            "title": f"🎁 Your free {TRIAL_DAYS}-day Venice trial",
            "description": (
                f"Your trial is active for **{TRIAL_DAYS} days** and linked to this Discord account. "
                "On the PC you play on, connect Discord to Venice and use the one-time code. "
                "The trial locks to that PC.\n\n"
                "When it runs out, run `/purchase` to keep going."),
            "fields": [
                {"name": "Connect Venice", "value": "https://zaeorion.com/connect", "inline": False},
                {"name": "When it ends", "value": "The trial ends automatically, with no card and no charge. Subscribe any time at https://zaeorion.com", "inline": False},
                {"name": "Support", "value": "Open a ticket if you hit any trouble.", "inline": False},
            ],
            "color": BRAND_EMBED_COLOR,
            "footer": {"text": "Venice — your Discord ID is not a password"},
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
        return ok({"ok": False, "code": "dm_failed", "message":
                   "I couldn't DM you. Turn on **Privacy Settings → Direct Messages** "
                   "for this server, then run `/claim_trial` again."})
    _discord_add_role(discord_id, DISCORD_TRIAL_ROLE_SSM)
    audit_log("bot_trial_issued", key, {"discord_id": discord_id})
    print(f"[INFO] AUDIT bot_trial_issued key ...{key[-4:]} discord={discord_id}")
    # No license key is delivered to the customer or returned to the Worker.
    return ok({"ok": True, "code": "issued", "plan": "trial", "expiry": expiry,
               "message": f"✅ Your {TRIAL_DAYS}-day trial is active. Check your DMs to connect Discord."})

# ── website Worker: Discord server membership (auto-join + purchase gate) ─────
# Every key (trial or paid) is delivered by bot DM, and Discord only lets the bot
# DM users who share a server with it. The website therefore (a) adds the user to
# the Venice server at sign-in with their `guilds.join` OAuth token and (b) refuses
# checkout / trial unless a LIVE membership check says they are in the server.
def _valid_discord_id(value):
    value = str(value or "")
    return 16 <= len(value) <= 22 and value.isascii() and value.isdigit()


_OAUTH_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-._~+/=")

def _valid_oauth_token(value):
    return (isinstance(value, str) and 16 <= len(value) <= 512
            and all(ch in _OAUTH_TOKEN_CHARS for ch in value))


def _small_json_body(event, limit=GUILD_ROUTE_BODY_MAX):
    """(dict, None) or (None, error_response). The size cap applies before parsing."""
    raw = event.get("body") or ""
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > limit:
        return None, err("payload_too_large", 413)
    try:
        body = json.loads(raw or "{}")
    except Exception:
        return None, err("invalid_json", 400)
    if not isinstance(body, dict):
        return None, err("invalid_json", 400)
    return body, None


def _worker_consumer(event, action):
    """Only the website Worker's own secret may call the guild routes. Returns
    (consumer, None) or (None, 403). A valid-but-wrong consumer is audited."""
    consumer = require_bot(event)
    if consumer == WORKER_BOT_CONSUMER:
        return consumer, None
    if consumer:
        audit(make_actor("webhook", consumer, "webhook", _client_ip(event)), action,
              target_type="discord", result="forbidden_consumer")
    return None, err("forbidden", 403)


def _discord_guild_member_call(method, discord_user_id, payload=None):
    """PUT/GET /guilds/{guild}/members/{user} with the bot token.

    Returns (http_status, discord_error_code); status 0 = misconfiguration or a
    transport failure. Never raises. Never logs the request body (a guild join
    carries the customer's OAuth access token) nor Discord's response body."""
    try:
        guild_id = ssm_get(DISCORD_GUILD_ID_SSM, decrypt=True).strip()
        token = ssm_get(DISCORD_BOT_TOKEN_SSM, decrypt=True).strip()
    except Exception as exc:
        print(f"[WARN] guild member {method}: config unavailable ({type(exc).__name__})")
        return 0, None
    if not guild_id or not token:
        print(f"[WARN] guild member {method}: guild id or bot token unset")
        return 0, None
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}",
        data=data, method=method)
    req.add_header("Authorization", "Bot " + token)
    req.add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return int(getattr(resp, "status", 0) or resp.getcode() or 0), None
    except urllib.error.HTTPError as exc:
        code = None
        try:
            parsed = json.loads(exc.read(2048) or b"{}")
            if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
                code = parsed["code"]
        except Exception:
            pass
        return int(exc.code), code
    except Exception as exc:
        print(f"[WARN] guild member {method}: transport {type(exc).__name__}")
        return 0, None


def handle_bot_guild_join(event):
    """POST /api/bot/guild-join {discord_id, access_token} — website Worker only.

    Adds a freshly signed-in customer to the Venice server so the bot can DM them
    their key. The access token is used for this one Discord call and is never
    stored, audited or printed. Discord answers 201 (added) or 204 (already a
    member); anything else is `failed` and the Worker keeps the login going."""
    consumer, cerr = _worker_consumer(event, "discord.guild_join")
    if cerr:
        return cerr
    body, berr = _small_json_body(event)
    if berr:
        return berr
    discord_id = str(body.get("discord_id") or "").strip()
    if not _valid_discord_id(discord_id):
        return err("invalid_discord_id", 400)
    access_token = body.get("access_token")
    if not _valid_oauth_token(access_token):
        return err("invalid_access_token", 400)
    actor = make_actor("webhook", consumer, "webhook", _client_ip(event))
    if not rate_limit_ok("guild_join", discord_id, limit=GUILD_JOIN_RATE_MAX,
                         window=GUILD_JOIN_RATE_WINDOW_S):
        audit(actor, "discord.guild_join", target=discord_id, target_type="discord",
              result="rate_limited")
        return err("rate_limited", 429, result="failed")
    if is_blacklisted(discord_id=discord_id):
        audit(actor, "discord.guild_join", target=discord_id, target_type="discord",
              result="blacklisted")
        return err("not_allowed", 403, result="failed")
    status, code = _discord_guild_member_call("PUT", discord_id,
                                              {"access_token": access_token})
    result = {201: "joined", 204: "already_member"}.get(status, "failed")
    audit(actor, "discord.guild_join", target=discord_id, target_type="discord",
          result=result, details={"discord_status": status, "discord_code": code})
    print(f"[INFO] guild_join discord=...{discord_id[-4:]} result={result} "
          f"status={status} code={code}")
    if result == "failed":
        return err("guild_join_failed", 502, result="failed",
                   discord_status=status, discord_code=code)
    return ok({"ok": True, "result": result})


def handle_bot_guild_member(event):
    """POST /api/bot/guild-member {discord_id, purpose?} — website Worker only.

    The LIVE server-membership check behind the purchase / trial gate. 200 ->
    member; 404 Unknown Member/User -> not a member; anything else is an error the
    Worker must treat as "unknown" (it fails closed on purchase). A single-member
    fetch does not need the privileged Server Members intent. Denials on a gated
    purpose (checkout / trial) are audited; config polls only print."""
    consumer, cerr = _worker_consumer(event, "discord.membership_check")
    if cerr:
        return cerr
    body, berr = _small_json_body(event)
    if berr:
        return berr
    discord_id = str(body.get("discord_id") or "").strip()
    if not _valid_discord_id(discord_id):
        return err("invalid_discord_id", 400)
    purpose = str(body.get("purpose") or "config").strip().lower()
    if purpose not in ("config", "checkout", "trial"):
        purpose = "config"
    actor = make_actor("webhook", consumer, "webhook", _client_ip(event))
    if not rate_limit_ok("guild_member", discord_id, limit=GUILD_MEMBER_RATE_MAX,
                         window=GUILD_MEMBER_RATE_WINDOW_S):
        return err("rate_limited", 429)
    status, code = _discord_guild_member_call("GET", discord_id)
    if status == 200:
        return ok({"ok": True, "member": True})
    if status == 404 and (code is None or code in DISCORD_NOT_MEMBER_CODES):
        if purpose != "config":
            audit(actor, "discord.membership_check", target=discord_id,
                  target_type="discord", result="not_member",
                  details={"purpose": purpose})
        print(f"[INFO] guild_member discord=...{discord_id[-4:]} member=False "
              f"purpose={purpose}")
        return ok({"ok": True, "member": False})
    print(f"[WARN] guild_member discord=...{discord_id[-4:]} unavailable "
          f"status={status} code={code} purpose={purpose}")
    if purpose != "config":
        audit(actor, "discord.membership_check", target=discord_id,
              target_type="discord", result="unavailable",
              details={"purpose": purpose, "discord_status": status, "discord_code": code})
    return err("membership_unavailable", 502, discord_status=status, discord_code=code)


def active_license_for_discord(discord_id):
    """Resolve an account's live entitlement without trusting a submitted ID."""
    rows, mode = find_licenses_by_discord(discord_id)
    if mode == "unavailable":
        raise EntitlementLookupUnavailable()
    eligible = []
    for row in rows:
        if (row.get("status") != "active"
                or str(row.get("discord_user_id") or "") != discord_id
                or str(row.get("license_key") or "").startswith(("PAIR#", "TRIAL#", "TRIALMACHINE#"))):
            continue
        entitled, _, entitlement_mode = discord_access_entitlement(row)
        if entitlement_mode == "unavailable":
            raise EntitlementLookupUnavailable()
        if entitled:
            eligible.append(row)
    eligible.sort(key=lambda row: (
        str(row.get("plan") or "").lower() != "trial",
        int(row.get("created_at") or 0)), reverse=True)
    return eligible[0] if eligible else None


class EntitlementLookupUnavailable(Exception):
    """Purchase evidence could not be read; it is not an entitlement denial."""


def _pair_request_discord_id(event):
    if not require_pair_issuer(event):
        return None, err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return None, err("invalid_json", 400)
    discord_id = str(body.get("discord_id") or "").strip()
    if not (16 <= len(discord_id) <= 22 and discord_id.isascii() and discord_id.isdigit()):
        return None, err("invalid_discord_id", 400)
    return discord_id, None


def handle_bot_pair_status(event):
    """Authenticated, non-minting provisioning check with a separate poll budget."""
    discord_id, error = _pair_request_discord_id(event)
    if error:
        return error
    limited = rate_limit_ok("pair_status", discord_id, limit=120, window=600,
                            fail_open=None)
    if limited is None:
        return err("rate_limit_unavailable", 503)
    if not limited:
        return err("rate_limited", 429, retry_after_s=600 - now_ts() % 600)
    if is_blacklisted(discord_id=discord_id):
        return err("blacklisted", 403)
    try:
        item = active_license_for_discord(discord_id)
    except EntitlementLookupUnavailable:
        return err("entitlement_unavailable", 503)
    return ok({"ok": True, "ready": bool(item)})

def handle_bot_pair_issue(event):
    """A trusted website Worker relays its verified Discord OAuth identity here."""
    discord_id, error = _pair_request_discord_id(event)
    if error:
        return error
    limited = rate_limit_ok("pair_issue", discord_id, limit=5, window=600,
                            fail_open=None)
    if limited is None:
        return err("rate_limit_unavailable", 503)
    if not limited:
        return err("rate_limited", 429, retry_after_s=600 - now_ts() % 600)
    if is_blacklisted(discord_id=discord_id):
        return err("blacklisted", 403)
    try:
        item = active_license_for_discord(discord_id)
    except EntitlementLookupUnavailable:
        return err("entitlement_unavailable", 503)
    if not item:
        return err("subscription_required", 403,
                   message="Claim a trial or subscribe on this Discord account first.")
    now = now_ts()
    for _ in range(3):
        code = gen_pair_code()
        marker = "PAIR#" + hashlib.sha256(code.encode("ascii")).hexdigest()
        try:
            licenses_table().put_item(
                Item={"license_key": marker, "status": "pair_pending",
                      "pair_discord_id": discord_id, "created_at": now,
                      "expires": now + PAIR_CODE_TTL_S, "ttl": now + PAIR_CODE_TTL_S},
                ConditionExpression="attribute_not_exists(license_key)")
            audit_log("pair_code_issued", item["license_key"], {"discord_id": discord_id})
            return ok({"ok": True, "pair_code": code, "expires": now + PAIR_CODE_TTL_S})
        except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
            continue
        except Exception as e:
            print(f"[ERROR] pair issue failed: {type(e).__name__}")
            return err("internal_error", 500)
    return err("internal_error", 500)

def consume_pair_code(code):
    """Consume once, then look up the live entitlement for the OAuth account."""
    if not (code.startswith("PAIR-") and len(code) == 37
            and all(char in PAIR_CODE_CHARS for char in code[5:])):
        return None
    marker = "PAIR#" + hashlib.sha256(code.encode("ascii")).hexdigest()
    now = now_ts()
    # Resolve entitlement BEFORE the one-use transition. An unavailable purchase
    # lookup must not burn the customer's valid code, and the normal activation
    # path checks entitlement again after this transition.
    try:
        pending = licenses_table().get_item(Key={"license_key": marker}).get("Item", {})
    except Exception as exc:
        print(f"[ERROR] pair lookup failed: {type(exc).__name__}")
        raise EntitlementLookupUnavailable() from exc
    if pending.get("status") != "pair_pending" or int(pending.get("expires") or 0) <= now:
        return None
    discord_id = str(pending.get("pair_discord_id") or "")
    if not discord_id:
        return None
    item = active_license_for_discord(discord_id)
    if not item:
        return None
    try:
        response = licenses_table().update_item(
            Key={"license_key": marker},
            UpdateExpression="SET #s = :consumed, consumed_at = :now",
            ConditionExpression="#s = :pending AND expires > :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":pending": "pair_pending",
                                       ":consumed": "pair_consumed", ":now": now},
            ReturnValues="ALL_NEW")
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return None
    except Exception as e:
        print(f"[ERROR] pair redeem failed: {type(e).__name__}")
        return None
    audit_log("pair_code_consumed", item["license_key"], {"discord_id": discord_id})
    return item


def restore_pair_code_after_lookup_outage(code):
    """A retriable backend failure after consume must not spend the sign-in code."""
    code = str(code or "").strip().upper()
    if not (code.startswith("PAIR-") and len(code) == 37):
        return False
    marker = "PAIR#" + hashlib.sha256(code.encode("ascii")).hexdigest()
    try:
        licenses_table().update_item(
            Key={"license_key": marker},
            UpdateExpression="SET #s = :pending REMOVE consumed_at",
            ConditionExpression="#s = :consumed",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":pending": "pair_pending",
                                       ":consumed": "pair_consumed"})
        return True
    except Exception as exc:
        print(f"[WARN] pair code restore failed: {type(exc).__name__}")
        return False

def handle_bot_deliver(event):
    """§4 BREAKING: /deliver is a STAFF mint path, so the Discord front-end must
    forward `actor_discord_id`. It is resolved against orion-staff and must carry
    role admin or owner — this replaces the old client-side ADMINISTRATOR-bit
    check, which the Lambda could neither see nor audit."""
    if not require_bot(event):
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    actor, aerr = resolve_discord_actor(event, body)
    if aerr:
        return aerr
    cap_err = require_capability(actor, "license.create")
    if cap_err:
        return cap_err
    discord_id = str(body.get("discord_id", "")).strip()
    plan = str(body.get("plan", "")).strip().lower() or "month"
    days = int(body.get("days") or TIER_DAYS.get(plan, 30))
    order_id = str(body.get("order_id", "")).strip()
    reason = str(body.get("reason") or "discord /deliver")[:REASON_MAX]
    note = str(body.get("note", "") or "")[:500]
    try:
        count = int(body.get("count", 1) or 1)
    except (TypeError, ValueError):
        return err("invalid_count", 400)
    if count < 1 or count > LICENSE_CREATE_MAX:
        return err("invalid_count", 400,
                   message=f"count must be 1..{LICENSE_CREATE_MAX}.")
    # Bulk mints are metered against the actor's own daily cap.
    actor_staff = find_staff_by_discord(str(body.get("actor_discord_id") or ""))
    cap_err = check_cap(actor, actor_staff, "keys", "keys_per_day", want=count)
    if cap_err:
        return cap_err

    # [rc1 RT-HIGH-07] staff key generation: REQUIRED attempt row before the order
    # marker is spent or any key is minted.
    audit_attempt(actor, "license.create", target_type="license", reason=reason,
                  ip=_client_ip(event), details={"plan": plan, "days": days, "count": count,
                                                 "discord_id": discord_id,
                                                 "route": "bot_deliver"})

    # HIGH-3: /deliver is the admin manual-mint path (no order in the general
    # case), but when an order_id IS supplied it is spend-once, just like
    # /provision — a replayed deliver can't double-mint the same order.
    if order_id:
        dup = _claim_order(order_id, "admin_deliver")
        if dup is not None:
            return ok({"ok": True, "license_key": dup, "plan": plan,
                       "duplicate_order": True, "message": "Order already delivered."}, 200)

    now = now_ts()
    expiry = 0 if plan == "lifetime" else now + days * 86400
    keys = []
    for n in range(count):
        key = gen_license_key()
        item = {
            "license_key": key, "status": "active", "revoked": False, "plan": plan,
            "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
            "created_at": now, "source": "admin_deliver",
            "created_by": f"{actor.get('type')}:{actor.get('id')}",
            "hwid_resets_used": 0, "hwid_paid_credits": 0, "hwid_reset_locked": False,
        }
        if discord_id:
            item["discord_user_id"] = discord_id
        if note:
            item["note"] = note
        # The order marker binds to the FIRST key of a bulk mint.
        if order_id and n == 0:
            item["order_id"] = order_id
        licenses_table().put_item(Item=item)
        keys.append({"license_key": key, "expiry": expiry})
        audit(actor, "license.create", target=key[-4:], target_type="license",
              reason=reason, ip=_client_ip(event),
              details={"plan": plan, "days": days, "discord_id": discord_id,
                       "order_id": order_id, "count": count, "route": "bot_deliver"})
        print(f"[INFO] AUDIT bot_deliver key ...{key[-4:]} plan={plan} "
              f"discord={discord_id} actor={actor.get('id')}")
    if order_id:
        _bind_order_license(order_id, keys[0]["license_key"])
    staff_consume(actor.get("id"), "keys", count)
    if count > 5 or normalize_role(actor.get("role")) == ROLE_ADMIN:
        owner_alert("license.create", actor, "License delivered via Discord",
                    {"plan": plan, "count": count, "to": discord_id, "reason": reason})
    return ok({"ok": True, "license_key": keys[0]["license_key"], "keys": keys,
               "count": len(keys), "plan": plan, "expiry": expiry,
               "message": f"Minted {count} {plan} license{'s' if count != 1 else ''}."})

def _notify_stripe_provision(discord_id, key, order_id, *, renewed=False):
    """Confirm an account-bound subscription without delivering its private key."""
    license_item = licenses_table().get_item(Key={"license_key": key}).get("Item", {})
    if str(license_item.get("discord_user_id") or "") != str(discord_id):
        return False
    marker = "ORDER#" + order_id
    if licenses_table().get_item(Key={"license_key": marker}).get("Item", {}).get("notified_at"):
        return True
    embed = ({
        "title": "🔁 Venice subscription renewed",
        "description": "Your monthly membership renewed on this Discord account. Keep using Venice on your linked PC; run `/status` to check access.",
        "fields": [
            {"name": "Moving to another PC?", "value": "Run `/hwid_reset`, then connect Discord again at https://zaeorion.com/connect.", "inline": False},
            {"name": "Manage or cancel", "value": BILLING_PORTAL_URL, "inline": False},
        ],
        "color": BRAND_EMBED_COLOR,
        "footer": {"text": "Venice • Need help? Open a ticket"},
    } if renewed else {
        "title": "✅ Venice subscription active",
        "description": "Your Venice membership is active and linked to this Discord account. Sign in with Discord on the PC where you use Venice; your public Discord ID is not a password or activation code.",
        "fields": [
            {"name": "Connect Venice", "value": "https://zaeorion.com/connect", "inline": False},
            {"name": "Check membership", "value": "Run `/status` privately in the Venice server.", "inline": False},
            {"name": "Manage or cancel", "value": BILLING_PORTAL_URL, "inline": False},
            {"name": "Support", "value": "Open a ticket in the Venice server if delivery or setup needs help.", "inline": False},
        ],
        "color": BRAND_EMBED_COLOR,
        "footer": {"text": "Venice • Your Discord ID is not a password"},
    })
    try:
        _discord_dm(discord_id, embed)
        licenses_table().update_item(
            Key={"license_key": marker},
            UpdateExpression="SET notified_at = :t",
            ExpressionAttributeValues={":t": now_ts()})
        return True
    except Exception as exc:
        print(f"[WARN] Stripe provision DM pending order={order_id}: {type(exc).__name__}")
        return False


def _reconcile_order_role(discord_id, key, order_id):
    """Persist the role obligation separately from the order mint and DM.

    Discord role PUT is idempotent. If Discord succeeds but the marker write fails,
    replaying the webhook repeats only that PUT; it never mints or renews again.
    """
    if not discord_id:
        return False
    license_item = licenses_table().get_item(Key={"license_key": key}).get("Item", {})
    if str(license_item.get("discord_user_id") or "") != str(discord_id):
        return False
    marker_key = "ORDER#" + order_id
    marker = licenses_table().get_item(Key={"license_key": marker_key}).get("Item", {})
    if marker.get("minted_license_key") != key:
        return False
    if marker.get("role_granted_at"):
        return True
    granted = _discord_add_role(discord_id, DISCORD_CUSTOMER_ROLE_SSM)
    try:
        values = {":t": now_ts(), ":s": "delivered" if granted else "pending"}
        update = "SET role_last_attempt_at = :t, role_delivery_status = :s"
        if granted:
            update += ", role_granted_at = :t"
        licenses_table().update_item(
            Key={"license_key": marker_key}, UpdateExpression=update,
            ExpressionAttributeValues=values)
    except Exception as exc:
        print(f"[WARN] role delivery state write failed order={order_id}: {type(exc).__name__}")
        return False
    if not granted:
        audit_log("stripe_role_delivery_pending", key, {"order_id": order_id})
    return granted


def handle_bot_provision(event):
    return with_stripe_event_dedup(event, _handle_bot_provision)

def _handle_bot_provision(event):
    """POST /api/bot/provision — automated mint for the Gumroad webhook Lambda.
    Kept separate from /api/bot/deliver (admin-triggered,
    no order_id) so audit_log entries and future validation don't conflate the two
    call sites."""
    consumer = require_bot(event)
    if not consumer:
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid json")
    plan = str(body.get("plan", "")).strip().lower() or "month"
    days = int(body.get("days") or TIER_DAYS.get(plan, 30))
    discord_id = str(body.get("discord_user_id", "")).strip()
    order_id = str(body.get("order_id", "")).strip()
    notify = body.get("notify") is True
    subscription_id = str(body.get("subscription_id") or "").strip()
    is_stripe = order_id.startswith("stripe:") or bool(subscription_id) or notify
    if is_stripe:
        if consumer != "/orion/worker_bot_secret":
            return err("forbidden", 403)
        if (plan != "month" or days != 30 or not notify
                or not ((order_id.startswith("stripe:checkout:cs_") and not body.get("renew"))
                        or (order_id.startswith("stripe:invoice:in_") and body.get("renew") is True))):
            return err("invalid_stripe_provision", 400)
    if notify and (not order_id.startswith("stripe:") or not discord_id
                 or not subscription_id.startswith("sub_")
                 or not subscription_id[4:].isalnum()):
        return err("invalid_notification_target", 400)

    renew_requested = (str(body.get("renew", "")).strip().lower()
                       in ("1", "true", "yes") and bool(discord_id))
    renew_rows, renew_mode = [], "none"
    if renew_requested:
        # Do not spend the order marker before a lookup that can fail. Otherwise
        # an outage turns the paid renewal into a permanently pending order.
        renew_rows, renew_mode = find_licenses_by_discord(discord_id)
        if renew_mode == "unavailable":
            return err("entitlement_unavailable", 503)

    # ── HIGH-3: scope the mint to a verified-UNSPENT order_id. One backend license
    # per paid order EVER — an attacker replaying a webhook (or a leaked webhook
    # secret firing repeated provisions) can no longer fan out N licenses. The
    # order marker is claimed atomically BEFORE minting.
    if not order_id:
        return err("order_id required", 400)
    dup = _claim_order(order_id, "sellhub")
    if dup is not None:
        if notify:
            if not dup:
                return err("order_pending", 503)
            marker = licenses_table().get_item(Key={"license_key": "ORDER#" + order_id}).get("Item", {})
            role_granted = _reconcile_order_role(discord_id, dup, order_id)
            notified = _notify_stripe_provision(discord_id, dup, order_id,
                                                renewed=bool(marker.get("renewed")))
            if not notified:
                return err("dm_pending", 503)
            if not role_granted:
                return err("role_pending", 503)
            return ok({"ok": True, "license_key_suffix": dup[-4:],
                       "duplicate_order": True, "notified": True,
                       "role_granted": True}, 200)
        if dup and discord_id:
            _reconcile_order_role(discord_id, dup, order_id)
        return ok({"ok": True, "license_key": dup, "plan": plan,
                   "duplicate_order": True}, 200)

    now = now_ts()

    # ── Recurring membership renewal (owner rule 2026-09-15: month, recurring) ──
    # A Gumroad membership fires a ping on EVERY charge. Minting a fresh key each
    # month would leave the customer re-pasting a new key and the old one dying,
    # so a renewal ping extends the key they already have instead. Guards: never
    # resurrect a revoked key, never touch `status` (a frozen key stays frozen for
    # the owner to release), and a key with no expiry has nothing to extend.
    if renew_requested:
        rows = renew_rows
        # A later trial or staff comp on the same Discord account is not the
        # paid subscription being renewed. Never convert that key into paid
        # access or extend it in place.
        paid_rows = [row for row in rows
                     if str(row.get("source") or "").lower() == "gumroad"
                     and str(row.get("order_id") or "").strip()
                     and str(row.get("plan") or "").lower() == "month"
                     and (not subscription_id
                          or row.get("stripe_subscription_id") == subscription_id)]
        paid_rows.sort(key=lambda row: int(row.get("created_at", 0) or 0), reverse=True)
        current = _newest_active(paid_rows) or (paid_rows[0] if paid_rows else None)
        if current and not bool(current.get("revoked", False)):
            ckey = current["license_key"]
            try:
                cur_expiry = int(current.get("expiry", 0) or 0)
            except (TypeError, ValueError):
                cur_expiry = 0
            if cur_expiry != 0:
                # A lapsed subscription renews from today, not from the old expiry.
                new_expiry = max(now, cur_expiry) + days * 86400
                update_expression = ("SET expiry = :x, #p = :p, renewed_at = :n, "
                                     "entitlement_type = :e, subscription_status = :a")
                values = {":x": new_expiry, ":p": plan, ":n": now,
                          ":e": "discord_subscription", ":a": "active"}
                if is_stripe:
                    update_expression += ", oauth_pair_required = :oauth"
                    values[":oauth"] = True
                licenses_table().update_item(
                    Key={"license_key": ckey},
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames={"#p": "plan"},
                    ExpressionAttributeValues=values)
                _bind_order_license(order_id, ckey)
                if notify:
                    licenses_table().update_item(Key={"license_key": "ORDER#" + order_id},
                                                  UpdateExpression="SET renewed = :r",
                                                  ExpressionAttributeValues={":r": True})
                audit_log("bot_provision_renew", ckey,
                          {"plan": plan, "days": days, "discord_id": discord_id,
                           "order_id": order_id, "expiry": new_expiry,
                           "lookup_mode": renew_mode})
                print(f"[INFO] AUDIT bot_provision_renew key ...{ckey[-4:]} plan={plan} "
                      f"days={days} order={order_id}")
                role_granted = _reconcile_order_role(discord_id, ckey, order_id)
                if notify:
                    notified = _notify_stripe_provision(discord_id, ckey, order_id,
                                                        renewed=True)
                    if not notified:
                        return err("dm_pending", 503)
                    if not role_granted:
                        return err("role_pending", 503)
                    return ok({"ok": True, "license_key_suffix": ckey[-4:], "plan": plan,
                               "expiry": new_expiry, "renewed": True,
                               "role_granted": role_granted, "notified": True}, 200)
                return ok({"ok": True, "license_key": ckey, "plan": plan,
                           "expiry": new_expiry, "renewed": True,
                           "role_granted": role_granted}, 200)

    key = gen_license_key()
    expiry = 0 if plan == "lifetime" else now + days * 86400
    item = {
        "license_key": key, "status": "active", "revoked": False, "plan": plan,
        "machine_id": "", "expiry": expiry, "activations": 0, "max_devices": 1,
        "created_at": now, "source": "gumroad", "order_id": order_id,
        "entitlement_type": "discord_subscription",
        "subscription_status": "active",
    }
    if discord_id:
        item["discord_user_id"] = discord_id
    if subscription_id:
        item["stripe_subscription_id"] = subscription_id
    if is_stripe:
        item["oauth_pair_required"] = True
    licenses_table().put_item(Item=item)
    _bind_order_license(order_id, key)
    audit_log("bot_provision", key, {"plan": plan, "days": days, "discord_id": discord_id, "order_id": order_id})
    print(f"[INFO] AUDIT bot_provision key ...{key[-4:]} plan={plan} days={days} order={order_id}")
    role_granted = _reconcile_order_role(discord_id, key, order_id) if discord_id else False
    if notify:
        notified = _notify_stripe_provision(discord_id, key, order_id)
        if not notified:
            return err("dm_pending", 503)
        if not role_granted:
            return err("role_pending", 503)
        return ok({"ok": True, "license_key_suffix": key[-4:], "plan": plan,
                   "expiry": expiry, "role_granted": role_granted, "notified": True}, 201)
    return ok({"ok": True, "license_key": key, "plan": plan, "expiry": expiry,
               "role_granted": role_granted}, 201)

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

def _valid_shard_build_id(build_id):
    return (isinstance(build_id, str) and len(build_id) == 64 and
            all(ch in "0123456789abcdef" for ch in build_id.lower()))


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
    try:
        max_activations = int(body.get("max_activations", 0))
    except (TypeError, ValueError):
        return err("max_activations must be an integer", 400)
    try:
        ttl_hours = int(body.get("ttl_hours", 0))
    except (TypeError, ValueError):
        return err("ttl_hours must be an integer", 400)
    if not build_id or not shard_hex:
        return err("build_id and shard required")
    if not _valid_shard_build_id(build_id):
        return err("build_id must be a 64-character hex digest", 400)
    try:
        shard_bytes = bytes.fromhex(shard_hex)
    except ValueError:
        return err("shard must be hex-encoded")
    if len(shard_bytes) != 32:
        return err("shard must be exactly 32 bytes")
    # Public Venice releases use ONE fragment per release build. Per-customer
    # enforcement belongs to the live license/session row; carrying it on the
    # shared build row would make the first customer lock out every other one.
    if license_id or hwid_hash or max_activations != 0:
        return err("public_release_shard_required", 400,
                   message=("Release shards must not include license_id, hwid_hash, "
                            "or a build-wide activation cap."))
    try:
        ct_hex, nonce_hex = _shard_encrypt(shard_bytes)
    except Exception as e:
        print(f"[SHARD] encryption failed: {e}")
        return err("internal_error", 500)
    now = now_ts()
    expires_at = (now + ttl_hours * 3600) if ttl_hours > 0 else 0
    audit_attempt(make_actor("owner", "break-glass", ROLE_OWNER, _client_ip(event)),
                  "shard.store", target=build_id[:16], target_type="shard",
                  reason="store release shard", ip=_client_ip(event))
    try:
        shards_table().put_item(Item={
            "build_id": build_id,
            "encrypted_shard": ct_hex,
            "shard_nonce": nonce_hex,
            "created_at": now,
            "expires_at": expires_at,
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

def _customer_shard_session(event, body, machine_id):
    """Resolve a customer session without exposing the global edge secret.

    The outer signed launcher persists these values with DPAPI after normal
    `/api/activate`. A copied Discord id or license key is not authentication.
    """
    headers = _headers(event)
    authorization = str(headers.get("authorization") or "")
    token_id = str(headers.get("x-orion-token-id") or
                   body.get("token_id") or "").strip()
    if not authorization.lower().startswith("bearer "):
        return None, None, err("missing_token", 401,
                               message="Connect Discord to Venice again.")
    token = authorization[7:].strip()
    if not token or not token_id:
        return None, None, err("missing_token", 401,
                               message="Connect Discord to Venice again.")
    try:
        token_item = tokens_table().get_item(Key={"token_id": token_id}).get("Item")
    except Exception:
        return None, None, err("internal_error", 500)
    if not token_item or token_item.get("kind") == "staff":
        return None, None, err("invalid_token", 403,
                               message="Connect Discord to Venice again.")
    supplied_hash = hashlib.sha256(token.encode()).hexdigest()
    if not secrets.compare_digest(
            supplied_hash, str(token_item.get("token_hash") or "")):
        return None, None, err("invalid_token", 403,
                               message="Connect Discord to Venice again.")
    try:
        token_expires = int(token_item.get("expires") or 0)
    except (TypeError, ValueError):
        token_expires = 0
    if token_expires <= now_ts():
        return None, None, err("token_expired", 403,
                               message="Connect Discord to Venice again.")
    bound_machine = str(token_item.get("machine_id") or "")
    if not bound_machine or not secrets.compare_digest(bound_machine, machine_id):
        return None, None, err(
            "machine_mismatch", 403,
            message="This subscription is linked to another PC. Use /hwid_reset, then retry.")
    license_key = str(token_item.get("license_key") or "").strip().upper()
    if not license_key:
        return None, None, err("invalid_token", 403,
                               message="Connect Discord to Venice again.")
    return license_key, token_item, None


def handle_shard_retrieve(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    build_id = (body.get("build_id") or "").strip()
    machine_id = (body.get("machine_id") or "").strip()
    if not build_id:
        return err("build_id required")
    if not _valid_shard_build_id(build_id):
        return err("build_id must be a 64-character hex digest", 400)
    if not machine_id or len(machine_id) > 256:
        return err("machine_id required")
    license_key, _token_item, session_error = _customer_shard_session(
        event, body, machine_id)
    if session_error:
        audit_log("shard_session_denied", extra={"build_id": build_id[:16]})
        return session_error
    rate_subject = hashlib.sha256(
        (license_key + "\0" + machine_id).encode("utf-8")).hexdigest()
    if not rate_limit_ok("shard_retrieve", rate_subject,
                         limit=SHARD_RETRIEVE_LIMIT, window=60,
                         fail_open=False):
        audit_log("shard_retrieve_rate_limited", license_key,
                  {"build_id": build_id[:16]})
        return err("rate_limited", 429,
                   message="Too many launch attempts. Wait one minute, then retry.")
    global_kill, _global_reason = get_global_kill()
    if global_kill:
        audit_log("shard_global_kill", license_key,
                  {"build_id": build_id[:16]})
        return kill_denial(_global_reason,
                           message="Venice is temporarily unavailable. Check the server for updates.")
    try:
        r = shards_table().get_item(Key={"build_id": build_id})
    except Exception as e:
        print(f"[SHARD] retrieve DDB error: {e}")
        return err("internal_error", 500)
    item = r.get("Item")
    if not item:
        return err("build_not_found", 404,
                   message="This Venice build is no longer available. Update Venice.")
    if item.get("revoked", False):
        audit_log("shard_retrieve_revoked", extra={"build_id": build_id[:16]})
        return err("build_revoked", 403,
                   message="This Venice build is no longer available. Update Venice.")
    expires_at = int(item.get("expires_at", 0))
    if expires_at > 0 and expires_at < now_ts():
        return err("build_expired", 403,
                   message="This Venice build is no longer available. Update Venice.")
    try:
        lr = licenses_table().get_item(Key={"license_key": license_key})
        lic = lr.get("Item")
    except Exception:
        return err("internal_error", 500)
    if not lic:
        return err("license_inactive", 403,
                   message="Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.")
    if lic.get("revoked", False) or lic.get("status") == "revoked":
        return err("license_revoked", 403,
                   message="Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.")
    if lic.get("status") != "active":
        return err("license_inactive", 403,
                   message="Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.")
    if not lic.get("machine_id") or not secrets.compare_digest(
            str(lic.get("machine_id")), machine_id):
        audit_log("shard_machine_mismatch", license_key,
                  {"build_id": build_id[:16]})
        return err("machine_mismatch", 403,
                   message="This subscription is linked to another PC. Use /hwid_reset, then retry.")
    try:
        lic_expiry = int(lic.get("expiry") or 0)
    except (TypeError, ValueError):
        lic_expiry = 0
    if 0 < lic_expiry < now_ts():
        return err("license_expired", 403,
                   message="Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.")
    entitled, entitlement_reason, lookup_mode = discord_access_entitlement(lic)
    if not entitled:
        if lookup_mode == "unavailable":
            return err("entitlement_unavailable", 503)
        audit_log("shard_subscription_required", license_key,
                  {"build_id": build_id[:16], "reason": entitlement_reason,
                   "lookup_mode": lookup_mode})
        return err("subscription_required", 403,
                   message="Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.")
    if is_blacklisted(machine_id=machine_id,
                      discord_id=lic.get("discord_user_id")):
        audit_log("shard_blacklisted", license_key,
                  {"build_id": build_id[:16]})
        return err("license_inactive", 403,
                   message="Your Venice access is not active. Open a ticket.")
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
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    build_id = (body.get("build_id") or "").strip()
    if not build_id:
        return err("build_id required")
    if not _valid_shard_build_id(build_id):
        return err("build_id must be a 64-character hex digest", 400)
    attempt_id = audit_attempt(actor, "shard.revoke", target=build_id[:16], target_type="shard",
                               reason=str(body.get("reason") or "shard revoke")[:REASON_MAX],
                               ip=_client_ip(event))
    try:
        shards_table().update_item(
            Key={"build_id": build_id},
            UpdateExpression="SET revoked = :r, revoked_at = :t",
            ExpressionAttributeValues={":r": True, ":t": now_ts()})
    except Exception as e:
        print(f"[SHARD] revoke failed: {e}")
        return err("internal_error", 500)
    audit_log("shard_revoked", extra={"build_id": build_id[:16]})
    audit_complete(attempt_id, actor, "shard.revoke", target=build_id[:16], target_type="shard",
                   ip=_client_ip(event))
    print(f"[WARNING] Shard revoked: build_id={build_id[:16]}...")
    return ok({"ok": True, "revoked": True})

def handle_shard_status(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    qs = (event.get("queryStringParameters") or {})
    body = {}
    if (event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
            == "POST"):
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return err("invalid_json", 400)
    build_id = (qs.get("build_id") or body.get("build_id") or "").strip()
    if not build_id:
        return err("build_id required")
    if not _valid_shard_build_id(build_id):
        return err("build_id must be a 64-character hex digest", 400)
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
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at", 0),
        "activation_count": item.get("activation_count", 0),
        "revoked": item.get("revoked", False),
        "last_retrieved": item.get("last_retrieved"),
    })

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL V2 — contract docs/ADMIN_PANEL_V2_CONTRACT.md (§1-§6)
# ══════════════════════════════════════════════════════════════════════════════

# ── §5 config (orion-config, one row per key, payload under `value`) ───────────
CONFIG_DEFAULTS = {
    "min_client_version":    "",
    "blocked_versions":      [],
    "motd":                  {},
    # `price_url` was dropped 2026-09-15: customers can no longer buy a reset, so
    # there is nothing to link to. An owner who stores one anyway is ignored.
    "reset_policy_defaults": {"free_resets": RESET_FREE_DEFAULT,
                              "penalty_days": RESET_PENALTY_DAYS,
                              "cooldown_s": RESET_COOLDOWN_S,
                              "self_service": True},
    "fraud_thresholds":      {"machines_30d": FRAUD_MACHINES_30D,
                              "resets_30d": FRAUD_RESETS_30D},
    "owner_totp_required":   False,
    "owner_ip_allowlist":    [],
    "alerts":                {"owner_discord_user_id": "", "events": list(ALERT_EVENTS_DEFAULT)},
}
CONFIG_KEYS = ["global_kill"] + list(CONFIG_DEFAULTS.keys())

def _from_ddb(v):
    """Decimal -> int/float, recursively, so config payloads round-trip as JSON."""
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, list):
        return [_from_ddb(x) for x in v]
    if isinstance(v, dict):
        return {k: _from_ddb(x) for k, x in v.items()}
    return v

# The heartbeat reads several config keys per beat (version gate, MOTD). A warm
# Lambda container caches them briefly so a busy fleet does not turn one heartbeat
# into six DynamoDB reads. `global_kill` is deliberately NOT read through here
# (get_global_kill stays uncached) so the kill switch is always instant, and any
# write through config_set drops the cache immediately.
CONFIG_CACHE_TTL_S = 15
_config_cache = {}

def config_get(key, default=None):
    """Read one config key. Missing row -> the contract default (never None for a
    known key), so a fresh deployment behaves exactly like a configured one."""
    if default is None:
        default = CONFIG_DEFAULTS.get(key)
        if isinstance(default, (dict, list)):
            default = json.loads(json.dumps(default))
    hit = _config_cache.get(key)
    if hit and hit[0] > now_ts():
        val = hit[1]
    else:
        try:
            item = config_table().get_item(Key={"config_key": key}).get("Item")
        except Exception as e:
            print(f"[CONFIG] read failed key={key}: {e}")
            return default
        val = _from_ddb(item["value"]) if (item and "value" in item) else None
        _config_cache[key] = (now_ts() + CONFIG_CACHE_TTL_S, val)
    if val is None:
        return default
    if isinstance(default, dict) and isinstance(val, dict):
        merged = json.loads(json.dumps(default))
        merged.update(val)
        return merged
    return val

def config_set(key, value):
    config_table().put_item(Item={
        "config_key": key,
        "value": json.loads(json.dumps(value, default=decimal_default)),
        "updated_at": now_ts(),
    })
    _config_cache.pop(key, None)

def reset_policy():
    return config_get("reset_policy_defaults")

def fraud_thresholds():
    return config_get("fraud_thresholds")

# ── §5 owner TOTP (RFC 6238, HMAC-SHA1, 30 s window, +/-1 step) ───────────────
def _b32_decode(secret_b32):
    s = (secret_b32 or "").strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))

def totp_at(secret_b32, counter):
    key = _b32_decode(secret_b32)
    h = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = ((h[o] & 0x7F) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3]
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)

def totp_now(secret_b32, at=None):
    return totp_at(secret_b32, int((at if at is not None else time.time()) // TOTP_STEP_S))

def totp_verify(secret_b32, code, at=None, window=TOTP_WINDOW_STEPS):
    """Accept the current step +/- `window` steps. Constant-time compare."""
    code = (str(code or "")).strip()
    if not code or not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    base = int((at if at is not None else time.time()) // TOTP_STEP_S)
    for drift in range(-window, window + 1):
        try:
            if secrets.compare_digest(code, totp_at(secret_b32, base + drift)):
                return True
        except Exception:
            return False
    return False

def gen_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

# [P-F follow-up] A re-enrolment is staged here and only replaces the ACTIVE
# secret at totp_confirm, so the current factor (and a break-glass session code)
# keeps working until the new authenticator has proven itself.
OWNER_TOTP_PENDING_SSM = "/orion/owner_totp_pending_secret"

def owner_totp_pending_secret():
    try:
        return ssm_get(OWNER_TOTP_PENDING_SSM, decrypt=True)
    except Exception:
        return ""

def _clear_pending_totp():
    try:
        get_ssm().delete_parameter(Name=OWNER_TOTP_PENDING_SSM)
    except Exception as e:
        # ParameterNotFound is the normal case; anything else is only a stale stage.
        if "ParameterNotFound" not in str(e):
            print(f"[TOTP] pending secret cleanup failed: {type(e).__name__}")

def owner_totp_secret():
    try:
        return ssm_get(OWNER_TOTP_SSM, decrypt=True)
    except Exception:
        return ""

# ── §5 owner IP allowlist ─────────────────────────────────────────────────────
def _ip_in_cidr(ip, cidr):
    try:
        import ipaddress
        entry = str(cidr).strip()
        if not entry:
            return False
        net = ipaddress.ip_network(entry if "/" in entry else entry + "/32", strict=False)
        return ipaddress.ip_address(ip) in net
    except Exception:
        return False

def ip_allowed(ip, allowlist):
    """Empty allowlist = allow everything (the default). A non-empty allowlist with
    an unparseable/absent client IP DENIES — fail closed on the owner surface."""
    if not allowlist:
        return True
    if not ip:
        return False
    ip = str(ip).split(",")[0].strip()
    return any(_ip_in_cidr(ip, c) for c in allowlist)

# ── §1 roles + capability matrix (ONE enforcement function) ───────────────────
ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT = "owner", "admin", "support"

def normalize_role(role):
    """Legacy rows carry role="staff" (written by the pre-V2 enroll path). They map
    to `support` — the least-privileged staff role — so an old row can never gain
    capability by accident."""
    r = str(role or "").strip().lower()
    if r in (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT):
        return r
    if r in ("staff", "", "member", "mod"):
        return ROLE_SUPPORT
    return ROLE_SUPPORT

CAPABILITIES = {
    "license.lookup":            (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT),
    "license.create":            (ROLE_OWNER, ROLE_ADMIN),
    "license.reset_machine":     (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT),
    "license.reset_machine.force": (ROLE_OWNER,),
    "license.extend":            (ROLE_OWNER, ROLE_ADMIN),
    "license.revoke":            (ROLE_OWNER, ROLE_ADMIN),
    "license.unrevoke":          (ROLE_OWNER, ROLE_ADMIN),
    "license.freeze":            (ROLE_OWNER, ROLE_ADMIN),
    "license.unfreeze":          (ROLE_OWNER, ROLE_ADMIN),
    "license.set_plan":          (ROLE_OWNER, ROLE_ADMIN),
    "license.transfer":          (ROLE_OWNER, ROLE_ADMIN),
    "license.set_reset_policy":  (ROLE_OWNER,),
    "license.blacklist":         (ROLE_OWNER,),
    "staff.manage":              (ROLE_OWNER,),
    "config.write":              (ROLE_OWNER,),
    "config.read":               (ROLE_OWNER,),
    "audit.read_all":            (ROLE_OWNER,),
    "audit.read_own":            (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT),
    "metrics":                   (ROLE_OWNER,),
}

def has_capability(actor, cap):
    return normalize_role((actor or {}).get("role")) in CAPABILITIES.get(cap, ())

def require_capability(actor, cap):
    """Returns None when allowed, or the 403 response. Denials are audited with the
    actor so an attempted privilege escalation leaves a trail."""
    if has_capability(actor, cap):
        return None
    audit(actor, cap, target_type="capability", result="forbidden",
          details={"capability": cap, "role": normalize_role((actor or {}).get("role"))})
    return err("forbidden", 403, message=f"Your role cannot perform {cap}.",
               capability=cap)

# ── §1 actor resolution ───────────────────────────────────────────────────────
def _headers(event):
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}

def admin_secret_ok(event):
    h = _headers(event)
    secret = h.get("x-orion-admin-secret") or ""
    if not secret:
        return False
    try:
        expected = ssm_get(ADMIN_SECRET_SSM, decrypt=True)
    except Exception:
        return False
    return secrets.compare_digest(secret, expected)

class ConfigUnavailable(Exception):
    """A SECURITY config key could not be read. The router answers 503
    config_unavailable: the owner surface must never fall back to the permissive
    default (TOTP off / allow-all) because DynamoDB blipped."""

def config_get_strict(key):
    """[rc1 RT-CRIT-01] Uncached, strongly consistent read of a security config key
    (owner_totp_required, owner_ip_allowlist). config_get() returns the permissive
    DEFAULT on a read error and caches for 15 s; both are wrong for these keys."""
    try:
        item = config_table().get_item(Key={"config_key": key},
                                       ConsistentRead=True).get("Item")
    except Exception as e:
        print(f"[CONFIG] security read failed key={key}: {e}")
        raise ConfigUnavailable(key)
    val = _from_ddb(item["value"]) if (item and "value" in item) else None
    if val is None:
        default = CONFIG_DEFAULTS.get(key)
        return json.loads(json.dumps(default)) if isinstance(default, (dict, list)) else default
    return val

def owner_totp_ok(event):
    """(ok, error_code). When config owner_totp_required is false this is a no-op,
    which is the shipped default until the owner enrolls."""
    if not config_get_strict("owner_totp_required"):
        return True, None
    code = _headers(event).get("x-orion-admin-totp") or ""
    secret = owner_totp_secret()
    if not secret:
        # Requirement on but no secret provisioned: fail CLOSED rather than
        # silently dropping the second factor.
        return False, "totp_not_provisioned"
    if not code:
        return False, "totp_required"
    if not totp_verify(secret, code):
        return False, "invalid_totp"
    return True, None

def resolve_owner(event):
    """§1 owner identity: break-glass admin secret (+TOTP) OR a staff row with
    role=owner presenting a machine-bound bearer token.
    Returns (actor, None) or (None, error_response)."""
    ip = _client_ip(event)
    allow = config_get_strict("owner_ip_allowlist") or []
    if not ip_allowed(ip, allow):
        audit(make_actor("owner", "unknown", ROLE_OWNER, ip), "owner.ip_denied",
              target_type="global", result="ip_not_allowed", ip=ip)
        return None, err("ip_not_allowed", 403,
                         message="This IP is not on the owner allowlist.")
    h = _headers(event)
    if h.get("x-orion-admin-secret"):
        if not admin_secret_ok(event):
            return None, err("forbidden", 403)
        tok_ok, tcode = owner_totp_ok(event)
        if not tok_ok:
            audit(make_actor("owner", "break-glass", ROLE_OWNER, ip), "owner.totp_denied",
                  target_type="global", result=tcode, ip=ip)
            return None, err(tcode, 403, message="A valid owner TOTP code is required.")
        return make_actor("owner", "break-glass", ROLE_OWNER, ip), None
    if h.get("authorization"):
        staff, e = require_staff(event)
        if e:
            return None, e
        role = normalize_role(staff.get("role"))
        if role != ROLE_OWNER:
            audit(make_actor("staff", staff.get("staff_id"), role, ip), "owner.role_denied",
                  target_type="global", result="forbidden", ip=ip)
            return None, err("forbidden", 403, message="Owner role required.")
        return make_actor("staff", staff.get("staff_id"), ROLE_OWNER, ip), None
    return None, err("forbidden", 403)

# ── [2026-09-23 rc1 RT-CRIT-01 / CSEC-12] owner step-up ───────────────────────
# resolve_owner() checks TOTP only on the break-glass path, so an owner-role STAFF
# bearer (obtained with staff_id + a claimed machine_id) could disable TOTP, rotate
# the break-glass secret, re-enroll TOTP, promote another owner or release the kill.
# Every such action now needs a FRESH, SINGLE-USE owner TOTP code
# (X-Orion-Admin-TOTP header or body `step_up_code`) regardless of which
# authenticator resolved the actor. When owner TOTP is not required there is no
# second factor to step up to: the break-glass secret (itself the possession factor)
# may still act, a staff bearer may not (403 step_up_required).
STEP_UP_CONFIG_KEYS = ("owner_totp_required", "owner_ip_allowlist", "alerts")
TOTP_USED_TTL_S = TOTP_STEP_S * (2 * TOTP_WINDOW_STEPS + 2)

def totp_match_counter(secret_b32, code, at=None, window=TOTP_WINDOW_STEPS):
    """The time-step counter `code` matches (+/- window), or None."""
    code = (str(code or "")).strip()
    if not code or not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    base = int((at if at is not None else time.time()) // TOTP_STEP_S)
    for drift in range(-window, window + 1):
        try:
            if secrets.compare_digest(code, totp_at(secret_b32, base + drift)):
                return base + drift
        except Exception:
            return None
    return None

def _totp_tag(secret_b32):
    """Short, non-reversible namespace for a secret's burned steps, so the step of
    one secret (e.g. a pending re-enrolment) never collides with another's."""
    return hashlib.sha256(("orion-totp-tag:" + str(secret_b32 or "")).encode()).hexdigest()[:16]

def totp_consume(counter, secret_b32=None):
    """Burn a matched TOTP step so the same code can never authorise a second
    sensitive action (or a second owner login). True = fresh, False = replayed.
    Raises on a store outage (callers answer 503, never 'allowed').
    The key is namespaced by the secret the step was matched against."""
    tag = _totp_tag(secret_b32 if secret_b32 is not None else owner_totp_secret())
    key = f"owner_totp_used:{tag}:{int(counter)}"
    now = now_ts()
    try:
        nonces_table().put_item(
            Item={"nonce": key, "ts": now, "expires": now + TOTP_USED_TTL_S,
                  "ttl": now + TOTP_USED_TTL_S},
            ConditionExpression="attribute_not_exists(nonce)")
        return True
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return False

def _step_up_code(event, body=None):
    code = _headers(event).get("x-orion-admin-totp") or ""
    if not code and isinstance(body, dict):
        code = body.get("step_up_code") or body.get("totp_code") or ""
    return str(code).strip()

def is_break_glass(actor):
    return (actor or {}).get("type") == "owner" and (actor or {}).get("id") == "break-glass"

def owner_step_up(event, actor, action, body=None, target=None, target_type="config",
                  reason="", details=None):
    """Gate one owner-sensitive action. Order: verify the code (deny + audit on
    failure) -> REQUIRED attempt audit (AuditUnavailable -> 503, nothing burned or
    mutated) -> burn the code (replay -> deny + audit). Returns
    (attempt_id, None) to proceed, or (None, error_response)."""
    ip = _client_ip(event)

    def deny(code, message):
        audit(actor, "owner.step_up_denied", target=action, target_type=target_type,
              result=code, ip=ip, details={"action": action})
        return None, err(code, 403, message=message, step_up=True)

    required = bool(config_get_strict("owner_totp_required"))
    counter = None
    if not required:
        if not is_break_glass(actor):
            return deny("step_up_required",
                        "This action needs a second factor. Enable owner TOTP, or use "
                        "the break-glass owner secret.")
    else:
        secret = owner_totp_secret()
        if not secret:
            return deny("totp_not_provisioned", "Owner TOTP is required but not provisioned.")
        code = _step_up_code(event, body)
        if not code:
            return deny("totp_required", "A fresh owner TOTP code is required for this action.")
        counter = totp_match_counter(secret, code)
        if counter is None:
            return deny("invalid_totp", "That owner TOTP code did not verify.")
    attempt_id = audit_attempt(actor, action, target=target, target_type=target_type,
                               reason=reason, ip=ip, details=details)
    if counter is not None:
        try:
            fresh = totp_consume(counter, secret)
        except Exception as e:
            print(f"[STEP-UP] replay store unavailable: {e}")
            return None, err("step_up_unavailable", 503,
                             message="The step-up check is unavailable. Retry shortly.")
        if not fresh:
            return deny("totp_replayed",
                        "That code was already used. Wait for the next code and retry.")
    return attempt_id, None

def admin_actor(event):
    """The owner actor the router's /api/admin/* gate resolved. Falls back to a
    direct require_admin() check so a handler invoked out of band (unit tests, a
    future route table) still AUTHENTICATES rather than trusting an absent gate."""
    actor = event.get("_actor")
    if actor:
        return actor
    if require_admin(event):
        return make_actor("owner", "break-glass", ROLE_OWNER, _client_ip(event))
    return None

def resolve_staff_actor(event):
    """Staff identity for /api/staff/*: bearer token + X-Machine-Id."""
    staff, e = require_staff(event)
    if e:
        return None, None, e
    actor = make_actor("staff", staff.get("staff_id"),
                       normalize_role(staff.get("role")), _client_ip(event))
    return staff, actor, None

def resolve_discord_actor(event, body):
    """§4 BREAKING: Discord front-ends forward `actor_discord_id`. Resolve it
    against orion-staff so the audit row names a person, not "the bot"."""
    actor_discord_id = str(body.get("actor_discord_id") or "").strip()
    if not actor_discord_id:
        return None, err("actor_required", 400,
                         message="This action requires actor_discord_id (the staff member running it).")
    staff = find_staff_by_discord(actor_discord_id)
    if not staff:
        audit(make_actor("customer", actor_discord_id, "customer", _client_ip(event)),
              "staff.actor_unknown", target_type="staff", result="not_staff")
        return None, err("not_staff", 403,
                         message="That Discord account is not a staff member.")
    if staff.get("disabled", False):
        return None, err("staff_disabled", 403, message="That staff account is disabled.")
    return make_actor("staff", staff.get("staff_id"),
                      normalize_role(staff.get("role")), _client_ip(event)), None

def require_self_or_staff(event, body, target_discord_id):
    """Discord read/self-service routes. A customer acting on their OWN account is
    open; acting on SOMEONE ELSE's requires a staff row (support+). The refusal
    code is `not_staff`, deliberately distinct from the edge-auth `forbidden` so
    the bots can tell "you are not staff" from "the gateway rejected you".
    Returns (actor, None) or (None, error_response)."""
    actor_discord_id = str(body.get("actor_discord_id") or "").strip()
    ip = _client_ip(event)
    if not actor_discord_id or actor_discord_id == str(target_discord_id):
        return make_actor("customer", target_discord_id, "customer", ip), None
    staff = find_staff_by_discord(actor_discord_id)
    if not staff or staff.get("disabled", False):
        audit(make_actor("customer", actor_discord_id, "customer", ip),
              "license.lookup", target=str(target_discord_id)[-6:],
              target_type="license", result="not_staff",
              details={"reason": "cross-account lookup by a non-staff account"})
        return None, err("not_staff", 403,
                         message="Only staff can act on another customer's account.")
    return make_actor("staff", staff.get("staff_id"),
                      normalize_role(staff.get("role")), ip), None

def find_staff_by_discord(discord_id):
    did = str(discord_id or "").strip()
    if not did:
        return None
    try:
        r = staff_table().query(
            IndexName=STAFF_DISCORD_GSI,
            KeyConditionExpression="discord_user_id = :d",
            ExpressionAttributeValues={":d": did}, Limit=1)
        items = r.get("Items", [])
        if items:
            return items[0]
        return None
    except Exception:
        pass
    try:
        rows = staff_table().scan(
            FilterExpression="discord_user_id = :d",
            ExpressionAttributeValues={":d": did}).get("Items", [])
        return rows[0] if rows else None
    except Exception as e:
        print(f"[STAFF] discord lookup failed: {e}")
        return None

# ── §1 per-staff caps / daily usage ───────────────────────────────────────────
def staff_caps(staff):
    caps = dict(DEFAULT_CAPS)
    raw = _from_ddb((staff or {}).get("caps") or {})
    if isinstance(raw, dict):
        for k in DEFAULT_CAPS:
            if raw.get(k) is not None:
                try:
                    caps[k] = int(raw[k])
                except (TypeError, ValueError):
                    pass
    return caps

def staff_usage_today(staff):
    usage = _from_ddb((staff or {}).get("usage") or {})
    today = day_str()
    if not isinstance(usage, dict) or usage.get("day") != today:
        return {"day": today, "keys": 0, "resets": 0}
    return {"day": today, "keys": int(usage.get("keys", 0) or 0),
            "resets": int(usage.get("resets", 0) or 0)}

def staff_consume(staff_id, field, n=1):
    """Bump today's usage counter. Owner/break-glass actors have no staff row and
    are never metered."""
    if not staff_id:
        return
    today = day_str()
    try:
        cur = staff_table().get_item(Key={"staff_id": staff_id}).get("Item") or {}
        usage = staff_usage_today(cur)
        usage[field] = int(usage.get(field, 0)) + int(n)
        usage["day"] = today
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET #u = :u",
            ExpressionAttributeNames={"#u": "usage"},
            ExpressionAttributeValues={":u": usage})
    except Exception as e:
        print(f"[STAFF] usage bump failed {staff_id}/{field}: {e}")

def check_cap(actor, staff, field, cap_name, want=1):
    """Owner is uncapped. Staff are bounded by caps.<cap_name> per UTC day."""
    if normalize_role(actor.get("role")) == ROLE_OWNER or not staff:
        return None
    caps = staff_caps(staff)
    used = staff_usage_today(staff).get(field, 0)
    limit = int(caps.get(cap_name, 0))
    if used + want > limit:
        audit(actor, "cap.exceeded", target_type="staff", target=actor.get("id"),
              result="cap_exceeded", details={"cap": cap_name, "used": used,
                                              "limit": limit, "want": want})
        return err("cap_exceeded", 429,
                   message=f"Daily cap reached ({used}/{limit} {cap_name}).",
                   cap=cap_name, used=used, limit=limit)
    return None

# ── §4 owner alerts (Discord DM; never block the operation) ───────────────────
def alert_enabled(event_name):
    cfg = config_get("alerts") or {}
    events = cfg.get("events") or []
    if event_name in events:
        return True
    # "config.*"-style wildcards from the contract's default list.
    prefix = event_name.split(".")[0] + ".*"
    return prefix in events

def owner_alert(event_name, actor, title, lines, force=False):
    """Best-effort DM to alerts.owner_discord_user_id. Failures are audited as
    `alert_failed` and swallowed — an alert must never fail a mutation."""
    try:
        if not force and not alert_enabled(event_name):
            return False
        cfg = config_get("alerts") or {}
        target = str(cfg.get("owner_discord_user_id") or "").strip()
        if not target:
            return False
        fields = [{"name": k, "value": str(v)[:1000], "inline": False} for k, v in lines.items()]
        _discord_dm(target, {
            "title": title[:250],
            "description": f"`{event_name}` by **{actor.get('type')}:{actor.get('id')}** "
                           f"({normalize_role(actor.get('role'))})",
            "fields": fields[:20],
            "color": BRAND_EMBED_COLOR,
            "footer": {"text": "Venice owner alert"},
        })
        return True
    except Exception as e:
        print(f"[ALERT] owner alert failed event={event_name}: {e}")
        audit(SYSTEM_ACTOR, "alert_failed", target=event_name, target_type="global",
              result="error", details={"error": str(e)[:200]})
        return False

# ── §5 version gate + MOTD ────────────────────────────────────────────────────
def _version_tuple(v):
    parts = []
    for chunk in str(v or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None

def version_gate(client_version):
    """(blocked: bool, info: dict). A client that sends NO version is not blocked —
    legacy launchers predate the field and must not be bricked by enabling the
    gate. Blocking those is a separate owner decision (global_kill)."""
    cv = str(client_version or "").strip()
    info = {"min_client_version": config_get("min_client_version", "") or ""}
    if not cv:
        return False, info
    blocked = config_get("blocked_versions", []) or []
    if cv in [str(b).strip() for b in blocked]:
        info["reason"] = "blocked_version"
        return True, info
    minv = info["min_client_version"]
    if minv:
        ct, mt = _version_tuple(cv), _version_tuple(minv)
        if ct is not None and mt is not None and ct < mt:
            info["reason"] = "below_minimum"
            return True, info
    return False, info

def get_motd():
    motd = config_get("motd") or {}
    if not isinstance(motd, dict) or not motd.get("text"):
        return None
    try:
        until = int(motd.get("until", 0) or 0)
    except (TypeError, ValueError):
        until = 0
    if until and until <= now_ts():
        return None
    level = str(motd.get("level", "info")).lower()
    if level not in ("info", "warn", "maint"):
        level = "info"
    return {"text": str(motd["text"])[:500], "level": level, "until": until}

# ── §2 blacklist (rows in orion-licenses, so no new table) ────────────────────
def _blacklist_key(kind, value):
    return f"BLACKLIST#{kind}#{value}"

def is_blacklisted(machine_id=None, discord_id=None):
    """Returns the matching blacklist kind ("machine"/"discord") or None."""
    for kind, val in (("machine", machine_id), ("discord", discord_id)):
        if not val:
            continue
        try:
            row = licenses_table().get_item(
                Key={"license_key": _blacklist_key(kind, str(val))}).get("Item")
        except Exception:
            row = None
        if row and not row.get("cleared", False):
            return kind
    return None

# ── §2/§3 license lookup (discord_user_id GSI, scan fallback) ─────────────────
def find_licenses_by_discord(discord_id):
    """(rows, lookup_mode). lookup_mode is "gsi" when the index served the read and
    "scan" when it is absent — surfaced in the response so the owner knows the GSI
    still has to be created. A partial page is never an authoritative empty lookup."""
    did = str(discord_id or "").strip()
    if not did:
        return [], "none"
    table = licenses_table()

    def all_pages(method, kwargs, max_pages):
        rows, start = [], None
        for _ in range(max_pages):
            request = dict(kwargs)
            if start:
                request["ExclusiveStartKey"] = start
            page = getattr(table, method)(**request)
            rows.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return rows
        raise RuntimeError("Discord licence lookup page limit reached")

    try:
        rows = all_pages("query", {
            "IndexName": LICENSE_DISCORD_GSI,
            "KeyConditionExpression": "discord_user_id = :d",
            "ExpressionAttributeValues": {":d": did}}, 64)
        return rows, "gsi"
    except Exception:
        pass
    try:
        # The fallback is bounded; a large table needs its GSI. An incomplete
        # scan reports unavailable instead of falsely denying a paying account.
        rows = all_pages("scan", {
            "FilterExpression": "discord_user_id = :d",
            "ExpressionAttributeValues": {":d": did}}, 32)
        return rows, "scan"
    except Exception as e:
        print(f"[LOOKUP] discord scan failed: {e}")
        return [], "unavailable"


def discord_access_entitlement(item, now=None):
    """Return ``(allowed, reason, lookup_mode)`` for launcher authority.

    A key identifies a Discord account; it is not itself the paid entitlement.
    The Discord account must own a live Gumroad purchase. This also permits a
    replacement key issued to an already-paying account without making manual
    key minting a subscription bypass.

    A Discord-issued trial is a separate short-lived entitlement. Historic
    Gumroad day/week/lifetime purchases remain valid while live, but require the
    same Discord ID and verified order provenance as the monthly membership.
    """
    now = int(now or now_ts())
    discord_id = str(item.get("discord_user_id") or "").strip()
    plan = str(item.get("plan") or "").strip().lower()
    source = str(item.get("source") or "").strip().lower()

    if not discord_id:
        return False, "missing_discord_id", "none"

    if plan == "trial":
        try:
            expiry = int(item.get("expiry") or 0)
        except (TypeError, ValueError):
            return False, "trial_expiry_invalid", "self"
        allowed = (source == "trial" and item.get("status") == "active"
                   and not bool(item.get("revoked", False)) and expiry > now)
        return allowed, ("trial_active" if allowed else "trial_inactive"), "self"

    rows, lookup_mode = find_licenses_by_discord(discord_id)
    if lookup_mode == "unavailable":
        return False, "lookup_unavailable", lookup_mode
    for candidate in rows:
        if str(candidate.get("source") or "").strip().lower() != "gumroad":
            continue
        if not str(candidate.get("order_id") or "").strip():
            continue
        if candidate.get("status") != "active" or bool(candidate.get("revoked", False)):
            continue
        try:
            expiry = int(candidate.get("expiry") or 0)
        except (TypeError, ValueError):
            continue
        candidate_plan = str(candidate.get("plan") or "").strip().lower()
        if candidate_plan == "lifetime" and expiry == 0:
            return True, "gumroad_lifetime", lookup_mode
        if expiry > now:
            return True, "gumroad_active", lookup_mode
    return False, "no_active_discord_purchase", lookup_mode

def _newest_active(rows):
    cand = [i for i in rows
            if not i.get("revoked", False) and i.get("status") == "active"
            and not str(i.get("license_key", "")).startswith(("ORDER#", "TRIAL#", "BLACKLIST#", "TRIALMACHINE#"))]
    cand.sort(key=lambda i: int(i.get("created_at", 0) or 0), reverse=True)
    return cand[0] if cand else None

def find_licenses_by_email(email):
    e = str(email or "").strip().lower()
    if not e:
        return [], "none"
    try:
        rows = licenses_table().scan(
            FilterExpression="email = :e",
            ExpressionAttributeValues={":e": e}).get("Items", [])
        return rows, "scan"
    except Exception:
        return [], "scan"

def find_licenses_by_machine(machine_id):
    m = str(machine_id or "").strip()
    if not m:
        return [], "none"
    try:
        rows = licenses_table().scan(
            FilterExpression="machine_id = :m",
            ExpressionAttributeValues={":m": m}).get("Items", [])
        return rows, "scan"
    except Exception:
        return [], "scan"

def resolve_license(body):
    """(item, lookup_mode, error_code). Accepts key | discord_user_id | email |
    machine_id (the contract's four lookup handles)."""
    key = (body.get("key") or body.get("license_key") or "").strip().upper()
    if key:
        try:
            item = licenses_table().get_item(Key={"license_key": key}).get("Item")
        except Exception:
            return None, "key", "internal_error"
        return (item, "key", None) if item else (None, "key", "invalid_key")
    for field, finder in (("discord_user_id", find_licenses_by_discord),
                          ("email", find_licenses_by_email),
                          ("machine_id", find_licenses_by_machine)):
        val = body.get(field)
        if val:
            rows, mode = finder(val)
            if mode == "unavailable":
                return None, mode, "entitlement_unavailable"
            item = _newest_active(rows) or (rows[0] if rows else None)
            return (item, mode, None) if item else (None, mode, "invalid_key")
    return None, "none", "missing_license_key"

# ── §3 HWID reset core ────────────────────────────────────────────────────────
def reset_state(item):
    pol = reset_policy()
    try:
        free = int(item.get("hwid_free_resets", pol.get("free_resets", RESET_FREE_DEFAULT)))
    except (TypeError, ValueError):
        free = RESET_FREE_DEFAULT
    # Per-key override first (license.set_reset_policy writes hwid_penalty_days and
    # until now nothing read it), then the global default. `deduct_days` is the
    # contract's spelling of the same number and is accepted as an alias.
    try:
        penalty = int(item.get("hwid_penalty_days",
                               pol.get("penalty_days",
                                       pol.get("deduct_days", RESET_PENALTY_DAYS))))
    except (TypeError, ValueError):
        penalty = RESET_PENALTY_DAYS
    return {
        "free_resets":  free,
        "resets_used":  int(item.get("hwid_resets_used", 0) or 0),
        "paid_credits": int(item.get("hwid_paid_credits", 0) or 0),
        "locked":       bool(item.get("hwid_reset_locked", False)),
        "last_reset_at": int(item.get("last_reset_at", item.get("bot_reset_last_at", 0)) or 0),
        "cooldown_s":   int(pol.get("cooldown_s", RESET_COOLDOWN_S)),
        "penalty_days": max(1, penalty),
        "plan":         str(item.get("plan", "") or "").strip().lower(),
        "self_service": bool(pol.get("self_service", True)),
    }

def apply_hwid_reset(item, actor, mode, reason, consume=None, expiry_delta=0):
    """The single mutation behind every reset path. `consume` is "free" | "paid" |
    None (staff/owner resets never touch the customer's counters, per §3)."""
    key = item["license_key"]
    now = now_ts()
    before = str(item.get("machine_id", "") or "")
    hist = _from_ddb(item.get("reset_history") or [])
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "ts": now,
        "by": f"{actor.get('type')}:{actor.get('id')}",
        "mode": mode,
        "machine_before_suffix": before[-6:] if before else "",
    })
    hist = hist[-RESET_HISTORY_MAX:]
    # [rc1 RT-HIGH-07] every reset path (staff, owner, Discord self-service) writes a
    # REQUIRED attempt row first; AuditUnavailable -> router 503, binding unchanged.
    attempt_id = audit_attempt(actor, "license.reset_machine", target=key[-4:],
                               target_type="license", reason=reason,
                               details={"mode": mode, "machine_before_suffix": before[-6:]})

    expr = ("SET machine_id = :e, activations = :z, last_reset_at = :n, "
            "bot_reset_last_at = :n, reset_history = :h")
    vals = {":e": "", ":z": 0, ":n": now, ":h": hist}
    st = reset_state(item)
    if consume == "free":
        expr += ", hwid_resets_used = :u"
        vals[":u"] = st["resets_used"] + 1
    elif consume == "paid":
        expr += ", hwid_paid_credits = :c"
        vals[":c"] = max(0, st["paid_credits"] - 1)
    if expiry_delta:
        try:
            cur_expiry = int(item.get("expiry", 0) or 0)
        except (TypeError, ValueError):
            cur_expiry = 0
        expr += ", expiry = :x"
        vals[":x"] = max(now, cur_expiry + expiry_delta)
    licenses_table().update_item(Key={"license_key": key},
                                 UpdateExpression=expr,
                                 ExpressionAttributeValues=vals)
    audit_complete(attempt_id, actor, "license.reset_machine", target=key[-4:],
                   target_type="license", reason=reason,
                   details={"mode": mode, "machine_before_suffix": before[-6:],
                            "expiry_delta": expiry_delta})
    try:
        new_item = licenses_table().get_item(Key={"license_key": key}).get("Item") or item
    except Exception:
        new_item = item
    return new_item

def resets_in_window(item, seconds):
    cutoff = now_ts() - seconds
    hist = _from_ddb(item.get("reset_history") or [])
    if not isinstance(hist, list):
        return 0
    return sum(1 for h in hist if int(h.get("ts", 0) or 0) >= cutoff)

# ── §6 fraud signals ──────────────────────────────────────────────────────────
def record_machine_history(item, machine_id):
    """Append the machine suffix to machine_history (last 10 distinct). Returns the
    updated list; the caller persists it."""
    hist = _from_ddb(item.get("machine_history") or [])
    if not isinstance(hist, list):
        hist = []
    suffix = str(machine_id or "")[-6:]
    hist = [h for h in hist if isinstance(h, dict) and h.get("suffix") != suffix]
    hist.append({"suffix": suffix, "ts": now_ts()})
    return hist[-MACHINE_HISTORY_MAX:]

def evaluate_fraud(item, machine_history):
    """§6: flag (never auto-kill). Returns (suspect, signals)."""
    th = fraud_thresholds()
    cutoff = now_ts() - 30 * 86400
    machines_30d = len({h.get("suffix") for h in (machine_history or [])
                        if int(h.get("ts", 0) or 0) >= cutoff and h.get("suffix")})
    resets_30d = resets_in_window(item, 30 * 86400)
    signals = {"machines_30d": machines_30d, "resets_30d": resets_30d,
               "thresholds": th}
    suspect = (machines_30d > int(th.get("machines_30d", FRAUD_MACHINES_30D)) or
               resets_30d > int(th.get("resets_30d", FRAUD_RESETS_30D)))
    return suspect, signals

def flag_fraud(item, signals, actor=None):
    key = item["license_key"]
    flags = _from_ddb(item.get("flags") or {})
    if not isinstance(flags, dict):
        flags = {}
    if flags.get("suspect"):
        return False
    flags["suspect"] = True
    flags["suspect_at"] = now_ts()
    flags["suspect_signals"] = signals
    try:
        licenses_table().update_item(
            Key={"license_key": key},
            UpdateExpression="SET flags = :f",
            ExpressionAttributeValues={":f": flags})
    except Exception as e:
        print(f"[FRAUD] flag write failed key=...{key[-4:]}: {e}")
        return False
    audit(actor or SYSTEM_ACTOR, "fraud.flag", target=key[-4:], target_type="license",
          reason="threshold exceeded", details=signals)
    owner_alert("fraud.flag", actor or SYSTEM_ACTOR, "Fraud signal on a license",
                {"key": "..." + key[-4:], "machines_30d": signals.get("machines_30d"),
                 "resets_30d": signals.get("resets_30d")})
    return True

# ── §2 license view ───────────────────────────────────────────────────────────
def license_view(item, role):
    """Support sees the machine id masked to its last 6 chars; owner/admin see the
    full binding. Everything else is identical."""
    key = str(item.get("license_key", ""))
    machine = str(item.get("machine_id", "") or "")
    role = normalize_role(role)
    if role == ROLE_SUPPORT:
        machine_out = ("..." + machine[-6:]) if machine else ""
    else:
        machine_out = machine
    st = reset_state(item)
    return {
        "license_key_suffix": key[-4:],
        "license_key": key if role in (ROLE_OWNER, ROLE_ADMIN) else None,
        "status":      item.get("status"),
        "revoked":     bool(item.get("revoked", False)),
        "plan":        item.get("plan"),
        "expiry":      int(item.get("expiry", 0) or 0),
        "frozen_at":   int(item.get("frozen_at", 0) or 0),
        "activations": int(item.get("activations", 0) or 0),
        "max_devices": int(item.get("max_devices", 1) or 1),
        "email":       item.get("email", ""),
        "discord_user_id": item.get("discord_user_id", ""),
        "machine_id":  machine_out,
        "machine_suffix": machine[-4:] if machine else None,
        "last_check_at": int(item.get("last_check_at", 0) or 0),
        "client_version": item.get("client_version", ""),
        "reset_policy": {"free_resets": st["free_resets"], "resets_used": st["resets_used"],
                         "paid_credits": st["paid_credits"], "locked": st["locked"],
                         "last_reset_at": st["last_reset_at"]},
        "reset_history": _from_ddb(item.get("reset_history") or []),
        "machine_history": _from_ddb(item.get("machine_history") or []),
        "flags":       _from_ddb(item.get("flags") or {}),
        "note":        item.get("note", item.get("notes", "")),
        "source":      item.get("source", ""),
    }

# ── §2 license mutations, shared by /api/admin/license and /api/staff/license ──
def mint_license(plan, days, actor, discord_user_id="", email="", note="",
                 max_devices=1, source="admin_create"):
    now = now_ts()
    key = gen_license_key()
    plan = (plan or "month").strip().lower()
    if days is None:
        days = TIER_DAYS.get(plan, 30)
    expiry = 0 if plan == "lifetime" else now + int(days) * 86400
    item = {
        "license_key": key, "status": "active", "revoked": False, "plan": plan,
        "machine_id": "", "expiry": expiry, "activations": 0,
        "max_devices": int(max_devices or 1), "created_at": now,
        "source": source, "created_by": f"{actor.get('type')}:{actor.get('id')}",
        "hwid_resets_used": 0, "hwid_paid_credits": 0, "hwid_reset_locked": False,
    }
    if discord_user_id:
        item["discord_user_id"] = str(discord_user_id)
    if email:
        item["email"] = str(email).strip().lower()
    if note:
        item["note"] = str(note)[:500]
    licenses_table().put_item(Item=item)
    return item

def handle_license_action(event, actor, staff=None, surface="admin"):
    """ONE implementation for the owner panel and the staff panel; the capability
    matrix (not the route) decides what an actor may do."""
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    action = (body.get("action") or "").strip().lower()
    reason = str(body.get("reason") or "")[:REASON_MAX]
    ip = _client_ip(event)

    if not action:
        return err("missing_action", 400, message="action is required.")

    # ── create: the only action that does not start from an existing row ──────
    if action == "create":
        e = require_capability(actor, "license.create")
        if e:
            return e
        if not reason:
            return err("reason_required", 400, message="Every mutation needs a reason.")
        try:
            count = int(body.get("count", 1) or 1)
        except (TypeError, ValueError):
            return err("invalid_count", 400)
        if count < 1 or count > LICENSE_CREATE_MAX:
            return err("invalid_count", 400,
                       message=f"count must be 1..{LICENSE_CREATE_MAX}.")
        cap_err = check_cap(actor, staff, "keys", "keys_per_day", want=count)
        if cap_err:
            return cap_err
        plan = str(body.get("plan", "month")).strip().lower()
        days = body.get("days")
        try:
            days = None if days in (None, "") else int(days)
        except (TypeError, ValueError):
            return err("invalid_days", 400)
        audit_attempt(actor, "license.create", target_type="license", reason=reason, ip=ip,
                      details={"plan": plan, "days": days, "count": count})
        keys = []
        for _ in range(count):
            row = mint_license(plan, days, actor,
                               discord_user_id=body.get("discord_user_id", ""),
                               email=body.get("email", ""),
                               note=body.get("note", ""),
                               max_devices=body.get("max_devices", 1))
            keys.append({"license_key": row["license_key"], "expiry": row["expiry"]})
            audit(actor, "license.create", target=row["license_key"][-4:],
                  target_type="license", reason=reason, ip=ip,
                  details={"plan": plan, "days": days, "count": count})
        staff_consume(actor.get("id") if staff else None, "keys", count)
        if count > 5 or normalize_role(actor.get("role")) == ROLE_ADMIN:
            owner_alert("license.create", actor, "Licenses minted",
                        {"count": count, "plan": plan, "reason": reason})
        return ok({"ok": True, "keys": keys, "count": len(keys), "plan": plan,
                   "license_key": keys[0]["license_key"] if keys else None}, 201)

    # ── blacklist targets an identity, not a licence row ─────────────────────
    if action in ("blacklist", "unblacklist"):
        e = require_capability(actor, "license.blacklist")
        if e:
            return e
        if not reason:
            return err("reason_required", 400, message="Every mutation needs a reason.")
        audit_attempt(actor, f"license.{action}", target_type="global", reason=reason, ip=ip,
                      details={"machine_suffix": str(body.get("machine_id", "") or "")[-6:],
                               "discord_user_id": str(body.get("discord_user_id", "") or "")})
        return _blacklist_mutate(actor, body, reason, add=(action == "blacklist"))

    # ── every other action resolves a license first ───────────────────────────
    item, lookup_mode, lerr = resolve_license(body)
    if lerr == "missing_license_key":
        return err("missing_license_key", 400,
                   message="Supply key, discord_user_id, email or machine_id.")
    if lerr == "internal_error":
        return err("internal_error", 500)
    if lerr == "entitlement_unavailable":
        return err("entitlement_unavailable", 503)
    if lerr or not item:
        return err("invalid_key", 404, message="No license matched.", lookup_mode=lookup_mode)
    key = item["license_key"]

    if action == "lookup":
        e = require_capability(actor, "license.lookup")
        if e:
            return e
        audit(actor, "license.lookup", target=key[-4:], target_type="license",
              reason=reason, ip=ip, details={"lookup_mode": lookup_mode})
        view = license_view(item, actor.get("role"))
        return ok({"ok": True, "license": view, "lookup_mode": lookup_mode,
                   "reset_history": view["reset_history"], "flags": view["flags"]})

    # Mutations from here on: reason mandatory.
    if not reason:
        return err("reason_required", 400, message="Every mutation needs a reason.")
    # [CX-014] durable attempt row BEFORE the mutation; refuse if it cannot be written.
    refused = audit_attempt_or_refuse(actor, f"license.{action}.attempt", key[-4:], "license", reason, ip=ip)
    if refused:
        return refused

    if action in ("revoke", "unrevoke"):
        e = require_capability(actor, f"license.{action}")
        if e:
            return e
        revoked = action == "revoke"
        licenses_table().update_item(
            Key={"license_key": key},
            UpdateExpression=("SET #s = :s, revoked = :r, revoke_reason = :k, revoked_at = :t"
                              if revoked else
                              "SET #s = :s, revoked = :r REMOVE revoke_reason, kill_reason"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=({":s": "revoked", ":r": True, ":k": reason, ":t": now_ts()}
                                       if revoked else {":s": "active", ":r": False}))
        audit(actor, f"license.{action}", target=key[-4:], target_type="license",
              reason=reason, ip=ip)
        if revoked:
            owner_alert("license.revoke", actor, "License revoked",
                        {"key": "..." + key[-4:], "reason": reason})
        return ok({"ok": True, "license_key_suffix": key[-4:], "action": action})

    if action in ("freeze", "unfreeze"):
        e = require_capability(actor, f"license.{action}")
        if e:
            return e
        now = now_ts()
        try:
            expiry = int(item.get("expiry", 0) or 0)
        except (TypeError, ValueError):
            expiry = 0
        if action == "freeze":
            if item.get("status") == "frozen":
                return err("already_frozen", 409, message="Already frozen.")
            licenses_table().update_item(
                Key={"license_key": key},
                UpdateExpression="SET #s = :s, frozen_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "frozen", ":t": now})
            audit(actor, "license.freeze", target=key[-4:], target_type="license",
                  reason=reason, ip=ip)
            return ok({"ok": True, "status": "frozen", "frozen_at": now})
        frozen_at = int(item.get("frozen_at", 0) or 0)
        # The clock stops while frozen: give back exactly the frozen interval.
        new_expiry = expiry + (now - frozen_at) if (expiry > 0 and frozen_at) else expiry
        licenses_table().update_item(
            Key={"license_key": key},
            UpdateExpression="SET #s = :s, expiry = :e REMOVE frozen_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "active", ":e": new_expiry})
        audit(actor, "license.unfreeze", target=key[-4:], target_type="license",
              reason=reason, ip=ip, details={"expiry": new_expiry,
                                             "frozen_s": (now - frozen_at) if frozen_at else 0})
        return ok({"ok": True, "status": "active", "expiry": new_expiry})

    if action == "extend":
        e = require_capability(actor, "license.extend")
        if e:
            return e
        try:
            days = int(body.get("days", 0) or 0)
        except (TypeError, ValueError):
            return err("invalid_days", 400)
        if days == 0:
            return err("invalid_days", 400, message="days must be non-zero.")
        if staff and normalize_role(actor.get("role")) != ROLE_OWNER:
            max_days = int(staff_caps(staff).get("extend_max_days", 0))
            if abs(days) > max_days:
                audit(actor, "cap.exceeded", target=key[-4:], target_type="license",
                      result="cap_exceeded", details={"cap": "extend_max_days",
                                                      "days": days, "limit": max_days})
                return err("cap_exceeded", 429,
                           message=f"Your cap allows at most {max_days} days per extend.",
                           cap="extend_max_days", limit=max_days)
        try:
            expiry = int(item.get("expiry", 0) or 0)
        except (TypeError, ValueError):
            expiry = 0
        if expiry == 0:
            return err("lifetime_key", 400, message="A lifetime key has no expiry to extend.")
        new_expiry = max(now_ts(), expiry + days * 86400)
        licenses_table().update_item(
            Key={"license_key": key},
            UpdateExpression="SET expiry = :e",
            ExpressionAttributeValues={":e": new_expiry})
        audit(actor, "license.extend", target=key[-4:], target_type="license",
              reason=reason, ip=ip, details={"days": days, "expiry": new_expiry})
        return ok({"ok": True, "expiry": new_expiry, "days": days})

    if action == "set_plan":
        e = require_capability(actor, "license.set_plan")
        if e:
            return e
        plan = str(body.get("plan", "")).strip().lower()
        if not plan:
            return err("missing_plan", 400)
        upd = {"expr": "SET #p = :p", "vals": {":p": plan}}
        if plan == "lifetime":
            upd["expr"] += ", expiry = :x"
            upd["vals"][":x"] = 0
        licenses_table().update_item(
            Key={"license_key": key}, UpdateExpression=upd["expr"],
            ExpressionAttributeNames={"#p": "plan"},
            ExpressionAttributeValues=upd["vals"])
        audit(actor, "license.set_plan", target=key[-4:], target_type="license",
              reason=reason, ip=ip, details={"plan": plan})
        owner_alert("license.set_plan", actor, "Plan changed",
                    {"key": "..." + key[-4:], "plan": plan, "reason": reason})
        return ok({"ok": True, "plan": plan})

    if action == "transfer":
        e = require_capability(actor, "license.transfer")
        if e:
            return e
        new_discord = str(body.get("discord_user_id", "") or "").strip()
        new_email = str(body.get("email", "") or "").strip().lower()
        if not new_discord and not new_email:
            return err("missing_target", 400,
                       message="transfer needs discord_user_id or email.")
        expr = "SET machine_id = :e, activations = :z"
        vals = {":e": "", ":z": 0}
        if new_discord:
            expr += ", discord_user_id = :d"
            vals[":d"] = new_discord
        if new_email:
            expr += ", email = :m"
            vals[":m"] = new_email
        # Counters travel WITH the key (contract §2): reset counters untouched.
        licenses_table().update_item(Key={"license_key": key},
                                     UpdateExpression=expr,
                                     ExpressionAttributeValues=vals)
        audit(actor, "license.transfer", target=key[-4:], target_type="license",
              reason=reason, ip=ip,
              details={"discord_user_id": new_discord, "email": new_email})
        owner_alert("license.transfer", actor, "License transferred",
                    {"key": "..." + key[-4:], "to": new_discord or new_email,
                     "reason": reason})
        return ok({"ok": True, "transferred": True})

    if action == "set_reset_policy":
        e = require_capability(actor, "license.set_reset_policy")
        if e:
            return e
        expr_parts, vals = [], {}
        if body.get("free_resets") is not None:
            expr_parts.append("hwid_free_resets = :f")
            vals[":f"] = int(body["free_resets"])
        if body.get("penalty_days") is not None:
            expr_parts.append("hwid_penalty_days = :p")
            vals[":p"] = int(body["penalty_days"])
        if body.get("locked") is not None:
            expr_parts.append("hwid_reset_locked = :l")
            vals[":l"] = bool(body["locked"])
        if body.get("paid_credits") is not None:
            expr_parts.append("hwid_paid_credits = :c")
            vals[":c"] = max(0, int(body["paid_credits"]))
        if not expr_parts:
            return err("nothing_to_set", 400)
        licenses_table().update_item(Key={"license_key": key},
                                     UpdateExpression="SET " + ", ".join(expr_parts),
                                     ExpressionAttributeValues=vals)
        audit(actor, "license.set_reset_policy", target=key[-4:], target_type="license",
              reason=reason, ip=ip, details={k: v for k, v in body.items()
                                             if k in ("free_resets", "penalty_days",
                                                      "locked", "paid_credits")})
        fresh = licenses_table().get_item(Key={"license_key": key}).get("Item") or item
        return ok({"ok": True, "reset_policy": license_view(fresh, actor.get("role"))["reset_policy"]})

    if action == "reset_machine":
        e = require_capability(actor, "license.reset_machine")
        if e:
            return e
        force = bool(body.get("force", False))
        if force:
            e = require_capability(actor, "license.reset_machine.force")
            if e:
                return e
        st = reset_state(item)
        if not force:
            if st["locked"]:
                return err("locked", 409, message="Resets are locked on this license.")
            if st["last_reset_at"] and (now_ts() - st["last_reset_at"]) < st["cooldown_s"]:
                retry_at = st["last_reset_at"] + st["cooldown_s"]
                return err("cooldown", 429, message="Reset cooldown active.",
                           retry_at=retry_at)
        cap_err = check_cap(actor, staff, "resets", "resets_per_day")
        if cap_err:
            return cap_err
        # Staff resets never consume the CUSTOMER's free count (contract §3).
        new_item = apply_hwid_reset(item, actor, "force" if force else "staff", reason)
        staff_consume(actor.get("id") if staff else None, "resets", 1)
        if staff:
            fresh_staff = staff_table().get_item(
                Key={"staff_id": actor.get("id")}).get("Item") or {}
            if staff_usage_today(fresh_staff).get("resets", 0) > 5:
                owner_alert("license.reset_machine", actor,
                            "Staff member exceeded 5 resets today",
                            {"staff_id": actor.get("id"),
                             "resets_today": staff_usage_today(fresh_staff).get("resets"),
                             "key": "..." + key[-4:]})
        return ok({"ok": True, "reset": True, "mode": "force" if force else "staff",
                   "license": license_view(new_item, actor.get("role"))})

    return err("unknown_action", 400, message=f"Unknown action: {action}")

def _blacklist_mutate(actor, body, reason, add=True):
    machine_id = str(body.get("machine_id", "") or "").strip()
    discord_id = str(body.get("discord_user_id", "") or "").strip()
    if not machine_id and not discord_id:
        return err("missing_target", 400,
                   message="blacklist needs machine_id or discord_user_id.")
    touched = []
    for kind, val in (("machine", machine_id), ("discord", discord_id)):
        if not val:
            continue
        row_key = _blacklist_key(kind, val)
        if add:
            licenses_table().put_item(Item={
                "license_key": row_key, "status": "blacklist", "revoked": True,
                "blacklist_kind": kind, "blacklist_value": val,
                "reason": reason, "created_at": now_ts(),
                "created_by": f"{actor.get('type')}:{actor.get('id')}"})
        else:
            licenses_table().delete_item(Key={"license_key": row_key})
        touched.append(row_key)
        audit(actor, "blacklist.add" if add else "blacklist.remove",
              target=val[-6:], target_type="global", reason=reason,
              details={"kind": kind})
    owner_alert("blacklist.add" if add else "blacklist.remove", actor,
                "Blacklist changed", {"entries": ", ".join(touched), "reason": reason})
    return ok({"ok": True, "blacklisted": add, "entries": touched})

# ── POST /api/admin/license (owner) ───────────────────────────────────────────
def handle_admin_license(event):
    actor = admin_actor(event)
    if not actor:
        return err("forbidden", 403)
    return handle_license_action(event, actor, staff=None, surface="admin")

# ── POST /api/staff/license (staff, capability-gated) ─────────────────────────
def handle_staff_license_v2(event):
    staff, actor, e = resolve_staff_actor(event)
    if e:
        return e
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    action = (body.get("action") or "").strip().lower()
    # Back-compat: the shipped OrionStaff build posts {action:lookup, license_key}
    # and expects 404 invalid_key + {"license": {...}}. handle_license_action keeps
    # that shape; only the missing-identifier error text is special-cased here.
    if not body.get("key") and not body.get("license_key") and action != "create" and \
            not any(body.get(f) for f in ("discord_user_id", "email", "machine_id")):
        return err("missing_license_key")
    return handle_license_action(event, actor, staff=staff, surface="staff")

# ── §2 staff management: POST /api/admin/staff {action,...} ───────────────────
def _hash_enroll_key(salt, key):
    return hashlib.sha256((salt + ":" + key).encode()).hexdigest()

def _issue_enroll_key(staff_id):
    """(enroll_key, salt, hash). Shown ONCE; only the salted hash is stored."""
    key = secrets.token_urlsafe(24)
    salt = secrets.token_hex(8)
    return key, salt, _hash_enroll_key(salt, key)

def _revoke_staff_tokens(staff_id):
    """Delete every live token bound to this staff_id (reset_machine / disable)."""
    n = 0
    try:
        scan_kw = {"FilterExpression": "staff_id = :s",
                   "ExpressionAttributeValues": {":s": staff_id}}
        while True:
            r = tokens_table().scan(**scan_kw)
            for t in r.get("Items", []):
                tokens_table().delete_item(Key={"token_id": t["token_id"]})
                n += 1
            if not r.get("LastEvaluatedKey"):
                break
            scan_kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    except Exception as e:
        print(f"[STAFF] token revoke failed {staff_id}: {e}")
    return n

def staff_view(s):
    return {
        "staff_id": s.get("staff_id"),
        "display_name": s.get("display_name", ""),
        "discord_user_id": s.get("discord_user_id", ""),
        "role": normalize_role(s.get("role")),
        "role_raw": s.get("role", ""),
        "disabled": bool(s.get("disabled", False)),
        # Never emit a staff machine id: the list is a privacy boundary, and a
        # leaked binding is half of an impersonation.
        "machine_bound": bool(s.get("machine_id", "")),
        "machine_suffix": str(s.get("machine_id", "") or "")[-6:],
        "caps": staff_caps(s),
        "usage": staff_usage_today(s),
        "pending_enrollment": bool(s.get("enroll_key_hash")) and not s.get("enrolled_at"),
        "created_by": s.get("created_by", ""),
        "created_at": s.get("created_at"),
        "last_login_at": s.get("last_login_at"),
    }

def handle_admin_staff_post(event):
    actor = admin_actor(event)
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "staff.manage")
    if e:
        return e
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    action = (body.get("action") or "list").strip().lower()
    reason = str(body.get("reason") or "")[:REASON_MAX]
    ip = _client_ip(event)

    if action == "list":
        return handle_admin_staff(event)

    if action == "create":
        if not reason:
            return err("reason_required", 400, message="Every mutation needs a reason.")
        role = normalize_role(body.get("role"))
        if str(body.get("role", "")).strip().lower() not in (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT):
            return err("invalid_role", 400, message="role must be owner, admin or support.")
        discord_user_id = str(body.get("discord_user_id", "") or "").strip()
        staff_id = str(body.get("staff_id", "") or "").strip() or ("staff_" + gen_ulid()[-10:].lower())
        if len(staff_id) > 64:
            return err("field_too_long", 400)
        try:
            if staff_table().get_item(Key={"staff_id": staff_id}).get("Item"):
                return err("staff_id_taken", 409)
        except Exception:
            return err("internal_error", 500)
        caps = dict(DEFAULT_CAPS)
        for k, v in (body.get("caps") or {}).items():
            if k in DEFAULT_CAPS:
                caps[k] = int(v)
        # [rc1 RT-CRIT-01] minting another OWNER is an owner-takeover path: step-up.
        # [rc1 RT-HIGH-07] every staff mutation writes a REQUIRED attempt row first.
        if role == ROLE_OWNER:
            attempt_id, denied = owner_step_up(event, actor, "staff.create", body=body,
                                               target=staff_id, target_type="staff",
                                               reason=reason, details={"role": role})
            if denied:
                return denied
        else:
            attempt_id = audit_attempt(actor, "staff.create", target=staff_id,
                                       target_type="staff", reason=reason, ip=ip,
                                       details={"role": role})
        enroll_key, salt, khash = _issue_enroll_key(staff_id)
        staff_item = {
            "staff_id": staff_id,
            "display_name": str(body.get("display_name", "") or staff_id)[:64],
            "role": role,
            "disabled": False,
            "machine_id": "",
            "enroll_key_hash": khash,
            "enroll_salt": salt,
            "caps": caps,
            "usage": {"day": day_str(), "keys": 0, "resets": 0},
            "created_by": f"{actor.get('type')}:{actor.get('id')}",
            "created_at": now_ts(),
        }
        # An EMPTY string is not a valid DynamoDB GSI key (discord_user_id-index):
        # omit the attribute instead of failing the whole create.
        if discord_user_id:
            staff_item["discord_user_id"] = discord_user_id
        staff_table().put_item(Item=staff_item)
        audit_complete(attempt_id, actor, "staff.create", target=staff_id, target_type="staff",
                       reason=reason, ip=ip, details={"role": role, "caps": caps,
                                                      "discord_user_id": discord_user_id})
        owner_alert("staff.create", actor, "Staff member created",
                    {"staff_id": staff_id, "role": role, "reason": reason})
        # enroll_key is returned ONCE and never stored in the clear.
        return ok({"ok": True, "staff_id": staff_id, "enroll_key": enroll_key,
                   "role": role, "caps": caps}, 201)

    staff_id = str(body.get("staff_id", "") or "").strip()
    if not staff_id:
        return err("missing_staff_id", 400)
    try:
        target = staff_table().get_item(Key={"staff_id": staff_id}).get("Item")
    except Exception:
        return err("internal_error", 500)
    if not target:
        return err("staff_not_found", 404)
    if not reason:
        return err("reason_required", 400, message="Every mutation needs a reason.")
    if action not in ("disable", "enable", "set_role", "set_caps", "reset_machine",
                      "reissue_enrollment"):
        return err("unknown_action", 400, message=f"Unknown action: {action}")
    # [rc1 RT-CRIT-01] anything that touches an OWNER row, or grants the owner role,
    # needs the owner step-up; [RT-HIGH-07] everything else a REQUIRED attempt row.
    target_is_owner = normalize_role(target.get("role")) == ROLE_OWNER
    grants_owner = (action == "set_role"
                    and str(body.get("role", "")).strip().lower() == ROLE_OWNER)
    if target_is_owner or grants_owner:
        attempt_id, denied = owner_step_up(event, actor, f"staff.{action}", body=body,
                                           target=staff_id, target_type="staff",
                                           reason=reason,
                                           details={"role": str(body.get("role", "") or "")})
        if denied:
            return denied
    else:
        attempt_id = audit_attempt(actor, f"staff.{action}", target=staff_id,
                                   target_type="staff", reason=reason, ip=ip,
                                   details={"role": str(body.get("role", "") or "")})

    if action in ("disable", "enable"):
        disabled = action == "disable"
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET disabled = :d",
            ExpressionAttributeValues={":d": disabled})
        if disabled:
            _revoke_staff_tokens(staff_id)
        audit_complete(attempt_id, actor, f"staff.{action}", target=staff_id,
                       target_type="staff", reason=reason, ip=ip)
        if disabled:
            owner_alert("staff.disable", actor, "Staff member disabled",
                        {"staff_id": staff_id, "reason": reason})
        return ok({"ok": True, "staff_id": staff_id, "disabled": disabled})

    if action == "set_role":
        role_raw = str(body.get("role", "")).strip().lower()
        if role_raw not in (ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT):
            audit_complete(attempt_id, actor, "staff.set_role", target=staff_id,
                           target_type="staff", reason=reason, ip=ip, result="invalid_role")
            return err("invalid_role", 400, message="role must be owner, admin or support.")
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET #r = :r",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":r": role_raw})
        audit_complete(attempt_id, actor, "staff.set_role", target=staff_id,
                       target_type="staff", reason=reason, ip=ip, details={"role": role_raw})
        owner_alert("staff.set_role", actor, "Staff role changed",
                    {"staff_id": staff_id, "role": role_raw, "reason": reason})
        return ok({"ok": True, "staff_id": staff_id, "role": role_raw})

    if action == "set_caps":
        caps = staff_caps(target)
        for k, v in (body.get("caps") or {}).items():
            if k in DEFAULT_CAPS:
                try:
                    caps[k] = int(v)
                except (TypeError, ValueError):
                    return err("invalid_caps", 400)
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET caps = :c",
            ExpressionAttributeValues={":c": caps})
        audit_complete(attempt_id, actor, "staff.set_caps", target=staff_id,
                       target_type="staff", reason=reason, ip=ip, details={"caps": caps})
        return ok({"ok": True, "staff_id": staff_id, "caps": caps})

    if action == "reset_machine":
        n = _revoke_staff_tokens(staff_id)
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET machine_id = :e",
            ExpressionAttributeValues={":e": ""})
        audit_complete(attempt_id, actor, "staff.reset_machine", target=staff_id,
                       target_type="staff", reason=reason, ip=ip, details={"tokens_revoked": n})
        return ok({"ok": True, "staff_id": staff_id, "tokens_revoked": n})

    if action == "reissue_enrollment":
        enroll_key, salt, khash = _issue_enroll_key(staff_id)
        staff_table().update_item(
            Key={"staff_id": staff_id},
            UpdateExpression="SET enroll_key_hash = :h, enroll_salt = :s REMOVE enrolled_at",
            ExpressionAttributeValues={":h": khash, ":s": salt})
        audit_complete(attempt_id, actor, "staff.reissue_enrollment", target=staff_id,
                       target_type="staff", reason=reason, ip=ip)
        return ok({"ok": True, "staff_id": staff_id, "enroll_key": enroll_key})

    return err("unknown_action", 400, message=f"Unknown action: {action}")

# ── §4 audit read: GET /api/admin/audit and GET /api/staff/audit ──────────────
def _audit_row_view(row):
    return {
        "audit_id":    row.get("audit_id") or row.get("event_id"),
        "ts":          int(row.get("ts", 0) or 0),
        "day":         row.get("day", ""),
        "actor_type":  row.get("actor_type", ""),
        "actor_id":    row.get("actor_id", ""),
        "role":        row.get("role", ""),
        "action":      row.get("action") or row.get("event_type", ""),
        "target_type": row.get("target_type", ""),
        "target":      row.get("target", ""),
        "reason":      row.get("reason", ""),
        "result":      row.get("result", "ok"),
        "ip":          row.get("ip", ""),
        "details":     _from_ddb(row.get("details") or {}),
    }

def _encode_cursor(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

def _decode_cursor(cur):
    try:
        pad = "=" * (-len(cur) % 4)
        return json.loads(base64.urlsafe_b64decode(cur + pad).decode())
    except Exception:
        return None

def read_audit(since=None, until=None, actor=None, action=None, target=None,
               limit=50, cursor=None):
    """Walks day partitions newest-first on day-ts-index; falls back to a scan when
    the GSI is absent (`index` in the response says which path served the read)."""
    limit = max(1, min(int(limit or 50), AUDIT_PAGE_MAX))
    now = now_ts()
    until_ts = int(until) if until else now
    since_ts = int(since) if since else (until_ts - 7 * 86400)
    cur = _decode_cursor(cursor) if cursor else None
    start_day = cur.get("day") if cur else day_str(until_ts)
    start_ts = cur.get("ts") if cur else None

    def _match(r):
        if actor and str(r.get("actor_id", "")) != str(actor):
            return False
        if action and str(r.get("action") or r.get("event_type", "")) != str(action):
            return False
        if target and str(target) not in str(r.get("target", "")):
            return False
        t = int(r.get("ts", 0) or 0)
        return since_ts <= t <= until_ts

    rows, index_used, next_cursor = [], "day-ts-index", None
    try:
        day_cursor = start_day
        walked = 0
        while len(rows) < limit and walked < AUDIT_DAY_SCAN_MAX:
            if day_cursor < day_str(since_ts):
                break
            kw = {
                "IndexName": AUDIT_DAY_GSI,
                "KeyConditionExpression": "#d = :d" + (" AND #t < :t" if start_ts else ""),
                "ExpressionAttributeNames": {"#d": "day"},
                "ExpressionAttributeValues": {":d": day_cursor},
                "ScanIndexForward": False,
                "Limit": limit * 4,
            }
            if start_ts:
                kw["ExpressionAttributeNames"]["#t"] = "ts"
                kw["ExpressionAttributeValues"][":t"] = int(start_ts)
            r = audit_table().query(**kw)
            for item in r.get("Items", []):
                if _match(item):
                    rows.append(item)
                    if len(rows) >= limit:
                        next_cursor = _encode_cursor({"day": item.get("day"),
                                                      "ts": int(item.get("ts", 0) or 0)})
                        break
            start_ts = None
            # step one UTC day back
            y, m, d = (int(x) for x in day_cursor.split("-"))
            prev = time.gmtime(time.mktime((y, m, d, 12, 0, 0, 0, 0, 0)) - 86400)
            day_cursor = time.strftime("%Y-%m-%d", prev)
            walked += 1
    except Exception as e:
        print(f"[AUDIT] GSI read unavailable, falling back to scan: {e}")
        index_used = "scan"
        rows = []
        try:
            scanned = audit_table().scan(Limit=2000).get("Items", [])
            rows = [r for r in scanned if _match(r)]
            rows.sort(key=lambda r: int(r.get("ts", 0) or 0), reverse=True)
            rows = rows[:limit]
        except Exception as e2:
            print(f"[AUDIT] scan failed: {e2}")
    return [_audit_row_view(r) for r in rows], next_cursor, index_used

def handle_admin_audit(event):
    actor = admin_actor(event)
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "audit.read_all")
    if e:
        return e
    qs = event.get("queryStringParameters") or {}
    rows, cursor, index_used = read_audit(
        since=qs.get("since"), until=qs.get("until"), actor=qs.get("actor"),
        action=qs.get("action"), target=qs.get("target"),
        limit=qs.get("limit", 50), cursor=qs.get("cursor"))
    return ok({"ok": True, "audit": rows, "count": len(rows),
               "cursor": cursor, "index": index_used})

def handle_staff_audit_read(event):
    staff, actor, e = resolve_staff_actor(event)
    if e:
        return e
    cap_err = require_capability(actor, "audit.read_own")
    if cap_err:
        return cap_err
    qs = event.get("queryStringParameters") or {}
    rows, cursor, index_used = read_audit(
        since=qs.get("since"), until=qs.get("until"),
        actor=staff["staff_id"],            # own rows ONLY — not caller-controlled
        action=qs.get("action"), limit=qs.get("limit", 50), cursor=qs.get("cursor"))
    return ok({"ok": True, "audit": rows, "count": len(rows),
               "cursor": cursor, "index": index_used,
               "staff_id": staff["staff_id"],
               "caps": staff_caps(staff), "usage": staff_usage_today(staff)})

# ── §5 config: GET/POST /api/admin/config ─────────────────────────────────────
def _config_snapshot():
    gkill, greason = get_global_kill()
    snap = {"global_kill": {"enabled": bool(gkill), "reason": greason}}
    for k in CONFIG_DEFAULTS:
        snap[k] = config_get(k)
    snap["owner_totp_enrolled"] = bool(owner_totp_secret())
    snap["owner_totp_pending"] = bool(owner_totp_pending_secret())
    return snap

def handle_admin_config(event):
    actor = admin_actor(event)
    if not actor:
        return err("forbidden", 403)
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    if method == "GET":
        e = require_capability(actor, "config.read")
        if e:
            return e
        return ok({"ok": True, "config": _config_snapshot()})

    e = require_capability(actor, "config.write")
    if e:
        return e
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    action = (body.get("action") or "set").strip().lower()
    reason = str(body.get("reason") or "")[:REASON_MAX]
    ip = _client_ip(event)

    if action == "totp_enroll":
        # Re-enrolling OVERWRITES the secret: an attacker who could do this owns the
        # second factor. Step-up with the CURRENT factor (or break-glass pre-TOTP).
        attempt_id, denied = owner_step_up(event, actor, "config.totp_enroll", body=body,
                                           target="owner_totp", reason=reason or "totp enrolment")
        if denied:
            return denied
        secret = gen_totp_secret()
        try:
            # Staged, not active: see OWNER_TOTP_PENDING_SSM.
            get_ssm().put_parameter(Name=OWNER_TOTP_PENDING_SSM, Value=secret,
                                    Type="SecureString", Overwrite=True)
        except Exception as e2:
            print(f"[TOTP] enroll write failed: {e2}")
            audit_complete(attempt_id, actor, "config.totp_enroll", target="owner_totp",
                           target_type="config", reason=reason, ip=ip, result="ssm_write_failed")
            return err("ssm_write_failed", 500)
        audit_complete(attempt_id, actor, "config.totp_enroll", target="owner_totp",
                       target_type="config", reason=reason or "totp enrolment", ip=ip)
        uri = (f"otpauth://totp/{TOTP_ISSUER}:owner?secret={secret}"
               f"&issuer={TOTP_ISSUER}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_S}")
        # Returned ONCE — the secret is never readable through the API again.
        return ok({"ok": True, "otpauth_uri": uri, "secret": secret, "pending": True,
                   "required": bool(config_get_strict("owner_totp_required"))})

    if action == "totp_confirm":
        # [P-F follow-up] The body code IS the proof for this action: it proves
        # possession of the staged secret (whose enrolment already needed the
        # current factor) or, with nothing staged, of the active secret itself.
        # There is no separate step-up burn that could collide with it.
        pending = owner_totp_pending_secret()
        target = pending or owner_totp_secret()
        if not target:
            return err("totp_not_enrolled", 400, message="Run totp_enroll first.")
        confirm_counter = totp_match_counter(target, body.get("code"))
        if confirm_counter is None:
            audit(actor, "owner.step_up_denied", target="config.totp_confirm",
                  target_type="config", result="invalid_totp", ip=ip,
                  details={"action": "config.totp_confirm", "pending": bool(pending)})
            return err("invalid_totp", 403, message="That code did not verify.")
        attempt_id = audit_attempt(actor, "config.totp_confirm", target="owner_totp",
                                   target_type="config", reason=reason or "totp enabled",
                                   ip=ip, details={"pending": bool(pending)})
        try:
            fresh = totp_consume(confirm_counter, target)
        except Exception as e2:
            print(f"[TOTP] replay store unavailable: {e2}")
            return err("step_up_unavailable", 503)
        if not fresh:
            audit(actor, "owner.step_up_denied", target="config.totp_confirm",
                  target_type="config", result="totp_replayed", ip=ip)
            return err("totp_replayed", 403, message="That code was already used.")
        if pending:
            try:
                get_ssm().put_parameter(Name=OWNER_TOTP_SSM, Value=pending,
                                        Type="SecureString", Overwrite=True)
            except Exception as e2:
                print(f"[TOTP] confirm promote failed: {type(e2).__name__}")
                audit_complete(attempt_id, actor, "config.totp_confirm", target="owner_totp",
                               target_type="config", reason=reason, ip=ip,
                               result="ssm_write_failed")
                return err("ssm_write_failed", 500)
            _clear_pending_totp()
        config_set("owner_totp_required", True)
        audit_complete(attempt_id, actor, "config.totp_confirm", target="owner_totp",
                       target_type="config", reason=reason or "totp enabled", ip=ip,
                       details={"rotated": bool(pending)})
        owner_alert("config.set", actor,
                    "Owner TOTP switched to a new authenticator" if pending else "Owner TOTP enabled",
                    {"reason": reason})
        return ok({"ok": True, "owner_totp_required": True})

    if action == "totp_disable":
        attempt_id, denied = owner_step_up(event, actor, "config.totp_disable", body=body,
                                           target="owner_totp", reason=reason or "totp disabled")
        if denied:
            return denied
        config_set("owner_totp_required", False)
        _clear_pending_totp()
        audit_complete(attempt_id, actor, "config.totp_disable", target="owner_totp",
                       target_type="config", reason=reason or "totp disabled", ip=ip)
        owner_alert("config.set", actor, "Owner TOTP DISABLED", {"reason": reason})
        return ok({"ok": True, "owner_totp_required": False})

    if action == "rotate_admin_secret":
        attempt_id, denied = owner_step_up(event, actor, "config.rotate_admin_secret",
                                           body=body, target="admin_secret",
                                           reason=reason or "secret rotation")
        if denied:
            return denied
        new_secret = secrets.token_urlsafe(32)
        try:
            get_ssm().put_parameter(Name=ADMIN_SECRET_SSM, Value=new_secret,
                                    Type="SecureString", Overwrite=True)
        except Exception as e2:
            print(f"[ADMIN] secret rotate failed: {e2}")
            audit_complete(attempt_id, actor, "config.rotate_admin_secret",
                           target="admin_secret", target_type="config", reason=reason,
                           ip=ip, result="ssm_write_failed")
            return err("ssm_write_failed", 500)
        audit_complete(attempt_id, actor, "config.rotate_admin_secret", target="admin_secret",
                       target_type="config", reason=reason or "secret rotation", ip=ip)
        owner_alert("config.rotate_admin_secret", actor, "Admin secret rotated",
                    {"reason": reason})
        # Returned ONCE.
        return ok({"ok": True, "admin_secret": new_secret})

    if action != "set":
        return err("unknown_action", 400, message=f"Unknown action: {action}")

    updates = body.get("config")
    if not isinstance(updates, dict):
        updates = {k: v for k, v in body.items()
                   if k in CONFIG_KEYS}
    if not updates:
        return err("nothing_to_set", 400)
    if not reason:
        return err("reason_required", 400, message="Every mutation needs a reason.")
    for key in updates:
        if key not in CONFIG_KEYS:
            return err("unknown_config_key", 400, message=f"Unknown config key: {key}",
                       key=key)

    def _kill_enabled(value):
        return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)

    # Security-weakening keys (the second factor, the IP allowlist, where owner
    # alerts go) and RELEASING the global kill need the step-up; engaging the kill
    # stays one step (the safe direction).
    sensitive = [k for k in updates if k in STEP_UP_CONFIG_KEYS
                 or (k == "global_kill" and not _kill_enabled(updates[k]))]
    if sensitive:
        _sid, denied = owner_step_up(event, actor, "config.set.sensitive", body=body,
                                     target=",".join(sensitive), reason=reason,
                                     details={"keys": sensitive})
        if denied:
            return denied
    applied = {}
    for key, value in updates.items():
        if key == "global_kill":
            enabled = _kill_enabled(value)
            kreason = (value.get("reason") if isinstance(value, dict) else "") or reason
            attempt_id = audit_attempt(actor, "config.set", key, "config", reason, ip=ip,
                                       details={"enabled": enabled})
            config_table().put_item(Item={"config_key": "global_kill",
                                          "enabled": enabled, "reason": kreason if enabled else "",
                                          "set_at": now_ts()})
            applied[key] = {"enabled": enabled, "reason": kreason if enabled else ""}
        else:
            attempt_id = audit_attempt(actor, "config.set", key, "config", reason, ip=ip)
            config_set(key, value)
            applied[key] = value
        audit_complete(attempt_id, actor, "config.set", target=key, target_type="config",
                       reason=reason, ip=ip, details={"value": applied[key]})
    owner_alert("config.set", actor, "Config changed",
                {"keys": ", ".join(applied.keys()), "reason": reason})
    return ok({"ok": True, "applied": applied, "config": _config_snapshot()})

# ── §6 metrics: GET /api/admin/metrics ────────────────────────────────────────
def _scan_all(table, **kw):
    items, start = [], None
    while True:
        if start:
            kw["ExclusiveStartKey"] = start
        r = table.scan(**kw)
        items.extend(r.get("Items", []))
        start = r.get("LastEvaluatedKey")
        if not start:
            break
    return items

def handle_admin_metrics(event):
    actor = admin_actor(event)
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "metrics")
    if e:
        return e
    if not rate_limit_ok("admin_metrics", actor.get("id") or "owner",
                         limit=METRICS_RATE_MAX, window=60):
        return err("rate_limited", 429, message="Metrics are limited to 6 reads a minute.")
    now = now_ts()
    d1, d7, d30 = now - 86400, now - 7 * 86400, now - 30 * 86400

    lic = {"active": 0, "frozen": 0, "revoked": 0, "expired": 0, "by_plan": {}}
    trials = {"active": 0, "claimed_7d": 0, "converted_30d": 0}
    activations = {"24h": 0, "7d": 0}
    resets = {"24h": 0, "7d": 0, "paid_7d": 0, "deduct_7d": 0}
    versions, online_now, fraud_flagged = {}, 0, 0
    try:
        rows = _scan_all(licenses_table())
    except Exception as ex:
        print(f"[METRICS] license scan failed: {ex}")
        rows = []
    for r in rows:
        key = str(r.get("license_key", ""))
        if key.startswith("TRIAL#"):
            if int(r.get("created_at", 0) or 0) >= d7:
                trials["claimed_7d"] += 1
            continue
        if key.startswith(("ORDER#", "BLACKLIST#", "TRIALMACHINE#")):
            continue
        plan = str(r.get("plan", "") or "unknown")
        lic["by_plan"][plan] = lic["by_plan"].get(plan, 0) + 1
        status = str(r.get("status", ""))
        try:
            expiry = int(r.get("expiry", 0) or 0)
        except (TypeError, ValueError):
            expiry = 0
        if r.get("revoked", False) or status == "revoked":
            lic["revoked"] += 1
        elif status == "frozen":
            lic["frozen"] += 1
        elif 0 < expiry < now:
            lic["expired"] += 1
        elif status == "active":
            lic["active"] += 1
            if plan == "trial":
                trials["active"] += 1
        if plan != "trial" and int(r.get("created_at", 0) or 0) >= d30 and r.get("discord_user_id"):
            trials["converted_30d"] += 1
        last_act = int(r.get("last_activated", 0) or 0)
        if last_act >= d1:
            activations["24h"] += 1
        if last_act >= d7:
            activations["7d"] += 1
        last_check = int(r.get("last_check_at", 0) or 0)
        if last_check >= now - 900:
            online_now += 1
        cv = str(r.get("client_version", "") or "")
        if cv:
            versions[cv] = versions.get(cv, 0) + 1
        flags = _from_ddb(r.get("flags") or {})
        if isinstance(flags, dict) and flags.get("suspect"):
            fraud_flagged += 1
        for h in (_from_ddb(r.get("reset_history") or []) or []):
            ts = int(h.get("ts", 0) or 0)
            if ts >= d1:
                resets["24h"] += 1
            if ts >= d7:
                resets["7d"] += 1
                if h.get("mode") == "paid":
                    resets["paid_7d"] += 1
                elif h.get("mode") == "deduct":
                    resets["deduct_7d"] += 1

    staff_stats = {"active": 0, "actions_24h": 0}
    try:
        for s in _scan_all(staff_table()):
            if not s.get("disabled", False):
                staff_stats["active"] += 1
    except Exception as ex:
        print(f"[METRICS] staff scan failed: {ex}")
    try:
        rows24, _, _ = read_audit(since=d1, until=now, limit=AUDIT_PAGE_MAX)
        staff_stats["actions_24h"] = sum(1 for r in rows24 if r.get("actor_type") == "staff")
    except Exception as ex:
        print(f"[METRICS] audit read failed: {ex}")

    audit(actor, "metrics.read", target_type="global")
    return ok({"ok": True, "licenses": lic, "trials": trials,
               "activations": activations, "online_now": online_now,
               "resets": resets, "versions": versions, "staff": staff_stats,
               "fraud_flagged": fraud_flagged, "ts": now})

# ── §3/§6 webhook-facing helpers (Gumroad "HWID reset" product, chargebacks) ──
def handle_bot_alert_target(event):
    """GET /api/bot/alert-target — returns `alerts.owner_discord_user_id` from
    orion-config so the Gumroad/SellHub webhook Lambdas can DM the owner with the
    bot token they already hold, instead of carrying a duplicate env var.

    Chosen over a POST /api/bot/alert relay on purpose: a relay would let anything
    holding a bot secret send arbitrary DMs to the owner (a phishing channel),
    whereas this leaks only an id the bots can already see in the guild."""
    if not require_bot(event):
        return err("forbidden", 403)
    cfg = config_get("alerts") or {}
    return ok({"ok": True,
               "owner_discord_user_id": str(cfg.get("owner_discord_user_id") or ""),
               "events": cfg.get("events") or []})

def handle_bot_reset_credit(event):
    """POST /api/bot/hwid-credit (alias /api/bot/reset-credit) — grants
    hwid_paid_credits += 1 on the customer's newest active key. The Gumroad
    "HWID reset" product webhook calls this; the webhook Lambda lives in
    discord_launch/ and is owned elsewhere, so the grant itself is implemented
    HERE and the webhook only has to POST."""
    consumer = require_bot(event)
    if not consumer:
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    discord_id = str(body.get("discord_user_id") or body.get("discord_id") or "").strip()
    key_in = str(body.get("key") or body.get("license_key") or "").strip().upper()
    reason = str(body.get("reason") or "gumroad hwid-reset purchase")[:REASON_MAX]
    order_id = str(body.get("order_id", "") or "").strip()
    actor = make_actor("webhook", consumer, "webhook", _client_ip(event))
    item, mode = None, "key"
    if key_in:
        item = licenses_table().get_item(Key={"license_key": key_in}).get("Item")
    elif discord_id:
        rows, mode = find_licenses_by_discord(discord_id)
        if mode == "unavailable":
            return err("entitlement_unavailable", 503)
        item = _newest_active(rows)
    if not item:
        audit(actor, "license.grant_reset_credit", result="invalid_key",
              target_type="license", reason=reason)
        return err("invalid_key", 404, message="No active license for that customer.")
    # Resolve the target before spending the order. A secondary lookup outage
    # must not leave a paid reset-credit order permanently claimed but ungranted.
    if order_id:
        dup = _claim_order("RESETCREDIT-" + order_id, "reset_credit")
        if dup is not None:
            return ok({"ok": True, "duplicate_order": True})
    key = item["license_key"]
    credits = int(item.get("hwid_paid_credits", 0) or 0) + 1
    licenses_table().update_item(
        Key={"license_key": key},
        UpdateExpression="SET hwid_paid_credits = :c",
        ExpressionAttributeValues={":c": credits})
    audit(actor, "license.grant_reset_credit", target=key[-4:], target_type="license",
          reason=reason, details={"credits": credits, "order_id": order_id,
                                  "lookup_mode": mode})
    return ok({"ok": True, "license_key_suffix": key[-4:], "hwid_paid_credits": credits,
               "lookup_mode": mode})

# ── [2026-09-23 rc1 RT-LOW-04 / CL3-F6-002] Stripe event.id dedup ─────────────
# The website Worker forwards the verified Stripe `event.id` as `stripe_event_id`.
# A marker `stripe_evt:<id>` in orion-nonces moves processing -> done; a replayed
# delivery of a DONE event is a 200 no-op (no second revoke/audit/owner alert, no
# second DM). A failed attempt DELETES its claim so Stripe's retry reprocesses; a
# concurrent in-flight delivery gets a retriable 409. Entitlement idempotency
# (ORDER# markers, revoke state) is unchanged underneath.
STRIPE_EVENT_TTL_S = 30 * 86400          # Stripe retries for up to 3 days
STRIPE_EVENT_LEASE_S = 120               # a crashed attempt's claim goes stale after this

def _valid_stripe_event_id(value):
    v = str(value or "")
    return v.startswith("evt_") and 5 <= len(v) <= 255 and v[4:].replace("_", "").isalnum()

def stripe_event_begin(event_id):
    """None = claimed, proceed. Otherwise the response to return right away."""
    key = "stripe_evt:" + event_id
    now = now_ts()
    try:
        cur = nonces_table().get_item(Key={"nonce": key}, ConsistentRead=True).get("Item")
    except Exception as e:
        print(f"[STRIPE] event store unavailable: {e}")
        return err("event_store_unavailable", 503)
    if cur and cur.get("state") == "done":
        return ok({"ok": True, "duplicate_event": True,
                   "result": str(cur.get("result") or "")})
    try:
        nonces_table().put_item(
            Item={"nonce": key, "state": "processing", "claimed_at": now,
                  "expires": now + STRIPE_EVENT_TTL_S, "ttl": now + STRIPE_EVENT_TTL_S},
            ConditionExpression="attribute_not_exists(nonce) OR "
                                "(#st = :p AND claimed_at < :stale)",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={":p": "processing", ":stale": now - STRIPE_EVENT_LEASE_S})
        return None
    except get_ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return err("event_in_progress", 409,
                   message="This Stripe event is already being processed. Retry later.")
    except Exception as e:
        print(f"[STRIPE] event claim failed: {e}")
        return err("event_store_unavailable", 503)

def stripe_event_finish(event_id, resp):
    """Mark DONE on a 2xx, release the claim otherwise (so Stripe's retry runs)."""
    key = "stripe_evt:" + event_id
    status = int(resp.get("statusCode", 500))
    try:
        if 200 <= status < 300:
            now = now_ts()
            nonces_table().put_item(Item={"nonce": key, "state": "done", "done_at": now,
                                          "result": str(status),
                                          "expires": now + STRIPE_EVENT_TTL_S,
                                          "ttl": now + STRIPE_EVENT_TTL_S})
        else:
            nonces_table().delete_item(Key={"nonce": key})
    except Exception as e:
        # Worst case: a later replay re-runs the (idempotent) handler once more.
        print(f"[STRIPE] event marker update failed event={event_id}: {e}")
    return resp

def with_stripe_event_dedup(event, handler):
    """Wrap a bot handler with event-id dedup when the Worker supplied one."""
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return handler(event)
    event_id = body.get("stripe_event_id") if isinstance(body, dict) else None
    if event_id in (None, ""):
        return handler(event)
    if require_bot(event) != WORKER_BOT_CONSUMER:
        return err("forbidden", 403)
    if not _valid_stripe_event_id(event_id):
        return err("invalid_event_id", 400)
    early = stripe_event_begin(event_id)
    if early is not None:
        return early
    try:
        resp = handler(event)
    except Exception:
        stripe_event_finish(event_id, {"statusCode": 500})
        raise
    return stripe_event_finish(event_id, resp)

def handle_bot_chargeback(event):
    return with_stripe_event_dedup(event, _handle_bot_chargeback)

def _handle_bot_chargeback(event):
    """POST /api/bot/chargeback — §6: refunded / disputed / chargebacked revoke the
    key, audit `webhook.chargeback` and alert the owner."""
    consumer = require_bot(event)
    if not consumer:
        return err("forbidden", 403)
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid_json")
    kind = str(body.get("kind") or body.get("event") or "chargebacked").strip().lower()
    if kind not in ("refunded", "disputed", "chargebacked"):
        return err("invalid_kind", 400,
                   message="kind must be refunded, disputed or chargebacked.")
    reason = str(body.get("reason") or f"gumroad {kind}")[:REASON_MAX]
    actor = make_actor("webhook", consumer, "webhook", _client_ip(event))
    key_in = str(body.get("key") or body.get("license_key") or "").strip().upper()
    discord_id = str(body.get("discord_user_id") or body.get("discord_id") or "").strip()
    subscription_id = str(body.get("subscription_id") or "").strip()
    if subscription_id and consumer != "/orion/worker_bot_secret":
        return err("forbidden", 403)
    item, mode = None, "key"
    if key_in:
        item = licenses_table().get_item(Key={"license_key": key_in}).get("Item")
    elif discord_id:
        rows, mode = find_licenses_by_discord(discord_id)
        if mode == "unavailable":
            return err("entitlement_unavailable", 503)
        paid_rows = [row for row in rows
                     if str(row.get("source") or "").lower() == "gumroad"
                     and str(row.get("order_id") or "").strip()
                     and (not subscription_id
                          or row.get("stripe_subscription_id") == subscription_id)]
        item = _newest_active(paid_rows)
        if not item and subscription_id and any(
                bool(r.get("revoked", False)) or r.get("status") == "revoked"
                for r in paid_rows):
            # [rc1 RT-LOW-04] a replay (or a second Stripe event for the same ended
            # subscription) after the revoke already landed: same terminal state,
            # so a quiet 200 - no second audit row, no second owner alert, and no
            # 404 that would make Stripe retry for three days.
            print(f"[INFO] chargeback replay: subscription already revoked kind={kind}")
            return ok({"ok": True, "revoked": True, "already_revoked": True, "kind": kind})
    if item and subscription_id and item.get("stripe_subscription_id") != subscription_id:
        return err("subscription_mismatch", 403)
    if item and key_in and (bool(item.get("revoked", False)) or item.get("status") == "revoked"):
        return ok({"ok": True, "revoked": True, "already_revoked": True,
                   "license_key_suffix": item["license_key"][-4:], "kind": kind})
    if not item:
        audit(actor, "webhook.chargeback", result="invalid_key", target_type="license",
              reason=reason, details={"kind": kind})
        return err("invalid_key", 404)
    key = item["license_key"]
    licenses_table().update_item(
        Key={"license_key": key},
        UpdateExpression="SET #s = :s, revoked = :r, revoke_reason = :k, revoked_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "revoked", ":r": True,
                                   ":k": f"{kind}: {reason}", ":t": now_ts()})
    if discord_id:
        rows, lookup_mode = find_licenses_by_discord(discord_id)
        if lookup_mode == "unavailable":
            # The revocation already succeeded, but missing evidence about
            # another purchase must not remove the shared Customer role.
            audit_log("chargeback_role_reconcile_pending", key,
                      {"discord_id": discord_id})
            rows = None
        other_paid = rows is None or any(
            row.get("license_key") != key
            and str(row.get("source") or "").lower() == "gumroad"
            and row.get("status") == "active" and not row.get("revoked", False)
            and (int(row.get("expiry", 0) or 0) == 0
                 or int(row.get("expiry", 0) or 0) > now_ts())
            for row in rows)
        if not other_paid:
            _discord_remove_role(discord_id, DISCORD_CUSTOMER_ROLE_SSM)
    audit(actor, "webhook.chargeback", target=key[-4:], target_type="license",
          reason=reason, details={"kind": kind, "lookup_mode": mode})
    owner_alert("webhook.chargeback", actor, f"Payment {kind} — key revoked",
                {"key": "..." + key[-4:], "kind": kind, "reason": reason}, force=True)
    return ok({"ok": True, "revoked": True, "license_key_suffix": key[-4:], "kind": kind})

# ── router ────────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    """[rc1 RT-HIGH-07 / RT-CRIT-01] A REQUIRED audit write (audit_attempt) or a
    security-config read (config_get_strict) that fails raises out of the handler
    BEFORE any mutation; it is answered here as a retriable 503 so no route can
    mutate state without its durable attempt row or fall back to a permissive
    owner default."""
    try:
        return _lambda_route(event, context)
    except AuditUnavailable as e:
        print(f"[ERROR] destructive mutation refused, audit unavailable: {e}")
        return err("audit_unavailable", 503, message=AUDIT_UNAVAILABLE_MESSAGE)
    except ConfigUnavailable as e:
        print(f"[ERROR] security config unavailable key={e}")
        return err("config_unavailable", 503,
                   message="Security settings could not be read. Retry shortly.")

def _lambda_route(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path   = event.get("rawPath", "") or event.get("path", "")

    print(f"[INFO] {method} {path}")

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    # The signed customer launcher cannot carry the global edge secret: embedding
    # it would authenticate every backend route after one binary extraction. The
    # one public exception is the exact shard retrieval method; that handler
    # requires a revocable, machine-bound customer session token instead. Every
    # store/status/revoke/admin/bot route remains behind edge auth.
    public_shard_retrieve = (
        path == "/api/shard/retrieve" and method == "POST")
    if not public_shard_retrieve:
        edge_err = require_edge_auth(headers)
        if edge_err:
            return edge_err

    # CRIT-2 / MED-1: throttle + alert on the privileged admin surface.
    if path.startswith("/api/admin/"):
        if not rate_limit_ok("admin", _client_ip(event), limit=20, window=60):
            admin_alert("admin_rate_limited", event, {"path": path})
            return err("rate_limited", 429)
        # §1/§5: ONE owner gate for the whole /api/admin/* surface — break-glass
        # secret (+TOTP when required) or an owner-role staff token, behind the
        # owner IP allowlist. The resolved actor rides on the event so every
        # handler audits a real identity instead of the string "admin".
        owner_actor, owner_err = resolve_owner(event)
        if owner_err:
            return owner_err
        event["_actor"] = owner_actor

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
    elif path == "/api/bot/pair-issue" and method == "POST":
        return handle_bot_pair_issue(event)
    elif path == "/api/bot/pair-status" and method == "POST":
        return handle_bot_pair_status(event)
    elif path == "/api/bot/guild-join" and method == "POST":
        return handle_bot_guild_join(event)
    elif path == "/api/bot/guild-member" and method == "POST":
        return handle_bot_guild_member(event)
    elif path == "/api/bot/deliver" and method == "POST":
        return handle_bot_deliver(event)
    elif path == "/api/bot/provision" and method == "POST":
        return handle_bot_provision(event)
    elif path in ("/api/bot/hwid-credit", "/api/bot/reset-credit") and method == "POST":
        return handle_bot_reset_credit(event)
    elif path == "/api/bot/chargeback" and method == "POST":
        return handle_bot_chargeback(event)
    elif path == "/api/bot/alert-target" and method == "GET":
        return handle_bot_alert_target(event)
    # ── staff routes ─────────────────────────────────────────────────────────
    elif path == "/api/staff/enroll" and method == "POST":
        return handle_staff_enroll(event)
    elif path == "/api/staff/login" and method == "POST":
        return handle_staff_login(event)
    elif path == "/api/staff/whoami" and method == "GET":
        return handle_staff_whoami(event)
    elif path == "/api/staff/license" and method == "POST":
        return handle_staff_license_v2(event)
    elif path == "/api/staff/audit" and method == "GET":
        return handle_staff_audit_read(event)
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
        return handle_admin_staff_post(event)
    elif path == "/api/admin/staff/audit" and method == "GET":
        return handle_admin_staff_audit(event)
    elif path == "/api/admin/tamper-report" and method == "POST":
        return handle_admin_tamper_report(event)
    # ── Admin Panel V2 (owner) ───────────────────────────────────────────────
    elif path == "/api/admin/license" and method == "POST":
        return handle_admin_license(event)
    elif path == "/api/admin/audit" and method == "GET":
        return handle_admin_audit(event)
    elif path == "/api/admin/config" and method in ("GET", "POST"):
        return handle_admin_config(event)
    elif path == "/api/admin/metrics" and method == "GET":
        return handle_admin_metrics(event)
    # ── shard routes (OrionPack packer key fragments) ────────────────────────
    elif path == "/api/shard/store" and method == "POST":
        return handle_shard_store(event)
    elif path == "/api/shard/retrieve" and method == "POST":
        return handle_shard_retrieve(event)
    elif path == "/api/admin/shard/revoke" and method == "POST":
        return handle_shard_revoke(event)
    elif path == "/api/admin/shard/status" and method in ("GET", "POST"):
        return handle_shard_status(event)
    else:
        return err("not found", 404)

# ── GET /api/admin/staff (missing handler backfill) ──────────────────────────
def handle_admin_staff(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "staff.manage")
    if e:
        return e
    try:
        result = staff_table().scan(Limit=200)
        members = [staff_view(s) for s in result.get("Items", [])]
        return ok({"ok": True, "staff": members, "count": len(members)})
    except Exception as ex:
        print(f"[ERROR] admin staff scan: {ex}")
        return err("internal_error", 500)

# ── GET /api/admin/staff/audit (missing handler backfill) ────────────────────
def handle_admin_staff_audit(event):
    actor = admin_actor(event)      # router /api/admin/* owner gate, or direct secret
    if not actor:
        return err("forbidden", 403)
    e = require_capability(actor, "audit.read_all")
    if e:
        return e
    # Legacy view over orion-staff-audit; GET /api/admin/audit is the paged §4
    # reader over the unified orion-audit table.
    try:
        result = staff_audit_table().scan(Limit=200)
        logs = sorted(result.get("Items", []), key=lambda x: int(x.get("ts", 0)), reverse=True)
        safe = [{"audit_id": l.get("audit_id"), "action": l.get("action"),
                 "actor": l.get("actor"), "ts": l.get("ts")} for l in logs]
        return ok({"ok": True, "audit": safe})
    except Exception as ex:
        print(f"[ERROR] admin audit scan: {ex}")
        return err("internal_error", 500)
