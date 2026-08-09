# Chiaki-ng Max Performance Build Plan

## Current State Summary

**CPU**: AMD Ryzen 7 5800X (Zen 3 / znver3) — 8 cores / 16 threads
**Compiler**: GCC 16.1.0 (MSYS2 MinGW64)
**Already applied**: LTO, -march=znver3, -O3, -funroll-loops, AV_CODEC_FLAG_LOW_DELAY, TCP_NODELAY, feedback flush 200→50ms

**Architecture flow**:
```
PS5 → UDP packets → takion recv loop → frame_processor (reassemble + FEC)
  → video_sample_cb → FFmpeg decoder (avcodec_send_packet / avcodec_receive_frame)
  → placebo Vulkan renderer → swapchain present
  → OrionFrameExport (named pipe) → Orion bot CV pipeline
```

**Critical threads**:
- Takion recv (network)
- Congestion control (200ms interval)
- Feedback sender (50ms max, was 200ms)
- FFmpeg decode (inline in video_sample_cb)
- Qt Quick render thread
- Deferred swap thread (HighPriority)
- Present pace thread
 Orion input router (1ms loop)
- Orion frame pipe reader/writer

---

## 1. NETWORKING

### 1.1 Zero-copy recv path (mmsg recvmmsg / WSASend/WSARecv scatter-gather)

**Explanation**: The takion recv loop calls `recv()` into a single buffer, then `memcpy`s packet data into frame_processor slots. Replace with `LPFN_WSARECVMSG` + scatter-gather buffers that place packet payload directly into the frame_processor slot at the correct offset, eliminating one memcpy per packet (~1400 bytes × ~30 packets/frame × 60fps = ~2.5MB/s saved).

**Why it helps**: Each memcpy is small but they're on the hot path — every packet goes through it. Eliminating the copy reduces cache pollution and frees the memory bus for the decoder.

**Impact**: Low-Medium  
**Difficulty**: Hard  
**Risk**: Medium — WSARECVMSG semantics differ from recv, edge cases with partial reads  
**Estimated gain**: ~0.5-1ms per frame in packet processing  
**Requires modifying**: networking, threading  
**Novel?**: Not in mainstream streaming clients; common in high-frequency trading

### 1.2 SO_RCVBUF scaling + SO_BUSY_POLL

**Explanation**: Current `SO_RCVBUF` is set to `takion->a_rwnd` (the protocol advertised window). On a 60fps 1080p stream with ~30 packets/frame, the kernel buffer can overflow during brief CPU spikes. Increase to 4MB + enable `SO_BUSY_POLL` (Windows: `SIO_LOOPBACK_FAST_PATH` for local, or `SO_BUSY_POLL` via registry for NIC) to keep the NIC polling path active.

**Why it helps**: Prevents packet drops during GC pauses in Qt/render thread. `SO_BUSY_POLL` reduces recv latency by ~50-100μs on supported NICs.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Fewer dropped frames, ~0.1ms recv latency reduction  
**Requires modifying**: networking  
**Novel?**: Common in HFT, not in streaming

### 1.3 Receive batching via `WSARecvFrom` with multiple buffers

**Explanation**: Instead of calling `recv()` once per packet in a loop, use `WSARecvFrom` with overlapped I/O and a completion port to batch-receive multiple packets per syscall. Each syscall wakeup processes all available packets instead of one.

**Why it helps**: Reduces user/kernel transitions from ~1800/s to ~60/s. Each transition costs ~1-2μs on Windows.

**Impact**: Medium  
**Difficulty**: Hard  
**Risk**: Medium — requires restructuring the takion recv loop  
**Estimated gain**: ~1-2ms CPU time reclaimed, reduced wakeups  
**Requires modifying**: networking, threading  
**Novel?**: Not attempted in chiaki; standard in high-perf network code

### 1.4 Congestion control interval: 200ms → 100ms

**Explanation**: The congestion control thread sends packet loss stats every 200ms. The PS5 uses this to adjust bitrate. At 200ms, the PS5 is 4-8 frames behind actual conditions. Reduce to 100ms for faster bitrate adaptation.

**Why it helps**: Faster congestion feedback → PS5 adjusts bitrate sooner → fewer lost frames during bandwidth spikes.

**Impact**: Low-Medium  
**Difficulty**: Easy  
**Risk**: Low — slightly more overhead, but negligible  
**Estimated gain**: Faster bitrate adaptation, fewer FEC recoveries  
**Requires modifying**: networking  
**Novel?**: No, standard tuning

---

## 2. VIDEO DECODE

### 2.1 Speculative decode-ahead (pipeline overlap)

**Explanation**: Currently `avcodec_send_packet` + `avcodec_receive_frame` happen synchronously in the video_sample_cb. Restructure so that after sending the current frame's packets to the decoder, we immediately start the next frame's reassembly while the decoder works. Use a double-buffered frame processor: while frame N is being decoded, frame N+1's packets are being reassembled.

**Why it helps**: Overlaps network I/O with decode, hiding ~2-3ms of decode latency behind packet reassembly.

**Impact**: High  
**Difficulty**: Hard  
**Risk**: Medium — requires careful synchronization between recv and decode  
**Estimated gain**: ~2-3ms end-to-end latency reduction  
**Requires modifying**: decoder, threading, FFmpeg  
**Novel?**: Not in chiaki; some commercial clients do pipelined decode

