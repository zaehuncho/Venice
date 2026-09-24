"""P-H remembered sign-in (owner-approved 2026-09-23): an oauth_pair_required key
may re-activate WITHOUT a PAIR- exchange ONLY on the machine it is already bound
to, only if every other activation check passes, and without changing the binding.
Anything else (new bind, other machine, unbound key, post-HWID-reset) still needs
the Discord pairing (`discord_signin_required`)."""
import time

from conftest import (TEST_BOT_SECRET, TEST_PAIR_SECRET, invoke, make_nonce_ts, no_dm,
                      audit_rows, put_license)

DISCORD_ID = "423456789012345678"


def _claim_trial(lf, monkeypatch):
    no_dm(lf, monkeypatch)
    s, b, _ = invoke(lf, "POST", "/api/bot/trial", body={"discord_id": DISCORD_ID},
                     headers={"x-orion-bot-secret": TEST_BOT_SECRET})
    assert s == 200 and b["ok"], b
    rows = lf.licenses_table().scan().get("Items", [])
    return next(r for r in rows if r.get("discord_user_id") == DISCORD_ID
                and r.get("plan") == "trial")


def _activate(lf, key, machine="OWNER-PC", nonce=None, **extra):
    nt = nonce or make_nonce_ts()
    return invoke(lf, "POST", "/api/license/redeem",
                  body={"license_key": key, "machine_id": machine, **nt, **extra})


def _paired_bind(lf, monkeypatch, machine="OWNER-PC"):
    trial = _claim_trial(lf, monkeypatch)
    _, issued, _ = invoke(lf, "POST", "/api/bot/pair-issue", body={"discord_id": DISCORD_ID},
                          headers={"x-orion-pair-secret": TEST_PAIR_SECRET})
    s, b, _ = _activate(lf, issued["pair_code"], machine)
    assert s == 200, b
    assert b["canonical_license_key"] == trial["license_key"]
    return trial["license_key"]


def _row(lf, key):
    return lf.licenses_table().get_item(Key={"license_key": key})["Item"]


def test_remembered_key_resumes_on_the_bound_machine(lf, monkeypatch):
    key = _paired_bind(lf, monkeypatch)
    before = _row(lf, key)
    s, b, _ = _activate(lf, key, "OWNER-PC")
    assert s == 200 and b["ok"] and b["token"], b
    after = _row(lf, key)
    # the binding is untouched (no new bind, no activation count bump)
    assert after["machine_id"] == "OWNER-PC"
    assert int(after["activations"]) == int(before["activations"])
    assert "canonical_license_key" not in b          # nothing new disclosed
    rows = audit_rows(lf, "activate_success")
    assert any((r.get("details") or {}).get("reason") == "bound_machine_resume" for r in rows)


def test_other_machine_still_needs_discord(lf, monkeypatch):
    key = _paired_bind(lf, monkeypatch)
    s, b, _ = _activate(lf, key, "ATTACKER-PC")
    assert s == 403 and b["error"] == "discord_signin_required"
    assert _row(lf, key)["machine_id"] == "OWNER-PC"


def test_unbound_key_still_needs_discord(lf, monkeypatch):
    trial = _claim_trial(lf, monkeypatch)
    s, b, _ = _activate(lf, trial["license_key"], "OWNER-PC")
    assert s == 403 and b["error"] == "discord_signin_required"
    assert not _row(lf, trial["license_key"]).get("machine_id")


def test_after_hwid_reset_still_needs_discord(lf, monkeypatch):
    key = _paired_bind(lf, monkeypatch)
    lf.licenses_table().update_item(Key={"license_key": key},
                                    UpdateExpression="SET machine_id = :e",
                                    ExpressionAttributeValues={":e": ""})
    s, b, _ = _activate(lf, key, "OWNER-PC")
    assert s == 403 and b["error"] == "discord_signin_required"


