"""Missing/rejected pixels must never become zero-valued timing observations."""
import numpy as np
import pytest
from test_simple_reader_read_rescue import _locked_reader, TRUE_BOX

@pytest.mark.parametrize('count',[1,2,3])
def test_missing_white_edge_is_absence_not_a_zero_rise_sample(monkeypatch,count):
    monkeypatch.setenv('ORION_READER_IDLE_PUBLISH_GATE','0')
    r,loc,frame=_locked_reader(monkeypatch)
    before=list(r._det_fill_hist); coarse=list(r._det_coarse_fill_hist)
    blank=np.full_like(frame,35)
    for k in range(count):
        ts=1.04+k*.02;loc.res=(True,TRUE_BOX,.9,ts)
        out=r.detect(blank,ts=ts)
        assert not out.detected
        assert out.rejection_reason=='detector_white_ribbon_missing'
        assert out.fill_velocity_pct_s==0.0 and out.eta_ms<0
        assert not out.gameplay_structure_verified
    assert list(r._det_fill_hist)==before
    assert list(r._det_coarse_fill_hist)==coarse
    # The lock survives absence; only measurements are withheld.
    assert r._det_state=='locked'
    ts=1.04+count*.02;loc.res=(True,TRUE_BOX,.9,ts)
    out=r.detect(frame,ts=ts)
    assert out.detected and out.fill_pct>30
    assert all(fill>0 for _,fill in r._det_fill_hist)

@pytest.mark.parametrize('reason',['geometry','occlusion'])
def test_rejected_measurement_cannot_latch_structure_or_pollute_history(monkeypatch,reason):
    monkeypatch.setenv('ORION_READER_IDLE_PUBLISH_GATE','0')
    r,loc,frame=_locked_reader(monkeypatch)
    r._require_gameplay_eligibility=True
    r.set_shot_state(True,1.,True);r.notify_physical_shot_start(501)
    r._latch_gameplay_structure_proof=lambda epoch: stamps.append(epoch)
    stamps=[]
    # Isolate the detector-fill lane, which must honor its geometry/occlusion veto.
    monkeypatch.setattr(r,'read',lambda *a,**k:{'detected':False,'fill':0.,'stage':'no_meter','bbox':[0,0,0,0],'confidence':0.})
    def rejected(*args,**kwargs):
        r._last_fill_coarse=15.
        r._det_pixel_ruler_reject=reason=='geometry'
        r._det_occ_censored_reject=reason=='occlusion'
        return 15.,(93.,97.,95.,4.,.95,20),85
    monkeypatch.setattr(r,'_measure_fill_in_box',rejected)
    before=list(r._det_fill_hist)
    loc.res=(True,TRUE_BOX,.9,1.04)
    out=r.detect(frame,ts=1.04)
    assert not out.detected
    assert stamps==[]
    assert not r._det_last_presence
    assert list(r._det_fill_hist)==before
