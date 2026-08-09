# demo_preflight.ps1 - CHECK-ONLY rig verification for the Orion demo (2026-08-08).
#
# Verifies and REPORTS the machine conditions that have historically degraded shot timing
# silently. It NEVER changes anything: every failing item prints the fix as text for the
# operator to run deliberately. Run it ~10 minutes before a counted batch:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\demo_preflight.ps1
#
# Run elevated for the complete set (process-elevation + Hyper-V checks degrade gracefully
# without admin). Windows PowerShell 5.1 compatible.

$ErrorActionPreference = 'SilentlyContinue'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$script:warnCount = 0
function Report {
    param([string]$Status, [string]$Item, [string]$Detail, [string]$Fix = '')
    $color = 'Green'
    if ($Status -eq 'WARN') { $color = 'Yellow'; $script:warnCount++ }
    if ($Status -eq 'INFO') { $color = 'Cyan' }
    Write-Host ("[{0}] {1}" -f $Status.PadRight(4), $Item) -ForegroundColor $color
    if ($Detail) { Write-Host ("       {0}" -f $Detail) }
    if ($Fix)    { Write-Host ("       FIX: {0}" -f $Fix) -ForegroundColor DarkGray }
}

Write-Host "=== Orion demo preflight (check-only, changes nothing) ===" -ForegroundColor White
Write-Host ("    {0}   repo: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $RepoRoot)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Report 'INFO' 'Shell is NOT elevated' 'Elevation/Hyper-V checks will be partial. Re-run as admin for the full set.'
}

# --------------------------------------------------------------------------------------------
# 1. Capture card holders (OBS et al). OBS holding the Elgato = opens-but-no-frames:
#    black preview, fresh=0, "no live device"/0xC00D3704. Busy, not a wrong index.
# --------------------------------------------------------------------------------------------
$holders = Get-Process -Name 'obs64','obs32','4KCaptureUtility','CameraHub','Camera Hub','EpocCam','vMix','XSplit*' -ErrorAction SilentlyContinue
if ($holders) {
    $names = ($holders | Select-Object -ExpandProperty Name -Unique) -join ', '
    Report 'WARN' 'Capture-card contender process running' ("{0} - if its DirectShow source is active it OWNS the Elgato and the sidecar reads black/fresh=0." -f $names) `
        'Close OBS (or deactivate its Elgato source) BEFORE launching Orion; if the sidecar already latched a dead route, relaunch via run_orion.local.ps1.'
} else {
    Report 'PASS' 'No known capture-card contender running (OBS/4KCaptureUtility/CameraHub/vMix/XSplit)'
}

# --------------------------------------------------------------------------------------------
# 2. OrionNative + python sidecar liveness (and elevation when we can see it)
# --------------------------------------------------------------------------------------------
$orion = Get-Process -Name 'OrionNative' -ErrorAction SilentlyContinue
if ($orion) {
    $elevNote = 'elevation not verifiable from this shell'
    if ($isAdmin) {
        $sig = @'
using System;
using System.Runtime.InteropServices;
public static class TokenElev {
    [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr h, uint acc, out IntPtr tok);
    [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr tok, int cls, out int info, int len, out int ret);
    public static int IsElevated(IntPtr proc) {
        IntPtr tok; int info; int ret;
        if (!OpenProcessToken(proc, 0x0008, out tok)) return -1;
        if (!GetTokenInformation(tok, 20, out info, 4, out ret)) return -1;
        return info;
    }
}
'@
        try { Add-Type -TypeDefinition $sig -ErrorAction Stop } catch {}
        $e = [TokenElev]::IsElevated($orion[0].Handle)
        if ($e -eq 1) { $elevNote = 'ELEVATED (correct: WinDivert needs admin)' }
        elseif ($e -eq 0) { $elevNote = 'NOT elevated - packet bridge will die' }
    }
    Report 'PASS' ("OrionNative running (pid {0})" -f $orion[0].Id) $elevNote `
        $(if ($elevNote -like 'NOT elevated*') { 'Close with the window X (NEVER force-kill: a kill mid-write zeroes actuation_lead_ms) and relaunch via run_orion.local.ps1, accept UAC.' } else { '' })
} else {
    Report 'INFO' 'OrionNative not running' 'Fine if you have not launched yet. ALWAYS launch via run_orion.local.ps1 (direct OrionNative.exe drops ~19 env keys and does not elevate).'
}
$sidecars = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'autogreen|orchestrator|sidecar|nexus' }
if ($sidecars) {
    Report 'PASS' ("Python sidecar running (pid {0})" -f (($sidecars | Select-Object -ExpandProperty ProcessId) -join ','))
} else {
    Report 'INFO' 'No python sidecar process' 'Expected before launch. An ORPHAN sidecar after closing Orion is the historical Elgato-holder trap; the launcher now reaps it with the 2.5s settle wait.'
}

