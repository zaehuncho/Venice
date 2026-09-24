"""[SHIP CONFIG 2026-09-17] What a PACKAGED install runs on when no environment is set.

THE PROBLEM THIS SUITE EXISTS FOR
---------------------------------
The configuration every graded session ran on was set by ENVIRONMENT VARIABLES on the dev
launch line (``run_orion.local.ps1`` and the ORION_* prefix the owner typed in front of it).
That launcher is gitignored (``.gitignore``: ``*.local.ps1``) and appears in no packaging list
(``installer/orion.iss``, ``tools/package_orion_release.py``), and the native parent
(``RemotePlaySession::startSidecar``) exports only the SidecarReaderProfile.h flags plus the
transport keys -- NONE of the switches below.  A customer therefore inherits ZERO ORION_*
variables, so any switch that lives only on that launch line ships in its SOURCE default.

That is the same divergence ``native_orion/src/SidecarReaderProfile.h`` was written to close,
one layer up: "every live batch this project has ever graded ran with those flags ON, and a
customer build inherits zero ORION_* variables".

So each test below imports the real module with a SCRUBBED environment (every ORION_* removed,
exactly what a fresh install has) and asserts the switch resolves to the value the owner's ship
sessions ran on.  Nothing here asserts BEHAVIOUR -- the behaviour suites are unchanged and own
that -- so moving a default on purpose is one line here plus a row in ``docs/SHIP_CONFIG.md``,
and moving one by accident is a failure.

The native half of the same fence is ``AutomationEngineTests::shipConfigDefaultsArePinned`` /
``::shipConfigDefaultsSurviveAKeylessSettingsFile``.

SHIP LINE (09-16 22:40, the last one graded), reproduced here so this file states the contract
it is testing:

    ON : BANNER_VERDICT_LIVE  PLAYER_ANCHOR  ANCHORED_SEARCH  ANCHOR_REFUSE_MODE=1
         EXPECTATION_WINDOW   CV_SHAPE_MIN_H_ARMED=8  CV_COL_W_MIN_ARMED=6
         READER_GHOST_FORGET_LOCATOR (+ its rate limit)  READER_BOX_LATCH
         READER_STATIC_ZONE_QUARANTINE  CV_TIPLESS_ARMED  SHOT_RECORDS
    OFF: READER_FRESH_AFTER_GHOST  READER_IDLE_PUBLISH_GATE  LOCATOR_IDLE_REUSE
         STALL_ATTRIB  FRAMEDUMP*  VISION_HOLD_BAND(_FADE)
"""
from __future__ import annotations

import os
import re
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- the scrub
@pytest.fixture
def ship_env(monkeypatch):
    """A fresh install's environment: not one ORION_* variable anywhere.

    Autouse is deliberately NOT used -- a test that wants to prove the ENV OVERRIDE still wins
    sets its own variable AFTER taking this fixture, and the scrub must not fight it.
    """
    for key in list(os.environ):
        if key.startswith("ORION_"):
            monkeypatch.delenv(key, raising=False)
    return os.environ


def test_the_scrub_really_removes_every_orion_variable(ship_env):
    """Guard for the guard: if the scrub missed, every assertion below would be vacuous."""
    leaked = sorted(k for k in os.environ if k.startswith("ORION_"))
    assert leaked == [], f"the ship-default fixture leaked {leaked}"


# --------------------------------------------------------------------------- 1. the locator
def _fresh_locator():
    import meter_locator_cv as mlc
    return mlc.MeterContourLocator()


def test_player_anchor_is_on_by_default(ship_env):
    """[ORION_PLAYER_ANCHOR] The anchor supplies WHERE; nothing below it works without it."""
    import player_anchor as pa
    assert pa.enabled() is True


def test_player_anchor_env_override_still_turns_it_off(ship_env, monkeypatch):
    import player_anchor as pa
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")
    assert pa.enabled() is False


def test_anchored_search_and_expectation_window_are_on_by_default(ship_env):
    """[ORION_ANCHORED_SEARCH / ORION_EXPECTATION_WINDOW] WHERE to look, and WHEN."""
    loc = _fresh_locator()
    assert loc.anchored_search is True
    assert loc.expect_window is True


