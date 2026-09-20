"""Keep a bounded press-window census attached to its already graded shot."""
import pytest

from shot_records import ShotRecorder


def recorder(tmp_path, *, dump='1', window='1'):
    now = [1000.0]
    rec = ShotRecorder.create(
        session='completion-fixture', start_thread=False, clock=lambda: now[0],
        env={'ORION_SHOT_RECORDS': '1', 'ORION_SHOT_RECORDS_ROOT': str(tmp_path),
             'ORION_FRAMEDUMP': dump, 'ORION_FRAMEDUMP_PRESS_WINDOW': window})
    assert rec is not None
    rec.note_press(15, ts_ms=now[0], mono_ms=1000., shot_type='Right Fade')
    now[0] = 1900.
    rec.note_release(15, now[0])
    return rec, now


def grade(rec):
    assert rec.note_oracle(15, {'gap_px': 1., 'verdict_proxy': 'green'})
    assert rec.note_banner({'release_seq': 15, 'attributed': 1,
                            'timing': 'EXCELLENT', 'green': True})


def test_complete_grades_wait_for_same_epoch_dump_census(tmp_path):
    rec, now = recorder(tmp_path)
    grade(rec)
    assert rec._q.empty(), 'graded record finalized before its frame census arrived'
    now[0] = 3600.
    assert not rec.note_frames(14, frames=999)
    assert rec._q.empty()
    assert rec.note_frames(15, dir='D:/fixture', first_idx=10, last_idx=179,
                           frames=170, dropped=0, reason='window_end')
    finished = rec._q.get_nowait()
    assert finished['framedump']['frames'] == 170
    assert finished['closed_reason'] == 'complete'
    assert finished['closed_ts_ms'] == 3600.
    assert not rec.note_frames(15, frames=999)


def test_dump_census_before_grades_still_completes_on_banner(tmp_path):
    rec, _ = recorder(tmp_path)
    assert rec.note_frames(15, frames=170, dropped=3)
    grade(rec)
    finished = rec._q.get_nowait()
    assert finished['framedump']['dropped'] == 3
    assert finished['closed_reason'] == 'complete'


@pytest.mark.parametrize('dump,window', [('0', '1'), ('1', '0'), ('0', '0'), ('off', 'true')])
def test_dump_disabled_keeps_immediate_completion(tmp_path, dump, window):
    rec, _ = recorder(tmp_path, dump=dump, window=window)
    grade(rec)
    finished = rec._q.get_nowait()
    assert finished['framedump'] is None
    assert finished['closed_reason'] == 'complete'


def test_writer_failure_cannot_keep_graded_record_open_past_grace(tmp_path):
    rec, now = recorder(tmp_path)
    grade(rec)
    assert rec._q.empty()
    now[0] = 1900. + rec.grace_ms + 1.
    rec.tick()
    finished = rec._q.get_nowait()
    assert finished['closed_reason'] == 'grace'
    assert finished['framedump'] is None


def test_session_end_does_not_wait_for_dump_worker(tmp_path):
    rec, _ = recorder(tmp_path)
    grade(rec)
    assert rec._q.empty()
    rec.close_all('session_end')
    assert rec._q.get_nowait()['closed_reason'] == 'session_end'


def test_direct_recorder_default_does_not_expect_dump(tmp_path):
    rec = ShotRecorder(tmp_path / 'direct.jsonl', start_thread=False, clock=lambda: 2000.)
    rec.note_press(15, ts_ms=1000., mono_ms=1000.)
    rec.note_release(15, 1900.)
    grade(rec)
    assert rec._q.get_nowait()['closed_reason'] == 'complete'
