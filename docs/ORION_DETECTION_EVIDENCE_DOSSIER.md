# Orion — Detection Stability Evidence Dossier + Concrete Plan

_Produced 2026-07-21 in response to `docs/ORION_DETECTION_INVESTIGATION_PROMPT.md`. A 7-agent read-only fan-out over `NexusVision` (reader / orchestrator / `AutomationEngine.cpp` / QML / launcher / logs / replay harness), followed by a lead adversarial-verify pass that re-read every load-bearing `file:line` against source. Evidence standard: **every root cause = exact `file:line` + a real log excerpt + an offline reproduction or an honest UNPROVEN label with the capture that would settle it.** Nothing applied — this is find-and-prove. All proposed fixes are flag-gated, default-OFF, byte-identical when off._

Repo: `C:\Users\aaron\Desktop\NexusVision` · branch `feat/detection-template-anchor-qml-render` · live evidence from session `2026-07-22T02:2x–02:32Z` (= 07-21 21:2x local) in `logs/orion_native.log`.

---

## 0. Executive summary — the one picture

Three of the five defects are **one mechanism wearing three masks**, plus a downstream symptom, plus a render red-herring:

1. **A single shared enabler drives D1 and D2.** The **armed-hold conf-floor** (`simple_meter_reader.py:2899-2900`) pins `conf ≥ CONF_MIN (0.95)` for up to `_armed_coast_max = 180` frames (~3 s) whenever the shot-gate is armed. That **prevents the lock-drop at `:2901`**, so when the meter leaves the search band the reader keeps serving the last box as stale `meter_memory` (`:2998-3001`) instead of dropping to `nodet` and cold-re-acquiring. The `STALEBREAK` fake-lock breaker that would kill this is **force-exempted for the entire armed window** (`:3298`, counter frozen at `:3306`, fire-gate needs `not _grace_active` at `:3320`). **In-shot** that is **D1** (box disappears mid-shot); **after the shot** it is **D2** (box coasts on a frozen peak). Both were reproduced offline.

2. **D3 is a separate, session-permanent poison.** The `_track_h_hist` seed gate (`:2373`, `cand >= 0.90*max(hist[-8:])`) is one-way: one tall outlier pins `max()` forever, so every true shorter full-track height is refused and `fillable_h` (the fill denominator **and** box height) stays inflated → the meter under-reads until process restart. The code's own comment admits this. `SCALE_RESET` (default-ON) does not cover it (keeps the poison, wipes the recovery accumulator on every lock-drop).

3. **D4 is not an engine bug — it is D1/D2/D3 seen from the trigger.** Feedforward is evaluated first and hard-returns (`AutomationEngine.cpp:3042`); vision only fires if feedforward declines. **79–81 % of all blind fires coincide with same-shot detection starvation.** The decisive proof: seq=44 graded `verdict=LATE +66 ms` while the reader reported 56 % fill — the "overfire" is the reader **under-reading**, not the trigger firing early. The real fix (`LIVE_TIP_FIRE`) is **confirmed unbuilt** (0 matches in the engine); `learning.json` shows the live drift (`global_appear_to_tip_ms=250.5` used where Standstill needs `412.88`).

