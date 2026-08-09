# Orion — Session Pickup / Handoff (2026-07-22)

**You are picking up an active debugging + build session on Orion**, a real-time NBA-2K auto-green bot. The owner is testing live on hardware between your turns. Continue the work below. Repo: `C:\Users\aaron\Desktop\NexusVision`; Chiaki fork (sibling): `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`). Read this whole file first — it encodes lessons that cost hours; don't repeat them.

## What Orion is
Capture-card CV reads the NBA-2K shot meter (Elgato HD60X, `simple_meter_reader.py`, `ORION_SIMPLE_READER=1`, pure numpy+cv2, no torch). A forked Chiaki Remote Play session injects the release. The C++ `AutomationEngine` schedules the release to land on the green tip. **Goal = make-rate (green shots).** Priority order the owner set: **detection flawless → tip timing → latency/optimizations.**

## 🔴 THE MOST IMPORTANT LESSON — check the feed FIRST
The owner's last live test reported "no meter detection, no tip timing, shots don't go in." The detector was NOT the cause. The data (`logs/orion_native.log`) showed **the capture feed intermittently FREEZING**: `uniqfps` dropping 60→0 while the game looked perfectly smooth on the owner's screen. The Elgato USB capture (`cv2.VideoCapture`, DSHOW) was **re-serving byte-frozen frames** — `cap.read()` succeeds but the pixels don't change, so the detector sees a stuck frame and fires blind. **Also:** the green tip often read as `green_c=-1` (not found) — likely downstream of the frozen feed.
- **Before diagnosing any detection/timing "regression," grep `Capture health` for `uniqfps`.** If it's dropping to 0, the feed is frozen and everything downstream is garbage-in. Do not tune the detector to chase a capture problem.

## Current state (all UNCOMMITTED in the working tree — nothing committed this session)

**Running:** `OrionNative.exe` pid may vary — a **dev build from HEAD** (`feat/detection-template-anchor-qml-render`), built with VS 2022 + Qt 6.8.0 (toolchain was installed this session; `cmake --build native_orion/build --config Release --target OrionNative` works).

**Fixes applied this session (in the working tree):**
1. **Capture auto-recover** — `capture_card_backend.py`: detects a byte-frozen feed (downsample hash stops changing >800ms) and **re-opens the device in place**. Default-ON (`ORION_CAPTURE_STALL_REOPEN`). New levers if it still stalls: **`ORION_CAPTURE_API=msmf`** (force MSMF — steadier than DSHOW on the HD60X) and `ORION_CAPTURE_BUFFERSIZE`. Owner also swapped the USB cable. **This is the fix currently being tested — verify it worked.**
2. **Box-hold render fix** — `OrionAppController.cpp:5798`: holds the overlay box across a 1-frame detection blink (bounded by the existing ~120ms window). Display-only; does not touch fill/timing. Fixes the cosmetic tip-flicker.
3. **`ORION_READER_TRACK_H_ROBUST_GATE` — ENABLED in the rig.** Proven safe alone (51/51, byte-identical on healthy history; only acts when the meter-height denominator is poisoned). Fixes "first shots perfect then degrades over the session."
4. **`--synthetic-noise` gate mode** — `tools/regression/run_gates.py`: JPEG Q=75 + 5% frame-drop for live-realistic offline testing.
5. **`shot_feedback_reader.py`** — revived (measurement-only make/miss reader). Scaffolded but **BLOCKED**: needs a real `-Framedump` capture of the feedback-word region to build `templates/feedback/*.png`. (The owner deemed banner/green reading unreliable — see timing section; this may be lower priority.)

