# REDTEAM_FINAL_V2 — post-fix adversarial audit of the live path + timing engine

Date: 2026-07-18 · Branch: feat/detection-template-anchor-qml-render · READ-ONLY (no code changed).
Scope: verify THIS session's committed fixes and hunt NEW issues on capture→detect→coast→arbitrate→fire→release and the timing engine. Cross-checked against docs/REDTEAM_{FINAL_LIVEPATH,TIMING_ARBITRATION,CHIAKI_FORK,CONCURRENCY,FINDINGS}.md to avoid re-reporting.

---

## TL;DR (act in the morning)

**Fix verdicts**

| Fix (commit) | Verdict | One-line reason |
|---|---|---|
| ORION_READER_SHOT_COAST (998e0dd0) | **CONFIRMED-CORRECT (core) / SUSPECT (completeness)** | Cannot over-hold static décor (grace needs sustained velocity>25 to latch); self-bounds. BUT the 24-frame budget is a frame count anchored to the last *rising* frame → erodes during a visible peak/hold and shrinks in wall-time at higher fps (NF-2). |
| Patch-A churn gate (ccab3875) | **CONFIRMED-CORRECT (stops churn) / SUSPECT (regressed recovery)** | Kills the 5s restart churn as designed and a truly-dead+stalled stream still recovers. BUT both native watchdogs now key on frame *arrival* age, so a frozen-echo (duplicate frames) + hook-down no longer recovers (NF-1). |
| N1 floor-anchor + N3 width-median (121bef0c) | **CONFIRMED-CORRECT** | N1 makes `_bvy` fill-invariant → *helps* SHOT_COAST hold position through occlusion; composes cleanly. Minor: N3 width history not cleared on lock-drop (NF-4). |
| N4 rise-probation (121bef0c) | **CONFIRMED-CORRECT** | Composes correctly with SHOT_COAST — probation is exempted only while grace is live/hw-armed, never for pure static décor. |
| N5 percentile-fill split (121bef0c) | **CONFIRMED-CORRECT** | Byte-identical when both flags off; when robust on, unchanged. |
| P1 frame-bundle + H1 (aaea830e/b77efa4d) | **CONFIRMED-CORRECT** | Current tree publishes `_frame_bundle` BEFORE `_frame_seq`/`_frame_count` bump; a reader that sees the new seq is guaranteed the new bundle. |
| Fade learn-loop closure (fcfa5c3b) | **CONFIRMED-PRESENT** | Landed; deep math per REDTEAM_TIMING_ARBITRATION Finding 1 (its stated target). Not re-derived here. |

**Top NEW findings (ranked)**

1. **NF-1 — MED→HIGH / HARD** — Frozen-echo blindness: churn gate + frame-stall watchdog both key on `frameAgeMs` (frame arrival), never `pixelAgeMs` (unique-frame). A capture card feeding duplicate frames after the source dies looks "healthy" → no restart, no sidecar wedge-heal (reads succeed) → bot stuck idle with no auto-recovery. ccab3875 *removed* the accidental hook-down recovery of this case.
2. **NF-2 — MED→HIGH / HARD** — SHOT_COAST budget is a 24-*frame* count anchored to the last *rising* read. It erodes during the visible peak/hold that precedes occlusion, and its wall-time shrinks with fps (24f ≈ 0.68s @35fps but ≈0.40s @60fps, ≈0.20s @120fps). The exact mid-shot blind-fire it fixes can recur on a slow-release shot or the higher-fps fork.
3. **NF-3 — LOW→MED / SOFT** — Bounded over-hold: while grace is live BOTH N4 and the breaker are exempt, so a lock that rose once then sits on persistent same-spot décor red holds up to 24 frames of frozen fill and blocks re-acquiring a real meter that pops up elsewhere in that window.
4. **NF-4 — LOW / SOFT** — `_track_w_hist` (and pre-existing `_track_h_hist`) are cleared only in `_reset_state()`, not on the per-lock-drop path → stale emitted bbox width (and, for height, fill denominator) for ~5 frames after re-acquiring a different-sized meter (camera cut / arena switch / replay).
5. **NF-5 — LOW / SOFT** — The breaker counter increments during grace (not reset), pre-charging suppression so a legitimate long green/peak freeze right after a real shot can be suppressed ~36 frames post-grace.

No new HIGH-confidence misfire (wrong-fire) bug found on the default (flags-off) path; the shipped default remains byte-identical. All new findings are on the live-gated flags this session is validating, plus one native watchdog regression that is live-active.