4. **D5 is mostly fine.** The emitted box already frames the **full track** (`:2414/:2428`), not the green window, and render is **not** the lag source (box + preview image are frame-ID-joined via `MeterBoxRing`, `OrionAppController.cpp:5731/5779`). The only real risk is the rig-forced `BOX_PREDICT` (up to 40 px stale overshoot on decelerating fades, never framedump-A/B'd, can compound with the render-side ≤60 px extrapolation).

5. **The rig runs a half-inert config.** `ORION_METER_LOCATOR=1` (YOLO/torch) is **not wired to the SimpleMeterReader** (the orchestrator builds `SimpleMeterReader` at `:606-643`; the locator's sole caller lives inside the never-constructed `MeterDetector.__init__`, `meter_detector.py:2947`); `ORION_METER_TRACK=1` (Kalman) is likewise inert. Torch **never loads** on the shipped path (0 ms, absent from `.venv`). Three default-OFF-in-code flags are **forced ON on the rig with no A/B backing them as defaults**: `BOX_PREDICT`, `ANCHOR`, `PCTL_FILL`.

**Bottom line:** fix the shared armed-hold absence-handling (D1/D2) and the `_track_h_hist` ratchet (D3), and D4's blind fires collapse on their own. D5 needs one revert. Track L is three cheap, measured launcher wins. **D1/D2/D3 reproduce offline right now** on existing Jul-17/06 framedumps; D4/Track-L timing needs the native suite made to emit its totals.

---

## 1. Cross-cutting findings (verified, multi-agent)

| Finding | Evidence (file:line) | Corroborated by |
|---|---|---|
| Armed-hold conf-floor is the shared D1+D2 enabler | `smr.py:2899-2900` (floor) → `:2901` (drop skipped) → `:2998` (stale serve) | D1 + D2 agents independently; lead re-read |
| STALEBREAK breaker is force-disabled while armed | `smr.py:3298` (`_grace_active=True`), `:3306` (freeze), `:3320` (needs `not _grace_active`) | D1 + D2; lead re-read |
| YOLO locator not wired to the shipped reader (dead weight) | orch `:606-643` builds `SimpleMeterReader`; locator sole caller `meter_detector.py:2947` inside never-constructed `MeterDetector`; torch absent from `.venv` | D3 + D5 + Track-L |
| MeterBoxKalman inert under SimpleMeterReader | `meter_box_kalman.py` loaded only inside `MeterDetector`; orch instantiates reader mutually-exclusively | D5 |
| Torch is 0 ms on startup (brief's "biggest chunk" guess is wrong) | boot log `detect_ms≈1 ms`, no locator-loaded line; torch/ultralytics absent from `.venv` | Track-L (4 independent lines) |
| "Overfire" is the reader under-reading, not early firing | seq=44 `verdict=LATE +66 ms` at 56 % read; 79–81 % blindFire↔starvation | D4 |
| D1/D2/D3 reproduce offline on existing captures | replay of `session_20260717_192510` / `_231912` / `session_20260706_190737` | D1 + D2 + Infra (all ran it) |
| Brief mis-cites: native gate count is **194**, not 216 | `docs/REGRESSION_GATES.md:8` (216 is a latency-IQR figure) | Infra |
| Brief correction: `fill_kalman.py` is pure numpy, no torch | `fill_kalman.py:10` | Track-L |

---

## 2. Per-defect dossiers

### D1 — Detection disappears mid-shot  *(SHIP-BLOCKER)*
**VERDICT: ROOT CAUSE FOUND** (mechanism proven + reproduced offline). Sub-case A-vs-B for the exact seq=42 frames: **PARTIAL** (frames were never dumped).

- **Symptom:** the box vanishes mid-shot / while holding Square; the whole shot is served from stale memory.
- **Evidence (repro):** real log `orion_native.log` seq=42: `samples=33 fresh=0 staleMem=33 confLow=0 staleFrame=0 nodet=0 firstFreshMs=-1`, `Release issued: … Released on learned 549ms hold-start clock (meter not visible) code=feedforward_target blindFire=1`. Offline: `replay_simple_reader.py --session session_20260717_192510` → `frozen_fill_max=150` (a 150-frame `meter_memory` dead-hold), 20 frozen runs ≥12 f.
- **Root cause:** armed-hold conf-floor `smr.py:2899-2900` pins `conf≥CONF_MIN` → the `no_meter` drop at `:2901` never runs → stale `meter_memory` served at `:2998-3001`. The `STALEBREAK` breaker that would break it is armed-exempted (`:3298/:3306/:3320`).
- **Sub-cause isolation (instrumented replay, mask-pixel logging on `_scan`/`_find_green_tip`):**
  - **reject-after-acquire — EXCLUDED:** every stale frame has an **empty** red mask (`redpx220=0 AND redpx170=0`, `greenpx=0`) → no contour for any gate to reject (`confLow=0` corroborates).
  - **window-slide — EXCLUDED as sustaining cause:** the coast-steal `SCAN[BAND]` (window-independent, full 1810×520 band, `:2719-2722`) also reads `redpx220=0` on the same frames. `staleMem=33, fresh=0, nodet=0` ⇒ the full-band steal reseated on none of the 33 frames.
  - **acquire-fail / meter-absent-from-band — CONFIRMED dominant:** what `nodet=0 / staleFrame=0 / confLow=0` leaves. `nodet=0` proves the armed-hold never let conf fall to the drop, so the honest cold-acquire path (`:2812-2872`) never runs; the meter presents no acquirable colour in the band and armed-hold masks that as `detected:True(stale)`.
  - Instrumented trace (stationary dead-hold, box frozen x=1017): seq 1328 `SCAN[win] HIT redpx220=1684` → fill 47.06 fresh; seq 1329+ `redpx=0` window **and** band → 57 `meter_memory` frames, conf floored 0.95. Near-cap run (x=677): seq 4453 HIT fill 88.25 → 17 `meter_memory` frames → seq 4471 meter REAPPEARS `redpx220=2360`. The lock sat on the real meter the frame before each gap (not décor), then the pixels vanished.
- **Complementary candidate for the "dived-below-band" flavor (lead observation):** `VZOOM_DOWN` (the arm-dive band-widen, default-ON `:1200`) **self-disables when `self.box is None`** (`_band_down_widen:1609`) and reverts to the nominal band once the ref ages (`_band_eff:1589`). So on a fast dive that drops the lock, the band snaps back up (y1≈770) above the dived meter floor (≈891, master-plan frame-scan of `session_20260717_231912`) — a mechanism for `redpx=0 in band`. This is one flavor of "meter absent from the scanned region," to be A/B-settled against the capture below.
- **Native census that produces the counts:** `AutomationEngine.cpp` genuineFresh `:1430`, fresh++ `:1464`, staleMem++ (reason=="stale_or_memory") `:1473-1474`, nodet `:1477`; emit `OrionAppController.cpp:2134-2149`. Note `firstMeterMs=-1` is a **derivative** of `fresh=0` (`:2289-2292`), not independent proof the meter was on-screen.
- **Existing flags:** `ARMED_HOLD` (default-ON, `:1043`) = the enabler — **keep** (it is the no-blind-fire guarantee) but it needs an absence concession; `ARMED_COAST=180` (`:1044`, ~3 s) far too long for a true absence; `STALEBREAK` (default-ON) reseats on reappearance but its steal is **strict red R≥220 only** (misses desaturated/green meters) and it can't break an in-shot dead-hold; `SHOT_COAST/RISE_PROBATION/FAKELOCK_BREAK` are commented-OFF (regressed the gates) — leave off.
- **Proposed fix (flag-gated, default-OFF):**
  1. **`ORION_READER_STEAL_RELAXED`** (primary, additive): extend the coast-steal (`:2719-2722`) to also try `R≥170` red **and** a full-band green-tip when the strict-red scan misses (mirror `_relocate` tiers 2–3, band-wide). Fires only after strict-red misses ⇒ pristine path byte-identical. Metric: on the present-but-desaturated subset, `staleMem→fresh`, `firstFreshMs -1→≥0`.
  2. **`ORION_READER_ABSENT_CONCEDE`** (secondary, honesty): while armed, if the full-band scan reads `redpx==0 && greenpx==0` for ≥K frames (K≈20, > a real occlusion), stop flooring conf → lock drops to `nodet`, cold-acquire runs. Gate on 0 band pixels so a partial occlusion (nonzero edge px) never trips it.
- **Verification plan:** instrument the harness to log per-frame `redpx220/redpx170/greenpx` over **both** `_relocate_window()` and `_band_eff()`; A/B on `session_20260717_192510` (`frozen_fill_max`, `within_shot_blinks`); `run_gates.py --no-timing` must hold **46/46**. Then the fresh capture (below) to settle A-vs-B on a real disappearing Standstill.
- **Residual risk:** cannot prove A-vs-B for seq=42 without its frames. If the meter is truly absent, conceding armed-hold only re-attributes `staleMem→nodet` (honest) — it does not add fresh reads or change the blind fire; **fix (1) is the only one that adds fresh reads, and only for the present-but-missed subset.** The 1002 zero-fresh shots across the log span months/shot-types — not guaranteed one uniform sub-mechanism.
- **CONFIDENCE:** High on the mechanism; Medium on seq=42's specific A-vs-B.

### D2 — Box stalls / coasts after the shot
**VERDICT: PARTIAL** — symptom root-caused + reproduced offline; the brief's *specific* self-arm loop is **REFUTED** on the shipped rig; the real driver is adjacent and proven.

- **Symptom:** the box stays painted, frozen, coasting on stale memory after the shot ends.
- **Evidence (repro):** log seq=38 `samples=91 fresh=14 staleMem=76`, seq=40 `staleMem=73`. Offline replay of `session_20260706_190737` (rig config): **715 `meter_memory` coast frames, all `armed=1`**, rise-state `{spent:281, peak:262, '':172}`, **0 rising**, velocity never >40; longest frozen run **85 frames** at fill 92.67.
- **Brief's loop — REFUTED:** the "stale velocity re-derives rising → self-arms forever" cycle is **broken by STALEBREAK** (`:2989-2997` zeros rise-state AND advertised velocity on stale coast frames). A/B: `STALEBREAK=0` → coast-rising frames `0→25`, `frozen_fill_max 85→111`, coast `715→884`. So the loop is real in principle but already neutralized on the rig.
- **Real root cause:** armed-hold floor `:2899-2900` pins `detected=True`; the shot-gate stays continuously armed off **real recurring shots** (log SHOT-GATE `ARMED seq=10524 → DISARMED 10975` = **451 contiguous armed frames**), refreshed via `remote_play_orchestrator.py:2217-2227`; the unlocked cold-acquire only runs when `box is None`, which the floor prevents; the only re-acquire that runs is the in-band coast-steal (`:2719`), **exempt from rise-probation while armed** (`:2750-2751`) → re-seats on frozen red without proving a rise → box coasts on the frozen **peak/spent** fill.
- **`FAKELOCK_BREAK` — proven inert:** appears only OR'd with `_stalebreak` (`:3307, :3320`), never standalone; with STALEBREAK on, enabling it changes nothing, and the breaker is frozen while armed anyway (`:3298`). The brief's suggested A/B is a dead end.
- **Proposed fix (flag-gated, default-OFF): `ORION_READER_SPENT_DROP`** — do **not** force `_grace_active` (do not freeze the breaker counter) when the held coast is `'spent'` (post-peak recession: `peak≥70 and fill < peak-8`, `:2521`); drop the lock even while armed if `rise_state=='spent'` → routes to a clean cold re-acquire. Metric: `staleMem` −~40 % direct (`spent=281/715`), more once the earlier drop prevents trailing peak/'' holds.
- **Verification plan:** A/B on `session_20260706_190737` (`frozen_fill_max`, coast-count); `run_gates --no-timing` 46/46; check `within_shot_blinks` unchanged (must not re-break D1).
- **Residual risk:** must **not** drop `'peak'` holds (a legit green make-window is a peak hold, `fill≥85 |vel|≤25`); restrict to `'spent'` only + a `green_seen` exemption ⇒ near-zero D1 risk. Lever is the armed-grace freeze at `:3298`, **not** the (inert) FAKELOCK_BREAK flag.
- **CONFIDENCE:** Med-High (High on mechanism + FAKELOCK inertness + offline A/B; Medium that the 07-22 shot is the same peak-coast shape — it wasn't frame-dumped).

### D3 — Session degradation (adaptive-state poisoning)
**VERDICT: ROOT CAUSE FOUND** (one-way ratchet, confirmed by code + the code's own comment). "Fired on this exact 07-22 shot": **Medium** (inferred from the under-read signature; internals were never logged).

- **Symptom:** first shot(s) fire perfectly to the tip, then accuracy decays until restart.
- **Evidence (repro):** same-session decay — seq21-24 `fresh~14/staleMem~11` → seq26-29 `staleMem 81/102/83/83` → seq41 `staleMem=0` → seq42 `fresh=0` → seq43 `staleMem=0`. `minFreshFill` collapses 15-18 → 49.1/16.9 (the under-read signature).
- **Root cause (Ratchet 1, the session-permanent trap):** `_track_h_hist` seed gate `smr.py:2373` `seed = full_cap and cand >= 0.90*max(hist[-8:])`; appends only on seed (`:2404`), **no mid-session reset** (only init `:1380`). A tall outlier enters trivially and becomes the new `max`; every true shorter full-cap frame is refused forever; `fillable_h = median(hist[-5:])` (`:2408`) — the fill denominator **and** box height (`track_top = ph - fillable_h`) — stays inflated → under-reads for the session; only a process restart clears it. **The code comment at `:2374-2389` states this defect verbatim.**
- **Why `SCALE_RESET` (default-ON, `:1156`) doesn't save it:** it enables a two-sided recovery (`:2391-2401`) that needs M=4 consecutive consistent refused frames with `cand ≥ 0.50*max(hist[-8:])` — but if the outlier is >2× true height, true frames fall **below** the 0.50 floor and are excluded → un-recoverable in-session; and the lock-drop/hard-break handlers **wipe `_low_cand_hist` (the recovery accumulator) while keeping the poison** (`:2924/:3357`). So every shot-end resets recovery progress but preserves the poison.
- **Ratchet 2 (`_scale_est`/`_size_base`):** `_size_base` frozen from the first 5 locks (`:1503-1507`), never reset; `_scale_est` EMA (`:1513-1514`) opens the size gates once `|est-1|>0.10` (`:1517-1519`). Unlike Ratchet 1 it **does** have a 5 s decay-to-1.0 path (`:2673`) gated by `SCALE_GUARD` (default-ON) to armed/rising locks — so it is a real but partially self-healing contributor, not strictly one-way.
- **Locator gives no protection:** the reader has zero `_locator` refs; it segments the full frame itself (`remote_play_orchestrator.py:2203`), so `ORION_METER_LOCATOR=1` neither constrains the reader nor blocks décor from feeding `_track_h_hist`.
- **Proposed fix (flag-gated, default-OFF): `ORION_READER_TRACK_H_ROBUST_GATE`** — in the refusal test (`:2373`) replace bare `max(hist[-8:])` with a robust ceiling `min(max(hist[-8:]), p*median(hist[-8:]))` (p≈1.15). Relaxes **only the shorter-frame refusal** (the recovery direction); **never** admits taller/wider frames. Optionally stop wiping `_low_cand_hist` on lock-drop. Metric: after an outlier, `fillable_h`/`max(_track_h_hist)` return to true within a few frames; `staleMem 80-102 → ~11`; `minFreshFill` recovers.
- **Verification plan:** patch the harness to log `max(_track_h_hist[-8:])`, `median(_track_h_hist[-5:])`, `_scale_est`, `_size_base` per frame (instance attrs, **no reader edit**); replay `session_20260717_231912`/`_192510` to surface the ratchet; A/B the fix; `run_gates --no-timing` 46/46 (watch `mid_rise` glitch — the reason a replace-style rebase was rejected before).
- **Residual risk:** any variant that raises the accepted **height ceiling** or widens size/aspect gates admits more décor → worse. Keep the flatness/`0.50*max` plausibility guards (they block a descending deflate-tail from dragging the median down → false-100 peaks). A/B before any default flip.
- **CONFIDENCE:** High on the one-way code path; Medium that seq26-29 specifically was this ratchet (no internals logged for that session).

### D4 — Overfire / blind feedforward
**VERDICT: ROOT CAUSE FOUND — downstream of D1/D2/D3, quantified.** No independent engine bug found.

- **Symptom:** the bot fires with no real timing, way early (e.g. 56 % fill vs a ~99.6 % target).
- **Release-path selection:** feedforward is evaluated first and **hard-returns** (`AutomationEngine.cpp:3042-3050`); vision (`green_confirmed:3350`, `predictive_target:3373`) only runs if feedforward declines. `ffFire` `:2888-2896`; tip-gate hands off to vision only when vision is **stably healthy** (`≥3 fresh accepts in 150 ms + velocity>0.02 + fill<target`, `:2928-2931`); `ffEligible` `:2944-2947` (fast types = Standstill/Fade always eligible). A starved reader can't supply 3 fresh accepts → control falls through to the feedforward preempt.
- **Quantified causal chain (full 41 MB log, per-shot join):** feedforward = **68 %** of 4216 releases. Of feedforward fires (n=2907), **79.1 % STARVED** (fresh=0 **or** peakFill never reached green). Of blindFire=1 (n=1945), **80.8 % starved, 100 % memTrusted=0**. 1002 zero-fresh shots of 2538 (**39.5 % of all shots saw zero fresh reads**). feedforward `fillAtRel` median = **50 %**.
- **The seq=42→43/44 chain:** seq=42 disappear (`fresh=0`) → seq=43 `fillAtRel=56.4 peakFill=56.4` `verdict=EARLY -90 ms`; seq=44 `fillAtRel=56.2 peakFill=81.4` (never reached green_lo 86.8) **`verdict=LATE +66 ms`**. The LATE verdict at a 56 % read is decisive: **the overfire is the reader under-reading, not the trigger firing early.**
- **The fix is unbuilt:** `ORION_LIVE_TIP_FIRE` (invert fire authority to the live-rise crossing) appears **only in docs, 0 matches in the engine**. `learning.json` shows the live drift: `global_appear_to_tip_ms=250.5` used where `shot_type_appear_to_tip_ms.Standstill=412.88`.
- **`blindFireSuppress` — OFF and wouldn't help:** default `false` (`AppConfig.h:200`); its predicate targets **zero-detection** frames (`detectionPresence=="rejected"`), but seq=42/43/44 are `presence=accepted conf=1.00` under-reads. The absolute hard cap (`:3611-3628`) guarantees release regardless, so suppression never violates guaranteed-release.
- **No independent overfire:** of 229 feedforward fires with `peakFill≥green_lo`, 61 fired in-green (a code-label race) and the other 168 have a median `peakFill−fillAtRel` gap of 47 pp = reader **bouncing** (D1/D2/D3), not stable healthy vision.
- **Proposed fix:** **no engine change for D4 itself** — fix D1/D2/D3 and feedforward correctly recedes to the invisible-meter safety net it is designed to be. Optional, default-OFF, hard-cap-backstopped: `blindFireUnderReadSuppressEnabled` (suppress when `lastSampleFreshAccept && fill+ffMaxRise ≪ target && peak never neared green`, hold for a fresh frame or the hard cap). The larger `LIVE_TIP_FIRE` (2a) project depends on D1 detection health and is Wave 2.
- **Verification plan:** cannot be replayed offline (native fire path) — needs the `OrionNativeTests` timing gates (currently not emitting totals here — see §3) or a live batch with release logging. A/B guardrail: `blindFire=0 && samples>0` is the win.
- **Residual risk:** any suppression may only ever *soft-skip* (clear `ffFire`); it must never gate the absolute hard cap — that would reintroduce a "won't release" hang.
- **CONFIDENCE:** High (79-81 % correlation quantified + every branch Read + the LATE-at-56 % proof).

### D5 — Box geometry (reader emit vs render)
**VERDICT: READER-SIDE FOUND (sizing correct; BOX_PREDICT overshoot px UNPROVEN) · RENDER-SIDE FOUND (not the lag source).**

- **Box spans the full track, not the green window:** `cap_top/cap_bot` (green make-window) at `smr.py:2335` feed a **separate** `green` field; the box top is `track_top = max(0, ph - fillable_h)` (`:2414`), emitted as `(x0, top_search+track_top, box_w, fillable_h)` (`:2428`). The old vertical-elongation defect is closed by `TIP_TIGHT` (default-ON `:1041`, `_apply_tip_lift:1624`).
- **BOX_PREDICT (rig-forced ON, default-OFF in code `:1227`):** deadband-when-still is **proven safe** (byte-frozen under |v|<2 px). Overshoot-on-fade is a **proven mechanism** — `bvx` is the last measured velocity held frozen through the hold; a fast-sliding meter that then stops/fades keeps getting shoved, bounded by decay to `bvx·9` clamped to **40 px** stale lead. Never framedump-A/B'd ("unit-proven" only, `run_orion.local.ps1:131`).
- **Render is NOT the lag:** the box and preview image update in **one** C++ call keyed to the same decoder frame — `OrionAppController.cpp handleRemoteFrame:5731` sets `frameSerial_:5743` (image token) and `meterBoxRing_.lookup(frameNumber):5779-5796` (box for THIS frame). `MeterBoxRing.h` explicitly exists to paint the correct box onto a just-decoded older preview image; on a join-miss it velocity-extrapolates, bounded `kJoinWindow=6`, `kMaxExtrapPx=60`. Any residual box-vs-meter error is reader-side.
- **`ORION_METER_TRACK` (Kalman) is inert** under SimpleMeterReader (loads only inside the retired `MeterDetector`). Rig flag is a no-op.
- **Proposed fix (flag-gated, default-OFF): set `ORION_READER_BOX_PREDICT=0`** on the rig (return to code default) until a framedump A/B is run; add a native clamp so BOX_PREDICT (≤40 px) and `MeterBoxRing` extrapolation (≤60 px) can't **compound** (combined ≤100 px box lead on a sparse-detection fast fade).
- **Verification plan:** fresh `-Framedump` A/B `BP=0` vs `BP=1` over the same fade/slide shot; overlay the emitted rect on meter pixels (the dump's `_overlay.png` already paints `result.bbox` on the exact frame — `remote_play_orchestrator.py:2077-2093`) and measure top/bottom/left/right px error + stale-lead on decel frames. Render-lag must be validated at the C++/screen layer (the Python dump paints on the same frame by construction and can't expose render frame-lag).
- **Residual risk:** the two forward-extrapolation layers compounding on fast fades; reverting BP removes one layer. Existing framedumps pre-date BP + the TIP_TIGHT flip (both 07-19), so they can't show BP overshoot — a fresh capture is required to quantify px.
- **CONFIDENCE:** reader-side High (full-track sizing); render-side High (frame-join wired); Med/Low on BOX_PREDICT px magnitude (no A/B exists).

### Track L — Launcher startup
**VERDICT: CRITICAL PATH IDENTIFIED WITH NUMBERS** (measured from the 2026-07-21 21:26 boot). The brief's "torch/.pt load = biggest chunk" hypothesis is **wrong** — it is 0 ms.

- **Measured critical path (double-click → first live preview ≈ 18–19 s):**
  1. **Native auto-update check gap — ~8.0 s** this boot (`02:26:32.875 controller armed` → `02:26:40.854 auto-update skipped`), variable (~0.6 s on the 18:26 boot). Blocking network call even on dev builds. *Attribution inferred from adjacent log lines — OrionNative C++ not readable here; needs a native timer to confirm.*
  2. **Sidecar import + capture open — ~4.09 s** (`config parsed` → `meter detection active`).
  3. **PS1 `Start-Sleep 4 s`** (`run_orion.local.ps1:193`) — unconditional; pure waste on a cold first launch (nothing was reaped).
  4. Capture fps warm 10.4→60 — ~5 s (Elgato HW; **overlapped** with the pre-connect preview phase → hidden from connect-to-play).
  5. Native config/unlock/arm ~1.05 s; venv spawn ~0.37 s (not a lever); **torch import + `.pt` load = 0 ms** (never executed on the shipped path — proven by 4 independent lines).
- **Proposed speedups (each low-risk, measured):**
  1. **Skip/async the dev-build update check** (`ORION_SKIP_UPDATE_CHECK=1`, native) → up to ~8000 ms saved cold. Dev builds never auto-update.
  2. **Make `Start-Sleep 4 s` conditional** on the reap actually killing a prior holder (`if ($killed) { Start-Sleep 4 }`), else 0 → ~4000 ms saved on cold launch.
  3. **Open the capture card in parallel with the update check** → overlap ~5000 ms of HW warm.
  4. **Delete the vestigial torch env flags** from `run_orion.local.ps1:59-96` (`METER_LOCATOR/_MODEL/_IMGSZ/_CONF/_EVERY`, `LOC_ROI`, `FILL_FORECAST`) — 0 ms now but prevents a silent ~3–6 s regression if torch is ever reinstalled while these are set.
  5. (smoothness, not startup) ensure `timeBeginPeriod(1)` for the preview thread — closes the ~3.5 fps deficit + 32 ms judder holes. **Not** the QVideoSink rewrite (out of scope).
- **CONFIDENCE:** High on the load-bearing "torch is dead weight" (4 proofs) and all path timings (measured); Medium on the ~8 s native-gap attribution (needs native instrumentation).

---

## 3. Reproduction & validation harness (what runs NOW, what's missing)

- **Detection replay — PROVEN runnable here** (no torch, `.venv/Scripts/python.exe`): `replay_simple_reader.py --session <dir>` drives the real `SimpleMeterReader` and already dumps per-frame `det/fill/vel/rise/**rejection_reason**/conf/bbox` and aggregates `within_shot_blinks (D1) / frozen_fill_runs (D2/D3) / bbox_teleports (D5)`. **D1/D2/D3 reproduced directly** on `session_20260717_192510`, `_231912`, `session_20260706_190737`.
- **Regression gate — PROVEN runnable here:** `run_gates.py --no-timing --sessions session_20260707_175017` → **10/10 PASS, exit 0, 73 s** (target is **46/46** detection; use `.venv` python — the `C:/Python314` usage string is stale). This is the guardrail for every D1/D2/D3/D5 fix.
- **Two small enablers needed to make the last proofs MEASURED not inferred:**
  1. **~5-line harness patch** to read reader instance attrs after `detect()`: `_scale_est`, `_size_base`, `_fresh_rise_left`, `fillable_h`, `max(_track_h_hist[-8:])`, band rect + `_vy_down/_vz_ref`, and the mask-pixel counts (`redpx220/redpx170/greenpx` over `_relocate_window()` **and** `_band_eff()`). No reader edit — turns D1's A-vs-B, D3's "fired here", and D2's rise-state from inferred to measured.
  2. **Native timing suite** `OrionNativeTests.exe` is built with all Qt DLLs present but produced **no `Totals:` line** here — so the **194** timing assertions (D4/Track-L invariants) can't self-validate offline until it's made to emit its txt (or validated on a live batch).
- **Fresh capture required only for the exact documented incident (2026-07-22, never dumped):** `.\run_orion.local.ps1 -Framedump -Detdiag` → **start shooting promptly after Connect** (the capture window does NOT skip the idle screen — how `session_20260706_043919` was wasted) → shoot the failing types **continuously** (per the log: **Go-To** and **left-side/Standstill**) → immediately copy `logs\diagnostics\framedump\session_<stamp>\` out (next launch's housekeeping ages it out).

---

## 4. The concrete plan — ranked, waved, flag-gated

**Guardrail for every wave:** each fix default-OFF and byte-identical when off; `run_gates.py --no-timing` stays **46/46**; A/B each flag on the reproduced session before any default flip; never widen décor admission; never create a path where a held shot doesn't release.

### Wave 0 — free, now, zero-risk (measured, no detection change)
| Item | Action | Evidence / expected |
|---|---|---|
| D5 | Set `ORION_READER_BOX_PREDICT=0` on the rig (return to code default) | unproven, ≤40 px overshoot, compounds with render ≤60 px |
| Track L #1 | Skip/async the dev-build update check (`ORION_SKIP_UPDATE_CHECK`) | ~8000 ms cold-start saved |
| Track L #2 | Make `Start-Sleep 4 s` conditional on a real reap (`run_orion.local.ps1:193`) | ~4000 ms cold-start saved |
| Track L #4 | Delete vestigial torch env flags (`METER_LOCATOR/LOC_ROI/FILL_FORECAST/METER_TRACK`) | 0 ms now, prevents a ~3–6 s regression + de-aliases the rig from shipped |

### Wave 1 — the ship-blocker chain, offline-provable NOW
1. **Instrument the replay harness** (§3 patch) — prerequisite so every A/B below is measured.
2. **D1 `ORION_READER_STEAL_RELAXED`** (+ `ORION_READER_ABSENT_CONCEDE`) — A/B on `session_20260717_192510`; target `within_shot_blinks↓`, `staleMem→fresh` on the present-but-missed subset; 46/46.
3. **D2 `ORION_READER_SPENT_DROP`** — A/B on `session_20260706_190737`; target `frozen_fill_max↓`, `staleMem −~40 %`; `within_shot_blinks` unchanged (must not re-break D1).
4. **D3 `ORION_READER_TRACK_H_ROBUST_GATE`** — A/B on `session_20260717_231912`; target `fillable_h` recovers post-outlier, `minFreshFill` restores; watch `mid_rise` glitch; 46/46.

> These three fixes attack **one enabler** (armed-hold absence-handling, D1/D2) and **one poison** (`_track_h_hist`, D3). Landing them is expected to collapse D4's blind-fire rate on its own (D4 is 79–81 % downstream).

### Wave 2 — capture-gated + native (settles the last UNPROVENs)
1. **Fresh `-Framedump -Detdiag` capture** of a disappearing Standstill + a Go-To (§3 recipe) → settle D1 A-vs-B on the real incident; confirm Wave-1 fixes on the exact frames.
2. **Make `OrionNativeTests` emit its Totals txt** → unblock offline validation of the 194 timing gates + D4 invariants.
3. **D4 `LIVE_TIP_FIRE` (the 2a project)** — invert fire authority to the live-rise crossing, learned clock demoted to a bounded backstop; **depends on Wave-1 detection health** (the ≥3-fresh/150 ms predicate must hold through the arm). Optional interim: `blindFireUnderReadSuppressEnabled` (hard-cap-backstopped).
4. **D5 `BOX_PREDICT` A/B** on the fresh capture (px error + stale-lead) — only re-enable if it measurably wins; add the native compound-clamp.

---

## 5. Honest gaps / still-UNPROVEN (and the exact thing that closes each)
1. **D1 seq=42 A-vs-B** — its frames were never dumped. Closes with the fresh `-Framedump` + the mask-pixel instrumentation.
2. **D3 "fired on 07-22 seq26-29"** — no internals logged for that session. Closes with the ~5-line attr-logging harness patch on a replayed session.
3. **D2 07-22 shot shape** — inferred peak-coast (not frame-dumped). Closes with the fresh capture.
4. **D4 / Track-L timing invariants** — the native suite doesn't emit totals here. Closes by fixing the txt-emit or a live batch.
5. **Track-L ~8 s update-check attribution** — inferred from adjacent log lines; C++ not readable. Closes with a native timer around the update call.
6. **BOX_PREDICT px magnitude** — no framedump A/B exists (captures pre-date the flag). Closes with a fresh `BP=0/BP=1` capture.

---

## 6. Flag appendix — "unproven fixes already shipped" (the brief's warning, confirmed)
- **Default-OFF in code but forced ON on the rig, no A/B backing them as defaults:** `ORION_READER_BOX_PREDICT` (`:1227`, D5), `ORION_READER_ANCHOR` (`:1089`, D5), `ORION_READER_PCTL_FILL` (`:1113`, D3). Comments literally say "default OFF → byte-identical."
- **Default-ON, recently flipped (behavior changed under-foot; "46/46" claims are days old):** `SCALE_RESET, STALEBREAK, SCALE_GUARD, VZOOM, VZOOM_DOWN, PERF` (07-18); `TIP_TIGHT, OCCL_WIDE, STEAL_PROBATION, SILENT_RESEAT` (07-19).
- **Commented-OFF on the rig because they regressed the gates — leave off:** `RISE_PROBATION`, `SHOT_COAST` (07-19); `FAKELOCK_BREAK` (inert under STALEBREAK anyway).
- **Inert under `ORION_SIMPLE_READER=1` (dead weight / de-alias the rig):** `ORION_METER_LOCATOR` (+model/imgsz/conf/every), `ORION_LOC_ROI`, `ORION_METER_TRACK`, `ORION_FILL_FORECAST` (torch never loads).

_Ends. Rank of attention: **D1 first** (ship-blocker), then D3 (session poison), then D2 (coast) — all Wave-1, offline-provable now; D4 collapses as a consequence; D5 + Track L are cheap Wave-0 wins._