### 2.2 Hardware decode zero-copy to Vulkan

**Explanation**: When hardware decode is active (D3D11VA / NVDEC), the decoded frame lives in a GPU texture. Currently chiaki does `av_hwframe_transfer_data` to CPU (for the Orion pipe export) then re-uploads to GPU for rendering. Instead, interop the HW decode surface directly to a Vulkan image via `VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D11_TEXTURE_BIT` / `VK_KHR_external_memory_win32`.

**Why it helps**: Eliminates GPU→CPU→GPU round-trip (~8-15ms at 1080p). The Orion pipe export can read from a CPU-side copy done in parallel, not on the render path.

**Why it helps**: This is the single biggest latency win available — the hwframe transfer is the dominant cost in the decode→present path.

**Impact**: Very High  
**Difficulty**: Extreme  
**Risk**: High — D3D11/Vulkan interop is driver-sensitive, may break on some GPUs  
**Estimated gain**: ~8-15ms at 1080p (the hwframe transfer cost)  
**Requires modifying**: decoder, rendering, Vulkan, FFmpeg  
**Novel?**: Not in chiaki; Moonlight does something similar with CUDA-OpenGL interop

### 2.3 Pre-allocated AVFrame pool (eliminate per-frame alloc)

**Explanation**: `avcodec_receive_frame` returns a new AVFrame each call. The decoder internally allocates buffers. Pre-allocate a pool of 4 AVFrames with `av_frame_alloc` + `av_buffer_ref` and cycle through them, passing `av_frame_ref` to the renderer instead of copying.

**Why it helps**: Eliminates malloc/free on every frame (~60/s). Each allocation is ~1-2KB of metadata + ref counting overhead.

**Impact**: Low  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~0.1-0.3ms per frame, reduced GC pressure  
**Requires modifying**: decoder, FFmpeg  
**Novel?**: Common in production decoders

### 2.4 AV_CODEC_FLAG2_CHUNKS + parallel slice decode

**Explanation**: For H.265 streams with multiple slices, enable `AV_CODEC_FLAG2_CHUNKS` to allow `avcodec_send_packet` to accept partial NALUs. Combined with `thread_type = FF_THREAD_SLICE` and `thread_count = 2-4`, this enables parallel slice decoding within a single frame.

**Why it helps**: For multi-slice H.265 (PS5 sends 2-4 slices per frame), this cuts decode time by ~30-50% on multi-core CPUs.

**Impact**: Medium-High (for software decode path)  
**Difficulty**: Moderate  
**Risk**: Low — slice threading is well-supported in FFmpeg  
**Estimated gain**: ~2-5ms decode time reduction (software decode only)  
**Requires modifying**: decoder, FFmpeg  
**Novel?**: Standard FFmpeg optimization, but chiaki currently uses thread_count=1

**Note**: We set thread_count=1 for low-delay. This is a tradeoff: slice threading adds ~0.5ms synchronization overhead but can save 2-5ms on multi-slice frames. Worth testing with thread_count=2.

### 2.5 Frame processor: eliminate per-frame malloc/realloc

**Explanation**: `chiaki_frame_processor_alloc_frame` calls `realloc` + `malloc` + `memset` every frame. Pre-allocate a maximum-size frame buffer + unit_slots array at init time and reuse them. Only realloc if the frame size exceeds the pre-allocated maximum.

**Why it helps**: Eliminates 2-3 malloc/free + 2 memset calls per frame at 60fps. The memset alone zeros ~100KB per frame.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.2-0.5ms per frame, reduced memory fragmentation  
**Requires modifying**: decoder  
**Novel?**: Standard optimization, not done in chiaki

### 2.6 Frame processor: eliminate memmove in flush

**Explanation**: `chiaki_frame_processor_flush` does a `memmove` for every unit to strip the 2-byte size prefix. Instead, pass the stride + offset to the decoder via a custom `AVPacket` side-data or by adjusting `packet->data` pointers to skip the prefix in-place.

**Why it helps**: Eliminates ~30 memmoves totaling ~100KB per frame.

**Impact**: Low-Medium  
**Difficulty**: Moderate  
**Risk**: Medium — the decoder expects contiguous data; may need to build a proper bytestream  
**Estimated gain**: ~0.1-0.3ms per frame  
**Requires modifying**: decoder, FFmpeg  
**Novel?**: Not attempted in chiaki

---

## 3. RENDERING

### 3.1 MAILBOX present mode instead of IMMEDIATE

**Explanation**: Currently using `VK_PRESENT_MODE_IMMEDIATE_KHR` (no vsync) or `VK_PRESENT_MODE_FIFO_KHR` (vsync). `VK_PRESENT_MODE_MAILBOX_KHR` is the best of both: it queues the latest frame and drops stale ones, so the display always shows the freshest frame without tearing. If MAILBOX is unsupported, IMMEDIATE is correct (current behavior).

**Why it helps**: MAILBOX eliminates tearing while maintaining minimum latency. On the 5800X + RX 6700 XT / RTX 3070 (typical Orion rigs), MAILBOX is usually supported.

**Impact**: Medium (visual quality + latency consistency)  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Eliminates tearing, ~0 frame latency cost vs IMMEDIATE  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Standard Vulkan optimization; chiaki already mentions MAILBOX in comments but doesn't use it

