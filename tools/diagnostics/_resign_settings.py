"""Reproduce SecurityManager::machineId() + settingsDigest() exactly and
(optionally) rewrite settings.json.sig. Verifies the reconstruction against
Qt's own QSysInfo values before trusting it.

machineId = sha256( hostName | MachineGuid | MachineGuid ).hexdigest()   (left 64)
  hostName    = GetComputerNameExW(ComputerNamePhysicalDnsHostname)
  MachineGuid = HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (64-bit view)
settingsDigest = sha256( b"orion-settings-v1|" + machineId + b"|" + <settings bytes> )
settings.json.sig = digest.hex() + "\n"
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import sys
import winreg
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ComputerNamePhysicalDnsHostname = 5


def host_name():
    buf = ctypes.create_unicode_buffer(256)
    size = wt.DWORD(256)
    ok = ctypes.windll.kernel32.GetComputerNameExW(
        ComputerNamePhysicalDnsHostname, buf, ctypes.byref(size))
    if not ok:
        raise OSError("GetComputerNameExW failed")
    return buf.value  # value stops at the null, == QString::fromWCharArray result


def machine_guid():
    with winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE,
                          r"SOFTWARE\Microsoft\Cryptography", 0,
                          winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
        val, _ = winreg.QueryValueEx(k, "MachineGuid")
    return str(val).strip()


def qt_machine_id(guid):
    """Authoritative parity check using the same Qt API as SecurityManager.

    Refuse to sign when PySide is unavailable: a stale hardcoded suffix allowed
    this helper to become unusable after a legitimate Windows machine-ID change,
    while blindly removing the check would make a reproduction bug sign the
    wrong digest. The repository venv already carries PySide6 for local tooling.
    """
    try:
        from PySide6.QtCore import QSysInfo
    except ImportError:
        return None
    qt_host = QSysInfo.machineHostName()
    qt_unique = bytes(QSysInfo.machineUniqueId())
    material = (qt_host.encode("utf-8") + b"|" + qt_unique + b"|" +
                guid.encode("utf-8"))
    return hashlib.sha256(material).hexdigest()[:64]


def main():
    hn = host_name()
    guid = machine_guid()
    material = hn.encode("utf-8") + b"|" + guid.encode("utf-8") + b"|" + guid.encode("utf-8")
    mid = hashlib.sha256(material).hexdigest()[:64]
    qt_mid = qt_machine_id(guid)
    # MachineGuid and the full derived machine id are stable device identifiers.  Keep them
    # out of terminals and captured support logs; the suffix is sufficient to verify parity
    # with SecurityManager before writing a signature.
    print(f"hostName present = {bool(hn)}")
    print(f"MachineGuid present = {bool(guid)}")
    print(f"machine suffix = {mid[-8:]}")
    print(f"Qt parity available = {qt_mid is not None}   match = {mid == qt_mid}")

    settings_path = REPO / "settings.json"
    with open(settings_path, "rb") as f:
        data = f.read()
    digest_material = b"orion-settings-v1|" + mid.encode("utf-8") + b"|" + data
    digest = hashlib.sha256(digest_material).hexdigest()
    print(f"settings bytes = {len(data)}")
    print(f"settingsDigest = {digest}")

    if qt_mid is None or mid != qt_mid:
        print("\n*** QSysInfo machineId MISMATCH — NOT writing sig. Reproduction is wrong. ***")
        return 2

    if "--write" in sys.argv:
        sig_path = REPO / "settings.json.sig"
        with open(sig_path, "wb") as f:
            f.write(digest.encode("ascii") + b"\n")
        print(f"\nWROTE {sig_path}")
    else:
        print("\n(dry run — pass --write to rewrite settings.json.sig)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
