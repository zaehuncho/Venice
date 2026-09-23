"""[CL2-P3-001/002/004/005 2026-09-23] Capture qualification by measurement + customer notices.

A customer with an unusual or misbehaving capture card must never get "the bot just doesn't
shoot" with no explanation. These pin: fire authority follows the MEASURED feed (not the
device name), non-card devices are never auto-selected and are reclaimed, and every failure
class produces a plain-language notice that names OBS only when the card is actually busy.
"""

import time
from types import SimpleNamespace

import pytest

import capture_card_backend as ccb
import remote_play_orchestrator as rpo
from capture_card_backend import (CaptureCardBackend, capture_notice_text,
                                  qualify_capture_feed)

IDS2 = "|".join(("dshow-moniker-sha256-v1:" + "1" * 64,
                 "dshow-moniker-sha256-v1:" + "2" * 64))


def test_generic_default_requires_explicit_stable_identity(monkeypatch):
    first, second = IDS2.split("|")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "USB Video|Integrated Webcam")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", IDS2)
    monkeypatch.delenv("ORION_CAPTURE_SELECTED_ID", raising=False)
    fresh = CaptureCardBackend(device_index=0)
    assert fresh._candidate_indices() == []
    assert fresh.identity_verified() is False
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    assert rpo._latency_route_scope(_scope_config()) == ""

    monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", first)
    chosen = CaptureCardBackend(device_index=0)
    assert chosen._candidate_indices() == [0]
    assert chosen.identity_verified() is True
    assert rpo._latency_route_scope(_scope_config())

    # Enumeration reordered under the old numerical index. The previous card
    # cannot inherit identity from index 0, even if its new name is generic.
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", second + "|" + first)
    stale = CaptureCardBackend(device_index=0)
    assert stale._candidate_indices() == []
    assert stale.identity_verified() is False
    assert rpo._latency_route_scope(_scope_config()) == ""

    monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", second)
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Webcam|USB Video")
    webcam = CaptureCardBackend(device_index=0)
    assert webcam._candidate_indices() == []
    assert webcam.identity_verified() is False


# ---- qualify_capture_feed ------------------------------------------------------------------

def test_healthy_60fps_feed_qualifies():
    ok, code, _ = qualify_capture_feed(59.8, 17.2, 3.0, 58, 60, samples=120)
    assert ok and code == ""


def test_30fps_feed_on_a_60_request_is_refused_low_fps():
    ok, code, detail = qualify_capture_feed(30.0, 33.4, 0.0, 30, 60, samples=60)
    assert not ok and code == "low_fps" and "30.0" in detail


def test_live_0921_degraded_session_is_refused():
    # 09-21 23:06: raw 34 fps, gaps to 207 ms -> must not keep fire authority.
    assert qualify_capture_feed(34.1, 105.6, 20.0, 30, 60, samples=68)[1] == "low_fps"
    # Enough frames on average but stuttering: the p95 gap catches it.
    ok, code, _ = qualify_capture_feed(55.0, 95.0, 5.0, 54, 60, samples=110)
    assert not ok and code == "gappy"


def test_frame_doubling_card_is_refused_duplicated():
    ok, code, _ = qualify_capture_feed(60.0, 17.0, 50.0, 30, 60, samples=120)
    assert not ok and code == "duplicated"


def test_static_menu_does_not_disqualify_a_good_card():
    # 100 % duplicates at a steady 60 raw fps is a menu/loading screen, not a bad card.
    ok, code, _ = qualify_capture_feed(60.0, 17.0, 100.0, 0, 60, samples=120)
    assert ok and code == ""


def test_explicit_30hz_pick_is_judged_on_its_own_grid():
    ok, _, _ = qualify_capture_feed(29.9, 34.0, 0.0, 30, 30, samples=60)
    assert ok


def test_no_samples_fails_closed():
    assert qualify_capture_feed(0.0, 0.0, 0.0, 0, 60, samples=0) == (
        False, "no_frames", "samples=0")


