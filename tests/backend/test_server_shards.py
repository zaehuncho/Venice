import hashlib
import json
import time

import pytest

from conftest import TEST_ADMIN_SECRET, invoke


BUILD_ID = "b" * 64
MACHINE_ID = "machine-fixture-001"
LICENSE_KEY = "ABCD-EFGH-IJKL-MNOP"
TOKEN_ID = "00000000-0000-4000-8000-000000000001"
TOKEN = "customer-session-token-fixture"
SHARD_HEX = "42" * 32


def body_of(response):
    return json.loads(response["body"])


def admin_headers():
    return {"x-orion-admin-secret": TEST_ADMIN_SECRET}


def seed_release_shard(lf, *, build_id=BUILD_ID, expires_at=0, revoked=False):
    encrypted, nonce = lf._shard_encrypt(bytes.fromhex(SHARD_HEX))
    lf.shards_table().put_item(Item={
        "build_id": build_id,
        "encrypted_shard": encrypted,
        "shard_nonce": nonce,
        "created_at": int(time.time()),
        "expires_at": expires_at,
        "activation_count": 0,
        "revoked": revoked,
    })


def seed_customer(lf, *, token=TOKEN, token_id=TOKEN_ID,
                  machine_id=MACHINE_ID, license_updates=None,
                  token_updates=None):
    license_row = {
        "license_key": LICENSE_KEY,
        "status": "active",
        "revoked": False,
        "plan": "trial",
        "source": "trial",
        "discord_user_id": "123456789012345678",
        "machine_id": machine_id,
        "expiry": int(time.time()) + 3600,
    }
    license_row.update(license_updates or {})
    lf.licenses_table().put_item(Item=license_row)
    token_row = {
        "token_id": token_id,
        "license_key": LICENSE_KEY,
        "machine_id": machine_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expires": int(time.time()) + 3600,
        "created_at": int(time.time()),
    }
    token_row.update(token_updates or {})
    lf.tokens_table().put_item(Item=token_row)


def retrieve(lf, *, build_id=BUILD_ID, machine_id=MACHINE_ID,
             token=TOKEN, token_id=TOKEN_ID, edge=False):
    return invoke(
        lf, path="/api/shard/retrieve", edge=edge,
        headers={
            "authorization": f"Bearer {token}",
            "x-orion-token-id": token_id,
        },
        body={"build_id": build_id, "machine_id": machine_id},
    )


class TestShardStore:
    def test_store_encrypts_one_public_release_fragment(self, lf):
        status, body, _ = invoke(
            lf, path="/api/shard/store", headers=admin_headers(),
            body={"build_id": BUILD_ID, "shard": SHARD_HEX,
                  "license_id": "", "hwid_hash": "",
                  "max_activations": 0, "ttl_hours": 0})
        assert status == 201
        assert body == {"ok": True, "build_id": BUILD_ID, "expires_at": None}
        row = lf.shards_table().get_item(Key={"build_id": BUILD_ID})["Item"]
        assert row["encrypted_shard"] != SHARD_HEX
        assert "license_id" not in row
        assert "hwid_hash" not in row
        assert "max_activations" not in row
        assert lf._shard_decrypt(row["encrypted_shard"], row["shard_nonce"]) == bytes.fromhex(SHARD_HEX)

    @pytest.mark.parametrize("shard,error", [
        ("zz", "shard must be hex-encoded"),
        ("00" * 31, "shard must be exactly 32 bytes"),
        ("00" * 33, "shard must be exactly 32 bytes"),
    ])
    def test_store_rejects_bad_fragment(self, lf, shard, error):
        status, body, _ = invoke(
            lf, path="/api/shard/store", headers=admin_headers(),
            body={"build_id": BUILD_ID, "shard": shard})
        assert status == 400
        assert body["error"] == error

    @pytest.mark.parametrize("field,value,error", [
        ("build_id", "not-a-digest", "build_id must be a 64-character hex digest"),
        ("ttl_hours", "later", "ttl_hours must be an integer"),
        ("max_activations", "many", "max_activations must be an integer"),
    ])
    def test_store_rejects_invalid_metadata(self, lf, field, value, error):
        request = {"build_id": BUILD_ID, "shard": SHARD_HEX}
        request[field] = value
        status, body, _ = invoke(
            lf, path="/api/shard/store", headers=admin_headers(), body=request)
        assert status == 400
        assert body["error"] == error

    @pytest.mark.parametrize("extra", [
        {"license_id": LICENSE_KEY},
        {"hwid_hash": "a" * 64},
        {"max_activations": 1},
    ])
    def test_store_rejects_per_customer_release_binding(self, lf, extra):
        request = {"build_id": BUILD_ID, "shard": SHARD_HEX}
        request.update(extra)
        status, body, _ = invoke(
            lf, path="/api/shard/store", headers=admin_headers(), body=request)
        assert status == 400
        assert body["error"] == "public_release_shard_required"

    def test_store_requires_edge_and_owner(self, lf):
        status, body, _ = invoke(
            lf, path="/api/shard/store", edge=False,
            body={"build_id": BUILD_ID, "shard": SHARD_HEX})
        assert status == 403
        assert body["error"] == "forbidden"
        status, body, _ = invoke(
            lf, path="/api/shard/store",
            body={"build_id": BUILD_ID, "shard": SHARD_HEX})
        assert status == 403
        assert body["error"] == "forbidden"


