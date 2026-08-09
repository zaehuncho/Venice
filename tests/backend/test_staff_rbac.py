from conftest import invoke, put_staff, make_staff_nonce_ts, put_license, TEST_ENROLL_KEY


class TestStaffEnroll:
    def test_enroll_happy_path(self, lf):
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": TEST_ENROLL_KEY, "staff_id": "staff1",
            "display_name": "Staff One", "machine_id": "MACHINE-S1", **nt})
        assert status == 200
        assert body["ok"] is True
        assert "token" in body
        assert body["role"] == "staff"

    def test_enroll_wrong_key(self, lf):
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": "wrong-key", "staff_id": "staff1",
            "machine_id": "MACHINE-S1", **nt})
        assert status == 403
        assert body["error"] == "invalid_enroll_key"

    def test_enroll_staff_id_taken(self, lf):
        put_staff(lf, "staff1")
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": TEST_ENROLL_KEY, "staff_id": "staff1",
            "machine_id": "MACHINE-S1", **nt})
        assert status == 409
        assert body["error"] == "staff_id_taken"

    def test_enroll_missing_fields(self, lf):
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": "", "staff_id": "", "machine_id": "", **nt})
        assert status == 400
        assert body["error"] == "missing_fields"

    def test_enroll_nonce_replay(self, lf):
        nt = make_staff_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": TEST_ENROLL_KEY, "staff_id": "staffA",
            "machine_id": "MACHINE-S1", **nt})
        assert s1 == 200
        s2, b2, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": TEST_ENROLL_KEY, "staff_id": "staffB",
            "machine_id": "MACHINE-S2", **nt})
        assert s2 == 401
        assert b2["error"] == "replay_detected"


class TestStaffLogin:
    def test_login_happy_path(self, lf):
        put_staff(lf, "staff1", machine_id="")
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "staff1", "machine_id": "MACHINE-S1", **nt})
        assert status == 200
        assert body["ok"] is True
        assert "token" in body
        assert body["role"] == "staff"

    def test_login_machine_mismatch(self, lf):
        put_staff(lf, "staff1", machine_id="MACHINE-S1")
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "staff1", "machine_id": "MACHINE-S2", **nt})
        assert status == 403
        assert body["error"] == "machine_mismatch"

    def test_login_disabled(self, lf):
        put_staff(lf, "staff1", machine_id="", disabled=True)
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "staff1", "machine_id": "MACHINE-S1", **nt})
        assert status == 403
        assert body["error"] == "staff_disabled"

    def test_login_unknown_staff(self, lf):
        nt = make_staff_nonce_ts()
        status, body, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "ghost", "machine_id": "MACHINE-S1", **nt})
        assert status == 403
        assert body["error"] == "invalid_credentials"


class TestStaffWhoami:
    def test_whoami_happy_path(self, lf):
        put_staff(lf, "staff1", machine_id="")
        nt = make_staff_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "staff1", "machine_id": "MACHINE-S1", **nt})
        token = b1["token"]
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami",
            headers={"authorization": f"Bearer {token}", "x-machine-id": "MACHINE-S1"})
        assert status == 200
        assert body["ok"] is True
        assert body["staff_id"] == "staff1"

    def test_whoami_missing_token(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami")
        assert status == 401
        assert body["error"] == "missing_token"

    def test_whoami_invalid_token(self, lf):
        status, body, _ = invoke(lf, "GET", "/api/staff/whoami",
            headers={"authorization": "Bearer not-a-real-token"})
        assert status == 403
        assert body["error"] == "invalid_token"


class TestStaffLicense:
    def _login(self, lf, staff_id="staff1", machine_id="MACHINE-S1"):
        put_staff(lf, staff_id, machine_id="")
        nt = make_staff_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": staff_id, "machine_id": machine_id, **nt})
        assert s1 == 200
        return b1["token"]

    def test_license_lookup(self, lf):
        put_license(lf, "ORION-LOOKUP-BBBB-CCCC", machine_id="MACHINE-1")
        token = self._login(lf)
        status, body, _ = invoke(lf, "POST", "/api/staff/license",
            body={"action": "lookup", "license_key": "ORION-LOOKUP-BBBB-CCCC"},
            headers={"authorization": f"Bearer {token}", "x-machine-id": "MACHINE-S1"})
        assert status == 200
        assert body["ok"] is True
        assert body["license"]["license_key_suffix"] == "CCCC"

    def test_license_invalid_key(self, lf):
        token = self._login(lf)
        status, body, _ = invoke(lf, "POST", "/api/staff/license",
            body={"action": "lookup", "license_key": "ORION-NOPE-NOPE-NOPE"},
            headers={"authorization": f"Bearer {token}", "x-machine-id": "MACHINE-S1"})
        assert status == 404
        assert body["error"] == "invalid_key"


class TestStaffTamperReport:
    def test_tamper_report(self, lf):
        put_staff(lf, "staff1", machine_id="")
        nt = make_staff_nonce_ts()
        s1, b1, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": "staff1", "machine_id": "MACHINE-S1", **nt})
        token = b1["token"]
        status, body, _ = invoke(lf, "POST", "/api/staff/tamper-report",
            body={"type": "debugger_detected", "machine_id": "MACHINE-S1", "details": {"pid": 1234}},
            headers={"authorization": f"Bearer {token}", "x-machine-id": "MACHINE-S1"})
        assert status == 200
        assert body["ok"] is True
        assert body["received"] is True
