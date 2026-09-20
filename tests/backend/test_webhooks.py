"""Webhook security properties:
  HIGH-2  a replayed Gumroad ping mints only ONE backend license (idempotency on
          the genuine license_key, not the attacker-chosen sale_id).
  NEW-3   the Gumroad URL token is compared timing-safe (wrong token -> 403).
  NEW-4   SellHub verify_signature strips the "sha256=" PREFIX, not a char set.
  NEW-6   webhook error bodies do not echo internal exception text.
"""
import json
import os
import sys
import importlib.util
import urllib.parse
import hmac
import hashlib
import pytest
from moto import mock_aws
import boto3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GUMROAD_PATH = os.path.join(REPO, "discord_launch", "gumroad_webhook", "lambda_function.py")
SELLHUB_PATH = os.path.join(REPO, "discord_launch", "deployed_bundle",
                            "orion-sellhub-webhook", "lambda_v6.py")

GUMROAD_TOKEN  = "gumroad-url-token-xyz"
SELLHUB_SECRET = "sellhub-secret-xyz"

WEBHOOK_SSM = {
    "/orion/gumroad_webhook_token":            GUMROAD_TOKEN,
    "/orion/bot_service_secret":               "bot-secret",
    "/orion/webhook_bot_secret":               "webhook-secret",
    "/orion/discord_bot_token":                "discord-bot-token",
    "/orion/edge_auth_secret":                 "edge-secret",
    "/orion/sellhub_webhook_secret":           SELLHUB_SECRET,
    "/orion/sellhub_webhook_secret_refunded":  "sellhub-refund-secret",
}

WEBHOOK_TABLES = [
    ("orion-gumroad-events", "event_key"),
    ("orion-gumroad-orders", "sale_id"),
    ("orion-licenses",       "license_key"),
    ("orion-webhook-events", "event_id"),
    ("orion-sellhub-orders", "order_id"),
]


def _load_module(path, name):
    for m in list(sys.modules):
        if m == name:
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def webhooks(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, pk in WEBHOOK_TABLES:
            ddb.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}])
        ssm = boto3.client("ssm", region_name="us-east-1")
        for n, v in WEBHOOK_SSM.items():
            ssm.put_parameter(Name=n, Value=v, Type="SecureString", Overwrite=True)

        gum = _load_module(GUMROAD_PATH, "gumroad_wh")
        sell = _load_module(SELLHUB_PATH, "sellhub_wh")

        # Stub network egress. provision_license just returns a fresh key + counts.
        state = {"provision_calls": 0}

        # provision_license returns the WHOLE backend reply (it grew `renewed`
        # when the monthly membership became recurring), not just the key.
        def fake_provision(plan, days, discord_user_id, order_id, renew=False):
            state["provision_calls"] += 1
            state["last_renew"] = renew
            key = f"ORION-MINT-{state['provision_calls']:04d}"
            return {"ok": True, "license_key": key, "plan": plan, "renewed": bool(renew)}

        monkeypatch.setattr(gum, "provision_license", fake_provision)
        monkeypatch.setattr(gum, "send_discord_dm", lambda *a, **k: None)
        monkeypatch.setattr(gum, "verify_license_key", lambda *a, **k: True)

        yield {"gum": gum, "sell": sell, "state": state}

        for name in ("gumroad_wh", "sellhub_wh"):
            sys.modules.pop(name, None)


def _gumroad_event(license_key, sale_id, token=GUMROAD_TOKEN):
    form = {
        "sale_id": sale_id,
        "seller_id": "4-7tKV7OFXGk7mm0h87eFQ==",   # == GUMROAD_SELLER_ID constant
        "product_permalink": "orion-30day",
        "license_key": license_key,
        "email": "buyer@test.com",
        "Discord ID": "123456789",
    }
    return {
        "pathParameters": {"token": token},
        "body": urllib.parse.urlencode(form),
        "isBase64Encoded": False,
    }


