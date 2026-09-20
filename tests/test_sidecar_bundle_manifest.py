import json
from pathlib import Path

import pytest

from tools import sidecar_bundle_manifest as sidecar


def _source_tree(root: Path) -> None:
    entry = root / "native_orion" / "backend" / "autogreen_sidecar.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('sidecar')\n", encoding="utf-8")
    (root / "decoder_pipe_identity.py").write_text("ENABLED = True\n", encoding="utf-8")
    (root / "simple_meter_reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    models = root / "models"
    models.mkdir()
    production_models = Path(__file__).resolve().parents[1] / "models"
    for name in ("tip_registration.json", "latency_factory_prior.json"):
        (models / name).write_bytes((production_models / name).read_bytes())
    # Manifest validation is intentionally dependency-free; the build script
    # performs an onnxruntime load/shape check before Nuitka compilation.
    (models / "orion_meter_detector.onnx").write_bytes(
        b"\x08orion-test-onnx-fixture" * 64
    )
    _reader_data_tree(root)


def _reader_data_tree(root: Path) -> None:
    """[ORION_BANNER_VERDICT_LIVE 2026-09-14] The live banner grader's two runtime inputs.

    Synthesised rather than copied from tools/timing/: the real grader and its template
    library are untracked dev tooling, and this suite must pass on a clean checkout.
    """
    timing = root / "tools" / "timing"
    timing.mkdir(parents=True, exist_ok=True)
    (timing / "panel_grade.py").write_text(
        "LIB_PATH = 'panel_templates.npz'\n", encoding="utf-8"
    )
    # npz is a zip container; the manifest checks the magic, not the arrays.
    (timing / "panel_templates.npz").write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)


def _bundle_models(root: Path, dist: Path) -> None:
    for relative in sidecar.BUNDLE_DATA_INPUTS:
        source = root / relative
        destination = dist / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def _source_identity(root: Path) -> str:
    return sidecar.build_input_digest(
        sidecar.source_hashes(root), sidecar.model_input_hashes(root)
    )