### 3.2 Swapchain depth: 3 → 2

**Explanation**: Current swapchain depth is 3. With IMMEDIATE present mode, depth 2 is sufficient and reduces the maximum queue depth by 1 frame (~16.7ms at 60fps). The deferred swap thread already handles backpressure.

**Why it helps**: Reduces maximum present latency by one frame.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low — may cause occasional stutter if the render path is slow; test needed  
**Estimated gain**: ~16.7ms worst-case latency reduction  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Standard swapchain tuning

### 3.3 Persistent mapped staging buffer for Orion frame export

**Explanation**: The OrionFrameExport does `av_hwframe_transfer_data` (GPU→CPU) then copies to a pack buffer then writes to the pipe. Instead, use a persistent mapped staging buffer (`vkMapMemory` with `VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT`) that's always mapped, and have the GPU copy directly into it via a copy command. The pipe writer reads from this buffer.

**Why it helps**: Eliminates the `av_hwframe_transfer_data` call (which creates a temporary CPU frame) and replaces it with a single GPU→staging copy. Saves ~2-4ms.

**Impact**: High  
**Difficulty**: Hard  
**Risk**: Medium — requires Vulkan staging buffer management  
**Estimated gain**: ~2-4ms in the export path  
**Requires modifying**: rendering, Vulkan, decoder  
**Novel?**: Not in chiaki; common in GPU compute pipelines

### 3.4 Disable Qt Quick compositing during active stream

**Explanation**: When the Orion stream is active and the QML overlay is minimal (just the meter bar), the Qt Quick render pass is unnecessary overhead. Skip the `quick_window->render()` call when the overlay is empty or static, and present the video frame directly.

**Why it helps**: Qt Quick compositing adds ~1-3ms per frame of GPU time + a Vulkan semaphore wait.

**Impact**: Medium  
**Difficulty**: Moderate  
**Risk**: Medium — QML UI elements (settings panel, etc.) won't render during stream  
**Estimated gain**: ~1-3ms GPU time, reduced semaphore synchronization  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Not attempted; most streaming clients composite UI on CPU

### 3.5 Vulkan command buffer pre-recording + ring

**Explanation**: The render path records Vulkan command buffers every frame. Pre-record a ring of command buffers for the "no overlay" case (just the video frame blit + present) and only re-record when the overlay changes. Submit from the ring.

**Why it helps**: Command buffer recording is ~0.5-1ms of CPU time per frame.

**Impact**: Low-Medium  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~0.5-1ms CPU time per frame  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Common in game engines, not in streaming clients

---

## 4. INPUT

### 4.1 Input router: 1ms → 0.5ms loop with hybrid sleep

**Explanation**: The Orion input router loops at 1ms (`time.sleep(remaining)`). Replace with a hybrid: `time.sleep(remaining - 0.0003)` then busy-wait the last 300μs with `time.perf_counter_ns()`. This eliminates oversleep (Windows `Sleep(1)` can sleep 1-2ms) while keeping CPU usage reasonable.

**Why it helps**: Reduces input-to-pipe latency jitter from ±1ms to ±0.1ms. For a timing-critical bot, this is the difference between a green and a late release.

**Impact**: High  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.5-1ms input latency jitter reduction  
**Requires modifying**: threading, operating system behavior  
**Novel?**: Common in game timing loops, not in streaming input

### 4.2 Direct input pipe write (bypass virtual controller)

**Explanation**: Already implemented (Item 11). The OrionInputBridge in Chiaki reads from `\\.\pipe\orion_input` and calls `chiaki_session_set_controller_state` directly. But the input router still goes through `PhysicalControllerReader → remap_engine → virtual_controller.submit_state`. The virtual_controller then writes to the pipe. Eliminate the virtual_controller layer and write directly to the pipe from the input router.

**Why it helps**: Removes one layer of indirection + one function call per input update.

**Impact**: Low (already fast, but removes a layer)  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1-0.2ms per input update  
**Requires modifying**: threading, SDL  
**Novel?**: Already partially done

### 4.3 Controller state diff-based send (only send on change)

**Explanation**: The feedback sender sends controller state every 50ms (max) or on change. But the "on change" check (`controller_state_equals_for_feedback_state`) compares the full state. Optimize the comparison to a 64-bit hash of the relevant fields (sticks + buttons) so the diff check is a single uint64 compare.

**Why it helps**: The current comparison does ~10 field comparisons. A hash makes it 1 compare + 1 hash computation (which the compiler can vectorize).

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Negligible CPU, but cleaner code  
**Requires modifying**: networking  
**Novel?**: No

### 4.4 Feedforward clock: use PTS-based frame timing instead of wall clock

**Explanation**: The AutomationEngine's `triggerRelease` uses `now` (wall clock ms). But the frame age (now - last_frame_time) includes variable decode + pipe transfer latency. Use the PTS from the frame header (already wired through in Item 12) to compute the true frame time, and schedule releases relative to PTS instead of wall clock.

**Why it helps**: Eliminates ~2-5ms of jitter in release timing caused by variable pipe transfer latency. The release fires at the correct frame-relative time regardless of when the frame arrived.

