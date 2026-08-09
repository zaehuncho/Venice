# owner_verify_venice.ps1
#
# READ-ONLY check that the VeniceNet (Path A) meter delay is actually on the
# wire.  No elevation needed.  It queries the services and tails the live
# logs; it never writes, rotates, deletes, or otherwise touches anything.
#
# LOG LOCATION: the build under test is the DEV build (ORION_PRODUCTION off),
# which writes its log beside the repo at <repo>\logs\orion_native.log.  That
# is the default below.  (The INSTALLED production app logs under
# %LOCALAPPDATA%\NexusVision\Orion Native\ - that is NOT the file this script
# reads, and this script never touches that directory.)
#
# The VeniceNet chain under test: OrionNative.exe LoadLibrary's VeniceNet.dll,
# the DLL keeps a loopback-TCP connection to VeniceNetSvc (C++ service,
# 127.0.0.1:47291), the service intercepts packets via WinDivert.  The truth
# lines this script matches in orion_native.log:
#   service armed        -> "Meter delay service state: ARMED (hello features=[meter_delay])"
#   DLL echo, intercept  -> "Meter delay service echo: intercept ACTIVE (applying N ms) [VeniceNet]"
#                           (the [VeniceNet] tag is emitted only by the wave-2B
#                            DLL snapshot path - it doubles as the "DLL loaded
#                            and driving" proof)
#   settled              -> "Meter delay condition: settled=1 key=...ms epoch=..."
# The service also writes its own log next to its exe (venicenet_svc.log);
# this script reads it for the service's first-person ARMED/DISARMED report
# and for the "Cannot bind 127.0.0.1:47291" port-conflict line.
#
# The checks come in two groups:
#   PRE-BATCH  - run right after enabling Meter Delay, BEFORE shooting:
#                VeniceNetSvc RUNNING, legacy service absent, WinDivert driver
#                RUNNING, VeniceNet.dll staged, service ARMED, echo intercept
#                ACTIVE via [VeniceNet].  Any FAIL here = the delay is not
#                engaging; fix it before you shoot, or the batch is worthless.
#   MID-BATCH  - run again after at least 5 shots:
#                settled=1, Outcome identity w/ armed_source=, vel= below 0.15.
#                FAILs here before any shots were fired are expected - these
#                signals need shot traffic in the log.

param(
    [int]$TailLines = 5000,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    # DEV build: orionDataDir() resolves to the repo root, so the log lives at
    # <repo>\logs\orion_native.log (NOT under %LOCALAPPDATA% - that is the
    # installed production app's location).
    $LogPath = Join-Path $Root "logs\orion_native.log"
}

$AppDll     = Join-Path $Root "native_orion\build\Release\VeniceNet.dll"
$SvcLogPath = Join-Path $Root "native_orion\build\venicenet_service\Release\venicenet_svc.log"

$results = New-Object System.Collections.ArrayList

function Add-Result([string]$group, [string]$name, [bool]$pass, [string]$detail) {
    [void]$results.Add([pscustomobject]@{ Group = $group; Signal = $name; Pass = $pass; Detail = $detail })
}

Write-Host ""
Write-Host "=== VeniceNet meter delay engagement check (read-only) ===" -ForegroundColor Cyan
Write-Host ""

# --- 1. VeniceNetSvc RUNNING (pre-batch) --------------------------------------
$svcOut = & sc.exe query VeniceNetSvc
if ($LASTEXITCODE -ne 0) {
    Add-Result "pre" "VeniceNetSvc service" $false "not registered (sc.exe query exit $LASTEXITCODE) - run scripts\owner_venice_setup.ps1 first"
} elseif (($svcOut | Out-String) -match "RUNNING") {
    Add-Result "pre" "VeniceNetSvc service" $true "RUNNING"
} else {
    $state = (($svcOut | Out-String) -split "`n" | Where-Object { $_ -match "STATE" } | Select-Object -First 1)
    if ($null -eq $state) { $state = "unknown state" }
    Add-Result "pre" "VeniceNetSvc service" $false ("registered but not running: " + $state.Trim() + " - start it BEFORE launching Orion: sc.exe start VeniceNetSvc")
}