class TestGumroadReplay:
    def test_replayed_key_mints_one_license(self, webhooks):
        gum = webhooks["gum"]; state = webhooks["state"]
        # Genuine purchase.
        r1 = gum.lambda_handler(_gumroad_event("KEY-AAAA", "sale-1"), None)
        assert r1["statusCode"] == 200
        assert json.loads(r1["body"]).get("success") is True
        # Forged replay: SAME license_key, FRESH sale_id.
        r2 = gum.lambda_handler(_gumroad_event("KEY-AAAA", "sale-2-forged"), None)
        assert r2["statusCode"] == 200
        assert json.loads(r2["body"]).get("reason") == "license_key_already_minted"
        # Only ONE mint happened.
        assert state["provision_calls"] == 1

    def test_wrong_url_token_rejected(self, webhooks):
        gum = webhooks["gum"]
        r = gum.lambda_handler(_gumroad_event("KEY-BBBB", "sale-9", token="wrong-token"), None)
        assert r["statusCode"] == 403
        assert json.loads(r["body"])["error"] == "invalid_token"


class TestGumroadTimingSafeToken:
    """NEW-3 (revert-catcher): the URL token MUST be compared with
    hmac.compare_digest, not `==`/`!=`. `test_wrong_url_token_rejected` alone is
    tautological — a plain `!=` rejects a wrong token just as well. Here we spy on
    the compare_digest name in the webhook module's own namespace and assert it is
    actually exercised on the token-verification path of a VALID request.

    Revert caught: change `hmac.compare_digest(token, expected_token)` back to
    `token != expected_token` and compare_digest is never called with the token
    pair -> `seen` has no matching entry -> this test FAILS.
    """
    def test_compare_digest_invoked_on_valid_token(self, webhooks, monkeypatch):
        gum = webhooks["gum"]
        real = gum.hmac.compare_digest
        seen = []

        def spy(a, b):
            seen.append((a, b))
            return real(a, b)

        # Patch the name as the webhook module resolves it (`gum.hmac` is the hmac
        # module object; setattr swaps compare_digest for the spy for this request).
        monkeypatch.setattr(gum.hmac, "compare_digest", spy)

        r = gum.lambda_handler(_gumroad_event("KEY-TS", "sale-ts-1"), None)
        # Valid token + fully-stubbed downstream -> a normal successful mint.
        assert r["statusCode"] == 200
        assert json.loads(r["body"]).get("success") is True
        # The token check compares (path_token, expected_token); with a valid
        # request both are GUMROAD_TOKEN. A `!=` revert would never reach here.
        assert any(a == GUMROAD_TOKEN and b == GUMROAD_TOKEN for (a, b) in seen), (
            "hmac.compare_digest was NOT called on the token-verification path — "
            "the timing-safe compare has been bypassed (e.g. reverted to `!=`)."
        )


def _sellhub_event(event_id, order_id, secret=SELLHUB_SECRET,
                   product_id="2577936c-b403-46f8-ae8e-36a65b424f02"):
    body = json.dumps({"event_id": event_id, "order": {
        "id": order_id, "status": "paid",
        "line_items": [{"product_id": product_id}],
        "custom_fields": {"Discord ID": "42"}}})
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return {"body": body, "isBase64Encoded": False,
            "headers": {"x-sellhub-signature": f"sha256={sig}"}}


