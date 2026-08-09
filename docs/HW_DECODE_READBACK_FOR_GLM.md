# Unlock Smooth 1080p60 — Hardware-Decode Frame Export (d3d11 readback) — GLM Handoff

## Goal
Make Orion's live capture smooth at **1080p60** by enabling **hardware decode** (NVDEC via `d3d11va`)
WITHOUT blinding the bot. Today hardware decode is force-disabled because the frame export can't read
GPU-decoded frames, so Orion is stuck on **software decode — which tops out at ~38 unique fps**
(`uniqfps=36-40`, 33-40% duplicate frames at `target_fps=60`) → the choppy feed the user reported.
Add the **GPU→CPU readback** so the export pipe can carry hardware-decoded frames.

Build/deploy via the existing pipeline (see `docs/TIER2_PROMPT_FOR_CLAUDE.md` + `docs/TIER2_PROMPT_FOR_GLM.md`
— same env, same `OrionStream.exe` deploy target, **portable `-march=x86-64-v2`** ship build).

## Background (Orion side — already in place, DON'T change)
- Toggle: `AppConfigData::hardwareDecode` (default **false**, `native_orion/src/AppConfig.h:48`). When
  true, `native_orion/src/RemotePlaySession.cpp:825` sets chiaki `hw_decoder=d3d11va`; when false it's
  empty → software decode.
- The code states the exact gap: `RemotePlaySession.cpp:822` — *"1080p60 at ~24-30 uniq fps (the laggy
  preview)"* under software; `AppConfig.h:46` — re-enabling HW decode *"needs the OrionStream d3d11
  readback."* **That readback is this task.**
- Present path: `use_zero_copy = vulkanBackend && hardwareDecode` (`RemotePlaySession.cpp:850`) — the
  Vulkan present already consumes the GPU frame zero-copy for a smooth **display**. **Keep that.** Your
  readback is a SEPARATE path that feeds the EXPORT pipe; it must not disturb the present.

## The problem precisely
With `hw_decoder=d3d11va`, FFmpeg outputs each decoded `AVFrame` as a **GPU texture**
(`frame->format == AV_PIX_FMT_D3D11`; `data[0]` = `ID3D11Texture2D*`, `data[1]` = array-slice index),
carrying a `hw_frames_ctx`. The OrionFrameExport writes a **CPU** pixel buffer (BGRA), so with HW decode
it has no CPU data to read → nothing reaches the pipe → that's why the toggle is forced off.

## The fix (OrionStream / chiaki-ng-src)
In the frame-export path, when the decoded frame is a HW frame, do a **GPU→CPU readback before writing
to the pipe**:

1. Where the export receives the decoded `AVFrame` — `gui/src/orionframeexport.cpp` and/or the decode
   callback in `lib/src/ffmpegdecoder.c` — branch on `frame->format == AV_PIX_FMT_D3D11`
   (equivalently `frame->hw_frames_ctx != NULL`).
2. **If HW:** `av_hwframe_transfer_data(sw_frame, hw_frame, 0)` into a reused CPU `AVFrame` (NV12), then
   convert to the export's pixel format (BGRA — the same `sws_scale` the software path already uses) and
   write to the pipe.
   **If software:** keep the current direct path unchanged.
3. **Reuse buffers** — allocate `sw_frame`, the `SwsContext`, and any staging **once** and reuse per
   frame (no per-frame alloc). `av_hwframe_transfer_data` reuses `sw_frame`'s buffers.
4. **Off the present critical path** — run the readback on the export thread (OrionFrameExport already
   runs ABOVE_NORMAL on its own slot), NOT on the render/present thread. The present keeps zero-copy.

(Raw-D3D11 alternative if the FFmpeg transfer fights the shared device: `CopyResource` the decoded
texture into a persistent `D3D11_USAGE_STAGING` texture, then `Map` it. But `av_hwframe_transfer_data`
is preferred — it handles the staging + device for you. If you need the device, it's on
`frame->hw_frames_ctx` → `AVHWFramesContext` → `AVD3D11VADeviceContext`.)

## Verify
- Enable the Hardware Decode toggle in Orion; launch on the **decoder pipe** (NOT `ORION_FRAME_PIPE=0`).
- Sidecar "Capture health" shows **uniqfps ~55-60** (was ~38) with **low dup%**, and the displayed feed
  is smooth at 1080p60.
- The bot still detects — meter box draws, shots release — i.e. exported frames are valid BGRA with the
  right dimensions/stride.
- The present is still smooth (no regression from the readback contending with it).
- Test **both** states: hardwareDecode **ON** (new path) and **OFF** (software fallback must still work).

## Guardrails
- **Don't change the export pipe protocol** — the bot reads the same BGRA payload + the `pts` (uint64,
  µs) header. Same dimensions/stride contract.
- The readback adds ~1-3ms GPU→CPU per frame — acceptable vs software decode's whole-frame CPU cost.
  Keep it off the present thread.
- Keep software decode as the fallback (toggle must work both ways).
- Two builds as before (portable `x86-64-v2` ship + `znver3` dev).

## After you land it (Orion side — I'll handle)
Flip `hardwareDecode` on (or wire the default), remove the temporary `ORION_FRAME_PIPE=0` GDI workaround
in the launcher, and re-validate the shot timing at true 1080p60.
