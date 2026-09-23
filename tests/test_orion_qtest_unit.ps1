param([string]$FixtureRoot = (Join-Path $env:TEMP ('orion-a7-unit-' + [guid]::NewGuid().ToString('N'))))

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$wrapper = Join-Path $repo 'scripts/run_orion_qtest.ps1'
New-Item -ItemType Directory -Force -Path (Join-Path $FixtureRoot 'native_orion') | Out-Null
[IO.File]::WriteAllText((Join-Path $FixtureRoot 'native_orion/fixture.cpp'), 'int fixture = 1;')
$source = @'
using System;
using System.IO;
using System.Threading;
public static class FakeQtTest {
    public static int Main(string[] args) {
        string marker = Environment.GetEnvironmentVariable("A7_QTEST_STARTED");
        if (!String.IsNullOrEmpty(marker)) File.WriteAllText(marker, "started");
        for (int i = 0; i + 1 < args.Length; i++) {
            if (args[i] == "-o") {
                string path = args[i + 1].Split(',')[0];
                File.WriteAllText(path, "PASS   : FakeQtTest::fixture()\n");
            }
        }
        string wait = Environment.GetEnvironmentVariable("A7_QTEST_WAIT_MS");
        if (!String.IsNullOrEmpty(wait)) Thread.Sleep(Int32.Parse(wait));
        string code = Environment.GetEnvironmentVariable("A7_QTEST_EXIT_CODE");
        return String.IsNullOrEmpty(code) ? 0 : Int32.Parse(code);
    }
}
'@

function New-FixtureUnit([string]$name) {
    $build = Join-Path $FixtureRoot $name
    $runtime = Join-Path $build 'Release'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $exe = Join-Path $runtime 'FakeQtTest.exe'
    Add-Type -TypeDefinition $source -OutputAssembly $exe -OutputType ConsoleApplication
    [IO.File]::WriteAllText((Join-Path $runtime 'fixture.dll'), "dependency-$name")
    [IO.File]::WriteAllText((Join-Path $build 'CMakeCache.txt'), "CMAKE_GENERATOR:INTERNAL=Fixture-$name")
    return $exe
}

function Invoke-Fixture([string]$exe, [string]$name, [int]$expectedExit) {
    $logRoot = Join-Path $FixtureRoot "logs-$name"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper `
        -TestExecutable $exe -TestName $name -Configuration Release `
        -LogDirectory $logRoot -SourceRoot $FixtureRoot -TimeoutSeconds 10 | Out-Null
    $observedExit = $LASTEXITCODE
    if ($observedExit -ne $expectedExit) {
        throw "$name exit=$observedExit expected=$expectedExit"
    }
    $resultPath = Get-ChildItem -LiteralPath $logRoot -Recurse -Filter result.json -File |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $resultPath) { throw "$name result.json absent" }
    $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
    $unitPath = Join-Path (Split-Path -Parent $resultPath) 'unit.json'
    if (-not (Test-Path -LiteralPath $unitPath)) { throw "$name unit.json absent" }
    $unit = Get-Content -LiteralPath $unitPath -Raw | ConvertFrom-Json
    if (-not $unit.before.identity_sha256 -or -not $unit.after.identity_sha256) {
        throw "$name before/after identity absent"
    }
    return [pscustomobject]@{ result = $result; unit = $unit; path = $unitPath }
}

$exeA = New-FixtureUnit 'generation-a'
$exeB = New-FixtureUnit 'generation-b'
$env:A7_QTEST_EXIT_CODE = '0'
$env:A7_QTEST_WAIT_MS = '0'
$a = Invoke-Fixture $exeA 'unit-a' 0
if (-not $a.unit.coherent -or $a.result.status -ne 'passed') { throw 'clean A unit not coherent/passed' }
$env:A7_QTEST_EXIT_CODE = '7'
$b = Invoke-Fixture $exeB 'unit-b' 7
if (-not $b.unit.coherent -or $b.result.child_exit_code -ne 7) { throw 'clean B child exit not retained' }
if ($a.unit.before.identity_sha256 -eq $b.unit.before.identity_sha256) { throw 'fixture generations collapsed' }

$env:A7_QTEST_EXIT_CODE = '0'
$env:A7_QTEST_WAIT_MS = '1500'
$marker = Join-Path $FixtureRoot 'mutation-started.txt'
$env:A7_QTEST_STARTED = $marker
$mutationLogs = Join-Path $FixtureRoot 'logs-mutation'
$out = Join-Path $FixtureRoot 'mutation-wrapper.out'
$err = Join-Path $FixtureRoot 'mutation-wrapper.err'
$args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $wrapper + '"'),
    '-TestExecutable', ('"' + $exeA + '"'), '-TestName', 'unit-mutation',
    '-Configuration', 'Release', '-LogDirectory', ('"' + $mutationLogs + '"'),
    '-SourceRoot', ('"' + $FixtureRoot + '"'), '-TimeoutSeconds', '10')
$process = Start-Process -FilePath powershell.exe -ArgumentList $args -PassThru `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
$null = $process.Handle
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $marker) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 20
    }
    if (-not (Test-Path -LiteralPath $marker)) { throw 'fixture child never started' }
    [IO.File]::WriteAllText((Join-Path (Split-Path -Parent $exeA) 'fixture.dll'), 'dependency-swapped-during-run')
    if (-not $process.WaitForExit(10000)) { throw 'wrapper did not finish' }
    $process.WaitForExit()
    $process.Refresh()
    if ($process.ExitCode -ne 125) { throw "mutation exit=$($process.ExitCode) expected=125" }
} finally { $process.Dispose() }
$resultPath = Get-ChildItem -LiteralPath $mutationLogs -Recurse -Filter result.json -File |
    Select-Object -First 1 -ExpandProperty FullName
$mutated = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
$mutatedUnit = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $resultPath) 'unit.json') -Raw | ConvertFrom-Json
if ($mutated.status -ne 'unit_changed' -or $mutatedUnit.coherent -or
    $mutatedUnit.before.identity_sha256 -eq $mutatedUnit.after.identity_sha256) {
    throw 'dependency mutation did not invalidate the unit'
}
Write-Output "A7_QTEST_UNIT_PASS A=$($a.unit.before.identity_sha256) B=$($b.unit.before.identity_sha256) mutated=$($mutatedUnit.after.identity_sha256) clean_exit=0 child_exit=7 mutation_exit=125"
