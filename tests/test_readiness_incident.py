"""Local fakes only: readiness state and diagnostic behavior, no client launch."""
import logging
import threading
from types import SimpleNamespace

import pytest
import remote_play_client as rpc


@pytest.mark.parametrize('code', [0, 1, 0xC0000005])
def test_exited_client_revokes_cached_status_and_records_exit(code, tmp_path):
    m = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        require_session_ready=True, session_log_dir=str(tmp_path)))
    m._process = SimpleNamespace(pid=4242, returncode=code, poll=lambda: code)
    m._status = rpc.RemotePlayClientStatus(ok=True, session_ready=True)
    assert not m.is_session_ready()
    assert m.status.session_ready is False
    d = m.readiness_diagnostic()
    assert d['reason'] == 'process_exited'
    assert d['pid'] == 4242 and d['exit_code'] == code
    assert set(d) == {'reason', 'pid', 'exit_code', 'session_log'}


def test_readiness_diagnostic_distinguishes_end_marker_and_no_proof(tmp_path):
    p = tmp_path / 'chiaki_session_fixture.log'
    p.write_bytes(b'StreamConnection successfully received streaminfo\n')
    m = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        require_session_ready=True, session_log_dir=str(tmp_path)))
    m._process = SimpleNamespace(pid=4242, returncode=None, poll=lambda: None)
    m._status = rpc.RemotePlayClientStatus(ok=True, session_ready=True)
    assert m.is_session_ready()
    p.write_bytes(p.read_bytes() + b'Session has quit\n')
    assert not m.is_session_ready()
    assert m.readiness_diagnostic()['reason'] == 'session_ended'
    assert m.readiness_diagnostic()['session_log'] == p.name


def test_session_log_poll_is_serialized_across_callers(tmp_path, monkeypatch):
    tracker = rpc._SessionReadinessTracker(str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    second_started = threading.Event()
    call_count = [0]
    count_lock = threading.Lock()

    def select():
        with count_lock:
            call_count[0] += 1
            count = call_count[0]
        if count == 1:
            entered.set()
            assert release.wait(3.0)
        else:
            second_entered.set()
        return ''

    monkeypatch.setattr(tracker, '_select_current_log', select)
    results = []
    errors = []

    def run(second=False):
        if second:
            second_started.set()
        try:
            results.append(tracker.poll())
        except BaseException as e:
            errors.append(e)

    first = threading.Thread(target=run)
    second = threading.Thread(target=lambda: run(True))
    try:
        first.start()
        assert entered.wait(2.0)
        second.start()
        assert second_started.wait(2.0)
        assert not second_entered.wait(0.2), 'shared offset/parser state admits two concurrent readers'
    finally:
        release.set()
        first.join(3.0)
        if second.ident is not None:
            second.join(3.0)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and results == [('waiting', ''), ('waiting', '')]


def test_orchestrator_logs_one_readiness_loss_transition(monkeypatch, caplog):
    import remote_play_orchestrator as rpo
    o = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    o._input_link_checked_at = 0.0
    o._input_link_ready = True
    o._client_manager = SimpleNamespace(
        is_session_ready=lambda: False,
        readiness_diagnostic=lambda: dict(reason='process_exited', pid=4242,
                                         exit_code=1, session_log='chiaki_session_fixture.log'))
    now = [10.0]
    monkeypatch.setattr(rpo.time, 'monotonic', lambda: now[0])
    with caplog.at_level(logging.WARNING, logger='RemotePlayOrchestrator'):
        assert not o.input_link_ready()
        now[0] += 0.5
        assert not o.input_link_ready()
    rows = [r.message for r in caplog.records if 'INPUT LINK LOST' in r.message]
    assert len(rows) == 1
    assert 'reason=process_exited' in rows[0] and 'exit_code=1' in rows[0]


def test_native_banner_route_preserves_coverage_for_learning():
    """The panel's coverage word must reach the engine, not be dropped at the controller.

    [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] The call gained a fourth argument,
    ``hasCoverage``, which says whether the panel HAD a coverage cell at all -- the 2-cell
    TIMING | DISTANCE layout has none, and it was 98 of the 281 graded releases across the
    09-18 sessions. Absent-cell is treated as an open shot; a cell that is present but
    unreadable stays excluded. Both arguments are pinned so neither can be quietly dropped:
    losing ``coverage`` would let contested shots calibrate the lead, and losing
    ``hasCoverage`` would starve the trim of every drill panel again.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / 'native_orion/src/OrionAppController.cpp').read_text(encoding='utf-8')
    call = 'automation_.observeBannerVerdict(static_cast<quint64>(releaseSeq), timing, coverage,'
    assert call in text
    assert 'hasCoverage);' in text[text.index(call):text.index(call) + 260]


def test_native_stop_intent_is_logged_before_recovery_state_is_cleared():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / 'native_orion/src/RemotePlaySession.cpp').read_text(encoding='utf-8')
    stop = text[text.index('void RemotePlaySession::stop()'):]
    assert stop.index('Remote Play stop requested:') < stop.index('inputRecoveryPending_ = false;')
    teardown = text[text.index('void RemotePlaySession::stopSidecar()'):]
    assert teardown.index('Sidecar shutdown requested asynchronously:') < teardown.index('new AsyncProcessRetirer')
    assert teardown.index('new AsyncProcessRetirer') < teardown.index('retiringSidecar_->start(')
    retire = (root / 'native_orion/src/AsyncProcessRetirer.h').read_text(encoding='utf-8')
    shutdown = retire[retire.index('void requestShutdown()'):]
    assert shutdown.index('process_->write(') < shutdown.index('process_->closeWriteChannel();')
