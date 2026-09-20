from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from decoder_pipe_identity import (
    canonical_executable_path,
    locked_executable_snapshot,
    windows_process_creation_time_100ns,
)

logger = logging.getLogger("RemotePlayClient")


REMOTE_PLAY_TITLES = (
    "Chiaki",
    "chiaki-ng",
)

_SAFE_TITLE_MARKERS = (
    "chiaki",
    "orion stream",   # the renamed OrionStream window; must beat the "orion" reject
)

_REJECT_TITLE_MARKERS = (
    "orion",
    "visual studio code",
    "powershell",
    "command prompt",
    "windows terminal",
    "google chrome",
    "microsoft edge",
    "mozilla firefox",
    "desktop",
)


@dataclass
class RemotePlayClientConfig:
    platform: str = "ps5"
    client_mode: str = "chiaki"
    console_ip: str = ""
    window_title: str = ""
    chiaki_path: str = ""
    chiaki_identity_size: int = -1
    chiaki_identity_sha256: str = ""
    wait_timeout_s: float = 18.0
    close_on_stop: bool = True
    # A window or a connected local named pipe proves only that the Chiaki
    # process is alive.  Orion's input route is authoritative only after the
    # current child has completed the PS5 stream handshake.
    require_session_ready: bool = False
    # Test/portable override.  Production uses Chiaki's ordinary APPDATA log.
    session_log_dir: str = ""
    # Capture-card mode uses OrionStream only for the console input session.
    # Disable its redundant video processing while preserving audio.  This is
    # deliberately explicit instead of inferred from the parent environment so
    # a later no-card decoder launch can never inherit input-only behaviour.
    disable_video: bool = False
    # Pre-launch rest-mode wake + post-failure console probes. Mid-session
    # input-link RECOVERY sets this False: the console was awake moments ago,
    # every probe second there comes out of a 17s/20s watchdog budget sized
    # before these probes existed, and auto-waking a console the user just
    # rested would fight an explicit user action.
    console_wake_allowed: bool = True
    # Called with the port-wait budget (s) right after a rest-mode wakeup is sent, so the
    # host can extend its own promote deadline. None = no extension possible (keep the
    # deadline-sized budget).
    on_console_waking: Optional[Callable[[float], None]] = None


@dataclass
class RemotePlayClientStatus:
    ok: bool = False
    mode: str = "none"
    path: str = ""
    pid: int = 0
    hwnd: int = 0
    window_title: str = ""
    message: str = ""
    session_ready: bool = False
    session_log_path: str = ""


_CHIAKI_SESSION_READY_MARKER = b"StreamConnection successfully received streaminfo"
_CHIAKI_SESSION_END_MARKERS = (
    b"StreamConnection is disconnecting",
    b"Session has quit",
)
_CHIAKI_SESSION_LOG_READ_CHUNK = 64 * 1024
_CHIAKI_SESSION_MARKER_OVERLAP = max(
    len(_CHIAKI_SESSION_READY_MARKER),
    *(len(marker) for marker in _CHIAKI_SESSION_END_MARKERS),
) - 1


def _chiaki_session_log_dir(override: str = "") -> str:
    if str(override or "").strip():
        return os.path.abspath(str(override).strip())
    appdata = str(os.environ.get("APPDATA", "") or "").strip()
    if not appdata:
        return ""
    return os.path.join(appdata, "Chiaki", "Chiaki", "log")


def _session_log_files(log_dir: str) -> List[str]:
    if not log_dir or not os.path.isdir(log_dir):
        return []
    try:
        paths = [
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.startswith("chiaki_session_") and name.lower().endswith(".log")
        ]
        # The timestamp is part of the filename; mtime is the primary key so a
        # restarted child always supersedes an older once-ready session.
        paths.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
        return paths
    except OSError:
        return []


def _snapshot_session_logs(log_dir: str) -> Dict[str, int]:
    """Byte offsets that scope readiness to work performed after this launch."""
    snapshot: Dict[str, int] = {}
    for path in _session_log_files(log_dir):
        try:
            snapshot[os.path.normcase(os.path.abspath(path))] = max(0, os.path.getsize(path))
        except OSError:
            continue
    return snapshot


class _SessionReadinessTracker:
    """Incremental readiness parser for exactly one launch generation.

    The initial streaminfo marker is latched, so a long healthy log cannot age it
    out of a tail window. Only appended bytes are read thereafter. A newer log
    file means a newer child/session and immediately resets the latch; an end
    marker revokes it until that same current log later reports streaminfo again.
    """

    def __init__(self, log_dir: str, baseline: Optional[Dict[str, int]] = None) -> None:
        self.log_dir = str(log_dir or "")
        self._lock = threading.RLock()
        self.reset(baseline or {})

    def reset(self, baseline: Dict[str, int]) -> None:
        with self._lock:
            self._reset_locked(baseline)

    def _reset_locked(self, baseline: Dict[str, int]) -> None:
        self.baseline = dict(baseline or {})
        self.path = ""
        self.offset = 0
        self.partial = b""
        self.ready = False
        self.saw_end = False

    def _select_current_log(self) -> str:
        for path in _session_log_files(self.log_dir):
            key = os.path.normcase(os.path.abspath(path))
            try:
                if os.path.getsize(path) > max(0, int(self.baseline.get(key, 0) or 0)):
                    return path
            except OSError:
                continue
        return ""

    def _consume(self, data: bytes) -> None:
        if not data:
            return
        joined = self.partial + data
        ready_at = joined.rfind(_CHIAKI_SESSION_READY_MARKER)
        ended_at = max(
            (joined.rfind(marker) for marker in _CHIAKI_SESSION_END_MARKERS),
            default=-1,
        )
        if ready_at >= 0 and ready_at > ended_at:
            self.ready = True
            self.saw_end = False
        elif ended_at >= 0 and ended_at > ready_at:
            self.ready = False
            self.saw_end = True
        # Retain only enough bytes to recognize a marker split across reads.
        # This buffer is bounded independently of total session-log size.
        self.partial = joined[-_CHIAKI_SESSION_MARKER_OVERLAP:]

    def poll(self) -> Tuple[str, str]:
        # Telemetry and launch/recovery may ask concurrently. The path,
        # offset, partial marker and ready latch form one parser transaction.
        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> Tuple[str, str]:
        candidate = self._select_current_log()
        if not candidate:
            # No current launch-scoped bytes means there is no authority proof.
            self.ready = False
            return "waiting", ""

        candidate_key = os.path.normcase(os.path.abspath(candidate))
        current_key = os.path.normcase(os.path.abspath(self.path)) if self.path else ""
        if candidate_key != current_key:
            self.path = candidate
            self.offset = max(0, int(self.baseline.get(candidate_key, 0) or 0))
            self.partial = b""
            self.ready = False
            self.saw_end = False

        try:
            size = os.path.getsize(self.path)
            if size < self.offset:
                # Truncation/replacement invalidates the prior latch.
                self.offset = max(0, int(self.baseline.get(candidate_key, 0) or 0))
                self.partial = b""
                self.ready = False
                self.saw_end = False
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                while True:
                    chunk = handle.read(_CHIAKI_SESSION_LOG_READ_CHUNK)
                    if not chunk:
                        break
                    self.offset += len(chunk)
                    self._consume(chunk)
        except OSError:
            self.ready = False
            return "waiting", self.path

        if self.ready:
            return "ready", self.path
        if self.saw_end:
            return "ended", self.path
        return "waiting", self.path


def _fresh_session_log_state(log_dir: str, baseline: Dict[str, int]) -> Tuple[str, str]:
    """One-shot helper retained for focused policy tests."""
    return _SessionReadinessTracker(log_dir, baseline).poll()


def _root_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dirs() -> List[str]:
    dirs = []
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else ""
    mei = str(getattr(sys, "_MEIPASS", "") or "")
    for item in (exe_dir, mei):
        if item and item not in dirs:
            dirs.append(item)
    return dirs


def _program_dirs() -> List[str]:
    vals = [
        *_bundle_dirs(),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
        os.path.join(os.path.expanduser("~"), "Downloads"),
        os.path.join(os.path.expanduser("~"), "Downloads", "Chiaki"),
        os.path.join(os.path.expanduser("~"), "Downloads", "Chiaki", "Chiaki"),
        _root_dir(),
        os.path.join(_root_dir(), "vendor"),
        os.path.join(_root_dir(), "vendor", "chiaki"),
        os.path.join(_root_dir(), "tools"),
    ]
    return [v for v in vals if v]


def _is_probably_remote_play_title(title: str, preferred_title: str = "") -> bool:
    lower = str(title or "").strip().lower()
    if not lower:
        return False
    preferred = str(preferred_title or "").strip().lower()
    # Safe markers beat the generic rejects ("orion stream" vs the "orion" reject
    # that keeps the launcher's own windows out).
    if any(marker in lower for marker in _SAFE_TITLE_MARKERS):
        return True
    if preferred and preferred in lower and not any(marker in lower for marker in _REJECT_TITLE_MARKERS):
        return True
    if any(marker in lower for marker in _REJECT_TITLE_MARKERS):
        return False
    return False


def _window_process_name(hwnd: int) -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _is_chiaki_process_window(hwnd: int, title: str) -> bool:
    lower = str(title or "").lower()
    if "__wgldummy" in lower or "nvogldc" in lower or "default ime" in lower:
        return False
    return _window_process_name(hwnd) in {"orionstream.exe", "chiaki.exe", "chiaki-ng.exe", "chiaki4deck.exe"}


def find_remote_play_window(preferred_title: str = "") -> Tuple[int, str]:
    if os.name != "nt":
        return 0, ""
    try:
        import win32gui
    except Exception:
        return 0, ""

    matches = []

    def consider(hwnd):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            # Area pre-filter (cheap) before the process check (which opens the
            # process). The stream window is large; this skips tiny child controls
            # so enumerating children stays inexpensive.
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(1, right - left) * max(1, bottom - top)
            except Exception:
                area = 1
            if area < 200 * 150:
                return
            title = win32gui.GetWindowText(hwnd)
            is_chiaki_proc = _is_chiaki_process_window(hwnd, title)
            if not _is_probably_remote_play_title(title, preferred_title) and not is_chiaki_proc:
                return
            lower = title.lower()
            preferred = str(preferred_title or "").lower()
            # Prefer the actual Chiaki process window with the largest visible area.
            # The launcher/setup window often has the exact title "Chiaki", while
            # the stream window may only be identifiable by process. Picking by
            # exact title first can leave capture attached to the wrong window.
            score = 60
            if is_chiaki_proc:
                score = 0
            elif preferred and preferred in lower:
                score = 8 if lower == preferred else 12
            elif any(marker in lower for marker in _SAFE_TITLE_MARKERS):
                score = 20
            matches.append((score, -area, hwnd, title))
        except Exception:
            pass

    def enum_handler(hwnd, _ctx):
        consider(hwnd)
        # Also search CHILD windows. Once Orion embeds the Chiaki stream window it
        # is reparented to be a CHILD of the Orion window, so a top-level-only scan
        # (EnumWindows) misses it -> "no stream window appeared" -> no CV. The
        # stream window is still identifiable by its chiaki.exe process even as a
        # child, and capture (GetWindowRect + PrintWindow) works on a child HWND.
        try:
            win32gui.EnumChildWindows(hwnd, lambda ch, _c: (consider(ch), True)[1], None)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(enum_handler, None)
    except Exception as exc:
        logger.debug("Window enumeration failed: %s", exc)
        return 0, ""

    if not matches:
        return 0, ""
    matches.sort(key=lambda item: (item[0], item[1]))
    return int(matches[0][2]), str(matches[0][3])


def _first_existing(paths: List[str]) -> str:
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return ""


def find_chiaki_binary(explicit: str = "") -> str:
    # When the pre-encryption INPUT HOOK (ORION_INPUT_HOOK) or the decoded-FRAME export
    # (ORION_FRAME_PIPE) is enabled, ALWAYS prefer the patched chiaki-ng build that ships the Orion
    # named-pipe bridges -- regardless of settings chiaki_path or any installed chiaki -- since only
    # that build understands CHIAKI_ORION_INPUT_PIPE / CHIAKI_ORION_FRAME_PIPE. Falls through to the
    # normal resolution if the patched build isn't present.
    if os.environ.get("ORION_INPUT_HOOK") or os.environ.get("ORION_FRAME_PIPE"):
        patched_dir = os.path.join(
            _root_dir(), "native_orion", "deploy", "chiaki-ng-orion", "chiaki-ng-Win"
        )
        for exe in ("OrionStream.exe", "chiaki.exe"):
            patched = os.path.join(patched_dir, exe)
            if os.path.isfile(patched):
                return patched
    if explicit and os.path.isfile(explicit):
        return explicit
    names = ("OrionStream.exe", "chiaki-ng.exe", "chiaki.exe", "chiaki4deck.exe")
    paths = []
    for base in _program_dirs():
        paths.extend(os.path.join(base, name) for name in names)
        paths.extend(os.path.join(base, "Chiaki", name) for name in names)
        paths.extend(os.path.join(base, "chiaki-ng", name) for name in names)
    found = _first_existing(paths)
    if found:
        return found
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return ""


_CHIAKI_IMAGE_NAMES = ("OrionStream.exe", "chiaki.exe", "chiaki-ng.exe", "chiaki4deck.exe")