# --- 2. Mutual exclusion: legacy NexusVisionSvc must be gone (pre-batch) ------
& sc.exe query NexusVisionSvc | Out-Null
if ($LASTEXITCODE -ne 0) {
    Add-Result "pre" "legacy NexusVisionSvc absent" $true "not registered"
} else {
    Add-Result "pre" "legacy NexusVisionSvc absent" $false ("the legacy Path-B service is still registered - both bridges bind TCP 47291, exactly one may exist. " +
        "Roll it back per docs\OWNER_MANUAL_TEST.md section 8 (sc.exe stop NexusVisionSvc, wait for STOPPED, sc.exe delete NexusVisionSvc)")
}

# --- 3. WinDivert kernel driver loaded (pre-batch) ----------------------------
$wdOut = & sc.exe query WinDivert
if ($LASTEXITCODE -ne 0) {
    Add-Result "pre" "WinDivert driver" $false "not installed/loaded (sc.exe query WinDivert exit $LASTEXITCODE) - the service loads it on demand when the intercept opens"
} elseif (($wdOut | Out-String) -match "RUNNING") {
    Add-Result "pre" "WinDivert driver" $true "RUNNING"
} else {
    Add-Result "pre" "WinDivert driver" $false "present but not RUNNING - the service has not opened a packet handle yet (or AV blocked the driver load)"
}

# --- 4. VeniceNet.dll staged next to the app (pre-batch) ----------------------
if (Test-Path -LiteralPath $AppDll) {
    Add-Result "pre" "VeniceNet.dll staged" $true "present next to OrionNative.exe"
} else {
    Add-Result "pre" "VeniceNet.dll staged" $false "missing at $AppDll - re-run scripts\owner_venice_setup.ps1 (the app degrades to no-backend without it)"
}

# --- 5. Service's own log: first-person ARMED report (pre-batch) --------------
if (-not (Test-Path -LiteralPath $SvcLogPath)) {
    Add-Result "pre" "service log ARMED" $false "no venicenet_svc.log at $SvcLogPath - the service has never run (start it: sc.exe start VeniceNetSvc)"
    $svcTail = @()
} else {
    $svcTail = Get-Content -LiteralPath $SvcLogPath -Tail 200 -Encoding UTF8
    $svcArmed    = $svcTail | Where-Object { $_ -match "INBOUND METER DELAY ARMED" }          | Select-Object -Last 1
    $svcDisarmed = $svcTail | Where-Object { $_ -match "Inbound meter delay DISARMED" }       | Select-Object -Last 1
    $svcBindFail = $svcTail | Where-Object { $_ -match "Cannot bind 127\.0\.0\.1:47291" }     | Select-Object -Last 1
    # The log accumulates across runs; judge by whichever arm-state line is LAST.
    $armedIdx    = if ($svcArmed)    { [array]::LastIndexOf($svcTail, $svcArmed) }    else { -1 }
    $disarmedIdx = if ($svcDisarmed) { [array]::LastIndexOf($svcTail, $svcDisarmed) } else { -1 }
    if ($armedIdx -ge 0 -and $armedIdx -gt $disarmedIdx) {
        Add-Result "pre" "service log ARMED" $true ($svcArmed.Trim())
    } elseif ($disarmedIdx -ge 0) {
        Add-Result "pre" "service log ARMED" $false "the service's own log says DISARMED - the per-service Environment arm value is missing; re-run scripts\owner_venice_setup.ps1"
    } else {
        Add-Result "pre" "service log ARMED" $false "no arm-state line in the last 200 lines of venicenet_svc.log"
    }
    if ($svcBindFail) {
        Add-Result "pre" "port 47291 free for the service" $false ("service logged: " + $svcBindFail.Trim() + " - another bridge owned the port when it started (the app's Python debug fallback, or the legacy service). Stop the other bridge, restart VeniceNetSvc, and start the service BEFORE the app next time")
    }
}

