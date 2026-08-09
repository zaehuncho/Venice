# Orion — Detection Stability Investigation Brief (find + PROVE, don't fix yet)

## Your role
You are a senior investigator on the Orion / NexusVision auto-green bot. Your job in this pass is **NOT to fix** — it is to **locate every root cause below and PROVE each one with concrete, reproducible evidence**, then propose a flag-gated fix per defect. A hypothesis without evidence is worthless here; the team has already burned cycles on plausible-but-wrong theories. Every claim you make must be backed by **(a) exact `file:line`, (b) a real log excerpt or replay measurement, and (c) a before/after number from a reproduction.** If you cannot prove it, label it UNPROVEN and say exactly what evidence would settle it.

Deliverable is an **evidence dossier**, one section per defect, in the format at the end. Ship a proposed fix (flag-gated, default-OFF) per defect, but the bar for "root cause found" is the proof, not the patch.

---

## System context (read before theorizing)

Orion reads the NBA-2K shot meter off an HDMI **capture card** and fires a perfectly-timed release ("green") over a forked Chiaki Remote Play input path. The shipped detector is a **pure-CV reader**, `ORION_SIMPLE_READER=1` → class `SimpleMeterReader` in **`simple_meter_reader.py`** (~2700 LOC). The legacy torch/YOLO chain is retired on this path. Pipeline:

```
capture_card_backend.py  → frame
  → remote_play_orchestrator.py  (per-frame detect loop, self-arm gate, detsummary emitter)
    → simple_meter_reader.py     (acquire → track → box geometry → fill% → green window)
      → C++ AutomationEngine.cpp (release scheduler: vision paths vs blind feedforward clock)
        → box render: native_orion/qml/pages/RemotePlayPage.qml  (the on-screen box overlay)
```

Sidecar (Python) talks to the native C++ app over a frame/JSON pipe. Detection telemetry is emitted as `detsummary:` (per shot) and `Release …:` lines into **`logs/orion_native.log`** (sidecar lines are prefixed `Sidecar:`). The rig launcher is `run_orion.local.ps1` (wrapped by `run_orion.local.vbs`, gitignored dev rig).

**Critical nuance — many fixes ALREADY EXIST as flags, default-OFF, unproven.** e.g. `simple_meter_reader.py` already contains an A1 "SCALE_RESET" two-sided seed gate (`self._scale_reset`, ~line 2374-2401), plus `TIP_TIGHT`, `BOX_PREDICT`, `FAKELOCK_BREAK`, and others. Part of your job is to determine, **with A/B evidence on the replay harness, whether each relevant flag actually fixes its target defect or is inert/harmful.** Do not assume a flag works because a comment says so. Enumerate every `ORION_READER_*` / `ORION_*` env flag the reader and orchestrator read (grep `os.environ`/`getenv`), and record each one's default and its state in `run_orion.local.ps1`.

---

## The defects to root-cause (each has REAL evidence from the 2026-07-21 21:2x live session)

### D1 — Detection DISAPPEARS during the shot / while holding Square (HIGHEST PRIORITY, root cause still unknown)
The box vanishes mid-shot — during the shooting motion or a held-Square. The team has **not** found the true cause. Prior leading theory (unproven): shooting-arm occlusion sweeping across the meter, worse on left-side/left-wing shots, made worse by a left fade sliding the search window left onto décor.

**Hard evidence (this must reproduce):**
```
detsummary: seq=42 samples=33 fresh=0 staleMem=33 confLow=0 staleFrame=0 nodet=0 firstFreshMs=-1 firstMeterMs=-1 minFreshFill=-1.0 shot=Standstill
```
An ENTIRE shot (33/33 frames) with **zero fresh detections** — `firstFreshMs=-1` means the reader NEVER got a real read for that shot; every frame was served from stale memory. Note `nodet=0` and `staleFrame=0`: it wasn't "no meter on screen" and it wasn't stale-frame rejection — the reader had a lock but **never refreshed it**. That distinction is a major clue: is the meter being REJECTED after acquisition (a gate closing), or is the search WINDOW being dragged off the meter, or is acquisition itself failing for this shot from frame 1? These three have different fixes — **prove which one** by instrumenting the per-frame reason each frame in seq=42 was NOT fresh.

