param(
    [Parameter(Mandatory = $true)][string]$TestExecutable,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')][string]$TestName,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')][string]$Configuration = 'Release',
    [Parameter(Mandatory = $true)][string]$LogDirectory,
    [string]$SourceRoot = '',
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 120
)

# Retain the FIRST invocation, including GUI-subsystem QtTest output. Never retry
# or translate a nonzero child exit into success. CTest's outer timeout must leave
# time for this wrapper to terminate only its own test child and flush evidence.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'orion_qtest_unit.ps1')
$SourceRoot = if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    Join-Path $PSScriptRoot '..'
} else { $SourceRoot }
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$root = if ([string]::IsNullOrWhiteSpace($env:ORION_QTEST_LOG_ROOT)) {
    $LogDirectory
} else {
    $env:ORION_QTEST_LOG_ROOT
}
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
$runDirectory = Join-Path ([IO.Path]::GetFullPath($root)) "$Configuration\$TestName\$runId"
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$qtestLog = Join-Path $runDirectory 'qtest.txt'
$stdoutLog = Join-Path $runDirectory 'stdout.txt'
$stderrLog = Join-Path $runDirectory 'stderr.txt'
$resultLog = Join-Path $runDirectory 'result.json'
$unitLog = Join-Path $runDirectory 'unit.json'
foreach ($path in @($qtestLog, $stdoutLog, $stderrLog)) {
    [IO.File]::WriteAllText($path, '')
}
$result = [ordered]@{
    schema_version = 1
    test = $TestName
    configuration = $Configuration
    executable = $TestExecutable
    arguments = @('-o', "$qtestLog,txt")
    working_directory = (Get-Location).Path
    timeout_seconds = $TimeoutSeconds
    started_utc = [DateTime]::UtcNow.ToString('o')
    finished_utc = $null
    pid = $null
    child_exit_code = $null
    exit_code = 125
    status = 'starting'
    error = $null
    qtest_log = $qtestLog
    stdout_log = $stdoutLog
    stderr_log = $stderrLog
    unit_manifest = $unitLog
    unit_before_sha256 = $null
    unit_after_sha256 = $null
    unit_coherent = $false
}
$unit = [ordered]@{ schema_version = 1; before = $null; after = $null; coherent = $false; error = $null }
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultLog -Encoding UTF8
$unit | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $unitLog -Encoding UTF8
Write-Output "[orion-qtest] $TestName ($Configuration) evidence: $runDirectory"
$process = $null
try {
    $exe = (Resolve-Path -LiteralPath $TestExecutable).Path
    if ([IO.Path]::GetExtension($exe) -ine '.exe' -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw 'QtTest target must be an existing executable file.'
    }
    $result.executable = $exe
    $unit.before = Get-OrionQtestUnitSnapshot -TestExecutable $exe `
        -Configuration $Configuration -SourceRoot $SourceRoot
    $result.unit_before_sha256 = $unit.before.identity_sha256
    $unit | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $unitLog -Encoding UTF8
    # Start-Process joins ArgumentList strings, so explicitly quote the complete
    # QtTest output argument (paths containing spaces remain ONE argument).
    $process = Start-Process -FilePath $exe -ArgumentList @('-o', ('"' + $qtestLog + ',txt"')) `
        -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $null = $process.Handle  # Retain the handle so very short children keep ExitCode.
    $result.pid = $process.Id
    $result.status = 'running'
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultLog -Encoding UTF8
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $result.status = 'timeout'
        $result.exit_code = 124
        $result.error = "QtTest exceeded $TimeoutSeconds seconds; only the spawned test process was terminated."
        $process.Kill()
        if (-not $process.WaitForExit(5000)) {
            $result.error = 'QtTest timed out and its spawned process did not exit within the 5-second termination bound.'
        }
    } else {
        # WaitForExit() drains redirected output after the bounded wait reports
        # that the process has exited. It does not extend the running-test bound.
        $process.WaitForExit()
        $result.child_exit_code = $process.ExitCode
        $result.exit_code = $process.ExitCode
        $result.status = if ($process.ExitCode -eq 0) { 'passed' } else { 'failed' }
        if ($process.ExitCode -eq 0 -and (Get-Item -LiteralPath $qtestLog).Length -eq 0) {
            $result.status = 'missing_qtest_output'
            $result.exit_code = 125
            $result.error = 'QtTest returned success without a retained transcript.'
        }
    }
} catch {
    $result.error = $_.Exception.Message
    # Preserve an already observed child failure or timeout even if evidence
    # collection also fails. An unobserved result always fails the harness.
    if ($result.exit_code -eq 0) { $result.exit_code = 125 }
    if ($result.status -ne 'timeout') { $result.status = 'harness_error' }
    if ($process -and -not $process.HasExited) {
        # An evidence-write/startup error must not leave our own test running.
        try {
            $process.Kill()
            $null = $process.WaitForExit(5000)
        } catch {
            $result.error += "; spawned-test cleanup: $($_.Exception.Message)"
        }
    }
} finally {
    if ($process) { $process.Dispose() }
    $result.finished_utc = [DateTime]::UtcNow.ToString('o')
    try {
        if ($unit.before) {
            try {
                $unit.after = Get-OrionQtestUnitSnapshot -TestExecutable $result.executable `
                    -Configuration $Configuration -SourceRoot $SourceRoot
                $result.unit_after_sha256 = $unit.after.identity_sha256
                $unit.coherent = ($unit.before.identity_sha256 -eq $unit.after.identity_sha256)
                $result.unit_coherent = $unit.coherent
                if (-not $unit.coherent) {
                    $result.status = 'unit_changed'
                    $result.exit_code = 125
                    $result.error = 'Native executable, DLL, build metadata, or source snapshot changed during the test.'
                }
            } catch {
                $unit.error = $_.Exception.Message
                $result.status = 'unit_snapshot_error'
                $result.exit_code = 125
                $result.error = "Native unit after-snapshot failed: $($unit.error)"
            }
        }
        $unit | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $unitLog -Encoding UTF8
        $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultLog -Encoding UTF8
        if ($result.exit_code -ne 0) {
            foreach ($path in @($qtestLog, $stdoutLog, $stderrLog)) {
                Write-Output "[orion-qtest] retained output: $path"
                Get-Content -LiteralPath $path
            }
        }
    } catch {
        if ($result.exit_code -eq 0) { $result.exit_code = 125 }
        Write-Output "[orion-qtest] evidence read/write failed: $($_.Exception.Message)"
    }
}
if ($result.error) { Write-Output "[orion-qtest] $($result.error)" }
Write-Output "[orion-qtest] status=$($result.status) child_exit=$($result.child_exit_code) exit=$($result.exit_code) result=$resultLog"
exit $result.exit_code
