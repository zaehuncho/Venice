# STATUS.md - Claude + Codex Loop Log

Append-only round history lives below. The loop reads only the token block under
`## Current Status`.

## Current Status

```
ROUND: 42
PHASE: owner-staff-auth-lock-ui-polish
DECISION: needs-live-owner-staff-flow-validation
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Round 42 (Codex, Owner/Staff auth-lock UI polish, NOT committed):**
  Reworked the privileged tool UI to match the launcher auth-gate pattern and
  keep operational controls hidden until authentication.
  - **UI change:** `native_orion/qml/admin/AdminMain.qml` now uses a
    launcher-style frameless shell/title bar, centered auth card, mode-specific
    Owner/Staff branding, and a post-auth dashboard loaded only after
    `admin.authenticated`. The old always-visible dashboard with disabled
    controls is gone.
  - **Auth lock:** unauthenticated Owner shows only the owner-secret unlock
    card; unauthenticated Staff shows only Discord ID + enrollment/login. The
    dashboard panels do not mount until the backend auth succeeds.
  - **Admin security cleanup:** packaged admin tools no longer block on the
    launcher's gameplay `settings.json` signature check because Owner/Staff do
    not consume gameplay settings. Release-manifest integrity, debugger/analysis
    locks, backend auth, server role checks, and machine-bound staff tokens still
    fail closed.
  - **Visual evidence:** packaged screenshots saved to
    `logs/diagnostics/ui/orion_owner_auth_lock.png` and
    `logs/diagnostics/ui/orion_staff_auth_lock.png`. Both show correct
    mode branding, clean centered auth-lock cards, release-manifest verified
    status, and no clipping at `1120 x 720`.
  - **Verification GREEN:** dev and production `OrionOwner`/`OrionStaff`
    targets built; package refreshed with `1310` manifest files; package audit
    OK; `tools/security/pack_orion_release.py --verify-only` OK; live backend
    contract checker OK; full `scripts/verify_orion.ps1 -StrictSecurity` OK
    (`162 passed, 1 xfailed`, dev ctest `2/2`, production ctest `2/2`,
    package/security/startup/tamper smokes all OK).
  - **Remaining production blockers:** real Owner login, Staff
    enrollment/login, role-limited staff action tests, staff disable/revoke/HWID
    reset enforcement, authenticated tamper-report audit validation, and a real
    packer run with packed smoke login/API tests.

- **Round 41 (Codex, live Owner/Staff backend contract verified, NOT committed):**
  Browser Claude deployed the missing live AWS routes and Codex verified the
  public contract from the local repo.
  - **Live contract fixed:** `tools/admin/check_backend_contract.py` now passes
    against `https://api.zaeorion.com`: `/api/version` reports `0.4.0`,
    staff identity/login routes exist and fail closed, owner staff-management
    exists and fails closed, and both owner/staff tamper-report routes exist and
    fail closed.
  - **Checker alignment:** the deployed staff auth routes return
    `401 missing_token` when no bearer token is supplied. That is a valid
    fail-closed auth response, so the local checker now accepts `missing_token`
    alongside the older staff-auth error names.
  - **Verification GREEN:** `python tools/admin/check_backend_contract.py` ->
    backend owner/staff route contract deployed; `pytest
    tests/test_backend_contract_check.py -q` -> `2 passed`; Python compile for
    `tools/admin/check_backend_contract.py` OK.
  - **Remaining production blockers:** run real Owner enrollment/login/action
    flows, real Staff enrollment/login/action flows, staff disable/revoke/HWID
    reset role-enforcement tests, authenticated tamper-report audit validation,
    and a real packer run followed by packed smoke login/API tests. Browser
    Claude's handoff also notes `/orion/staff_token_secret` and
    `/orion/staff_enroll_key_hash` must be seeded before first staff
    enrollment if they are not already present.

- **Round 40 (Codex, authenticated tamper-report path, NOT committed):**
  Closed the last local client-side gap in the Owner/Staff security plan:
  authenticated tools now best-effort report a security-lock/tamper condition to
  the backend audit path without ever unlocking or bypassing the fail-closed
  startup/integrity checks.
  - **Backend local contract added:** `backend/lambda_function.py` now includes
    `/api/admin/tamper-report` and `/api/staff/tamper-report`. Owner reports
    require the admin secret; staff reports require a valid staff bearer token.
    Reports normalize event/detail/machine/tool/version/integrity fields and
    write suffix-only audit records.
  - **Owner/Staff client behavior:** `AdminToolController` posts one
    deduplicated tamper report after authentication if the local security state
    is locked. It uses normal owner/staff auth headers, keeps secrets in memory
    only, and does not route around `requireUsableSecurity()`.
  - **Contract guard extended:** `tools/admin/check_backend_contract.py` now
    requires the staff auth routes, owner staff-management route, and both
    tamper-report routes to exist and fail closed instead of returning `404`.
  - **Verification GREEN:** focused Python suite
    `tests/test_backend_staff_auth.py tests/test_backend_contract_check.py
    tests/test_orion_admin.py` -> `37 passed`; Python compile OK; dev and
    production `OrionOwner`/`OrionStaff` targets built; package audit OK;
    `tools/security/pack_orion_release.py --verify-only` passed package audit,
    Owner/Staff startup integrity, and missing-manifest tamper refusal.
  - **Full strict gate GREEN:** `scripts/verify_orion.ps1 -StrictSecurity` ->
    Python suite `162 passed, 1 xfailed`; dev native build + ctest `2/2`;
    production build + ctest `2/2`; release package `1310` manifest files;
    release security audit OK; Owner/Staff startup-integrity smoke OK;
    missing-manifest tamper-refusal smoke OK.
  - **Live backend blocker unchanged, now more explicit:** live
    `https://api.zaeorion.com/api/version` is reachable at `0.4.0`, but
    `/api/staff/whoami`, `/api/staff/login`, `/api/admin/staff`,
    `/api/admin/tamper-report`, and `/api/staff/tamper-report` return `404`.
    The AWS deployment must merge these local staff/tamper routes into the
    current live v0.4 Lambda; do **not** blindly overwrite production with the
    local backend file if it would regress `/api/update`.

- **Round 39 (Codex, packaged Owner/Staff UI visual pass, NOT committed):**
  Validated and fixed the privileged tool UI layout in the actual packaged
  release path.
  - **Visual issue found:** packaged `OrionOwner.exe` / `OrionStaff.exe`
    screenshots showed the left authentication/status column expanding across
    the window and pushing the operational panels off-screen; after the first
    fix, Owner's Staff action row was clipped.
  - **UI fix:** `native_orion/qml/admin/AdminMain.qml` now constrains the left
    auth/status column to a dashboard width, top-aligns both main columns, caps
    the status card height, and increases the Owner Staff card height so all
    buttons fit at the default packaged window size.
  - **Visual evidence:** final packaged screenshots saved under
    `logs/diagnostics/ui/orion_owner_ui_final.png` and
    `logs/diagnostics/ui/orion_staff_ui_final.png`. Both show the Orion-style
    header, authentication/status column, license/result panels, and Owner staff
    controls without obvious clipping at `1136 x 759`.
  - **Post-pack dry run GREEN:** `python tools/security/pack_orion_release.py
    --verify-only` passed package audit, Owner/Staff startup integrity, and
    missing-manifest tamper refusal after the UI fix.
  - **Full verification GREEN:** `scripts/verify_orion.ps1 -StrictSecurity` ->
    Python suite `160 passed, 1 xfailed`; dev native build + ctest `2/2`;
    production build + ctest `2/2`; release package `1310` manifest files;
    release security audit OK; Owner/Staff startup-integrity smoke OK;
    missing-manifest tamper-refusal smoke OK.
  - **Remaining blocker unchanged:** `tools/admin/check_backend_contract.py`
    still reports live `/api/version` reachable at `0.4.0`, but
    `/api/staff/whoami`, `/api/staff/login`, and `/api/admin/staff` return
    `404`. Production approval still requires backend route deployment, real
    Owner/Staff enrollment/login/action validation, and a real packer run.

- **Round 38 (Codex, owner/staff packing workflow, NOT committed):**
  Turned the "packing is the last layer" requirement into a concrete, repeatable
  local workflow instead of a hand-written release note.
  - **New tool:** `tools/security/pack_orion_release.py` copies the strict
    release package to `release/orion-package-packed`, optionally runs an
    external packer command template over first-party EXE/DLL targets, regenerates
    `release_manifest.json` after packing, runs package audit, runs Owner/Staff
    `--check-startup-security`, proves missing-manifest tamper refusal, and can
    require the live backend contract check with `--require-live-backend`.
  - **New docs:** `docs/PACKING_RUNBOOK.md` defines the post-StrictSecurity pack
    command path, default pack targets, required post-pack evidence, and release
    blockers. `docs/RELEASE_SECURITY.md` now points Phase 8 at the wrapper.
  - **New tests/gate wiring:** `tests/test_packing_workflow.py` covers packer
    command placeholder/quoting and target parsing; `scripts/verify_orion.ps1`
    now compiles the wrapper and runs the tests.
  - **Post-pack dry run GREEN:** `python tools/security/pack_orion_release.py
    --verify-only` produced `release/orion-package-packed`, regenerated the
    manifest, passed package audit, passed Owner/Staff startup integrity, and
    passed missing-manifest tamper refusal. No real packer was run in this dry
    run.
  - **Verification GREEN:** focused packing/admin/staff tests -> `38 passed`;
    full `verify_orion.ps1 -StrictSecurity` -> Python suite `160 passed,
    1 xfailed`; dev native build + ctest `2/2`; production build + ctest `2/2`;
    release package `1310` manifest files; release security audit OK;
    Owner/Staff startup-integrity smoke OK; missing-manifest tamper-refusal smoke
    OK.
  - **Remaining blocker unchanged:** `tools/admin/check_backend_contract.py`
    still reports live `/api/version` reachable at `0.4.0`, but
    `/api/staff/whoami`, `/api/staff/login`, and `/api/admin/staff` return
    `404`. Actual packer application, packed smoke login/API tests, and
    end-to-end Owner/Staff flows remain blocked until the live backend route
    contract is deployed.

- **Round 37 (Codex, backend deployment contract guard, NOT committed):**
  Added a non-secret live backend checker so the Owner/Staff work cannot be
  accidentally marked done while production still serves stale routes.
  - **New tool:** `tools/admin/check_backend_contract.py` probes
    `https://api.zaeorion.com` without owner/admin secrets. It requires
    `/api/version` to be reachable with Ed25519 update-signing advertised, then
    verifies `/api/staff/whoami`, `/api/staff/login`, and `/api/admin/staff`
    exist and fail closed with JSON auth/missing-field errors instead of `404`.
  - **New tests:** `tests/test_backend_contract_check.py` proves the checker
    rejects missing staff routes and accepts routes that fail closed.
  - **Gate integration:** `scripts/verify_orion.ps1` now compiles the checker
    and runs the checker tests with the Python suite.
  - **Deployment docs tightened:** `backend/README.md`, `docs/ADMIN_TOOL.md`,
    and `docs/SERVER_HANDOFF.md` now warn not to blindly deploy the local
    Lambda over production if it would regress live `/api/update`; the staff
    routes must be merged into the current live backend revision and then the
    checker must pass.
  - **Verification GREEN:** focused admin/staff/backend tests -> `35 passed`;
    full `verify_orion.ps1 -StrictSecurity` -> Python suite `157 passed,
    1 xfailed`; dev native build + ctest `2/2`; production build + ctest `2/2`;
    release package `1310` manifest files; release security audit OK;
    Owner/Staff startup-integrity smoke OK; missing-manifest tamper-refusal smoke
    OK.
  - **Live production check still fails as expected:** `check_backend_contract.py`
    reports `/api/version` reachable at `0.4.0`, but `/api/staff/whoami`,
    `/api/staff/login`, and `/api/admin/staff` return `404`. Production
    Owner/Staff approval remains blocked on AWS/API Gateway deployment and
    end-to-end staff enrollment/login validation.

- **Round 36 (Codex, owner/staff security gate recheck, NOT committed):**
  Re-ran the full strict release gate after adding offline staff-auth abuse coverage.
  - **New backend red-team tests:** `tests/test_backend_staff_auth.py` now covers staff-login nonce replay,
    wrong-machine staff token refusal, server-authoritative staff role enforcement, one-time enrollment key
    consumption, and expired enrollment-key rejection.
  - **Focused verification GREEN:** `tests/test_security_audit.py`, `tests/test_backend_staff_auth.py`, and
    `tests/test_orion_admin.py` -> `40 passed`; source audit OK; package audit OK.
  - **Full strict verification GREEN:** `scripts\verify_orion.ps1 -StrictSecurity` -> Python suite
    `155 passed, 1 xfailed`; dev native build + ctest `2/2`; production build + ctest `2/2`; release package
    built with `1310` manifest files; release security audit OK; Owner/Staff startup-integrity smoke OK;
    missing-manifest tamper-refusal smoke OK.
  - **Live backend blocker remains:** public route probe confirms `/api/version` returns 200, but
    `/api/staff/whoami` and `/api/admin/staff` still return 404 from `https://api.zaeorion.com`. The local
    backend code has the routes, but this shell has no AWS credentials, so deployment and real
    Owner/Staff enrollment/login validation are still required before production approval.

- **Round 35 (Codex, owner/staff tamper + package red-team gate, NOT committed):**
  Tightened the privileged Owner/Staff release gate beyond the Round 34 package integration.
  - **Startup fail-closed check:** production `OrionOwner.exe` and `OrionStaff.exe` now support
    `--check-startup-security`; it verifies `release_manifest.json` before QML loads and exits non-zero
    if a manifest-covered file is missing/modified. In normal production startup, the same failed check
    refuses to load the privileged UI.
  - **Automated tamper smoke:** `scripts/verify_orion.ps1 -StrictSecurity` now runs both admin EXEs
    against the verified package, then copies the package, removes `release_manifest.json`, and confirms
    `OrionStaff.exe --check-startup-security` refuses it.
  - **Package audit tightened:** `tools/security_audit.py` now rejects loose Orion app QML source in
    release packages, requires `OrionOwner.exe` and `OrionStaff.exe`, and extracts printable ASCII/UTF-16
    strings from first-party Orion EXEs/DLLs to scan for concrete dev keys, private keys, Discord bot
    tokens, AWS keys, Cloudflare-token-shaped leaks, and Sellhub/webhook secrets. Third-party Qt/OpenSSL
    DLLs are excluded from binary-string scanning to avoid vendor parser-template false positives.
  - **Verification GREEN:** focused `tests/test_security_audit.py` -> `7 passed`; source audit OK; package
    audit OK; full `verify_orion.ps1 -StrictSecurity` GREEN (`150 passed, 1 xfailed`, dev + production
    build/test, `1310` manifest files, package audit OK, startup integrity smoke OK, missing-manifest
    tamper refusal smoke OK).
  - **Backend validation blocker:** non-secret live route probes show `https://api.zaeorion.com/api/version`
    is reachable but `/api/staff/whoami`, `/api/staff/login`, and `/api/admin/staff` still return `404`.
    Local `backend/lambda_function.py` contains the staff routes, but AWS credentials are not configured
    in this shell (`aws sts get-caller-identity` -> `NoCredentials`), so deployment/real Owner/Staff
    enrollment/login validation remains required before production approval.

- **Round 34 (Codex, owner/staff GUI tools + production package hardening, NOT committed):**
  Added the native Owner/Staff admin executables and verified they ship through the strict production path.
  - **Native admin tools:** added `OrionOwner.exe` and `OrionStaff.exe` targets backed by
    `AdminToolController`. The controller uses the existing security/update primitives, keeps owner secrets
    and staff tokens in memory only, derives a staff-tool machine ID from `SecurityManager`, blocks privileged
    actions when release integrity/security is locked, and calls the owner/staff backend routes for staff
    enrollment/login, staff management, license lookup, HWID reset, and deactivation.
  - **Self-contained QML:** added `native_orion/qml/admin/AdminMain.qml` with local admin-only controls. The
    first smoke found launcher component reuse was invalid because shared components import the `OrionNative`
    QML module; the admin screen now packages only itself plus `Theme.qml`, avoiding loose launcher QML
    dependencies.
  - **Package/security integration:** `scripts/verify_orion.ps1` builds both admin tools in dev and production
    trees; `tools/package_orion_release.py` includes `OrionOwner.exe` and `OrionStaff.exe` while stripping
    loose admin QML source; `tools/security_audit.py` requires both EXEs in the release package; docs updated.
  - **Verification GREEN:** Owner/Staff smoke launch stayed alive with a clean QML runtime log; focused admin
    pytest `32 passed`; source security audit `[orion-security] OK`; standard `verify_orion.ps1` GREEN
    (`147 passed, 1 xfailed`, native build clean, ctest 2/2); strict `verify_orion.ps1 -StrictSecurity`
    GREEN (production build/test/package, `1310` manifest files, release security audit `[orion-security] OK`).
  - **Remaining validation:** confirm the deployed backend is serving the same owner/staff route contract, then
    run one real Owner/Staff login/enrollment smoke against `https://api.zaeorion.com`.

