"""Offline Windows harness regressions; no application, keys, or network access."""

import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run_orion_qtest.ps1"
POWERSHELL = shutil.which("powershell.exe")
CSC = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"


@pytest.mark.parametrize("mismatch", ["none", "source", "test_binary"])
def test_checkout_identity_uses_script_root_not_process_cwd(tmp_path, mismatch):
    if os.name != "nt" or not POWERSHELL:
        pytest.skip("Windows PowerShell required")
    root = tmp_path / "candidate"
    build = root / "native_orion" / "build"
    build.mkdir(parents=True)
    foreign = tmp_path / "other checkout"
    foreign.mkdir()
    home = (foreign if mismatch == "source" else root) / "native_orion"
    (build / "CMakeCache.txt").write_text(f"CMAKE_HOME_DIRECTORY:INTERNAL={home.as_posix()}\n")
    test_root = foreign if mismatch == "test_binary" else build
    names = ("OrionNativeTests", "OrionPassiveCourtFlowTests", "OrionShmInteropTests", "OrionUpdaterTests")
    (build / "CTestTestfile.cmake").write_text("\n".join(
        f'add_test("{name}" "{test_root.as_posix()}/Release/{name}.exe")' for name in names))
    text = (ROOT / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    helper = "function Assert-OrionCMakeOwnership {" + text.split(
        "function Assert-OrionCMakeOwnership {", 1)[1].split("function Invoke-OrionNativeTests {", 1)[0]
    script = tmp_path / "ownership.ps1"
    script.write_text(helper + r'''
$ErrorActionPreference = 'Stop'
$Root = $env:FIXTURE_ROOT
Set-Location -LiteralPath $Root
# Set-Location does not change System.IO's process current directory.
[Environment]::CurrentDirectory = $env:FIXTURE_FOREIGN
Assert-OrionCMakeOwnership -BuildDirectory 'native_orion\build' -RequireTests
Write-Output 'OWNERSHIP_OK'
''', encoding="utf-8")
    env = dict(os.environ, FIXTURE_ROOT=str(root), FIXTURE_FOREIGN=str(foreign))
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          cwd=foreign, env=env, capture_output=True, text=True, timeout=10)
    if mismatch == "none":
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OWNERSHIP_OK" in proc.stdout
    else:
        assert proc.returncode != 0
        assert "OWNERSHIP_OK" not in proc.stdout


def test_cmake_wraps_every_qtest_and_keeps_target_file_identity():
    text = (ROOT / "native_orion" / "CMakeLists.txt").read_text(encoding="utf-8")
    targets = set(re.findall(r"add_executable\((Orion\w+Tests)\b", text))
    assert targets
    assert targets == set(re.findall(r"orion_add_qtest\((Orion\w+Tests)\)", text))
    assert '"$<TARGET_FILE:${target}>"' in text
    assert '-Configuration "$<CONFIG>"' in text
    assert "-TimeoutSeconds 120" in text
    assert "TIMEOUT 135" in text
    assert "-NoProfile -NonInteractive -ExecutionPolicy Bypass" in text


