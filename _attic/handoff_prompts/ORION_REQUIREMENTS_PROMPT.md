# Orion — Requirements & Proof Mandate (What Must Work)

**You are the implementer/investigator for Orion**, a real-time NBA-2K auto-green bot. This document is the **owner's definition of "done"** — the exact behaviors that must work, and the **evidence bar** each must clear. It deliberately does **NOT** tell you how to fix anything. Your job is to (1) find why each behavior isn't working today, (2) fix it, and (3) **prove it works with 100% concrete evidence** — file:line references, real log excerpts, live framedump captures, and before/after numbers. No claim is accepted without proof. If something can only be proven on live hardware, say so explicitly and state exactly what to capture.

Repo: `C:\Users\aaron\Desktop\NexusVision`. Chiaki fork (sibling): `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`). The bot reads the shot meter over an Elgato HD60X capture card (`simple_meter_reader.py`), injects the release through a forked Chiaki Remote Play session, and schedules the release in the C++ `AutomationEngine`. The PS5 is routed to the internet **through the PC via ICS** (so the PC can see the console's game traffic). Test rig: `run_orion.local.ps1 -Framedump -Detdiag`. Diagnostic log: `logs/orion_native.log`. Offline gate: `python tools/regression/run_gates.py`.

---

## The Evidence Standard (applies to EVERYTHING below)

For every requirement, you must deliver:
- **Current-state proof** — *why* it isn't working now, grounded in `file:line` + a real artifact (a log line, a framedump frame, a gate result, a replay number). Not a theory — a reproduction.
- **The working proof** — after your fix, concrete evidence it now works: a **live framedump batch** frame-by-frame, log excerpts across a full session, per-shot-type numbers, before→after deltas.
- **Live vs offline honesty** — clearly label what you proved offline (replays, gates) vs what required live hardware. Do not present an offline pass as live-working.
- **Reproduce, don't assert** — every defect must be reproduced (offline replay or a live capture) before you claim to have fixed it, and re-verified after.
- **No degradation** — prove your change didn't break anything else (gates stay green; other shot types unaffected).

---

## PILLAR 1 — Meter Detection Overlay (the green box)

### What the owner wants
When a shot is taken, a green bounding box must appear on screen around the shot meter and **stay locked to it the entire time the meter is visible** — no blinking, no disappearing, no losing track when the camera pans on Fades. **Every shot, every time.**

### Concrete acceptance criteria (all must hold)
1. **Continuous presence.** From the frame the meter first appears to the frame it disappears, the overlay box is drawn on **every** frame the meter is on screen. Zero frames where the box vanishes while the meter is still visible. (Measured on live captures as: no mid-shot box-blink — `within_shot_ghost_frames = 0`, `within_shot_maxgap` at floor.)
2. **Locks through camera motion.** On **Left Fades and Right Fades**, the camera pans and the meter moves with the player — the box must track it, not fall off or freeze at the old position.
3. **Locks through occlusion.** When the shooting-arm animation crosses in front of the meter, the box must hold on the meter (not drop and re-acquire with a gap).
4. **Correct framing.** The box frames the **whole meter** — from the very tip (top) to the base (bottom). It must not box only the green window, must not sit short of the tip, and must not overshoot/overlay past the meter edges.
5. **Every shot type, every time.** Standstill, Left Fade, Right Fade, Pull-up, Go-To, Post — the box behaves identically well on all of them, on the 1st shot and the 100th.

### What you must prove
- A **live framedump** of each shot type, stepped frame-by-frame, showing the box present and correctly positioned across the entire meter-visible window — especially through the Fade camera pan and the arm-cross occlusion.
- Log evidence (`DETDIAG` lines) showing the detector reports a valid meter continuously through each shot (no `detected=False` / `roi_not_found` gaps while the meter is up).
- If the box currently blinks/vanishes: reproduce the exact frames it fails on (frame numbers + fill% + reject reason), root-cause it to `file:line`, then prove those frames now hold the box.
- **Distinguish two failure modes explicitly** (they have different causes): (a) the *detector* losing the meter (a detection gap), vs (b) the detector holding it but the *overlay render* not drawing it (a display gap). Prove which one(s) you fixed.

---

## PILLAR 2 — Bot Timing to the Tip (greens every shot)

### What the owner wants
The bot must read the meter fill rising in real-time, predict the exact millisecond it hits the green zone, and release the button right there. This must work for **every shot type** — Standstills, Left Fades, Right Fades, Pull-ups, Go-Tos, Post moves. **Not just the first shot. Not just standing still. Every shot greens.**

### Concrete acceptance criteria (all must hold)
1. **Live, dynamic timing.** The release is driven by the *live rising meter* read this shot — the bot predicts the tip from the current fill + rise velocity and fires to land on it. It must NOT fall back to a stale/average learned clock that fires at a fixed fill%.
2. **Lands on the tip.** The release registers when the meter is in the green window — producing a green / made result — consistently, not occasionally.
3. **Every shot type.** Standstill, Left Fade, Right Fade, Pull-up, Go-To, Post — each greens at a high rate. Fades and Go-To (harder types with occlusion / delayed meters) must green, not just Standstills.
4. **No degradation over a session.** Shot 1 and shot 50 green at the same rate. The bot must not "work at first then drift" or under-read later in a session.
5. **Latency-compensated.** The release fires early by the true loop latency (capture delay + input travel) so it *registers* at the tip, not ~30ms late. This compensation must be correct and self-consistent per shot type.

### What you must prove
- **A ground-truth for "did it green."** The bot currently cannot reliably tell if a shot hit the tip (its internal `verdict` is a meter self-grade that produces a phantom "LATE 66ms" — it is not a real result). You must establish a **reliable, concrete way to measure whether each shot greened / how far off it landed**, and use it to report real numbers. State your method and prove it's reliable.
- **Per-shot-type green-rate over a batch** — e.g., "Standstill X/Y green, Left Fade X/Y, Right Fade X/Y, Go-To X/Y, Pull-up X/Y, Post X/Y" — with the evidence behind each count.
- **Session-stability proof** — the green-rate across a *long* continuous session (many shots) shows no mid-session decay; shot-N timing error is not systematically worse than shot-1.
- **Proof the live rise drives the fire** — log/trace evidence that the release fired on the live meter crossing, not on a blind fixed-fill clock, for each shot type.
- **The lead/latency is correct** — evidence of the actual loop latency and that the release is compensated by it (per shot type), with the release landing on the tip rather than early/late.
- Reproduce the current failure per shot type (e.g., "Fades fire at 56% fill and read LATE") with log evidence, then prove the fix lands them on the tip.

---

## PILLAR 3 — Network Sync (Court IP, RTT, Jitter) via passive packet sniffing

### What the owner wants
When connected to an online game, the dashboard must show the **real game server IP** (the "court"), **live round-trip latency (RTT)**, and **live jitter** — obtained via **passive packet sniffing** of the console's game traffic. And these values must **feed into the bot's release timing** so online shots are automatically compensated for network delay.

### Concrete acceptance criteria (all must hold)
1. **Real court IP displayed.** When in an online game, the Network tab shows the **actual public game-server IP** the PS5 is connected to (not a blank "—", not a frozen value, not the LAN gateway, not the console's own IP). It updates live as the server changes between games.
2. **Live RTT + jitter.** The dashboard shows round-trip latency and jitter values that **reflect the real connection** and **change live** with network conditions — not a static or near-zero LAN reading.
3. **Passive sniffing works.** The system passively captures the console's game traffic (the PS5 is routed through the PC via ICS, so its packets pass through the PC) and identifies the court server from that flow. Prove the sniffer is actually seeing and locking the court.
4. **Feeds the release timing.** The measured latency/jitter must actually influence the bot's release lead — so online shots are compensated for network delay automatically, and the compensation adjusts as jitter/latency change. Prove the value flows into the fire scheduler and moves the release time (not computed-but-unused).
5. **Honest display.** Every value on the Network tab must be a true live reading of what it claims to be. No placeholder, frozen, or mislabeled numbers. If a value can't be measured yet, it must say so honestly rather than show a fake number.

### What you must prove
- **A screenshot/log showing the real court IP** locked while in an online game, and that it's the genuine 2K server (a public IP the console is actually talking to), with the packet-flow evidence that identified it.
- **Live RTT + jitter traces** that move with the connection (e.g., across a session, showing variation), proven to be the real hop — not the LAN gateway and not a frozen value.
- **The consumption trace** — `file:line` proof that the measured latency/jitter reaches the release scheduler and changes the fire time (with a before/after showing the release shifting when the measured value changes).
- **Root-cause of the current dead state** — the court detection has not locked since a specific date; passive capture is currently off/uninstalled. Prove exactly what is required to turn it on (opt-in, service, ICS path) and that once on, it locks a real court. Surface any admin/permission/privacy trade-offs of enabling passive capture rather than silently flipping it.
- Clearly separate what you can prove offline (the plumbing, the code path) from what needs a live online game (the actual court lock, the live RTT).

---

## CROSS-CUTTING — Peak performance at ALL times

These apply across all three pillars and are themselves acceptance criteria:
1. **No degradation, ever.** The bot and the meter detection must perform at **peak 100% of the time** — never getting worse the longer a session runs. Prove there is no mid-session decay in detection quality or timing accuracy.
2. **All shot types, equally.** The bot must lock to the tip for **all** shot types — Left Fades, Right Fades, Go-To shots, Standstills, Pull-ups, Post moves. Not a subset. Each must be individually proven, not assumed from Standstills working.
3. **Robust to real conditions.** It must work on the **live H.264 capture-card feed** (compression artifacts, occlusion, camera motion, different courts/lighting), not just on clean recorded frames. Offline gate passes do **not** count as proof of live robustness — prove it live.
4. **The feed must be stable.** Detection and timing are worthless on a frozen/stuttering capture feed. Prove the capture feed is delivering fresh, unique frames continuously during shots (no stalls), because a frozen feed masquerades as a detection/timing failure.

---

## Deliverable

For each of the three pillars (and the cross-cutting requirements), deliver:
1. **Current state** — proven, with `file:line` + artifacts, of exactly what is not working and why.
2. **The fix** — what you changed (found by you; this document intentionally prescribes none), flag-gated and reversible, not breaking anything else.
3. **The proof it works** — live framedump batches, per-shot-type numbers, log traces, before/after deltas, session-stability evidence.
4. **Honest gaps** — what still needs live hardware, what's unproven, what you couldn't determine.

Rank your work by **impact on make-rate** (greening shots is the only thing that matters). Be blunt about anything that is not actually working, even if it looks close. The bar is: **every shot type, every shot, greens to the tip; the box never leaves the meter; and the Network tab shows real, live, timing-feeding values — all proven with concrete evidence, live.**
