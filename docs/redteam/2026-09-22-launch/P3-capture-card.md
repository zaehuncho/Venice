# P3 Capture card: reliability review (Claude, 2026-09-22)

Lane: P3 (product pipeline, capture card). ID prefix `CL2-P3-`. Read-only: no source, settings or git edits, no builds, no launch, and the capture device was not opened.

Scope: `capture_card_backend.py`, the capture code in `remote_play_orchestrator.py`, `native_orion/backend/autogreen_sidecar.py`, device selection and persistence (`logs/capture_card_resolved.json`, native `capture_card_index`), and the setup UI (`StreamSetupGate.qml`, `StreamSetupForm.qml`). Evidence: `logs/orion_native.log` and `.log.1` (09-21 18:56 to 09-22 22:31), plus `logs/orion_user.log`.

## Known history: is it actually closed?

| History item | State now | Evidence |
|---|---|---|
| OBS holding the card gives "no live device" | **Diagnosed, but only in the developer log.** The backend now tells "busy" apart from "absent" (`capture_card_backend.py:1059-1091`), but the customer never sees that text (see CL2-P3-005). While the configured card is busy, the candidate walk can also settle on a different device and stay there (see CL2-P3-001). | 0 "IN USE / NO SIGNAL" lines in either log (no incident in this window) |
| 120 Hz requested on an HD60 X gave 60 Hz and caused lates | **Closed for new installs.** The UI offers only 30 or 60 Hz (`StreamSetupForm.qml:323-349`), and the cadence re-lock follows the rate the card actually delivers (`capture_card_backend.py:1429-1456`). A settings file that already holds 120 is still honoured and the UI hides it (CL2-P3-008). | Every health line: `cap_req_fps=60`, `cap_mode=1920x1080@60/YUY2/buf-1`; 0 "cadence re-locked" lines |
| MSMF fallback bricked timing ("warm timing revoked") | **Closed for MSMF** (this was GM-010). `_reclaim_dshow_route_if_invalid` (`remote_play_orchestrator.py:6967-7031`) drops the card and retries DirectShow every 15 s, and a return to the configured route earns timing back from a cold start. The same trap is **still open for a DirectShow route on the wrong index**, because the reclaim returns at `:7014` whenever the API is DSHOW (CL2-P3-001). | 0 "warm timing revoked", 0 "route ADOPTED", 0 "attestation REFUSED" in both logs |
| Low disk disables framedumps | **Closed.** Framedumps are opt-in (`ORION_FRAMEDUMP`), with a 10 GiB / 2 % free-space floor, a duty-cycle cap and backoff (`remote_play_orchestrator.py:1442-1563`). | n/a |

## Pacing across recent sessions (grep -a "Capture health", 5 s windows)

| Session (UTC) | Windows | uniqfps min / p5 / median | dup% max (windows ≥5 %) | raw_fps min | raw_gap_max_ms p95 / max (windows >100 ms) | raw_late sum | Shots |
|---|---|---|---|---|---|---|---|
| 09-21 18:56-19:00 | 48 | 58 / 59 / 60 | 3 (0) | 59.8 | 24.5 / 24.6 (0) | 0 | few |
| 09-21 20:51-23:01 | 1546 | 26 / 58 / 60 | 7 (9) | 39.5 | 22.6 / 104.8 (1) | 72 | 55 graded |
| **09-21 23:06-23:21** | 178 | **7** / 50 / 60 | **53 (8)** | **34.1** | **105.6 / 207.2 (9)** | **647** | none |
| 09-22 03:06-03:26 | 227 | 0* / 58 / 60 | 100* (5) | 58.8 | 27.8 / 54.0 (0) | 69 | 27 graded |
| 09-22 21:46-21:50 | 33 | 0* / 25 / 60 | 100* (2) | 60.0 | 27.2 / 33.0 (0) | 3 | none |
| 09-22 21:55-22:31 | 339 | 54 / 59 / 60 | 5 (4) | 59.6 | 32.5 / 51.8 (0) | 119 | 361 graded |

\* `uniqfps=0 dup%=100` shows up only as isolated single windows at 60 raw fps (menus or loading screens). None lasted long enough to look like a wedge. No `stall=` attribution was non-zero, `gc2` was always 0, and no SUSPECT token appeared.

In short: the owner's HD60 X is clean in 5 of 6 sessions. The 09-21 23:06 session had a real delivery slowdown: the reader got 34-48 fps with gaps up to 207 ms, and the preview `source_fps` dropped to 51 at the same moment, so the drop was upstream of the detector. That session took no shots, so its effect on make rate is unmeasured (CL2-P3-004).

