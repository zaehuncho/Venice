"""Failure-atomic publication of the package, update ZIP and update manifest."""

import importlib.util
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "package_orion_release", _ROOT / "tools" / "package_orion_release.py"
)
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)


def _publishable_stage(path: Path) -> None:
    path.mkdir()
    (path / "OrionSidecar.exe").write_bytes(b"sidecar-new")
    (path / "ORION_SIDECAR_BUILD.json").write_text("{}", encoding="utf-8")
    (path / "security_policy.json").write_text("{}", encoding="utf-8")
    (path / pkg.RELEASE_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    for rel in pkg.COMPILED_SERVICE_REQUIRED_FILES:
        entry = path / rel
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"service-new")


def test_archive_failure_preserves_previous_zip(monkeypatch, tmp_path):
    import zipfile

    package = tmp_path / "package"
    package.mkdir()
    (package / "OrionNative.exe").write_bytes(b"new-runtime")
    out = tmp_path / "out"
    out.mkdir()
    previous = out / "orion-package-1.2.3.zip"
    previous.write_bytes(b"previous-good-zip")
    original_writestr = zipfile.ZipFile.writestr

    def fail_write(self, *args, **kwargs):
        original_writestr(self, *args, **kwargs)
        raise OSError("injected ZIP write fault")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(OSError, match="injected ZIP write fault"):
        pkg.create_archive(package, "1.2.3", out)
    assert previous.read_bytes() == b"previous-good-zip"
    assert not list(out.glob("*.tmp-*"))


def test_release_unit_publication_rolls_back_on_second_swap(monkeypatch, tmp_path):
    package = tmp_path / "orion-package"
    package.mkdir()
    (package / "old.txt").write_bytes(b"old-package")
    archive = tmp_path / "orion-package-1.2.3.zip"
    archive.write_bytes(b"old-zip")
    manifest = tmp_path / "update_manifest.unsigned.json"
    manifest.write_bytes(b"old-manifest")

    stage = tmp_path / ".orion-package.staging-test"
    _publishable_stage(stage)
    staged_zip = tmp_path / ".orion-package-1.2.3.zip.staging-test"
    staged_zip.write_bytes(b"new-zip")
    staged_manifest = tmp_path / ".update_manifest.staging-test"
    staged_manifest.write_bytes(b"new-manifest")

    original_replace = Path.replace

    def fail_archive_publish(self, target):
        if self == staged_zip and Path(target) == archive:
            raise OSError("injected second-swap fault")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_archive_publish)
    with pytest.raises(OSError, match="injected second-swap fault"):
        pkg.publish_release_unit(stage, staged_zip, staged_manifest,
                                 package, archive, manifest, require_signature=False)
    assert (package / "old.txt").read_bytes() == b"old-package"
    assert archive.read_bytes() == b"old-zip"
    assert manifest.read_bytes() == b"old-manifest"


def test_release_unit_publication_commits_coherent_triple(tmp_path):
    package = tmp_path / "orion-package"
    package.mkdir()
    (package / "old.txt").write_bytes(b"old-package")
    archive = tmp_path / "orion-package-1.2.3.zip"
    archive.write_bytes(b"old-zip")
    manifest = tmp_path / "update_manifest.unsigned.json"
    manifest.write_bytes(b"old-manifest")
    stage = tmp_path / ".orion-package.staging-test"
    _publishable_stage(stage)
    staged_zip = tmp_path / ".orion-package-1.2.3.zip.staging-test"
    staged_zip.write_bytes(b"new-zip")
    staged_manifest = tmp_path / ".update_manifest.staging-test"
    staged_manifest.write_bytes(b"new-manifest")

    pkg.publish_release_unit(stage, staged_zip, staged_manifest,
                             package, archive, manifest, require_signature=False,
                             archive_dir=tmp_path / "archive")
    assert (package / "OrionSidecar.exe").read_bytes() == b"sidecar-new"
    assert not (package / "old.txt").exists()
    assert archive.read_bytes() == b"new-zip"
    assert manifest.read_bytes() == b"new-manifest"
    archived = list((tmp_path / "archive").glob("orion-package-*"))
    assert len(archived) == 1
    assert (archived[0] / "old.txt").read_bytes() == b"old-package"
    assert (archived[0] / "ARCHIVE_MANIFEST.json").is_file()


def test_manifest_swap_failure_restores_all_three(monkeypatch, tmp_path):
    package = tmp_path / "orion-package"
    package.mkdir()
    (package / "old.txt").write_bytes(b"old-package")
    archive = tmp_path / "orion-package-1.2.3.zip"
    archive.write_bytes(b"old-zip")
    manifest = tmp_path / "update_manifest.unsigned.json"
    manifest.write_bytes(b"old-manifest")
    stage = tmp_path / ".orion-package.staging-test"
    _publishable_stage(stage)
    staged_zip = tmp_path / ".orion-package-1.2.3.zip.staging-test"
    staged_zip.write_bytes(b"new-zip")
    staged_manifest = tmp_path / ".update_manifest.staging-test"
    staged_manifest.write_bytes(b"new-manifest")
    original_replace = Path.replace

    def fail_manifest_publish(self, target):
        if self == staged_manifest and Path(target) == manifest:
            raise OSError("injected third-swap fault")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_manifest_publish)
    with pytest.raises(OSError, match="injected third-swap fault"):
        pkg.publish_release_unit(stage, staged_zip, staged_manifest,
                                 package, archive, manifest, require_signature=False)
    assert (package / "old.txt").read_bytes() == b"old-package"
    assert archive.read_bytes() == b"old-zip"
    assert manifest.read_bytes() == b"old-manifest"


def test_invalid_artifact_url_is_rejected_before_staging(monkeypatch, tmp_path):
    package = tmp_path / "orion-package"
    package.mkdir()
    (package / "old.txt").write_bytes(b"old-package")
    monkeypatch.setattr(pkg, "PACKAGE_DIR", package)
    monkeypatch.setattr(pkg, "RELEASE_DIR", tmp_path)
    monkeypatch.setattr(pkg, "sidecar_dist_dir", lambda **_kw: tmp_path)
    monkeypatch.setattr(pkg, "create_package_staging_dir",
                        lambda _dir: pytest.fail("invalid metadata reached staging"))
    monkeypatch.setattr(sys, "argv", ["package_orion_release.py", "--allow-dev-build",
                                     "--artifact-url", "http://example.invalid/archive.zip"])
    with pytest.raises(SystemExit, match="artifact_url must be https"):
        pkg.main()
    assert (package / "old.txt").read_bytes() == b"old-package"
