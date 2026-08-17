from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

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
        self.reset(baseline or {})

    def reset(self, baseline: Dict[str, int]) -> None:
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


def terminate_chiaki_processes() -> None:
    if os.name != "nt":
        return
    names = ("OrionStream.exe", "chiaki.exe", "chiaki-ng.exe", "chiaki4deck.exe")
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    deadline = time.time() + 6.0
    while True:
        for name in names:
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
        PROCESS_SET_INFORMATION = 0x0200
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        HIGH_PRIORITY_CLASS = 0x00000080
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            return
        try:
            ctypes.windll.kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS)
            # Disable dynamic priority boost changes for steadier frame cadence.
            ctypes.windll.kernel32.SetProcessPriorityBoost(handle, True)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as exc:
        logger.debug("Chiaki process optimization failed: %s", exc)


class RemotePlayClientManager:
    def __init__(self, config: Optional[RemotePlayClientConfig] = None) -> None:
        self._config = config or RemotePlayClientConfig()
        self._process: Optional[subprocess.Popen] = None
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
        if not self.is_running() or not self._status.ok:
            return False
        if not self._config.require_session_ready:
            return True
        state, path = self._session_tracker.poll()
        if state != "ready":
            self._status.session_ready = False
            if path:
                self._status.session_log_path = path
            return False
        if path:
            self._status.session_log_path = path
        return True

    def ensure_running(self) -> RemotePlayClientStatus:
        self._failed_start_reaped = False
        self._stale_client_cleanup_done = False
        # The Orion input/frame hooks use fixed named pipes. A stale OrionStream
        # process can keep those server pipes open after a watchdog restart; if we
        # "adopt" that window, the fresh stream process cannot create its bridges
        # (ERROR_PIPE_BUSY) and both detection and input hook silently degrade.
        if self._config.close_on_stop and (os.environ.get("ORION_INPUT_HOOK") or os.environ.get("ORION_FRAME_PIPE")):
            terminate_chiaki_processes()
            self._stale_client_cleanup_done = True

        # Snapshot AFTER stale-child cleanup.  Any old success marker, including
        # final shutdown bytes written during cleanup, is outside this launch's
        # authority window.
        self._session_log_baseline = _snapshot_session_logs(self._session_log_dir)
        self._session_tracker.reset(self._session_log_baseline)

        hwnd, title = find_remote_play_window(self._config.window_title)
        if hwnd and not self._config.require_session_ready:
            self._status = RemotePlayClientStatus(
                ok=True,
                mode="existing",
                hwnd=hwnd,
                window_title=title,
                message="Remote Play window already running",
                session_ready=True,
            )
            return self._status

        mode = self._normalize_mode(self._config.client_mode, self._config.platform)
        launch = self._launch(mode)
        if not launch.ok:
            self._status = launch
            return launch

        deadline = time.time() + max(3.0, float(self._config.wait_timeout_s))
        ready_hwnd = 0
        ready_title = ""
        last_session_state = "waiting"
        last_session_log = ""
        while time.time() < deadline:
            hwnd, title = find_remote_play_window(self._config.window_title)
            if hwnd:
                ready_hwnd, ready_title = hwnd, title
            if self._config.require_session_ready:
                last_session_state, last_session_log = self._session_tracker.poll()
                if last_session_state == "ready":
                    # The controller route is not authorized before this fresh
                    # marker.  Keep the new child at Windows' normal priority
                    # during process/decoder startup so it cannot pre-empt the
                    # already-live capture-card/QML preview, then apply Orion's
                    # streaming priority policy exactly when input becomes live.
                    optimize_chiaki_process(int(getattr(self._process, "pid", 0) or 0))
                    self._status = RemotePlayClientStatus(
                        ok=True,
                        mode=launch.mode,
                        path=launch.path,
                        pid=launch.pid,
                        hwnd=ready_hwnd,
                        window_title=ready_title,
                        message="Remote Play console input session ready",
                        session_ready=True,
                        session_log_path=last_session_log,
                    )
                    return self._status
                if last_session_state == "ended":
                    # A launch-scoped disconnect/quit before streaminfo is a
                    # definitive console-session failure, not a reason to keep
                    # displaying Connecting for the remainder of the configured
                    # connection budget. OrionStream may keep its Qt process alive
                    # and retry every five seconds, but none of those retries is
                    # authorized to hold the controller route open.
                    break
            elif ready_hwnd:
                self._status = RemotePlayClientStatus(
                    ok=True,
                    mode=launch.mode,
                    path=launch.path,
                    pid=launch.pid,
                    hwnd=ready_hwnd,
                    window_title=ready_title,
                    message="Remote Play client window ready",
                    session_ready=True,
                )
                return self._status
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
            time.sleep(0.025)

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
        return self._status

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

    def _launch(self, mode: str) -> RemotePlayClientStatus:
        chiaki = find_chiaki_binary(self._config.chiaki_path)
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
            terminate_chiaki_processes()
            self._stale_client_cleanup_done = True

        # Auto-discover registered console nickname via `chiaki list` so we can
        # invoke direct stream mode and bypass the Chiaki lobby/discovery UI.
        probe = _probe_chiaki_client(chiaki)
        nickname = probe.nickname
        host = str(self._config.console_ip or "").strip()

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
                return subprocess.Popen(
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
                    # Identity (size + SHA-256) is verified above and re-checked
                    # against the running producer at pipe-bind time; the former
                    # deny-write file lock across this Popen was removed as it was
                    # fragile in practice (AV/concurrent readers tripped it).
                    self._process = _spawn()
            else:
                # Developer/test compatibility is cold-only: absence of a native
                # expectation prevents decoder cache scope elsewhere.
                self._process = _spawn()
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


def _detect_chiaki_nickname(chiaki_path: str) -> str:
    """Back-compat shim: nickname only. Prefer _probe_chiaki_client, which also says
    whether an empty result means 'nothing registered' or 'client will not run'."""
    return _probe_chiaki_client(chiaki_path).nickname
