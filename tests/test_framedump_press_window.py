"""ORION_FRAMEDUMP_PRESS_WINDOW: every frame inside a press window, nothing outside.

THE FAILURE THIS GUARDS.  The 09-15 drill asked for a frame every 0.1 s and got 826 of
1421 -- `dropped=595 skipped=119 state=stopped:idle` -- because a 1080p PNG costs ~49 ms
and the writer was additionally rate-limited by a one-slot queue and a 25 % duty cooldown.
The anchor work needs the OPPOSITE trade: a bounded burst of EVERY frame around the press,
and silence in between.
"""

import os
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as rpo


_GIB = 1024 ** 3
FPS = 60.0
DT = 1.0 / FPS


def _press_orchestrator(tmp_path, monkeypatch, **env):
    """A framedump-only orchestrator in press-window mode, with a REAL writer thread."""
    # Positive writer/window assertions must not depend on workstation free space.
    # Dedicated disk-guard tests retain the production low-space policy.
    monkeypatch.setattr(rpo.shutil, "disk_usage", lambda _path:
                        SimpleNamespace(total=100 * _GIB, used=_GIB, free=99 * _GIB))
    monkeypatch.setenv("ORION_FRAMEDUMP_PRESS_WINDOW", "1")
    monkeypatch.setenv("ORION_FRAMEDUMP_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))

    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._init_framedump_press_window()
    orch._framedump_dir = str(tmp_path)
    orch._framedump_env_enabled = True
    orch._framedump_enabled = True
    orch._framedump_disabled_reason = ""
    orch._framedump_permanent_stop = ""
    orch._framedump_count = 0
    orch._framedump_max = 100000
    orch._framedump_interval = 0.2          # deliberately COARSE: the window must ignore it
    orch._framedump_last_ts = 0.0
    orch._framedump_armed = True
    orch._framedump_dropped = 0
    orch._framedump_reported_dropped = 0
    orch._framedump_skipped = 0
    orch._framedump_raw_only = True
    orch._framedump_min_free_bytes = 1
    orch._framedump_min_free_pct = 0.5
    orch._framedump_max_duty = 0.25
    orch._framedump_max_write_s = 2.0
    orch._framedump_max_error_streak = 5
    orch._framedump_backoff_until = 0.0
    orch._framedump_backoff_s = 0.0
    orch._framedump_backoff_max_s = 1.0
    orch._framedump_slow_streak = 0
    orch._framedump_error_streak = 0
    orch._framedump_last_write_ms = 0.0
    orch._framedump_last_write_ts = 0.0
    orch._framedump_heartbeat_ts = 0.0
    orch._framedump_heartbeat_s = 0.0       # no heartbeat noise in the test
    orch._framedump_index_failed = False
    orch._framedump_generation = 1
    orch._meter_detector = None
    orch._shot_records = None
    orch._framedump_writer_stop = threading.Event()
    orch._framedump_q = queue.Queue(maxsize=orch._framedump_queue_depth)
    orch._framedump_writer = threading.Thread(
        target=orch._framedump_writer_loop, name="framedump-writer-test", daemon=True)
    orch._framedump_writer.start()
    return orch


def _result(detected=True, fill=42.0):
    return SimpleNamespace(gameplay_eligible=True, detected=detected, fill_pct=fill,
                           confidence=0.9, rejection_reason="", green_window_center_pct=95.0,
                           green_window_confidence=0.8, bbox=(1, 2, 3, 4),
                           green_window_start_row=-1, green_window_end_row=-1)


def _frame(i):
    f = np.zeros((48, 64, 3), dtype=np.uint8)
    f[:, :, 0] = i % 251
    return f


def _pace(deadline_s):
    """Emit active-window frames on the synthetic 60 fps wall clock.

    Waiting only when the queue was full let the producer compress 30 simulated
    seconds into under a real second. A single Windows scheduler pause could then
    overflow the queue even though a real 60 fps producer would still be waiting
    for its next frame. We keep the zero-drop assertion, but give the writer the
    actual inter-frame interval rather than an opportunistic queue-full timeout.
    """
    remaining = deadline_s - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)


