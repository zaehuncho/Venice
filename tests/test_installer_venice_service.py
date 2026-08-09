"""Installer wave 3 — Venice rebrand of the packet-bridge service (2026-08-08).

The customer installer must register the wave-2A C++ service under the
customer-facing name VeniceNetSvc, arm it the way the service actually reads its
arm switch in SCM mode (the per-service Environment registry value, NOT the
binPath flag — venicenet_service/main.cpp runService), keep the Interactive-Users
start DACL, and remove the legacy NexusVisionSvc BEFORE creating the new service
(both bind exclusive TCP 47291).

These are string-level contracts on installer/orion.iss and its build script.
They regress silently otherwise: the installer compiles fine with the old name
and every customer machine ships a Meter Delay toggle that cannot engage.
Windows-authoritative behaviour (sc.exe semantics) is exercised on the rig via
installer/build_installer.ps1's compiled-script read-back; this file keeps the
same strings pinned in CI where iscc does not exist.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ISS = (_ROOT / "installer" / "orion.iss").read_text(encoding="utf-8", errors="replace")
_BUILD_PS1 = (_ROOT / "installer" / "build_installer.ps1").read_text(
    encoding="utf-8", errors="replace"
)
_OWNER_PS1 = (_ROOT / "scripts" / "owner_venice_setup.ps1").read_text(
    encoding="utf-8", errors="replace"
)
_SERVICE_ARGS_H = (
    _ROOT / "native_orion" / "venicenet_service" / "ServiceArgs.h"
).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 1. the service is registered under the Venice name, from the C++ exe
# --------------------------------------------------------------------------- #
def test_installer_creates_venicenetsvc_from_the_cpp_exe():
    assert "create VeniceNetSvc binPath= " in _ISS
    assert r"{app}\packet_bridge\VeniceNetSvc.exe" in _ISS
    # demand-start + LocalSystem, same shape the legacy registration used and
    # scripts/owner_venice_setup.ps1 mirrors on the dev rig.
    assert "start= demand type= own obj= LocalSystem" in _ISS
    assert 'DisplayName= "Venice Packet Service"' in _ISS
    # The arm flag stays baked into binPath (arms a bare developer launch).
    assert "--arm-meter-delay" in _ISS


def test_installer_never_registers_the_legacy_service_name():
    # The legacy name may only appear in stop/delete/removal paths.
    assert "create NexusVisionSvc" not in _ISS
    assert "description NexusVisionSvc" not in _ISS
    assert "sdset NexusVisionSvc" not in _ISS


# --------------------------------------------------------------------------- #
# 2. rollback compatibility: legacy service removed BEFORE the new one exists
#    (port 47291 is exclusive; the two bridges must never coexist)
# --------------------------------------------------------------------------- #
def test_legacy_service_is_removed_before_venicenetsvc_is_created():
    remove_legacy = _ISS.find("RemoveBridgeService('NexusVisionSvc')")
    create_new = _ISS.find("create VeniceNetSvc binPath= ")
    assert remove_legacy != -1, "legacy NexusVisionSvc removal is gone from orion.iss"
    assert create_new != -1
    assert remove_legacy < create_new, (
        "NexusVisionSvc must be stopped+deleted BEFORE VeniceNetSvc is created "
        "(both bind exclusive TCP 47291)"
    )


def test_stale_nuitka_bundle_is_purged_on_upgrade():
    # Old installs carried the Nuitka bundle under packet_bridge\; Inno never removes
    # files absent from the new payload, so an explicit purge is required.
    assert "[InstallDelete]" in _ISS
    assert r'Type: filesandordirs; Name: "{app}\packet_bridge"' in _ISS


def test_uninstall_removes_both_service_registrations():
    m = re.search(
        r"procedure CurUninstallStepChanged.*?\nend;", _ISS, flags=re.DOTALL
    )
    assert m, "orion.iss lost its CurUninstallStepChanged handler"
    body = m.group(0)
    assert "RemoveBridgeService('VeniceNetSvc')" in body
    assert "RemoveBridgeService('NexusVisionSvc')" in body


# --------------------------------------------------------------------------- #
# 3. the service-mode arm contract: per-service Environment registry value
# --------------------------------------------------------------------------- #
def test_installer_writes_the_service_mode_arm_env_value():
    m = re.search(r'kMeterArmEnv\s*=\s*"([^"]+)"', _SERVICE_ARGS_H)
    assert m, "kMeterArmEnv not found in ServiceArgs.h"
    env_name = m.group(1)  # the name the C++ service actually reads in SCM mode
    assert f"{env_name}=1" in _ISS, (
        f"orion.iss must write {env_name}=1 into the per-service Environment "
        "value — the binPath flag alone does NOT arm SCM service mode "
        "(venicenet_service/main.cpp runService)"
    )
    assert re.search(
        r"RegWriteMultiStringValue\(HKLM,\s*EnvKey,\s*'Environment'", _ISS
    ), "the arm value must be a REG_MULTI_SZ 'Environment' under the service key"
    assert r"SYSTEM\CurrentControlSet\Services\VeniceNetSvc" in _ISS
    # And a read-back so a silent write failure is reported, not shipped.
    assert "RegQueryMultiStringValue" in _ISS


# --------------------------------------------------------------------------- #
# 4. DACL parity with the dev-rig registration script
# --------------------------------------------------------------------------- #
def _iss_sddl() -> str:
    m = re.search(r"Sddl\s*:=\s*'([^']*)'\s*\+\s*'([^']*)';", _ISS)
    assert m, "SDDL assignment not found in orion.iss"
    return m.group(1) + m.group(2)


def test_dacl_matches_owner_venice_setup():
    m = re.search(r"\$Sddl\s*=\s*'([^']+)'", _OWNER_PS1)
    assert m, "SDDL not found in scripts/owner_venice_setup.ps1"
    assert _iss_sddl() == m.group(1), (
        "installer and owner_venice_setup.ps1 must register the same service DACL"
    )


def test_dacl_grants_interactive_users_start_stop():
    sddl = _iss_sddl()
    # RP (start) + WP (stop) for Interactive Users; SYSTEM/Admins keep control.
    assert "(A;;CCLCSWRPWPLOCRRC;;;IU)" in sddl
    assert ";;;SY)" in sddl and ";;;BA)" in sddl
    assert "sdset VeniceNetSvc" in _ISS


# --------------------------------------------------------------------------- #
# 5. description style preserved (customer-visible Services.msc text)
# --------------------------------------------------------------------------- #
def test_service_description_preserved():
    assert "description VeniceNetSvc" in _ISS
    assert "Privileged WinDivert packet bridge for Venice." in _ISS
    assert "without running as Administrator" in _ISS


# --------------------------------------------------------------------------- #
# 6. build script: payload gate + artifact identity + read-back
# --------------------------------------------------------------------------- #
def test_build_script_requires_the_wave3_payload():
    assert r"packet_bridge\VeniceNetSvc.exe" in _BUILD_PS1
    assert r"packet_bridge\WinDivert64.dll" in _BUILD_PS1
    assert r"packet_bridge\WinDivert64.sys" in _BUILD_PS1


def test_build_script_verifies_the_compiled_installer():
    # The artifact is the customer-facing VeniceSetup name (OutputBaseFilename).
    assert "VeniceSetup-$Version.exe" in _BUILD_PS1
    # And the compiled-script read-back pins the registration strings.
    assert "orion.preprocessed.iss" in _BUILD_PS1
    assert "create VeniceNetSvc binPath= " in _BUILD_PS1
    assert "ORION_METER_DELAY_ARMED=1" in _BUILD_PS1


def test_release_policy_requires_the_cpp_service_payload():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "release_filter_policy", _ROOT / "tools" / "release_filter_policy.py"
    )
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    assert policy.COMPILED_SERVICE_REQUIRED_FILES == frozenset(
        {
            "packet_bridge/VeniceNetSvc.exe",
            "packet_bridge/WinDivert64.dll",
            "packet_bridge/WinDivert64.sys",
        }
    )
