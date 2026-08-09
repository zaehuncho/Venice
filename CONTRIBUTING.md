# Contributing to Orion

Orion is a Windows C++/Qt + Python project (the native app `OrionNative`, the embedded custom
chiaki-ng stream `OrionStream`, and Python detection/automation sidecars). This guide gets a new
developer building, testing, and committing safely.

## 1. Prerequisites (Windows)
- **Qt 6.8.0** (`msvc2022_64`) — e.g. `C:\Qt\6.8.0\msvc2022_64`
- **MSVC 2022** (Visual Studio Build Tools) + **CMake**
- **OpenCV** (C++ build on PATH for the native app; Python `cv2` for the sidecars)
- **Python 3.14** at `C:\Python314` with `opencv-python` + `pytest` (this is the interpreter the
  tests + tools expect — it has cv2; do **not** rely on other interpreters)
- **MSYS2 / MinGW** — only if you rebuild the chiaki fork (`OrionStream`). The chiaki source lives
  **outside this repo** (`Desktop\chiaki-ng-src`) and builds with MSYS2/MinGW, **not** MSVC. Most
  contributors use the prebuilt `OrionStream.exe` and never touch this.

## 2. Build
```
cmake --build native_orion/build --config Release --target OrionNative
```
(First time: configure with CMake against `native_orion/CMakeLists.txt`.) Admin tools build via the
`OrionOwner` / `OrionStaff` targets; the full build also produces `OrionNativeTests`.

## 3. Test (run before every PR)
```
ctest --test-dir native_orion/build -C Release          # native unit tests (fast; mock clock)
C:\Python314\python.exe -m pytest tests/ -q             # Python suite
powershell scripts\verify_orion.ps1 -Python C:\Python314\python.exe   # full release gate
```
All three must be green. The native tests use an injectable mock clock, so they run in ~1s.

## 4. Secret hygiene — READ THIS
**Never `git add -A`.** The following must NEVER be committed (they are gitignored — keep it that way):
- `settings.json` (+ its signature), `learning.json` (runtime data)
- any vault, code-signing keys, license keys, `*.pem` / `*.key` / `*.p12`, `.env`
- model weights (`*.pt`) — share via an artifact store / Releases, not git
- built bundles / DLLs (`redteam_sandbox/`, deploy dirs)

**Dev keys** go in environment variables or a `*.local.ps1` file (gitignored) — never committed.
Production signing/license keys live with the maintainer / CI secrets only. Embedding a **public**
value (e.g. the Cloudflare cert-pin SHA256 in `NetworkSecurity.cpp`) is fine; a secret is not.

**Editing `settings.json`:** Python round-trip only (`json.load`/`json.dump`), with the app closed.
Do **not** edit it with PowerShell — PS 5.1 writes a UTF-8 BOM that silently breaks the Python and Qt
JSON parsers. The app re-signs it on next save.

## 5. Workflow
- Branch from `main` → open a **Pull Request** → review → squash-merge. Don't push to `main` directly.
- Keep commits scoped + conventional (`feat(...)`, `fix(...)`, `chore(...)`), like the existing log.
- Before the first push to a shared remote, run the secret audit (history + tree) — the repo is
  currently clean (51 commits, no secrets).

## 6. Layout (orientation)
- `native_orion/` — the Qt app (`src/`, `qml/`), `backend/autogreen_sidecar.py`, `build/`
- `meter_detector.py`, `remote_play_orchestrator.py`, `controller_remap.py` — detection/automation
- `tools/` — diagnostics, admin, packaging, training; `tests/` — pytest; `docs/` — design + runbooks
- `models/` — pose/bar weights (gitignored)
