# Venice launch red team, internal wave: Claude consolidated report (2026-09-22)

- **Sources:** nine read-only Opus lane reviews, `P1-*.md` … `P9-*.md` in this folder (P7 is a short fix list; its full write-up was stopped by a safety classifier).
- **Carryover:** the open blockers in `docs/redteam/2026-09-21/RED_TEAM_REPORT.codex.final.md`.
- **Not covered here:** lanes L1–L5 (security: licence, money, update/package, admin, local tamper) belong to Codex and Gemini this wave.

**Verdict: blocked** until the launch-critical batch below lands and Codex re-gates it.
- No lane found a confirmed path to an unwanted input in a live game. P8-001 has a speculative one: two instances could briefly drive one console.
- The blockers are availability and trust failures:
  - the human locked out;
  - the bot silently doing nothing;
  - the bot shooting blind on a 2K patch;
  - a SYSTEM service writing where users can write;
  - Codex's open input, persistence, kill and audit blockers.

**Evidence of what works (09-22 owner sessions):**
- **Input path:** 4,308 critical transactions, 0 faults, worst delivery 0.7 ms against a 40 ms budget; the new fork image (`7786742a`).
- **Timing:** fire-to-wire median 0.22 ms (425/425 releases).
- **Detector:** Codex's fail-closed patch never tripped (0 faults in 1,187 health lines).
- **Quick Square re-shots:** clean.
- **Frame pacing:** clean in 5 of 6 sessions.

## Severity counts (new CL2 findings)

| Lane | High | Medium | Low |
|---|---:|---:|---:|
| P1 meter detection | 1 | 2 | 1 |
| P2 decoder / stream | 1 | 3 | 2 |
| P3 capture card | 2 | 5 | 3 |
| P4 controller input | 1 | 5 | 3 |
| P5 launcher performance | 0 | 4 | 6 |
| P6 timing engine | 0 | 4 | 3 |
| P7 local IPC / services | 1 | 3 | 1 |
| P8 real-world robustness | 2 | 5 | 3 |
| P9 2K patch response | 3 | 4 | 1 |
| **Total** | **11** | **35** | **23** |

## HIGH findings (ranked)

| # | ID | Finding | Owner |
|---|---|---|---|
| 1 | **CL2-P4-001** (+ CL2-P5-001) | **No dead-man switch.** The input tick, detection receipt and subtick run on the GUI thread, which has the lowest priority of the three timing processes. A GUI stall leaves the console holding the last input indefinitely and the human locked out. Measured receipt spikes: p99 187 ms, max 280 ms. | Claude |
| 2 | **CL2-P9-001** | **When the meter disappears, the bot keeps shooting blind** on hard-coded 2K27 hold times. The "NO METER" label does not stop it, and one false lock clears it. This is the 2K-patch-day failure. | Claude (+ Astra for the reader signal) |
| 3 | **CL2-P9-002** | A meter that is detected but changed (speed or geometry) gives silently wrong timing. Nothing compares what is observed with the compiled constants. | Claude / Astra |
| 4 | **CL2-P9-003** | No fleet telemetry: the owner learns of a break from Discord. | Claude (+ owner privacy decision) |
| 5 | **CL2-P8-001** | A second Venice instance runs in full. Its Connect kills the first instance's live stream, and both share the pipe and files. | Claude |
| 6 | **CL2-P8-002** | The fire lease lapses silently after sleep or a 10–15 min internet drop. The UI says Ready and the bot never fires. There is no retry until the next 5-minute tick. | Claude |
| 7 | **CL2-P7-001** | VeniceNetSvc (SYSTEM) writes its token under `C:\ProgramData\NexusVision`, a folder a standard user can own. | Claude (+ installer) |
| 8 | **CL2-P3-002** | A capture card whose name matches no hint word never gets timing authority, and nothing tells the customer why. | Claude |
| 9 | **CL2-P3-001** | A non-card device can become the session's video source, and the real card is never retried. The error text blames OBS. | Claude |
| 10 | **CL2-P1-001** | Right-stick Go-To meters appear ~2 s after the stick push; the windows assume 0.7–1.6 s (2K26 values pinned by tests). This caused the lost 09-22 16:50:03 shot. It fails closed. | Astra (reader) + Claude (engine windows) |
| 11 | **CL2-P2-001** | Remote-Play-only mode has no retained field evidence. The owner reports it works well; a logged retest is planned after the patches. | Owner test |

