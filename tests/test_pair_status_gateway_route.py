"""The paid connection poll must be explicitly routed through API Gateway."""

import runpy
from pathlib import Path


def test_non_minting_pair_status_has_explicit_gateway_route():
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / "tools" / "admin" / "create_gateway_routes.py"))
    assert ("POST", "/api/bot/pair-status") in namespace["REQUIRED"]
    assert all(not path.startswith("$") for _, path in namespace["REQUIRED"])