def test_the_armed_floors_are_the_graded_eight_and_six(ship_env):
    """[ORION_CV_SHAPE_MIN_H_ARMED=8 / ORION_CV_COL_W_MIN_ARMED=6]

    These are the two numbers that let a 12-14 %% onset be accepted inside the anchor's patch.
    They used to default to the UNARMED floors (14 and 8), i.e. to "today's behaviour", which
    is exactly the configuration no graded session ran.
    """
    loc = _fresh_locator()
    assert loc.shape_min_h_armed == 8.0
    assert loc.col_w_min_armed == 6.0
    # ...and they are still only the ARMED floors: the unarmed gates are untouched.
    assert loc.shape_min_h == 14.0
    assert loc.col_w_min == 8.0


def test_the_armed_floors_can_never_sit_above_the_unarmed_ones(ship_env, monkeypatch):
    """The armed floor is a RELAXATION, so the ship default follows a lowered unarmed floor.

    Without the `min`, a dev A/B that lowered ORION_CV_SHAPE_MIN_H would end up with an ARMED
    floor above its own unarmed one -- a relaxation that tightens.
    """
    monkeypatch.setenv("ORION_CV_SHAPE_MIN_H", "5")
    monkeypatch.setenv("ORION_CV_COL_W_MIN", "4")
    loc = _fresh_locator()
    assert loc.shape_min_h_armed == 5.0
    assert loc.col_w_min_armed == 4.0


def test_anchor_refuse_mode_counts_refusals_by_default(ship_env):
    """[ORION_ANCHOR_REFUSE_MODE=1] The owner sees what the anchor refused before it goes quiet."""
    assert _fresh_locator().refuse_mode == 1


def test_tipless_armed_is_on_by_default(ship_env):
    """[ORION_CV_TIPLESS_ARMED] The one gate a real meter can fail outright."""
    assert _fresh_locator().tipless_armed is True


def test_locator_idle_reuse_is_off_by_default(ship_env):
    """[ORION_LOCATOR_IDLE_REUSE] In the OFF half of the ship line -- an idle-cost win with no

    graded live hours, and the 09-16 blind run was traced to exactly this class of unrated
    short-circuit. The code and its suite stay; only the default is off.
    """
    assert _fresh_locator()._idle_reuse is False


def test_locator_idle_reuse_env_override_still_arms_it(ship_env, monkeypatch):
    monkeypatch.setenv("ORION_LOCATOR_IDLE_REUSE", "1")
    assert _fresh_locator()._idle_reuse is True


def test_the_armed_floor_env_overrides_still_win(ship_env, monkeypatch):
    monkeypatch.setenv("ORION_CV_SHAPE_MIN_H_ARMED", "11")
    monkeypatch.setenv("ORION_CV_COL_W_MIN_ARMED", "9")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "0")
    monkeypatch.setenv("ORION_EXPECTATION_WINDOW", "0")
    monkeypatch.setenv("ORION_CV_TIPLESS_ARMED", "0")
    monkeypatch.setenv("ORION_ANCHOR_REFUSE_MODE", "0")
    loc = _fresh_locator()
    assert (loc.shape_min_h_armed, loc.col_w_min_armed) == (11.0, 9.0)
    assert loc.anchored_search is False
    assert loc.expect_window is False
    assert loc.tipless_armed is False
    assert loc.refuse_mode == 0


# --------------------------------------------------------------------------- 2. the reader
def _fresh_reader():
    from simple_meter_reader import SimpleMeterReader
    return SimpleMeterReader(1280, 720)


def test_ghost_forget_locator_is_on_and_rate_limited_by_default(ship_env):
    """[ORION_READER_GHOST_FORGET_LOCATOR + ORION_READER_FORGET_RATE_LIMIT]

    The forget is what stops a retired ghost seeding its own successor; the RATE LIMIT is what
    stops the forget itself wiping the real meter's promotion pair 10x a second (the 09-16
    14:20 blind run, six consecutive backstopped presses). Shipping one without the other is
    the configuration that caused that run, so both are pinned together.
    """
    r = _fresh_reader()
    assert r._ghost_forget_locator is True
    assert r._forget_rate_limit is True
    assert r._forget_zone_px == 64.0
    assert r._forget_defer_max == 12


