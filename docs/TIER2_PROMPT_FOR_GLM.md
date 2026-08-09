# Tier 2 Chiaki Build — GLM Handoff

## Mission
Finish the optimized `OrionStream.exe` (custom chiaki‑ng) and make it **shippable to any customer CPU**, then — as a separate, later track — add the pre‑encryption input hook.

The per‑item Tier 2 specs and the exact build/deploy/patch commands already live in
`docs/TIER2_PROMPT_FOR_CLAUDE.md` (env, Python build subprocess, deploy copy, patch‑script
conventions). **Use that file for the mechanics.** This doc sits on top of it and adds the
**critical corrections** + the **input‑hook track**. Where they disagree, this doc wins.

---

## 0. CRITICAL — make the build portable (do this FIRST; it's a ship‑blocker)

The current build uses `-march=znver3`. That bakes AVX2 / FMA / BMI2 / F16C into every
instruction stream unconditionally. On the dev 5800X it's fine — but the moment
`OrionStream.exe` runs on a customer CPU that lacks those (any pre‑Haswell Intel, low‑end /
Atom / Celeron, some laptops) it **crashes instantly with an illegal‑instruction fault
(SIGILL)** — there is no graceful fallback. Since `OrionStream.exe` ships as the production
decoder‑feed capture path, a `znver3` binary **will hard‑crash customers**.

**Fix — produce TWO builds from the same source tree:**

- **Shipping build (the default that gets deployed):** replace `-march=znver3` with
  **`-march=x86-64-v2`** (SSE4.2 / POPCNT baseline — runs on essentially every x86‑64 CPU
  since ~2009). The heavy hot paths — H.264/HEVC **decode lives in FFmpeg** and **AES in
  OpenSSL**, both of which do their **own runtime AVX2 dispatch regardless of `-march`** — so
  the real‑world perf loss vs `znver3` is negligible; only chiaki's own glue code drops to
  SSE4.2.
  - Alternative if you want a little more headroom and are OK dropping pre‑2013 CPUs:
    `-march=x86-64-v3` (AVX2 baseline). But `v2` is the safe "works for everyone" choice the
    user asked for — default to `v2` unless told otherwise.
- **Dev build (optional, local only):** keep a `-march=znver3` variant in a separate
  `build-orion-znver3` dir for the user's own 5800X testing. **Never deploy it to the package.**

**Exact change** (apply in BOTH `C:\Users\Administrator\Desktop\chiaki-ng-src\orion_build_optimized.bat`
AND the Python build command in `TIER2_PROMPT_FOR_CLAUDE.md`):

```
-DCMAKE_C_FLAGS="-march=x86-64-v2 -O3 -funroll-loops"
-DCMAKE_CXX_FLAGS="-march=x86-64-v2 -O3 -funroll-loops"
```

Then clean‑rebuild + redeploy (back up the current exe to `.bak` first, as you already do).
Sanity‑check no `znver3`/AVX‑512 codegen leaked in (`objdump -d` spot check, or just confirm
it launches on a non‑Zen3 target).

> Note: `orion_build_optimized.bat`'s closing `echo` lines still reference `chiaki.exe` and an
> old `NexusVision\chiaki\OrionStream.exe` path — the real output is
> `build-orion-optimized\gui\OrionStream.exe` and the deploy target is
> `native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`. Fix the echo text while
> you're in there so the script is self‑documenting.

---

## 1. Two corrections to verify on the existing Tier 1 patches

- **`ffmpegdecoder.c` thread count.** The build summary said `thread_count=1`, but
  `TIER2_PROMPT_FOR_CLAUDE.md` says `thread_count=2` + `FF_THREAD_SLICE`. Confirm it is the
  **slice‑threaded 2**: `FF_THREAD_SLICE` keeps latency low (no multi‑frame reorder buffer)
  while using 2 cores so 1080p60 decode never falls behind. If it's actually `1`, bump it to
  `2 + FF_THREAD_SLICE` — single‑thread + `LOW_DELAY` risks dropped frames on bitrate spikes.

