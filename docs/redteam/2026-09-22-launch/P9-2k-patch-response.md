# P9: 2K patch response (Claude lane report, 2026-09-22)

Scope: what happens to Venice when an NBA 2K27 patch changes the shot meter. The lane covers our own software's resilience and our release process only. Nothing here touched 2K or PSN. This was a read-only review: no source, settings or git state changed, and nothing was built or launched.

ID prefix `CL2-P9-`. Severity uses the product-pipeline scale (customer impact). Findings are ranked.

## Short answers

| Question | Answer |
|---|---|
| 1. Detection | **Total loss** of detection (new colour, shape or tip): the meter path **keeps firing blind** on 2K27 hold constants. After 3 shots the "NO METER DETECTED" label appears, but it is presentation-only and does not stop the bot. **Partial change** (the meter is still found, but its geometry or speed differs): **silent wrong timing**, with no guard. Venice cannot tell a customer "detection unavailable" and then stop. |
| 2. Timing | The phase constant, curve table, blind-hold table, onset buckets and slope ceilings are compiled 2K27 meter-time constants. The only speed adaptation is a stretch that handles a **slower** meter, capped at 40 %. A **faster** meter fires late with no guard. The scale knob `ORION_METER_TIME_SCALE` exists but is env-only, and nothing remote can set it. |
| 3. Response speed | **No hot path.** The version is compiled into the exe, so even a model or JSON change needs a full release: native build, Nuitka sidecar, Strict, Lethe pack, VM test, offline sign, publish. Realistic time to the first customers is **~6-10 h** for a constant fix, **1-2 days** for a model retrain, and **2-4 days** for a timing rebaseline. Clients pick up the update within 30 min, but only once they are idle. |
| 4. Regression tooling | The harness exists and fails closed when no session is available. Its **committed reference corpus is five July 2K26 sessions, none of which are on disk**. Two 2K27 dumps (09-14, about 6 GB) sit locally but are unregistered, have no floors and have no backup. The pytest version of the gate skips silently. |
| 5. Owner visibility | **None.** Backend metrics count licences only, and no shot, lock or verdict counter ever leaves the client. The owner will hear about a break from Discord first. |

---

### [CL2-P9-001] high: when the meter is gone, the bot keeps shooting blind, and the "NO METER" warning does not stop it
- **Lane:** P9
- **Failure path:**
  1. A patch changes the meter's colour, shape or tip. The shipped CV proposer only accepts an achromatic white column (max ≥ 225, spread ≤ 25), 8-22 px wide, with an HSV-green tip exactly 100 px above its base (`meter_locator_cv.py:9-24`). A changed meter therefore yields no box, the reader reports no meter, and the engine has no owned shot.
  2. `meterBlindBackstop` defaults to **true** (`native_orion/src/AppConfig.h:1464`, `AutomationEngine.h:424`). Its block reasons are only `off`, `not_live_meter`, `unclassified`/`excluded_type` and `no_lead` (`AutomationEngine.cpp:21429-21456`). None of them consider whether detection has been failing for several shots.
  3. The backstop therefore releases Square on every press at `max(450, H_ref + Δ(type) + R)`, where `H_ref = noMeterHoldMs = 650` (`AutomationEngine.h:320`). Δ comes from a hard-coded 2K27 table: Standstill 0, Left Fade +276, Right Fade +304 (`AutomationEngine.cpp:21235-21262`).
  4. `observeMeterBlindness` raises `meterBlindWarning_` after 3 armed epochs with no genuine detection (`OrionAppController.h:2597`, `.cpp:11473-11535`). By its own contract it is display-only (`OrionAppController.h:531-537`). The engine never reads it; it is not referenced anywhere in `AutomationEngine.*`.
  5. Any single genuine raw detection clears the warning at once (`OrionAppController.cpp:11494-11503`). One false lock on a white jersey or court line after a patch therefore resets the streak.
  6. The customer sees "NO METER DETECTED" plus "Check the Style and Color settings match your in-game meter" (`OrionAppController.h:1177-1181`, `MeterConfigPanel.qml:177-192`). On patch day that sends them into settings that are already correct, while the bot keeps releasing on a timer.
