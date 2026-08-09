# Pre-flight for the autonomous tip-timing live batch.
#
# Every check here exists because it has silently burned a batch before. Run it immediately before
# launching; it is read-only and takes a couple of seconds.
#
#   powershell -ExecutionPolicy Bypass -File scripts\preflight_tip_batch.ps1
#
# Exit code 0 = clear to launch. 1 = at least one BLOCK.

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$blocks = @()
$warns  = @()

function Ok   ($m) { Write-Host ("  [ OK ]   " + $m) -ForegroundColor Green }
function Block($m) { Write-Host ("  [BLOCK]  " + $m) -ForegroundColor Red;    $script:blocks += $m }
function Warn ($m) { Write-Host ("  [ warn]  " + $m) -ForegroundColor Yellow; $script:warns  += $m }

Write-Host ""
Write-Host "=== Orion tip-timing batch pre-flight ===" -ForegroundColor Cyan

# --- 1. capture device is free -------------------------------------------------------------
# An OBS dshow source OWNS the Elgato. The symptom is "no live device" / 0xC00D3704, a black
# preview and fresh=0 -- opens-but-no-frames means BUSY, not a wrong device index.
Write-Host "`n[1] Capture device"
$obs = Get-Process -Name obs64, obs32, obs -ErrorAction SilentlyContinue
if ($obs) { Block "OBS is running (pid $($obs.Id -join ',')) - it holds the Elgato. Close it." }
else      { Ok "no OBS process holding the capture card" }

# --- 2. ICS / console reachability ---------------------------------------------------------
# ICS binds Hyper-V vEthernet vNICs, never physical NICs. A GW-Lab VM with a PS5-Internal NIC
# steals 192.168.137.1 and kills all discovery/registration.
Write-Host "`n[2] Network (ICS + console)"
$ics = Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue
if (-not $ics) {
    Block "192.168.137.1 is not assigned - ICS is down; the console cannot be reached"
} else {
    $adapters = @($ics | ForEach-Object { (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue).Name })
    $adapters = @($adapters | Where-Object { $_ })
    if ($adapters.Count -gt 1) {
        Block "192.168.137.1 claimed by MULTIPLE adapters ($($adapters -join ', ')) - a VM is stealing it"
    } else {
        Ok "ICS host address on $($adapters[0])"
    }
}
# Reachability is the real proof the ICS path works, so it decides the adapter-name question too:
# a renamed vNIC that routes correctly is fine, and only an unreachable console makes the name
# worth questioning.
$consoleUp = Test-Connection -ComputerName 192.168.137.100 -Count 1 -Quiet -ErrorAction SilentlyContinue
if ($consoleUp) {
    Ok "console reachable at 192.168.137.100 (proves the ICS path routes)"
} else {
    Warn "192.168.137.100 not answering (fine if the PS5 is asleep - it MUST be up before you shoot)"
    if ($adapters -and $adapters[0] -notmatch 'vEthernet') {
        Warn "...and 192.168.137.1 is on '$($adapters[0])', not a vEthernet vNIC - suspect a VM stealing ICS"
    }
}

# --- 3. binaries current -------------------------------------------------------------------
Write-Host "`n[3] Build freshness"
$exe = Get-Item (Join-Path $root 'native_orion\build\Release\OrionNative.exe') -ErrorAction SilentlyContinue
if (-not $exe) {
    Block "OrionNative.exe missing - build Release first"
} else {
    $newest = Get-ChildItem (Join-Path $root 'native_orion\src') -Include *.cpp, *.h -Recurse |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($exe.LastWriteTime -lt $newest.LastWriteTime) {
        Block "OrionNative.exe is OLDER than $($newest.Name) - you would batch a stale binary"
    } else {
        Ok "OrionNative.exe newer than newest source ($($newest.Name))"
    }
}

