# Orion IP-Protection Packaging Plan

Status: **scoping / read-only research** (2026-07-17). Nothing here is built yet.
Goal: ship a single signed, standalone Orion release where the crown-jewel Python
readers are **compiled** (not shippable as readable `.py`), the algorithmic modules are
obfuscated/encrypted, and **no dev secrets** enter the bundle.

This plan is written against the pipeline that already exists — it is an *extension*, not a
rewrite. Route everything through `tools/package_orion_release.py`; do not hand-zip the repo.

---

## 1. Current packaging / release pipeline (as-built)

There **is** a real, working release pipeline. It is a native-C++ package assembler, **not** a
Python freezer. No PyInstaller / Nuitka / cx_Freeze / NSIS / WiX / Inno is wired in today (the
only PyInstaller mention is a stale comment in `tools/install_remote_play_backend.ps1:6`; the
only Nuitka artifact is a red-team sandbox experiment that is on the packaging deny-list).

Driver chain:

1. `scripts/verify_orion.ps1 -StrictSecurity` — the release driver.
   - `:81` configures a **separate production build tree**: `cmake -S native_orion -B native_orion\build_prod -DORION_PRODUCTION=ON`.
   - `:97` runs `windeployqt --release --qmldir native_orion\qml …\OrionNative.exe` (Qt 6.8.0 MSVC).
   - `:106` runs `python tools\package_orion_release.py --build-dir build_prod\Release`.
2. `tools/package_orion_release.py` (651 LOC) — assembles `release/orion-package/` from the
   build's `Release/` dir: copies the native binaries, writes `security_policy.json` +
   `release_manifest.json` (SHA-256 of every shipped file), zips a reproducible
   `release/orion-package-<ver>.zip`, builds/signs the Ed25519 **auto-update** manifest, then
   calls `tools/verify_release_integrity.py` as the final ship gate. Refuses to package a build
   not configured `-DORION_PRODUCTION=ON` (`require_production_build()`, lines 529–549).
3. `tools/security/pack_lethe_release.py` — optional wrapper that runs an **external commercial
   packer** (VMProtect/Themida-style) over the first-party PEs only, then re-audits. Currently
   **verify-only, no packer wired** (`release/orion-packing-report.json` shows
   `"(verify-only; no packer run)"`).

Native build output (`native_orion/CMakeLists.txt`): primary exe `OrionNative` (`:210`), plus
`OrionOwner` / `OrionStaff` / `OrionUpdater`, and 6 gameplay DLLs — `OrionCommon`,
`AutomationCore`, `VisionCore`, `RemotePlayCore`, `SecurityCore`, `UpdaterCore`. `windeployqt`
runs as a POST_BUILD step (`:374–385`); ViGEm/OpenCV/assets are copied in (`:324–368`). Release
hardening flags: `/GL /LTCG /guard:cf /CETCOMPAT`, ASLR/NX.

