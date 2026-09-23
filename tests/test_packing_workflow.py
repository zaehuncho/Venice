import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_lethe_release", ROOT / "tools" / "security" / "pack_lethe_release.py"
)
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


def test_packer_command_requires_input_and_output_placeholders():
    with pytest.raises(ValueError):
        pack.format_packer_command("packer.exe {input}", Path("a.exe"), Path("b.exe"))
    with pytest.raises(ValueError):
        pack.format_packer_command("packer.exe {output}", Path("a.exe"), Path("b.exe"))


def test_packer_command_quotes_substituted_paths():
    command = pack.format_packer_command(
        "packer.exe {input} {output} -pf profile.vmp",
        Path("C:/Program Files/Orion/OrionStaff.exe"),
        Path("C:/Temp/packed staff.exe"),
    )

    assert '"C:\\Program Files\\Orion\\OrionStaff.exe"' in command or '"C:/Program Files/Orion/OrionStaff.exe"' in command
    assert '"C:\\Temp\\packed staff.exe"' in command or '"C:/Temp/packed staff.exe"' in command
    assert "{input}" not in command
    assert "{output}" not in command


def test_parse_targets_normalizes_and_rejects_empty():
    assert pack.parse_targets("OrionOwner.exe, subdir\\SecurityCore.dll ") == [
        "OrionOwner.exe",
        "subdir/SecurityCore.dll",
    ]
    with pytest.raises(ValueError):
        pack.parse_targets(" , ")


def test_production_defaults_are_exe_only():
    assert pack.DEFAULT_TARGETS == (
        "OrionOwner.exe",
        "OrionStaff.exe",
        "OrionNative.exe",
        "OrionUpdater.exe",
    )
    pack.validate_release_targets(list(pack.DEFAULT_TARGETS))


@pytest.mark.parametrize("target", ["SecurityCore.dll", "subdir/VisionCore.DLL"])
def test_production_release_rejects_dll_targets(target):
    with pytest.raises(ValueError, match="loader-safe deferred initialization"):
        pack.validate_release_targets(["OrionNative.exe", target])


def test_run_packer_refuses_dll_before_touching_package(monkeypatch, tmp_path):
    invoked = False

    def unexpected_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(pack, "run", unexpected_run)
    with pytest.raises(ValueError, match="production release packing is EXE-only"):
        pack.run_packer(tmp_path, ["SecurityCore.dll"], "packer {input} {output}")
    assert not invoked


def test_main_refuses_dll_before_copy_or_signing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ORION_UPDATE_SIGNING_KEY_PEM", raising=False)
    copied = False

    def unexpected_copy(*_args, **_kwargs):
        nonlocal copied
        copied = True

    monkeypatch.setattr(pack, "copy_package", unexpected_copy)
    with pytest.raises(SystemExit) as exc:
        pack.main([
            "--verify-only",
            "--input",
            str(tmp_path / "unused"),
            "--targets",
            "OrionNative.exe,SecurityCore.dll",
        ])
    assert exc.value.code == 2
    assert "loader-safe deferred initialization" in capsys.readouterr().err
    assert not copied


def test_verify_only_needs_existing_package_and_never_requires_a_key(monkeypatch, tmp_path):
    # FIX #3: --verify-only is READ-ONLY. It validates an existing package against the
    # pinned key and never copies/regenerates/signs, so it must NOT demand a signing key.
    # With an absent input it fails because the package is missing, not for lack of a key.
    monkeypatch.delenv("ORION_UPDATE_SIGNING_KEY_PEM", raising=False)
    with pytest.raises(SystemExit) as exc:
        pack.main(["--verify-only", "--input", str(tmp_path / "unused")])
    message = str(exc.value).lower()
    assert "signing-key" not in message and "signing key" not in message
    assert "verify-only" in message or "package" in message
