# Orion — Master Findings & Execution Plan (Detection + RTT/Sync)

_Consolidated 2026-07-22 from two evidence-graded investigations (detection stability + RTT/network-sync), each run as a multi-agent read-only fan-out with a lead adversarial-verify pass. **Every claim below is backed by exact `file:line` + a real artifact** (log excerpt, settings value, `sc query`, or an offline reproduction). Full proofs live in the two companion dossiers; this is the pickup brief._

- Full detail: **`docs/ORION_DETECTION_EVIDENCE_DOSSIER.md`** (D1–D5 + Track L) · **`docs/ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`** (Q0–Q6a, incl. Addendum v2).
- Repos: `C:\Users\aaron\Desktop\NexusVision` · fork `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`).
- Live evidence session: `2026-07-22T02:2x–02:32Z` in `logs/orion_native.log`.
- **Rules (both tracks):** every fix flag-gated, default-OFF, byte-identical when off; nothing applied; no default flipped; the guaranteed-release invariant is sacred (no path may block a held shot's release).

---

## TL;DR — where the make-rate actually lives

1. **The make-rate is lost in DETECTION, not timing or network.** The primary error is a ~66 ms dead-top under-read: the reader loses the meter as it recedes/dives and serves stale memory, so the bot fires blind. This is the ship-blocker (**D1**) and the cause of the "overfire" (**D4**) and the drifted-clock fires.
2. **Three detection defects are one mechanism in three masks** — the **armed-hold conf-floor** (`simple_meter_reader.py:2899`) that refuses to drop a lock whose meter has left the search band. Fixing it (D1) + the session-poison ratchet (D3) collapses the blind-fire rate on its own.
3. **RTT/network sync is second-order.** Today it applies a ~6.5 ms wrong-hop nudge (10 % of the error). Its one genuinely make-rate-moving idea is **Q6a: a jitter-adaptive aim that shifts tip→center under jitter** so the server can't reconcile-away a marginal green.
4. **A make/miss oracle is the missing enabler.** The bot cannot currently see whether a shot went in — so "advantage" (Q5), Q0's hop confirmation, and Q6a's A/B are all unprovable until it can. Cheapest lever in the whole plan.
5. **What runs offline right now:** detection replay + `run_gates.py --no-timing` (46/46) reproduce D1/D2/D3 today. **What's live-only:** all RTT timing claims, Q0, Q5, Q6a.

---

# PART A — Detection (the make-rate driver)

### D1 — Detection disappears mid-shot  ·  **SHIP-BLOCKER · ROOT CAUSE FOUND**
- **Mechanism:** the armed-hold conf-floor pins `conf ≥ CONF_MIN` for up to `_armed_coast_max=180` frames (`simple_meter_reader.py:2899-2900`), so the lock-drop at `:2901` never runs and the reader serves stale `meter_memory` (`:2998`) while the meter is **absent from the search band**. Sub-cause isolated by instrumented replay: **not** window-slide, **not** gate-rejection (red pixels = 0 in the tight window *and* the full band) → meter genuinely gone (occlusion/fade/dive), armed-hold masks it.
- **Why the existing fix fails:** `VZOOM_DOWN` (arm-dive band-widen, built + default-ON `:1200`) self-disables when `self.box is None` (`:1609`) — the instant the lock drops it can't recover a dived meter. `STALEBREAK`'s breaker is armed-exempted (`:3298`), so it can't break an in-shot dead-hold.
- **Repro (offline, now):** `replay_simple_reader.py --session session_20260717_192510` → `frozen_fill_max=150` (150-frame dead-hold). Live: seq=42 `fresh=0 staleMem=33 firstFreshMs=-1`.
- **Fix (flag-gated, default-OFF):** ① `ORION_READER_STEAL_RELAXED` — extend the coast-steal (`:2719`) to also try R≥170 red + a full-band green-tip when strict-red misses (recovers desaturated/green meters). ② `ORION_READER_ABSENT_CONCEDE` — after K≈20 all-band-zero-pixel frames while armed, stop flooring conf → drop to `nodet`, cold-acquire runs. Metric: `staleMem→fresh`, `firstFreshMs −1→≥0`.

### D2 — Box stalls/coasts after the shot  ·  **PARTIAL (brief's loop refuted)**
- **Real cause:** the same armed-hold floor keeps `detected=True`; the shot-gate stays armed off real recurring shots (451-frame contiguous armed window in the log); the coast-steal re-seats on frozen red without proving a rise → box coasts on a frozen **peak/spent** fill. The brief's "stale-velocity self-arm loop" is **already broken by STALEBREAK** (`:2989`); `FAKELOCK_BREAK` is **inert** (OR'd with STALEBREAK, frozen while armed).
- **Repro:** `session_20260706_190737` → 715 coast frames, 85-frame frozen run at fill 92.67.
- **Fix:** `ORION_READER_SPENT_DROP` — drop the lock while armed **only** when rise-state is `spent` (post-peak, `:2521`) → clean cold re-acquire. ~40 % `staleMem` cut. Must **not** drop `peak` holds (D1 risk) — `spent`-only + `green_seen` exemption.

