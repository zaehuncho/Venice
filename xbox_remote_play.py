"""Attach to an explicitly selected Microsoft Remote Play window; never own its session.

No console credentials, protocol implementation, browser injection, or process launch/kill.
Microsoft's client owns login/transport. Venice owns only WGC capture and its virtual pad.
"""
from dataclasses import dataclass
from pathlib import PureWindowsPath


_PROCESSES = frozenset(("msedge.exe", "chrome.exe", "firefox.exe", "xboxpcapp.exe",
                        "xboxapp.exe", "xboxgame streaming.exe", "applicationframehost.exe"))


@dataclass(frozen=True)
class XboxWindow:
    hwnd: int
    pid: int
    title: str
    process: str


def is_xbox_window(title: str, process: str) -> bool:
    name = PureWindowsPath(process).name.casefold()
    return name in _PROCESSES and "xbox" in str(title).casefold()


def enumerate_xbox_windows():
    """Visible top-level Xbox client/browser windows, with a pinned process identity."""
    import ctypes
    from ctypes import wintypes
    import win32gui
    import win32process
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    found = []
    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if "xbox" not in title.casefold():
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if is_xbox_window(title, buf.value):
                    found.append(XboxWindow(int(hwnd), int(pid), title, buf.value))
        finally:
            kernel.CloseHandle(handle)
    win32gui.EnumWindows(visit, None)
    return found


def select_xbox_window(title: str, candidates=None) -> XboxWindow:
    title = str(title or "").strip()
    if not title:
        raise ValueError("Select the Xbox Remote Play window in Setup before connecting.")
    candidates = enumerate_xbox_windows() if candidates is None else candidates
    matches = [w for w in candidates if w.title == title and is_xbox_window(w.title, w.process)]
    if len(matches) != 1:
        raise ValueError("Xbox window is missing or ambiguous. Open Remote Play, keep its window visible, then select it in Setup.")
    return matches[0]


def xbox_window_still_matches(window: XboxWindow) -> bool:
    try:
        import win32gui
        import win32process
        # Pin the existing target, not a fresh enumeration on every 60 Hz callback.
        # WGC's on_closed retires the capture; a reused HWND/PID alone cannot reopen it.
        return (win32gui.IsWindow(window.hwnd) and win32gui.IsWindowVisible(window.hwnd)
                and not win32gui.IsIconic(window.hwnd)
                and win32gui.GetWindowText(window.hwnd) == window.title
                and win32process.GetWindowThreadProcessId(window.hwnd)[1] == window.pid)
    except Exception:
        return False
