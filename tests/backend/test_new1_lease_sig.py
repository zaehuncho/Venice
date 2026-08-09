"""NEW-1: the heartbeat lease is Ed25519-signed so the client can trust it."""
import time
from conftest import invoke, put_license, verify_lease_sig


class TestLeaseSignature:
    def test_check_returns_valid_lease_signature(self, lf):
        put_license(lf, "ORION-LEASE-BBBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LEASE-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        assert "lease_sig" in body
        assert body["public_key_id"] == "orion-lease-ed25519-v1"
        assert verify_lease_sig("ORION-LEASE-BBBB-CCCC", "MACHINE-1",
                                body["lease_expires_at"], body["lease_sig"]) is True

    def test_forged_lease_expiry_does_not_verify(self, lf):
        # A MITM bumping lease_expires_at cannot keep the signature valid.
        put_license(lf, "ORION-LEASE2-BBBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LEASE2-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 200
        forged_expiry = body["lease_expires_at"] + 999999
        assert verify_lease_sig("ORION-LEASE2-BBBB-CCCC", "MACHINE-1",
                                forged_expiry, body["lease_sig"]) is False

    def test_signature_binds_machine_id(self, lf):
        put_license(lf, "ORION-LEASE3-BBBB-CCCC", machine_id="MACHINE-1")
        status, body, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-LEASE3-BBBB-CCCC", "machine_id": "MACHINE-1"})
        # Same signature must not verify for a different machine_id.
        assert verify_lease_sig("ORION-LEASE3-BBBB-CCCC", "OTHER-MACHINE",
                                body["lease_expires_at"], body["lease_sig"]) is False
