# Meter-Detection Tracking — Synthesis & Triage Plan

Read-only synthesis of the external-AI consensus + the grounded prior analysis against the
**actual source**. Goal: the shortest list of changes that provably fixes the 8 failure modes
(drift, no-lock, inaccuracy, loses-lock, box-shrink, false-locks, incomplete-shot-tracking,
disappears-under-occlusion). Nothing here is theory-only — every NOVEL item has an offline
A/B recipe against real framedumps.

Date: 2026-07-17. Branch: `feat/detection-template-anchor-qml-render`.

---

## 1. LIVE-PATH DETERMINATION (decides everything)

There are **two** possible live readers, and which one runs depends on how the sidecar was
launched. This ambiguity is itself a shipping bug and is the first thing to resolve.

### The reader selector
`remote_play_orchestrator.py` (~586-631) picks the reader from env, all default OFF:
- `ORION_SIMPLE_READER` default `'0'` → `SimpleMeterReader`
- `ORION_COMPRESSED_READER` default `'0'` → `CompressedMeterReader`
- neither set → `else: MeterDetector(styles_dir, det_config)` — the **legacy ~5,700-LOC chain**.

### Who sets the flags in the launch path (whole-repo evidence)
| Flag | Default | Set where in live launch | Effective |
|---|---|---|---|
| `ORION_COMPILED_SIDECAR` | unset | **nowhere in repo** (`RemotePlaySession.cpp:1634` only *reads* it) | **OFF** |
| `ORION_SIMPLE_READER` | `'0'` | `RemotePlaySession.cpp:1692`, but inside `if(useCompiledSidecar)` → skipped when COMPILED_SIDECAR unset | **OFF by repo evidence** |
| `ORION_COMPRESSED_READER` | `'0'` | nowhere | OFF |
| `ORION_READER_ROBUST` | `'0'` (`simple_meter_reader.py:981`) | nowhere | **OFF** |
| `ORION_READER_OCCLUSION` | `'1'` (`simple_meter_reader.py:989`) | nowhere (code default ON) | **ON** |
| `ORION_METER_TRACK` | `'0'` (`meter_box_kalman.py` try_load) | `run_orion.local.ps1:94` (dev rig only) | ON dev-rig / OFF elsewhere |
| `ORION_METER_LOCATOR` | `'0'` | `run_orion.local.ps1:76` (dev rig only) | ON dev-rig / OFF elsewhere |
| `ORION_PARK_TEMPORAL` | `'1'` (`orch:558`) | code default | ON (legacy chain only) |

### Verdict
- **Shipped-to-end-users product (the compiled Nuitka bundle): `SimpleMeterReader`.** The bundle
  excludes torch/ultralytics/scipy, so the legacy chain would crash on import; `RemotePlaySession.cpp:1687-1692`
  forces `ORION_SIMPLE_READER=1` to avoid that. On this path **`ORION_READER_ROBUST` is OFF,
  `ORION_READER_OCCLUSION` is ON, and `ORION_METER_TRACK` is irrelevant** (Kalman box-track is a
  `MeterDetector`-only feature; `SimpleMeterReader` never instantiates it).
- **Repo-default & dev rig (`run_orion.local.ps1`, or `python backend/autogreen_sidecar.py` from
  source): the legacy `MeterDetector` chain** + YOLO locator + `MeterBoxKalman` (ORION_METER_TRACK=1).
- Corroboration that live sessions ran SimpleReader: `tools/diagnostics/replay_simple_reader.py:37-38`
  states *"today's sidecar env had NO ORION_READER_* overrides: occl ON, robust OFF…"* — i.e. the
  today-dated framedumps were produced under the SimpleReader defaults.