# --------------------------------------------------------------------------------------------
# 3. Power plan
# --------------------------------------------------------------------------------------------
$scheme = powercfg /getactivescheme
if ($scheme -match '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c' -or $scheme -match 'e9a42b02-d5df-448d-aa00-03f14749eb61' -or $scheme -match 'High performance|Ultimate') {
    Report 'PASS' 'Power plan is High/Ultimate performance' ($scheme -replace '^Power Scheme GUID:\s*','')
} else {
    Report 'WARN' 'Power plan is NOT High performance' ($scheme -replace '^Power Scheme GUID:\s*','') `
        'powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
}

# --------------------------------------------------------------------------------------------
# 4. USB selective suspend (scheme-level, AC) - can power-manage the Elgato mid-capture
# --------------------------------------------------------------------------------------------
$usbq = powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226
$acLine = ($usbq | Where-Object { $_ -match 'Current AC Power Setting Index' } | Select-Object -First 1)
if ($acLine -match '0x0+$' -or $acLine -match '0x00000000') {
    Report 'PASS' 'USB selective suspend (AC) disabled'
} elseif ($acLine) {
    Report 'WARN' 'USB selective suspend (AC) is ENABLED' $acLine.Trim() `
        'powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 ; powercfg /setactive SCHEME_CURRENT'
} else {
    Report 'INFO' 'USB selective suspend state not readable' 'Check manually: powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3'
}

# --------------------------------------------------------------------------------------------
# 5. HAGS + Game Mode (reported, not judged - no measured direction on this rig)
# --------------------------------------------------------------------------------------------
$hags = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers' -Name HwSchMode -ErrorAction SilentlyContinue).HwSchMode
$hagsState = 'driver default (value absent)'
if ($hags -eq 2) { $hagsState = 'ENABLED' } elseif ($hags -eq 1) { $hagsState = 'DISABLED' }
Report 'INFO' ("Hardware-Accelerated GPU Scheduling: {0}" -f $hagsState) 'Change only deliberately (Settings > Display > Graphics > Default settings); needs reboot.'
$gm = Get-ItemProperty 'HKCU:\Software\Microsoft\GameBar' -ErrorAction SilentlyContinue
$gmState = 'default (ON in Win11)'
if ($gm -and $null -ne $gm.AutoGameModeEnabled) { if ($gm.AutoGameModeEnabled -eq 1) { $gmState = 'ON' } else { $gmState = 'OFF' } }
Report 'INFO' ("Game Mode: {0}" -f $gmState) 'Game Mode boosts the FOREGROUND game process; Orion is not a game - if the batch feels starved, toggle in Settings > Gaming > Game Mode.'

