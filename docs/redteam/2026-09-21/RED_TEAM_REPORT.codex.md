# Red team report — codex — 2026-09-21
Working tree: NexusVision @ `0eca7d2` (+ uncommitted), chiaki-ng-src @ `c7515213` (+ uncommitted)
Surfaces covered: A, B, C, D, E, S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11, S12, S13, S14, S15, S16 — targeted source, log, package, and offline-test review; not exhaustive runtime proof.
Surfaces NOT covered and why: native execution/builds, clean-machine installation, physical controller/capture fault injection, console receipt of input, live API abuse, and live billing/revocation were not performed. The task forbids builds, launches, and mutations. AWS/Cloudflare/Stripe environment credential presence checks were negative; deployed IAM/WAF/gateway/webhook/price configuration was not verified. No other team's report was consulted. Public Stripe documentation was read, not account infrastructure.
Method: independently traced the working trees and supplied evidence packet; bounded, line-numbered log reads; PE/import/marker inspection without loading binaries; 1,218 existing Python tests plus four isolated characterizations; website static verifier; strict-warning package integrity verifier. All source/config/package work was read-only. Verification details and coverage matrix follow the findings. The requested report date is retained; completion crossed into September 22 local time.

Evidence convention: source-proven paths are not claims of successful live exploitation. Reproduction items describe isolated, non-destructive validation; only tests explicitly named as executed in Evidence were run. No customer identifiers or secret values are included.

## CATASTROPHIC

### [CX-001] CATASTROPHIC — Invalid existing learning state can be silently replaced without a last-good recovery generation
- Surface: S1 settings and learning persistence.
- Exploit / failure path: An existing learning file becomes unreadable, empty, or invalid JSON. Loading treats that condition like absent first-run data; reload explicitly resets the in-memory learning object. A subsequent successful normal save can replace the only stored generation with default/new learning.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.cpp:79-86`, `:235-241`, `:397-400`, `:1194-1208`; profile-specific learning files selected at `:228-232`.
- Impact: Previously tuned learning/calibration can be lost with no product-provided last-good restore. This is the requested tuned-profile-loss severity. The separate customer lead in settings is NOT proven erased by this path; current QSaveFile writes also mean the historical in-place-truncation bug must not be described as still present.
- Reproduction:
  1. In a disposable AppConfig fixture, retain a non-default expected learning snapshot.
  2. Exercise unreadable, truncated, and zero-byte copies through load/reload, then a normal save after the storage fault clears.
  3. Compare the resulting state with the expected snapshot and check for an automatic recovery generation.
- Evidence: The helper returns the same empty object for read and parse failures; reload assigns `LearningData{}` before reading. No last-good restore exists in these paths. Atomic commit exists, but atomicity does not recover already-invalid input. The prior zero-byte incident is user-supplied history, not reproduced live in this audit.
- Root-cause hypothesis: Persistence distinguishes neither absent from invalid nor committed from recoverable generations.
  - Supporting evidence: The cited loader has no diagnostic/status result for invalid learning and the writer replaces one file.
  - Evidence that would refute it: A preceding recovery layer that restores exact learned values before these functions and demonstrably handles both invalid and temporarily inaccessible files.
- Required fix: Preserve a validated last-good generation; quarantine invalid data; expose recovery status; prevent automatic default overwrite until recovery or explicit reset is resolved.
- Verification test: Native temporary-directory tests for missing/empty/truncated/wrong-root/denied-read files; true first run may default, existing invalid profiles must recover exact values and retain corrupt evidence across a second restart.
- Owner decision needed: yes — backup retention and explicit-reset UX.
- Confidence: high for the source failure path; native fault test remains unrun.

### [CX-002] CATASTROPHIC — Stripe refund/dispute events do not revoke the paid entitlement
- Surface: S6 purchase → unlock; S10 website/Stripe.
- Exploit / failure path: A paid account receives a full refund or qualifying dispute without a subscription cancellation. The Worker has no payment-refund/dispute branch, returns `ignored`, and acknowledges the event; the backend entitlement remains active under the reviewed implementation.
- Affected: `C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:626-705`; `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:4813-4873` (`/api/bot/chargeback`).
- Impact: Under the requested refund/chargeback-revokes-access policy, a paid-only account can retain access after its qualifying entitlement is invalid. This is the supplied CATASTROPHIC entitlement class, not a claim that a live customer's account was tested.
- Reproduction:
  1. Use an offline payment-event fixture with a previously provisioned paid-only entitlement.
  2. Evaluate the refund and dispute event cases while the subscription remains active.
  3. Assert whether a revocation job is created and whether the fake entitlement changes; do not submit account events.
- Evidence: Only paid checkout/invoice, subscription deletion/canceled/unpaid, and payment-failed cases appear in the handler; all other types fall through at line 686 and receive success at 702. The backend already supports refunded/disputed/chargebacked revocation. Stripe documents that disputes normally leave a subscription cycling unless separate cancellation behavior is configured. [Stripe cancellation documentation](https://docs.stripe.com/billing/subscriptions/cancel).
- Root-cause hypothesis: Subscription lifecycle handling was mistaken for complete payment lifecycle handling.
  - Supporting evidence: No refund/dispute dispatch in the reviewed Worker; no corresponding revocation reconciler found in the reviewed production paths.
  - Evidence that would refute it: An independently verified deployed reconciler that processes those payment events, resolves the exact entitlement, and revokes within the required deadline. Live account configuration was not available here.
- Required fix: Add authenticated, idempotent payment-event reconciliation against the exact stored subscription; propagate the correct refund/dispute reason; preserve other valid entitlements and audit the decision.
- Verification test: Offline cases for full/partial refunds, dispute states, replay, out-of-order delivery, multiple subscriptions, identity mismatch, and revocation retry; confirm both backend revocation fields and role policy.
- Owner decision needed: yes — partial-refund policy and whether a newly opened dispute suspends or permanently revokes access.
- Confidence: high for missing source handling; deployed compensating controls unverified.

## CRITICAL

### [CX-004] CRITICAL — One local-delivery timeout stops the human input session; source fixes are not the incident binary
- Surface: A, D, E; S4 controller/network variance.
- Exploit / failure path: A required controller transaction misses the bridge's 25 ms local-delivery wait. The bridge aborts queued transport, sends a failed ACK, and stops the Chiaki session. The launcher closes its pipe, revokes timing authority, and begins recovery. An old delayed release echo can place a subsequent edge behind a 40 ms delay; queue saturation is not required.
- Affected: `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:20`, `:143-164`, `:279-291`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionInputClient.cpp:351-352`, `:565-570`; deployed `C:\Users\aaron\Desktop\NexusVision\native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe`.
- Impact: Session-wide loss of controller input during ordinary play. Capture-card preview can remain live, so the visible symptom looks like input-only disconnection. The scheduled bot must remain disarmed; weakening that proof is not a fix.
- Reproduction:
  1. In an existing native transport fixture, exercise adjacent ordinary button/trigger edges with a pending delayed release echo and a local-delivery deadline.
  2. Check required-edge latency, exact-sequence ACK, and whether the failure path requests session stop.
  3. Compare the exact deployed image identity against the corrected build before any later owner-approved playtest.
- Evidence: `C:\Users\aaron\Desktop\NexusVision\logs\orion_native.log:22212-22231` shows route revocation, failed repair, recovery, then Failed ACK stage 128/error 15. Line 22222 identifies the incident image SHA256 `0dad53db1267721b7e0db029b58f8447722e9b2f689e93405b65c0333527f149`; its current on-disk hash is identical. Current source ALREADY expedites optional repeats before a new required edge (`C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\feedbacksender.c:274-277`) and now includes terminal-trigger redundancy. That source improvement is not evidence that the incident image contains it.
- Root-cause hypothesis: The old release-spacing behavior can produce the observed bridge timeout; a local timeout is then escalated to a session-ending transport fault.
  - Supporting evidence: Explicit 25 ms/40 ms mismatch, failed ACK signature, unchanged deployed image, and a current source fix specifically removing that artificial wait. The E diagram below separates initiating failure from recovery consequences.
  - Evidence that would refute it: Exact-incident bridge trace showing another timeout cause, or proof that the incident image already includes equivalent queue-expedite logic. Per-packet bridge traces were not retained in the supplied excerpt.
