"""Verify that every DLL a shipped Windows binary imports actually resolves.

WHY THIS EXISTS (2026-08-13)
----------------------------
A rebuilt `OrionStream.exe` was deployed after its MSYS2 toolchain bumped FFmpeg 7 -> 8.
The new binary imported `avcodec-62.dll` / `avutil-60.dll`; the deploy folder still held
`avcodec-61.dll` / `avutil-59.dll`. The client therefore died in the Windows loader with
`0xC0000135 STATUS_DLL_NOT_FOUND` before reaching `main()` -- no window, no stdout, no
log line of its own. Every Connect failed, and because the caller only looked for a
`Host:` line it reported "no registered Chiaki nickname", pointing the investigation at
the console instead of at the client.

Nothing in the pipeline would have caught it. `copy_chiaki_runtime()` checked only that
`OrionStream.exe` EXISTS; the packager's forbidden-file scan checks names, not linkage.
A binary that cannot load is indistinguishable from a healthy one until someone runs it.

This module closes that gap by parsing the PE import directory directly -- no dumpbin, no
Dependencies.exe, no toolchain assumptions -- and resolving each imported name the way
Windows would for an application whose directory is the package root.

Deliberately NOT covered (documented so absence is not mistaken for coverage):
  * delay-loaded imports (IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT) -- these fail at first use,
    not at load, and the shipped stack does not currently use them;
  * `LoadLibrary` at runtime (this is how chiaki-ng reaches avrt.dll / vulkan layers) --
    unknowable statically;
  * export-level checks -- a DLL of the right NAME but the wrong version resolves here and
    fails at load with STATUS_ENTRYPOINT_NOT_FOUND (0xC0000139).
The FFmpeg class of breakage -- a SONAME bump leaving the old runtime behind -- is exactly
what this does catch, and it is the one that has actually bitten.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# API sets are resolved by the loader through the API-set schema, not by file lookup.
# They have no on-disk presence and must never be reported missing.
_APISET_PREFIXES = ("api-ms-win-", "ext-ms-")

_SYSTEM_DIRS = (
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32",
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SysWOW64",
)


class PEFormatError(ValueError):
    """The file is not a PE image this parser can read."""


def pe_imports(path: Path) -> list[str]:
    """Return the DLL names in `path`'s import directory (normal imports only)."""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PEFormatError(f"not an MZ image: {path}")
    (e_lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise PEFormatError(f"not a PE image: {path}")

    coff = e_lfanew + 4
    n_sections, = struct.unpack_from("<H", data, coff + 2)
    opt_size, = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    magic, = struct.unpack_from("<H", data, opt)
    if magic not in (0x10B, 0x20B):
        raise PEFormatError(f"unknown optional-header magic 0x{magic:X}: {path}")
    # Data directories start after the standard+windows fields: 96 bytes (PE32) / 112 (PE32+).
    data_dirs = opt + (112 if magic == 0x20B else 96)
    import_rva, import_size = struct.unpack_from("<II", data, data_dirs + 8)
    if import_rva == 0 or import_size == 0:
        return []

    sections = []
    sec_table = opt + opt_size
    for i in range(n_sections):
        base = sec_table + 40 * i
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((vaddr, vsize, raddr, rsize))

    def rva_to_offset(rva: int) -> int | None:
        for vaddr, vsize, raddr, rsize in sections:
            # Use the larger of virtual/raw size: a section may be zero-padded either way.
            if vaddr <= rva < vaddr + max(vsize, rsize):
                return raddr + (rva - vaddr)
        return None

    names: list[str] = []
    offset = rva_to_offset(import_rva)
    if offset is None:
        raise PEFormatError(f"import directory RVA 0x{import_rva:X} is outside every section: {path}")
    while True:
        if offset + 20 > len(data):
            raise PEFormatError(f"truncated import directory: {path}")
        _ilt, _stamp, _chain, name_rva, _iat = struct.unpack_from("<IIIII", data, offset)
        if name_rva == 0:
            break
        name_off = rva_to_offset(name_rva)
        if name_off is None:
            raise PEFormatError(f"import name RVA 0x{name_rva:X} is outside every section: {path}")
        end = data.index(b"\0", name_off)
        names.append(data[name_off:end].decode("latin-1"))
        offset += 20
    return names


def _resolves(dll: str, search_dirs: list[Path]) -> bool:
    lower = dll.lower()
    if lower.startswith(_APISET_PREFIXES):
        return True
    for directory in search_dirs:
        if (directory / dll).exists():
            return True
    return False


def unresolved_imports(root: Path) -> dict[Path, list[str]]:
    """Map each binary under `root` to the DLLs it imports that cannot be found.

    Resolution mirrors the loader for an app whose application directory is `root`:
    the binary's own directory, then `root`, then the system directories. Qt plugins in
    subdirectories therefore correctly resolve `Qt6Core.dll` from the package root.
    """
    root = root.resolve()
    if os.name != "nt":
        # System DLL presence is unknowable off-Windows; a partial answer here would be
        # worse than none, because it would read as a pass.
        raise RuntimeError("import verification requires Windows (system DLL set is host-specific)")

    findings: dict[Path, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".exe", ".dll"):
            continue
        try:
            imported = pe_imports(path)
        except (PEFormatError, OSError, IndexError, struct.error):
            # Not a readable PE (or a stale non-binary with a .exe/.dll name). The
            # packager's own filters decide whether it ships; linkage is moot.
            continue
        search = [path.parent, root, *_SYSTEM_DIRS]
        missing = [d for d in imported if not _resolves(d, search)]
        if missing:
            findings[path] = missing
    return findings


def split_by_severity(
    root: Path, findings: dict[Path, list[str]]
) -> tuple[dict[Path, list[str]], dict[Path, list[str]]]:
    """Split findings into (fatal, degraded).

    FATAL  -- the executable, or a DLL sitting directly in the application directory.
              These are the loader's hard dependency set: one missing name and the
              process dies at 0xC0000135 before `main()`. This is the FFmpeg case.
    DEGRADED -- a plugin in a subdirectory (Qt's platforms/, imageformats/,
              networkinformation/, tls/, ...). Qt probes these opportunistically and
              simply skips any that will not load, so the app still starts with reduced
              capability. Worth reporting, never worth failing a release build over --
              two such gaps have been present in this package for months without
              symptom, and treating them as fatal would just teach everyone to pass
              --allow-degraded-imports.
    """
    root = root.resolve()
    fatal: dict[Path, list[str]] = {}
    degraded: dict[Path, list[str]] = {}
    for path, missing in findings.items():
        top_level = path.resolve().parent == root
        (fatal if (top_level or path.suffix.lower() == ".exe") else degraded)[path] = missing
    return fatal, degraded


def verify_or_raise(root: Path, label: str = "package") -> list[str]:
    """Raise on fatal unresolved imports; return human-readable degraded warnings."""
    fatal, degraded = split_by_severity(root, unresolved_imports(root))
    warnings = [
        f"{path.resolve().relative_to(root.resolve())} will not load (missing "
        f"{', '.join(missing)}) - that plugin's capability is unavailable"
        for path, missing in degraded.items()
    ]
    if fatal:
        lines = [f"{label}: {len(fatal)} binary(ies) import DLLs that are not present."]
        for path, missing in fatal.items():
            lines.append(f"  {path.resolve().relative_to(root.resolve())}: {', '.join(missing)}")
        lines.append(
            "These fail at load with 0xC0000135 STATUS_DLL_NOT_FOUND before main() - "
            "the process never starts and writes no log of its own.")
        raise SystemExit("\n".join(lines))
    return warnings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <folder>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    fatal, degraded = split_by_severity(root, unresolved_imports(root))
    for path, missing in degraded.items():
        print(f"DEGRADED  {path.resolve().relative_to(root.resolve())}: {', '.join(missing)}")
    for path, missing in fatal.items():
        print(f"FATAL     {path.resolve().relative_to(root.resolve())}: {', '.join(missing)}")
    if fatal:
        print(f"\n{len(fatal)} binary(ies) would fail to load (0xC0000135) - the app cannot start.")
        return 1
    if degraded:
        print(f"\nNo fatal problems. {len(degraded)} optional plugin(s) will be skipped by Qt.")
        return 0
    print(f"OK: every import resolves under {root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