### D3 — Session degradation (adaptive-state poison)  ·  **ROOT CAUSE FOUND**
- **Mechanism:** the `_track_h_hist` seed gate (`:2373`, `cand ≥ 0.90*max(hist[-8:])`) is one-way, appends only on seed, **never resets mid-session** (`:1380`). One tall outlier pins `max()` forever → `fillable_h` (fill denominator **and** box height) stays inflated → under-reads until restart. **The code's own comment (`:2374`) states this defect verbatim.** `SCALE_RESET` (default-ON) doesn't cover it — it keeps the poison while wiping the recovery accumulator on every lock-drop (`:2924`).
- **Fix:** `ORION_READER_TRACK_H_ROBUST_GATE` — replace bare `max(hist[-8:])` with `min(max, 1.15*median)` in the refusal test; relaxes only the shorter-frame refusal, never widens acceptance. A/B on `session_20260717_231912`; watch `mid_rise` glitch.

### D4 — Overfire / blind feedforward  ·  **ROOT CAUSE FOUND (downstream of D1–D3)**
- Feedforward fires first and hard-returns (`AutomationEngine.cpp:3042`); vision only fires if it declines. **79–81 % of blind fires coincide with same-shot detection starvation.** Decisive proof: seq=44 graded `verdict=LATE +66 ms` while the reader showed 56 % fill — **the "overfire" is the reader under-reading, not early firing.** The real fix `ORION_LIVE_TIP_FIRE` is **confirmed never built** (0 matches in the engine); `learning.json` shows the drift (`global_appear_to_tip_ms=250.5` used where Standstill needs `412.88`).
- **Fix:** none for D4 itself — fixing D1–D3 makes feedforward recede to the safety net it is. Optional default-OFF `blindFireUnderReadSuppressEnabled` (hard-cap-backstopped). The larger `LIVE_TIP_FIRE` (invert authority to the live-rise crossing) is a separate project that depends on D1 detection health.

### D5 — Box geometry  ·  **reader FOUND-correct · render FOUND-not-the-lag**
- Box already spans the **full track** (`track_top`, `:2414/:2428`), not the green window; `TIP_TIGHT` (default-ON) closed the old elongation. Render is **not** the lag — box + preview image are frame-ID-joined (`MeterBoxRing`, `OrionAppController.cpp:5731/5779`). Only real risk: **rig-forced `BOX_PREDICT`** (default-OFF in code, ON on rig) — up to 40 px stale overshoot on decelerating fades, never framedump-A/B'd, can compound with the render-side ≤60 px extrapolation.
- **Fix:** set `ORION_READER_BOX_PREDICT=0` on the rig (return to default) pending a framedump A/B; add a native clamp so the two extrapolation layers can't compound.

### Track L — Launcher startup  ·  **critical path measured; brief's torch guess is wrong**
- **torch/`.pt` load = 0 ms** — it never loads on the shipped `SimpleMeterReader` path (4 independent proofs; torch absent from `.venv`). Real cold-start cost (double-click→preview ≈18–19 s): **native auto-update check ~8 s** (blocking, even dev builds) + **unconditional `Start-Sleep 4s`** (`run_orion.local.ps1:193`) + ~4 s sidecar/capture-open.
- **Fixes (low-risk):** skip/async the dev-build update check (~8 s), make `Start-Sleep` conditional on a real reap (~4 s), open the capture card in parallel, and **delete the vestigial torch env flags** (`METER_LOCATOR/LOC_ROI/FILL_FORECAST/METER_TRACK` — all inert under `SIMPLE_READER`; de-aliases the rig from shipped).

