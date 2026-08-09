"""Black-screen capture regression coverage for RemotePlayOrchestrator.

Guards the Jun-1 regression where the GDI-only window capture returned an
all-black bitmap for Chiaki's GPU-rendered surface. The tiered _capture_window
must detect black and fall through GDI -> PrintWindow -> screen-region until it
finds a non-black frame.
"""
import numpy as np
import pytest

from remote_play_orchestrator import RemotePlayOrchestrator as O


def _black(h=120, w=160):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _noisy(h=120, w=160):
    rng = np.random.default_rng(0)
    return (rng.random((h, w, 3)) * 255).astype(np.uint8)


def test_frame_is_probably_black_detects_zero_frame():
    assert O._frame_is_probably_black(_black()) is True
    assert O._frame_is_probably_black(None) is True
    # A near-black but slightly noisy frame is still treated as content-free.
    assert O._frame_is_probably_black(np.full((80, 80, 3), 1, dtype=np.uint8)) is True


def test_frame_is_probably_black_passes_real_content():
    assert O._frame_is_probably_black(_noisy()) is False


def test_frame_is_probably_black_rejects_partial_black():
    # A partial-black grab (top ~60% black, game content only at the bottom) is a
    # corrupt/occluded capture and must be rejected so the tier chain falls through.
    frame = _noisy()
    h = frame.shape[0]
    frame[: int(h * 0.6), :, :] = 0
    assert O._frame_is_probably_black(frame) is True


def test_frame_is_probably_black_keeps_mostly_content_frame():
    # A small black band (e.g. letterbox ~20%) over mostly-live content is still good.
    frame = _noisy()
    h = frame.shape[0]
    frame[: int(h * 0.2), :, :] = 0
    assert O._frame_is_probably_black(frame) is False


def test_capture_window_falls_through_to_printwindow(monkeypatch):
    """GDI returns black -> PrintWindow returns content -> that frame is used."""
    pw = _noisy()
    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: _black()))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(lambda hwnd: pw))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(lambda hwnd: _black()))
    out = O._capture_window(1234)
    assert out is pw


def test_capture_window_falls_through_to_screen_region(monkeypatch):
    """GDI + PrintWindow black -> desktop CAPTUREBLT region is used."""
    region = _noisy()
    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: _black()))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(lambda hwnd: None))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(lambda hwnd: region))
    out = O._capture_window(1234)
    assert out is region


def test_capture_window_prefers_gdi_when_not_black(monkeypatch):
    """Cheapest tier wins when it already yields content (no needless fallback)."""
    gdi = _noisy()
    called = {"pw": False, "region": False}

    def _pw(hwnd):
        called["pw"] = True
        return _black()

    def _region(hwnd):
        called["region"] = True
        return _black()

    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: gdi))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(_pw))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(_region))
    out = O._capture_window(1234)
    assert out is gdi
    assert called == {"pw": False, "region": False}


def test_capture_window_returns_last_frame_when_all_black(monkeypatch):
    """If every tier is black, still return a sized frame (not None) so the
    pipeline keeps running rather than stalling."""
    region = _black()
    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: _black()))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(lambda hwnd: None))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(lambda hwnd: region))
    out = O._capture_window(1234)
    assert out is region


def test_capture_window_tier_attributes_printwindow(monkeypatch):
    """_capture_window_tier reports WHICH tier produced the frame; the wrapper still
    returns just the frame (back-compat)."""
    pw = _noisy()
    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: _black()))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(lambda hwnd: pw))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(lambda hwnd: _black()))
    out, tier = O._capture_window_tier(1234)
    assert out is pw
    assert tier == "printwindow"
    assert O._capture_window(1234) is pw


def test_capture_window_tier_blackfallback(monkeypatch):
    """All tiers black -> still return a sized frame, tier name marked *_blackfallback."""
    region = _black()
    monkeypatch.setattr(O, "_capture_window_gdi", staticmethod(lambda hwnd: None))
    monkeypatch.setattr(O, "_capture_window_printwindow", staticmethod(lambda hwnd: None))
    monkeypatch.setattr(O, "_capture_window_screen_region", staticmethod(lambda hwnd: region))
    out, tier = O._capture_window_tier(1234)
    assert out is region
    assert tier.endswith("_blackfallback")


