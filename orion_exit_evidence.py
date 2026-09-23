"""Bounded, parent-owned OrionStream termination evidence.

The child cannot write a final line after TerminateProcess or a job close.  The
launcher therefore records intent *before* an action and records its observed
result independently of Chiaki's rotating session logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Mapping


MAX_SESSION_LOG_BYTES = 4 * 1024 * 1024
_MAX_CONTEXT_BYTES = 512 * 1024
_MAX_LEDGER_BYTES = 1024 * 1024
_MAX_BUNDLES = 4
_MAX_BUNDLE_BYTES = 24 * 1024 * 1024
_write_lock = threading.Lock()
_ip_re = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_secret_re = re.compile(rb"\b[0-9a-fA-F]{16,}\b")
_labeled_re = re.compile(rb"(?i)(?:token|key|password|secret|duid|account_id)\s*[:=]\s*\S+")
_opaque_re = re.compile(rb"\b[A-Za-z0-9_+\-/=]{24,}\b")


def _redact(data: bytes) -> bytes:
    data = _labeled_re.sub(b"<redacted-field>", data)
    data = _ip_re.sub(b"<redacted-ip>", data)
    data = _secret_re.sub(b"<redacted-id>", data)
    return _opaque_re.sub(b"<redacted-opaque>", data)


def _tail(path: Path, limit: int, *, redact: bool = True) -> tuple[bytes, bool]:
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        truncated = size > limit
        file.seek(max(0, size - limit), os.SEEK_SET)
        data = file.read(limit)
        return (_redact(data) if redact else data), truncated


def _append_record(log_dir: Path, record: dict) -> None:
    """Append one fsynced, allowlisted JSON line; diagnostics never gate teardown."""
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "orion_parent_termination.jsonl"
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _write_lock:
        if target.exists() and target.stat().st_size + len(encoded) > _MAX_LEDGER_BYTES:
            os.replace(target, log_dir / "orion_parent_termination.jsonl.1")
        with target.open("ab") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())


def _write_incident_bundle(log_dir: Path, record: dict, session_log_path: Path | None,
                           context_paths: Mapping[str, Path]) -> None:
    root = log_dir / "incidents"
    root.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-g{record['session_generation']}-p{record['pid']}"
    folder = root / name
    folder.mkdir()
    files: dict[str, dict] = {}
    inputs: dict[str, tuple[Path | None, int]] = {
        "session.log": (session_log_path, MAX_SESSION_LOG_BYTES),
        "parent_termination.jsonl": (log_dir / "orion_parent_termination.jsonl", _MAX_CONTEXT_BYTES),
    }
    for name, path in context_paths.items():
        if name in ("native.log", "sidecar_fault.log", "sidecar_crash.log"):
            inputs[name] = (Path(path), _MAX_CONTEXT_BYTES)
    for name, (path, limit) in inputs.items():
        if path is None:
            files[name] = {"status": "missing"}
            continue
        try:
            data, truncated = _tail(path, limit)
            output = folder / name
            with output.open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            files[name] = {"status": "copied", "sha256": hashlib.sha256(data).hexdigest(),
                           "bytes": len(data), "truncated": truncated}
        except OSError:
            files[name] = {"status": "missing_or_unreadable"}
    manifest = {"schema": 1, "classification": record["classification"],
                "pid": record["pid"], "creation_time_100ns": record["creation_time_100ns"],
                "session_generation": record["session_generation"], "files": files}
    with (folder / "manifest.json").open("x", encoding="utf-8") as file:
        json.dump(manifest, file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    # Keep the newest incident pinned while bounding total retained evidence.
    folders = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    total = 0
    for index, item in enumerate(folders):
        try:
            size = sum(p.stat().st_size for p in item.iterdir() if p.is_file())
            if index == 0 or (index < _MAX_BUNDLES and total + size <= _MAX_BUNDLE_BYTES):
                total += size
            else:
                for child in item.iterdir():
                    if child.is_file():
                        child.unlink()
                item.rmdir()
        except OSError:
            pass


class SessionExitEvidence:
    """One owned child identity, one terminal classification."""

    def __init__(self, log_dir: os.PathLike | str, *, pid: int,
                 creation_time_100ns: int, session_generation: int,
                 session_log_path: os.PathLike | str | None = None,
                 context_paths: Mapping[str, os.PathLike | str] | None = None,
                 pin_abnormal: bool = True, persist_unbound: bool = False) -> None:
        self.log_dir = Path(log_dir)
        self.pid = int(pid)
        self.creation_time_100ns = int(creation_time_100ns)
        self.session_generation = int(session_generation)
        self.persistent = (self.pid > 0 and (self.session_generation > 0 or persist_unbound)
                           and bool(str(log_dir)))
        self.pin_abnormal = bool(pin_abnormal)
        self.session_log_path = Path(session_log_path) if session_log_path else None
        self.context_paths = {name: Path(path) for name, path in (context_paths or {}).items()}
        self._pending_forced = False
        self._graceful_confirmed = False
        self._forced_confirmed = False
        self._observed_classification: str | None = None
        self._lock = threading.Lock()

    def _record(self, event: str, **fields: object) -> dict:
        record = {"event": event, "time_unix_ns": time.time_ns(), "pid": self.pid,
                  "creation_time_100ns": self.creation_time_100ns,
                  "session_generation": self.session_generation, **fields}
        if self.persistent:
            try:
                _append_record(self.log_dir, record)
            except OSError:
                pass
        return record

    def request(self, initiator: str, reason: str, *, forced: bool,
                requested_exit_code: int) -> None:
        with self._lock:
            self._pending_forced = bool(forced)
            self._record("termination_request", initiator=initiator, reason=reason,
                         forced=bool(forced), requested_exit_code=int(requested_exit_code),
                         call_result="pending")

    def request_result(self, succeeded: bool) -> None:
        with self._lock:
            if succeeded:
                if self._pending_forced:
                    self._forced_confirmed = True
                else:
                    self._graceful_confirmed = True
            self._record("termination_call_result", forced=self._pending_forced,
                         call_result="succeeded" if succeeded else "failed")

    def observe(self, exit_code: int | None) -> str:
        with self._lock:
            if self._observed_classification is not None:
                return self._observed_classification
            child_tail = b""
            if self.session_log_path:
                try:
                    child_tail, _ = _tail(self.session_log_path, 128 * 1024, redact=False)
                except OSError:
                    pass
            child_final_zero = b"Orion process_exit code=0" in child_tail
            child_fault = (b"orion_bridge_fatal" in child_tail
                           or b"Orion process_exit code=1" in child_tail)
            if self._forced_confirmed:
                classification = "forced_by_parent"
            elif exit_code == 0 and self._graceful_confirmed:
                classification = "clean"
            elif child_fault:
                classification = "child_fault"
            elif exit_code == 0 and child_final_zero:
                classification = "clean"
            else:
                classification = "external_or_runtime_unknown"
            self._observed_classification = classification
            record = self._record("observed_exit", exit_code=exit_code,
                                  classification=classification,
                                  child_final_line=child_final_zero,
                                  child_fault_line=child_fault)
            if classification != "clean" and self.persistent and self.pin_abnormal:
                try:
                    _write_incident_bundle(self.log_dir, record, self.session_log_path,
                                           self.context_paths)
                except OSError:
                    pass
            return classification
