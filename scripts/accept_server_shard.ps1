[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$BuildId,
    [string]$OutputDir = "D:\NexusVision\server-shard-acceptance",
    [switch]$RecordOnly
)

$ErrorActionPreference = "Stop"
if ($BuildId -notmatch '^[0-9a-fA-F]{64}$') {
    throw "BuildId must be the 64-character Lethe content digest."
}

$cases = @(
    @{ Id='a'; Name='fresh_install'; Expected='Fresh install fetches the shard and Venice starts without an extra user prompt.' },
    @{ Id='b'; Name='server_blocked'; Expected='With execute-api blocked, launch refuses with the cannot-reach-Venice Retry dialog and no fallback.' },
    @{ Id='c'; Name='build_revoked'; Expected='After owner revocation, launch refuses with the update-Venice message.' },
    @{ Id='d'; Name='machine_copy'; Expected='A machine-A install copied to machine B refuses with machine mismatch.' },
    @{ Id='e'; Name='lease_continuity'; Expected='A valid launch runs for at least 20 minutes and signed lease refresh keeps firing enabled.' },
    @{ Id='f'; Name='second_build_update'; Expected='Signed update installs a second build_id; it fetches its new fragment and the old build is revoked after grace.' }
)

$results = @()
foreach ($case in $cases) {
    Write-Host "[$($case.Id)] $($case.Expected)"
    if ($RecordOnly) {
        $verdict = 'NOT_RUN'
        $evidence = ''
    } else {
        $verdict = (Read-Host 'Result (PASS/FAIL/NOT_RUN)').Trim().ToUpperInvariant()
        if ($verdict -notin @('PASS','FAIL','NOT_RUN')) {
            throw "Invalid result '$verdict'. Use PASS, FAIL, or NOT_RUN."
        }
        $evidence = Read-Host 'Evidence path or concise observation (no secrets)'
    }
    $results += [ordered]@{
        id = $case.Id
        name = $case.Name
        expected = $case.Expected
        result = $verdict
        evidence = $evidence
    }
}

$required = $results | Where-Object { $_.id -in @('a','b','c','d','e') }
$approved = (($required | Where-Object result -ne 'PASS').Count -eq 0)
$record = [ordered]@{
    schema = 'orion.server_shard.acceptance.v1'
    build_id = $BuildId.ToLowerInvariant()
    recorded_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
    host = $env:COMPUTERNAME
    approved_for_release = $approved
    results = $results
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$path = Join-Path $OutputDir ("server-shard-{0}.json" -f $BuildId.Substring(0,16).ToLowerInvariant())
$record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
Write-Host "Acceptance record: $path"
if (-not $approved) {
    Write-Error 'Server-shard acceptance is not approved: every case (a)-(e) must PASS.' -ErrorAction Continue
    exit 1
}
Write-Host 'Server-shard acceptance: PASS (a-e).'
