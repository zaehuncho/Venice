# Prompt for Claude: Implement Tier 2 Chiaki Performance Optimizations

## Context

You are working on a heavily customized build of chiaki-ng (a PlayStation Remote Play client) that powers "Orion" — an automated NBA 2K bot. The bot captures frames from the chiaki stream via a named pipe, runs computer vision (meter detection, pose timing), and injects controller input back via a named pipe. Latency and frame pacing directly affect the bot's shot-release timing accuracy.

### Environment

- **CPU**: AMD Ryzen 7 5800X (Zen 3 / znver3) — 8 cores / 16 threads
- **OS**: Windows 11
- **Compiler**: GCC 16.1.0 (MSYS2 MinGW64), Ninja generator
- **Chiaki source**: `C:\Users\Administrator\Desktop\chiaki-ng-src`
- **Orion repo**: `C:\Users\Administrator\Desktop\NexusVision`
- **Build dir**: `C:\Users\Administrator\Desktop\chiaki-ng-src\build-orion-optimized`
- **Deploy target**: `C:\Users\Administrator\Desktop\NexusVision\native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`
- **Python**: `C:\Python314\python.exe`

### Build command (use Python subprocess, NOT .bat — cmd.exe mangles GCC flags)

```python
import os, subprocess
env = dict(os.environ)
env['PATH'] = r'C:\msys64\mingw64\bin;' + env['PATH']
env['OPENSSL_ROOT_DIR'] = r'C:\msys64\mingw64'
r = subprocess.run([
    'cmake', '-G', 'Ninja', '-S', '.', '-B', 'build-orion-optimized',
    '-DCMAKE_BUILD_TYPE=Release',
    '-DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/cc.exe',
    '-DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/c++.exe',
    '-DCMAKE_AR=C:/msys64/mingw64/bin/gcc-ar.exe',
    '-DCMAKE_RANLIB=C:/msys64/mingw64/bin/gcc-ranlib.exe',
    '-DCMAKE_C_FLAGS=-march=x86-64-v2 -O3 -funroll-loops',
    '-DCMAKE_CXX_FLAGS=-march=x86-64-v2 -O3 -funroll-loops',
    '-DCMAKE_C_FLAGS_RELEASE=-O3 -DNDEBUG',
    '-DCMAKE_CXX_FLAGS_RELEASE=-O3 -DNDEBUG',
    '-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON',
    '-DCHIAKI_ENABLE_TESTS=OFF', '-DCHIAKI_ENABLE_CLI=OFF',
    '-DCHIAKI_ENABLE_BOREALIS=OFF', '-DCHIAKI_ENABLE_STEAMDECK_NATIVE=OFF',
    '-DCHIAKI_ENABLE_SETSU=OFF', '-DCHIAKI_ENABLE_STEAM_SHORTCUT=OFF',
    '-DCHIAKI_ENABLE_GUI=ON', '-DCHIAKI_ENABLE_FFMPEG_DECODER=ON',
    '-DCHIAKI_LIB_ENABLE_OPUS=ON',
    '-DCMAKE_PREFIX_PATH=C:/msys64/mingw64',
    '-DOPENSSL_ROOT_DIR=C:/msys64/mingw64',
    '-DQt6_DIR=C:/msys64/mingw64/lib/cmake/Qt6',
], capture_output=True, text=True, env=env, cwd=r'C:\Users\Administrator\Desktop\chiaki-ng-src')
print(r.stdout[-500:], r.stderr[-300:], r.returncode)

# Build:
r = subprocess.run(['cmake', '--build', 'build-orion-optimized', '--parallel', '16'],
    capture_output=True, text=True, env=env, cwd=r'C:\Users\Administrator\Desktop\chiaki-ng-src')
print(r.stdout[-2000:], r.stderr[-500:], r.returncode)
```

### Deploy command

```python
import shutil
shutil.copy2(
    r'C:\Users\Administrator\Desktop\chiaki-ng-src\build-orion-optimized\gui\OrionStream.exe',
    r'C:\Users\Administrator\Desktop\NexusVision\native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe'
)
```

### IMPORTANT: How to patch chiaki-ng-src files

The chiaki-ng-src directory is OUTSIDE the NexusVision workspace. You CANNOT use the `edit` tool on it. You MUST use Python scripts via `run_command` to read/write files. Always use `encoding='utf-8', errors='replace'` when opening files. When doing string replacements, print `repr()` of the target lines first to verify exact whitespace (tabs vs spaces) before constructing the `old` string.

---

