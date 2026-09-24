# P-H patch: remember the sign-in (2026-09-23)

Problem (from the installed app's log): the production build never remembered a sign-in. The
canonical licence key lived only in `OrionAppController::authLicenseKey_`;
`cacheLocalEntitlement()` stored user/plan/token metadata but not the key; the only auto-auth
paths (`ORION_AUTO_UNLOCK_LOCAL`, `ORION_LICENSE_KEY`) are compiled out of production. Every
launch showed AuthGate, and a keyless customer (PAIR- one-time code from zaeorion.com/connect)
needed a new one-time code every launch.

Nothing was built, launched, deployed, committed or sent to a live endpoint. The owner's
settings / `.vault` files were not touched.

## BLOCKER: the backend refuses the stored key for most customers today

Reading `backend/lambda_function.py::handle_activate` (not a live call):

```python
if item.get("oauth_pair_required") and not paired:
    return err("discord_signin_required", 403, ...)
```

`oauth_pair_required = True` is set on **every new trial** (`handle_bot_trial`, ~L2410) and on
**every Stripe key, new or renewed** (`_handle_bot_provision`, ~L3049 / ~L3096). The client can only
re-submit the canonical key through `/api/license/redeem`, which then answers
`discord_signin_required` for exactly the keyless customers this patch is for. With the client
patch alone those customers see a short "Signing you in…" and are then sent back to Connect
Discord, which is what already happens today. Legacy (Gumroad / non-Stripe / older trial) keys
work at once.

`/api/license/check` (the 5-minute heartbeat) already accepts canonical key + the bound
`machine_id` with **no** pairing requirement and returns the signed lease. So allowing activation
of an `oauth_pair_required` key **on the machine it is already bound to** grants nothing the
heartbeat does not already grant every 5 minutes. Proposed backend fix (NOT applied; owner +
Codex decide; needs a Lambda deploy):

```python
# handle_activate, replacing the unconditional oauth_pair_required refusal.
# Pairing stays mandatory to BIND a machine (fresh key, after /hwid_reset, second PC).
# Re-activating the machine this key is already bound to is what /api/license/check
# already authorises every 5 minutes.
bound_machine = item.get("machine_id", "")
if item.get("oauth_pair_required") and not paired and not (bound_machine and bound_machine == machine_id):
    audit_log("activate_discord_signin_required", license_key)
    return err("discord_signin_required", 403, message=...)
```

Tests to add with it (tests/backend/test_pairing.py pattern): paired bind then unpaired re-activate
on the same machine -> 200; unpaired activate on a different machine -> `discord_signin_required`;
unpaired activate of a never-bound key -> `discord_signin_required`; after hwid reset (machine_id
cleared) -> `discord_signin_required`; revoked/expired/subscription checks still run first.
Note the deployed bundle `discord_launch/deployed_bundle/orion-activate/lambda_function.py` has no
`oauth_pair_required` at all, so check which revision is live before reasoning about behaviour.

Alternative that needs no backend change: resume through `/api/license/check` (heartbeat) instead
of `/api/license/redeem`. It works today but is a NEW unlock path (no activation nonce/timestamp,
no activation token), so it needs its own Codex review. Not implemented.

## Design

1. **Store on success.** In the `activationFinished` ok branch, after the PAIR- -> canonical
   replacement and after `authenticated_ = true`, `SecurityManager::storeRememberedLicenseKey()`
   persists `authLicenseKey_`. Only the canonical shape `^[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$` is accepted
   (`isRememberableLicenseKey`), exact match, no trimming/uppercasing, so a PAIR- code or an NVDEV-
   dev key can never be stored. If a successful sign-in cannot be remembered (non-canonical legacy
   key, DPAPI failure) any OLDER remembered key is deleted: the newest sign-in wins.
2. **Storage.** `<orionDataDir>/.vault/venice_signin.dat` (next to `orion_entitlement.cache`;
   `.vault` is already excluded by `release_filter_policy.py`). Outer JSON
   `{schema: venice.remembered_signin_file.v1, blob_b64}`; the key, the binding
   `{machine_id, user}` and `stored_at` are inside the DPAPI blob. DPAPI CURRENT_USER scope
   (`CryptProtectData` without `CRYPTPROTECT_LOCAL_MACHINE`), `CRYPTPROTECT_UI_FORBIDDEN`,
   entropy `"venice-remembered-signin-v1|" + machineId()` (distinct from the entitlement cache's
   `"orion-entitlement-v1|"`). **No plaintext fallback**: without DPAPI nothing is remembered.
   Deliberately not bound to the build hash, so an update does not sign the customer out.
   Written with `QSaveFile` (atomic). The DPAPI call is factored into `dpapiProtect()` /
   `dpapiUnprotect()`; `protectBytes()/unprotectBytes()` (entitlement cache) now call the same
   helpers with their unchanged entropy, so there is one `CryptProtectData` call site.
   `dpapiUnprotect` zeroes DPAPI's plaintext buffer before `LocalFree`.
3. **Load.** `loadRememberedLicenseKey()` returns the key only after: size <= 64 KiB, outer schema,
   DPAPI decrypt with the sign-in entropy, payload schema, binding machine_id == `machineId()` and
   user == `currentUserBinding()`, key is canonical. **Any** failure deletes the file and reports
   `RememberedKeyLoad::Cleared` (never crashes, never unlocks). The detail string carries the key
   suffix only.
4. **Startup.** After the dev hooks (`#endif`), `if (!authenticated_ && !authBusy_)
   beginRememberedSignIn("launch")`. It reads the file (fresh every attempt), sets
   `rememberedSignInAttempt_`, calls the existing `authenticate(key)` and, while the request is in
   flight, shows "Signing you in…" (AuthGate busy state, Unlock disabled as "Verifying…"). It never
   sets `authenticated_`, `currentPage_`, the lease or the entitlement cache itself; the existing
   activation handler does that only on a server `ok`. Fire stays lease-gated exactly as before.
   Dev hooks still win in dev builds (they set `authenticated_` / `authBusy_` first), so
   `run_orion.local.ps1` is unaffected.
5. **Verdicts** (`isRememberedSignInRefusalCode`, `LicenseClient.cpp`, exact code match; retry
   decision `rememberedSignInAfterFailure`, `LicenseHeartbeatPolicy.h`):
   - Delete the stored key + AuthGate with the existing plain-language reason:
     `invalid_key license_revoked revoked license_expired expired license_invalid_status inactive
     frozen blacklisted device_mismatch device_limit_reached subscription_required
     discord_signin_required trial_used pair_invalid` (both endpoints' spellings; activation says
     `license_revoked`, the heartbeat says `revoked`). The heartbeat kill branch also deletes the
     store for these codes.
   - Keep the key and retry on the heartbeat ladder (15 s / 30 s / 60 s, then 5 min; `rate_limited`
     jumps to 60 s), or the customer presses Unlock with an empty field
     (`resumeRememberedSignIn()`): transport failure (empty code), `internal_error`,
     `entitlement_unavailable`, `kill_state_unavailable`, `config_unavailable`, `rate_limited`,
     `timestamp_expired`, `replay_*`, `license_record_invalid`, unknown codes, **`service_disabled`**
     and **`version_blocked`**.
   - **Deviation from the brief, for Codex/owner:** the brief listed `service_disabled` as
     definitive. It is kept instead: it is the owner-wide pause (global kill switch), not a verdict
     on the key, and deleting on it would force every keyless customer back through Discord when
     the pause ends. The server still refuses during the pause, so nothing unlocks. Likewise
     `version_blocked` (update, then the same key works). Flipping either is one line in
     `isRememberedSignInRefusalCode`.
   - Only the activation that was STARTED from the store can delete it (`std::exchange` of
     `rememberedSignInAttempt_` in the handler): a mistyped code on AuthGate never erases the
     remembered sign-in.
6. **Sign out.** `Q_INVOKABLE signOut()`: the same teardown as a server kill (heartbeat stopped,
   stream disconnected, `authenticated_ = false`, `leaseGate_.recordHeartbeatKill()`, token and
   in-memory key cleared), then deletes the remembered key and the local entitlement cache and
   resets the profile. Refused while an activation is in flight (button disabled while busy). UI:
   "Sign out" at the bottom of the account flyout (Sidebar, `licenseFlyoutSignOut`), and on AuthGate
   next to "Venice remembers this PC. Press Unlock to sign in again." (`authRememberedRow`,
   `authForgetPc`) so a shared-PC user can forget the PC without signing in. Copy: "Signed out. To
   sign in again, get a one-time code from zaeorion.com/connect and press Unlock."; Activity line
   "Signed out of Venice on this PC.". AuthGate's hint picker returns no second line for "Signed
   out" / "Signing you in".

## Files

- `native_orion/src/SecurityManager.{h,cpp}`: `RememberedKeyLoad`, `storeRememberedLicenseKey`,
  `loadRememberedLicenseKey`, `clearRememberedLicenseKey`, `hasRememberedLicenseKey`, private
  `rememberedLicenseKeyPath` / `rememberedSignInEntropy`, free `isRememberableLicenseKey`;
  `dpapiProtect`/`dpapiUnprotect` helpers; `protectBytes`/`unprotectBytes` refactored onto them.
- `native_orion/src/LicenseClient.{h,cpp}`: `isRememberedSignInRefusalCode`.
- `native_orion/src/LicenseHeartbeatPolicy.h`: `RememberedSignInDecision`,
  `rememberedSignInAfterFailure`.
- `native_orion/src/OrionAppController.{h,cpp}`: property `rememberedSignInAvailable`,
  `resumeRememberedSignIn()`, `signOut()`, private `beginRememberedSignIn()`,
  `forgetRememberedSignIn()`, retry timer + backoff; store in the activation ok branch; keep/clear
  in the failure branch; clear in the heartbeat kill branch; startup resume; `#include <utility>`.
- `native_orion/qml/pages/AuthGate.qml`, `native_orion/qml/components/Sidebar.qml`.
- `native_orion/tests/RememberedSignInTests.cpp` (new), `native_orion/CMakeLists.txt`
  (target + ctest PATH list), `scripts/verify_orion.ps1` (both `--target` lists).
- `tests/test_remember_signin_ph_20260923.py` (new).

## Tests

Python (run, 19 new, all pass; neighbours `test_copy_fixes_20260923`, `test_customer_states_pe_20260923`,
`test_native_qtest_diagnostics`, the QML/contract/release-filter suites and a
`-k "license or security or auth or signin or qml or contract or copy or strict"` sweep: 345 + 205 pass):
store is DPAPI-only / canonical-only / not build-bound; entropy + path; load deletes every bad
record and returns the key only after all checks; entitlement cache entropy unchanged and one
`CryptProtectData` site; refusal classifier covers both endpoints and keeps the owner-wide gates;
**every 403 code `handle_activate` can return is classified** (a new backend code fails the test
until someone decides); store only after the server ok and after the PAIR exchange; clear only on
a refusal of the remembered attempt; heartbeat kill clears for key verdicts; startup resume runs
after and outside the dev hooks; resume goes through `authenticate()` and never sets
`authenticated_`/lease/page; sign-out teardown; no log line prints a full key; Sidebar + AuthGate
wiring and copy; AuthGate hint for the new messages (node); target registered in CMake and both
verifier lists.

Native `OrionRememberedSignInTests` (written, NOT built): canonical-shape table; store -> load
round trip (file never contains the key text, fresh manager reads it); store replaces; PAIR- /
NVDEV- / lowercase / padded / empty never stored and a refused store keeps the good record;
missing file -> None; corrupt files (empty, garbage, array, wrong schema, empty blob, undecryptable
blob, plaintext key, oversized) -> Cleared + deleted; tampered ciphertext -> deleted; entitlement
entropy cannot open a sign-in record; foreign machine id / foreign Windows user / PAIR- or
lowercase key inside a valid DPAPI record -> deleted; sign-out clears (idempotent); definitive
refusal codes -> clear decision and file deleted; transient codes -> kept, retry 15/30/60/300/300 s;
`rate_limited` -> 60 s; key-level kill codes are refusals, `service_disabled`/`version_blocked`
are not; no key text in error/detail strings, `securityEvent`s or Qt messages. File tests QSKIP
under `ORION_PRODUCTION_BUILD` (orionDataDir would be the real AppLocalData) via a macro (QSKIP
must be in the slot itself). Fail-first: none of these APIs exist at HEAD.

## Native targets Claude must build and run

- Full `OrionNative` (dev AND production): `SecurityManager.{h,cpp}`, `LicenseClient.{h,cpp}`,
  `LicenseHeartbeatPolicy.h`, `OrionAppController.{h,cpp}`.
- **New** `OrionRememberedSignInTests`.
- `OrionLicenseHeartbeatPolicyTests`, `OrionSettingsSignatureTransactionTests`,
  `OrionMachineIdParityTests`, `OrionActivateContractTests`, `OrionNativeTests` (SecurityCore and
  the controller changed), `OrionOwner` / `OrionStaff` (link SecurityCore).
- StrictSecurity pass (licence/auth + secret handling changed).
- QML smoke: AuthGate (empty-field Unlock with a remembered sign-in, the remembered row, busy
  "Signing you in…"), Sidebar flyout Sign out. Manual e2e on a throwaway Windows profile: sign in
  -> restart -> signs in without a code (legacy key, or Stripe/trial only after the backend fix);
  pull the network -> restart -> key kept, retry line in the log; Sign out -> restart -> AuthGate.

## Security notes for Codex re-review

- Authority unchanged: the stored key is only re-submitted to `/api/license/redeem`; device
  binding, revocation, kill switch, subscription and version gate are server-side. No code path
  sets `authenticated_`, the lease or the page from the file. Lease-gated fire unchanged.
- At-rest: DPAPI CURRENT_USER + per-machine entropy; binding re-checked after decrypt; any
  mismatch deletes. A same-user process can still call `CryptUnprotectData` and read the key: that
  is DPAPI's model and the same exposure as the existing entitlement cache / the typed key in
  memory. The key alone does not bind a new machine (device_mismatch / pairing).
- A copied `venice_signin.dat` does not open on another PC or Windows account (DPAPI user key +
  entropy with machineId + binding check).
- Logs: suffix only (`key_suffix=%1` with `.right(4)`), server codes, reasons. No full key in
  details, errors, security events or the Activity feed (tests pin it).
- Kept on `service_disabled` / `version_blocked` / transient: see section 5; server still refuses.
- Dev: an `ORION_LICENSE_KEY` activation now also writes `.vault/venice_signin.dat` in the checkout
  (gitignored, release-filtered). Env/auto-unlock hooks still take precedence.
- Open: the backend blocker above; sign-out does not tell the server (no logout endpoint; the
  token simply expires). Blue-team: no audit event is added client-side; server audit already
  records each activation.
