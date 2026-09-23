"""Fail the offline Python suite on attempted non-loopback network egress.

An audit hook is process-wide, so an application that catches the socket error
cannot turn an unmocked backend or console call into a passing test.  Local
socketpair/asyncio traffic is allowed (including Windows IPv4/IPv6 loopback).
"""

from __future__ import annotations

import ipaddress
import sys

import pytest


_blocked_egress: list[tuple[str, str, str]] = []
_active_node = "collection"
_reported_count = 0


def _is_loopback_address(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        # AF_UNIX/pipe addresses have no routable host.
        return True
    return _is_loopback_host(address[0])


def _is_loopback_host(value: object) -> bool:
    if value is None or value == "":
        # getaddrinfo(None, ...) denotes local bind/service discovery.
        return True
    host = (value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)).split("%", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _offline_socket_audit(event: str, args: tuple[object, ...]) -> None:
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                 "socket.gethostbyaddr", "socket.getnameinfo"}:
        value = args[0] if args else None
        host = value[0] if isinstance(value, tuple) and value else value
        if not _is_loopback_host(host):
            _blocked_egress.append((_active_node, event, str(host)))
            raise RuntimeError("offline test guard blocked non-loopback network egress")
        return
    if event not in {"socket.connect", "socket.sendto"}:
        return
    address = args[1] if len(args) > 1 else None
    if _is_loopback_address(address):
        return
    host = str(address[0]) if isinstance(address, tuple) and address else str(address)
    _blocked_egress.append((_active_node, event, host))
    raise RuntimeError("offline test guard blocked non-loopback network egress")


sys.addaudithook(_offline_socket_audit)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global _active_node
    _active_node = item.nodeid


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    global _reported_count
    outcome = yield
    report = outcome.get_result()
    attempts = _blocked_egress[_reported_count:]
    if attempts:
        _reported_count = len(_blocked_egress)
        detail = ", ".join(f"{event}({host})" for _, event, host in attempts)
        report.outcome = "failed"
        report.longrepr = f"offline test attempted {len(attempts)} non-loopback operation(s): {detail}"


def pytest_sessionfinish(session, exitstatus):
    if len(_blocked_egress) != _reported_count:
        session.exitstatus = 1
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line(f"A7_OFFLINE_GUARD_BLOCKS {len(_blocked_egress)}")
