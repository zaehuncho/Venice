# P-C capture identity patch (RT-HIGH-06, RT-MED-05; CL3-F5-002/003/006/007/008)

Owner of this patch: agent P-C. Date: 2026-09-23. Not committed. No native build and no rig run was done.

## Files changed

| File | Change |
|---|---|
| `capture_card_backend.py` | Live DirectShow inventory (ctypes COM, bounded to 1.5 s, same ID scheme as native). Refresh before and after every reopen. `identity_failure()` added. The selected ID is authoritative in `_candidate_indices` and `identity_verified`. Narrower card-name hints. The 60 fps validated-rate gate in `qualify_capture_feed`. New notices `wrong_device` and `preview_only`. `DUPLICATED_PERSIST_WINDOWS`. |
| `remote_play_orchestrator.py` | Only the capture parts: `_start_frame_backend` passes `refresh_inventory` on every open after the first. The route-guard adoption rule honours the pick. `_latency_route_scope` rejects a non-selected card-named row. `_update_capture_feed_qualification` handles `wrong_device` and the persistence of "duplicated". |
| `tests/test_capture_identity_pc.py` | New file with 38 hermetic tests. It never opens a device and never enumerates. |
| `tests/test_capture_qualification_cl2_p3.py` | Three edits for intended policy changes: 30 Hz is now `preview_only`; name identity needs exactly one card-named device; the relay and deny-list contract covers the two new codes. |

Unchanged: `native_orion/src/RemotePlaySession.{h,cpp}` and `native_orion/backend/autogreen_sidecar.py`. The sidecar refreshes its own inventory, and `_capture_notice_wire` already relays any code. **Nothing native needs a build for this patch.** `OrionSidecar.exe` does need a rebuild: `capture_card_backend.py` and `remote_play_orchestrator.py` are bound in the sidecar bundle manifest, so the packager correctly refuses a stale sidecar until it is rebuilt.

## Test commands and counts

Command, run from the repo root:

`.venv\Scripts\python.exe -B -m pytest <files> -q -p no:cacheprovider --basetemp C:/Users/aaron/AppData/Local/Temp/pc-tests`

- **Fail before:** the new test file run against the HEAD copies of `capture_card_backend.py` and `remote_play_orchestrator.py`, in a scratch dir (`scratchpad/failfirst`, `PYTHONPATH` scratch first): **28 failed, 10 passed**. The 10 that pass are positive controls, for example "real cards still classify as cards" and "a healthy 60 fps feed qualifies".
- **Pass after:** `tests/test_capture_identity_pc.py` gives **38 passed**.
- **Regression set:** 19 related files, **369 passed, 1 deselected**. The files are:
  - `test_capture_card_backend`, `test_capture_qualification_cl2_p3`, `test_capture_timing_epoch`, `test_capture_fps_env`
  - `test_capture_cadence_relock`, `test_capture_pts_continuity`, `test_capture_pts_lock`
  - `test_capture_publication_lifecycle`, `test_capture_health_diagnostics`
  - `test_orchestrator_capture`, `test_dual_capture`, `test_capture_identity_pc`
  - `test_remote_play_frame_pipe`, `test_decoder_pipe_identity`, `test_green_zone_window`, `test_release_oracle`
  - `test_route_transition_source_contract`, `test_xbox_remote_play`, `test_controller_route_lifecycle_contract`
- **Deselected:** `-k "not delivers_bgr_frame_if_device_present"`. That test opens the real card at index 0 and must not run while the owner may be playing.

## Findings

### 1. RT-HIGH-06 / CL3-F5-008: the inventory is refreshed on every reopen; authority requires the selected ID on the opened device

- **How reopens work now:**
  - The first open in a sidecar uses native's launch inventory, which is enumerated just before the sidecar is spawned.
  - Every later open (2 s card retry, self-heal, DSHOW reclaim) builds `CaptureCardBackend(refresh_inventory=True)`.
  - That backend enumerates live, then opens, then enumerates again.
  - The opened index must carry the same stable ID in both snapshots. With a pick, it must be the **selected** ID. Otherwise `identity_failure()` returns `wrong_device`: preview stays up, fire authority is denied, and this notice is shown: "The device Venice opened (X) is not the capture card you picked, so the bot will not shoot. Check the card's USB cable, then pick your card in Stream Setup and press Refresh."