def test_verifier_retains_ctest_and_qtest_run_logs_without_retry():
    text = (ROOT / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    assert text.count("Invoke-OrionNativeTests -BuildDirectory") == 2
    assert "--output-log $ctestLog" in text
    assert "$ctestExit = $LASTEXITCODE" in text
    assert "$global:LASTEXITCODE = $ctestExit" in text
    assert "$env:ORION_QTEST_LOG_ROOT = $previousLogRoot" in text
    assert "--no-tests=error" in text
    assert "tests\\test_native_qtest_diagnostics.py" in text
    assert not re.search(r"--repeat|retry-until", text)
    assert '"native_orion\\build_prod_codex"' in text


@pytest.fixture(scope="module")
def fake_qtest(tmp_path_factory):
    if os.name != "nt" or not POWERSHELL or not CSC.is_file():
        pytest.skip("Windows PowerShell and .NET Framework compiler required")
    folder = tmp_path_factory.mktemp("fake qtest target")
    source = folder / "FakeQTest.cs"
    source.write_text(
        r'''
using System;
using System.IO;
using System.Diagnostics;
using System.Threading;
class FakeQTest {
    static int Main(string[] args) {
        string mode = Environment.GetEnvironmentVariable("FAKE_QTEST_MODE") ?? "pass";
        if (args.Length != 2 || args[0] != "-o" || !args[1].EndsWith(",txt")) return 43;
        string path = args[1].Substring(0, args[1].Length - 4);
        File.AppendAllText(Environment.GetEnvironmentVariable("FAKE_QTEST_CALLS"), "called\n");
        Console.WriteLine("stdout fixture");
        Console.Error.WriteLine("stderr fixture");
        if (mode == "empty") return 0;
        if (mode == "empty_fail") return 7;
        File.WriteAllText(path, "CASE fixture_input\nPID=" + Process.GetCurrentProcess().Id + "\n");
        if (mode == "timeout") { Thread.Sleep(30000); return 0; }
        if (mode == "large") Console.WriteLine(new String('x', 131072));
        File.AppendAllText(path, "cwd=" + Directory.GetCurrentDirectory() + "\n");
        File.AppendAllText(path, "environment=" + Environment.GetEnvironmentVariable("FAKE_QTEST_INHERITED") + "\n");
        File.AppendAllText(path, mode == "fail" ? "FAIL fixture_input\n" : "PASS fixture_input\n");
        return mode == "fail" ? 7 : 0;
    }
}
''',
        encoding="utf-8",
    )
    exe = folder / "Fake QTest.exe"
    proc = subprocess.run([str(CSC), "/nologo", "/target:exe", f"/out:{exe}", str(source)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return exe


def invoke(fake_qtest, tmp_path, mode="pass", configuration="Release", timeout=5, override=None):
    assert WRAPPER.is_file(), "QtTest diagnostics wrapper missing"
    logs = tmp_path / "retained logs"
    calls = tmp_path / (mode + "-" + configuration + "-calls.txt")
    env = os.environ.copy()
    env.pop("ORION_QTEST_LOG_ROOT", None)
    env.pop("ORION_VERIFY_LOG_ROOT", None)
    env.update(FAKE_QTEST_MODE=mode, FAKE_QTEST_CALLS=str(calls), FAKE_QTEST_INHERITED="fixture inherited")
    if override is not None:
        env["ORION_QTEST_LOG_ROOT"] = str(override)
    cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER),
           "-TestExecutable", str(fake_qtest), "-TestName", "FakeQTest", "-Configuration", configuration,
           "-LogDirectory", str(logs), "-TimeoutSeconds", str(timeout)]
    proc = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    root = override or logs
    metadata = list(root.glob(f"{configuration}/FakeQTest/*/result.json"))
    return proc, metadata, calls


def result(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@pytest.mark.parametrize("mode,exit_code,status", [
    ("pass", 0, "passed"), ("fail", 7, "failed"),
    ("empty", 125, "missing_qtest_output"), ("empty_fail", 7, "failed"), ("large", 0, "passed"),
])
def test_child_result_and_original_transcript_are_retained(fake_qtest, tmp_path, mode, exit_code, status):
    proc, metadata, calls = invoke(fake_qtest, tmp_path, mode)
    assert proc.returncode == exit_code, proc.stdout + proc.stderr
    assert calls.read_text() == "called\n"  # No retry on ANY outcome.
    assert len(metadata) == 1
    data = result(metadata[0])
    assert data["schema_version"] == 1
    assert data["status"] == status
    assert data["exit_code"] == exit_code
    assert data["child_exit_code"] == (7 if "fail" in mode else 0)
    assert data["finished_utc"]
    assert data["working_directory"] == str(tmp_path)
    assert "stdout fixture" in Path(data["stdout_log"]).read_text()
    assert "stderr fixture" in Path(data["stderr_log"]).read_text()
    transcript = Path(data["qtest_log"]).read_text()
    if mode not in ("empty", "empty_fail"):
        assert "fixture_input" in transcript
        assert "environment=fixture inherited" in transcript
    if mode == "fail":
        assert "FAIL fixture_input" in proc.stdout
    assert str(metadata[0].parent) in proc.stdout


def test_timeout_retains_partial_log_and_terminates_only_child(fake_qtest, tmp_path):
    start = time.monotonic()
    proc, metadata, calls = invoke(fake_qtest, tmp_path, "timeout", timeout=1)
    assert proc.returncode == 124, proc.stdout + proc.stderr
    assert time.monotonic() - start < 12
    assert calls.read_text() == "called\n"
    data = result(metadata[0])
    assert data["status"] == "timeout"
    assert data["child_exit_code"] is None
    assert "CASE fixture_input" in proc.stdout
    # A check against the recorded PID proves the timed-out fixture was reaped.
    check = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                            f"if (Get-Process -Id {data['pid']} -ErrorAction SilentlyContinue) {{ exit 1 }}"],
                           capture_output=True, timeout=10)
    assert check.returncode == 0