# --------------------------------------------------------------------------------------------
# 6. Kernel timer resolution + the per-process trap (the fire-jitter root cause, 2026-08-06)
# --------------------------------------------------------------------------------------------
$sig2 = @'
using System;
using System.Runtime.InteropServices;
public static class TimerRes {
    [DllImport("ntdll.dll")] public static extern int NtQueryTimerResolution(out uint min, out uint max, out uint cur);
    [DllImport("kernel32.dll")] public static extern void Sleep(uint ms);
}
'@
try { Add-Type -TypeDefinition $sig2 -ErrorAction Stop } catch {}
$mn = [uint32]0; $mx = [uint32]0; $cu = [uint32]0
[void][TimerRes]::NtQueryTimerResolution([ref]$mn, [ref]$mx, [ref]$cu)
$curMs = $cu / 10000.0
# Probe THIS (default-resolution) process's actual Sleep quantum:
$sw = [System.Diagnostics.Stopwatch]::StartNew(); for ($i = 0; $i -lt 20; $i++) { [TimerRes]::Sleep(1) }; $sw.Stop()
$quantum = $sw.Elapsed.TotalMilliseconds / 20.0
Report 'INFO' ("Kernel timer resolution: current={0:N3} ms (global); this default process quantum ~{1:N1} ms" -f $curMs, $quantum) `
    'Windows 11 grants 1ms PER PROCESS and IGNORES the request while a window is occluded/minimized. A global 1.000 here does NOT prove OrionNative gets it.'
Report 'INFO' 'Fire-wakeup protection during the batch' `
    'Keep the Orion window VISIBLE (not minimized, not fully covered) for the whole batch - its timeBeginPeriod(1) is ignored while occluded (measured 4-15.6ms release-submit tail). Belt+braces: set ORION_PRECISE_WAIT_HIRES=1 and ORION_TIMER_RES_GUARD=1 in the launcher env (default-OFF flags, 2026-08-06).'

# --------------------------------------------------------------------------------------------
# 7. ICS / vEthernet for the PS5 bridge (192.168.137.1 must belong to a host vEthernet)
# --------------------------------------------------------------------------------------------
$ics = Get-NetIPAddress -IPAddress '192.168.137.1' -ErrorAction SilentlyContinue
if ($ics) {
    $first = $ics | Select-Object -First 1
    $alias = $first.InterfaceAlias
    $desc = (Get-NetAdapter -InterfaceIndex $first.InterfaceIndex -ErrorAction SilentlyContinue).InterfaceDescription
    # The killer trap is the HOST not owning .1 (GW-Lab VM stole it) - any local adapter owning
    # it means the bridge is plumbed. Report which adapter for topology drift awareness.
    Report 'PASS' ("192.168.137.1 owned by this host: '{0}' ({1})" -f $alias, $desc)
    if (-not ($alias -match 'vEthernet' -or $desc -match 'Hyper-V Virtual')) {
        Report 'INFO' 'Note: .1 sits on a non-Hyper-V adapter' 'Earlier topology had ICS on a vEthernet vNIC. Fine if the PS5 is wired to this NIC and the .100 neighbor shows below.'
    }
} else {
    Report 'WARN' 'No local adapter owns 192.168.137.1' 'ICS gateway missing OR the GW-Lab VM stole it -> all PS5 discovery/registration dies.' `
        'If GW-Lab VM is running: disconnect its PS5-Internal NIC (Hyper-V Manager > GW-Lab > Settings > Network Adapter > Not connected), then re-enable ICS on the host adapter.'
}
if ($isAdmin) {
    $gwlab = Get-VM -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'GW.?Lab' }
    if ($gwlab) {
        foreach ($vm in $gwlab) {
            if ($vm.State -eq 'Running') {
                $nics = Get-VMNetworkAdapter -VM $vm | Where-Object { $_.SwitchName -match 'PS5|Internal' -and $_.Connected }
                if ($nics) {
                    Report 'WARN' ("GW-Lab VM '{0}' RUNNING with connected NIC on '{1}'" -f $vm.Name, (($nics | Select-Object -ExpandProperty SwitchName) -join ',')) `
                        'This VM steals 192.168.137.1 and kills PS5 discovery.' `
                        ('Disconnect-VMNetworkAdapter -VMName "{0}"  (or Hyper-V Manager > Settings > Network Adapter > Not connected)' -f $vm.Name)
                } else {
                    Report 'PASS' ("GW-Lab VM running but no connected PS5-Internal NIC")
                }
            } else {
                Report 'PASS' ("GW-Lab VM present, state={0}" -f $vm.State)
            }
        }
    } else {
        Report 'PASS' 'No GW-Lab VM found (or Hyper-V module absent)'
    }
}
$ps5 = arp -a | Select-String '192\.168\.137\.' | Select-String -NotMatch '192\.168\.137\.1 |192\.168\.137\.255'
if ($ps5) {
    Report 'PASS' ("PS5-side neighbor present on the 137 subnet: {0}" -f (($ps5 | Select-Object -First 1).ToString().Trim()))
} else {
    Report 'INFO' 'No 192.168.137.x neighbor in ARP yet' 'Normal if the PS5 is asleep / not yet connected. It should DHCP to .100 once Remote Play starts.'
}

# --------------------------------------------------------------------------------------------
# 8. settings.json armed flags + lead sanity (READ-ONLY)
# --------------------------------------------------------------------------------------------
$settingsPath = Join-Path $RepoRoot 'settings.json'
$settings = $null
if (Test-Path $settingsPath) { $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json }
if ($settings) {
    foreach ($flag in 'ownership_proof_two_frame','tip_phase_anchor_base20') {
        $v = $settings.$flag
        if ($v -eq $true) {
            Report 'PASS' ("settings.json {0} = true (armed for the counted batch)" -f $flag)
        } else {
            Report 'WARN' ("settings.json {0} = {1}" -f $flag, $v) 'Batch plan (section 10 of the pickup prompt) expects BOTH candidate flags armed. Do not flip mid-session.' `
                'Edit settings.json deliberately while Orion is CLOSED (backup exists: settings.json.pre_batch_20260805).'
        }
    }
    $lead = $settings.actuation_lead_ms
    $userSet = $settings.actuation_lead_user_set
    if ($lead -eq 300 -and $userSet -eq $true) {
        Report 'PASS' 'actuation_lead_ms = 300 (user-set) - sane'
    } elseif ($lead -eq 218.5 -or $lead -eq 0 -or $userSet -ne $true) {
        Report 'WARN' ("actuation_lead_ms = {0}, user_set = {1} - CORRUPTION SIGNATURE" -f $lead, $userSet) `
            'A force-kill mid-write zeroes the lead; the bot then fires on the 218.5 factory prior and every shot reads late.' `
            'With Orion CLOSED, restore actuation_lead_ms=300 and actuation_lead_user_set=true in settings.json.'
    } else {
        Report 'INFO' ("actuation_lead_ms = {0}, user_set = {1} (non-standard; confirm intentional)" -f $lead, $userSet)
    }
} else {
    Report 'WARN' 'settings.json not readable' '' 'Verify the repo path; do not launch until it exists.'
}