- **`takion.c` TCP_NODELAY.** The patch sets `TCP_NODELAY` on `takion->sock`. Remote Play's
  controller feedback rides the **UDP** takion data channel, where Nagle / `TCP_NODELAY` don't
  exist — `setsockopt(IPPROTO_TCP, …)` on a UDP socket is a silent no‑op. Confirm `takion->sock`
  is actually the **TCP** control socket; if it's the UDP socket, that "20–40 ms Nagle" saving
  isn't real (harmless, just not a win). The genuine send‑side latency lever is the input hook
  in §3 — not this.

---

## 2. Tier 2 optimizations

Implement the 7 items from `docs/TIER2_PROMPT_FOR_CLAUDE.md` in the order listed there
(**9.3 → 6.5 → 13.1 → 3.3 → 2.1 → 7.1 → 2.2**). Two notes:

- **13.1 (PTS‑corrected release timing)** touches the **Orion repo**
  (`native_orion/src/AutomationEngine.cpp/.h`, `OrionAppController.cpp`) — that's inside
  `NexusVision`, version‑controlled. Keep it on its **own commit**; it's the highest
  autogreen‑accuracy win and is independent of the chiaki binary.
- **7.1 (PGO)** must be profiled+rebuilt on the **shipping `-march=x86-64-v2`** build, not
  `znver3`, or the profile bakes in `znver3` codegen you can't ship.

---

## 3. Pre‑encryption input hook (SEPARATE TRACK — after live timing validation)

**Answering the user's question directly:** the input hook is **not a separate binary** — it's
additional **source patches to the same `OrionStream.exe`**, built through the **same pipeline
you already have** (`patch_*.py` + the build script). So yes, it means "rebuild the custom
chiaki again," but incrementally — not a new project.

**What it buys:** today Orion's `AutomationEngine` drives a **ViGEm virtual DualSense** → chiaki
reads that pad on its input poll → encrypts → sends over UDP. The ViGEm round‑trip + chiaki's
poll interval add latency *and* jitter. The hook injects the controller state **directly into
chiaki's feedback sender, before encryption** — bypassing ViGEm and the poll. Lower latency,
no poll jitter, exact send‑timing, no physical‑pad leak.

**Approach (two‑sided):**
- **Chiaki side:** add an input source that reads controller state from the existing
  `\\.\pipe\orion_input` named pipe and feeds it into the feedback path in `feedbacksender.c`
  (the takion feedback send) **before the encrypt step**, instead of / in addition to the
  SDL pad input. Gate it behind an env flag (e.g. `ORION_INPUT_HOOK=1`) so a normal launch is
  untouched.
- **Orion side:** the orchestrator/engine writes the authoritative controller state to
  `\\.\pipe\orion_input` at release time; the ViGEm write becomes the fallback path.

**Sequencing (important):** do this **only after the current meter/skele timing loop is
validated live**. The hook changes the input path, which would confound timing validation if
done now. Keep **ViGEm as the env‑gated fallback** so a hook regression can't brick shooting.
This matches the architecture: decoder‑feed first (done) → input hook in a separate branch
after timing is locked.

---

## Guardrails
- **Do not change the pipe protocol** — `\\.\pipe\orion_frames` (frame header incl. the `pts`
  uint64, microseconds) and `\\.\pipe\orion_input`. The autogreen sidecar depends on both.
- **Keep ViGEm as the input fallback.** The hook is additive + env‑gated.
- **Two builds:** shipping = `x86-64-v2` (portable, deployed); dev = `znver3` (local 5800X only).
- **Back up** the deployed exe to `.bak` before each redeploy (you already do this).
- **Don't touch `settings.json` / `learning.json`** (signed / auto‑generated).
- After each change: build clean → deploy → the user runs a live streaming session to verify
  pacing + autogreen before moving on.