def test_sidecar_manifest_binds_executable_and_all_root_sources(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    executable = dist / sidecar.EXECUTABLE_NAME
    executable.write_bytes(b"compiled-current-code")
    (dist / "python312.dll").write_bytes(b"runtime-dependency")
    _bundle_models(tmp_path, dist)
    identity = _source_identity(tmp_path)

    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity) == []

    document = json.loads((dist / sidecar.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert document["schema"] == sidecar.SCHEMA
    assert set(document["sources"]) == {
        "decoder_pipe_identity.py",
        "native_orion/backend/autogreen_sidecar.py",
        "simple_meter_reader.py",
        # not a repo-root module, but --include-module compiles it INTO the executable,
        # so its bytes belong to the build identity like any root module's
        "tools/timing/panel_grade.py",
    }
    assert set(document["models"]) == set(sidecar.BUNDLE_DATA_INPUTS)
    assert set(document["files"]) == {
        sidecar.EXECUTABLE_NAME,
        "python312.dll",
        *sidecar.BUNDLE_DATA_INPUTS,
    }
    assert document["build_input_digest"] == identity
    assert document["source_digest"] == identity


def test_sidecar_manifest_rejects_stale_source_or_binary(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    executable = dist / sidecar.EXECUTABLE_NAME
    executable.write_bytes(b"compiled-current-code")
    _bundle_models(tmp_path, dist)
    identity = _source_identity(tmp_path)
    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)

    (tmp_path / "simple_meter_reader.py").write_text("VALUE = 2\n", encoding="utf-8")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("source files changed" in failure for failure in failures)
    assert any("source identity mismatch" in failure for failure in failures)

    (tmp_path / "simple_meter_reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    executable.write_bytes(b"tampered-binary")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("sidecar hash mismatch" in failure for failure in failures)
    assert any("bundle files changed" in failure for failure in failures)


def test_sidecar_manifest_rejects_tampered_standalone_dependency(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / sidecar.EXECUTABLE_NAME).write_bytes(b"compiled-current-code")
    dependency = dist / "cv2.pyd"
    dependency.write_bytes(b"trusted-extension")
    _bundle_models(tmp_path, dist)
    identity = _source_identity(tmp_path)
    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)

    dependency.write_bytes(b"tampered-extension")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("sidecar bundle files changed" in failure for failure in failures)


def test_sidecar_manifest_cannot_reattest_binary_with_old_embedded_sources(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    executable = dist / sidecar.EXECUTABLE_NAME
    executable.write_bytes(b"old-compiled-code")
    _bundle_models(tmp_path, dist)
    old_identity = _source_identity(tmp_path)
    (tmp_path / "simple_meter_reader.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="different source digest"):
        sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: old_identity)


def test_prepare_build_writes_current_identity_module(tmp_path):
    _source_tree(tmp_path)
    path, digest = sidecar.prepare_build(tmp_path)

    assert path == tmp_path / sidecar.BUILD_ID_MODULE
    assert digest == _source_identity(tmp_path)
    assert digest in path.read_text(encoding="utf-8")


def test_prepare_build_fails_closed_when_a_runtime_model_is_missing_or_invalid(tmp_path):
    _source_tree(tmp_path)
    factory = tmp_path / "models" / "latency_factory_prior.json"
    factory.unlink()
    with pytest.raises(FileNotFoundError, match="latency_factory_prior"):
        sidecar.prepare_build(tmp_path)

    factory.write_text('{"schema":"wrong"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid sidecar model input"):
        sidecar.prepare_build(tmp_path)

    _source_tree(tmp_path / "detector")
    detector = tmp_path / "detector" / sidecar.DETECTOR_MODEL_INPUT
    detector.write_bytes(b"\0" * 2048)
    with pytest.raises(ValueError, match="detector model has invalid content"):
        sidecar.prepare_build(tmp_path / "detector")


def test_sidecar_manifest_rejects_new_root_module_and_missing_manifest(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / sidecar.EXECUTABLE_NAME).write_bytes(b"compiled-current-code")
    _bundle_models(tmp_path, dist)

    assert sidecar.verify_manifest(tmp_path, dist) == [
        f"missing {sidecar.MANIFEST_NAME}"
    ]
    identity = _source_identity(tmp_path)
    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    (tmp_path / "new_detector_path.py").write_text("ENABLED = True\n", encoding="utf-8")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("new source files" in failure for failure in failures)


def test_sidecar_manifest_covers_and_rejects_stale_runtime_models(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _bundle_models(tmp_path, dist)
    (dist / sidecar.EXECUTABLE_NAME).write_bytes(b"compiled-current-code")
    identity = _source_identity(tmp_path)

    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    document = json.loads((dist / sidecar.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(document["models"]) == set(sidecar.BUNDLE_DATA_INPUTS)
    assert set(sidecar.BUNDLE_DATA_INPUTS) <= set(document["files"])

    model = dist / "models" / "latency_factory_prior.json"
    model.write_text('{"schema":"tampered"}\n', encoding="utf-8")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("bundled model differs from source" in failure for failure in failures)
    assert any("sidecar bundle files changed" in failure for failure in failures)


def test_source_model_change_invalidates_embedded_identity_and_manifest(tmp_path):
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _bundle_models(tmp_path, dist)
    (dist / sidecar.EXECUTABLE_NAME).write_bytes(b"compiled-current-code")
    old_identity = _source_identity(tmp_path)
    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: old_identity)

    factory_path = tmp_path / "models" / "latency_factory_prior.json"
    factory = json.loads(factory_path.read_text(encoding="utf-8"))
    factory["profiles"][0]["mean_ms"] += 1.0
    factory_path.write_text(json.dumps(factory), encoding="utf-8")
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: old_identity)
    assert any("model inputs changed" in failure for failure in failures)
    assert any("build input digest mismatch" in failure for failure in failures)
    assert any("source identity mismatch" in failure for failure in failures)


def test_sidecar_build_explicitly_bundles_dependency_free_predictor():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "build_orion_sidecar.ps1").read_text(encoding="utf-8")
    assert "--include-module=latency_estimator" in script
    assert "--include-module=tip_registration_infer" in script
    assert "models/tip_registration.json" in script.replace("\\", "/")
    assert "models/latency_factory_prior.json" in script.replace("\\", "/")
    assert "models/orion_meter_detector.onnx" in script.replace("\\", "/")
    assert "--include-module=meter_detector_yolo" in script
    # [ORION_PILL_RULER 2026-09-19] simple_meter_reader imports pill_fill_ruler inside the
    # Pill branch and swallows the ImportError, so Nuitka cannot see it and a missing module
    # would SILENTLY fall back to the box-relative ruler in a compiled build instead of
    # failing loudly. That is a shipped-feature-off, so it is pinned here.
    assert "--include-module=pill_fill_ruler" in script
    assert "--include-module=onnxruntime" in script
    assert "--include-package=onnxruntime" not in script
    # [ORION_SIDECAR_NO_CUDA 2026-09-19] Nuitka bundles every capi/onnxruntime*.dll; a stray
    # CUDA/TensorRT provider in the build interpreter must never reach the DirectML bundle.
    assert "--noinclude-dlls=onnxruntime/capi/onnxruntime_providers_cuda*" in script
    assert "--noinclude-dlls=onnxruntime/capi/onnxruntime_providers_tensorrt*" in script
    assert "onnxruntime_providers_(cuda|tensorrt)" in script
    assert "onnxruntime" not in next(
        line for line in script.splitlines() if "--nofollow-import-to=" in line
    )
    assert "Test-OrionSidecarModelsFresh" in script
    assert "--detector-smoke-file" in script
    assert "Start-Process -FilePath $SmokeExe" in script
    assert "-WindowStyle Hidden" in script
    assert "-Wait" in script
    assert "DmlExecutionProvider" in script
    assert "p90_ms -gt 35.0" in script
    assert "--include-package=scipy" not in script


def test_sidecar_python_capability_probe_cannot_abort_candidate_scan():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "build_orion_sidecar.ps1").read_text(encoding="utf-8")

    assert "function Test-SidecarPythonCapability" in script
    assert "System.Diagnostics.ProcessStartInfo" in script
    assert "RedirectStandardError = $true" in script
    assert "if (Test-SidecarPythonCapability -Interpreter $full)" in script
    assert "& $full -c" not in script
    probe = next(line for line in script.splitlines() if "$probe =" in line)
    assert "windows_capture" in probe and "cv2" in probe
    assert "m.version('windows-capture') == '2.0.0'" in probe


# ─────────────────────────────────────────────────────────────────────────────
# [ORION_BANNER_VERDICT_LIVE 2026-09-14] Shipping the live banner grader.
#
# banner_verdict_live.py grades the game's own shot-feedback panel by IMPORTING
# tools/timing/panel_grade.py, and the grader needs its template library at runtime.
# Neither was in the bundle, so the reader fail-softed off in every shipped build: the
# owner's live tally worked on a dev rig and silently did nothing for a customer.
#
# The grader is a READER, so docs/IP_PROTECTION_PLAN.md says it ships COMPILED: the build
# copies it to the repo-root name orion_panel_grade.py, --include-module compiles it into
# the executable, and the copy is deleted. Its bytes are bound into the SOURCE identity;
# only the npz is a file the dist carries.
# ─────────────────────────────────────────────────────────────────────────────


def test_bundle_binds_the_banner_grader_and_its_template_library(tmp_path):
    # The grader is COMPILED IN, so it is a source input, never a shipped file.
    assert "tools/timing/panel_grade.py" in sidecar.READER_SOURCE_INPUTS
    assert "tools/timing/panel_grade.py" not in sidecar.BUNDLE_DATA_INPUTS
    assert "tools/timing/panel_templates.npz" in sidecar.READER_DATA_INPUTS
    # The models keep their own tuple; BUNDLE_DATA_INPUTS is everything the bundle must
    # carry verbatim, and it is what the embedded identity binds.
    assert set(sidecar.MODEL_INPUTS) < set(sidecar.BUNDLE_DATA_INPUTS)

    _source_tree(tmp_path)
    hashes = sidecar.model_input_hashes(tmp_path)
    assert set(hashes) == set(sidecar.BUNDLE_DATA_INPUTS)
    # ...and the grader's bytes ride in the SOURCE inventory instead.
    assert "tools/timing/panel_grade.py" in sidecar.source_hashes(tmp_path)

    # A change to either file must invalidate the compiled sidecar: the grader is code the
    # bundle executes, and the npz is the template library it grades against.
    before = _source_identity(tmp_path)
    (tmp_path / "tools" / "timing" / "panel_grade.py").write_text(
        "LIB_PATH = 'panel_templates.npz'\nTWEAKED = True\n", encoding="utf-8"
    )
    assert _source_identity(tmp_path) != before

    middle = _source_identity(tmp_path)
    (tmp_path / "tools" / "timing" / "panel_templates.npz").write_bytes(
        sidecar.NPZ_MAGIC + b"\x01" * 128
    )
    assert _source_identity(tmp_path) != middle


def test_bundle_fails_closed_on_a_missing_or_wrong_kind_of_reader_input(tmp_path):
    _source_tree(tmp_path)
    library = tmp_path / "tools" / "timing" / "panel_templates.npz"

    # Ship the grader without its templates and the reader loads, finds an empty library and
    # disables itself for the session -- a silent feature-off. Fail the BUILD instead.
    library.unlink()
    with pytest.raises(FileNotFoundError, match="panel_templates.npz"):
        sidecar.prepare_build(tmp_path)

    library.write_bytes(b"not-an-archive")
    with pytest.raises(ValueError, match="not an npz archive"):
        sidecar.prepare_build(tmp_path)

    library.write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)
    grader = tmp_path / "tools" / "timing" / "panel_grade.py"
    grader.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="reader module is empty"):
        sidecar.prepare_build(tmp_path)

    # The grader is what --include-module compiles; missing, the build must fail rather
    # than produce an executable whose banner reader is quietly absent.
    grader.write_text("LIB_PATH = 'panel_templates.npz'\n", encoding="utf-8")
    grader.unlink()
    with pytest.raises(FileNotFoundError, match="reader source not found"):
        sidecar.prepare_build(tmp_path)


def test_bundled_reader_inputs_must_be_present_in_the_dist(tmp_path):
    """The identity covers the bytes; this covers the LAYOUT.

    The template library must be IN the bundle: the grader is compiled into the executable,
    but an empty library disables the reader for the session, so binding the hash without
    shipping the npz would produce a bundle that passes attestation and cannot read a single
    banner. The grader itself must NOT be in the dist -- shipping it as readable .py is the
    IP leak this arrangement exists to prevent.
    """
    _source_tree(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / sidecar.EXECUTABLE_NAME).write_bytes(b"compiled-current-code")
    _bundle_models(tmp_path, dist)
    identity = _source_identity(tmp_path)
    sidecar.write_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert (dist / "tools" / "timing" / "panel_templates.npz").is_file()
    assert not (dist / "tools" / "timing" / "panel_grade.py").exists()

    (dist / "tools" / "timing" / "panel_templates.npz").unlink()
    failures = sidecar.verify_manifest(tmp_path, dist, identity_reader=lambda _: identity)
    assert any("required bundled model missing" in failure for failure in failures)


def test_build_script_compiles_the_grader_and_ships_only_its_templates():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "build_orion_sidecar.ps1").read_text(encoding="utf-8")
    normalized = script.replace("\\", "/")
    assert "tools/timing/panel_grade.py" in normalized
    assert "tools/timing/panel_templates.npz" in normalized

    # The grader is COMPILED IN under a nameable repo-root module...
    assert "--include-module=orion_panel_grade" in script
    assert "Copy-Item -LiteralPath $SidecarGraderSource -Destination $SidecarGraderShim" \
        in script
    # ...and the copy is removed in a finally, so a failed build cannot leave readable
    # reader source lying in the working tree.
    shim_copy = script.index("-Destination $SidecarGraderShim")
    nuitka = script.index("--include-module=orion_panel_grade")
    cleanup = script.index("Remove-Item -LiteralPath $SidecarGraderShim")
    assert shim_copy < nuitka < cleanup
    assert "} finally {" in script[nuitka:cleanup]

    # The dist carries the npz and NOTHING of the grader: no copy step for the .py, and the
    # reader-data list holds the template library alone.
    assert 'Bundled = Join-Path $Dist "tools\\timing\\panel_grade.py"' not in script
    assert 'Destination = "tools/timing/panel_grade.py"' not in script
    reader_block = script[script.index("$SidecarReaderData = @("):
                          script.index("$SidecarBundledData = @(")]
    assert "panel_templates.npz" in reader_block
    assert "panel_grade.py" not in reader_block

    # The npz is still copied explicitly before the freshness gate, so a failed copy fails
    # the build rather than shipping a reader with an empty template library.
    assert "function Copy-OrionSidecarReaderData" in script
    assert "Copy-OrionSidecarReaderData\n" in script
    assert script.index("Copy-OrionSidecarReaderData\n") < script.index(
        "if (-not (Test-OrionSidecarModelsFresh)) {"
    )
    # The freshness gate covers the reader data too, not just the three models.
    assert "foreach ($model in $SidecarBundledData)" in script


