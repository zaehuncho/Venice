# Meter delay — manual test on this rig (2026-08-08)

> **Recommended primary path:** `docs\OWNER_MANUAL_TEST_VENICE.md` (the new
> VeniceNet C++ service + DLL). This doc is the **fallback** (legacy Python
> service) — same wire, same recipe; exactly one of the two services may be
> registered at a time.

Everything is already built. No installer. Three files matter:

- `scripts\owner_manual_test_setup.ps1` — one-time service registration (admin)
- `scripts\owner_verify_meter_delay.ps1` — read-only "is the delay on the wire" check
- this doc

Run every command in this doc from the repo root — open PowerShell and
`cd C:\Users\aaron\Desktop\NexusVision` first — so the relative paths below
resolve.

Binaries in place: `native_orion\build\Release\OrionNative.exe` (fresh build, all
suites green) and `build\service\nexus_svc.dist\NexusVisionSvc.exe` (Nuitka,
WinDivert pair verified beside it — the DLL was missing from the dist and has
been fixed; the setup script re-checks it).

## 1. One-time setup

Open Windows Terminal or PowerShell **as Administrator**. Then run:

```
powershell -ExecutionPolicy Bypass -File "C:\Users\aaron\Desktop\NexusVision\scripts\owner_manual_test_setup.ps1"
```

(Do not bother with right-click on the file — this rig has no "Run as
administrator" context-menu entry for `.ps1` files.)

It prints its plan, one `Y` to confirm. It registers `NexusVisionSvc`
(demand-start, LocalSystem, `--arm-meter-delay` in binPath, same start rights
the installer grants) pointing at the dev build. It does **not** start the
service or load the driver. Skip if already done — re-running is safe, it
offers a clean re-register.

## 2. Before you test — fix the lead

`actuation_lead_ms` is still **320** from the 08-08 session. That is past the
tip runway: every shot aborts as `unschedulable_lead` no matter what meter
delay does, and the batch is garbage before it starts.

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
powershell -ExecutionPolicy Bypass -File scripts\owner_verify_meter_delay.ps1
```

**First run — right after enabling Meter Delay, before shooting.** Only the
**PRE-BATCH** table must pass: service RUNNING, WinDivert driver RUNNING,
`Meter delay service state: ARMED`, `Meter delay service echo: intercept
ACTIVE` (= `meter_delay_active=true` from the service). Any PRE-BATCH FAIL =
the delay is not engaging — fix it first (section 7). Shooting on a
PRE-BATCH FAIL makes the batch worthless; this is the only case where "batch
worthless" applies.

**Second run — after at least 5 shots.** Now the **MID-BATCH** table must
also pass: `Meter delay condition: settled=1` (= `meter_delay_settled=true`),
a recent `Outcome identity:` line with `armed_source=`, and a `vel=` reading
under 0.15 %/ms (baseline ~0.226). MID-BATCH FAILs on the first run are
expected — those signals need shot traffic in the log. If they still fail
after shots have gone up, that is a batch-collection issue: re-run the verify
after a few more shots before condemning the batch.

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

If hit rate is up but `vel=` is unchanged, that is a batch break, not a delay
effect — don't credit it.

## 7. If the service won't start

From an elevated prompt: `sc.exe start NexusVisionSvc` and read the error
code. (Always type `sc.exe`, not `sc` — in PowerShell, `sc` is the
Set-Content alias and silently writes a file instead of talking to the SCM.)
Most likely cause: AV blocking the WinDivert driver load. Add AV exclusions
for `build\service\nexus_svc.dist\NexusVisionSvc.exe` and
`build\service\nexus_svc.dist\pydivert\windivert_dll\WinDivert64.sys`
(excluding the whole `nexus_svc.dist` folder is simplest), then
`sc.exe start` again. Note the exe is unsigned and opens a kernel driver — a
heuristic hit is expected, not alarming.

If the service runs but verify says the echo stays `intercept inactive` with
the card at "Waiting for a live game session (Court IP unknown)": the delay
only works when the console's traffic routes **through this PC** (the ICS
setup). Check ICS is up and the GW-Lab VM is not holding 192.168.137.1 —
disconnect its PS5-Internal NIC if it is.

## 8. Rollback

Elevated prompt, one line at a time:

```
sc.exe stop NexusVisionSvc
sc.exe delete NexusVisionSvc
```

That is the machine back to its pre-test state: the driver unloads when the
service stops (it is never boot-loaded), and setup changed nothing else — no
files under `%LOCALAPPDATA%\NexusVision\Orion Native\` were touched.