# --- 6. App-log signals -------------------------------------------------------
if (-not (Test-Path -LiteralPath $LogPath)) {
    Add-Result "pre" "log file" $false "not found at $LogPath"
    Add-Result "mid" "log file" $false "not found at $LogPath"
    $tail = @()
} else {
    # Read-only tail. The log contains stray binary bytes; UTF8 decoding turns
    # them into replacement garbage, which is fine - every line we match is ASCII.
    $tail = Get-Content -LiteralPath $LogPath -Tail $TailLines -Encoding UTF8

    # 6a. (pre-batch) Service reports itself ARMED on the current connection
    #     (hello features=[meter_delay], logged by the app). ARMED is
    #     transition-deduped: it logs once per service connection near session
    #     start, hence the deep default tail.
    $armed    = $tail | Where-Object { $_ -match "Meter delay service state: ARMED" }    | Select-Object -Last 1
    $disarmed = $tail | Where-Object { $_ -match "Meter delay service state: DISARMED" } | Select-Object -Last 1
    if ($armed) {
        Add-Result "pre" "service ARMED (app log)" $true "seen in tail"
    } elseif ($disarmed) {
        Add-Result "pre" "service ARMED (app log)" $false "service reports DISARMED - it started without the arm environment value (re-run setup), or the app connected to an unarmed bridge (Python debug fallback?)"
    } else {
        Add-Result "pre" "service ARMED (app log)" $false "no 'Meter delay service state: ARMED' in the last $TailLines lines"
    }

    # 6b. (pre-batch) The DLL's applied-delay echo. The [VeniceNet] tag is only
    #     emitted by the wave-2B DLL snapshot path, so this single line proves
    #     DLL loaded + connected + intercept active.
    $echoActive   = $tail | Where-Object { $_ -match "Meter delay service echo: intercept ACTIVE" -and $_ -match "\[VeniceNet\]" }   | Select-Object -Last 1
    $echoLegacy   = $tail | Where-Object { $_ -match "Meter delay service echo: intercept ACTIVE" -and $_ -notmatch "\[VeniceNet\]" } | Select-Object -Last 1
    $echoInactive = $tail | Where-Object { $_ -match "Meter delay service echo: intercept inactive" } | Select-Object -Last 1
    if ($echoActive) {
        Add-Result "pre" "delay ACTIVE (VeniceNet echo)" $true ($echoActive.Trim())
    } elseif ($echoLegacy) {
        Add-Result "pre" "delay ACTIVE (VeniceNet echo)" $false "an ACTIVE echo exists but WITHOUT the [VeniceNet] tag - the app under test is not the VeniceNet-wired build (re-run setup; it stages the wired exe)"
    } elseif ($echoInactive) {
        Add-Result "pre" "delay ACTIVE (VeniceNet echo)" $false "last echo says intercept inactive - session gates not open (court IP known? Remote Play running?)"
    } else {
        Add-Result "pre" "delay ACTIVE (VeniceNet echo)" $false "no service echo in the last $TailLines lines - DLL not loaded yet, service not running, or Meter Delay not enabled"
    }

    # 6c. (mid-batch) The delay reached its target and held.
    $settled = $tail | Where-Object { $_ -match "Meter delay condition: settled=1" } | Select-Object -Last 1
    if ($settled) {
        Add-Result "mid" "delay settled" $true ($settled.Trim())
    } else {
        Add-Result "mid" "delay settled" $false "no 'Meter delay condition: settled=1' in the last $TailLines lines (no shots yet, still ramping, or never engaged)"
    }

    # 6d. (mid-batch) Recent Outcome identity with the armed_source attribution field.
    $outcome = $tail | Where-Object { $_ -match "Outcome identity:" -and $_ -match "armed_source=" } | Select-Object -Last 1
    if ($outcome) {
        Add-Result "mid" "Outcome identity w/ armed_source" $true ($outcome.Trim())
    } else {
        Add-Result "mid" "Outcome identity w/ armed_source" $false "no recent 'Outcome identity:' line carrying armed_source= (no shots yet, or an old exe without the attribution field)"
    }

    # 6e. (mid-batch) Any vel= reading below 0.15 %/ms (baseline ~0.226; a drop = engagement).
    $vels = @()
    foreach ($line in ($tail | Where-Object { $_ -match "Release attribution:" })) {
        if ($line -match "vel=([0-9]+\.[0-9]+)") { $vels += [double]$Matches[1] }
    }
    if ($vels.Count -eq 0) {
        Add-Result "mid" "vel= below 0.15" $false "no 'Release attribution: ... vel=' lines in the last $TailLines lines - take a few shots first (or re-run with a larger -TailLines)"
    } else {
        $minVel = ($vels | Measure-Object -Minimum).Minimum
        $detail = "$($vels.Count) readings, min=$minVel (baseline ~0.226)"
        if ($minVel -lt 0.15) {
            Add-Result "mid" "vel= below 0.15" $true $detail
        } else {
            Add-Result "mid" "vel= below 0.15" $false ($detail + " - meter speed unchanged, the delay is not reaching the console")
        }
    }
}