## What's Already Been Done (Tier 1 — ALL APPLIED AND BUILDING SUCCESSFULLY)

### Compiler/build level
- LTO (`CMAKE_INTERPROCEDURAL_OPTIMIZATION=ON`, 41 LTRANS jobs)
- `-march=x86-64-v2 -O3 -funroll-loops` (AVX2, FMA, BMI2, SSE4.2, POPCNT, F16C)
- Disabled: tests, CLI, Borealis, Steam Deck native, setsu, Steam shortcut
- Opus enabled, OpenSSL + Qt6 from MSYS2

### Source patches already in chiaki-ng-src

1. **`lib/src/ffmpegdecoder.c`**: `AV_CODEC_FLAG_LOW_DELAY` + `AV_CODEC_FLAG2_FAST` + `thread_count=2` (slice parallelism) + `FF_THREAD_SLICE`

2. **`lib/src/feedbacksender.c`**: `FEEDBACK_STATE_TIMEOUT_MAX_MS` 200→50ms (faster controller state flush)

3. **`lib/src/takion.c`**: `TCP_NODELAY` setsockopt on takion socket (disables Nagle)

4. **`lib/include/chiaki/log.h`**: `CHIAKI_LOGV` wrapped in `#ifdef NDEBUG` → compiled out in Release (saves ~1ms/frame of log overhead)

5. **`lib/src/frameprocessor.c`**: Reduced `memset` to only the used portion (not entire pre-allocated buffer); `__attribute__((hot))` on `chiaki_frame_processor_put_unit` and `chiaki_frame_processor_flush`

6. **`lib/src/videoreceiver.c`**: All `frames_lost` / `frames_lost_total` access replaced with `__atomic_fetch_add` / `__atomic_store_n` / `__atomic_load_n` (no more mutex); bitmap-based reference frame tracking (`reference_frames_bitmap` — O(1) lookup); `__attribute__((hot))` on `chiaki_video_receiver_av_packet`

7. **`lib/include/chiaki/videoreceiver.h`**: Added `uint16_t reference_frames_bitmap` field

8. **`gui/src/qmlmainwindow.cpp`**:
   - `#include <windows.h>` and `#include <immintrin.h>` at top
   - MAILBOX present mode instead of FIFO (with FIFO fallback if MAILBOX unsupported)
   - Swapchain depth 3→2
   - Render thread pinned to core 2 via `SetThreadAffinityMask(GetCurrentThread(), 0x04)` + `THREAD_PRIORITY_TIME_CRITICAL` (in a `QThread::started` lambda)
   - Hybrid sleep in `throttleFramePresentation`: coarse `QThread::usleep` for bulk, then `_mm_pause` busy-wait for final ≤500μs (±0.1ms pacing precision vs ±1ms)

### Windows system optimizations (scripts/orion_windows_optimize.py — already run)
- High Performance power plan
- CPU core parking disabled
- Process priority HIGH for OrionStream.exe (registry IFEO)
- EcoQoS / power throttling disabled
- Game Mode enabled
- Global timer resolution requests enabled
- Delivery Optimization disabled

### Orion-side patches already applied
- **`remote_play_orchestrator.py`**: Input router hybrid sleep (coarse `time.sleep` + busy-wait final 300μs); PTS stored and calibrated for frame-locked sync
- **`chiaki_backend.py`**: `pts` field added to `FrameData`, passed from pipe header
- **`autogreen_sidecar.py`**: PTS-corrected frame age used when available
- **`AutomationEngine.cpp`**: Autonomous calibration with per-shot-type latency residual learning, nonlinear meter-top deceleration correction, dual-signal consensus release, meter-memory fill extrapolation, Kalman filter for fill prediction, jitter-predictive release guard

### Patch scripts in chiaki-ng-src (for reference)
- `patch_orion_opts.py` — takion TCP_NODELAY, ffmpegdecoder low-delay, feedbacksender 50ms
- `patch_tier1.py` — all Tier 1 source patches (log.h, frameprocessor.c, videoreceiver.c/h, qmlmainwindow.cpp MAILBOX+swapchain, ffmpegdecoder thread_count)
- `patch_render_affinity.py` — render thread CPU affinity
- `patch_include_win.py` — windows.h include
- `patch_hybrid_sleep.py` — throttle hybrid sleep
- `fix_mailbox2.py` — MAILBOX fallback brace fix

---

## TIER 2 OPTIMIZATIONS TO IMPLEMENT

