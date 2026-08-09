# Regression Gates -- detection + timing quality lock

This is the **offline** guard that locks in the detection + timing quality achieved on the current
`HEAD`. It replays the recorded live framedumps through the **real** production reader and asserts
metric floors, and it points at the native timing suite that pins the release-clock invariants. Any
future change that silently regresses either surface **FAILS** a gate -- no live rig required.

Status on committed HEAD: **detection 46/46 gates PASS**, **native timing 194 passed / 0 failed**.

## One command

```bash
# Detection gates (Python replay) + native-timing summary:
C:/Python314/python.exe tools/regression/run_gates.py

# Same detection gates as part of the pytest suite (skips sessions whose dump is absent):
C:/Python314/python.exe -m pytest tests/test_detection_regression.py -v

# Native timing gates (QtTest): build once, then run
cmake --build native_orion/build --target OrionNativeTests --config Release
#   add C:\Qt\6.8.0\msvc2022_64\bin to PATH, then:
native_orion/build/Release/OrionNativeTests.exe
```

`run_gates.py` also invokes the built `OrionNativeTests.exe` and folds its totals into the report
(it forces QtTest's `-o file,txt` output because QtTest routes to OutputDebugString when no console
is attached). Whole run is a few minutes.

---

## How the detection gates work

`tools/regression/replay_gates.py` is the shared core (used by both the CLI and pytest). For each
session it:

1. **Locates the shot window** -- a full 2 MB-PNG replay of every session is ~30 min, and a blind
   frame prefix misses the shots (they often start 700-2000 frames in, after warmup). A cheap coarse
   scan (decode every 8th frame) finds the first strong detection; the gate then measures a bounded
   dense window (`ORION_GATE_WINDOW`, default **900 frames**) anchored ~40 frames before it.
   Deterministic (same frames every run), always contains shots, ~1 min/session.
2. **Finds shots** on an UNARMED pass -- a run of >=5 detected frames, expanded over the surrounding
   detected span (gap <=3), requiring a peak fill >=70%. (Same consensus logic as the diagnostics
   head-to-head harness, but self-referential to the production reader -- no YOLO/chain needed.)
3. **Re-runs ARMED** -- `set_shot_state(True)` inside every shot span. This is the **production
   condition**: the bot KNOWS it is shooting (held trigger + v9 pose), so this is the pass all gate
   metrics are measured on.
4. Computes the metrics below and compares each to its committed floor in
   `tools/regression/gate_floors.json` (a safety margin below the measured HEAD value).

Framedumps live under `logs/diagnostics/framedump/`. A session whose dump is absent is **skipped**
(pytest `skip`, CLI `SKIP`) so CI without the multi-GB dumps still runs. Where a serving-chain cache
(`logs/diagnostics/simple_vs_chain/chaincache_*.jsonl`) exists, an extra fill-vs-chain gate is added
(only `session_20260704_210801` has one). Set `ORION_GATE_WINDOW=0` for an exhaustive full-session
replay (then re-measure the floors on the full run before tightening).

### Detection gate catalogue

| Gate | Metric | Direction | Protects |
|------|--------|-----------|----------|
| `n_shots` | shot spans found in the window | `>=` floor | detector didn't go dark (guards a degenerate "0 shots -> vacuous pass") |
| `within_shot_detect_rate` | armed detected / in-shot frames | `>=` floor | within-shot detection stays high |
| `within_shot_maxgap` | longest consecutive miss inside a shot | `<=` floor | **no within-shot disappearance** through rise->cap->deflate |
| `offshot_falselock_episodes` | off-shot runs that are detached **and** sustained **and** static | `<=` 0 | **0 decor false-locks** (City kiosk / 2K logo / banners) |
| `top_clips` | frames where `bbox.top > red_top_row` | `<=` 0 | **tip always captured** -- box never clips the arrow-tip apex (commit 4464f4a9) |
| `mid_rise_glitches` | >20pp backward fill drops before peak | `<=` 0 | monotone rise; no fill snap-back |
| `median_shot_peak` | median per-shot peak fill | `>=` floor | reader still reads shots to completion |
| `camera_adapt_x1.6` / `_x0.6` | detect-rate on a 1.6x / 0.6x scaled crop | `>=` floor | scale-adaptivity -- a differently-zoomed/resolution camera still locks |
| `fill_vs_chain_within5pp` | share of frames within 5pp of the serving chain | `>=` floor | fill accuracy vs the retired 5,700-LOC chain (chaincache sessions only) |

**`top_clips`**: the box top (`bbox[1]`) must sit at or above the red top row (`top_pixel_row`). If a
change lets the box top drop below the red column, the tip is clipped -> fill reads high -> the shot
fires early (the exact bug commit 4464f4a9 fixed). Floor is 0.

**`offshot_falselock_episodes`**: a decor lock is the reader latching a *static red graphic* off-shot.
It is distinguished from a real meter fragment by three tests, **all** required to count: DETACHED
(> 8 frames from every shot -> not a rise/deflate tail), SUSTAINED (>= 5 frames -> not a boundary
blip), and STATIC (fill range < 12pp -> a real meter, even a rejected low-peak contested shot, RISES;
a graphic reads a ~constant fill). This is exactly 0 on HEAD and stays sensitive to a genuine
static-decor lock.