def test_start_failure_preserves_structured_evidence(fake_qtest, tmp_path):
    proc, metadata, calls = invoke(tmp_path / "absent.exe", tmp_path)
    assert proc.returncode == 125
    assert not calls.exists()
    data = result(metadata[0])
    assert data["status"] == "harness_error"
    assert data["child_exit_code"] is None
    assert data["error"]
    assert all(Path(data[key]).is_file() for key in ("qtest_log", "stdout_log", "stderr_log"))


def test_runs_and_configurations_do_not_overwrite(fake_qtest, tmp_path):
    first, metadata, _ = invoke(fake_qtest, tmp_path)
    original = metadata[0].read_bytes()
    second, metadata, _ = invoke(fake_qtest, tmp_path)
    third, debug, _ = invoke(fake_qtest, tmp_path, configuration="Debug")
    assert first.returncode == second.returncode == third.returncode == 0
    assert len(metadata) == 2 and len(debug) == 1
    assert any(path.read_bytes() == original for path in metadata)


def test_environment_log_root_is_respected(fake_qtest, tmp_path):
    override = tmp_path / "verifier run" / "dev"
    proc, metadata, _ = invoke(fake_qtest, tmp_path, override=override)
    assert proc.returncode == 0
    assert len(metadata) == 1
    assert not (tmp_path / "retained logs").exists()


@pytest.mark.parametrize("configuration,timeout", [("../escape", 5), ("..", 5), ("Release", 0), ("Release", 121)])
def test_malformed_arguments_fail_before_child_launch(fake_qtest, tmp_path, configuration, timeout):
    proc, _, calls = invoke(fake_qtest, tmp_path, configuration=configuration, timeout=timeout)
    assert proc.returncode != 0
    assert not calls.exists()


