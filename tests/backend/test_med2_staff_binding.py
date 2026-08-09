"""MED-2: staff machine-binding is enforced UNCONDITIONALLY. A stolen bearer
token used without the X-Machine-Id header (or a different one) is rejected."""
from conftest import invoke, put_staff, make_staff_nonce_ts


def _login(lf, staff_id="staff1", machine_id="MACHINE-S1"):
    put_staff(lf, staff_id, machine_id="")
    nt = make_staff_nonce_ts()
    s, b, _ = invoke(lf, "POST", "/api/staff/login", body={
        "staff_id": staff_id, "machine_id": machine_id, **nt})
    assert s == 200
    return b["token"]


class TestStaffBindingUnconditional:
    def test_missing_machine_header_rejected(self, lf):
        token = _login(lf)
        # No X-Machine-Id header at all -> must NOT skip binding.
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami",
            headers={"authorization": f"Bearer {token}"})
        assert status == 403
        assert body["error"] == "machine_mismatch"

    def test_wrong_machine_header_rejected(self, lf):
        token = _login(lf)
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami",
            headers={"authorization": f"Bearer {token}", "x-machine-id": "OTHER-MACHINE"})
        assert status == 403
        assert body["error"] == "machine_mismatch"

    def test_correct_machine_header_ok(self, lf):
        token = _login(lf)
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami",
            headers={"authorization": f"Bearer {token}", "x-machine-id": "MACHINE-S1"})
        assert status == 200
        assert body["ok"] is True