# --------------------------------------------------------------------------------------------
# 9. Recent session poison markers in the live log (informational)
# --------------------------------------------------------------------------------------------
$log = Join-Path $RepoRoot 'logs\orion_native.log'
if (Test-Path $log) {
    $tail = Get-Content $log -Tail 4000
    $mismatch = ($tail | Select-String 'capture_route_mismatch').Count
    $rejected = ($tail | Select-String 'route_scope_rejected').Count
    $revoked = $tail | Select-String 'warm timing revoked' | Select-Object -Last 1
    if ($mismatch -gt 0 -or $rejected -gt 0) {
        $d = ("capture_route_mismatch={0} route_scope_rejected={1} in the last 4000 lines" -f $mismatch, $rejected)
        if ($revoked) { $d = $d + " | last cause: " + $revoked.ToString().Trim() }
        Report 'WARN' 'Route-poison markers in recent log' $d `
            'A single mismatch revokes warm timing ONE-WAY for the sidecar''s life -> zero releases. Relaunch via run_orion.local.ps1 (it settles the capture driver) and re-verify liveness per section 5 of the pickup prompt.'
    } else {
        Report 'PASS' 'No capture_route_mismatch / route_scope_rejected in the last 4000 log lines'
    }
} else {
    Report 'INFO' 'logs\orion_native.log absent' 'Expected on a machine that has not run Orion yet.'
}

Write-Host ''
if ($script:warnCount -eq 0) {
    Write-Host '=== PREFLIGHT CLEAN: no warnings ===' -ForegroundColor Green
} else {
    Write-Host ("=== PREFLIGHT: {0} WARNING(S) above - fix deliberately, nothing was changed ===" -f $script:warnCount) -ForegroundColor Yellow
}
exit 0