# --- 4. latency prior ----------------------------------------------------------------------
# The engine actuates on this. A 241.4 mean here is the pre-fix artefact and reproduces the
# 8-of-10 abort session.
Write-Host "`n[4] Latency factory prior"
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$priorInfo = & $py -c @"
import json, sys
sys.path.insert(0, r'$root')
from latency_estimator import _FACTORY_PRIOR_PATH
d = json.load(open(_FACTORY_PRIOR_PATH))
means = sorted(set(p['mean_ms'] for p in d['profiles']))
print(d.get('model_version', '?'))
print(','.join(str(m) for m in means))
print(_FACTORY_PRIOR_PATH)
"@ 2>&1
$lines = @($priorInfo -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($lines.Count -lt 3) {
    Block "could not read the factory prior: $priorInfo"
} else {
    $ver = $lines[0]; $means = $lines[1]
    if ($means -eq '241.4') { Block "prior is the pre-fix uniform 241.4 across all routes" }
    elseif ($ver -notmatch '^sha256-') { Warn "prior model_version '$ver' is not content-derived - possible hand-edit" }
    else { Ok "prior $ver  means=$means" }
    # generator agreement
    & $py (Join-Path $root 'tools\timing\build_latency_factory_prior.py') --check *> $null
    if ($LASTEXITCODE -eq 0) { Ok "prior is byte-identical to a fresh regeneration" }
    else { Warn "prior does NOT match its generator (--check failed) - it has been hand-edited" }
}

# --- 5. measurement instrumentation --------------------------------------------------------
# DETDIAG (log lines) and DETCSV (detframes.csv) are INDEPENDENT gates. Setting only the first
# makes the usual liveness grep pass while the CSV stays stale -- this is exactly how the
# 2026-08-03 batch was lost.
Write-Host "`n[5] Measurement instrumentation"
$launcher = Get-Content (Join-Path $root 'run_orion.local.ps1') -Raw
if ($launcher -match 'ORION_DETCSV\s*=\s*"1"') { Ok "-Detdiag sets ORION_DETCSV (detframes.csv will be written)" }
else { Block "run_orion.local.ps1 never sets ORION_DETCSV - detframes.csv stays empty and the batch yields no residuals" }
if ($launcher -match 'if \(-not \$env:ORION_FRAMEDUMP_MAX\)') { Ok "-Framedump respects pre-set INTERVAL/MAX" }
else { Warn "-Framedump may clobber operator-set ORION_FRAMEDUMP_INTERVAL/MAX" }
if ($launcher -match 'ORION_FILL_KALMAN\s*=\s*"1"') { Ok "ORION_FILL_KALMAN enabled (kalmanTipMs telemetry)" }
else { Warn "ORION_FILL_KALMAN not set in the launcher - export it manually to grade Kalman vs sampler" }

$det = Join-Path $root 'logs\diagnostics\detframes.csv'
if (Test-Path $det) {
    $age = (New-TimeSpan -Start (Get-Item $det).LastWriteTime -End (Get-Date)).TotalDays
    Warn ("detframes.csv exists and is {0:N1} days old ({1} bytes) - archive it, then confirm it GROWS after launch" -f $age, (Get-Item $det).Length)
} else { Ok "no stale detframes.csv" }

# --- 6. log emitters present ---------------------------------------------------------------
# A checklist whose greps silently match nothing is worse than no checklist.
Write-Host "`n[6] Diagnostic emitters"
$emitters = @{
  'TIP RESERVATION:'         = 'native_orion\src\AutomationEngine.cpp'
  'reservation_disposition=' = 'native_orion\src\AutomationEngine.cpp'
  'TIP DEADLINE DECISION:'   = 'native_orion\src\AutomationEngine.cpp'
  'READER ACQUIRE'           = 'simple_meter_reader.py'
  'fillKalman'               = 'remote_play_orchestrator.py'
}
foreach ($pat in $emitters.Keys) {
    $f = Join-Path $root $emitters[$pat]
    if ((Test-Path $f) -and (Select-String -Path $f -SimpleMatch -Pattern $pat -Quiet)) { Ok "'$pat' is emitted" }
    else { Block "'$pat' is NOT emitted by $($emitters[$pat]) - the post-batch grep would match nothing" }
}

# --- 7. disk headroom ----------------------------------------------------------------------
Write-Host "`n[7] Disk"
$drive = Get-PSDrive -Name ((Get-Item $root).PSDrive.Name)
$freeGb = [math]::Round($drive.Free / 1GB, 1)
if ($freeGb -lt 5) { Block "only ${freeGb} GB free - framedump PNGs will fill the disk mid-batch" }
elseif ($freeGb -lt 20) { Warn "${freeGb} GB free - a long framedump batch may be tight" }
else { Ok "${freeGb} GB free" }

# --- verdict -------------------------------------------------------------------------------
Write-Host ""
if ($blocks.Count -gt 0) {
    Write-Host "=== NOT CLEAR TO LAUNCH - $($blocks.Count) blocking issue(s) ===" -ForegroundColor Red
    $blocks | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "=== CLEAR TO LAUNCH ===" -ForegroundColor Green
if ($warns.Count -gt 0) {
    Write-Host "$($warns.Count) warning(s) - read them, they are not automatically fine:" -ForegroundColor Yellow
    $warns | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
}
Write-Host ""
Write-Host "Launch:  .\run_orion.local.ps1 -Framedump -Detdiag"
Write-Host "Then confirm BOTH:  log has DETDIAG lines  AND  logs\diagnostics\detframes.csv is growing."
exit 0