- Required fix: Integrate and validate the queue-spacing/terminal-release changes in one coherent deployment unit; keep exact ACK, abort, and epoch fences. Do not raise timeouts or relabel a local ACK as console receipt to hide failures.
- Verification test: Existing native edge/repeat/barrier fixtures on the exact package; ordinary Circle+Triangle press/release orderings, trigger ramps, stick traffic, sequence wrap, and load. Require no deadline consumed by optional spacing, no duplicate press, and no old-route fire after a fault. Native execution was prohibited in this audit.
- Owner decision needed: no.
- Confidence: high for bridge-initiated stop and binary identity; medium for the precise packet sequence that caused the first incident.

### [CX-013] CRITICAL — Kill-switch read errors are interpreted as permission to continue
- Surface: S7 licence/lease; S9 backend/config.
- Exploit / failure path: The kill switch is enabled, but its configuration-table read fails while licence/signing dependencies remain healthy. The helper returns disabled instead of unknown/error, allowing activation/heartbeat callers to continue.
- Affected: `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:459-465`, `:591`, `:1729-1732`, `:1788`, `:2921`; activation, heartbeat, and shard gates.
- Impact: The owner's emergency kill can be written but not enforced during a selective config outage; successful lease issuance can prolong access. The lease TTL is 900 seconds, not instantaneous revocation.
- Reproduction:
  1. Run the exact helper with an in-memory config-table adapter that raises a storage error.
  2. Keep unrelated fake dependencies healthy and inspect the returned kill state and caller branch.
- Evidence: Executed `D:\NVRT-CX0922-233756\test_observed_contracts.py`; literal result `OBSERVED_KILL_READ_FAILURE=(False, empty_reason)`. Heartbeat rejects only when that boolean is true. No live kill was changed.
- Root-cause hypothesis: Configuration availability failure is collapsed into the default permissive value.
  - Supporting evidence: Unconditional `except Exception: return False, ""`.
  - Evidence that would refute it: A mandatory upstream independent kill gate that remains authoritative during this exact outage and blocks issuance, demonstrated for every caller.
- Required fix: Return an explicit unavailable state and deny new/renewed authority; never replace a known-enabled kill with disabled on read failure. Define a bounded existing-lease policy and alert on control-plane uncertainty.
- Verification test: Selective config outage with enabled kill, missing config, cold/warm processes, and existing leases; no fresh authority while state is unknown; documented recovery after healthy read.
- Owner decision needed: yes — permitted existing-lease grace, if any, during a control-plane outage.
- Confidence: high.

### [CX-014] CRITICAL — Destructive mutations succeed when the audit row cannot be written
- Surface: S9 backend/admin; S14 consoles.
- Exploit / failure path: A permitted destructive action updates its target, then the common audit write fails. The audit helper logs a warning and returns a success-looking identifier; the handler returns success without a durable audit row.
- Affected: `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:409-445`, `:3960-3978` (licence revoke), `:4633-4640` (config mutation), and other shared audit callers.
- Impact: Staff/owner destructive actions lack the required audit record during audit-table failure. The fallback warning contains only action/error, not the complete actor/target/reason record needed for recovery.
- Reproduction:
  1. In a local storage fixture, allow the target mutation and fail only audit persistence.
  2. Verify the returned operation/audit status and durable rows; inspect the fallback event's completeness.
- Evidence: Executed exact `audit()` helper characterization: `OBSERVED_AUDIT_FAILURE=returned_id_without_persisted_row`. Source orders revoke update before audit and returns success afterward. This was not a live destructive action.
- Root-cause hypothesis: Operational availability was prioritized over the mandatory mutation-plus-audit invariant.
  - Supporting evidence: The helper's docstring says it never raises; exceptions are swallowed at 443-445.
  - Evidence that would refute it: A durable, atomic audit outbox/stream carrying the full mutation context that is guaranteed independently of the failing write. None is used by this handler.
- Required fix: Couple mutation and durable audit/outbox atomically; use idempotent recovery. Do not simply throw after a mutation already committed and call that rollback.
- Verification test: Audit denial/throttle/outage before and after mutation, response loss, and retries; every committed destructive change must have one recoverable complete audit event.
- Owner decision needed: no.
- Confidence: high.