def test_video_region_status_flags_black_core():
    """A black VIDEO CORE is reported black regardless of the edges."""
    frame = _noisy()
    h, w = frame.shape[:2]
    frame[int(h * 0.12):int(h * 0.88), int(w * 0.12):int(w * 0.88), :] = 0
    core_black, _ = O._video_region_status(frame)
    assert core_black is True


def test_video_region_status_none_frame():
    core_black, core_hash = O._video_region_status(None)
    assert core_black is True
    assert core_hash is None


def test_video_region_status_hash_is_core_only():
    """The core hash ignores edge/chrome changes, so 'core static while the whole
    frame changes' is detectable (the meterless-feed signature)."""
    base = _noisy()
    h, w = base.shape[:2]
    a = base.copy()
    b = base.copy()
    b[: int(h * 0.10), :, :] = 255  # change only the top edge (outside the 12-88% core)
    _, ha = O._video_region_status(a)
    _, hb = O._video_region_status(b)
    assert ha == hb
    # A change INSIDE the core does alter the hash.
    c = base.copy()
    c[int(h * 0.45):int(h * 0.55), int(w * 0.45):int(w * 0.55), :] = 255
    _, hc = O._video_region_status(c)
    assert hc != ha


def test_frame_keeps_pipeline_alive_backend_black_is_alive():
    """A BLACK frame delivered by an active backend (capture card / decoder / WGC) still proves the
    pipeline is alive — it must refresh the freshness clock so a transient black run (loading
    screen / fade / PS5-network blip) can't climb frame-age into the native frame-stall watchdog."""
    black = _black()
    assert O._frame_keeps_pipeline_alive(black, from_backend=True, is_black=True) is True


def test_frame_keeps_pipeline_alive_window_black_is_dead():
    """A black WINDOW-capture grab (no backend) means the BitBlt itself failed — it must NOT refresh
    the clock, so the climbing age honestly reflects capture being down."""
    black = _black()
    assert O._frame_keeps_pipeline_alive(black, from_backend=False, is_black=True) is False


def test_frame_keeps_pipeline_alive_content_always_refreshes():
    """A non-black frame always refreshes the clock, backend or window."""
    frame = _noisy()
    assert O._frame_keeps_pipeline_alive(frame, from_backend=True, is_black=False) is True
    assert O._frame_keeps_pipeline_alive(frame, from_backend=False, is_black=False) is True


def test_frame_keeps_pipeline_alive_none_is_dead():
    """A missing frame (backend delivered nothing / grab returned None) never refreshes the clock —
    that path escalates to the backend's own is_healthy() in-process re-open."""
    assert O._frame_keeps_pipeline_alive(None, from_backend=True, is_black=True) is False
    assert O._frame_keeps_pipeline_alive(None, from_backend=False, is_black=True) is False


class _BlackCardBackend:
    """Minimal capture-card-shaped backend that delivers a small BLACK frame every read
    (the Elgato-emits-black-on-signal-loss / loading-screen case)."""

    def __init__(self):
        import numpy as _np
        self._frame = _np.zeros((120, 160, 3), dtype=_np.uint8)
        self._n = 0

    def get_frame(self, timeout=0.02):
        from chiaki_backend import FrameData
        import time as _t
        self._n += 1
        return FrameData(
            frame=self._frame,
            timestamp_ns=_t.perf_counter_ns(),
            frame_number=self._n,
            capture_api="DSHOW",
            capture_device_index=0,
            capture_width=1280,
            capture_height=720,
            capture_fps=60.0,
            capture_fourcc="MJPG",
            capture_buffer_size=1.0,
        )

    def get_frame_nonblocking(self):
        return self.get_frame()

    def is_healthy(self):
        return True


