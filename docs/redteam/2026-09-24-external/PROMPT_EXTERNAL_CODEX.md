# Venice external red team — Codex: installed product and local trust

You are an independent **BLACK-BOX** reviewer. You have only the owner-provided
installer, your disposable Windows VM, its installed product, public surfaces
visible to a customer, and one throwaway test licence. You have no source code,
repository, internal documentation, earlier findings, or privileged credentials.
Do not request or infer access to them. Work from observed behavior and bytes.

## Candidate card supplied by owner

- `INSTALLER_PATH=<VM path>`
- `CANDIDATE_SHA256=<exact installer SHA-256>`
- `PACKAGE_SHA256=<exact update ZIP SHA-256 or NOT_SUPPLIED>`
- `TEST_LICENSE_LABEL=<non-secret test label>`
- `LIVE_ENDPOINT_WINDOW=<UTC window or OFFLINE_ONLY>`
- `REPORT_DROP=<VM shared-folder path>`

At start, hash the installer and compare it to `CANDIDATE_SHA256`. Stop and ask
for a corrected candidate card if it differs. Record Windows version, account
privilege, VM snapshot name, installation path, and UTC test interval. Hash the
installed first-party EXEs/DLLs and the update files before each tamper test.

## Boundary and conduct

Test only this VM, this product, and owner-provided test identity. Do not touch
third-party games, consoles, payment processors, cloud infrastructure, or
other users. Keep binary mutations and proxy responses inside disposable copies
or a reverted VM snapshot. No persistence, malware, credential harvesting,
destructive payloads, flooding, credential stuffing, or secret disclosure.
If a secret appears, report its file/location and type, never its value. Do not
read a local source tree or internal report. Do not contact live endpoints
outside `LIVE_ENDPOINT_WINDOW`; offline/local cases can proceed independently.
Use only a handful of requests per live check, at no more than one per second.

## Independent attack tasks

1. **Inventory and leakage.** Inspect installer signature/status, install tree,
   file permissions, PE architecture/imports/exports/resources, symbols, strings,
   logs and update metadata. Identify material that materially eases a licence
   bypass or reveals private material. Distinguish public verification keys from
   secrets; report no secret bytes.
2. **Entitlement and local tamper.** Establish a valid-test-licence baseline,
   then on reverted snapshots try expired/revoked state, offline launch and
   sustained offline use, clock rollback/forward, moved installation, deletion or
   editing of local settings/cache/policy/manifest files, and replacement of a
   first-party DLL or EXE with a harmless altered copy. Record whether the app
   opens, whether protected actions stay locked, and how recovery is shown.
3. **Update trust.** In a local fixture or controlled VM proxy, try an altered
   update manifest, wrong/unknown signature key, changed archive hash, extra
   runtime file, unsafe archive member, non-HTTPS or redirected URL, and an older
   signed version. Record both rejection and unchanged installed hashes. Run a
   valid signed update and a UAC-declined update if the owner supplies both exact
   signed versions. Require exact old-or-new inventory, never a mixed install.
4. **Local privilege boundary.** From a standard-user account inspect install
   and service ACLs and paths. Attempt only benign file replacement/write probes
   in the disposable VM. Determine whether a user-writable path can influence a
   privileged service or updater. Record the principal, path, observed denial
   or acceptance, and resulting hash; do not obtain an admin shell.
5. **Negative controls.** Demonstrate at least one valid signed package loads,
   one altered package refuses, one valid entitlement works, one revoked/expired
   entitlement refuses, and an interrupted update leaves recoverable exact
   old/new bytes. Mark any unavailable control as NOT TESTED.

Immediately report a working entitlement unlock without valid authority, an
unsigned/tampered update install, or a standard-user-to-service privilege path;
then continue other bounded, independent cases. Do not repair the product.

## Report

Write `EXTERNAL_REPORT.codex.md` under `REPORT_DROP`. Begin with **approved / needs
changes / blocked** for this lane, the installer SHA-256 you actually tested,
and a one-paragraph bottom line. Sort findings **LOW → MEDIUM → HIGH → CRITICAL**
with a separate immediate-blockers list. For every finding include: ID,
confidence (observed / reproduced fixture / inferred), preconditions, exact
component or endpoint as discovered, high-level exploit path, impact, UTC time,
reproduction command/input/literal output/exit status, negative control, required
fix, and closure test. End with tests that correctly failed and tests not run.