def test_banner_reader_lookup_order_prefers_the_compiled_module(tmp_path, monkeypatch):
    """The ONE lookup that has to work from the bundle AND from source.

    Order, as banner_verdict_live.load_panel_grade() implements it:
      1. `import orion_panel_grade` -- the repo-root name the release build compiles into
         the sidecar. It WINS, so a shipped build never depends on readable reader source.
      2. `import tools.timing.panel_grade` -- the repo namespace package, how the dev
         sidecar and every offline tool run.
      3. an explicit file load of "<root>/tools/timing/panel_grade.py" for root in the
         module directory, the CWD, sys._MEIPASS and the executable's directory -- a source
         checkout reached from an unusual CWD.
    Returns None (a quiet feature-off) when none resolve.
    """
    bvl = pytest.importorskip("banner_verdict_live")

    tried = []

    def _only_compiled(name):
        tried.append(name)
        if name == "orion_panel_grade":
            mod = type("M", (), {"LIB_PATH": "", "MARKER": "compiled"})
            return mod
        raise ImportError(name)

    monkeypatch.setattr(bvl.importlib, "import_module", _only_compiled)
    monkeypatch.delenv("ORION_PANEL_TEMPLATES", raising=False)
    loaded = bvl.load_panel_grade()
    assert loaded is not None and loaded.MARKER == "compiled"
    assert tried == ["orion_panel_grade"], "the compiled module must be tried FIRST"

    # ...and when it is absent, the repo namespace package is next.
    sentinel = type("M", (), {"LIB_PATH": "", "MARKER": "repo"})
    monkeypatch.setattr(bvl.importlib, "import_module",
                        lambda name: (_ for _ in ()).throw(ImportError(name))
                        if name == "orion_panel_grade" else sentinel)
    assert bvl.load_panel_grade() is sentinel

    def _no_package(name):
        raise ImportError(name)

    monkeypatch.setattr(bvl.importlib, "import_module", _no_package)
    # Both file-load roots point at the fixture: the module's own directory (which IS the
    # dist for a compiled sidecar) and the process CWD. Without moving __file__ this would
    # find the developer's real tools/timing/ next to the repo copy of the module.
    monkeypatch.setattr(bvl, "__file__", str(tmp_path / "banner_verdict_live.py"))
    monkeypatch.chdir(tmp_path)
    assert bvl.load_panel_grade() is None, "no tools/ tree must be a quiet feature-off"

    # The bundle layout: the grader at its repository-relative path, the npz beside it.
    timing = tmp_path / "tools" / "timing"
    timing.mkdir(parents=True)
    (timing / "panel_grade.py").write_text(
        "import os\n"
        "HERE = os.path.dirname(os.path.abspath(__file__))\n"
        "LIB_PATH = os.path.join(HERE, 'panel_templates.npz')\n"
        "MARKER = 'bundle'\n",
        encoding="utf-8",
    )
    (timing / "panel_templates.npz").write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)

    loaded = bvl.load_panel_grade()
    assert loaded is not None and loaded.MARKER == "bundle"
    # panel_grade resolves its library relative to its OWN file, so shipping the pair
    # together is what makes the reader arm instead of reporting an empty library.
    assert Path(loaded.LIB_PATH) == timing / "panel_templates.npz"
    assert Path(loaded.LIB_PATH).is_file()


