# owner_venice_setup.ps1
#
# ONE-TIME setup for the VeniceNet (Path A) manual test (2026-08-08 work).
# Run it from an elevated PowerShell (open Windows Terminal or PowerShell
# "as Administrator" - there is no right-click "Run as administrator" entry
# for .ps1 files on this rig):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\aaron\Desktop\NexusVision\scripts\owner_venice_setup.ps1"
#
# It registers the VeniceNetSvc packet-bridge service (the wave-2A C++
# service, no Python at runtime) against the DEV build, the same way
# installer/orion.iss registers the legacy service on a customer machine
# (demand-start + LocalSystem + armed + Interactive-Users start rights),
# but WITHOUT running the installer.
#
# MUTUAL EXCLUSION: VeniceNetSvc and the legacy NexusVisionSvc both bind
# TCP 47291 - exactly ONE of them may be registered at a time. This script
# refuses to proceed while NexusVisionSvc is registered (Path B,
# docs\OWNER_MANUAL_TEST.md).
#
# It does NOT start the service, does NOT load the WinDivert driver, and
# does NOT touch anything under %LOCALAPPDATA%\NexusVision\Orion Native\.
#
# Companion doc: docs\OWNER_MANUAL_TEST_VENICE.md
# Rollback (elevated, one line at a time; wait for STOPPED between them):
#   sc.exe stop VeniceNetSvc
#   sc.exe query VeniceNetSvc     (repeat until STATE shows STOPPED)
#   sc.exe delete VeniceNetSvc

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# Relative paths below resolve against the repo root no matter how the script
# was launched.
Set-Location -LiteralPath $Root

$AppDir     = Join-Path $Root "native_orion\build\Release"
$AppExe     = Join-Path $AppDir "OrionNative.exe"
$AppDll     = Join-Path $AppDir "VeniceNet.dll"
# 2026-08-08 PATH CORRECTION: the split-tree design (build_venicenet_wave2b for
# the app, build_venicenet_svc for the service) never landed - cmake generates
# both the app and the service subprojects under the single native_orion\build
# tree. The wave-2A/2B binaries live at these actual paths:
$DllSrc     = Join-Path $Root "native_orion\build\venicenet\Release\VeniceNet.dll"
$Wave2bApp  = $AppDir  # OrionNative.exe is built directly into build\Release
$BackupDir  = Join-Path $Root "native_orion\build\pre_wave2b_backup"
$SvcExe     = Join-Path $Root "native_orion\build\venicenet_service\Release\VeniceNetSvc.exe"
$WdDll      = Join-Path $Root "native_orion\build\venicenet_service\Release\WinDivert64.dll"
$WdSys      = Join-Path $Root "native_orion\build\venicenet_service\Release\WinDivert64.sys"
$SvcName    = "VeniceNetSvc"
$LegacySvc  = "NexusVisionSvc"
$SvcRegKey  = "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName"
# The app binaries that the wave-2B build (build_venicenet_wave2b) produces and
# that must be staged coherently into build\Release together.
$AppSet = @("OrionNative.exe", "AutomationCore.dll", "OrionCommon.dll",
            "RemotePlayCore.dll", "SecurityCore.dll", "UpdaterCore.dll",
            "VisionCore.dll")
# Same DACL installer/orion.iss sets: SYSTEM/Admins full control, Interactive
# Users get START/STOP/QUERY so the UNELEVATED app (or an unelevated console)
# can start the demand-start service.
$Sddl = 'D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWRPWPLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)'

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "SETUP STOPPED: $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# The wave-2B app resolves the VeniceNet DLL exports by name; those ASCII
# strings ("venicenet_init" etc.) are baked into the exe. An exe without them
# predates the VeniceNet wiring and would never load the DLL - the whole
# Path-A test would silently run the old architecture.
function Test-VeniceWiring([string]$exePath) {
    $bytes = [System.IO.File]::ReadAllBytes($exePath)
    $text  = [System.Text.Encoding]::ASCII.GetString($bytes)
    return $text.Contains("venicenet_init")
}

