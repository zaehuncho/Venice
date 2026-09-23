# A6 — Packaging, installer, updater and build scripts

**Decision: blocked for production update-path approval; needs changes in packaging.** Three newly isolated reliability findings follow. The existing M-01 package/source mismatch remains open and is not counted again. This review does not approve an installer or signed update end to end.

## Coverage and evidence boundary

Read the requested audit context and reviewed the package assembly/filter/sidecar attestation/signing/publication flow, installer build and Inno script, updater launch/elevation/wait/apply/rollback/relaunch flow, archive helper, native manifest verifier, and corresponding tests. Product source and settings were not edited. All custom builds and fixtures ran under the two scratch directories below; no launcher, real updater, installer, service, or remote endpoint was run.

- `C:\Users\aaron\AppData\Local\Temp\nexus-a6-updater-audit-20260923`: byte-identical copy of `UpdaterArchive.cpp`, headers, CMake project, benign test DLL, `audit.cpp`, Release harness and `results.txt`.
- `C:\Users\aaron\AppData\Local\Temp\nexus-a6-pytest-20260923`: Python test output, package-integrity JSON/output, `archive_transaction_fixture.py` and `archive_transaction_results.txt`.

Fresh source hashes, rechecked before writing:

| Source under `C:\Users\aaron\Desktop\NexusVision` | SHA-256 |
|---|---|
| `native_orion/src/UpdaterArchive.cpp` | `AF8548A184A0E04CC21AD7B5B424B4623A8308401230265D73AC60968E1D7B21` |
| `native_orion/src/updater_main.cpp` | `46D86E3263166905229BBF9480C3E37D1995EAAF29A3CBADE96E83293B5C6541` |
| `native_orion/src/OrionAppController.cpp` | `568FB02F001C4DC91831E099DDB67EBE203667618035B59E69FEA43D8C1E6C72` |
| `tools/package_orion_release.py` | `4EBA56B7E9DDBED765BBDC21CB71E95D8874FDD252B5E39A7510E65F86165EFC` |

## New findings

### [AUD-A6-001] high — The updater overwrites its own loaded runtime, and rollback encounters the same image lock

- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:5825-5826,5872-5889` launches the updater directly from the install directory, including the new elevated path. `C:\Users\aaron\Desktop\NexusVision\native_orion\CMakeLists.txt:402-414,444-451` links it against install-local UpdaterCore, SecurityCore and Qt DLLs. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\UpdaterArchive.cpp:220-226,581-598` unconditionally removes every existing destination file before copying, both during apply and restore. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\updater_main.cpp:350-360` attempts rollback in the same still-loaded process. The coherent scratch harness loaded an ordinary benign DLL from its fixture install, took a backup, then invoked the actual copied helpers. Literal output:
  ```text
  loaded=1 backup=1 apply=0 written=1 error=Could not overwrite: C:\Users\aaron\AppData\Local\Temp\a6-updater-QYzmIc\install\runtime.dll
  rollback=0 error=Could not overwrite: C:\Users\aaron\AppData\Local\Temp\a6-updater-QYzmIc\install\runtime.dll
  rollback_after_unload=1
  ```
  The DLL bytes were even identical between source and destination: no hash comparison avoids the attempted remove. This is new evidence under the broader M-14 update-delivery gate, distinct from its known Program Files elevation problem.
- Customer impact: an otherwise authentic update can abort when it reaches the updater executable or a loaded DLL, after earlier files have been replaced. The rollback uses the same locked tree and can also fail, producing the documented reinstall-required state. Elevation does not release image mappings. The fixture proves both failure paths, not that every earlier changed file necessarily remains changed after its particular failed restore.
- Proposed change: make the update transaction execute from a separately staged, verified helper **and its complete runtime dependency closure**, outside the destination tree. Wait for every install-local process/service that owns files to exit, with checked timeout results, before modifying it. Preserve a durable transaction/backup until post-update validation succeeds. Moving only `OrionUpdater.exe` while loading its DLLs from the install directory is insufficient. This is a transaction/lifecycle change, not an honest one-line diff; no source change was applied.
- Verification: add a Windows integration fixture that runs the real helper outside a disposable install containing its original updater/Qt/Core DLLs, updates those files to different hashes, verifies the exact final inventory, and proves rollback restores all prior hashes under injected mid-copy failure. Include a still-running child/service and failed launcher exit; no destination changes may start until the ownership barrier succeeds. The current scratch harness exits **0** because it successfully detects the expected defect; it is not a passing end-to-end update. No real signed-update run was performed.
- Confidence: confirmed

### [AUD-A6-002] medium — Successful updates preserve retired runtime files rather than install the reviewed runtime set

- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\native_orion\src\UpdaterArchive.cpp:581-584` applies only a recursive overlay. The contract in `C:\Users\aaron\Desktop\NexusVision\native_orion\src\UpdaterArchive.h:69-73` explicitly leaves destination-only files untouched; `C:\Users\aaron\Desktop\NexusVision\native_orion\tests\OrionUpdaterTests.cpp:739-762` tests preservation without separating mutable data from retired runtime. Fresh scratch output after successful apply: `overlay_apply=1 retired_runtime_exists=1`. The fixture's `retired-plugin.dll` is a plain marker file, not an executable payload. `C:\Users\aaron\Desktop\NexusVision\native_orion\src\SecurityManager.cpp:570-578` iterates the new manifest entries; it does not enumerate and reject all destination extras.
- Customer impact: DLL/plugin/model files removed from a newly reviewed package remain installed for existing customers. Fresh installs and upgraded installs therefore have different runtime contents, and pruning a retired component in the packager does not reliably retire its installed copy. This does not establish that any specific leftover is loaded; nor does it imply that the current native verifier rejects every extra file.
- Proposed change: compute retired runtime entries from the previously verified manifest minus the new verified manifest, remove only those entries within the same backed-up transaction, and validate the resulting runtime inventory. Preserve explicitly permitted mutable data and unrelated user files; do not indiscriminately prune the whole installation. Rollback must restore removed old runtime files as well as overwritten ones. This requires manifest-aware policy above the generic copy primitive, so a blanket `pruneToBackup(stage, install)` diff would be incorrect.
- Verification: upgrade a fixture with an old manifested plugin/model absent from the new manifest; assert the old runtime files are gone, new file hashes match, and user data remains byte-identical. Inject failure after removal and assert the old manifested inventory and all user data are restored. Retain the existing generic-copy preservation test, but add an update-transaction inventory test. Fresh copied-helper reproduction exited **0** with the literal output above.
- Confidence: confirmed

### [AUD-A6-003] medium — Package publication commits before archive/manifest validation, and archive creation destroys the previous output on failure

- Kind: reliability
- Evidence: `C:\Users\aaron\Desktop\NexusVision\tools\package_orion_release.py:1359-1364` publishes the staged package before creating the update ZIP or manifest. `:901-910` deletes the existing same-version ZIP and writes directly to its final path. `:1379-1398` then constructs/signs/writes the update manifest; artifact URL validation happens only at `:1062`, after package/ZIP replacement. The final combined verifier runs at `:1401-1411`, without restoring the already-published package and artifact when it fails. A scratch-only import of the actual function, with a controlled disk-write failure, produced:
  ```text
  exception=fixture disk write failure; previous_artifact_preserved=False; output_size=22
  invalid_url_rejected=1; previous_artifact_preserved=False; new_archive_exists=True
  ```
  The second case uses `http://example.invalid/update.zip` only as a string passed to validation; no network access occurs.
- Customer impact: a failed release build can leave a new package directory alongside an incomplete/replaced ZIP and an old update manifest. Signature/hash checks should reject mismatches, but the previous locally prepared release unit is no longer intact; an operator retry or upload can fail or publish an inconsistent unit. This is not an authentication/signature bypass.
- Proposed change: validate all CLI metadata before touching published paths; build the package, ZIP and signed update manifest together in private staging; run the combined verifier against those staged paths; publish a versioned immutable release directory only after success, then atomically select that directory. At minimum, create_archive must write to a unique sibling temporary file and `os.replace` the final ZIP only after successful close/hash. The following small part prevents the late URL-validation case, **but does not fix the full transaction**:
  ```diff
  --- a/tools/package_orion_release.py
  +++ b/tools/package_orion_release.py
  @@
  -        publish_staged_package(
  +        if not args.skip_archive:
  +            _validate_artifact_url(args.artifact_url)
  +        publish_staged_package(
  ```
  Do not treat that partial proposal alone as closure.