class TestOrphanedPaidKeyRetry:
    """V3 MED (revert-catcher): on a provision failure the idempotency claim is
    RELEASED so the retry re-mints, instead of orphaning a paid key behind a
    stale 'already minted' / 'duplicate' marker.

      Gumroad: the `lickey:<key>` claim is deleted on failure.
      SellHub: the Layer-1 `event_id` claim is deleted on failure.

    Revert caught: delete the `events_table.delete_item(...)` line in the failure
    branch and the retry hits the surviving marker -> skip (no mint) -> FAIL.
    """

    def test_gumroad_lickey_released_on_failure_then_retry_mints(self, webhooks, monkeypatch):
        gum = webhooks["gum"]
        calls = {"n": 0}

        def flaky(plan, days, discord_user_id, order_id, renew=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient provision failure")
            return {"ok": True, "license_key": f"ORION-RETRY-{calls['n']:04d}",
                    "plan": plan, "renewed": bool(renew)}

        monkeypatch.setattr(gum, "provision_license", flaky)

        # First ping: provision fails -> 500, and the lickey claim must be released.
        r1 = gum.lambda_handler(_gumroad_event("KEY-ORPHAN", "sale-orphan-1"), None)
        assert r1["statusCode"] == 500
        assert json.loads(r1["body"])["error"] == "provision_failed"
        marker = gum.events_table.get_item(
            Key={"event_key": "lickey:KEY-ORPHAN"}).get("Item")
        assert marker is None, "lickey claim was NOT released after provision failure"

        # Retry the SAME license_key with a FRESH sale_id (Gumroad's per-sale
        # `event_key` marker is a separate, pre-existing dedupe that the
        # lickey-release fix intentionally does not touch — see residual note).
        # provision now succeeds -> the license IS minted (no orphan, no false
        # "already minted" skip).
        r2 = gum.lambda_handler(_gumroad_event("KEY-ORPHAN", "sale-orphan-2"), None)
        assert r2["statusCode"] == 200
        b2 = json.loads(r2["body"])
        assert b2.get("success") is True
        assert b2.get("reason") != "license_key_already_minted"
        assert calls["n"] == 2, "retry did not re-attempt the mint"

    def test_sellhub_event_id_released_on_failure_then_retry_mints(self, webhooks, monkeypatch):
        sell = webhooks["sell"]
        calls = {"n": 0}

        def flaky(plan, days, discord_user_id, order_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient provision failure")
            return f"ORION-RETRY-{calls['n']:04d}"

        monkeypatch.setattr(sell, "provision_license", flaky)
        monkeypatch.setattr(sell, "send_discord_dm", lambda *a, **k: None)

        ev = _sellhub_event("ev-orphan", "ord-orphan")

        # First ping: provision fails -> 500, and the event_id claim must be released.
        r1 = sell.lambda_handler(ev, None)
        assert r1["statusCode"] == 500
        assert json.loads(r1["body"])["error"] == "provision_failed"
        marker = sell.events_table.get_item(
            Key={"event_id": "ev-orphan"}).get("Item")
        assert marker is None, "event_id claim was NOT released after provision failure"

        # Re-send the SAME ping: order_id was never persisted (Layer-2 is written
        # only after a successful mint) and the event_id claim was released, so the
        # retry re-mints rather than returning duplicate_event_id.
        r2 = sell.lambda_handler(ev, None)
        assert r2["statusCode"] == 200
        b2 = json.loads(r2["body"])
        assert b2.get("success") is True
        assert calls["n"] == 2, "retry did not re-attempt the mint"


class TestSellhubSignaturePrefix:
    def test_prefix_strip_not_charset(self, webhooks):
        sell = webhooks["sell"]
        body = b'{"event_id":"e1","order":{"id":"o1","status":"paid"}}'
        digest = hmac.new(SELLHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
        # A digest that starts with chars in {s,h,a,2,5,6,=} would be corrupted by
        # lstrip("sha256="). Verify the "sha256=<digest>" header form works.
        assert sell.verify_signature(body, f"sha256={digest}", SELLHUB_SECRET) is True
        # And the bare-digest form (no prefix) still works.
        assert sell.verify_signature(body, digest, SELLHUB_SECRET) is True

    def test_digest_leading_hex_not_eaten(self, webhooks):
        sell = webhooks["sell"]
        # Find a (body, secret) whose digest starts with a char lstrip would eat.
        found = False
        for i in range(200):
            body = f'{{"n":{i}}}'.encode()
            d = hmac.new(SELLHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
            if d[0] in "sha256=":
                assert sell.verify_signature(body, f"sha256={d}", SELLHUB_SECRET) is True
                found = True
                break
        assert found, "no digest with a strippable leading char in sample (unlikely)"


class TestSellhubErrorHygiene:
    def test_provision_error_body_is_generic(self, webhooks, monkeypatch):
        sell = webhooks["sell"]

        def boom(*a, **k):
            raise RuntimeError("secret-table-name boto internal detail")
        monkeypatch.setattr(sell, "provision_license", boom)
        monkeypatch.setattr(sell, "send_discord_dm", lambda *a, **k: None)

        body = json.dumps({"event_id": "e2", "order": {
            "id": "o2", "status": "paid",
            "line_items": [{"product_id": "2577936c-b403-46f8-ae8e-36a65b424f02"}],
            "custom_fields": {"Discord ID": "42"}}})
        raw = body.encode()
        sig = hmac.new(SELLHUB_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        event = {"body": body, "isBase64Encoded": False,
                 "headers": {"x-sellhub-signature": f"sha256={sig}"}}
        r = sell.lambda_handler(event, None)
        assert r["statusCode"] == 500
        parsed = json.loads(r["body"])
        assert parsed == {"error": "provision_failed"}   # no 'detail' leak
        assert "detail" not in parsed
