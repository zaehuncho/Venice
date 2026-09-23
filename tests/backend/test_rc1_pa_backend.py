"""P-A (rc1 final red team, 2026-09-23): backend release blockers.

RT-CRIT-01  owner step-up: a stolen owner-role bearer cannot disable TOTP, rotate
            the break-glass secret or otherwise weaken owner controls without a
            FRESH, single-use TOTP code; owner-role login needs the code too.
RT-CRIT-02  unknown global-kill state never mints a new/renewed lease (no warm
            "kill off" reuse) and is reported as a retriable, NON-kill code.
RT-HIGH-07  every destructive staff/admin route writes a REQUIRED attempt audit
            before mutating; an audit-table outage -> 503 with state unchanged.
RT-LOW-04   Stripe event.id dedup: a replayed event is a 200 no-op (no second
            revoke audit, no second owner alert); a failed attempt can retry.
RT-MED-12   heartbeat lease v2: the signed lease binds the client's request
            nonce + server issue time (backward compatible with v1 clients).
"""
import base64
import time

import pytest

from conftest import (invoke, put_license, put_staff, make_nonce_ts, make_staff_nonce_ts,
                      audit_rows, sign_update_manifest_canonical, verify_lease_sig,
                      LEASE_PUB_RAW, OWNER_H, BOT_H, TEST_ADMIN_SECRET, TEST_WORKER_SECRET)

WORKER_H = {"x-orion-bot-secret": TEST_WORKER_SECRET}
KILL_CODES = {"service_disabled", "revoked", "expired", "device_mismatch", "invalid_key",
              "inactive", "frozen", "blacklisted", "version_blocked", "subscription_required"}


# ── helpers ───────────────────────────────────────────────────────────────────
def _enroll_totp(lf, required=True):
    secret = lf.gen_totp_secret()
    lf.get_ssm().put_parameter(Name=lf.OWNER_TOTP_SSM, Value=secret,
                               Type="SecureString", Overwrite=True)
    lf.config_set("owner_totp_required", required)
    return secret