def test_capture_loop_black_card_frames_keep_clock_fresh():
    """End-to-end guard for the "capture dies + respawns after a few shots" bug: while a capture-card
    backend delivers BLACK frames, _capture_loop must keep advancing _last_capture_ts (so the native
    8s frame-stall watchdog never fires) WITHOUT counting any of them as unique frames."""
    import threading
    import time
    from types import SimpleNamespace

    o = O.__new__(O)
    o._running = True
    o._frame_backend = _BlackCardBackend()
    o._frame_backend_mode = "capture_card"
    o._cc_mode = True
    o._cc_last_retry = 0.0
    o._video_callback = None
    o._window_handle = None
    o._black_run = 0
    o._capture_bad_run = 0
    o._last_capture_ts = 0.0
    o._last_frame_hash = None
    o._last_frame = None
    o._last_frame_ts = 0.0
    o._last_y_plane = None
    o._last_frame_pts = 0
    o._last_frame_epoch_ms = 0.0
    o._frame_seq = 0
    o._frame_count = 0
    o._capture_count = 0
    o._unique_count = 0
    o._capture_fps = 0
    o._unique_frame_fps = 0
    o._fps = 0
    o._duplicate_frame_pct = 0.0
    o._last_fps_time = time.perf_counter()
    o._last_capture_health_log = time.perf_counter()
    o._last_capture_tier = ""
    o._tier_counts = {}
    o._video_core_black_run = 0
    o._video_core_static_run = 0
    o._video_core_hash = None
    o._preview_duplicate_refreshes = 0
    o._backend_identity_ref = None
    o._backend_identity_source_generation = 0
    o._backend_identity_generation = 0
    o._source_identity = 0
    o._source_frame_number = 0
    o._source_timestamp_ns = 0
    o._last_forwarded_frame_number = -1
    o._detector_min_width = 1280
    o._detector_min_height = 720
    o._detector_aspect_tolerance = 0.035
    o._capture_warm_cache_expected_index = 0
    o._capture_warm_cache_revoked = False
    o.config = SimpleNamespace(
        target_fps=60,
        capture_mode="1280x720@60.000|fourcc=mjpg|buffer=1.000",
    )
    # The integrity fail-closed path is part of the capture-loop contract now.  This
    # lightweight __new__ fixture bypasses RemotePlayOrchestrator.__init__, so mirror
    # the minimum synchronization/publication state that production construction owns.
    o._frame_integrity_counts = {}
    o._frame_integrity_lock = threading.Lock()
    o._frame_integrity_generation = 0
    o._telemetry_revision = 0
    o._frame_ready_evt = threading.Event()

    th = threading.Thread(target=o._capture_loop, daemon=True)
    th.start()
    try:
        time.sleep(0.15)
        ts_after = o._last_capture_ts
    finally:
        o._running = False
        th.join(timeout=2.0)

    assert ts_after > 0.0, "black backend frames must advance the freshness clock (pipeline is alive)"
    # Black frames carry no meter, so none of them may register as a unique/detectable frame.
    assert o._frame_seq == 0, "black frames must not count as unique frames"
    # The reader was reported healthy and delivering, so the loop never tore the backend down.
    assert o._frame_backend is not None, "a delivering backend must not be detached"


def _overlay_obj(show_required=2, hide_required=12):
    """Build a bare orchestrator with only the hysteresis fields, avoiding the
    full __init__ (which needs ViGEm/CV/RTT hardware)."""
    o = O.__new__(O)
    o._green_show_streak = 0
    o._green_hide_streak = 0
    o._green_show_required = show_required
    o._green_hide_required = hide_required
    o._last_green_window = None
    return o


def test_green_overlay_show_hysteresis_rejects_single_frame():
    """A 1-frame green detection must NOT show the overlay; it takes
    show_required consecutive frames (rejects transient false positives)."""
    o = _overlay_obj(show_required=2)
    gw = {"start": 90.0, "end": 100.0, "center": 95.0, "width": 10.0, "green_px": 1}
    o._update_green_overlay(gw)
    assert o._last_green_window is None, "one frame must not show the overlay"
    o._update_green_overlay(gw)
    assert o._last_green_window == gw, "second consecutive frame shows it"


