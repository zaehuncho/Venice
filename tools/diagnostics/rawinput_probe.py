#!/usr/bin/env python3
"""
RawInput / Sony HID probe for the Orion native input path.

Mirrors what native OrionAppController does:
  - GetRawInputDeviceList -> for each HID, read usUsagePage/usUsage + device name
    (the app keeps only usUsagePage==0x01 and usUsage in {0x04 gamepad, 0x05 joystick})
  - classify Sony by VID_054C + PID (matches classifyRawInputController)
  - then opens each Sony HID collection and reads a few INPUT reports to capture the
    actual report id / length / first bytes (validates decodeSonyReport offsets:
    USB DualSense id 0x01 axis@1 face@8 ; BT id 0x31 axis@2 face@9).

No admin needed. Run:  python tools/diagnostics/rawinput_probe.py
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

RIM_TYPEHID = 2
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WAIT_OBJECT_0 = 0x0
ERROR_IO_PENDING = 997


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [
        ("dwVendorId", wintypes.DWORD),
        ("dwProductId", wintypes.DWORD),
        ("dwVersionNumber", wintypes.DWORD),
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
    ]


class RID_DEVICE_INFO_KEYBOARD(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSubType", wintypes.DWORD),
                ("dwKeyboardMode", wintypes.DWORD), ("dwNumberOfFunctionKeys", wintypes.DWORD),
                ("dwNumberOfIndicators", wintypes.DWORD), ("dwNumberOfKeysTotal", wintypes.DWORD)]


class _RID_INFO_U(ctypes.Union):
    _fields_ = [("hid", RID_DEVICE_INFO_HID), ("kbd", RID_DEVICE_INFO_KEYBOARD)]


class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwType", wintypes.DWORD), ("u", _RID_INFO_U)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


# --- 64-bit-safe signatures (HANDLE args MUST be pointer-sized) ---
user32.GetRawInputDeviceList.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputDeviceList.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.CancelIo.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def device_name(h):
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, buf, ctypes.byref(size)) == 0xFFFFFFFF:
        return ""
    return buf.value


def device_info(h):
    info = RID_DEVICE_INFO()
    info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
    size = wintypes.UINT(ctypes.sizeof(RID_DEVICE_INFO))
    r = user32.GetRawInputDeviceInfoW(h, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(size))
    if r == 0xFFFFFFFF or r == 0:
        return None
    return info


def read_one_report(path, want_bytes=78, timeout_ms=1500):
    h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                             FILE_FLAG_OVERLAPPED, None)
    if not h or h == INVALID_HANDLE_VALUE:
        h = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if not h or h == INVALID_HANDLE_VALUE:
        return None, f"CreateFile failed err={ctypes.get_last_error()}"
    try:
        ev = kernel32.CreateEventW(None, True, False, None)
        ov = OVERLAPPED()
        ov.hEvent = ev
        buf = ctypes.create_string_buffer(want_bytes)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(h, buf, want_bytes, ctypes.byref(read), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                return None, f"ReadFile err={err}"
            if kernel32.WaitForSingleObject(ev, timeout_ms) != WAIT_OBJECT_0:
                kernel32.CancelIo(h)
                return None, "no report within timeout (device idle or grabbed)"
            kernel32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(read), False)
        return buf.raw[:read.value], None
    finally:
        kernel32.CloseHandle(h)


def main():
    count = wintypes.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if count.value == 0:
        print("No raw input devices.")
        return
    arr = (RAWINPUTDEVICELIST * count.value)()
    got = user32.GetRawInputDeviceList(arr, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if got == 0xFFFFFFFF:
        print(f"GetRawInputDeviceList failed err={ctypes.get_last_error()}")
        return

    print(f"== {got} raw input devices (all HID listed) ==")
    sony_pass = []
    for d in arr[:got]:
        if d.dwType != RIM_TYPEHID:
            continue
        info = device_info(d.hDevice)
        name = device_name(d.hDevice)
        if not info:
            print(f"  [HID] info FAILED  {name}")
            continue
        hid = info.u.hid
        is_sony = (hid.dwVendorId == 0x054C)
        passes = (hid.usUsagePage == 0x01 and hid.usUsage in (0x04, 0x05))
        tag = "SONY " if is_sony else "     "
        gate = "PASS" if passes else "skip"
        print(f"  [{tag}{gate}] VID={hid.dwVendorId:04X} PID={hid.dwProductId:04X} "
              f"page={hid.usUsagePage:#04x} usage={hid.usUsage:#04x}")
        print(f"            {name}")
        if is_sony and passes:
            sony_pass.append(name)

    if not sony_pass:
        print("\n>>> NO Sony device passes the app filter (page 0x01, usage 0x04/0x05).")
        print(">>> findRawInputController finds nothing -> 'no live reports'.")
        return

    print(f"\n== reading input reports from {len(sony_pass)} Sony collection(s) ==")
    for name in sony_pass:
        data, err = read_one_report(name)
        if err:
            print(f"[{name}]\n    READ ERROR: {err}")
            continue
        rid = data[0] if data else -1
        hexs = " ".join(f"{b:02x}" for b in data[:20])
        print(f"[{name}]\n    len={len(data)} reportId=0x{rid:02x}  first20={hexs}")
        if rid == 0x01:
            print("    -> USB layout (decodeSonyReport axis@1 face@8 l2@5 r2@6) EXPECTED")
        elif rid == 0x31:
            print("    -> BT layout (decodeSonyReport axis@2 face@9 l2@6 r2@7) EXPECTED")
        else:
            print(f"    -> UNEXPECTED reportId 0x{rid:02x}; decodeSonyReport offsets likely wrong")


if __name__ == "__main__":
    main()
