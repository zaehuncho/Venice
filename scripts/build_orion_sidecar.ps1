param(
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# [ORION_SIDECAR_PYTHON] Resolve an interpreter that can ACTUALLY build the sidecar, i.e. one
# with Nuitka importable — not merely one that exists.
#
# This cost a full release cycle on 2026-08-06. Two separate defaults were both wrong:
#   * verify_orion.ps1 passed the literal string "python", which is not a path, so the
#     Test-Path below threw "Sidecar build Python not found at 'python'".
#   * the .venv fallback here DOES exist but has no Nuitka, so it would have failed later
#     at the "import nuitka" check with a different, equally confusing message.
# On this machine Nuitka lives in the system Python312, not the venv. Selecting on capability
# rather than existence makes the documented command work from any shell, whatever PATH says.
function Resolve-SidecarPython {
    param([string]$Preferred)

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Preferred)) {
        $candidates += $Preferred                                   # explicit -Python wins
        $cmd = Get-Command $Preferred -ErrorAction SilentlyContinue  # ...even if bare ("python")
        if ($cmd) { $candidates += $cmd.Source }
    }
    $candidates += (Join-Path $Root ".venv\Scripts\python.exe")
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" `
                        -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })

    $seen = @{}
    $existing = @()
    foreach ($c in $candidates) {
        if ([string]::IsNullOrWhiteSpace($c)) { continue }
        if (-not (Test-Path -LiteralPath $c)) { continue }
        $full = (Resolve-Path -LiteralPath $c).Path
        if ($seen.ContainsKey($full)) { continue }
        $seen[$full] = $true
        $existing += $full
        & $full -c "import nuitka" 2>$null
        if ($LASTEXITCODE -eq 0) { return $full }
    }

    if ($existing.Count -eq 0) {
        throw "No Python interpreter found. Pass -Python <python.exe>."
    }
    throw ("None of these interpreters has Nuitka: " + ($existing -join ", ") + ". " +
           "Install the pinned build deps with: <python.exe> -m pip install -r requirements-build.txt")
}

$Python = Resolve-SidecarPython -Preferred $Python
Write-Host "[orion-sidecar] using interpreter: $Python"

$Dist = Join-Path $Root "build\sidecar\autogreen_sidecar.dist"
$ManifestTool = Join-Path $Root "tools\sidecar_bundle_manifest.py"
$OutputDir = Join-Path $Root "build\sidecar"
$SidecarModels = @(
    [pscustomobject]@{
        Source = Join-Path $Root "models\tip_registration.json"
        Bundled = Join-Path $Dist "models\tip_registration.json"
        Destination = "models/tip_registration.json"
    },
    [pscustomobject]@{
        Source = Join-Path $Root "models\latency_factory_prior.json"
        Bundled = Join-Path $Dist "models\latency_factory_prior.json"
        Destination = "models/latency_factory_prior.json"
    }
)

foreach ($model in $SidecarModels) {
    if (-not (Test-Path -LiteralPath $model.Source -PathType Leaf)) {
        throw "Required sidecar model not found at '$($model.Source)'."
    }
}

function Test-OrionSidecarModelsFresh {
    foreach ($model in $SidecarModels) {
        if (-not (Test-Path -LiteralPath $model.Bundled -PathType Leaf)) {
            return $false
        }
        $sourceHash = (Get-FileHash -LiteralPath $model.Source -Algorithm SHA256).Hash
        $bundleHash = (Get-FileHash -LiteralPath $model.Bundled -Algorithm SHA256).Hash
        if ($sourceHash -ne $bundleHash) {
            return $false
        }
    }
    return $true
}

function Remove-OrionSidecarBuildPath {
    param([Parameter(Mandatory = $true)][string]$Target)

    $outputFull = [System.IO.Path]::GetFullPath($OutputDir).TrimEnd('\', '/')
    $targetFull = [System.IO.Path]::GetFullPath($Target).TrimEnd('\', '/')
    $parentFull = [System.IO.Path]::GetFullPath((Split-Path -Parent $targetFull)).TrimEnd('\', '/')
    $allowedLeaf = @(
        "autogreen_sidecar.dist",
        "autogreen_sidecar.build",
        "autogreen_sidecar.onefile-build"
    )
    if ($parentFull -ne $outputFull -or $allowedLeaf -notcontains (Split-Path -Leaf $targetFull)) {
        throw "Refusing to remove unexpected sidecar build path '$targetFull'."
    }
    if (Test-Path -LiteralPath $targetFull) {
        Remove-Item -LiteralPath $targetFull -Recurse -Force
    }
}

if (-not $Force) {
    & $Python $ManifestTool --verify --root $Root --dist $Dist
    if ($LASTEXITCODE -eq 0 -and (Test-OrionSidecarModelsFresh)) {
        Write-Host "[orion-sidecar] current bundle is fresh; rebuild skipped"
        exit 0
    }
}

if ($Force) {
    Remove-OrionSidecarBuildPath (Join-Path $OutputDir "autogreen_sidecar.dist")
    Remove-OrionSidecarBuildPath (Join-Path $OutputDir "autogreen_sidecar.build")
    Remove-OrionSidecarBuildPath (Join-Path $OutputDir "autogreen_sidecar.onefile-build")
}

& $Python -c "import nuitka"
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka is required to build OrionSidecar.exe. Install pinned build dependencies with: $Python -m pip install -r requirements-build.txt"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
& $Python $ManifestTool --prepare-build --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar source identity preparation failed" }
$ModelDataOptions = @($SidecarModels | ForEach-Object {
    "--include-data-files=$($_.Source)=$($_.Destination)"
})
& $Python -m nuitka `
    --standalone `
    --assume-yes-for-downloads `
    --include-package=cv2 `
    --include-module=decoder_pipe_identity `
    --include-module=latency_estimator `
    --include-module=tip_registration_infer `
    $ModelDataOptions `
    --windows-console-mode=disable `
    --nofollow-import-to=torch,torchvision,ultralytics,scipy,mediapipe,onnxruntime `
    --output-dir=$OutputDir `
    -o OrionSidecar.exe `
    native_orion\backend\autogreen_sidecar.py
if ($LASTEXITCODE -ne 0) {
    throw "OrionSidecar Nuitka build failed with exit code $LASTEXITCODE"
}
if (-not (Test-OrionSidecarModelsFresh)) {
    throw "OrionSidecar bundle is missing an exact production model used by source."
}

& $Python $ManifestTool --write --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar build manifest write failed" }
& $Python $ManifestTool --verify --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar freshness verification failed after build" }

Write-Host "[orion-sidecar] production bundle ready: $Dist\OrionSidecar.exe"
