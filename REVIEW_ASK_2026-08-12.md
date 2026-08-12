# Adversarial review ask — 2026-08-12

Repo: `C:\Users\aaron\Desktop\NexusVision`, branch `fix/timing-input-and-remoteplay-blockers`.
Review range: **`04a60c2..3786a5d`** (6 commits) plus two live-settings changes described below.

This is an NBA 2K shot-timing bot shipping **2026-08-28**. It reads the shot meter from an HDMI
capture card and issues a controller release through a named pipe to a PS5. Timing errors of
~20 ms are the difference between a make and a miss, so shot-path changes are the highest-risk
category in the codebase.

**I want you to try to break these changes, not confirm them.** Where you agree, say so briefly
and move on. Spend your effort on what is wrong, unproven, or fragile.

---

## Rules

- **READ-ONLY.** Do not edit, build, run, or commit. Return findings as a document.
- **Never modify** `run_orion.local.ps1` (contains a licence key), `settings.json`, or
  `learning.json` — these are live owner state.
- The app may be running. Do not start or stop `VeniceNetSvc`, and never `taskkill /F` the app
  (a kill mid-write zeroes `actuation_lead_ms`).
- Cite `file:line` for every claim. If you cannot point at code, mark it as speculation.
- If a claim below is simply correct, say "confirmed" and give the line that proves it. I would
  rather have five verified findings than twenty guesses.

---

## Part 1 — the risky change: `021f6af` Square passthrough (#88)

**Problem it solves.** The Tempo remap converts a held Square into a right-stick gather/flick and
strips `XINPUT_GAMEPAD_X` from the output. It does this at **21 sites across 12 functions**. That
is correct for shooting, but it also means Square's other job — the steal — is unreachable while
Tempo is on.

**What I did.** `process()` is now a thin wrapper:

```cpp
ControllerState AutomationEngine::process(const ControllerState& physical)
{
    ControllerState output = processInternal(physical);
    applySquarePassthrough(output, physical);
    return output;
}
```

`applySquarePassthrough` (same file, just above) does: if `squarePassthroughEnabled` **and**
`tempoRemapEnabled` **and** the configured stick-click bit (default R3) is held in `physical`, then
set `XINPUT_GAMEPAD_X` on the output and clear the stick-click bit.

The rest of the old `process()` body was renamed `processInternal` unchanged.

**Questions I actually want answered:**

1. **Is post-hoc injection safe?** The engine's shot-intent detection reads `physical`, so R3
   should never arm the bot — I assert this and a test covers `shotsAttempted() == 0`. But does
   anything else consume the **output** and interpret `XINPUT_GAMEPAD_X` as "the bot is shooting"?
   Candidates to check: the PRESSED overlay, release telemetry, `VirtualController`,
   `OrionInputClient`, the input hook, any output-feedback path. If the engine ever reads its own
   previous output as evidence, this injection is a latent false shot.

2. **Does consuming the R3 bit break anything?** I clear `XINPUT_GAMEPAD_RIGHT_THUMB` from the
   output so the game does not also see a stick press. Verify nothing downstream needs it. I
   checked that the engine reads but never acts on it (`WinMmButtonMapping.h:46-47`,
   `OrionAppController.cpp:478-479,528-529`) — confirm or refute.

3. **Did the rename miss a caller?** Every caller of `AutomationEngine::process` must now get the
   wrapper. Is there any call site — tests, tools, another module — that was calling the old body
   and now silently skips the passthrough, or worse, calls `processInternal` directly?

4. **Five return points.** `processInternal` returns at 5 places. I chose the wrapper specifically
   so early returns are covered. Confirm all 5 actually flow through the wrapper.

5. **Is R3 the right default?** L3 is turbo in NBA 2K (binding Square there would steal on every
   sprint), so I defaulted to R3. Is R3 genuinely unbound in NBA 2K 26 gameplay? If it has a
   function, say so — this is a gameplay question, not a code question, and I may be wrong.

6. **Config plumbing.** `squarePassthroughEnabled` / `squarePassthroughButton` were added to
   `RemapConfig` (AutomationEngine.h), `AppConfigData` (AppConfig.h), load (AppConfig.cpp ~1104),
   save (~475), and the engine copy (AutomationEngine.cpp ~1305). Anything missed — a UI binding,
   a profile export/import allowlist (`VeniceProfile.h`), a migration path?

---

## Part 2 — two changes to LIVE owner settings (no code)

I edited `settings.json` directly with the app closed, after backing it up.

**(a) `press_anchored_predictor_enabled: true -> false`.**
Evidence: joining `Outcome identity`'s `armed_source` to `Release landing`'s `peak_fill` in
`logs/orion_native.log`:

| armed_source | n | peak_fill p50 | fill_at_rel | sigma | peak<95 |
|---|---|---|---|---|---|
| phase | 114 | 98.6 | 38.9 | 15.5 | 5% |
| press_anchored | 4 | 87.7 | 20.3 | 58.4 | **100%** |

