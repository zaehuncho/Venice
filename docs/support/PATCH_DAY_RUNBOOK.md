# Patch-day runbook (NBA 2K27 update or Venice incident)

Written 2026-09-23 for RT-HIGH-04 (documentation part) of the final internal red team. It turns the
P9 plan (`docs/redteam/2026-09-22-launch/P9-2k-patch-response.md`, "2K patch-day plan") into steps
with an owner for each one and the commands that already exist in the repo.

**Read this first.** Venice has no fleet signal and no detection-only off switch yet (see "What is
missing" at the end). Today you learn about a patch from Discord and your own rig. The only remote
lever is the **global kill switch**, which pauses the whole product for everyone. Use the MOTD
first. Use the kill switch only when shots are actively doing damage.

## Who does what

| Role | Person | Owns |
|---|---|---|
| Incident owner | Owner (Isaiah) | Every decision below, MOTD, kill switch, update publish, the signing key |
| Support | Staff on Discord | Tickets, the macros in `docs/support/SUPPORT_MACROS.md`, reporting patterns to the owner |
| Build | Claude (main session) | Native build, sidecar build, tests, packaging, StrictSecurity |
| Gate | Codex | Release-integrity review before anything ships |

Staff never touch the kill switch, the MOTD, the update manifest or the signing key.

## Customer-visible states you will see in tickets

These are the real in-app strings (after the 2026-09-23 P-E patch). Quote them; do not paraphrase.

| State | What the customer sees |
|---|---|
| Venice cannot see the meter (blind latch) | Live page banner **NOT TIMING** with the meter hint; the meter card's red line |
| Service paused (kill switch on) | Sign-in card: "Venice is paused by the service right now. Nothing is wrong with your PC or internet." Hint: "Check #announcements in the Venice Discord for when it's back, then press Unlock again." |
| MOTD | Top-centre banner tagged NOTICE / WARNING / MAINTENANCE with your text |
| Update required | "Update required — this version is no longer allowed (minimum version X)." Hint: "Restart Venice to update, or reinstall from #downloads." |
| Lease lapse / network blip | Banner **SHOTS PAUSED**: "Reconnecting to Venice servers — shots paused until your subscription is confirmed. Usually a few seconds." |

## 1. Detect (T+0)

Signals, in the order they usually arrive:

1. 2K announces an update, or the PS5 downloads one.
2. Tickets or #paid-chat messages: "no greens", "not timing", "NOT TIMING banner".
3. Your own rig session shows the NOT TIMING banner, `METER BLIND` / `DETECTION UNAVAILABLE`
   lines in `logs/orion_native.log`, or a sudden drop in greens.

Support: when two or more customers report the same thing within an hour, ping the owner with the
ticket links and the exact in-app text they see. Use macro **M-PATCH-1** in the meantime.

## 2. Communicate early (T+0 to T+15 min, owner)

Post a `warn` MOTD before you know the cause. It rides the licence heartbeat and `/api/version`.

```powershell
orion-admin server-config set --motd-text "NBA 2K27 update detected. Venice is checking meter detection. If shots feel off, shoot manually until we confirm." --motd-level warn --motd-until <unix time ~6 h ahead> --reason "2K patch check"
```

(`orion-admin` is `tools/admin/orion_admin.py`; see `docs/ADMIN_TOOL.md` for sign-in. Every
mutation needs `--reason`.)

Then post the **patch_detected** template from
`discord_launch/launch_embeds/patch_day_status_templates.json` in #announcements (Triton or Nereus
`/announce`). Fill `{VERSION}` / `{TIME}` placeholders by hand. **Never run
`discord_launch/register_commands.py`**: it overwrites the guild command tree.

## 3. Confirm (T+15 min to T+1 h, owner + Claude on the dev rig)

Always launch through the launcher, never `OrionNative.exe` directly:

```powershell
$env:ORION_FRAMEDUMP = "1"          # one run per directory
powershell -ExecutionPolicy Bypass -File run_orion.local.ps1
```

1. Take 20 shots with the meter style customers use (Arrow2, White).
2. Read, in order: the Meter Detection card line, the `METER BLIND` / `SHOT NOT OWNED … backstop=`
   lines in `logs/orion_native.log`, and the banner tally.
3. Grade the shots: `.venv\Scripts\python.exe tools\timing\panel_grade.py` (see its `--help`).
4. Replay the new dump against a pre-patch dump:
   `.venv\Scripts\python.exe tools\diagnostics\replay_framedump.py` (see its `--help`).
5. Session summary for the ticket thread: `.venv\Scripts\python.exe tools\diagnostics\session_report.py`.

Decide:

- **Unchanged** (lock rate and EXCELLENT share within noise of the pre-patch baseline): clear the
  MOTD (`--motd-text "" --reason "patch verified unchanged"`), post **all_clear**. Done.
- **Look changed** (meter not detected): Venice is already failing closed (NOT TIMING, no timer
  shots in METER mode). Keep the MOTD, update it to the "not timing" wording in the templates.
  Fix the reader/proposer constants or retrain; gate with the replay corpus.
- **Speed changed** (detected but timing off): this is the dangerous case. There is no mismatch
  sentinel yet, so shots **will** fire on wrong timing. Go to step 4 now.

## 4. Kill or keep (owner)

