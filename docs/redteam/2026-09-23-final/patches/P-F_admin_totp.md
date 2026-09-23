# P-F: owner/staff console TOTP step-up (client half of RT-CRIT-01 / CSEC-12), 2026-09-23

This is the client side of P-A (`P-A_backend.md`, section "client changes"). Once P-A is deployed, the backend requires:
- a **fresh, single-use owner TOTP code** in `X-Orion-Admin-TOTP` (or body `step_up_code`) for owner-sensitive actions;
- a body `totp_code` (or the same header) at an **owner-role** staff login while owner TOTP is required.

This patch makes OrionOwner, OrionStaff and the `orion-admin` CLI send the code exactly when it is needed. The code is never stored, logged or printed.

Constraints held for this patch: no native build, no live calls, no deploys, no commits, no secrets printed.

## Files

| File | Change |
|---|---|
| `native_orion/src/AdminToolController.h` | New `orion::admin_step_up` pure policy (inline, QString/QVariant only): which actions need step-up, when to prompt, code sanitising, plain error messages. New properties `stepUpPending`, `stepUpPrompt`, `stepUpError` and `staffLoginNeedsCode`. New invokables `submitStepUp(code)` and `cancelStepUp()`. `staffLogin(staffId, ownerCode = "")`. `applyAuth` and `sendRequest` take a per-request `stepUpCode`. The parked-call struct `StepUpCall` holds the request, never the code. |
| `native_orion/src/AdminToolController.cpp` | See "Behaviour" below. |
| `native_orion/qml/admin/AdminMain.qml` | One modal "OWNER CODE" `Popup`, driven by `admin.stepUpPending`. The field is wiped on submit, open and close. Closing the popup cancels the parked action. |
| `native_orion/qml/admin/AdminLoginPane.qml` | Staff build only: an optional "Owner code (owner accounts only)" field, passed to `staffLogin` and wiped immediately. The label and hint switch to "required" after a code refusal. The break-glass owner form is unchanged. |
| `tools/admin/orion_admin.py` | `build_auth_headers(config, step_up_code="")`. Step-up routing in `OrionAdminClient.server_config_update()` / `staff()`. `staff-login --owner`. Plain messages for step-up errors. A `code_prompter` (getpass on a tty). |
| `tests/test_admin_owner_step_up.py` (new) | 73 tests: CLI behaviour plus native/QML source contract. |
| `native_orion/tests/AdminStepUpPolicyTests.cpp` (new) | QtTest over `orion::admin_step_up`. **Not registered in CMake yet** (CMakeLists is not in this lane; see below). |

Pre-patch copies of the edited files are in this agent's scratchpad. All six files were clean against HEAD `ea8729b` before the edit.

## Behaviour

### Actions that need step-up

These mirror the backend's `owner_step_up()` callers:
- `/api/admin/config`:
  - actions `totp_enroll`, `totp_confirm`, `totp_disable`, `rotate_admin_secret`;
  - `set` of `owner_totp_required`, `owner_ip_allowlist` or `alerts`;
  - **releasing** `global_kill` (bool or `{enabled:false}`). Engaging the kill stays one step.
- `/api/admin/staff`:
  - `create` with role owner;
  - any action on a row whose role is owner. For an unknown row the client prompts, since the server would demand a code anyway;
  - `set_role` to owner.

Everything else (licence actions, kill engage, metrics, audit, config reads, support/admin staff rows) sends **no** code and **no** prompt.

### Native (OrionOwner / OrionStaff)

- **Parking the action.** A sensitive action goes through `sendOwnerAction()`. When a code is needed, the request (tag, path, body, context) is parked, `stepUpChanged` fires, and QML opens the popup. `submitStepUp(code)`:
  - digits only, exactly 6;
  - sends the parked request once, with the code in `X-Orion-Admin-TOTP`;
  - clears the parked action;
  - zero-fills and clears its local copy.

  The code never goes into a body, `pending.context` (which gets only a boolean `step_up` flag), a property, or `admin_tool.log`. The diagnostics log only `step_up_prompted` and `step_up_submitted` with the tag.
- **When to prompt** (`shouldPromptForCode`):
  - **Break-glass:** only when owner TOTP is required. The admin secret is itself the possession factor, and the backend lets break-glass act while TOTP is off.
  - **Owner-role bearer:** always, unless the loaded config positively says `owner_totp_required=false`. In that case the server returns `step_up_required` and the console shows the plain "turn TOTP on from break-glass" message.
- **Headers** (`applyAuth`):
  - Break-glass is unchanged: the secret, plus the session TOTP on every owner request. On a step-up request the fresh code replaces the session code in the same header.
  - An owner-role bearer sends `X-Orion-Admin-TOTP` **only** on a step-up request.
  - `AuthKind::Staff` (staff routes) never sends it.
- **Refusals.** `invalid_totp` / `totp_replayed` on a step-up request or a staff login show **"That code was already used or is wrong — wait for the next code."**. For step-up, the popup re-opens for the same action with that message. `totp_required`, `step_up_required`, `step_up_unavailable`, `totp_not_provisioned`, `config_unavailable` and `audit_unavailable` also get plain messages. A step-up refusal never ends the session.
- **Staff login.** `staffLogin(id, ownerCode)` adds body `totp_code` only when a code was typed; a malformed code is refused locally. The request's `pending.body` copy has `totp_code` stripped, so the code does not live for the length of the reply.
- **Session end.** `logout()` and `resetSessionData()` drop any parked action.
- **Side fix.** `/api/admin/whoami` answers `owner_totp_required`, but the controller read `totp_required`, so the break-glass "TOTP required" state was never learned from whoami. It now reads both. The break-glass prompt decision depends on this.

