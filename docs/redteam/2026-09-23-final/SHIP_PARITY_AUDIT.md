# SHIP PARITY AUDIT: dev launcher vs installed production build (2026-09-23)

READ-ONLY audit. Nothing was built, tested, launched or edited except this file.

- **Dev reference:** `run_orion.local.ps1` (no switches), `settings.json` and `learning.json` in the repo root.
- **Production:** `C:\Program Files\Venice` with `%LOCALAPPDATA%\NexusVision\Orion Native\settings.json` and `learning.json`. Both files were read and diffed against the dev copies. The installed settings were reset at 15:33 (`settings.rejected.json`) and last written at 16:18.

## 0. Bottom line

The installed build differs from the validated dev behaviour in **7 timing-relevant ways**. Six of them hit fades harder than standstills. None of them is a code or reader defect: each is a validated choice that lives only on the dev launch line, in the dev settings file or in the dev learning file.

| # | Item | Dev (validated) | Installed / shipped default | Direction of the error | Fades? |
|---|---|---|---|---|---|
| 1 | Aim (learned_phase_physical_ms) + `tip_phase_aim_frozen` | 271 canonical, **frozen** | installed copy **291.6, unfrozen**; a fresh install uses the seed **319**, unfrozen | installed fires **~20.6 ms later**, a fresh install **~48 ms later**, and the learner keeps walking (freeze-rides-release trap) | all types |
| 2 | `ORION_CURVE_STRETCH_ALPHA` | 0 | **0.6** (compiled default) | the stretch latches on the slow first segment and gives **+57..+101 ms LATE** fires (09-09: 3/27 shots, and 5/5 of the severe lates) | any shot that reaches the curve path |
| 3 | `ORION_TIP_PHASE_SOLO` | 1 | **0** (compiled default; not in the native profile) | the sampler (+37.9 ms late bias, IQR 16 ms) can veto the phase member again. The 09-11 note says this fix removed the random EARLIES | yes (RF phase sd 13.1) |
| 4 | `tip_phase_anchor_base20` | true | **false** | anchor at 30 %, 2 rungs. Runway drops from 129 to 71 ms at lead 274, and late-seen shots fall off the ladder onto the biased sampler, so you get random LATES | **yes, the most exposed** |
| 5 | `tip_phase_type_trim_enabled` | true (LF -4, RF -6) | **false** | fades fire 4-6 ms later (banner-graded: fades want ~12 ms more lead, p=0.0019) | **fades only** |
| 6 | `ownership_proof_two_frame` | true | **false** | one detector frame (~14-17 ms) less runway on late-onset fades | **fades mostly** |
| 7 | `no_meter_fade_trim_ms` | 6 | 0 | the fade blind-backstop hold is 6 ms shorter (minor; safety-net path only) | fades only |

The env-side items #2 and #3 affect **every** production install, fresh or upgraded. Items #4-#7 are settings, so they need both a default change **and** a v3 settings migration, because the installer-repaired file stores `false`/`0` explicitly. Item #1 needs a learning-side fix (see §4).

---

## 1. ENV parity: every active assignment in `run_orion.local.ps1`

The production channels were verified in code:
- `main.cpp:340-347` runs `applyShippedNativeTimingProfile(false)` in production. It force-pins `ORION_HORIZON_DEBIAS=1` and `ORION_RAMP_SHAPE=1` into the native process before `AutomationEngine` is built (`SidecarReaderProfile.h:122-128`).
- `RemotePlaySession.cpp:2371-2484` runs `applyShippedReaderProfile(env, !productionBuild)`. It force-pins 10 reader flags and 6 values (`SidecarReaderProfile.h:61-116`). It also forces `ORION_SIMPLE_READER=1` and pins the frame pipe on no-card Remote Play.
- `OrionAppController.cpp:2068-2090` `envDefaultOn` sets `ORION_INPUT_HOOK`. Its default-on is also used for `ORION_PREVIEW_ASYNC` (`:2152`), precise wait (`:1505`) and the timer guard (`:2228`).
- A customer inherits no other `ORION_*` variable. Every other knob runs at its source default.