def _code(lf, secret, offset=0):
    return lf.totp_at(secret, int(time.time() // lf.TOTP_STEP_S) + offset)


def _owner_bearer(lf, secret=None, staff_id="owner-a", machine="MACHINE-A"):
    put_staff(lf, staff_id, role="owner", machine_id=machine)
    body = {"staff_id": staff_id, "machine_id": machine, **make_staff_nonce_ts()}
    if secret:
        body["totp_code"] = _code(lf, secret, -1)
    s, b, _ = invoke(lf, "POST", "/api/staff/login", body=body)
    assert s == 200, b
    return {"authorization": "Bearer " + b["token"], "x-machine-id": machine}


def _admin_secret(lf):
    return lf.ssm_get(lf.ADMIN_SECRET_SSM, decrypt=True)


def _totp_required(lf):
    lf._config_cache.clear()
    return lf.config_get("owner_totp_required")


@pytest.fixture
def quiet_alerts(lf, monkeypatch):
    sent = []
    monkeypatch.setattr(lf, "owner_alert", lambda *a, **k: sent.append(a[0] if a else ""))
    return sent


class _AuditSwitch:
    """Wraps the audit table so PUTs can be failed on demand (reads keep working)."""
    def __init__(self, real):
        self.real = real
        self.fail = False

    def put_item(self, **kw):
        if self.fail:
            raise OSError("fixture audit write failure")
        return self.real.put_item(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
def audit_switch(lf, monkeypatch):
    sw = _AuditSwitch(lf.audit_table())
    monkeypatch.setattr(lf, "audit_table", lambda: sw)
    return sw


# ══════════════════════════════════════════════════════════════════════════════
# RT-CRIT-01 owner step-up
# ══════════════════════════════════════════════════════════════════════════════
class TestOwnerStepUp:
    def test_stolen_bearer_without_code_cannot_disable_totp(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "totp_disable", "reason": "stolen"})
        assert s == 403 and b["error"] == "totp_required"
        assert _totp_required(lf) is True
        assert audit_rows(lf, "owner.step_up_denied")
        assert not audit_rows(lf, "config.totp_disable")

    def test_stolen_bearer_without_code_cannot_rotate_admin_secret(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "rotate_admin_secret", "reason": "stolen"})
        assert s == 403 and b["error"] == "totp_required"
        assert "admin_secret" not in b
        assert _admin_secret(lf) == TEST_ADMIN_SECRET

    @pytest.mark.parametrize("bad", ["000000", "expired"])
    def test_wrong_or_expired_code_is_refused(self, lf, quiet_alerts, bad):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        code = _code(lf, secret, -3) if bad == "expired" else bad
        if code == _code(lf, secret, 0):      # vanishingly unlikely collision
            code = "999999" if code != "999999" else "000001"
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": code},
                         body={"action": "rotate_admin_secret", "reason": "x"})
        assert s == 403 and b["error"] == "invalid_totp"
        assert _admin_secret(lf) == TEST_ADMIN_SECRET

    def test_replayed_code_is_refused(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        code = _code(lf, secret, 0)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": code},
                         body={"action": "rotate_admin_secret", "reason": "quarterly"})
        assert s == 200 and b["admin_secret"]
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": code},
                         body={"action": "totp_disable", "reason": "replay"})
        assert s == 403 and b["error"] == "totp_replayed"
        assert _totp_required(lf) is True

    def test_wrong_machine_is_refused(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-machine-id": "MACHINE-B",
                                  "x-orion-admin-totp": _code(lf, secret, 0)},
                         body={"action": "totp_disable", "reason": "x"})
        assert s == 403 and b["error"] == "machine_mismatch"
        assert _totp_required(lf) is True

    def test_disabled_owner_is_refused(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        lf.staff_table().update_item(Key={"staff_id": "owner-a"},
                                     UpdateExpression="SET disabled = :d",
                                     ExpressionAttributeValues={":d": True})
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": _code(lf, secret, 0)},
                         body={"action": "totp_disable", "reason": "x"})
        assert s == 403 and b["error"] == "staff_disabled"
        assert _totp_required(lf) is True

    def test_audit_outage_refuses_with_state_unchanged(self, lf, quiet_alerts, audit_switch):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        audit_switch.fail = True
        for action in ("totp_disable", "rotate_admin_secret"):
            s, b, _ = invoke(lf, "POST", "/api/admin/config",
                             headers={**h, "x-orion-admin-totp": _code(lf, secret, 0)},
                             body={"action": action, "reason": "x"})
            assert s == 503 and b["error"] == "audit_unavailable", (action, b)
        assert _totp_required(lf) is True
        assert _admin_secret(lf) == TEST_ADMIN_SECRET

    def test_fresh_code_allows_disable_with_attempt_and_completion_rows(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": _code(lf, secret, 0)},
                         body={"action": "totp_disable", "reason": "new phone"})
        assert s == 200 and b["owner_totp_required"] is False
        attempt = audit_rows(lf, "config.totp_disable.attempt")
        done = audit_rows(lf, "config.totp_disable")
        assert attempt and done
        assert done[0]["event_id"] == attempt[0]["event_id"] + "-ok"

    def test_owner_role_login_requires_totp_when_required(self, lf):
        secret = _enroll_totp(lf)
        put_staff(lf, "owner-b", role="owner", machine_id="MACHINE-A")
        s, b, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "owner-b", "machine_id": "MACHINE-A", **make_staff_nonce_ts()})
        assert s == 403 and b["error"] == "totp_required"
        assert "token" not in b
        s, b, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "owner-b", "machine_id": "MACHINE-A",
            "totp_code": _code(lf, secret, 0), **make_staff_nonce_ts()})
        assert s == 200 and b["token"]
        # a support login is unaffected (the factor guards the owner role only)
        put_staff(lf, "sup-b", role="support", machine_id="MACHINE-S")
        s, b, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "sup-b", "machine_id": "MACHINE-S", **make_staff_nonce_ts()})
        assert s == 200

    def test_config_set_cannot_switch_totp_off_without_step_up(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        for cfg in ({"owner_totp_required": False}, {"owner_ip_allowlist": ["0.0.0.0/0"]},
                    {"alerts": {"owner_discord_user_id": "1"}}):
            s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                             body={"action": "set", "config": cfg, "reason": "x"})
            assert s == 403 and b["error"] == "totp_required", (cfg, b)
        assert _totp_required(lf) is True

    def test_totp_enroll_cannot_overwrite_secret_without_step_up(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "totp_enroll", "reason": "x"})
        assert s == 403 and "secret" not in b
        assert lf.owner_totp_secret() == secret

    def test_staff_owner_without_any_second_factor_cannot_rotate(self, lf, quiet_alerts):
        # TOTP never enrolled: the bearer is the ONLY factor, so the step-up
        # actions are refused on that path; the break-glass secret still works.
        h = _owner_bearer(lf)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "rotate_admin_secret", "reason": "x"})
        assert s == 403 and b["error"] == "step_up_required"
        assert _admin_secret(lf) == TEST_ADMIN_SECRET
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "totp_enroll", "reason": "x"})
        assert s == 403 and b["error"] == "step_up_required"
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H,
                         body={"action": "rotate_admin_secret", "reason": "break-glass"})
        assert s == 200 and b["admin_secret"]

    def test_staff_owner_privilege_grants_need_step_up(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=h, body={
            "action": "create", "staff_id": "evil", "role": "owner", "reason": "x"})
        assert s == 403 and b["error"] == "totp_required"
        assert not lf.staff_table().get_item(Key={"staff_id": "evil"}).get("Item")
        put_staff(lf, "adm-x", role="admin", machine_id="M-X")
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=h, body={
            "action": "set_role", "staff_id": "adm-x", "role": "owner", "reason": "x"})
        assert s == 403
        assert lf.staff_table().get_item(Key={"staff_id": "adm-x"})["Item"]["role"] == "admin"
        # with a fresh code it goes through
        s, b, _ = invoke(lf, "POST", "/api/admin/staff",
                         headers={**h, "x-orion-admin-totp": _code(lf, secret, 0)},
                         body={"action": "set_role", "staff_id": "adm-x", "role": "owner",
                               "reason": "promotion"})
        assert s == 200

    def test_global_kill_release_needs_step_up(self, lf, quiet_alerts):
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        lf.config_table().put_item(Item={"config_key": "global_kill", "enabled": True,
                                         "reason": "incident", "set_at": 1})
        s, b, _ = invoke(lf, "POST", "/api/admin/unkill", headers=h,
                         body={"target_type": "global", "reason": "x"})
        assert s == 403 and b["error"] == "totp_required"
        assert lf.get_global_kill()[0] is True
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h, body={
            "action": "set", "global_kill": {"enabled": False}, "reason": "x"})
        assert s == 403
        assert lf.get_global_kill()[0] is True
        # ENGAGING the kill stays one step (the safe direction)
        lf.config_table().put_item(Item={"config_key": "global_kill", "enabled": False,
                                         "reason": "", "set_at": 1})
        s, b, _ = invoke(lf, "POST", "/api/admin/kill", headers=h,
                         body={"target_type": "global", "reason": "incident"})
        assert s == 200 and lf.get_global_kill()[0] is True

    def test_security_config_read_failure_fails_closed(self, lf, monkeypatch):
        """owner_totp_required / owner_ip_allowlist used to read as their DEFAULT
        (off / allow-all) on a config-table error: the second factor vanished."""
        _enroll_totp(lf)
        real = lf.config_table()

        class Broken:
            def get_item(self, **kw):
                if kw.get("Key", {}).get("config_key") in ("owner_totp_required",
                                                           "owner_ip_allowlist"):
                    raise OSError("fixture config outage")
                return real.get_item(**kw)

            def __getattr__(self, n):
                return getattr(real, n)

        lf._config_cache.clear()
        monkeypatch.setattr(lf, "config_table", lambda: Broken())
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 503 and b["error"] == "config_unavailable"