def test_gap_p95_is_published_by_cadence_stats():
    backend = CaptureCardBackend(fps=60)
    now = time.perf_counter_ns()
    stamps = [now - int(i * 16.7e6) for i in range(60)][::-1]
    stamps[30] = stamps[29] + int(120e6)    # one long hole among regular gaps
    with backend._arrival_lock:
        backend._arrival_ns.extend(stamps)
    stats = backend.cadence_stats(window_s=5.0)
    assert "gap_p95_ms" in stats
    assert stats["gap_p95_ms"] < 40.0 < stats["max_gap_ms"]


# ---- customer notices ----------------------------------------------------------------------

def test_notice_texts_are_plain_language_and_blame_obs_only_when_busy():
    low = capture_notice_text("low_fps", fps=30.2, requested_fps=60)
    assert "sending 30 fps" in low and "steady 60" in low
    assert "1080p60 or 720p60" in low and "USB 3.0" in low
    not_card = capture_notice_text("not_card", device="Microsoft Camera Front")
    assert "isn't your capture card" in not_card and "Stream Setup" in not_card
    busy = capture_notice_text("busy", device="Game Capture HD60 X")
    assert "busy" in busy and "OBS" in busy
    for code in ("low_fps", "gappy", "duplicated", "no_frames", "absent", "invalid",
                 "not_card", "ok"):
        text = capture_notice_text(code, fps=30, requested_fps=60, detail="undersized")
        assert text.startswith("Capture: ")
        assert "OBS" not in text, code
        assert text.isascii(), code
        # The Activity feed drops counter-looking lines (3+ key=value tokens).
        assert text.count("=") == 0, code
    assert "smaller than 720p" in capture_notice_text("invalid", detail="undersized")


# ---- CL2-P3-001: never auto-select an unknown-named device ---------------------------------

def test_candidate_walk_never_includes_an_unknown_named_camera(monkeypatch):
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Game Capture HD60 X|Microsoft Camera Front")
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: 1)   # a stale cache points at it
    assert CaptureCardBackend(device_index=0)._candidate_indices() == [0]


def test_explicitly_picked_unknown_named_card_is_still_opened(monkeypatch):
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Webcam|USB Video")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", IDS2)
    monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", IDS2.split("|")[1])
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: -1)
    assert CaptureCardBackend(device_index=1)._candidate_indices() == [1]


def test_busy_card_never_falls_through_to_a_webcam(monkeypatch):
    """The finding's fixture: card at 0 is busy, a 1080p camera at 1 would stream."""
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Game Capture HD60 X|Microsoft Camera Front")
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: -1)
    monkeypatch.setattr(ccb, "_BRUTE_SCANNED", True)
    pytest.importorskip("cv2")
    backend = CaptureCardBackend(device_index=0)
    opened = []

    def fake_open(index=None):
        opened.append(index)
        if index == 0:
            backend._open_diag = "busy"
            return None, ""
        return object(), "DSHOW"      # would "work" -- must never be reached

    monkeypatch.setattr(backend, "_open", fake_open)
    assert backend.start() is False
    assert opened == [0]
    code, dev, _ = backend.last_start_failure()
    assert code == "busy" and dev == "Game Capture HD60 X"


# ---- CL2-P3-002: explicit pick scopes; webcam pick does not --------------------------------

def _scope_config():
    return rpo.OrchestratorConfig(
        frame_source="capture_card", resolution="1920x1080", target_fps=60,
        console_identity="registered-host-sha256-v1:" + "d" * 64,
        controller_route="pipe",
        capture_mode="1920x1080@60.000|fourcc=mjpg|buffer=1.000")


def test_route_scope_trusts_an_explicitly_picked_card_without_a_hint_word(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "1")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Camera|USB Video")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", IDS2)
    monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", IDS2.split("|")[1])
    scope = rpo._latency_route_scope(_scope_config())
    assert scope and "device=usb video" in scope