def test_compiled_grader_finds_the_template_library_in_the_dist(tmp_path, monkeypatch):
    """A COMPILED orion_panel_grade has no source file beside it, so panel_grade's own
    self-relative LIB_PATH can name a path Nuitka never created. The npz ships as data and
    load_panel_grade() must find it anyway -- otherwise the reader loads, reports an empty
    template library and disables itself in exactly the builds that were supposed to fix it.
    """
    bvl = pytest.importorskip("banner_verdict_live")
    monkeypatch.delenv("ORION_PANEL_TEMPLATES", raising=False)

    def _compiled(name):
        if name == "orion_panel_grade":
            return type("M", (), {"LIB_PATH": str(tmp_path / "nonexistent.npz")})
        raise ImportError(name)

    monkeypatch.setattr(bvl.importlib, "import_module", _compiled)
    monkeypatch.setattr(bvl, "__file__", str(tmp_path / "banner_verdict_live.py"))
    monkeypatch.chdir(tmp_path)

    # 1. beside the module / the executable, which is where the dist root is
    beside = tmp_path / "panel_templates.npz"
    beside.write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)
    assert Path(bvl.load_panel_grade().LIB_PATH) == beside

    # 2. at the repository-relative path the build script copies it to
    beside.unlink()
    timing = tmp_path / "tools" / "timing"
    timing.mkdir(parents=True)
    (timing / "panel_templates.npz").write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)
    assert Path(bvl.load_panel_grade().LIB_PATH) == timing / "panel_templates.npz"

    # 3. an explicit operator override beats both
    override = tmp_path / "elsewhere.npz"
    override.write_bytes(sidecar.NPZ_MAGIC + b"\x00" * 128)
    monkeypatch.setenv("ORION_PANEL_TEMPLATES", str(override))
    assert Path(bvl.load_panel_grade().LIB_PATH) == override
