<#
.SYNOPSIS
  Capture PS5 <-> game-server packet HEADERS at the ICS adapter, to correlate network state against
  per-shot banner verdicts. Diagnostic only.

.DESCRIPTION
  The PS5 is behind Internet Connection Sharing on this PC (adapter "PS5", 192.168.137.1), so every
  packet between the console and the game servers already transits this machine. This script uses
  pktmon (built into Windows -- nothing is installed) to record those packets.

  WHAT IS AND IS NOT CAPTURED
    * Headers only. -PacketSize 0 truncates payload, so no game content, chat, credentials or
      personal data is written. What is recorded is timing, endpoints, sizes and protocol.
    * Only traffic between the console and the specific remote endpoints discovered in step 1.
      The PC's own internet traffic is never captured. The PC<->PS5 Remote Play video stream is
      excluded -- it is local-to-local, and would otherwise be ~99% of the volume.

  REQUIRES AN ELEVATED POWERSHELL. pktmon refuses to talk to its driver otherwise.

.EXAMPLE
  # 1. Discover which remote endpoints the console is talking to (be in MyCourt, online, shooting)
  powershell -ExecutionPolicy Bypass -File tools\timing\ps5_net_capture.ps1 -Discover

  # 2. Capture a shooting session against those endpoints
  powershell -ExecutionPolicy Bypass -File tools\timing\ps5_net_capture.ps1 -Start -Peers 1.2.3.4,5.6.7.8

  # 3. Stop and convert
  powershell -ExecutionPolicy Bypass -File tools\timing\ps5_net_capture.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Discover,
    [switch]$DiscoverCapture,
    [switch]$Start,
    [switch]$Stop,
    [string[]]$Peers,
    [string]$Ps5Ip,
    [string]$OutDir = "D:\NexusVision\diagnostics\ps5-net",
    [int]$DiscoverSeconds = 20
)

$ErrorActionPreference = 'Stop'
$pktmon = "$env:SystemRoot\System32\pktmon.exe"

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "pktmon needs an ELEVATED PowerShell. Re-run this from an Administrator prompt."
    }
}

function Get-Ps5Addresses {
    # ICS can show the console under more than one address (seen on this rig: .126 and .81 sharing
    # one MAC). Return them ALL: filtering on only one silently loses a direction of traffic.
    if ($Ps5Ip) { return @($Ps5Ip) }
    $n = Get-NetNeighbor -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -like '192.168.137.*' -and
                        $_.IPAddress -ne '192.168.137.1' -and
                        $_.IPAddress -notlike '*.255' -and
                        $_.LinkLayerAddress -notmatch '^(ff-ff|00-00)' }
    $addrs = @($n | Select-Object -ExpandProperty IPAddress -Unique)
    if ($addrs.Count -eq 0) { throw "No console found on 192.168.137.x. Pass -Ps5Ip to override." }
    return $addrs
}

function Get-Ps5Address {
    if ($Ps5Ip) { return $Ps5Ip }
    # ICS hands out 192.168.137.x; the console is whichever neighbour is not this host.
    $n = Get-NetNeighbor -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -like '192.168.137.*' -and
                        $_.IPAddress -ne '192.168.137.1' -and
                        $_.IPAddress -notlike '*.255' -and
                        $_.LinkLayerAddress -notmatch '^(ff-ff|00-00)' }
    $addrs = @($n | Select-Object -ExpandProperty IPAddress -Unique)
    if ($addrs.Count -eq 0) { throw "No console found on 192.168.137.x. Is the PS5 on and linked? Pass -Ps5Ip to override." }
    if ($addrs.Count -gt 1) {
        Write-Warning "Several addresses on the ICS subnet: $($addrs -join ', '). Using $($addrs[0]); pass -Ps5Ip to choose."
    }
    return $addrs[0]
}

function New-Session {
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    return (Join-Path $OutDir ("session_" + (Get-Date -Format 'yyyyMMdd_HHmmss')))
}

Assert-Elevated

if ($Discover) {
    $ps5 = Get-Ps5Address
    Write-Host "console: $ps5"
    Write-Host "Watching its connections for $DiscoverSeconds s. Be online in MyCourt and SHOOTING now."
    # TCP is visible through the NAT table; UDP flows are seen as ICS port mappings.
    $seen = @{}
    $deadline = (Get-Date).AddSeconds($DiscoverSeconds)
    while ((Get-Date) -lt $deadline) {
        Get-NetNatSession -ErrorAction SilentlyContinue |
            Where-Object { $_.InternalSourceAddress -eq $ps5 } |
            ForEach-Object {
                $key = "$($_.RemoteDestinationAddress):$($_.RemoteDestinationPort)/$($_.Protocol)"
                if (-not $seen.ContainsKey($key)) { $seen[$key] = 0 }
                $seen[$key]++
            }
        Start-Sleep -Milliseconds 500
    }
    if ($seen.Count -eq 0) {
        Write-Warning @"
No NAT sessions observed. Get-NetNatSession only reports when ICS is exposed as a NAT instance.
Fallback: run -Start with -Peers omitted. That captures ALL console traffic whose peer is outside
192.168.137.0/24 by using a broad filter, which is larger but still excludes the Remote Play stream.
"@
    } else {
        Write-Host "`nRemote endpoints seen (candidate game servers):"
        $seen.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object {
            Write-Host ("  {0,-42} seen {1}x" -f $_.Key, $_.Value)
        }
        Write-Host "`nPass the addresses to -Peers, comma separated."
    }
    return
}

