# Chiaki Present Stutter — single-threaded CPU bottleneck — GLM Handoff

## Symptom
Live feed is choppy. Root-caused with hard evidence (not inference):

- **Source is a clean 60fps.** chiaki's own session log
  (`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log`):
  `[libplacebo] Estimated source FPS: 59.999` — steady. PS5 + wired Ethernet are perfect.
- **But the present CRATERS in bursts:** same log, `display FPS:` swings `66 → 19.8 → 19.0 →
  13.6 → 20.0`, with absurd spikes (1309, 791) in between = burst-then-stall. The effective
  feed is ~13-38fps → the choppiness.
- **One core pegged, system idle:** with the stream running, **Total CPU 28%** but **core 11 at
  94%** (16 logical cores, the rest 5-46%). So it's a **single CPU-bound thread**, not overall
  saturation and not contention with OrionNative (which had crashed and wasn't even running).
- **HW decode is active but wasteful:** the log shows
  `Using hardware decoder "d3d11va"` followed by
  `Transferring hardware-decoded frames to software for presentation: d3d11` — i.e. a per-frame
  **GPU→CPU round-trip** instead of zero-copy. Resolution at the time was 720p (1280x720),
  export cap 60 — so neither resolution nor the export cap is the limiter.

## The ask
**Profile OrionStream to find which thread is the CPU-bound one** (pegging the core) and fix it.
Leading suspects, most likely first:

1. **The HW-decode "transfer to software for presentation" round-trip.** This is the opposite of
   zero-copy — it pulls each decoded frame GPU→CPU (and presumably back) every frame, which will
   peg a core at 60fps. This is essentially **Tier 2 item 2.2 (HW-decode zero-copy to placebo)**:
   keep the `d3d11va` frame on the GPU and hand it to libplacebo as a `pl_tex` without the CPU
   transfer. That alone may clear it. (Until then, software decode may actually be *less* CPU
   than HW+transfer — worth A/B-measuring, but it was also choppy, so the transfer isn't the
   whole story.)
2. **A busy-wait in the present pacing.** The Tier 1 hybrid sleep does a `_mm_pause` spin for the
   final ≤500µs in `throttleFramePresentation` (`qmlmainwindow.cpp`). If the coarse-sleep math is
   off and it falls into the spin too early/long, that spin pegs a core and disrupts pacing.
   Verify the spin window is actually ≤500µs under load; cap it hard.
3. **The OrionFrameExport path** — per-frame BGRA `sws_scale` + the pipe write on one thread. At
   60fps 1080p that can saturate a core; confirm it's not the pegged thread (it has its own slot).

Note the stutter reproduces on **both** the Tier 2 and the pre-Tier 2 binaries, so it's a
**baseline** issue, not something Tier 2 introduced — except the HW-decode transfer, which is
the freshest lead.

## Verify
- chiaki log `display FPS:` holds steady ~60 (no bursts down to 13-20).
- `nvidia-smi dmon -s u` shows NVDEC busy and **no single CPU core pegged** while streaming.
- Orion's "Capture health" `uniqfps` rises toward ~60 with low `dup%`, and the feed is visibly
  smooth at 60.
- Test with HW decode ON (after the zero-copy fix) — that's the target ship config.

## Build/deploy
Existing pipeline (`docs/TIER2_PROMPT_FOR_CLAUDE.md` / `docs/TIER2_PROMPT_FOR_GLM.md`), portable
`-march=x86-64-v2`, back up the deployed exe first.
