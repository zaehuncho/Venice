from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from decoder_pipe_identity import (
    ProducerExpectation,
    ProducerObservation,
    WindowsNamedPipeServerIdentityApi,
    bound_expectation_reason,
    expectation_from_owned_process,
    producer_identity_reason,
)

logger = logging.getLogger("ChiakiBackend")

# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

@dataclass
class ChiakiConfig:
    """Remote Play connection settings."""
    # Connection
    console_ip: str = ""
    psn_account_id: str = ""
    rp_regist_key: str = ""
    console_type: str = "ps5"           # ps5 | ps4 | xbox
    # Video
    resolution: str = "1080p"           # 720p | 1080p
    fps: int = 60
    bitrate_kbps: int = 30000           # 15000-50000
    codec: str = "h265"                 # h264 | h265
    hw_decoder: str = "auto"            # auto | nvdec | vaapi | d3d11va | software
    # Network
    disable_nagle: bool = True
    dscp_marking: bool = True
    # Chiaki binary
    chiaki_path: str = ""               # auto-detect if empty
    # Frame pipe
    frame_width: int = 1920
    frame_height: int = 1080
    frame_format: str = "bgr24"         # bgr24 | nv12 | yuv420p
    # I-frame forcing
    force_iframe_interval: int = 8      # request I-frame every N frames
    # Adaptive bitrate
    adaptive_bitrate: bool = True
    min_bitrate_kbps: int = 15000
    max_bitrate_kbps: int = 50000


@dataclass
class FrameData:
    """A decoded video frame with metadata."""
    frame: np.ndarray
    # Canonical capture/source instant on the host monotonic clock.  ``timestamp_ns``
    # is retained as the compatibility spelling and is normalized to this value in
    # ``__post_init__``.  Publication has its own clock below: conversion, validation,
    # copying, and callback/queue delay must increase frame age rather than restamping
    # old pixels as fresh.
    timestamp_ns: int = 0
    frame_number: int = 0
    decode_latency_ms: float = 0.0
    is_iframe: bool = False
    pts: int = 0  # decoder presentation timestamp (for frame-locked sync)
    # Epoch stamp (time.time_ns) taken at the same instant as timestamp_ns. The release
    # markers + latency oracle run on the epoch wall clock, so stamping capture in epoch
    # puts the fill timeline and the release timeline on ONE timebase (A0 clock unification).
    epoch_ns: int = 0
    capture_timestamp_ns: int = 0
    capture_epoch_ns: int = 0
    publication_timestamp_ns: int = 0
    publication_epoch_ns: int = 0
    # Native decoder Y (luma) plane, zero-copy view of the wire payload when the source is
    # the NV12/I420 frame pipe (None elsewhere). H.264 4:2:0 destroys chroma but protects
    # luma -- the compressed-path reader (CompressedMeterReader) reads fill from Y, and this
    # hands it the decoder's own luma without a BGR round trip. (backlog Tier-1 #5)
    y_plane: Optional[np.ndarray] = None
    # Raw wire payload (NV12/I420 bytes) -- populated ONLY when the pipe backend is built
    # with keep_payload=True (the dual-capture recorder archives frames LOSSLESSLY: the
    # codec artifacts ARE the training signal, never re-lossy-compress them). None on the
    # live path (no reason to hold an extra ~1.4 MB per frame there).
    payload: Optional[bytes] = None
    fmt: int = -1        # wire format when payload is kept: 0=NV12, 1=I420
    # Phase-1 freeze hardening: set True when the capture card content-stall detector
    # has exhausted its reopen attempts (3x) and the feed is genuinely frozen. The
    # sidecar reads this to emit feed_healthy=false in telemetry so the native engine
    # can suppress blind fires on a dead feed.
    feed_frozen: bool = False
    # Monotonic source-connection generation.  A backend that reconnects without
    # replacing its Python object (the Orion decoded-frame pipe) increments this so
    # downstream dedup/timestamp state cannot bleed across stream sessions.
    source_generation: int = 0
    # Capture-card route proof.  DirectShow stable-device inventory scopes any
    # persisted latency posterior, but OpenCV may resolve another index or fall
    # back to MSMF at runtime.  Safe defaults make legacy producers/test doubles
    # explicitly unverified instead of silently inheriting warm timing authority.
    capture_api: str = ""
    capture_device_index: int = -1
    capture_width: int = 0
    capture_height: int = 0
    capture_fps: float = 0.0
    capture_fourcc: str = ""
    capture_buffer_size: float = 0.0
    # Decoder-pipe v2 provenance. The producer stamps these at callback entry, before
    # rate limiting/readback/latest-wins/pipe I/O. ``timestamp_ns`` mirrors the
    # producer stamp for the normal freshness contract; arrival_timestamp_ns remains
    # available for transport/conversion diagnostics.
    producer_generation: int = 0
    producer_sequence: int = 0
    producer_timestamp_ns: int = 0
    arrival_timestamp_ns: int = 0
    # The named-pipe server was proven to be the exact Python-owned OrionStream
    # launch (PID + launch generation + process creation + path + native SHA).
    # False frames remain usable for preview/cold learning only; they can never
    # restore or persist route timing.
    producer_identity_verified: bool = False
    producer_process_id: int = 0
    producer_launch_generation: int = 0
    decoder_width: int = 0
    decoder_height: int = 0
    decoder_format: int = -1
    # Proof that ``frame`` is a private, C-contiguous, read-only allocation.  The
    # orchestrator verifies this contract before skipping its defensive full-frame
    # copy.  False is the safe default for legacy/untrusted producers.
    integrity_isolated: bool = False

    def __post_init__(self) -> None:
        """Keep the legacy and canonical capture spellings exactly equivalent.

        Legacy/test producers commonly provide only ``timestamp_ns``/``epoch_ns``;
        newer transports provide the explicit capture fields.  Publication remains
        zero when a producer cannot attest it -- fabricating ``now`` here would hide
        producer/callback delay and violate the freshness contract.
        """
        capture_ns = int(self.capture_timestamp_ns or self.timestamp_ns or 0)
        capture_epoch_ns = int(self.capture_epoch_ns or self.epoch_ns or 0)
        self.capture_timestamp_ns = capture_ns
        self.timestamp_ns = capture_ns
        self.capture_epoch_ns = capture_epoch_ns
        self.epoch_ns = capture_epoch_ns


# ---------------------------------------------------------------------------
#  Chiaki Process Manager
# ---------------------------------------------------------------------------

class ChiakiProcess:
    """Manages the Chiaki subprocess lifecycle."""

    def __init__(self, config: ChiakiConfig) -> None:
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running and self._process is not None and self._process.poll() is None

    def _find_chiaki_binary(self) -> str:
        """Locate chiaki/chiaki-ng executable."""
        if self._config.chiaki_path and os.path.isfile(self._config.chiaki_path):
            return self._config.chiaki_path

        # Search common locations
        search_names = ["OrionStream.exe", "chiaki-ng.exe", "chiaki.exe", "chiaki4deck.exe"]
        search_dirs = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "native_orion", "deploy", "chiaki-ng-orion", "chiaki-ng-Win"),
            os.path.dirname(os.path.abspath(__file__)),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "chiaki"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "chiaki"),
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Chiaki"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chiaki"),
        ]

        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for name in search_names:
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    return path

        # Try PATH
        for name in search_names:
            found = shutil.which(name)
            if found:
                return found

        return ""

    def _build_command(self) -> List[str]:
        """Build Chiaki launch command with optimized settings."""
        binary = self._find_chiaki_binary()
        if not binary:
            raise FileNotFoundError(
                "Chiaki binary not found. Install chiaki-ng from "
                "https://sr.ht/~thestr4ng3r/chiaki/ and place it in "
                "the NexusVision/vendor/chiaki/ directory."
            )

        cmd = [binary]

        # Stream mode with raw frame pipe output
        cmd.extend(["stream", self._config.console_ip])

        # Resolution and FPS
        res_map = {"720p": "720", "1080p": "1080"}
        cmd.extend(["--resolution", res_map.get(self._config.resolution, "1080")])
        cmd.extend(["--fps", str(self._config.fps)])

        # Bitrate
        cmd.extend(["--bitrate", str(self._config.bitrate_kbps)])

        # Codec
        if self._config.codec == "h265":
            cmd.append("--h265")

        # Hardware decoder
        if self._config.hw_decoder != "software":
            cmd.extend(["--hw-decoder", self._config.hw_decoder])

        # Registration key
        if self._config.rp_regist_key:
            cmd.extend(["--registkey", self._config.rp_regist_key])

        # PSN Account ID
        if self._config.psn_account_id:
            cmd.extend(["--psn-account-id", self._config.psn_account_id])

        # Console type
        if self._config.console_type == "ps4":
            cmd.append("--ps4")

        # Raw frame pipe output for CV
        cmd.extend(["--ffmpeg-pipe-stdout"])
        cmd.extend(["--ffmpeg-pipe-format", self._config.frame_format])

        return cmd

    def start(self) -> bool:
        """Launch Chiaki subprocess."""
        with self._lock:
            if self._running:
                return True
            try:
                cmd = self._build_command()
                logger.info("Starting Chiaki: %s", " ".join(cmd))

                env = dict(os.environ)
                if self._config.disable_nagle:
                    env["CHIAKI_DISABLE_NAGLE"] = "1"
                # Native Orion owns the physical controller -> virtual pad route.
                # Chiaki should not open the plugged-in Sony HID directly, or it
                # races Orion and the pad appears to stop working when streaming
                # starts. The native virtual output is XUSB-first, so these SDL
                # filters still leave Orion's virtual pad visible to Chiaki.
                env["SDL_GAMECONTROLLER_IGNORE_DEVICES"] = (
                    "0x054c/0x0ce6,0x054c/0x05c4,0x054c/0x09cc,"
                    "0x054c/0x0df2,0x054c/0x0e5f"
                )
                env["SDL_JOYSTICK_HIDAPI_PS5"] = "0"
                env["SDL_JOYSTICK_HIDAPI_PS4"] = "0"

                CREATE_NO_WINDOW = 0x08000000
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                    bufsize=0,
                )
                self._running = True
                logger.info("Chiaki started (PID %d)", self._process.pid)
                return True
            except Exception as e:
                logger.error("Failed to start Chiaki: %s", e)
                self._running = False
                return False

    def stop(self) -> None:
        """Gracefully stop Chiaki."""
        with self._lock:
            self._running = False
            proc = self._process
            self._process = None

        if proc is None:
            return

        try:
            if proc.poll() is None:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            logger.info("Chiaki stopped")
        except Exception as e:
            logger.warning("Error stopping Chiaki: %s", e)

    @property
    def stdout(self):
        with self._lock:
            return self._process.stdout if self._process else None

    @property
    def stderr(self):
        with self._lock:
            return self._process.stderr if self._process else None