Pull the global kill switch only when shots are firing wrong (speed change) or you cannot tell:

```powershell
orion-admin killswitch status
orion-admin killswitch engage --reason "2K patch: meter timing changed, fix in progress"
```

Effect: new sign-ins fail with the "paused by the service" card; running sessions disconnect at
their next heartbeat (≤ 5 min, sooner on retry). Post **service_paused** in #announcements at the
same time. Staff switch to macro **M-PATCH-2**.

Known gap (RT-CRIT-02, backend): a warm cached "kill off" state can be reused if the kill-state
read fails. Until that fix is deployed, confirm with `orion-admin killswitch status` and one test
sign-in on a spare profile after engaging.

## 5. Ship a signed update (Claude builds, Codex gates, owner publishes)

Bump the version (single source: CMake `PROJECT_VERSION`), then, in order:

1. Native build (Claude, central).
2. Sidecar: `powershell -ExecutionPolicy Bypass -File scripts\build_orion_sidecar.ps1`, then
   `.venv\Scripts\python.exe tools\sidecar_bundle_manifest.py --verify --root . --dist build\sidecar\autogreen_sidecar.dist`.
3. Release gate (produces the signed package, `release\orion-package-<ver>.zip` and the re-signed
   `release\update_manifest.json`):

   ```powershell
   $env:ORION_UPDATE_SIGNING_KEY_PEM = "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python "C:\Users\aaron\AppData\Local\Programs\Python\Python312\python.exe" -StrictSecurity
   ```
   Full detail and failure fixes: `docs/LAUNCH_PACKING_CHECKLIST.md` Step 1.
4. Pack: `tools\security\pack_lethe_release.py` per `docs/PACKING_RUNBOOK.md` (production key).
5. Verify the exact artifact before upload (step 3 builds it through
   `tools/package_orion_release.py`; this checks the files you are about to publish):

   ```powershell
   .venv\Scripts\python.exe tools\verify_release_integrity.py --package release\orion-package --update-manifest release\update_manifest.json --artifact release\orion-package-<ver>.zip --strict-warnings
   ```
6. Codex release gate on those exact hashes.
7. Upload the zip; set the final `artifact_url` and have it signed by the signing path (never
   hand-edit the signature; RT-MED-11). Publish `/api/update` with `mandatory: true`.
8. If the fix is only safe from this version on, raise the version gate so older clients get the
   "Update required" card:

   ```powershell
   orion-admin server-config set --min-client-version <new ver> --reason "patch fix required"
   ```

Installer rebuild is only needed for new downloads (`installer\build_installer.ps1`, Step 3 of the
checklist). The beta installer ships unsigned (owner decision 2026-09-23); publish its SHA-256 in
#downloads with the file.

Known gap (RT-HIGH-02): the default install in `C:\Program Files\Venice` may not prompt for
elevation, so the in-app update can fail to stage. Until the elevation fix ships, tell customers
to reinstall from #downloads if "Update" does not finish (macro **M-UPDATE-1**).

## 6. Verify (owner + support)

1. On a clean customer profile: the old version updates (or shows "Update required"), the new
   version signs in, 20 shots on the rig grade within noise of the pre-patch baseline.
2. `orion-admin metrics` for activation errors after publish.
3. Watch tickets for 30 minutes after release.

## 7. Recover (owner)

1. Release the kill switch: `orion-admin killswitch release --reason "fix vX.Y.Z live"`.
2. Update the MOTD: `--motd-text "Fixed in vX.Y.Z. Restart Venice to update." --motd-level info`.
3. Post **fix_shipped**, and **all_clear** once tickets stop.
4. If the update itself is bad: publish the previous signed manifest again (the updater only moves
   forward unless `allow_rollback` is signed in), or raise `min_client_version` past it and ship a
   corrected build. Customers can always reinstall the last good installer from #downloads.
5. Write a short incident note in `docs/redteam/` (timeline, customer impact, what to change).

## What is missing (for the post-launch build, not implemented here)

- **Fleet signal (CL2-P9-003).** Count-only, privacy-minimal counters on `/api/license/check`:
  `armed_epochs`, `owned_locks`, `detection_unavailable_trips`, `blind_backstop_blocks`, per client
  version, per 5-minute bucket, no identifiers beyond the existing key/machine. Backend needs a
  schema, aggregation, an owner alert when `owned_locks / armed_epochs` drops by 50 % against the
  trailing 24 h, and a dashboard number in `orion-admin metrics`. Client needs the counters, a
  bounded payload and a unit test.
- **Detection-only off switch (CL2-P9-004).** A signed `detection_policy {autonomous: off, message}`
  in the lease/heartbeat response. Client: no release while off, Live-page banner shows `message`,
  heartbeat test for on/off/unknown (unknown must fail closed). Backend: owner-only config field
  with an audit row, same TOTP step-up as the kill switch.
- **Mismatch sentinel (CL2-P9-002).** Compare observed meter rise time and box height against the
  compiled table; on mismatch fail closed to pass-through with a banner.
- **Drill.** One timed signed no-op update: commit → package → manifest published → a customer
  profile updated, with timings recorded. Not done yet.
- **Canary ring.** The server has one `current` manifest; a `channel=beta` ring would let the owner
  update his own rig first.
