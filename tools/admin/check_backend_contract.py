#!/usr/bin/env python3
"""Validate the deployed Orion backend route contract.

This is intentionally a non-secret smoke check. It never needs the owner/admin
secret and never prints tokens. The goal is to catch a partial/stale backend
deploy where the public version route works but the staff/admin route contract is
missing from API Gateway or Lambda.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://api.zaeorion.com"


class ProbeResult:
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = body

    @property
    def json(self) -> dict:
        try:
            data = json.loads(self.body or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


def request(base_url: str, method: str, path: str, body: dict | None = None) -> ProbeResult:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return ProbeResult(method, path, resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return ProbeResult(method, path, exc.code, exc.read().decode("utf-8", "replace"))


def fail(message: str) -> None:
    print(f"FAIL: {message}")


def ok(message: str) -> None:
    print(f"OK: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check deployed Orion backend routes without secrets.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API base URL (default {DEFAULT_BASE_URL})")
    args = parser.parse_args(argv)

    failures: list[str] = []
    version = request(args.base_url, "GET", "/api/version")
    if version.status != 200 or not version.json.get("ok"):
        failures.append(f"/api/version expected 200 ok=true, got {version.status}: {version.body[:160]}")
    else:
        data = version.json
        ok(f"/api/version reachable version={data.get('version', '(unknown)')} service={data.get('service', data.get('api', '(unknown)'))}")
        if data.get("update_signing") != "ed25519":
            failures.append("/api/version does not advertise update_signing=ed25519; verify this is not a stale backend.")

    # These probes prove route existence without needing a valid secret or token.
    # A deployed route should fail with auth/missing-field JSON, not API Gateway 404.
    checks = [
        (
            request(args.base_url, "GET", "/api/staff/whoami"),
            {401, 403},
            {"missing_token", "staff_auth_required", "bad_staff_token", "staff_token_expired", "staff_disabled"},
            "staff identity route",
        ),
        (
            request(args.base_url, "POST", "/api/staff/login", {}),
            {400},
            {"missing_fields"},
            "staff login route",
        ),
        (
            request(args.base_url, "GET", "/api/admin/staff"),
            {403, 500},
            {"forbidden", "config_error"},
            "owner staff-management route",
        ),
        (
            request(args.base_url, "POST", "/api/admin/tamper-report", {}),
            {403, 500},
            {"forbidden", "config_error"},
            "owner tamper-report route",
        ),
        (
            request(args.base_url, "POST", "/api/staff/tamper-report", {}),
            {401, 403},
            {"missing_token", "staff_auth_required", "bad_staff_token", "staff_token_expired", "staff_disabled"},
            "staff tamper-report route",
        ),
    ]

    for result, allowed_statuses, allowed_errors, label in checks:
        error = result.json.get("error")
        if result.status == 404:
            failures.append(f"{label} missing: {result.method} {result.path} returned 404.")
            continue
        if result.status not in allowed_statuses or (allowed_errors and error not in allowed_errors):
            failures.append(
                f"{label} returned unexpected status/error: {result.status} {error or '(no-json-error)'} body={result.body[:160]}"
            )
            continue
        ok(f"{label} exists and fails closed: {result.status} {error}")

    if failures:
        for message in failures:
            fail(message)
        return 1
    ok("backend owner/staff route contract is deployed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