def test_box_latch_and_static_zone_quarantine_are_on_by_default(ship_env):
    """[ORION_READER_BOX_LATCH / ORION_READER_STATIC_ZONE_QUARANTINE] Both graded ON."""
    r = _fresh_reader()
    assert r._box_latch is True
    assert r._static_zone_q is True


def test_fresh_after_ghost_is_off_by_default(ship_env):
    """[ORION_READER_FRESH_AFTER_GHOST] OFF on the ship line: GHOST_FORGET (rate-limited) is the

    layer that was graded; this one overlaps it and has no live hours of its own.
    """
    assert _fresh_reader()._fresh_after_ghost is False


def test_idle_publish_gate_is_off_by_default(ship_env):
    """[ORION_READER_IDLE_PUBLISH_GATE] OFF on the ship line -- one more publication layer that

    can refuse a real first read, which is the failure the 09-16 blind run was.
    """
    assert _fresh_reader()._idle_pub_gate is False


def test_the_reader_switches_still_honour_their_env(ship_env, monkeypatch):
    monkeypatch.setenv("ORION_READER_GHOST_FORGET_LOCATOR", "0")
    monkeypatch.setenv("ORION_READER_FORGET_RATE_LIMIT", "0")
    monkeypatch.setenv("ORION_READER_BOX_LATCH", "0")
    monkeypatch.setenv("ORION_READER_STATIC_ZONE_QUARANTINE", "0")
    monkeypatch.setenv("ORION_READER_FRESH_AFTER_GHOST", "1")
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "1")
    r = _fresh_reader()
    assert r._ghost_forget_locator is False
    assert r._forget_rate_limit is False
    assert r._box_latch is False
    assert r._static_zone_q is False
    assert r._fresh_after_ghost is True
    assert r._idle_pub_gate is True


def test_the_readers_tipless_press_window_publishes_by_default(ship_env):
    """[ORION_CV_TIPLESS_ARMED] The reader publishes the press window for the tipless path even

    with the anchor off, so the locator's WHEN survives an anchor kill switch.
    """
    from simple_meter_reader import SimpleMeterReader
    assert SimpleMeterReader._pa_arm_only() is True


# --------------------------------------------------------------------------- 3. banner verdict
class _RecordingLog:
    """Captures what the module said instead of what it returned.

    ``BannerVerdictLive.create`` returns None for the OFF switch AND for every fail-soft path
    (a missing template library, an unimportable grader), so the return value cannot tell the
    two apart. The log line can: OFF says "reader: OFF", every fail-soft path says "DISABLED".
    """

    def __init__(self):
        self.lines = []

    def _record(self, msg, *args, **kwargs):
        try:
            self.lines.append(str(msg) % args if args else str(msg))
        except Exception:
            self.lines.append(str(msg))

    info = warning = error = debug = exception = _record


def test_banner_verdict_live_is_on_by_default(ship_env):
    """[ORION_BANNER_VERDICT_LIVE] The live grader every lead number since 09-15 was fitted on.

    It shipped OFF on 09-14 after one bad session and was never flipped back in source -- only
    on the dev launch line, which no customer has. With it OFF a packaged install has no banner
    verdicts, so ``banner_lead_trim`` (default TRUE, and calibrated WITH the trim running) never
    receives an observation and the shipped lead cannot correct itself. The grader it needs DOES
    ship: ``tools/sidecar_bundle_manifest.py`` compiles ``tools/timing/panel_grade.py`` into
    OrionSidecar.exe and gates the build on ``tools/timing/panel_templates.npz``.
    """
    import banner_verdict_live as bvl
    log = _RecordingLog()
    bvl.BannerVerdictLive.create(env={}, log=log)
    assert not any("reader: OFF" in line for line in log.lines), log.lines


def test_banner_verdict_live_env_override_still_turns_it_off(ship_env):
    import banner_verdict_live as bvl
    log = _RecordingLog()
    assert bvl.BannerVerdictLive.create(env={"ORION_BANNER_VERDICT_LIVE": "0"}, log=log) is None
    assert any("reader: OFF" in line for line in log.lines), log.lines


