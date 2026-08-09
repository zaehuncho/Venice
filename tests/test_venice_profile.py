r"""venice-profile.json export/import — the settings backup surface (2026-08-08).

Shot Lead, Tip Timing, Meter Delay and the per-shot-type press-anchored learned
constants are hard-won values; the profile file lets a customer carry them across
a reinstall or to a second rig without hand-editing settings.json (which the
settings signature would flag anyway).

The implementation is C++ (native_orion/src/VeniceProfile.h, wired through the
single OrionAppController::runVeniceProfileAction slot). Following the
test_installer_venice_service.py pattern: this file pins the string-level
contracts everywhere, AND — when the native build tree is present (the rig) —
actually executes the behavioural suite (OrionVeniceProfileTests, QTest) via
ctest, which applies the offscreen-Qt environment the CMake test properties
declare. In CI without a build tree the string pins alone keep the contracts.

Contracts:
  * venice_profile_round_trips           — export -> import reproduces the config
                                           bit-identically (deterministic payload).
  * venice_profile_import_clamps_out_of_range_values
                                         — lead=9999 imports as the 800 ceiling;
                                           every value clamps into the AppConfig
                                           load bands; clamps are logged.
  * venice_profile_never_exports_license — allowlist snapshot; license/token/
                                           account data is structurally absent.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HEADER = (_ROOT / "native_orion" / "src" / "VeniceProfile.h").read_text(
    encoding="utf-8", errors="replace"
)
_TESTS_CPP = (_ROOT / "native_orion" / "tests" / "VeniceProfileTests.cpp").read_text(
    encoding="utf-8", errors="replace"
)
_CONTROLLER = (_ROOT / "native_orion" / "src" / "OrionAppController.cpp").read_text(
    encoding="utf-8", errors="replace"
)
_BUILD_DIR = _ROOT / "native_orion" / "build"

_cpp_result = None  # (ran: bool, detail: str)


def _cpp_suite():
    """Run OrionVeniceProfileTests once via ctest when the rig build exists."""
    global _cpp_result
    if _cpp_result is not None:
        return _cpp_result
    ctest = shutil.which("ctest")
    exe_present = any(_BUILD_DIR.glob("**/OrionVeniceProfileTests.exe"))
    if not ctest or not (_BUILD_DIR / "CTestTestfile.cmake").exists() or not exe_present:
        _cpp_result = (False, "native build tree not present - string pins only")
        return _cpp_result
    proc = subprocess.run(
        [ctest, "-C", "Release", "-R", "^OrionVeniceProfileTests$", "--output-on-failure"],
        cwd=str(_BUILD_DIR),
        capture_output=True,
        text=True,
        timeout=300,
    )
    detail = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        pytest.fail(
            "OrionVeniceProfileTests failed under ctest:\n" + detail[-4000:],
            pytrace=False,
        )
    _cpp_result = (True, detail)
    return _cpp_result


# --------------------------------------------------------------------------- #
# 1. round trip
# --------------------------------------------------------------------------- #
def test_venice_profile_round_trips():
    # The C++ behavioural assertion exists and compares the WHOLE payload…
    assert "void veniceProfileRoundTrips()" in _TESTS_CPP
    assert "QCOMPARE(veniceProfileExport(restored, restoredLearning), exported);" in _TESTS_CPP
    # …which is only meaningful because the export is deterministic (no timestamps).
    assert "EXPORT IS DETERMINISTIC" in _HEADER
    for banned in ("currentDateTime", "QDateTime", "toString(Qt::ISODate"):
        assert banned not in _HEADER, (
            f"VeniceProfile.h grew a {banned} - a non-deterministic payload breaks "
            "the bit-identical round-trip contract"
        )
    ran, _ = _cpp_suite()
    if not ran:
        print("note: native build absent; behavioural round trip runs on the rig")


# --------------------------------------------------------------------------- #
# 2. import clamps
# --------------------------------------------------------------------------- #
def test_venice_profile_import_clamps_out_of_range_values():
    # The headline case is pinned in the C++ suite: lead 9999 -> the 800 ceiling.
    assert 'profile.insert(QStringLiteral("actuation_lead_ms"), 9999.0);' in _TESTS_CPP
    assert "QCOMPARE(data.actuationLeadMs, AppConfigData::kActuationLeadMaxMs);" in _TESTS_CPP
    assert "QCOMPARE(data.actuationLeadMs, 800.0);" in _TESTS_CPP
    # And the import path clamps against the SAME constants the load path uses —
    # never literals that could drift from AppConfig.
    for needle in (
        "AppConfigData::kActuationLeadMinMs",
        "AppConfigData::kActuationLeadMaxMs",
        "AppConfigData::kMeterDelayMinMs",
        "AppConfigData::kMeterDelayMaxMs",
        "std::clamp(raw, lo, hi)",
    ):
        assert needle in _HEADER, f"VeniceProfile.h lost its clamp against {needle}"
    # Clamps are REPORTED, not silent (the controller logs each note).
    assert "result.notes" in _HEADER
    assert 'appendLog(QStringLiteral("Profile import: %1").arg(note));' in _CONTROLLER
    ran, _ = _cpp_suite()
    if not ran:
        print("note: native build absent; behavioural clamp checks run on the rig")


# --------------------------------------------------------------------------- #
# 3. never exports license / secrets
# --------------------------------------------------------------------------- #
def test_venice_profile_never_exports_license():
    # Snapshot of the export allowlist lives in the C++ suite; pin its presence and
    # that the vocabulary check covers the secret classes.
    assert "void veniceProfileNeverExportsLicense()" in _TESTS_CPP
    for needle in ('"license"', '"entitlement"', '"bearer"', '"nexus_bridge"'):
        assert needle in _TESTS_CPP.replace("QLatin1String(", "").replace(
            "QStringLiteral(", ""
        ), f"the C++ vocabulary check lost {needle}"
    # The header's CODE (comments and human-facing string literals stripped) must
    # never reference secret-bearing fields or keys — the allowlist cannot reach them.
    header_code = "\n".join(
        line
        for line in _HEADER.splitlines()
        if not line.lstrip().startswith("//") and not line.lstrip().startswith('"')
    )
    for forbidden in (
        "license",
        "entitlement",
        "session_token",
        "chiakiPath",
        "remotePlayConsoleIp",
        "remotePlayProfile",
        "nexus_bridge",
    ):
        assert forbidden.lower() not in header_code.lower(), (
            f"VeniceProfile.h code references '{forbidden}' - the allowlist must not touch it"
        )
    ran, _ = _cpp_suite()
    if not ran:
        print("note: native build absent; sentinel/allowlist snapshot runs on the rig")


# --------------------------------------------------------------------------- #
# 4. wiring pins (the ship-window constraint: ONE slot + ONE signal)
# --------------------------------------------------------------------------- #
def test_profile_wiring_is_one_slot_one_signal():
    header = (_ROOT / "native_orion" / "src" / "OrionAppController.h").read_text(
        encoding="utf-8", errors="replace"
    )
    assert (
        "Q_INVOKABLE void runVeniceProfileAction(const QString& action, const QUrl& fileUrl);"
        in header
    )
    assert "void veniceProfileActionCompleted(bool ok, const QString& summary);" in header


def test_profile_import_persists_learning_before_settings():
    # Same ORDER-MATTERS contract as setTipTimingMs: the settings save re-runs
    # applyConfig, which must restore the NEW tip prior in the same pass.
    body_start = _CONTROLLER.find("void OrionAppController::runVeniceProfileAction")
    assert body_start != -1, "runVeniceProfileAction is gone from OrionAppController.cpp"
    body = _CONTROLLER[body_start : body_start + 6000]
    save_learning = body.find("config_.saveLearning(learning")
    save_settings = body.find("saveConfigSilently(data)")
    assert save_learning != -1 and save_settings != -1
    assert save_learning < save_settings, (
        "profile import must persist learning.json BEFORE the settings save "
        "(the settings save is what re-runs applyConfig)"
    )


def test_profile_card_routes_through_the_single_slot():
    card = (
        _ROOT / "native_orion" / "qml" / "components" / "VeniceProfileCard.qml"
    ).read_text(encoding="utf-8", errors="replace")
    assert 'orion.runVeniceProfileAction("export", selectedFile)' in card
    assert 'orion.runVeniceProfileAction("import", selectedFile)' in card
    assert "venice-profile.json" in card
    # The card is on the Debug page (owner-visible, low-traffic), NOT the meter panel.
    debug = (_ROOT / "native_orion" / "qml" / "pages" / "DebugPage.qml").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "VeniceProfileCard" in debug
    meter_panel = (
        _ROOT / "native_orion" / "qml" / "components" / "MeterConfigPanel.qml"
    ).read_text(encoding="utf-8", errors="replace")
    assert "VeniceProfileCard" not in meter_panel
    assert "runVeniceProfileAction" not in meter_panel
