"""Installer contract for the RETIRED packet bridge (2026-09-24, owner).

The owner removed the packet bridge (VeniceNetSvc, a LocalSystem service, plus the
WinDivert kernel driver) from the beta: meter delay is shelved and no packet-level
driver ships. The installer must

* never register the bridge again (no sc create, no arm switch, no DACL),
* retire any bridge an earlier build registered (NexusVisionSvc and VeniceNetSvc),
  on install and on uninstall,
* purge the old packet_bridge folder on upgrade,

and the build script and release policy must refuse a package that still carries it.
These are string-level contracts on installer/orion.iss and its build script; the
compiled-script read-back in build_installer.ps1 enforces the same on the rig.
"""
import importlib.util
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ISS = (_ROOT / "installer" / "orion.iss").read_text(encoding="utf-8", errors="replace")
_BUILD_PS1 = (_ROOT / "installer" / "build_installer.ps1").read_text(
    encoding="utf-8", errors="replace"
)


def _policy():
    spec = importlib.util.spec_from_file_location(
        "release_filter_policy", _ROOT / "tools" / "release_filter_policy.py"
    )
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


def test_installer_never_registers_a_packet_bridge():
    for forbidden in ("create VeniceNetSvc", "create NexusVisionSvc", "--arm-meter-delay",
                      "ORION_METER_DELAY_ARMED", "sdset VeniceNetSvc", "RegisterPacketBridgeService"):
        assert forbidden not in _ISS, forbidden


def test_install_retires_both_legacy_bridge_services():
    m = re.search(r"procedure RetireLegacyPacketBridge;.*?\nend;", _ISS, flags=re.DOTALL)
    assert m, "orion.iss lost RetireLegacyPacketBridge"
    assert "RemoveBridgeService('NexusVisionSvc')" in m.group(0)
    assert "RemoveBridgeService('VeniceNetSvc')" in m.group(0)
    post = re.search(r"if CurStep = ssPostInstall then.*?\n  end;", _ISS, flags=re.DOTALL)
    assert post and "RetireLegacyPacketBridge;" in post.group(0)


def test_stale_bridge_folder_is_purged_on_upgrade():
    assert "[InstallDelete]" in _ISS
    assert r'Type: filesandordirs; Name: "{app}\packet_bridge"' in _ISS


def test_uninstall_removes_both_service_registrations():
    m = re.search(r"procedure CurUninstallStepChanged.*?\nend;", _ISS, flags=re.DOTALL)
    assert m, "orion.iss lost its CurUninstallStepChanged handler"
    body = m.group(0)
    assert "RemoveBridgeService('VeniceNetSvc')" in body
    assert "RemoveBridgeService('NexusVisionSvc')" in body


def test_build_script_refuses_a_package_with_the_bridge():
    assert "retired packet bridge" in _BUILD_PS1
    for rel in (r"packet_bridge\VeniceNetSvc.exe", r"packet_bridge\WinDivert64.dll",
                r"packet_bridge\WinDivert64.sys"):
        assert rel in _BUILD_PS1
    # and the compiled-script read-back forbids re-registration
    assert "still registers the retired packet bridge" in _BUILD_PS1
    assert "VeniceSetup-$Version.exe" in _BUILD_PS1
    assert "orion.preprocessed.iss" in _BUILD_PS1


def test_release_policy_requires_nothing_and_forbids_the_bridge():
    policy = _policy()
    assert policy.COMPILED_SERVICE_REQUIRED_FILES == frozenset()
    assert policy.RETIRED_PACKET_BRIDGE_FILES == frozenset(
        {"packet_bridge/VeniceNetSvc.exe", "packet_bridge/WinDivert64.dll",
         "packet_bridge/WinDivert64.sys"}
    )
    assert "packet_bridge/VeniceNetSvc.exe" not in policy.RELEASE_ADMITTED_NESTED_EXECUTABLES