**Impact**: Very High (for autogreen accuracy)  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~2-5ms release timing accuracy improvement  
**Requires modifying**: decoder, threading  
**Novel?**: Not in any streaming client; specific to the Orion bot use case

---

## 5. MEMORY

### 5.1 Arena allocator for frame processor

**Explanation**: Replace `malloc`/`free` in `chiaki_frame_processor_alloc_frame` with a bump allocator that allocates from a pre-allocated arena. Reset the arena (bump pointer to 0) at the start of each frame instead of freeing individual allocations.

**Why it helps**: Eliminates malloc/free overhead (~0.1ms per frame) and improves cache locality (all frame data is contiguous in memory).

**Impact**: Low-Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1-0.2ms per frame, better cache behavior  
**Requires modifying**: decoder  
**Novel?**: Standard in game engines

### 5.2 Cache-aligned hot structs

**Explanation**: The `ChiakiFrameProcessor` and `ChiakiVideoReceiver` structs are accessed from the takion recv thread. Their hot fields (frame_buf, unit_slots, units_source_received) should be on separate cache lines from the cold fields (log, packet_stats). Add `alignas(64)` to the hot fields or split the struct.

**Why it helps**: Prevents false sharing between the recv thread and the decode thread.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1ms from reduced cache invalidation  
**Requires modifying**: decoder  
**Novel?**: Standard optimization

### 5.3 Pre-fetch frame buffer during packet reassembly

**Explanation**: While reassembling packets into `frame_buf`, issue `__builtin_prefetch` for the next packet's destination offset. The CPU will start loading the cache line while the current packet is being processed.

**Why it helps**: Hides memory latency for the ~30 sequential packet copies per frame.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1ms per frame  
**Requires modifying**: decoder  
**Novel?**: Common in memcpy-heavy code

---

## 6. THREADING

### 6.1 CPU affinity: pin critical threads to specific cores

**Explanation**: The 5800X has 8 cores / 16 threads. Pin:
- Core 0 (physical): Takion recv + frame processor (network hot path)
- Core 1 (physical): FFmpeg decode (CPU-intensive)
- Core 2 (physical): Qt render + present (GPU submission)
- Core 3 (physical): Orion input router + frame pipe writer
- Cores 4-7: Everything else (Qt UI, congestion control, Orion CV)

**Why it helps**: Prevents thread migration between cores, which causes cache cold-start (~0.5ms per migration). On SMT, physical cores 0-3 get dedicated L1/L2 cache.

**Impact**: High  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~1-2ms from eliminated cache misses + reduced scheduler overhead  
**Requires modifying**: threading, operating system behavior  
**Novel?**: Common in game engines and HFT; chiaki has affinity callbacks but they're not used on Windows

### 6.2 Eliminate mutex on frames_lost counter

**Explanation**: `chiaki_video_receiver_flush_frame` takes `frames_lost_mutex` to increment `frames_lost`. This is a single int32 counter. Replace with `_InterlockedIncrement` (Windows) or `__atomic_fetch_add` (GCC). The mutex lock/unlock is ~0.05ms but happens on every frame.

**Why it helps**: Removes a lock/unlock pair from the hot path.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.05ms per frame  
**Requires modifying**: threading  
**Novel?**: Standard

### 6.3 Lock-free single-producer single-consumer ring for frame export

**Explanation**: The OrionFrameExport uses a mutex + condition variable + `pending_` AVFrame pointer. Replace with a lock-free SPSC ring of AVFrame pointers (capacity 2). The export side writes, the writer thread reads, no locks.

**Why it helps**: Eliminates mutex contention between the decode thread (exportFrame) and the writer thread.

**Impact**: Low-Medium  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~0.1-0.2ms per frame  
**Requires modifying**: threading, decoder  
**Novel?**: Not in chiaki; standard in audio/video pipelines

### 6.4 Windows timer resolution: global 0.5ms

**Explanation**: The input router calls `timeBeginPeriod(1)`. Set it to 0.5ms (`timeBeginPeriod(1)` is the minimum on Windows, but we can use `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION` on Windows 10+ for sub-ms waits). Alternatively, use a multimedia timer at 0.5ms.

**Why it helps**: The 1ms timer resolution means `Sleep(1)` can sleep 1-2ms. With high-res timers, `Sleep(0.5)` sleeps 0.5-1ms.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low — slightly higher CPU from more frequent timer interrupts  
**Estimated gain**: ~0.5ms timing precision  
**Requires modifying**: operating system behavior, threading  
**Novel?**: Known but rarely used

### 6.5 MMCSS "Pro Audio" task for critical threads

**Explanation**: Register the takion recv, decode, and present threads with the MMCSS "Pro Audio" task class. This gives them priority boost + dedicated CPU time slices similar to audio drivers, preventing preemption by background tasks.

**Why it helps**: Windows scheduler gives MMCSS Pro Audio threads priority over most other threads, including some system services.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Reduced preemption jitter, ~0.5-1ms  
**Requires modifying**: operating system behavior, threading  
**Novel?**: Used in pro audio, not in streaming clients

---

## 7. COMPILER

### 7.1 PGO (Profile-Guided Optimization)

**Explanation**: Build chiaki with `-fprofile-generate`, run a 5-minute streaming session, then rebuild with `-fprofile-use`. GCC will use the profile data to optimize branch prediction, function layout, and inlining.