# ══════════════════════════════════════════════════════════════════════════════
# RT-CRIT-02 unknown kill state
# ══════════════════════════════════════════════════════════════════════════════
class _SelectiveKillOutage:
    def __init__(self, real):
        self.real = real
        self.fail = False
        self.calls = []

    def get_item(self, **kw):
        if kw.get("Key", {}).get("config_key") == "global_kill":
            self.calls.append(kw)
            if self.fail:
                raise OSError("fixture selective read outage")
        return self.real.get_item(**kw)

    def __getattr__(self, n):
        return getattr(self.real, n)


@pytest.fixture
def kill_outage(lf, monkeypatch):
    sw = _SelectiveKillOutage(lf.config_table())
    monkeypatch.setattr(lf, "config_table", lambda: sw)
    return sw


def _check(lf, key):
    return invoke(lf, "POST", "/api/license/check",
                  body={"license_key": key, "machine_id": "MACHINE-A", "client_version": "1.0.0"})


class TestKillStateFailClosed:
    def test_warm_off_owner_kill_selective_outage_then_recovery(self, lf, quiet_alerts, kill_outage):
        key = "ORION-KILLW-BBBB-CCCC"
        put_license(lf, key, machine_id="MACHINE-A")
        s, b, _ = _check(lf, key)                         # warms "kill off"
        assert s == 200 and b.get("lease_sig")
        s, _, _ = invoke(lf, "POST", "/api/admin/kill", headers=OWNER_H,
                         body={"target_type": "global", "reason": "incident"})
        assert s == 200
        kill_outage.fail = True
        s, b, _ = _check(lf, key)
        assert s == 503 and b["error"] == "kill_state_unavailable"
        assert b["error"] not in KILL_CODES               # client keeps its bounded lease, retries
        assert "lease_sig" not in b and "lease_expires_at" not in b
        # recovery: the read works again and reports the real (engaged) kill
        kill_outage.fail = False
        s, b, _ = _check(lf, key)
        assert s == 503 and b["error"] == "service_disabled"
        h = {**OWNER_H}
        s, _, _ = invoke(lf, "POST", "/api/admin/unkill", headers=h,
                         body={"target_type": "global", "reason": "resolved"})
        assert s == 200
        s, b, _ = _check(lf, key)
        assert s == 200 and b.get("lease_sig")

    def test_cold_outage_is_retriable_not_a_kill(self, lf, kill_outage):
        key = "ORION-KILLC-BBBB-CCCC"
        put_license(lf, key, machine_id="MACHINE-A")
        lf._LAST_GLOBAL_KILL = None
        kill_outage.fail = True
        s, b, _ = _check(lf, key)
        assert s == 503 and b["error"] == "kill_state_unavailable"
        assert "lease_sig" not in b

    def test_activation_denied_on_unknown_kill_state(self, lf, kill_outage):
        key = "ORION-KILLA-BBBB-CCCC"
        put_license(lf, key, machine_id="")
        kill_outage.fail = True
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": key, "machine_id": "MACHINE-A", "client_version": "1.0.0",
            **make_nonce_ts()})
        assert s == 503 and b["error"] == "kill_state_unavailable"
        assert "token" not in b and "lease_sig" not in b
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert row.get("machine_id", "") == ""

    def test_kill_read_is_strongly_consistent(self, lf, kill_outage):
        lf.get_global_kill()
        assert kill_outage.calls and kill_outage.calls[-1].get("ConsistentRead") is True


