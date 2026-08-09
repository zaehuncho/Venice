# Orion timing autonomy — architecture & current state

> Single reference for the autonomous shot-meter timing work: what is built, which flags gate it,
> what is proven offline, and what still needs a live batch. Companion docs:
> [`CHIAKI_DECOUPLE_EXPORT.md`](CHIAKI_DECOUPLE_EXPORT.md) (capture path),
> [`UPDATER_CLIENT.md`](UPDATER_CLIENT.md) / [`ADMIN_TOOL.md`](ADMIN_TOOL.md) (release/admin).
> Last updated 2026-06-20 (autonomous hardening run).

## Goal — the AUTOGREENER bar
The project is an **autogreener**: the bot reads the shot meter *freshly every shot* and releases at
the contest-invariant **tip / green window** for every shot type, with **no per-type sliders to
re-dial**. The acceptance bar: **on a clean wired (Ethernet, no lag/jitter) connection the bot is
100% accurate** — the *connection* is the only allowed limiter. So every component must play its role
with no bugs: detector never misses an on-screen meter, engine times the tip, the input lands
deterministically.

## Component status vs the bar
| Role | Component | State |
|---|---|---|
| See the meter | `meter_detector.py` (live) | ~95% on a healthy feed, ~99% on the clean new-export feed; blinks bridged by echo + native memory-trust. Residual misses are *degraded-feed* artifacts (out of the clean-wired bar). |
| Time the tip | `AutomationEngine` Phase C | ✅ autonomous global clock WIRED (flag-gated OFF, unit-proven); needs live A/B to become default |
| Deliver the input | `OrionInputBridge` + `OrionInputClient` | ✅ BUILT + live-active + audited: pre-encryption inject, `TIME_CRITICAL`, latest-wins coalesce, ViGEm fallback. *This is the live output path*, not ViGEm. |
| Network term | `RttEstimator` | ✅ spike-rejecting, unit-tested |
| Don't freeze the feed | chiaki decode-path export | ✅ decoupled from present (Codex); wired in; **needs the live occlusion test** |

## 2026-06-20 autonomous hardening run (committed, all green, flag-gated where live-touching)
- `583d277` injectable mock clock — native ctest 208s→~1s (the iteration enabler)
- `db0300c` removed the troll/DoS feature end-to-end (native + nexus_svc.py + standalone scripts)
- `265343c` autonomous_vision **Phase C** — global-clock release fire
- `450f142` robust **RttEstimator**
- Network tab fixed (`network_packet_capture_enabled`→true, re-signed); input-hook audited (no bug).

## Pipeline (live)
```
OrionNative.exe (native launcher + bot + AutomationEngine)
  └─ embeds OrionStream.exe (custom chiaki-ng fork, Vulkan)         ← frames out (decode-path export)
  └─ autogreen_sidecar (Python: remote_play_orchestrator.py)        ← detector (meter_detector.py)
  └─ release path (live): AutomationEngine → OrionInputClient named pipe
       → chiaki OrionInputBridge → chiaki_session_set_controller_state (PRE-ENCRYPTION inject)
       (ViGEm virtual DualSense is submitted in parallel as the fallback only)
```
Capture tiers (best first): **decoder pipe** (`OrionFrameExport` named pipe) > `screen_region` > `gdi`.
Timing brain = the **native C++ `AutomationEngine`** (authoritative in the live OrionNative path).
Output = the **pre-encryption input hook** (lowest-latency, jitter-free); ViGEm is fallback only.

## Engine timing model (`native_orion/src/AutomationEngine.{h,cpp}`)
- `nowMs()` = monotonic clock; `process()` reads `now` once and threads it through. `scheduleFire`
  arms a sub-tick deadline; the precise fire thread is controller-side, with an in-tick fallback
  (`now >= deadline + schedulerGraceMs`) so a scheduled shot is never lost.
