"""Fail-closed identity binding for Orion's decoded-frame named pipe.

The frame exporter is the *server* and the Python reader opens the client end.
Consequently the only process identity API that describes the writer is
``GetNamedPipeServerProcessId``.  This module keeps the Win32 details out of the
capture loop and exposes small immutable records that are easy to regression
test without creating real pipes.

An identity failure is not a claim that decoded pixels are unusable.  Callers
may continue with an unscoped cold estimator, but must never restore or persist
route timing from an unverified producer.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def windows_process_creation_time_100ns(process_handle: Any) -> int:
    """Read the creation FILETIME from an already-held Windows process handle."""
    if os.name != "nt" or not process_handle:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = _FileTime()
        exit_time = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(int(process_handle)),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0
        return ((int(creation.dwHighDateTime) << 32)
                | int(creation.dwLowDateTime))
    except Exception:
        return 0


def canonical_executable_path(path: str) -> str:
    """Return a comparison-only canonical path; empty/invalid input stays empty."""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(text)))
    except (OSError, TypeError, ValueError):
        return ""


@dataclass(frozen=True)
class ExecutableSnapshot:
    path: str = ""
    size: int = -1
    sha256: str = ""
    reason: str = ""

    @property
    def valid(self) -> bool:
        return bool(
            self.path
            and self.size >= 0
            and _SHA256_RE.fullmatch(self.sha256)
            and not self.reason
        )


def stable_executable_snapshot(path: str) -> ExecutableSnapshot:
    """Hash one immutable file identity, rejecting replace/modify races.

    Metadata is checked before, during, and after the streaming hash.  Device and
    inode/file-index checks catch a same-size/same-mtime replacement on platforms
    that expose them; SHA-256 remains the content authority.
    """
    canonical = canonical_executable_path(path)
    if not canonical:
        return ExecutableSnapshot(reason="configured_path_missing")
    try:
        before = os.stat(canonical)
        digest = hashlib.sha256()
        with open(canonical, "rb") as binary:
            opened = os.fstat(binary.fileno())
            if (
                opened.st_size != before.st_size
                or opened.st_mtime_ns != before.st_mtime_ns
                or getattr(opened, "st_dev", None) != getattr(before, "st_dev", None)
                or getattr(opened, "st_ino", None) != getattr(before, "st_ino", None)
            ):
                return ExecutableSnapshot(reason="configured_image_changed")
            for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                digest.update(chunk)
            closed = os.fstat(binary.fileno())
        after = os.stat(canonical)
        identity = (
            int(opened.st_size),
            int(opened.st_mtime_ns),
            getattr(opened, "st_dev", None),
            getattr(opened, "st_ino", None),
        )
        if identity != (
            int(closed.st_size),
            int(closed.st_mtime_ns),
            getattr(closed, "st_dev", None),
            getattr(closed, "st_ino", None),
        ) or identity != (
            int(after.st_size),
            int(after.st_mtime_ns),
            getattr(after, "st_dev", None),
            getattr(after, "st_ino", None),
        ):
            return ExecutableSnapshot(reason="configured_image_changed")
        return ExecutableSnapshot(
            path=canonical,
            size=int(after.st_size),
            sha256=digest.hexdigest(),
        )
    except (OSError, TypeError, ValueError):
        return ExecutableSnapshot(reason="configured_image_unreadable")


@contextmanager
def locked_executable_snapshot(path: str):
    """Yield a stable identity snapshot for the launch image.

    Historically this also held a Windows deny-write/delete handle
    (``FILE_SHARE_READ`` only) across the hash and ``CreateProcess`` so the
    child PID was evidence of the exact bytes native hashed.  That file lock was
    removed (2026-08-08): in practice it was fragile and got in the way -- a
    benign concurrent reader/writer or an AV scan could fail the ``CreateFileW``
    and turn a healthy Remote Play launch into an "executable identity mismatch"
    with no real tamper involved.

    Identity is still verified: ``stable_executable_snapshot`` checks size +
    SHA-256 before spawn (native independently re-hashes the same image), and the
    named-pipe producer identity is re-verified against the *running* process at
    bind time (see ``WindowsNamedPipeServerIdentityApi``).  Tamper is therefore
    still DETECTED; it is simply no longer additionally guarded by a held lock.
    The context-manager shape is retained so callers are unchanged.
    """
    yield stable_executable_snapshot(path)


@dataclass(frozen=True)
class ProducerExpectation:
    pid: int = 0
    launch_generation: int = 0
    creation_time_100ns: int = 0
    path: str = ""
    size: int = -1
    sha256: str = ""
    # ``subprocess.Popen`` owns this exact process handle for the life of the
    # launch manager.  Keeping it here makes the anti-PID-reuse lease explicit.
    process_handle: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ProducerObservation:
    pid: int = 0
    creation_time_100ns: int = 0
    path: str = ""
    size: int = -1
    sha256: str = ""
    # A held process handle pins the process object, so a recycled numeric PID
    # cannot silently become the bound producer.  It is intentionally excluded
    # from equality/repr and closed by the identity API.
    process_handle: Any = field(default=None, repr=False, compare=False)


def expectation_from_owned_process(
    owned: Optional[Mapping[str, Any]],
    configured_path: str,
    configured_size: int,
    configured_sha256: str,
) -> Tuple[ProducerExpectation, str]:
    """Join the native image expectation to the exact Python-owned child."""
    owned = owned if isinstance(owned, Mapping) else {}
    try:
        pid = int(owned.get("pid", 0) or 0)
        generation = int(owned.get("launch_generation", 0) or 0)
        creation_time = int(owned.get("creation_time_100ns", 0) or 0)
        expected_size = int(configured_size)
    except (TypeError, ValueError, OverflowError):
        return ProducerExpectation(), "expected_process_invalid"
    expected_path = canonical_executable_path(configured_path)
    launched_path = canonical_executable_path(str(owned.get("path", "") or ""))
    expected_sha = str(configured_sha256 or "").strip().lower()
    if pid <= 0 or generation <= 0:
        return ProducerExpectation(), "expected_process_missing"
    process_handle = owned.get("process_handle")
    if not process_handle:
        return ProducerExpectation(), "expected_process_handle_missing"
    if creation_time <= 0:
        return ProducerExpectation(), "expected_creation_time_missing"
    if not expected_path or not launched_path:
        return ProducerExpectation(), "expected_path_missing"
    if expected_path != launched_path:
        return ProducerExpectation(), "launched_path_mismatch"
    if expected_size < 0 or not _SHA256_RE.fullmatch(expected_sha):
        return ProducerExpectation(), "expected_hash_missing"
    return ProducerExpectation(
        pid=pid,
        launch_generation=generation,
        creation_time_100ns=creation_time,
        path=expected_path,
        size=expected_size,
        sha256=expected_sha,
        process_handle=process_handle,
    ), ""


def producer_identity_reason(
    expected: ProducerExpectation, observed: ProducerObservation
) -> str:
    """Return the first exact producer mismatch, or ``""`` when verified."""
    if expected.pid <= 0 or expected.launch_generation <= 0:
        return "expected_process_missing"
    if observed.pid <= 0:
        return "server_pid_missing"
    if observed.pid != expected.pid:
        return "server_pid_mismatch"
    if observed.creation_time_100ns <= 0:
        return "server_creation_time_missing"
    if observed.creation_time_100ns != expected.creation_time_100ns:
        return "server_creation_time_mismatch"
    if canonical_executable_path(observed.path) != expected.path:
        return "server_path_mismatch"
    if int(observed.size) != int(expected.size):
        return "server_size_mismatch"
    if str(observed.sha256 or "").strip().lower() != expected.sha256:
        return "server_hash_mismatch"
    return ""


def bound_expectation_reason(
    bound: ProducerExpectation, current: ProducerExpectation
) -> str:
    """Fence a pipe when its owned launch generation changes underneath it."""
    if current.pid != bound.pid:
        return "owned_pid_changed"
    if current.launch_generation != bound.launch_generation:
        return "owned_launch_generation_changed"
    if current.creation_time_100ns != bound.creation_time_100ns:
        return "owned_creation_time_changed"
    if current.process_handle != bound.process_handle:
        return "owned_process_handle_changed"
    if current.path != bound.path:
        return "owned_path_changed"
    if current.size != bound.size or current.sha256 != bound.sha256:
        return "owned_image_changed"
    return ""


class WindowsNamedPipeServerIdentityApi:
    """Win32 reader-side producer inspection with a process-object lease."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF

    def __init__(self) -> None:
        self._kernel32 = None
        self._wintypes = None
        self._filetime_type = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _FileTime(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # Windows 7+; getattr is deliberately deferred into inspect so a
            # stripped/unsupported API is a testable fail-closed state.
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32 = kernel32
            self._wintypes = wintypes
            self._filetime_type = _FileTime
        except Exception:
            self._kernel32 = None

    @staticmethod
    def _filetime_value(value: Any) -> int:
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    def inspect(self, pipe_handle: Any) -> Tuple[Optional[ProducerObservation], str]:
        if self._kernel32 is None or self._wintypes is None or self._filetime_type is None:
            return None, "server_identity_api_missing"
        try:
            import ctypes

            get_server_pid = getattr(self._kernel32, "GetNamedPipeServerProcessId", None)
            if get_server_pid is None:
                return None, "server_pid_api_missing"
            get_server_pid.argtypes = [
                self._wintypes.HANDLE,
                ctypes.POINTER(self._wintypes.ULONG),
            ]
            get_server_pid.restype = self._wintypes.BOOL
            pid = self._wintypes.ULONG()
            if not get_server_pid(self._wintypes.HANDLE(int(pipe_handle)), ctypes.byref(pid)):
                return None, "server_pid_query_failed"
            if int(pid.value) <= 0:
                return None, "server_pid_missing"

            process_handle = self._kernel32.OpenProcess(
                self._PROCESS_QUERY_LIMITED_INFORMATION | self._SYNCHRONIZE,
                False,
                int(pid.value),
            )
            if not process_handle:
                return None, "server_process_open_failed"
            keep_handle = False
            try:
                path_buffer = ctypes.create_unicode_buffer(32768)
                path_size = self._wintypes.DWORD(len(path_buffer))
                if not self._kernel32.QueryFullProcessImageNameW(
                    process_handle, 0, path_buffer, ctypes.byref(path_size)
                ):
                    return None, "server_path_query_failed"
                creation = self._filetime_type()
                exit_time = self._filetime_type()
                kernel = self._filetime_type()
                user = self._filetime_type()
                if not self._kernel32.GetProcessTimes(
                    process_handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None, "server_creation_time_query_failed"
                snapshot = stable_executable_snapshot(path_buffer.value)
                if not snapshot.valid:
                    return None, snapshot.reason or "server_image_unreadable"
                keep_handle = True
                return ProducerObservation(
                    pid=int(pid.value),
                    creation_time_100ns=self._filetime_value(creation),
                    path=snapshot.path,
                    size=snapshot.size,
                    sha256=snapshot.sha256,
                    process_handle=process_handle,
                ), ""
            finally:
                if not keep_handle:
                    self._kernel32.CloseHandle(process_handle)
        except Exception:
            return None, "server_identity_query_failed"

    def lease_reason(self, observed: ProducerObservation) -> str:
        """Prove the held process object is alive and still the same creation."""
        if self._kernel32 is None or not observed.process_handle:
            return "server_process_lease_missing"
        try:
            import ctypes

            wait = int(self._kernel32.WaitForSingleObject(observed.process_handle, 0))
            if wait == self._WAIT_OBJECT_0:
                return "server_process_exited"
            if wait == self._WAIT_FAILED:
                return "server_process_wait_failed"
            if wait != self._WAIT_TIMEOUT:
                return "server_process_wait_invalid"
            creation = self._filetime_type()
            exit_time = self._filetime_type()
            kernel = self._filetime_type()
            user = self._filetime_type()
            if not self._kernel32.GetProcessTimes(
                observed.process_handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return "server_creation_time_query_failed"
            if self._filetime_value(creation) != observed.creation_time_100ns:
                return "server_process_reused"
            return ""
        except Exception:
            return "server_process_lease_failed"

    def close(self, observed: Optional[ProducerObservation]) -> None:
        handle = getattr(observed, "process_handle", None) if observed is not None else None
        if self._kernel32 is not None and handle:
            try:
                self._kernel32.CloseHandle(handle)
            except Exception:
                pass