The conditional blocks (`-Detdiag`, `-Framedump`, `-VarianceHunt`, `-OffsetSweep`, `-OffsetList`, `-LateCarryAB`, `-LegacyFireWait`) are **off on a plain launch**. They are listed in §1e and not counted as parity items.

### 1a. DIFFERENT: behaviour-changing (timing)

| Env (launcher line) | What it does | Read at | Prod value when absent | Prod reads it? | Dev vs prod |
|---|---|---|---|---|---|
| `ORION_TIP_PHASE_SOLO=1` (:246) | a dated, horizon-plausible phase member cannot be withdrawn by the extrapolating sampler | `AutomationEngine.cpp:2676-2683`; default `AutomationEngine.h:1441` `tipPhaseSolo = false`; gate `AutomationEngine.cpp:16853` | **false** | yes (not fenced), but nothing sets it | **1 vs 0** |
| `ORION_CURVE_STRETCH_ALPHA=0` (:403) | curve-model rate stretch when the first slope is below the table rate | `AutomationEngine.cpp:2795-2801`; default `AutomationEngine.h:1561` `curveRateStretchAlpha = 0.6`; used at `:20834`, `:20996` | **0.6** | yes (not fenced), but nothing sets it | **0 vs 0.6** |

### 1b. DIFFERENT: low impact, onboarding or display-only

| Env | What it does | Read at | Prod default | Note |
|---|---|---|---|---|
| `ORION_PROBE_LEAD_PERSIST=1` (:303) | persist the probe-measured latency prior across sessions | `latency_estimator.py:633` `_env_flag(..., False)` | **False** | Cold start relearns every launch. Firing is unchanged while the lead is user-set (`user_lead_satisfies_authority=true` in both). This affects onboarding and readiness, not the fire instant. Optional: add to `kShippedReaderProfileFlags`. |
| `ORION_READER_BOX_PREDICT=1`, `..._CAP_PX=20` (:575-576) | forward-predict the DRAWN box on hold frames | `simple_meter_reader.py:3095-3097` (default 0 / 40) | **0** | **Reader lane (not ours).** Display-only per its own comment. Deliberately excluded from the profile (`SidecarReaderProfile.h:144-146`). The overlay just looks different to the owner. |
| `ORION_READER_BOX_TIGHT=2` (:603) | reshape the DRAWN box to hug the housing | `simple_meter_reader.py:3129` (default 0) | **0** | **Reader lane.** The ownership proof now judges `det_bbox` (`AutomationEngine.h:400-414`, default true), so this is display-only. The 09-18/19 pickup stalls were caused by this hug flapping 26<->44 px, so **production (0) is the safer arm**. Leave it off. |
| `ORION_POSE_CAMERA_ANCHOR=1` (:319) | pose player-lock prior | `pose_timing.py:384` (default "0") | **0** | Inert. Pose loads only when `no_meter_enabled` (`remote_play_orchestrator.py:1989`), and NO METER is compile-fenced off in production (`AutomationEngine.cpp:1431-1438`). |

### 1c. SAME: the production default or pin already equals dev