## MEDIUM findings

| ID | Finding | Owner |
|---|---|---|
| **CL2-P2-003** | **The launcher-side 25 ms ACK timeout still kills the console session.** It is the other half of the Triangle+Circle bug; the fork side was fixed on 09-21. | Claude — **launch-critical** |
| **CL2-P4-002** | **Regression from my 09-22 R2 patch.** The shared neutral predicate now includes the triggers, so holding R2 after a pad blip keeps the whole pad neutral. The UI does not mention the triggers. | Claude — **launch-critical** |
| CL2-P4-003 / 004 | After a soft fault, a release goes out without its copies. The fork and the launcher can then disagree on who owns input (the launcher logs "ViGEm carries input" while the fork drops ViGEm). | Claude (with CL-001) |
| CL2-P4-005 | Chiaki's Qt keep-alive is a second producer, and a stale read can re-press a released button. Dormant on the owner rig. | Claude (fork) |
| CL2-P4-006 | SHOT_RELEASE bypasses the fork's own "never copy a press" rule. | Claude (fork) |
| CL2-P2-002 | When the console ends the session, the feedback sender is torn down under a live input-bridge thread: a destroyed-mutex race that can crash or hang OrionStream. | Claude (fork) |
| CL2-P2-004 | In Remote-Play-only mode, per-frame logging shares the lock the input path logs under. | Claude (fork) |
| **CL2-P5-003** | Shot records are ON by default, hard-coded to **`D:\NexusVision\shot_records`** (most customers have no D:), fsync every shot and have no cap. | Claude — **launch-critical** |
| CL2-P5-002 | `orion_user.log` does synchronous I/O on the GUI thread and never rotates. | Claude |
| CL2-P5-004 | The full security evaluation runs every second and re-notifies ~90 QML properties. | Claude |
| CL2-P6-001 | Displaced releases still train four learners (press-to-tip, no-meter hold, velocity prior, clock seed), three of them persisted. Seven real cases were found. | Claude |
| CL2-P6-002 | With FF gain 0, the A/B arms and the late carry go unfenced from phase/landing. | Claude |
| CL2-P6-003 | The dev sweep does not fence the banner trim or oracle: a +24 ms shot moved the trim 3 ms earlier on 09-22. | Claude |
| **CL2-P6-004** | The FF fence has no size floor, so it discards ~45% of learner evidence; 70% of the displacements it discards are under 4 ms. This likely explains why the trim is so slow (7 steps in 430 shots). | Claude |
| CL2-P7-002 / 003 / 004 | The frame pipe accepts unverified frames for detection. The input pipe has a fixed name, no first-instance flag and no peer check. Every user can read the service token. | Claude |
| CL2-P3-003 / 007 | The device is stored as a bare index; a second card can be adopted "by name". | Claude |
| CL2-P3-004 | A 34–48 fps feed with gaps up to 207 ms passes health checks and can fire. | Claude |
| CL2-P3-005 | There is no plain-language capture error for the customer. | Claude |
| CL2-P3-006 | MJPG cards, 1440p/4K, HDR and 30 Hz cards have never been tested. | Owner (buy one cheap dongle) + VM |
| CL2-P8-003 / 004 | Resume from sleep latches SAFE MODE ("UI froze"); a backward clock step blinds vision for its length. | Claude |
| CL2-P8-005 | A DHCP move of the PS5 reaches the stream only; Meter Delay and the RTT probe keep the stale IP. | Claude |
| CL2-P8-006 / 007 | Raw `timestamp_expired` when the clock is 5+ min off. A crash between the settings save and its signature locks production with an opaque reason. | Claude |
| CL2-P1-002 | Shot-record onset/pickup can log the previous meter (586 of 1,587 pickups). Live timing is unaffected. The 09-22 late-carry onset claim was re-run with those rows removed and holds. | Astra / Claude |
| CL2-P1-003 | 30+ ft: 50% LATE vs 18.6%. The range reader returned "unknown" on 1,608 of 1,608 records. | Astra (range) |
| CL2-P9-004 | No remote safe mode, only a MOTD or the global kill. | Claude (backend config flag) |
| CL2-P9-005 | Every detection fix is a full signed release, with no canary (6–10 h at best). | Claude + owner |
| **CL2-P9-006** | The regression corpus is missing (5 sessions, all 2K26). **The two local 2K27 framedumps (~6 GB) have no backup.** | **Owner: back them up now** |
| CL2-P9-007 | The banner-verdict reader goes silent on a UI change, with no health signal. | Claude |

