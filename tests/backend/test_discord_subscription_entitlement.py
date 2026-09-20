"""Discord-account entitlement is the authority for launcher access.

A syntactically valid key is only a credential for locating an account.  Paid
access comes from an active Gumroad subscription tied to that key's Discord ID;
trial and verified lifetime purchases remain explicit entitlement types.
"""

import time

from conftest import invoke, make_nonce_ts


def _put(lf, key, *, discord_id=None, source="admin_deliver", plan="month",
         expiry=None, machine_id="", order_id=None, status="active", revoked=False):
    now = int(time.time())
    item = {
        "license_key": key,
        "status": status,
        "revoked": revoked,
        "plan": plan,
        "machine_id": machine_id,
        "expiry": now + 30 * 86400 if expiry is None else expiry,
        "created_at": now,
        "activations": 0,
        "max_devices": 1,
        "source": source,
    }
    if discord_id is not None:
        item["discord_user_id"] = str(discord_id)
    if order_id is not None:
        item["order_id"] = str(order_id)
    lf.licenses_table().put_item(Item=item)
    return item


def _activate(lf, key, machine="PC-1"):
    return invoke(lf, "POST", "/api/activate", body={
        "license_key": key, "machine_id": machine, **make_nonce_ts()})[:2]


class TestDiscordSubscriptionEntitlement:
    def test_regular_unsubscribed_key_is_denied(self, lf):
        _put(lf, "ORION-REGULAR-NO-SUB")
        status, body = _activate(lf, "ORION-REGULAR-NO-SUB")
        assert status == 403
        assert body["error"] == "subscription_required"

    def test_discord_id_without_paid_subscription_is_denied(self, lf):
        _put(lf, "ORION-DISCORD-NO-SUB", discord_id="10001")
        status, body = _activate(lf, "ORION-DISCORD-NO-SUB")
        assert status == 403
        assert body["error"] == "subscription_required"

    def test_gumroad_subscription_tied_to_discord_id_is_allowed(self, lf):
        _put(lf, "ORION-SUBSCRIBED", discord_id="10002", source="gumroad",
             order_id="sale-active")
        status, body = _activate(lf, "ORION-SUBSCRIBED")
        assert status == 200
        assert body["ok"] is True

    def test_regular_key_uses_same_discord_accounts_active_subscription(self, lf):
        _put(lf, "ORION-PAID-SIBLING", discord_id="10003", source="gumroad",
             order_id="sale-sibling")
        _put(lf, "ORION-REPLACEMENT", discord_id="10003")
        status, body = _activate(lf, "ORION-REPLACEMENT")
        assert status == 200
        assert body["ok"] is True

    def test_expired_subscription_does_not_authorize_regular_key(self, lf):
        _put(lf, "ORION-EXPIRED-SIBLING", discord_id="10004", source="gumroad",
             order_id="sale-expired", expiry=int(time.time()) - 1)
        _put(lf, "ORION-REGULAR-EXPIRED-ACCOUNT", discord_id="10004")
        status, body = _activate(lf, "ORION-REGULAR-EXPIRED-ACCOUNT")
        assert status == 403
        assert body["error"] == "subscription_required"

    def test_discord_trial_is_allowed_without_paid_subscription(self, lf):
        _put(lf, "ORION-TRIAL", discord_id="10005", source="trial", plan="trial")
        status, body = _activate(lf, "ORION-TRIAL")
        assert status == 200
        assert body["ok"] is True

    def test_verified_lifetime_purchase_is_allowed(self, lf):
        _put(lf, "ORION-LIFETIME", discord_id="10006", source="gumroad",
             plan="lifetime", expiry=0, order_id="sale-lifetime")
        status, body = _activate(lf, "ORION-LIFETIME")
        assert status == 200
        assert body["ok"] is True

    def test_heartbeat_revokes_an_unsubscribed_running_key(self, lf):
        _put(lf, "ORION-RUNNING-NO-SUB", discord_id="10007", machine_id="PC-7")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-RUNNING-NO-SUB", "machine_id": "PC-7"})
        assert status == 403
        assert body["error"] == "subscription_required"