# --------------------------------------------------------------------------- 4. shot records
def test_shot_records_are_on_by_default(ship_env, monkeypatch, tmp_path):
    """[ORION_SHOT_RECORDS] "Labels for free": one JSONL record per shot from ordinary play.

    The recorder refuses to arm under pytest unless the switch is set EXPLICITLY, so the
    production corpus can never be appended to by a test run. That guard is what this test has
    to step around to observe the SHIP default -- so it neutralises the guard (and only the
    guard) and then passes an environment with no ORION_* in it at all.
    """
    import shot_records
    monkeypatch.setattr(shot_records, "sys", types.SimpleNamespace(modules={}))
    rec = shot_records.ShotRecorder.create(
        session="session_ship_defaults",
        env={"ORION_SHOT_RECORDS_ROOT": str(tmp_path / "records")},
        start_thread=False,
    )
    assert rec is not None, "ORION_SHOT_RECORDS must default ON"
    rec.stop()


def test_shot_records_env_override_still_turns_them_off(ship_env, monkeypatch, tmp_path):
    import shot_records
    monkeypatch.setattr(shot_records, "sys", types.SimpleNamespace(modules={}))
    assert shot_records.ShotRecorder.create(
        session="session_ship_defaults",
        env={"ORION_SHOT_RECORDS": "0",
             "ORION_SHOT_RECORDS_ROOT": str(tmp_path / "records")},
        start_thread=False,
    ) is None


def test_the_pytest_guard_on_the_corpus_is_still_in_force(ship_env, tmp_path):
    """The guard above is load-bearing and must not be the thing that breaks silently."""
    import shot_records
    assert shot_records.ShotRecorder.create(
        session="session_ship_defaults",
        env={"ORION_SHOT_RECORDS_ROOT": str(tmp_path / "records")},
        start_thread=False,
    ) is None


# --------------------------------------------------------------------------- 5. the OFF half
def test_stall_attributor_is_off_by_default(ship_env):
    """[ORION_STALL_ATTRIB] A diagnostic that costs ~0.45 %% of the frame budget at 5 ms."""
    import stall_attributor
    assert stall_attributor.enabled() is False


def test_stall_attributor_env_override_still_arms_it(ship_env, monkeypatch):
    import stall_attributor
    monkeypatch.setenv("ORION_STALL_ATTRIB", "1")
    assert stall_attributor.enabled() is True


def test_framedump_has_no_default_at_all(ship_env):
    """[ORION_FRAMEDUMP*] A 1080p PNG costs 49 ms against a 16.7 ms frame budget.

    Read at the SOURCE rather than by importing the orchestrator: the gate is one expression in
    a 7,000-line module whose construction pulls in the whole capture stack, and what has to be
    true for a customer is simply that the expression carries no default. A default sneaking in
    here would arm a frame dump on every packaged install.
    """
    src = (REPO / "remote_play_orchestrator.py").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"_framedump_enabled\s*=\s*os\.environ\.get\(\s*'ORION_FRAMEDUMP'\s*,\s*''\s*\)",
                  src)
    assert m, "the ORION_FRAMEDUMP gate must stay `os.environ.get('ORION_FRAMEDUMP', '')`"
    # The press-window dump (60 fps, JPEG) is opt-in for the same reason.
    assert "ORION_FRAMEDUMP_PRESS_WINDOW" in src
    for key in ("ORION_FRAMEDUMP", "ORION_FRAMEDUMP_PRESS_WINDOW"):
        assert key not in os.environ


