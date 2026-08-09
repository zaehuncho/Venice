# Orion — Second-Opinion Review & Gap-Finding Brief

**You are a senior engineer giving a critical second opinion** on the current state of Orion, a real-time NBA-2K auto-green bot. The team wants you to **review the approach, find anything missing, and suggest concrete improvements** — not rubber-stamp it. Challenge assumptions. If a whole area is being neglected, or a simpler/more robust approach exists, say so. Ground every claim in `file:line` + a real artifact (log, gate result, replay), and be honest about what's live-only or unproven. Repo: `C:\Users\aaron\Desktop\NexusVision`; fork `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`).

## What Orion is (the loop)
- **Detection:** watches the NBA-2K shot meter over an **HDMI capture card** (Elgato HD60X) with a pure-CV reader (`simple_meter_reader.py`, `ORION_SIMPLE_READER=1` — no torch on the shipped path). It reads fill% of a red vertical meter with a green tip.
- **Input:** injects the release through a **forked Chiaki Remote Play** session to a PS5 (input hook, ViGEm fallback).
- **Engine:** a C++ `AutomationEngine` schedules the release to land on the green window (the "tip").
- **Goal that matters:** green rate / make-rate. Everything else is secondary.
- **Priority order (owner's):** meter detection flawless → tip timing → other optimizations.

## Ground truth — current state (verified this session, 2026-07-22)

**Build/platform:**
- Native toolchain (VS 2022 + Qt 6.8.0) was missing after a PC rebuild; now installed, native builds work. The running exe is a fresh **dev** build from HEAD (`feat/detection-template-anchor-qml-render`). A prior **overwrite of the known-good deployed exe with an untested HEAD build regressed the overlay render + timing** — recovered by reverting to pure HEAD + the flags below. **Lesson the reviewer should weigh:** changes were stacked and validated offline, then tested live all-at-once; the discipline going forward is one-change-at-a-time, live-verified.

**Detection (`simple_meter_reader.py`):**
- Runs at the **proven-stable config** (all the session's new flags OFF). Detection is ~66% of frames on a live capture (the ~34% misses are largely meter-absent frames between shots — needs confirmation).
- **Flags built this session but left DEFAULT-OFF / dormant because they regressed live or corrupted fill:**
  - `ABSENT_CONCEDE`, `VZOOM_DOWN_COLD`, `SPENT_DROP`, `TRACK_H_ROBUST_GATE` — passed offline gates (51/51) but **A/B on a real live capture (`session_20260722_135817`) showed −8% detection** (66%→58%): they drop stale locks the live H.264 feed can't re-acquire. Offline gates lied.
  - `STEAL_RELAXED` — gate-safe but **zero measured benefit** (the offline dumps' D1 residual is full occlusion + a sub-gate green sliver, not the desaturation it targets).
  - `SHOT_HOLD` — a reader-side "hold the box through a re-acquire blink" attempt; **abandoned** because it holds a stale fill → **corrupted the read** (broke `mid_rise_glitches` gates). Confirmed the *wrong layer*.
- **The "meter disappears mid-shot" complaint was root-caused:** it's a **cosmetic 1-frame box FLICKER** at the tip (measured: 16 events at 82–90% fill on the live capture), from the reader's steal/relock re-acquire honestly emitting `detected=False` for one frame. **Detection/timing are correct** (the reader being honest is right); only the *drawing* flickered.
- **The fix that shipped (this session, awaiting live confirm):** a **one-line render-side change** in `OrionAppController.cpp:5798` — hold the last overlay box across a blink, bounded by the existing ~120ms `meterConfirmed` freshness window, cleared only when the meter is genuinely gone. Display-only; cannot touch fill/timing/gates.
- Offline regression gate: `python tools/regression/run_gates.py` → **51/51** (uses `.venv` python; note the `C:/Python314` usage string is stale). Reproduces on `logs/diagnostics/framedump/session_*`.

**Timing (`native_orion/src/AutomationEngine.cpp`):**
- The engine **already** has the "fire at the tip on healthy vision" mechanism — the **T4 Tip Gate** (`tip_gate_enabled=true`, defers the learned clock so a tracked shot releases on the meter, `~:2910-2940`). The live-rise crossing math exists (`predTip = now + (target - fillPct) / vg`, `:2519`).
- **Built but OFF:** the stronger **fused-fire ladder** (`fused_fire=false`, `fusedOwns` gated on it + a learned lead), the **prior→posterior blend**, and **measured_lead** (`measured_lead=false`). So the learned feedforward clock is still the backstop, and the Tip Gate is the active tip-timer.
- **Fades are the hard type** — occluded meter + they get **no network offset** (`:2838`); Standstills are the reliable type. Observed live: Standstills time well; Fades read LATE/EARLY.
- `LIVE_TIP_FIRE` as a from-scratch project is **unbuilt/unneeded** — the Tip Gate is that mechanism under a different name; it just needs healthy detection to feed it.

**RTT / network (second-order — do not over-invest):**
- Court detection **dead since 2026-07-02** (packet-capture opt-in off + service not installed + needs ICS). The **PS5 IS routed through the PC via ICS** (confirmed: `vEthernet (PS5-Internal)=192.168.137.1 Preferred`, PS5 `70-66-2A-…` on the scope), so court capture is *feasible* but not enabled.
- **Two-hop model (owner-confirmed):** the RTT that matters is the **PC↔PS5 Remote Play loop** (the fork's `q.rtt`/senkusha — *measured and discarded* at `streamconnection.c:704`), NOT the PS5↔court hop. Court **jitter** is a reconciliation/safety-margin signal (2K greens are console-timed + lag-compensated; marginal green-edge releases get reconciled-away under jitter, worst on tempo). See `docs/ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`.
- **Make/miss oracle is the missing enabler** — the bot cannot currently see whether a shot went in (the `verdict` field is a degenerate meter self-grade producing the fake "LATE 66ms"). `shot_feedback_reader.py` (measurement-only, reads 2K's on-screen feedback word) was **revived + unit-tested (13/13)**; its CV step needs one `-Framedump` capture of the feedback region to finish.

**Reference docs (cross-check, don't trust as done):** `docs/ORION_MASTER_FINDINGS_AND_PLAN.md`, `docs/ORION_DETECTION_EVIDENCE_DOSSIER.md`, `docs/ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`.

## What we want from you — review these, add anything we missed

1. **Detection robustness — is the approach right?** The reader is per-frame acquire+track+coast with many gates. It works ~66% but has fragile re-acquire blinks the offline gates don't catch. **Is there a simpler, more robust design** (e.g., a competitor-style "lock the box at shot start and hold position for the shot, read fill within it, release on shot-end")? Would that be more foolproof than the current complex per-frame logic? What are the trade-offs? Is 66% frame-detection actually a problem, or fine if the misses are meter-absent frames? How would you *measure* "flawless"?

2. **Offline-gates-vs-live gap.** Four flags passed 51/51 offline but regressed live. **Is the offline gate set representative?** What's missing from it (feed realism, H.264 artifacts, shot-type coverage)? How should we validate detection changes so offline green actually predicts live?

3. **Tip timing.** Given the Tip Gate is the active mechanism and Standstills time well but Fades don't: is enabling the **fused-fire ladder** / **measured_lead** the right next lever, or a footgun (measured_lead coupled to a live-learned oracle)? Should Fades get a fundamentally different treatment? Is there a timing gap we're not seeing?

4. **The make/miss oracle.** Is `shot_feedback_reader.py` (reading the on-screen feedback word) the right measurement, or is there a better/simpler make/miss signal (score-delta OCR, a made-shot visual cue)? This gates our ability to *prove* any improvement — is there a faster path to a trustworthy make-rate number?

5. **RTT.** Is the two-hop model (Remote Play RTT = timing lever, court jitter = margin lever) sound? Is plumbing the fork `q.rtt` worth it given it's second-order? Any hop or netcode consideration we've mis-analyzed?

6. **Risk / process.** We had a regression from stacking changes + trusting offline validation. What guardrails would you add? Anything in the guaranteed-release invariant (the bot must ALWAYS release a held shot) that's at risk?

7. **What's MISSING entirely?** A whole area we're not considering — capture-card color/latency calibration, shot-type detection accuracy, multi-resolution robustness, failure recovery, anti-detection, packaging/licensing for ship, telemetry we should be logging, etc.

## Deliverable
A **prioritized list** of findings — *missing things* and *improvements* — ranked by impact on **make-rate**, each with: the claim + `file:line`/artifact, why it matters, a concrete suggested change, and its risk. Separate **offline-provable** from **live-only**. Call out anything you think we've gotten *wrong*. Be blunt; the goal is to ship a bot that greens shots, and a polite miss helps no one.

## Guardrails (context for your suggestions)
Every change is expected to be flag-gated default-OFF + byte-identical when off, validated on `run_gates.py` (51/51) **and** a real live capture before any default flip; one change at a time, live-verified; the guaranteed-release hard cap is sacred (no detection/network/RTT value may block a held shot's release); detection is the make-rate driver (do it first), RTT is second-order.