### ⚠️ Load-bearing risk to resolve FIRST (item 0)
As committed, `ORION_SIMPLE_READER=1` fires only when `ORION_COMPILED_SIDECAR` is set, and **nothing
in the repo sets it** (no installer, script, or settings.json). So either the real installer sets it
outside this tree, or the shipped product is silently running the **legacy chain**, contradicting the
packaging docs (`tools/package_orion_release.py:86-87`, `installer/README.md:53`). **Confirm the live
reader identity from a session's sidecar log** — grep the log for `CV detector = SimpleMeterReader`
vs `CV detector built: … MeterDetector` — before landing any code. If the frozen-fill sessions
(192218/192510) were captured on the legacy chain, the SimpleReader fixes below will not reproduce
the failure and must be ported to `MeterDetector` instead.

**This plan targets `SimpleMeterReader` as the live path** (the shipped-to-users reader, and the
subject of the entire grounded analysis), and calls out where a fix must also be mirrored into the
legacy chain if item-0 finds that is what actually ran.

---

## 2. RANKED TABLE — consensus ideas + grounded findings vs the actual code

Legend: **AI** = already-implemented, **N** = novel/do-it, **R** = reject.

| # | Idea | Status | Evidence / why |
|---|---|---|---|
| C1 | Freeze bbox to FIXED geometry; measure fill separately; never size box from red fill | **AI** | The *reported* box `tbox` is the FULL-TRACK box (`track_top→floor`, denominator `fillable_h` from `_track_h_hist` 5-median), fill read separately as red-extent/`fillable_h`. `_read_fill` (`simple_meter_reader.py` ~1918-1998). Two residual gaps → see N2, N3. |
| C2 | Kalman predict + velocity-aware search window + coast through occlusion | **AI (live path, non-Kalman)** | Live SimpleReader has velocity-widened bounded relocate (`_relocate_window`), motion-follow EMA `_bvx/_bvy`, conf-decay coast + `_occl` forward-predict + `_armed_hold` no-disappear. `MeterBoxKalman` is a *full* CA/CV filter with velocity-gate + 0.7s pure-predict coast but is **OFF/other-path**. Porting the whole filter = **R** (redundant, higher risk); porting only its notch-anchor insight = **N1**. |
| C3 | Multi-cue corroborated acquire; hard "green tip directly above red base" constraint | **AI** | `_acquire_structure` requires a red stub + a strict-green tip dot at the track-height offset (`TIP_UP_MIN..MAX`) above the stub floor, within `±TIP_DX` of centre (`simple_meter_reader.py` ~1515-1548). This IS the constraint. |
| C4 | Green-tip as PRIMARY fixed-size acquire anchor, present at EVERY fill incl. 0 | **R (as stated) / AI (as hold anchor)** | **Already tried and reverted**: all-fills green-tip fingerprint dropped detection to 14% at 720p (~1px chevron unreliable on moving fades) — commit `6fc6509d`/`fbc2e49d`. Green tip is used as the **HOLD** anchor (`_find_green_tip`, green-only hold) not an acquire *requirement*. Requiring it at fill=0 would re-break 720p. |
| C5 | Luma-first edges (4:2:0 kills chroma; Y survives) | **AI (compressed path) / R (capture-card live path)** | `CompressedMeterReader` K2 luma fill + native-Y + tip-sliver + rail-pair, auto quality-managed. But on the **capture-card SimpleReader** path chroma is high-bitrate and intact; git note `compressed_meter_reader.py:17` + history: "luma branches are dead code on the capture-card path." Luma-first there would regress. |
| C6 | Preventive décor rejection (static-red persistence-subtract / accumulator + rise-probation) | **Split: AI (compressed) / N4 (SimpleReader) / R (pixel accumulator)** | Compressed path has a pre-arm rail-pair **registry** + E4 rise-confirm. SimpleReader's only décor guard is the **reactive** fake-lock breaker (40/60 frozen frames) — grounded finding confirmed. A per-pixel static-red **accumulator/EMA** was tried and self-poisoned (`c49dc3d0`) → **R**. The safe preventive form = a **rise-probation gate** on fresh acquire → **N4**. |
| C7 | Fade handling via player-proxy motion (phase-correlation / player-bbox velocity as control input) | **R** | Low value for this meter: on fades the meter POPS IN fully-formed ~60ms before release as *structure*, not a tracked slide (`simple_meter_reader.py` ~947-960). The sliding-fade case is already covered by the box-velocity motion-follow + velocity-widened window. No player-bbox proxy exists and adding phase-correlation is high-cost/speculative. |
| C8 | Explicit state machine (acquire/track/coast + rise/hold/release) | **AI** | `read()` already switches stages `acquire / track / track_green / coast / no_meter / static_suppressed`, evidence `red/green/ncc`, and `rise_state` `rising/peak/spent`. A formal FSM rewrite = churn with no new behavior → **R**. |
| G-A | "ROBUST OFF disables blur-relax + occlusion coast + percentile fill" | **Partly true, partly wrong** | ROBUST OFF *does* disable: percentile occlusion-tolerant fill READ, trajectory gate, dead-reckon coast, blur-relax, scale-rebase, armed-slow-decay-with-fit. It does **NOT** disable the occlusion COAST/relocate/no-disappear — those live under `_occl`/`_armed_hold` (**ON**). The percentile fill READ + blur-relax being off is a real gap → **N5**. |
| G-B | Acquisition red-column-primary → late lock (needs 15-50px red), misses early rise | **True → N6 (guarded, low prio)** | Cold acquire needs `H_ACQ=33`; armed relaxes to `H_ACQ_ARMED=15` + `AR_MIN_ARMED=0.7`. Pure-green fill=0 cannot acquire. An earlier green-corroborated acquire helps but is bounded by the C4/720p regression → guarded, off-by-default. |
| G-C | Box-center velocity polluted by fill growth → phantom upward velocity → drift + coast walk-off; anchor to notch/floor | **True → N1 (TOP)** | Confirmed: `read()` ~2341-2346 computes `_bvy` from the red-column CENTER `cy+ch*0.5`. On a static-position rising meter the floor `cy+ch` is fixed but the center rises at ~½ the fill rate → `_bvy` gains a spurious upward term that drives the `_occl` forward-predicted coast box (~2267-2287) and the relocate window. `MeterBoxKalman` already anchors to the notch (`cb = match.y + match.h`) — proving the fix is correct — but that path is OFF live. |
| G-D | `_box_from_tip` uses red-column height (current fill) → shrink | **True → N2** | `_box_from_tip` (~1553-1556) sets `h = self.box[3]` = last red-column height (= current fill height). On a low-fill green-hold this places the strip floor too high → wrong fill geometry / short box. Should use the full-track height (`_track_h_hist` median). |
| G-E | Per-frame width re-measure → jitter | **True → N3** | Box width `pw` = `col` width + pads, re-measured every frame in `_fill_strip`; the height is already steadied by `_track_h_hist` but width is not → visible box jitter and momentary width-shrink. |
| G-F | Fake-lock breaker is REACTIVE (fires after 40-67 dead frames) | **True → N4** | `detect()` ~2442-2457: suppresses only after `_static_fill_n > 40` (mid) / `60` (near-tip). Feeds the timing stack a dead value for ~0.67-1.0s first. Preventive rise-probation (N4) cuts that to the probation window. |

