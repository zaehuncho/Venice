"""[CL2-P5 2026-09-23] Shot-record root selection: D: only when it really accepts a file.

The old rule fell back to LOCALAPPDATA only when the D: drive did not exist; a present but
read-only or full D: made the recorder fail later. And a compiled customer build must create
nothing at all unless the switch is set on purpose.
"""

import errno
import logging
import os
import sys

import pytest

import shot_records


@pytest.fixture
def roots(tmp_path, monkeypatch):
    drive = tmp_path / "D_drive"
    local = tmp_path / "localappdata"
    monkeypatch.setattr(shot_records, "_PRIMARY_DRIVE", str(drive))
    env = {"ORION_SHOT_RECORDS": "1", "LOCALAPPDATA": str(local),
           "ORION_SHOT_RECORDS_SESSION": "s1"}
    return drive, local, env


def _create(env):
    return shot_records.ShotRecorder.create(env=env, start_thread=False,
                                            log=logging.getLogger("t"))


def _d_root(drive):
    return os.path.join(str(drive), "NexusVision", "shot_records")


def _l_root(local):
    return os.path.join(str(local), "NexusVision", "shot_records")


def _fail_open_under(prefix, exc, monkeypatch):
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).startswith(prefix):
            raise exc
        return real_open(path, *a, **k)

    monkeypatch.setattr(shot_records, "open", fake_open, raising=False)


def test_valid_d_is_used(roots):
    drive, local, env = roots
    drive.mkdir()
    rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _d_root(drive)
    assert os.listdir(_d_root(drive)) == []          # the probe file is cleaned up
    assert not local.exists()


def test_absent_d_falls_back_to_localappdata(roots):
    drive, local, env = roots
    rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _l_root(local)
    assert not drive.exists()


def test_read_only_d_falls_back(roots, monkeypatch, caplog):
    drive, local, env = roots
    drive.mkdir()
    _fail_open_under(str(drive), PermissionError(errno.EACCES, "read-only"), monkeypatch)
    with caplog.at_level(logging.WARNING):
        rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _l_root(local)
    assert any("not writable" in r.getMessage() for r in caplog.records)


def test_full_d_falls_back_when_the_write_fails(roots, monkeypatch):
    drive, local, env = roots
    drive.mkdir()
    opened = []
    real_open = open
    real_fsync = os.fsync

    def tracking_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    def fake_fsync(fd):
        # A full disk: the directory and the file open fine, the data does not land.
        if opened and opened[-1].startswith(str(drive)):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(shot_records, "open", tracking_open, raising=False)
    monkeypatch.setattr(shot_records.os, "fsync", fake_fsync)
    rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _l_root(local)
    assert not [f for f in os.listdir(_d_root(drive)) if f.endswith(".tmp")]


def test_nothing_writable_disables_without_raising(roots, monkeypatch):
    drive, local, env = roots
    drive.mkdir()
    _fail_open_under(str(drive.parent), OSError(errno.ENOSPC, "full"), monkeypatch)
    assert _create(env) is None


def test_unwritable_explicit_root_falls_back_instead_of_disabling(roots, monkeypatch):
    drive, local, env = roots
    explicit = drive.parent / "explicit"
    env = dict(env, ORION_SHOT_RECORDS_ROOT=str(explicit))
    _fail_open_under(str(explicit), PermissionError(errno.EACCES, "denied"), monkeypatch)
    rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _l_root(local)


def _outside_pytest(monkeypatch):
    # create() treats any process with pytest imported as a test run and records nothing;
    # hide that so the COMPILED-build rule is what is actually exercised.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)


def test_compiled_customer_build_without_the_switch_creates_nothing(roots, monkeypatch):
    drive, local, _ = roots
    drive.mkdir()
    env = {"LOCALAPPDATA": str(local)}                    # no ORION_SHOT_RECORDS at all
    monkeypatch.setattr(shot_records, "_compiled_customer_build", lambda: True)
    _outside_pytest(monkeypatch)
    assert _create(env) is None
    assert os.listdir(str(drive)) == []
    assert not local.exists()


def test_control_source_run_outside_pytest_does_record(roots, monkeypatch):
    """Proves the test above is not passing only because of the pytest guard."""
    drive, local, _ = roots
    drive.mkdir()
    env = {"LOCALAPPDATA": str(local), "ORION_SHOT_RECORDS_SESSION": "s1"}
    monkeypatch.setattr(shot_records, "_compiled_customer_build", lambda: False)
    _outside_pytest(monkeypatch)
    rec = _create(env)
    assert rec is not None and os.path.dirname(rec.path) == _d_root(drive)
