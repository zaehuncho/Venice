"""Release-filter coverage for local developer/test debris."""

from pathlib import Path
import re

from tools.release_filter_policy import is_crown_jewel_python, is_forbidden_file_name


def test_transient_test_transcripts_are_forbidden_but_license_text_is_allowed():
    transient = (
        "focused_tempo.txt",
        "focused_tempo2.txt",
        "focused_trace.txt",
        "full_tempo_latest.txt",
        "full_tempo_prod.txt",
        "tempo_focused_latest.txt",
        "test_output.txt",
        "test_output2.txt",
    )

    assert all(is_forbidden_file_name(name) for name in transient)
    assert not is_forbidden_file_name("LICENSE.txt")
    assert not is_forbidden_file_name("README.txt")


def test_packet_delay_lab_artifacts_are_never_release_files():
    """Diagnostic traffic-manipulation labs are not customer runtime inputs."""

    assert is_forbidden_file_name("delay_test.py")
    assert is_forbidden_file_name("ADAPTIVE_DELAY_PLAN.md")


def test_decoder_pipe_identity_is_compiled_tested_and_never_shipped_as_source():
    root = Path(__file__).resolve().parents[1]
    verifier = (root / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    sidecar_builder = (root / "scripts" / "build_orion_sidecar.ps1").read_text(
        encoding="utf-8"
    )

    # The Python sidecar test gate runs for both standard verification and the
    # strict invocation before production packaging begins.
    assert "    decoder_pipe_identity.py `" in verifier
    assert "        tests\\test_decoder_pipe_identity.py `" in verifier
    strict_boundary = verifier.index("if ($StrictSecurity)")
    assert verifier.index("    decoder_pipe_identity.py `") < strict_boundary
    assert verifier.index("        tests\\test_decoder_pipe_identity.py `") < strict_boundary
    # Keep this explicit even though Nuitka currently discovers the module via
    # chiaki_backend: a future lazy-import refactor must not omit the gate.
    assert "    --include-module=decoder_pipe_identity `" in sidecar_builder
    assert is_crown_jewel_python(Path("decoder_pipe_identity.py"))


def test_transient_test_transcripts_are_dropped_from_recursive_runtime_copy(tmp_path):
    # Import lazily so the test exercises the packager's canonical shared policy.
    from tools.package_orion_release import copy_tree, scan_forbidden

    source = tmp_path / "runtime-source"
    destination = tmp_path / "runtime-package"
    source.mkdir()
    (source / "OrionStream.exe").write_bytes(b"runtime")
    (source / "focused_trace.txt").write_text("obsolete failure", encoding="utf-8")
    (source / "test_output2.txt").write_text("obsolete failure", encoding="utf-8")
    (source / "LICENSE.txt").write_text("runtime license", encoding="utf-8")

    copy_tree(source, destination)

    assert (destination / "OrionStream.exe").is_file()
    assert (destination / "LICENSE.txt").is_file()
    assert not (destination / "focused_trace.txt").exists()
    assert not (destination / "test_output2.txt").exists()
    assert scan_forbidden(destination) == []


def test_verifier_builds_every_registered_native_test_executable():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    cmake = (root / "native_orion" / "CMakeLists.txt").read_text(encoding="utf-8")
    registered = set(re.findall(r"add_test\(NAME\s+(\w+)", cmake))
    assert registered

    build_lines = {
        "standard": next(
            line for line in script.splitlines()
            if "cmake --build native_orion\\build " in line
        ),
        "strict": next(
            line for line in script.splitlines()
            if "cmake --build $ProdBuild" in line
        ),
    }
    for gate, build_line in build_lines.items():
        for target in registered:
            assert target in build_line, (
                f"{gate} verifier registers but does not build {target}"
            )