def _idle_gap(orch, budget_s=2.0):
    """Model the 1.35 real seconds BETWEEN windows, in which the writer catches up.

    Compressed test time otherwise carries a window's backlog straight into the next
    window's pre-roll, which would make the drop count an artefact of the harness.
    """
    end = time.perf_counter() + budget_s
    while not orch._framedump_q.empty() and time.perf_counter() < end:
        time.sleep(0.0005)


def _settle(orch, timeout=10.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if orch._framedump_q.empty():
            time.sleep(0.02)
            if orch._framedump_q.empty():
                return True
        time.sleep(0.005)
    return False


def _teardown(orch):
    orch._framedump_writer_stop.set()
    try:
        orch._framedump_q.put_nowait(None)
    except Exception:
        pass
    orch._framedump_writer.join(timeout=2.0)
    fh = getattr(orch, "_framedump_index_fh", None)
    if fh is not None:
        fh.close()
        orch._framedump_index_fh = None


def _written(tmp_path):
    return sorted(p for p in os.listdir(str(tmp_path)) if p.endswith((".jpg", ".png")))


# --------------------------------------------------------------------------- the run
def test_thirty_seconds_ten_windows_no_drops_inside_no_frames_outside(tmp_path, monkeypatch):
    """30 s of synthetic 60 fps with 10 presses: every in-window frame, nothing else."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        assert orch._framedump_press_window is True
        assert orch._framedump_format == "jpg"          # PNG cannot express 60 fps
        assert orch._framedump_queue_depth >= 64        # the burst has to fit

        pre_s = orch._framedump_press_pre_ms / 1000.0
        post_s = orch._framedump_press_post_ms / 1000.0
        hold_s = 0.65
        presses = [2.0 + 3.0 * k for k in range(10)]     # one press every 3 s
        releases = [p + hold_s for p in presses]
        expected = 0
        t0 = 1000.0
        n_frames = int(30.0 * FPS)
        pi = 0
        opened = closed = False
        wall_anchor = time.perf_counter()
        for i in range(n_frames):
            now = t0 + i * DT
            rel_t = now - t0
            if float(orch._framedump_press_until or 0.0) <= now:
                _idle_gap(orch)
                # Quiet synthetic time is compressed. Start a fresh real-time
                # clock when the next press window opens; do not catch up in a
                # burst after the writer drains between windows.
                wall_anchor = time.perf_counter() - i * DT
            if pi < len(presses) and rel_t >= presses[pi] and not opened:
                orch.framedump_press_open(1000 + pi, mono_now=now)
                opened = True
            if opened and not closed and rel_t >= releases[pi]:
                orch.framedump_press_close(1000 + pi, mono_now=now)
                closed = True
            if closed and rel_t >= releases[pi] + post_s:
                pi += 1
                opened = closed = False
            _pace(wall_anchor + i * DT)
            orch._dump_frame(_frame(i), _result(), now=now)
            # ground truth: is this frame inside SOME [press-pre, release+post]?
            for p, r in zip(presses, releases):
                if p - pre_s <= rel_t <= r + post_s:
                    expected += 1
                    break
        last_window = int(orch._framedump_press_stats["frames"]) \
            if orch._framedump_press_stats else 0
        orch._framedump_close_press_stats("test_end")
        assert _settle(orch)

        files = _written(tmp_path)
        # ZERO frames outside a window, and every in-window frame present.
        assert orch._framedump_dropped == 0, "dropped %d inside the windows" % orch._framedump_dropped
        assert orch._framedump_skipped == 0
        assert orch._framedump_press_windows == 10
        assert orch._framedump_press_frames == len(files)
        # The realised window is quantised to frames (it opens on the frame that carries
        # the press, replays its pre-roll from THAT moment, and ends POST_MS after the
        # frame that carries the release), so each edge can be a frame late: never more
        # than three frames per window in total.
        assert abs(len(files) - expected) <= 10 * 3, (len(files), expected)
        # ...and the mean full window is the configured pre+hold+post, in frames. Derived
        # from the knobs, not hard-coded: ORION_FRAMEDUMP_PRESS_BANNER moves POST_MS from
        # 400 ms to 1700 ms and this bound has to follow it, or it stops meaning anything.
        ideal = (pre_s + hold_s + post_s) * FPS
        mean_full = (len(files) - last_window) / 9.0
        assert ideal - 2 <= mean_full <= ideal + 4, (mean_full, ideal)
        # every file names its own shot epoch -> the JSONL joins by epoch alone
        epochs = {f.split("_")[0] for f in files}
        assert epochs == {"ep%d" % (1000 + k) for k in range(10)}
    finally:
        _teardown(orch)


def test_frames_carry_the_pre_roll_before_the_press(tmp_path, monkeypatch):
    """press-200 ms is already in the past when the arm lands: the ring must replay it."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        t0 = 500.0
        for i in range(60):                       # 1 s of quiet feed
            orch._dump_frame(_frame(i), _result(detected=False), now=t0 + i * DT)
        assert _written(tmp_path) == []           # nothing outside a window
        press_at = t0 + 60 * DT
        orch.framedump_press_open(7, mono_now=press_at)
        assert _settle(orch)
        n_preroll = len(_written(tmp_path))
        # 200 ms of 60 fps = 12 frames; the ring holds 12 + 2 slack
        assert 11 <= n_preroll <= 14
        stats_frames = orch._framedump_press_frames
        assert stats_frames == n_preroll
    finally:
        _teardown(orch)


def test_type_upgrade_rearm_does_not_restart_the_window(tmp_path, monkeypatch):
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        now = 100.0
        assert orch.framedump_press_open(42, mono_now=now) is True
        until = orch._framedump_press_until
        orch._dump_frame(_frame(1), _result(), now=now + DT)
        # the native's source=type_upgrade re-arm carries the SAME epoch
        assert orch.framedump_press_open(42, mono_now=now + 0.2) is True
        assert orch._framedump_press_until == until
        assert orch._framedump_press_windows == 1
    finally:
        _teardown(orch)


def test_window_has_a_hard_cap_for_an_unanswered_press(tmp_path, monkeypatch):
    """A press with no release (the stuck-Square class) must not dump forever."""
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_PRESS_MAX_MS=500)
    try:
        t0 = 10.0
        orch.framedump_press_open(9, mono_now=t0)
        for i in range(120):                       # 2 s of feed, cap is 0.5 s
            orch._dump_frame(_frame(i), _result(), now=t0 + i * DT)
        assert _settle(orch)
        files = _written(tmp_path)
        assert 28 <= len(files) <= 33              # ~0.5 s at 60 fps, not 120 frames
        assert orch._framedump_press_until == 0.0  # the window closed itself
    finally:
        _teardown(orch)


