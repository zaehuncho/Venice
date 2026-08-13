"""HDMI capture-card frame backend.

A third detector frame source (besides the decoder pipe + GDI/WGC window capture):
read a USB/PCIe video-capture DEVICE directly with ``cv2.VideoCapture``. For users
who run PS5 -> HDMI capture card -> PC this is the cleanest source — true 1080p60,
no Remote Play, and immune to the whole window-capture/occlusion/off-screen-embed
problem class. This is the standard approach for capture-card mode: a background
thread over ``cv2.VideoCapture`` with DSHOW first then MSMF (more predictable
frame ordering).

Drop-in for the orchestrator's decoder path: same ``start`` / ``stop`` /
``get_frame_nonblocking`` / ``get_frame`` contract and the shared ``FrameData`` /
latest-wins ``FrameRingBuffer`` as ``OrionFramePipeBackend`` / ``WGCCaptureBackend``.
``start()`` returns ``False`` (never raises) when no device opens, so the capture
loop transparently falls back.

NOTE on HDCP: the PS5 HDMI output is HDCP-protected; the capture card must strip
HDCP (most do, or a passthrough splitter is used) or frames read black. That's a
hardware setup concern, not handled here — a black feed is rejected downstream by
the orchestrator's existing black-frame guard.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import List, Optional

import numpy as np

from chiaki_backend import FrameData, FrameRingBuffer

logger = logging.getLogger(__name__)

# --- device-name gating (webcam protection) --------------------------------------
# The native launcher passes the DirectShow FriendlyName list (index-ordered; ICreateDevEnum
# order == cv2 CAP_DSHOW index, same mapping the Stream-Setup picker uses) via
# ORION_VIDEO_DEVICE_NAMES ("name0|name1|..."). With names we NEVER open a webcam-looking
# device — the old auto-detect blindly cv2.VideoCapture'd indices 0-7 (webcam included, LED
# on + privacy scare) and rode the ~2s card-retry loop, so the user's camera light blinked
# "pretty frequently". Without names (pre-rebuild), the brute scan is allowed ONCE per
# process, and the resolved index is persisted so later launches/retries go straight to the
# card without scanning at all.
_CARD_NAME_HINTS = ("elgato", "cam link", "camlink", "hd60", "avermedia", "live gamer",
                    "game capture", "capture", "magewell", "ezcap")
_WEBCAM_NAME_HINTS = ("webcam", "web cam", "facecam", "integrated camera", "hd camera",
                      "facetime", "streamcam", "brio", "c920", "c922", "c930", "kiyo",
                      "virtual camera", "obs virtual", "droidcam", "ivcam", "snap camera")
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_INDEX_CACHE = os.path.join(_MODULE_DIR, "logs", "capture_card_resolved.json")
_BRUTE_SCANNED = False   # once per process — never again on the ~2s retry cadence
_LAST_BUSY_DIAG_NS = 0   # rate-limit the "device busy" explainer to once per 30s

# --- abandoned (wedged) capture handles ------------------------------------------
# stop() detaches a handle whose reader is still blocked inside cap.read() and must keep it
# REFERENCED, because cv2.VideoCapture.__del__ calls release() — running that finalizer while
# the reader is mid-read is the MSMF/DSHOW access-violation this whole backend exists to avoid.
# The reference has to outlive the BACKEND OBJECT: the orchestrator drops the backend
# (`self._frame_backend = None`) the moment is_healthy() reports dead, so an instance attribute
# is garbage a moment later and the finalizer fires anyway. Module scope survives that.
# The owning reader thread releases the handle itself when the read finally returns and then
# drops it from this list, so it is not a leak — a wedged-forever handle is reclaimed at
# process exit, which is exactly the intended fallback.
_ABANDONED_CAPS: List = []
_ABANDONED_LOCK = threading.Lock()

# Serialises MSMF/DSHOW device-GRAPH transitions (build = cv2.VideoCapture(), teardown =
# cap.release()) across every thread in this process.
#
# Bughunt #7 established that release must happen on the READER thread, because releasing under a
# blocked read() access-violates. That is still true, but it left the mirror-image race open and it
# killed a live session on 2026-08-12 (sidecar pid 25792, `Windows fatal exception: code 0xc0000374`
# = STATUS_HEAP_CORRUPTION). The faulthandler dump names both halves at once:
#
#     Current thread ... capture_card_backend.py, line 1127 in _run     <- abandoned reader RELEASING
#     Thread ...        capture_card_backend.py, line  693 in _open     <- recovery thread OPENING
#
# i.e. the in-process re-open ("Capture-card reader died; detaching for in-process re-open") builds a
# new graph on the SAME physical device while the abandoned reader's read() finally returns 2.7s
# later and tears the old one down. Both mutate the driver's shared allocator; the heap does not
# survive it. The log shows exactly that spacing: abandon 18:17:09.890 -> crash 18:17:12.582.
#
# Only the two graph transitions are serialised. Frame reads are deliberately NOT held here: a wedged
# read() would then block every future open forever, which is the failure this backend exists to
# recover from.
_DEVICE_GRAPH_LOCK = threading.Lock()


def _park_abandoned_cap(cap) -> None:
    """Keep a wedged handle alive at module scope (see _ABANDONED_CAPS)."""
    if cap is None:
        return
    with _ABANDONED_LOCK:
        if not any(c is cap for c in _ABANDONED_CAPS):
            _ABANDONED_CAPS.append(cap)
        n = len(_ABANDONED_CAPS)
    logger.debug("Capture-card: parked abandoned handle at module scope (%d parked)", n)


def _discard_abandoned_cap(cap) -> None:
    """Drop a parked handle once its OWNING reader thread has released it — the finalizer is
    then a no-op, so it is safe to let the object be collected."""
    if cap is None:
        return
    with _ABANDONED_LOCK:
        for i, c in enumerate(_ABANDONED_CAPS):
            if c is cap:
                del _ABANDONED_CAPS[i]
                logger.debug("Capture-card: released+unparked an abandoned handle "
                             "(%d still parked)", len(_ABANDONED_CAPS))
                return

# --- timing / PTS-alignment ------------------------------------------------------
# Capture-card frames carry pts=0, so a frame's timestamp is the wall clock sampled at
# the instant cv2.read() RETURNS. Two problems feed timing jitter:
#   (a) it UNDER-counts true device latency (sensor->USB->decode, tens of ms), and
#   (b) the read-return instant JITTERS (scheduling/USB/decode variance) relative to the
#       frame's true capture time, so frame_age_ms — which the timing/oracle consume —
#       carries that variance straight into release-timing jitter.
# The source runs a FIXED cadence (60fps => 16.667ms grid), so the true frame times lie on
# a regular grid; the read-stamp noise is the removable part. CadenceLock models that grid
# with a phase/frequency PLL and snaps each stamp to it (drift-corrected, outlier-rejecting).

# Documented device-latency estimate for the Elgato HD60 X USB pipeline (glass->USB->decode).
# NOT applied by default (see ORION_CAPTURE_DEVICE_LATENCY_MS below): shifting the mean needs a
# LIVE session to confirm it doesn't detune the already-tuned release compensation. Set the env
# to this (or the live-measured value) to enable the mean correction.
HD60X_DEVICE_LATENCY_ESTIMATE_MS = 35.0


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else float(default)
    except Exception:
        return float(default)


def _frame_contract_reason(
    frame,
    min_width: int = 1280,
    min_height: int = 720,
    aspect_ratio: float = 16.0 / 9.0,
    aspect_tolerance: float = 0.035,
) -> str:
    """Return an integrity rejection reason, or ``""`` for a detector-grade frame.

    Capture APIs treat width/height/fourcc requests as advisory.  Accepting whatever
    happened to negotiate is how a 640x480/4:3 device (or a transient malformed buffer)
    reached a detector tuned on a full-resolution 16:9 game image.  The detector must
    never compensate by stretching that image: an invalid capture fails closed here.
    """
    if frame is None or getattr(frame, "size", 0) <= 0:
        return "empty"
    if not isinstance(frame, np.ndarray):
        return "not_ndarray"
    if frame.dtype != np.uint8:
        return "dtype"
    if frame.ndim != 3 or frame.shape[2] != 3:
        return "channels"
    height, width = int(frame.shape[0]), int(frame.shape[1])
    if width < int(min_width) or height < int(min_height):
        return "undersized"
    actual_aspect = width / float(max(1, height))
    if abs(actual_aspect - float(aspect_ratio)) / max(0.001, float(aspect_ratio)) \
            > float(aspect_tolerance):
        return "aspect"
    return ""


def _isolate_immutable_frame(frame, verify_copy: bool = True):
    """Copy a driver-owned buffer and return an immutable, contiguous ndarray.

    ``cv2.VideoCapture.read`` normally allocates a fresh ndarray, but that is a backend
    implementation detail rather than a lifetime guarantee.  Publishing the driver buffer
    directly lets a backend that recycles it overwrite pixels while detection/preview still
    read the previous frame (a genuine torn-frame race).  The explicit owned copy is the
    capture/detector ownership boundary.  When ``verify_copy`` is enabled, compare the source
    once after the copy; a buffer that changed during the copy is rejected as torn.

    Returns ``(owned_frame, reason)``.  ``owned_frame`` is read-only on success.
    """
    try:
        owned = np.array(frame, dtype=np.uint8, order="C", copy=True)
        if verify_copy and not np.array_equal(frame, owned):
            return None, "torn_copy"
        owned.setflags(write=False)
        return owned, ""
    except Exception:
        return None, "isolation"


class CadenceLock:
    """Phase/frequency-locked loop that snaps a jittery per-frame timestamp to the source's
    fixed-fps grid.

    The capture source emits at a regular cadence (dt_nom = 1e9/fps ns), so the TRUE frame
    times are t0, t0+dt, t0+2dt, ... The measured read-return times sit on that grid plus
    zero-mean noise (scheduling/USB/decode). We track an expected next-frame time and correct
    it SLOWLY toward the measurement (one-pole phase term) with a much slower frequency term
    that absorbs a real 59.94-vs-60.00 period offset — instead of taking the raw jittery stamp.
    Net effect: the emitted timestamp advances smoothly at the true cadence, so frame_age_ms is
    consistent frame-to-frame => less release jitter, WITHOUT changing the long-run mean.

    Guards:
      * Outlier rejection — a single late read (|phase error| > outlier fraction of dt) does NOT
        yank the grid; the stamp advances one nominal period and the jerk is dropped.
      * Drop quantization (bughunt #5) — an off-grid read that lands ON an integer grid multiple
        k>=2 periods out is a multi-period frame DROP (device cadence never moved); the grid
        advances k periods so subsequent frames are not stamped (k-1) periods too old (which
        inflated frame_age -> over-lead -> early fire).
      * Re-lock — a sustained run of outliers, or a large inter-read GAP (genuine fps drop / stall
        / pause), abandons the old grid and re-locks to the fresh read so we don't coast on a dead
        cadence. This makes a cadence CHANGE re-lock instead of drifting.
    """

    def __init__(self, fps: float, alpha: float = 0.10, beta: float = 0.002,
                 outlier_frac: float = 0.5, relock_after: int = 6,
                 relock_gap_frac: float = 4.0, freq_clamp_frac: float = 0.05,
                 drop_snap_frac: float = 0.25) -> None:
        fps = float(fps) if fps and fps > 0 else 60.0
        self.dt_nom = 1e9 / fps                 # nominal frame period (ns)
        self.dt_est = self.dt_nom               # tracked period (ns) — absorbs 59.94 vs 60.00
        self.alpha = float(alpha)               # phase-correction gain (per frame)
        self.beta = float(beta)                 # frequency-correction gain (slow)
        self.outlier_ns = float(outlier_frac) * self.dt_nom
        # Bughunt #5: how close (fraction of dt) an off-grid read must land to an INTEGER grid
        # multiple to be accepted as a multi-period frame DROP instead of an outlier. Tighter
        # than outlier_frac on purpose — 0.5*dt tiles the whole axis (every read would "match"
        # some multiple), 0.25*dt only matches reads genuinely ON the grid.
        self.drop_snap_ns = float(drop_snap_frac) * self.dt_nom
        self.relock_after = int(relock_after)
        self.relock_gap_ns = float(relock_gap_frac) * self.dt_nom
        self._freq_lo = self.dt_nom * (1.0 - float(freq_clamp_frac))
        self._freq_hi = self.dt_nom * (1.0 + float(freq_clamp_frac))
        self.t_locked = None                    # current locked stamp (ns, float)
        self._last_meas = None                  # last raw measurement (ns) — gap detection
        self._outlier_run = 0
        self._skip_run = 0                      # consecutive multi-period (k>1) gaps
        self.relocks = 0                        # diagnostics: how many hard re-locks happened

    def reset(self, t_meas_ns: float) -> None:
        self.t_locked = float(t_meas_ns)
        self._last_meas = float(t_meas_ns)
        self.dt_est = self.dt_nom
        self._outlier_run = 0
        self._skip_run = 0

    def update(self, t_meas_ns: float) -> int:
        """Feed the raw read-return timestamp (ns); return the cadence-locked timestamp (ns)."""
        t_meas = float(t_meas_ns)
        if self.t_locked is None:
            self.reset(t_meas)
            return int(self.t_locked)

        gap = t_meas - self._last_meas
        self._last_meas = t_meas

        # Hard re-lock: a big inter-read gap is a genuine cadence break (drop/stall/pause) — the
        # old grid is stale, so re-anchor to the fresh read rather than emit a coasted stamp.
        if gap > self.relock_gap_ns:
            self.relocks += 1
            self.reset(t_meas)
            return int(self.t_locked)

        predicted = self.t_locked + self.dt_est
        error = t_meas - predicted              # phase error vs the one-period grid

        if abs(error) > self.outlier_ns:
            # Bughunt #5: before treating an off-grid read as an outlier, test the multi-period
            # DROP hypothesis. When the driver drops 1-3 frames, the next read lands ON the grid
            # k periods out (the device cadence never moved) — the old code advanced the grid
            # exactly ONE period regardless, so every subsequent frame was stamped (k-1) periods
            # too OLD (inflated frame_age -> over-lead -> early fire) for up to `relock_after`
            # frames until the outlier run finally forced a re-lock. Quantize against the
            # SMOOTHED grid (not the previous raw read: differencing two raw reads doubles the
            # jitter) and only accept a k that lands within drop_snap of an integer multiple.
            k = int(round((t_meas - self.t_locked) / self.dt_est)) if self.dt_est > 0 else 1
            if k >= 2:
                pred_k = self.t_locked + k * self.dt_est
                err_k = t_meas - pred_k         # residual vs the k-quantized grid
                if abs(err_k) <= self.drop_snap_ns:
                    self._skip_run += 1
                    if self._skip_run >= self.relock_after:
                        # Sustained multi-period gaps are a cadence CHANGE (e.g. 60->30fps),
                        # not drops: re-lock rather than riding k-multiples of a dead grid.
                        self.relocks += 1
                        self.reset(t_meas)
                        return int(self.t_locked)
                    # Genuine drop: advance k periods (on-grid stamp), normal in-lock tracking.
                    self._outlier_run = 0
                    self.t_locked = pred_k + self.alpha * err_k
                    self.dt_est += self.beta * err_k
                    if self.dt_est < self._freq_lo:
                        self.dt_est = self._freq_lo
                    elif self.dt_est > self._freq_hi:
                        self.dt_est = self._freq_hi
                    return int(self.t_locked)
            self._outlier_run += 1
            if self._outlier_run >= self.relock_after:
                # Sustained off-grid reads => the cadence really changed; re-lock.
                self.relocks += 1
                self.reset(t_meas)
                return int(self.t_locked)
            # Transient jitter spike: ride the grid one period, ignore the jerk.
            self.t_locked = predicted
            return int(self.t_locked)

        # In lock: slow phase pull toward the measurement + slower frequency track.
        self._outlier_run = 0
        self._skip_run = 0
        self.t_locked = predicted + self.alpha * error
        self.dt_est += self.beta * error
        if self.dt_est < self._freq_lo:
            self.dt_est = self._freq_lo
        elif self.dt_est > self._freq_hi:
            self.dt_est = self._freq_hi
        return int(self.t_locked)


def _device_names() -> Optional[List[str]]:
    raw = os.environ.get("ORION_VIDEO_DEVICE_NAMES", "")
    if not raw.strip():
        return None
    return [n.strip() for n in raw.split("|")]


def _classify_name(name: str) -> str:
    low = name.lower()
    # webcam hints win on conflict (e.g. "Elgato Facecam" is a webcam by a card vendor)
    if any(h in low for h in _WEBCAM_NAME_HINTS):
        return "webcam"
    if any(h in low for h in _CARD_NAME_HINTS):
        return "card"
    return "unknown"


def _load_cached_index() -> int:
    try:
        import json
        with open(_INDEX_CACHE, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("index", -1))
    except Exception:
        return -1


def _save_cached_index(idx: int, name: str = "") -> None:
    try:
        import json
        os.makedirs(os.path.dirname(_INDEX_CACHE), exist_ok=True)
        with open(_INDEX_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"index": int(idx), "name": name}, fh)
    except Exception:
        pass


class CaptureCardBackend:
    """Capture frames from a video-capture device via cv2.VideoCapture."""

    def __init__(
        self,
        device_index: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 60,
        use_mjpg: bool = True,
    ) -> None:
        self._device_index = int(device_index)
        self._width = int(width)
        self._height = int(height)
        self._fps = int(fps)
        self._use_mjpg = bool(use_mjpg)

        self._ring = FrameRingBuffer(capacity=2)
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cap = None
        # Bughunt #7: a handle abandoned by stop() because the reader was still blocked inside
        # cap.read(). Kept referenced so the GC finalizer (cv2 release) can never fire on an
        # arbitrary thread mid-read; the reader's own teardown (or process exit) reclaims it.
        self._wedged_cap = None
        self._api_name = ""
        self._frame_number = 0
        self._source_generation = 0
        self._last_geom = (0, 0)
        # frame-freshness clock for is_healthy(): a wedged MSMF/DSHOW device can BLOCK read()
        # forever (live 2026-07-04: frame counter stuck 17.5s, reader thread alive, zero "read
        # failing" logs) — thread-aliveness alone reported "healthy" while the feed was dead, so
        # the orchestrator's in-process re-open never fired and the native watchdog restarted the
        # whole sidecar instead (reconnect churn + blind-fire window).
        self._last_put_ns = 0
        self._started_ns = 0
        self._stall_logged = False
        # A capture driver can degrade without stopping: the live HD60 X session on
        # 2026-07-31 kept returning valid, changing frames at only 2-4fps. The old
        # freshness-only health check considered that healthy forever because a frame
        # still arrived inside _STALL_S. Keep a bounded arrival window so is_healthy()
        # can distinguish a short scheduler hiccup from a sustained unusable cadence.
        self._arrival_lock = threading.Lock()
        self._arrival_ns = deque(maxlen=max(32, self._fps * 8))
        # Per-publication timing proof, kept as primitive tuples so the reader's
        # hot path does no formatting, aggregation, or logging.  Tuple schema:
        #   (publish_perf_ns, publish_epoch_ns, frame_number,
        #    read_start_ns, read_return_ns, isolate_done_ns)
        # ``cadence_stats`` copies this bounded ring under the same short lock as
        # ``_arrival_ns`` and performs every calculation after releasing it.
        self._cadence_samples = deque(maxlen=max(32, self._fps * 8))
        self._cadence_health_window_s = max(
            2.0, _env_float("ORION_CAPTURE_HEALTH_WINDOW_S", 5.0))
        self._cadence_health_grace_s = max(
            self._cadence_health_window_s,
            _env_float("ORION_CAPTURE_HEALTH_GRACE_S", 10.0),
        )
        # A legitimate 30fps negotiation remains usable; a severely degraded
        # nominal-60fps feed recovers. This floor is operator-tunable per card.
        self._min_health_fps = max(
            1.0, min(float(self._fps),
                     _env_float("ORION_CAPTURE_MIN_HEALTH_FPS",
                                max(12.0, float(self._fps) * 0.35))))
        self._cadence_logged = False

        # --- content-stall auto-recover (ORION_CAPTURE_STALL_REOPEN, opt-in) -------------
        # A USB capture card can WEDGE by re-serving the SAME buffer: cap.read() keeps
        # returning ok=True with a valid-but-BYTE-FROZEN frame, so the bad-read counter and
        # the freshness clock above stay happy while the feed is dead (uniqfps->0, the
        # game looks fine on the passthrough but the bot's grab is stuck). This detects a
        # CONTENT freeze (a cheap downsample hash stops changing) and RE-OPENS the device in
        # place to clear the wedge -- the driver-level recovery the read-failure path never
        # triggers. Guards: a fresh reopen gets a grace window; a source a reopen cannot
        # un-freeze (a genuinely static menu) backs off after a few tries and re-arms only
        # once the content moves again, so it never loops on a legitimately static screen.
        # Exact pixel equality alone cannot distinguish a wedged capture device from a
        # legitimately static source (ESRB splash, pause/menu, fade, HDMI no-signal slate).
        # Re-opening an exclusive DirectShow device on that ambiguous signal creates the
        # disconnect loop it is meant to cure.  Keep the experiment available for a rig
        # whose driver has been live-validated, but never enable it implicitly.  Proven
        # transport stalls (read() stops returning) remain covered by is_healthy() and the
        # orchestrator/native transport-age recovery path.
        self._stall_reopen = _env_flag("ORION_CAPTURE_STALL_REOPEN", False)
        # 800ms, not 300ms: a real gameplay scene is never byte-identical that long, while a
        # MENU/pause/loading screen legitimately is. At 300ms those static screens burn the
        # reopen budget every cooldown for no reason, and each reopen is a chance to lose the
        # device to a driver race. A genuine wedge stays wedged, so the only cost of the longer
        # window is ~500ms of extra recovery latency, once.
        self._stall_ms = max(200.0, _env_float("ORION_CAPTURE_STALL_MS", 800.0))
        self._stall_cooldown_ns = int(max(1.0, _env_float("ORION_CAPTURE_STALL_COOLDOWN_S", 2.0)) * 1e9)
        # Track feed freeze state for downstream signaling. After _MAX_STALL_REOPENS failed
        # reopens the feed is genuinely frozen; this flag is stamped on FrameData so
        # the sidecar can emit feed_healthy=false and the engine can suppress blind fires.
        self._feed_frozen = False
        self._max_stall_reopens = 5
        # Handle-release settle window for the in-place stall re-open. The capture card is
        # EXCLUSIVE-ACCESS: while this process still holds the handle a second open of the same
        # device node cannot succeed, so the re-open must RELEASE FIRST and then wait out the
        # DirectShow handle-release window (2500-4000ms observed live on the Elgato — the same
        # window kSidecarRestartDelayMs / kCapturePreviewResumeDelayMs were picked from).
        self._reopen_settle_s = max(0.0, _env_float("ORION_CAPTURE_REOPEN_SETTLE_S", 3.0))
        # While a re-open is in flight the device is deliberately CLOSED, so no frames land and
        # the _STALL_S freshness check in is_healthy() would fire mid-recovery (the orchestrator
        # would detach the backend before the fresh handle exists). Bounded grace: perf-clock
        # deadline, armed only for the release+settle+open sequence, so a re-open that hangs
        # still reports unhealthy.
        self._reopen_grace_ns = 0
        # Set by _open() on failure: "busy" (node opened, no frames) vs "absent" (no node).
        self._open_diag = ""
        self._buffersize = max(1, int(_env_float("ORION_CAPTURE_BUFFERSIZE", 1.0)))
        self._negotiated_width = 0
        self._negotiated_height = 0
        self._negotiated_fps = 0.0
        self._negotiated_fourcc = ""
        self._negotiated_buffer_size = 0.0
        # Detector-frame contract.  A capture card may silently ignore our requested
        # 1920x1080 mode and hand back a webcam-like 640x480 frame; sending that onward
        # either destroys meter detail or tempts a caller to stretch it.  Fail closed at
        # 1280x720, 16:9 by default.  These are deliberately minimums (720p and 1080p are
        # both valid detector inputs), not a resize target.
        self._min_width = max(1, int(_env_float("ORION_CAPTURE_MIN_WIDTH", 1280.0)))
        self._min_height = max(1, int(_env_float("ORION_CAPTURE_MIN_HEIGHT", 720.0)))
        self._aspect_ratio = 16.0 / 9.0
        self._aspect_tolerance = max(
            0.001, min(0.25, _env_float("ORION_CAPTURE_ASPECT_TOLERANCE", 0.035)))
        # A second full-frame comparison after copying is intentional: it proves the
        # driver buffer did not change while ownership crossed into Orion.  The copy is
        # always made; this flag only exists as a diagnostic escape hatch for unusually
        # slow hardware and defaults fail-safe/on.
        self._verify_copy = _env_flag("ORION_CAPTURE_VERIFY_COPY", True)
        self._integrity_rejects = {}
        self._last_integrity_error = ""
        self._bad_integrity_run = 0
        self._sig = None
        self._sig_change_ns = 0
        self._last_reopen_ns = 0
        self._reopens_no_change = 0

        # --- PTS-alignment / cadence-lock ---------------------------------
        # ORION_CAPTURE_PTS_LOCK (default on): snap each frame timestamp to the fps grid to
        #   kill read-return jitter in frame_age_ms. Off => raw read-return stamps (legacy).
        # ORION_CAPTURE_DEVICE_LATENCY_MS (default 0): ms subtracted from the stamp so the
        #   frame is dated at true glass-time (read happened LATER than capture). Default 0 keeps
        #   the current mean; set to HD60X_DEVICE_LATENCY_ESTIMATE_MS (~35) after live confirmation.
        # ORION_CAPTURE_USE_HW_PTS (default on): if CAP_PROP_POS_MSEC proves monotonic at cadence,
        #   use that device timeline as the lock's measurement (jitter-free at source) instead of the
        #   wall clock. The proof gate remains mandatory, so invalid device clocks stay on QPC.
        self._pts_lock = _env_flag("ORION_CAPTURE_PTS_LOCK", True)
        self._latency_ns = int(max(0.0, _env_float("ORION_CAPTURE_DEVICE_LATENCY_MS", 0.0)) * 1e6)
        # Prefer a real device timeline when the bounded cadence probe validates it;
        # invalid/absent hardware PTS automatically falls back to read-return QPC.
        self._hw_pts_enabled = _env_flag("ORION_CAPTURE_USE_HW_PTS", True)
        self._cadence = CadenceLock(self._fps)
        self._perf_to_epoch_off_ns = 0          # perf_counter_ns -> time_ns offset (slow EMA)
        # hardware-PTS probe state
        self._hw_pts_prev_ms = None
        self._hw_pts_good = 0
        self._hw_pts_anchor = None              # (pos_ms0, perf_ns0) once monotonic
        self._hw_pts_decided = False
        self._hw_pts_ok = False

    # -- lifecycle ---------------------------------------------------------

    def _api_order(self) -> List:
        import cv2
        if os.name == "nt":
            dshow = (cv2.CAP_DSHOW, "DSHOW")
            msmf = (cv2.CAP_MSMF, "MSMF")
            # DSHOW first by default — faster and more predictable frame ordering; MSMF fallback.
            # ORION_CAPTURE_API=msmf forces MSMF first: on some cards (HD60X) DSHOW is the one
            # that wedges/re-serves stale buffers, and MSMF rides through it.
            pref = os.environ.get("ORION_CAPTURE_API", "").strip().lower()
            if pref == "msmf":
                return [msmf, dshow]
            return [dshow, msmf]
        return [(cv2.CAP_ANY, "ANY")]

    def active_route(self):
        """Return ``(actual_api, resolved_index)`` for warm-cache authority.

        The configured index and API preference are not proof of the route that
        opened: candidate selection may resolve another index and DSHOW can fall
        back to MSMF.  Callers must re-check this snapshot while frames flow,
        because the in-place stall recovery can also switch capture APIs.
        """
        return str(self._api_name or "").strip().upper(), int(self._device_index)

    def negotiated_mode(self):
        """Return exact driver-reported mode fields used by timing-cache provenance."""
        return (
            int(self._negotiated_width), int(self._negotiated_height),
            float(self._negotiated_fps), str(self._negotiated_fourcc or "").strip().upper(),
            float(self._negotiated_buffer_size),
        )

    def warm_cache_route_matches(self, directshow_index: int) -> bool:
        """True only when the live route matches native's DirectShow identity row."""
        try:
            expected_index = int(directshow_index)
        except (TypeError, ValueError, OverflowError):
            return False
        api_name, resolved_index = self.active_route()
        return api_name == "DSHOW" and resolved_index == expected_index

    def _contract_reason(self, frame) -> str:
        return _frame_contract_reason(
            frame,
            min_width=self._min_width,
            min_height=self._min_height,
            aspect_ratio=self._aspect_ratio,
            aspect_tolerance=self._aspect_tolerance,
        )

    def _note_integrity_reject(self, reason: str) -> None:
        reason = str(reason or "unknown")
        self._last_integrity_error = reason
        self._integrity_rejects[reason] = self._integrity_rejects.get(reason, 0) + 1
        self._bad_integrity_run += 1
        n = self._integrity_rejects[reason]
        if n == 1 or n % 60 == 0:
            logger.warning(
                "Capture-card frame rejected by integrity contract: reason=%s "
                "run=%d total_for_reason=%d (required >=%dx%d, %.3f aspect)",
                reason, self._bad_integrity_run, n, self._min_width, self._min_height,
                self._aspect_ratio,
            )

    def integrity_stats(self) -> dict:
        """Small read-only snapshot for orchestrator/sidecar health telemetry."""
        return {
            "rejects": dict(self._integrity_rejects),
            "last_error": self._last_integrity_error,
            "bad_run": int(self._bad_integrity_run),
        }

    def cadence_stats(self, window_s: Optional[float] = None) -> dict:
        """Return verified publication cadence over a bounded recent window.

        The reader records only primitive stage timestamps after a verified frame
        is put in the backend ring.  The worst publication gap is decomposed into
        blocking ``cap.read()``, integrity isolation, and remaining reader work.
        This distinguishes an upstream read wait from an Orion-side copy/publish
        delay without putting any aggregation or logging on the per-frame hot path.
        """
        try:
            span_s = float(window_s) if window_s is not None \
                else float(self._cadence_health_window_s)
        except (TypeError, ValueError, OverflowError):
            span_s = float(self._cadence_health_window_s)
        span_ns = int(max(0.1, span_s) * 1e9)
        now_ns = time.perf_counter_ns()
        # Copy only while holding the reader's lock.  Filtering, gap scans, and
        # attribution run outside it so diagnostics cannot delay publication by
        # walking the deque while the capture thread is ready to append.
        with self._arrival_lock:
            arrival_snapshot = tuple(self._arrival_ns)
            stage_snapshot = tuple(self._cadence_samples)
        arrivals = tuple(
            int(stamp) for stamp in arrival_snapshot
            if now_ns - int(stamp) <= span_ns
        )
        stages = tuple(
            sample for sample in stage_snapshot
            if now_ns - int(sample[0]) <= span_ns
        )
        if len(arrivals) < 2:
            return {
                "samples": len(arrivals),
                "fps": 0.0,
                "max_gap_ms": 0.0,
                "late_gaps": 0,
                "worst_gap_frame_number": 0,
                "worst_gap_event_ns": 0,
                "read_block_ms": 0.0,
                "isolate_ms": 0.0,
                "post_ms": 0.0,
            }
        gaps = tuple(
            max(0, arrivals[i] - arrivals[i - 1])
            for i in range(1, len(arrivals))
        )
        covered_s = max(1e-9, (arrivals[-1] - arrivals[0]) / 1e9)
        nominal_ns = 1e9 / max(1.0, float(self._fps))
        worst_frame_number = 0
        worst_event_ns = 0
        worst_read_block_ms = 0.0
        worst_isolate_ms = 0.0
        worst_post_ms = 0.0
        if len(stages) >= 2:
            worst_prev = None
            worst_curr = None
            worst_gap_ns = -1
            for previous, current in zip(stages, stages[1:]):
                gap_ns = max(0, int(current[0]) - int(previous[0]))
                if gap_ns > worst_gap_ns:
                    worst_gap_ns = gap_ns
                    worst_prev = previous
                    worst_curr = current
            if worst_prev is not None and worst_curr is not None:
                read_block_ns = max(0, int(worst_curr[4]) - int(worst_curr[3]))
                isolate_ns = max(0, int(worst_curr[5]) - int(worst_curr[4]))
                # Everything in the publication interval outside the current
                # blocking read and integrity isolation is Orion reader work:
                # FrameData/ring publication plus the prior frame's bounded
                # post-publication bookkeeping and loop transition.
                post_ns = max(0, worst_gap_ns - read_block_ns - isolate_ns)
                worst_frame_number = int(worst_curr[2])
                worst_event_ns = int(worst_curr[1])
                worst_read_block_ms = read_block_ns / 1e6
                worst_isolate_ms = isolate_ns / 1e6
                worst_post_ms = post_ns / 1e6

        return {
            "samples": len(arrivals),
            "fps": (len(arrivals) - 1) / covered_s,
            "max_gap_ms": max(gaps) / 1e6,
            "late_gaps": sum(1 for gap in gaps if gap > nominal_ns * 1.5),
            "worst_gap_frame_number": worst_frame_number,
            "worst_gap_event_ns": worst_event_ns,
            "read_block_ms": worst_read_block_ms,
            "isolate_ms": worst_isolate_ms,
            "post_ms": worst_post_ms,
        }

    def _open(self, index=None):
        """Try each capture API in order on `index` (default the configured one); return
        (cap, api_name) for the first that opens AND yields a real frame, else (None, '')."""
        import cv2
        idx = self._device_index if index is None else int(index)
        # Classify the failure for the caller: "absent" (no device node answered on any API)
        # vs "busy" (the node OPENED but never yielded a frame). "busy" is the signature of
        # another application holding the card exclusively (OBS, a stale sidecar) or of the
        # HDMI input carrying no signal -- a completely different fix from "wrong index".
        opened_any = False
        invalid_reason = ""
        for api, name in self._api_order():
            cap = None
            try:
                # Graph BUILD. Serialised against every teardown in this process, including an
                # abandoned reader's late release of this same device (see _DEVICE_GRAPH_LOCK).
                with _DEVICE_GRAPH_LOCK:
                    cap = cv2.VideoCapture(idx, api)
                    opened = bool(cap) and cap.isOpened()
                    if not opened and cap is not None:
                        cap.release()
                if not opened:
                    continue
                opened_any = True
                # Best-effort device config. MJPG lets most USB cards do 1080p60
                # (the default YUY2 caps bandwidth). All set() calls are advisory.
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                if self._use_mjpg:
                    try:
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                    except Exception:
                        pass
                cap.set(cv2.CAP_PROP_FPS, self._fps)
                # Minimise capture latency: a 1-deep driver buffer means read() returns the FRESHEST frame
                # instead of draining a queue of stale ones (the whole point of the card is low latency).
                # Advisory (DSHOW may ignore it); the latest-wins ring below is the real guarantee.
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, self._buffersize)
                except Exception:
                    pass
                ok, frame = cap.read()
                if not ok or frame is None or getattr(frame, "size", 0) == 0:
                    with _DEVICE_GRAPH_LOCK:
                        cap.release()
                    continue
                invalid_reason = self._contract_reason(frame)
                if invalid_reason:
                    logger.warning(
                        "Capture-card %s index %d negotiated a non-detector frame "
                        "(%s, shape=%s); refusing it instead of resizing/stretching",
                        name, idx, invalid_reason, getattr(frame, "shape", None),
                    )
                    with _DEVICE_GRAPH_LOCK:
                        cap.release()
                    continue
                # Log what the driver ACTUALLY negotiated (advisory set() calls are often overridden) so
                # we can confirm 1080p60 + MJPG + a shallow buffer really took, per the latency audit.
                try:
                    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                    fcc = "".join(chr((fourcc >> (8 * k)) & 0xFF) for k in range(4)) if fourcc else "?"
                    self._negotiated_width = int(frame.shape[1])
                    self._negotiated_height = int(frame.shape[0])
                    self._negotiated_fps = float(cap.get(cv2.CAP_PROP_FPS))
                    self._negotiated_fourcc = str(fcc or "").strip().upper()
                    self._negotiated_buffer_size = float(cap.get(cv2.CAP_PROP_BUFFERSIZE))
                    logger.info("Capture-card negotiated: %dx%d @%.0ffps fourcc=%s buffersize=%s (frame %dx%d)",
                                int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                                cap.get(cv2.CAP_PROP_FPS), fcc, cap.get(cv2.CAP_PROP_BUFFERSIZE),
                                frame.shape[1], frame.shape[0])
                except Exception:
                    # Missing negotiated-mode telemetry disables warm-cache reuse; live capture
                    # and cold calibration remain available.
                    self._negotiated_width = 0
                    self._negotiated_height = 0
                    self._negotiated_fps = 0.0
                    self._negotiated_fourcc = ""
                    self._negotiated_buffer_size = 0.0
                return cap, name
            except Exception as exc:
                logger.debug("CaptureCard open via %s failed: %s", name, exc)
                if cap is not None:
                    try:
                        with _DEVICE_GRAPH_LOCK:
                            cap.release()
                    except Exception:
                        pass
        self._open_diag = ("invalid:" + invalid_reason) if invalid_reason else (
            "busy" if opened_any else "absent")
        return None, ""

    def _find_best_device_index(self, max_indices: int = 8, min_bright: float = 12.0) -> int:
        """Scan indices 0..max_indices-1 for the first device that opens AND yields a NON-BLACK
        (mean brightness > min_bright), >=720p frame — i.e. the live capture card, skipping the
        Elgato's black pin and non-capture webcams. Returns that index, or -1 if none. Reuses the
        same DSHOW->MSMF probe as tools/diagnostics/list_capture_devices.py."""
        import cv2
        for idx in range(max(1, int(max_indices))):
            for api, _name in self._api_order():
                cap = None
                try:
                    cap = cv2.VideoCapture(idx, api)
                    if not cap or not cap.isOpened():
                        if cap is not None:
                            cap.release()
                        continue
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                    if self._use_mjpg:
                        try:
                            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                        except Exception:
                            pass
                    ok, frame = cap.read()
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    cap = None
                    if ok and frame is not None and getattr(frame, "size", 0) > 0:
                        if w >= 1280 and h >= 720 and float(np.mean(frame)) > min_bright:
                            return idx
                except Exception:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
        return -1

    def _candidate_indices(self) -> List[int]:
        """Ordered candidate device indices with webcam-named entries EXCLUDED. Configured index
        first (explicit user choice from the named picker), then the persisted last-good index,
        then name-matched capture cards, then unknown-named devices. Never a webcam name."""
        names = _device_names()
        cand: List[int] = []

        def add(i: int) -> None:
            if i >= 0 and i not in cand:
                if names is not None and i < len(names) and _classify_name(names[i]) == "webcam":
                    logger.info("Capture-card: skipping index %d (%r looks like a webcam)", i, names[i])
                    return
                cand.append(i)

        add(self._device_index)
        add(_load_cached_index())
        if names is not None:
            for i, n in enumerate(names):
                if _classify_name(n) == "card":
                    add(i)
            for i, n in enumerate(names):
                if _classify_name(n) == "unknown":
                    add(i)
        return cand

    def start(self) -> bool:
        try:
            import cv2  # noqa: F401  (presence check)
        except Exception as exc:
            logger.warning("CaptureCard backend needs opencv (%s)", exc)
            return False

        # QUICK single-pass acquisition (no long internal sleep — the ORCHESTRATOR owns the retry-over-
        # time policy so nothing blocks). Probe the name-gated candidate list in order (configured →
        # persisted → name-matched cards → unknowns; webcam names never opened). The old blind
        # brightness scan over indices 0-7 is now the LAST resort, only when no device-name list
        # exists (pre-rebuild launcher), and only ONCE per process — it opens every video device
        # including the webcam, and riding the ~2s retry loop was the "camera turns on" bug.
        global _BRUTE_SCANNED
        names = _device_names()
        cap, api_name = None, ""
        # A BUSY candidate is the informative one, so remember it across the whole sweep: a later
        # "absent" index must not overwrite the fact that the real card opened-but-would-not-stream.
        any_busy = False
        for idx in self._candidate_indices():
            self._open_diag = ""
            cap, api_name = self._open(idx)
            if cap is not None:
                if idx != self._device_index:
                    logger.info("Capture-card: configured index %d had no live feed; using device "
                                "at index %d", self._device_index, idx)
                self._device_index = idx
                break
            if self._open_diag == "busy":
                any_busy = True
        if cap is None and names is None and not _BRUTE_SCANNED:
            _BRUTE_SCANNED = True
            best = self._find_best_device_index()                # one-shot legacy brightness scan
            if best >= 0:
                if best != self._device_index:
                    logger.info("Capture-card: auto-detected live device at index %d (one-shot scan)",
                                best)
                self._device_index = best
                cap, api_name = self._open(best)
        if cap is None:
            # "no live device" is NOT one failure -- it is two, with opposite fixes. Say which.
            #   busy   : the device node OPENED but never handed over a frame. Another process owns
            #            the card exclusively (OBS with a Video Capture Device source is the usual
            #            one; also a stale sidecar), or the HDMI input is carrying no signal. On
            #            MSMF this surfaces as 0xC00D3704 MF_E_HW_MFT_FAILED_START_STREAMING.
            #            Re-scanning indices can NEVER fix this -- the user must free the device.
            #   absent : nothing answered on that index; a rescan/replug is the right move.
            global _LAST_BUSY_DIAG_NS
            if str(self._open_diag).startswith("invalid:"):
                logger.error(
                    "CaptureCard backend: device opened but its frames violate the detector "
                    "contract (%s). Required >=%dx%d at 16:9; Orion will not stretch or feed "
                    "the malformed/low-resolution image to the bot.",
                    self._open_diag.split(":", 1)[1], self._min_width, self._min_height,
                )
            elif any_busy or self._open_diag == "busy":
                now_ns = time.perf_counter_ns()
                if _LAST_BUSY_DIAG_NS == 0 or (now_ns - _LAST_BUSY_DIAG_NS) > 30e9:
                    _LAST_BUSY_DIAG_NS = now_ns
                    dev = (names[self._device_index]
                           if names is not None and 0 <= self._device_index < len(names) else "capture card")
                    logger.warning(
                        "CaptureCard backend: device IN USE / NO SIGNAL — %r (index %d) opened but "
                        "delivered no frames. Another application is holding the card exclusively "
                        "(most often OBS with a 'Video Capture Device' source — close OBS or remove "
                        "that source), or the HDMI input has no signal (console off / cable out). "
                        "Retrying every ~2s; re-scanning device indices cannot fix this.",
                        dev, self._device_index)
                else:
                    logger.debug("CaptureCard backend: device still busy (index %d)", self._device_index)
            else:
                logger.warning("CaptureCard backend: no live device (index %d, DSHOW/MSMF) — no remote-play "
                               "fallback; caller will retry", self._device_index)
            return False
        _save_cached_index(self._device_index,
                           (names[self._device_index]
                            if names is not None and self._device_index < len(names) else ""))

        self._cap = cap
        self._api_name = api_name
        self._source_generation += 1
        self._stop_evt.clear()
        self._last_put_ns = 0
        self._started_ns = time.perf_counter_ns()
        self._stall_logged = False
        with self._arrival_lock:
            self._arrival_ns.clear()
            self._cadence_samples.clear()
        self._cadence_logged = False
        self._last_integrity_error = ""
        self._bad_integrity_run = 0
        self._thread = threading.Thread(target=self._run, name="CaptureCardReader", daemon=True)
        self._thread.start()
        logger.info("Capture-card capture started (index %d via %s)", self._device_index, api_name)
        logger.info("Capture-card timestamp: pts_lock=%s device_latency=%.1fms hw_pts=%s (target %dfps)",
                    "on" if self._pts_lock else "off", self._latency_ns / 1e6,
                    "on" if self._hw_pts_enabled else "off", self._fps)
        return True

    # _run() gives up after this many consecutive failed reads (>=0.6s at the 5ms retry sleep);
    # is_healthy() then reports the dead thread so the orchestrator detaches + re-opens.
    _BAD_READ_LIMIT = 120
    # Malformed/torn/wrong-geometry frames are successful driver reads, so they do not
    # increment _BAD_READ_LIMIT.  Bound their own run: eight consecutive bad buffers are
    # enough evidence that this negotiated mode is unusable, and exiting lets the
    # orchestrator reopen rather than spinning forever on data the detector must not see.
    _BAD_INTEGRITY_LIMIT = 8

    def _run(self) -> None:
        import cv2  # noqa: F401
        cap = self._cap
        bad = 0
        try:
            while not self._stop_evt.is_set() and cap is not None and self._cap is cap:
                read_start_ns = time.perf_counter_ns()
                try:
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None
                read_return_ns = time.perf_counter_ns()
                read_return_epoch_ns = time.time_ns()
                if not ok or frame is None or getattr(frame, "size", 0) == 0:
                    bad += 1
                    if bad >= self._BAD_READ_LIMIT:
                        logger.warning("Capture-card read failing; stopping (orchestrator falls back)")
                        break
                    time.sleep(0.005)
                    continue
                bad = 0
                try:
                    # Freeze capture/source truth at read return. Integrity validation, the
                    # full ownership copy, ring contention, and callbacks happen later and
                    # must therefore remain visible in downstream frame age. A validated
                    # device PTS may replace only this measurement.
                    ts_perf_ns, ts_epoch_ns = self._stamp_frame(
                        read_return_ns, read_return_epoch_ns)
                    reason = self._contract_reason(frame)
                    if reason:
                        self._note_integrity_reject(reason)
                        if self._bad_integrity_run >= self._BAD_INTEGRITY_LIMIT:
                            logger.error("Capture-card integrity reject limit reached; reader exiting "
                                         "for fail-closed in-process reopen")
                            break
                        continue
                    # Ownership boundary: never publish a cv2/driver-owned array.  The
                    # immutable copy can be shared by detector + preview without either
                    # observing a DMA/backend recycle or mutating the other's pixels.
                    frame, reason = _isolate_immutable_frame(frame, self._verify_copy)
                    isolate_done_ns = time.perf_counter_ns()
                    if frame is None:
                        self._note_integrity_reject(reason)
                        if self._bad_integrity_run >= self._BAD_INTEGRITY_LIMIT:
                            logger.error("Capture-card isolation reject limit reached; reader exiting "
                                         "for fail-closed in-process reopen")
                            break
                        continue
                    self._bad_integrity_run = 0
                    self._last_integrity_error = ""
                    geom = (frame.shape[1], frame.shape[0])
                    if geom != self._last_geom:
                        logger.info("Capture-card frame geometry %dx%d", geom[0], geom[1])
                        self._last_geom = geom
                    self._frame_number += 1
                    publication_perf_ns = time.perf_counter_ns()
                    publication_epoch_ns = time.time_ns()
                    fd = FrameData(
                        frame=frame,
                        timestamp_ns=ts_perf_ns,
                        epoch_ns=ts_epoch_ns,
                        capture_timestamp_ns=ts_perf_ns,
                        capture_epoch_ns=ts_epoch_ns,
                        publication_timestamp_ns=publication_perf_ns,
                        publication_epoch_ns=publication_epoch_ns,
                        frame_number=self._frame_number,
                        feed_frozen=self._feed_frozen,
                        capture_api=str(self._api_name or "").strip().upper(),
                        capture_device_index=int(self._device_index),
                        capture_width=int(self._negotiated_width),
                        capture_height=int(self._negotiated_height),
                        capture_fps=float(self._negotiated_fps),
                        capture_fourcc=str(self._negotiated_fourcc),
                        capture_buffer_size=float(self._negotiated_buffer_size),
                        source_generation=int(self._source_generation),
                        integrity_isolated=True,
                    )
                    self._ring.put(fd)
                    publish_perf_ns = time.perf_counter_ns()
                    publish_epoch_ns = time.time_ns()
                    # Liveness/stall detection uses the verified ring-publication time, never the
                    # smoothed frame stamp: a coasted cadence-lock value must not mask a genuine
                    # feed freeze, and a frame is not live until consumers can actually acquire it.
                    self._last_put_ns = publish_perf_ns
                    with self._arrival_lock:
                        self._arrival_ns.append(publish_perf_ns)
                        self._cadence_samples.append((
                            publish_perf_ns,
                            publish_epoch_ns,
                            int(self._frame_number),
                            int(read_start_ns),
                            int(read_return_ns),
                            int(isolate_done_ns),
                        ))
                    self._stall_logged = False
                    if self._reopen_grace_ns:
                        # A fresh frame landed => the in-place re-open completed; drop the
                        # is_healthy() grace immediately so a later stall is caught normally.
                        self._reopen_grace_ns = 0
                    # --- capture-stall auto-recover: a wedged card re-serves the SAME buffer,
                    # so read() succeeds but the PIXELS are byte-frozen. Detect the content
                    # freeze (cheap downsample hash) and re-open the device in place to clear it.
                    if self._stall_reopen:
                        try:
                            sig = hash(frame[::53, ::53].tobytes())
                        except Exception:
                            sig = None
                        if sig is not None and sig != self._sig:
                            self._sig = sig
                            self._sig_change_ns = read_return_ns
                            self._reopens_no_change = 0
                            if self._feed_frozen:
                                self._feed_frozen = False
                                logger.info("Capture-card feed recovered (content changed after freeze)")
                        elif (sig is not None and self._sig_change_ns
                                and (read_return_ns - self._sig_change_ns) / 1e6 > self._stall_ms
                                and (read_return_ns - self._last_reopen_ns) > self._stall_cooldown_ns
                                and self._reopens_no_change < self._max_stall_reopens):
                            frozen_ms = (read_return_ns - self._sig_change_ns) / 1e6
                            self._last_reopen_ns = read_return_ns
                            self._reopens_no_change += 1
                            logger.warning("Capture-card CONTENT STALL: feed byte-frozen %.0fms "
                                           "(uniqfps->0) -> re-opening device (attempt %d)",
                                           frozen_ms, self._reopens_no_change)
                            # RELEASE FIRST, THEN RE-OPEN. The capture card is EXCLUSIVE-ACCESS, so
                            # the old order (open a NEW handle while this process still held the old
                            # one) could never succeed: every re-open failed, the recovery was a
                            # no-op that just burned its 5-attempt budget and latched feed_frozen.
                            # The native side asserts the same invariant ("The Elgato can only be
                            # opened once, so tear the preview down FIRST", RemotePlaySession.cpp).
                            self._reopen_grace_ns = time.perf_counter_ns() + int(
                                (self._reopen_settle_s + self._REOPEN_OPEN_BUDGET_S) * 1e9)
                            self._cap = None
                            try:
                                cap.release()
                            except Exception as exc:
                                logger.debug("Capture-card stall re-open: release failed: %s", exc)
                            cap = None
                            # DirectShow releases the device asynchronously (2500-4000ms window
                            # observed live); re-opening inside it just fails again. Wait it out —
                            # and abort instantly if stop() arrives (wait() returns True when set).
                            if self._stop_evt.wait(self._reopen_settle_s):
                                self._reopen_grace_ns = 0
                                break
                            newcap, newapi = self._open()
                            if newcap is None:
                                # No handle left to read from: end the reader. is_healthy() then
                                # reports dead (thread gone) and the orchestrator detaches + re-opens
                                # the backend in-process on its ~2s retry cadence.
                                self._reopen_grace_ns = 0
                                self._feed_frozen = True
                                logger.warning("Capture-card stall re-open FAILED (device did not come "
                                               "back %.1fs after release) — reader exiting so the "
                                               "orchestrator re-opens the backend", self._reopen_settle_s)
                                break
                            if self._stop_evt.is_set():
                                # stop() ran during the settle/open and already nulled self._cap.
                                # Publishing the fresh handle here would RESURRECT a device nobody
                                # owns (and nobody would ever release). Release it on this — the
                                # reader — thread, which is the only thread allowed to, and exit.
                                try:
                                    newcap.release()
                                except Exception as exc:
                                    logger.debug("Capture-card re-open release-on-stop failed: %s", exc)
                                self._reopen_grace_ns = 0
                                logger.info("Capture-card re-open discarded: stop() arrived mid-recovery")
                                break
                            cap = newcap
                            self._cap = cap
                            self._api_name = newapi
                            self._source_generation += 1
                            # NB: _sig is deliberately NOT reset — an unchanged signature after the
                            # re-open is what lets the 5-attempt budget expire on a legitimately
                            # static screen instead of re-opening forever.
                            self._sig_change_ns = time.perf_counter_ns()   # grace for the fresh device
                            self._feed_frozen = False
                            # _reopen_grace_ns stays ARMED until the first fresh frame lands (cleared
                            # in the put path above) so is_healthy() covers the whole recovery.
                            logger.info("Capture-card re-opened via %s after stall (settle %.1fs)",
                                        newapi, self._reopen_settle_s)
                            continue
                        # After the reopen budget is spent, signal feed_frozen on every
                        # subsequent FrameData so the sidecar/engine knows the feed is dead.
                        if self._reopens_no_change >= self._max_stall_reopens:
                            self._feed_frozen = True
                except Exception as exc:
                    # A malformed frame (or a transient allocation failure) must skip THIS frame, never
                    # kill the reader thread: a dead reader forces the orchestrator to tear down and
                    # re-open the whole backend. Non-fatal — the read above already succeeded, so the
                    # device is fine and the next frame is processed normally.
                    logger.debug("Capture-card frame post-process skipped: %s", exc)
                    continue
        finally:
            # Bughunt #7: the READER thread owns the cv2 handle teardown. Releasing from any
            # other thread while this one is blocked inside cap.read() can access-violate in
            # MSMF/DSHOW and take down the whole sidecar — so release happens HERE, after the
            # last read has returned (also covers the wedged-then-recovered case: stop()
            # abandoned the handle, the read finally returned, and we reclaim it safely).
            if self._cap is cap:
                self._cap = None
            if cap is not None:
                try:
                    # Graph TEARDOWN. This is the exact line the 2026-08-12 heap-corruption dump
                    # caught racing a concurrent _open() of the same device after stop() abandoned
                    # this reader. The lock makes the recovery re-open wait for this release (or
                    # vice versa) instead of interleaving driver-side graph mutations.
                    with _DEVICE_GRAPH_LOCK:
                        cap.release()
                except Exception as exc:
                    logger.debug("Capture-card reader release failed: %s", exc)
                # Released on the OWNING thread => the finalizer is now a no-op, so the
                # module-level keep-alive (if stop() abandoned this handle) can be dropped.
                _discard_abandoned_cap(cap)

    # -- timestamp stamping (PTS-alignment) --------------------------------

    def _stamp_frame(self, read_perf_ns: int, read_epoch_ns: int):
        """Turn a raw (read_perf_ns, read_epoch_ns) pair into the cadence-locked, latency-
        corrected (timestamp_ns, epoch_ns) to stamp on the frame.

        Both output clocks stay identical to the legacy stamps' clocks — perf_counter_ns and
        time_ns — so nothing downstream needs to change; only the jitter is removed."""
        # Track the near-constant perf->epoch offset once, drift-corrected with a slow EMA, so we
        # can derive a jitter-free epoch stamp from the locked perf stamp (both were sampled at the
        # same read instant, so their difference is the offset).
        off = read_epoch_ns - read_perf_ns
        if self._perf_to_epoch_off_ns == 0:
            self._perf_to_epoch_off_ns = off
        else:
            self._perf_to_epoch_off_ns = int(self._perf_to_epoch_off_ns * 0.99 + off * 0.01)

        meas_ns = self._measure_ns(read_perf_ns)
        locked_perf = self._cadence.update(meas_ns) if self._pts_lock else meas_ns
        # Device latency: the glass event happened BEFORE read() returned, so date the frame
        # earlier => frame_age_ms reflects true glass->detector time. Default 0 (no mean change).
        locked_perf -= self._latency_ns
        locked_epoch = int(locked_perf) + self._perf_to_epoch_off_ns
        return int(locked_perf), int(locked_epoch)

    def _measure_ns(self, read_perf_ns: int) -> int:
        """Measurement fed to the cadence lock. Wall-clock read-return time by default; the
        anchored hardware PTS (CAP_PROP_POS_MSEC) when it is enabled AND proven monotonic.

        Also probes CAP_PROP_POS_MSEC once and logs whether the card exposes a usable per-frame
        hardware timestamp (many USB cards report 0/garbage; some report a monotonic device clock)."""
        cap = self._cap
        if cap is None:
            return read_perf_ns
        # HOT PATH: cap.get(CAP_PROP_POS_MSEC) is a DirectShow filter-graph round-trip, and it was
        # being issued on EVERY frame forever. It is only needed while the one-shot probe is still
        # deciding (~45 frames) or when the hardware-PTS timeline is actually in use. With
        # When ORION_CAPTURE_USE_HW_PTS is explicitly off the value is dead weight once the probe
        # latches, so skip the query entirely from then on.
        # (Also skips it when the probe decided the card's PTS is unusable — the branch below
        # requires _hw_pts_ok, so pos_ms would be read and thrown away every frame.)
        if self._hw_pts_decided and not (self._hw_pts_enabled and self._hw_pts_ok):
            return read_perf_ns
        try:
            import cv2
            pos_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC))
        except Exception as exc:
            logger.debug("Capture-card CAP_PROP_POS_MSEC read failed: %s", exc)
            pos_ms = 0.0

        if not self._hw_pts_decided:
            self._probe_hw_pts(pos_ms, read_perf_ns)

        if self._hw_pts_enabled and self._hw_pts_ok and self._hw_pts_anchor is not None and pos_ms > 0.0:
            pos_ms0, perf0 = self._hw_pts_anchor
            # Device timeline anchored onto the perf clock: jitter-free at the source. The lock's
            # slow frequency term still absorbs device-vs-perf clock drift.
            return int(perf0 + (pos_ms - pos_ms0) * 1e6)
        return read_perf_ns

    def _probe_hw_pts(self, pos_ms: float, read_perf_ns: int) -> None:
        """Decide (once) whether CAP_PROP_POS_MSEC advances monotonically at ~frame cadence."""
        dt_nom_ms = 1000.0 / (self._fps if self._fps > 0 else 60.0)
        if pos_ms and pos_ms > 0.0 and self._hw_pts_prev_ms is not None:
            delta = pos_ms - self._hw_pts_prev_ms
            # accept ~1..2 nominal periods of forward motion (allow an occasional dropped frame)
            if 0.4 * dt_nom_ms <= delta <= 2.5 * dt_nom_ms:
                self._hw_pts_good += 1
                if self._hw_pts_anchor is None:
                    self._hw_pts_anchor = (pos_ms, read_perf_ns)
            else:
                self._hw_pts_good = 0
                self._hw_pts_anchor = None
        self._hw_pts_prev_ms = pos_ms if (pos_ms and pos_ms > 0.0) else None

        # Decide after ~30 frames of evidence.
        if self._hw_pts_good >= 20:
            self._hw_pts_ok = True
            self._hw_pts_decided = True
            logger.info("Capture-card hardware PTS (CAP_PROP_POS_MSEC): monotonic at cadence — "
                        "%s as lock measurement", "USING" if self._hw_pts_enabled else "available (disabled)")
        elif self._frame_number >= 45:
            self._hw_pts_ok = False
            self._hw_pts_decided = True
            logger.info("Capture-card hardware PTS (CAP_PROP_POS_MSEC): not usable "
                        "(pos_ms=%.1f) — locking on wall-clock read time", pos_ms)

    # a wedged device blocks read() with no frames; startup gets a longer leash than steady-state
    _STALL_S = 2.5
    _STARTUP_GRACE_S = 8.0
    # How long the re-open itself (device enumeration + first frame) may take after the settle
    # wait, before is_healthy() stops covering it. Bounds the grace at settle + this.
    _REOPEN_OPEN_BUDGET_S = 3.0

    def is_healthy(self) -> bool:
        """True while the reader thread is pumping FRESH frames. False when (a) _run() gave up
        after _BAD_READ_LIMIT (120) consecutive bad reads (thread dead: USB hiccup / HDMI
        renegotiation / device grabbed), or (b) the device WEDGED with read() blocking forever — thread alive but no new
        frame for _STALL_S (live 2026-07-04: counter froze 17.5s while the thread stayed alive, so
        the old aliveness-only check said healthy and the native watchdog restarted the whole
        sidecar), or (c) frames keep arriving at a severely degraded cadence for a complete
        health window. The orchestrator DETACHES the backend and re-opens in-process."""
        t = self._thread
        if t is None or not t.is_alive():
            return False
        now = time.perf_counter_ns()
        # In-place stall re-open in flight: the device is deliberately CLOSED for the DirectShow
        # handle-release settle window, so "no fresh frames" is expected, not a fault. Without this
        # the _STALL_S check below fires ~2.5s into a ~3s settle and the orchestrator detaches the
        # backend before the fresh handle exists — the in-place recovery could never complete. The
        # deadline is armed only for release+settle+open and is cleared by the first frame that
        # lands, so a re-open that hangs still falls through and reports unhealthy.
        if self._reopen_grace_ns:
            if now < self._reopen_grace_ns:
                return True
            self._reopen_grace_ns = 0
        if self._last_put_ns > 0:
            stalled = (now - self._last_put_ns) > int(self._STALL_S * 1e9)
        else:
            stalled = self._started_ns > 0 and (now - self._started_ns) > int(self._STARTUP_GRACE_S * 1e9)
        if stalled and not self._stall_logged:
            self._stall_logged = True
            age_s = (now - (self._last_put_ns or self._started_ns)) / 1e9
            logger.warning("Capture-card frame stall %.1fs (device wedged, read() blocking) — "
                           "reporting unhealthy for in-process re-open", age_s)
        if stalled:
            return False

        # A total stall is not the only failure mode. Some drivers continue to return
        # changing buffers at a few frames per second, which keeps _last_put_ns fresh but
        # is unusable for both the detector and preview. Judge only a complete rolling
        # window after startup so one scheduler/GC hitch never churns the exclusive card.
        cadence_bad = False
        if (self._started_ns > 0
                and (now - self._started_ns) >= int(self._cadence_health_grace_s * 1e9)):
            window_ns = int(self._cadence_health_window_s * 1e9)
            cutoff = now - window_ns
            with self._arrival_lock:
                arrival_snapshot = tuple(self._arrival_ns)
            arrivals = [stamp for stamp in arrival_snapshot if stamp >= cutoff]
            if len(arrivals) >= 3:
                covered_s = (now - arrivals[0]) / 1e9
                if covered_s >= self._cadence_health_window_s * 0.80:
                    observed_fps = (len(arrivals) - 1) / max(covered_s, 1e-6)
                    cadence_bad = observed_fps < self._min_health_fps
                    if cadence_bad and not self._cadence_logged:
                        self._cadence_logged = True
                        logger.warning(
                            "Capture-card cadence degraded: %.1ffps over %.1fs "
                            "(minimum %.1ffps) - reporting unhealthy for in-process re-open",
                            observed_fps, covered_s, self._min_health_fps,
                        )
                    elif not cadence_bad:
                        self._cadence_logged = False
        return not cadence_bad

    # stop(): how long to wait for the reader to notice _stop_evt before abandoning a wedged
    # handle. A healthy reader wakes within one frame period (~17ms), so this only elapses in
    # the genuine wedged-read() case.
    _STOP_JOIN_S = 2.0

    def stop(self) -> None:
        """Bughunt #7: NEVER release the cv2 handle from this (non-reader) thread — a wedged
        read() blocks forever and an MSMF/DSHOW release-during-read can access-violate, taking
        down the sidecar in exactly the wedge-recovery path this backend exists for. The reader
        thread owns release (see _run's finally). A wedged reader is ABANDONED: detach the
        handle so a re-open never touches it, keep a reference so the GC finalizer can't release
        it on an arbitrary thread, and let the reader's teardown / process exit reclaim it."""
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self._STOP_JOIN_S)
            if t.is_alive():
                # Reader still blocked inside cap.read(): abandon the wedged handle. The keep-alive
                # reference lives at MODULE scope (_ABANDONED_CAPS) — the orchestrator drops this
                # backend object right after stop(), so an instance attribute would be collected
                # and cv2's finalizer would release the handle under the blocked reader anyway.
                # _wedged_cap is kept purely as a diagnostic alias on the (soon dead) instance.
                _park_abandoned_cap(self._cap)
                self._wedged_cap = self._cap
                self._cap = None
                logger.warning("Capture-card reader still blocked in read() after stop; "
                               "abandoning device handle (reclaimed on reader exit or process exit)")
                return
        # Reader exited (it already released its own handle in _run's finally) or never ran
        # (start() failed before the thread spawned): any handle left here has no reader blocked
        # on it, so an inline release is safe.
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                with _DEVICE_GRAPH_LOCK:
                    cap.release()
            except Exception:
                pass
        logger.info("Capture-card capture stopped")

    # -- frame access (decoder-backend contract) ---------------------------

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        return self._ring.get_latest_nonblocking()

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        return self._ring.get_latest(timeout)
