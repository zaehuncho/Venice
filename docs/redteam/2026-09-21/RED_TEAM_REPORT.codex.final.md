# Red-team final gate — Codex — 2026-09-22

**Overall release verdict: blocked.**  **Owner-rig handoff verdict: blocked pending the three input fixes listed below.**

This is the final gate requested by `PROMPT_CODEX_FINAL_PASS.md`, not another product-wide survey. It covers Claude's patched lane, the directly interacting Astra fork changes, and the nine adversarial questions. No source was changed, no service was contacted or deployed, and no launcher/native product build was performed. The requested report is the only workspace artifact created by this pass.

## Identity and test evidence

- NexusVision: `0eca7d2251f92d2e94edcd769c5fc96172def5dd`, branch `fix/timing-input-and-remoteplay-blockers`, dirty working tree preserved.
- chiaki-ng-src: `c7515213abbebd2e28eb20dfb2eed9e0eb769dcb`, branch `orion`, dirty working tree preserved.
- Deployed `C:\Users\aaron\Desktop\NexusVision\native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe` and optimized-build `C:\Users\aaron\Desktop\chiaki-ng-src\build-orion-optimized-ffmpeg7\gui\OrionStream.exe` are both 7,023,016 bytes and SHA256 `7786742a9260650a22d2c7c2d8db4d89222934c46fbc85aa8ab61e18343bf843`. The two files also had the same UTC mtime. This proves image identity, not live behavior.
- A pre-test inventory of 1,039 selected source files and 45 native/test images was re-hashed after the tests: zero changed and zero missing. The requested report did not exist at that comparison point.
- No Orion/Chiaki/Venice process was running at the pre-test or final identity check. The fork target reported `ninja: no work to do`; therefore this pass proves that the existing unit target is current according to Ninja, not that every object was freshly compiled.

| Check | Exact command / environment note | Literal result | Exit |
|---|---|---|---:|
| fork target | `C:/msys64/mingw64/bin/cmake.exe --build build-orion-unit-ffmpeg7 --target chiaki-unit --parallel 2` from `C:\Users\aaron\Desktop\chiaki-ng-src`, MinGW64 first on `PATH` | `ninja: no work to do.` | 0 |
| fork unit | `build-orion-unit-ffmpeg7/test/chiaki-unit.exe`, MinGW64 then the fork's FFmpeg 7 shared `bin` on `PATH` | `147 of 147 (100%) tests successful, 0 (0%) test skipped.` | 0 |
| native | `ctest --test-dir native_orion/build -C Release --output-on-failure` | `100% tests passed out of 30`; 49.81 s | 0 |
| backend | `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -p offline_only --basetemp D:\NVFG-CX0922-024256\tmp-backend tests/backend` | `360 passed in 141.75s` | 0 |
| staff auth | same isolated invocation against `tests/test_backend_staff_auth.py` | `17 passed in 486.89s` | 0 |
| admin CLI | same isolated invocation against `tests/test_orion_admin.py` | `76 passed in 1.22s` | 0 |
| Worker | `node --import file:///D:/NVFG-CX0922-024256/offline_fetch.mjs --test tests/worker.test.mjs` from `website` | `tests 38`, `pass 38`, `fail 0` | 0 |
| site | `.venv\Scripts\python.exe tests/verify_site.py public/index.html` from `website` | `VERIFY_OK=...index_sha256:CC740F16198E2E21E20279CFDBACBF5878C0FC61B60B7BD33F2907FF1E7414A6` | 0 |
| targeted Worker fixture | `node D:\NVFG-CX0922-024256\worker_refund_probe.mjs` | current full refund and dispute: `refund_without_invoice_recorded`, zero backend revocations; legacy mapping: `refund_revoked`; replay: first success, second `Orion backend returned 404` | 0 |
| targeted backend fixture | `.venv\Scripts\python.exe D:\NVFG-CX0922-024256\backend_gate_probe.py` | warm disabled cache remained `(false, "")` during selective outage; cold outage returned `(true, "kill_state_unavailable")`; five sampled destructive handlers had no required-attempt call; one attempt+success counted as two staff rows | 0 |