def test_drops_are_counted_and_logged_never_silent(tmp_path, monkeypatch, caplog):
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_QUEUE_DEPTH=1)
    try:
        # Stall the writer so the one-slot queue saturates.
        gate = threading.Event()
        real_write = orch._framedump_write_item

        def _slow(item):
            gate.wait(2.0)
            return real_write(item)

        orch._framedump_write_item = _slow
        now = 5.0
        orch.framedump_press_open(3, mono_now=now)
        for i in range(30):
            orch._dump_frame(_frame(i), _result(), now=now + i * DT)
        assert orch._framedump_dropped > 0
        assert orch._framedump_press_stats["dropped"] == orch._framedump_dropped
        gate.set()
        _settle(orch)
        with caplog.at_level("ERROR", logger="RemotePlayOrchestrator"):
            orch._framedump_close_press_stats("test")
        assert any("FRAMEDUMP PRESS WINDOW" in r.message for r in caplog.records)
    finally:
        gate.set()
        _teardown(orch)


def test_shooter_crop_writes_the_patch_not_the_frame(tmp_path, monkeypatch):
    import cv2
    import player_anchor as pa
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_CROP="shooter",
                               ORION_FRAMEDUMP_CROP_W=120, ORION_FRAMEDUMP_CROP_H=150)
    try:
        monkeypatch.setattr(pa.ANCHOR, "_last", (300.0, 400.0, 1.0, 0.0), raising=False)
        now = 1.0
        orch.framedump_press_open(21, mono_now=now)
        big = np.zeros((720, 1280, 3), dtype=np.uint8)
        orch._dump_frame(big, _result(), now=now + DT)
        assert _settle(orch)
        files = _written(tmp_path)
        assert len(files) == 1
        img = cv2.imread(os.path.join(str(tmp_path), files[0]))
        assert img.shape[0] == 150 and img.shape[1] == 120
    finally:
        _teardown(orch)


