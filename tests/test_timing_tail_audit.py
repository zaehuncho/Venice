"""Session joins and deadline labels must not invent timing variance."""
from datetime import datetime, timezone
import importlib.util
import inspect
import os
from pathlib import Path

import pytest


ROOT = Path(os.environ.get('ORION_AUDIT_TEST_ROOT', Path(__file__).parents[1]))

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tools/timing'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

g, pipeline = load('onset_ff_grade'), load('shot_pipeline_audit')
T = 1790020000000.

def line(ms, text):
    stamp = datetime.fromtimestamp(ms/1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return stamp+'  '+text

def record(session='one', epoch=1, start=T, timing='EXCELLENT'):
    return dict(session=session, epoch=epoch, press_ts_ms=start, closed_ts_ms=start+2000,
                shot_type='Standstill', onset_ms=500., outcome='released',
                banner=dict(timing=timing, coverage='WIDE OPEN'), network={})

def events(r, offset=None):
    t,ep = r['press_ts_ms'],r['epoch']
    out = [line(t+1000, f"BANNER VERDICT: timing={r['banner']['timing']} coverage=WIDE OPEN has_cov=1 release_seq={ep} attributed=1"),
           line(t+1100, f'SHOT RECORD: epoch={ep} type=Standstill onset_ms=500 court_ready=0')]
    if offset is not None:
        out += [line(t+800, f'ONSET FF: reason=applied dev_ms=50 applied_ms={offset} physical_epoch={ep}'),
                line(t+900, f'Release onsetff: seq=1 applied_ms={offset} scheduled=1 shot_attempt=1 physical_epoch={ep}')]
    return out

def collect(records, lines):
    # Run the SAME available log/JSON evidence against the legacy and repaired
    # API. Legacy's lack of record input is the reproduced session-join defect.
    if 'records' in inspect.signature(g.collect).parameters:
        return g.collect(lines, None, records)[0]
    return g.collect(lines, None)[0]

def test_reused_epoch_cannot_merge_two_sessions():
    a,b = record(timing='EARLY'),record('two',start=T+10000,timing='LATE')
    shots=list(collect([a,b],events(a,-10)+events(b,0)).values())
    assert len(shots)==2
    assert sorted((s['timing'],s['ff']) for s in shots)==[('EARLY',-10),('LATE',0)]

def test_missing_new_offset_does_not_inherit_old_session():
    a,b=record(),record('two',start=T+10000)
    shots=list(collect([b],events(a,-10)+events(b)).values())
    assert len(shots)==1 and shots[0]['ff'] is None

def test_ambiguous_overlapping_session_interval_leaves_offset_unknown():
    a,b=record(),record('two')
    shots=list(collect([a,b],events(a,-10)).values())
    assert len(shots)==2 and all(s['ff'] is None for s in shots)

def test_conflicting_release_displacements_are_not_last_writer_wins():
    r=record()
    lines=events(r,-10)+[line(T+901,'Release onsetff: seq=1 applied_ms=0 scheduled=1 shot_attempt=1 physical_epoch=1')]
    assert next(iter(collect([r],lines).values()))['ff'] is None

def test_identical_duplicate_lines_are_idempotent():
    r=record(); lines=events(r,-10)
    shots=collect([r,r],lines+lines)
    assert len(shots)==1 and next(iter(shots.values()))['ff']==-10

@pytest.mark.parametrize('value', ['nan','inf','-inf'])
def test_nonfinite_measurements_stay_unknown(value):
    assert g._f(value) is None

def test_cold_latest_arm_does_not_retain_earlier_deviation():
    r=record()
    lines=events(r,-10)+[line(T+850,'ONSET FF: reason=cold applied_ms=0 physical_epoch=1')]
    s=next(iter(collect([r],lines).values()))
    assert s['ff_reason']=='cold' and s['dev'] is None

def test_session_partition_does_not_use_only_idle_gap():
    rows=[dict(session='a',day='2026-09-21',t=10),dict(session='b',day='2026-09-21',t=11)]
    assert len(g.sessions(rows))==2

def test_unknown_displacement_is_not_reported_as_control(capsys):
    records=[record(epoch=i+1,start=T+3000*i) for i in range(8)]
    lines=sum((events(r) for r in records), [])
    shots=collect(records,lines)
    g.grade(shots,'Standstill')
    text=capsys.readouterr().out
    assert 'displacement_unknown 8' in text
    assert all(s['dev'] is None for s in shots.values()), 'do not fabricate a hindsight reference'

def test_offset_event_outside_record_is_not_joined():
    r=record()
    lines=events(r)+[line(T+3000,'Release onsetff: seq=1 applied_ms=-10 scheduled=1 shot_attempt=1 physical_epoch=1')]
    assert next(iter(collect([r],lines).values()))['ff'] is None

def test_conflicting_duplicate_record_is_rejected():
    a,b=record(),record(timing='EARLY')
    with pytest.raises(ValueError):collect([a,b],events(a))

def test_large_integer_epoch_remains_exact():
    r=record(epoch=2**53+1)
    s=next(iter(collect([r],events(r,-10)).values()))
    assert s['epoch']==2**53+1 and s['ff']==-10

def test_base_deadline_difference_is_not_labelled_scheduler_error():
    logs=[line(T+1000,'Release delivery identity: physical_epoch=1 release_seq=1'),
          line(T+1000,'Scheduled fire: seq=1 scheduled=1 deltaMs=0.00 aligned_ms=100'),
          line(T+1000,'Precise dispatch timing: seq=1 command_issued_ms=90 active_route_wait_ms=0.4'),
          line(T+1000,'Release onsetff: seq=1 physical_epoch=1 applied_ms=-10 scheduled=1')]
    m=pipeline.audit([record()],logs)['shots'][0]['measurements']
    assert 'command_minus_deadline_ms' not in m
    assert m['command_minus_base_deadline_ms']==-10
    assert m['scheduler_error_ms_rounded']==0
    assert m['reported_onset_ff_ms']==-10

def test_missing_offset_is_unknown_not_zero():
    logs=[line(T+1000,'Release delivery identity: physical_epoch=1 release_seq=1')]
    m=pipeline.audit([record()],logs)['shots'][0]['measurements']
    assert 'reported_onset_ff_ms' not in m

def test_wrong_epoch_offset_does_not_join_via_matching_release_seq():
    logs=[line(T+1000,'Release delivery identity: physical_epoch=1 release_seq=1'),
          line(T+1000,'Release onsetff: seq=1 physical_epoch=2 applied_ms=-10 scheduled=1')]
    assert 'reported_onset_ff_ms' not in pipeline.audit([record()],logs)['shots'][0]['measurements']

@pytest.mark.parametrize('age,expected', [('27ms',27.),('27.5ms',27.5),('-1ms',None),('nanms',None)])
def test_release_age_keeps_units_and_unknowns(age,expected):
    logs=[line(T+1000,'Release delivery identity: physical_epoch=1 release_seq=1'),
          line(T+1000,f'Release issued: seq=1 age={age}')]
    m=pipeline.audit([record()],logs)['shots'][0]['measurements']
    assert m.get('reported_frame_age_at_release_ms')==expected