**Built but REJECTED by data (default-OFF, dormant — recommend deletion, do NOT enable):**
- `ORION_READER_ABSENT_CONCEDE`, `VZOOM_DOWN_COLD`, `SPENT_DROP` — passed offline gates 51/51 but **regressed live −8%** (drop stale locks the live H.264 feed can't re-acquire). **Offline gates LIED.**
- `ORION_READER_STEAL_RELAXED`, `ORION_READER_SHOT_ROI_LOCK` — proven net-negative / zero-benefit offline. SHOT_ROI_LOCK specifically corrupts fill on moving meters.

**Detection reality (verified):** on a *stable* feed, within-shot detection is **~99%** (the reader finds the red meter reliably — valid boxes, correct fill). It is NOT the bottleneck. Overall 66% figures are dominated by idle-between-shots frames, not mid-shot misses.

## Timing model (owner-aligned — build to this)
- The engine **already** times dynamically per-shot: the **Tip Gate** (`tip_gate_enabled=true`) fires on the live-rise crossing `predTip = now + (tipTarget − fill) / velocity` (`AutomationEngine.cpp:2519`), resets each shot. This is the correct design — do NOT rewrite it (`LIVE_TIP_FIRE` is unbuilt and unnecessary; the Tip Gate is that mechanism).
- The one thing that must be *learned* is the **lead** — the ~30ms invisible loop latency (capture delay + input travel). The bot must fire early by the lead so the release *registers* at the tip.
- **THE KEYSTONE NEXT BUILD (banner-free lead self-tuning):** measure the lead directly from the bot's own timeline — **release-sent → meter-peak-seen = the exact lead** (input + capture latency), requiring NO banner/green/make-miss reading. The engine has the pieces (`holdStartMs` = press, `firstMeterSeenMs` = appearance, reader peak detection). Wire the lead off this direct measurement (per-shot, self-correcting) instead of the current fake self-grade (`verdict` field is a degenerate meter self-grade — the phantom "LATE 66ms"). This is a native engine change; do it once the feed is confirmed stable.
- **Fades** are the hard type (occlusion + they get their own learned offset at `:2839`, not the global lead). Don't blindly add the global `effectiveLatency` to fades — it double-counts. Tune per-type with real data.
- RTT/network sync is **second-order** (court detection dead since 07-02, wrong hop; the real hop is the fork `q.rtt`, currently discarded). Do not over-invest. See `docs/ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`.

## Do NOT (hard-won guardrails)
- **Don't stack changes then test them all at once.** We caused two native regressions that way. ONE change → live-verify → then the next.
- **Don't trust offline gates alone.** They passed flags that regressed live. Always live-A/B; use `--synthetic-noise`.
- **Don't rebuild the native exe from HEAD without a runtime smoke-test.** An untested HEAD build regressed the overlay + timing (the deployed exe was overwritten). Verify the box renders + a Standstill times before handing a build to the owner.
- **Don't chase the detector for a capture problem.** Check `uniqfps` first.
- **Independently verify any subagent/other-AI claim** (re-run gates, read the code) before acting — don't take reports on faith.
- Flag-gated default-OFF + byte-identical-when-off for every change. Guaranteed-release hard cap is sacred (no vision/network value may block a held shot's release).
- Spawned agents default to `model: fable`.

## Next steps (in order)
1. **Owner is testing the capture auto-recover + new cable NOW.** When they report back, pull from `logs/orion_native.log`: (a) `Capture health` — did `uniqfps` hold ~60? any `CONTENT STALL -> re-opening` lines (auto-recover firing)? (b) detection rate + `green_c` (is green found on a stable feed?); (c) `Release submit` / `Shot outcome` timing.
2. **If feed still stalls:** set `ORION_CAPTURE_API=msmf` in `run_orion.local.ps1`, relaunch.
3. **If feed stable but green fails:** investigate green-tip detection (threshold/tuning in `simple_meter_reader.py`, likely NOT a model — a trained YOLO model was already tried and retired for the pure-CV reader). Get a `-Framedump` and LOOK at the green tip.
4. **If feed stable + detection good:** wire the **release→peak lead auto-tune** (the keystone above). Native change; verify offline (`OrionNativeTests`) + live.
5. **Extract the release→peak latency** from any batch to seed/validate the lead.

## Key commands / files
- Launch (owner's rig, gitignored): `.\run_orion.local.ps1 -Detdiag` (add `-Framedump` to capture frames). It auto-reaps the prior instance.
- Detection gates: `ORION_SIMPLE_READER=1 .venv/Scripts/python.exe tools/regression/run_gates.py` (target **51/51**; `--synthetic-noise` for realism). Use `.venv/Scripts/python.exe` (the `C:/Python314` string is stale).
- Replay a session offline: `.venv/Scripts/python.exe tools/diagnostics/replay_simple_reader.py --session <session_dir_name> --jsonl out.jsonl` (framedumps under `logs/diagnostics/framedump/session_*`).
- Native build: `cmake --build native_orion/build --config Release --target OrionNative` (build tree configured for VS2022 + Qt `C:/Users/aaron/Qt/6.8.0/msvc2022_64`).
- Reader unit tests: `.venv/Scripts/python.exe -m pytest tests/test_simple_meter_reader.py` (+ `test_shot_feedback_reader.py`).
- Logs: `logs/orion_native.log` — grep `DETDIAG` (per-frame detection: detected/fill/green_c/bbox), `Capture health` (uniqfps/dup%/cv_fps), `Release submit`, `Shot outcome`.
- Context docs: `docs/ORION_MASTER_FINDINGS_AND_PLAN.md`, `ORION_DETECTION_EVIDENCE_DOSSIER.md`, `ORION_RTT_SYNC_EVIDENCE_DOSSIER.md`.

## How to launch the rig WITH framedump (every way)

The launcher is **`run_orion.local.ps1`** (repo root, gitignored). It takes two switches: **`-Framedump`** (dump frames) and **`-Detdiag`** (per-frame detection telemetry). Use **both** for a diagnostic batch. The `.vbs` double-click wrapper does a **normal launch only — it does NOT pass `-Framedump`**, so a framedump run must go through the `.ps1` with the switch.

**Ways to start it (all equivalent — pick per context):**
1. **From your PowerShell tool (the way this session used):**
   `& "C:\Users\aaron\Desktop\NexusVision\run_orion.local.ps1" -Framedump -Detdiag`
2. **From your Bash tool (call PowerShell):**
   `powershell -ExecutionPolicy Bypass -File "C:/Users/aaron/Desktop/NexusVision/run_orion.local.ps1" -Framedump -Detdiag`
3. **Owner runs it in their own terminal** (from the repo dir): `.\run_orion.local.ps1 -Framedump -Detdiag`
4. **Owner runs it in-session** via the `!` prefix: `! powershell -ExecutionPolicy Bypass -File run_orion.local.ps1 -Framedump -Detdiag`
- Elevation not required (auto-auth works either way; only packet-capture would need admin, and it's off). The launcher **auto-reaps any prior instance** first, so you can just relaunch. It then `Start-Process`es `OrionNative.exe` and exits (the app is windowed; the script does not host it).

**What `-Framedump` does:** dumps raw full-frame PNGs at ~30/s (`ORION_FRAMEDUMP_INTERVAL=0.033`, `RAW_ONLY=1`) to a **per-launch timestamped dir**: `logs\diagnostics\framedump\session_<YYYYMMDD_HHMMSS>\` (non-destructive — never overwrites a prior session). Cap `ORION_FRAMEDUMP_MAX=6000` (~200s).

**Critical caveats (tell the owner before they shoot):**
- Capture starts at **feed-live and does NOT skip the idle/menu** — so **connect and start shooting PROMPTLY and continuously**, or the 6000-frame window burns on the idle screen and holds zero real shots.
- After the run, **copy the session dir out** (or just note its name) before the next launch — housekeeping can age old dumps out.
- **Confirm it's capturing:** the launcher prints the target dir; verify with `ls logs/diagnostics/framedump/session_*/ | wc -l` (file count climbing) or `grep DETDIAG logs/orion_native.log | tail`.
- **Replay a captured session offline:** `.venv/Scripts/python.exe tools/diagnostics/replay_simple_reader.py --session <session_dir_name> --jsonl out.jsonl`.

**Note:** the owner recently added a `$env:PATH` prepend for Qt 6.8.0 + OpenCV bins at the top of the `.ps1` (needed for the native exe + cv2 to resolve their DLLs) — leave it in place.

## One-line status
Detection is solid (~99% on a stable feed) + box-hold + degradation fix. The live blocker being fixed right now is **capture-feed freezing** (cable swap + auto-recover). Once the feed is confirmed stable, the remaining work is: green-tip read (if needed) → the **release→peak lead auto-tune** (the keystone for greening to the tip, banner-free). Nothing is committed; everything is flag-gated in the working tree.