# ══════════════════════════════════════════════════════════════════════════════
# RT-HIGH-07 required pre-mutation audit on every destructive staff/admin route
# ══════════════════════════════════════════════════════════════════════════════
SHARD_ID = "ab" * 32


def _setup_and_request(lf, route):
    """Returns (method, path, headers, body, snapshot_fn)."""
    lic = "ORION-AUDX-BBBB-CCCC"
    put_license(lf, lic, machine_id="MACHINE-A")

    def lic_row():
        r = lf.licenses_table().get_item(Key={"license_key": lic})["Item"]
        return (r.get("status"), r.get("revoked"), r.get("machine_id"), int(r.get("expiry", 0)))

    def gkill():
        item = lf.config_table().get_item(Key={"config_key": "global_kill"}).get("Item") or {}
        return bool(item.get("enabled", False))

    def lic_count():
        return len(lf.licenses_table().scan()["Items"])

    def staff_row(sid):
        def f():
            r = lf.staff_table().get_item(Key={"staff_id": sid}).get("Item") or {}
            return (r.get("role"), r.get("disabled"), r.get("machine_id"),
                    str(r.get("caps")), r.get("enroll_key_hash"))
        return f

    if route == "kill_global":
        return "POST", "/api/admin/kill", OWNER_H, {"target_type": "global", "reason": "x"}, gkill
    if route == "kill_license":
        return "POST", "/api/admin/kill", OWNER_H, {"target_type": "license_key",
                                                    "target_id": lic, "reason": "x"}, lic_row
    if route == "unkill_global":
        lf.config_table().put_item(Item={"config_key": "global_kill", "enabled": True,
                                         "reason": "r", "set_at": 1})
        return "POST", "/api/admin/unkill", OWNER_H, {"target_type": "global", "reason": "x"}, gkill
    if route == "unkill_license":
        lf.licenses_table().update_item(Key={"license_key": lic},
                                        UpdateExpression="SET #s = :s, revoked = :r",
                                        ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":s": "revoked", ":r": True})
        return "POST", "/api/admin/unkill", OWNER_H, {"target_type": "license_key",
                                                      "target_id": lic, "reason": "x"}, lic_row
    if route == "provision":
        return "POST", "/api/admin/provision", OWNER_H, {"plan": "month", "reason": "x"}, lic_count
    if route == "license_create":
        return "POST", "/api/admin/license", OWNER_H, {"action": "create", "reason": "x"}, lic_count
    if route == "license_revoke":
        return "POST", "/api/admin/license", OWNER_H, {"action": "revoke", "key": lic,
                                                       "reason": "x"}, lic_row
    if route == "blacklist":
        return "POST", "/api/admin/license", OWNER_H, {"action": "blacklist",
                                                       "machine_id": "MACHINE-Z",
                                                       "reason": "x"}, lic_count
    if route == "staff_create":
        return "POST", "/api/admin/staff", OWNER_H, {"action": "create", "staff_id": "new1",
                                                     "role": "support", "reason": "x"}, staff_row("new1")
    for act in ("disable", "enable", "set_role", "set_caps", "reset_machine", "reissue_enrollment"):
        if route == "staff_" + act:
            put_staff(lf, "tgt1", role="support", machine_id="M-T",
                      disabled=(act == "enable"))
            body = {"action": act, "staff_id": "tgt1", "reason": "x"}
            if act == "set_role":
                body["role"] = "admin"
            if act == "set_caps":
                body["caps"] = {"keys_per_day": 99}
            return "POST", "/api/admin/staff", OWNER_H, body, staff_row("tgt1")
    if route == "rotate_admin_secret":
        return "POST", "/api/admin/config", OWNER_H, {"action": "rotate_admin_secret",
                                                      "reason": "x"}, lambda: _admin_secret(lf)
    if route == "shard_revoke":
        lf.shards_table().put_item(Item={"build_id": SHARD_ID, "revoked": False})

        def shard():
            return lf.shards_table().get_item(Key={"build_id": SHARD_ID})["Item"].get("revoked")
        return "POST", "/api/admin/shard/revoke", OWNER_H, {"build_id": SHARD_ID}, shard
    if route == "update_publish":
        m = {"latest_version": "1.2.3", "minimum_supported_version": "1.0.0",
             "artifact_url": "https://orion-releases.s3.amazonaws.com/v1.2.3/orion.zip",
             "sha256": "a" * 64, "published_at": "2026-09-23T00:00:00Z",
             "mandatory": False, "allow_rollback": False, "public_key_id": "orion-ed25519-v1"}
        m["signature"] = sign_update_manifest_canonical(m)

        def manifest():
            return lf.update_manifest_table().get_item(Key={"record_id": "current"}).get("Item")
        return "POST", "/api/update", OWNER_H, m, manifest
    if route == "staff_license_reset":
        h = _support_headers(lf)
        return "POST", "/api/staff/license", h, {"action": "reset_machine", "key": lic,
                                                 "reason": "x"}, lic_row
    if route == "bot_hwid_reset":
        lf.licenses_table().update_item(Key={"license_key": lic},
                                        UpdateExpression="SET discord_user_id = :d",
                                        ExpressionAttributeValues={":d": "900000000000000001"})
        return "POST", "/api/bot/hwid-reset", BOT_H, {"discord_id": "900000000000000001",
                                                      "actor_discord_id": "900000000000000001",
                                                      "reason": "x"}, lic_row
    raise AssertionError(route)