def test_parallel_runs_keep_distinct_evidence(fake_qtest, tmp_path):
    # Separate invocation markers, common evidence root and configuration/name.
    def run(i):
        folder = tmp_path / str(i)
        folder.mkdir()
        return invoke(fake_qtest, folder, override=tmp_path / "shared logs")[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert [proc.returncode for proc in results] == [0, 0]
    metadata = list((tmp_path / "shared logs").glob("Release/FakeQTest/*/result.json"))
    assert len(metadata) == 2
    assert len({result(path)["pid"] for path in metadata}) == 2


@pytest.mark.parametrize("verify_override", [False, True])
@pytest.mark.parametrize("mode,exit_code", [("pass", 0), ("fail", 8), ("throw", 125)])
def test_verifier_preserves_ctest_exit_and_restores_environment(
        fake_qtest, tmp_path, mode, exit_code, verify_override):
    text = (ROOT / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    helper = "function Invoke-OrionNativeTests {" + text.split("function Invoke-OrionNativeTests {", 1)[1].split(
        "function Resolve-OrionLibCryptoSource {", 1)[0]
    script = tmp_path / "verify fixture.ps1"
    script.write_text(helper + r'''
$ErrorActionPreference = 'Stop'
function Assert-OrionCMakeOwnership {
    param($BuildDirectory, [switch]$RequireTests)
    if (-not $RequireTests) { throw 'ownership test requirement lost' }
}
$global:calls = 0
function ctest {
    $global:calls++
    $logFlag = [Array]::IndexOf($args, '--output-log')
    if ($logFlag -lt 0 -or $args -notcontains '--no-tests=error') { throw 'log/no-tests gate missing' }
    Set-Content -LiteralPath $args[$logFlag + 1] -Value 'fixture ctest transcript'
    if ($env:FAKE_CTEST_MODE -eq 'throw') { throw 'fixture invocation failed' }
    $global:LASTEXITCODE = if ($env:FAKE_CTEST_MODE -eq 'fail') { 8 } else { 0 }
}
$env:ORION_QTEST_LOG_ROOT = 'original environment fixture'
try { Invoke-OrionNativeTests -BuildDirectory (Join-Path (Get-Location) 'build fixture') } catch {}
$code = $LASTEXITCODE
[ordered]@{exit_code=$code; calls=$global:calls; restored=$env:ORION_QTEST_LOG_ROOT} | ConvertTo-Json -Compress
exit $code
''', encoding="utf-8")
    env = os.environ.copy()
    env.pop("ORION_VERIFY_LOG_ROOT", None)
    if verify_override:
        env["ORION_VERIFY_LOG_ROOT"] = str(tmp_path / "short evidence")
    env["FAKE_CTEST_MODE"] = mode
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == exit_code, proc.stdout + proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data == {"exit_code": exit_code, "calls": 1, "restored": "original environment fixture"}
    log_root = ((tmp_path / "short evidence" / "build fixture") if verify_override
                else (tmp_path / "build fixture" / "Testing" / "OrionVerification"))
    logs = list(log_root.glob("*/ctest.txt"))
    if verify_override:
        assert not (tmp_path / "build fixture" / "Testing").exists()
    assert len(logs) == 1
    assert logs[0].read_text().strip() == "fixture ctest transcript"


def test_verifier_short_root_separates_builds_and_preserves_every_run(fake_qtest, tmp_path):
    text = (ROOT / "scripts" / "verify_orion.ps1").read_text(encoding="utf-8")
    helper = "function Invoke-OrionNativeTests {" + text.split("function Invoke-OrionNativeTests {", 1)[1].split(
        "function Resolve-OrionLibCryptoSource {", 1)[0]
    script = tmp_path / "verify separation fixture.ps1"
    script.write_text(helper + r'''
$ErrorActionPreference = 'Stop'
function Assert-OrionCMakeOwnership {
    param($BuildDirectory, [switch]$RequireTests)
    if (-not $RequireTests) { throw 'ownership test requirement lost' }
}
$global:records = @()
function ctest {
    $logFlag = [Array]::IndexOf($args, '--output-log')
    $buildFlag = [Array]::IndexOf($args, '--test-dir')
    if ($logFlag -lt 0 -or $buildFlag -lt 0 -or $args -notcontains '--no-tests=error') {
        throw 'test invocation or evidence gate lost'
    }
    $log = $args[$logFlag + 1]
    $build = $args[$buildFlag + 1]
    $global:records += [ordered]@{build=$build; log=$log; qtest_root=$env:ORION_QTEST_LOG_ROOT}
    Set-Content -LiteralPath $log -Value ('first invocation ' + $global:records.Count)
    $global:LASTEXITCODE = 0
}
$env:ORION_QTEST_LOG_ROOT = 'original environment fixture'
foreach ($leaf in @('build', 'build_prod_codex', 'build')) {
    Invoke-OrionNativeTests -BuildDirectory (Join-Path (Get-Location) $leaf)
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
[ordered]@{records=$global:records; restored=$env:ORION_QTEST_LOG_ROOT; override=$env:ORION_VERIFY_LOG_ROOT} |
    ConvertTo-Json -Depth 5 -Compress
''', encoding="utf-8")
    override = tmp_path / "short evidence"
    env = os.environ.copy()
    env.pop("ORION_VERIFY_LOG_ROOT", None)
    env["ORION_VERIFY_LOG_ROOT"] = str(override)
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["restored"] == "original environment fixture"
    assert data["override"] == str(override)
    assert len(data["records"]) == 3  # One invocation each, never a hidden retry.
    paths = []
    for index, (record, leaf) in enumerate(zip(data["records"], ("build", "build_prod_codex", "build")), 1):
        log = Path(record["log"])
        assert log.parent.parent == override / leaf
        assert re.fullmatch(r"\d{8}T\d{9}Z-[0-9a-f]{32}", log.parent.name)
        assert Path(record["build"]) == tmp_path / leaf
        assert Path(record["qtest_root"]) == log.parent / "qtest"
        assert log.read_text().strip() == f"first invocation {index}"
        assert not (tmp_path / leaf / "Testing").exists()
        paths.append(log)
    assert len(set(paths)) == 3
    assert len(list((override / "build").glob("*/ctest.txt"))) == 2
    assert len(list((override / "build_prod_codex").glob("*/ctest.txt"))) == 1
