"""[P-C 2026-09-23] Capture identity: RT-HIGH-06 / RT-MED-05 (CL3-F5-002/003/006/007/008).

Every test here is hermetic: no real capture device is opened and no real DirectShow
enumeration runs (``ccb._enumerate_video_inventory`` and ``CaptureCardBackend._open`` are
always replaced).

  F5-008  inventory refreshed on every reopen; opened device must carry the SELECTED ID
  F5-007  a busy explicit pick never falls through to another device; narrow card hints
  F5-006  timing authority only at the validated 60 fps; 30 Hz is preview-only
  F5-002  the saved stable ID finds the card again after a between-session reorder
  F5-003  the "duplicated" alarm must persist before it benches or warns
"""

import os
from types import SimpleNamespace

import pytest

import capture_card_backend as ccb
import remote_play_orchestrator as rpo
from capture_card_backend import CaptureCardBackend, capture_notice_text, qualify_capture_feed

_REAL_BOUNDED_ENUM = getattr(ccb, "_enumerate_video_inventory", None)
PFX = "dshow-moniker-sha256-v1:"
A = PFX + "a" * 64
B = PFX + "b" * 64
C = PFX + "c" * 64


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(ccb, "_INDEX_CACHE", str(tmp_path / "no_cache.json"))
    monkeypatch.setattr(ccb, "_BRUTE_SCANNED", True)
    monkeypatch.setattr(ccb, "_save_cached_index", lambda *a, **k: None)
    monkeypatch.setattr(ccb, "_enumerate_video_inventory",
                        lambda: pytest.fail("unexpected live DirectShow enumeration"),
                        raising=False)
    # Sentinels so monkeypatch restores whatever a refresh writes into os.environ.
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", "")
    monkeypatch.delenv("ORION_CAPTURE_SELECTED_ID", raising=False)
    monkeypatch.delenv("ORION_CAPTURE_LEGACY_SCAN", raising=False)
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    pytest.importorskip("cv2")


def _inventory(monkeypatch, names, ids, selected=None):
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "|".join(names))
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", "|".join(ids))
    if selected:
        monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", selected)


def _fake_open(backend, busy=(), opened=None):
    def fake_open(index=None):
        if opened is not None:
            opened.append(index)
        if index in busy:
            backend._open_diag = "busy"
            return None, ""
        return object(), "DSHOW"
    return fake_open


def _no_reader(monkeypatch):
    class _Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(ccb.threading, "Thread", _Thread)


# ---- CL3-F5-008: fresh inventory on every reopen ------------------------------------------

def test_reopen_uses_a_fresh_inventory_not_the_launch_snapshot(monkeypatch):
    """F5 fixture: launch saw USB Video (selected) at 0; mid-session only a webcam is left at 0."""
    _inventory(monkeypatch, ["USB Video", "Integrated Webcam"], [A, B], selected=A)
    monkeypatch.setattr(ccb, "_enumerate_video_inventory",
                        lambda: (["Integrated Webcam"], [B]))
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0, refresh_inventory=True)
    opened = []
    monkeypatch.setattr(backend, "_open", _fake_open(backend, opened=opened))
    assert backend.start() is False
    assert opened == []                       # the webcam now at index 0 is never opened
    code, dev, _ = backend.last_start_failure()
    assert code == "absent" and dev == ""     # never names another device as "your card"
    assert backend.identity_verified() is False
    # Every other reader in the process (route scope, notices) sees the fresh inventory.
    assert os.environ["ORION_VIDEO_DEVICE_NAMES"] == "Integrated Webcam"
    assert os.environ["ORION_VIDEO_DEVICE_IDS"] == B


def test_reopen_refinds_the_selected_card_by_id_after_replug(monkeypatch):
    _inventory(monkeypatch, ["USB Video", "Integrated Webcam"], [A, B], selected=A)
    monkeypatch.setattr(ccb, "_enumerate_video_inventory",
                        lambda: (["Integrated Webcam", "USB Video"], [B, A]))
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0, refresh_inventory=True)
    opened = []
    monkeypatch.setattr(backend, "_open", _fake_open(backend, opened=opened))
    assert backend.start() is True
    assert opened == [1]
    assert backend.active_route() == ("DSHOW", 1)
    assert backend.route_identity_basis() == "configured"
    assert backend.identity_verified() is True


def test_reopen_with_failed_enumeration_fails_closed(monkeypatch):
    _inventory(monkeypatch, ["USB Video"], [A], selected=A)
    monkeypatch.setattr(ccb, "_enumerate_video_inventory", lambda: None)
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0, refresh_inventory=True)
    monkeypatch.setattr(backend, "_open", _fake_open(backend))
    backend.start()                                   # preview may come up ...
    assert backend.identity_verified() is False       # ... but never with fire authority
    assert os.environ.get("ORION_VIDEO_DEVICE_IDS", "") == ""