**What "prove it" means here:** replay the exact frames of a disappearing shot through the reader with per-frame logging of: acquire success/fail + reason, the search-window rect vs the meter's true rect, every gate that rejected a candidate (conf, scale deadband, band clamp, occlusion budget, red-persistence), and `_bvx/_bvy` window velocity. Then state, with frame numbers, the FIRST frame it went wrong and WHY. If it's occlusion, show the meter pixels are actually occluded on those frames (not just "the gate closed"). If the window slid off, show the rect diverging from the meter. Distinguish left-side vs right-side shots quantitatively (drop rate by shot x-position).

Anchors to start (line numbers may have drifted — re-locate): search band prior + right-logo trim `~simple_meter_reader.py:912-914`; band res-scale `~:1261`; `_relocate_window` clamp `~:1660-1661`; occlusion mitigations `~:1699-1712`; armed-coast conf floor `~:2360-2377`; lock-drop inline reset `~:2383-2390`; rise-state from stale velocity `~:2122-2125,:2446-2450`. Orchestrator CV self-arm `remote_play_orchestrator.py:~2179-2189`.

### D2 — Box STALLS / lingers frozen on screen AFTER the shot ends
After the shot is over the box stays painted on screen, frozen, coasting on stale memory instead of clearing.

**Hard evidence:**
```
detsummary: seq=38 samples=91 fresh=14 staleMem=76 ...
detsummary: seq=40 samples=88 fresh=14 staleMem=73 ...
```
76 and 73 frames served from **stale memory** in a single shot window — the box coasting long after the meter's gone. The armed-hold conf floor (`_armed_coast_max`, ~180 frames) × the orchestrator's self-arm refresh (120 frames on any `detected&&rising`) × a "rising" state re-derived from **stale** velocity form a self-sustaining loop: the coast keeps re-arming itself so the box never dies and cold re-acquire never runs. **Prove the loop exists**: trace, frame by frame across a high-staleMem shot, what keeps `detected=True`, what refreshes the self-arm, and show the fresh-read path is starved the whole time. A `FAKELOCK_BREAK`/`STALEBREAK` flag may already exist — A/B whether enabling it actually collapses staleMem toward 0 without hurting acquisition.

### D3 — Degradation over a session: first shot(s) perfect, then it decays
Every launch, the **first shot fires perfectly to the tip**, sometimes several, then accuracy degrades until restart. Classic session-permanent adaptive-state poisoning.

**Hard evidence:** in the same session, early shots are clean (`seq=41 fresh=18 staleMem=0`, `seq=43 fresh=14 staleMem=0`) while others collapse to stale (`seq=42 fresh=0 staleMem=33`). The suspected ratchets, all in `simple_meter_reader.py`, none reset per-shot or on lock-drop:
- **`_track_h_hist` upward ratchet** — `fillable_h = median(_track_h_hist[-5:])` is BOTH the fill denominator and the box height. The seed gate (`~:2369-2408`) is one-way (`seed = full_cap and cand >= 0.90*max(hist[-8:])`); one tall outlier poisons `max(hist)` → every true height <90% of it is refused forever → fill under-reads for the rest of the session → vision paths starve → blind fire. The two-sided recovery (`_scale_reset`, `~:2374-2401`) **already exists behind a flag** — prove whether it recovers the denominator after an injected tall outlier, on replay, with `fillable_h` and `max(_track_h_hist)` logged per frame.
- **`_scale_est` / `_size_base` poisoning** — EMA fed by ANY confident red lock incl. décor false-locks (`~:2593-2595`), baseline frozen from first 5 locks (`~:1312-1316`), deadband opens once `|est-1|>0.10` and never decays → gates widen → more décor admitted → wider still. Prove it ratchets and never recovers.