| Env | Prod mechanism |
|---|---|
| `ORION_HORIZON_DEBIAS=1`, `ORION_RAMP_SHAPE=1` | native profile force-pin (`SidecarReaderProfile.h:122-125`, `main.cpp:346`) |
| `ORION_LATENCY_REGIME_REOPEN_SOFT`, `ORION_READER_ANCHOR`, `ORION_READER_PCTL_FILL`, `ORION_READER_TRACK_H_CAP`, `ORION_METER_SUBPIXEL_SESSION_RULER`, `..._BASE_HOLD`, `..._SESSION_PROVISIONAL`, `ORION_METER_DETECTOR`, `ORION_METER_DETECTOR_SYNC_ACQUIRE` | sidecar profile force-pin `=1` (`SidecarReaderProfile.h:61-94`) |
| `ORION_SIMPLE_READER=1` | forced unconditionally (`RemotePlaySession.cpp:2427`) |
| `ORION_METER_PROPOSER=cv` | from setting `meter_proposer` = "cv" in both files (`SidecarReaderProfile.h:207-227`) |
| `ORION_METER_DETECTOR_MIN_INTERVAL_MS=0` | default is "0" when the provider is cv-contour (`meter_detector_yolo.py:562-564`) |
| `ORION_CV_OUTLINE_MIN=0` | default 0.0 (`meter_locator_cv.py:172`); reader-lane file, same value |
| `ORION_METER_DETECTOR_PHASED_ACQUIRE=0` | default '0' (`simple_meter_reader.py:1888`) |
| `ORION_READER_FAKELOCK_BREAK=1` | default '1' (`simple_meter_reader.py:2498`); also a proven no-op (`SidecarReaderProfile.h:132-136`) |
| `ORION_READER_POST_RELEASE_YIELD=0` | default '0' (`simple_meter_reader.py:3026`) |
| `ORION_METER_TIME_SCALE=1.0` | default 1.0 (`AutomationEngine.h:1462`) |
| `ORION_GOTO_METER_WAIT=1` | default ON (`AutomationEngine.cpp:2219`) |
| `ORION_USER_LOG=1` | default ON (`AutomationEngine.cpp:242`) |
| `ORION_INPUT_HOOK=1`, `ORION_PREVIEW_ASYNC=1`, `ORION_PRECISE_WAIT_HIRES=1`, `ORION_TIMER_RES_GUARD=1` | default ON (`OrionAppController.cpp:2085`, `:2152`, `:1505`, `:2228`) |
| `ORION_FRAME_PIPE=1`, `ORION_REQUIRE_FRAME_PIPE=1` | Production pins both on no-card Remote Play (`RemotePlaySession.cpp:2458-2476`). Both setups are `video_source=capture_card`, where both builds strip them (`:2482-2483`). |

### 1d. SAME-INERT: set by dev, read only by retired or unused paths

| Env | Why inert in both |
|---|---|
| `ORION_METER_LOCATOR`, `_MODEL`, `_IMGSZ`, `_CONF`, `_EVERY`, `ORION_LOC_ROI`, `ORION_METER_TRACK` | Read only by the legacy `MeterDetector` chain (`meter_locator_infer.py:123-140`, `meter_detector.py:46/3021`, `meter_box_kalman.py:319`). `ORION_SIMPLE_READER=1` is forced, and `simple_meter_reader.py` imports only `DetectResult` from it. |
| `ORION_METER_PROVIDER_PRIORITY=cuda,dml,cpu` | Production pins `dml,cpu` (`SidecarReaderProfile.h:114`). This is hardware-specific and only matters when the ONNX detector proposes (Pill/yolo route). With `meter_proposer=cv` on Arrow2/White it is not on the fire path. |

### 1e. DEV-ONLY: must NOT ship (all confirmed absent or fenced in production)

| Env | Status |
|---|---|
| `ORION_LICENSE_KEY` (:269) | inside `#ifndef ORION_PRODUCTION_BUILD` (`OrionAppController.cpp:4873-4905`) |
| `ORION_AUTO_UNLOCK_LOCAL` (:275) | same fence plus `localDevAllowed()` (`:4877`) |
| `ORION_PYREMOTEPLAY_PYTHON` (:285) | interpreter override for the source sidecar only (`RemotePlaySession.cpp:1849`); production runs the compiled `OrionSidecar.exe` |
| `ORION_METER_DELAY_ARMED` (:266) | meter delay is shelved (4dc80b1). The installer arms only the service's own env (`docs/SHIP_CONFIG.md:48`). Do not add it to any profile. |
| Conditional, off by default: `ORION_DETDIAG*`, `ORION_DETCSV*`, `ORION_FRAMEDUMP*`, `ORION_AVCLOCK*`, `ORION_ONSET_FF_AB`, `ORION_DEV_FIRE_OFFSET_SWEEP`, `ORION_DEV_LATE_CARRY_AB`, `-LegacyFireWait` | diagnostics or A/B only. They must stay out of every shipped profile. |

**ENV counts (46 active unconditional assignments):** 27 SAME, 8 SAME-INERT, **2 DIFFERENT (timing)**, 5 DIFFERENT (low impact, display or inert), 4 DEV-ONLY. UNCLEAR: 0. See §5 for the one code-parity caveat.

---

## 2. SETTINGS parity: dev `settings.json` vs AppConfigData defaults