Docs: `docs/RELEASE_SECURITY.md` (authoritative security model + the deferred "Phase 8
obfuscation & packing" plan), `docs/PACKING_RUNBOOK.md`, `docs/CODE_SIGNING.md`.

### What ships readable vs compiled TODAY

- **C++ everything is already compiled** into the exe/DLLs (VisionCore.dll holds the native
  `MeterDetector.cpp`; SecurityCore.dll holds the licensing/entitlement gate).
- **App QML is already protected** — `strip_app_qml_source()` (lines 328–341) deletes the QML
  source from the package; the app loads compiled QML resources.
- **Python ships as PLAIN-READABLE `.py`.** `.pyc`/`.pyo` are in `FORBIDDEN_SUFFIXES`
  (`package_orion_release.py:118–119`) and `__pycache__` in `FORBIDDEN_NAMES`, so nothing is even
  byte-compiled today. `copy_runtime()` (lines 278–283) copies exactly two loose scripts into
  `release/orion-package/native_orion/backend/`:
  - `autogreen_sidecar.py`
  - `ps5_remoteplay_helper.py`

### Crown-jewel exposure + a packaging GAP (important)

The shipped `autogreen_sidecar.py` is a thin entrypoint. Its real logic is NOT self-contained:
`autogreen_sidecar.py:346` does `from remote_play_orchestrator import RemotePlayOrchestrator`,
and `_bootstrap()` (lines 32–37) injects the repo root onto `sys.path` at runtime to find it.
That transitively pulls in the crown-jewel modules **which the packager does not copy**:
`remote_play_orchestrator.py`, `simple_meter_reader.py`, `compressed_meter_reader.py`,
`luma_meter.py`, `meter_detector.py`, `pose_timing.py`, `controller_remap.py`, etc. `models/` is
**also not bundled** by any script.

Consequence: the current shipped package's Python auto-green path is **incomplete as packaged** —
it would only run if those 200K–300K modules + model weights were also dropped in as plain `.py`
/ `.pt`. This is simultaneously (a) the IP-leak risk to close and (b) a functional gap. Compiling
the sidecar with Nuitka fixes **both** at once, which is why this plan is low-regret.

> Note on the "crown jewels ship as source" premise: the named readers
> (`simple_meter_reader.py`, `compressed_meter_reader.py`, luma) live at repo **root** as `.py`
> and are imported by the sidecar, but they are **not currently in the shipped package** — the
> packager only grabs the two `backend/*.py` files. So today the leak is latent (the source is on
> the dev tree, not in `release/orion-package/`). Any release that actually runs the Python
> sidecar must ship them, and today that would mean shipping them readable.

---

## 2. Crown-jewel inventory

**Must protect (compile + obfuscate):**

| Module (repo-root unless noted) | LOC | IP it embodies |
|---|---|---|
| `simple_meter_reader.py` | 2,479 | **Production reader.** Shot-gated red-column reader; encodes the exact tuned params (RED BGR ranges, contour geometry gates, search band, confidence-decay lock) + matchTemplate NCC fast-path + green-tip read. ~1% LOC of the legacy chain at ~0.6ms/frame. |
| `compressed_meter_reader.py` | 748 | Chiaki/H.264-4:2:0 reader; adds luma-domain 4-param-logistic fill read, bright-tip anchor, encoder skip-MB stale gate. |
| `luma_meter.py` | 341 | Pure-numpy Y-plane math: K2 IRLS-Huber + Gauss-Newton logistic fill-boundary fit w/ analytic Jacobian; the most compression-durable measurement kernel. |
| `remote_play_orchestrator.py` | ~2,844 | Detection/timing orchestrator that drives the readers: reader selection, release/probe markers, RTT sync, stdout JSONL contract. Core integration IP. |
| `pose_timing.py` | 1,877 | Pose-driven release-timing engine (the "when to let go" logic). Core IP. |
| `controller_remap.py` | 1,375 | Stick/button remap + timing state machine (FSM). Algorithmic IP. |
| `tip_registration_infer.py` | 386 | scipy `least_squares` / Pchip curve-fit tip registration. IP. |
| `meter_detector.py` | 5,105 | Legacy serving chain (locator→loc_mem→park→T5). Still importable; the simple reader is its flag-selectable replacement. Ship only if a shipped flag still selects it. |
| `*_infer.py` heads (`meter_reader_infer`, `meter_locator_infer`, `meter_reader_y_infer`, `fill_forecaster_infer`) | ~120–160 each | torch/ultralytics inference wrappers. Protect **only if** their `ORION_*` flags ship enabled (see §3/§5). |

**Model weights (data IP, not code):** `models/*.pt`, `models/meter_reader.json`,
`meter_reader_y.*`, `tip_registration.json`, `hand_landmarker.task`, YOLO/RF-DETR/pose weights.
These are tuned artifacts worth protecting but **cannot be "compiled"** — see §5 model handling.

**Doesn't matter if readable (glue/config — leave as source or don't ship):**