class TestShardRetrieve:
    def test_exact_wire_contract_works_without_edge_secret(self, lf):
        seed_release_shard(lf)
        seed_customer(lf)
        status, body, _ = retrieve(lf, edge=False)
        assert status == 200
        assert body == {"ok": True, "shard": SHARD_HEX}
        row = lf.shards_table().get_item(Key={"build_id": BUILD_ID})["Item"]
        assert int(row["activation_count"]) == 1

    def test_retrieve_never_accepts_license_key_as_authentication(self, lf):
        seed_release_shard(lf)
        seed_customer(lf)
        status, body, _ = invoke(
            lf, path="/api/shard/retrieve", edge=False,
            body={"build_id": BUILD_ID, "machine_id": MACHINE_ID,
                  "license_key": LICENSE_KEY})
        assert status == 401
        assert body["error"] == "missing_token"

    @pytest.mark.parametrize("headers,expected", [
        ({}, "missing_token"),
        ({"authorization": "Bearer wrong", "x-orion-token-id": TOKEN_ID}, "invalid_token"),
        ({"authorization": f"Bearer {TOKEN}"}, "missing_token"),
    ])
    def test_token_denials(self, lf, headers, expected):
        seed_release_shard(lf)
        seed_customer(lf)
        status, body, _ = invoke(
            lf, path="/api/shard/retrieve", edge=False, headers=headers,
            body={"build_id": BUILD_ID, "machine_id": MACHINE_ID})
        assert status in (401, 403)
        assert body["error"] == expected

    def test_expired_token_is_denied(self, lf):
        seed_release_shard(lf)
        seed_customer(lf, token_updates={"expires": int(time.time()) - 1})
        status, body, _ = retrieve(lf)
        assert status == 403
        assert body["error"] == "token_expired"

    def test_token_machine_mismatch_is_denied(self, lf):
        seed_release_shard(lf)
        seed_customer(lf)
        status, body, _ = retrieve(lf, machine_id="different-machine")
        assert status == 403
        assert body["error"] == "machine_mismatch"

    @pytest.mark.parametrize("license_updates,expected", [
        ({"revoked": True}, "license_revoked"),
        ({"status": "inactive"}, "license_inactive"),
        ({"expiry": int(time.time()) - 1}, "license_expired"),
        ({"machine_id": "other-machine"}, "machine_mismatch"),
        ({"source": "manual", "plan": "monthly"}, "subscription_required"),
    ])
    def test_license_denials(self, lf, license_updates, expected):
        seed_release_shard(lf)
        seed_customer(lf, license_updates=license_updates)
        status, body, _ = retrieve(lf)
        assert status == 403
        assert body["error"] == expected

    @pytest.mark.parametrize("row_updates,expected,status_code", [
        ({"revoked": True}, "build_revoked", 403),
        ({"expires_at": int(time.time()) - 1}, "build_expired", 403),
    ])
    def test_build_denials(self, lf, row_updates, expected, status_code):
        seed_release_shard(lf, **row_updates)
        seed_customer(lf)
        status, body, _ = retrieve(lf)
        assert status == status_code
        assert body["error"] == expected

    def test_missing_build_is_update_required(self, lf):
        seed_customer(lf)
        status, body, _ = retrieve(lf)
        assert status == 404
        assert body["error"] == "build_not_found"

    def test_rate_limit_is_per_license_machine_and_fails_closed(self, lf, monkeypatch):
        seed_release_shard(lf)
        seed_customer(lf)
        observed = []

        def deny(scope, subject, **kwargs):
            observed.append((scope, subject, kwargs))
            return False

        monkeypatch.setattr(lf, "rate_limit_ok", deny)
        status, body, _ = retrieve(lf)
        assert status == 429
        assert body["error"] == "rate_limited"
        scope, subject, kwargs = observed[0]
        assert scope == "shard_retrieve"
        assert subject == hashlib.sha256(
            (LICENSE_KEY + "\0" + MACHINE_ID).encode()).hexdigest()
        assert subject != BUILD_ID
        assert kwargs["fail_open"] is False

    def test_global_kill_blocks_fragment(self, lf):
        seed_release_shard(lf)
        seed_customer(lf)
        lf.config_table().put_item(Item={
            "config_key": "global_kill", "enabled": True,
            "reason": "maintenance"})
        status, body, _ = retrieve(lf)
        assert status == 503
        assert body["error"] == "service_disabled"

    def test_fragment_never_appears_in_audit_or_logs(self, lf, capsys):
        seed_release_shard(lf)
        seed_customer(lf)
        status, body, _ = retrieve(lf)
        assert status == 200
        assert body["shard"] == SHARD_HEX
        logs = capsys.readouterr().out
        rows = lf.audit_table().scan().get("Items", [])
        serialized = json.dumps(rows, default=str)
        assert SHARD_HEX not in logs
        assert SHARD_HEX not in serialized
        assert any(row.get("action") == "shard_retrieved" or
                   row.get("event_type") == "shard_retrieved" for row in rows)