---

### [CL2-P3-001] high — A non-card device on DirectShow can become the session's video source, and nothing ever retries the real card
- Lane: P3
- Exploit / failure path:
  1. `start()` walks `_candidate_indices()` (`capture_card_backend.py:980-1003`) in this order: the configured index, the cached index, devices whose names look like a card, and then **every "unknown"-named device**. Only names in `_WEBCAM_NAME_HINTS` (`:45-47`) are skipped.
  2. Many real cameras classify as unknown: "Microsoft Camera Front/Rear" (Surface), "Integrated IR Camera", "Insta360 Link", "EMEET SmartCam", "Anker PowerConf C200", "NVIDIA Broadcast", "XSplit VCam". If the configured card is busy (OBS), has no signal (PS5 off or in rest mode, HDCP on) or has moved to another index after a replug, the walk opens them. Most 1080p webcams honour the 1920x1080 request and pass the 16:9 ≥720p contract (`:150-185`), so one of them gets accepted.
  3. Its route basis is `uncertain`, or `configured` when the persisted index itself has drifted onto it. The guard revokes timing (`remote_play_orchestrator.py:4452`, `:4563`), and in the drifted-index case the scope rejects it with `configured_index_not_a_capture_card` (`:815`).
  4. **There is no way out.** The backend is DSHOW and healthy, so `_reclaim_dshow_route_if_invalid` returns at `:7014` (`if api == 'DSHOW': return`). The self-heal detach needs `is_healthy()` to be False (`:5651`), and the 2 s retry needs `_frame_backend is None`. The session stays on the webcam until Venice restarts, even after the user closes OBS or wakes the PS5.
  5. The customer does see a message, the "TIMING DISABLED … close anything else using the capture card (OBS…)" text (`RemotePlaySession.cpp:2986-2991`). But it is wrong for this case: closing OBS does nothing, because Venice never goes back to the card. The preview shows the user's own camera, and the webcam LED comes on. This is the "camera turns on" privacy scare the name gate was written to prevent.
- Affected: `capture_card_backend.py` `_candidate_indices`/`start`, `remote_play_orchestrator.py` `_reclaim_dshow_route_if_invalid`
- Impact: on a customer PC with an unrecognised camera, one common start order (OBS open, or console asleep, when Connect is pressed) leaves the bot unusable for the whole session with a misleading message, and turns the webcam on. No wrong inputs: timing fails closed.
- Reproduction (high level): in a fixture, set `ORION_VIDEO_DEVICE_NAMES="Game Capture HD60 X|Microsoft Camera Front"` with index 0 configured. Stub `cv2.VideoCapture` so index 0 opens but never delivers a frame (busy) and index 1 delivers 1920x1080. `start()` returns True on index 1 with basis `uncertain`, and `_reclaim_dshow_route_if_invalid` then never detaches it.
- Required fix: (a) Never accept an `unknown`-named device automatically. Only the configured index (and only when its name classifies as a card) or card-named devices should be candidates, and unknowns should need an explicit pick in Setup. (b) Extend the reclaim so it also detaches when the route is invalid on DSHOW at a non-configured index, with the same 15 s cooldown. (c) Add `camera` to the webcam hints, or better, key on `card` names only.
- Verification test: a unit test for the fixture above that expects `start()` to return False, not to open index 1, and not to log "TIMING DISABLED". Plus a test that a DSHOW backend sitting on the wrong index is detached by the reclaim.
- Confidence: confirmed (code-proven path; the device names are probable examples)