def test_legacy_mode_is_unchanged_when_the_knob_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ORION_FRAMEDUMP_PRESS_WINDOW", raising=False)
    monkeypatch.delenv("ORION_FRAMEDUMP_FORMAT", raising=False)
    monkeypatch.delenv("ORION_FRAMEDUMP_QUEUE_DEPTH", raising=False)
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._init_framedump_press_window()
    assert orch._framedump_press_window is False
    assert orch._framedump_format == "png"      # legacy dumps keep PNG byte-for-byte
    assert orch._framedump_crop == ""


@pytest.mark.parametrize("fmt,ext", [("jpg", ".jpg"), ("png", ".png")])
def test_encoding_selection(tmp_path, monkeypatch, fmt, ext):
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_FORMAT=fmt)
    try:
        assert orch._framedump_encoding()[0] == ext
    finally:
        _teardown(orch)


# --------------------------------------------------------------------------- #
# [ORION_FRAMEDUMP_SESSION_DIR 2026-09-17] One folder per session.
#
# THE FAILURE.  Press-window filenames carry the SHOT-GATE EPOCH and the epoch restarts at
# 1 on every sidecar start, but the dump wrote into one shared ORION_FRAMEDUMP_ROOT: the
# 2026-09-17 root holds `ep2_f000100_*` twice over (126 files for a 97-frame window) and a
# frames.csv of 6,649 rows of which 4,339 belong to one session.
# --------------------------------------------------------------------------- #
def test_press_window_composes_a_per_session_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_FRAMEDUMP_PRESS_WINDOW", "1")
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_dir = str(tmp_path)
    orch._init_framedump_press_window()
    assert orch._framedump_session.startswith("session_")
    assert os.path.basename(orch._framedump_dir) == orch._framedump_session
    assert os.path.dirname(orch._framedump_dir) == str(tmp_path)


def test_a_directory_that_already_names_a_session_is_honoured(tmp_path, monkeypatch):
    r"""The launcher composes <root>\session_<stamp> itself; do not nest a second one."""
    monkeypatch.setenv("ORION_FRAMEDUMP_PRESS_WINDOW", "1")
    given = os.path.join(str(tmp_path), "session_20260917_030758")
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_dir = given
    orch._init_framedump_press_window()
    assert orch._framedump_dir == given
    assert orch._framedump_session == "session_20260917_030758"


def test_legacy_interval_mode_keeps_the_bare_root(tmp_path, monkeypatch):
    monkeypatch.delenv("ORION_FRAMEDUMP_PRESS_WINDOW", raising=False)
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_dir = str(tmp_path)
    orch._init_framedump_press_window()
    assert orch._framedump_dir == str(tmp_path)
    assert orch._framedump_session == ""