### Cross-cutting (detection)
- **The rig runs a half-inert config:** `ORION_METER_LOCATOR` (YOLO/torch) is **not wired to the shipped reader** (`remote_play_orchestrator.py:2203` detects on the full frame; the locator's only caller is inside the never-constructed `MeterDetector`); `ORION_METER_TRACK` (Kalman) is likewise inert. → the rig tests a config that's partly dead and loads torch for nothing.
- **Unproven-fixes-already-shipped:** default-OFF-in-code but **forced ON on the rig with no A/B backing them as defaults** — `BOX_PREDICT`, `ANCHOR`, `PCTL_FILL`. Recently-flipped-ON (behavior changed under-foot): `SCALE_RESET, STALEBREAK, SCALE_GUARD, VZOOM, VZOOM_DOWN, TIP_TIGHT, OCCL_WIDE, STEAL_PROBATION, SILENT_RESEAT`.
- **Reproduction/validation:** `python tools/regression/run_gates.py --no-timing` → **46/46** runs here (proven 10/10 on one session, no torch; use `.venv` python — the `C:/Python314` usage string is stale). D1/D2/D3 reproduce on existing Jul-4..18 framedumps. Gaps: a ~5-line harness patch to log reader internals (`_track_h_hist`, `_scale_est`, `fillable_h`, band rect, mask-pixel counts) turns D1's A-vs-B and D3's "fired here" from inferred to measured; the native timing suite is **194** gates (not "216") and won't emit its Totals line in this environment. The exact 07-22 incident was never frame-dumped — capture recipe: `run_orion.local.ps1 -Framedump -Detdiag`, shoot promptly + continuously (Go-To + left-side/Standstill), copy the dir out.

---

# PART B — RTT / Network sync (second-order; one real lever)

### State (root cause, verified)
- **Court detection dead since exactly `2026-07-02T09:37:18Z`.** Opt-in gate (`OrionAppController.cpp:2286-2291`) is the sole path to a court lock; `network_packet_capture_opt_in=false` (`settings.json:92`). Compounded: the `NexusVisionSvc` isn't even installed (`sc query`→**1060**) and the WinDivert `NETWORK_FORWARD` sniff needs **ICS topology** to see any packets. **Reviving needs three things, not one:** opt-in + one-time admin install + ICS.
- **Wrong hop by design.** The engine pings PC→gateway ICMP; the release rides PS5→court. The fork measures the correct PS5→court `q.rtt` and **discards it** (`streamconnection.c:704` stores only `measured_bitrate`; no struct field; the log is compiled-out).
- **UI reads wrong** because the bridge-off merge takes the sidecar-wholesale branch always (`OrionAppController.cpp:1810`): Court IP frozen "—", RTT/Jitter/offset all live-but-LAN-hop, packet cards frozen 0. A `syncSource`/`syncConfidence` property pair **exists but is bound by zero QML**. "RRT" typo at `DashboardPage.qml:376`.

### Q1 (the gate) — **APPLIED-BUT-NEGLIGIBLE-WRONG-HOP**
`useMeasuredLead=FALSE` (`measured_lead:false` + `lead_rebaselined:false`) → the offset is **applied, not zeroed**: Standstill + Go-To get ~6.5 ms (`heldOffsetMs=6.6/6.3/6.2`), **fades get nothing** (`:2838`). That ~6.5 ms is `decode_comp + 2.5 ms tick + ~0.4 ms LAN` — the real court round-trip is absent. **The trap:** the instant `measured_lead` is switched on, `:2358` **zeroes** `networkOffsetMs` — so a correct court-RTT must feed the **measured-lead oracle**, not `networkOffsetMs`.

### Q0 — Which hop governs the green — **MIXED: console-timed lead + court-jitter margin**
Confirms the operator model: 2K lag-comps to the local release moment (court RTT is **not** a timing lever); court **jitter** is a reconciliation/safety-margin signal (marginal green-edge releases get reconciled-away under jitter, worst on tempo; dead-center survives). Orion encodes **zero** adjudication/lag-comp — external netcode, must be settled empirically. Confidence **medium** — the log can't confirm it (no hop-varying RTT; `court=-` 100 %).

### ⚠ The pivotal gap — no make/miss oracle
**The bot cannot see whether a shot went in.** 0 make/miss/banner lines in 41 MB; the `verdict` field is a **degenerate meter self-grade** (`OrionAppController.cpp:1669`) whose `LATE=66 ms` mass is literally `meterRecedeLatePct×meterMsPerPct` (the dead-top **detection** artifact, not network). The real timing banner is read only in `bannerCalibration` mode (`AutomationEngine.h:133-137`, off). **Q0 confirmation, Q5 "advantage," and Q6a's A/B all require this oracle first.**

### Q6a — Jitter-adaptive tip→center aim — **the one make-rate lever**
The engine already carries the primitive: `greenCenterPct` computed per shot (`:1341/:1372/:1387`), a `green_center` target mode, and a fused-path tip-backoff `deltaAim` (`:2648-2651`) that already grows with landing uncertainty — but the aim is pinned to `"tip"` (`AutomationEngine.h:79`). **Lever:** `aimJitterBackoffMs = clamp(gain·max(0, courtJitter − floor), 0, maxBackoff[bucket])` added to the aim so higher jitter lands tip→center; **clamped never below `greenCenterPct`** (still deep green); **tempo gets the largest `maxBackoff`** (`mode==TempoSquare`, `:1787/:1886`). Court jitter is the fuel (feed `networkJitterEmaMs_` real RFC3550 court jitter, not the ~0.3 ms LAN). Default-OFF, jitter=0 → byte-identical; fires earlier-never-later (can't touch the release cap). **A/B live-only** (needs the make/miss oracle).

---

# PART C — Unified execution order (both tracks)

Detection is the make-rate; do it first. RTT Wave 0/0.5 are cheap and the oracle unblocks *measuring everything* (including detection make-rate), so run them in parallel.

| # | Track | Item | Gate | Provable |
|---|---|---|---|---|
| **1** | Detection | Instrument the replay harness (log reader internals + mask pixels) | — | offline now |
| **2** | Detection | **D1** `STEAL_RELAXED` + `ABSENT_CONCEDE` · **D2** `SPENT_DROP` · **D3** `TRACK_H_ROBUST_GATE` — A/B each on the reproduced sessions, `run_gates --no-timing` 46/46 | 1 | offline now |
| **3** | Both | **Make/miss oracle** (`bannerCalibration=true` or a CV make/miss+banner reader; log make/miss + release margin `|fillAtRel − greenCenterPct|`) | — | live (enables all proof) |
| **4** | RTT | Wave 0 honest UI (RRT→RTT, surface `syncSource`/hop, Court-IP states, gate packet cards, cut dead `orion_sync_*`) | — | offline now |
| **5** | Detection | `BOX_PREDICT=0` + delete vestigial torch flags + Track-L launcher wins (update-check skip, conditional `Start-Sleep`) | — | offline/measured |
| **6** | RTT | Q3 STEP-1 **dual-RTT log-only** probe (`ORION_QRTT` stderr; RP `q.rtt` + court ping alongside each graded shot) | 3 | live |
| **7** | RTT | Consume RP `q.rtt` as the **lead** delta (layered on the oracle; not `networkOffsetMs` if measured-lead ever on) | 6 | live |
| **8** | RTT | **Q6a jitter→center aim** (the make-rate lever) | 3 + court jitter | live A/B |
| **9** | RTT | Court detection revival — **diagnostic + jitter-feed only** (Q0: zero timing value) | Q0 | live |
| **10** | Detection | `LIVE_TIP_FIRE` (invert to live-rise) — depends on D1 detection health | 2 | live/native |

---

# PART D — Guardrails & honest gaps

**Guardrails (every item):** flag-gated default-OFF, byte-identical when off; fails soft (no network/RTT/court value may block a release; the guaranteed-release hard cap is untouched); `run_gates.py --no-timing` stays 46/46 and reader byte-identity holds; native `OrionNativeTests` green (note: won't emit Totals here — needs fixing or a live batch); packet capture is a surfaced opt-in (admin install + ICS + kernel-driver trade-offs), never silent-flipped.

**Honest gaps (what needs capture / a live rig):**
- Detection: D1 seq=42 A-vs-B and D3 "fired here" need the harness-internals patch + a fresh `-Framedump`; the native 194-timing suite doesn't self-validate here; BOX_PREDICT px magnitude needs a fresh capture (existing dumps pre-date the flag).
- RTT: q.rtt units (seconds vs ms) + cadence + which fork channel is live are STEP-1's job; court re-lock needs opt-in + service + ICS + a game; **Q0/Q5/Q6a cannot be proven without the make/miss oracle**; the ~8 s update-check attribution needs a native timer.

---

# Appendix — quick anchors
- **Detection:** armed-hold floor `simple_meter_reader.py:2899` · breaker armed-exempt `:3298` · `_track_h_hist` seed gate `:2373` · `VZOOM_DOWN` guard `:1609` · box `track_top` `:2428` · feedforward preempt `AutomationEngine.cpp:3042` · `LIVE_TIP_FIRE` = absent · launcher `run_orion.local.ps1:193`.
- **RTT:** opt-in gate `OrionAppController.cpp:2286` · merge-wholesale `:1810` · `useMeasuredLead`/zeroing `AutomationEngine.cpp:2338/:2358` · fork `q.rtt` discard `streamconnection.c:704` · UI typo `DashboardPage.qml:376` · `syncSource` unused `.h:181` · Q6a aim `AutomationEngine.h:79` + `deltaAim :2648` + `greenCenterPct :1341` · make/miss self-grade `OrionAppController.cpp:1669` / banner hook `AutomationEngine.h:133-137`.

_Consolidated from `ORION_DETECTION_EVIDENCE_DOSSIER.md` + `ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`. Nothing applied; all fixes flag-gated default-OFF, pending human review + a live batch._