### [CL2-P3-002] high — A capture card whose DirectShow name has no hint word never gets timing authority, and nothing tells the customer why
- Lane: P3
- Exploit / failure path: `_latency_route_scope` (`remote_play_orchestrator.py:812-817`) rejects the scope unless `_classify_name(names[index]) == 'card'`. The hint list (`capture_card_backend.py:43-44`) holds only elgato, cam link, hd60, avermedia, live gamer, game capture, capture, magewell and ezcap. Generic MS2109 or MS2130 dongles often enumerate as "USB Video" or "USB3 Video", and names like "Razer Ripsaw", "ShadowCast", "NZXT Signal 4K30" and "EVGA XR1" also classify as `unknown`. When the scope is empty, every attestation fails as `route_scope_rejected`. The bot then stays in "TIMING WARMING UP - SHOTS STAY MANUAL" for every session (this failure mode was seen live on 08-18 and 08-27; see the comment at `RemotePlaySession.cpp:597-602`). Setup still accepts the device, shows its video and lets the customer continue (`StreamSetupGate.qml:109-133`).
- Affected: `remote_play_orchestrator.py:_latency_route_scope`, `capture_card_backend.py:_CARD_NAME_HINTS`, Setup UI
- Impact: every customer with a card outside the hint list has a bot that never fires, on every session. There is no plain-language reason, so this turns into refund and support load.
- Reproduction (high level): run `_latency_route_scope` with `ORION_VIDEO_DEVICE_NAMES="USB Video"`, `ORION_CAPTURE_CARD_INDEX=0`, valid moniker IDs and console identity. It returns `''` with reason `configured_index_not_a_capture_card`.
- Required fix: once the customer has explicitly picked a device in Setup, trust that pick (the moniker SHA already carries the identity; the name class is only a heuristic). Use the classifier only for automatic selection. If a scope is still rejected, surface a Setup-level message ("Venice does not recognise <name> as a capture card; pick it explicitly or contact support").
- Verification test: a scope test where "USB Video" is the explicitly configured index and the scope comes back non-empty; plus a UI or log test that a rejected scope produces a user-log line.
- Confidence: confirmed (code path); which products are affected is probable

### [CL2-P3-003] medium — The device is stored as a bare index, so a replug or a new camera silently moves the selection
- Lane: P3
- Exploit / failure path: native persists `capture_card_index` as an int from 0 to 16 (`AppConfig.cpp:695`, `:1404`). The picker binds only `currentIndex` (`StreamSetupForm.qml:274-283`). `ORION_VIDEO_DEVICE_IDS` carries stable moniker SHAs (`VideoInputDeviceEnumeration.cpp`), but they are used only to key the timing scope, never to re-resolve the selection. `capture_card_resolved.json` saves `{index, name}`, and `_load_cached_index` (`capture_card_backend.py:463-469`) ignores the name. On the owner's rig the card sits at index 1, so whatever is at index 0 is one replug away from being "configured".
- Affected: `AppConfig.*`, `OrionAppController::setCaptureCardIndex`, `capture_card_backend.py:_load_cached_index`
- Impact: this is the entry condition for CL2-P3-001 and CL2-P3-007. After a USB replug, a new webcam or a driver update, Venice opens a different device than the one the customer picked.
- Reproduction (high level): save index 1, reorder `ORION_VIDEO_DEVICE_NAMES` so the card is at 0 and another device is at 1, and launch. The configured candidate is the other device.
- Required fix: persist the chosen device's stable moniker ID (and name) alongside the index. On launch, re-resolve the index from the ID, and fall back to asking the user when the ID is missing. Verify the cached name before using the cached index.
- Verification test: a unit test where the saved ID is re-resolved to its new index after the inventory is reordered.
- Confidence: confirmed

### [CL2-P3-004] medium — A badly degraded frame rate passes every health check and still gets fire authority
- Lane: P3
- Exploit / failure path: `_min_health_fps` defaults to max(12, 0.35 × fps), which is 21 fps at 60 (`capture_card_backend.py:553-556`). `is_healthy()` checks only that rate and a total stall (`:1571-1636`). `feed_healthy` is `integrity_healthy and not backend_frozen` (`autogreen_sidecar.py:2668-2671`), and `feed_frozen` is only ever set by the opt-in stall-reopen path (`ORION_CAPTURE_STALL_REOPEN` is off by default, `:575`). So a feed delivering 34-48 fps with 100-207 ms gaps (seen live 09-21 23:06-23:21: 9 windows over 100 ms, uniqfps down to 7, raw_late 647) counts as healthy, and the engine can fire through a 200 ms hole in the meter.
- Affected: `capture_card_backend.py:is_healthy`, the sidecar `feed_healthy` field, engine gating
- Impact: occasional lates or misreads during slowdowns, and no customer-visible warning. The cause of the 09-21 slowdown was not identified (not GC; the preview source dropped too, which points at the reader, CPU or USB).
- Reproduction (high level): feed the backend a fake capture delivering 40 fps with 150 ms gaps. `is_healthy()` stays True and `feed_healthy` stays True.
- Required fix: publish a per-shot capture-gap figure (the largest publication gap inside the arm-to-release window) to the engine. Fail the shot closed, or at least mark it, when that gap is over about 3 frame periods. Emit a plain-language user-log warning when `raw_gap_max_ms` stays above 50 ms for three or more windows.
- Verification test: a sidecar test in which a 150 ms gap inside the shot window yields `feed_healthy=false` or an abort reason; and a log test for the new warning.
- Confidence: probable (the pacing is confirmed in the logs; the shot impact is unmeasured)

