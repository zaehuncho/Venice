needs changes

# A5 — Licence, backend, website and Discord

Read-only audit of the dirty working tree, rechecked 2026-09-23 02:53 UTC. No deployment, source/settings mutation, live-service call, application launch or build. Tests used existing local binaries, mocked AWS/Discord fixtures and mocked Worker fetches. Reports propose fixes; none are applied. Standard/StrictSecurity on an exact rebuilt release remains a separate release gate.

The master report and previous reliability report remain the baseline. Items below are new behavior or new evidence on recently revised code, not repetitions of the retained M-18/M-21/M-22/M-23/M-35 findings.

### [AUD-A5-001] high — Paid-account activation polling exhausts its own pairing quota before the advertised wait completes
- Kind: bug
- Evidence: The new GMC-001 wait state at `C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:8-9,574-579,617-634` polls the actual pair-issue endpoint every five seconds, up to 24 retries. `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:2469-2475` limits that endpoint to five requests per 600-second fixed window **before** checking whether provisioning has completed. The limiter increments every attempt (`:349-366`), including the preceding subscription-not-ready responses. The Worker treats 429 as an outage and renders a page without the activating refresh (`worker.js:622-624`). With no bucket rollover, the sixth request at roughly 25 seconds exhausts the quota; the claimed two-minute wait cannot complete. `C:\Users\aaron\Desktop\NexusVision\website\tests\worker.test.mjs:993-1014` passes arbitrary attempt numbers to a stateless mocked backend and does not exercise this shared quota. This is new evidence about the GMC-001 repair, separate from the old webhook provisioning/claim problems in M-23.
- Customer impact: A customer whose payment takes more than approximately 25 seconds to provision sees the waiting page turn into an outage. Even after provisioning succeeds, checking again can remain rate-limited until the current ten-minute window ends. The UI tells the customer to wait a minute, which need not be enough.
- Proposed change: Separate non-minting entitlement/provisioning status polling from code issuance, with independently bounded quotas. Issue one pairing code only after readiness is confirmed. Preserve the existing code-issuance cap; do not merely raise it to accommodate page refreshes. Return a truthful retry deadline for 429 and retain a bounded pending UI rather than converting it into generic server failure.
- Verification: Add a fake-clock Worker/backend contract test spanning an entire fixed bucket. Provisioning becomes ready after 40-90 seconds; verify the page obtains exactly one code without consuming six issuance attempts. Cover a bucket boundary, preexisting quota usage, multiple browser tabs, real entitlement denial, and pending timeout. All dependencies must be mocked; no purchase or real account is needed.
- Confidence: confirmed

### [AUD-A5-002] high — Entitlement lookup failure is reported as an authoritative lost subscription and stops a valid session
- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:3589-3611` catches a failed Discord GSI query, tries a scan, and converts scan failure into `([], "scan")`. `discord_access_entitlement` consequently returns `no_active_discord_purchase` (`:3643-3660`). The heartbeat translates that into `subscription_required` HTTP 403 (`:1805-1810`), even if its direct license read succeeded. The native client classifies that code as a kill (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\LicenseClient.cpp:249-256`); `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:2400-2427` stops retrying, disconnects Remote Play and clears authentication. This differs from M-21's cached global-kill failure: the failure here is a secondary purchase lookup becoming a false entitlement verdict.
- Customer impact: A transient database query/scan outage or permission problem can disconnect an otherwise valid paying customer and require sign-in again, rather than keeping the existing bounded lease and retrying. The message incorrectly says that the account lacks a subscription.
- Proposed change: Preserve an explicit unavailable result or typed exception from `find_licenses_by_discord`; distinguish it from a successful lookup with zero matching purchases. Return a non-kill 503 code for unavailable entitlement evidence. Do not grant or extend a lease from incomplete evidence: the client retains only its already-issued bounded lease, retries, and remains fail-closed when that lease expires. Apply the distinction to pairing/status consumers too so they do not advertise “no license” during an outage.
- Verification: Mock a successful direct license read followed by both secondary lookup failures. Assert no fresh lease is issued, the response is a retriable 503 rather than `subscription_required`, and the client does not immediately disconnect or erase authentication. Advance its lease deadline and assert automation then pauses. Separately verify that a successful empty lookup still produces the genuine entitlement-denial path. Existing profile/validate tests passed but do not establish this outage distinction.
- Confidence: confirmed