- `native_orion/backend/autogreen_sidecar.py` — thin stdio/arg wrapper (becomes the Nuitka entry).
- `virtual_controller.py` — ViGEm/DS4 `ctypes` bindings; mechanical, reconstructable.
- `learning.json` — data/config, not an algorithm.
- Everything in `tools/`, `tests/`, `discord_launch/`, `backend/` (server) — **never shipped in the client bundle** regardless.

**Not a crown jewel to worry about: the licensing client.** There is **no Python license
client.** Activation/entitlement lives in native C++ `SecurityManager.cpp` (machine-bound,
DPAPI-sealed cache at `.vault/orion_entitlement.cache`, schema `orion.local_entitlement.v1`),
already compiled in SecurityCore.dll. Obfuscating Python does **not** touch the license gate — it
is already protected.

---

## 3. Nuitka feasibility

**Verdict: feasible, but NOT drop-in.** Three things must change; none is a blocker, and the
hardest one is avoidable by a shipping decision.

### 3a. Spawn path must change (small, but load-bearing)

The sidecar is launched today as **`python.exe <script.py>`**, not a frozen exe. Exact site —
`native_orion/src/RemotePlaySession.cpp`, `RemotePlaySession::startSidecar()` (line 1623):

```cpp
const QString py = pythonExecutable();        // 1629  -> resolves an interpreter
const QString script = sidecarScriptPath();   // 1630  -> .../backend/autogreen_sidecar.py
proc->setProgram(py);                          // 1646
proc->setArguments({ script,                   // 1647-1651
    QStringLiteral("--root"), root,
    QStringLiteral("--config-json"), QString::fromUtf8(cfgJson) });
proc->start();                                 // 1800  (+ CREATE_NO_WINDOW at 1658)
```

Interpreter resolution — `pythonExecutable()` (lines 1412–1472): tries `ORION_PYREMOTEPLAY_PYTHON`
env, `<root>/.venv311/Scripts/python.exe`, **`<appDir>/python/python.exe`**, home venvs,
Miniconda, then bare `python.exe`, probing each with `python -c "import cv2"`. Script resolution —
`sidecarScriptPath()` (lines 1489–1502).

To run a Nuitka **`OrionSidecar.exe`** instead, change `startSidecar()` to:
`proc->setProgram(<appDir>/OrionSidecar.exe); proc->setArguments({"--root", root, "--config-json", cfgJson});`
— i.e. drop the interpreter and the leading `script` arg. `pythonExecutable()` and the cv2-probe
become dead code for this path (keep them behind a dev fallback so `--allow-dev-build` can still
run the loose `.py`). **Caution:** this path is race-sensitive (watchdog restart timing in
`SidecarWatchdog.h`, warm-sidecar reuse, `restartSidecar()` at OrionAppController.cpp:2883/2945).
Preserve those semantics; only the program/args change. Same change applies to the
`ps5_remoteplay_helper.py` spawn (`runHelper()`, RemotePlaySession.cpp:1340–1410) if that is
compiled too. Note candidate `<appDir>/python/python.exe` already implies an intended
embedded-Python shipping slot — the compiled exe simply supersedes it.

### 3b. Dynamic imports — LOW risk

The runtime crown-jewel modules contain **no `importlib` / `__import__` / `exec` / plugin
loading** (all such hits are in `tests/` and `tools/diagnostics/`). Nuitka's static import
following will handle the normal `from remote_play_orchestrator import …` chain — **the
`_bootstrap()` sys.path hack becomes unnecessary** (Nuitka bundles the modules), though it can
stay harmless. Compile with `autogreen_sidecar.py` as the entry and let Nuitka follow imports; add
`--include-module` for anything reached only via the runtime sys.path trick if the analyzer misses
it.

### 3c. Heavy native deps — the real work, mostly avoidable

