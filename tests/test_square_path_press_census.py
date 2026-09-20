"""Square-path press census invariants.

[SQUARE PATH AUDIT 2026-09-19] The Square (shoot button) path already emits enough
forensics to decide, for EVERY physical Square press, what happened to it. This test
pins the accounting so a future change cannot re-open a silent terminal.

THE INVARIANTS, in the order they are asserted:

  I1  Every ``Physical shot epoch: ... intent=square_edge`` line is answered by exactly
      one terminal within its session:
        * ``Release delivery identity: physical_epoch=N``      -> the engine owned it
        * ``SHOT NOT OWNED: reason=press_unanswered_no_meter`` -> pass-through, no meter
        * ``SHOT NOT OWNED: reason=ownership_proof_incomplete``/``_structure_stamp_missing``
        * ``PRESS UNDELIVERABLE: epoch=N``                     -> no live input route
      Zero unaccounted presses.  (2026-09-18/19 logs: 582 presses, 0 unaccounted.)

  I2  Every square press gets a ``Square-down delivery identity`` line naming the SAME
      epoch.  The app must never lose a press between the epoch mint and the route.

  I3  ``Square-up route audit`` phases alternate raw_up -> debounced_up.  A second
      raw_up with no debounced_up between them means the physical button was read as
      HELD again inside the up debounce, so the engine merged two presses into one
      console hold (the "stuck Square" phenotype).  Bounded, not banned: the test
      reports the count and fails only above ``MAX_MERGED_PRESS_RATE``.

  I4  At most one ``PRESS ANALOG TRACE`` per (session, epoch) carries >= 4 filled
      ladder slots.  A second substantive trace under one epoch is a physical press
      the pad layer saw and the epoch minter refused -- a press swallowed by the app.

  I5  ``SHOT NOT OWNED: ... backstop=user_released`` must not be emitted for a press
      whose blind deadline had already passed, which the engine itself reports as
      ``METER VISION WAIT: epoch=N ... blind_release_suppressed=1``.  The two lines
      contradict each other: one says the player let go early, the other says the
      deadline fired and was suppressed.  This is the diagnostic-integrity bug the
      2026-09-19 audit proved (``meterBlindBackstopBlockReason`` does not consider
      ``green_window_priority``), and it is asserted as an EXPECTED-FAIL marker so the
      test turns green the moment the reason token learns the word.

The parser is deliberately regex-only and depends on no product code, so it keeps
working against a returned production log.  It runs against a checked-in fixture
always, and additionally against ``logs/orion_native.log*`` when those exist.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# .logtxt, not .log: the repo .gitignore has a global `*.log` rule, and a fixture that
# cannot be committed is not a fixture.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "square_path_census.logtxt"

# A merged press costs the player a shot, so the bar is low but not zero: one
# bounce in the 2026-09-18/19 corpus (1 / 582 = 0.17 %).
MAX_MERGED_PRESS_RATE = 0.01
# Same corpus: one swallowed re-press (1 / 582).
MAX_SWALLOWED_PRESS_RATE = 0.01

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+(.*)$")
_SESSION_MARK = "Packet bridge service not installed"
_KV = re.compile(r"([A-Za-z_0-9]+)=([^\s]+)")


def _kv(body: str) -> dict[str, str]:
    return dict(_KV.findall(body))


@dataclass
class Press:
    session: int
    epoch: int
    ts: str
    line: int
    down_delivered: bool = False
    owned: bool = False
    not_owned_reason: str | None = None
    backstop: str | None = None
    hold_ms: float | None = None
    undeliverable: bool = False
    vision_wait: bool = False
    analog_traces: list[int] = field(default_factory=list)

    @property
    def terminals(self) -> list[str]:
        out: list[str] = []
        if self.owned:
            out.append("owned")
        if self.not_owned_reason:
            out.append(self.not_owned_reason)
        if self.undeliverable:
            out.append("undeliverable")
        return out


@dataclass
class Census:
    presses: dict[tuple[int, int], Press] = field(default_factory=dict)
    merged_presses: list[Press] = field(default_factory=list)
    up_phase_anomalies: list[tuple[int, str, str]] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


def parse(text: str) -> Census:
    census = Census()
    session = 0
    prev_up_phase: str | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        matched = _TS.match(raw)
        if not matched:
            continue
        ts, body = matched.group(1), matched.group(2)

        if _SESSION_MARK in body:
            session += 1
            prev_up_phase = None
            continue

        if body.startswith("Physical shot epoch:"):
            fields = _kv(body)
            if fields.get("intent") != "square_edge":
                continue
            epoch = int(fields["epoch"])
            census.presses[(session, epoch)] = Press(session, epoch, ts, lineno)
            census.counts["press"] += 1

        elif body.startswith("Square-down delivery identity:"):
            fields = _kv(body)
            press = census.presses.get((session, int(fields["physical_epoch"])))
            if press is not None:
                press.down_delivered = True

        elif body.startswith("Release delivery identity:"):
            fields = _kv(body)
            press = census.presses.get((session, int(fields["physical_epoch"])))
            if press is not None:
                press.owned = True

        elif body.startswith("PRESS UNDELIVERABLE:"):
            fields = _kv(body)
            press = census.presses.get((session, int(fields["epoch"])))
            if press is not None:
                press.undeliverable = True

        elif body.startswith("METER VISION WAIT:"):
            fields = _kv(body)
            press = census.presses.get((session, int(fields["epoch"])))
            if press is not None:
                press.vision_wait = True

        elif body.startswith("SHOT NOT OWNED:"):
            fields = _kv(body)
            epoch = int(fields.get("physical_epoch", -1))
            press = census.presses.get((session, epoch))
            if press is None:
                census.counts["not_owned_orphan"] += 1
                continue
            if press.not_owned_reason is None:
                press.not_owned_reason = fields.get("reason", "?")
                press.backstop = fields.get("backstop")
                if "hold_ms" in fields:
                    press.hold_ms = float(fields["hold_ms"])
            else:
                census.counts["not_owned_repeat"] += 1

        elif body.startswith("PRESS ANALOG TRACE:"):
            fields = _kv(body)
            press = census.presses.get((session, int(fields["epoch"])))
            if press is None:
                continue
            ladder = re.search(r"r2=\[([^\]]*)\]", body)
            filled = 0 if ladder is None else sum(
                1 for slot in ladder.group(1).split(",") if slot != "-"
            )
            press.analog_traces.append(filled)

        elif body.startswith("Square-up route audit:"):
            fields = _kv(body)
            phase = fields.get("phase", "?")
            if phase == prev_up_phase:
                census.up_phase_anomalies.append((lineno, phase, ts))
                if phase == "raw_up":
                    press = census.presses.get(
                        (session, int(fields.get("latest_physical_epoch", -1)))
                    )
                    if press is not None:
                        census.merged_presses.append(press)
            prev_up_phase = phase

    return census


def _sources() -> list[tuple[str, str]]:
    out = [("fixture", FIXTURE.read_text(encoding="utf-8"))]
    for name in ("orion_native.log.1", "orion_native.log"):
        live = REPO_ROOT / "logs" / name
        if live.exists():
            out.append((name, live.read_text(encoding="utf-8", errors="replace")))
    return out


@pytest.fixture(scope="module", params=_sources(), ids=lambda s: s[0])
def census(request) -> Census:
    return parse(request.param[1])


def test_i1_every_press_has_exactly_one_terminal(census: Census) -> None:
    """No square press may end with no decision, and none with two."""
    unaccounted = [p for p in census.presses.values() if not p.terminals]
    doubled = [p for p in census.presses.values() if len(p.terminals) > 1]
    assert census.presses, "fixture parsed no square presses"
    assert not unaccounted, (
        "square presses ended with NO terminal line: "
        + ", ".join(f"s{p.session}/e{p.epoch}@{p.ts}" for p in unaccounted[:10])
    )
    assert not doubled, (
        "square presses were answered twice: "
        + ", ".join(
            f"s{p.session}/e{p.epoch}@{p.ts} -> {p.terminals}" for p in doubled[:10]
        )
    )


def test_i2_every_press_reaches_the_route_layer(census: Census) -> None:
    """A press must always produce a Square-down delivery identity for its own epoch."""
    missing = [p for p in census.presses.values() if not p.down_delivered]
    assert not missing, (
        "square presses with no Square-down delivery identity: "
        + ", ".join(f"s{p.session}/e{p.epoch}@{p.ts}" for p in missing[:10])
    )


def test_i3_up_phases_alternate(census: Census) -> None:
    """raw_up -> debounced_up, never raw_up -> raw_up above the tolerated rate.

    A repeated raw_up means the physical Square read HELD again inside the three-poll
    up debounce, so the two presses were merged into one console hold.
    """
    total = max(1, len(census.presses))
    rate = len(census.merged_presses) / total
    assert rate <= MAX_MERGED_PRESS_RATE, (
        f"merged-press rate {rate:.4f} exceeds {MAX_MERGED_PRESS_RATE}: "
        + ", ".join(
            f"s{p.session}/e{p.epoch}@{p.ts}" for p in census.merged_presses[:10]
        )
    )


def test_i4_no_press_is_swallowed_by_the_app(census: Census) -> None:
    """A second substantive analog trace under one epoch = a press that got no epoch."""
    swallowed = [
        p
        for p in census.presses.values()
        if sum(1 for filled in p.analog_traces if filled >= 4) > 1
    ]
    total = max(1, len(census.presses))
    rate = len(swallowed) / total
    assert rate <= MAX_SWALLOWED_PRESS_RATE, (
        f"swallowed-press rate {rate:.4f} exceeds {MAX_SWALLOWED_PRESS_RATE}: "
        + ", ".join(f"s{p.session}/e{p.epoch}@{p.ts}" for p in swallowed[:10])
    )


@pytest.mark.xfail(
    reason=(
        "[SQUARE PATH AUDIT 2026-09-19] AutomationEngine::meterBlindBackstopBlockReason "
        "(AutomationEngine.cpp:20892) does not test config_.greenWindowPriority, which is "
        "what actually suppresses the backstop at AutomationEngine.cpp:21356. So a press "
        "whose blind deadline passed and was suppressed still reports "
        "backstop=user_released. Flip to strict once the reason token learns "
        "green_window_priority."
    ),
    strict=False,
)
def test_i5_backstop_token_does_not_contradict_the_vision_wait(census: Census) -> None:
    """`backstop=user_released` may not describe a press whose deadline demonstrably fired."""
    liars = [
        p
        for p in census.presses.values()
        if p.vision_wait and p.backstop == "user_released"
    ]
    assert not liars, (
        "presses reported as backstop=user_released although METER VISION WAIT proves the "
        "blind deadline passed and was suppressed: "
        + ", ".join(
            f"s{p.session}/e{p.epoch}@{p.ts} hold={p.hold_ms}" for p in liars[:10]
        )
    )


def test_detectors_fire_on_the_known_merged_press() -> None:
    """The 2026-09-18 03:54:13Z epoch-187 window, verbatim from logs/orion_native.log.1.

    Ground truth, from the lines in the fixture:
      * epoch 187 pressed at 13.224Z, raw_up at 13.337Z with ``requested_square=1``
      * a SECOND ``PRESS ANALOG TRACE: epoch=187`` at 13.513Z with seven filled ladder
        slots -- a second physical Square-down edge, ~160 ms long, that the epoch minter
        refused because the three-clean-UP-poll debounce had not completed
      * a SECOND ``raw_up`` at 13.513Z, still ``requested_square=1``
      * ``debounced_up`` only at 13.521Z -- so the console was told Square-DOWN for
        ~184 ms after the player let go, and the two presses reached the game as one hold

    This pins both detectors against real evidence, so I3/I4 cannot silently stop working.
    """
    merged = Path(__file__).resolve().parent / "fixtures" / "square_path_merged_press.logtxt"
    census = parse(merged.read_text(encoding="utf-8"))

    assert len(census.presses) == 2, "fixture should carry epochs 186 and 187"
    epochs = {epoch for (_session, epoch) in census.presses}
    assert epochs == {186, 187}

    # I3: the repeated raw_up is seen, and attributed to epoch 187.
    assert [p.epoch for p in census.merged_presses] == [187]

    # I4: epoch 187 carries two substantive analog traces; epoch 186 carries one.
    traces_187 = census.presses[(1, 187)].analog_traces
    traces_186 = census.presses[(1, 186)].analog_traces
    assert sum(1 for filled in traces_187 if filled >= 4) == 2, traces_187
    assert sum(1 for filled in traces_186 if filled >= 4) == 1, traces_186

    # Both presses still reach a terminal -- the defect is the MERGE, not a lost decision.
    assert census.presses[(1, 186)].not_owned_reason == "press_unanswered_no_meter"
    assert census.presses[(1, 187)].not_owned_reason == "press_unanswered_no_meter"


def test_detectors_stay_quiet_on_a_clean_press() -> None:
    """A press with the ordinary raw_up -> debounced_up pair trips neither detector."""
    clean = "\n".join(
        [
            "2026-09-18T18:29:30.000Z  Packet bridge service not installed: tried [VeniceNetSvc]. Starting local sniff-only debug mode.",
            "2026-09-18T18:29:44.185Z  Physical shot epoch: epoch=22 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(19,-127) rs=(0,0) l2=0 r2=255 sprint_released=0",
            "2026-09-18T18:29:44.186Z  Square-down delivery identity: physical_epoch=22 shot_attempt=0 square_bit=1 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0",
            "2026-09-18T18:29:44.388Z  PRESS ANALOG TRACE: epoch=22 r2=[255,255,255,255,255,255,255,255] ls=[129,129,128,128,128,127,127,128] rs=[0,0,0,0,0,0,0,0] r2_release_edge_ms=none",
            "2026-09-18T18:29:45.135Z  Release delivery identity: physical_epoch=22 shot_attempt=19 release_seq=19 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0",
            "2026-09-18T18:29:45.840Z  Square-up route audit: latest_physical_epoch=22 phase=raw_up physical_square=0 requested_square=0 pipe_snapshot_square=0",
            "2026-09-18T18:29:45.849Z  Square-up route audit: latest_physical_epoch=22 phase=debounced_up physical_square=0 requested_square=0 pipe_snapshot_square=0",
        ]
    )
    census = parse(clean)
    assert len(census.presses) == 1
    assert census.merged_presses == []
    assert census.up_phase_anomalies == []
    press = census.presses[(1, 22)]
    assert press.terminals == ["owned"]
    assert sum(1 for filled in press.analog_traces if filled >= 4) == 1


def test_census_shape_is_reported(census: Census, capsys) -> None:
    """Not an assertion about behaviour -- it prints the census so `-s` is a report."""
    buckets: Counter = Counter()
    for press in census.presses.values():
        buckets[press.terminals[0] if press.terminals else "unaccounted"] += 1
    holds = [p.hold_ms for p in census.presses.values() if p.hold_ms is not None]
    with capsys.disabled():
        print(f"\n  square presses: {len(census.presses)}")
        for name, count in buckets.most_common():
            print(f"    {name:<34} {count}")
        if holds:
            short = sum(1 for h in holds if h < 300)
            print(f"    unanswered holds < 300 ms          {short} / {len(holds)}")
            print(f"    unanswered holds >= 450 ms         "
                  f"{sum(1 for h in holds if h >= 450)} / {len(holds)}")
    assert True
