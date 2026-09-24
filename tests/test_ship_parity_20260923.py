"""[SHIP PARITY 2026-09-23] The packaged build must run the owner's validated DEV configuration.

Source: docs/redteam/2026-09-23-final/SHIP_PARITY_AUDIT.md (R1-R8, R10, R11) and
docs/redteam/2026-09-23-final/patches/P-G_ship_parity.md.

These are cross-language CONTRACT pins read from the native source text, so
``pytest tests/test_ship_parity_20260923.py`` answers "does a fresh or upgraded customer install
fire like the owner's dev rig?" without a native build. The behavioural proof is native:
``AutomationEngineTests::shipParity*`` and ``RemotePlayExecutablePolicyTests::
nativeTimingProfilePinsSoloAndStretchInProduction``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "native_orion" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _profile_header() -> str:
    return _read(SRC / "SidecarReaderProfile.h")


def _app_config_h() -> str:
    return _read(SRC / "AppConfig.h")


def _app_config_cpp() -> str:
    return _read(SRC / "AppConfig.cpp")


# --------------------------------------------------------------- R1-R3: the native timing profile
def _native_flag_block(header: str) -> str:
    m = re.search(r"kShippedNativeTimingProfileFlags\[\]\s*=\s*\{(.*?)\};", header, re.S)
    assert m, "kShippedNativeTimingProfileFlags is missing"
    return m.group(1)


def test_r1_tip_phase_solo_is_a_production_pinned_native_flag():
    block = _native_flag_block(_profile_header())
    assert '"ORION_TIP_PHASE_SOLO"' in block
    # the dev-rig pins that were already there stay
    assert '"ORION_HORIZON_DEBIAS"' in block and '"ORION_RAMP_SHAPE"' in block


def test_r2_curve_stretch_alpha_is_pinned_to_zero_by_the_native_profile():
    header = _profile_header()
    m = re.search(r"kShippedNativeTimingProfileValues\[\]\s*=\s*\{(.*?)\};", header, re.S)
    assert m, "kShippedNativeTimingProfileValues is missing"
    assert '{"ORION_CURVE_STRETCH_ALPHA", "0"}' in m.group(1)
    # ...and the resolver actually walks that list (production force-pin, dev preserves env)
    fn = header[header.index("applyShippedNativeTimingProfile("):]
    assert "for (const auto& setting : kShippedNativeTimingProfileValues)" in fn
    assert "qputenv(setting.name, value)" in fn


def test_r2_the_reader_lane_source_default_is_not_touched():
    """The 0.6 compiled default stays (46 mechanism tests); only the shipped profile pins 0."""
    engine_h = _read(SRC / "AutomationEngine.h")
    assert re.search(r"double\s+curveRateStretchAlpha\s*=\s*0\.6\s*;", engine_h)
    assert re.search(r"bool\s+tipPhaseSolo\s*=\s*false\s*;", engine_h)


def test_r3_timing_profile_id_is_bumped_everywhere():
    new_id = "2k27-2026-09-23-v3"
    assert f'"{new_id}"' in _profile_header()
    assert f'"ORION_TIMING_PROFILE_ID": "{new_id}"' in _read(
        REPO / "tools" / "quality" / "consistency_bench.py")
    assert new_id in _read(REPO / "tests" / "test_consistency_bench.py")
    assert "2k27-2026-09-04-v2" not in _profile_header()


# --------------------------------------------------------------- R4-R8: the AppConfigData defaults
SHIP_BOOL_DEFAULTS = {
    "ownershipProofTwoFrame": "ownership_proof_two_frame",
    "tipPhaseAnchorBase20": "tip_phase_anchor_base20",
    "tipPhaseTypeTrimEnabled": "tip_phase_type_trim_enabled",
    "tipPhaseAimFrozen": "tip_phase_aim_frozen",
}


def test_r4_r6_r7_boolean_ship_defaults_are_on_in_the_header():
    header = _app_config_h()
    for member in SHIP_BOOL_DEFAULTS:
        assert re.search(r"\bbool\s+" + member + r"\s*=\s*true\s*;", header), (
            f"{member} must default true (owner's validated dev setup)")


def test_r4_r6_r7_the_loader_inherits_the_member_default():
    """No second hard-coded copy: the per-key fallback is the member itself."""
    cpp = _app_config_cpp()
    for member, key in SHIP_BOOL_DEFAULTS.items():
        assert re.search(
            r"data_\." + member + r"\s*=\s*cleanBool\(obj,\s*\"" + key + r"\",\s*data_\."
            + member + r"\)", cpp), f"loader for {key} must fall back to data_.{member}"


def test_r8_no_meter_fade_trim_defaults_to_six_in_header_loader_and_save_clamp():
    assert re.search(r"double\s+noMeterFadeTrimMs\s*=\s*6\.0\s*;", _app_config_h())
    cpp = _app_config_cpp()
    assert re.search(r'cleanDouble\(obj,\s*"no_meter_fade_trim_ms",\s*6\.0', cpp)
    assert "noMeterFadeTrimMsForSave" not in cpp  # guard against a helper hiding a 0.0
    save = cpp[cpp.index("bool AppConfig::save("):]
    m = re.search(r"data\.noMeterFadeTrimMs\s*=\s*std::isfinite\(data\.noMeterFadeTrimMs\)(.*?);",
                  save, re.S)
    assert m and ": 6.0" in m.group(1), "a NaN trim must save as the ship default 6.0"


# --------------------------------------------------------------- R7: the aim prior
def test_r7_factory_aim_prior_is_271_canonical_and_applied_by_appconfig():
    header = _app_config_h()
    assert re.search(r"kShippedPhasePhysicalMs\s*=\s*271\.0\s*;", header)
    cpp = _app_config_cpp()
    # the prior is a persistence-layer default, applied on every learning (re)load path
    assert "applyShippedPhasePrior(" in cpp
    reload = cpp[cpp.index("void AppConfig::reloadLearning()"):]
    reload = reload[: reload.index("\n}\n") if "\n}\n" in reload else len(reload)]
    assert "applyShippedPhasePrior(" in reload or "reloadLearningFromDisk" in reload
    # LearningData{} stays -1 so the engine's own fixtures keep the seed path
    assert re.search(r"double\s+learnedPhasePhysicalMs\s*=\s*-1\.0\s*;", header)


def test_r7_engine_latches_the_frozen_aim_after_the_base20_constellation():
    """The audit's ordering risk: the seed fallback must read the POST-transition seed."""
    engine = _read(SRC / "AutomationEngine.cpp")
    apply_start = engine.index("void AutomationEngine::applyConfig(")
    constellation = engine.index("if (settings.tipPhaseAnchorBase20 != config_.anchorBase20) {",
                                 apply_start)
    latch = engine.index("frozenAimPhysicalMs_ = learning.learnedPhasePhysicalMs > 0.0", apply_start)
    assert latch > constellation, (
        "frozenAimPhysicalMs_ must be latched after the base-20 constellation is applied")