### [AUD-A5-003] medium — A transient Customer-role grant failure is acknowledged as complete and webhook retries do not repair it
- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:1949-1974` catches role-grant failures and returns false. Paid mint and renewal handlers still return successful HTTP results after the DM succeeds (`:2752-2758,2782-2787`), with `role_granted:false` merely informational. The Worker accepts the successful provision response and acknowledges the event (`C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:688-697,710-719`). Even if an event is retried for another reason, the duplicate-order branch (`lambda_function.py:2685-2695`) only retries `_notify_stripe_provision`; it never retries the role. The notification helper returns immediately after a persisted `notified_at` (`:2612-2614`). This concerns delivery reconciliation, not M-23's already-reported stuck order claim.
- Customer impact: Payment and launcher entitlement work, but the customer can remain without the paid Discord access promised by onboarding after a transient Discord error. A normal webhook replay does not repair it; support or a later renewal must intervene.
- Proposed change: Keep granting the paid entitlement independently of Discord availability, but persist role delivery as a separately retryable obligation. Track role and DM completion independently and let duplicate-order processing reconcile any incomplete side effect. Do not mint again, extend the subscription again, or resend a previously confirmed DM while repairing a role. Surface persistent failure to support.
- Verification: Mock role failure and successful DM, then replay the same order after role service recovery. Assert one license/one entitlement change/one successful DM, but a new role-grant attempt which completes the pending obligation. Also cover role success with DM failure and both succeeding. Existing plan tests stub role grants as always successful, so their pass does not cover this path.
- Confidence: confirmed

### [AUD-A5-004] medium — A previously successful heartbeat makes ordinary later lease expiry look like a broken PC clock
- Kind: bug
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\LicenseHeartbeatPolicy.h:98-109` maps any blocked lease plus `lastHeartbeatServerOk=true` to `ClockOff`; that boolean has no age and remains true while a new request is pending (`:163-181,188-201`). Normal expiry/staleness is independently possible in `C:\Users\aaron\Desktop\NexusVision\native_orion\src\LeaseGate.cpp:84-99`, for example after a successful heartbeat followed by sleep past the lease. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:5118-5144` displays the clock-reset/restart instruction and only triggers the lease-lapse immediate-check fallback for `Reconnecting`, not `ClockOff`. The existing native test at `C:\Users\aaron\Desktop\NexusVision\native_orion\tests\LicenseHeartbeatPolicyTests.cpp:211-219` explicitly asserts this insufficient predicate. New evidence on the revised M-09 / CL2-P8-002 UI/retry implementation, not a claim that the old five-minute-only implementation remains unchanged.
- Customer impact: A user wakes a PC with an accurate clock after an earlier successful heartbeat and may be told to change Windows clock settings and restart Venice while the next heartbeat is still pending. If a resume event was missed, the specific lease-lapse fallback does not run for that state. The scheduled timer may still recover; this is not a claim of an indefinite lockout.
- Proposed change: Distinguish a newly verified heartbeat whose signed expiry is already invalid at receipt from a previously valid lease that later ages out. Only the former supports the clock warning; ordinary expiry/staleness should show reconnecting and schedule the same debounced recheck. Expose a lease-block reason or retain the receipt-time validation outcome rather than deriving clock health from the last request's success bit.
- Verification: Extend the existing fake-clock simulator: receive a valid heartbeat, advance both clocks consistently beyond the lease during sleep, resume with a delayed response, and assert Reconnecting rather than ClockOff plus one bounded recheck. Separately supply a newly verified response whose expiry is already past relative to the client and assert the clock-specific notice. Repeat with a missed power event and with a genuine network failure.
- Confidence: confirmed

## Review decisions and duplicate suppression

- The current heartbeat coordinator remembers an immediate request that arrives during an in-flight request and reruns it after failure. The previous reliability report's exact no-op race is addressed in current source; its new simulator cases passed below. AUD-A5-004 addresses a different state-classification defect.
- Production `LeaseGate::enabledFromEnvironment()` returns true. The embedded lease verification key is nonempty and the controller requires its signature for refresh. The old environment-disable allegation is not revived. M-35's request-binding concern remains in the master report and is not duplicated here.
- M-18 (TOTP step-up), M-21 (global-kill cache), M-22 (destructive audit coverage) and M-23 (payment lifecycle/idempotency) are retained prior gates, not closed by these passing tests or by this report. No live permission/configuration posture was inspected or approved.
- Renewal after a revoked key mints a new key by an explicit existing contract (`C:\Users\aaron\Desktop\NexusVision\tests\backend\test_plan_surface.py:115-126`). This audit does not label that intentionally tested behavior an accidental defect or change the owner's policy. Frozen-key preservation is separately covered at `:128-138`.
- Discord bot/Worker current diffs were inspected. No real Discord action, account lookup, role mutation, webhook send or secret retrieval was performed. Source mentions of prices are product-copy context, not verification of live billing configuration.

## Tests actually run

```powershell
# CWD C:\Users\aaron\Desktop\NexusVision
$env:PYTHONDONTWRITEBYTECODE='1'
& .\.venv\Scripts\python.exe -m pytest tests/backend/test_profile.py tests/backend/test_validate.py tests/backend/test_plan_surface.py -q -p no:cacheprovider --basetemp "$env:TEMP\orion-a5-audit-pytest"
```

Literal result: `48 passed in 32.43s`; exit 0. AWS and Discord dependencies use the existing mock fixtures in `C:\Users\aaron\Desktop\NexusVision\tests\backend\conftest.py`.

```powershell
# CWD C:\Users\aaron\Desktop\NexusVision\website
node --test tests/worker.test.mjs
```

Literal summary: `tests 42`, `pass 42`, `fail 0`, `cancelled 0`, `skipped 0`, `todo 0`, `duration_ms 484.8407`; exit 0. Test fetch functions return local fixture responses. The current GMC-001 tests do not carry stateful rate-limit behavior across page requests.

```powershell
# CWD C:\Users\aaron\Desktop\NexusVision
$out=Join-Path $env:TEMP 'orion-a5-heartbeat-policy.txt'
& .\native_orion\build\Release\OrionLicenseHeartbeatPolicyTests.exe -o "$out,txt"
```

Literal result: `Totals: 21 passed, 0 failed, 0 skipped, 0 blacklisted, 8ms`; exit 0. Output reopened at `C:\Users\aaron\AppData\Local\Temp\orion-a5-heartbeat-policy.txt`. The executable was dated 02:47:26 UTC, after the policy header's 02:43:14 UTC edit; this run includes the in-flight simulator cases. It does not instantiate the full launcher/network UI, prove release-package identity, or validate live service operation.

## Recheck identities

SHA-256 at 02:53 UTC; concurrent work may change these after the snapshot. All paths below are under `C:\Users\aaron\Desktop\NexusVision`.

| File | SHA-256 |
|---|---|
| `backend/lambda_function.py` | `423BEE95675A646697E17CE3246F9EE61112764CCEA6BBC282B618A50857923E` |
| `website/src/worker.js` | `438C02717ADB25A34126B0D0348AF17EEC03130FF97F983536B4F02D43473C60` |
| `native_orion/src/LicenseClient.cpp` | `5307583CB0D42920D54EAA6D5AC4FD8498C8C5F2200AC272A8B46E81BBC55163` |
| `native_orion/src/LeaseGate.cpp` | `310DAF1FBC9604D9F73F265CD7ACB42FB4697B32BA66F0737F258CA597B59FBC` |
| `native_orion/src/LicenseHeartbeatPolicy.h` | `756F5D9E0B94EC27053456B6BB2F7B2BD9A625807C504E353ACF8B60DA6365F1` |
| `native_orion/src/OrionAppController.cpp` | `568FB02F001C4DC91831E099DDB67EBE203667618035B59E69FEA43D8C1E6C72` |
| `discord_launch/orion_bot.py` | `BA25D4771DD6387B0ADD50BD599BBF44D6B463226CF180128C76C37C0E1ABA75` |
| `discord_launch/orion_worker.js` | `36CF05ADE39114D9A923E03DD0F019B8D3F6D69556F6817DB63368A3DC930E45` |

