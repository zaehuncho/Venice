param(
    # Empty, NOT "python": a bare name is not a path, and passing it made
    # build_orion_sidecar.ps1 throw "Sidecar build Python not found at 'python'" before it
    # could look anywhere sensible. Empty delegates to that script's capability-based
    # resolution (it picks the first interpreter that can actually import Nuitka).
    [string]$Python = "",
    [string]$QtRoot = "",
    [string]$OpenCVDir = "",
    [string]$LibCryptoPath = "",
    [switch]$StrictSecurity
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# [ORION_VERIFY_PYTHON] Resolve $Python to a REAL interpreter path before anything uses it.
# This script invokes it directly (py_compile, pytest, packaging, security audit) as well as
# passing it to build_orion_sidecar.ps1, so an unresolved value fails in two different ways:
# the literal "python" is not a path and made the sidecar script throw, while an empty string
# makes "& $Python" fail with "The expression after '&' ... must result in a command name".
# Prefer an interpreter that can import Nuitka, since the sidecar build needs it; fall back to
# any working interpreter so the non-sidecar steps still run.
if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python)) {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        $c = Get-Command $Python -ErrorAction SilentlyContinue
        if ($c) {
            $candidates += $c.Source
        } else {
            # [2026-09-21 verification provenance] An EXPLICIT -Python that is neither an
            # existing path nor a resolvable command name must not silently degrade to
            # whatever interpreter happens to be around: the gate's evidence would then
            # name an interpreter nobody asked for (Astra, release review).
            throw "-Python '$Python' does not exist and is not a command on PATH. Pass a real interpreter path, or omit -Python to auto-resolve."
        }
    }
    $candidates += (Join-Path $Root ".venv\Scripts\python.exe")
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" `
                        -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })

    $usable = @()
    foreach ($c in $candidates) {
        if ([string]::IsNullOrWhiteSpace($c) -or -not (Test-Path -LiteralPath $c)) { continue }
        $full = (Resolve-Path -LiteralPath $c).Path
        if ($usable -contains $full) { continue }
        $usable += $full
    }
    $picked = $null
    foreach ($c in $usable) {
        & $c -c "import nuitka" 2>$null
        if ($LASTEXITCODE -eq 0) { $picked = $c; break }
    }
    if (-not $picked -and $usable.Count -gt 0) { $picked = $usable[0] }
    if (-not $picked) { throw "No usable Python interpreter found. Pass -Python <python.exe>." }
    $Python = $picked
}
Write-Host "[orion] using Python: $Python"

if ([string]::IsNullOrWhiteSpace($QtRoot)) {
    $DevCache = Join-Path $Root "native_orion\build\CMakeCache.txt"
    $CachedQt = if (Test-Path -LiteralPath $DevCache) {
        Select-String -LiteralPath $DevCache -Pattern '^Qt6_DIR:[^=]*=(.+)$' |
            Select-Object -First 1 -ExpandProperty Matches |
            ForEach-Object { $_.Groups[1].Value }
    }
    if (-not [string]::IsNullOrWhiteSpace($CachedQt)) {
        $QtRoot = [System.IO.Path]::GetFullPath((Join-Path $CachedQt "..\..\.."))
    } elseif (-not [string]::IsNullOrWhiteSpace($env:Qt6_DIR)) {
        $QtRoot = [System.IO.Path]::GetFullPath((Join-Path $env:Qt6_DIR "..\..\.."))
    } else {
        $QtRoot = Get-ChildItem -LiteralPath "C:\Qt" -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "msvc2022_64" } |
            Where-Object { Test-Path -LiteralPath (Join-Path $_ "lib\cmake\Qt6\Qt6Config.cmake") } |
            Sort-Object -Descending |
            Select-Object -First 1
    }
}
$QtRoot = [System.IO.Path]::GetFullPath($QtRoot)
if (-not (Test-Path -LiteralPath (Join-Path $QtRoot "lib\cmake\Qt6\Qt6Config.cmake"))) {
    throw "Qt 6 MSVC package not found at '$QtRoot'. Pass -QtRoot <msvc2022_64-dir>."
}
if ([string]::IsNullOrWhiteSpace($OpenCVDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:OpenCV_DIR)) {
        $OpenCVDir = $env:OpenCV_DIR
    } else {
        $OpenCVDir = Join-Path $env:USERPROFILE "Downloads\opencv\build"
    }
}
$OpenCVDir = [System.IO.Path]::GetFullPath($OpenCVDir)
if (-not (Test-Path -LiteralPath (Join-Path $OpenCVDir "OpenCVConfig.cmake"))) {
    throw "OpenCV CMake package not found at '$OpenCVDir'. Pass -OpenCVDir <opencv-build-dir>."
}
$OpenCVBin = Join-Path $OpenCVDir "x64\vc16\bin"

