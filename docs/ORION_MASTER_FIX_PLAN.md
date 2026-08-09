# Orion — Master Fix Plan (5 Tracks, 13 Issues)

_Generated 2026-07-18. Source: provably-fable multi-track diagnose+fix workflow (`wf_7411f9e8-ac8`, 14 agents, read-only across `NexusVision` @ `feat/detection-template-anchor-qml-render` and the `chiaki-ng-src` fork @ `orion`). Every fix was **reproduced at gate/replay/log level or honestly flagged LIVE-ONLY**. Spec-only — nothing applied. All flags default-OFF → byte-identical shipped behavior until per-cell enabled._

---

## The two CRITICAL findings (why the bot misses the tip)

1. **2a — Every standstill release fires at 49–64% fill on a 250ms-drifted clock.** Log-exact from `logs/orion_native.log` (23:24 session, seq 116-123): `tipGateDeferMs=0` on all releases, fill-at-release 49–64% vs a 99.6% target, driven by a `250.5ms` learned clock recorded inside `learning.json` against a real `412.9ms` need. **The bot leans on drifted per-type clocks instead of the live rise — so it structurally cannot reach the tip on its primary shot type.** Fix: invert fire authority — the closed-form live-rise crossing owns the release; the learned clock is demoted to a bounded backstop.

2. **1a — The meter dives below the search band exactly when the shot arms.** Full-frame ground-truth scan of `session_20260717_231912` (f100-260): the shot-synchronized camera dive carries the meter floor from y≈616 to y≈891 — **through and below the fixed band bottom y1=770 — on every shot.** The existing `ORION_READER_VZOOM` follow shifts the band **up only** (the bottom-follow was removed in a prior A/B), so the descent is unhandled by design → frozen coast, false green-holds pinning a stale fill, truncated columns misreading ~50% as ~16–44%, and `detected=False` overlay blinks. **This is the "disappears when arming" complaint, and it's the prerequisite for the timing fixes' health gates to hold through the arm.**

---

## Ranked (impact × confidence)

| # | Track | Flag | Impact | Conf | Δms | Offline-provable? |
|---|---|---|---|---|---|---|
| **2a** | TIMING | `ORION_LIVE_TIP_FIRE` | CRITICAL — fixes every standstill release | HIGH dx / MED live-land | 0 | math offline; ~70ms lead LIVE |
| **1a** | DETECTION | `ORION_READER_VZOOM_DOWN` | CRITICAL — the arm-dive drop | HIGH | +0.07 | YES (frame-exact repro) |
| **2c** | TIMING | `tempoOwnClockOnly` | HIGH — tempo fires 250–600ms early | HIGH dx | 0 | selection offline; RS-flick LIVE |
| **2b** | TIMING | `ORION_GOTO_METER_WAIT` | HIGH — Go-To blind-fires, never sees a meter | HIGH | 0 | YES (4/4 native gates) |
| **1b** | DETECTION | `ORION_READER_FILL_GEOM` | HIGH — 3.6–4.0pp fill/green scale skew | HIGH | +0.01 | YES (283-frame proof) |
| **4** | CONNECTION | `CHIAKI_ORION_PARKED` | HIGH — safe-mode wedge blocks all live A/B | MED-HIGH | 0 | mechanism offline; race LIVE |
| **2d** | TIMING | `ORION_FADE_LIVE_RISE` | HIGH (fades) — −162±76ms vs 13–43ms window | MED (grader saturates) | 0 | needs 1d+2a live |
| **3b** | STREAM | `CHIAKI_ORION_EXPORT_DEFER` | MED-HIGH — ~30ms decode stalls, dup% to 22 | MED | −2 (fork thread) | attribution LIVE |
| **1d** | DETECTION | steal-probation (`STEAL_PROBATION`) | MED direct / HIGH enabling (unblocks B1) | MED-HIGH | 0 | structural (episode latent on head) |
| **1c** | DETECTION | `ORION_READER_BAND_FOLLOW` | MED — position/scale beyond 1a | MED | +0.05 | repro YES; fix not yet A/B'd |
| **3a** | STREAM | `ORION_PREVIEW_SINK` | MED — ~70–110ms preview lag | MED-HIGH | 0 | stage costs offline; glass LIVE |
| **5a** | RTT | P9 staged (`compose_takion`) | MED — court detect dead since 07-02, wrong hop | HIGH dx / MED mag | 0 | dx offline; q.rtt units LIVE |
| **5b** | RTT | `appendUserLog` | LOW gameplay / MED usability | HIGH | 0 | YES (byte-identity test) |

---

## Ship waves

