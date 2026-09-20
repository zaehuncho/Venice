"""Gumroad-webhook half of Admin Panel V2 (docs/ADMIN_PANEL_V2_CONTRACT.md §3/§6).

Real DynamoDB/SSM via moto (same harness shape as tests/backend/test_webhooks.py);
the only stubs are the module's single HTTP egress seam `_urlopen` and the
Gumroad license verify. Assertions are about what LANDS in DynamoDB and what
PAYLOAD leaves for the backend, so the backend agent can cross-check the routes.

Pinned here:
  §6  refunded / disputed / chargebacked -> status=revoked + revoked=True +
      revoke_reason, per-kind idempotency, owner alert on dispute/chargeback.
      dispute_won -> un-revoke, but ONLY a revoke this webhook made.
  §3  "Paid credit path": a sale of GUMROAD_HWID_RESET_PRODUCT calls
      POST /api/bot/hwid-credit {discord_user_id, order_id} instead of minting.
  §4  alerts never block the operation.
And the refund regression: the Gumroad `uses` replay guard must not be applied to
revoke-type pings (it used to 401 every refund before it could revoke).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.parse

import pytest

# moto/boto3 are the same extra deps that keep tests/backend a standalone suite;
# this file lives in the ROOT suite, so skip rather than fail a minimal env.
boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WEBHOOK_PATH = os.path.join(REPO, "discord_launch", "gumroad_webhook", "lambda_function.py")

TOKEN = "gumroad-url-token-xyz"
SELLER = "4-7tKV7OFXGk7mm0h87eFQ=="        # == GUMROAD_SELLER_ID
OWNER_ID = "999000111222333444"

SSM_PARAMS = {
    "/orion/gumroad_webhook_token": TOKEN,
    "/orion/bot_service_secret": "bot-secret",
    "/orion/webhook_bot_secret": "webhook-secret",
    "/orion/discord_bot_token": "discord-bot-token",
    "/orion/edge_auth_secret": "edge-secret",
}

TABLES = [
    ("orion-gumroad-events", "event_key"),
    ("orion-gumroad-orders", "sale_id"),
    ("orion-licenses", "license_key"),
]


class FakeHTTP:
    """Stands in for `_urlopen`. Records (url, headers, json body) and replies
    per-URL-suffix. Discord DM calls answer with a channel id."""

    def __init__(self):
        self.calls = []
        self.fail_urls = set()

    class _R:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __call__(self, req, timeout=10):
        url = req.full_url
        try:
            body = json.loads(req.data.decode()) if req.data else {}
        except Exception:
            body = urllib.parse.parse_qs(req.data.decode()) if req.data else {}
        self.calls.append({"url": url, "body": body, "headers": dict(req.headers)})
        if any(f in url for f in self.fail_urls):
            raise RuntimeError("stubbed egress failure")
        if url.endswith("/users/@me/channels"):
            return self._R({"id": "dm-channel-1"})
        if "/channels/" in url and url.endswith("/messages"):
            return self._R({"id": "msg-1"})
        if url.endswith("/api/bot/hwid-credit"):
            return self._R({"ok": True, "hwid_paid_credits": 1})
        if url.endswith("/api/bot/provision"):
            return self._R({"license_key": "ORION-MINT-0001"})
        return self._R({})

    def to(self, suffix):
        return [c for c in self.calls if c["url"].endswith(suffix)]

    def dm_embeds(self):
        return [c["body"]["embeds"][0] for c in self.calls
                if "/channels/" in c["url"] and c["url"].endswith("/messages")]


def _load(name="gumroad_wh_adminv2"):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, WEBHOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wh(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_USER_ID", OWNER_ID)
    monkeypatch.setenv("GUMROAD_HWID_RESET_PRODUCT", "orion-hwid-reset")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, pk in TABLES:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}])
        ssm = boto3.client("ssm", region_name="us-east-1")
        for n, v in SSM_PARAMS.items():
            ssm.put_parameter(Name=n, Value=v, Type="SecureString", Overwrite=True)

        mod = _load()
        http = FakeHTTP()
        monkeypatch.setattr(mod, "_urlopen", http)
        monkeypatch.setattr(mod, "verify_license_key", lambda *a, **k: True)
        yield mod, http
        sys.modules.pop("gumroad_wh_adminv2", None)


def ping(sale_id="sale-1", license_key="KEY-AAAA", permalink="orion-30day",
         token=TOKEN, discord_id="123456789", **flags):
    form = {"sale_id": sale_id, "seller_id": SELLER, "product_permalink": permalink,
            "license_key": license_key, "email": "buyer@test.com", "Discord ID": discord_id}
    form.update({k: "true" for k, v in flags.items() if v})
    return {"pathParameters": {"token": token},
            "body": urllib.parse.urlencode(form), "isBase64Encoded": False}


def body(r):
    return json.loads(r["body"])


def seed_order(mod, sale_id="sale-1", license_key="ORION-MINT-0001", status="active"):
    mod.orders_table.put_item(Item={"sale_id": sale_id, "license_key": license_key,
                                    "discord_user_id": "123456789", "plan": "30day",
                                    "status": status, "created_at": 1})
    mod.licenses_table.put_item(Item={"license_key": license_key, "status": "active",
                                      "revoked": False, "plan": "30day", "expiry": 1800000000})


# ── event classification ──────────────────────────────────────────────────────

@pytest.mark.parametrize("flags,kind", [
    ({}, "purchase"),
    ({"refunded": "true"}, "refund"),
    ({"disputed": "true"}, "dispute"),
    ({"chargebacked": "true"}, "chargeback"),
    ({"chargeback": "true"}, "chargeback"),
    ({"disputed": "true", "dispute_won": "true"}, "dispute_won"),
    ({"refunded": "false"}, "purchase"),
])
def test_classify_event(wh, flags, kind):
    mod, _ = wh
    assert mod.classify_event(flags) == kind


# ── §6 revoke paths ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag,kind,reason", [
    ("refunded", "refund", "refunded"),
    ("disputed", "dispute", "disputed"),
    ("chargebacked", "chargeback", "chargebacked"),
])
def test_revoke_writes_status_and_revoked_flag(wh, flag, kind, reason):
    mod, http = wh
    seed_order(mod)
    r = mod.lambda_handler(ping(**{flag: True}), None)
    assert r["statusCode"] == 200
    assert body(r) == {"revoked": True, "sale_id": "sale-1", "kind": kind, "reason": reason}

    lic = mod.licenses_table.get_item(Key={"license_key": "ORION-MINT-0001"})["Item"]
    # BOTH names: backend/lambda_function.py checks `revoked` on activate and
    # `status` on validate — a status-only write left the key usable on one path.
    assert lic["status"] == "revoked" and lic["revoked"] is True
    assert lic["revoke_reason"] == reason and lic["revoked_at"]

    order = mod.orders_table.get_item(Key={"sale_id": "sale-1"})["Item"]
    assert order["status"] == "revoked" and order["revoke_reason"] == reason


def test_refund_ping_is_not_killed_by_the_replay_guard(wh, monkeypatch):
    """Regression: verify_license_key used to run with increment+enforce on EVERY
    ping, so the refund's verify read uses=2 and 401'd before revoking. Revoke
    events must verify without incrementing and without the uses ceiling."""
    mod, http = wh
    seen = {}

    def spy(key, permalink, product_id, increment=True, enforce_uses=True):
        seen["increment"] = increment
        seen["enforce_uses"] = enforce_uses
        return True

    monkeypatch.setattr(mod, "verify_license_key", spy)
    seed_order(mod)
    r = mod.lambda_handler(ping(refunded=True), None)
    assert r["statusCode"] == 200
    assert seen == {"increment": False, "enforce_uses": False}

    # …while a purchase still spends its single use (HIGH-2 intact).
    mod.lambda_handler(ping(sale_id="sale-2", license_key="KEY-NEW"), None)
    assert seen == {"increment": True, "enforce_uses": True}


def test_dispute_and_chargeback_alert_the_owner(wh):
    mod, http = wh
    seed_order(mod)
    mod.lambda_handler(ping(disputed=True), None)
    dm_targets = [c["body"]["recipient_id"] for c in http.to("/users/@me/channels")]
    assert OWNER_ID in dm_targets
    assert any("dispute" in (e.get("title") or "").lower() for e in http.dm_embeds())


def test_plain_refund_does_not_alert(wh):
    """Contract §4 default alert set is dispute/chargeback (`webhook.chargeback`).
    A routine refund is noise."""
    mod, http = wh
    seed_order(mod)
    mod.lambda_handler(ping(refunded=True), None)
    assert http.to("/users/@me/channels") == []


def test_refund_removes_customer_role(wh, monkeypatch):
    mod, _ = wh
    role_events = []
    monkeypatch.setattr(mod, "set_customer_role",
                        lambda user_id, add: role_events.append((user_id, add)) or True)
    seed_order(mod)
    r = mod.lambda_handler(ping(refunded=True), None)
    assert r["statusCode"] == 200
    assert role_events == [("123456789", False)]


def test_alert_failure_never_blocks_the_revoke(wh):
    mod, http = wh
    http.fail_urls.add("/users/@me/channels")
    seed_order(mod)
    r = mod.lambda_handler(ping(chargebacked=True), None)
    assert r["statusCode"] == 200 and body(r)["revoked"] is True
    lic = mod.licenses_table.get_item(Key={"license_key": "ORION-MINT-0001"})["Item"]
    assert lic["status"] == "revoked"


def test_dispute_on_an_unknown_sale_still_alerts(wh):
    mod, http = wh
    r = mod.lambda_handler(ping(sale_id="never-seen", disputed=True), None)
    assert body(r)["skipped"] == "no_existing_order_to_revoke"
    assert http.to("/users/@me/channels"), "an unexplained dispute must reach the owner"


def test_each_event_kind_has_its_own_idempotency_slot(wh):
    mod, _ = wh
    seed_order(mod)
    assert mod.lambda_handler(ping(refunded=True), None)["statusCode"] == 200
    # a re-POST of the SAME kind is a duplicate…
    assert body(mod.lambda_handler(ping(refunded=True), None))["reason"] == "duplicate_event"
    # …but a later dispute on the same sale_id is a different event and runs.
    r = mod.lambda_handler(ping(disputed=True), None)
    assert body(r).get("revoked") is True


# ── dispute_won ───────────────────────────────────────────────────────────────

def test_dispute_won_unrevokes_what_the_webhook_revoked(wh):
    mod, http = wh
    seed_order(mod)
    mod.lambda_handler(ping(disputed=True), None)
    r = mod.lambda_handler(ping(disputed=True, dispute_won=True), None)
    assert body(r)["unrevoked"] is True

    lic = mod.licenses_table.get_item(Key={"license_key": "ORION-MINT-0001"})["Item"]
    assert lic["status"] == "active" and lic["revoked"] is False
    assert "revoke_reason" not in lic and "revoked_at" not in lic
    assert mod.orders_table.get_item(Key={"sale_id": "sale-1"})["Item"]["status"] == "active"


def test_dispute_won_never_undoes_an_owner_revoke(wh):
    """An owner/staff revoke writes kill_reason, not revoke_reason. A won dispute
    must leave it alone and tell the owner, not silently re-arm a banned key."""
    mod, http = wh
    seed_order(mod)
    mod.licenses_table.update_item(
        Key={"license_key": "ORION-MINT-0001"},
        UpdateExpression="SET #s = :s, revoked = :r, kill_reason = :k",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "revoked", ":r": True, ":k": "key sharing"})
    r = mod.lambda_handler(ping(disputed=True, dispute_won=True), None)
    assert body(r) == {"unrevoked": False, "reason": "not_webhook_revoked", "sale_id": "sale-1"}
    lic = mod.licenses_table.get_item(Key={"license_key": "ORION-MINT-0001"})["Item"]
    assert lic["status"] == "revoked" and lic["revoked"] is True
    assert http.to("/users/@me/channels"), "the owner must be told it was left alone"


# ── §3 paid HWID-reset credit ─────────────────────────────────────────────────

def test_hwid_reset_product_grants_a_credit_not_a_license(wh):
    mod, http = wh
    r = mod.lambda_handler(ping(sale_id="sale-hw", license_key="KEY-HW",
                                permalink="orion-hwid-reset"), None)
    assert r["statusCode"] == 200
    assert body(r) == {"success": True, "sale_id": "sale-hw", "plan": "hwid_reset",
                       "hwid_credit": True, "dm_status": "delivered"}
    assert http.to("/api/bot/provision") == [], "no license may be minted for a reset product"

    call = http.to("/api/bot/hwid-credit")[0]
    assert call["body"] == {"discord_user_id": "123456789", "order_id": "sale-hw"}
    # the same server-to-server auth as /provision
    assert call["headers"]["X-orion-bot-secret"] == "webhook-secret"
    assert call["headers"]["X-edge-auth"] == "edge-secret"

    order = mod.orders_table.get_item(Key={"sale_id": "sale-hw"})["Item"]
    assert order["plan"] == "hwid_reset" and order["status"] == "credited"
    assert any("HWID reset credit" in (e.get("title") or "") for e in http.dm_embeds())


def test_hwid_credit_is_granted_once_per_gumroad_key(wh):
    mod, http = wh
    mod.lambda_handler(ping(sale_id="sale-hw", license_key="KEY-HW", permalink="orion-hwid-reset"), None)
    r = mod.lambda_handler(ping(sale_id="sale-hw-replay", license_key="KEY-HW",
                                permalink="orion-hwid-reset"), None)
    assert body(r)["reason"] == "credit_already_granted"
    assert len(http.to("/api/bot/hwid-credit")) == 1


def test_failed_credit_releases_the_claim_so_the_retry_works(wh):
    mod, http = wh
    http.fail_urls.add("/api/bot/hwid-credit")
    r = mod.lambda_handler(ping(sale_id="sale-hw", license_key="KEY-HW", permalink="orion-hwid-reset"), None)
    assert r["statusCode"] == 500 and body(r)["error"] == "hwid_credit_failed"
    assert mod.events_table.get_item(Key={"event_key": "lickey:KEY-HW"}).get("Item") is None

    http.fail_urls.clear()
    r2 = mod.lambda_handler(ping(sale_id="sale-hw-2", license_key="KEY-HW",
                                 permalink="orion-hwid-reset"), None)
    assert body(r2)["success"] is True


def test_hwid_reset_purchase_needs_a_discord_id(wh):
    mod, _ = wh
    r = mod.lambda_handler(ping(sale_id="sale-hw", license_key="KEY-HW",
                                permalink="orion-hwid-reset", discord_id=""), None)
    assert r["statusCode"] == 422


# ── the ordinary purchase path still works ────────────────────────────────────

def test_normal_purchase_still_mints_and_dms(wh):
    mod, http = wh
    r = mod.lambda_handler(ping(), None)
    assert body(r)["success"] is True
    prov = http.to("/api/bot/provision")[0]
    assert prov["body"] == {"plan": "30day", "days": 30,
                            "discord_user_id": "123456789", "order_id": "sale-1"}
    assert http.to("/api/bot/hwid-credit") == []
    assert any("License Activated" in (e.get("title") or "") for e in http.dm_embeds())


def test_bad_token_and_seller_still_refused(wh):
    mod, _ = wh
    assert mod.lambda_handler(ping(token="nope"), None)["statusCode"] == 403
    ev = ping()
    ev["body"] = ev["body"].replace(urllib.parse.quote_plus(SELLER), "someone-else")
    assert mod.lambda_handler(ev, None)["statusCode"] == 401


def test_owner_alert_recipient_resolution(wh, monkeypatch):
    mod, http = wh
    assert mod.owner_discord_user_id() == OWNER_ID
    # env cleared -> SSM fallback -> '' (and alert_owner must not raise)
    monkeypatch.setattr(mod, "OWNER_DISCORD_USER_ID_ENV", "")
    assert mod.owner_discord_user_id() == ""
    assert mod.alert_owner("t", "d") is False
