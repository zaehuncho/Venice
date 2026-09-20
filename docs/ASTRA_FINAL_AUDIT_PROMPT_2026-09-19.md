# Astra — final audit + correctness / implementation-bug check (Venice / Orion)

**Date handed off:** 2026-09-19. **Repo:** `C:\Users\aaron\Desktop\NexusVision`, branch
`fix/timing-input-and-remoteplay-blockers`. **Nothing is committed** — the whole session's work is in
the working tree. **Ship target: Monday 2026-09-22.** The owner (Isaiah) will test on the real rig
after this audit and report back.

You are doing **one combined pass**: an independent correctness audit AND an implementation-bug hunt
over everything below. Treat every claim in this document as a hypothesis to falsify, not as
settled fact. Where I state a measurement, the underlying data is named so you can re-derive it.
**I would rather you break one of my conclusions than confirm all of them.**

---

## 0. What the product is (so the audit has the right frame)

NBA 2K27 shot-timing assistant. Elgato 1080p60 capture card → Python sidecar (meter detection +
reading) → C++ / Qt6 engine (timing decision) → patched Chiaki fork ("OrionStream") → PS5 over the
network. The engine watches the shot meter fill and releases Square so the shot lands inside the
game's green window.

**Valid grading instruments — use only these:**
- The game's own feedback panel, read live by the sidecar: `BANNER VERDICT: timing=EXCELLENT|LATE|EARLY coverage=…`
- The retraction oracle: `RELEASE ORACLE: … gap_px=` — gap ≤ 3 px historically means green (27/27, then 35/36).
- Offline: `tools/timing/panel_grade.py`.
- **Never** grade with the engine's own self-assessment (`greenConfirmed`, `peak_fill`, `travel_pp`).
  `peak_fill` is saturated and information-free; `settled_fill` is post-retraction.

**Logs:** `logs/orion_native.log` (2026-09-18T18:29Z → 09-19T02:48Z) and `logs/orion_native.log.1`
(09-18T00:14Z → 18:29Z). They contain non-UTF8 bytes — always `grep -a`. Each app launch begins at a
`Sidecar env keys` line. **There is no engine log before 2026-09-18T00:14Z** (rotated away), so
Sept-17 and earlier sessions have sidecar-only fields and predate commits `b411cbd…e104e88`; do not
pool them for tuning.