### CLI (`tools/admin/orion_admin.py`)

- **Owner-role bearer** (staff token, no admin secret): a sensitive request takes the code from `ORION_ADMIN_TOTP` (for that one command) or asks for it with hidden input.
  - The code goes in `X-Orion-Admin-TOTP` for that one request. It is not written to the config, not printed, and not reused: each sensitive request asks again.
  - Staff actions first `GET /api/admin/staff` to learn the target row's role.
  - With no code source, the CLI refuses **before** sending.
- **Break-glass** is unchanged: `build_auth_headers` output is identical, the existing per-invocation TOTP prompt in `main()` stays, and there is no new prompting.
- **`staff-login --owner`** asks for a code and sends body `totp_code`. Without the flag nothing is sent. A `totp_required` refusal then tells the owner to re-run with `--owner`. This avoids a guaranteed-denied attempt, which would write a `staff.login_denied` audit row.
- **`_unwrap`** maps the step-up error codes to the same plain messages.

## Tests

- **Python (run):**
  `.venv\Scripts\python.exe -B -m pytest tests/test_admin_owner_step_up.py tests/test_orion_admin.py tests/test_security_audit.py -q -p no:cacheprovider --basetemp C:/Users/aaron/AppData/Local/Temp/pf-tests`
  - **173 passed.** Of those, `test_orion_admin.py` accounts for 76 (76 before the patch as well, so no regressions) and the new file for 73.
  - The new file covers:
    - backend-parity guards: CLI and C++ `STEP_UP_CONFIG_KEYS` are parsed against `backend/lambda_function.py`;
    - the policy tables;
    - the header being sent exactly on sensitive bearer requests (8 sensitive and 8 ordinary commands), and never on staff routes or support roles;
    - one prompt per request with no reuse; the env code used for step-up only;
    - refusal before sending when there is no code or it is malformed;
    - plain messages for replayed/wrong and the other step-up codes;
    - break-glass headers unchanged;
    - `staff-login` with and without `--owner`, including the code never appearing in the saved config or output;
    - the code never appearing in any request body or output line;
    - native source contract: `applyAuth` branches, all six sensitive methods routed through `sendOwnerAction`, no code in any `appendAdminDiagnostic` call or `StepUpCall`, `totp_code` stripped from `pending.body`;
    - QML: the field is cleared before submit, on open and on close; the break-glass form is unchanged.
- **Native (not built here):** `native_orion/tests/AdminStepUpPolicyTests.cpp` covers the config/staff rules, the kill direction, the prompt policy table, code sanitising, plain messages and the retryable set.

## Native targets to build (central build)

1. `OrionOwner` and `OrionStaff`. Both compile `AdminToolController.cpp` and the `qml/admin` module (QML changed: `AdminMain.qml`, `AdminLoginPane.qml`).
2. The new QtTest target. Add it to `native_orion/CMakeLists.txt` inside `if(ORION_BUILD_TESTS)`, next to `OrionLicenseHeartbeatPolicyTests`:

```cmake
    # [rc1 RT-CRIT-01 P-F] owner step-up policy for OrionOwner/OrionStaff.
    add_executable(OrionAdminStepUpPolicyTests
        tests/AdminStepUpPolicyTests.cpp
    )
    orion_apply_target_defaults(OrionAdminStepUpPolicyTests)
    # AdminToolController.h has an inline appVersion() that expands this macro.
    target_compile_definitions(OrionAdminStepUpPolicyTests PRIVATE ORION_NATIVE_VERSION="${PROJECT_VERSION}")
    target_link_libraries(OrionAdminStepUpPolicyTests PRIVATE
        OrionCommon
        SecurityCore
        Qt6::Core
        Qt6::Network
        Qt6::Test
    )
    orion_add_qtest(OrionAdminStepUpPolicyTests)
```

Then run `ctest -R OrionAdminStepUpPolicyTests`. This is a StrictSecurity-scope change (staff/admin, secret handling), so StrictSecurity must pass on the rc2 unit.

## Residual / notes for P-A and the owner

- **Re-enrol while TOTP is required (backend).** `totp_enroll` overwrites the SSM secret but leaves `owner_totp_required=true`. From that point:
  - every break-glass request needs a code from the **new** secret, so the cached session code dies;
  - `totp_confirm` needs two codes from the new secret in **different** 30 s steps: the step-up is burned first, then the confirm code, and the same step gives `totp_replayed`. The console's prompt says so.

  Suggestion for P-A: in `totp_confirm`, reuse the step-up counter when it equals `confirm_counter`, or skip the step-up when the body code already verifies against the pending secret.
- **Break-glass edge.** Break-glass with TOTP required but not yet known to the console (config and whoami not loaded) sends the cached session code as the step-up code. If that code is stale, the server refuses with `invalid_totp`, the console marks TOTP required, and the next click prompts. There is no silent success with a reused code, because the server burns each step.
- **Bearer with TOTP off.** An owner-role bearer can never do step-up actions while owner TOTP is off. That is by design in P-A: enrol via break-glass first. The console prompts unless the config is loaded; the refusal is the plain `step_up_required` message.
- **Memory.** Qt keeps its own copy of the header inside the in-flight `QNetworkRequest`/`QNetworkReply` until the reply is deleted (`deleteLater` on finish). The controller's copies are zero-filled and cleared right after the send, but `QString` gives no guarantee of scrubbing. The code is single-use server-side, so a leftover copy is a spent code.
- **CLI.** `ORION_ADMIN_TOTP` stays environment-only (it is never saved). A script that reuses it across two sensitive commands gets `totp_replayed` on the second, which is correct.
