import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_backend_contract", ROOT / "tools" / "admin" / "check_backend_contract.py"
)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def test_contract_checker_rejects_missing_staff_routes(monkeypatch):
    responses = {
        ("GET", "/api/version"): check.ProbeResult("GET", "/api/version", 200, '{"ok":true,"version":"0.4.0","update_signing":"ed25519"}'),
        ("GET", "/api/staff/whoami"): check.ProbeResult("GET", "/api/staff/whoami", 404, ""),
        ("POST", "/api/staff/login"): check.ProbeResult("POST", "/api/staff/login", 404, ""),
        ("GET", "/api/admin/staff"): check.ProbeResult("GET", "/api/admin/staff", 404, ""),
        ("POST", "/api/admin/tamper-report"): check.ProbeResult("POST", "/api/admin/tamper-report", 404, ""),
        ("POST", "/api/staff/tamper-report"): check.ProbeResult("POST", "/api/staff/tamper-report", 404, ""),
    }

    monkeypatch.setattr(check, "request", lambda _base, method, path, body=None: responses[(method, path)])

    assert check.main(["--base-url", "https://example.invalid"]) == 1


def test_contract_checker_accepts_fail_closed_staff_routes(monkeypatch):
    responses = {
        ("GET", "/api/version"): check.ProbeResult("GET", "/api/version", 200, '{"ok":true,"version":"0.4.0","update_signing":"ed25519"}'),
        ("GET", "/api/staff/whoami"): check.ProbeResult("GET", "/api/staff/whoami", 401, '{"ok":false,"error":"staff_auth_required"}'),
        ("POST", "/api/staff/login"): check.ProbeResult("POST", "/api/staff/login", 400, '{"ok":false,"error":"missing_fields"}'),
        ("GET", "/api/admin/staff"): check.ProbeResult("GET", "/api/admin/staff", 403, '{"ok":false,"error":"forbidden"}'),
        ("POST", "/api/admin/tamper-report"): check.ProbeResult("POST", "/api/admin/tamper-report", 403, '{"ok":false,"error":"forbidden"}'),
        ("POST", "/api/staff/tamper-report"): check.ProbeResult("POST", "/api/staff/tamper-report", 401, '{"ok":false,"error":"staff_auth_required"}'),
    }

    monkeypatch.setattr(check, "request", lambda _base, method, path, body=None: responses[(method, path)])

    assert check.main(["--base-url", "https://example.invalid"]) == 0