- **Round 33 (Codex, staff/owner backend auth + release-security hardening, NOT committed):**
  Implemented and locally verified the owner/staff security foundation. No secrets were printed or embedded.
  - **Backend staff contract wired locally:** `backend/lambda_function.py` now exposes `/api/staff/enroll`,
    `/api/staff/login`, `/api/staff/whoami`, `/api/staff/license`, `/api/admin/staff`,
    `/api/admin/staff/audit`, `/api/admin/search`, and `/api/admin/whoami`. Staff accounts are Discord-ID
    registered, one-time enrollment keys are salted-hash stored only, staff sessions are HMAC signed,
    short-lived, machine-bound, and disabled/wrong-machine/role-ineligible staff fail closed. All route
    exceptions now return structured JSON instead of empty 500s.
  - **Admin CLI aligned:** `tools/admin/orion_admin.py` now supports `staff-enroll`, `staff-login`, owner
    staff create/disable/reset-machine/reissue-enrollment, staff-token license operations via `/api/staff/license`,
    suffix-only audit formatting, fresh nonce/timestamp, and a hashed local staff-tool machine marker.
  - **Security gate tightened:** `tools/security_audit.py` now blocks Discord bot tokens, AWS temporary keys,
    Cloudflare-token-shaped leaks, and Sellhub/webhook secret leaks, with a narrow allowlist for its own
    synthetic test fixture only. `docs/ADMIN_TOOL.md` and `docs/RELEASE_SECURITY.md` now document the actual
    staff endpoints and the owner/staff build hardening rule: no client-side authority, backend-enforced roles,
    audit-required destructive actions, same production manifest/package path as the launcher, and packing as
    cost-raising only (not a trust boundary).
  - **Native build fix encountered during gate:** `OrionAppController::quitGame()` was declared/invokable by
    Round 32 UI but missing an implementation, causing LNK2001. Added a minimal cancellable quit countdown
    that uses the existing `disconnectRemotePlay()` path; no gameplay timing logic changed.
  - **Gates GREEN:** focused pytest `32 passed`; source security audit `[orion-security] OK`; standard
    `verify_orion.ps1` GREEN (`147 passed, 1 xfailed`, native build clean, ctest 2/2); strict
    `verify_orion.ps1 -StrictSecurity` GREEN (dev + production build/test, production package,
    release security audit `[orion-security] OK`). **Deployment still required** before AWS serves the new
    staff routes.

