"""Pin the SimpleMeterReader env-flag DEFAULTS to the shipped reader profile.

WHY THIS EXISTS. Every timing number this project has ever calibrated against was
measured with three reader flags ON, historically set only by the gitignored dev
launcher (run_orion.local.ps1). A customer build inherits ZERO ORION_* variables,
so customer/rig parity survives ONLY because the module defaults now match the
launcher. Nothing wired the native-side pin (native_orion/src/SidecarReaderProfile.h
``applyShippedReaderProfile`` is included nowhere), so these Python defaults are
the single load-bearing edge — this test makes it impossible for them to silently
regress. The flag set and its evidence (production-wired A/B: zero detection
flips, one-sided +fill bias when OFF) live in SidecarReaderProfile.h; keep the
two lists in sync when a flag clears the bar for the profile.

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
