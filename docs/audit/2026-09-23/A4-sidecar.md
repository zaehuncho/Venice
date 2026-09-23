# A4 — Python sidecar and orchestrator

Status: **needs changes**. This is a read-only source/test audit; no customer sidecar was rebuilt or launched. The current capture qualification suite and adjacent orchestrator/record/verdict suites passed (`225 passed, 2 skipped`, exit 0), but that does not establish the identity and attribution invariants below. Sources were re-read after concurrent edits; fingerprints at review: `capture_card_backend.py` SHA-256 `E7705621C1D81820A9479E033BBE457DD6F144A78859C1C442D48542BA409D43`, `banner_verdict_live.py` `79FA9FD0F8A08EB2ABC6F7140A54D3BA88C3BE017A8F4E1C02111F560FD336C6`, `native_orion/backend/autogreen_sidecar.py` `332C1380347BA264915A03DD5524136A5DA9F226005CA7EC8865942B3828FA76`.

### [AUD-A4-001] high — default index is mistaken for an explicit capture-card choice
- Kind: bug
- Evidence: This is a residual of master **M-15 / CL2-P3-001** after its recent name/measurement repair. `native_orion/src/AppConfig.h:268` defaults `captureCardIndex=0`. `native_orion/qml/components/StreamSetupForm.qml:271-285` displays that index but records a choice only in `onActivated`; `native_orion/qml/pages/StreamSetupGate.qml:107-130` accepts *any* nonempty device list. `native_orion/src/RemotePlaySession.cpp:2504,2513-2519` passes the default index and enumerated names/IDs. `capture_card_backend.py:1152-1170,1221-1227` labels a successful open at that index `configured`; `:842-858` accepts any non-webcam-classified name with that label, but neither checks a user-choice bit nor the enumerated stable ID. `tests/test_capture_qualification_cl2_p3.py:379-394` tests a generic name as `configured=True` but assumes, rather than proves, an explicit pick. Read-only fixture with `ORION_VIDEO_DEVICE_NAMES='USB Video'`, `CaptureCardBackend(device_index=0)` printed `default_index= 0 route_basis= configured identity_verified= True` (exit 0). A generic first device can therefore gain identity/fire authority without being selected, if its subsequent cadence/geometry gates also pass. No actual wrong-device firing was observed.
- Customer impact: A fresh installation with a generic-named camera/adapter at index 0 can appear to have a valid capture route. It can silently watch the wrong pixels or, if the feed looks meter-like and passes later gates, drive timing from an unintended device. Enumeration order changes compound this.
- Proposed change: Store an explicit user selection *and* stable enumerated device ID with the capture configuration; default to unselected. Carry both to the sidecar and grant `configured` identity only when the selected ID still matches the opened inventory entry. Keep the existing named-card fallback as a separately evidenced route. Do not equate `index==default` with user intent. This crosses config, UI, launch environment and backend, so a one-line backend diff would not preserve generic-card support.
- Verification: Fresh settings with `USB Video` at index 0 and no picker activation must not gain fire authority; explicit selection of that stable ID may qualify; reorder the inventory and prove the old index cannot impersonate the selected ID; named card fallback and known webcams retain their intended behavior. Run the native setup-gate tests and `tests/test_capture_qualification_cl2_p3.py` against a coherent build.
- Confidence: confirmed

