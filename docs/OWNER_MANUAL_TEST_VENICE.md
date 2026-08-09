# Meter delay — VeniceNet (Path A) manual test on this rig (2026-08-08)

Everything is already built. No installer. Three files matter:

- `scripts\owner_venice_setup.ps1` — one-time service registration (admin)
- `scripts\owner_verify_venice.ps1` — read-only "is the delay on the wire" check
- this doc

Run every command in this doc from the repo root — open PowerShell and
`cd C:\Users\aaron\Desktop\NexusVision` first — so the relative paths below
resolve.

## 0. Which path am I on?

Two ways to run the same test. **Exactly one packet-bridge service may be
registered at a time** — both bind TCP 47291 and cannot coexist.

- **Path A (VeniceNet) = this doc. Primary.** The wave-2A C++ service
  (`VeniceNetSvc.exe`, registered as `VeniceNetSvc`) — no Python at runtime —
  with meter-delay actuation routed through `VeniceNet.dll`, which the app
  LoadLibrary's and drives over loopback TCP.
- **Path B (fallback) = `docs\OWNER_MANUAL_TEST.md`.** The legacy Python
  service (`NexusVisionSvc.exe`, a frozen `nexus_svc.py`, registered as
  `NexusVisionSvc`). Same wire protocol, same test recipe. Use it if Path A
  hits any issue.

Switching paths = roll back whichever service is currently registered, then
run the other path's setup. Rollback commands (elevated, one line at a time,
wait for STOPPED between stop and delete):

- Path A registered: `sc.exe stop VeniceNetSvc` … `sc.exe delete VeniceNetSvc`
  (full steps in section 8 below)
- Path B registered: `sc.exe stop NexusVisionSvc` … `sc.exe delete NexusVisionSvc`
  (full steps in `docs\OWNER_MANUAL_TEST.md`, section 8)

Both setup scripts refuse to run while the other path's service is registered
and print the exact rollback commands.