# --- PTS->clock offset re-seed (live-path bughunt #3) -----------------------------------


def test_pts_offset_seeds_then_ema_tracks_drift():
    from remote_play_orchestrator import _pts_offset_update, _PTS_JUMP_S

    off, reseeded = _pts_offset_update(0.0, 1000.0, _PTS_JUMP_S)
    assert off == 1000.0 and reseeded is False          # first sample seeds (legacy contract)
    off2, reseeded = _pts_offset_update(off, 1000.010, _PTS_JUMP_S)
    assert reseeded is False
    assert abs(off2 - (1000.0 * 0.9 + 1000.010 * 0.1)) < 1e-9   # legacy slow EMA unchanged


def test_pts_offset_reseeds_only_on_explicit_source_reset():
    """A queue-delay spike must not impersonate a reconnect and map stale PTS to now."""
    from remote_play_orchestrator import _pts_offset_update, _PTS_JUMP_S, _PTS_JUMP_MS

    # pts->wall (seconds domain): session offset 1000.0, reconnect makes the sample 1500.0
    off, reseeded = _pts_offset_update(1000.0, 1500.0, _PTS_JUMP_S)
    assert reseeded is False
    assert off == 1000.0, "unexpected positive offset jump is backlog, not a reconnect"

    reset_off, reseeded = _pts_offset_update(
        1000.0, 1500.0, _PTS_JUMP_S, source_reset=True)
    assert reseeded is True and reset_off == 1500.0

    # pts->epoch (ms domain): same one-step snap
    off_ms, reseeded = _pts_offset_update(
        2_000_000.0, 2_500_000.0, _PTS_JUMP_MS, source_reset=True)
    assert reseeded is True and off_ms == 2_500_000.0

    # just under the threshold still EMA-tracks (normal drift is never treated as a jump)
    off3, reseeded = _pts_offset_update(1000.0, 1000.0 + _PTS_JUMP_S * 0.9, _PTS_JUMP_S)
    assert reseeded is False
    assert abs(off3 - (1000.0 * 0.9 + (1000.0 + _PTS_JUMP_S * 0.9) * 0.1)) < 1e-9


# --- shared stdout JSONL lock (live-path bughunt #4) -------------------------------------


class _LockCheckedStdout:
    """Stdout stub that records every write and whether the shared IPC lock was held."""

    def __init__(self, lock):
        self._lock = lock
        self.chunks = []
        self.unlocked_writes = 0

    def write(self, s):
        if not self._lock.locked():
            self.unlocked_writes += 1
        self.chunks.append(s)
        return len(s)

    def flush(self):
        if not self._lock.locked():
            self.unlocked_writes += 1


def _patched_stdout(monkeypatch):
    import sys
    import remote_play_orchestrator as rpo
    fake = _LockCheckedStdout(rpo.STDOUT_EMIT_LOCK)
    monkeypatch.setattr(sys, "stdout", fake)
    return fake


def test_pose_landmark_emit_holds_shared_stdout_lock(monkeypatch):
    """pose_landmark is the no-meter RELEASE trigger — its stdout line must be written under
    the shared IPC lock or it can interleave with the sidecar's ~130KB preview line and both
    get silently dropped by the native JSON parse."""
    import json
    from types import SimpleNamespace

    fake = _patched_stdout(monkeypatch)
    o = O.__new__(O)
    o._active_pose_arm_token = 73
    lm = SimpleNamespace(kind="release", frame_seq=42, subframe_seq=41.5,
                         confidence=0.91, arm_token=73)
    o._on_pose_landmark(lm)
    assert fake.unlocked_writes == 0, "pose_landmark wrote stdout without the shared lock"
    line = "".join(fake.chunks)
    obj = json.loads(line)
    assert obj["event"] == "pose_landmark" and obj["frame_seq"] == 42
    assert obj["arm_token"] == "73"