The first fork-unit launch with only MinGW64 on `PATH` exited `0xC0000135`; static imports and the build cache showed that the test image also needs the fork's FFmpeg 7 shared DLL directory. Adding that directory to the **test process environment only** produced the 147/147 result. No DLL was copied and no package was changed.

`OrionNativeTests`' fresh Qt log independently records `learningRecoversFromBackupWhenCorrupt` and `learningWithoutBackupQuarantinesAndDefaults` as PASS, with totals `1232 passed, 0 failed, 7 skipped`. `OrionInputProtocolTests` records `runningTelemetryLossRequestsOnlyOneFailClosedRecovery`, `idleReassertProvesRouteAndDetectsWedgeAtIdle`, and the ACK/order tests as PASS, with totals `48 passed, 0 failed, 0 skipped`.

**StrictSecurity was not established.** `scripts\verify_orion.ps1 -StrictSecurity` also builds/deploys Remote Play assets, builds the production launcher, runs deployment helpers, packages, launches owner/staff tools, and repacks the customer bundle (`scripts/verify_orion.ps1:363-475`). Running it would contradict this task's no-deploy/no-source-build boundary. Under `AGENTS.md`, this alone prevents production approval; it does not invalidate the narrower tests above.

## Patched findings — per-ID verdicts

### [CL-001 / CX-004 / GM-003] blocked — the timeout path is ownership-safe but can retain stale output and starve human input without a bound

- **Exploit / failure path:** On exact `TIMEOUT + delivery_status NONE`, the bridge latches ownership, drops queued work, writes a Failed ACK, closes the pipe instance, and re-listens (`chiaki-ng-src/gui/src/orioninputbridge.cpp:298-310,360-379`). It never checks whether `chiaki_session_drop_orion_transport` or the Failed ACK succeeded, and it has no repeated-soft-fault counter, elapsed deadline, neutralization, or escalation.
- **Affected:** `chiaki-ng-src/gui/src/orioninputbridge.cpp:145-176,240-343,360-379`; `chiaki-ng-src/lib/src/feedbacksender.c:501-535,1199-1219`.
- **Impact:** I found no code path that *unmasks physical passthrough over stale owned work*: the bridge keeps `owning_ = true`, the sender keeps `orion_ownership_active = true`, and only a successfully delivered ownership-release barrier clears it. That part fails closed. However, an already-applied owned button/trigger state is not neutralized by `drop`; a timeout/reconnect/timeout loop can keep that stale state while masking the human indefinitely. This can be worse for availability than the old session-ending path and directly intersects the stuck-input blocker.
- **Evidence / refutation:** Fork test `/chiaki/feedbacksender_orion/soft_drop_keeps_sender_alive` passed, but it covers queued work followed by a successful fresh packet. It does not cover an already-applied bot hold, a failed Failed-ACK write, a drop error, or repeated reconnect timeouts. `ensureConnected()` does clear the launcher's `haveLast_` on a fresh pipe (`native_orion/src/OrionInputClient.cpp:196-217`), so a successful reconnect forces a full state seed; there is simply no upper bound if those seeds keep timing out.
- **Required fix:** Make the soft transition a checked transaction. If queue-drop or Failed-ACK delivery fails, escalate. Add a small bounded soft-fault budget and a state-safe terminal action after exhaustion. Add an explicit, ACKed neutral/ownership disposition that cannot expose passthrough ahead of stale UDP work.
- **Verification test:** Fake transport with an already-applied held R2/Square, timeout, failed ACK, reconnect, repeated timeout, then release/ownership barrier. Assert bounded time to either a confirmed neutral fresh seed or terminal session teardown, never physical unmask over stale work.

### [CL-002] approved — narrowly, for expedite and quiet-queue echo shedding

- **Path / affected:** `chiaki-ng-src/lib/src/feedbacksender.c:274-304` expedites delayed optional repeats before a new required edge and admits the +40 ms echo only when the resulting queue is quiet; `lib/src/orioninput.c:84-87` defines the threshold.
- **Impact:** Removes the demonstrated optional-repeat head-of-line delay without re-sending a late release after a new press.
- **Evidence:** `/chiaki/orion_input/release_echo_allowed_only_when_quiet`, the wrapped-FIFO test, the `no_late_up`/repress tests, barrier/latest-stick tests, all Circle+Triangle chord cases, and 147/147 passed. The deployed image hash matches the optimized build.
- **Residual / next test:** This approval does not close CL-010: every analog trigger change is still classified as a button edge at `feedbacksender.c:234-237` and enters the ordered FIFO. Stress analog travel separately.

