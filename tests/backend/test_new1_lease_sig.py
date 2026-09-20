"""NEW-1: the heartbeat lease is Ed25519-signed so the client can trust it."""
import base64
import time
from conftest import LEASE_PUB_RAW, invoke, put_license, verify_lease_sig


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


# ── Cross-language contract with the C++ client (2026-09-19, B2) ──────────────
# The tests above prove the SERVER is self-consistent; the LeaseGate tests in
# native_orion/tests/AutomationEngineTests.cpp prove the CLIENT is. Neither
# proves the two agree — and if they disagree, a production build stops firing
# ~15 minutes after activation (the heartbeat never refreshes the lease).
#
# This vector is the contract. The identical four constants are pinned in
# AutomationEngineTests.cpp::leaseGateVerifiesPythonSignedLeaseVector, which
# verifies this exact signature with LeaseGate::verifyLeaseSignature and
# re-signs the same lease to prove the canonical message matches byte for byte.
# The key is the deterministic TEST key from conftest (seed
# b"orion-test-lease-signing-key-032") — never the production key.
XLANG_LICENSE_KEY = "ORION-XLANG-TEST-0001"
XLANG_MACHINE_ID = "machine-xlang-0001"
XLANG_LEASE_EXPIRES_AT = 1800000900
XLANG_PUBLIC_KEY_B64 = "XhINspCkRLXHoyUzhl25QLbSNe3iySas/xUbM21flP0="
XLANG_SIGNATURE_B64URL = (
    "R3EJKupYxNV7THtWejSnUs-mM26IZsJd8enfaF7hS4w4xuX5qipno4N2Uwwj1g5wjK_wAy0inC6jeLQL_xC3Cg")


class TestCrossLanguageLeaseVector:
    def test_test_key_is_the_pinned_one(self):
        assert base64.b64encode(LEASE_PUB_RAW).decode() == XLANG_PUBLIC_KEY_B64

    def test_sign_lease_reproduces_the_cpp_fixture(self, lf):
        # Ed25519 is deterministic: the server must emit exactly the signature
        # the C++ test verifies, in base64url with no padding.
        sig = lf.sign_lease(XLANG_LICENSE_KEY, XLANG_MACHINE_ID, XLANG_LEASE_EXPIRES_AT)
        assert sig == XLANG_SIGNATURE_B64URL
        assert "=" not in sig and "+" not in sig and "/" not in sig

    def test_case_and_whitespace_normalisation_matches_the_client(self, lf):
        # LeaseGate::verifyLeaseSignature trims + upper-cases the license key and
        # trims the machine id before verifying, so the server must do the same.
        assert lf.sign_lease("  " + XLANG_LICENSE_KEY.lower() + "  ",
                             " " + XLANG_MACHINE_ID + " ",
                             XLANG_LEASE_EXPIRES_AT) == XLANG_SIGNATURE_B64URL

    def test_vector_verifies_with_the_shared_public_key(self):
        assert verify_lease_sig(XLANG_LICENSE_KEY, XLANG_MACHINE_ID,
                                XLANG_LEASE_EXPIRES_AT, XLANG_SIGNATURE_B64URL) is True