def test_pose_landmark_from_previous_arm_epoch_is_not_relabelled(monkeypatch):
    """A detector callback finishing after the next physical arm keeps its old token and is dropped."""
    from types import SimpleNamespace

    fake = _patched_stdout(monkeypatch)
    o = O.__new__(O)
    o._active_pose_arm_token = 102
    delayed = SimpleNamespace(kind="release", frame_seq=99, subframe_seq=98.4,
                              confidence=0.95, arm_token=101)
    o._on_pose_landmark(delayed)
    assert fake.chunks == []
    assert fake.unlocked_writes == 0


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.25, "", "0", "01", "1.0", "x"])
def test_pose_arm_token_parser_rejects_noncanonical_values(value):
    from remote_play_orchestrator import _parse_pose_arm_token

    assert _parse_pose_arm_token(value) == 0


def test_pose_arm_token_parser_preserves_uint64_identity():
    from remote_play_orchestrator import _parse_pose_arm_token

    assert _parse_pose_arm_token("18446744073709551615") == 0xFFFFFFFFFFFFFFFF
    assert _parse_pose_arm_token("18446744073709551616") == 0


def test_pose_overlay_emit_holds_shared_stdout_lock(monkeypatch):
    import json
    from types import SimpleNamespace

    fake = _patched_stdout(monkeypatch)
    o = O.__new__(O)
    o._pose_timing = SimpleNamespace(last_overlay={"kpts": [[1, 2, 0.9]], "box": [1, 2, 3, 4]})
    o._overlay_was_active = False
    o._emit_pose_overlay()
    assert fake.unlocked_writes == 0, "pose_overlay wrote stdout without the shared lock"
    obj = json.loads("".join(fake.chunks))
    assert obj["event"] == "pose_overlay" and obj["box"] == [1, 2, 3, 4]


def test_calibrate_meter_status_emit_holds_shared_stdout_lock(monkeypatch):
    import json
    from types import SimpleNamespace

    fake = _patched_stdout(monkeypatch)
    o = O.__new__(O)
    o._meter_detector = SimpleNamespace(
        calibration_status=lambda: {"version": 3, "shots_done": 2, "shots_needed": 5,
                                    "state": "collect", "learned_date": "", "calibrating": True})
    o._last_cal_status_version = -1
    o._emit_calibration_status(force=True)
    assert fake.unlocked_writes == 0, "calibrate_meter_status wrote stdout without the shared lock"
    obj = json.loads("".join(fake.chunks))
    assert obj["event"] == "calibrate_meter_status" and obj["shots_done"] == 2


def test_sidecar_adopts_orchestrator_stdout_lock():
    """autogreen_sidecar._emit and the orchestrator's emitters must serialize on the SAME
    lock object, otherwise the per-module locks don't actually exclude each other."""
    import importlib.util
    import os
    import sys

    import remote_play_orchestrator as rpo

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "native_orion", "backend", "autogreen_sidecar.py")
    spec = importlib.util.spec_from_file_location("_ag_sidecar_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        assert mod._emit_lock is not rpo.STDOUT_EMIT_LOCK  # separate until adoption
        mod._adopt_shared_stdout_lock()
        assert mod._emit_lock is rpo.STDOUT_EMIT_LOCK, "sidecar must adopt the shared lock"
    finally:
        sys.modules.pop(spec.name, None)


def test_green_overlay_hide_hysteresis_holds_through_dropouts():
    """Once shown, a brief detection dropout must NOT immediately clear the
    overlay; it must be absent for hide_required frames before clearing."""
    o = _overlay_obj(show_required=1, hide_required=3)
    gw = {"start": 90.0, "end": 100.0, "center": 95.0, "width": 10.0, "green_px": 1}
    o._update_green_overlay(gw)
    assert o._last_green_window == gw
    # Two absent frames (< hide_required) keep the overlay up (no flicker).
    o._update_green_overlay(None)
    o._update_green_overlay(None)
    assert o._last_green_window == gw, "brief dropout must not flicker the overlay off"
    # The third absent frame finally clears it.
    o._update_green_overlay(None)
    assert o._last_green_window is None