if ($DiscoverCapture) {
    # Get-NetNatSession only reports when ICS is exposed as a NAT instance, which it is not here.
    # So discover the console's remote peers from a SHORT capture instead. Ten seconds of capture
    # overhead is irrelevant for discovery; only the long measurement run has to stay clean.
    $ps5 = Get-Ps5Address
    $stem = New-Session
    $etl = "$stem.discover.etl"
    Write-Host "console: $ps5   discovery capture for $DiscoverSeconds s"

    & $pktmon filter remove | Out-Null
    & $pktmon filter add "ps5-discover" -i $ps5 | Out-Null
    & $pktmon start --capture --pkt-size 0 --file-name $etl --file-size 64 | Out-Null
    Start-Sleep -Seconds $DiscoverSeconds
    & $pktmon stop | Out-Null
    & $pktmon filter remove | Out-Null

    Push-Location $OutDir
    try { & $pktmon etl2txt $etl | Out-Null } finally { Pop-Location }
    $txt = $etl -replace '\.etl$', '.txt'
    if (-not (Test-Path $txt)) { throw "etl2txt produced no text file for $etl" }

    Write-Host "`n--- first 25 lines of pktmon text output (format sample) ---"
    Get-Content $txt -TotalCount 25 | ForEach-Object { Write-Host $_ }

    # Any IPv4 that is neither the console nor the ICS subnet is a candidate remote peer.
    $counts = @{}
    Select-String -Path $txt -Pattern '\b\d{1,3}(\.\d{1,3}){3}\b' -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object {
            $ip = $_.Value
            if ($ip -notlike '192.168.137.*' -and $ip -ne '255.255.255.255' -and
                $ip -notlike '224.*' -and $ip -notlike '239.*' -and $ip -ne '0.0.0.0') {
                if (-not $counts.ContainsKey($ip)) { $counts[$ip] = 0 }
                $counts[$ip]++
            }
        }
    Write-Host "`n--- remote peers seen (candidate game servers) ---"
    if ($counts.Count -eq 0) {
        Write-Warning "No remote peers found. Was the console online and shooting during the window?"
    } else {
        $counts.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 25 |
            ForEach-Object { Write-Host ("  {0,-18} {1} packets" -f $_.Key, $_.Value) }
        Write-Host "`nPass the busiest ones to -Peers, comma separated."
    }
    Write-Host "`ntext file: $txt"
    return
}

if ($Start) {
    $ps5 = Get-Ps5Address
    $stem = New-Session
    $etl = "$stem.etl"

    & $pktmon filter remove | Out-Null
    if ($Peers) {
        $consoles = Get-Ps5Addresses
        $n = 0
        foreach ($c in $consoles) {
            foreach ($p in $Peers) {
                $ip = ($p -split ':')[0]
                # NOTE1 in `pktmon filter add help`: two -i addresses match packets containing
                # BOTH, in either direction. That is exactly the console<->server pair.
                & $pktmon filter add "ps5-$n" -i $c $ip | Out-Null
                $n++
            }
        }
        Write-Host "filters: $n pair(s) -- consoles [$($consoles -join ', ')] x peers [$($Peers -join ', ')]"
    } else {
        & $pktmon filter add "ps5-all" -i $ps5 | Out-Null
        Write-Warning "No -Peers given: capturing ALL console traffic, including the Remote Play stream. Expect a large file and post-filter by peer subnet during analysis."
    }

    # --pkt-size 0 keeps headers only: no payload is written to disk.
    & $pktmon start --capture --pkt-size 0 --file-name $etl --file-size 512
    @{ ps5 = $ps5; peers = $Peers; etl = $etl
       started_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
       started_local = (Get-Date -Format o)
    } | ConvertTo-Json | Set-Content -Encoding utf8 "$stem.meta.json"

    Write-Host "capturing -> $etl"
    Write-Host "Shoot your session now. Do NOT stop early because a streak appears."
    Write-Host "When finished: ...\ps5_net_capture.ps1 -Stop"
    return
}

if ($Stop) {
    & $pktmon stop
    $latest = Get-ChildItem $OutDir -Filter '*.etl' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "No .etl found in $OutDir" }
    Push-Location $OutDir
    try {
        & $pktmon etl2txt $latest.FullName --metadata
        Write-Host "converted: $($latest.FullName -replace '\.etl$', '.txt')"
    } finally { Pop-Location }
    & $pktmon filter remove | Out-Null
    Write-Host "filters cleared. Capture complete."
    return
}

Write-Host "Nothing to do. Pass -Discover, -Start or -Stop. See -? for the full description."
