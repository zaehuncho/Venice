# Complete identity for one retained native QtTest invocation. This file is
# dot-sourced by run_orion_qtest.ps1 and by the isolated harness fixture.
function Get-OrionQtestUnitSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$TestExecutable,
        [Parameter(Mandatory = $true)][string]$Configuration,
        [Parameter(Mandatory = $true)][string]$SourceRoot
    )

    $exe = [IO.Path]::GetFullPath($TestExecutable)
    $root = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
    $runtimeDir = Split-Path -Parent $exe
    $buildDir = Split-Path -Parent $runtimeDir
    $sourceDir = Join-Path $root 'native_orion'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Missing test executable: $exe" }
    if (-not (Test-Path -LiteralPath $sourceDir -PathType Container)) { throw "Missing native source tree: $sourceDir" }

    function Get-UnitFileRecord([string]$path, [string]$prefix, [string]$base) {
        $full = [IO.Path]::GetFullPath($path)
        $relative = $full.Substring($base.Length).TrimStart('\', '/').Replace('\', '/')
        $stream = [IO.File]::OpenRead($full)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        } finally {
            $stream.Dispose()
            $hasher.Dispose()
        }
        return [ordered]@{
            path = "$prefix/$relative"
            size = (Get-Item -LiteralPath $full).Length
            sha256 = $digest
        }
    }

    # Hash the target EXE and every adjacent/runtime DLL, including Qt plugins.
    # Timestamp equality is deliberately not used as a coherence signal.
    $runtimePaths = @($exe) + @(Get-ChildItem -LiteralPath $runtimeDir -Recurse -File -Filter '*.dll' |
        ForEach-Object FullName | Sort-Object -Unique)
    $runtime = @($runtimePaths | ForEach-Object {
        Get-UnitFileRecord $_ 'runtime' $runtimeDir
    } | Sort-Object { $_.path })

    # git includes dirty/untracked source files but excludes ignored build trees.
    # Fixture roots need no git metadata; their small source tree is enumerated.
    $gitRoot = Join-Path $root '.git'
    if (Test-Path -LiteralPath $gitRoot) {
        $sourcePaths = @(& git -C $root ls-files --cached --others --exclude-standard -- native_orion |
            Where-Object { $_ -match '\.(cpp|c|h|hpp|qml|py|cmake|in|rc|qrc|json)$' -or $_ -match '/CMakeLists\.txt$' } |
            Where-Object { $_ -notmatch '^native_orion/build[^/]*/' } |
            ForEach-Object { Join-Path $root $_ } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
        if ($LASTEXITCODE -ne 0) { throw 'Unable to enumerate native source snapshot from git.' }
    } else {
        $sourcePaths = @(Get-ChildItem -LiteralPath $sourceDir -Recurse -File |
            Where-Object { $_.FullName -notmatch '[\\/]build[^\\/]*[\\/]' -and
                ($_.Name -eq 'CMakeLists.txt' -or $_.Extension -match '^\.(cpp|c|h|hpp|qml|py|cmake|in|rc|qrc|json)$') } |
            ForEach-Object FullName)
    }
    $sources = @($sourcePaths | Sort-Object -Unique | ForEach-Object {
        Get-UnitFileRecord $_ 'source' $root
    } | Sort-Object { $_.path })

    $metadataPaths = @()
    $cache = Join-Path $buildDir 'CMakeCache.txt'
    if (Test-Path -LiteralPath $cache -PathType Leaf) { $metadataPaths += $cache }
    $compilerFile = Get-ChildItem -LiteralPath (Join-Path $buildDir 'CMakeFiles') `
        -Recurse -File -Filter 'CMakeCXXCompiler.cmake' -ErrorAction SilentlyContinue |
        Sort-Object FullName | Select-Object -Last 1
    if ($compilerFile) { $metadataPaths += $compilerFile.FullName }
    $metadata = @($metadataPaths | ForEach-Object {
        Get-UnitFileRecord $_ 'build' $buildDir
    } | Sort-Object { $_.path })
    $compiler = [ordered]@{}
    if ($compilerFile) {
        foreach ($line in Get-Content -LiteralPath $compilerFile.FullName) {
            if ($line -match '^set\((CMAKE_CXX_COMPILER(?:_ID|_VERSION)?)\s+"?([^"\)]+)') {
                $compiler[$Matches[1]] = $Matches[2]
            }
        }
    }
    if (Test-Path -LiteralPath $cache -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $cache) {
            if ($line -match '^(CMAKE_GENERATOR|CMAKE_BUILD_TYPE|CMAKE_CXX_FLAGS_RELEASE):[^=]*=(.*)$') {
                $compiler[$Matches[1]] = $Matches[2]
            }
        }
    }
    $identityObject = [ordered]@{
        schema_version = 1
        configuration = $Configuration
        executable = $exe
        source_root = $root
        compiler = $compiler
        runtime_files = $runtime
        build_metadata_files = $metadata
        source_files = $sources
    }
    $json = ConvertTo-Json -InputObject $identityObject -Depth 8 -Compress
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($json)
        $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    $identityObject['identity_sha256'] = $digest
    return $identityObject
}