Read the full plan at `C:\Users\Administrator\Desktop\NexusVision\docs\CHIAKI_MAX_PERFORMANCE_PLAN.md` for detailed explanations. Here are the 7 Tier 2 items, in priority order:

### 2.1 — Speculative Decode-Ahead (Pipeline Overlap)
**Impact**: High | **Difficulty**: Hard | **Est. Gain**: 2-3ms

**What to do**: Restructure the decode pipeline so that after sending the current frame's packets to the decoder, the next frame's reassembly begins immediately while the decoder works. Use a double-buffered frame processor: while frame N is being decoded, frame N+1's packets are being reassembled.

**Files to modify**:
- `lib/src/videoreceiver.c` — the `chiaki_video_receiver_av_packet` and `chiaki_video_receiver_flush_frame` functions
- `lib/src/frameprocessor.c` — `ChiakiFrameProcessor` needs to support double-buffering
- `lib/include/chiaki/frameprocessor.h` — struct layout changes
- `lib/include/chiaki/videoreceiver.h` — add a second `ChiakiFrameProcessor`

**Approach**: Add a second `ChiakiFrameProcessor frame_processor_next` to `ChiakiVideoReceiver`. When a new frame index arrives while the current frame is still being decoded, start reassembling into `frame_processor_next`. After the current frame's decode completes, swap the processors. The key insight: `avcodec_send_packet` is asynchronous — the decoder works in the background while we can do other work.

**Risk**: Medium — requires careful synchronization between recv and decode. The takion recv thread must not block on decode. The video_sample_cb (which does the decode) must not block on recv.

### 2.2 — Hardware Decode Zero-Copy to Vulkan
**Impact**: Very High | **Difficulty**: Extreme | **Est. Gain**: 8-15ms

**What to do**: When hardware decode is active (D3D11VA / NVDEC), the decoded frame lives in a GPU texture. Currently chiaki does `av_hwframe_transfer_data` (GPU→CPU) then re-uploads to GPU for rendering. Instead, interop the HW decode surface directly to a Vulkan image via `VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D11_TEXTURE_BIT` / `VK_KHR_external_memory_win32`.

**Files to modify**:
- `gui/src/qmlmainwindow.cpp` — the `FFmpegDecoder` integration and the `pl_render` path. Look for where `av_hwframe_transfer_data` is called and where the decoded frame is passed to placebo.
- `lib/src/ffmpegdecoder.c` — the decoder setup, `av_hwdevice_ctx_create` configuration
- The Orion frame export path (the pipe writer) — it needs a CPU-side copy done in parallel, NOT on the render path

**Approach**:
1. Configure FFmpeg's Vulkan hwdevice context (`AVVulkanDeviceContext`) to share the same VkDevice/VkQueue that placebo uses
2. Set `hw_type = AV_HWDEVICE_TYPE_VULKAN` instead of D3D11VA
3. The decoded frame will be a `VkImage` wrapped in `AVFrame`
4. Pass this `VkImage` directly to placebo as a `pl_tex` without any CPU transfer
5. For the Orion pipe export, do a separate `vkCmdCopyImageToBuffer` to a persistent staging buffer (see item 3.3) in a background command buffer, NOT on the render critical path

**Risk**: High — D3D11/Vulkan interop is driver-sensitive. The `AVVulkanDeviceContext` must share the same VkDevice that placebo creates. This is the hardest optimization but also the single biggest latency win available. Moonlight does something similar with CUDA-OpenGL interop.

**Note**: There are existing `nb_decode_queues` deprecation warnings in the build — FFmpeg's Vulkan hwcontext API has evolved. Check the FFmpeg version in MSYS2 and use the correct API.

### 7.1 — PGO (Profile-Guided Optimization) Build
**Impact**: Medium | **Difficulty**: Moderate | **Est. Gain**: 0.5-1ms

**What to do**: Build chiaki with `-fprofile-generate`, run a 5-minute streaming session, then rebuild with `-fprofile-use`. GCC will use the profile data to optimize branch prediction, function layout, and inlining.

**Approach**:
1. Add `-fprofile-generate` to `CMAKE_C_FLAGS` and `CMAKE_CXX_FLAGS`
2. Build to a separate dir: `build-orion-pgo-gen`
3. Deploy and run a streaming session for 5+ minutes (the user will do this)
4. Rebuild with `-fprofile-use -fprofile-correction` pointing to the generated profile data
5. Build to `build-orion-pgo-use`
6. Deploy the PGO-optimized binary

**Note**: The profile data is written to the working directory of the process. Set `GCOV_PREFIX` environment variable to control where the `.gcda` files are written. After the profiling run, use `-fprofile-dir=build-orion-pgo-gen` to tell GCC where to find them.