---

## Verified fixes — detail

### SHOT_COAST (998e0dd0) — `simple_meter_reader.py`

Mechanics confirmed by reading the whole loop:
- Decrement: line **2232** runs once per `detect()` (top of loop), unconditional while `>0`.
- Latch: line **2543** sets `_rise_recent_n = _shot_coast_max` (24) ONLY on `evidence=="red" and not green_hold and rs=="rising"`.
- `rs=="rising"` (line **2110**) requires `self._velocity > 25.0` — a least-squares slope over the velocity window, NOT a single-frame delta.

**Q: Can it over-hold a spent/décor meter past a real shot?** For *pure static décor* — **NO** (CONFIRMED). A static object has velocity≈0 → never `rising` → grace never latches → N4 and the breaker still fire normally. The earlier worry that "a single noisy frame latches 24 frames of grace" does **not** hold, because the trigger is the windowed velocity gate, not a per-frame rise. So SHOT_COAST does not weaken the décor guards on static décor. The residual over-hold is NF-3 (requires a genuine rise first).

**Q: Does the 24-frame self-bound expire?** **YES** (CONFIRMED). Decrement is unconditional; re-latch only on a rising red read. Once rising stops, it decays to 0 in ≤24 frames; at 0, `_grace` is False, the CONF_MIN floor (line 2349) lifts (needs `armed or _grace`), conf decays at CONF_RATE and the lock drops within ~1 frame (line 2352→2357). Also hard-reset to 0 on lock-drop (2363), on the N4 no_rise drop (2475). No indefinite-hold path for static content.

**Q: Can the N4/breaker exemption let a genuine fake-lock survive during an armed window?** During a real hw-armed shot the lock is already held by the pre-existing `armed_hold` floor regardless of SHOT_COAST — that is not new. The SHOT_COAST-specific exemptions (N4 at line **2446**, breaker at **2622**) are scoped to `_rise_recent_n>0`, i.e. only within 24 frames of a *provably rising* read. That is the correct scope; it does not create a new armed-window fake-lock. **CONFIRMED-CORRECT** on this axis.

Residual completeness gaps → **NF-2, NF-3, NF-5**.

### Churn gate (ccab3875) — `OrionAppController.cpp:6201`, `RemotePlaySession.h:228`

The restart now requires `orionInput_.connected()==false` AND `frameAgeMs() > 5000` (line **6203**). On the live rig `connected()` is permanently false (pipe never connects; ViGEm fallback carries input), so in practice the branch reduces to a second frame-stall check at a 5s threshold, coordinated with the 15s watchdog via `lastWatchdogRestartMs_`. For its stated goal — stop the ~5s kill/respawn churn and the multiple-Chiaki-window spawn — this is **CONFIRMED-CORRECT**: a healthy feed stands down; a dead+stalled feed still recovers at 5s (faster than the 15s backstop, no double-fire). Regression → **NF-1**.

### N1 anchor (121bef0c) — `simple_meter_reader.py:2492`

N1 anchors `_bvy` to the fill-invariant floor `cy+ch` instead of the fill-polluted centre. This is the correct sign and **directly benefits** SHOT_COAST: during the occlusion coast the box is dead-reckoned by `_bvx/_bvy` (line 2375); with N1 a static-position rising meter yields `_bvy≈0` so the held box does not walk upward off the meter during the extended coast. `_prev_floor_y` is reset on every drop path (2360/2472), so no stale anchor leaks. **CONFIRMED-CORRECT / composes cleanly.**

### N4 rise-probation (121bef0c) — `simple_meter_reader.py:2445`

