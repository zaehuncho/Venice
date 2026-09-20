import time
from conftest import invoke, TEST_BOT_SECRET, TEST_PAIR_SECRET, make_nonce_ts, no_dm


def _trial_key(lf, discord_id):
    """The private database key never reaches the customer or bot response."""
    rows = lf.licenses_table().scan().get("Items", [])
    cand = [r for r in rows
            if r.get("discord_user_id") == discord_id and r.get("plan") == "trial"]
    assert cand, f"no trial row for {discord_id}"
    return cand[0]["license_key"]


def _pair_code(lf, discord_id):
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-issue",
        body={"discord_id": discord_id},
        headers={"x-orion-pair-secret": TEST_PAIR_SECRET})
    assert status == 200 and body["ok"]
    return body["pair_code"]


class TestTrial:
    def test_discord_id_is_not_a_bearer_license(self, lf, monkeypatch):
        """A public account ID must never unlock a claimed, live trial."""
        sent = no_dm(lf, monkeypatch)
        discord_id = "123456789012345678"
        status, claim, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert status == 200 and claim["ok"] is True

        private_key = _trial_key(lf, discord_id)
        assert "License Key" not in str(sent[0])
        assert "https://zaeorion.com/connect" in str(sent[0])
        assert private_key != discord_id
        assert lf.licenses_table().get_item(
            Key={"license_key": discord_id}).get("Item") is None

        copied_status, copied_body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": discord_id, "machine_id": "ATTACKER-PC",
                  **make_nonce_ts()})
        assert copied_status == 403
        assert copied_body["error"] == "invalid_key"

        owner_status, owner_body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": private_key, "machine_id": "OWNER-PC",
                  **make_nonce_ts()})
        assert owner_status == 403 and owner_body["error"] == "discord_signin_required"

        owner_status, owner_body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": _pair_code(lf, discord_id), "machine_id": "OWNER-PC",
                  **make_nonce_ts()})
        assert owner_status == 200 and owner_body["ok"] is True

        copied_check_status, copied_check, _ = invoke(lf, "POST",
            "/api/license/check", body={
                "license_key": discord_id, "machine_id": "ATTACKER-PC"})
        assert copied_check_status == 403
        assert copied_check["error"] == "invalid_key"

        stolen_key_status, stolen_key_body, _ = invoke(lf, "POST",
            "/api/activate", body={
                "license_key": private_key, "machine_id": "ATTACKER-PC",
                **make_nonce_ts()})
        assert stolen_key_status == 403
        assert stolen_key_body["error"] == "discord_signin_required"
        wrong_pc_status, wrong_pc, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": _pair_code(lf, discord_id),
                  "machine_id": "ATTACKER-PC", **make_nonce_ts()})
        assert wrong_pc_status == 403 and wrong_pc["error"] == "device_mismatch"

    def test_trial_days_exist_only_until_expiry(self, lf, monkeypatch):
        no_dm(lf, monkeypatch)
        discord_id = "223456789012345678"
        status, claim, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert status == 200 and claim["ok"] is True
        private_key = _trial_key(lf, discord_id)
        item = lf.licenses_table().get_item(
            Key={"license_key": private_key})["Item"]
        assert item["plan"] == "trial"
        # Bound the trial by the SHIPPED constant, not a literal, so changing the trial
        # length is a one-line policy change and not a test-fixing exercise.
        assert 0 < int(item["expiry"]) - int(item["created_at"]) <= lf.TRIAL_DAYS * 86400

        lf.licenses_table().update_item(
            Key={"license_key": private_key},
            UpdateExpression="SET expiry = :expired",
            ExpressionAttributeValues={":expired": int(time.time()) - 1})
        expired_status, expired_body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": private_key, "machine_id": "OWNER-PC",
                  **make_nonce_ts()})
        assert expired_status == 403
        assert expired_body["error"] == "license_expired"

    def test_trial_already_claimed(self, lf, monkeypatch):
        # The server-side trial DM is a live HTTPS call; stub the transport so the
        # (correct) DM-bounce rollback does not fire offline.
        sent = no_dm(lf, monkeypatch)
        discord_id = "111222333"
        # First claim
        s1, b1, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s1 == 200
        assert b1["ok"] is True
        assert len(sent) == 1
        # Second claim -> already claimed (ok=False + message, no "error" key)
        s2, b2, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s2 == 200
        assert b2["ok"] is False
        assert b2["message"] == "You've already claimed your free trial."

    def test_trial_dm_bounce_rolls_the_claim_back(self, lf, monkeypatch):
        """Revert-catcher: a bounced DM must release the one-per-account claim so
        the customer can retry, instead of permanently burning their trial."""
        no_dm(lf, monkeypatch, fail=True)
        discord_id = "999000111"
        s1, b1, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s1 == 200
        assert b1["ok"] is False
        assert "DM" in b1["message"]
        assert lf.licenses_table().get_item(
            Key={"license_key": "TRIAL#" + discord_id}).get("Item") is None
        # Retry with a working DM now succeeds.
        no_dm(lf, monkeypatch)
        s2, b2, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s2 == 200 and b2["ok"] is True

    def test_trial_hwid_guard_blocks_second(self, lf, monkeypatch):
        no_dm(lf, monkeypatch)
        # First trial from MACHINE-T1
        discord_id1 = "444555666111222333"
        s1, b1, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id1},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s1 == 200
        assert b1["ok"] is True
        pair_code = _pair_code(lf, discord_id1)
        # Activate from MACHINE-T1
        nt = make_nonce_ts()
        sa, ba, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": pair_code, "machine_id": "MACHINE-T1", **nt})
        assert sa == 200
        # Now a second discord user claims a trial
        discord_id2 = "777888999111222333"
        s2, b2, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id2},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s2 == 200
        assert b2["ok"] is True
        pair_code2 = _pair_code(lf, discord_id2)
        # Try to activate the second trial from the SAME machine -> blocked
        nt2 = make_nonce_ts()
        s3, b3, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": pair_code2, "machine_id": "MACHINE-T1", **nt2})
        assert s3 == 403
        assert b3["error"] == "trial_used"

    def test_blacklisted_account_cannot_claim_trial(self, lf, monkeypatch):
        no_dm(lf, monkeypatch)
        discord_id = "666555444"
        lf.licenses_table().put_item(Item={
            "license_key": "BLACKLIST#discord#" + discord_id,
            "status": "blacklist", "revoked": True, "created_at": int(time.time())})
        s, b, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s == 403
        assert b["error"] == "blacklisted"
