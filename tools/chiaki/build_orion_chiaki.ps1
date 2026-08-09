param(
    [string]$SourceDir = "",
    [string]$BuildDir = "",
    [string]$OutputDir = "",
    [string]$Msys2Root = "C:\msys64"
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath([string]$Path) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Require-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (!$command) {
        throw "$Name was not found on PATH."
    }
    return $command.Source
}

$repoRoot = Resolve-FullPath (Join-Path $PSScriptRoot "..\..")

if (!$SourceDir) {
    $sourceCandidates = @(
        (Join-Path $repoRoot "vendor\chiaki-ng-orion"),
        (Join-Path (Split-Path $repoRoot -Parent) "chiaki-ng-src")
    )
    $SourceDir = $sourceCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (!$SourceDir) {
    throw "Patched Chiaki-ng source was not found. Expected vendor\chiaki-ng-orion or sibling chiaki-ng-src."
}

$source = Resolve-FullPath $SourceDir
$requiredSource = @(
    "gui\src\orionframeexport.cpp",
    "gui\src\orioninputbridge.cpp",
    "lib\src\ffmpegdecoder.c"
)
foreach ($relativePath in $requiredSource) {
    if (!(Test-Path -LiteralPath (Join-Path $source $relativePath))) {
        throw "Required Orion Chiaki-ng source is missing: $relativePath"
    }
}

$decoderSource = Get-Content -LiteralPath (Join-Path $source "lib\src\ffmpegdecoder.c") -Raw
if ($decoderSource -notmatch "chiaki_ffmpeg_decoder_set_frame_decoded_cb") {
    throw "Decoder-path frame export is absent. Refusing to build the occlusion-coupled runtime."
}

if (!$BuildDir) {
    if ($source.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        $BuildDir = Join-Path $repoRoot "native_orion\build\chiaki-ng-orion"
    } else {
        $BuildDir = Join-Path $source "build"
    }
}
if (!$OutputDir) {
    $OutputDir = Join-Path $repoRoot "native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win"
}

$build = Resolve-FullPath $BuildDir
$output = Resolve-FullPath $OutputDir
$mingwBin = Join-Path $Msys2Root "mingw64\bin"
$usrBin = Join-Path $Msys2Root "usr\bin"
if (!(Test-Path -LiteralPath $mingwBin) -or !(Test-Path -LiteralPath $usrBin)) {
    throw "MSYS2 MinGW64 toolchain not found under $Msys2Root."
}

$env:PATH = "$mingwBin;$usrBin;$env:PATH"
$cmake = Require-Command "cmake"
$ninja = Require-Command "ninja"
$pkgConfig = Require-Command "pkg-config"

& $pkgConfig --exists libavcodec libavutil opus sdl2
if ($LASTEXITCODE -ne 0) {
    throw "Required MSYS2 development packages are missing: FFmpeg, Opus, or SDL2."
}

New-Item -ItemType Directory -Force -Path $build | Out-Null
New-Item -ItemType Directory -Force -Path $output | Out-Null

Write-Host "Configuring Orion Chiaki-ng from $source"
& $cmake -S $source -B $build -G Ninja `
    -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_C_STANDARD=17 `
    -DCHIAKI_ENABLE_TESTS=OFF
if ($LASTEXITCODE -ne 0) {
    throw "Orion Chiaki-ng configure failed."
}

Write-Host "Building OrionStream.exe"
& $cmake --build $build --target chiaki
if ($LASTEXITCODE -ne 0) {
    throw "Orion Chiaki-ng build failed."
}

$candidates = @(
    (Join-Path $build "gui\OrionStream.exe"),
    (Join-Path $build "gui\Release\OrionStream.exe"),
    (Join-Path $build "gui\chiaki.exe"),
    (Join-Path $build "gui\Release\chiaki.exe")
)
$exe = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (!$exe) {
    throw "Build completed but OrionStream.exe was not found under $build."
}

$destination = Join-Path $output "OrionStream.exe"
Copy-Item -LiteralPath $exe -Destination $destination -Force
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash
Write-Host "OrionStream deployed to $destination"
Write-Host "SHA256 $hash"
