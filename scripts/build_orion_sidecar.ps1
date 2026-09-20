param(
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# [ORION_SIDECAR_PYTHON] Resolve an interpreter that can ACTUALLY build the sidecar, i.e. one
# with Nuitka AND the production ONNX runtime importable — not merely one that exists.
#
# This cost a full release cycle on 2026-08-06. Two separate defaults were both wrong:
#   * verify_orion.ps1 passed the literal string "python", which is not a path, so the
#     Test-Path below threw "Sidecar build Python not found at 'python'".
#   * the .venv fallback here DOES exist but has no Nuitka, so it would have failed later
#     at the "import nuitka" check with a different, equally confusing message.
# On this machine Nuitka lives in the system Python312, not the venv. Selecting on capability
# rather than existence makes the documented command work from any shell, whatever PATH says.
function Test-SidecarPythonCapability {
    param([Parameter(Mandatory = $true)][string]$Interpreter)

    # Do not invoke a candidate directly while ErrorActionPreference is Stop.
    # Windows PowerShell promotes native stderr from a failed import probe to a
    # terminating NativeCommandError before Resolve-SidecarPython can inspect
    # LASTEXITCODE and continue to the next candidate.
    $probe = "import importlib.metadata as m, nuitka, cv2, windows_capture, onnxruntime as ort; assert m.version('onnxruntime-directml') == '1.22.0'; assert m.version('windows-capture') == '2.0.0'; assert 'DmlExecutionProvider' in ort.get_available_providers()"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Interpreter
    $startInfo.Arguments = '-c "' + $probe + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = $null
    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)
        $null = $process.StandardOutput.ReadToEnd()
        $null = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

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
        if (Test-SidecarPythonCapability -Interpreter $full) { return $full }
    }

    if ($existing.Count -eq 0) {
        throw "No Python interpreter found. Pass -Python <python.exe>."
    }
    throw ("None of these interpreters has Nuitka, OpenCV, pinned DirectML and Windows Capture: " + ($existing -join ", ") + ". " +
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
    },
    [pscustomobject]@{
        Source = Join-Path $Root "models\orion_meter_detector.onnx"
        Bundled = Join-Path $Dist "models\orion_meter_detector.onnx"
        Destination = "models/orion_meter_detector.onnx"
    }
)

# [ORION_BANNER_VERDICT_LIVE 2026-09-14] The live banner grader.
#
# banner_verdict_live.py grades the game's own shot-feedback panel with the SAME reader the
# validated offline grader uses -- by IMPORTING tools/timing/panel_grade.py.
#
# The GRADER SHIPS COMPILED, never as readable .py: docs/IP_PROTECTION_PLAN.md requires
# readers to ship compiled, and panel_grade is a reader. Nuitka can only --include-module a
# module it can NAME, and tools/timing is not a package, so the grader is copied to the
# repo-root name orion_panel_grade.py for the duration of the compile, named on the Nuitka
# command line, and deleted in a finally afterwards -- it must never linger as readable
# source in a working tree or a source drop. load_panel_grade() tries `orion_panel_grade`
# FIRST, so a shipped sidecar uses the compiled module and the tools/timing path stays the
# developer fallback. tools/sidecar_bundle_manifest.py binds the grader's bytes into the
# SOURCE identity (READER_SOURCE_INPUTS), so editing it still invalidates the bundle.
#
# The npz is DATA and still ships at its repository-relative path, copied explicitly after
# the compile rather than passed as --include-data-files so the freshness gate below covers
# it. A compiled module has no source file beside it, so panel_grade's self-relative
# LIB_PATH can miss; load_panel_grade() resolves the npz from the dist instead. Shipping the
# module without the npz is still a silent feature-off (an empty template library disables
# the reader for the session), which is why this stays a build-gated input.
$SidecarGraderSource = Join-Path $Root "tools\timing\panel_grade.py"
$SidecarGraderShim = Join-Path $Root "orion_panel_grade.py"
if (-not (Test-Path -LiteralPath $SidecarGraderSource -PathType Leaf)) {
    throw "Required banner grader not found at '$SidecarGraderSource'."
}
$SidecarReaderData = @(
    [pscustomobject]@{
        Source = Join-Path $Root "tools\timing\panel_templates.npz"
        Bundled = Join-Path $Dist "tools\timing\panel_templates.npz"
        Destination = "tools/timing/panel_templates.npz"
    }
)
# Everything the bundle must carry VERBATIM, and that tools/sidecar_bundle_manifest.py binds
# into the embedded build identity (BUNDLE_DATA_INPUTS).
$SidecarBundledData = @($SidecarModels) + @($SidecarReaderData)