def test_device_swapped_during_open_is_denied_authority(monkeypatch):
    """Enumerate -> open -> re-enumerate: the opened handle must still sit on the selected ID."""
    _inventory(monkeypatch, ["USB Video"], [A], selected=A)
    snapshots = iter([(["USB Video"], [A]), (["Integrated Webcam"], [B])])
    monkeypatch.setattr(ccb, "_enumerate_video_inventory", lambda: next(snapshots))
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0, refresh_inventory=True)
    monkeypatch.setattr(backend, "_open", _fake_open(backend))
    assert backend.start() is True
    assert backend.identity_verified() is False
    assert backend.identity_failure() == "wrong_device"


def test_first_open_keeps_using_the_native_launch_inventory(monkeypatch):
    """The launch inventory was enumerated by native moments before spawn: no extra COM call."""
    _inventory(monkeypatch, ["USB Video"], [A], selected=A)
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0)          # refresh_inventory defaults False
    monkeypatch.setattr(backend, "_open", _fake_open(backend))
    assert backend.start() is True
    assert backend.identity_verified() is True


def test_orchestrator_requests_a_fresh_inventory_on_every_reopen(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    _inventory(monkeypatch, ["USB Video"], [A], selected=A)
    made = []

    class _FakeBackend:
        def __init__(self, device_index=0, fps=60, use_mjpg=True, refresh_inventory=False):
            made.append(bool(refresh_inventory))

        def start(self):
            return True

        def route_identity_basis(self):
            return "configured"

        def active_route(self):
            return ("DSHOW", 0)

        def stop(self):
            pass

    monkeypatch.setattr(ccb, "CaptureCardBackend", _FakeBackend)
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False, virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        controller_route="pipe", capture_mode=""))
    monkeypatch.setattr(orch, "_guard_capture_latency_route", lambda **k: True)
    assert orch._start_frame_backend() is True
    orch._frame_backend = None                          # reader died -> in-process reopen
    assert orch._start_frame_backend() is True
    assert made == [False, True]


def test_selected_pick_never_verifies_a_different_card_named_device(monkeypatch):
    """Codex repro 1: selected A, a card-named B opened -> identity_verified must be False."""
    _inventory(monkeypatch, ["USB Video", "Game Capture HD60 X"], [A, B], selected=A)
    backend = CaptureCardBackend(device_index=1)
    backend._route_basis = "card_name"
    assert backend.identity_verified() is False
    assert backend.identity_failure() == "wrong_device"


def test_wrong_device_notice_is_plain_and_published(monkeypatch):
    text = capture_notice_text("wrong_device", device="Integrated Webcam")
    assert text.startswith("Capture: ") and text.isascii() and "=" not in text
    assert "not the capture card you picked" in text and "Stream Setup" in text
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    _inventory(monkeypatch, ["Integrated Webcam"], [B], selected=A)
    orch = _cc_orch()
    orch._frame_backend = SimpleNamespace(
        cadence_stats=lambda window_s=None: {"samples": 120, "fps": 60.0, "gap_p95_ms": 16.7},
        active_route=lambda: ("DSHOW", 0), identity_verified=lambda: False,
        identity_failure=lambda: "wrong_device")
    orch._frame_backend_mode = "capture_card"
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_feed_reason == "wrong_device"
    assert orch._capture_notice[1] == "wrong_device"
    assert "Integrated Webcam" in orch._capture_notice[2]


def test_python_stable_id_matches_the_native_scheme():
    """Twin of native stableVideoInputDeviceId: domain-separated sha256, trim + casefold."""
    import hashlib
    raw = r"@device:pnp:\\?\usb#vid_0fd9&pid_0080#SERIAL-DO-NOT-EXPORT#{camera-guid}\global"
    want = PFX + hashlib.sha256(b"orion-dshow-moniker-v1\0" + raw.lower().encode()).hexdigest()
    assert ccb.stable_video_device_id(raw) == want
    assert ccb.stable_video_device_id("  %s  " % raw.upper()) == want
    assert "serial" not in want.lower()
    assert ccb.stable_video_device_id("") == ""