Binaries in place: `native_orion\build\Release\OrionNative.exe` (the setup
script verifies it carries the VeniceNet wiring — the `venicenet_init` import
string — and fail-closes with a rebuild instruction if it does not),
`native_orion\build\venicenet_service\Release\VeniceNetSvc.exe`
(WinDivert64.dll + .sys verified beside it), and `VeniceNet.dll` next to
OrionNative.exe (staged by the DLL target's own POST_BUILD copy).

## 1. One-time setup

Open Windows Terminal or PowerShell **as Administrator**. Then run:

```
powershell -ExecutionPolicy Bypass -File "C:\Users\aaron\Desktop\NexusVision\scripts\owner_venice_setup.ps1"
```

(Do not bother with right-click on the file — this rig has no "Run as
administrator" context-menu entry for `.ps1` files.)

It prints its plan, one `Y` to confirm. It registers `VeniceNetSvc`
(demand-start, LocalSystem, `--arm-meter-delay` in binPath, **plus**
`ORION_METER_DELAY_ARMED=1` in the service's per-service Environment registry
value — the C++ service reads the arm switch from its environment in service
mode, so the registry value is the actual arm — and the same start rights the
installer grants). It does **not** start the service or load the driver. Skip
if already done — re-running is safe, it offers a clean re-register.

## 2. Before you test — start the service, then fix the lead

**Start the service first, before launching Orion** (this is different from
Path B, where the app starts the service itself):

```
sc.exe start VeniceNetSvc
```

Why the order matters: the app's auto-start only knows the legacy service
name. If nothing owns TCP 47291 when the app comes up, the app spawns the
Python sniff-only debug bridge, which then **blocks VeniceNetSvc from binding
the port** — the batch would silently run the wrong architecture, disarmed.
Starting the service first makes the app find the port occupied and simply
connect to it.

Then the lead: `actuation_lead_ms` is still **320** from the 08-08 session.
That is past the tip runway: every shot aborts as `unschedulable_lead` no
matter what meter delay does, and the batch is garbage before it starts.

Launch via `Launch Orion.local.bat` (never OrionNative.exe directly). On the
Remote Play page, find the **Shot Lead card** — its own card, directly below
the Meter Delay panel — and set it back to **280–300**. Do **not** touch the
Tip Timing card: that is a different control with the opposite sign
convention.

Secondary note: if you dragged Tip Timing itself to the floor last session,
reset that first (on the Tip Timing card), then set Shot Lead as above.

## 3. The A/B/A test

Three batches, same court, same shot type, same jumper, **minimum 30 shots
each**:

| Batch | Meter Delay toggle |
|---|---|
| 1 | OFF |
| 2 | ON |
| 3 | OFF |

Note: `meter_delay_enabled` defaults **ON**, so check the toggle actually says
Off before batch 1.

Grade by hand-counting the game's own TIMING banner. The in-app self-grader
emits the LATE-66 artifact — do not credit its verdicts.

## 4. Verify engagement (batch 2)

The verify script prints two labelled tables. Run it **twice**:

```
powershell -ExecutionPolicy Bypass -File scripts\owner_verify_venice.ps1
```

**First run — right after enabling Meter Delay, before shooting.** Only the
**PRE-BATCH** table must pass: `VeniceNetSvc` RUNNING, legacy `NexusVisionSvc`
absent, WinDivert driver RUNNING, `VeniceNet.dll` staged, the service's own
log saying `INBOUND METER DELAY ARMED`, `Meter delay service state: ARMED` in
the app log, and `Meter delay service echo: intercept ACTIVE … [VeniceNet]`
(the `[VeniceNet]` tag is emitted only by the DLL path — it is the proof the
new architecture, not the old bridge, is driving). Any PRE-BATCH FAIL = the
delay is not engaging — fix it first (section 7). Shooting on a PRE-BATCH
FAIL makes the batch worthless; this is the only case where "batch worthless"
applies.

**Second run — after at least 5 shots.** Now the **MID-BATCH** table must
also pass: `Meter delay condition: settled=1`, a recent `Outcome identity:`
line with `armed_source=`, and a `vel=` reading under 0.15 %/ms (baseline
~0.226). MID-BATCH FAILs on the first run are expected — those signals need
shot traffic in the log. If they still fail after shots have gone up, that is
a batch-collection issue: re-run the verify after a few more shots before
condemning the batch.

## 5. After each batch — per-source attribution

```
python tools\timing\armed_source_report.py logs\orion_native.log --list
```

(The dev build under test writes its log to `logs\orion_native.log` in the
repo root — not to the installed app's `%LOCALAPPDATA%` location.)

then re-run with `--hand GGLGE...` (your banner count, in shot order) for the
batch's window (`--session` / `--window` to slice; `--max-lead-ms 300` guards
against contaminated-lead rows). Hypothesis to check: the `armed_source=phase`
share goes **up** in batch 2.

## 6. What "worked" looks like

- Hit rate up in batch 2 vs batches 1 and 3.
- `vel=` measurably down when the delay is on (baseline ~0.226 %/ms; engaged
  should read below ~0.15).
- Phase-confident share (`armed_source=phase`) higher in batch 2.
- Every ACTIVE echo line carries the `[VeniceNet]` tag — the batch ran on the
  new architecture end to end.

If hit rate is up but `vel=` is unchanged, that is a batch break, not a delay
effect — don't credit it.

## 7. If the service won't start

From an elevated prompt: `sc.exe start VeniceNetSvc` and read the error code.
(Always type `sc.exe`, not `sc` — in PowerShell, `sc` is the Set-Content
alias and silently writes a file instead of talking to the SCM.)

Most likely cause: AV blocking the new binary or the WinDivert driver load.
`VeniceNetSvc.exe` is a freshly compiled, unsigned MSVC C++ exe that opens a
kernel driver — a brand-new AV signature, so a heuristic hit is expected, not
alarming. Add an AV exclusion for the whole folder
`native_orion\build\venicenet_service\Release\` (covers the exe,
`WinDivert64.dll` and `WinDivert64.sys` in one rule), then `sc.exe start`
again.

Second cause specific to Path A: **the port was taken first**. The service's
own log (`venicenet_svc.log` next to `VeniceNetSvc.exe`) saying
`Cannot bind 127.0.0.1:47291` means another bridge owned the port when it
started — usually the app's Python sniff-only debug fallback because Orion was
launched before the service (section 2 order), or a legacy service that was
never rolled back. Close Orion, kill any stray `python`/`NexusVisionSvc`
process, `sc.exe start VeniceNetSvc`, then relaunch Orion.

If the service runs but verify says the echo stays `intercept inactive` with
the card at "Waiting for a live game session (Court IP unknown)": the delay
only works when the console's traffic routes **through this PC** (the ICS
setup). Check ICS is up and the GW-Lab VM is not holding 192.168.137.1 —
disconnect its PS5-Internal NIC if it is.

## 8. Rollback

Elevated prompt, one line at a time:

```
sc.exe stop VeniceNetSvc
sc.exe query VeniceNetSvc
sc.exe delete VeniceNetSvc
```

Repeat the middle query until `STATE` shows `STOPPED` before deleting —
deleting a still-stopping service only marks it for deletion and blocks the
next registration with error 1072.

That is the machine back to its pre-test state: the driver unloads when the
service stops (it is never boot-loaded), `sc.exe delete` removes the service
key including the per-service Environment arm value, and setup changed
nothing else — no files under `%LOCALAPPDATA%\NexusVision\Orion Native\` were
touched. (If setup staged the wave-2B app binaries, the replaced originals
remain in `native_orion\build\pre_wave2b_backup` — copy them back only if you
also want to undo the app update.)

To switch to Path B afterwards, run `scripts\owner_manual_test_setup.ps1` per
`docs\OWNER_MANUAL_TEST.md`.
