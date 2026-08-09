"""NEW-2: device-cap bind is an atomic conditional write. A max_devices=1 key
cannot be fanned out to two machines, and the activation counter is not inflated
past the cap by a racing second bind."""
import time
from conftest import invoke, put_license, make_nonce_ts


class TestDeviceCapAtomic:
    def test_cap_holds_second_machine_rejected(self, lf):
        put_license(lf, "ORION-CAP-BBBB-CCCC", machine_id="", max_devices=1)
        nt1 = make_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-CAP-BBBB-CCCC", "machine_id": "MACHINE-A", **nt1})
        assert s1 == 200
        nt2 = make_nonce_ts()
        s2, b2, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-CAP-BBBB-CCCC", "machine_id": "MACHINE-B", **nt2})
        assert s2 == 403
        assert b2["error"] in ("device_mismatch", "device_limit_reached")
        item = lf.licenses_table().get_item(
            Key={"license_key": "ORION-CAP-BBBB-CCCC"}).get("Item", {})
        assert item["machine_id"] == "MACHINE-A"
        assert int(item["activations"]) == 1

    def test_conditional_write_backstops_toctou(self, lf):
        # Simulate the race directly: two binds that BOTH passed a stale pre-read
        # (activations==0). The atomic ConditionExpression must let only one win.
        put_license(lf, "ORION-RACE-BBBB-CCCC", machine_id="", max_devices=1)
        tbl = lf.licenses_table()
        cond = ("machine_id = :m OR attribute_not_exists(machine_id) OR machine_id = :empty "
                "OR activations < :max OR attribute_not_exists(activations)")

        def bind(machine):
            tbl.update_item(
                Key={"license_key": "ORION-RACE-BBBB-CCCC"},
                UpdateExpression="ADD activations :one SET machine_id = :m, last_activated = :t",
                ConditionExpression=cond,
                ExpressionAttributeValues={":one": 1, ":m": machine, ":t": int(time.time()),
                                           ":empty": "", ":max": 1})

        bind("MACHINE-A")   # first wins: activations 0 -> 1
        import pytest
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as ei:
            bind("MACHINE-B")   # second: activations now 1, 1 < 1 is False -> rejected
        assert ei.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def test_seeded_at_cap_rejected_through_handler(self, lf):
        """Baseline cap enforcement THROUGH the real handler: a key already bound
        to one machine AND at max_devices rejects a second machine. (This case is
        also caught by the pre-check at handle_activate; the revert-catcher for the
        atomic write itself is test_stale_read_still_rejected_by_conditional_write.)
        """
        put_license(lf, "ORION-ATCAP-KEY", machine_id="MACHINE-A",
                    activations=1, max_devices=1)
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-ATCAP-KEY", "machine_id": "MACHINE-B", **nt})
        assert s == 403
        assert b["error"] in ("device_mismatch", "device_limit_reached")
        item = lf.licenses_table().get_item(
            Key={"license_key": "ORION-ATCAP-KEY"}).get("Item", {})
        assert item["machine_id"] == "MACHINE-A"
        assert int(item["activations"]) == 1

    def test_stale_read_still_rejected_by_conditional_write(self, lf, monkeypatch):
        """NEW-2 TOCTOU revert-catcher THROUGH the real handler.

        Simulate a concurrent bind that advanced the row to cap AFTER this
        handler's pre-read: the DynamoDB GetItem the handler sees reports a STALE,
        below-cap activations (0), so the handler's pre-check (activations>=max)
        does NOT reject. The row is really AT cap (machine_id=MACHINE-A,
        activations=1, max=1). Only the handler's ATOMIC conditional ADD — which
        DynamoDB evaluates against the true current row — can catch this.

        Revert caught: replace the handler's `ConditionExpression` with a plain
        non-atomic `SET activations = :a+1` computed from the stale read. That
        revert would over-provision — read stale activations=0, write 1, overwrite
        machine_id to MACHINE-B, return 200. This test asserts 403 +
        device_limit_reached and that the real row is untouched, so the revert
        FAILS it.

        Residual limit (documented): moto/boto3 are single-threaded, so we cannot
        drive two truly-simultaneous writers. We instead inject the stale read the
        winning race would have left, which is exactly the state the atomic guard
        must survive; the guard's correctness under real concurrency is DynamoDB's
        contract, not something moto can exercise.
        """
        put_license(lf, "ORION-TOCTOU-KEY", machine_id="MACHINE-A",
                    activations=1, max_devices=1)
        real_tbl = lf.licenses_table()   # capture the REAL table BEFORE patching

        class StaleReadTable:
            """Wrap the real table; report a stale below-cap `activations` on
            GetItem for the target key. All writes (update_item) pass through to
            the real row unchanged."""
            def get_item(self, **kw):
                r = real_tbl.get_item(**kw)
                it = r.get("Item")
                if it and it.get("license_key") == "ORION-TOCTOU-KEY":
                    it = dict(it)
                    it["activations"] = 0          # STALE: pretend still below cap
                    r = dict(r)
                    r["Item"] = it
                return r

            def __getattr__(self, name):
                return getattr(real_tbl, name)

        monkeypatch.setattr(lf, "licenses_table", lambda: StaleReadTable())

        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-TOCTOU-KEY", "machine_id": "MACHINE-B", **nt})
        assert s == 403
        assert b["error"] == "device_limit_reached"

        # The real row must be UNTOUCHED — no over-provision, still bound to A.
        item = real_tbl.get_item(
            Key={"license_key": "ORION-TOCTOU-KEY"}).get("Item", {})
        assert item["machine_id"] == "MACHINE-A"
        assert int(item["activations"]) == 1