# --------------------------------------------------------------------------- #
# [ORION_FRAMEDUMP_PREROLL_CONTINUOUS 2026-09-17] A back-to-back press still has context.
#
# THE FAILURE.  ep35/37/39 of the 2026-09-17 dump report `preroll=0` and their first frame
# is the press itself (+7 ms).  Those three presses landed 0.8-1.1 s after an UNRELEASED
# press whose window runs to the 3000 ms hard cap, so the previous window was still open and
# the ring -- which was only fed while no window was open -- was empty.
# --------------------------------------------------------------------------- #
def test_the_ring_is_fed_inside_a_window_too(tmp_path, monkeypatch):
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        now = 10.0
        orch.framedump_press_open(1, mono_now=now)
        for i in range(6):
            orch._dump_frame(_frame(i), _result(), now=now + i * DT)
        # frames written INSIDE window 1 are still in the ring, flagged as already written
        assert len(orch._framedump_preroll) == 6
        assert all(item[3] for item in orch._framedump_preroll)
    finally:
        _teardown(orch)


def test_a_superseding_press_reports_its_preroll_as_on_disk(tmp_path, monkeypatch):
    """The frames exist -- under the PREVIOUS epoch's prefix -- so they are counted, not
    written twice: re-dumping them would only double the bytes."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        now = 20.0
        orch.framedump_press_open(1, mono_now=now)
        for i in range(6):
            orch._dump_frame(_frame(i), _result(), now=now + i * DT)
        written_in_first = orch._framedump_count
        orch.framedump_press_open(2, mono_now=now + 6 * DT)      # supersedes window 1
        stats = orch._framedump_press_stats
        assert stats["epoch"] == 2
        assert stats["preroll"] == 0
        assert stats["preroll_ondisk"] == 6
        assert orch._framedump_count == written_in_first          # nothing re-written
    finally:
        _teardown(orch)


def test_an_idle_gap_still_supplies_real_preroll(tmp_path, monkeypatch):
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        now = 30.0
        for i in range(6):                     # no window open: pure pre-roll
            orch._dump_frame(_frame(i), _result(), now=now + i * DT)
        assert orch._framedump_count == 0
        orch.framedump_press_open(5, mono_now=now + 6 * DT)
        stats = orch._framedump_press_stats
        assert stats["preroll"] == 6
        assert stats["preroll_ondisk"] == 0
        assert orch._framedump_count == 6
    finally:
        _teardown(orch)


def test_the_window_line_appends_preroll_ondisk(tmp_path, monkeypatch, caplog):
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        now = 40.0
        orch.framedump_press_open(7, mono_now=now)
        orch._dump_frame(_frame(0), _result(), now=now + DT)
        _settle(orch)
        with caplog.at_level("ERROR", logger="RemotePlayOrchestrator"):
            orch._framedump_close_press_stats("test")
        line = [r.getMessage() for r in caplog.records if "FRAMEDUMP PRESS WINDOW" in
                r.getMessage()]
        assert line and " preroll_ondisk=" in line[0]
        # append-only: every pre-existing field is still in its original order
        assert line[0].index("frames=") < line[0].index("preroll=") \
            < line[0].index("dropped=") < line[0].index("skipped=") \
            < line[0].index("idx=") < line[0].index("preroll_ondisk=")
    finally:
        _teardown(orch)


# ---------------------------------------------------------- the banner leg (09-17)
def test_the_window_reaches_the_banner_by_default(tmp_path, monkeypatch):
    """[ORION_FRAMEDUMP_PRESS_BANNER 2026-09-17] The game's TIMING|DISTANCE panel lands
    1.2-1.45 s after the RELEASE, and the window used to close at release+400 ms -- so of
    the 53 press windows dumped on 2026-09-17 03:07 only NINE contained a panel at all, and
    those nine were the PREVIOUS shot's.  Every offline study that needs the banner as its
    oracle was reading a corpus that structurally could not contain its own answer."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        assert orch._framedump_press_banner is True
        assert orch._framedump_press_post_ms == 1700.0
        # ...and the hard cap has to clear the longest real shot plus that leg, or it
        # silently truncates exactly the frames the change is for (a Go-To releases ~2.1 s
        # after the press: 2100 + 1700 = 3800 ms).
        assert orch._framedump_press_max_ms >= 3800.0
    finally:
        _teardown(orch)


