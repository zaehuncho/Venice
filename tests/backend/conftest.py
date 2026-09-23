import json, time, importlib, sys, os, hashlib, base64
import pytest
from moto import mock_aws
import boto3
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

TEST_TOKEN_SECRET   = "test-token-secret-xxxx"
TEST_ADMIN_SECRET   = "test-admin-secret-xxxx"
TEST_BOT_SECRET     = "test-bot-secret-xxxx"
TEST_PAIR_SECRET    = "test-website-pair-secret-xxxx"
TEST_WORKER_SECRET  = "test-worker-secret-xxxx"
TEST_SELLHUB_SECRET = "test-sellhub-secret-xxxx"
TEST_EDGE_SECRET    = "test-edge-secret-xxxx"
TEST_DISCORD_PUBKEY = "a" * 64  # 32-byte hex
TEST_DISCORD_BOT    = "test-bot-token"
TEST_GUILD_ID       = "123456789"
TEST_CUSTOMER_ROLE  = "111111111"
TEST_LIFETIME_ROLE  = "222222222"
TEST_STAFF_TOKEN_SECRET = "test-staff-token-secret-xxxx"
TEST_ENROLL_KEY     = "test-enroll-key-xxxx"
TEST_SHARD_ENC_KEY  = "11" * 32

# ── Test Ed25519 keypairs (update-manifest signer + lease signer) ──────────────
# Derived from FIXED seeds (not .generate()) so that when pytest imports this
# module twice — once as the conftest plugin and once via `from conftest import`
# — both instances produce the IDENTICAL keypair. Otherwise the SSM public key
# (put by the fixture) and the test's signing key would diverge.
def _gen_ed25519(seed: bytes):
    k = Ed25519PrivateKey.from_private_bytes(seed)
    pem = k.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    raw_pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    return k, pem, raw_pub

UPDATE_PRIV, UPDATE_PRIV_PEM, UPDATE_PUB_RAW = _gen_ed25519(b"orion-test-update-signing-key-32")
UPDATE_PUB_B64 = base64.b64encode(UPDATE_PUB_RAW).decode()
LEASE_PRIV, LEASE_PRIV_PEM, LEASE_PUB_RAW = _gen_ed25519(b"orion-test-lease-signing-key-032")

SSM_PARAMS = {
    "/orion/token_secret":              TEST_TOKEN_SECRET,
    "/orion/admin_secret":              TEST_ADMIN_SECRET,
    "/orion/bot_service_secret":        TEST_BOT_SECRET,
    "/orion/website_pair_secret":       TEST_PAIR_SECRET,
    "/orion/worker_bot_secret":         TEST_WORKER_SECRET,
    "/orion/sellhub_webhook_secret":    TEST_SELLHUB_SECRET,
    "/orion/discord_public_key":        TEST_DISCORD_PUBKEY,
    "/orion/discord_bot_token":         TEST_DISCORD_BOT,
    "/orion/discord_guild_id":          TEST_GUILD_ID,
    "/orion/discord_customer_role_id":  TEST_CUSTOMER_ROLE,
    "/orion/discord_lifetime_role_id":  TEST_LIFETIME_ROLE,
    "/orion/edge_auth_secret":          TEST_EDGE_SECRET,
    "/orion/staff_token_secret":        TEST_STAFF_TOKEN_SECRET,
    "/orion/staff_enroll_key_hash":     hashlib.sha256(TEST_ENROLL_KEY.encode()).hexdigest(),
    "/orion/ed25519_public_key":        UPDATE_PUB_B64,
    "/orion/lease_signing_key":         LEASE_PRIV_PEM,
    "/orion/shard_encryption_key":      TEST_SHARD_ENC_KEY,
}


