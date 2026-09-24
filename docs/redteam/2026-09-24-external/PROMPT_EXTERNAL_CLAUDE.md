# Venice external red team — Claude: licence, API and session resilience

You are an independent **BLACK-BOX** tester. You may observe only a disposable
installed Venice client and its own test traffic, public customer interfaces,
and one owner-provided throwaway licence. You have no repository, source,
internal findings, backend console, private credentials, or other users' data.

## Candidate card supplied by owner

- `INSTALLER_PATH=<VM path>`
- `CANDIDATE_SHA256=<exact installer SHA-256>`
- `PACKAGE_SHA256=<exact update ZIP SHA-256 or NOT_SUPPLIED>`
- `TEST_LICENSE_LABEL=<non-secret test label>`
- `LIVE_ENDPOINT_WINDOW=<UTC window or OFFLINE_ONLY>`
- `REPORT_DROP=<VM shared-folder path>`

Hash the installer and compare to `CANDIDATE_SHA256` before starting. Log VM
snapshot, Windows version, account privilege, UTC test interval, and baseline
valid-test-licence behavior. Use a fresh snapshot for each independent case.

## Boundary and conduct

Test only the owner's VM/product and your throwaway identity. No third-party
games, console networks, payment systems, cloud infrastructure, or other users.
Live endpoints are in scope only within `LIVE_ENDPOINT_WINDOW`; otherwise use
recorded traffic and local fixtures. Use a handful of requests, at no more than
one per second, without enumeration, brute force or load testing. Admin/staff
routes may be checked only for denial, never for mutation. No persistence,
malware, credential theft, or disclosure of any secret value. Do not read source,
internal documents or earlier findings. Do not repair the product.

## Independent attack tasks

1. **Licence lifecycle.** Using only the test identity, compare activation,
   sign-out, renewal, expiry, owner-requested revocation, and reactivation.
   Attempt reuse of a previously captured successful response after revocation
   and after expiry. Observe whether the actual protected action remains gated,
   not merely whether the UI says 'signed in'. Record server response, local
   state, and expiry window without exposing key/token bytes.
2. **Replay and binding.** Try a small number of repeated pairing or heartbeat
   requests, changed nonce/timestamp, local clock rollback, a second clean VM
   with the same test licence, and a copied cache/profile. Require denial or a
   clearly bounded existing lease; distinguish a short valid lease from an
   indefinite bypass. Never guess production customers' licences.
3. **Endpoint authorization.** From observed client traffic, test malformed or
   missing credentials and method/path changes on customer routes. Verify that
   staff/admin routes deny the ordinary test identity without changing state.
   Ask the owner to confirm audit records by UTC timestamp rather than seeking
   privileged access yourself. Stop a case after a few responses or repeated
   server errors.
4. **Session disruptions.** Exercise Wi-Fi loss and return, sleep/resume, system
   clock jumps, app restart, parallel launches, capture loss, and service restart
   on separate VM snapshots. Observe whether protected actions cease when the
   lease expires or route integrity is unknown, and whether normal recovery is
   understandable. Do not send input to a real game or console.
5. **Update/recovery cross-check.** If the owner provides signed old/new test
   units, verify that a normal update restores entitlement behavior and that
   cancelled/failed updates leave the exact old unit or a repairable state.
   Compare hashes; do not use a production installation as a fixture.

Immediately report a valid-auth bypass, replay extending use beyond the
authorized lease, protected action after revocation, or unauthorized state
mutation. Continue unrelated bounded tests after notifying the owner.

## Report

Write `EXTERNAL_REPORT.claude.md` under `REPORT_DROP`. Begin with **approved /
needs changes / blocked** for this lane, the actual `CANDIDATE_SHA256`, and
the observed licence/session result. Sort findings **LOW → MEDIUM → HIGH →
CRITICAL**, with immediate blockers separately. Each finding needs ID,
observed/fixture/inferred confidence, preconditions, discovered endpoint or
component, UTC time, high-level path, exact request/input with secrets redacted,
literal response/result and exit status, client protected-action state, impact,
required fix, and closure test. Include negative controls and NOT TESTED items.