- **Affected:** `AutomationEngine.cpp` (backstop gate), `OrionAppController.cpp` (blind advisor), `meter_locator_cv.py`, `MeterConfigPanel.qml`.
- **Impact:** After a look change, every customer's meter-path shot is released blind. The bot looks alive and "shooting badly" rather than off. Blind releases do not feed the learners (`AutomationEngine.cpp:21413-21421`), so nothing self-corrects either. This hurts the customer and the refund/dispute rate.
- **Reproduction (high level):** replay a framedump with the meter masked out, or set a wrong meter colour on the rig. Take 4+ armed Square shots. The log shows `METER BLIND: 3 shots…` and then further backstop releases on later presses.
- **Required fix:** feed the blind state into the engine. While `meterBlindWarning_` is true, apply the backstop block reason `detection_unavailable`. Fail closed: the player's own release passes through untouched, exactly like `ORION_METER_BLIND_BACKSTOP=0`. Change the customer text to: "Meter detection unavailable. Venice is not timing your shots." Clear the state only after **2 owned shots** (a promoted, structure-stamped lock), not after one raw frame.
- **Verification test:** a QtTest that runs 3 armed epochs with no genuine detection, then checks that a 4th press produces no `emitShotGateRelease` from the backstop and a pass-through release. A second case checks that one raw detection does not re-enable the backstop, while two owned shots do.
- **Confidence:** confirmed (code-proven).

### [CL2-P9-002] high: a meter that is still detected but behaves differently gives silent wrong timing, and no guard compares observed speed or geometry with the tables
- **Lane:** P9
- **Failure path:**
  1. **Geometry is compiled in.** The CV proposer builds the box from two landmarks a **fixed 100 px @720p** apart (`meter_locator_cv.py:9-24`). The reader's fill denominator is that box (see memory `fill-denominator-is-the-detector-box`). A taller or shorter meter still produces a box with the old height, so every fill reading scales wrongly in one direction.
  2. **Meter time is compiled in.** The compiled values are:
     - `tipPhaseConstantMs = 393` and `tipPhaseSeedPhysicalMs = 319` (`AutomationEngine.h:1969, 2022`);
     - the convex curve table, measured over 20→100 = 385.5 ms (`AutomationEngine.h:6649-6667, 6685-6687`);
     - the onset buckets 500/580/775/915 ms (`BannerLeadTrim.h:262-265`);
     - the blind Δ table (CL2-P9-001);
     - the sampler slope ceiling of 0.21 %/ms + 0.001/pp (`AutomationEngine.h:4712-4725`).
  3. **The guards do not look at the meter.**
     - `curveModelApplicable()` only range-checks the engine's *own* effective constant (300-480 ms × scale) (`AutomationEngine.cpp:20967-20975`). It is not an observation of the game.
     - The rate stretch only lengthens (a slower meter), is capped at +40 % (`curveRateStretchMax 0.40`, `AutomationEngine.h:1561-1563`; `AutomationEngine.cpp:20778-20782`), and the rig pins its alpha to 0 (`AutomationEngine.h:1558-1560`).
     - A **faster** meter is not corrected at all. The phase path fires at the old meter-time and lands late; memory records "late = 100 % miss" on 2K27.
     - A faster meter also pushes low-fill sampler fits over the slope ceiling. Those candidates are refused, which drops the shot to the backstop (CL2-P9-001).
  4. **The one scale knob is unreachable for customers.** `ORION_METER_TIME_SCALE` is env-only (`AutomationEngine.cpp:20464-20477`). It is read only on the anchor-base20 regime transition (`AutomationEngine.cpp:2495-2499`), and `learning.json`'s meter-time constants must be rescaled by hand (`AutomationEngine.h:1457-1462`). There is no setting, no UI and no server path for it.
  5. **Green-window rules are compiled in.** The engine aims at fill = 100, at the window's late edge (`AutomationEngine.h:1960-1965`). If 2K moves or resizes the green window, the aim is wrong and nothing notices except the banner tally, which is presentation-only (CL2-P9-003).
