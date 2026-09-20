# Venice / Orion — Repo-Wide Bug Audit Brief

**Prepared 2026-08-11. Ship date: 2026-08-28 (early access). 17 days.**

You are auditing a shipping product, not a greenfield codebase. The bar is **"will this hurt a
paying customer in the next 17 days"**, not "is this the nicest way to write it." Read the
"Known-intentional" and "Verification traps" sections BEFORE filing anything — most plausible-
looking findings in this repo are deliberate, and the traps below have already produced several
confidently-wrong conclusions.

---

## 1. What the product is

Venice (internally "Orion") is a shot-timing assistant for NBA 2K26 on PS5. Pipeline:

```
PS5 --HDMI--> Elgato HD60 X --> Python sidecar (OpenCV meter reader)
                                     |
PS5 <--input-- Chiaki fork <-- C++ Qt app (AutomationEngine: predicts the meter tip,
                                            schedules the release)
```

- **`native_orion/`** — C++/Qt6 app. `AutomationEngine` is the brain (timing, prediction,
  grading, learning). ~30k lines in one .cpp; this is where correctness matters most.
- **`remote_play_orchestrator.py`** — Python sidecar. Capture, detection, frame contracts.
- **`simple_meter_reader.py`** — the LIVE detector (colour thresholding, ~0.6 ms). Replaced an
  older 5700-LOC model chain.
- **`controller_remap.py`** — input remap / shot-trigger classification.
- **`native_orion/venicenet_service/`** — `VeniceNetSvc.exe`, a LocalSystem Windows service doing
  WinDivert packet interception for the "meter delay" feature. IPC on 127.0.0.1:47291.
- **Deployed Chiaki fork lives in a SIBLING directory `chiaki-ng-src` (branch `orion`)**, NOT in
  `vendor/chiaki-orion`. Do not audit the vendored copy and assume it ships.

---

## 2. Hard safety rules — violating these damages the owner's rig

1. **NEVER modify `run_orion.local.ps1`.** It contains `ORION_LICENSE_KEY`, is gitignored, and is
   read-only by policy. You may READ it. Do not echo secrets into your report.
2. **NEVER force-kill `OrionNative.exe`** (no `taskkill /F`, no `Stop-Process -Force`). A kill
   mid-write zeroes `actuation_lead_ms` in settings and silently ruins the owner's tuning. Ask
   for it to be closed via the window X.
3. **Never launch `OrionNative.exe` directly** — the launcher sets ~19 environment keys and
   elevates for WinDivert. Direct launch produces a subtly broken session.
4. Do not start/stop `VeniceNetSvc` while the app is running unless you say so explicitly.
5. Treat `settings.json` (repo root) and `learning.json` as LIVE owner state. Back up before
   touching. Both are runtime data; `settings.json` is gitignored.

---

## 3. Verification traps — these have already caused wrong conclusions

**Read this section twice. Every item below burned someone in the last 48 hours.**

### Build
- **Stale test binaries.** A failed compile leaves the previous `.exe` in place and the suite
  prints "all checks passed" from the OLD binary. **Always `Remove-Item` the test exe first**,
  confirm it's gone, then build. If it's absent and the suite runs, the pass is real.
- **Partial target builds mislead.** `cmake --build build --config Release --target X` can succeed
  while other targets are broken. For any claim about suite health, build ALL targets
  (`cmake --build build --config Release`) then `ctest --test-dir build -C Release`.
- **The running app locks DLLs.** If Venice is open you get
  `LNK1104: cannot open file ...\Release\AutomationCore.dll`. That is a lock, not a code error.
- **Timestamps do not prove a change is in a binary.** A change to a `.cpp` that adds no exports
  will not relink dependents. To actually verify, search the binary for a distinctive constant
  (e.g. `struct.pack('<d', 1928.0)`) rather than reasoning from mtimes.
- Test suite names are prefixed: `OrionNativeTests` (this holds AutomationEngineTests),
  `OrionVeniceNetServiceTests`, etc. 16 ctest suites total; all pass as of `e1f1e2b`.

### Log analysis
- **`logs/orion_native.log` is APPEND-ONLY across restarts and currently holds ~27 sessions.**
  Both `seq` and `physical_epoch` **restart at 1 every session.** Joining on a bare `seq` welds
  shot #1 of session 7 onto shot #1 of session 2. This produced a bogus "57.6% collapse rate."
  Namespace every key by session. A working analyser exists — ask for `pa_report.py`.
- **Substring matching inflates counts.** Grepping `abort` matches the field `aborted=0` inside
  `preview_stats` lines — that turned 4 real aborts into "76". Same for `error` vs `ack_error=0`.
  Anchor your patterns.
