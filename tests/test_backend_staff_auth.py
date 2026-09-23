"""Offline tests for Orion backend staff/auth security contract.

The live Lambda uses DynamoDB + SSM. These tests monkeypatch those boundaries so
the staff security contract can be verified without touching AWS (and without
moto — the heavier, moto-backed integration suite lives in tests/backend/).

REWRITTEN 2026-07-05 against the v0.4.x backend that now lives in
backend/lambda_function.py. The previous version of this file tested the old
v0.1.0-era staff design (stateless HMAC "staff.<b64>.<sig>" tokens,
get_token_secret/make_staff_token/verify_staff_token/role_allows, per-staff
salted one-time enrollment keys). That design was NEVER DEPLOYED; the repo file
was intentionally replaced with the verbatim deployed bundle (see
discord_launch/LAMBDA_RECONCILIATION.md). The new design:

  * opaque random staff tokens stored server-side (orion-tokens, sha256 hash,
    token_hash-index GSI) — require_staff() resolves the CURRENT staff record
    from DynamoDB on every request (role/disabled always server-side truth),
  * one global enrollment key, verified against a sha256 hash held in SSM via
    secrets.compare_digest, role hardcoded to "staff" at enroll,
  * nonce (conditional-put, single-use) + timestamp window on enroll/login,
  * a read-only staff surface (whoami / license lookup / tamper-report) — no
    staff-triggered revoke/reset/deactivate exists at all.

Old properties DROPPED BY DESIGN (not silently — see the audit report):
  * role_allows()/capability matrix: no privileged staff actions remain to
    gate; test_staff_surface_has_no_privileged_license_actions pins that.
  * per-staff single-use/expiring enrollment keys: replaced by the global
    key + staff_id_taken anti-hijack guard + nonce/timestamp replay window;
    the nearest enforced properties are pinned below.
  * JSON {"error": "internal_error"} catch-all on handler exceptions: the
    deployed router has no catch-all (unhandled exceptions become an API
    Gateway 502 — fail-closed but unstructured). Flagged as PROPOSED work,
    not testable without changing deployed behaviour.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "lambda_function.py"
_spec = importlib.util.spec_from_file_location("orion_backend_lambda", _MODULE_PATH)
backend = importlib.util.module_from_spec(_spec)
sys.modules["orion_backend_lambda"] = backend
_spec.loader.exec_module(backend)

UNIT_ENROLL_KEY = "ORION-STAFF-unit-enroll-key"
UNIT_STAFF_SECRET = "unit-staff-token-secret"


# ── AWS-boundary fakes ────────────────────────────────────────────────────────

class FakeTable:
    """Minimal DynamoDB Table fake keyed on a single hash key."""

    def __init__(self, key_name, items=None):
        self.key_name = key_name
        self.items = {i[key_name]: dict(i) for i in (items or [])}
        self.put_history = []

    def get_item(self, Key):
        item = self.items.get(Key[self.key_name])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **_kwargs):
        self.items[Item[self.key_name]] = dict(Item)
        self.put_history.append(dict(Item))
        return {}

    def scan(self, **_kwargs):
        return {"Items": [dict(i) for i in self.items.values()]}

    def update_item(self, Key, UpdateExpression="", ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kwargs):
        # Only what the handlers under test need.
        item = self.items[Key[self.key_name]]
        vals = ExpressionAttributeValues or {}
        if "machine_id = :e" in UpdateExpression and ":e" in vals:
            item["machine_id"] = vals[":e"]
        return {}


class FakeTokensTable(FakeTable):
    """Adds the token_hash-index GSI query require_staff() relies on."""

    def query(self, IndexName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, Limit=1, **_kwargs):
        assert IndexName == "token_hash-index"
        wanted = (ExpressionAttributeValues or {}).get(":h")
        matches = [dict(i) for i in self.items.values() if i.get("token_hash") == wanted]
        return {"Items": matches[:Limit]}


class FakeNoncesTable:
    """Conditional-put semantics: second put of the same nonce raises."""

    def __init__(self):
        self.nonces = set()

    def put_item(self, Item, ConditionExpression=None, **_kwargs):
        nonce = Item["nonce"]
        if ConditionExpression and nonce in self.nonces:
            raise Exception("ConditionalCheckFailedException")
        self.nonces.add(nonce)
        return {}


class Env:
    def __init__(self):
        self.staff = FakeTable("staff_id")
        self.tokens = FakeTokensTable("token_id")
        self.nonces = FakeNoncesTable()
        self.audit = FakeTable("audit_id")
        self.unified_audit = FakeTable("event_id")
        self.config = FakeTable("config_key")
        self.licenses = FakeTable("license_key")


def _fake_ssm_get(name, decrypt=False):
    if name == backend.ENROLL_KEY_SSM:
        return hashlib.sha256(UNIT_ENROLL_KEY.encode()).hexdigest()
    raise RuntimeError(f"unit tests must not read SSM param {name!r}")


@pytest.fixture
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(backend, "staff_table", lambda: e.staff)
    monkeypatch.setattr(backend, "tokens_table", lambda: e.tokens)
    monkeypatch.setattr(backend, "nonces_table", lambda: e.nonces)
    monkeypatch.setattr(backend, "staff_audit_table", lambda: e.audit)
    monkeypatch.setattr(backend, "audit_table", lambda: e.unified_audit)
    monkeypatch.setattr(backend, "config_table", lambda: e.config)
    monkeypatch.setattr(backend, "_config_cache", {})
    monkeypatch.setattr(backend, "licenses_table", lambda: e.licenses)
    monkeypatch.setattr(backend, "get_staff_token_secret", lambda: UNIT_STAFF_SECRET)
    monkeypatch.setattr(backend, "ssm_get", _fake_ssm_get)
    return e


# ── event helpers ─────────────────────────────────────────────────────────────

def make_event(method="POST", path="/api/staff/login", body=None, headers=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": headers or {},
        "body": json.dumps(body or {}),
        "queryStringParameters": {},
    }


def fresh_nonce_body(**overrides):
    body = {"nonce": uuid.uuid4().hex, "timestamp": int(time.time())}
    body.update(overrides)
    return body


def seed_staff(env, staff_id="staff_1", machine_id="machine-a", **overrides):
    item = {
        "staff_id": staff_id,
        "display_name": "Helper",
        "role": "staff",
        "machine_id": machine_id,
        "disabled": False,
        "created_at": int(time.time()),
    }
    item.update(overrides)
    env.staff.put_item(Item=item)
    return item


def login(env, staff_id="staff_1", machine_id="machine-a"):
    resp = backend.handle_staff_login(make_event(
        body=fresh_nonce_body(staff_id=staff_id, machine_id=machine_id)))
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["token"]


def bearer(token, machine_id=None):
    headers = {"authorization": "Bearer " + token}
    if machine_id is not None:
        headers["x-machine-id"] = machine_id
    return headers


# ── enrollment ────────────────────────────────────────────────────────────────

def test_enroll_key_verified_against_hash_and_never_stored(env):
    # Wrong key fails closed, no staff record is created.
    resp = backend.handle_staff_enroll(make_event(body=fresh_nonce_body(
        enroll_key="wrong-key", staff_id="staff_1", machine_id="machine-a")))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "invalid_enroll_key"
    assert env.staff.items == {}

    # Correct key enrolls; a client-supplied role is ignored (server pins "staff").
    resp = backend.handle_staff_enroll(make_event(body=fresh_nonce_body(
        enroll_key=UNIT_ENROLL_KEY, staff_id="staff_1", machine_id="machine-a",
        role="owner")))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["ok"] is True
    assert body["role"] == "staff"
    stored = env.staff.items["staff_1"]
    assert stored["role"] == "staff"
    # The enrollment secret must never be persisted anywhere.
    everything = json.dumps(env.staff.items) + json.dumps(env.audit.items) \
        + json.dumps(env.tokens.items)
    assert UNIT_ENROLL_KEY not in everything


def test_staff_enroll_cannot_hijack_existing_staff_id(env):
    first = backend.handle_staff_enroll(make_event(body=fresh_nonce_body(
        enroll_key=UNIT_ENROLL_KEY, staff_id="staff_1", machine_id="machine-a")))
    assert first["statusCode"] == 200

    # Even with the valid global key and a fresh nonce, an existing staff_id
    # cannot be re-enrolled (rebinding it to an attacker machine).
    second = backend.handle_staff_enroll(make_event(body=fresh_nonce_body(
        enroll_key=UNIT_ENROLL_KEY, staff_id="staff_1", machine_id="machine-evil")))
    assert second["statusCode"] == 409
    assert json.loads(second["body"])["error"] == "staff_id_taken"
    assert env.staff.items["staff_1"]["machine_id"] == "machine-a"


def test_staff_enroll_stale_timestamp_rejected(env):
    resp = backend.handle_staff_enroll(make_event(body=fresh_nonce_body(
        enroll_key=UNIT_ENROLL_KEY, staff_id="staff_1", machine_id="machine-a",
        timestamp=int(time.time()) - 600)))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "timestamp_expired"
    assert env.staff.items == {}


# ── login / nonce replay ──────────────────────────────────────────────────────

def test_staff_login_replay_nonce_is_rejected(env):
    seed_staff(env)
    body = fresh_nonce_body(staff_id="staff_1", machine_id="machine-a")

    first = backend.handle_staff_login(make_event(body=body))
    second = backend.handle_staff_login(make_event(body=body))

    assert first["statusCode"] == 200
    assert second["statusCode"] == 401
    assert json.loads(second["body"])["error"] == "replay_detected"


def test_staff_login_stale_timestamp_rejected(env):
    seed_staff(env)
    resp = backend.handle_staff_login(make_event(body=fresh_nonce_body(
        staff_id="staff_1", machine_id="machine-a",
        timestamp=int(time.time()) - 600)))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "timestamp_expired"


def test_staff_login_bound_machine_mismatch_fails_closed(env):
    seed_staff(env, machine_id="machine-a")
    resp = backend.handle_staff_login(make_event(body=fresh_nonce_body(
        staff_id="staff_1", machine_id="machine-b")))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "machine_mismatch"


# ── token verification ────────────────────────────────────────────────────────

def test_staff_token_round_trip_uses_current_staff_record(env):
    seed_staff(env)
    token = login(env)

    staff, err = backend.require_staff(make_event(
        method="GET", path="/api/staff/whoami",
        headers=bearer(token, "machine-a")))

    assert err is None
    assert staff["staff_id"] == "staff_1"


def test_disabled_staff_token_fails_closed(env):
    seed_staff(env)
    token = login(env)
    env.staff.items["staff_1"]["disabled"] = True

    staff, err = backend.require_staff(make_event(
        headers=bearer(token, "machine-a")))

    assert staff is None
    assert err["statusCode"] == 403
    assert json.loads(err["body"])["error"] == "staff_disabled"


def test_staff_token_wrong_machine_fails_closed(env):
    seed_staff(env)
    token = login(env)  # token bound to machine-a

    staff, err = backend.require_staff(make_event(
        headers=bearer(token, "machine-b")))

    assert staff is None
    assert err["statusCode"] == 403
    assert json.loads(err["body"])["error"] == "machine_mismatch"


def test_staff_role_comes_from_server_not_client(env):
    seed_staff(env, role="staff")
    token = login(env)

    # The opaque token record carries no role at all…
    assert all("role" not in t for t in env.tokens.items.values())

    # …and whoami reflects the CURRENT server-side record, even after it changes.
    env.staff.items["staff_1"]["role"] = "auditor"
    resp = backend.handle_staff_whoami(make_event(
        method="GET", path="/api/staff/whoami", headers=bearer(token, "machine-a")))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["role"] == "auditor"


# ── staff surface is read-only ───────────────────────────────────────────────

def test_staff_surface_has_no_privileged_license_actions(env):
    seed_staff(env)
    token = login(env)
    env.licenses.put_item(Item={
        "license_key": "ORION-UNIT-TEST-KEYS", "status": "active",
        "revoked": False, "plan": "month", "machine_id": "cust-machine",
        "expiry": int(time.time()) + 86400, "activations": 1,
    })

    # lookup (the only supported action) works…
    ok_resp = backend.handle_staff_license(make_event(
        path="/api/staff/license", headers=bearer(token, "machine-a"),
        body={"action": "lookup", "license_key": "ORION-UNIT-TEST-KEYS"}))
    assert ok_resp["statusCode"] == 200

    # …but no mutating action exists on the staff surface at all.
    for action in ("revoke", "reset_hwid", "deactivate", "extend", "set_plan"):
        resp = backend.handle_staff_license(make_event(
            path="/api/staff/license", headers=bearer(token, "machine-a"),
            body={"action": action, "license_key": "ORION-UNIT-TEST-KEYS"}))
        assert resp["statusCode"] == 400, action
        assert json.loads(resp["body"])["error"] == "unknown_action"

    lic = env.licenses.items["ORION-UNIT-TEST-KEYS"]
    assert lic["revoked"] is False
    assert lic["status"] == "active"


def test_staff_license_lookup_returns_suffixes_not_full_ids(env):
    seed_staff(env)
    token = login(env)
    env.licenses.put_item(Item={
        "license_key": "ORION-UNIT-TEST-KEYS", "status": "active",
        "revoked": False, "plan": "month",
        "machine_id": "customer-machine-abcdef123456",
        "expiry": int(time.time()) + 86400, "activations": 1,
    })
    resp = backend.handle_staff_license(make_event(
        path="/api/staff/license", headers=bearer(token, "machine-a"),
        body={"action": "lookup", "license_key": "ORION-UNIT-TEST-KEYS"}))
    assert resp["statusCode"] == 200
    raw = resp["body"]
    assert "customer-machine-abcdef123456" not in raw
    assert json.loads(raw)["license"]["machine_suffix"] == "3456"


# ── router / admin surfaces ───────────────────────────────────────────────────

def test_router_requires_staff_auth_on_whoami(env, monkeypatch):
    monkeypatch.setattr(backend, "require_edge_auth", lambda headers: None)
    resp = backend.lambda_handler(make_event(
        method="GET", path="/api/staff/whoami", headers={}), None)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "missing_token"


def test_admin_staff_list_requires_admin_and_omits_machine_ids(env, monkeypatch):
    seed_staff(env, machine_id="staff-machine-abcdef123456")

    monkeypatch.setattr(backend, "require_admin", lambda _event: False)
    resp = backend.handle_admin_staff(make_event(method="GET", path="/api/admin/staff"))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "forbidden"

    monkeypatch.setattr(backend, "require_admin", lambda _event: True)
    resp = backend.handle_admin_staff(make_event(method="GET", path="/api/admin/staff"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["staff"], "seeded staff member should be listed"
    assert "staff-machine-abcdef123456" not in resp["body"]
    assert all("machine_id" not in s for s in body["staff"])


def test_version_reports_service_identity(env):
    resp = backend.handle_version(make_event(method="GET", path="/api/version"))
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["ok"] is True
    assert body["service"] == "orion-activate"
    assert body["update_signing"] == "ed25519"


# ── tamper reports ────────────────────────────────────────────────────────────

def test_admin_tamper_report_is_audited_without_secret_leak(env, monkeypatch):
    monkeypatch.setattr(backend, "require_admin", lambda _event: True)

    resp = backend.handle_admin_tamper_report(make_event(
        path="/api/admin/tamper-report",
        headers={"x-orion-admin-secret": "unit-admin-secret"},
        body={
            "type": "integrity_lock",
            "details": {"detail": "Hash mismatch: OrionStaff.exe"},
            "machine_id": "admintool-machine-abcdef123456",
        }))

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["ok"] is True
    row = env.audit.put_history[-1]
    assert row["action"] == "admin_tamper_report"
    assert env.unified_audit.put_history[-1]["action"] == "admin_tamper_report"
    assert env.unified_audit.put_history[-1]["event_id"] == row["audit_id"]
    dumped = json.dumps(row)
    assert "admintool-machine-abcdef123456" not in dumped  # suffix only
    assert "3456" in row["details"]
    assert "unit-admin-secret" not in dumped


def test_staff_tamper_report_requires_valid_staff_token_and_audits(env):
    # No token → fail closed.
    resp = backend.handle_staff_tamper_report(make_event(
        path="/api/staff/tamper-report",
        body={"type": "settings_signature_invalid"}))
    assert resp["statusCode"] == 401

    seed_staff(env)
    token = login(env)
    resp = backend.handle_staff_tamper_report(make_event(
        path="/api/staff/tamper-report", headers=bearer(token, "machine-a"),
        body={
            "type": "settings_signature_invalid",
            "details": {"detail": "Settings signature missing or invalid"},
            "machine_id": "staff-machine-abcdef123456",
        }))

    assert resp["statusCode"] == 200
    row = env.audit.put_history[-1]
    assert row["action"] == "staff_tamper_report"
    assert env.unified_audit.put_history[-1]["action"] == "staff_tamper_report"
    assert env.unified_audit.put_history[-1]["event_id"] == row["audit_id"]
    assert row["actor"] == "staff_1"
    assert "staff-machine-abcdef123456" not in json.dumps(row)
    assert "3456" in row["details"]
