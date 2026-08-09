# NexusVision Launcher Rules

**MANDATORY LAUNCH PROTOCOL FOR ORION NATIVE**

Do not run OrionNative.exe directly and do not invent your own env setup. You MUST use the rig script run_orion.local.ps1 (or .vbs). Skipping it means missing DLLs, stale sidecars holding the capture device hostage, or triggering the inert YOLO chain.

**The script handles:**
1. Setting the full environment (ORION_SIMPLE_READER=1).
2. Prepending $env:PATH for Qt/OpenCV/Release DLLs.
3. Reaping stale instances (OrionNative, chiaki, OrionStream, OrionUpdater, orphaned python sidecars).
4. Launching the correct native_orion\build\Release\OrionNative.exe from the repo root.

### The Three Ways to Launch:

1. **Normal run (no console window) — default for live testing**
   - Execute: run_orion.local.vbs
   - Use this for a clean, windowless live batch. Transcripts are preserved to logs\launcher.log.

2. **Normal run, visible console — for debugging the launcher itself**
   - Execute: .\run_orion.local.ps1
   - Use this if the launcher itself misbehaves and you need to see the terminal output.

3. **Framedump run — capture every-frame dumps for detector batching**
   - Execute: .\run_orion.local.ps1 -Framedump
   - Sets ORION_FRAMEDUMP=1 (~30/s, raw-only, async writer, max 6000 into logs\diagnostics\framedump\session_<stamp>\).
   - *Note*: Capture starts at feed-live.
   - *Optional switch*: -Detdiag sets ORION_DETDIAG=1 at ORION_DETDIAG_INTERVAL=0.04 (~25 samples/s) for per-frame detection telemetry. (e.g. .\run_orion.local.ps1 -Framedump -Detdiag)

### Key Facts:
- **Never launch build_prod**: The script launches the dev/Release build. Do not use the PROD exe, it stalls on auth.
- **Missing Exe**: If it errors "OrionNative.exe not found", you must rebuild before relaunching.
- **Process Reaping**: The script handles cleanup automatically. You never need to manually hunt for a leftover sidecar.
- **Logs**: Watch logs\orion_native.log (engine/reader) and logs\launcher.log (launcher transcript).