- Encoding: read with `utf-8-sig`, `errors='replace'`. On Windows, set
  `sys.stdout.reconfigure(encoding='utf-8')` or printing log excerpts crashes on cp1252.
- **The live dev build writes to the REPO ROOT** (`./settings.json`, `./logs/`), not
  `%LOCALAPPDATA%\NexusVision\Orion Native\`. The LOCALAPPDATA copy is a stale installed build
  from 2026-08-08. Auditing the wrong one wastes an hour.

### Statistics
- Small-n conclusions have repeatedly been wrong here. Go-To was "broken" for weeks on n=10 and
  came back 8/9 good in a clean session. State your n. If n < 20, say so in the finding.
- Do not derive performance ceilings from quantization arithmetic. That was tried, produced a
  confident "60 Hz = 34%" result, and was **retracted as circular**.

---

## 4. Known-intentional — do NOT report these as bugs

- **Default-OFF feature flags.** Many exist (`pressAnchoredPredictorEnabled`,
  `meterSettleAllowSmoothMotion`, `tipPhaseTypeTrimEnabled`, ...). They are staged rollouts, not
  dead code. Report one only if the flag is *unreachable* (no settings path, no UI, no env var).
- **Fail-closed behaviour.** Aborting a shot on missing evidence is the design. "The bot didn't
  fire" is correct when authority/proof is absent. Only report it if the gate is unreachable on
  real data (that HAS happened — thresholds unmeetable live, feature off forever).
- **ASCII-only source in `RemotePlaySession.*`** — documented at `RemotePlaySession.h:140`
  (no BOM, no `/utf-8`). Em-dashes and smart quotes there are real bugs; plain ASCII is correct.
- **Load-bearing "dead" files.** Several files look unused but break the build or launch if
  removed. Do not recommend deletions without proving it with a build.
- **`ORION_PRODUCTION_BUILD` gating.** Dev builds intentionally auto-re-sign `settings.json`
  (`SecurityManager.cpp:389`); production hard-locks. Both branches are deliberate.
- **The 1280x720 detector normalization** (`remote_play_orchestrator.py:3577`) is a deliberate
  source-independence contract, not laziness — see the comment at `:3573`. It IS a known quality
  limitation (we capture 1080p and downscale) but it is a tracked design decision, not a defect.
- **Internal `orion-*` slugs/resources are NOT renamed to Venice.** Venice is customer-facing
  only. Renaming them breaks things.

---

## 5. Priority areas

Ranked. Depth beats breadth — a real bug in #1 is worth more than ten style notes in #6.

### P1 — `native_orion/src/AutomationEngine.cpp` (the shot path)
The highest-consequence file. Focus on:
- Lifetime/ownership across the GUI thread and the precise-fire worker thread.
- Epoch/generation guards. The pattern `xEpoch_ == shot_.physicalShotEpoch` appears throughout to
  stop a stale async result acting on a newer shot. **Look for a consumer that forgot the check.**
  One such bug (a frozen authority lease) silently killed shots and took days to find.
- Timer/deadline races: `meterCapDeadlineMs_`, `postReleaseWindowMs`, `QTimer::singleShot`
  fallbacks that may fire after teardown or after a newer connect superseded them.
- Anything that can make a scheduled release fire twice, or fire after abort.

### P2 — `venicenet_service/` (runs as LocalSystem)
Elevated privilege + network input parsing. Highest security exposure in the product.
- IPC input validation on 127.0.0.1:47291 (`IpcServer.cpp`, `Json.h`). A depth limit and a
  double-free were already fixed; look for siblings.
- Socket/thread/handle lifetime in `handleClient`. **A per-connection thread/handle leak is a
  known-open finding (#87) — confirm and characterise it rather than rediscovering it.**
- `MeterDelayIntercept.cpp`: WinDivert packet handling, and a latched `DriverState::Error` that
  may never clear.
- **Known-open (#87): port-range mismatch — detector uses 30099, intercept uses 30020.** This is
  real but NOT a typo; `nexus_svc.py` used 30020 too. Widening is a behavioural change. Assess,
  don't "fix".

### P3 — Capture / detection path
- `remote_play_orchestrator.py` — frame contracts, the capture-API selection (MSMF vs DSHOW), and
  the **one-way `_capture_warm_cache_revoked` latch (#47, SHIP BLOCKER).** Around `:3074-3089` an
  `already_revoked` branch returns False unconditionally and never re-tests, so the success path
  at `:3091` is unreachable and the bot cannot recover without a full restart. A plain-language
  warning shipped 2026-08-10; **the real fix is still open.** Verify my reading and propose a fix
  (likely: split "poisoned" from "route-invalid" into separate flags).
- `simple_meter_reader.py` — the live detector. ROI tracking, band/colour logic.

### P4 — `controller_remap.py` + input
- **Known-open (#88): Square tap is still swallowed** when tempo remap is on, so the user can't
  steal or use menus. The IDLE-strip half was fixed (`_process_idle` else-branch); the tap case
  remains. A `shot_trigger_mode == 'passthrough'` path exists but is unreachable because
  `RemotePlaySession.cpp:742` hardcodes `"button"`.
- **Known-open (#42): D-Pad Up is double-booked** — it both toggles `meterDelayBypassOnDefense`
  and is the dormant Defense Mode trigger.

### P5 — Settings / migration / updater
- `AppConfig.cpp` load/save round-tripping, clamp ranges vs defaults. **A real bug of this shape
  already shipped**: `noMeterBaseOffsetMs` default 83 against a clamp of [-40, 40]. Look for other
  defaults outside their own clamp.
- Settings versioning/migration, updater relaunch paths.

### P6 — Build / packaging / installer
Installer repackage is scheduled LAST (by Aug 24) — flag issues, don't fix.
- **Known hazard:** the app sc-starts only `NexusVisionSvc` on some paths while wave-3 installs
  register `VeniceNetSvc`, so Meter Delay is dead on a fresh install until both names are tried.
  Believed addressed (#64) — **verify it actually covers every call site.**

---

## 6. Already-known open items — verify, don't rediscover

Confirm or refute these; do not spend time re-finding them. Full list is in the task tracker.

| # | Item |
|---|---|
| 47 | Capture-API one-way revocation bricks the bot (**ship blocker**, real fix open) |
| 87 | VeniceNetSvc: thread/handle leak, latched DriverState::Error, per-packet alloc, port mismatch |
| 88 | Square tap swallowed with tempo remap on |
| 42 | D-Pad Up double-booked |
| 43 | `scheduledFireConfirmCompletesRelease` clock tolerance too tight (test flake) |
| 13 | Synchronous `updateSecurityStatus()` callers |
| 49 | Possession detector for bypass-on-defense |
| 89 | Remote Play connect ~3.1-3.4 s; the 2.5 s `kStreamPromoteFallbackMs` is COSMETIC, the real cost is sidecar Chiaki bring-up |
| 84 | No Dip inconsistency |

**Also unexplained, worth a fresh pair of eyes:** in a clean 61-landing session on 2026-08-11,
7 shots graded EARLY vs 2 LATE, with errors clustered at -56.8, -59.7, -59.3, -56.8, -55.8 ms.
Five samples inside a 4 ms band is not jitter — something applies a systematic ~-57 ms offset on a
subset of shots. 5 of the 7 came from the `phase` tip source. Independently corroborated by
`settled_fill` (EARLY median 87.80 vs EXCELLENT 94.24). **Cause unknown. High value if you find it.**

---

## 7. Severity rubric

- **SHIP BLOCKER** — data loss, security exposure, bricks the bot until restart, or makes the core
  feature unusable for a paying customer. Must be fixed before Aug 28.
- **HIGH** — wrong behaviour a user will hit in normal use, with no workaround.
- **MEDIUM** — wrong behaviour that is rare, recoverable, or has a workaround.
- **LOW / POLISH** — cosmetic, or correctness that cannot be observed by a user.
- **NOT A BUG** — say so explicitly. A well-argued "this looks wrong but is intentional because X"
  is a genuinely useful finding and saves the next person the same investigation.

---

## 8. Output format

For each finding:

```
### [SEVERITY] Short title
file:line
WHAT:      the defect, one or two sentences.
TRIGGER:   concrete inputs/state that reach it. If you cannot construct one, say so and
           downgrade to SUSPECTED.
IMPACT:    what the user experiences.
EVIDENCE:  the code path, or a log excerpt, or a test you ran. Not "it looks like".
CONFIDENCE: CONFIRMED (proved it) | PLAUSIBLE (read the code, didn't run it)
FIX:       proposed change, and whether it is behaviour-changing.
```

Rules:
- **Separate CONFIRMED from PLAUSIBLE.** Do not present a code-reading as a proven bug.
- If you assert something is broken at runtime, try to reproduce or show the log line.
- Do not fix anything. Report only — the owner decides what lands this close to ship.
- Explicitly list what you looked at and found CLEAN. Negative results have value here.
- If a finding contradicts something in this brief, say so loudly. This brief is my current
  understanding and parts of it have been wrong before.
