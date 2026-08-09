"""MED-1: activate now enforces the nonce+timestamp replay window and is
server-side rate-limited."""
import time
from conftest import invoke, put_license, make_nonce_ts


class TestActivateReplay:
    def test_nonce_replay_rejected(self, lf):
        put_license(lf, "ORION-REPLAY-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-REPLAY-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert s1 == 200
        # Same nonce again -> replay.
        s2, b2, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-REPLAY-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert s2 == 401
        assert b2["error"] == "replay_detected"

    def test_stale_timestamp_rejected(self, lf):
        put_license(lf, "ORION-STALE-BBBB-CCCC", machine_id="")
        nt = {"request_nonce": "nonce-stale-1", "request_timestamp": int(time.time()) - 10_000}
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-STALE-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
        assert status == 401
        assert body["error"] == "timestamp_expired"

    def test_missing_replay_fields_rejected(self, lf):
        put_license(lf, "ORION-NOREPLAY-BBBB-CCCC", machine_id="")
        status, body, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-NOREPLAY-BBBB-CCCC", "machine_id": "MACHINE-1"})
        assert status == 400
        assert body["error"] == "replay_fields_required"

    def test_replay_fields_via_headers_accepted(self, lf):
        # The client also sends them as X-Orion-Request-* headers.
        put_license(lf, "ORION-HDR-BBBB-CCCC", machine_id="")
        status, body, _ = invoke(lf, "POST", "/api/activate",
            body={"license_key": "ORION-HDR-BBBB-CCCC", "machine_id": "MACHINE-1"},
            headers={"x-orion-request-nonce": "hdr-nonce-1",
                     "x-orion-request-timestamp": str(int(time.time()))})
        assert status == 200
        assert body["ok"] is True

    def test_rate_limited_after_burst(self, lf):
        put_license(lf, "ORION-RL-BBBB-CCCC", machine_id="", max_devices=99)
        saw_429 = False
        for i in range(40):
            nt = {"request_nonce": f"rl-nonce-{i}", "request_timestamp": int(time.time())}
            status, body, _ = invoke(lf, "POST", "/api/activate", body={
                "license_key": "ORION-RL-BBBB-CCCC", "machine_id": "MACHINE-1", **nt})
            if status == 429:
                saw_429 = True
                assert body["error"] == "rate_limited"
                break
        assert saw_429, "expected a 429 within the burst window"
