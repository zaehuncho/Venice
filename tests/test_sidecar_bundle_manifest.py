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


def _bundle_models(root: Path, dist: Path) -> None:
    for relative in sidecar.MODEL_INPUTS:
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
    }
    assert set(document["models"]) == set(sidecar.MODEL_INPUTS)
    assert set(document["files"]) == {
        sidecar.EXECUTABLE_NAME,
        "python312.dll",
        *sidecar.MODEL_INPUTS,
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
    assert set(document["models"]) == set(sidecar.MODEL_INPUTS)
    assert set(sidecar.MODEL_INPUTS) <= set(document["files"])

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
    assert "Test-OrionSidecarModelsFresh" in script
    assert "--include-package=scipy" not in script
