# Orion Release Package — Footprint

What the shipped Orion runtime package actually weighs, why, and what the installer's
free-space check should reserve. Numbers are from the current build tree
(`native_orion/build/Release`, `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win`,
`models/`) measured 2026-07-17. Owned by `tools/package_orion_release.py`.

## TL;DR

| | size |
|---|---|
| **Trimmed shipped package (on disk)** | **~500 MB** |
| Naive worst case (models + debug DLLs + stale bak swept in) | ~1.35 GB |
| **Recommended installer free-space check** | **~800 MB** |

The trim removes ~880 MB of cruft a naive packager could sweep in: the 713 MB `models/`
dev-weight tree, a 121 MB debug OpenCV DLL, and ~46 MB of stale `.bak` binaries.

## What ships (trimmed, ~500 MB)

| component | ~size | notes |
|---|---:|---|
| Native app: exes + release DLLs (`OrionNative/Owner/Staff/Updater`, `*Core.dll`, `ViGEmClient`, `libcrypto-3-x64`, **release** `opencv_world4110.dll` 64.8 MB) | ~234 MB | build-root `.exe/.dll/.json` minus debug twins |
| Qt/QML runtime dirs (`qml/`, `platforms/`, `imageformats/`, `tls/`, ...) | ~14 MB | `TOP_LEVEL_DIRS` |
| `assets/` + `meter_styles/` | ~0.4 MB | pure-CV reader's style JSONs live in `meter_styles/`, not `models/` |
| Chiaki Remote Play client (`chiaki-ng-Win/OrionStream.exe` + its Qt) | ~250 MB | net of stale `.bak` (~46 MB excluded) |
| backend wrappers, policy/readme/release manifest | <1 MB | `autogreen_sidecar.py`, `ps5_remoteplay_helper.py` |
| **shipped timing data** | **<0.1 MB** | two JSON inputs copied only from the verified `OrionSidecar` bundle |
| **Total** | **~500 MB** | lands in the target 450–550 MB band |

## What is trimmed OUT (the cruft, ~880 MB)

### (a) Dev/experiment model weights — ~713 MB trimmed
`models/` holds 21 `.pt` weights, a `.task`, and small timing JSON inputs. The shipped
sidecar runs the pure-CV reader and dependency-free timing predictor; its compiled bundle
excludes `torch/ultralytics/scipy/mediapipe`. The packager never bulk-copies `models/`.
Only `tip_registration.json` and `latency_factory_prior.json` ship, and they enter the
release exclusively through the source-bound sidecar bundle and its verified file map.

| models/ file (family) | ships? | why |
|---|---|---|
| `orion_meter_*.pt` (n, 11n_v3, v4–v7, prev, baseline) | no | torch YOLO locator (`meter_locator_infer.py`), `ORION_METER_LOCATOR=0` default; torch excluded |
| `orion_pose2k_*.pt`, `orion_player_detect_v9*.pt`, `orion_bar_*.pt` | no | pose/stamina path (`pose_timing.py`, ultralytics) — only when `no_meter` + torch, neither ships |
| `meter_reader*.pt` / `meter_reader*.json` | no | torch reader head (`meter_reader_infer.py`), `ORION_METER_READER=0` default; JSON is torch-paired |
| `release_predictor*.pt`, `shot_start_classifier.pt`, `user_player_classifier.pt` | no | torch heads via `pose_timing.py`; dev/diagnostics only |
| `tip_registration.json` | yes | dependency-free timing input, hashed into and copied from the verified sidecar bundle |
| `latency_factory_prior.json` | yes | versioned zero-setup route prior, hashed into and copied from the verified sidecar bundle |
| `hand_landmarker.task` (7.5 MB) | no* | mediapipe hand model, loaded only by the pose path (`pose_timing.py`) which does not ship |
| `fill_forecaster/`, `meter_roi/`, `rfdetr_player_*` | no | torch / training / diagnostics only |

\* If POSE is ever shipped (torch+mediapipe bundled **and** `config.no_meter_enabled`
reachable), its runtime data must first be added to the sidecar build inventory and shared
release policy; it must not be copied directly from the repository model tree.

The pure-CV reader's real calibration inputs are `meter_styles/*.json` and
`calibration/meter_color_profiles.json` (both already shipped), **not** anything under
`models/`.

### (b) MSVC debug DLLs — ~122 MB
`opencv_world4110d.dll` (121 MB) and `opencv_videoio_msmf4110_64d.dll` (0.6 MB) are the
**debug** twins of their release DLLs. `is_debug_dll()` rejects a `<base>d.dll` when its
release `<base>.dll` sibling exists (context-aware, so `Qt6Gamepad.dll` — a real module
ending in 'd' with no `Qt6Gamepa.dll` sibling — is safe), plus a name-only
`opencv_*<digit>d.dll` pattern so the debug twin is still caught in a post-copy scan where
the release twin was already dropped. Wired into both copy filters and `scan_forbidden`.

### (c) Stale backup binaries — ~46 MB
`OrionStream.exe.bak*`, `.exe.tier2`, `.exe.running-old`, `.june30.bak`, ... in the chiaki
deploy tree. Already caught by `is_stale_binary_name` (unchanged here).

## HTTPS-only for emitted URLs (CRIT-2)
Any `artifact_url` the packager writes into the (signed) update manifest must be HTTPS.
`_validate_artifact_url()` fails loud locally on a non-`https://` URL, and the server
`/api/update` independently enforces HTTPS + an owned-host allow-list before storing a
manifest (`backend/lambda_function.py` `_artifact_url_allowed`). Confirmed: the URL surface
is HTTPS-only on both halves.

## Free-space requirement — ~800 MB
Install peak = downloaded zip artifact + extracted package coexisting on disk. The package
is ~500 MB installed; the DEFLATE zip of mostly-already-compressed binaries is ~250–300 MB;
peak during extract ≈ **~750–800 MB**. Reserve **~800 MB** in the installer's disk chip.

## Follow-up trims (not done here)
1. **Headless OpenCV in the compiled sidecar** — when `OrionSidecar` ships, its embedded
   Python `cv2` is ~124 MB; switching to `opencv-python-headless` drops it to ~40 MB
   (**~84 MB saved**). (Separate from the native app's `opencv_world4110.dll` C++ lib.)
2. **Qt de-duplication** — the native app and the bundled `OrionStream.exe` each ship their
   own Qt6 runtime (~62 MB of Qt6 DLLs in the chiaki tree). Sharing one Qt set would save
   **~62 MB**.

Together these two follow-ups would bring the shipped footprint toward ~350–400 MB.
