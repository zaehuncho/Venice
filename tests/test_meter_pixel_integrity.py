"""Pixel-level white meter regression cases; no model, device, or live input."""
import os
import importlib.util
import sys
from pathlib import Path
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORION_") and k != "ORION_READER_TEST_FILE":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")

BH, BW = 107, 24

def patch(edge=86):
    p=np.full((BH,BW,3),40,np.uint8)
    p[:100,5:19]=(30,70,140)
    p[edge:100,5:19]=(250,252,253)
    p[100:103,5:19]=(168,170,171)
    for row,width in {3:2,4:4,5:6,6:10,7:12,8:14}.items():
        c=(BW-width)//2
        p[row,c:c+width]=(60,200,60)
    return p

def measure(p,r=None,k=0):
    r=r or SimpleMeterReader(1280,720)
    f=np.full((720,1280,3),35,np.uint8)
    f[300:407,600:624]=p
    return r._measure_fill_in_box(f,(600,300,BW,BH),ts=100+k/60)

@pytest.mark.parametrize("edge",[82,86,92])
@pytest.mark.parametrize("subpixel",[False,True])
def test_detached_white_band_never_replaces_observed_base_fill(monkeypatch,edge,subpixel):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL",str(int(subpixel)))
    clean=patch(edge); dirty=clean.copy(); dirty[40:68,:]=(248,248,248)
    a=SimpleMeterReader(1280,720); b=SimpleMeterReader(1280,720)
    for k in range(5):
        good=measure(clean,a,k); actual=measure(dirty,b,k)
        assert actual[2]==good[2]==edge
        assert actual[0]==pytest.approx(good[0],abs=.5)
        assert actual[1][:4]==pytest.approx(good[1][:4],abs=.5)

@pytest.mark.parametrize("kind",["below","above_side","court_only","thin_cap","split_cap"])
@pytest.mark.parametrize("subpixel",[False,True])
def test_green_window_is_one_aligned_cap_not_union_of_decor(monkeypatch,kind,subpixel):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL",str(int(subpixel)))
    clean=patch(62); dirty=clean.copy()
    if kind=="below": dirty[35:43,8:16]=(60,200,60)
    if kind=="above_side": dirty[0:2,0:5]=(60,200,60)
    if kind=="court_only":
        dirty[:15]=(30,70,140)
        dirty[35:43,8:16]=(60,200,60)
    if kind=="thin_cap":
        dirty[:15]=(30,70,140)
        dirty[3:7,10:14]=(60,200,60)
    if kind=="split_cap":
        dirty[:15]=(30,70,140)
        dirty[3:8,6:10]=(60,200,60)
        dirty[3:8,14:18]=(60,200,60)
    expected=measure(clean); actual=measure(dirty)
    if kind=="court_only": assert actual[1] is None
    elif kind in {"thin_cap","split_cap"}:
        assert actual[1] is not None
        assert actual[1][0]>90 and actual[1][3]<5
    else:
        assert actual[1] is not None
        assert actual[1][:4]==pytest.approx(expected[1][:4],abs=.1)
        assert actual[1][4:]==expected[1][4:]

@pytest.mark.parametrize("direction",[-1,1])
def test_translated_rising_white_meter_keeps_true_edge(direction):
    r=SimpleMeterReader(1280,720)
    for k,edge in enumerate([92,89,86,83,80,77]):
        p=patch(edge); p[40:68,:]=(248,248,248)
        f=np.full((720,1280,3),35,np.uint8)
        x=600+direction*k*4; y=300-k
        f[y:y+BH,x:x+BW]=p
        fill,g,top=r._measure_fill_in_box(f,(x,y,BW,BH),ts=100+k/60)
        assert top==edge
        assert 0<fill<40
        assert g is not None and g[3]<10


class FixedLocator:
    provider="fixture"
    infer_ms=0.0
    def submit(self,frame,ts): self.result=(True,(600,300,24,107),.99,ts)
    def latest(self): return self.result
    def detect_now(self,frame,ts):
        self.submit(frame,ts)
        return self.result
    def submit_priority(self,frame,ts): self.submit(frame,ts)

@pytest.mark.parametrize("values,risen",[
    ([15.,11.,13.,15.],True),
    ([15.,14.,13.,12.],False),
    ([0.,15.,15.,15.],False),
    ([15.,0.,15.,15.],False),
])
def test_static_quarantine_requires_real_ordered_rise(monkeypatch,values,risen):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL","0")
    r=SimpleMeterReader(1280,720)
    r._meter_detector=FixedLocator()
    r.set_shot_state(True,1.,True)
    r.notify_physical_shot_start(901)
    frame=np.full((720,1280,3),35,np.uint8)
    frame[300:407,600:624]=patch()
    for k,value in enumerate(values):
        def measured(*args,**kwargs):
            r._last_fill_coarse=value
            return value,None,(90 if value>0 else -1)
        monkeypatch.setattr(r,"_measure_fill_in_box",measured)
        r.detect(frame,ts=100.+k/60.)
    assert r._det_lock_read_n>=3
    r._note_static_zone_drop(101.,"nometer_strikes",(600,300,24,107))
    assert bool(r._static_zones) is not risen


@pytest.mark.parametrize("subpixel",[False,True])
def test_floating_stripe_plus_ragged_text_is_not_low_meter_fill(monkeypatch,subpixel):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL",str(int(subpixel)))
    p=patch(100)
    p[40:68,:]=(248,248,248)
    # Nameplate-like disconnected strokes: enough white pixels per row, but no
    # coherent broad ribbon rising from a base. A lower run is not automatically fill.
    for k in range(10):
        p[88+k,10:12]=250
        p[88+k,1:9 if k%2 else 1]=250
        if k%2==0:p[88+k,15:23]=250
    fill,green,top=measure(p)
    assert top==-1 and fill==0 and green is None