- **Wave 1 — `1a, 2b, 2c, 5b, 4`.** Highest impact-per-risk, all flag-gated, each offline-provable. **`4` first** — its safe-mode wedge currently interrupts every live A/B, so it must land before the live-only validations can even run.
- **Wave 2 — `2a, 1b, 1d`.** `2a` lands once `1a`'s live batch confirms detection holds through the arm (its health predicate depends on it) and wave-1 telemetry has measured the real lead; enable per-cell, **Standstill first.** `1b` rides the same detection batch. `1d` unblocks the B1 flip.
- **Wave 3 — `3b, 5a, 1c`.** `3b` after wave-1's attach/detach discrimination confirms readback attribution (and after `4` re-baselines the stall). `5a` STEPS 2–4 only if the STEP-1 `q.rtt` probe validated units/cadence. `1c` re-scoped after `1a` lands (likely narrows to cold-acquire + chevron-clip).
- **Wave 4 — `2d, 3a`.** `2d` is last by hard dependency (needs `1d` fade density + `2a` crossing both live-confirmed); enable **Left Fade cell first** (2× divergence). `3a` is lowest-urgency comfort.

---

## Frame budget (the reader's `<5ms` gate)

Only the **reader's** per-frame CV cost counts (native-engine + fork changes don't). Adders: `1a` +0.07, `1b` +0.01, `1c` +0.05 (amortized — Tier B runs only armed+cold+missed, every 2nd frame). `1d, 2a–2d, 4, 5a, 5b` +0.00 reader-side. `3b` is −2ms but on the **fork takion thread** — must NOT be claimed as reader shed.

**Gross: ~5.5 + 0.13 = ~5.63ms — the fixes make an already-over budget slightly worse.** Sheds:
1. **Secondary (from 1a itself):** fewer coast/steal frames (18→8 in repro) → each avoided coast skips the band-wide steal `_scan` (the most expensive path). Expect −0.1 to −0.3 on shot-window medians — **measure, don't assume.**
2. **Primary (new, must pass full discipline):** `ORION_READER_COLDSCAN_DECIMATE` — when **unarmed AND unlocked**, run the full-band cold scans every 2nd frame. Off-shot acquisition latency is timing-irrelevant (nothing fires off-shot); armed path untouched. Expected −0.6 to −1.0ms.

**Net (if the decimation shed proves out):** ~4.3–4.9ms, under budget. Tripwire: `read_ms_median < 5.0` every session in the all-flags-ON run. **HONEST: the decimation shed is proposed, not prototyped — its estimate is engineering, not measurement. Without it the set lands at ~5.63ms, still over.**

---

## Cross-track dependencies (the A2×B1 lesson: sweep combined, not alone)

- **2a underlies 2c and 2d** — both hand fire authority to 2a's crossing machinery. Land/validate 2a's crossing+lead before grading 2c seeding or flipping 2d.
- **1a feeds 2a/2d health gates** — the ≥3-fresh-accepts/150ms predicate fails during today's arm dive; without 1a, 2a degrades to backstop fires on exactly the shots that matter. Land 1a in the same or earlier wave as 2a.
- **1d before 2d and before any B1 flip** — 2d needs 1d's fade sample density; B1 (`OCCL_WIDE`) can't ship without 1d's steal-probation (the A2×B1 knife-edge).
- **1a/1c/1d all touch band/steal/coast in `simple_meter_reader.py`** → sweep as one combined flag set. 1a first, then re-run 1c's repro with 1a ON (1c likely narrows to Tier B).
- **1b × 1c fill-scale collision** — 1b re-references 0% to the red-column floor; 1c un-clips the chevron bottom (~+11pp shift). With both ON the fill scale differs from either alone — re-run 1b's self-consistency (≤1.5pp) + `median_shot_peak` floor with 1c ON before either defaults.
- **2a/2b/2c/2d all edit the same `processHolding` arbitration** (`AutomationEngine.cpp ~:2733-2996`) — implement as one reviewed series: 2b/2c are independent surgical gates (land first), 2a rewrites the arbitration they feed, 2d edits the scheduler pre-commit 2a also touches (co-design the tip-gate inversion with the pre-commit clamp).
- **3a/3b/4 share the stream/present path** — 4's present-skip removes render-thread contention 3b partly blames for stalls. Land 4 first, then re-baseline 3b. **Batch 3b+4+5a-STEP1 telemetry into ONE fork build** to spend one live batch, not three.

---

## Validation protocol

