# For Astra: Pill (beta) re-enabled pending one live session (2026-09-22)

Hi Astra, a heads-up because Pill touches your reader lane. **I (Claude) did not edit the reader files.** Codex notes that shared reader code has changed since 09-19 (the green-cap selector now prefers the highest eligible component; quarantine and detector-fault handling changed); `pill_fill_ruler.py` and the Pill branch are byte-identical to 09-19.

## What changed (Claude, on the owner's word)

Pill (beta) was withdrawn on 09-21 by removing the OPTION only, so the engine stayed dormant. It is offered again for one pre-registered live test.

- `native_orion/src/AppConfig.h` `normalizedMeterStyle()` accepts `"pill"` again. That one shared rule covers load, save, `setMeterStyle` and profile switch.
- `native_orion/qml/components/MeterConfigPanel.qml`: the menu offers `["Arrow2", "Pill (beta)"]`. The caption reads "Arrow2 is the recommended… Pill (beta) is newer and less tested."
- `tools/release_filter_policy.py`: the `"pill.json"` denylist entry you added on 09-21 is **removed**, so the customer package runs exactly the Pill profile the session tests. The hygiene test is inverted to match.
- Tests updated: `RemotePlayExecutablePolicyTests.cpp` (a persisted Pill now loads as Pill and routes to the yolo proposer), `tests/test_venice_ui_contract.py`, `tests/test_release_filter_hygiene.py`.
- Unchanged and already in place: `applyPillYoloRoute` (Pill → `ORION_METER_PROPOSER=yolo`, `ORION_METER_STYLE=pill`), the landmark ruler `pill_fill_ruler.py` (your 09-19 work), the reader's Pill branch (`simple_meter_reader.py` ~9000), and `--include-module=pill_fill_ruler` in `scripts/build_orion_sidecar.ps1`. That last item closes item 1 of `docs/PILL_STYLE_STATUS.md` §7.6.

**Verification:**
- Native: builds; ctest 29/30 or 30/30. `OrionShmInteropTests::abandonedPartialPublishIsDroppedThenCleanWriterRecoversAcrossProcesses` fails intermittently. That code dates from 08-02/09-21, was untouched today, and passes standalone; treat it as a pre-existing flaky test.
- Python: the 45 files mentioning Pill or meter style give 1061 passed.

## The live test (pre-registered before the data)

The owner runs one session: switch the in-game meter to Pill, then take about 50 open standstills plus about 10 fades, with framedump on. It runs in the same session as the Batch 1 input check.

- **Ship "Pill (beta)"** if open standstills green **≥ 60 %** (Arrow2 runs ~70–76 %) and there are no misfires or false fires.
- **Otherwise re-withdraw:** delete the `"pill"` line in `normalizedMeterStyle`, restore `"pill.json"` (lowercase) to the denylist, set the QML option back to Arrow2 only, and revert the three tests.
- **Watch:**
  - the Pill Shot Lead should move back up toward the Arrow2 value (~269), since the ruler moves the 20 % anchor ~17–23 ms later (a projection, never measured live);
  - `cap_ok = 0` frames (§7.6.3);
  - whether the per-press lock/carry path, which so far has only been unit-tested, behaves on real presses.

## What we'd value from you

A look at the live Pill session's reader behaviour, particularly the ruler latch and carry across presses. And tell us if anything in the reader's Pill branch has changed since 09-19 that the session should account for.

## Live result (2026-09-23 01:23Z, `session_20260922_202346`)

- **Route:** the Pill detector loaded after a reconnect (`METER STYLE: Pill -> proposer=yolo`). The first attempt had switched style mid-session, and the sidecar stayed on the CV locator, which sees nothing on Pill. That is now fixed: a Pill↔other switch mid-session restarts detection.
- **Results:**

| Shot type | n | EXCELLENT | EARLY | LATE | Other |
|---|---:|---:|---:|---:|---|
| Standstill | 20 | 12 (60 %) | 2 | 5 | 1 no-banner |
| Right Fade | 9 | 6 | 1 | 0 | 2 no-banner |
| Left Fade | 3 | 0 | 2 | 0 | 1 no-banner |

- **Verdict:** the ≥60 % rule passes narrowly on 20 of the planned ~50 shots. It stays "Pill (beta)". The owner's bar is "if we get it as consistent as Arrow2 it can stay", and he asks for a **Pill meter detection upgrade**.
- **No framedump.** C: had < 4.65 GB free, and D: fails the 250 ms first-write guard. So the ruler latch/carry, `cap_ok`, and box-ruler fallback on this run cannot be checked (Codex's point).
- **Pickup fields are empty** (`first_sight_*` = None) on every Pill record, so pickup timing on Pill is currently invisible in the shot records.

**Asks for the upgrade:**
1. Pickup logging on the Pill route.
2. A framedump Pill session once C: has room.
3. The fill-noise / ruler work from `docs/PILL_STYLE_STATUS.md` §7.6.

## RE-WITHDRAWN 2026-09-23 (owner)

"Pill is beta, it performed terribly in a real game, so we can leave it out."
- Everything in "What changed" above is reverted:
  - `normalizedMeterStyle` rejects "pill";
  - the QML offers Arrow2 only;
  - "pill.json" is back on the denylist;
  - the three tests are back to the withdrawn contract.
- The mid-session style-switch sidecar restart was also removed: it dropped the console session for ~14 s (01:55:23 → 01:55:37).
- Your ruler, the reader's Pill branch and the yolo route stay dormant, exactly as on 09-21.
- Pill detection work is a **post-launch** item (see `docs/ROADMAP_POST_LAUNCH_2026-09.md`, item 1). Nothing is needed from you for launch.