def test_live_enumeration_is_bounded_and_never_stacks(monkeypatch):
    import threading
    release = threading.Event()
    calls = []

    def wedged():
        calls.append(1)
        release.wait(5.0)
        return (["X"], [A])

    monkeypatch.setattr(ccb, "_enumerate_dshow_inventory_com", wedged)
    monkeypatch.setattr(ccb, "_INVENTORY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(ccb, "_INVENTORY_WORKER", None)
    try:
        assert _REAL_BOUNDED_ENUM() is None          # timed out -> fail closed
        assert _REAL_BOUNDED_ENUM() is None          # wedged walk still running -> no 2nd
        assert calls == [1]
    finally:
        release.set()


# ---- CL3-F5-007: busy explicit pick never falls through; narrow hints ---------------------

def test_busy_explicit_pick_never_falls_through_to_a_card_named_device(monkeypatch):
    _inventory(monkeypatch, ["USB Video", "Game Capture HD60 X"], [A, B], selected=A)
    backend = CaptureCardBackend(device_index=0)
    opened = []
    monkeypatch.setattr(backend, "_open", _fake_open(backend, busy={0}, opened=opened))
    assert backend.start() is False
    assert opened == [0]
    code, dev, _ = backend.last_start_failure()
    assert code == "busy" and dev == "USB Video"


def test_explicit_pick_candidates_are_the_selected_id_only(monkeypatch):
    _inventory(monkeypatch, ["USB Video", "Game Capture HD60 X", "Elgato Cam Link 4K"],
               [A, B, C], selected=A)
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: 1)
    assert CaptureCardBackend(device_index=0)._candidate_indices() == [0]


@pytest.mark.parametrize("name", [
    "screen-capture-recorder", "Screen Capture Recorder", "AVerMedia Live Streamer CAM 313",
    "AVerMedia PW313", "AVerMedia PW513 4K", "Unity Video Capture", "XSplit VCam",
    "ManyCam Virtual Webcam", "Desktop Capture",
])
def test_screen_filters_and_vendor_webcams_are_never_cards(name):
    assert ccb._classify_name(name) != "card"


@pytest.mark.parametrize("name", [
    "Game Capture HD60 X", "Elgato Cam Link 4K", "AVerMedia Live Gamer ULTRA GC553",
    "USB3.0 Capture", "Magewell USB Capture HDMI", "ezcap 261",
])
def test_real_capture_cards_still_classify_as_cards(name):
    assert ccb._classify_name(name) == "card"


def test_unselected_name_fallback_needs_exactly_one_card(monkeypatch):
    _inventory(monkeypatch, ["Integrated Webcam", "Game Capture HD60 X", "Elgato Cam Link 4K"],
               [A, B, C])
    assert CaptureCardBackend(device_index=0)._candidate_indices() == []
    _inventory(monkeypatch, ["Integrated Webcam", "Game Capture HD60 X"], [A, B])
    assert CaptureCardBackend(device_index=0)._candidate_indices() == [1]


def test_route_scope_refuses_a_card_named_row_that_is_not_the_selected_id(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "1")
    _inventory(monkeypatch, ["USB Video", "Game Capture HD60 X"], [A, B], selected=A)
    assert rpo._latency_route_scope(_scope_config()) == ""


# ---- CL3-F5-002: saved ID re-resolves a between-session reorder ---------------------------

def test_between_session_reorder_is_resolved_by_saved_id(monkeypatch):
    """F5 fixture: Integrated Webcam|USB Video, ids [b,a], selected a, saved index 0."""
    _inventory(monkeypatch, ["Integrated Webcam", "USB Video"], [B, A], selected=A)
    _no_reader(monkeypatch)
    backend = CaptureCardBackend(device_index=0)
    assert backend._candidate_indices() == [1]
    monkeypatch.setattr(backend, "_open", _fake_open(backend))
    assert backend.start() is True
    assert backend.route_identity_basis() == "configured"
    assert backend.identity_verified() is True


def test_route_guard_adopts_a_selected_id_resolution_cold(monkeypatch):
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    _inventory(monkeypatch, ["Integrated Webcam", "USB Video"], [B, A], selected=A)
    orch = _cc_orch()
    orch._frame_backend = SimpleNamespace(
        active_route=lambda: ("DSHOW", 1),
        negotiated_mode=lambda: (1920, 1080, 60.0, "YUY2", -1.0),
        route_identity_basis=lambda: "configured")
    assert orch._guard_capture_latency_route(frame_data=_card_frame(1)) is True
    assert orch._capture_warm_cache_expected_index == 1
    assert orch._capture_warm_cache_revoked
    assert os.environ.get("ORION_CAPTURE_CARD_INDEX") == "1"


def test_route_guard_never_adopts_a_card_name_resolution_against_a_selection(monkeypatch):
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    _inventory(monkeypatch, ["USB Video", "Game Capture HD60 X"], [A, B], selected=A)
    orch = _cc_orch()
    orch._frame_backend = SimpleNamespace(
        active_route=lambda: ("DSHOW", 1),
        negotiated_mode=lambda: (1920, 1080, 60.0, "YUY2", -1.0),
        route_identity_basis=lambda: "card_name")
    assert orch._guard_capture_latency_route(frame_data=_card_frame(1)) is False
    assert orch._capture_warm_cache_expected_index == 0


