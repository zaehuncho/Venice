param(
    [string]$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
)

$candidates = @(
    "$env:ORION_PYREMOTEPLAY_PYTHON",
    "$Root\.venv311\Scripts\python.exe",
    "$env:USERPROFILE\Miniconda3\envs\cosmic_env\python.exe",
    "python.exe"
) | Where-Object { $_ -and $_.Trim() }

$helper = Join-Path $Root "native_orion\backend\ps5_remoteplay_helper.py"
foreach ($py in $candidates) {
    $resolved = Get-Command $py -ErrorAction SilentlyContinue
    if (-not $resolved) { continue }
    & $resolved.Source $helper --root $Root check
    if ($LASTEXITCODE -eq 0) { exit 0 }
}

Write-Error "No usable Python/pyremoteplay backend was found."
exit 1