The installed file was diffed key by key against dev (220 vs 213 keys). Apart from per-machine keys (console IP, chiaki path, capture device id, lightbar colours, lead per-source map), **only these differ**:

| Key | Dev | Installed | Compiled default (`AppConfig.h`) | Kind | Validated? |
|---|---|---|---|---|---|
| `tip_phase_aim_frozen` | true | false | `:483` false | tuning (lock) | YES. 09-11 20:20: the aim walked 388->369 while the lead sat still. Frozen, then owner-nudged to 345 effective = canonical 271 (`docs/HANDOFF_2026-09-01_TIMING_LANE.md:1509-1597`). |
| `tip_timing_user_set` | true | false | `:1013` false | companion of the above | same |
| `tip_phase_anchor_base20` | true | false | `:409` false | tuning | YES. 09-11 22:27: "ONE setting", runway 71->129 ms. 09-11 23:26 banner-graded: 108/108 armed from phase (`HANDOFF_2026-09-01_TIMING_LANE.md:1627-1650`, `:1658-1670`) |
| `tip_phase_type_trim_enabled` | true | false | `:438` false (map default LF -4 / RF -6 = dev) | tuning | YES. 09-11 23:26 banner-graded, fades p=0.0019 (`:1686-1690`). Also cited 09-13 (`HANDOFF_2026-09-13_MAKE_RATE_CEILING.md:42`). |
| `ownership_proof_two_frame` | true | false | `:403` false | tuning | YES (09-14 18:47). Recommended 09-12 20:14 and 09-14, flipped 09-14 18:47 (`HANDOFF_2026-09-14_UI_POLISH.md:309-317`). **Pairing rule satisfied:** `anchor_rise_min_pct` = 3 in dev, installed and default (`AppConfig.h:394`). The "never pair with 4.0 pp" condition does not apply. `AppConfig.cpp` (the settingsMigrations comment block) still says "never confirmed by a counted live batch", so that comment is stale versus the 09-14 flip. |
| `no_meter_fade_trim_ms` | 6 | 0 | `AppConfig.cpp:2042` 0.0 | owner slider (tuning) | owner-set 09-14. It rides into `blindReleaseHold` for LF/RF (`AutomationEngine.cpp:21389-21394`), which the **meter blind backstop** uses (`:21893`), so it is live in meter mode. Low priority. |
| `lead_factory_placeholder_ms` | 274 | 269 | `AppConfig.cpp:2177` 269 | per-rig lead seed | Not a parity item. The owner's lead is `actuation_lead_ms` 274, user-set in both. |
| `input_timed_lead_ms` | 190 | 272 | `AppConfig.cpp:2032` 272 | NO METER only | Inert. `input_timed_enabled` is fenced false in production (`docs/SHIP_CONFIG.md:140`). |
| `press_anchored_tip_ms` / `_n` / `_sigma_ms` | learned (n=100) | learned (n=9-30) | n/a | **LEARNED** | Must not become defaults. The predictor and fallback are both false, so these are inert on the fire path. |
| `meter_delay_enabled` / `meter_delay_ms` | false / 141 | **true** / 250 | `:655` true | shelved | Ignored per brief. Confirm the shelving (4dc80b1) makes `true` a no-op, because the installed file carries `true`. |

Everything else (all banner_trim_*, lead_offset_* (LF -6 / RF +8 / mid 6), aim_margin 69, tip_frame_native, tip_phase_first_sight_anchor, onset_ff_*, meter_backstop_*, vision_hold_band_* 0, tip_gate_*, anchor_*, owned_meter_never_aborts, ownership_proof_leniency, user_lead_satisfies_authority, meter_proposer, meter_style, meter_color) is **identical** in dev and installed.

### 2b. LEARNING parity (`learning.json`, not shipped by the installer, `orion.iss:254-255`)

| Key | Dev | Installed | Engine fallback when absent |
|---|---|---|---|
| `learned_phase_physical_ms` (canonical base-30 aim) | **271** (frozen, owner-set) | **291.63** (learner, unfrozen) | seed `tipPhaseSeedPhysicalMs = 319` (`AutomationEngine.h:2022`), constant 393 (`:1969`) |
| `measured_phase_physical_ms` | 286.1 | 291.6 | n/a (display/warn only) |
| `banner_lead_trim_by_type` | `Right Fade/normal: 3` | absent | LEARNED; do not default |
| `no_meter_hold_by_type` | n=100 per type | n=9-30 | LEARNED; do not default |