### [CX-015] CRITICAL — Persistent log I/O failure can block the GUI/input relay and prevent shutdown
- Surface: A/D reliability; S16 logging.
- Exploit / failure path: The log worker cannot open/write/flush its file and retries forever. Outstanding bytes reach the 8 MiB limit; the GUI's periodic enqueue then waits indefinitely for capacity. Shutdown also waits for enqueue/drain/join without an abort condition.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrderedFileLogSink.cpp:79-83`, `:106-130`, `:208-257`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrderedFileLogSink.h:23-27`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:14986-14996`.
- Impact: A full/denied/unavailable log volume can freeze normal launcher/input processing indefinitely. The independent GUI-freeze watchdog attempts neutral/disarm after six seconds (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:4902-4925`), which mitigates continued automation but does not restore human input service or unblock shutdown.
- Reproduction:
  1. Give a native sink fixture a failing writer/flush adapter and a small queue budget.
  2. Admit enough normal diagnostic traffic to fill the budget; measure enqueue and shutdown completion with independent fixture deadlines.
  3. Never fill or lock the real application's log disk.
- Evidence: Capacity wait has no timeout/cancellation predicate; open/write/flush retry loops have no stopping/error budget. GUI calls enqueue synchronously. Python source-contract tests pass but do not execute this native persistent-failure condition.
- Root-cause hypothesis: Lossless diagnostic delivery applies unbounded backpressure to a real-time application control path.
  - Supporting evidence: Queue memory is bounded, but time is unbounded; stop must first acquire the enqueue mutex and join the retrying worker.
  - Evidence that would refute it: A separate nonblocking admission layer or bounded failure exit before the GUI call, plus native permanently-failed-writer tests proving responsiveness.
- Required fix: Make diagnostic admission and shutdown bounded; retain a bounded emergency ring/drop counter and visible storage-fault state. Separate mandatory security audit durability from optional real-time telemetry.
- Verification test: Permanent and recoverable open/write/flush failures, rotation failure, and concurrent shutdown; bounded UI/neutralization/shutdown latency with explicit lost-log accounting.
- Owner decision needed: yes — diagnostic loss budget and shutdown timeout.
- Confidence: high for source deadlock/wait path; no live disk failure induced.

## HIGH

### [CX-008] HIGH — Legacy learned timing fields bypass the validation applied to newer fields
- Surface: S1 persistence; S2 timing seed/lead.
- Exploit / failure path: Invalid but syntactically valid learned clock/offset entries are read directly into learning maps and copied into engine configuration. Timing branches can consume them despite other newer fields having plausibility bounds.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.cpp:2113-2130`, `:2160-2178`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AutomationEngine.cpp:2093-2096`, `:2228-2235`, `:11631-11657`.
- Impact: Customers with a corrupted/imported older learning record can receive wrong per-type timing or unusable learned thresholds. This does not demonstrate fire without current vision: the later vision/reachability gates still apply.
- Reproduction:
  1. Supply an out-of-domain learned clock in a disposable parser/engine fixture.
  2. Inspect the parsed value, applied engine configuration, and selected clock threshold before any output is emitted.
  3. Compare with adjacent phase fields that reject invalid ranges.
- Evidence: Direct `toDouble()` insertion and engine copies are present; the cited consumption applies a lower floor, not a complete plausible upper bound. Adjacent phase fields at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.cpp:2134-2153` already implement bounded validity.
- Root-cause hypothesis: Incremental field-specific checks never became a common learning-schema validator.
  - Supporting evidence: Guarded and unguarded timing fields coexist in the same loader; settings checksum does not cover the learning file.
  - Evidence that would refute it: A complete downstream validator for each listed field before every reachable deadline branch. A single later vision gate is not such a validator.
- Required fix: Apply shared type/finite/domain/shot-type validation on load, import, migration, and save; retain last-good values and report rejected records.
- Verification test: Table-driven valid boundaries, wrong types, unknown buckets, and out-of-domain values; invalid persisted state must never become a trusted clock or silently replace a valid record.
- Owner decision needed: yes — supported domain ranges and partial-record salvage policy.
- Confidence: high for missing validation; exact gameplay effect of each legacy branch remains untested.

### [CX-016] HIGH — Monthly payment periods are converted to fixed 30-day entitlement extensions
- Surface: S6 renewal; S10 billing.
- Exploit / failure path: Checkout and renewal always provision 30 days. Backend expiry is `now + 30 days`, or `max(now, expiry) + 30 days`, rather than the verified paid invoice/subscription period end.
- Affected: `C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:638-668`; `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:2634-2637`, `:2692`, `:2728`.
- Impact: A 31-day paid month can lose a day of access before renewal; shorter months and delayed/replayed delivery can accumulate mismatched entitlement. This is an access-duration bug, not proof of a wrong charge amount.
- Reproduction:
  1. Use a paid monthly fixture spanning October 1 to November 1, 2026.
  2. Compare the exact current 30-day calculation with that paid period end.
  3. Repeat across February and delayed renewal processing.
- Evidence: Executed arithmetic characterization: `OBSERVED_31_DAY_PERIOD=access_expiry_2026-10-31; renewal_2026-11-01; gap_86400_seconds`. Worker/backend force 30 days. Stripe monthly anchors follow calendar dates rather than a constant 30-day duration. [Stripe billing-cycle documentation](https://docs.stripe.com/billing/subscriptions/billing-cycle).
- Root-cause hypothesis: A legacy duration-based mint contract was reused for recurring calendar billing.
  - Supporting evidence: No paid period boundary is transmitted in these provision payloads.
  - Evidence that would refute it: A verified price configured as every 30 days rather than monthly, with matching advertised terms, or a separate authoritative expiry reconciler. Neither was verified.
- Required fix: Derive entitlement from validated paid service-period boundaries and enforce monotonic, idempotent updates bound to subscription/invoice identity.
- Verification test: 28/29/30/31-day months, delayed events, duplicate events, cancellation, multiple subscriptions, and an existing longer valid entitlement.
- Owner decision needed: no — the specified product is monthly.
- Confidence: high for source/calculation mismatch; live price metadata unverified.

### [CX-017] HIGH — A process/storage failure after order claim can leave a paid purchase permanently pending
- Surface: S6 purchase → unlock; S9 backend.
- Exploit / failure path: The backend commits a spend-once order marker before minting or extending access. Failure before binding the completed entitlement leaves an unbound marker. Every later retry treats it as already claimed and returns pending; no claim lease or recovery transaction exists. Binding errors are also swallowed.
- Affected: `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:1830-1852`, `:2649-2657`, `:2700-2705`, `:2742-2743`.
- Impact: An individual paying customer may never unlock or receive the renewal without operator repair. This is not asserted to break every purchase.
- Reproduction:
  1. In an in-memory order fixture, allow claim persistence but simulate interruption before mint/bind.
  2. Retry the same order and inspect pending/completed state without creating a new order.
- Evidence: Exact `_claim_order()` characterization returned `OBSERVED_INTERRUPTED_ORDER_RETRIES=empty,empty,empty; no minted row`. Marker has no claim deadline/completion transaction; caller maps empty prior result to 503 `order_pending`. No real order was created.
- Root-cause hypothesis: At-most-once claiming is implemented without crash-safe completion/reconciliation.
  - Supporting evidence: Claim, entitlement mutation, and marker binding are separate writes; bind failure only logs.
  - Evidence that would refute it: A deployed recovery worker that safely reconciles every unbound claim against authoritative entitlement state, with interruption tests and exact-order evidence.
- Required fix: Transactionally bind completion with entitlement mutation or implement a recoverable idempotent state machine. Never blindly expire an unbound marker and mint again; the original mutation might already have committed.
- Verification test: Fault each persistence boundary, lose responses, and retry concurrently; each paid order produces one correct entitlement and eventually reaches a terminal recorded state.
- Owner decision needed: no.
- Confidence: high.

### [CX-018] HIGH — Optional runtime DLLs retain ambient search-path fallback without the crypto provider's trust gate
- Surface: S5 service client; S15 runtime/package trust.
- Exploit / failure path: When the intended local DLL cannot load, VeniceNet and ViGEm loaders fall back to bare library names. Those paths do not verify the loaded file against the signed manifest before loading; ABI/export checking occurs afterward.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\VeniceNetClient.cpp:37-84`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\VirtualController.cpp:263-281`; call site `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:15479-15500`.
- Impact: A writable ambient search location can supply unintended code in a missing/quarantined-DLL scenario. This is a source-level local load-trust defect; no DLL payload, local execution demonstration, elevation, remote-code execution, or entitlement unlock was produced.
- Reproduction:
  1. Inspect the loader in an inert loader-mock test with the intended library unavailable.
  2. Record the fallback candidate and whether byte/path trust is checked before the mocked load call.
  3. Validate refusal rather than substituting or executing a DLL.
- Evidence: Both fallbacks are unconditional in these methods. VeniceNet is loaded before the helper's feature-disabled return. In contrast, `C:\Users\aaron\Desktop\NexusVision\native_orion\src\Ed25519.cpp:208-269` requires an app-local build-pinned crypto provider in production. Package hashing proves packaged bytes, not ambient fallback bytes.
- Root-cause hypothesis: Convenience loader fallback was not brought under the production trust policy.
  - Supporting evidence: Explicit bare-name loads, followed only by export/ABI validation.
  - Evidence that would refute it: A mandatory pre-load verifier plus process-wide restricted DLL search policy that excludes every untrusted candidate and dependency path in the supported production entry point.
- Required fix: Production must resolve a canonical manifest-covered library and dependencies, verify before loading, and fail visibly when absent/invalid; keep any developer fallback compile-gated.
- Verification test: Loader-mock candidates for absent, quarantined, wrong-hash, wrong-ABI, moved install, and ambient unrelated libraries; no unverified load may be attempted. Follow with an owner-approved clean-machine package test.
- Owner decision needed: no.
- Confidence: high for unsafe fallback; exploitability depends on deployment/search-path permissions.

### [CX-019] HIGH — Support diagnostics contain raw console/network identifiers
- Surface: S16 log privacy.
- Exploit / failure path: Session and promotion messages include the configured console address and identifying path/name context; the launcher persists those messages into support logs without a redaction/export boundary.
- Affected: `C:\Users\aaron\Desktop\NexusVision\remote_play_client.py:2597`; `C:\Users\aaron\Desktop\NexusVision\remote_play_orchestrator.py:3170`; `C:\Users\aaron\Desktop\NexusVision\logs\orion_native.log:1808`, `:22222`; native log append at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:14977-14981`.
- Impact: Customers pasting raw logs disclose local network and workstation context. HIGH follows the request's explicit treatment of court/network identifiers as PII; this is not evidence of a leaked private key, password, or Discord token.
- Reproduction:
  1. Feed synthetic address/nickname/path values through a log-format fixture.
  2. Inspect the persisted/support-export representation for those literals.
- Evidence: Bounded inspection found `console_ip=[IP REDACTED]` at native log line 1808 and a full workstation deployment path at 22222. Values are deliberately not reproduced here.
- Root-cause hypothesis: Debug identity messages are reused as customer-shareable support logs.
  - Supporting evidence: Source formatting contains raw fields; persistent append does not redact them.
  - Evidence that would refute it: A mandatory support-export sanitizer with tests covering all listed fields and guidance preventing raw-log sharing. Such a boundary was not established in this review.
- Required fix: Default to stable session-local aliases/redacted paths; offer an explicit privileged diagnostic mode if raw values are genuinely necessary; sanitize support exports and document retention.
- Verification test: Synthetic addresses, user-directory names, Discord identifiers, emails, codes, keys, and nested error strings; assert default logs/exports contain none of the raw identifiers or secrets.
- Owner decision needed: yes — opt-in diagnostic identifiers and retention policy.
- Confidence: high for observed raw identifiers.

## MEDIUM

### [CX-003] MEDIUM — Dynamic account-page price copy disagrees with the approved storefront
- Surface: S6/S10 customer billing presentation.
- Exploit / failure path: The authenticated account-page template displays `$20/month`; the public storefront and backend confirmation display `$19.99/month`. The static verifier does not render the account page.
- Affected: `C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:531`; `C:\Users\aaron\Desktop\NexusVision\website\public\index.html:7`; `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:2590`.
- Impact: Conflicting purchase copy. A different amount actually charged than the final checkout displays would be CATASTROPHIC, but no charge or live price object was inspected; that stronger assertion from the preserved draft is not substantiated here.
- Reproduction:
  1. Render the account page with a synthetic authenticated session in a local Worker fixture.
  2. Compare its price label with the storefront/checkout contract.
- Evidence: The stale literal is present in current source, independently reread. Website static verification passes because its supplied input is the static HTML.
- Root-cause hypothesis: Price text is duplicated outside a shared rendered-route contract.
  - Supporting evidence: Independent literals and a static-only verifier.
  - Evidence that would refute it: A current deploy transform replacing that exact dynamic label before publication, with output evidence.
- Required fix: Use one reviewed price representation and verify every rendered customer route against the configured price metadata.
- Verification test: Public page, account page, checkout, and confirmation fixtures all show the approved amount/currency/interval; deployment gate compares the actual configured price without exposing credentials.
- Owner decision needed: no.
- Confidence: high for copy mismatch; actual charged amount unverified.

### [CX-006] MEDIUM — Missing settings checksum is treated as first run even when settings already exist
- Surface: S1 settings integrity.
- Exploit / failure path: Startup loads existing settings before checking whether their companion checksum exists; absence triggers saving and generating a new checksum. An existing unsigned/mismatched settings state can therefore be silently blessed as first run.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:1953-1980`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SecurityManager.cpp:469-476`, `:792-817`, `:915-926`.
- Impact: The advertised settings-integrity decision is unreliable. The checksum is not an authenticator against a same-user process. This is NOT a demonstrated bypass of signed release integrity, paid entitlement, or the compiled production gates.
- Reproduction:
  1. Exercise bootstrap with a disposable existing settings file and missing companion metadata.
  2. Assert whether startup distinguishes that state from a truly absent first-run profile and preserves the existing file.
- Evidence: The startup comment acknowledges the missing-signature case and the branch performs it; digest construction is unkeyed SHA256 of machine-local material and settings. The same branch also resolves the genuine first-launch bootstrap, so do not remove it without a replacement first-run path.
- Root-cause hypothesis: Checksum absence is used as initialization evidence, conflating recovery, tampering, and first run.
  - Supporting evidence: No independent initialization predicate in the branch.
  - Evidence that would refute it: A production pre-bootstrap validator authenticating existing settings before this save.
- Required fix: Distinguish absent/invalid/existing state, preserve evidence, and make recovery explicit. Keep security-critical policy server-signed or compiled independently of user-editable preferences.
- Verification test: Genuine first run, missing checksum on existing data, corrupted data, wrong-machine metadata, denied writes, and concurrent startup; no silent trust reset or tuned-data overwrite.
- Owner decision needed: yes — whether this artifact is a corruption checksum or a genuine security boundary; UI/docs and implementation must agree.
- Confidence: high.

### [CX-007] MEDIUM — Calibration Cancel means save partial progress, contrary to its label
- Surface: S2 calibration UX/persistence.
- Exploit / failure path: Each changed verdict immediately persists the lead and user-set latch. Cancel stops the flow without restoring the original values, although the visible control is named Cancel.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:9184-9202`, `:9222-9255`; `C:\Users\aaron\Desktop\NexusVision\native_orion\qml\components\ShotLeadCard.qml:389-403`.
- Impact: Recoverable but misleading behavior after a mistaken verdict or abandoned calibration. The code logs the retained value; this is not proof that every partial calibration makes timing wrong.
- Reproduction:
  1. In a controller/config fixture, begin from a known lead and accept one changed verdict.
  2. Cancel, reload, and compare value and user-set latch with the original.
- Evidence: Immediate persistence and intentional retain-on-cancel behavior are explicit in current source; no original-value transaction is kept by this flow.
- Root-cause hypothesis: Incremental crash-resilient saving and conventional cancel semantics were mixed.
  - Supporting evidence: QML label and implementation disagree.
  - Evidence that would refute it: Pre-action UI explicitly explaining that Cancel retains progress, rather than only logging it afterward.
- Required fix: Either provide Restore original versus Save and exit, or rename/explain the existing behavior before grading begins; preserve route-specific state correctly.
- Verification test: Cancel/restore, save/exit, crash-resume, and route changes with exact value/latch comparisons.
- Owner decision needed: yes — discard, retain, or separately resume partial progress.
- Confidence: high.

### [CX-020] MEDIUM — Calibration can claim a 300 ms saved result without applying that initial candidate
- Surface: S2 lead calibration.
- Exploit / failure path: With configured lead zero, begin creates an internal 300 ms candidate but does not apply it. Good verdicts leave that candidate unchanged; three Good verdicts lock it, while persistence runs only when the candidate changes. The UI can report Saved although configured lead remains zero or has independently auto-seeded elsewhere.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:9132-9135`, `:9176-9181`, `:9222-9228`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\LeadCalibrationPolicy.h:89-96`; default `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.h:905`.
- Impact: A new customer's guided setup can report a value it did not test/apply/save. This is a source-level state mismatch, not an observed live three-shot result.
- Reproduction:
  1. Start a controller fixture with a running-session flag and zero configured lead.
  2. Begin calibration and submit three Good verdicts without other mutations.
  3. Compare locked/displayed candidate, active engine lead, user-set latch, and saved settings.
- Evidence: Begin assigns only `leadCal_`; Good increments/locks without changing `leadMs`; setter is conditional on `leadMs != before`. The success hint unconditionally says Saved.
- Root-cause hypothesis: Candidate, applied state, and committed state are not represented separately.
  - Supporting evidence: No initial apply or final unconditional successful-commit check exists in these methods.
  - Evidence that would refute it: An intervening mandatory apply/commit path tied to this calibration instance and route, with a test of the zero-lead/Good-only case.
- Required fix: Explicitly apply the initial candidate before grading, track the actual route/value used, and declare completion only after verified persistence; freeze/reconcile competing seed/trim changes during the flow.
- Verification test: Zero/nonzero/clamped initial lead, Good-only, Skip-only, reversal, failed save, concurrent auto-seed, manual slider, and route switch; displayed/applied/persisted values must agree.
- Owner decision needed: no.
- Confidence: high for source branch behavior; native UI test unrun.

### [CX-022] MEDIUM — Dismissed one-time secrets remain available in the raw-response viewer
- Surface: S14 owner/staff console handling.
- Exploit / failure path: A successful response populates the dedicated one-time secret field and also stores the entire response in `resultText_`. Dismissing the secret clears only the dedicated field; Raw response still presents the previous payload until explicitly cleared or replaced.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AdminToolController.cpp:1111-1114`, `:690-694`, `:814-823`, `:1249-1271`, `:1351-1358`; `C:\Users\aaron\Desktop\NexusVision\native_orion\qml\admin\AdminMain.qml:410-427`.
- Impact: The show-once/dismissal contract is false and screen-sharing/support-copy risk increases. No secret was displayed or retrieved in this audit, and no cross-account or persistent-file exposure was demonstrated; hence not a CATASTROPHIC secret-exposure claim.
- Reproduction:
  1. Inject a synthetic secret-bearing success response into an isolated controller/view fixture.
  2. Dismiss its one-time card and inspect the raw-result model for the synthetic marker.
- Evidence: Unredacted JSON is stored after `handleSuccess`; clear methods do not clear/redact `resultText_`; QML binds Raw response directly to that field.
- Root-cause hypothesis: Secret-specific UX and generic response diagnostics maintain independent copies.
  - Supporting evidence: Whole-object serialization follows the one-time success handling.
  - Evidence that would refute it: A mandatory sanitizer before `setResult` or a dismissal path clearing every representation, demonstrated with synthetic markers.
- Required fix: Redact sensitive response fields before generic display/signals; clear dedicated one-time representations on dismissal/session reset; keep request diagnostics metadata-only.
- Verification test: Synthetic staff tokens, generated keys, enrollment data, TOTP material, rotated secrets, and pairing codes never appear in generic result text or diagnostics; dismissal cannot reveal them again.
- Owner decision needed: no.
- Confidence: high.

## LOW

### [CX-009] LOW — Topology-aware affinity failures are discarded and placement is not performance-class-aware
- Surface: D fork affinity; S4 hardware variability.
- Exploit / failure path: The helper enumerates physical cores and returns an error when a requested core/affinity cannot be selected. The callback discards that result; enumeration position also does not explicitly choose a hybrid CPU's performance class.
- Affected: `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\thread.c:149-200`; `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\streamsession.cpp:55-71`.
- Impact: Missing placement diagnostics and an unmeasured performance assumption. The prior hard-coded LP0/LP1/SMT claim is not accurate for current source. No crash or measurable timing regression on another CPU was established.
- Reproduction:
  1. Use a topology/affinity adapter fixture that returns absent cores, multiple groups, and rejected affinity requests.
  2. Check whether the caller records applied versus fallback placement.
- Evidence: Current helper uses `GetLogicalProcessorInformationEx(RelationProcessorCore)` and `SetThreadGroupAffinity`, checks main API failures, and returns errors; callback ignores the result.
- Root-cause hypothesis: Placement optimization lacks observable fallback semantics and explicit hybrid policy.
  - Supporting evidence: Returned status is discarded; no efficiency-class selection in the reviewed helper.
  - Evidence that would refute it: Another caller-level placement telemetry layer or platform tests demonstrating a deliberate supported topology policy.
- Required fix: Record chosen group/mask and failed placement once; default to OS scheduling on failure; benchmark before selecting P/E classes.
- Verification test: 4-core, non-SMT, hybrid, VM, and multigroup fixtures plus later hardware measurements; failure must remain nonfatal and observable.
- Owner decision needed: yes — supported hardware/performance qualification matrix.
- Confidence: high for missing diagnostics; performance consequence unverified.

### [CX-012] LOW — Updater component checks are still separate from final filesystem writes
- Surface: S8/S15 updater namespace integrity.
- Exploit / failure path: Path components are checked before path-based remove/copy operations, leaving a residual namespace-change interval in a concurrently writable destination. The current implementation explicitly documents this limitation.
- Affected: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\UpdaterArchive.h:30-39`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\UpdaterArchive.cpp:212-231`.
- Impact: Residual robustness/security risk dependent on installation ACLs and concurrent write authority. No out-of-root write or privilege-boundary crossing was demonstrated; do not equate this with acceptance of an unsigned update.
- Reproduction:
  1. Review the check/write sequence with an inert filesystem adapter that models a component identity change.
  2. Confirm final operations remain bound to the originally validated object; do not race a live installer or privileged path.
- Evidence: Repeated reparse rejection is present, but final copy remains a separate path-based call. Header explicitly records the residual. Current strict-warning package integrity verification passed.
- Root-cause hypothesis: Namespace validation and final operation lack one stable handle-bound identity.
  - Supporting evidence: Separate validation/remove/copy operations in the cited path.
  - Evidence that would refute it: Handle-relative no-follow final operations, or enforced supported-install ACL invariants excluding cross-boundary concurrent mutation.
- Required fix: Prefer handle-bound no-follow traversal/final writes; retain current checks and verify install ACLs. Track this as an explicit residual rather than claiming complete race resistance.
- Verification test: Deterministic filesystem-adapter identity-change tests and later controlled native integration, with zero out-of-root writes and exact rollback preservation.
- Owner decision needed: yes — supported writable/portable installs and residual acceptance.
- Confidence: high for source limitation; practical privilege escalation unproven.

### [CX-021] LOW — Discord interactions Worker still advertises a three-day trial
- Surface: S11 Discord; S6 purchase copy.
- Exploit / failure path: The interactions Worker's purchase response uses the historical three-day text while the gateway bot, website, and backend define seven days.
- Affected: `C:\Users\aaron\Desktop\NexusVision\discord_launch\orion_worker.js:246`; `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:95`, `:2191`; `C:\Users\aaron\Desktop\NexusVision\discord_launch\orion_bot.py:685`.
- Impact: Misleading source/deployable copy if that Worker path is active; no entitlement duration bypass demonstrated.
- Reproduction:
  1. Render the offline purchase response from both Discord entry points.
  2. Compare their trial labels with the backend's configured duration.
- Evidence: Current Worker literal says three days; backend constant and gateway command say seven. Deployment of that Worker version was not checked.
- Root-cause hypothesis: Trial copy was updated on one command delivery path but not the other.
  - Supporting evidence: Conflicting literals in the two implementations.
  - Evidence that would refute it: Proof that the old response is excluded from the active deployable command surface.
- Required fix: Share a tested product-copy contract or retire the unused path explicitly.
- Verification test: Both supported command renderers and website agree on seven days; fixture-only, no command registration/posts.
- Owner decision needed: no.
- Confidence: high for source mismatch; deployed visibility unverified.

## Wave 1: causal trace and recovery state machine

### First incident: what the evidence does and does not establish

All timestamps in this subsection are UTC. Log citations refer to `C:\Users\aaron\Desktop\NexusVision\logs\orion_native.log`.

1. Lines 22207 and 22209 show a connected bridge and writes/ACKs at 8933/8933 and then 8934/8934. At line 22211, writes reach 8961 while the last required ACK remains 8939; `ack_attempted=0` for the latest packet. **Subtracting these counters does not measure queued unacknowledged transactions.** Ordinary unflagged movement need not request an ACK. The 27 additional writes are mapped-state deliveries, not a raw physical report trace proving that only Circle+Triangle produced them.
2. At 03:11:12.550, line 22212 revokes scheduled authority after the live controller route changes. The next two lines show repair already failing and fallback proof awaiting an exact sidecar echo. The capture preview remains approximately 60 fps at line 22216; that does not prove the separate controller session is alive.
3. Lines 22217-22223 show scope-transition proof revocation, route re-keying, and input-only recovery. At 03:11:13.072, line 22228 reports Failed ACK stage 128/error 15 with exact expected sequence 8977. At 03:11:14.827, line 22231 reports readiness failure. Heartbeats summarize earlier events; their print order is not a per-packet chronology.
4. The bridge's explicit failure rule is verified: `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:20`, `:279-291` waits 25 ms for required local delivery; `:143-164` sends failure, aborts the queue, and requests `chiaki_session_stop`. The launcher reacts to an explicit Failed ACK at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionInputClient.cpp:351-352`, `:565-570`. This is not a rule that kills a session merely because cumulative write and ACK counters differ.
5. Separate launcher budgets are a 3 ms pipe-write deadline, 25 ms enqueue ACK budget, and 50 ms final ACK budget (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionInputClient.cpp:73-83`, `:358`). The final budget begins after the matching Enqueued reply. The bridge may emit a negative final ACK before the launcher's own deadline expires.

The bridge-stop failure class is supported by source and the negative-ACK signature. The **exact first flagged packet, queue occupancy, and physical chord ordering were not recorded**, so the 27-write burst alone cannot prove which packet consumed the deadline. Historical optional release spacing is a plausible mechanism, not a binary-disassembly finding. A human chord does not need to fill the 64-entry queue to encounter a delayed optional repeat. Current source already expedites repeats before a required edge; its correctness and deployment remain separate questions.

### Periodic recovery sequence (surface E)

```text
Physical controller -> launcher: fresh selected-device state
Launcher -> bridge: sequenced controller transaction / proof request
Bridge -> feedback sender: enqueue required local delivery
Bridge -> launcher: explicit Failed ACK if delivery misses 25 ms or mismatches
Bridge -> Chiaki session: stop requested; queued authority aborted
Launcher -> automation: route generation changes; old scheduled authority revoked
Launcher -> fallback: ViGEm/XUSB route selected; neutral-delivery proof awaits scope echo
Sidecar -> launcher: new route/scope; stale route evidence cannot restore authority
Launcher -> input child: bounded input-only recovery; capture may remain live
Input child -> sidecar: readiness fails if its Chiaki session has already ended
Launcher -> new bridge: fresh current-device state / forced proof delivery
Sidecar -> launcher: exact current route/scope echo and fresh health
Launcher -> automation: recovery gate clears only with accepted current proof and
                        physical shot controls neutral; old fire is not replayed
Later required transaction -> same runtime fault: cycle can recur
```

Source anchors for the transitions:

- Route authority is revoked **before automation processing** at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:12963-12983`. A failed scheduled transaction clears worker authorization, pending grade, release marker, route context, and engine state at `:12998-13043`.
- Recovery checks physical Square/right-stick shot intent for three neutral polls at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:13061-13074`; output is taken from the currently selected device at `:13076`, not blindly replayed from the failed transaction. Accepted forced delivery and current proof are required at `:13910-13937`; fallback isolation is logged at `:13939`.
- The repair failure diagnostic is emitted at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:13709-13716`. A repair can itself be the flagged transaction that fails. That logging branch does not independently prove a second automatic escalation or identify the first packet.
- Input-only recovery and contained escalation are at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:13991-14067`. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SidecarWatchdog.h:86-91` specifies 5 heartbeats before recovery, 15 between retries, at most 3 attempts, then one contained restart. These constants are heartbeat counts, not a measured fixed wall-clock flap period.
- `C:\Users\aaron\Desktop\NexusVision\remote_play_orchestrator.py:2786-2831` checks child readiness and cleans up a failed child. `C:\Users\aaron\Desktop\NexusVision\remote_play_client.py:1825` identifies an already-ended Chiaki session. In the observed negative-ACK cycle, readiness failure is a **consequence**, not evidence that the vision-health check initiated the original close.

Later recurrence evidence: route generation 16 at line 24473 (03:22:12.656), readiness failure at 24492, recovery at 24506, repair failure/result 5 at 24514, and another recovery at 24550. Lines 24568-24571 show another failed repair and generation 18; readiness fails at 24592 and recovers at 24605. Later generation transitions appear at 24625 and 24727, with Failed ACK/error 15 at 24735. These establish repeated loss/recovery, not an exact initiator for every close. The cadence is compatible with bounded recovery followed by recurrent bridge failure; the excerpts do not establish an independent launcher/sidecar livelock.

**Minimal next engineering step:** validate and integrate the already-present queue-expedite and terminal-trigger-release changes as a coherent, hash-identified native/fork deployment unit. Current `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\feedbacksender.c:263-277`, `:328`, `:365` prechecks whole transactions and expedites optional repeats before required edges/barriers. Retain the deadline, exact-sequence checks, route revocation, and fresh-epoch gates. Raising a timeout or trusting a fallback immediately would conceal rather than close CX-004. Stop/recovery termination must be demonstrated on the exact new runtime, not inferred from passing source contracts.

### R2, coalescing, ownership, and shortcuts

- Current coalescing only folds compatible unflagged motion; it stops at flags or changed buttons/L2/R2 (`C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:180-214`). Queue capacity is 64; the optional echo constant is 40 ms (`C:\Users\aaron\Desktop\chiaki-ng-src\lib\include\chiaki\orioninput.h:22`, `:28`). Queue saturation was not measured in the incident.
- Current `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\orioninput.c:64-81` **does include terminal analog-trigger down-to-zero release redundancy**, provided the transition does not also introduce a new press/increased trigger. The evidence packet's blanket statement that analog triggers lack redundancy is stale for this working tree. Optional twin/echo scheduling is in `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\feedbacksender.c:1139-1155`.
- A local-delivery ACK is not a console-receipt ACK; the bridge explicitly logs `console_ack=0` at `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:310`. Finite UDP repeats cannot by themselves prove every remote release arrived. Conversely, that limitation does not establish the reported phantom R2 root cause.
- `C:\Users\aaron\Desktop\NexusVision\native_orion\src\VirtualController.cpp:342-347` maps both triggers to XUSB. The recovery predicate in `C:\Users\aaron\Desktop\NexusVision\native_orion\src\ControllerRoutingPolicy.h:209-215` concerns **shot-control neutrality**, not every physical input being zero. A user physically holding R2 for sprint is legitimate; blindly forcing all triggers to zero at every recovery would be a regression. Stale input, mapped output, and game-observed state need a correlated trace to distinguish a real latch from a real held trigger.
- SDL/ViGEm ownership masking appears at `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\streamsession.cpp:1315`, `:1386`. Existing Python contracts cover route/epoch/idempotence behavior, not native hook scheduling or actual button receipt. The engine's once-per-press/pending-epoch backstop is at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\AutomationEngine.cpp:21607-21616`; abort clears copied precise deadlines at `:23666` onward. No independently confirmed current stale-epoch fire or duplicate fire was produced.
- **No hardcoded Circle+Triangle quit/stream shortcut was found on the injected-input path.** There is a configurable upstream GUI menu chord: `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\qmlbackend.cpp:1885-1902`, default mappings in `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\settings.cpp:1090-1111` (L1/R1/L3/R3), and the GUI chord action at `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\qmlcontroller.cpp:69-70`. Individual Circle/Triangle GUI mappings at `:10-18` are not a hardcoded two-button stream-close rule. The owner's effective custom GUI shortcut configuration was not verified.

Other candidate initiators to distinguish in later bounded fixtures: write/final-ACK timeouts, enqueue exhaustion, owned-pipe exit (`C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:251-258`, `:334-339`), controller unplug/stale reports, sidecar restart, court/source changes, and log-sink backpressure (CX-015). These are not all claimed as observed causes of the supplied incidents.

## Coverage matrix and unresolved verification

"Reviewed" below means the identified current source/log/test surface was inspected. It does not mean every proposed adversarial or hardware case was executed.

| Surface | Reviewed result | Remaining evidence needed |
|---|---|---|
| A — controller input | CX-004/CX-015; sequence/deadline/coalescing/recovery trace above. Trigger mapping and current analog redundancy inspected. | Exact incident per-packet trace; native sequence-wrap, reconnect, simultaneous chord/trigger/stick tests and actual controller-rest confirmation. |
| B — automation | Old-route authority clears before engine processing; once-per-press and abort fences reviewed. CX-008 affects learned timing. | Native scheduled-fire/physical-release races, starvation/cooldown behavior, and labelled gameplay; Python contracts do not prove native timing. |
| C — meter/sidecar | Fresh-onset, async lifecycle, gate atomicity, idempotence, fade budget, HSV and capture contracts passed. Dark-frame rejection in `C:\Users\aaron\Desktop\NexusVision\remote_play_orchestrator.py:5686-5752` revokes usable evidence; display liveness is separate. | Labelled near/far/fade/feedback-meter corpus, DirectML-to-CPU failure, locked temp directories and sustained load. No claim of near-perfect detection or eliminated game variance. |
| D — session/decoder/fork | CX-004/CX-009; source ordering and decoder identity tests passed. `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\time.c:11-22` uses 64-bit QPC quotient/remainder, not an assumed 32-bit microsecond clock. | Native thread/handle lifecycle and cross-topology soak. Diagnostic AV-clock output at `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\time.c:72-81` opens the supplied path with `wb`; use unique no-follow destinations. No live overwrite was induced. `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\takion.c:465` avoids the senkusha double-open case. |
| E — route recovery | Detailed causal diagram above; bounded recovery does not restore stale scheduled fire. | Trace every close origin on the exact integrated image; demonstrate flap cessation while proof remains fail-closed. |
| S1 — settings/learning | CX-001/CX-006/CX-008. QSaveFile exists for settings, learning, and checksum (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\AppConfig.cpp:1041-1049`, `:1194-1202`; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SecurityManager.cpp:792-817`). Settings/learning writes were not performed. | Multi-file commit and last-good generation tests. No QLockFile was found; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\main.cpp:342-349` only short-circuits a successful deep-link handoff, not every ordinary second launch. Concurrent stale-writer loss is an additional unexecuted fixture case, not a second proven loss finding. |
| S2 — latency/lead | CX-007/CX-020 and legacy validation CX-008. Factory/cache authority is route-scoped (`C:\Users\aaron\Desktop\NexusVision\latency_estimator.py:2761-2776`, `:2873-2875`, `:2918`); Early decreases and Late increases the candidate (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\LeadCalibrationPolicy.h:69-110`). | Native zero-lead/Good-only/save-failure tests; wrong verdicts, Skip-only, competing trim/seed and route changes. No customer lead was changed. |
| S3 — capture tier | Capture API fallback at `C:\Users\aaron\Desktop\NexusVision\capture_card_backend.py:679-727` includes actual backend/index route identity; measured cadence relock at `:1436-1456` addresses requested 120/delivered 60. Relevant Python tests passed. | Real busy/unplugged cards, two-device selection, format/DPI/multimonitor changes, SHM saturation and host-specific latency. Historical failure statements are not automatic current-source findings. |
| S4 — hardware/network | CX-004/CX-009. Real-report USB diagnostics at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:14890-14911`; PS5-wake Python tests passed using mocks. | USB/Bluetooth/Edge/third-party pads, suspend, hot-unplug, two-pad identity, wake, DHCP/court changes and loss bursts. No console/network fault injection occurred. |
| S5 — meter-delay service | CX-018. Client gates playing/court/support at `C:\Users\aaron\Desktop\NexusVision\native_orion\src\MeterDelayController.cpp:350-367`; delay/retarget/flush/drain code reviewed in `C:\Users\aaron\Desktop\NexusVision\native_orion\venicenet_service\MeterDelayIntercept.cpp:135`, `:398`, `:625`, `:722`. Service IPC is loopback TCP, not the controller named pipe. | Service absent/stopped/retarget/headroom and uninstall integration. Token ACLs and constant-time auth at `C:\Users\aaron\Desktop\NexusVision\native_orion\venicenet_service\IpcServer.cpp:143-151`, `:468-490`, `:675-715` are source evidence, not a live service pass. |
| S6 — purchase/unlock | CX-002/CX-003/CX-016/CX-017. Seven-day trial, machine claim, one-time pairing and staff delivery inspected; backend/Discord tests passed. `C:\Users\aaron\Desktop\NexusVision\backend\lambda_function.py:703-734`, `:2185`, `:2422`, `:2490-2505`. | Live-account route/webhook/price/role/renewal/refund configuration; duplicate or out-of-order event reconciliation. A public Discord ID is not sufficient pairing authority in the reviewed path. |
| S7 — licence/offline | CX-013. Signed lease and DPAPI cache code inspected; invalid lease does not renew authority and adverse auth results disable it (`C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:2368-2389`). Heartbeat is configured every five minutes at `:2405-2409`; server lease TTL is 900 seconds. | Revocation latency under DNS/outage/skew/captive-portal conditions on the actual package. No operational cache/HWID/auth bypass was attempted or demonstrated. |
| S8 — updater/package | 577-file strict-warning integrity pass. Signature/key/hash/HTTPS/version/archive filters and rollback-reporting source/tests reviewed; CX-012 residual. Crypto is build-pinned; absent optional `update_pubkeys.json` is not by itself a defect. | Native negative-case install/rollback and clean-machine launch. No unsigned update acceptance demonstrated. Full StrictSecurity was not run because it invokes prohibited build work. |
| S9 — API/admin | CX-013/CX-014/CX-017. Capability checks, disabled-staff guards, reason requirements, nonce/rate-limit and staff-auth tests reviewed; 360 backend plus separately invoked 17 staff tests passed. | Deployed five gateway routes, IAM least privilege, audit GSIs, WAF/rate limits, monitoring and incident recovery. Source route presence is not deployment evidence. |
| S10 — website/Stripe | CX-002/CX-003/CX-016. Safe return paths, cookie attributes, security headers, guild check, signature window and body bounds reviewed at `C:\Users\aaron\Desktop\NexusVision\website\src\worker.js:30-66`, `:135`, `:303-325`, `:358-361`, `:689`. Static site verifier passed. | Live price object, portal and webhook subscriptions. Per-click checkout UUID at `:325` deserves duplicate-click/existing-subscription tests; no duplicate charge was observed. |
| S11 — Discord | CX-021; 128 offline tests passed. Nereus disables mentions (`C:\Users\aaron\Desktop\NexusVision\discord_launch\nereus_bot.py:99-104`); Guard persistent verification components were reviewed, not posted. | Deployed command scopes, ephemeral visibility and verify-button survival after message PATCH. No bot posts/registrations or role mutations performed. |
| S12 — installer/first launch | Packaging/installer source tests passed. `C:\Users\aaron\Desktop\NexusVision\installer\orion.iss:92`, `:167`, `:411`, `:437`, `:516`, `:568-580` covers elevation, prerequisites, service handling and meter-delay registration. | Clean VM, standard-user elevation, Unicode/space paths, upgrade/uninstall, driver/provider/AV failures. Launch selection is distribution-mode-dependent (`:51-54`); direct Native launch is not universally wrong independent of that mode. |
| S13 — experimental gates | `C:\Users\aaron\Desktop\NexusVision\native_orion\src\RemotePlaySession.cpp:962-976` gates Xbox acknowledgement/window and missing executable; `C:\Users\aaron\Desktop\NexusVision\native_orion\src\RemotePlayExecutablePolicy.h:34-79`, `:162-175` checks bundled producer identity and process/pipe binding. | Actual tier transitions and late-frame refusal on native package. No PSN/NBA/Xbox service testing performed. |
| S14 — owner/staff tools | CX-014/CX-022. CLI and capability tests passed; reviewed TLS failure handling and server-enforced permissions rather than treating hidden UI controls as authorization. | Native startup-security entry points and kill engage/disengage UI round trip. Consoles were not launched and no secrets were generated. |
| S15 — binary/package | CX-018/CX-012. Static PE/import/marker review of six images; customer and dev binaries differ. Package has service in `C:\Users\aaron\Desktop\NexusVision\release\orion-package\packet_bridge\VeniceNetSvc.exe`, not at package root. | Native pre-load trust and supported install ACLs, moved/renamed package behavior, integrity refusals. No replacement DLL, patched executable or exploit artifact was made. |
| S16 — logs/telemetry | CX-015/CX-019. Current native rotation setting is 16 MiB with a previous file, not the evidence packet's old 8 MB statement; queued-byte bound is a separate 8 MiB. Warning relay/throttling reviewed. | Support-bundle redaction and bounded sidecar fault/crash retention. Append paths in `C:\Users\aaron\Desktop\NexusVision\native_orion\backend\autogreen_sidecar.py:1574-1616` need long-run retention confirmation. Missing WARNINGs are not proof a path never ran. |

## Verification ledger and snapshot identity

### Executed checks

Working directory: `C:\Users\aaron\Desktop\NexusVision`. Interpreter: `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe`. `PYTHONDONTWRITEBYTECODE=1`; pytest cache disabled and temporary data isolated under `D:\NVRT-CX0922-233756`.

The owned runner `D:\NVRT-CX0922-233756\run_checks.py` invokes the existing test suites. Its result JSON records each complete argv. The runner uses fake AWS credentials, disabled metadata lookup, mocked test dependencies, and `D:\NVRT-CX0922-233756\offline_only.py`, which rejects non-loopback `socket.connect` calls. This is a Python guard, not an OS network sandbox. Backend and staff tests ran in **separate pytest invocations**. No account queries or mutations were needed.

| Command/input | Literal terminal result | Exit | Evidence |
|---|---|---:|---|
| Interpreter + `D:\NVRT-CX0922-233756\run_checks.py backend` | `360 passed in 150.13s (0:02:30)` | 0 | `D:\NVRT-CX0922-233756\logs\backend.txt`; `D:\NVRT-CX0922-233756\backend-result.json` |
| Interpreter + `D:\NVRT-CX0922-233756\run_checks.py staff` | `17 passed in 487.00s (0:08:06)` | 0 | `D:\NVRT-CX0922-233756\logs\staff.txt`; `D:\NVRT-CX0922-233756\staff-result.json` |
| Interpreter + `D:\NVRT-CX0922-233756\run_checks.py pipeline` | `253 passed in 22.63s` | 0 | `D:\NVRT-CX0922-233756\logs\pipeline.txt`; `D:\NVRT-CX0922-233756\pipeline-result.json` |
| Interpreter + `D:\NVRT-CX0922-233756\run_checks.py security` | `214 passed in 4.31s` | 0 | `D:\NVRT-CX0922-233756\logs\security.txt`; `D:\NVRT-CX0922-233756\security-result.json` |
| Interpreter + `D:\NVRT-CX0922-233756\run_checks.py discord` | `128 passed, 1 warning in 17.92s` | 0 | `D:\NVRT-CX0922-233756\logs\discord.txt`; `D:\NVRT-CX0922-233756\discord-result.json` |
| Extra latency/wake/log contract suite below | `146 passed in 2.11s` | 0 | `D:\NVRT-CX0922-233756\logs\extra.txt` |
| Four isolated characterizations below | `4 passed in 1.09s` | 0 | `D:\NVRT-CX0922-233756\logs\characterization.txt` |
| Static website verifier below | `VERIFY_OK=single_plan_19_99_month,plain_storefront,no_synthetic_preview,no_invented_social_proof,header_account_control,inline_css_in_sync,30fps_canvas,optimized_logo,tab_favicon,stripe_live_production_checkout,discord_identity,security_headers,assets,legal,index_sha256:CC740F16198E2E21E20279CFDBACBF5878C0FC61B60B7BD33F2907FF1E7414A6` | 0 | `D:\NVRT-CX0922-233756\logs\website.txt` |
| Strict-warning package verifier below | `[verify] RESULT: PASS — integrity chain intact (577 files verified)` | 0 | `D:\NVRT-CX0922-233756\logs\package.txt`; `D:\NVRT-CX0922-233756\package-result.json` |

Existing unique tests total **1,218 passed**. The Discord warning concerns deprecated Python `audioop`, not a failed test. The four additional characterization passes mean the adverse behavior was confirmed in an isolated model; they are **not four security fixes**. Python source-contract tests are not substitutes for unexecuted native C++ tests.

Additional exact command arguments (after the interpreter, from the working directory above):

```text
-m pytest -q -p no:cacheprovider -p offline_only --basetemp D:/NVRT-CX0922-233756/tmp-extra tests/test_latency_estimator.py tests/test_latency_corroboration.py tests/test_latency_estimator_retraction.py tests/test_ps5_wake.py tests/test_async_log_sink_contract.py
-m pytest -q -s -p no:cacheprovider --basetemp D:/NVRT-CX0922-233756/tmp-characterization D:/NVRT-CX0922-233756/test_observed_contracts.py
website/tests/verify_site.py website/public/index.html
tools/verify_release_integrity.py --package release/orion-package --strict-warnings --json D:/NVRT-CX0922-233756/package-result.json
```

`D:\NVRT-CX0922-233756\test_observed_contracts.py` extracts only the reviewed helper definitions into an in-memory fake-table fixture; it does not import the live backend, contact endpoints or obtain credentials. The fourth case is calendar arithmetic tied to the reviewed fixed-day conversion. Literal outputs:

```text
OBSERVED_KILL_READ_FAILURE=(False, empty_reason)
[WARNING] AUDIT write failed action=fixture.mutation: fixture storage outage
OBSERVED_AUDIT_FAILURE=returned_id_without_persisted_row
OBSERVED_INTERRUPTED_ORDER_RETRIES=empty,empty,empty; no minted row
OBSERVED_31_DAY_PERIOD=access_expiry_2026-10-31; renewal_2026-11-01; gap_86400_seconds
```

No source fix was applied, so no before/after fix or rollback success is claimed. No native executable was loaded for these checks. Full Standard/StrictSecurity verification remains a later authorized build/integration gate, not a passed gate in this report.

### Snapshot and package identities

- Main HEAD: `0eca7d2251f92d2e94edcd769c5fc96172def5dd`; branch `fix/timing-input-and-remoteplay-blockers`.
- Fork HEAD: `c7515213abbebd2e28eb20dfb2eed9e0eb769dcb`; branch `orion`.
- Both working trees were already dirty; HEAD alone does not describe the reviewed source. `D:\NVRT-CX0922-233756\git-status-before.txt` and `D:\NVRT-CX0922-233756\source-before.json` preserve the selected baseline. A repeat comparison in `D:\NVRT-CX0922-233756\source-after.json` checked **1,036 tracked source files with zero hash changes**. This is not a claim that every untracked file or concurrently produced build/log remained unchanged.
- Customer manifest: `C:\Users\aaron\Desktop\NexusVision\release\orion-package\release_manifest.json`, SHA256 `45bc510bd0d6c284f786fb64db229365b0d3e401339f21436ce190d123e58aab`; signature SHA256 `8d3155a7e05e424fd6b380fcc5534201d4aa6f144f50d1f9b6ae44da93ca90f6` at `C:\Users\aaron\Desktop\NexusVision\release\orion-package\release_manifest.sig`.
- Package metadata reports customer version 1.0.0, generated `2026-09-22T02:18:01.784262+00:00`, required Ed25519 signature, required manifest, automation lock, and no local-development bypass. The verifier checked 577 files, with no reported errors or warnings.

| Image | SHA256 |
|---|---|
| `C:\Users\aaron\Desktop\NexusVision\release\orion-package\OrionNative.exe` | `2941cea27cd506acbe6a0f2d2cdae69e1639531ae6d38e10953c1646d43242dd` |
| `C:\Users\aaron\Desktop\NexusVision\release\orion-package\OrionUpdater.exe` | `d7cc601524d79d17fb7e815b046a35d8396ba08dde35a889033563b4f15290fa` |
| `C:\Users\aaron\Desktop\NexusVision\release\orion-package\OrionSidecar.exe` | `47fbdebe7d198d58c1b3249af9241c3f72a9b339ab6f1001537eb7d949f2f9b1` |
| `C:\Users\aaron\Desktop\NexusVision\native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe` | `0dad53db1267721b7e0db029b58f8447722e9b2f689e93405b65c0333527f149` |

Static image results are recorded in `D:\NVRT-CX0922-233756\binary-static.json`. The dev Native image contains `ORION_AUTO_SIGN_SETTINGS`, `ORION_DEV_ROOT_FALLBACK`, and `NVDEV` markers; the tested customer Native image does not. Marker absence is not exhaustive decompilation or proof that every bypass is impossible. Customer Owner/Staff executables are absent as expected; dev admin endpoint strings are not themselves secret exposure. No secrets/private keys were reported from the reviewed package scan; this audit did not perform or claim an exhaustive secret scan of every historic log.

### Concurrent report reconciliation

After this independent source review, a same-team Codex draft appeared at the requested report path. Its exact bytes were preserved at `D:\NVRT-CX0922-233756\codex-report-concurrent-draft.md`, SHA256 `b8dbe6e33a83278abb855ac2ec68537cd88f700c8d64da1490c37fa590ffd794`. No Claude/Gemini report was read. Findings from the earlier Codex draft were checked against source rather than accepted by attribution; its other test/scan counts are not included in this report's verification totals.

Stable IDs are retained where a source-supported finding remains. CX-003 is price-copy mismatch, not a proven different charge; CX-006 is local checksum bootstrapping, not a signed-release or entitlement bypass; CX-009 no longer assumes that current topology code hardcodes SMT pairs. **CX-005, CX-010 and CX-011 are not retained as findings**: lost-release/phantom-R2 causation is unproven, consuming a pairing attempt can be an intentional one-use fail-closed design, and heuristic machine identity alone does not demonstrate a bypass of the current server-side one-machine claim. Their unanswered validation questions remain in the coverage matrix. ID gaps are intentional, preventing silent reuse of withdrawn claims.

## Executive summary

- Beta is blocked: passing packaging and Python checks did not close source-level release blockers.
- Nineteen findings remain: 2 CATASTROPHIC, 4 CRITICAL, 5 HIGH, 5 MEDIUM, 3 LOW.
- Profile recovery is missing for invalid existing learning; current atomic writes do not repair old corrupt state.
- Refund/dispute revocation handling is absent under the required paid-entitlement policy; live compensating controls remain unverified.
- A local input-delivery timeout can stop the human session; the incident executable has not changed.
- Current fork source already fixes optional-repeat spacing and terminal trigger redundancy; deployment is not proven.
- Kill-state read failure becomes permissive, destructive audit failure is swallowed, and failed log I/O can indefinitely block the launcher.
- Fixed-day monthly entitlement and unrecoverable order claims can interrupt paid access.
- Learned-value validation, optional DLL trust, and support-log privacy also need closure.
- The exact phantom-R2 cause, a wrong charged amount, remote code execution, and unsigned-update acceptance were not demonstrated.
- Topology code is already topology-aware; a generic SMT-assumption allegation is not a current finding.
- 1,218 existing tests, four characterizations, static site verification, and the 577-file package check passed within their stated bounds.
- No source/config/package edits, builds, application launches, deployments, or customer-account mutations were made.

## Findings table

| ID | Severity | Surface | One-line finding |
|---|---|---|---|
| CX-001 | CATASTROPHIC | S1 | Invalid existing learning has no last-good recovery and can be replaced silently. |
| CX-002 | CATASTROPHIC | S6, S10 | Refund/dispute handling does not revoke the affected paid entitlement. |
| CX-004 | CRITICAL | A, D, E, S4 | A required local-delivery timeout stops the human input session. |
| CX-013 | CRITICAL | S7, S9 | Kill-switch read error returns permission to continue. |
| CX-014 | CRITICAL | S9, S14 | Destructive mutation can succeed without a durable audit row. |
| CX-015 | CRITICAL | A, D, S16 | Persistent log I/O failure can block GUI/input relay and shutdown. |
| CX-008 | HIGH | S1, S2 | Legacy learned timing fields bypass validation. |
| CX-016 | HIGH | S6, S10 | Calendar-month payments are converted to fixed 30-day access. |
| CX-017 | HIGH | S6, S9 | Interrupted order claims can remain permanently pending. |
| CX-018 | HIGH | S5, S15 | Optional runtime DLL fallbacks are outside the pre-load trust gate. |
| CX-019 | HIGH | S16 | Support diagnostics expose raw console/network identifiers. |
| CX-003 | MEDIUM | S6, S10 | Dynamic account-page price copy differs from the approved storefront. |
| CX-006 | MEDIUM | S1 | Missing settings checksum is bootstrapped despite existing settings. |
| CX-007 | MEDIUM | S2 | Calibration Cancel preserves partial changes despite its label. |
| CX-020 | MEDIUM | S2 | Calibration can claim an initial candidate was saved without applying it. |
| CX-022 | MEDIUM | S14 | Dismissing a one-time secret leaves it available in raw response data. |
| CX-009 | LOW | D, S4 | Affinity failures are discarded; hybrid placement is unqualified. |
| CX-012 | LOW | S8, S15 | Updater namespace checks and final writes retain a race interval. |
| CX-021 | LOW | S6, S11 | One Discord Worker response still advertises a three-day trial. |

## Release verdict for the beta

**blocked**

Exact blocking findings: **CX-001, CX-002, CX-004, CX-008, CX-013, CX-014, CX-015, CX-016, CX-017, CX-018, CX-019**.

Close those root causes with the finding-specific regression evidence (or a verified compensating control where explicitly identified), then validate one coherent, hash-identified deployment unit. Require current StrictSecurity, native input/recovery and persistence tests, clean-machine installation/rollback, and deployed billing/auth/admin configuration evidence before release approval. Medium/Low findings remain in the release disposition list; their presence must not be used to distract from the hard blockers. No fix, live recovery, or production security approval is asserted by this read-only report.
