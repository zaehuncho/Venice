from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight_tip_batch.ps1"


def test_preflight_requires_an_explicit_matching_shot_lead_arm():
    source = PREFLIGHT.read_text(encoding="utf-8")

    assert "[double]$ExpectedShotLeadMs = 300.0" in source
    assert "actuation_lead_ms - $ExpectedShotLeadMs" in source
    assert "actuation_lead_ms != declared arm $ExpectedShotLeadMs" in source
    assert "[double]$settings.actuation_lead_ms -ne 300.0" not in source


def test_preflight_labels_nonreference_leads_as_ab_treatments():
    source = PREFLIGHT.read_text(encoding="utf-8")

    assert "declared single-treatment A/B arm" in source
    assert "-ExpectedShotLeadMs 285" in source


def test_preflight_pins_every_other_timing_authority():
    source = PREFLIGHT.read_text(encoding="utf-8")

    assert "[double]$ExpectedTipTimingMs = 421.3" in source
    assert "$ExpectedTipTimingMs -gt 0" not in source
    assert "effectiveTipMs - $ExpectedTipTimingMs" in source
    assert "tip_gate_enabled -ne $true" in source
    assert "tip_gate_cap_ms - $ExpectedTipGateCapMs" in source
    assert "active_profile" in source and "$ExpectedProfile" in source
    assert "Get-ProfileLearningPath $actualProfile" in source


def test_preflight_checks_the_loaded_timing_modules_for_freshness():
    source = PREFLIGHT.read_text(encoding="utf-8")

    assert "AutomationCore.dll" in source
    assert "OrionCommon.dll" in source
    assert "stale loaded module" in source


def test_preflight_is_valid_powershell_syntax():
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$null=[scriptblock]::Create([IO.File]::ReadAllText('{PREFLIGHT}'))"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