def test_route_scope_still_refuses_a_webcam_pick(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Camera|USB Video")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", IDS2)
    monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", IDS2.split("|")[0])
    assert rpo._latency_route_scope(_scope_config()) == ""


# ---- orchestrator: qualification state machine, notices, reclaim ---------------------------

def _cc_orch(monkeypatch, names="Game Capture HD60 X|Microsoft Camera Front"):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", names)
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", IDS2)
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False, virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        controller_route="pipe", capture_mode="",
    ))
    orch._capture_warm_cache_expected_index = 0
    orch._requested_capture_fps = 60
    return orch


def _fake_backend(stats, index=0):
    return SimpleNamespace(cadence_stats=lambda window_s=None: dict(stats),
                           active_route=lambda: ("DSHOW", index),
                           identity_verified=lambda: True)


def test_capture_card_feed_starts_unqualified_and_other_sources_do_not(monkeypatch):
    assert _cc_orch(monkeypatch)._capture_feed_qualified is False
    monkeypatch.delenv("ORION_CAPTURE_CARD", raising=False)
    decoder = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False, virtual_controller=False))
    assert decoder._capture_feed_qualified is True


def test_slow_feed_never_qualifies_and_says_why(monkeypatch):
    orch = _cc_orch(monkeypatch)
    orch._frame_backend = _fake_backend({"samples": 60, "fps": 30.0, "gap_p95_ms": 33.4})
    orch._frame_backend_mode = "capture_card"
    orch._unique_frame_fps, orch._duplicate_frame_pct = 30, 0.0
    for _ in range(5):
        orch._update_capture_feed_qualification(time.perf_counter())
    assert orch._capture_feed_qualified is False
    seq, code, text = orch._capture_notice
    assert seq == 1 and code == "low_fps"      # once, not once per second
    assert "sending 30 fps" in text and "steady 60" in text


def test_steady_feed_qualifies_then_degrades_then_recovers(monkeypatch):
    orch = _cc_orch(monkeypatch)
    good = {"samples": 120, "fps": 59.9, "gap_p95_ms": 17.0}
    bad = {"samples": 70, "fps": 34.0, "gap_p95_ms": 105.0}
    orch._frame_backend_mode = "capture_card"
    orch._unique_frame_fps, orch._duplicate_frame_pct = 58, 2.0
    orch._frame_backend = _fake_backend(good)
    for i in range(3):
        assert orch._capture_feed_qualified is False, i    # fail closed until proven
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    assert orch._capture_notice[0] == 0                   # no noise on a clean start

    orch._frame_backend = _fake_backend(bad)
    orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True           # one bad window is tolerated
    orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_notice[1] == "low_fps"

    orch._frame_backend = _fake_backend(good)
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    assert orch._capture_notice[1] == "ok"
    assert "steady at 60 fps" in orch._capture_notice[2]


def test_dshow_route_on_a_non_card_index_is_reclaimed(monkeypatch):
    """CL2-P3-001(b): a healthy DSHOW device at the wrong index used to be kept forever."""
    orch = _cc_orch(monkeypatch)
    orch._capture_route_currently_invalid = True
    stopped = []
    orch._frame_backend = SimpleNamespace(active_route=lambda: ("DSHOW", 1),
                                          stop=lambda: stopped.append(True))
    orch._reclaim_dshow_route_if_invalid(100.0)
    assert stopped == [True] and orch._frame_backend is None
    seq, code, text = orch._capture_notice
    assert code == "not_card" and "Microsoft Camera Front" in text
    assert "OBS" not in text


def test_dshow_route_on_the_configured_index_is_left_alone(monkeypatch):
    orch = _cc_orch(monkeypatch)
    orch._capture_route_currently_invalid = True
    backend = SimpleNamespace(active_route=lambda: ("DSHOW", 0),
                              stop=lambda: pytest.fail("must not detach the configured card"))
    orch._frame_backend = backend
    orch._reclaim_dshow_route_if_invalid(100.0)
    assert orch._frame_backend is backend