### [CL-003 / GM-004] needs changes — ordered split is sound for its narrow case, but the first packet is not always a copyable release

- **Exploit / failure path:** The client splits only when a trigger reaches zero **and** a new digital button is pressed (`native_orion/src/OrionInputClient.cpp:420-438`). The first packet is initialized as `pre = p` and only its buttons are restored. If R2 releases while L2 increases in the same poll, the first packet still contains the L2 increase. The fork explicitly refuses to copy a release carrying any trigger increase (`chiaki-ng-src/lib/src/orioninput.c:64-81`). A trigger release without a new digital press is not split at all.
- **Affected:** `native_orion/src/OrionInputClient.cpp:412-438`; `chiaki-ng-src/lib/src/orioninput.c:64-81`; `native_orion/src/ShotIntentPolicy.h:72-89`.
- **Impact:** The common “trigger up plus new button down” case is improved, but cross-trigger motion and release-without-new-button cases still lack the claimed redundant terminal release. This can preserve a stuck trigger class.
- **Evidence / refutation:** The two transactions cannot reorder within this client: `ioMutex_` covers the call, the first transaction awaits its exact ACK before the second, and `transmitLocked` increments the sequence (`OrionInputClient.cpp:430-437,524-602`). The second can fail; in that case `last_` correctly remains at the confirmed `pre`, the pipe closes on ambiguity, and reconnection clears `haveLast_`. The fork's passing `terminal_trigger_release_classification` test itself proves that an opposite-trigger increase returns false. There is no native test that drives the actual two-packet client split through this cross-trigger case.
- **Required fix:** Construct `pre` from the last confirmed packet plus only terminal zero-crossings; do not copy any new trigger increase, stick move, or other new edge into it. Decide and test whether every terminal trigger release needs this split, not only release+new-button.
- **Verification test:** Native pipe fixture for R2-up+L2-rise+X-down, L2-up+R2-rise, release with no new digital press, and second-transaction failure. Inspect both wire packets and the fork's history copies.

### [CL-004] approved — bounded constants are internally ordered

- **Path / affected:** Bridge local delivery is 40 ms (`chiaki-ng-src/gui/src/orioninputbridge.cpp:20-23`), final client ACK is 65 ms (`native_orion/src/OrionInputClient.cpp:81-83`), and release repair is 60 ms (`native_orion/src/OrionAppController.h:2240-2243`).
- **Impact:** The repair no longer intentionally lands inside the +40 ms optional echo window, while the client still waits longer than the bridge's local budget.
- **Evidence:** 147/147 and 30/30 passed; chord repair-after-15ms cases passed. This is source/fixture approval, not proof of console receipt or a live 60 ms repair.
- **Verification test:** Owner trace must show one confirmed repair transaction, no `ack_stage=128`, and no session drop during the chord/sprint-release matrix.

### [CL-005] needs changes — normal abort is fatal, but two sender-thread exits remain invisible

- **Exploit / failure path:** Normal abort sets `should_stop`, clears queues, and broadcasts (`feedbacksender.c:456-498`); the waiter returns CANCELED and the bridge takes the fatal path. But an initial state-mutex failure returns from the sender thread without setting `should_stop` (`feedbacksender.c:862-869`), and condition-wait errors break the loop without setting it (`:881-904`). The thread broadcasts and exits at `:1233-1236`; a later bridge wait sees `TIMEOUT + NONE` and takes the new soft path.
- **Affected:** `chiaki-ng-src/lib/src/feedbacksender.c:404-453,862-904,1233-1236`; bridge `orioninputbridge.cpp:298-317`.
- **Impact:** A genuinely dead sender is not guaranteed to end the session; it can be mislabeled as a recoverable packet timeout and feed the unbounded loop in CL-001.
- **Evidence / refutation:** CANCELED, true TIMEOUT, and mismatch are otherwise kept distinct. Status starts as NONE; matching completion is consumed under the state mutex; the platform deadline race rechecks the predicate under the same mutex (`lib/src/thread.c:478-516`); and a successful wrong status is converted to fatal UNKNOWN. The defect is specifically the silent exits that leave `should_stop == false`.
- **Required fix:** Publish a terminal sender state on every exit and make `wait_orion_delivery` return CANCELED/internal error when the sender is not alive. Never log `sender_alive=1` unconditionally from `drop`.
- **Verification test:** Inject initial-lock failure and both condition-wait error sites; each must wake the exact waiter with a fatal result and request one session stop.