- Verification: inject failures at ZIP member writing, archive close/hash, manifest signing/write, combined verification and final publication. After every failed run the previous package, ZIP and manifest hashes must all remain unchanged, and no partial final ZIP may be visible. After success the three outputs must correspond to one verified staged unit. The supplied scratch fixture exits **0** while observing current failure behavior; existing happy-path packaging tests pass and do not cover this failure transaction.
- Confidence: confirmed

## Tests and release-state refresh

1. Existing isolated Python suites:
   ```powershell
   $env:PYTHONDONTWRITEBYTECODE='1'
   $tmp='C:\Users\aaron\AppData\Local\Temp\nexus-a6-pytest-20260923'
   New-Item -ItemType Directory -Force $tmp | Out-Null
   .venv/Scripts/python.exe -B -m pytest tests/test_release_packaging.py tests/test_release_filter_hygiene.py tests/test_packager_quick_style_prune.py tests/test_packager_updater_attestation.py tests/test_verify_release_integrity.py -q -o "cache_dir=$tmp/cache" --basetemp "$tmp/temp"
   ```
   Result: **82 passed in 2.42s; exit 0**. An initial invocation with a missing external basetemp parent had 65 setup errors and 17 passes; creating that scratch parent resolved the harness setup error. No product fix was involved.

2. Copied native archive primitive fixture, MSVC 19.44 / Qt 6.8.0:
   ```powershell
   cmake -S "$env:TEMP/nexus-a6-updater-audit-20260923" -B "$env:TEMP/nexus-a6-updater-audit-20260923/build" -G 'Visual Studio 17 2022' -A x64 -DCMAKE_PREFIX_PATH=C:/Users/aaron/Qt/6.8.0/msvc2022_64
   cmake --build "$env:TEMP/nexus-a6-updater-audit-20260923/build" --config Release
   $env:PATH='C:\Users\aaron\Qt\6.8.0\msvc2022_64\bin;'+$env:PATH
   & "$env:TEMP/nexus-a6-updater-audit-20260923/build/Release/a6-updater.exe"
   ```
   Configure/build/run exit **0**; exact observed primitive outputs appear above. The copied `UpdaterArchive.cpp` hash still equals the current source hash. All installation operations targeted QTemporaryDir fixtures.

3. Archive failure fixture:
   ```powershell
   .venv/Scripts/python.exe -B "$env:TEMP/nexus-a6-pytest-20260923/archive_transaction_fixture.py"
   ```
   Exit **0**; two literal outputs under AUD-A6-003. The monkeypatch affects only the scratch Python process.

4. Read-only current package check:
   ```powershell
   .venv/Scripts/python.exe -B tools/verify_release_integrity.py --package release/orion-package --json "$env:TEMP/nexus-a6-pytest-20260923/package-integrity.json"
   ```
   Result: **PASS, integrity chain intact, 577 files verified; exit 0**. The package contains 579 total files / 719,922,636 bytes, below the current 1 GiB extraction-byte ceiling. This verifies the existing signed package, not the concurrently edited source and not the updater transaction.

### Existing M-01 refreshed, not a new finding

The current packaged `OrionNative.exe`, `AutomationCore.dll`, `OrionUpdater.exe` and `SecurityCore.dll` still differ from the corresponding current build outputs. Package hashes are respectively:

```text
2941CEA27CD506ACBE6A0F2D2CDAE69E1639531AE6D38E10953C1646D43242DD
C5C16974251114F48EDDDDF824CF8825506056FD2A988A1BF4BEB848F0568E6C
D7CC601524D79D17FB7E815B046A35D8396BA08DDE35A889033563B4F15290FA
C545B588DD2876A40E19B487C14CE56DE159EA6A628EA8A8C172E1E79E4EE834
```

Five of 46 source hashes in the packaged sidecar build manifest differ from the current checkout: `capture_card_backend.py`, `native_orion/backend/autogreen_sidecar.py`, `remote_play_orchestrator.py`, `shot_records.py`, `simple_meter_reader.py`. This is current provenance evidence for M-01, not a conclusion that either binary's contents are malicious or that every build output is newer/coherent. Rebuild one coherent deployment unit after the concurrent fixes, then run Standard and StrictSecurity on that exact unit and a disposable Windows installer/update/rollback canary. The new launcher elevation branch was inspected but not exercised; M-14 is not closed by source inspection alone.