# ---------------------------------------------------------------------------
#  Frame Decoder - Reads raw frames from Chiaki pipe
# ---------------------------------------------------------------------------

class FrameDecoder:
    """Decodes raw video frames from Chiaki's pipe output."""

    def __init__(self, config: ChiakiConfig) -> None:
        self._config = config
        self._frame_size = self._calc_frame_size()
        self._frame_number = 0
        self._use_cuda = False
        self._cuda_stream = None
        self._probe_cuda()

    def _calc_frame_size(self) -> int:
        w, h = self._config.frame_width, self._config.frame_height
        fmt = self._config.frame_format
        if fmt == "bgr24":
            return w * h * 3
        elif fmt in ("nv12", "yuv420p"):
            return w * h * 3 // 2
        return w * h * 3

    def _probe_cuda(self) -> None:
        """Check if CUDA-accelerated color conversion is available."""
        try:
            cuda_mod = getattr(cv2, "cuda", None)
            if cuda_mod and hasattr(cuda_mod, "cvtColor"):
                count = cuda_mod.getCudaEnabledDeviceCount()
                if count > 0:
                    self._use_cuda = True
                    logger.info("CUDA color conversion available (%d devices)", count)
                    return
        except Exception:
            pass
        self._use_cuda = False
        logger.info("Using CPU color conversion")

    def decode_frame(self, raw_bytes: bytes) -> Optional[FrameData]:
        """Decode raw bytes into a BGR numpy array."""
        t0 = time.perf_counter_ns()
        capture_epoch_ns = time.time_ns()
        w, h = self._config.frame_width, self._config.frame_height
        fmt = self._config.frame_format

        try:
            if fmt == "bgr24":
                frame = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((h, w, 3))
            elif fmt == "nv12":
                yuv = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((h * 3 // 2, w))
                if self._use_cuda:
                    frame = self._cuda_nv12_to_bgr(yuv)
                else:
                    frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            elif fmt == "yuv420p":
                yuv = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((h * 3 // 2, w))
                if self._use_cuda:
                    frame = self._cuda_yuv420_to_bgr(yuv)
                else:
                    frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
            else:
                frame = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((h, w, 3))

            self._frame_number += 1
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0

            publication_ns = time.perf_counter_ns()
            publication_epoch_ns = time.time_ns()
            return FrameData(
                frame=frame,
                timestamp_ns=t0,
                epoch_ns=capture_epoch_ns,
                capture_timestamp_ns=t0,
                capture_epoch_ns=capture_epoch_ns,
                publication_timestamp_ns=publication_ns,
                publication_epoch_ns=publication_epoch_ns,
                frame_number=self._frame_number,
                decode_latency_ms=elapsed_ms,
            )
        except Exception as e:
            logger.warning("Frame decode error: %s", e)
            return None

    def _cuda_nv12_to_bgr(self, yuv: np.ndarray) -> np.ndarray:
        try:
            gpu_mat = cv2.cuda.GpuMat()
            gpu_mat.upload(yuv)
            bgr_gpu = cv2.cuda.cvtColor(gpu_mat, cv2.COLOR_YUV2BGR_NV12)
            return bgr_gpu.download()
        except Exception:
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)

    def _cuda_yuv420_to_bgr(self, yuv: np.ndarray) -> np.ndarray:
        try:
            gpu_mat = cv2.cuda.GpuMat()
            gpu_mat.upload(yuv)
            bgr_gpu = cv2.cuda.cvtColor(gpu_mat, cv2.COLOR_YUV2BGR_I420)
            return bgr_gpu.download()
        except Exception:
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def reconfigure(self, width: int, height: int) -> bool:
        """Update the expected frame geometry (and packed size) to match the
        stream's ACTUAL negotiated resolution. The raw pipe is headerless and
        tightly packed, so if these dims don't match the real stream the reader
        slices frames on the wrong byte boundaries → the Y/chroma planes misalign
        → the 'purple-stained' picture. Returns True if anything changed."""
        if width <= 0 or height <= 0:
            return False
        if width == self._config.frame_width and height == self._config.frame_height:
            return False
        self._config.frame_width = int(width)
        self._config.frame_height = int(height)
        self._frame_size = self._calc_frame_size()
        logger.info("Decoder geometry set to %dx%d (frame_size=%d, fmt=%s)",
                    width, height, self._frame_size, self._config.frame_format)
        return True


# ---------------------------------------------------------------------------
#  Ring Buffer for inter-thread frame passing
# ---------------------------------------------------------------------------

class FrameRingBuffer:
    """Lock-free-ish ring buffer for passing frames between threads.

    The writer (decode thread) always succeeds - overwrites oldest frame.
    The reader (CV thread) gets the latest frame without blocking.
    """

    def __init__(self, capacity: int = 4) -> None:
        self._capacity = max(2, capacity)
        self._buffer: List[Optional[FrameData]] = [None] * self._capacity
        self._write_idx = 0
        self._read_idx = 0
        self._lock = threading.Lock()
        self._event = threading.Event()

    def put(self, frame: FrameData) -> None:
        with self._lock:
            # This is the actual latest-wins publication boundary. Preserve the
            # producer/capture clock and stamp queue visibility separately so any
            # decode, copy, validation, or delayed callback time remains observable.
            frame.publication_timestamp_ns = time.perf_counter_ns()
            frame.publication_epoch_ns = time.time_ns()
            self._buffer[self._write_idx % self._capacity] = frame
            self._write_idx += 1
            self._event.set()

    def get_latest(self, timeout: float = 0.05) -> Optional[FrameData]:
        """Get the most recent frame, blocking up to timeout."""
        if not self._event.wait(timeout):
            return None
        with self._lock:
            if self._write_idx == 0:
                return None
            idx = (self._write_idx - 1) % self._capacity
            frame = self._buffer[idx]
            self._read_idx = self._write_idx
            self._event.clear()
            return frame

    def get_latest_nonblocking(self) -> Optional[FrameData]:
        with self._lock:
            if self._write_idx == 0:
                return None
            idx = (self._write_idx - 1) % self._capacity
            return self._buffer[idx]

    @property
    def frames_available(self) -> int:
        with self._lock:
            return max(0, self._write_idx - self._read_idx)

    @property
    def total_frames(self) -> int:
        with self._lock:
            return self._write_idx

    def clear(self) -> None:
        with self._lock:
            self._buffer = [None] * self._capacity
            self._write_idx = 0
            self._read_idx = 0
            self._event.clear()


# ---------------------------------------------------------------------------
#  Chiaki Backend - Main interface
# ---------------------------------------------------------------------------

class ChiakiBackend:
    """High-level Remote Play backend.

    Usage:
        backend = ChiakiBackend(config)
        backend.start()
        ...
        frame = backend.get_frame()  # returns latest FrameData or None
        ...
        backend.stop()
    """

    def __init__(self, config: Optional[ChiakiConfig] = None) -> None:
        self._config = config or ChiakiConfig()
        self._process = ChiakiProcess(self._config)
        self._decoder = FrameDecoder(self._config)
        self._ring = FrameRingBuffer(capacity=4)
        self._stop_evt = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stats = {
            "frames_decoded": 0,
            "frames_dropped": 0,
            "decode_errors": 0,
            "avg_decode_ms": 0.0,
            "last_frame_ts": 0.0,
            "fps_actual": 0.0,
            "connected": False,
            "stream_active": False,
        }
        self._stats_lock = threading.Lock()
        self._fps_samples: List[float] = []
        self._on_frame_callback: Optional[Callable[[FrameData], None]] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None
        # Stream resolution detected from Chiaki stderr. The stderr thread parses it
        # and stashes it here; the frame reader applies it (re-aligning framing) so a
        # preset that streams a non-default resolution doesn't desync into a
        # purple-stained picture. Guarded by _geom_lock.
        self._geom_lock = threading.Lock()
        self._pending_geom: Optional[Tuple[int, int]] = None

    @property
    def connected(self) -> bool:
        return self._process.running

    @property
    def config(self) -> ChiakiConfig:
        return self._config

    def set_on_frame(self, callback: Callable[[FrameData], None]) -> None:
        self._on_frame_callback = callback

    def set_on_disconnect(self, callback: Callable[[], None]) -> None:
        self._on_disconnect_callback = callback

    def start(self) -> bool:
        """Start Remote Play connection and frame capture."""
        self._stop_evt.clear()
        self._ring.clear()

        if not self._process.start():
            return False

        self._reader_thread = threading.Thread(
            target=self._frame_reader_loop,
            name="ChiakiFrameReader",
            daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            name="ChiakiStderrReader",
            daemon=True,
        )
        self._stderr_thread.start()

        with self._stats_lock:
            self._stats["connected"] = True
        logger.info("ChiakiBackend started")
        return True

    def stop(self) -> None:
        """Stop Remote Play and cleanup."""
        self._stop_evt.set()
        self._process.stop()

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3.0)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)

        with self._stats_lock:
            self._stats["connected"] = False
            self._stats["stream_active"] = False
        logger.info("ChiakiBackend stopped")

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        """Get the latest decoded frame (blocks up to timeout)."""
        return self._ring.get_latest(timeout)

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        """Get the latest frame without blocking."""
        return self._ring.get_latest_nonblocking()

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return dict(self._stats)

    def update_bitrate(self, kbps: int) -> None:
        """Update target bitrate for the next Chiaki session.

        NOTE: Chiaki does not support runtime bitrate changes via IPC.
        This only updates the config value — it takes effect on the next
        start() call, not during an active stream.
        """
        self._config.bitrate_kbps = max(
            self._config.min_bitrate_kbps,
            min(self._config.max_bitrate_kbps, kbps)
        )

    # -----------------------------------------------------------------------
    #  Internal threads
    # -----------------------------------------------------------------------

    def _frame_reader_loop(self) -> None:
        """Continuously read raw frames from Chiaki stdout pipe."""
        stdout = self._process.stdout
        if stdout is None:
            logger.error("No stdout pipe from Chiaki")
            return

        frame_size = self._decoder.frame_size
        buf = bytearray()
        decode_times: List[float] = []
        fps_window_start = time.perf_counter()
        fps_frame_count = 0

        with self._stats_lock:
            self._stats["stream_active"] = True

        while not self._stop_evt.is_set():
            try:
                # Apply a stderr-detected resolution change: re-align framing to the
                # true geometry and drop the partial buffer so we don't slice a frame
                # across the old/new boundary (which is what tinted the picture).
                pending = None
                with self._geom_lock:
                    if self._pending_geom is not None:
                        pending = self._pending_geom
                        self._pending_geom = None
                if pending is not None and self._decoder.reconfigure(pending[0], pending[1]):
                    frame_size = self._decoder.frame_size
                    buf.clear()

                chunk = stdout.read(min(frame_size - len(buf), 65536))
                if not chunk:
                    break
                buf.extend(chunk)

                while len(buf) >= frame_size:
                    raw = bytes(buf[:frame_size])
                    del buf[:frame_size]

                    frame_data = self._decoder.decode_frame(raw)
                    if frame_data is None:
                        with self._stats_lock:
                            self._stats["decode_errors"] += 1
                        continue

                    self._ring.put(frame_data)
                    decode_times.append(frame_data.decode_latency_ms)

                    # FPS calculation
                    fps_frame_count += 1
                    now = time.perf_counter()
                    elapsed = now - fps_window_start
                    if elapsed >= 1.0:
                        fps = fps_frame_count / elapsed
                        avg_decode = sum(decode_times) / len(decode_times) if decode_times else 0.0
                        with self._stats_lock:
                            self._stats["frames_decoded"] += fps_frame_count
                            self._stats["fps_actual"] = round(fps, 1)
                            self._stats["avg_decode_ms"] = round(avg_decode, 2)
                            self._stats["last_frame_ts"] = now
                        fps_frame_count = 0
                        fps_window_start = now
                        decode_times.clear()

                    # Callback
                    if self._on_frame_callback:
                        try:
                            self._on_frame_callback(frame_data)
                        except Exception:
                            pass

            except Exception as e:
                if not self._stop_evt.is_set():
                    logger.warning("Frame read error: %s", e)
                break

        with self._stats_lock:
            self._stats["stream_active"] = False

        if not self._stop_evt.is_set():
            logger.warning("Chiaki stream ended unexpectedly")
            if self._on_disconnect_callback:
                try:
                    self._on_disconnect_callback()
                except Exception:
                    pass

    # Resolutions Chiaki/PS Remote Play actually negotiates per quality preset.
    # The pipe is headerless, so we learn the real geometry from stderr and feed
    # the reader; anything outside this set is treated as a false match.
    _KNOWN_RESOLUTIONS = {
        (3840, 2160), (2560, 1440), (1920, 1080), (1600, 900),
        (1280, 720), (960, 540), (848, 480), (640, 360),
    }
    _RES_RE = re.compile(r"(?<!\d)(\d{3,4})\s*[xX×]\s*(\d{3,4})(?!\d)")

    def _stderr_reader_loop(self) -> None:
        """Read Chiaki stderr for log/error messages, and learn the negotiated
        video resolution so the frame reader can align to it (prevents the
        purple-stained desync on non-default quality presets)."""
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for line in iter(stderr.readline, b""):
                if self._stop_evt.is_set():
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                logger.debug("[Chiaki] %s", text)
                for m in self._RES_RE.finditer(text):
                    w, h = int(m.group(1)), int(m.group(2))
                    if (w, h) in self._KNOWN_RESOLUTIONS:
                        with self._geom_lock:
                            self._pending_geom = (w, h)
                        break
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  Screen Capture Fallback (when Chiaki pipe unavailable)
# ---------------------------------------------------------------------------

class ScreenCaptureBackend:
    """Fallback: capture Remote Play window via Win32 GDI BitBlt.

    Used when Chiaki doesn't support raw pipe output, or when
    the user runs their own Remote Play client (official Sony/MS app).

    Finds the target window by title and captures its client area. It never
    captures the full desktop, because that can recursively capture Orion
    instead of console video.
    """

    # Window titles to search for (checked in order)
    _WINDOW_TITLES = [
        "PS Remote Play",
        "PS4 Remote Play",
        "chiaki",
        "chiaki-ng",
        "Chiaki",
        "Xbox",
        "Xbox Remote Play",
    ]

    def __init__(self, config: Optional[ChiakiConfig] = None) -> None:
        self._config = config or ChiakiConfig()
        self._ring = FrameRingBuffer(capacity=4)
        self._stop_evt = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._hwnd: int = 0
        self._window_title: str = ""
        self._stats = {
            "frames_captured": 0,
            "fps_actual": 0.0,
            "capture_latency_ms": 0.0,
            "connected": False,
            "capture_mode": "none",
            "window_title": "",
        }
        self._stats_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        with self._stats_lock:
            return self._stats["connected"]

    def _find_window(self) -> int:
        """Find the Remote Play window handle by title."""
        if os.name != "nt":
            return 0
        try:
            import ctypes
            user32 = ctypes.windll.user32

            # Try exact titles first
            for title in self._WINDOW_TITLES:
                hwnd = user32.FindWindowW(None, title)
                if hwnd and hwnd != 0:
                    self._window_title = title
                    logger.info("Found Remote Play window: '%s' (HWND=0x%X)", title, hwnd)
                    return hwnd

            # Partial match: enumerate all windows
            found_hwnd = 0
            found_title = ""

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def _enum_cb(hwnd, _lparam):
                nonlocal found_hwnd, found_title
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.lower()
                for target in ("remote play", "chiaki", "ps remote"):
                    if target in title:
                        found_hwnd = hwnd
                        found_title = buf.value
                        return False  # Stop enumeration
                return True

            user32.EnumWindows(_enum_cb, 0)
            if found_hwnd:
                self._window_title = found_title
                logger.info("Found Remote Play window (partial): '%s' (HWND=0x%X)", found_title, found_hwnd)
                return found_hwnd

        except Exception as e:
            logger.warning("Window search failed: %s", e)
        return 0

    def _capture_window_gdi(self, hwnd: int) -> Optional[np.ndarray]:
        """Capture window client area via Win32 GDI BitBlt."""
        try:
            import ctypes
            import ctypes.wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            if not hwnd or hwnd == 0:
                return None

            # Get client rect dimensions
            rect = ctypes.wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))

            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return None

            # Get device contexts
            src_dc = user32.GetDC(hwnd)
            if not src_dc:
                return None

            mem_dc = gdi32.CreateCompatibleDC(src_dc)
            bmp = gdi32.CreateCompatibleBitmap(src_dc, w, h)
            old_bmp = gdi32.SelectObject(mem_dc, bmp)

            # BitBlt: copy source window into our bitmap
            SRCCOPY = 0x00CC0020
            gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, 0, 0, SRCCOPY)

            # Read bitmap data into numpy array
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint),
                    ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int),
                    ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort),
                    ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint),
                    ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int),
                    ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # Top-down DIB
            bmi.biPlanes = 1
            bmi.biBitCount = 32  # BGRA
            bmi.biCompression = 0  # BI_RGB

            buf_size = w * h * 4
            buf = ctypes.create_string_buffer(buf_size)

            DIB_RGB_COLORS = 0
            gdi32.GetDIBits(
                mem_dc, bmp, 0, h,
                buf, ctypes.byref(bmi), DIB_RGB_COLORS,
            )

            # Cleanup GDI resources
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, src_dc)

            # Convert BGRA buffer to BGR numpy array
            img = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
            bgr = img[:, :, :3].copy()  # Drop alpha, ensure contiguous

            # Resize to configured resolution if needed
            target_w, target_h = self._config.frame_width, self._config.frame_height
            if bgr.shape[1] != target_w or bgr.shape[0] != target_h:
                bgr = cv2.resize(bgr, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

            return bgr

        except Exception as e:
            logger.warning("GDI capture failed: %s", e)
            return None

    def start(self, device_index: int = 0) -> bool:
        """Start screen capture targeting the Remote Play window."""
        self._stop_evt.clear()

        # Find the Remote Play window
        self._hwnd = self._find_window()
        mode = "window"

        if not self._hwnd:
            logger.error("No Remote Play window found; desktop capture is disabled")
            return False

        # Verify GDI capture works
        test = self._capture_window_gdi(self._hwnd)
        if test is None:
            logger.error("GDI capture test failed")
            return False

        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="ScreenCapture",
            daemon=True,
        )
        self._capture_thread.start()
        with self._stats_lock:
            self._stats["connected"] = True
            self._stats["capture_mode"] = mode
            self._stats["window_title"] = self._window_title
        logger.info("Screen capture started (%s: %s)", mode, self._window_title)
        return True

    def stop(self) -> None:
        self._stop_evt.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3.0)
        self._hwnd = 0
        with self._stats_lock:
            self._stats["connected"] = False

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        return self._ring.get_latest(timeout)

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        return self._ring.get_latest_nonblocking()

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return dict(self._stats)

    def _capture_loop(self) -> None:
        frame_num = 0
        fps_start = time.perf_counter()
        fps_count = 0
        target_interval = 1.0 / max(1, self._config.fps)
        window_recheck_interval = 5.0  # Re-find window every 5s if lost
        last_window_check = time.perf_counter()

        while not self._stop_evt.is_set():
            t0 = time.perf_counter()

            # Re-find window periodically if targeting a specific one
            if self._window_title and (t0 - last_window_check) >= window_recheck_interval:
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    if self._hwnd and not user32.IsWindow(self._hwnd):
                        logger.info("Target window lost, re-searching...")
                        self._hwnd = self._find_window()
                except Exception:
                    pass
                last_window_check = t0

            frame = self._capture_window_gdi(self._hwnd)
            capture_ns = time.perf_counter_ns()
            capture_epoch_ns = time.time_ns()
            if frame is None:
                time.sleep(0.005)
                continue

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            frame_num += 1
            fps_count += 1

            publication_ns = time.perf_counter_ns()
            publication_epoch_ns = time.time_ns()
            fd = FrameData(
                frame=frame,
                timestamp_ns=capture_ns,
                epoch_ns=capture_epoch_ns,
                capture_timestamp_ns=capture_ns,
                capture_epoch_ns=capture_epoch_ns,
                publication_timestamp_ns=publication_ns,
                publication_epoch_ns=publication_epoch_ns,
                frame_number=frame_num,
                decode_latency_ms=elapsed_ms,
            )
            self._ring.put(fd)

            now = time.perf_counter()
            if now - fps_start >= 1.0:
                with self._stats_lock:
                    self._stats["frames_captured"] += fps_count
                    self._stats["fps_actual"] = round(fps_count / (now - fps_start), 1)
                    self._stats["capture_latency_ms"] = round(elapsed_ms, 2)
                fps_count = 0
                fps_start = now

            # Frame pacing — don't burn CPU faster than target FPS
            remaining = target_interval - (time.perf_counter() - t0)
            if remaining > 0.001:
                time.sleep(remaining)


