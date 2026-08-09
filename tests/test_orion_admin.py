"""Unit tests for the Orion admin CLI (tools/admin/orion_admin.py).

Everything here runs offline: a FakeTransport stands in for the network so command
flows, confirmation/reason gating, and key masking are exercised without touching
the real backend.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "admin" / "orion_admin.py"
_spec = importlib.util.spec_from_file_location("orion_admin", _MODULE_PATH)
admin = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses can resolve the module during class creation.
sys.modules["orion_admin"] = admin
_spec.loader.exec_module(admin)


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #

class FakeTransport(admin.Transport):
    def __init__(self, responses):
        # responses: list of (matcher(method, url, body) -> bool, ApiResult)
        self.responses = responses
        self.calls = []

    def request(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for matcher, result in self.responses:
            if matcher(method, url, body):
                return result
        return admin.ApiResult(False, 404, {}, "no fake response")


def make_client(transport, **config_kwargs):
    cfg = admin.AdminConfig(admin_secret=config_kwargs.pop("admin_secret", "owner-secret"), **config_kwargs)
    return admin.OrionAdminClient(cfg, transport)


def collect_output():
    lines = []
    return lines, lambda s: lines.append(str(s))


def parse(argv):
    return admin.build_parser().parse_args(argv)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_mask_key_shows_only_last_four():
    assert admin.mask_key("ORION-ABCD-EFGH-1234") == "ORION-…-1234"
    assert admin.mask_key("") == "(none)"
    assert admin.mask_key("abcd") == "****"


def test_compute_extend_expiry_never_shortens():
    now = 1_000_000
    future = now + 100 * 86400
    # extending an active key adds to the existing (future) expiry
    assert admin.compute_extend_expiry(future, now, 30) == future + 30 * 86400
    # extending an expired key adds from now
    assert admin.compute_extend_expiry(now - 86400, now, 10) == now + 10 * 86400


def test_redact_config_hides_secrets():
    cfg = admin.AdminConfig(admin_secret="supersecret", staff_token="zzz999tokenval", discord_user_id="123", role="owner")
    red = admin.redact_config(cfg)
    assert red["admin_secret"] == "set"
    assert red["staff_token"] == "set"
    assert "supersecret" not in json.dumps(red)
    assert "zzz999tokenval" not in json.dumps(red)


def test_is_destructive():
    assert admin.is_destructive("revoke")
    assert admin.is_destructive("reset-hwid")
    assert admin.is_destructive("deactivate")
    assert not admin.is_destructive("lookup")
    assert not admin.is_destructive("create")


def test_role_can_gates_advisory():
    assert admin.role_can("owner", "staff-create")          # owner can do anything
    assert admin.role_can("admin", "revoke")
    assert not admin.role_can("support", "revoke")
    assert admin.role_can("support", "reset-hwid")
    assert admin.role_can(None, "revoke")                   # unknown -> defer to server


def test_build_auth_headers_owner_vs_staff():
    owner = admin.build_auth_headers(admin.AdminConfig(admin_secret="s"))
    assert owner["X-Orion-Admin-Secret"] == "s"
    assert "Authorization" not in owner

    staff = admin.build_auth_headers(admin.AdminConfig(staff_token="t", discord_user_id="42"))
    assert staff["Authorization"] == "Bearer t"
    assert staff["X-Orion-Discord-Id"] == "42"


def test_format_audit_row_masks_full_key():
    row = admin.format_audit_row({
        "timestamp": 1_700_000_000,
        "actor_discord_id": "999",
        "action": "revoke",
        "license_key": "ORION-AAAA-BBBB-7777",
        "target_email": "user@example.com",
        "reason": "chargeback",
    })
    assert "ORION-AAAA-BBBB-7777" not in row
    assert "7777" in row
    assert "chargeback" in row


def test_load_config_env_overrides(tmp_path):
    cfg_path = tmp_path / "admin_config.json"
    cfg_path.write_text(json.dumps({"base_url": "https://file.example", "admin_secret": "filesecret"}), encoding="utf-8")
    env = {"ORION_ADMIN_SECRET": "envsecret", "ORION_API_BASE": "https://env.example"}
    cfg = admin.load_config(cfg_path, env=env)
    assert cfg.admin_secret == "envsecret"     # env wins
    assert cfg.base_url == "https://env.example"


def test_save_config_roundtrip(tmp_path):
    cfg_path = tmp_path / "admin_config.json"
    admin.save_config(admin.AdminConfig(admin_secret="x", role="owner"), cfg_path)
    loaded = admin.load_config(cfg_path, env={})
    assert loaded.admin_secret == "x"
    assert loaded.role == "owner"


# --------------------------------------------------------------------------- #
# Transport guard
# --------------------------------------------------------------------------- #

def test_urllib_transport_refuses_non_https():
    result = admin.UrllibTransport().request("GET", "http://insecure.example/api/version", {}, None)
    assert not result.ok
    assert "non-HTTPS" in result.error


# --------------------------------------------------------------------------- #
# Command flows (offline)
# --------------------------------------------------------------------------- #

def test_create_shows_full_key_once():
    transport = FakeTransport([
        (lambda m, u, b: "/api/provision" in u,
         admin.ApiResult(True, 201, {"license_key": "ORION-FULL-KEY1-9999", "email": "a@b.com",
                                     "plan": "pro", "expiry_date": 1_800_000_000})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    args = parse(["license", "create", "--email", "a@b.com", "--plan", "pro"])
    rc = admin.run(args, client, out, lambda _p: True)
    assert rc == 0
    blob = "\n".join(lines)
    assert "ORION-FULL-KEY1-9999" in blob            # full key shown at creation
    assert transport.calls[0]["body"]["email"] == "a@b.com"


def test_revoke_requires_reason():
    transport = FakeTransport([])
    client = make_client(transport)
    lines, out = collect_output()
    args = parse(["license", "revoke", "--key", "ORION-AAAA-BBBB-1234"])
    rc = admin.run(args, client, out, lambda _p: True)
    assert rc == 1
    assert any("requires --reason" in ln for ln in lines)
    assert transport.calls == []                      # never hit the API


def test_revoke_aborts_when_not_confirmed():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/license" in u, admin.ApiResult(True, 200, {"ok": True})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    args = parse(["license", "revoke", "--key", "ORION-AAAA-BBBB-1234", "--reason", "fraud"])
    rc = admin.run(args, client, out, lambda _p: False)   # user declines confirmation
    assert rc == 1
    assert any("Aborted" in ln for ln in lines)
    assert transport.calls == []


def test_revoke_success_sends_reason_and_actor():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/license" in u, admin.ApiResult(True, 200, {"ok": True})),
    ])
    client = make_client(transport, discord_user_id="owner-1")
    lines, out = collect_output()
    args = parse(["--yes", "license", "revoke", "--key", "ORION-AAAA-BBBB-1234", "--reason", "fraud"])
    rc = admin.run(args, client, out, lambda _p: True)
    assert rc == 0
    body = transport.calls[0]["body"]
    assert body["action"] == "revoke"
    assert body["reason"] == "fraud"
    assert body["actor_discord_id"] == "owner-1"
    # output masks the key
    assert all("ORION-AAAA-BBBB-1234" not in ln for ln in lines)


def test_lookup_masks_key_by_default_and_reveals_on_flag():
    lic = {"license": {"license_key": "ORION-AAAA-BBBB-1234", "email": "u@e.com", "plan": "std",
                       "status": "active", "revoked": False, "machine_id": "abcdef123456"}}
    transport = FakeTransport([(lambda m, u, b: "/api/admin/license" in u, admin.ApiResult(True, 200, lic))])
    client = make_client(transport)

    lines, out = collect_output()
    admin.run(parse(["license", "lookup", "--key", "ORION-AAAA-BBBB-1234"]), client, out, lambda _p: True)
    assert all("ORION-AAAA-BBBB-1234" not in ln for ln in lines)
    assert any("ORION-…-1234" in ln for ln in lines)

    lines2, out2 = collect_output()
    admin.run(parse(["license", "lookup", "--key", "ORION-AAAA-BBBB-1234", "--reveal"]), client, out2, lambda _p: True)
    assert any("ORION-AAAA-BBBB-1234" in ln for ln in lines2)


def test_deactivate_requires_reason():
    transport = FakeTransport([])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["license", "deactivate", "--key", "ORION-AAAA-BBBB-1234"]), client, out, lambda _p: True)
    assert rc == 1
    assert transport.calls == []


def test_extend_computes_new_expiry_from_current():
    current = {"license": {"expiry": 2_000_000_000}}
    captured = {}

    def capture(m, u, b):
        if b is not None and "expiry" in b:
            captured.update(b)
            return True
        return False

    transport = FakeTransport([
        (lambda m, u, b: m == "GET" and "/api/admin/license" in u, admin.ApiResult(True, 200, current)),
        (capture, admin.ApiResult(True, 200, {"ok": True})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["license", "extend", "--key", "ORION-AAAA-BBBB-1234", "--days", "30"]), client, out, lambda _p: True)
    assert rc == 0
    assert captured["expiry"] == 2_000_000_000 + 30 * 86400


def test_search_degrades_gracefully_when_not_deployed():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/search" in u, admin.ApiResult(False, 404, {}, "Not Found")),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["license", "lookup", "--email", "u@e.com"]), client, out, lambda _p: True)
    assert rc == 1
    assert any("staff backend" in ln for ln in lines)


def test_license_requires_authentication():
    transport = FakeTransport([])
    client = admin.OrionAdminClient(admin.AdminConfig(), transport)  # no secret/token
    lines, out = collect_output()
    rc = admin.run(parse(["license", "lookup", "--key", "ORION-AAAA-BBBB-1234"]), client, out, lambda _p: True)
    assert rc == 1
    assert any("Not authenticated" in ln for ln in lines)
    assert transport.calls == []


def test_staff_disable_requires_reason_and_confirmation():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/staff" in u, admin.ApiResult(True, 200, {"ok": True})),
    ])
    client = make_client(transport)
    # missing reason
    lines, out = collect_output()
    rc = admin.run(parse(["staff", "disable", "--discord", "555"]), client, out, lambda _p: True)
    assert rc == 1
    assert transport.calls == []


def test_killswitch_status_reports_engaged_state():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/status" in u,
         admin.ApiResult(True, 200, {"global_kill": True, "kill_reason": "maintenance"})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["killswitch", "status"]), client, out, lambda _p: True)
    assert rc == 0
    assert any("ENGAGED" in ln and "maintenance" in ln for ln in lines)


def test_killswitch_engage_requires_reason():
    transport = FakeTransport([])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["killswitch", "engage"]), client, out, lambda _p: True)
    assert rc == 1
    assert any("requires --reason" in ln for ln in lines)
    assert transport.calls == []


def test_killswitch_engage_aborts_when_not_confirmed():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/kill" in u, admin.ApiResult(True, 200, {"ok": True})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    args = parse(["killswitch", "engage", "--reason", "test"])
    rc = admin.run(args, client, out, lambda _p: False)
    assert rc == 1
    assert any("Aborted" in ln for ln in lines)
    assert transport.calls == []


def test_killswitch_engage_success_sends_global_target():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/kill" in u, admin.ApiResult(True, 200, {"ok": True, "global_kill": True})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    args = parse(["--yes", "killswitch", "engage", "--reason", "incident"])
    rc = admin.run(args, client, out, lambda _p: True)
    assert rc == 0
    body = transport.calls[0]["body"]
    assert body["target_type"] == "global"
    assert body["reason"] == "incident"
    assert any("engaged" in ln for ln in lines)


def test_killswitch_release_sends_global_target():
    transport = FakeTransport([
        (lambda m, u, b: "/api/admin/unkill" in u, admin.ApiResult(True, 200, {"ok": True, "global_kill": False})),
    ])
    client = make_client(transport)
    lines, out = collect_output()
    rc = admin.run(parse(["killswitch", "release"]), client, out, lambda _p: True)
    assert rc == 0
    assert transport.calls[0]["body"] == {"target_type": "global"}
    assert any("released" in ln.lower() for ln in lines)


def test_killswitch_requires_authentication():
    transport = FakeTransport([])
    client = admin.OrionAdminClient(admin.AdminConfig(), transport)  # no secret/token
    lines, out = collect_output()
    rc = admin.run(parse(["killswitch", "status"]), client, out, lambda _p: True)
    assert rc == 1
    assert any("Not authenticated" in ln for ln in lines)
    assert transport.calls == []
