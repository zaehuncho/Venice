"""The optional local CUDA launcher must not inherit a DML-only CPU fallback."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "run_orion.local.ps1"


@pytest.mark.parametrize("inherited,expected", [
    (None, "cuda,dml,cpu"),
    ("", "cuda,dml,cpu"),
    ("cpu", "cpu"),
    ("dml,cpu", "dml,cpu"),
])
def test_local_provider_profile_preserves_explicit_override(inherited, expected):
    shell = shutil.which("powershell.exe")
    if not shell or not LAUNCHER.exists():
        pytest.skip("optional Windows source-tree launcher")
    source = LAUNCHER.read_text(encoding="utf-8-sig")
    begin = source.find("# [ORION_DEV_GPU_PROFILE]")
    end = source.find("# [/ORION_DEV_GPU_PROFILE]")
    assert begin >= 0 and end > begin, "local CUDA provider profile is missing"
    # Execute only this bounded provider assignment, never launch the app,
    # read key assignments, touch other environment policy, or stop processes.
    assignment = source[begin:end]
    env = os.environ.copy()
    if inherited is None:
        env.pop("ORION_METER_PROVIDER_PRIORITY", None)
    else:
        env["ORION_METER_PROVIDER_PRIORITY"] = inherited
    result = subprocess.run(
        [shell, "-NoProfile", "-Command",
         assignment + "\nWrite-Output $env:ORION_METER_PROVIDER_PRIORITY"],
        env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == expected