- **Round 32 (Claude, 2026-06-12 live-batch diagnosis + embed wrong-window fix + calibration prep, NOT committed):**
  User live-tested Round 31: stream floated in its own window; bot timed no shot type to the tip.
  - **Embed root cause (log-proven):** the watchdog embedded `hwnd=0xf084c title=""` — a HIDDEN,
    UNTITLED OrionStream helper window matched by exe name alone (the real "Orion Stream" window
    didn't exist yet at embed time) — and the fast path locked it in as "Embedded" forever. Fixed:
    (1) `findChiakiWindow` exe-only matches must now be visible and >=200x120 (hidden candidates
    need a matching title); (2) the fast path validates the embedded child's title each tick and
    EVICTS a bogus child (restore style/parent, hide, re-search).
  - **Timing diagnosis (35-shot batch):** 33/35 releases fired the open-loop feedforward clock with
    "(meter not visible)"; only Standstill ever got the low-variance meter-appear anchor; fades/Go-To
    fell back to the high-variance hold-start clock; only 6/35 shots produced calibration verdicts →
    every type stuck at phase=acquire. Decoder pipe itself was healthy (tier=decoder, no black runs)
    BUT 1080p60 "Quality" is SOFTWARE decode and delivered only ~24 unique fps (dup% ~60) — vision
    starved. The engine-side fill froze at its hold-start value across whole shots.
  - **Detector noise hole found (synthetic, marked xfail):** dense RGB noise forms 3-24px
    post-morphology blobs that pass EVERY acquisition gate at any confidence; the tracking latch
    follows them blob-to-blob; `_measure_track` synthesizes a plausible ~150px track around them.
    Every offline discriminator tried also kills a real early-rise fill (equally tiny) or a moving
    fade — gate must be tuned on live per-frame data. `test_detect_random_noise_*` xfail'd with the
    revisit condition; suspected contributor to live fades losing the meter mid-shot (frozen fill at
    arbitrary values matches a wrong-blob lock).
  - **Calibration prep shipped:** detframes.csv now ALWAYS ON (rotates prior sessions, keeps 8;
    ORION_DETCSV=0 opts out) so the next batch records per-frame rejection reasons;
    `banner_calibration=true` flipped in settings.json (ground-truth verdicts; this batch doubles as
    the banner band live check).
  - **Settings.json incident (self-inflicted, fixed):** a PowerShell ConvertTo-Json/Set-Content edit
    wrote a UTF-8 BOM that breaks BOTH `load_detector_config` (json.load, silent default fallback)
    AND Qt's parser; rewritten clean via Python round-trip. settings.json.sig is stale until the
    app's next save re-signs (or launch with ORION_AUTO_SIGN_SETTINGS=1). NEVER write settings.json
    with PowerShell.
  - **Gates:** pytest 159 passed + 1 xfailed; native rebuild clean; ctest pending at entry time.
  - **Next live batch protocol:** paced one-shot-at-a-time per type, A/B Balanced 720p60 (hardware
    decode path, ~60 uniq fps expected) vs Quality 1080p60; user gameplay video welcome for offline
    detector tuning against the detframes.csv.

- **Round 31 (Claude, launcher revamp + embed/lightbar/precision pass — gates green, NOT committed):**
  User-reported regressions fixed + full UI restyle + precision hardening (NO timing-constant changes —
  live tip-tuning stays the user's next batch):
  - **Chiaki embed regression root-caused:** `findChiakiWindow()` skipped any title containing "orion",
    which excluded the renamed "Orion Stream" window before its exe-name check could run. Fixed with
    PID-based self-exclusion (GetCurrentProcessId) + WS_CHILD skip + hidden-window candidates; replaced
    the one-shot 6s embed poll with a persistent 100 ms watchdog (20 s startup grace, fast-path geometry
    sync once embedded, one-time embed log).
  - **Stream latency:** chiaki registry now written `render_backend=vulkan` + `use_zero_copy=true` (the
    proven lag-free pair) via new `stream_render_backend` config; if the Vulkan window can't embed in ~8 s
    the watchdog persists an opengl fallback and restarts the stream once.
  - **Lightbar re-enabled:** three stub early-returns removed; AppConfig double force-disable fixed (save
    path hardcoded false + load-normalize override). Precision guard: HID writes skipped unless HoldState
    is Idle/Cooldown. Bluetooth pads get honest "requires USB" status (0x11/0x31+CRC32 out of scope).
  - **Pump-fake output leak fixed (real bug):** a 1-frame Square dropout during the hold-arm window leaked
    a release edge through the pass-through output (the 3-frame history filter only shielded the state
    machine) = phantom in-game pump fake. Output now bridges the dropout; test
    `squareDropoutDuringArmWindowDoesNotLeakReleaseEdge` guards it.
  - **Detector color purity generalized:** the live-proven Purple floatie purity gate extended per-color
    via `_COLOR_PURITY_GATES` (Yellow/Orange/Red-with-hue-wrap/Green/Cyan/White sat-val inversion);
    Purple branch byte-identical. New `tests/test_meter_detector_color_purity.py` (22 tests incl.
    synthetic end-to-end bars). Real fixtures still Purple/Arrow2-only; other colors validated synthetically.
  - **UI revamp:** Theme.qml token system (spacing/type ramp/surfaces/hairlines), shared `ColorWheel.qml`
    (fixes invisible Appearance wheel: Canvas.Image render target + Immediate strategy), quality picker
    moved back under Stream Setup, all components restyled on Theme tokens + user accent.
  - **Gates GREEN:** build clean, ctest 2/2 (incl. new pump-fake test), pytest **160 passed**
    (+22 purity), `verify_orion.ps1` `[orion] OK`, app launches responsive, qmllint 0 errors on revamped
    QML. Live items for the user: embed-on-start inside LIVE CAPTURE, USB lightbar, Vulkan latency feel +
    fallback, fade/Go-To tip batches. NOTE seen in log during launch check: license activation against
    api.zaeorion.com returns Internal Server Error (backend Lambda issue, unrelated to this pass).

- **Round 30 (Claude, Codex release-gate review — 3 blockers fixed, both gates green, NOT committed):**
  Codex flagged release/verification mismatches (no gameplay/timing/detector/controller changes). All three
  confirmed real against the files and fixed:
  - **Blocker 1 (StrictSecurity couldn't pass):** `verify_orion.ps1 -StrictSecurity` built+packaged the dev
    tree (`native_orion\build`, `ORION_PRODUCTION=OFF`) → `package_orion_release.py` correctly REFUSED it.
    Fixed via **option B** (dev/prod trees stay separate — required, not just preferred: a production build
    forces `releaseManifestRequired=true` and fails closed without a release manifest, so it can't be the
    daily/live-test binary). `-StrictSecurity` now configures+builds+ctests `native_orion\build_prod`
    (`-DORION_PRODUCTION=ON -DORION_BUILD_TESTS=ON`), deploys Qt via `windeployqt`, stages libcrypto, then
    `package_orion_release.py --strict --build-dir native_orion\build_prod\Release` + audit. Added `--build-dir`
    to the packager. (First strict run caught a real bug: stale `build_prod` cache had `ORION_BUILD_TESTS=OFF`
    → MSB1009 on the test project; fixed by forcing the flag in the configure.)
  - **Blocker 2 (new pytest suites unwired):** added `test_meter_detector_units.py`, `test_meter_banner.py`,
    `test_rtt_sync_engine.py` to the official pytest list. Gate now reports the true **138 passed**.
  - **Blocker 3 (banner default contradicted handoff):** `AppConfig.h bannerCalibration` had drifted to `true`
    (Phase 1A), shipping ON unvalidated on fresh installs (settings.json isn't packaged). Reverted to `false`
    (OFF pending live crop/band-check, per handoff + deadtop memory); flipped the guard test to set the flag
    explicitly. Codex option A.
  - **Gates GREEN:** standard `verify_orion.ps1` → pytest 138, ctest 2/2 (198.6s), `[orion] OK`. Strict
    `-StrictSecurity` → pytest 138, **dev ctest 2/2 + production ctest 2/2** (197.8s), windeployqt, package
    **1309 files / release_manifest 1308**, audit `[orion-security] OK`, `[orion] OK`. Packager REFUSES the dev
    build, ACCEPTS the production build. Dev tree left `ORION_PRODUCTION=OFF` (morning binary unchanged;
    settings.json already pins banner OFF so live behavior identical). NOT committed (per Codex).

- **Round 29 (Claude, autonomous ship-plan execution): Phases 0-7 COMPLETE + production hardening.**
  All build/test gates green: ctest 2/2 (OrionNativeTests 199.6s + OrionUpdaterTests), pytest 138
  (+15 new detector unit tests), production-hardened tree (`-DORION_PRODUCTION=ON`) compiles clean,
  no-env smoke QML-warning-clean with production defaults self-selected.
  - **Production defaults flipped ON**: `ORION_INPUT_HOOK` + `ORION_FRAME_PIPE` default ON (explicit `=0`
    unsets so the Python sidecar/chiaki truthiness checks read OFF). No env vars needed for the
    live-confirmed lowest-latency config.
  - **Security: `-DORION_PRODUCTION=ON` compiles out EVERY dev escape hatch** (local-dev license,
    update-gate skip, debug UI, ORION_FREEZE_CAL, force-virtual-neutral) and forces the release-integrity
    manifest required regardless of the on-disk policy file. `package_orion_release.py` refuses to package
    a non-production build. Phase 8 obfuscation/packing plan written in docs/RELEASE_SECURITY.md.
  - **Crash logs**: exportDiagnostics now bundles the newest crash dumps (<=3, <=32MB) + README; new
    Open Logs Folder button. **Window**: 1860x1000 -> 1560x900 (still fits the 1280x720 capture floor).
  - **Bloat removed**: 4 orphaned QML components deleted (SettingsRow, EmptyState, DashboardSliderSpin,
    PatchNotesIconButton).
  - **REMAINING = the live gate (user runs in the morning):** per-type tip-greens (Standstill, L/R Fade,
    Go-To, contested + open), ACQUIRE->LOCK unaided, zero pump-fakes/hitches; then the wifi batch
    (high-90s, no spike misfires). Phase 8 strict-security/obfuscation begins only after live sign-off.

- **Round 28 (Claude, user-directed): pre-live-test AUDIT of the input-hook path + fixed a teardown
  DEADLOCK; gate GREEN** (native build + ctest 100% — OrionNativeTests 164.9s + OrionUpdaterTests —
  + py_compile + pytest 109). User asked to find obvious/deep bugs BEFORE running the frame+hook live test.
  - **Audit verified safe (each would have wasted a live session):** pipe protocol byte-identical
    (24-byte packed `OrionInputPacket` + magic 'ORIN'); all 16 button bits match (Square=BOX=`1<<2`, so the
    shot-release press registers); CONTINUOUS ownership (call site sends `own=true` every tick — required
    because the patched chiaki doesn't pump SDL, so movement won't die); stick sign/scale consistent
    (up=negative both sides, ×258→int16); bridge created AFTER `chiaki_session_init` + destroyed (joined)
    BEFORE `chiaki_session_fini`; `chiaki_session_set_controller_state` is internally mutex-guarded
    (bridge thread + 30Hz keep-alive concurrent calls are race-free); SendFeedbackState flood capped 30Hz;
    frame export decoupled from the decode thread; launch wiring intact (`find_chiaki_binary` force-selects
    the patched build + `chiaki_controller_env` passes both pipe envs, pipe names match). Build freshness
    confirmed (binaries newer than sources).
  - **DEEP BUG FIXED — input-bridge teardown deadlock.** `~OrionInputBridge` only opened a dummy client to
    unblock, which fails (ERROR_PIPE_BUSY — single-instance pipe) when Orion is connected-but-idle, so the
    blocking `ReadFile` never returned and `thread.join()` hung → chiaki hangs on stream-stop/exit, leaving
    an orphan holding the pipe name → the NEXT launch's hook silently fails to start. Fixed by tracking the
    live server pipe HANDLE (mutex-guarded `live_pipe_`) and `DisconnectNamedPipe`-ing it in the dtor to
    force the blocked read to fail — mirrors `OrionFrameExport`'s teardown. Chiaki rebuilt + redeployed.
  - **Cosmetic (Orion):** corrected the stale "windowed ownership" comments in `OrionInputClient.{h,cpp}`
    (the live path is continuous own=1) and removed the dead `hookOwnUntilMs_` member (+ its `(void)` cast).
  - **Files:** chiaki-ng-src `gui/include/orioninputbridge.h` + `gui/src/orioninputbridge.cpp`;
    `native_orion/src/OrionInputClient.{h,cpp}` + `OrionAppController.{cpp,h}`; `STATUS.md`. **NOT committed.**
  - **LIVE_VALIDATION_REQUIRED:** launched OrionNative (PID 17772) with `ORION_FRAME_PIPE=1
    ORION_INPUT_HOOK=1 ORION_INPUT_DIAG=1` + local-UI/unlock; "Input hook ENABLED" confirmed. Connect +
    move stick (RESPONSIVE = cracked) + take per-type shots. Confirm: `logs/hook_test_console.log`
    `OrionInputBridge: inject/s=` tracks the stick FAST (vs the old ~0.8/s crawl) + `orion_native.log`
    `Input diag: … hook=1` tracks `rawLX`→`outLX`. If movement lags, unset ORION_INPUT_HOOK to fall back.

- **Round 27 (Claude, user-directed): DEAD-TOP targeting for ALL shot types + grader FREEZE +
  banner-calibrate workflow. Gate GREEN** (verify_orion.ps1: Python + native build + ctest 100%, 163s).
  Offline session (no live gameplay available); per-type fade/Go-To convergence is the only piece left
  and it needs live batches.
  - **DEAD-TOP (Phase 1):** all types now aim the green window's TOP EDGE (`GreenWindowTracker::bestEndPct_`,
    the contest-invariant make-point), not the center. `adaptiveTargetPct` rewritten (no width-based
    center/tip split; `targetMode` always `green_tip`); new tunable `RemapConfig::meterTipMarginPct`=0.
    Updated the 3 tests that pinned the old center behavior. Resolves always-tip vs center = ALWAYS-tip.
  - **PROVEN: dead-top Standstill converges in ~2 shots then hits the tip 100%.** Banner-classified
    `2026-06-08 22-24-27.mp4` via new `extract_shot_banners.py` (validated): 9/11 ON-TARGET = 9/9 green
    AFTER the 2-shot cold-start acquire (seq1-2 missed while the lead converged; seq3-11 all green). The
    shipped bot (frozen at dialed values) has no cold-start -> ~100% on Standstill.
  - **The meter self-grader CANNOT grade green-vs-late at the tip** (a made shot and a late shot BOTH peak
    ~100 then recede; no drop-from-peak / gap-below-window heuristic separates a Standstill GREEN from a
    contested-fade MISS -- the Round-26 fade test caught every attempt). On this batch it was wrong BOTH
    ways: false-LATE'd 5 greens AND false-EXCELLENT'd the seq2 real miss. Corrects "meter is ground truth".
  - **FIX = FREEZE the grader + BANNER-calibrate (user-chosen):** `settings.freeze_calibration` / env
    `ORION_FREEZE_CAL` guards `learnFromOutcome` -> per-type clock/offset HOLD at learning.json (the per-type
    CLOCK still self-learns from reliable vision-timed releases). Dial the per-type OFFSET from the TIMING
    banner via new `tools/diagnostics/extract_shot_banners.py`. Offset ~52ms (shared release->register
    latency); dialed clocks ship OPEN-LOOP (no runtime HUD reliance). learning.json: Standstill dialed
    (52/502/402); Left/Right Fade + Go-To pre-seeded offset 52 + animation clocks.
  - **Files:** `AutomationEngine.{cpp,h}`, `AppConfig.{cpp,h}`, `AutomationEngineTests.cpp`,
    `tools/diagnostics/extract_shot_banners.py` (new), `learning.json` (+`.pre-deadtip.bak`), `STATUS.md`.
    **NOT committed.** Custom-chiaki input hook deferred (3/4 blockers fixed, input-lag remains) -- see
    memory `nexusvision-hook-session-status` + `nexusvision-deadtop-banner-calibrate`.
  - **LIVE_VALIDATION_REQUIRED:** paused per-type batches (Right Fade next), run extract_shot_banners.py,
    dial the per-type offset from the banner, repeat to 95%. Fades may hit the ViGEm jitter floor.

- **Round 26 (Claude, user-directed): window-relative post-release grader fix (kills the last false-EXCELLENT
  on fades) + the contest-window finding that explains why fades miss. Gate GREEN** (Python 89, native clean,
  ctest 100% incl. 1 new test). Validated against a USER-LABELED live batch (`2026-06-08 14-25-08.mp4`).
  - **Live batch (default ViGEm path), USER labels vs bot self-grade:** Standstill 8 green/3 late — bot
    MATCHED exactly (honest). Left Fade **0 green** (7 late/3 early) — bot self-graded 4 EXCELLENT (FALSE).
    Right Fade 5 green/5 late/1 early/1 false-shot — bot ~6 EXCELLENT (slight over-report). Cross-checked by
    reading the in-game TIMING banner straight from the recording (ffmpeg crop, top band): bot DIRECTION
    always honest (LATE->LATE, EARLY->EARLY) but EXCELLENT over-reported on fades.
  - **ROOT CAUSE (data-proven via recording replay):** a CONTESTED FADE's green window is a SLIVER near the
    very top (~[98,100], ~2-3%); Standstill's is wide (~[90,100], ~10%). The grader's EXCELLENT used a
    +/-7% CENTER deadband, so a fade that settled ~7% BELOW the [98,100] sliver got stamped EXCELLENT (false
    green -> false LOCK -> fades never corrected). Real greens settle in/at the window (gap<=~2); fade misses
    settle well below it (gap 6-26).
  - **Fix (`AutomationEngine.cpp evaluatePostReleaseMeter`):** EXCELLENT now requires the settled fill GENUINELY
    inside the detected green window `[gs-winTol, ge+winTol]` (window-RELATIVE), winTol=`meterGreenWindowTolPct`
    =3 (replaces `meterGreenDeadbandPct`=7). Reproduces ALL 31 user labels (Standstill 8G/3L, Left 0G, Right 5G).
    +1 test `postReleaseGradesRelativeToGreenWindowNotCenterDeadband` (real numbers: settled-92 vs window[98,100]
    -> LATE not EXCELLENT; settled-89 vs window[90,100] -> EXCELLENT).
  - **The deeper truth:** fades miss because the contest window (~2-3% = ~10ms in time) is SMALLER than the
    ViGEm-path shot-to-shot JITTER. The grader fix makes the self-grade HONEST (no false locks, keeps centering)
    but does NOT by itself make fades green. The lever for fades = the INPUT HOOK (Round 25) which removes the
    ViGEm-submit + SDL-poll jitter so the release can land in the sliver. Next batch should be WITH the hook.
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/tests/AutomationEngineTests.cpp`,
    `tools/diagnostics/analyze_shot_settle.py`, `STATUS.md`. **NOT committed.** OrionNative rebuilt + relaunched
    (default path, hook OFF). learning.json carries the batch's per-type calibration.
  - **LIVE_VALIDATION_REQUIRED:** a paused per-type batch WITH `ORION_INPUT_HOOK=1` -> does jitter reduction let
    fades land in the [98,100] sliver? Self-grades are now honest, so the batch is truthfully measurable.

- **Round 25 (Claude, user-directed): CUSTOM CHIAKI-NG built from source + pre-encryption INPUT HOOK
  implemented & wired into Orion (gated OFF; ViGEm fallback). The long-"blocked" production milestone is
  unblocked.** Chiaki builds clean; OrionNative builds clean with the hook client.
  - **Why:** the last ~10-15% of fade green-rate is bounded by input timing JITTER (ViGEm submit + SDL
    poll-phase) on a ~30-40ms green window. The fix is injecting Orion's ControllerState INSIDE chiaki,
    pre-encryption (no ViGEm/SDL), at the deterministic instant Orion chose.
  - **Custom chiaki-ng BUILT** (MSYS2/MinGW) after clearing 4 env dead-ends (first-run runtime close;
    pacboy :p translation; CMake-4.3.3 + Meson home-dir bug; invalid -Dlibunwind). KEY: use MSYS2 packages
    `libplacebo` 7.360.1 (exact CI version, no ffmpeg dep) + `SDL2` instead of the from-source builds;
    pinned ffmpeg 7.1. Recipe in [[nexusvision-custom-chiaki-architecture]] + `Desktop/chiaki_build.sh`.
    Source: `Desktop/chiaki-ng-src`. Patched build deployed SEPARATELY to
    `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/` (working chiaki-ng-Win untouched).
  - **Input hook (chiaki side):** new `gui/include|src/orioninputbridge.{h,cpp}` — named-pipe server
    (`\\.\pipe\orion_input`) reader thread -> `chiaki_session_set_controller_state()` (mutex-guarded,
    flushes feedback history immediately, pre-encryption). Wired into `StreamSession` (create/destroy +
    `SendFeedbackState`/`DpadSendFeedbackState` defer while owning). Opt-in via env `CHIAKI_ORION_INPUT_PIPE`.
  - **Orion side:** `native_orion/src/OrionInputClient.{h,cpp}` (pipe client; XInput->PS mapping,
    Square=BOX, sticks ×258 same-sign, de-dupes) wired in `OrionAppController` right after the ViGEm submit,
    gated by env `ORION_INPUT_HOOK` (default OFF). `remote_play_client.py chiaki_controller_env()` passes
    `CHIAKI_ORION_INPUT_PIPE` when the hook is on. ViGEm is still submitted every tick as the fallback.
  - **To enable (live test):** launch OrionNative with env `ORION_INPUT_HOOK=1` — that's it. The patched
    chiaki AUTO-selects (`find_chiaki_binary` prefers it when the hook is on, overriding settings
    chiaki_path) and gets `CHIAKI_ORION_INPUT_PIPE`. Default (no env) = unchanged ViGEm path + normal
    installed chiaki (the Round-24 fades-fixed bot).
  - **Files:** chiaki-ng-src (orioninputbridge + streamsession + CMakeLists + build-libplacebo script);
    `native_orion/src/OrionInputClient.{h,cpp}`, `OrionAppController.{cpp,h}`, `native_orion/CMakeLists.txt`,
    `remote_play_client.py`, deploy tree, `STATUS.md`. **NOT committed.** Decoder feed still TODO.
  - **LIVE_VALIDATION_REQUIRED:** enable the hook, take a paused per-type batch; confirm input works
    (movement + shoot) and fade green-rate improves vs the ViGEm path. Mapping/own-timing are best-effort
    (un-live-tested); iterate from the first batch. If anything is off, unset ORION_INPUT_HOOK to fall back.

- **Round 24 (Claude, user-directed /plan): KEYSTONE bbox wiring + deflate-convergence + near-top green floor; gate GREEN**
  (Python **89 passed**, native build **clean/0 warnings**, ctest **1/1** incl. 1 new regression test). Full-system
  audit per AUDIT_BRIEF.md. User decision: meter-only calibration (NO banner), brief pause between shots OK.
  - **ROOT CAUSE (keystone, code-proven): the meter bbox was NEVER wired from the sidecar to the engine.**
    `RemotePlaySession.cpp` built the sidecar `DetectionResult` without setting `x/y/width/height`, so every
    post-release `MeterCalSample.bx/by` was 0 -> `meterSettledFrames` saw ZERO motion every frame -> a sliding
    fade/Go-To meter looked "settled" and was graded mid-deflate (at the ~1.2s soft deadline while the meter was
    still near peak) -> **false EXCELLENT**. Round 23's meter-settle only ever worked in unit tests (which set
    bx/by by hand). Fix: serialize `_last_meter_bbox` in `autogreen_sidecar.py`; parse it in `RemotePlaySession.cpp`
    into `sidecarResult.x/y/width/height` (the engine already maps these to bx/by). +1 regression test
    `settleGradingRequiresBboxMotionSignal` (sliding bbox SKIPPED vs static bbox GRADED) so it can't silently break again.
  - **Deflate-LATE over-correction (the Right/Left Fade oscillation): the recede AMOUNT does not encode HOW late**
    (any overtime snaps the post-shot marker to ~the same low rest point). The old grader scaled the shorten step
    BY recede -> railed every deflate at ±160 -> bang-banged EARLY<->LATE, never settling. Fix: deflate-LATE is now
    a FIXED bounded nudge (`meterRecedeLatePct*meterMsPerPct`=66ms); `meterMaxErrorMs` 160->90 bounds EARLY/overshoot
    symmetrically. Smooth ACQUIRE convergence into the window.
  - **D2 near-top green floor (`meter_detector.py` `_measure_track`):** reject a green whose mapped CENTER < 60% of
    track or width > 50% (in the PUBLISHED scale, applied AFTER geometry so fill is UNTOUCHED). Live 2026-06-07
    bogus green centers 27-56% on full meters were driving the engine's release target mid-track -> firing fades far
    too early. A/B on `2026-06-07 21-08-10.mp4`: bogus green frames rejected, all moving shots now read green_center
    81-100% (was 67-88), detection rate unchanged, fill unchanged.
  - **Dead code removed:** the `fixed_fallback` release path (gated on `holdReleaseStrategy=="fixed"`, never set) +
    the `holdReleaseStrategy` field. `updateShotOutcome` kept (native test hook). Config round-trip verified intact.
  - **Validated OFFLINE on the newest recording** (`tools/diagnostics/analyze_orion_recording.py` +
    new `analyze_shot_settle.py`): fades DEFLATE peak100->settled20-63 (= genuinely LATE, confirms the keystone
    mechanism); rise-to-peak ~383ms; settle thresholds (meterSettleMaxMovePx=12) already separate slide (27-80px)
    from settle (~2px) cleanly -> no change. Timing-seed catch-22 NOT manifesting (clocks seed/arm in the live log).
  - **Files:** `native_orion/backend/autogreen_sidecar.py`, `native_orion/src/RemotePlaySession.cpp`,
    `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/tests/AutomationEngineTests.cpp`, `meter_detector.py`,
    `tools/diagnostics/analyze_shot_settle.py` (new), `STATUS.md`. **NOT committed.** learning.json already clean;
    OrionNative rebuilt + relaunched.
  - **LIVE_VALIDATION_REQUIRED:** paused per-type batch (Standstill/Left/Right Fade/Go-To, ~8-10 each, pause ~2-3s
    between shots). Confirm fades/Go-To now self-grade honestly (LATE when they deflate, not false EXCELLENT),
    each type converges (acquire->lock) into green, and the self-grade matches what you see.

- **Round 23 (Claude): meter-SETTLE grading (fixes false EXCELLENT on MOVING shots); gate GREEN**
  (native build clean, ctest **1/1**, **82** methods incl. **2 new**). User-directed, off a live batch.
  - **Why:** Round 22's recede fix made Standstill (static meter) bootstrap perfectly (LATE→9 EXCELLENT,
    LOCKED @406ms), BUT the bot's self-grades were **FALSE for MOVING shots** — the user (eyes on the
    game = ground truth) saw only ~2-3 Left / 1 Right / **0 Go-To** actually green vs the log's "all
    EXCELLENT + lock". Lesson: convergence-to-EXCELLENT is NOT proof; a grader fed garbage locks on its
    own bad reads. Root cause: the post-release meter read is reliable only when the meter is STATIC; on
    a fade/Go-To the airborne player slides the bbox → noisy read → median lands near green → false EXCELLENT.
  - **User-confirmed meter mechanic (authoritative):** EARLY = meter STOPS before green; LATE = overtimed,
    reaches the top then BOUNCES BACK; GREEN = literally TOUCHES green. This exactly matches the peak-gated
    grader (peak<green→EARLY; peak-reached + settled-deflated→LATE; settled-in-green→EXCELLENT).
  - **Fix (`AutomationEngine` meter-settle):** grade ONLY the SETTLED frozen marker. `meterSettledFrames()`
    keeps the latest STATIC-bbox run (consecutive clean frames within `meterSettleMaxMovePx`=12) and its
    fill-stable TAIL (within `meterSettleFillTolPct`=8 of the last fill — excludes the rise/bounce).
    Adaptive deadline: grade once settled (soft `postReleaseWindowMs`), else keep capturing to
    `postReleaseMaxWindowMs`=3000 then grade-or-SKIP. A never-settling moving meter → SKIP (no false grade).
    `MeterCalSample` gained bbox center (bx,by). **Requires the user to PAUSE ~2-3s between shots** so the
    fade/Go-To meter settles + shots don't overlap.
  - **Tests (+2):** `postReleaseMovingMeterIsNotGraded` (moving bbox + would-be deflate-LATE → SKIPPED,
    clock unchanged), `postReleaseGradesSettledTailAfterMotion` (sliding then static tail → graded LATE).
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/tests/AutomationEngineTests.cpp`,
    `STATUS.md`. **NOT committed.** learning.json reset to clean slate; OrionNative relaunched.
  - **LIVE_VALIDATION_REQUIRED:** fresh PAUSED per-bucket batch — confirm fades/Go-To now grade accurately
    (self-grade matches what you see) and converge. If a fade meter never settles statically → fall back to
    ROI-tracking it or a release-triggered banner read.

- **Round 22 (Claude): post-release grader recede-INVERSION fix; detector exonerated; gate GREEN**
  (native build clean, ctest **1/1**, **80** methods incl. **2 new**). User-directed audit. The session
  brief's premise (detector under-reads a full meter as ~52%) was **DISPROVEN**, then the real bug found,
  fixed, and validated on the recordings.
  - **Detector is ACCURATE (not the bug).** Cropped the real meter from `2026-06-07 05-55-09.mp4`: a "52%"
    read is a genuinely half-full meter; a "95%" read is genuinely full. The planned `_measure_track`
    rewrite was **abandoned** (would corrupt a correct sensor). `meter_detector.py` UNCHANGED.
  - **Real bug = `evaluatePostReleaseMeter` inverts LATE→EARLY.** Recording-proven via 06-06 (worked) vs
    06-07 (broke) + the in-game TIMING banner as ground truth: the post-release meter **encodes the
    outcome** — a made/EXCELLENT shot **holds ~92%** (near green), a LATE shot rises through green then
    **DEFLATES to ~52%**. The grader medianed the settled fill: 92≈green→EXCELLENT (right), but 52≪green→it
    called **EARLY** (wrong; it's LATE) → `learnFromOutcome` LENGTHENED the clock → runaway (06-07 standstill
    seq1-7: all EARLY@−160, offset→−120, ffClock 568→926). This was a **regression**: the prior rewrite
    "deleted the bogus recede→LATE rule" — but that rule was correct; the new code keeps it **peak-gated**.
  - **Fix 1 (primary) — recede-aware verdict (`AutomationEngine.cpp evaluatePostReleaseMeter`):** settled
    in green/deadband→EXCELLENT; above green→LATE(overshoot); **settled below green BUT peak reached green
    →LATE(deflate)** [the missing case]; peak fell short of green→EARLY. The **PEAK gate** (peak already
    captured in `meterCapPeakFillPct_`) separates a deflate (LATE) from a genuine early shot (EARLY) even
    when the settled fill is identical (~52). New `RemapConfig::meterRecedeLatePct=12`.
  - **Fix 2 — clock-runaway divergence guard (`learnFromOutcome`):** per-type railed-same-sign streak >
    `calDivergenceGuardShots`=10 freezes clock integration so a degraded signal can't pin the clock at a
    clamp. Runtime counters `calRailStreak_`/`calRailSign_` (not persisted).
  - **Tests (+2):** `postReleaseDeflatedMeterGradesLateVsEarly` (identical settled 52 → LATE if peak 97 vs
    EARLY if peak 55 — pins the peak gate), `clockRunawayGuardFreezesOnRailedStreak`. ctest 100% (80 methods).
  - **Offline A/B on real frames (detector→grader replay):** 06-07 seq4 (banner LATE) flips **EARLY→LATE**
    (matches banner); 06-06 seq28/29 EXCELLENT **unchanged**. End-to-end validated.
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/tests/AutomationEngineTests.cpp`,
    `STATUS.md`, `TASK.md`. **NOT committed.** Plan: `.claude/plans/zazzy-drifting-lerdorf.md`. Detector
    untouched. Deferred (optional, not blocking): detector near-top green-window sanity gate (bogus 20-98
    green on the 06-04 full fixture); meter-appear anchor false-trigger guard.
  - **LIVE_VALIDATION_REQUIRED:** relaunch OrionNative, take a per-bucket batch (warm OR cold `learning.json`);
    confirm the bot's self-grade now MATCHES the banner (LATE shots graded LATE) and the per-type clock
    CONVERGES/LOCKs into green instead of railing. Cold start should now bootstrap (correct LATE → shorten →
    feedforward clock trains → release like 06-06 → EXCELLENT).

- **Round 21 (Claude): per-type ACQUIRE→LOCK calibration + meter-appearance anchor A/B + Go-To
  unrail; gate GREEN** (native build clean — OrionNative + OrionNativeTests + OrionNative.exe —
  ctest **1/1**, 78 native methods incl. **7 new**). Direct user-directed work: get EVERY shot type
  to the right-fade-level green reliability (Standstill / Left & Back Fade / Go-To still under),
  implementing the user's "find an offset that's super close, then make only minuscule adjustments"
  model.
  - **Root cause:** slow meters (Right Fade ~650ms) are forgiving of timing jitter; FAST meters
    (~400-580ms) cross the same green window in less time so the same jitter misses. Every fast shot
    fired on the per-type feedforward clock measured from **hold-start**, whose lever arm to the tip
    carries variable gather/animation-start jitter. And the calibrator **never locked** — it kept
    integrating `gain×error` forever, so a dialed-in type drifted/hunted the ~30-40ms window.
  - **Lever 1 — ACQUIRE→LOCK controller (`learnFromOutcome`):** per type, ACQUIRE converges fast
    (annealed gain); after `calLockAfterGreens`=3 consecutive in-green outcomes it **LOCKS** (baseline
    frozen, only a ≤`calLockMaxTrimMs`=4ms micro-trim per shot); `calUnlockAfterMisses`=3 misses drop
    it back to ACQUIRE (gain restored). Phase persists per type (`LearningData::shotTypeCalPhase`) so a
    dialed-in type restarts LOCKED. Removed the dead `feedforwardCalib*` trio.
  - **Lever 2 — meter-appearance anchor + runtime A/B (`RemapConfig::feedforwardAnchor`,
    settings.json `feedforward_anchor`):** new learned clock `shotTypeMeterToReleaseMs`
    (firstMeterSeen→release) times the FIXED meter-appears→tip segment — far less variance than
    holdStart→tip for fast meters. `triggerRelease` seeds BOTH clocks from every vision-timed release;
    `processHolding` fires on the meter-appear clock when armed+seen, falling back to the hold-start
    clock when the meter is invisible. Default `meter_appear`; flip to `hold_start` for the live A/B.
  - **Lever 3 — Go-To unrail + telemetry:** per-type offset clamp widened ±60→±120 (Go-To was railed
    at +60 = structurally late). `Shot outcome:` line gains `meterClockMs=`/`anchor=`/`phase=`/
    `greens=`/`misses=` tokens (new tokens only; existing fields untouched). Persistence: AppConfig
    load/save the 2 new maps; OrionAppController persists `meterClockUpdated`/`calPhaseUpdated`.
  - **Tests (+7):** `calLocksAfterConsecutiveGreens`, `lockedTrimIsBounded`, `calUnlocksAfterMissStreak`,
    `learnedOffsetClampWidenedBeyondOld60`, `meterAppearAnchorReleasesOnAppearClock`,
    `meterAppearAnchorFallsBackToHoldStartWhenInvisible`, `bothAnchorClocksLearnedFromVisionRelease`.
    All existing outcome/feedforward tests still pass (calibrating both clocks keeps them green).
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/src/AppConfig.{cpp,h}`,
    `native_orion/src/OrionAppController.cpp`, `native_orion/tests/AutomationEngineTests.cpp`,
    `STATUS.md`, `TASK.md`. **NOT committed.** Plan: `.claude/plans/jiggly-jingling-bentley.md`.
  - **LIVE_VALIDATION_REQUIRED:** relaunch OrionNative (loads rebuilt DLLs), reset `learning.json`,
    take a per-bucket recorded batch with the TIMING HUD. Run `feedforward_anchor=meter_appear`, then
    flip to `hold_start`, compare per-type green rate (`Shot outcome:` lines). Confirm `phase=acquire`
    → `phase=lock` after ~3 greens and locked types hold green across Standstill/fades/Go-To. Open math
    follow-up (user Q, not yet built): meter-appear **template phase-alignment** predictor; feed-latency
    reduction (custom-chiaki) is the remaining lever for the last few %.

- **Round 20 (Claude): targeting correction (tip-anchored, REVERTED a center-aim detour) + feedforward
  plan; gate GREEN** (native build clean, ctest **1/1**, **71** native).
  - **User live evidence (authoritative):** fades GREENED miniscule/contested windows "that didn't appear
    to be there" by timing the **TIP**, regardless of contest. Mechanism: the green window's **TOP edge =
    the make-point and is contest-INVARIANT** — contest shrinks the window from BELOW; the top is also the
    one part still detectable when the sliver is invisible (it sits at the meter max). So **tip-anchored
    aiming is correct; center is wrong** (it drifts up with contest and needs detecting a tiny moving
    window). A brief center-aim change was made then **REVERTED** — `adaptiveTargetPct` is back to
    wide→center / contested→tip (`start + 0.9*width`), `targetMode` green_center/green_tip by width. Tests
    restored (71); the center test was removed.
  - **NEXT (the real lever for near-perfect, user-authorized "add any prediction math"):** a **FEEDFORWARD
    deterministic-timing model.** The meter is a deterministic per-shot-type animation; the bot can learn
    the time from shot-start (hold begin) to the green tip PER TYPE and release on that learned clock —
    robust to detection dropouts and INVISIBLE/contested windows (it times the animation, not the pixels).
    Fuse with the reactive quadratic crossing when detection is good; lean on feedforward when it's poor.
    The Round-19 HUD verdict loop trains/validates the per-type feedforward time. Needs the USER'S
    RECORDINGS of the working tip-timed fades to verify the contest-from-below model + calibrate per type
    (user offered them).
  - **HUD reader — EARLY sign + game-mode robustness (from the user's 4 `*-master_playlist.mp4` clips,
    Downloads, 1920x1080 60fps ~15s each, real 1v1 game mode w/ COVERAGE+contest):** captured the
    **EARLY** template (`tests/fixtures/hud/EARLY.png`) — the loop can now distinguish EARLY (red, err
    -28) from LATE (red, +28); SIGN reading is complete both directions. Fixed a game-mode bug: the HUD
    also shows a GREEN "WIDE OPEN" COVERAGE banner that the reader misread as an on-target GOOD verdict —
    now an unmatched colour word of EITHER colour is OFF/ignored (only a matched verdict template counts).
    +2 fixtures (`early_game.png`, `wideopen_game.png`) + 2 tests (14 HUD tests pass). NOTE: game-mode
    HUD layout differs (COVERAGE｜TIMING｜DISTANCE + scoreboard; verdict x-position shifts) — a TIMING-label
    anchor is the proper follow-up for full game-mode robustness (current reader handles contested clips
    where COVERAGE is yellow; WIDE-OPEN green is neutralized not yet read).
  - **Feedforward FEASIBILITY CONFIRMED on the clips:** the meter detector traces each shot's fill rising
    to 100% over a consistent **~0.4-0.5s** (meter-appear → top), so a learnable per-type hold-start→tip
    time exists → open-loop timed release is viable. (No matching `orion_native.log` for these clips, so
    live the engine measures hold-start→tip directly.)
  - **Files:** `native_orion/src/AutomationEngine.cpp`, `native_orion/tests/AutomationEngineTests.cpp`,
    `hud_feedback.py`, `tests/test_hud_feedback.py`, `tests/fixtures/hud/EARLY.png`,
    `tests/fixtures/hud_frames/{early_game,wideopen_game}.png`, `STATUS.md`. **NOT committed.**
    `settings.json early_late_offset_ms` still 100.
  - **CLIPS LABELED + ANALYZED (user-directed):** clip1 = GO-TO, clips 2/3/4 = CONTESTED RIGHT FADES,
    all the USER's own shots (manual). EARLY/LATE verdicts in the clips are the AI OPPONENT's (the loop
    must only learn from the bot's OWN offensive releases — the [500,3000]ms post-release pairing handles
    it; a "verdict APPEARS after my shot, ignore a lingering opponent verdict" refinement is the robust
    version). With a widened band (analysis only) the reader read EVERY clip correctly: AI EARLY/LATE
    early, then the **user's EXCELLENT** at the shot — i.e. **the user's shots are all EXCELLENT (gold
    standard to replicate)**, and the reader distinguishes user-vs-AI by timing. **clip4 = a contested
    R-fade with a ~1.8% green SLIVER ("didn't appear to be there") that the user hit EXCELLENT** →
    confirms contested = tiny sliver near the TOP, tip is the anchor (validates the Round-20 tip revert).
    Per-type meter rise differs (go-to ~550ms vs faster fades) → per-type feedforward justified.
  - **GAME-MODE HUD READING — DONE (sliding matcher, better than the planned label anchor).** The white-
    label-anchor idea was confounded by the bright in-game architecture; instead `_match_sliding` now
    SCALE-slides each verdict template over the colour mask and takes the peak correlation — it locates
    the verdict by SHAPE at any position/size, so a WIDE band (0.30-0.60W) can't merge the word with
    adjacent UI (COVERAGE/DISTANCE/scoreboard/background). KEY fix: templates are stored at TRUE aspect
    (height-normalized, NO width cap) — the old canonical squished long words (EXCELLENT) and broke the
    slide. Validated: practice LATE/EXCELLENT, live 1550x697 LATE, game-mode EARLY (clip2) AND the USER's
    EXCELLENT shots (clip1/2/3, 0.81-0.93), WIDE-OPEN COVERAGE → NONE (not misread). 15 HUD tests
    (+excellent_game). Python-only, no native build needed. Dead `_match` removed.
  - **REMAINING accuracy build — FEEDFORWARD model** (learn hold-start→green-tip per shot type LIVE,
    trained by the bidirectional HUD loop, fused with the reactive crossing). **BLOCKED:** it's a native
    engine change and OrionNative won't compile while the "Quit" packet-drop DoS is present — unblocks
    once that's removed (user is having another AI remove it).
  - **RTT sync: VERIFIED working for timing** (inspection): RTTSampler(ICMP/TCP)+KalmanFilter1D+
    CourtIPDetector → orchestrator feeds rtt_half_ms+jitter to `update_network_offset` → every live
    release carries a real `offset=` (6-27ms). Tuning note only: offset feeds as soon as RTT has ANY
    samples (not just when "ready"), so first shots can use a noisier estimate.
  - **DoS FULLY REMOVED + BUILD UNBLOCKED (Round 20 cont.):** the "Quit"/Lag features are GONE end-to-end
    — native (`killSession`, `startPacketDrop`/`drop_all`, `setPacketDropping`/session_kill.flag, the
    lag-switch, all sessionKill/dropping state + the QML cards) AND the WinDivert bridge `nexus_svc.py`
    (the ICMP-flood builders, `set_drop_all`/`set_lag_switch`/`_drop_loop`/`_lag_loop`/`_flood_loop`/
    `_inject_rst` ~442 lines, the drop_all/lag_switch command handlers). Only the LEGITIMATE passive
    packet capture (court-IP/RTT telemetry) remains. **OrionNative now COMPILES CLEAN** (was blocked).
  - **RTT SYNC VERIFIED — synced to the court IP (working to its fullest):** NetworkBridge detects the
    court IP (passive capture) → `setCourtIp` → `set_court_ip` cmd → sidecar → `rtt_engine.set_target_ip`
    → active RTT ping to the court → `update_network_offset` into the release lead. Live log shows real
    per-shot offsets. Chain complete + correct.
  - **FEEDFORWARD model — BUILT (ctest 74/74).** Per-shot-type learned **hold-start→release CLOCK**
    (`LearningData::shotTypeFeedforwardMs`, persisted to learning.json; mirrors the offset learning).
    Trained from VISION-timed releases (green/predictive) in `triggerRelease` (EMA, gain 0.25); a
    feedforward release does NOT retrain itself. New release block in `processHolding` placed BEFORE the
    "never release before a meter surfaces" gate, so it times a contested/**INVISIBLE** sliver ("the
    window that wasn't there") that never passed the detection gate — released on the clock instead of
    waiting out the no-meter abort. Gated on `!lastSampleFreshAccept` so it NEVER preempts a visible-meter
    green/predictive release (fusion: vision when present, clock when not). Per-type = ANIMATION-LENGTH
    compensation (Go-To's long gather vs quick Standstill each learn their own). Go-To disarm-on-release
    already correct (`processReleasing`/`processCooldown` neutralise RS-up); feedforward also ends the
    Go-To over-hold (releases on the clock vs a late safety). Tests: `feedforwardLearnsFromVisionTimedRelease`,
    `feedforwardReleasesBlindWhenMeterInvisible`, `feedforwardDoesNotPreemptVisibleMeter`. Files:
    `AppConfig.{h,cpp}`, `AutomationEngine.{h,cpp}`, `OrionAppController.cpp` (persist via `feedforwardUpdated`),
    `AutomationEngineTests.cpp`. **NOT committed.** Final precision tuning (gain, fusion thresholds) needs a
    live batch (the clock learns from real shots over a session).
  - **ACCURACY ROADMAP (plan `nifty-mapping-stream.md`, approved): Phases B + C BUILT (ctest 76/76).**
    Scorecard: ~8/13 of the user's checklist already done; the real remaining wins were small.
    **Phase B — latency frame-age:** `effectiveLatency` now folds the capture staleness `shot_.frameAgeMs`
    (clamped to new `RemapConfig::captureAgeLeadCapMs=60`) — the predictor was extrapolating from an
    already-N-ms-old fill without comp; RTT jitter was already in `networkOffsetMs`. Test
    `captureFrameAgeAddsToReleaseLead`. **Phase C — nonlinear top/bounce predictor:**
    `TemporalSampler::predictCrossingMs` now, when the well-fit quadratic is CONCAVE-down and PEAKS below
    target (meter recedes near the top, never reaches target), returns the time to the actual PEAK instead
    of falling back to the LINEAR phantom crossing. Strictly gated (quad adopted + a<0 + peakVal<target),
    so the only behavior change is the recede-caps-below-target case. Exported `TemporalSampler`
    (`ORION_AUTOMATION_API`) for the unit test `predictorReturnsPeakWhenMeterDeceleratesBelowTarget`.
    Files: `AutomationEngine.{h,cpp}`, `AutomationEngineTests.cpp`. **NOT committed.**
    **Phase A (validate/tune live) = user-run batch, gates further tuning. Phase D (chiaki decoder +
    release hardening) deferred.** One lever at a time: B and C each need their own live confirm.
  - **PRODUCTION ARCHITECTURE MILESTONE (deferred, tracked):** a single CUSTOM CHIAKI-NG FORK unblocks
    BOTH (1) the decoder frame feed (true-1080p capture + small UI; GDI is embed-bound ~1550x697) AND
    (2) a PRE-ENCRYPTION INPUT HOOK (Orion writes its computed ControllerState to a named pipe; custom
    Chiaki injects it pre-serialize → removes ViGEm submit + SDL poll jitter, no physical leak, send-timing
    sync). Both blocked on the same patched-chiaki build chain. Sequence: separate branch, AFTER the timing
    loop is live-validated; decoder feed FIRST (read-only/safe), input hook SECOND (read-write/risky); keep
    ViGEm as the validated fallback; never rewrite encrypted UDP. See
    `memory/nexusvision-custom-chiaki-architecture.md`.
  - **ANNEALING calibration + control-path pin + telemetry (2026-06-06, gate GREEN: 78 native, 15 HUD):**
    `learnFromOutcome` now uses an ANNEALING gain (per-type `shotTypeLearnCount` → 0.6 initial decaying to
    0.3 stable; `applyConfig` seeds count=6 for persisted buckets) for fast from-scratch calibration that
    settles — the "severe calibration" fix. Pinned the held-Square+bot contract
    (`heldSquareStaysReleasedAfterBotRelease`). Enriched the `Shot outcome:` line with `ffClockMs=` +
    `learnCount=` (per-shot calibration state). New tests `outcomeGainAnnealsForFastInitialCalibration`,
    `captureFrameAgeAddsToReleaseLead`, `predictorReturnsPeakWhenMeterDeceleratesBelowTarget`,
    `heldSquareStaysReleasedAfterBotRelease`. `outcomeLateRaisesLearnedOffset` updated for the new initial gain.
  - **CURRENT STATE / NEXT:** ALL of the above is in the working tree, **gate-green, UNCOMMITTED**; the
    latest OrionNative (closed loop + feedforward + annealing + frame-age + peak predictor + telemetry +
    DoS removed) builds + was relaunched for the user. **NEXT = PHASE A live batch** (user-run): a recorded
    per-bucket batch at Quality-1080p with the TIMING HUD visible → grade per bucket + tune gains + turn ON
    gradation reading (needs the batch's SLIGHTLY/VERY HUD frames). `settings.json early_late_offset_ms=100`
    (gitignored). Full accuracy state: `memory/nexusvision-hud-closed-loop.md`.
  - **NETWORK-DISRUPTION (DoS) FEATURES — removal in progress:** **Lag All / Lag Opponents REMOVED**
    (source) — deleted `startLagAll/stopLagAll/startLagOpponents/stopLagOpponents`, the
    `NetworkBridge::startLagSwitch/stopLagSwitch/lagMode_/lagging_` + the `lag_switch` command block, the
    h declarations/properties/state, and the two QML buttons. **The "Quit" packet-drop DoS REMAINS and
    BLOCKS the build** — `killSession → setPacketDropping/startPacketDrop → drop_all` targets every
    detected `peer_ips` ("each peer ... whose connection we want to disrupt", NetworkBridge.cpp) = an ICMP
    flood against opponents, NOT a harmless troll. OrionNative will not compile while it's present (build
    correctly blocked). AWAITING user decision to remove it (recommended); a genuinely harmless
    self-only troll could replace it. The passive RTT/telemetry packet OBSERVATION (handleLine) is
    legitimate and stays.

- **Round 19 (Claude): CLOSED-LOOP timing — the bot now reads its own in-game TIMING verdict and
  self-calibrates the per-shot-type offset; gate GREEN** (Python **97 passed** incl. 12 new HUD tests,
  native build clean, ctest **1/1** incl. 4 new outcome tests). Direct user-directed work (not the
  bounded loop), off the question "can't the bot adjust the offset in real time?".
  - **Why:** the bot had NO perception of whether a shot landed EARLY/LATE/EXCELLENT. `learnFromRelease`
    estimated error from `releaseFillPct - targetPct` off the same delayed capture it acts on (blind to
    pipeline latency, wrong-signed for predictive shots, and the ±120ms outlier gate rejects ~all
    predictive releases → dormant). The only ground truth — the in-game TIMING HUD — was never read back.
    So the global `early_late_offset_ms` had to be hand-tuned every session, and it is clamped at ±100
    (`AppConfig.cpp:464`) ~8ms short of the measured Standstill optimum.
  - **New: `hud_feedback.py` HudFeedbackReader** — resolution-independent (live 1550x697 + 1920x1080)
    TIMING-verdict reader. Locates the top-center banner by a relative band, isolates the verdict WORD
    (strips the pill underline bar + shot-clock digits), scale-normalizes it, and classifies by colour
    (red=off / green=on-target, 100% reliable) + template correlation (LATE/EXCELLENT seeded from real
    frames; file-based bank so EARLY/GOOD/VERY/SLIGHTLY drop in as fixtures). `aggregate_verdicts` takes
    the windowed consensus (the banner fades in/out → per-frame OFF transients discarded). Validated on
    the 13:02 batch: colour 15/15, word 15/15 per-shot via aggregation, live frames LATE.
  - **Closed loop wired:** orchestrator reads the HUD each frame → publishes windowed consensus
    `hud_outcome` on the sidecar payload (`autogreen_sidecar.py`) → `RemotePlaySession` parses + emits
    `shotOutcomeReady` → `OrionAppController` → `AutomationEngine::updateShotOutcome`. The engine PAIRS
    the verdict to the matching release (age in [500,3000]ms, consume-once) and `learnFromOutcome` does
    **integral control on the residual** (`learned += 0.35*errorMs`): LATE adds lead until shots center,
    EXCELLENT (residual 0) holds, EARLY removes. Writes `shotTypeLearnedOffsetMs[type]` — which adds ON
    TOP of the global offset in the release lead (`AutomationEngine.cpp:902`), giving **±60 of headroom
    BEYOND the global ±100 clamp**, so the loop solves the clamp ceiling automatically and per-type.
  - **Also fixed:** `saveLearning` was declared but **never called** — the learned offset loaded from
    `learning.json` but was never persisted. Now `learningUpdated → config_.saveLearning` so calibration
    survives a restart. Once a real verdict arrives, `learnFromRelease` defers to the real-outcome
    learner (`outcomeFeedbackActive_`). New `Shot outcome:` telemetry line (seq-paired, space-free
    tokens, `shot=` last).
  - **Files:** `hud_feedback.py` (new), `tests/test_hud_feedback.py` (new, 12) + `tests/fixtures/hud/`
    (templates) + `tests/fixtures/hud_frames/` (4 frames), `remote_play_orchestrator.py`,
    `native_orion/backend/autogreen_sidecar.py`, `native_orion/src/RemotePlaySession.{cpp,h}`,
    `native_orion/src/OrionAppController.cpp`, `native_orion/src/AutomationEngine.{cpp,h}`,
    `native_orion/tests/AutomationEngineTests.cpp` (+4), `scripts/verify_orion.ps1`, `STATUS.md`,
    `TASK.md`. **NOT committed** (per user). `settings.json early_late_offset_ms` left at **100** (the
    loop now fine-tunes the residual per type on top of it).
  - **LIVE_VALIDATION_REQUIRED:** relaunch OrionNative (loads the rebuilt DLLs) and take a batch —
    RECORD the full set (Standstill + fades + Go-To). Confirm: `Shot outcome:` lines appear with the
    right verdict (cross-check the recording HUD), `learnedOffset` climbs for late-leaning types then
    stabilizes as shots turn EXCELLENT, and the per-type offset persists to `learning.json`. Watch for
    over-correction/oscillation (tune the 0.35 gain down if it hunts) and for any EARLY misread (no live
    EARLY/GOOD template yet — add fixtures from the first such frames).

- **Round 18 (Claude): ROOT-CAUSE of "Standstill doesn't time at all" found + fixed; gate GREEN**
  (`verify_orion.ps1`: Python **85 passed**, native build clean, ctest **1/1** in 55.1s). Autonomous
  deep-dive of the 02:13 live batch (`orion_native.log` 07:13–07:16Z + `2026-06-05 02-13-30.mp4`).
  - **Symptom:** held-Square Standstill released INSTANTLY (~150ms into the hold) and untimed. Batch
    proof: of 3 standstills, **2 fired `reactive_target` at fill=100.0 / conf=0.50 / vel=0.0000 / no
    green ~150ms in**, while the synchronized video shows the REAL meter at ~52–65% at that instant
    (seq2/seq3). The 1 good standstill used `green_confirmed`. `conf=0.50` appears ONLY on the two
    broken shots; every other shot is conf 0.82–1.00.
  - **Pipeline trace (the mechanism):** the native (authoritative) engine is NOT fed the raw detector
    output — the sidecar publishes the **Python `RemapEngine._shot.fill_pct/confidence`**
    (`autogreen_sidecar.py:249`), and `_last_release_fusion`/`_last_meter_track` are never assigned so
    the native's fusion/tracking payloads are empty and it falls back to `shot` (`RemotePlaySession.cpp:1550`).
    A **lingering prior-shot meter / weak false-positive** (a real on-screen full meter that isn't
    THIS shot's) is detected at ~100%/conf~0.50 and relayed as a usable at-target sample.
  - **ROOT CAUSE (engine, `AutomationEngine.cpp`):** the non-Go-To **`reactive_target`** path fired on
    ANY fresh at-target meter passing the loose gate (`detection_confidence_percent=50` → gate 0.50;
    phantom conf 0.50 is a knife-edge pass) with **no requirement that the meter ROSE into the target
    this shot** — unlike Go-To, which got freshness/rising protection in Round 11/12. So a meter that is
    already full from the FIRST sample (lingering / false-positive) fires the shot immediately.
  - **FIX (engine, narrow):** added `ShotContext::minFreshFillPct` (lowest fresh fill seen this shot,
    tracked in `updateDetection`) and `RemapConfig::reactiveRiseMarginPct=25`. The reactive path now
    requires `lastSampleFreshAccept && minFreshFillPct <= target - reactiveRiseMarginPct` — i.e. the
    meter was genuinely seen ≥25% below target before an at-target release. A phantom at-target-from-
    frame-one is suppressed; the bot keeps holding for the shot's own rising meter (distinct waiting
    code `phantom_target`). Predictive/green paths unaffected (a real rising meter fires those first).
  - **Tests:** new `phantomAtTargetSuppressesReactiveRelease` (reproduces the live bug: 5 at-target
    phantom samples must NOT release → `phantom_target`; then a real rise from below → `reactive_target`
    fires). Updated `reactiveTargetCrossingReportsReactiveTargetCode` to feed a below-target sample
    first. `contestedNoGreenTimesMeterToTop` unaffected (rises from 40 via predictive).
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/tests/AutomationEngineTests.cpp`,
    `STATUS.md`, `TASK.md`. **NOT committed** (per user). The running OrionNative was closed, so the
    rebuilt `AutomationCore.dll` (with the fix) is ready — just relaunch to test.
  - **STILL OPEN (separate issue, NOT this fix):** Fade/Go-To timing VARIANCE (release-path/green-
    confirmation race + no-green `meter_full` riding high) — diagnosed + instrumented in Round 17, not
    a phantom-fire bug. The Round-17 attribution telemetry is the lever for that next.
  - **LIVE_VALIDATION_REQUIRED:** relaunch and take a Standstill batch — confirm standstills now HOLD
    and time (no instant ~150ms `reactive_target` at fill 100; the holding telemetry shows
    `code=phantom_target` while a lingering/false meter is at-target, then a proper
    `green_confirmed`/`predictive_target` release on the real rising meter).

- **Round 17 (Claude): Go-To release-path ATTRIBUTION instrumentation; gate GREEN**
  (`verify_orion.ps1`: Python **84 passed**, native build clean, ctest **1/1** in 54.7s).
  Instrumentation + diagnostics ONLY — NO behavior change, NO detector/capture/ownership/HidHide
  edits, NO `early_late_offset_ms` change in code. The `+15` sweep
  (`release_correlation_012706_plus15.csv`) showed Go-To timing is **bimodal**, not a fixed lead
  error: seq4/5 `green_confirmed` rode to fill 100 (late), seq6 `predictive_target` fired at 72
  (early). Codex: a global offset can't fix release-path variance — diagnose first, no more global
  tuning. The current `Release issued:` line can't separate the two (seq6 `target=99.6` is
  ambiguous between a green tip and `meter_full`). This pass makes every Go-To release mechanically
  attributable.
  - **Leading hypothesis (this instrument must CONFIRM/REFUTE, not assume):** the split is a race —
    `green_confirmed` fires when `greenTracker_.confirmed()` wins (target = green **tip** ≈ top →
    rides to ~100), else `predictive_target` fires at the reachability floor (target = `meter_full`
    → ~70). Whether the tracker confirms in time is flaky for a fast, late, narrow Go-To window.
  - **Engine (`AutomationEngine.{cpp,h}`):** a read-only path-attribution snapshot on `ShotContext`
    (`greenConfirmedAtRelease`, `targetModeAtRelease`, `greenWidthAtReleasePct`,
    `greenConfirmFillPct`/`greenConfirmMs` = the green-confirm race, `releaseVelocityPctMs`,
    `releaseCrossingEtaMs`, `expectedRiseAtReleasePct`, `withinReachAtRelease`), stamped each
    Holding frame after target/crossing/velocity/withinReach are computed and BEFORE any release
    block, so it is current when `triggerRelease` fires. Recording only — `shouldRelease`/target/
    timing/output untouched.
  - **Telemetry (`OrionAppController.cpp`):** a SEPARATE seq-paired `Release attribution:` line
    (`Release issued:` is byte-for-byte unchanged — `correlate_releases.py` parses it). All values
    single space-free tokens; `shot=<type>` last (per `[[feedback-telemetry-log-shape]]`).
  - **Diagnostics (`correlate_releases.py`):** `_ATTRIBUTION` regex + `attribution_seq` joined by
    seq; **added the `seq` column** (parsed but never written) plus `attr_*` columns; new "Go-To
    release-path attribution" printout (per-shot greenConfirmed/targetMode/fill@confirm/release_fill
    + a confirmed-vs-velocity mean-fill tally).
  - **Tests:** +2 native pins (`greenConfirmedReleaseStampsAttributionSnapshot`,
    `predictiveTargetReleaseStampsMeterFullAttribution`) assert the snapshot matches the path AND
    that the release **code is unchanged** (proves the read-only snapshot didn't re-route the
    release); +1 Python parse test (`tests/test_correlate_releases_parse.py`, wired into
    `verify_orion.ps1`) pins the attribution↔release seq join.
  - **Known-noisy telemetry (NOT a routing issue):** shots 1-3 `possible_routing_leak` is a
    heuristic false-positive when the user keeps holding **physical** Square while Orion cleanly
    clears **virtual** output. `ORION_FORCE_VIRTUAL_NEUTRAL=1` already proved Chiaki obeys ViGEm
    only — no live HidHide/routing problem to chase. Not revisited.
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/src/OrionAppController.cpp`,
    `native_orion/tests/AutomationEngineTests.cpp`, `tools/diagnostics/correlate_releases.py`,
    `tests/test_correlate_releases_parse.py`, `scripts/verify_orion.ps1`, `STATUS.md`, `TASK.md`.
    **NOT committed** (per user).
  - **LIVE_VALIDATION_REQUIRED:** first reset `early_late_offset_ms = 0` in `settings.json` (it is
    `+15` from the sweep) so the batch runs at a neutral offset. Then ONE batch (3 Square fade/
    standstill + 3 strict RS-up Go-To, Arrow2 Purple) →
    `correlate_releases.py --video <mp4> --video-start-utc <Z>`. Read the Go-To attribution block:
    hypothesis CONFIRMED if `green_confirmed` Go-Tos show `greenConfirmed=1 targetMode=green_tip
    release_fill≈100` and `predictive_target` Go-Tos `greenConfirmed=0 targetMode=meter_full
    release_fill≈70`. ONLY then choose the fix (single sub-top target vs cap-tip-and-clamp-full).

- **Round 16 (Claude): controller-ownership instrumentation across the full release pulse;
  gate GREEN** (`verify_orion.ps1`: Python suite **82 passed**, native build clean, ctest
  **1/1** in 53.7s). Instrumentation + diagnostics only — NO detector geometry, NO timing
  policy, NO `early_late_offset_ms` change. The 21:46:51 live run was blocked with two
  indistinguishable symptoms — **intermittent/incomplete bot ownership** and **release lead
  still too late** — and the old telemetry logged only ONE `Release submit` tick (no physical
  button, no source, no persistence), so the two could not be separated. This pass makes each
  shot mechanically classifiable.
  - **Per-tick ownership trace** (`OrionAppController.cpp`): every shot-state transition logs
    `Shot state: <prev> -> <curr> mode= seq= phys_sq= out_sq= src= kind=` (a physical Square
    press with NO `Idle -> Armed` = pass-through / missed arm = "felt manual"); through the
    whole Releasing+Cooldown window it logs `Release tick: seq= state= t_ms= phys_sq= out_sq=
    ok= backend= rs= src= kind=` on the first tick / any phys-or-out Square change / state
    change; and on window end emits `Release ownership: seq= phys_held_all= out_cleared_all=
    max_out_sq= phys_release_t_ms= ticks= dur_ms= backend= src= kind=`. **`out_cleared_all=1`
    proves Orion held output Square at 0 for the WHOLE pulse+cooldown** (any 0 = a real engine
    override bug); `phys_held_all` + `phys_release_t_ms` say whether the USER kept holding.
  - **Codex telemetry-shape requirements honored:** all values are single space-free tokens
    (`telemetryToken` collapses device-label spaces; `holdStateToken`/`shotModeToken` give
    space-free state/mode — `holdStateText`'s "Green Window"/"Pump Fake" are UI-only). The old
    `Release submit:` line is **byte-for-byte unchanged** (`correlate_releases.py` depends on
    it). The accumulator lives in three small helpers (`updateReleaseOwnershipTrace`,
    `flushReleaseOwnershipTrace`, `logShotStateTransition`), not inline in the poll loop. The
    summary is flushed when the shot leaves Releasing/Cooldown AND on pad teardown / virtual
    disconnect, so a fast cooldown can't lose it.
  - **`correlate_releases.py`:** parses the new summary and adds columns
    `ownership_phys_held_all / ownership_out_cleared_all / ownership_phys_release_t_ms /
    ownership_ticks / ownership_dur_ms / ownership_class`. `ownership_class` ∈
    `{engine_override_bug, possible_routing_leak, user_released_physical, clean_owned}` — and
    deliberately does NOT claim a routing leak from the video (only labels `possible_*`; the
    recording confirms). Added an ownership-class tally to the printout.
  - **Files:** `native_orion/src/OrionAppController.{cpp,h}`,
    `tools/diagnostics/correlate_releases.py`, `STATUS.md`. **NOT committed** (per user).
  - **LIVE_VALIDATION_REQUIRED:** ONE instrumented live batch (3 Square fade/standstill + 3
    strict RS-up Go-To), then `correlate_releases.py --video <mp4> --video-start-utc <Z>`.
    Read the ownership tally: all shots `out_cleared_all=1` ⇒ Orion-side ownership is clean →
    if the game still didn't fire while `phys_held_all=1`, that's `possible_routing_leak`
    (Chiaki obeying the physical DualSense) → run the HidHide/unplug isolation test. Any
    `out_cleared_all=0` ⇒ engine override bug. Physical presses with no `Idle -> Armed` ⇒
    missed-arm pass-through. **Only after** ownership proves clean: a SEPARATE run with
    `early_late_offset_ms = 40` to test the late-lead hypothesis (runtime knob, no rebuild).

- **Round 15 (Claude): AutomationEngine timing — gate Square/fade low-fill dumps + jitter-
  proof Go-To RS-up disarm; gate GREEN** (`verify_orion.ps1`: Python suite + native build +
  ctest **62 passed/0 failed** in 53.7s). Engine-only — no detector/capture changes. Off the
  detector-pass live batch (516 fed samples, floatie fixed) the remaining defects were
  timing:
  - **Fix 1 — Square/fade `timeout_fallback` low-fill dumps** (live: seq=2 Left Fade
    timeout_fallback fill=49.0). In `processHolding`, the 650ms tempo timeout AND the 1200ms
    max-hold ceiling now defer while the meter is **recoverable/rising**
    (`recoverableOrRising = meterFresh && (lastSampleFreshAccept || (sampler velocity>0 &&
    fill<target-1))`) and never release below a **min-fill floor**
    (`nonGotoMinReleaseFillPct=70`). The **absolute hard cap** (postMeterCeiling×3) still
    ALWAYS releases (unfloored) — `max_hold_hard_cap` reason text, `max_hold_safety` code —
    so a held shoot never yields nothing. Go-To paths untouched.
  - **Fix 2 — Go-To RS-up "holds too long" / re-arm.** The neutral latch
    `stickUpLatchedUntilNeutral_` was cleared on a raw 1-frame `!stickUp`; a diagonal/
    sideways RS or a 1-frame Remote Play dropout is `!stickUp` but NOT neutral and dropped
    the latch → the still-held RS re-armed a Go-To. Now the latch clears only when the
    PHYSICAL RS is **truly neutral** (`hypot(rsX,rsY) < stickUpThreshold*kStickUnitScale`)
    for **3 consecutive frames** (`rightStickNeutralFrames_`), and while latched-and-
    deflected (any direction) virtual RS is pinned `(0,0)` and re-arm is blocked. Mirrors the
    Square `allFalse(3)` filter.
  - **Files:** `native_orion/src/AutomationEngine.{cpp,h}` (+ `nonGotoMinReleaseFillPct`,
    `rightStickNeutralFrames_`), `native_orion/tests/AutomationEngineTests.cpp`. **+4 native
    tests** (`timeoutDeferredWhileMeterFreshOrRising`, `timeoutDoesNotDumpBelowMinFillFloor`,
    `nonGotoHardCapReleasesEvenBelowFloor`, `goToRsLatchHoldsUntilTrulyNeutral`); **2 updated**
    (`stuckMeterReleasesViaMaxHoldSafety`, `timeoutFallbackReleasesWithDistinctReasonCode` —
    stuck fill 50→80 + `stale_or_memory` so they stay non-recoverable above the new floor).
    **NOT committed** (per user).
  - **LIVE_VALIDATION_REQUIRED:** a fresh PS5 batch (3 Square fades/standstill + 3 strict
    RS-up Go-To, Arrow2 Purple) to confirm far fewer low-fill `timeout_fallback`, Square/fade
    hold-then-release near target (or hard-cap, never a 49% dump), and the Go-To RS-up
    disarms at release and does not re-arm while RS-up stays held. **Follow-up (out of scope
    this pass):** Go-To releasing at high fill (96–100) = release-lead/`earlyLateOffsetMs`
    tuning; Square bot-ownership consistency.

- **Round 14 (Claude): capture-tier hypothesis DEAD; fixed the real bug = floatie
  false-accepts; gate GREEN** (`verify_orion.ps1`: 82 py tests incl. 6 new live-GDI,
  native build, ctest 1/1 in 48.9s). The user ran the `ORION_FRAMEDUMP` batch + tier
  probe: live **GDI capture is clean** (probe shows GDI=live game, screen_region=the
  occluding desktop, PrintWindow=NONE; `Capture health tier=gdi core_static_run=0`, no
  SUSPECT) — so **no tier-selection change** (keep GDI). Evidence-first detector analysis
  of the raw GDI frames (1081-frame batch) overturned the "visible meter rejected"
  framing:
  - The cited `roi_not_found` frames (f00120/170/325) have **NO Arrow2 Purple meter** —
    only the cosmetic **pink floatie** + the yellow/cyan **stamina bar** (unrelated UI) —
    so `roi_not_found` there is CORRECT. Across the batch only ~2 substantial meters were
    missed (both at low/edge positions via pre-existing search-zone gates, not color).
  - The REAL defect: **floatie false-accepts fed to the engine as bogus fill.** The pink
    floatie (median Hue~166, Sat~142, squarish) overlaps the loose Purple mask
    (H[132..167]) and was accepted (fill ~20-29, `green_not_found` → which the live feed
    gate DOES feed), poisoning timing. Ground truth: the real meter is a saturated
    true-magenta HUD bar at **Hue 150 / Sat ~203 / thin-vertical with a green cap**.
  - **Fix (detector-only, Purple/Arrow2-scoped):** a conservative colour-**purity gate**
    in `meter_detector.py` `_color_detect` — reject a Purple candidate whose masked-pixel
    median Hue > 159 OR Sat < 165 (meter is Hue<=151 / Sat>=184, so 8-19px margin →
    robust to lighting/compression). Plus SOFT, never-required boosters (tight-magenta
    purity bonus + green-cap-above via `_green.green_span_abs`). New config fields
    `purple_purity_*`. Also fixed `tools/diagnostics/debug_detect_frame.py` to actually
    `set_active_style("Arrow2")` (+ sys.path/survey-vs-settled).
  - **A/B on the batch (identical 'fed' metric):** true-meter feeds **140→140 (delta 0,
    no real meter lost)**; floatie false-feeds **36→5**, and those 5 residual are
    themselves H150/S~200 = real low-fill meters the strict ground-truth mislabeled — i.e.
    **every genuine floatie false-feed eliminated, zero true-meter loss.**
  - **Tests:** new `tests/test_meter_detector_live_gdi.py` (3 positive on-meter fixtures
    across fill + 2 floatie false-box + 1 no-meter, raw GDI 1550x697) wired into
    `verify_orion.ps1`; existing detector suite unregressed. **NOT committed** (per user).
  - **User ground truth recorded:** meter style/color is fixed by UI (Arrow2 Purple is
    authoritative; detector must not auto-switch); detector must ignore stamina
    bar/floatie/jersey/body/ball/reflections; if no Arrow2 Purple meter exists,
    `roi_not_found` is correct. **Out of scope this pass (flagged for a later round):**
    Go-To holding RS-up too long (should disarm/neutral immediately on release) and
    inconsistent bot ownership on Square holds — both AutomationEngine/timing.
  - **LIVE_VALIDATION_REQUIRED:** a fresh PS5 batch should confirm the floatie no longer
    feeds the engine and real shots still detect/time. THEN revisit Go-To over-hold +
    Square ownership.

- **Round 13 (Claude): capture-freeze DIAGNOSTICS — no behavior change; gate GREEN**
  (76 py tests, native build, ctest 1/1). Root-cause framing from 12:53: capture is NOT
  frozen (`uniqfps=44-60` during `roi_not_found` runs) yet the detector fails live while
  the OBS recording of the same screen detects fine — so the live-captured CONTENT differs
  from the true screen. Live path is **window capture** (GDI BitBlt -> PrintWindow
  `PW_RENDERFULLCONTENT` -> desktop screen-region); `_frame_is_probably_black` and
  `_frame_hash` are WHOLE-frame, so a stale/black video region can pass while the frame
  still "changes." Added (diagnostics only, `remote_play_orchestrator.py`):
  `_capture_window_tier` (per-tier attribution; wrapper keeps `_capture_window` contract),
  `_video_region_status` (video-CORE black + core-only change hash), and a throttled
  **"Capture health: tier=… core_black_run=… core_static_run=… SUSPECT"** log. New
  `tools/diagnostics/capture_tier_probe.py` compares the 3 tiers side-by-side with NO
  gameplay. +5 tests. **The fix (prefer/validate the composited screen-region tier when
  on-screen) is NOT applied — it's contingent on the probe/log confirming WHICH tier
  produces the dead video.** Allowed-files honored (no detector/timing/native edits).
- **Round 12 (Claude): Go-To releases ONLY predictively now; gate GREEN** (71 py,
  native build, ctest 1/1 in 48.8s). 12:53 batch (`release_correlation_1253.csv` +
  recording `12-53-02.mp4`) confirmed: strict arming held (no dribble hijack), the stale
  phantom-100 is reduced, but Go-To still released LATE via `reactive_target` at 90-100%
  fill (10/17 Go-Tos) and unsafely via `max_hold_safety` at dropout fills (2/17). Fix:
  for Go-To, `reactive_target` (fill>=target) and `max_hold_safety` are DISABLED — a
  Go-To releases ONLY via green/predictive/ETA (blocks 1-3, which fire BEFORE target) and
  ABORTS neutral at the absolute hard cap if no timeable crossing fired. ViGEm still
  healthy (all `ok=1 sq=0`). +3 tests (`gotoDoesNotReactiveReleaseAtTarget`,
  `gotoDoesNotReleaseViaMaxHold`, `gotoReleasesViaPredictiveCrossing`);
  `gotoLowFillSuppressesMaxHoldDump` assertion updated (await_meter->no-max_hold).
- **CAVEAT (surfaced to user):** with reactive/max_hold off, Go-To is now
  "release-correctly-or-abort." While the live CAPTURE keeps dropping mid-shot (the same
  freeze behind the fade `timeout_fallback`, see [[nexusvision-capture-freeze-rootcause]]),
  the predictive paths often won't get a clean crossing -> EXPECT MORE Go-To aborts (no
  shot) rather than late misses. The capture-freeze fix is now the gating blocker for
  Go-To success; fades remain deferred.
- **Round 11 (Claude) shipped the Go-To freshness gate + strict arming; gate GREEN**
  (`verify_orion.ps1`: 71 py tests, native build, ctest 1/1 in 38.9s). The 11:58 batch
  confirmed Fix #1 (ViGEm) healthy (36/36 `ok=1 square_bit=0`) but Go-To reactive-released
  a phantom fill=100 from a held/extrapolated sidecar sample while the real meter was
  `roi_not_found`. Fix:
  - **Layer 2 (propagate):** orchestrator publishes `raw_fed` (=`_should_feed_engine`,
    false on meter_memory/roi_not_found) on the sidecar payload; `RemotePlaySession`
    marks such samples `rejectionReason=stale_or_memory`.
  - **Layer 1 (engine):** `updateDetection` only advances the sampler / green tracker /
    `sawFreshMeterThisShot` on a FRESH raw accept; a SINGLE Go-To freshness gate after
    ALL release blocks (green_confirmed/predictive/reactive/max_hold) suppresses any
    Go-To release unless `lastSampleFreshAccept && fill>=gotoMinReleaseFillPct`, and the
    hard-cap abort is generalized to "never earned fresh trust." Strict arming: Go-To
    arms only on vertical-dominant RS-up (`gotoLateralMaxRatio`) with no Square
    held/owned, no LS/R2/L2 move/post context, sustained (not a flick).
  - **DESIGN NOTE / deviation from the approved plan:** the trust signal is the
    per-sample FRESH-ACCEPT flag, NOT a rise-magnitude threshold. A rise gate
    (`gotoMinRiseForReleasePct`) would have blocked a legitimate Go-To that surfaces near
    the top in one clean frame (and broke `rightStickUpHoldOwnsGoTo`). Fresh-accept is the
    correct discriminator (a held phantom is `stale_or_memory` at any fill; a real meter
    is not) and still satisfies both refinements (all paths gated; green is shot-local +
    fresh-only via the update gate + `beginShot` reset). `gotoMinRiseForReleasePct` was
    dropped; `gotoMinReleaseFillPct` (55) floor retained.
  - +8 native tests (stale-100 / stale-green / fresh-green-releases / diagonal / square /
    LS / R2+L2 / flick). Existing Go-To tests still pass unchanged.
- Active task is now **T1b - Live release output + Go-To + detector dropouts** (see
  `TASK.md`; T1's offline phase moved to Done). Driven directly by the user (not the
  bounded loop) off the 2026-06-04 10:28 batch video + log.
- **Round 10 (Claude) shipped, gate GREEN** (`verify_orion.ps1`: 71 py tests, native
  build, ctest 1/1 in 27s):
  - **Step 0** `tools/diagnostics/correlate_releases.py`: correlates each "Release
    issued" to the recording's REAL meter (cropped stream ROI 452,232,1169,658;
    `--video-start-utc 2026-06-04T15:28:09Z`). CSV at
    `logs/diagnostics/release_correlation.csv`.
  - **Step 1 (Fix #1)** ViGEm submit verification: `ShotContext::releaseSeq` (stamped
    in `triggerRelease`) echoed in "Release issued", paired with a new "Release submit:
    seq= ok= backend= square_bit= rs=" line logged at the actual `controller_.submit()`
    in `pollPhysicalController` (incl. a `NOT_SUBMITTED virtual_disconnected` case).
    Proves whether a logged release reaches the pad ("no interaction" mode).
  - **Step 2 (Fix #2)** Go-To `gotoMinReleaseFillPct=55`: a Go-To below the floor never
    takes a `max_hold_safety` dump (the 13-42% live dumps); it keeps holding for a
    fresh/recovered meter, and ABORTS (no blind shot) at the absolute hard cap. +3
    native tests (`gotoLowFillSuppressesMaxHoldDump`, `gotoRecoveredMeterReleasesNotMaxHold`,
    `gotoNeverRecoversAbortsAtHardCap`).
  - **Step 3 (Fix #3) — evidence says DO NOT change the detector.** Offline replay of
    the batch video shows detection is CONTINUOUS once the meter appears, identical at
    1169x658 and upscaled 1550x697 (resolution-independent). The live failures are
    multi-second (up to 4.8s) mid-shot `roi_not_found` runs that occur ONLY live; the
    release-time correlation proves it (engine read 13-27% frozen while the recording's
    real meter was 92-100% at that instant). **Root cause = the live 1550x697 GL capture
    freezing / feeding stale frames mid-shot — a capture-pipeline issue, NOT detector
    geometry.** No `meter_detector.py` change made (it would not fix this and risks
    regressing the working offline detection). Fix #2 mitigates the Go-To consequence.
- **Restored** `native_orion/backend/ps5_remoteplay_helper.py`'s shebang/docstring safety
  header (out-of-scope deletion flagged in Round 9); diff vs baseline now empty.
- **LIVE_VALIDATION_REQUIRED: yes** — a fresh PS5 batch is needed to (a) read the new
  `Release submit:` lines on the "no interaction" standstills (does the cleared-Square
  write reach the pad?), (b) confirm Go-To no longer low-dumps, and (c) decide the
  capture-freeze fix. Then, with capture stable, tune `early_late_offset_ms`.
- **NOT committed** (per instruction).

- Active task: **T1 - Stabilize live shot timing and verification wiring** (see
  `TASK.md`).
- This round (Round 9 / Claude) **converges the offline phase and flags live
  validation.** No source/test change this round. Verified by inspection that every
  offline T1 do-item is complete: do #1 (`test_stability_tracking.py` is in
  `verify_orion.ps1`'s pytest call, confirmed in the diff); do #2 *mechanism* (the
  reachability cap `maxPredictiveRisePct=25` / `reachabilitySlackPct=6` is wired and
  `fastMeterCapsPredictiveReleaseNearTop` asserts a fast meter releases near the top,
  not ~57% low); do #3 (ownership: `stickInputsPassThroughWithoutAutomation`,
  `rightStickUpHoldOwnsGoTo`, `leftStickAloneNeverTriggersRelease`,
  `tapAndFlickPassThrough`, `squareHoldUsesOnlyVirtualSquare`); do #4/#5 (every
  REACHABLE release/waiting reason code is pinned; `fixed_fallback` re-confirmed
  unreachable dead code — `holdReleaseStrategy` defaults to `"hybrid"`
  (AutomationEngine.h:26), read once (AutomationEngine.cpp:1037), never assigned). The
  **only** remaining T1 work is do #2's CORRECTNESS — whether 25/6 actually close the
  live standstill-releases-low vs fade-greens spread — which is an empirical tuning
  question answerable **only by live PS5/Remote Play gameplay**. Per `TASK.md`
  ("If progress requires live PS5/Remote Play gameplay, set `LIVE_VALIDATION_REQUIRED:
  yes` and stop") this round sets `LIVE_VALIDATION_REQUIRED: yes` and `STOP_REQUESTED:
  yes`. The loop runs its authoritative gate this round BEFORE the stop-token check
  (`agent-loop.ps1` Test-StopConditions evaluates the verify outcome at #5, the live
  flag at #6), so a native build break is still caught first and this flag cannot mask
  it.
- **Out-of-scope diff flagged for human review (NOT touched this round):**
  `native_orion/backend/ps5_remoteplay_helper.py` carries a pre-existing working-tree
  modification (8-line header/docstring removal) that is **outside T1's allowed file
  set** and predates this round (it was already `M` at session start). It is not a
  protected file and the total changed-file count is under 20, so neither the loop's
  protected-file nor >20-file guard flags it. A human should confirm whether that
  docstring removal is intended before the diff is committed.
- Prior round (Round 8 / Claude) pinned the LAST reachable release code that still
  lacked a dedicated reason-code test: `reactive_target` (the last-resort "already
  at/past target" path). It was previously only ACCEPTED as one of several allowed codes
  (`contestedNoGreenTimesMeterToTop`, `holdingFramesReportHonestWaitingCode`), never
  pinned on its own — the same gap `green_confirmed` had before Round 6. With this,
  **every reachable release/waiting code now has a dedicated pin**: `green_confirmed`,
  `predictive_target`, `reactive_target`, `max_hold_safety`, `await_meter`,
  `stale_sample`, `out_of_reach`, `confidence_low`, and `timeout_fallback`. **Finding
  (answers Round 7's open question):** the only remaining unpinned code,
  `fixed_fallback`, is **unreachable dead code** — `config_.holdReleaseStrategy`
  (AutomationEngine.h:26) defaults to `"hybrid"`, is read in exactly one place (the
  `fixed_fallback` block, AutomationEngine.cpp:1037), and is **never assigned** from
  `AppConfigData` or any setter, so the block cannot fire. It therefore cannot be
  pinned test-only; wiring it through config or removing it is a deliberate
  behavior/scope change for a FUTURE Active Task, not this conservative pass.
  **Test-only this round: NO change to any source file or runtime behavior** — the
  sole `native_orion` edit is `AutomationEngineTests.cpp`.
- Offline coverage for do-items #4/#5 is now complete; the remaining open T1 question
  is do-item #2's CORRECTNESS — whether the reachability-cap values
  (`maxPredictiveRisePct=25` / `reachabilitySlackPct=6`) actually close the live
  standstill-releases-low vs fade-greens spread — which can only be decided by live
  PS5 gameplay. The next agent should flag `LIVE_VALIDATION_REQUIRED: yes` once the
  loop's gate confirms the accumulated test additions build and pass.
- Prior round (Round 7 / Claude) pinned the one remaining UNLABELLED release path: the
  tempo-timeout safety release (`timeout_fallback`). Of the release/waiting reason
  codes, `green_confirmed`, `predictive_target`, `reactive_target`,
  `max_hold_safety`, `await_meter`, `stale_sample`, `out_of_reach`, and
  `confidence_low` each had a dedicated pin; the tempo-timeout safety — the path a
  stuck button shot falls through to after `tempoFallbackTimeoutMs` — did not. The
  earlier reachability-cap + meter-gating timing changes alter WHICH path a stuck
  shot reaches, so pinning the label guards against a regression that silently
  re-routes it (do #4 honest distinct release reasons / do #5 tests for changed
  timing logic). **Test-only: NO change to any source file or runtime behavior** —
  the sole `native_orion` edit is `AutomationEngineTests.cpp`.
- Prior round (Round 6 / Claude) closed the last coverage gap in do-item #4's enumerated
  distinguish list: of `green_confirmed` / `predictive_target` / `max_hold_safety`
  / `waiting_for_meter` / stale states, every code had a dedicated pinning test
  EXCEPT `green_confirmed` — the BEST release path, listed first in do #4.
  `replayedRisingTrajectoryReleasesInsideWindow` only accepted it as one of three
  allowed codes. Added a focused, deterministic native test that confirms a green
  window (constant below-target fill so no predictive/reactive path can fire), then
  drives the fill to the target so the confirmed-green crossing path (block 1)
  fires and reports `green_confirmed`. **Test-only: no source/behavior change** —
  the only file touched in `native_orion` is `AutomationEngineTests.cpp`.
- Prior round (Round 5) closed a do-item #4 telemetry conflation: the
  stale-sample waiting branch had a distinct machine code (`stale_sample`) but its
  human-readable `releaseReason` text was byte-for-byte identical to the
  never-saw-a-meter `await_meter` path (`waiting_for_meter`). The stale branch now
  emits its own reason text (`meter_sample_stale` / `goto_meter_sample_stale`) so the
  `reason=` field can tell "meter went stale mid-shot" apart from "meter never
  appeared." Telemetry-only — no `shouldRelease`/timing/target/output change. Added a
  focused native test.
- Prior round (Round 4) extended do-item #4 to cover the reachability cap
  (do-item #2): a fresh, confident meter held back BELOW target by the reachability
  cap reports the distinct waiting code `out_of_reach` instead of the generic
  `eta_not_ready`. Telemetry-only. Added a focused native test.
- Prior round (Round 3) implemented do-item #4's waiting branches: the waiting
  branches in `processHolding` overwrite the stale predictive/green release code
  with a waiting code, so a HOLDING frame can no longer falsely report a release
  path.
- Supervisor authorization (Round 2) remains in effect: `TASK.md` has
  `GAMEPLAY_CHANGES: yes` and allows `AutomationEngine.cpp`, `AutomationEngine.h`,
  and `AutomationEngineTests.cpp` for this pass.
- User authorized up to 10 bounded rounds. The loop still stops for failed gates,
  more than 20 changed files, protected files, deleted tracked files, or live
  validation.

---

## Round History

### Round 13 - Capture-freeze diagnostics (Claude, no behavior change)

```
DECISION: needs-changes
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: yes
```

- **Files:** `remote_play_orchestrator.py` (diagnostics), new
  `tools/diagnostics/capture_tier_probe.py`, `tests/test_orchestrator_capture.py`,
  `STATUS.md`. No detector/timing/native edits (per the approved safeguards).
- **Gate:** `verify_orion.ps1` GREEN — 76 py tests, native build, ctest 1/1 (48.7s).
- **Why:** with all Go-To safety releases off, capture/feed reliability is THE blocker.
  12:53 evidence: `uniqfps=44-60` during `roi_not_found` runs (capture not frozen) +
  offline recording detects the same content -> live-captured content ≠ true screen.
- **Added (diagnostics only):** per-tier attribution (`_capture_window_tier`), video-CORE
  liveness (`_video_region_status` — black + core-only hash, derived proportionally from
  the frame, NOT a hardcoded crop), and a throttled "Capture health" log that flags
  SUSPECT on (core black run) or (whole-frame-changing-while-core-static run) — never on a
  merely-static idle scene. New `capture_tier_probe.py` saves GDI/PrintWindow/screen-region
  side-by-side (no gameplay). +5 tests.
- **NOT done (contingent):** the tier preference/validation FIX. Next: run the probe (or a
  short instrumented capture and read the "Capture health … SUSPECT" lines) to confirm
  WHICH tier yields the dead/stale video; then prefer/validate the composited screen-region
  tier ONLY when the window is on-screen/un-occluded. Then a live gameplay batch.

### Round 12 - T1b Go-To predictive-only release (Claude)

```
DECISION: needs-changes
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: yes
```

- **Files:** `native_orion/src/AutomationEngine.cpp`,
  `native_orion/tests/AutomationEngineTests.cpp`, `STATUS.md`.
- **Gate:** `verify_orion.ps1` GREEN — 71 py, native build, ctest 1/1 (48.8s).
  (First run failed `LNK1104` because the 12:53 OrionNative (PID 8292) still held
  AutomationCore.dll; stopped it and rebuilt clean.)
- **Problem (12:53 batch, Codex + confirmed):** Go-To released LATE via `reactive_target`
  at 90-100% fill (10/17; e.g. fill 100 while green was 42-80%) and unsafely via
  `max_hold_safety` on dropout fills (2/17). Only 5/17 used the good predictive/green
  paths. ViGEm healthy.
- **Fix:** for Go-To, `reactive_target` and `max_hold_safety` are disabled (`!isGoto`);
  the hard-cap abort is generalized to fire whenever a Go-To reaches the cap without a
  predictive/green/ETA release. Go-To now releases only BEFORE target or aborts neutral.
- **Live-test next:** Go-To should release earlier (predictive/green) or abort — never a
  late reactive dump. Because the capture still drops mid-shot, expect more aborts than
  greens until the capture-freeze is fixed (that is now the Go-To blocker).

### Round 11 - T1b Go-To freshness gate + strict arming (Claude)

```
DECISION: needs-changes
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: yes
```

- **Files:** `remote_play_orchestrator.py`, `native_orion/backend/autogreen_sidecar.py`,
  `native_orion/src/RemotePlaySession.cpp`, `native_orion/src/AutomationEngine.{cpp,h}`,
  `native_orion/tests/AutomationEngineTests.cpp`, `TASK.md`, `STATUS.md`.
- **Gate:** `verify_orion.ps1` GREEN — 71 py tests, native build, ctest 1/1 (38.9s).
- **Problem (11:58 batch):** Go-To `reactive_target` fired at engine fill=100 while the
  real meter was `roi_not_found` (seq=14/15/16) — the sidecar fusion holds/extrapolates a
  confident fill when raw detection drops but frames keep flowing.
- **Fix:** Layer 2 propagates `raw_fed` -> native `stale_or_memory`; Layer 1 only grants
  freshness trust to a clean raw accept and gates EVERY Go-To release path through one
  suppression point (+ generalized hard-cap abort). Strict arming blocks
  diagonal/Square/LS/R2/L2/flick from arming Go-To so dribbles aren't hijacked.
- **Deviation:** used the per-sample fresh-accept flag as the trust signal instead of the
  planned rise-magnitude threshold (`gotoMinRiseForReleasePct`, dropped) — the rise gate
  would have blocked a legit one-frame top-of-meter Go-To. Both plan refinements still
  hold (all paths gated; green shot-local + fresh-only).
- **Live-test next:** confirm no Go-To releases at a stale/held 100 (they `await_meter` ->
  real release or clean abort), dribbles (RS flick, LS+RS, R2+RS) are not hijacked,
  diagonal RS-up doesn't arm, and a real Go-To still greens.

### Round 10 - T1b live release output + Go-To + detector evidence (Claude)

```
DECISION: needs-changes
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: yes
```

- **Agent / round:** Claude, implementer (user-directed off the 10:28 batch).
- **Files changed:** `tools/diagnostics/correlate_releases.py` (new),
  `native_orion/src/AutomationEngine.{cpp,h}`, `native_orion/src/OrionAppController.{cpp,h}`,
  `native_orion/tests/AutomationEngineTests.cpp`, `scripts/verify_orion.ps1`,
  `native_orion/backend/ps5_remoteplay_helper.py` (header restored), `TASK.md`, `STATUS.md`.
- **Gate:** `verify_orion.ps1` GREEN — 71 Python tests, native build, ctest 1/1 (27.2s)
  including the 3 new Go-To tests.
- **Key evidence (10:28 batch, `release_correlation.csv` + `detframes.csv`):**
  - Fade `timeout_fallback` (n=8, fill 14-51%) and Go-To `max_hold_safety` (fill
    13-42%) are DROPOUTS: the engine froze low while the recording's real meter peaked
    75-100% (e.g. log 13.4% vs real 92.3%).
  - `detframes.csv`: 61% `roi_not_found` live, with mid-shot dropout runs up to 4.8s
    (278 frames) starting at 9-38% fill. Offline replay of the same video has NO such
    mid-shot runs (continuous once the meter appears), identical at 1169x658 and
    1550x697 -> the live dropout is a CAPTURE freeze, not detector geometry.
  - Standstill `green_confirmed` log fill (65-76%) ~ matches the real meter once the
    recording's ~0.3-0.5s drifting clock offset is applied -> NOT a fill-reading error;
    Fix #1's submit logging is the decisive next probe for the "no interaction" mode.
- **Decision:** Fixes #1 (submit verification) and #2 (Go-To floor) shipped + tested.
  Fix #3 (detector) intentionally NOT made — evidence shows the detector is fine and the
  cause is the live capture. Stop for a fresh live PS5 batch.

### Round 9 - T1 convergence + live-validation flag (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: yes
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `STATUS.md`, `AGENT_DIALOGUE.md` only. **No source or test
  change this round** — `native_orion/**` is untouched.
- **Step taken:** converged the offline phase of T1 and flagged live validation. The
  prior six rounds (3-8) progressively pinned every reachable release/waiting reason
  code; Round 8 itself concluded offline coverage was complete and explicitly handed
  off "next agent should flag `LIVE_VALIDATION_REQUIRED: yes`." This round verifies
  that conclusion against the actual tree (not just the narrative) and acts on it.
- **Verification by inspection (offline T1 do-items complete):**
  - **do #1** — `scripts/verify_orion.ps1` pytest call includes
    `tests\test_stability_tracking.py` (confirmed in `git diff`).
  - **do #2 mechanism** — `AutomationEngine.cpp:923-925` clamps the predictive rise to
    `config_.maxPredictiveRisePct` and gates `withinReach` by
    `+ config_.reachabilitySlackPct`; `AutomationEngine.h` declares
    `maxPredictiveRisePct=25.0` / `reachabilitySlackPct=6.0`;
    `fastMeterCapsPredictiveReleaseNearTop` (test line 821) asserts a fast meter
    releases within the ceiling of the top (`>= 100 - ceiling - 12`, `< 100`), not
    ~57% low. The CORRECTNESS of 25/6 is the only open item.
  - **do #3** — ownership behavior is covered:
    `stickInputsPassThroughWithoutAutomation`, `rightStickUpHoldOwnsGoTo`,
    `leftStickAloneNeverTriggersRelease`, `tapAndFlickPassThrough`,
    `squareHoldUsesOnlyVirtualSquare`.
  - **do #4 / #5** — every reachable release/waiting reason code has a dedicated pin
    (`green_confirmed`, `predictive_target`, `reactive_target`, `max_hold_safety`,
    `await_meter`, `stale_sample`, `out_of_reach`, `confidence_low`,
    `timeout_fallback`). `fixed_fallback` re-confirmed unreachable dead code:
    `holdReleaseStrategy` defaults to `"hybrid"` (AutomationEngine.h:26), is read in
    exactly one place (AutomationEngine.cpp:1037), and is never assigned (grep of
    `native_orion` returns only the declaration + the single read).
- **Why stop now (not another pin):** there are no more reachable reason codes to pin
  and no changed timing logic left to cover offline; another pin would be make-work.
  The remaining T1 question — do #2's CORRECTNESS, whether 25/6 close the live
  standstill-low vs fade-green release spread — is an empirical tuning question that
  only live PS5/Remote Play gameplay can answer. Per `TASK.md` ("If progress requires
  live PS5/Remote Play gameplay, set `LIVE_VALIDATION_REQUIRED: yes` ... and stop"),
  this round flags it and halts.
- **Out-of-scope diff flagged (NOT touched):**
  `native_orion/backend/ps5_remoteplay_helper.py` has a pre-existing working-tree
  modification (8-line header/docstring removal) outside T1's allowed file set; it was
  already `M` at session start. Not a protected file and total changed files < 20, so
  the loop's guards won't flag it. A human should confirm the docstring removal is
  intended before committing.
- **Tests run by Claude:** none executable in-sandbox — same persistent limitation as
  Rounds 1/3-8. The native build (`cmake`/`ctest`) and `verify_orion.ps1` require
  interactive approval here, and `C:\Python314\python.exe` lives outside the session's
  allowed working directory (a `Test-Path` on it is blocked). Deferred to the loop's
  authoritative gate, which runs THIS round before the stop-token check.
- **Remaining blockers:** the loop's gate must build the accumulated
  `AutomationEngine.cpp`/`.h` changes + 430 lines of new tests green. If it does, the
  loop halts on `LIVE_VALIDATION_REQUIRED: yes` for the human; if the native build
  breaks, the loop halts on the unclear-build-break condition first (it is checked
  ahead of the live flag), which is the correct precedence.
- **Live-test next (for the human):** run live PS5/Remote Play and check whether the
  reachability-cap values `maxPredictiveRisePct=25` / `reachabilitySlackPct=6` close
  the per-shot-type spread — fast standstill shots should now release near the top
  (green) instead of ~57% low, while slower fade/go-to shots should still release
  before going late/over-green. The `out_of_reach` / `below_reachability_cap`
  telemetry code surfaces when the cap is actively holding a fast meter back; tune the
  two values UP toward 45/12 if shots start landing late/over-green, DOWN if fast
  shots still land short.

### Round 8 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`,
  `AGENT_DIALOGUE.md`.
- **Change made (do-item #4 / #5, coverage):** Pinned the LAST reachable release
  code without a dedicated reason-code test: `reactive_target` (release block 4,
  AutomationEngine.cpp:962 — the last-resort path that fires when the meter is
  already at/past target with no confirmed green window and no usable
  velocity/ETA prediction). It was previously only ACCEPTED as one of several allowed
  codes in `contestedNoGreenTimesMeterToTop` (line 813) and excluded as a release code
  in `holdingFramesReportHonestWaitingCode` (line 899), but never asserted on its own
  — the identical gap `green_confirmed` had before Round 6. Added
  `reactiveTargetCrossingReportsReactiveTargetCode`. **Test-only: NO change to any
  source file or runtime behavior** — the sole `native_orion` source edit is the test
  file.
- **Test added:** `AutomationEngineTests::reactiveTargetCrossingReportsReactiveTargetCode`
  arms a square shot to Holding (no meter yet -> `await_meter`), waits past the ~140ms
  standstill commit floor, then feeds ONE fresh contested sample already at the top
  (fill 100, green window nulled -> target = full meter, velocity 0, ETA -1). Because
  the green tracker never confirms (block 1 out), `fill < target` fails (block 2 out),
  and ETA is -1 (block 3 out), the reactive "already at/over target" path (block 4) is
  the only reachable release route, so the release frame must report
  `releaseReasonCode == reactive_target` (and `!= predictive_target`). The
  timeout/fixed/max-hold safeties are pushed to 5000ms so none can pre-empt the
  at-target frame. Deterministic: block 4 has no `withinReach`/`developing` gate, so a
  fresh at-target sample fires it on the first post-commit frame.
- **Finding (answers Round 7's open question on `fixed_fallback`):** `fixed_fallback`
  is the only release code still without a dedicated pin, but it is **unreachable dead
  code** in the current build. `config_.holdReleaseStrategy` (AutomationEngine.h:26)
  defaults to `"hybrid"` and is **never assigned anywhere** — not from `AppConfigData`
  (which has no such field) and not via any setter — while the `fixed_fallback` block
  (AutomationEngine.cpp:1036-1042) only fires when it equals `"fixed"`. So the path
  cannot fire and cannot be pinned with a test-only change. Wiring `holdReleaseStrategy`
  through config (to enable it) or removing the dead block are both deliberate
  behavior/scope changes that belong in a FUTURE Active Task, not this conservative
  test-only pass. With that documented, **every REACHABLE release/waiting code now has
  a dedicated pin** and do-items #4/#5 offline coverage is complete.
- **Scope/regression check (by inspection):** Only `AutomationEngineTests.cpp` adds
  one slot declaration and one test body; no production code, no existing test, and no
  `releaseReasonCode` string changed. Every symbol/field the new test uses
  (`readyMeterSample`, `DetectionResult` green/velocity/eta fields,
  `AppConfigData::{remotePlayInputSource,minimumHoldMs,tempoFallbackTimeoutMs,fixedHoldMs,maximumHoldMs}`,
  `HoldState::{Releasing,Cooldown}`, `engine.context().releaseReasonCode`) is already
  used by the sibling `timeoutFallbackReleasesWithDistinctReasonCode` test, so it adds
  no new dependency.
- **Tests run by Claude:** none executable in-sandbox. The native build
  (`cmake --build ... OrionNativeTests`), `ctest`, and `verify_orion.ps1` all require
  interactive approval here and the Qt/OpenCV toolchain lives outside the session's
  allowed working directory — same limitation as Rounds 1, 3, 4, 5, 6, and 7. The
  build was refused by the sandbox this round; deferred to the loop's authoritative
  gate.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python subset +
  native build + native ctest) to confirm the new test plus the in-flight
  reachability-cap + telemetry changes build and pass. With offline coverage now
  complete, the next substantive step (do #2 correctness of the 25/6 reachability-cap
  values) requires live PS5 gameplay.
- **Live-test next:** none from this test-only change. Once the gate is green and
  Codex approves the in-flight diff, the open gameplay question — whether the
  reachability-cap values close the standstill-low / fade-green release spread —
  needs live PS5 confirmation; the next agent should set `LIVE_VALIDATION_REQUIRED:
  yes` at that point.

### Round 7 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`,
  `AGENT_DIALOGUE.md`.
- **Change made (do-item #4 / #5, coverage):** The release-reason telemetry has a
  dedicated pinning test for every release/waiting code EXCEPT the tempo-timeout
  safety release `timeout_fallback`. `green_confirmed`
  (`greenConfirmedCrossingReportsGreenConfirmedCode`), `predictive_target`
  (`standstillTimedReleaseSetsPredictiveReasonCode`,
  `fastMeterCapsPredictiveReleaseNearTop`), `max_hold_safety`
  (`stuckMeterReleasesViaMaxHoldSafety`), `await_meter`
  (`noMeterAbortsInsteadOfBlindRelease`, `goToWaitsForLateMeterThenTimes`),
  `out_of_reach` (`holdingReportsOutOfReachWhenMeterBelowCap`), and `stale_sample`
  (`holdingStaleSampleReportsDistinctReason`) all have one; `timeout_fallback` — the
  path a stuck button shot reaches after holding past `tempoFallbackTimeoutMs` — did
  not. Added `timeoutFallbackReleasesWithDistinctReasonCode`. **Test-only: NO change
  to any source file or runtime behavior** — the sole `native_orion` source edit is
  the test file.
- **Test added:** `AutomationEngineTests::timeoutFallbackReleasesWithDistinctReasonCode`
  arms a square shot to Holding, gets past the 140ms standstill commit floor, then
  feeds a stuck contested meter (fill 50, no green -> target 100, velocity 0, ETA -1)
  so blocks 1-4 cannot fire. With `tempoFallbackTimeoutMs=300` and
  `maximumHoldMs=5000`, it asserts the shot keeps Holding at elapsed ~160ms (timeout
  not yet due AND the meter is still "developing"), then after a 450ms wait — past
  BOTH the 300ms tempo timeout AND the ~400ms peak-progress grace (re-feeding fill 50
  doesn't advance the peak, so the meter is no longer developing) — the tempo-timeout
  safety fires and reports `releaseReasonCode == timeout_fallback`, `!= max_hold_safety`
  (whose 5000ms post-meter ceiling is far off). Deterministic: the timeout is the only
  reachable release route, isolated from the predictive paths and from max-hold.
- **Scope/regression check (by inspection):** Only `AutomationEngineTests.cpp` adds
  one slot declaration and one test body; no production code, no existing test, and
  no `releaseReasonCode` string changed. `holdReleaseStrategy` defaults to `hybrid`
  (not `fixed`) so the `fixed_fallback` block cannot pre-empt; and even if it were
  `fixed`, `fixedHoldMs`(650) > the ~610ms elapsed at release, so the timeout
  (elapsed>=300) still fires first. The stuck-sample helper mirrors
  `stuckMeterReleasesViaMaxHoldSafety` (which itself relies on default non-`fixed`
  strategy), differing only in `tempoFallbackTimeoutMs`/`maximumHoldMs` so the
  tempo-timeout path is reached before the max-hold ceiling.
- **Tests run by Claude:** none executable in-sandbox. `scripts\verify_orion.ps1`
  (Python subset + native build + native ctest), a direct `cmake --build`, and `ctest`
  all require interactive approval here and the Qt/OpenCV toolchain lives outside the
  session's allowed working directory — same limitation as Rounds 1, 3, 4, 5, and 6.
  The gate was refused by the sandbox this round; deferred to the loop's authoritative
  gate.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python
  subset + native build + native ctest) to confirm the new test plus the in-flight
  reachability-cap + telemetry changes build and pass.
- **Live-test next:** none from this test-only change. Live PS5 confirmation of the
  reachability-cap release timing remains the open gameplay question; this round
  only strengthens release-reason telemetry coverage and does not require live
  validation.

### Round 6 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`,
  `AGENT_DIALOGUE.md`.
- **Change made (do-item #4 / #5, coverage):** Do-item #4 requires the release
  reason to distinguish `green_confirmed`, `predictive_target`, `max_hold_safety`,
  `waiting_for_meter`, and stale/no-sample states. Each of those had a dedicated
  pinning test — `predictive_target`
  (`standstillTimedReleaseSetsPredictiveReasonCode`,
  `fastMeterCapsPredictiveReleaseNearTop`), `max_hold_safety`
  (`stuckMeterReleasesViaMaxHoldSafety`), `await_meter`
  (`noMeterAbortsInsteadOfBlindRelease`, `goToWaitsForLateMeterThenTimes`),
  `out_of_reach` (`holdingReportsOutOfReachWhenMeterBelowCap`), `stale_sample`
  (`holdingStaleSampleReportsDistinctReason`) — **except `green_confirmed`**, the
  best/first path in do #4. `replayedRisingTrajectoryReleasesInsideWindow` only
  accepts it as one of three allowed codes, so the specific green-window crossing
  label was never pinned. Added `greenConfirmedCrossingReportsGreenConfirmedCode`.
  **Test-only: NO change to any source file or runtime behavior** — the sole
  `native_orion` source edit is the test file.
- **Test added:** `AutomationEngineTests::greenConfirmedCrossingReportsGreenConfirmedCode`
  arms a square hold to Holding, gets past the commit floor, then feeds five
  consistent green-window samples (86-98, width 12 ≥ visibleGreenWidthPct 8 → target
  = center 92) at a CONSTANT below-target fill (70) so velocity stays ~0 and neither
  the predictive (block 2, needs velocity>0.001) nor reactive (block 4, needs
  fill≥target) path can fire while the green window confirms (tracker confirms after
  4 consistent frames). It asserts the bot keeps Holding throughout, then feeds one
  sample at fill 96 (≥ target): with the window confirmed and the meter at/over the
  predicted crossing, block 1 fires and the test asserts
  `releaseReasonCode == green_confirmed` and `!= predictive_target`. ETA is forced
  to -1 so the detector-ETA path can't fire, leaving the confirmed green crossing as
  the only release route — deterministic, no reliance on a race between paths.
- **Scope/regression check (by inspection):** Only `AutomationEngineTests.cpp` adds
  one slot declaration and one test body; no production code, no existing test, and
  no `releaseReasonCode` string changed. `holdReleaseStrategy` defaults to `hybrid`
  (not `fixed`) and `tempoFallbackTimeoutMs`/`maximumHoldMs` are set to 5000ms in
  the test, so no safety/fallback path can pre-empt the green-window release;
  `learningBiasPct` defaults to 0 so the confirmed target is exactly the center (92).
- **Tests run by Claude:** none executable in-sandbox. The native build
  (`cmake --build ... OrionNativeTests`), `ctest`, and `verify_orion.ps1` all
  require interactive approval here and the Qt/OpenCV toolchain lives outside the
  session's allowed working directory — same limitation as Rounds 1, 3, 4, and 5.
  Build/build-probe and the gate were both refused by the sandbox this round.
  Deferred to the loop's authoritative gate.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python
  subset + native build + native ctest) to confirm the new test plus the in-flight
  reachability-cap + telemetry changes build and pass.
- **Live-test next:** none from this test-only change. Live PS5 confirmation of the
  reachability-cap release timing remains the open gameplay question; this round
  only strengthens telemetry-honesty coverage and does not require live validation.

### Round 5 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\src\AutomationEngine.cpp`,
  `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`.
- **Change made (do-item #4 telemetry honesty):** In
  `AutomationEngine::processHolding`, the `!shouldRelease && !meterFresh`
  waiting branch already set a distinct machine code `stale_sample` (a meter that
  was seen this shot but whose latest sample has gone stale), but its
  human-readable `releaseReason` text was `waiting_for_meter` /
  `waiting_for_goto_meter` — byte-for-byte identical to the never-saw-a-meter
  `await_meter` early-return path (lines ~907-909). So the `reason=` field in live
  telemetry could not distinguish "meter went stale mid-shot" from "meter never
  appeared," contradicting do-item #4's requirement to distinguish
  `waiting_for_meter` from stale states. The stale branch now emits its own reason
  text (`meter_sample_stale` / `goto_meter_sample_stale`). **Telemetry-only: no
  change to `shouldRelease`, release timing, target, `withinReach`, the
  `stale_sample` code itself, or controller output** — only the human-readable
  `releaseReason`/`releasePlan` strings on a non-releasing (HOLDING) frame.
- **Test added:** `AutomationEngineTests::holdingStaleSampleReportsDistinctReason`
  arms to Holding, gets past the commit floor, feeds one fresh contested
  (no-green -> target 100) far-low sample (fill 40) to latch `meterSeenThisShot`,
  then ages the sample past `meterFreshWindowMs` (220ms) with no new detection and
  processes again. It asserts the holding frame reports `releaseReasonCode ==
  stale_sample`, `releaseReason == meter_sample_stale`, and that the reason is NOT
  the `await_meter` path's `waiting_for_meter` text. Without the fix the reason
  collides with `waiting_for_meter` and the test fails.
- **Scope/regression check (by inspection):** The edit only rewrites the
  `releaseReason`/`releasePlan` strings inside the existing `!meterFresh` waiting
  branch; the `stale_sample` code is unchanged, so the
  `holdingFramesReportHonestWaitingCode` invariant (holding frames must not report a
  release code) is unaffected. No existing test asserts the literal
  `waiting_for_meter` reason text on a stale (meter-seen) frame — the `await_meter`
  early-return path (never-saw-meter) is untouched and still emits
  `waiting_for_meter`. Every release-path reason-code assertion reads the code at a
  RELEASE frame, which this branch never reaches.
- **Tests run by Claude:** none executable in-sandbox. The native build
  (`cmake --build ... OrionNativeTests`), `ctest`, `verify_orion.ps1`, and even
  `pytest` all require interactive approval here, and the Qt/OpenCV toolchain lives
  outside the session's allowed working directory (file access is restricted to the
  repo), so the gate's native build/test cannot run in-sandbox — same limitation as
  Rounds 1, 3, and 4. Deferred to the loop's authoritative gate.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python
  subset + native build + native ctest) to confirm the new test plus the in-flight
  reachability-cap + telemetry changes build and pass.
- **Live-test next:** none from this telemetry change itself. Live PS5 confirmation
  of the reachability-cap release timing (Round-4/prior rounds) remains the open
  gameplay question; the now-distinct `meter_sample_stale` reason makes a
  stalled/dropped-feed shot easier to read in live telemetry but does not require
  live validation to land.

### Round 4 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\src\AutomationEngine.cpp`,
  `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`.
- **Change made (do-item #4 telemetry, supporting do-item #2):** In
  `AutomationEngine::processHolding`, the waiting-telemetry block had three honest
  waiting codes — `stale_sample`, `confidence_low`, `eta_not_ready`. But a meter
  that is fresh AND confident yet still further below the target than the
  reachability cap allows (`!withinReach`) skips every predictive path and fell
  through to the generic `eta_not_ready`. That hid the real reason the bot was
  waiting: the reachability cap (do-item #2, `maxPredictiveRisePct +
  reachabilitySlackPct`) deliberately holding a fast meter back until it climbs near
  the top. Added a fourth waiting branch (after `confidence_low`, before the
  `eta_not_ready` else) that reports `releaseReasonCode = out_of_reach` with reason
  `below_reachability_cap` / `goto_below_reachability_cap`. **Telemetry-only: no
  change to `shouldRelease`, release timing, target, `withinReach`, or controller
  output** — the new code only writes the plan/reason/code strings on a
  non-releasing (HOLDING) frame.
- **Test added:** `AutomationEngineTests::holdingReportsOutOfReachWhenMeterBelowCap`
  drives a fresh, confident, contested (no-green -> target 100) meter stuck far
  below target (fill 40, ~0 velocity) and asserts the holding frame reports
  `out_of_reach` while the state stays `Holding`. Without the new branch the frame
  reports `eta_not_ready` and the test fails.
- **Scope/regression check (by inspection):** `out_of_reach` is NOT a release code,
  so `holdingFramesReportHonestWaitingCode`'s invariant (holding frames must not
  report a release code) still holds; every other reason-code assertion
  (`standstillTimedReleaseSetsPredictiveReasonCode`,
  `stuckMeterReleasesViaMaxHoldSafety`, `contestedNoGreenTimesMeterToTop`,
  `replayedRisingTrajectoryReleasesInsideWindow`,
  `fastMeterCapsPredictiveReleaseNearTop`, the `await_meter` checks) reads the code
  only at a RELEASE frame or in the early-return await/abort paths, none of which
  this branch touches. No existing test asserts `eta_not_ready`.
- **Tests run by Claude:** none executable in-sandbox. The native build
  (`cmake --build ... OrionNativeTests`), `ctest`, `verify_orion.ps1`, and even
  `pytest` all require interactive approval here, and the Qt/OpenCV runtime `PATH`
  that the gate needs cannot be set (env-var modification is blocked in this
  sandbox) — same limitation as Rounds 1 and 3. Deferred to the loop's authoritative
  gate.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python
  subset + native build + native ctest) to confirm the new test and the in-flight
  reachability-cap + telemetry changes build and pass.
- **Live-test next:** none from this telemetry change itself. Live PS5 confirmation
  of the reachability-cap release timing (per the prior rounds) is still the open
  gameplay question; the new `out_of_reach` code makes that easier to read in live
  telemetry but does not require live validation to land.

### Round 3 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Claude, implementer (one focused step).
- **Files changed:** `native_orion\src\AutomationEngine.cpp`,
  `native_orion\tests\AutomationEngineTests.cpp`, `STATUS.md`.
- **Change made (do-item #4, honest telemetry):** In
  `AutomationEngine::processHolding`, the predictive blocks set
  `releaseReasonCode` to `green_confirmed`/`predictive_target`/`reactive_target`
  as a side effect of *evaluating* each path, even on frames where `shouldRelease`
  stayed false (e.g. the meter is within reach but the extrapolated crossing is
  still > one lead away). The code resets to `tracking` each frame, so leaving a
  release code in place on a non-releasing frame falsely told live telemetry a
  release fired while the bot was merely waiting. The three waiting branches now
  set an honest waiting code: `stale_sample` (meter seen this shot but the latest
  sample is stale — distinct from the never-saw-a-meter `await_meter`),
  `confidence_low`, and `eta_not_ready`. **Telemetry-only: no change to
  `shouldRelease`, release timing, target, or controller output.**
- **Test added:** `AutomationEngineTests::holdingFramesReportHonestWaitingCode`
  drives a slow rising meter (ETA disabled so the velocity path governs timing) and
  asserts the invariant: every HOLDING frame reports a waiting code, and only the
  releasing frame reports a release code. Without the fix, the within-reach frames
  report a stale `predictive_target`/`green_confirmed` and the test fails.
- **Tests run by Claude:** none executable in-sandbox. The native build
  (`cmake --build ... OrionNativeTests`) requires interactive approval here, and the
  Qt/OpenCV runtime `PATH` that `verify_orion.ps1` sets for `ctest` cannot be set
  (env-var modification is blocked in this sandbox). Deferred to the loop's
  authoritative gate, consistent with Round 1.
- **Verification by inspection:** the change only writes `releaseReasonCode` inside
  `!shouldRelease` branches; every existing reason-code assertion
  (`standstillTimedReleaseSetsPredictiveReasonCode`,
  `stuckMeterReleasesViaMaxHoldSafety`, `goToWaitsForLateMeterThenTimes`,
  `replayedRisingTrajectoryReleasesInsideWindow`,
  `fastMeterCapsPredictiveReleaseNearTop`, the `await_meter` checks) reads the code
  only at release time or in the early-return await/abort paths, so none are
  affected.
- **Remaining blockers:** the loop must run
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1` (Python
  subset + native build + native ctest) to confirm the new test and the in-flight
  reachability-cap change build and pass.
- **Live-test next:** none from this telemetry change. Live PS5 confirmation of the
  reachability-cap release timing (per the prior round) is still the open
  gameplay question, but this round does not require it.

### Round 2 - supervisor authorization (Codex)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Agent / round:** Codex, supervisor setup.
- **Files changed:** `TASK.md`, `agent-loop.ps1`, `scripts\run_agent_loop.ps1`,
  `scripts\watch_agent_loop.ps1`, `STATUS.md`.
- **Change made:** authorized the current AutomationEngine gameplay task, set the
  wrapper default to 10 rounds, added a real-time watcher, and fixed
  `agent-loop.ps1` so it reads the Current Status token block instead of old
  history entries.
- **Tests run:** pending after the loop resumes. This was coordination tooling.
- **Remaining blockers:** none for scope. The next loop run may continue under the
  new bounded 10-round authorization.
- **Live-test next:** none until the agents set `LIVE_VALIDATION_REQUIRED: yes` or
  the verification gate passes and live gameplay needs confirmation.

### Round 1 - T1 review (Codex, reviewer/debugger)

```
DECISION: blocked
STOP_REQUESTED: yes
LIVE_VALIDATION_REQUIRED: no
```

- **Decision:** blocked at the time because `TASK.md` did not yet authorize
  gameplay/core edits. This has now been superseded by Round 2 authorization.
- **Tests run:** `C:\Python314\python.exe -m pytest tests\test_stability_tracking.py -q`
  -> `8 passed`; wrapper dry run -> preflight green; Codex CLI responded after the
  local model was changed from unsupported `gpt-5.6` to `gpt-5`.

### Round 1 - T1 implementation (Claude, implementer)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Files changed:** `scripts\verify_orion.ps1`, `STATUS.md`.
- **Change made:** added `tests\test_stability_tracking.py` to the verify script's
  Python sidecar pytest list.
- **Tests run:** none by Claude; the loop/human reviewer ran the stability test
  afterward.

### Round 0 - scaffolding (Claude, setup pass)

```
DECISION: needs-changes
STOP_REQUESTED: no
LIVE_VALIDATION_REQUIRED: no
```

- **Files changed:** created `TASK.md`, `STATUS.md`, `AGENT_RULES.md`,
  `TEST_COMMANDS.md`, `agent-loop.ps1`.
- **Change made:** established the bounded two-agent coordination workflow.

---

## How agents update this file

Each round updates the `## Current Status` token block and appends a new block under
Round History. Always set:

```
DECISION: approved | needs-changes | blocked
STOP_REQUESTED: yes | no
LIVE_VALIDATION_REQUIRED: yes | no
```

Set `LIVE_VALIDATION_REQUIRED: yes` and `STOP_REQUESTED: yes` when progress depends
on live PS5 / Remote Play gameplay.