- **S0 baseline** — all new flags unset: `run_gates.py` → 46/46; record per-session SHA-256; native `OrionNativeTests` → 221/221. Every flag-OFF run thereafter must be SHA-identical.
- **S1 per-fix** — each fix's own gate alone (1a descend gate on `session_20260717_231912` f100-260: fresh-red ≥0.88, floor-err>25px ≤2; 1b self-consistency p50 ≤1.5pp; 1c position_adapt probe; 1d steal-ordinal preservation `{123,320,423,516,686,734}` + `occl_relock {219,608}` + `ORION_GATE_WINDOW=0` knife-edge sweep).
- **S2 all-detection-flags-ON** — defaults + `VZOOM_DOWN` + `FILL_GEOM` + `BAND_FOLLOW` + `OCCL_WIDE` + `STEAL_PROBATION` (+`COLDSCAN_DECIMATE` if pursued): `run_gates` → **46/46** with `offshot_falselock=0`, `top_clips=0`, `camera_adapt≥0.78` both, `read_ms_median<5.0` every session, `median_shot_peak≥82`, `mid_rise=0`. Re-run 1b self-consistency inside this combo.
- **S3 native** — rebuild; 221 existing + new (2a synthetic standstill: OFF identical, ON `|press+L−t_tip|` median<8/p95<14ms; 2b hold/abort + late-meter; 2c repro/fix/D1; 2d fade synthetic; 3a presenter; 5b user-log). Then the **engine all-flags-ON combo** re-runs S3 + `run_gates` 46/46.
- **S4 pytest** — `test_simple_meter_reader`, `test_detection_regression`, `test_reader_robust`, `test_rtt_sync_engine` (+3 new takion/compose).
- **S5 fork** — env-unset build shows zero new log lines; 3b pixel-correctness (5-min framedump via `EXPORT_DEFER=1 d3d11va` → lock rate within 1% of eager, 0 mean-Y<16 frames); burst-stall gate inter-arrival p99 within +5%.

**Offline-provable:** 1a, 1b, 1c, 1d (structurally — its episode is latent on head), 2a math, 2b, 2c, 5b, 3a stage costs. **LIVE-ONLY:** 2a's ~70ms end-to-end lead, 2c RS-flick register latency, 2d green-rate parity (grader saturates), 3a glass-to-glass ms, 3b stall-vs-network split, 4 DWM image-hold wedge, 5a q.rtt units, 1a residual H.264-blur.

---

## Live watchlist (next batch: `ORION_DETDIAG=1`, wave-1 flags ON, one variable per cell)

