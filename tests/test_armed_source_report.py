"""Tests for tools/timing/armed_source_report.py (per-source shot attribution).

The synthetic fixture reproduces the REAL orion_native.log line shapes this
tool must survive: pre-armed_source outcome lines (attributed only through
their TIP RESERVATION promotion), 2026-08-08+ stamped outcome lines, the
stamped armed_source=none sentinel, an app restart (schedule_token/release_seq
regression), "Shot lead set to" config-drag marks, rejected_missed aborts in
the unschedulable-lead regime, and raw binary bytes mid-file (the live log is
not clean UTF-8).

Loaded by file path so the test does not depend on tools/ being an importable
package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "armed_source_report", ROOT / "tools" / "timing" / "armed_source_report.py")
asr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(asr)


def _promoted(ts, source, sigma, fill, eta, lead, epoch, attempt, token):
    return (f"{ts}  TIP RESERVATION: disposition=reservation_promoted source={source} "
            f"command_eta_ms={eta:.3f} lead_kind=factory lead_ms={lead:.3f} "
            f"predictor_sigma_ms={sigma:.3f} fill_pct={fill:.2f} physical_epoch={epoch} "
            f"shot_attempt={attempt} schedule_token={token} reservation_age_ms=96.430 "
            f"reservation_updates=22 first_fill=20.32")


def _outcome_old(ts, epoch, attempt, seq, verdict):
    return (f"{ts}  Outcome identity: physical_epoch={epoch} shot_attempt={attempt} "
            f"release_seq={seq} verdict={verdict}")


def _outcome_new(ts, epoch, attempt, seq, verdict, source, sigma, fill, eta, token):
    return (f"{ts}  Outcome identity: physical_epoch={epoch} shot_attempt={attempt} "
            f"release_seq={seq} verdict={verdict} grader_truth=0 armed_source={source} "
            f"armed_sigma_ms={sigma:.3f} armed_fill_pct={fill:.2f} "
            f"command_eta_ms={eta:.3f} armed_schedule_token={token}")


def _missed(ts, source, lead, disposition):
    return (f"{ts}  TIP DEADLINE DECISION: disposition=rejected_missed source={source} "
            f"tip_eta_ms=242.524 command_eta_ms=-57.476 lateness_ms=57.476 "
            f"lead_kind=factory lead_ms={lead:.3f} lead_sd_ms=6.000 "
            f"predictor_sigma_ms=15.101 fill_pct=40.69 frame_age_ms=13.30 "
            f"reservation_disposition={disposition} reservation_age_ms=0.000 "
            f"reservation_updates=1 reservation_ever_armable=0 reservation_promoted=0")


def _lead_mark(ts, lead):
    return (f"{ts}  Shot lead set to {lead} ms (your value; locked — "
            f"the learner will not move it)")


@pytest.fixture
def synthetic_log(tmp_path):
    """Two app runs in one file, exactly like the concatenated production log.

    Session 1 (sane lead 220.9): shot A joined via promotion, shot B stamped,
    shot C stamped armed_source=none (must NOT be joined through B's stale
    promotion identity), then a config drag to lead=320 and shot D released
    under it plus an unschedulable_lead abort.
    Session 2 (restart, token/seq regress to 1): one joined shot at lead 320.
    """
    lines = [
        _promoted("2026-08-08T03:18:08.044Z", "phase", 39.323, 34.50, 119.150,
                  220.900, 15, 1, 1),
        _outcome_old("2026-08-08T03:18:09.368Z", 15, 1, 1, "LATE"),
        _promoted("2026-08-08T03:20:13.731Z", "sampler", 31.810, 41.20, 27.900,
                  220.900, 16, 2, 7),
        _outcome_new("2026-08-08T03:20:15.052Z", 16, 2, 2, "EXCELLENT",
                     "sampler", 31.810, 41.20, 27.900, 7),
        # a promotion exists for (17,3) but the release says armed_source=none:
        # the engine's sentinel wins over the (possibly stale) promotion join.
        _promoted("2026-08-08T03:20:20.000Z", "sampler", 29.425, 22.00, 30.000,
                  220.900, 17, 3, 8),
        _outcome_new("2026-08-08T03:20:23.216Z", 17, 3, 3, "LATE",
                     "none", -1.0, -1.0, -1.0, 0),
        _lead_mark("2026-08-08T03:22:00.000Z", 320),
        _missed("2026-08-08T03:23:49.280Z", "phase", 320.0, "unschedulable_lead"),
        # released in the contaminated regime, unattributed: governing lead
        # must come from the config mark.
        _outcome_old("2026-08-08T03:24:11.115Z", 20, 6, 4, "LATE"),
        # --- app restart: schedule_token and release_seq regress to 1 ---
        _promoted("2026-08-08T05:39:27.816Z", "registration+sampler_far", 25.651,
                  30.10, 44.000, 320.000, 6, 1, 1),
        _outcome_old("2026-08-08T05:39:29.000Z", 6, 1, 1, "EARLY"),
    ]
    raw = "\n".join(lines).encode("utf-8")
    # the live log interleaves binary bytes; the reader must not crash on them
    raw += b"\n\x00\xff\xfe binary noise line \x80\x81\n"
    path = tmp_path / "orion_native.log"
    path.write_bytes(raw)
    return path


def test_load_survives_binary_and_parses_all_event_kinds(synthetic_log):
    events, marks = asr.load(str(synthetic_log))
    kinds = [ev["kind"] for ev in events]
    assert kinds.count("promoted") == 4
    assert kinds.count("outcome") == 5
    assert kinds.count("missed") == 1
    assert len(marks) == 1 and "Shot lead set to 320" in marks[0][2]


def test_sessions_split_on_token_and_seq_regression(synthetic_log):
    events, _ = asr.load(str(synthetic_log))
    sessions = asr.split_sessions(events)
    assert len(sessions) == 2
    assert sum(1 for ev in sessions[0] if ev["kind"] == "outcome") == 4
    assert sum(1 for ev in sessions[1] if ev["kind"] == "outcome") == 1


def test_attribution_stamped_beats_join_and_none_is_sentinel(synthetic_log):
    events, _ = asr.load(str(synthetic_log))
    session1 = asr.split_sessions(events)[0]
    shots, promos = asr.attribute(session1)
    assert len(shots) == 4 and len(promos) == 3

    joined, stamped, sentinel, contaminated = shots

    # pre-field outcome: attributed through its promotion, inheriting the
    # promoting decision's sigma/fill/eta/lead.
    assert joined["how"] == "via promotion join"
    assert joined["src"] == "phase"
    assert joined["sigma"] == pytest.approx(39.323)
    assert joined["fill"] == pytest.approx(34.50)
    assert joined["eta"] == pytest.approx(119.150)
    assert joined["lead"] == pytest.approx(220.900)

    # stamped outcome: the fired token's own values, not the promotion's.
    assert stamped["how"] == "stamped"
    assert stamped["src"] == "sampler"
    assert stamped["sigma"] == pytest.approx(31.810)
    assert stamped["armed_token"] == 7

    # armed_source=none is the engine saying "unattributed"; the tool must not
    # resurrect an attribution through the promotion join.
    assert sentinel["how"] == "unattributed"
    assert sentinel["src"] == "none"
    assert sentinel["sigma"] is None

    # unattributed release after the config drag: lead from the config mark.
    assert contaminated["how"] == "unattributed"
    assert contaminated["lead"] == pytest.approx(320.0)


def test_hand_count_parsing_accepts_all_documented_forms():
    assert asr.parse_hand("GGLX") == ["GOOD", "GOOD", "LATE", "SKIP"]
    assert asr.parse_hand("good early late skip") == ["GOOD", "EARLY", "LATE", "SKIP"]
    assert asr.parse_hand("G, e\nL") == ["GOOD", "EARLY", "LATE"]
    with pytest.raises(SystemExit):
        asr.parse_hand("garbage_token")


def test_wilson_interval_is_sane():
    lo, hi = asr.wilson(0, 0)
    assert (lo, hi) == (0.0, 0.0)
    lo, hi = asr.wilson(3, 7)
    assert 0.0 < lo < 3 / 7 < hi < 1.0


def test_main_end_to_end_discards_contaminated_lead_rows(synthetic_log, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "armed_source_report.py", str(synthetic_log), "--max-lead-ms", "299"])
    asr.main()
    out = capsys.readouterr().out
    # session 1: 4 released, 1 discarded (the lead-320 row);
    # session 2: 1 released, 1 discarded (joined promotion lead 320).
    assert "released shots with an outcome line: 3" in out
    assert out.count("CONTAMINATED-LEAD FILTER") == 2
    assert "discarded 1 released shot(s) whose governing lead exceeded 299 ms" in out
    assert "released shots with an outcome line: 0" in out
    # per-source rows for the kept shots survive the filter
    assert "phase" in out and "sampler" in out


def test_main_joins_hand_count_per_source(synthetic_log, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "armed_source_report.py", str(synthetic_log), "--session", "1",
        "--hand", "LGXL"])
    asr.main()
    out = capsys.readouterr().out
    assert "hand-counted miss rate per armed source" in out
    # phase: 1 counted shot, verdict LATE -> miss rate 100%
    assert "phase" in out
    # the sentinel/contaminated rows land under source "none" but stay joinable
    assert "none" in out


def test_hand_count_length_mismatch_refuses_to_guess(synthetic_log, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "armed_source_report.py", str(synthetic_log), "--session", "1",
        "--hand", "GG"])
    asr.main()
    out = capsys.readouterr().out
    assert "HAND COUNT NOT JOINED" in out