- **Affected:** `AutomationEngine.h/.cpp`, `BannerLeadTrim.h`, `meter_locator_cv.py`, `simple_meter_reader.py` (colour bands and geometry priors, e.g. `simple_meter_reader.py:1083-1130`).
- **Impact:** the worst failure mode. The bot fires confidently on every shot with a systematic offset. The customer's make rate collapses with no message. The banner trim is clamped (`BannerLeadTrim.h:587`, ±maxMs, `kPersistCeilingMs 40`) and cannot absorb a large shift. The divergence guard freezes the learner at the clamp instead of alerting anyone (`AutomationEngine.cpp:18893-18910`).
- **Reproduction (high level):** replay a 2K27 framedump resampled in time by 0.85× (a faster meter) through the reader and engine fixtures. Fire times keep the old phase and land late.
- **Required fix (minimal sentinel):** the engine already fits every owned rise. Per session, keep a rolling median of the observed 20→tip time (or the 20→80 secant) over owned shots, and compare it with `curveOffsetFromBase20Ms(100) × meterTimeScale`. If the deviation is above ~12 % over ≥ 5 owned shots, or the tip-to-base box height deviates from its prior by more than ~10 %, set `meterModelMismatch`, which:
  - blocks autonomous releases, failing closed to pass-through;
  - shows "Meter changed, detection unavailable" to the customer;
  - logs one `METER MODEL MISMATCH` line with both numbers.

  Separately, make `meter_time_scale` a signed server-policy field (CL2-P9-004) so a pure speed change can be answered without a build.
- **Verification test:** QtTests that feed 5 owned rises at 0.85× and at 1.15× the table speed. Both trip the mismatch and the next shot produces no release. Rises at 1.0× ±5 % never trip it.
- **Confidence:** confirmed for the absence of a guard (code-proven). Probable for the late landing under a faster meter, which follows from the memory-validated "late = miss" rule.

### [CL2-P9-003] high: no fleet telemetry, so the owner learns about a break from Discord
- **Lane:** P9
- **Failure path:**
  1. The backend's only metrics endpoint is `handle_admin_metrics` (`backend/lambda_function.py:4706-4740`). It scans the **licence table** and reports licences, trials, activations, resets and versions.
  2. The route table (`backend/lambda_function.py:4956-5050`) has no client telemetry, shot or health endpoint. `native_orion/src/*.cpp` calls no such endpoint.
  3. The per-shot evidence is local only:
     - The banner verdict is "never … in the telemetry snapshot" (`OrionAppController.cpp:8327-8329`).
     - `detector_health` is "PRESENTATION ONLY" (`simple_meter_reader.py:8081-8084`, `autogreen_sidecar.py:832-839`).
     - `METER BLIND` and the backstop `SHOT NOT OWNED … backstop=` lines exist only in `%LOCALAPPDATA%` logs (`AutomationEngine.cpp:8608-8625`, `OrionAppController.cpp:11528-11532`).
- **Affected:** backend, `LicenseClient`, `OrionAppController`.
- **Impact:** after a patch, the first signal is customer complaints. The owner cannot tell whether a problem affects everyone or one user, and cannot confirm that a hotfix worked across the fleet.
- **Reproduction:** read the code. No request carries shot or detection data.
- **Required fix (minimal):** add a small **count-only** aggregate to the licence check/lease call the client already makes. Per session since the last call, send:
  - `armed_epochs`, `owned_locks`, `backstop_fires`, `blind_warnings`;
  - banner `exc`/`early`/`late`/`none` counts;
  - `mismatch_trips` (from CL2-P9-002);
  - `client_version`.

  Send no identifiers beyond the existing licence context; this is compatible with CX-019. The server rolls these into daily per-version totals in `/api/admin/metrics`. It raises an owner alert through the existing `alerts.events` mechanism (`lambda_function.py:3114`) when the fleet `owned_locks/armed_epochs` or EXCELLENT share drops by more than 30 % against the trailing 7-day value.
- **Verification test:** a backend fixture with two days of aggregates where the second day's lock rate drops by 50 % fires one alert, and a flat series fires none. A client unit test checks that the payload holds integers only.
- **Confidence:** confirmed.