And a paired trace 4 seconds apart: `seq=136` promoted `source=phase command_eta=96.9 sigma=15.1`
→ released at fill 36.1, peak 97.4, EXCELLENT. `seq=137` promoted `source=press_anchored
command_eta=5.0 sigma=49.5` → released at fill 17.7, peak 80.3, EARLY, errorMs=-90. On the bad one
the phase source was still in `TIP PHASE IMMINENT HOLD` at fill 17.71 vs its 20.0 anchor.

`AppConfig.h:622-624` says "Phase B (THIS FLAG, default OFF)… DO NOT flip the default".

**Challenge this.** n=4 is thin. Is there an alternative explanation — e.g. press_anchored only
wins on shots that were already going to be bad, making it a symptom rather than a cause? Is
turning it off going to break the delay-starved bootstrap path
(`[ORION_PRESS_ANCHOR_BOOTSTRAP]`), which is supposed to be flag-independent? Verify that claim in
the engine, because I asserted it without reading the code path.

**(b) `meter_delay_lead_offset_ms: 155 -> 90`.**
With meter delay applied, total lead = base 290 + offset. `maxSchedulableTipLeadMs()` was 386, so
445 was unschedulable and **151 of 151 delayed shots aborted**. 90 puts the total at 380.
Confirm the ceiling arithmetic and whether 386 is itself correct.

---

## Part 3 — analytical conclusions to attack

These drive what we build next, so a wrong one is expensive. **All data is in
`logs/orion_native.log`** (11 MB; per-session, split on `Sidecar env keys`; note `seq` RESETS per
session, so never join across sessions on `seq` alone — I made that mistake once today).

**C1. The residual timing error is downstream of the press.** Delay-off, `armed_source=phase`,
n=421: under-timed shots (`peak_fill<95`) and on-target shots command at the same fill (39.0 vs
39.4) but differ in `travel_pp` (52.4 vs 59.4). Command spread 6.4pp; travel spread 9.7pp.
→ Conclusion: no predictor/reader/sub-pixel improvement can fix the residual.
**Attack:** is `travel_pp` independent of `fill_at_rel` by construction? Is
`peak = fill_at_rel + travel_pp` an identity that makes this circular? That is my main worry.

**C2. `peak_fill` is a valid early-fire instrument with delay OFF.** Because the meter stops where
the release registers, so peak IS the release point. **Attack:** is that true, or does the meter
continue past the release in some shot types?

**C3. The press-anchored predictor is not bound by the 386 ms ceiling in principle**, because it
anchors on the press (wall time) rather than in-video evidence, and `press_to_tip` is 638-1960 ms.
Every `disposition=rejected_missed` had `source=press_anchored` with `command_eta_ms` of -186 to
-314. **Attack:** is exempting it from `maxSchedulableTipLeadMs()` actually sound, or is the
ceiling load-bearing for a reason I have not found?

**C4. Things I claim are dead ends** — say if any deserves another look:
`ORION_CAPTURE_MJPG` is a no-op (driver returns YUY2 regardless); 1080p detection is null
(19.1% vs 22.0% at 6pp+, p=0.80 against a same-day control); `ORION_READER_SUBPIX_EDGE` is
correctly off (0.23 ms noise removed vs 1.88 ms bias introduced, per its own header comment).

**C5. A finding I already distrust — tell me if I am right to.** `meter_x >= 0.70` (right of
screen) correlates with wide green windows in 7/7 sessions (16% vs 38% at 6pp+). But a second
instrument, `logs/diagnostics/green_zone_labels/*.jsonl`, reports the OPPOSITE direction
(`win_width_pp` 7.8 right vs 12.7 left). Also every one of the 408 records in those files is
labelled `EARLY`, which looks like a degenerate/broken label field. Which instrument is wrong?

---

## Part 4 — free hunting

Today produced four bugs that were all the same shape: **a value computed and then discarded, or a
failure that reported success.** The truncated `Capture health` line (its ` SUSPECT` marker never
once reached the log in 8.6 MB), a lead offset 59 ms past a hard ceiling with no complaint, a fix
that landed in a layer that could not reach the console, and a settings flag silently overriding a
better predictor.

**Go looking for more of that shape.** Specifically:
- Diagnostics that compute a reason and then null it before logging (there is a known one at
  `simple_meter_reader.py:5678-5681` — B7/B8 veto a courtwide steal candidate before the log line
  sees it).
- Log lines whose informative tail is cut by `RemotePlaySession.cpp:2682` `.left(300)`.
- Config values that can exceed a hard limit without an error.
- Features gated ON in settings that the code comments say should be OFF.

Return findings ranked by **ship risk before Aug 28**, each with `file:line`, the concrete failure
scenario, and how you verified it. Mark anything you could not verify as unverified.