- **1a:** during arm→jump, `band_y1` rides 770→~930 with the bbox floor, stage stays `track`, fill steady (vs today's coast/track_green flip). **Tripwire: any off-shot frame with `lock=1 AND band_y1>770` = bottom-band false lock (must be zero).**
- **2a:** `fillAtRel` 49-64 → ≥88, `peakFill` 96-100, `tipGateDeferMs>0`, `landing_short_ms` median within ±14ms, zero armed no-fire past backstop; `L_used` converging to oracle at N≥3.
- **2b:** zero Go-To `feedforward_target` fires at fill 0/nodet; hold→firstMeterSeen 1.5–2.5s; abort >~3% = fix detection coverage, not gating.
- **2c:** fade|tempo `fillAtRel` median ≥88 (was 0–64.5); a `|tempo` key written within 2 shots; `clockSrc` never `borrowed`.
- **2d (after 1d+2a):** live_rise ownership >70% of fade fires; Left Fade FusedShadow `|delta|` 162 → <25ms. **Tripwire: any fade committing at the +100ms clamp rail = biased-late crossings, freeze cell.**
- **4:** 10 connects — capture-health at +5s shows `export_fps≥55/dup%<10` every connect (failure was 4/97-100); zero frozen-echo restarts; connect→first_unique <20s. **Any `wedge_suspect` with `parked=1` falsifies the fix.**
- **5a:** `ORION_QRTT` lines ≥0.5/s scaling vs senkusha (answers units+cadence before any timing use); Network tab flips to "Takion RTT (PS5)" 3–15ms.
- **5b:** `orion_user.log` local timestamps, one released-line per shot, verdicts match the on-screen banner, zero engineer tokens.

---

## Per-fix appendix (flag · file:line · repro)

- **1a** `ORION_READER_VZOOM_DOWN` — `simple_meter_reader.py` `__init__ :1138-1156`, `_band_eff :1443-1467` (widen y1 only), vshift `:2883-2891`, telemetry `:2607/2664/2895`; `remote_play_orchestrator.py :2462` DETDIAG. **Repro: frame-exact offline; scratchpad fix 83→93/105 fresh-red, floor-err 10→1, 46/46.**
- **1b** `ORION_READER_FILL_GEOM` — `simple_meter_reader.py` `__init__ ~:1079`, `_read_fill ~:2xxx` (one physically-referenced scale: col floor=0%, apex=100%, geometric g_start). **Repro: 283 frames, 2 sessions, synthetic pad-bias proof.**
- **1c** `ORION_READER_BAND_FOLLOW` — `simple_meter_reader.py` `__init__ ~:1141` (Tier A bottom-saturation ratchet on relocate clamp + Tier B armed-miss extended acquire). **Repro: gate-level on real framedumps; fix feasibility-checked, not yet A/B'd.**
- **1d** steal-probation — `simple_meter_reader.py` `__init__` after `:1137` + the coast-steal `:2455-2486` (a non-hw steal must prove rise-or-green within 4 frames or drop + clear the spent occl window). **Repro: structural — probe 4 < MIN_CORE 5; A2's 6 steal ordinals preserved.**
- **2a** `ORION_LIVE_TIP_FIRE` — `native_orion/src/AutomationEngine.cpp` tip-gate/clock-arbitration `~:2820-2850`. **Repro: log-level (fill 49-64% at release, 250.5ms drift). ~70ms lead is LIVE-seeded until oracle N≥3.**
- **2b** `ORION_GOTO_METER_WAIT` — `AutomationEngine.h` RemapConfig + the two blind-fire sites (exclude Go-To; 15s hard cap → neutral abort, latches cleared). **Repro: shipped test binary, 4/4 targeted gates.**
- **2c** `tempoOwnClockOnly` — `AutomationEngine.h` RemapConfig + tempo clock-selection (borrowed clock never holds tempo fire authority; unlearned bucket → unarmed → vision times + seeds `|tempo`). **Repro: log-level, 770−110=660ms planned clock, zero `|tempo` keys in learning.json.**
- **2d** `ORION_FADE_LIVE_RISE` — `AutomationEngine.cpp` scheduler pre-commit `:2965-2996`. **Repro: log-level (42/43 fades fired on clock, −162±76ms). Magnitude unproven — grader saturates ±66/90ms.**
- **3a** `ORION_PREVIEW_SINK` — `autogreen_sidecar.py:_preview_loop ~:598`, `RemoteFrameProvider`/QML (direct QSG texture-swap vs image:// round-trip). **Repro: stage costs measured (5.9+6.3ms); glass-to-glass LIVE. ~35ms is HD60X device latency, unfixable in software.**
- **3b** `CHIAKI_ORION_EXPORT_DEFER` — FORK `lib/src/ffmpegdecoder.c :366-373` (cb outside mutex) + export off the takion thread for d3d11va/cuda + pooled readback. **Repro: log-level jitter signature; stall-vs-network split is LIVE.**
- **4** `CHIAKI_ORION_PARKED` — FORK `gui/src/qmlmainwindow.cpp init() :4713-4719` + render() pipe-mode present-skip (parked window unconditionally skips present). **Repro: failure chain log-reproduced both repos; DWM race is LIVE.**
- **5a** P9 staged — FORK `lib/src/streamconnection.c :692-708` (store+log `q.rtt`), `streamconnection.h:80`; then netstats pipe → `compose_takion` into the Kalman with unit-normalizer; ping demoted to bias. **Repro: log-proven dead court feed since 07-02 + wrong hop. STEP-1 log-only probe first — nothing consumes q.rtt for timing until units confirmed.**
- **5b** `appendUserLog` — `OrionAppController.h/.cpp` (second local-time plain-language emitter to `logs/orion_user.log` from event-edge sites; additive only). **Repro: single-emitter chain verified; byte-identity guaranteed.**

---

## Honest gaps (no hype)

1. **Integration-level:** the combined all-flags-ON state (S2+S3) has **never been run** — each spec swept its own neighborhood. The 1b×1c fill-scale collision and the 1a+1c band-bottom overlap are identified but **unmeasured.**
2. **Carried-over:** 1d's full 5-session 46/46 sweep + pytest were still running at report time; its false-lock kill is structural, not a flipped gate. 1c's fix is feasibility-checked, not A/B'd; Tier B décor-uniqueness is asserted, not measured.
3. **Live-only unprovables:** 2a's ~70ms lead is a seed until oracle-confirmed; 2c RS-flick latency invisible offline; 2d green parity only the banner tally decides; 3b attribution undecided until the attach/detach test; 4's DWM race can't be forced offline (blocking hop inferred from a code comment, not a stack trace); 5a's q.rtt units entirely unverified — **nothing may consume it for timing before the STEP-1 probe.**
4. **The frame-budget shed** (coldscan decimation, −0.6..1.0ms) is **proposed, not prototyped.** Without it the detection set lands at ~5.63ms, over target, with only 1a's unquantified coast reduction as offset.
5. **~35ms of capture-card preview delay is HD60X device latency** — no software fix in any wave removes it.

_Five analysis docs now in `docs/`: `ORION_100PCT_TIMING_PLAN.md`, `ORION_CHIAKI_INJECT_PATH_PLAN.md`, `ORION_A2_B1_DECONFLICT.md`, and this master plan. Nothing applied — spec-only. To implement: work the waves in order, each fix behind its flag, validated by S0→S5 before any default flip._
