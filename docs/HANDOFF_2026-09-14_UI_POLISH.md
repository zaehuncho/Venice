# UI polish + first-connect batch — handoff (2026-09-14, ~14:20-15:00 local)

Owner request (verbatim intent): faster first Chiaki connection; remove the "Timing: Route
unavailable" pill; remove the Overview tab; remove the "PREVIEW ONLY — BOT + CONTROLLER OFF" badge;
"Enable Bot + Controller" → "Connect"; meter detector overlay → blue; refresh-rate section under the
capture-card slot; cleaner Setup tab; a Rhythm slider (flick earlier/later) when Rhythm is on; modest
production-ready revamp; remove the "VENICE / Precision timing" logo block; fix bugs found; then
improve the remote-play-only path. Built by three Opus 5 agents (QML / engine+controller / connect
path) + one read-only survey agent. Nothing committed, nothing deployed.

## What changed

**Launcher (QML)** — `native_orion/qml/**`
- Sidebar: brand block gone, Overview tab gone (Live / Setup / Updates; Debug only in dev builds).
  `GeneralPage.qml` deleted (and removed from `CMakeLists.txt`); AppShell no longer routes "general"
  (a persisted `current_page=general` falls through to Live).
- Live page: preview-only badge removed; `liveTimingStatus` pill removed; CTA is
  `Connect` / `Connecting…` / `Connected`; placeholder "Press Connect to start".
- Setup page: the "Production Setup" pill strip (incl. `passiveTimingStatus`) removed; order is
  Connection → Controller lightbar → Profiles → **Account** (license state, time left, masked key +
  Copy, version — moved from the old Overview). `StreamSetupForm` regrouped Console → Video source
  (device + **Refresh rate** 30/60/120 Hz, `objectName captureFpsCombo`, capture-card only) → Stream
  quality.
- Rhythm card: **Flick timing** slider (`objectName rhythmFlickSlider`, 1..100, 50 = default,
  Earlier ← → Later, readout "+12 ms later"), visible only when Rhythm is on.
- Theme: `meterLock #FF2BD6 → #1E90FF`, `meterLockKeyline → #06182F`.
- Six latent QML bugs fixed on the way (combos that lost their binding after the first pick, a card
  reaching into another card's width, shrinking text fields, stale tour copy, a NaN guard on the new
  slider, a quality-model lookup that only worked by parenting accident).

**Engine / controller (C++)** — `native_orion/src/**`
- `rhythm_flick_delay_ms` (double, −50..+50, default 0; `orion.rhythmFlickDelayMs`). Positive = the
  stick flick fires later. Applied as `lead − delay` (floored at 0) inside
  `AutomationEngine::measuredLeadForActuationMs()` ONLY when the release will be a TempoSquare flick
  (live shot mode → pending Square remap latch → global `tempoEnabled || tempoRemapEnabled`), so
  every fence (reservation, deadline, unschedulable-lead abort, lead-conflict surfaces) sees the same
  number. Logged as `rhythm_delay=` on the TIP DEADLINE DECISION line and `rhythm_delay_ms=` on
  `Input timer:`.
- `capture_card_fps` (int, snapped to {30,60,120}, default 60; `orion.captureCardFps`) →
  `ORION_CAPTURE_FPS` in the sidecar environment (capture-card branch only) →
  `CaptureCardBackend(fps=…)`; requested vs negotiated logged (`Frame source: capture card index=N
  fps=M (requested)`, `cap_req_fps=` on the health line). The SUSPECT black/static run thresholds
  were a hidden 60 fps assumption and now derive from the requested rate (byte-identical at 60).
- `kMeterOverlayDefaultColor = "#1E90FF"` (loader already pins every persisted colour to the default).
- "press Enable Bot + Controller" wording in `RemotePlaySession.cpp` → "press Connect".

**Connect path (Python)** — `remote_play_client.py`, `remote_play_orchestrator.py`
- ROOT CAUSE of the 3.0-3.4 s first connect: `stale_sweep_ms` was a mislabelled bucket. The time was
  `_promote_standby → _resolve_console_host` paying socket timeouts (0.4 + 0.8 + 1.2 s) because the
  configured console address was silent (DHCP drift .126 → .81); every slow connect is preceded by a
  `CONSOLE ADDRESS DRIFT` line inside its own window. The real sweep is ~12 ms. Toolhelp truncation
  hypothesis REFUTED (argtypes declared anyway).
