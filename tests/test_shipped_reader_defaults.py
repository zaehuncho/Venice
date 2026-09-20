"""Pin the SimpleMeterReader env-flag DEFAULTS to the shipped reader profile.

WHY THIS EXISTS. Every timing number this project has ever calibrated against was
measured with three reader flags ON, historically set only by the gitignored dev
launcher (run_orion.local.ps1). A customer build inherits ZERO ORION_* variables,
so customer/rig parity survives ONLY because the module defaults now match the
launcher. The native launcher now force-pins this profile for production while
the Python defaults keep offline tools aligned. This test makes it impossible
for either edge to silently regress. The flag set and its evidence live in
SidecarReaderProfile.h; keep the two lists in sync when a flag clears the bar.

This is the recurring failure pattern in this codebase (a shipped default that
nothing pins, tests green because fixtures set their own values), so the test is
deliberately blunt: construct the production reader with a scrubbed environment
and read the resolved flags.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simple_meter_reader import SimpleMeterReader  # noqa: E402

# Mirror of kShippedReaderProfileFlags in native_orion/src/SidecarReaderProfile.h,
# mapped to the attribute each flag resolves into. Additions must clear the same
# bar documented there: live-graded ON + a production-wired A/B with zero
# detection flips.
SHIPPED_PROFILE = {
    "ORION_READER_ANCHOR": "_anchor",
    "ORION_READER_PCTL_FILL": "_pctl_fill",
    "ORION_READER_TRACK_H_CAP": "_track_h_cap",
}

LIVE_ONLY_PROFILE = {
    "ORION_METER_SUBPIXEL_SESSION_RULER",
    "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL",
    "ORION_METER_SUBPIXEL_BASE_HOLD",
    "ORION_METER_DETECTOR",
    "ORION_METER_DETECTOR_SYNC_ACQUIRE",
    "ORION_LATENCY_REGIME_REOPEN_SOFT",
    "ORION_METER_PARTIAL_OCCLUSION",
}

SHIPPED_PROFILE_VALUES = {
    "ORION_METER_PARTIAL_OCCLUSION_MAX_MS": "120",
    "ORION_METER_NEGATIVE_BRIDGE_MAX_MS": "45",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT": "3",
    "ORION_METER_PARTIAL_OCCLUSION_MIN_COLS": "2",
    "ORION_METER_DETECTOR_CONF": "0.35",
    "ORION_METER_PROVIDER_PRIORITY": "dml,cpu",
}

NATIVE_TIMING_PROFILE = {
    "ORION_HORIZON_DEBIAS",
    "ORION_RAMP_SHAPE",
}


def _scrub_reader_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ORION_READER_* var: a customer environment is silent."""
    for name in list(os.environ):
        if name.startswith("ORION_READER_"):
            monkeypatch.delenv(name, raising=False)


def test_shipped_reader_profile_flags_default_on(monkeypatch):
    """A silent (customer) environment must resolve the shipped profile flags ON."""
    _scrub_reader_env(monkeypatch)
    reader = SimpleMeterReader(1280, 720)
    resolved = {env: getattr(reader, attr) for env, attr in SHIPPED_PROFILE.items()}
    assert all(resolved.values()), (
        "customer/rig reader parity regressed — these shipped-profile flags no longer "
        f"default ON in simple_meter_reader.py: "
        f"{sorted(env for env, on in resolved.items() if not on)}. "
        "Every live-graded timing number was measured with them ON "
        "(see native_orion/src/SidecarReaderProfile.h)."
    )


def test_anchor_sub_flags_follow_master_by_default(monkeypatch):
    """N1/N3 default to the master anchor flag, so the shipped profile carries them."""
    _scrub_reader_env(monkeypatch)
    reader = SimpleMeterReader(1280, 720)
    assert reader._anchor_n1 is True
    assert reader._anchor_n3 is True


def test_explicit_operator_zero_still_wins(monkeypatch):
    """The profile contract's other half: an explicit env value (even '0') is honored,
    so a support instruction or A/B can still disable one flag."""
    _scrub_reader_env(monkeypatch)
    for env in SHIPPED_PROFILE:
        monkeypatch.setenv(env, "0")
    reader = SimpleMeterReader(1280, 720)
    for env, attr in SHIPPED_PROFILE.items():
        assert getattr(reader, attr) is False, f"{env}=0 must disable {attr}"


def test_native_profile_contains_every_live_only_timing_flag_and_is_applied():
    """Customer startup, not a local launcher, must own calibrated values."""

    header = (ROOT / "native_orion" / "src" / "SidecarReaderProfile.h").read_text(
        encoding="utf-8"
    )
    session = (ROOT / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )

    for flag in {*SHIPPED_PROFILE, *LIVE_ONLY_PROFILE}:
        assert f'"{flag}"' in header
    for name, value in SHIPPED_PROFILE_VALUES.items():
        assert f'{{"{name}", "{value}"}}' in header
    assert '#include "SidecarReaderProfile.h"' in session
    assert "applyShippedReaderProfile(env, !productionBuild)" in session
    # [ORION_METER_PROPOSER 2026-09-10] the box proposer is a SETTING (meter_proposer), not a
    # profile pin: the header owns the key and the session applies the user's value.
    assert 'kMeterProposerEnvKey[] = "ORION_METER_PROPOSER"' in header
    assert "applyMeterProposerSetting(" in session and "meterProposer" in session
    assert 'QStringLiteral("ORION_METER_MODEL")' in session
    assert "models/orion_meter_detector.onnx" in session

    main = (ROOT / "native_orion" / "src" / "main.cpp").read_text(
        encoding="utf-8"
    )
    for flag in NATIVE_TIMING_PROFILE:
        assert f'"{flag}"' in header
    assert '#include "SidecarReaderProfile.h"' in main
    assert "applyShippedNativeTimingProfile(preserveTimingOverrides)" in main
    assert "ORION_TIMING_PROFILE_ID" in header


def test_production_profile_overrides_inherited_values_but_dev_preserves_them():
    header = (ROOT / "native_orion" / "src" / "SidecarReaderProfile.h").read_text(
        encoding="utf-8"
    )
    assert "preserveExplicitOverrides && env.contains(key)" in header
    assert "QString::fromLatin1(setting.value)" in header
    assert 'origin = QLatin1String("production")' in header


def test_partial_occlusion_python_defaults_match_certified_profile(monkeypatch):
    for name in {*LIVE_ONLY_PROFILE, *SHIPPED_PROFILE_VALUES}:
        monkeypatch.delenv(name, raising=False)

    reader = SimpleMeterReader(1280, 720)

    assert reader._det_occ_fill is True
    assert reader._det_occ_max_gap_s == pytest.approx(0.120)
    assert reader._det_occ_negative_bridge_s == pytest.approx(0.045)
    assert reader._det_occ_min_direct == 3
    assert reader._det_occ_min_cols == 2
