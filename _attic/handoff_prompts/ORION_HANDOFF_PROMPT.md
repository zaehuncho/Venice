# ORION — WHAT I NEED BUILT

**Repo:** `C:\Users\aaron\Desktop\NexusVision`  
**Date:** 2026-07-22  
**Context:** This is a shot-meter timing bot for NBA 2K. It reads the shot meter via computer vision from a PS5 remote-play video feed (HDMI capture card, 1080p60), calculates when the meter will reach the green tip, and releases the shot button at the exact millisecond to green every shot. It also has a network sync layer that sniffs game packets to measure latency to the game server and compensate release timing accordingly.

---

## THE THREE THINGS THAT NEED TO WORK — AND DON'T

### 1. METER DETECTION: THE OVERLAY BOX IS NOT RENDERING OVER THE METER

The bot is supposed to draw a bounding box overlay on-screen around the shot meter while it's visible. Right now:

- **The overlay box does not appear on screen at all during shots.** The user sees the game feed in the Orion window but there is no visible rectangle drawn around the meter while shooting.
- **The detection telemetry shows `roi_not_found` continuously** — meaning the vision system is not finding the meter in the video frames at all during live gameplay, even though the meter is clearly visible on screen.
- **The detection DID work on the very first shot** of a session, then failed on all subsequent shots. This means the initial acquisition works but the system loses the meter and cannot re-acquire it.
- **The problem is worse on lateral shots** (Left Fade, Right Fade, Pull-ups) where the camera pans during the shooting animation. The meter moves across the screen with the player and the detector loses it completely.
- **Offline regression tests pass 51/51** — the detector works perfectly on saved frame sequences. The gap is between offline (static PNGs) and live (H.264 compressed, camera-panning, real-time).

**What I want:** The meter detection overlay must be visible on screen for EVERY shot, locked onto the meter from the moment it appears until it disappears. No blinking, no dropout, no vanishing mid-shot. The bounding box must track the meter smoothly even when the camera pans laterally on Fade shots. If the meter moves, the box moves with it. 100% of shots should have a visible overlay — not 1 out of 10.

---

### 2. BOT TIMING: THE BOT DOES NOT TIME TO THE TIP FOR ALL SHOTS

The bot is supposed to release the shot button at the exact moment the rising red meter fill reaches the green tip (the "make window" at the top of the meter). Right now:

- **The bot only timed correctly on the first shot.** After that, it either fires too early, too late, or doesn't fire at all because it has no detection data to work with (see Issue 1 above — no detection = no timing).
- **Fade shots (Left Fade, Right Fade) are consistently wrong.** The bot does not compensate for the different animation timing and latency on moving shots. Standstill shots occasionally time correctly; Fades almost never do.
- **The bot should work like a meter timing aimbot** — every single shot, regardless of shot type (Standstill, Left Fade, Right Fade, Pull-up, Go-To, Post Fade, Post Hook), should release at the green window. The user presses the shot button, the bot reads the meter rise in real-time, predicts exactly when it will hit the tip, and releases at that exact millisecond.
- **When the detection drops out (Issue 1), the bot falls back to a blind "feedforward" clock** — a fixed timer from button press to release. This blind clock is inaccurate because meter speeds vary by shot type, player build, and animation. The bot MUST have live vision of the meter to time correctly.

**What I want:** The bot must green every shot type. Standstills, Fades (left and right), Pull-ups, Go-Tos, Post moves — all of them. The release must land in the green window consistently. The bot needs to:
1. See the meter on every frame (Issue 1 must be solved first).
2. Track the fill percentage rising in real-time with sub-frame velocity extrapolation.
3. Predict the exact millisecond the fill will reach the green tip.
4. Release the button at that predicted moment, compensated for input latency and network delay.
5. Self-tune its timing offset based on make/miss feedback so it gets more accurate over time, not less.

---

### 3. NETWORK TAB: COURT IP, RTT, JITTER ARE ALL DEAD