**This is the biggest single gap.** Effective aim = learned canonical + (393 - 319) [+58.3 at base-20]. Dev aims at 271. The installed copy aims at 291.6, which is **20.6 ms later with every other setting equal**. That is more than the fitted green half-window (12.1 ms, 09-11 23:26). A fresh customer install aims at the seed, 319, which is **48 ms later** than the validated aim. Unfrozen, that aim then walks in the direction of the bot's own releases: the anchor-to-freeze instrument measures the release landing (the `[ORION_FREEZE_RIDES_RELEASE]` note in `AppConfig.cpp`'s migration block). A factory `lead_factory_placeholder_ms` of 269 that was tuned against aim 271 would land those shots late.

---

## 3. Reader-lane flags (report only; no edits proposed in `simple_meter_reader.py` / `meter_locator_cv.py`)

- `ORION_READER_BOX_PREDICT`, `ORION_READER_BOX_TIGHT`: display-only differences. **Recommendation: leave production at 0.** If the owner wants the dev look, the lever is `kShippedReaderProfileValues` in `SidecarReaderProfile.h`, which is caller-side. The header's exclusion rationale (lines 144-146) and the 09-19 BOX_TIGHT pickup-stall history argue against it.
- `ORION_CV_OUTLINE_MIN`, `ORION_READER_FAKELOCK_BREAK`, `ORION_READER_POST_RELEASE_YIELD`, `ORION_METER_DETECTOR_PHASED_ACQUIRE`: dev equals the reader's own default, so there is nothing to do.
- All reader flags that matter for fill (ANCHOR, PCTL_FILL, TRACK_H_CAP, SUBPIXEL x3, DETECTOR, SYNC_ACQUIRE) are already force-pinned by the native profile.

---

## 4. Recommended change list

The engine and AppConfig files are ours. Every change is caller-side; none touches the reader lane.