# ---------------------------------------------------------------------------
#  Unified Remote Play Interface
# ---------------------------------------------------------------------------

class RemotePlayEngine:
    """Unified interface that picks the best available backend.

    Priority:
      1. Chiaki pipe (lowest latency)
      2. Screen capture (fallback)
    """

    def __init__(self, config: Optional[ChiakiConfig] = None) -> None:
        self._config = config or ChiakiConfig()
        self._backend: Any = None
        self._mode = "none"
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._backend.connected if self._backend else False

    def start(self) -> bool:
        """Start the best available backend."""
        with self._lock:
            # Try Chiaki first
            if self._config.console_ip:
                chiaki = ChiakiBackend(self._config)
                if chiaki.start():
                    self._backend = chiaki
                    self._mode = "chiaki"
                    logger.info("Remote Play started via Chiaki")
                    return True
                logger.warning("Chiaki failed, falling back to screen capture")

            # Fallback to screen capture
            capture = ScreenCaptureBackend(self._config)
            if capture.start():
                self._backend = capture
                self._mode = "screen_capture"
                logger.info("Remote Play started via screen capture")
                return True

            self._mode = "none"
            return False

    def stop(self) -> None:
        with self._lock:
            if self._backend:
                self._backend.stop()
                self._backend = None
            self._mode = "none"

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        with self._lock:
            if self._backend:
                return self._backend.get_frame(timeout)
            return None

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        with self._lock:
            if self._backend:
                return self._backend.get_frame_nonblocking()
            return None

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            stats = self._backend.get_stats() if self._backend else {}
            stats["mode"] = self._mode
            return stats