def test_the_banner_leg_has_a_kill_switch(tmp_path, monkeypatch):
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_PRESS_BANNER=0)
    try:
        assert orch._framedump_press_banner is False
        assert orch._framedump_press_post_ms == 400.0
        assert orch._framedump_press_max_ms == 3000.0
    finally:
        _teardown(orch)


def test_an_explicit_post_ms_beats_both_defaults(tmp_path, monkeypatch):
    orch = _press_orchestrator(tmp_path, monkeypatch, ORION_FRAMEDUMP_PRESS_POST_MS=250)
    try:
        assert orch._framedump_press_banner is True
        assert orch._framedump_press_post_ms == 250.0
    finally:
        _teardown(orch)


def test_a_goto_length_window_still_reaches_the_banner(tmp_path, monkeypatch):
    """The hard cap is the thing that could quietly undo this, so it is tested with the
    longest shot in the corpus: ep33 of session_20260917_030758 released 2102 ms after
    its press."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    try:
        t0 = 900.0
        orch.framedump_press_open(33, mono_now=t0)
        orch.framedump_press_close(33, mono_now=t0 + 2.102)
        # the banner lands release + 1.2..1.45 s; the window must still be open then
        assert orch._framedump_press_active(t0 + 2.102 + 1.45) is True
        assert orch._framedump_press_active(t0 + 2.102 + 1.70 + 0.01) is False
    finally:
        _teardown(orch)


def test_one_window_of_real_720p_frames_still_drops_nothing(tmp_path, monkeypatch):
    """The 30 s run above uses 48x64 frames, so it proves the WINDOW logic and not the
    encoder.  A 720p JPEG costs 1.8 ms to encode (measured on this workstation), so a
    raw+overlay pair is ~3.6 ms against a 16.7 ms frame period -- this asserts that on
    real-sized frames, for a full banner-length window."""
    orch = _press_orchestrator(tmp_path, monkeypatch)
    orch._framedump_raw_only = False          # the 09-17 configuration: raw AND overlay
    try:
        rng = np.random.default_rng(3)
        big = [rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8) for _ in range(4)]
        import cv2
        cv2.imencode(".jpg", big[0])          # warm the encoder, as a real session does
        t0 = 2000.0
        wall0 = time.perf_counter()
        span = (orch._framedump_press_pre_ms + 650.0
                + orch._framedump_press_post_ms) / 1000.0
        n = int(span * FPS)
        orch.framedump_press_open(1, mono_now=t0)
        closed = False
        for i in range(n):
            now = t0 + i * DT
            if not closed and (now - t0) >= 0.65:
                orch.framedump_press_close(1, mono_now=now)
                closed = True
            # paced to a REAL 60 fps: the claim under test is that the writer keeps up
            # with the feed, and a producer running faster than the feed cannot test it.
            due = wall0 + i * DT
            while time.perf_counter() < due:
                time.sleep(0.0005)
            orch._dump_frame(big[i % len(big)], _result(), now=now)
        written = orch._framedump_press_frames
        orch._framedump_close_press_stats("test_end")
        assert _settle(orch, timeout=30.0)
        assert orch._framedump_dropped == 0, orch._framedump_dropped
        assert orch._framedump_skipped == 0
        # every accepted frame reached the disk as a raw + overlay pair...
        assert len(_written(tmp_path)) == 2 * written
        # ...and the window really did carry the banner leg (hold + post at 60 fps; there
        # is no pre-roll here because nothing was captured before the press).
        assert written >= int((0.65 + orch._framedump_press_post_ms / 1000.0) * FPS) - 2
    finally:
        _teardown(orch)
