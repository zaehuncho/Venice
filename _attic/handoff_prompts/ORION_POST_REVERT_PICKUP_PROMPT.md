# Orion — Post-Revert Pickup Prompt

**You are picking up the Orion / NexusVision auto-green bot after a hard revert.** The user rolled back
a batch of changes a previous AI built. This document is the ground truth for where things actually
stand so you can continue **on top of the revert** without re-deriving anything or re-breaking what
already works. Read it fully before touching code.

---

## 0. First thing you do: confirm the baseline

The clean baseline is commit **`b21922c7`** (`test(reader): pin TIP_TIGHT=0 in the chevron-absent tip
test`) on branch `feat/detection-template-anchor-qml-render`.

```bash
git log -1 --format="%h %s"      # expect b21922c7 ...
git status --short                # see what survived / what's still uncommitted after the revert
```

**Do not assume the working tree matches HEAD.** After the revert there may still be uncommitted files
(rig scripts, docs, or partially-kept work). Diff before you build:

```bash
git status --short
git stash list                    # the reverted work may be stashed, not deleted
```

If anything looks load-bearing but untracked (rig launchers, env-gated files, path-spawned children),
**verify before deleting** — some files that look dead break the build/launch if removed.

---

## 1. What this bot is (30-second model)

NBA-2K perfect-green bot. Capture-card CV reads the in-game shot meter → a native timing engine fires
the shoot input through a forked Chiaki Remote Play link, timed to release exactly at the meter tip.
"Perfect local timing = made shot" (online MyCourt is server-adjudicated but **lag-compensated to local
release**, so correct local timing greens regardless of connection).

---

## 2. The live pipeline (authoritative — do not confuse the two readers)

- **LIVE detector = `simple_meter_reader.py`** — pure BGR `cv2.inRange` + `matchTemplate`, gated by
  `ORION_SIMPLE_READER=1`, no torch, ~1ms/frame. This is what runs.
- **INERT = the `meter_detector.py` YOLO chain** (`ORION_METER_LOCATOR`/`METER_TRACK`). Bypassed on the
  shipped path (`ORION_SIMPLE_READER=1` precedes the locator in `remote_play_orchestrator.py ~:586-631`).
  torch never loads. Do not tune it thinking it's live.

- **Timing engine = `native_orion/src/AutomationEngine.cpp`.** Active stack:
  - `autonomous_vision=true` (global feedforward clock path).
  - `tip_gate_enabled=true` — a T4 Tip Gate that defers the global clock and fires on the live-rise
    crossing `predTip = now + (tipTarget - fill) / vg` (~`:2519`).
  - `fused_fire=false`, `measured_lead=false` — both OFF/dormant.
  - Feedforward is the backstop clock (`ffClockMs≈566`, `meterClockMs≈251`).
  - The `verdict` field (`LATE 66ms` / `EARLY 90ms`, `greens=0`) is a **degenerate meter self-grade,
    NOT a real make/miss.** Do not build timing feedback on top of it.

- **Capture = Elgato HD60X** via `cv2.VideoCapture` (DSHOW-first then MSMF), 1-deep buffer, in
  `capture_card_backend.py`.

---

## 3. How to launch (never launch the exe bare)

Use the gitignored rig `run_orion.local.ps1` — it sets `ORION_SIMPLE_READER=1`, the Qt/OpenCV/Release
`$env:PATH` prepend, reaps stale `OrionNative`/`chiaki`/`OrionStream`/`OrionUpdater` + orphan python
sidecars, then `Start-Process` the exe from repo root.

