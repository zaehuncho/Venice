<#
.SYNOPSIS
    Builds the orion-activate Lambda deployment package (lambda_function.py +
    vendored Linux wheels for python3.12 / x86_64).

.DESCRIPTION
    The live orion-activate package ships lambda_function.py ONLY, so every
    `import cryptography` fails at runtime:

        [LEASE] signing unavailable (fail-soft): No module named 'cryptography'

    That kills heartbeat-lease signing (sign_lease), update-manifest signature
    verification (/api/update POST) and the shard AES-GCM crypto. This script
    vendors the manylinux x86_64 cp312-compatible wheels into the zip.

    The zip is built with a deterministic entry order, fixed timestamps and
    Unix 0644 file modes (Windows' Compress-Archive writes BACKSLASH entry names
    and no Unix modes, which Lambda cannot unpack correctly - never use it here).

    It VERIFIES, before zipping, that:
      * every vendored native module is ELF64 / x86-64 (not a Windows .pyd/.dll),
      * every vendored .so needs glibc <= 2.34 (Amazon Linux 2023),
      * every wheel tag is x86_64-manylinux or pure-python and cp312-compatible,
      * no runtime-provided package (boto3/botocore/...) was vendored,
      * no key material (*.pem/*.key/PRIVATE KEY) is in the package.

    It does NOT upload anything. Deploy is the coordinator's step:
      aws lambda update-function-code --function-name orion-activate `
        --region us-east-1 --zip-file fileb://D:\NexusVision\signing\orion-activate.zip

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File backend\build_lambda_zip.ps1
#>
[CmdletBinding()]
param(
    # Output package. Must live outside the repo (large binary artifact).
    [string] $OutZip = "D:\NexusVision\signing\orion-activate.zip",
    # Wheel staging dir. Wiped and recreated on every run.
    [string] $Staging = "D:\NexusVision\signing\lambda_staging",
    # Build interpreter (any CPython with pip; the wheels are cross-downloaded).
    [string] $Python = "",
    # Pin so the package is reproducible. 50.0.1 ships cp311-abi3 manylinux2014
    # (glibc 2.17) x86_64 wheels, which run on the AL2023 python3.12 runtime.
    [string] $CryptographyVersion = "50.0.1",
    # Handler source. Defaults to the repo's backend/lambda_function.py.
    [string] $HandlerSource = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $HandlerSource) { $HandlerSource = Join-Path $repoRoot "backend\lambda_function.py" }
if (-not (Test-Path $HandlerSource)) { throw "handler source not found: $HandlerSource" }

if (-not $Python) {
    $candidates = @(
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $Python = $c; break } }
}
if (-not $Python -or -not (Test-Path $Python)) { throw "no build interpreter found; pass -Python <path to python.exe>" }

# --- guard the destructive staging wipe -------------------------------------
$stagingFull = [System.IO.Path]::GetFullPath($Staging)
if ($stagingFull -notmatch '(?i)lambda_staging$') {
    throw "refusing to wipe '$stagingFull': -Staging must end in 'lambda_staging'"
}
$outDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutZip))
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
if (Test-Path $stagingFull) { Remove-Item -Recurse -Force $stagingFull }
New-Item -ItemType Directory -Path $stagingFull -Force | Out-Null

Write-Host "repo       : $repoRoot"
Write-Host "python     : $Python"
Write-Host "handler    : $HandlerSource"
Write-Host "staging    : $stagingFull"
Write-Host "out zip    : $OutZip"
Write-Host ""

# --- 1. vendor the Linux wheels ---------------------------------------------
# --platform/--implementation/--python-version REQUIRE --only-binary=:all:.
# cryptography pulls cffi, which pulls pycparser. boto3/botocore ship with the
# runtime and are deliberately NOT vendored.
Write-Host "[1/5] pip install (manylinux2014_x86_64, cp312) cryptography==$CryptographyVersion ..."
& $Python -m pip install `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all: `
    --no-compile `
    --disable-pip-version-check `
    --target $stagingFull `
    "cryptography==$CryptographyVersion"
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# --- 2. add the handler ------------------------------------------------------
Write-Host "[2/5] copying lambda_function.py ..."
Copy-Item $HandlerSource (Join-Path $stagingFull "lambda_function.py") -Force

# --- 3+4+5. verify, zip, report (python does the ELF/tag checks + zipfile) ---
$py = @'
import base64, hashlib, os, re, sys, zipfile

staging, out_zip = sys.argv[1], sys.argv[2]
RUNTIME_PROVIDED = {"boto3", "botocore", "s3transfer", "jmespath", "dateutil",
                    "python_dateutil", "urllib3", "six"}
MAX_GLIBC = (2, 34)          # Amazon Linux 2023
fail = []

# ---- collect files ----------------------------------------------------------
# `bin/` holds pip's console-script launchers, which are WINDOWS .exe stubs when
# the build host is Windows. Nothing imports them; drop the directory.
files = []
for root, dirs, names in os.walk(staging):
    dirs[:] = sorted(d for d in dirs if d != "__pycache__")
    if os.path.relpath(root, staging).replace("\\", "/").split("/")[0] == "bin":
        continue
    for n in sorted(names):
        p = os.path.join(root, n)
        rel = os.path.relpath(p, staging).replace("\\", "/")
        files.append((rel, p))
files.sort()

top = sorted({rel.split("/")[0] for rel, _ in files})
for t in top:
    stem = t.split("-")[0].lower()
    if stem in RUNTIME_PROVIDED:
        fail.append("runtime-provided package vendored: %s" % t)

# ---- no key material --------------------------------------------------------
for rel, p in files:
    if re.search(r"\.(pem|key|pfx|p12|crt)$", rel, re.I):
        fail.append("key-like file in package: %s" % rel)
with open(os.path.join(staging, "lambda_function.py"), "rb") as f:
    if b"PRIVATE KEY" in f.read():
        fail.append("lambda_function.py contains a PRIVATE KEY block")

# ---- nothing Windows-native (PE/MZ) may ride along --------------------------
for rel, p in files:
    with open(p, "rb") as f:
        if f.read(2) == b"MZ":
            fail.append("Windows PE binary in package: %s" % rel)

# ---- native modules must be Linux x86-64 ------------------------------------
native = [(rel, p) for rel, p in files if rel.endswith((".so", ".pyd", ".dll"))]
if not native:
    fail.append("no native extension module found - cryptography did not vendor")
for rel, p in native:
    if rel.endswith((".pyd", ".dll")):
        fail.append("WINDOWS binary in package: %s" % rel)
        continue
    with open(p, "rb") as f:
        head = f.read(20)
        f.seek(0)
        blob = f.read()
    if head[:4] != b"\x7fELF":
        fail.append("not an ELF object: %s" % rel); continue
    if head[4] != 2:
        fail.append("not ELF64: %s" % rel)
    if int.from_bytes(head[18:20], "little") != 0x3E:
        fail.append("not x86-64 (e_machine=0x%02x): %s" % (head[18], rel))
    vers = {tuple(int(x) for x in m) for m in re.findall(rb"GLIBC_(\d+)\.(\d+)", blob)}
    mx = max(vers) if vers else (0, 0)
    if mx > MAX_GLIBC:
        fail.append("needs glibc %d.%d > %d.%d: %s" % (mx[0], mx[1], MAX_GLIBC[0], MAX_GLIBC[1], rel))
    print("  ELF64 x86-64, max GLIBC %d.%d  %s" % (mx[0], mx[1], rel))

# ---- wheel tags -------------------------------------------------------------
for rel, p in files:
    if not rel.endswith(".dist-info/WHEEL"):
        continue
    tags = [l.split(":", 1)[1].strip() for l in open(p, encoding="utf-8").read().splitlines()
            if l.lower().startswith("tag:")]
    dist = rel.split("/")[0]
    print("  wheel %-34s %s" % (dist, ",".join(tags)))
    for t in tags:
        py_t, abi, plat = t.split("-")
        pure = (plat == "any")
        if not pure and ("x86_64" not in plat or "manylinux" not in plat):
            fail.append("wheel tag not manylinux x86_64: %s (%s)" % (t, dist))
        ok_py = pure or abi == "abi3" and py_t <= "cp312" or py_t in ("cp312", "py3")
        if not ok_py:
            fail.append("wheel tag not cp312-compatible: %s (%s)" % (t, dist))

if fail:
    print("")
    for f_ in fail:
        print("FAIL: %s" % f_)
    sys.exit(2)

# ---- deterministic zip ------------------------------------------------------
if os.path.exists(out_zip):
    os.remove(out_zip)
FIXED = (1980, 1, 1, 0, 0, 0)
with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for rel, p in files:
        zi = zipfile.ZipInfo(rel, date_time=FIXED)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.create_system = 3                       # Unix
        zi.external_attr = (0o100644) << 16        # -rw-r--r--
        with open(p, "rb") as f:
            z.writestr(zi, f.read())

size = os.path.getsize(out_zip)
raw = open(out_zip, "rb").read()
sha = hashlib.sha256(raw).hexdigest()
h_src = hashlib.sha256(open(os.path.join(staging, "lambda_function.py"), "rb").read()).hexdigest()
with zipfile.ZipFile(out_zip) as z:
    names = z.namelist()
listing = out_zip + ".files.txt"
with open(listing, "w", encoding="utf-8") as f:
    f.write("\n".join(names) + "\n")

print("")
print("zip            : %s" % out_zip)
print("zip bytes      : %d (%.2f MiB)  [50 MiB direct-upload limit]" % (size, size / 1048576.0))
print("zip sha256     : %s" % sha)
print("zip CodeSha256 : %s   (base64, matches aws lambda get-function)"
      % base64.b64encode(bytes.fromhex(sha)).decode())
print("entries        : %d" % len(names))
print("top level      : %s" % ", ".join(top))
print("handler sha256 : %s  lambda_function.py" % h_src)
print("file list      : %s" % listing)
if size > 50 * 1024 * 1024:
    print("FAIL: over the 50 MiB direct-upload limit - use an S3 upload instead")
    sys.exit(2)
'@

Write-Host ""
Write-Host "[3/5] verifying vendored wheels (ELF/glibc/tags/no-secrets) ..."
Write-Host "[4/5] building deterministic zip ..."
$tmpPy = Join-Path $env:TEMP ("build_lambda_zip_" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $tmpPy -Value $py -Encoding UTF8
try {
    & $Python $tmpPy $stagingFull $OutZip
    $rc = $LASTEXITCODE
} finally {
    Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
}
if ($rc -ne 0) { throw "package verification failed ($rc) - the zip was NOT written" }

Write-Host ""
Write-Host "[5/5] OK. Nothing was uploaded. Deploy (coordinator, after owner OK):"
Write-Host "      aws lambda update-function-code --function-name orion-activate --region us-east-1 --zip-file fileb://$OutZip"
