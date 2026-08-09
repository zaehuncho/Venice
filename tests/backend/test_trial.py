import time
from conftest import invoke, TEST_BOT_SECRET, make_nonce_ts


class TestTrial:
    def test_trial_already_claimed(self, lf):
        discord_id = "111222333"
        # First claim
        s1, b1, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s1 == 200
        assert b1["ok"] is True
        # Second claim -> already claimed (ok=False + message, no "error" key)
        s2, b2, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s2 == 200
        assert b2["ok"] is False
        assert b2["message"] == "You've already claimed your free trial."

    def test_trial_hwid_guard_blocks_second(self, lf):
        # First trial from MACHINE-T1
        discord_id1 = "444555666"
        s1, b1, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id1},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s1 == 200
        assert b1["ok"] is True
        trial_key = b1["license_key"]
        # Activate from MACHINE-T1
        nt = make_nonce_ts()
        sa, ba, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": trial_key, "machine_id": "MACHINE-T1", **nt})
        assert sa == 200
        # Now a second discord user claims a trial
        discord_id2 = "777888999"
        s2, b2, _ = invoke(lf, "POST", "/api/bot/trial",
            body={"discord_id": discord_id2},
            headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert s2 == 200
        assert b2["ok"] is True
        trial_key2 = b2["license_key"]
        # Try to activate the second trial from the SAME machine -> blocked
        nt2 = make_nonce_ts()
        s3, b3, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": trial_key2, "machine_id": "MACHINE-T1", **nt2})
        assert s3 == 403
        assert b3["error"] == "trial_used"