---

## 3. NOVEL-DO-IT items (with mandatory offline verification)

**Primary harness:** `tools/diagnostics/replay_simple_reader.py --session <dir> [--jsonl <path>]`.
It constructs the real `SimpleMeterReader(cfg=_Cfg())`, drives it frame-by-frame with the synthetic
60fps clock + CV self-arm contract, and already emits: detect latency (p50/p95/max), **within-shot
disappearance (blinks)**, **frozen-fill runs (count + max len)**, **bbox teleport count**,
**velocity-through-rise fraction**, and agreement with the live `detected` flag encoded in each
`fNNNNN_D_raw.png` filename.

**Acceptance harness (committed gate):** `tools/regression/run_gates.py` (wraps
`tools/regression/replay_gates.py::measure_session`) with floors in `tools/regression/gate_floors.json`.
It already computes, over the production reader on each session: `within_shot_detect_rate`
(floor 0.85), `offshot_falselock_episodes` (max 0), `top_clips` (max 0) and `mid_rise_glitches`
(max 0) — the two available **box-shrink / tip-clip proxies** — plus `median_shot_peak` (min 82) and
`fill_vs_chain_within5pp`. Any change must keep every floor green.

**Real sessions:** ⚠️ `session_20260717_192218` **does not exist** — the only 2026-07-17 dump is
`logs/diagnostics/framedump/session_20260717_192510` (5098 frames; the frozen-fill session cited in
the breaker comment: held 25.55 for 222f, 50.61/46.78 for 181f, 91.61 for 120f). Use it as the
primary target plus the other seven `session_*` dirs for regression breadth
(`session_20260704_210801` fade reference/1327f, `_20260706_041446`/2412f, `_20260706_043919`/4261f,
`_20260706_190737`/6777f, `_20260707_121446`/6000f, `_20260707_135309`/6976f, `_20260707_175017`/1709f).
**A/B method:** run both harnesses on `main`-behavior (flag off) vs the change (flag on) over 192510
+ the seven others; the target metric moves in the right direction with no gate-floor regression.

