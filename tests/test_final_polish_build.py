"""The named native verification build must refresh the pure math checks too."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_math_checks_are_a_native_regression_build_dependency():
    cmake = (ROOT / "native_orion/CMakeLists.txt").read_text(encoding="utf-8")
    assert re.search(
        r"add_dependencies\(\s*OrionNativeTests\s+GreenWindowMathChecks\s*\)", cmake
    ), "the normal named-target gate must not reuse a stale math-check executable"
    assert "add_test(NAME GreenWindowMathChecks COMMAND GreenWindowMathChecks)" in cmake
