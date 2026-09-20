"""Validate the event-driven meter emission (latency sprint, launch July 8).

The native engine's ONLY meter feed is the sidecar telemetry loop
(``autogreen_sidecar._telemetry_loop``). It used to emit on a FIXED 60 Hz tick
(``stop_evt.wait(1/60)``); it now waits on the orchestrator's per-frame
``RemotePlayOrchestrator._frame_ready_evt`` -- set in ``_processing_loop`` the
instant a detection publishes fresh meter state -- with a 1/60 s timeout fallback.

Net effect: a completed detection is emitted the instant it is ready instead of
waiting up to ~16.7 ms (avg ~8 ms) for the next fixed tick, and detection faster
than 60 fps is no longer throttled by the publisher.

This suite asserts, OFFLINE (no live session / no chiaki / no capture device):
  (a) emit fires IMMEDIATELY when a new frame is signaled (frame_ready),
  (b) the 1/60 s timeout fallback STILL emits when the meter is idle,
  (c) the emitted JSON schema is UNCHANGED by the event mechanism.

The pacing primitive under test (``_telemetry_wait``) is the REAL sidecar code.
The end-to-end timing tests drive it with a fake orchestrator + a fake emit, and
the schema tests lock the payload key set and cross-check it against both the live
sidecar source and the orchestrator wiring.

Run standalone:
    C:/Python314/python.exe tests/test_event_driven_meter_emission.py
or via pytest:
    C:/Python314/python.exe -m pytest tests/test_event_driven_meter_emission.py -v
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIDECAR_DIR = os.path.join(_REPO_ROOT, "native_orion", "backend")
_SIDECAR_SRC = os.path.join(_SIDECAR_DIR, "autogreen_sidecar.py")
_ORCH_SRC = os.path.join(_REPO_ROOT, "remote_play_orchestrator.py")
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

# Module-level import at top: the sidecar imports ONLY stdlib at module scope
# (cv2 / numpy are lazy-imported inside functions), so this is cheap and side-effect free.
import autogreen_sidecar as sidecar  # noqa: E402

_TICK = 1.0 / 60.0

# --- The payload schema the native engine parses (see autogreen_sidecar._telemetry_loop).
# Always-present top-level fields:
_REQUIRED_KEYS = {
    "event", "fps", "requested_fps", "capture_loop_fps", "unique_frame_fps",
    "capture_width", "capture_height", "capture_tier", "duplicate_frame_pct",
    "frame_age_ms", "pixel_age_ms", "transport_age_ms",
    "capture_publication_age_ms", "backend_frozen",
    "capture_ts_ms", "measurement_capture_ts_ms",
    "frame_count", "goto_active", "goto_triggered", "raw_fed",
    # RC-2b staleness contract: a monotonic per-emission heartbeat (advances even during a stall
    # when frame_count is frozen) + a stalled flag (true when pixel_age_ms crossed the watchdog).
    "heartbeat", "stalled",
}
# Conditionally-present top-level fields (added only when the data exists):
_OPTIONAL_KEYS = {"green", "shot", "bbox", "bbox_wh", "banner", "tracking", "fusion", "rtt"}
_GREEN_KEYS = {"start", "end", "center", "width"}
_SHOT_KEYS = {"fill_pct", "confidence", "rtt_offset_ms"}


# ---------------------------------------------------------------------------
#  Fakes: a minimal orchestrator exposing exactly the telemetry-read surface.
# ---------------------------------------------------------------------------
class _FakeShot:
    fill_pct = 42.5
    confidence = 0.87
    rtt_offset_ms = 12.0


class _FakeRemap:
    _shot = _FakeShot()


class _FakeOrch:
    """Stands in for RemotePlayOrchestrator: carries the per-frame event and the
    published meter/telemetry state the sidecar's _telemetry_loop reads live."""

    def __init__(self):
        self._frame_ready_evt = threading.Event()
        self._remap_engine = _FakeRemap()
        self._last_green_window = {"start": 92.0, "end": 100.0, "center": 96.0, "width": 8.0}
        self._last_meter_bbox = (100, 200, 30, 120)
        self._last_meter_bbox_wh = (1920, 1080)
        self._last_raw_fed = True
        self._fps = 60
        self._capture_fps = 61
        self._unique_frame_fps = 60
        self._duplicate_frame_pct = 3.0
        self._last_capture_tier = "capture_card"
        self._frame_count = 12345
        # frame-age inputs
        self._last_pts = 0
        self._pts_to_wall_offset = 0.0
        self._last_capture_ts = time.perf_counter()
        self._last_processed_epoch_ms = time.time() * 1000.0
        self._last_processed_measurement_epoch_ms = self._last_processed_epoch_ms + 20.0
        self._last_capture_publication_age_ms = 7.25
        self._last_frame_ts = time.perf_counter()
        self._last_feed_frozen = False
        # optional payload sources left absent (None) -> tested via source-scan instead
        self._last_meter_track = None
        self._last_release_fusion = None
        self._last_banner = None
        self._rtt_engine = None