**Ground-truth caveat:** no framedump session carries GT boxes or GT fill — the only in-dir truth is
the binary live `detected` filename flag. So **box-center error vs a true box and fill-at-release are
NOT computable offline** (they need the dual-capture `labels.csv` path via `eval_meter_locator --meta`,
or new labeling) and those items are marked live-gated below.

Three metrics should be **added** (small, ~30 lines, into `replay_simple_reader._metrics` and/or
`replay_gates.measure_session`): (a) *frames-to-first-lock* per detected run; (b) *coast box-center-Y
drift* (max |cy − cy_at_coast_start| across a coast — the self-referential proxy for N1, needs no GT);
(c) *box-shrink events* = frames whose box height or width < 0.8× the running median within a detected
run (a true shrink counter beyond the existing `top_clips`/`mid_rise_glitches` proxies).

---

### N1 — Anchor box velocity to the notch/floor, not the fill-column center  ⭐ TOP
- **File/func:** `simple_meter_reader.py`, `read()` velocity-update block (~2341-2346), + `_reset_state`.
- **Change:** compute `_bvy` from the red-column **floor** `cy+ch` (fill-invariant) instead of the
  center `cy+ch*0.5`; keep `_bvx` on center-x (x is not fill-polluted). Track `_prev_floor_y`
  alongside `_prev_cy`. This mirrors `MeterBoxKalman`'s `cb` notch anchor.
- **Fixes:** drift, loses-lock, disappears-under-occlusion, incomplete-shot-tracking (the phantom
  upward `_bvy` was walking the coast box + relocate window off a static rising meter).
- **Effort/risk:** ~15 lines / **LOW** — arithmetic change on an internal velocity; the reported box
  geometry is unchanged.
- **Verify:** session 192510 + the regression set; **metric = coast box-center-Y drift (new (b)) + within-shot
  disappearance (blinks) + bbox teleport**. Expect drift ↓ and blinks ↓; frozen-fill unchanged.

### N2 — `_box_from_tip` uses full-track height, not red-column height
- **File/func:** `simple_meter_reader.py`, `_box_from_tip` (~1553-1556).
- **Change:** `h = median(_track_h_hist[-5:])` (fall back to the current `self.box[3]` when history is
  empty) so the tip-anchored relocate box reaches the true floor.
- **Fixes:** box-shrink, inaccuracy (green-hold floor placement), loses-lock on cap/deflate.
- **Effort/risk:** ~5 lines / **LOW** — only affects green-only-hold relocate frames.
- **Verify:** session 192510 + the regression set; **metric = box-shrink events (new (c)) on green-hold frames + fill-at-peak
  stability**. Expect short-box frames → ~0; peak fill unchanged (±0.5pp).

### N3 — Steady the box WIDTH with a small median history
- **File/func:** `simple_meter_reader.py`, `_read_fill`/`_fill_strip` (box width `pw`), add a
  `_track_w_hist` deque mirroring `_track_h_hist`.