**Why it helps**: The hot paths (takion recv, frame processor, decoder) have predictable branch patterns that PGO can optimize. Expected 5-10% improvement in hot-path code.

**Impact**: Medium  
**Difficulty**: Moderate (requires two builds + a profiling run)  
**Risk**: Low  
**Estimated gain**: ~5-10% faster hot paths, ~0.5-1ms cumulative  
**Requires modifying**: compiler  
**Novel?**: Standard compiler optimization, not yet applied

### 7.2 Hot/cold function splitting

**Explanation**: Mark error-handling paths (CHIAKI_LOGE, CHIAKI_LOGW) with `__attribute__((cold))` and hot paths with `__attribute__((hot))`. GCC will move cold code to a separate section, improving instruction cache density for the hot path.

**Why it helps**: The error paths in takion.c, frameprocessor.c, and videoreceiver.c are large but rarely executed. Moving them out of the hot code section improves I-cache hit rate.

**Impact**: Low-Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1-0.3ms from better I-cache utilization  
**Requires modifying**: compiler  
**Novel?**: Standard but rarely applied manually

### 7.3 Link-order optimization: hot functions first

**Explanation**: Use a linker script to place the hottest functions (takion recv, frame_processor_flush, avcodec_send/receive) at the start of the .text section, where they're more likely to be in the L1 I-cache.

**Why it helps**: The L1 I-cache is typically 32KB. Placing hot functions first ensures they're in the cache when needed.

**Impact**: Low  
**Difficulty**: Hard  
**Risk**: Low  
**Estimated gain**: ~0.1ms from I-cache improvement  
**Requires modifying**: compiler  
**Novel?**: Common in embedded, not in desktop apps

---

## 8. FRAME PACING

### 8.1 Hybrid sleep: coarse sleep + busy-wait final 500μs

**Explanation**: `throttleFramePresentation` already does 1ms-granularity `QThread::usleep` in a loop. Replace with: `Sleep(remaining_ms - 1)` for the coarse part, then `while (now < target) _mm_pause()` for the final <1ms. This gives sub-ms precision without burning CPU for the full wait.

**Why it helps**: Current pacing has ±1ms jitter. The hybrid approach has ±0.05ms jitter.

**Impact**: High (for frame pacing consistency)  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.5-1ms frame pacing jitter reduction  
**Requires modifying**: rendering, threading, operating system behavior  
**Novel?**: Common in game engines, not in streaming clients

### 8.2 Adaptive present interval based on decode latency

**Explanation**: Currently the present interval is fixed to the display refresh interval. Make it adaptive: if decode latency is low (<10ms), present at full refresh rate. If decode latency spikes (>20ms), temporarily present at half rate to avoid building a backlog, then return to full rate when latency recovers.

**Why it helps**: Prevents the "backlog spiral" where a slow frame causes more frames to queue, increasing latency further.

**Impact**: Medium  
**Difficulty**: Moderate  
**Risk**: Medium — may cause visible stutter during adaptation  
**Estimated gain**: Prevents 30-50ms latency spikes during transient slowdowns  
**Requires modifying**: rendering, threading  
**Novel?**: Not in chiaki; some commercial clients do adaptive bitrate but not adaptive present

### 8.3 VRR (Variable Refresh Rate) awareness

**Explanation**: If the display supports VRR (FreeSync/G-Sync), set `VK_PRESENT_MODE_IMMEDIATE_KHR` and let the display sync to the frame rate. This eliminates the need for throttle-based pacing entirely — the display will present each frame as soon as it's ready.

**Why it helps**: Eliminates all pacing jitter. Frame is presented the instant it's ready, no waiting for vblank.

**Impact**: High (if VRR display is available)  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Eliminates ~16.7ms of pacing wait  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Standard for VRR displays, but chiaki doesn't check for VRR

---

## 9. FPS CAP

### 9.1 Latency-aware frame cap (not time-based)

**Explanation**: Instead of capping at a fixed FPS, cap based on end-to-end latency. If the decode→present latency is <15ms, allow uncapped FPS. If it exceeds 20ms, cap at 30fps to prevent backlog. The cap adjusts dynamically.

**Why it helps**: A fixed 60fps cap with 25ms decode latency creates a 5-frame backlog. A latency-aware cap prevents this.

**Impact**: High  
**Difficulty**: Moderate  
**Risk**: Medium — may confuse the PS5's bitrate adaptation  
**Estimated gain**: Prevents latency buildup during slow decode  
**Requires modifying**: rendering, decoder, threading  
**Novel?**: Not in any streaming client

### 9.2 Uncapped internal pipeline, capped display output

**Explanation**: Run the decode pipeline uncapped (decode every frame as fast as it arrives), but only present every Nth frame to the display. The Orion pipe export gets every frame (for maximum bot accuracy), while the display runs at 30/60fps.

**Why it helps**: The bot gets maximum frame rate for timing accuracy, while the display doesn't waste GPU time on frames the user can't see.

**Impact**: Medium (for bot accuracy)  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: Bot gets 60fps feed even if display is capped at 30  
**Requires modifying**: rendering, decoder  
**Novel?**: Specific to the Orion use case

### 9.3 Move cap to presentation stage only