def _support_headers(lf):
    put_staff(lf, "sup-aud", role="support", machine_id="M-SUP")
    s, b, _ = invoke(lf, "POST", "/api/staff/login", body={
        "staff_id": "sup-aud", "machine_id": "M-SUP", **make_staff_nonce_ts()})
    assert s == 200
    return {"authorization": "Bearer " + b["token"], "x-machine-id": "M-SUP"}


DESTRUCTIVE_ROUTES = [
    "kill_global", "kill_license", "unkill_global", "unkill_license", "provision",
    "license_create", "license_revoke", "blacklist", "staff_create", "staff_disable",
    "staff_enable", "staff_set_role", "staff_set_caps", "staff_reset_machine",
    "staff_reissue_enrollment", "rotate_admin_secret", "shard_revoke", "update_publish",
    "staff_license_reset", "bot_hwid_reset",
]


class TestRequiredDestructiveAudit:
    @pytest.mark.parametrize("route", DESTRUCTIVE_ROUTES)
    def test_audit_outage_refuses_with_state_unchanged(self, lf, quiet_alerts, audit_switch, route):
        method, path, headers, body, snap = _setup_and_request(lf, route)
        before = snap()
        audit_switch.fail = True
        s, b, _ = invoke(lf, method, path, headers=headers, body=body)
        assert s == 503 and b.get("error") == "audit_unavailable", (route, s, b)
        audit_switch.fail = False
        assert snap() == before, route

    def test_kill_writes_attempt_then_idempotent_completion(self, lf, quiet_alerts):
        s, _, _ = invoke(lf, "POST", "/api/admin/kill", headers=OWNER_H,
                         body={"target_type": "global", "reason": "incident"})
        assert s == 200
        attempt = audit_rows(lf, "config.set.attempt")
        done = audit_rows(lf, "config.set")
        assert attempt and done and attempt[0]["result"] == "attempt"
        assert done[0]["event_id"] == attempt[0]["event_id"] + "-ok"
        # completion is idempotent: re-writing it replaces, never duplicates
        lf.audit_complete(attempt[0]["event_id"], lf.make_actor("owner", "break-glass", "owner"),
                          "config.set", target="global_kill", target_type="config")
        assert len([r for r in audit_rows(lf, "config.set")
                    if r["event_id"].startswith(attempt[0]["event_id"])]) == 1

    def test_completion_audit_failure_does_not_undo_or_hide_the_mutation(self, lf, quiet_alerts,
                                                                        audit_switch, monkeypatch):
        real_put = audit_switch.real.put_item
        n = {"i": 0}

        def flaky(**kw):
            n["i"] += 1
            if n["i"] > 1:           # attempt row lands, completion rows fail
                raise OSError("late audit outage")
            return real_put(**kw)
        monkeypatch.setattr(audit_switch, "put_item", flaky)
        s, b, _ = invoke(lf, "POST", "/api/admin/kill", headers=OWNER_H,
                         body={"target_type": "global", "reason": "incident"})
        assert s == 200 and lf.get_global_kill()[0] is True
        assert audit_rows(lf, "config.set.attempt")


