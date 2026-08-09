from conftest import invoke, put_license, TEST_ADMIN_SECRET, TEST_BOT_SECRET


class TestAdminAuth:
    def test_admin_rejects_wrong_secret(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/admin/search",
            qs={"q": "ORION-TEST"}, headers={"x-orion-admin-secret": "wrong"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_admin_rejects_missing_secret(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/admin/search",
            qs={"q": "ORION-TEST"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_admin_accepts_correct_secret(self, lf):
        put_license(lf, "ORION-ADMIN-AUTH-TEST")
        status, body, _ = invoke(lf, "GET", "/api/admin/search",
            qs={"q": "ORION-ADMIN-AUTH-TEST"},
            headers={"x-orion-admin-secret": TEST_ADMIN_SECRET})
        assert status == 200
        assert body["ok"] is True
        assert body["found"] is True
        assert body["license"]["license_key_suffix"] == "TEST"

    def test_admin_search_not_found(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/admin/search",
            qs={"q": "ORION-NOPE-NOPE-NOPE"},
            headers={"x-orion-admin-secret": TEST_ADMIN_SECRET})
        assert status == 200
        assert body["ok"] is True
        assert body["found"] is False

    def test_admin_whoami_requires_secret(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/admin/whoami")
        assert status == 403
        status, body, _ = invoke(lf, "GET", "/api/admin/whoami",
            headers={"x-orion-admin-secret": TEST_ADMIN_SECRET})
        assert status == 200
        assert body["ok"] is True
        assert body["role"] == "admin"


class TestBotAuth:
    def test_bot_rejects_wrong_secret(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/killswitch",
            body={"action": "status"}, headers={"x-orion-bot-secret": "wrong"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_bot_rejects_missing_secret(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/killswitch",
            body={"action": "status"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_bot_accepts_correct_secret(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/killswitch",
            body={"action": "status"}, headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert status == 200
        assert body["ok"] is True