- **Refresh failure fails closed:** a timeout, COM error or wedged walk removes both env keys. Identity and the route scope then fail closed, and the device runs preview-only.
- **Refreshed inventory is shared:** it is written into `ORION_VIDEO_DEVICE_NAMES/IDS`, so the route scope and notices read the same inventory as the backend.
- **Selected ID decides:** with a pick, `identity_verified()` is True only when `ids[opened_index] == ORION_CAPTURE_SELECTED_ID`, whatever the route basis says. Codex repro 1 (selected A busy, card B opened) now returns False with `wrong_device`.
- **Tests:**
  - `test_reopen_uses_a_fresh_inventory_not_the_launch_snapshot` (the F5 `f5_stale_inventory` fixture: the webcam is never opened, the failure is `absent`, and no other device is named)
  - `test_reopen_refinds_the_selected_card_by_id_after_replug`
  - `test_reopen_with_failed_enumeration_fails_closed`
  - `test_device_swapped_during_open_is_denied_authority`
  - `test_orchestrator_requests_a_fresh_inventory_on_every_reopen`
  - `test_selected_pick_never_verifies_a_different_card_named_device`
  - `test_wrong_device_notice_is_plain_and_published`
  - `test_python_stable_id_matches_the_native_scheme`
  - `test_live_enumeration_is_bounded_and_never_stacks`
- **Read-only check on this PC:** the enumerator was run once. It only reads the property bag; nothing was bound or opened. It took 12 ms and listed `Logitech Webcam C925e` (webcam), `Elgato HD60 X` (card) and `OBS Virtual Camera` (webcam). **No saved `capture_card_device_id` exists** in either settings file, so byte parity between the Python and native IDs is shown by construction and a unit vector, not yet against a real native-written ID. See the rig tests.
- **Residual risk:**
  - The open is still by index (cv2), not moniker-bound (`IMoniker::BindToObject`). The enumerate → open → enumerate bracket narrows the window to the open itself. A device swap that exactly restores the ID order within that window cannot be seen.
  - The opt-in in-place stall reopen (`ORION_CAPTURE_STALL_REOPEN`, default OFF) reopens the same index without a refresh.
  - A non-ASCII moniker path could fold differently in Python and Qt. That would only cause a mismatch, which denies authority.

### 2. RT-HIGH-06 / CL3-F5-007: a busy explicit pick never falls through; narrower hints

- **Candidate rules:**
  - With a selected ID, the only candidate is the index carrying that ID. A busy or asleep pick fails with the existing "busy" notice, which names the picked device.
  - No other device is opened, even a card-named one.
  - Unselected installs fall back by name only when **exactly one** device is card-named.
  - The cached last-good index no longer chooses anything.
- **Route guard and scope:**
  - The guard adopts `card_name` only when no pick exists.
  - `_latency_route_scope` rejects a card-named row that is not the selected ID (`capture_device_is_not_the_selected_card`).
- **Name hints:**
  - "capture" now matches only as a whole word.
  - Added to the webcam/virtual deny list: screen and desktop capture filters, virtual cameras (vcam, ManyCam, "virtual", NVIDIA Broadcast), and AVerMedia webcam lines ("live streamer cam", "avermedia pw", " pw3", " pw5").
- **Tests:**
  - `test_busy_explicit_pick_never_falls_through_to_a_card_named_device`
  - `test_explicit_pick_candidates_are_the_selected_id_only`
  - `test_screen_filters_and_vendor_webcams_are_never_cards[9 names]`
  - `test_real_capture_cards_still_classify_as_cards[6 names]`
  - `test_unselected_name_fallback_needs_exactly_one_card`
  - `test_route_scope_refuses_a_card_named_row_that_is_not_the_selected_id`
  - `test_route_guard_never_adopts_a_card_name_resolution_against_a_selection`
- **Residual risk:**
  - An unselected install with two card-named devices now gets no timing until the customer picks one. The existing "Select your capture card" gate covers this.
  - A future card whose name contains "virtual" would classify as a webcam; picking it explicitly by ID still works.

### 3. RT-MED-05 / CL3-F5-006: timing authority only at 60 fps

- **Behaviour:**
  - `qualify_capture_feed` returns `(False, "preview_only", ...)` for any requested rate other than 60, which covers 30 Hz and a 120 request. This cannot be tuned by env.
  - The orchestrator shows this notice once: "Your capture card is set to 30 Hz. Shot timing is only tested at 60 Hz, so this is preview only and the bot will not shoot. Set Refresh rate to 60 Hz in Stream Setup and reconnect."