### [CL-006] needs changes — input-pipe liveness exists independently, but total telemetry silence has a stale-health gap

- **Path / affected:** Missing `input_ready` now correctly does nothing; only explicit false requests telemetry-driven recovery (`native_orion/src/SidecarWatchdog.h:155-180`, `RemotePlaySession.cpp:4205-4219`). Independently, the controller checks pipe connectivity about once per second and runs the bounded input-only/full-restart ladder (`OrionAppController.cpp:13961-14080`). At digital rest it sends an exact MustDeliver reassert, which detects a wedged reader (`:14081-14117`). QProcess exit and launch-failure handlers cover actual process death (`RemotePlaySession.cpp:2537-2705`).
- **Impact:** Therefore, no-telemetry plus a disconnected or wedged input pipe is recoverable without telemetry. However, a still-running sidecar that stops emitting all telemetry while its pipe remains connected can leave `frameAgeMs`, `transportAgeMs`, and `backendFrozen` at their last cached values; their getters return stored values (`RemotePlaySession.h:153-167`). I found no independent “last telemetry receipt” deadline. That can suppress full-sidecar recovery even while detector/capture health is stale.
- **Evidence:** `runningTelemetryLossRequestsOnlyOneFailClosedRecovery` and `idleReassertProvesRouteAndDetectsWedgeAtIdle` passed. The former tests the pure explicit/missing policy, not an alive-but-silent event-loop timeline.
- **Required fix:** Keep MISSING distinct from false, but track monotonic age since any sidecar telemetry record. At a bounded age, disarm and prove process/capture/pipe liveness independently; only then choose input-only versus contained full restart.
- **Verification test:** Event-loop fixture with a live QProcess that emits no stdout telemetry while (a) pipe is healthy, (b) pipe is wedged, and (c) transport counters are frozen. Assert bounded fail-closed state and one recovery escalation.

### [CL-007] approved — for the established-route cleanup, with a required owner trace

- **Path / affected:** `inputRecoveryStarted` now disarms and resets, then calls `neutralizeOwnedInput()` before retiring the old direct connection (`OrionAppController.cpp:2668-2692`). Cleanup serializes with submissions, emits virtual neutral, and sends a forced owned neutral only on an already connected/confirmed owned direct route (`:6074-6108`).
- **Impact:** A bot-held Square is no longer omitted from this recovery-start path. The function deliberately does not reconnect solely to clean an unproven route.
- **Evidence:** Native `ownedNeutralCleanup` cases, Square watchdog copies, ambiguous-route masking, and 30/30 targets passed. There is no full signal-to-pipe integration case for this exact lambda, so approval is bounded to the helper/control order.
- **Verification test:** Owner trace must show `Owned input cleanup ... local_route_ack=1` before old-route reset when recovery begins from an owned connected state; a disconnected route must remain masked/fail closed.

### [GM-002 / CX-001] needs changes — last-good recovery works in the happy fault case, but “valid,” collision, and copy guarantees are overstated

