"""Offline credential-rotation controls; no real credentials or cloud calls."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import Mock


SCRIPT = (Path(__file__).resolve().parents[2] / ".codex_artifacts" /
          "nereus-announcements-20260916" / "rotate_nereus_token.py")


def _module(monkeypatch):
    remote = Mock(return_value="active\nenabled\n")
    monkeypatch.setitem(sys.modules, "deploy_nereus", types.SimpleNamespace(remote=remote))
    spec = importlib.util.spec_from_file_location("nereus_rotation_fixture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, remote


def test_already_stored_token_starts_service_even_if_clipboard_clear_fails(monkeypatch, capsys):
    module, remote = _module(monkeypatch)
    ssm = Mock()
    ssm.get_parameter.return_value = {"Parameter": {"Type": "SecureString", "Value": "fixture-token"}}
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: ssm)
    monkeypatch.setattr(module, "identity", lambda value: {"id": module.BOT_ID, "bot": True})
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="fixture-token\n"),
        subprocess.CompletedProcess([], 1),
    ]))
    module.main()
    ssm.put_parameter.assert_not_called()
    remote.assert_called_once()
    assert "rotation:already_stored; ssm:SecureString; service:active/enabled; clipboard:clear_failed" in capsys.readouterr().out


def test_revoked_old_token_replaced_and_read_back(monkeypatch, capsys):
    module, remote = _module(monkeypatch)
    ssm = Mock()
    ssm.get_parameter.side_effect = [
        {"Parameter": {"Type": "SecureString", "Value": "old-fixture"}},
        {"Parameter": {"Type": "SecureString", "Value": "fixture-token"}},
    ]
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: ssm)
    monkeypatch.setattr(module, "identity", lambda value: None if value == "old-fixture" else {"id": module.BOT_ID, "bot": True})
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="fixture-token\n"),
        subprocess.CompletedProcess([], 0),
    ]))
    module.main()
    assert ssm.put_parameter.call_args.kwargs["Value"] == "fixture-token"
    remote.assert_called_once()
    assert "rotation:newly_stored_old_revoked; ssm:SecureString; service:active/enabled; clipboard:cleared" in capsys.readouterr().out
