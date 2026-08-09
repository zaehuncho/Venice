import boto3
from conftest import invoke, TEST_EDGE_SECRET, TEST_BOT_SECRET, put_license, make_nonce_ts


class TestEdgeAuth:
    """HIGH-4: edge auth is enforced on ALL routes and fails CLOSED."""

    def test_valid_edge_header_passes(self, lf):
        put_license(lf, "ORION-EDGE-TEST-KEY", machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-EDGE-TEST-KEY", "machine_id": "MACHINE-1", **nt})
        assert status == 200
        assert body["ok"] is True

    def test_missing_edge_header_denied(self, lf):
        put_license(lf, "ORION-EDGE-TEST-KEY2", machine_id="")
        nt = make_nonce_ts()
        # edge=False -> no X-Edge-Auth header at all: must be DENIED, not passed.
        status, body, _ = invoke(lf, "POST", "/api/activate", edge=False, body={
            "license_key": "ORION-EDGE-TEST-KEY2", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_wrong_edge_header_denied(self, lf):
        put_license(lf, "ORION-EDGE-TEST-KEY3", machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": "ORION-EDGE-TEST-KEY3", "machine_id": "MACHINE-1", **nt},
            headers={"x-edge-auth": "wrong-secret"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_bot_route_now_behind_edge_auth(self, lf):
        # HIGH-3: /api/bot/* is no longer exempt. Without the edge header it is denied
        # even with a valid bot secret.
        status, body, _ = invoke(lf, "POST", "/api/bot/killswitch", edge=False,
            body={"action": "status"}, headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_bot_route_with_edge_and_bot_secret(self, lf):
        status, body, _ = invoke(lf, "POST", "/api/bot/killswitch",
            body={"action": "status"}, headers={"x-orion-bot-secret": TEST_BOT_SECRET})
        assert status == 200
        assert body["ok"] is True

    def test_fail_closed_when_secret_absent(self, lf):
        # If the edge secret is not configured in SSM, the gate must DENY (fail
        # closed), not silently pass through.
        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.delete_parameter(Name="/orion/edge_auth_secret")
        put_license(lf, "ORION-EDGE-TEST-KEY4", machine_id="")
        nt = make_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-EDGE-TEST-KEY4", "machine_id": "MACHINE-1", **nt})
        assert status == 403
        assert body["error"] == "forbidden"