**Frame dumps** (60 fps JPEG + `frames.csv` with the reader's per-frame `bbox_*` / `fill_pct`):
`D:\NexusVision\framedump\session_2026091{7_050219, 7_142445, 8_135725, 8_152024, 8_162245}`.
**Shot records:** `D:\NexusVision\shot_records\*.jsonl`.

**Hard rules while you work:**
- Never launch `OrionNative.exe` yourself; never force-kill it. The owner runs it via
  `run_orion.local.ps1 -NoElevate`.
- `native_orion/build` is the in-place build — do not build into it if the app is running
  (OrionCommon.dll locks). Use a throwaway short-path dir (e.g. `C:/Users/aaron/obld_a`) and delete it.
- Never edit `settings.json` or `learning.json`. If a setting must change, say so and let the owner do
  it; any edit requires `python tools/diagnostics/_resign_settings.py --write` afterwards.
- pytest: always `--basetemp=D:/NexusVision/pytest_tmp/<name>` (create the parent). The default temp
  dir is locked by another session.
- C: has ~16 GB free. Large outputs go to `D:\NexusVision\`.
- Do not commit or deploy anything. The owner says "commit" / "deploy" explicitly.

---

## 1. The owner's punch list — this is what the release is judged on

Verbatim intent, 2026-09-19:

1. **Standstills** hit more consistently **when wide open** and **on weird animations**.
2. **More consistency with fades.**
3. **Timing better with small green windows.**
4. **No bugs on the square path** (the long-standing "square is stuck, next press does nothing").
5. **Controller visibly moves the PC cursor / hovers over things** during a session.
6. **No disconnect bugs.**
7. **Instant connect.**
8. **An Xbox path for Xbox users.**

Earlier, still in force: "once those random lates are gone we're done — that's the ship blocker";
standstills near 100 % without earlies; mid-range fades should behave like standstills; 50-60 % on
three-point fades is acceptable; no late meter detection; no false locks. NO METER stays shelved.

---

## 2. Audit surface — everything changed in this session

**Engine (C++):** `native_orion/src/` → `AppConfig.{h,cpp}`, `AutomationEngine.{h,cpp}`,
`BannerLeadTrim.h`, `ControllerRoutingPolicy.h`, `OrionAppController.{h,cpp}`, `OrionTypes.h`,
`RemotePlaySession.{h,cpp}`.
**Sidecar (Python):** `banner_verdict_live.py`, `pill_fill_ruler.py` *(new)*, `player_anchor.py`,
`remote_play_client.py`, `remote_play_orchestrator.py`, `shot_range.py`, `simple_meter_reader.py`.
**Tooling:** `tools/timing/shot_pipeline_audit.py`.
**New/changed tests:** `tests/test_ownership_proof_detector_box.py`, `test_pill_fill_ruler.py`,
`test_player_anchor_acquire.py`, `test_remote_play_teardown_ordering.py`,
`test_remote_play_client_stale_sweep.py`, `test_square_path_press_census.py`,
`test_xbox_path_hazards.py`, `test_banner_verdict_live.py`, `test_ship_defaults.py`,
`test_shot_range.py`, `test_shot_pipeline_audit.py`; native `AutomationEngineTests.cpp`,
`ShotVerdictTallyTests.cpp`, `InputSessionRetryPolicyTests.cpp`, `SharedMemoryFrameReaderTests.cpp`.

**Supporting documents written this session (read these; they carry the evidence):**
`docs/GREEN_WINDOW_WIDTH_2026-09-19.md`, `docs/STANDSTILL_LATES_2026-09-19.md`,
`docs/SQUARE_PATH_AUDIT_2026-09-19.md`, `docs/CONTROLLER_AND_CONNECT_2026-09-19.md`,
`docs/XBOX_PATH_DESIGN_2026-09-19.md`, `docs/PLAYER_ANCHOR_ACQUISITION_2026-09-19.md`,
`docs/PILL_STYLE_STATUS.md`, plus the running record `docs/HANDOFF_2026-09-14_UI_POLISH.md`
(append-only; the 09-19 sections are at the end).

> **Warning about line anchors.** Several of these files were edited concurrently by multiple agents.
> Line numbers in the docs may have drifted by tens of lines and **the drift is not uniform**. Always
> re-locate by symbol or quoted text, never by line number alone.

---

## 3. Findings to falsify — ranked by how much rides on them

### 3.1 "Fades are not timed worse than standstills" (HIGH confidence, HIGH impact)
**Claim:** at **matched green-window width**, fades and standstills grade the same — 2.0-3.0 pp band:
68 % vs 67 %; 3.0-4.5 pp: 82 % vs 88 %. Landing precision is equal (oracle |miss| ≤ 3 px on 63 % vs
64 %). The fade window is ~40 % narrower (p50 2.17 vs 3.62 pp) and half of all fades fall in the
0-2.0 pp bucket where nothing scores well. **Therefore there is no fade-specific offset to add**, and
the only lever for fades is reduced |miss|.
**Data:** `docs/GREEN_WINDOW_WIDTH_2026-09-19.md`, 281 graded releases, 252 with a landing line.
**Attack it by:** checking my width-bucketing for selection bias (does window width correlate with
something else — distance, contest, tempo — that actually drives the verdict?); confirming
`green_start`/`green_end` from `Release landing` are trustworthy per-shot; and re-running the matched
comparison with a different bucketing. **Known weakness I already flagged:** widths ≥ 5 pp are partly
misreads (max observed 16.5 pp; one EXCELLENT landing reported `green=[71.5, 85.3]`), and the ≥ 4.5 pp
bucket grades *worse* than 3.0-4.5. If the wide tail is junk, some of the fade/standstill parity may be
too. **This is the single conclusion I most want checked.**

### 3.2 "The lates decompose into three classes" (HIGH confidence, HIGH impact)
Over 96 OPEN/WIDE-OPEN standstills (76 EXC / 18 LATE / 2 EARLY):

| class | n | LATE | P(LATE) |
|---|---|---|---|
| b — pickup stall (ownership proof) | 9 | 6 | 66.7 % |
| c — meter drawn ≥ 565 ms after press | 13 | 4 | 30.8 % |
| a — marginal, indistinguishable from EXC | 72 | 6 | 8.3 % |

Class b is mechanical and logged: `BOX LATCHED` → first `TIP RESERVATION` is normally ~3 ms but is
**95-132 ms** on these, because the ownership-proof chain restarts 4-7 times on `break_geometry`
(detector box width oscillating, e.g. 44 px ↔ 26 px, `iou=0.583 width_scale=0.591`). First accepted
proof frame lands at 34-40 % fill instead of ~17 %; `command_eta_ms` goes negative; the engine logs
`TIP DEADLINE DECISION: disposition=fired_late`. Censuses: `first_fill ≥ 32 %` → 7 LATE / 4 EXC /
0 EARLY (63.6 %) vs 20.9 % under 25 % (n=163); latch→reservation ≥ 100 ms → 8 LATE / 2 EXC (80 %) vs
18.9 % (n=164); `break_geometry` = 70 of 73 recorded breaks.
**Also established:** the only valid millisecond ruler is `TIP DEADLINE DECISION.lateness_ms` — under
~15 ms late still grades EXCELLENT, over ~25 ms is LATE 8/9, so **the usable late margin is ~20 ms**.
**Attack it by:** class b is only 9 open shots (17 overall). Re-derive the classification independently
and check whether class a is really irreducible (I found AUC ≈ 0.5 on `fill_at_rel`, `hold−onset`,
`fire_centre_delta_ms`, `predictor_sigma`, distance — if you find a feature that separates it, that is
the most valuable thing you could hand back).
**Doc:** `docs/STANDSTILL_LATES_2026-09-19.md`.

### 3.3 "Stuck Square = the merged press" (HIGH confidence, owner-visible bug)
`ShotIntentPolicy.h::updateSquareGesture` retires the Square latch early **only** when
`deliveredSquareReleasePending_` is set, and that bit is set **only** by
`noteOwnedSquareReleaseDelivered()` — i.e. only for a press the engine OWNED. `kReleaseSamples` = 3,
`kDeliveredSquareReleaseSamples` = 2, so an **unowned** press whose rebound inactive run is 2 polls can
never spend it: the latch survives, the re-press mints **no epoch**, and the console is told Square-down
continuously across both presses.
**Proven event:** epoch 187, `logs/orion_native.log.1` 03:54:13Z — released at +113 ms, re-pressed
~160 ms, no epoch, console held Square **297 ms**. This is the only mechanism in 582 presses that
reproduces the owner's symptom.
**Status: patch DRAFTED, NOT APPLIED** — see §5.
**Attack it by:** confirming the state machine really can reach that state, and that the proposed fix
cannot weaken the 3-poll debounce for owned shots (that debounce exists to stop a false-UP poll minting
a second epoch and erasing ownership proof — `ShotIntentPolicy.h:116-121`).

### 3.4 "The controller cursor leak is an external pad mapper" (MEDIUM-HIGH, partly unverified)
No `QKeyEvent` synthesis or `SendInput` exists anywhere in our repo, and QML has no controller
navigation. The mechanism is a PC-side mapper (Steam Input desktop config / DS4Windows / DualSenseX)
converting HID reports to synthetic mouse input. The pre-existing defence could not have worked:
pointer motion and wheel were never classified at all, and the guard armed only on a **button edge**
while a mapper drives the pointer from an **analog stick** (no edge).
**Fixed** in `ControllerRoutingPolicy.h` + `OrionAppController.{h,cpp}` using
`GetCurrentInputMessageSource` to discriminate injected vs hardware input, plus a level-triggered
stick guard. Env escapes: `ORION_CONTROLLER_UI_PASSTHROUGH=1`,
`ORION_CONTROLLER_UI_INJECTED_ISOLATION=0`.
**UNVERIFIED:** that the owner's specific mapper reports as injected. Both legs ship for that reason.
**Second contributor documented but NOT fixed:** on the ViGEm route our own virtual pad mirrors
gameplay to the desktop and a mapper maps that too; neutralising it there would kill input delivery.
**Attack it by:** checking the message-filter changes for correctness, and confirming the real mouse
is genuinely unaffected (a false positive here makes the app unusable).

### 3.6 Pickup stall — ROOT CAUSE FOUND AND FIXED (HIGH confidence, HIGH impact)
**The ownership-proof geometry gate was judging the DRAWN overlay rectangle, not the detector's
proposal.** `run_orion.local.ps1` sets `ORION_READER_BOX_TIGHT=2`, so `_tight_display_box` re-shapes
the outgoing bbox at the `detect()` boundary into a per-frame colour "hug" whose edges may sit up to
**18 px** outside the served box — re-derived every frame, so it appears and disappears on a
**stationary** meter. That drawn box was the only one on the wire.
**Arithmetic from the live log** (2026-09-18T23:00:53Z, epoch 72): reader
`BOX LATCHED box=[831,318,26,110]`; engine `Ownership geometry break: old_box=813,318,44,118
new_box=830,319,26,118 width_scale=0.591`. **831 − 18 = 813**, **26 + 18 = 44**, right edge invariant.
Same signature on epochs 58, 65, 33. `detframes_20260917_142730.csv`: `det_w = 26` on all 2,465
detections while published `w` ran 26-44. Over **20,887 consecutive detected pairs across 7 sessions**
the gate failed **28×** on the drawn rectangle and **2×** on the detector's.
**Fix:** carry both rectangles (`det_bbox` on the wire, `DetectionResult::detX..detHeight`), gate on
the detector's. **Nothing relaxed** — same IoU/dimension/aspect bounds, `max_proof_samples` untouched,
geometry break not disabled. No `det_bbox` ⇒ falls back to the drawn box ⇒ byte-identical, **so the
sidecar and engine must ship together or the fix is silently inert.** Kill switch
`ORION_OWNERSHIP_PROOF_DETECTOR_BOX=0`. Refusal preserved (a real detector shape change 26→60 still
breaks; `pill_locator_replay --cls neg`: 113 hard negatives, 0 proposals).
**Attack it by:** confirming the fallback really is byte-identical, and that no path can populate
`detWidth` with the hugged box. **Unverified:** no live A/B; the −5.2-of-18 conversion is a projection.
**Doc:** `docs/PICKUP_STALL_2026-09-19.md`.
**This also invalidated past offline validation** — `replay_with_presses.py`'s pinned `LIVE_ENV` never
set `ORION_READER_BOX_TIGHT`, so every past replay published the DETECTOR box while the product
published the hugged one. That is why offline said "0 diff" while the live logs were full of
`break_geometry`. Its `_png_for()` also matched only the retired `f%05d_?_raw.png` naming, so the
Sept-17/18 dumps loaded as "no decodable frames". Both fixed. **Before trusting ANY offline null in
this repo, confirm the harness env matches the launcher's.**

### 3.7 Pill landmark ruler — DONE, Arrow2 proven byte-identical (HIGH confidence, bonus feature)
The Pill proposer box carries ~16 px of pedestal+cap padding, so its box-relative ruler read
`7.5 + 0.88·true` and fired the 20 % phase anchor ~30 ms early. Replaced with a landmark ruler
(`pill_fill_ruler.py`, `fill = S·(base−top)/(base−apex)`, S = 96.0 = Arrow2's frozen `green_end`,
independently re-measured as p10 95.52 / p50 96.11 / p90 96.60 over 1745 frames).
**GT result:** slope **0.966** / intercept **−0.08** at 720p and 0.958 / 0.18 at 1080p, vs the shipped
box ruler's 0.888 / **+7.14**. **Arrow2 no-change proof (the ship gate): 3414 frames, 0 differ**,
verified against a pristine copy of the reader with the hook removed.
**Live dump:** frozen `green_end` p50 96.00 with **sd 0.000** (box: 95.54, sd 3.20); frozen-meter
ruler jitter sd 1.27 → **0.41**; 20 % crossing lands **+16.5 to +22.6 ms later**.
**Honest gap:** velocity IQR did NOT reach Arrow2's 0.003 (0.034 → 0.021); σ over the commit band is
**unchanged** (0.625 vs 0.648) — the box ruler's damage is on the plateau, not the rise.
**Note the agent falsified my own spec:** the "median-of-3 seed, relatch on >15 %" I specified FAILED
on real data (one bad cap frame re-latched at the wrong scale and held it for ten consecutive shots).
It ships instead with a rolling-window median, clipped caps barred from teaching the latch, and an
8-consecutive-sample relatch — all regression-tested. **Projection, not measured:** the later anchor
means the owner's Pill Shot Lead should go back UP toward the Arrow2 ~269.
**I fixed one ship bug from this lane:** `scripts/build_orion_sidecar.ps1` passes an explicit
`--include-module` list to Nuitka and the hook imports `pill_fill_ruler` inside a function with the
ImportError swallowed, so a compiled sidecar would have **silently** fallen back to the box ruler.
Added the include + a pin in `tests/test_sidecar_bundle_manifest.py`. (PowerShell note: that Nuitka
call is one backtick-continued statement — a `#` comment between its lines is a parse error.)

### 3.8 Player anchor — +6 locks, and the range classifier is DEAD on this court
**The brief's premise was stale and the agent said so:** the "2-34 % lock / PS corr 0.519" figure was
pre-`ORION_ANCHOR_ACQUIRE`. Re-measured on 145 presses across 5 dumps the anchor already locked
**90.3 %**. The fix takes it to **94.5 %** (98.6 % of presses with ground truth), `not_in_topk` 6 → 0.
**Root cause:** `_coarse_map` sized its template BY THE ROW, so a top-strip template is ~half a
bottom-strip one and TM_CCOEFF_NORMED runs high on anything small — crowd, stands, scoreboard and
every FAR player's small plate scored 0.62-0.82 while the owner's real plate scored 0.46-0.61 and fell
out of the GLOBAL top-16 despite being top-3 in its OWN strip. Fix = per-y-strip coarse peaks
(`ORION_ANCHOR_COARSE_PER_STRIP=1`).
**Ship gate:** confident false locks **0 of 766 junk boxes, unchanged** from baseline.
**The agent self-corrected after handing back** — worth reading as a model of what I want from you:
it had attributed a number to the wrong knob, re-ran the full 2×2, and reported that the shipped
default reaches refuse-confidence on **451 frames vs 1090** before on the 09-12 park corpus
(containment 36.8 % → 32.9 %). Safe-direction for false locks (fewer vetoes cannot create one) but it
**weakens the anchor's veto on that court**, and is a plausible regression mechanism where the owner's
plate is the strongest peak with near-neighbours in its own strip. On the five dumps this lane targets
it moves the other way. **`ORION_ANCHOR_COARSE_PER_STRIP=0` is the one-knob revert to the 09-17
search — put it in the ship notes.** Second knob `ORION_ANCHOR_TRACK_ID_MISS` ships **OFF** (bought
zero locks, was the only arm where confident false locks moved 0→2).
**`shot_range` still emits `unknown`, and this is now PROVEN, not a gap:** on 102 presses carrying both
`range_cells` and `banner.distance_ft`, **mid precision and recall are 0.000**; the 77.5 % "agreement"
is an 88:14 class imbalance. Plate cuts at 17.2 / 19.7 / 20.1 / 21.1 / 21.2 ft show the "3" cell
**plainly drawn**, so on this court it is not a behind-the-arc flag at the press. Arming it would ship
a constant `three` into `lead_offset_fade_mid_ms`. **Do not arm it.**
**Pickup barely moved:** first-sight fill and latency p50 are FLAT; what improved is patch containment.
**Doc:** `docs/PLAYER_ANCHOR_ACQUISITION_2026-09-19.md`.

### 3.5 Connect timing (MEDIUM, arithmetic not measurement)
Measured press→Running on the current build: **median 1324 ms** (min 1067, max 3519, n=10). Stages:
native prep 264 ms (152 of it Win32 window containment), standby claim 150, standby promote 304,
console handshake 494, `host_resolve` 0 ms in 8/10 but **2422-2440 ms in 2/10** (a prewarm TTL race).
Two fixes applied: refresh the console-host cache **before** it expires, and move the 152 ms window
containment to **after** the promotion command. **Expected ~1150 ms median with the 3.5 s tail gone —
this is arithmetic over measured stage times, not a new measurement. Please verify the reasoning.**
**Rest mode has ZERO samples in any log** — the wake path is unmeasured and was deliberately not
touched. **Biggest untouched number:** app-boot → first frame is **7163 ms median, of which 4071 ms is
the capture-card open**, completely uninstrumented. That is likely what the owner experiences as "not
instant". Worth your attention.

---

## 4. Decisions I made that deserve a second opinion

1. **The lead-trim bias integrator ships DISABLED** (`banner_trim_bias_votes = 0`).
   It was built and fully tested (net `#LATE − #EARLY ≥ votes` in a sliding window buys one ±3 ms
   step). I turned it off because the forensics refuted its premise: a kernel fit over the clean onset
   band (500-535 ms, n=202) puts the LATE/EARLY crossing at hold **641 ms** against a population median
   of **646** — the lead is already within ~5 ms of optimum — and shifting 3/6/9 ms trades 2.7/5.2/7.4
   LATEs for 2.6/5.6/9.1 EARLIEs (net ≈ 0; optimum +2 ms, worth 0.1 pp). The 18:2 imbalance comes from
   classes a lead cannot fix. Left armed it would spend its whole ±15 ms clamp turning EXCELLENTs into
   EARLIEs. **Check the kernel fit and the net-trade arithmetic.** The OPEN-only slice hints at +18 ms
   but has only 5 EARLYs in 148 shots (unestimable) — if you can estimate it properly, do.
2. **A feedback panel with no coverage cell now calibrates the trim** (`banner_trim_absent_coverage_open
   = true`). The 2-cell `TIMING | DISTANCE` layout (drills, no defender context) was **98 of 281**
   verdicts and a 09-18 change was excluding all of them, starving the trim. The sidecar now sends
   `has_coverage`; absent ⇒ treated as open; a present-but-unreadable cell stays excluded.
   **Check:** is "no coverage cell" really equivalent to "open"? That is the load-bearing assumption.
3. **settings.json drift restored** to validated ship values: never-seen probe 100 → **0**, vision hold
   band 40/60 → **0/0**, aim margin 62 → **69**. Lead slider left at 274 (owner's). Re-signed.
4. **`tools/timing/shot_pipeline_audit.py` completed** — it shipped with 4 tests asserting fields it
   never emitted. I implemented `release_green_confirmed`, `release_green_window_pct`,
   `first_valid_tip_evaluation`, `late_decision`, `ownership_proof` and the count
   `reported_green_open_late_epochs`. 17/17 pass. **Check the semantics**, especially that absent vs
   repeated evidence yields `None` rather than a wrong value.

---

## 5. Pending / unapplied — please review before or as you apply

1. **Square merged-press fix — I ATTEMPTED 3a AND REVERTED IT. Read the addendum before retrying.**
   Full reasoning in `docs/SQUARE_PATH_AUDIT_2026-09-19.md` §ADDENDUM. Short version: 3a gates the
   relaxed latch-retire on "the engine does not own a shot", but **ownership proof ACCUMULATES BEFORE
   the engine owns anything** — that is the point of the proof. So the gate reads "unowned" during
   exactly the window where a spurious epoch destroys the evidence being gathered, which is the
   pathology §3.6 just spent a day removing. It also breaks the deliberate regression at
   `AutomationEngineTests.cpp` ("Ordinary input keeps the established three-UP lifetime"), which
   *encodes* that protection rather than incidentally covering it. Cost/benefit: the merged press is
   **1 proven event in 582 presses (0.17 %)**; the risk is a new route into an 8.5 % failure class.
   **Recommended path is now 3b** (cap the unowned OUTPUT hold: never touches edge minting, measured
   need 184 ms against a median of 8 ms, a 20 ms cap would have cost the corpus nothing), or a 3a
   gated on a real "no ownership proof is accumulating" predicate, which does not exist today.
   **Owner's call which — this is his oldest reported bug, so do not silently drop it.**
2. **`backstop=` reason token is lying** — `meterBlindBackstopBlockReason`
   (`AutomationEngine.cpp`, ~L20937) never tests `config_.greenWindowPriority`, which is what actually
   short-circuits the backstop (~L21401). So **280 of 281** abort lines report `backstop=user_released`,
   including 1361 ms and 1039 ms holds whose own `METER VISION WAIT … blind_release_suppressed=1` line
   proves otherwise. Zero-risk token fix; patch in the audit doc; `test_i5` is checked in as an xfail
   against it. **Some past diagnosis was reading a wrong label — worth knowing.**
3. **`meter_blind_backstop` is dead code** — **0 fires in 582 presses** against 26 `METER VISION WAIT`
   suppressions. `green_window_priority=true` and `meter_blind_backstop=true` ship together and
   contradict each other; three grace/probe knobs tune nothing. Recommendation is **retire it honestly**
   (no gameplay change) rather than re-arm it — blind releases graded 3 EXC / 3 EARLY vs vision's
   15 EXC / 8 EARLY / 0 LATE. **This is an owner decision, not an audit call.**
4. **ViGEm pad stays plugged after an unexpected sidecar exit.** Unplugging on every exit breaks the
   restart path; the right fix is unplug-on-exhaustion. **Owner decision.**
5. **The three lanes that were in flight are now LANDED, built and green — audit them as findings,
   not as promises.** Each is summarised in §3.5-3.7 below.
6. **Housekeeping:** `native_orion/tests/InputSessionRetryPolicyTests.cpp` is **untracked in git** even
   though `native_orion/CMakeLists.txt:750` builds it. Test fixture `.log` files were being swallowed
   by the global `*.log` gitignore and were renamed `.logtxt`.

---

## 6. Refuted — do NOT re-propose these

- **"Stuck Square is an R2 release coincident with the press."** Dead. Across 582 presses, presses with
  an R2 release edge in the window were owned at **63.2 %** vs a **49.1 %** baseline — *more* likely to
  fire. `sprint_released=1` on **0 of 582**. `sprint_release_on_square` stays false and fenced for
  hygiene, not because it causes anything.
- **"48 % of presses do nothing."** An artifact of counting taps as shots: **254 of 281** unanswered
  presses held < 300 ms (median 98.9 ms) and the meter's first sight is 500-850 ms after a press. The
  honest unanswered-shot rate is **22/582 = 3.8 %**, and 19 of those had `PICKUP first_sight_fill=-1`
  (the reader proposed nothing at all).
- **A global lead shift, or a hold cap.** Net ≈ 0 shots (§4.1); a 700 ms hold cap trades 6 LATEs for
  **7** EXCELLENTs.
- **Extending `FadePhaseCatchup` to standstills.** Its discrete-jump premise is absent from the frame
  data: max excess progression 1.4-6.0 ms against its own 20.8-41.7 ms threshold, 0 of 16 rises. It is
  fade-only, env-gated, and **fired 0 times in every Sept-18 session**.
- **The poll-phase tracker.** Refuted on 284 shots (verdict independent of release phase, p = 0.73).
- **The oracle gap as a signed millisecond ruler.** Within EXCELLENT it does not move with our command
  (slope +0.0047 px/ms, r = 0.048, n = 238). It is a |miss| **magnitude** — EARLY shots also sit at
  large gaps.
- **Relaxing the ownership proof to catch a single late pickup**, or raising `max_proof_samples` /
  disabling the geometry break to fix the pickup stall. The shape gate is the false-lock defence and
  false locks were the original abort blocker. Fix the stall at its cause instead.

---

## 7. What I want from you

**A. Correctness + implementation-bug pass over §2's audit surface.** Specifically hunt for: state
machines that can reach a stuck state, races between the press epoch / shot gate / release, generation
or identity tokens that are checked in one path and not a sibling, resources (ViGEm pad, capture-card
handle, Chiaki session) that can leak on an error path, and any place a guard was added for one input
class but the symptom lives in another (that was exactly the controller-leak bug).

**B. Falsify §3**, starting with 3.1 (the fade/window conclusion) and 3.2 (class a being irreducible).

**C. Review §4's four decisions** and tell me plainly if any is wrong.

**D. Apply §5.1 and §5.2** if you agree with them, with tests. Leave §5.3 and §5.4 for the owner.

**E. Build the Xbox path** if you have room — design is in `docs/XBOX_PATH_DESIGN_2026-09-19.md`.
Architecture is owner-confirmed: **ride Microsoft's own Xbox Remote Play app** — capture its window
(Windows.Graphics.Capture, **not** `PrintWindow`/`BitBlt`) and inject via a **ViGEm X360** virtual pad
with **HidHide** hiding the physical one. We never touch Microsoft's protocol, so there is no transport
problem. Phases: (1) Xbox *controller* on the existing PS5 rig, 3-5 days, low risk — the internal
button mask is already XInput (`square() == XINPUT_GAMEPAD_X`); (2) **the timing bench is THE GATE**,
~1 week, and **day one is the black-frame check** — if the Remote Play window is DRM-flagged, WGC
returns black and the route is dead; (3) Xbox mode, 3-5 weeks, where the only genuinely new C++
component is HidHide (port `virtual_controller.py:297-373`). `remotePlayConsole = "Xbox"` today is a
**stub** (saves a string, referenced nowhere) — do not budget it as partial work.
**The timing bench can be run on the existing PS5 rig for free** by pointing it at Sony's official PS
Remote Play app instead of Chiaki: same architecture (capture a streaming app's window + inject a
virtual pad), and it measures the added latency **variance** that actually matters. The engine
subtracts a learned lead, so a constant offset is survivable; **sigma is not**. Baseline to beat:
Elgato loop **213.7 ms median** (206-244, n=277), 60.0 fps / 16.667 ms cadence, frame-phase lock
**sd 0.05 ms**. Remote Play inserts an encoder, a network hop and a jitter buffer that deliberately
**re-times frames**, plus a compositor hop — none of it ever measured here.

**F. Verification before you hand back:** full native suite via `ctest -C Release` from
`native_orion/build` (**note:** the test executables print nothing when run directly from a shell —
that is a console quirk, exit code 0, not a failure; use ctest), plus the Python suites with
`--basetemp` as above. Report counts and name every pre-existing failure you did not cause.

**The tree you are inheriting is GREEN, measured 2026-09-19 after every lane landed:**
- in-place Release build clean; `ctest -C Release` → **22/22 targets pass**
- `python -m pytest tests -q --ignore=tests/discord` → **3228 passed, 19 skipped, 8 xfailed, 2 xpassed**
- `tests/discord` needs the `discord` module, which is only in `.venv` — run those with
  `.venv/Scripts/python.exe` or ignore them; that is environmental, not a regression.
- **Known load-sensitive:** `tests/test_idle_scan_cost.py::test_the_burst_that_stalls_the_consumer_on_the_full_compare_does_not_on_the_strided_one`
  fails under heavy parallel load and passes alone (verified 3×). It is a wall-clock perf assertion.
  If you see it red, check your machine load before investigating.
If anything above is red when you start, it is a merge artefact — say so rather than working around it.

**G. Be explicit about what you could not verify without the rig.** The owner tests next and needs a
short, concrete list of what to look for. Current open items needing the live rig: a mapped
pointer/click/wheel no longer reaching Venice's controls while the real mouse still works; pressing
Disconnect **during** an input recovery (console must not report a cable/LAN error and the preview must
resume); holding Square while pressing Disconnect (no shot may be owned after the click); a dozen
connects spread over several minutes (expect ~1.15 s, no 3.5 s outliers); and **one rest-mode
connect**, which has never been sampled.

**Do not** weaken the false-lock defences, reintroduce a lead term on a press-anchored path, un-shelve
NO METER, change `settings.json`, or commit/deploy. If you disagree with a decision here, say so with
the measurement that beats it.