- **Release blocks #1–#3** (green-confirmed crossing / velocity-trajectory / ETA) fire at
  `predictedCrossing − effectiveLatency`. For fast types the per-type **feedforward clock** preempts
  them; **Go-To already times vision-only** (the user's model) and is left as-is.
- `effectiveLatency = controllerChainMs + remotePlayPipelineMs + |networkOffset| + earlyLateOffsetMs
  + (per-type offset  OR  global learnedLatencyMs) + clamped frameAge`.

### Autonomous-vision (the migration toward one global model)
- **`autonomous_vision_shadow`** (default OFF, **safe to run live**): computes the global-velocity,
  phase-aligned autonomous release deadline each shot and **logs it only** (`Shadow timing:` line) —
  never controls output. This is the live A/B data source.
- **`autonomous_vision`** (default OFF, **control**): *Phase-C-partial*. Today it only swaps the
  per-type offset for the global `learnedLatencyMs` in `effectiveLatency`. The global clock-fire is
  **not yet** wired into the feedforward path (still per-type). Completing + live-validating that flip
  is Phase C — keep this flag OFF until then; use the shadow flag meanwhile. (Documented at the
  `globalApplies` definition in the engine.)
- **Global self-learners** (in `triggerRelease`, gated to vision-timed non-Go-To releases): EMA
  `globalRiseVelocityPctMs` (seed 0.226 %/ms, type-invariant — proven offline), `globalAppearToTipMs`,
  `globalHoldToReleaseMs`, persisted via the `globalTimingLearned` signal. Learn from clean releases
  only — never dropouts/fallback/ambiguous/stale/low-confidence.

### Detector freshness — memory-trust tip-guard
`memory_trust_enabled` (default OFF) promotes a ≤1-frame `meter_memory` echo to fresh-equivalent for
`lastSampleFreshAccept` only, anchored to the last genuine accept (real multi-frame dropouts still go
stale). **Tip-guard:** `memoryTrustMaxFillPct` (=85) blocks promotion at/above the near-green band, so
the precision-critical tip always requires a fresh read. Offline (06-14 capture): rise-freshness
48%→78% with **0** tip-risk frames. Does not touch any detector threshold (no false-accept exposure).

## Flags (settings.json / learning.json)
| key | file | default | state |
|---|---|---|---|
| `autonomous_vision_shadow` | settings | **true** (set for the batch) | compute+log only; live-safe |
| `autonomous_vision` | settings | false | Phase-C-partial control; keep OFF |
| `memory_trust_enabled` | settings | false | tip-guarded; live A/B pending |
| `memory_trust_max_fill_pct` | settings | 85 | tip-guard band |
| `global_rise_velocity_pct_ms` | learning | 0.226 | global velocity seed |
| `global_appear_to_tip_ms` / `global_hold_to_release_ms` / `learned_latency_ms` | learning | learned | self-tuned |
| `stream_render_backend` | settings | vulkan | — |
| `meter_color` | settings | Purple | Arrow2 magenta meter |

Edit settings via the **Python round-trip only** (app closed) then re-sign
(`tools/diagnostics/_resign_settings.py --write`) — never PowerShell (BOM corrupts it).

## Capture: custom chiaki decoder-path export
The bot's frames come from the custom chiaki over the `OrionFrameExport` named pipe. The export now
runs on the **FFmpeg decode callback** (packet-driven), not the Vulkan present — so it **survives
window occlusion/minimize** (the old present-coupled export froze → safe mode → the black-screen
batches). Wired: `resolveChiakiPath` prefers `deploy/chiaki-ng-orion/.../OrionStream.exe`
(SHA `f38308e8`) + `settings.chiaki_path` repointed. Code-reviewed 2026-06-20, no bug (latest-wins
frame slot, blocking WriteFile on a writer thread off the decode path, pre-import HW readback avoids
the Vulkan luma-crush race). Detail: `CHIAKI_DECOUPLE_EXPORT.md`.

## Tests / dev velocity
`AutomationEngine` has an **injectable mock clock** (`advanceTestClock`, test-only — `testClockMs_`
defaults < 0 so the live path is unchanged). Native suite: **207.86s → 0.97s** (128 cases). One
2-engine feedforward race keeps real `qWait`. Run: `ctest --test-dir native_orion/build -C Release -R
OrionNativeTests`.

## Offline tools (`tools/diagnostics/`)
- `replay_predictor.py` — ports `predictCrossingMs`; crossing-error by lead + block-2 sim.
- `replay_autonomous.py` — global-velocity phase-alignment **leave-one-session-out** gate.
- `analyze_shadow.py` — pairs `Release/Shadow/Shot outcome` log lines; per-type model-fired % + deltaMs.
- `replay_detector.py --sim-memory-trust [--mem-trust-max-fill N]` — memory-trust + tip-guard A/B.

## Offline findings (proven)
Rise velocity type-invariant (0.226 %/ms); green window ~23–36 ms wide in time; ~110 ms effective
latency; meter rises ~linearly then hard-caps ~98% (the plateau is part of the template — never
extrapolate through it). **Root limiter = detector lock-on consistency**, not the model: ~84% of
rise-misses are recoverable (meter on screen, gate rejected). Pure vision-only would regress the
working open-loop Standstill — hence the hybrid global clock + bounded vision nudge, shadow-first.

## State & resume checklist
**Committed + green:** AV shadow mode, global learners (dormant), memory-trust tip-guard, injectable
clock, offline tools, chiaki wiring. Native ctest 100% / Python 131 + 1 xfail.

**Live-gated (needs the PS5 — do on return):**
1. Launch `.\run_orion.local.ps1`, **disable Discord overlay** / close OBS. Confirm
   `OrionFrameExport: decoder-path hw readback active`; cover the window → feed must keep flowing
   (the occlusion-freeze fix test).
2. Framedump batch on the **new** export → then build the detector locked-crop recovery (#29) against
   *current* data (the old 06-14 framedump was deleted; re-validate on new data).
3. A/B `memory_trust_enabled` (watch for new EARLY shots) and shadow→`autonomous_vision` control,
   then dial timing.

**Codex's lane (uncommitted):** owner/staff auth + admin tool; import `chiaki-ng-src` →
`vendor/chiaki-ng-orion`. (In-file overlap with the shared OrionAppController/AppConfig/verify_orion
snapshot is already committed — noted in commit `169b075`.)
