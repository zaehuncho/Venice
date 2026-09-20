#!/usr/bin/env python3
"""Build/verify the source- and bundle-binding manifest for OrionSidecar.exe.

The production package intentionally does not ship Orion's detector/timing Python
modules as source.  That makes a stale Nuitka bundle especially dangerous: the
native binaries and tests can be current while the executable doing live meter
detection still contains older code. This manifest binds the compiled sidecar
to every repository-root Python module it can embed, its entry point, and each
production timing model it reads at runtime.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path


SCHEMA = "orion.sidecar_build.v4"
MANIFEST_NAME = "ORION_SIDECAR_BUILD.json"
EXECUTABLE_NAME = "OrionSidecar.exe"
BUILD_ID_MODULE = "native_orion/backend/orion_sidecar_build_id.py"
IDENTITY_TIMEOUT_S = 10.0
MODEL_INPUTS = (
    "models/tip_registration.json",
    "models/latency_factory_prior.json",
    "models/orion_meter_detector.onnx",
)
DETECTOR_MODEL_INPUT = "models/orion_meter_detector.onnx"
# [ORION_BANNER_VERDICT_LIVE 2026-09-14] The live banner grader.
#
# banner_verdict_live.py re-uses tools/timing/panel_grade.py BY IMPORT (nothing about the
# reading is re-implemented), and the grader needs its template library at runtime.
#
# The GRADER SHIPS COMPILED, never as readable source: docs/IP_PROTECTION_PLAN.md requires
# readers to ship compiled, and panel_grade is a reader. The build script copies
# tools/timing/panel_grade.py to the repo-root name orion_panel_grade.py, passes
# --include-module=orion_panel_grade so Nuitka compiles it INTO the executable, and deletes
# the copy afterwards; load_panel_grade() tries that module name first. So the grader is
# bound into the identity as a SOURCE input (its bytes decide the compiled output) and is
# NOT a file the dist carries.
#
# The npz is data and stays a bundled data input at its repository-relative path. A
# compiled module has no readable source file beside it, so panel_grade's self-relative
# LIB_PATH can miss; load_panel_grade()._resolve_library falls back to the npz beside the
# module / the executable / at tools/timing inside the dist. Shipping the module without
# the npz is still a silent feature-off (an empty library disables the reader), which is
# why the npz is a build-gated input rather than best-effort.
# [ORION_BANNER_DISTANCE 2026-09-17] banner_distance.py reads the panel's DISTANCE cell
# (`23'5"`). It lives at the repo root, so the root glob already binds its bytes into the
# identity and Nuitka already compiles it -- it is listed here only so the roster of what
# the live banner reader depends on is in ONE place. It carries its own digit templates
# inline, so unlike panel_grade there is no DATA input that can go missing.
READER_SOURCE_INPUTS = (
    "tools/timing/panel_grade.py",
)
BANNER_DISTANCE_MODULE = "banner_distance.py"
READER_DATA_INPUTS = (
    "tools/timing/panel_templates.npz",
)
# Build artefact: the transient repo-root copy of the grader that --include-module compiles.
# Excluded from the root glob so the source digest is identical whether or not a previous
# build left it behind; its CONTENT is bound via READER_SOURCE_INPUTS.
GRADER_SHIM_MODULE = "orion_panel_grade.py"
# Every non-source input the bundle must carry VERBATIM at the same relative path, and
# whose bytes are bound into the embedded build identity.
BUNDLE_DATA_INPUTS = MODEL_INPUTS + READER_DATA_INPUTS
# npz is a zip container.
NPZ_MAGIC = b"PK\x03\x04"
# A source module and a template library are both far smaller than this; the bound is only
# here so a wrong path cannot drag an arbitrary file into the identity.
MAX_READER_DATA_BYTES = 8 * 1024 * 1024
TIP_MODEL_VERSION_FIELDS = (
    "u_grid", "g_vals", "u_tip", "priors", "q33", "q66", "uncertainty",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_files(root: Path) -> list[Path]:
    """Return the deterministic set of code inputs covered by the bundle.

    The sidecar's imports are deliberately feature-gated and often occur inside
    functions, which makes a hand-maintained import list easy to under-specify.
    All repository-root Python modules are therefore bound.  That can trigger a
    conservative rebuild after an unrelated root helper changes, but can never
    let an imported detector module change without invalidating the bundle.
    """

    root = root.resolve()
    entry = root / "native_orion" / "backend" / "autogreen_sidecar.py"
    roots = [p for p in root.glob("*.py") if p.name != GRADER_SHIM_MODULE]
    # The banner grader lives outside the repo root but is compiled INTO the sidecar (see
    # READER_SOURCE_INPUTS): bind its bytes here so a change to it invalidates the bundle,
    # exactly as a change to a root module does.
    readers = [root / relative for relative in READER_SOURCE_INPUTS]
    for path in readers:
        if not path.is_file():
            raise FileNotFoundError(f"required sidecar reader source not found: {path}")
        _validate_reader_data_input(path.relative_to(root).as_posix(), path)
    candidates = [entry, *roots, *readers]
    files = sorted({path.resolve() for path in candidates if path.is_file()})
    if entry.resolve() not in files:
        raise FileNotFoundError(f"sidecar entry point not found: {entry}")
    return files


def source_hashes(root: Path) -> dict[str, str]:
    root = root.resolve()
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in source_files(root)
    }


def source_digest(hashes: dict[str, str]) -> str:
    """Hash a canonical source inventory for embedding in the executable."""

    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finite_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a model number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("model number is not finite")
    return number


def _validate_tip_registration_model(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != "orion.tip_registration.v2":
        raise ValueError("unsupported tip-registration model schema")
    if payload.get("model_id") != "tip-registration-global":
        raise ValueError("unexpected tip-registration model_id")
    material = {key: payload[key] for key in TIP_MODEL_VERSION_FIELDS}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected_version = "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    if payload.get("model_version") != expected_version:
        raise ValueError("tip-registration content version mismatch")
    grid = payload.get("u_grid")
    values = payload.get("g_vals")
    if (not isinstance(grid, list) or not isinstance(values, list)
            or len(grid) != len(values) or len(grid) < 16):
        raise ValueError("invalid tip-registration template arrays")
    parsed_grid = [_finite_number(value) for value in grid]
    parsed_values = [_finite_number(value) for value in values]
    if any(right <= left for left, right in zip(parsed_grid, parsed_grid[1:])):
        raise ValueError("tip-registration grid is not increasing")
    if any(right < left - 1e-7 for left, right in zip(parsed_values, parsed_values[1:])):
        raise ValueError("tip-registration template is not monotone")
    u_tip = _finite_number(payload.get("u_tip"))
    if not parsed_grid[0] <= u_tip <= min(parsed_grid[-1], 1.25):
        raise ValueError("tip-registration u_tip is outside support")
    priors = payload.get("priors")
    if not isinstance(priors, dict) or set(priors) != {"g", "0", "1", "2"}:
        raise ValueError("tip-registration priors are incomplete")
    for prior in priors.values():
        if not isinstance(prior, list) or len(prior) != 3:
            raise ValueError("invalid tip-registration prior")
        duration, amplitude, _ = (_finite_number(value) for value in prior)
        if duration < 40.0 or amplitude < 5.0:
            raise ValueError("tip-registration prior is out of range")
    q33 = _finite_number(payload.get("q33"))
    q66 = _finite_number(payload.get("q66"))
    if not 40.0 <= q33 <= q66 <= 1000.0:
        raise ValueError("tip-registration duration terciles are invalid")
    if int(payload.get("n_shots", 0)) < 8 or int(payload.get("n_sessions", 0)) < 1:
        raise ValueError("tip-registration training corpus is too small")
    uncertainty = payload.get("uncertainty")
    if not isinstance(uncertainty, dict) or not isinstance(uncertainty.get("basis"), str):
        raise ValueError("tip-registration uncertainty calibration is missing")
    base_ms = _finite_number(uncertainty.get("base_ms"))
    horizon_scale = _finite_number(uncertainty.get("horizon_scale"))
    maximum_ms = _finite_number(uncertainty.get("maximum_ms"))
    if base_ms < 0.0 or not 0.0 <= horizon_scale <= 2.0 or not 25.0 <= maximum_ms <= 1000.0:
        raise ValueError("tip-registration uncertainty calibration is invalid")


def _validate_latency_factory_prior(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != "orion.latency_factory_prior.v1":
        raise ValueError("unsupported latency factory-prior schema")
    model_id = payload.get("model_id")
    version = payload.get("model_version")
    profiles = payload.get("profiles")
    if (not isinstance(model_id, str) or not model_id or len(model_id) > 64
            or not isinstance(version, str) or not version or len(version) > 32
            or not isinstance(profiles, list) or not profiles):
        raise ValueError("latency factory-prior identity or profiles are invalid")
    scopes: set[tuple[str, ...]] = set()
    coverage: set[tuple[str, bool]] = set()
    names: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("latency factory-prior profile is not an object")
        name = profile.get("name")
        terms = profile.get("scope_contains")
        if (not isinstance(name, str) or not name or len(name) > 48 or name in names
                or not isinstance(terms, list) or not terms):
            raise ValueError("latency factory-prior profile identity is invalid")
        normalized = tuple(sorted(
            term.strip().lower() for term in terms
            if isinstance(term, str) and term.strip()
        ))
        if len(normalized) != len(terms) or len(set(normalized)) != len(normalized) or normalized in scopes:
            raise ValueError("latency factory-prior scope terms are invalid or ambiguous")
        mean_ms = _finite_number(profile.get("mean_ms"))
        sd_ms = _finite_number(profile.get("sd_ms"))
        if not 15.0 <= mean_ms <= 500.0 or not 6.0 <= sd_ms <= 100.0:
            raise ValueError("latency factory-prior value is out of range")
        names.add(name)
        scopes.add(normalized)
        route = "capture_card" if "capture_card" in normalized else (
            "decoder" if "decoder" in normalized else ""
        )
        if not route:
            raise ValueError("latency factory-prior profile has no video route")
        coverage.add((route, "controller=pipe" in normalized))
    required = {
        ("capture_card", True), ("capture_card", False),
        ("decoder", True), ("decoder", False),
    }
    if not required <= coverage:
        raise ValueError("latency factory-prior lacks pipe/fallback route coverage")


def validate_model_document(relative: str, payload: object) -> None:
    if relative == "models/tip_registration.json":
        _validate_tip_registration_model(payload)
    elif relative == "models/latency_factory_prior.json":
        _validate_latency_factory_prior(payload)
    else:
        raise ValueError(f"unrecognized sidecar model input: {relative}")


def _validate_reader_data_input(relative: str, path: Path) -> None:
    """Content check for the banner grader's module + template library.

    Deliberately dependency-free (no numpy import here): enough to prove the right KIND of
    file is being bound, so a truncated or wrong-path input fails the build instead of
    shipping a sidecar whose banner reader silently disables itself at runtime.
    """

    size = path.stat().st_size
    if size <= 0 or size > MAX_READER_DATA_BYTES:
        raise ValueError(f"sidecar reader data input has invalid size: {relative}")
    if relative.endswith(".npz"):
        if path.read_bytes()[:4] != NPZ_MAGIC:
            raise ValueError(f"sidecar template library is not an npz archive: {relative}")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"sidecar reader module is not readable UTF-8: {relative}: {exc}") from exc
    if not text.strip():
        raise ValueError(f"sidecar reader module is empty: {relative}")


def model_input_files(root: Path) -> list[Path]:
    """Resolve the exact runtime data allowlist without permitting path escape."""

    root = root.resolve()
    files: list[Path] = []
    for relative in BUNDLE_DATA_INPUTS:
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"sidecar model input escapes repository root: {relative}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"required sidecar model input not found: {path}")
        size = path.stat().st_size
        if relative in READER_DATA_INPUTS:
            _validate_reader_data_input(relative, path)
        elif relative == DETECTOR_MODEL_INPUT:
            # ONNX is a protobuf with no fixed magic. Keep validation dependency-free
            # here, bind every byte into the embedded digest, and let the build script
            # load it through the exact packaged onnxruntime before compilation.
            if size < 1024 or size > 64 * 1024 * 1024:
                raise ValueError(f"sidecar detector model has invalid size: {relative}")
            prefix = path.read_bytes()[:64]
            if not prefix or not any(prefix):
                raise ValueError(f"sidecar detector model has invalid content: {relative}")
        else:
            if size <= 0 or size > 64 * 1024:
                raise ValueError(f"sidecar model input has invalid size: {relative}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate_model_document(relative, payload)
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid sidecar model input {relative}: {exc}") from exc
        files.append(path)
    return files


def model_input_hashes(root: Path) -> dict[str, str]:
    root = root.resolve()
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in model_input_files(root)
    }


def build_input_digest(sources: dict[str, str], models: dict[str, str]) -> str:
    """Hash code and runtime data under separate namespaces for embedded attestation."""

    inventory = {"sources": sources, "models": models}
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_build(root: Path) -> tuple[Path, str]:
    """Generate the tiny module Nuitka embeds as proof of source identity."""

    root = root.resolve()
    digest = build_input_digest(source_hashes(root), model_input_hashes(root))
    destination = root / Path(BUILD_ID_MODULE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (
        '"""Generated by tools/sidecar_bundle_manifest.py; do not edit."""\n\n'
        f'ORION_SIDECAR_SOURCE_DIGEST = "{digest}"\n'
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(destination)
    return destination, digest


def _safe_bundle_name(path: Path, dist: Path) -> str:
    relative = path.relative_to(dist).as_posix()
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError(f"unsafe sidecar bundle path: {relative!r}")
    return relative


def bundle_hashes(dist: Path) -> dict[str, dict[str, int | str]]:
    """Inventory every shipped standalone file except this manifest itself."""

    dist = dist.resolve()
    files: dict[str, dict[str, int | str]] = {}
    casefold_names: dict[str, str] = {}
    for path in sorted((item for item in dist.rglob("*") if item.is_file()), key=lambda p: p.as_posix().lower()):
        name = _safe_bundle_name(path, dist)
        if name == MANIFEST_NAME:
            continue
        folded = name.casefold()
        previous = casefold_names.get(folded)
        if previous is not None and previous != name:
            raise ValueError(f"case-insensitive sidecar bundle collision: {previous!r} and {name!r}")
        casefold_names[folded] = name
        files[name] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return files


def read_embedded_identity(executable: Path) -> str:
    """Ask the compiled binary for the source digest embedded by Nuitka."""

    executable = executable.resolve()
    with tempfile.TemporaryDirectory(prefix="orion-sidecar-identity-") as tmp:
        output = Path(tmp) / "identity.txt"
        completed = subprocess.run(
            [str(executable), "--build-identity-file", str(output)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=IDENTITY_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            tail = completed.stderr.decode("utf-8", errors="replace").strip()[-300:]
            suffix = f": {tail}" if tail else ""
            raise RuntimeError(f"sidecar identity command exited {completed.returncode}{suffix}")
        if not output.is_file():
            raise RuntimeError("sidecar identity command produced no identity file")
        identity = output.read_text(encoding="ascii").strip().lower()
    if len(identity) != 64 or any(ch not in "0123456789abcdef" for ch in identity):
        raise RuntimeError("sidecar returned an invalid embedded source digest")
    return identity


def write_manifest(
    root: Path,
    dist: Path,
    *,
    identity_reader: Callable[[Path], str] = read_embedded_identity,
) -> Path:
    root = root.resolve()
    dist = dist.resolve()
    executable = dist / EXECUTABLE_NAME
    if not executable.is_file():
        raise FileNotFoundError(f"compiled sidecar not found: {executable}")

    sources = source_hashes(root)
    models = model_input_hashes(root)
    digest = build_input_digest(sources, models)
    embedded_digest = identity_reader(executable).strip().lower()
    if embedded_digest != digest:
        raise RuntimeError(
            "compiled sidecar embeds a different source digest; perform a clean rebuild "
            f"(binary={embedded_digest or '<missing>'}, source={digest})"
        )
    files = bundle_hashes(dist)
    if EXECUTABLE_NAME not in files:
        raise RuntimeError(f"bundle inventory omitted {EXECUTABLE_NAME}")
    for name, expected_hash in models.items():
        bundled = files.get(name)
        if not isinstance(bundled, dict) or bundled.get("sha256") != expected_hash:
            raise RuntimeError(f"compiled sidecar bundle has a missing or stale model input: {name}")

    document = {
        "schema": SCHEMA,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "executable": EXECUTABLE_NAME,
        "executable_sha256": files[EXECUTABLE_NAME]["sha256"],
        "build_input_digest": digest,
        "source_digest": digest,
        "embedded_source_digest": embedded_digest,
        "sources": sources,
        "models": models,
        "files": files,
    }
    destination = dist / MANIFEST_NAME
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def verify_manifest(
    root: Path,
    dist: Path,
    *,
    identity_reader: Callable[[Path], str] = read_embedded_identity,
) -> list[str]:
    root = root.resolve()
    dist = dist.resolve()
    executable = dist / EXECUTABLE_NAME
    manifest_path = dist / MANIFEST_NAME
    failures: list[str] = []

    if not executable.is_file():
        failures.append(f"missing {EXECUTABLE_NAME}")
    if not manifest_path.is_file():
        failures.append(f"missing {MANIFEST_NAME}")
        return failures

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        failures.append(f"invalid {MANIFEST_NAME}: {exc}")
        return failures

    if document.get("schema") != SCHEMA:
        failures.append(f"unexpected manifest schema: {document.get('schema')!r}")
    if document.get("executable") != EXECUTABLE_NAME:
        failures.append("manifest executable name mismatch")
    if executable.is_file() and document.get("executable_sha256") != sha256_file(executable):
        failures.append("compiled sidecar hash mismatch")

    try:
        current_sources = source_hashes(root)
        current_models = model_input_hashes(root)
    except (OSError, ValueError) as exc:
        failures.append(f"cannot inventory sidecar build inputs: {exc}")
        return failures
    current_digest = build_input_digest(current_sources, current_models)
    if document.get("build_input_digest") != current_digest:
        failures.append("sidecar build input digest mismatch")
    if document.get("source_digest") != current_digest:
        failures.append("sidecar source digest mismatch")
    recorded_sources = document.get("sources")
    if not isinstance(recorded_sources, dict):
        failures.append("manifest sources map missing")
    else:
        missing = sorted(set(current_sources) - set(recorded_sources))
        removed = sorted(set(recorded_sources) - set(current_sources))
        changed = sorted(
            name for name in set(current_sources) & set(recorded_sources)
            if current_sources[name] != recorded_sources[name]
        )
        if missing:
            failures.append("new source files after sidecar build: " + ", ".join(missing))
        if removed:
            failures.append("recorded source files no longer exist: " + ", ".join(removed))
        if changed:
            failures.append("source files changed after sidecar build: " + ", ".join(changed))

    recorded_models = document.get("models")
    if not isinstance(recorded_models, dict):
        failures.append("manifest models map missing")
    else:
        missing_models = sorted(set(current_models) - set(recorded_models))
        removed_models = sorted(set(recorded_models) - set(current_models))
        changed_models = sorted(
            name for name in set(current_models) & set(recorded_models)
            if current_models[name] != recorded_models[name]
        )
        if missing_models:
            failures.append("new model inputs after sidecar build: " + ", ".join(missing_models))
        if removed_models:
            failures.append("recorded model inputs no longer exist: " + ", ".join(removed_models))
        if changed_models:
            failures.append("model inputs changed after sidecar build: " + ", ".join(changed_models))

    if executable.is_file():
        try:
            embedded_digest = identity_reader(executable).strip().lower()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append(f"cannot read compiled sidecar identity: {exc}")
        else:
            if embedded_digest != current_digest:
                failures.append("compiled sidecar source identity mismatch")
            if document.get("embedded_source_digest") != embedded_digest:
                failures.append("manifest embedded source identity mismatch")

    recorded_files = document.get("files")
    if not isinstance(recorded_files, dict):
        failures.append("manifest bundle file map missing")
    else:
        try:
            current_files = bundle_hashes(dist)
        except (OSError, ValueError) as exc:
            failures.append(f"cannot inventory sidecar bundle: {exc}")
        else:
            for name, expected_hash in current_models.items():
                bundled = current_files.get(name)
                if not isinstance(bundled, dict):
                    failures.append(f"required bundled model missing: {name}")
                elif bundled.get("sha256") != expected_hash:
                    failures.append(f"bundled model differs from source input: {name}")
            missing_files = sorted(set(recorded_files) - set(current_files))
            new_files = sorted(set(current_files) - set(recorded_files))
            changed_files = sorted(
                name
                for name in set(current_files) & set(recorded_files)
                if current_files[name] != recorded_files[name]
            )
            if missing_files:
                failures.append("recorded bundle files missing: " + ", ".join(missing_files))
            if new_files:
                failures.append("unrecorded bundle files present: " + ", ".join(new_files))
            if changed_files:
                failures.append("sidecar bundle files changed: " + ", ".join(changed_files))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "build" / "sidecar" / "autogreen_sidecar.dist",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-build", action="store_true", help="write the source identity module before compiling")
    action.add_argument("--write", action="store_true", help="write a manifest after a successful build")
    action.add_argument("--verify", action="store_true", help="verify executable and source freshness")
    args = parser.parse_args()

    if args.prepare_build:
        path, digest = prepare_build(args.root)
        print(f"[orion-sidecar] prepared {path} ({digest})")
        return 0

    if args.write:
        path = write_manifest(args.root, args.dist)
        print(f"[orion-sidecar] wrote {path}")
        return 0

    failures = verify_manifest(args.root, args.dist)
    if failures:
        print("[orion-sidecar] stale or invalid compiled sidecar:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("[orion-sidecar] compiled sidecar matches current sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