# --------------------------------------------------------------------------- 6. the launch path
def test_the_native_sidecar_launch_sets_none_of_these(ship_env):
    """The other half of the argument: nothing upstream puts these back.

    ``RemotePlaySession::startSidecar`` is the ONLY place the packaged app builds the sidecar's
    environment.  It applies ``applyShippedReaderProfile`` (SidecarReaderProfile.h), the meter
    proposer setting and the transport keys -- and none of the switches this file pins.  If a
    future change starts EXPORTING one of them from the native side, that becomes a SECOND
    source of truth for the same knob and this test says so.  Mentions in comments are fine and
    are expected (the sidecar's own knobs are documented where the pipe reads them); what is
    forbidden is a WRITE -- ``env.insert``, ``env.remove`` or ``qputenv`` on one of these keys.
    """
    cpp = [(REPO / "native_orion" / "src" / name).read_text(encoding="utf-8", errors="replace")
           for name in ("RemotePlaySession.cpp", "OrionAppController.cpp",
                        "SidecarReaderProfile.h")]
    for key in ("ORION_BANNER_VERDICT_LIVE", "ORION_PLAYER_ANCHOR", "ORION_ANCHORED_SEARCH",
                "ORION_ANCHOR_REFUSE_MODE", "ORION_EXPECTATION_WINDOW",
                "ORION_CV_SHAPE_MIN_H_ARMED", "ORION_CV_COL_W_MIN_ARMED",
                "ORION_READER_GHOST_FORGET_LOCATOR", "ORION_READER_FORGET_RATE_LIMIT",
                "ORION_READER_BOX_LATCH", "ORION_READER_STATIC_ZONE_QUARANTINE",
                "ORION_CV_TIPLESS_ARMED", "ORION_SHOT_RECORDS",
                "ORION_READER_FRESH_AFTER_GHOST", "ORION_READER_IDLE_PUBLISH_GATE",
                "ORION_LOCATOR_IDLE_REUSE", "ORION_STALL_ATTRIB"):
        writes = (rf'env\.(insert|remove)\(\s*QStringLiteral\(\s*"{key}"',
                  rf'qputenv\(\s*"{key}"',
                  rf'"{key}"\s*,\s*"')          # a kShippedReaderProfileValues row
        for src in cpp:
            for pattern in writes:
                assert not re.search(pattern, src), (
                    f"{key} is now written by the native launch path as well as defaulted in "
                    "Python -- one of the two has to be the source of truth "
                    "(see docs/SHIP_CONFIG.md)")
        profile = cpp[2]
        assert f'"{key}",' not in profile, (
            f"{key} joined kShippedReaderProfileFlags; it is a Python source default now")


def test_the_dev_launcher_can_still_override_everything(ship_env):
    """``run_orion.local.ps1`` must keep working unchanged: env still beats the source default.

    The launcher is gitignored, so this only asserts the property the launcher relies on -- that
    each switch is read from the environment on every resolution -- using the two that are read
    per call (the rest are covered by the explicit override tests above).
    """
    import player_anchor as pa
    import stall_attributor
    assert pa.enabled() is True
    assert stall_attributor.enabled() is False
    os.environ["ORION_PLAYER_ANCHOR"] = "0"
    os.environ["ORION_STALL_ATTRIB"] = "1"
    try:
        assert pa.enabled() is False
        assert stall_attributor.enabled() is True
    finally:
        os.environ.pop("ORION_PLAYER_ANCHOR", None)
        os.environ.pop("ORION_STALL_ATTRIB", None)


# --------------------------------------------------------------------------- 7. the C++ half
def test_the_two_band_defaults_moved_in_the_header_and_the_loader(ship_env):
    """[ORION_VISION_HOLD_BAND] The band is OFF for ship -- refuted live 09-16 21:00.

    The default is written TWICE on the native side (the header's member initialiser, and the
    per-key fallback in ``AppConfig::load``), and a fresh install uses the first while an
    upgraded one uses the second.  ``shipConfigDefaultsArePinned`` and
    ``shipConfigDefaultsSurviveAKeylessSettingsFile`` pin both from C++; this is the cheap
    cross-language guard so a Python-only test run still catches a header edit that forgot the
    loader.
    """
    header = (REPO / "native_orion" / "src" / "AppConfig.h").read_text(
        encoding="utf-8", errors="replace")
    assert re.search(r"double\s+visionHoldBandMs\s*=\s*0\.0\s*;", header)
    assert re.search(r"double\s+visionHoldBandFadeMs\s*=\s*0\.0\s*;", header)
    loader = (REPO / "native_orion" / "src" / "AppConfig.cpp").read_text(
        encoding="utf-8", errors="replace")
    assert re.search(r'cleanDouble\(obj,\s*"vision_hold_band_ms",\s*0\.0', loader)
    assert re.search(r'cleanDouble\(obj,\s*"vision_hold_band_fade_ms",\s*0\.0', loader)


