"""[2026-09-21] windeployqt copies EVERY Qt Quick Controls style because the style is chosen
at runtime, so the shipped package carried ~2,200 files (of ~2,900) that no app ever loads:
FluentWinUI3/Fusion/Imagine/Universal/Windows twice over (OrionNative + OrionStream).
prune_unused_quick_styles() keeps only the style each app pins (Basic for the launcher,
Material + Basic fallback for OrionStream), the shared `impl` helpers, and drops the
per-style DLLs with their directories. A pinned style that is missing fails closed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALL_STYLES = ("Basic", "FluentWinUI3", "Fusion", "Imagine", "Material", "Universal", "Windows")


@pytest.fixture(scope="module")
def pk():
    spec = importlib.util.spec_from_file_location(
        "package_orion_release_qs", ROOT / "tools" / "package_orion_release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["package_orion_release_qs"] = mod
    spec.loader.exec_module(mod)
    return mod


def _qt_tree(app_root: Path, *, styles=ALL_STYLES, native_style=True) -> None:
    controls = app_root / "qml" / "QtQuick" / "Controls"
    controls.mkdir(parents=True)
    (controls / "qmldir").write_text("module QtQuick.Controls", encoding="utf-8")
    (controls / "plugins.qmltypes").write_text("{}", encoding="utf-8")
    (controls / "qtquickcontrols2plugin.dll").write_bytes(b"plugin")
    (controls / "impl").mkdir()
    (controls / "impl" / "qmldir").write_text("module QtQuick.Controls.impl", encoding="utf-8")
    (controls / "impl" / "qtquickcontrols2implplugin.dll").write_bytes(b"impl-plugin")
    for style in styles:
        d = controls / style
        d.mkdir()
        (d / "qmldir").write_text(f"module QtQuick.Controls.{style}", encoding="utf-8")
        (d / f"qtquickcontrols2{style.lower()}styleplugin.dll").write_bytes(b"style-plugin")
        (d / "Button.qml").write_text("Item {}", encoding="utf-8")
        (d / "images").mkdir()
        (d / "images" / "button.png").write_bytes(b"png")
        (app_root / f"Qt6QuickControls2{style}.dll").write_bytes(b"style")
        (app_root / f"Qt6QuickControls2{style}StyleImpl.dll").write_bytes(b"impl")
        # Qt Quick Dialogs ships one file-selector folder per style.
        sel = app_root / "qml" / "QtQuick" / "Dialogs" / "quickimpl" / "qml" / ("+" + style)
        sel.mkdir(parents=True)
        (sel / "FileDialog.qml").write_text("Item {}", encoding="utf-8")
    for core in ("Qt6QuickControls2.dll", "Qt6QuickControls2Impl.dll", "Qt6Quick.dll"):
        (app_root / core).write_bytes(b"core")
    layouts = app_root / "qml" / "QtQuick" / "Layouts"
    layouts.mkdir()
    (layouts / "qmldir").write_text("module QtQuick.Layouts", encoding="utf-8")
    if native_style:
        ns = app_root / "qml" / "QtQuick" / "NativeStyle"
        ns.mkdir()
        (ns / "qmldir").write_text("module QtQuick.NativeStyle", encoding="utf-8")


def _package(tmp_path: Path, pk) -> Path:
    package = tmp_path / "package"
    _qt_tree(package)
    _qt_tree(package / pk.CHIAKI_PACKAGE_RELATIVE_DIR)
    (package / "OrionNative.exe").write_bytes(b"exe")
    (package / pk.CHIAKI_PACKAGE_RELATIVE_DIR / "OrionStream.exe").write_bytes(b"exe")
    return package


def _styles_left(app_root: Path) -> set[str]:
    controls = app_root / "qml" / "QtQuick" / "Controls"
    return {p.name for p in controls.iterdir() if p.is_dir()}


def test_launcher_keeps_only_the_basic_style_it_pins(pk, tmp_path):
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    assert _styles_left(package) == {"Basic", "impl"}
    # ...and the audit's independent read of the same policy agrees the package is clean.
    assert pk.qt_quick_style_violations(package) == []
    # The module's own files and every other module survive untouched.
    controls = package / "qml" / "QtQuick" / "Controls"
    assert (controls / "qmldir").is_file()
    assert (controls / "plugins.qmltypes").is_file()
    assert (controls / "qtquickcontrols2plugin.dll").is_file()
    assert (package / "qml" / "QtQuick" / "Layouts" / "qmldir").is_file()


def test_stream_keeps_material_plus_the_basic_fallback(pk, tmp_path):
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    assert _styles_left(package / pk.CHIAKI_PACKAGE_RELATIVE_DIR) == {"Basic", "Material", "impl"}
    # ...and the launcher tree did NOT inherit Material from the stream's allowlist.
    assert "Material" not in _styles_left(package)


def test_per_style_dlls_leave_with_their_style_and_core_dlls_stay(pk, tmp_path):
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    for style in ("FluentWinUI3", "Fusion", "Imagine", "Material", "Universal", "Windows"):
        assert not (package / f"Qt6QuickControls2{style}.dll").exists(), style
        assert not (package / f"Qt6QuickControls2{style}StyleImpl.dll").exists(), style
    for kept in ("Qt6QuickControls2.dll", "Qt6QuickControls2Impl.dll", "Qt6Quick.dll",
                 "Qt6QuickControls2Basic.dll", "Qt6QuickControls2BasicStyleImpl.dll"):
        assert (package / kept).is_file(), kept
    stream = package / pk.CHIAKI_PACKAGE_RELATIVE_DIR
    assert (stream / "Qt6QuickControls2Material.dll").is_file()
    assert (stream / "Qt6QuickControls2MaterialStyleImpl.dll").is_file()
    assert not (stream / "Qt6QuickControls2Universal.dll").exists()


def test_native_style_module_goes_with_the_windows_style(pk, tmp_path):
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    assert not (package / "qml" / "QtQuick" / "NativeStyle").exists()
    assert not (package / pk.CHIAKI_PACKAGE_RELATIVE_DIR / "qml" / "QtQuick" / "NativeStyle").exists()


def test_native_style_module_survives_when_windows_style_is_pinned(pk, tmp_path, monkeypatch):
    package = _package(tmp_path, pk)
    # The allowlist is ONE policy object read by the packager AND the audit's violation check:
    # both bindings are repointed, exactly as a real allowlist edit would change both.
    policy = sys.modules[pk.qt_quick_style_violations.__module__]
    roots = ((".", frozenset({"Basic", "Windows"})),)
    monkeypatch.setattr(pk, "QT_QUICK_STYLE_ROOTS", roots)
    monkeypatch.setattr(policy, "QT_QUICK_STYLE_ROOTS", roots)
    pk.prune_unused_quick_styles(package)
    assert _styles_left(package) == {"Basic", "Windows", "impl"}
    assert (package / "qml" / "QtQuick" / "NativeStyle" / "qmldir").is_file()


def test_dialog_file_selector_folders_leave_with_their_style(pk, tmp_path):
    """A `+<Style>` selector folder can only activate under that style; once the style is
    unselectable the folder is unreachable, and pruning it makes that a checkable fact."""
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    sel = package / "qml" / "QtQuick" / "Dialogs" / "quickimpl" / "qml"
    assert sorted(p.name for p in sel.iterdir() if p.is_dir()) == ["+Basic"]   # launcher: Basic only
    stream_sel = package / pk.CHIAKI_PACKAGE_RELATIVE_DIR / "qml" / "QtQuick" / "Dialogs" / "quickimpl" / "qml"
    assert sorted(p.name for p in stream_sel.iterdir() if p.is_dir()) == ["+Basic", "+Material"]


def test_a_pinned_style_missing_its_plugin_or_qmldir_or_impl_dll_is_refused(pk, tmp_path):
    victims = ("qml/QtQuick/Controls/Basic/qmldir",
               "qml/QtQuick/Controls/Basic/qtquickcontrols2basicstyleplugin.dll",
               "Qt6QuickControls2BasicStyleImpl.dll",
               "qml/QtQuick/Controls/impl/qtquickcontrols2implplugin.dll",
               "Qt6QuickControls2Impl.dll")
    for i, victim in enumerate(victims):
        package = _package(tmp_path / f"v{i}", pk)    # short: the fixture tree nears MAX_PATH
        (package / victim).unlink()
        with pytest.raises(SystemExit, match="incomplete"):
            pk.prune_unused_quick_styles(package)


def test_a_root_the_source_build_deploys_qt_for_must_be_staged(pk, tmp_path):
    package = tmp_path / "package"
    _qt_tree(package)                       # launcher tree only; the stream tree is absent
    (package / "OrionNative.exe").write_bytes(b"exe")
    stream_root = pk.CHIAKI_PACKAGE_RELATIVE_DIR.as_posix()
    # not required -> the absent stream tree is left alone
    pk.prune_unused_quick_styles(package)
    # required (the source build deploys Qt for the stream) -> refused before any manifest
    with pytest.raises(SystemExit, match="no qml/QtQuick/Controls tree"):
        pk.prune_unused_quick_styles(package, required_roots=frozenset({stream_root}))


def test_the_audit_refuses_an_unpruned_package_and_accepts_a_pruned_one(pk, tmp_path):
    package = _package(tmp_path, pk)
    before = pk.qt_quick_style_violations(package)
    assert any(v.endswith("Controls/FluentWinUI3") for v in before)
    assert any(v.endswith("Qt6QuickControls2Fusion.dll") for v in before)
    assert any(v.endswith("+Universal") for v in before)
    assert any(v.endswith("QtQuick/NativeStyle") for v in before)
    pk.prune_unused_quick_styles(package)
    assert pk.qt_quick_style_violations(package) == []
    # a hand-planted stray style after the prune is caught again
    (package / "qml" / "QtQuick" / "Controls" / "Fusion").mkdir()
    assert pk.qt_quick_style_violations(package) == ["qml/QtQuick/Controls/Fusion"]


def test_reports_what_it_removed(pk, tmp_path, capsys):
    package = _package(tmp_path, pk)
    before = sum(1 for f in package.rglob("*") if f.is_file())
    result = pk.prune_unused_quick_styles(package)
    after = sum(1 for f in package.rglob("*") if f.is_file())
    # launcher: 6 styles, stream: 5 styles
    assert result["styles"] == 11
    assert result["files"] == before - after
    assert "pruned 11 unused Qt Quick Controls styles" in capsys.readouterr().out


def test_missing_pinned_style_fails_closed(pk, tmp_path):
    package = tmp_path / "package"
    _qt_tree(package, styles=("Fusion", "Material"))   # no Basic
    with pytest.raises(SystemExit, match="'Basic'"):
        pk.prune_unused_quick_styles(package)
    # Nothing was removed before the refusal.
    assert _styles_left(package) == {"Fusion", "Material", "impl"}


def test_missing_stream_fallback_fails_closed(pk, tmp_path):
    package = tmp_path / "package"
    _qt_tree(package)
    _qt_tree(package / pk.CHIAKI_PACKAGE_RELATIVE_DIR, styles=("Basic", "Fusion"))   # no Material
    with pytest.raises(SystemExit, match="'Material'"):
        pk.prune_unused_quick_styles(package)


def test_tree_without_a_qt_deployment_is_left_alone(pk, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "OrionNative.exe").write_bytes(b"exe")
    assert pk.prune_unused_quick_styles(package) == {"styles": 0, "files": 0}
    assert sorted(p.name for p in package.iterdir()) == ["OrionNative.exe"]


def test_prune_is_idempotent(pk, tmp_path):
    package = _package(tmp_path, pk)
    pk.prune_unused_quick_styles(package)
    snapshot = sorted(str(p.relative_to(package)) for p in package.rglob("*"))
    assert pk.prune_unused_quick_styles(package) == {"styles": 0, "files": 0}
    assert sorted(str(p.relative_to(package)) for p in package.rglob("*")) == snapshot
