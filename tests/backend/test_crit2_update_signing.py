"""CRIT-2: /api/update is no longer a signing oracle.

The Lambda must NOT sign attacker-supplied artifact_url/sha256. It now stores a
PRE-SIGNED manifest, verifies the signature with the PUBLIC key, and refuses any
artifact_url outside the owned allow-list.
"""
from conftest import invoke, sign_update_manifest_canonical, TEST_ADMIN_SECRET

ALLOWED_URL = "https://orion-releases.s3.amazonaws.com/v1.2.3/orion.zip"
EVIL_URL    = "https://evil.example.com/trojan.zip"

ADMIN_H = {"x-orion-admin-secret": TEST_ADMIN_SECRET}


def _manifest(artifact_url=ALLOWED_URL, sha="a" * 64):
    return {
        "latest_version": "1.2.3",
        "minimum_supported_version": "1.0.0",
        "artifact_url": artifact_url,
        "sha256": sha,
        "published_at": "2026-07-17T00:00:00Z",
        "mandatory": False,
        "allow_rollback": False,
        "public_key_id": "orion-ed25519-v1",
    }


class TestUpdateSigningOracleClosed:
    def test_presigned_allowed_host_accepted(self, lf):
        m = _manifest()
        m["signature"] = sign_update_manifest_canonical(m)
        status, body, _ = invoke(lf, "POST", "/api/update", body=m, headers=ADMIN_H)
        assert status == 200
        assert body["ok"] is True
        assert body["signature"] == m["signature"]
        # And it is served back verbatim.
        s2, b2, _ = invoke(lf, "GET", "/api/update", headers=ADMIN_H)
        assert s2 == 200
        assert b2["artifact_url"] == ALLOWED_URL
        assert b2["signature"] == m["signature"]

    def test_unsigned_manifest_rejected(self, lf):
        # No signature field at all -> the Lambda will NOT mint one.
        m = _manifest()
        status, body, _ = invoke(lf, "POST", "/api/update", body=m, headers=ADMIN_H)
        assert status == 400
        assert "signature" in body["error"]

    def test_disallowed_artifact_host_rejected(self, lf):
        m = _manifest(artifact_url=EVIL_URL)
        m["signature"] = sign_update_manifest_canonical(m)  # even a valid sig can't help
        status, body, _ = invoke(lf, "POST", "/api/update", body=m, headers=ADMIN_H)
        assert status == 400
        assert body["error"] == "artifact_url_not_allowed"

    def test_http_scheme_rejected(self, lf):
        m = _manifest(artifact_url="http://orion-releases.s3.amazonaws.com/x.zip")
        m["signature"] = sign_update_manifest_canonical(m)
        status, body, _ = invoke(lf, "POST", "/api/update", body=m, headers=ADMIN_H)
        assert status == 400
        assert body["error"] == "artifact_url_not_allowed"

    def test_tampered_signature_rejected(self, lf):
        # Sign the honest manifest, then tamper the sha256 -> signature no longer
        # matches the canonical bytes -> rejected (cannot re-sign server-side).
        m = _manifest()
        m["signature"] = sign_update_manifest_canonical(m)
        m["sha256"] = "b" * 64
        status, body, _ = invoke(lf, "POST", "/api/update", body=m, headers=ADMIN_H)
        assert status == 400
        assert body["error"] == "invalid_signature"

    def test_admin_secret_required(self, lf):
        m = _manifest()
        m["signature"] = sign_update_manifest_canonical(m)
        status, body, _ = invoke(lf, "POST", "/api/update", body=m,
                                 headers={"x-orion-admin-secret": "wrong"})
        assert status == 403