def _running_image_pids(targets: Tuple[str, ...]) -> Optional[List[Tuple[int, str]]]:
    """(pid, lower-cased name) for each running ``targets`` process, via ONE
    in-process toolhelp snapshot (~1ms).

    Returns a (possibly empty) list, or None when the snapshot API is
    unavailable/failed so callers can fall back to their conservative path.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        ULONG_PTR = ctypes.c_size_t

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        # ctypes defaults BOTH the return value and every undeclared argument to
        # a 32-bit C int. HANDLE is pointer-sized on the shipped x64 runtime, so
        # an undeclared Process32FirstW/Process32NextW could truncate the
        # snapshot handle and turn the probe into an exception (-> None -> the
        # spawning fallback sweep). Measured on this rig the handles are small
        # enough that it never fired, but the declaration is free and removes the
        # failure mode from the only thing standing between a connect and 3s of
        # taskkill.exe.
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            wanted = {name.lower() for name in targets}
            found: List[Tuple[int, str]] = []
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = str(entry.szExeFile or "").lower()
                if name in wanted:
                    found.append((int(entry.th32ProcessID), name))
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
            return found
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None


def _running_image_names(targets: Tuple[str, ...]) -> Optional[set]:
    """Which of ``targets`` are running, via ONE in-process toolhelp snapshot (~1ms).

    Returns a (possibly empty) lower-cased name set, or None when the snapshot
    API is unavailable/failed so the caller can fall back to the spawning sweep
    (the fixed-named-pipe-freeing guarantee must never rest on a failed probe).
    """
    pids = _running_image_pids(targets)
    if pids is None:
        return None
    return {name for _pid, name in pids}


def _terminate_pids_in_process(pids: List[Tuple[int, str]],
                               wait_s: float = 1.5) -> Tuple[List[int], List[int]]:
    """TerminateProcess every ``(pid, name)`` in-process. Returns (killed, failed).

    [ORION_CONNECT_LATENCY 2026-09-14] The spawning sweep costs ~250ms per
    taskkill.exe; OpenProcess + TerminateProcess + a bounded WaitForSingleObject
    costs microseconds and, unlike `taskkill /IM`, can never touch a process the
    caller did not name. NEVER kills this process or a system pid; the caller is
    responsible for only ever handing it target-image pids.
    """
    killed: List[int] = []
    failed: List[int] = []
    if os.name != "nt" or not pids:
        return killed, failed
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x00000000

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception:
        return killed, [int(pid) for pid, _name in pids]

    self_pid = os.getpid()
    budget_ms = max(0, int(float(wait_s) * 1000.0))
    for pid, _name in pids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 4 or pid == self_pid:
            # pid 0/4 are System/Idle; our own pid is never a chiaki image.
            continue
        handle = 0
        try:
            handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
            if not handle:
                # Already gone (the common case) or access denied: the post-kill
                # snapshot below is the authority, not this call.
                failed.append(pid)
                continue
            if not kernel32.TerminateProcess(handle, 1):
                failed.append(pid)
                continue
            if kernel32.WaitForSingleObject(handle, budget_ms) == WAIT_OBJECT_0:
                killed.append(pid)
            else:
                failed.append(pid)
        except Exception:
            failed.append(pid)
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
    return killed, failed


def _terminate_chiaki_processes_spawning(names: Tuple[str, ...],
                                         running: Optional[set]) -> None:
    """Legacy last-resort sweep: taskkill.exe /IM <name> /F /T per image, with
    tasklist.exe verification when the toolhelp snapshot is unavailable.

    Kept ONLY as the fallback for a failed snapshot or a pid-scoped kill that did
    not clear the image, so the "fixed named pipes are free before launch"
    guarantee never rests on an in-process path that just failed. ``/T`` is kept
    here (and deliberately NOT reproduced in the pid path) because this branch
    runs blind: with no snapshot we cannot enumerate a tree ourselves."""
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    deadline = time.time() + 6.0
    while True:
        targets = (names if running is None
                   else tuple(n for n in names if n.lower() in running)) or names
        for name in targets:
            try:
                subprocess.run(
                    ["taskkill.exe", "/IM", name, "/F", "/T"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                    creationflags=_NO_WINDOW,
                )
            except Exception:
                pass
        if time.time() >= deadline:
            return
        running = _running_image_names(names)
        if running is not None:
            if not running:
                return
            time.sleep(0.15)
            continue
        # Snapshot unavailable: legacy tasklist verification.
        try:
            still_running = False
            for name in names:
                result = subprocess.run(
                    ["tasklist.exe", "/FI", f"IMAGENAME eq {name}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                    creationflags=_NO_WINDOW,
                )
                if name.lower() in (result.stdout or "").lower():
                    still_running = True
                    break
            if not still_running:
                return
        except Exception:
            return
        time.sleep(0.15)


# Last sweep's factual report (pids/names found, what was killed, whether the
# spawning fallback ran, wall cost). ensure_running() folds it into the stage
# summary so the NEXT connect-latency diagnosis is read, not inferred.
_LAST_SWEEP_REPORT: Dict[str, object] = {}


def last_sweep_report() -> Dict[str, object]:
    return dict(_LAST_SWEEP_REPORT)


def terminate_chiaki_processes() -> Dict[str, object]:
    """Free the fixed Orion named pipes: kill every chiaki-family process.

    [ORION_CONNECT_LATENCY 2026-08-29] One toolhelp snapshot answers "is there
    anything to kill?" in ~10ms, so a sweep with nothing running spawns NOTHING.
    [ORION_CONNECT_LATENCY 2026-09-14] When something IS running the kill is now
    in-process and PID-scoped (OpenProcess/TerminateProcess + a bounded wait)
    instead of 4x `taskkill.exe /IM <name> /F /T` at ~250ms each. Only pids whose
    image name is in ``_CHIAKI_IMAGE_NAMES`` are ever touched — never this
    process, never an unrelated one. Child TREES are intentionally not walked:
    the fixed pipes (orion_input / orion_frames under \\\\.\\pipe) are created by the
    client process itself, and OrionStream spawns no children that hold them, so
    a name-scoped kill is exactly the guarantee /T was standing in for. The
    legacy spawning sweep remains the last-resort fallback for a failed snapshot
    or a pid kill that did not clear the image.

    Returns a factual report (also available via ``last_sweep_report()``).
    """
    global _LAST_SWEEP_REPORT
    started = time.perf_counter()
    report: Dict[str, object] = {"found": [], "killed": [], "failed": [],
                                 "fallback": False, "ms": 0.0}
    _LAST_SWEEP_REPORT = report
    if os.name != "nt":
        return report
    names = _CHIAKI_IMAGE_NAMES
    running = _running_image_pids(names)
    if running is not None and not running:
        report["ms"] = (time.perf_counter() - started) * 1000.0
        return report
    if running:
        report["found"] = [f"{name}:{pid}" for pid, name in running]
        # Two in-process rounds: the second catches a client that respawned or a
        # handle that was still closing. Past that the image is not behaving like
        # our own client and the blind sweep takes over.
        for _round in range(2):
            killed, failed = _terminate_pids_in_process(running)
            report["killed"] = list(report["killed"]) + killed  # type: ignore[arg-type]
            report["failed"] = failed
            running = _running_image_pids(names)
            if running is not None and not running:
                report["ms"] = (time.perf_counter() - started) * 1000.0
                return report
            if running is None:
                break
            time.sleep(0.05)
    # Snapshot unavailable, or the pid-scoped kill did not clear the image.
    report["fallback"] = True
    _terminate_chiaki_processes_spawning(names, {n for _p, n in running} if running else None)
    report["ms"] = (time.perf_counter() - started) * 1000.0
    return report


# ---------------------------------------------------------------------------
# CONSOLE ADDRESS DRIFT prewarm
# ---------------------------------------------------------------------------
# [ORION_CONNECT_LATENCY 2026-09-14] _resolve_console_host() costs ~2.4-3.4s
# whenever the configured ip is silent (0.4s session-port probe + 0.8s discovery
# probe + 1.2s subnet broadcast). On this rig the console moved .126 -> .81 and
# settings still name .126, so EVERY first connect of an app run paid it inside
# ensure_running() — it was the whole of the mis-labelled `stale_sweep=3020ms`.
# The answer does not depend on the connect: resolve it in the BACKGROUND during
# the warm preview (the standby prewarm pass calls prewarm_console_host()) and
# the connect reads a cache. A cold cache falls back to today's synchronous
# resolution unchanged — the prewarm is an accelerator, never an authority.
_CONSOLE_HOST_PREWARM_TTL_S = 120.0
# [ORION_CONNECT_LATENCY 2026-09-19] MEASURED RACE, and the whole reason a connect
# is sometimes 3.5 s instead of 1.3 s.
#
# prewarm_console_host() used to short-circuit on "the entry is still fresh", so a
# refresh could only ever START once the entry had ALREADY expired. The orchestrator
# ticks it every 30 s (remote_play_orchestrator.py:3105) against a 120 s TTL, which
# leaves the cache EMPTY from expiry until the next tick finishes its ~2.4 s lookup —
# up to ~32 s of every 120 s, i.e. ~27 % of the wall clock. A Connect landing in that
# hole pays _console_host_lookup() synchronously inside its own start_stream budget.
#
# Live proof (logs/orion_native.log):
#   2026-09-19T02:33:23.578Z  CONSOLE ADDRESS DRIFT: ... console at 192.168.137.126   <- prewarm
#   2026-09-19T02:35:34.181Z  Chiaki Remote Play settings saved.                      <- click, +130.6 s
#   2026-09-19T02:35:37.550Z  start_stream timing: total=3103ms ... host_resolve=2440ms
# and 2026-09-18T18:28:06.028Z -> 18:28:09.546Z, total=3266ms host_resolve=2422ms.
# 2 of the 10 measured connects on this build; the other 8 had host_resolve=0 ms.
#
# The fix is to re-resolve while the answer is still VALID, so the cache never empties
# between ticks. REFRESH_AFTER must sit above the 30 s tick (so a tick is not a
# refresh every time) and far enough below the TTL that a ~2.4 s lookup always lands
# before expiry: 45 s gives worst-case refresh-start at 75 s and fresh again by ~78 s
# against a 120 s TTL. The SERVED answer is never older than the TTL — this only
# changes when the refresh starts, never how stale a returned address may be.
_CONSOLE_HOST_PREWARM_REFRESH_AFTER_S = 45.0
_console_host_prewarm: Dict[str, Tuple[str, float]] = {}
_console_host_prewarm_lock = threading.Lock()
_console_host_prewarm_inflight: set = set()
# configured host -> last adopted address we shouted about (log-level throttle).
_console_host_last_logged: Dict[str, str] = {}


def _console_host_prewarm_get(host: str) -> Optional[str]:
    """The background-resolved address for ``host``, or None when there is no
    fresh answer."""
    host = str(host or "").strip()
    if not host:
        return None
    with _console_host_prewarm_lock:
        entry = _console_host_prewarm.get(host)
    if not entry:
        return None
    resolved, stamped = entry
    if (time.monotonic() - float(stamped)) >= _CONSOLE_HOST_PREWARM_TTL_S:
        return None
    return str(resolved or "") or None


def _console_host_prewarm_age(host: str) -> Optional[float]:
    """Seconds since ``host`` was last background-resolved, or None when there is
    no entry at all. Deliberately ignores the TTL: this answers "how old", not
    "is it still servable" (that stays _console_host_prewarm_get's job)."""
    host = str(host or "").strip()
    if not host:
        return None
    with _console_host_prewarm_lock:
        entry = _console_host_prewarm.get(host)
    if not entry:
        return None
    return max(0.0, time.monotonic() - float(entry[1]))


def reset_console_host_prewarm() -> None:
    """Drop every prewarmed answer (tests, and a settings host change)."""
    with _console_host_prewarm_lock:
        _console_host_prewarm.clear()
        _console_host_last_logged.clear()


def prewarm_console_host(host: str) -> str:
    """Resolve CONSOLE ADDRESS DRIFT for ``host`` on a daemon thread so Connect
    never pays for it. Returns a factual state token; never raises, never blocks.

    Called from the standby prewarm pass (which the orchestrator already ticks
    every 30s while the preview is warm). Idempotent and single-flight.
    """
    host = str(host or "").strip()
    if not host:
        return "no-host"
    if str(os.environ.get("ORION_PS5_DISCOVER_DRIFT", "1")).strip().lower() in {
            "0", "false", "off"}:
        return "disabled"
    # [ORION_CONNECT_LATENCY 2026-09-19] Refresh BEFORE the entry expires (see the
    # _CONSOLE_HOST_PREWARM_REFRESH_AFTER_S note): the old `is not None -> "fresh"`
    # short-circuit meant a re-resolve could only start once the cache was already
    # empty, so every TTL boundary opened a ~32 s window in which a Connect paid the
    # full ~2.4 s lookup. An entry past REFRESH_AFTER is still SERVED normally while
    # this background refresh runs — nothing is invalidated here.
    age = _console_host_prewarm_age(host)
    if age is not None and age < _CONSOLE_HOST_PREWARM_REFRESH_AFTER_S:
        return "fresh"
    refreshing = age is not None
    with _console_host_prewarm_lock:
        if host in _console_host_prewarm_inflight:
            return "in-flight"
        _console_host_prewarm_inflight.add(host)

    def _worker() -> None:
        try:
            resolved = _console_host_lookup(host)
            with _console_host_prewarm_lock:
                _console_host_prewarm[host] = (resolved, time.monotonic())
        except Exception as exc:  # fail-open: a cold cache is today's behaviour
            logger.debug("Console host prewarm failed for %s: %s", host, exc)
        finally:
            with _console_host_prewarm_lock:
                _console_host_prewarm_inflight.discard(host)

    threading.Thread(target=_worker, name="console-host-prewarm",
                     daemon=True).start()
    # "refreshing" == an answer is still cached and servable while this runs;
    # "started" == the cache was cold, so a Connect right now still pays the lookup.
    return "refreshing" if refreshing else "started"


def _console_host_lookup(host: str) -> str:
    """The expensive part of CONSOLE ADDRESS DRIFT: returns the address to launch
    against (the configured one unless discovery names exactly one alternative).

    Shared by the connect path and the background prewarm. Never raises.
    """
    host = str(host or "").strip()
    if not host:
        return host
    try:
        import ps5_wake
        if not ps5_wake.is_ip_literal(host):
            return host
        if ps5_wake.session_port_open(host, timeout_s=0.4):
            return host
        if ps5_wake.probe_console_state(host, timeout_s=0.8).state != "no_answer":
            return host
        broadcast = ps5_wake.subnet_broadcast(host)
        if not broadcast:
            return host
        found = [c for c in ps5_wake.discover_consoles(broadcast, timeout_s=1.2)
                 if c.host != host]
        if len(found) != 1:
            if found:
                logger.error(
                    "CONSOLE ADDRESS DRIFT: configured %s is silent and %d consoles answer "
                    "discovery (%s) - not guessing; set remote_play_console_ip",
                    host, len(found), ", ".join(f"{c.host}:{c.state}" for c in found))
            return host
        adopted = found[0]
        # ERROR the first time and on every change (the native relay never
        # throttles ERROR, so the owner is told what to put in settings); INFO on
        # the repeats, because the background prewarm re-resolves on a timer and
        # an ERROR every two minutes would bury the line that matters.
        level = (logger.error
                 if _console_host_last_logged.get(host) != adopted.host
                 else logger.info)
        _console_host_last_logged[host] = adopted.host
        level(
            "CONSOLE ADDRESS DRIFT: configured %s is silent; discovery found the console at "
            "%s (%s) - using it for this launch. Update settings remote_play_console_ip to %s.",
            host, adopted.host, adopted.state, adopted.host)
        return adopted.host
    except Exception:
        return host


def chiaki_controller_env(disable_video: bool = False) -> dict:
    env = os.environ.copy()
    # Keep the physical DualSense/DualShock out of Chiaki's SDL HID path.
    # Orion mirrors the physical pad into ViGEm XUSB; if Chiaki also reads the
    # physical device, tempo remap and no-release automation are bypassed.
    env["SDL_GAMECONTROLLER_IGNORE_DEVICES"] = (
        "0x054c/0x0ce6,0x054c/0x05c4,0x054c/0x09cc,0x054c/0x0df2,0x054c/0x0e5f"
    )
    env["SDL_JOYSTICK_HIDAPI_PS5"] = "0"
    env["SDL_JOYSTICK_HIDAPI_PS4"] = "0"
    # Pre-encryption INPUT HOOK: when Orion is launched with ORION_INPUT_HOOK set, tell the patched
    # chiaki-ng to open its named-pipe input bridge so Orion can inject the bot's computed
    # ControllerState pre-encryption (bypassing ViGEm/SDL -> jitter-free release timing). Harmless on
    # stock chiaki (the var is simply ignored). Requires chiaki_path to point at the patched build
    # (native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/chiaki.exe).
    if os.environ.get("ORION_INPUT_HOOK"):
        env["CHIAKI_ORION_INPUT_PIPE"] = os.environ.get(
            "CHIAKI_ORION_INPUT_PIPE", r"\\.\pipe\orion_input"
        )
    # Decoded-FRAME export: when Orion is launched with ORION_FRAME_PIPE set, tell the patched chiaki-ng
    # to ship every decoded video frame to the bot over its named pipe (OrionFrameExport) so detection
    # reads the real decoded frame instead of GDI-capturing the window. Harmless on stock chiaki (the
    # var is ignored). The orchestrator attaches as the reader via frame_source='decoder'.
    if os.environ.get("ORION_FRAME_PIPE"):
        env["CHIAKI_ORION_FRAME_PIPE"] = os.environ.get(
            "CHIAKI_ORION_FRAME_PIPE", r"\\.\pipe\orion_frames"
        )
        # Lift OrionFrameExport's built-in 30fps export cap to 60. The PS5 already streams 60fps
        # and the GPU already decodes 60fps (chiaki log: "Estimated source FPS: 59.999"), but the
        # frame EXPORT to the detector/preview pipe was hard-capped at 30 ("OrionFrameExport:
        # export cap 30 fps"). That single cap was the sole cause of BOTH the 30fps live capture
        # AND the inconsistent bot (the engine only saw a meter sample every ~33ms vs a ~30-40ms
        # green window). 60fps export => ~16ms between samples, comfortably inside the window.
        # Overridable (drop back to "30" if the GPU readback ever starves chiaki's present).
        env["CHIAKI_ORION_FRAME_FPS"] = os.environ.get("CHIAKI_ORION_FRAME_FPS", "60")
    # The capture card is Orion's sole video source in this mode.  The custom
    # OrionStream still performs the PS5/input handshake (and may carry audio),
    # but discarding its video before decode avoids a hidden 60 Hz decoder/drop
    # loop competing with the SHM preview.  Explicitly remove a parent value on
    # every other route so decoder-pipe users always retain decoded video.
    if disable_video:
        env["CHIAKI_ORION_DISABLE_VIDEO"] = "1"
    else:
        env.pop("CHIAKI_ORION_DISABLE_VIDEO", None)
    return env


def optimize_chiaki_process(pid: int) -> None:
    if os.name != "nt" or not pid:
        return
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_SET_INFORMATION = 0x0200
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        HIGH_PRIORITY_CLASS = 0x00000080
        # ctypes defaults an undeclared return value to a 32-bit C int.
        # HANDLE is pointer-sized on the shipped x64 runtime; truncation here
        # can silently break priority setup or prevent the handle from closing.
        # Bind a private DLL instance rather than changing shared windll APIs.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.SetProcessPriorityBoost.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        kernel32.SetProcessPriorityBoost.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            logger.debug("Chiaki priority setup OpenProcess failed: %d", ctypes.get_last_error())
            return
        try:
            if not kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS):
                logger.debug("Chiaki SetPriorityClass failed: %d", ctypes.get_last_error())
            # Disable dynamic priority boost changes for steadier frame cadence.
            if not kernel32.SetProcessPriorityBoost(handle, True):
                logger.debug("Chiaki SetProcessPriorityBoost failed: %d", ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        logger.debug("Chiaki process optimization failed: %s", exc)


# ---------------------------------------------------------------------------
# Standby client pool — "pre-booted client, deferred session"
# ---------------------------------------------------------------------------
# [ORION_CONNECT_LATENCY 2026-08-30] The measured warm-Connect breakdown left the
# Chiaki client's OWN boot (process spawn -> Qt/QML/decoder init, ~1.0-1.3s idle,
# ~3.2s under detector/capture CPU contention) as the dominant cost. The pool
# spawns that client EARLY — while the warm capture preview runs, long before
# Connect — in the fork's `--standby` mode, which pays the whole boot but opens
# NO console session (the fork refuses every session-creation path until the
# control pipe's explicit `open` command). On Connect the manager promotes the
# already-booted client, paying only the ~0.6s PS5 handshake plus ~1ms of IPC.
#
# SAFETY BY CONSTRUCTION: a standby client has no chiaki session (guarded at the
# fork's single `new StreamSession` site), so no input can reach the console and
# the PS5 is never held while the UI says disconnected. Readiness stays PROVEN:
# promotion still requires the launch-scoped streaminfo marker in a fresh
# session log, exactly like a cold spawn.
#
# OLD-CLIENT FAIL-CLOSED (corrected 2026-08-30, measured on the real pre-standby
# binary): a pre-standby OrionStream.exe does NOT exit on the unknown `--standby`
# option. QCommandLineParser.process() on a Windows GUI app raises a MODAL native
# error dialog (class #32770, title "Chiaki") and the process lingers un-exited
# behind it, VISIBLE on the user's desktop. It never touches the console — the
# parse error precedes all session work (verified: no session log, no pipes) —
# but a dialog per preview is unacceptable, so the pool never spawns `--standby`
# at a binary that cannot parse it: it first sniffs the executable for the
# CHIAKI_ORION_STANDBY_PIPE literal (load-bearing in every standby-capable
# build, absent from every earlier one; measured 3 hits vs 0). A binary that
# passes the sniff but never creates its control pipe is killed and latched
# unsupported at the boot deadline; one that exits young with an error latches
# via the early-exit verdict. `ORION_STANDBY_CLIENT=0` disables the pool.

_STANDBY_READY_REPLY = "ok standby"
_STANDBY_OPEN_OK_REPLY = "ok opening"
# A standby child that dies this quickly never served a promote; treat its exit
# code as a verdict on `--standby` support for this exact binary.
_STANDBY_EARLY_EXIT_S = 15.0
# How long an "unsupported binary" verdict stands before re-probing (a doomed
# probe spawn costs ~100ms once per TTL — cheap insurance against a false latch).
_STANDBY_UNSUPPORTED_TTL_S = 600.0
# The env-var literal main.cpp reads to name the control pipe. Present in every
# standby-capable OrionStream.exe by construction, absent from every earlier
# build — the spawn-free capability test that keeps a stale install from ever
# showing the parser-error dialog (see OLD-CLIENT FAIL-CLOSED above).
_STANDBY_CAPABILITY_MARKER = b"CHIAKI_ORION_STANDBY_PIPE"
# A standby that has not produced its control pipe by now never will (measured
# boot-to-pipe: ~0.9s idle, ~3.2s under live detector/capture contention —
# this is >10x the worst case, so a breach is a wedge, not load). The claim
# path has its own 2s budget; this deadline is the PREWARM-side containment
# that kills the wedge and latches the binary so it is not respawned.
_STANDBY_BOOT_DEADLINE_S = 45.0


def standby_client_enabled() -> bool:
    return str(os.environ.get("ORION_STANDBY_CLIENT", "1")).strip().lower() not in (
        "0", "false", "off", "no")


def _standby_pipe_path(pipe_name: str) -> str:
    return "\\\\.\\pipe\\" + str(pipe_name)


def _standby_pipe_exists(pipe_path: str) -> bool:
    """True once the standby control pipe exists — the fork creates it only AFTER
    its full boot completed, so existence IS the boot-complete proof."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.WaitNamedPipeW(str(pipe_path), 1))
    except Exception:
        return False


def _standby_pipe_transact(pipe_path: str, command: str, timeout_s: float = 2.0,
                           *, cancel_event=None, write_lock=None) -> str:
    """One line out, one line back on the standby control pipe. Returns the reply
    (stripped) or "" on any failure/timeout. Runs in a worker thread so a wedged
    pipe can never hang the caller past its budget."""
    result: List[str] = []
    expired = threading.Event()

    def _worker() -> None:
        try:
            handle = open(pipe_path, "r+b", buffering=0)
        except OSError:
            return
        try:
            # A timed-out daemon can finish opening the pipe MUCH later. It must
            # not issue an old "open" after shutdown/a subsequent connection.
            # Serialize only the small command write with manager.stop(), never
            # the potentially blocking pipe open or reply read.
            with write_lock if write_lock is not None else nullcontext():
                if expired.is_set() or (cancel_event is not None and cancel_event.is_set()):
                    return
                handle.write(command.encode("ascii", "replace") + b"\n")
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 256:
                chunk = handle.read(1)
                if not chunk:
                    break
                reply += chunk
            result.append(reply.strip().decode("ascii", "replace"))
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass

    thread = threading.Thread(target=_worker, name="standby-pipe", daemon=True)
    thread.start()
    thread.join(timeout=max(0.1, float(timeout_s)))
    expired.set()
    return result[0] if result else ""


@dataclass
class StandbyClient:
    process: subprocess.Popen
    pipe_name: str
    pipe_path: str
    executable_path: str
    nickname: str
    host: str
    disable_video: bool
    identity_sha256: str
    identity_size: int
    spawned_at: float
    # Set the first time the control pipe is observed: a child that WAS ready and
    # later died was a working standby build (crash/sweep), not an unsupported one.
    ready_seen: bool = False


class StandbyClientPool:
    """Owns at most ONE standby client. Spawned during the warm preview by the
    orchestrator's prewarm loop; claimed (removed) by the manager's promote path
    on Connect. Every validation failure kills the candidate and reports None so
    the caller falls back to today's cold-spawn path — the pool can defer work,
    never block a connect."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: Optional[StandbyClient] = None
        self._unsupported: Dict[tuple, str] = {}
        # fingerprint -> bool: does the binary embed the standby marker? A
        # fingerprint pins path+size+mtime, so a verdict cannot outlive the
        # bytes it was measured on; sniff FAILURES are never cached.
        self._capability: Dict[tuple, bool] = {}
        self._spawn_counter = 0
        self._atexit_registered = False

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _binary_fingerprint(path: str) -> Optional[tuple]:
        try:
            st = os.stat(path)
            return (os.path.normcase(os.path.abspath(path)),
                    int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            return None

    def _binary_supports_standby(self, path: str, fingerprint: tuple) -> bool:
        """Spawn-free capability test: scan the executable for the standby
        control-pipe env-var literal. Measured 2026-08-30 on the real binaries:
        3 hits in a standby build, 0 in the pre-standby one — and the old
        client, if spawned with `--standby`, would raise a visible modal error
        dialog and linger instead of exiting (see OLD-CLIENT FAIL-CLOSED).
        Fails CLOSED on read errors: only the shortcut is lost, the cold spawn
        path is untouched."""
        cached = self._capability.get(fingerprint)
        if cached is not None:
            return cached
        marker = _STANDBY_CAPABILITY_MARKER
        supported = False
        try:
            with open(path, "rb") as fh:
                tail = b""
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    if marker in tail + chunk:
                        supported = True
                        break
                    tail = chunk[-(len(marker) - 1):]
        except OSError as exc:
            logger.warning("Standby capability sniff failed for %s: %s", path, exc)
            return False
        if len(self._capability) >= 8:
            self._capability.clear()
        self._capability[fingerprint] = supported
        if not supported:
            logger.info(
                "Standby mode unavailable: %s predates --standby support "
                "(no control-pipe marker); Connect keeps today's cold-spawn path",
                path)
        return supported

    def _enforce_boot_deadline_locked(self) -> None:
        """Kill and latch a standby whose control pipe never appeared. The old
        pre-standby client wedges on a modal parser-error dialog instead of
        exiting; the capability sniff should keep it from ever being spawned,
        and this deadline contains anything else that boots but never serves."""
        client = self._client
        if client is None or client.ready_seen:
            return
        if _standby_pipe_exists(client.pipe_path):
            client.ready_seen = True
            return
        if time.monotonic() - client.spawned_at < _STANDBY_BOOT_DEADLINE_S:
            return
        self._client = None
        fingerprint = self._binary_fingerprint(client.executable_path)
        if fingerprint is not None:
            self._unsupported[fingerprint] = (
                time.monotonic(),
                f"control pipe never appeared within {_STANDBY_BOOT_DEADLINE_S:.0f}s")
            logger.warning(
                "Standby mode marked unsupported for %s (%s) - Connect keeps "
                "today's cold-spawn path (re-probed after %.0fs or a binary change)",
                client.executable_path, self._unsupported[fingerprint][1],
                _STANDBY_UNSUPPORTED_TTL_S)
        self._kill_locked(client, "control pipe never appeared within the boot deadline")

    def _kill_locked(self, client: StandbyClient, reason: str) -> None:
        logger.info("Standby client discarded (%s): pid=%s",
                    reason, getattr(client.process, "pid", 0))
        try:
            if client.process.poll() is None:
                client.process.terminate()
                try:
                    client.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    client.process.kill()
                    try:
                        client.process.wait(timeout=1.0)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Standby discard cleanup failed: %s", exc)

    def _prune_dead_locked(self) -> None:
        client = self._client
        if client is None:
            return
        rc = None
        try:
            rc = client.process.poll()
        except Exception:
            rc = -1
        if rc is None:
            return
        self._client = None
        age_s = time.monotonic() - client.spawned_at
        if (age_s < _STANDBY_EARLY_EXIT_S and int(rc or 0) != 0
                and not client.ready_seen):
            # Never became ready and died immediately with an error: this binary
            # (most likely a pre-standby client rejecting the unknown option)
            # does not support standby. The latch EXPIRES so a false positive —
            # e.g. a young standby caught by a broad taskkill sweep, whose
            # victims also exit 1 — self-heals instead of disabling standby for
            # the life of the binary.
            fingerprint = self._binary_fingerprint(client.executable_path)
            if fingerprint is not None:
                self._unsupported[fingerprint] = (
                    time.monotonic(),
                    f"standby child exited rc={rc} after {age_s:.1f}s")
                logger.warning(
                    "Standby mode marked unsupported for %s (%s) - Connect keeps "
                    "today's cold-spawn path (re-probed after %.0fs or a binary change)",
                    client.executable_path, self._unsupported[fingerprint][1],
                    _STANDBY_UNSUPPORTED_TTL_S)
        else:
            logger.info("Standby client exited (rc=%s after %.1fs); will respawn on "
                        "the next prewarm pass", rc, age_s)

    # -- public API --------------------------------------------------------

    def state(self) -> str:
        with self._lock:
            self._prune_dead_locked()
            self._enforce_boot_deadline_locked()
            client = self._client
            if client is None:
                return "absent"
            if client.ready_seen or _standby_pipe_exists(client.pipe_path):
                client.ready_seen = True
                return "ready"
            return "booting"

    def ensure_spawned(self, *, chiaki_path: str = "", console_ip: str = "",
                       disable_video: bool = False, identity_sha256: str = "",
                       identity_size: int = -1) -> str:
        """Spawn the standby client if none exists. Returns a factual state token;
        never raises. Refuses to spawn beside ANY other chiaki-family process."""
        if not standby_client_enabled():
            return "disabled"
        if os.name != "nt":
            return "unsupported-os"
        # [ORION_CONNECT_LATENCY 2026-09-14] Pay CONSOLE ADDRESS DRIFT resolution
        # here, on the prewarm thread, for the same reason the client boot is paid
        # here: it is ~2.4s of socket timeouts that does not depend on the connect.
        # Non-blocking and single-flight; a cold cache just means today's
        # synchronous resolution inside ensure_running().
        try:
            prewarm_console_host(console_ip)
        except Exception as exc:
            logger.debug("Console host prewarm kick failed: %s", exc)
        with self._lock:
            self._prune_dead_locked()
            self._enforce_boot_deadline_locked()
            if self._client is not None:
                return "present"
            chiaki = find_chiaki_binary(chiaki_path)
            if not chiaki:
                return "no-binary"
            fingerprint = self._binary_fingerprint(chiaki)
            if fingerprint is not None:
                verdict = self._unsupported.get(fingerprint)
                if verdict is not None:
                    if time.monotonic() - verdict[0] < _STANDBY_UNSUPPORTED_TTL_S:
                        return "unsupported"
                    del self._unsupported[fingerprint]
            host = str(console_ip or "").strip()
            if not host:
                return "no-host"
            running = _running_image_pids(_CHIAKI_IMAGE_NAMES)
            if running is None or running:
                # A live session, a stale client, or an unanswerable snapshot:
                # the connect path's sweep owns those cases; a standby spawned
                # beside them would only be swept as one more stale process.
                return "busy"
            if fingerprint is not None and not self._binary_supports_standby(
                    chiaki, fingerprint):
                # A pre-standby client would show a modal parser-error dialog
                # and linger (verified 2026-08-30); never spawn `--standby` at
                # one. Sits after the cheap refusals so the 7MB scan (cached by
                # fingerprint) only ever runs when a spawn was otherwise due.
                return "no-standby-support"
            probe = _probe_chiaki_client_cached(chiaki)
            if not probe.client_ran or not probe.nickname:
                return "no-nickname"

            expected_sha = str(identity_sha256 or "").strip().lower()
            try:
                expected_size = int(identity_size)
            except (TypeError, ValueError, OverflowError):
                expected_size = -1
            identity_requested = bool(expected_sha or expected_size >= 0)

            self._spawn_counter += 1
            pipe_name = f"orion_standby_{os.getpid()}_{self._spawn_counter}"
            env = chiaki_controller_env(disable_video=bool(disable_video))
            env["CHIAKI_ORION_STANDBY_PIPE"] = pipe_name
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

            def _spawn() -> subprocess.Popen:
                # Options must precede the positionals (QCommandLineParser runs in
                # ParseAsPositionalArguments mode; see the cold-launch comment).
                return subprocess.Popen(
                    [chiaki, "--standby", "stream", probe.nickname, host],
                    cwd=os.path.dirname(chiaki) if os.path.isfile(chiaki) else None,
                    env=env,
                    startupinfo=startupinfo,
                )

            try:
                if identity_requested:
                    if (expected_size < 0 or len(expected_sha) != 64
                            or any(ch not in "0123456789abcdef" for ch in expected_sha)):
                        return "identity-malformed"
                    with locked_executable_snapshot(chiaki) as snapshot:
                        if (not snapshot.valid or snapshot.size != expected_size
                                or snapshot.sha256 != expected_sha):
                            return "identity-mismatch"
                        process = _spawn()
                else:
                    process = _spawn()
            except Exception as exc:
                logger.warning("Standby client spawn failed: %s", exc)
                return "spawn-failed"

            self._client = StandbyClient(
                process=process,
                pipe_name=pipe_name,
                pipe_path=_standby_pipe_path(pipe_name),
                executable_path=chiaki,
                nickname=probe.nickname,
                host=host,
                disable_video=bool(disable_video),
                identity_sha256=expected_sha if identity_requested else "",
                identity_size=expected_size if identity_requested else -1,
                spawned_at=time.monotonic(),
            )
            if not self._atexit_registered:
                self._atexit_registered = True
                atexit.register(self.shutdown)
            logger.info(
                "Standby Remote Play client spawned (pid=%s, nickname=%s) - boot "
                "is being paid now instead of on Connect",
                getattr(process, "pid", 0), probe.nickname)
            # Isolation proof, mirroring the cold-spawn line: env= is authoritative
            # for the child, so a routing-leak investigation can confirm the
            # no-cloak path for a PROMOTED standby too.
            logger.info(
                "Standby isolation env (pid=%s): IGNORE_DEVICES=%s HIDAPI_PS5=%s HIDAPI_PS4=%s",
                getattr(process, "pid", 0),
                env.get("SDL_GAMECONTROLLER_IGNORE_DEVICES"),
                env.get("SDL_JOYSTICK_HIDAPI_PS5"),
                env.get("SDL_JOYSTICK_HIDAPI_PS4"),
            )
            return "spawned"

    def claim(self, *, nickname: str, disable_video: bool, executable_path: str,
              identity_sha256: str = "", identity_size: int = -1,
              wait_ready_s: float = 2.0) -> Optional[StandbyClient]:
        """Hand the standby to a connect attempt, removing it from the pool.

        Every mismatch against the CURRENT connect parameters kills the standby
        and returns None: a standby is a shortcut, never an authority — the
        caller's cold path remains the source of truth."""
        if not standby_client_enabled():
            return None
        with self._lock:
            self._prune_dead_locked()
            client = self._client
            if client is None:
                return None
            self._client = None

            expected_sha = str(identity_sha256 or "").strip().lower()
            try:
                expected_size = int(identity_size)
            except (TypeError, ValueError, OverflowError):
                expected_size = -1
            identity_requested = bool(expected_sha or expected_size >= 0)

            if str(nickname or "") != client.nickname:
                self._kill_locked(client, "nickname changed since spawn")
                return None
            if bool(disable_video) != client.disable_video:
                self._kill_locked(client, "video-disable route changed since spawn")
                return None
            if (os.path.normcase(os.path.abspath(str(executable_path or "")))
                    != os.path.normcase(os.path.abspath(client.executable_path))):
                self._kill_locked(client, "client binary path changed since spawn")
                return None
            if identity_requested and (client.identity_sha256 != expected_sha
                                       or client.identity_size != expected_size):
                self._kill_locked(client, "executable identity changed since spawn")
                return None

            deadline = time.monotonic() + max(0.0, float(wait_ready_s))
            while not _standby_pipe_exists(client.pipe_path):
                if client.process.poll() is not None:
                    self._kill_locked(client, "died before becoming ready")
                    return None
                if time.monotonic() >= deadline:
                    self._kill_locked(client, "not ready within the claim budget")
                    return None
                time.sleep(0.02)
            client.ready_seen = True

            reply = _standby_pipe_transact(client.pipe_path, "ping", timeout_s=1.0)
            if reply != _STANDBY_READY_REPLY:
                self._kill_locked(client, f"unexpected ping reply {reply!r}")
                return None
            return client

    def discard(self, reason: str = "") -> None:
        with self._lock:
            client = self._client
            self._client = None
            if client is not None:
                self._kill_locked(client, reason or "discarded")

    def shutdown(self) -> None:
        """Best-effort clean exit for an unclaimed standby (atexit + orchestrator
        stop). The broad image-name sweeps and the native job object remain the
        containment backstops."""
        with self._lock:
            client = self._client
            self._client = None
        if client is None:
            return
        try:
            if client.process.poll() is None:
                _standby_pipe_transact(client.pipe_path, "quit", timeout_s=0.5)
                try:
                    client.process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass
            if client.process.poll() is None:
                client.process.kill()
                try:
                    client.process.wait(timeout=1.0)
                except Exception:
                    pass
            logger.info("Standby client shut down (pid=%s)",
                        getattr(client.process, "pid", 0))
        except Exception as exc:
            logger.debug("Standby shutdown cleanup failed: %s", exc)


_STANDBY_POOL: Optional[StandbyClientPool] = None
_STANDBY_POOL_LOCK = threading.Lock()


def get_standby_pool() -> StandbyClientPool:
    global _STANDBY_POOL
    with _STANDBY_POOL_LOCK:
        if _STANDBY_POOL is None:
            _STANDBY_POOL = StandbyClientPool()
        return _STANDBY_POOL


class RemotePlayClientManager:
    def __init__(self, config: Optional[RemotePlayClientConfig] = None) -> None:
        self._config = config or RemotePlayClientConfig()
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.RLock()
        self._stop_requested = threading.Event()
        # Monotonic within this manager.  The decoder-pipe reader joins the pipe
        # server PID to this exact owned launch generation; a numeric PID alone is
        # not sufficient because Windows eventually recycles PIDs.
        self._launch_generation_counter = 0
        self._owned_launch_generation = 0
        self._owned_launch_path = ""
        self._owned_creation_time_100ns = 0
        self._status = RemotePlayClientStatus()
        self._session_log_dir = _chiaki_session_log_dir(self._config.session_log_dir)
        self._session_log_baseline: Dict[str, int] = {}
        self._session_tracker = _SessionReadinessTracker(self._session_log_dir)
        self._failed_start_reaped = False
        self._stale_client_cleanup_done = False
        # Per-stage wall-clock (ms) of the most recent ensure_running() run.
        self.last_stage_timings: Dict[str, float] = {}

    @property
    def status(self) -> RemotePlayClientStatus:
        return self._status

    def owned_process_identity(self) -> Dict[str, object]:
        """Return the live child identity used by decoder-pipe provenance checks.

        ``Popen`` retains the Windows process handle for this exact process object.
        Keeping the manager (and therefore that handle) alive prevents a stale PID
        from being mistaken for a replacement process between pipe inspection and
        frame delivery.
        """
        process = self._process
        if process is None:
            return {}
        try:
            if process.poll() is not None:
                return {}
            pid = int(getattr(process, "pid", 0) or 0)
        except Exception:
            return {}
        if pid <= 0 or self._owned_launch_generation <= 0 or not self._owned_launch_path:
            return {}
        process_handle = getattr(process, "_handle", None)
        return {
            "pid": pid,
            "launch_generation": int(self._owned_launch_generation),
            "creation_time_100ns": int(self._owned_creation_time_100ns),
            "path": self._owned_launch_path,
            "process_handle": process_handle,
        }

    def is_running(self) -> bool:
        """Return whether the client this manager owns/adopted is still alive.

        The orchestrator used the mere existence of this manager object as a
        liveness proof. In capture-card input-only mode Chiaki can be alive at
        promotion time, exit seconds later, and leave the object behind forever;
        all later promotes then became no-ops while the bot had no console route.
        """
        if self._process is not None:
            return self._process.poll() is None
        hwnd = int(getattr(self._status, "hwnd", 0) or 0)
        if not hwnd or os.name != "nt":
            return False
        try:
            import ctypes
            return bool(ctypes.windll.user32.IsWindow(hwnd))
        except Exception:
            return False

    def is_session_ready(self) -> bool:
        """Current console-session authority, independent of local pipe/process liveness."""
        if self._stop_requested.is_set() or not self.is_running():
            self._status.session_ready = False
            self._readiness_reason = ('process_exited' if self._process is not None
                                      else 'not_running')
            return False
        if not self._status.ok:
            self._status.session_ready = False
            self._readiness_reason = 'launch_not_ready'
            return False
        if not self._config.require_session_ready:
            with self._process_lock:
                if self._stop_requested.is_set():
                    self._readiness_reason = 'stopped'
                    return False
                self._readiness_reason = 'readiness_not_required'
                return True
        state, path = self._session_tracker.poll()
        with self._process_lock:
            if self._stop_requested.is_set():
                self._status.session_ready = False
                self._readiness_reason = 'stopped'
                return False
            self._readiness_reason = 'session_' + state
            self._status.session_ready = state == 'ready'
            if path:
                self._status.session_log_path = path
            return self._status.session_ready

    def readiness_diagnostic(self) -> Dict[str, object]:
        """Snapshot only: no launch, I/O, credentials, command line, or authority grant."""
        process = self._process
        return {
            'reason': getattr(self, '_readiness_reason', 'not_checked'),
            'pid': int(getattr(process, 'pid', 0) or 0),
            'exit_code': getattr(process, 'returncode', None),
            'session_log': os.path.basename(self._status.session_log_path or '')[:128],
        }

    def _cancelled_status(self):
        return RemotePlayClientStatus(ok=False, session_ready=False,
                                      message="Remote Play launch cancelled by shutdown")

    def _publish_ready_status(self, status):
        # Window scans and priority bookkeeping above this boundary can block or
        # yield. A result that completed after stop is not current authority.
        with self._process_lock:
            if self._stop_requested.is_set():
                return self._cancelled_status()
            self._status = status
            return status

    def ensure_running(self) -> RemotePlayClientStatus:
        # The native side hands us wait_timeout_s sized against ITS OWN hard
        # promotion deadline (15s vs kStreamPromoteDeadlineMs=20000): everything
        # this method does — stale sweeps, client probe, console wake, launch,
        # readiness poll — must fit inside that budget TOGETHER. Anchor the
        # whole run here so prep time (including a rest-mode wake) comes out of
        # the readiness poll instead of extending the total past the native
        # deadline, where our carefully-built error text is never surfaced.
        if self._stop_requested.is_set():
            return self._cancelled_status()
        self._prep_started = time.time()
        # [ORION_CONNECT_LATENCY 2026-08-29] Per-stage wall-clock instrumentation.
        # The 5.3s warm promote was diagnosed from a single fallback-timer log line
        # once already; every stage now stamps its own cost so the next breakdown
        # is read, not inferred. Exposed via last_stage_timings / stage_summary().
        stages: Dict[str, float] = {}
        self.last_stage_timings = stages
        _mark = time.perf_counter()
        self._failed_start_reaped = False
        self._stale_client_cleanup_done = False
        # [ORION_STANDBY 2026-08-30] Promote a pre-booted standby client when one is
        # available and matches this exact connect: pays ~1ms of IPC instead of the
        # client's whole boot. Every mismatch or hiccup returns None and the cold
        # path below runs unchanged — the standby is an accelerator, never an
        # authority. A terminal (ok=False) result is a console wake refusal, which
        # is the same verdict the cold path would produce.
        launch: Optional[RemotePlayClientStatus] = None
        if self._config.require_session_ready and standby_client_enabled():
            # [ORION_CONNECT_LATENCY 2026-09-14] Timed SEPARATELY. Until today the
            # _mark above ran straight through to the stale_sweep_ms stamp, so the
            # whole standby attempt — including _resolve_console_host()'s 2.4s of
            # socket timeouts — was reported as `stale_sweep=3020ms` and three
            # weeks of diagnosis chased a taskkill sweep that cost ~12ms.
            launch = self._promote_standby(stages)
            stages["standby_attempt_ms"] = (time.perf_counter() - _mark) * 1000.0
            if launch is not None and not launch.ok:
                self._status = launch
                self._log_stage_summary("wake_refused")
                return launch
        if self._stop_requested.is_set():
            return self._cancelled_status()
        if launch is None:
            _mark = time.perf_counter()
            # The Orion input/frame hooks use fixed named pipes. A stale OrionStream
            # process can keep those server pipes open after a watchdog restart; if we
            # "adopt" that window, the fresh stream process cannot create its bridges
            # (ERROR_PIPE_BUSY) and both detection and input hook silently degrade.
            if self._config.close_on_stop and (os.environ.get("ORION_INPUT_HOOK") or os.environ.get("ORION_FRAME_PIPE")):
                terminate_chiaki_processes()
                self._stale_client_cleanup_done = True
                stages["stale_sweep_ms"] = (time.perf_counter() - _mark) * 1000.0
                self._note_sweep(stages)
                _mark = time.perf_counter()

            # Snapshot AFTER stale-child cleanup.  Any old success marker, including
            # final shutdown bytes written during cleanup, is outside this launch's
            # authority window.
            self._session_log_baseline = _snapshot_session_logs(self._session_log_dir)
            self._session_tracker.reset(self._session_log_baseline)

            hwnd, title = find_remote_play_window(self._config.window_title)
            stages["prelaunch_scan_ms"] = (time.perf_counter() - _mark) * 1000.0
            if hwnd and not self._config.require_session_ready:
                return self._publish_ready_status(RemotePlayClientStatus(
                    ok=True,
                    mode="existing",
                    hwnd=hwnd,
                    window_title=title,
                    message="Remote Play window already running",
                    session_ready=True,
                ))

            mode = self._normalize_mode(self._config.client_mode, self._config.platform)
            _mark = time.perf_counter()
            launch = self._launch(mode)
            stages["launch_total_ms"] = (time.perf_counter() - _mark) * 1000.0
        if self._stop_requested.is_set():
            return self._cancelled_status()
        if not launch.ok:
            self._status = launch
            self._log_stage_summary("launch_failed")
            return launch

        deadline = self._readiness_deadline(time.time())
        ready_hwnd = 0
        ready_title = ""
        last_session_state = "waiting"
        last_session_log = ""
        wait_started = time.perf_counter()
        last_window_scan = 0.0
        while time.time() < deadline:
            if self._stop_requested.is_set():
                return self._cancelled_status()
            # [ORION_CONNECT_LATENCY 2026-08-29] On the session-ready path the
            # window is bookkeeping (embed/graceful-close), never the readiness
            # authority — the log marker is. Scanning every 25ms iteration ran
            # EnumWindows+EnumChildWindows+OpenProcess ~40x/s for nothing; under
            # live capture/detector load each scan is tens of ms and steals poll
            # cadence from the marker. Throttle it to 10Hz there; the window-only
            # (non-session) path keeps its every-iteration scan, where the window
            # IS the readiness signal.
            _now = time.perf_counter()
            if (not self._config.require_session_ready
                    or _now - last_window_scan >= 0.1):
                last_window_scan = _now
                hwnd, title = find_remote_play_window(self._config.window_title)
                if hwnd:
                    ready_hwnd, ready_title = hwnd, title
            if self._config.require_session_ready:
                last_session_state, last_session_log = self._session_tracker.poll()
                if self._stop_requested.is_set():
                    return self._cancelled_status()
                if last_session_log and "client_boot_ms" not in stages:
                    # First launch-scoped bytes from the child: everything before
                    # this is client start-up, everything after is the console
                    # handshake (chiaki's first write lands with its session
                    # request — measured 2026-08-29, they are <10ms apart).
                    stages["client_boot_ms"] = (time.perf_counter() - wait_started) * 1000.0
                if last_session_state == "ready":
                    ready_wait_ms = (time.perf_counter() - wait_started) * 1000.0
                    stages["ready_wait_ms"] = ready_wait_ms
                    stages["handshake_ms"] = max(
                        0.0, ready_wait_ms - stages.get("client_boot_ms", 0.0))
                    # The window may not have been scanned since it appeared
                    # (10Hz throttle above); one final scan keeps status.hwnd
                    # fresh for embed/graceful-close without ever having gated
                    # readiness on it.
                    if not ready_hwnd:
                        hwnd, title = find_remote_play_window(self._config.window_title)
                        if hwnd:
                            ready_hwnd, ready_title = hwnd, title
                    # The controller route is not authorized before this fresh
                    # marker.  Keep the new child at Windows' normal priority
                    # during process/decoder startup so it cannot pre-empt the
                    # already-live capture-card/QML preview, then apply Orion's
                    # streaming priority policy exactly when input becomes live.
                    optimize_chiaki_process(int(getattr(self._process, "pid", 0) or 0))
                    status = self._publish_ready_status(RemotePlayClientStatus(
                        ok=True,
                        mode=launch.mode,
                        path=launch.path,
                        pid=launch.pid,
                        hwnd=ready_hwnd,
                        window_title=ready_title,
                        message="Remote Play console input session ready",
                        session_ready=True,
                        session_log_path=last_session_log,
                    ))
                    self._log_stage_summary("ready" if status.ok else "cancelled")
                    return status
                if last_session_state == "ended":
                    # A launch-scoped disconnect/quit before streaminfo is a
                    # definitive console-session failure, not a reason to keep
                    # displaying Connecting for the remainder of the configured
                    # connection budget. OrionStream may keep its Qt process alive
                    # and retry every five seconds, but none of those retries is
                    # authorized to hold the controller route open.
                    break
            elif ready_hwnd:
                return self._publish_ready_status(RemotePlayClientStatus(
                    ok=True,
                    mode=launch.mode,
                    path=launch.path,
                    pid=launch.pid,
                    hwnd=ready_hwnd,
                    window_title=ready_title,
                    message="Remote Play client window ready",
                    session_ready=True,
                ))
            if self._process and self._process.poll() is not None:
                break
            # [ORION_CONNECT_LATENCY 2026-08-12] #89. The readiness MARKER is the console's to
            # produce -- measured 2026-08-12, the Chiaki child launch plus PS5 stream handshake is
            # ~2.4s of the ~3.3s connect and none of it is ours to remove here. What IS ours is how
            # long we sit on a marker that has already landed: a 100ms poll adds up to 100ms (50ms
            # average) of pure dead time AFTER the console is ready. 25ms cuts that to ~12ms.
            #
            # Cheap because the loop body is two window/log-tail probes, not I/O: at 40Hz for the
            # ~2.4s handshake that is ~96 iterations against ~24, on a thread that is otherwise
            # blocked doing nothing. Deliberately not lower -- past ~40Hz the probes start costing
            # more than the latency they save, and the remaining win is bounded by 25ms anyway.
            #
            # This does NOT make connect near-instant. The architectural fix is pre-launching
            # Chiaki during live preview so the handshake is already done when Connect is pressed;
            # that is a real design change and is not attempted here.
            #
            # [ORION_STANDBY 2026-08-30] On a standby PROMOTE the client boot is already
            # paid, so the whole wait is ~0.7s and the 25ms quantum is a visible slice of
            # it (mean +12.5ms sitting on a marker that has already landed). The poll body
            # measures 0.315ms, so 100Hz costs ~3% of one core for well under a second —
            # cheap next to the live capture threads it briefly shares a box with. The
            # cold path keeps the measured 25ms choice above unchanged.
            time.sleep(0.010 if "standby" in stages else 0.025)

        if self._config.require_session_ready:
            detail = ("the current Chiaki session ended before becoming ready"
                      if last_session_state == "ended"
                      else "no fresh console-session readiness marker was observed")
            target = str(self._config.console_ip or "the configured PS5").strip()
            message = (
                f"Remote Play input session to {target} did not become ready: {detail}. "
                "Verify the PS5 is powered/reachable on the selected adapter and registered "
                "in Chiaki, then press Connect again."
            )
            message += self._console_failure_evidence()
        else:
            message = (
                "Remote Play client launched, but no stream window appeared. "
                "Finish pairing/sign-in in the client, then press Connect again."
            )
        self._status = RemotePlayClientStatus(
            ok=False,
            mode=launch.mode,
            path=launch.path,
            pid=launch.pid,
            hwnd=ready_hwnd,
            window_title=ready_title,
            message=message,
            session_ready=False,
            session_log_path=last_session_log,
        )
        # `Session has quit` often leaves the OrionStream GUI process alive in
        # its internal retry loop.  Reap the child we launched before returning
        # the failed readiness verdict so the named input pipe cannot linger and
        # a later Connect starts from a clean generation.  This intentionally
        # bypasses the normal multi-second graceful-close path: Chiaki has
        # already declared this session ended (or never established one).
        if not self._reap_failed_start():
            self._status.message += (
                " The failed OrionStream input client did not confirm exit; "
                "disconnect Orion before retrying."
            )
        self.last_stage_timings["ready_wait_ms"] = (
            time.perf_counter() - wait_started) * 1000.0
        self._log_stage_summary("not_ready")
        return self._status

    def stage_summary(self) -> str:
        """One-line factual stage breakdown of the last ensure_running() run."""
        stages = dict(getattr(self, "last_stage_timings", {}) or {})
        if not stages:
            return ""
        # NOTE: only *_ms keys belong here (the formatter strips the suffix); the
        # bare "standby" marker is emitted by the sorted leftover loop below.
        order = ("standby_attempt_ms", "host_resolve_ms", "stale_sweep_ms",
                 "prelaunch_scan_ms", "binary_resolve_ms",
                 "probe_ms", "standby_claim_ms", "wake_check_ms", "identity_ms",
                 "spawn_ms", "standby_promote_ms", "launch_total_ms",
                 "client_boot_ms", "handshake_ms", "ready_wait_ms")
        parts = []
        for key in order:
            if key in stages:
                parts.append(f"{key[:-3]}={stages.pop(key):.0f}ms")
        for key in sorted(stages):
            value = stages[key]
            parts.append(f"{key}={value:.0f}ms" if isinstance(value, float)
                         else f"{key}={value}")
        return " ".join(parts)

    @staticmethod
    def _note_sweep(stages: Dict[str, object]) -> None:
        """Fold the last sweep's factual report into the stage summary so the next
        diagnosis reads WHAT was killed instead of inferring it from a total."""
        try:
            report = last_sweep_report()
        except Exception:
            return
        found = list(report.get("found") or [])
        killed = list(report.get("killed") or [])
        stages["sweep_found"] = ",".join(str(f) for f in found) if found else "none"
        if killed:
            stages["sweep_killed"] = ",".join(str(p) for p in killed)
        if report.get("failed"):
            stages["sweep_failed"] = ",".join(str(p) for p in report["failed"])  # type: ignore[index]
        if report.get("fallback"):
            stages["sweep_fallback"] = "taskkill"
        if found:
            logger.info("Stale-client sweep: found=%s killed=%s failed=%s fallback=%s (%.0fms)",
                        found, killed, report.get("failed"), report.get("fallback"),
                        float(report.get("ms") or 0.0))

    def _log_stage_summary(self, outcome: str) -> None:
        summary = self.stage_summary()
        if summary:
            logger.info("ensure_running stage timing (%s): %s", outcome, summary)

    def _reap_failed_start(self) -> bool:
        """Promptly stop an owned client after readiness failed.

        The manager never adopts an existing window when
        ``require_session_ready`` is enabled, so ``_process`` is the exact child
        created for this launch generation.  Cleanup is bounded and PID-scoped;
        it does not use the broad image-name sweep reserved for normal teardown.
        """
        self._failed_start_reaped = False
        proc = self._process
        self._process = None
        if proc is None:
            self._failed_start_reaped = True
            return True
        try:
            if proc.poll() is not None:
                self._failed_start_reaped = True
                return True
        except Exception:
            return False

        try:
            proc.terminate()
        except Exception as exc:
            logger.debug("Failed-start Chiaki terminate() failed: %s", exc)

        try:
            proc.wait(timeout=0.75)
            self._failed_start_reaped = True
            return True
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            logger.debug("Failed-start Chiaki reap wait failed: %s", exc)

        pid = int(getattr(proc, "pid", 0) or 0)
        if os.name == "nt" and pid > 0:
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/F", "/T"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                logger.debug("Failed-start Chiaki taskkill failed: %s", exc)
        else:
            try:
                proc.kill()
            except Exception as exc:
                logger.debug("Failed-start Chiaki kill() failed: %s", exc)

        try:
            proc.wait(timeout=0.5)
            self._failed_start_reaped = True
            return True
        except Exception:
            # Nothing else may block the sidecar's explicit failure event.  The
            # native job object remains the final process-containment boundary.
            logger.warning("Failed-start Chiaki child did not confirm exit within cleanup budget")
            return False

    # --- graceful shutdown ------------------------------------------------
    # Seconds to wait for chiaki to exit after WM_CLOSE before escalating to TerminateProcess.
    # WM_CLOSE on the Qt stream window is what drives StreamSession::Stop() ->
    # chiaki_session_stop(), i.e. the Remote Play DISCONNECT handshake with the console.
    # proc.terminate() on Windows IS TerminateProcess (identical to .kill()), and taskkill /F
    # is the same thing: both skip the handshake entirely, so the PS5 only sees the transport
    # vanish mid-session and reports it as "the LAN cable was disconnected" (user-reported bug).
    # 3s covers the observed teardown (session stop + takion goodbye + Vulkan/Qt shutdown);
    # anything slower falls through to the legacy force-kill path so stop() can never hang.
    _GRACEFUL_CLOSE_S = 3.0

    @staticmethod
    def _post_wm_close(hwnd: int) -> bool:
        """Post WM_CLOSE to the tracked stream window. Returns True when the message was
        queued (i.e. it is worth waiting for a graceful exit). Any failure — non-Windows,
        missing/stale HWND, PostMessage refused — returns False so the caller falls straight
        through to the legacy terminate path."""
        if os.name != "nt":
            return False
        try:
            hwnd = int(hwnd or 0)
        except (TypeError, ValueError):
            return False
        if not hwnd:
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                logger.debug("Chiaki graceful close skipped: hwnd 0x%X is no longer a window", hwnd)
                return False
            WM_CLOSE = 0x0010
            if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                logger.debug("Chiaki graceful close: PostMessageW(WM_CLOSE) refused (hwnd 0x%X)", hwnd)
                return False
            logger.info("Chiaki: posted WM_CLOSE to the stream window (graceful session disconnect)")
            return True
        except Exception as exc:
            logger.debug("Chiaki graceful close unavailable: %s", exc)
            return False

    def _await_window_closed(self, hwnd: int) -> bool:
        """Adopted-window case ("existing" mode): we never owned the process handle, so wait on
        the WINDOW disappearing instead of on the process. Returns True once it is gone."""
        if os.name != "nt":
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32
            deadline = time.time() + self._GRACEFUL_CLOSE_S
            while time.time() < deadline:
                if not user32.IsWindow(int(hwnd)):
                    logger.info("Chiaki stream window closed gracefully (adopted session)")
                    return True
                time.sleep(0.05)
        except Exception as exc:
            logger.debug("Chiaki graceful-close wait failed: %s", exc)
            return False
        logger.warning("Chiaki stream window still open %.1fs after WM_CLOSE; force-killing",
                       self._GRACEFUL_CLOSE_S)
        return False

    def stop(self) -> None:
        with self._process_lock:
            if self._stop_requested.is_set():
                return
            self._stop_requested.set()
            self._status.session_ready = False
            if not self._config.close_on_stop:
                return
            proc = self._process
            self._process = None
        # The stream window HWND tracked by ensure_running() (both the launched and the adopted
        # path set it). No HWND / not Windows => every branch below no-ops and behaviour is
        # exactly the legacy terminate + taskkill + terminate_chiaki_processes().
        hwnd = int(getattr(self._status, "hwnd", 0) or 0)
        if proc is not None:
            if proc.poll() is None and self._post_wm_close(hwnd):
                try:
                    proc.wait(timeout=self._GRACEFUL_CLOSE_S)
                    logger.info("Chiaki exited gracefully after WM_CLOSE "
                                "(chiaki_session_stop ran; console sees a clean disconnect)")
                except subprocess.TimeoutExpired:
                    logger.warning("Chiaki did not exit %.1fs after WM_CLOSE; escalating to "
                                   "terminate/taskkill", self._GRACEFUL_CLOSE_S)
                except Exception as exc:
                    logger.debug("Chiaki graceful-close wait failed: %s", exc)
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception as exc:
                    logger.debug("Chiaki terminate() failed: %s", exc)
                try:
                    if os.name == "nt":
                        subprocess.Popen(
                            ["taskkill.exe", "/PID", str(int(getattr(proc, "pid", 0) or 0)), "/F", "/T"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    else:
                        proc.kill()
                except Exception:
                    try:
                        proc.kill()
                    except Exception as exc:
                        logger.debug("Chiaki kill() failed: %s", exc)
        elif hwnd:
            # We adopted a window we did not launch (mode="existing"), so there is no process
            # handle to wait on — but the same graceful path applies: ask the window to close
            # and give the disconnect handshake its budget before terminate_chiaki_processes()
            # force-kills whatever is left.
            if self._post_wm_close(hwnd):
                self._await_window_closed(hwnd)
        terminate_chiaki_processes()

    @staticmethod
    def _normalize_mode(mode: str, platform: str) -> str:
        mode = str(mode or "auto").strip().lower()
        platform = str(platform or "").strip().lower()
        if mode in {"chiaki", "chiaki-ng"}:
            return "chiaki"
        return "chiaki"

    def _readiness_deadline(self, now: float) -> float:
        """Prep-anchored readiness deadline.

        The poll ends when the ORIGINAL budget (measured from ensure_running
        entry) runs out, never later than the un-anchored deadline — so stale
        sweeps, the client probe, and a rest-mode wake come out of the poll
        instead of extending the total past the native promotion deadline. The
        5s floor keeps a freshly-woken console's handshake (~2.4s) viable even
        after a long wake wait consumed most of the prep.
        """
        budget = max(3.0, float(self._config.wait_timeout_s))
        anchored = getattr(self, "_prep_started", now) + budget
        return min(now + budget, max(now + 5.0, anchored))

    def _console_failure_evidence(self) -> str:
        """One bounded re-probe so a session failure names the console's actual
        state instead of leaving the user with the generic checklist.

        Hard-capped at ~1.5s (1.0 probe + 0.5 port): it runs after the
        readiness budget already expired, inside the ~5s the native deadline
        leaves for wrap-up and the reap. Suppressed entirely for recovery
        launches (console_wake_allowed=False), whose watchdog arithmetic
        predates these probes.
        """
        if not bool(getattr(self._config, "console_wake_allowed", True)):
            return ""
        host = str(getattr(self._config, "console_ip", "") or "").strip()
        if not host:
            return ""
        try:
            import ps5_wake
            if not ps5_wake.is_ip_literal(host):
                return ""
            probe = ps5_wake.probe_console_state(host, timeout_s=1.0)
            if probe.state == "standby":
                return (" Console check: the PS5 is answering discovery but is in "
                        "REST MODE - wake it (or reconnect to let Orion wake it).")
            if probe.state == "no_answer":
                if ps5_wake.session_port_open(host, timeout_s=0.5):
                    return ""  # console is up; the failure is session-level
                return (" Console check: the PS5 answered neither discovery nor its "
                        "Remote Play port - it looks powered off, unplugged from the "
                        "network, or in rest mode with 'Stay Connected to the "
                        "Internet' disabled. Power it on at the console.")
        except Exception:
            pass
        return ""

    def _resolve_console_host(self, host: str) -> str:
        """CONSOLE ADDRESS DRIFT (2026-09-02): if the configured ip is silent but exactly one
        console answers a subnet discovery broadcast, use THAT address for this launch.

        The PS5 lives on the PC's ICS network and its DHCP lease has moved three times in
        two days (.100 -> .138 -> .126); every move ended the same way -- the input session
        to the stale ip died before it became ready while the console answered discovery
        one address over. Bounded to ~1.6s and paid ONLY when the configured host answers
        neither its session port nor discovery (a launch that would fail anyway). Two or
        more answering consoles, or none, keep the configured host: this never guesses.
        The adopted address is logged at ERROR level (the native relay never throttles that
        level) so the drift is named the moment it happens; settings.json is NOT rewritten
        here (it is signed) -- the log line tells the owner what to update.
        Fail-open: any exception means the configured host, unchanged.

        [ORION_CONNECT_LATENCY 2026-09-14] This method WAS the first-connect stall.
        A silent configured host costs 0.4s + 0.8s + 1.2s of real socket timeouts,
        and ensure_running() charged the whole thing to `stale_sweep`. It is now
        (a) separately timed as host_resolve_ms and (b) normally served from the
        background prewarm the warm preview already paid for."""
        stages = getattr(self, "last_stage_timings", None)
        _mark = time.perf_counter()

        def _stamp(value: str) -> str:
            if isinstance(stages, dict):
                stages["host_resolve_ms"] = (
                    float(stages.get("host_resolve_ms", 0.0))
                    + (time.perf_counter() - _mark) * 1000.0)
            return value

        host = str(host or "").strip()
        if not host:
            self._console_host_adopted = ""
            return host
        # One adoption serves the whole connect (standby check + cold spawn) and the next
        # ~180s of retries, so the probe is not paid twice per Connect.
        cached = getattr(self, "_console_host_adoption", None)
        if (cached and cached[0] == host
                and (time.monotonic() - float(cached[2])) < 180.0):
            self._console_host_adopted = cached[1] if cached[1] != host else ""
            return _stamp(cached[1])
        self._console_host_adopted = ""
        if str(os.environ.get("ORION_PS5_DISCOVER_DRIFT", "1")).strip().lower() in {"0", "false", "off"}:
            return _stamp(host)
        # The warm preview's prewarm pass already resolved this host in the
        # background: a connect must never pay 2.4s of socket timeouts for an
        # answer that is sitting in memory.
        prewarmed = _console_host_prewarm_get(host)
        if prewarmed:
            if prewarmed != host:
                self._console_host_adoption = (host, prewarmed, time.monotonic())
                self._console_host_adopted = prewarmed
                logger.info("CONSOLE ADDRESS DRIFT: using the prewarmed address %s "
                            "for configured %s (resolved during the warm preview)",
                            prewarmed, host)
            return _stamp(prewarmed)
        resolved = _console_host_lookup(host)
        if resolved != host:
            # Only an ADOPTION is cached (unchanged from 2026-09-02): a host that
            # answers costs ~1ms to re-verify, and caching "the configured host is
            # fine" would hide a drift that happens mid-session.
            self._console_host_adoption = (host, resolved, time.monotonic())
            self._console_host_adopted = resolved
        # Refresh an EXISTING prewarm entry only (never create one here): the
        # prewarm cache is process-wide and a connect must not seed it for a host
        # the preview never warmed.
        with _console_host_prewarm_lock:
            if host in _console_host_prewarm:
                _console_host_prewarm[host] = (resolved, time.monotonic())
        return _stamp(resolved)

    def _ensure_console_awake(self, nickname: str, host: str,
                              chiaki_path: str) -> Optional["RemotePlayClientStatus"]:
        """Wake a resting PS5 before the input client is spawned.

        Returns None to proceed with the launch, or a terminal
        RemotePlayClientStatus when launching now cannot succeed. Budgeted to
        fit inside the native side's 20s stream-promotion deadline
        (kStreamPromoteDeadlineMs): probe <=2.5s, then at most ~9s waiting for
        a woken console's session port. A console that needs longer gets an
        honest "waking - reconnect shortly" verdict instead of the generic
        session-timeout text. Fail-open: any error in the wake plumbing means
        "proceed", never a new failure mode.
        """
        self._last_console_probe = "unprobed"
        if not bool(getattr(self._config, "console_wake_allowed", True)):
            return None
        if str(os.environ.get("ORION_PS5_WAKE", "1")).strip().lower() in {"0", "false", "off"}:
            return None
        try:
            import ps5_wake
        except ImportError:
            return None
        try:
            if not ps5_wake.is_ip_literal(host):
                # A hostname would drag synchronous DNS resolution into every
                # probe send, unbounded by the probe budget. Production always
                # configures an IP literal (native discovery yields IPs).
                return None
            # FAST PATH: if the Remote Play session port already answers, the console is
            # awake and reachable -- no wake needed. A quick TCP check (~0.4s worst case,
            # instant on the common case) skips the 2.5s UDP discovery probe that is paid
            # on EVERY connect otherwise (and burns the full 2.5s when a firewall eats the
            # UDP replies). This is the dominant fixed cost of "Connect" on an awake console.
            try:
                if ps5_wake.session_port_open(host, timeout_s=0.4):
                    self._last_console_probe = "ready"
                    return None
            except Exception:
                pass
            # WAKE FIRST, ASK QUESTIONS SECOND (2026-08-29). The session port is closed, so
            # the console is resting, off, or unreachable. Previously we spent up to 2.5s on a
            # UDP discovery probe BEFORE sending the wakeup, so the console's own ~10-20s boot
            # only started ~3s after Connect. A wakeup is a no-op to an awake console and
            # inert to an off one, so send it immediately and let a SHORT probe (1.0s, in
            # parallel with the boot it just started) decide only the messaging/budget.
            regist_key, is_ps5 = ps5_wake.read_chiaki_regist_key(nickname)
            wake_sent = False
            if regist_key:
                try:
                    wake_sent = bool(ps5_wake.send_wakeup(host, regist_key, ps5=is_ps5))
                except Exception:
                    wake_sent = False
            probe = ps5_wake.probe_console_state(host, timeout_s=1.0)
            self._last_console_probe = probe.state
            if probe.state == "ready":
                return None          # awake after all (transient TCP miss) -> normal launch
            if probe.state != "standby":
                # no_answer: can't distinguish a firewall eating UDP replies from a powered-off
                # console -> keep today's control flow (launch; the failure path adds evidence).
                # The wakeup already went out above, so a resting console behind such a
                # firewall is booting by the time the user presses Connect again.
                return None
            if probe.state == "standby":
                logger.info(
                    "PS5 at %s is in rest mode - Remote Play wakeup %s", host,
                    "sent" if wake_sent else "NOT sent (no stored key)")
            if not regist_key:
                return RemotePlayClientStatus(
                    ok=False,
                    mode="chiaki",
                    path=chiaki_path,
                    message=(
                        "The PS5 is in rest mode and the stored Remote Play "
                        "wakeup key could not be read, so it cannot be woken "
                        "from here. Wake the console manually (power button or "
                        "PS button), then press Connect again."
                    ),
                )
            if not wake_sent:
                return RemotePlayClientStatus(
                    ok=False,
                    mode="chiaki",
                    path=chiaki_path,
                    message=(
                        "The PS5 is in rest mode and the Remote Play wakeup "
                        "packet could not be sent. Wake the console manually, "
                        "then press Connect again."
                    ),
                )
            wake_started = time.time()
            # Budget sized against the native promote deadline that wait_timeout_s mirrors
            # (kStreamPromoteDeadlineMs=20s -> wait_timeout_s 18s): leave ~5s for the TCP
            # check + probe already spent and the Chiaki spawn + handshake still to come.
            # Was a fixed 9s, which FAILED on any console needing longer (most do: 10-20s)
            # and cost the user a manual "press Connect again" round-trip. A no_answer probe
            # (firewall or powered off) gets a shorter wait so a dead console fails fast.
            try:
                _wt = float(getattr(self._config, "wait_timeout_s", 18.0) or 18.0)
            except Exception:
                _wt = 18.0
            port_budget = max(6.0, min(15.0, _wt - 5.0))
            _cb = getattr(self._config, "on_console_waking", None)
            if callable(_cb):
                # The host will extend its promote deadline by our budget, so cover the full
                # documented rest-mode boot (15-25s) instead of the awake-sized window.
                port_budget = 25.0
            try:
                _ov = os.environ.get("ORION_PS5_WAKE_PORT_BUDGET_S", "").strip()
                if _ov:
                    port_budget = max(3.0, min(40.0, float(_ov)))
            except Exception:
                pass
            if callable(_cb):
                try:
                    _cb(float(port_budget))
                except Exception:
                    pass
            if ps5_wake.wait_for_session_port(host, budget_s=port_budget):
                logger.info(
                    "PS5 woke and is accepting Remote Play sessions (%.1fs)",
                    time.time() - wake_started)
                self._last_console_probe = "ready"
                return None
            return RemotePlayClientStatus(
                ok=False,
                mode="chiaki",
                path=chiaki_path,
                message=(
                    "The PS5 was in rest mode - a Remote Play wakeup was sent "
                    "and the console is still waking (waited %.0fs). Press Connect "
                    "again in a moment - it connects immediately once the console "
                    "is up." % (time.time() - wake_started)
                ),
            )
        except Exception as exc:  # fail-open: wake plumbing must never block a launch
            logger.warning("Console wake check failed (proceeding to launch): %s", exc)
            return None

    @staticmethod
    def _discard_claimed_standby(client: "StandbyClient", reason: str) -> None:
        """Kill a standby ALREADY REMOVED from the pool that failed post-claim
        validation/promotion. Bounded and PID-scoped; the cold path's broad sweep
        (which runs next) remains the backstop."""
        logger.info("Claimed standby discarded (%s): pid=%s",
                    reason, getattr(client.process, "pid", 0))
        try:
            if client.process.poll() is None:
                client.process.terminate()
                try:
                    client.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    client.process.kill()
                    try:
                        client.process.wait(timeout=1.0)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Claimed-standby cleanup failed: %s", exc)

    def _promote_standby(self, stages: Dict[str, float]) -> Optional[RemotePlayClientStatus]:
        """[ORION_STANDBY 2026-08-30] Promote the pre-booted standby client instead
        of spawning one. Returns:
          - ok=True status: promotion command accepted; the caller runs the SAME
            launch-scoped session-log readiness poll a cold spawn gets (readiness
            stays proven, never assumed from the promote ack);
          - ok=False status: terminal console wake refusal (identical verdict to
            the cold path's);
          - None: no usable standby — fall back to today's spawn path.
        """
        pool = get_standby_pool()
        if pool.state() == "absent":
            return None
        _mark = time.perf_counter()
        chiaki = find_chiaki_binary(self._config.chiaki_path)
        stages["binary_resolve_ms"] = (time.perf_counter() - _mark) * 1000.0
        if not chiaki:
            return None
        _mark = time.perf_counter()
        probe = _probe_chiaki_client_cached(chiaki)
        stages["probe_ms"] = (time.perf_counter() - _mark) * 1000.0
        if not probe.client_ran or not probe.nickname:
            return None
        host = str(self._config.console_ip or "").strip()
        if not host:
            return None
        # CONSOLE ADDRESS DRIFT: the standby was pre-spawned against the CONFIGURED host,
        # but the spawn-time host is NOT binding — the fork's standby control protocol
        # takes the address on the promote command and it wins:
        #   "open [<host>] -> clears the hold, opens the session to <host> (default: the
        #    spawn-time host)"  (chiaki-ng-src/gui/src/main.cpp RunStreamStandby)
        # So a drifted console is promoted against the ADOPTED address instead of being
        # thrown away. [ORION_CONNECT_LATENCY 2026-09-14] The old bail cost every first
        # connect since 09-13 its whole standby saving (~3s of cold client boot) on this
        # rig, where settings still name .126 and the console moved to .81.
        resolved_host = self._resolve_console_host(host)
        if resolved_host and resolved_host != host:
            logger.info("Standby promote retargeted to the adopted console address %s "
                        "(configured %s is silent)", resolved_host, host)
            host = resolved_host

        _mark = time.perf_counter()
        client = pool.claim(
            nickname=probe.nickname,
            disable_video=bool(self._config.disable_video),
            executable_path=chiaki,
            identity_sha256=self._config.chiaki_identity_sha256,
            identity_size=self._config.chiaki_identity_size,
            # User connected before the standby finished booting: waiting a beat
            # is cheaper than a cold spawn, but never hang — past this budget the
            # standby is killed and the cold path runs.
            wait_ready_s=2.0,
        )
        stages["standby_claim_ms"] = (time.perf_counter() - _mark) * 1000.0
        if client is None:
            return None

        # Scoped stale check stands in for the broad taskkill sweep: a standby
        # holds NO fixed Orion pipes (the input/frame bridges are created only
        # inside a StreamSession, which a standby by construction does not have),
        # so the sweep's only remaining job is OTHER chiaki-family processes —
        # and any of those sends us to the sweeping cold path.
        running = _running_image_pids(_CHIAKI_IMAGE_NAMES)
        standby_pid = int(getattr(client.process, "pid", 0) or 0)
        if running is None or {pid for pid, _name in running} - {standby_pid}:
            self._discard_claimed_standby(
                client, "stale-check inconclusive or other chiaki processes present")
            return None

        # Launch-scoped readiness baseline: the standby has written NO session log
        # (no session ever existed), so everything on disk predates this launch.
        self._session_log_baseline = _snapshot_session_logs(self._session_log_dir)
        self._session_tracker.reset(self._session_log_baseline)

        _mark = time.perf_counter()
        wake_refusal = self._ensure_console_awake(probe.nickname, host, chiaki)
        stages["wake_check_ms"] = (time.perf_counter() - _mark) * 1000.0
        if wake_refusal is not None:
            # Terminal for THIS connect. The orchestrator's failure path sweeps
            # every chiaki-family image right after this returns, so returning the
            # unpromoted standby to the pool would only leave the pool holding a
            # soon-dead handle — discard it cleanly instead; the prewarm loop
            # re-arms a fresh standby for the retry.
            self._discard_claimed_standby(client, "console wake refused")
            return wake_refusal

        launched_path = canonical_executable_path(chiaki)
        if not launched_path:
            self._discard_claimed_standby(client, "executable identity unavailable")
            return None

        with self._process_lock:
            if self._stop_requested.is_set():
                self._discard_claimed_standby(client, "shutdown before promotion")
                return self._cancelled_status()
            self._process = client.process
        _mark = time.perf_counter()
        reply = _standby_pipe_transact(
            client.pipe_path, f"open {host}", timeout_s=2.5,
            cancel_event=self._stop_requested, write_lock=self._process_lock)
        stages["standby_promote_ms"] = (time.perf_counter() - _mark) * 1000.0
        with self._process_lock:
            if self._stop_requested.is_set():
                return self._cancelled_status()  # stop owns the detached child
            if reply != _STANDBY_OPEN_OK_REPLY:
                self._process = None
        if reply != _STANDBY_OPEN_OK_REPLY:
            self._discard_claimed_standby(client, f"open not accepted (reply={reply!r})")
            return None
        stages["standby"] = 1
        self._launch_generation_counter += 1
        self._owned_launch_generation = self._launch_generation_counter
        self._owned_launch_path = launched_path
        self._owned_creation_time_100ns = windows_process_creation_time_100ns(
            getattr(client.process, "_handle", None))
        if self._owned_creation_time_100ns <= 0:
            logger.warning(
                "Standby client creation identity unavailable; reusable decoder timing disabled")
        logger.info(
            "Standby client promoted: open %s sent to pre-booted pid=%s "
            "(client boot was paid during the warm preview)", host, standby_pid)
        return RemotePlayClientStatus(
            ok=True,
            mode="chiaki",
            path=chiaki,
            pid=standby_pid,
            message="Promoted pre-booted standby Remote Play client",
        )

    def _launch(self, mode: str) -> RemotePlayClientStatus:
        stages = getattr(self, "last_stage_timings", None)
        if stages is None:
            stages = self.last_stage_timings = {}
        _mark = time.perf_counter()
        chiaki = find_chiaki_binary(self._config.chiaki_path)
        stages["binary_resolve_ms"] = (time.perf_counter() - _mark) * 1000.0
        if not chiaki:
            return RemotePlayClientStatus(
                ok=False,
                mode="chiaki",
                message=(
                    "Chiaki/chiaki-ng was not found. Set the Chiaki executable path, "
                    "place chiaki.exe in native_orion\\deploy\\chiaki, or install it on PATH."
                ),
            )

        # Hook-enabled ensure_running() already performed this sweep before its
        # window scan so it could never adopt a stale fixed-pipe owner. Avoid a
        # second identical multi-process sweep immediately before Popen; on a
        # slow Windows host that duplicate consumed a material share of the
        # bounded console-readiness deadline.
        if not self._stale_client_cleanup_done:
            _mark = time.perf_counter()
            terminate_chiaki_processes()
            stages["stale_sweep_ms"] = (
                stages.get("stale_sweep_ms", 0.0)
                + (time.perf_counter() - _mark) * 1000.0)
            self._note_sweep(stages)
            self._stale_client_cleanup_done = True

        # Auto-discover registered console nickname via `chiaki list` so we can
        # invoke direct stream mode and bypass the Chiaki lobby/discovery UI.
        # Fingerprint-cached: a warm reconnect reuses the last successful answer
        # instead of spawning the client again (see _probe_chiaki_client_cached).
        _mark = time.perf_counter()
        probe = _probe_chiaki_client_cached(chiaki)
        stages["probe_ms"] = (time.perf_counter() - _mark) * 1000.0
        nickname = probe.nickname
        host = str(self._config.console_ip or "").strip()
        if host:
            host = self._resolve_console_host(host)   # CONSOLE ADDRESS DRIFT, see the method

        # [ORION_CLIENT_LAUNCHABILITY 2026-08-13] Fail HERE, loudly, when the client
        # itself will not run. Falling through to the lobby in this state cannot work
        # — the lobby is the same executable — and it costs the caller its whole
        # readiness deadline before reporting a registration problem that does not
        # exist. Scoped deliberately narrow: only the unambiguous "never reached
        # main()" exits set client_ran=False, so a slow or chatty client keeps the
        # historical fallback.
        if not probe.client_ran:
            logger.error("Remote Play client is not runnable: %s", probe.detail)
            return RemotePlayClientStatus(
                ok=False,
                mode="chiaki",
                path=chiaki,
                message=(
                    f"{probe.detail}. This is a broken client install, not a console "
                    f"or network problem — the console registration is untouched. "
                    f"Reinstall Venice, or restore a known-good OrionStream.exe in "
                    f"{os.path.dirname(chiaki) or chiaki}."
                ),
            )

        if nickname and host:
            # CLI direct-stream mode never wakes a sleeping console (chiaki-ng's
            # `stream` path skips discovery entirely; only the GUI click path
            # sends WAKEUP). Detect standby here and wake it BEFORE spawning the
            # client, or every Connect against a resting PS5 dies ~5s in with
            # "Session request connect failed: Timeout".
            _mark = time.perf_counter()
            wake_refusal = self._ensure_console_awake(nickname, host, chiaki)
            stages["wake_check_ms"] = (time.perf_counter() - _mark) * 1000.0
            if wake_refusal is not None:
                return wake_refusal
            # IMPORTANT: chiaki-ng uses QCommandLineParser::ParseAsPositionalArguments mode,
            # which means any options PLACED AFTER the positional args (`stream nickname host`)
            # are treated as additional positional args. Putting `--fullscreen` at the end
            # makes chiaki read host = "--fullscreen" => "Failed to parse host address".
            # Therefore: any options must come BEFORE `stream`. We launch in normal windowed
            # mode (no --fullscreen) so the launcher can embed/capture the chiaki window.
            cmd = [chiaki, "stream", nickname, host]
            logger.info("Launching Chiaki in direct stream mode: %s -> %s", nickname, host)
            return self._launch_process(cmd, "chiaki", chiaki)

        # Fallback: launch Chiaki's lobby UI so the user can pair/select manually.
        if not nickname:
            logger.warning("No registered Chiaki nickname found; opening Chiaki lobby for manual selection.")
        elif not host:
            logger.warning("No console IP set; opening Chiaki lobby.")
        return self._launch_process([chiaki], "chiaki", chiaki)

    def _launch_process(self, cmd: List[str], mode: str, path: str) -> RemotePlayClientStatus:
        try:
            # IMPORTANT: do not pass CREATE_NO_WINDOW for Chiaki, and do NOT redirect
            # stdout/stderr to DEVNULL — Chiaki uses these handles for its diagnostic
            # output and some builds refuse to initialize without them. We also want
            # the stream window to appear normally so the launcher can embed it.
            launch_env = chiaki_controller_env(
                disable_video=bool(self._config.disable_video))

            # The OrionStream (chiaki-ng) window is later reparented/parked off-screen
            # by the native watchdog (OrionAppController), but that adoption happens
            # a beat after launch, which leaves a race where the window floats visibly
            # on the desktop/taskbar. Suppress the initial show so it never appears as
            # a top-level window in the first place; this only affects window
            # visibility, not the process itself (decode/capture/input are unaffected).
            # SW_HIDE=0 (numeric constant, no pywin32 dependency).
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            def _spawn():
                with self._process_lock:
                    if self._stop_requested.is_set():
                        raise RuntimeError("client launch cancelled by shutdown")
                    self._process = subprocess.Popen(
                        cmd,
                        cwd=os.path.dirname(path) if os.path.isfile(path) else None,
                        env=launch_env,
                        startupinfo=startupinfo,
                    )

            expected_sha = str(
                self._config.chiaki_identity_sha256 or "").strip().lower()
            try:
                expected_size = int(self._config.chiaki_identity_size)
            except (TypeError, ValueError, OverflowError):
                expected_size = -1
            stages = getattr(self, "last_stage_timings", None)
            if stages is None:
                stages = self.last_stage_timings = {}
            _mark = time.perf_counter()
            identity_requested = bool(expected_sha or expected_size >= 0)
            if identity_requested:
                if (expected_size < 0 or len(expected_sha) != 64
                        or any(ch not in "0123456789abcdef" for ch in expected_sha)):
                    return RemotePlayClientStatus(
                        ok=False, mode=mode, path=path,
                        message=f"Failed to start {mode}: malformed executable identity")
                with locked_executable_snapshot(path) as snapshot:
                    if (not snapshot.valid or snapshot.size != expected_size
                            or snapshot.sha256 != expected_sha):
                        return RemotePlayClientStatus(
                            ok=False, mode=mode, path=path,
                            message=f"Failed to start {mode}: executable identity mismatch")
                    stages["identity_ms"] = (time.perf_counter() - _mark) * 1000.0
                    _mark = time.perf_counter()
                    # Identity (size + SHA-256) is verified above and re-checked
                    # against the running producer at pipe-bind time; the former
                    # deny-write file lock across this Popen was removed as it was
                    # fragile in practice (AV/concurrent readers tripped it).
                    _spawn()
            else:
                # Developer/test compatibility is cold-only: absence of a native
                # expectation prevents decoder cache scope elsewhere.
                _spawn()
            stages["spawn_ms"] = (time.perf_counter() - _mark) * 1000.0
            launched_path = canonical_executable_path(path)
            if not launched_path:
                # The child may already exist, but an unnameable executable can
                # never become the trusted decoder producer.  Keep the process for
                # normal bounded cleanup while returning a launch failure.
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process = None
                return RemotePlayClientStatus(
                    ok=False, mode=mode, path=path,
                    message=f"Failed to start {mode}: executable identity unavailable")
            self._launch_generation_counter += 1
            self._owned_launch_generation = self._launch_generation_counter
            self._owned_launch_path = launched_path
            self._owned_creation_time_100ns = windows_process_creation_time_100ns(
                getattr(self._process, "_handle", None))
            if self._owned_creation_time_100ns <= 0:
                # The video/input child remains usable.  The pipe identity gate
                # sees the explicit missing creation stamp and permanently keeps
                # reusable timing cold for this sidecar process.
                logger.warning(
                    "Chiaki process creation identity unavailable; reusable decoder timing disabled")
            # Isolation proof: record the SDL ignore-device vars actually passed to the
            # Chiaki child (env= is authoritative for the child's environment) alongside
            # its PID, so a routing-leak investigation can confirm the no-cloak path.
            logger.info(
                "Chiaki isolation env (pid=%s): IGNORE_DEVICES=%s HIDAPI_PS5=%s HIDAPI_PS4=%s",
                int(getattr(self._process, "pid", 0) or 0),
                launch_env.get("SDL_GAMECONTROLLER_IGNORE_DEVICES"),
                launch_env.get("SDL_JOYSTICK_HIDAPI_PS5"),
                launch_env.get("SDL_JOYSTICK_HIDAPI_PS4"),
            )
            # When current-session readiness is required, input is deliberately
            # unauthorized until the fresh stream marker arrives.  Raising the
            # just-spawned process to HIGH_PRIORITY_CLASS before that marker
            # caused the already-live capture preview to miss presentation
            # beats during promotion.  The readiness branch above raises it as
            # soon as the console route is real.  Legacy/window-only launches
            # retain their historical immediate optimization.
            if not self._config.require_session_ready:
                optimize_chiaki_process(int(getattr(self._process, "pid", 0) or 0))
            return RemotePlayClientStatus(
                ok=True,
                mode=mode,
                path=path,
                pid=int(getattr(self._process, "pid", 0) or 0),
                message=f"Started {mode}",
            )
        except Exception as exc:
            return RemotePlayClientStatus(ok=False, mode=mode, path=path, message=f"Failed to start {mode}: {exc}")


class ChiakiClientProbe(NamedTuple):
    """Result of the pre-launch `OrionStream list` probe.

    `nickname` is the first registered console, "" if none.
    `client_ran` is False ONLY when the client could not execute at all (loader
    failure / not spawnable) — i.e. when an empty nickname says nothing about
    whether a console is registered.
    `detail` is the human-readable reason, empty when the probe was clean.
    """

    nickname: str
    client_ran: bool
    detail: str


# Windows NTSTATUS exit codes that mean "the process never reached main()", mapped to
# what the operator can actually do about them. A client that dies in the loader writes
# nothing to stdout, which is indistinguishable from a clean "no console registered"
# unless the exit code is read — see _probe_chiaki_client.
_NTSTATUS_LAUNCH_FAILURES = {
    0xC0000135: "a DLL it needs is missing from the client folder (STATUS_DLL_NOT_FOUND)",
    0xC0000139: "a DLL it needs is the wrong version (STATUS_ENTRYPOINT_NOT_FOUND)",
    0xC0000142: "a DLL failed to initialize (STATUS_DLL_INIT_FAILED)",
    0xC000007B: "a 32/64-bit mismatch in its DLLs (STATUS_INVALID_IMAGE_FORMAT)",
    0xC0000005: "an access violation during start-up (STATUS_ACCESS_VIOLATION)",
    0xC0000409: "a stack buffer overrun during start-up (STATUS_STACK_BUFFER_OVERRUN)",
}


def _probe_chiaki_client(chiaki_path: str) -> ChiakiClientProbe:
    """Run `chiaki list` and report BOTH the registered nickname and whether the
    client is runnable at all.

    Output format (chiaki-ng):
        Host: <nickname>

    WHY THIS RETURNS client_ran (2026-08-13): this used to return a bare string, and
    every failure — including "the executable died in the Windows loader" — collapsed
    into "". The caller read that as "no console registered", logged exactly that, and
    fell back to the Chiaki lobby, which then burned the full readiness deadline and
    reported a registration problem. That is what happened on this rig: a rebuilt
    OrionStream.exe linked against FFmpeg 8 while the deploy folder shipped FFmpeg 7,
    so it exited 0xC0000135 before main() with empty stdout. The registration was
    intact the whole time; the log said it was missing. An unrunnable client and an
    unregistered console need different fixes, so they must not share a message.
    """
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [chiaki_path, "list"],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=flags if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        # It STARTED and then hung. Deliberately still "ran": a slow machine must keep
        # its historical lobby fallback rather than being newly hard-blocked here.
        logger.warning("chiaki list timed out after 4s; falling back to lobby launch.")
        return ChiakiClientProbe("", True, "the client did not answer within 4 seconds")
    except OSError as exc:
        return ChiakiClientProbe("", False, f"the client could not be started ({exc})")
    except Exception as exc:  # noqa: BLE001 - probe must never take the caller down
        logger.debug("chiaki list failed: %s", exc)
        return ChiakiClientProbe("", True, f"the client probe failed ({exc})")

    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.lower().startswith("host:"):
            nickname = line.split(":", 1)[1].strip()
            if nickname:
                return ChiakiClientProbe(nickname, True, "")

    rc = int(result.returncode or 0)
    status = rc & 0xFFFFFFFF
    if status in _NTSTATUS_LAUNCH_FAILURES:
        return ChiakiClientProbe(
            "", False,
            f"the Remote Play client cannot start: {_NTSTATUS_LAUNCH_FAILURES[status]}"
            f" [exit 0x{status:08X}]")
    if status >= 0xC0000000:
        # Unmapped NTSTATUS. Still unambiguously a crash, not a clean empty list.
        return ChiakiClientProbe(
            "", False,
            f"the Remote Play client crashed during start-up [exit 0x{status:08X}]")

    # Exited normally with no Host: line — genuinely no registered console.
    return ChiakiClientProbe("", True, "")


def _registered_hosts_stamp() -> int:
    """Change stamp for chiaki's registered-hosts store (HKCU registry).

    Max LastWriteTime (100ns FILETIME) across the registered_hosts key and its
    per-host subkeys, so registering/re-registering ANY console invalidates a
    cached nickname probe. -1 when unreadable (still a stable cache key: the
    probe cache only compares stamps for equality).
    """
    if os.name != "nt":
        return 0
    try:
        import winreg

        base = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Chiaki\Chiaki\registered_hosts")
        try:
            sub_count, _values, stamp = winreg.QueryInfoKey(base)
            stamp = int(stamp)
            for index in range(int(sub_count)):
                try:
                    sub = winreg.OpenKey(base, winreg.EnumKey(base, index))
                except OSError:
                    continue
                try:
                    stamp = max(stamp, int(winreg.QueryInfoKey(sub)[2]))
                finally:
                    winreg.CloseKey(sub)
            return stamp
        finally:
            winreg.CloseKey(base)
    except OSError:
        return -1
    except Exception:
        return -1


def _client_probe_fingerprint(chiaki_path: str) -> Optional[tuple]:
    """Cache key binding a probe result to the exact client install + registration state.

    Covers the exe (path/size/mtime), the DLL set beside it (count/total size/max
    mtime — the 2026-08-13 loader-failure incident was a stale DLL beside a fresh
    exe), and the registered-hosts registry stamp (a new/changed registration must
    re-probe so the nickname stays chiaki's own answer). None disables caching.
    """
    try:
        st = os.stat(chiaki_path)
        dll_count = 0
        dll_size = 0
        dll_mtime = 0
        with os.scandir(os.path.dirname(os.path.abspath(chiaki_path))) as entries:
            for entry in entries:
                if entry.name.lower().endswith(".dll") and entry.is_file():
                    es = entry.stat()
                    dll_count += 1
                    dll_size += int(es.st_size)
                    dll_mtime = max(dll_mtime, int(es.st_mtime_ns))
        return (
            os.path.normcase(os.path.abspath(chiaki_path)),
            int(st.st_size),
            int(st.st_mtime_ns),
            dll_count,
            dll_size,
            dll_mtime,
            _registered_hosts_stamp(),
        )
    except OSError:
        return None


_PROBE_CACHE: Dict[tuple, ChiakiClientProbe] = {}
_PROBE_CACHE_MAX = 8


def _probe_chiaki_client_cached(chiaki_path: str) -> ChiakiClientProbe:
    """`chiaki list` probe with a fingerprint-keyed success cache.

    [ORION_CONNECT_LATENCY 2026-08-29] The probe spawns the full client once per
    connect (~75ms idle, more under live capture/detector load) to answer two
    stable questions: the registered nickname and whether the client can run at
    all. Both answers are functions of the client install + registration state,
    so a SUCCESSFUL probe (client ran AND a nickname was found) is reused while
    the fingerprint matches. Failures and empty nicknames are never cached —
    those drive fail-fast messaging / the lobby fallback, where re-probing is
    the point. Monkeypatched tests keep working: the real probe is looked up on
    the module at call time and unknown paths never fingerprint.
    """
    fingerprint = _client_probe_fingerprint(chiaki_path)
    if fingerprint is not None:
        cached = _PROBE_CACHE.get(fingerprint)
        if cached is not None and cached.client_ran and cached.nickname:
            return cached
    probe = _probe_chiaki_client(chiaki_path)
    if fingerprint is not None and probe.client_ran and probe.nickname:
        if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[fingerprint] = probe
    return probe


def _detect_chiaki_nickname(chiaki_path: str) -> str:
    """Back-compat shim: nickname only. Prefer _probe_chiaki_client, which also says
    whether an empty result means 'nothing registered' or 'client will not run'."""
    return _probe_chiaki_client(chiaki_path).nickname