# ══════════════════════════════════════════════════════════════════════════════
# RT-LOW-04 Stripe event.id dedup
# ══════════════════════════════════════════════════════════════════════════════
SUB = "sub_dedup1"
DISCORD = "123456789012345678"


def _paid_row(lf, key="ORION-STRP-BBBB-CCCC"):
    put_license(lf, key, machine_id="MACHINE-A", discord_user_id=DISCORD,
                order_id="stripe:checkout:cs_x", extra={"stripe_subscription_id": SUB})
    return key


def _chargeback(lf, event_id=None, headers=WORKER_H):
    body = {"discord_user_id": DISCORD, "kind": "refunded",
            "reason": "Stripe charge.refunded ch_1", "subscription_id": SUB}
    if event_id:
        body["stripe_event_id"] = event_id
    return invoke(lf, "POST", "/api/bot/chargeback", headers=headers, body=body)


class TestStripeEventDedup:
    def test_replayed_event_is_a_noop(self, lf, quiet_alerts):
        key = _paid_row(lf)
        s, b, _ = _chargeback(lf, "evt_refund1")
        assert s == 200 and b["revoked"] is True
        s, b, _ = _chargeback(lf, "evt_refund1")
        assert s == 200 and b.get("duplicate_event") is True
        rows = [r for r in audit_rows(lf, "webhook.chargeback") if r.get("result") == "ok"]
        assert len(rows) == 1
        assert quiet_alerts.count("webhook.chargeback") == 1
        assert lf.licenses_table().get_item(Key={"license_key": key})["Item"]["revoked"] is True

    def test_replay_without_event_id_of_an_already_revoked_subscription_is_idempotent(
            self, lf, quiet_alerts):
        _paid_row(lf)
        s, _, _ = _chargeback(lf)
        assert s == 200
        s, b, _ = _chargeback(lf)
        assert s == 200 and b.get("already_revoked") is True
        assert quiet_alerts.count("webhook.chargeback") == 1

    def test_failed_processing_releases_the_claim_for_stripe_retry(self, lf, quiet_alerts):
        s, b, _ = _chargeback(lf, "evt_retry1")          # no licence yet -> 404
        assert s == 404
        _paid_row(lf)
        s, b, _ = _chargeback(lf, "evt_retry1")          # Stripe retries the same id
        assert s == 200 and b["revoked"] is True and not b.get("duplicate_event")

    def test_event_in_progress_is_retriable(self, lf, quiet_alerts):
        _paid_row(lf)
        lf.nonces_table().put_item(Item={"nonce": "stripe_evt:evt_busy1", "state": "processing",
                                         "claimed_at": int(time.time()), "expires": 0})
        s, b, _ = _chargeback(lf, "evt_busy1")
        assert s == 409 and b["error"] == "event_in_progress"

    def test_event_id_only_from_the_worker(self, lf, quiet_alerts):
        _paid_row(lf)
        s, b, _ = _chargeback(lf, "evt_x1", headers=BOT_H)
        assert s == 403

    def test_malformed_event_id_rejected(self, lf, quiet_alerts):
        _paid_row(lf)
        s, b, _ = _chargeback(lf, "not-an-event")
        assert s == 400 and b["error"] == "invalid_event_id"

    def test_provision_replay_is_a_noop_and_mints_once(self, lf, monkeypatch):
        dms = []
        monkeypatch.setattr(lf, "_discord_dm", lambda d, e: dms.append(d) or {"id": "x"})
        body = {"order_id": "stripe:checkout:cs_dedup1", "discord_user_id": DISCORD,
                "plan": "month", "days": 30, "renew": False, "notify": True,
                "subscription_id": SUB, "stripe_event_id": "evt_paid1"}
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s == 201, b
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s == 200 and b.get("duplicate_event") is True
        minted = [r for r in lf.licenses_table().scan()["Items"]
                  if r.get("order_id") == "stripe:checkout:cs_dedup1"]
        assert len(minted) == 1 and len(dms) == 1


