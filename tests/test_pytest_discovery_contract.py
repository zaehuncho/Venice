"""Root pytest discovery must stay scoped to release-gated active tests."""

from configparser import ConfigParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_root_pytest_discovery_allowlist_and_exclusions():
    parser = ConfigParser()
    assert parser.read(ROOT / "pytest.ini", encoding="utf-8")
    config = parser["pytest"]

    testpaths = set(config.get("testpaths", "").split())
    assert testpaths == {"tests", "tools/security/packer/tests"}
    assert all((ROOT / path).is_dir() for path in testpaths)

    excluded = set(config.get("norecursedirs", "").split())
    assert {
        "tests/backend",
        ".claude",
        ".venv",
        ".venv311",
        "build",
        "build_*",
        "native_orion/build*",
        "tools/diagnostics",
    } <= excluded
