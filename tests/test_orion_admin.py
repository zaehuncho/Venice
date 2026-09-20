"""Unit tests for the Orion admin CLI (tools/admin/orion_admin.py).

Everything here runs offline: a FakeTransport stands in for the network so command
flows, confirmation/reason gating, and key masking are exercised without touching
the real backend.

The command-flow tests assert the request each subcommand makes -- method, path
and body -- against docs/ADMIN_PANEL_V2_CONTRACT.md §2-§6. If the contract moves,
these fail before anyone ships a tool that talks to the wrong endpoint.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
    def __init__(self, responses=None, default=None):
        # responses: list of (matcher(method, url, body) -> bool, ApiResult)
        self.responses = responses or []
        self.default = default if default is not None else admin.ApiResult(True, 200, {"ok": True})
        self.calls = []

    def request(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for matcher, result in self.responses:
            if matcher(method, url, body):
                return result
        return self.default

    @property
    def last(self):
        return self.calls[-1]

    def path(self, index=0):
        return self.calls[index]["url"].split("?", 1)[0].replace(admin.DEFAULT_BASE_URL, "")

    def query(self, index=0):
        url = self.calls[index]["url"]
        return url.split("?", 1)[1] if "?" in url else ""


def collect_output():
    lines = []
    return lines, lambda s: lines.append(str(s))


def parse(argv):
    return admin.build_parser().parse_args(argv)


def make_client(transport, **config_kwargs):
    config_kwargs.setdefault("admin_secret", "owner-secret")
    config_kwargs.setdefault("role", "owner")
    cfg = admin.AdminConfig(**config_kwargs)
    return admin.OrionAdminClient(cfg, transport)


def run_cli(argv, transport=None, confirm=True, **config_kwargs):
    """Run a parsed command against a fake transport. Returns (rc, lines, transport)."""
    transport = transport or FakeTransport()
    client = make_client(transport, **config_kwargs)
    lines, out = collect_output()
    rc = admin.run(parse(argv), client, out, lambda _p: confirm)
    return rc, lines, transport


STAFF_CONFIG = {"admin_secret": "", "staff_token": "tok", "role": "support", "discord_user_id": "42",
                "staff_id": "staff_abc"}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_mask_key_shows_only_last_four():
    assert admin.mask_key("ORION-ABCD-EFGH-1234") == "ORION-…-1234"
    assert admin.mask_key("") == "(none)"
    assert admin.mask_key("abcd") == "****"


def test_redact_config_hides_secrets():
    cfg = admin.AdminConfig(admin_secret="supersecret", admin_totp="123456",
                            staff_token="zzz999tokenval", discord_user_id="123", role="owner")
    red = admin.redact_config(cfg)
    assert red["admin_secret"] == "set"
    assert red["admin_totp"] == "set"
    assert red["staff_token"] == "set"
    blob = json.dumps(red)
    assert "supersecret" not in blob
    assert "zzz999tokenval" not in blob
    assert "123456" not in blob


def test_is_destructive():
    assert admin.is_destructive("revoke")
    assert admin.is_destructive("reset-machine")
    assert admin.is_destructive("blacklist")
    assert admin.is_destructive("rotate-secret")
    assert not admin.is_destructive("lookup")
    assert not admin.is_destructive("create")


def test_role_can_is_display_only_matrix():
    assert admin.role_can("owner", "staff.create")           # owner: everything
    assert admin.role_can("admin", "license.revoke")
    assert not admin.role_can("support", "license.revoke")
    assert admin.role_can("support", "license.reset_machine")
    assert admin.role_can(None, "license.revoke")            # unknown -> defer to server


def test_validate_reason_bounds():
    assert admin.validate_reason("  chargeback  ") == "chargeback"
    with pytest.raises(admin.CommandError):
        admin.validate_reason("")
    with pytest.raises(admin.CommandError):
        admin.validate_reason("x" * (admin.REASON_MAX_CHARS + 1))


def test_build_auth_headers_owner_sends_secret_and_totp():
    owner = admin.build_auth_headers(admin.AdminConfig(admin_secret="s"))
    assert owner["X-Orion-Admin-Secret"] == "s"
    assert "X-Orion-Admin-TOTP" not in owner
    assert "Authorization" not in owner

    with_totp = admin.build_auth_headers(admin.AdminConfig(admin_secret="s", admin_totp="654321"))
    assert with_totp["X-Orion-Admin-TOTP"] == "654321"


def test_build_auth_headers_staff_sends_machine_id():
    # require_staff refuses a machine-bound token without X-Machine-Id.
    staff = admin.build_auth_headers(admin.AdminConfig(staff_token="t", discord_user_id="42"))
    assert staff["Authorization"] == "Bearer t"
    assert staff["X-Orion-Discord-Id"] == "42"
    assert staff["X-Machine-Id"] == admin.local_machine_id()


def test_format_audit_row_v2_shape_masks_full_key():
    row = admin.format_audit_row({
        "ts": 1_700_000_000,
        "actor_type": "staff",
        "actor_id": "staff_abc",
        "action": "license.revoke",
        "target_type": "license",
        "license_key": "ORION-AAAA-BBBB-7777",
        "result": "ok",
        "reason": "chargeback",
    })
    assert "ORION-AAAA-BBBB-7777" not in row
    assert "7777" in row
    assert "staff:staff_abc" in row
    assert "license.revoke" in row
    assert "chargeback" in row


def test_format_license_masks_unless_revealed():
    lic = {"license_key": "ORION-AAAA-BBBB-1234", "status": "active", "plan": "pro",
           "hwid_free_resets": 3, "hwid_resets_used": 1, "hwid_paid_credits": 0}
    masked = "\n".join(admin.format_license(lic, reveal=False))
    assert "ORION-AAAA-BBBB-1234" not in masked
    assert "ORION-…-1234" in masked
    assert "free 1/3" in masked
    revealed = "\n".join(admin.format_license(lic, reveal=True))
    assert "ORION-AAAA-BBBB-1234" in revealed


def test_parse_list_and_caps():
    assert admin.parse_list(" a, b ,,c ") == ["a", "b", "c"]
    assert admin.parse_list("") == []
    args = parse(["staff", "create", "--discord", "1", "--role", "admin",
                  "--keys-per-day", "5", "--extend-max-days", "30", "--reason", "r"])
    assert admin.parse_caps(args) == {"keys_per_day": 5, "extend_max_days": 30}


def test_freshness_fields_use_contract_names():
    fields = admin.request_freshness_fields()
    assert set(fields) == {"nonce", "timestamp"}


def test_load_config_env_overrides(tmp_path):
    cfg_path = tmp_path / "admin_config.json"
    cfg_path.write_text(json.dumps({"base_url": "https://file.example", "admin_secret": "filesecret"}),
                        encoding="utf-8")
    env = {"ORION_ADMIN_SECRET": "envsecret", "ORION_API_BASE": "https://env.example",
           "ORION_ADMIN_TOTP": "999111"}
    cfg = admin.load_config(cfg_path, env=env)
    assert cfg.admin_secret == "envsecret"     # env wins
    assert cfg.base_url == "https://env.example"
    assert cfg.admin_totp == "999111"


def test_save_config_roundtrip_never_writes_totp(tmp_path):
    cfg_path = tmp_path / "admin_config.json"
    admin.save_config(admin.AdminConfig(admin_secret="x", admin_totp="123456", role="owner",
                                        staff_id="staff_1"), cfg_path)
    assert "123456" not in cfg_path.read_text(encoding="utf-8")
    loaded = admin.load_config(cfg_path, env={})
    assert loaded.admin_secret == "x"
    assert loaded.role == "owner"
    assert loaded.staff_id == "staff_1"


def test_owner_route_selection():
    assert admin.AdminConfig(admin_secret="s").uses_owner_routes
    assert admin.AdminConfig(staff_token="t", role="owner").uses_owner_routes
    assert not admin.AdminConfig(staff_token="t", role="admin").uses_owner_routes


# --------------------------------------------------------------------------- #
# Transport guard
# --------------------------------------------------------------------------- #

def test_urllib_transport_refuses_non_https():
    result = admin.UrllibTransport().request("GET", "http://insecure.example/api/version", {}, None)
    assert not result.ok
    assert "non-HTTPS" in result.error


# --------------------------------------------------------------------------- #
# Licenses (contract §2 - POST {action, reason, ...})
# --------------------------------------------------------------------------- #

def test_lookup_posts_action_lookup_to_owner_route():
    rc, lines, t = run_cli(["license", "lookup", "--key", "orion-aaaa-bbbb-1234"])
    assert rc == 0
    assert t.last["method"] == "POST"
    assert t.path() == "/api/admin/license"
    assert t.last["body"] == {"action": "lookup", "key": "ORION-AAAA-BBBB-1234"}


def test_lookup_accepts_every_selector():
    rc, _lines, t = run_cli(["license", "lookup", "--email", "u@e.com", "--discord", "9",
                             "--machine", "mach-1"])
    assert rc == 0
    assert t.last["body"] == {"action": "lookup", "email": "u@e.com",
                              "discord_user_id": "9", "machine_id": "mach-1"}


def test_lookup_without_selector_never_calls_the_api():
    rc, lines, t = run_cli(["license", "lookup"])
    assert rc == 1
    assert t.calls == []


def test_staff_uses_the_staff_license_route_and_machine_header():
    rc, _lines, t = run_cli(["license", "lookup", "--key", "K-1234"], **STAFF_CONFIG)
    assert rc == 0
    assert t.path() == "/api/staff/license"
    assert t.last["headers"]["X-Machine-Id"] == admin.local_machine_id()
    assert t.last["headers"]["Authorization"] == "Bearer tok"


def test_lookup_masks_by_default_and_reveals_on_flag():
    lic = {"ok": True, "license": {"license_key": "ORION-AAAA-BBBB-1234", "status": "active", "plan": "std"}}
    responses = [(lambda m, u, b: True, admin.ApiResult(True, 200, lic))]

    rc, lines, _t = run_cli(["license", "lookup", "--key", "ORION-AAAA-BBBB-1234"],
                            transport=FakeTransport(responses))
    assert rc == 0
    assert all("ORION-AAAA-BBBB-1234" not in ln for ln in lines)
    assert any("ORION-…-1234" in ln for ln in lines)

    rc, lines, _t = run_cli(["license", "lookup", "--key", "ORION-AAAA-BBBB-1234", "--reveal"],
                            transport=FakeTransport(responses))
    assert any("ORION-AAAA-BBBB-1234" in ln for ln in lines)


def test_create_sends_plan_days_count_and_shows_keys_once():
    created = {"ok": True, "keys": ["ORION-FULL-KEY1-9999", "ORION-FULL-KEY2-8888"]}
    transport = FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 201, created))])
    rc, lines, t = run_cli(["license", "create", "--plan", "pro", "--days", "90", "--count", "2",
                            "--discord", "77", "--email", "a@b.com", "--note", "bulk",
                            "--reason", "launch batch"], transport=transport)
    assert rc == 0
    assert t.path() == "/api/admin/license"
    assert t.last["body"] == {"action": "create", "reason": "launch batch", "plan": "pro",
                              "days": 90, "count": 2, "discord_user_id": "77",
                              "email": "a@b.com", "note": "bulk"}
    blob = "\n".join(lines)
    assert "ORION-FULL-KEY1-9999" in blob and "ORION-FULL-KEY2-8888" in blob


def test_create_refuses_more_than_25_per_call():
    rc, _lines, t = run_cli(["license", "create", "--count", "26", "--reason", "r"])
    assert rc == 1
    assert t.calls == []


@pytest.mark.parametrize("action", ["revoke", "unrevoke", "freeze", "unfreeze"])
def test_simple_actions_send_action_key_reason(action):
    rc, _lines, t = run_cli(["--yes", "license", action, "--key", "ORION-AAAA-BBBB-1234",
                             "--reason", "fraud"])
    assert rc == 0
    assert t.last["body"] == {"action": action, "key": "ORION-AAAA-BBBB-1234", "reason": "fraud"}


def test_revoke_requires_reason_and_never_calls_the_api():
    rc, lines, t = run_cli(["license", "revoke", "--key", "ORION-AAAA-BBBB-1234"])
    assert rc == 1
    assert any("requires --reason" in ln for ln in lines)
    assert t.calls == []


def test_revoke_aborts_when_not_confirmed():
    rc, lines, t = run_cli(["license", "revoke", "--key", "ORION-AAAA-BBBB-1234", "--reason", "fraud"],
                           confirm=False)
    assert rc == 1
    assert any("Aborted" in ln for ln in lines)
    assert t.calls == []


def test_revoke_output_masks_the_key():
    rc, lines, _t = run_cli(["--yes", "license", "revoke", "--key", "ORION-AAAA-BBBB-1234",
                             "--reason", "fraud"])
    assert rc == 0
    assert all("ORION-AAAA-BBBB-1234" not in ln for ln in lines)


def test_reset_machine_force_is_opt_in():
    rc, _lines, t = run_cli(["--yes", "license", "reset-machine", "--key", "K-1234", "--reason", "support"])
    assert rc == 0
    assert t.last["body"] == {"action": "reset_machine", "key": "K-1234", "reason": "support"}

    rc, _lines, t = run_cli(["--yes", "license", "reset-machine", "--key", "K-1234",
                             "--force", "--reason", "owner override"])
    assert t.last["body"]["force"] is True


def test_extend_sends_days_not_a_computed_expiry():
    rc, _lines, t = run_cli(["license", "extend", "--key", "K-1234", "--days", "30", "--reason", "goodwill"])
    assert rc == 0
    assert t.last["body"] == {"action": "extend", "key": "K-1234", "days": 30, "reason": "goodwill"}


def test_extend_requires_positive_days():
    rc, _lines, t = run_cli(["license", "extend", "--key", "K-1234", "--reason", "r"])
    assert rc == 1
    assert t.calls == []


def test_set_plan_sends_plan():
    rc, _lines, t = run_cli(["license", "set-plan", "--key", "K-1234", "--plan", "lifetime",
                             "--reason", "upgrade"])
    assert rc == 0
    assert t.last["body"] == {"action": "set_plan", "key": "K-1234", "plan": "lifetime", "reason": "upgrade"}


def test_transfer_needs_a_destination():
    rc, _lines, t = run_cli(["--yes", "license", "transfer", "--key", "K-1234", "--reason", "gift"])
    assert rc == 1
    assert t.calls == []

    rc, _lines, t = run_cli(["--yes", "license", "transfer", "--key", "K-1234", "--discord", "55",
                             "--reason", "gift"])
    assert rc == 0
    assert t.last["body"] == {"action": "transfer", "key": "K-1234", "reason": "gift",
                              "discord_user_id": "55"}


def test_set_reset_policy_is_owner_route_only():
    rc, _lines, t = run_cli(["license", "set-reset-policy", "--key", "K-1234", "--free-resets", "5",
                             "--locked", "true", "--reason", "vip"], **STAFF_CONFIG)
    assert rc == 0
    # Owner-only action: always the /api/admin/ route, even from a staff config.
    assert t.path() == "/api/admin/license"
    assert t.last["body"] == {"action": "set_reset_policy", "key": "K-1234", "reason": "vip",
                              "free_resets": 5, "locked": True}


def test_set_reset_policy_requires_a_field():
    rc, _lines, t = run_cli(["license", "set-reset-policy", "--key", "K-1234", "--reason", "vip"])
    assert rc == 1
    assert t.calls == []


def test_blacklist_and_unblacklist():
    rc, _lines, t = run_cli(["--yes", "license", "blacklist", "--machine", "mach-9", "--reason", "abuse"])
    assert rc == 0
    assert t.path() == "/api/admin/license"
    assert t.last["body"] == {"action": "blacklist", "reason": "abuse", "machine_id": "mach-9"}

    rc, _lines, t = run_cli(["license", "unblacklist", "--discord", "77", "--reason", "appeal granted"])
    assert rc == 0
    assert t.last["body"] == {"action": "unblacklist", "reason": "appeal granted", "discord_user_id": "77"}


# --------------------------------------------------------------------------- #
# Staff (contract §2 staff management)
# --------------------------------------------------------------------------- #

def test_staff_list_is_a_get():
    rows = {"ok": True, "staff": [{"staff_id": "staff_1", "discord_user_id": "5", "role": "admin",
                                   "disabled": False, "caps": {"keys_per_day": 10},
                                   "usage": {"keys": 2}}]}
    rc, lines, t = run_cli(["staff", "list"], transport=FakeTransport([(lambda m, u, b: True,
                                                                       admin.ApiResult(True, 200, rows))]))
    assert rc == 0
    assert t.last["method"] == "GET"
    assert t.path() == "/api/admin/staff"
    assert any("staff_1" in ln and "keys 2/10" in ln for ln in lines)


def test_staff_create_shows_the_one_time_enroll_key():
    created = {"ok": True, "staff_id": "staff_new", "enroll_key": "ENROLL-ONE-TIME-KEY"}
    rc, lines, t = run_cli(["staff", "create", "--discord", "5", "--role", "admin",
                            "--keys-per-day", "10", "--resets-per-day", "5", "--extend-max-days", "30",
                            "--reason", "new hire"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, created))]))
    assert rc == 0
    assert t.path() == "/api/admin/staff"
    assert t.last["body"] == {"action": "create", "discord_user_id": "5", "role": "admin",
                              "reason": "new hire",
                              "caps": {"keys_per_day": 10, "resets_per_day": 5, "extend_max_days": 30}}
    assert any("ENROLL-ONE-TIME-KEY" in ln for ln in lines)


@pytest.mark.parametrize("cli_name,action", [
    ("disable", "disable"),
    ("enable", "enable"),
    ("reset-machine", "reset_machine"),
    ("reissue-enrollment", "reissue_enrollment"),
])
def test_staff_actions_send_staff_id_and_action(cli_name, action):
    rc, _lines, t = run_cli(["--yes", "staff", cli_name, "--staff-id", "staff_1", "--reason", "offboarding"])
    assert rc == 0
    assert t.path() == "/api/admin/staff"
    assert t.last["body"] == {"action": action, "staff_id": "staff_1", "reason": "offboarding"}


def test_staff_reissue_enrollment_prints_the_new_key():
    reissued = {"ok": True, "enroll_key": "NEW-ENROLL-KEY"}
    rc, lines, _t = run_cli(["--yes", "staff", "reissue-enrollment", "--staff-id", "staff_1",
                             "--reason", "lost key"],
                            transport=FakeTransport([(lambda m, u, b: True,
                                                      admin.ApiResult(True, 200, reissued))]))
    assert rc == 0
    assert any("NEW-ENROLL-KEY" in ln for ln in lines)


def test_staff_set_role_and_set_caps():
    rc, _lines, t = run_cli(["staff", "set-role", "--staff-id", "staff_1", "--role", "support",
                             "--reason", "scope down"])
    assert rc == 0
    assert t.last["body"] == {"action": "set_role", "staff_id": "staff_1", "role": "support",
                              "reason": "scope down"}

    rc, _lines, t = run_cli(["staff", "set-caps", "--staff-id", "staff_1", "--resets-per-day", "3",
                             "--reason", "tighten"])
    assert rc == 0
    assert t.last["body"] == {"action": "set_caps", "staff_id": "staff_1", "reason": "tighten",
                              "caps": {"resets_per_day": 3}}


def test_staff_set_caps_requires_at_least_one_cap():
    rc, _lines, t = run_cli(["staff", "set-caps", "--staff-id", "staff_1", "--reason", "tighten"])
    assert rc == 1
    assert t.calls == []


def test_staff_disable_requires_reason():
    rc, _lines, t = run_cli(["staff", "disable", "--staff-id", "staff_1"])
    assert rc == 1
    assert t.calls == []


# --------------------------------------------------------------------------- #
# Enrolment / login (contract §2 body shape)
# --------------------------------------------------------------------------- #

def test_staff_enroll_sends_contract_body_and_saves_token(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "admin_config.json")
    enrolled = {"ok": True, "token": "tok-1", "staff": {"staff_id": "staff_1", "role": "admin",
                                                        "discord_user_id": "5"}}
    transport = FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, enrolled))])
    rc, lines, t = run_cli(["staff-enroll", "--staff-id", "staff_1", "--enroll-key", "ENROLL-1"],
                           transport=transport)
    assert rc == 0
    assert t.path() == "/api/staff/enroll"
    body = t.last["body"]
    assert body["staff_id"] == "staff_1"
    assert body["enroll_key"] == "ENROLL-1"
    assert body["machine_id"] == admin.local_machine_id()
    assert set(body) == {"staff_id", "enroll_key", "machine_id", "nonce", "timestamp"}
    assert all("tok-1" not in ln for ln in lines)               # token never printed
    saved = admin.load_config(admin.CONFIG_PATH, env={})
    assert saved.staff_token == "tok-1"
    assert saved.role == "admin"


def test_staff_login_sends_staff_id_and_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "admin_config.json")
    logged_in = {"ok": True, "token": "tok-2", "staff": {"staff_id": "staff_1", "role": "support"}}
    transport = FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, logged_in))])
    rc, _lines, t = run_cli(["staff-login", "--staff-id", "staff_1"], transport=transport)
    assert rc == 0
    assert t.path() == "/api/staff/login"
    assert set(t.last["body"]) == {"staff_id", "machine_id", "nonce", "timestamp"}


# --------------------------------------------------------------------------- #
# Audit (contract §4)
# --------------------------------------------------------------------------- #

def test_audit_reads_the_audit_key_with_filters():
    rows = {"ok": True, "audit": [{"ts": 1_700_000_000, "actor_type": "owner", "actor_id": "owner",
                                   "action": "config.update", "result": "ok"}],
            "next_cursor": "CUR2"}
    rc, lines, t = run_cli(["audit", "--since", "1700000000", "--actor", "staff_1",
                            "--action", "license.revoke", "--target", "1234", "--limit", "10"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, rows))]))
    assert rc == 0
    assert t.last["method"] == "GET"
    assert t.path() == "/api/admin/audit"
    query = t.query()
    for expected in ("limit=10", "since=1700000000", "actor=staff_1",
                     "action=license.revoke", "target=1234"):
        assert expected.split("=")[0] + "=" in query
    assert "config.update" in "\n".join(lines)
    assert any("CUR2" in ln for ln in lines)


def test_audit_limit_is_capped_at_200():
    rc, _lines, t = run_cli(["audit", "--limit", "5000"])
    assert rc == 0
    assert "limit=200" in t.query()


def test_staff_audit_uses_the_staff_route():
    rc, _lines, t = run_cli(["audit", "--cursor", "CUR1"], **STAFF_CONFIG)
    assert rc == 0
    assert t.path() == "/api/staff/audit"
    assert "cursor=CUR1" in t.query()


# --------------------------------------------------------------------------- #
# Config / killswitch / metrics (contract §5, §6)
# --------------------------------------------------------------------------- #

def test_server_config_show_is_a_get():
    cfg = {"ok": True, "config": {"global_kill": False, "min_client_version": "1.4.0"}}
    rc, lines, t = run_cli(["server-config", "show"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, cfg))]))
    assert rc == 0
    assert t.last["method"] == "GET"
    assert t.path() == "/api/admin/config"
    assert any("min_client_version" in ln for ln in lines)


def test_server_config_set_builds_the_patch():
    rc, _lines, t = run_cli(["server-config", "set",
                             "--min-client-version", "1.5.0",
                             "--blocked-versions", "1.3.0, 1.3.1",
                             "--motd-text", "Maintenance at 02:00 UTC",
                             "--motd-level", "maint",
                             "--motd-until", "1800000000",
                             "--free-resets", "3", "--penalty-days", "3", "--cooldown-s", "86400",
                             "--self-service", "true",
                             "--machines-30d", "3", "--resets-30d", "4",
                             "--alert-owner", "999", "--alert-events", "staff.create, license.revoke",
                             "--owner-totp-required", "true",
                             "--ip-allowlist", "203.0.113.0/24",
                             "--reason", "hardening pass"])
    assert rc == 0
    assert t.last["method"] == "POST"
    assert t.path() == "/api/admin/config"
    body = t.last["body"]
    assert body["min_client_version"] == "1.5.0"
    assert body["blocked_versions"] == ["1.3.0", "1.3.1"]
    assert body["motd"] == {"text": "Maintenance at 02:00 UTC", "level": "maint", "until": 1800000000}
    assert body["reset_policy_defaults"] == {"free_resets": 3, "penalty_days": 3,
                                             "cooldown_s": 86400, "self_service": True}
    assert body["fraud_thresholds"] == {"machines_30d": 3, "resets_30d": 4}
    assert body["alerts"] == {"owner_discord_user_id": "999",
                              "events": ["staff.create", "license.revoke"]}
    assert body["owner_totp_required"] is True
    assert body["owner_ip_allowlist"] == ["203.0.113.0/24"]
    assert body["reason"] == "hardening pass"


def test_server_config_set_refuses_an_empty_patch():
    rc, _lines, t = run_cli(["server-config", "set", "--reason", "nothing"])
    assert rc == 1
    assert t.calls == []


def test_totp_enroll_and_confirm():
    enrolled = {"ok": True, "otpauth_uri": "otpauth://totp/Orion:owner?secret=ABC", "secret": "ABC"}
    rc, lines, t = run_cli(["server-config", "totp-enroll", "--reason", "enrol owner mfa"],
                           transport=FakeTransport([(lambda m, u, b: True,
                                                     admin.ApiResult(True, 200, enrolled))]))
    assert rc == 0
    assert t.last["body"] == {"action": "totp_enroll", "reason": "enrol owner mfa"}
    assert any("otpauth://" in ln for ln in lines)

    rc, _lines, t = run_cli(["server-config", "totp-confirm", "--code", "123456", "--reason", "enrol owner mfa"])
    assert rc == 0
    assert t.last["body"] == {"action": "totp_confirm", "code": "123456", "reason": "enrol owner mfa"}


def test_rotate_secret_shows_the_new_secret_once():
    rotated = {"ok": True, "admin_secret": "brand-new-secret"}
    rc, lines, t = run_cli(["--yes", "server-config", "rotate-secret", "--reason", "quarterly rotation"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, rotated))]))
    assert rc == 0
    assert t.last["body"] == {"action": "rotate_admin_secret", "reason": "quarterly rotation"}
    assert any("brand-new-secret" in ln for ln in lines)


def test_rotate_secret_aborts_without_confirmation():
    rc, _lines, t = run_cli(["server-config", "rotate-secret", "--reason", "quarterly rotation"],
                            confirm=False)
    assert rc == 1
    assert t.calls == []


def test_killswitch_status_reads_config():
    cfg = {"ok": True, "config": {"global_kill": True, "kill_reason": "maintenance"}}
    rc, lines, t = run_cli(["killswitch", "status"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, cfg))]))
    assert rc == 0
    assert t.last["method"] == "GET"
    assert t.path() == "/api/admin/config"
    assert any("ENGAGED" in ln and "maintenance" in ln for ln in lines)


def test_killswitch_engage_writes_config_global_kill():
    rc, lines, t = run_cli(["--yes", "killswitch", "engage", "--reason", "incident"])
    assert rc == 0
    assert t.path() == "/api/admin/config"
    assert t.last["body"] == {"global_kill": True, "reason": "incident"}
    assert any("engaged" in ln for ln in lines)


def test_killswitch_release_writes_false():
    rc, _lines, t = run_cli(["killswitch", "release", "--reason", "incident over"])
    assert rc == 0
    assert t.last["body"] == {"global_kill": False, "reason": "incident over"}


def test_killswitch_engage_requires_reason_and_confirmation():
    rc, lines, t = run_cli(["killswitch", "engage"])
    assert rc == 1
    assert any("requires --reason" in ln for ln in lines)
    assert t.calls == []

    rc, lines, t = run_cli(["killswitch", "engage", "--reason", "test"], confirm=False)
    assert rc == 1
    assert any("Aborted" in ln for ln in lines)
    assert t.calls == []


def test_metrics_is_owner_get():
    metrics = {"ok": True, "metrics": {"licenses": {"active": 12}, "online_now": 3}}
    rc, lines, t = run_cli(["metrics"],
                           transport=FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 200, metrics))]))
    assert rc == 0
    assert t.last["method"] == "GET"
    assert t.path() == "/api/admin/metrics"
    assert any("online_now: 3" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# Auth gating / error surfacing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("argv", [
    ["license", "lookup", "--key", "K-1"],
    ["staff", "list"],
    ["audit"],
    ["server-config", "show"],
    ["killswitch", "status"],
    ["metrics"],
])
def test_commands_require_authentication(argv):
    transport = FakeTransport()
    client = admin.OrionAdminClient(admin.AdminConfig(), transport)  # no secret/token
    lines, out = collect_output()
    rc = admin.run(parse(argv), client, out, lambda _p: True)
    assert rc == 1
    assert any("Not authenticated" in ln for ln in lines)
    assert transport.calls == []


def test_server_error_code_is_surfaced():
    refused = admin.ApiResult(False, 403, {"ok": False, "error": "ip_not_allowed",
                                           "message": "owner allowlist"}, "")
    rc, lines, _t = run_cli(["license", "lookup", "--key", "K-1"],
                            transport=FakeTransport([(lambda m, u, b: True, refused)]))
    assert rc == 1
    assert any("ip_not_allowed" in ln for ln in lines)


def test_ok_false_body_is_treated_as_failure():
    denied = admin.ApiResult(True, 200, {"ok": False, "error": "forbidden"})
    rc, lines, _t = run_cli(["--yes", "license", "revoke", "--key", "K-1", "--reason", "r"],
                            transport=FakeTransport([(lambda m, u, b: True, denied)]))
    assert rc == 1
    assert any("forbidden" in ln for ln in lines)


def test_caps_command_is_display_only_and_needs_no_network():
    rc, lines, t = run_cli(["caps"])
    assert rc == 0
    assert t.calls == []
    blob = "\n".join(lines)
    assert "owner" in blob and "support" in blob and "license.lookup" in blob


# ── owner rule 2026-09-15: month is the only plan sold ────────────────────────

def test_create_defaults_to_the_month_plan():
    """The old default was the meaningless string "standard", which minted rows
    whose plan matched nothing in TIER_DAYS."""
    created = {"ok": True, "keys": ["ORION-FULL-KEY1-9999"]}
    transport = FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 201, created))])
    rc, _lines, t = run_cli(["license", "create", "--reason", "one comp"],
                            transport=transport)
    assert rc == 0
    assert admin.SELLABLE_PLAN == "month"
    assert t.last["body"]["plan"] == "month"
    assert t.last["body"]["days"] == 30


def test_legacy_plans_can_still_be_minted_explicitly():
    """Staff comps and historical tiers must stay reachable — the rule is that
    they are not ADVERTISED, not that the CLI refuses them."""
    created = {"ok": True, "keys": ["ORION-FULL-KEY1-9999"]}
    transport = FakeTransport([(lambda m, u, b: True, admin.ApiResult(True, 201, created))])
    rc, _lines, t = run_cli(["license", "create", "--plan", "lifetime", "--days", "0",
                             "--reason", "comp"], transport=transport)
    assert rc == 0 and t.last["body"]["plan"] == "lifetime"


def test_reset_policy_defaults_mirror_the_backend():
    assert admin.RESET_FREE_DEFAULT == 3
    assert admin.RESET_PENALTY_DAYS == 1


def test_deduct_days_is_accepted_as_an_alias_for_penalty_days():
    """`deduct_days` is the contract's spelling; `penalty_days` is what the wire
    has always used. Both reach the same field."""
    rc, _lines, t = run_cli(["server-config", "set", "--deduct-days", "1",
                             "--reason", "owner rule"])
    assert rc == 0
    assert t.last["body"]["reset_policy_defaults"] == {"penalty_days": 1}

    rc, _lines, t = run_cli(["license", "set-reset-policy", "--key", "K-1234",
                             "--deduct-days", "2", "--reason", "vip"])
    assert rc == 0
    assert t.last["body"]["penalty_days"] == 2