The Orion dashboard has a "Sync" panel that should show real network telemetry:
- **Court IP** (the actual 2K game server IP address the PS5 is connected to)
- **RTT** (round-trip time to the game server in milliseconds)
- **Jitter** (variance in packet timing)
- **Applied offset** (how many milliseconds the bot is leading/lagging its release to compensate for network delay)

Right now:

- **Court IP shows `"—"` or `"Detecting..."`** — it never resolves to an actual IP address.
- **RTT shows `0.0 ms`** — no measurements are being taken.
- **Jitter shows `0.0 ms`** — same.
- **Applied offset shows `0.0 ms`** — the bot is not compensating for network latency at all.

The system has a WinDivert-based packet sniffer (`nexus_svc.py`) that is supposed to passively observe UDP game traffic, identify the court server IP, measure packet inter-arrival times, and calculate RTT/jitter. This data feeds into the release timing so the bot can lead its release by the exact network delay.

**What I want:** When I'm connected to an online game:
1. The Court IP field must show the actual game server IP address (e.g., `104.xxx.xxx.xxx`).
2. RTT must show the real measured round-trip latency to that server, updating live.
3. Jitter must show the real measured packet timing variance.
4. The Applied offset must show the actual millisecond compensation being applied to release timing.
5. All of this must feed into the bot's release calculation so that online shots are timed as accurately as offline/local shots.

---

## WHAT "DONE" LOOKS LIKE

When all three of these work together:

1. I launch the app, connect to PS5, enter an online game.
2. The Network tab shows the real court IP, live RTT, and jitter values updating in real time.
3. I take a shot — ANY shot type — and the green bounding box overlay appears on screen locked around the meter from the instant it appears.
4. The bot watches the red fill rise, predicts the tip crossing, and releases at the exact right millisecond — compensated for the live network delay.
5. The shot greens.
6. This works for shot #1, shot #2, shot #50, shot #200 — no degradation over a session.
7. This works for Standstills, Left Fades, Right Fades, Pull-ups, Go-Tos, and every other shot type.
8. The overlay never blinks, never disappears mid-shot, and the timing never drifts.

That is the product. Build it.

---

## KEY FILES TO READ FIRST

| File | What It Does |
|:---|:---|
| `simple_meter_reader.py` | The vision engine. Finds the meter, measures the red fill %, calculates velocity. This is where detection lives. |
| `native_orion/src/AutomationEngine.cpp` | The timing engine. Decides when to release based on fill %, velocity, predicted tip crossing, and latency offsets. |
| `native_orion/qml/pages/DashboardPage.qml` | The UI dashboard. Court IP, RTT, Jitter, Applied Offset display. |
| `native_orion/src/OrionAppController.cpp` | Wires everything together — telemetry, overlay rendering, network bridge signals. |
| `rtt_sync_engine.py` | Python-side RTT measurement, Kalman filtering, jitter estimation, sync offset calculation. |
| `nexus_svc.py` | WinDivert packet capture service — sniffs game UDP traffic to detect court IP and measure packet timing. |
| `run_orion.local.ps1` | Dev launcher. Sets all environment flags and launches `OrionNative.exe`. Run with `-Framedump -Detdiag` for diagnostics. |
| `remote_play_orchestrator.py` | Orchestrator that bridges the Python sidecar (detection) with the native C++ engine (timing/release). |
| `logs/orion_native.log` | Runtime log. Grep for `DETDIAG`, `Release issued`, `Court IP locked` to diagnose. |
| `docs/ORION_PICKUP_PROMPT.md` | Previous session handoff notes with guardrails and hard-won lessons. |

## HOW TO LAUNCH FOR TESTING

```powershell
powershell -ExecutionPolicy Bypass -File .\run_orion.local.ps1 -Framedump -Detdiag
```

## HOW TO VERIFY OFFLINE

```powershell
# Python tests (78/78 must pass)
.venv\Scripts\pytest.exe tests/test_simple_meter_reader.py tests/test_shot_feedback_reader.py tests/test_rtt_sync_engine.py -q

# Offline detection gates (51/51 must pass)
.venv\Scripts\python.exe tools/regression/run_gates.py

# Full native build + test suite
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python .venv\Scripts\python.exe
```