def sign_update_manifest_canonical(manifest_dict):
    """Sign a manifest the way the OFFLINE signer does, for CRIT-2 tests."""
    fields = ["latest_version", "minimum_supported_version", "artifact_url",
              "sha256", "published_at", "mandatory", "allow_rollback", "public_key_id"]
    canonical = {f: manifest_dict[f] for f in fields}
    payload = json.dumps(canonical, sort_keys=False, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    sig = UPDATE_PRIV.sign(payload)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def verify_lease_sig(license_key, machine_id, lease_expires_at, sig_b64url):
    """Verify a lease signature with the test lease public key (NEW-1 tests)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    pub = Ed25519PublicKey.from_public_bytes(LEASE_PUB_RAW)
    msg = f"{license_key.strip().upper()}:{machine_id.strip()}:{int(lease_expires_at)}".encode()
    pad = "=" * (-len(sig_b64url) % 4)
    sig = base64.urlsafe_b64decode(sig_b64url + pad)
    try:
        pub.verify(sig, msg)
        return True
    except InvalidSignature:
        return False

TABLE_DEFS = [
    # Admin Panel V2 §2: discord_user_id GSI on orion-licenses (the Lambda falls
    # back to a scan and reports lookup_mode="scan" when it is absent).
    ("orion-licenses",     {"KeySchema": [{"AttributeName": "license_key", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "license_key", "AttributeType": "S"},
                                {"AttributeName": "discord_user_id", "AttributeType": "S"},
                            ],
                            "GlobalSecondaryIndexes": [{
                                "IndexName": "discord_user_id-index",
                                "KeySchema": [{"AttributeName": "discord_user_id", "KeyType": "HASH"}],
                                "Projection": {"ProjectionType": "ALL"},
                            }]}),
    ("orion-nonces",       {"KeySchema": [{"AttributeName": "nonce", "KeyType": "HASH"}],
                            "AttributeDefinitions": [{"AttributeName": "nonce", "AttributeType": "S"}]}),
    ("orion-staff",        {"KeySchema": [{"AttributeName": "staff_id", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "staff_id", "AttributeType": "S"},
                                {"AttributeName": "discord_user_id", "AttributeType": "S"},
                            ],
                            "GlobalSecondaryIndexes": [{
                                "IndexName": "discord_user_id-index",
                                "KeySchema": [{"AttributeName": "discord_user_id", "KeyType": "HASH"}],
                                "Projection": {"ProjectionType": "ALL"},
                            }]}),
    ("orion-staff-audit",  {"KeySchema": [{"AttributeName": "audit_id", "KeyType": "HASH"}],
                            "AttributeDefinitions": [{"AttributeName": "audit_id", "AttributeType": "S"}]}),
    ("orion-tokens",       {"KeySchema": [{"AttributeName": "token_id", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "token_id", "AttributeType": "S"},
                                {"AttributeName": "token_hash", "AttributeType": "S"},
                            ],
                            "GlobalSecondaryIndexes": [{
                                "IndexName": "token_hash-index",
                                "KeySchema": [{"AttributeName": "token_hash", "KeyType": "HASH"}],
                                "Projection": {"ProjectionType": "ALL"},
                            }]}),
    ("orion-config",       {"KeySchema": [{"AttributeName": "config_key", "KeyType": "HASH"}],
                            "AttributeDefinitions": [{"AttributeName": "config_key", "AttributeType": "S"}]}),
    # Admin Panel V2 §4: day-ts-index backs the paged audit reader. The live table
    # is keyed on event_id (DynamoDB cannot change a PK), so the ULID is written to
    # BOTH event_id and audit_id.
    ("orion-audit",        {"KeySchema": [{"AttributeName": "event_id", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "event_id", "AttributeType": "S"},
                                {"AttributeName": "day", "AttributeType": "S"},
                                {"AttributeName": "ts", "AttributeType": "N"},
                            ],
                            "GlobalSecondaryIndexes": [{
                                "IndexName": "day-ts-index",
                                "KeySchema": [{"AttributeName": "day", "KeyType": "HASH"},
                                              {"AttributeName": "ts", "KeyType": "RANGE"}],
                                "Projection": {"ProjectionType": "ALL"},
                            }]}),
    ("orion-ratelimit",    {"KeySchema": [{"AttributeName": "rl_key", "KeyType": "HASH"}],
                             "AttributeDefinitions": [{"AttributeName": "rl_key", "AttributeType": "S"}]}),
    ("orion-shards",       {"KeySchema": [{"AttributeName": "build_id", "KeyType": "HASH"}],
                             "AttributeDefinitions": [{"AttributeName": "build_id", "AttributeType": "S"}]}),
    ("orion-update-manifest", {"KeySchema": [{"AttributeName": "record_id", "KeyType": "HASH"}],
                            "AttributeDefinitions": [{"AttributeName": "record_id", "AttributeType": "S"}]}),
]


@pytest.fixture
def lf(monkeypatch):
    """Start moto, create tables + SSM, then import/reload lambda_function."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, spec in TABLE_DEFS:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", **spec)
        ssm = boto3.client("ssm", region_name="us-east-1")
        for name, val in SSM_PARAMS.items():
            # The Ed25519 PUBLIC key is not secret and is read with decrypt=False
            # (load_ed25519_public_key_b64) -> store it as a plain String so the
            # value round-trips (a SecureString read undecrypted is mangled).
            ptype = "String" if name == "/orion/ed25519_public_key" else "SecureString"
            ssm.put_parameter(Name=name, Value=val, Type=ptype, Overwrite=True)

        # Remove any cached module
        for mod_name in list(sys.modules):
            if mod_name == "lambda_function" or mod_name.startswith("lambda_function."):
                del sys.modules[mod_name]

        # Ensure backend dir is importable
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
        backend_dir = os.path.abspath(backend_dir)
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        import lambda_function as mod
        importlib.reload(mod)

        # The backend suite must never reach the real Discord API. Tests that
        # assert role delivery/removal replace these stubs with explicit mocks;
        # unrelated licence/order tests get a deterministic successful service.
        monkeypatch.setattr(mod, "_discord_add_role", lambda *args, **kwargs: True)
        monkeypatch.setattr(mod, "_discord_remove_role", lambda *args, **kwargs: True)

        # Clean edge-auth env
        monkeypatch.delenv("EDGE_AUTH_ENFORCE", raising=False)

        yield mod

        # cleanup
        for mod_name in list(sys.modules):
            if mod_name == "lambda_function" or mod_name.startswith("lambda_function."):
                del sys.modules[mod_name]