| # | Change | Where | Migration for existing installs? |
|---|---|---|---|
| R1 | Add `"ORION_TIP_PHASE_SOLO"` to `kShippedNativeTimingProfileFlags` (production force-pins "1") | `native_orion/src/SidecarReaderProfile.h:122-125` | No (env, applies on every launch). Leaves `AutomationEngine.h:1441` and its test fixtures untouched, the same pattern as HORIZON_DEBIAS/RAMP_SHAPE. |
| R2 | Pin `ORION_CURVE_STRETCH_ALPHA=0` in production. Preferred: add a small `kShippedNativeTimingProfileValues[] = {{"ORION_CURVE_STRETCH_ALPHA","0"}}` handled by `applyShippedNativeTimingProfile` exactly like the sidecar value list (`:106-116`, `:177-193`). Alternative: flip `AutomationEngine.h:1561` 0.6 -> 0, which the header says needs its 46 mechanism tests moved. | `SidecarReaderProfile.h:122-128`, `:330-356` | No (env) |
| R3 | Bump `kShippedTimingProfileId` (e.g. `2k27-2026-09-23-v3`) and its two pins | `SidecarReaderProfile.h:127-128`; `tests/test_consistency_bench.py:58`; `tools/quality/consistency_bench.py:111` | No |
| R4 | `tipPhaseAnchorBase20 = true` | `native_orion/src/AppConfig.h:409` (the loader `AppConfig.cpp:1776` inherits the member default) | **YES.** v3 rule `{3, "tip_phase_anchor_base20", false, true, ""}`. The installer-repaired file stores `false` explicitly. The regime flip clears the in-session sample window and adds +58.3 on restore (`AutomationEngine.cpp:1397-1408`, `:2503-2520`), so the migration is self-consistent. |
| R5 | `tipPhaseTypeTrimEnabled = true` | `AppConfig.h:438` (loader `:1786`) | **YES.** v3 rule `{3, "tip_phase_type_trim_enabled", false, true, ""}`. The map default is already LF -4 / RF -6. |
| R6 | `ownershipProofTwoFrame = true` | `AppConfig.h:403` (loader `:1774`) | **YES.** v3 rule `{3, "ownership_proof_two_frame", false, true, ""}`. Keep the `anchor_rise_min_pct` 3.0 pairing (do not raise it to 4). |
| R7 | Aim parity: ship a frozen, validated aim | `AppConfig.h:483` `tipPhaseAimFrozen = true` (loader `:1808`) **plus** an aim value: **either** (a) a factory `learnedPhasePhysicalMs` prior of 271 used when learning.json has none (`AppConfig.h:1805` is -1 today; `AutomationEngine.cpp:1406-1423` then falls back to the seed), **or** (b) move `tipPhaseConstantMs` 393 -> 345 with the seed kept at 319 (`AutomationEngine.h:1969`), so a fresh install's effective aim equals the owner's 345 (base-30). | **YES, and it needs more than a settings rule.** A v3 `{3, "tip_phase_aim_frozen", false, true, "tip_timing_user_set"}` rule alone would **freeze a drifted learner** (the installed copy would lock at 291.6). It must be paired with a one-time learning.json re-seed of `learned_phase_physical_ms` to the validated 271, gated on `tip_timing_user_set == false`. Owner decision needed on (a) vs (b), and on whether 271 transfers across rigs, because aim and lead trade 1:1 in fire time. **Immediate owner workaround for today's A/B:** copy dev `learned_phase_physical_ms: 271` into the installed learning.json and set `tip_phase_aim_frozen`/`tip_timing_user_set` true, with the app CLOSED. |
| R8 | `no_meter_fade_trim_ms` default 0 -> 6 (optional, low value) | `AppConfig.cpp:2042` fallback + the header member | Optional v3 rule `{3, "no_meter_fade_trim_ms", 0, 6, ""}`. It is an owner slider value, so skip it if unsure. |
| R9 | Optional: add `ORION_PROBE_LEAD_PERSIST` to `kShippedReaderProfileFlags` | `SidecarReaderProfile.h:61-94` | No. Onboarding-only; firing is unaffected with a user-set lead. |
| R10 | Bump `kSettingsVersion` 2 -> 3 and append the R4-R8 rules | `AppConfig.h:2207`; registry `AppConfig.cpp:504-573` (append-only) | This is the migration itself. Also update the stale "never confirmed" comment for two_frame / base20 in the same block. |
| R11 | Update the pins and docs | `AutomationEngineTests::shipConfigDefaultsArePinned` / `...SurviveAKeylessSettingsFile`, `AutomationEngineTests.cpp:14901-14936, 15142` (these pin `ownership_proof_two_frame` / `tip_phase_anchor_base20` false), `tests/test_ship_defaults.py`, `docs/SHIP_CONFIG.md` §3b | n/a. Also fix the SHIP_CONFIG drift: `lead_offset_left_fade_ms` is documented as 8.0 but is **-6.0** in code (`AppConfig.cpp:2140`) and in both files. |

These are security/release-adjacent (settings migration + shipped timing profile), so they need a StrictSecurity verify and a rebuilt package. Only do this after the owner's play-test.

---

## 5. Caveats and UNCLEAR

- **Code parity is out of scope.** This audit compares configuration only. Dev runs the working-tree `native_orion\build\Release\OrionNative.exe` and the source Python sidecar. Production runs rc3's compiled `OrionSidecar.exe`. The reader sources were being edited in another lane, so confirm the installed sidecar was built from the same reader revision the owner validated on.
- `frozenAimPhysicalMs_` falls back to `config_.tipPhaseSeedPhysicalMs` at `AutomationEngine.cpp:1421-1423`, **before** the base-20 constellation is applied at `:2503-2520` in the same `applyConfig`. On a fresh install with no learning prior, frozen, and base-20 turning on in that same pass, check that the latched frozen aim includes the +58.3 shift. If it does not, it would fire ~58 ms early. This is one more reason to prefer R7(a)/(b) with an explicit prior over relying on the seed fallback.
- Both setups run `video_source=capture_card`, so the dev frame-pipe env is stripped in both. Parity for decoder (no-card) customers was verified only through code (the production force-pin).