foreach ($model in $SidecarBundledData) {
    if (-not (Test-Path -LiteralPath $model.Source -PathType Leaf)) {
        throw "Required sidecar model not found at '$($model.Source)'."
    }
}

function Copy-OrionSidecarReaderData {
    foreach ($item in $SidecarReaderData) {
        $parent = Split-Path -Parent $item.Bundled
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -LiteralPath $item.Source -Destination $item.Bundled -Force
    }
}

function Get-OrionFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    # Use the .NET primitive directly: nested release shells can lose module
    # autoloading after the Nuitka build, while SHA-256 must still be checked.
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Test-OrionSidecarModelsFresh {
    foreach ($model in $SidecarBundledData) {
        if (-not (Test-Path -LiteralPath $model.Bundled -PathType Leaf)) {
            return $false
        }
        $sourceHash = Get-OrionFileSha256 -Path $model.Source
        $bundleHash = Get-OrionFileSha256 -Path $model.Bundled
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

& $Python -c "import importlib.metadata as m, nuitka, onnxruntime as ort; assert m.version('onnxruntime-directml') == '1.22.0'; assert 'DmlExecutionProvider' in ort.get_available_providers()"
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka and onnxruntime are required to build OrionSidecar.exe. Install pinned build dependencies with: $Python -m pip install -r requirements-build.txt"
}

# Load the exact release model through the portable DirectML provider before
# compiling. CUDA ORT's provider DLL needs roughly a gigabyte of CUDA/cuDNN
# runtime DLLs that were never in the package; DirectML ships its complete
# 18MB runtime and accelerates the same model on Windows GPUs.
$DetectorModel = ($SidecarModels | Where-Object {
    $_.Destination -eq "models/orion_meter_detector.onnx"
}).Source
& $Python -c "import sys, onnxruntime as ort; so=ort.SessionOptions(); so.enable_mem_pattern=False; so.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL; s=ort.InferenceSession(sys.argv[1], sess_options=so, providers=['DmlExecutionProvider','CPUExecutionProvider']); i=s.get_inputs()[0]; o=s.get_outputs(); assert s.get_providers()[0]=='DmlExecutionProvider'; assert len(i.shape)==4 and i.shape[1]==3 and i.shape[2]==i.shape[3] and int(i.shape[2])>=128; assert o; print('[orion-sidecar] detector validated: provider=%s input=%s shape=%s outputs=%d' % (s.get_providers()[0], i.type, i.shape, len(o)))" $DetectorModel
if ($LASTEXITCODE -ne 0) {
    throw "Production meter detector failed onnxruntime validation: $DetectorModel"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
& $Python $ManifestTool --prepare-build --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar source identity preparation failed" }
$ModelDataOptions = @($SidecarModels | ForEach-Object {
    "--include-data-files=$($_.Source)=$($_.Destination)"
})
# Include the runtime root and let Nuitka follow its C API imports. Including
# the whole package also drags in ORT's offline transformer/quantization
# toolchain, which is unused at runtime and can stall/bloat the release build.
# The banner grader, under the repo-root name --include-module can address. The finally
# removes it whether Nuitka succeeds or throws, so it never lingers as readable source.
#
# [ORION_PILL_RULER 2026-09-19] pill_fill_ruler is listed below because
# simple_meter_reader imports it INSIDE the Pill branch and swallows the ImportError
# (fail-open by design). Nuitka cannot see a function-local import, so without that line a
# compiled sidecar silently falls back to the box-relative ruler -- a SILENT FEATURE-OFF on
# the Pill style rather than a crash, which is the worst possible failure shape.
# [ORION_SIDECAR_NO_CUDA 2026-09-19] Nuitka's stock onnxruntime rule bundles EVERY
# onnxruntime/capi/onnxruntime*.dll it finds. The build interpreter's site-packages can hold a
# CUDA/TensorRT provider left behind by an unrelated onnxruntime(-gpu) install (on 2026-09-19 a
# 185 MB onnxruntime_providers_cuda.dll from a 1.30 wheel sat beside the pinned 1.22 DirectML
# runtime). Shipping it bloats the installer, is not ABI-matched to the bundled runtime, and
# can never load without a CUDA runtime the package does not carry. The two --noinclude-dlls
# lines exclude them; the $GpuProviders check after the compile fails the build if one slips in.
# NOTE for future edits: the Nuitka call below is one backtick-continued statement. A `#`
# comment between its lines is a PowerShell parse error -- keep commentary here, not inline.
Copy-Item -LiteralPath $SidecarGraderSource -Destination $SidecarGraderShim -Force
try {
    & $Python -m nuitka `
        --standalone `
        --assume-yes-for-downloads `
        --include-package=cv2 `
        --include-package=windows_capture `
        --include-module=xbox_remote_play `
        --include-module=decoder_pipe_identity `
        --include-module=latency_estimator `
        --include-module=tip_registration_infer `
        --include-module=meter_detector_yolo `
        --include-module=orion_panel_grade `
        --include-module=pill_fill_ruler `
        --include-module=onnxruntime `
        --noinclude-dlls=onnxruntime/capi/onnxruntime_providers_cuda* `
        --noinclude-dlls=onnxruntime/capi/onnxruntime_providers_tensorrt* `
        $ModelDataOptions `
        --windows-console-mode=disable `
        --nofollow-import-to=torch,torchvision,ultralytics,scipy,mediapipe `
        --output-dir=$OutputDir `
        -o OrionSidecar.exe `
        native_orion\backend\autogreen_sidecar.py
    $NuitkaExitCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $SidecarGraderShim -Force -ErrorAction SilentlyContinue
}
if ($NuitkaExitCode -ne 0) {
    throw "OrionSidecar Nuitka build failed with exit code $NuitkaExitCode"
}
# [ORION_BANNER_VERDICT_LIVE 2026-09-14] The grader's TEMPLATE LIBRARY. The grader itself is
# compiled into the executable above; only its data ships. Before the freshness check below,
# so a failed copy is a build failure and never a quiet feature-off.
Copy-OrionSidecarReaderData
$GpuProviders = @(Get-ChildItem -LiteralPath $Dist -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^onnxruntime_providers_(cuda|tensorrt)' })
if ($GpuProviders.Count -gt 0) {
    throw ("OrionSidecar bundle carries a CUDA/TensorRT onnxruntime provider it must not ship: " + (($GpuProviders | ForEach-Object { $_.FullName }) -join ', '))
}
if (-not (Test-OrionSidecarModelsFresh)) {
    throw "OrionSidecar bundle is missing an exact production model used by source."
}

# Exercise the freshly compiled executable, not the build interpreter. This
# proves Nuitka carried DirectML.dll and the DML provider into the standalone
# bundle and that end-to-end preprocessing + inference meets the acquisition
# budget. The GUI-subsystem exe writes a file because it has no reliable stdout.
$SmokeResult = Join-Path $OutputDir "orion_detector_smoke.json"
Remove-Item -LiteralPath $SmokeResult -Force -ErrorAction SilentlyContinue
$SmokeExe = Join-Path $Dist "OrionSidecar.exe"
# PowerShell does not synchronously wait for a GUI-subsystem executable invoked
# with `&`. Use Start-Process -Wait so the result file and exit code belong to
# the completed smoke run rather than racing its startup.
$SmokeProcess = Start-Process -FilePath $SmokeExe `
    -ArgumentList @(
        "--root", ('"{0}"' -f $Dist),
        "--detector-smoke-file", ('"{0}"' -f $SmokeResult)
    ) `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($SmokeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SmokeResult -PathType Leaf)) {
    throw "Compiled OrionSidecar detector/provider smoke failed with exit code $($SmokeProcess.ExitCode)"
}
$SmokeJson = Get-Content -Raw -LiteralPath $SmokeResult | ConvertFrom-Json
Write-Host ("[orion-sidecar] compiled detector smoke: provider={0} median={1}ms p90={2}ms p99={3}ms max={4}ms" -f `
    $SmokeJson.provider, $SmokeJson.median_ms, $SmokeJson.p90_ms, $SmokeJson.p99_ms, $SmokeJson.max_ms)
if (-not $SmokeJson.ok -or $SmokeJson.provider -ne "DmlExecutionProvider" -or [double]$SmokeJson.p90_ms -gt 35.0) {
    throw "Compiled OrionSidecar detector is not timing-eligible (requires DirectML and p90 <= 35ms)"
}
Remove-Item -LiteralPath $SmokeResult -Force

& $Python $ManifestTool --write --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar build manifest write failed" }
& $Python $ManifestTool --verify --root $Root --dist $Dist
if ($LASTEXITCODE -ne 0) { throw "OrionSidecar freshness verification failed after build" }

Write-Host "[orion-sidecar] production bundle ready: $Dist\OrionSidecar.exe"