def make_event(method="POST", path="/api/activate", body=None, headers=None, qs=None, raw_body=None):
    """Build an API-Gateway-style event dict."""
    if raw_body is not None:
        body_str = raw_body
    elif body is not None:
        body_str = json.dumps(body)
    else:
        body_str = "{}"
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {k: v for k, v in (headers or {}).items()},
        "body": body_str,
        "queryStringParameters": qs or {},
    }


def invoke(lf, method="POST", path="/api/activate", body=None, headers=None, qs=None, raw_body=None, edge=True):
    """Invoke lambda_handler and return (status_code, parsed_body).

    HIGH-4: edge auth is now enforced on ALL routes and fails closed, so by
    default we inject a valid X-Edge-Auth header (mimicking the Cloudflare/webhook
    caller). Pass edge=False to omit it, or supply your own x-edge-auth header, to
    exercise the edge-auth gate itself."""
    headers = dict(headers or {})
    if edge and not any(k.lower() == "x-edge-auth" for k in headers):
        headers["x-edge-auth"] = TEST_EDGE_SECRET
    event = make_event(method=method, path=path, body=body, headers=headers, qs=qs, raw_body=raw_body)
    resp = lf.lambda_handler(event, None)
    status = resp.get("statusCode", 0)
    try:
        parsed = json.loads(resp.get("body", "{}"))
    except Exception:
        parsed = {}
    return status, parsed, resp


def make_nonce_ts():
    """Return a dict with a fresh nonce + current timestamp."""
    import uuid as _uuid
    return {"request_nonce": _uuid.uuid4().hex, "request_timestamp": int(time.time())}


def make_staff_nonce_ts():
    """Return the bare nonce/timestamp field names handle_staff_enroll/login expect."""
    import uuid as _uuid
    return {"nonce": _uuid.uuid4().hex, "timestamp": int(time.time())}