### [CL2-P3-005] medium — The customer gets no plain-language explanation when capture fails
- Lane: P3
- Exploit / failure path: the busy, absent and invalid-format diagnostics (`capture_card_backend.py:1059-1091`) go only to the sidecar log and on to `orion_native.log`. Nothing in `native_orion/src` or `qml` parses them (grep for "IN USE", "NO SIGNAL" and "no live device" finds only a comment at `OrionAppController.cpp:8096`). `orion_user.log` holds zero capture-state events across its 3.7 MB. The preview only shows "No stream / Press Connect to start" (`RemotePlayPage.qml:407-428`). The only capture message a customer ever sees is the TIMING DISABLED banner, and that fires only on a route revocation.
- Affected: `RemotePlaySession.cpp` sidecar-line handling, `RemotePlayPage.qml`, the user log
- Impact: the most common first-run failures (OBS open, HDCP on, console asleep, a 640x480-only device) look like "Venice does nothing". That becomes a support ticket or a refund.
- Reproduction (high level): with OBS holding the card, open Venice. The preview stays on "No stream" and `orion_user.log` gains no capture line.
- Required fix: map the three backend diagnostics (busy/no-signal, absent, invalid:<reason>) to a status property and a user-log line, and show it in the preview placeholder ("Capture card in use by another app or no HDMI signal", and so on).
- Verification test: a sidecar-line parser test that each warning maps to the right status string.
- Confidence: confirmed

### [CL2-P3-006] medium — Setups the owner's rig has never exercised: MJPG cards, 1440p and 4K output, HDR, 30 Hz cards
- Lane: P3
- Exploit / failure path:
  - **MJPG:** MJPG is requested by default (`remote_play_orchestrator.py:3862`, `capture_card_backend.py:878-882`). The HD60 X ignores the request and negotiates YUY2 (every health line reads `/YUY2/`), so the MJPG path (4:2:0 plus JPEG artefacts, with decoding on the reader thread) has never run on the reference rig. Earlier work found that the green tip does not survive re-encoding. Nearly every cheap USB card will run MJPG.
  - **Cards that pass through 4K or 1440p without scaling:** the contract accepts any 16:9 frame of at least 720p, so a 3840x2160 frame gets a 24.9 MB owned copy plus a verify compare (`_isolate_immutable_frame`) on the reader thread, and a resize to 720p after that (`remote_play_orchestrator.py:220-255`). Geometry is safe; CPU cost at 60 fps has not been measured.
  - **HDR:** nothing detects an HDR or tone-mapped feed, and the Setup hint mentions only HDCP (`StreamSetupForm.qml:314`). The meter reader keys on colour (White or green tip), so a washed-out HDR capture could break green and fill reads without failing closed.
  - **30 Hz-only cards:** these are accepted, and re-lock and the health floor handle them, but timing resolution halves. Untested for make rate.
- Affected: `capture_card_backend.py:_open`, the detector colour thresholds, Setup copy
- Impact: customers on untested hardware may get silent misreads or high CPU.
- Reproduction (high level): framedump or replay with MJPG-compressed 1080p frames and with an HDR-to-SDR tone-mapped capture, and grade the reader against YUY2.
- Required fix: before launch, either (a) publish a supported-cards list and add "PS5 HDR off, 1080p output" to the Setup hint, or (b) run one replay A/B of an MJPG and an HDR capture. Add `hdr_suspect` and `fourcc` to the user-visible Setup status.
- Verification test: a replay grade of an MJPG capture against the YUY2 baseline, within an agreed tolerance.
- Confidence: speculative (untested, not observed failing)

### [CL2-P3-007] medium — On a two-card rig, a second card can be adopted "by name" and read in place of the configured one
- Lane: P3
- Exploit / failure path: if the configured card has no signal or is busy, the walk reaches another card-named device, gets `route_basis='card_name'` (`capture_card_backend.py:1037-1040`), and the guard ADOPTS it with cold timing authority (`remote_play_orchestrator.py:4412-4447`). The adoption was built for enumeration drift on the same physical card. It cannot tell that apart from a genuinely different second card, such as a Cam Link on a face camera, a second console or a PC-capture card.
- Affected: `remote_play_orchestrator.py:_guard_capture_latency_route` adoption branch
- Impact: usually the bot just sits idle on a meterless feed. If the second feed shows a 2K meter (a streamer with two consoles), the bot could fire into the Remote Play-controlled console off the wrong feed, which is a damaging wrong input (a niche case).
- Reproduction (high level): with two card-named devices in the fixture, make the configured one busy. The backend resolves the other with basis `card_name`, and the guard logs "route ADOPTED".
- Required fix: allow adoption only when the adopted device's moniker ID equals the persisted ID of the configured card (which needs CL2-P3-003). Otherwise treat the device as `uncertain`.
- Verification test: an adoption test in which two distinct IDs share a card-class name and the adoption is refused.
- Confidence: speculative

