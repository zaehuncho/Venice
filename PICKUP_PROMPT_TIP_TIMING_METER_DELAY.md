# PICKUP: Venice tip-timing + meter-delay + Connect + logs

Copy-paste-ready pickup prompt for the next AI agent taking this over. Everything you need to resume without asking the user follow-up questions is in this file.

---

## 1. What is Venice

- NBA-2K shot-timing assistant (customer brand: **Venice**; internal slug still `orion-*` — never rename internal slugs/resources).
- Repo: `C:\Users\aaron\Desktop\NexusVision`, primary branch: `fix/timing-input-and-remoteplay-blockers`, main is `main`.
- Stack: Qt/QML native GUI (`OrionNative.exe`) + Python sidecar via Nuitka (`OrionSidecar.exe`) + Chiaki PS5 Remote Play fork + Windows Ed25519-signed update manifests + AWS Lambda backend + DynamoDB (`orion-licenses`, `orion-update-manifest`) + Inno Setup installer.
- User has an authorized owner license and is testing on the same machine you build on. License minting: `python tools/admin/orion_admin.py license create --email <email> --plan owner --days 3650 --yes --reason "self-test"`.

## 2. What just shipped tonight (already installed on user's main machine)

**Installer path:** `C:\Users\aaron\Desktop\NexusVision\installer\Output\VeniceSetup-1.0.0.exe` (~190 MB, built 2026-08-08 00:25). Manifest signed with the trusted Ed25519 `orion-ed25519-v1` key. Customer manifest correctly excludes `OrionOwner.exe` / `OrionStaff.exe` (they'd fail closed on customer machines otherwise).

**Round 1-3 changes that landed (all currently uncommitted — `git status` shows the full set — do NOT commit or reset without user go-ahead):**

- **Timing**:
  - `user_lead_satisfies_authority` default ON (fixes the "adapting → nothing works" deadlock)
  - `latency_probe_count` 16 → 24
  - `ORION_PRECISE_WAIT_HIRES`, `ORION_TIMER_RES_GUARD` default ON (opt-out via `=0`)
  - Tempo commit-nack no longer collapses a valid gather
- **Meter delay** (NEW subsystem — inbound network delay via WinDivert in `nexus_svc.py`; NOT input delay):
  - Default ON, 250 ms, `OffenseDefense` policy, range 200-300 ms
  - `MeterDelayController` in `native_orion/src/MeterDelayController.{h,cpp}`
  - Wired hooks: court-known signal (from `telemetryCourtIp()` in 4 sites), physical Square edge (raw HID at 4 ms poll), shot-cycle active hint (from `AutomationEngine::shotStateChanged`)
  - `readyForArm()` gate on startup so first shots don't die during ramp
  - Snap-on-recover to kill mid-play disarm
  - `nexus_svc.py` gated by `ORION_METER_DELAY_ARMED` env (launcher sets it)
- **Launcher perf / UX**:
  - Constructor SHA-256 of 2828-file manifest moved to async periodic evaluator
  - All sliders `onMoved: if (!pressed)` idiom
  - `CREATE_NO_WINDOW` on every `taskkill` / `sc.exe` / `python.exe` spawn
  - Timer-res guard skipped on battery unless RP session active
  - Connect + virtual-controller gate use `hasRecentRawInput()` (not stale enumeration)
  - Physical-pad grace 60s → 8s + user-visible warn past 3s
- **Security**:
  - `sha256FileHex` cache keyed on ntfs `changeTime` + 4096-entry FIFO cap
  - Security watchdog 5s → 30s with proper queue-unblock on timeout

**Tests all green:** 477 native / 1437 python / 31 MeterDelay standalone.

## 3. What the user just found broken (this pickup's WORK)

User tested on their main machine. Reported bugs (their words, verbatim priority):

1. **`live_tip_deadline_missed` drift-to-abort** — **ABSOLUTE PRIORITY**. First 2 shots hit tip perfectly, then instant lates, then shots abort entirely with reason `live_tip_deadline_missed`. Ship-blocker + core selling point ("tip timing ABSOLUTE PRIORITY... perfect timed shots always unless there's lag or jitter").
2. **Connect fails 5-10 times with pad plugged in** — "no 'plug in controller' bug when I'm already plugged in". Connect must succeed first try.
3. **Meter delay toggle inoperative** — user couldn't test meter delay because the toggle "doesn't work". Must be robust and foolproof; bot must work entirely with it on.
4. **Logs not copy-pasteable** — user wants to select+copy the entire Activity view to paste back for debugging.

User's directive on execution: **Fable 5 model ONLY** for agents. Agents implement fixes directly. Do NOT ping user until everything is production-ready.

## 4. Log evidence I already extracted from screenshots

Screenshots live in `C:\Users\aaron\Pictures\Screenshots\`. Most recent (analyzed):

| Time | Log line (paraphrased) |
|---|---|
| 22:18:15 | `PHASE SAMPLE raw_ms=312.5 normalized=312.5 anchor_pct=30.0 accepted=1 band 240..430` |
| 22:18:15 | `Tip phase measurement: physical_median_ms=310.2 n=3 sample_ms=312.5 anchor_pct=30.0 aim_offset_ms=74.0` |
| 22:18:15 | `Release landing: seq=3 graded=1 peak_fill=98.13 settled_fill=50.43 fill_at_rel=61.64 travel_pp=36.49` |
| 22:18:15 | `Outcome identity: physical_epoch=17 shot_attempt=3 release_seq=3 verdict=LATE` |
| 22:20:26 | `Tip phase measurement: physical_median_ms=310.2 n=17 sample_ms=304.2 anchor_pct=30.0 aim_offset_ms=74.0` |
| 22:20:26 | `Release landing: seq=17 graded=1 peak_fill=96.06 settled_fill=93.33 fill_at_rel=43.95 travel_pp=52.11` |
| 22:20:26 | `Outcome identity: physical_epoch=34 shot_attempt=19 release_seq=17 verdict=EXCELLENT` |
| 22:20:26 | `Self-grade diagnostic: seq=17 verdict=EXCELLENT errorMs=0.0 learnedOffset=0.0 ffClockMs=` |
| 00:40:57 | `Shot abort identity: physical_epoch=24 shot_attempt=17 release_seq=0 schedule_token=0 reason=live_tip_deadline_missed` |
| 00:40:57 | `Shot automation aborted: live_tip_deadline_missed` |
| 00:41:39 | `TIP RESERVATION: disposition=reservation_canceled reason=live_tip_deadline_missed physical_epoch=32 shot_attempt=24 release_seq=0 schedule_token=0` |

**Critical observations:**
- `physical_median_ms` is stable at 310.2 (measurement fine).
- `anchor_pct` and `aim_offset_ms` are stable at 30.0 / 74.0 (aim geometry fine).
- `learnedOffset=0.0` even on EXCELLENT shots.
- Working shots land at `fill_at_rel=43-45` (well before/at tip).
- Late shots land at `fill_at_rel=61+` (well past tip).
- Aborts have `release_seq=0` — **release never scheduled**, killed at reservation cancel.
- Drift is progressive: EXCELLENT → LATE → aborted-entirely.

**Drift can't be in measurement or aim (both stable) — must be in release scheduling, latency estimator poisoning, or MeterDelayController subtly stealing lead.**

## 5. Prior work you must NOT redo

- Do NOT re-implement the meter-delay subsystem — it exists at `native_orion/src/MeterDelayController.{h,cpp}` and is wired.
- Do NOT weaken production integrity checks (`ORION_PRODUCTION_BUILD`, Ed25519 manifest verify, SHA-256 file check).
- Do NOT rename customer-facing "Venice" identifiers OR internal `orion-*` slugs (memory: [[venice-rebrand]]).
- Do NOT change default `AlwaysOn` / 250 ms / `meterDelayEnabled=true` — fix mechanism, not defaults.
- Do NOT touch `OrionOwner.exe` / `OrionStaff.exe` — internal admin binaries, correctly excluded from customer manifest.
- Do NOT `commit`, `reset --hard`, `checkout --`, or `clean -fd` anything — every source file listed in `git status` is intended in-flight work.

## 6. Hard constraints from user (verbatim)

- **"FABLE 5 ONLY for the workflow"** — use `model: 'fable'` on every subagent spawn (including verifiers).
- **"agents implement the changes and debug heavy"** — do not manually apply patches; orchestrate agents.
- **"ping me when it's ready again"** — one ping at end, only when all four bugs are fixed AND installer is rebuilt AND verified.
- Ship-blockers (user's word): tip timing, meter delay, aborts, lates. All four must resolve before ping.

## 7. Environment / build reference

- Qt: `C:\Users\aaron\Qt\6.8.0\msvc2022_64\bin` (prepend to `$env:PATH` before running tests).
- Native build (production): `cmake --build native_orion/build_prod_codex --config Release --target OrionNative`
- Native build (test target): `cmake --build native_orion/build_verify --config Release --target OrionNativeTests`
- Native test runner: `native_orion/build_verify/Release/OrionNativeTests.exe` (must stay at 477 pass / 0 fail / 5 skip).
- Standalone meter-delay tests: `native_orion/build_verify/Release/OrionMeterDelayTests.exe` (31 pass).
- Python tests: `python -m pytest tests/ -q` (must stay at 1437 pass, 18 skip, 6 xfail, 1 xpass).
- Sidecar build: `scripts/build_orion_sidecar.ps1` (Nuitka; produces `build/sidecar/autogreen_sidecar.dist/OrionSidecar.exe`).
- Manifest package + sign: `python tools/package_orion_release.py --build-dir build_prod_codex/Release --signing-key codesigning/venice_update_signing.pem --skip-archive`
- Manifest signing key fetch (needs AWS creds): `scripts/fetch_signing_key.ps1` (writes `codesigning/venice_update_signing.pem`, gitignored).
- Installer compile: `& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer/orion.iss` → `installer/Output/VeniceSetup-1.0.0.exe`.
- Log directory (installed): `%LOCALAPPDATA%\Venice\logs\` (most likely).
- Replay framedump harness for offline detector validation: memory [[replay-framedump-harness]].
- Launcher **must** be started via the launcher path — never directly (memory [[launcher-is-mandatory]]).

## 8. Where the four bugs likely live

### Bug 1: `live_tip_deadline_missed` drift → abort (ABSOLUTE PRIORITY)
- Search: `live_tip_deadline_missed`, `TIP RESERVATION`, `Shot automation aborted`, `deadlineMissed` in `native_orion/src/`
- Suspects:
  - Latency estimator poisoning after N shots (own inputs measured with meter-delay ramp bleed?)
  - MeterDelayController's applied delay quietly adding to release scheduling twice
  - `PeriodicSecurityEvaluator` firing in the shot path and stealing timer budget
  - `learnedOffset=0.0` on EXCELLENT is suspicious — the learner might not be updating from correct rungs
  - Tempo commit-nack fix (Round 2) that made confirm(false) a no-op may leave state that shifts scheduling
- Read: `native_orion/src/AutomationEngine.{h,cpp}` (release scheduling), `latency_estimator.py`, `models/latency_factory_prior.json`, `learning.json`, `ShotReleasePolicy.h`
- Validate offline with the framedump harness before rebuilding.
- Cross-check: does the pattern reproduce with `meterDelayEnabled=false`? If NO, delay is the cause; if YES, it's the shot pipeline. Try both in the fix agent's diagnosis before applying.

### Bug 2: Connect fails 5-10 times with pad plugged in
- Round 3 introduced `hasRecentRawInput()` predicate gating Connect. Check: does `lastRawInputMs_` only update on state CHANGE, not on every report? An idle-plugged pad producing "no button" reports may never satisfy the 3000 ms window.
- File: `native_orion/src/OrionAppController.cpp` around lines 6080, 6802 (Connect gates), plus wherever `lastRawInputMs_` is assigned. Grep for `lastRawInputMs_` and `rawInputPresent_`.
- Fix likely: update `lastRawInputMs_` on *any* HID input report arrival (even neutral state), OR extend window to 30 s cold-launch fallback, OR add "pad enumerated at least 3 seconds AND alive" as an alternate satisfaction path.

### Bug 3: Meter delay toggle inoperative
- QML: `native_orion/qml/components/MeterConfigPanel.qml` — check whether toggle emits a signal / binds to a property that reaches C++.
- C++ slot: search for `setMeterDelayEnabled`, `meterDelayEnabledChanged` in `native_orion/src/OrionAppController.{h,cpp}`.
- Possible cause: Round 2's QML slider `onMoved: if (!pressed)` idiom applied to a toggle switch will break it (Switch doesn't have `pressed`). Verify the toggle is not a Slider.
- Also check `AppConfig::setMeterDelayEnabled` path saves + emits change.

### Bug 4: Copy-pasteable Activity log
- QML: search for the Activity view component. Likely a `ListView` with `Text` delegate — swap for `TextArea { readOnly: true; selectByMouse: true; selectByKeyboard: true }` OR add a "Copy all" button that concatenates the rolling buffer to `Clipboard.setText(...)`.
- Look at: `native_orion/qml/pages/DashboardPage.qml`, `native_orion/qml/components/*Hud.qml`, and any file matching `Activity` or `Log`.

## 9. Task list currently open

```
#15 [in_progress] HUNT + FIX: live_tip_deadline_missed drift-to-abort
#16 [in_progress] FIX: Connect fails 5-10 times with pad plugged in
#17 [in_progress] FIX: Meter delay toggle inoperative in UI
#18 [in_progress] Make Activity log copy-pasteable
#19 [pending]     Post-fix rebuild + repackage installer  (blocked by 15,16,17,18)
#20 [pending]     Ping user for main-machine retest       (blocked by 19)
```

## 10. Handoff checklist (do these in order)

1. Read the four bug sections above + the log evidence table.
2. Spawn **four parallel Fable-5 subagents**, one per bug, with `subagent_type: 'general-purpose'` and `model: 'fable'`. Each brief:
   - Full bug description + user's ship-blocker priority language
   - Concrete file/line/grep pointers from section 8
   - Rebuild + rerun 477 native / 31 meter / 1437 python tests before returning
   - No commits, no resets, no default changes
3. When all four return green, spawn a Fable-5 **adversarial reviewer** on the combined diff (findings from prior review pattern worked well: HIGH/MEDIUM/LOW with `file:line` + failure scenario). Apply any HIGH/MEDIUM finds via another Fable-5 patch agent.
4. Rebuild sidecar (Nuitka) + launcher (Release) + regenerate signed manifest with `--customer` default + Inno Setup → new `VeniceSetup-1.0.0.exe`.
5. Silent-install smoke: verify manifest integrity via `tools/verify_release_integrity.py`; must find zero missing entries.
6. Ping user with: install path, one-line summary per bug fixed, any caveats.

## 11. Relevant persistent memory (already in `MEMORY.md` — read the linked files if you need depth)

- [[venice-rebrand]] — customer-facing name only; keep internal slugs.
- [[launcher-is-mandatory]] — never launch `OrionNative.exe` directly.
- [[force-killing-orion-corrupts-settings]] — mid-write kill resets `actuation_lead_ms` to 0 → factory-prior firing looks like a regression.
- [[replay-framedump-harness]] — offline detector validation without live rig.
- [[timing-ceiling-reached-stop-engineering]] — background context; user has since said "getting closer" so incremental work continues.
- [[green-window-shrinks-aim-high-is-correct]] — aiming at tip is deliberate; do NOT re-implement centring.
- [[cleanup-load-bearing-traps]] — files that look dead but break the build if removed.
- [[verify-claimed-fixes-not-just-tests]] — passing tests ≠ shipped fix; re-verify each claim by reading the code before committing/claiming.

## 12. If you get stuck

- Root-cause first; do not paper over. User's memory [[verify-claimed-fixes-not-just-tests]] applies.
- If two hypotheses fit, pick the one closer to the log evidence (drift is progressive, aim is stable, `release_seq=0` on aborts).
- If you must ask the user, ask ONE targeted question with a concrete alternative — do not open-ended "what should I do next".

---

**Bottom line for the next agent:** Four parallel Fable-5 fixes → adversarial review → rebuild + repackage → ping. Everything above is context; the four bugs in section 3 with the log evidence in section 4 and the pointers in section 8 are what you actually do.
