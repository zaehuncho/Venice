import collections
import json
import logging
import os
import re
import shutil
import sys
import threading
import queue
import time
from dataclasses import dataclass, field, fields, is_dataclass
from functools import wraps
import cv2
import numpy as np
import win32con
from virtual_controller import PhysicalControllerReader, DS4Button
from controller_remap import RemapEngine, RemapConfig
from rtt_sync_engine import RTTSyncEngine, RTTSyncConfig
from decoder_pipe_identity import stable_executable_snapshot
from async_diagnostic_csv import AsyncDiagnosticCsv
logger = logging.getLogger('RemotePlayOrchestrator')


def _with_shot_gate_lock(fn):
    """Serialize short gate/control transitions, never pixel inference.

    Snapshot plus reader handoff must be atomic with a physical arm. Otherwise an
    expired snapshot can clear the epoch that the control thread just installed.
    The fallback supports lightweight diagnostic/test instances built via __new__.
    """
    @wraps(fn)
    def guarded(self, *args, **kwargs):
        lock = getattr(self, '_shot_gate_lock', None)
        if lock is None:
            lock = self.__dict__.setdefault('_shot_gate_lock', threading.RLock())
        with lock:
            return fn(self, *args, **kwargs)
    return guarded


def _configure_opencv_threads() -> int:
    """Bound the process-wide OpenCV pool once, before video workers start."""
    raw = os.environ.get('ORION_CV2_THREADS', '').strip()
    requested = 1
    source = 'default'
    if raw:
        try:
            override = int(raw)
            # The native API takes a signed int. Negative values restore the
            # machine-wide default, defeating this process's bounded pool.
            if not 0 <= override <= 0x7fffffff:
                raise ValueError('thread count is outside the nonnegative int range')
        except (TypeError, ValueError, OverflowError):
            logger.warning('runtime hygiene: invalid ORION_CV2_THREADS=%r; using default=1', raw)
        else:
            requested = override
            source = 'override'
    # Zero disables parallel regions; OpenCV reports one executing thread for
    # both zero and one. Preserve the requested override in the startup log.
    cv2.setNumThreads(requested)
    effective = int(cv2.getNumThreads())
    logger.info('runtime hygiene: OpenCV threads=%d requested=%d source=%s',
                effective, requested, source)
    return effective


# --- stdout JSONL IPC lock (bughunt #4) --------------------------------------------------
# Every JSONL line this process writes to stdout must hold ONE lock: the sidecar's preview
# emitter writes ~130KB base64 lines that split across several underlying BufferedWriter
# writes, so an unlocked concurrent writer (pose_landmark â€” the no-meter RELEASE trigger â€”
# pose_overlay every frame, calibrate_meter_status) could interleave mid-line and corrupt
# BOTH lines (native QJsonDocument::fromJson drops them silently; a lost meter sample at
# the tip = mistimed release). autogreen_sidecar adopts this same lock for its _emit, so
# orchestrator-side and sidecar-side writers serialize together.
STDOUT_EMIT_LOCK = threading.Lock()


def emit_stdout_jsonl(line: str) -> None:
    """Write one newline-terminated JSONL line to stdout under the shared IPC lock."""
    with STDOUT_EMIT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _parse_pose_arm_token(value) -> int:
    """Parse the canonical unsigned 64-bit decimal pose-arm token.

    Native deliberately transports this as a JSON string: accepting floats here
    would reintroduce 53-bit rounding and could make two shots share an identity.
    Zero and non-canonical strings are invalid sentinels.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        token = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        if value[0] == "0":
            return 0
        try:
            token = int(value, 10)
        except (TypeError, ValueError, OverflowError):
            return 0
        if str(token) != value:
            return 0
    else:
        return 0
    return token if 0 < token <= 0xFFFFFFFFFFFFFFFF else 0


# --- PTS->clock offset maintenance (bughunt #3) ------------------------------------------
# A Chiaki reconnect restarts decoder PTS near 0.  Re-seeding is safe only when an
# explicit backend connection generation changes.  A positive offset jump within
# one generation can be pipe backlog and must remain stale, never map itself to now.
_PTS_JUMP_S = 0.25      # pts->wall offset domain (seconds)
_PTS_JUMP_MS = 250.0    # pts->epoch offset domain (milliseconds)


def _pts_offset_update(prev: float, sample: float, jump: float, source_reset: bool = False):
    """EMA-track a PTS->clock calibration offset without laundering backlog.

    Returns (new_offset, reseeded). prev==0.0 is the uninitialized seed (legacy contract);
    only an explicit source-generation reset may re-seed a large discontinuity.
    An unexpected positive offset jump is pipe/decode backlog, not proof of reconnect,
    so the old mapping is retained and downstream age remains honestly stale."""
    if source_reset:
        return sample, True
    if prev == 0.0:
        return sample, False
    if abs(sample - prev) > jump:
        return prev, False
    return prev * 0.9 + sample * 0.1, False


def _detector_frame_contract_reason(
    frame,
    min_width: int = 1280,
    min_height: int = 720,
    aspect_ratio: float = 16.0 / 9.0,
    aspect_tolerance: float = 0.035,
) -> str:
    """Return why a frame is unsafe for detection, or ``""`` when valid.

    This is a detector contract, not a display preference.  Frames are never resized
    here: a low-resolution or non-16:9 grab fails closed so the meter cannot be stretched
    into detector coordinates that no longer match the game image.
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
    actual = width / float(max(1, height))
    if abs(actual - float(aspect_ratio)) / max(0.001, float(aspect_ratio)) \
            > float(aspect_tolerance):
        return "aspect"
    return ""


def _isolate_detector_frame(frame, trusted_isolated: bool = False):
    """Return ``(immutable_owned_frame, reason)`` for the detector boundary.

    A trusted capture backend may prove it already copied the driver buffer.  Every
    other source is copied and then compared with the source once; a difference means
    the source changed while copying and is rejected as a torn/aliased frame.  Read-only
    ownership is asserted before the array is published to detector and preview.
    """
    try:
        if trusted_isolated:
            flags = frame.flags
            if (not bool(flags["C_CONTIGUOUS"]) or not bool(flags["OWNDATA"])
                    or bool(flags["WRITEABLE"])):
                return None, "isolation_contract"
            return frame, ""
        owned = np.array(frame, dtype=np.uint8, order="C", copy=True)
        if not np.array_equal(frame, owned):
            return None, "torn_copy"
        owned.setflags(write=False)
        return owned, ""
    except Exception:
        return None, "isolation"


def _detector_contract_size():
    """[ORION_DETECTOR_1080P 2026-08-11] Resolve the detector's geometry contract.

    DEFAULT IS UNCHANGED (1280x720) -- set ORION_DETECTOR_1080P=1 to feed the detector the card's
    full 1920x1080 instead. A/B-able without a rebuild, same convention as ORION_CAPTURE_MJPG.

    WHY IT MIGHT WIN. The capture card already negotiates 1920x1080; we then downscale to 720p
    before detection. Meanwhile simple_meter_reader's own pixel priors are declared AT 1080p
    (`_REF_W, _REF_H = 1920.0, 1080.0  # priors are px @1080p; scaled to the live capture size`)
    and are scaled DOWN to whatever we hand it. So today we throw away a third of the resolution
    and then shrink the reader's native-resolution priors to match the smaller frame.

    That matters because of what the loss actually costs. Across 321 graded landings the green
    window width predicts almost everything: under 3pp only 5.2% of shots land badly and 3% bounce;
    over 12pp it is 96.6% bad and 78% bounce. 37% of shots sit in the 6pp+ danger zone. A green cap
    a few pixels tall, carried in MJPG 4:2:0 chroma (half vertical resolution) and then scaled by
    0.667, is exactly how a crisp band turns into a smeared one.

    NOT DEFAULT-ON: this is a real behaviour change (more pixels per detect, different rounding in
    every derived box) and it deserves a framedump A/B or a counted live batch first. The
    source-independence contract the caller documents is PRESERVED either way -- every source is
    still normalised to ONE size, just a different one.
    """
    raw = str(os.environ.get('ORION_DETECTOR_1080P', '') or '').strip().lower()
    if raw in ('1', 'true', 'yes', 'on'):
        return 1920, 1080
    return 1280, 720


_DETECTOR_CONTRACT_W, _DETECTOR_CONTRACT_H = _detector_contract_size()


def _normalize_detector_frame(frame, width: int = 1280, height: int = 720):
    """Aspect-preserving normalization to the detector's exact 1280x720 contract.

    Healthy 1920x1080 capture-card/decoder frames are downscaled, never rejected.
    A small aspect mismatch within the source tolerance is center-cropped to 16:9
    before a uniform scale, so no vertical/horizontal stretch can move the meter.
    Returns ``(read_only_owned_frame, reason)``.
    """
    try:
        src_h, src_w = int(frame.shape[0]), int(frame.shape[1])
        dst_w, dst_h = int(width), int(height)
        if src_w == dst_w and src_h == dst_h:
            if bool(frame.flags["OWNDATA"]) and not bool(frame.flags["WRITEABLE"]):
                return frame, ""
            out = np.array(frame, dtype=np.uint8, order="C", copy=True)
        else:
            target_aspect = dst_w / float(dst_h)
            source_aspect = src_w / float(src_h)
            crop = frame
            if source_aspect > target_aspect:
                crop_w = max(1, min(src_w, int(round(src_h * target_aspect))))
                x0 = (src_w - crop_w) // 2
                crop = frame[:, x0:x0 + crop_w]
            elif source_aspect < target_aspect:
                crop_h = max(1, min(src_h, int(round(src_w / target_aspect))))
                y0 = (src_h - crop_h) // 2
                crop = frame[y0:y0 + crop_h, :]
            interpolation = cv2.INTER_AREA if (crop.shape[1] >= dst_w and crop.shape[0] >= dst_h) \
                else cv2.INTER_LINEAR
            out = cv2.resize(crop, (dst_w, dst_h), interpolation=interpolation)
            out = np.ascontiguousarray(out)
        if out.dtype != np.uint8 or out.shape != (dst_h, dst_w, 3):
            return None, "normalization_contract"
        out.setflags(write=False)
        return out, ""
    except Exception:
        return None, "normalization"


def _normalize_detector_y_plane(plane, source_width: int, source_height: int,
                                width: int = 1280, height: int = 720):
    """Normalize an optional decoder Y plane with the exact BGR crop/scale geometry.

    Returning ``None`` is deliberately safe: readers then derive gray from the already
    normalized BGR frame.  A mismatched or mutable native plane must never be paired with
    1280x720 detector pixels because its coordinates would describe a different image.
    """
    if plane is None:
        return None
    try:
        src_w, src_h = int(source_width), int(source_height)
        dst_w, dst_h = int(width), int(height)
        if (not isinstance(plane, np.ndarray) or plane.dtype != np.uint8
                or plane.ndim != 2 or plane.shape != (src_h, src_w)):
            return None
        crop = plane
        target_aspect = dst_w / float(dst_h)
        source_aspect = src_w / float(src_h)
        if source_aspect > target_aspect:
            crop_w = max(1, min(src_w, int(round(src_h * target_aspect))))
            x0 = (src_w - crop_w) // 2
            crop = plane[:, x0:x0 + crop_w]
        elif source_aspect < target_aspect:
            crop_h = max(1, min(src_h, int(round(src_w / target_aspect))))
            y0 = (src_h - crop_h) // 2
            crop = plane[y0:y0 + crop_h, :]
        if crop.shape != (dst_h, dst_w):
            interpolation = cv2.INTER_AREA if (crop.shape[1] >= dst_w and crop.shape[0] >= dst_h) \
                else cv2.INTER_LINEAR
            out = cv2.resize(crop, (dst_w, dst_h), interpolation=interpolation)
        else:
            out = np.array(crop, dtype=np.uint8, order="C", copy=True)
        out = np.ascontiguousarray(out)
        if out.dtype != np.uint8 or out.shape != (dst_h, dst_w):
            return None
        out.setflags(write=False)
        return out
    except Exception:
        return None


def _frames_identical(a, b) -> bool:
    """Exact duplicate check; unlike the retired coarse grid hash it cannot miss a thin meter."""
    if a is None or b is None:
        return False
    try:
        return a.shape == b.shape and a.dtype == b.dtype and bool(np.array_equal(a, b))
    except Exception:
        # Failure to compare is not evidence of equality; the caller's frame contract
        # already rejects malformed arrays before reaching this helper.
        return False
# LEGACY CHAIN RETIRED (2026-07-18): MeterDetector is NO LONGER imported at module
# scope. The shipped live path is the pure-CV SimpleMeterReader (ORION_SIMPLE_READER=1)
# / CompressedMeterReader (ORION_COMPRESSED_READER=1); the retired torch/YOLO serving
# chain (MeterDetector + its flag-gated locator/reader/tracker stack) is imported
# LAZILY inside the legacy else-branch of start() only, so selecting the simple or
# compressed reader never binds the retired chain here. DetectorConfig (a plain
# dataclass, no torch at module scope) is imported lazily where det_config is built.
# NOTE: remote_play_cv still imports meter_detector at ITS module scope for
# DetectResult/DetectorConfig â€” that import is torch-free (torch only loads inside the
# flag-gated *_infer try_load paths, and only when a MeterDetector is instantiated).
try:
    from remote_play_cv import RemotePlayCVConfig, GreenWindowAnalyzer
    CV_AVAILABLE = True
except ImportError as exc:
    CV_AVAILABLE = False
    logger.warning('CV pipeline modules not available: %s', exc)
try:
    from remote_play_client import RemotePlayClientConfig, RemotePlayClientManager, find_remote_play_window
    CLIENT_AVAILABLE = True
except Exception as exc:
    CLIENT_AVAILABLE = False
    RemotePlayClientConfig = None
    RemotePlayClientManager = None
    find_remote_play_window = None
    logger.warning('Remote Play client manager unavailable: %s', exc)
try:
    from remote_play_client import get_standby_pool, standby_client_enabled
except Exception:
    get_standby_pool = None
    standby_client_enabled = None
try:
    # [ORION_CONNECT_LATENCY 2026-09-14] CONSOLE ADDRESS DRIFT prewarm: resolves a silent
    # configured console address off the connect path (see remote_play_client).
    from remote_play_client import prewarm_console_host
except Exception:
    prewarm_console_host = None
try:
    # [ORION_STALL_ATTRIB 2026-09-15] Names the thread that ate a consumer-callback gap.
    # Optional and inert when absent; see stall_attributor.py for the whole instrument.
    import stall_attributor as _stall
except Exception:
    _stall = None

@dataclass(frozen=True, slots=True)
class _DetectionLatencyLabel:
    """Diagnostic phase captured before publication can trigger a native release."""
    release_seq: int
    phase: str


@dataclass
class _MeterTrackPayload:
    """Per-frame tracking the sidecar serializes into the emitted JSON's ``tracking`` object
    (autogreen_sidecar `_dataclass_payload` -> asdict). The native engine reads
    ``tracking.velocity_pct_s`` / ``acceleration_pct_s2`` / ``eta_to_target_ms``
    (RemotePlaySession.cpp) â€” these were 0 because this object was never populated. The detector
    computes the real fill velocity (healthy 250-650 %/s); this carries it to the native half.
    ``fill_pct`` is included so the native engine has a DIRECT path to the detector's real fill
    (vs. the remap engine's shot.fill_pct which can be one frame stale in capture-card mode)."""
    velocity_pct_s: float = 0.0
    acceleration_pct_s2: float = 0.0
    eta_to_target_ms: float = -1.0
    fill_pct: float = -1.0
    coarse_fill_pct: float = -1.0
    fill_estimator_mode: str = ""
    # Canonical decimal string: the native parser rejects JSON numbers so this
    # remains exact if the process-monotonic counter ever exceeds 2^53.
    fill_estimator_generation: str = "0"
    confidence: float = 0.0


def _freeze_flat_payload(value) -> tuple:
    """Copy a flat telemetry object into immutable ``(name, value)`` pairs.

    Tracking/fusion payloads are dataclasses today, but accepting mappings and simple
    namespace-style objects keeps the publication boundary compatible with test doubles
    and future producers.  Only JSON scalar values cross this boundary; an unexpected
    nested/mutable value is deliberately omitted instead of leaking a live reference.
    """
    if value is None:
        return ()
    if isinstance(value, dict):
        items = value.items()
    elif is_dataclass(value):
        items = ((item.name, getattr(value, item.name, None)) for item in fields(value))
    else:
        data = getattr(value, "__dict__", None)
        items = data.items() if isinstance(data, dict) else ()
    frozen = []
    for name, raw in items:
        if isinstance(raw, np.generic):
            raw = raw.item()
        if raw is None or isinstance(raw, (bool, int, float, str)):
            frozen.append((str(name), raw))
    return tuple(frozen)


@dataclass(frozen=True)
class _ProcessedFrameSnapshot:
    """One atomically-published view of detector completion + integrity state.

    The capture, detector, and telemetry loops are separate threads.  Publishing these
    fields individually allowed telemetry to join a new sequence number to the previous
    frame's timestamp/identity, and allowed an old detector completion to overwrite a
    newer capture rejection.  A frozen object assigned through one reference is the
    pairing boundary consumed by the sidecar.
    """
    seq: int = 0
    frame_number: int = 0
    frame_ts: float = 0.0
    epoch_ms: float = 0.0
    # Canonical timing epoch for these pixels. Decoder sources use the PTS clock
    # mapped onto epoch; capture-card sources use epoch_ms unchanged. epoch_ms
    # remains the raw source-publication identity and must not be substituted for
    # this value in sampler/registration math.
    measurement_epoch_ms: float = 0.0
    pts: int = 0
    frame_wh: tuple = (0, 0)
    integrity_healthy: bool = False
    reject_reason: str = ""
    reject_counts: tuple = ()
    backend_frozen: bool = False
    revision: int = 0
    # Actionable detector state is published through the SAME immutable reference as
    # the frame identity above.  Tuples (rather than live dict/dataclass references)
    # keep the frozen snapshot deeply immutable while the processing loop starts work
    # on the next frame.
    meter_present: bool = False
    raw_fed: bool = False
    # Independent current hardware-shot meter proof from SimpleMeterReader. Unlike a
    # monotonic fill trajectory, connected meter structure cannot be minted by a generic
    # animated loading bar. It rides the atomic snapshot so epochs cannot tear.
    gameplay_structure_verified: bool = False
    gameplay_structure_epoch: int = 0
    stage: str = ""
    bbox: tuple = ()
    # [ORION_PROOF_DETECTOR_BOX 2026-09-19] The same frame's DETECTOR rectangle, before the
    # reader's display hug (ORION_READER_BOX_TIGHT). Native judges the ownership proof's shape
    # continuity on this one; `bbox` above stays what is DRAWN. Empty when the reader did not
    # publish one, in which case native falls back to `bbox`.
    det_bbox: tuple = ()
    bbox_wh: tuple = ()
    green: tuple = ()
    tracking: tuple = ()
    fusion: tuple = ()
    shot: tuple = ()
    tip_registration: tuple = ()


@dataclass
class OrchestratorConfig:
    console_ip: str = ''
    # Opaque native-derived identity of the uniquely registered console. Raw MAC, PSN id and
    # registration credentials never cross this boundary. Missing/ambiguous identity disables
    # persisted timing reuse while leaving cold live calibration available.
    console_identity: str = ''
    # Current native-confirmed local delivery backend. It is intentionally empty at process start;
    # a distinct neutral delivery transaction re-keys the estimator before any cache can restore.
    controller_route: str = ''
    capture_mode: str = ''
    platform: str = 'ps5'
    client_mode: str = 'chiaki'
    auto_launch_client: bool = True
    close_client_on_disconnect: bool = True
    chiaki_path: str = ''
    # Native hashes the exact selected OrionStream image before starting the
    # sidecar.  The decoder pipe can use cold timing without these fields, but
    # persisted route timing is impossible unless both survive exact validation.
    chiaki_identity_size: int = -1
    chiaki_identity_sha256: str = ''
    # Filled only from an accepted ORF2 frame. Configured resolution is an intent,
    # not proof of what the decoder actually exported.
    decoder_mode: str = ''
    window_title: str = 'Chiaki'
    # Seconds to wait for the chiaki stream window to appear before treating the launch as failed.
    # chiaki's cold start (~11s Vulkan init + PS5 handshake) can surface the window at ~28s, so the
    # old hidden 18s default bailed before it was up. 40s > the 35s connect-stall trigger, < the 75s
    # startup grace. Threaded into RemotePlayClientConfig in start().
    wait_timeout_s: float = 40.0
    resolution: str = '1920x1080'
    # Capture/polling rate. Higher gives TemporalSuperSampler more meter samples
    # per sweep -> tighter crossing prediction -> more accurate release. Duplicate
    # frames are dropped by _frame_hash, so polling above the Chiaki stream rate
    # only costs CPU on the hash check, never adds noise. 120 is a good default;
    # up to 240 is supported (clamped in the capture loop).
    target_fps: int = 120
    show_video: bool = True
    virtual_controller: bool = True
    hidhide: bool = True
    goto_shot: bool = False
    # DEFAULT CORRECTED 'Purple' -> 'Red' (2026-08-04). This default was DEAD for as long as the
    # park colour pin existed: park_temporal_enabled defaults ON and unconditionally overwrote
    # meter_color with "Red" a few lines into detector construction, so every launch that relied
    # on this default actually ran RED. Now that the pin is correctly scoped to the legacy chain,
    # this default became live again -- and would have silently flipped every default-config
    # entry point (tools, harnesses, a bare sidecar run) from the Red they have really been
    # running to an unvalidated Purple. 'Red' states the effective shipped behaviour honestly and
    # matches both the native default (native_orion/src/AppConfig.h:66) and
    # meter_bar_colors.FALLBACK. Real launches are unaffected either way: the native app always
    # sends an explicit meter_color.
    meter_color: str = 'Red'
    meter_style: str = 'Arrow2'
    confidence_gate: float = 0.32
    green_window_start_pct: float = 93.0
    green_window_end_pct: float = 100.0
    timing_delay_ms: float = 0.0
    latency_compensation_ms: float = 45.0
    stable_frames_required: int = 3
    tempo_flick_hold_ms: float = 50.0
    input_mode: str = 'both'
    no_meter_enabled: bool = False
    no_meter_release_point: str = 'push'
    no_meter_base_offset_ms: float = 83.0
    decode_latency_ms: float = 7.5
    no_meter_confidence_gate: float = 0.70
    no_meter_push_release_window_ms: float = 200.0
    no_meter_handedness: str = 'Right'
    # Default to the simple HOLD-square autogreen flow (tempo/stick remap off).
    shot_trigger_mode: str = 'button'
    active_shot_type: str = 'Standstill'
    shot_type_offsets: dict = field(default_factory=dict)
    # Frame source selection:
    #   'auto'    -> windowed Chiaki client + tiered window capture (default; the
    #                path that keeps video visible and survived the black-screen fix)
    #   'capture' -> force window capture only
    #   'decoder' -> low-latency decoded H.26x frames from a pipe-capable Chiaki
    #                build (chiaki_backend.ChiakiBackend), falling back to window
    #                capture if no decoded frame is available
    #   'wgc'     -> Windows Graphics Capture (wgc_backend.WGCCaptureBackend):
    #                occlusion-proof window/monitor capture, falls back to window
    #                capture if unavailable (also enabled by env ORION_WGC=1)
    #   'capture_card' -> HDMI capture device via cv2.VideoCapture
    #                (capture_card_backend.CaptureCardBackend; also ORION_CAPTURE_CARD=1,
    #                device index ORION_CAPTURE_CARD_INDEX). Falls back if no device.
    frame_source: str = 'auto'


def _configured_decoder_identity(config):
    """Return the native-issued image identity after a fresh stable-file check."""
    executable = str(getattr(config, 'chiaki_path', '') or '').strip()
    expected_sha = str(getattr(
        config, 'chiaki_identity_sha256', '') or '').strip().lower()
    try:
        expected_size = int(getattr(config, 'chiaki_identity_size', -1))
    except (TypeError, ValueError, OverflowError):
        return None
    if (expected_size < 0 or len(expected_sha) != 64
            or any(ch not in '0123456789abcdef' for ch in expected_sha)):
        return None
    snapshot = stable_executable_snapshot(executable)
    if (not snapshot.valid or snapshot.size != expected_size
            or snapshot.sha256 != expected_sha):
        return None
    return snapshot


def _decoder_wire_mode(width: int, height: int, fmt: int) -> str:
    try:
        width, height, fmt = int(width), int(height), int(fmt)
    except (TypeError, ValueError, OverflowError):
        return ''
    if width <= 0 or height <= 0 or fmt not in (0, 1):
        return ''
    return 'orf2:%dx%d:%s' % (width, height, 'nv12' if fmt == 0 else 'i420')


def _capture_mode_descriptor(mode_values) -> str:
    """Return a canonical DirectShow mode identity, or ``''`` when it is ambiguous.

    ``CAP_PROP_BUFFERSIZE`` is advisory and the Windows DirectShow backend reports ``-1``
    (and some drivers report ``0``) when that property is unsupported.  That does not make
    the actual DirectShow device/API/geometry/fps/fourcc route ambiguous.  Preserve the
    unsupported result as an explicit token instead of erasing the whole route identity.
    Unexpected negative values still fail closed.
    """
    try:
        mode_width, mode_height, mode_fps, mode_fourcc, mode_buffer = mode_values
        mode_width, mode_height = int(mode_width), int(mode_height)
        mode_fps, mode_buffer = float(mode_fps), float(mode_buffer)
        mode_fourcc = str(mode_fourcc or '').strip().upper()
    except (TypeError, ValueError, OverflowError):
        return ''

    if (mode_width <= 0 or mode_height <= 0
            or not np.isfinite(mode_fps) or mode_fps <= 0.0
            or len(mode_fourcc) != 4 or not mode_fourcc.isalnum()
            or not np.isfinite(mode_buffer)):
        return ''
    if mode_buffer > 0.0:
        buffer_identity = f'{mode_buffer:.3f}'
    elif mode_buffer in (-1.0, 0.0):
        buffer_identity = 'unreported'
    else:
        return ''
    return (
        f'{mode_width}x{mode_height}@{mode_fps:.3f}'
        f'|fourcc={mode_fourcc.lower()}|buffer={buffer_identity}')


_SCOPE_REJECT_LOGGED: set = set()


def _scope_reject(reason: str) -> str:
    """Log WHY the latency route scope is empty, once per distinct reason per process.

    [ORION_SCOPE_DIAG 2026-08-18] An empty scope makes the sidecar reject every controller
    route attestation with the single opaque token `route_scope_rejected`, and the bot sits
    benched in waiting_for_latency_calibration for the whole session. That exact bench has now
    cost three debugging sessions (2026-08-08 the 72-second bench; 2026-08-17; 2026-08-18 a
    full session with zero bot releases) because nothing ever said WHICH of the dozen
    conditions failed. Fail-closed stays fail-closed -- but it must name itself.
    """
    if reason not in _SCOPE_REJECT_LOGGED:
        _SCOPE_REJECT_LOGGED.add(reason)
        # ERROR, not WARNING, and the level is load-bearing. The native relay
        # throttles sidecar WARNING lines (RemotePlaySession.cpp: isWarning ->
        # kSidecarWarnThrottleMs) but lets ERROR through unthrottled. Emitted at
        # WARNING on 2026-08-18 this line NEVER ONCE reached orion_native.log --
        # it was competing with per-second capture-health and reader warnings --
        # so the diagnostic built specifically to end this bench was itself
        # invisible for another two sessions. A once-per-process cause-of-death
        # line must never be sampled.
        logger.error(
            'Latency route scope EMPTY (%s): timing attestation will be rejected as '
            'route_scope_rejected until this is resolved', reason)
    return ''


# [ORION_CAPTURE_FPS 2026-09-14] Allowed capture-card refresh rates, mirrored from
# native_orion/src/AppConfig.h (AppConfigData::captureCardFps / snappedCaptureCardFps).
# These are the only rates an Elgato-class 1080p HDMI card negotiates.
CAPTURE_FPS_ALLOWED = (30, 60, 120)
CAPTURE_FPS_DEFAULT = 60


def snap_capture_fps(value) -> int:
    """Snap a requested capture-card rate to the nearest allowed mode.

    SNAP, never clamp, and never pass a raw number through: ``cap.set(CAP_PROP_FPS, x)``
    is advisory, so asking a card for 45 yields whatever the driver feels like while
    CadenceLock goes on modelling a 45 Hz grid that does not exist.  Ties resolve to the
    LOWER rate (the safer request on a bandwidth-limited USB card), matching the C++
    ``snappedCaptureCardFps`` exactly.  Anything non-numeric is the default, 60 -- the
    rate every build before this setting existed hard-coded.
    """
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return CAPTURE_FPS_DEFAULT
    return min(CAPTURE_FPS_ALLOWED, key=lambda allowed: (abs(requested - allowed), allowed))


def requested_capture_fps(env=None) -> int:
    """The capture-card rate this launch asks for, from ORION_CAPTURE_FPS.

    Written by RemotePlaySession.cpp in the capture-card branch only (the decoder branch
    removes it), so a missing/blank value is the ordinary decoder or standalone case and
    means "the default, 60".
    """
    source = os.environ if env is None else env
    raw = str(source.get('ORION_CAPTURE_FPS', '') or '').strip()
    if not raw:
        return CAPTURE_FPS_DEFAULT
    return snap_capture_fps(raw)


def capture_suspect_run_frames(fps=None) -> tuple:
    """(black_core_run, static_core_run) frame thresholds for the SUSPECT warning.

    Both used to be hard-sized for a 60fps card (15 and 30 frames = 0.25 s and 0.5 s).
    Expressed in frames they mean a different WALL-CLOCK dropout at every other rate, so
    derive them from the rate the card was actually asked for.  At 60 -- the shipped
    default and the only rate any build before this setting ran -- this returns exactly
    the historical (15, 30).
    """
    rate = max(1, snap_capture_fps(CAPTURE_FPS_DEFAULT if fps is None else fps))
    return (max(1, int(round(rate * 0.25))), max(1, int(round(rate * 0.5))))


def _latency_route_scope(config) -> str:
    """Stable fixed-path identity for reusable measured-latency posteriors.

    Decoder and capture-card pipelines are never interchangeable; ambiguous legacy/window sources
    return an empty scope and cannot restore. The version tag also separates the current measured-
    total domain from older experiments that decomposed an unrelated public-court RTT.
    """
    source = str(getattr(config, 'frame_source', '') or '').strip().lower()
    if source in ('capturecard', 'card'):
        source = 'capture_card'
    # CAPTURE-CARD MODE IS AUTHORITATIVE OVER THE CONFIG TOKEN.
    # `_cc_mode` (see __init__) is `frame_source in (capture_card...) OR
    # ORION_CAPTURE_CARD`, and in that mode the card is the EXCLUSIVE video
    # source -- the decoder pipe is never used. But the native still ships
    # frame_source='decoder' (the decoded-frame pipe is enabled for the
    # PREVIEW), so this function saw 'decoder', walked the decoder branch,
    # failed its identity check and returned '' -- on a rig whose timing feed
    # was a perfectly healthy capture card. Resolve the same way the pipeline
    # does, or the scope describes a source that is not being used.
    if str(os.environ.get('ORION_CAPTURE_CARD', '')).strip().lower() in (
            '1', 'true', 'yes', 'on'):
        source = 'capture_card'
    if source == 'auto':
        # RESOLVE 'auto' THE SAME WAY THE PIPELINE DOES. The config token the
        # native sends defaults to 'auto' (autogreen_sidecar.py: cfg.get(
        # "frame_source", "auto")), while capture-card mode is actually
        # established by ORION_CAPTURE_CARD -- exactly the predicate `_cc_mode`
        # uses at __init__. This function used to compare the UNRESOLVED token
        # against the resolved names, so on the shipped capture-card rig it fell
        # through to the bare `return ''` below and produced an EMPTY scope on a
        # perfectly healthy route.
        #
        # An empty scope is not cosmetic: attest_controller_latency_route
        # refuses on it, the native logs the bare `route_scope_rejected`, and
        # the bot is benched with fire authority it can never earn -- observed
        # 2026-08-18 (161 refusals in 4 minutes) and again live on 2026-08-27
        # ("scope_empty=True equal=True estimator=True", route_invalid=False,
        # i.e. everything else about the route was provably fine.)
        if str(os.environ.get('ORION_CAPTURE_CARD', '')).strip().lower() in (
                '1', 'true', 'yes', 'on'):
            source = 'capture_card'
    if source not in ('capture_card', 'decoder'):
        # Never silent again. Legacy/window/wgc sources are still unscoped BY
        # DESIGN, but an unscoped route benches the bot, so it must say so.
        return _scope_reject('frame_source_unscopeable (%r)' % source[:32])
    console_identity = str(getattr(config, 'console_identity', '') or '').strip().lower()
    console_prefix = 'registered-host-sha256-v1:'
    if (not console_identity.startswith(console_prefix)
            or len(console_identity) != len(console_prefix) + 64
            or any(ch not in '0123456789abcdef'
                   for ch in console_identity[len(console_prefix):])):
        return _scope_reject('console_identity_missing_or_malformed')
    controller_route = str(getattr(config, 'controller_route', '') or '').strip().lower()
    if controller_route not in ('pipe', 'vigem_ds4', 'vigem_xusb'):
        return _scope_reject('controller_route_unproven (%r)' % controller_route)
    parts = [
        # v4 adds an opaque, registration-derived console identity. A DHCP address is not an
        # identity and two paired consoles must never share the same timing posterior.
        # The only verified RTT available here is PC -> public court server; it
        # is not a measurement of either the Chiaki LAN video path or HDMI.
        'orion-latency-route-v4-total-controlled',
        'console=' + console_identity,
        'controller=' + controller_route,
        str(getattr(config, 'platform', '') or '').strip().lower(),
        str(getattr(config, 'client_mode', '') or '').strip().lower(),
        source,
        str(getattr(config, 'resolution', '') or '').strip().lower(),
        str(int(getattr(config, 'target_fps', 0) or 0)),
    ]
    if source == 'capture_card':
        capture_mode = str(getattr(config, 'capture_mode', '') or '').strip().lower()
        if (not capture_mode or len(capture_mode) > 128
                or any(not (ch.isalnum() or ch in '.@x|=_-') for ch in capture_mode)):
            return _scope_reject('capture_mode_unattested (%r)' % capture_mode[:64])
        # Index alone is not a device identity: unplugging/reordering cards can put a different
        # pipeline at index 0. Native supplies the DirectShow names in index order. Restore only
        # when the configured index names exactly one unambiguous capture card and every other
        # listed device is a known webcam; otherwise a backend fallback could silently swap cards.
        raw_names = str(os.environ.get('ORION_VIDEO_DEVICE_NAMES', '') or '')
        names = [name.strip() for name in raw_names.split('|')] if raw_names.strip() else []
        raw_ids = str(os.environ.get('ORION_VIDEO_DEVICE_IDS', '') or '')
        stable_ids = [value.strip().lower() for value in raw_ids.split('|')] \
            if raw_ids.strip() else []
        try:
            index = int(str(os.environ.get('ORION_CAPTURE_CARD_INDEX', '') or '').strip())
        except (TypeError, ValueError, OverflowError):
            return _scope_reject('capture_index_env_missing_or_malformed')
        if (index < 0 or index >= len(names) or not names[index]
                or len(stable_ids) != len(names)
                or any(not value for value in stable_ids)
                or len(set(stable_ids)) != len(stable_ids)):
            return _scope_reject(
                'device_inventory_inconsistent (index=%d names=%d ids=%d)'
                % (index, len(names), len(stable_ids)))
        id_prefix = 'dshow-moniker-sha256-v1:'
        if any(
                not value.startswith(id_prefix)
                or len(value) != len(id_prefix) + 64
                or any(ch not in '0123456789abcdef' for ch in value[len(id_prefix):])
                for value in stable_ids):
            return _scope_reject('device_moniker_ids_malformed')
        try:
            from capture_card_backend import _classify_name
            classes = [_classify_name(name) if name else 'unknown' for name in names]
        except Exception:
            return _scope_reject('name_classifier_unavailable')
        # [ORION_SCOPE_IDENTITY 2026-08-18] The old rule here also demanded that EVERY OTHER
        # device on the system classify as a known webcam. That keyed timing eligibility on the
        # machine's entire device census, and it benched the 2026-08-18 rig for a full session
        # (zero bot releases, route_scope_rejected every 1.5s): Elgato's driver had registered a
        # second DirectShow filter for the same physical card, two entries classified 'card', and
        # the scope emptied -- with the configured Elgato sitting at its configured index,
        # streaming perfectly, its identity fully verifiable. Any new virtual camera with an
        # unrecognized name ('NVIDIA Broadcast' classifies unknown) would do the same.
        #
        # The census never carried the identity anyway. The scope embeds device-id= -- the
        # configured index's DirectShow moniker SHA -- so a swapped card yields a DIFFERENT
        # scope string and the old posterior is unreachable
        # (test_latency_cache_capture_scope_rejects_identically_named_card_swap proves exactly
        # this with no census involved). What the configured device IS remains load-bearing;
        # what else exists on the machine does not.
        if classes[index] != 'card':
            return _scope_reject(
                'configured_index_not_a_capture_card (%r -> %s)'
                % (names[index][:48], classes[index]))
        stable_name = ' '.join(names[index].casefold().split())
        parts.extend([
            str(index),
            'device=' + stable_name,
            'device-id=' + stable_ids[index],
            'mjpg=' + str(os.environ.get('ORION_CAPTURE_MJPG', '1') or '1').strip().lower(),
            'negotiated=' + capture_mode,
        ])
    else:
        identity = _configured_decoder_identity(config)
        decoder_mode = str(getattr(config, 'decoder_mode', '') or '').strip().lower()
        if (identity is None
                or re.fullmatch(r'orf2:[1-9][0-9]{2,4}x[1-9][0-9]{2,4}:(?:nv12|i420)',
                                decoder_mode) is None):
            # The LAST silent return. This one hid the live 2026-08-27 failure:
            # capture-card mode with frame_source='decoder' walked in here and
            # returned '' with nothing logged, so three sessions of hunting saw
            # only the downstream `route_scope_rejected`.
            return _scope_reject(
                'decoder_identity_unproven (identity=%s mode=%r)'
                % ('missing' if identity is None else 'ok', decoder_mode[:32]))
        binary_stamp = f'{identity.size}:sha256={identity.sha256}'
        parts.extend([identity.path, binary_stamp, 'wire=' + decoder_mode])
    return '|'.join(parts)

def get_default_gateway():
    try:
        import subprocess
        import re
        out = subprocess.check_output(['route', 'print'], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).decode()
        match = re.search('0\\.0\\.0\\.0\\s+0\\.0\\.0\\.0\\s+([0-9.]+)', out)
        if match:
            return match.group(1)
    except Exception:
        pass
    return '8.8.8.8'

# detframes.csv schema. The first 13 columns are the original Round 32 set and
# MUST stay byte-identical (existing readers use csv.DictReader, but the column
# names are load-bearing). The appended columns carry the detector's per-frame
# gate diagnostics (meter_detector.MeterDetector.last_debug) so a live batch can
# show exactly WHERE the chain rejects a moving fade meter. wall_ms is epoch
# milliseconds so rows join directly against the UTC log and local OBS video.
#   pts       = decoder presentation timestamp (microseconds, AV_TIME_BASE) of the frame, 0 in
#               screen-capture mode -- joins a detframes row to the exact decoded frame for tip timing.
#   rel_seq   = id of the most recent release COMMAND (monotonic; 0 before any release) so post-release
#               rows group by shot for the frozen-meter latency oracle.
#   mtr_phase = rise | frozen | plateau | none -- separates the shot-meter freeze (the F_stop oracle
#               signal, right after a release) from a later post-shot feedback-UI plateau.
#   q_frame..is_iframe = compressed-stream quality/staleness diagnostics (the offline tuning record
#               for the quality-adaptive reader): per-frame quality q_frame + hysteretic band
#               q_session, fill-edge curvature + fitted sigmoid width (sharpness/blur of the exact
#               edge being timed), NCC peak margin (tracker self-diagnostic), meter-ROI SAD vs the
#               previous frame + the stale verdict (encoder-skip duplicate) + sample validity, the
#               measurement variance R fed to the temporal estimators, and the decoder keyframe
#               flag (ORF2 fork export; 0 until wired). -1 / defaults until each producer lands.
#   coarse_fill_pct..frame_integrity_generation = the exact fill-ruler identity that reached the
#               native phase estimator.  These are deliberately appended: a batch must be able to
#               prove that both samples of an interpolated anchor used one coarse/subpixel
#               generation and one capture-source generation without disturbing legacy readers.
_DETCSV_HEADER = ('t_ms,detected,fill_pct,confidence,x,y,w,h,rejection_reason,'
                  'green_center_pct,green_confidence,frame_w,frame_h,'
                  'wall_ms,fed,stab_streak,stab_tracking,stab_jump_px,acq_gate_px,'
                  'stab_event,zone,roi_miss,cand_n,cand_size_ok,purity_rej,'
                  'med_h,med_s,mem_left,uniqfps,dup_pct,'
                  'anchor_found,anchor_score,anchor_x,anchor_y,rise_state,frame_no,'
                  'pts,rel_seq,mtr_phase,'
                  'q_frame,q_session,edge_curv,sig_width,ncc_margin,roi_sad,stale,valid,'
                  'R_used,is_iframe,'
                  'det_x,det_y,det_w,det_h,'
                  'coarse_fill_pct,fill_estimator_mode,fill_estimator_generation,'
                  'frame_integrity_generation,'
                  'sample_shot_epoch,gameplay_structure_verified,gameplay_structure_epoch,'
                  'raw_fed,reader_stage,processed_seq,source_epoch_ms,source_identity,backend_frozen')
# wall_ms is written %.3f, NOT %.0f: the capture stamp is nanosecond-sourced end to end
# (backend capture_epoch_ns -> _frame_measurement_epoch_ms -> here), and the old integer-ms
# truncation at THIS line was the reason every offline frame-interval census read a median of
# exactly 17.000ms â€” a 1ms-quantized grid defeats any sub-frame/lattice measurement. Column
# count/order unchanged (schema guard: tests/test_detframes_schema.py).
_DETCSV_ROW_FMT = ('%.1f,%d,%.2f,%.3f,%d,%d,%d,%d,%s,%.1f,%.3f,%d,%d,'
                   '%.3f,%d,%d,%d,%.1f,%.1f,%s,%s,%d,%d,%d,%d,%.1f,%.1f,%d,%d,%.1f,'
                   '%d,%.3f,%d,%d,%s,%d,'
                   '%d,%d,%s,'
                   '%.3f,%.3f,%.4f,%.2f,%.3f,%.2f,%d,%d,%.4f,%d,'
                   '%d,%d,%d,%d,'
                   '%.2f,%s,%d,%d,'
                   '%d,%d,%d,%d,%s,%d,%.3f,%d,%d')


@dataclass(frozen=True)
class _CaptureHealthSnapshot:
    """Primitive capture-loop state handed to the diagnostics worker.

    ``backend`` is the sole non-primitive member.  Keeping the exact backend
    alive lets the worker query its thread-safe cadence/export snapshots without
    racing a later backend replacement and, importantly, without doing that work
    on the frame-delivery thread.
    """

    backend: object = field(repr=False, compare=False)
    tier: str
    tier_counts: tuple
    unique_frame_fps: int
    duplicate_frame_pct: float
    cv_fps: float
    cv_detect_ms: float
    source_sequence_skips: int
    preview_duplicate_refreshes: int
    core_black_run: int
    core_static_run: int
    suspect: bool
    # [ORION_CAPTURE_FPS 2026-09-14] The rate the capture card was ASKED for (ORION_CAPTURE_FPS).
    # Carried on the snapshot rather than read off the orchestrator because the emitter is a
    # @staticmethod running on the diagnostics worker â€” it has no `self`, by design, so that
    # formatting never touches the frame-delivery thread's state. Defaulted so every existing
    # construction (and every test that builds one) stays valid.
    requested_fps: int = CAPTURE_FPS_DEFAULT


class _LatestCaptureHealthSink:
    """One-slot, latest-wins worker for periodic capture diagnostics.

    The capture loop may replace a diagnostic that has not begun processing, but
    it never waits for backend statistics, log formatting, handlers, or stdout.
    Shutdown drains the newest pending sample before joining the worker.
    """

    def __init__(self, emit):
        self._emit = emit
        self._condition = threading.Condition()
        self._pending = None
        self._stopping = True
        self._thread = None
        self._replaced = 0

    @property
    def replaced(self):
        return self._replaced

    def is_running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self):
        with self._condition:
            if self.is_running():
                return True
            self._pending = None
            self._stopping = False
            self._replaced = 0
            thread = threading.Thread(
                target=self._run,
                name='capture-health-diagnostics',
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._condition:
                self._thread = None
                self._stopping = True
            raise
        return True

    def submit(self, snapshot):
        # The condition is held only for one pointer swap.  The worker releases it
        # before every backend query and logging call, so capture cannot inherit
        # diagnostic I/O latency.
        with self._condition:
            if self._stopping or not self.is_running():
                return False
            if self._pending is not None:
                self._replaced += 1
            self._pending = snapshot
            self._condition.notify()
        return True

    def stop(self, timeout=2.0):
        thread = self._thread
        if thread is None:
            return True
        with self._condition:
            self._stopping = True
            self._condition.notify()
        thread.join(timeout=max(0.0, float(timeout)))
        stopped = not thread.is_alive()
        if stopped:
            with self._condition:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None and self._stopping:
                    return
                snapshot = self._pending
                self._pending = None
            try:
                self._emit(snapshot)
            except Exception as exc:
                # This is already off the capture path.  Keep the sink alive so a
                # transient backend/statistics failure cannot retire diagnostics.
                logger.warning('Capture-health diagnostics worker error: %s', exc)


class RemotePlayOrchestrator:

    def __init__(self, config):
        self.config = config
        self._running = False
        self._thread = None
        self._capture_thread = None
        self._input_router_thread = None
        # Event-driven meter feed. _processing_loop set()s this the instant a detection
        # publishes fresh meter state; the sidecar telemetry loop (the native engine's ONLY
        # meter feed) waits on it with a 1/60s fallback, so a completed detection is emitted
        # immediately instead of waiting up to ~16.7ms for the next fixed 60Hz tick (and the
        # 60fps emit ceiling is lifted). Read via getattr on the sidecar side -> inert-safe.
        self._frame_ready_evt = threading.Event()
        # Separate capture -> detector handoff. Do not share the completed-frame
        # telemetry event: its consumer clears that event independently.
        self._detector_frame_ready_evt = threading.Event()
        self._video_callback = None
        self._client_lifecycle_lock = threading.RLock()
        self._client_stop_requested = False
        self._client_launch_inflight = False
        self._client_manager = None
        self._input_link_ready = False
        self._input_link_checked_at = 0.0
        self._virtual_controller = None
        self._window_handle = None
        self._meter_detector = None
        self._green_analyzer = None
        # [ORION_BANNER_VERDICT_LIVE] Live reader of the game's own shot-feedback panel
        # (banner_verdict_live.BannerVerdictLive). Created at the top of the processing loop,
        # None whenever the gate is off or tools/timing/panel_grade.py is unavailable.
        # Fed frames by the detect loop (submit) and release edges by release_shot_gate
        # (note_release); a panel with no bot release behind it is logged but NOT forwarded,
        # so a replay screen can never inflate the activity feed or the shot tally.
        self._banner_verdict = None
        self._fill_forecaster = None       # model #3, lazily loaded iff ORION_FILL_FORECAST=1
        self._fill_forecast_log_n = 0      # throttle the predTipMs telemetry line
        # Latest fill-forecast, published for the sidecar telemetry loop to fold into the SAME
        # per-frame JSON payload the native engine consumes (seq-paired predTipMs + a confidence
        # proxy). Stays None unless ORION_FILL_FORECAST=1, so the field is absent + inert when off.
        self._last_fill_forecast = None
        self._fill_kalman = None            # bot-math: const-accel Kalman over fill, iff ORION_FILL_KALMAN=1
        self._fill_kalman_log_n = 0
        self._last_fill_kalman = None       # seq-paired kalmanTipMs/kalmanVel telemetry (inert unless enabled)
        # FAR-HORIZON tip predictor (phase/template registration): loaded iff ORION_TIP_REG=1. Emits a
        # seq-paired reg_tip_ms + reg_conf the native engine can commit off at the ~200ms lead where the
        # Kalman/CNN forecaster fall apart. Stays None (field absent) when off.
        self._tip_reg = None
        self._tip_reg_log_n = 0
        self._last_tip_reg = None
        # A predictor's fill history belongs to one source and one reader ruler.
        # Native phase samples already carry this identity; auxiliary predictors
        # must not blend the old ruler into a freshly re-seated camera lock.
        self._prediction_sample_identity = None
        # POST-HOC full-shot refit label (per archived shot): {'n', 'tip_epoch_ms', 'conf'}.
        # The Phase-1 clock prior EMAs learn from THIS (template extrapolation survives the
        # release-freeze censoring the observed peak), never from the frozen self-grade.
        self._posthoc_seen_n = 0
        self._last_posthoc = None
        # FROZEN-METER ORACLE + BOOT-PROBE: self-measures the real release-path latency live (the engine
        # historically modeled ~13ms vs ~40-100ms real). Enabled by default (telemetry from Python; the
        # native engine chooses whether to consume measured_latency_ms). Fed the fill stream + release
        # markers; emits a rolling robust measured_latency_ms.
        self._latency_estimator = None
        self._latency_route_scope_value = ''
        self._latency_route_lock = threading.Lock()
        # Process-local authority epoch.  ``1`` names the startup estimator/scope;
        # every effective replacement advances it under ``_latency_route_lock``.
        # Native binds its ACK to this value so a same-scope source reset cannot
        # silently reuse proof for the discarded estimator.
        self._latency_estimator_scope_epoch = 1
        self._latency_attestation_command_lock = threading.Lock()
        # Native-issued, process-monotonic neutral-delivery proof.  It is deliberately kept
        # outside the estimator: it is route attestation metadata, never a release/sample/label.
        # The token is echoed only while it is bound to the estimator's exact current scope.
        self._latency_controller_attestation_generation = 0
        self._latency_controller_attestation_scope = ''
        self._latency_controller_attestation_route = ''
        # Never reset these high-water fields during this sidecar process.  They
        # make a native generation an immutable route/scope/estimator binding:
        # an exact retry is idempotent, while lower or rebound generations fail.
        self._latency_controller_highest_attestation_generation = 0
        self._latency_controller_highest_attestation_scope = ''
        self._latency_controller_highest_attestation_route = ''
        self._latency_controller_highest_attestation_epoch = 0
        # A persisted capture-card posterior is scoped by native DirectShow
        # inventory.  OpenCV may nevertheless resolve another index or fall back
        # to MSMF, so warm authority starts unverified and is revoked one-way on
        # the first runtime-route mismatch.  Cold live learning may continue.
        self._capture_warm_cache_expected_index = 0
        self._capture_warm_cache_verified = False
        # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47. These two used to be ONE flag, and
        # conflating them bricked the bot for a whole session.
        #
        # `_capture_warm_cache_revoked` is POISONING: once OpenCV has been observed resolving a
        # route other than the configured one, the PERSISTED posterior -- which is keyed by native
        # DirectShow inventory, not by what OpenCV actually delivered -- can never be trusted again
        # in this process. That is correctly permanent and still gates `restore_cache`.
        #
        # `_capture_route_currently_invalid` is "the route is wrong RIGHT NOW". It must be
        # RETRYABLE, because a transient DSHOW->MSMF fallback is a DOCUMENTED, INTENDED recovery on
        # the shipping HD60X (capture_card_backend.py ~:514 "on some cards (HD60X) DSHOW is the one
        # that wedges... MSMF rides through it"). Treating that blip as permanent meant the stall
        # recovery ARMED the brick: every consumer pinned to generation 0 for the rest of the
        # process, so the C++ brain rejected measured latency and fell back to its ~13 ms model
        # against 40-100 ms of real loop. Only a full restart recovered.
        #
        # Recovery is deliberately COLD, never warm: on return to the exact configured
        # DSHOW/index/mode/geometry we re-key to a real scope with restore_cache=False, so the
        # process re-earns attestation from fresh evidence and never reloads the poisoned cache.
        self._capture_warm_cache_revoked = False
        self._capture_route_currently_invalid = False
        self._capture_latency_active_route = None
        self._capture_latency_route_lock = threading.Lock()
        # One-way for this sidecar process.  Any missing/mismatched decoder
        # producer identity permanently removes cache restore/persistence while
        # retaining safe unscoped cold learning from otherwise valid frames.
        self._decoder_warm_cache_revoked = False
        self._decoder_latency_route_lock = threading.Lock()
        # Capture-card (HDMI) mode: the card is the EXCLUSIVE video source â€” never fall back to the
        # remote-play decoder pipe / window capture (that showed the remote-play video = the user's bug).
        self._xbox_mode = str(getattr(config, 'platform', '')).lower() == 'xbox'
        self._xbox_window = None
        self._cc_mode = (not self._xbox_mode and (str(getattr(config, 'frame_source', '') or '').lower() in ('capture_card', 'capturecard', 'card')
                         or os.environ.get('ORION_CAPTURE_CARD', '').strip().lower() in ('1', 'true', 'yes', 'on'))
                         )
        # Native production builds set this only for the no-capture-card Remote
        # Play source. It is a one-way stricter policy: the decoder pipe must be
        # the detector's eye, and no window/WGC fallback may gain authority.
        self._frame_pipe_required = (
            not self._xbox_mode and not self._cc_mode
            and os.environ.get('ORION_REQUIRE_FRAME_PIPE', '').strip().lower()
            in ('1', 'true', 'yes', 'on'))
        self._cc_last_retry = 0.0          # capture-loop background re-attempt throttle
        self._last_error_msg = ''
        self._last_frame = None
        self._last_frame_ts = 0.0
        # Fail-closed detector input contract.  720p is the minimum retained meter
        # detail proven for this project; 1080p also passes.  No code in this path
        # resizes a bad source to fit -- undersized/non-16:9 frames are rejected.
        try:
            # Environment overrides may tighten the shipping contract, never
            # relax it below the proven 720p detector input.
            self._detector_min_width = max(
                1280, int(float(os.environ.get('ORION_DETECTOR_MIN_WIDTH', '1280') or '1280')))
        except Exception:
            self._detector_min_width = 1280
        try:
            self._detector_min_height = max(
                720, int(float(os.environ.get('ORION_DETECTOR_MIN_HEIGHT', '720') or '720')))
        except Exception:
            self._detector_min_height = 720
        try:
            # A wider tolerance admits visibly distorted/non-16:9 sources.
            # Overrides can tighten only.
            self._detector_aspect_tolerance = max(
                0.001, min(0.035, float(os.environ.get(
                    'ORION_DETECTOR_ASPECT_TOLERANCE', '0.035') or '0.035')))
        except Exception:
            self._detector_aspect_tolerance = 0.035
        try:
            # 50 ms is the production target. Keep a hard 125 ms ceiling for
            # diagnosed slow hardware; env cannot authorise arbitrarily stale video.
            self._max_detector_frame_age_ms = max(
                16.0, min(125.0, float(os.environ.get(
                    'ORION_MAX_DETECTOR_FRAME_AGE_MS', '50') or '50')))
        except Exception:
            self._max_detector_frame_age_ms = 50.0
        self._frame_integrity_counts = {}
        self._last_frame_reject_reason = ''
        # Serializes reject/completion ordering and the immutable telemetry snapshot.
        # A generation advances on every fail-closed reject; a frame may restore health
        # only if no newer rejection occurred after that frame was published.
        self._frame_integrity_lock = threading.Lock()
        self._frame_integrity_generation = 0
        # False from the first suspicious frame until a later VALID frame has
        # completed detection.  Telemetry uses this to fail closed immediately;
        # capture liveness is tracked separately by _last_capture_ts.
        self._capture_integrity_healthy = False
        self._pending_frame_isolated = False
        self._pending_source_frame_number = 0
        self._pending_source_identity = 0
        # Owned exclusively by the detector-processing thread. Generic frame
        # integrity rejects must not discard its learned meter ruler.
        self._detector_processed_source_identity = 0
        self._pending_backend_frozen = False
        # Diagnostic-only capture -> backend-publication delay for the latest raw
        # FrameData. It never enters detector authority, frame age, or release math.
        self._last_capture_publication_age_ms = 0.0
        # Capture-source identity is an attach generation, not id(backend).  Keeping
        # the current object strongly referenced until its replacement is observed
        # also makes Python address reuse irrelevant across a detach/reopen.
        self._backend_identity_ref = None
        self._backend_identity_source_generation = 0
        self._backend_identity_generation = 0
        self._source_identity = 0
        self._source_frame_number = 0
        self._source_timestamp_ns = 0
        # Capture-card cadence attribution.  A fresh-number exact-pixel repeat is
        # intentionally detector-ineligible, but it is still a valid display
        # refresh.  Count both upstream sequence skips and those display-only
        # refreshes so live logs distinguish driver gaps from detector policy.
        self._source_sequence_skips = 0
        self._preview_duplicate_refreshes = 0
        self._pts_source_identity = 0
        self._pts_source_last = 0
        # No arrival-vs-PTS baseline: decoder PTS is a nominal-rate counter, not a
        # clock, so any such baseline drifts.  See _source_pts_reason.  Staleness
        # is _max_detector_frame_age_ms against a real clock in _source_frame_reason.
        self._last_processed_seq = 0
        self._last_processed_frame_number = 0
        self._last_processed_frame_ts = 0.0
        self._last_processed_epoch_ms = 0.0
        self._last_processed_measurement_epoch_ms = 0.0
        self._last_processed_pts = 0
        self._last_processed_frame_wh = (0, 0)
        self._processed_frame_snapshot = _ProcessedFrameSnapshot()
        self._cv_frames_skipped = 0
        self._telemetry_revision = 0
        # Native decoder Y (luma) plane riding with the committed frame (Tier-1 #5): the
        # NV12/I420 pipe backend already exposes a zero-copy Y view on FrameData; passing
        # it to the compressed reader skips the BGR->GRAY round trip that rebuilds the
        # luma the decoder already had. None on capture-card/window paths.
        self._pending_y_plane = None       # y_plane of the LAST backend fd (pre-commit)
        self._last_y_plane = None          # y_plane committed WITH self._last_frame
        # A0 unified timebase: epoch-ms stamp of the last UNIQUE frame (taken at the backend
        # read when a frame backend is active). This is the ONE clock the reader velocity,
        # tip-registration, latency oracle, and the native engine's fusion all share â€” the
        # release markers are already epoch ms, so capture-epoch stamps make the fill
        # timeline and the release timeline directly subtractable.
        self._last_frame_epoch_ms = 0.0
        # Canonical measurement time computed by the capture thread and committed in
        # ``_frame_bundle`` with the exact pixels.  The detector thread must never
        # recompute this from the mutable live PTS mapping.
        self._last_frame_measurement_epoch_ms = 0.0
        # P1 fix: the capture thread publishes (frame, ts, y_plane, pts, epoch_ms,
        # measurement_epoch_ms, seq, source_frame_number, backend_frozen,
        # integrity_generation, source_identity) as ONE
        # atomic tuple; the processing thread reads this single reference so a mid-detect()
        # commit of a newer frame can't skew the staleness clock (was: live _last_frame_ts
        # read AFTER detect() under-reported frame age -> fired early). D5: `seq` rides INSIDE
        # the tuple so the snapshot is self-describing (the loop can only mark done the frame it
        # actually processed); seq 0 is the never-published init value, matching _frame_seq.
        self._frame_bundle = (None, 0.0, None, 0, 0.0, 0.0, 0, 0, True, 0, 0)
        # Reader stage of the last processed frame (track/track_green/acquire/coast/no_meter);
        # shipped in the sidecar payload so the engine can tell a fresh read from a coast.
        self._last_meter_stage = ''
        # Wall-clock of the last SUCCESSFUL capture (unique OR duplicate). Freshness
        # = "is the stream alive", which is NOT the same as "did the scene change".
        # A static HUD between shots produces duplicate frames; basing frame-age on
        # last-unique-frame made age spike on a perfectly live stream and tripped
        # the stale-frame reject. Capture liveness is the correct freshness signal.
        self._last_capture_ts = 0.0
        self._last_pts = 0  # decoder PTS from the latest frame (for frame-locked sync)
        self._pts_to_wall_offset = 0.0  # calibration offset: wall_clock - pts_seconds
        # PTS->EPOCH calibration (decoder path). Distinct from _pts_to_wall_offset, which is in
        # the perf_counter domain: the timing consumers (latency oracle, tip registration, fill
        # forecaster/kalman, detect ts) live on the EPOCH axis the native release markers use.
        # EMA of (frame epoch stamp - pts) keeps the same epoch MEAN but lets samples ride the
        # decoder's PTS clock, removing per-frame pipe/dequeue arrival jitter from the fill
        # timeline. pts=0 sources (capture card / WGC / window) leave both inert.
        self._pts_to_epoch_ms = 0.0
        self._last_frame_pts = 0        # pts snapshotted WITH the last UNIQUE frame
        self._last_frame_hash = None
        # Consecutive all-black captures. A sustained run (e.g. after a launcher
        # tab switch hides/re-surfaces the Chiaki window) forces a window-handle
        # re-find so capture recovers without the user having to reconnect.
        self._black_run = 0
        # Consecutive bad/black/None grabs (any tier), for the capture-health log.
        self._capture_bad_run = 0
        # Optional low-latency decoded-frame backend (frame_source='decoder').
        self._frame_backend = None
        self._frame_backend_mode = 'capture'
        # RC-2c NEVER-RESET monotonic unique-frame counter. This is the native engine's monotonic
        # dedup key: it MUST NOT wrap/reset or fresh detections collide with a prior second's value
        # and get dropped on an fps drop. (It used to reset to 0 every second for a per-second rate
        # that is served separately by _unique_frame_fps.) Emitted as `frame_count`.
        self._frame_count = 0
        self._capture_count = 0
        self._unique_count = 0
        self._frame_seq = 0
        # RC-2b staleness watchdog: if no NEW unique frame arrives for this long, a pure capture stall
        # is in progress. A stall NEVER produces a fresh unique frame, so the committed meter_present:false
        # (which is only asserted on a processed frame) is defeated by exactly the condition it was written
        # for. The CV loop's idle branch synthesizes a no-meter state after this threshold so the native
        # fresh gate stops coasting on the last held fill. ~250ms (env ORION_STALL_WATCHDOG_MS).
        self._stall_watchdog_ms = float(os.environ.get("ORION_STALL_WATCHDOG_MS", "250") or "250")
        self._stall_active = False
        self._fps = 0
        self._capture_fps = 0
        # [ORION_CAPTURE_FPS 2026-09-14] The rate the capture card was ASKED for this launch
        # (ORION_CAPTURE_FPS). Resolved here so the health line can print requested-vs-negotiated
        # even before a backend exists, and so the decoder path reports the same default the
        # backend would have used.
        self._requested_capture_fps = requested_capture_fps()
        self._unique_frame_fps = 0
        self._duplicate_frame_pct = 0.0
        # --- CV-throughput profiling (separates the 38fps cap's cause). The
        # processing loop runs detection on a SEPARATE thread from capture, gated on
        # the capture sequence, so CV cost and capture rate are decoupled. We time
        # _meter_detector.detect() with an EMA so the capture-health log can compare:
        #   cv_fps (= 1000/detect_ms ceiling) vs unique_fps vs the backend export_fps.
        # If cv_fps >> unique_fps the limiter is decode/export (single-thread sw
        # decode -> fork thread_count fix); if cv_fps ~= unique_fps the CV loop is the
        # wall (trim per-frame CV / preview cost). Pure measurement, no behaviour change.
        self._cv_detect_ms_ema = 0.0      # EMA of per-frame detect() wall time (ms)
        self._cv_processed = 0            # frames the processing loop ran detect() on
        self._cv_fps = 0.0               # frames/sec the CV loop actually processed
        self._cv_fps_t0 = time.perf_counter()
        # Whether the LAST processed frame had a clean raw detection worth feeding the
        # engine (vs a meter_memory echo / roi_not_found). Published to the native engine
        # so it can refuse to trust a held/extrapolated stale sample. Default False until
        # a real frame is processed.
        self._last_raw_fed = False
        self._last_gameplay_structure_verified = False
        self._last_gameplay_structure_epoch = 0
        # Per-frame meter tracking published to the native engine (velocity/accel/eta). The sidecar
        # telemetry loop serializes this into the emitted JSON's `tracking` object; native reads
        # velocity_pct_s from it (0 until now because it was never assigned). Populated every processed
        # frame below â€” with the detector's live velocity on a detection, zeroed on a loss.
        self._last_meter_track = None
        # Companion `fusion` object (native prefers it over `tracking`). No separate release-fusion is
        # computed in this build, so it stays None -> the sidecar omits `fusion` and native uses `tracking`.
        self._last_release_fusion = None
        # Top-level `meter_present` for every emitted frame: True when a meter was detected/served this
        # frame, False on loss (native relaxes its emit gate on a false frame and resets its HOLD clock).
        self._last_meter_present = False
        # Phase-1: feed freeze flag from the capture card backend (FrameData.feed_frozen).
        # Published to the sidecar telemetry as feed_healthy so the native engine can
        # suppress blind fires on a dead feed. False when the feed is live.
        self._last_feed_frozen = False
        # --- Closed-loop timing feedback: RETIRED the in-game TIMING text banner reader.
        # The banner is delayed ~1.2s, lingers ~1.5s, is drawn for every player, and is read
        # by fragile template matching -- so in rapid play the WRONG verdict gets pinned to a
        # shot and the bot detunes itself (recording-proven: a real EXCELLENT was tagged LATE
        # off the previous shot's lingering banner). The native engine now calibrates from the
        # bot's OWN post-release shot meter (AutomationEngine::evaluatePostReleaseMeter), read
        # off the per-frame detection telemetry it already receives -- only the user's meter is
        # ever drawn (no opponent confusion), it lingers after release, and being static it
        # carries no view-lag. So the orchestrator no longer reads or publishes a HUD verdict.
        # --- Capture-health diagnostics (which tier produced each frame + whether the
        # VIDEO CORE is live, separate from the whole-frame signals). These explain a
        # live roi_not_found dropout: the whole frame can keep changing (uniqfps high)
        # while the captured video core is black/stale, so the detector sees a
        # meterless frame the on-screen video (and an OBS recording) clearly has. Pure
        # diagnostics â€” they do NOT change capture behavior.
        self._last_capture_tier = ''
        self._tier_counts = {}
        self._video_core_hash = None
        self._video_core_black_run = 0     # consecutive good frames with a black core
        self._video_core_static_run = 0    # whole-frame changed but core did NOT
        self._last_capture_health_log = 0.0
        self._capture_health_suspect = False
        # cadence_stats(), export_stats(), formatting, and logger handlers used to
        # execute synchronously here every ~5 seconds.  Live evidence showed the
        # resulting callback gap recurring at ~32 ms even while raw capture stayed
        # healthy.  A one-slot worker preserves the diagnostic while making it
        # impossible for an old report to queue behind newer frame state.
        self._capture_health_diagnostics = _LatestCaptureHealthSink(
            self._emit_capture_health_diagnostic)
        # Opt-in per-frame detection diagnostic (ORION_DETDIAG=1). Off by default so
        # normal runs stay quiet; when on, throttled WARNING lines surface real
        # detected/fill/conf/green/rejection so detection vs delivery is distinguishable.
        self._detdiag_enabled = os.environ.get('ORION_DETDIAG', '').strip() in ('1', 'true', 'True', 'yes')
        self._last_detect_log_ts = 0.0
        # Throttle interval (seconds) between DETDIAG lines. DETDIAG is opt-in (off by
        # default), and when it's ON you want to actually SEE detection â€” 1/s is too
        # sparse to segment shots or compute a within-shot fed-rate â€” so the default is
        # dense (~10/s). Override with ORION_DETDIAG_INTERVAL (e.g. 1.0 for a quiet
        # long run, 0.04 for ~25/s). The detector still runs every frame; this only
        # samples WHICH results get logged, so the fed-RATE estimate stays unbiased.
        try:
            self._detdiag_interval = max(0.0, float(os.environ.get('ORION_DETDIAG_INTERVAL', '0.1')))
        except (TypeError, ValueError):
            self._detdiag_interval = 0.1
        # One-shot build marker so we can PROVE which code the live sidecar loaded
        # (stale-bytecode check): logs the resolved DETDIAG interval + whether the
        # meter_detector tracking-latch fix is present. RETIREMENT GUARD: the latch
        # probe only imports meter_detector when the LEGACY chain is actually
        # selected â€” with the simple/compressed reader the retired chain is never
        # imported by the orchestrator at all.
        if (os.environ.get('ORION_SIMPLE_READER', '1').strip().lower() in ('1', 'true', 'yes', 'on')
                or os.environ.get('ORION_COMPRESSED_READER', '0').strip().lower() in ('1', 'true', 'yes', 'on')):
            _latch = "n/a_legacy_chain_retired"
        else:
            try:
                import meter_detector as _md
                _latch = hasattr(_md._StabilityValidator, "note_frame_width")
            except Exception:
                _latch = "import_failed"
        logger.warning("ORION_BUILD_MARKER detdiag_interval=%.3f stability_tracking_latch=%s",
                       self._detdiag_interval, _latch)
        # Capture, tracking and preview already have dedicated workers. OpenCV's
        # per-core default pool (16 on the development rig) oversubscribes those
        # workers for small-ROI operations. Use one thread unless explicitly
        # overridden; change only parallelism, never pixels or frame cadence.
        # Configure before initialization starts detector/capture/preview work.
        _configure_opencv_threads()
        # Full-rate detection CSV remains opt-in. The writer owns all filesystem
        # operations; capture only formats a timestamped row and offers it to a
        # bounded FIFO. Queue/disk/logger delays never become detector-frame delays.
        self._detcsv = None
        self._detcsv_t0 = 0.0
        self._detcsv_enabled = os.environ.get('ORION_DETCSV', '0').strip() in ('1', 'true', 'True', 'yes', 'on')
        self._start_detcsv()
        # Opt-in frame dump (ORION_FRAMEDUMP=1): saves exactly what the SIDECAR
        # detector sees on the live stream â€” both the raw captured frame and an
        # annotated overlay (bbox + fill line + green band) â€” so live detection can
        # be inspected without a console. Throttled + capped so it can't fill disk.
        self._framedump_enabled = os.environ.get('ORION_FRAMEDUMP', '').strip() in ('1', 'true', 'True', 'yes')
        # ORION_FRAMEDUMP_ROOT is normally consumed by the LAUNCHER, which composes
        # ORION_FRAMEDUMP_DIR = <root>\session_<stamp>.  Honour it here too so a sidecar
        # started without the launcher still lands on the operator's chosen volume
        # instead of the repo default on C: (7 GB free on this workstation).
        self._framedump_dir = (os.environ.get('ORION_FRAMEDUMP_DIR', '').strip()
                               or os.environ.get('ORION_FRAMEDUMP_ROOT', '').strip()
                               or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               'logs', 'diagnostics', 'framedump'))
        self._init_framedump_press_window()
        try:
            self._framedump_interval = max(0.0, float(os.environ.get('ORION_FRAMEDUMP_INTERVAL', '0.2')))
        except ValueError:
            self._framedump_interval = 0.2
        # PRESS-WINDOW mode writes only ~60 frames per shot but writes ALL of them, so the
        # 900-frame default (15 shots) is the wrong cap for a session; 20000 is ~330 shots.
        # [ORION_FRAMEDUMP_PRESS_BANNER 2026-09-17] With the banner leg on, a shot is
        # ~140 (Standstill) to ~230 (Go-To) frames, so the same 20000 is ~110-140 shots.
        # It is left where it is on purpose: the cap is a SAFETY stop, and the real limit
        # on a long session is the disk floor, not the frame count.  Halve the bytes with
        # ORION_FRAMEDUMP_RAW_ONLY=1 (the offline studies read only *_raw).
        _fd_max_default = '20000' if self._framedump_press_window else '900'
        try:
            self._framedump_max = max(1, int(os.environ.get('ORION_FRAMEDUMP_MAX', _fd_max_default)))
        except ValueError:
            self._framedump_max = int(_fd_max_default)
        self._framedump_count = 0
        # Set once the dump has seen its first gameplay-eligible frame (see _dump_frame).
        # A PRESS is gameplay by definition, so press-window mode needs no gate at all.
        self._framedump_armed = (self._framedump_press_window
                                 or os.environ.get('ORION_FRAMEDUMP_GATE', '1').strip()
                                 in ('0', 'false', 'False', 'no'))
        self._framedump_last_ts = 0.0
        # Raw-only skips the annotated overlay PNG (the offline rise-report only reads *_raw.png),
        # halving the per-frame write cost.
        self._framedump_raw_only = os.environ.get('ORION_FRAMEDUMP_RAW_ONLY', '').strip() in ('1', 'true', 'True', 'yes')
        self._framedump_q = None
        self._framedump_writer = None
        self._framedump_writer_stop = threading.Event()
        self._framedump_dropped = 0
        self._framedump_reported_dropped = 0
        self._framedump_disabled_reason = ''
        # A deep queue does not make diagnostics safer: it retains hundreds of MB of full-resolution
        # frames and lets the PNG worker keep competing with capture long after the producer falls
        # behind.  One pending frame plus the frame currently being encoded is the complete backlog.
        # The producer drops before copying when this slot is occupied.
        #
        # PRESS-WINDOW mode is the exception the rule was not written for.  It writes a
        # BOUNDED BURST (~60 frames, ~1 s) and then nothing at all until the next press, so
        # a deep queue here does not retain frames "long after the producer falls behind" --
        # it is the only thing that lets a 60 fps burst survive a writer hiccup.  The burst
        # is JPEG (4.3 ms full frame / 0.6 ms crop, measured) so the queue drains faster
        # than it fills and a 96-slot ring is ~24 MiB of 720p references at worst.
        # (resolved in _init_framedump_press_window, which owns the mode-dependent default)
        # Runtime disk guard.  Absolute AND percentage floors matter: 5 GiB is critical on this
        # workstation's 1 TiB system volume, while 2% alone is too small on a compact capture drive.
        # Invalid/unsafe overrides fall back to conservative values rather than disabling the guard.
        try:
            _min_free_gb = max(1.0, float(os.environ.get('ORION_FRAMEDUMP_MIN_FREE_GB', '10')))
        except ValueError:
            _min_free_gb = 10.0
        try:
            self._framedump_min_free_pct = max(
                0.5, min(25.0, float(os.environ.get('ORION_FRAMEDUMP_MIN_FREE_PCT', '2'))))
        except ValueError:
            self._framedump_min_free_pct = 2.0
        self._framedump_min_free_bytes = int(_min_free_gb * (1024 ** 3))
        # PNG encoding is off-thread but still shares CPU and storage bandwidth with capture.  Bound
        # its average duty cycle; callers that need denser research data may opt upward, but never
        # beyond 50% through this diagnostic path.
        try:
            _duty_pct = max(
                5.0, min(50.0, float(os.environ.get('ORION_FRAMEDUMP_MAX_DUTY_PCT', '25'))))
        except ValueError:
            _duty_pct = 25.0
        self._framedump_max_duty = _duty_pct / 100.0
        try:
            self._framedump_max_write_s = max(
                0.05, min(2.0, float(os.environ.get('ORION_FRAMEDUMP_MAX_WRITE_MS', '250')) / 1000.0))
        except ValueError:
            self._framedump_max_write_s = 0.250
        # [2026-09-14] SLOW WRITE == BACK OFF, NOT DEATH.  A single PNG write over
        # _framedump_max_write_s used to call _framedump_disable('writer_stall'), which is
        # PERMANENT for the process generation.  That is what ended session_20260914_204600 after
        # 2096 frames / 6 minutes of a 67-minute batch: the writer had been running at ~35 ms per
        # frame all session and one write took ~350 ms, so the dump died an hour before the aborts
        # it was launched to capture.  (The same guard killed session_20260914_135124 after ONE
        # frame on D:.)  The pressure the guard exists to remove is real, so keep it -- but express
        # it as a cooldown: skip frames while writes are slow, resume automatically when they are
        # fast again, and reserve permanent disabling for disk-full / directory faults.
        try:
            self._framedump_backoff_max_s = max(
                0.5, min(120.0, float(os.environ.get('ORION_FRAMEDUMP_BACKOFF_MAX_S', '15'))))
        except ValueError:
            self._framedump_backoff_max_s = 15.0
        try:
            self._framedump_heartbeat_s = max(
                0.0, min(600.0, float(os.environ.get('ORION_FRAMEDUMP_HEARTBEAT_S', '60'))))
        except ValueError:
            self._framedump_heartbeat_s = 60.0
        # Consecutive hard write EXCEPTIONS (not slow writes) before the dump is given up on.
        try:
            self._framedump_max_error_streak = max(
                1, min(100, int(os.environ.get('ORION_FRAMEDUMP_MAX_ERRORS', '5'))))
        except ValueError:
            self._framedump_max_error_streak = 5
        # The env opt-in is separate from the LIVE flag: the live flag is cleared by every stop()
        # and by the back-off/disable paths, and _start_framedump() consults the opt-in to decide
        # whether a fresh capture generation gets a writer back.
        self._framedump_env_enabled = bool(self._framedump_enabled)
        self._framedump_permanent_stop = ''
        self._framedump_skipped = 0
        self._framedump_backoff_until = 0.0
        self._framedump_backoff_s = 0.0
        self._framedump_slow_streak = 0
        self._framedump_error_streak = 0
        self._framedump_last_write_ms = 0.0
        self._framedump_last_write_ts = 0.0
        self._framedump_heartbeat_ts = 0.0
        self._framedump_generation = 0
        self._framedump_enabled = False
        self._start_framedump()
        # [ORION_SHOT_RECORDS 2026-09-16] "Labels for free": one JSON line per shot the
        # owner takes, joining the press, the reader's first sight, the release marker,
        # the RELEASE ORACLE and the game's own banner verdict on the shot-gate physical
        # epoch.  Created here (not in the processing loop) so a capture restart does not
        # start a second corpus file mid-session.  None when ORION_SHOT_RECORDS=0 or the
        # module/output is unavailable; every call site is guarded.
        self._shot_records = None
        # Fallback meter-onset clock for the record: the FIRST frame of this press on
        # which the reader reported a detection.  Written by the detect loop from fields
        # that frame already computed (see _processing_loop); read on the control thread.
        self._shot_record_onset_epoch = 0
        self._shot_record_onset_done = False
        self._shot_record_icon_next = 0.0
        try:
            from shot_records import ShotRecorder as _ShotRecorder
            # [ORION_FRAMEDUMP_SESSION_DIR 2026-09-17] One label for both outputs: the
            # record file and the press-window frame folder carry the same session stamp, so
            # a JSONL row and its frames join by (session, epoch) instead of by epoch alone
            # -- which was ambiguous the moment two sessions shared one dump root.
            self._shot_records = _ShotRecorder.create(
                log=logger, session=str(getattr(self, '_framedump_session', '') or ''))
        except Exception as _sr_exc:
            logger.warning('shot records unavailable: %s', _sr_exc)
        # The nameplate "3"-cell sampler is OPT-IN: it is the only thing in this batch that
        # reads pixels the detect thread had no reason to read (one 22x22 ROI mean at
        # 10 Hz, 0.026 ms measured).  tools/diagnostics/hud_3pt_icon.py's own detector is
        # an 8-scale template sweep at 157.7 ms/frame and can never run live.
        self._shot_record_icon = str(
            os.environ.get('ORION_SHOT_RECORD_ICON', '0') or '0').strip().lower() in (
                '1', 'true', 'yes', 'on')
        # [ORION_SHOT_RANGE 2026-09-17] THREE or MID for the live press, read off the same
        # nameplate "3" cell 40-120 ms after the Square edge and sent to the engine on the
        # existing stdout JSONL channel so the fade trim can key on RANGE as well as on
        # (type, tempo).  The detect thread only appends a frame REFERENCE; every pixel
        # read, the vote and the emit happen on the reader's own worker.  See shot_range.py
        # for the calibration status -- the verdict ships as `unknown` until it is armed.
        self._shot_range = None
        try:
            from shot_range import ShotRangeReader as _ShotRangeReader, anchor_plate
            self._shot_range = _ShotRangeReader.create(
                emit=emit_stdout_jsonl, plate_fn=anchor_plate,
                on_result=self._shot_range_result, log=logger)
        except Exception as _srg_exc:
            logger.warning('shot range unavailable: %s', _srg_exc)
        self._last_fps_time = time.perf_counter()
        self._last_meter_bbox = None
        # [ORION_PROOF_DETECTOR_BOX 2026-09-19] the same frame's DETECTOR rectangle (pre display hug)
        self._last_meter_det_bbox = None
        self._last_meter_bbox_wh = None   # (w,h) of the frame the bbox was computed in (overlay-map scale)
        self._last_green_window = None
        # Release-window diagnostic bridge: the reader owns the immutable native release id and
        # this layer only transports it.  The record compares command position to detector pixels;
        # it is not a gameplay outcome and must never become timing-learning authority.
        self._green_grade_pending = None
        self._green_grade_seq = 0
        self._green_grade_lock = threading.Lock()
        # Track-fill overlay hysteresis: a green window must be seen for a few
        # consecutive frames before it is reported (rejects 1-frame false
        # positives), and must be absent for several frames before it is cleared
        # (rejects brief detection dropouts). This stops the overlay flickering
        # on/off when there is no stable meter on screen.
        self._green_show_streak = 0
        self._green_hide_streak = 0
        self._green_show_required = 2
        self._green_hide_required = 12
        self._goto_shot_active = False
        self._goto_shot_start_time = 0.0
        self._goto_shot_triggered = False
        self._release_time = None
        self._prev_square_pressed = False
        self._prev_rs_up = False
        self._prev_rs_down = False
        # Shot-gate arming for a shot-gated reader (SimpleMeterReader). A shot-start signal
        # (native pose_arm / SQUARE / stick edge) arms the reader for ORION_SHOT_GATE_ARM_FRAMES so
        # its early-rise acquire gate is relaxed during the shot (recovers the first 3-7 rise frames)
        # and its coast is extended for fast-fade blur. Auto-expires (bounded) and re-arms on each
        # shot-start; a fresh detected meter also refreshes it so a live shot stays armed. Inert for
        # the serving chain (MeterDetector has no set_shot_state).
        self._shot_gate_lock = threading.RLock()
        self._shot_gate_deadline_seq = -1
        self._shot_gate_deadline_monotonic = -1.0
        # SEPARATE hardware-arm deadline (plan B1 [fix]): set ONLY by _arm_shot_gate (physical
        # shot-begin signals: native pose_arm / SQUARE / stick edges). The CV self-arm refreshes
        # the MERGED deadline above but must NEVER touch this one -- the ColorCalibrator trains
        # only inside hw-armed windows, so a CV false lock can never teach itself its colours.
        self._shot_gate_hw_deadline_seq = -1
        self._shot_gate_hw_deadline_monotonic = -1.0
        self._shot_gate_source = ''          # 'pose'|'square'|'stick_up'|'stick_down'|'cv'
        self._shot_gate_epoch = 0             # controller-origin uint64; zero = untrusted/local arm
        self._shot_gate_armed_prev = False   # for arm/disarm transition logging (confirm live)
        # A native first-edge wake precedes beginShot's tokenized pose_arm for the
        # SAME physical shot. Consume this flag on pose_arm so the reader gets one
        # new-shot reset, not a destructive second reset ~190ms into the rise.
        self._shot_gate_edge_pending = False
        self._last_cal_status_version = -1   # calibrate_meter_status IPC de-dupe
        try:
            # Dual bound: 2400 frames covers a 20s Go-To even if a source really
            # delivers 120fps; monotonic time is the authoritative cap when invalid
            # loading/dark frames intentionally stop frame_seq from advancing.
            self._shot_gate_arm_frames = max(1, int(os.environ.get('ORION_SHOT_GATE_ARM_FRAMES', '2400')))
        except Exception:
            self._shot_gate_arm_frames = 2400
        try:
            self._shot_gate_max_seconds = min(30.0, max(
                2.0, float(os.environ.get('ORION_SHOT_GATE_MAX_SECONDS', '20.0'))))
        except Exception:
            self._shot_gate_max_seconds = 20.0
        try:
            self._shot_gate_post_release_seconds = min(5.0, max(
                1.0, float(os.environ.get('ORION_SHOT_GATE_POST_RELEASE_SECONDS', '3.0'))))
        except Exception:
            self._shot_gate_post_release_seconds = 3.0
        remap_cfg = RemapConfig(
            enabled=True,
            release_enabled=False,
            input_mode=config.input_mode,
            confidence_gate=config.confidence_gate,
            goto_enabled=config.goto_shot,
            early_late_offset_ms=config.timing_delay_ms,
            stable_frames_required=max(1, int(config.stable_frames_required)),
            tempo_flick_hold_ms=float(config.tempo_flick_hold_ms),
            goto_flick_hold_ms=float(config.tempo_flick_hold_ms),
            gpc_flick_chain_ms=max(0.0, float(config.latency_compensation_ms)),
            meter_style=str(getattr(config, 'meter_style', '') or ''),
            shot_trigger_mode=config.shot_trigger_mode,
            active_shot_type=str(getattr(config, 'active_shot_type', 'Standstill') or 'Standstill'),
        )
        _sto = getattr(config, 'shot_type_offsets', None)
        if isinstance(_sto, dict) and _sto:
            merged = dict(remap_cfg.shot_type_offsets)
            for _name, _val in _sto.items():
                try:
                    merged[str(_name)] = max(-80.0, min(80.0, float(_val)))
                except (TypeError, ValueError):
                    continue
            remap_cfg.shot_type_offsets = merged
        self._remap_engine = RemapEngine(remap_cfg)
        logger.info("Python release path disabled (release_enabled=False) - C++ AutomationEngine is sole release authority")
        # Load the RTT/court-sync config from settings.json so the UDP court-port
        # window (network_udp_port_min/max) and the rtt_sync.* tunables actually
        # take effect. Previously a bare RTTSyncConfig() was used, which silently
        # ignored every network_* / rtt_sync setting and pinned the defaults.
        try:
            from rtt_sync_engine import load_rtt_sync_config
            rtt_cfg = load_rtt_sync_config()
            rtt_cfg.ping_enabled = True
        except Exception as _rtt_cfg_exc:
            logger.warning('RTT sync config load failed (%s); using defaults', _rtt_cfg_exc)
            rtt_cfg = RTTSyncConfig(ping_enabled=True)
        self._rtt_engine = RTTSyncEngine(rtt_cfg)
        self._anim_anchor = None      # shadow-only animation anchor (env ORION_ANIM_ANCHOR_SHADOW)
        self._anim_miss = 0
        if CV_AVAILABLE:
            try:
                # Lazy: DetectorConfig is a plain dataclass (no torch at module scope).
                # Imported here instead of module scope so the retired legacy chain's
                # module is only touched where the config contract actually needs it.
                from meter_detector import DetectorConfig
                det_config = DetectorConfig()
                det_config.meter_color = config.meter_color
                det_config.confidence_threshold = config.confidence_gate
                # PARK temporal locator (distractor rejection for the rec-mode park, where
                # fire/jerseys/HUD are bright-red and false-lock the colour scan). Enabled on the
                # LIVE detector here (not via settings.json, so it never rides along with the
                # unit-test/load_detector_config path). ORION_PARK_TEMPORAL=0 disables (no rebuild).
                det_config.park_temporal_enabled = (
                    os.environ.get('ORION_PARK_TEMPORAL', '1').strip().lower()
                    not in ('0', 'false', 'no', 'off')
                )
                # Which CV reader will actually be built from this config?  Resolved HERE,
                # ABOVE the park colour pin, because the pin below must not apply to the
                # simple/compressed readers (see the pin's own comment).  The full rationale
                # for each flag is at the construction site further down.
                _simple_on = os.environ.get('ORION_SIMPLE_READER', '1').strip().lower() \
                    in ('1', 'true', 'yes', 'on')
                _compressed_on = os.environ.get('ORION_COMPRESSED_READER', '0').strip().lower() \
                    in ('1', 'true', 'yes', 'on')
                # Park IS the RED rec-mode meter. PIN meter_color=Red (+ disable auto-colour) when
                # park is on, so park activation and the constrained colour read NEVER depend on a
                # mislabeled/auto-thrashed meter_color â€” the historical misconfig that silently ran
                # the wrong-colour mask and false-locked on park reds (the "old buggy detection").
                #
                # SCOPE (2026-08-04): the pin belongs to the LEGACY serving chain's park path and
                # ONLY to it.  `park_temporal_enabled` defaults ON, so an unscoped pin overwrote
                # meter_color on EVERY launch â€” including for the SimpleMeterReader built from
                # this same det_config below, which has no park path at all.  That made the UI
                # colour picker completely inert: a user selecting Purple got a Red mask, no
                # detections, and no explanation.  CompressedMeterReader must be excluded too:
                # it SUBCLASSES SimpleMeterReader and is built from this same config, so guarding
                # on `_simple_on` alone would leave the Remote Play path pinned to Red.
                if det_config.park_temporal_enabled and not (_simple_on or _compressed_on):
                    if str(det_config.meter_color) != "Red":
                        logger.warning('park: overriding meter_color %s -> Red (park is the red meter)', det_config.meter_color)
                    det_config.meter_color = "Red"
                    det_config.auto_meter_color = False
                # FIX: MeterDetector(styles_dir, cfg). Previously the config was
                # passed positionally as styles_dir, so NO style JSONs loaded
                # (colors + per-meter contours were ignored) and the configured
                # meter_color was discarded in favour of load_detector_config().
                # That left detection unable to pick up the meter at all.
                styles_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "meter_styles"
                )
                # ORION_SIMPLE_READER (default ON): flag-select the shot-gated SimpleMeterReader
                # INSTEAD of the ~5,700-LOC serving chain. It emits the SAME DetectResult contract
                # (fill/velocity/bbox/confidence/rejection_reason/rise_state) so the whole downstream
                # (feed gate, sidecar telemetry: meter_present / pixel_age_ms / heartbeat / stalled,
                # native, timing stack) consumes it UNCHANGED. A head-to-head proved it matches the
                # chain on accuracy at ~1% of the code with ZERO decor false-locks
                # (logs/diagnostics/simple_vs_chain/FINAL_*.txt).
                # DEFAULT FLIPPED to '1': this IS the shipped path â€” the native launcher injects
                # ORION_SIMPLE_READER=1 on every real launch, so the old '0' default only ever
                # applied to entry points that DON'T inject it (tools, harnesses, a bare sidecar
                # run), which then fell through to the retired chain and errored. Opt OUT with
                # ORION_SIMPLE_READER=0.
                # ORION_COMPRESSED_READER=1: the Chiaki/Remote-Play variant of the simple reader
                # (chroma-first + K2 luma fill fallback + encoder-skip stale gate). Same
                # DetectResult contract; collapses to SimpleMeterReader behavior on pristine
                # sources by construction. Implies the simple reader (it subclasses it).
                # Both flags are resolved ABOVE the park colour pin (see there); this is the
                # single source of truth for them -- do not recompute either one here.
                if _compressed_on:
                    from compressed_meter_reader import CompressedMeterReader
                    self._meter_detector = CompressedMeterReader(
                        cfg=det_config, require_gameplay_eligibility=True)
                    logger.warning(
                        'CV detector = CompressedMeterReader (ORION_COMPRESSED_READER=1) '
                        'meter_color=%s style=%s -- luma-fallback reader for the compressed stream',
                        det_config.meter_color, config.meter_style,
                    )
                    # Bug #1 (docs/COMPRESSED_PATH_DESIGN.md): seed the reader's quality
                    # controller with the CONFIGURED Chiaki rung. Without this the
                    # QualityEstimator starts at prior=1.0 (pristine) on a 4 Mbps stream
                    # and q_session gates the luma branches OFF exactly when needed.
                    try:
                        from chiaki_backend import load_chiaki_config as _load_ck
                        _ck = _load_ck()
                        _rw, _rh = {'720p': (1280, 720), '1080p': (1920, 1080)}.get(
                            str(_ck.resolution).strip().lower(),
                            (int(_ck.frame_width), int(_ck.frame_height)))
                        self._meter_detector.set_stream_profile(
                            _rw, _rh, float(_ck.fps), float(_ck.bitrate_kbps))
                        logger.warning(
                            'CompressedMeterReader stream profile: %dx%d@%d %dkbps -> q0=%.3f',
                            _rw, _rh, int(_ck.fps), int(_ck.bitrate_kbps),
                            float(self._meter_detector._qe.q_session))
                    except Exception as _sp_exc:
                        logger.warning('set_stream_profile skipped (%s); quality prior '
                                       'stays pristine until pixel evidence lands', _sp_exc)
                elif _simple_on:
                    from simple_meter_reader import SimpleMeterReader
                    self._meter_detector = SimpleMeterReader(
                        cfg=det_config, require_gameplay_eligibility=True)
                    # Style is applied via update_meter -> set_active_style, exactly like the chain.
                    logger.warning(
                        'CV detector = SimpleMeterReader (ORION_SIMPLE_READER=1) meter_color=%s style=%s'
                        ' -- shot-gated simple reader IN PLACE OF the serving chain (A/B)',
                        det_config.meter_color, config.meter_style,
                    )
                else:
                    # RETIRED: the legacy torch/YOLO serving chain (~5,700 LOC
                    # locator->loc_mem->park->T5 stack). The shipped path is
                    # ORION_SIMPLE_READER=1 (or ORION_COMPRESSED_READER=1). This
                    # branch survives only for an explicit dev A/B; if the legacy
                    # modules are ever removed it fails LOUDLY instead of silently
                    # running without a detector.
                    try:
                        from meter_detector import MeterDetector as _LegacyMeterDetector
                    except ImportError as _legacy_exc:
                        raise RuntimeError(
                            'Legacy meter-detector chain is RETIRED and meter_detector is '
                            'unavailable (%s). Set ORION_SIMPLE_READER=1 (shipped pure-CV '
                            'reader) or ORION_COMPRESSED_READER=1 (Chiaki/Remote-Play '
                            'variant) instead of the legacy chain.' % (_legacy_exc,)
                        ) from _legacy_exc
                    self._meter_detector = _LegacyMeterDetector(styles_dir, det_config)
                    logger.warning(
                        'CV detector = LEGACY MeterDetector chain (RETIRED; dev A/B only). '
                        'The shipped path is ORION_SIMPLE_READER=1.')
                # Detector-stack fields APPENDED to the one built-line (a separate WARNING 1ms
                # later was eaten by the native relay's WARNING throttle, 2026-07-04): everything
                # below announces itself at INFO, which the relay drops â€” batch forensics could
                # not even tell whether the v6 locator had loaded. key=value, no spaces in values.
                try:
                    _loc = getattr(self._meter_detector, '_locator', None)
                    _stack = (
                        'locator=%d locator_device=%s locator_imgsz=%s locator_conf=%s '
                        'locator_every=%s locator_model=%s box_track=%d reader=%d' % (
                            1 if (_loc is not None and getattr(_loc, 'enabled', False)) else 0,
                            str(getattr(_loc, '_device', '-')).replace(' ', ''),
                            getattr(_loc, '_imgsz', '-'), getattr(_loc, '_conf', '-'),
                            getattr(_loc, '_every', '-'),
                            (os.path.basename(os.environ.get('ORION_METER_LOCATOR_MODEL', '').strip())
                             or 'default') if _loc is not None else '-',
                            1 if getattr(self._meter_detector, '_box_track', None) is not None else 0,
                            1 if getattr(self._meter_detector, '_reader', None) is not None else 0,
                        ))
                except Exception:
                    _stack = 'stack=unavailable'
                logger.warning(
                    'CV detector built: park_temporal=%s meter_color=%s style=%s %s',
                    det_config.park_temporal_enabled, det_config.meter_color, config.meter_style,
                    _stack,
                )
                cv_config = RemotePlayCVConfig(green_window_start_pct=config.green_window_start_pct, green_window_end_pct=config.green_window_end_pct)
                self._green_analyzer = GreenWindowAnalyzer(cv_config)
                logger.info('CV pipeline initialized')
                # Fill-trajectory forecaster (model #3): flag-gated (ORION_FILL_FORECAST=1). When the flag is
                # off, try_load() returns None and torch is never imported. When on, predicts ms-to-tip from
                # the running fill curve; emitted as telemetry (predTipMs) for live validation vs the actual
                # tip BEFORE the native engine trusts it. Default off = zero behaviour change.
                try:
                    from fill_forecaster_infer import try_load as _load_fill_forecaster
                    self._fill_forecaster = _load_fill_forecaster()
                    if self._fill_forecaster is not None:
                        logger.warning('Fill forecaster ENABLED (bias=%+.1fms) -> predTipMs telemetry',
                                       self._fill_forecaster.bias_frames * (1000.0 / 60.0))
                except Exception as _ffe:
                    self._fill_forecaster = None
                    logger.error(f'fill forecaster init failed: {_ffe}')
                # Kalman fill estimator (bot-math precision): const-accel Kalman over the fill signal ->
                # ~11x smoother velocity + an analytical sub-frame ms-to-target. Flag-gated
                # (ORION_FILL_KALMAN=1), TELEMETRY-ONLY alongside the forecaster so kalmanTipMs can be graded
                # vs predTipMs live before either drives release. Off = never imported.
                try:
                    from fill_kalman import try_load as _load_fill_kalman
                    self._fill_kalman = _load_fill_kalman()
                    if self._fill_kalman is not None:
                        logger.warning('Fill Kalman ENABLED -> kalmanTipMs/kalmanVel telemetry')
                except Exception as _fke:
                    self._fill_kalman = None
                    logger.error(f'fill kalman init failed: {_fke}')
                # Far-horizon tip-registration predictor (flag-gated ORION_TIP_REG=1). Loads the
                # OFFLINE-learned template + priors (models/tip_registration.json); never refits live.
                try:
                    from tip_registration_infer import try_load as _load_tip_reg
                    self._tip_reg = _load_tip_reg()
                    if self._tip_reg is not None:
                        logger.warning('Tip registration ENABLED -> regTipMs/regConf telemetry')
                        # B2: wire the fit into a robust reader (trajectory gate + dead-reckoned
                        # coast read the last online fit through predict_fill). Guarded â€” the
                        # serving chain has no set_fit_provider; absent hook = features inert.
                        _sfp = getattr(self._meter_detector, 'set_fit_provider', None)
                        if callable(_sfp) and hasattr(self._tip_reg, 'predict_fill'):
                            _sfp(self._tip_reg.predict_fill)
                except Exception as _tre:
                    self._tip_reg = None
                    logger.error(f'tip registration init failed: {_tre}')
                # Detector-only release-window diagnostic. The reader record already carries the
                # native release id captured by notify_release(seq); the transport must not stamp a
                # mutable "latest" id or route the label into session/learning authority.
                try:
                    _sgs = getattr(self._meter_detector, 'set_green_grade_sink', None)
                    if callable(_sgs):
                        _sgs(self._on_green_grade)
                except Exception:
                    pass
                # [ORION_RELEASE_ORACLE_TRIM 2026-09-15] The reader's post-release retraction
                # measurement leaves as ONE JSON line per release on the banner verdict's own
                # stdout channel (RemotePlaySession parses `event == "release_oracle"` and feeds
                # gap_px + verdict_proxy to the Shot Lead trim). The reader can find this writer
                # by itself through sys.modules, but the wiring is made explicit here so the
                # data path is greppable next to the other sinks. Guarded: an older reader
                # without the hook simply keeps its ERROR line.
                try:
                    _ros = getattr(self._meter_detector, 'set_release_oracle_sink', None)
                    if callable(_ros):
                        # [ORION_SHOT_RECORDS] The tee forwards the identical bytes to the
                        # native after handing the record a copy; the engine's trim is
                        # unaffected whether or not the recorder exists.
                        _ros(self._shot_record_oracle_sink
                             if getattr(self, '_shot_records', None) is not None
                             else emit_stdout_jsonl)
                except Exception:
                    pass
                # Frozen-meter latency oracle + boot-probe (ORION_MEASURE_LATENCY, default on).
                try:
                    from latency_estimator import try_load as _load_latency
                    self._latency_route_scope_value = _latency_route_scope(self.config)
                    self._latency_estimator = _load_latency(
                        route_scope=self._latency_route_scope_value)
                    if self._latency_estimator is not None:
                        logger.warning('Measured-latency oracle ENABLED -> measured_latency_ms telemetry')
                except Exception as _lte:
                    self._latency_estimator = None
                    logger.error(f'latency estimator init failed: {_lte}')
                if os.environ.get('ORION_ANIM_ANCHOR_SHADOW', '').strip().lower() in ('1', 'true', 'yes', 'on'):
                    try:
                        from animation_anchor import AnimationAnchor
                        self._anim_anchor = AnimationAnchor(enabled=True)
                        logger.info('Animation anchor SHADOW enabled (compute+log only; never controls release)')
                    except Exception as _ae:
                        logger.error(f'anim anchor init failed: {_ae}')
            except Exception as e:
                logger.error(f'Failed to initialize CV pipeline: {e}')
        # Pose-based timing detector for no-meter mode
        self._active_pose_arm_token = 0
        self._pose_timing = None
        if config.no_meter_enabled:
            try:
                os.environ['ORION_POSE_ZEROCROSS'] = '1'  # Enable sub-frame zero-crossing release
                from pose_timing import PoseTimingDetector
                self._pose_timing = PoseTimingDetector(
                    pose_model_path="models/orion_pose2k_n_v2.pt",
                    bar_model_path="models/orion_bar_park.pt",
                    player_model_path="models/orion_player_detect_v9.pt",
                    handedness=config.no_meter_handedness,
                    on_landmark=self._on_pose_landmark,
                )
                logger.info('Pose timing detector initialized (no-meter mode)')
            except Exception as e:
                logger.error(f'Failed to initialize pose timing detector: {e}')

    def set_video_callback(self, callback):
        self._video_callback = callback

    def _on_pose_landmark(self, landmark):
        """Callback for pose timing detector â€” emits pose_landmark event for C++ engine."""
        try:
            import json as _json
            arm_token = _parse_pose_arm_token(getattr(landmark, "arm_token", 0))
            if arm_token == 0 or arm_token != self._active_pose_arm_token:
                logger.warning(
                    "POSE LANDMARK dropped: arm_token=%s active=%s frame_seq=%s",
                    arm_token, self._active_pose_arm_token,
                    getattr(landmark, "frame_seq", -1),
                )
                return
            payload = _json.dumps({
                "event": "pose_landmark",
                "kind": landmark.kind,
                "frame_seq": landmark.frame_seq,
                "subframe_seq": round(float(getattr(landmark, "subframe_seq", -1.0)), 3),
                "confidence": round(landmark.confidence, 3),
                "arm_token": str(arm_token),
            }, separators=(",", ":"))
            emit_stdout_jsonl(payload + "\n")   # shared IPC lock (bughunt #4)
            logger.warning("POSE LANDMARK: kind=%s frame_seq=%d subframe=%.1f conf=%.2f",
                           landmark.kind, landmark.frame_seq,
                           float(getattr(landmark, "subframe_seq", -1.0)), landmark.confidence)
        except Exception as exc:
            logger.warning("pose_landmark emit failed: %s", exc)

    def _emit_pose_overlay(self):
        """Stream the locked player's skeleton + lock box to native for the on-capture overlay.
        last_overlay is {"kpts": [[x,y,conf]x17], "box": [x1,y1,x2,y2], plus the camera-anchor
        fields "anchor"/"lock_center"/"indicator" (all full-frame px)} or None
        when unlocked. Emit every frame while locked + one clear when the lock drops.
        anchor/lock_center/indicator are forwarded when present; a null indicator (player marker
        not detected this frame) is passed through as null so the consumer simply skips its draw."""
        try:
            ov = getattr(self._pose_timing, "last_overlay", None)
            active = ov is not None
            if not active and not getattr(self, "_overlay_was_active", False):
                return
            import json as _json
            if active:
                payload = _json.dumps({"event": "pose_overlay", "kpts": ov.get("kpts", []),
                                       "box": ov.get("box"),
                                       "anchor": ov.get("anchor"),
                                       "lock_center": ov.get("lock_center"),
                                       "indicator": ov.get("indicator")},
                                      separators=(",", ":"))
            else:
                payload = _json.dumps({"event": "pose_overlay", "box": None}, separators=(",", ":"))
            emit_stdout_jsonl(payload + "\n")   # shared IPC lock (bughunt #4)
            self._overlay_was_active = active
        except Exception:
            pass

    def _on_green_grade(self, rec: dict):
        """Queue one marker-backed release-window diagnostic for sidecar telemetry.

        Identity comes from the reader's shot record.  Proxy/invalid records stay available in
        the reader's local diagnostic log but cannot cross the production telemetry boundary.
        """
        try:
            rec = dict(rec)
            release_seq = int(rec.get("release_seq", 0))
            physical_epoch = _parse_pose_arm_token(rec.get("physical_epoch", 0))
            shot_attempt = _parse_pose_arm_token(rec.get("shot_attempt", 0))
            if (bool(rec.get("release_proxy", True)) or release_seq <= 0
                    or physical_epoch <= 0 or shot_attempt <= 0):
                logger.warning(
                    "release-window diagnostic dropped: complete marker-backed identity missing")
                return False
            with self._green_grade_lock:
                self._green_grade_seq += 1
                rec["seq"] = int(self._green_grade_seq)
                rec["release_seq"] = release_seq
                rec["physical_epoch"] = physical_epoch
                rec["shot_attempt"] = shot_attempt
                self._green_grade_pending = rec
            return True
        except Exception:
            return False

    def pop_green_grade(self):
        """Drain the pending release-window diagnostic once (legacy method name)."""
        try:
            with self._green_grade_lock:
                rec, self._green_grade_pending = self._green_grade_pending, None
            return rec
        except Exception:
            return None

    @_with_shot_gate_lock
    def _arm_shot_gate(self, source: str = "hw", shot_epoch=0,
                       notify_reader_start: bool = True):
        """Arm a shot-gated meter reader (SimpleMeterReader) for a bounded window from the current
        frame. THE plumb between the shot-begin signals (native pose_arm / SQUARE / stick edges) and
        the reader's set_shot_state hook: while armed the reader relaxes its early-rise acquire gate
        and extends its coast. Guarded so it is a no-op for the serving chain (no set_shot_state).
        Every caller is a PHYSICAL shot-begin signal, so this also opens the hw-arm window (the
        colour-calibration training gate); `source` classifies the arm for diagnostics. The CV
        self-arm in _processing_loop never calls this -- it refreshes the merged deadline only.

        Returns (incoming_epoch, effective_epoch, notified_reader) so the public native-command
        entry can log a one-line receipt without re-deriving the epoch arbitration."""
        incoming_epoch = _parse_pose_arm_token(shot_epoch)
        current_epoch = _parse_pose_arm_token(getattr(self, '_shot_gate_epoch', 0))
        if 0 < incoming_epoch < current_epoch:
            return incoming_epoch, current_epoch, False
        _gate_now = time.perf_counter()
        self._shot_gate_deadline_seq = self._frame_seq + self._shot_gate_arm_frames
        self._shot_gate_hw_deadline_seq = self._frame_seq + self._shot_gate_arm_frames
        self._shot_gate_deadline_monotonic = _gate_now + self._shot_gate_max_seconds
        self._shot_gate_hw_deadline_monotonic = _gate_now + self._shot_gate_max_seconds
        self._shot_gate_source = str(source)
        # Native/controller epochs are monotonic and authoritative. An untokenized local
        # input-router or later pose refresh may extend detector wake-up, but once a native
        # epoch exists it must never overwrite that epoch or reset the reader's shot identity.
        new_tokenized_epoch = bool(incoming_epoch > current_epoch)
        if new_tokenized_epoch:
            self._shot_gate_epoch = incoming_epoch
        elif current_epoch == 0:
            self._shot_gate_epoch = 0
        effective_epoch = _parse_pose_arm_token(self._shot_gate_epoch)
        notify_reader_start = bool(
            notify_reader_start
            and (new_tokenized_epoch or (incoming_epoch == 0 and current_epoch == 0)))
        det = self._meter_detector
        notified_reader = False
        notify_start = getattr(det, "notify_physical_shot_start", None)
        if notify_reader_start and callable(notify_start):
            try:
                notify_start(effective_epoch)
                notified_reader = True
            except TypeError:
                # Older readers can still wake, but without an epoch they can never
                # mint automatic-calibration authority.
                try:
                    notify_start()
                    notified_reader = True
                except Exception as exc:
                    logger.debug("notify_physical_shot_start failed: %s", exc)
            except Exception as exc:
                logger.debug("notify_physical_shot_start failed: %s", exc)
        fn = getattr(det, "set_shot_state", None)
        if callable(fn):
            try:
                # controller/native shot-start is ground truth -> full arm confidence; the 3rd
                # arg pushes the hw-arm bit (guarded for readers with the legacy 2-arg hook).
                try:
                    fn(True, 1.0, True)
                except TypeError:
                    fn(True, 1.0)
            except Exception as e:
                logger.debug("set_shot_state(arm) failed: %s", e)
        return incoming_epoch, effective_epoch, notified_reader

    def _forward_shot_gate_shot_type(self, epoch: int, shot_type: str, rhythm: bool) -> bool:
        """Hand the reader the engine's classification for `epoch`. -> did it take it?

        [ORION_SHOT_GATE_TYPE 2026-09-15] Guarded exactly like every other reader hook on this
        path: an older reader without the method simply keeps the union onset window. Never
        raises -- a missing type costs a wider expectation window, never a frame.
        """
        if epoch <= 0 or not str(shot_type or "").strip():
            return False
        fn = getattr(self._meter_detector, "notify_physical_shot_type", None)
        if not callable(fn):
            return False
        try:
            fn(epoch, str(shot_type), bool(rhythm))
            return True
        except Exception as exc:
            logger.debug("notify_physical_shot_type failed: %s", exc)
            return False

    @_with_shot_gate_lock
    def arm_shot_gate(self, source: str = "hw", shot_epoch=0, shot_type="", rhythm=False):
        """Public, input-only wake-up used by the native first-edge command.

        It grants detector acquisition/coast state only. Release authority remains
        in the native engine and its later tokenized pose_arm/vision-epoch checks.

        [ORION_SHOT_GATE_TYPE 2026-09-15] `shot_type` is the engine's own classification of
        this press ("Standstill" / "Left Fade" / ...) and `rhythm` whether the release carries
        the Rhythm flick offset. Both are OPTIONAL: an old native sends neither and the reader
        keeps the union meter-onset window, which is exactly the pre-change behaviour. The
        native re-sends the SAME epoch with source="type_upgrade" when its blind 200 ms grace
        re-types a Standstill into a fade; that lands here as a duplicate epoch (notify=0, the
        reader's shot identity and its early trajectory are kept) and updates only the type.
        """
        incoming_epoch = _parse_pose_arm_token(shot_epoch)
        current_epoch = _parse_pose_arm_token(getattr(self, '_shot_gate_epoch', 0))
        if 0 < incoming_epoch < current_epoch:
            logger.warning("SHOT-GATE STALE ARM IGNORED: epoch=%d current_epoch=%d",
                           incoming_epoch, current_epoch)
            return False
        incoming_epoch, effective_epoch, notified_reader = self._arm_shot_gate(
            source, shot_epoch, notify_reader_start=True)
        # AFTER the arm: notify_physical_shot_start installs the epoch the type belongs to.
        typed = self._forward_shot_gate_shot_type(effective_epoch, shot_type, rhythm)
        self._shot_gate_edge_pending = True
        # [ORION_SHOT_RECORDS 2026-09-16] THE PRESS IS THE RECORD'S t=0.  Both of these are
        # O(1) on this thread (a dict insert and a deque replay of at most PRE_MS of frame
        # REFERENCES); neither touches the disk, the detector or the engine.  A duplicate
        # epoch (the native's source=type_upgrade re-arm) updates the type in place and
        # leaves the open window and the press timestamp exactly where they were.
        rec = getattr(self, '_shot_records', None)
        if rec is not None and effective_epoch > 0:
            try:
                rec.note_press(effective_epoch, ts_ms=time.time() * 1000.0,
                               mono_ms=time.perf_counter() * 1000.0, source=source,
                               shot_type=shot_type, rhythm=rhythm)
            except Exception as exc:
                logger.debug('shot record note_press failed: %s', exc)
        # [ORION_SHOT_RANGE 2026-09-17] The range window is press-anchored exactly as the
        # record is, so it opens on the same edge and on the same clock.  A duplicate epoch
        # (the native's type_upgrade re-arm) is refused inside note_press: the press did not
        # move, and restarting its window would throw away frames already collected.
        _rng = getattr(self, '_shot_range', None)
        if _rng is not None and effective_epoch > 0:
            try:
                _rng.note_press(effective_epoch, time.perf_counter() * 1000.0)
            except Exception as exc:
                logger.debug('shot range note_press failed: %s', exc)
        if effective_epoch > 0 and int(getattr(self, '_shot_record_onset_epoch', 0)) != int(
                effective_epoch):
            self._shot_record_onset_epoch = int(effective_epoch)
            self._shot_record_onset_done = False
            self._shot_record_icon_next = 0.0
        self.framedump_press_open(effective_epoch)
        # PERMANENT t=0 RECEIPT FORENSICS (one line per physical shot edge). This is the
        # arrival half of the native's silent Path A ("shot_gate_arm send" on the native
        # side); the existing "SHOT-GATE ARMED/DISARMED" lines fire only on gate
        # TRANSITIONS, so a per-shot receipt was previously invisible whenever the gate
        # stayed armed across shots. notify=0 with a nonzero epoch means the epoch did
        # not advance (duplicate/stale arm) so the reader's shot identity was kept.
        # [ORION_SHOT_GATE_TYPE 2026-09-15] shot_type/rhythm/typed are APPENDED to the line, so
        # every existing reader of this receipt is unaffected. `unclassified` is EMITTED, never
        # omitted, when the arm carried no type -- a reader must be able to separate "this edge
        # was never typed" from "this build predates the field". typed=0 with a nonzero
        # shot_type means the reader refused/lacks the hook and stayed on the union window.
        try:
            logger.warning(
                "SHOT-GATE ARM RECEIPT: src=%s epoch=%d effective_epoch=%d notify=%d "
                "frame_seq=%d shot_type=%s rhythm=%d typed=%d",
                str(source)[:24], incoming_epoch, effective_epoch,
                int(notified_reader), self._frame_seq,
                (str(shot_type).strip().replace(" ", "_")[:24] or "unclassified"),
                int(bool(rhythm)), int(typed))
        except Exception:
            pass
        return True

    def release_shot_gate(self, shot_epoch=0, release_ms=0.0):
        """The engine issued this press's RELEASE edge (native `shot_gate_release`).

        [ORION_SHOT_GATE_RELEASE 2026-09-15] The arrival half of the native's
        "shot_gate_release send" line. The reader's press window (player_anchor.ARM, which
        bounds the nameplate anchor and the meter-onset expectation) ends HERE instead of
        timing out on ORION_ANCHOR_ARM_S ~1 s after the ball has left the hand.

        Detector acquisition/coast state is NOT touched: the latency oracle and the
        release-window diagnostic still need roughly 1.2 s of post-release meter frames, and
        _settle_shot_gate remains the only owner of those deadlines.
        """
        # [ORION_BANNER_VERDICT_LIVE 2026-09-15] This edge is the ONLY thing that makes the
        # game's next shot-feedback panel ours. It is the single funnel for every engine
        # release -- the normal one and the METER BACKSTOP one -- so the live banner reader
        # binds its verdicts here and refuses to forward a panel nobody shot (a replay
        # screen re-read 16 times in 39 s on 2026-09-15). disarm_shot_gate deliberately does
        # NOT feed it: a cancelled press is not a shot. Never raises, costs one lock.
        _bv = getattr(self, '_banner_verdict', None)
        if _bv is not None:
            try:
                _bv.note_release(_parse_pose_arm_token(shot_epoch), release_ms)
            except Exception:
                pass
        return self._close_shot_gate_press(shot_epoch, "release", release_ms)

    def disarm_shot_gate(self, shot_epoch=0, reason="disarm"):
        """The press ended with NO release edge: a manual cancel, a tap, or an engine abort.

        Same effect on the reader as release_shot_gate -- the press is over either way, and an
        anchor left armed on a press nobody will ever shoot is pure cost plus a false-lock
        licence on whatever comes on screen next.
        """
        return self._close_shot_gate_press(shot_epoch, str(reason or "disarm"), None)

    @_with_shot_gate_lock
    def _close_shot_gate_press(self, shot_epoch, reason: str, release_ms):
        """Shared body of release_shot_gate/disarm_shot_gate + their one receipt line."""
        epoch = _parse_pose_arm_token(shot_epoch)
        closed = False
        fn = getattr(self._meter_detector, "notify_physical_shot_release", None)
        if callable(fn):
            try:
                closed = bool(fn(epoch, release_ms, reason))
            except TypeError:
                # Older readers may expose only the epoch form.
                try:
                    closed = bool(fn(epoch))
                except Exception as exc:
                    logger.debug("notify_physical_shot_release failed: %s", exc)
            except Exception as exc:
                logger.debug("notify_physical_shot_release failed: %s", exc)
        # _shot_gate_edge_pending is deliberately NOT touched: it is arm_pose's "did the native
        # already wake the reader for THIS shot" latch, and clearing it here would let a
        # late pose_arm re-notify the reader and erase the press's early trajectory.
        #
        # [ORION_SHOT_RECORDS 2026-09-16] The reader's release hook above is what flushes this
        # press's PICKUP line, so its record is complete exactly HERE and nowhere earlier.
        # Read it (never write it) and fold it into the shot record, then mark the release.
        rec = getattr(self, '_shot_records', None)
        if rec is not None and epoch > 0:
            try:
                pickup = self._read_pickup_record(epoch)
                if pickup is not None:
                    rec.note_pickup(epoch, pickup)
                if reason == "release":
                    rec.note_release(epoch, release_ms)
                else:
                    rec.note_disarm(epoch, reason)
            except Exception as exc:
                logger.debug('shot record close failed: %s', exc)
        # [ORION_SHOT_RANGE 2026-09-17] Flush the range window if the press ended before it
        # filled (a tap, an abort).  A window that already went to the worker is untouched.
        _rng = getattr(self, '_shot_range', None)
        if _rng is not None and epoch > 0:
            try:
                _rng.note_close(epoch)
            except Exception as exc:
                logger.debug('shot range note_close failed: %s', exc)
        self.framedump_press_close(epoch)
        label = "RELEASE" if reason == "release" else "DISARM"
        try:
            logger.warning(
                "SHOT-GATE %s RECEIPT: epoch=%d reason=%s release_ms=%.1f closed=%d "
                "frame_seq=%d",
                label, epoch, str(reason)[:32].replace(" ", "_"),
                float(release_ms) if release_ms is not None else -1.0,
                int(closed), self._frame_seq)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ #
    #  SHOT RECORDS -- the three late instruments that do not arrive on the press edge.
    # ------------------------------------------------------------------ #
    def _read_pickup_record(self, epoch):
        """READ (never write) the locator's PICKUP record for `epoch`.

        This is the same dict `SimpleMeterReader._flush_pickup_line` logs -- first sight
        of the meter and whether the player anchor is what found it. It lives on the CV
        proposer: SimpleMeterReader._meter_detector -> AsyncMeterLocator._base.
        A direct locator/legacy adapter may instead expose _base itself. These are the
        only supported layouts; this is not a recursive search for another press's record.
        Under a proposer that keeps no pickup record this
        returns None and the shot record simply carries `pickup: null` and falls back to
        the detect loop's own first-detection stamp for the onset.
        """
        try:
            requested_epoch = _parse_pose_arm_token(epoch)
            if requested_epoch <= 0:
                return None
            reader = getattr(self, '_meter_detector', None)
            locator = getattr(reader, '_meter_detector', None)
            base = getattr(locator if locator is not None else reader, '_base', None)
            pickup = getattr(base, 'pickup', None)
            if not isinstance(pickup, dict):
                return None
            out = dict(pickup)
            if _parse_pose_arm_token(out.get('epoch', 0)) != requested_epoch:
                return None                 # a record for a different press is not ours
            stats = getattr(base, 'stats', None)
            if isinstance(stats, dict):
                out['patch_hits'] = int(stats.get('anchor_patch_hit', 0) or 0)
            return out
        except Exception:
            return None

    def _shot_record_oracle_sink(self, line):
        """Tee the reader's `release_oracle` JSON line: shot record first, then the pipe.

        The native's Shot Lead trim is the line's real consumer, so the forward to stdout
        happens whatever the record does -- and it happens even if this method raises.
        """
        rec = getattr(self, '_shot_records', None)
        if rec is not None:
            try:
                payload = json.loads(line)
                rec.note_oracle(payload.get('release_seq', 0), payload)
            except Exception as exc:
                logger.debug('shot record oracle tee failed: %s', exc)
        emit_stdout_jsonl(line)

    def _shot_record_banner_sink(self, line):
        """Tee the banner reader's `banner_verdict` JSON line the same way.

        Only ATTRIBUTED verdicts reach an emit_line at all (banner_verdict_live refuses to
        forward a panel nobody shot), so every line here already carries the release_seq
        the record is keyed on.
        """
        rec = getattr(self, '_shot_records', None)
        if rec is not None:
            try:
                rec.note_banner(json.loads(line))
            except Exception as exc:
                logger.debug('shot record banner tee failed: %s', exc)
        emit_stdout_jsonl(line)

    def _shot_record_frame_hook(self, frame, result, now):
        """Detect-thread half of the shot record.  READS ONLY FIELDS THIS FRAME ALREADY HAS.

        Two things, both bounded:
          * the FALLBACK meter onset -- the first frame of this press the reader called
            detected.  Two attribute reads and a comparison (~0.5 us); the PICKUP record
            still wins when it exists.
          * the OPT-IN nameplate "3"-cell brightness sample at 10 Hz
            (ORION_SHOT_RECORD_ICON=1), taken at the plate position player_anchor already
            found on this frame.  0.026 ms per sample, measured; off by default.
        """
        rec = getattr(self, '_shot_records', None)
        if rec is None:
            return
        epoch = int(getattr(self, '_shot_record_onset_epoch', 0) or 0)
        if epoch <= 0:
            return
        detected = bool(getattr(result, 'detected', False)) if result is not None else False
        if detected and not getattr(self, '_shot_record_onset_done', False):
            self._shot_record_onset_done = True
            try:
                press_ms = self._shot_record_press_mono_ms(epoch)
                if press_ms is not None:
                    rec.note_onset(epoch, now * 1000.0 - press_ms,
                                   fill=getattr(result, 'fill_pct', None),
                                   source='detect_loop')
            except Exception:
                pass
        if getattr(self, '_shot_record_icon', False)                 and now >= float(getattr(self, '_shot_record_icon_next', 0.0) or 0.0):
            self._shot_record_icon_next = now + 0.1        # 10 Hz
            try:
                self._shot_record_icon_sample(rec, epoch, frame, now)
            except Exception:
                pass

    def _shot_range_result(self, epoch, payload, cells):
        """[ORION_SHOT_RANGE 2026-09-17] The range reader's own worker calls this once per
        press, just before it emits the JSON line.  It only files the reading in the shot
        record: the engine is told on the stdout channel, never from here."""
        rec = getattr(self, '_shot_records', None)
        if rec is None:
            return
        try:
            rec.note_range(epoch, payload, cells)
        except Exception as exc:
            logger.debug('shot range record tee failed: %s', exc)

    def _shot_record_press_mono_ms(self, epoch):
        rec = getattr(self, '_shot_records', None)
        if rec is None:
            return None
        try:
            with rec._lock:                       # noqa: SLF001 -- same-process read
                open_rec = rec._open.get(int(epoch))
                return None if open_rec is None else open_rec.get('press_mono_ms')
        except Exception:
            return None

    def _shot_record_icon_sample(self, rec, epoch, frame, now):
        """One nameplate-"3"-cell brightness sample, derived offline into icon_off_ms.

        GEOMETRY comes straight from tools/diagnostics/hud_3pt_icon.py: the "3" disc sits
        one disc-pitch (25 px at scale 1.0) to the LEFT of the PlayStation-logo disc that
        player_anchor already tracks.  The icon is a near-black disc with a bright glyph;
        when the ball goes it is replaced by court wood, so the cell's MEAN jumps and its
        dark fraction collapses -- which is the edge the offline pass reads.
        """
        import player_anchor as _pa
        last = getattr(_pa.ANCHOR, '_last', None)
        if not last or frame is None:
            return
        icon_x, icon_y, scale = float(last[0]), float(last[1]), float(last[2] or 1.0)
        half = max(6.0, 11.0 * scale)
        cx = icon_x - 25.0 * scale
        h, w = frame.shape[:2]
        x0, x1 = int(max(0, cx - half)), int(min(w, cx + half))
        y0, y1 = int(max(0, icon_y - half)), int(min(h, icon_y + half))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return
        cell = frame[y0:y1, x0:x1]
        mean = float(cell.mean())
        dark = float((cell.max(axis=2) < 75).mean()) if cell.ndim == 3 else float(
            (cell < 75).mean())
        press_ms = self._shot_record_press_mono_ms(epoch)
        t_ms = (now * 1000.0 - press_ms) if press_ms is not None else -1.0
        rec.note_icon_sample(epoch, t_ms, mean, dark)

    @_with_shot_gate_lock
    def _refresh_cv_shot_gate(self, frame_seq):
        """Refresh merged acquisition only; CV never creates hardware authority."""
        # MERGED deadline only. NEVER _shot_gate_hw_deadline_seq (plan
        # B1 [fix]): a CV-armed window must not open colour training --
        # a false lock training itself was the failure mode this kills.
        if frame_seq > self._shot_gate_deadline_seq:
            self._shot_gate_source = 'cv'   # never overwrites a live physical source
        self._shot_gate_deadline_seq = frame_seq + self._shot_gate_arm_frames
        self._shot_gate_deadline_monotonic = (
            time.perf_counter() + self._shot_gate_max_seconds)

    @_with_shot_gate_lock
    def _push_reader_shot_gate(self, frame_seq):
        """Snapshot and apply one gate state without losing a concurrent press."""
        _sg = getattr(self._meter_detector, "set_shot_state", None)
        if callable(_sg):
            # Frame sequence intentionally freezes on dark/loading frames, so the
            # monotonic cap is mandatory.  Both bounds must remain valid.
            _armed_now, _armed_hw_now = self._shot_gate_state(frame_seq)
            if _armed_now != self._shot_gate_armed_prev:
                # CLEAR arm/disarm marker so a live log can confirm the shot-gate is
                # firing (the missing signal in session_20260706_190737).
                logger.warning("SHOT-GATE %s at frame_seq=%d (deadline=%d hw=%d src=%s)",
                               "ARMED" if _armed_now else "DISARMED",
                               frame_seq, self._shot_gate_deadline_seq,
                               self._shot_gate_hw_deadline_seq,
                               self._shot_gate_source or "-")
                self._shot_gate_armed_prev = _armed_now
            try:
                _sg(_armed_now, 0.0, _armed_hw_now)
            except TypeError:
                # legacy 2-arg reader hook (guarded: chain readers differ)
                try:
                    _sg(_armed_now)
                except Exception:
                    pass
            except Exception:
                pass

    @_with_shot_gate_lock
    def _shot_gate_state(self, frame_seq: int = None, monotonic_now: float = None):
        """Return (merged, hardware) gate state under BOTH frame and wall caps.

        Valid capture frame_seq intentionally freezes on dark/loading frames. A
        sequence-only arm therefore never expired while the game was loading and
        could still authorize a menu false-lock when pixels resumed. Wall time is
        authoritative for that case; the sequence cap remains defense in depth.
        """
        _seq = self._frame_seq if frame_seq is None else int(frame_seq)
        _now = time.perf_counter() if monotonic_now is None else float(monotonic_now)
        _merged = (_seq <= self._shot_gate_deadline_seq
                   and _now <= self._shot_gate_deadline_monotonic)
        _hardware = (_seq <= self._shot_gate_hw_deadline_seq
                     and _now <= self._shot_gate_hw_deadline_monotonic)
        return _merged, _hardware

    @_with_shot_gate_lock
    def _settle_shot_gate(self, source: str = "release"):
        """Bound post-release vision without cutting off detector diagnostics.

        The latency oracle and release-window diagnostic need roughly 1.2 seconds of post-release
        meter
        frames.  Keep a short three-second tail, but never leave the full long-Go-To arm active in
        menus between shots.  A later physical arm always replaces these deadlines.
        """
        _now = time.perf_counter()
        _tail = self._shot_gate_post_release_seconds
        if self._shot_gate_deadline_monotonic >= 0.0:
            self._shot_gate_deadline_monotonic = min(
                self._shot_gate_deadline_monotonic, _now + _tail)
        if self._shot_gate_hw_deadline_monotonic >= 0.0:
            self._shot_gate_hw_deadline_monotonic = min(
                self._shot_gate_hw_deadline_monotonic, _now + _tail)
        self._shot_gate_source = str(source)

    @_with_shot_gate_lock
    def arm_pose(self, arm_token):
        """Arm the pose zero-cross release search at the current frame. Called from the native engine
        via the 'pose_arm' stdin command on shot-begin. The live virtual_controller=False path has NO
        orchestrator input-router loop (that only starts when the sidecar drives the controller), so the
        SQUARE-press arming MUST come from the native that owns the controller -- this is that hook."""
        token = _parse_pose_arm_token(arm_token)
        if token == 0:
            logger.error("POSE ARM rejected: invalid arm_token=%r", arm_token)
            return False
        self._active_pose_arm_token = token
        # Shot-start also arms the shot-gated meter reader. If the native already
        # sent the first physical edge for this shot, refresh only: a second
        # notify_physical_shot_start here would erase the early trajectory.
        had_early_edge = self._shot_gate_edge_pending
        self._shot_gate_edge_pending = False
        self._arm_shot_gate("pose", notify_reader_start=not had_early_edge)
        if self._pose_timing is not None:
            try:
                self._pose_timing.notify_shot_start(
                    self._frame_seq, arm_token=token)
                logger.warning(
                    "POSE ARM: shot-start -> armed pose + shot-gate at frame_seq=%d token=%d",
                    self._frame_seq, token)
            except Exception as e:
                logger.error("arm_pose failed: %s", e)
                return False
        else:
            logger.warning("POSE ARM: shot-start received (pose detector None) -> armed shot-gate only "
                           "at frame_seq=%d", self._frame_seq)
        return True

    def update_meter(self, meter_style=None, meter_color=None):
        """Live-apply a meter style/color change to the running detector so the
        Meter tab's color/style controls take effect WITHOUT a reconnect.
        meter_color drives the HSV detection mask (the important one); a
        meter_style narrows detection to that meter's contour."""
        det = self._meter_detector
        if det is None:
            return
        try:
            color = str(meter_color).strip() if meter_color else ''
            cfg = getattr(det, '_cfg', None)
            # Park mode IS the red meter: don't let a live UI colour change un-pin Red and silently
            # disable the park path (it would drop back to the old wrong-colour false-locking scan).
            #
            # SCOPE (2026-08-04): same scoping as the construction-time pin -- park belongs to the
            # LEGACY serving chain. The simple/compressed readers have no park path, so ignoring a
            # live colour change for them just silently discarded the user's selection. Detected
            # from the READER ITSELF (not the env flags) so this stays correct for any entry point
            # that constructs the reader directly. CompressedMeterReader subclasses
            # SimpleMeterReader, so the isinstance check covers both.
            _park_owns_colour = False
            if cfg is not None and getattr(cfg, 'park_temporal_enabled', False):
                _park_owns_colour = True
                try:
                    from simple_meter_reader import SimpleMeterReader as _SMR
                    if isinstance(det, _SMR):
                        _park_owns_colour = False
                except Exception:
                    pass
            if color and color != "Red" and _park_owns_colour:
                logger.warning('park: ignoring live meter_color change %s (pinned Red while park is on)', color)
                color = ''
            # Native sends the complete remap after EVERY settings save, including
            # the previous shot's calibration save during a new shot. Those are
            # not source/profile changes: reloading the same colour used to erase
            # the live lock, session ruler and trained HSV on each such message.
            # Keep applied identity separately from cfg: callers may mutate that
            # shared object before notifying us. Reader-owned identity takes
            # precedence so explicit reload_config calls are also observed.
            prior = getattr(self, '_meter_applied_profile', None)
            same_detector = prior is not None and prior[0] is det
            applied_color = getattr(det, '_meter_color', None)
            if applied_color is None:
                applied_color = prior[1] if same_detector else getattr(cfg, 'meter_color', '')
            applied_style = getattr(det, '_tracking_meter_style', None)
            if applied_style is None:
                applied_style = prior[2] if same_detector else getattr(det, '_active', None)
            color_changed = bool(color) and color.casefold() != str(applied_color or '').strip().casefold()
            style = str(meter_style).strip() if meter_style else ''
            style_changed = bool(style) and style.casefold() != str(applied_style or '').strip().casefold()
            if color:
                self.config.meter_color = color
                if cfg is not None:
                    cfg.meter_color = color
            if color_changed:
                if cfg is not None:
                    # New color => the trained HSV ranges no longer apply.
                    cfg.meter_hsv_low = None
                    cfg.meter_hsv_high = None
                    if hasattr(det, 'reload_config'):
                        det.reload_config(cfg)
            if style and hasattr(det, 'set_active_style'):
                self.config.meter_style = style
                if cfg is not None and hasattr(cfg, 'meter_style'):
                    cfg.meter_style = style
                if style_changed:
                    det.set_active_style(style)
            if color_changed and not style_changed and hasattr(det, 'reset_tracking'):
                # Color-only change: drop stale tracking so the next frame
                # re-locks with the new mask instead of fighting old history.
                det.reset_tracking()
            self._meter_applied_profile = (
                det, color if color else applied_color,
                style if style else applied_style)
            if color_changed or style_changed:
                logger.info('Meter detector updated live (style=%s, color=%s)', style or '-', color or '-')
        except Exception as e:
            logger.error(f'update_meter failed: {e}')

    def _client_state_lock(self):
        # Initialized before runtime threads start. Lazy initialization also supports
        # lightweight diagnostic/test instances constructed without __init__.
        lock = getattr(self, '_client_lifecycle_lock', None)
        if lock is None:
            lock = self.__dict__.setdefault('_client_lifecycle_lock', threading.RLock())
        return lock

    def _launch_remote_play_client(self, wait_timeout_s=None, console_wake_allowed=True):
        """Single-flight bring-up; shutdown is terminal for this orchestrator instance.

        Never hold the publication lock across console discovery/readiness waits.
        close_remote_play_client can revoke and close the exact in-flight manager.
        """
        with self._client_state_lock():
            if (getattr(self, '_client_stop_requested', False)
                    or getattr(self, '_client_launch_inflight', False)):
                return False
            self._client_launch_inflight = True
        try:
            return self._launch_remote_play_client_current(wait_timeout_s, console_wake_allowed)
        finally:
            with self._client_state_lock():
                self._client_launch_inflight = False

    def _launch_remote_play_client_current(self, wait_timeout_s, console_wake_allowed):
        with self._client_state_lock():
            manager = self._client_manager
        if manager is not None:
            alive_fn = getattr(manager, 'is_running', None)
            ready_fn = getattr(manager, 'is_session_ready', None)
            ready = (callable(alive_fn) and bool(alive_fn())
                     and callable(ready_fn) and bool(ready_fn()))
            with self._client_state_lock():
                if (getattr(self, '_client_stop_requested', False)
                        or self._client_manager is not manager):
                    return False
                if ready:
                    self._input_link_ready = True
                    self._input_link_checked_at = time.monotonic()
                    logger.info('Remote Play console session already ready; skipping duplicate launch')
                    return True
                self._client_manager = None
                self._input_link_ready = False
            try:
                manager.stop()
            except Exception as exc:
                logger.debug('Stale Remote Play client cleanup failed: %s', exc)
        if not (CLIENT_AVAILABLE and RemotePlayClientConfig and RemotePlayClientManager):
            logger.warning('Remote Play client manager unavailable')
            return False
        client_cfg = RemotePlayClientConfig(
            # Set by the sidecar's start_stream handler; lets a rest-mode wake extend the
            # native promote deadline instead of failing at 20s (see _ensure_console_awake).
            on_console_waking=getattr(self, "console_waking_callback", None),
            platform=self.config.platform,
            client_mode=self.config.client_mode,
            console_ip=self.config.console_ip,
            window_title=self.config.window_title,
            chiaki_path=self.config.chiaki_path,
            chiaki_identity_size=self.config.chiaki_identity_size,
            chiaki_identity_sha256=self.config.chiaki_identity_sha256,
            close_on_stop=self.config.close_client_on_disconnect,
            # Capture/detection already run independently. Spend the configured
            # connect budget proving console readiness instead of returning after
            # a two-second window/process probe.
            wait_timeout_s=(self.config.wait_timeout_s
                            if wait_timeout_s is None else float(wait_timeout_s)),
            require_session_ready=True,
            # HDMI is already the authoritative video source.  Keep the
            # hidden Chiaki child input/audio-only so its unused decoder cannot
            # steal presentation time from the capture-card SHM preview.
            disable_video=bool(self._cc_mode),
            # Recovery attempts run under a 17s/20s watchdog whose plan
            # arithmetic predates the rest-mode wake; they must not spend
            # attempt time probing or waking a console the user may have
            # just rested deliberately.
            console_wake_allowed=bool(console_wake_allowed),
        )

        with self._client_state_lock():
            if getattr(self, '_client_stop_requested', False):
                return False
            manager = RemotePlayClientManager(client_cfg)
            self._client_manager = manager
        try:
            status = manager.ensure_running()
            ready_fn = getattr(manager, 'is_session_ready', None)
            session_ready = bool(status.ok and callable(ready_fn) and ready_fn())
        except Exception:
            with self._client_state_lock():
                own_cleanup = self._client_manager is manager
                if own_cleanup:
                    self._client_manager = None
                    self._input_link_ready = False
            if own_cleanup:
                manager.stop()
            raise
        # The result and tracker belong to THIS manager, never whichever object
        # happens to occupy the slot after a concurrent stop/replacement.
        with self._client_state_lock():
            if (getattr(self, '_client_stop_requested', False)
                    or self._client_manager is not manager):
                return False
            try:
                summary_fn = getattr(manager, 'stage_summary', None)
                self.last_promotion_stage_summary = str(summary_fn()) if callable(summary_fn) else ''
            except Exception:
                self.last_promotion_stage_summary = ''
            self._input_link_ready = session_ready
            self._input_link_checked_at = time.monotonic()
            if session_ready:
                if status.window_title:
                    self.config.window_title = status.window_title
                if status.hwnd:
                    self._window_handle = status.hwnd
                logger.info('Remote Play console input session ready: %s', status)
                return True
            self._last_error_msg = status.message or 'No current console-session readiness proof.'
            logger.warning('Remote Play input session failed readiness: %s', self._last_error_msg)
            self._client_manager = None
        if not bool(getattr(manager, '_failed_start_reaped', False)):
            try:
                manager.stop()
            except Exception as exc:
                logger.debug('Failed Remote Play input cleanup failed: %s', exc)
        return False

    def input_link_ready(self):
        """Continuously revocable console-route authority, cached for 250 ms."""
        if getattr(self, '_xbox_mode', False):
            from xbox_remote_play import xbox_window_still_matches
            backend = self._frame_backend
            return bool(not getattr(self, '_client_stop_requested', False)
                        and self._xbox_window is not None and backend is not None
                        and backend.is_healthy() and xbox_window_still_matches(self._xbox_window))
        with self._client_state_lock():
            if getattr(self, '_client_stop_requested', False):
                return False
            now = time.monotonic()
            if now - self._input_link_checked_at < 0.25:
                return bool(self._input_link_ready)
            self._input_link_checked_at = now
            was_ready = bool(self._input_link_ready)
            manager = self._client_manager
        # Poll outside the lifecycle lock. A slow log read must not consume the
        # graceful-close budget, and an old poll must not reauthorize a new slot.
        ready_fn = getattr(manager, 'is_session_ready', None) if manager is not None else None
        try:
            ready = bool(callable(ready_fn) and ready_fn())
        except Exception as exc:
            logger.warning('Remote Play input readiness check failed closed: %s', exc)
            ready = False
        with self._client_state_lock():
            if (getattr(self, '_client_stop_requested', False)
                    or self._client_manager is not manager):
                return False
            self._input_link_ready = ready
            if was_ready and not self._input_link_ready:
                # Preserve the reason BEFORE recovery discards this manager. Never
                # dump its config/status repr: they are not a diagnostic allowlist.
                diagnostic = {}
                try:
                    health = getattr(manager, 'readiness_diagnostic', None)
                    diagnostic = health() if callable(health) else {}
                    if not isinstance(diagnostic, dict):
                        diagnostic = {}
                except Exception:
                    pass  # evidence failure must not interrupt readiness revocation
                logger.warning(
                    'INPUT LINK LOST: reason=%s pid=%s exit_code=%s session_log=%s',
                    str(diagnostic.get('reason', 'unavailable'))[:48],
                    diagnostic.get('pid', 0), diagnostic.get('exit_code'),
                    os.path.basename(str(diagnostic.get('session_log', '')))[:128])
            return bool(self._input_link_ready)

    def _rekey_latency_route(self, reason: str = '') -> None:
        """Atomically bind timing state to the config's current exact route identity."""
        lock = getattr(self, '_latency_route_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._latency_route_lock = lock
        with lock:
            source = str(getattr(
                self.config, 'frame_source', '') or '').strip().lower()
            capture_poisoned = bool(
                source in ('capture_card', 'capturecard', 'card')
                and getattr(self, '_capture_warm_cache_revoked', False))
            decoder_revoked = bool(getattr(
                self, '_decoder_warm_cache_revoked', False))
            # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47. Capture poisoning no longer blanks the
            # scope (see _replace_latency_authority) -- blanking it would re-pin generation 0 via
            # the `not scope` snapshot test and undo the recovery path. It must still forbid
            # RESTORE, and that is NOT automatic here: try_load defaults restore_cache=True, so the
            # flag has to be passed explicitly below or a poisoned route would reload the very
            # posterior we stopped trusting.
            capture_invalid_now = bool(
                source in ('capture_card', 'capturecard', 'card')
                and getattr(self, '_capture_route_currently_invalid', False))
            new_scope = ('' if (capture_invalid_now
                                or (source == 'decoder' and decoder_revoked))
                         else _latency_route_scope(self.config))
            old_scope = str(getattr(self, '_latency_route_scope_value', '') or '')
            if (new_scope == old_scope
                    and getattr(self, '_latency_estimator', None) is not None):
                return
            # An ACK token from the old estimator/scope must never ride telemetry from the
            # replacement, even when the delivery backend name itself did not change.
            self._latency_controller_attestation_generation = 0
            self._latency_controller_attestation_scope = ''
            self._latency_controller_attestation_route = ''
            replacement = None
            try:
                from latency_estimator import try_load as _load_latency
                replacement = _load_latency(
                    route_scope=new_scope,
                    restore_cache=not capture_poisoned)
            except Exception as exc:
                logger.warning('Latency route re-key failed closed: %s', exc)
            self._latency_estimator = replacement
            self._latency_route_scope_value = new_scope
            self._latency_estimator_scope_epoch = max(1, int(getattr(
                self, '_latency_estimator_scope_epoch', 1) or 1)) + 1
            logger.warning('Latency route re-keyed (%s); restored authority awaits fresh health',
                           str(reason or 'config_transition')[:48])

    def _attest_reject(self, reason: str) -> bool:
        """Name WHY a controller-route attestation was refused, once per distinct reason.

        [ORION_ATTEST_DIAG 2026-08-18] attest_controller_latency_route has ~10 refusal paths
        and the sidecar collapses every one of them into the single token
        `route_scope_rejected`. Three sessions have now been lost to that opacity (08-08,
        08-17, 08-18 -- the 08-18 evening session rejected 161 times in 4 minutes with the
        scope NON-empty, refuting the first diagnosis made from the bare token). Fail-closed
        stays fail-closed; it names itself. Returns False so callers can `return
        self._attest_reject(...)`.
        """
        seen = getattr(self, '_attest_reject_logged', None)
        if seen is None:
            seen = set()
            self._attest_reject_logged = seen
        if reason not in seen:
            seen.add(reason)
            logger.warning(
                'Controller-route attestation REFUSED (%s): verified=%s route_invalid=%s '
                'epoch=%s highest_gen=%s -- native will log this as route_scope_rejected',
                reason,
                bool(getattr(self, '_capture_warm_cache_verified', False)),
                bool(getattr(self, '_capture_route_currently_invalid', False)),
                getattr(self, '_latency_estimator_scope_epoch', '?'),
                getattr(self, '_latency_controller_highest_attestation_generation', '?'))
        return False

    def attest_controller_latency_route(self, delivery_route: str,
                                        attestation_generation: int) -> bool:
        """Bind cache provenance to a distinct native-confirmed neutral delivery backend.

        This command carries no timestamp, release id, shot attempt, or fill data and cannot touch
        the oracle's pending release/sample state. A backend change atomically replaces the old
        estimator before the new route can emit timing telemetry.
        """
        route = str(delivery_route or '').strip().lower()
        if route not in ('pipe', 'vigem_ds4', 'vigem_xusb'):
            return self._attest_reject('delivery_route_invalid (%r)' % route[:24])
        if (isinstance(attestation_generation, bool)
                or not isinstance(attestation_generation, int)
                or not 0 < attestation_generation <= 0xFFFFFFFFFFFFFFFF):
            return self._attest_reject('attestation_generation_invalid')
        command_lock = getattr(self, '_latency_attestation_command_lock', None)
        if command_lock is None:
            command_lock = threading.Lock()
            self._latency_attestation_command_lock = command_lock
        with command_lock:
            source = str(getattr(
                self.config, 'frame_source', '') or '').strip().lower()
            capture_source = source in ('capture_card', 'capturecard', 'card')
            # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47: gate on "the route is wrong NOW", not on
            # the permanent poisoning flag. `_capture_warm_cache_verified` still fences this on a
            # real stamped frame, so recovery cannot attest on backend properties alone.
            if (capture_source and (bool(getattr(
                    self, '_capture_route_currently_invalid', False)) or not bool(getattr(
                    self, '_capture_warm_cache_verified', False)))):
                return self._attest_reject('capture_route_invalid_or_unverified')
            if (source == 'decoder' and bool(getattr(
                    self, '_decoder_warm_cache_revoked', False))):
                return self._attest_reject('decoder_warm_cache_revoked')

            lock = getattr(self, '_latency_route_lock', None)
            if lock is None:
                return self._attest_reject('latency_route_lock_missing')
            # Reject a replay before it can mutate config or replace authority.
            with lock:
                highest = int(getattr(
                    self, '_latency_controller_highest_attestation_generation', 0) or 0)
                highest_route = str(getattr(
                    self, '_latency_controller_highest_attestation_route', '') or '')
                if (attestation_generation < highest
                        or (attestation_generation == highest
                            and route != highest_route)):
                    return self._attest_reject(
                        'generation_replay_precheck (gen=%d highest=%d)'
                        % (attestation_generation, highest))

            if route != str(getattr(
                    self.config, 'controller_route', '') or '').strip().lower():
                self.config.controller_route = route
                self._rekey_latency_route('controller_delivery_route_transition')
            else:
                expected_scope = _latency_route_scope(self.config)
                if (getattr(self, '_latency_estimator', None) is None
                        or str(getattr(self, '_latency_route_scope_value', '') or '')
                        != expected_scope):
                    self._rekey_latency_route('controller_delivery_route_retry')

            with lock:
                # Re-check provenance after re-keying.  Capture revocation is set
                # before it waits for this lock, so it cannot race a successful ACK.
                source = str(getattr(
                    self.config, 'frame_source', '') or '').strip().lower()
                capture_source = source in ('capture_card', 'capturecard', 'card')
                if ((capture_source and (bool(getattr(
                        self, '_capture_route_currently_invalid', False)) or not bool(getattr(
                        self, '_capture_warm_cache_verified', False))))
                        or (source == 'decoder' and bool(getattr(
                            self, '_decoder_warm_cache_revoked', False)))):
                    return self._attest_reject('capture_route_invalid_or_unverified_postkey')
                scope = _latency_route_scope(self.config)
                current_scope = str(getattr(
                    self, '_latency_route_scope_value', '') or '')
                epoch = max(1, int(getattr(
                    self, '_latency_estimator_scope_epoch', 1) or 1))
                if (not scope or scope != current_scope
                        or getattr(self, '_latency_estimator', None) is None):
                    self._latency_controller_attestation_generation = 0
                    self._latency_controller_attestation_scope = ''
                    self._latency_controller_attestation_route = ''
                    return self._attest_reject(
                        'scope_mismatch_or_estimator_missing (scope_empty=%s equal=%s '
                        'estimator=%s)'
                        % (not scope, scope == current_scope,
                           getattr(self, '_latency_estimator', None) is not None))

                highest = int(getattr(
                    self, '_latency_controller_highest_attestation_generation', 0) or 0)
                binding = (
                    str(getattr(
                        self, '_latency_controller_highest_attestation_route', '') or ''),
                    str(getattr(
                        self, '_latency_controller_highest_attestation_scope', '') or ''),
                    int(getattr(
                        self, '_latency_controller_highest_attestation_epoch', 0) or 0),
                )
                if attestation_generation < highest:
                    return self._attest_reject(
                        'generation_replay (gen=%d highest=%d)'
                        % (attestation_generation, highest))
                if (attestation_generation == highest
                        and binding != (route, scope, epoch)):
                    return self._attest_reject(
                        'generation_binding_mismatch (gen=%d route_same=%s scope_same=%s '
                        'epoch %s vs %s)'
                        % (attestation_generation, binding[0] == route,
                           binding[1] == scope, binding[2], epoch))
                if attestation_generation > highest:
                    self._latency_controller_highest_attestation_generation = \
                        attestation_generation
                    self._latency_controller_highest_attestation_route = route
                    self._latency_controller_highest_attestation_scope = scope
                    self._latency_controller_highest_attestation_epoch = epoch

                # Store only after route re-key/validation completed.  A concurrently arriving
                # route transition takes this same lock and clears the current echo fields.
                self._latency_controller_attestation_generation = attestation_generation
                self._latency_controller_attestation_scope = scope
                self._latency_controller_attestation_route = route
                return True

    def controller_latency_route_attestation_receipt(
            self, delivery_route: str, attestation_generation: int) -> str:
        """Return the exact committed estimator scope for a direct native ACK.

        The caller hashes this value before it crosses stdout.  Re-reading under the same lock as
        route re-keying prevents a successful command from being acknowledged after a concurrent
        source/console/backend transition already revoked it.
        """
        return self.controller_latency_route_attestation_receipt_snapshot(
            delivery_route, attestation_generation)[0]

    def controller_latency_route_attestation_receipt_snapshot(
            self, delivery_route: str, attestation_generation: int) -> tuple:
        """Atomically return ``(scope, estimator_epoch)`` for a valid direct ACK."""
        route = str(delivery_route or '').strip().lower()
        if route not in ('pipe', 'vigem_ds4', 'vigem_xusb'):
            return '', 0
        if (isinstance(attestation_generation, bool)
                or not isinstance(attestation_generation, int)
                or not 0 < attestation_generation <= 0xFFFFFFFFFFFFFFFF):
            return '', 0
        lock = getattr(self, '_latency_route_lock', None)
        if lock is None:
            return '', 0
        with lock:
            source = str(getattr(
                self.config, 'frame_source', '') or '').strip().lower()
            capture_source = source in ('capture_card', 'capturecard', 'card')
            if ((capture_source and (bool(getattr(
                    self, '_capture_route_currently_invalid', False)) or not bool(getattr(
                    self, '_capture_warm_cache_verified', False))))
                    or (source == 'decoder' and bool(getattr(
                        self, '_decoder_warm_cache_revoked', False)))):
                return '', 0
            scope = str(getattr(self, '_latency_route_scope_value', '') or '')
            epoch = max(1, int(getattr(
                self, '_latency_estimator_scope_epoch', 1) or 1))
            if (getattr(self, '_latency_estimator', None) is None or not scope
                    or str(getattr(
                        self, '_latency_controller_attestation_scope', '') or '') != scope
                    or int(getattr(
                        self, '_latency_controller_attestation_generation', 0) or 0)
                        != attestation_generation
                    or str(getattr(
                        self, '_latency_controller_attestation_route', '') or '').strip().lower()
                        != route
                    or int(getattr(
                        self, '_latency_controller_highest_attestation_epoch', 0) or 0)
                        != epoch
                    or int(getattr(
                        self, '_latency_controller_highest_attestation_generation', 0) or 0)
                        != attestation_generation
                    or str(getattr(
                        self, '_latency_controller_highest_attestation_scope', '') or '')
                        != scope
                    or str(getattr(
                        self, '_latency_controller_highest_attestation_route', '') or '')
                        != route):
                return '', 0
            return scope, epoch

    def promote_to_stream(self, console_ip=None, console_identity=None):
        """Warm-preview -> full-stream promotion. Brings up the Chiaki client + input hook IN THE
        ALREADY-RUNNING preview sidecar, WITHOUT restarting the process and WITHOUT disturbing the
        live capture-card feed + detector (both keep running on their own threads â€” the panel never
        blinks). Driven by the native ``start_stream`` stdin command on Connect when it reuses the
        warm preview sidecar instead of killing+rebuilding it (~5-6s saved).

        The capture card is INPUT-only for Chiaki here (video = the card), so this reuses the exact
        same Chiaki/input bring-up the cold connect path uses (``_launch_remote_play_client``) â€” it
        just invokes it in place. Idempotent: if Chiaki is already running it will not double-launch.

        Returns True once the Chiaki/input launch is established (or already was)."""
        ip = (str(console_ip).strip() if console_ip else '') or (self.config.console_ip or '')
        if ip:
            self.config.console_ip = ip
        identity = str(console_identity or '').strip().lower()
        if identity != str(getattr(self.config, 'console_identity', '') or '').strip().lower():
            self.config.console_identity = identity
            self._rekey_latency_route('console_identity_transition')
        # Also (re)point the RTT ping target at the (possibly rediscovered) console IP so latency
        # compensation tracks the promoted stream. Best-effort â€” never fail the promotion on it.
        try:
            if ip and getattr(self, '_rtt_engine', None) is not None \
                    and hasattr(self._rtt_engine, 'set_ping_target'):
                self._rtt_engine.set_ping_target(ip)
        except Exception as exc:
            logger.debug('promote_to_stream: RTT ping retarget skipped: %s', exc)
        logger.info('promote_to_stream: bringing up Chiaki/input in place (console_ip=%s)', ip or '-')
        ok = self._launch_remote_play_client()
        if ok:
            logger.info('promote_to_stream: Chiaki/input link kicked off â€” capture/detector untouched')
        else:
            logger.warning('promote_to_stream: Chiaki/input launch reported failure (%s)',
                           self._last_error_msg or 'unknown')
        return ok

    def prewarm_standby_client(self):
        """[ORION_STANDBY 2026-08-30] Pre-boot the Remote Play client during the warm
        preview so Connect only pays the PS5 handshake, not the client's own boot.

        Starts a daemon loop that keeps ONE standby client alive while this
        orchestrator runs WITHOUT an active client manager (i.e. preview, or after a
        failed promotion restored preview). The pool refuses to spawn beside any
        other chiaki-family process and never spawns `--standby` at a binary that
        lacks it (spawn-free marker sniff â€” a pre-standby client would raise a
        visible modal parser-error dialog and linger, verified 2026-08-30), so
        this is inert on a pre-standby install. Never raises; never blocks the
        caller.
        """
        if get_standby_pool is None or standby_client_enabled is None:
            return
        if not standby_client_enabled():
            logger.info('Standby prewarm disabled via ORION_STANDBY_CLIENT')
            return
        if getattr(self, '_standby_prewarm_thread', None) is not None \
                and self._standby_prewarm_thread.is_alive():
            return

        def _loop():
            first = True
            while getattr(self, '_running', False):
                try:
                    # [ORION_CONNECT_LATENCY 2026-09-14] Console-address prewarm runs on EVERY
                    # tick, not only while no client manager exists: a reconnect without a
                    # preceding stop keeps the manager, and without this the first connect after
                    # a DHCP drift paid the 2.4 s discovery inside its own start_stream budget.
                    # Single-flight, TTL-cached, never blocks, never raises.
                    if prewarm_console_host is not None and self.config.console_ip:
                        prewarm_console_host(self.config.console_ip)
                    if self._client_manager is None:
                        state = get_standby_pool().ensure_spawned(
                            chiaki_path=self.config.chiaki_path,
                            console_ip=self.config.console_ip,
                            disable_video=bool(self._cc_mode),
                            identity_sha256=self.config.chiaki_identity_sha256,
                            identity_size=self.config.chiaki_identity_size,
                        )
                        if first or state == 'spawned':
                            logger.info('Standby prewarm pass: %s', state)
                except Exception as exc:
                    logger.warning('Standby prewarm pass failed: %s', exc)
                first = False
                # 1s ticks keep shutdown responsive; the spawn check itself is a
                # ~1ms toolhelp probe when a standby is already present.
                for _ in range(30):
                    if not getattr(self, '_running', False):
                        return
                    time.sleep(1.0)

        self._standby_prewarm_thread = threading.Thread(
            target=_loop, name='standby-prewarm', daemon=True)
        self._standby_prewarm_thread.start()

    def recover_input_link(self):
        """Restart only Chiaki/input, preserving capture and detector threads.

        Console-side ownership of the prior Remote Play session can outlive the
        local child for a short interval. Retry that transient hand-off with a
        strict wall-clock/attempt budget; capture and detector workers remain live,
        while ``_input_link_ready`` stays false until a newly launched child emits
        its own launch-scoped console readiness marker.
        """
        with self._client_state_lock():
            if not self._running or getattr(self, '_client_stop_requested', False):
                self._input_link_ready = False
                return False
            manager = self._client_manager
            self._client_manager = None
            self._input_link_ready = False
            self._input_link_checked_at = 0.0
        if manager is not None:
            try:
                manager.stop()
            except Exception as exc:
                logger.warning('Input-link client cleanup failed: %s', exc)
        logger.warning('Recovering Chiaki input link in place; capture/detector stay live')

        # The native recovery watchdog is 20 seconds. Keep a margin for the
        # sidecar verdict to cross stdout and use longer proof time on the middle
        # attempt; fast "remote already in use" exits can still reach attempt 3.
        deadline = time.monotonic() + 17.0
        plans = ((0.0, 3.0), (0.75, 9.0), (1.25, 3.0))
        attempts = 0
        for backoff_s, desired_wait_s in plans:
            # [ORION_DISCONNECT_AUDIT 2026-09-19] A teardown makes this recovery moot,
            # and continuing it actively hurts: _launch_remote_play_client() would spawn
            # a fresh Remote Play client while close_remote_play_client() is closing the
            # old one, so the console ends the teardown with a live session it was never
            # told about. Checked at every attempt boundary (this loop is the only place
            # that spends real time) rather than mid-launch, so an in-flight launch still
            # completes and is cleaned up normally by stop().
            if not self._running:
                logger.warning(
                    'Input-link recovery abandoned after %d attempt(s): the orchestrator '
                    'is stopping', attempts)
                self._input_link_ready = False
                return False
            remaining_s = deadline - time.monotonic()
            if remaining_s < backoff_s + 3.0:
                break
            if backoff_s > 0.0:
                logger.warning(
                    'Input-link recovery backoff %.2fs before attempt %d/%d; '
                    'capture/detector remain live',
                    backoff_s, attempts + 1, len(plans))
                time.sleep(backoff_s)
            if not self._running or getattr(self, '_client_stop_requested', False):
                self._input_link_ready = False
                return False
            remaining_s = deadline - time.monotonic()
            if remaining_s < 3.0:
                break
            attempts += 1
            wait_s = min(desired_wait_s, max(3.0, remaining_s - 0.25))
            logger.warning(
                'Input-link recovery attempt %d/%d (readiness_budget=%.2fs)',
                attempts, len(plans), wait_s)
            if self._launch_remote_play_client(wait_timeout_s=wait_s,
                                               console_wake_allowed=False):
                # Defense in depth: never promote process liveness. The manager
                # must still prove this exact launch generation is session-ready.
                if self.input_link_ready():
                    logger.info(
                        'Input-link recovery proved fresh console readiness on attempt %d',
                        attempts)
                    return True
                logger.error(
                    'Input-link recovery launch returned without current readiness proof; '
                    'remaining fail-closed')
                self._input_link_ready = False
                break

        self._input_link_ready = False
        self._input_link_checked_at = time.monotonic()
        logger.error(
            'Input-link recovery exhausted after %d attempt(s); capture/detector remain live '
            'and automation remains disarmed', attempts)
        return False

    def start(self):
        if self._running:
            logger.warning('Orchestrator already running')
            return False
        logger.info(f'Starting Remote Play orchestrator with config: {self.config}')
        if getattr(self, '_xbox_mode', False):
            # External window attach is intentionally distinct from Chiaki's owned
            # process/decoder/input-pipe handshake. No PS5 discovery or subprocess.
            from xbox_remote_play import select_xbox_window
            try:
                self._xbox_window = select_xbox_window(self.config.window_title)
                self._window_handle = self._xbox_window.hwnd
            except Exception as exc:
                self._last_error_msg = str(exc)
                return False
        if self.config.virtual_controller:
            from virtual_controller import check_vigem_status
            ok, err_msg = check_vigem_status()
            if not ok:
                self._last_error_msg = err_msg
                logger.error(err_msg)
                return False
            try:
                from virtual_controller import ViGEmClient
                self._virtual_controller = ViGEmClient()
                if not self._virtual_controller.connect():
                    self._last_error_msg = 'ViGEmBus connection failed.\n\nVerify the driver is running or reinstall from:\nhttps://github.com/nefarius/ViGEmBus/releases'
                    self._virtual_controller = None
                    return False
                logger.info('Virtual controller connected')
            except Exception as e:
                self._last_error_msg = f'Virtual controller initialization failed: {e}'
                self._virtual_controller = None
                return False
        # Beat the connect watchdog: attach the decoder-pipe reader's background reconnect BEFORE
        # launching/awaiting chiaki, so it connects the instant chiaki creates the pipe. chiaki
        # blocks its PS5 session until a reader attaches; the old post-ensure_running attach left
        # it waiting past the launcher's 15s connect watchdog -> restart loop.
        self._preattach_decoder_pipe()
        if self._frame_pipe_required and self._frame_backend is None:
            self._last_error_msg = self._last_error_msg or (
                'Required decoded-frame pipe could not start. Production Remote Play '
                'automation will not use window capture.'
            )
            logger.error(self._last_error_msg)
            if self._virtual_controller:
                try:
                    self._virtual_controller.disconnect()
                except Exception:
                    pass
                self._virtual_controller = None
            return False
        if self.config.auto_launch_client and not getattr(self, '_xbox_mode', False):
            if not self._launch_remote_play_client():
                # A decoder reader may already have been pre-attached. Tear the
                # partial start down even though _running is still false.
                backend, self._frame_backend = self._frame_backend, None
                self._frame_backend_mode = 'capture'
                if backend is not None:
                    try:
                        backend.stop()
                    except Exception:
                        pass
                if self._virtual_controller:
                    try:
                        self._virtual_controller.disconnect()
                    except Exception:
                        pass
                    self._virtual_controller = None
                return False
        # Starting a background reader thread is not video readiness. Production
        # no-card mode must prove that a complete, fresh, valid v2 frame crossed the
        # actual producer/pipe/conversion contract before start() can succeed.
        if self._frame_pipe_required and not self._wait_for_required_frame_pipe():
            logger.error(self._last_error_msg)
            backend, self._frame_backend = self._frame_backend, None
            self._frame_backend_mode = 'capture'
            if backend is not None:
                try:
                    backend.stop()
                except Exception:
                    pass
            if self._virtual_controller:
                try:
                    self._virtual_controller.disconnect()
                except Exception:
                    pass
                self._virtual_controller = None
            return False
        if not self._window_handle:
            self._window_handle = self._find_window(self.config.window_title)
        if not self._window_handle:
            # FIX B (HWND-decouple): chiaki's cold start can surface the window well after the
            # window-wait (~28s observed). If the decoder pipe is already pre-attached, the
            # detection feed reads \\.\pipe\orion_frames INDEPENDENT of the window HWND, and the
            # capture loop tolerates a missing window in decoder mode â€” so a not-yet-visible window
            # must NOT fail the connect or tear the pipe down (that teardown is what starved the C++
            # decoder-stall watchdog -> restart loop -> free-floating chiaki window). Only hard-fail
            # when there is no window AND no decoder pipe (genuinely nothing to capture from).
            # Capture-card mode reads the HDMI device for video, so it needs NO chiaki window at all â€”
            # chiaki runs only for input. Never hard-fail on a missing window here; the capture-card
            # backend is brought up just below (_start_frame_backend). (Without this, a slow chiaki cold
            # start fails the connect before the card backend is even started â€” FLAG A1.)
            _cc_mode = (str(getattr(self.config, 'frame_source', '') or '').lower()
                        in ('capture_card', 'capturecard', 'card')
                        or os.environ.get('ORION_CAPTURE_CARD', '').strip().lower() in ('1', 'true', 'yes', 'on'))
            if self._frame_backend is None and not _cc_mode:
                self._last_error_msg = self._last_error_msg or (
                    'Chiaki stream window not found.\n\n'
                    'Install or start Chiaki/chiaki-ng, finish console registration there, '
                    'then press Connect again or enter the exact Chiaki window title.'
                )
                logger.error(self._last_error_msg)
                return False
            if _cc_mode:
                logger.info('Capture-card mode: proceeding without a chiaki stream window (card is the video source).')
            logger.warning('Stream window not up yet (chiaki cold start) â€” proceeding on the '
                           'pre-attached decoder pipe; the HWND is re-found opportunistically for '
                           'the window-capture fallback only.')
        self._running = True
        # Optional in development, mandatory for production no-card Remote Play.
        # A required pipe failure tears the partial start down; it can never fall
        # through to the occlusion-prone window path.
        backend_started = False
        try:
            backend_started = bool(self._start_frame_backend())
        except Exception as e:
            logger.warning('Frame backend init skipped: %s', e)
        if getattr(self, '_xbox_mode', False):
            # Thread creation is not readiness. Require an actual fresh WGC frame;
            # normal black-frame/geometry/ownership gates still govern automation.
            ready = backend_started and self._frame_backend_mode == 'wgc'
            deadline = time.monotonic() + 3.0
            while ready and not self._frame_backend.is_healthy() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready or not self.input_link_ready():
                self._last_error_msg = 'Xbox capture is not ready. Keep the selected Remote Play window visible and unminimized; WGC capture is required.'
                self.stop()
                return False
        if (self._frame_pipe_required
                and (not backend_started or self._frame_backend_mode != 'decoder')):
            self._last_error_msg = (
                'Required decoded-frame pipe is unavailable; production automation '
                'failed closed instead of using window capture.'
            )
            logger.error(self._last_error_msg)
            self.stop()
            return False
        if self._rtt_engine:
            try:
                self._rtt_engine.start()
                gateway_ip = (self.config.console_ip or '').strip() or get_default_gateway()
                # Seed the RTT PING target ONLY (console/gateway) for initial
                # latency measurement. Do NOT call set_target_ip here: that also
                # records the value as the detected COURT IP, which is exactly
                # why the Network tab showed the console IP as the court IP. The
                # real court IP is locked later via the 'set_court_ip' command
                # once NetworkBridge detects a public game-server peer.
                if hasattr(self._rtt_engine, 'set_ping_target'):
                    self._rtt_engine.set_ping_target(gateway_ip)
                else:
                    try:
                        self._rtt_engine._sampler.target_ip = gateway_ip
                    except Exception:
                        self._rtt_engine.set_target_ip(gateway_ip)
                logger.info(f'RTT sync engine started. Ping target IP: {gateway_ip} (court IP awaits detection)')
            except Exception as e:
                logger.error(f'Failed to start RTT sync engine: {e}')
        self._start_detcsv()
        # Re-arm the frame dump for this capture generation.  stop() tears the writer down and the
        # dump used to stay dead for the life of the process, silently, while DETCSV restarted here
        # and made the session look fully instrumented.  No-op when the dump was never opted in.
        self._start_framedump()
        try:
            self._capture_health_diagnostics.start()
        except Exception as exc:
            # Telemetry must never strand an otherwise healthy video/input start.
            # The failure is visible, and no synchronous fallback is allowed on
            # the capture thread.
            logger.warning('Capture-health diagnostics worker failed to start: %s', exc)
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self._thread = threading.Thread(target=self._processing_loop, daemon=True)
        self._thread.start()
        if self.config.virtual_controller and self._virtual_controller:
            self._input_router_thread = threading.Thread(target=self._input_router_loop, daemon=True)
            self._input_router_thread.start()
        logger.info('Remote Play orchestrator started')
        return True

    def close_remote_play_client(self):
        """The PS5 DISCONNECT handshake, on its own, callable before anything else.

        [ORION_DISCONNECT_AUDIT 2026-09-19] stop() has always put this first *within
        itself*, with an explicit comment about the native's ~5 s
        kSidecarGracefulShutdownMs budget. That reasoning was defeated one level up:
        autogreen_sidecar.py's ``finally:`` joins the preview worker (2.0 s) and the
        preview-stats worker (1.0 s) BEFORE it ever calls stop(), so on a slow preview
        join the WM_CLOSE only went out at t≈3 s and its own 3 s wait ran past the
        native's force-kill at t=5 s. chiaki_session_stop() then never completed and
        the console reported the transport simply vanishing -- the "LAN cable was
        disconnected" error.

        Exposing it separately lets the sidecar spend the FIRST part of the budget on
        the only step with an external, non-recoverable consequence. Idempotent:
        _client_manager is cleared, so stop()'s later call is a no-op.
        """
        # Publish the stopping intent BEFORE the close. recover_input_link() may be
        # running on the session-command worker and would otherwise keep launching a
        # replacement client underneath this teardown. stop() sets the same flag a
        # moment later; setting it here makes the standalone call equally safe.
        with self._client_state_lock():
            self._client_stop_requested = True
            self._running = False
            self._input_link_ready = False
            self._input_link_checked_at = 0.0
            manager = self._client_manager
            self._client_manager = None
        # Detach BEFORE the blocking close: reentrant/parallel teardown must not
        # stop twice or clear a newer object installed in the slot.
        if manager is not None:
            try:
                manager.stop()
            except Exception as exc:
                logger.warning('Chiaki client stop failed: %s', exc)

    def stop(self):
        # A failed start can still own real resources.  In particular, the
        # decoded-frame reader is pre-attached and OrionStream is launched before
        # the required-pipe readiness verdict.  The old early return leaked those
        # partial-start resources when start() failed after either step, leaving a
        # live OrionStream window/pipe owner for the next reconnect.  Treat stop()
        # as idempotent resource cleanup, not merely a running-state transition.
        has_resources = any((
            self._client_manager is not None,
            self._frame_backend is not None,
            self._capture_thread is not None,
            self._thread is not None,
            self._input_router_thread is not None,
            bool(getattr(self, '_framedump_writer', None)
                 and self._framedump_writer.is_alive()),
            bool(getattr(self, '_detcsv', None)
                 and self._detcsv.is_running()),
            bool(getattr(self, '_capture_health_diagnostics', None)
                 and self._capture_health_diagnostics.is_running()),
            self._virtual_controller is not None,
            getattr(self, '_rtt_engine', None) is not None,
        ))
        if not self._running and not has_resources:
            self.close_remote_play_client()
            return
        logger.info('Stopping Remote Play orchestrator%s',
                    '' if self._running else ' (partial start cleanup)')
        self._running = False
        # Wake an idle detector before potentially slow client/backend teardown.
        detector_event = getattr(self, '_detector_frame_ready_evt', None)
        if detector_event is not None:
            detector_event.set()
        self._input_link_ready = False
        self._input_link_checked_at = 0.0
        # GRACEFUL CHIAKI SHUTDOWN GOES FIRST. The client manager now posts WM_CLOSE to the
        # stream window, which runs chiaki_session_stop() -> the Remote Play DISCONNECT
        # handshake. That handshake needs a couple of seconds, and the NATIVE side only gives
        # this whole stop() ~5s (kSidecarGracefulShutdownMs) before force-killing the sidecar â€”
        # so joining the capture/processing threads first (up to 2s each) used to eat the entire
        # budget and the handshake never ran. The console then only saw the transport vanish
        # mid-session: the user-reported "LAN cable was disconnected" error. The loops below
        # already exited on `self._running = False`, and neither touches _client_manager.
        self.close_remote_play_client()
        # An unclaimed standby client (pre-booted, no session) must not outlive the
        # orchestrator: ask it to quit cleanly, then kill. The broad image-name
        # sweep in the client manager stop above and the native job object remain
        # the backstops for anything this misses.
        if get_standby_pool is not None:
            try:
                get_standby_pool().shutdown()
            except Exception as exc:
                logger.debug('Standby pool shutdown failed: %s', exc)
        # Joins are VERIFIED: a thread that outlived its join is still calling into the frame
        # backend / RTT engine, so tearing those down (and nulling them) underneath it is a
        # use-after-free-shaped race (cv2 release during a live read; RTT sampler on a stopped
        # engine). If any loop survived, skip the teardown and leave it to process exit.
        threads_stuck = []
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            if self._capture_thread.is_alive():
                threads_stuck.append('capture')
            else:
                self._capture_thread = None
        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                threads_stuck.append('processing')
            else:
                self._thread = None
        # CSV is independent diagnostic I/O. Drain normally, but do not delay
        # teardown on a blocked disk; the worker alone owns/ultimately closes its file.
        detcsv = getattr(self, '_detcsv', None)
        if detcsv is not None:
            # The old 500 ms budget was not "what the flush needs" -- it was a guess, and the
            # message it printed ("pending rows discarded") is the last minute of a measurement
            # session going missing.  Draining a bounded 256-row deque is sub-millisecond on a
            # healthy disk, so the larger budget costs nothing in the normal case; it is bounded at
            # 5 s because the native parent force-kills the sidecar after kSidecarGracefulShutdownMs
            # and the Chiaki disconnect handshake (already done, above) must not be starved.
            try:
                close_budget = max(0.1, min(5.0, float(
                    os.environ.get('ORION_DETCSV_CLOSE_MS', '2000')) / 1000.0))
            except (TypeError, ValueError):
                close_budget = 2.0
            _snap = detcsv.snapshot()
            if detcsv.close(timeout=close_budget):
                self._detcsv = None
                logger.error('DETCSV drained on shutdown: rows=%d queued_at_stop=%d '
                             'drop_full=%d drop_stopped=%d bytes=%d parts=%d',
                             _snap['accepted'], _snap['queued'], _snap['dropped_full'],
                             _snap.get('dropped_stopped', 0), _snap.get('total_bytes', 0),
                             _snap.get('parts', 1))
            else:
                _lost = detcsv.snapshot()
                logger.error('DETCSV writer still exiting after %.0fms; pending rows discarded '
                             '(rows=%d written=%d queued=%d drop_shutdown=%d)',
                             close_budget * 1000.0, _lost['accepted'], _lost['written'],
                             _lost['queued'], _lost['dropped_shutdown'])
        # Framedump is diagnostic-only and must not drain queued PNG work during shutdown.  Stop it
        # after the processing producer has exited, with a short independent bound.
        if getattr(self, '_framedump_writer', None) is not None:
            self._framedump_close_press_stats('session_end')
            _ring = getattr(self, '_framedump_preroll', None)
            if _ring is not None:
                _ring.clear()       # do not hold this session's frames past its teardown
            self._stop_framedump_writer(timeout=0.5)
        # [ORION_SHOT_RECORDS] Close every open press and flush the corpus file.
        #
        # The recorder itself is deliberately KEPT: stop()/start() also run on an in-process
        # capture restart (a warm-preview -> stream promotion, a reconnect), and retiring the
        # writer here would end the session's corpus at the first promotion exactly the way
        # the frame dump used to end its own -- silently. Its writer is a daemon with a
        # per-record fsync, so an idle one costs nothing.
        _sr = getattr(self, '_shot_records', None)
        if _sr is not None:
            try:
                _sr.close_all('session_end')
                _snap = _sr.snapshot()
                logger.error('SHOT RECORDS: presses=%d written=%d dropped=%d errors=%d '
                             'orphan_oracle=%d orphan_banner=%d -> %s',
                             _snap['presses'], _snap['written'], _snap['dropped'],
                             _snap['errors'], _snap['orphan_oracle'],
                             _snap['orphan_banner'], _sr.path)
            except Exception as exc:
                logger.warning('shot records flush failed: %s', exc)
        # [ORION_SHOT_RANGE 2026-09-17] Drain whatever window the last press left queued, so a
        # session's final shot still gets its reading into the record.  The READER is kept for
        # exactly the reason the recorder is: stop()/start() also run on an in-process capture
        # restart, and retiring it here would silently end the session's range corpus.
        _rng = getattr(self, '_shot_range', None)
        if _rng is not None:
            try:
                _rng._worker_drain()              # noqa: SLF001 -- same-process synchronous drain
                logger.error('SHOT RANGE: presses=%d emitted=%d dropped=%d',
                             _rng.presses, _rng.emitted, _rng.dropped)
            except Exception as exc:
                logger.warning('shot range flush failed: %s', exc)
        if self._input_router_thread:
            self._input_router_thread.join(timeout=2.0)
            if self._input_router_thread.is_alive():
                # Does not touch the frame backend / RTT engine, so it does not gate their
                # teardown â€” but it is still worth knowing it outlived the join.
                logger.warning('Orchestrator stop: input-router loop still running after join')
            else:
                self._input_router_thread = None
        capture_health_sink = getattr(self, '_capture_health_diagnostics', None)
        if capture_health_sink is not None and not capture_health_sink.stop(timeout=2.0):
            # The worker may still hold a reference to and query the frame backend.
            # Gate backend teardown exactly as a stuck capture thread does.
            threads_stuck.append('capture-health-diagnostics')
        if self._virtual_controller:
            try:
                self._virtual_controller.disconnect()
            except Exception as exc:
                # A partially initialized ViGEm object must not prevent the frame backend,
                # client, and RTT resources from being cleaned up.
                logger.warning('Virtual controller disconnect failed during stop: %s', exc)
            self._virtual_controller = None
        if threads_stuck:
            logger.warning('Orchestrator stop: %s loop(s) still running after join; leaving the '
                           'frame backend / RTT engine attached (teardown deferred to process '
                           'exit) so they are not torn down under a live thread',
                           '+'.join(threads_stuck))
        else:
            if self._frame_backend:
                try:
                    self._frame_backend.stop()
                except Exception as exc:
                    logger.debug('Frame backend stop failed: %s', exc)
                self._frame_backend = None
                self._frame_backend_mode = 'capture'
            if hasattr(self, '_rtt_engine') and self._rtt_engine:
                try:
                    self._rtt_engine.stop()
                except Exception as exc:
                    logger.debug('RTT engine stop failed: %s', exc)
                self._rtt_engine = None
        self._window_handle = None
        logger.info('Remote Play orchestrator stopped')

    def _owned_decoder_process_identity(self):
        """Return only the exact live child owned by this orchestrator's manager."""
        manager = getattr(self, '_client_manager', None)
        identity = getattr(manager, 'owned_process_identity', None) \
            if manager is not None else None
        if not callable(identity):
            return {}
        try:
            value = identity()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _preattach_decoder_pipe(self):
        r"""Start the decoder frame-pipe reader EARLY â€” before ensure_running() â€” so it attaches
        the instant chiaki creates \\.\pipe\orion_frames (~11s after launch, post Vulkan init)
        instead of after ensure_running()'s up-to-18s window poll.

        Evidence: chiaki BLOCKS its PS5 session at "waiting for Orion on \\.\pipe\orion_frames"
        until a reader attaches; the old post-ensure_running attach left it waiting past the
        launcher's 15s connect watchdog -> restart loop + a stray parked chiaki window. The
        backend is non-blocking (its own daemon thread retries the connect until the pipe exists),
        so starting it first costs nothing and removes the gap. No-op unless the decoder pipe is
        the source and nothing is attached yet; capture-card/WGC sources still start later in
        _start_frame_backend() once the window is located (they need the HWND)."""
        if getattr(self, '_xbox_mode', False) or self._frame_backend is not None:
            return
        frame_pipe = os.environ.get('CHIAKI_ORION_FRAME_PIPE')
        if not frame_pipe and os.environ.get('ORION_FRAME_PIPE'):
            frame_pipe = r'\\.\pipe\orion_frames'
        if not frame_pipe and self._frame_pipe_required:
            frame_pipe = r'\\.\pipe\orion_frames'
        if not frame_pipe:
            return
        source = str(getattr(self.config, 'frame_source', 'auto') or 'auto').lower()
        if not self._frame_pipe_required and source in ('capture_card', 'capturecard', 'card', 'wgc'):
            return
        if (not self._frame_pipe_required
                and (os.environ.get('ORION_CAPTURE_CARD', '').strip().lower() in ('1', 'true', 'yes', 'on')
                or os.environ.get('ORION_WGC', '').strip().lower() in ('1', 'true', 'yes', 'on'))):
            return
        try:
            from chiaki_backend import OrionFramePipeBackend
            backend = OrionFramePipeBackend(
                frame_pipe,
                expected_producer_path=self.config.chiaki_path,
                expected_producer_size=self.config.chiaki_identity_size,
                expected_producer_sha256=self.config.chiaki_identity_sha256,
                owned_process_identity_provider=self._owned_decoder_process_identity,
            )
            if backend.start():
                self._frame_backend = backend
                self._frame_backend_mode = 'decoder'
                logger.info('Decoder pipe pre-attached early (%s) â€” reader reconnecting in the '
                            'background so chiaki need not wait at "waiting for Orion"', frame_pipe)
            elif self._frame_pipe_required:
                self._last_error_msg = (
                    'Required decoded-frame pipe backend failed to start; window fallback is disabled.')
        except Exception as exc:
            if self._frame_pipe_required:
                self._last_error_msg = (
                    f'Required decoded-frame pipe backend failed: {exc}. '
                    'Window fallback is disabled.')
                logger.error(self._last_error_msg)
            else:
                logger.warning('Early decoder-pipe pre-attach skipped (%s); will attach after client launch', exc)

    def _wait_for_required_frame_pipe(self) -> bool:
        """Wait for an accepted v2 frame, not merely a live reader thread."""
        if not self._frame_pipe_required:
            return True
        backend = self._frame_backend
        waiter = getattr(backend, 'wait_until_ready', None) if backend is not None else None
        if self._frame_backend_mode != 'decoder' or not callable(waiter):
            self._last_error_msg = (
                'Required decoded-frame pipe has no v2 frame-readiness contract; '
                'production automation remains disabled.')
            return False
        try:
            timeout_s = float(os.environ.get('ORION_FRAME_PIPE_READY_TIMEOUT_S', '12') or '12')
        except Exception:
            timeout_s = 12.0
        # Overrides may make tests/development stricter, never leave production
        # waiting indefinitely after the client launch path has returned.
        timeout_s = min(20.0, max(0.1, timeout_s))
        if not bool(waiter(timeout_s)):
            self._last_error_msg = (
                f'Required decoded-frame pipe produced no fresh valid v2 frame within '
                f'{timeout_s:.1f}s; production automation failed closed.')
            return False
        logger.info('Required decoder frame pipe ready (fresh valid v2 frame received)')
        return True

    def _start_frame_backend(self):
        """Bring up the low-latency decoded-frame backend when frame_source
        requests it. Returns True if a decoder backend is producing frames.

        Development retains the legacy non-fatal fallback. When native marks the
        no-card production pipe required, any failure remains non-authoritative
        and ``start()`` tears down instead of permitting window capture."""
        # Idempotent: the decoder pipe may already be pre-attached early (before
        # ensure_running) by _preattach_decoder_pipe() to beat the 15s connect watchdog.
        if self._frame_backend is not None:
            return True
        if getattr(self, '_xbox_mode', False):
            from wgc_backend import WGCCaptureBackend
            from xbox_remote_play import xbox_window_still_matches
            window = self._xbox_window
            if window is None or not xbox_window_still_matches(window):
                return False
            backend = WGCCaptureBackend(window_hwnd=window.hwnd,
                target_validator=lambda: xbox_window_still_matches(window))
            if not backend.start():
                return False  # never fall through to Chiaki, GDI, a monitor or another app
            self._frame_backend = backend
            self._frame_backend_mode = 'wgc'
            logger.info('Xbox Remote Play: explicit window WGC capture; external client owns transport')
            return True
        source = str(getattr(self.config, 'frame_source', 'auto') or 'auto').lower()

        # HDMI capture-card source (PS5 -> card -> PC, read as a cv2 video device).
        # Opt-in via frame_source='capture_card' or ORION_CAPTURE_CARD=1; device index
        # from ORION_CAPTURE_CARD_INDEX (default 0). Cleanest source when present:
        # true 1080p60, no Remote Play / window-capture issues. Non-fatal fallback.
        cc_requested = (source in ('capture_card', 'capturecard', 'card')
                        or os.environ.get('ORION_CAPTURE_CARD', '').strip().lower() in ('1', 'true', 'yes', 'on'))
        if cc_requested:
            try:
                from capture_card_backend import CaptureCardBackend
                idx_env = os.environ.get('ORION_CAPTURE_CARD_INDEX', '').strip()
                configured_idx = int(idx_env) if idx_env.lstrip('-').isdigit() else 0
                self._capture_warm_cache_expected_index = configured_idx
                # ORION_CAPTURE_MJPG=0 -> request YUY2 (4:2:2, full vertical chroma) instead of MJPG
                # (4:2:0). YUY2 preserves the thin meter's colour better per row (sharper fill-top/green
                # tip) at the cost of USB bandwidth; MJPG is the safe 1080p60 default. A/B without rebuild.
                _use_mjpg = os.environ.get('ORION_CAPTURE_MJPG', '1').strip().lower() not in ('0', 'false', 'no', 'off')
                # [ORION_CAPTURE_FPS 2026-09-14] Customer-selectable card refresh rate
                # (native setting capture_card_fps -> ORION_CAPTURE_FPS). CaptureCardBackend
                # derives its CadenceLock, its nominal frame period, its arrival/cadence
                # window sizes and its health floor from this, so it must be the rate the
                # card was actually asked for -- never a hard-coded 60.
                _fps = requested_capture_fps()
                self._requested_capture_fps = _fps
                backend = CaptureCardBackend(
                    device_index=configured_idx,
                    fps=_fps,
                    use_mjpg=_use_mjpg)
                if backend.start():
                    # This check precedes backend publication, frame ingestion,
                    # and sidecar telemetry.  A DirectShow inventory identity
                    # never authorises an MSMF fallback or auto-resolved index.
                    self._guard_capture_latency_route(backend=backend)
                    self._frame_backend = backend
                    self._frame_backend_mode = 'capture_card'
                    logger.info('Frame source: HDMI capture card (requested %dfps)', _fps)
                    return True
            except Exception as exc:
                logger.warning('Capture-card backend error (%s)', exc)
            # EXCLUSIVE: the card is the only valid video source in capture-card mode. Do NOT fall back to
            # WGC / the decoder pipe / window capture below â€” those render the REMOTE-PLAY video, which is
            # exactly the "reverts to remote play" bug. Return without a backend; the capture loop keeps
            # re-attempting the card (idle, never grabbing the chiaki window) until it comes up.
            logger.warning('Capture-card not up yet; staying on capture-card (NO remote-play fallback), '
                           'will keep retrying the card')
            return False

        # Windows Graphics Capture (occlusion-proof window/monitor capture). Opt-in
        # via frame_source='wgc' or ORION_WGC=1 -- takes precedence over the pipe.
        # Targets the located Remote Play window HWND (falls back to title, then to
        # the monitor in ORION_WGC_MONITOR). Non-fatal: window capture if it can't
        # start. Unlike GDI, WGC does not black out when the window is occluded.
        wgc_requested = (not self._frame_pipe_required and (source == 'wgc'
                         or os.environ.get('ORION_WGC', '').strip().lower() in ('1', 'true', 'yes', 'on')))
        if wgc_requested:
            try:
                from wgc_backend import WGCCaptureBackend
                monitor_env = os.environ.get('ORION_WGC_MONITOR', '').strip()
                backend = WGCCaptureBackend(
                    window_name=(self.config.window_title or None),
                    window_hwnd=(self._window_handle or None),
                    monitor_index=(int(monitor_env) if monitor_env.isdigit() else None),
                )
                if backend.start():
                    self._frame_backend = backend
                    self._frame_backend_mode = 'wgc'
                    logger.info('Frame source: WGC (Windows Graphics Capture)')
                    return True
                logger.warning('WGC backend unavailable; falling back to pipe/window capture')
            except Exception as exc:
                logger.warning('WGC backend error (%s); falling back', exc)

        # ATTACH mode: when the live GUI chiaki is exporting decoded frames over its named pipe
        # (ORION_FRAME_PIPE / CHIAKI_ORION_FRAME_PIPE -> chiaki's OrionFrameExport), connect to that
        # pipe instead of spawning a second headless chiaki. This is the path that pairs with the
        # user's display + input hook on one chiaki process. Enabling the env is sufficient -- it
        # works regardless of frame_source (no separate 'decoder' config needed).
        frame_pipe = os.environ.get('CHIAKI_ORION_FRAME_PIPE')
        if not frame_pipe and os.environ.get('ORION_FRAME_PIPE'):
            frame_pipe = r'\\.\pipe\orion_frames'
        if not frame_pipe and self._frame_pipe_required:
            frame_pipe = r'\\.\pipe\orion_frames'
        if frame_pipe:
            try:
                from chiaki_backend import OrionFramePipeBackend
                backend = OrionFramePipeBackend(
                    frame_pipe,
                    expected_producer_path=self.config.chiaki_path,
                    expected_producer_size=self.config.chiaki_identity_size,
                    expected_producer_sha256=self.config.chiaki_identity_sha256,
                    owned_process_identity_provider=self._owned_decoder_process_identity,
                )
                if not backend.start():
                    logger.warning('Decoder frame-pipe backend unavailable%s',
                                   '; window fallback disabled' if self._frame_pipe_required
                                   else '; using window capture')
                    return False
                # Commit immediately: chiaki's export pipe only appears a few seconds after launch (once
                # it is streaming), so don't block here. The backend reconnects in the background. Dev may
                # use GDI until decoded frames arrive; required production mode waits with no authoritative
                # frame instead, so the bot cannot act on an occluded/window-captured image.
                self._frame_backend = backend
                self._frame_backend_mode = 'decoder'
                logger.info('Frame source: decoder (chiaki frame pipe %s%s)', frame_pipe,
                            '; required/no window fallback' if self._frame_pipe_required
                            else '; window capture until frames flow')
                return True
            except Exception as exc:
                logger.warning('Decoder frame-pipe backend error (%s)%s', exc,
                               '; window fallback disabled' if self._frame_pipe_required
                               else '; using window capture')
                return False

        # SPAWN mode (legacy): no live export pipe -> ChiakiBackend launches its own headless chiaki.
        # Only when explicitly requested via frame_source='decoder'.
        if self._frame_pipe_required:
            return False
        if source not in ('decoder',):
            return False
        try:
            from chiaki_backend import ChiakiBackend, load_chiaki_config
        except Exception as exc:
            logger.warning('Decoder frame source unavailable (chiaki_backend import failed: %s); using window capture', exc)
            return False
        try:
            cfg = load_chiaki_config()
            if self.config.console_ip:
                cfg.console_ip = self.config.console_ip
            backend = ChiakiBackend(cfg)
            if not backend.start():
                logger.warning('Decoder backend failed to start; using window capture')
                return False
            # Wait briefly for the first decoded frame before committing.
            deadline = time.perf_counter() + 3.0
            while time.perf_counter() < deadline and self._running:
                if backend.get_frame_nonblocking() is not None:
                    self._frame_backend = backend
                    self._frame_backend_mode = 'decoder'
                    logger.info('Frame source: decoder (Chiaki pipe)')
                    return True
                time.sleep(0.05)
            logger.warning('Decoder backend produced no frame in 3s; using window capture')
            backend.stop()
            return False
        except Exception as exc:
            logger.warning('Decoder backend error (%s); using window capture', exc)
            return False

    def _export_stats(self):
        """``(wire_fps, wire_gap_fps)`` from the decoder backend, or ``(0, 0)``.

        OrionStream v2 assigns callback sequence before FPS gating/readback/the
        latest-wins slot, so gaps include producer-side skips and overwrites as well
        as transport discontinuities. Other backends return zeroes.
        """
        backend = self._frame_backend
        fn = getattr(backend, 'export_stats', None) if backend is not None else None
        if fn is None:
            return (0.0, 0.0)
        try:
            return fn()
        except Exception:
            return (0.0, 0.0)

    def _queue_capture_health_diagnostic(self, suspect):
        """Snapshot capture-owned counters and perform only a one-slot handoff."""
        sink = getattr(self, '_capture_health_diagnostics', None)
        if sink is None or not sink.is_running():
            return False

        try:
            snapshot = _CaptureHealthSnapshot(
                backend=self._frame_backend,
                tier=str(self._last_capture_tier or ''),
                tier_counts=tuple(self._tier_counts.items()),
                unique_frame_fps=int(self._unique_frame_fps),
                duplicate_frame_pct=float(self._duplicate_frame_pct),
                cv_fps=float(self._cv_fps),
                cv_detect_ms=float(self._cv_detect_ms_ema),
                source_sequence_skips=int(self._source_sequence_skips),
                preview_duplicate_refreshes=int(self._preview_duplicate_refreshes),
                core_black_run=int(self._video_core_black_run),
                core_static_run=int(self._video_core_static_run),
                suspect=bool(suspect),
                requested_fps=int(getattr(self, '_requested_capture_fps', CAPTURE_FPS_DEFAULT)
                                  or CAPTURE_FPS_DEFAULT),
            )
            return sink.submit(snapshot)
        except Exception:
            # Diagnostics are observational.  A malformed counter or a worker
            # teardown race must never skip the display callback below this site.
            return False

    def _capture_health_report_due(self, now, suspect):
        """Bound health reports while preserving immediate state transitions.

        A persistent suspect condition used to submit on every captured frame.
        The latest-wins sink bounded memory, but the worker could still format
        and emit warnings at its own throughput.  Emit the first SUSPECT and the
        recovery transition immediately, then cap steady-state reports at 5 s.
        """
        current = bool(suspect)
        previous = bool(getattr(self, '_capture_health_suspect', False))
        self._capture_health_suspect = current
        try:
            elapsed = float(now) - float(self._last_capture_health_log)
        except (TypeError, ValueError, OverflowError, AttributeError):
            elapsed = 5.0
        if current != previous or elapsed >= 5.0:
            self._last_capture_health_log = float(now)
            return True
        return False

    @staticmethod
    def _emit_capture_health_diagnostic(snapshot):
        """Collect backend stats and emit the historical log schema off-loop."""
        backend = snapshot.backend
        exp_fps = 0.0
        gap_fps = 0.0
        export_stats = getattr(backend, 'export_stats', None) if backend is not None else None
        if callable(export_stats):
            try:
                exp_fps, gap_fps = export_stats()
            except Exception:
                exp_fps, gap_fps = 0.0, 0.0

        raw_stats = {}
        cadence_stats = getattr(backend, 'cadence_stats', None) if backend is not None else None
        if callable(cadence_stats):
            try:
                raw_stats = cadence_stats(window_s=5.0) or {}
            except Exception:
                raw_stats = {}
        stage_kind = str(raw_stats.get('stage_kind', '') or '').strip().lower()
        if stage_kind == 'capture':
            stage_kind = 'cap'
        elif stage_kind == 'decoder':
            stage_kind = 'dec'
        elif stage_kind not in ('cap', 'dec'):
            stage_kind = '?'

        # What the DRIVER actually gave us, not what we asked for. capture_card_backend logs this
        # once at open time (logger.info "Capture-card negotiated"), but sidecar INFO is only
        # forwarded to orion_native.log in the shutdown tail -- so the one line that says whether
        # ORION_CAPTURE_MJPG=0 / ORION_DETECTOR_1080P=1 actually TOOK has never been visible during
        # a session. Every capture-format A/B until now was faith-based. This warning IS forwarded,
        # so restate the negotiated mode here, cheaply, on every health tick.
        cap_mode = '?'
        negotiated_mode = getattr(backend, 'negotiated_mode', None) if backend is not None else None
        if callable(negotiated_mode):
            try:
                _nw, _nh, _nfps, _ncc, _nbuf = negotiated_mode()
                cap_mode = '%dx%d@%.0f/%s/buf%.0f' % (_nw, _nh, _nfps, _ncc or '?', _nbuf)
            except Exception:
                cap_mode = '?'

        # FIELD ORDER IS LOAD-BEARING. RemotePlaySession.cpp:2682/:2716 forward every sidecar line
        # to orion_native.log as trimmed.left(300), and the Python logging prefix
        # ("<ts> WARNING RemotePlayOrchestrator: ") eats 56 of those, leaving 244 for the message.
        # This line renders ~360, so the last ~120 characters have ALWAYS been cut: grep the whole
        # 8.6 MB log and "preview_dup_refresh", "core_black_run" and " SUSPECT" appear ZERO times.
        # SUSPECT is the degraded-feed marker -- the single most important token here -- and it has
        # never once been visible. Until the C++ cap is raised, anything worth reading must sit at
        # the FRONT, so the suspect flag and the negotiated mode lead the line.
        # [ORION_STALL_ATTRIB 2026-09-15] gen-2 collections + the worst stall in this window,
        # near the FRONT for the reason the block above gives: the relay trims the message to
        # ~244 characters and this line already renders longer than that. A gen-2 sweep over
        # the frame ring is the textbook cause of an isolated 200 ms consumer hitch, and
        # `stall=` says whether the attributor saw one at all in the same window. The canonical
        # home for gc2 is the sidecar's own `preview_stats` line (it owns callback_gap_ms);
        # that tree is out of scope here, and stall_attributor.stats() exposes the field for
        # a one-line addition there.
        _sa = {}
        if _stall is not None:
            try:
                _sa = _stall.get().stats() or {}
            except Exception:
                _sa = {}
        logger.warning(
            'Capture health:%s cap_mode=%s gc2=%d gcms=%.0f stall=%d/%.0f '
            'cap_req_fps=%d tier=%s tiers=%s uniqfps=%d dup%%=%.0f '
            'export_fps=%.0f gap_fps=%.0f cv_fps=%.0f detect_ms=%.1f '
            'raw_fps=%.1f raw_gap_max_ms=%.1f raw_late=%d '
            'raw_stage=%s:%.2f/%.2f/%.2f '
            'source_skip=%d raw_gap_frame=%d raw_gap_event_ms=%.0f '
            'preview_dup_refresh=%d '
            'core_black_run=%d core_static_run=%d',
            ' SUSPECT' if snapshot.suspect else '',
            cap_mode,
            int(_sa.get('gc2', 0) or 0), float(_sa.get('gc_worst_ms', 0.0) or 0.0),
            int(_sa.get('stalls', 0) or 0), float(_sa.get('worst_gap_ms', 0.0) or 0.0),
            # [ORION_CAPTURE_FPS 2026-09-14] REQUESTED, next to the NEGOTIATED rate carried in
            # cap_mode's `@<fps>` field. A customer who picks 120 on a card that only does 60
            # is otherwise indistinguishable from one running a healthy 60.
            int(snapshot.requested_fps or CAPTURE_FPS_DEFAULT),
            snapshot.tier, dict(snapshot.tier_counts),
            snapshot.unique_frame_fps, snapshot.duplicate_frame_pct,
            exp_fps, gap_fps, snapshot.cv_fps, snapshot.cv_detect_ms,
            float(raw_stats.get('fps', 0.0) or 0.0),
            float(raw_stats.get('max_gap_ms', 0.0) or 0.0),
            int(raw_stats.get('late_gaps', 0) or 0),
            stage_kind,
            float(raw_stats.get('read_block_ms', 0.0) or 0.0),
            float(raw_stats.get('isolate_ms', 0.0) or 0.0),
            float(raw_stats.get('post_ms', 0.0) or 0.0),
            snapshot.source_sequence_skips,
            int(raw_stats.get('worst_gap_frame_number', 0) or 0),
            float(raw_stats.get('worst_gap_event_ns', 0) or 0) / 1e6,
            snapshot.preview_duplicate_refreshes,
            snapshot.core_black_run, snapshot.core_static_run)

    def _replace_latency_authority(self, route_scope: str, reason: str,
                                   *, fence_frames: bool,
                                   restore_cache: bool = False) -> None:
        """Replace, never mutate, every timing authority for a route generation.

        ``restore_cache=False`` is load-bearing for a reconnect/mode change: it
        cannot immediately reload the posterior it is supposed to invalidate.
        The sole restore-enabled caller is the first exact runtime capture-route
        attestation, where native cannot know the negotiated mode before launch.
        Fresh controlled evidence may later repopulate the exact new scope.
        """
        effective_scope = str(route_scope or '')
        config = getattr(self, 'config', None)
        source = str(getattr(
            config, 'frame_source', '') or '').strip().lower()
        # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47. Capture poisoning used to blank the scope
        # here. That is what made the fix at the guard insufficient on its own: an empty scope
        # yields `_default_cache_path('') is None` (no persistence, no restore) AND trips the
        # `not scope` test in latency_estimator_attestation_snapshot, which pins generation 0
        # forever. So blanking the scope was itself a second, independent brick.
        #
        # Poisoning now gates only RESTORE. A scoped estimator with restore_cache=False is exactly
        # the state we want after recovery: it has a real identity (so it can attest and later
        # persist freshly-earned controlled evidence) but it never reloads the posterior we stopped
        # trusting. Decoder revocation keeps its original blank-the-scope semantics untouched --
        # that path removes restore AND persistence by design.
        if source in ('capture_card', 'capturecard', 'card'):
            if bool(getattr(self, '_capture_warm_cache_revoked', False)):
                restore_cache = False
            if bool(getattr(self, '_capture_route_currently_invalid', False)):
                # While the route is ACTIVELY wrong, stay unscoped exactly as before: an empty
                # scope yields no cache path, so a bad route can neither persist into nor attest
                # against the configured route's identity. Revocation still cannot be laundered by
                # calling a different re-key path. This lifts only once `matches` is true again.
                effective_scope = ''
        if (source == 'decoder'
                and bool(getattr(self, '_decoder_warm_cache_revoked', False))):
            effective_scope = ''
        replacement = None
        try:
            from latency_estimator import try_load as _load_latency
            replacement = _load_latency(
                route_scope=effective_scope,
                restore_cache=bool(restore_cache))
        except Exception as exc:
            logger.warning('Latency authority reset failed closed: %s', exc)
        lock = getattr(self, '_latency_route_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._latency_route_lock = lock
        with lock:
            self._latency_controller_attestation_generation = 0
            self._latency_controller_attestation_scope = ''
            self._latency_controller_attestation_route = ''
            if fence_frames:
                # Keep the estimator swap and the detector generation fence in
                # one critical section.  The processing loop takes these locks in
                # the same order when it snapshots an estimator for a frame, so
                # an old completion can update only the discarded old estimator,
                # never the replacement authority.
                self._note_frame_reject(
                    str(reason or 'source_generation_transition'))
            self._latency_estimator = replacement
            self._latency_route_scope_value = effective_scope
            self._latency_estimator_scope_epoch = max(1, int(getattr(
                self, '_latency_estimator_scope_epoch', 1) or 1)) + 1
        logger.warning('Latency authority reset (%s); fresh route evidence required',
                       str(reason or 'route_transition')[:64])

    def _latency_estimator_for_frame(self, integrity_generation: int):
        """Snapshot the authority that belongs to one detector-frame generation.

        A source/mode transition can occur while ``detect()`` is in flight.  By
        joining the frame's integrity generation and estimator under the same
        lock order used by :meth:`_replace_latency_authority`, a stale completion
        either keeps a reference to the discarded old estimator or receives no
        estimator at all.  It can never train the replacement.
        """
        latency_lock = getattr(self, '_latency_route_lock', None)
        integrity_lock = getattr(self, '_frame_integrity_lock', None)
        if latency_lock is None or integrity_lock is None:
            return None
        with latency_lock:
            with integrity_lock:
                if int(integrity_generation) != int(getattr(
                        self, '_frame_integrity_generation', 0) or 0):
                    return None
                return getattr(self, '_latency_estimator', None)

    def _guard_decoder_latency_route(self, backend=None, frame_data=None) -> bool:
        """Bind reusable decoder timing to the exact pipe producer and ORF2 mode."""
        expected_source = str(getattr(
            self.config, 'frame_source', '') or '').strip().lower()
        if expected_source != 'decoder':
            return True
        backend = backend if backend is not None else getattr(self, '_frame_backend', None)
        status_fn = getattr(backend, 'producer_identity_status', None)
        try:
            status = status_fn() if callable(status_fn) else {}
        except Exception:
            status = {}
        state = str(status.get('state', 'unverified') or 'unverified').strip().lower()
        reason = str(status.get('reason', '') or '')

        if frame_data is None:
            if state in ('unverified', 'rejected'):
                verified = False
            else:
                # Pending/disconnected suspends attestation but does not by itself
                # poison a cache; no pixels from that state can enter timing.
                return bool(state == 'verified'
                            and not self._decoder_warm_cache_revoked
                            and getattr(self.config, 'decoder_mode', ''))
        else:
            frame_verified = bool(getattr(
                frame_data, 'producer_identity_verified', False))
            frame_pid = int(getattr(frame_data, 'producer_process_id', 0) or 0)
            frame_generation = int(getattr(
                frame_data, 'producer_launch_generation', 0) or 0)
            verified = bool(
                state == 'verified'
                and frame_verified
                and frame_pid > 0
                and frame_pid == int(status.get('pid', 0) or 0)
                and frame_generation > 0
                and frame_generation == int(status.get('launch_generation', 0) or 0)
            )

        lock = getattr(self, '_decoder_latency_route_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._decoder_latency_route_lock = lock
        with lock:
            if not verified:
                if not self._decoder_warm_cache_revoked:
                    self._decoder_warm_cache_revoked = True
                    self._replace_latency_authority(
                        '', 'decoder_producer_' + (reason or 'unverified'),
                        fence_frames=True)
                return False

            mode = _decoder_wire_mode(
                getattr(frame_data, 'decoder_width', 0),
                getattr(frame_data, 'decoder_height', 0),
                getattr(frame_data, 'decoder_format', -1),
            )
            if not mode:
                if not self._decoder_warm_cache_revoked:
                    self._decoder_warm_cache_revoked = True
                    self._replace_latency_authority(
                        '', 'decoder_wire_mode_missing', fence_frames=True)
                return False
            if self._decoder_warm_cache_revoked:
                return False

            prior_mode = str(getattr(self.config, 'decoder_mode', '') or '')
            if not prior_mode:
                self.config.decoder_mode = mode
                self._rekey_latency_route('decoder_wire_mode_attested')
            elif prior_mode != mode:
                self.config.decoder_mode = mode
                new_scope = _latency_route_scope(self.config)
                self._replace_latency_authority(
                    new_scope, 'decoder_wire_mode_transition', fence_frames=True)
            return True

    def _guard_capture_latency_route(self, backend=None, frame_data=None) -> bool:
        """Validate the actual capture route before timing evidence is used.

        Native scopes persisted calibration with a DirectShow moniker identity,
        while OpenCV can silently use MSMF or resolve a different device index.
        The first mismatch atomically replaces the estimator with an unscoped,
        cold instance.  Revocation is one-way for this process so a later reopen
        cannot resurrect the startup cache; fresh online calibration can still
        converge on the route that is actually delivering frames.
        """
        if not bool(getattr(self, '_cc_mode', False)):
            return True
        lock = getattr(self, '_capture_latency_route_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._capture_latency_route_lock = lock
        with lock:
            try:
                expected = int(getattr(
                    self, '_capture_warm_cache_expected_index', 0))
            except (TypeError, ValueError, OverflowError):
                expected = -1

            if frame_data is not None:
                geometry_matches = False
                geometry_reason = 'frame_geometry_missing'
                try:
                    actual_api = str(getattr(
                        frame_data, 'capture_api', '') or '').strip().upper()
                    actual_index = int(getattr(
                        frame_data, 'capture_device_index', -1))
                    mode_values = (
                        int(getattr(frame_data, 'capture_width', 0) or 0),
                        int(getattr(frame_data, 'capture_height', 0) or 0),
                        float(getattr(frame_data, 'capture_fps', 0.0) or 0.0),
                        str(getattr(frame_data, 'capture_fourcc', '') or '').strip().upper(),
                        float(getattr(frame_data, 'capture_buffer_size', 0.0) or 0.0),
                    )
                    frame = getattr(frame_data, 'frame', None)
                    if isinstance(frame, np.ndarray) and frame.ndim >= 2:
                        frame_height, frame_width = int(frame.shape[0]), int(frame.shape[1])
                        geometry_matches = bool(
                            frame_width == mode_values[0]
                            and frame_height == mode_values[1])
                        geometry_reason = ('' if geometry_matches else
                                           'frame_geometry_stamp_mismatch')
                except (TypeError, ValueError, OverflowError):
                    actual_api, actual_index, mode_values = '', -1, ()
            else:
                geometry_matches = True
                geometry_reason = ''
                route_backend = (backend if backend is not None
                                 else getattr(self, '_frame_backend', None))
                route_fn = getattr(route_backend, 'active_route', None)
                mode_fn = getattr(route_backend, 'negotiated_mode', None)
                try:
                    actual_api, actual_index = route_fn() if callable(route_fn) else ('', -1)
                    actual_api = str(actual_api or '').strip().upper()
                    actual_index = int(actual_index)
                    mode_values = mode_fn() if callable(mode_fn) else ()
                except Exception:
                    actual_api, actual_index, mode_values = '', -1, ()

            actual_mode = _capture_mode_descriptor(mode_values)

            # [ORION_ROUTE_ADOPT 2026-08-17] #47, the half the 08-11 fix could not reach. That fix
            # lets a session recover when the card RETURNS to the configured index -- but on
            # 2026-08-17 the Elgato enumerated at index 1 for the whole boot (configured 0 had no
            # live feed), so "return to 0" was unreachable and the session was timing-dead for its
            # entire life while the backend's own log said it had deliberately resolved the card
            # by NAME at index 1. Our own name-gated resolution was being treated as an untrusted
            # route forever.
            #
            # ADOPT such a resolution, under every condition that keeps this safe:
            #   * DSHOW only, healthy negotiated mode, geometry verified -- the same bar `matches`
            #     itself sets. An MSMF fallback still revokes (the #47 brick-on-MSMF ship blocker
            #     is a DIFFERENT failure and keeps its fail-closed behaviour).
            #   * The backend must attest the index was chosen by CARD-NAME identity
            #     (route_identity_basis() == 'card_name'). A cached index, an unknown-named
            #     device, or a brightness-scan hit carries no identity evidence and still bricks.
            #   * COLD only, permanently: poisoning is set with the adoption, so the persisted
            #     posterior (scoped to a route this process did not open) can never be restored
            #     here -- _replace_latency_authority forces restore_cache=False while poisoned.
            #     Fresh controlled evidence re-earns authority from zero, exactly like the 08-11
            #     recovery path.
            # Adoption mutates the process-local expectation and falls through: `matches` then
            # holds on this and every later frame, and the existing recovered/transition branches
            # do the re-keying with their own reasons.
            if (actual_api == 'DSHOW' and expected >= 0
                    and actual_index >= 0 and actual_index != expected
                    and bool(actual_mode) and geometry_matches):
                basis_backend = (backend if backend is not None
                                 else getattr(self, '_frame_backend', None))
                basis_fn = getattr(basis_backend, 'route_identity_basis', None)
                basis = ''
                try:
                    basis = str(basis_fn()) if callable(basis_fn) else ''
                except Exception:
                    basis = ''
                resolved_route = getattr(basis_backend, 'active_route', None)
                try:
                    backend_api, backend_index = (resolved_route()
                                                  if callable(resolved_route) else ('', -1))
                except Exception:
                    backend_api, backend_index = '', -1
                if (basis == 'card_name'
                        and str(backend_api or '').strip().upper() == 'DSHOW'
                        and int(backend_index) == actual_index):
                    logger.warning(
                        'Capture-card route ADOPTED: configured DirectShow index %d had no live '
                        'feed; the backend resolved the capture card BY NAME at index %d and '
                        'timing continues with COLD calibration there (the persisted posterior '
                        'stays revoked). This replaces the former whole-session timing brick.',
                        expected, actual_index)
                    self._capture_warm_cache_expected_index = actual_index
                    expected = actual_index
                    self._capture_warm_cache_revoked = True
                    # [ORION_SCOPE_IDENTITY 2026-08-18] The route scope derives the device
                    # identity (name + moniker SHA) from ORION_CAPTURE_CARD_INDEX. Follow the
                    # adoption in this process's env so every later scope evaluation keys on the
                    # device timing actually runs on -- otherwise evidence earned on the adopted
                    # card would persist under the CONFIGURED row's identity, which is exactly
                    # the cross-device contamination the scope exists to prevent.
                    os.environ['ORION_CAPTURE_CARD_INDEX'] = str(actual_index)

            prior_mode = str(getattr(self.config, 'capture_mode', '') or '').strip().lower()
            mode_changed = actual_mode != prior_mode
            actual_route = (actual_api, actual_index, actual_mode)
            matches = (actual_api == 'DSHOW' and actual_index == expected
                       and bool(actual_mode) and geometry_matches)
            prior_route = getattr(self, '_capture_latency_active_route', None)
            route_changed = prior_route is not None and actual_route != prior_route
            already_poisoned = bool(getattr(
                self, '_capture_warm_cache_revoked', False))

            # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47. There used to be an unconditional
            # `if already_revoked: ... return False` here, so once the flag was set the `matches`
            # test below was DEAD CODE and the only `return True` in this function was
            # unreachable for the life of the process. A documented, intended DSHOW->MSMF stall
            # recovery on the HD60X therefore permanently disabled measured-latency timing.
            #
            # Poisoning is still honoured -- it forbids reloading the persisted posterior (see
            # _replace_latency_authority) -- but it no longer decides whether the CURRENT route is
            # usable. That question is re-answered from `matches` on every call, so a route that
            # comes back to the exact configured DSHOW/index/mode/geometry re-earns COLD authority
            # in-process, with no restart.
            if matches:
                recovered = bool(getattr(
                    self, '_capture_route_currently_invalid', False))
                self._capture_route_currently_invalid = False
                if recovered and not (mode_changed or route_changed):
                    # The route healed without tripping either transition branch below (same
                    # api/index/mode as the last good observation). Nothing else would re-key the
                    # authority, so do it here or the session stays pinned at generation 0.
                    self._replace_latency_authority(
                        _latency_route_scope(self.config),
                        'capture_route_recovered_cold', fence_frames=True)
                    self._capture_warm_cache_verified = False
                    logger.warning(
                        'Capture-card route recovered to configured DirectShow index %d; '
                        're-earning COLD timing authority (persisted posterior stays revoked)',
                        expected)
                if mode_changed:
                    self.config.capture_mode = actual_mode
                    new_scope = _latency_route_scope(self.config)
                    # Native cannot know OpenCV's negotiated mode before launch.
                    # The first exact DSHOW/index/mode observation may restore its
                    # exact scoped posterior.  Any later mode change is a new live
                    # generation and must start cold.
                    initial_attestation = not prior_mode and prior_route is None
                    self._replace_latency_authority(
                        new_scope,
                        ('capture_negotiated_mode_attested' if initial_attestation
                         else 'capture_negotiated_mode_transition'),
                        fence_frames=True,
                        restore_cache=initial_attestation)
                elif route_changed:
                    # Defensive coverage for a runtime expected-index/inventory
                    # change that still resolves to a valid DSHOW route.
                    self._replace_latency_authority(
                        _latency_route_scope(self.config),
                        'capture_api_or_index_transition', fence_frames=True)
                self._capture_latency_active_route = actual_route
                # Backend properties can pre-load the exact scoped posterior,
                # but only a real immutable FrameData sample proves that its
                # ndarray geometry matches those negotiated properties.  A
                # start/reopen/mode transition therefore remains unverified
                # until the first exact stamped sample arrives.
                if frame_data is None and (mode_changed or route_changed or not bool(
                        getattr(self, '_capture_warm_cache_verified', False))):
                    self._capture_warm_cache_verified = False
                    return False
                self._capture_warm_cache_verified = True
                return True

            # Empty route scope disables both restore and persistence.  This
            # first mismatch is a one-way process-local revocation and a source
            # generation fence, even when no warm estimator happened to load.
            # Publish revocation before waiting for the latency lock so a
            # concurrent controller attestation cannot re-key back to a scope.
            #
            # [ORION_CAPTURE_ROUTE_RETRY 2026-08-11] #47: two flags now. Poisoning
            # (`_capture_warm_cache_revoked`) stays one-way and permanently forbids reloading the
            # persisted posterior. Suppression (`_capture_route_currently_invalid`) is re-evaluated
            # from `matches` on every call, so it lifts when the card returns to the configured
            # route. Set the suppression flag BEFORE taking the latency lock, for the same
            # published-before-waiting reason as the line above.
            # IDEMPOTENCE (load-bearing): this guard runs per frame. The old code relied on the
            # unconditional early-out above to avoid re-entering here on a route that is simply
            # STILL wrong. With the early-out gone, only an actual CHANGE may replace the authority
            # -- otherwise a persistently bad route would swap the estimator and fence the detector
            # on every single frame, and `test_capture_frame_msmf_fallback_revokes_warm_estimator
            # _before_return` (which pins that the cold estimator survives a repeat call) would be
            # right to fail.
            newly_revoked = not already_poisoned
            # [ORION_CAPTURE_ROUTE_RETRY fix-2 2026-08-11] `was_valid` closes a hole the FIRST cut
            # of this commit introduced. `actual_route` is (api, index, mode) -- geometry is NOT in
            # it -- but `matches` also requires geometry_matches. So a frame whose ndarray shape
            # disagrees with its own stamped mode, while api/index/mode all still read configured,
            # arrives here with route_changed AND mode_changed both False; after a poison+recovery
            # cycle newly_revoked is False too, so the reset below was skipped. The estimator then
            # kept the CONFIGURED scope handed to it by recovery while the route was invalid --
            # i.e. a live _cache_path -- so a controlled label formed in that window could persist
            # a CONTAMINATED posterior into the trusted on-disk scope, which a future process
            # restores at startup. Pre-#47 that was impossible: a poisoned process stayed unscoped
            # for life, so there was nothing to persist into. Re-fence on ANY valid->invalid
            # transition, whatever caused it. Idempotence is preserved because was_valid is False
            # while the route is merely STILL wrong.
            was_valid = not bool(getattr(
                self, '_capture_route_currently_invalid', False))
            self._capture_route_currently_invalid = True
            self._capture_warm_cache_revoked = True
            self._capture_warm_cache_verified = False
            if mode_changed:
                self.config.capture_mode = actual_mode
            if newly_revoked or route_changed or mode_changed or was_valid:
                self._replace_latency_authority(
                    '', 'capture_route_mismatch', fence_frames=True)
                logger.warning(
                    'Capture-card warm timing revoked: actual route is not configured '
                    'DirectShow index %d (actual_api=%s actual_index=%d mode=%s); '
                    'continuing with cold live calibration',
                    expected, actual_api or 'UNVERIFIED', actual_index,
                    actual_mode or ('UNVERIFIED/' + geometry_reason
                                    if geometry_reason else 'UNVERIFIED'))
            self._capture_latency_active_route = actual_route
            return False

    def latency_estimator_snapshot(self):
        """Return timing telemetry only after capture warm-route validation.

        The sidecar calls this on every emission, closing the interval between an
        in-place DSHOW->MSMF reopen and delivery of its first stamped frame.
        """
        return self.latency_estimator_attestation_snapshot()[0]

    def latency_estimator_attestation_snapshot(self):
        """Atomically pair timing telemetry with its native neutral-delivery generation.

        A route transition clears the generation under the same lock that replaces the estimator.
        Consequently an in-flight old estimator can echo only its old generation, while a new
        estimator emits zero until native proves the new exact controller route.
        """
        if bool(getattr(self, '_cc_mode', False)):
            backend = getattr(self, '_frame_backend', None)
            if backend is None or str(getattr(
                    self, '_frame_backend_mode', '') or '') != 'capture_card':
                return None, 0, ''
            # A mismatch atomically replaces the estimator with a cold one and clears the token.
            if not self._guard_capture_latency_route(backend=backend):
                lock = getattr(self, '_latency_route_lock', None)
                if lock is None:
                    return getattr(self, '_latency_estimator', None), 0, ''
                with lock:
                    return getattr(self, '_latency_estimator', None), 0, ''
        elif str(getattr(self.config, 'frame_source', '') or '').strip().lower() == 'decoder':
            backend = getattr(self, '_frame_backend', None)
            if (backend is None or str(getattr(
                    self, '_frame_backend_mode', '') or '') != 'decoder'
                    or not self._guard_decoder_latency_route(backend=backend)):
                # Keep any current cold estimator available for live measurement,
                # but never echo the neutral-delivery token while producer/video
                # provenance is pending, disconnected, or unverified.
                lock = getattr(self, '_latency_route_lock', None)
                if lock is None:
                    return getattr(self, '_latency_estimator', None), 0, ''
                with lock:
                    return getattr(self, '_latency_estimator', None), 0, ''
        lock = getattr(self, '_latency_route_lock', None)
        if lock is None:
            return getattr(self, '_latency_estimator', None), 0, ''
        with lock:
            estimator = getattr(self, '_latency_estimator', None)
            scope = str(getattr(self, '_latency_route_scope_value', '') or '')
            attested_scope = str(getattr(
                self, '_latency_controller_attestation_scope', '') or '')
            generation = int(getattr(
                self, '_latency_controller_attestation_generation', 0) or 0)
            route = str(getattr(
                self, '_latency_controller_attestation_route', '') or '').strip().lower()
            source = str(getattr(
                self.config, 'frame_source', '') or '').strip().lower()
            capture_invalid = bool(
                source in ('capture_card', 'capturecard', 'card')
                and (bool(getattr(self, '_capture_route_currently_invalid', False))
                     or not bool(getattr(
                         self, '_capture_warm_cache_verified', False))))
            decoder_invalid = bool(
                source == 'decoder'
                and getattr(self, '_decoder_warm_cache_revoked', False))
            epoch = max(1, int(getattr(
                self, '_latency_estimator_scope_epoch', 1) or 1))
            if (estimator is None or not scope or attested_scope != scope
                    or route not in ('pipe', 'vigem_ds4', 'vigem_xusb')
                    or capture_invalid or decoder_invalid
                    or int(getattr(
                        self, '_latency_controller_highest_attestation_generation', 0) or 0)
                        != generation
                    or str(getattr(
                        self, '_latency_controller_highest_attestation_scope', '') or '')
                        != scope
                    or str(getattr(
                        self, '_latency_controller_highest_attestation_route', '') or '')
                        != route
                    or int(getattr(
                        self, '_latency_controller_highest_attestation_epoch', 0) or 0)
                        != epoch):
                generation = 0
                route = ''
            return estimator, generation, route

    def latency_estimator_scope_epoch(self) -> int:
        """Return the process-monotonic estimator/scope authority epoch."""
        lock = getattr(self, '_latency_route_lock', None)
        if lock is None:
            return 0
        with lock:
            return max(1, int(getattr(
                self, '_latency_estimator_scope_epoch', 1) or 1))

    def _snapshot_actionable_state_locked(self) -> dict:
        """Copy the completed frame's detector/remap values for atomic publication.

        The caller holds ``_frame_integrity_lock``.  Detection writes the legacy
        ``_last_*`` fields before calling ``_finish_processed_frame``; copying them
        here makes that finish call the sole publication point visible to telemetry.
        """
        meter_present = bool(getattr(self, '_last_meter_present', False))
        raw_fed = bool(meter_present and getattr(self, '_last_raw_fed', False))
        structure_verified = bool(
            raw_fed and getattr(self, '_last_gameplay_structure_verified', False))
        structure_epoch = (_parse_pose_arm_token(
            getattr(self, '_last_gameplay_structure_epoch', 0))
            if structure_verified else 0)

        bbox = ()
        try:
            raw_bbox = getattr(self, '_last_meter_bbox', None)
            if raw_bbox is not None and len(raw_bbox) >= 4:
                bbox = tuple(int(raw_bbox[i]) for i in range(4))
        except Exception:
            bbox = ()

        # [ORION_PROOF_DETECTOR_BOX 2026-09-19] see _ProcessedFrameSnapshot.det_bbox. Rides the
        # SAME atomic snapshot as `bbox` so the two rectangles can never come from different
        # frames -- a proof that compared a fresh drawn box against a stale detector box would be
        # exactly the failure this field exists to remove.
        det_bbox = ()
        try:
            raw_det_bbox = getattr(self, '_last_meter_det_bbox', None)
            if (raw_det_bbox is not None and len(raw_det_bbox) >= 4
                    and int(raw_det_bbox[2]) > 0 and int(raw_det_bbox[3]) > 0):
                det_bbox = tuple(int(raw_det_bbox[i]) for i in range(4))
        except Exception:
            det_bbox = ()

        bbox_wh = ()
        try:
            raw_bbox_wh = getattr(self, '_last_meter_bbox_wh', None)
            if raw_bbox_wh is not None and len(raw_bbox_wh) >= 2:
                bbox_wh = (int(raw_bbox_wh[0]), int(raw_bbox_wh[1]))
        except Exception:
            bbox_wh = ()

        green = ()
        raw_green = getattr(self, '_last_green_window', None)
        if isinstance(raw_green, dict):
            try:
                green = tuple((name, float(raw_green.get(name, 0.0) or 0.0))
                              for name in ('start', 'end', 'center', 'width'))
            except Exception:
                green = ()

        shot = ()
        remap = getattr(self, '_remap_engine', None)
        if remap is not None:
            remap_lock = getattr(remap, '_lock', None)

            def _copy_shot():
                current = getattr(remap, '_shot', None)
                if current is None:
                    return ()
                return (
                    ('fill_pct', (float(getattr(current, 'fill_pct', 0.0) or 0.0)
                                  if meter_present else 0.0)),
                    ('confidence', (float(getattr(current, 'confidence', 0.0) or 0.0)
                                    if meter_present else 0.0)),
                    ('rtt_offset_ms', float(getattr(current, 'rtt_offset_ms', 0.0) or 0.0)),
                )

            try:
                if remap_lock is not None:
                    with remap_lock:
                        shot = _copy_shot()
                else:
                    shot = _copy_shot()
            except Exception:
                shot = ()

        # Registration authority must be joined to this exact completed frame.  The
        # predictor writes the legacy dict before _finish_processed_frame(); accepting
        # only an exact sequence match prevents frame N telemetry from ever borrowing
        # the forecast already computed for N+1 on the detector thread.
        tip_registration = ()
        raw_tip_registration = getattr(self, '_last_tip_reg', None)
        if isinstance(raw_tip_registration, dict):
            try:
                if int(raw_tip_registration.get('seq', 0) or 0) == int(
                        getattr(self, '_last_processed_seq', 0) or 0):
                    tip_registration = _freeze_flat_payload(raw_tip_registration)
            except (TypeError, ValueError, OverflowError):
                tip_registration = ()

        return {
            'meter_present': meter_present,
            'raw_fed': raw_fed,
            'gameplay_structure_verified': structure_verified,
            'gameplay_structure_epoch': structure_epoch,
            'stage': str(getattr(self, '_last_meter_stage', '') or ''),
            'bbox': bbox,
            'det_bbox': det_bbox,
            'bbox_wh': bbox_wh,
            'green': green,
            'tracking': _freeze_flat_payload(getattr(self, '_last_meter_track', None)),
            'fusion': _freeze_flat_payload(getattr(self, '_last_release_fusion', None)),
            'shot': shot,
            'tip_registration': tip_registration,
        }

    def _clear_actionable_state_locked(self) -> None:
        """Clear legacy detector state while publishing an unhealthy snapshot.

        The immutable snapshot is authoritative, but zeroing the compatibility fields
        prevents an older sidecar or diagnostic reader from seeing a stale meter after
        a reject.  The caller holds ``_frame_integrity_lock``.
        """
        self._last_meter_present = False
        self._last_raw_fed = False
        self._last_gameplay_structure_verified = False
        self._last_gameplay_structure_epoch = 0
        self._last_meter_stage = ''
        self._last_meter_track = _MeterTrackPayload()
        self._last_release_fusion = None
        self._last_meter_bbox = None
        self._last_meter_det_bbox = None
        self._last_meter_bbox_wh = None
        self._last_green_window = None
        self._last_tip_reg = None
        self._green_show_streak = 0
        self._green_hide_streak = 0

        remap = getattr(self, '_remap_engine', None)
        if remap is None:
            return
        remap_lock = getattr(remap, '_lock', None)

        def _zero_shot():
            current = getattr(remap, '_shot', None)
            if current is not None:
                current.fill_pct = 0.0
                current.confidence = 0.0

        try:
            if remap_lock is not None:
                with remap_lock:
                    _zero_shot()
            else:
                _zero_shot()
        except Exception:
            # The immutable fail-closed snapshot below remains authoritative even if
            # an older/custom remap object does not expose mutable shot fields.
            pass

    def _note_frame_reject(self, reason: str, fail_closed: bool = True) -> None:
        """Fail the bot closed for a suspicious capture without pretending it is fresh."""
        reason = str(reason or 'unknown')
        lock = getattr(self, '_frame_integrity_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._frame_integrity_lock = lock
        with lock:
            self._frame_integrity_counts[reason] = self._frame_integrity_counts.get(reason, 0) + 1
            n = self._frame_integrity_counts[reason]
            if not fail_closed:
                return
            self._frame_integrity_generation = int(
                getattr(self, '_frame_integrity_generation', 0) or 0) + 1
            self._telemetry_revision = int(getattr(self, '_telemetry_revision', 0) or 0) + 1
            self._last_frame_reject_reason = reason
            self._capture_integrity_healthy = False
            self._clear_actionable_state_locked()
            # Preserve the last completed frame identity, but atomically revoke its
            # authority immediately.  A later detector completion publishes a new
            # identity only if its captured integrity generation is still current.
            prior = getattr(self, '_processed_frame_snapshot', None)
            self._processed_frame_snapshot = _ProcessedFrameSnapshot(
                seq=int(getattr(prior, 'seq', getattr(self, '_last_processed_seq', 0)) or 0),
                frame_number=int(getattr(
                    prior, 'frame_number', getattr(self, '_last_processed_frame_number', 0)) or 0),
                frame_ts=float(getattr(
                    prior, 'frame_ts', getattr(self, '_last_processed_frame_ts', 0.0)) or 0.0),
                epoch_ms=float(getattr(
                    prior, 'epoch_ms', getattr(self, '_last_processed_epoch_ms', 0.0)) or 0.0),
                measurement_epoch_ms=float(getattr(
                    prior, 'measurement_epoch_ms',
                    getattr(self, '_last_processed_measurement_epoch_ms', 0.0)) or 0.0),
                pts=int(getattr(prior, 'pts', getattr(self, '_last_processed_pts', 0)) or 0),
                frame_wh=tuple(getattr(
                    prior, 'frame_wh', getattr(self, '_last_processed_frame_wh', (0, 0)))),
                integrity_healthy=False,
                reject_reason=reason,
                reject_counts=tuple(sorted(self._frame_integrity_counts.items())),
                backend_frozen=bool(getattr(prior, 'backend_frozen', False)),
                revision=int(self._telemetry_revision),
            )
        if fail_closed:
            self._set_latency_video_health(False, reason)
        if n == 1 or n % 60 == 0:
            logger.warning('Detector frame rejected: reason=%s count=%d (feed fails closed)', reason, n)
        # Do not wait for the normal telemetry tick to tell native that the eyes are
        # unsafe.  The sidecar's priority event (when installed) also keeps preview
        # stdout work behind this health payload.
        priority_evt = getattr(self, '_telemetry_priority_evt', None)
        if priority_evt is not None:
            priority_evt.set()
        self._frame_ready_evt.set()

    @staticmethod
    def _frame_reject_fails_closed(reason: str) -> bool:
        """Whether one rejected capture invalidates the prior detector lease now.

        Poll repeats and a new-sequence exact-pixel repeat are non-authoritative
        skips: they never run detection or refresh frame/pixel age, but one common
        render hold must not manufacture a meter blink. The prior sample expires
        naturally under the strict age lease if repeats persist. Every malformed,
        dark, torn, stale, or regressing frame still revokes immediately.
        """
        return str(reason or '') not in ('source_poll_repeat', 'pixel_duplicate')

    def _finish_processed_frame(self, seq: int, source_frame_number: int,
                                backend_frozen: bool, success: bool,
                                failure_reason: str = 'detector_exception',
                                frame_ts: float = 0.0, epoch_ms: float = 0.0,
                                measurement_epoch_ms: float = 0.0,
                                pts: int = 0, frame_wh=None,
                                integrity_generation=None) -> None:
        """Publish processing completion only after the detector finished reading the frame.

        Preview uses ``_last_processed_seq`` as a backpressure barrier, so advancing it
        before ``detect()`` returns would let JPEG/base64 work compete with the bot's eyes.
        """
        if not success or backend_frozen:
            self._note_frame_reject(
                'backend_content_frozen' if backend_frozen else failure_reason)
        lock = getattr(self, '_frame_integrity_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._frame_integrity_lock = lock
        with lock:
            current_generation = int(
                getattr(self, '_frame_integrity_generation', 0) or 0)
            frame_generation = (current_generation if integrity_generation is None
                                else int(integrity_generation))
            can_restore = bool(success and not backend_frozen
                               and frame_generation == current_generation)
            self._last_processed_seq = int(seq or 0)
            self._last_processed_frame_number = int(source_frame_number or 0)
            self._last_processed_frame_ts = float(frame_ts or 0.0)
            self._last_processed_epoch_ms = float(epoch_ms or 0.0)
            # Never manufacture timing authority from the raw identity clock.
            # Every production processing path passes the explicit measurement
            # epoch above; a missing value must remain missing/fail-closed.
            self._last_processed_measurement_epoch_ms = float(
                measurement_epoch_ms or 0.0)
            self._last_processed_pts = int(pts or 0)
            if frame_wh is not None:
                self._last_processed_frame_wh = (int(frame_wh[0]), int(frame_wh[1]))
            if can_restore:
                self._capture_integrity_healthy = True
                self._last_frame_reject_reason = ''
                self._telemetry_revision = int(
                    getattr(self, '_telemetry_revision', 0) or 0) + 1
                actionable = self._snapshot_actionable_state_locked()
            else:
                # A stale detector completion may have rewritten the legacy globals
                # after a newer capture reject cleared them.  Revoke those writes again;
                # the unhealthy snapshot below intentionally carries no actionable state.
                self._clear_actionable_state_locked()
                actionable = {}
            self._processed_frame_snapshot = _ProcessedFrameSnapshot(
                seq=self._last_processed_seq,
                frame_number=self._last_processed_frame_number,
                frame_ts=self._last_processed_frame_ts,
                epoch_ms=self._last_processed_epoch_ms,
                measurement_epoch_ms=self._last_processed_measurement_epoch_ms,
                pts=self._last_processed_pts,
                frame_wh=tuple(self._last_processed_frame_wh),
                integrity_healthy=bool(self._capture_integrity_healthy),
                reject_reason=str(self._last_frame_reject_reason or ''),
                reject_counts=tuple(sorted(self._frame_integrity_counts.items())),
                backend_frozen=bool(backend_frozen),
                revision=int(self._telemetry_revision),
                **actionable,
            )
        if can_restore:
            self._set_latency_video_health(True, '')
        if not can_restore:
            # _note_frame_reject already signalled the failure.  A generation mismatch
            # is an older successful completion losing to a newer reject, so it must
            # publish unhealthy metadata but must not invent a second rejection.
            priority_evt = getattr(self, '_telemetry_priority_evt', None)
            if priority_evt is not None:
                priority_evt.set()
            self._frame_ready_evt.set()
            return
        priority_evt = getattr(self, '_telemetry_priority_evt', None)
        if priority_evt is not None:
            priority_evt.set()
        self._frame_ready_evt.set()

    def _set_latency_video_health(self, healthy: bool, reason: str = '') -> bool:
        """Attest/suspend restored timing from frame health only; never create shot evidence."""
        estimator = getattr(self, '_latency_estimator', None)
        setter = getattr(estimator, 'set_restored_route_attested', None)
        if not callable(setter):
            return False
        if healthy:
            expected_source = str(getattr(self.config, 'frame_source', '') or '').strip().lower()
            active_source = str(getattr(self, '_frame_backend_mode', '') or '').strip().lower()
            if expected_source in ('capturecard', 'card'):
                expected_source = 'capture_card'
            if expected_source not in ('capture_card', 'decoder') or active_source != expected_source:
                setter(False, 'source_backend_mismatch')
                return False
            if expected_source == 'capture_card' and not self._guard_capture_latency_route(
                    backend=getattr(self, '_frame_backend', None)):
                setter(False, 'capture_route_unverified')
                return False
            if expected_source == 'decoder' and not self._guard_decoder_latency_route(
                    backend=getattr(self, '_frame_backend', None)):
                setter(False, 'decoder_route_unverified')
                return False
        return bool(setter(bool(healthy), reason or 'capture_unhealthy'))

    def _revoke_restored_latency_on_source_transition(self, reason: str) -> None:
        # A generation change invalidates fresh online labels just as surely as a
        # restored posterior. Replacing the estimator also clears pending release/
        # probe state; restore_cache=False prevents the old scope from bouncing
        # straight back in. Keep the exact route scope so new controlled evidence
        # may persist only after being relearned on this generation.
        scope = str(getattr(self, '_latency_route_scope_value', '') or '')
        if bool(getattr(self, '_decoder_warm_cache_revoked', False)):
            scope = ''
        self._replace_latency_authority(
            scope, str(reason or 'source_transition'), fence_frames=True)

    def _prepare_detector_frame(self, frame, trusted_isolated: bool = False):
        reason = _detector_frame_contract_reason(
            frame,
            min_width=self._detector_min_width,
            min_height=self._detector_min_height,
            aspect_ratio=16.0 / 9.0,
            aspect_tolerance=self._detector_aspect_tolerance,
        )
        if reason:
            return None, reason
        owned, reason = _isolate_detector_frame(
            frame, trusted_isolated=trusted_isolated)
        if reason:
            return None, reason
        # The detector has one geometry contract regardless of whether a healthy
        # source negotiated 720p or 1080p.  This is a uniform scale (plus at most a
        # centered aspect crop), never the independent-axis stretch that made meter
        # width/height and court position source-dependent.
        # [ORION_DETECTOR_1080P 2026-08-11] The contract SIZE is now selectable (default 1280x720,
        # unchanged); the one-size-for-every-source property this comment protects is untouched.
        return _normalize_detector_frame(
            owned, width=_DETECTOR_CONTRACT_W, height=_DETECTOR_CONTRACT_H)

    def _source_frame_reason(self, frame, source_identity: int, frame_number: int,
                             timestamp_ns: int, now_s: float) -> str:
        """Reject repeated/regressing/stale source frames and exact pixel duplicates.

        A nonblocking decoder backend legitimately returns its cached FrameData between
        arrivals; that is classified ``source_poll_repeat`` and never reaches detection.
        It also never refreshes pixel age.  New source sequence numbers still undergo an
        exact full-frame equality test so a driver re-serving frozen pixels cannot masquerade
        as a new detector sample merely by incrementing a software counter.
        """
        source_identity = int(source_identity or 0)
        frame_number = int(frame_number or 0)
        timestamp_ns = int(timestamp_ns or 0)
        if source_identity != self._source_identity:
            if self._source_identity != 0:
                self._revoke_restored_latency_on_source_transition(
                    'decoder_or_capture_source_generation_transition')
            self._source_identity = source_identity
            self._source_frame_number = 0
            self._source_timestamp_ns = 0
            # Do not compare a new device/session's first frame against pixels from
            # the previous source.  The old bundle remains safe for any in-flight
            # detector call because it owns an immutable array.
            self._last_frame = None
            self._last_frame_hash = None

        sequence_skip = 0
        if frame_number > 0 and self._source_frame_number > 0:
            if frame_number == self._source_frame_number:
                return 'source_poll_repeat'
            if frame_number < self._source_frame_number:
                return 'source_sequence_regression'
            if frame_number > self._source_frame_number + 1:
                sequence_skip = frame_number - self._source_frame_number - 1

        if timestamp_ns > 0:
            if self._source_timestamp_ns > 0 and timestamp_ns <= self._source_timestamp_ns:
                return 'source_timestamp_regression'
            age_ms = (float(now_s) * 1e9 - timestamp_ns) / 1e6
            if age_ms > self._max_detector_frame_age_ms:
                return 'source_timestamp_stale'
            # CadenceLock can legitimately lead a raw read by a few milliseconds;
            # anything >20ms into the future is a corrupt/mixed clock-domain stamp.
            if age_ms < -20.0:
                return 'source_timestamp_future'

        # Record the observed source progress even when its pixels are an exact
        # duplicate.  Otherwise the next genuinely fresh frame would look like a
        # sequence jump from an artificially old sample.
        if frame_number > 0:
            self._source_sequence_skips += sequence_skip
            self._source_frame_number = frame_number
        if timestamp_ns > 0:
            self._source_timestamp_ns = timestamp_ns

        if _frames_identical(frame, self._last_frame):
            return 'pixel_duplicate'
        return ''

    def _source_pts_reason(self, pts: int, epoch_ns: int, source_identity: int) -> str:
        """Validate decoded-frame PTS ORDERING before detector publication.

        This deliberately does NOT derive staleness from ``epoch_ns - pts``.  The
        decoder PTS is a synthetic nominal-rate counter (chiaki advances it by a
        fixed 1e6/max_fps step per received frame), so against a real 59.94 fps
        PS5 that difference drifts linearly -- measured 0.910 ms/s on the live
        producer log.  The former backlog test compared it to a ``min()`` floor,
        which can only ratchet DOWN and so never absorbs the drift: ~55 s into a
        session it crossed the budget and then rejected EVERY frame until the
        source generation changed.  That was a duplicate of the same latch in
        OrionFramePipeBackend._wire_pts_reason and killed the feed the same way.

        Freshness is already enforced ahead of this call by _source_frame_reason,
        which ages frames against a real clock (`source_timestamp_stale`), so
        nothing here needs to re-derive it and genuinely stale frames still fail
        closed.
        """
        pts = int(pts or 0)
        if pts <= 0:
            return ''  # capture-card/window sources do not carry decoder PTS
        source_identity = int(source_identity or 0)
        if source_identity != int(getattr(self, '_pts_source_identity', 0) or 0):
            self._pts_source_identity = source_identity
            self._pts_source_last = 0
            # The connection generation is authoritative, so reset both mapping
            # domains before their next first-sample seed.
            self._pts_to_wall_offset = 0.0
            self._pts_to_epoch_ms = 0.0
        if self._pts_source_last > 0 and pts <= self._pts_source_last:
            return 'decoder_pts_regression'
        if int(epoch_ns or 0) <= 0:
            return 'decoder_pts_clock_missing'
        self._pts_source_last = pts
        return ''

    def _backend_source_identity(self, backend, source_generation: int = 0) -> int:
        """Return a monotonic identity for backend object + connection generation.

        The decoded-frame backend reconnects in place, so object identity alone is
        insufficient: a new Chiaki process/session must reset source seq/timestamp
        and pixel-comparison state even though ``backend is old_backend``.
        """
        if backend is None:
            return 0
        source_generation = int(source_generation or 0)
        if (backend is not getattr(self, '_backend_identity_ref', None)
                or source_generation != int(getattr(
                    self, '_backend_identity_source_generation', 0) or 0)):
            self._backend_identity_ref = backend
            self._backend_identity_source_generation = source_generation
            self._backend_identity_generation = int(
                getattr(self, '_backend_identity_generation', 0) or 0) + 1
        return int(self._backend_identity_generation)

    @staticmethod
    def _backend_is_event_driven(mode: str, backend) -> bool:
        """Whether capture can wait directly for the producer's next frame."""
        return (str(mode or '').lower() in ('capture_card', 'decoder', 'wgc')
                and callable(getattr(backend, 'get_frame', None)))

    @staticmethod
    def _frame_publication_age_ms(frame_data) -> float:
        """Return bounded capture-to-ring-publication delay for diagnostics only.

        Missing, reversed, or implausibly old clocks return zero. In particular,
        this helper never substitutes ``time.perf_counter_ns()``: a delayed callback
        must remain visible and must never manufacture capture freshness.
        """
        try:
            capture_ns = int(getattr(frame_data, 'capture_timestamp_ns', 0) or 0)
            if capture_ns <= 0:
                capture_ns = int(getattr(frame_data, 'timestamp_ns', 0) or 0)
            publication_ns = int(
                getattr(frame_data, 'publication_timestamp_ns', 0) or 0)
            if capture_ns <= 0 or publication_ns < capture_ns:
                return 0.0
            age_ms = (publication_ns - capture_ns) / 1e6
            return age_ms if 0.0 <= age_ms <= 60_000.0 else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def capture_publication_age_ms(self) -> float:
        """Latest validated FrameData publication delay; observability only."""
        try:
            value = float(self._last_capture_publication_age_ms or 0.0)
            return value if 0.0 <= value <= 60_000.0 else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _capture_decoded_frame(self, block=False):
        """Pull the latest decoded BGR frame from the decoder backend, or None.
        Returns (frame, pts, epoch_ns, mono_ns): pts is the decoder presentation timestamp
        (0 if unavailable); epoch_ns is the backend's capture-instant epoch stamp (0 if
        unavailable) â€” the A0 unified timebase every timing consumer (reader velocity, tip-reg,
        latency oracle) runs on; mono_ns is the backend's perf_counter_ns capture stamp
        (0 if unavailable). For the capture card that mono stamp is now cadence-locked (PTS-
        aligned), so using it for frame_age removes read-return jitter from release timing.

        block=True waits on the ring event for the NEXT fresh frame instead of
        polling. Capture-card and decoded Remote Play both use it, removing poll
        phase lag and preventing a timeout from re-publishing a cached frame."""
        backend = self._frame_backend
        self._pending_y_plane = None
        self._pending_frame_isolated = False
        self._pending_source_frame_number = 0
        self._pending_source_identity = 0
        self._pending_backend_frozen = False
        if backend is None:
            return None, 0, 0, 0
        try:
            if block and hasattr(backend, 'get_frame'):
                fd = backend.get_frame(timeout=0.02)
            else:
                fd = backend.get_frame_nonblocking()
            if fd is None:
                return None, 0, 0, 0
            if str(getattr(self, '_frame_backend_mode', '') or '') == 'capture_card':
                # Check the stamp on the exact sample before its pixels can enter
                # detection or the latency estimator.  Missing fields are the
                # fail-closed legacy/test-double default.
                self._guard_capture_latency_route(
                    backend=backend, frame_data=fd)
            elif str(getattr(self, '_frame_backend_mode', '') or '') == 'decoder':
                # This occurs before pixels, PTS, or source generation can enter
                # detection/timing. Unverified frames remain usable for cold live
                # operation only after the estimator has been made unscoped.
                self._guard_decoder_latency_route(
                    backend=backend, frame_data=fd)
            frame = getattr(fd, 'frame', None)
            if frame is None or getattr(frame, 'size', 0) <= 0:
                return None, 0, 0, 0
            # Native Y pass-through (Tier-1 #5): keep the decoder's zero-copy luma plane
            # with this frame so the compressed reader can skip its BGR2GRAY round trip.
            self._pending_y_plane = getattr(fd, 'y_plane', None)
            self._pending_frame_isolated = bool(getattr(fd, 'integrity_isolated', False))
            self._pending_source_frame_number = int(getattr(fd, 'frame_number', 0) or 0)
            self._pending_source_identity = self._backend_source_identity(
                backend, int(getattr(fd, 'source_generation', 0) or 0))
            self._pending_backend_frozen = bool(getattr(fd, 'feed_frozen', False))
            pts = getattr(fd, 'pts', 0) or 0
            # The explicit capture fields are the canonical source clock. Legacy
            # producers remain compatible through the old aliases, but publication
            # time is never allowed to replace capture time or timing authority.
            epoch_ns = int(getattr(fd, 'capture_epoch_ns', 0) or 0)
            if epoch_ns <= 0:
                epoch_ns = int(getattr(fd, 'epoch_ns', 0) or 0)
            mono_ns = int(getattr(fd, 'capture_timestamp_ns', 0) or 0)
            if mono_ns <= 0:
                mono_ns = int(getattr(fd, 'timestamp_ns', 0) or 0)
            self._last_capture_publication_age_ms = \
                self._frame_publication_age_ms(fd)
            # Stash the decoder's monotonic frame number so the display forward can DEDUP (skip
            # re-forwarding the same decoded frame). Display-only: detection still uses every img.
            self._last_decoded_frame_number = self._pending_source_frame_number
            # Phase-1: propagate the capture card's feed_frozen flag so the sidecar
            # telemetry can emit feed_healthy=false for the native engine.
            self._last_feed_frozen = self._pending_backend_frozen
            return frame, pts, epoch_ns, mono_ns
        except Exception as exc:
            self._note_frame_reject('backend_frame_exception')
            logger.debug('Frame backend delivery rejected: %s', exc)
            return None, 0, 0, 0

    @staticmethod
    def _frame_is_probably_black(img):
        """A captured frame is 'bad' when it is mostly black. Chiaki's GPU surface
        returns a valid-sized but all-zero bitmap through window-DC BitBlt (fully
        black), and an occluded/partly-composited grab can return a frame that is
        black over a large band with game content elsewhere (partial black). Both
        must be rejected so the tier chain falls through instead of feeding the
        detector a half-blank frame."""
        if img is None or getattr(img, 'size', 0) <= 0:
            return True
        try:
            h, w = img.shape[:2]
            step_y = max(1, h // 80)
            step_x = max(1, w // 80)
            sample = img[::step_y, ::step_x]
            # Fully black: uniformly dark + noise-free.
            if float(np.mean(sample)) < 3.0 and float(np.std(sample)) < 4.0:
                return True
            # Partial black: a large fraction of the frame is near-zero (e.g. a
            # black band over the game). A real gameplay frame has very few pure
            # black pixels; >55% near-black means the grab is corrupt/incomplete.
            near_black = (np.max(sample, axis=2) < 12) if sample.ndim == 3 else (sample < 12)
            if float(np.count_nonzero(near_black)) / float(near_black.size) > 0.55:
                return True
            return False
        except Exception:
            return False

    @staticmethod
    def _frame_keeps_pipeline_alive(img, from_backend, is_black):
        """Liveness policy for one captured frame â€” does it PROVE the capture pipeline is alive?

        A frame DELIVERED by an active frame backend (capture card / decoder pipe / WGC) proves the
        pipeline is alive even when the content is fully black: the backend reader put a fresh frame
        this instant, and black is a legitimate transient â€” a loading screen, a fade-to-black, or a
        brief PS5-network / HDMI blip that makes the Elgato emit black â€” NOT a dead feed. Such a
        frame MUST keep the freshness clock current, so a few seconds of black can't climb frame-age
        past the native 8s frame-stall watchdog and restart the whole sidecar mid-session (the
        "capture dies + respawns after a few shots" bug). A genuinely dead backend delivers NOTHING
        (get_frame -> None), which returns False here and self-heals via the backend's is_healthy()
        in-process re-open long before the native watchdog. For WINDOW capture (no backend) a black
        grab means the BitBlt itself failed (window hidden/occluded/GPU surface lost) = a real
        capture-down signal, so black must NOT refresh the clock there."""
        if img is None:
            return False
        if not is_black:
            return True
        return bool(from_backend)

    @staticmethod
    def _capture_window_gdi(hwnd):
        import win32gui
        import win32ui

        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        try:
            save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(save_bitmap)
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
            bmpinfo = save_bitmap.GetInfo()
            bmpstr = save_bitmap.GetBitmapBits(True)
            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img.shape = (bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        finally:
            win32gui.DeleteObject(save_bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

    @staticmethod
    def _capture_window_printwindow(hwnd):
        """Capture via PrintWindow with PW_RENDERFULLCONTENT, which asks the DWM
        to render the window's full (incl. GPU/DirectComposition) content into a
        bitmap. Works even when the window is occluded, unlike the screen-region
        fallback, and unlike plain window-DC BitBlt on accelerated surfaces."""
        import win32gui
        import win32ui

        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        try:
            save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(save_bitmap)
            # PW_RENDERFULLCONTENT=0x2 ONLY. We deliberately do NOT OR in
            # PW_CLIENTONLY (0x1): the bitmap is sized to the full WINDOW rect,
            # so a client-only render leaves the non-client border region of the
            # bitmap uninitialized -> random colored/shaded garbage (the "quality
            # preset artifacts"). Rendering the full window fills every pixel.
            result = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0x2)
            if not result:
                return None
            bmpinfo = save_bitmap.GetInfo()
            bmpstr = save_bitmap.GetBitmapBits(True)
            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img.shape = (bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception:
            return None
        finally:
            win32gui.DeleteObject(save_bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

    @staticmethod
    def _capture_window_screen_region(hwnd):
        """Capture the composited screen pixels occupied by the Chiaki window.

        Chiaki renders its stream through an accelerated surface, so normal
        window-DC BitBlt can return a valid-sized but fully black bitmap. The
        desktop DC fallback captures the already-composited window rectangle,
        which is what the user actually sees inside Orion after embedding.
        Requires the window to be unoccluded on screen.
        """
        import win32gui
        import win32ui
        import win32api

        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        # Never read desktop pixels through the screen DC: only a genuinely
        # on-screen, non-minimized window is safe to capture this way. If the
        # window is hidden, minimized, or positioned off-screen, bail so this
        # tier can't leak the Windows desktop into the live capture.
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
        vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        if right <= vx or bottom <= vy or left >= vx + vw or top >= vy + vh:
            return None
        screen_dc = win32gui.GetDC(0)
        mfc_dc = win32ui.CreateDCFromHandle(screen_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        try:
            save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(save_bitmap)
            rop = win32con.SRCCOPY | getattr(win32con, 'CAPTUREBLT', 0)
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (left, top), rop)
            bmpinfo = save_bitmap.GetInfo()
            bmpstr = save_bitmap.GetBitmapBits(True)
            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img.shape = (bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        finally:
            win32gui.DeleteObject(save_bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(0, screen_dc)

    @staticmethod
    def _capture_window(hwnd):
        """Backward-compatible wrapper returning just the frame (see
        _capture_window_tier for the diagnostic tier attribution)."""
        return RemotePlayOrchestrator._capture_window_tier(hwnd)[0]

    @staticmethod
    def _capture_window_tier(hwnd):
        """Tiered capture: window-DC BitBlt (cheapest) -> PrintWindow full
        content (handles GPU/occluded) -> desktop-DC CAPTUREBLT (composited).
        Returns (frame, tier) where tier is the path that produced the accepted
        frame: 'gdi' | 'printwindow' | 'screen_region', or '<name>_blackfallback'
        when every tier was black and we returned a sized frame anyway, or 'none'.

        The tier is a DIAGNOSTIC: if 'printwindow'/'gdi' dominate during live
        roi_not_found runs, the accelerated stream surface is being captured
        stale/black while the composited 'screen_region' tier (what an OBS recording
        sees) would carry the meter. Returns the first non-black tier, else the last
        non-None result so the pipeline still gets sized frames.

        This recovers from the Jun-1 regression where the GDI-only path returned
        all-black bitmaps for Chiaki's accelerated stream surface."""
        gdi = RemotePlayOrchestrator._capture_window_gdi(hwnd)
        if gdi is not None and not RemotePlayOrchestrator._frame_is_probably_black(gdi):
            return gdi, 'gdi'
        try:
            pw = RemotePlayOrchestrator._capture_window_printwindow(hwnd)
        except Exception:
            pw = None
        if pw is not None and not RemotePlayOrchestrator._frame_is_probably_black(pw):
            return pw, 'printwindow'
        try:
            region = RemotePlayOrchestrator._capture_window_screen_region(hwnd)
        except Exception:
            region = None
        if region is not None and not RemotePlayOrchestrator._frame_is_probably_black(region):
            return region, 'screen_region'
        # Every tier came back black/None: still hand back a sized frame so the
        # pipeline keeps running. (Plain `or` is ambiguous for numpy arrays.)
        for candidate, name in ((region, 'screen_region'), (pw, 'printwindow'), (gdi, 'gdi')):
            if candidate is not None:
                return candidate, name + '_blackfallback'
        return None, 'none'

    @staticmethod
    def _video_region_status(frame, hash_fn=None):
        """Liveness of the VIDEO CORE (centre) of a captured frame, kept SEPARATE
        from the whole-frame black/hash signals. The captured Chiaki window is mostly
        the stream, so the meter lives in the central game area while the outer band
        can be letterbox/border/UI; a dead core with a live outer band is the
        capture-feeds-a-meterless-frame signature. The core is derived PROPORTIONALLY
        from the frame (not a hardcoded crop) so it tracks whatever size the capture
        is. Returns (core_black, core_hash). NOTE: a static core is NOT itself a
        failure (an idle scene is legitimately static) â€” the caller only treats it as
        suspicious when the WHOLE frame is changing while the core is not, or the core
        is black, or the detector has a sustained roi_not_found run in gameplay."""
        if frame is None or getattr(frame, 'size', 0) <= 0:
            return True, None
        try:
            h, w = frame.shape[:2]
            core = frame[int(h * 0.12):int(h * 0.88), int(w * 0.12):int(w * 0.88)]
            if core.size <= 0:
                core = frame
            black = RemotePlayOrchestrator._frame_is_probably_black(core)
            ch = (hash_fn or RemotePlayOrchestrator._frame_hash)(core)
            return black, ch
        except Exception:
            return False, None

    def _find_window(self, title):
        if CLIENT_AVAILABLE and find_remote_play_window:
            hwnd, window_title = find_remote_play_window(title)
            if hwnd:
                logger.info('Remote Play window selected: %s', window_title)
                return hwnd
        try:
            import win32gui
            targets = []
            raw = [
                title,
                'Orion Stream',
                'Chiaki',
                'chiaki-ng',
            ]
            for item in raw:
                item = str(item or '').strip()
                if item and item.lower() not in {x.lower() for x in targets}:
                    targets.append(item)
            target_lowers = [x.lower() for x in targets]
            # 'orion stream' must be SAFE before the generic 'orion' reject below,
            # or the renamed OrionStream window can never be matched by title.
            safe_markers = ('chiaki', 'orion stream')
            reject_markers = (
                'orion',
                'visual studio code',
                'powershell',
                'command prompt',
                'windows terminal',
                'google chrome',
                'microsoft edge',
                'mozilla firefox',
                'desktop',
            )
            found = []

            def callback(hwnd, ctx):
                if win32gui.IsWindowVisible(hwnd):
                    window_title = win32gui.GetWindowText(hwnd)
                    lower = window_title.lower()
                    if not lower:
                        return True
                    if any(marker in lower for marker in reject_markers) and not any(marker in lower for marker in safe_markers):
                        return True
                    for idx, target in enumerate(target_lowers):
                        if target and target in lower:
                            score = 0 if lower == target else 10 + idx
                            if any(marker in lower for marker in safe_markers):
                                score -= 5
                            found.append((score, hwnd, window_title))
                            break
                return True
            try:
                win32gui.EnumWindows(callback, None)
            except Exception:
                pass
            if not found:
                return None
            found.sort(key=lambda item: item[0])
            logger.info('Remote Play window selected: %s', found[0][2])
            return found[0][1]
        except ImportError:
            logger.warning('win32gui not available')
            return None
        except Exception as e:
            logger.error(f'Error finding window: {e}')
            return None

    def _capture_loop(self):
        import win32gui
        # Raise the Windows timer resolution to 1ms so the pacing sleeps below are
        # accurate. At the default ~15.6ms resolution, sub-frame sleeps overshoot
        # and the loop stutters; 1ms makes frame cadence smooth (less jitter).
        _winmm = None
        try:
            import ctypes
            _winmm = ctypes.windll.winmm
            _winmm.timeBeginPeriod(1)
        except Exception:
            _winmm = None
        # [ORION_STALL_ATTRIB 2026-09-15] Start the sampler HERE, right after the 1 ms timer
        # resolution is raised: at Windows' default ~15.6 ms tick a 5 ms sampler period
        # silently becomes 15.6 ms and the ring covers three times less than it claims.
        # start() is a no-op when ORION_STALL_ATTRIB=0 and when it is already running.
        if _stall is not None:
            try:
                _stall.get().start()
            except Exception as _sa_exc:
                logger.debug('stall attributor not started: %r', _sa_exc)
        last_refind = 0.0
        next_capture = time.perf_counter()
        while self._running:
            try:
                # Prefer the decoder backend (lowest latency, artifact-free).
                # Development may use tiered window capture only when no backend
                # is selected; production no-card mode forbids that authority.
                _frame_pts = 0
                _frame_epoch_ns = 0
                _frame_mono_ns = 0
                _local_window_capture = False
                self._pending_y_plane = None
                self._pending_frame_isolated = False
                self._pending_source_frame_number = 0
                self._pending_source_identity = 0
                self._pending_backend_frozen = False
                # Producer backends are EVENT-DRIVEN: block on the ring event for
                # the next fresh capture-card OR decoded-pipe frame. Poll+pace gave
                # Remote Play an avoidable 0..one-frame phase lag.
                _cc_active = (self._frame_backend is not None
                              and self._frame_backend_mode == 'capture_card')
                _event_driven_active = self._backend_is_event_driven(
                    self._frame_backend_mode, self._frame_backend)
                if self._frame_backend is not None:
                    img, _frame_pts, _frame_epoch_ns, _frame_mono_ns = self._capture_decoded_frame(
                        block=_event_driven_active)
                    # [ORION_STALL_ATTRIB 2026-09-15] THE CONSUMER CALLBACK, timestamped.
                    # This is the instant a frame is handed to the sidecar's consumer, i.e.
                    # the cadence `preview_stats callback_gap_ms` measures one stage later.
                    # Five operations; it never formats and never logs (the watchdog thread
                    # does both). A gap wider than ORION_STALL_GAP_MS queues one STALL line
                    # naming the thread that was running through it.
                    if img is not None and _stall is not None:
                        _stall.note_callback()
                else:
                    img = None
                _cap_tier = (self._frame_backend_mode if img is not None else None)
                if (img is None and self._frame_backend is None
                        and (self._frame_pipe_required or getattr(self, '_xbox_mode', False))):
                    if self._last_frame_reject_reason != 'decoder_frame_pipe_unavailable':
                        self._note_frame_reject('decoder_frame_pipe_unavailable')
                    time.sleep(0.02)
                    continue
                # Capture-card mode with the card not attached yet: NEVER GDI-grab the chiaki window (that
                # is the remote-play video = the user's bug). Idle and re-attempt the card every ~2s so it
                # attaches the instant it's available (device freed after relaunch / PS5 shows content).
                if img is None and self._frame_backend is None and self._cc_mode:
                    _now = time.perf_counter()
                    if _now - self._cc_last_retry >= 2.0:
                        self._cc_last_retry = _now
                        try:
                            self._start_frame_backend()
                        except Exception as _cce:
                            logger.debug('capture-card retry skip: %s', _cce)
                    time.sleep(0.05)
                    continue
                if img is None:
                    # DECODER/backend mode: the frame backend (decoder pipe / capture-card) is the
                    # source. In QML-render the chiaki window is parked OFF-SCREEN, so GDI-grabbing it
                    # only yields black/dup100 frames -> the watchdog restart loop. NEVER fall to window
                    # capture while a backend is active; wait at pipe rate for the next decoded frame.
                    if self._frame_backend is not None:
                        # SELF-HEAL: if the card reader thread DIED (30 bad reads: USB hiccup / HDMI
                        # renegotiation / device grabbed), the old code left the dead backend attached
                        # forever -> frameAge climbed -> the NATIVE watchdog restarted the whole sidecar
                        # (stray floating chiaki window + webcam rescan + 2-strike safe mode). Detach the
                        # corpse here so the ~2s card retry above re-opens IN-PROCESS instead.
                        if (_cc_active and hasattr(self._frame_backend, 'is_healthy')
                                and not self._frame_backend.is_healthy()):
                            logger.warning('Capture-card reader died; detaching for in-process re-open '
                                           '(no sidecar restart)')
                            try:
                                self._frame_backend.stop()
                            except Exception:
                                pass
                            self._frame_backend = None
                            self._frame_backend_mode = 'capture'
                            continue
                        if not _event_driven_active:
                            time.sleep(0.002)
                        continue
                    if not self._window_handle or not win32gui.IsWindow(self._window_handle):
                        now = time.perf_counter()
                        # Fast re-find (was 1.0s): a hidden/occluded/stale window is the main cause of
                        # the frame-age climb, so re-acquire it quickly (legacy window-grab mode only).
                        if now - last_refind >= 0.15:
                            self._window_handle = self._find_window(self.config.window_title)
                            last_refind = now
                        time.sleep(0.03)
                        continue
                    img, _cap_tier = self._capture_window_tier(self._window_handle)
                    # Window/GDI capture has no producer clock, so stamp its actual
                    # local readback boundary explicitly. Backend paths may only use
                    # their FrameData clocks and never inherit a fabricated local now.
                    _frame_mono_ns = time.perf_counter_ns()
                    _frame_epoch_ns = time.time_ns()
                    _local_window_capture = True
                now = time.perf_counter()
                # Every active backend stamps perf_counter_ns.  The detector-age contract below
                # rejects stale/regressing stamps before publication; window capture uses `now`.
                _cap_ts = (_frame_mono_ns / 1e9) if _frame_mono_ns > 0 else now
                new_unique_frame = False
                _frame_for_preview = None
                # Reject None / fully-black / partial-black grabs: do NOT feed them to detection.
                # LIVENESS, though, is separate from CONTENT: a frame delivered by an active frame
                # backend (capture card / decoder / WGC) â€” even a black one â€” proves the pipeline is
                # alive, so it keeps the freshness clock current (a legit loading screen / fade / brief
                # HDMI blip must NOT climb frame-age into the native 8s frame-stall watchdog and restart
                # the whole sidecar). A window-capture black grab (no backend) means the BitBlt failed,
                # so it stays frozen and the climbing age HONESTLY reflects capture being down, tearing
                # down the (likely hidden/stale) window handle for a fast re-find.
                #
                # Dark content remains useful as a DEVICE-liveness signal, but it is never a
                # detector sample.  The integrity fault is emitted immediately, clears held meter
                # state, and stays unhealthy until a later valid frame completes detection.
                _from_backend = self._frame_backend is not None
                _source_height = int(img.shape[0]) if isinstance(img, np.ndarray) and img.ndim >= 2 else 0
                _source_width = int(img.shape[1]) if isinstance(img, np.ndarray) and img.ndim >= 2 else 0
                _dark = (img is not None) and self._frame_is_probably_black(img)
                if self._frame_keeps_pipeline_alive(img, _from_backend, _dark):
                    self._last_capture_ts = _cap_ts
                self._capture_count += 1
                reject_reason = ''
                detector_frame = None
                if _dark:
                    # Display and detector authority are intentionally separate. A dark/loading
                    # frame must revoke the bot immediately, but an active backend delivering it
                    # is still a real live picture; freezing the launcher on the last bright frame
                    # made a normal loading transition look like capture had stalled. Prepare the
                    # same immutable/aspect-safe 1280x720 snapshot for DISPLAY ONLY. It is never
                    # published to `_frame_bundle`, never advances detector seq, and never restores
                    # integrity health.
                    if _from_backend:
                        _frame_for_preview, _ = self._prepare_detector_frame(
                            img, trusted_isolated=bool(self._pending_frame_isolated))
                    reject_reason = 'dark_frame'
                elif self._pending_backend_frozen:
                    reject_reason = 'backend_content_frozen'
                else:
                    detector_frame, reject_reason = self._prepare_detector_frame(
                        img, trusted_isolated=bool(self._pending_frame_isolated))
                    if not reject_reason:
                        reject_reason = self._source_frame_reason(
                            detector_frame,
                            self._pending_source_identity,
                            self._pending_source_frame_number,
                            _frame_mono_ns,
                            now,
                        )
                    if not reject_reason:
                        reject_reason = self._source_pts_reason(
                            _frame_pts, _frame_epoch_ns,
                            self._pending_source_identity)
                    # Exact pixel equality is a detector rejection, not a display
                    # fault.  The source sequence and timestamp have already proved
                    # that this is a newly delivered, current frame.  Forward the
                    # immutable normalized snapshot to preview at source cadence while
                    # keeping it out of `_frame_bundle`, `_frame_seq`, latency
                    # calibration, and every bot-authority path.  Skipping it here was
                    # the live 32/47 ms preview hitch: one/two fresh repeated renders
                    # disappeared even though SHM itself had zero failures.
                    if reject_reason == 'pixel_duplicate':
                        _frame_for_preview = detector_frame
                        self._preview_duplicate_refreshes += 1
                if reject_reason:
                    self._black_run += 1
                    self._capture_bad_run += 1
                    self._note_frame_reject(
                        reject_reason,
                        fail_closed=self._frame_reject_fails_closed(reject_reason))
                    if self._frame_backend is None and self._black_run >= 3:
                        self._window_handle = None
                        self._last_frame_hash = None
                        self._black_run = 0
                    # Surface a sustained capture failure so a real dropout has a
                    # visible cause in orion_native.log (throttled).
                    if self._frame_backend is None and (self._capture_bad_run in (10, 60) or (self._capture_bad_run % 180 == 0)):
                        logger.warning(
                            'Capture degraded: %d consecutive rejected frames (last=%s)',
                            self._capture_bad_run, reject_reason,
                        )
                else:
                    self._black_run = 0
                    self._capture_bad_run = 0
                    # Detector and preview share only this immutable, owned frame.  A
                    # driver/decoder buffer can never be recycled underneath either consumer.
                    img = detector_frame
                    _frame_for_preview = detector_frame
                    # A good frame: the stream is genuinely live (liveness clock advanced above).
                    # Item 12: Store PTS and calibrate PTS-to-wall-clock offset.
                    # The PTS gives exact frame timestamps from the decoder, eliminating
                    # pipe transfer jitter from frame age calculation.
                    if _frame_pts > 0:
                        self._last_pts = _frame_pts
                        # PTS is in microseconds (AV_TIME_BASE). Calibrate offset so
                        # pts_seconds + offset â‰ˆ wall_clock. Slow EMA to track drift.
                        pts_s = _frame_pts / 1000000.0
                        # Connection-generation changes reset this mapping before
                        # calibration. Unexpected jumps retain the old mapping so
                        # queued frames stay honestly old instead of mapping to now.
                        self._pts_to_wall_offset, _ = _pts_offset_update(
                            self._pts_to_wall_offset, now - pts_s, _PTS_JUMP_S)
                    frame_hash = self._frame_hash(img)
                    # Exact uniqueness was already established by _source_frame_reason.
                    # Keep the historical hash for diagnostics only; it is no longer a gate
                    # that can step over a thin moving meter.
                    if not reject_reason:
                        self._last_frame_hash = frame_hash
                        self._last_frame = img
                        # Y plane rides ONLY with a backend-delivered frame (window/GDI
                        # grabs have none); committed together so they can never skew.
                        # [ORION_DECODER_YPLANE 2026-09-14] The plane has exactly ONE consumer,
                        # CompressedMeterReader.set_native_y, which no shipped route reaches
                        # (ORION_SIMPLE_READER=1 everywhere). Normalising a 720p plane 60x/s for
                        # a reader that discards it was pure waste on the decoder route, so it is
                        # skipped unless the live reader can consume it. A missing reader keeps
                        # the plane (diagnostics/tests inspect the commit); capture-card frames
                        # carry no plane at all, so that route is untouched.
                        _det = getattr(self, '_meter_detector', None)
                        _wants_y = _det is None or callable(getattr(_det, 'set_native_y', None))
                        self._last_y_plane = (
                            _normalize_detector_y_plane(
                                self._pending_y_plane,
                                source_width=_source_width,
                                source_height=_source_height,
                                width=int(img.shape[1]),
                                height=int(img.shape[0]),
                            ) if (self._frame_backend is not None and _wants_y) else None
                        )
                        self._last_frame_ts = _cap_ts
                        # A0 unified timebase: epoch stamp of THIS unique frame. Backend
                        # sources must supply it; only the explicit local window/GDI path
                        # above is allowed to manufacture a local capture stamp.
                        self._last_frame_epoch_ms = (
                            (_frame_epoch_ns / 1e6)
                            if _frame_epoch_ns > 0
                            and (_from_backend or _local_window_capture)
                            else 0.0
                        )
                        # Snapshot the pts WITH the unique frame (self._last_pts also advances on
                        # duplicate-hash frames, which must not restamp this frame's consumers),
                        # and calibrate the PTS->epoch offset from this frame's own epoch stamp.
                        self._last_frame_pts = _frame_pts
                        if _frame_pts > 0 and self._last_frame_epoch_ms > 0.0:
                            _pe = self._last_frame_epoch_ms - _frame_pts / 1000.0
                            self._pts_to_epoch_ms, _ = _pts_offset_update(
                                self._pts_to_epoch_ms, _pe, _PTS_JUMP_MS)
                        self._last_frame_measurement_epoch_ms = \
                            self._frame_measurement_epoch_ms(
                                self._last_frame_pts,
                                self._last_frame_epoch_ms,
                                self._pts_to_epoch_ms,
                            )
                        # P1 fix (+H1): publish the whole frame tuple as ONE atomic reference BEFORE
                        # bumping _frame_seq, so a reader that observes the new seq is guaranteed to see
                        # THIS frame's bundle. (Publishing AFTER the bump let the processing loop pass
                        # its _frame_seq freshness gate on frame N yet still read N-1's bundle across a
                        # GIL switch -> stale/duplicate sample + frame N skipped.) The tuple read stays
                        # tear-free (single atomic reference); detect()'s 6-40ms can't skew the staleness
                        # clock because every consumer uses this snapshot, not the live self._last_* fields.
                        # D5 [fix]: the bundle carries its OWN sequence number. The processing loop
                        # used to take `last_seq = self._frame_seq` and read `self._frame_bundle` as
                        # two separate loads, so a capture-thread publish landing between them made
                        # that iteration detect frame N+1's PIXELS while recording `last_seq = N`:
                        # frame N was skipped entirely and N+1 was detected TWICE -- the second time
                        # as a zero-dt duplicate fed straight into the velocity chain. Publishing the
                        # seq INSIDE the tuple makes the snapshot self-describing, so the loop can
                        # only ever mark done the frame it actually processed.
                        _new_seq = self._frame_seq + 1
                        with self._frame_integrity_lock:
                            _publish_integrity_generation = self._frame_integrity_generation
                        self._frame_bundle = (self._last_frame, self._last_frame_ts,
                                              self._last_y_plane, self._last_frame_pts,
                                              self._last_frame_epoch_ms,
                                              self._last_frame_measurement_epoch_ms,
                                              _new_seq,
                                              int(self._pending_source_frame_number or 0),
                                              bool(self._pending_backend_frozen),
                                              int(_publish_integrity_generation),
                                              int(self._pending_source_identity or 0))
                        self._frame_count += 1
                        self._unique_count += 1
                        self._frame_seq = _new_seq
                        detector_event = getattr(self, '_detector_frame_ready_evt', None)
                        if detector_event is not None:
                            detector_event.set()
                        new_unique_frame = True
                    # --- Capture-health diagnostics (no behavior change): which tier
                    # produced this frame, and whether the VIDEO CORE is live. A black
                    # core, or a core that stays static WHILE the whole frame changes, is
                    # the "capture feeds a meterless frame" signature behind live
                    # roi_not_found runs. A merely-static core (idle scene) is NOT flagged.
                    self._last_capture_tier = _cap_tier or ''
                    if _cap_tier:
                        self._tier_counts[_cap_tier] = self._tier_counts.get(_cap_tier, 0) + 1
                    core_black, core_hash = self._video_region_status(img)
                    self._video_core_black_run = (self._video_core_black_run + 1) if core_black else 0
                    if new_unique_frame and core_hash is not None and core_hash == self._video_core_hash:
                        self._video_core_static_run += 1   # whole frame changed, core did not
                    elif new_unique_frame:
                        self._video_core_static_run = 0
                    self._video_core_hash = core_hash
                if now - self._last_fps_time >= 1.0:
                    elapsed = max(0.001, now - self._last_fps_time)
                    self._capture_fps = int(round(self._capture_count / elapsed))
                    self._unique_frame_fps = int(round(self._unique_count / elapsed))
                    self._fps = self._unique_frame_fps
                    if self._capture_count > 0:
                        duplicates = max(0, self._capture_count - self._unique_count)
                        self._duplicate_frame_pct = duplicates / max(1, self._capture_count) * 100.0
                    else:
                        self._duplicate_frame_pct = 0.0
                    # RC-2c: _frame_count is a NEVER-RESET monotonic dedup key now (see __init__); the
                    # per-second counters that DO reset are _capture_count / _unique_count below.
                    self._capture_count = 0
                    self._unique_count = 0
                    self._last_fps_time = now
                # Capture-health diagnostic: surface the tier mix + video-core liveness so
                # a live/gameplay run self-explains roi_not_found dropouts. Logged every
                # ~5s, or immediately on SUSPECT/recovery transitions (black core,
                # or a sustained whole-frame-changing-while-core-static run).
                # Persistent SUSPECT is still capped at 5s; otherwise the one-slot
                # worker could write warnings at its own throughput indefinitely.
                # [ORION_CAPTURE_FPS 2026-09-14] These two were FRAME counts hard-sized for a
                # 60fps card: 15 frames = 0.25s of a black core, 30 = 0.5s of a frame that
                # changes while its core does not. At 30fps they silently became 0.5s/1.0s and
                # at 120fps 0.125s/0.25s -- the same warning meaning three different durations.
                # Derive both from the requested rate so SUSPECT means the same WALL-CLOCK
                # dropout at every supported rate; at 60 (the shipped default and the only rate
                # any previous build ran) this is byte-identical to the old 15/30.
                _black_run_frames, _static_run_frames = capture_suspect_run_frames(
                    getattr(self, '_requested_capture_fps', CAPTURE_FPS_DEFAULT))
                _suspect = (self._video_core_black_run >= _black_run_frames
                            or self._video_core_static_run >= _static_run_frames)
                if self._capture_health_report_due(now, _suspect):
                    # Delivery-vs-CV attribution is preserved exactly, but all
                    # backend queries, dict/log formatting, handlers, and stdout
                    # now execute on the bounded diagnostics worker.
                    self._queue_capture_health_diagnostic(_suspect)
                self._reclaim_dshow_route_if_invalid(now)
                # DISPLAY forward. The decoder-pipe path polls a latest-wins ring, so a 60fps poll
                # against a 60fps decode with independent phase re-reads the SAME frame on ~half the
                # polls -> forwarding every poll showed each image for 2 vsyncs (a "held" frame) =
                # judder that reads as "not true 60fps". DEDUP on the decoder's monotonic frame number
                # so each decoded frame is forwarded to the preview exactly once (evenly paced). This
                # is NOT the old hash-uniqueness gate (that pinned display to the scene-CHANGE rate ~38);
                # frame_number increments every decode, so a static scene still forwards a fresh 60/s.
                # Fallback: frame_number==0 (unavailable, e.g. window/GDI tiers) -> forward every frame
                # as before. Detection above still uses EVERY img, so meter sampling is unchanged.
                if self._video_callback and _frame_for_preview is not None:
                    _fn = int(getattr(self, '_last_decoded_frame_number', 0) or 0)
                    if _fn == 0 or _fn != getattr(self, '_last_forwarded_frame_number', -1):
                        self._last_forwarded_frame_number = _fn
                        self._video_callback((
                            _frame_for_preview,
                            _fn,
                            int(self._frame_seq),
                            int(_frame_mono_ns or 0),
                        ))
                # Capture-card is self-paced by the blocking get_frame() above (wakes on each new card
                # frame) â€” skip the poll pacing entirely so we never add phase lag on top of it.
                if _event_driven_active:
                    continue
                # Clamp the polling rate to [30, 240]. The accumulating scheduler
                # (next_capture += target_delay) keeps the long-run rate exact.
                target_delay = 1.0 / min(240.0, max(30.0, float(self.config.target_fps)))
                next_capture += target_delay
                sleep_for = next_capture - time.perf_counter()
                if sleep_for > 0.0005:
                    # Sleep up to a FULL frame interval (not a fixed 6ms cap). The old
                    # 6ms cap made the loop re-capture every ~6-8ms regardless of the
                    # target fps, so a 60fps stream was polled at ~150fps -> ~60%
                    # duplicate frames (wasted CPU + velocity jitter). Pacing to the
                    # real frame interval (with the 1ms timer above) captures at the
                    # stream rate: each grab is far more likely to be a fresh frame.
                    time.sleep(min(target_delay, sleep_for))
                else:
                    # At/past schedule (incl. a stall/GC drift) â€” rebase to now so we
                    # don't burst-capture to "catch up".
                    next_capture = time.perf_counter()
            except Exception as e:
                logger.error(f'Error in capture loop: {e}')
                time.sleep(0.1)
        if _winmm is not None:
            try:
                _winmm.timeEndPeriod(1)
            except Exception:
                pass
        if _stall is not None:
            try:
                _stall.get().stop()
            except Exception:
                pass

    @staticmethod
    def _frame_hash(frame):
        # Denser subsample (~48x64 grid vs the old 18x32). The shot meter is a
        # small region; the coarse grid could step right over it, so a filling
        # meter on an otherwise-static HUD hashed identical -> the frame was
        # judged a duplicate -> unique-frame-fps fell to 0 and frame-age spiked
        # -> the sample was rejected as stale mid-shot. A finer grid catches the
        # meter's change. Reprocessing cost of any extra "unique" frames is
        # negligible (detection is already gated on the capture sequence).
        try:
            # 96x128 grid (was 48x64): at 60fps a meter that moves 1-2px/frame still hashed
            # identical on the coarser grid -> the frame was judged DUPLICATE -> uniqfps collapsed
            # to ~30 (the "feels 30fps") and frame-age spiked. The finer grid catches sub-cell motion.
            sample = frame[::max(1, frame.shape[0] // 96), ::max(1, frame.shape[1] // 128)]
            return hash(sample.tobytes())
        except Exception:
            return None

    def _update_green_overlay(self, gw):
        """Apply show/hide hysteresis to the reported green window so the
        track-fill overlay does not flicker. gw is a window dict when a green
        window was found this frame, else None."""
        if gw is not None:
            self._green_show_streak += 1
            self._green_hide_streak = 0
            if self._green_show_streak >= self._green_show_required:
                self._last_green_window = gw
        else:
            self._green_hide_streak += 1
            self._green_show_streak = 0
            if self._green_hide_streak >= self._green_hide_required:
                self._last_green_window = None

    def _clear_meter_overlay_immediate(self):
        """Clear cached meter geometry for an authoritative non-gameplay rejection."""
        self._last_meter_bbox = None
        self._last_meter_det_bbox = None
        self._last_meter_bbox_wh = None
        self._last_green_window = None
        self._green_show_streak = 0
        self._green_hide_streak = 0

    @staticmethod
    def _should_feed_engine(result) -> bool:
        """Whether a detection result should be fed to the timing engine.

        Feed ONLY VALIDATED detections â€” rejection_reason '' (accepted) or
        'green_not_found' (meter found + confident + positionally STABLE, just no
        green window yet, which is the normal rising/contested phase). Everything
        else is excluded: 'bbox_unstable' (jittery position â†’ noisy fill that
        whipsaws the velocity/crossing prediction and causes wild early releases),
        'low_confidence', 'roi_not_found', and the idle 'meter_memory' echo. On the
        small embedded capture the bbox_unstable frames were the main source of
        noisy fill being fed; gating them out gives the predictor a clean signal.

        SAMPLER TIER (plan B2 [fix â€” blocker]): 'fill_gated' (trajectory-gate hold)
        and 'dead_reckoned' (template coast) ARE fed to the engine â€” the fill/box
        keep flowing so the engine doesn't starve mid-shot â€” but they are EXCLUDED
        from the raw tier below (raw_fed stays false â†’ the native engine marks them
        stale_or_memory, so synthetic/held fill can never look fresh to a fire path),
        and they never reach the oracle/tip-reg/forecaster feeds.
        """
        if not result or not getattr(result, 'detected', False):
            return False
        bbox = getattr(result, 'bbox', None)
        if not bbox or bbox[2] <= 0 or bbox[3] <= 0:
            return False
        return getattr(result, 'rejection_reason', '') in (
            '', 'green_not_found', 'fill_gated', 'dead_reckoned')

    @staticmethod
    def _is_raw_accepted(result) -> bool:
        """RAW tier: THIS frame is a clean, fresh, accepted detection (feeds raw_fed, the
        latency oracle, tip registration, forecaster/kalman). Strictly narrower than
        _should_feed_engine: the sampler-tier reasons (fill_gated / dead_reckoned) are
        deliberately excluded â€” a gated or synthetic sample must never teach a model or
        grant freshness trust downstream."""
        if not result or not getattr(result, 'detected', False):
            return False
        bbox = getattr(result, 'bbox', None)
        if not bbox or bbox[2] <= 0 or bbox[3] <= 0:
            return False
        return getattr(result, 'rejection_reason', '') in ('', 'green_not_found')

    def _start_detcsv(self):
        """Start one optional metadata sink; startup/restart never touches capture I/O."""
        if not getattr(self, '_detcsv_enabled', False):
            self._detcsv_start_status = 'off'
            return
        existing = getattr(self, '_detcsv', None)
        if existing is not None and existing.is_running():
            if not existing.snapshot()['accepting']:
                # A previous bounded close timed out. That daemon still owns
                # the file; do not spawn a second owner or silently call it active.
                self._detcsv_start_status = 'disabled_previous_writer_closing'
                logger.warning('DETCSV disabled_this_start reason=previous_writer_still_closing; '
                               'no replacement writer started')
            else:
                self._detcsv_start_status = 'active_existing'
            return
        self._detcsv = None
        self._detcsv_start_status = 'starting_async'
        try:
            keep = max(1, int(os.environ.get('ORION_DETCSV_KEEP', '24')))
            # PER-FILE budget, not a session cap: the sink now rotates into
            # detframes_<ts>_partNN.csv and keeps recording (see AsyncDiagnosticCsv).  64 MiB is
            # ~53 minutes at the -Detdiag row rate, which is how the 2026-09-14 session lost its
            # last seven minutes; max_parts bounds the total the session may write.
            max_bytes = max(1, int(os.environ.get('ORION_DETCSV_MAX_BYTES', str(64 * 1024 * 1024))))
            max_parts = max(1, int(os.environ.get('ORION_DETCSV_MAX_PARTS', '16')))
            flush_s = max(0.0, min(5.0, float(os.environ.get('ORION_DETCSV_FLUSH_MS', '2000')) / 1000.0))
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'logs', 'diagnostics', 'detframes.csv')
            self._detcsv_t0 = time.perf_counter()
            self._detcsv = AsyncDiagnosticCsv(path, _DETCSV_HEADER, keep=keep,
                                             capacity=256, report=logger.warning,
                                             report_critical=logger.error,
                                             max_bytes=max_bytes, max_parts=max_parts,
                                             flush_interval=flush_s)
        except Exception as exc:
            # No synchronous fallback: diagnostics are best-effort, capture is not.
            self._detcsv_start_status = 'startup_failed'
            logger.warning('DETCSV worker startup failed: %s', type(exc).__name__)

    # ------------------------------------------------------------------ #
    #  PRESS-WINDOW FRAME DUMP (ORION_FRAMEDUMP_PRESS_WINDOW=1)
    #
    #  THE FAILURE THIS REPLACES.  The 09-15 drill asked the writer for a frame every
    #  0.1 s and got 826 of 1421: `dropped=595 skipped=119 state=stopped:idle`.  The
    #  reason is arithmetic, not policy -- a 1080p PNG costs **49 ms** to encode and write
    #  on this workstation (measured; level-1 PNG is worse at 94 ms because the file
    #  triples).  One slot of queue plus a 25 % duty cooldown plus a 250 ms stall guard
    #  cannot express 60 fps at 49 ms/frame, and nothing in the chain was ever going to.
    #
    #  For the animation anchor we do not want a thin 10 Hz sample of the whole session.
    #  We want EVERY frame of the ~1 s around each press and nothing else at all.  That
    #  changes the arithmetic completely:
    #
    #      1080p JPEG-90       4.27 ms    83 KiB   -> 26 % duty at 60 fps, 5.0 MB/shot
    #      400x500 JPEG-90     0.57 ms   8.6 KiB   ->  3 % duty at 60 fps, 0.5 MB/shot
    #      1080p PNG          49.18 ms  1108 KiB   -> IMPOSSIBLE (3x the frame budget)
    #
    #  so this mode writes JPEG by default, keeps the crop option for the shooter patch,
    #  gives the writer a real queue for the burst, and drops the interval throttle and
    #  the duty cooldown INSIDE a window (they are what thinned the drill dump).  Outside
    #  a window it writes nothing whatsoever.
    #
    #  PRE-ROLL.  press-200 ms is in the past when the arm reaches the control thread, so
    #  the detect thread keeps a ring of the last few frame REFERENCES.  No copy is made:
    #  capture already hands out an isolated array per frame (ORION_CAPTURE_ISOLATE_COPY),
    #  so retaining one costs a pointer, and the ring is bounded by the pre-roll length.
    #
    #  Everything the existing writer earned stays: the disk floor, the heartbeat, the
    #  slow-write cooldown, the error streak, and the counted-never-silent drop policy.
    # ------------------------------------------------------------------ #
    def _init_framedump_press_window(self):
        """Read the press-window knobs. Called from __init__ BEFORE the shared knobs,
        because it changes their defaults (cap, queue depth, format)."""
        def _on(name, default='0'):
            return str(os.environ.get(name, default) or '').strip().lower() in (
                '1', 'true', 'yes', 'on')

        def _f(name, default, lo, hi):
            try:
                return max(lo, min(hi, float(os.environ.get(name, '') or default)))
            except (TypeError, ValueError):
                return float(default)

        self._framedump_press_window = _on('ORION_FRAMEDUMP_PRESS_WINDOW', '0')
        # ---------------------------------------------------------------- #
        # PER-SESSION OUTPUT DIRECTORY (press-window mode only).
        #
        # THE FAILURE THIS FIXES.  Press-window filenames carry the SHOT-GATE EPOCH
        # (`ep<epoch>_f<idx>_<det>_raw.jpg`) and the epoch restarts at 1 on every sidecar
        # start, but the dump wrote into ONE shared `ORION_FRAMEDUMP_ROOT` for every session.
        # On 2026-09-17 that produced `ep2_f000100_*` twice over (126 files for a 97-frame
        # window) and a `frames.csv` with 6,649 rows of which only 4,339 belonged to the
        # session being analysed -- so a join by epoch silently mixed two sessions' shots.
        #
        # The stamp is the SAME one the shot-record file uses (shot_records.default_session_
        # name reads exactly these two variables), so `<session>.jsonl` and
        # `<root>\session_<stamp>\` carry one label and join by (session, epoch).  An
        # explicit ORION_FRAMEDUMP_DIR that already names a session folder is honoured
        # untouched -- that is what the launcher composes -- and the legacy interval mode is
        # not touched at all.
        self._framedump_session = ''
        _root = str(getattr(self, '_framedump_dir', '') or '')
        if self._framedump_press_window and _root:
            base = os.path.basename(_root.rstrip('\\/'))
            if base.startswith('session_'):
                self._framedump_session = base
            else:
                self._framedump_session = time.strftime('session_%Y%m%d_%H%M%S')
                self._framedump_dir = os.path.join(_root, self._framedump_session)
        self._framedump_press_pre_ms = _f('ORION_FRAMEDUMP_PRESS_PRE_MS', 200.0, 0.0, 2000.0)
        # ---------------------------------------------------------------- #
        # REACH THE BANNER (ORION_FRAMEDUMP_PRESS_BANNER=1, the default).
        #
        # THE FAILURE THIS FIXES.  The window closed at release+400 ms, and the game's
        # `TIMING | DISTANCE` panel does not land until release+1.2..1.45 s -- so of the 53
        # press windows dumped on 2026-09-17 03:07, only NINE contained a panel at all, and
        # those nine were the PREVIOUS shot's.  Every offline study that needs the banner as
        # its oracle (the timing verdict, and the free DISTANCE range label the shot-range
        # classifier is calibrated against) was therefore reading a corpus that structurally
        # could not contain its own answer.
        #
        # 1700 ms is release+1.45 s (the far end of the measured landing window) plus a
        # ~250 ms margin for the panel's own rise, and it is the POST leg only: the window
        # still ends POST_MS after the FIRST close, and the hard cap below still bounds a
        # press nobody ever closed.  ORION_FRAMEDUMP_PRESS_BANNER=0 restores 400 ms
        # exactly, and an explicit ORION_FRAMEDUMP_PRESS_POST_MS always wins over both.
        # ---------------------------------------------------------------- #
        self._framedump_press_banner = (self._framedump_press_window
                                        and _on('ORION_FRAMEDUMP_PRESS_BANNER', '1'))
        _post_default = 1700.0 if self._framedump_press_banner else 400.0
        self._framedump_press_post_ms = _f('ORION_FRAMEDUMP_PRESS_POST_MS',
                                           _post_default, 0.0, 5000.0)
        # Hard stop so an unanswered press (the stuck-Square class) cannot dump forever.
        # It has to clear the longest real shot plus the banner leg or it would silently
        # truncate exactly the frames the change above is for: a Go-To releases ~2.1 s after
        # the press (ep33 of session_20260917_030758), so 2100 + 1700 = 3800 ms is the bar.
        _max_default = 4500.0 if self._framedump_press_banner else 3000.0
        self._framedump_press_max_ms = _f('ORION_FRAMEDUMP_PRESS_MAX_MS',
                                          _max_default, 200.0, 20000.0)
        # 'shooter' = the player-anchor patch +- margin (~400x500). Anything else = full frame.
        self._framedump_crop = str(os.environ.get('ORION_FRAMEDUMP_CROP', '') or '').strip().lower()
        self._framedump_crop_w = int(_f('ORION_FRAMEDUMP_CROP_W', 400.0, 64.0, 1920.0))
        self._framedump_crop_h = int(_f('ORION_FRAMEDUMP_CROP_H', 500.0, 64.0, 1080.0))
        fmt = str(os.environ.get('ORION_FRAMEDUMP_FORMAT', '') or '').strip().lower()
        if fmt not in ('jpg', 'jpeg', 'png'):
            # Legacy dumps stay PNG byte-for-byte; only the new mode changes its default.
            fmt = 'jpg' if self._framedump_press_window else 'png'
        self._framedump_format = 'jpg' if fmt in ('jpg', 'jpeg') else 'png'
        self._framedump_jpeg_quality = int(_f('ORION_FRAMEDUMP_JPEG_QUALITY', 90.0, 40.0, 100.0))
        # Pre-roll ring: pre_ms of 60 fps frames + 2 slack, capped.
        depth = int(self._framedump_press_pre_ms / (1000.0 / 60.0)) + 2
        self._framedump_preroll_max = int(_f('ORION_FRAMEDUMP_PREROLL_MAX', float(depth), 0.0, 120.0))
        self._framedump_preroll = collections.deque(maxlen=max(1, self._framedump_preroll_max))
        # The ring and the writer both hold frames the detect thread has moved on from, so
        # they depend on capture handing out an ISOLATED array per frame.  That is the
        # default (ORION_CAPTURE_ISOLATE_COPY=1); with it switched off the only sound thing
        # to do is copy on hand-off, which is what this flag makes the offer path do.
        try:
            import capture_card_backend as _ccb
            self._framedump_press_copy = not bool(getattr(_ccb, '_ISOLATE_COPY', True))
        except Exception:
            self._framedump_press_copy = False
        # QUEUE DEPTH lives here because press-window mode changes its default AND its cap.
        _qd_default = '96' if self._framedump_press_window else '1'
        _qd_cap = 512 if self._framedump_press_window else 4
        try:
            self._framedump_queue_depth = max(
                1, min(_qd_cap, int(os.environ.get('ORION_FRAMEDUMP_QUEUE_DEPTH', _qd_default))))
        except ValueError:
            self._framedump_queue_depth = int(_qd_default)
        self._framedump_press_epoch = 0
        self._framedump_press_until = 0.0       # perf_counter deadline; 0 = no open window
        self._framedump_press_hard_until = 0.0
        self._framedump_press_stats = None
        self._framedump_census_lock = threading.RLock()
        self._framedump_press_windows = 0
        self._framedump_press_frames = 0
        self._framedump_press_dropped = 0

    def framedump_press_open(self, epoch, mono_now=None):
        """Open one bounded window; type upgrades do not replace the same epoch."""
        if not getattr(self, '_framedump_press_window', False) \
                or not getattr(self, '_framedump_env_enabled', False):
            return False
        try:
            ep = _parse_pose_arm_token(epoch)
            if ep <= 0:
                return False
            now = float(mono_now) if mono_now is not None else time.perf_counter()
            with self._framedump_census_lock:
                if ep == self._framedump_press_epoch and self._framedump_press_until > now:
                    return True
                old = self._framedump_detach_press_stats('superseded')
                self._framedump_press_epoch = ep
                self._framedump_press_hard_until = now + self._framedump_press_max_ms / 1000.0
                self._framedump_press_until = self._framedump_press_hard_until
                stats = self._framedump_press_stats = {
                    'epoch': ep, 'first_idx': -1, 'last_idx': -1, 'frames': 0,
                    'dropped': 0, 'skipped': 0, 'preroll': 0, 'preroll_ondisk': 0,
                    'opened': now, 'saved': 0, 'indexed': 0, 'pending': 0,
                    'discarded': 0, 'first_saved_idx': -1, 'last_saved_idx': -1,
                    '_closed_reason': '', '_revision': 0,
                    '_dir': str(getattr(self, '_framedump_dir', '')),
                    '_session': str(getattr(self, '_framedump_session', '') or '')}
                self._framedump_press_windows += 1
                ring = getattr(self, '_framedump_preroll', None)
                preroll = list(ring) if ring is not None else []
                if ring is not None:
                    ring.clear()
            if old is not None:
                self._framedump_publish_census(old, initial=True)
            cutoff = now - self._framedump_press_pre_ms / 1000.0
            for item in preroll:
                if item[0] < cutoff:
                    continue
                if len(item) > 3 and item[3]:
                    # Legacy name: this means accepted by the PREVIOUS window,
                    # not proven saved. That window's saved/indexed census is the
                    # completion evidence; never count this frame as saved here.
                    with self._framedump_census_lock:
                        if self._framedump_press_stats is not stats:
                            break
                        stats['preroll_ondisk'] += 1
                    continue
                self._framedump_offer(item[1], item[2], item[0], preroll=True,
                                      expected_stats=stats)
            return True
        except Exception as exc:
            logger.warning('framedump press window open failed: %s', exc)
            return False

    def framedump_press_close(self, epoch, mono_now=None):
        """Shorten the matching window once; duplicate closes cannot extend it."""
        if not getattr(self, '_framedump_press_window', False):
            return False
        try:
            ep = _parse_pose_arm_token(epoch)
            now = float(mono_now) if mono_now is not None else time.perf_counter()
            with self._framedump_census_lock:
                if ep <= 0 or ep != self._framedump_press_epoch:
                    return False
                self._framedump_press_until = min(
                    self._framedump_press_until, self._framedump_press_hard_until,
                    now + self._framedump_press_post_ms / 1000.0)
            return True
        except Exception:
            return False

    def _framedump_detach_press_stats(self, reason, expected_stats=None):
        """Detach under the census lock; no logging, recorder calls or image I/O."""
        stats = getattr(self, '_framedump_press_stats', None)
        if expected_stats is not None and stats is not expected_stats:
            return None
        self._framedump_press_stats = None
        self._framedump_press_epoch = 0
        self._framedump_press_until = 0.0
        self._framedump_press_hard_until = 0.0
        if stats is not None:
            stats['_closed_reason'] = str(reason)
        return stats

    def _framedump_close_press_stats(self, reason, expected_stats=None):
        """Close capture immediately; finish its census when accepted work resolves."""
        if getattr(self, '_framedump_press_stats', None) is None:
            return
        with self._framedump_census_lock:
            stats = self._framedump_detach_press_stats(reason, expected_stats)
        if stats is not None:
            self._framedump_publish_census(stats, initial=True)

    def _framedump_publish_census(self, stats, initial=False):
        """Snapshot counters only; neither the lock nor capture waits on image I/O.

        Closed-window state is held by its bounded queue items, not an ever-growing
        epoch registry. A hung writer therefore retains at most queue_depth + 1
        old windows. Monotonic revisions prevent delayed publishers from replacing
        a newer snapshot in ShotRecorder.
        """
        with self._framedump_census_lock:
            reason = stats.get('_closed_reason', '')
            if not reason:
                return
            stats['_revision'] = int(stats.get('_revision', 0)) + 1
            fields = {key: int(stats.get(key, 0)) for key in
                      ('frames', 'preroll', 'preroll_ondisk', 'dropped', 'skipped',
                       'saved', 'indexed', 'pending', 'discarded')}
            fields.update({key: int(stats.get(key, -1)) for key in
                           ('first_idx', 'last_idx', 'first_saved_idx', 'last_saved_idx')})
            fields.update(dir=stats.get('_dir', ''), session=stats.get('_session', ''),
                          reason=reason, census_revision=stats['_revision'],
                          census_complete=fields['pending'] == 0)
            epoch = int(stats.get('epoch', 0))
        # Keep frames/idx as ACCEPTED-work fields for existing readers. saved and
        # its index bounds are raw files actually written; discarded is the
        # accepted subset subsequently lost. frames = saved + discarded + pending.
        if initial or fields['census_complete']:
            try:
                logger.error('FRAMEDUMP PRESS WINDOW: epoch=%d frames=%d preroll=%d dropped=%d '
                             'skipped=%d idx=%d..%d reason=%s -> %s preroll_ondisk=%d '
                             'saved=%d indexed=%d pending=%d discarded=%d census_complete=%d',
                             epoch, fields['frames'], fields['preroll'], fields['dropped'],
                             fields['skipped'], fields['first_idx'], fields['last_idx'],
                             reason, fields['dir'], fields['preroll_ondisk'], fields['saved'],
                             fields['indexed'], fields['pending'], fields['discarded'],
                             int(fields['census_complete']))
            except Exception:
                pass
        rec = getattr(self, '_shot_records', None)
        if rec is not None:
            try:
                rec.note_frames(epoch, **fields)
            except Exception:
                pass

    def _framedump_finish_item(self, item):
        """A diagnostic accounting failure must not terminate the image worker."""
        try:
            self._framedump_account_item(item)
        except Exception as exc:
            try:
                logger.error('FRAMEDUMP census accounting failed: %s', type(exc).__name__)
            except Exception:
                pass

    def _framedump_account_item(self, item):
        """Account one accepted item exactly once, against its originating window."""
        try:
            idx, _frame, info = item
        except (TypeError, ValueError):
            # Legacy diagnostic tests may seed sentinel objects in the queue.
            self._framedump_dropped = int(getattr(self, '_framedump_dropped', 0)) + 1
            return
        stats = info.get('_framedump_stats')
        if stats is None:
            if not info.get('_framedump_raw_saved', False):
                self._framedump_dropped = int(getattr(self, '_framedump_dropped', 0)) + 1
            return
        with self._framedump_census_lock:
            if info.get('_framedump_accounted', False):
                return
            info['_framedump_accounted'] = True
            stats['pending'] -= 1
            if info.get('_framedump_raw_saved', False):
                stats['saved'] += 1
                stats['indexed'] += int(bool(info.get('_framedump_indexed', False)))
                if stats['first_saved_idx'] < 0:
                    stats['first_saved_idx'] = idx
                stats['last_saved_idx'] = idx
            else:
                stats['discarded'] += 1
                stats['dropped'] += 1
                self._framedump_dropped += 1
                self._framedump_press_dropped += 1
            closed = bool(stats.get('_closed_reason'))
        if closed:
            self._framedump_publish_census(stats)

    def _framedump_press_active(self, now):
        """Expire only the observed window, never a replacement opened concurrently."""
        with self._framedump_census_lock:
            until = float(getattr(self, '_framedump_press_until', 0.0) or 0.0)
            if until <= 0.0:
                return False
            if now <= until:
                return True
            expired = self._framedump_press_stats
        self._framedump_close_press_stats('window_end', expected_stats=expired)
        with self._framedump_census_lock:
            return self._framedump_press_until >= now and self._framedump_press_until > 0.0

    def _framedump_offer(self, frame, info, now, preroll=False, expected_stats=None):
        """Bounded nonblocking handoff; reserve accounting before publishing work."""
        with self._framedump_census_lock:
            stats = getattr(self, '_framedump_press_stats', None)
            q = getattr(self, '_framedump_q', None)
            if q is None or stats is None or stats.get('_closed_reason'):
                return False
            if expected_stats is not None and stats is not expected_stats:
                return False
            # As in legacy mode, saturation must be tested BEFORE an optional
            # capture-isolation copy. Never copy a full frame just to discard it.
            if self._framedump_count >= self._framedump_max or q.full():
                stats['dropped'] += 1
                self._framedump_dropped += 1
                self._framedump_press_dropped += 1
                return False
            idx = self._framedump_count
            info = dict(info)
            info['epoch'] = int(stats.get('epoch', 0))
            info['preroll'] = 1 if preroll else 0
            info['_framedump_stats'] = stats
            if getattr(self, '_framedump_press_copy', False) and frame is not None:
                frame = frame.copy()
            # The writer takes this same SHORT metadata lock after I/O. It must
            # never finish an item before its pending reservation exists.
            try:
                q.put_nowait((idx, frame, info))
            except queue.Full:
                self._framedump_dropped += 1
                self._framedump_press_dropped += 1
                stats['dropped'] += 1
                return False
            self._framedump_count = idx + 1
            self._framedump_press_frames += 1
            stats['frames'] += 1
            stats['pending'] += 1
            if preroll:
                stats['preroll'] += 1
            if stats['first_idx'] < 0:
                stats['first_idx'] = idx
            stats['last_idx'] = idx
            return True

    def _framedump_crop_frame(self, frame):
        """The shooter patch (player-anchor plate, +- margin), or the frame unchanged.

        Runs on the WRITER thread.  The anchor is whatever ``player_anchor`` already
        found on the detect thread; nothing is searched for here.
        """
        if str(getattr(self, '_framedump_crop', '')) != 'shooter':
            return frame
        try:
            import player_anchor as _pa
            last = getattr(_pa.ANCHOR, '_last', None)
            if not last:
                return frame
            icon_x, icon_y = float(last[0]), float(last[1])
            h, w = frame.shape[:2]
            cw, ch = int(self._framedump_crop_w), int(self._framedump_crop_h)
            # The shooter stands ABOVE his own nameplate, so the patch is centred on the
            # plate in x and reaches upward from it in y (the same geometry the anchor
            # uses to predict the meter box).
            x0 = int(max(0, min(w - 1, icon_x - cw * 0.5)))
            y0 = int(max(0, min(h - 1, icon_y - ch * 0.75)))
            x1 = int(min(w, x0 + cw))
            y1 = int(min(h, y0 + ch))
            if x1 - x0 < 16 or y1 - y0 < 16:
                return frame
            return frame[y0:y1, x0:x1]
        except Exception:
            return frame

    def _framedump_disk_status(self):
        """Return ``(ok, free_bytes, floor_bytes, error)`` for the dump volume.

        This runs only at initialization and on the diagnostic writer thread.  A failed query is
        unsafe for a best-effort bulk writer, so it disables framedump rather than guessing that
        storage is available.  Capture and automation remain live.
        """
        absolute_floor = int(getattr(self, '_framedump_min_free_bytes', 10 * (1024 ** 3)))
        try:
            usage = shutil.disk_usage(self._framedump_dir)
            percent_floor = int(
                int(usage.total) * float(getattr(self, '_framedump_min_free_pct', 2.0)) / 100.0)
            floor_bytes = max(absolute_floor, percent_floor)
            free_bytes = int(usage.free)
            return free_bytes >= floor_bytes, free_bytes, floor_bytes, ''
        except Exception as exc:
            return False, None, absolute_floor, str(exc)

    # THE FRAME DUMP'S LIFECYCLE LINES GO OUT AT **ERROR**, DELIBERATELY.
    #
    # They are logger.error only so they SURVIVE THE RELAY, not because anything is broken: the
    # native parent relays sidecar stderr through RemotePlaySession::onSidecarStderr, where every
    # WARNING line competes for ONE shared slot per second (kSidecarWarnThrottleMs = 1000 ms,
    # RemotePlaySession.cpp:2883-2889) while ERROR / CRITICAL bypass the throttle outright
    # (isError, :2798).  Under -Framedump the sidecar also emits DETDIAG at 25 lines/second, so a
    # one-shot `logger.warning("FRAMEDUMP disabled ...")` has roughly a 1-in-25 chance of being the
    # line that wins the slot -- which is precisely why session_20260914_204600 stopped dead at
    # 20:51:57 with NO warning anywhere in logs/orion_native.log.  The same relay comment block
    # (:2802-2841) already records two earlier instances of this exact loss (the probe diagnostics
    # and the authority-death pair), and the remedy there was the same: bypass the throttle.
    #
    # These lines stay OUT of the customer Activity feed regardless: UiNotificationPolicy.h rule 3
    # drops every raw `Sidecar:` line, and rule 4 classifies any 3+ `key=value` line as engineering
    # telemetry.  They reach logs/orion_native.log and nothing else.
    @staticmethod
    def _framedump_log(message, *args):
        logger.error(message, *args)

    def _start_framedump(self):
        """Create (or RE-create) the async frame-dump writer; idempotent and best-effort.

        WHY THIS IS NOT INLINE IN __init__ ANY MORE.  stop() calls _stop_framedump_writer(), which
        clears `_framedump_enabled`, nulls the queue and latches `_framedump_writer_stop` -- and
        start() only ever restarted the DETCSV sink (`self._start_detcsv()`), never this one.  So
        any in-process capture restart (a stream re-promotion, a reconnect) ended the frame dump
        for good, WITHOUT A SINGLE LOG LINE, while DETDIAG/DETCSV carried on and made the session
        look instrumented.  start() now re-arms the dump through this method.

        Permanent faults (missing/unwritable directory, disk below the floor) are NOT retried: they
        latch `_framedump_permanent_stop` so a re-arm cannot turn one bad target into a per-restart
        log storm.
        """
        if not getattr(self, '_framedump_env_enabled', False):
            return False
        if getattr(self, '_framedump_permanent_stop', ''):
            return False
        writer = getattr(self, '_framedump_writer', None)
        if writer is not None and writer.is_alive():
            return True
        # Generation 0 is the constructor's first arm; anything after that is a capture restart and
        # is worth a line, because "the dump came back" is exactly what was missing before.
        restarting = int(getattr(self, '_framedump_generation', 0)) > 0
        self._framedump_enabled = True
        self._framedump_disabled_reason = ''
        try:
            os.makedirs(self._framedump_dir, exist_ok=True)
        except Exception as exc:
            self._framedump_disable('output_directory_unavailable', detail=str(exc))
            return False
        space_ok, free_bytes, floor_bytes, space_error = self._framedump_disk_status()
        if not space_ok:
            self._framedump_disable(
                'low_disk' if not space_error else 'disk_check_failed',
                free_bytes=free_bytes, floor_bytes=floor_bytes, detail=space_error)
            return False
        stop_evt = getattr(self, '_framedump_writer_stop', None)
        if stop_evt is None:
            stop_evt = threading.Event()
            self._framedump_writer_stop = stop_evt
        # The event is LATCHED by the previous generation's teardown; a fresh writer that inherits
        # it set would return from its very first cooldown and die silently.
        stop_evt.clear()
        self._framedump_backoff_until = 0.0
        self._framedump_backoff_s = 0.0
        self._framedump_slow_streak = 0
        self._framedump_error_streak = 0
        self._framedump_index_failed = False
        self._framedump_generation = int(getattr(self, '_framedump_generation', 0)) + 1
        # ASYNC writer: PNG encoding is heavy; doing it inline on the capture thread
        # stalls the stream.  The one-slot DROP queue bounds both memory and stale work.
        self._framedump_q = queue.Queue(maxsize=self._framedump_queue_depth)
        self._framedump_writer = threading.Thread(
            target=self._framedump_writer_loop,
            name='framedump-writer-%d' % self._framedump_generation, daemon=True)
        self._framedump_writer.start()
        if restarting:
            self._framedump_log(
                'FRAMEDUMP: writer re-armed generation=%d frames=%d skipped=%d dir=%s',
                self._framedump_generation, int(getattr(self, '_framedump_count', 0)),
                int(getattr(self, '_framedump_skipped', 0)),
                getattr(self, '_framedump_dir', '?'))
        if getattr(self, '_framedump_press_window', False):
            self._framedump_log(
                'FRAMEDUMP PRESS WINDOW armed: press-%.0fms..release+%.0fms (hard cap %.0fms) '
                'full rate, format=%s q=%d crop=%s preroll=%d max=%d -> %s banner=%d',
                self._framedump_press_pre_ms, self._framedump_press_post_ms,
                self._framedump_press_max_ms, self._framedump_format,
                self._framedump_queue_depth, self._framedump_crop or 'none',
                self._framedump_preroll.maxlen if self._framedump_preroll is not None else 0,
                self._framedump_max, self._framedump_dir,
                int(getattr(self, '_framedump_press_banner', False)))
        return True

    def _framedump_state(self):
        """One word for the heartbeat/teardown lines: what the dump is doing right now."""
        if getattr(self, '_framedump_permanent_stop', ''):
            return 'disabled:' + str(self._framedump_permanent_stop)
        if not getattr(self, '_framedump_enabled', False):
            return 'stopped:' + (str(getattr(self, '_framedump_disabled_reason', '')) or 'idle')
        if float(getattr(self, '_framedump_backoff_until', 0.0) or 0.0) > time.perf_counter():
            return 'backoff'
        if getattr(self, '_framedump_press_window', False):
            # "idle" here is the HEALTHY state between shots, not a stall -- say which,
            # or the next reader of a heartbeat repeats the 09-15 stopped:idle scare.
            return ('press_window' if float(getattr(self, '_framedump_press_until', 0.0) or 0.0)
                    > time.perf_counter() else 'armed:between_presses')
        return 'active'

    def _framedump_heartbeat(self, now):
        """Emit `FRAMEDUMP: ...` once a minute so a STALLED dump is visible within a minute.

        The old instrument was one-shot by construction: it announced itself on frame 0, announced
        its cap, and otherwise spoke only when it died -- into a throttled relay.  A session could
        therefore be six minutes of frames followed by an hour of silence that reads EXACTLY like a
        healthy dump.  `last_write_age_s` is the field that distinguishes them.
        """
        interval = float(getattr(self, '_framedump_heartbeat_s', 60.0) or 0.0)
        if interval <= 0.0:
            return
        last = float(getattr(self, '_framedump_heartbeat_ts', 0.0) or 0.0)
        if last <= 0.0:
            self._framedump_heartbeat_ts = now
            return
        if (now - last) < interval:
            return
        self._framedump_heartbeat_ts = now
        write_ts = float(getattr(self, '_framedump_last_write_ts', 0.0) or 0.0)
        age = (now - write_ts) if write_ts > 0.0 else -1.0
        self._framedump_log(
            'FRAMEDUMP: frames=%d last_write_ms=%.0f last_write_age_s=%.1f dropped=%d skipped=%d '
            'backoff_s=%.1f state=%s windows=%d window_frames=%d window_dropped=%d dir=%s',
            int(getattr(self, '_framedump_count', 0)),
            float(getattr(self, '_framedump_last_write_ms', 0.0) or 0.0), age,
            int(getattr(self, '_framedump_dropped', 0)),
            int(getattr(self, '_framedump_skipped', 0)),
            float(getattr(self, '_framedump_backoff_s', 0.0) or 0.0),
            self._framedump_state(),
            int(getattr(self, '_framedump_press_windows', 0)),
            int(getattr(self, '_framedump_press_frames', 0)),
            int(getattr(self, '_framedump_press_dropped', 0)),
            getattr(self, '_framedump_dir', '?'))

    def _framedump_back_off(self, reason, detail=''):
        """Pause the dump for a bounded, doubling cooldown instead of killing it.

        Returns the cooldown in seconds.  The producer consults `_framedump_backoff_until` and
        SKIPS frames (without consuming an index) until it expires; the first fast write after that
        clears the streak.  Nothing here disables capture, detection, automation -- or the dump.
        """
        streak = int(getattr(self, '_framedump_slow_streak', 0)) + 1
        self._framedump_slow_streak = streak
        previous = float(getattr(self, '_framedump_backoff_s', 0.0) or 0.0)
        cap = float(getattr(self, '_framedump_backoff_max_s', 15.0) or 15.0)
        cooldown = min(cap, previous * 2.0 if previous > 0.0 else 1.0)
        self._framedump_backoff_s = cooldown
        self._framedump_backoff_until = time.perf_counter() + cooldown
        self._framedump_log(
            'FRAMEDUMP: backing off %.1fs (%s%s) streak=%d frames=%d skipped=%d dir=%s; '
            'the dump RESUMES on its own when writes are fast again',
            cooldown, reason, (': ' + detail) if detail else '', streak,
            int(getattr(self, '_framedump_count', 0)),
            int(getattr(self, '_framedump_skipped', 0)),
            getattr(self, '_framedump_dir', '?'))
        return cooldown

    def _framedump_note_healthy_write(self, elapsed_s):
        """Record a fast write; announce the recovery exactly once per back-off episode."""
        self._framedump_last_write_ms = float(elapsed_s) * 1000.0
        self._framedump_last_write_ts = time.perf_counter()
        self._framedump_error_streak = 0
        if int(getattr(self, '_framedump_slow_streak', 0)) <= 0:
            return
        self._framedump_log(
            'FRAMEDUMP: writes are fast again (%.0fms) after %d slow write(s); resuming '
            'frames=%d skipped=%d dir=%s',
            self._framedump_last_write_ms, int(self._framedump_slow_streak),
            int(getattr(self, '_framedump_count', 0)),
            int(getattr(self, '_framedump_skipped', 0)),
            getattr(self, '_framedump_dir', '?'))
        self._framedump_slow_streak = 0
        self._framedump_backoff_s = 0.0
        self._framedump_backoff_until = 0.0

    def _framedump_disable(self, reason, free_bytes=None, floor_bytes=None, detail=''):
        """Permanently silence this diagnostic writer for the process generation.

        Reserved for faults the dump cannot write its way out of: an unusable output directory, a
        volume under the free-space floor, a failed disk query, a PNG write the imaging library
        REFUSED (disk full / bad path), or repeated hard writer exceptions.  A merely SLOW write is
        handled by _framedump_back_off() instead and never reaches here.

        The disable flag is set before logging so an in-flight producer stops enqueueing even if a
        log handler is slow.  This method never disables capture, detection, or automation.
        """
        was_enabled = bool(getattr(self, '_framedump_enabled', False))
        self._framedump_enabled = False
        self._framedump_disabled_reason = str(reason or 'unknown')
        self._framedump_permanent_stop = self._framedump_disabled_reason
        stop_evt = getattr(self, '_framedump_writer_stop', None)
        if stop_evt is not None:
            stop_evt.set()
        if not was_enabled:
            return
        if free_bytes is not None and floor_bytes is not None:
            self._framedump_log(
                'FRAMEDUMP disabled (%s): target=%s free=%.2f GiB required=%.2f GiB frames=%d; '
                'live capture remains active',
                self._framedump_disabled_reason,
                getattr(self, '_framedump_dir', '?'),
                float(free_bytes) / float(1024 ** 3),
                float(floor_bytes) / float(1024 ** 3),
                int(getattr(self, '_framedump_count', 0)))
        elif detail:
            self._framedump_log(
                'FRAMEDUMP disabled (%s): target=%s error=%s frames=%d; live capture remains active',
                self._framedump_disabled_reason,
                getattr(self, '_framedump_dir', '?'), detail,
                int(getattr(self, '_framedump_count', 0)))
        else:
            self._framedump_log(
                'FRAMEDUMP disabled (%s): frames=%d; live capture remains active',
                self._framedump_disabled_reason, int(getattr(self, '_framedump_count', 0)))

    @staticmethod
    def _framedump_cooldown_s(write_elapsed_s, max_duty):
        """Writer-only yield needed to keep average diagnostic encode/write duty bounded."""
        try:
            elapsed = max(0.0, float(write_elapsed_s))
            duty = max(0.01, min(1.0, float(max_duty)))
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, (elapsed / duty) - elapsed)

    def _framedump_encoding(self):
        """(extension, imwrite params) for this dump.

        MEASURED on this workstation, 1080p BGR: PNG default 49.2 ms / 1108 KiB, PNG
        level-1 93.6 ms / 1357 KiB, JPEG-90 4.27 ms / 83 KiB; a 400x500 crop is 11.5 ms
        PNG vs 0.57 ms / 8.6 KiB JPEG-90.  Only JPEG fits a 60 fps window, which is why
        press-window mode defaults to it; legacy dumps keep PNG byte-for-byte.
        """
        if str(getattr(self, '_framedump_format', 'png')) == 'jpg':
            return '.jpg', [cv2.IMWRITE_JPEG_QUALITY,
                            int(getattr(self, '_framedump_jpeg_quality', 90))]
        return '.png', []

    def _framedump_discard_pending(self):
        """Release queued full-resolution arrays without writing them; never waits."""
        q = getattr(self, '_framedump_q', None)
        if q is None:
            return 0
        discarded = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            if item is not None:
                discarded += 1
                self._framedump_finish_item(item)
        return discarded

    def _framedump_info(self, frame, result, now):
        """The per-frame index row this dump carries alongside the pixels.

        WALL-CLOCK OF THE FRAME, not of the write. Without this the dump is index-only,
        and index*interval is NOT a time axis: the throttle jitters against the capture
        cadence, and on a full queue we DROP without consuming the index, so consecutive
        indices can be one frame apart or ten. Any offline rate measurement (meter fill
        ms, appear->tip) read off the index is therefore silently wrong -- and looks
        perfectly plausible.
        """
        return {
            't': now,
            'wall': time.time(),
            'det': bool(getattr(result, 'detected', False)) if result else False,
            'fill': float(getattr(result, 'fill_pct', 0.0) or 0.0) if result else 0.0,
            'conf': float(getattr(result, 'confidence', 0.0) or 0.0) if result else 0.0,
            'rej': (getattr(result, 'rejection_reason', '') if result else 'no_result') or '',
            'gc': float(getattr(result, 'green_window_center_pct', -1.0) or -1.0) if result else -1.0,
            'gconf': float(getattr(result, 'green_window_confidence', 0.0) or 0.0) if result else 0.0,
            'bbox': getattr(result, 'bbox', None) if result else None,
            'gs': int(getattr(result, 'green_window_start_row', -1) or -1) if result else -1,
            'ge': int(getattr(result, 'green_window_end_row', -1) or -1) if result else -1,
            'park': bool(getattr(self._meter_detector, 'last_debug', {}).get('park', False)) if self._meter_detector else False,
        }

    def _dump_frame(self, frame, result, now=None):
        """ENQUEUE a frame for the async writer (opt-in, ORION_FRAMEDUMP=1). The capture thread does
        only a throttle check plus a NON-BLOCKING one-slot handoff.  Queue saturation is tested
        before ``frame.copy()`` so backpressure does not even copy a 1080p array.  If the writer is
        behind we DROP (count not consumed).

        `now` is the producer's monotonic clock; tests drive a synthetic feed through it so a
        press-window run does not have to take 30 real seconds."""
        try:
            now = time.perf_counter() if now is None else float(now)
            # The heartbeat runs FIRST and unconditionally: a dump that has stopped (capped,
            # disabled, backing off, or waiting for the gameplay gate) is exactly the case the
            # owner needs to see in the log, and the old code returned before saying anything.
            self._framedump_heartbeat(now)
            if not getattr(self, '_framedump_enabled', False) \
                    or self._framedump_count >= self._framedump_max \
                    or frame is None or self._framedump_q is None:
                return
            # SKIP, don't die: the writer asked for a cooldown after a slow write.  Frames missed
            # here do not consume an index, so the dump simply thins out and then recovers.
            backoff_until = float(getattr(self, '_framedump_backoff_until', 0.0) or 0.0)
            if backoff_until > 0.0:
                if now < backoff_until:
                    self._framedump_skipped = int(getattr(self, '_framedump_skipped', 0)) + 1
                    stats = getattr(self, '_framedump_press_stats', None)
                    if stats is not None:
                        stats['skipped'] = int(stats.get('skipped', 0)) + 1
                    return
                self._framedump_backoff_until = 0.0
            # ---------------------------------------------------------------- #
            # PRESS-WINDOW MODE: every frame inside a window, nothing outside one.
            # No interval throttle (the throttle is what thinned the 09-15 drill to
            # 826/1421) and no gameplay gate (a press IS gameplay).
            # ---------------------------------------------------------------- #
            if getattr(self, '_framedump_press_window', False):
                info = self._framedump_info(frame, result, now)
                written = False
                if self._framedump_press_active(now):
                    written = bool(self._framedump_offer(frame, info, now))
                ring = getattr(self, '_framedump_preroll', None)
                if ring is not None and ring.maxlen:
                    # REFERENCE ONLY -- capture already isolates each frame, so the ring
                    # costs a pointer, never a 1080p copy on this thread.
                    #
                    # [ORION_FRAMEDUMP_PREROLL_CONTINUOUS 2026-09-17] Fed on EVERY frame,
                    # inside a window as well as between them, so a press that lands while
                    # the previous window is still draining still has its press-200 ms
                    # context.  The legacy `written` flag means accepted and keeps a frame from being queued
                    # twice; see framedump_press_open.
                    with self._framedump_census_lock:
                        ring.append((now, frame, info, written))
                return
            # GAMEPLAY GATE (2026-08-31). The dump used to start at launch, so connect + warmup ate
            # the whole budget: the 2026-08-30_201954 dump spent all 9000 frames before the owner
            # took a single shot (13 detections in 9000 rows) and the session's six failures had NO
            # pixels at all. Arm the dump on the first frame the reader calls gameplay-eligible and
            # keep dumping from then on -- that still captures the PREVIOUS shot's leftover meter and
            # the inter-shot idle (both load-bearing for ghost/onset analysis), while skipping the
            # menu/preview/warmup that carries no meter. ORION_FRAMEDUMP_GATE=0 restores dumping
            # from launch. Fail-open: any error arms the dump rather than losing the capture.
            if not self._framedump_armed:
                try:
                    eligible = bool(getattr(result, "gameplay_eligible", None)
                                    if result is not None and hasattr(result, "gameplay_eligible")
                                    else (result is not None and bool(getattr(result, "detected", False))))
                except Exception:
                    eligible = True
                if not eligible:
                    return
                self._framedump_armed = True
            if now - self._framedump_last_ts < self._framedump_interval:
                return
            # Most importantly this check precedes frame.copy().  The old put_nowait-only guard
            # copied every full-resolution frame while a 64-frame queue was already saturated.
            if self._framedump_q.full():
                self._framedump_dropped += 1
                return
            # Capture the epoch clock on the producer/capture thread.  The async
            # PNG writer can trail this point by hundreds of milliseconds (and,
            # under a full queue, more than a second), so stamping time.time() in
            # the writer makes outcome-banner joins look precise while pairing
            # them with the wrong shot.  Keep both clocks in frames.csv: t_wall is
            # this handoff instant; write_wall is writer/queue observability only.
            idx = self._framedump_count
            info = self._framedump_info(frame, result, now)
            try:
                self._framedump_q.put_nowait((idx, frame.copy(), info))
            except queue.Full:
                self._framedump_dropped += 1   # writer behind -> drop (do NOT consume the index)
                return
            self._framedump_last_ts = now
            self._framedump_count += 1
            if idx == 0:
                self._framedump_log("FRAMEDUMP active -> %s (every %ss, max %d, raw_only=%s, "
                                    "async queue=%d duty<=%.0f%% min_free>=%.1fGiB)",
                                    self._framedump_dir, self._framedump_interval,
                                    self._framedump_max, self._framedump_raw_only,
                                    self._framedump_queue_depth,
                                    self._framedump_max_duty * 100.0,
                                    self._framedump_min_free_bytes / float(1024 ** 3))
            if self._framedump_count == self._framedump_max:
                self._framedump_log("FRAMEDUMP queued %d frames -> %s (dropped=%d skipped=%d); "
                                    "the cap is reached and no further frames will be written",
                                    self._framedump_max, self._framedump_dir,
                                    self._framedump_dropped,
                                    int(getattr(self, '_framedump_skipped', 0)))
        except Exception as exc:
            logger.warning("frame dump enqueue error: %s", exc)

    def _reclaim_dshow_route_if_invalid(self, now: float) -> None:
        """Periodically retry the configured DirectShow route while the card is on MSMF.

        WHY THIS EXISTS. `_guard_capture_latency_route` fails closed on any route
        that is not DSHOW-at-the-expected-index, which is correct: MSMF's index
        namespace is a different enumeration, so nothing attests WHICH device
        "MSMF index 0" is, and the negotiated mode arrives without a FOURCC (the
        observed MSMF open was 1080p30 with a blank fourcc, against a 60fps
        timing budget). Adopting MSMF would be unsound, and this method
        deliberately does NOT do that.

        The problem was that the failure had no way OUT. A HEALTHY MSMF backend
        satisfies neither of the existing reopen triggers -- the 2s card retry
        needs `_frame_backend is None`, and the self-heal detach needs
        `is_healthy()` False -- so it produced frames forever while every guard
        call failed and the bot stayed benched for the life of the process.
        Observed 2026-08-26: the PS5 was asleep, the dark HDMI feed gave
        DirectShow no usable first frame, `_open` fell through to MSMF, and
        timing was disabled from that moment on.

        The fix is to release the card and let the EXISTING DSHOW-first `_open`
        run again. Recovery needs no new trust logic: when DSHOW at the expected
        index comes back, the guard's own `matches` branch re-earns COLD
        authority (`capture_route_recovered_cold`). Worst case DSHOW is still
        dead, we reopen on MSMF and probe again after the cooldown -- and timing
        is already disabled in that state, so the only cost is a brief preview
        blink. Never calls `_replace_latency_authority` itself; it only detaches.
        """
        if not getattr(self, '_cc_mode', False):
            return
        if not getattr(self, '_capture_route_currently_invalid', False):
            return
        backend = self._frame_backend
        if backend is None:
            return
        try:
            cooldown = float(os.environ.get('ORION_CAPTURE_DSHOW_RECLAIM_S', '15') or 15.0)
        except (TypeError, ValueError):
            cooldown = 15.0
        if cooldown <= 0.0:
            return          # explicitly disabled
        if str(os.environ.get('ORION_CAPTURE_API', '')).strip().lower() == 'msmf':
            return          # the operator asked for MSMF; do not fight them
        try:
            api = str(backend.active_route()[0] or '').upper()
        except Exception:
            return
        if api == 'DSHOW':
            return          # already on the configured API; the guard decides the rest
        last = float(getattr(self, '_cc_route_reclaim_last', 0.0) or 0.0)
        if last and (now - last) < cooldown:
            return
        self._cc_route_reclaim_last = now
        logger.warning(
            'Capture route is %s, not the configured DirectShow route, so shot timing is '
            'fail-closed; releasing the card to retry DirectShow', api or 'UNKNOWN')
        try:
            backend.stop()
        except Exception:
            pass
        self._frame_backend = None
        self._frame_backend_mode = 'capture'
        # Land the reopen after the card's handle-release window rather than
        # immediately, or the reopen races the release and lands on MSMF again.
        self._cc_last_retry = now + 1.0

    def _framedump_write_index(self, idx, info):
        """Append this frame's row to frames.csv (the dump's time axis + reader verdict).

        Best-effort and self-silencing: an index-file problem must never cost us the
        PNG stream, so every failure disables the index and leaves the dump running.
        `t_ms` is relative to the first dumped frame, so it is directly usable as the
        x-axis for fill-rate work without needing to know the session start.
        """
        if getattr(self, '_framedump_index_failed', False):
            return False
        try:
            fh = getattr(self, '_framedump_index_fh', None)
            if fh is None:
                # APPEND, never truncate. The launcher hands the same
                # ORION_FRAMEDUMP_DIR to every sidecar generation, and a restart
                # mid-session (a failed stream promotion will do it) restarts the
                # frame index at 0. Opening 'w' silently erased the previous
                # generation's rows -- observed 2026-08-26, where the generation
                # that captured the route mismatch was lost. t_wall disambiguates
                # generations that both restart t_ms at 0.  New files also carry
                # write_wall: its presence identifies the capture-clock schema.
                # If an old process generation already created this path, retain
                # its legacy row width rather than corrupting the CSV mid-file.
                _pth = os.path.join(self._framedump_dir, 'frames.csv')
                _new = not os.path.exists(_pth) or os.path.getsize(_pth) == 0
                _capture_clock_schema = _new
                if not _new:
                    try:
                        with open(_pth, 'r', encoding='utf-8', errors='replace') as _rfh:
                            _capture_clock_schema = 'write_wall' in _rfh.readline().strip().split(',')
                    except OSError:
                        _capture_clock_schema = False
                fh = open(_pth, 'a', encoding='utf-8', newline='')
                if _new:
                    fh.write('idx,t_ms,t_wall,detected,fill_pct,conf,green_center_pct,'
                             'bbox_x,bbox_y,bbox_w,bbox_h,rejection,write_wall\n')
                self._framedump_index_fh = fh
                self._framedump_index_capture_clock_schema = _capture_clock_schema
                self._framedump_index_t0 = float(info.get('t', 0.0))
            t0 = getattr(self, '_framedump_index_t0', 0.0)
            t_ms = (float(info.get('t', 0.0)) - t0) * 1000.0
            bbox = info.get('bbox') or (0, 0, 0, 0)
            try:
                bx, by, bw, bh = (int(v) for v in bbox)
            except (TypeError, ValueError):
                bx = by = bw = bh = 0
            rej = str(info.get('rej', '') or '').replace(',', ';')
            write_wall = time.time()
            if getattr(self, '_framedump_index_capture_clock_schema', False):
                capture_wall = info.get('wall')
                if capture_wall is None:
                    # Defensive compatibility for direct/unit callers.  The live
                    # producer always supplies wall at enqueue time.
                    capture_wall = write_wall
                fh.write(f"{idx},{t_ms:.2f},{float(capture_wall):.6f},"
                         f"{int(bool(info['det']))},{info['fill']:.2f},{info['conf']:.3f},"
                         f"{info['gc']:.2f},{bx},{by},{bw},{bh},{rej},{write_wall:.6f}\n")
            else:
                # Legacy append: t_wall historically meant writer time.  Keeping
                # the old width is safer than producing a malformed mixed-schema
                # CSV; timestamped launcher sessions always start with v2 above.
                fh.write(f"{idx},{t_ms:.2f},{write_wall:.3f},"
                         f"{int(bool(info['det']))},{info['fill']:.2f},{info['conf']:.3f},"
                         f"{info['gc']:.2f},{bx},{by},{bw},{bh},{rej}\n")
            fh.flush()
            return True
        except Exception as exc:
            self._framedump_index_failed = True
            logger.warning("frame dump index disabled (%s); PNG stream continues", exc)
            return False

    def _framedump_write_item(self, item):
        try:
            return self._framedump_write_item_impl(item)
        finally:
            self._framedump_finish_item(item)

    def _framedump_write_item_impl(self, item):
        """Write one accepted diagnostic item; return whether the worker may continue.

        All potentially blocking work in this method runs on ``framedump-writer``.  A disk-query
        failure or a REFUSED image write disables only framedump, so a bad diagnostic target cannot
        keep applying pressure to the live pipeline; a merely SLOW write -- or a transient
        exception -- costs a bounded cooldown (_framedump_back_off) and nothing else.  Capture,
        detection and automation are never touched by either path.
        """
        if not getattr(self, '_framedump_enabled', False):
            return False
        space_ok, free_bytes, floor_bytes, space_error = self._framedump_disk_status()
        if not space_ok:
            self._framedump_disable(
                'low_disk' if not space_error else 'disk_check_failed',
                free_bytes=free_bytes, floor_bytes=floor_bytes, detail=space_error)
            return False
        started = time.perf_counter()
        try:
            idx, frame, info = item
            # PRESS-WINDOW naming carries the SHOT-GATE EPOCH, which is the join key every
            # shot record, banner verdict and release oracle already uses -- so a frame file
            # names its own shot without a second index.
            epoch = int(info.get('epoch', 0) or 0)
            if epoch > 0:
                base = os.path.join(self._framedump_dir,
                                    f"ep{epoch}_f{idx:06d}_{int(bool(info['det']))}")
                frame = self._framedump_crop_frame(frame)
            else:
                base = os.path.join(self._framedump_dir, f"f{idx:05d}_{int(bool(info['det']))}")
            ext, params = self._framedump_encoding()
            # raw = exactly what the detector received. cv2 reports disk/full/path failures by
            # returning False on several builds, so an unchecked call can create an index row for a
            # file that does not exist.
            if not cv2.imwrite(base + '_raw' + ext, frame, params):
                self._framedump_disable('raw_png_write_failed')
                return False
            info['_framedump_raw_saved'] = True
            info['_framedump_indexed'] = bool(self._framedump_write_index(idx, info))
            if not self._framedump_raw_only:
                ann = frame.copy()
                bbox = info.get('bbox')
                if info['det'] and bbox:
                    x, y, w, h = (int(v) for v in bbox)
                    cv2.rectangle(ann, (x, y), (x + w, y + h), (255, 0, 255), 2)
                    cv2.line(ann, (x - 6, y), (x + w + 6, y), (0, 255, 255), 1)
                    gs, ge = info.get('gs', -1), info.get('ge', -1)
                    if gs >= 0 and ge >= 0:
                        top, bot = sorted((gs, ge))
                        cv2.rectangle(ann, (x - 4, top), (x + w + 4, bot), (0, 255, 0), 2)
                label = (f"det={int(bool(info['det']))} PARK={int(bool(info.get('park')))} "
                         f"fill={info['fill']:.0f}% conf={info['conf']:.2f} "
                         f"g={info['gc']:.0f}/{info['gconf']:.2f} rej={info['rej']} "
                         f"{frame.shape[1]}x{frame.shape[0]}")
                cv2.putText(ann, label, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(ann, label, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
                if not cv2.imwrite(base + '_overlay' + ext, ann, params):
                    self._framedump_disable('overlay_png_write_failed')
                    return False
        except Exception as exc:
            # A hard exception may still be transient (a locked path, a momentary I/O error), so
            # back off and retry; only a persistent run of them gives the dump up.
            streak = int(getattr(self, '_framedump_error_streak', 0)) + 1
            self._framedump_error_streak = streak
            if streak >= int(getattr(self, '_framedump_max_error_streak', 5)):
                self._framedump_disable('writer_error', detail='%s (x%d)' % (exc, streak))
                return False
            self._framedump_back_off('writer_error', detail=str(exc))
            return bool(getattr(self, '_framedump_enabled', False))

        elapsed = time.perf_counter() - started
        if elapsed > float(getattr(self, '_framedump_max_write_s', 0.250)):
            # NOT a disable.  See _framedump_back_off: one slow write is a cooldown, not the end of
            # the instrument.  The frame just written is KEPT -- it completed.
            self._framedump_last_write_ms = elapsed * 1000.0
            self._framedump_last_write_ts = time.perf_counter()
            self._framedump_error_streak = 0
            self._framedump_back_off(
                'slow_write', detail='%.0fms > %.0fms limit'
                % (elapsed * 1000.0, float(getattr(self, '_framedump_max_write_s', 0.250)) * 1000.0))
            # Drop whatever the producer queued during the stall rather than writing stale frames
            # back-to-back the moment the cooldown starts.
            self._framedump_discard_pending()
            return bool(getattr(self, '_framedump_enabled', False))
        self._framedump_note_healthy_write(elapsed)

        dropped = int(getattr(self, '_framedump_dropped', 0))
        reported = int(getattr(self, '_framedump_reported_dropped', 0))
        if dropped != reported:
            self._framedump_reported_dropped = dropped
            logger.warning('FRAMEDUMP backpressure: dropped=%d (+%d); capture thread was not blocked',
                           dropped, max(0, dropped - reported))

        # THE DUTY COOLDOWN IS THE WRONG TOOL INSIDE A PRESS WINDOW.  It exists to keep an
        # UNBOUNDED session-long dump from competing with capture; a press window is bounded
        # (~60 frames, ~1 s, then silence until the next press), so sleeping 3x the encode
        # time between frames does not bound anything -- it just loses the frames the window
        # was opened to get.  Average duty in this mode is governed by the shot rate.
        cooldown = 0.0 if getattr(self, '_framedump_press_window', False) \
            else self._framedump_cooldown_s(
                elapsed, getattr(self, '_framedump_max_duty', 0.25))
        stop_evt = getattr(self, '_framedump_writer_stop', None)
        if cooldown > 0.0:
            if stop_evt is not None:
                if stop_evt.wait(cooldown):
                    return False
            else:
                time.sleep(cooldown)
        return bool(getattr(self, '_framedump_enabled', False))

    def _framedump_writer_loop(self):
        """Daemon: pop and write diagnostics off-thread at low scheduling priority."""
        if os.name == 'nt':
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # THREAD_PRIORITY_LOWEST. Failure is non-fatal; queue/duty guards remain active.
                kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -2)
            except Exception:
                pass
        q = self._framedump_q
        while q is not None:
            try:
                item = q.get()
            except Exception as exc:
                self._framedump_disable('queue_read_failed', detail=str(exc))
                return
            if item is None:
                return
            if not self._framedump_write_item(item):
                self._framedump_discard_pending()
                return

    def _stop_framedump_writer(self, timeout=0.5):
        """Stop the diagnostic worker without draining queued PNG work.

        This used to be the quietest way a session lost its pixels: stop() called it, start() never
        re-armed the dump (only DETCSV), and NOTHING was logged -- so a mid-session capture restart
        produced a session that looked instrumented and had no frames after the restart.  Say so,
        and let _start_framedump() bring the writer back on the next start().
        """
        was_running = bool(getattr(self, '_framedump_enabled', False)
                           or (getattr(self, '_framedump_writer', None) is not None
                               and self._framedump_writer.is_alive()))
        self._framedump_enabled = False
        stop_evt = getattr(self, '_framedump_writer_stop', None)
        if stop_evt is not None:
            stop_evt.set()
        q = getattr(self, '_framedump_q', None)
        if q is not None:
            self._framedump_discard_pending()
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
            except Exception:
                pass
        writer = getattr(self, '_framedump_writer', None)
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=max(0.0, float(timeout)))
        if writer is not None and writer.is_alive():
            self._framedump_log(
                'FRAMEDUMP writer still exiting after %.0fms; daemon teardown deferred '
                '(frames=%d dropped=%d skipped=%d dir=%s)',
                max(0.0, float(timeout)) * 1000.0,
                int(getattr(self, '_framedump_count', 0)),
                int(getattr(self, '_framedump_dropped', 0)),
                int(getattr(self, '_framedump_skipped', 0)),
                getattr(self, '_framedump_dir', '?'))
            return False
        if was_running:
            self._framedump_log(
                'FRAMEDUMP: writer stopped frames=%d dropped=%d skipped=%d state=%s dir=%s',
                int(getattr(self, '_framedump_count', 0)),
                int(getattr(self, '_framedump_dropped', 0)),
                int(getattr(self, '_framedump_skipped', 0)),
                self._framedump_state(), getattr(self, '_framedump_dir', '?'))
        self._framedump_writer = None
        self._framedump_q = None
        fh = getattr(self, '_framedump_index_fh', None)
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            self._framedump_index_fh = None
        return True

    def _record_detection_diagnostics(self, frame, result, *, _frame_wall_ms,
                                      _snap_source_frame_number, _snap_frame_pts,
                                      _snap_latency_estimator, _snap_integrity_generation,
                                      _snap_seq, _snap_frame_epoch_ms,
                                      _snap_source_identity, _snap_backend_frozen):
        """Best-effort diagnostics after publication, using the same captured frame.

        CSV formatting, frame-dump copies and optional animation analysis must
        never hold this frame's meter update behind display/research work, or
        revoke an already published measurement when a diagnostic fails.
        """
        try:
            # Full-rate per-frame row (direct file write, bypasses relay).
            if self._detcsv is not None:
                try:
                    _bb2 = (getattr(result, 'bbox', (0, 0, 0, 0)) if result else (0, 0, 0, 0)) or (0, 0, 0, 0)
                    _dbg = getattr(self._meter_detector, 'last_debug', None) or {}
                    self._detcsv.write((_DETCSV_ROW_FMT + '\n') % (
                        (time.perf_counter() - self._detcsv_t0) * 1000.0,
                        1 if (result and getattr(result, 'detected', False)) else 0,
                        float(getattr(result, 'fill_pct', 0.0) or 0.0) if result else 0.0,
                        float(getattr(result, 'confidence', 0.0) or 0.0) if result else 0.0,
                        int(_bb2[0]), int(_bb2[1]), int(_bb2[2]), int(_bb2[3]),
                        (getattr(result, 'rejection_reason', '') if result else 'no_result') or 'none',
                        float(getattr(result, 'green_window_center_pct', -1.0) or -1.0) if result else -1.0,
                        float(getattr(result, 'green_window_confidence', 0.0) or 0.0) if result else 0.0,
                        int(frame.shape[1]), int(frame.shape[0]),
                        _frame_wall_ms,
                        1 if self._should_feed_engine(result) else 0,
                        int(_dbg.get('stab_streak', -1)),
                        1 if _dbg.get('stab_tracking', False) else 0,
                        float(_dbg.get('stab_jump_px', -1.0)),
                        float(_dbg.get('acq_gate_px', -1.0)),
                        str(_dbg.get('stab_event', '') or 'none'),
                        str(_dbg.get('zone', '') or 'none'),
                        int(_dbg.get('roi_miss', -1)),
                        int(_dbg.get('cand_n', -1)),
                        int(_dbg.get('cand_size_ok', -1)),
                        int(_dbg.get('purity_rej', -1)),
                        float(_dbg.get('med_h', -1.0)),
                        float(_dbg.get('med_s', -1.0)),
                        int(_dbg.get('mem_left', -1)),
                        int(getattr(self, '_unique_frame_fps', 0) or 0),
                        float(getattr(self, '_duplicate_frame_pct', 0.0) or 0.0),
                        int(_dbg.get('anchor_found', 0)),
                        float(_dbg.get('anchor_score', -1.0)),
                        int(_dbg.get('anchor_x', -1)),
                        int(_dbg.get('anchor_y', -1)),
                        str(getattr(result, 'rise_state', '') or 'none') if result else 'none',
                        # decoder/capture frame number: joins CSV rows to framedump
                        # PNGs + exposes drop patterns (2026-07-04 forensics had NO
                        # way to align the two artifacts).
                        # Bind identity to the same pixels/wall_ms even when
                        # capture publishes another frame during detection.
                        int(_snap_source_frame_number or 0),
                        # pts (decoder PTS us) + rel_seq (latest release id) + mtr_phase
                        # (rise/frozen/plateau/none) -- the frozen-meter latency oracle keys.
                        int(_snap_frame_pts or 0),
                        int(_snap_latency_estimator.release_seq) if _snap_latency_estimator is not None else 0,
                        (_snap_latency_estimator.phase if _snap_latency_estimator is not None else 'none'),
                        # quality/staleness diagnostics (reader last_debug; -1/default
                        # until the compressed-path producers land -- schema is stable
                        # so offline tooling can be written against it now).
                        float(_dbg.get('q_frame', -1.0)),
                        float(_dbg.get('q_session', -1.0)),
                        float(_dbg.get('edge_curv', -1.0)),
                        float(_dbg.get('sig_width', -1.0)),
                        float(_dbg.get('ncc_margin', -1.0)),
                        float(_dbg.get('roi_sad', -1.0)),
                        int(_dbg.get('stale', 0)),
                        int(_dbg.get('valid', 1)),
                        float(_dbg.get('R_used', -1.0)),
                        int(getattr(self, '_last_frame_is_iframe', 0) or 0),
                        # det_* = the DETECTOR's rectangle, before the display-only
                        # ORION_READER_BOX_TIGHT reshape. x/y/w/h above are what is
                        # DRAWN; with the flag armed those two differ, and every
                        # box-stability statistic computed from x/y/w/h was measuring
                        # the presentation transform instead of detection. The reader
                        # publishes det_box only when it actually reshaped, so the
                        # fallback to _bb2 keeps the columns equal in mode 0.
                        *(int(v) for v in (_dbg.get('det_box') or _bb2)[:4]),
                        # Fill-ruler provenance consumed by the native phase-anchor
                        # fence.  Keep the raw coarse companion even when subpixel is
                        # selected so the next batch can measure scale transitions
                        # without reconstructing them from rounded fill_pct values.
                        float(getattr(result, 'raw_fill_pct', 0.0) or 0.0)
                            if result else 0.0,
                        str(getattr(result, 'fill_estimator_mode', '') or 'none')
                            if result else 'none',
                        int(getattr(result, 'fill_estimator_generation', 0) or 0)
                            if result else 0,
                        int(_snap_integrity_generation or 0),
                        # Authority diagnostics belong to this result and captured
                        # frame locals, never mutable reader/next-frame globals.
                        # raw_fed is eligibility,
                        # not a claim that telemetry/native accepted the sample.
                        int(getattr(result, 'gameplay_sample_epoch', 0) or 0)
                            if result else 0,
                        int(bool(getattr(result, 'gameplay_structure_verified', False)))
                            if result else 0,
                        int(getattr(result, 'gameplay_structure_epoch', 0) or 0)
                            if result else 0,
                        int(bool(self._is_raw_accepted(result))),
                        str(_dbg.get('stage', '') or 'none'),
                        int(_snap_seq or 0),
                        float(_snap_frame_epoch_ms or 0.0),
                        int(_snap_source_identity or 0),
                        int(bool(_snap_backend_frozen)),
                    ))
                except Exception:
                    pass
            # --- Detection diagnostic (opt-in via ORION_DETDIAG=1, throttled
            #     ~1/s). Surfaces the sidecar's REAL per-frame detection so a
            #     detection failure (fill stays 0 -> engine starves ->
            #     max_hold_safety) is distinguishable from a delivery failure.
            #     Logged at WARNING so it reaches orion_native.log via the
            #     (now WARNING-aware) stderr relay; gated so normal runs stay
            #     quiet. Includes the detector's rejection_reason + green band. ---
            if self._detdiag_enabled:
                _now_dbg = time.perf_counter()
                if _now_dbg - getattr(self, '_last_detect_log_ts', 0.0) >= self._detdiag_interval:
                    self._last_detect_log_ts = _now_dbg
                    try:
                        _det = bool(getattr(result, 'detected', False)) if result else False
                        _f = getattr(result, 'fill_pct', None) if result else None
                        _c = getattr(result, 'confidence', None) if result else None
                        _bb = getattr(result, 'bbox', None) if result else None
                        _rej = getattr(result, 'rejection_reason', '') if result else 'no_result'
                        _gc = getattr(result, 'green_window_center_pct', None) if result else None
                        _gconf = getattr(result, 'green_window_confidence', None) if result else None
                        _fh, _fw = frame.shape[0], frame.shape[1]
                        logger.warning(
                            'DETDIAG detected=%s fill=%s conf=%s bbox=%s green_c=%s green_conf=%s '
                            'reject=%r frame=%dx%d color=%r style=%r uniqfps=%s',
                            _det, _f, _c, _bb, _gc, _gconf, _rej, _fw, _fh,
                            self.config.meter_color, self.config.meter_style,
                            self._unique_frame_fps,
                        )
                    except Exception:
                        pass
            # Gated on the ENV opt-in, not the live flag: _dump_frame owns the heartbeat, and a
            # dump that has stopped is exactly the state that has to keep reporting itself.
            if getattr(self, '_framedump_env_enabled', False):
                self._dump_frame(frame, result)
            # [ORION_SHOT_RECORDS 2026-09-16] Fallback onset + the opt-in nameplate cell.
            # Guarded on the recorder existing, so a session with ORION_SHOT_RECORDS=0 runs
            # exactly one attribute read more than before.
            if getattr(self, '_shot_records', None) is not None:
                try:
                    self._shot_record_frame_hook(frame, result, time.perf_counter())
                except Exception:
                    pass
            # [ORION_SHOT_RANGE 2026-09-17] The press+40..120 ms range window.  Two attribute
            # reads and (inside the window, at most three times per press) one list append of
            # a frame REFERENCE -- measured below 0.005 ms/frame.  Everything else is on the
            # reader's worker.
            _rng = getattr(self, '_shot_range', None)
            if _rng is not None:
                try:
                    _rng.note_frame(frame, time.perf_counter(),
                                    measurement_epoch_s=float(_frame_wall_ms or 0.0) / 1000.0)
                except Exception:
                    pass
            # Shadow-only animation anchor: compute + LOG a candidate motion/pose release
            # landmark; NEVER controls release. meterFill at the anchor is the correlation
            # signal (a consistent fill-at-anchor across shots => a usable phase anchor).
            # Reset between shots (meter gone a few frames). Fully guarded + flag-OFF.
            if self._anim_anchor is not None:
                try:
                    _adet = bool(result and getattr(result, 'detected', False))
                    if _adet:
                        self._anim_miss = 0
                    else:
                        self._anim_miss += 1
                        if self._anim_miss == 6:
                            self._anim_anchor.reset()
                    _ares = self._anim_anchor.update(frame)
                    if _ares.found:
                        _afill = float(getattr(result, 'fill_pct', -1.0) or -1.0) if result else -1.0
                        logger.info('AnimAnchor: tMs=%.1f kind=%s conf=%.2f motion=%.3f meterDet=%d meterFill=%.1f'
                                    % (time.perf_counter() * 1000.0, _ares.kind, _ares.confidence,
                                       _ares.motion, 1 if _adet else 0, _afill))
                except Exception:
                    pass
        except Exception:
            logger.debug('post-publication detection diagnostic skipped', exc_info=True)

    def _reset_prediction_history(self):
        """Drop source/ruler-dependent state on the detector thread only."""
        self._last_tip_reg = None
        self._last_fill_forecast = None
        self._last_fill_kalman = None
        self._last_posthoc = None
        self._prediction_sample_identity = None
        for name, method in (('_tip_reg', 'reset_tracking'),
                             ('_fill_forecaster', 'reset'),
                             ('_fill_kalman', 'reset')):
            reset = getattr(getattr(self, name, None), method, None)
            if callable(reset):
                try:
                    reset()
                except Exception:
                    # One optional model must not prevent the other histories
                    # resetting or leave its old trajectory active on a new ruler.
                    setattr(self, name, None)
                    if name == '_tip_reg':
                        set_provider = getattr(self._meter_detector, 'set_fit_provider', None)
                        if callable(set_provider):
                            set_provider(None)
                    logger.warning('Disabled %s after trajectory reset failed', name)

    def _sync_prediction_sample_identity(self, result, source_identity):
        """Reset only on a genuine sample's source/ruler boundary, never on coast.

        Called on the detector thread before any predictor consumes this result.
        Missing ruler metadata stays an explicit legacy identity rather than
        borrowing a previous known ruler. A later known ruler starts clean.
        """
        if not self._is_raw_accepted(result):
            return
        try:
            source = max(0, int(source_identity or 0))
        except (TypeError, ValueError, OverflowError):
            source = 0
        try:
            mode = str(getattr(result, 'fill_estimator_mode', '') or '').strip().lower()
            generation = int(getattr(result, 'fill_estimator_generation', 0) or 0)
            if mode not in ('coarse', 'subpixel') or generation <= 0:
                mode, generation = '', 0
            identity = (source, mode, generation)
        except (TypeError, ValueError, OverflowError):
            identity = (source, '', 0)
        previous = getattr(self, '_prediction_sample_identity', None)
        if previous == identity:
            return
        if previous is not None:
            self._reset_prediction_history()
        self._prediction_sample_identity = identity

    def _wait_for_detector_frame(self, last_seq):
        """Return one latest committed bundle, or None on watchdog/stop wake.

        Clear BEFORE checking the immutable bundle. A commit racing with clear
        is visible in that check; a later commit latches the event before wait.
        Recheck after wake because an event is a hint, never frame authority.
        There is no frame queue and no additional timestamp/identity assignment.
        """
        event = getattr(self, '_detector_frame_ready_evt', None)
        if event is None:
            # Compatibility for lightweight __new__ fixtures; real construction
            # creates the event before the capture/processing threads start.
            event = threading.Event()
            self._detector_frame_ready_evt = event
        event.clear()
        if not self._running:
            return None
        bundle = self._frame_bundle
        if bundle[0] is not None and bundle[6] != last_seq:
            return bundle
        # Keep the existing frozen-meter watchdog deadline, not a fresh full
        # interval on each idle call. No-meter idle wakes remain bounded so even
        # legacy test producers that lack the event cannot strand the loop.
        timeout = 0.25
        if self._last_frame_ts > 0.0 and self._last_meter_present:
            remaining = (self._stall_watchdog_ms / 1000.0
                         - (time.perf_counter() - self._last_frame_ts))
            timeout = max(0.0, min(timeout, remaining))
        event.wait(timeout=timeout)
        if not self._running:
            return None
        bundle = self._frame_bundle
        return bundle if bundle[0] is not None and bundle[6] != last_seq else None

    def _processing_loop(self):
        # [ORION_RUNTIME_HYGIENE] Optional detect-thread priority raise
        # (ORION_DETECT_THREAD_PRIORITY=above|highest). Default ABSENT = normal, byte-identical
        # behaviour. Rationale: this thread is the latency-critical consumer (its output feeds the
        # native decision budget), yet it runs at THREAD_PRIORITY_NORMAL while the preview thread
        # is explicitly demoted and the native fire thread runs TIME_CRITICAL. Under live
        # contention (cv2 pool, telemetry, 1 kHz input router in the same process) a normal-
        # priority detect thread is the first to lose its core. Effect is only observable under
        # live load â€” offline replay cannot validate it â€” so this stays opt-in until a counted
        # batch says otherwise.
        _prio = os.environ.get('ORION_DETECT_THREAD_PRIORITY', '').strip().lower()
        if _prio and os.name == 'nt':
            _lvl = {'above': 1, 'highest': 2}.get(_prio)
            if _lvl is None:
                logger.warning('runtime hygiene: unknown ORION_DETECT_THREAD_PRIORITY=%r ignored',
                               _prio)
            else:
                try:
                    import ctypes as _ct
                    _ok = _ct.windll.kernel32.SetThreadPriority(
                        _ct.windll.kernel32.GetCurrentThread(), _lvl)
                    logger.warning('runtime hygiene: detect thread priority %s (%d) %s',
                                   _prio, _lvl, 'applied' if _ok else 'FAILED')
                except Exception:
                    logger.warning('runtime hygiene: detect thread priority raise failed',
                                   exc_info=True)
        # [ORION_BANNER_VERDICT_LIVE 2026-09-14] Arm the live shot-verdict reader for this
        # session. create() returns None when ORION_BANNER_VERDICT_LIVE=0 or the offline
        # grader (tools/timing/panel_grade.py + panel_templates.npz) is not importable, and
        # never raises, so the detect loop below is byte-identical when it is off.
        try:
            from banner_verdict_live import BannerVerdictLive as _BannerVerdictLive
            # [ORION_SHOT_RECORDS] Same tee as the release oracle: the record gets a copy of
            # every ATTRIBUTED verdict, the native gets the identical bytes it always got.
            self._banner_verdict = _BannerVerdictLive.create(
                emit_line=(self._shot_record_banner_sink
                           if getattr(self, '_shot_records', None) is not None
                           else emit_stdout_jsonl))
        except Exception as _bv_exc:
            self._banner_verdict = None
            logger.warning('banner verdict reader unavailable: %s', _bv_exc)
        last_seq = -1
        while self._running:
            try:
                # Wake on capture commit, not a timer-quantized polling sleep.
                # The bundled sequence is the sole dedup key and the same tuple
                # supplies every pixel, source identity and measurement below.
                _snap_bundle = self._wait_for_detector_frame(last_seq)
                if _snap_bundle is None:
                    # RC-2b STALL WATCHDOG. No NEW unique frame this poll. Duplicates keep frame_age~0
                    # (capture is "alive"), so nothing else would tell the native engine the meter is
                    # gone â€” a pure stall never produces a fresh unique frame, so the meter_present:false
                    # that fires only on a processed frame is defeated by exactly this condition. When
                    # the pixel age (since the last UNIQUE frame) crosses the watchdog threshold,
                    # synthesize a no-meter state so the native fresh gate stops coasting on a frozen
                    # held fill (the 293s HOLD / RISE 0.0 / idle_overlay echo).
                    if self._last_frame_ts > 0.0 and self._last_meter_present:
                        _stale_ms = (time.perf_counter() - self._last_frame_ts) * 1000.0
                        if _stale_ms >= self._stall_watchdog_ms:
                            self._last_meter_present = False
                            self._last_raw_fed = False
                            self._last_meter_track = _MeterTrackPayload()
                            if not self._stall_active:
                                self._stall_active = True
                                logger.warning('stall watchdog: no unique frame for %.0fms -> '
                                               'meter_present=false (was holding)', _stale_ms)
                    continue
                self._stall_active = False
                # P1 fix: read the atomically-published frame tuple (see capture commit) so
                # detect()'s latency below can't skew the staleness clock. Every timing consumer
                # in this iteration uses these snapshot locals, never the live self._last_* fields.
                frame, _snap_frame_ts, _snap_y_plane, _snap_frame_pts, _snap_frame_epoch_ms, \
                    _snap_measurement_epoch_ms, _snap_seq, _snap_source_frame_number, \
                    _snap_backend_frozen, \
                    _snap_integrity_generation = _snap_bundle[:10]
                # Source identity must describe these exact pixels, not the
                # capture thread's potentially newer mutable source globals.
                # Ten-field legacy test bundles carry no source attestation.
                _snap_source_identity = int(_snap_bundle[10] or 0) \
                    if len(_snap_bundle) > 10 else 0
                _snap_latency_estimator = self._latency_estimator_for_frame(
                    _snap_integrity_generation)
                # D5 fix: mark done the frame THIS iteration actually read, taken from the same
                # atomic snapshot as its pixels -- never a second, independently-timed load of
                # self._frame_seq (which the capture thread may have advanced in between, skipping
                # one frame and re-detecting the next as a zero-dt duplicate).
                if last_seq >= 0 and _snap_seq > last_seq + 1:
                    self._cv_frames_skipped += (_snap_seq - last_seq - 1)
                last_seq = _snap_seq
                # Defensive: the H1 publish-order (bundle stored BEFORE the _frame_seq bump) already
                # guarantees a non-None bundle whenever the seq gate above passes, but a torn read of
                # the init tuple (None, ...) would make detect(None) raise and drop a frame. Re-poll
                # on the (impossible-by-construction, but free-to-check) None so a single bad snapshot
                # can never fault the loop.
                if frame is None:
                    time.sleep(0.0005)
                    continue
                # One canonical measurement epoch follows these exact pixels through
                # detection, telemetry, the native sampler, and tip registration. It
                # was computed and atomically committed by capture; consulting the live
                # PTS mapping here could pair frame N with frame N+1's calibration.
                # Raw _snap_frame_epoch_ms remains a separate identity stamp.
                _frame_wall_ms = float(_snap_measurement_epoch_ms or 0.0)
                # [ORION_BANNER_VERDICT_LIVE 2026-09-14] Read the GAME'S OWN shot-feedback
                # banner off these exact pixels. Placed before the meter/pose split so it
                # grades both modes. Cost on THIS thread is a 53 KB strip copy every 6th
                # frame (~0.02 ms); the HSV/cell/NCC work runs on the reader's own worker
                # thread. None unless the reader armed; never raises.
                if self._banner_verdict is not None:
                    self._banner_verdict.submit(
                        frame, _snap_seq, frame_ts=_snap_frame_ts,
                        epoch_ms=(_frame_wall_ms or _snap_frame_epoch_ms))
                # No-meter mode: run pose timing detector instead of meter detector
                if self._pose_timing is not None:
                    _pose_ok = False
                    try:
                        self._pose_timing.update(frame, _snap_seq)
                        self._emit_pose_overlay()
                        _pose_ok = True
                    except Exception as e:
                        logger.error(f'Pose timing error: {e}')
                    self._finish_processed_frame(
                        _snap_seq, _snap_source_frame_number, _snap_backend_frozen,
                        _pose_ok, 'pose_detector_exception',
                        frame_ts=_snap_frame_ts, epoch_ms=_snap_frame_epoch_ms,
                        measurement_epoch_ms=_frame_wall_ms,
                        pts=_snap_frame_pts, frame_wh=(frame.shape[1], frame.shape[0]),
                        integrity_generation=_snap_integrity_generation)
                    time.sleep(0.001)
                    continue
                if self._meter_detector and self._green_analyzer:
                    try:
                        _processed_source = int(getattr(
                            self, '_detector_processed_source_identity', 0) or 0)
                        _prediction_identity = getattr(self, '_prediction_sample_identity', None)
                        if (_prediction_identity is not None
                                and _snap_source_identity != _prediction_identity[0]):
                            # The reader can query the previous fit DURING detect().
                            # Clear it before the new source's pixels reach that hook.
                            self._reset_prediction_history()
                        if (_snap_source_identity > 0 and _processed_source > 0
                                and _snap_source_identity != _processed_source):
                            # Normalization keeps both sources at 1280x720, so
                            # image-shape checks cannot invalidate an old ruler
                            # or outstanding locator result. Reset on THIS thread
                            # before installing per-frame shot state/native Y.
                            _reset = getattr(self._meter_detector, 'reset_tracking', None)
                            if callable(_reset):
                                _reset()
                        # Push the shot-gate armed state to a shot-gated reader BEFORE detect(): armed
                        # while a shot-start signal is within its window (bounded, auto-expiring).
                        # Guarded -> a no-op for the serving chain. Never suppresses detection; it only
                        # relaxes early-rise acquisition + extends the coast while the bot is shooting.
                        self._push_reader_shot_gate(_snap_seq)
                        _cv_t0 = time.perf_counter()
                        # A0 unified timebase: every timing consumer below gets the FRAME's epoch
                        # stamp (capture instant), not the loop's processing time â€” this removes
                        # dequeue/processing jitter from velocities and aligns the fill timeline
                        # with the epoch release markers.
                        # detect(ts=...) moves the reader's velocity clock onto capture time (the ts
                        # param existed but was never wired â€” reader fell back to perf_counter at
                        # call time). Both SimpleMeterReader and MeterDetector accept ts (seconds).
                        # Native Y pass-through (compressed reader only, guarded no-op
                        # elsewhere): one-shot per frame -- the reader consumes it inside
                        # this read() and clears it, so a stale plane can never be reused.
                        _sy = getattr(self._meter_detector, 'set_native_y', None)
                        if callable(_sy):
                            try:
                                _sy(_snap_y_plane)
                            except Exception:
                                pass
                        # [ORION_EPOCH_TS_GUARD 2026-08-11] _frame_measurement_epoch_ms() returns a
                        # FAIL-CLOSED 0.0 when the source epoch is missing/non-finite/<=0. Passing
                        # that straight through is a trap: `0.0 is not None`, so the reader does NOT
                        # fall back to perf_counter() and the literal 0.0 enters the velocity clock.
                        # A _vel_hist window mixing 0.0 with real epoch-seconds (~1.7e9) produces a
                        # garbage least-squares slope; an all-0.0 window pins velocity to 0. That
                        # drives eta_ms/rise_state and gates the CV self-arm. Hand the reader None so
                        # its own perf_counter() fallback runs. Steady-state capture-card frames
                        # carry a real epoch, so the shipping path is byte-identical -- this only
                        # bites at session start and on degraded frames.
                        _detect_ts = ((_frame_wall_ms / 1000.0)
                                      if _frame_wall_ms > 0.0 else None)
                        result = self._meter_detector.detect(frame, ts=_detect_ts)
                        self._sync_prediction_sample_identity(result, _snap_source_identity)
                        # A failed initial detect does not establish a processed
                        # source. Cold start and same-source drops retain state.
                        if _snap_source_identity > 0:
                            self._detector_processed_source_identity = _snap_source_identity
                        # Publish the reader's stage for the sidecar payload (coast vs fresh read).
                        try:
                            self._last_meter_stage = str((getattr(self._meter_detector, 'last_debug', None)
                                                          or {}).get('stage', '') or '')
                        except Exception:
                            self._last_meter_stage = ''
                        # CV SELF-ARM (virtual_controller=False safety net): the native `pose_arm` stdin
                        # may not fire for this config, so a detected RISING meter also arms/refreshes the
                        # shot-gate here. This keeps the reader's coast extended + early-rise gate relaxed
                        # through the live shot even with no controller-side signal -- fulfilling the
                        # "a fresh detected meter refreshes the arm" contract. Rising-only (velocity gate)
                        # so a static dÃ©cor blob can never self-arm. Native pose_arm still provides the
                        # earlier (pre-detection) arm when it is emitted.
                        try:
                            if result is not None and getattr(result, 'detected', False):
                                _rs = str(getattr(result, 'rise_state', '') or '')
                                _vel = float(getattr(result, 'fill_velocity_pct_s', 0.0) or 0.0)
                                if _rs == 'rising' or _vel > 40.0:
                                    self._refresh_cv_shot_gate(_snap_seq)
                        except Exception:
                            pass
                        # Fill-trajectory forecaster (flag-gated): feed the running fill curve, stash the
                        # predicted ms-to-tip on the result + emit a throttled telemetry line so we can grade
                        # predicted-tip vs the actual tip live. Never affects timing (telemetry only) until
                        # validated. Wrapped so a model hiccup can never stall the CV loop.
                        if self._fill_forecaster is not None and result is not None:
                            try:
                                _fed = self._is_raw_accepted(result)   # raw tier: no gated/synthetic fill
                                self._fill_forecaster.update(
                                    _frame_wall_ms,   # capture-epoch ms; only diffs are used
                                    float(getattr(result, 'fill_pct', 0.0) or 0.0),
                                    float(getattr(result, 'green_window_center_pct', -1.0) or -1.0),
                                    bool(_fed),
                                )
                                _ptip = self._fill_forecaster.predict() if _fed else None
                                _fc_fill = float(getattr(result, 'fill_pct', 0.0) or 0.0)
                                _fc_vel = float(getattr(result, 'fill_velocity_pct_s', 0.0) or 0.0)
                                _fc_val = float(_ptip) if _ptip is not None else -1.0
                                # Forecast-confidence proxy: the model emits no uncertainty, so gate the
                                # detection confidence on a VALID prediction + a clearly RISING fill (the
                                # forecaster already returns None near the top / when receding). Native must
                                # threshold this before it trusts predTipMs; 0.0 => "no usable forecast this
                                # frame, keep the appear->tip clock."
                                _fc_conf = (float(getattr(result, 'confidence', 0.0) or 0.0)
                                            if (_ptip is not None and _fc_vel > 0.0) else 0.0)
                                try:
                                    setattr(result, 'predicted_tip_ms', _fc_val)
                                except Exception:
                                    pass
                                # Publish the latest forecast on a stable orchestrator attribute so the
                                # sidecar telemetry loop can fold predTipMs into the SAME per-frame JSON
                                # payload the native engine already reads (paired with the capture seq),
                                # rather than it living only in a throttled log line. Updated EVERY frame
                                # (pred_tip_ms=-1 when there is nothing to forecast) so the native engine
                                # sees a fresh, seq-paired value at its 60 Hz read; t_ms lets it reject a
                                # stale forecast. Inert (attribute stays None) unless ORION_FILL_FORECAST=1.
                                self._last_fill_forecast = {
                                    'seq': int(_snap_seq),
                                    'pred_tip_ms': _fc_val,
                                    'confidence': _fc_conf,
                                    'fill_pct': _fc_fill,
                                    't_ms': time.perf_counter() * 1000.0,
                                }
                                if _ptip is not None:
                                    self._fill_forecast_log_n += 1
                                    if self._fill_forecast_log_n % 6 == 0:   # ~10Hz -> keep the log light
                                        logger.info('fillForecast seq=%d fill=%.1f predTipMs=%.0f conf=%.2f',
                                                    int(_snap_seq), _fc_fill, float(_ptip), _fc_conf)
                            except Exception as _ffx:
                                logger.debug(f'fill forecast skip: {_ffx}')
                        # Kalman fill estimator (flag-gated, TELEMETRY-ONLY): smoothed velocity + analytical
                        # sub-frame ms-to-target, graded live vs the forecaster. Never affects timing here.
                        if self._fill_kalman is not None and result is not None \
                                and str(getattr(result, 'rejection_reason', '') or '') != 'stale_frame':
                            # stale_frame = encoder-skip duplicate: MISSING DATA, not a
                            # measurement -- feeding the held fill would poison the velocity.
                            try:
                                _kfed = self._is_raw_accepted(result)   # raw tier: no gated/synthetic fill
                                _kfill = float(getattr(result, 'fill_pct', 0.0) or 0.0)
                                # Held/dead-reckoned fills are not measurements. Feeding
                                # them here silently pulled velocity toward zero through
                                # occlusion even though the published prediction was gated.
                                if _kfed and _frame_wall_ms > 0.0:
                                    self._fill_kalman.update(_kfill, _frame_wall_ms / 1000.0)
                                elif not bool(getattr(result, 'detected', False)):
                                    self._fill_kalman.reset()
                                _ktip = None
                                if _kfed and _frame_wall_ms > 0.0 and self._fill_kalman.ready:
                                    _ktgt = float(getattr(result, 'green_window_center_pct', 0.0) or 0.0)
                                    if _ktgt <= 0.0:
                                        _ktgt = 95.0    # fall back to the tip when no green window is read
                                    _ktip = self._fill_kalman.ms_to_target(_ktgt)
                                self._last_fill_kalman = {
                                    'seq': int(_snap_seq),
                                    'kalman_tip_ms': float(_ktip) if _ktip is not None else -1.0,
                                    'kalman_vel_pct_s': float(self._fill_kalman.velocity()),
                                    'kalman_fill_pct': float(self._fill_kalman.fill()),
                                    't_ms': time.perf_counter() * 1000.0,
                                }
                                if _ktip is not None:
                                    self._fill_kalman_log_n += 1
                                    if self._fill_kalman_log_n % 6 == 0:
                                        # WARNING, not INFO: the native stderr relay drops INFO, so
                                        # this line -- the entire substrate for grading the Kalman
                                        # tip estimate against the sampler -- never reached
                                        # orion_native.log. The 2026-08-03 session emitted it all
                                        # session and only ~5 exit-tail samples survived, leaving
                                        # the Kalman-vs-sampler question unanswerable offline.
                                        # Already decimated 1-in-6 and only while a meter is rising.
                                        logger.warning('fillKalman seq=%d fill=%.1f kalmanTipMs=%.0f vel=%.0f',
                                                       int(_snap_seq), _kfill, float(_ktip),
                                                       self._fill_kalman.velocity())
                            except Exception as _kfx:
                                logger.debug(f'fill kalman skip: {_kfx}')
                        # CV-throughput profiling: EMA the detect() wall time and count
                        # processed frames so the capture-health log can attribute the
                        # unique-fps cap to decode vs CV (see __init__ for the rationale).
                        _cv_dt = (time.perf_counter() - _cv_t0) * 1000.0
                        self._cv_detect_ms_ema = (_cv_dt if self._cv_detect_ms_ema <= 0.0
                                                  else self._cv_detect_ms_ema * 0.9 + _cv_dt * 0.1)
                        self._cv_processed += 1
                        _cv_now = time.perf_counter()
                        _cv_el = _cv_now - self._cv_fps_t0
                        if _cv_el >= 1.0:
                            self._cv_fps = self._cv_processed / _cv_el
                            self._cv_processed = 0
                            self._cv_fps_t0 = _cv_now
                        # (HUD text-banner reader retired -- the native engine calibrates from
                        # the bot's own post-release meter off this detection telemetry.)
                        # Far-horizon tip registration + frozen-meter latency oracle. present/fed mirror
                        # the meter-present gate below so the detframes row + payload carry THIS frame's
                        # phase / reg prediction / pts. Telemetry only; never affects timing here.
                        _csv_latency_label = None
                        try:
                            _mp = bool(result and getattr(result, 'detected', False) and getattr(result, 'bbox', None)
                                       and result.bbox[2] > 0 and result.bbox[3] > 0)
                            # RAW tier: the oracle + tip registration must only ever ingest clean
                            # accepted samples ([fix]: refit only on accepted samples; a gated or
                            # dead-reckoned fill would poison labels and the online fit alike).
                            _mfed = self._is_raw_accepted(result) if result else False
                            _mfill = float(getattr(result, 'fill_pct', 0.0) or 0.0) if (result and _mp) else 0.0
                            # A0: BOTH the oracle and tip-reg now run on the frame's capture-epoch
                            # stamp. The oracle previously used loop-time epoch (processing jitter in
                            # every label); tip-reg used perf_counter (a different clock domain from
                            # the epoch release markers, which made cross-consumer math impossible).
                            if _snap_latency_estimator is not None:
                                _snap_latency_estimator.update(
                                    _frame_wall_ms, _mfill, _mp, _mfed)
                                if self._detcsv is not None:
                                    # Freeze before _finish_processed_frame wakes native.
                                    # A release prompted by this frame may mutate the
                                    # estimator while post-publication CSV work runs.
                                    _csv_latency_label = _DetectionLatencyLabel(
                                        int(_snap_latency_estimator.release_seq),
                                        str(_snap_latency_estimator.phase))
                            if self._tip_reg is not None:
                                self._tip_reg.update(_frame_wall_ms, _mfill, _mp, _mfed)
                                _details_fn = getattr(self._tip_reg, 'predict_details', None)
                                _details = (_details_fn() if _mfed and callable(_details_fn)
                                            else None)
                                _tp = (self._tip_reg.predict()
                                       if _mfed and not callable(_details_fn) else None)
                                _reg_ms = float(_details.get('ms_to_tip', -1.0)) \
                                    if _details is not None else float(
                                        _tp[0] if _tp is not None else -1.0)
                                _reg_conf = float(_details.get('confidence', 0.0)) \
                                    if _details is not None else float(
                                        _tp[1] if _tp is not None else 0.0)
                                # Fit-quality companions (Phase-1 fusion needs an honest sigma):
                                # unweighted residual RMS in fill-% + uncensored sample count.
                                _fit = self._tip_reg.last_fit() \
                                    if (_details is not None or _tp is not None) else None
                                self._last_tip_reg = {
                                    'seq': int(_snap_seq),
                                    'reg_tip_ms': float(_reg_ms),
                                    'reg_conf': float(_reg_conf),
                                    'reg_rmse_pp': float(_fit['rms_unw_pp']) if _fit else -1.0,
                                    'reg_n': int(_fit['n_used']) if _fit else 0,
                                    # Structured NumPy serving contract. The absolute tip and
                                    # sample clock are both capture-epoch milliseconds, never
                                    # sidecar arrival/perf-counter time.
                                    'reg_tip_capture_ms': float(
                                        _details.get('tip_capture_ms', 0.0))
                                        if _details is not None else 0.0,
                                    'reg_sample_capture_ms': float(
                                        _details.get('sample_clock_ms', 0.0))
                                        if _details is not None else 0.0,
                                    'reg_sigma_ms': float(_details.get('sigma_ms', -1.0))
                                        if _details is not None else -1.0,
                                    'reg_model_id': str(_details.get('model_id', ''))[:64]
                                        if _details is not None else '',
                                    'reg_model_version': str(
                                        _details.get('model_version', ''))[:32]
                                        if _details is not None else '',
                                    'reg_fit_method': str(
                                        _details.get('fit_method', ''))[:48]
                                        if _details is not None else '',
                                    'fill_pct': _mfill,
                                    't_ms': time.perf_counter() * 1000.0,
                                }
                                # POST-HOC full-shot refit label (once per archived shot): the
                                # clock prior's EMA teacher â€” t0 + T*u_tip on capture-epoch ms,
                                # robust to the release-freeze censoring the observed peak.
                                _an = int(getattr(self._tip_reg, 'shot_archive_n', 0) or 0)
                                if _an != self._posthoc_seen_n:
                                    self._posthoc_seen_n = _an
                                    _ph = self._tip_reg.posthoc_tip_ms()
                                    if _ph is not None:
                                        self._last_posthoc = {
                                            'n': _an,
                                            'tip_epoch_ms': float(_ph[0]),
                                            'conf': float(_ph[1]),
                                        }
                                        logger.info('posthocTip n=%d tip_epoch_ms=%.0f conf=%.2f',
                                                    _an, float(_ph[0]), float(_ph[1]))
                                if _details is not None or _tp is not None:
                                    self._tip_reg_log_n += 1
                                    if self._tip_reg_log_n % 6 == 0:
                                        logger.info('tipReg seq=%d fill=%.1f regTipMs=%.0f conf=%.2f',
                                                    int(_snap_seq), _mfill, float(_reg_ms), float(_reg_conf))
                        except Exception as _trx:
                            logger.debug(f'tip reg / latency skip: {_trx}')
                        if result and result.detected and result.bbox and result.bbox[2] > 0 and result.bbox[3] > 0:
                            self._last_meter_bbox = result.bbox
                            # [ORION_PROOF_DETECTOR_BOX 2026-09-19] The same frame's DETECTOR
                            # rectangle, stamped by the reader at its production boundary on
                            # every published frame (it equals result.bbox when the display hug
                            # is off). Native's ownership proof judges shape continuity on it;
                            # anything unreadable falls back to the drawn box, which is today's
                            # behaviour exactly.
                            _det_box = None
                            try:
                                _rdbg = getattr(self._meter_detector, 'last_debug', None)
                                _cand = _rdbg.get('det_box') if isinstance(_rdbg, dict) else None
                                if (_cand is not None and len(_cand) >= 4
                                        and int(_cand[2]) > 0 and int(_cand[3]) > 0):
                                    _det_box = tuple(int(_cand[i]) for i in range(4))
                            except Exception:
                                _det_box = None
                            self._last_meter_det_bbox = (
                                _det_box if _det_box is not None
                                else tuple(int(v) for v in result.bbox[:4]))
                            # Publish the detector's LIVE velocity/accel/eta so it reaches the native
                            # engine (msg["tracking"]). The detector computes these every frame (healthy
                            # 250-650 %/s on a rise); they were 0 downstream only because this object was
                            # never assigned. Meter is present this frame (covers the whole rise, incl.
                            # green_not_found frames, which ARE detected).
                            self._last_meter_present = True
                            _tv = getattr(result, 'fill_velocity_pct_s', None)
                            _ta = getattr(result, 'fill_acceleration_pct_s2', None)
                            # ETA to the release TARGET: prefer the green-window center (the actual
                            # release point); fall back to eta-to-top (100%) when no green window is read.
                            _te = getattr(result, 'eta_to_green_center_ms', None)
                            if _te is None or float(_te) < 0.0:
                                _te = getattr(result, 'eta_ms', None)
                            _tf = float(getattr(result, 'fill_pct', 0.0) or 0.0)
                            _tcf = float(getattr(result, 'raw_fill_pct', -1.0) or 0.0)
                            _tem = str(getattr(result, 'fill_estimator_mode', '') or '').strip().lower()
                            _teg = int(getattr(result, 'fill_estimator_generation', 0) or 0)
                            if _tem not in ('coarse', 'subpixel') or _teg <= 0:
                                _tem = ''
                                _teg = 0
                            _tc = float(getattr(result, 'confidence', 0.0) or 0.0)
                            self._last_meter_track = _MeterTrackPayload(
                                velocity_pct_s=float(_tv) if _tv is not None else 0.0,
                                acceleration_pct_s2=float(_ta) if _ta is not None else 0.0,
                                eta_to_target_ms=float(_te) if _te is not None else -1.0,
                                fill_pct=_tf,
                                coarse_fill_pct=_tcf,
                                fill_estimator_mode=_tem,
                                fill_estimator_generation=str(_teg),
                                confidence=_tc,
                            )
                            # Stamp the frame WxH the bbox was computed in, so native maps it with the
                            # CORRECT scale (not a live, possibly-different captureWidth/Height) -> the
                            # overlay box can't jump when the capture resolution flips.
                            self._last_meter_bbox_wh = (int(frame.shape[1]), int(frame.shape[0]))
                            # Use the detector's TRACK-relative green window (scanned over the
                            # FULL meter track, above the rising fill). The old
                            # green_analyzer.analyze(result.bbox) scanned INSIDE the fill bbox,
                            # so it missed the green at the top of the track and reported bogus
                            # green at ~1-4% â€” never a usable release target.
                            if getattr(result, 'green_window_confidence', 0.0) > 0.0 and float(getattr(result, 'green_window_center_pct', -1.0)) >= 0.0:
                                start_pct = float(result.green_window_start_pct)
                                end_pct = float(result.green_window_end_pct)
                                center_pct = float(result.green_window_center_pct)
                                width_pct = float(result.green_window_width_pct)
                                green_px = int(getattr(result, 'green_cluster_px', 0))
                                self._update_green_overlay({'start': start_pct, 'end': end_pct, 'center': center_pct, 'width': width_pct, 'green_px': green_px})
                            else:
                                start_pct = end_pct = center_pct = -1.0
                                width_pct = 0.0
                                # Meter present but no green window this frame.
                                self._update_green_overlay(None)
                            # Feed CV data on EVERY *real* meter detection, not only when the green
                            # zone is visible. green_*_pct are already -1 when no green is found, so
                            # the engine still gets continuous fill/velocity/eta to drive
                            # ARMED->HOLDING and predictive release for the whole shot.
                            #
                            # Gate: feed any REAL detection, excluding ONLY the idle "meter_memory"
                            # echo. A detected meter with no green window yet carries
                            # rejection_reason="green_not_found" â€” that is the ENTIRE rising phase of
                            # every shot (and all of a contested shot). It MUST reach the engine
                            # (green stays -1 until it appears) so the engine tracks fill/velocity and
                            # times the release. Requiring rejection_reason=="" here was the bug that
                            # starved the engine -> presence=no_sample_ever -> max_hold_safety. The
                            # stability validator's N-consecutive gate + this meter_memory exclusion
                            # still keep idle false positives out.
                            # raw_fed = THIS frame had a clean RAW detection worth
                            # feeding (accepted / green_not_found), NOT a meter_memory
                            # echo / roi_not_found / unstable â€” and NOT the sampler-tier
                            # fill_gated / dead_reckoned frames (plan B2 [fix]: those are
                            # FED below but must read as stale_or_memory natively, never
                            # as a fresh raw sample). Published so the native engine can
                            # distinguish a fresh accept from a held/extrapolated one
                            # (Go-To freshness trust gate).
                            self._last_raw_fed = self._is_raw_accepted(result)
                            self._last_gameplay_structure_verified = bool(
                                self._last_raw_fed
                                and getattr(result, 'gameplay_structure_verified', False))
                            self._last_gameplay_structure_epoch = (
                                _parse_pose_arm_token(getattr(
                                    result, 'gameplay_structure_epoch', 0))
                                if self._last_gameplay_structure_verified else 0)
                            if self._should_feed_engine(result):
                                if self._rtt_engine:
                                    # Shot in flight: tighten the RTT ping cadence so the
                                    # offset latched at the next hold-start is fresh.
                                    self._rtt_engine.notify_meter_active()
                                if self._remap_engine:
                                    if self._rtt_engine:
                                        rtt_snap = self._rtt_engine.get_snapshot()
                                        # Feed the NETWORK one-way time (half-RTT) into the engine
                                        # lead as soon as RTT has ANY samples (ready OR converging) â€”
                                        # not only when fully ready, which often never happened and
                                        # left offset=0. We feed ONLY the network half-RTT (not the
                                        # full effective_offset): the encode/decode/display pipeline
                                        # is covered separately by the engine's remotePlayPipelineMs,
                                        # so folding the RTT decode_comp here too would double-count.
                                        _half = float(getattr(rtt_snap, 'rtt_half_ms', 0.0) or 0.0)
                                        _jit = float(getattr(rtt_snap, 'jitter_margin_ms', 0.0) or 0.0)
                                        _trusted_rtt = bool(getattr(rtt_snap, 'ready', False)) \
                                            and bool(getattr(rtt_snap, 'target_verified', False))
                                        if _trusted_rtt and _half > 0.0:
                                            self._remap_engine.update_network_offset(_half + _jit)
                                    # NOTE: do NOT use the `value or default` idiom here.
                                    # A legitimate 0 (zero confidence, zero stable frames,
                                    # eta==0 == "fire now") is falsy and would be silently
                                    # replaced by the default, spoofing stability/confidence
                                    # and causing premature releases. Only fall back when the
                                    # attribute is genuinely missing/None.
                                    _fill = getattr(result, 'fill_pct', None)
                                    _conf = getattr(result, 'confidence', None)
                                    _vel = getattr(result, 'fill_velocity_pct_s', None)
                                    _accel = getattr(result, 'fill_acceleration_pct_s2', None)
                                    _eta = getattr(result, 'eta_to_green_center_ms', None)
                                    _cvf = getattr(result, 'consecutive_frames', None)
                                    # Age of this CV reading: capture -> here.
                                    # Folded into release latency compensation so
                                    # the bot accounts for how stale the meter
                                    # sample is when it fires.
                                    _frame_age_ms = max(0.0, (time.perf_counter() - _snap_frame_ts) * 1000.0)
                                    if self._rtt_engine:
                                        # Live decode comp: the RTT engine EMAs this
                                        # measured staleness in place of its fixed
                                        # decode_latency_comp_ms constant.
                                        self._rtt_engine.observe_frame_age_ms(_frame_age_ms)
                                    self._remap_engine.update_cv_data(
                                        fill_pct=float(_fill) if _fill is not None else 0.0,
                                        confidence=float(_conf) if _conf is not None else 0.0,
                                        velocity_pct_s=float(_vel) if _vel is not None else 0.0,
                                        green_start_pct=start_pct,
                                        green_end_pct=end_pct,
                                        green_center_pct=center_pct,
                                        eta_to_green_ms=float(_eta) if _eta is not None else -1.0,
                                        consecutive_valid_frames=int(_cvf) if _cvf is not None else 0,
                                        meter_detected=True,
                                        acceleration_pct_s2=float(_accel) if _accel is not None else 0.0,
                                        frame_age_ms=_frame_age_ms,
                                    )
                                if self.config.goto_shot and self._goto_shot_active:
                                    if self.config.green_window_start_pct <= center_pct <= self.config.green_window_end_pct:
                                        if not self._goto_shot_triggered and self._release_time is None:
                                            if self.config.timing_delay_ms > 0:
                                                self._release_time = time.time() + self.config.timing_delay_ms / 1000.0
                                                logger.info(f'Scheduled release in {self.config.timing_delay_ms} ms')
                                            else:
                                                self._trigger_shot_release()
                        else:
                            # No meter detected this frame -> drive the hide
                            # hysteresis so the track-fill overlay clears cleanly
                            # instead of flickering on transient detections.
                            self._last_raw_fed = False
                            self._last_gameplay_structure_verified = False
                            self._last_gameplay_structure_epoch = 0
                            # Meter LOSS: publish an explicit no-meter frame so the native engine
                            # learns the meter vanished (relaxed emit gate accepts meter_present=false
                            # with fill/conf=0 and resets its HOLD clock) instead of coasting on the
                            # last held fill. Zero the tracking so a stale velocity can't linger.
                            self._last_meter_present = False
                            self._last_meter_track = _MeterTrackPayload()
                            self._update_green_overlay(None)
                            # A gameplay-gate rejection is authoritative scene state, not a
                            # one-frame detector dropout.  Clear every cached overlay field now;
                            # the ordinary 12-frame green hysteresis must not keep an ESRB/menu
                            # false box or fill visible after the trusted shot window is closed.
                            _reject = str(getattr(result, 'rejection_reason', '') or '') \
                                if result is not None else ''
                            if _reject in ('gameplay_ineligible', 'shot_candidate_unverified'):
                                self._clear_meter_overlay_immediate()
                        # Colour-calibration lifecycle status (B3): version-checked, so this is a
                        # no-op unless a calibrator state/commit actually changed this frame.
                        self._emit_calibration_status()
                        # Event-driven meter feed: this frame's detection just published fresh
                        # meter state (bbox / green / shot fill / raw_fed). Wake the sidecar
                        # telemetry loop to emit it immediately instead of waiting up to ~16.7ms
                        # for its next fixed 60Hz tick (and lift the 60fps emit ceiling when
                        # detection runs faster). Payload/schema unchanged -- only WHEN we emit.
                        self._finish_processed_frame(
                            _snap_seq, _snap_source_frame_number, _snap_backend_frozen, True,
                            frame_ts=_snap_frame_ts, epoch_ms=_snap_frame_epoch_ms,
                            measurement_epoch_ms=_frame_wall_ms,
                            pts=_snap_frame_pts, frame_wh=(frame.shape[1], frame.shape[0]),
                            integrity_generation=_snap_integrity_generation)
                        self._record_detection_diagnostics(
                            frame, result, _frame_wall_ms=_frame_wall_ms,
                            _snap_source_frame_number=_snap_source_frame_number,
                            _snap_frame_pts=_snap_frame_pts,
                            _snap_latency_estimator=_csv_latency_label,
                            _snap_integrity_generation=_snap_integrity_generation,
                            _snap_seq=_snap_seq, _snap_frame_epoch_ms=_snap_frame_epoch_ms,
                            _snap_source_identity=_snap_source_identity,
                            _snap_backend_frozen=_snap_backend_frozen)
                    except Exception as e:
                        logger.error(f'CV processing error: {e}')
                        self._finish_processed_frame(
                            _snap_seq, _snap_source_frame_number, _snap_backend_frozen,
                            False, 'detector_exception',
                            frame_ts=_snap_frame_ts, epoch_ms=_snap_frame_epoch_ms,
                            measurement_epoch_ms=_frame_wall_ms,
                            pts=_snap_frame_pts, frame_wh=(frame.shape[1], frame.shape[0]),
                            integrity_generation=_snap_integrity_generation)
                else:
                    self._finish_processed_frame(
                        _snap_seq, _snap_source_frame_number, _snap_backend_frozen,
                        False, 'detector_unavailable',
                        frame_ts=_snap_frame_ts, epoch_ms=_snap_frame_epoch_ms,
                        measurement_epoch_ms=_frame_wall_ms,
                        pts=_snap_frame_pts, frame_wh=(frame.shape[1], frame.shape[0]),
                        integrity_generation=_snap_integrity_generation)
                # No post-process sleep: already-committed latest frames are
                # immediately available; true idle blocks on the capture event.
            except Exception as e:
                logger.error(f'Error in processing loop: {e}')
                time.sleep(0.1)
        # Session over: retire the banner reader's worker thread (daemon, so this is only
        # about the closing stats line + a clean handoff to the next session).
        _bv = self._banner_verdict
        self._banner_verdict = None
        if _bv is not None:
            try:
                _bv.stop()
            except Exception:
                pass

    def trigger_goto_shot(self):
        if not self._running or not self._virtual_controller:
            logger.warning('Cannot trigger go-to shot: not running or no controller')
            return
        logger.info('Go-to shot triggered')
        self._goto_shot_active = True
        self._goto_shot_triggered = False
        self._goto_shot_start_time = time.time()

    def _trigger_shot_release(self):
        logger.info(f'Shot release signaled at green window center: {self._last_green_window}')
        self._goto_shot_triggered = True
        self._goto_shot_active = False
        # Frozen-meter oracle: stamp the release COMMAND on the fill wall clock so the estimator can
        # close the oracle when the meter freezes at F_stop (self-measured, orchestrator-issued path).
        self.mark_release()

    def mark_release(self, wall_ms: float = None, seq: int = None,
                      calibration: bool = False,
                      validation_target_pct: float = None,
                      validation_tolerance_pct: float = None,
                      physical_epoch=0, shot_attempt=0) -> int:
        """Record a release COMMAND for the frozen-meter latency oracle. Callable both from the
        orchestrator's own Go-To release and from a native release marker pushed over the sidecar
        stdin (the live path where the native engine owns the controller). `calibration=True`
        marks a controlled early release whose clean non-cap freeze may use the estimator's wider
        calibration observation bound. Public-court RTT remains telemetry-only, so both video
        routes feed the estimator the directly observed total loop. Returns the release seq."""
        # Keep enough post-release vision for the latency oracle/release diagnostic, but do not leave
        # the full long-shot acquisition authority open between shots or into a menu.
        try:
            release_seq = int(seq) if seq is not None else 0
            marker_physical_epoch = _parse_pose_arm_token(physical_epoch)
            marker_shot_attempt = _parse_pose_arm_token(shot_attempt)
            identity_present = marker_physical_epoch > 0 or marker_shot_attempt > 0
            identity_verified = bool(
                marker_physical_epoch > 0 and marker_shot_attempt > 0
                and marker_physical_epoch == _parse_pose_arm_token(
                    getattr(self, '_shot_gate_epoch', 0))
                and marker_shot_attempt == _parse_pose_arm_token(
                    getattr(self, '_active_pose_arm_token', 0)))
            if identity_present and not identity_verified:
                logger.warning(
                    "release marker dropped: ownership identity mismatch "
                    "physical_epoch=%d/%d shot_attempt=%d/%d release_seq=%d",
                    marker_physical_epoch,
                    _parse_pose_arm_token(getattr(self, '_shot_gate_epoch', 0)),
                    marker_shot_attempt,
                    _parse_pose_arm_token(getattr(self, '_active_pose_arm_token', 0)),
                    release_seq)
                return 0

            self._settle_shot_gate("release")

            def _notify_reader_release():
                notify = getattr(self._meter_detector, 'notify_release', None)
                if not callable(notify):
                    return
                try:
                    notify(release_seq, physical_epoch=marker_physical_epoch,
                           shot_attempt=marker_shot_attempt,
                           identity_verified=identity_verified)
                except TypeError:
                    notify(release_seq)

            estimator = self.latency_estimator_snapshot()
            if estimator is None:
                # Still relay the release to a calibrating reader (corroboration proxy, B3):
                # the colour lifecycle must not silently lose corroboration when the oracle
                # is disabled.
                try:
                    _notify_reader_release()
                except Exception:
                    pass
                return 0
            ts = float(wall_ms) if wall_ms is not None else time.time() * 1000.0
            # The RTT engine's authoritative target is the public NBA court peer,
            # not the PS5/Chiaki LAN transport.  It therefore cannot be subtracted
            # from command-to-visible-effect latency: decoder video returns over the
            # private Remote Play path while capture-card video returns over HDMI.
            # Keep the oracle in its directly measured TOTAL domain on both routes.
            _rtt = 0.0
            rs = estimator.mark_release(
                ts, seq, rtt_ms=None,
                calibration=bool(calibration),
                validation_target_pct=validation_target_pct,
                validation_tolerance_pct=validation_tolerance_pct)
            # Only a caller-supplied id is a native release identity.  The latency estimator's
            # local sequence is a different namespace and must never be laundered into native
            # attribution for an orchestrator-owned release.
            # wall_ms %.3f (was %.0f): sub-ms release stamps are what make the offline
            # release-phase-vs-frame-grid (lattice) joins meaningful. Existing parsers that
            # match wall_ms=(\d+) still bind the integer part.
            logger.info('release marker: seq=%d wall_ms=%.3f calibration=%d physical_epoch=%d '
                        'shot_attempt=%d identity_verified=%d target_pct=%.1f '
                        'tolerance_pct=%.1f rtt_ms=%.1f measured_latency_ms=%.1f '
                        'l_fixed=%.1f sd=%.1f',
                        rs, ts, int(bool(calibration)), marker_physical_epoch,
                        marker_shot_attempt, int(identity_verified),
                        float(validation_target_pct) if validation_target_pct is not None else -1.0,
                        (float(validation_tolerance_pct)
                         if validation_tolerance_pct is not None else -1.0), _rtt,
                        estimator.value_ms(),
                        getattr(estimator, 'l_fixed_ms', 0.0),
                        getattr(estimator, 'l_fixed_sd_ms', 0.0))
            # Corroboration proxy half 1 (plan B3 [fix]: no grade event crosses native->sidecar):
            # relay the release to a calibrating reader so "release_marker received for the shot
            # AND calibrator peak >= 70" can corroborate the current colour-training shot.
            try:
                _notify_reader_release()
            except Exception:
                pass
            return rs
        except Exception as exc:
            logger.debug(f'mark_release skip: {exc}')
            return 0

    def mark_probe(self, wall_ms: float = None, seq: int = None,
                   spawn_offset_ms: float = 0.0) -> bool:
        """[ORION_PROBE] Record a warmup pump-fake probe PRESS for the latency estimator.
        Pushed from the native engine over the sidecar stdin (probe_marker cmd). Public-court
        RTT remains telemetry-only; the probe stays in the same measured-total domain as releases."""
        try:
            estimator = self.latency_estimator_snapshot()
            if estimator is None or not hasattr(estimator, 'mark_probe'):
                # WARNING, and it names the reason. This return used to be silent, and the
                # success path logged only at INFO -- which the native relay drops. So on
                # 2026-08-05 a user-triggered probe run was invisible at this step in BOTH
                # directions: five runs produced arms, visible shots, and READER ACQUIRE lines,
                # yet no probe was ever registered, and nothing anywhere said so.
                #
                # latency_estimator_snapshot() returns None until capture warm-route validation
                # completes, and a controller-delivery route transition revokes it again. A probe
                # run started inside that window is silently discarded end to end.
                logger.warning(
                    'probe marker DROPPED: seq=%s reason=%s â€” the latency estimator is not '
                    'available yet (capture warm-route not validated, or a route transition '
                    'revoked it). Wait for the timing route to settle, then rerun.',
                    str(seq),
                    'no_estimator' if estimator is None else 'no_mark_probe_attr')
                return False
            ts = float(wall_ms) if wall_ms is not None else time.time() * 1000.0
            _rtt = 0.0
            ok = estimator.mark_probe(ts, seq, rtt_ms=None,
                                      spawn_offset_ms=spawn_offset_ms)
            # WARNING for the same reason as the drop path: a user-triggered diagnostic has to be
            # legible in the log it writes to, and INFO does not survive the relay.
            logger.warning('probe marker: seq=%s wall_ms=%.3f rtt_ms=%.1f spawn_offset=%.1f ok=%s',
                           str(seq), ts, _rtt, float(spawn_offset_ms or 0.0), bool(ok))
            return bool(ok)
        except Exception as exc:
            # Never swallow this at debug level again.
            logger.warning('mark_probe FAILED: seq=%s exc=%r', str(seq), exc)
            return False

    def calibrate_meter(self, phase: str, count: int = 0) -> bool:
        """Shoot-to-train colour calibration (plan B3 â€” implements the wired sidecar stub).
        start  = snapshot + reset + RELEARN with a 10-shot window (5 committed shots bake).
        finish = derive/clamp/persist + LOCKED (>=1 committed shot; envelope-clamped = safe).
        cancel = restore the pre-calibration snapshot.
        Per-shot progress is emitted as {"event":"calibrate_meter_status",...} on stdout
        (the native RemotePlaySession parses it into the meterCalibration* properties)."""
        det = self._meter_detector
        phase = str(phase or '').strip().lower()
        try:
            if det is None or not hasattr(det, 'ensure_calibrator'):
                logger.warning('calibrate_meter(%s): active detector has no colour calibrator '
                               '(serving chain?) -> ignored', phase)
                return False
            if phase == 'start':
                det.start_color_calibration(target=5, window=10)
                logger.warning('calibrate_meter START: RELEARN window open (5 committed shots '
                               'of 10 bake + lock)')
            elif phase in ('finish', 'stop'):
                det.finish_color_calibration()
                logger.warning('calibrate_meter FINISH (count=%s)', count)
            elif phase == 'cancel':
                det.cancel_color_calibration()
                logger.warning('calibrate_meter CANCEL: snapshot restored')
            else:
                logger.warning('calibrate_meter: unknown phase %r', phase)
                return False
            self._emit_calibration_status(force=True)
            return True
        except Exception as exc:
            logger.warning('calibrate_meter(%s) failed: %s', phase, exc)
            return False

    def _emit_calibration_status(self, force: bool = False):
        """Push the calibrator lifecycle state over stdout when it changes (per committed/
        discarded shot, per state transition). Consumed by RemotePlaySession ->
        OrionAppController meterCalibration* -> the MeterConfigPanel badge."""
        try:
            det = self._meter_detector
            fn = getattr(det, 'calibration_status', None)
            st = fn() if callable(fn) else None
            if st is None:
                return
            ver = int(st.get('version', 0))
            if not force and ver == self._last_cal_status_version:
                return
            self._last_cal_status_version = ver
            import json as _json
            payload = _json.dumps({
                "event": "calibrate_meter_status",
                "shots_done": int(st.get('shots_done', 0)),
                "shots_needed": int(st.get('shots_needed', 5)),
                "state": str(st.get('state', 'seed')),
                "learned_date": str(st.get('learned_date', '')),
                "calibrating": bool(st.get('calibrating', False)),
            }, separators=(",", ":"))
            emit_stdout_jsonl(payload + "\n")   # shared IPC lock (bughunt #4)
        except Exception as exc:
            logger.debug('calibration status emit skip: %s', exc)

    def _frame_measurement_epoch_ms(self, pts=None, epoch_ms=None,
                                    pts_to_epoch_ms=None) -> float:
        """Epoch-ms measurement timestamp for the current frame's timing consumers (latency
        oracle, tip registration, forecaster/kalman, detect ts). Decoder path rides the PTS
        clock mapped onto the epoch axis (EMA-calibrated: same mean as the raw capture stamp,
        but per-frame pipe/dequeue arrival jitter removed â€” the PTS-domain oracle feed).
        pts=0 sources use the raw capture-instant epoch stamp unchanged.

        This helper is called at capture publication, with an explicit offset snapshot,
        and its result rides in ``_frame_bundle``. Missing source epoch never falls back
        to ``time.time()``: only the actual local window/GDI path may create that stamp,
        at its capture boundary. Zero therefore remains an honest fail-closed value."""
        try:
            _pts = int(self._last_frame_pts if pts is None else (pts or 0))
            _epoch = float(
                self._last_frame_epoch_ms if epoch_ms is None else (epoch_ms or 0.0))
            _offset = float(
                self._pts_to_epoch_ms
                if pts_to_epoch_ms is None else (pts_to_epoch_ms or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if not np.isfinite(_epoch) or _epoch <= 0.0:
            return 0.0
        if _pts > 0:
            mapped = _pts / 1000.0 + _offset if _offset != 0.0 else 0.0
            return mapped if np.isfinite(mapped) and mapped > 0.0 else 0.0
        return _epoch

    def _input_router_loop(self):
        logger.info('Input router thread started')
        physical_reader = PhysicalControllerReader()
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        while self._running:
            t0 = time.perf_counter()
            try:
                state = physical_reader.read_state()
                if state is not None:
                    # Detect SQUARE rising edge to arm pose timing (controller-state arming)
                    # This is the ground-truth "shot is starting" signal â€” 100% arm rate, 60ms IQR
                    square_now = bool(state.buttons & DS4Button.SQUARE)
                    if square_now and not self._prev_square_pressed:
                        self._arm_shot_gate("square")
                        if self._pose_timing is not None:
                            self._pose_timing.notify_shot_start(self._frame_seq)
                    self._prev_square_pressed = square_now
                    # Also detect Right-Stick-Up rising edge for stick-triggered shots
                    rs_up_now = state.right_stick_y < -50
                    if rs_up_now and not self._prev_rs_up:
                        self._arm_shot_gate("stick_up")
                        if self._pose_timing is not None:
                            self._pose_timing.notify_shot_start(self._frame_seq)
                    self._prev_rs_up = rs_up_now
                    # Detect Right-Stick-Down rising edge for stick-triggered shots
                    rs_down_now = state.right_stick_y > 50
                    if rs_down_now and not self._prev_rs_down:
                        self._arm_shot_gate("stick_down")
                        if self._pose_timing is not None:
                            self._pose_timing.notify_shot_start(self._frame_seq)
                    self._prev_rs_down = rs_down_now
                    if self._remap_engine:
                        state = self._remap_engine.process(state)
                    else:
                        is_stick_up = state.right_stick_y < -50
                        if is_stick_up:
                            if not self._goto_shot_active and (not self._goto_shot_triggered):
                                logger.info('Physical right stick UP detected - arming go-to shot')
                                self._goto_shot_active = True
                                self._goto_shot_triggered = False
                                self._goto_shot_start_time = time.time()
                                self._release_time = None
                        elif not self._goto_shot_active:
                            self._goto_shot_triggered = False
                            self._release_time = None
                        if self._goto_shot_active and self._release_time is not None:
                            if time.time() >= self._release_time:
                                self._trigger_shot_release()
                                self._release_time = None
                        if self._goto_shot_active and (not self._goto_shot_triggered):
                            state.right_stick_y = -128
                            state.right_stick_x = 0
                        elif self._goto_shot_triggered:
                            state.right_stick_y = 0
                            state.right_stick_x = 0
                            state.set_button(DS4Button.SQUARE, False)
                    if self._virtual_controller:
                        self._virtual_controller.submit_state(state)
            except Exception as e:
                logger.error(f'Error in input router loop: {e}')
            elapsed = time.perf_counter() - t0
            remaining = 0.001 - elapsed
            if remaining > 0.0003:
                time.sleep(remaining - 0.0003)
            # Busy-wait the final 300us for sub-ms precision
            while time.perf_counter() - t0 < 0.001:
                pass
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        logger.info('Input router thread stopped')