### [AUD-A4-002] medium — delayed banner can attach to the newer of two eligible releases
- Kind: reliability
- Evidence: `banner_verdict_live.py:155-162` permits appearance 400–2600 ms after a release. `_match_release()` at `:684-702` unconditionally returns the *most recent* unused release in that interval, and `:908-941` consumes that sequence for the verdict. A read-only fixture with releases `(seq=1,t=100000)` and `(seq=2,t=101700)`, then first-panel appearance `t=102400`, printed `selected_seq= 2 first_age= 2400 second_age= 700` (exit 0): both are valid by the configured interval, but the first shot's delayed panel would be labeled shot 2. Retained `logs/orion_native.log.1:53713,53779` shows two physical release submits only 1,688 ms apart, so the overlap is not merely a fabricated cadence. This does **not** prove that the particular retained pair had a delayed first banner. The master M-41 records a separate regression-corpus/template-reader concern; this is a concrete ambiguous-two-release counterexample.
- Customer impact: A rare late banner can mark the wrong physical shot, poison a per-shot timing verdict/trim update, and make shot records or the offset-sweep join incorrect. The size/frequency in real play are not measured.
- Proposed change: Treat multiple unused releases inside the attribution window as ambiguous unless an independently measured onset/game-state discriminator selects one; emit an `ambiguous` diagnostic but do not forward a training/tally verdict. Do not merely switch to the oldest release, which has its own failure mode. This needs a deliberate attribution policy rather than a safe one-line diff.
- Verification: Add a two-release fixture with a delayed first panel and a normal second panel, asserting neither receives the other's grade and the ambiguous result cannot update `BannerLeadTrim`/shot records. Measure ambiguous-window frequency on held-out retained sessions before tightening the window.
- Confidence: probable

### [AUD-A4-003] low — valid non-object JSON terminates the sidecar command reader
- Kind: test-gap
- Evidence: `native_orion/backend/autogreen_sidecar.py:3082-3095` catches `json.loads()` syntax exceptions only, then assumes `msg.get`; `_stdin_loop_wrapped()` at `:3298-3308` has no exception handler. Thus a syntactically valid `[]`, `null`, or `1` line raises in the daemon reader and stops all later commands while the sidecar main loop remains alive. `tests/test_remote_play_teardown_ordering.py:173-188` inspects deferral but has no schema-recovery case. The normal native writer is expected to send objects; a live customer trigger was not observed.
- Customer impact: If the local command channel ever receives a non-object line, later `shutdown`, release markers and session commands disappear while the process looks running, yielding a stuck/misdiagnosed session.
- Proposed change: Minimal source proposal, **not applied**:

  ```diff
  --- a/native_orion/backend/autogreen_sidecar.py
  +++ b/native_orion/backend/autogreen_sidecar.py
  @@
               except Exception as exc:
                   _note_drop("stdin_bad_json", exc)
                   continue
  +            if not isinstance(msg, dict):
  +                _note_drop("stdin_bad_schema", TypeError("command must be object"))
  +                continue
               cmd = str(msg.get("cmd", ""))
  ```

- Verification: Feed `[]\n{"cmd":"shutdown"}\n` to a command-loop fixture; assert the bad line is counted and shutdown still executes. Repeat for `null` and a number, and ensure EOF remains separately logged.
- Confidence: confirmed

## Validation boundary

Command: `$env:PYTHONDONTWRITEBYTECODE='1'; .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\orion_a4_audit_pytest_2" tests/test_capture_qualification_cl2_p3.py tests/test_capture_card_backend.py tests/test_banner_verdict_live.py tests/test_shot_records.py tests/test_orchestrator_feed_gate.py tests/test_orchestrator_capture.py tests/test_remote_play_teardown_ordering.py` → `225 passed, 2 skipped in 22.76s`, exit `0`. Earlier transient capture-notice failure was fixed concurrently and did **not** recur; it is not a current finding. `shot_records.py` currently has a writable-root probe and compiled-customer default-OFF path, so old M-29 is not re-reported as a present source defect. Astra independently reviewed `simple_meter_reader.py` and `meter_locator_cv.py` with **no new confirmed customer-impact defect** beyond prior M-16/CL2 items. Six reader/CV suites first returned `239 passed, 1 failed` on a timing-only benchmark (`test_meter_locator_cv.py:710`, 5.08 versus 3.05 ms with a 1.5 ms tolerance); three isolated fresh-process reruns passed and the complete repeat was `240 passed in 17.61s`, exit `0`. The first failure is a host-timing flake, not a reader correctness proof. Those two sources received no proposed diffs; their reviewed SHA-256 hashes were respectively `54BDEBA785A42C703BCF7903245B60F1DBADDEFAB0D91875F08E3365831FEA21` and `B01F60A34C76B2146E882B42783B373674DB8E74E84E7003C8FEC0549A2CB0D3`.
