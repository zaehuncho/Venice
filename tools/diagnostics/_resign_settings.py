"""Reproduce SecurityManager::machineId() + settingsDigest() exactly and
(optionally) rewrite settings.json.sig. Verifies against the logged
machine_id_suffix before trusting the reproduction.

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
EXPECT_SUFFIX = "6f4bd59e"  # from orion_native.log "machine_id_suffix="

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


def main():
    hn = host_name()
    guid = machine_guid()
    material = hn.encode("utf-8") + b"|" + guid.encode("utf-8") + b"|" + guid.encode("utf-8")
    mid = hashlib.sha256(material).hexdigest()[:64]
    # MachineGuid and the full derived machine id are stable device identifiers.  Keep them
    # out of terminals and captured support logs; the suffix is sufficient to verify parity
    # with SecurityManager before writing a signature.
    print(f"hostName present = {bool(hn)}")
    print(f"MachineGuid present = {bool(guid)}")
    print(f"machine suffix = {mid[-8:]}   expected = {EXPECT_SUFFIX}   match = {mid[-8:] == EXPECT_SUFFIX}")

    settings_path = REPO / "settings.json"
    with open(settings_path, "rb") as f:
        data = f.read()
    digest_material = b"orion-settings-v1|" + mid.encode("utf-8") + b"|" + data
    digest = hashlib.sha256(digest_material).hexdigest()
    print(f"settings bytes = {len(data)}")
    print(f"settingsDigest = {digest}")

    if mid[-8:] != EXPECT_SUFFIX:
        print("\n*** machineId suffix MISMATCH — NOT writing sig. Reproduction is wrong. ***")
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