# ══════════════════════════════════════════════════════════════════════════════
# RT-MED-12 heartbeat lease bound to the request nonce
# ══════════════════════════════════════════════════════════════════════════════
def _verify_v2(key, machine, expires, issued, nonce, sig):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    msg = (f"orion-lease-v2\n{key.strip().upper()}\n{machine.strip()}\n{int(expires)}\n"
           f"{int(issued)}\n{nonce}").encode()
    try:
        Ed25519PublicKey.from_public_bytes(LEASE_PUB_RAW).verify(
            base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), msg)
        return True
    except InvalidSignature:
        return False


class TestLeaseNonceBinding:
    def test_v2_lease_binds_nonce_and_issue_time(self, lf):
        key = "ORION-LEAS-BBBB-CCCC"
        put_license(lf, key, machine_id="MACHINE-A")
        nonce = "n-" + "a1b2c3d4e5f6a7b8c9d0"
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": key, "machine_id": "MACHINE-A", "client_version": "1.0.0",
            "lease_nonce": nonce})
        assert s == 200
        assert b["lease_nonce"] == nonce and b["lease_sig_version"] == 2
        assert abs(int(b["lease_issued_at"]) - time.time()) < 60
        assert _verify_v2(key, "MACHINE-A", b["lease_expires_at"], b["lease_issued_at"],
                          nonce, b["lease_sig_v2"])
        # a response captured for nonce A does not verify for a fresh nonce B
        assert not _verify_v2(key, "MACHINE-A", b["lease_expires_at"], b["lease_issued_at"],
                              "n-" + "ffffffffffffffffffff", b["lease_sig_v2"])
        # v1 stays intact for already-shipped clients
        assert verify_lease_sig(key, "MACHINE-A", b["lease_expires_at"], b["lease_sig"])

    def test_v1_clients_unchanged(self, lf):
        key = "ORION-LEAV-BBBB-CCCC"
        put_license(lf, key, machine_id="MACHINE-A")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": key, "machine_id": "MACHINE-A", "client_version": "1.0.0"})
        assert s == 200 and b["lease_sig"] and "lease_sig_v2" not in b

    @pytest.mark.parametrize("nonce", ["short", "x" * 200, "bad\nnonce-0123456789", 12345])
    def test_malformed_nonce_rejected(self, lf, nonce):
        key = "ORION-LEAB-BBBB-CCCC"
        put_license(lf, key, machine_id="MACHINE-A")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": key, "machine_id": "MACHINE-A", "client_version": "1.0.0",
            "lease_nonce": nonce})
        assert s == 400 and b["error"] == "invalid_lease_nonce"