- **Exploit / failure path:** `readObjectStatus` calls any nonempty JSON object valid (`native_orion/src/AppConfig.cpp:90-116`). Save removes the existing backup and performs an unchecked copy of that merely syntactic object (`:1254-1259`). Reload uses second-resolution quarantine names and ignores both rename and restore-copy results (`:270-302`). Missing primary never restores an existing backup. A second corrupt load in the same second can collide, leave the bad primary in place, fail the copy back, yet log that recovery succeeded.
- **Affected:** `AppConfig.cpp:90-116,249-302,1254-1275`; `AppConfig.h:2085-2092`; `OrionAppController.cpp:6894-6903`.
- **Impact:** The exact tested zero-byte + healthy-backup case recovers, but semantic corruption, filesystem denial, collision, or failed copy can still destroy/strand the only last-good generation or report a restore that did not persist.
- **Evidence / refutation:** The two new Qt cases passed and prove the intended zero-byte/truncated flows. Profile paths and backups are profile-specific (`AppConfig.cpp:249-267`). A corrupt empty/non-object `.bak` is **not** restored, which is correct. A syntactically valid but semantically invalid object can be promoted/restored, and CX-008's unvalidated legacy timing fields remain open. `learningLoadNote()` is a non-consuming getter; the “surfaced once” property is neither implemented nor tested. Both new cases compile to empty bodies under `ORION_PRODUCTION_BUILD`, so a production test can pass without exercising recovery.
- **Required fix:** Define and run full semantic schema validation before promotion; copy new backup to a temporary file and atomically replace only after success; use collision-resistant quarantine names; check/act on rename/copy failures; restore a valid backup when primary is missing unexpectedly; consume or explicitly de-duplicate the customer notification. Ensure production-build tests execute the cases.
- **Verification test:** Denied read/write, failed backup copy, two corruptions in one second, missing-primary+valid-backup, empty/corrupt/semantic-invalid backup, every profile namespace, and two restarts. Assert exact bytes and truthful notice.

### [GM-006 / CX-007 / CX-020] needs changes — cancel restores one value case but not zero/automatic provenance

- **Exploit / failure path:** Calibration saves only the numeric starting lead (`OrionAppController.cpp:9182-9186`). On cancel after a shot, it restores `start > 0 ? start : current` through `setActuationLeadMs` (`:9191-9213`). A zero start therefore keeps a changed candidate; even one unchanged “Good” can route zero through a clamping setter. The setter always writes `actuationLeadUserSet = true` (`:9248-9267`), so canceling an automatically seeded lead converts it into a permanent user override.
- **Affected:** `OrionAppController.cpp:9130-9149,9182-9267`; controller calibration state.
- **Impact:** “Cancel means cancel” is false for an unset lead and for provenance. CX-020 also remains: the UI can say the 300 ms candidate is “Saved” on Good/lock even though the candidate is only persisted when the numeric lead changes (`:9233-9239`).
- **Evidence:** The native suite's `leadCalibrationConvergesAndNeverInvertsTheSign` tests pure policy math, not controller persistence/cancel. No controller flow test covers zero, automatic provenance, save failure, or Good-only lock.
- **Required fix:** Snapshot numeric value, `actuationLeadUserSet`, and per-source stash before calibration; restore all of them transactionally on cancel. Apply/persist the starting candidate before describing it as saved, or change the UI contract.
- **Verification test:** Start from zero, auto-seeded nonzero, explicit user lead, Good-only lock, changed lead, and save failure; compare settings bytes and engine authority before/after cancel.

### [CX-013] blocked — a warm “kill disabled” cache still defeats a later owner kill during selective outage

- **Exploit / failure path:** `_LAST_GLOBAL_KILL` caches both false and true indefinitely. After a warm container reads disabled, another container can commit the owner's kill; if the first container's later DynamoDB reads fail, it returns its stale false (`backend/lambda_function.py:481-501`). The read is not requested as strongly consistent and the cached false has no age/version.
- **Affected:** `backend/lambda_function.py:481-501`; activation/heartbeat and other kill callers, including `:1765-1768`. Existing lease TTL is 900 seconds (`:1700-1703`).
- **Impact:** A selective control-plane outage can preserve fresh authority despite an owner emergency kill. That directly contradicts the release blocker “kill switch is written but not enforced.”
- **Evidence:** The extracted helper fixture produced: healthy disabled `(false, "")`; then selective outage `(false, "")`; cold outage `(true, "kill_state_unavailable")`; known-enabled then outage `(true, "owner kill")`. Cold fail-closed is correct for security, but its operational effect is 503 for activation/heartbeat until the state is readable; clients with a current signed lease can retain authority for at most the existing 900-second lease window.
- **Required fix:** Do not reuse a cached false across an unbounded failed read. Use a recent bounded disabled proof plus a monotonic config revision/owner-publish invalidation, or fail closed on uncertainty. Preserve enabled as sticky. Define and monitor the outage policy.
- **Verification test:** Two warm containers, disabled read, owner kill on the other, selective read outage, eventual-consistency delay, cold container, recovery, and signed leases. No fresh lease while kill is enabled or unknown.
- **Owner decision:** Choose the maximum outage grace for existing leases; the current technical ceiling is 15 minutes. New/renewed authority should remain fail closed.