**Explanation**: Currently `throttleFramePresentation` is called before `pl_swapchain_submit_frame`. Move it AFTER the submit, so the frame is already in the swapchain queue when we sleep. This means the GPU can start processing while we wait.

**Why it helps**: Overlaps the sleep with GPU work, hiding ~1-2ms of render time.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~1-2ms from overlapping sleep with GPU work  
**Requires modifying**: rendering  
**Novel?**: Not in chiaki

---

## 10. WINDOWS-SPECIFIC

### 10.1 Process priority: ABOVE_NORMAL → HIGH

**Explanation**: Set the chiaki process priority to `HIGH_PRIORITY_CLASS` (not `REALTIME` which can starve the system). This gives chiaki priority over most user processes.

**Why it helps**: Prevents background processes (antivirus, Windows Update, etc.) from preempting the decode/render threads.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Reduced preemption, ~0.5-1ms  
**Requires modifying**: operating system behavior  
**Novel?**: Standard

### 10.2 Disable CPU parking + set high-performance power plan

**Explanation**: On the 5800X, Windows may park cores or reduce frequency under light load. Force the "High Performance" power plan + disable core parking via registry. This keeps all cores at max frequency.

**Why it helps**: Prevents ~5-10ms frequency ramp-up delay when a burst of frames arrives.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low (higher power consumption)  
**Estimated gain**: Eliminates frequency ramp latency  
**Requires modifying**: operating system behavior  
**Novel?**: Standard for gaming

### 10.3 Game Mode + DWM exclusion

**Explanation**: Enable Windows Game Mode (which disables Windows Update during gaming) and set the chiaki window to "fullscreen exclusive" or "borderless fullscreen" to bypass DWM compositing.

**Why it helps**: DWM adds ~1-2ms of compositing latency in windowed mode.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~1-2ms present latency  
**Requires modifying**: operating system behavior, rendering  
**Novel?**: Standard

### 10.4 Interrupt affinity: bind NIC IRQ to core 0

**Explanation**: Use `MSI-X` interrupt affinity to bind the network card's interrupt to core 0 (where the takion recv thread runs). This ensures the packet arrival interrupt and the recv thread are on the same core, reducing cache misses.

**Why it helps**: Reduces packet handling latency by ~0.1-0.5ms.

**Impact**: Low-Medium  
**Difficulty**: Moderate (requires PowerShell + admin)  
**Risk**: Low  
**Estimated gain**: ~0.1-0.5ms packet latency  
**Requires modifying**: operating system behavior  
**Novel?**: Common in HFT, not in streaming

### 10.5 EcoQoS: prevent efficiency throttling

**Explanation**: Windows 11 has "EcoQoS" (Eco Quality of Service) which can throttle background processes. Ensure chiaki is tagged as "foreground" / "game" to prevent EcoQoS throttling.

**Why it helps**: Prevents Windows from reducing chiaki's CPU frequency when it thinks it's background.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Prevents throttling  
**Requires modifying**: operating system behavior  
**Novel?**: Windows 11 specific

---

## 11. HIDDEN BOTTLENECKS

### 11.1 Logging overhead: compile out CHIAKI_LOGV in Release

**Explanation**: `CHIAKI_LOGV` calls are in the hot path (every packet, every frame). Even if the log level filters them, the function call + format string evaluation still happens. Wrap `CHIAKI_LOGV` in `#ifdef NDEBUG` to compile it out entirely in Release builds.

**Why it helps**: Each CHIAKI_LOGV call is ~0.01ms but there are ~100 per frame = ~1ms total.

**Impact**: Medium  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.5-1ms per frame  
**Requires modifying**: networking, decoder, rendering  
**Novel?**: Standard

### 11.2 Atomic memory ordering: use relaxed/acquire instead of seq_cst

**Explanation**: The render path uses `std::atomic` with default (seq_cst) ordering for flags like `present_backpressure_active`, `deferred_present_in_flight`. Most of these don't need sequential consistency — `relaxed` (for flags) or `acquire/release` (for data) is sufficient.

**Why it helps**: seq_cst emits `mfence` on x86, which is ~30 cycles. relaxed/acquire uses `mov` (1 cycle).

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low — if done carefully  
**Estimated gain**: ~0.1ms per frame from reduced fence overhead  
**Requires modifying**: rendering, threading  
**Novel?**: Standard optimization

### 11.3 String allocations in debug logging

**Explanation**: `qCDebug(chiakiGui) << ...` constructs a `QDebug` object + string formatting even when debug logging is disabled. Wrap in `qCDebugCategory().isEnabled(QtDebugMsg)` check or compile out.

**Why it helps**: Each disabled debug log still allocates ~100 bytes of temporary strings.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1ms per frame  
**Requires modifying**: rendering  
**Novel?**: Standard

### 11.4 Reference frame tracking: linear scan → bitmap

**Explanation**: `have_ref_frame` does a linear scan of 16 int32s. Replace with a 16-bit bitmap + `_bittestandset`. The scan becomes O(1).

**Why it helps**: Minor, but it's on the per-frame path.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: Negligible  
**Requires modifying**: decoder  
**Novel?**: No

---

## 12. RADICAL IDEAS

### 12.1 Custom UDP recv loop with IOCP + thread pool

**Explanation**: Replace the takion recv thread with an IOCP-based recv loop that processes packets on a thread pool. Each packet is dispatched to a worker that handles decryption + frame assembly. The main thread only handles frame flush + decode.