# ---------------------------------------------------------------------------
#  Orion decoded-frame pipe backend (ATTACH mode)
# ---------------------------------------------------------------------------

class OrionFramePipeBackend:
    """Reads decoded frames the LIVE chiaki GUI exports over a named pipe (the chiaki-side
    OrionFrameExport, gated by env CHIAKI_ORION_FRAME_PIPE).

    Unlike ChiakiBackend this does NOT spawn its own headless chiaki -- the bot's main path already
    runs the GUI chiaki (for the user's display + the input hook), and that one process exports each
    decoded frame here. So the bot detects the shot meter from the SAME frame chiaki presents (fresh,
    true-resolution, pre-composite) instead of GDI-capturing the window -- which also frees chiaki to
    use the low-latency Vulkan renderer (GDI BitBlt blacks on a Vulkan swapchain).

    Wire format (per frame): an exact 64-byte v2 header then the packed YUV payload. It carries magic,
    version/header size, exporter generation, decoder-callback sequence, callback-time QPC nanoseconds,
    geometry/format/length, and PTS. The identity and timestamp are assigned before the producer's
    latest-wins slot, so queue overwrites become visible gaps and pipe delay can never be restamped fresh.
    The deliberately incompatible fixed header makes stale v1 producers fail closed.
    Same interface as ChiakiBackend (start/stop/get_frame_nonblocking) so it drops straight into the
    orchestrator's decoder path."""

    # magic, version, header_size, flags, generation, callback_seq,
    # producer_monotonic_ns, w, h, fmt, payload_len, pts, reserved -> 64 bytes
    _HEADER = struct.Struct("<IHHIQQQIIIIqI")
    _MAGIC = 0x5246524F                  # 'ORFR'
    _VERSION = 2
    # Authoritative freshness budget. producer_monotonic_ns and arrival_perf_ns are BOTH
    # QueryPerformanceCounter samples (see orionframeexport.cpp monotonicNowNs), so this
    # difference is a true elapsed duration and never drifts. This is the ONLY staleness
    # test on the wire path -- see _wire_pts_reason for why PTS must not be used for one.
    _MAX_PRODUCER_AGE_NS = 50_000_000    # producer callback -> full payload read
    _MAX_PRODUCER_FUTURE_NS = 2_000_000  # only QPC conversion/rounding tolerance
    # Liveness escape: no freshness gate may reject 100% of frames indefinitely. After this
    # many CONSECUTIVE rejects the monotonic baselines are re-seeded and readiness is
    # dropped so the stall is surfaced instead of silently starving the detector. This never
    # widens _MAX_PRODUCER_AGE_NS: every frame is still age-checked against the real clock.
    _WIRE_REJECT_REBASELINE_FRAMES = 30
    _FMT_NV12 = 0
    _FMT_I420 = 1
    _MAX_DIMENSION = 4096

    # BT.709 limited-range YUV->BGR (rows = B,G,R), used by cv2.transform on merged YUV.
    # The PS5 Remote Play stream is BT.709; cv2.COLOR_YUV2BGR_NV12 applies BT.601
    # constants, which hue-shifts saturated content -- pure meter red drifts toward
    # orange and out of the reader's narrow inRange (measured on the re-encode probe:
    # a 601/709 matrix mismatch alone zeroed the red mask). ORION_PIPE_BT709=0 reverts.
    _BT709_M = np.array([
        [1.164383, 2.112402, 0.000000, -289.017],   # B
        [1.164383, -0.213249, -0.532909, 76.878],   # G
        [1.164383, 0.000000, 1.792741, -248.078],   # R
    ], dtype=np.float64)

    def __init__(self, pipe_name: str = r"\\.\pipe\orion_frames", connect_timeout_s: float = 6.0,
                 keep_payload: bool = False, *, expected_producer_path: str = "",
                 expected_producer_size: int = -1,
                 expected_producer_sha256: str = "",
                 owned_process_identity_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 producer_identity_api=None) -> None:
        self._pipe_name = pipe_name
        self._connect_timeout_s = connect_timeout_s
        self._ring = FrameRingBuffer(capacity=2)
        self._keep_payload = bool(keep_payload)   # dual-capture recorder: archive raw NV12
        self._bt709 = os.environ.get("ORION_PIPE_BT709", "1").strip().lower() \
            not in ("0", "false", "no", "off")
        self._logged_matrix = False
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handle = None
        self._expected_producer_path = str(expected_producer_path or "")
        try:
            self._expected_producer_size = int(expected_producer_size)
        except (TypeError, ValueError, OverflowError):
            self._expected_producer_size = -1
        self._expected_producer_sha256 = str(expected_producer_sha256 or "").strip().lower()
        self._owned_process_identity_provider = owned_process_identity_provider
        self._producer_identity_api = (
            producer_identity_api
            if producer_identity_api is not None
            else WindowsNamedPipeServerIdentityApi()
        )
        self._producer_identity_lock = threading.Lock()
        self._producer_identity_state = "pending"
        self._producer_identity_reason = ""
        self._bound_producer_expectation = ProducerExpectation()
        self._bound_producer_observation: Optional[ProducerObservation] = None
        self._config = ChiakiConfig()   # orchestrator sets console_ip on it; unused in attach mode
        self._frame_number = 0
        self._connection_generation = 0
        self._source_generation = 0
        self._producer_generation = 0
        self._connected = False
        self._last_frame_arrival_ns = 0
        # Decoder PTS is microseconds and is only checked for ORDERING (see
        # _wire_pts_reason).  Freshness comes from producer_monotonic_ns, a real
        # QPC clock, so no arrival-vs-PTS baseline is kept: that baseline drifted
        # with the 59.94-vs-60.000 source mismatch and latched the feed off.
        self._wire_pts_last = 0
        self._wire_producer_ts_last = 0
        # Liveness escape (applies to EVERY wire gate, not just one reason).
        self._wire_consecutive_rejects = 0
        self._wire_reject_latch_logged = False
        self._last_geom: Tuple[int, int] = (0, 0)
        self._last_wire_mode: Tuple[int, int, int] = (0, 0, -1)
        # Export-rate diagnostics. v2 assigns callback_seq at decoder-callback entry,
        # before FPS gating/readback/latest-wins, so gaps expose every producer-side
        # skip or overwrite rather than merely discontinuities after the writer slot.
        self._wire_seq_last = 0           # last seq seen on the wire
        self._wire_seq_gaps = 0           # observed wire discontinuities (sum of jumps > 1)
        self._export_count = 0            # frames received this measurement window
        self._export_fps = 0.0            # frames/sec arriving on the pipe (== decode/export rate)
        self._export_gap_fps = 0.0        # observed missing wire identities/sec
        self._export_fps_t0 = time.perf_counter()
        self._wire_reject_counts: Dict[str, int] = {}
        self._wire_reject_last_log_s: Dict[str, float] = {}
        # Reusable YUV->BGR scratch, keyed by geometry.  These hold INTERMEDIATES
        # only; the returned frame is always freshly allocated because the ring
        # keeps a reference to it (the OWNDATA isolation contract in _reader_loop).
        # Measured 1080p: 6.91 ms -> 3.95 ms per frame, bit-identical output.
        self._cvt_geom: Tuple[int, int] = (0, 0)
        self._cvt_u_full: Optional[np.ndarray] = None
        self._cvt_v_full: Optional[np.ndarray] = None
        self._cvt_merged: Optional[np.ndarray] = None

    @property
    def config(self) -> ChiakiConfig:
        return self._config

    def _set_producer_identity_state(self, state: str, reason: str = "") -> None:
        with self._producer_identity_lock:
            self._producer_identity_state = str(state or "unverified")
            self._producer_identity_reason = str(reason or "")

    def producer_identity_status(self) -> Dict[str, Any]:
        """Return non-sensitive pipe provenance for the timing-authority gate."""
        with self._producer_identity_lock:
            expectation = self._bound_producer_expectation
            observation = self._bound_producer_observation
            return {
                "state": self._producer_identity_state,
                "reason": self._producer_identity_reason,
                "verified": self._producer_identity_state == "verified",
                "pid": int(getattr(observation, "pid", 0) or 0),
                "launch_generation": int(
                    getattr(expectation, "launch_generation", 0) or 0),
            }

    def _current_producer_expectation(self) -> Tuple[ProducerExpectation, str]:
        provider = self._owned_process_identity_provider
        if not callable(provider):
            owned = {}
        else:
            try:
                owned = provider() or {}
            except Exception:
                owned = {}
        return expectation_from_owned_process(
            owned,
            self._expected_producer_path,
            self._expected_producer_size,
            self._expected_producer_sha256,
        )

    def _release_producer_lease(self) -> None:
        observation = self._bound_producer_observation
        self._bound_producer_observation = None
        self._bound_producer_expectation = ProducerExpectation()
        try:
            self._producer_identity_api.close(observation)
        except Exception:
            pass

    def _bind_pipe_producer(self) -> None:
        """Inspect the *server* process for this just-opened client handle.

        Failure deliberately does not tear down decoded video.  It marks the
        connection unverified so the orchestrator permanently moves timing to an
        unscoped cold estimator for this process.
        """
        self._release_producer_lease()
        expected, reason = self._current_producer_expectation()
        if reason == "expected_process_missing":
            # CreateProcess makes the child runnable just before Popen publishes
            # its object/PID back to the manager. The exporter can therefore win
            # a very small startup race and create its server pipe first. Bound
            # that race locally instead of permanently poisoning an otherwise
            # exact session; no frame bytes are trusted during this wait.
            deadline = time.perf_counter() + 0.25
            while (reason == "expected_process_missing"
                   and not self._stop_evt.is_set()
                   and time.perf_counter() < deadline):
                self._stop_evt.wait(0.002)
                expected, reason = self._current_producer_expectation()
        if reason:
            self._set_producer_identity_state("unverified", reason)
            logger.warning("OrionFramePipeBackend: producer identity unavailable reason=%s", reason)
            return
        try:
            observed, inspect_reason = self._producer_identity_api.inspect(self._handle)
        except Exception:
            observed, inspect_reason = None, "server_identity_query_failed"
        if inspect_reason or observed is None:
            reason = inspect_reason or "server_identity_query_failed"
            self._set_producer_identity_state("unverified", reason)
            logger.warning("OrionFramePipeBackend: producer identity unavailable reason=%s", reason)
            return
        reason = producer_identity_reason(expected, observed)
        if reason:
            try:
                self._producer_identity_api.close(observed)
            except Exception:
                pass
            self._set_producer_identity_state("unverified", reason)
            logger.warning(
                "OrionFramePipeBackend: producer identity mismatch reason=%s server_pid=%d",
                reason, int(observed.pid or 0))
            return
        self._bound_producer_expectation = expected
        self._bound_producer_observation = observed
        self._set_producer_identity_state("verified", "")
        logger.info(
            "OrionFramePipeBackend: exact producer verified pid=%d launch_generation=%d",
            expected.pid, expected.launch_generation)

    def _producer_lease_reason_now(self) -> str:
        if self._producer_identity_state != "verified":
            return ""
        bound = self._bound_producer_expectation
        observed = self._bound_producer_observation
        current, reason = self._current_producer_expectation()
        if reason:
            return reason
        reason = bound_expectation_reason(bound, current)
        if reason:
            return reason
        try:
            return str(self._producer_identity_api.lease_reason(observed) or "")
        except Exception:
            return "server_process_lease_failed"

    def _accept_wire_mode(self, width: int, height: int, fmt: int) -> bool:
        """Record an ORF2 mode and advance source identity on a live transition."""
        if self._source_generation <= 0:
            # Compatibility for synthetic readers/tests that seed only the
            # historical connection generation. Real connects advance both.
            self._source_generation = max(1, int(self._connection_generation or 0))
        wire_mode = (int(width), int(height), int(fmt))
        changed = bool(
            self._last_wire_mode != (0, 0, -1)
            and wire_mode != self._last_wire_mode
        )
        if changed:
            self._source_generation += 1
        self._last_wire_mode = wire_mode
        return changed

    def start(self) -> bool:
        if os.name != "nt":
            logger.warning("OrionFramePipeBackend is Windows-only")
            return False
        try:
            import win32file  # noqa: F401  (presence check for pywin32)
        except Exception as exc:
            logger.warning("OrionFramePipeBackend needs pywin32 (%s)", exc)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._ring.clear()
        self._ready_evt.clear()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="OrionFramePipeReader", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        # Supervisor: keep (re)connecting until stopped. chiaki creates the export pipe only once its
        # stream session is up (seconds after launch) and may recreate it across reconnects, so we
        # retry rather than fail. Development may use GDI until the first frame arrives; a production
        # orchestrator marked ORION_REQUIRE_FRAME_PIPE waits without publishing a frame, so automation
        # stays fail-closed through startup, disconnects, and malformed-contract reconnects.
        while not self._stop_evt.is_set():
            if not self._connect_once():
                # The exporter blocks its session startup waiting for this reader.
                # A 250 ms retry period added visible startup/reconnect latency; an
                # interruptible 50 ms wait attaches promptly without a busy loop.
                self._stop_evt.wait(0.05)
                continue
            self._reader_loop()
            self._connected = False
            self._ready_evt.clear()
            if self._producer_identity_state == "verified":
                self._set_producer_identity_state("disconnected", "pipe_disconnected")
            # Never let a not-yet-consumed frame from a dead connection become the
            # first frame of the next session.  Freshness is worth more than continuity.
            self._ring.clear()
            self._close()

    def _connect_once(self) -> bool:
        # Reconnect never inherits readiness from a prior pipe generation.
        self._ready_evt.clear()
        try:
            import win32file
            import pywintypes
        except Exception:
            return False
        try:
            self._handle = win32file.CreateFile(
                self._pipe_name, win32file.GENERIC_READ, 0, None,
                win32file.OPEN_EXISTING, 0, None)
            self._connection_generation += 1
            self._source_generation += 1
            self._wire_seq_last = 0
            self._producer_generation = 0
            self._wire_pts_last = 0
            self._wire_producer_ts_last = 0
            self._last_wire_mode = (0, 0, -1)
            self._wire_consecutive_rejects = 0
            self._wire_reject_latch_logged = False
            self._export_count = 0
            self._wire_seq_gaps = 0
            self._export_fps_t0 = time.perf_counter()
            self._ring.clear()
            self._connected = True
            self._set_producer_identity_state("pending", "")
            self._bind_pipe_producer()
            logger.info("OrionFramePipeBackend connected to %s", self._pipe_name)
            return True
        except pywintypes.error:
            return False

    def _read_exact(self, n: int) -> Optional[bytes]:
        import win32file
        buf = bytearray()
        while len(buf) < n and not self._stop_evt.is_set():
            try:
                hr, data = win32file.ReadFile(self._handle, n - len(buf))
            except Exception:
                return None
            if not data:
                return None
            buf += data
        return bytes(buf) if len(buf) == n else None

    def _split_planes(self, payload: bytes, w: int, h: int, fmt: int):
        """(y, u, v) half-res chroma planes as arrays; y is a ZERO-COPY view of the payload."""
        buf = np.frombuffer(payload, dtype=np.uint8)
        y = buf[: w * h].reshape(h, w)
        if fmt == self._FMT_NV12:
            uv = buf[w * h:].reshape(h // 2, w)
            u = np.ascontiguousarray(uv[:, 0::2])
            v = np.ascontiguousarray(uv[:, 1::2])
        elif fmt == self._FMT_I420:
            q = w * h // 4
            u = buf[w * h: w * h + q].reshape(h // 2, w // 2)
            v = buf[w * h + q: w * h + 2 * q].reshape(h // 2, w // 2)
        else:
            return None
        return y, u, v

    def _to_bgr(self, payload: bytes, w: int, h: int, fmt: int) -> Optional[np.ndarray]:
        try:
            yuv = np.frombuffer(payload, dtype=np.uint8).reshape((h * 3 // 2, w))
            if not self._bt709:
                if fmt == self._FMT_NV12:
                    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                if fmt == self._FMT_I420:
                    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                return None
            planes = self._split_planes(payload, w, h, fmt)
            if planes is None:
                return None
            y, u, v = planes
            if not self._logged_matrix:
                self._logged_matrix = True
                logger.info("OrionFramePipeBackend: BT.709 YUV->BGR matrix active "
                            "(ORION_PIPE_BT709=0 reverts to the cv2 BT.601 path)")
            # Reuse the chroma-upsample and interleave scratch across frames. These
            # three 1080p buffers were reallocated per frame (~8 MB/frame, ~470 MB/s
            # at 60 fps) purely to be overwritten; recycling them measured
            # 6.91 ms -> 3.95 ms per frame with bit-identical output.
            #
            # The scratch is INTERMEDIATE ONLY. cv2.transform below still allocates
            # the returned frame fresh every call, because the ring keeps a
            # reference to it -- _reader_loop enforces that with its OWNDATA
            # isolation contract, and handing back a recycled buffer would let the
            # next frame overwrite one already queued for the detector.
            if self._cvt_geom != (w, h) or self._cvt_merged is None:
                self._cvt_u_full = np.empty((h, w), dtype=np.uint8)
                self._cvt_v_full = np.empty((h, w), dtype=np.uint8)
                self._cvt_merged = np.empty((h, w, 3), dtype=np.uint8)
                self._cvt_geom = (w, h)
            cv2.resize(u, (w, h), dst=self._cvt_u_full, interpolation=cv2.INTER_LINEAR)
            cv2.resize(v, (w, h), dst=self._cvt_v_full, interpolation=cv2.INTER_LINEAR)
            cv2.merge([y, self._cvt_u_full, self._cvt_v_full], dst=self._cvt_merged)
            # affine per-pixel with C++ saturation: dst = M[:, :3] @ [Y,U,V] + M[:, 3]
            return cv2.transform(self._cvt_merged, self._BT709_M)
        except Exception as exc:
            logger.warning("OrionFramePipeBackend convert error (%dx%d fmt=%d): %s", w, h, fmt, exc)
            return None

    @classmethod
    def _wire_header_reason(cls, w: int, h: int, fmt: int, payload_len: int) -> str:
        """Validate the self-describing 4:2:0 packet before reading its payload.

        Exact length + even geometry prevents a corrupt/malicious pipe peer from
        shifting frame boundaries or making OpenCV interpret mismatched planes.
        """
        try:
            w, h, fmt, payload_len = int(w), int(h), int(fmt), int(payload_len)
        except Exception:
            return "header_type"
        if w <= 0 or h <= 0 or w > cls._MAX_DIMENSION or h > cls._MAX_DIMENSION:
            return "geometry"
        if (w & 1) or (h & 1):
            return "chroma_geometry"
        if fmt not in (cls._FMT_NV12, cls._FMT_I420):
            return "format"
        expected = w * h * 3 // 2
        if payload_len != expected:
            return "payload_length"
        return ""

    @classmethod
    def _wire_contract_reason(cls, magic: int, version: int, header_size: int,
                              flags: int, generation: int, callback_seq: int,
                              producer_monotonic_ns: int, reserved: int) -> str:
        """Validate all fixed v2 envelope fields before trusting payload length."""
        if int(magic) != cls._MAGIC:
            return "magic"
        if int(version) != cls._VERSION:
            return "version"
        if int(header_size) != cls._HEADER.size:
            return "header_size"
        if int(flags) != 0:
            return "flags"
        if int(generation) <= 0:
            return "generation"
        if int(callback_seq) <= 0:
            return "sequence"
        if int(producer_monotonic_ns) <= 0:
            return "producer_timestamp"
        if int(reserved) != 0:
            return "reserved"
        return ""

    @classmethod
    def _producer_timestamp_reason(cls, producer_ns: int, arrival_ns: int) -> str:
        """Validate the QPC-domain producer age after the complete payload arrives."""
        age_ns = int(arrival_ns) - int(producer_ns)
        if age_ns < -cls._MAX_PRODUCER_FUTURE_NS:
            return "producer_timestamp_future"
        if age_ns > cls._MAX_PRODUCER_AGE_NS:
            return "producer_timestamp_stale"
        return ""

    def _note_wire_reject(self, reason: str, seq: int, generation: int) -> None:
        """Count every rejection but rate-limit log I/O on a degraded 60/120 Hz feed."""
        reason = str(reason or "unknown")
        count = self._wire_reject_counts.get(reason, 0) + 1
        self._wire_reject_counts[reason] = count
        now_s = time.perf_counter()
        last_s = self._wire_reject_last_log_s.get(reason, 0.0)
        # First occurrence is immediate. Sustained faults remain observable at most
        # every 5 s or every 120 frames, whichever comes first, without a 60 Hz log storm.
        if count == 1 or count % 120 == 0 or now_s - last_s >= 5.0:
            self._wire_reject_last_log_s[reason] = now_s
            logger.warning(
                "OrionFramePipeBackend: rejected wire frame reason=%s seq=%d generation=%d count=%d",
                reason, seq, generation, count)

    def _note_wire_accept(self) -> None:
        """A frame cleared every gate: the feed is live, so drop the latch state."""
        self._wire_consecutive_rejects = 0
        self._wire_reject_latch_logged = False

    def _wire_reject_latched(self, reason: str) -> bool:
        """Count a consecutive reject and break a 100%-reject latch.

        No freshness gate may reject every frame indefinitely -- that is a liveness
        bug regardless of which gate does it, and it is exactly how the PTS drift
        latch killed the feed silently for minutes.  After
        ``_WIRE_REJECT_REBASELINE_FRAMES`` consecutive rejects this re-seeds the
        MONOTONIC baselines and clears readiness.

        This never relaxes freshness.  Only ordering baselines are reset -- the
        state a single bogus sample (a far-future producer stamp, a PTS jump) can
        poison permanently so that every later frame is mis-classified.  True
        elapsed age is re-checked against the real QPC clock on every frame by
        _producer_timestamp_reason, and that budget is untouched, so a genuinely
        stale producer keeps being rejected.  Returns True when the escape fired.
        """
        self._wire_consecutive_rejects += 1
        if self._wire_consecutive_rejects < self._WIRE_REJECT_REBASELINE_FRAMES:
            return False
        self._wire_pts_last = 0
        self._wire_producer_ts_last = 0
        self._wire_consecutive_rejects = 0
        # A reader that accepts nothing is NOT ready.  Clearing readiness makes the
        # stall fail closed loudly (the transport watchdog sees it) instead of the
        # detector starving behind a stale "ready" flag.
        self._ready_evt.clear()
        if not self._wire_reject_latch_logged:
            self._wire_reject_latch_logged = True
            logger.error(
                "OrionFramePipeBackend: %d consecutive wire rejects (latest reason=%s); "
                "re-baselining monotonic state and clearing readiness. The freshness "
                "budget is unchanged -- genuinely stale frames are still rejected.",
                self._WIRE_REJECT_REBASELINE_FRAMES, reason)
        return True

    def _wire_pts_reason(self, pts: int) -> str:
        """Validate decoder PTS ordering only.  PTS must NEVER gate freshness.

        The exporter's PTS is a SYNTHETIC NOMINAL-RATE COUNTER, not a clock:
        chiaki seeds it at 1e6/max_fps and advances it by that fixed step per
        received frame (lib/src/ffmpegdecoder.c), and its re-estimator both clamps
        the observed duration to a floor of nominal and only adapts on a >=20%
        error -- so a sub-1% source mismatch is structurally uncorrectable.

        A real PS5 runs at 59.94, not 60.000, so ``arrival - pts`` drifts linearly.
        Measured on the live producer log: 0.910 ms/s (59.9454 fps implied), i.e.
        it crosses a 50 ms budget ~55 s into every session.  The old gate compared
        that difference against a floor computed with ``min()``, which can only
        ratchet DOWN and therefore never absorbs the drift: once crossed, EVERY
        frame was rejected until the pipe reconnected.  Live capture confirmed the
        latch -- 483 frames, 480 rejects, 1:1, feed dead (uniqfps 60 -> 0).

        Freshness is enforced by _producer_timestamp_reason instead, which
        compares two QueryPerformanceCounter samples of the SAME clock and so
        measures true elapsed time.  Genuinely stale frames are still rejected
        there; only the drift-driven false rejects are gone.
        """
        pts = int(pts or 0)
        if pts <= 0:
            return "pts_missing"
        if self._wire_pts_last > 0 and pts <= self._wire_pts_last:
            self._wire_pts_last = pts
            return "pts_regression"
        self._wire_pts_last = pts
        return ""

    def _reader_loop(self) -> None:
        while not self._stop_evt.is_set():
            lease_reason = self._producer_lease_reason_now()
            if lease_reason:
                # A previously verified pipe may not coast after its owned child
                # exits or a new manager launch generation replaces it.  A fresh
                # connection is re-inspected from scratch.
                self._set_producer_identity_state("unverified", lease_reason)
                logger.warning(
                    "OrionFramePipeBackend: producer lease revoked reason=%s",
                    lease_reason)
                break
            hdr = self._read_exact(self._HEADER.size)
            if hdr is None:
                break
            (magic, version, header_size, flags, generation, seq,
             producer_monotonic_ns, w, h, fmt, payload_len, pts,
             reserved) = self._HEADER.unpack(hdr)
            contract_reason = self._wire_contract_reason(
                magic, version, header_size, flags, generation, seq,
                producer_monotonic_ns, reserved)
            if contract_reason:
                logger.warning(
                    "OrionFramePipeBackend: invalid v2 envelope reason=%s; stopping",
                    contract_reason)
                break
            header_reason = self._wire_header_reason(w, h, fmt, payload_len)
            if header_reason:
                logger.warning(
                    "OrionFramePipeBackend: invalid header reason=%s geometry=%dx%d fmt=%d bytes=%d; stopping",
                    header_reason, w, h, fmt, payload_len)
                break
            if self._producer_generation == 0:
                self._producer_generation = generation
            elif generation != self._producer_generation:
                logger.warning(
                    "OrionFramePipeBackend: producer generation changed inside one pipe connection; stopping")
                break
            # A duplicate/regressing callback identity inside one generation is a
            # protocol fault. Process restarts are safe because their generation changes.
            if self._wire_seq_last and seq <= self._wire_seq_last:
                logger.warning(
                    "OrionFramePipeBackend: wire sequence regression last=%d current=%d; stopping",
                    self._wire_seq_last, seq)
                break
            if (self._wire_producer_ts_last
                    and producer_monotonic_ns <= self._wire_producer_ts_last):
                logger.warning(
                    "OrionFramePipeBackend: producer timestamp regression last=%d current=%d; stopping",
                    self._wire_producer_ts_last, producer_monotonic_ns)
                break
            self._wire_producer_ts_last = producer_monotonic_ns
            payload = self._read_exact(payload_len)
            if payload is None:
                break
            # Stamp as soon as the complete wire frame is available.  Conversion can
            # cost several ms at 1080p and must be INCLUDED in downstream frame age,
            # never hidden by stamping after cv2 returns.
            arrival_perf_ns = time.perf_counter_ns()
            arrival_epoch_ns = time.time_ns()
            self._last_frame_arrival_ns = arrival_perf_ns
            timestamp_reason = self._producer_timestamp_reason(
                producer_monotonic_ns, arrival_perf_ns)
            if timestamp_reason:
                self._note_wire_reject(timestamp_reason, seq, generation)
                self._wire_reject_latched(timestamp_reason)
                if self._wire_seq_last and seq > self._wire_seq_last + 1:
                    self._wire_seq_gaps += (seq - self._wire_seq_last - 1)
                self._wire_seq_last = seq
                continue
            pts_reason = self._wire_pts_reason(pts)
            if pts_reason:
                self._note_wire_reject(pts_reason, seq, generation)
                self._wire_reject_latched(pts_reason)
                # Progress the producer sequence even for a rejected frame so the
                # next sample cannot disguise it as a sequence discontinuity.
                if self._wire_seq_last and seq > self._wire_seq_last + 1:
                    self._wire_seq_gaps += (seq - self._wire_seq_last - 1)
                self._wire_seq_last = seq
                if pts_reason == "pts_regression":
                    break
                continue
            self._note_wire_accept()
            if self._accept_wire_mode(w, h, fmt):
                # Geometry/format is part of latency provenance.  Advancing the
                # source generation fences in-flight detection and forces a cold
                # route transition without dropping otherwise valid pixels.
                logger.warning(
                    "OrionFramePipeBackend: wire mode changed within connection; "
                    "timing generation advanced")
            if (w, h) != self._last_geom:
                logger.info("OrionFramePipeBackend frame geometry %dx%d (fmt=%d)", w, h, fmt)
                self._last_geom = (w, h)
            # Export-rate accounting: callback_seq was assigned before the producer's
            # FPS gate and latest-wins slot. Every jump is therefore an observable
            # producer callback that did not reach this reader.
            if self._wire_seq_last and seq > self._wire_seq_last + 1:
                self._wire_seq_gaps += (seq - self._wire_seq_last - 1)
            self._wire_seq_last = seq
            self._export_count += 1
            _now = time.perf_counter()
            _elapsed = _now - self._export_fps_t0
            if _elapsed >= 1.0:
                self._export_fps = self._export_count / _elapsed
                self._export_gap_fps = self._wire_seq_gaps / _elapsed
                self._export_count = 0
                self._wire_seq_gaps = 0
                self._export_fps_t0 = _now
            frame = self._to_bgr(payload, w, h, fmt)
            if frame is None:
                continue
            try:
                frame = np.ascontiguousarray(frame)
                if frame.dtype != np.uint8 or frame.shape != (h, w, 3) \
                        or not bool(frame.flags["OWNDATA"]):
                    logger.warning("OrionFramePipeBackend: conversion ownership/shape contract failed")
                    break
                frame.setflags(write=False)
            except Exception:
                logger.warning("OrionFramePipeBackend: conversion isolation contract failed")
                break
            self._frame_number = seq
            # y_plane: zero-copy view of this frame's payload bytes (immutable; safe to
            # hand downstream) -- the decoder's own luma for the compressed-path reader.
            try:
                y_view = np.frombuffer(payload, dtype=np.uint8)[: w * h].reshape(h, w)
            except Exception:
                y_view = None
            source_age_ns = max(0, arrival_perf_ns - producer_monotonic_ns)
            source_epoch_ns = arrival_epoch_ns - source_age_ns
            publication_perf_ns = time.perf_counter_ns()
            publication_epoch_ns = arrival_epoch_ns + (
                publication_perf_ns - arrival_perf_ns)
            self._ring.put(FrameData(frame=frame, timestamp_ns=producer_monotonic_ns,
                                     epoch_ns=source_epoch_ns,
                                     capture_timestamp_ns=producer_monotonic_ns,
                                     capture_epoch_ns=source_epoch_ns,
                                     publication_timestamp_ns=publication_perf_ns,
                                     publication_epoch_ns=publication_epoch_ns,
                                     frame_number=seq, pts=pts,
                                     decode_latency_ms=(publication_perf_ns
                                                        - producer_monotonic_ns) / 1e6,
                                     y_plane=y_view,
                                     payload=(payload if self._keep_payload else None),
                                     fmt=(fmt if self._keep_payload else -1),
                                     source_generation=self._source_generation,
                                     producer_generation=generation,
                                     producer_sequence=seq,
                                     producer_timestamp_ns=producer_monotonic_ns,
                                     arrival_timestamp_ns=arrival_perf_ns,
                                     producer_identity_verified=(
                                         self._producer_identity_state == "verified"),
                                     producer_process_id=int(
                                         self._bound_producer_expectation.pid or 0),
                                     producer_launch_generation=int(
                                         self._bound_producer_expectation.launch_generation or 0),
                                     decoder_width=int(w),
                                     decoder_height=int(h),
                                     decoder_format=int(fmt),
                                     integrity_isolated=True))
            # Readiness means a complete, fresh, valid v2 frame is already in the
            # ring -- never merely that the reader thread or named pipe exists.
            self._ready_evt.set()
        # Disconnected / stopped -> return to the _run supervisor, which closes + retries.
        return

    def export_stats(self) -> Tuple[float, float]:
        """Return ``(wire_fps, wire_gap_fps)`` for the current pipe connection.

        The v2 producer assigns callback sequence before every producer-side gate,
        so ``wire_gap_fps`` includes callback frames lost to FPS limiting, failed
        readback, and latest-wins overwrite as well as transport discontinuities.
        """
        return (self._export_fps, self._export_gap_fps)

    def _close(self) -> None:
        self._release_producer_lease()
        h, self._handle = self._handle, None
        if h is not None:
            try:
                import win32file
                win32file.CloseHandle(h)
            except Exception:
                pass

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        return self._ring.get_latest_nonblocking()

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        return self._ring.get_latest(timeout)

    def is_ready(self) -> bool:
        return self._connected and self._ready_evt.is_set()

    def wait_until_ready(self, timeout_s: float) -> bool:
        if not self._ready_evt.wait(max(0.0, float(timeout_s))):
            return False
        # A disconnect racing the waiter cannot preserve readiness from the last
        # accepted frame of the dead pipe generation.
        return self._connected and self._ready_evt.is_set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._ready_evt.clear()
        # Synchronous ReadFile can otherwise outlive the join timeout.  Closing the
        # handle first wakes it; then wait for the supervisor to exit before return.
        self._connected = False
        self._set_producer_identity_state("disconnected", "stopped")
        self._close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._ring.clear()
        logger.info("OrionFramePipeBackend stopped")


# ---------------------------------------------------------------------------
#  Config loader
# ---------------------------------------------------------------------------

def load_chiaki_config(settings_path: Optional[str] = None) -> ChiakiConfig:
    """Load ChiakiConfig from settings.json."""
    cfg = ChiakiConfig()
    if settings_path is None:
        base = os.path.dirname(os.path.abspath(__file__))
        desk = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop", "NexusVision")
        for d in (base, desk):
            p = os.path.join(d, "settings.json")
            if os.path.isfile(p):
                settings_path = p
                break
    if not settings_path or not os.path.isfile(settings_path):
        return cfg

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return cfg

    rp = raw.get("remote_play", {})
    if not isinstance(rp, dict):
        return cfg

    cfg.console_ip = str(rp.get("console_ip", cfg.console_ip) or "")
    cfg.console_type = str(rp.get("console_type", cfg.console_type) or "ps5")
    cfg.resolution = str(rp.get("resolution", cfg.resolution) or "1080p")
    cfg.fps = max(30, min(120, int(rp.get("fps", cfg.fps) or 60)))
    cfg.bitrate_kbps = max(5000, min(100000, int(rp.get("bitrate_kbps", cfg.bitrate_kbps) or 30000)))
    cfg.codec = str(rp.get("codec", cfg.codec) or "h265")
    cfg.hw_decoder = str(rp.get("hw_decoder", cfg.hw_decoder) or "auto")
    cfg.chiaki_path = str(rp.get("chiaki_path", cfg.chiaki_path) or "")
    cfg.adaptive_bitrate = bool(rp.get("adaptive_bitrate", cfg.adaptive_bitrate))
    cfg.disable_nagle = bool(rp.get("disable_nagle", cfg.disable_nagle))
    cfg.force_iframe_interval = max(1, min(60, int(rp.get("force_iframe_interval", cfg.force_iframe_interval) or 8)))

    # Sensitive credentials: prefer DPAPI vault, fallback to settings.json
    try:
        from nexus_crypto import get_vault
        vault = get_vault()
        psn_bytes = vault.retrieve("psn_account_id", context="tokens")
        if psn_bytes:
            cfg.psn_account_id = psn_bytes.decode("utf-8")
        else:
            cfg.psn_account_id = str(rp.get("psn_account_id", cfg.psn_account_id) or "")
        rk_bytes = vault.retrieve("rp_regist_key", context="tokens")
        if rk_bytes:
            cfg.rp_regist_key = rk_bytes.decode("utf-8")
        else:
            cfg.rp_regist_key = str(rp.get("regist_key", cfg.rp_regist_key) or "")
    except Exception:
        # Vault unavailable, fall back to plaintext settings
        cfg.psn_account_id = str(rp.get("psn_account_id", cfg.psn_account_id) or "")
        cfg.rp_regist_key = str(rp.get("regist_key", cfg.rp_regist_key) or "")

    return cfg


def store_psn_credentials(psn_account_id: str, regist_key: str) -> bool:
    """Store PSN credentials in the DPAPI-protected vault.

    Call this once during setup. After storing, the plaintext values
    in settings.json can be removed.
    """
    try:
        from nexus_crypto import get_vault
        vault = get_vault()
        ok1 = vault.store("psn_account_id", psn_account_id.encode("utf-8"), context="tokens")
        ok2 = vault.store("rp_regist_key", regist_key.encode("utf-8"), context="tokens")
        return ok1 and ok2
    except Exception:
        return False