$env:PYTHONPATH = $Root
$env:PATH = "$QtRoot\bin;$OpenCVBin;$Root\native_orion\build\Release;" + $env:PATH

function Invoke-OrionStep {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host $Name
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

function Assert-OrionCMakeOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$BuildDirectory,
        [switch]$RequireTests
    )

    # PowerShell's Set-Location does not update System.IO's process cwd. Resolve
    # relative build names against this verifier's checkout, never its caller.
    if (-not [IO.Path]::IsPathRooted($BuildDirectory)) {
        $BuildDirectory = Join-Path $Root $BuildDirectory
    }
    $cache = Join-Path $BuildDirectory "CMakeCache.txt"
    if (-not (Test-Path -LiteralPath $cache)) {
        throw "CMake cache missing from '$BuildDirectory'."
    }
    $expectedSource = [System.IO.Path]::GetFullPath((Join-Path $Root "native_orion")).Replace('\', '/')
    $homeLine = Select-String -LiteralPath $cache -Pattern '^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$' |
        Select-Object -First 1
    $actualSource = if ($homeLine) { $homeLine.Matches[0].Groups[1].Value.Replace('\', '/') } else { "" }
    if (-not $actualSource.Equals($expectedSource, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing cross-checkout CMake cache '$cache' (source '$actualSource', expected '$expectedSource')."
    }

    if ($RequireTests) {
        $testFile = Join-Path $BuildDirectory "CTestTestfile.cmake"
        if (-not (Test-Path -LiteralPath $testFile)) {
            throw "CTest metadata missing from '$BuildDirectory'."
        }
        $buildFull = [System.IO.Path]::GetFullPath($BuildDirectory).Replace('\', '/').TrimEnd('/')
        $testText = Get-Content -LiteralPath $testFile -Raw
        foreach ($testExe in @("OrionNativeTests.exe", "OrionPassiveCourtFlowTests.exe", "OrionShmInteropTests.exe", "OrionUpdaterTests.exe")) {
            $expectedTest = "$buildFull/Release/$testExe"
            if ($testText.IndexOf($expectedTest, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
                throw "CTest metadata for $testExe does not target this checkout: '$testFile'."
            }
        }
    }
}

function Invoke-OrionNativeTests {
    param([Parameter(Mandatory = $true)][string]$BuildDirectory)

    if (-not [IO.Path]::IsPathRooted($BuildDirectory)) {
        $BuildDirectory = Join-Path $Root $BuildDirectory
    }
    Assert-OrionCMakeOwnership -BuildDirectory $BuildDirectory -RequireTests
    $runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
    $buildFull = [IO.Path]::GetFullPath($BuildDirectory)
    $evidenceBase = Join-Path $buildFull 'Testing\OrionVerification'
    if (-not [string]::IsNullOrWhiteSpace($env:ORION_VERIFY_LOG_ROOT)) {
        # Opt in to a short evidence root for Windows PowerShell path limits.
        # Keep dev/prod build identity and the full unique invocation ID; no
        # test, transcript, timeout, or failure status is omitted or retried.
        $buildLeaf = Split-Path -Leaf $buildFull.TrimEnd('\')
        $evidenceBase = Join-Path ([IO.Path]::GetFullPath($env:ORION_VERIFY_LOG_ROOT)) $buildLeaf
    }
    $runDirectory = Join-Path $evidenceBase $runId
    New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
    $ctestLog = Join-Path $runDirectory 'ctest.txt'
    $previousLogRoot = $env:ORION_QTEST_LOG_ROOT
    $ctestExit = 125
    try {
        $env:ORION_QTEST_LOG_ROOT = Join-Path $runDirectory 'qtest'
        Write-Host "    Native test evidence: $runDirectory"
        # QtTest wrappers retain their first invocation and enforce the original
        # 120 s child bound. CTest allows wrapper termination/evidence grace.
        ctest --test-dir $BuildDirectory -C Release --output-on-failure --timeout 135 --no-tests=error --output-log $ctestLog
        $ctestExit = $LASTEXITCODE
    } finally {
        $env:ORION_QTEST_LOG_ROOT = $previousLogRoot
        Write-Host "    CTest exit=$ctestExit; retained log: $ctestLog"
        # Printing logs/environment restoration must not turn a failed CTest
        # gate green when Invoke-OrionStep inspects LASTEXITCODE.
        $global:LASTEXITCODE = $ctestExit
    }
}

function Resolve-OrionLibCryptoSource {
    param([string]$ExcludePath = "")

    $excluded = if ([string]::IsNullOrWhiteSpace($ExcludePath)) {
        ""
    } else {
        [System.IO.Path]::GetFullPath($ExcludePath)
    }
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($LibCryptoPath)) {
        $candidates += $LibCryptoPath
    }
    # Prefer the active Aaron dev tree. The legacy build_prod cache on this
    # workstation belongs to another checkout and is only a last-resort DLL
    # source (the production CMake/test metadata itself is never reused).
    $candidates += (Join-Path $Root "native_orion\build\Release\libcrypto-3-x64.dll")
    $candidates += (Join-Path $Root "native_orion\build_prod\Release\libcrypto-3-x64.dll")
    $gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($gitCommand) {
        $gitRoot = Split-Path -Parent (Split-Path -Parent $gitCommand.Source)
        $candidates += (Join-Path $gitRoot "mingw64\bin\libcrypto-3-x64.dll")
    }
    $source = $candidates |
        Where-Object {
            -not [string]::IsNullOrWhiteSpace($_) -and
            (Test-Path -LiteralPath $_) -and
            ([string]::IsNullOrWhiteSpace($excluded) -or
             [System.IO.Path]::GetFullPath($_) -ne $excluded)
        } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($source)) {
        throw "libcrypto-3-x64.dll not found. Pass -LibCryptoPath <dll> so Ed25519 tests cannot be skipped."
    }
    return [System.IO.Path]::GetFullPath($source)
}

function Ensure-OrionLibCrypto {
    param([string]$DestinationDirectory)

    $destination = Join-Path $DestinationDirectory "libcrypto-3-x64.dll"
    if (Test-Path -LiteralPath $destination) {
        return
    }
    $source = Resolve-OrionLibCryptoSource -ExcludePath $destination
    New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

Write-Host "[orion] Python compile checks"
& $Python -m py_compile `
    capture_card_backend.py `
    chiaki_backend.py `
    controller_remap.py `
    decoder_pipe_identity.py `
    meter_detector.py `
    meter_detector_yolo.py `
    remote_play_client.py `
    remote_play_cv.py `
    remote_play_orchestrator.py `
    async_diagnostic_csv.py `
    shot_records.py `
    rtt_sync_engine.py `
    simple_meter_reader.py `
    virtual_controller.py `
    native_orion\backend\autogreen_sidecar.py `
    tools\diagnostics\analyze_orion_recording.py `
    tools\diagnostics\correlate_releases.py `
    tools\diagnostics\meter_stability_report.py `
    tools\diagnostics\session_report.py `
    tools\sidecar_bundle_manifest.py `
    tools\quality\consistency_bench.py `
    tools\timing\epoch_table.py `
    tools\training\eval_meter_occlusion.py `
    tools\security_audit.py `
    tools\security\pack_lethe_release.py `
    tools\admin\orion_admin.py `
    tools\admin\check_backend_contract.py
if ($LASTEXITCODE -ne 0) { throw "Python compile checks failed with exit code $LASTEXITCODE" }

Invoke-OrionStep "[orion] Python sidecar tests" {
    & $Python -m pytest tests\test_controller_remap.py tests\test_orchestrator_capture.py `
        tests\test_decoder_pipe_identity.py `
        tests\test_decoder_ordering_reliability.py `
        tests\test_wgc_lifecycle_reliability.py `
        tests\test_xbox_remote_play.py `
        tests\test_decoder_payload_assembly.py `
        tests\test_meter_detector_yolo_geometry.py `
        tests\test_meter_locator_cv.py `
        tests\test_fade_candidate_budget.py `
        tests\test_meter_hsv_hotpath.py `
        tests\test_player_anchor.py tests\test_player_anchor_acquire.py `
        tests\test_meter_locator_crop_context.py `
        tests\test_meter_detector_async_priority.py `
        tests\test_meter_detector_async_lifecycle.py `
        tests\test_detector_source_transition.py `
        tests\test_meter_update_idempotence.py `
        tests\test_async_diagnostic_csv.py `
        tests\test_framedump_census_reliability.py `
        tests\test_green_cap_association.py `
        tests\test_lossless_frame_archive.py `
        tests\test_pipeline_clock_trace.py `
        tests\test_shot_pipeline_audit.py tests\test_timing_tail_audit.py `
        tests\test_shot_record_framedump_completion.py `
        tests\test_shot_records.py `
        tests\test_shot_records_wiring.py `
        tests\test_shot_record_clocks.py `
        tests\test_detcsv_frame_identity.py `
        tests\test_detcsv_authority.py `
        tests\test_detector_frame_wakeup.py `
        tests\test_simple_reader_occlusion_template.py `
        tests\test_simple_reader_occlusion_search_origin.py `
        tests\test_meter_detector_provider_priority.py `
        tests\test_readiness_incident.py `
        tests\test_remote_play_client_lifecycle.py `
        tests\test_remote_play_frame_pipe.py `
        tests\test_verify_remote_play_backend.py `
        tests\test_meter_detector_motion.py tests\test_meter_detector_video.py `
        tests\test_meter_detector_live_gdi.py tests\test_meter_detector_units.py `
        tests\test_rtt_sync_engine.py `
        tests\test_chiaki_decoder_geometry.py tests\test_orchestrator_feed_gate.py `
        tests\test_capture_card_backend.py tests\test_capture_pts_lock.py `
        tests\test_capture_publication_lifecycle.py `
        tests\test_capture_timing_epoch.py `
        tests\test_capture_pts_continuity.py `
        tests\test_frame_integrity_pipeline.py tests\test_stream_quality.py `
        tests\test_export_rate_stats.py tests\test_pipe_bt709.py `
        tests\test_stall_staleness.py `
        tests\test_simple_meter_reader.py tests\test_reader_robust.py `
        tests\test_reader_camera_adaptive.py tests\test_meter_reader_y.py `
        tests\test_simple_reader_lock_lifecycle.py `
        tests\test_simple_reader_tracking_geometry.py `
        tests\test_simple_reader_fade_tracking.py `
        tests\test_simple_reader_template_cadence.py `
        tests\test_simple_reader_source_lifecycle.py `
        tests\test_compressed_reader_source_lifecycle.py `
        tests\test_simple_reader_ghost_press.py `
        tests\test_simple_reader_box_width_gate.py `
        tests\test_simple_reader_floating_white.py `
        tests\test_simple_reader_session_ruler.py `
        tests\test_simple_reader_partial_occlusion.py `
        tests\test_simple_reader_sidecar_wiring.py `
        tests\test_event_driven_meter_emission.py `
        tests\test_live_session_diagnostics.py `
        tests\test_sidecar_bundle_manifest.py `
        tests\test_sidecar_detector_smoke.py `
        tests\test_shipped_reader_defaults.py `
        tests\test_consistency_bench.py `
        tests\test_epoch_table.py `
        tests\test_eval_meter_occlusion.py `
        tests\test_finetune_meter_lowfill.py `
        tests\test_correlate_releases_parse.py `
        tests\test_security_audit.py `
        tests\test_release_packaging.py `
        tests\test_packing_workflow.py `
        tests\test_native_qtest_diagnostics.py `
        tests\test_orion_admin.py `
        tests\test_backend_contract_check.py `
        tests\test_backend_staff_auth.py `
        tests\test_venice_ui_contract.py `
        tests\test_deep_link_target_policy_contract.py `
        tests\test_preview_qml_contract.py `
        tests\test_gui_cadence_contract.py `
        tests\test_latency_calibration_ui_contract.py `
        tests\test_passive_court_flow_contract.py `
        tests\test_roi_relock.py tests\test_stability_tracking.py `
        tests\discord -q
    # [2026-09-21] tests\discord was outside this allowlist entirely, so the customer-
    # facing bot copy (the /purchase embed and its price) had NO release-gate coverage:
    # a price change passed Strict with the bot test still pinning the old price. The
    # whole directory is gated now; its one stale contract (test_venice_guard_refresh:
    # the keyless status() handler) was reconciled the same day rather than xfail-ed.
}

Invoke-OrionStep "[orion] Custom Remote Play runtime" {
    & $Python tools\verify_remote_play_backend.py
}

# Lethe (the PE packer/protector) is now a standalone repo at
# C:\Users\aaron\Desktop\Lethe; its build + end-to-end round-trip gate lives
# there and is no longer part of the NexusVision release verification. The
# NexusVision-side release wrapper (tools\security\pack_lethe_release.py) and
# its unit test (tests\test_packing_workflow.py) remain gated above.

Invoke-OrionStep "[orion] Native build" {
    Assert-OrionCMakeOwnership -BuildDirectory "native_orion\build"
    cmake --build native_orion\build --config Release --target OrionNative OrionOwner OrionStaff VeniceNet OrionVeniceNetTests OrionVeniceNetIpcClientTests VeniceNetSvc OrionVeniceNetServiceTests OrionNativeTests OrionFireEpochClockTests OrionMeterDelayTests OrionMeterDelaySettingsTests OrionRemotePlayPathTests OrionInputRetryTests OrionXboxPolicyTests OrionLauncherResponseTests OrionVeniceProfileTests OrionPassiveCourtFlowTests OrionPreviewPresentationTests OrionActivityFeedPolicyTests OrionLicenseHeartbeatPolicyTests OrionSettingsSignatureTransactionTests OrionGuiFreezeWatchdogPolicyTests OrionAdminStepUpPolicyTests OrionShotVerdictTallyTests OrionOrderedFileLogSinkTests OrionInputProtocolTests OrionDeepLinkTargetPolicyTests OrionShmInteropTests OrionRouteTransitionTests OrionShmNotificationTests OrionPreciseWaitTests OrionUpdater OrionUpdaterTests OrionActivateContractTests OrionBrokerInstallTrustTests OrionBrokerSelfTrustGateTests OrionBrokerSessionStoreTests OrionBrokerSpkiPinTests OrionMachineIdParityTests
}

Invoke-OrionStep "[orion] Native tests" {
    Ensure-OrionLibCrypto (Join-Path $Root "native_orion\build\Release")
    Invoke-OrionNativeTests -BuildDirectory "native_orion\build"
}

if ($StrictSecurity) {
    # Production verification runs in a SEPARATE Aaron-owned build tree. The
    # legacy native_orion/build_prod cache on this workstation belongs to the
    # Administrator checkout and must never be reused for this repository.
    # so the dev build (native_orion\build) stays a non-production binary for daily
    # work and live testing. -DORION_PRODUCTION=ON forces releaseManifestRequired=true
    # and compiles out every dev escape hatch, so it must NOT become the dev binary.
    $ProdBuild = "native_orion\build_prod_codex"
    # Production signature verification is bootstrapped by one exact libcrypto
    # build input. CMake hashes these bytes, embeds the pin, and stages this same
    # DLL beside SecurityCore so PATH/current-directory substitution cannot win.
    $ProdLibCryptoDestination = Join-Path $Root "$ProdBuild\Release\libcrypto-3-x64.dll"
    $ProdLibCryptoSource = Resolve-OrionLibCryptoSource -ExcludePath $ProdLibCryptoDestination

    Invoke-OrionStep "[orion] Configure production build (-DORION_PRODUCTION=ON)" {
        # Force ORION_BUILD_TESTS=ON explicitly: a stale build_prod cache may have it
        # OFF, and CMake cache is sticky (a reconfigure without -D keeps the cached value).
        cmake -G "Visual Studio 17 2022" -A x64 -S native_orion -B $ProdBuild -DORION_PRODUCTION=ON -DORION_BUILD_TESTS=ON `
            -DQt6_DIR="$($QtRoot -replace '\\','/')/lib/cmake/Qt6" `
            -DOpenCV_DIR="$($OpenCVDir -replace '\\','/')" `
            -DCMAKE_PREFIX_PATH="$($QtRoot -replace '\\','/')" `
            -DORION_LIBCRYPTO_PATH="$($ProdLibCryptoSource -replace '\\','/')"
    }
    Invoke-OrionStep "[orion] Production native build" {
        Assert-OrionCMakeOwnership -BuildDirectory $ProdBuild
        cmake --build $ProdBuild --config Release --target OrionNative OrionOwner OrionStaff VeniceNet OrionVeniceNetTests OrionVeniceNetIpcClientTests VeniceNetSvc OrionVeniceNetServiceTests OrionNativeTests OrionFireEpochClockTests OrionMeterDelayTests OrionMeterDelaySettingsTests OrionRemotePlayPathTests OrionInputRetryTests OrionXboxPolicyTests OrionLauncherResponseTests OrionVeniceProfileTests OrionPassiveCourtFlowTests OrionPreviewPresentationTests OrionActivityFeedPolicyTests OrionLicenseHeartbeatPolicyTests OrionSettingsSignatureTransactionTests OrionGuiFreezeWatchdogPolicyTests OrionAdminStepUpPolicyTests OrionShotVerdictTallyTests OrionOrderedFileLogSinkTests OrionInputProtocolTests OrionDeepLinkTargetPolicyTests OrionShmInteropTests OrionRouteTransitionTests OrionShmNotificationTests OrionPreciseWaitTests OrionUpdater OrionUpdaterTests OrionActivateContractTests OrionBrokerInstallTrustTests OrionBrokerSelfTrustGateTests OrionBrokerSessionStoreTests OrionBrokerSpkiPinTests OrionMachineIdParityTests
    }
    Invoke-OrionStep "[orion] Production dev-hook marker audit" {
        $ProdNative = Join-Path $Root "$ProdBuild\Release\OrionNative.exe"
        $ProdBytes = [System.IO.File]::ReadAllBytes($ProdNative)
        $ProdAscii = [System.Text.Encoding]::ASCII.GetString($ProdBytes)
        $ProdUnicode = [System.Text.Encoding]::Unicode.GetString($ProdBytes)
        foreach ($marker in @(
            "ORION_AUTO_UNLOCK_LOCAL",
            "ORION_AUTO_SIGN_SETTINGS",
            "ORION_LICENSE_KEY",
            "ORION_LOCAL_UI_TEST",
            "NVDEV-"
        )) {
            if ($ProdAscii.Contains($marker) -or $ProdUnicode.Contains($marker)) {
                throw "Production OrionNative.exe still contains a developer auth/signing hook marker."
            }
        }
    }
    Invoke-OrionStep "[orion] Production native tests" {
        Invoke-OrionNativeTests -BuildDirectory $ProdBuild
    }
    # Deploy the Qt runtime (Qt6 DLLs + platforms/qml/styles/tls plugin trees) into the
    # production tree. CMake POST_BUILD already stages ViGEmClient + OpenCV next to the exe.
    Invoke-OrionStep "[orion] Deploy Qt runtime into production tree" {
        & "$QtRoot\bin\windeployqt.exe" --release --qmldir native_orion\qml "$ProdBuild\Release\OrionNative.exe"
    }
    Invoke-OrionStep "[orion] Current compiled detection sidecar" {
        & powershell -ExecutionPolicy Bypass -File scripts\build_orion_sidecar.ps1 -Python $Python -Force
    }
    Invoke-OrionStep "[orion] Hardened internal package (production build)" {
        & $Python tools\package_orion_release.py --strict --no-customer --skip-archive --build-dir "$ProdBuild\Release"
    }
    Invoke-OrionStep "[orion] Internal release security audit" {
        # Strict means the source tree as well as the staged package. Package-only scanning
        # previously let a concrete dev key or webhook secret sit in tracked docs/tests while the
        # release gate printed OK, ready to be copied into a later build or support bundle.
        & $Python tools\security_audit.py
    }
    Invoke-OrionStep "[orion] Owner/staff startup integrity smoke" {
        $PackageDir = Resolve-Path "release\orion-package"
        foreach ($exe in @("OrionOwner.exe", "OrionStaff.exe")) {
            $process = Start-Process -FilePath (Join-Path $PackageDir $exe) `
                -ArgumentList "--check-startup-security" -WorkingDirectory $PackageDir `
                -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -ne 0) {
                throw "$exe rejected the verified package with exit code $($process.ExitCode)"
            }
        }
    }
    Invoke-OrionStep "[orion] Owner/staff tamper refusal smoke" {
        $TempRoot = Join-Path $env:TEMP ("orion-package-tamper-" + [guid]::NewGuid().ToString("N"))
        $TamperedPackage = Join-Path $TempRoot "orion-package"
        try {
            New-Item -ItemType Directory -Path $TempRoot | Out-Null
            Copy-Item "release\orion-package" $TamperedPackage -Recurse
            Remove-Item (Join-Path $TamperedPackage "release_manifest.json") -Force
            $process = Start-Process -FilePath (Join-Path $TamperedPackage "OrionStaff.exe") `
                -ArgumentList "--check-startup-security" -WorkingDirectory $TamperedPackage `
                -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -eq 0) {
                throw "OrionStaff.exe accepted a package with release_manifest.json removed"
            }
        } finally {
            Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Invoke-OrionStep "[orion] Hardened customer package (production build)" {
        & $Python tools\package_orion_release.py --strict --customer --build-dir "$ProdBuild\Release"
    }
    Invoke-OrionStep "[orion] Customer release security audit" {
        & $Python tools\security_audit.py
    }
}

Write-Host "[orion] OK"