**Why it helps**: Overlaps packet processing with decode. On an 8-core CPU, this can process 4-8 packets in parallel.

**Impact**: High  
**Difficulty**: Extreme  
**Risk**: High — requires complete rewrite of takion.c  
**Estimated gain**: ~2-5ms from parallel packet processing  
**Requires modifying**: networking, threading  
**Novel?**: Not in any streaming client

### 12.2 Speculative frame flush (flush before all packets arrive)

**Explanation**: The PS5 sends FEC packets that can reconstruct missing source packets. If we have enough packets for FEC to succeed, flush the frame immediately without waiting for the remaining packets. This is already done (`chiaki_frame_processor_flush_possible`), but the threshold is conservative. Lower it to flush as soon as FEC is mathematically possible.

**Why it helps**: Saves ~1-2ms of waiting for the last 1-2 packets that FEC can reconstruct anyway.

**Impact**: Medium  
**Difficulty**: Moderate  
**Risk**: Medium — more FEC recoveries, slightly higher CPU  
**Estimated gain**: ~1-2ms per frame  
**Requires modifying**: decoder  
**Novel?**: Not attempted

### 12.3 Adaptive decode thread count based on frame size

**Explanation**: For 720p frames (small), use thread_count=1 (low latency). For 1080p frames (large), use thread_count=2 (parallel slice decode). Switch dynamically based on the negotiated resolution.

**Why it helps**: 720p decode is fast enough single-threaded; 1080p benefits from parallelism.

**Impact**: Medium  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~2-3ms on 1080p decode  
**Requires modifying**: decoder, FFmpeg  
**Novel?**: Not attempted

### 12.4 Self-profiling runtime: adaptive optimization

**Explanation**: Add a lightweight profiler that measures decode→present latency every frame. If latency exceeds a threshold, automatically: (1) reduce present interval, (2) increase decode thread count, (3) lower export FPS cap. When latency recovers, reverse the changes.

**Why it helps**: Adapts to changing conditions (other processes, thermal throttling, etc.) without manual tuning.

**Impact**: High  
**Difficulty**: Hard  
**Risk**: Medium — adaptation oscillation  
**Estimated gain**: Prevents sustained latency spikes  
**Requires modifying**: rendering, decoder, threading  
**Novel?**: Not in any streaming client

### 12.5 Replace placebo renderer with direct Vulkan blit

**Explanation**: The placebo renderer does color conversion, scaling, and tone mapping. For the Orion use case (we just need the frame on screen + exported to the pipe), a simple Vulkan blit shader is sufficient. Replace placebo with a ~50-line Vulkan blit pipeline.

**Why it helps**: Eliminates placebo's ~2-4ms of GPU processing per frame. The blit shader is <0.5ms.

**Impact**: High  
**Difficulty**: Hard  
**Risk**: High — loses HDR, scaling, tone mapping  
**Estimated gain**: ~2-4ms GPU time per frame  
**Requires modifying**: rendering, Vulkan  
**Novel?**: Not attempted; most clients use a full renderer

### 12.6 Compile-time specialization for known resolution