### [CX-014] blocked — the required attempt guard covers only a subset and breaks downstream semantics

- **Exploit / failure path:** The source contains only three mutation-guard call sites: generic licence action at `backend/lambda_function.py:3996` and generic config-set entries at `:4673,:4682`. Destructive routes that return before those sites mutate without a required audit row: provision/create (`:1088-1099`, `:3921-3960`), blacklist (`:3962-3969,4203-4222`), kill/unkill (`:1109-1198`), staff changes (`:4303-4434`), TOTP and secret rotation (`:4626-4652`), and webhook chargeback (`:4860-4920`).
- **Affected:** common audit writer `lambda_function.py:409-468`; all paths above; metrics `:4781-4792`; `native_orion/qml/admin/AdminAuditPage.qml:38-40,246-249`.
- **Impact:** Several security-sensitive mutations can still commit without durable attempt evidence. On guarded routes, an attempt and its successful completion are counted as two staff actions because metrics count rows, not logical operations. The QML audit page renders every non-`ok` result, including `attempt`, with the error color. The attempt has no operation ID linking it to completion.
- **Evidence / refutation:** The AST fixture confirmed no required-attempt call in `handle_kill`, `handle_unkill`, `_blacklist_mutate`, `handle_admin_staff_post`, or `handle_bot_chargeback`; one synthetic attempt+success produces metric count 2. The 360 backend, 17 staff, and 76 CLI tests all pass, showing the suite does not enforce complete path coverage or UI/metric semantics. Read-only `license.lookup` returns before the guard (`:3982-3990`), which is correct; the defect is missing mutation coverage, not attempts on reads. The generic licence guard is also before the action's capability check (`:3992-4003`), so denied authenticated actions can write “attempt” rows and inflate metrics.
- **Required fix:** Put every destructive route behind one path-sensitive mutation wrapper or transactional outbox. Record actor, full unambiguous target identity in protected fields, normalized intent, and one operation ID before commit; link completion/failure to it. Run capability checks before recording a mutation attempt. Teach metrics/UI that `attempt` is an in-progress state, not an error or a second action.
- **Verification test:** Table-drive every write route with audit storage unavailable; zero target mutations may occur. Then assert one logical metric action and correct pending/success/failure UI state for attempt+completion.

### [CX-002] blocked — current Stripe Charge identity mapping acknowledges qualifying events without revocation; replay is not idempotent