def test_the_rest_of_the_native_ship_list_is_where_the_launch_line_left_it(ship_env):
    """The knobs the 09-16 sessions were graded with, pinned from the header's own text.

    The authoritative pin is the C++ test; this one exists so `pytest tests/test_ship_defaults.py`
    alone answers "is the packaged configuration still the graded one?" without a native build.
    """
    header = (REPO / "native_orion" / "src" / "AppConfig.h").read_text(
        encoding="utf-8", errors="replace")
    expected = {
        "sprintReleaseOnSquare": "false",          # refuted live 09-16 23:04; code kept
        "squarePressR2HoldMs": "0.0",              # 09-21 owner pass-through default; 50 ms is opt-in A/B only
        "bannerLeadTrim": "true",
        "bannerTrimTempoBuckets": "true",
        "leadAutoSeed": "true",
        "aimMarginMs": "69.0",                     # owner's slider 269 - fixed 200
        "leadFactoryPlaceholderMs": "269.0",
        "leadOffsetLeftFadeMs": "-6.0",            # [ORION_LEFT_FADE_LATER 2026-09-22]
        "leadOffsetRightFadeMs": "8.0",
        "meterBackstopGraceMs": "100.0",
        "meterBackstopGraceFadeMs": "220.0",
        # [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-17] flipped to 0 = collapse OFF: the
        # 09-17 dump showed it firing blind 19-39 ms before a LATE GATHER's own meter existed.
        "meterBackstopNeverSeenProbeMs": "0.0",
        "meterBackstopNeverSeenProbeFadeMs": "0.0",
        # [ORION_LEAD_OFFSET_FADE_MID 2026-09-17] the mid-range fade's own lead offset
        "leadOffsetFadeMidMs": "6.0",
        "bannerTrimRangeBuckets": "true",
        # [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] a panel with NO coverage cell (the 2-cell
        # TIMING | DISTANCE layout: no defender context) calibrates as an open shot. It was 98
        # of the 281 graded releases across the 09-18 sessions and all 98 were excluded.
        "bannerTrimAbsentCoverageOpen": "true",
        # [ORION_BANNER_TRIM_BIAS 2026-09-19] the net-vote integrator: a margin of N LATE over
        # EARLY inside the last 12 calibrating verdicts would buy one step. It SHIPS DISABLED at
        # 0 votes (the kill switch): the forensics that motivated it refuted its premise -- a
        # kernel fit on the clean onset band puts the LATE/EARLY crossing at hold 641 ms against a
        # population median of 646, so the lead is already within ~5 ms of optimum, and the 18:2
        # imbalance comes from a pickup stall and late-drawn meters, which no lead can fix. Left
        # armed it would spend the whole +-15 ms clamp turning EXCELLENTs into EARLIEs.
        # Re-arming is an owner decision backed by a measurement, never a default change.
        "bannerTrimBiasWindow": "12",
        "bannerTrimBiasVotes": "0",
        "tipFrameNative": "true",
        "inputTimedEnabled": "false",              # NO METER shelved
        # [SHIP_PARITY 2026-09-23] the owner's validated dev timing set (SHIP_PARITY_AUDIT.md R4-R8)
        "tipPhaseAnchorBase20": "true",
        "tipPhaseTypeTrimEnabled": "true",
        "ownershipProofTwoFrame": "true",
        "noMeterFadeTrimMs": "6.0",
        "tipPhaseAimFrozen": "true",
        "anchorRiseMinPct": "3.0",                 # two-frame pairing rule: never 4.0
    }
    for member, value in expected.items():
        pattern = (r"\b(?:bool|double|int)\s+" + re.escape(member) + r"\s*=\s*"
                   + re.escape(value) + r"\s*;")
        assert re.search(pattern, header), f"{member} is no longer pinned at {value}"


def test_no_meter_is_fenced_shut_not_merely_defaulted(ship_env):
    """[ORION_NO_METER_SHELVED] ``input_timed_enabled`` is forced false on BOTH routes.

    A default alone would let a restored backup or a hand edit resurrect a shelved mode, so the
    loader ANDs it with a process-wide fence whose shipped answer is false.
    """
    cpp = (REPO / "native_orion" / "src" / "AppConfig.cpp").read_text(
        encoding="utf-8", errors="replace")
    assert re.search(r"g_inputTimedAllowed\s*=\s*false", cpp)
    assert re.search(r"inputTimedAllowed\(\)\s*&&\s*cleanBool\(obj,\s*\"input_timed_enabled\",\s*false\)",
                     cpp)