- **Always needed:** `numpy`, `cv2` (OpenCV), `pywin32` (`win32con`), `ctypes` (ViGEm). Nuitka
  standalone with numpy+cv2 is well-trodden but produces a **large** bundle (opencv is ~tens of
  MB). Use `--standalone` (+ optionally `--onefile` for the sidecar only) with
  `--enable-plugin=numpy` and explicit `--include-package=cv2`.
- **Optional / flag-gated (lazy, in-function imports):** `torch` (`pose_timing.py:225,475`,
  `meter_reader_infer.py:36`, `meter_reader_y_infer.py`), `ultralytics` YOLO
  (`meter_locator_infer.py:42`), `scipy` (`tip_registration_infer.py:76-77`). Nuitka will NOT
  auto-follow these deferred imports.

  **Decision lever:** the production readers (`simple_`/`compressed_`/`luma_`) are **numpy+cv2
  only**. If the release ships with the `ORION_METER_READER` / `ORION_METER_LOCATOR` /
  `ORION_TIP_REG` ML flags **OFF**, then **torch / ultralytics / scipy do not need to be bundled
  at all** — the Nuitka job collapses to numpy+cv2+pywin32, which is tractable and small(er).
  **→ Action item: confirm which `ORION_*` flags the production build ships enabled** (grep
  `buildSidecarConfig()` / the shipped `security_policy.json` / AppConfig). This single fact
  decides whether Nuitka is "a weekend" or "a saga."

- **Unknown to verify:** `hand_landmarker.task` in `models/` suggests a MediaPipe tasks path may be
  reachable from the pose code. MediaPipe is notoriously hard to freeze. **Confirm whether any
  shipped-enabled path imports `mediapipe`** before committing to Nuitka; if yes, treat it like
  torch (ship the flag off, or keep that one head as a separately-spawned interpreter).

**Net:** Nuitka is the right tool. Plan for it as **not drop-in**: budget the one native-code
spawn change (§3a) + confirming the ML flags are off (§3c) so the dependency graph is just
numpy/cv2/pywin32.

---

## 4. Secret-leak audit for the release build

Only **two live secrets sit in the working tree**, both in gitignored `*.local.ps1` files:

| Secret | Value | Location | Committed? | Naive folder-zip grabs it? |
|---|---|---|---|---|
| `ORION_DEV_KEY` (dev/master license) | `<redacted-dev-key-rotate-required>` | `orion-dev-key.local.ps1:11` | No — gitignored (`*.local.ps1`) | **YES** (highest-value leak) |
| `ORION_LICENSE_KEY` (test key) | `ORION-TEST-2026-0002` | `run_orion.local.ps1:23` | No — gitignored | **YES** (low value; also in committed test fixtures) |

Everything else is clean:

- **Discord bot token** — never in repo. `discord_launch/orion_bot.py:42` reads
  `os.environ["DISCORD_BOT_TOKEN"]`; server fetches it from SSM `/orion/discord_bot_token`. Only a
  placeholder template `run_bot.local.ps1.template` is committed. **Risk flag:** if an operator
  later fills `run_bot.local.ps1`, it is gitignored → a naive folder-zip would grab it. It is a
  server/bot file, not part of the client bundle, but the runbook should say "never zip the repo."
- **SSM / AWS** — all backend secrets are SSM Parameter Store **references (names only)** in
  `backend/lambda_function.py` (`/orion/token_secret`, `/orion/ed25519_private_key`, etc.), fetched
  at runtime `WithDecryption=True`. No `AKIA…`/`ASIA…` keys, no `.aws`, `.env`, `credentials.json`
  anywhere. These are **server** artifacts, never in the desktop client bundle.