- **Exploit / failure path:** The Worker resolves refund/dispute identity from `charge.invoice` (`website/src/worker.js:686-713`) while its Stripe API requests are pinned to `2026-08-26.dahlia` (`:5-7,282-293`). Stripe's current Charge reference no longer lists `invoice`, while the older `2025-02-24.acacia` reference does. A modern full refund therefore returns `refund_without_invoice_recorded`; a dispute fetches the modern Charge and does the same. `stripeWebhook` then returns 200 (`:718-734`), so the qualifying event is acknowledged without audit, retry, alert, or revoke. [Current Charge object](https://docs.stripe.com/api/charges/object) · [Acacia Charge object with `invoice`](https://docs.stripe.com/api/charges/object?api-version=2025-02-24.acacia)
- **Affected:** `website/src/worker.js:626-734`; backend `lambda_function.py:3662-3667,4860-4920`.
- **Impact:** Paid-only access can remain valid after a full refund or dispute. If a legacy-shaped event does resolve and revokes once, replay is not idempotent: `_newest_active` excludes the now-revoked entitlement, the backend returns 404, and the Worker returns 500. Stripe explicitly documents duplicate delivery and tells integrations to track event/object IDs; no such ledger exists here. [Stripe webhook duplicate/event-version behavior](https://docs.stripe.com/webhooks#event-delivery-behaviors)
- **Evidence:** The inert fake-boundary fixture observed: current full refund → `refund_without_invoice_recorded`, backend calls 0; current dispute → fetched Charge then same result; partial refund → `partial_refund_recorded`; test mode → `non_live_ignored`; legacy invoice-shaped full refund → `refund_revoked` with exact subscription; replay → first success, second `Orion backend returned 404`. The backend accepts `kind` values `refunded`, `disputed`, and `chargebacked` (`lambda_function.py:4870-4873`), so the disputed mapping itself is compatible. The live-key gate requires live secret/publishable prefixes and both event/object `livemode === true` (`worker.js:626-630`), so the reviewed test-mode event cannot revoke a live entitlement.
- **Further failure modes:** Webhook event shape is set by the endpoint version, not by the fetch header; that live endpoint version was not verified. Missing invoice/subscription is silently acknowledged. The refund branch does not apply `paidSubscriptionDiscordId`'s live/status/price checks. Exact backend subscription matching reduces cross-entitlement damage when an ID is present, but does not repair missing identity.
- **Required fix:** Resolve Charge → PaymentIntent/Invoice Payment → Invoice using the current Stripe model, validate live account/customer/subscription/price against the stored provision record, and enqueue a durable event-ID/object-ID ledger before acknowledging. Make backend revoke idempotent for an already-revoked exact entitlement. Alert/retry unresolved qualifying events; do not return 200 as “recorded” without durable reconciliation.
- **Verification test:** Current pinned-version full/partial refunds and disputes, direct and invoice payments, missing/ambiguous identity, two subscriptions, out-of-order events, duplicate same Event, distinct duplicate Events for one object, backend timeout after commit, and test-mode/live-mode separation.

### [GM-009 / CX-003 / CX-021] approved in source; deployment not approved

- `website/src/worker.js:522-536` now renders `$19.99/month`; `discord_launch/orion_worker.js` renders a 7-day trial. The site verifier passed and the Worker suite passed.
- The website and Discord Worker were not deployed in this lane. Approval is only for the reviewed literals/static surface; production copy and live Stripe price remain deployment checks.

## Answers to the nine adversarial questions

1. **Soft drop / ownership:** no reviewed sequence unmasks physical passthrough ahead of stale owned work; both ownership latches remain true until a confirmed release barrier. A stale already-applied command can nevertheless persist while the human is masked. Repeated soft faults have no bound and can starve input longer than the old session kill. **Verdict: blocked (CL-001/CX-004/GM-003).**
2. **CANCELED vs TIMEOUT:** normal abort/dead-state publication produces CANCELED and remains fatal; exact TIMEOUT+NONE alone is soft; mismatch remains fatal, and deadline races are rechecked under lock. Initial mutex/condition-wait error exits do not publish stop and can masquerade as TIMEOUT. **Verdict: needs changes (CL-005).**
3. **Two MustDeliver transactions:** client ordering is serialized and exact-ACKed; the second can fail, but cache state remains the confirmed first packet and reconnect forces a seed. The fork copies the first packet only when it is a pure release; `pre = p` preserves a simultaneous opposite-trigger increase, which makes it non-pure. **Verdict: needs changes (CL-003/GM-004).**
4. **MISSING vs total silence:** pipe-connect heartbeat, idle exact-ACK reassert, and QProcess exit/failure paths independently recover a disconnected/wedged input link. There is no proven monotonic deadline for an alive process that emits no telemetry while cached pipe/transport health remains apparently good. **Verdict: needs changes (CL-006).**
5. **Kill cache:** yes, stale disabled can survive an external owner kill plus selective outage in a warm container. Cold fail-closed is correct; operationally it blocks new activation/heartbeat, while already-issued leases have the existing 900-second ceiling. The owner must choose the bounded existing-lease grace, not permit unbounded cached false. **Verdict: blocked (CX-013).**
6. **Audit attempts:** attempts double-count row-based metrics and render as errors in the QML audit page. They are absent from multiple destructive paths and absent from the reviewed lookup read path as intended. **Verdict: blocked (CX-014).**
7. **Refund/dispute:** partial refund is intentionally non-revoking; `disputed` is accepted; test mode is rejected. Current Charge identity resolution and replay are broken as detailed above. **Verdict: blocked (CX-002).**
8. **learning.json:** profile namespace is correct; an actually corrupt non-object backup is not restored. “Valid” is only syntactic object validity, quarantine can collide, file-operation failures are ignored, and the note is not consume-once. **Verdict: needs changes (GM-002/CX-001).**
9. **Surface / fail-closed / fork conflict:** the soft loop, stale false kill cache, incomplete audit wrapper, and 200-on-unresolved refund weaken fail-closed guarantees. No direct textual/ABI conflict was found between Astra's affinity/AV-clock/expedite/chord work and Claude's bridge edits; one optimized binary contains the combined tree and 147/147 passes. This does not close Astra's open CL-009/016/017 work, CL-010 analog-edge pressure, or any unpatched release blocker. StrictSecurity is missing. **Overall: blocked.**

## Deliberately unpatched carryover

These were not re-surveyed; their prior evidence remains the applicable gate.

| IDs | Verdict | Exact gate |
|---|---|---|
| CL-008 | blocked | owner policy/gate unresolved for No-Meter/Input-Timed epoch join |
| CL-009 / CX-009, CL-016, CL-017 | needs changes | Astra lane remains uncommitted/open; this pass only found no merge conflict and ran the combined fork unit image |
| CL-010/011/012/013/015/018/019 | needs changes | analog edge FIFO and remaining input/authority/logging issues are not closed by this patch |
| CX-008 | blocked | semantic validation gap also undermines “valid backup” |
| CX-015 | blocked | unbounded log-sink wait remains a GUI/input/shutdown release blocker |
| CX-016 | needs changes | calendar-month entitlement is still represented as fixed 30 days |
| CX-017 | blocked | interrupted order claims can remain pending without recovery |
| CX-018 / GM-012 | blocked | DLL preload/fallback trust gate remains incomplete |
| CX-019 / GM-018 | needs changes | customer/network identifiers remain exposed in diagnostics/logs |
| GM-001, GM-005 and other deferred GM rows | needs changes | coordinator downgrades/owner policy calls remain open; no new approval is inferred |

## Owner-rig handoff

**blocked** as a candidate claimed to have closed the input blocker. Before the owner runs the requested Circle+Triangle and sprint-release matrix:

1. Bound CL-001's reconnect loop, check drop/Failed-ACK results, and prove stale held output reaches confirmed neutral or terminal teardown.
2. Make every sender-thread exit publish a fatal terminal state (CL-005).
3. Correct the cross-trigger `pre` packet and add the native split test (CL-003/GM-004).

After those three changes, the owner test should remain bounded and observable: Circle+Triangle ×20 across both release orders and rapid repress, R2/L2 terminal releases including cross-trigger motion, forced one-shot delivery timeout, then recovery. Require no `fatal_local_delivery_fault` for the recoverable timeout, no repeated soft-fault loop, no `ack_stage=128`, all physical controls at rest after release, and a confirmed cleanup/seed or one intentional terminal teardown. Do not treat a visually live capture feed as proof of input recovery.

## External red-team priorities after repackaging

1. **Input ownership fault matrix:** already-applied held button/trigger + dropped ACK + reconnect + duplicate/mismatched/late ACK + ownership release; verify console-visible rest and a bounded human-input outage.
2. **Kill/audit selective-failure matrix:** two warm Lambda containers, owner kill, eventual/read outage, audit-table denial on every destructive route, retry after response loss, and exactly-one logical audit operation.
3. **Current Stripe lifecycle:** pinned-version full/partial refund, dispute, duplicate/out-of-order delivery, modern Charge identity, multiple subscriptions, and backend commit-then-timeout.
4. **Persistence power/fault matrix:** semantically invalid learning, denied/colliding rename, failed backup copy, missing primary, corrupt backup, profile switch, two restarts, and production-build test execution.
5. **Calibration transaction:** zero/automatic/explicit lead, provenance, Good-only lock, cancel, save failure, and per-video-source stash identity.
6. **Carryover release blockers:** DLL search order/preload trust, persistent log I/O failure, pending order-claim recovery, 30-day billing mismatch, and diagnostics identifier minimization.

No external red-team handoff or production approval should occur until the blocking findings are fixed, a coherent launcher/Worker/Lambda/package is built, the exact package passes `verify_orion.ps1 -StrictSecurity`, and deployment identity is re-proved.