**Prove D3**: build a poisoning-replay — feed a session that starts clean then hits a tall-outlier/décor frame, log the denominator + scale + staleMem rate per shot, and show the exact frame after which every subsequent shot under-reads. Then show which existing flag(s) (if enabled) arrest it, with the same metric.

### D4 — Overfire / "doesn't time at all" (downstream symptom of D1–D3, confirm the chain)
Sometimes the bot fires with no real timing — way early.

**Hard evidence:**
```
Release freshness: seq=44 blindFire=1 code=feedforward_target lastFresh=1 fresh=15 staleMs=4 memTrusted=0 ...
Release timing:    seq=44 code=feedforward_target ... fillAtRel=56.2 peakFill=81.4 ...
Release issued:    fill 56.2% target 99.6% (green 86.8-100.0) ... code=feedforward_target ...
```
It released at **56.2% fill** (target 99.6%, green window 86.8–100) on the **blind feedforward clock** (`code=feedforward_target`, `blindFire=1`) — `peakFill=81.4` means the meter never even reached green because the reader under-read it. **Prove the causal chain**: show that `feedforward_target`/`blindFire` fires precisely when the vision paths (`green_confirmed` / `predictive_target`) are starved by D1/D2/D3, i.e. overfire is not an independent engine bug but the *consequence* of detection failing. Anchor: release-path selection + blind clock in `native_orion/src/AutomationEngine.cpp` (grep `feedforward_target`, `blindFire`, `predictive_target`, `green_confirmed`). If you find overfire that does NOT correlate with starved vision, that's a separate engine bug — flag it distinctly.