- Fixes: true stage attribution (`standby_attempt_ms`, `host_resolve_ms`); in-process PID kill
  (OpenProcess/TerminateProcess/WaitForSingleObject, 4.4 ms vs ~250 ms per taskkill) with the
  spawning sweep kept as last resort; sweep report `sweep_found=/sweep_killed=/sweep_fallback=`;
  `prewarm_console_host()` (daemon thread, single-flight, 120 s TTL) ticked from the standby loop on
  EVERY pass (orchestrator patched so a reconnect without a stop also benefits); standby promote now
  RETARGETS to the adopted address (the fork's `open <host>` protocol already supported it) instead
  of discarding a fully-booted client.
- Expected first connect: `stale_sweep` ~10-12 ms, `host_resolve` ~0, total ~1.0-1.4 s with the
  standby promotable (was 4.6-5.3 s). Settings: `remote_play_console_ip` set to .81 and re-signed;
  the durable fix is a static IP on the PS5 (manual 192.168.137.81 / gateway 192.168.137.1).

## Verification
- OrionNativeTests **810 / 0 / 6 skipped** (+3 new); OrionPreviewPresentationTests 50/0;
  OrionMeterDelaySettingsTests 16/0. OrionNative built clean (14:51), launched 14:55 via
  `run_orion.local.ps1 -NoElevate`: 0 QML warnings, 0 sidecar errors; log shows
  `Frame source: capture card index=1 fps=60 (requested)` and `rhythm_delay_ms=0.0`.
- Python: targeted run 200 passed / 1 skipped (stale-sweep 11 new, capture-fps 24 new, UI contracts,
  frame-pipe copy test). Broader `tests/` (minus backend/discord): 2403 passed, 8 failed — ALL
  pre-existing or foreign: `test_nexus_svc_delay` ×2 and `test_release_filter_hygiene` fail on a clean
  HEAD checkout too; `test_latency_estimator::test_probe_ignores_sub_fmin_onset_samples` pins the
  pre-09-03 tip-registration prior (`models/tip_registration.json` was rebuilt 09-03);
  `test_native_qtest_diagnostics.py` is an UNTRACKED file from the other Claude session (its subject
  `verify_orion.ps1` is not at the repo root); `test_decoder_pipe_identity` / `test_capture_card_backend`
  `PermissionError` on `%TEMP%\pytest-of-aaron` = the other session holds that dir (use `--basetemp`).

## Tip timing — answer to "how do customers find their lead"
The press→tip constant is the GAME's animation and is already universal: it ships frozen in
`learning.json` / `models/tip_registration.json` (see memory `aim-constant-drift-tip-timing-card`),
so no customer needs a Tip Timing card. The per-rig part is **Shot Lead** (TV/capture/USB latency):
default 272 (graded zero-offset on this rig), tuned by the card's own EARLY/LATE hint against the
game's TIMING banner. A "universal" mechanism = auto-tune Shot Lead from the banner read live by the
sidecar (the validated `panel_grade.py` template matcher) — proposed, not built.

## Remote-play-only path — survey findings (read-only, to act on next)
- Every source is normalised to 1280×720 before the reader (`_detector_contract_size`), so the CV
  locator's pixel gates are numerically identical on both routes; what differs is compression.
  The shape gate's `edge sd ≤ 1.2 px` and the 3-px green-tip count have no measured compression margin.
- Decoder frames have NO CadenceLock (raw decode-callback QPC, 50 ms freshness guard). Today's live
  decoder window (19:04-19:09Z) shows lower `frame_age` sd (2.75 vs 5.30 ms) — it may not need one.
- The latency posterior/factory prior is route-scoped; **Shot Lead is NOT** — switching video source
  carries the capture-card lead across. The 18 accepted decoder labels today average ~33 ms lower
  than capture-card (n=18, one sitting, preset changed mid-window → a hypothesis, not a finding).
- The decoder route's make rate has NEVER been graded with the banner grader; there is no decoder
  framedump corpus on this machine (framedump dir empty; archives hold CSVs only).
- `ORION_COMPRESSED_READER` is still unreachable AND its chroma path is red-meter specific → delete
  the flag/module rather than enable it. The decoder zero-copy Y plane is computed and discarded every
  frame (`set_native_y` exists only on the compressed reader) — pure deletion.
- Nuitka bundling of `chiaki_backend`: REFUTED as a risk (the 08-08 built `OrionSidecar.exe` contains
  it; the 08-04 audit's own §530 says the same). BUT `meter_locator_cv.py` and
  `tools/timing/panel_grade.py` are still UNTRACKED — `git add` them before any package/commit.
- DONE 15:20: the decoder Y-plane normalisation is now skipped unless the live reader has
  `set_native_y` (`remote_play_orchestrator.py`, marker ORION_DECODER_YPLANE); 129 frame-pipe /
  integrity / source-transition tests pass. IN PROGRESS: an offline re-encode study (H.264 720p 4/12
  Mbps, 1080p 12 Mbps) of the CV locator + shape gate over the 201355 corpus (tools/quality +
  logs/diagnostics/reencode_study) and a per-video-source Shot Lead stash (`actuation_lead_by_source`).
- Ranked next steps: (1) route-scope Shot Lead or at least warn on source switch; (2) drop the dead
  Y-plane work; (3) rebuild `models/latency_factory_prior.json` with the decoder labels (check the
  builder's per-label routing first); (4) owner session on the decoder route with `-Framedump`, graded
  by `panel_grade.py`, interleaving sources in ONE sitting; (5) decide/delete the compressed reader.

## Still owed (unchanged)
Admin V2 decisions + deploy order (`docs/HANDOFF_2026-09-14_ADMIN_PANEL_V2.md`), commit (everything
uncommitted; several files untracked), purge the Helios bypass scripts from the repo root, pad USB
power fix (`tools\diagnostics\fix_dualsense_usb_power.ps1`), red-team before the trial link.

## Evening additions (owner asks 2, 3, 4: far shots, engine polish, Profile, NO METER v2)
- **Far shots (measured, `logs/diagnostics/farshot_study/`):** owner's own shots seen 34/37; blind
  banner rows were team-mates' shots (no meter drawn). The meter never shrinks with distance; the real
  blind spot is box top ≤ 44 px (scan starts at row 57; tip/spill windows clamp). Knob
  `ORION_METER_TOP_STRIP` (default 0) recovers 61/80 real meters shifted into that zone with 0/12,000
  frames changed and no cost — FLIP THE DEFAULT to 1.0 (`meter_locator_cv.py` ~line 180) after the
  compression study finishes. Tests 66/66. Gate 9 refusals in miss windows were all junk: keep it.
- **NO METER root cause (measured on 487 graded meter-path shots, `tools/timing/nometer_prior_audit.py`,
  design `docs/NO_METER_V2_DESIGN.md`):** the `− lead` term was the whole bug. Console hold = PC hold
  (same pipe both ways; latency cancels). Vision-path holds: Standstill 651, Left Fade 927, Right Fade
  955 (Rhythm +27..+56 → +40); the learned prior already equals the hold (+6..+14). NO METER held 398
  → ~256 ms early; the 390 floor blocked nothing. v2 = `hold = max(450, H_ref + Δ(type) + R)`,
  `H_ref = 500 + 3·(slider−1)` (default 51 = 650), Δ LF +276 / RF +304, drop `input_timed_lead_ms`
  and `input_timed_delay_ms`. Blind-release ceiling ≈ 23 % green on Standstill (animation variance).
- **Press-anchored fallback on the meter path** (`press_anchored_fallback_enabled`, default on; env
  `ORION_PRESS_ANCHORED_FALLBACK`): fires when no meter is accepted by the moment, vision-first by
  construction (Idle-state site), single-fire, learner-excluded. Being reworked to the no-lead law
  (press + prior + bias, floor 450, Rhythm +40; `press_anchored_fallback_bias_ms`).
- **In progress:** Profile page (Discord ID, plan, days left, HWID resets) + activity-feed hygiene
  (engineering telemetry out of the customer ring; the `Network bridge … Access is denied` spam);
  per-video-source Shot Lead stash DONE (`actuation_lead_by_source`, 813/0/6); NO METER v2
  implementation queued behind the fallback + Profile agents (shared files).
- Owner facts: meter OFF widens the window; competitors' meter was ON (press-timed); ship target
  = weekend Sept 19-20.
- Build note: while the app runs, `native_orion/build` cannot link (OrionCommon.dll locked) —
  C++ agents build in a throwaway short-path dir (e.g. `C:/Users/aaron/obld_x`) and delete it.

### Profile page + activity-feed hygiene — DONE (Opus 5, ~18:30 local)
- Backend: `profile` object on `/api/activate` and `/api/license/check` (`discord_user_id`,
  `discord_username` (always "" today — no mint path stores one), `plan`, `expiry` (0 = lifetime),
  `activated_at`, `hwid_resets{used,free_total,free_remaining,paid_credits}`), read-only projection,
  no new attributes; `tests/backend` 230/230. NOT deployed.
- Launcher: `LicenseProfile` parse (tolerant of the live Lambda lacking it); Q_PROPERTYs `profileKnown`,
  `profileDiscordId/Name`, `profilePlan`, `profileExpiryEpochS`, `profileDaysLeft` (−1 unknown, −2
  lifetime), `profileLifetime`, `profileActivatedEpochS`, `profileHwidResets*`, `machineIdMasked`,
  `copyProfileDiscordId()`; `timeLeft` now derives from the same days-left policy. New
  `qml/pages/ProfilePage.qml` + sidebar "Profile" (Live / Setup / Profile / Updates); the Account card
  left Setup.
- Activity ring rule in ONE place: `native_orion/src/UiNotificationPolicy.h::shouldEnterActivityRing()`
  (allow-list → deny-list → `Sidecar:` prefix → ≥3 key=value pairs → keep); `appendCustomerEvent()`
  routes the 7 plain-language shot notices; raw `Release issued:` / `Shot automation aborted:` lines
  unchanged on disk. `OrionActivityFeedPolicyTests` 10/10.
- `Network bridge … Access is denied` spam: `NetworkBridge` emits `connectionChanged` on EVERY service
  `error` (WinDivert needs elevation); the controller slot now logs state CHANGES only. Not-polling-
  when-shelved still owed (predicate `networkEnabled || meterDelayEnabled` in AppConfig).
- Telemetry lines (`Telemetry stage split`, `preview_pipeline`, `SHM preview frame read`,
  `qml_preview_pipeline`) once per 60 s unless a health threshold trips.
- Suites: MeterDelaySettings 18/0, PreviewPresentation 52/0, OrionNative builds; Python 2504 passed /
  7 pre-existing failures (same set as before).

### NO METER v2 + fallback law fix — IN PROGRESS (fresh agent owns AutomationEngine/AppConfig/
OrionAppController/QML): one hold law `max(450, H_ref + Δ(type) + R)`, `no_meter_hold_ms` 500..800
default 650, Δ table LF +276 / RF +304 (learned override from vision-path holds when n ≥ 8), R +40 for
Rhythm; NO METER re-enabled in the shipped UI (mode switch + one "Release timing" slider); the
press-anchored fallback (today's `prior − lead` version fires ~256 ms early — the orchestrator sent its
corrections to the wrong agent) is being re-pointed at the same function.

### Remote-play route detection — DONE from the compression study (~19:30 local)
- Study (`tools/quality/reencode_gate_study.py` / `_sweep.py` / `_report.py`, outputs
  `logs/diagnostics/reencode_study/`; x264 proxy for the PS5 encoder, chiaki's BT.709 matrix, 283
  true-meter + 1,213 adversarial frames): shape gate keeps 3-10x margin under every rung, 0 false
  locks, box displacement 0-2 px, reader fill error p90 ≤ 1 pp, tip-frame timing 0 frames. The ONLY
  binding gate is the green tip's HSV SATURATION floor (90): 4:2:0 chroma washes the small triangle
  (even qp=1 loses 5 pp). First-sight locks: 720p/4M 83-89 %, 720p/12M 89-91 %, 1080p/12M 93-97 %.
- Applied: `ORION_CV_GREEN_S_MIN` knob in `meter_locator_cv.py` (default 90 = capture card,
  byte-identical); the sidecar (`autogreen_sidecar.py`, marker ORION_DECODER_TIP_MARGINS) sets 60 and
  `ORION_CV_TIP_PX_MIN=2` on the decoder route only (setdefault: sweeps win). Measured effect:
  720p/4M 83→93-97 %, 720p/12M 91→98 %, 1080p 96.5→98 %, 0 false locks. Do NOT widen the tip window
  (measured worse) or relax gate 1/9. Prefer the 1080p/12 Mbps rung on the decoder route.
- `ORION_METER_TOP_STRIP` default flipped to ON (far-shot fix); tests updated (`clamped_locator`
  fixture = the legacy behaviour). `tests/test_meter_locator_cv.py` 70 tests.
- Nuitka `chiaki_backend` bundling: REFUTED as a risk (in the 08-08 built exe; audit §530).

### Relaunch #2 (16:46 local) verification + round-2 fixes
- Build 16:44 clean; in-place suites: OrionNativeTests 821/0/6, MeterDelaySettings 18/0,
  PreviewPresentation 52/0, ActivityFeedPolicy 10/0. Relaunched 16:46: 0 QML errors; log
  `Input timer: enabled=1 hold_ms=650.0` (the owner's settings had NO METER on, now honoured);
  Profile page renders (Local Dev licence -> "Not reported yet" until the Lambda ships `profile`).
- The owner set Refresh rate = 120 Hz at 16:35 (log: "Capture card refresh rate set to 120 fps");
  the Elgato HD60 X ACCEPTS 1080p120 YUY2 but DELIVERS 60 (raw_fps 60.0, raw_late ~300/window).
  Fix: `capture_card_backend.delivered_cadence_fps()` + a 90-frame warm-up re-locks the
  CadenceLock to the delivered rate and logs "Capture cadence re-locked …"
  (tests/test_capture_cadence_relock.py). Recommendation to the owner: 60 Hz on this card.
- NoMeterCard: the hint sat between the 500/797 ms rails and pushed the column past the card
  (clipped notes) -> rails row + wrapped hint row.
- Activity ring deny-list += `shm preview open pending`, `shm preview reader opened`,
  `meter delay service state:`, `sidecar shipped timing profile:`.

### Owner live feedback on NO METER v2 (~17:30 local) + follow-ups in flight
- "i was hitting beyond half court shots … standstill shots are basically perfect, fades need
  work … switching between the two should cut off the other and vice versa … the lead card is too
  long 1-100 is fine … remove all the unnecessary text". Session: 116 NO METER arms / 114 releases;
  the owner tuned `no_meter_hold_ms` 650 → 641; in METER mode the press-anchored fallback fired 5×
  at 22:11Z WITH `rhythm_offset=40` (a blind stick flick) = the "tempo still takes place" glitch.
- Agent A (engine/QML): `press_anchored_fallback_enabled` default → FALSE (meter mode = meter only);
  rhythm/tempo flag leakage audit between the paths; mode switch clears all transient release state;
  No Meter card compact (1..100 readout, ms caption, rails 1/100, one hint line); new
  `no_meter_fade_trim_ms` (−60..+60) "Fades" slider applied to both fade Δs.
- Agent B (sidecar): LIVE banner verdict reader (`banner_verdict_live.py`, panel_grade's validated
  matcher at ~10 Hz on the normalised frame, `banner_verdict` sidecar event → `RemotePlaySession`
  signal). Phase 2 (controller property + a "last 10: 6 green · 3 early · 1 late → move right"
  tally on the No Meter / Shot Lead cards) after agent A releases the controller/QML.
- Owner asked NOT to close the launcher (testing): agents build in throwaway dirs; the in-place
  rebuild + relaunch happens when he closes it.
- Owner: "remove the pause timing" (No Meter card) and "a few jumpshots not timed all the way".
  Log: 116 arms / 114 releases; scheduler lateness median 0.00 max 0.09 ms; the 2 unreleased arms
  were `input_timer_manual_cancel` (owner let go at +183 / +347 ms = by design). Third suspected
  cause: type classified ONCE at the Square edge (`ls=(0,0)` on every arm; 94 Standstill / 22 fades)
  → a late stick push makes a fade release at the standstill hold. Agent A adds a 200 ms
  reclassification grace window (upgrade only), `ls=` on the arm line, removes the Pause toggle
  (+ the launch-time auto-pause), and suppresses the obsolete "N shots with no detection — Purple"
  advisor in NO METER mode (OrionAppController.cpp ~10843).
- Owner (~17:45): "sometimes it overshoots, buttons weird (holding L2?), random lates/earlies came
  back for that game". Log 22:20-22:50Z (meter mode, 18 meter-path shots): input hook failures=0 /
  ack_failures=0; no pad-silence lines; trigger values at presses only 0/255 (r2=255 on 50 presses =
  turbo, l2=255 on 7 = post shots) → the L2 moment is not in the log; agent A adds an OUTPUT
  DIVERGENCE audit (out vs phys for l2/r2/buttons, + l2/r2 on Release tick). 3 blind fallback fires
  in that game (default → off in the next build). 120 Hz request: after the 90-frame re-lock the
  timestamps are right, but `late_gaps` (gap > 1.5×nominal from `self._fps`), the hw-PTS probe and
  the health floor still used 120 → `raw_late ~300/window` vs 0-1 at 60 Hz; fixed: the backend now
  ADOPTS the delivered rate (`_fps` → 60, `_fps_requested` kept; test
  `test_relock_adopts_the_delivered_rate_for_every_consumer`). Banner verdicts for that game are
  not available (no framedump, live reader not built yet) → the earlies/lates cannot be graded.
### Live banner verdict reader — sidecar half DONE (~17:45 local)
- `banner_verdict_live.py` (repo root; bundle-manifest picks it up) reuses panel_grade's validated
  grader by import (strip geometry, thresholds, 3-cell logic); the 29 NCC template matches became one
  matrix-vector product (`test_matcher_agrees_with_panel_grade`, abs 2e-3). Hook: orchestrator
  `_processing_loop` before the pose/meter split, stride 6, crop on the detect thread (0.0021 ms),
  read on a daemon worker via a 1-slot mailbox; 0.03-0.10 ms per captured frame. One event per
  banner appearance (arm on down→up, flush when timing+coverage read, re-arm after 2 no-panel
  samples, 1.5 s same-words debounce). Event `banner_verdict` {timing, timing_color, green (bool =
  EXCELLENT/PERFECT), coverage, distance_color, ncc, cov_ncc, cells, frame_ts_ns, frame_epoch_ms,
  frame_seq, seq}; INFO line `BANNER VERDICT: …`. Env ORION_BANNER_VERDICT_LIVE / _STRIDE / _DEBOUNCE_MS.
  Native: `RemotePlaySession::bannerVerdict(timing, timingColor, coverage, ncc, frameEpochMs, seq)`
  (+ setupMessage so it lands in the native log). Tests 21/1 skipped.
- PHASE 2 (after agent A): controller deque of 10 → `bannerGreen10/Early10/Late10/Count10`,
  `bannerLastTiming/Coverage`, `bannerSuggestion`, `resetBannerTally()`; card line
  "last 10: 6 green · 3 early · 1 late" + "→ move Shot Lead right / ← left / ✓ this is your value".
- RELEASE NOTE: `tools/timing/panel_grade.py` + `panel_templates.npz` are untracked dev tooling and
  NOT in the sidecar bundle → the reader self-disables on a packaged install until both are added to
  the bundle inputs.
- 17:41 relaunch (owner asked; he set 60 Hz): one sidecar cold-start death (code -1073740022 =
  0xC000070A) at 17:42:00 during "Stream window not up yet (chiaki cold start)", auto-restarted 10 s
  later, Running by 17:42:18. First occurrence today; watch for recurrence.
- **ROOT CAUSE CONFIRMED by the owner (~17:50):** the 120 Hz request was behind the random
  earlies/lates ("turned it off and went perfect from the field"). 120 is being removed from the
  Refresh-rate combo (30/60 only; settings/env still accept 120); the delivered-rate re-lock stays.

### 18:03-18:21 local: "aborts on WIDE OPEN shots" — REGRESSION FOUND (banner reader), rolled back live
- Owner: "sometimes it barely times the meter or times it half way … a ton of shot aborts on WIDE
  OPEN shots … whatever was added needs to be removed". Log evidence: (1) "half way" releases =
  the press-anchored FALLBACK firing mid-fill while the engine was `waiting_for_genuine_meter_before_
  ownership` (4 fires 17:47-17:50) → disabled live via `ORION_PRESS_ANCHORED_FALLBACK=0` at 18:03
  (and default OFF in agent A's build); (2) the "perfect" game (17:42-18:02) had 12 blind rescues in
  34 shots → vision was failing to OWN the meter by 641 ms on a third of shots, masked by the fallback;
  (3) 18:03 session: 2 `ownership_proof_incomplete` with `restarts=6 break_geometry=6` and
  `restarts=4 break_geometry=4` (never seen before today: 341 releases pre-16:52 had 4 geometry breaks
  TOTAL) + 1 `live_tip_deadline_missed`; every `press_unanswered_no_meter` was a <300 ms tap (same as
  the clean games); (4) DETECTOR HEALTH infer TAIL: without the banner reader (21:33-22:20Z) max 87 ms
  once in ~1,200 samples; with it live (17:41+): 62/66/70/73/76/77/93/96/98/102/120/141/154/166/204/
  262/271 ms stalls → skipped frames during shots → geometry breaks. The far-shot TOP_STRIP change is
  CLEARED by the 16:33-17:20 window (147 releases, 0 aborts, TOP_STRIP on, no reader).
- Action: relaunched 18:21 with `ORION_BANNER_VERDICT_LIVE=0` + `ORION_PRESS_ANCHORED_FALLBACK=0`
  (same exe); the reader's author agent is diagnosing the stall (GIL/lock/cv2 thread contention) with
  a 60 fps regression test; default flips to OFF until proven. Phase-2 tally UI continues (display
  only). Watch armed for infer > 60 ms / proof aborts in the 18:21 session.

### Verdict tally UI (phase 2) — DONE (~18:35 local), inert while the reader is off
- `native_orion/src/ShotVerdictTally.h` (pure policy; `OrionShotVerdictTallyTests` 13/0), controller
  props `bannerGreen10/Early10/Late10/Other10/Count10/Contested10`, `bannerLastTiming/Coverage`,
  `bannerSuggestion`, `bannerPattern10` (g/e/l/o oldest→newest), `resetBannerTally()`; auto-reset on
  Shot Lead / No Meter hold / Fades commits and on session start/stop (sidecar seq restarts).
  Feed line `Shot: EXCELLENT · WIDE OPEN` (allow-listed). QML `components/ShotVerdictTally.qml`
  mounted under the Fades slider (NoMeterCard) and under the rails (ShotLeadCard). Suites: 827/0/8,
  19/0, 53/0, 10/0, 13/0; Python 36 + 21 + 21.
- Packaging: manifest `READER_DATA_INPUTS` + build-script copy of `tools/timing/panel_grade.py` +
  `panel_templates.npz` into the dist → the bundle identity digest CHANGED (the existing compiled
  OrionSidecar.exe is now reported stale; `scripts/build_orion_sidecar.ps1 -Force` owed before the
  next package). IP concern: the grader would ship as readable .py → the reader's author agent is
  changing it to a compiled repo-root module `orion_panel_grade` (+ `--include-module`), npz as data.
- Reader default → OFF until the stall regression test passes; tally shows "collecting…" meanwhile.

### 18:33 diagnostic session (-Framedump, all three switches off): CLEAN
- 73 releases / 2 aborts, both `live_tip_deadline_missed` = `unschedulable_lead` by 4 and 16 ms at a
  user lead of 286 (owner was tuning 274/281/286 during the session; settings now 281). Ownership
  census: 0 geometry breaks in 75 shots. Reader-level box geometry breaks from detframes.csv: 3.5 per
  1,000 consecutive pairs vs 3.1 in the 13:56 court batch → the reader's boxes are as stable as the
  known-good reference. The 18:29 session's 6 breaks / 4 shots (same switches, no framedump) remain
  unexplained (small sample, online lobby).
- Decisions: `ORION_METER_TOP_STRIP` default reverted to OFF in source (knob kept; re-enable after a
  live A/B on a Rec court); banner reader default OFF (being reworked); fallback default OFF. At the
  next close: flip `ownership_proof_two_frame` true in settings.json (+ resign) for ~14 ms more
  runway, rebuild in place, relaunch without env switches. Recommend Shot Lead 274.

### 18:47 build — RUNNING (launched 18:48, no env switches)
- In-place build 18:47; suites in place: OrionNativeTests 829/0/6, MeterDelaySettings 19/0,
  PreviewPresentation 53/0, ActivityFeedPolicy 10/0, ShotVerdictTally 13/0; 0 QML errors at launch.
- settings.json (re-signed): `press_anchored_fallback_enabled` false (the old build had persisted its
  default-true — a persisted true would re-enable the blind fallback under the new default-false),
  `ownership_proof_two_frame` true (+~14 ms runway), `actuation_lead_ms` 274 (+ per-source mirror);
  the owner had been tuning 274/281/286 — 286 produced the two `unschedulable_lead` misses.
- Source defaults now: fallback OFF, banner reader OFF (`ORION_BANNER_VERDICT_LIVE` default "0"),
  `ORION_METER_TOP_STRIP` OFF (opt-in until a live A/B), 120 Hz gone from the combo.
- Watches armed: blind fires / QML errors / crashes / detector stalls > 60 ms / aborts / mode
  switches / OUTPUT DIVERGENCE audits.
- Still owed: banner reader stall fix + compiled packaging (agent in progress); commit everything;
  Lambda deploy; admin decisions; purge bypass scripts; red-team; decoder-route graded session.

### Banner reader — stall FIXED + compiled packaging (~19:00 local)
- Mechanism: `panel_grade.find_cells` had two pure-Python pixel loops (row `_runs()` scan + an O(n²)
  candidate-pair loop; n≈20 on a real HUD strip) holding the GIL 0.8-3.5 ms per sample on nearly
  every sampled frame (the dark gate passes on the HUD plate). With the detect thread at
  THREAD_PRIORITY_HIGHEST and the sidecar's other Python threads, that is a priority inversion → the
  measured 60-270 ms detect stalls. OpenCV pool contention / lock-across-crop / allocation churn ruled out.
- Fix (`banner_verdict_live.py`): erosion with a 1×70 kernel replaces the row scan, one float32 gemm +
  argmax replaces the pair loop, `planes()` computed once, colour class via cv2.compare/inRange/
  countNonZero, 29 templates as one matrix-vector product. Pinned exact vs panel_grade on 200+ strips
  (`test_fast_cells_match_panel_grade_exactly`, `test_live_read_matches_panel_grade_match_event`).
  Per read 0.26-0.63 ms (was 1.4-4.5); per captured frame 0.036 ms; detect thread delta p99 +0.22 ms,
  max stall 13.99 ms (quiet box) — gate `test_detect_thread_stall_budget_with_the_reader_active`.
  Default OFF (`ORION_BANNER_VERDICT_LIVE` "0", pinned by a test) until the owner signs off; flip with
  the env at launch (the tally UI then lights up).
- Packaging: `load_panel_grade()` tries `import orion_panel_grade` first (build script copies
  tools/timing/panel_grade.py → repo-root orion_panel_grade.py before Nuitka, `--include-module`,
  deletes it in a `finally`; `.gitignore` entry), then the dev paths; npz resolved via
  `ORION_PANEL_TEMPLATES` → self-relative → beside module/exe → tools/timing in the dist. Manifest:
  `READER_SOURCE_INPUTS` (panel_grade.py bound into the digest, fail-closed if missing — it is still
  UNTRACKED in git) + `READER_DATA_INPUTS` (npz). Tests: bundle manifest 17, banner live 26/1 skip,
  affected set 115/2.

### 19:11 / 19:30 sessions: "aborts on wide open shots again" — runway, not code
- Same exe/Python since 18:47. Runway at reservation (tip_eta at `reservation_created`) vs lead 274:
  court test 13:40 med 355 (p10 323, 2/120 below lead); 18:33 med 347 (2/56); 18:48 med 359 (0/30);
  19:01 med 359 (0/49); 19:11 med 288 (2/5 below); 19:30 med 315 (1/6). In the two abort sessions the
  meter was first accepted at ~22 % fill (vs 18) and ownership completed at 28-36 % (vs 24) — later
  pickup on that court/camera (one miss at meter_x=0.095, the frame's left edge). Misses were 4-22 ms.
- Fix queued to the hybrid agent: `late_fire_tolerance_ms` (default 24): a validated, owned shot whose
  command is ≤ 24 ms late FIRES immediately (`disposition=fired_late`) instead of
  `live_tip_deadline_missed`; beyond that it aborts as before.
- Hybrid NO METER (vision pre-empts blind; defer instead of mid-fill; `no_meter_vision_assist`) +
  learned-Δ shrinkage blend (k=25) are in the same agent build. Rebuild in place + relaunch when it
  reports (app closed 19:41).

### Hybrid NO METER + fade blend + late-fire tolerance — DONE (~19:55 local), building in place
- Vision un-gated in NO METER at 3 sites (`updateDetection` master gate, `reevaluateScheduleOnFreshSample`
  subtick arm, `measuredLeadAuthoritative/userLeadAuthorityActive` clock-compat); Idle→ownership
  promotion fenced to the meter path. Precedence: an armed vision token ends the tick (blind
  unreachable); no candidate → blind at the hold; rising candidate → blind deadline deferred
  (opened 24 ms before the deadline, cap `kNoMeterVisionDeferMaxMs` 400, candidate_lost/cap_expired
  logged). Switch `no_meter_vision_assist` (default true; env ORION_NO_METER_VISION_ASSIST; no UI).
  Blind NO METER releases now teach NOTHING (circular); NO METER vision releases are ordinary vision
  landings (note: not in the meter path's outcome/attribution join — follow-up).
- Learned Δ shrinkage: Δ = (n·learned + 25·table)/(n+25); Standstill reference still needs n ≥ 8;
  `delta_src=blend n= table= learned=` on the arm/fallback lines.
- `late_fire_tolerance_ms` (default 24, clamp 0..40, env ORION_LATE_FIRE_TOLERANCE_MS): a validated,
  owned shot ≤ 24 ms late FIRES (`disposition=fired_late … session_count=N`) instead of
  `live_tip_deadline_missed`; learners excluded for those shots (same set as devFireOffset);
  7 fail-closed tests pin tolerance 0 to keep proving the old contract.
- Native (throwaway): OrionNativeTests 837/0/8, others at baseline.

### 20:03-20:10 session on the 20:02 build: 16 shots, 2 aborts
- `live_tip_deadline_missed` #1: `source=sampler tip_eta_ms=244 lateness_ms=41.6 lead_ms=286 …
  reservation_first_fill=46.41 predictor_sigma_ms=23.6` — the PHASE source never anchored on that
  shot (first sight past the base-20 anchor → sampler fallback → late, noisy reservation) and the
  owner's lead is back at 286 (settings.json 286); 41.6 > the 24 ms late-fire tolerance. The other 16
  reservations were phase (runway med 354). #2: `ownership_proof_incomplete site=pending_stick_fault`
  on a Go-To (stick) shot — the Go-To lane, not the Square path.
- Meter box height is 110 px in EVERY session (court test and online) → not a camera/zoom
  difference. First-sight fill: 18.4 on the court test/18:33/19:01/20:03; 22 on 19:11/19:30.
- Next lever (queued): phase-anchor fallback — when the 20 % crossing is missed, anchor the phase
  model at the first accepted fill (base-30 variant exists in learning.json) instead of dropping to
  the sampler; would turn the 1-2 % sampler-fallback aborts into normal phase-timed shots.

### Owner: "aborts on wide open shots = ship blocker" — engine batch #3 DONE (~20:50), not yet built in place
- `tip_phase_first_sight_anchor` (default true; env ORION_TIP_PHASE_FIRST_SIGHT): when the first
  accepted phase sample is above every ladder rung (≤ 60 % fill), anchor via the SAME
  `tipPhaseLevelAdjustmentMs(fill)` the rungs use; `source=phase_firstsight`; sigma widened in
  quadrature (14.0 ms at 46 % vs sampler 23.6); learner rejects such landings (`learn_reject=
  phase_first_sight`); exact-match anchor-authority privileges not granted. FINDING: on a clean strict
  acquisition `anchorMaxFirstFillPct` (40) already bounds the first phase sample → the feature is
  inert there; it engages after detector re-lock / fill-rollback resets. The 20:09 abort's
  `reservation_first_fill=46.41` is the RESERVATION's first fill, not the first accepted sample —
  re-check that shot against per-frame accepts (the 20:46 framedump session records them).
- `owned_meter_never_aborts` (default true; env ORION_OWNED_METER_NEVER_ABORTS): a validated, owned
  shot past its command deadline fires at ANY lateness (`fired_late … beyond_tolerance=1
  session_beyond_tolerance=N never_aborts=1`); safety refusals unchanged; learner fenced.
- `ownership_proof_leniency` (default true; env ORION_OWNERSHIP_PROOF_LENIENCY): a proof restarted by
  a GEOMETRY break (same candidate/identity, no cliff/gap) is accepted with ≥ 2 post-break samples
  when the arming horizon ≤ 60 ms, sigma + breaks×4 ms (`OWNERSHIP ACCEPTED LENIENT: …`);
  candidate-change/gap breaks still refused (09-12 false-lock protection intact).
- Tests: OrionNativeTests 841/0/8 (throwaway), others baseline; 11 fail-closed fixtures pin the new
  flags off to keep proving the old contract. Build in place + relaunch (-Framedump) at the next close.
- 20:46 session: launched with -Framedump on the 20:02 build (`session_20260914_204600`) to capture
  the owner's wide-open aborts with frames + detframes.csv; C: 23.5 GB free before it.

### 20:46-21:53 frame-dumped session (20:02 build, lead 274) — instruments failed, 3 aborts
- 56 presses / 16 released / 1 `fired_late` (tolerance worked) / 3 aborts at 21:51-21:52:
  (a) `detector_authority_lost_abort` after `OWNED-SHOT UNRESOLVED: owned_fill_rollback_reacquire`
  → `owned_square_detector_unresolved presence=rejected` (meter_x=0.157); (b) `ownership_proof_
  incomplete site=square_early_release` Right Fade first_fill 53.7 → 49.1 (late-seen fade meter:
  ~150 ms runway < 274 lead → vision can never schedule it); (c) `live_trajectory_timeout_abort`:
  `DETDIAG reject='roi_not_found'` for the whole shot window, first meter at press_age 1055 ms
  (class 3: genuine meter never picked up during the shot).
- INSTRUMENTS: the frame dump stopped at 20:51:57 (2,097 frames) with NO disable warning in any log;
  detframes.csv stopped at 21:46:27 at the 64 MB cap (213,917 rows) → no frames/rows for the aborts.
  Agent hardening both (slow-write back-off instead of permanent disable, 60 s FRAMEDUMP status
  line, CSV rotation + flush/drain, launcher env under -Framedump).
- Batch #3 built in place 21:5x: OrionNativeTests 843/0/6, others baseline. Not relaunched (owner
  "done"). Next: meter-mode blind backstop (hybrid deferral semantics; retires the old fallback;
  `meter_blind_backstop` default true) → rebuild → relaunch with -Framedump for the next test.

### Instruments hardened (~22:20 local) — `remote_play_orchestrator.py`, `async_diagnostic_csv.py`, launcher
- Silent dump death = one slow PNG write (≈350 ms > the 250 ms guard; frames.csv idx 0..2095 contiguous,
  last cycle 459 ms) → permanent `writer_stall` disable, whose WARNING lost the native relay's shared
  1 Hz WARNING slot (`kSidecarWarnThrottleMs`, RemotePlaySession.cpp:2883) under 25/s DETDIAG lines
  (only 2 of ~400 DETCSV health lines survived tonight). Third path: `stop()` killed the dump and
  `start()` never re-armed it. Fixes: doubling back-off 1→15 s with frame skipping and auto-resume
  (`_framedump_back_off`), permanent disable only for disk/dir faults or 5 consecutive writer errors,
  60 s `FRAMEDUMP: frames= last_write_age_s= … state=` heartbeat, lifecycle lines at ERROR (bypass the
  throttle; ring-filtered), `_start_framedump()` re-armed from `start()`.
- DETCSV: the 64 MiB cap STOPPED the writer (67,108,730 bytes, 134 short) → now rotates into
  `detframes_<ts>_partNN.csv` (max_parts 16), 2 s flush beat, `ORION_DETCSV_CLOSE_MS` drain (2 s ≤ 5 s),
  ERROR-level rotate/close lines with row counts. Launcher under -Framedump/-Detdiag: 1 GiB parts ×8,
  `DIAG STATUS:` line. Tests: test_framedump_resilience.py (13) + async CSV tests; 113 passed.
- Relay throttle note: ANY one-shot sidecar WARNING can be lost under DETDIAG; use ERROR for
  lifecycle notices (or fix the relay to per-template throttling — follow-up).

### Meter-mode blind backstop — DONE (~22:40), shared `BlindBackstop` helper
- `AutomationEngine::BlindBackstop` (deadline/deferFrom/deferCap/typeGraceEnd) ×2 (`noMeterBackstop_`,
  `meterBackstop_`); shared `resolveBlindDeadline`, `blindShotTypeUpgrade`, `blindReleaseHold`. Meter
  hook: `processIdle` Square branch inside `if (liveMeterOnly)` → `maybeFireMeterBlindBackstop`.
  Precedence: vision owns → Idle left → site unreachable; no candidate by the deadline → blind at
  press + hold; rising candidate → defer (cap 400 ms) then fire on candidate_lost/cap_expired; owned
  shots keep never-aborts/late-fire/leniency. Mode bit = the meter path's own rhythm. Learners
  structurally unreachable. Setting `meter_blind_backstop` (default true; env
  ORION_METER_BLIND_BACKSTOP). RETIRED: `maybeFirePressAnchoredFallback`, `ORION_PRESS_ANCHORED_
  FALLBACK`; `press_anchored_fallback_enabled` key load-tolerated only. Log vocabulary: `METER
  BACKSTOP: fired|blind deadline deferred|deferral ended|reclassified`, `press_answered_by_backstop`,
  census field `backstop=`. Tests: 11 `meterBlindBackstop*` replace the 8 fallback tests; 844/0/8
  (throwaway). NOTE for log greps/monitors: `PRESS-ANCHORED FALLBACK` no longer exists.

### NO METER — "further engineer it" plan (~23:00 local)
- Owner: standstill good but needs consistency; fades need work; (odd) "as the frame dump started I
  rarely got aborts".
- Physics: the console samples input once per frame (60 Hz = 16.667 ms) and judges the release per
  frame; the green window ≈ 1 frame. A blind hold that is not a whole number of frames lands ON a
  boundary → coin flip between N and N+1 frames (641 ms = 38.5 fr felt inconsistent; 650 = 39.0 fr
  "basically perfect"). Step 1 (agent, in flight): frame-quantize every blind hold
  (`console_frame_ms` 16.6667, `no_meter_frame_quantize` true; total snapped after the 450 floor;
  Rhythm offset = 2 frames), card caption `39 fr · 650 ms`, press-edge forwarding latency audit.
- Step 2 (next build): fade CONTEXT learner — per stick angle/magnitude at the press, pre-press
  movement, time since the catch (the "3" icon ON edge) — instead of per direction; blended with
  the table by n.
- Step 3: icon-based learning with the meter OFF — icon-off − press = the true per-shot hold; with
  the banner verdict (live reader) the engine learns the optimal hold per context without a meter.
- Ceiling: standstill high-80s % green blind; fades tighter but multi-animation → keep the hybrid
  (vision wins when the meter is visible).

### Frame-quantized blind holds — DONE (~23:45), not yet built in place
- `blindReleaseHold`: floor 450 first, then snap the TOTAL to the nearest console frame (round half
  up; 450 = 27 fr exactly; microsecond-rounded so 39 fr = 650.0). `console_frame_ms` (default exactly
  1000/60, clamp 8..40, env ORION_CONSOLE_FRAME_MS), `no_meter_frame_quantize` (default true, env
  ORION_NO_METER_FRAME_QUANTIZE; off = pre-change law bit-for-bit incl. the 40 ms rhythm offset).
  Rhythm = `kBlindReleaseRhythmFrames` 2 × frame (33.3 ms) when on. Card caption `39 fr · 650 ms`,
  Fades readout `+1 fr`; props `noMeterHoldFrames/SnappedMs/consoleFrameMs/noMeterFrameQuantize`;
  logs gain `raw_ms= frames= frame_ms= quantize= rhythm_frames=`. 851/0/6 native; UI contract 20.
- Press-edge audit (no change): WM_INPUT → latest-wins mailbox → 4 ms poll → engine → pipe → UDP;
  mailbox→poll 0-4 ms, epoch→UDP-accepted median 1 ms p99 3 max 4, ack 0.38 ms median; worst ≈ 8 ms,
  jitter ≈ 4-5 ms p-p → cannot cross a 16.7 ms frame; event-driven wake rejected as unsafe (advances
  the 3-sample tracking/tempo windows). Side finding: 3.9 % of Square-down epochs delivered with
  square_bit=0 (`stabilizing_tempo_movement_intent` suppression) = a ≥ 1-poll policy delay.
- Owner relaunched (via a resumed agent) at 23:31 on the 22:28 build WITHOUT -Framedump; the
  frame-quantized build lands at the next close.

### 00:23-00:26 session (00:22 build, NO METER): 29 released, 1 LOST SHOT + a log flood — regression from the hybrid
- `LeadRebaseline: REFUSED — implausible delta 220.9ms (measured 220.9, learned 0.0)` every engine tick
  in NO METER mode (28,514 lines in the 00:08-00:17 session; 6,566 in 3 min): the vision-assist
  un-gating made `rebaselineLeadClocks()` reachable per tick with a 0 learned lead.
- Epoch 3: `NO METER: hold_ms=633.3 … frames=38` then NOTHING until `input_timer_late_abort` 1.75 s
  later — the 24 ms pre-arm window for the precise waiter was missed (engine on the GUI thread's 4 ms
  timer; stall under the flood). Fix agent: no per-tick rebaseline in NO METER + memoised refusal log;
  arm the precise waiter with the full deadline at the press (re-arm on deadline change, cancel on
  manual release/vision ownership/mode switch), same for the METER BACKSTOP; an unarmed passed deadline
  fires immediately with `NO METER: DEADLINE MISSED unarmed` instead of dying at the authority limit.
- Interim (if testing before the fix): `ORION_NO_METER_VISION_ASSIST=0` at launch = pure blind NO METER
  (the "amazing" configuration) without the flood.

### 09-15 morning: NO METER waiter fix + rebaseline gate — DONE (856/0/6 throwaway), built in place
- Flood: the single `rebaselineLeadClocks()` call site is `updateDetection` (per detector payload);
  now `maybeRebaselineLeadClocks()`: never in NO METER, once per shot attempt, refusal line de-duped
  per (measured, learned) pair per session. Coupling fixed: `measuredLeadEpochReady_` now keys off
  `leadClocksCompatible = autonomousLiveMeterTimingEnabled() || noMeterVisionAssistActive()` (meter
  mode bit-identical).
- Blind release tick-independent: `armBlindPreciseFire()` arms the InputTimed precise-fire token at
  the press with the full deadline (horizon = runway+1); re-armed on deadline change (type grace,
  deferral open/end) via `invalidateUnconfirmedSchedule("blind_deadline_moved")`; cancelled by manual
  release/route/disarm/mode switch; vision may steal it only while the deadline is > 24 ms away;
  `OrionPreciseFireThread::refreshReleaseOutput()` keeps the armed packet ≤ 1 tick old.
  `maybeRescueOverdueBlindRelease()`: an unarmed passed deadline fires immediately (`NO METER:
  DEADLINE MISSED unarmed by=`), `input_timer_late_abort` impossible for an armed press. METER
  BACKSTOP logs `DEADLINE MISSED` but keeps tick-driven firing (no ShotContext; stall = lateness only).
- Player-anchored pickup layer (nameplate → meter patch, expectation window, relaxed floor inside the
  patch, knob-gated, offline validation) in flight; detector second opinion = `models/
  orion_meter_detector.onnx` (YOLO26-n, ~13 ms full frame FP16, cheap on a patch).
- Discord: `docs/DISCORD_SERVER_SETUP_PROMPT.md` written for Astra (roles/channels/permissions/bots).

### 09-15 ~14:30: player-anchored pickup layer BUILT (knob-gated, defaults unchanged) + licence strip
- Profile tab removed; licence strip in the Sidebar footer (`licenseStrip/State/Days`, `licenseFlyout`
  with key/Discord/resets/activated/machine/build); built in place 13:46 (856/0/6 etc.).
- `player_anchor.py` (PS-disc template + runtime-learned gamertag; patch = icon +30 x / −150 y,
  ±85/±70; two-tier track/acquire, armed-only for `ORION_ANCHOR_ARM_S` 2.5 s; p50 0.21 ms, p99 2.97,
  max 4.1) + locator gate 10 (anchored patch search, relaxed floors inside the patch
  `ORION_CV_SHAPE_MIN_H_ARMED` / `ORION_CV_COL_W_MIN_ARMED`, gate-9 ABSTAIN between the relaxed and
  shipped floors, expectation window `ORION_EXPECTATION_WINDOW`, out-of-patch refusal
  `ORION_ANCHOR_REFUSE_MODE`) + reader `PICKUP:` line per press. Knobs: `ORION_PLAYER_ANCHOR`,
  `ORION_ANCHORED_SEARCH` (all default 0). Tests 23 + 526 wider.
- Findings: low-fill meters die on `shape_short` (10-12 px runs) / gate 4 `w>=8` (7 px) / `no_tip`,
  and lowering the floor alone gains nothing (gate 9 then calls them irregular) → abstain + window:
  in-patch recall at 10-15 % fill 1/140 → 77/140. Plate present at the meter offset: real shots
  0.60-0.73 vs out-of-shot junk 0.01-0.09 (~10× separation) → a confident anchor refuses 91-96 % of
  the false-lock population; 0 outside-patch accepts on all three corpora. First-sight gain NOT
  measurable offline (7 fps dumps) → live 60 fps session with `PICKUP:` lines is the instrument.
- Plan: (1) observation A/B: `ORION_PLAYER_ANCHOR=1 ORION_ANCHORED_SEARCH=1 ORION_ANCHOR_REFUSE_MODE=1`
  with floors at today's values; (2) if clean, flip `ORION_CV_SHAPE_MIN_H_ARMED=8
  ORION_CV_COL_W_MIN_ARMED=6 ORION_EXPECTATION_WINDOW=1` together (expected first sight ~10-12 %
  instead of ~18 %); (3) then `ORION_ANCHOR_REFUSE_MODE=0`. Engine-side (agent in flight): `shot_type=`
  + `rhythm=` on `shot_gate_arm` (re-sent on the grace upgrade) and a `shot_gate_release` /
  `shot_gate_disarm` marker so the window is per type and ends at the release.

### 09-15 ~15:30: shot-gate protocol extended (DONE, 861/0) + final engine pass in flight
- `native_orion/src/ShotGateProtocol.h`: `shot_gate_arm` now carries `shot_type` (omitted when empty)
  + `rhythm`, re-sent with `source=type_upgrade` on the 200 ms grace; new `shot_gate_release`
  (`release_ms` epoch ms) and `shot_gate_disarm` (`reason`); at most one close per epoch, release
  beats disarm. Sidecar: `arm_shot_gate(source, epoch, shot_type, rhythm)` → reader
  `notify_physical_shot_type`; `release_shot_gate`/`disarm_shot_gate` → `_close_shot_gate_press`
  (latency-oracle deadlines untouched); `player_anchor.ArmState` gains rhythm/release_ts;
  `onset_window_ms("")` = (50, 1100), Standstill (50, 550), Left Fade (575, 1075). Additive both ways.
  Tests: native +7, Python +21 (73 in the target set; 478 + 245 regression).
- In flight (one agent): PART A frame-native predictor (`tip_frame_native`, default true): game-frame
  phase lock from fill STEP EDGES, anchor dated to the game frame, fire at the tip-frame CENTRE − lead
  (guard near boundaries), phase-invariance tests at capture phases 0/4/8/12 ms; PART B shelve NO
  METER (mode switch + NoMeterCard/Rhythm mounts removed, `inputTimedEnabled` forced false on
  load/save, engine + tests kept; meter backstop stays).
- Owner: target high-80s/90 %+ green and stop chasing; pricing advice given (7d $12 / 1m $30 /
  season-lifetime $100 capped, HWID credit $5, launch promo); Astra is setting up Discord + bots.

## 09-15 ~16:10: pricing decision + second Astra prompt + backend/bot pass dispatched

**Isaiah's final pricing (replaces the 7d/1m/lifetime advice):** free 3-day trial → **$25/month
recurring** plus a cheap one-time activation fee; **no lifetime**, no 7-day; after the 3 free HWID
resets each reset **deducts one day** from the subscription (no customer reset product); `/purchase`
= one button straight to the store; Discord `#welcome` = welcome + rules + ToS + status in ONE
message, footer "Venice • Official".

- `docs/DISCORD_SERVER_SETUP_PROMPT.md` rewritten for Astra (roles drop Lifetime; `#pricing` = one
  $25/month embed; `/purchase` link-only; HWID/Support/Welcome copy as supplied; `#verify` gate).
- Backend/bot agent (Opus 5) in flight: plans month+trial only (lifetime/week loadable, unadvertised),
  Gumroad two-product checkout (`orion-30day` membership + `orion-activation` one-time, activation
  ping mints no key), reset policy 3 free → `mode=deduct` 1 day, trial refusal, 24 h cooldown kept,
  `/purchase` single-button embed, `LIFETIME_ROLE_ID` legacy. Tests in tests/backend, tests/discord,
  tests/test_orion_admin.py. Nothing deployed.
- Engine final pass (frame-native predictor + shelve NO METER) still running; in-place build is still
  13:46 (licence strip + waiter fix). Shot-gate protocol change exists only in a throwaway build.
- Memory: `pricing-decision-25-monthly-only.md`.

## 09-15 ~16:45: pricing rules BUILT (backend + bots + webhook + admin CLI) — verified, not deployed

Re-ran myself: `tests/backend` 245/245 (was 230); `tests/discord` + `test_orion_admin` +
`test_orion_bot_commands` + `test_backend_contract_check` + `test_security_audit` 220/220.
- Backend: `RESET_PENALTY_DAYS=1` (`deduct_days` on the wire next to `penalty_days`), `price_url`
  never sent, `trial_no_deduct` refusal (trials used to get unlimited 4th resets — the floor-at-now
  bug), per-key `hwid_penalty_days` honoured (was a silent no-op), credits = staff goodwill only.
  `payment_required` keeps its name (both deployed front-ends switch on it) + `confirm_required`.
- Worker/bot: `/purchase` = one embed + one button → `STORE_URL` (wrangler var, currently `""` —
  set before deploy); reset confirm = "Deduct 1 day and reset" / "Cancel"; `GUMROAD_HWID_RESET_SLUG`
  and `HWID_RESET_BUY_URL` removed; `LIFETIME_ROLE_ID` legacy.
- Gumroad webhook: `orion-monthly` (membership, $25/30 d) mints on first charge and RENEWS on
  recurring charges (fresh `sale_id` per charge; the `lickey:` once-ever marker is bypassed for
  recurring pings — it would otherwise have let the key lapse while billing continued);
  `orion-activation` mints nothing (`plan=activation_fee`). Legacy slugs kept for refunds.
- Owner to-dos: create the two Gumroad products (key generation ON, custom field `Discord ID`), give
  the website URL for `STORE_URL`, re-run `register_commands.py`, confirm with one real renewal ping
  that Gumroad re-sends `Discord ID` on recurring charges. A refund on a renewal revokes the key.
- Launcher copy check: only "Lifetime" display fallbacks in Sidebar/admin pages (staff comps) — fine.

## 09-15 15:29 local: FINAL PASS BUILT IN PLACE + LAUNCHED (ready for Isaiah's test)

- In-place build 15:26 (`cmake --build native_orion/build --config Release`): OrionNative + 5 test
  targets. Suites (QTest `-o file,txt`, Qt bin on PATH — `-silent` to a redirect writes NOTHING and
  still exits 0, do not trust it): OrionNativeTests **872/0/6**, MeterDelaySettings 19/0,
  PreviewPresentation 53/0, ActivityFeedPolicy 10/0, ShotVerdictTally 13/0; `test_venice_ui_contract`
  20/20. Includes the shot-gate protocol change and the final engine pass.
- Final pass contents: `GameFramePhase.h` estimator (O(1) running sums, fed unconditionally), frame-
  native anchor dating `frameNativeCrossingMs()` (fail-closed, ≤ 1 frame correction), phase-aligned
  firing `phaseAlignedFireTargetMs()` at the reservation + subtick arm (`fire = tip frame centre −
  lead`; `decision.tipAbsMs` untouched; near-boundary guard 2 ms AND grid sd > 2 ms → instant),
  `tip_frame_native` default TRUE, env `ORION_TIP_FRAME_NATIVE` 0/1 = no-rebuild kill switch; sigma
  credit shipped at 0 (`tipFrameNativeSigmaCreditMs`). Logs: `FRAME PHASE:` per shot, `TIP
  RESERVATION: … fire_target=frame_centre|instant frame_offset_ms=`, miss line carries the same.
  **Premise refuted:** capture-phase does NOT spread the interpolated anchor (0.00 ms spread, sd
  0.08 ms vs truth); the gain is the fit (sd 2.23 vs 2.77 at 3 ms jitter) + frame-centring
  (P(intended frame) 49→52 % at σ 11.7; 76→91 % at σ 5). `source=phase` label deliberately kept
  (token-authority consumers match it exactly).
- NO METER shelved (three doors): RemotePlayPage mounts removed `[ORION_NO_METER_SHELVED
  2026-09-15]`, `AppConfig` load+save force `input_timed_enabled=false` (`g_inputTimedAllowed`),
  controller refuses `setInputTimedEnabled(true)`; NoMeterCard/RhythmCard stay in the tree; hold
  tuning keys round-trip; learner + meter blind backstop untouched. Test door
  `setInputTimedAllowedForTesting`.
- Launched 15:28 via `run_orion.local.ps1 -NoElevate` with `ORION_BANNER_VERDICT_LIVE=1
  ORION_PLAYER_ANCHOR=1 ORION_ANCHORED_SEARCH=1 ORION_ANCHOR_REFUSE_MODE=1` (floors unchanged, no
  framedump): 0 QML errors, all four flags in the sidecar env, preview 60 fps (dark_frame — console
  off). Settings loaded: lead **286** (Isaiah's own last slider move 04:39Z, after his 274 — left
  alone), `input_timed_enabled` true in the file but forced off by the fence, hold 647.
- Watch this session for: `FRAME PHASE` lock sd, `fire_target=frame_centre` share, banner tally,
  `PICKUP:` lines, aborts. If timing reads wrong on the banner: `ORION_TIP_FRAME_NATIVE=0` relaunch.
- Pre-existing pytest failures (not from this pass): nexus_svc_delay ×2, latency_estimator,
  banner_verdict_live stall budget, framedump_resilience, native_qtest_diagnostics ×3 (sandbox
  exit 125), release_filter_hygiene (regex misses `add_test(NAME ${target}` in `orion_add_qtest`).
- New untracked: `native_orion/src/GameFramePhase.h`, `tests/discord/worker_probe.mjs`,
  `tests/discord/test_gumroad_membership.py`, `tests/discord/test_orion_worker_purchase.py`,
  `tests/backend/test_plan_surface.py`.

## 09-15 ~16:10: LIVE SESSION 15:48-15:51 — "meter doesn't detect" = backstop pre-empting vision

37 presses: 26 vision (banner 15 EXCELLENT / 8 EARLY / 0 LATE), 9 `METER BACKSTOP` blind
(`no_candidate_by_deadline`; banner 3/3), 2 taps. Vision first sample 509-586 ms after press,
proof 537-620 ms (fades 749-899 vs 950 deadline) → 30-55 ms margin against the 650 ms backstop;
the 24 ms rising-candidate deferral never opened. Fix in flight (Opus 5 agent, in-place build —
the app was closed 15:51): `meter_backstop_grace_ms` default 100 (meter path only), fired line
`grace_ms=`; tests; PICKUP line one-per-press fix. Frame-native cleared (frame_offset mean +0.88,
EARLY/EXCELLENT identical). Lead 286 reads ~25 ms early on both instruments → advise 274.
Anchor: PICKUP epoch 14 anchor_used=1 conf 0.67, epoch 13 anchor_used=0; refused_outside=0.

## 09-15 16:10 local: grace fix BUILT + RELAUNCHED with the relaxation trio

- In-place build 16:06 (OrionNative) / 16:07 (tests): `meter_backstop_grace_ms` default 100
  (clamp 0..400, env `ORION_METER_BACKSTOP_GRACE_MS`, RemapConfig + applyConfig), applied at both
  METER backstop deadline sites (Standstill 650→750, fade ~933→1033); fired line `grace_ms=`.
  Fixture `meterBlindBackstopConfig()` pins grace 0 so the law tests keep their arithmetic.
  Suites: OrionNativeTests **876/0/6** (+4), MeterDelaySettings 19, PreviewPresentation 53,
  ActivityFeedPolicy 10, ShotVerdictTally 13; pytest player_anchor+ui_contract+banner 81/1 skip.
- PICKUP line now one per press (`MeterContourLocator.open_pickup_record`, reader
  `_open_pickup_record` in `_publish_press_window`; misses flush as `first_sight_fill=-1.0`).
- Relaunched via `run_orion.local.ps1 -NoElevate` with BANNER_VERDICT_LIVE=1 PLAYER_ANCHOR=1
  ANCHORED_SEARCH=1 ANCHOR_REFUSE_MODE=1 **+ ORION_CV_SHAPE_MIN_H_ARMED=8
  ORION_CV_COL_W_MIN_ARMED=6 ORION_EXPECTATION_WINDOW=1** (relaxation trio, first live run).
- Isaiah's second ask: "instantly lock onto the meter when it's displayed and track it until it
  disappears" — tracking verified solid (break_geometry=0 on 26/26, 0 restarts); the delay is
  acquisition (14 px shape floor ≈ 18-22 % fill). Next session: read `PICKUP:` per press
  (anchor hit rate, first_sight_fill/ms) to size the remaining gap; candidate = second-opinion
  YOLO on the anchor patch.
- Lead still 286 (his); advised 274.

## 09-15 17:05 local: session 16:13-16:54 read-out (grace + relaxation trio live) + Isaiah's next list

- 30 releases (29 vision, 1 `live_tip_fired_late`), **1 METER BACKSTOP** (was 9), 0 unanswered;
  banner **22 EXCELLENT / 6 LATE / 3 EARLY** (71 %, was 62 %). Isaiah moved lead 286→274 mid-
  session (seq 20+): at 274 → 9 EXC / 1 EARLY / 2 LATE (seq 29, 30 = "2 random lates").
- Lates: seq 18 fired 4.95 ms late on a 29 ms-old frame (`frame_age_ms=29.16`, held 8 ms) = a
  frame-delivery hiccup; seq 29/30: latency normal (209 ms), reservations normal (sigma 14,
  frame_offset −4.2/+5.1, correction 0/0.66), frame_age 26.9/23.9, seq 30's PHASE anchor taken at
  25 % (missed the 20 % crossing) → consistent with delayed frames, not proven. Frame-age > 22 ms
  at reservation: LATE 3/4 vs EXC 18/35 vs EARLY 2/11 (n too small).
- 60 fps: raw capture steady (raw_fps 59.8-60.1, raw_gap_max 24-30 ms, dup 0-2 %) but the
  sidecar CONSUMER stalls: `preview_stats callback_gap_ms max` 223 + 280 ms (21:50:35-40Z, idle),
  75-78 ms twice, 41-52 ms ×4; the session's only backstop sat in a 51 ms-gap window. Box: 16
  cores, 5-6 % load after close; `headroom.exe`'s python child (pid 3392, 25 threads) averages
  ~7 % of a core — not a starvation candidate. Attribution tooling in flight (STALL:/GC: lines,
  `ORION_STALL_ATTRIB`).
- `PICKUP:` printed 0/30 presses after the "fix" (2/37 before) → agent re-fixing against the real
  object graph.
- UI agent (throwaway build): remove Sidebar "Quick Start" footer + tour action; tour only on first
  launch (AppShell gates on `orion.preflightComplete` — trace why it re-shows); find/remove the
  "session start bubble"; redesign the licence flyout (`licenseFlyout`, keep objectNames).
- Next: in-place rebuild when both land (app closed 16:54), run suites, relaunch same flags.

## 09-15 17:18 local: UI bubbles pass BUILT IN PLACE (not launched yet — sidecar agent still running)

- `[ORION_UI_BUBBLES 2026-09-15]` (Sidebar.qml, AppShell.qml, FirstRunTour.qml, ui contract test):
  Sidebar "Quick Start" footer button + `tourRequested` + nav `action:"tour"` removed; tour is
  first-launch-only via the existing `preflight_complete` key, now marked the moment it auto-opens
  (before: only on Finish/Skip → a mid-tour close re-showed it every launch; and Main.qml's gate
  Loader rebuilds AppShell on licence/gate changes → re-showed mid-session). "Session start bubble"
  = the footer `StatusPill label:"Session" Offline/Live` (INFERRED — no popup exists at session
  start; one-line revert if Isaiah meant something else). Licence flyout rebuilt as a Card-style
  296 px card: tone chip + plan, days-left headline, `DetailRow` grid (Key mono ElideLeft + Copy,
  Discord + Copy, Name, PC resets, Activated, Machine, Build), Esc/click-outside close, no
  hard-coded colours, objectNames kept.
- In-place build 17:17: OrionNativeTests 876/0/6, MeterDelaySettings 19, PreviewPresentation 53,
  ActivityFeedPolicy 10, ShotVerdictTally 13; pytest ui_contract + non_live_surface +
  live_page_cleanup 32/32.
- Waiting on the sidecar agent (PICKUP live fix + STALL/GC attributor) before relaunch.

## 09-15 ~17:55 local: session 17:30-17:38 (rapid-fire drill) — "5 lates in a row" read-out

- 15 presses / 13 vision releases / 1 backstop / 1 abort (`detector_authority_lost_abort` ep12).
  Banner (1:1 by exact ms): lates = seq 5, 6, 9, 10, [ep12 user release], 11, 12 (7); EXC = 1-4,
  7, 8, 13 + the ep6 backstop. Owner's "5 in a row" = seq 9-12 + ep12 (22:37:26-38Z).
- STALL attributor: 58 stalls, ALL 22:30-22:35Z (menus/loading, before the first press):
  CaptureCardReader `_isolate_immutable_frame`/`np.array_equal` and meter-yolo `_find` on static
  frames, p50 50 ms, max 497. ZERO during shooting → stalls are NOT the late cause. Sampler cost
  live 1.05-1.34 % of a core at 10 ms (p99 0.27-0.39 ms).
- The 5-late regime = rapid-fire (press every 2.5-3 s): on ep10-15 the previous meter was still
  locked at 83-94 % at the press → STALE/GHOST drop → identity retired after 2 no-finds → the next
  lock is SEEDED (`seeded` 23→65, `locks` 42→84, `drops` flat in 12 s = re-locks without drops).
  Engine side: holds 661/695/741/641 on the lates vs 630-652 EXC; `peak_fill` at freeze IDENTICAL
  on lates and EXC (90.4-91.9 reader scale) while `green_obs_start` on lates sat 2-7 pp lower in
  the box (88.3-93.1 vs 94.7-95.2) — consistent with a ghost-seeded box seated a few px high
  (reads low → tip predicted later → late), NOT proven (no per-frame boxes without -Framedump).
  Latency outliers 288/290 ms on seq 8/9 (freeze mis-measured in the overlap).
- Banner reader artefact: 22:36:20-59Z 16× "EXCELLENT · WIDE OPEN" with 0 presses = a static
  panel re-read every ~2 s (ncc cycling 0.913/0.916/0.958/0.961) + a static column at
  [777,342,26,110] locked/dropped 20× (the "white lines" false-lock class). Both reached the
  customer feed/tally.
- In flight (Opus 5 ×2): reader — fresh acquisition after a ghost retire (no seed from the ghost's
  box), box latched for the owned press, static-zone quarantine until a low-fill rise, ERROR-level
  lines, offline replay 0-diff; banner — edge-trigger per panel appearance + attribution to a
  release within 0.4-2.6 s, unattributed verdicts logged only.
- Decisive experiment still owed: a 2-min rapid-fire drill under `-Framedump` (per-frame boxes +
  meter pixels) to separate "box offset" from "window/animation".
- 18:15: banner reader DONE (agent): content-based edge trigger (`_CLEAR_RUN` 5 samples absent
  before the same words count again), attribution via `release_shot_gate` → `note_release`
  (8-deep ring, consumed once; window `ORION_BANNER_VERDICT_ATTR_MIN_MS/MAX_MS` 400..2600, measured
  release→panel 790-1139 ms median 996); unattributed → `BANNER VERDICT UNATTRIBUTED:` at ERROR,
  not forwarded (no feed line / tally). Forwarded line gains `attributed=1 release_seq= delay_ms=`.
  Go-To (`mark_release`) not fed; `ORION_BANNER_VERDICT_REQUIRE_RELEASE=0` = old behaviour.
- 18:40: reader agent DONE: the seed lived in the PROPOSER (`meter_locator_cv._last_box`; the
  tip-corroborate escape at ~570 accepts a washed-tip leftover at conf 0.75 purely by co-location;
  reader-side drops never cleared it → the retired ghost seeded its successor; evict path used
  `_det_reset_lock_state` not `_det_drop_lock` → locks↑ drops flat). Fix: `forget_position()` on
  locator + async wrapper; reader knobs default ON `ORION_READER_GHOST_FORGET_LOCATOR`,
  `ORION_READER_FRESH_AFTER_GHOST` (in-zone ≥40 % withheld until the press sights its own meter
  low+rising), `ORION_READER_BOX_LATCH` (teleport re-seat refused while owned; a break still drops),
  `ORION_READER_STATIC_ZONE_QUARANTINE` (2 never-risen strikes → no warm seed, 20 s TTL). ERROR lines
  `RESEED REFUSED:` `BOX LATCHED:` `STATIC ZONE QUARANTINED/RELEASED:`; health `reseed_refused=
  loc_forget= staticq=`. Replay session_20260912_201355: 0/12000 rows differ (Red + White).
  **REFUTED my census fill-rate claim** (rates overlap; `vel=` 0.176-0.183 on every shot) → the
  5-late cause is still open; second contamination channel: `player_anchor.ANCHOR.note_meter`
  taught from a conf≥0.90 leftover (meter_locator_cv ~641). Tests: new
  tests/test_reader_fresh_onset_lock.py 14; my run 184/2 skipped across reader/locator/anchor/banner/
  attributor/health.
- 18:42: RELAUNCHED `-NoElevate -Framedump` with the seven flags + STALL_ATTRIB=1 (10 ms) +
  `ORION_FRAMEDUMP_ROOT=D:\NexusVision\framedump` (default root is on C: — 7 GB free!) +
  DETCSV capped 2×256 MiB. Ask: a 2-min rapid-fire drill → grade boxes/window per frame.

## 09-15 ~20:55 local: FRAMEDUMP DRILL session 18:54-20:39 (shots 20:36-20:39) — read-out

- 34 presses / 29 vision / 3 backstops; attributed banner 7 LATE / 2 EARLY / 7 EXCELLENT (16 of 29
  releases got a banner). Reader layers fired: BOX LATCHED 32, STATIC ZONE QUARANTINED 2, RESEED
  REFUSED 0; ghost-at-press on 13 presses. **Ghost-at-press is NOT the discriminator**: ghost
  3 EXC / 3 LATE, clean 4 EXC / 4 LATE / 2 EARLY. LATE standstills released at holds 641/642/643
  (= the EXC holds 645-654) with the same fill@rel (37.8-38.1) and the same freeze peak (90.7 vs
  90.8 reader units); reservation fill/age/tip_eta identical; frame_offset mean +2.2 (LATE) vs +4.0
  (EXC). → identical releases graded differently = the landing sits on the top edge of a ~1-frame
  window (`green_obs_width` 1.4-2.3 pp ≈ 1 frame in the drill vs 1.5-5.2 pp in the 5v5 game).
  Context-dependent: game@274 = 9/1/2, game@286 = 8 EARLY/0 LATE, drill@274 = 7 LATE/2 EARLY.
- Framedump: writer wrote only 826 frames (dropped=595, skipped=119, `stopped:idle`) → ~half the
  drill covered; `detframes.csv` 111 MB covers it all. Forensics agent (Opus 5) running: per-shot
  table, H1 box geometry (detframes), H3 pixel fill/window at freeze, panel_grade on the PNGs.
- Dispatched: engine `banner_lead_trim` (bounded ±15 ms, 3 ms steps, per-type, hysteresis 2-of-4,
  EXC decays, persists with 50 % start decay, resets on slider move; ShotLeadCard caption).

## 09-15 ~21:40 local: FORENSICS VERDICT + engine trim/fade-grace BUILT + auto-seed dispatched

- **Forensics (D:\NexusVision\framedump\_analysis\, panel_grade = truth: EXC 15 / LATE 11 / EARLY 2
  of 28):** identical meter landings on LATE and EXC (fill@command 43.0 ± 0.9 both, slope 0.180,
  peak 90.5 ± 0.4); box h 119 ± 1 both; green band bottom 91.3 vs 92.0 %; frame_age 19.5 vs 21.4;
  latency flat 210-216; frame_offset −0.03 vs +1.0; STALLs all pre-press → H1/H2/H4 refuted. THE
  ORACLE: post-top-out retraction gap white-top→green-bottom ≤3 px = EXCELLENT, ≥4 px = miss (27/27;
  `settled_fill` EXC 90.06 vs LATE 85.47). `peak_fill` saturated (never grade on it). Fade LATEs ↔
  press→meter-onset 778-866 vs 703-752 ms (holds 1018-1099 vs 945-987); standstill LATEs ↔ nothing
  (poll-phase coin flip on a ~1-frame window). Backstops: ep5 meter appeared +769 (after the 754
  deadline); ep8/ep21 meters on screen 320-340 ms with NO green tip (0-4 px) → never proposed.
  Live banner reader: 16/30 panels, emit 2.5-3 s after onset. Memory:
  `lates-not-in-the-meter-retraction-oracle.md`.
- Engine (in place, 888/0/6, Preview 54, Tally 24, ui contract 21): `banner_lead_trim` (BannerLeadTrim.h;
  ±15, step 3, per-bucket 4-deep hysteresis incl. decay; rides the USER lead only; keyed on the
  shot-gate physical epoch = the sidecar's `release_seq`; `BANNER TRIM:` lines; `TIP RESERVATION …
  banner_trim_ms=` last field; ShotLeadCard caption `shotLeadAutoTrimCaption`; learning
  `banner_lead_trim_by_type`, 50 % start decay, reset on slider move) + `meter_backstop_grace_fade_ms`
  220 (`meterBackstopGraceMsForType()`, fired line ` grace_kind=`).
- In flight: reader tipless-armed pickup + `RELEASE ORACLE` line (Python); banner reader recall/
  latency (Python); engine `lead_auto_seed` (measured + `aim_margin_ms` 62, placeholder 274, trim
  rides it, `lead_source=auto`, ShotLeadCard caption).
- 21:55: banner reader round 2 DONE: offline replay of the 824-frame dump 16/30 → **30/30** panels,
  0 phantom, onset→emit median 115 ms (p90 233); attribution on ONSET (release→onset 995-1744 ms,
  window 400..2600); `_CLEAR_RUN`/`_DOWN_RUN_GONE` 1; 2-cell `TIMING | DISTANCE` layout completes
  without a coverage cell; verdict identity = (word, colour), re-report only behind a NEW release;
  line adds `onset_ms= emit_latency_ms=`. Reader default still OFF (env `ORION_BANNER_VERDICT_LIVE=1`
  on the launch line — consider flipping the default for ship).
- 22:00: `lead_auto_seed` DONE (in place, build 21:50:42): unconfigured install flies
  `measured_latency + aim_margin_ms(62)` (placeholder 274 until `measuredLeadAuthoritative`), same
  path as the user lead (trim/session trim/meter delay/rhythm apply), `lead_source=auto`,
  `LEAD AUTO SEED:` lines, `TIP RESERVATION … auto_seed_ms= auto_seed_kind=` (append-only after
  `banner_trim_ms`), `BANNER TRIM … lead_base=user|auto`; ShotLeadCard `shotLeadAutoSeedCaption`;
  36 fixtures pin `leadAutoSeed=false` (they measure the authority path); rate limit 2 ms/shot except
  the first value and the placeholder→measured adoption. My verification on the final build:
  OrionNativeTests **893/0/6**, MeterDelaySettings 19, PreviewPresentation 55, ActivityFeedPolicy 10,
  ShotVerdictTally 24; ui contracts 32/32. Waiting on the reader tipless+oracle agent, then launch.
- 22:20: reader tipless path DONE (`ORION_CV_TIPLESS_ARMED` default 1, conf 0.70, rise pair within
  200 ms inside the shot type's onset window while a press is armed; gate-9 floor respected; tip
  window must be READ not clamped — a real 0.90 proposal loss found+fixed; `TIPLESS LOCK:` ERROR
  line) — ep8 locks +933 ms / ep21 +949 ms (were +1256 / never); replay 0912 0/12000 diff on the
  shipped arming. `RELEASE ORACLE:` line per release (medians over 300-500 ms after the command,
  3.5 px threshold): **25/25 agree with the banner** on the drill dump. Reader `cold_first_read_
  unproven` veto still adds 33 ms at 60 fps between tipless proposal and publication. Python
  suites importing the reader/locator: 1002 passed / 16 skipped (agent); my run 228/1.
- 22:22: ACCEPTANCE LAUNCH `run_orion.local.ps1 -NoElevate` with BANNER_VERDICT_LIVE=1 PLAYER_ANCHOR=1
  ANCHORED_SEARCH=1 ANCHOR_REFUSE_MODE=1 CV_SHAPE_MIN_H_ARMED=8 CV_COL_W_MIN_ARMED=6
  EXPECTATION_WINDOW=1; no STALL_ATTRIB, no framedump. Build 21:50:42 (trim + fade grace + auto-seed),
  reader tipless + oracle, banner 30/30 recall. Watch: `METER BACKSTOP` count, `TIPLESS LOCK`,
  `RELEASE ORACLE verdict_proxy`, `BANNER TRIM`, banner tally, aborts.

## 09-15 ~22:40 local: tonight's batch dispatched (owner: "most if not all of this can be situated tonight")

Owner asks: close the loop WITHOUT the banner; ship NO METER with everything else; stick pull-down
for Rhythm ("already pre-built"); idle-time stalls; detector locking onto non-meters.
Answer given: oracle hill-climb (unsigned gap) closes the loop banner-free; NO METER back for
standstill/set shots with fades marked beta (late fades = animation onset drift, needs the pose
anchor → next update); the rest tonight.
- Engine agent (throwaway C:/Users/aaron/obld_en; app running): (A) `release_oracle` JSON parse →
  `observeReleaseOracle`, BannerLeadTrim descent on |gap| (3-shot medians, direction flip, 3 ms,
  ±15, banner sign takes precedence, `source=oracle|banner`, `ORACLE:` line); (B) un-shelve NO METER
  (3 doors reversed, mounts back, `noMeterFadesBetaCaption`, trim rides the hold with inverted sign
  via `no_meter_hold_trim_by_type`); (C) Rhythm right-stick pull-DOWN arms the shot
  (`intent=stick_down`, `shot_gate_arm source=stick_down`).
- Sidecar agent: idle stalls (signature-based duplicate check, no full-frame `array_equal`, locator
  result reuse on static idle frames ≤250 ms); `ORION_READER_IDLE_PUBLISH_GATE` (publish a lock only
  when armed, rising ≥3 pp/3 reads, or continuing a published lock; `idle_unpublished=` in health);
  `{"type":"release_oracle",…}` on the banner's stdout JSON channel (schema shared with the engine
  agent).
- Acceptance run (build 21:50:42 + reader tipless/oracle) is LIVE now under the owner.
- 22:45 OWNER DECISION: "no meter can wait, we can shelve that until we fix the fade issue but
  everything else is a yes" → engine agent told to DROP task B (NO METER stays shelved, three doors
  untouched); tasks A (oracle-driven trim) + C (stick pull-down) and the whole sidecar batch stand.
- 23:05: engine batch DONE in a throwaway build (obld_en deleted; NOT yet in place — app running):
  (A) `release_oracle` JSON (`event` or `type`) parsed at RemotePlaySession.cpp:~4903 →
  `OrionAppController::observeReleaseOracle` → `AutomationEngine::observeReleaseOracle`;
  `BannerLeadTrim::observeOracle()` = bounded 1-D descent on unsigned |gap| (6-deep series, decision
  every 3rd graded shot, strict-greater median flips direction, step 3 only while median > 3.5 px
  deadband, green decays like EXCELLENT, same clamp/persistence); oracle parked 2600 ms so a banner
  for the same epoch takes precedence (one move per epoch). Lines: `BANNER TRIM: … source=banner`
  (appended) / `… verdict=MISS|GREEN … source=oracle gap_px= median_gap_px= dir= flip= reason=`,
  `ORACLE: epoch= gap_px= proxy= bucket= used= reason=`. (C) Rhythm right-stick pull-down: the
  TempoStick transaction existed; its door `stickTempoArmAllowed()` only opened for
  `remote_play_input_source != "square"` → now also when `tempoEnabled` (the Rhythm switch);
  refused while a Square press is pending; wire label `intent=stick_down_edge` /
  `shot_gate_arm source=stick_down_edge rhythm=1` (existing value, kept). Task B (NO METER)
  dropped per owner; shelving tests untouched and green. Throwaway totals: Native 899/0/8 (= 893+8
  −2 libcrypto skips), Preview 55, Tally 32, MeterDelaySettings 19, ui contract 21.
- TODO when the owner closes: in-place rebuild (all six targets), suites, launch.
- 23:30: sidecar batch DONE: (1) idle stalls — capture `_isolate_immutable_frame` full-frame
  `array_equal` (2.81 ms + 6.2 MB temp/frame → gen-2 GC) → row-strided `[::8]` 0.29 ms
  (`ORION_CAPTURE_VERIFY_STRIDE`, `_VERIFY_FULL`); copy KEPT (`ORION_CAPTURE_ISOLATE_COPY` default 1 —
  it guards driver buffer recycling, not mutation); locator idle reuse on byte-identical unarmed
  frames ≤250 ms (`ORION_LOCATOR_IDLE_REUSE`); idle cost ~40 → ~5.9 ms/frame across both threads;
  attributor-harness test proves no STALL on the new path; replays 0/824 and 0/12000 diff.
  (2) `ORION_READER_IDLE_PUBLISH_GATE` (armed | rose ≥3 pp/3 reads | continuation ≤0.5 s): 120
  unarmed static frames publish 117 → 0; armed frames byte-identical (pinned); 7 legacy suites
  fixture the gate off; unarmed-shot first publication moves one read later (~17 ms live).
  (3) `{"event":"release_oracle","type":"release_oracle",…}` on the banner's stdout JSONL channel
  (`emit_stdout_jsonl`), `release_seq` = shot-gate physical epoch, once per release. Agent totals:
  56 suites 1237/16 skipped; whole tests/ 2788 passed / 12 pre-existing failures.
- READY FOR: owner "done" → in-place rebuild (six targets) → suites → launch (flags as the
  acceptance run; oracle loop + stick pull-down + idle fixes live).
- 23:50: IN-PLACE REBUILD (build 23:49:54) with the oracle loop + stick pull-down: OrionNativeTests
  **901/0/6**, MeterDelaySettings 19, PreviewPresentation 55, ActivityFeedPolicy 10, ShotVerdictTally
  32; ui contract 21. Relaunched (owner: "go ahead and relaunch") with the acceptance flags; sidecar
  now carries idle-stall fixes, idle publish gate, oracle forwarding, tipless path, banner 30/30.

## 09-16 00:05 local: session 23:51-23:54 graded — late streak = LONG HOLDS

42 presses / 40 vision / 1 backstop / 2 TIPLESS LOCKs; banner 22 EXC / 15 LATE / 3 EARLY; oracle vs
banner (engine-paired) **35/36 agree** live. Standstill holds: EXC 622-700 (median 648), LATE 649/651/
661 + **705/719/719/726/727/733/775**, EARLY 608/632 → 7 of 10 lates are vision releasing 55-125 ms
after the law because the drawn meter came late (forensics: onset lag, window did not move). Trim
engaged (max +6) but Isaiah moved the lead 274→286→299→274 (3 resets). Dispatched: engine
`vision_hold_band_ms` 40 / `vision_hold_band_fade_ms` 60 — vision release clamped to law ± band,
`HOLD BAND:` lines, clamped releases excluded from latency/hold learners (in-place build; app closed).
- 00:20: owner confirmed the stick variant he wants = "hold the right stick down, Venice lets it go"
  (return-to-neutral release), not the up-flick. Addendum sent to the hold-band agent (same build):
  `tempo_release_style` flick|letgo (default flick, env `ORION_TEMPO_RELEASE_STYLE`), RS → 0 at the
  same release instant, physical stick-down suppressed for the pulse window, `Release tempo: …
  release_style=` appended, RhythmCard "Release style: Flick up / Let go" segmented control
  (`rhythmReleaseStyle`, Q_PROPERTY `tempoReleaseStyle`).

## 09-16 00:35 local: HOLD BAND + Let-go release style BUILT IN PLACE (verified) — ready to launch

- `vision_hold_band_ms` 40 / `vision_hold_band_fade_ms` 60 (0 = off; env `ORION_VISION_HOLD_BAND_MS`
  / `_FADE_MS`): `clampVisionFireToHoldBand()` (AutomationEngine.cpp ~19478) at the 3 fire-instant
  sites (tick ~9213, reservation ~14349, sub-tick arm ~16077); anchor = `physicalPressMs` (the
  Square edge), law = `blindReleaseHold(type)` honouring the backstop's fade re-type; rhythm term for
  Tempo modes; correction ceiling 150 ms → `abstain_out_of_range` (unlisted types are Standstill-timed
  by construction); NO METER excluded; band applied to the frame-centred target then re-snapped.
  Lines `HOLD BAND: clamped_late|clamped_early|abstain_out_of_range|marker_suppressed`; `Release
  timing … hold_band=late|early|none` appended. Clamped releases: no `releaseMarker` (latency
  estimator skips), `no_meter_hold_by_type` / phase constant / landing lead learners fenced.
  10 legacy fixtures pin the band to 0. NOTE: press+700 clamps to 690 (band [610,690]).
- `tempo_release_style` flick|letgo (default flick; env `ORION_TEMPO_RELEASE_STYLE`):
  `ShotReleasePolicy.h` `TempoReleaseStyle`, `applyShotReleaseEdge` writes RS 0 under LetGo for the
  whole pulse window then `applyReleased()` holds neutral; submit-fence validator moves in lockstep;
  precise-fire mailbox samples the style at arm; `Release tempo … release_style=` appended;
  RhythmCard "Release style" segmented control (`rhythmReleaseStyle`, `_flick`, `_letgo`, caption).
  Pre-existing inconsistency flagged: controller-side `applyShotReleaseEdge` calls default
  `isFadeGesture=false` (never passed the fade latch) — separate look.
- My verification (build 00:32:57): OrionNativeTests **907/0/6**, MeterDelaySettings 19,
  PreviewPresentation 56, ActivityFeedPolicy 10, ShotVerdictTally 32; ui contracts 33/33.
- Launch line unchanged (BANNER_VERDICT_LIVE=1 + anchor/trio flags). Ship test = no late run, ~0
  backstops, slider untouched at 274.

## 09-16 ~15:00 local: REGRESSION on the 00:32 build + rollback to the known-good baseline

- Session 14:15-14:21 (6 min, 30 presses): 9 vision / **10 METER BACKSTOP** / 11 taps; banner
  5 EXC / 5 LATE / 6 EARLY. Six consecutive blind presses 14:20:46-14:21:11: locator `found`
  grew (meters proposed) and reader `locks` 3→42 in 2 s with `drops` flat, yet the engine received
  NO samples (no candidate → backstop). Only ONE `RESEED REFUSED` (fresh-after-ghost) line in the
  window; `idle_unpublished=` is in the part of DETECTOR HEALTH the 300-char relay trim cuts off.
  Same sidecar code as the 23:51 session (1 backstop/42) → suspects: the 00:32 engine build (hold
  band `marker_suppressed` removes the releaseMarker → reader post-release accounting; stick
  pull-down arming `src=stick_down_edge shot_type=Go-To` seen) and the rapid-fire/tap context.
  Hold band acted 4× as designed (720→687, 769→686, 742→692, 608→610; 876 abstained), no
  verdicts joined. Isaiah: "all over the place, it's a hot mess right now".
- ROLLBACK (no rebuild; env kill switches) to the config of the 71 % session (09-15 16:13):
  READER_GHOST_FORGET_LOCATOR=0 FRESH_AFTER_GHOST=0 BOX_LATCH=0 STATIC_ZONE_QUARANTINE=0
  IDLE_PUBLISH_GATE=0 LOCATOR_IDLE_REUSE=0 CV_TIPLESS_ARMED=0 VISION_HOLD_BAND_MS=0 (+FADE) 
  BANNER_LEAD_TRIM=0; kept: banner reader, anchor + trio, grace 100/220, frame-native, oracle
  line, release-style default flick. Next: a -Framedump reproduction to get per-frame reader
  stages (`detframes.csv` `stage`/`rejection_reason`), then re-enable layers ONE at a time.

## 09-16 15:05 local: BASELINE SESSION 14:58-15:00 — standstills perfect, fades late

29 presses / 19 vision / 3 backstops (ep2, ep11 standstill with a leftover meter at 101-105 % at
the press never re-acquired; ep21 Right Fade never seen) / 2 taps; banner 13 EXC / 8 LATE / 0 EARLY.
Oracle (reader): Standstill gaps 0/0/0/0/0/0.5/0.5/2.0 px (all green; holds 640-687); Fades
1.0/2.0 (green, holds 914/944) and 5.0/6.0/8.5/9.0/9.0 px (miss = LATE; holds 944-1020; settled
87.8-89.7 vs green bottom 94.4-97.2) → fades land ~7-10 ms late with the same slider lead as
standstills (no per-type term on the user path). Isaiah: "standstills basically perfect, fades just
need some work, all the fades I got were late". Dispatched: `lead_offset_by_type` (fades +8 ms
default, env `ORION_LEAD_OFFSET_FADE_MS`, kill `ORION_LEAD_OFFSET_BY_TYPE=0`) on the user/auto
lead paths. Next session = baseline + fade offset + the banner/oracle trim re-enabled (layer 1);
later layers one per session: GHOST_FORGET_LOCATOR (the ghost-at-press backstops), TIPLESS
(fade never seen), hold band, idle gate.
- 15:30: `lead_offset_by_type` BUILT in place (fades +8 / standstill 0 / other 0, ±40, env
  `ORION_LEAD_OFFSET_FADE_MS`, kill `ORION_LEAD_OFFSET_BY_TYPE=0`; applied on all three user/auto
  lead paths; `TIP RESERVATION … lead_offset_ms=` last; `BANNER TRIM … lead_offset_ms=`;
  `shotTypeWithBackstopFadeRetype()` shared with the hold band). Next launch = baseline kill
  switches + fade offset ON + `ORION_BANNER_LEAD_TRIM` re-enabled (layer 1).

## 09-16 15:40 local: session 15:23-15:26 (baseline + fade +8 + trim ON) — the spike class

57 presses / 45 vision / 1 backstop / 6 taps; banner 27 EXC / 16 LATE / 3 EARLY. Standstills
"perfect" outside two streaks (ep1-3 and ep52-57, 6 in a row). Trim climbed to +12 during the
streak, then EXC. Fades with +8: 5 EXC / 3 LATE / 2 EARLY (Right Fade trim −1.5 after 2 EARLY).
**Cross-session (7 sessions, 166 graded releases): `latency observation total_ms` > median+25 →
LATE 11/14 (79 %) vs 22 %; spikes +45..+73 ms; capture path clean in those windows → the extra
time is on the COMMAND side (engine → pipe → fork → PS5).** Late classes overall (45 LATE): spike
11, long hold 19, neither 16. Today's streak: 7 spikes (ep1, 15, 16, 17, 52, 53, 54), all in
rapid-fire with a ghost at the press. Agents: (A) transport forensics + instrument + fix the fork's
send path (chiaki-ng-src orion; no deploy); (B) `replay_with_presses.py` harness driving the real
press timeline to A/B the reader layers on the drill dump.
- 16:10 CORRECTION: the transport agent REFUTED the spike class — `total_ms` spikes are the
  estimator's ONSET-branch artefact on retraction (missed) traces (Δ46 ms, dead `peak>=97` deflate
  gate, rise buffer not reset per shot); local path ≤1.03 ms on all 111 releases; fork edge already
  immediate + redundant; 09-13 fork proposals never applied, not needed. Dispatched: estimator fix
  (scale-relative deflate gate `RETRACTION_TOL_PCT` 1.5, per-release rise reset) — it also stops
  contaminated observations poisoning `measured_latency_ms`/`lead_auto_seed`. Fork tree has
  uncommitted edits newer than the deployed Sep 2 binary (flag for ship).
- 16:30: REPLAY A/B DONE (`tools/diagnostics/replay_with_presses.py`, press timeline from the log,
  9 configs × 824 frames, `D:\NexusVision\framedump\_layer_ab\`): baseline 32/33 published, 28/32
  in time; every layer byte-identical to baseline except TIPLESS_ARMED (ep8 fade −329 ms, into the
  deadline, but at 88.8 % fill — candidate not proof). Live-log forensics of the 6-in-a-row loss:
  `loc_forget` 0→148 in lockstep with `locks`, drops flat (39 locks/42 seeds/39 forgets in 4 s);
  `forget_position()` wipes `_pending/_expect/_tipless` (two-frame first-sight pairs) on EVERY
  ghost eviction (~10 Hz live) → the real meter's pair never completes → first publication
  810-912 ms vs the 750 deadline. **Cause = ORION_READER_GHOST_FORGET_LOCATOR.** BOX_LATCH +
  STATIC_ZONE safe (inert for acquisition); FRESH hold (same trigger); IDLE gate/reuse hold (no
  live counter — trimmed off the health line); TIPLESS next, watched. Dump cannot reproduce (8.8
  fps; 0.76 evictions/press vs 19.5 live). Dispatched: forget preserves other-column pairs +
  ≤1 forget/press/zone + counters moved ahead of the relay trim + per-press withhold summary.
  Next launch: baseline + fade offset + trim + BOX_LATCH + STATIC_ZONE + TIPLESS (watched).
- 16:50: latency estimator FIXED (`latency_estimator.py`): `RETRACTION_TOL_PCT` 1.5 (env
  `ORION_LATENCY_RETRACTION_TOL_PCT`), deflate gate scale-relative → `reject=deflate_retraction`;
  `mark_release()` clears `_rise/_rise_peak` per shot; I aligned the onset branch to the same
  tolerance (was `+1.0`) so no accepted label is ever onset-dated. Validation on 710 logged rows:
  accepted p50 221.2 → 219.8, sd 20.7 → 17.7, 44 onset-dated rows (p50 256.4) rejected; the
  flagged session's seq 1/16/41 posterior jumps removed. Tests: retraction suite 11/11;
  `test_overshoot_trace_labels_the_lock_not_the_crossing` retired → rejected-as-retraction test;
  remaining failures pre-existing (`test_probe_ignores_sub_fmin_onset_samples` = tip_registration
  prior; nexus_svc_delay clamps). Also noted: seq 19-21 low-`f_stop` crossing rows (totals 153-254)
  are a separate population.
- 17:10: ghost-forget FIXED (`forget_position(box=None)` surgical: clears `_last_box/_last_ts`,
  keeps `_tip_pending/_pending/_expect/_tipless` unless co-located with the forgotten box, tol =
  max pair tolerance × scale; reader rate limit 1 forget/press/zone (64 px), never while a pair is
  live (defer ≤12 frames), kill `ORION_READER_FORGET_RATE_LIMIT=0`; `LOCATOR POSITION FORGOTTEN:`
  once/press). Health line reordered: `provider= loc_forget=fired/suppressed/deferred reseed_refused=
  staticq= idle_unpublished= idle_reuse= press_fresh_withheld= tipless=` inside the 300-char relay
  trim; `PRESS WITHHOLD SUMMARY: epoch= ghost_static= cold_first= idle_gate= fresh_zone= static_zone=
  reseed= end=` at press close. Replay: loc_forget 98→31, 0 presses shifted. Tests +19; my run of
  the reader/locator/anchor/oracle/banner/estimator suites green. READY: launch = baseline kills
  minus BOX_LATCH/STATIC_ZONE (on) + TIPLESS (on, watched) + fade offset + trim; GHOST_FORGET
  (rate-limited) next session.

## 09-16 20:30 local: SESSION 20:26-20:28 — "perfect! when I found the right lead" (slider → 269)

29 presses / 27 vision / **0 backstops / 0 taps**; banner **22 EXC / 3 LATE / 2 EARLY (81 %)**;
Isaiah walked the slider 274 → 259 → 261 → 266 → **269** (20:27:38); from that point 12 EXCELLENT in a
row through the end. Health counters (all visible now): loc_forget 0/0/0, tipless 0/0, staticq 0/0,
idle 0. Estimator (fixed): accepted totals 205-213, `fixed_ms` ≈ 200 → his aim margin = 269 − 200 ≈
**69 ms** (shipped `aim_margin_ms` default is 62 → consider 69 for fresh installs; the fade +8 sits on
top). Config: baseline + fade offset + trim + BOX_LATCH + STATIC_ZONE + TIPLESS (watched; never fired
— no tipless meter this session).
- 20:35: after the slider hit 269: 11/11 EXCELLENT (6 standstills, 5 fades; oracle 0-2.5 px; fade
  effective lead 277 = 269 + 8). Dispatched: ship defaults `aim_margin_ms` 62 → 69,
  `lead_factory_placeholder_ms` 274 → 269 (in-place build). Next session's one layer: GHOST_FORGET
  (rate-limited) to clear the leftover-meter blind class.
- 20:45: ship defaults BUILT in place: `aim_margin_ms` 69, `lead_factory_placeholder_ms` 269 (40
  string edits across 7 files; seed test now `208.3 + 69.0 = 277.3`; captions "using 269 ms" /
  "+ 69 ms margin"). All six targets current; suites re-run below. SHIP CONFIG (launch line) =
  BANNER_VERDICT_LIVE=1 PLAYER_ANCHOR=1 ANCHORED_SEARCH=1 ANCHOR_REFUSE_MODE=1 CV_SHAPE_MIN_H_ARMED=8
  CV_COL_W_MIN_ARMED=6 EXPECTATION_WINDOW=1 READER_BOX_LATCH=1 READER_STATIC_ZONE_QUARANTINE=1
  CV_TIPLESS_ARMED=1 BANNER_LEAD_TRIM=1; OFF: READER_GHOST_FORGET_LOCATOR, READER_FRESH_AFTER_GHOST,
  READER_IDLE_PUBLISH_GATE, LOCATOR_IDLE_REUSE, VISION_HOLD_BAND(_FADE). These env-only switches must
  become the DEFAULTS in source before packaging (customers do not run run_orion.local.ps1).

## 09-16 20:50 local: session 20:43-20:45 (ship config + GHOST_FORGET rate-limited) — "a mix of both"

22 presses / 22 vision / **0 backstops**; banner 10 EXC / 6 LATE / 5 EARLY; ghost-forget: 9 fired /
30 suppressed / 0 deferred, every ghost press published (`ghost_static` withholds 3-8 per press,
harmless); TIPLESS fired once (ep16 fade, fill 18, published in time). **Two populations:** ghost-at-
press shots hold 690-734 (ep4/5 LATE at 272/275; ep6/7 EXC only because the trim had reached +9 =
278) vs clean shots hold 595-660 (EARLY at 274 ×3; EXC at 271-273). One trim cannot serve both →
it oscillated; plus the EXCELLENT decay walked 271 → 274 → EARLY ×2 at the end. Isaiah moved the
slider 269 → 274 (trim reset). Fixes: (1) hold band ON next session (clamps the ghost-class
690-734 holds to ≤690 so the trim can settle ~272); (2) trim: EXCELLENT/GREEN = HOLD, idle decay
only after 12 consecutive EXC (`banner_trim_hold_shots`) — dispatched. Ghost-forget = SAFE, stays on.
- 21:05: trim HOLD policy BUILT in place (`BannerLeadTrim.h`: EXCELLENT/GREEN = hold; idle decay
  1 ms only after `banner_trim_hold_shots` = 12 consecutive good verdicts, `reason=idle_decay`;
  LATE/EARLY/MISS reset the streak; one streak per bucket shared by banner + oracle; not persisted).
  All six targets rebuilt; suites below. NEXT LAUNCH = ship config + GHOST_FORGET=1 (graded safe)
  + VISION_HOLD_BAND 40/60 ON (the watched layer for the ghost-class long holds).

## 09-16 21:10 local: session 21:00-21:01 (ship config + GHOST_FORGET + HOLD BAND 40/60) — "perfect, then again"

21 presses / 20 vision / 1 backstop; banner 11 EXC / 7 EARLY / 3 LATE. Isaiah set 286 then 281
(two trim resets). At 281: **10 EXC in a row** (holds 627-693, first sight 468-535 ms). Then the
tempo changed: first sight 434-450 ms, natural holds 583-602 → the game's window ~608 ± 5 (602 EARLY,
613/616 LATE). The HOLD BAND clamped 4/5 of those and was wrong on all four (746→696 EARLY,
583→616 LATE, 596→613 LATE, 602 untouched EARLY) → **band OFF again**; the trim STEPPED on
alternating E,L,E,L (2-of-4 hysteresis is satisfied by alternation). Insight: the "random" episodes
= a different animation class (quick / normal / slow, observable as press→meter-onset 434-450 /
468-535 / 550-630 ms for standstills) with its own window; per-type buckets are too coarse.
Dispatched: trim alternation guard (two MOST RECENT verdicts must agree) + tempo sub-buckets keyed
on meter onset (thresholds to be validated offline on sessions 11-14) with kill switch
`banner_trim_tempo_buckets`.

## 09-16 ~22:00 local: tempo sub-buckets + alternation guard BUILT; "stuck Square" diagnosed

- Trim: alternation guard (a step needs the two MOST RECENT verdicts to agree; E,L,E,L holds,
  `reason=alternating` once per flip); tempo sub-buckets keyed on the ENGINE onset
  (`firstMeterSeenMs − physicalPressMs`; capture-clock PICKUP values are ~36 ms earlier):
  Standstill quick <500 / normal 500-580 / slow >580, fades <775 / 775-915 / >915; keys
  `type/tempo` in `banner_lead_trim_by_type` (legacy → `/normal`), `banner_trim_tempo_buckets`
  (env `ORION_BANNER_TRIM_TEMPO=0`). Offline: EARLY-longer-than-LATE inversions 31.5 % → 18.2 %
  (Standstill), 40 % → 25 % (fades); session-14 tail 4 moves → 1 (the right one). Totals 915/0/6,
  Tally 42/0. Watch `tempo=quick` — ghost-at-press shots land there until GHOST_FORGET settles.
- "Stuck Square" = 7/300 real holds (400-980 ms, normal spacing, Square-down accepted by the fork
  in 0.5 ms) with NO meter candidate ever seen; the owner releases before the blind deadline
  (750 / 1170) → feels stuck; stick tempo works because it needs no meter. Not: engine output
  (recovers ≤11 ms), swallowed presses (120/120 epochs), debounce, queued presses, fork cadence
  (≤50 ms resend). Dispatched: never-seen probe → blind release at the law (no grace),
  `meter_backstop_never_seen_probe_ms` 100. Still owed: frames of one dead press (hidden meter vs
  missed meter).
- 22:40: never-seen collapse BUILT: `meter_backstop_never_seen_probe_ms` 100 (Standstill/other:
  meterless press answered at press+650, not 750; a candidate seen before the law restores the
  grace) + `meter_backstop_never_seen_probe_fade_ms` 0 (fades EXCLUDED — slow fades first-seen
  1000-1051 ms grade EXCELLENT under the 220 grace); fired line ` never_seen=1|0` (fact about the
  press). Full in-place rebuild + suites below. LAUNCH LINE unchanged from the 21:00 session except
  VISION_HOLD_BAND(_FADE)=0.

## 09-16 22:30 local: "STUCK SQUARE" PINPOINTED — Square at full sprint is ignored by the game

Isaiah gave the time (22:23 local = 03:23:51-54Z): six Square presses in a row (ep41-46) produced no
meter/no shot; ep41/42 held 962/640 ms with `ls=(127,35)/(127,18) r2=255`; then a right-stick
pull-down/up (ep47/48) produced a meter in 486 ms. Every Square-down was accepted by the fork
(`square_bit=1 local_udp_accepted`), every physical up propagated (route audits recover ≤11 ms).
Across the archive: ALL 5 long dead presses (923-980 ms) = LS pinned ≥100 + R2=255 (sprint with
the ball) — session 11 ep25/31/35 + tonight ep41/42; answered presses have R2 held only 26 % and at
lower stick magnitudes. 2K27 does not start a Square jumper at full sprint; the Pro Stick does →
"stuck". The other 5 dead presses (standstill, 382-539 ms, no stick, no R2) are a separate small
class (now bounded at 650 by the never-seen collapse). Dispatched: `sprint_release_on_square`
(default ON, R2 ≥ 200 at the Square edge → output R2 forced 0 for the press; `SPRINT RELEASED FOR
SHOT:` line; kill `ORION_SPRINT_RELEASE_ON_SQUARE=0`).

## 09-16 22:45 local: ANIMATION ANCHOR started (owner: "whatever gets shots consistent")

Two agents (Opus 5), neither touching the timing path: (A) design + feasibility →
`docs/ANIMATION_ANCHOR_V2.md`: pose landmarks on the shooter crop (best.pt YOLO26n-pose, ONNX FP16)
vs meter tip / "3"-icon-off / banner on the drill dump + 2K26 60 fps clips; per-frame cost on this
box; fusion rule + kill switch; what needs a 60 fps dump. (B) `shot_records.py`: one JSONL training
record per shot from normal play (press, type, tempo, onset, release, oracle gap, banner verdict,
icon-off, pose_track placeholder) to D:\NexusVision\shot_records\ + `SHOT RECORD:` line; and a
press-window 60 fps dump mode (`ORION_FRAMEDUMP_PRESS_WINDOW=1`, `ORION_FRAMEDUMP_CROP=shooter`)
with a bounded writer that never drops inside a window. Engine sprint-release fix still building.
- 23:05: `sprint_release_on_square` BUILT in place (default ON, R2 ≥ 200 at the physical Square-down
  edge → output R2 = 0 for the whole press; `applySprintReleaseOnSquare()` AutomationEngine.cpp:4286,
  the single writer of output.r2; `SPRINT RELEASED FOR SHOT:` line; `Physical shot epoch …
  sprint_released=`; `Square-down delivery identity … delivered_r2= sprint_released=`; OUTPUT
  DIVERGENCE attributes it; kill `ORION_SPRINT_RELEASE_ON_SQUARE=0`). Suites below (Native 928/0/6).

## 09-16 23:20 local: LABELS FOR FREE — `shot_records.py` + press-window framedump (sidecar)

Every shot the owner takes in normal play now produces ONE labelled training record, so the
animation-anchor model can be fitted from ordinary sessions instead of hand-labelled clips.

- **`shot_records.py` (new, repo root, sidecar-importable).** `ShotRecorder` keyed on the
  SHOT-GATE PHYSICAL EPOCH — the same id `release_seq`, the banner verdict and the release
  oracle already use. Joins: press ts + `source` + `shot_type`/`rhythm` (arm receipt, control
  thread), the reader's PICKUP fields (read off `AsyncMeterLocator._base.pickup` at the release,
  because the reader's release hook is what flushes that record), the meter onset, the release
  marker (`release_ms`), the tempo bucket, `RELEASE ORACLE` (`gap_px`/`settled_fill`/
  `verdict_proxy`), the attributed `BANNER VERDICT` (timing word + `release_delay_ms`), the
  nameplate-"3"-cell samples, and a reserved 60 fps `pose_track` (null; schema
  `coco17/[ts_ms,x,y,conf]` pinned now so today's corpus stays joinable). One JSON line per shot,
  **appended** + fsynced to `D:\NexusVision\shot_records\<session>.jsonl`, plus one
  `SHOT RECORD: epoch= type= tempo= onset_ms= release_ms= oracle_gap= banner= icon_off_ms=`
  **ERROR** line (WARNINGs share one 1 s relay slot that the SHOT-GATE receipt already takes).
- **Close policy.** A released press stays open `ORION_SHOT_RECORD_GRACE_MS` (3500) because the
  oracle lands ~0.5 s and the banner 1.0-1.7 s after the command; it closes EARLY on
  release+oracle+banner (`closed_reason=complete`). A press with no release closes at
  `ORION_SHOT_RECORD_TIMEOUT_MS` (8000) with `outcome=unanswered` — that is the stuck-Square
  class, and it is a record, not a gap. Disarm → `outcome=disarmed` + reason. Open presses are
  bounded at 8 (`superseded`).
- **TEMPO is computed with the ENGINE's own thresholds** (mirrors `BannerLeadTrim::tempoFor`):
  Standstill quick <500 / normal 500-580 / slow >580, Left/Right Fade <775 / 775-915 / >915,
  "Other" always normal. The sidecar's first sight is ~36 ms EARLIER than the engine's first
  accepted sample, so the record converts before bucketing:
  `onset_engine_ms = onset_ms + ORION_SHOT_RECORD_ONSET_LAG_MS` (36) — and writes `onset_ms`,
  `onset_lag_ms`, `onset_engine_ms`, `tempo` and `tempo_key` so a later re-calibration of the lag
  can re-bucket the whole corpus offline.
- **The "3" icon.** `tools/diagnostics/hud_3pt_icon.py`'s `probe()` measured **157.7 ms/frame**
  (8-scale template sweep over a 566-row band) — ~9.5 frame times at 60 fps, unrunnable live. So
  `icon_off_ms` is NULL and derived offline from `icon_cell`: the mean brightness + dark fraction
  of the "3" cell sampled at 10 Hz in the release window (**0.026 ms/sample**), read at the plate
  position `player_anchor` already found that frame. Opt-in: `ORION_SHOT_RECORD_ICON=1`.
- **Costs (measured).** Control thread, the real `arm_shot_gate` + `release_shot_gate` pair
  including the existing receipts: **p50 0.081 ms, p99 0.32 ms, max 0.46 ms per shot** (budget
  2 ms); the recorder's own calls are p50 0.053 / p99 0.29 ms. Detect thread:
  `_shot_record_frame_hook` **0.00031 ms/frame** with the icon sampler off.

- **`ORION_FRAMEDUMP_PRESS_WINDOW=1` — the dump that actually keeps frames.** The 09-15 drill lost
  595 of 1421 because a 1080p PNG costs **49.2 ms** (level-1 is worse: 93.6 ms, 1.36 MiB) against
  a 16.7 ms frame budget, behind a 1-slot queue, a 25 % duty cooldown and a 0.1 s throttle. Press
  window mode dumps **every** frame from press−200 ms to release+400 ms (hard cap 3000 ms) at full
  rate and **nothing** outside a window: no interval throttle, no gameplay gate, no duty cooldown
  inside the window, queue 96, JPEG-90 by default. Measured: 1080p JPEG-90 **4.27 ms / 83 KiB**
  (~5 MB/shot), 400x500 crop JPEG-90 **0.57 ms / 8.6 KiB** (~0.5 MB/shot). A pre-roll ring of
  frame REFERENCES (no copy; capture already isolates each frame) supplies the press−200 ms half.
  Files are named `ep<epoch>_f<idx>_<det>_raw.jpg`, so a frame names its own shot and the JSONL
  joins by epoch alone; `frames.csv` is unchanged. Drops are counted per window and reported in
  `FRAMEDUMP PRESS WINDOW: epoch= frames= preroll= dropped= skipped= idx=a..b` (ERROR) and in the
  heartbeat (`windows= window_frames= window_dropped=`, `state=press_window|armed:between_presses`).
  Knobs: `_PRESS_PRE_MS` 200, `_PRESS_POST_MS` 400, `_PRESS_MAX_MS` 3000, `_CROP=shooter`
  (+`_CROP_W/_CROP_H`, default 400x500), `_FORMAT=jpg|png`, `_JPEG_QUALITY` 90, `_PREROLL_MAX`,
  `_QUEUE_DEPTH` (cap 512 in this mode). With the knob OFF every legacy default is byte-identical
  (PNG, queue 1, cap 900, duty cooldown, gameplay gate).
- Detect-thread cost of the producer: **0.0043 ms/frame** in a window (bounded `put_nowait`, no
  copy), **0.0019 ms/frame** outside one (the ring append).
- Launch line: add `ORION_FRAMEDUMP=1 ORION_FRAMEDUMP_PRESS_WINDOW=1
  ORION_FRAMEDUMP_ROOT=D:\NexusVision\framedump` (and `ORION_FRAMEDUMP_CROP=shooter` for the
  0.5 MB/shot variant) to the ship config. `ORION_SHOT_RECORDS` defaults to 1 and needs nothing.
- Tests: `tests/test_shot_records.py` 32, `tests/test_shot_records_wiring.py` 6,
  `tests/test_framedump_press_window.py` 9 (30 s synthetic 60 fps feed, 10 press windows, 0 drops
  inside, 0 frames outside, filenames join the JSONL by epoch). Regression sweep over the 74
  suites importing the orchestrator/reader/locator/banner/anchor: **1501 passed / 13 skipped**;
  the only failures are the two wall-clock CPU-budget tests in `test_stall_attributor.py` /
  `test_banner_verdict_live.py`, which pass in isolation and flake under a 1500-test batch.
- NOTE: the recorder refuses to arm under pytest unless `ORION_SHOT_RECORDS` is set explicitly —
  the corpus is production data and a test run must not append synthetic shots to it.

## 09-16 23:15 local: SPRINT FIX REFUTED LIVE — reverting; new mechanism

Session 23:04-23:05 (52 presses): with `sprint_release_on_square` ON every R2-held press went dead
(9 long holds 806-1105 ms + ~30 taps, no meter, no shot; incl. ls=(14,14) = a normal moving
pull-up); standstill presses fine. Cross-session base rates without it: sprint+pinned 81 answered /
4 dead / 4 backstop (91 % shoot). → sprinting does not block Square; an R2 RELEASE in the same game
frame as the Square press does (we manufactured it 100 %; the thumb does it ~9 % on sprint
pull-ups). Dispatched: default OFF + `square_press_r2_hold_ms` 50 (latch output R2 through the
press window) + `PRESS ANALOG TRACE:` per press (R2/stick at −32..+200 ms, R2 release-edge offset).
Memory `stuck-square-is-full-sprint.md` rewritten as CORRECTED. Isaiah: "square button bug is worse".
- 23:20: ANIMATION ANCHOR feasibility DONE → `docs/ANIMATION_ANCHOR_V2.md` (D:\NexusVision\
  anchor_study\). Thesis CONFIRMED: press→pose-half-rise vs press→meter-onset slope 1.048, r 0.653
  (the "meter lag" is the animation moving). 08-09 cue-TCN failed for PLAYER LOCK (only 41/251
  tracks on the shooter), not data volume; 5v5 nearest-to-meter lock flips 20 % of frames →
  player_anchor identity is risk #1. Cost: ORT CUDA fp32 p50 7.0 ms @320-448 (overhead-bound, crop
  buys nothing, fp16 slower, DML not testable in this venv; `import torch` first or ORT silently
  runs CPU); a pose thread at 48 Hz left the detect tick p99 0.73 ms. Ship order: (a) anchor-keyed
  tempo bucket (moves no fire instant) → (b) inverse-variance fusion gated on the 60 fps residual →
  (c) NO METER fade hold. Sizing: ~90 shots for the two live buckets (2-4 sessions). BLOCKER: the
  8 fps dumps have a 48 ms interpolation floor vs a ~15 ms effect → need `ORION_FRAMEDUMP_INTERVAL=0`
  on D: (~3.2 GB/min; verify frames.csv gap p90 — the writer dropped 595/1421 at 0.1 s).
  best.pt's training corpus no longer exists on C: (retrain needs a player-locked dump).

## 09-16 23:45 local: SPRINT FIX OFF + R2 HOLD + PRESS ANALOG TRACE — BUILT IN PLACE

Three parts, one in-place rebuild of `OrionNative` + both suites. App was closed; nothing launched.

- **`sprint_release_on_square` default → FALSE** (`AppConfig.h:703`, `AutomationEngine.h:171`).
  The code and all six tests are KEPT — only the pinned default moved, and
  `ORION_SPRINT_RELEASE_ON_SQUARE=1` re-arms it. `introducedKeys` note rewritten to say the key now
  changes nothing. Test helper `sprintReleaseConfig()` turns it on explicitly, and the settings
  round trip now persists the NON-default value (true) so the trip still proves something.
- **NEW `square_press_r2_hold_ms`, default 50.0, clamp 0..150, env `ORION_SQUARE_PRESS_R2_HOLD_MS`**
  (ignore-non-numeric / clamp-numeric). `AppConfig.h:732`, save-clamp `AppConfig.cpp:518`, persist
  `:662`, load `:1497`, engine + env `AutomationEngine.cpp:1809-1822`. Predicate
  `squarePressR2HoldWouldEngage()` `AutomationEngine.cpp:4313`, line
  `finishSquarePressR2Hold()` `:4357`, shaping inside the SAME single writer of `output.r2`
  (`applySprintReleaseOnSquare()` `:4377`, called from `process()` `:4271` after
  `applySquarePassthrough`). At a physical Square-DOWN edge with phys R2 ≥ 200 (the shared
  `sprint_release_r2_threshold` cut) the OUTPUT R2 is PINNED at the edge value for 50 ms, so a
  trigger release the thumb lands in the button's own game frame is not forwarded; at the end of
  the window the output follows the physical trigger again, in both directions. Mutually exclusive
  with the sprint release by construction (the predicate refuses whenever that one would engage)
  and explicit precedence in the writer. Untouched: no Square, phys R2 < 200 at the edge,
  stick-armed presses, NO METER, disarmed/disabled. `0` = inert, byte-identical.
  Line, once per press and ONLY when it actually masked a release (phys_r2_min below the cut):
  `R2 HELD THROUGH PRESS: epoch= held_ms= phys_r2_min=`.
- **NEW `PRESS ANALOG TRACE` (always on, shapes nothing)** — `updatePressAnalogTrace()`
  `AutomationEngine.cpp:4511`, emit `:4480`, nearest-sample `:4454`, ring + state
  `AutomationEngine.h:5856-5890`. A 64-sample ring of the PHYSICAL pad (4 ms tick ≈ 256 ms) is
  pushed every `process()` tick before the state machine, so a press the engine gates out is still
  described. One line per physical Square-down edge, emitted at press+200 ms or at the physical end
  of the press, whichever is first:
  `PRESS ANALOG TRACE: epoch= r2=[…] ls=[…] rs=[…] r2_release_edge_ms=`
  — eight values per array on the FIXED ladder −32,−16,0,+16,+33,+50,+100,+200 ms (nearest sample
  within 12 ms, else `-`; ls/rs are magnitudes), and `r2_release_edge_ms` = the offset of the first
  R2 drop below 100 in the window, **which can be negative** (the pre-press half is scanned out of
  the ring at the edge, because a trigger that came up one or two frames BEFORE the button is
  exactly what the mechanism accuses). `none` when the trigger never let go. The negative slots are
  resolved at the edge so a short ring can never lose them; `reset()` drops a pending trace
  silently.
- Tests: 5 new (`squarePressR2HoldMasksATriggerReleaseInsideTheWindow` — incl. the +50 ms boundary
  and `held_ms=50.0 phys_r2_min=0`; `…LeavesEveryOtherPressAlone` — 4 refusals;
  `…ZeroIsByteIdentical` — every other byte compared; `…SettingsRoundTripAndEnvOverride`;
  `pressAnalogTraceDescribesEveryPhysicalSquareEdge` — full press AND tap, exact line format).
  **OrionNativeTests Totals: 933 passed, 0 failed, 6 skipped** (baseline 928/0/6 + 5 new);
  **OrionShotVerdictTally 42 passed, 0 failed**. No new compiler warnings.
- 23:40: BUILT in place (verified): `sprint_release_on_square` default FALSE (code kept);
  `square_press_r2_hold_ms` 50 (output R2 latched at the edge value for the window when phys
  R2 ≥ 200 at a Square-down; `R2 HELD THROUGH PRESS:` only when it masked a release; shares the 200
  cut); `PRESS ANALOG TRACE: epoch= r2=[−32,−16,0,+16,+33,+50,+100,+200] ls=[…] rs=[…]
  r2_release_edge_ms=` per physical Square edge (always on). Native 933/0/6. Next launch = ship
  config + `ORION_FRAMEDUMP=1 ORION_FRAMEDUMP_PRESS_WINDOW=1 ORION_FRAMEDUMP_ROOT=D:\NexusVision\
  framedump` (60 fps press windows, JPEG) so anchor data accumulates; shot records on by default.

## 09-17 ~00:15 local: cheap fade paths checked on 16 sessions (59 fades with banner + first sight)

- Stick magnitude at the press (127-152 on ALL fades) and R2 (255 on ALL) separate nothing → the
  "input at press" alternative is refuted as a magnitude question.
- Meter onset does NOT separate fade verdicts across the archive (EXC onsets 667-1051, LATE 784-1091,
  EARLY 750-1050 — continuous, not clustered); the 09-15 drill's "late fades = later onset" was a
  7-shot artefact. Hold − onset ≈ 145 ms on every fade (the release lands at a constant offset
  after first sight) yet verdicts scatter → the game grades something the drawn meter's onset does
  not carry. Tempo buckets will help fades less than hoped; the pose set-point is the remaining
  pre-release candidate (demo agent measuring its 60 fps jitter now); the new PRESS ANALOG TRACE
  answers the lean-TIMING variant next session.

## 09-17 ~00:30 local: POLL-PHASE TRACKER — owner's pick for fades ("learning the poll phase beats it")

Owner: no way to widen the window (rating-gated); frame-native only partially fixes the coin flip;
build the phase learner if it gets fades to 60-70 %. Dispatched measurement-first (Opus 5): on 16
sessions, release instant mod 16.667 ms in the engine / game-frame / capture clocks vs verdict +
signed oracle gap; per-session circular fit + autocorrelation + shuffled null; drift rate; the
reader-noise alternative; convergence estimate → `docs/POLL_PHASE_TRACKER.md` with the engine
design (per-session circular estimator, ±half-frame nudge after the frame-native centre, kill
switch) or a stated blocker. Pose real-time demo agent still running.

## 09-17 ~01:20 local: DISK + a parallel packaging session

- C: fell to 1.86 GB (blocks the Nuitka sidecar bundle which builds into `build\sidecar` on C:).
  Moved `logs/diagnostics/framedump` (5.9 GB, sessions 09-14) → `D:\NexusVision\framedump_from_c`
  with a JUNCTION at the old path (all tools resolve); deleted my own scratch (`stage` 1.8 GB of
  09-14 frame copies, finished pytest basetemps). C: now ~9.5 GB. Still on C: and NOT mine to
  remove (owner's call): `archive/local-runtime` 5.7 GB + `release/` 1.2 GB + `build/sidecar`
  0.6 GB = six `orion-package-20260917-*` trees and `orion-package-1.0.0.zip` built 22:31-23:48
  by ANOTHER session (Codex / the System32 Claude session) — those packages predate the ship
  defaults + tracker and must not ship; `Windows.old` 71 GB; `symbols*` 8.7 GB; `.codex` logs
  2.2 GB; Downloads/Omnisphere 57 GB; VMs 153 GB.
- Running: poll-phase measurement (→ docs/POLL_PHASE_TRACKER.md), pose real-time demo, ship
  defaults flip (→ docs/SHIP_CONFIG.md, tests/test_ship_defaults.py), pre-ship repo audit
  (→ docs/PRE_SHIP_REPO_AUDIT.md: purge list + git add list for owner approval).
- 01:35: PRE-SHIP REPO AUDIT DONE → `docs/PRE_SHIP_REPO_AUDIT.md`. Owner decisions owed: (1) purge
  25 tracked competitor-RE / auth-bypass scripts in the repo root (12 named + 13 by content sweep;
  proved unreferenced by product/tests/manifest/CMake); `git rm` + history scrub if ever pushed;
  (2) `codesigning/venice_update_signing.pem` is ON THE BOX (119 B, gitignored) — standing rule says
  it must not be; owner to remove/relocate; (3) `.gitignore` gap FIXED now (`*.json.bak-*`,
  `/probe.txt` — 14 backups carried the dev IP + key). MUST-ADD list (product modules, panel_grade
  + npz, 8 native headers, 5 native tests, admin QML, ~50 tests, tools+tests together) is in the
  report. 83 ` D` = old packer retired for Lethe (intentional).

## 09-17 ~02:00 local: POLL-PHASE TRACKER REFUTED → frame-centre fire repair instead

`docs/POLL_PHASE_TRACKER.md`: 284 graded shots; release phase vs game grid measurable (sd 1.07)
and stable (drift −0.05 ms/s, console 60.003 Hz) but verdict independent of it (P(EXC) amp 0.045,
p 0.73; oracle p 0.19; per-session below null; bound ~20 pp vs ~100 predicted) → NOT built. Side
finding: `fire_target=frame_centre` inert — schedule carries `frame_offset_ms` (99 % identity) but
`command_issued = anchor + phase_const − lead` (MAD 0.03, slope 0.29); |phase − centre| 3.83 vs
4.17 uniform. Residual LATE ↔ late arming (`tip_eta_ms` AUC 0.61, `resv_fill_pct` 0.40) and a
worse grid fit. Dispatched: engine agent to reproduce the identity in a failing test, fix every
re-arm to use the aligned instant, append `fire_grid_phase_ms= fire_centre_delta_ms= fire_target=`
to `Release submit:` and `aligned_ms= unaligned_ms=` to `Scheduled fire:`.
- 02:15: SHIP DEFAULTS IN SOURCE (`docs/SHIP_CONFIG.md`, `tests/test_ship_defaults.py` 30 tests,
  `AutomationEngineTests::shipConfigDefaultsArePinned/…SurviveAKeylessSettingsFile`): the packaged
  launch path writes NONE of the ship switches (verified: main.cpp timing profile, controller
  envDefaultOn, RemotePlaySession startSidecar reader profile) → source defaults = customer
  behaviour. Flipped ON: BANNER_VERDICT_LIVE, PLAYER_ANCHOR, ANCHORED_SEARCH, EXPECTATION_WINDOW,
  CV_SHAPE_MIN_H_ARMED=min(8,·), CV_COL_W_MIN_ARMED=min(6,·). Flipped OFF: FRESH_AFTER_GHOST,
  IDLE_PUBLISH_GATE, LOCATOR_IDLE_REUSE. C++: hold band 40/60 → 0/0 in AppConfig header + loader
  (applyConfig NaN fallback → 0 folded into the frame-centre agent's build). Native 935/0/6; full
  tests/ 2917 passed / 13 pre-existing failures (native_qtest_diagnostics ×?, discord guard_refresh
  signature drift). The dev launcher still overrides via env.
- 02:25: my verification of the ship-defaults suites: 207 passed / 1 skipped (the 5 "errors" on
  the first run were a missing pytest basetemp parent after my scratch cleanup — harness, not
  code). Python tree FINAL → sidecar bundle rebuild dispatched (`scripts/build_orion_sidecar.ps1
  -Force`, log to D:\NexusVision\bundle_build\, NO deploy). Engine: frame-centre repair + the
  applyConfig NaN fallbacks 40/60 → 0 in flight. Owner decisions still open: purge 25, pem, the
  other session's 7 GB of stale packages, Windows.old, then commit / deploy / Gumroad / red-team.

## 09-17 ~02:45 local: owner said "remove ×4"

- Stale packages REMOVED: `archive/local-runtime/orion-package-2026091*` (8 trees, 5.7 GB) +
  `release/orion-package` + `release/orion-package-1.0.0.zip` (1.0 GB); kept
  `release/orion-packing-report.json`, `release/update_manifest.json` (Aug). C: 9.6 → 16.1 GB free.
- Purge of the 25 RE/bypass scripts: `git rm` (staged deletion, NOT committed) after a repo-wide
  reference check — running in the background at the time of writing; result recorded below when
  it lands. History scrub (if the repo was ever pushed to zaehuncho/Venice) is a separate,
  explicit decision.
- Signing key: NOT deleted. `codesigning/venice_update_signing.pem` is the Ed25519 PRIVATE key
  whose public half is pinned in the app (`tools/package_orion_release.py` ED25519_PUBLIC_KEY_B64
  "orion-ed25519-v1", `UpdateManifest.cpp` / `updater_main.cpp` trust anchor); the packager reads
  it via `ORION_UPDATE_SIGNING_KEY_PEM=<path>`. Deleting the only copy = no future update can be
  signed; rotation requires one last update signed by the OLD key to ship the new public key.
  Correct action = move it off the box (USB / password manager / signing machine) and point the
  env var at it at packaging time. `codesigning/artifact-signing-metadata.json` is Azure Trusted
  Signing metadata (cert profile / account / endpoint — no secret).
- Windows.old (71 GB): the sandbox refused an elevated Disk Cleanup (irreversible) — owner runs
  `cleanmgr` → C: → Clean up system files → "Previous Windows installation(s)".
- 02:45: SIDECAR BUNDLE BUILT (system Python312 + Nuitka 4.1.3 + onnxruntime-directml 1.22.0;
  5.5 min): `build\sidecar\autogreen_sidecar.dist\OrionSidecar.exe` 36.2 MB, dist 61 files /
  0.26 GB, manifest `ORION_SIDECAR_BUILD.json` v4, source digest `6aa86c09…c69c4`, exe sha256
  `01e359cb…8559`; 0 `.py` in the dist, `orion_panel_grade` compiled, `panel_templates.npz`
  bundled, detector smoke DML p90 18 ms (gate 35), `--build-identity-file` matches, `--verify`
  clean. NOT deployed. **Must be rebuilt after the root purge lands** — the manifest hashes every
  repo-root `*.py`, so removing the 25 RE scripts changes the source digest. test_ship_defaults =
  30 tests (doc count corrected).
- 03:00: PURGE DONE — 25 root RE/bypass scripts `git rm`'d (staged, not committed), 0 references
  found by a repo-wide sweep, none on disk, no root file mentions helios/inputsense. Bundle rebuild
  #2 dispatched for the new source digest. History scrub = owner decision (remote zaehuncho/Venice).
- 03:05: POSE REAL-TIME DEMO DONE (D:\NexusVision\pose_rt\, `tools/diagnostics/pose_realtime_demo.py`):
  60 fps real-time on 1080p60 (CUDA), 14/17 keypoints @0.84; 30 Hz duty costs 14.2/17.4 ms (GPU
  downclock) not 7/10; nameplate lock 3.7× fewer identity jumps than top-person; set-point via
  wrist-velocity onset = the gather (estimator noise 12-14 ms) → fusion stays gated, tempo-bucket
  keying viable; 2K26 nameplate (star) invisible to player_anchor; E: not mounted. Post-ship item
  per owner. Videos sent to Isaiah (best_clipB.mp4, sheets).
- 03:15: full Python suite on the purged + defaults tree: 2910 passed / 2 failed — discord
  guard_refresh (pre-existing signature drift) and `test_b_idle_fallback_still_emits_at_60hz`
  (wall-clock test; 3/3 pass in isolation; flaked under the concurrent Nuitka compile). Signing key
  MOVED out of the repo to `C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem` (EFS
  encrypted folder, owner's account only); packaging: `$env:ORION_UPDATE_SIGNING_KEY_PEM =
  "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"`. Owner still owes an OFF-BOX
  copy (USB / password manager) — losing the key = no future updates can be signed.
- 03:25: SIDECAR BUNDLE #2 (post-purge) BUILT: 41 manifest sources (65 − 24 .py; the .cpp was never
  bound), source digest `b7399792fde4863e17d076ba22db201ec13856cc99e94d8ba4453f9825e1a467`, exe
  sha256 `f99f956d…c686d`, built 07:55:37Z, 2m09s, `--verify` clean, 0 `.py` in dist, DML smoke
  p90 17 ms. `build\sidecar\autogreen_sidecar.dist\OrionSidecar.exe`. NOT deployed. This is the
  ship candidate unless source changes again (the frame-centre fix is C++ only → no rebuild).
- 03:30: signing key confirmed EFS-ENCRYPTED at `C:\Users\aaron\Desktop\VeniceSigning\
  venice_update_signing.pem` (Encrypted attribute set; the earlier cmd-routed cipher call had
  silently done nothing). Repo copy gone. Owner still owes an off-box copy.

## 09-17 03:40 local: FRAME-CENTRE FIRE REPAIRED (the §10 finding reproduced in a failing test)

Site: `processAutonomousLiveMeterHolding()` (4 ms tick) armed `tip − lead` unaligned and killed the
sub-tick's aligned token whenever |frame_offset| > 1 ms (~88 %): `TIP TOKEN KILL
site=tick_reschedule` → re-arm unaligned at 250 Hz = last writer. Fixed: the tick snaps via
`phaseAlignedFireTargetMs()` exactly like the mirror; both arm sites stamp `fireTargetMode /
fireTargetFrameOffsetMs / fireAlignedFireAtMs / fireUnalignedFireAtMs`; `schedFireArmedFrameOffsetMs_`
restores `TIP PHASE RATE RECOVERY` on centred shots. Instrumentation: `Scheduled fire … aligned_ms=
unaligned_ms=`; `Release submit … fire_grid_phase_ms= fire_centre_delta_ms= fire_target=` (lead
added before placing the instant on the grid; `releasePhaseOnGrid()` in GameFramePhase.h;
sentinels −1/−99). applyConfig NaN fallbacks 40/60 → 0. Tests +4 (fail on the old code: armed
468.5623 == tip − lead). Legacy non-autonomous MeterVision reschedule left unaligned (unused).

## 09-17 03:10 local: acceptance session #1 — standstills perfect, FADES BLOCKED by a stale setting

33 presses / 19 vision / 1 backstop / 10 unanswered; banner 12 EXC / 4 LATE / 3 EARLY. Every fade
attempt (R2=255 + stick lean) died unanswered (holds 187-932 ms, no meter) — `SPRINT RELEASED FOR
SHOT` fired 9× although the source default is now FALSE: the 23:04 session (default true at the
time) had persisted `sprint_release_on_square: true` into the dev `settings.json`, and the file
wins over the default. The R2 hold-through never engaged (physical R2 stayed held; trace lines
show r2=[255…255] with no release edge). FIX: settings.json → false, re-signed. Customers never
had a true persisted (fresh installs get the source default). Standstills: 12/16 EXC, 0 blind.

## 09-17 03:12+ local: acceptance session #2 (sprint-release off) — in progress at 03:12

53 presses / 44 vision / 3 backstops / 2 taps (partial). Standstill: 15 EXC / 0 LATE / 3 EARLY
(83 %, oracle median 0 px). Fades: 13 EXC / 8 LATE / 3 EARLY (54 %); normal tempo 12/6/2; LATE
holds 916-1074 vs EXC 894-970; fade oracle LATE 4-11 px. **Frame-centre repair CONFIRMED LIVE:**
`fire_centre_delta_ms` median |d| 0.13 ms, 42/42 within ±1 ms (was 3.83 ms mean, 23 % within
1 ms). Isaiah: "standstills are like 95 % perfect, fades are getting there". R2 hold-through
engaged once; sprint release 0.
- 03:25: owner asks: standstills → ~100 % without earlies (answer: slider back to 269, hands off —
  the 3 earlies came after the 03:08 move to 286; frame-centre now exact); mid-range fades →
  standstill-level (dispatched: `range` = third trim dimension for fades from the nameplate "3"
  icon at press+40..120 ms via the collector's cell sampler, calibrated vs the banner DISTANCE
  cell, `shot_range` message, `lead_offset_fade_mid_ms` 6, kill `ORION_BANNER_TRIM_RANGE=0`);
  rare no-meter shots (dispatched: read-only check of the 3 backstop epochs in tonight's press-
  window dump — drawn & missed / tipless / not drawn). Three-point fades at 50-60 % accepted.
  Sprint-release fence agent still running (throwaway build).
- 03:35: `sprint_release_on_square` FENCED (`g_sprintReleaseAllowed` AppConfig.cpp:56; load() ANDs a
  persisted true away + logs `SPRINT RELEASE: persisted true ignored (feature fenced 2026-09-17;
  env ORION_SPRINT_RELEASE_ON_SQUARE=1 to re-enable for testing)` once; save() writes false; the
  env var (exact "1") is the only door; `setSprintReleaseAllowedForTesting`). Throwaway build
  938/0/8 (+1 test, 2 libcrypto skips); NOT yet rebuilt in place (app running). Running: dump
  check of the 3 backstop epochs; range-aware fade trim (sidecar half first, engine half after the
  fence marker).
- 03:40 correction: of the 3 standstill EARLYs, 1 came at 269 (08:08:15Z) and 2 after the 08:08:52Z
  move to 286 — so 286 is high, and 269 is near-centred with one coin-flip early; the trim (hold on
  EXC, step on 2 consecutive EARLY) is the instrument for the last 1-3 ms, provided the slider stays.
- 03:50: NO-METER DUMP CHECK DONE (D:\NexusVision\nometer_check\): nothing drawn-and-missed. ep7 =
  no gather (sprint dribble, 0/105 frames); ep35/ep50 = LATE GATHER (track +614/+617 vs ~407
  normal; fill +672/+708; locator HIT +704/+738) and the never-seen collapse fired at +653 → my
  09-16 change turned real shots into blind fires → **probe default → 0** (grace 100 back) folded
  into the range agent's AppConfig pass. No-gather sprint presses 4/53 (7.5 %) — undecidable
  without a fork input-receipt stamp. Dump hygiene: shared root collides across sessions
  (per-session subdir requested), ep20 dropped 48/88, 3 windows preroll=0. Armed 8 px floor engages
  on 34 % of presses only (anchor lock rate) → ~33 ms slower first sight (next lever for pickup).
- 04:00: owner asks about the PILL meter style as a ship bonus. Status: proven end-to-end 08-30 on
  the YOLO Arrow2 proposer + rung-tolerant reader fill; the CV contour locator that ships now has
  NO rung handling (solid-column gates) → unverified. Dispatched: find Pill footage on disk, run the
  CV locator on it, design rung-aware gates keyed on `meter_style=pill` → docs/PILL_STYLE_STATUS.md;
  if no footage, the owner records a ~5 min Pill session with the press-window dump.

## 09-17 ~04:20 local: RANGE bucket built (ships `unknown`); ANCHOR ACQUISITION is the real blocker

- Range agent: `shot_range.py` (cell samples at press+40..120 on a worker; `{"event":"shot_range",…}`),
  engine keys `type/tempo/range` for fades (`BannerLeadTrim` range dimension, migration, fallback
  range → tempo → type), `lead_offset_fade_mid_ms` 6 for `range=mid`, kill `ORION_BANNER_TRIM_RANGE=0`;
  `meter_backstop_never_seen_probe_ms` → 0 everywhere (+ tests/docs); press-window dump now
  per-session `<root>\session_<stamp>\` with the same stamp as the JSONL; preroll=0 root cause =
  ring only fed outside windows (fixed: `preroll_ondisk=`); ep20's drop = one 536 ms D: write →
  1 s back-off (guard as designed). Throwaway Native 943/0/8, Tally 49/0; Python 199/2 + sweep 936.
- CALIBRATION FAILED: only 9/32 labelled shots produced a plate reading; 3/9 agreement → `unknown`.
  Root cause upstream: `player_anchor` locks on ~2-34 % of presses on this court (PS-disc peak
  0.519 vs 0.58 gate; top-3 coarse ranking). Dispatched: anchor acquisition fix validated on both
  dumps with the 09-15 refusal study unchanged; press-window post-roll → 1700 ms (banner inside);
  banner DISTANCE text into the record; re-run the confusion matrix.
- NOT yet rebuilt in place (app running). Pill footage check still running.
- 04:40: PILL STATUS (docs/PILL_STYLE_STATUS.md): CV locator 0/642 on real Pill (3 px core vs
  col_w_min 8); packaged ONNX = the Pill net, 661/661, IoU 0.93, 0 FP; reader fill fine; style
  never reached the locator; UI pinned to Arrow2 → hazard (Pill + cv = blind). Dispatched Route A
  (native, throwaway build): style=pill → `ORION_METER_PROPOSER=yolo` + `ORION_METER_STYLE`,
  `pill_yolo_route` kill switch, `METER STYLE:`/`METER STYLE MISMATCH:` lines, combo unlocked
  with "Pill (beta)". Owner recording ask: ~5 min Pill in MyCourt with the press-window dump
  (timing on Pill is unmeasured).
- 05:00: PILL ROUTE A BUILT (throwaway): `SidecarReaderProfile.h` `applyPillYoloRoute()` — style
  pill → `ORION_METER_PROPOSER=yolo` + `ORION_METER_STYLE=pill` (beats the persisted proposer and a
  dev pin), `pill_yolo_route` (default true; env `ORION_PILL_YOLO_ROUTE=0` exact) → else `METER
  STYLE MISMATCH:` when style=Pill + proposer=cv; non-Pill env byte-identical (pinned). UI:
  `MeterConfigPanel.qml` combo `["Arrow2","Pill (beta)"]` (`meterStyleCombo`, caption
  `meterStyleCaption`; label-to-label compare so a persisted 2K26 style is not rewritten). Tests:
  RemotePlayPath 14 → 18, ui contract +1; Native 943/0/8, Preview 56, MeterDelaySettings 19.
  Pill TIMING still unmeasured → beta until the owner's 5-min Pill dump session.
- PENDING IN-PLACE REBUILD (app must be closed): sprint fence, range dimension, probe 0, dump
  subdirs, Pill route, + the anchor-acquisition agent's Python (running).

## 09-17 04:45 local: IN-PLACE BUILD 04:41 (fence + range + probe 0 + Pill route) VERIFIED

OrionNativeTests **945/0/6**, ShotVerdictTally 49, RemotePlayPath 18, Preview 56, MeterDelaySettings
19, ActivityFeedPolicy 10; ui contract + ship defaults 53. Ready to launch once the anchor
acquisition agent (Python only) lands; then sidecar bundle #3 (Python changed again).
Tonight's play after the sprint fix (08:07-08:16Z, two launches, 53 presses): Standstill 19 EXC /
1 LATE / 4 EARLY (79 %; at 286: 15/1/3, at 269: 4/0/1); Fades 14 / 8 / 5 (52 %); 6 blind (the
late-gather class, now covered by grace 100 again); 0 dead presses ≥ 400 ms; `fire_centre_delta`
median 0.11 ms, 51/51 within 1 ms. Isaiah: "standstills ~95 % perfect, fades getting there".

## 2026-09-17 10:20 PILL SESSION GRADED — the Pill fill ruler reads +6.5 pp high at the 20 % anchor (the early bias); Arrow2 restored in settings.json

**Session** 10:02:17-10:05:30Z, `meter_style: "Pill"` (route confirmed: `METER STYLE: Pill -> proposer=yolo`,
detector `ok=True provider=CUDAExecutionProvider`), press-window dump `D:\NexusVision\framedump\session_20260917_050219`
(15,658 JPEG, `frames.csv` = the reader's per-frame box/fill). Owner: "tons of earlies, majorly inconsistent … the
green window is a lot more visible on the pill meter". Slider moves during the session: 286 → 274 → 261 → 223 → 261.

| bucket | n | EXC | LATE | EARLY | green |
|---|---|---|---|---|---|
| Standstill | 36 | 17 | 4 | 11 | 53 % |
| Fades | 13 | 2 | 1 | 7 | 20 % |

At lead 261 standstills went 6 EXC / 6 EARLY / 0 LATE; at 223 they went 4 EXC / 4 LATE / 0 EARLY → the Pill ideal is
~240, i.e. the Pill path fires **~25-30 ms earlier than Arrow2 at the same slider** (Arrow2 ideal 269). Final tip
source was `phase` on 45/49 shots (promoted at the 20-27 % crossing), so this is NOT the sampler; creation-time
`predictor_sigma` 43 ms vs 14 ms is only because the Pill is first seen at 11-19 % (sampler reservation, then promoted).
Fill velocity at release (matched fill 36-40 %): Pill 0.163 pp/ms IQR 0.022 vs Arrow2 0.181 IQR 0.003 (7× noisier).
frame_age p50 25.9 vs 18.6 ms (YOLO 17-30 ms/call vs CV 7-9). STALE LOCK DROPPED AT PRESS 25/53 presses (Arrow2 144/508).
Oracle fired on 12/49 only; two 45.5 px gaps on EARLY fades = the retraction oracle does not transfer to the capsule.

**Root cause (measured, `tools/diagnostics/pill_fill_check.py --n 40 --scale 0.66667`, GT frames vs the reader in the
YOLO box):** reader ≈ 7.5 + 0.88·hand — hand 4.9→11.4, 11.8→18.1, 23.5→28.2, 35.3→37.7, 47.1→48.7, 65.7→65.2,
78.4→78.1, 93.2→91.3, 97.1→93.0. The YOLO box (~20×116 @720p) is ~16 px taller than the true fill span (~8.5 px of
pedestal below the base, ~7 px of cap above the apex); the box-relative ruler therefore reads high at low fill, low at
the top, and box-bottom jitter (±3 px) goes straight into the fill. The engine's 20 % phase anchor lands at true ~14 %
→ ~30 ms early. Arrow2's CV box is fitted to the white column, so it has no such intercept.

**Green window:** the reader sees the Pill green band at 4.46 pp (p25-p75 3.65-5.36) vs 2.75 pp on Arrow2, green_start
91.9 vs 93.4, green_end 96.4 vs 96.0 → the DRAWN green segment is ~1.7× taller on the capsule (rung-snapped), which is
what Isaiah sees. The banner does not show a wider timing window: at +19 ms from ideal the Pill went 6/12 green where
Arrow2 at +17 ms went 19/24 (confounded by the 7× fill noise). Unconfirmed either way; do not tell customers the Pill is
more forgiving.

**Actions:** `settings.json` `meter_style` → "Arrow2" + re-signed (10:15Z). Agent (Opus 5) building a landmark-anchored
Pill ruler (`pill_fill_ruler.py`: base/apex latched per lock, S≈96 so the apex lands where Arrow2's green_end lands,
one hook in `_measure_fill_in_box` for style=pill only) with offline validation on the dump + GT set + Arrow2 no-change
proof. Sidecar bundle #3 must follow. Until then Pill stays "(beta)" and needs ~25-30 ms less lead than Arrow2.

## 2026-09-19 ~01:30 PROBE AFTER ASTRA'S PASS + "OPEN SHOT CORRECTNESS" PLAN

Owner: "me and astra were basically just working on reliability and open shot correctness, if i'm open/wide open and
there's a meter the bot should time it correctly … as close as possible to a final polish". Astra's pass (Sept 17 pm →
Sept 18 22:39, no notes left) added: ShotIntentPolicy.h, GreenWindowMath.h (+tests, CMake), FadePhaseCatchup.h (fade-only
discrete-jump anchor refinement, env ORION_FADE_PHASE_CATCHUP != 0 → ON, never fired in any Sept 18 session),
PreciseFirePolicy.h (rolling lease seam in the controller), coverage-gated banner trim (only OPEN/WIDE OPEN calibrate),
ownership-acquisition census, stop-request diagnostics, reader fixes (floating white, tiny green cap, missing pixels),
shot_records/shot_range clock fixes, scripts/verify_orion.ps1, tools/timing/shot_pipeline_audit.py, ~18 tests. Release
binary rebuilt 22:39 (after the last session); build_prod_codex 17:32; sidecar bundle 17:38 is STALE (manifest --verify:
remote_play_client.py, remote_play_orchestrator.py, simple_meter_reader.py changed after it). Aim++ on the Desktop is an
unrelated project (its OrionNative.exe was the process running at probe time — must be closed before a launch here).

**Sept 18 scoreboard (281 graded releases, 13 launches):** OPEN/WIDE OPEN standstills 76/18/2 (79 %); contested
standstills 22/18/7; no-coverage-cell standstills 24/6/5; OPEN fades 11/0/1; no-coverage fades 35/14/14 (the fade drills
were 2-cell panels). Blind fires 0, hold-band clamps 4, probe 0. Open-standstill LATEs split into (a) marginal: hold
637-662, oracle gap 2-5 px, hold−onset identical to EXC (118-146 ms); (b) late pickup: first sight 41-44 % at 624-655 ms;
(c) late onset 570-628 ms, hold 700-751, gap 8-12 px. Fade-drill dump traces show NO discrete catch-up jump (max-step −
median 0.8 pp, >2.5 pp on 4-9 %). The trim stepped 0-2×/session (streak rule needs consecutive agreeing verdicts).

**Done:** settings.json drift restored to the validated ship values (never_seen probe 100→0, hold band 40/60→0/0,
aim_margin 62→69; slider left at 274) + re-signed; stray `(Join-Path` file removed (QtTest junk); court_profiles.json `{}`
matches HEAD. **Agents (Opus 5) running:** (1) engine trim v2 = per-bucket bias integrator (net LATE−EARLY votes ≥ 3 in a
12-shot window → ±3 ms) + `has_coverage` from the banner reader so a 2-cell panel calibrates (`banner_trim_absent_coverage_open`);
(2) standstill-late forensics → docs/STANDSTILL_LATES_2026-09-19.md with counterfactuals; (3) player-anchor acquisition
≥ 80 % lock + range classifier; (4) Pill landmark ruler. Then: in-place engine build, sidecar bundle #3, suites, verify.

### 2026-09-19 02:10 shot_pipeline_audit completed (4 dangling tests) + the open-shot disagreement census

Astra's `tools/timing/shot_pipeline_audit.py` (09-17 21:19) shipped with `tests/test_shot_pipeline_audit.py` (09-18 02:38)
asserting four fields the tool never emitted: `release_green_confirmed`, `release_green_window_pct`,
`first_valid_tip_evaluation`, `late_decision`, `ownership_proof`, and the count
`reported_green_open_late_epochs`. Implemented all of them (the census is exactly the owner's question: the engine
confirmed it released inside its green window, the panel said OPEN/WIDE OPEN, and the game still said LATE).
`green (92.0-96.0)` is prose on the `Release issued:` line, so it is parsed in `log_events` into a hashable
`green_window_pct` field; counts use a new `count()` helper because `identity()` rejects a legitimate 0.
**tests/test_shot_pipeline_audit.py 17/17 pass** (was 13/17). Native suite via ctest: **22/22 targets pass** — note the
test exes print nothing when run from a shell (console quirk, exit 0); use `ctest -C Release`, not the raw .exe.

**Census on session_20260918_174450 (175 records, 76 released, 53 EXC / 13 LATE / 5 EARLY):** only 4 of the 13 LATEs
were reported-green AND open. Three of those four had a completely healthy pipeline — first valid tip evaluation
24-40 ms after onset at fill 20-25 %, command ETA +74 to +104 ms of runway, frame age 22 ms, identical to the 31
matched EXCELLENTs (median after-onset 39.7 ms, ETA 93 ms) — yet the oracle gap was 7.5-9.4 px (green is ≤3). Only
ep72 was a real runway failure (ETA −25 ms, `TIP DEADLINE DECISION` lateness 25 ms, first valid 679 ms after press).
**Reading: the residual open-shot lates are an AIM bias, not a scheduling failure** — the engine hit the target it
computed and the target was ~1 frame late. That is what the bias-vote trim (agent in flight) is built to remove; a
runway fix would not touch three of these four. Other sessions: 213313 census empty (2 LATEs, neither reported-green),
192239 one (ep18). Outlier worth watching: ep46 graded EXCELLENT with `green_win=[71.5, 85.3]`, i.e. the engine's own
green-window read is occasionally garbage without changing the outcome.

### 2026-09-19 03:30 GREEN WINDOW WIDTH explains the fade gap; bias integrator SHIPS OFF

`docs/GREEN_WINDOW_WIDTH_2026-09-19.md`: at MATCHED green-window width, fades and standstills grade
identically (2.0-3.0 pp: 68 % vs 67 %; 3.0-4.5: 82 % vs 88 %) and their landing precision is identical
(|miss| ≤ 3 px on 63 % vs 64 %). The fade's window is ~40 % narrower (p50 2.17 vs 3.62 pp) and half of
all fades land in the 0-2.0 pp bucket where nothing scores well. Narrow-window misses are SYMMETRIC
(16 L / 11 E standstill), so no lead offset can fix them — only precision. The window is already
confirmed pre-fire on 68 % of shots, a median 50 ms early, and agrees with the landing width.

**Decision: `banner_trim_bias_votes` ships 0 (integrator OFF).** The agent built and tested it
(58 ShotVerdictTally + 1175 OrionNative + 22 ShmInterop + 78 pytest, all pass) but the forensics
refuted its premise: a kernel fit on the clean onset band (500-535 ms, n=202) puts the LATE/EARLY
crossing at hold 641 ms vs a population median of 646, i.e. the lead is already within ~5 ms of
optimum, and a 3/6/9 ms shift trades 2.7/5.2/7.4 LATEs for 2.6/5.6/9.1 EARLIEs (net ≈ 0; optimum
+2 ms, worth 0.1 pp). The 18:2 imbalance comes from classes a lead cannot fix (pickup stall 9 shots
at 67 % LATE, late-drawn meters 13 at 31 %). Left on, the loop would spend its whole ±15 ms clamp and
turn EXCELLENTs into EARLIEs. Code + tests stay; `ORION_BANNER_TRIM_BIAS_VOTES=3` re-arms it.
**KEPT ON:** the `has_coverage` fix — a 2-cell TIMING|DISTANCE panel (98 of 281 verdicts) now
calibrates the streak loop again instead of being COVERAGE_EXCLUDED.

### 2026-09-19 05:00 SQUARE PATH ROOT-CAUSED + XBOX COSTED (agent hand-back)

`docs/SQUARE_PATH_AUDIT_2026-09-19.md` (582 presses, 26 h, 12 sessions) and
`docs/XBOX_PATH_DESIGN_2026-09-19.md`. New tests: `tests/test_square_path_press_census.py`
(29 passed, 3 xfailed — the xfails pin the unfixed bugs) and `tests/test_xbox_path_hazards.py`.
Fixture `.log` files were being swallowed by the global `*.log` gitignore → renamed `.logtxt`.

**STUCK SQUARE SOLVED — the merged press.** `ShotIntentPolicy.h::updateSquareGesture` retires the
latch early only via `deliveredSquareReleasePending_`, which ONLY an owned delivered release sets, so
an UNOWNED press can never spend its 2-poll rebound run against the 3-poll debounce: the re-press
mints no epoch and the console is told Square-down continuously. Proven on epoch 187 (log.1 03:54:13Z,
297 ms across two presses). Patch drafted (needs an `unownedSquareLatchRetirable_` bit fed from
`shot_.state != HoldState::Idle` at OrionAppController.cpp ~L14081); **held until the pickup and
controller agents land** to avoid a collision, then applied as one batch.
**The 09-16 R2 theory is REFUTED**: presses with an R2 release edge were owned 63.2 % vs 49.1 %
baseline; `sprint_released=1` on 0 of 582. Memory rewritten.
**The 48 % "unanswered" is an artifact**: 254 of 281 were taps < 300 ms. Honest unanswered-shot rate
= 22/582 = **3.8 %**, 19 of which had `PICKUP first_sight_fill=-1` (reader proposed nothing).

**Also found: `backstop=` is lying.** `meterBlindBackstopBlockReason` (AutomationEngine.cpp:20937)
never tests `config_.greenWindowPriority`, which is what actually short-circuits the backstop at
:21401 — so 280 of 281 abort lines report `backstop=user_released` including 1361 ms and 1039 ms
holds whose own `METER VISION WAIT … blind_release_suppressed=1` proves otherwise. Zero-risk token
fix, queued. Related: `meter_blind_backstop` fired **0 times in 582 presses** (26 suppressions
instead) — `green_window_priority` and it contradict each other and three grace/probe knobs are
tuning dead code. Recommendation is to retire it honestly rather than re-arm it (blind releases
graded 3 EXC / 3 EARLY vs vision 15 EXC / 8 EARLY / 0 LATE) — **owner decision, not an audit call.**

**XBOX:** ride Microsoft's app (WGC capture + ViGEm X360 + HidHide); transport problem gone, the ship
gate is TIMING. Measured Elgato baseline from these logs: **213.7 ms median (206-244, n=277)**, 60.0 fps
/ 16.667 ms cadence, frame-phase sd **0.05 ms**. Remote Play inserts an encoder, a network hop and a
jitter buffer that RE-TIMES frames, plus a compositor hop — never measured here. Phases: (1) Xbox
*controller* on the PS5 rig, 3-5 days, low risk — the internal mask is already XInput
(`square() == XINPUT_GAMEPAD_X`); (2) timing bench ~1 week = THE GATE, day one is the black-frame
check (a DRM-flagged window returns black under WGC and kills the route), and it can measure the
ViGEm route on the EXISTING PS5 rig for free; (3) Xbox mode 3-5 weeks, only genuinely new C++ is
HidHide (port virtual_controller.py:297-373). `remotePlayConsole = "Xbox"` today is a STUB — do not
budget it as partial work. Do NOT copy the Aim++ prototype's `PrintWindow`/`BitBlt` capture.

## 2026-09-19 LAUNCH PREP (storefront, payments, Discord, signing) — coordinator log

Owner brief: "getting everything set up for launch so that all i have to do is pack and make an installer",
then "you have my authorization for everything". Payment path = **Stripe** (Gumroad legacy). **$25/month
flat, NO activation fee.** Free 3-day trial, **no card**, claimable on the website (and `/claim_trial`).
Hard gate: **no purchase and no trial unless signed in with Discord on the site AND a member of the guild**,
checked LIVE at click time and failing CLOSED.

### Done and verified live
- **License server (`orion-activate`) redeployed** — `CodeSha256 cvjfh4XiLKayfLQkLAFz2X7x2IeuR6TMOaM5enQdODM=`,
  5,035,388 B, python3.12 x86_64, now vendoring `cryptography` (+cffi/pycparser, manylinux cp312). Cold start
  clean, no import error. Rollback zip: `D:\NexusVision\signing\orion-activate-ROLLBACK.zip` (52,564 B).
- **Lease keypair ROTATED** (the 07-17 private half does not exist anywhere — 358,907 files searched).
  Private in SSM `/orion/lease_signing_key`; public pinned in `LeaseGate.cpp` =
  `KKBzDoL7+FrwNmBg86z+Vk84oEdRQFLqfD1ahFb/XhY=`. **Fixes the launch-breaker**: production clients stop
  firing exactly 900 s after unlock without a server-signed lease. Any prod build made before this is dead.
- **API Gateway** (`v348t5hg3i`, EXPLICIT routes): created + attached `POST /api/bot/guild-join` and
  `POST /api/bot/guild-member` to integration `gpamg89` (orion-activate). Verified live (403 forbidden,
  edge-auth enforced). Auto-deploy: no manual deploy needed.
- **Stripe**: customer portal configured + link ACTIVATED
  (`https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00`, cancel at period end, no plan switching,
  redirect to /discord); **webhook signing secret ROLLED** (old expired immediately; it had been exposed).
  Confirmed: account Verified, live price `Venice Monthly $25/mo`, webhook on API `2026-08-26.dahlia` with
  the exact 5 events the code handles, **no Payment Links** (the site is the only purchase surface).
- **Triton `CREATE_INSTANT_INVITE`** granted by the owner; verified true via the bot API (auto-join unblocked).

### Built, tested, NOT yet deployed
- **Storefront v1 redesign** (Lighthouse mobile 97/100/100/100, desktop 100s, CLS 0, −12 % transferred
  bytes) + **v2 in progress** per owner feedback ("less ai, straight forward, more polished", reference
  screenshot) and "show the connected Discord on the site".
- **`POST /api/trial`** (site trial via the backend's `/api/bot/trial`), **live membership gate** on trial +
  checkout (fail closed), **guilds.join auto-join** at login, `avatarUrl` on `/api/checkout/config`
  (built server-side; the page never gets the raw Discord id). Site suite **38/38**, backend **324**.
- **Checkout fixes**: `automatic_tax` REMOVED (Stripe Tax is not active — re-enabling it without activating
  Tax breaks every live checkout), `payment_method_types=card` (async methods complete as `unpaid` and would
  charge without provisioning), `Stripe-Version: 2026-08-26.dahlia` pinned, and **B3: renewals now check
  `status === "paid"`** because Stripe removed `invoice.paid` in basil — every day-30 renewal would have failed.
- **Legal pages corrected** to Stripe / no-key / portal-cancellation. Flagged for a lawyer: Terms carry NO
  trial clause, and "generally final" vs "all sales are final" disagree across three surfaces.
- **Discord tidy script** ready at `D:\NexusVision\discord_changes\tidy.py --execute` (owner runs it; the
  harness blocked the agent's own execution and it was NOT laundered through the coordinator). 12 steps incl.
  delete Co-Owner + Support (both zero-member), Staff perms `8796361457680` → `1099511635970`, voice sync,
  7 new channels, read-only pass over 14 channels. **Correction that mattered:** denying `@everyone` alone is
  a no-op — Customer/Trial overwrites are what grant Send on #downloads/#setup-guide/#changelog.

### Owner-only, still outstanding
IAM inline policy on `orion-activate-lambda-role` (config GetItem/PutItem, ratelimit UpdateItem, audit
Query/Scan — rate limits fail OPEN and `owner_totp_required` defaults false without it); the two
`wrangler secret put` commands (live `sk_live` + the NEW `whsec`); unpublish the Gumroad product; run the
Discord command; drag **Bots** below Staff (a leaked bot token can currently grant Administrator);
`artifact_url` in `update_manifest.json`; off-box backup of the signing key.

### SECURITY — update-signing key exposed twice (owner decision)
The UPDATE/release-manifest private key (public `OJQ2E7ZA…`) exists **unencrypted** at
`Desktop\EXPLOITS\helios_bypass\codesigning\venice_update_signing.pem` and in cleartext inside a
2026-09-14 agent transcript JSONL. Recommend rotating it pre-launch (cheap now, expensive once clients
pin it), and **do not delete the plaintext copy before an off-box backup** — the other copy is
EFS-encrypted and dies with the Windows account. `/orion/manifest_signing_key` is an unused orphan;
the real chain (VeniceSigning PEM → `/orion/ed25519_private_key` → client pin) is consistent, so the
09-17 "manifest key mismatch" blocker was a FALSE ALARM (the local PEM was simply absent).

### 2026-09-19 website v3 DEPLOYED (Fable 5.1 pass: no slogan, 3 sections)
`venice-site-production` version **654a8867-a0cc-46ed-9663-70b09006f3fd** (replaces the v2 that Astra
deployed as 6a0247b4…; wrangler's list showed 9a138b69… from 09-16 as the prior 100 % entry). Front end
only — `src/worker.js`, `wrangler.jsonc`, legal pages untouched. Stylesheet is now INLINED in index.html
(verifier `inline_css_in_sync` keeps it equal to `public/styles.css`, which the other pages still link);
Geist font re-instanced to a 400-700 axis under the same filename. Local Lighthouse 100/100/100/100 both
form factors, 35,675 B transferred (−27 % vs v2). **Rollback:** `D:\NexusVision\site_backup_2026-09-19\v2_public\`
(copy over `public/`, `npm run check`, `npm run deploy:production`) or `npx wrangler rollback --env production`.
