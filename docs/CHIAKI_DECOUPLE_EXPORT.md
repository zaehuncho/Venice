# Chiaki Decoder-Path Frame Export

## Status

Implemented and rebuilt on 2026-06-19. Live occlusion validation remains required before release approval.

The patched source was recovered at `C:\Users\Administrator\Desktop\chiaki-ng-src`. Its existing build output exactly matched the previously deployed `OrionStream.exe` SHA256, proving it is the source tree used for the deployed Orion runtime.

## Root Cause

`OrionFrameExport::exportFrame()` previously ran from the QML frame-delivery callback after `chiaki_ffmpeg_decoder_pull_frame()`. Although this was before presentation, it still shared the QML frame worker that prepares frames for the renderer. Occlusion or a blocked renderer could therefore stop frame pulls and stall the export pipe.

## Implemented Design

- FFmpeg now drains decoded frames immediately after `avcodec_send_packet()` in `chiaki_ffmpeg_decoder_video_sample_cb()`.
- The decoder owns one latest-wins `AVFrame` slot for the renderer. New decoded frames replace stale unconsumed frames rather than creating latency.
- A synchronized optional decoded-frame callback invokes `OrionFrameExport` directly after `avcodec_receive_frame()` succeeds.
- Hardware-frame readback still occurs before the renderer imports the frame, preventing the previous Vulkan layout race.
- QML now only pulls the latest decoded frame for presentation; it no longer owns or triggers Orion export.
- Teardown detaches the decoded-frame callback before destroying `OrionFrameExport`, preventing callback/use-after-free races.
- The existing `OrionInputBridge`, frame-pipe protocol, Orion reader, and settings remain unchanged.

## Build Provenance

- Upstream: `https://github.com/streetpea/chiaki-ng.git`
- Upstream base: `4a0115e1`
- Orion branch commit: `b4695bf0`
- Generator: Ninja
- Compiler: MSYS2 MinGW64 GCC 16.1.0
- C language mode: C17
- Built target: `build\gui\OrionStream.exe`
- Deployed target: `native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`
- Deployed SHA256: `F38308E80941060CDC09DBC65C267D6B40520B31191F69DBD7D09F33AD861F82`

`tools\chiaki\build_orion_chiaki.ps1` now discovers the patched Chiaki-ng tree, validates both Orion hooks and the decoder-path callback, activates the installed MSYS2 toolchain, verifies required packages, builds, and deploys the executable. It refuses to build the legacy stock fork or an export implementation still coupled to QML.

## Validation Completed

- Existing deployed binary matched the recovered build-tree binary before modification.
- Patched `OrionStream.exe` built and linked successfully.
- Chiaki unit suite passed: `105/105`.
- Existing archives and `release\orion-package` were not modified.

## Verification Blocker — RESOLVED (not a hang)

The earlier `OrionNativeTests` "stall" was a measurement artifact, not a hang. Confirmed
`ctest -R OrionNativeTests --timeout 400` => **`Passed 207.72 sec`, 100% (1/1), exit 0**. The
binary is GUI-subsystem (no streamed per-case output) and real-time/timer-driven (~80 cases, ~79s of
`qWait` + engine-timer waits), so `ctest` prints `Start 1:` then ~3.5 min of silence; the isolated
`--timeout 60` rerun simply fenced it far below its ~208s runtime. `verify_orion.ps1`'s test step has
no short timeout (CMake default ~1500s), so StrictSecurity passes it if run to completion.

Fix applied: `scripts/verify_orion.ps1` now prints an expected-runtime note before both native-test
steps and bounds them with `--timeout 900` (catches a true hang without false-failing the slow pass).
Follow-up (quality, not release-blocking): make the suite fast + deterministic via an injectable
engine clock so tests advance time instead of `qWait` (~208s -> a few seconds).

## Required Live Test

1. Start an Orion Remote Play session with decoder-pipe capture active.
2. Confirm the log reports `OrionFrameExport: decoder-path hw readback active`.
3. Record decoder/export unique-frame rate for at least 60 seconds.
4. Cover the stream window, open a notification above it, and minimize/restore it.
5. Verify frame sequence continues increasing, `uniqfps` remains nonzero, no `screen_region` fallback occurs, and Orion does not enter frame-feed-stall safe mode.
6. Repeat with Vulkan and the configured fallback renderer.

Until that live test passes, the build is technically validated but not release-approved.