class TestShardAdmin:
    def test_revoke_and_status_get_or_post(self, lf):
        seed_release_shard(lf)
        status, body, _ = invoke(
            lf, path="/api/admin/shard/status", method="GET",
            headers=admin_headers(), qs={"build_id": BUILD_ID})
        assert status == 200
        assert body["build_id"] == BUILD_ID
        assert "encrypted_shard" not in body
        assert "shard_nonce" not in body
        assert "license_id" not in body
        assert "hwid_hash" not in body

        status, body, _ = invoke(
            lf, path="/api/admin/shard/status", method="POST",
            headers=admin_headers(), body={"build_id": BUILD_ID})
        assert status == 200
        assert body["revoked"] is False

        status, body, _ = invoke(
            lf, path="/api/admin/shard/revoke", headers=admin_headers(),
            body={"build_id": BUILD_ID})
        assert status == 200
        assert body == {"ok": True, "revoked": True}

        seed_customer(lf)
        status, body, _ = retrieve(lf)
        assert status == 403
        assert body["error"] == "build_revoked"

    @pytest.mark.parametrize("path,method", [
        ("/api/shard/store", "POST"),
        ("/api/admin/shard/revoke", "POST"),
        ("/api/admin/shard/status", "GET"),
        ("/api/admin/shard/status", "POST"),
    ])
    def test_privileged_routes_are_not_edge_exempt(self, lf, path, method):
        status, body, _ = invoke(
            lf, path=path, method=method, edge=False,
            headers=admin_headers(), body={"build_id": BUILD_ID})
        assert status == 403
        assert body["error"] == "forbidden"
