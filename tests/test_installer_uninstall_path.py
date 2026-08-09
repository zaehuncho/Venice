r"""Installer uninstall path — service removal order + cleanup surface (2026-08-08).

Wave 3 verified INSTALL/UPGRADE (legacy NexusVisionSvc removed before VeniceNetSvc
is created). This file pins the UNINSTALL contract in installer/orion.iss:

1. Both bridge service names are stopped + deleted at usUninstall — BEFORE any file
   removal — via the shared RemoveBridgeService helper, whose sc.exe order is
   stop -> poll STOPPED -> delete. The poll matters twice over: sc stop only
   REQUESTS the stop (deleting a running service leaves it delete-pending), and the
   C++ service unloads the WinDivert kernel driver during its own runService
   teardown (venicenet_service/main.cpp::unloadWinDivertDriver) BEFORE the SCM
   reports STOPPED — so waiting for STOPPED is what guarantees the driver is
   unloaded before {app}\packet_bridge\WinDivert64.sys is deleted.
2. A WinDivert safety-net STOP (never delete) covers the crashed-bridge residue.
3. The bearer token files (%ProgramData% + %LOCALAPPDATA% NexusVision\
   nexus_bridge.token) are removed; the NexusVision parent folds away dirifempty.
4. User data (%LOCALAPPDATA%\NexusVision\Orion Native\) is removed only on an
   explicit interactive Yes (default No); silent uninstall always keeps it.
5. The whole {app} tree (including packet_bridge\) is purged.

String-level contracts, same rationale as test_installer_venice_service.py: the
script compiles fine without any of this and every customer machine then leaks a
SYSTEM service registration or a resident kernel driver. Windows-authoritative
behaviour (sc.exe semantics) is exercised on the rig via build_installer.ps1's
compiled-script read-back; this file keeps the strings pinned in CI.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ISS = (_ROOT / "installer" / "orion.iss").read_text(encoding="utf-8", errors="replace")
_BUILD_PS1 = (_ROOT / "installer" / "build_installer.ps1").read_text(
    encoding="utf-8", errors="replace"
)


def _block(name: str) -> str:
    m = re.search(rf"(?:procedure|function) {name}.*?\nend;", _ISS, flags=re.DOTALL)
    assert m, f"orion.iss lost its {name} procedure"
    return m.group(0)


def _code_only(text: str) -> str:
    """Strip full-line ';' comments and Pascal {{ }} comment blocks."""
    no_braces = re.sub(r"\{[^}]*\}", "", text, flags=re.DOTALL)
    return "\n".join(
        line for line in no_braces.splitlines() if not line.lstrip().startswith(";")
    )


# --------------------------------------------------------------------------- #
# 1. service removal: both names, at usUninstall, stop -> poll -> delete
# --------------------------------------------------------------------------- #
def test_uninstall_removes_both_bridge_service_names_at_usuninstall():
    body = _block("CurUninstallStepChanged")
    assert "usUninstall" in body
    assert "RemoveBridgeService('VeniceNetSvc')" in body
    assert "RemoveBridgeService('NexusVisionSvc')" in body


def test_remove_bridge_service_orders_stop_poll_delete():
    body = _block("RemoveBridgeService")
    stop = body.find("RunSc('stop ' + ServiceName")
    poll = body.find("WaitServiceStopped(ServiceName)")
    delete = body.find("RunSc('delete ' + ServiceName")
    assert stop != -1, "sc stop is gone from RemoveBridgeService"
    assert poll != -1, "the STOPPED poll is gone — sc stop only REQUESTS the stop"
    assert delete != -1, "sc delete is gone from RemoveBridgeService"
    assert stop < poll < delete, (
        "uninstall must be sc stop -> poll STOPPED -> sc delete: deleting a running "
        "service marks it delete-pending, and the service's own teardown (which "
        "unloads the WinDivert driver) only completes when the SCM reports STOPPED"
    )


def test_stopped_poll_actually_polls_the_scm():
    body = _block("WaitServiceStopped")
    assert 'findstr /C:"STOPPED"' in body
    assert "sc query" in body
    assert "Sleep(" in body


def test_services_are_removed_before_files():
    # usUninstall fires before the uninstaller removes files, so putting the
    # service removal there is the rollback guarantee: a failed uninstall can
    # leave files-without-service (harmless) but never a registered service
    # pointing at a deleted exe.
    body = _block("CurUninstallStepChanged")
    assert "usUninstall" in body
    assert "usPostUninstall" not in body.split("RemoveBridgeService")[0], (
        "bridge service removal must run at usUninstall (before file removal), "
        "not usPostUninstall (after)"
    )


# --------------------------------------------------------------------------- #
# 2. WinDivert driver: safety-net stop, never delete; shared drivers kept
# --------------------------------------------------------------------------- #
def test_windivert_safety_net_stops_but_never_deletes():
    body = _block("StopWinDivertDriver")
    assert "RunSc('stop WinDivert'" in body
    assert "WaitServiceStopped('WinDivert')" in body
    assert "RunSc('delete WinDivert" not in _ISS and "delete WinDivert'" not in _code_only(_ISS), (
        "never sc delete the WinDivert registration — it is demand-created by the "
        "next handle open and may belong to another product"
    )
    # And the safety net actually runs on the uninstall path.
    assert "StopWinDivertDriver;" in _block("CurUninstallStepChanged")


def test_shared_input_drivers_survive_uninstall():
    # ViGEmBus/HidHide are shared vendor drivers; uninstall must not remove them.
    code = _code_only(_ISS)
    assert "delete ViGEmBus" not in code
    assert "delete HidHide" not in code
    for m in re.finditer(r"msiexec\s+/x", code, flags=re.IGNORECASE):
        raise AssertionError(f"unexpected driver uninstall step in orion.iss: {m.group(0)}")


# --------------------------------------------------------------------------- #
# 3. cleanup surface: token files, install tree, parent-dir folding
# --------------------------------------------------------------------------- #
def _uninstall_delete_section() -> str:
    m = re.search(r"\[UninstallDelete\](.*?)\n\[", _ISS, flags=re.DOTALL)
    assert m, "orion.iss lost its [UninstallDelete] section"
    return m.group(1)


def test_uninstall_purges_the_whole_install_tree():
    section = _uninstall_delete_section()
    assert r'Type: filesandordirs; Name: "{app}"' in section, (
        "the {app} purge is what removes runtime-created files AND the "
        "packet_bridge\\ sub-tree (VeniceNetSvc.exe + WinDivert64.dll/.sys)"
    )


def test_uninstall_removes_bridge_token_files():
    section = _uninstall_delete_section()
    assert r'Type: files; Name: "{commonappdata}\NexusVision\nexus_bridge.token"' in section
    assert r'Type: files; Name: "{localappdata}\NexusVision\nexus_bridge.token"' in section


def test_uninstall_folds_empty_nexusvision_dirs_only():
    section = _uninstall_delete_section()
    # dirifempty (NOT filesandordirs): the "Orion Native" data dir must survive
    # unless the user explicitly chose removal, and sibling products keep the dir.
    assert r'Type: dirifempty; Name: "{commonappdata}\NexusVision"' in section
    assert r'Type: dirifempty; Name: "{localappdata}\NexusVision"' in section
    assert r'filesandordirs; Name: "{localappdata}\NexusVision"' not in _ISS, (
        "never blanket-delete the NexusVision dir — user data removal is an "
        "explicit interactive choice (MaybeRemoveUserData)"
    )


# --------------------------------------------------------------------------- #
# 4. user data: explicit choice, safe defaults, silent keeps everything
# --------------------------------------------------------------------------- #
def test_user_data_removal_is_an_explicit_choice():
    body = _block("MaybeRemoveUserData")
    assert r"{localappdata}\NexusVision\Orion Native" in body, (
        "the data dir is %LOCALAPPDATA%\\NexusVision\\Orion Native (OrionPaths.h "
        "orionDataDir), NOT \\Venice\\ and NOT the install dir"
    )
    assert "MsgBox('Remove Venice data?'" in body
    assert "mbConfirmation" in body and "MB_YESNO" in body
    assert "MB_DEFBUTTON2" in body, "default button must be No (keep the data)"
    assert "= IDYES" in body, "DelTree may run only on an explicit Yes"
    assert "DelTree(DataDir, True, True, True)" in body


def test_silent_uninstall_never_deletes_user_data():
    body = _block("MaybeRemoveUserData")
    silent = body.find("UninstallSilent")
    deltree = body.find("DelTree(")
    assert silent != -1, "the silent-mode guard is gone"
    assert deltree != -1
    assert silent < deltree, (
        "the UninstallSilent early-exit must run before DelTree — an unattended "
        "uninstall must never destroy learned calibration"
    )
    assert "MaybeRemoveUserData;" in _block("CurUninstallStepChanged")


def test_running_processes_are_killed_before_uninstall():
    # Without this the uninstaller cannot delete Qt6Core/libcrypto and reports
    # partial-uninstall (files left behind, service already gone).
    body = _block("InitializeUninstall")
    assert "KillVeniceProcesses" in body


# --------------------------------------------------------------------------- #
# 5. build script read-back pins the compiled uninstall surface
# --------------------------------------------------------------------------- #
def test_build_script_verifies_the_uninstall_strings():
    for needle in (
        "CurUninstallStepChanged",
        "RemoveBridgeService(''VeniceNetSvc'')",
        "stop WinDivert",
        "nexus_bridge.token",
        r"NexusVision\Orion Native",
    ):
        assert needle in _BUILD_PS1, (
            f"build_installer.ps1 read-back lost the uninstall needle: {needle}"
        )