Verified the ordering: probation is armed only on a fresh COLD red-scan acquire (2318), evaluated on subsequent frames, and exempted at 2446 when `hw or (shot_coast and _rise_recent_n>0)`. Because the grace latch (2543) is *after* the probation check within a frame, a genuinely rising cold lock earns its exemption from frame 2 onward (frame 1 latches, frame 2's check sees it) — correct. A static décor never latches → probation drops it at `_prob_frames`. **CONFIRMED-CORRECT.**

### N3 width-median / N5 pctl-fill (121bef0c)

N3 smooths only the *emitted* bbox width (`box_w`); raw `pw` still drives masks + scale so fill is byte-identical (confirmed at the diff site). N5 gates the percentile read on `robust or pctl_fill`; off→skipped→byte-identical. Both **CONFIRMED-CORRECT**. N3 caveat → NF-4.

### H1 / P1 (b77efa4d) — Python capture loop

Confirmed the current order is: build `_frame_bundle` tuple → `_frame_count += 1` → `_unique_count += 1` → `_frame_seq += 1`. A processing-loop reader that passes its seq freshness gate is guaranteed to unpack THIS frame's bundle (single atomic reference). **CONFIRMED-CORRECT.**

---

## NEW findings — detail

### NF-1 — Frozen-echo defeats both native recovery watchdogs (MED→HIGH, HARD)

- **Where:** churn gate `native_orion/src/OrionAppController.cpp:6203` (`frameAge > kHookDownVideoStallMs_`) and frame-stall watchdog `:2873` (`frameAge > 15000.0`); both read `RemotePlaySession::frameAgeMs()`. The distinct frozen signal `pixelAgeMs_` (`RemotePlaySession.h:229-231` — "time since the last UNIQUE frame … climbs during a frozen echo even while the capture clock still ticks") is consulted by neither.
- **Why frameAge misses it:** `capture_card_backend.py:538` advances `_last_put_ns` on every *successful* read; a frozen HDMI source (PS5 stream dead but the capture device keeps emitting the last/duplicate frame, or a "no-signal" static) returns `ok=True` every read → `frameAgeMs` stays low. The sidecar's in-process wedge self-heal only triggers on read *failure* (`bad >= _BAD_READ_LIMIT`, `:509`), which a duplicate-but-valid frame never hits.
- **Concrete failure:** source dies mid-session, capture card free-runs on a frozen frame, input pipe down (permanent on this rig). Churn gate sees `frameAge<5000` → logs "video feed healthy … NOT restarting". Frame-stall watchdog never trips (`frameAge<15000`). The detection stale-gate correctly stops feeding advancing fill (bot goes idle — no misfire), but nothing restarts the sidecar/stream → **bot stuck idle with no auto-recovery** until the user intervenes.
- **Regression tie:** before ccab3875 the hook-down-alone restart (pathologically) DID respawn the sidecar every 5s, which recovered this case by luck. The churn fix removed that without substituting a content-based signal.
- **Severity:** MED baseline (safe idle, not a misfire), HIGH if the source stays dead (permanent stuck). HARD.
- **Fix (describe):** gate both watchdogs on `frameAge > X OR pixelAge > Y` (e.g. pixelAge > ~4000ms), reusing the existing `pixelAgeMs_` the freshness gate already computes. For the churn gate specifically, require `(frameAge > 5000 OR pixelAge > 5000)` before restarting so a frozen echo with a live capture clock is treated as the dead stream it is.

### NF-2 — SHOT_COAST budget is frame-count + rise-anchored → erodes before/through occlusion (MED→HIGH, HARD)

- **Where:** `simple_meter_reader.py:1058` (`_shot_coast_max = 24` frames) and the latch at `:2543` (only while `rs=="rising"`).
- **Failure 1 (peak erosion):** a 2K shot decelerates into its green/peak; once `fill>=85 and |vel|<=25` the state is `"peak"`, not `"rising"` (line 2112), so the grace stops re-latching *before* the release. If the shot shows N visible peak/hold frames before the arm occludes the meter, only `24-N` grace frames remain when the occlusion (col=None) begins. A shot with a pronounced hang at the top can leave <21 frames — shorter than the measured 21-frame occlusion → lock drops mid-occlusion → the blind fire the fix targets recurs.
- **Failure 2 (fps sensitivity):** 24 is a frame count. The fix was measured on a ~35fps feed (24f≈0.68s vs the 21f≈0.6s occlusion — only ~3 frames of slack). The Chiaki fork targets 60/120fps (REDTEAM_CHIAKI_FORK N1); a shooting-motion occlusion is a fixed *wall-time* (~0.5–0.6s), so at 60fps it is ~30–36 frames and at 120fps ~60–72 frames — both blow past the 24-frame budget → lock drops → blind fire.
- **Severity:** MED→HIGH (a live blind release is the top-severity class). HARD (structural: budget unit is wrong).
- **Fix (describe):** (a) derive `_shot_coast_max` from wall-time (ms) ÷ measured frame interval, not a fixed frame count; and/or (b) also re-latch (or hold) the grace on `rs=="peak"` while locked (the peak is still a live shot in progress), so the countdown starts at the disarm/occlusion edge rather than the last rising frame. Keep the self-bound so a true dead-hold still expires.

### NF-3 — Bounded over-hold blocks re-acquire during grace (LOW→MED, SOFT)

- **Where:** `simple_meter_reader.py:2622` (breaker exempt while `_grace_active`) + `:2446` (N4 exempt) + the coast emit at `:2421`.
- **Failure:** a real meter rises (latches grace 24), the shot ends, then persistent décor red sits at the same spot the relocate window covers. For up to 24 frames the reader keeps the lock, emits a frozen `last_fill`, and both décor guards are suppressed. If the player starts a NEW shot in that ≤24-frame window whose meter appears at a *different* location, the reader is still glued to the old box (relocate finds the décor) and does not cold-acquire the new meter → first rise frames missed / shot missed.
- **Severity:** LOW→MED; requires a genuine prior rise + same-spot décor + a shot within ~0.4–0.7s. Bounded (≤24f). SOFT.
- **Fix (describe):** during grace, still allow a cold re-acquire elsewhere if the held box's fill is frozen AND no rising read has occurred for >K frames; or drop the grace early when a stronger red candidate appears outside the coasted box.

### NF-4 — Track-width/height history not cleared on lock-drop (LOW, SOFT)

- **Where:** `_reset_state()` clears `_track_w_hist`/`_track_h_hist` (`:1198`/`:1202`) but the per-lock-drop path (`:2357`, and N4 drop `:2469`) does not, and `_reset_state()` runs only at init/config-reload.
- **Failure:** after a lock drops and a differently-sized meter is acquired (camera cut, arena switch, replay zoom), the 5-sample median emitted width — and, for `_track_h_hist`, the fill *denominator* — is dominated by the previous meter's samples for ~5 frames → wrong reported bbox width (cosmetic + any bbox-width consumer) and, via height, a transiently wrong fill%.
- **Severity:** LOW (width is not the fill; height bleed self-corrects in ~5 frames and the h-history non-reset is pre-existing). SOFT.
- **Fix (describe):** clear `_track_w_hist`/`_track_h_hist` (and `_fillable_hist`) on the lock-drop paths too, or key them to a per-lock generation id.

### NF-5 — Breaker counter pre-charges during grace (LOW, SOFT)

- **Where:** `simple_meter_reader.py:2608` increments `_static_fill_n` on byte-identical reported fills regardless of grace; only the *suppression* (`:2623`) checks `not _grace_active`.
- **Failure:** during a 24-frame grace holding a frozen peak, the counter climbs to ~24. If the meter then genuinely freezes at that same peak (a real made-shot green/outcome freeze), the remaining budget to the hi-cap (60) is only ~36 frames (~1s @35fps) before the breaker suppresses a legitimate peak hold.
- **Severity:** LOW; borderline against real outcome-freeze durations. SOFT.
- **Fix (describe):** reset or freeze `_static_fill_n` while `_grace_active`, so the post-grace budget is the full cap.

---

## Stress-tested negatives (checked, NOT reporting)

- **SHOT_COAST latches on décor jitter** — NEGATIVE. `rs=="rising"` needs windowed velocity>25 pct/s; per-frame décor jitter cannot reach it. Pure static décor never latches; N4/breaker remain fully active.
- **SHOT_COAST neuters N4** — NEGATIVE. Exemption is scoped to a provably-rising lock; static décor still dropped at `_prob_frames`.
- **N1 anchor drifts the coast box** — NEGATIVE. N1 *reduces* vertical coast drift (fill-invariant `_bvy`); `_prev_floor_y` reset on all drop paths.
- **N5 changes fill when off** — NEGATIVE. Byte-identical guard confirmed at the read site.
- **H1 publishes stale bundle** — NEGATIVE. Bundle published before seq/count bump in the current tree.
- **Churn gate leaves a truly-dead+stalled stream hung** — NEGATIVE. `frameAge>5000` path restarts once and coordinates with the 15s watchdog; only the frozen-*echo* subclass escapes (NF-1).
- **SHOT_COAST adds a cross-thread race** — NEGATIVE. All grace state lives in the single detect thread.

## Not re-reported (already in prior docs)

Timing-engine fade FF non-closure / rebaseline over-shift / double-count / consensus exclusion / bandit misattribution (REDTEAM_TIMING_ARBITRATION F1–F5); H2 Go-To cold-gate, H3 memory extrapolation, P1/P2/P4 reader-mutation races (REDTEAM_FINAL_LIVEPATH / CONCURRENCY); Chiaki fork F1 ref-frame bitmap saturation, F2 dropped history-send, #8/#9 poll-quantize & 1-deep slot (REDTEAM_CHIAKI_FORK). These remain OPEN per those docs and are out of scope for this pass.
