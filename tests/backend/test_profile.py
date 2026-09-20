"""`profile` block on /api/activate and /api/license/check.

The launcher's Profile page ("your Discord ID and how many days you have left")
renders exactly this object. It is a READ-ONLY projection of the existing license
row plus the existing reset-state helper: no new DynamoDB attribute, no schema
change, and it must never appear on a failed verdict.
"""
import time

from conftest import invoke, make_nonce_ts, put_license


PROFILE_KEYS = {"discord_user_id", "discord_username", "plan", "expiry",
                "activated_at", "hwid_resets"}
RESET_KEYS = {"used", "free_total", "free_remaining", "paid_credits"}


def _assert_shape(profile):
    assert set(profile) == PROFILE_KEYS
    assert isinstance(profile["discord_user_id"], str)
    assert isinstance(profile["discord_username"], str)
    assert isinstance(profile["plan"], str)
    assert isinstance(profile["expiry"], int)
    assert isinstance(profile["activated_at"], int)
    resets = profile["hwid_resets"]
    assert set(resets) == RESET_KEYS
    for key in RESET_KEYS:
        assert isinstance(resets[key], int)
        assert resets[key] >= 0


class TestProfileOnActivate:
    def test_activate_returns_profile(self, lf):
        expiry = int(time.time()) + 30 * 86400
        put_license(lf, "ORION-PROF-BBBB-CCCC", machine_id="", plan="month",
                    expiry=expiry, extra={"discord_user_id": "424242424242424242"})
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-PROF-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 200
        profile = body["profile"]
        _assert_shape(profile)
        assert profile["discord_user_id"] == "424242424242424242"
        assert profile["plan"] == "month"
        assert profile["expiry"] == expiry
        # The bind that just happened is the activation stamp, not a previous one.
        assert profile["activated_at"] >= nt["request_timestamp"]
        row = lf.licenses_table().get_item(
            Key={"license_key": "ORION-PROF-BBBB-CCCC"}).get("Item", {})
        assert int(row["last_activated"]) == profile["activated_at"]

    def test_activate_without_discord_entitlement_is_denied(self, lf):
        """A regular key without a Discord subscription cannot reach profile."""
        put_license(lf, "ORION-NODISCORD-BB-CCCC", machine_id="", discord_user_id="",
                    source="admin_deliver", order_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-NODISCORD-BB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "subscription_required"

    def test_failed_activation_carries_no_profile(self, lf):
        put_license(lf, "ORION-REVPROF-BB-CCCC", machine_id="", revoked=True)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-REVPROF-BB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert "profile" not in body

    def test_lifetime_reports_zero_expiry(self, lf):
        put_license(lf, "ORION-LIFE-BBBB-CCCC", machine_id="", plan="lifetime", expiry=0)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-LIFE-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 200
        assert body["profile"]["plan"] == "lifetime"
        assert body["profile"]["expiry"] == 0


class TestProfileOnCheck:
    def test_heartbeat_returns_profile(self, lf):
        expiry = int(time.time()) + 14 * 86400
        put_license(lf, "ORION-HBPROF-BB-CCCC", machine_id="MACHINE-1", plan="month",
                    expiry=expiry,
                    extra={"discord_user_id": "1234", "discord_username": "isaiah",
                           "last_activated": 1700000000})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-HBPROF-BB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        profile = body["profile"]
        _assert_shape(profile)
        assert profile["discord_user_id"] == "1234"
        assert profile["discord_username"] == "isaiah"
        assert profile["plan"] == "month"
        assert profile["expiry"] == expiry == body["expiry"]
        assert profile["activated_at"] == 1700000000

    def test_hwid_reset_allowance_is_the_owner_rule(self, lf):
        """Three free resets per key, then paid credit / deduct. The block reports
        the same counters the /api/bot/hwid-reset state machine spends."""
        put_license(lf, "ORION-RESETS-BB-CCCC", machine_id="MACHINE-1",
                    extra={"hwid_resets_used": 1, "hwid_paid_credits": 2})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-RESETS-BB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        resets = body["profile"]["hwid_resets"]
        assert resets["used"] == 1
        assert resets["free_total"] == 3
        assert resets["free_remaining"] == 2
        assert resets["paid_credits"] == 2

    def test_exhausted_free_allowance_never_goes_negative(self, lf):
        put_license(lf, "ORION-SPENT-BBB-CCCC", machine_id="MACHINE-1",
                    extra={"hwid_resets_used": 9})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-SPENT-BBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        resets = body["profile"]["hwid_resets"]
        assert resets["used"] == 9
        assert resets["free_remaining"] == 0

    def test_never_activated_key_reports_zero(self, lf):
        put_license(lf, "ORION-NEVER-BBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-NEVER-BBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert body["profile"]["activated_at"] == 0

    def test_legacy_activated_at_attribute_is_accepted(self, lf):
        put_license(lf, "ORION-LEGACY-BB-CCCC", machine_id="MACHINE-1",
                    extra={"activated_at": 1690000000})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LEGACY-BB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert body["profile"]["activated_at"] == 1690000000

    def test_failed_heartbeat_carries_no_profile(self, lf):
        put_license(lf, "ORION-KILLED-BB-CCCC", machine_id="MACHINE-1", status="revoked")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-KILLED-BB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert "profile" not in body
