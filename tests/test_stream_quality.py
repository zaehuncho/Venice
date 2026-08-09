"""Quality layer: the adaptation law's q=1 collapse identity, Schmitt hysteresis under
bitrate flapping, and the compressed reader's auto luma gate."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))          # sibling test-module helpers
from test_compressed_meter_reader import _chroma_frame  # noqa: E402

from simple_meter_reader import ReaderParams
from stream_quality import QualityEstimator, rung_prior


# --------------------------------------------------------------------------- #
#  adaptation law
# --------------------------------------------------------------------------- #
def test_for_quality_collapse_identity():
    """THE collapse contract: q=1 IS the shipped capture-card tuning, field for field."""
    assert ReaderParams.for_quality(1.0) == ReaderParams()


def test_for_quality_monotone():
    qs = np.linspace(0.0, 1.0, 11)
    rows = [ReaderParams.for_quality(float(q)) for q in qs]
    for a, b in zip(rows, rows[1:]):        # b = higher quality
        assert a.red_frac_min <= b.red_frac_min
        assert a.ncc_lock <= b.ncc_lock
        assert a.conf_rate <= b.conf_rate           # lower q decays slower (longer coast)
        assert a.g_area_min <= b.g_area_min
        assert a.green_s_floor <= b.green_s_floor
        assert a.vel_win >= b.vel_win               # lower q fuses over a longer window


def test_rung_prior_ordering():
    """The static prior must order the real RemotePlay ladder sensibly."""
    quality = rung_prior(1920, 1080, 60, 12000)
    performance = rung_prior(1280, 720, 60, 12000)
    balanced = rung_prior(1280, 720, 60, 4000)
    ultralow = rung_prior(640, 360, 30, 1200)
    card = rung_prior(1920, 1080, 60, None)
    assert card >= quality > balanced > ultralow
    assert performance > balanced


def test_r_for_grows_as_quality_drops():
    assert QualityEstimator.r_for(1.0) == pytest.approx(0.16, abs=1e-6)
    assert QualityEstimator.r_for(0.5) > QualityEstimator.r_for(0.9)
    assert QualityEstimator.r_for(0.0) > 10.0


# --------------------------------------------------------------------------- #
#  Schmitt hysteresis
# --------------------------------------------------------------------------- #
def _drive(qe, ts0, seconds, fit_q, fps=60):
    ts = ts0
    for k in range(int(seconds * fps)):
        ts = ts0 + k / fps
        qe.update(ts, fit_q=fit_q)
    return ts


def test_band_settles_high_on_sharp_evidence():
    qe = QualityEstimator(prior=1.0)
    _drive(qe, 0.0, 3.0, fit_q=20.0)        # pristine-sharp fits
    assert qe.pristine


def test_band_settles_low_on_blurred_evidence():
    qe = QualityEstimator(prior=0.55)       # 4 Mbps-ish prior
    _drive(qe, 0.0, 3.0, fit_q=3.0)         # smeared fits
    assert not qe.pristine
    assert qe.q_session < 0.8


def test_bitrate_flapping_cannot_oscillate_the_band():
    """Adaptive-bitrate wobble (alternating sharp/soft evidence at ~1s period) must not
    flip the Schmitt band back and forth."""
    qe = QualityEstimator(prior=0.55)
    _drive(qe, 0.0, 3.0, fit_q=6.0)         # settle mid
    band0 = qe.q_session
    flips = 0
    last = qe.q_session
    ts = 3.0
    for cycle in range(6):                   # 6 s of flapping
        ts = _drive(qe, ts, 0.5, fit_q=9.0)
        ts = _drive(qe, ts, 0.5, fit_q=4.0)
        if qe.q_session != last:
            flips += 1
            last = qe.q_session
    assert flips <= 1, (band0, flips)


def test_stale_frames_carry_no_quality_evidence():
    qe = QualityEstimator(prior=1.0)
    _drive(qe, 0.0, 3.0, fit_q=20.0)
    q_before = qe.q_frame
    for k in range(60):
        qe.update(3.0 + k / 60.0, stale=True)
    assert qe.q_frame == q_before


# --------------------------------------------------------------------------- #
#  reader auto gate (end to end on synthetic frames)
# --------------------------------------------------------------------------- #
def test_reader_auto_gate_collapses_on_pristine_chroma():
    """Default (quality-managed) compressed reader on a pristine chroma stream must end up
    in the pristine band with the luma branches gated OFF, running the exact base row."""
    from compressed_meter_reader import CompressedMeterReader
    r = CompressedMeterReader(1920, 1080)    # no luma_tracking arg -> auto
    ts = 0.0
    for cycle in range(4):                   # several rises: sharp chroma evidence
        for k, frac in enumerate(np.linspace(0.1, 0.95, 20)):
            ts += 1 / 60.0
            r.read(_chroma_frame(float(frac)), ts=ts)
    assert r._qe.pristine
    assert r._luma_tracking is False
    assert r.NCC_LOCK == ReaderParams().ncc_lock       # q=1 row applied


def test_reader_auto_gate_fails_open_when_chroma_stops():
    """Pristine band + chroma goes quiet (>1s): the gate must re-enable luma tracking
    rather than stay luma-blind on stale quality history."""
    from compressed_meter_reader import CompressedMeterReader
    r = CompressedMeterReader(1920, 1080)
    ts = 0.0
    for cycle in range(4):
        for k, frac in enumerate(np.linspace(0.1, 0.95, 20)):
            ts += 1 / 60.0
            r.read(_chroma_frame(float(frac)), ts=ts)
    assert r._luma_tracking is False
    flat = np.full((1080, 1920, 3), 60, np.uint8)
    for k in range(90):                      # 1.5 s of no chroma at all
        ts += 1 / 60.0
        r.read(flat, ts=ts)
    assert r._luma_tracking is True
