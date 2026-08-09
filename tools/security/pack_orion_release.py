#!/usr/bin/env python3
"""Apply an external Windows packer to an Orion release package, then verify it.

This script is deliberately a wrapper around a reputable commercial/in-house
packer, not a packer implementation. It keeps the workflow repeatable:

1. copy the already-strict release package
2. run the configured packer command on selected first-party EXE targets
3. regenerate release_manifest.json after packing
4. run the existing package security audit
5. prove Owner/Staff tools still pass startup integrity
6. prove a missing manifest is still refused

Packing is only a tamper-resistance layer. Authorization must still be enforced
by the backend.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "release" / "orion-package"
DEFAULT_OUTPUT = ROOT / "release" / "orion-package-packed"
DEFAULT_TARGETS = (
    "OrionOwner.exe",
    "OrionStaff.exe",
    "OrionNative.exe",
    "OrionUpdater.exe",
)


def quote_cmd_arg(path: Path) -> str:
    text = str(path)
    if '"' in text:
        raise ValueError(f"path contains a double quote and cannot be safely passed to shell: {text}")
    return f'"{text}"'


def format_packer_command(template: str, input_path: Path, output_path: Path) -> str:
    if "{input}" not in template or "{output}" not in template:
        raise ValueError("packer command must contain both {input} and {output} placeholders")
    return template.replace("{input}", quote_cmd_arg(input_path)).replace("{output}", quote_cmd_arg(output_path))


def copy_package(source: Path, dest: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"input package does not exist: {source}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def run(command: list[str] | str, cwd: Path | None = None) -> None:
    print(f"[orion-pack] run: {command if isinstance(command, str) else ' '.join(command)}")
    subprocess.run(command, cwd=cwd or ROOT, shell=isinstance(command, str), check=True)


def run_packer(package_dir: Path, targets: list[str], command_template: str) -> list[dict[str, str | int]]:
    validate_release_targets(targets)
    packed: list[dict[str, str | int]] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="orion-pack-targets-"))
    try:
        for rel in targets:
            target = package_dir / rel
            if not target.exists():
                raise FileNotFoundError(f"packer target is missing from package: {rel}")
            packed_output = temp_dir / target.name
            command = format_packer_command(command_template, target, packed_output)
            run(command)
            if not packed_output.exists() or packed_output.stat().st_size <= 0:
                raise RuntimeError(f"packer did not produce a non-empty output for {rel}: {packed_output}")
            original_size = target.stat().st_size
            shutil.move(str(packed_output), target)
            packed.append({
                "target": rel,
                "original_size": original_size,
                "packed_size": target.stat().st_size,
            })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return packed


def regenerate_manifest(package_dir: Path, signing_key: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from tools.package_orion_release import write_release_manifest

    manifest = package_dir / "release_manifest.json"
    version = None
    if manifest.is_file():
        try:
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, TypeError):
            version = None
    write_release_manifest(package_dir, version=version, signing_key=signing_key)


def audit_package(package_dir: Path) -> None:
    run([sys.executable, "tools/security_audit.py", "--package-only", "--package-dir", str(package_dir)])


def startup_integrity_smoke(package_dir: Path) -> None:
    for exe in ("OrionOwner.exe", "OrionStaff.exe"):
        run([str(package_dir / exe), "--check-startup-security"], cwd=package_dir)


def tamper_refusal_smoke(package_dir: Path) -> None:
    temp_root = Path(tempfile.mkdtemp(prefix="orion-pack-tamper-"))
    tampered = temp_root / "orion-package"
    try:
        shutil.copytree(package_dir, tampered)
        (tampered / "release_manifest.json").unlink()
        proc = subprocess.run(
            [str(tampered / "OrionStaff.exe"), "--check-startup-security"],
            cwd=tampered,
            check=False,
        )
        if proc.returncode == 0:
            raise RuntimeError("OrionStaff.exe accepted a packed package with release_manifest.json removed")
        print("[orion-pack] tamper refusal OK")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def live_backend_contract(base_url: str) -> None:
    run([sys.executable, "tools/admin/check_backend_contract.py", "--base-url", base_url])


def write_report(output_dir: Path, input_dir: Path, packed: list[dict[str, str | int]], command_template: str | None) -> None:
    report = {
        "schema": "orion.packing_report.v1",
        "input_package": str(input_dir),
        "output_package": str(output_dir),
        "packer_command_template": command_template or "(verify-only; no packer run)",
        "targets": packed,
        "note": "Report is intentionally outside the shipped package; do not include packer internals in customer builds.",
    }
    report_path = output_dir.parent / "orion-packing-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[orion-pack] report: {report_path}")


def parse_targets(raw: str) -> list[str]:
    targets = [item.strip().replace("\\", "/") for item in raw.split(",") if item.strip()]
    if not targets:
        raise ValueError("at least one pack target is required")
    return targets


def validate_release_targets(targets: list[str]) -> None:
    """Enforce the production packer's loader-safe EXE-only boundary.

    OrionPack's internal DLL round-trip harness remains useful lab coverage,
    but packed DLLs currently unpack and resolve protected imports from
    ``DllMain``. The production wrapper must not ship that loader-lock risk.
    """
    non_exe = [target for target in targets if Path(target).suffix.lower() != ".exe"]
    if non_exe:
        rendered = ", ".join(non_exe)
        raise ValueError(
            "production release packing is EXE-only; refused non-EXE target(s): "
            f"{rendered}. DLL release packing remains blocked until "
            "loader-safe deferred initialization exists"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Strict release package to copy from.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Packed package output directory.")
    parser.add_argument(
        "--targets",
        default=",".join(DEFAULT_TARGETS),
        help="Comma-separated package-relative EXE targets (production is EXE-only).",
    )
    parser.add_argument(
        "--packer-command",
        help=(
            "External packer command template. Use unquoted {input} and {output}; "
            "the script inserts quoted paths. Example: VMProtect_Con.exe {input} {output} -pf owner.vmp"
        ),
    )
    parser.add_argument("--verify-only", action="store_true", help="Copy/regenerate/audit/smoke without running a packer.")
    parser.add_argument(
        "--signing-key",
        default=os.environ.get("ORION_UPDATE_SIGNING_KEY_PEM", ""),
        help="Required Ed25519 private key PEM used to re-sign the exact packed release manifest bytes.",
    )
    parser.add_argument("--require-live-backend", action="store_true", help="Also require live owner/staff route contract to pass.")
    parser.add_argument("--backend-url", default="https://api.zaeorion.com")
    args = parser.parse_args(argv)

    if not args.verify_only and not args.packer_command:
        parser.error("--packer-command is required unless --verify-only is set")
    if args.packer_command:
        # Validate early so a long copy/build does not hide a bad template.
        format_packer_command(args.packer_command, Path("input.bin"), Path("output.bin"))
    try:
        targets = parse_targets(args.targets)
        validate_release_targets(targets)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.signing_key:
        parser.error("--signing-key or ORION_UPDATE_SIGNING_KEY_PEM is required; packed output may not be unsigned")

    signing_key = Path(args.signing_key).expanduser().resolve()
    sys.path.insert(0, str(ROOT))
    from tools.package_orion_release import require_trusted_production_signing_key
    require_trusted_production_signing_key(signing_key, package_dir=args.output.resolve())

    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    copy_package(input_dir, output_dir)

    packed: list[dict[str, str | int]] = []
    if args.packer_command and not args.verify_only:
        packed = run_packer(output_dir, targets, args.packer_command)
    else:
        print("[orion-pack] verify-only mode; no packer command executed")

    regenerate_manifest(output_dir, signing_key)
    audit_package(output_dir)
    startup_integrity_smoke(output_dir)
    tamper_refusal_smoke(output_dir)
    if args.require_live_backend:
        live_backend_contract(args.backend_url)
    write_report(output_dir, input_dir, packed, args.packer_command)
    print("[orion-pack] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