### Baseline measured on HEAD (the numbers being protected)

| Session | frames (window) | shots | detect-rate | maxgap | false-lock | top-clip | glitch | median-peak | cam 1.6/0.6 | fill vs chain <=5pp |
|---------|-----------------|-------|-------------|--------|-----------|----------|--------|-------------|-------------|--------------------|
| session_20260707_135309 | 900 (1112-2012 / 6976) | 9 | 0.963 | 3 | 0 | 0 | 0 | 93.9 | 0.892 | -- |
| session_20260707_175017 | 837 (872-1709 / 1709) | 10 | 0.895 | 3 | 0 | 0 | 0 | 90.6 | 0.867 | -- |
| session_20260707_121446 | 900 (608-1508 / 6000) | 4 | 0.941 | 3 | 0 | 0 | 0 | 70.4* | 0.883 | -- |
| session_20260706_190737 | 900 (152-1052 / 6777) | 9 | 0.964 | 3 | 0 | 0 | 0 | 91.0 | 0.883 | -- |
| session_20260704_210801 (fade) | 900 (336-1236 / 1327) | 14 | 0.942 | 3 | 0 | 0 | 0 | 94.0 | 0.915 | 0.543 |

\* `session_20260707_121446` shots are contested/early-release and genuinely peak ~70 on HEAD, so it
carries a per-session `median_shot_peak_min` of 60 (the `_default` peak floor is 82).

---

## Native timing gates (QtTest `OrionNativeTests`)

The release-clock invariants are pinned by `native_orion/tests/AutomationEngineTests.cpp`. Every
invariant the recent timing work established already has a dedicated test:

| Invariant | Test(s) | Status on HEAD |
|-----------|---------|----------------|
| Fade routes to its **per-type** clock (~380ms), not the 566/250 global | `fadeUsesPerTypeClockWhileStandstillUsesGlobal` | PASS |
| Standstill still rides the **global** clock (fade exemption didn't regress it) | `fadeUsesPerTypeClockWhileStandstillUsesGlobal`, `autonomousVisionFiresOnGlobalClock` | PASS |
| Go-To is **meter-anchored** + **~1600ms blind floor** (not the ~566ms global hold clock) | `gotoUsesMeterAnchoredClockNotGlobalHoldClock` | PASS |
| Go-To no-meter path **blind-fires at the cap** | `gotoNoMeterBlindFires`, `gotoStalePhantomBlindFiresAtNoMeterCap` | PASS |
| Fade **safety-release** at the make-window floor (~92%) | `fadeSafetyReleasesAtMakeWindowFloor` | PASS |
| Fade targets the green-window **ENTRY**, not the center | `fadeTargetsGreenWindowEntryNotCenter` | PASS |
| Grader/artifact **freeze** on a railed error streak | `clockRunawayGuardFreezesOnRailedStreak`, `postReleaseMovingMeterIsNotGraded` | PASS |
| Grader dials the **global** latency, not per-type | `autonomousGraderDialsGlobalLatencyNotPerType` | PASS |
| Overlay **frame-id ring join** (draw the box on the frame it was detected on) | `meterBoxRingJoinsBboxToItsOwnFrame` | PASS |
| **Blind-fire suppression** (abort instead of a blind release when no meter) | `noMeterAbortsInsteadOfBlindRelease`, `gotoLowFillSuppressesMaxHoldDump` | PASS |

All required invariants are covered; no new native test was needed. Full suite on HEAD:
**194 passed, 0 failed** (an older cached log showed 5 failures -- it predated the July-7 timing
fixes; a fresh build is fully green).

---

## Updating the baseline (only for a deliberate, verified improvement)

The floors in `tools/regression/gate_floors.json` are the contract. If you *intentionally* improve the
reader, regenerate the measured numbers and re-tighten the floors:

```bash
C:/Python314/python.exe tools/regression/replay_gates.py --dump --out baseline.json   # per-session metrics
```

then hand-edit `gate_floors.json` (`_default` + per-session overrides are merged; keep a margin below
each measured value). Do **not** loosen a floor to make a regression pass -- that defeats the gate.

## Notes / traps

- The detection replay measures a coarse-located ~900-frame shot window per session -> ~1 min each,
  a few minutes total. It is a protect-the-gains gate, not a per-commit hook; the pytest module
  measures each session once and caches it across its gate assertions, and carries a `slow` marker
  (`-m "not slow"` or `ORION_SKIP_SLOW_REGRESSION=1` to deselect in a quick suite sweep).
- `tools/diagnostics/` is deliberately **not** on `sys.path`; `replay_gates.py` inlines its own
  framedump IO so there is no collision with the root `simple_meter_reader.py`.
- Gates are measured on the **ARMED** pass (production condition). The UNARMED pass only defines
  where the shots are.
- RTK trap: run pytest with `C:/Python314/python.exe -m pytest ...` (not a heredoc).
