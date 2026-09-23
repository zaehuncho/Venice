"""The suite guard must fail caught egress without rejecting local socketpair use."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_caught_off_host_socket_still_fails_nested_pytest(tmp_path):
    case = tmp_path / "test_guard_fixture.py"
    case.write_text(
        """import socket
import sys

def test_caught_transport_error():
    try:
        socket.create_connection(("203.0.113.1", 9), timeout=0.01)
    except RuntimeError:
        pass

def test_caught_dns_lookup_error():
    # Synthetic audit event: exercise the DNS boundary without any actual query.
    try:
        sys.audit("socket.getaddrinfo", "unit.invalid", 443, 0, 0, 0)
    except RuntimeError:
        pass

def test_windows_asyncio_style_loopback():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1) as client:
            peer, _ = server.accept()
            peer.close()
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pytest.ini"), str(case),
         "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "nested-basetemp")],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "2 failed, 1 passed" in output, output
    assert "A7_OFFLINE_GUARD_BLOCKS 2" in output, output
    assert "offline test attempted 1 non-loopback" in output, output
