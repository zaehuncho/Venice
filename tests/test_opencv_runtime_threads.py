"""The sidecar bounds OpenCV's pool without changing image work or cadence."""

import ast
import inspect
import logging
import textwrap

import pytest

import remote_play_orchestrator as rpo


@pytest.mark.parametrize(
    ("override", "requested", "invalid"),
    [
        (None, 1, False),
        ("", 1, False),
        ("   ", 1, False),
        ("0", 0, False),
        ("1", 1, False),
        ("2", 2, False),
        (" 4 ", 4, False),
        ("-1", 1, True),
        ("1.5", 1, True),
        ("auto", 1, True),
        ("2147483648", 1, True),
    ],
)
def test_opencv_pool_defaults_and_explicit_overrides(monkeypatch, caplog, override, requested, invalid):
    if override is None:
        monkeypatch.delenv("ORION_CV2_THREADS", raising=False)
    else:
        monkeypatch.setenv("ORION_CV2_THREADS", override)
    calls = []
    monkeypatch.setattr(rpo.cv2, "setNumThreads", calls.append)
    # OpenCV reports one executing thread when setNumThreads(0) disables its pool.
    monkeypatch.setattr(rpo.cv2, "getNumThreads", lambda: max(1, calls[-1]))
    caplog.set_level(logging.INFO, logger=rpo.logger.name)

    effective = rpo._configure_opencv_threads()

    assert calls == [requested]
    assert effective == max(1, requested)
    assert f"OpenCV threads={effective} requested={requested}" in caplog.text
    assert ("invalid ORION_CV2_THREADS" in caplog.text) == invalid
    if not override or not override.strip():
        assert "source=default" in caplog.text
    elif not invalid:
        assert "source=override" in caplog.text


def test_opencv_configuration_only_runs_during_orchestrator_construction():
    """Global OpenCV pool changes must not race live capture/processing work."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(rpo.RemotePlayOrchestrator)))
    call_sites = []
    for method in tree.body[0].body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_configure_opencv_threads"):
                call_sites.append(method.name)
    assert call_sites == ["__init__"]