# --------------------------------------------------------------- R10: the v3 migration
def test_r10_settings_version_is_three():
    assert re.search(r"static constexpr int kSettingsVersion\s*=\s*3\s*;", _app_config_h())


def test_r10_v3_registry_rules_are_single_key_and_old_default_gated():
    cpp = _app_config_cpp()
    reg = cpp[cpp.index("static const QList<SettingsMigration> registry"):]
    reg = reg[: reg.index("};")]
    expected = [
        ("tip_phase_anchor_base20", "QJsonValue(false)", "QJsonValue(true)", "QString{}"),
        ("tip_phase_type_trim_enabled", "QJsonValue(false)", "QJsonValue(true)", "QString{}"),
        ("ownership_proof_two_frame", "QJsonValue(false)", "QJsonValue(true)", "QString{}"),
        ("no_meter_fade_trim_ms", "QJsonValue(0.0)", "QJsonValue(6.0)", "QString{}"),
        ("tip_phase_aim_frozen", "QJsonValue(false)", "QJsonValue(true)",
         'QStringLiteral("tip_timing_user_set")'),
    ]
    for key, old, new, companion in expected:
        pattern = (r"\{3,\s*QStringLiteral\(\"" + re.escape(key) + r"\"\),\s*"
                   + re.escape(old) + r",\s*" + re.escape(new) + r",\s*" + re.escape(companion))
        assert re.search(pattern, reg), f"v3 rule for {key} missing or mis-shaped"


def test_r10_one_time_aim_reset_is_gated_on_a_pre_v3_file_and_not_user_owned_aim():
    cpp = _app_config_cpp()
    load = cpp[cpp.index("bool AppConfig::load()"):]
    load = load[: load.index("\nbool AppConfig::save(")]
    assert "kAimResetSettingsVersion" in load
    assert "aimIsUserOwned(" in load
    assert "kShippedPhasePhysicalMs" in load
    assert "saveLearning(" in load


def test_r10_stale_never_confirmed_comment_is_gone():
    cpp = _app_config_cpp()
    assert "never confirmed by a counted live batch" not in cpp


# --------------------------------------------------------------- R11: docs
def test_r11_ship_config_doc_matches_the_code():
    doc = _read(REPO / "docs" / "SHIP_CONFIG.md")
    row = next(line for line in doc.splitlines() if line.startswith("| `lead_offset_left_fade_ms`"))
    assert "`-6.0`" in row and "`8.0`" not in row
    for key in ("tip_phase_anchor_base20", "tip_phase_type_trim_enabled",
                "ownership_proof_two_frame", "tip_phase_aim_frozen", "no_meter_fade_trim_ms",
                "ORION_TIP_PHASE_SOLO", "ORION_CURVE_STRETCH_ALPHA", "2k27-2026-09-23-v3"):
        assert key in doc, f"docs/SHIP_CONFIG.md does not document {key}"
    assert "2k27-2026-09-04-v2" not in doc


# --------------------------------------------------------------- the shelved meter delay
def test_meter_delay_setting_cannot_arm_the_shelved_feature():
    ctl = _read(SRC / "OrionAppController.cpp")
    assert re.search(r"constexpr bool kMeterDelayShelved\s*=\s*true\s*;", ctl)
    assert re.search(r"return\s+!kMeterDelayShelved\s*&&\s*d\.meterDelayEnabled\s*;", ctl)
    # every consumer of the persisted flag goes through the shelved gate
    raw_reads = [m.start() for m in re.finditer(r"\.meterDelayEnabled\b", ctl)]
    allowed = {"return !kMeterDelayShelved && d.meterDelayEnabled",
               "data.meterDelayEnabled == value", "data.meterDelayEnabled = value"}
    for pos in raw_reads:
        line = ctl[ctl.rfind("\n", 0, pos) + 1: ctl.find("\n", pos)]
        assert any(a in line for a in allowed), f"raw meter_delay_enabled read: {line.strip()}"
