"""§5 revoke-fast (BREAKING): /api/license/check returns machine-readable CODES
(the six the launcher already kills on, plus frozen / blacklisted /
version_blocked) and moves the prose into `message`."""
import time
from conftest import invoke, put_license, make_nonce_ts


class TestValidate:
    def test_happy_path(self, lf):
        put_license(lf, "ORION-VALID-BBBB-CCCC", machine_id="MACHINE-1", extra={"tier": "pro"})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-VALID-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert body["ok"] is True
        assert body["license_key"] == "ORION-VALID-BBBB-CCCC"
        assert body["machine_id"] == "MACHINE-1"
        assert body["tier"] == "pro"
        assert body["lease_expires_at"] > int(time.time())

    def test_default_tier_standard(self, lf):
        put_license(lf, "ORION-DEFTIER-BBBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-DEFTIER-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert body["tier"] == "standard"

    def test_machine_mismatch(self, lf):
        put_license(lf, "ORION-MISMATCH-BBBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-MISMATCH-BBBB-CCCC", "machine_id": "MACHINE-2"})
        assert status == 403
        assert body["error"] == "device_mismatch"

    def test_unbound_machine_rejected(self, lf):
        # machine_id defaults to "" until /api/activate binds it; check never auto-binds
        put_license(lf, "ORION-UNBOUND-BBBB-CCCC", machine_id="")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-UNBOUND-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "device_mismatch"

    def test_revoked_flag(self, lf):
        put_license(lf, "ORION-REVOKED-BBBB-CCCC", machine_id="MACHINE-1", revoked=True)
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-REVOKED-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "revoked"

    def test_status_revoked(self, lf):
        put_license(lf, "ORION-STATREV-BBBB-CCCC", machine_id="MACHINE-1", status="revoked")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-STATREV-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "revoked"

    def test_invalid_key(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-NOPE-NOPE-NOPE", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "invalid_key"

    def test_missing_fields(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "", "machine_id": ""})
        assert status == 400
        assert body["error"] == "license_key and machine_id required"

    def test_killswitch(self, lf):
        put_license(lf, "ORION-VALID-BBBB-CCCC", machine_id="MACHINE-1")
        lf.config_table().put_item(Item={
            "config_key": "global_kill", "enabled": True, "reason": "maintenance"})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-VALID-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 503
        assert body["error"] == "service_disabled"

    def test_expired_license_rejected(self, lf):
        # An expired (e.g. lapsed trial/short plan) license must not keep
        # renewing its heartbeat lease.
        put_license(lf, "ORION-EXPIRED-BBBB-CCCC", machine_id="MACHINE-1",
                    expiry=int(time.time()) - 100)
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-EXPIRED-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "expired"

    def test_lifetime_expiry_zero_accepted(self, lf):
        put_license(lf, "ORION-LIFE-BBBB-CCCC", machine_id="MACHINE-1",
                    plan="lifetime", expiry=0)
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LIFE-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert body["ok"] is True

    def test_legacy_row_without_discord_subscription_is_denied(self, lf):
        # A legacy bare key is not a current Discord subscription entitlement.
        lf.licenses_table().put_item(Item={
            "license_key": "ORION-LEGACY-BBBB-CCCC", "status": "active",
            "revoked": False, "machine_id": "MACHINE-1"})
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LEGACY-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 403
        assert body["error"] == "subscription_required"


class TestVerify:
    def _activate(self, lf, key):
        put_license(lf, key, machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": key, "machine_id": "MACHINE-1", **nt})
        assert status == 200
        return body["token"], body["tid"]

    def test_valid_session_token_ok(self, lf):
        token, tid = self._activate(lf, "ORION-VERIFY-BBBB-CCCC")
        status, body, _ = invoke(lf, "POST", "/api/verify", body={
            "token": token, "token_id": tid})
        assert status == 200
        assert body["valid"] is True

    def test_verify_rejects_license_expired_after_activation(self, lf):
        # Session tokens live 30 days; a 1-day trial that expires mid-session
        # must be cut off at the next verify.
        token, tid = self._activate(lf, "ORION-VEXPIRE-BBBB-CCCC")
        lf.licenses_table().update_item(
            Key={"license_key": "ORION-VEXPIRE-BBBB-CCCC"},
            UpdateExpression="SET expiry = :e",
            ExpressionAttributeValues={":e": int(time.time()) - 100})
        status, body, _ = invoke(lf, "POST", "/api/verify", body={
            "token": token, "token_id": tid})
        assert status == 403
        assert body["error"] == "license_expired"

    def test_verify_rejects_revoked_license(self, lf):
        token, tid = self._activate(lf, "ORION-VREVOKE-BBBB-CCCC")
        lf.licenses_table().update_item(
            Key={"license_key": "ORION-VREVOKE-BBBB-CCCC"},
            UpdateExpression="SET revoked = :r",
            ExpressionAttributeValues={":r": True})
        status, body, _ = invoke(lf, "POST", "/api/verify", body={
            "token": token, "token_id": tid})
        assert status == 403
        assert body["error"] == "license_revoked"