def test_same_notice_is_not_repeated_by_the_retry_loop(monkeypatch):
    orch = _cc_orch(monkeypatch)
    assert orch._post_capture_notice("busy", device="Game Capture HD60 X")
    assert not orch._post_capture_notice("busy", device="Game Capture HD60 X")
    assert orch._post_capture_notice("absent")               # a new cause is immediate
    assert orch._capture_notice[0] == 2


# ---- sidecar relay -------------------------------------------------------------------------

def test_sidecar_relays_each_capture_notice_exactly_once():
    import importlib
    import sys
    from pathlib import Path
    backend_dir = str(Path(__file__).resolve().parents[1] / "native_orion" / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    sidecar = importlib.import_module("autogreen_sidecar")
    orch = SimpleNamespace(_capture_notice=(0, "", ""))
    state = {"seq": 0}
    assert sidecar._capture_notice_wire(orch, state) is None
    orch._capture_notice = (1, "busy", capture_notice_text("busy"))
    wire = sidecar._capture_notice_wire(orch, state)
    assert wire["event"] == "log" and wire["capture_notice"] == "busy"
    assert wire["msg"].startswith("Capture: ")
    assert sidecar._capture_notice_wire(orch, state) is None


# ---- round 2 (Codex CL2 r1 "needs changes") ------------------------------------------------

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("args", [
    (NAN, 17.0, 0.0, 60, 60),        # Codex repro 1: raw_fps NaN used to qualify
    (60.0, NAN, 0.0, 60, 60),        # Codex repro 2: gap_p95 NaN used to qualify
    (60.0, 17.0, NAN, 60, 60),
    (60.0, 17.0, 0.0, NAN, 60),
    (INF, 17.0, 0.0, 60, 60),
    (60.0, -INF, 0.0, 60, 60),
    (60.0, 17.0, 0.0, 60, NAN),
    (60.0, 17.0, 0.0, 60, INF),
    (None, 17.0, 0.0, 60, 60),
    (60.0, "x", 0.0, 60, 60),
    (60.0, -1.0, 0.0, 60, 60),
])
def test_non_finite_or_invalid_measurement_fails_closed(args):
    ok, code, detail = qualify_capture_feed(*args, samples=120)
    assert ok is False and code == "bad_measurement", (args, code, detail)


def test_non_finite_sample_count_fails_closed():
    assert qualify_capture_feed(60.0, 17.0, 0.0, 60, 60, samples=NAN)[1] == "bad_measurement"


def test_non_finite_env_threshold_cannot_open_the_gate(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_QUALIFY_MIN_FPS_RATIO", "nan")
    monkeypatch.setenv("ORION_CAPTURE_QUALIFY_MAX_GAP_P95_MS", "inf")
    assert qualify_capture_feed(30.0, 17.0, 0.0, 30, 60, samples=60)[1] == "low_fps"
    assert qualify_capture_feed(60.0, 95.0, 0.0, 60, 60, samples=120)[1] == "gappy"


def test_orchestrator_revokes_on_nan_stats_and_notifies(monkeypatch):
    orch = _cc_orch(monkeypatch)
    orch._frame_backend_mode = "capture_card"
    orch._unique_frame_fps, orch._duplicate_frame_pct = 58, 2.0
    orch._frame_backend = _fake_backend({"samples": 120, "fps": 59.9, "gap_p95_ms": 17.0})
    for _ in range(3):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is True
    orch._frame_backend = _fake_backend({"samples": 120, "fps": NAN, "gap_p95_ms": 17.0})
    orch._update_capture_feed_qualification(0.0)
    orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_feed_reason == "bad_measurement"
    assert orch._capture_notice[1] == "bad_measurement"
    assert orch._capture_notice[2].startswith("Capture: ")


# -- CL2-P3-001 r2: explicit identity before fire authority

def test_missing_enumeration_opens_only_the_configured_index(monkeypatch):
    monkeypatch.delenv("ORION_VIDEO_DEVICE_NAMES", raising=False)
    monkeypatch.delenv("ORION_CAPTURE_LEGACY_SCAN", raising=False)
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: 3)   # never used without names
    monkeypatch.setattr(ccb, "_BRUTE_SCANNED", False)
    pytest.importorskip("cv2")
    backend = CaptureCardBackend(device_index=0)
    opened = []

    def fake_open(index=None):
        opened.append(index)
        backend._open_diag = "absent"
        return None, ""

    monkeypatch.setattr(backend, "_open", fake_open)
    monkeypatch.setattr(backend, "_find_best_device_index",
                        lambda *a, **k: pytest.fail("identity-free brightness scan ran"))
    assert backend.start() is False
    assert opened == [0]
    assert ccb._BRUTE_SCANNED is False


def test_a_live_device_without_enumeration_is_never_identified(monkeypatch):
    """A webcam at the configured index with no inventory: it may stream, never fire."""
    monkeypatch.delenv("ORION_VIDEO_DEVICE_NAMES", raising=False)
    backend = CaptureCardBackend(device_index=0)
    backend._route_basis = "configured"
    assert backend.identity_verified() is False


def test_webcam_as_the_only_live_device_is_never_opened(monkeypatch):
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Webcam")
    monkeypatch.setattr(ccb, "_load_cached_index", lambda: 0)
    monkeypatch.setattr(ccb, "_BRUTE_SCANNED", False)
    pytest.importorskip("cv2")
    backend = CaptureCardBackend(device_index=0)
    monkeypatch.setattr(backend, "_open",
                        lambda index=None: pytest.fail("webcam %r opened" % index))
    assert backend.start() is False
    code, dev, _ = backend.last_start_failure()
    assert code == "not_card" and dev == "Integrated Webcam"
    assert "isn't your capture card" in capture_notice_text(code, device=dev)


@pytest.mark.parametrize("names,index,basis,expected", [
    ("Game Capture HD60 X", 0, "configured", True),
    ("Webcam|USB Video", 1, "configured", True),         # explicit pick, no hint word
    ("Game Capture HD60 X|Elgato Cam Link", 1, "card_name", True),
    ("Game Capture HD60 X|USB Video", 1, "uncertain", False),
    ("Integrated Webcam", 0, "configured", False),
    ("", 0, "configured", False),                           # enumeration empty
])
def test_identity_verified_matrix(monkeypatch, names, index, basis, expected):
    if names:
        monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", names)
        ids = IDS2.split("|")[:len(names.split("|"))]
        monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", "|".join(ids))
        if basis == "configured":
            monkeypatch.setenv("ORION_CAPTURE_SELECTED_ID", ids[index])
    else:
        monkeypatch.delenv("ORION_VIDEO_DEVICE_NAMES", raising=False)
    backend = CaptureCardBackend(device_index=index)
    backend._route_basis = basis
    assert backend.identity_verified() is expected


@pytest.mark.parametrize("ids", [
    None,                                    # timed-out native enumeration: names-only fallback
    "",                                      # empty ID field
    "dshow-moniker-sha256-v1:" + "1" * 64 + "|",  # missing second row
    IDS2.split("|")[0] + "|" + IDS2.split("|")[0],  # duplicate physical identity
    "not-a-moniker|" + IDS2.split("|")[1],  # malformed first row
])
def test_card_name_identity_rejects_name_only_or_invalid_inventory(monkeypatch, ids):
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Game Capture HD60 X|Elgato Cam Link")
    if ids is None:
        monkeypatch.delenv("ORION_VIDEO_DEVICE_IDS", raising=False)
    else:
        monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", ids)
    backend = CaptureCardBackend(device_index=1)
    backend._route_basis = "card_name"
    assert backend.identity_verified() is False


def test_unidentified_device_never_qualifies_even_when_steady(monkeypatch):
    orch = _cc_orch(monkeypatch)
    orch._frame_backend_mode = "capture_card"
    orch._unique_frame_fps, orch._duplicate_frame_pct = 60, 0.0
    backend = _fake_backend({"samples": 120, "fps": 60.0, "gap_p95_ms": 16.7})
    backend.identity_verified = lambda: False
    orch._frame_backend = backend
    for _ in range(6):
        orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False
    assert orch._capture_feed_reason == "unidentified"
    assert orch._capture_notice[1] == "unidentified"
    assert orch._capture_notice[0] == 1                       # once, not per second
    # A backend that cannot even answer the identity question is unidentified too.
    del backend.identity_verified
    orch._update_capture_feed_qualification(0.0)
    assert orch._capture_feed_qualified is False


# ---- sidecar -> native relay contract ("Capture:" line) ------------------------------------

def _repo_text(*parts):
    from pathlib import Path
    return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")


def test_capture_notice_wire_matches_the_native_relay_contract(capsys):
    """Pins both halves: the JSONL line the sidecar writes, and the native branch that parses it.

    RemotePlaySession::handleSidecarMessage relays a {"event":"log"} whose object has
    "capture_notice" and whose msg starts "Capture: " WITHOUT the "Sidecar:" prefix; the
    controller routes "Capture: " to appendCustomerEvent; the Activity ring keeps lines that
    are not "Sidecar:"-prefixed, not deny-listed and not counter-like.
    """
    import json
    import re
    import sys
    from pathlib import Path
    backend_dir = str(Path(__file__).resolve().parents[1] / "native_orion" / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import autogreen_sidecar as sidecar

    for code, kw in (("busy", {"device": "Game Capture HD60 X"}),
                     ("invalid", {"detail": "undersized"}),
                     ("low_fps", {"fps": 30.0, "requested_fps": 60}),
                     ("bad_measurement", {}),
                     ("unidentified", {"device": "USB Video"}),
                     ("ok", {"fps": 60.0})):
        orch = SimpleNamespace(_capture_notice=(1, code, capture_notice_text(code, **kw)))
        wire = sidecar._capture_notice_wire(orch, {"seq": 0})
        capsys.readouterr()
        assert sidecar._emit(wire)
        line = capsys.readouterr().out
        assert line.endswith("\n") and line.count("\n") == 1
        msg = json.loads(line)
        assert msg["event"] == "log" and "capture_notice" in msg
        assert msg["msg"].startswith("Capture: ") and not msg["msg"].startswith("Sidecar:")
        assert len(msg["msg"]) <= 400
        # Activity-ring rule 4 (UiNotificationPolicy.h looksLikeCounterLine): < 3 key=value.
        assert len(re.findall(r"(?:^|\s)[A-Za-z_][A-Za-z0-9_.\[\]]*=[^\s]", msg["msg"])) < 3

    rps = _repo_text("native_orion", "src", "RemotePlaySession.cpp")
    log_branch = rps[rps.index('event == QLatin1String("log")'):]
    log_branch = log_branch[:log_branch.index("} else if (event ==")]
    assert 'QStringLiteral("capture_notice")' in log_branch
    assert 'text.startsWith(QLatin1String("Capture: "))' in log_branch
    assert "emit setupMessage(text.left(400));" in log_branch
    ctl = _repo_text("native_orion", "src", "OrionAppController.cpp")
    assert 'message.startsWith(QLatin1String("Capture: "))' in ctl
    assert "appendCustomerEvent(message);" in ctl
    # Rule 2: no deny-list marker of the Activity ring matches any capture notice.
    policy = _repo_text("native_orion", "src", "UiNotificationPolicy.h")
    body = policy[policy.index("inline bool isEngineeringTelemetryLine"):]
    body = body[:body.index("inline bool looksLikeCounterLine")]
    deny = re.findall(r'QStringLiteral\("([^"]+)"\)', body)
    assert deny
    for code in ("busy", "absent", "invalid", "low_fps", "gappy", "duplicated", "no_frames",
                 "not_card", "unidentified", "bad_measurement", "ok"):
        text = capture_notice_text(code).lower()
        assert not any(marker.lower() in text for marker in deny), code
