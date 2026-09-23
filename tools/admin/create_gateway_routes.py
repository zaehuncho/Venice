#!/usr/bin/env python
"""Create explicit API Gateway routes needed by owner tools and pairing, idempotently.

WHY THIS EXISTS. `OrionOwner.exe`, `OrionStaff.exe` and `tools/admin/orion_admin.py` call routes the
Lambda handles correctly and that API Gateway `v348t5hg3i` never had: the gateway uses EXPLICIT
routes, so an unrouted path 404s before it reaches the function. Measured 2026-09-21 — a direct
`aws lambda invoke` of `/api/admin/whoami` returns 403 (handler present, auth rejected) while the
same path through `api.zaeorion.com` returns 404. Evidence and the full table:
`docs/OWNER_TOOLS_GATEWAY_BLOCKER_2026-09-21.md`.

SAFE BY DEFAULT: dry run. Pass `--apply` to create. It never creates `$default` (that would expose
every route in the Lambda's table, including `/api/shard/*`), never deletes or modifies an existing
route, and copies the integration target from a route that already works rather than guessing it.

    python tools/admin/create_gateway_routes.py            # dry run: what is missing
    python tools/admin/create_gateway_routes.py --apply    # create them, then verify over HTTPS

Needs `apigateway:GET` + `apigateway:POST` on this API. The `venice-automation` user does not have
them by default; the least-privilege policy is in the runbook above.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

API_ID = "v348t5hg3i"
PUBLIC_HOST = "https://api.zaeorion.com"

# Every (method, path) the owner/staff tools ACTUALLY call that the gateway lacked on 2026-09-21,
# plus the non-minting paid-checkout poll required by the 2026-09-23 A5 fixup.
# Verified against AdminToolController.cpp + orion_admin.py + website/src/worker.js, not the full Lambda table:
# the Lambda also handles /api/admin/{status,unkill,provision,search,staff/audit}, but no tool calls
# them, and an unused route is surface for nothing. whoami/search/staff-whoami already existed as
# GET - the first probe assumed POST for everything and mis-reported them as missing.
REQUIRED = [
    ("POST", "/api/bot/pair-status"),  # paid connection readiness; no pairing code minted
    ("GET", "/api/admin/metrics"),      # dashboard
    ("GET", "/api/admin/config"),       # config read: MOTD, version gate, kill switch state
    ("POST", "/api/admin/config"),      # config write: kill engage/disengage, TOTP, secret rotation
    ("GET", "/api/staff/audit"),        # the staff role's audit view
    ("GET", "/api/admin/audit"),        # the OWNER audit view. The console tree showed a GET /audit that
                                        # turned out to be /api/admin/staff/audit (nested); this one 404'd
                                        # on a GET probe. Do not trust the flattened tree text for nesting.
]
# A route that already works, used only to read the integration id off it.
TEMPLATE_ROUTE = "GET /api/admin/whoami"


def aws(*args: str) -> dict | list:
    proc = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().splitlines()
        raise SystemExit("aws " + " ".join(args[:2]) + " failed:\n  " + "\n  ".join(err[-3:]))
    return json.loads(proc.stdout or "null")


def probe(method: str, path: str) -> int:
    """Unauthenticated call with the method the tool uses: 404 = unrouted, anything else = routed."""
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(PUBLIC_HOST + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-id", default=API_ID)
    ap.add_argument("--apply", action="store_true", help="actually create the routes")
    args = ap.parse_args()

    routes = aws("apigatewayv2", "get-routes", "--api-id", args.api_id)["Items"]
    existing = {r["RouteKey"]: r for r in routes}
    print(f"api {args.api_id}: {len(existing)} routes exist")

    template = existing.get(TEMPLATE_ROUTE)
    if not template or not template.get("Target"):
        raise SystemExit(f"cannot read the integration: {TEMPLATE_ROUTE!r} is missing or has no target")
    target = template["Target"]          # e.g. "integrations/abc1234"
    print(f"integration copied from {TEMPLATE_ROUTE!r}: {target}")

    missing = [(m, p) for m, p in REQUIRED if f"{m} {p}" not in existing]
    for method, path in REQUIRED:
        state = "present" if (method, path) not in missing else "MISSING"
        print(f"  {state:8} {method} {path}")
    if not missing:
        print("nothing to do: every required route already exists")
        return 0
    if not args.apply:
        print(f"\ndry run — {len(missing)} route(s) would be created. Re-run with --apply.")
        return 0

    for method, path in missing:
        key = f"{method} {path}"
        assert not key.startswith("$"), "refusing to create a catch-all route"
        aws("apigatewayv2", "create-route", "--api-id", args.api_id,
            "--route-key", key, "--target", target)
        print(f"created {key}")

    print("\nverifying over HTTPS (404 = still unrouted):")
    bad = []
    for method, path in REQUIRED:
        code = probe(method, path)
        print(f"  {code:>3}  {method} {path}")
        if code == 404 or code == 0:
            bad.append(f"{method} {path}")
    if bad:
        print("\nSTILL UNROUTED: " + ", ".join(bad))
        return 1
    print("\nall required routes answer past the gateway; sign in with the owner tool to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