## LOW findings (batch after launch unless trivial)

| Lane | IDs |
|---|---|
| P1 | 004 |
| P2 | 005 / P4-008 (the same issue: every normal stop logged as a fatal fault); 006 |
| P3 | 008, 009, 010 |
| P4 | 007, 009 |
| P5 | 005–010 |
| P6 | 005 (late carry edge cases), **006 (`ORION_ONSET_FF_*` env knobs live in production: make dev-only, trivial)**, 007 |
| P7 | 005 |
| P8 | 008–010 |
| P9 | 008 |

## Carryover blockers (Codex final gate, 09-22 03:14): all still open

| Item | IDs |
|---|---|
| Fork soft-fault bound and checked transitions; this wave adds the 30 Hz resend | CL-001 |
| Cross-trigger `pre` packet | CL-003 |
| Sender exit states | CL-005 |
| Telemetry age | CL-006 |
| learning.json semantic/atomic | GM-002 / CX-001 / CX-008 |
| Calibration cancel provenance | GM-006 / CX-007 / CX-020 |
| Warm kill cache | CX-013 |
| Audit wrapper | CX-014 |
| Stripe Charge mapping and idempotency | CX-002 |
| Log sink on a full disk (P5 and P8 add evidence) | CX-015 |
| Monthly entitlement | CX-016 |
| Pending order claims | CX-017 |
| DLL preload | CX-018 |
| Identifiers in diagnostics | CX-019 |
| No-Meter policy | CL-008 (owner) |

## Patch order

**Batch 1: input and availability** (the owner cannot safely retest without it)

| Item | IDs |
|---|---|
| Dead-man switch and thread priority | P4-001 + P5-001 |
| Launcher-side 25 ms kill | P2-003 |
| R2 regression | P4-002 |
| Fork soft-fault bound and checked transitions | CL-001, incl. P4-003/004 |
| Sender exit states | CL-005 |
| Cross-trigger `pre` | CL-003 |
| Telemetry age | CL-006 |
| Single instance | P8-001 |

**Batch 2: silent-failure states**

| Item | IDs |
|---|---|
| Pause instead of blind shooting | P9-001 |
| Remote pause flag in the existing config | P9-004 |
| Lease retry, resume check and banner | P8-002 |
| Capture: qualify by measurement, plain-language errors, retry the real card | P3-001 / 002 / 004 / 005 |
| Shot records into the app data folder, capped (off for customers?) | P5-003 |
| Disk-full log sink | CX-015 |

**Batch 3: trust and persistence**

| Item | IDs |
|---|---|
| ProgramData DACL; pipe hardening | P7-001; P7-002–004 |
| learning.json | GM-002 / CX-001 / CX-008 |
| Calibration cancel | GM-006 / CX-007 / CX-020 |
| Fence fixes | P6-001 / 002 / 003 / 004 |
| FF env knobs dev-only | P6-006 |
| Kill cache | CX-013 |
| Audit wrapper | CX-014 |
| Stripe | CX-002 / 016 / 017 |
| DLL preload | CX-018 |

**Batch 4: with Astra**

| Item | IDs |
|---|---|
| Go-To windows | P1-001 |
| Range reader | P1-003 |
| Pickup forensics | P1-002 |

**Owner actions** (no code):
- Back up the 2K27 framedumps (P9-006).
- Decide whether to advertise Remote-Play-only at launch, then run the logged retest (P2-001).
- Buy one cheap USB capture dongle to set the minimum spec (P3-006).
- Decide whether the telemetry and privacy wording ships (P9-003).
- Run the VM install checklist (P10) after the rebuild.

Then: Codex re-gates Batches 1–3 → package + StrictSecurity → deploy on the owner's word → external pass.