# --- 0. Elevation ------------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail ("This script must run elevated. Open Windows Terminal or PowerShell " +
          "as Administrator, then run:`n" +
          "  powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`"")
}

# --- 1. Preflight: binaries, DLL, wiring, mutual exclusion -------------------
if (-not (Test-Path -LiteralPath $AppExe)) {
    Fail "OrionNative.exe not found at $AppExe  (the Release build is missing - rebuild native_orion\build first)"
}
if (-not (Test-Path -LiteralPath $SvcExe)) {
    Fail "VeniceNetSvc.exe not found at $SvcExe  (build it: cmake --build native_orion\build --config Release --target VeniceNetSvc)"
}
if (-not (Test-Path -LiteralPath $WdDll)) {
    Fail ("WinDivert64.dll missing at $WdDll - the service cannot open a packet handle without it. " +
          "The wave-2A build stages it next to the exe; copy it from vendor\windivert\WinDivert64.dll if it was cleaned.")
}
if (-not (Test-Path -LiteralPath $WdSys)) {
    Fail "WinDivert64.sys missing at $WdSys - copy it from vendor\windivert\WinDivert64.sys"
}
if ($SvcExe -match ' ') {
    Fail ("The service path contains a space ($SvcExe); the simple binPath registration below " +
          "would mis-parse. Move the repo to a space-free path or register manually with " +
          "escaped quotes (see installer/orion.iss RegisterPacketBridgeService).")
}

# The app under test must actually contain the wave-2B VeniceNet wiring. With
# the single-build-tree correction above, $Wave2bApp == $AppDir, so the stage
# step is a no-op when OrionNative.exe was rebuilt after the wave-2B rewire
# landed - the check only trips if someone tests against a genuinely pre-rewire
# exe (e.g. an old artifact copied in from elsewhere). If so, the fail-early
# message tells the operator to rebuild rather than silently staging from an
# identical source location.
$needsAppStage = -not (Test-VeniceWiring $AppExe)
if ($needsAppStage) {
    Fail ("OrionNative.exe at $AppExe predates the VeniceNet wiring (no venicenet_init " +
          "import string). Rebuild it: cmake --build native_orion\build --config Release " +
          "--target OrionNative")
}

$needsDllCopy = -not (Test-Path -LiteralPath $AppDll)
if ($needsDllCopy -and -not (Test-Path -LiteralPath $DllSrc)) {
    Fail "VeniceNet.dll is neither next to OrionNative.exe ($AppDll) nor at the source build path ($DllSrc). Build it: cmake --build native_orion\build --config Release --target VeniceNet"
}

# MUTUAL EXCLUSION: refuse while the legacy Path-B service is registered.
& sc.exe query $LegacySvc | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "The legacy '$LegacySvc' service (Path B, the Python bridge) is registered." -ForegroundColor Red
    Write-Host "VeniceNetSvc and $LegacySvc both bind TCP 47291 - exactly ONE may exist at a time."
    Write-Host ""
    Write-Host "Roll the legacy service back first (elevated, one line at a time):"
    Write-Host "  sc.exe stop $LegacySvc"
    Write-Host "  sc.exe query $LegacySvc      (repeat until STATE shows STOPPED)"
    Write-Host "  sc.exe delete $LegacySvc"
    Write-Host ""
    Write-Host "That is the rollback documented in docs\OWNER_MANUAL_TEST.md, section 8."
    Fail "refusing to register $SvcName while $LegacySvc is registered"
}