class _FakeConfig:
    target_fps = 60


def _build_payload(orch, orch_config):
    """Faithful REPLICA of the required-key + green/shot/bbox payload build in
    autogreen_sidecar._telemetry_loop. Kept in lockstep with that code; the
    source-scan test below asserts the real sidecar still emits every one of these
    keys, so drift in the live schema is caught. Purpose: lock the schema (c)."""
    green = getattr(orch, "_last_green_window", None)
    shot = orch._remap_engine._shot if getattr(orch, "_remap_engine", None) else None
    _capture_ts = float(getattr(orch, "_last_capture_ts", 0.0) or 0.0)
    _frame_age_ms = (max(0.0, (time.perf_counter() - _capture_ts) * 1000.0)
                     if _capture_ts > 0.0 else 0.0)
    payload = {
        "event": "telemetry",
        "fps": int(getattr(orch, "_fps", 0) or 0),
        "requested_fps": int(getattr(orch_config, "target_fps", 60) or 60),
        "capture_loop_fps": int(getattr(orch, "_capture_fps", 0) or 0),
        "unique_frame_fps": int(getattr(orch, "_unique_frame_fps", 0) or 0),
        "capture_width": 1920,
        "capture_height": 1080,
        "capture_tier": str(getattr(orch, "_last_capture_tier", "") or ""),
        "duplicate_frame_pct": float(getattr(orch, "_duplicate_frame_pct", 0.0) or 0.0),
        "frame_age_ms": _frame_age_ms,
        # Raw epoch is identity-only. The explicit measurement epoch is the
        # canonical timing clock and may differ in either direction on decoder PTS.
        "capture_ts_ms": float(getattr(orch, "_last_processed_epoch_ms", 0.0) or 0.0),
        "measurement_capture_ts_ms": float(getattr(
            orch, "_last_processed_measurement_epoch_ms", 0.0) or 0.0),
        "pixel_age_ms": 0.0,
        "transport_age_ms": sidecar._raw_transport_age_ms(orch),
        "capture_publication_age_ms": sidecar._capture_publication_age_ms(orch),
        "backend_frozen": bool(getattr(orch, "_last_feed_frozen", False)),
        "frame_count": int(getattr(orch, "_frame_count", 0) or 0),
        "goto_active": bool(getattr(orch, "_goto_shot_active", False)),
        "goto_triggered": bool(getattr(orch, "_goto_shot_triggered", False)),
        "raw_fed": bool(getattr(orch, "_last_raw_fed", True)),
        # RC-2b: heartbeat is stamped per-emission; stalled is set by _apply_stall_override.
        "heartbeat": 1,
        "stalled": False,
    }
    if green:
        payload["green"] = {
            "start": float(green.get("start", 0.0)),
            "end": float(green.get("end", 0.0)),
            "center": float(green.get("center", 0.0)),
            "width": float(green.get("width", 0.0)),
        }
    if shot:
        payload["shot"] = {
            "fill_pct": float(getattr(shot, "fill_pct", 0.0) or 0.0),
            "confidence": float(getattr(shot, "confidence", 0.0) or 0.0),
            "rtt_offset_ms": float(getattr(shot, "rtt_offset_ms", 0.0) or 0.0),
        }
    bbox = getattr(orch, "_last_meter_bbox", None)
    if bbox is not None:
        payload["bbox"] = [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
        _bwh = getattr(orch, "_last_meter_bbox_wh", None)
        if _bwh is not None:
            payload["bbox_wh"] = [int(_bwh[0]), int(_bwh[1])]
    return payload


def _run_emit_loop(orch, emit_times, stop, timeout):
    """Mirror of _telemetry_loop's structure using the REAL _telemetry_wait: emit
    (record a timestamp), then pace. Records emission times so tests can measure
    both event-driven immediacy and the idle fallback cadence."""
    frame_ready = getattr(orch, "_frame_ready_evt", None)
    while not stop.is_set():
        emit_times.append(time.perf_counter())     # stand-in for _emit(payload)
        sidecar._telemetry_wait(frame_ready, stop, timeout=timeout)


# ===========================================================================
#  Part 1 -- the real _telemetry_wait pacing primitive
# ===========================================================================
def test_wait_immediate_when_event_already_set():
    evt, stop = threading.Event(), threading.Event()
    evt.set()
    t0 = time.perf_counter()
    woke = sidecar._telemetry_wait(evt, stop, timeout=_TICK)
    dt = time.perf_counter() - t0
    assert woke is True
    assert dt < _TICK / 3, f"should return instantly, took {dt*1000:.2f}ms"


def test_wait_wakes_on_late_signal_not_fallback():
    # Long fallback (1s): a prompt return can ONLY be the event, not the timeout.
    evt, stop = threading.Event(), threading.Event()

    def signal_soon():
        time.sleep(0.004)
        evt.set()

    threading.Thread(target=signal_soon, daemon=True).start()
    t0 = time.perf_counter()
    woke = sidecar._telemetry_wait(evt, stop, timeout=1.0)
    dt = time.perf_counter() - t0
    assert woke is True
    assert dt < 0.05, f"woke on the 1s fallback ({dt*1000:.1f}ms), not the ~4ms signal"


def test_wait_timeout_fallback_when_idle():
    evt, stop = threading.Event(), threading.Event()
    t0 = time.perf_counter()
    woke = sidecar._telemetry_wait(evt, stop, timeout=_TICK)
    dt = time.perf_counter() - t0
    assert woke is False, "no frame signaled -> must fall through on timeout"
    assert dt >= _TICK * 0.7, f"did not actually wait ~1/60s (got {dt*1000:.2f}ms) -> would busy-spin"


def test_wait_clears_event_for_next_iteration():
    evt, stop = threading.Event(), threading.Event()
    evt.set()
    sidecar._telemetry_wait(evt, stop, timeout=_TICK)
    assert not evt.is_set(), "event must be cleared so the next wait blocks for the NEXT frame"


def test_wait_inert_without_event_uses_stop_fallback():
    # frame_ready=None (an orchestrator build without the event) -> fixed-interval wait.
    stop = threading.Event()
    t0 = time.perf_counter()
    woke = sidecar._telemetry_wait(None, stop, timeout=0.02)
    dt = time.perf_counter() - t0
    assert woke is False
    assert dt >= 0.014, f"inert path must still throttle at the fallback interval (got {dt*1000:.2f}ms)"


# ===========================================================================
#  Part 2 -- (a) immediacy and (b) idle fallback, end-to-end in the loop
# ===========================================================================
def test_a_emit_fires_immediately_on_frame_ready():
    orch = _FakeOrch()
    emit_times, stop = [], threading.Event()
    # LONG fallback (1s): an emit shortly after we signal can only be event-driven.
    th = threading.Thread(target=_run_emit_loop, args=(orch, emit_times, stop, 1.0), daemon=True)
    th.start()
    time.sleep(0.03)                         # let it emit once and block in the 1s wait
    t_signal = time.perf_counter()
    orch._frame_ready_evt.set()              # a fresh detection publishes state
    time.sleep(0.03)
    stop.set()
    orch._frame_ready_evt.set()              # unblock the final wait so the thread exits
    th.join(timeout=1.0)

    after = [t for t in emit_times if t >= t_signal]
    assert after, "no emit after frame_ready was set"
    latency_ms = (after[0] - t_signal) * 1000.0
    # Must be far below the 1000ms fallback (and below a 60Hz tick) -> event-driven.
    assert latency_ms < 15.0, f"emit latency after frame_ready was {latency_ms:.1f}ms (not event-driven)"


def test_b_idle_fallback_still_emits_at_60hz():
    orch = _FakeOrch()
    emit_times, stop = [], threading.Event()
    th = threading.Thread(target=_run_emit_loop, args=(orch, emit_times, stop, _TICK), daemon=True)
    th.start()
    time.sleep(0.25)                         # ~15 ticks at 60Hz, NO frames signaled
    stop.set()
    orch._frame_ready_evt.set()
    th.join(timeout=1.0)

    n = len(emit_times)
    # 0.25s / (1/60s) ~= 15 emits; generous bounds for scheduler jitter, but must prove
    # the feed keeps flowing (not stalled) and is not busy-spinning (not hundreds).
    assert 8 <= n <= 30, f"idle fallback produced {n} emits, expected ~15 (>=60Hz, not a spin)"


# ===========================================================================
#  Part 3 -- (c) the JSON schema is unchanged by the event mechanism
# ===========================================================================
def test_c_payload_schema_matches_golden():
    payload = _build_payload(_FakeOrch(), _FakeConfig())
    keys = set(payload.keys())
    # every required key present
    missing = _REQUIRED_KEYS - keys
    assert not missing, f"payload missing required keys: {missing}"
    # no unexpected top-level keys
    unexpected = keys - _REQUIRED_KEYS - _OPTIONAL_KEYS
    assert not unexpected, f"payload has unexpected keys: {unexpected}"
    assert payload["event"] == "telemetry"
    # nested schemas
    assert set(payload["green"].keys()) == _GREEN_KEYS
    assert set(payload["shot"].keys()) == _SHOT_KEYS
    assert isinstance(payload["bbox"], list) and len(payload["bbox"]) == 4
    assert isinstance(payload["bbox_wh"], list) and len(payload["bbox_wh"]) == 2


def test_transport_age_tracks_raw_delivery_not_detector_eligibility():
    orch = _FakeOrch()
    # The detector can remain invalid/stale for a long loading screen while raw frames still
    # arrive. Process liveness must follow only the latter clock.
    orch._last_frame_ts = 10.0
    orch._last_capture_ts = 99.992
    assert sidecar._raw_transport_age_ms(orch, now=100.0) == pytest.approx(8.0)
    orch._last_feed_frozen = True
    payload = _build_payload(orch, _FakeConfig())
    assert payload["backend_frozen"] is True
    assert payload["transport_age_ms"] >= 0.0


def test_transport_age_is_bounded_for_startup_future_and_malformed_timestamps():
    orch = _FakeOrch()
    orch._last_capture_ts = 0.0
    assert sidecar._raw_transport_age_ms(orch, now=100.0) == 0.0
    orch._last_capture_ts = 101.0
    assert sidecar._raw_transport_age_ms(orch, now=100.0) == 0.0
    orch._last_capture_ts = "invalid"
    assert sidecar._raw_transport_age_ms(orch, now=100.0) == 0.0


def test_capture_publication_age_is_additive_and_bounded():
    orch = _FakeOrch()
    assert sidecar._capture_publication_age_ms(orch) == pytest.approx(7.25)
    assert _build_payload(orch, _FakeConfig())["capture_publication_age_ms"] \
        == pytest.approx(7.25)
    orch._last_capture_publication_age_ms = -1.0
    assert sidecar._capture_publication_age_ms(orch) == 0.0
    orch._last_capture_publication_age_ms = 60_001.0
    assert sidecar._capture_publication_age_ms(orch) == 0.0
    orch._last_capture_publication_age_ms = "invalid"
    assert sidecar._capture_publication_age_ms(orch) == 0.0


def test_c_payload_serialises_with_sidecar_separators():
    # The sidecar emits with json.dumps(payload, separators=(",", ":")). Assert the
    # payload round-trips and the compact form carries the schema byte-for-byte.
    payload = _build_payload(_FakeOrch(), _FakeConfig())
    line = json.dumps(payload, separators=(",", ":"))
    assert '"event":"telemetry"' in line
    assert json.loads(line) == payload


def test_c_all_schema_keys_present_in_sidecar_source():
    """Cross-check the golden schema against the LIVE sidecar source so a field
    rename/removal in the real _telemetry_loop is caught here (ties the replica to
    real code without importing/constructing the whole orchestrator)."""
    src = open(_SIDECAR_SRC, encoding="utf-8").read()
    for key in (_REQUIRED_KEYS | _OPTIONAL_KEYS | _GREEN_KEYS | _SHOT_KEYS):
        assert f'"{key}"' in src, f'schema key "{key}" no longer emitted by the sidecar'


def test_c_event_mechanism_does_not_touch_payload():
    # The pacing primitive is payload-agnostic: it takes only Events + a timeout and
    # returns a bool, so it structurally cannot add/remove/reshape a payload field.
    import inspect
    sig = inspect.signature(sidecar._telemetry_wait)
    assert list(sig.parameters) == ["frame_ready", "stop_evt", "timeout"]
    got = sidecar._telemetry_wait(threading.Event(), threading.Event(), timeout=0.001)
    assert got is False  # returns a bool verdict, never a payload
    # It never emits: no _emit(...) call in its body (docstring prose is stripped first).
    body = inspect.getsource(sidecar._telemetry_wait)
    doc = sidecar._telemetry_wait.__doc__ or ""
    for line in doc.splitlines():
        body = body.replace(line, "")
    assert "_emit(" not in body


# ===========================================================================
#  Wiring -- prove both ends of the event are connected in the real sources
# ===========================================================================
def test_orchestrator_creates_frame_ready_event():
    src = open(_ORCH_SRC, encoding="utf-8").read()
    assert "self._frame_ready_evt = threading.Event()" in src, \
        "orchestrator __init__ no longer creates _frame_ready_evt"


def test_orchestrator_sets_event_in_processing_loop():
    src = open(_ORCH_SRC, encoding="utf-8").read()
    loop_at = src.index("def _processing_loop(")
    next_def = src.index("\n    def ", loop_at + 1)
    loop_body = src[loop_at:next_def]
    # Processing completion is centralized so detector pixels, source frame number,
    # timestamps, health, and the wakeup are published as one paired state change.
    assert "self._finish_processed_frame(" in loop_body, \
        "_processing_loop no longer publishes paired frame completion"
    finish_at = src.index("def _finish_processed_frame(")
    finish_next = src.index("\n    def ", finish_at + 1)
    finish_body = src[finish_at:finish_next]
    assert "self._frame_ready_evt.set()" in finish_body, \
        "processed-frame publication no longer wakes the telemetry event"


def test_sidecar_telemetry_loop_is_event_driven():
    src = open(_SIDECAR_SRC, encoding="utf-8").read()
    assert "_telemetry_wait(frame_ready, stop_evt)" in src, \
        "telemetry loop no longer waits on the frame-ready event"
    assert 'getattr(orch, "_frame_ready_evt", None)' in src, \
        "telemetry loop no longer reads the orchestrator's frame-ready event"


# ===========================================================================
#  Detector health -- the Meter Detection card's {"event":"detector_health"} line
# ===========================================================================

# The reader's documented snapshot (SimpleMeterReader.detector_health_snapshot).
_HEALTH_SNAPSHOT = {
    "provider": "cv-contour", "infer_ms": 0.53, "state": "locked",
    "calls": 812, "found": 640, "locks": 12, "drops": 3, "hot_submit": 7, "reseat_x": 1,
    "cv_hit": 640, "cv_no_tip": 150, "cv_no_outline": 22, "cv_outline_bridged": 4,
}


class _FakeReader:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def detector_health_snapshot(self):
        return self._snapshot


def test_detector_health_wire_is_none_without_a_reader():
    from types import SimpleNamespace
    assert sidecar._detector_health_wire(SimpleNamespace()) is None
    assert sidecar._detector_health_wire(SimpleNamespace(_meter_detector=None)) is None
    # A reader without the snapshot (legacy chain) is not a reason to emit...
    assert sidecar._detector_health_wire(SimpleNamespace(_meter_detector=object())) is None
    # ...and neither is the reader failing closed (snapshot -> None).
    assert sidecar._detector_health_wire(
        SimpleNamespace(_meter_detector=_FakeReader(None))) is None


def test_detector_health_wire_flattens_the_reader_snapshot():
    from types import SimpleNamespace
    payload = sidecar._detector_health_wire(
        SimpleNamespace(_meter_detector=_FakeReader(dict(_HEALTH_SNAPSHOT))))
    assert payload["event"] == "detector_health"
    for key, value in _HEALTH_SNAPSHOT.items():
        assert payload[key] == value
    # Native parses it with QJsonDocument: the sidecar's separators must round-trip and
    # the line must stay small (a status line, not a telemetry-sized payload).
    line = json.dumps(payload, separators=(",", ":"))
    assert json.loads(line) == payload
    assert len(line) < 400


def test_detector_health_wire_coerces_foreign_values_to_wire_types():
    from types import SimpleNamespace
    snapshot = {"provider": object(), "infer_ms": "0.4", "state": None, "locks": True,
                "note": "x" * 200}
    payload = sidecar._detector_health_wire(
        SimpleNamespace(_meter_detector=_FakeReader(snapshot)))
    assert isinstance(payload["provider"], str)      # never a raw object on the wire
    assert payload["infer_ms"] == "0.4"              # strings pass through, bounded
    assert payload["state"] is None
    assert payload["locks"] is True
    assert len(payload["note"]) == 48
    json.dumps(payload)                              # always serialisable


def test_sidecar_telemetry_loop_emits_detector_health_on_its_own_line():
    """The health line rides the telemetry THREAD (one periodic mechanism) but never the
    telemetry PAYLOAD (the engine's meter feed): its own small event at ~2 s."""
    src = open(_SIDECAR_SRC, encoding="utf-8").read()
    start = src.index("def _telemetry_loop():")
    end = src.index("threading.Thread(target=_telemetry_loop", start)
    loop = src[start:end]
    assert "_detector_health_wire(orch)" in loop
    assert "_emit(_health)" in loop
    assert "_DETECTOR_HEALTH_INTERVAL_S" in loop
    assert sidecar._DETECTOR_HEALTH_INTERVAL_S == 2.0
    # Not a key of the engine-facing telemetry payload, by schema and by source.
    assert "detector_health" not in (_REQUIRED_KEYS | _OPTIONAL_KEYS | _GREEN_KEYS | _SHOT_KEYS)
    assert 'payload["detector_health"]' not in loop


if __name__ == "__main__":
    # Standalone runner (no pytest dependency needed).
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
