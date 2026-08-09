import time
from conftest import invoke, make_nonce_ts, put_license


class TestActivate:
    def test_happy_path_binds_machine(self, lf):
        put_license(lf, "ORION-AAAA-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-AAAA-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 200
        assert body["ok"] is True
        assert "token" in body
        item = lf.licenses_table().get_item(Key={"license_key": "ORION-AAAA-BBBB-CCCC"}).get("Item", {})
        assert item.get("machine_id") == "MACHINE-1"
        assert int(item.get("activations", 0)) == 1

    def test_reactivate_same_machine_ok(self, lf):
        put_license(lf, "ORION-SAME-BBBB-CCCC", machine_id="MACHINE-1", activations=1, max_devices=1)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-SAME-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 200
        assert body["ok"] is True

    def test_device_mismatch_quota_exhausted(self, lf):
        # device_mismatch only fires once activations >= max_devices
        put_license(lf, "ORION-AAAA-BBBB-CCCC", machine_id="MACHINE-1", activations=1, max_devices=1)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-AAAA-BBBB-CCCC", "machine_id": "MACHINE-2", **nt})
        assert status == 403
        assert body["error"] == "device_mismatch"

    def test_multi_device_within_quota_rebinds(self, lf):
        put_license(lf, "ORION-MULTI-BBBB-CCCC", machine_id="MACHINE-1", activations=1, max_devices=3)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-MULTI-BBBB-CCCC", "machine_id": "MACHINE-2", **nt})
        assert status == 200
        assert body["ok"] is True

    def test_revoked_flag(self, lf):
        put_license(lf, "ORION-REVOKED-BBBB-CCCC", machine_id="", revoked=True)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-REVOKED-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "license_revoked"

    def test_status_revoked(self, lf):
        put_license(lf, "ORION-STATREV-BBBB-CCCC", machine_id="", status="revoked")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-STATREV-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "license_revoked"

    def test_invalid_status(self, lf):
        put_license(lf, "ORION-SUSPEND-BBBB-CCCC", machine_id="", status="suspended")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-SUSPEND-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "license_invalid_status"

    def test_expired_key(self, lf):
        put_license(lf, "ORION-EXPIRED-BBBB-CCCC", machine_id="", expiry=int(time.time()) - 100)
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-EXPIRED-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "license_expired"

    def test_invalid_key(self, lf):
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-NOPE-NOPE-NOPE", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "invalid_key"

    def test_killswitch(self, lf):
        put_license(lf, "ORION-AAAA-BBBB-CCCC", machine_id="")
        lf.config_table().put_item(Item={
            "config_key": "global_kill", "enabled": True, "reason": "maintenance"})
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-AAAA-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 503
        assert body["error"] == "service_disabled: maintenance"

    def test_missing_fields(self, lf):
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "", "machine_id": "", **nt})
        assert status == 400
        assert body["error"] == "license_key and machine_id required"

    def test_machine_id_too_long(self, lf):
        put_license(lf, "ORION-LONGMID-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-LONGMID-BBBB-CCCC", "machine_id": "M" * 300, **nt})
        assert status == 400
        assert body["error"] == "machine_id too long"
