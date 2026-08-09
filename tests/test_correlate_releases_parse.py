"""Parse-only unit test for tools/diagnostics/correlate_releases.py.

Confirms the new "Release attribution:" line is parsed and joins to its "Release issued:"
line by seq (the attribution is paired by seq, so the join must be exact). Pure text — no
video / OpenCV needed (parse_log never touches cv2). Loaded by file path so the test does
not depend on tools/ being an importable package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "correlate_releases", ROOT / "tools" / "diagnostics" / "correlate_releases.py")
correlate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(correlate)


# seq6-style: a no-green velocity release (predictive_target) that fired early.
_ISSUED_PRED = (
    "2026-06-05T06:27:41.782Z  Release issued: fill 72.0% target 99.6% (green 98.0-100.0) "
    "plan=Release scheduled reason=goto_release / Fill trajectory reaches target inside release "
    "lead (meter_full) code=predictive_target presence=accepted src=sidecar-fusion conf=0.82 "
    "age=3ms offset=24.0ms seq=6 shot=Go-To"
)
_ATTR_PRED = (
    "2026-06-05T06:27:41.782Z  Release attribution: seq=6 greenConfirmed=0 targetMode=meter_full "
    "greenWidth=0.0 greenConfirmFill=-1.0 greenConfirmMs=-1 vel=0.1850 crossingEta=-1 "
    "expectedRise=25.0 withinReach=1 shot=Go-To"
)

# seq4-style: a confirmed-green release (green_confirmed) that rode the tip to 100.
_ISSUED_GREEN = (
    "2026-06-05T06:27:30.585Z  Release issued: fill 100.0% target 99.2% (green 96.0-100.0) "
    "plan=Release scheduled reason=goto_release / Confirmed green-window crossing predicted "
    "(green_tip) code=green_confirmed presence=accepted src=sidecar-fusion conf=0.91 age=2ms "
    "offset=24.0ms seq=4 shot=Go-To"
)
_ATTR_GREEN = (
    "2026-06-05T06:27:30.585Z  Release attribution: seq=4 greenConfirmed=1 targetMode=green_tip "
    "greenWidth=4.0 greenConfirmFill=88.5 greenConfirmMs=120 vel=0.0900 crossingEta=0 "
    "expectedRise=22.0 withinReach=1 shot=Go-To"
)


def test_attribution_line_parses_and_joins_predictive(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(_ISSUED_PRED + "\n" + _ATTR_PRED + "\n", encoding="utf-8")

    releases, _submits_seq, _submits_list, _ownership_seq, attribution_seq, _attr_list = correlate.parse_log(log)

    assert len(releases) == 1
    assert releases[0]["seq"] == 6
    assert releases[0]["code"] == "predictive_target"

    # Joined by the SAME seq -> the velocity block is fully attributable.
    assert 6 in attribution_seq
    attr = attribution_seq[6]
    assert attr["green_confirmed"] == 0
    assert attr["target_mode"] == "meter_full"
    assert attr["green_confirm_fill"] == -1.0
    assert attr["green_confirm_ms"] == -1
    assert attr["crossing_eta"] == -1
    assert attr["within_reach"] == 1
    assert abs(attr["vel"] - 0.1850) < 1e-9


def test_attribution_line_parses_and_joins_green(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(_ISSUED_GREEN + "\n" + _ATTR_GREEN + "\n", encoding="utf-8")

    releases, _submits_seq, _submits_list, _ownership_seq, attribution_seq, _attr_list = correlate.parse_log(log)

    assert releases[0]["seq"] == 4
    assert releases[0]["code"] == "green_confirmed"
    attr = attribution_seq[4]
    assert attr["green_confirmed"] == 1
    assert attr["target_mode"] == "green_tip"
    assert attr["green_confirm_fill"] == 88.5
    assert attr["green_confirm_ms"] == 120
    assert attr["within_reach"] == 1


def test_pair_attribution_resolves_cross_session_seq_collision(tmp_path):
    # seq restarts at 1 each app launch; two sessions both log seq=6 with DIFFERENT fields.
    # A seq-only dict would last-win and staple the wrong attribution onto the earlier release;
    # pair_attribution must pick the one whose timestamp matches each release.
    s1_issued = (
        "2026-06-05T07:13:57.094Z  Release issued: fill 98.1% target 99.6% (green 98.1-100.0) "
        "plan=p reason=goto_release / x code=predictive_target presence=accepted src=sidecar-fusion "
        "conf=0.94 age=4ms offset=0.0ms seq=6 shot=Go-To"
    )
    s1_attr = (
        "2026-06-05T07:13:57.095Z  Release attribution: seq=6 greenConfirmed=0 targetMode=meter_full "
        "greenWidth=0.0 greenConfirmFill=-1.0 greenConfirmMs=-1 vel=0.5581 crossingEta=-2 "
        "expectedRise=25.0 withinReach=1 shot=Go-To"
    )
    s2_issued = (
        "2026-06-05T09:01:02.000Z  Release issued: fill 72.7% target 99.5% (green 98.9-100.0) "
        "plan=p reason=goto_release / x code=green_confirmed presence=accepted src=sidecar-fusion "
        "conf=0.85 age=5ms offset=0.0ms seq=6 shot=Go-To"
    )
    s2_attr = (
        "2026-06-05T09:01:02.001Z  Release attribution: seq=6 greenConfirmed=1 targetMode=green_tip "
        "greenWidth=1.1 greenConfirmFill=46.6 greenConfirmMs=1716 vel=0.3644 crossingEta=30 "
        "expectedRise=25.0 withinReach=1 shot=Go-To"
    )
    log = tmp_path / "orion_native.log"
    log.write_text("\n".join([s1_issued, s1_attr, s2_issued, s2_attr]) + "\n", encoding="utf-8")

    releases, _ss, _sl, _os, _aseq, attribution_list = correlate.parse_log(log)
    assert len(releases) == 2
    # The earlier release (seq=6, predictive) must pair to the meter_full attribution, NOT the
    # later session's green_tip — even though both are seq=6.
    early = next(r for r in releases if r["code"] == "predictive_target")
    late = next(r for r in releases if r["code"] == "green_confirmed")
    assert correlate.pair_attribution(early, attribution_list)["target_mode"] == "meter_full"
    assert correlate.pair_attribution(late, attribution_list)["target_mode"] == "green_tip"


def test_release_submit_unknown_square_is_preserved_and_seq_paired(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(
        _ISSUED_GREEN
        + "\n2026-06-05T06:27:30.586Z  Release submit: backend=PIPE "
          "square_bit=-1 delivery_stage=local_udp_accepted ok=1 seq=4\n",
        encoding="utf-8",
    )

    releases, submits_seq, submits_list, *_rest = correlate.parse_log(log)

    assert submits_seq[4]["square"] == "unknown"
    assert submits_list[0]["seq"] == 4
    assert correlate.pair_submit(releases[0], submits_seq, submits_list) == (
        "ok=1 sq=unknown",
        "seq",
    )


def test_release_submit_known_square_parses_regardless_of_field_order(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(
        _ISSUED_PRED
        + "\n2026-06-05T06:27:41.783Z  Release submit: square_bit=0 "
          "packet_snapshot=precise_pipe_mailbox seq=6 ok=1 backend=PIPE\n",
        encoding="utf-8",
    )

    releases, submits_seq, submits_list, *_rest = correlate.parse_log(log)

    assert submits_seq[6]["square"] == "0"
    assert correlate.pair_submit(releases[0], submits_seq, submits_list) == (
        "ok=1 sq=0",
        "seq",
    )


# --- Release freshness: (blind-fire verdict + memory-trust A/B census) -----------------------
_FRESH_GREEN = (
    "2026-06-05T06:27:30.585Z  Release freshness: seq=4 blindFire=0 code=green_confirmed "
    "lastFresh=1 fresh=7 staleMs=12 memTrusted=3 shot=Go-To"
)
_ISSUED_BLIND = (
    "2026-06-05T06:30:00.000Z  Release issued: fill 88.0% target 96.0% (green 94.0-100.0) "
    "plan=Release scheduled reason=feedforward release (meter invisible) code=feedforward_target "
    "presence=stale src=none conf=0.00 age=120ms offset=0.0ms seq=9 shot=Left Fade"
)
_FRESH_BLIND = (
    "2026-06-05T06:30:00.001Z  Release freshness: seq=9 blindFire=1 code=feedforward_target "
    "lastFresh=0 fresh=0 staleMs=180 memTrusted=0 shot=Left Fade"
)


def test_freshness_line_attaches_with_memtrust_census(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(_ISSUED_GREEN + "\n" + _FRESH_GREEN + "\n", encoding="utf-8")
    releases, *_rest = correlate.parse_log(log)
    assert len(releases) == 1
    fr = releases[0]["freshness"]
    assert fr is not None
    assert fr["blind_fire"] == 0
    assert fr["last_fresh"] == 1
    assert fr["fresh"] == 7
    assert fr["stale_ms"] == 12
    assert fr["mem_trusted"] == 3          # the memory-trust A/B census field
    assert fr["code"] == "green_confirmed"


def test_freshness_blindfire_attaches(tmp_path):
    log = tmp_path / "orion_native.log"
    log.write_text(_ISSUED_BLIND + "\n" + _FRESH_BLIND + "\n", encoding="utf-8")
    releases, *_rest = correlate.parse_log(log)
    assert releases[0]["seq"] == 9
    fr = releases[0]["freshness"]
    assert fr["blind_fire"] == 1           # fired on the feedforward clock, vision stale
    assert fr["code"] == "feedforward_target"
    assert fr["stale_ms"] == 180
    assert fr["mem_trusted"] == 0


def test_pair_freshness_resolves_cross_session_seq_collision(tmp_path):
    # seq restarts at 1 each launch; two sessions both log seq=6 freshness with OPPOSITE verdicts.
    # The timestamp-aware pairing must staple each verdict onto the right session's release.
    issued_a = (
        "2026-06-05T07:13:57.094Z  Release issued: fill 98.1% target 99.6% (green 98.1-100.0) "
        "plan=p reason=x code=green_confirmed presence=accepted src=sidecar-fusion conf=0.94 "
        "age=4ms offset=0.0ms seq=6 shot=Go-To"
    )
    fresh_a = (
        "2026-06-05T07:13:57.095Z  Release freshness: seq=6 blindFire=0 code=green_confirmed "
        "lastFresh=1 fresh=9 staleMs=8 memTrusted=0 shot=Go-To"
    )
    issued_b = (
        "2026-06-05T09:01:02.000Z  Release issued: fill 70.0% target 96.0% (green 94.0-100.0) "
        "plan=p reason=x code=feedforward_target presence=stale src=none conf=0.00 "
        "age=120ms offset=0.0ms seq=6 shot=Go-To"
    )
    fresh_b = (
        "2026-06-05T09:01:02.001Z  Release freshness: seq=6 blindFire=1 code=feedforward_target "
        "lastFresh=0 fresh=0 staleMs=200 memTrusted=0 shot=Go-To"
    )
    log = tmp_path / "orion_native.log"
    log.write_text("\n".join([issued_a, fresh_a, issued_b, fresh_b]) + "\n", encoding="utf-8")
    releases, *_rest = correlate.parse_log(log)
    assert len(releases) == 2
    early = next(r for r in releases if r["code"] == "green_confirmed")
    late = next(r for r in releases if r["code"] == "feedforward_target")
    assert early["freshness"]["blind_fire"] == 0
    assert late["freshness"]["blind_fire"] == 1


def test_summarize_freshness_aggregates():
    releases = [
        {"seq": 1, "code": "green_confirmed",
         "freshness": {"blind_fire": 0, "code": "green_confirmed", "last_fresh": 1,
                       "fresh": 7, "stale_ms": 10, "mem_trusted": 2}},
        {"seq": 2, "code": "feedforward_target",
         "freshness": {"blind_fire": 1, "code": "feedforward_target", "last_fresh": 0,
                       "fresh": 0, "stale_ms": 180, "mem_trusted": 0}},
        {"seq": 3, "code": "predictive_target", "freshness": None},  # pre-freshness / unpaired
    ]
    text = "\n".join(correlate.summarize_freshness(releases))
    assert "2 releases with a verdict" in text     # the None one is skipped
    assert "1/2" in text                            # one blind fire of two verdicts
    assert "180" in text                            # blind staleMs surfaced
    assert "total=2" in text                        # memTrusted summed across releases


def test_summarize_freshness_empty_on_pre_freshness_log():
    # A log with no "Release freshness:" lines (older build) yields no summary, not a crash.
    assert correlate.summarize_freshness([{"seq": 1, "freshness": None}]) == []