# ══════════════════════════════════════════════════════════════════════════════
# P-F follow-up: re-enrolment while TOTP is required, and totp_confirm replay
# ══════════════════════════════════════════════════════════════════════════════
def _bg(code):
    return {**OWNER_H, "x-orion-admin-totp": code}


class TestTotpReEnrolment:
    def test_reenrol_keeps_the_current_factor_until_confirm(self, lf, quiet_alerts):
        """P-F issue 1: totp_enroll with owner_totp_required=true used to overwrite
        the ACTIVE secret, so the break-glass session code died mid-enrolment."""
        old = _enroll_totp(lf)
        session = _code(lf, old, 0)
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=_bg(session))
        assert s == 200
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**_bg(session), },
                         body={"action": "totp_enroll", "reason": "new phone",
                               "step_up_code": _code(lf, old, 1)})
        assert s == 200 and b["secret"], b
        new = b["secret"]
        # the session code of the CURRENT factor still opens the owner surface
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=_bg(session))
        assert s == 200, b
        assert lf.owner_totp_secret() == old
        # confirming with the NEW secret switches the factor over
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=_bg(session),
                         body={"action": "totp_confirm", "code": _code(lf, new, 0),
                               "reason": "switch"})
        assert s == 200 and b["owner_totp_required"] is True, b
        assert lf.owner_totp_secret() == new
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=_bg(_code(lf, old, -1)))
        assert s == 403
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=_bg(_code(lf, new, 1)))
        assert s == 200

    def test_wrong_confirm_code_leaves_the_current_factor_active(self, lf, quiet_alerts):
        old = _enroll_totp(lf)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=_bg(_code(lf, old, 0)),
                         body={"action": "totp_enroll", "reason": "x",
                               "step_up_code": _code(lf, old, 1)})
        assert s == 200
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=_bg(_code(lf, old, 0)),
                         body={"action": "totp_confirm", "code": _code(lf, old, -1),
                               "reason": "x"})
        assert s == 403 and b["error"] == "invalid_totp"
        assert lf.owner_totp_secret() == old

    def test_confirm_with_one_code_in_one_window(self, lf, quiet_alerts):
        """P-F issue 2: the step-up code was burned BEFORE the confirm code was
        checked, so a header code and a body code from the same 30 s step of the
        same secret failed as totp_replayed."""
        secret = _enroll_totp(lf)
        h = _owner_bearer(lf, secret)
        code = _code(lf, secret, 0)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": code},
                         body={"action": "totp_confirm", "code": code, "reason": "x"})
        assert s == 200 and b["owner_totp_required"] is True, b
        # ...and the code is still single-use afterwards
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": code},
                         body={"action": "totp_disable", "reason": "x"})
        assert s == 403 and b["error"] == "totp_replayed"

    def test_confirm_after_fresh_reenrol_in_the_same_window(self, lf, quiet_alerts):
        """The P-F console flow: the enrolment step-up and the confirm are made
        seconds apart; neither may collide with the other's burned step."""
        old = _enroll_totp(lf)
        h = _owner_bearer(lf, old)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": _code(lf, old, 0)},
                         body={"action": "totp_enroll", "reason": "x"})
        assert s == 200
        new = b["secret"]
        c = _code(lf, new, 0)
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**h, "x-orion-admin-totp": c},
                         body={"action": "totp_confirm", "code": c, "reason": "x"})
        assert s == 200, b
        assert lf.owner_totp_secret() == new

    def test_stolen_bearer_cannot_confirm_without_the_new_secret(self, lf, quiet_alerts):
        old = _enroll_totp(lf)
        h = _owner_bearer(lf, old)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=h,
                         body={"action": "totp_confirm", "code": "123456", "reason": "x"})
        assert s == 403
        assert lf.owner_totp_secret() == old