# --- 7. Summary ---------------------------------------------------------------
function Show-Group([string]$title, [object[]]$rows) {
    Write-Host ""
    Write-Host $title -ForegroundColor Cyan
    Write-Host ("{0,-36} {1,-6} {2}" -f "SIGNAL", "RESULT", "DETAIL")
    Write-Host ("{0,-36} {1,-6} {2}" -f "------", "------", "------")
    $ok = $true
    foreach ($r in $rows) {
        if ($r.Pass) {
            Write-Host ("{0,-36} {1,-6} {2}" -f $r.Signal, "PASS", $r.Detail) -ForegroundColor Green
        } else {
            $ok = $false
            Write-Host ("{0,-36} {1,-6} {2}" -f $r.Signal, "FAIL", $r.Detail) -ForegroundColor Red
        }
    }
    return $ok
}

$preRows = @($results | Where-Object { $_.Group -eq "pre" })
$midRows = @($results | Where-Object { $_.Group -eq "mid" })
$preOk = Show-Group "PRE-BATCH checks (run right after enabling Meter Delay, before shooting):" $preRows
$midOk = Show-Group "MID-BATCH checks (run after at least 5 shots):" $midRows

Write-Host ""
if ($preOk -and $midOk) {
    Write-Host "ALL PASS - the delay is on the wire via VeniceNet and shots are being attributed. This batch's data is valid." -ForegroundColor Green
} elseif (-not $preOk) {
    Write-Host "PRE-BATCH FAIL - the delay is NOT engaging. Fix this before shooting; do not credit the batch." -ForegroundColor Red
    Write-Host "(Did you run this before enabling Meter Delay in the app? Enable it first, then re-run.)"
    Write-Host "Troubleshooting is in docs\OWNER_MANUAL_TEST_VENICE.md, section 7."
} else {
    Write-Host "PRE-BATCH all PASS, MID-BATCH has FAILs - have you fired shots yet?" -ForegroundColor Yellow
    Write-Host "If not, take at least 5 shots and re-run. If you have, this is a batch-collection"
    Write-Host "issue - re-run after a few more shots before condemning the batch"
    Write-Host "(docs\OWNER_MANUAL_TEST_VENICE.md, section 4)."
}

# --- 8. Context: last few meter-delay lines from both logs --------------------
if ($tail.Count -gt 0) {
    Write-Host ""
    Write-Host "Last meter-delay lines in the app log (context):" -ForegroundColor Cyan
    $tail | Where-Object { $_ -match "Meter delay" } | Select-Object -Last 6 | ForEach-Object { Write-Host ("  " + $_.Trim()) }
}
if ($svcTail.Count -gt 0) {
    Write-Host ""
    Write-Host "Last lines of the service's own log (context):" -ForegroundColor Cyan
    $svcTail | Select-Object -Last 6 | ForEach-Object { Write-Host ("  " + $_.Trim()) }
}
Write-Host ""
Read-Host "Press Enter to close"