### [CL2-P9-004] medium: the owner has no remote "detection safe mode", only a MOTD or the global kill
- **Lane:** P9
- **Failure path:** the server-side levers today are limited:
  - **MOTD** (`lambda_function.py:3103`, parsed at `LicenseClient.cpp:205-240`) is display-only.
  - **Global kill** returns 503 at activation (`lambda_function.py:627-629`) and takes the whole product down.
  - **`blocked_versions` / `min_client_version`** are evaluated only at activation/validate (`lambda_function.py:3535-3553, 632-638`).

  The proposer choice (`meter_proposer`, `AppConfig.cpp:1376-1377`; applied at `RemotePlaySession.cpp:2357`) is a local settings key with no UI and no server path. The backstop can only be disabled by env, and the meter-time scale likewise. Nothing lets the owner say "turn off autonomous release for everyone until the hotfix" while keeping Remote Play and the licence working.
- **Affected:** backend config (`CONFIG_DEFAULTS`, `lambda_function.py:3100-3115`), licence response, `OrionAppController`.
- **Impact:** in the hours between a patch and a hotfix, the owner can only post text or kill the product. Customers keep shooting badly, or lose everything.
- **Required fix:** add a `detection_policy` config key:
  - `autonomous`: `on` or `off`;
  - `backstop`: bool;
  - `proposer`: `cv` or `yolo`;
  - `meter_time_scale`: 0.5-3.0;
  - `message`.

  Return it inside the already-verified licence/lease response and apply it at the next check and at stream start. `off` must fail closed to pass-through, and an unknown or malformed value means "no change". Setting it goes through the existing audited `/api/admin/config` path, with a reason required (the CX-014 audit guard applies). A forged `off` is only a denial of service, and a forged `on` only restores the default, so the risk is bounded. Still route it only through authenticated responses.
- **Verification test:** a backend test that sets `autonomous=off`, then a client test that parses the response, checks no release fires and the customer banner shows `message`. A malformed policy leaves behaviour unchanged.
- **Confidence:** confirmed.

### [CL2-P9-005] medium: every detection fix is a full signed release with no canary; realistic lead time is 6-10 h at best
- **Lane:** P9
- **Path from a code change to customers:**

  | Step | Evidence | Realistic time |
  |---|---|---|
  | Diagnose on the patched game, capture a framedump (`ORION_FRAMEDUMP=1`, `remote_play_orchestrator.py:1438-1442`) and grade shots | tools exist, see CL2-P9-006 | 1-2 h |
  | Fix: constants or colour bands (A), YOLO relabel and retrain (B), timing rebaseline (C) | the 2K26→2K27 rebaseline took 08-27..09-03 (`AutomationEngine.h:1327-1392`) | A 2-4 h · B 1-2 days · C 2-4 days |
  | **Version bump** (required: the updater refuses non-newer versions; the version is compiled in, `CMakeLists.txt:3-4`, `OrionAppController.h:983`), so a model- or JSON-only fix still needs a **native rebuild** | | 0.25-0.5 h |
  | Nuitka sidecar rebuild; models are bundled and hash-checked (`scripts/build_orion_sidecar.ps1:27, 90-104, 175-181`; `tools/release_filter_policy.py:116-126`) | | ~0.5-1 h (estimate, not measured in repo) |
  | `verify_orion.ps1 -StrictSecurity` (owner step) | memory: ~20 min | 0.3-0.5 h |
  | `pack_lethe_release.py` assembly: Lethe pack, manifest, Ed25519 sign with owner passphrase, smokes, zip, update manifest (`docs/PACKING_RUNBOOK.md:25-60`) | clean pinned Lethe commit required | 0.5-1 h |
  | Clean-VM launch rig test, which is mandatory and blocking (`docs/PACKING_RUNBOOK.md` "startup/tamper smokes are an offline PROXY") | | 0.5-1 h |
  | Upload the zip, then `POST /api/update` with the offline-signed manifest (`lambda_function.py:975-1019`) | a single `record_id="current"` with **no ring and no canary** (`lambda_function.py:903`; `channel` is ignored) | 0.25 h |
  | Clients: check at startup plus **every 30 min**; auto-apply **only when idle**, deferred while streaming (`OrionAppController.cpp:2511, 2530-2539`); `mandatory:true` forces it at the next launch | | 0.5 h to end of session |

  **Totals from the patch landing:**
  - (A) constant or colour fix: **~6-10 h**;
  - (B) model retrain: **~1.5-2.5 days**;
  - (C) speed rebaseline: **~3-5 days**.

  The Codex review and release-integrity gate required by AGENTS.md add to each.
