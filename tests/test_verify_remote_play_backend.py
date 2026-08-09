from __future__ import annotations

import json

from tools import verify_remote_play_backend


def test_backend_check_requires_custom_chiaki(monkeypatch, capsys):
    monkeypatch.setattr(verify_remote_play_backend, "find_bundled_chiaki_binary", lambda: "")
    monkeypatch.setattr(
        verify_remote_play_backend,
        "find_remote_play_window",
        lambda: (1234, "Unrelated existing window"),
    )

    assert verify_remote_play_backend.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["build_bundle_ready"] is False
    assert payload["runtime_client_available"] is False


def test_backend_check_accepts_bundled_custom_chiaki(monkeypatch, capsys):
    monkeypatch.setattr(
        verify_remote_play_backend,
        "find_bundled_chiaki_binary",
        lambda: r"C:\\orion\\OrionStream.exe",
    )
    monkeypatch.setattr(verify_remote_play_backend, "find_remote_play_window", lambda: (0, ""))

    assert verify_remote_play_backend.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["build_bundle_ready"] is True
    assert payload["runtime_client_available"] is True
