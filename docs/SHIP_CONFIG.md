# SHIP CONFIG — what a packaged install runs on

**Status:** current as of 2026-09-23 (ship-parity pass, §7). Owned jointly by `tests/test_ship_defaults.py` (Python)
and `AutomationEngineTests::shipConfigDefaultsArePinned` /
`::shipConfigDefaultsSurviveAKeylessSettingsFile` (C++). A change to any row below without a
change to those tests is a bug in one of the two.

---

## 1. The problem this document closes

The configuration every graded session ran on was set by **environment variables on the dev
launch line** — `run_orion.local.ps1`, plus the `ORION_*=1` prefixes the owner typed in front
of it. That launcher is:

* **gitignored** (`.gitignore`: `*.local.ps1`), and
* **in no packaging list** (`installer/orion.iss` `[Files]`, `tools/package_orion_release.py`).

A customer's install therefore inherits **zero** `ORION_*` variables. Any switch that lived
only on that launch line shipped at its **source default** — which, for eight of them, was the
*opposite* of what the owner played on. The 09-16 20:26 session he called perfect
(22 EXCELLENT / 3 LATE / 2 EARLY, 12 EXCELLENT in a row) would not have reproduced on a
packaged build.

This is the same failure class `native_orion/src/SidecarReaderProfile.h` was written to close
one layer down, and its header states the rule this document extends:

> Every live batch this project has ever graded — and therefore every timing number the release
> lead was calibrated against — ran with those flags ON. A customer build inherits ZERO
> `ORION_*` variables, so it would run a detector configuration that has never been played on.

**The fix is the same one that header chose: make the graded value the SOURCE default, and keep
the environment as an override.** `run_orion.local.ps1` keeps working unchanged.

---

## 2. What the packaged launch path actually sets

There are exactly three places a packaged install writes `ORION_*` before the detector runs.
None of them touches the knobs in §3.

