"""Deterministic cross-thread shot identity and stale-command regressions."""
import inspect
import textwrap
import threading
from types import SimpleNamespace
import pytest
import remote_play_orchestrator as rpo
from simple_meter_reader import SimpleMeterReader


def make_orch(monkeypatch):
    monkeypatch.setenv('ORION_METER_DETECTOR','0')
    o=rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    o._meter_detector=SimpleMeterReader(1280,720,require_gameplay_eligibility=True)
    o._frame_seq=10;o._shot_gate_arm_frames=2400;o._shot_gate_max_seconds=20.
    o._shot_gate_post_release_seconds=3.
    o._shot_gate_deadline_seq=o._shot_gate_hw_deadline_seq=-1
    o._shot_gate_deadline_monotonic=o._shot_gate_hw_deadline_monotonic=-1.
    o._shot_gate_source='';o._shot_gate_epoch=0;o._shot_gate_armed_prev=False
    o._shot_gate_edge_pending=False;o._shot_records=None;o._shot_range=None
    o._framedump_press_window=False;o._framedump_env_enabled=False
    return o


def frame_push(o):
    fn=getattr(o,'_push_reader_shot_gate',None)
    if fn:return fn(o._frame_seq)
    # Preserve a runnable baseline witness: execute the exact old inline handoff,
    # not an invented model of it. The production implementation uses the helper.
    src=inspect.getsource(type(o)._processing_loop)
    start=src.index('                        _sg = getattr(self._meter_detector, "set_shot_state", None)')
    end=src.index('                        _cv_t0 =',start)
    body=textwrap.dedent(src[start:end]);scope={'logger':rpo.logger}
    exec('def push(self, _snap_seq):\n'+textwrap.indent(body,'    '),scope)
    return scope['push'](o,o._frame_seq)


class ObservedLock:
    def __init__(self,attempt):self.inner=threading.RLock();self.attempt=attempt
    def __enter__(self):
        if threading.current_thread().name=='arm-worker':self.attempt.set()
        self.inner.acquire();return self
    def __exit__(self,*args):self.inner.release()


@pytest.mark.parametrize('epoch',[1,301,2**53+7])
def test_frame_snapshot_cannot_erase_a_new_physical_epoch(monkeypatch,epoch):
    o=make_orch(monkeypatch);r=o._meter_detector
    ready=threading.Event();resume=threading.Event();attempt=threading.Event();errors=[]
    o._shot_gate_lock=ObservedLock(attempt)
    state=o._shot_gate_state
    def paused(*a,**kw):
        answer=state(*a,**kw)
        if threading.current_thread().name=='frame-worker':
            ready.set();assert resume.wait(3)
        return answer
    monkeypatch.setattr(o,'_shot_gate_state',paused)
    set_state=r.set_shot_state
    def watched(*a,**kw):
        set_state(*a,**kw)
        if threading.current_thread().name=='arm-worker':attempt.set()
    monkeypatch.setattr(r,'set_shot_state',watched)
    def work(fn):
        try:fn()
        except BaseException as e:errors.append(e)
    frame=threading.Thread(target=work,args=(lambda:frame_push(o),),name='frame-worker')
    arm=threading.Thread(target=work,args=(lambda:o._arm_shot_gate('square_edge',epoch),),name='arm-worker')
    frame.start()
    try:
        assert ready.wait(3);arm.start();assert attempt.wait(3)
    finally:
        resume.set();frame.join(3)
        if arm.ident is not None:arm.join(3)
    assert not frame.is_alive() and not arm.is_alive() and not errors,errors
    assert r._physical_shot_epoch==epoch
    assert r._shot_armed_hw


@pytest.mark.parametrize('stale',[1,40])
def test_old_arm_cannot_retype_or_refresh_the_current_shot(monkeypatch,stale):
    o=make_orch(monkeypatch)
    assert o.arm_shot_gate('square_edge',41,'Standstill',False)
    before=(o._shot_gate_deadline_seq,o._shot_gate_deadline_monotonic,
            o._shot_gate_hw_deadline_monotonic,o._shot_gate_source)
    r=o._meter_detector
    calls=[];o._shot_records=SimpleNamespace(note_press=lambda *a,**k:calls.append((a,k)))
    accepted=o.arm_shot_gate('type_upgrade',stale,'Left Fade',True)
    assert not accepted
    assert r._physical_shot_epoch==41 and r._pa_shot_type=='Standstill' and not r._pa_rhythm
    assert before==(o._shot_gate_deadline_seq,o._shot_gate_deadline_monotonic,
                    o._shot_gate_hw_deadline_monotonic,o._shot_gate_source)
    assert not calls


def test_same_epoch_type_upgrade_keeps_structure_and_updates_type(monkeypatch):
    o=make_orch(monkeypatch);o.arm_shot_gate('square_edge',41,'Standstill')
    r=o._meter_detector;r._latch_gameplay_structure_proof(41)
    assert o.arm_shot_gate('type_upgrade',41,'Right Fade',True)
    assert r._pa_shot_type=='Right Fade' and r._pa_rhythm
    assert r._gameplay_structure_proof_epoch==41


def test_real_expiry_still_disarms_and_clears_identity(monkeypatch):
    o=make_orch(monkeypatch);o._arm_shot_gate('square_edge',41)
    o._shot_gate_deadline_monotonic=o._shot_gate_hw_deadline_monotonic=-1.
    frame_push(o)
    assert not o._meter_detector._shot_armed_hw
    assert o._meter_detector._physical_shot_epoch==0