- **Change:** report `pw` as the 5-sample median of recent locked widths (raw width still drives the
  masks; only the emitted box width is steadied).
- **Fixes:** box-shrink, drift (visual jitter).
- **Effort/risk:** ~10 lines / **LOW**.
- **Verify:** session 192510 + the regression set; **metric = box-width stddev within a detected run (add) + teleport count**.
  Expect width stddev ↓; teleport unchanged.

### N4 — Preventive rise-probation gate on fresh acquire (replaces reliance on the reactive breaker)
- **File/func:** `simple_meter_reader.py`, `read()` acquire branch (~2183-2225) + a per-lock counter;
  behind a new default-OFF flag `ORION_READER_RISE_PROBATION`.
- **Change:** a freshly-acquired lock enters PROBATION: it must show a rising fill (Δfill above noise,
  or `rise_state=='rising'`) within N armed frames (~4-6) or be dropped and its
  location briefly refused — the safe form of C6, ported from the compressed reader's E4 confirm.
  Does **not** use a per-pixel static-red accumulator (that self-poisoned in `c49dc3d0`).
- **Fixes:** false-locks (static red décor / jerseys / logos) — kills them in ~4 frames instead of 40.
- **Effort/risk:** ~30 lines / **MED** — must not clip real early-rise; keep behind the flag until
  live-validated.
- **Verify:** session 192510 + the regression set; **metric = frozen-fill max-run length + false-lock count (detected frames
  where live `D==0`)**. Expect max frozen-run 120-222f → < probation window; **within-shot
  disappearance on real shots must NOT increase** (guard metric).

### N5 — Validate ROBUST's occlusion-tolerant percentile fill READ, then split it out
- **File/func:** `simple_meter_reader.py`, `_read_fill` percentile block (~1963-1981, currently
  `if self._robust`). Split the percentile fill READ (and optionally the bounded blur-relax at
  `~1595`) out of the monolithic `ORION_READER_ROBUST` bundle into a granular default-OFF flag so it
  can ship without dragging the trajectory-gate / dead-reckon / scale-rebase along.
- **Fixes:** inaccuracy + disappears-under-occlusion (fill read survives ~2/3 column occlusion).
- **Effort/risk:** ~20 lines refactor / **MED** — mostly a flag split; the code exists and is tested
  (`tests/test_reader_robust.py`).
- **Verify:** OFFLINE has no fill ground-truth, so use **fill monotonicity + velocity smoothness
  through the rise + frozen-fill** as proxies on session 192510 + the regression set, and mark this **live-gated**: it may
  only default-on after a live A/B (there is no offline fill oracle).

### N6 — (LOW PRIORITY, guarded) Green-corroborated earlier acquire on the early rise
- **File/func:** `simple_meter_reader.py`, `_acquire_structure` / acquire branch; behind default-OFF
  `ORION_READER_EARLY_ACQUIRE`.
- **Change:** allow a shorter red stub (`STUB_H_MIN` ↓) to acquire *only* with the strict green-tip-dot
  corroboration already required — earning the first 2-4 rise frames without a pure-green acquire.
- **Fixes:** no-lock / late-lock on the early rise.
- **Effort/risk:** **MED effort / HIGH risk** — bounded by the C4/720p regression (`6fc6509d`); must be
  proven not to raise false-locks at 720p. Keep off by default.
- **Verify:** session 192510 + a 720p session if available; **metric = frames-to-first-lock (add (a))
  vs false-lock count**. Ship only if first-lock improves with no false-lock increase.

---

## 4. IMPLEMENTATION ORDER for a fleet of agents

**Item 0 — BLOCKING, do first (no code until done):** confirm the live reader from a session sidecar
log (`CV detector = SimpleMeterReader` vs `MeterDetector`) and confirm/repair the
`ORION_COMPILED_SIDECAR` wiring gap (§1 risk). If the failing sessions ran the legacy chain, re-target
N1-N4 into `MeterDetector` (note: `MeterBoxKalman` already fixes N1 there, so on the legacy path the
correct action is `ORION_METER_TRACK=1` validation, not new code).