# --- 2. Say exactly what will happen, then wait for one Y --------------------
Write-Host ""
Write-Host "=== VeniceNet (Path A) manual-test setup ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will do the following, and nothing else:"
Write-Host ""
# (No staging step - wave-2B binaries are built directly into native_orion\build\Release.)
if ($needsDllCopy) {
    Write-Host "  0b. Copy VeniceNet.dll next to OrionNative.exe (the app LoadLibrary's it from there)."
}
Write-Host "  1. If a service named '$SvcName' already exists: offer to sc.exe stop + sc.exe delete it."
Write-Host "  2. sc.exe create $SvcName binPath= `"$SvcExe`" start= demand type= own obj= LocalSystem"
Write-Host "     DisplayName= `"Venice Packet Service`""
Write-Host "  3. sc.exe config $SvcName binPath= `"$SvcExe --arm-meter-delay`"   (the arm flag)"
Write-Host "  4. Write ORION_METER_DELAY_ARMED=1 into the service's per-service Environment"
Write-Host "     registry value (HKLM\...\Services\$SvcName\Environment). The C++ service reads"
Write-Host "     the arm switch from its environment in service mode - the binPath flag alone"
Write-Host "     does not arm it (verified against venicenet_service\main.cpp runService)."
Write-Host "  5. sc.exe description + sc.exe sdset (grant Interactive Users start/stop, same as the installer)"
Write-Host ""
Write-Host "It will NOT start the service, NOT load the WinDivert driver, and NOT touch"
Write-Host "your live settings/logs under %LOCALAPPDATA%\NexusVision\Orion Native\."
Write-Host ""
$answer = Read-Host "Proceed? [Y/N]"
if ($answer -ne 'Y' -and $answer -ne 'y') {
    Write-Host "Nothing was changed."
    Read-Host "Press Enter to close"
    exit 0
}

# --- 3. (Stage step retired - wave-2B binaries live in build\Release already) --
# VeniceNet.dll is staged next to OrionNative.exe by the DLL target's own
# POST_BUILD copy, and the preflight above already fail-closes if either the
# exe lacks wiring or the DLL is missing. Nothing to copy here.
if ($needsDllCopy) {
    Copy-Item -LiteralPath $DllSrc -Destination $AppDll -Force
    Write-Host "Copied VeniceNet.dll next to OrionNative.exe (from $DllSrc)." -ForegroundColor Green
}
if (-not (Test-Path -LiteralPath $AppDll)) {
    Fail "VeniceNet.dll still missing at $AppDll"
}

# --- 4. Existing VeniceNetSvc? -----------------------------------------------
& sc.exe query $SvcName | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "A service named '$SvcName' is already registered:" -ForegroundColor Yellow
    & sc.exe qc $SvcName
    Write-Host ""
    $rm = Read-Host "Remove it and re-register against the dev build? [Y/N]"
    if ($rm -ne 'Y' -and $rm -ne 'y') {
        Write-Host "Left the existing service alone. Nothing else was changed."
        Read-Host "Press Enter to close"
        exit 0
    }
    & sc.exe stop $SvcName | Out-Null       # fine if it was not running; stop is asynchronous
    # sc.exe stop only REQUESTS the stop. Deleting while the service is still
    # running marks it "for deletion" and the sc.exe create below fails (1072).
    # Poll until the SCM reports STOPPED (or the service is gone), max ~10 s.
    $stopped = $false
    for ($i = 0; $i -lt 20; $i++) {
        $q = (& sc.exe query $SvcName | Out-String)
        if ($LASTEXITCODE -ne 0 -or $q -match "STATE\s*:.*STOPPED") { $stopped = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $stopped) {
        Fail ("service '$SvcName' did not reach STOPPED within 10 seconds. " +
              "Stop it manually from an elevated prompt (sc.exe stop $SvcName), wait for " +
              "'sc.exe query $SvcName' to show STOPPED, then re-run this script.")
    }
    & sc.exe delete $SvcName
    if ($LASTEXITCODE -ne 0) { Fail "sc.exe delete failed with exit code $LASTEXITCODE" }
    Start-Sleep -Milliseconds 600           # let the SCM finish the delete (installer does the same)
}

# --- 5. Register -------------------------------------------------------------
& sc.exe create $SvcName binPath= "$SvcExe" start= demand type= own obj= LocalSystem DisplayName= "Venice Packet Service"
if ($LASTEXITCODE -ne 0) { Fail "sc.exe create failed with exit code $LASTEXITCODE" }

# Arm flag baked into the ImagePath: mirrors the legacy registration and arms a
# developer's bare/debug launch of the exe. In SCM service mode however the C++
# service deliberately reads the arm switch from its ENVIRONMENT
# (ORION_METER_DELAY_ARMED), not from binPath args - so the registry value
# below is the actual service-mode arm. Both are set; the verify script
# confirms the service's own ARMED report before any batch.
& sc.exe config $SvcName binPath= "$SvcExe --arm-meter-delay"
if ($LASTEXITCODE -ne 0) { Fail "sc.exe config (arm flag) failed with exit code $LASTEXITCODE" }

try {
    New-ItemProperty -Path $SvcRegKey -Name "Environment" -PropertyType MultiString `
        -Value @("ORION_METER_DELAY_ARMED=1") -Force -ErrorAction Stop | Out-Null
} catch {
    Fail "could not write the per-service Environment arm value under $SvcRegKey : $($_.Exception.Message)"
}
$envCheck = (Get-ItemProperty -Path $SvcRegKey -Name "Environment" -ErrorAction SilentlyContinue).Environment
if ($envCheck -notcontains "ORION_METER_DELAY_ARMED=1") {
    Fail "the per-service Environment arm value did not read back from $SvcRegKey - the service would start DISARMED"
}

