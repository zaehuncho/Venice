# Live-Capture Stutter — Root Cause & Fix Handoff

**TL;DR:** The Orion live feed stutters because chiaki's Vulkan renderer (libplacebo) **cannot
present on the off-screen/parked window** — `pl_swapchain_start_frame` fails for *seconds* at a
time, so the frame export starves and the feed drops to ~13-38fps in bursts (and chiaki
restart-loops). It is **NOT** the decode, the zero-copy transfer, the input hook, OBS, the GPU,
or the network. The fix is chiaki-side: decouple the frame export from the swapchain present, or
make the parked window's present able to cycle.

## Symptom
- Live feed visibly choppy ("frames drop"), worse during motion/shots. **Pre-existing.**
- Orion shows its **own QML render of the decoder-pipe frames** (`image://remote/live`,
  `RemotePlayPage.qml:98`) — the chiaki window is **parked off-screen**. So the user sees the
  **export/pipe rate**, NOT chiaki's on-screen present. (Don't measure chiaki's `display FPS` —
  that's the invisible parked window.)

## What it is NOT — ruled out with evidence, do not re-chase
| Layer | Why it's excluded |
|---|---|
| PS5 / source | chiaki log `Estimated source FPS: 59.999` — clean 60, wired; game is smooth on the PS5's own display |
| Network | wired Ethernet; stutters even at 720p / 4 Mbps |
| GPU | RTX 3060 12GB, ~25% load, **zero TDR / driver-reset events** in 18h |
| Decode | stutters at 720p AND 1080p, software AND `d3d11va` hardware decode |
| Zero-copy transfer | the `av_hwframe_transfer_data` "transfer to software for presentation" is HW-only; software decode has none and still stutters |
| Hook / OBS / capture method (pipe vs GDI) / chiaki build (Tier2 vs pre-Tier2) | all swapped live, zero change |
| Pacer busy-wait | `throttleFramePresentation` (`qmlmainwindow.cpp:5453`) bounds the `_mm_pause` spin to ≤500µs/frame (~3% of a core) — not the 94% peg |

## The real root — hard evidence
chiaki session log (`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log`):
```
Failed to start Placebo frame  consecutive_failures=98  failure_duration_us=3,015,729   (~3s stalled)
reason=placebo_discard  cause=present_vsync_disabled  swap_age_us=3,232,127  vsync=0
Vulkan renderer fallback: Orion pipe mode active; exiting for launcher-managed relaunch
```
- `pl_swapchain_start_frame` **fails for seconds** (98 consecutive failures), then chiaki exits
  and restart-loops.
- The Orion window is **parked off-screen** (Orion paints its own pipe-frame render + overlay).
  MAILBOX present mode (GLM's latency change, `qmlmainwindow.cpp` ~5896) disables vsync; on a
  hidden window with no presentable display the swapchain images never release → `start_frame`
  can't get an image → the present stalls → the export (coupled to the render/present) starves →
  the ~13-38fps bursty feed.
- One core pegged at 94% while system total is 28% = a single chiaki thread spinning/blocked in
  this present-retry loop.
- This is the long-documented "occlusion-freeze / present stalls on a hidden window" issue
  (project memory `nexusvision-decoder-feed`), now pinned to the swapchain `start_frame` failure.

## Fix — chiaki-side (`C:\Users\Administrator\Desktop\chiaki-ng-src`), cheapest first
### Option A — present mode / window (cheap, ~1 build cycle, decent odds)
- Revert MAILBOX → FIFO for the Orion-pipe (parked-window) path, OR keep the window on-screen but
  behind/under the Orion window (so the swapchain has a presentable surface) instead of fully
  off-screen. See the swapchain setup in `qmlmainwindow.cpp` (~5896) + how Orion-pipe mode parks
  the window. **Risk:** a truly hidden window may stall under FIFO too — if so, go to B.

### Option B — decouple export from the swapchain present (the proper fix)
- Render the decoded frame to an **offscreen placebo target** for the export, independent of the
  on-screen swapchain present, so the export never depends on a presentable window and the
  swapchain present becomes optional/non-blocking.
- See the present loop in `qmlmainwindow.cpp` (`pl_swapchain_start_frame` / `pl_render_image` /
  `pl_swapchain_submit_frame`, ~5660-5670), `gui/src/orionframeexport.cpp` (export reads the
  rendered frame). The `nexusvision-decoder-feed` memory says Codex did a decouple 2026-06-19 —
  verify whether it regressed or never covered this `start_frame` failure.

## Build / deploy
`cd C:\Users\Administrator\Desktop\chiaki-ng-src` then `C:\Python314\python.exe rebuild_deploy.py`
- Clean LTO build (~3-5 min, 16 jobs, `-march=x86-64-v2` portable); sets MSYS2 / `OPENSSL_ROOT_DIR`
  / `Qt6_DIR` / `CMAKE_PREFIX_PATH`; auto-backs-up + deploys to
  `native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`.
- **Reap `OrionNative` / `OrionStream` / the python sidecar BEFORE running** (the deploy copy
  needs the exe free). The `.bat` is incomplete — use the Python script.

## Verify
- chiaki log: **no** "Failed to start Placebo frame"; steady ~60 present.
- Orion "Capture health" (`logs/orion_native.log`): `uniqfps` ~60, low `dup%`, no `Capture degraded`.
- No single core pegged (`nvidia-smi dmon -s u` + per-core); chiaki doesn't restart-loop.
- Feed visibly smooth → re-run the meter + skele batches (the timing engine already logs
  `EXCELLENT`; this is purely the feed/display).

## Current state (as of this session, 2026-06-27)
- Deployed `OrionStream.exe` = current source (x86-64-v2, 6.6 MB). `.bak` = prior deployed;
  `.tier2` = the znver3-flagged build. Rebuilding from current source did NOT fix the stutter
  (confirmed it's the present-stall, not stale binary).
- **Orion-side crash FIXED + committed** (`97e614a`): a `VirtualController` double-free
  (threading race on the teardown guard → heap corruption → the 1GB OrionNative dumps). Atomic
  compare-exchange guard. Separate from the stutter.
- Launcher `run_orion.local.ps1` (gitignored) debug env to clean up later: `ORION_INPUT_HOOK=0`
  (ViGEm — the pre-encryption hook causes *shot-correlated* lag, a separate bug), `ORION_FRAME_PIPE=0`
  (note: the Python side treats "0" as truthy, so the pipe stays on regardless), camera-anchor on.
- Separate GLM-handoff docs now owned here: `HW_DECODE_READBACK_FOR_GLM.md` (1080p HW-decode
  capture — lower priority), `TIER2_PROMPT_FOR_GLM.md` (build portability — done), and
  `CHIAKI_PRESENT_STUTTER_FOR_GLM.md` (**superseded by this doc** — that one guessed the transfer;
  this one has the real `start_frame` root).

## Key files
- chiaki: `gui/src/qmlmainwindow.cpp` (swapchain/present ~5453-5670, MAILBOX ~5896, `map_frame` ~2202),
  `gui/src/orionframeexport.cpp` (export), `gui/src/qmlbackend.cpp` (zeroCopy/transfer ~440-465).
- Orion: `native_orion/qml/pages/RemotePlayPage.qml:98` (image-provider display),
  `native_orion/src/OrionAppController.cpp` (frame ingestion ~4880).
- Build: `chiaki-ng-src/rebuild_deploy.py`.