**Parallel group A — independent, low-risk bug-fixes (each A/B via `replay_simple_reader.py`):**
- **N1** (velocity block in `read()` + `_reset_state`)
- **N2** (`_box_from_tip`)
- **N3** (`_read_fill` width history)

These touch disjoint functions and can run concurrently. Ship them behind ONE shared default-OFF
gate `ORION_READER_ANCHOR`, validate offline on session 192510 + the regression set (byte-identical on pristine frames;
strictly-better on the failing sessions), then a single live A/B, then default-on.

**Sequential after A is validated:**
- **N4** rise-probation — validate *after* the geometry fixes so its metrics aren't confounded.
  Default-OFF flag until a live A/B shows no added within-shot disappearance.

**Parallel validation track (no product code, runs anytime):**
- **N5** — replay ROBUST-on vs -off on session 192510 + the regression set; decide per-feature; split the flag; **live-gated**.
- Harness metric additions (a)/(b)/(c) — a prerequisite for N1/N2/N4 verification; assign to one agent
  up front so the others have the metrics ready.

**Lowest priority / optional:**
- **N6** — only if first-lock latency is still a complaint after N1-N4; HIGH-risk, off-by-default,
  needs a 720p session.

**Flag discipline:** N1/N2/N3 (`ORION_READER_ANCHOR`), N4 (`ORION_READER_RISE_PROBATION`),
N5 (granular split of `ORION_READER_ROBUST`), N6 (`ORION_READER_EARLY_ACQUIRE`) — **all stay
default-OFF until live-validated**, per the repo's byte-identical-shipped convention.

---

## 5. What the consensus got WRONG for this codebase
1. **"Green-tip as primary acquire anchor at every fill" (C4)** — tried and reverted; dropped
   detection to 14% at 720p (`6fc6509d`). Green tip is a HOLD anchor, never an acquire requirement.
2. **"Luma-first edges" (C5)** — correct for the compressed Chiaki stream, WRONG for the capture-card
   SimpleReader live path where chroma is intact and luma is dead code by design.
3. **"Port the Kalman filter" (C2)** — the *notch-anchor insight* is right (→ N1), but the full filter
   is OFF on the live path and re-implementing it is redundant with the 15-line `_bvy` fix; the
   velocity-window + coast it prescribes already exist in SimpleReader.
4. **"Player-proxy / phase-correlation fade tracking" (C7)** — low value; this meter's fades pop in as
   instantaneous structure ~60ms pre-release, not a tracked slide.
5. **"Static-red persistence-subtraction ACCUMULATOR" (C6)** — the per-pixel accumulator/EMA form
   self-poisoned once (`c49dc3d0`, "first 3-4 shots perfect then blind"). The safe preventive form is a
   pre-arm structural registry (exists in compressed) + rise-probation (→ N4), not a red EMA.
6. **"Grounded claim: ROBUST-off disables the occlusion coast" (G-A)** — imprecise; the occlusion
   coast/relocate/no-disappear are under `_occl`/`_armed_hold` (ON). Only the percentile fill READ +
   blur-relax are off (→ N5).

## 6. Bottom line
- **Live path = `SimpleMeterReader`** (shipped bundle), `ROBUST` OFF, `OCCLUSION` ON, no Kalman —
  *pending item-0 confirmation that the installer actually sets `ORION_COMPILED_SIDECAR`; the repo as
  committed would run the legacy chain.*
- **The whole fix set is 3 tiny bug-fixes + 1 guarded gate + 1 flag-split.** Most of the consensus
  (fixed-geometry box, corroborated acquire, coast, velocity window, state machine, green-hold anchor,
  décor guard) is **already implemented**; the genuinely-broken pieces are the fill-polluted box
  velocity (N1), the red-height tip box (N2), the jittery width (N3), the too-late décor breaker (N4),
  and the off-by-flag occlusion fill READ (N5).