& sc.exe description $SvcName "Privileged WinDivert packet bridge for Venice (DEV-BUILD registration for the VeniceNet manual test). Lets the app apply the inbound meter delay without running as Administrator." | Out-Null

& sc.exe sdset $SvcName $Sddl
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: sc.exe sdset failed (exit $LASTEXITCODE). The service is registered and armed," -ForegroundColor Yellow
    Write-Host "but an unelevated console will NOT be able to start it - use an elevated 'sc.exe start $SvcName' instead." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Registered configuration:" -ForegroundColor Cyan
& sc.exe qc $SvcName
Write-Host "Per-service Environment: $($envCheck -join '; ')"

# --- 6. Done -----------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete. The service was NOT started." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps (test morning, in this order):"
Write-Host "  1. START THE SERVICE FIRST, before launching Orion:  sc.exe start $SvcName"
Write-Host "     (Unlike the legacy path, the app cannot demand-start the Venice-named"
Write-Host "     service - its auto-start only knows the legacy name - and if nothing owns"
Write-Host "     TCP 47291 when the app comes up, the app spawns the Python sniff-only debug"
Write-Host "     bridge, which then blocks VeniceNetSvc from binding the port.)"
Write-Host "  2. Launch the app the normal way: 'Launch Orion.local.bat' in the repo root."
Write-Host "     (Never start OrionNative.exe directly - the launcher sets the env it needs.)"
Write-Host "  3. Set Shot Lead back to 280-300 ms: Remote Play page -> Shot Lead card"
Write-Host "     (its own card, directly below the Meter Delay panel). It is currently 320,"
Write-Host "     which aborts every shot as unschedulable. Do NOT touch the Tip Timing card -"
Write-Host "     that is a different control with the opposite sign convention."
Write-Host "  4. Follow the batch recipe in docs\OWNER_MANUAL_TEST_VENICE.md."
Write-Host "  5. After enabling Meter Delay, confirm engagement with:"
Write-Host "     powershell -ExecutionPolicy Bypass -File `"$Root\scripts\owner_verify_venice.ps1`""
Write-Host "  6. In the log, look for: 'Meter delay service state: ARMED',"
Write-Host "     'Meter delay service echo: intercept ACTIVE ... [VeniceNet]',"
Write-Host "     'Meter delay condition: settled=1'."
Write-Host ""
Read-Host "Press Enter to close"
