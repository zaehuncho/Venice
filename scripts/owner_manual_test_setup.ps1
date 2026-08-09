# owner_manual_test_setup.ps1
#
# ONE-TIME setup for the meter-delay manual test (2026-08-08 work).
# Run it from an elevated PowerShell (open Windows Terminal or PowerShell
# "as Administrator" - there is no right-click "Run as administrator" entry
# for .ps1 files on this rig):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\aaron\Desktop\NexusVision\scripts\owner_manual_test_setup.ps1"
#
# It registers the NexusVisionSvc packet-bridge service against the DEV build,
# exactly the way installer/orion.iss registers it on a customer machine
# (same demand-start + LocalSystem + --arm-meter-delay + Interactive-Users
# start rights), but WITHOUT running the installer.
#
# It does NOT start the service, does NOT load the WinDivert driver, and
# does NOT touch anything under %LOCALAPPDATA%\NexusVision\Orion Native\.
#
# Companion doc: docs\OWNER_MANUAL_TEST.md
# Rollback (elevated, one line at a time):
#   sc.exe stop NexusVisionSvc
#   sc.exe delete NexusVisionSvc

$ErrorActionPreference = "Stop"

$Root      = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$AppExe    = Join-Path $Root "native_orion\build\Release\OrionNative.exe"
$SvcExe    = Join-Path $Root "build\service\nexus_svc.dist\NexusVisionSvc.exe"
$WdDll     = Join-Path $Root "build\service\nexus_svc.dist\pydivert\windivert_dll\WinDivert64.dll"
$WdSys     = Join-Path $Root "build\service\nexus_svc.dist\pydivert\windivert_dll\WinDivert64.sys"
$SvcName   = "NexusVisionSvc"
# Same DACL installer/orion.iss sets: SYSTEM/Admins full control, Interactive
# Users get START/STOP/QUERY so the UNELEVATED app can demand-start the service.
# Without this, "enable Meter Delay in the app" cannot start the service and
# only an elevated `sc.exe start` works.
$Sddl = 'D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWRPWPLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)'

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "SETUP STOPPED: $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# --- 0. Elevation ------------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail ("This script must run elevated. Open Windows Terminal or PowerShell " +
          "as Administrator, then run:`n" +
          "  powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`"")
}

# --- 1. Preflight: both binaries must exist ---------------------------------
if (-not (Test-Path -LiteralPath $AppExe)) {
    Fail "OrionNative.exe not found at $AppExe  (the Release build is missing - rebuild native_orion\build first)"
}
if (-not (Test-Path -LiteralPath $SvcExe)) {
    Fail "NexusVisionSvc.exe not found at $SvcExe  (rebuild with scripts\build_nexus_service.ps1 -Force)"
}
if (-not (Test-Path -LiteralPath $WdDll)) {
    Fail ("WinDivert64.dll missing at $WdDll - the service cannot open a packet handle without it. " +
          "Copy it from vendor\windivert\WinDivert64.dll (Nuitka drops DLLs from package data; " +
          "this happened once already on 2026-08-08).")
}
if (-not (Test-Path -LiteralPath $WdSys)) {
    Fail "WinDivert64.sys missing at $WdSys - copy it from vendor\windivert\WinDivert64.sys"
}
if ($SvcExe -match ' ') {
    Fail ("The service path contains a space ($SvcExe); the simple binPath registration below " +
          "would mis-parse. Move the repo to a space-free path or register manually with " +
          "escaped quotes (see installer/orion.iss RegisterPacketBridgeService).")
}

# --- 2. Say exactly what will happen, then wait for one Y --------------------
Write-Host ""
Write-Host "=== NexusVision meter-delay manual-test setup ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will do the following, and nothing else:"
Write-Host ""
Write-Host "  1. If a service named '$SvcName' already exists: offer to sc.exe stop + sc.exe delete it."
Write-Host "  2. sc.exe create $SvcName binPath= `"$SvcExe`" start= demand type= own obj= LocalSystem"
Write-Host "     DisplayName= `"NexusVision Packet Service`""
Write-Host "  3. sc.exe config $SvcName binPath= `"$SvcExe --arm-meter-delay`"   (the arm flag)"
Write-Host "  4. sc.exe description + sc.exe sdset (grant Interactive Users start/stop, same as the installer)"
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

# --- 3. Existing service? ----------------------------------------------------
& sc.exe query $SvcName | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "A service named '$SvcName' is already registered:" -ForegroundColor Yellow
    & sc.exe qc $SvcName
    Write-Host ""
    $rm = Read-Host "Remove it and re-register against the dev build? [Y/N]"
    if ($rm -ne 'Y' -and $rm -ne 'y') {
        Write-Host "Left the existing service alone. Nothing was changed."
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

# --- 4. Register -------------------------------------------------------------
& sc.exe create $SvcName binPath= "$SvcExe" start= demand type= own obj= LocalSystem DisplayName= "NexusVision Packet Service"
if ($LASTEXITCODE -ne 0) { Fail "sc.exe create failed with exit code $LASTEXITCODE" }

# Arm flag baked into the ImagePath: this IS the deliberate operator arm action.
# A service registered without it comes up DISARMED and says so in the app.
& sc.exe config $SvcName binPath= "$SvcExe --arm-meter-delay"
if ($LASTEXITCODE -ne 0) { Fail "sc.exe config (arm flag) failed with exit code $LASTEXITCODE" }

& sc.exe description $SvcName "Privileged WinDivert packet bridge for Venice (DEV-BUILD registration for the meter-delay manual test). Lets the app apply the inbound meter delay without running as Administrator." | Out-Null

& sc.exe sdset $SvcName $Sddl
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: sc.exe sdset failed (exit $LASTEXITCODE). The service is registered and armed," -ForegroundColor Yellow
    Write-Host "but the unelevated app will NOT be able to start it - use an elevated 'sc.exe start $SvcName' instead." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Registered configuration:" -ForegroundColor Cyan
& sc.exe qc $SvcName

# --- 5. Done -----------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete. Start the service when you want to test, either via" -ForegroundColor Green
Write-Host "'sc.exe start $SvcName' (admin) or by launching Orion and enabling Meter Delay" -ForegroundColor Green
Write-Host "(which will attempt to start it)." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Launch the app the normal way: 'Launch Orion.local.bat' in the repo root."
Write-Host "     (Never start OrionNative.exe directly - the launcher sets the env it needs.)"
Write-Host "  2. FIRST set Shot Lead back to 280-300 ms: Remote Play page -> Shot Lead card"
Write-Host "     (its own card, directly below the Meter Delay panel). It is currently 320,"
Write-Host "     which aborts every shot as unschedulable. Do NOT touch the Tip Timing card -"
Write-Host "     that is a different control with the opposite sign convention. (If you also"
Write-Host "     dragged Tip Timing to the floor last session, reset that first.)"
Write-Host "  3. Follow the batch recipe in docs\OWNER_MANUAL_TEST.md."
Write-Host "  4. After enabling Meter Delay, confirm engagement with:"
Write-Host "     powershell -ExecutionPolicy Bypass -File `"$Root\scripts\owner_verify_meter_delay.ps1`""
Write-Host "  5. In the log, look for: 'Meter delay service state: ARMED',"
Write-Host "     'Meter delay service echo: intercept ACTIVE', 'Meter delay condition: settled=1'."
Write-Host ""
Read-Host "Press Enter to close"
