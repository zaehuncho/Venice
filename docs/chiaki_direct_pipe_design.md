# Direct-to-Pipe Input Injection + Frame-Locked Detection Sync

## Item 11: Direct-to-Pipe Input Injection

### Goal
Bypass ViGEm → Windows controller stack → Chiaki reads controller, eliminating ~8-15ms
of Windows controller stack latency. Write controller state directly to Chiaki's
`chiaki_feedback_sender_set_controller_state` via a named pipe IPC.

### Architecture
1. **Chiaki side**: Add a named pipe server (`\\.\pipe\OrionControllerInput`) in
   `streamconnection.c` that accepts controller state packets and calls
   `chiaki_feedback_sender_set_controller_state` directly.
2. **Orion side**: Replace/augment `OrionInputClient::send()` in `OrionInputClient.cpp`
   to write to the named pipe instead of (or alongside) the ViGEm virtual controller.

### Chiaki Changes (lib/src/streamconnection.c)
```c
// Add a background thread that reads from the named pipe and feeds controller state
static void *orion_pipe_thread(void *user) {
    ChiakiStreamConnection *conn = (ChiakiStreamConnection *)user;
    HANDLE pipe = CreateNamedPipeW(L"\\\\.\\pipe\\OrionControllerInput",
        PIPE_ACCESS_INBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1, 256, 256, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE) return NULL;
    while (conn->orion_pipe_running) {
        if (!ConnectNamedPipe(pipe, NULL) && GetLastError() != ERROR_PIPE_CONNECTED)
            continue;
        ChiakiControllerState state = {};
        DWORD read;
        while (ReadFile(pipe, &state, sizeof(state), &read, NULL) && read > 0) {
            chiaki_feedback_sender_set_controller_state(&conn->feedback_sender, &state);
        }
        DisconnectNamedPipe(pipe);
    }
    CloseHandle(pipe);
    return NULL;
}
```

### Orion Changes (OrionInputClient.cpp)
Add a fallback path that writes to `\\.\pipe\OrionControllerInput` when the pipe exists.
If the pipe is not available, fall back to the existing ViGEm path.

### Latency Budget
- Current: Engine → ViGEm submit (~1ms) → Windows controller stack (~5-10ms) → Chiaki read (~2ms)
- Direct: Engine → Named pipe write (~0.1ms) → Chiaki feedback sender (~1ms)
- **Savings: ~7-11ms**

### Build Requirements
- Chiaki build environment (CMake, Qt6, ffmpeg, vcpkg)
- Build script: `C:\Users\Administrator\Desktop\chiaki_build.sh`

---

## Item 12: Frame-Locked Detection Sync

### Goal
Sync the CV detection pipeline to Chiaki's frame timestamps from the H.26x decoder,
making `frameAgeMs` exact instead of estimated. This eliminates a source of timing
uncertainty in the release lead calculation.

### Architecture
1. **Chiaki side**: In `videoreceiver.c` / `ffmpegdecoder.c`, attach the decoder's
   presentation timestamp (PTS) to each decoded frame. Expose via a shared memory
   ring buffer or named pipe alongside the video frame.
2. **Orion side**: In `chiaki_backend.py` / `wgc_backend.py`, read the PTS from the
   shared memory and use it as the exact frame timestamp instead of
   `time.perf_counter()` at capture time.

### Chiaki Changes (lib/src/ffmpegdecoder.c)
```c
// Attach PTS to the decoded frame output
typedef struct {
    uint8_t *data;
    size_t size;
    int64_t pts;  // presentation timestamp from the H.26x stream
    int64_t pts_wall;  // wall clock when this frame was decoded
} OrionDecodedFrame;
```

### Orion Changes (chiaki_backend.py)
```python
# When a decoded frame arrives, use the PTS as the exact timestamp
frame_ts = frame.pts / 1000.0  # PTS is in microseconds
frame_age_ms = (time.time() * 1000.0 - frame_ts)  # exact age
```

### Impact
- Current: `frameAgeMs` is estimated as `time.perf_counter() - capture_time`, which
  includes capture latency, decode latency, and queue latency. Error: ±5-10ms.
- Frame-locked: `frameAgeMs` is exact from the PTS. Error: ±0.5ms.
- **Savings: ~5-10ms of timing uncertainty in the release lead**

### Build Requirements
- Same Chiaki build environment as item 11
- Shared memory or named pipe for PTS delivery alongside video frames