- **Impact:** customers run broken timing for hours to days. Without CL2-P9-001, -002 and -004 that means bad shots, not a safe "off".
- **Required fix:**
  1. Write `docs/PATCH_DAY_RUNBOOK.md` using the plan below.
  2. Keep a pinned clean Lethe commit, the signing passphrase procedure and a patched-game capture rig ready.
  3. Have the server honour `channel` (serve `beta`/`internal` manifests) so the hotfix goes to the owner and a few testers first.
  4. Longer term: allow a signed data-only overlay for `models/*` and reader constants that does not need a version bump. This is a L3 security decision and must go through Codex.
- **Verification test:** a dry-run drill on a no-op version bump, with the time from commit to published manifest measured and recorded.
- **Confidence:** confirmed for the pipeline. The per-step times are estimates.

### [CL2-P9-006] medium: the regression harness exists, but its reference corpus is missing, stale (2K26) and not backed up
- **Lane:** P9
- **Failure path:**
  1. `tools/regression/replay_gates.py:444-450` names five **July 2026 (2K26 Red/Arrow2)** sessions, and `gate_floors.json` holds 2K26 floors (red fill, peak ~90 %). `logs/diagnostics/framedump/` contains **none of them**; only `session_20260914_183310` (3,976 frames) and `session_20260914_204600` (2,097 frames) exist, about 6 GB of 2K27.
  2. `run_gates.py` correctly fails closed when no session is present ("a skipped replay is not a pass", `run_gates.py:180-183`). The pytest gate, however, **skips** (`tests/test_detection_regression.py:50, 76`), so the standard verify stays green with no detection gate at all.
  3. The two 2K27 dumps are not in `SESSIONS`, have no floors and are gitignored (`.gitignore:86`). No off-machine copy is documented.
  4. The tracked fixtures (`tests/fixtures/meter/arrow2_*`, `*_purple_*`) are 2K26-era. There is **no tracked 2K27 white-meter frame**.
  5. A known trap applies: framedumps can splice two runs (memory `framedump-session-is-two-runs-merged`), so both 09-14 dumps must be checked for a single mtime cluster before anyone trusts them.
- **Affected:** `tools/regression/*`, `tests/test_detection_regression.py`, `tools/diagnostics/replay_framedump.py`, `logs/diagnostics/framedump/`.
- **Impact:** on patch day there is no pre-patch 2K27 baseline to prove that a fix restores detection without breaking the old meter. A disk loss would remove the only 2K27 raw frames.
- **Required fix:**
  1. Verify both 09-14 dumps are single-run.
  2. Add them to `SESSIONS` and measure 2K27 floors.
  3. Copy them, plus `detframes_*.csv` and `avclock_*.csv`, to a second disk.
  4. Commit about 20 downscaled 2K27 fixture frames covering white Straight, Pill, low/mid/near-tip fill, a no-meter frame and a jersey false-lock frame, with a fast pytest over them.
  5. Record a fresh pre-patch baseline dump every week.
- **Verification test:** `run_gates.py` with defaults returns PASS on 2K27 sessions, and the pytest fixture set runs in CI without dumps.
- **Confidence:** confirmed (checked on disk).

### [CL2-P9-007] medium: the banner-verdict reader is template-bound and goes silent on a UI change with no health signal
- **Lane:** P9
- **Failure path:** verdicts come from NCC templates (`tools/timing/banner_templates/*.png`, `tools/timing/panel_templates.npz`, loaded via `banner_verdict_live.py`, created at `remote_play_orchestrator.py:7700-7705`). If 2K restyles the feedback panel:
  - no verdict is emitted;
  - the customer's "Shot: …" feed stops (`OrionAppController.cpp:8370-8375`);
  - the banner trim stops moving.

  None of this is reported. It is also the only local measure of timing quality, and the proposed fleet signal (CL2-P9-003) would depend on it.
- **Required fix:** count bot releases against attributed verdicts. After 10 releases with 0 verdicts, raise "shot feedback unreadable" (a log line plus a telemetry counter) and do not treat "no verdicts" as "no misses".
- **Verification test:** a sidecar unit test that runs 10 attributed releases with a blank panel and checks for one health event.
- **Confidence:** confirmed (code-proven).