| Where | What it sets | Contract |
|---|---|---|
| `native_orion/src/main.cpp:346` → `applyShippedNativeTimingProfile()` (`SidecarReaderProfile.h`) | `ORION_HORIZON_DEBIAS=1`, `ORION_RAMP_SHAPE=1`, `ORION_TIP_PHASE_SOLO=1`, `ORION_CURVE_STRETCH_ALPHA=0`, `ORION_TIMING_PROFILE_ID=2k27-2026-09-23-v3` into the **current process**, before `AutomationEngine` is constructed | production force-pins; dev preserves an explicit value |
| `native_orion/src/OrionAppController.cpp:1930-2040` | `ORION_AUTONOMOUS_VISION`, `ORION_INPUT_HOOK`, `ORION_FRAME_PIPE`, `ORION_FREEZE_CAL`, `ORION_GREEN_SELF_GRADE`, `ORION_QML_RENDER` — all "default ON, `=0` opts out" (`envDefaultOn`) | process env, inherited by the sidecar |
| `native_orion/src/RemotePlaySession.cpp:2282-2464` → `startSidecar()` | `applyShippedReaderProfile(env, !productionBuild)` (the 11 reader flags + 6 numeric values in `SidecarReaderProfile.h`), `ORION_METER_MODEL`, `ORION_METER_PROPOSER` (from the user's Meter Detection setting), `ORION_SIMPLE_READER=1`, `PYTHONUNBUFFERED=1`, `ORION_PREVIEW_SHM*`, the frame-pipe / capture-card / video-device transport keys, and `SDL_*`. **Strips** `ORION_DETDIAG*` unless explicitly truthy | production ignores an inherited value; dev keeps an explicit one |

`installer/orion.iss` sets no `ORION_*` except `ORION_METER_DELAY_ARMED` on the **service's**
own registry environment (`ServiceArgs.h`), which is unrelated to detection or timing.

So for every knob in §3 the answer is: **nothing upstream sets it; the Python/C++ source
default is what a customer runs.** `tests/test_ship_defaults.py::
test_the_native_sidecar_launch_sets_none_of_these` fails if that ever stops being true.

---

## 3. The table

`→` marks a default this packaging pass MOVED (09-17). Everything else was already correct and
is now pinned so it cannot drift.

### 3a. Sidecar (Python) — the detector, the reader and the graders

| Knob | Ship default | Env override | Lives in |
|---|---|---|---|
| `ORION_BANNER_VERDICT_LIVE` | **ON** → *(was OFF)* | `=0` disables; every fail-soft path still disables after one WARNING | `banner_verdict_live.py:576` (`BannerVerdictLive.create`) |
| `ORION_PLAYER_ANCHOR` | **ON** → *(was OFF)* | `=0` makes the module inert | `player_anchor.py:155` (`enabled()`, read per call) |
| `ORION_ANCHORED_SEARCH` | **ON** → *(was OFF)* | `=0` restores the full-band search | `meter_locator_cv.py:241` |
| `ORION_EXPECTATION_WINDOW` | **ON** → *(was OFF)* | `=0` drops the rise-pair rule | `meter_locator_cv.py:242` |
| `ORION_CV_SHAPE_MIN_H_ARMED` | **8** → *(was 14 = the unarmed floor)*, and never above `ORION_CV_SHAPE_MIN_H` | any float; 14 restores the pre-ship floor | `meter_locator_cv.py:243-247` |
| `ORION_CV_COL_W_MIN_ARMED` | **6** → *(was 8 = the unarmed floor)*, and never above `ORION_CV_COL_W_MIN` | any float; 8 restores it | `meter_locator_cv.py:255` |
| `ORION_ANCHOR_REFUSE_MODE` | **1** (scan the band and COUNT refusals) | `=0` skips the band scan entirely | `meter_locator_cv.py:260` |
| `ORION_ANCHOR_CORE_PX` | **19** → *(was 27 = the whole crop)* | `=0` restores the shipped 27x27 template. The corners carry the 09-12 court's blond wood; on the 09-17 blue court the same plate scores 0.62 with them and 0.85 without | `player_anchor.py` (`_match_tmpl`) |
| `ORION_ANCHOR_SCALE_WIDE` | **1** → *(was the fixed 0.92/1.0/1.08 ladder)* | `=0` restores `SCALES_ACQ`. The plate is a PERSPECTIVE element: its disc runs 11-30 px across the corpus | `player_anchor.py` (`_scales`) |
| `ORION_ANCHOR_COARSE_DIV` / `_STRIPS` / `_TOPK` | **2 / 3 / 8** → *(was 4 / 1 / 3)* | half-resolution coarse pass, one template size per y-strip (scale ~ row), 8 refined peaks | `player_anchor.py` (`_coarse_map`, `_acquire_pass`) |
| `ORION_ANCHOR_PLATE_GATE` | **1** | `=0` drops the "is there a gamertag beside this disc" floor (`_GRAD` 30 / `_BRIGHT` 0.04 / `_STD` 20, all ~5x under the measured true p10) | `player_anchor.py` (`_tag_stats`) |
| `ORION_ANCHOR_PS_ADAPT` | **1** | `=0` pins the floor at `ORION_ANCHOR_PS_MIN`. Only a CONFIRMED identity may lower it, never below `ORION_ANCHOR_PS_FLOOR_MIN` (0.42) | `player_anchor.py` (`_ps_floor`) |
| `ORION_ANCHOR_DY_SCALED` | **1** | `=0` restores the constant 150 px icon->box offset. The meter is a fixed-size HUD panel and the plate is not, so the gap scales with the plate | `player_anchor.py` (`_anchor_from`) |
| `ORION_ANCHOR_OFFSET_IDENT` | **1** | `=0` restores the circular rule (learn an offset only when today's patch already contains the box). Guarded by `_OFFSET_TX` 0.55 and `_OFFSET_SANE` | `player_anchor.py` (`note_meter`) |
| `ORION_FRAMEDUMP_PRESS_BANNER` | **1** | `=0` restores `POST_MS` 400 / `MAX_MS` 3000. On: 1700 / 4500, so the window reaches the game's `TIMING\|DISTANCE` panel (it lands release+1.2-1.45 s) | `remote_play_orchestrator.py` (`_init_framedump_press_window`) |
| `ORION_CV_TIPLESS_ARMED` | **ON** | `=0` requires a green tip on every candidate | `meter_locator_cv.py:284`; reader side `simple_meter_reader.py:4724` (`_pa_arm_only`) |
| `ORION_READER_GHOST_FORGET_LOCATOR` | **ON** | `=0` leaves the proposer's positional memory alone | `simple_meter_reader.py:2099` |
| `ORION_READER_FORGET_RATE_LIMIT` | **ON** (one forget per press per zone; never inside a promotion window) | `=0`; bounds `ORION_READER_FORGET_ZONE_PX` 64, `ORION_READER_FORGET_DEFER_MAX` 12 | `simple_meter_reader.py:2131-2134` |
| `ORION_READER_BOX_LATCH` | **ON** | `=0` lets a teleport re-seed replace the box mid-shot | `simple_meter_reader.py:2187` |
| `ORION_READER_STATIC_ZONE_QUARANTINE` | **ON** | `=0`; its six numeric bounds are separate knobs | `simple_meter_reader.py:2204` |
| `ORION_SHOT_RECORDS` | **ON** | `=0` off. Refuses to arm under pytest unless set explicitly (the corpus is production data) | `shot_records.py:211` |
| `ORION_SHOT_RANGE` | **ON** | `=0` off. The nameplate "3"-cell THREE/MID reading for the live press. Its VERDICT is withheld (`unknown`) until `ORION_SHOT_RANGE_CALIBRATED=1`: the 2026-09-17 offline pass did not clear the 90 % agreement bar. The cell samples are still recorded into the shot-record JSONL | `shot_range.py` |
| `ORION_READER_FRESH_AFTER_GHOST` | **OFF** → *(was ON)* | `=1` re-arms it byte-for-byte | `simple_meter_reader.py:2177` |
| `ORION_READER_IDLE_PUBLISH_GATE` | **OFF** → *(was ON)* | `=1` re-arms the gate | `simple_meter_reader.py:2424` |
| `ORION_LOCATOR_IDLE_REUSE` | **OFF** → *(was ON)* | `=1` re-arms the static-frame short circuit | `meter_locator_cv.py:352` |
| `ORION_STALL_ATTRIB` | **OFF** (already) | `=1` arms the attributor (~0.45 % of the frame budget) | `stall_attributor.py:97` |
| `ORION_FRAMEDUMP` | **OFF** (already — no default at all) | `=1` plus `ORION_FRAMEDUMP_*`; `_PRESS_WINDOW=1` for the 60 fps press-window mode | `remote_play_orchestrator.py:1406` |

**`ORION_BANNER_VERDICT_LIVE` has a packaging dependency and it is satisfied.** The live grader
imports `tools/timing/panel_grade.py`, which `tools/sidecar_bundle_manifest.py` lists as a
READER SOURCE input — copied to the repo-root name `orion_panel_grade` and compiled *into*
`OrionSidecar.exe` (a reader never ships as readable source, per `docs/IP_PROTECTION_PLAN.md`)
— and `tools/timing/panel_templates.npz` as a build-gated DATA input. A build missing either
one fails the manifest gate rather than shipping a silently graded-blind sidecar.

Also pinned by `SidecarReaderProfile.h` and therefore *not* Python source defaults (production
force-pins them; do not duplicate the decision in Python):
`ORION_READER_ANCHOR`, `ORION_READER_PCTL_FILL`, `ORION_READER_TRACK_H_CAP`,
`ORION_METER_SUBPIXEL_SESSION_RULER`, `…_SESSION_PROVISIONAL`, `…_BASE_HOLD`,
`ORION_METER_DETECTOR`, `ORION_METER_DETECTOR_SYNC_ACQUIRE`,
`ORION_LATENCY_REGIME_REOPEN_SOFT`, `ORION_METER_PARTIAL_OCCLUSION` (+ its 4 numeric bounds),
`ORION_METER_DETECTOR_CONF=0.35`, `ORION_METER_PROVIDER_PRIORITY=dml,cpu`.

### 3b. Native (C++) — `AppConfigData` compiled defaults

Every one of these is written **twice**: the member initialiser in `AppConfig.h` (what a *fresh*
install gets) and the per-key fallback in `AppConfig::load` (what an *upgraded* install with a
key-less `settings.json` gets). They have to agree, which is what
`shipConfigDefaultsSurviveAKeylessSettingsFile` proves.

| Setting (JSON key) | Ship default | Env override | Lives in |
|---|---|---|---|
| `vision_hold_band_ms` | **0.0 = OFF** → *(was 40.0)* | `ORION_VISION_HOLD_BAND_MS` | `AppConfig.h:1451`, loader `AppConfig.cpp:1754` |
| `vision_hold_band_fade_ms` | **0.0 = OFF** → *(was 60.0)* | `ORION_VISION_HOLD_BAND_FADE_MS` | `AppConfig.h:1465`, loader `AppConfig.cpp:1757` |
| `sprint_release_on_square` | `false` | `ORION_SPRINT_RELEASE_ON_SQUARE=1` | `AppConfig.h:703` |
| `square_press_r2_hold_ms` | `50.0` (clamp 0..150) | `ORION_SQUARE_PRESS_R2_HOLD_MS` | `AppConfig.h:732` |
| `banner_lead_trim` | `true` | `ORION_BANNER_LEAD_TRIM=0` | `AppConfig.h:1127` |
| `banner_trim_tempo_buckets` | `true` | `ORION_BANNER_TRIM_TEMPO=0` | `AppConfig.h:1167` |
| `banner_trim_range_buckets` | `true` (fades key on `type/tempo/range`) | `ORION_BANNER_TRIM_RANGE=0` | `AppConfig.h` |
| `banner_trim_absent_coverage_open` | `true` (a panel with **no coverage cell** — the 2-cell `TIMING \| DISTANCE` layout, i.e. no defender context — calibrates as an open shot; an *unreadable* cell is still excluded) | `ORION_BANNER_TRIM_ABSENT_COVERAGE_OPEN=0` | `AppConfig.h` |
| `banner_trim_bias_window` | `12` (calibrating verdicts per bucket in the net-vote window, band 2..60) | `ORION_BANNER_TRIM_BIAS_WINDOW` | `AppConfig.h` |
| `banner_trim_bias_votes` | `0` = **OFF as shipped** (net `#LATE − #EARLY` that buys one step when enabled; band 0..20; premise refuted 09-19, the lead is already within ~5 ms of optimum) | `ORION_BANNER_TRIM_BIAS_VOTES` | `AppConfig.h` |
| `lead_auto_seed` | `true` | `ORION_LEAD_AUTO_SEED=0` | `AppConfig.h:1235` |
| `aim_margin_ms` | `69.0` | `ORION_AIM_MARGIN_MS` | `AppConfig.h:1243` |
| `lead_factory_placeholder_ms` | `269.0` | — (settings/UI) | `AppConfig.h:1250` |
| `lead_offset_left_fade_ms` | `-6.0` (left fades fire 6 ms LATER; a pre-rev-2 file still holding the old +8 is migrated once, `lead_offset_left_fade_rev`) | `ORION_LEAD_OFFSET_FADE_MS`, `ORION_LEAD_OFFSET_BY_TYPE=0` | `AppConfig.h`, loader `AppConfig.cpp` |
| `lead_offset_fade_mid_ms` | `6.0` (used instead of the +-8 when `range=mid`) | `ORION_LEAD_OFFSET_FADE_MID_MS` | `AppConfig.h` |
| `lead_offset_right_fade_ms` | `8.0` | same | `AppConfig.h:1206` |
| `lead_offset_standstill_ms` / `_other_ms` | `0.0` | same | `AppConfig.h:1207-1208` |
| `meter_backstop_grace_ms` | `100.0` | `ORION_METER_BACKSTOP_GRACE_MS` | `AppConfig.h:1333` |
| `meter_backstop_grace_fade_ms` | `220.0` | `ORION_METER_BACKSTOP_GRACE_FADE_MS` | `AppConfig.h:1353` |
| `meter_backstop_never_seen_probe_ms` | `0.0` (collapse OFF, 2026-09-17) | `ORION_METER_BACKSTOP_NEVER_SEEN_PROBE_MS` | `AppConfig.h:1381` |
| `meter_backstop_never_seen_probe_fade_ms` | `0.0` (fades excluded) | `ORION_METER_BACKSTOP_NEVER_SEEN_PROBE_FADE_MS` | `AppConfig.h:1403` |
| `tip_frame_native` | `true` | `ORION_TIP_FRAME_NATIVE=0` | `AppConfig.h:440` |
| `tip_phase_anchor_base20` | **`true`** → *(was false; 2026-09-23)* | settings only | `AppConfig.h` (loader inherits the member) |
| `tip_phase_type_trim_enabled` | **`true`** → *(was false)*; map LF `-4` / RF `-6` | settings only | `AppConfig.h` |
| `ownership_proof_two_frame` | **`true`** → *(was false)*; paired with `anchor_rise_min_pct` `3.0` (never 4.0) | settings only | `AppConfig.h` |
| `no_meter_fade_trim_ms` | **`6.0`** → *(was 0.0)* | `ORION_NO_METER_FADE_TRIM_MS` | `AppConfig.h`, loader + save clamp `AppConfig.cpp` |
| `tip_phase_aim_frozen` | **`true`** → *(was false)*, with the factory aim prior `LearningData::kShippedPhasePhysicalMs` = **271** (canonical base-30) installed by `AppConfig` whenever `learning.json` has none | Tip Timing card Reset/Unlock | `AppConfig.h`, `AppConfig::applyShippedPhasePrior` |
| `input_timed_enabled` (NO METER) | `false`, **and fenced** | none — `AppConfig::inputTimedAllowed()` returns `false` process-wide, so the loader ANDs a disk `true` to `false` | `AppConfig.h:1030`, fence `AppConfig.cpp:36/39`, load `AppConfig.cpp:1604` |

---

## 4. Why the defaults that moved DOWN moved (five knobs, four reasons)

The ON half is straightforward — every one of those was ON in every graded session. The OFF
half is the part that needs its reasoning written down, because the code stays:

* **`vision_hold_band_ms` / `_fade_ms` 40/60 → 0.** Shipped for one evening and **refuted live**
  on 09-16 21:00-21:01: a faster animation tempo moved the game's own window (first sight
  434-450 ms, natural holds 583-602 against a window of ~608 ± 5), the band clamped 4 of 5
  presses and was **wrong on all four** (746→696 EARLY, 583→616 LATE, 596→613 LATE, 602
  untouched EARLY). The band is anchored on a law that does not move with tempo, so it fights
  the per-tempo trim. Re-arm it for an A/B the moment a tempo-aware law exists.
* **`ORION_READER_FRESH_AFTER_GHOST` → OFF.** It overlaps the rate-limited
  `GHOST_FORGET_LOCATOR`, which is the layer the ship sessions were actually graded with
  (09-16 20:43: 9 fired / 30 suppressed / 0 deferred, every ghost press published). This one has
  no graded live hours of its own.
* **`ORION_READER_IDLE_PUBLISH_GATE` → OFF.** One more publication layer that can refuse a real
  first read. The 09-16 14:20 blind run — six consecutive presses with **no vision sample at
  all** — was exactly that failure class, and it took a day to attribute because the layer
  counters sat past the native relay's 300-char trim.
* **`ORION_LOCATOR_IDLE_REUSE` → OFF.** A genuine idle-cost win (32 ms → 0.7 ms per static
  1080p frame) with no live hours, and the same "unrated short-circuit" shape. Its suite
  (`tests/test_idle_scan_cost.py`) arms it explicitly and still owns the mechanism.

---

## 5. Known gap (owned by whoever next touches `AutomationEngine.cpp`)

`AutomationEngine::applyConfig` falls back to a **hard-coded 40.0 / 60.0** when
`settings.visionHoldBand*Ms` is non-finite (`AutomationEngine.cpp:2209` and `:2226`). That is
now the only place in the tree still carrying the pre-ship number. It can only be reached by an
in-memory NaN (the file and settings routes both clamp), and
`visionHoldBandSettingsRoundTrip()` pins the current behaviour, so it is not a live defect —
but it should become `AppConfigData{}.visionHoldBandMs` (or 0.0) with the next
`AutomationEngine.cpp` change. This pass deliberately did not touch that file.

---

## 6. How to change a default

1. Move it in the source file named in §3 (both places for a C++ one: header + loader).
2. Move the pin in `tests/test_ship_defaults.py` and, for a C++ knob, in
   `AutomationEngineTests::shipConfigDefaultsArePinned`.
3. If the key is newly persisted, add/adjust its note in the `introducedKeys` block of
   `AutomationEngineTests.cpp` — that note has to say what the compiled default *does* to an
   existing install.
4. Move the row here, and say **why** in §4 if the direction is "off".

---

## 7. Ship-parity pass (2026-09-23)

Source: `docs/redteam/2026-09-23-final/SHIP_PARITY_AUDIT.md`; patch notes
`docs/redteam/2026-09-23-final/patches/P-G_ship_parity.md`. The installed build differed from
the owner's validated dev setup in seven timing-relevant ways; all seven now ship.

* **Env (native timing profile, every install):** `ORION_TIP_PHASE_SOLO=1` and
  `ORION_CURVE_STRETCH_ALPHA=0` are force-pinned in production by
  `applyShippedNativeTimingProfile` (`kShippedNativeTimingProfileFlags` /
  `kShippedNativeTimingProfileValues`); profile id `2k27-2026-09-23-v3`. The compiled engine
  defaults (`tipPhaseSolo=false`, `curveRateStretchAlpha=0.6`) are unchanged on purpose, the
  same pattern as `ORION_HORIZON_DEBIAS` / `ORION_RAMP_SHAPE`.
* **Settings (§3b rows above):** new compiled defaults for a fresh install, plus the
  **settings_version 3** migration for existing installs (`AppConfig::settingsMigrations`): each
  key moves only when absent or still at its old default (`false` / `0`), never over any other
  value, and never again once the file is stamped v3. `tip_phase_aim_frozen` is vetoed by
  `tip_timing_user_set`.
* **Aim (owner-approved):** a fresh install flies `learned_phase_physical_ms` 271, frozen. An
  existing install crossing v3 whose aim is not user-owned (neither `tip_timing_user_set` nor
  `tip_phase_aim_frozen` true in the pre-migration file) has its learned aim reset to 271 once,
  logged as `settings migration v3 applied: learned_phase_physical_ms`.
* **Meter delay** stays shelved: `kMeterDelayShelved` (`OrionAppController.cpp`) makes a stored
  `meter_delay_enabled=true` a no-op.
