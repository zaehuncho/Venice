#!/usr/bin/env python
"""stream_quality.py -- stream-quality estimation for the quality-adaptive meter reader.

Two timescales, one code path (the compressed-reader design, Q3):

  * q_frame  (continuous, per frame): drives ONLY the measurement variance R fed to the
    temporal estimators. A Kalman/LSQ is natively stable under varying R -- no hysteresis.
  * q_session (slow, hysteretic): drives every DISCRETE tunable via ReaderParams.for_quality
    and the luma-tracking gate. Schmitt bands + minimum dwell so adaptive-bitrate flapping
    can never oscillate the reader's thresholds.

At q=1 the adaptation law evaluates to the shipped capture-card constants exactly
(ReaderParams.for_quality(1.0) == ReaderParams(), asserted by tests) -- the pristine path
is the identity limit of the compressed path, zero regression by construction.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np


def rung_prior(width: int, height: int, fps: float = 60.0,
               bitrate_kbps: Optional[float] = None) -> float:
    """Static quality prior from the configured rung: bits-per-pixel x meter-pixel scale.
    Without a known bitrate (capture card / unknown source) resolution alone decides --
    a 1080p source with no encode knowledge is treated as pristine."""
    scale = min(1.0, (width / 1920.0))
    if bitrate_kbps is None or bitrate_kbps <= 0:
        return max(0.3, scale)
    bpp = (bitrate_kbps * 1000.0) / max(1.0, width * height * fps)
    # log ramp: 0.03 bpp (starved) -> 0.25 bpp (clean) covers the RemotePlay ladder.
    # MULTIPLICATIVE with the meter-pixel scale: quality needs BOTH bits AND pixels --
    # an additive blend lets 360p30's high bpp outrank 720p60/4M, which is exactly
    # backwards (the meter is 7-11 px wide at 360p; resolution is the binding constraint).
    q_bpp = float(np.clip(np.log2(max(bpp, 1e-4) / 0.03) / np.log2(0.25 / 0.03), 0.0, 1.0))
    return float(np.clip(np.sqrt(q_bpp) * scale, 0.0, 1.0))


class QualityEstimator:
    """Fuses the static rung prior with per-frame pixel evidence (the K2 fit's own
    step-SNR/width statistic -- computed for free -- plus NCC health) into q_frame, and
    integrates q_frame into a Schmitt-hysteretic q_session."""

    # Schmitt bands: 4 quality bands with asymmetric up/down edges + minimum dwell.
    UP_EDGES = (0.35, 0.62, 0.88)
    DOWN_EDGES = (0.28, 0.55, 0.80)
    DWELL_S = 2.0
    # K2 fit q ((step/resid)/width) normalization: pristine sharp ~15-25, 4 Mbps ~2-5
    FIT_Q_NORM = 15.0

    def __init__(self, prior: float = 1.0):
        self._q_s = float(np.clip(prior, 0.0, 1.0))
        self._hist = deque(maxlen=90)          # ~1.5 s of q_frame at 60fps
        self._band = None                      # current Schmitt band index (0..3)
        self._band_ts: Optional[float] = None
        self.q_frame = self._q_s
        self.q_session = self._q_s

    def set_stream_profile(self, width: int, height: int, fps: float = 60.0,
                           bitrate_kbps: Optional[float] = None) -> None:
        self._q_s = rung_prior(width, height, fps, bitrate_kbps)
        # Bug #1 (compressed-path design): the profile is a BUILD-TIME prior. Before any
        # per-frame pixel evidence exists, re-seed the running state so q_session STARTS
        # in the rung's band -- otherwise the first Schmitt banding integrates the
        # constructor's pristine q_frame=1.0 and gates the luma branches OFF on a 4 Mbps
        # stream (the exact mis-init the :466 comment documents). With evidence already
        # in the history (mid-session reconfigure) only the prior blend weight moves.
        if not self._hist:
            self.q_frame = self._q_s
            self.q_session = self._q_s
            self._band = None
            self._band_ts = None

    @staticmethod
    def r_for(q: float) -> float:
        """Fill measurement variance (%^2) for a quality level: sigma 0.4pp at q=1
        (sub-pixel-tuned) growing to ~3.4pp at q=0."""
        sigma = 0.4 + 3.0 * (1.0 - float(np.clip(q, 0.0, 1.0))) ** 1.5
        return sigma * sigma

    def _band_of(self, q: float, current: Optional[int]) -> int:
        if current is None:
            edges = self.UP_EDGES
            for i, e in enumerate(edges):
                if q < e:
                    return i
            return len(edges)
        # move up only past an UP edge, down only past a DOWN edge (hysteresis gap)
        band = current
        while band < len(self.UP_EDGES) and q >= self.UP_EDGES[band]:
            band += 1
        while band > 0 and q < self.DOWN_EDGES[band - 1]:
            band -= 1
        return band

    def update(self, ts: float, *, fit_q: Optional[float] = None,
               chroma_alive: bool = False, ncc_peak: Optional[float] = None,
               stale: bool = False) -> tuple:
        """Feed one frame's evidence; returns (q_frame, q_session). Stale (encoder-skip)
        frames carry no pixel evidence -- they neither raise nor lower quality."""
        if not stale:
            if fit_q is not None:
                q_p = float(np.clip(fit_q / self.FIT_Q_NORM, 0.0, 1.0))
            elif chroma_alive:
                # the tuned chroma read is running: pixel quality is at least "good";
                # NCC peak (when tracking) refines it
                q_p = 0.9 if ncc_peak is None else float(np.clip(0.5 + 0.5 * ncc_peak, 0.0, 1.0))
            else:
                q_p = None
            if q_p is not None:
                self.q_frame = float(np.clip(0.35 * self._q_s + 0.65 * q_p, 0.0, 1.0))
                self._hist.append(self.q_frame)
        if len(self._hist) >= 12:
            q_med = float(np.median(np.asarray(self._hist)))
        else:
            q_med = self.q_frame
        new_band = self._band_of(q_med, self._band)
        if self._band is None:
            self._band, self._band_ts = new_band, ts
        elif new_band != self._band:
            if self._band_ts is None or ts - self._band_ts >= self.DWELL_S:
                self._band, self._band_ts = new_band, ts
        else:
            self._band_ts = self._band_ts if self._band_ts is not None else ts
        # band -> representative q_session (band centers; top band = pristine row)
        centers = (0.20, 0.48, 0.74, 1.0)
        self.q_session = centers[self._band]
        return self.q_frame, self.q_session

    @property
    def pristine(self) -> bool:
        """Top Schmitt band: the reader should run the exact capture-card row (luma
        branches gated OFF -- the formal collapse)."""
        return self._band is not None and self._band == len(self.UP_EDGES)