- **Hardcoded keys in source** — none. `SecurityManager.cpp` only base64-encodes an
  already-encrypted local blob. The dev-key string appears in tracked source only in
  `tests/test_security_audit.py` (the audit's own fixture, explicitly allowlisted).
- **Update-manifest Ed25519 private key** — never in repo; loaded from a PEM path outside the
  package (env `ORION_UPDATE_SIGNING_KEY_PEM`; `package_orion_release.py:464–476` refuses a key
  inside the package dir), or signed server-side via SSM.
- **Public / safe to ship:** SPKI pins in `NetworkSecurity.cpp:40–56` (public issuer-CA hashes),
  the Ed25519 **public** update key, API Gateway base URL.

**Does the current packaging accidentally include any of these?** **No — if you use the tool.**
`package_orion_release.py` builds from `native_orion/build/Release`, not the repo, and its
`scan_forbidden()` + `scan_secret_content()` gates hard-fail (exit 2/3) on `.local.ps1`, `.env`,
`codesigning/`, `auth_tokens.json`, and on secret-looking **content** (delegating to
`tools/security_audit.py`, whose patterns include the literal `NVDEV-…`, PEM blocks, Stripe/
GitHub/AWS/Discord tokens). **The only real exposure is a hand-rolled "zip the folder" that
bypasses the tool** — which would grab both `*.local.ps1` secrets. Mitigation: ship exclusively
via the tool; the runbook must forbid manual zips.

---

## 5. Recommended plan (step-by-step)

### Phase A — Make the Python path complete & compilable (prereq)

1. **Confirm production reader flags.** Grep `buildSidecarConfig()` (RemotePlaySession.cpp) +
   shipped `security_policy.json` + AppConfig for `ORION_METER_READER` / `ORION_METER_LOCATOR` /
   `ORION_TIP_REG` / `ORION_COMPRESSED_READER` and any `mediapipe` path. **Goal: ship with the
   torch/ultralytics/scipy/mediapipe ML heads OFF** so the sidecar dep graph is numpy+cv2+pywin32.
   If a head must ship on, add it to the Nuitka include set (accept the size) — decide per-head.
2. **Enumerate the true transitive import set** of `autogreen_sidecar.py` (orchestrator + readers
   + timing + controller_remap + virtual_controller + rtt_sync_engine + stream_quality + any
   shipped `*_infer.py`). This is the compile unit.

### Phase B — Nuitka-compile the sidecar into `OrionSidecar.exe`

3. Build **one** Nuitka `--standalone` (optionally `--onefile`) target with
   `autogreen_sidecar.py` as entry, `--enable-plugin=numpy`, `--include-package=cv2`,
   `--windows-console-mode=disable` (matches CREATE_NO_WINDOW). Output `OrionSidecar.exe` + its
   dependency dir. Keep the `_bootstrap` sys.path hack as a no-op fallback.
4. **Compile `ps5_remoteplay_helper.py`** the same way (or leave as source — it is setup glue, low
   IP; decide by effort). Everything else Python (tools/tests/backend/bot) is **not shipped** —
   leave untouched.
5. **Do NOT try to compile model weights.** `models/*.pt/.json/.task` are data. Handling options,
   in increasing protection:
   - (min) Ship only the weights the shipped flags actually load; strip the rest (today `models/`
     ships nothing — so first make the shipped subset explicit).
   - (better) AES-encrypt the shipped `.pt/.json` at package time; the compiled sidecar decrypts
     in-memory with a key derived like SecurityManager's entitlement binding (machine-bound). This
     protects the tuned weights, which are real IP the compiler cannot cover.

### Phase C — Wire the native app to the compiled sidecar

6. Change `RemotePlaySession::startSidecar()` (RemotePlaySession.cpp:1646–1651) to spawn
   `<appDir>/OrionSidecar.exe` with `--root/--config-json` and no interpreter/script args; gate
   the legacy `python.exe <script.py>` path behind a dev/`--allow-dev-build` fallback. Preserve
   watchdog/restart/warm-reuse semantics. Rebuild native with `-DORION_PRODUCTION=ON`.

### Phase D — Assemble & sign the single distributable

7. **Extend `package_orion_release.py`:** copy `OrionSidecar.exe` (+ its Nuitka dep dir) and the
   encrypted `models/` subset into the package; **remove** the loose `.py` copy of the sidecar
   from the shipped tree (or keep only under a dev flag). Keep `.pyc/.pyo` forbidden — but add a
   **new gate that also forbids the crown-jewel `.py` names** (`remote_play_orchestrator.py`,
   `*_meter_reader.py`, `meter_detector.py`, `pose_timing.py`, `controller_remap.py`) anywhere in
   the package, so a readable reader can never slip in again.
8. **Authenticode signing plugs in AFTER copy, BEFORE manifest hashing.** Sign every first-party PE
   — `OrionNative/Owner/Staff/Updater.exe`, the 6 DLLs, **and `OrionSidecar.exe`** — with
   `signtool sign /fd SHA256 /tr <timestamp> /td SHA256 …` (EV cert on HSM per
   `docs/CODE_SIGNING.md`). Then `write_release_manifest()` hashes the **signed** bytes, and
   `verify_release_integrity.py` runs. (The existing packer wrapper
   `tools/security/pack_lethe_release.py` regenerates the manifest post-mutation — reuse that exact
   insertion order for signing; optionally run the commercial packer over the same first-party PE
   list, but **not** over `OrionSidecar.exe`'s numpy/cv2 native libs, which packers often corrupt.)
9. **Enable the tamper-lock only after signing is proven.** `SecurityManager::enforceAuthenticode()`
   (SecurityManager.cpp:577) reads `enforce_authenticode` from `security_policy.json` and defaults
   **OFF** (line 587); the lock fires only at cpp:300 when the flag is on AND the exe is
   signed-but-invalid — an unsigned build is never bricked. So: sign → verify a genuinely-signed
   release passes → flip `enforce_authenticode: true` in the packaged `security_policy.json`
   (currently written by `write_security_policy()`, lines 241–251, without the field). Ship a
   real (public-distributed) build with it **on**.
10. **The "single .exe" reality.** A literal one-file exe for the whole app is impractical
    (multi-DLL Qt app + chiaki + sidecar). Deliver instead **one signed installer** (Inno Setup or
    MSIX; MSIX/EV is what earns SmartScreen reputation per CODE_SIGNING.md) that lays down the
    signed folder package. The **sidecar** can genuinely be onefile (`OrionSidecar.exe`). Set
    expectations: "single signed installer that extracts a signed, self-contained runtime," not a
    literal monolithic exe.

### Nuitka feasibility — PROVEN (2026-07-17): verdict WEEKEND, not saga

A bounded experiment actually built the CV-path sidecar with Nuitka standalone. Result: **it works.**
- **Built** `autogreen_sidecar.exe` (→ `OrionSidecar.exe`) exit 0 on Nuitka 4.1.3 / MSVC 14.5.
- **196 MB** `.dist/` (cv2 124 MB, exe 24 MB with the 10 crown-jewel modules compiled in — **no loose
  `.py`**, numpy.libs 21 MB, python DLL). **torch/ultralytics/scipy/mediapipe/cuda NOT pulled** —
  confirmed in the dist listing and at runtime.
- **Ran headless** with `ORION_SIMPLE_READER=1`: loaded numpy/cv2/win32 from the bundle, built
  `SimpleMeterReader`, and failed only at the absent capture card (expected) — no import/DLL crash.
- **The critical lever the generic §3 command missed:**
  `--nofollow-import-to=torch,torchvision,ultralytics,scipy,mediapipe,onnxruntime` (+ the `*_infer`
  heads) — without it, Nuitka follows the in-function `import torch` (pose_timing.py:225) and bundles
  GBs. With it, the deferred ML imports are cut at the leaf and degrade gracefully via their existing
  try/except guards.
- **Working recipe** (scratch build script preserved): `python -m nuitka --standalone
  --assume-yes-for-downloads --enable-plugin=numpy --include-package=cv2 --windows-console-mode=disable
  --nofollow-import-to=torch,torchvision,ultralytics,scipy,mediapipe,onnxruntime
  -o OrionSidecar.exe native_orion/backend/autogreen_sidecar.py`.

Soft caveats: pin the build interpreter to **Python 3.13** (Nuitka 4.1.3 flags 3.14 as experimental,
though it built+ran); optionally ship `ORION_TIP_REG=0`/`ORION_FILL_FORECAST=0` to silence the
graceful-degradation warnings. Model weights still handled separately (§5 encryption).

**Confirmed Phase-A fact:** production `startSidecar()` (RemotePlaySession.cpp:1660-1717) sets NEITHER
reader flag → a clean shipped env defaults to the legacy `MeterDetector` chain. **The packaging pass
MUST set `ORION_SIMPLE_READER=1` in the shipped sidecar env** (alongside the §3a spawn change) so the
numpy+cv2 reader is the path — which is exactly what keeps the Nuitka bundle small.

### Phase E — Installer (the delivery mechanism; NOT a standalone exe)

**Decision (2026-07-17): ship an installer, not a monolithic exe.** This is forced, not stylistic —
the product loads **kernel-mode drivers that cannot be baked into any exe**: ViGEmBus (virtual pad),
HidHide (physical-pad cloak), and the already-vendor-signed WinDivert64.dll. A driver requires an
elevated install step; a bare standalone exe would launch and then fail the instant it needs the
virtual controller. The only genuine "standalone exe" in the design is the compiled `OrionSidecar.exe`
component *inside* the package.

11. **Build an Inno Setup installer (EV-signed).** Replaces the crude `Orion Setup.bat`. It must:
    - Run the **ViGEmBus** and **HidHide** driver installers as silent, elevated install steps
      (bundle their signed MSIs; skip/repair if already present).
    - Lay down the signed folder package from Phase D: `OrionNative.exe` + the 6 Orion DLLs + Qt
      runtime + `OrionStream.exe` (the fork) + `OrionSidecar.exe` + its Nuitka dep dir + the encrypted
      `models/` subset + assets. Include the VC++ runtime if not statically linked.
    - Create Start-menu + optional desktop shortcut, a proper **uninstaller**, and install into a
      fixed dir so the `OrionUpdater` auto-update path is stable.
    - Be **EV-signed** (`signtool` on the installer .exe too) so SmartScreen/Defender grant reputation
      — a raw Nuitka/packer exe trips SmartScreen until an EV cert earns trust (see `docs/CODE_SIGNING.md`).
    - **NOT MSIX** — its app-container sandbox fights kernel drivers, subprocess spawning, raw
      capture-card device access, and input injection. Inno Setup (or a classic elevated installer) is
      the correct model for a driver-backed, subprocess-spawning, device-accessing app.

### Final packaging red-team checklist

- [ ] `scan_forbidden` + `scan_secret_content` pass (already gated). Additionally: **strings-dump
      `OrionSidecar.exe` and grep for `NVDEV-`, `ORION-TEST-`, any key literal** — Nuitka embeds
      module constants; a secret pasted into a shipped `.py` would survive compilation.
- [ ] **No crown-jewel `.py` in the package** (new name gate): assert absence of
      `remote_play_orchestrator.py`, `*_meter_reader.py`, `luma_meter.py`, `meter_detector.py`,
      `pose_timing.py`, `controller_remap.py`, `tip_registration_infer.py`.
- [ ] No `.venv`/`python/` interpreter dir shipped (the compiled exe replaces it); no `*.local.*`.
- [ ] `OrionSidecar.exe` runs headless from the package dir, emits the JSONL contract, and the
      native app drives it end-to-end (warm preview → connect → detect → fire → release).
- [ ] Model weights shipped = exactly the set the shipped flags load; encrypted if that path chosen;
      no stray `.pt` from `models/`.
- [ ] `signtool verify /pa /v` passes on every first-party PE incl. `OrionSidecar.exe`.
- [ ] `release_manifest.json` hashes the **signed** bytes; `verify_release_integrity.py` green.
- [ ] `enforce_authenticode: true` present in the packaged `security_policy.json` for public builds.
- [ ] AV/SmartScreen smoke: install + launch on a clean VM; confirm the Nuitka exe + any packer
      output does not trip Defender (the runbook already lists "persistent AV false positives" as a
      release blocker).
- [ ] License gate unaffected: obfuscation is client tamper-resistance only; Owner/Staff authority
      still server-enforced (per PACKING_RUNBOOK).

---

## Risks & unknowns (soft = re-testable, hard = fundamental)

1. **[SOFT→HARD if ML on] Nuitka standalone dependency bundling.** numpy+cv2+pywin32 is tractable
   but large; **torch / ultralytics / scipy / (maybe) mediapipe are the hard part.** Mitigation is a
   *shipping decision*: ship the ML-infer flags OFF so the production numpy+cv2 readers are the only
   path — then Nuitka is straightforward. Risk stays soft **only if** §5-A1 confirms the flags are
   off; if a torch/mediapipe head must ship, it becomes a hard bundling/size problem for that head.
2. **[SOFT] Native spawn-path change on a race-sensitive surface.** The one-function edit to
   `startSidecar()` is small, but it sits in the watchdog/restart/warm-reuse hot path
   (SidecarWatchdog.h, restartSidecar). A regression there manifests as intermittent
   detection stalls, not a clean failure — needs the live/replay harness to validate, not just unit
   tests.
3. **[SOFT, process] Packaging completeness + model-weight IP + AV.** Today the packager ships
   *neither* the readers nor `models/`, so the Python path is currently incomplete — the plan must
   add them (compiled/encrypted) without regressing the ship gates. And **compiling code does not
   protect the `.pt` weights** (separate IP) — model encryption is a distinct decision. Plus
   Nuitka/packer output routinely trips SmartScreen/Defender until an EV cert earns reputation;
   budget a clean-VM AV pass and EV signing lead time.

---

## Phase-A finding (2026-07-17) — the reader-flag hinge, half-resolved

The sidecar's reader selection is in `remote_play_orchestrator.py:578-631`:
- `ORION_SIMPLE_READER=1` → `SimpleMeterReader` (numpy+cv2 only)
- `ORION_COMPRESSED_READER=1` → `CompressedMeterReader` (numpy+cv2 only)
- **neither set (DEFAULT) → `MeterDetector(...)` — the legacy locator chain that pulls torch/
  ultralytics.**

So the "weekend vs saga" question reduces to a single fact still to confirm: **does the shipped
`OrionNative` spawn the sidecar with `ORION_SIMPLE_READER=1` (or `ORION_COMPRESSED_READER=1`)?**
- If YES → the shipped dep graph is numpy+cv2+pywin32 and Nuitka is the tractable path. **This is
  almost certainly the intent** — the whole detection PIVOT (task #34) productionized the shot-gated
  SimpleMeterReader as the flag-selectable replacement for the chain.
- If NO (ships the default chain) → torch/ultralytics must be bundled (saga), OR the packaging step
  must set the flag as part of the shipped sidecar env.

**Action for the packaging execution:** find where `OrionNative` sets the sidecar `QProcessEnvironment`
(near `startSidecar()` in RemotePlaySession.cpp) or the shipped launch env, confirm the CV flag is on,
and if it isn't, MAKE it on for the release build. Do NOT begin the Nuitka compile until this is nailed
— it determines the entire dependency graph. (This edit was left for the focused packaging pass so it
lands with the native `startSidecar()` change, not piecemeal.)

---

Also worth stating plainly: **the Python-source leak is currently latent, not live** — the
crown-jewel `.py` sit on the dev tree but are not in `release/orion-package/`. And the **license
gate is already native C++**, so Python obfuscation is about protecting the CV/timing algorithms
and tuned weights, not the entitlement check.