def put_license(lf, key, **kwargs):
    """Insert a license row directly into DynamoDB."""
    now = int(time.time())
    item = {
        "license_key": key,
        "email": kwargs.get("email", "test@test.com"),
        "plan": kwargs.get("plan", "standard"),
        "status": kwargs.get("status", "active"),
        "revoked": kwargs.get("revoked", False),
        "machine_id": kwargs.get("machine_id", ""),
        "expiry": kwargs.get("expiry", now + 365 * 86400),
        "created_at": now,
        "activations": kwargs.get("activations", 0),
        "max_devices": kwargs.get("max_devices", 1),
        # Test licenses represent a paid Discord-linked entitlement by default.
        # Tests for unsubscribed/manual keys insert their rows explicitly.
        "source": kwargs.get("source", "gumroad"),
    }
    discord_user_id = kwargs.get("discord_user_id", "test-discord-account")
    order_id = kwargs.get("order_id", "test-paid-order")
    if discord_user_id:
        item["discord_user_id"] = str(discord_user_id)
    if order_id:
        item["order_id"] = str(order_id)
    item.update(kwargs.get("extra", {}))
    lf.licenses_table().put_item(Item=item)
    return item


def put_staff(lf, staff_id, **kwargs):
    """Insert a staff row directly into DynamoDB."""
    now = int(time.time())
    item = {
        "staff_id": staff_id,
        "display_name": kwargs.get("display_name", "Test Staff"),
        "role": kwargs.get("role", "staff"),
        "disabled": kwargs.get("disabled", False),
        "machine_id": kwargs.get("machine_id", ""),
        "created_at": now,
        "enrolled_at": now,
    }
    if kwargs.get("discord_user_id"):
        item["discord_user_id"] = str(kwargs["discord_user_id"])
    if kwargs.get("caps"):
        item["caps"] = kwargs["caps"]
    item.update(kwargs.get("extra", {}))
    lf.staff_table().put_item(Item=item)
    return item


# ── Admin Panel V2 helpers ────────────────────────────────────────────────────
OWNER_H = {"x-orion-admin-secret": TEST_ADMIN_SECRET}
BOT_H   = {"x-orion-bot-secret": TEST_BOT_SECRET}


def staff_login(lf, staff_id, role="support", machine_id=None, **kw):
    """Create a staff row with `role` and log in. Returns the auth headers."""
    machine_id = machine_id or ("MACHINE-" + staff_id.upper())
    put_staff(lf, staff_id, role=role, machine_id="", **kw)
    nt = make_staff_nonce_ts()
    s, b, _ = invoke(lf, "POST", "/api/staff/login",
                     body={"staff_id": staff_id, "machine_id": machine_id, **nt})
    assert s == 200, b
    return {"authorization": f"Bearer {b['token']}", "x-machine-id": machine_id}


def no_dm(lf, monkeypatch, fail=False):
    """Stub the Discord DM sender. The real one makes a live HTTPS call, which is
    why the two baseline trial tests failed offline (the DM bounced and the trial
    correctly rolled back). We stub the transport, never the rollback."""
    sent = []

    def _dm(discord_user_id, embed):
        if fail:
            raise RuntimeError("stubbed DM failure")
        sent.append((str(discord_user_id), embed))
        return {"id": "stub"}

    monkeypatch.setattr(lf, "_discord_dm", _dm)
    monkeypatch.setattr(lf, "_discord_add_role", lambda *a, **k: True)
    return sent


def set_config(lf, key, value):
    """Write a V2 config key straight into orion-config."""
    lf.config_set(key, value)


def audit_rows(lf, action=None):
    rows = lf.audit_table().scan().get("Items", [])
    if action:
        rows = [r for r in rows if r.get("action") == action]
    return sorted(rows, key=lambda r: int(r.get("ts", 0)), reverse=True)


def sellhub_hmac(raw_body_str):
    """Compute the SellHub webhook signature for a raw body string."""
    import hmac, hashlib
    return hmac.new(TEST_SELLHUB_SECRET.encode(), raw_body_str.encode(), hashlib.sha256).hexdigest()
