# Astra — wire up SERVER-SHARD mode for the Venice release (Lethe + license server)

**Handed off:** 2026-09-19. **Repo:** `C:\Users\aaron\Desktop\NexusVision` (branch
`fix/timing-input-and-remoteplay-blockers`, everything uncommitted). **Packer:** `C:\Users\aaron\Desktop\Lethe`.
**Owner (Isaiah), verbatim:** "we go ahead and go the extra mile with the server shard — one of my biggest
fears is someone getting a cracked copy and distributing it — wire that up today and we should be done and
shipped in a day or 2."

**Goal in one sentence:** the shipped, packed `OrionNative.exe` must be *incomplete on disk* — it decrypts
itself only after the license server hands back a 32-byte key fragment, after checking the customer's
license, machine and the build's revocation state — so a patched binary is broken, not unlocked, and the app
cannot start offline. The mechanism already exists on both sides; it is **experimental and not release-ready**
for the nine concrete reasons in §3. Your job is to close them, prove it end to end, and hand the coordinator
a deploy sequence.

**Ship posture:** this is the LAST blocker before packing. Do it right, but do not gold-plate. Everything
else in the release (site, Stripe, license server, Discord, lease signing) is live and verified; do not
touch it except where this doc says.

---

## 0. Hard rules

- **Never print, log, paste, commit, or write into the repo** a private key, the shard fragment, the shard
  encryption key, an SSM SecureString value, the Stripe keys, the Discord tokens, or the edge-auth secret.
  Generate secrets straight into AWS SSM (`--type SecureString`, value via `file://` from a 0600 temp file
  under `D:\NexusVision\signing\`, then shred). Show only lengths / hash prefixes / public halves.
- **Do not deploy anything live.** No `aws lambda update-function-code`, no `wrangler deploy`, no API
  Gateway changes, no Discord writes. You build, test and write the EXECUTE section; the coordinator deploys
  and the owner does the console steps.
- **No git commands that mutate state.** `git diff`/`status`/`log` are fine.
- **Never edit `settings.json` / `learning.json`.** Never launch `OrionNative.exe` for timing tests (the owner
  runs it via `run_orion.local.ps1 -NoElevate`). A **packed** build MAY be launched for the shard acceptance
  test only, on the owner's rig, with the owner present.
- **Do not touch:** `website/`, `discord_launch/`, `backend/lambda_function.py` outside the shard/auth
  functions named below, the timing engine (`AutomationEngine*`, `BannerLeadTrim.h`, the sidecar readers),
  `LeaseGate.cpp`'s pinned key (rotated today — leave it), `register_commands.py` (would delete slash
  commands), the update-signing key at `Desktop\VeniceSigning\` (the ONLY encrypted copy; a plaintext
  duplicate under `Desktop\EXPLOITS\...` is queued for the owner to back up then delete — leave both alone).
- Builds: never into `native_orion/build` or `build_prod_codex` unless you are running the documented
  release gate (`scripts/verify_orion.ps1 -StrictSecurity`) at the end; throwaway builds go under a short
  path like `C:/Users/aaron/obld_shard` and get deleted. pytest with `--basetemp=D:/NexusVision/pytest_tmp/shard`
  (the default temp dir is poisoned). C: has ~16 GB free — big outputs to `D:\NexusVision\`.
- AWS CLI is IAM user `venice-automation` (acct 987622176566, us-east-1): SSM Get/Put on `/orion/*`, Lambda
  get/update-code/invoke on the two Orion Lambdas, Logs read. **No IAM, no API Gateway, no DynamoDB
  console** — those are owner steps; write them out exactly.

---

## 1. What exists today (verified 2026-09-19, cite these, do not rediscover)

**Server** (`backend/lambda_function.py`, live as `orion-activate`, python3.12 x86_64, now vendoring
`cryptography` so AES-GCM works; CodeSha256 `EyMIAq911NK1V7HfBhHob4LUF/w33KstSAK5X+Qbx7A=`):
- `shards_table()` ~L342 (DynamoDB, key `build_id`); `SHARD_ENC_KEY_SSM = "/orion/shard_encryption_key"` L150;
  `_get_shard_enc_key` ~L2749, `_shard_encrypt/_shard_decrypt` ~L2755-2766 (AES-GCM at rest).
- `handle_shard_store` ~L2767: **`require_admin`**, body `{build_id, shard (64 hex = 32 bytes), license_id,
  hwid_hash, max_activations, ttl_hours}` → encrypts and stores.
- `handle_shard_retrieve` ~L2817: body `{build_id, license_key, machine_id}`; `rate_limit_ok("shard_retrieve",
  build_id, …)`; checks shard row revoked / expires_at / max_activations / hwid_hash (sha256 of machine_id);
  then the LICENSE row: exists, `status == "active"`, not revoked, `machine_id` binding, expiry; increments
  `activation_count`; audits; returns `{"ok":true,"shard":"<64 hex>"}`.
- `handle_shard_revoke` ~L2888, `handle_shard_status` ~L2911 (admin). Router ~L4870.
- `require_edge_auth(headers)` ~L1673 is applied to **every route with no exemption** (~L4765).
- Tests: **zero** backend tests mention shards (`grep -rl shard tests/backend` is empty).

**Packer** (`C:\Users\aaron\Desktop\Lethe`):
- `lethe.py` L255-277: `--server-shard --enable-experimental-server-shard --shard-url --shard-auth`
  (`LETHE_SHARD_AUTH` env). README L116-118 and `docs/RELEASE_CHECKLIST.md` L11, L97-105: *"Do not ship
  server-shard mode until its backend contract, TLS policy, signed launcher, denial paths, and signed
  application launch pass an external end-to-end acceptance gate."*
- `bootstrap/shard_bootstrap.c`: WinHTTP (`WINHTTP_FLAG_SECURE`, redirects disabled, 10/10/15/15 s
  timeouts) POST to `SHARD_HOST`/`SHARD_PATH` with body **`{"build_id","license_key","hwid"}`** (L336),
  header **only** `Content-Type: application/json` (L357); reads the license key from a JSON settings file
  (`read_license_key`, L171-204, needle `"license_key"`); `compute_hwid` (L229-246) = SHA-256 over the
  computer name (+ whatever else is in that function); expects 64 hex chars back (L412 `memcpy(...,64)`);
  **no disk cache** of the fragment (fetch on every launch — keep that property).

**Client app:** `native_orion/src/SecurityManager.h` L51 `machineId()` is the app's machine identity;
`LicenseClient.cpp` L294/L307 send `machine_id` to the server. Customers never see a license key: access is
Discord-linked; the app unlocks with a one-time `PAIR-…` code from zaeorion.com/connect, which the server
resolves to a real license row (`handle_activate` ~L604-610: `paired_item["license_key"]`).

**Release pack path:** `pack_lethe_release.py`, `installer/build_installer.ps1` (must be given
`-PackageDir release\orion-package-packed`), `docs/LAUNCH_PACKING_CHECKLIST.md`, gate
`scripts/verify_orion.ps1 -StrictSecurity` (needs `ORION_UPDATE_SIGNING_KEY_PEM` pointed at the
VeniceSigning PEM path, and `$env:TEMP` redirected). **Nothing in the release path passes `--server-shard`.**

---

## 2. Design decisions — make these explicitly, in the doc, before coding

**D1. One shard per RELEASE build, enforcement per LICENSE.** The current shard row carries `hwid_hash` and
`max_activations` *per build_id*, i.e. it was designed for one packed exe per customer. That does not fit
a public installer + auto-update. Decide: one fragment per release `build_id`; leave the row's `hwid_hash`
empty and `max_activations = 0`; keep ALL per-customer enforcement on the license row (it already checks
active/revoked/expiry/machine binding). Per-customer builds are out of scope for launch — say so.

**D2. Identity the bootstrap presents.** The stub currently reads a `license_key` from a settings file that
the app may not write. Find what the app actually persists after a successful activation (grep
`LicenseClient.cpp`, `SecurityManager`, the settings/credential store, `AppConfig` — look for the resolved
license key, an activation token, and the machine id) and key the fetch on **that**, so a customer who
paired via Discord has exactly what the stub needs. Document the file/format the stub reads and make the
app write it if it does not already. Never make the customer type anything.

**D3. Machine identity must be ONE derivation.** `compute_hwid` (stub) and `SecurityManager::machineId()`
(app) are different functions; the server binds the license to the app's value. Either port the app's exact
derivation into the stub (bit-for-bit, verified by a test that runs both on this machine and compares) or
have the app persist its machine id where the stub reads it. Choose the one that cannot drift.

**D4. Auth model for `/api/shard/retrieve`.** The global edge-auth header would have to be embedded in the
shipped binary — **not acceptable** (it authenticates every other route). Options, pick and justify:
(a) exempt only `/api/shard/retrieve` from edge auth and rely on TLS + license/machine binding + a strict
per-license rate limit; (b) require the activation token the app already holds as a bearer on that route.
Store/revoke/status stay admin-only behind edge auth. Whatever you choose: no shared secret in the exe.

**D5. Rate limit and abuse keys.** `rate_limit_ok("shard_retrieve", build_id, …)` is keyed on the build —
shared by every customer of a release — so a busy launch weekend locks everyone out. Re-key to
`license_key + machine_id` (and optionally IP), with limits that survive a customer relaunching the app a
few times a minute. Keep the audit rows (`shard_retrieved`, `shard_hwid_mismatch`, …) but never log the
fragment.

**D6. Failure behaviour is product, not an afterthought.** No server ⇒ no start, by design — but the user
must see *why*. Distinct, human messages for: cannot reach servers (with Retry), license not active /
expired (subscribe at zaeorion.com), machine mismatch (use `/hwid_reset`), build revoked (update Venice),
rate limited (wait a minute). Never a silent exit, never a hex error. Map each server `error` string to one.

**D7. TLS.** Keep `WINHTTP_FLAG_SECURE` + no redirects; require TLS 1.2+; do NOT pin the API Gateway leaf
certificate (AWS rotates it and you would brick every install). Document the policy — the Lethe checklist
asks for one.

**D8. Rotation and revocation.** Each release gets a fresh `build_id` and a fresh fragment (generated at pack
time by CSPRNG, uploaded via store, never written to the repo). The updater ships the new packed exe;
`handle_shard_revoke` on the old build_id after a grace period is the kill switch for a leaked build. Write
the runbook lines for both.

---

## 3. The nine gaps — every one must be closed

1. **Gateway routes missing.** `POST /api/shard/retrieve`, `POST /api/shard/store`,
   `POST /api/admin/shard/revoke`, `GET|POST /api/admin/shard/status` all return the gateway's own
   `{"message":"Not Found"}` on HTTP API `v348t5hg3i` (explicit routes). → Owner step in EXECUTE: create
   them attached to integration `gpamg89` (`orion-activate`), like today's `/api/bot/guild-join`.
2. **`/orion/shard_encryption_key` does not exist in SSM** (`describe-parameters` returns nothing). Every
   store/retrieve would 500. → EXECUTE step: generate 32 random bytes (hex/base64 per what
   `_get_shard_enc_key` expects — read it), `put-parameter --type SecureString`. Confirm the Lambda role can
   read it (watch CloudWatch for `AccessDenied … shard_encryption_key`; IAM fix is owner-only).
3. **Wire contract mismatch**: stub sends `hwid`, server reads `machine_id`. Fix one side, add a contract
   test that builds the stub's exact JSON and runs it through `handle_shard_retrieve`'s parser.
4. **Edge auth blocks the stub** (D4).
5. **Identity model** (D2) and **machine-id derivation** (D3).
6. **Per-build rate limit / cap / hwid** (D1, D5).
7. **Zero tests.** Backend: store/retrieve/revoke/status happy paths and every denial (revoked shard,
   expired shard, cap, hwid mismatch, missing/inactive/revoked/expired license, machine mismatch, rate
   limit, bad hex, wrong length, non-admin store); the auth model; audit rows without the fragment. Stub:
   a host-side test harness for `compute_hwid`/identity parity and the JSON body; a fake-server test for
   each error mapping in D6.
8. **Packer integration.** The release command in `docs/LAUNCH_PACKING_CHECKLIST.md` must run Lethe with
   shard mode ON, generate the fragment, upload it (store needs admin auth — determine what `require_admin`
   accepts and how the packing machine presents it WITHOUT leaving a secret on disk or in the checklist),
   record the `build_id` next to the release artefacts, and refuse to continue if the upload fails. Also fix
   `installer/build_installer.ps1` so it defaults to the packed dir (today it silently packages the
   UNPACKED build unless `-PackageDir release\orion-package-packed` is passed — a release-integrity bug).
9. **Acceptance gate** (Lethe checklist §105): a written, runnable script for the owner's rig:
   (a) fresh install, launch → shard fetched, app runs; (b) block `execute-api` in the hosts file /
   firewall, launch → refuses with the "cannot reach servers" message, no crash, no fallback;
   (c) `/api/admin/shard/revoke` the build → refuses with "update Venice"; (d) activate on machine A, copy
   the install to machine B → "machine mismatch"; (e) run ≥ 20 min → still firing (lease refresh);
   (f) update-manifest install of a second build_id → new fragment fetched, old one revoked after grace.
   Record each result. Nothing ships until (a)–(e) pass live.

---

## 4. Deliverables

- Code: backend (shard handlers, auth model, rate-limit keys, tests), Lethe stub + `lethe.py` flags
  promoted out of "experimental" ONLY after the acceptance gate passes (until then keep the flags but make
  the release path pass them), `pack_lethe_release.py` + checklist + installer default fix.
- `backend/build_lambda_zip.ps1` run → new zip at `D:\NexusVision\signing\orion-activate.zip` with its
  CodeSha256 printed (do not upload).
- `docs/SERVER_SHARD_2026-09-19.md`: decisions D1–D8 with reasoning, the closed gaps with file:line, the
  test counts, the acceptance script, the rotation/revocation runbook, and an **EXECUTE** section in order:
  (1) owner: SSM shard key (or coordinator, since `ssm:PutParameter` on `/orion/*` is allowed — say which),
  (2) owner: the four gateway routes, (3) coordinator: Lambda deploy with `--revision-id`, (4) pack with
  shard ON, (5) acceptance run on the rig, (6) only then installer → `#downloads`.
- Update `docs/LAUNCH_PACKING_CHECKLIST.md` so the owner's pack command is the shard-enabled one.

**Report back with:** the decisions taken, every gap's fix (file:line), test counts (backend + stub),
the zip's CodeSha256, the exact owner/coordinator steps, and anything you could not verify without the
rig. Do not claim the mechanism works until (a)–(e) have run on a real machine against the live server.

---

## 5. Before gap 7 will even run — the test harness has no shard fixtures

`tests/backend/conftest.py` stands up moto and creates every table + SSM param the Lambda touches, then
reloads `lambda_function`. Two things it does **not** create, so any shard test import-errors or 500s today:

- **The shards table.** `TABLE_DEFS` (conftest L86) has no `orion-shards` (or whatever `shards_table()`
  resolves — read the env/name it uses). Add it with the real key schema (`build_id` HASH) so
  `shards_table()` binds.
- **`/orion/shard_encryption_key`.** `SSM_PARAMS` has every other `/orion/*` secret but not this one. Add
  it as a `SecureString` holding a valid 32-byte test key in exactly the encoding `_get_shard_enc_key`
  expects (read it — hex vs base64 vs raw), or `_shard_encrypt/_decrypt` throws.

Call the routes through the existing `invoke(lf, method, path, body=…, headers=…, edge=…)` helper
(conftest L204). `edge=True` supplies the edge-auth header; **prove D4 by calling `/api/shard/retrieve`
with `edge=False` and asserting it still works** while `store/revoke/status` with `edge=False` are
rejected. Assert on the audit rows with the same `audit_table().scan()` pattern the other tests use — and
add an explicit test that **no audit row, log line, or response body ever contains the fragment hex**.

---

## 6. How to work, and how to verify before you hand back

**Order that de-risks it.** (1) Lock the design — write D1–D8 into `docs/SERVER_SHARD_2026-09-19.md`
first, because D2/D3/D4 decide the wire and half the code. (2) Backend: fix the contract (gap 3), the auth
model (D4), the rate-limit keys (D5), the D1 row shape; add the conftest fixtures; write every test in
gap 7. (3) Stub: reconcile identity (D2/D3) and the error mapping (D6); host-side parity test. (4) Packer
+ checklist + installer default (gap 8). (5) Write the acceptance script (gap 9) — you cannot *run* it,
the owner does, on the rig, against live AWS.

**The green tree you are inheriting (measured 2026-09-19, same as the timing audit):**
- in-place Release build clean; `ctest -C Release` from `native_orion/build` → **22/22 pass**. This lane
  touches no C++ in `native_orion`, so that number must stay 22/22 — if it moves, you broke something
  unrelated.
- `python -m pytest tests -q --ignore=tests/discord` → **3228 passed, 19 skipped, 8 xfailed, 2 xpassed**.
  Your backend + any Python stub-parity tests **add** to the passed count; nothing here should turn red.
- Run the backend suite directly while iterating:
  `python -m pytest tests/backend -q --basetemp=D:/NexusVision/pytest_tmp/shard` (base interpreter has
  `moto`; do **not** use `.venv` here — that is only for `tests/discord`).
- **Known load-sensitive, not yours:** `tests/test_idle_scan_cost.py::test_the_burst_that_stalls_the_consumer_on_the_full_compare_does_not_on_the_strided_one`
  is a wall-clock assertion that fails under heavy parallel load and passes alone. If it is red, check
  machine load before touching it.
- The C stub has no ctest target today. Make the parity/JSON harness runnable and name the exact command
  in the doc; a Python `ctypes`/subprocess harness that shells the compiled stub is fine if a C test rig
  is heavier than the gap deserves.
- Last thing before handback: the release gate `scripts/verify_orion.ps1 -StrictSecurity` (needs
  `ORION_UPDATE_SIGNING_KEY_PEM` → the VeniceSigning PEM and `$env:TEMP` redirected) must still exit 0.
- Name every pre-existing failure you did not cause. If anything above is red the moment you start, it is
  a merge artefact — say so, do not code around it.

---

## 7. What I want from you

**A. Falsify the design before you build it.** §2's D1–D8 are my calls, not gospel. The ones most likely
to be wrong: **D4** (is an edge-auth exemption on one route actually safe, or does the activation-token
bearer close a hole I am not seeing?) and **D3** (can the stub's `compute_hwid` and the app's
`machineId()` be made bit-identical, or will one of them drift across a Windows update / hardware change
and lock a paying customer out of their own copy?). If either is wrong, the failure mode is a customer who
**paid** and **cannot launch** — that is worse than a cracked copy. Tell me with the evidence, don't just
comply.

**B. The one bar this ships against.** A patched, hex-edited, or offline binary is **dead** — it never
gets the fragment, so it never decrypts, so it does not run. A legitimate customer who paired through
Discord **never notices this exists** — no key to type, no extra prompt, sub-second added to launch, and
if the server is unreachable they get a plain-English "can't reach Venice, retry," not a crash or a hex
code. If your implementation cannot deliver both halves, say which half breaks and why.

**C. Close all nine gaps in §3** with a file:line for each fix, plus the two fixtures in §5.

**D. Do NOT** (recap of §0, because these are the ones that end a launch): deploy anything live; print /
log / commit / write any secret or the fragment; touch the timing engine, the site, Discord, the pinned
`LeaseGate.cpp` key, or the update-signing PEMs; run `register_commands.py`; or claim the mechanism works.
It is not proven until acceptance (a)–(e) in gap 9 have run on a real machine against the live server —
and you cannot run them, so hand the owner a script and say so plainly.

**E. Report back with:** the D1–D8 decisions taken (and any you overrode, with the measurement); every
gap's fix as file:line; backend + stub test counts; the `orion-activate.zip` CodeSha256 (built, not
uploaded); the exact owner-console and coordinator steps in the EXECUTE section; and a short, concrete
list of everything you could not verify without the rig or live AWS, so the owner knows exactly what to
watch for on the first packed launch.