**Explanation**: If we know the stream is always 1080p60 H.265 (Orion's typical case), specialize the frame processor at compile time: fixed buffer sizes, fixed unit counts, no realloc path. Use `if constexpr` (C++) or `#define ORION_1080P_60` to eliminate all dynamic size checks.

**Why it helps**: Eliminates ~10 branches per frame, all of which are always false.

**Impact**: Low  
**Difficulty**: Easy  
**Risk**: Low  
**Estimated gain**: ~0.1ms per frame  
**Requires modifying**: decoder, compiler  
**Novel?**: Common in embedded, not in desktop

---

## 13. AUTOGREEN SYSTEM INTEGRITY

### 13.1 PTS-corrected release timing (complete Item 12)

**Explanation**: The PTS is now wired through from Chiaki → pipe → FrameData → orchestrator → sidecar. But the AutomationEngine's `triggerRelease` still uses wall-clock `now`. Wire the PTS through to the AutomationEngine so release timing accounts for decode + pipe latency.

**Why it helps**: The #1 source of release timing error is the gap between "when the frame was decoded" and "when the bot sees it." PTS closes this gap.

**Impact**: Very High (for autogreen accuracy)  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: ~2-5ms release timing accuracy  
**Requires modifying**: decoder, threading  
**Novel?**: Specific to Orion

### 13.2 Frame-locked detection: process CV at PTS, not arrival time

**Explanation**: The CV pipeline processes frames in arrival order. But if frame N+1 arrives before frame N (out of order due to pipe buffering), the CV processes them in the wrong order. Use PTS to order frames in the CV pipeline.

**Why it helps**: Prevents the bot from acting on a stale frame.

**Impact**: Medium  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: Prevents occasional mistimed releases  
**Requires modifying**: decoder, threading  
**Novel?**: Specific to Orion

### 13.3 Dual-signal consensus: meter + pose timing

**Explanation**: The AutomationEngine already has dual-signal consensus (Item 2 from the previous work). Ensure it's fully wired: the pose timing detector's release signal and the meter-based green window signal must agree within ±2 frames. If they disagree, hold the shot (don't release).

**Why it helps**: Prevents false releases when one signal is corrupted (occlusion, meter not visible, etc.).

**Impact**: High (for autogreen reliability)  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: Eliminates false releases  
**Requires modifying**: decoder, threading  
**Novel?**: Specific to Orion

### 13.4 Post-release meter capture: calibrate from every shot

**Explanation**: The AutomationEngine has `startPostReleaseMeterCapture` which collects the settled meter reading after a release. Ensure this is active for every shot type (not just the default). Use it to continuously calibrate the feedforward clock.

**Why it helps**: The feedforward clock learns from every shot, improving timing over time.

**Impact**: High (for autogreen accuracy over time)  
**Difficulty**: Moderate  
**Risk**: Low  
**Estimated gain**: Improves release timing by ~1-2ms per session  
**Requires modifying**: decoder  
**Novel?**: Specific to Orion

---

## PRIORITY RANKING

### Tier 1: Do First (Highest impact / lowest effort)

| # | Optimization | Impact | Difficulty | Est. Gain |
|---|---|---|---|---|
| 6.1 | CPU affinity pinning | High | Easy | 1-2ms |
| 8.1 | Hybrid sleep (busy-wait final 500μs) | High | Easy | 0.5-1ms |
| 10.1 | Process priority HIGH | Medium | Easy | 0.5-1ms |
| 10.2 | Disable CPU parking + high perf plan | Medium | Easy | 5-10ms ramp |
| 11.1 | Compile out CHIAKI_LOGV in Release | Medium | Easy | 0.5-1ms |
| 2.5 | Pre-allocated frame processor buffers | Medium | Easy | 0.2-0.5ms |
| 4.1 | Hybrid sleep in input router | High | Easy | 0.5-1ms |
| 3.1 | MAILBOX present mode | Medium | Easy | visual + latency |

### Tier 2: Do Second (High impact, moderate effort)

| # | Optimization | Impact | Difficulty | Est. Gain |
|---|---|---|---|---|
| 2.1 | Speculative decode-ahead | High | Hard | 2-3ms |
| 2.2 | HW decode zero-copy to Vulkan | Very High | Extreme | 8-15ms |
| 7.1 | PGO build | Medium | Moderate | 0.5-1ms |
| 3.3 | Persistent staging buffer for export | High | Hard | 2-4ms |
| 13.1 | PTS-corrected release timing | Very High | Moderate | 2-5ms |
| 9.3 | Move cap to after submit | Medium | Easy | 1-2ms |
| 6.5 | MMCSS Pro Audio task | Medium | Easy | 0.5-1ms |

### Tier 3: Do Third (Medium impact, higher effort)

| # | Optimization | Impact | Difficulty | Est. Gain |
|---|---|---|---|---|
| 1.3 | IOCP recv batching | Medium | Hard | 1-2ms |
| 2.4 | Adaptive decode thread count | Medium | Moderate | 2-3ms |
| 3.5 | Vulkan command buffer ring | Low-Med | Moderate | 0.5-1ms |
| 8.2 | Adaptive present interval | Medium | Moderate | prevents spikes |
| 9.1 | Latency-aware frame cap | High | Moderate | prevents backlog |
| 12.4 | Self-profiling adaptive runtime | High | Hard | adaptive |

### Tier 4: Nice to Have

| # | Optimization | Impact | Difficulty | Est. Gain |
|---|---|---|---|---|
| 1.1 | Zero-copy recv | Low-Med | Hard | 0.5-1ms |
| 1.2 | SO_RCVBUF scaling | Medium | Easy | fewer drops |
| 2.3 | AVFrame pool | Low | Moderate | 0.1-0.3ms |
| 2.6 | Eliminate memmove in flush | Low-Med | Moderate | 0.1-0.3ms |
| 5.1 | Arena allocator | Low-Med | Easy | 0.1-0.2ms |
| 5.2 | Cache-aligned structs | Low | Easy | 0.1ms |
| 6.2 | Atomic counter for frames_lost | Low | Easy | 0.05ms |
| 6.3 | Lock-free SPSC ring | Low-Med | Moderate | 0.1-0.2ms |
| 7.2 | Hot/cold splitting | Low-Med | Easy | 0.1-0.3ms |
| 11.2 | Relaxed atomics | Low | Easy | 0.1ms |
| 12.5 | Replace placebo with blit | High | Hard | 2-4ms |
| 12.6 | Compile-time specialization | Low | Easy | 0.1ms |

---

## ESTIMATED TOTAL GAIN (Tier 1 + Tier 2)

| Category | Current | After Tier 1+2 | Savings |
|---|---|---|---|
| Network recv → frame ready | ~5ms | ~3ms | ~2ms |
| Frame → decode complete | ~8ms (SW) / ~3ms (HW) | ~5ms (SW) / ~1ms (HW+ZC) | ~3-7ms |
| Decode → present | ~5ms | ~2ms | ~3ms |
| Present → Orion pipe | ~3ms | ~1ms | ~2ms |
| Input → PS5 | ~50ms max | ~50ms max | (already optimized) |
| Frame pacing jitter | ±2ms | ±0.1ms | ~1.9ms |
| **Total end-to-end** | **~21ms** | **~11ms** | **~10ms** |

With hardware decode zero-copy (2.2), the total could drop to ~6-8ms.