- **Tests:**
  - `test_30hz_request_never_earns_timing_authority`
  - `test_preview_only_notice_names_the_setting`
  - `test_orchestrator_keeps_a_30hz_session_preview_only`
  - The existing `test_explicit_30hz_pick_is_judged_on_its_own_grid` became `test_explicit_30hz_pick_is_preview_only`.
- **Residual:** the Stream Setup picker still offers "30 Hz" with no warning. That is QML, owned by another agent. Recommended copy: "30 Hz (preview only)", or remove the option.

### 4. CL3-F5-002: the saved ID re-finds the card after a between-session reorder

- **Behaviour:**
  - The candidate walk searches the fresh inventory for the selected ID, not just the saved index.
  - `route_basis` is `configured` for the index carrying that ID, and `identity_verified` is True.
  - The route guard adopts that resolution cold and rewrites `ORION_CAPTURE_CARD_INDEX` so the scope keys on the adopted row.
  - An absent pick names no device, which fixes the misleading "reading a camera that isn't your capture card (Integrated Webcam)".
- **Tests:**
  - `test_between_session_reorder_is_resolved_by_saved_id` (the F5 `f5_reorder` fixture: candidates `[1]`, basis `configured`, identity True)
  - `test_route_guard_adopts_a_selected_id_resolution_cold`
- **Residual:**
  - Native still persists the stale index, and the picker binds by index (AppConfig and QML belong to other agents).
  - Each reordered session therefore starts COLD, because adoption revokes the warm posterior, until the customer re-picks.
  - Recommended follow-up: native re-resolves `captureCardIndex` from `captureCardDeviceId` at launch and persists it.

### 5. CL3-F5-003: the "duplicated" alarm must persist

- **Behaviour:**
  - A `duplicated` verdict counts only after `DUPLICATED_PERSIST_WINDOWS = 5` consecutive 1 s verdicts.
  - Before that, a qualified feed keeps authority and no notice is shown.
  - A feed that is not yet qualified still cannot qualify on those windows, so this fails closed.
  - The existing fail-run then revokes and warns (about 6 s for a card that genuinely pads 30 fps to 60).
- **Tests:**
  - `test_transient_duplicated_content_on_a_healthy_card_is_silent` (the live 09-23 shape: raw 60, dup 40 %, unique 36, 3 windows)
  - `test_persistent_frame_doubling_still_revokes_and_warns`
  - `test_a_doubling_card_never_qualifies_from_cold`
- **Residual:** a card that truly pads frames, and turns bad after it has qualified, keeps authority for about 5–6 s. The fps and gap-p95 checks are unchanged and still revoke within about 2 s.

## Rig tests the owner must run (packaged build, rebuilt sidecar)

1. **ID parity (do this first):**
   - Pick the HD60 X in Stream Setup and press Refresh, so native writes `capture_card_device_id`.
   - Then run `.venv\Scripts\python.exe -B -c "import capture_card_backend as c; print(c._enumerate_video_inventory())"`.
   - The Elgato row's ID must equal the saved `capture_card_device_id`. If it does not, every reopen would deny authority (fail closed, but timing would drop after the first reopen).
2. **Unplug and replug mid-session with the C925e attached.**
   - Expect authority revoked and no webcam LED.
   - Expect the card re-found by ID (a different USB port is fine) and a "Capture feed QUALIFIED" line again.
   - Expect no `wrong_device` notice once it is back.
3. **OBS holding the HD60 X, with the HD60 X picked:** expect the "busy" notice and that no other device opens (webcam LED off).
4. **Two capture devices (or HD60 X plus a Cam Link), second one picked:** timing runs only on the picked one. With the picked one busy, nothing else opens.
5. **Reorder between sessions:** plug the webcam into an earlier port so it enumerates first, then launch. The card must be found by ID, the log should read "resolved the capture card BY SELECTED ID", and timing runs cold.
6. **30 Hz selected:** expect the preview to run, the `preview_only` notice once, and zero bot releases. Switch to 60 Hz and reconnect, and the bot shoots.
7. **Normal 60 fps session through menus and replays:** expect no "repeating frames" notice (the 09-23 12:00 false alarm).
8. **Unselected install (fresh profile) with HD60 X, C925e and OBS Virtual Camera:** the single card-named device (HD60 X) is used. This is the owner rig's current state, because no device ID is saved.