### D5 — Box geometry quality: perfect box around the WHOLE meter, no lag, no overlay drift
Requirement: the on-screen box must frame the **entire meter** — not overlay the meter itself, not sit over just the green window, no tracking lag, 100% stable. Investigate BOTH the geometry the reader emits (apex/top, elongation, `TIP_TIGHT`, `BOX_PREDICT`) AND the QML overlay that paints it (`native_orion/qml/pages/RemotePlayPage.qml`, the box/`meterBox` elements + how it consumes `frameSerial`/box coords). Determine: is any lag a reader-side prediction issue (`BOX_PREDICT`, default-OFF, never A/B'd on framedumps) or a render-side latency (the box coords are N frames behind the preview image)? Is the box ever sized to the green window instead of the full track (the `cap_top/cap_bot` vs `track_top` distinction, `~:2340-2408`)? Prove each with a framedump: overlay the emitted box rect on the actual meter pixels and measure the pixel error (top/bottom/left/right) across a shot, and the frame-lag between box and image.

---

## Evidence sources & tools (use them — do not theorize from reading alone)

- **Live logs** (`logs/orion_native.log` — large; grep, don't cat): `detsummary:` per-shot fields — `samples` (frames in window), `fresh` (real detections), `staleMem` (served from memory), `confLow` (conf-rejected), `staleFrame`, `nodet` (no meter), `firstFreshMs` (-1 = never got a fresh read), `firstMeterMs`, `minFreshFill`, `shot` (bucket). Plus `Release freshness/timing/issued/tempo` lines (`blindFire`, `code=`, `fillAtRel`, `peakFill`, `green a-b`). `logs/orion_user.log` is the human-readable subset.
- **Replay harnesses** (`tools/diagnostics/`): `replay_simple_reader.py` (drive the reader offline on recorded frames — PRIMARY for A/B), `replay_framedump.py`, `replay_detector.py`, `replay_predictor.py`, `replay_autonomous.py`. Use these to reproduce each defect deterministically and to A/B every flag. If a framedump for a disappearing/degrading session doesn't exist, say so and specify exactly what capture is needed (which session, which frames).
- **Regression gates** (MUST stay green for any proposed fix): `python tools/regression/run_gates.py` (target 46/46 detection + 216 native timing). Reader unit tests: `tests/test_simple_meter_reader.py`, `tests/test_reader_camera_adaptive.py`.
- **Flag inventory**: grep the reader + orchestrator for every `os.environ`/`os.getenv` and every `ORION_*` flag; record name, default, effect, and rig state (`run_orion.local.ps1`). Confirm flag-OFF is byte-identical to shipped for anything not yet flipped.
- **Rig reality check**: the rig currently runs with `ORION_READER_BOX_PREDICT=1` (default-OFF in code, never A/B'd) and `ORION_METER_LOCATOR=1` loading a YOLO `.pt` (`models/orion_meter_n_v6_mycourt960.pt`) **alongside** the simple reader — i.e. torch is still imported/loaded on this path. Determine whether the locator is load-bearing for the reader or dead weight (relevant to both degradation and launcher startup time, below).

---

## Required methodology (evidence standard)
1. **Reproduce offline** on the replay harness before claiming anything — a defect you can't reproduce deterministically is UNPROVEN.
2. **Revert-trace**: for any fix that "passes tests," revert the fix and show the test fails / metric regresses — prove the test actually covers the defect (a passing test ≠ a landed fix).
3. **Quantify**: every root cause needs a number that moves (staleMem rate, `firstFreshMs`, denominator drift, box pixel-error, blindFire rate) before → after.
4. **A/B every relevant existing flag** on the same replayed session; keep only what measurably helps; report keep/revert per flag with the metric.
5. **Isolate D1's three sub-causes** (reject-after-acquire vs window-slide vs acquire-fail) — do not lump them.
6. Distinguish **detection** faults from **render** faults (D5) and from **engine** faults (D4) with evidence, not assumption.

---

## Track L — Launcher: faster, smoother startup (separate, lower risk)
The launcher should load quicker and run smoother, especially at startup. Evidence from the last launch: preview fps ramps `10.4 → 55` over the first ~5 s (capture warm-up), and startup spawns `OrionNative` + 3 python processes with a YOLO `.pt` model load. Investigate and quantify the startup critical path: venv Python spawn, **torch import + `.pt` locator model load** (likely the biggest chunk — confirm with timing), capture-card open/warm-up, first-frame-to-window latency. Files: `run_orion.local.ps1`, `run_orion.local.vbs`, `native_orion/backend/autogreen_sidecar.py` (startup + `preview_stats`/pace/`gate=0.85` de-alias knobs `~:439-529`), `capture_card_backend.py` (`CadenceLock`/`ORION_CAPTURE_PTS_LOCK` `~:290-302`). Propose concrete, low-risk speedups (defer/skip the locator load if it's not load-bearing; parallelize sidecar init vs window show; warm the capture card earlier; timer resolution). **Measure startup ms before/after** for each proposed change. Do NOT attempt the QVideoSink preview rewrite — out of scope.

---

## Guardrails
- Every proposed fix is **flag-gated, default-OFF, byte-identical when off**, validated on the replay harness, then `run_gates.py` must stay 46/46 + 216 native and reader units green. Flip a default ON only with A/B proof.
- Do not loosen décor/false-lock acceptance to "fix" disappearance — that trades a visible miss for silent false-locks (which feed D3's poisoning). Show your fix doesn't widen décor admission.
- Guaranteed-release invariant is sacred: nothing you propose may create a path where a held shot never releases.

---

## Deliverable format — one section per defect (D1–D5 + Track L)
```
## D<n> — <name>
VERDICT: ROOT CAUSE FOUND | PARTIAL | UNPROVEN
Symptom + user report:
Evidence (reproduction):   <replay cmd + session/frames, the metric before>
Root cause:                <exact file:line, the mechanism, WHY it produces the symptom>
Proof:                     <log excerpt / replay numbers / framedump / revert-trace showing THIS is the cause>
  - for D1: which of {reject-after-acquire | window-slide | acquire-fail}, first bad frame #, why
Existing flags tested:     <flag → on/off metric → keep/revert>
Proposed fix:              <flag name, default-OFF, the change, expected metric move>
Verification plan:         <replay A/B + gate/unit deltas that would confirm the fix>
Residual risk / unknowns:
```
Rank the dossier by impact: **D1 (disappearing) first** — that's the ship-blocker. If D1's true cause is genuinely not determinable from available captures, say exactly which capture to record next and how, rather than guessing.
