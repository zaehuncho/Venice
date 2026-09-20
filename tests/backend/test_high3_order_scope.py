"""HIGH-3: bot mint endpoints are order-scoped (spend-once) and behind edge auth.
A replayed provision cannot mint a second license for the same order_id."""
from conftest import invoke, put_staff, TEST_BOT_SECRET

BOT_H = {"x-orion-bot-secret": TEST_BOT_SECRET}


def _active_license_rows(lf):
    rows = lf.licenses_table().scan().get("Items", [])
    return [r for r in rows if r.get("status") == "active" and not str(r["license_key"]).startswith("ORDER#")]


class TestProvisionOrderScope:
    def test_provision_requires_order_id(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/provision",
            body={"plan": "month", "days": 30}, headers=BOT_H)
        assert status == 400
        assert body["error"] == "order_id required"

    def test_provision_mints_once_per_order(self, lf):
        payload = {"plan": "lifetime", "days": None, "order_id": "ORDER-XYZ",
                   "discord_user_id": "42"}
        s1, b1, _ = invoke(lf, "POST", "/api/bot/provision", body=payload, headers=BOT_H)
        assert s1 == 201
        first_key = b1["license_key"]
        # Replay the exact same order -> no new license; returns the same key.
        s2, b2, _ = invoke(lf, "POST", "/api/bot/provision", body=payload, headers=BOT_H)
        assert s2 == 200
        assert b2.get("duplicate_order") is True
        assert b2["license_key"] == first_key
        # Exactly ONE real license row exists for this order.
        rows = _active_license_rows(lf)
        assert len(rows) == 1
        assert rows[0]["order_id"] == "ORDER-XYZ"

    def test_deliver_dedupes_when_order_id_present(self, lf):
        # §4 BREAKING: /deliver now requires actor_discord_id resolved to a staff
        # row with role admin+.
        put_staff(lf, "adm1", role="admin", discord_user_id="9001")
        payload = {"plan": "week", "order_id": "DLV-1", "discord_id": "7",
                   "actor_discord_id": "9001", "reason": "manual delivery"}
        s1, b1, _ = invoke(lf, "POST", "/api/bot/deliver", body=payload, headers=BOT_H)
        assert s1 == 200 and b1["ok"] is True
        first_key = b1["license_key"]
        s2, b2, _ = invoke(lf, "POST", "/api/bot/deliver", body=payload, headers=BOT_H)
        assert b2.get("duplicate_order") is True
        assert b2["license_key"] == first_key
        assert len(_active_license_rows(lf)) == 1

    def test_provision_denied_without_bot_secret(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/provision",
            body={"plan": "month", "order_id": "O-1"})
        assert status == 403
        assert body["error"] == "forbidden"
