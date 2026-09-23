"""[2026-09-21] The signing key may stay ENCRYPTED on disk: the packager decrypts it in memory
from ORION_UPDATE_SIGNING_KEY_PASSPHRASE (or an interactive prompt), never from the command
line, and never writes a decrypted copy. Keys here are throwaway test keys generated per run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pk():
    spec = importlib.util.spec_from_file_location(
        "package_orion_release_pw", ROOT / "tools" / "package_orion_release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["package_orion_release_pw"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_key(path: Path, passphrase: bytes | None) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8, enc))
    return key


def _pub(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_unencrypted_key_still_loads_without_a_passphrase(pk, tmp_path, monkeypatch):
    monkeypatch.delenv(pk.SIGNING_KEY_PASSPHRASE_ENV, raising=False)
    key = _write_key(tmp_path / "plain.pem", None)
    loaded = pk.load_signing_key(tmp_path / "plain.pem", package_dir=tmp_path / "pkg")
    assert _pub(loaded) == _pub(key)


def test_encrypted_key_loads_from_the_passphrase_env(pk, tmp_path, monkeypatch):
    key = _write_key(tmp_path / "enc.pem", b"correct horse")
    monkeypatch.setenv(pk.SIGNING_KEY_PASSPHRASE_ENV, "correct horse")
    loaded = pk.load_signing_key(tmp_path / "enc.pem", package_dir=tmp_path / "pkg")
    assert _pub(loaded) == _pub(key)
    assert b"ENCRYPTED" in (tmp_path / "enc.pem").read_bytes()   # still encrypted on disk


def test_encrypted_key_without_passphrase_fails_closed_non_interactively(pk, tmp_path, monkeypatch):
    _write_key(tmp_path / "enc.pem", b"correct horse")
    monkeypatch.delenv(pk.SIGNING_KEY_PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr(pk.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="encrypted"):
        pk.load_signing_key(tmp_path / "enc.pem", package_dir=tmp_path / "pkg")


def test_wrong_passphrase_is_refused_without_echoing_it(pk, tmp_path, monkeypatch):
    _write_key(tmp_path / "enc.pem", b"correct horse")
    monkeypatch.setenv(pk.SIGNING_KEY_PASSPHRASE_ENV, "battery staple")
    with pytest.raises(SystemExit) as excinfo:
        pk.load_signing_key(tmp_path / "enc.pem", package_dir=tmp_path / "pkg")
    assert "passphrase" in str(excinfo.value)
    assert "battery staple" not in str(excinfo.value)


def test_interactive_prompt_is_used_when_no_env_and_a_tty(pk, tmp_path, monkeypatch):
    key = _write_key(tmp_path / "enc.pem", b"correct horse")
    monkeypatch.delenv(pk.SIGNING_KEY_PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr(pk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(pk.getpass, "getpass", lambda prompt="": "correct horse")
    loaded = pk.load_signing_key(tmp_path / "enc.pem", package_dir=tmp_path / "pkg")
    assert _pub(loaded) == _pub(key)