### [CL2-P3-008] low — A saved 120 Hz setting is still used while the UI shows "60 Hz (recommended)", and the re-lock log line prints the wrong rate
- Lane: P3
- Exploit / failure path: `snappedCaptureCardFps` still allows {30, 60, 120} (`AppConfig.h:97-108`, `AppConfig.cpp:1410`), and the Python mirror does the same (`remote_play_orchestrator.py:644`). A beta settings file holding 120 is therefore still requested. `fpsLabel()` maps anything other than 30 to "60 Hz (recommended)" (`StreamSetupForm.qml:33-35`), so the customer cannot see it or fix it. Re-lock corrects the timing grid. But its warning is emitted after `self._fps` has been overwritten (`capture_card_backend.py:1446-1456`), so it reads "requested 60 fps but the device delivers 60.00 fps", and it tells the user to "Pick the delivered rate in Setup", which already appears selected.
- Affected: `AppConfig.h/.cpp`, `StreamSetupForm.qml`, `capture_card_backend.py:1446-1456`
- Impact: a latent return of the 120 Hz trap for beta testers who carry old settings, with a misleading diagnostic.
- Reproduction (high level): load settings containing `capture_card_fps: 120`. The log shows "fps=120 (requested)" while Setup shows 60.
- Required fix: drop 120 from the allowed set (load-time migration to 60), and log `self._fps_requested` in the re-lock message.
- Verification test: an AppConfig load test (120 becomes 60) and a re-lock log assertion.
- Confidence: confirmed

### [CL2-P3-009] low — The MSMF fallback reuses the DirectShow index number, so it can open a different device between reclaims
- Lane: P3
- Exploit / failure path: `_open` tries DSHOW and then MSMF on the same integer (`capture_card_backend.py:866-872`). MSMF numbers devices in its own enumeration, as the reclaim docstring at `remote_play_orchestrator.py:6973-6975` acknowledges, so MSMF index N can be a webcam. Timing fails closed, and the reclaim retries every 15 s, but between reclaims the preview and detector can run on the wrong camera and the webcam LED can blink.
- Affected: `capture_card_backend.py:_open`, `_api_order`
- Impact: a cosmetic and privacy scare. No wrong input.
- Required fix: only fall back to MSMF when the MSMF device's symbolic link or friendly name matches the DSHOW name at that index, or skip MSMF entirely when names are known.
- Verification test: a fixture in which DSHOW fails and the MSMF index maps to a webcam name, and the fallback is refused.
- Confidence: probable

### [CL2-P3-010] low — `capture_card_resolved.json` lives in the module directory and its name field is ignored
- Lane: P3
- Exploit / failure path: `_INDEX_CACHE` sits under `<module dir>/logs` (`capture_card_backend.py:54`). In a Program Files install or a frozen sidecar, that folder is unwritable or temporary, and `_save_cached_index` swallows the error (`:472-479`). When it is written, the saved `name` is never checked on load.
- Impact: the cache does nothing useful in production, or goes stale with no name check (this feeds CL2-P3-003).
- Required fix: move the cache to `%LOCALAPPDATA%\NexusVision\Orion Native\`, store the moniker ID, and verify it on load, or delete the cache in favour of CL2-P3-003.
- Verification test: a unit test that a cache whose name does not match is ignored.
- Confidence: confirmed

---

**Verdict: needs changes**

Top 5 fixes in priority order:
1. **CL2-P3-001:** never auto-accept unknown-named devices, and extend the DSHOW reclaim to cover the wrong index. This removes the session-long webcam lock and the misleading "close OBS" message.
2. **CL2-P3-002:** trust the customer's explicit Setup pick for the timing scope, or at minimum tell them why timing never starts. Otherwise every non-hint-named card is a bot that never fires.
3. **CL2-P3-005:** show busy, no-signal and invalid-format in the preview and the user log.
4. **CL2-P3-003 (then CL2-P3-007):** persist the device's moniker ID, re-resolve the index from it, and adopt a different index only when the IDs match.
5. **CL2-P3-004:** add a per-shot capture-gap gate or flag and a user warning for a sustained slow frame rate. Then run the MJPG and HDR replay A/B (CL2-P3-006) before advertising cards other than Elgato.
