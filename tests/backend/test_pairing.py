"""A public Discord ID is a lookup key, never launcher authority."""
import hashlib
import time

from conftest import TEST_BOT_SECRET, TEST_PAIR_SECRET, invoke, make_nonce_ts, no_dm


DISCORD_ID = "323456789012345678"


def _claim_trial(lf, monkeypatch):
    no_dm(lf, monkeypatch)
    status, body, _ = invoke(lf, "POST", "/api/bot/trial",
        body={"discord_id": DISCORD_ID},
        headers={"x-orion-bot-secret": TEST_BOT_SECRET})
    assert status == 200 and body["ok"]
    rows = lf.licenses_table().scan().get("Items", [])
    return next(row for row in rows if row.get("discord_user_id") == DISCORD_ID
                and row.get("plan") == "trial")


def _issue(lf, discord_id=DISCORD_ID, authorized=True):
    headers = {"x-orion-pair-secret": TEST_PAIR_SECRET} if authorized else {}
    return invoke(lf, "POST", "/api/bot/pair-issue",
        body={"discord_id": discord_id}, headers=headers)


def _activate(lf, key, machine="OWNER-PC"):
    return invoke(lf, "POST", "/api/license/redeem",
        body={"license_key": key, "machine_id": machine, **make_nonce_ts()})


def test_pair_issue_requires_trusted_oauth_relay_and_live_trial(lf, monkeypatch):
    status, body, _ = _issue(lf, authorized=False)
    assert status == 403 and body["error"] == "forbidden"
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-issue",
        body={"discord_id": DISCORD_ID},
        headers={"x-orion-bot-secret": TEST_BOT_SECRET})
    assert status == 403 and body["error"] == "forbidden"
    status, body, _ = _issue(lf)
    assert status == 403 and body["error"] == "subscription_required"
    trial = _claim_trial(lf, monkeypatch)
    status, body, _ = _issue(lf)
    assert status == 200 and body["ok"]
    assert body["pair_code"].startswith("PAIR-")
    assert len(body["pair_code"]) == 37
    assert body["pair_code"] != DISCORD_ID
    marker = "PAIR#" + hashlib.sha256(body["pair_code"].encode()).hexdigest()
    stored = lf.licenses_table().get_item(Key={"license_key": marker})["Item"]
    assert stored["pair_discord_id"] == DISCORD_ID
    assert body["pair_code"] not in str(stored)
    assert 0 < int(stored["expires"]) - int(time.time()) <= 300
    assert trial["license_key"] not in str(body)


def test_pair_code_exchanges_once_for_canonical_key_and_bound_heartbeat(lf, monkeypatch):
    trial = _claim_trial(lf, monkeypatch)
    _, issued, _ = _issue(lf)
    code = issued["pair_code"]

    copied_status, copied, _ = _activate(lf, DISCORD_ID, "ATTACKER-PC")
    assert copied_status == 403 and copied["error"] == "invalid_key"

    private_status, private_body, _ = _activate(lf, trial["license_key"], "ATTACKER-PC")
    assert private_status == 403 and private_body["error"] == "discord_signin_required"

    status, activated, _ = _activate(lf, code)
    assert status == 200 and activated["ok"]
    assert activated["canonical_license_key"] == trial["license_key"]
    assert activated["canonical_license_key"] != code

    check_status, check, _ = invoke(lf, "POST", "/api/license/check",
        body={"license_key": activated["canonical_license_key"],
              "machine_id": "OWNER-PC"})
    assert check_status == 200 and check["ok"]

    replay_status, replay, _ = _activate(lf, code)
    assert replay_status == 403 and replay["error"] == "pair_invalid"

    _, fresh, _ = _issue(lf)
    wrong_pc_status, wrong_pc, _ = _activate(lf, fresh["pair_code"], "ATTACKER-PC")
    assert wrong_pc_status == 403 and wrong_pc["error"] == "device_mismatch"


def test_expired_pair_code_and_expired_trial_fail_closed(lf, monkeypatch):
    trial = _claim_trial(lf, monkeypatch)
    _, issued, _ = _issue(lf)
    code = issued["pair_code"]
    marker = "PAIR#" + hashlib.sha256(code.encode()).hexdigest()
    lf.licenses_table().update_item(Key={"license_key": marker},
        UpdateExpression="SET expires = :old",
        ExpressionAttributeValues={":old": int(time.time()) - 1})
    status, body, _ = _activate(lf, code)
    assert status == 403 and body["error"] == "pair_invalid"

    lf.licenses_table().update_item(Key={"license_key": trial["license_key"]},
        UpdateExpression="SET expiry = :old",
        ExpressionAttributeValues={":old": int(time.time()) - 1})
    status, body, _ = _issue(lf)
    assert status == 403 and body["error"] == "subscription_required"


def test_paid_account_pairing_requires_a_live_verified_purchase(lf):
    key = "ABCD-EFGH-JKLM-NPQR"
    now = int(time.time())
    lf.licenses_table().put_item(Item={
        "license_key": key, "discord_user_id": DISCORD_ID,
        "source": "gumroad", "order_id": "stripe:checkout:fixture",
        "plan": "month", "status": "active", "revoked": False,
        "machine_id": "", "activations": 0, "max_devices": 1,
        "created_at": now, "expiry": now + 30 * 86400,
    })
    status, issued, _ = _issue(lf)
    assert status == 200
    status, activated, _ = _activate(lf, issued["pair_code"])
    assert status == 200 and activated["canonical_license_key"] == key

    lf.licenses_table().update_item(Key={"license_key": key},
        UpdateExpression="SET expiry = :old",
        ExpressionAttributeValues={":old": now - 1})
    status, body, _ = _issue(lf)
    assert status == 403 and body["error"] == "subscription_required"