### 3.3 — Persistent Mapped Staging Buffer for Orion Frame Export
**Impact**: High | **Difficulty**: Hard | **Est. Gain**: 2-4ms

**What to do**: The OrionFrameExport does `av_hwframe_transfer_data` (GPU→CPU) then copies to a pack buffer then writes to the pipe. Instead, use a persistent mapped staging buffer (`vkMapMemory` with `VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT`) that's always mapped, and have the GPU copy directly into it via a copy command. The pipe writer reads from this buffer.

**Files to modify**:
- `gui/src/qmlmainwindow.cpp` — the Orion frame export path. Search for `orionFrameExport` or `OrionFrameExport` or the pipe writer thread.
- You'll need to create a Vulkan buffer with `VK_BUFFER_USAGE_TRANSFER_DST_BIT`, allocate `VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT` memory, `vkMapMemory` it once at startup, and use `vkCmdCopyImageToBuffer` in the render command buffer to copy the decoded frame into it.

**Approach**:
1. At swapchain creation time, also create a staging buffer sized for 1080p BGRA (1920×1080×4 = ~8MB)
2. `vkMapMemory` it once — keep it mapped for the session lifetime
3. After `pl_render`, add a `vkCmdCopyImageToBuffer` from the rendered image to the staging buffer
4. The pipe writer thread reads from the mapped pointer — no `av_hwframe_transfer_data` needed
5. Use a fence to ensure the GPU copy is complete before the pipe writer reads

**Risk**: Medium — requires Vulkan buffer/memory management. The mapped pointer must remain valid. Use `VK_MEMORY_PROPERTY_HOST_COHERENT_BIT` to avoid explicit flushes.

### 13.1 — PTS-Corrected Release Timing
**Impact**: Very High (for autogreen accuracy) | **Difficulty**: Moderate | **Est. Gain**: 2-5ms

**What to do**: The PTS is already wired through from Chiaki → pipe → FrameData → orchestrator → sidecar. But the AutomationEngine's `triggerRelease` still uses wall-clock `now`. Wire the PTS through to the AutomationEngine so release timing accounts for decode + pipe latency.

**Files to modify**:
- `C:\Users\Administrator\Desktop\NexusVision\native_orion\src\AutomationEngine.cpp` — the `triggerRelease` function and the scheduling logic
- `C:\Users\Administrator\Desktop\NexusVision\native_orion\src\AutomationEngine.h` — add PTS tracking fields
- `C:\Users\Administrator\Desktop\NexusVision\native_orion\src\OrionAppController.cpp` — pass PTS from the frame data to the AutomationEngine
- `C:\Users\Administrator\Desktop\NexusVision\chiaki_backend.py` — ensure PTS is passed through the pipe correctly (already done)
- `C:\Users\Administrator\Desktop\NexusVision\remote_play_orchestrator.py` — ensure PTS is passed to the sidecar (already done)

**Approach**:
1. The AutomationEngine receives frame data with a `pts` field (already in the pipe protocol)
2. Compute `frame_age_ms = now - pts_to_wall(pts)` where `pts_to_wall` is the calibrated offset
3. In `triggerRelease`, use `frame_age_ms` instead of raw wall-clock time to determine if the release is on-time
4. The feedforward clock should learn from PTS-relative timing, not wall-clock timing
5. The `shot_.releaseTriggerMs` should be recorded as PTS-relative, not wall-clock

**Why this matters**: The #1 source of release timing error is the gap between "when the frame was decoded" and "when the bot sees it." The pipe transfer adds 2-5ms of variable latency. PTS closes this gap by anchoring timing to the frame's actual decode time, not its arrival time.

### 9.3 — Move Frame Cap to After Submit
**Impact**: Medium | **Difficulty**: Easy | **Est. Gain**: 1-2ms

**What to do**: Currently `throttleFramePresentation` is called before `pl_swapchain_submit_frame`. Move it AFTER the submit, so the frame is already in the swapchain queue when we sleep. This means the GPU can start processing while we wait.

**Files to modify**:
- `gui/src/qmlmainwindow.cpp` — in the `close_started_frame` lambda or the render function, find where `throttleFramePresentation` is called relative to `pl_swapchain_submit_frame` and swap their order.

**Approach**:
1. Find the `throttleFramePresentation(paced_interval_s)` call in the render path
2. Move it to AFTER `pl_swapchain_submit_frame(placebo_swapchain)` 
3. The GPU starts processing the submitted frame while we sleep
4. When we wake up, the frame is already rendered and ready for `pl_swapchain_swap_buffers`