- **Windowless live run:** double-click `run_orion.local.vbs` (hidden window; output → `logs\launcher.log`).
- **Visible console / debug the launcher:** `.\run_orion.local.ps1`
- **Framedump for detector A/B:** `.\run_orion.local.ps1 -Framedump` → `logs\diagnostics\framedump\session_<stamp>\`
  (~30/s, raw-only, max ~200s; **start shooting promptly** — capture begins at feed-live). Feed to
  `tools/diagnostics/replay_simple_reader.py`.
- **Per-frame telemetry:** add `-Detdiag`.
- Exe: `native_orion\build\Release\OrionNative.exe` (dev/Release — dev auth works; **do NOT** use the
  PROD `build_prod` exe with the dev rig, it stalls on auth).

---

## 4. What was actually true before the revert (facts, so you don't re-litigate)

- Detection is **~99% mid-shot on a stable feed.** The apparent "34% miss" in one session was a single
  ~738-frame idle gap, not a detector failure.
- Box flicker was root-caused to ~16 one-frame `steal_reseat`/`occl_relock` blinks at 82-90% fill; the
  fix is **render-side** (overlay box holds across a 1-frame join miss), not reader-side.
- Reader-side hold/box-lock experiments (`SHOT_HOLD`, `SHOT_ROI_LOCK`) were proven **net-negative** —
  they corrupt the fill denominator. Don't reintroduce them.
- Offline gates can lie: a flag set passed 51/51 offline but regressed live −8% (dropped stale locks
  the live H.264 feed couldn't re-acquire). **Always live-verify one change at a time.**
- The real live blockers observed were: (a) **capture feed freeze** (`uniqfps 60→0`, device re-serving
  byte-frozen frames) and (b) **green tip not read during shots** (`green_c=-1`), likely downstream of
  the freeze. These are the things to reproduce and fix on a stable feed.

---

## 5. The keystone unsolved problem (highest value)

**Self-tuning the ~30ms invisible loop latency ("the lead") with NO banner/green reading.**

Proposed method, not yet built: **release-sent → meter-peak-seen delay = the exact lead.** The engine
already has `holdStartMs`, `firstMeterSeenMs`, and the reader has peak detection — wire the measured
delay back as the lead base. Do it from a *real* input→response measurement, **never** from the fake
`verdict` self-grade.

Design stance the user endorsed:
- Live velocity-crossing (the Tip Gate) should **own** the fire when vision is healthy.
- The learned EMA clock is demoted to a **bounded backstop** for when vision can't see (occlusion/fades)
  — keep it (guaranteed-release safety), don't rip it out.
- The lead must be **fully self-calibrating, no manual seeding.**
- Trap: enabling `measured_lead` **zeroes `networkOffsetMs`** (`AutomationEngine.cpp:2358`) — reconcile
  the two so you don't drop network compensation.

---

## 6. Guardrails (non-negotiable)

1. **Flag-gated, default-OFF, byte-identical when off.** Nothing changes shipped behavior until proven.
2. **One change at a time, live-verified.** Offline green ≠ landed.
3. **Independently verify any claim** by reading the code + revert-tracing the test — passing tests ≠ a
   real fix. (This is exactly why the prior batch got reverted — verify before you trust.)
4. **Guaranteed-release invariant is sacred:** every hold must release (both button and tempo modes).
   The tempo `ls_cancel` path (`AutomationEngine.cpp:1744-1770`) can abort-and-rearm forever with no
   release — do not regress it.
5. **Check `uniqfps` (the feed) before blaming the detector.** A frozen feed masquerades as a detector bug.

Regression gate (must stay green): `python tools/regression/run_gates.py` (46/46 detection + native
timing tests) + the reader unit tests.

---

## 7. Suggested first moves after the revert

1. `git status` + `git stash list` → establish the exact post-revert tree; rebuild if the native tree changed.
2. Run one clean live batch and capture `logs\orion_native.log`: confirm `uniqfps~60` (stable feed) and
   whether the green tip reads (`green_c`). This decides whether the freeze/green issues survived the revert.
3. Report back **what the revert actually left in place** (diff summary) and which of the pre-revert
   blockers still reproduce — **before** building anything. When the primary owner (Opus) is back it
   will scope fixes from your report.

Keep changes small, gated, and evidence-backed. When in doubt, measure and report rather than build.
