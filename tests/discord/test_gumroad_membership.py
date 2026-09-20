"""Gumroad single-product checkout + the recurring monthly membership.

Owner rule 2026-09-15: *"only the 3 day trial then 25 monthly after that, no
lifetime so it can keep recurring"*. The live storefront has one Gumroad product:

  `orion-monthly`     a MEMBERSHIP. Its ping mints the key on the first charge and
                      RENEWS it on every charge after that.
Legacy activation-fee handling remains opt-in for old events, but it is disabled
by default and is not part of the storefront.

The renewal half is the part that can quietly cost money: a membership re-sends
the SAME `license_key` every month, so the `lickey:` "one key per Gumroad key
EVER" marker — correct for one-off sales — would swallow every renewal and let a
paying customer's licence lapse. These tests pin that it doesn't.

Same harness shape as test_gumroad_webhook_admin_v2.py: real DynamoDB/SSM via
moto, `_urlopen` and the Gumroad licence verify stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.parse

import pytest

boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WEBHOOK_PATH = os.path.join(REPO, "discord_launch", "gumroad_webhook", "lambda_function.py")

TOKEN = "gumroad-url-token-xyz"
SELLER = "4-7tKV7OFXGk7mm0h87eFQ=="        # == GUMROAD_SELLER_ID

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
    """Stands in for `_urlopen`; records every call and answers per URL suffix."""

    def __init__(self):
        self.calls = []
        self.provision_reply = {"ok": True, "license_key": "ORION-MINT-0001",
                                "plan": "month", "expiry": 1800000000}

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
        self.calls.append({"url": url, "body": body})
        if url.endswith("/users/@me/channels"):
            return self._R({"id": "dm-channel-1"})
        if "/channels/" in url and url.endswith("/messages"):
            return self._R({"id": "msg-1"})
        if url.endswith("/api/bot/provision"):
            return self._R(self.provision_reply)
        if url.endswith("/api/bot/hwid-credit"):
            return self._R({"ok": True, "hwid_paid_credits": 1})
        return self._R({})

    def to(self, suffix):
        return [c for c in self.calls if c["url"].endswith(suffix)]

    def dm_embeds(self):
        return [c["body"]["embeds"][0] for c in self.calls
                if "/channels/" in c["url"] and c["url"].endswith("/messages")]


def _load(name="gumroad_wh_membership"):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, WEBHOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wh(monkeypatch):
    monkeypatch.setenv("GUMROAD_ACTIVATION_PRODUCT", "orion-activation")
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
        sys.modules.pop("gumroad_wh_membership", None)


def ping(sale_id="sale-1", license_key="KEY-AAAA", permalink="orion-monthly",
         discord_id="123456789", **flags):
    form = {"sale_id": sale_id, "seller_id": SELLER, "product_permalink": permalink,
            "license_key": license_key, "email": "buyer@test.com", "Discord ID": discord_id}
    form.update({k: "true" for k, v in flags.items() if v})
    return {"pathParameters": {"token": TOKEN},
            "body": urllib.parse.urlencode(form), "isBase64Encoded": False}


def body(r):
    return json.loads(r["body"])


# ── the product map ───────────────────────────────────────────────────────────

class TestProductMap:
    def test_the_membership_slug_maps_to_the_month_plan(self, wh):
        mod, _ = wh
        assert mod.MEMBERSHIP_PRODUCT == "orion-monthly"
        assert mod.PRODUCT_MAP["orion-monthly"] == {
            "plan": "month", "days": 30, "slug": "ORION_MONTH"}

    def test_legacy_slugs_survive_for_refund_pings(self, wh):
        """A refund on a key sold last year still has to resolve to a plan."""
        mod, _ = wh
        for slug in ("orion-7day", "orion-30day", "orion-lifetime", "orion-beta"):
            assert slug in mod.PRODUCT_MAP

    def test_the_activation_fee_is_not_a_product_that_mints(self, wh):
        mod, _ = wh
        assert mod.ACTIVATION_PRODUCT == "orion-activation"
        assert mod.ACTIVATION_PRODUCT not in mod.PRODUCT_MAP


# ── the activation fee ────────────────────────────────────────────────────────

class TestActivationFee:
    def test_activation_ping_mints_nothing(self, wh):
        mod, http = wh
        r = mod.lambda_handler(ping(sale_id="sale-act", license_key="KEY-ACT",
                                    permalink="orion-activation"), None)
        assert r["statusCode"] == 200
        b = body(r)
        assert b["success"] is True and b["license_issued"] is False
        assert b["plan"] == "activation_fee"
        assert http.to("/api/bot/provision") == [], "the fee must not mint a licence"

    def test_activation_ping_tells_the_buyer_a_key_is_not_coming_from_it(self, wh):
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-act2", license_key="KEY-ACT2",
                                permalink="orion-activation"), None)
        embeds = http.dm_embeds()
        assert len(embeds) == 1
        text = json.dumps(embeds[0]).lower()
        assert "activation fee" in text
        assert "subscription" in text, "must point at the second checkout"

    def test_activation_ping_is_idempotent(self, wh):
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-act3", license_key="KEY-ACT3",
                                permalink="orion-activation"), None)
        r = mod.lambda_handler(ping(sale_id="sale-act4", license_key="KEY-ACT3",
                                    permalink="orion-activation"), None)
        assert body(r).get("skipped") is True
        assert len(http.dm_embeds()) == 1

    def test_a_refunded_activation_fee_revokes_no_licence(self, wh):
        mod, _ = wh
        mod.lambda_handler(ping(sale_id="sale-act5", license_key="KEY-ACT5",
                                permalink="orion-activation"), None)
        r = mod.lambda_handler(ping(sale_id="sale-act5", license_key="KEY-ACT5",
                                    permalink="orion-activation", refunded=True), None)
        assert r["statusCode"] == 200
        order = mod.orders_table.get_item(Key={"sale_id": "sale-act5"})["Item"]
        assert order["license_key"] == ""


# ── the recurring membership ──────────────────────────────────────────────────

class TestRecurringMembership:
    def test_current_gumroad_membership_without_legacy_license_key_mints(self, wh, monkeypatch):
        """Current Gumroad Memberships do not expose the legacy license-key
        switch. The exact allow-listed slug therefore uses the secret Ping path
        plus seller id and remains idempotent by Gumroad sale id."""
        mod, http = wh
        verify_calls = []
        monkeypatch.setattr(mod, "verify_license_key",
                            lambda *a, **k: verify_calls.append((a, k)) or True)
        r = mod.lambda_handler(ping(sale_id="sale-nokey", license_key=""), None)
        assert r["statusCode"] == 200, r
        assert body(r)["success"] is True
        assert len(http.to("/api/bot/provision")) == 1
        assert verify_calls == []

        replay = mod.lambda_handler(ping(sale_id="sale-nokey", license_key=""), None)
        assert body(replay) == {"skipped": True, "reason": "duplicate_event"}
        assert len(http.to("/api/bot/provision")) == 1

    def test_non_allowlisted_product_without_license_key_fails_closed(self, wh):
        mod, http = wh
        r = mod.lambda_handler(ping(sale_id="sale-bad", license_key="",
                                    permalink="orion-30day"), None)
        assert r["statusCode"] == 401
        assert body(r) == {"error": "missing_license_key"}
        assert http.to("/api/bot/provision") == []

    def test_first_charge_mints(self, wh):
        mod, http = wh
        r = mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        assert r["statusCode"] == 200
        b = body(r)
        assert b["success"] is True and b["plan"] == "month" and b["renewed"] is False
        sent = http.to("/api/bot/provision")[0]["body"]
        assert sent["plan"] == "month" and sent["days"] == 30
        assert "renew" not in sent, "the first charge must mint, not renew"

    def test_recurring_charge_renews_instead_of_being_swallowed(self, wh):
        """THE regression this file exists for: the second charge re-sends the same
        Gumroad license_key, and the `lickey:` marker would answer
        'already minted' — billing the customer while their key expires."""
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        http.provision_reply = {"ok": True, "license_key": "ORION-MINT-0001",
                                "plan": "month", "expiry": 1802592000, "renewed": True}

        r = mod.lambda_handler(ping(sale_id="sale-m2", license_key="KEY-SUB",
                                    is_recurring_charge=True), None)
        assert r["statusCode"] == 200, r
        b = body(r)
        assert b.get("skipped") is not True, "a renewal must never be skipped"
        assert b["renewed"] is True
        calls = http.to("/api/bot/provision")
        assert len(calls) == 2
        assert calls[1]["body"]["renew"] is True
        assert calls[1]["body"]["order_id"] == "sale-m2"

    def test_a_renewal_dm_does_not_resend_a_key(self, wh):
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        http.provision_reply = {"ok": True, "license_key": "ORION-MINT-0001",
                                "plan": "month", "renewed": True}
        mod.lambda_handler(ping(sale_id="sale-m2", license_key="KEY-SUB",
                                is_recurring_charge=True), None)
        first, second = http.dm_embeds()
        assert "ORION-MINT-0001" in json.dumps(first), "the first charge delivers the key"
        assert "ORION-MINT-0001" not in json.dumps(second), \
            "a renewal must not re-send a key that has not changed"
        assert "renew" in json.dumps(second).lower()

    def test_a_replayed_recurring_ping_is_still_deduped_by_sale_id(self, wh):
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        http.provision_reply = {"ok": True, "license_key": "ORION-MINT-0001",
                                "plan": "month", "renewed": True}
        mod.lambda_handler(ping(sale_id="sale-m2", license_key="KEY-SUB",
                                is_recurring_charge=True), None)
        r = mod.lambda_handler(ping(sale_id="sale-m2", license_key="KEY-SUB",
                                    is_recurring_charge=True), None)
        assert body(r).get("skipped") is True
        assert len(http.to("/api/bot/provision")) == 2

    def test_a_non_recurring_replay_of_one_key_still_cannot_re_mint(self, wh):
        """The HIGH-2 guard must survive the renewal carve-out: a forged ping with
        a fresh sale_id but no recurring flag still hits the lickey marker."""
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        r = mod.lambda_handler(ping(sale_id="sale-forged", license_key="KEY-SUB"), None)
        assert body(r).get("reason") == "license_key_already_minted"
        assert len(http.to("/api/bot/provision")) == 1

    def test_recurring_flag_detection(self, wh):
        mod, _ = wh
        assert mod.is_recurring_charge({"is_recurring_charge": "true"}) is True
        assert mod.is_recurring_charge({"recurring_charge": "true"}) is True
        assert mod.is_recurring_charge({"recurrence": "monthly"}) is False, \
            "`recurrence` is set on the FIRST charge too — it must still mint"
        assert mod.is_recurring_charge({}) is False

    def test_the_backend_decides_whether_it_renewed(self, wh):
        """If the server declines to renew (no extendable key) it returns a fresh
        key with renewed absent — the webhook must then send the KEY DM, not the
        renewal DM, or the customer is left with no key at all."""
        mod, http = wh
        mod.lambda_handler(ping(sale_id="sale-m1", license_key="KEY-SUB"), None)
        http.provision_reply = {"ok": True, "license_key": "ORION-MINT-0002",
                                "plan": "month"}          # no `renewed`
        mod.lambda_handler(ping(sale_id="sale-m2", license_key="KEY-SUB",
                                is_recurring_charge=True), None)
        assert "ORION-MINT-0002" in json.dumps(http.dm_embeds()[-1])

    def test_failed_license_less_purchase_releases_both_claims_for_retry(self, wh, monkeypatch):
        """A transient provision failure must not strand a paid membership."""
        mod, _ = wh
        calls = 0

        def flaky_provision(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary backend outage")
            return {"ok": True, "license_key": "ORION-MINT-RETRY", "plan": "month"}

        monkeypatch.setattr(mod, "provision_license", flaky_provision)
        event = ping(sale_id="sale-license-less-retry", license_key="")
        failed = mod.lambda_handler(event, None)
        assert failed["statusCode"] == 500
        assert body(failed)["error"] == "provision_failed"

        retried = mod.lambda_handler(event, None)
        assert retried["statusCode"] == 200
        assert body(retried).get("skipped") is not True
        assert calls == 2