def test_resume_still_runs_every_validity_check(lf, monkeypatch):
    key = _paired_bind(lf, monkeypatch)
    table = lf.licenses_table()
    # revoked
    table.update_item(Key={"license_key": key}, UpdateExpression="SET revoked = :r",
                      ExpressionAttributeValues={":r": True})
    s, b, _ = _activate(lf, key)
    assert s == 403 and b["error"] == "license_revoked"
    table.update_item(Key={"license_key": key}, UpdateExpression="SET revoked = :r",
                      ExpressionAttributeValues={":r": False})
    # frozen
    table.update_item(Key={"license_key": key}, UpdateExpression="SET #s = :s",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "frozen"})
    s, b, _ = _activate(lf, key)
    assert s == 403 and b["error"] == "frozen"
    table.update_item(Key={"license_key": key}, UpdateExpression="SET #s = :s",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "active"})
    # expired
    table.update_item(Key={"license_key": key}, UpdateExpression="SET expiry = :e",
                      ExpressionAttributeValues={":e": int(time.time()) - 10})
    s, b, _ = _activate(lf, key)
    assert s == 403 and b["error"] == "license_expired"
    table.update_item(Key={"license_key": key}, UpdateExpression="SET expiry = :e",
                      ExpressionAttributeValues={":e": int(time.time()) + 86400})
    # global kill engaged, and kill state unknown (fail closed, retriable)
    lf.config_table().put_item(Item={"config_key": "global_kill", "enabled": True,
                                     "reason": "incident", "set_at": 1})
    s, b, _ = _activate(lf, key)
    assert s == 503 and b["error"].startswith("service_disabled")
    lf.config_table().put_item(Item={"config_key": "global_kill", "enabled": False,
                                     "reason": "", "set_at": 1})
    # machine blacklist
    table.put_item(Item={"license_key": lf._blacklist_key("machine", "OWNER-PC"),
                         "status": "blacklist", "revoked": True})
    s, b, _ = _activate(lf, key)
    assert s == 403 and b["error"] == "blacklisted"
    table.delete_item(Key={"license_key": lf._blacklist_key("machine", "OWNER-PC")})
    # sanity: clean again -> resumes
    s, b, _ = _activate(lf, key)
    assert s == 200, b


def test_resume_keeps_nonce_replay_protection(lf, monkeypatch):
    key = _paired_bind(lf, monkeypatch)
    nt = make_nonce_ts()
    s, _, _ = _activate(lf, key, nonce=nt)
    assert s == 200
    s, b, _ = _activate(lf, key, nonce=nt)
    assert s == 401 and b["error"] == "replay_detected"
    s, b, _ = _activate(lf, key, nonce={"request_nonce": "fresh-nonce-xyz",
                                        "request_timestamp": int(time.time()) - 3600})
    assert s == 401 and b["error"] == "timestamp_expired"


def test_resume_races_a_concurrent_hwid_reset_fail_closed(lf, monkeypatch):
    """The binding is re-checked atomically at write time: a reset landing between
    the read and the write must not let the unpaired resume through (or re-bind)."""
    key = _paired_bind(lf, monkeypatch)
    real_get = lf.licenses_table

    class ResetAfterRead:
        def __init__(self):
            self.t = real_get()
            self.done = False

        def get_item(self, **kw):
            r = self.t.get_item(**kw)
            if kw.get("Key", {}).get("license_key") == key and not self.done:
                self.done = True
                self.t.update_item(Key={"license_key": key},
                                   UpdateExpression="SET machine_id = :e",
                                   ExpressionAttributeValues={":e": ""})
            return r

        def __getattr__(self, n):
            return getattr(self.t, n)

    wrapper = ResetAfterRead()
    monkeypatch.setattr(lf, "licenses_table", lambda: wrapper)
    s, b, _ = _activate(lf, key)
    assert s == 403 and b["error"] == "discord_signin_required", b
    monkeypatch.setattr(lf, "licenses_table", real_get)
    assert not _row(lf, key).get("machine_id")


def test_paid_stripe_key_resumes_on_its_bound_machine(lf):
    key = "ORION-STRB-BBBB-CCCC"
    put_license(lf, key, machine_id="OWNER-PC", activations=1,
                extra={"oauth_pair_required": True})
    s, b, _ = _activate(lf, key, "OWNER-PC")
    assert s == 200, b
    s, b, _ = _activate(lf, key, "OTHER-PC")
    assert s == 403 and b["error"] == "discord_signin_required"
