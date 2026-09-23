# Venice post-launch roadmap (drafted 2026-09-22, owner-approved direction)

Launch target: Saturday 2026-09-26, at the current online make rate (~70–76% on open standstills). Nothing here blocks launch.

## Order

| # | Item | Why | Reuses | Size |
|---|---|---|---|---|
| 1 | **More meter styles** (Pill, Dial, other colours) | Every customer not on Arrow2/White is shut out today | The Pill engine exists (withdrawn 09-21); Dial needs new fill logic | M |
| 2 | **Venice Spritz**: a standalone Cronus Zen script, the budget tier / trial funnel | No PC needed; cheap entry point | The owner's own `tools/cronus/MAN O WAR.gpc` (976 lines), renamed; 2K27 hold times retuned from Venice's shot data | S–M |
| 3 | **Remote Play-only, official** (no capture card) | Entry cost drops to a PC + PS5 | Works per the owner; needs a logged session (CL2-P2-001), setup polish, and the per-frame logging fix (CL2-P2-004) | S |
| 4 | **Venice Coach**: a training mode that presses nothing | A new audience (players who won't run a bot), low terms-of-service risk, openly marketable | Reader, banner grader, shot records; a new UI and a no-input mode | M |
| 5 | **Xbox** | Doubles the market | The existing, gated Xbox path; untested | M–L |
| 6 | **No-Meter mode** (plan below) | A headline feature; many players shoot meter-off | No-Meter v2 lead work, the YOLO26n pose model (`logs/diagnostics/pose_train/.../best.pt`, not in `models/`) | L |
| 7 | **Titan Two output for Venice** | The console sees a wired controller; no Remote Play input | `tools/cronus/CRONUS_TITAN_PATH.md` (KMG Capture, no code change expected). The owner rebuys a Titan Two if the project does well | S (+ hardware) |
| 8 | **Other games with visible timing meters** | Growth once Venice is stable | The vision → predict → press pipeline; each game is a new detection lane | L each |

Alongside, not code:
- **Pricing tiers:** Venice Lite (Standstill only) / Pro (all shot types, No-Meter, Coach stats), built on existing feature flags.
- **An annual plan** at a discount (lifetime is ruled out).
- **A paid 1-on-1 setup session**, which cuts refunds.

Carried from the launch red team as fast follows:
- The late carry, decided by the pre-registered A/B.
- 2K-patch safety: pause instead of blind shooting, a remote pause flag, basic health telemetry (CL2-P9-*).
- Capture cards qualified by measurement instead of by name (CL2-P3-002).
- Go-To 2K27 windows (CL2-P1-001).

## No-Meter plan

**Why offsets alone cap out.** The meter onset (press → meter visible) varies per shot with sd ~62–70 ms, and it is random shot to shot (lag-1 ~0). The meter is a picture of the animation, so a fixed press→release offset inherits that scatter. The green window is tens of ms wide. That is the ceiling of blind offsets, and of Spritz and competitor No-Meter modes.

**Approach: anchor on the animation.**
1. A small, fast pose model on a crop of the shooter (YOLO26n-pose, already trained 08-09, beat the shipped v2). It detects the gather or jump-start frame. At 60 fps, frame quantisation (16.7 ms) matters more than model size; a larger model does not help.
2. **Labels for free.** Meter-ON sessions give ground truth for each shot's ideal release (the meter tip) against the animation, so the model learns "animation start → perfect release" from the existing corpus (~3,000 graded shots, plus framedumps). No hand labelling.
3. The existing per-type press-anchored offsets remain the fallback when pose confidence is low.
4. Fix the known No-Meter v2 `− lead` bug (shots ~256 ms early) and resolve CL-008 (No-Meter / input-timed epoch join) first.

**Offline go / no-go** before any live test: run the pose anchor over recorded meter-on shots and measure the residual scatter of (pose-predicted release − meter tip).

| Residual sd | Decision |
|---|---|
| ≤ ~15 ms | Build it: No-Meter can approach the meter-on make rate |
| ~15–30 ms | Ship as a mid-tier feature, with honest copy |
| ≥ ~40 ms | Not worth it over blind offsets; stop |

History: the 08-09 cue TCN failed on too little data (val MAE 582 ms); the corpus is now far larger.

**Needs:** a backup of the 2K27 framedumps (CL2-P9-006; there are actually 12 sessions, ~23 GB, 09-12..09-20 under `D:\NexusVision\framedump`, plus ~19k loose frames; one owner and one rig, so collect opt-in footage from other players for generalisation), the pose model moved into `models/` with a manifest entry, and a sidecar venv that has torch (the shipped sidecar lacks it per the 08-08 audit; use ONNX/DirectML instead).