# ---- CL3-F5-006: 30 Hz is preview-only ----------------------------------------------------

def test_30hz_request_never_earns_timing_authority():
    ok, code, _ = qualify_capture_feed(30.0, 34.0, 0.0, 30.0, 30, samples=60)
    assert ok is False and code == "preview_only"
    ok, code, _ = qualify_capture_feed(60.0, 17.0, 0.0, 60.0, 120, samples=120)
    assert ok is False and code == "preview_only"
    assert qualify_capture_feed(59.9, 17.0, 2.0, 58.0, 60, samples=120)[0] is True


def test_preview_only_notice_names_the_setting():
    text = capture_notice_text("preview_only", fps=30, requested_fps=30)
    assert text.startswith("Capture: ") and text.isascii() and "=" not in text
    assert "30 Hz" in text and "60 Hz" in text and "Refresh rate" in text
    assert "OBS" not in text


def test_orchestrator_keeps_a_30hz_session_preview_only(monkeypatch):
    orch = _cc_orch()
    orch._requested_capture_fps = 30
    orch._frame_backend_mode = "capture_card"
    orch._unique_frame_fps, orch._duplicate_frame_pct = 30, 0.0
    orch._frame_backend = _fake_backend({"samples": 60, "fps": 30.0, "gap_p95_ms": 34.0})
    for _ in range(6):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_notice[1] == "preview_only"
    assert orch._capture_notice[0] == 1


# ---- CL3-F5-003: transient "duplicated" content is not a hardware alarm -------------------

def test_transient_duplicated_content_on_a_healthy_card_is_silent(monkeypatch):
    """Live 09-23 12:00: raw 60, dup 40 %, unique 36, cleared in ~3 s."""
    orch = _cc_orch()
    orch._frame_backend_mode = "capture_card"
    orch._frame_backend = _fake_backend({"samples": 120, "fps": 60.0, "gap_p95_ms": 18.6})
    orch._unique_frame_fps, orch._duplicate_frame_pct = 59, 1.0
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    orch._unique_frame_fps, orch._duplicate_frame_pct = 36, 40.0
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    assert orch._capture_notice[0] == 0                   # no false hardware alarm
    orch._unique_frame_fps, orch._duplicate_frame_pct = 59, 1.0
    orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True and orch._capture_notice[0] == 0


def test_persistent_frame_doubling_still_revokes_and_warns(monkeypatch):
    orch = _cc_orch()
    orch._frame_backend_mode = "capture_card"
    orch._frame_backend = _fake_backend({"samples": 120, "fps": 60.0, "gap_p95_ms": 17.0})
    orch._unique_frame_fps, orch._duplicate_frame_pct = 59, 1.0
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    orch._unique_frame_fps, orch._duplicate_frame_pct = 30, 50.0
    for _ in range(8):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_notice[1] == "duplicated"


def test_a_doubling_card_never_qualifies_from_cold(monkeypatch):
    orch = _cc_orch()
    orch._frame_backend_mode = "capture_card"
    orch._frame_backend = _fake_backend({"samples": 120, "fps": 60.0, "gap_p95_ms": 17.0})
    orch._unique_frame_fps, orch._duplicate_frame_pct = 30, 50.0
    for i in range(3):
        orch._update_capture_feed_qualification(0.0)
        assert orch._capture_feed_qualified is False, i
    assert orch._capture_notice[0] == 0                   # silent until it persists
    for _ in range(5):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_notice[1] == "duplicated"


# ---- helpers --------------------------------------------------------------------------------

def _scope_config():
    return rpo.OrchestratorConfig(
        frame_source="capture_card", resolution="1920x1080", target_fps=60,
        console_identity="registered-host-sha256-v1:" + "d" * 64,
        controller_route="pipe",
        capture_mode="1920x1080@60.000|fourcc=mjpg|buffer=1.000")


def _cc_orch():
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False, virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        controller_route="pipe", capture_mode=""))
    orch._capture_warm_cache_expected_index = int(os.environ["ORION_CAPTURE_CARD_INDEX"])
    orch._requested_capture_fps = 60
    return orch


def _card_frame(index):
    import time
    import numpy as np
    from chiaki_backend import FrameData
    frame = np.full((1080, 1920, 3), 64, dtype=np.uint8)
    frame.setflags(write=False)
    return FrameData(frame=frame, frame_number=1, timestamp_ns=time.perf_counter_ns(),
                     capture_api="DSHOW", capture_device_index=index, capture_width=1920,
                     capture_height=1080, capture_fps=60.0, capture_fourcc="YUY2",
                     capture_buffer_size=-1.0)


def _fake_backend(stats, index=0):
    return SimpleNamespace(cadence_stats=lambda window_s=None: dict(stats),
                           active_route=lambda: ("DSHOW", index),
                           identity_verified=lambda: True)