### [CL2-P9-008] low: the customer-facing blind hint names the wrong cause on patch day
- **Lane:** P9
- `meterBlindHint()` always says "Check the Style and Color settings…" (`OrionAppController.h:1177-1181`). After a patch this is misleading and will produce support tickets.
- **Fix:** let `detection_policy.message` (CL2-P9-004) or the mismatch state (CL2-P9-002) override the hint.
- **Confidence:** confirmed.

---

## 2K patch-day plan (minimal, usable today)

**T+0, patch announced or detected.**
- Owner posts a MOTD at `warn` level: "NBA 2K27 update detected. Venice is verifying meter detection. If shots feel off, switch to manual until we confirm." MOTD works today (`/api/admin/config`).
- Do not run the global kill unless shots are damaging.

**T+0 to 1 h, measure** on the dev rig via `run_orion.local.ps1`, never the exe directly.
1. Launch with `ORION_FRAMEDUMP=1` (one run per directory).
2. Take 20 shots per style: Straight, then Pill.
3. Read, in order:
   - the Meter Detection card line (`detectorHealthLine`: locks/refused);
   - `METER BLIND` and `SHOT NOT OWNED … backstop=` lines;
   - the banner tally.
4. Grade the shots with `tools/timing/panel_grade.py`.
5. Replay the new dump through `tools/diagnostics/replay_framedump.py` against the 09-14 pre-patch dumps.

**Decide.**
- **Unchanged** (lock rate and EXCELLENT share within noise of the pre-patch baseline): clear the MOTD.
- **Look changed:** fix the proposer or reader constants, or try the YOLO route (`meter_proposer=yolo`) on the rig. If the look is new, relabel and retrain the detector. Gate with `run_gates.py` on the old 2K27 sessions plus the new dump.
- **Speed changed:** first try `ORION_METER_TIME_SCALE=k` on the rig, with `learning.json` meter-time constants scaled to match, and grade 30 shots. Then rebaseline properly.

**Ship.** Bump the version, then run in order:
1. native build;
2. `build_orion_sidecar.ps1`;
3. StrictSecurity;
4. `pack_lethe_release.py` (production key);
5. clean-VM launch;
6. Codex release gate;
7. upload;
8. `POST /api/update` with `mandatory:true`.

Update the MOTD to "Fixed in vX.Y.Z; restart Venice", and post the same in Discord.

**Until the fix is live:** with no remote safe mode today, the MOTD should tell customers to use manual shooting. Once CL2-P9-004 lands, set `detection_policy.autonomous=off` instead.

## Smallest code changes that make a break safe and visible (in order)

1. **Blind means off** (CL2-P9-001). Wire `meterBlindWarning_` into the backstop block reason (`detection_unavailable`) and require 2 owned shots to clear it. About 20 lines plus a QtTest.
2. **Meter-model mismatch sentinel** (CL2-P9-002). Compare the rolling observed rise time and box height with the compiled table. On a mismatch, fail closed to pass-through and show a customer banner. About 60 lines plus tests.
3. **Server `detection_policy`** (CL2-P9-004). Fields: autonomous on/off, backstop, proposer, meter_time_scale, message. Delivered in the verified licence response and set through the audited config route.
4. **Count-only fleet health** (CL2-P9-003), piggybacked on the lease/check call, with an owner alert on a more than 30 % fleet drop.
5. **2K27 corpus** (CL2-P9-006). Register the two 09-14 dumps, add floors, keep an off-machine copy and commit a small 2K27 fixture set.

**Verdict: needs changes.** A patch that changes the meter would today produce silent bad shots or blind timer shots for every customer. The owner would have no telemetry and no remote off-switch, and a fix would take 6 h to days to reach customers.

Top 5 fixes in priority order:
1. CL2-P9-001: blind means off.
2. CL2-P9-002: mismatch sentinel.
3. CL2-P9-004: remote `detection_policy`.
4. CL2-P9-003: count-only fleet telemetry and alert.
5. CL2-P9-006: 2K27 regression corpus and backup, plus the `PATCH_DAY_RUNBOOK.md` (CL2-P9-005).