**Risk**: Low — the throttle is just a sleep, moving it doesn't change correctness. But verify that the `present_backpressure_active` logic still works correctly when the throttle is after submit.

### 6.5 — MMCSS "Pro Audio" Task for Critical Threads
**Impact**: Medium | **Difficulty**: Easy | **Est. Gain**: 0.5-1ms

**What to do**: Register the takion recv, decode, and present threads with the MMCSS "Pro Audio" task class. This gives them priority boost + dedicated CPU time slices similar to audio drivers, preventing preemption by background tasks.

**Files to modify**:
- `gui/src/qmlmainwindow.cpp` — in the render thread `started` lambda (where we already set `SetThreadAffinityMask`), add `AvSetMmThreadCharacteristicsW(L"Pro Audio", &taskIndex)`
- `lib/src/takion.c` or `lib/src/streamconnection.c` — in the takion thread start, add the same call
- `lib/src/ffmpegdecoder.c` — if the decoder has its own thread, add it there too

**Approach**:
```c
#if defined(_WIN32)
#include <avrt.h>
DWORD taskIndex = 0;
HANDLE hTask = AvSetMmThreadCharacteristicsW(L"Pro Audio", &taskIndex);
// ... thread runs ...
if (hTask) AvRevertMmThreadCharacteristics(hTask);
#endif
```
Link with `-lwinmm` (already linked via Qt).

---

## Implementation Order

1. **9.3** (Move cap after submit) — easiest, do first, 1-2ms gain
2. **6.5** (MMCSS Pro Audio) — easy, 0.5-1ms gain
3. **13.1** (PTS-corrected release timing) — moderate, 2-5ms autogreen accuracy
4. **3.3** (Persistent staging buffer) — hard, 2-4ms gain
5. **2.1** (Speculative decode-ahead) — hard, 2-3ms gain
6. **7.1** (PGO build) — moderate, requires a profiling run
7. **2.2** (HW decode zero-copy) — extreme, 8-15ms gain, do last

## Verification

After each optimization:
1. Build successfully (183/183 compile units, LTO linked)
2. Deploy to `native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`
3. Verify the binary size is reasonable (~6.8MB)
4. The user will test by launching Orion and running a streaming session

## Key Files Reference

| File | Location | Purpose |
|---|---|---|
| `qmlmainwindow.cpp` | `gui/src/` | Main render/present/swapchain logic (~7500 lines) |
| `ffmpegdecoder.c` | `lib/src/` | FFmpeg decoder wrapper |
| `frameprocessor.c` | `lib/src/` | Frame reassembly + FEC |
| `videoreceiver.c` | `lib/src/` | Video packet handling + flush |
| `takion.c` | `lib/src/` | Network protocol (UDP recv/send) |
| `feedbacksender.c` | `lib/src/` | Controller state sender |
| `log.h` | `lib/include/chiaki/` | Logging macros |
| `thread.c` | `lib/src/` | Thread/mutex/cond wrappers |
| `AutomationEngine.cpp` | `NexusVision/native_orion/src/` | Bot logic (release timing) |
| `remote_play_orchestrator.py` | `NexusVision/` | Python orchestrator (input router, CV) |
| `chiaki_backend.py` | `NexusVision/` | Python chiaki integration (pipe, FrameData) |

## Critical Notes

- **chiaki-ng-src is outside the NexusVision workspace** — you cannot use the `edit` tool on it. Use Python scripts via `run_command` with `open(path, 'r', encoding='utf-8', errors='replace')` to read and `open(path, 'w', encoding='utf-8')` to write.
- **Always check `repr()` of target lines** before constructing replacement strings — the codebase mixes tabs and spaces.
- **The build uses Ninja, not Make** — there's no `link.txt`, Ninja tracks dependencies internally.
- **LTO is active** — the build takes ~5 minutes due to 41 serial LTRANS jobs. This is normal.
- **The `nb_decode_queues` deprecation warnings** in the build output are from FFmpeg's Vulkan hwcontext API — they're harmless but indicate the API has changed.
- **The Orion pipe protocol** uses named pipes: `\\.\pipe\orion_input` (controller state) and `\\.\pipe\orion_frames` (frame export). The frame pipe header includes a `pts` field (uint64_t, microseconds).
- **The user wants a 100% functional autogreen system** — the PTS-corrected release timing (13.1) is critical for this. It directly affects the bot's ability to time shot releases accurately.
