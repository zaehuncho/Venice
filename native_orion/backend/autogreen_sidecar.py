#!/usr/bin/env python3
"""
Autogreen Remote Play sidecar for the Orion native launcher.

This is a long-running process that:
  - Starts or attaches to Chiaki and consumes decoded frames (or an HDMI capture card);
    Win32 window capture is a development-only fallback
  - Runs the meter detector + green window analyzer
  - Holds and releases the virtual controller through ViGEmBus
  - Forwards live status as JSONL on stdout for the C++ launcher to read

Usage:
    python autogreen_sidecar.py --root <NexusVision-root> --config-json <inline-JSON>

The sidecar reads commands on stdin (also JSONL) so the launcher can
push live config updates and trigger goto-shot manually.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import queue
import signal
import statistics
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

try:
    # Generated immediately before the production Nuitka build.  Keeping this a
    # literal module lets the built executable prove which source inventory it
    # actually contains; a JSON manifest cannot self-attest an older binary.
    from orion_sidecar_build_id import ORION_SIDECAR_SOURCE_DIGEST
except ImportError:
    ORION_SIDECAR_SOURCE_DIGEST = ""

# Ensure NexusVision modules import correctly
def _bootstrap(root: Path) -> None:
    candidates = [root, root.parent, Path(__file__).resolve().parents[2]]
    for c in candidates:
        if (c / "remote_play_orchestrator.py").is_file():
            sys.path.insert(0, str(c))
            return


_emit_lock = threading.Lock()

# Module logger for HOT paths. Deliberately NOT the JSON `_log` emit: a per-frame failure written
# to stdout would flood the launcher's JSONL protocol (and, on the preview path, interleave with
# the ~130KB base64 frame lines). stderr logging is relayed by the native side's ERROR/CRITICAL/
# WARNING filter and is always captured in the rolling sidecarStderrTail_, so it is visible without
# being able to corrupt the command/telemetry channel. `_log`/`_emit` stay reserved for rare,
# user-relevant events.
_LOG = logging.getLogger("autogreen")


def _detector_provider_smoke(
    model_path: Path, *, warmup: int = 20, runs: int = 100,
    p90_limit_ms: float = 35.0, output_path: Path | None = None,
) -> int:
    """Exercise the detector through the provider embedded by Nuitka.

    Production intentionally requires DirectML: CUDAExecutionProvider depends
    on a large CUDA/cuDNN DLL set that is not part of the release. Testing the
    compiled executable catches a bundle that imports on the build host but
    silently falls back to the timing-ineligible CPU provider after packaging.
    """

    try:
        import numpy as np
        from meter_detector_yolo import MeterYoloLocator

        os.environ["ORION_METER_PROVIDER_PRIORITY"] = "dml,cpu"
        detector = MeterYoloLocator(model_path=str(model_path))
        if not detector.ok:
            raise RuntimeError("detector did not load")
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for _ in range(max(0, int(warmup))):
            detector.detect_box(frame)
        samples = []
        for _ in range(max(1, int(runs))):
            started = time.perf_counter()
            detector.detect_box(frame)
            samples.append((time.perf_counter() - started) * 1000.0)
        ordered = sorted(samples)
        p90 = ordered[min(len(ordered) - 1, math.ceil(0.90 * len(ordered)) - 1)]
        p99 = ordered[min(len(ordered) - 1, math.ceil(0.99 * len(ordered)) - 1)]
        payload = {
            "ok": (
                detector.provider == "DmlExecutionProvider"
                and p90 <= float(p90_limit_ms)
            ),
            "provider": detector.provider,
            "model": model_path.name,
            "runs": len(samples),
            "median_ms": round(statistics.median(samples), 3),
            "p90_ms": round(p90, 3),
            "p99_ms": round(p99, 3),
            "max_ms": round(max(samples), 3),
            "p90_limit_ms": float(p90_limit_ms),
        }
        line = json.dumps(payload, sort_keys=True)
        if output_path is not None:
            output_path.write_text(line + "\n", encoding="utf-8", newline="\n")
        try:
            print(line, flush=True)
        except (AttributeError, OSError):
            pass
        return 0 if payload["ok"] else 5
    except Exception as exc:
        line = json.dumps(
            {
                "ok": False,
                "provider": "unavailable",
                "model": model_path.name,
                "error": str(exc),
            },
            sort_keys=True,
        )
        if output_path is not None:
            output_path.write_text(line + "\n", encoding="utf-8", newline="\n")
        try:
            print(line, flush=True)
        except (AttributeError, OSError):
            pass
        return 6

# FIX 3 (silent drops on the live path): every swallowed non-fatal fault on a path that MATTERS
# (frame delivery, preview encode, telemetry emit, resource teardown) increments a counter here
# instead of vanishing into a bare `except: pass`. `preview_*` are summed into the telemetry
# `preview_dropped` field + the 5s preview_stats line so a customer-visible "the video is choppy /
# the bot sees nothing" can be traced to a real number instead of guessed at.
_DROPS: dict = {}
_DROPS_LOCK = threading.Lock()


def _note_drop(kind: str, exc=None) -> int:
    """Count a non-fatal drop and make it VISIBLE without spamming.

    The first occurrence of each `kind` is logged at WARNING (so the native stderr relay surfaces
    it exactly once), then every 500th, with everything in between at DEBUG. Returns the running
    count for the kind so a caller can decide whether the event is rare enough to also `_log` it
    to the launcher. Never raises — this is called from `except` handlers on 60fps paths."""
    try:
        with _DROPS_LOCK:
            n = _DROPS.get(kind, 0) + 1
            _DROPS[kind] = n
        if n == 1 or n % 500 == 0:
            _LOG.warning("drop[%s] n=%d: %r", kind, n, exc)
        else:
            _LOG.debug("drop[%s] n=%d: %r", kind, n, exc)
        return n
    except Exception:
        return 0


def _drop_count(*kinds: str) -> int:
    """Sum of the named drop counters (plain int reads; the GIL makes this consistent enough for
    a telemetry gauge without taking the lock on the emit path)."""
    total = 0
    for k in kinds:
        try:
            total += int(_DROPS.get(k, 0))
        except Exception:
            pass
    return total


def _release_window_diagnostic_wire(record) -> dict | None:
    """Serialize only a real, immutable native release-window diagnostic.

    Proxy records remain useful in the reader's local forensic log, but they cannot enter the
    native telemetry stream.  This is a defense-in-depth boundary in addition to the orchestrator
    and AutomationEngine checks.
    """
    try:
        if not isinstance(record, dict) or bool(record.get("release_proxy", True)):
            return None
        label = str(record.get("label", "")).upper()
        seq = int(record.get("seq", 0))
        release_seq = int(record.get("release_seq", 0))
        physical_epoch = _safe_uint64(record.get("physical_epoch"))
        shot_attempt = _safe_uint64(record.get("shot_attempt"))
        g_lo = float(record.get("g_lo", -1.0))
        fill_at_release = float(record.get("fill_at_release", -1.0))
        window_conf = float(record.get("window_conf", 0.0))
        if (label not in {"EARLY", "GREEN", "OVER"} or seq <= 0
                or release_seq <= 0 or physical_epoch <= 0 or shot_attempt <= 0):
            return None
        if not all(math.isfinite(v) for v in (g_lo, fill_at_release, window_conf)):
            return None
        return {
            "label": label,
            "seq": seq,
            "release_seq": release_seq,
            "physical_epoch": str(physical_epoch),
            "shot_attempt": str(shot_attempt),
            "g_lo": g_lo,
            "fill_at_release": fill_at_release,
            "window_conf": window_conf,
            "release_proxy": False,
        }
    except (TypeError, ValueError, OverflowError):
        return None


def _trusted_rtt_snapshot_ms(snap):
    """Extract release-authoritative RTT from one already-captured snapshot."""
    try:
        if not (bool(getattr(snap, "ready", False))
                and bool(getattr(snap, "target_verified", False))):
            return None
        rtt_ms = float(getattr(snap, "rtt_filtered_ms", 0.0) or 0.0)
        if not math.isfinite(rtt_ms) or rtt_ms <= 0.0 or rtt_ms > 500.0:
            return None
        return rtt_ms
    except Exception:
        return None


def _trusted_live_rtt_ms(orch):
    """Return the current filtered RTT only when its route identity is verified.

    An unverified target (or a merely warming RTT engine) must not move release authority.  The
    estimator falls back to its calibration-shot RTT median when this returns ``None``.
    """
    try:
        engine = getattr(orch, "_rtt_engine", None)
        return _trusted_rtt_snapshot_ms(engine.get_snapshot()) if engine is not None else None
    except Exception:
        return None


def _latency_oracle_wire(snapshot, live_rtt_ms=None,
                         controller_attestation_generation=0,
                         controller_delivery_route="",
                         scope_epoch=0) -> dict:
    """Serialize one coherent latency-oracle snapshot, including its cold state.

    Older consumers ignore these additive fields. Native already treats an
    explicit measured_latency_ms=0 exactly like an omitted value, so exposing
    N/readiness/starvation/rejection cannot accidentally grant timing authority.
    """
    if snapshot is None:
        return {}
    # Backward-compatible only for direct helper callers. Production captures
    # telemetry_snapshot() once before entering this function and passes the
    # returned frozen object; no live estimator property is sampled here.
    snapshot_getter = getattr(snapshot, "telemetry_snapshot", None)
    if callable(snapshot_getter):
        snapshot = snapshot_getter()

    def _field(name, default):
        value = getattr(snapshot, name, default)
        return value() if callable(value) else value

    # ``live_rtt_ms`` is retained only for packaged-call compatibility. The RTT engine measures
    # PC -> public court-server reachability, not the private PS5/Chiaki or HDMI path inside the
    # command-to-visible meter loop. Feeding it into value_ms() moves release authority on an
    # unrelated clock. Both production video routes therefore consume the oracle's directly
    # measured TOTAL value. Predictor samples already live on the capture-aligned clock, so native
    # must not add route frame age to this command-to-visible-effect value a second time.
    _ = live_rtt_ms
    # Keep the observed posterior and the value permitted to actuate as two
    # separate wire fields.  In particular, an ordinary N=1 observation may
    # move ``value_ms`` but must not silently replace the packaged factory lead.
    value_ms = float(_field("value_ms", 0.0) or 0.0)
    # Native spends N as a count of VALIDATION-CAPABLE evidence, at both ends: the factory contract
    # requires N < 6 and the passive convergence path requires N >= 6. Corroboration labels are
    # deliberately excluded from ever minting validated authority, so publishing the raw total here
    # walks a session off a cliff: six clean post-L1 target-less traces demote to corroboration,
    # push the total to 6, and native then fails BOTH gates at once -- factoryPriorStructured goes
    # false because N is no longer < 6, while validated is unreachable by construction. The engine
    # loses lead authority entirely and stops shooting mid-session. Publish the validation-capable
    # count; the raw total travels separately as measured_latency_corroboration_n for diagnostics.
    corroboration_n = max(0, int(_field("corroboration_labels", 0) or 0))
    n_total = max(0, int(_field("n_labels", 0) or 0))
    n_labels = max(0, n_total - corroboration_n)
    sd_ms = float(_field("l_fixed_sd_ms", 0.0) or 0.0)
    factory_prior_claim = bool(_field("factory_prior_active", False))
    default_ready = (value_ms > 0.0 and n_labels >= 6 and math.isfinite(sd_ms)
                      and 0.0 < sd_ms <= 3.3)
    ready_for_native = bool(_field("ready_for_native", default_ready))
    # Authority is an atomic producer contract, not something the serializer may
    # reconstruct from mutable observation fields. A stale/mixed packaged build
    # that lacks any member of the tuple remains useful for diagnostics but must
    # fail closed for actuation. This specifically prevents an ordinary N=1
    # observation from masquerading as the immutable factory value.
    explicit_authority_tuple = all(hasattr(snapshot, name) for name in (
        "authority_kind", "authority_value_ms", "authority_sd_ms"))
    authority_kind = (str(_field("authority_kind", "none") or "none")
                      .strip().lower() if explicit_authority_tuple else "none")
    if authority_kind not in ("factory", "validated", "none"):
        authority_kind = "none"
    authority_value_ms = float(
        _field("authority_value_ms", 0.0) or 0.0) if explicit_authority_tuple else 0.0
    authority_sd_ms = float(
        _field("authority_sd_ms", 0.0) or 0.0) if explicit_authority_tuple else 0.0
    authority_valid = (
        authority_kind in ("factory", "validated")
        and math.isfinite(authority_value_ms)
        and 0.0 < authority_value_ms <= 500.0
        and math.isfinite(authority_sd_ms)
        and 0.0 < authority_sd_ms <= 250.0)
    if authority_kind == "factory":
        authority_valid = authority_valid and factory_prior_claim
    elif authority_kind == "validated":
        authority_valid = authority_valid and ready_for_native
    if not authority_valid:
        authority_kind = "none"
        authority_value_ms = 0.0
        authority_sd_ms = 0.0
    factory_prior = authority_kind == "factory"
    status = str(_field("last_status", "") or "unknown")[:64]
    rejection = str(_field("last_rejection", "") or "")[:64]
    method = str(_field("label_method", "") or "")[:32]
    generation = _safe_uint64(controller_attestation_generation)
    epoch = _safe_uint64(scope_epoch)
    delivery_route = str(controller_delivery_route or "").strip().lower()
    # Route generation and scope epoch are one provenance tuple.  A legacy or
    # torn read with no epoch must never authorize timing on a replaced
    # estimator, even when the route/generation pair itself looks plausible.
    if (delivery_route not in ("pipe", "vigem_ds4", "vigem_xusb")
            or epoch <= 0):
        generation = 0
        delivery_route = ""
    return {
        "measured_latency_ms": value_ms,
        "measured_latency_observed_ms": value_ms,
        "measured_latency_authority_kind": authority_kind,
        "measured_latency_authority_ms": authority_value_ms,
        "measured_latency_authority_sd_ms": authority_sd_ms,
        "measured_latency_conf": float(_field("confidence", 0.0) or 0.0),
        "measured_latency_boot": bool(_field("bootstrapped", True)),
        "measured_latency_factory_prior": factory_prior,
        "measured_latency_prior_source": str(
            _field("factory_prior_source", "") or "")[:112]
            if factory_prior else "",
        "measured_latency_model_version": str(
            _field("factory_prior_version", "") or "")[:32]
            if factory_prior else "",
        "measured_latency_n": n_labels,
        # Diagnostics only. Native must not add these back into its N gates -- see the comment at
        # the n_labels computation above for why that would revoke lead authority mid-session.
        "measured_latency_corroboration_n": corroboration_n,
        "measured_latency_total_n": n_total,
        "measured_latency_ready": ready_for_native,
        # L1 is telemetry-only. Native may use it solely to schedule the distinct in-green
        # validation stop; this bit is never sufficient for tip actuation.
        "measured_latency_controlled_anchor": bool(
            _field("controlled_anchor_available", False)),
        # Validated warm authority is distinct from L1 telemetry. Native repeats the N/SD,
        # controlled-anchor, freshness and epoch bounds and ignores inconsistent snapshots.
        "measured_latency_provisional": bool(
            _field("provisional_ready", False)),
        # Restored total timing is explicitly provisional. Python has independently attested only
        # the exact video route/capture health; native must also prove its selected controller
        # delivery route with a neutral packet before this value can become actuatable.
        "measured_latency_restored": bool(
            _field("restored_from_cache", False)),
        "measured_latency_video_route_attested": bool(
            _field("restored_route_attested", False)),
        # Canonical decimal string: JSON numbers cannot exactly carry every uint64.  Zero/absent
        # is fail-closed and is intentionally not emitted as a valid attestation generation.
        "measured_latency_attestation_generation": str(generation) if generation else "",
        "measured_latency_delivery_route": delivery_route if generation else "",
        "measured_latency_scope_epoch": str(epoch) if epoch else "",
        "measured_latency_starved": bool(
            _field("label_starved", False)),
        # [ORION_AUTO_PROBE_ONBOARD]/[ORION_PROBE_PERSIST] additive, flag-gated (default OFF ->
        # False on every install today). probe_recommended is the fresh-install onboarding hint:
        # native may auto-start the warmup probe run only after ANDing its own conditions
        # (user lead not set, engine idle, streaming, one run per session). Telemetry only —
        # neither field participates in any authority gate.
        "measured_latency_probe_recommended": bool(
            _field("probe_recommended", False)),
        "measured_latency_probe_prior_restored": bool(
            _field("probe_prior_restored", False)),
        # [ORION_REOPEN_SOFT] additive, flag-gated (default OFF -> always False). True while the
        # estimator publishes its retained pre-reopen converged authority instead of the live
        # (reopened) posterior. Diagnostics only -- native gates on the numeric authority tuple.
        "measured_latency_reopen_retained": bool(
            _field("reopen_guard_active", False)),
        "measured_latency_status": status,
        "measured_latency_last_rejection": rejection,
        "measured_latency_method": method,
        "measured_latency_rtt_regime": str(
            _field("rtt_regime", "unknown") or "unknown")[:16],
        "measured_latency_scope": "end_to_end",
        "measured_l_fixed_ms": float(
            _field("l_fixed_ms", 0.0) or 0.0),
        "measured_latency_sd_ms": sd_ms,
    }


def _latency_scope_epoch_snapshot(orch) -> int:
    """Read the sidecar process-local timing scope epoch, failing closed."""
    getter = getattr(orch, "latency_estimator_scope_epoch", None)
    if not callable(getter):
        return 0
    try:
        return _safe_uint64(getter())
    except Exception as exc:
        _LOG.warning("latency scope epoch snapshot failed: %r", exc)
        return 0


def _adopt_shared_stdout_lock() -> None:
    """Bughunt #4: the orchestrator has in-process emitters that write the SAME stdout JSONL
    pipe as _emit (pose_landmark — the no-meter RELEASE trigger — pose_overlay every frame,
    calibrate_meter_status). The ~130KB preview line splits across several underlying
    BufferedWriter writes, so a writer that doesn't hold _emit_lock can interleave mid-line
    and corrupt both lines (the launcher's JSON parse drops them silently — a lost meter
    sample at the tip = mistimed release). Adopt the orchestrator's module-level
    STDOUT_EMIT_LOCK so every stdout writer in this process serializes on ONE lock. Called
    from main() once the orchestrator module is importable (after _bootstrap)."""
    global _emit_lock
    try:
        import remote_play_orchestrator as _rpo
        shared = getattr(_rpo, "STDOUT_EMIT_LOCK", None)
        if shared is not None and hasattr(shared, "acquire"):
            _emit_lock = shared
    except Exception as exc:
        # NOT silent: without the shared lock the orchestrator's in-process emitters (pose_landmark
        # — the no-meter RELEASE trigger — pose_overlay, calibrate_meter_status) can interleave
        # mid-line with our preview/telemetry writes and the launcher silently drops BOTH lines
        # (a lost meter sample at the tip = a mistimed release). Once, at startup, so WARNING is
        # cheap and the native relay surfaces it.
        _LOG.warning("shared stdout lock NOT adopted (%r) - stdout JSONL lines may interleave", exc)


def _emit(payload: dict) -> bool:
    # Serialise writes: the telemetry, main, and (60fps) preview-encoder threads all
    # emit. The preview's ~130KB base64 line can be split into several underlying
    # BufferedWriter writes, so without this lock a concurrent telemetry line could
    # interleave mid-line and corrupt the launcher's JSON parse.
    try:
        # STAGE SPLIT (2026-08-06): stamp the dispatch instant on telemetry lines so the
        # capture->decision staleness can be split into its two halves instead of being one
        # opaque number. With capture_ts already on the payload the native side gets:
        #   capture -> emit_ts_ms   = sidecar-side cost (detect + payload build)
        #   emit_ts_ms -> receipt   = serialise + emit lock + pipe + native event loop
        # Two whole sessions ran p50 31-36ms staleness (+19ms vs baseline) with no way to tell
        # [ORION_EMIT_TS_CORRECTION 2026-08-07] Both stamps recorded. queued_ts_ms is the
        # pre-lock stamp (was `emit_ts_ms` before this fix); emit_ts_ms is now WIRE TIME.
        #
        # Why: on the packaged install, the preview-encoder chunker (60 fps * ~25 flushes
        # per frame) monopolises _emit_lock and telemetry queued for MULTIPLE SECONDS
        # behind it (n=447 samples with p50=1538 ms measured). With the timestamp taken
        # pre-lock, the "emit_ts_ms -> receipt" metric reported that queue wait as wire
        # latency, and the native side interpreted it as a broken IPC path -- SAFE MODE
        # and gate-open behaviour followed. Stamping INSIDE the lock reports the actual
        # wire cost the receipt latency was invented to measure. The pre-lock stamp is
        # kept as queued_ts_ms so the queue-vs-wire split stays diagnosable (the original
        # intent of the pre-lock stamp) -- the difference (emit - queued) is now the
        # lock-contention component, cleanly separable from wire time (receipt - emit).
        #
        # Preview lock contention itself is a separate fix (mandate SHM for preview so
        # the 130KB JPEG never touches this pipe); tracked in POST_LAUNCH_HARDENING.md.
        if payload.get("event") == "telemetry":
            payload["queued_ts_ms"] = time.time() * 1000.0
        line = None
        with _emit_lock:
            if payload.get("event") == "telemetry":
                payload["emit_ts_ms"] = time.time() * 1000.0
                line = json.dumps(payload, separators=(",", ":")) + "\n"
            else:
                line = json.dumps(payload, separators=(",", ":")) + "\n"
            sys.stdout.write(line)
            sys.stdout.flush()
        return True
    except Exception as exc:
        # A failed write here means the launcher LOST this line (telemetry sample, preview frame,
        # started/error event). Must stay non-fatal, and must NOT recurse through _log/_emit — a
        # broken stdout pipe would loop forever. Counter + stderr logging only.
        _note_drop("emit", exc)
        return False


def _log(msg: str, level: str = "info") -> None:
    _emit({"event": "log", "level": level, "msg": msg})


_PREVIEW_SHM_WRITE_FAILURE_LIMIT = 10


class _PreviewShmFailover:
    """One-way, session-scoped SHM -> JPEG handoff state.

    A failed SHM write must never opportunistically emit a JPEG while native is
    still consuming the mapping: that creates two independently queued preview
    sources and lets an older SHM completion overwrite the newer JPEG.  The
    first nine consecutive failures therefore shed display-only frames.  At the
    bounded threshold the writer is retired and this object emits one FIFO
    control record before the first JPEG record is allowed onto stdout.

    The per-session ready-event name is a source-epoch fence.  It is not a
    secret, but it prevents a delayed line from an older sidecar generation from
    retiring the current mapping.
    """

    def __init__(self, ready_event_name: str,
                 failure_limit: int = _PREVIEW_SHM_WRITE_FAILURE_LIMIT):
        self.ready_event_name = str(ready_event_name or "")
        self.failure_limit = max(1, int(failure_limit))
        self.failure_streak = 0
        self.pending = False
        self.announced = False
        self.reason = ""
        self.first_jpeg_frame_number = 0

    def note_success(self) -> None:
        if not self.pending and not self.announced:
            self.failure_streak = 0

    def note_failure(self, frame_number: int) -> bool:
        """Return true exactly when the writer must be retired."""
        if self.pending or self.announced:
            return False
        self.failure_streak += 1
        if self.failure_streak < self.failure_limit:
            return False
        self.request("shm_write_failures", frame_number)
        return True

    def request(self, reason: str, first_jpeg_frame_number: int = 0) -> None:
        if self.announced:
            return
        self.pending = True
        self.reason = str(reason or "shm_unavailable")[:48]
        try:
            self.first_jpeg_frame_number = max(0, int(first_jpeg_frame_number or 0))
        except (TypeError, ValueError, OverflowError):
            self.first_jpeg_frame_number = 0

    def payload(self) -> dict:
        return {
            "event": "preview_transport",
            "protocol": 1,
            "mode": "jpeg",
            "reason": self.reason or "shm_unavailable",
            "shm_ready_event": self.ready_event_name,
            "first_jpeg_frame_number": self.first_jpeg_frame_number,
        }

    def announce(self, emit_fn=None) -> bool:
        """Publish the handoff once; false means JPEG must remain withheld."""
        if self.announced:
            return True
        if not self.pending:
            return False
        sender = emit_fn or _emit
        if not bool(sender(self.payload())):
            return False
        self.pending = False
        self.announced = True
        return True


def _retire_preview_shm_after_write_failure(writer, failover, frame_number,
                                            emit_fn=None):
    """Actual preview-loop seam for one failed SHM write.

    Returns ``(writer_or_none, allow_jpeg, retired_now)``.  ``allow_jpeg`` is
    false for every transient failure and while the FIFO control record cannot
    be written.  Tests drive this same function with a fake writer, so the
    no-mixed-transport invariant is behavioral rather than a source-text check.
    """
    if writer is None or failover is None:
        return writer, False, False
    if not failover.note_failure(frame_number):
        return writer, False, False
    try:
        writer.stop()
    except Exception as exc:
        _note_drop("preview_shm_stop", exc)
    return None, bool(failover.announce(emit_fn)), True


def _build_frame_line(b64: bytes, frame_number: int) -> bytes:
    """[D2] Assemble the preview `frame` JSONL line directly in BYTES.

    The generic `_emit` path costs four full-size copies + a scan of a 200-600 KB payload, ALL of
    them holding the GIL (so they stall the 60 Hz capture/detect thread in the same process):

        buf.tobytes()          -> copy of the JPEG
        .decode("ascii")       -> bytes -> str copy
        json.dumps(...)        -> scans every character looking for something to escape, then
                                  builds yet another str
        sys.stdout.write(str)  -> str -> UTF-8 encode = a fourth copy

    None of that work can find anything to do: base64 output is drawn from ``[A-Za-z0-9+/=]``, so
    it contains no character JSON has to escape and no character UTF-8 has to widen. Concatenating
    the three literal fragments around the raw base64 bytes produces a byte-identical line for
    ~one copy. The schema is deliberately unchanged (same keys, same order) — the native side's
    fast byte-slice path scans for ``"jpeg_b64":"`` and its ordinary QJsonDocument parse both still
    read it verbatim.

    Pure + bytes-in/bytes-out so the exact wire format can be pinned by a unit test.
    """
    if not isinstance(b64, (bytes, bytearray)):
        raise TypeError("b64 must be raw base64 bytes")
    return (b'{"event":"frame","jpeg_b64":"' + bytes(b64)
            + b'","frame_number":' + str(int(frame_number)).encode("ascii") + b'}\n')


def _emit_frame_line(b64: bytes, frame_number: int) -> bool:
    """Write one preview frame line (see _build_frame_line) to stdout. Returns success.

    Takes the SAME `_emit_lock` every other stdout writer in this process takes, and drains the
    text layer first, so the binary write can never interleave mid-line with a telemetry /
    pose_landmark line (the launcher silently drops both halves of a spliced line). A stdout object
    with no binary layer (a test harness swapping in StringIO) falls back to the ordinary `_emit`
    so the frame still reaches the launcher rather than vanishing."""
    try:
        line = _build_frame_line(b64, frame_number)
    except Exception as exc:
        _note_drop("preview_emit", exc)
        return False

    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        try:
            _emit({"event": "frame",
                   "jpeg_b64": bytes(b64).decode("ascii"),
                   "frame_number": int(frame_number)})
            return True
        except Exception as exc:
            _note_drop("preview_emit", exc)
            return False
    try:
        with _emit_lock:
            sys.stdout.flush()
            buf.write(line)
            buf.flush()
        return True
    except Exception as exc:
        _note_drop("preview_emit", exc)
        return False


_PREVIEW_CHUNK_BYTES = 8 * 1024
_PREVIEW_MAX_B64_BYTES = 2 * 1024 * 1024


def _build_frame_chunk_line(b64_chunk: bytes, frame_number: int, chunk_frame_id: int,
                            chunk_index: int, chunk_count: int) -> bytes:
    """Build one bounded preview record; pure for protocol tests."""
    if not isinstance(b64_chunk, (bytes, bytearray)):
        raise TypeError("b64_chunk must be bytes")
    if not (0 <= int(chunk_index) < int(chunk_count) <= 256):
        raise ValueError("invalid preview chunk index/count")
    if int(chunk_frame_id) <= 0:
        raise ValueError("invalid preview chunk frame id")
    if not (0 < len(b64_chunk) <= _PREVIEW_CHUNK_BYTES):
        raise ValueError("invalid preview chunk size")
    return (b'{"event":"frame_chunk","frame_number":'
            + str(int(frame_number)).encode("ascii")
            + b',"chunk_frame_id":' + str(int(chunk_frame_id)).encode("ascii")
            + b',"chunk_index":' + str(int(chunk_index)).encode("ascii")
            + b',"chunk_count":' + str(int(chunk_count)).encode("ascii")
            + b',"jpeg_b64":"' + bytes(b64_chunk) + b'"}\n')


def _emit_frame_chunks(b64: bytes, frame_number: int, chunk_frame_id: int = 0,
                       priority_event=None, lock_timeout_s: float = 0.002) -> bool:
    """Emit a preview as small independently locked JSONL records.

    A full 1600px JPEG/base64 line can exceed 200 KB. A blocking stdout write
    holding the shared lock made fresh meter telemetry wait behind display-only
    bytes. Releasing the lock after every <=8 KB payload bounds that inversion to
    one chunk; if detector telemetry becomes pending between chunks, abandon the
    incomplete preview (native latest-wins reassembly discards it) and yield now.
    """
    try:
        raw = bytes(b64)
        if not raw or len(raw) > _PREVIEW_MAX_B64_BYTES:
            raise ValueError("preview base64 outside bounded frame contract")
        chunk_frame_id = int(chunk_frame_id or frame_number or 0)
        if chunk_frame_id <= 0:
            raise ValueError("preview chunk transport identity is missing")
        chunk_count = (len(raw) + _PREVIEW_CHUNK_BYTES - 1) // _PREVIEW_CHUNK_BYTES
        buf = getattr(sys.stdout, "buffer", None)
        for chunk_index in range(chunk_count):
            if priority_event is not None and priority_event.is_set():
                return False
            start = chunk_index * _PREVIEW_CHUNK_BYTES
            line = _build_frame_chunk_line(
                raw[start:start + _PREVIEW_CHUNK_BYTES], frame_number, chunk_frame_id,
                chunk_index, chunk_count)
            if not _emit_lock.acquire(timeout=max(0.0, float(lock_timeout_s))):
                return False
            try:
                # Recheck after acquiring: telemetry may have become ready while
                # this display thread waited for the preceding small writer.
                if priority_event is not None and priority_event.is_set():
                    return False
                if buf is None:
                    written = sys.stdout.write(line.decode("ascii"))
                    sys.stdout.flush()
                else:
                    sys.stdout.flush()
                    written = buf.write(line)
                    buf.flush()
                if written is not None and int(written) != len(line):
                    raise OSError("short preview chunk write")
            finally:
                _emit_lock.release()
            # Give a telemetry waiter a scheduling point before this display-only
            # producer attempts the next chunk's lock.
            if priority_event is not None and priority_event.is_set():
                return False
            time.sleep(0)
        return True
    except Exception as exc:
        _note_drop("preview_emit", exc)
        return False


def _apply_stall_override(payload: dict, pixel_age_ms: float, stall_ms: float) -> bool:
    """RC-2b: synthesize a no-meter state on a capture STALL. `pixel_age_ms` is the age since the
    last UNIQUE frame (the honest staleness — frame_age_ms is refreshed by duplicate frames and reads
    ~0 through a freeze). When it crosses `stall_ms`, force meter_present/raw_fed false + zero the shot
    fill so the native fresh gate can't keep serving a frozen held fill (the 293s HOLD / RISE-0.0 /
    idle_overlay echo). Returns whether a stall was applied. Pure + side-effecting on `payload` so it
    is unit-testable without the orchestrator/capture stack."""
    try:
        stalled = float(pixel_age_ms or 0.0) >= float(stall_ms)
    except Exception:
        stalled = False
    payload["stalled"] = bool(stalled)
    if stalled:
        # A pixel-age stall is an integrity failure even when a nonblocking backend
        # is still returning the same cached FrameData.  Label it unhealthy before
        # the common health override strips held bbox/tracking/fusion state.
        payload["feed_healthy"] = False
        if not payload.get("frame_reject_reason"):
            payload["frame_reject_reason"] = "pixel_stall"
        payload["meter_present"] = False
        payload["raw_fed"] = False
        payload["gameplay_structure_verified"] = False
        payload["gameplay_structure_epoch"] = "0"
        shot = payload.get("shot")
        if isinstance(shot, dict):
            shot["fill_pct"] = 0.0
            shot["confidence"] = 0.0
    return stalled


def _apply_feed_health_override(payload: dict) -> bool:
    """Remove every actionable held detector value when frame integrity is unhealthy.

    ``feed_healthy=false`` is the fail-closed authority.  Merely adding that bit while
    leaving a prior ``meter_present``/bbox/tracking/fusion payload intact asks every
    downstream consumer to remember to ignore stale state.  Zero/remove it here as well,
    so a dark/torn/duplicate/stale frame cannot be served as a fresh bot sample or overlay.
    """
    healthy = bool(payload.get("feed_healthy", False))
    payload["frame_integrity_ok"] = healthy
    if healthy:
        return False
    payload["meter_present"] = False
    payload["raw_fed"] = False
    payload["gameplay_structure_verified"] = False
    payload["gameplay_structure_epoch"] = "0"
    payload["stalled"] = True
    shot = payload.get("shot")
    if isinstance(shot, dict):
        shot["fill_pct"] = 0.0
        shot["confidence"] = 0.0
    payload.pop("green", None)
    payload.pop("bbox", None)
    # [ORION_PROOF_DETECTOR_BOX 2026-09-19] the detector rectangle rides the bbox exactly
    payload.pop("det_bbox", None)
    payload.pop("bbox_wh", None)
    payload.pop("tracking", None)
    payload.pop("fusion", None)
    return True


def _snapshot_pairs_dict(raw) -> dict:
    """Thaw immutable ``(name, scalar)`` pairs copied at detector completion."""
    try:
        return dict(raw or ())
    except Exception:
        return {}


def _tip_registration_wire(raw) -> dict:
    """Serialize one already frame-joined registration snapshot.

    ``raw`` must come from ``_ProcessedFrameSnapshot``.  This helper deliberately
    has no orchestrator fallback, so telemetry cannot read detector frame N+1 while
    it is still emitting the immutable identity for frame N.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    try:
        seq = int(raw.get("seq", 0) or 0)
        if seq <= 0:
            return {}
        return {
            "reg_tip_ms": float(raw.get("reg_tip_ms", -1.0)),
            "reg_conf": float(raw.get("reg_conf", 0.0)),
            "reg_seq": seq,
            "reg_rmse_pp": float(raw.get("reg_rmse_pp", -1.0)),
            "reg_n": int(raw.get("reg_n", 0)),
            "reg_tip_capture_ms": float(raw.get("reg_tip_capture_ms", 0.0)),
            "reg_sample_capture_ms": float(raw.get("reg_sample_capture_ms", 0.0)),
            "reg_sigma_ms": float(raw.get("reg_sigma_ms", -1.0)),
            "reg_model_id": str(raw.get("reg_model_id", ""))[:64],
            "reg_model_version": str(raw.get("reg_model_version", ""))[:32],
            "reg_fit_method": str(raw.get("reg_fit_method", ""))[:48],
        }
    except (TypeError, ValueError, OverflowError):
        return {}


# [METER DETECTION CARD 2026-09-10] Cadence of the reader's detector-health line. It rides the
# telemetry THREAD (one periodic mechanism) but never the telemetry PAYLOAD: the 60 Hz meter
# feed is the engine's input and must not grow for a display value.
_DETECTOR_HEALTH_INTERVAL_S = 2.0


def _detector_health_wire(orch) -> dict | None:
    """{"event":"detector_health", ...} for the native Meter Detection card, or None while the
    orchestrator has no reader with a health snapshot (pre-warm, retired chain, legacy reader).

    Flat copy of SimpleMeterReader.detector_health_snapshot(): provider ("cv-contour" or an
    ONNX provider id), infer_ms, state (idle|pending|locked), calls/found/locks/drops/
    hot_submit/reseat_x and the cv_* gate counters when the pure-CV proposer is active.
    PRESENTATION ONLY -- native formats it into a status line; nothing reaches the engine."""
    reader = getattr(orch, "_meter_detector", None)
    snapshot_fn = getattr(reader, "detector_health_snapshot", None)
    if not callable(snapshot_fn):
        return None
    snapshot = snapshot_fn()
    if not isinstance(snapshot, dict):
        return None
    payload = {"event": "detector_health"}
    for key, value in snapshot.items():
        if value is None or isinstance(value, (bool, int, float)):
            payload[str(key)] = value
        else:
            # Never let a foreign object (or a long string) break the JSONL line.
            payload[str(key)] = str(value)[:48]
    return payload


def _read_processed_frame_snapshot(orch) -> dict:
    """Read detector completion metadata from one atomic orchestrator snapshot.

    New orchestrators publish a frozen object through a single reference after all
    fields are paired.  The legacy fallback preserves compatibility with older
    orchestrators, but current code must never independently join seq/frame/timestamp/
    health fields that can change on different threads.
    """
    snapshot = getattr(orch, "_processed_frame_snapshot", None)
    if snapshot is not None:
        raw_counts = getattr(snapshot, "reject_counts", ()) or ()
        try:
            reject_counts = dict(raw_counts)
        except Exception:
            reject_counts = {}
        frame_wh = getattr(snapshot, "frame_wh", (0, 0)) or (0, 0)
        raw_bbox = getattr(snapshot, "bbox", ()) or ()
        # [ORION_PROOF_DETECTOR_BOX 2026-09-19] the pre-display-hug DETECTOR rectangle of the
        # same frame; empty on an orchestrator that does not publish one (native falls back).
        raw_det_bbox = getattr(snapshot, "det_bbox", ()) or ()
        raw_bbox_wh = getattr(snapshot, "bbox_wh", ()) or ()
        return {
            "seq": int(getattr(snapshot, "seq", 0) or 0),
            "frame_number": int(getattr(snapshot, "frame_number", 0) or 0),
            "frame_ts": float(getattr(snapshot, "frame_ts", 0.0) or 0.0),
            "epoch_ms": float(getattr(snapshot, "epoch_ms", 0.0) or 0.0),
            "measurement_epoch_ms": float(
                getattr(snapshot, "measurement_epoch_ms", 0.0) or 0.0),
            "pts": int(getattr(snapshot, "pts", 0) or 0),
            "frame_wh": (int(frame_wh[0]), int(frame_wh[1])),
            "integrity_healthy": bool(getattr(snapshot, "integrity_healthy", False)),
            "reject_reason": str(getattr(snapshot, "reject_reason", "") or ""),
            "reject_counts": reject_counts,
            "backend_frozen": bool(getattr(snapshot, "backend_frozen", False)),
            "revision": int(getattr(snapshot, "revision", 0) or 0),
            "meter_present": bool(getattr(snapshot, "meter_present", False)),
            "raw_fed": bool(getattr(snapshot, "raw_fed", False)),
            "gameplay_structure_verified": bool(
                getattr(snapshot, "gameplay_structure_verified", False)),
            "gameplay_structure_epoch": _safe_uint64(
                getattr(snapshot, "gameplay_structure_epoch", 0)),
            "stage": str(getattr(snapshot, "stage", "") or ""),
            "bbox": tuple(int(v) for v in raw_bbox[:4]) if len(raw_bbox) >= 4 else (),
            "det_bbox": (tuple(int(v) for v in raw_det_bbox[:4])
                         if len(raw_det_bbox) >= 4 else ()),
            "bbox_wh": (tuple(int(v) for v in raw_bbox_wh[:2])
                        if len(raw_bbox_wh) >= 2 else ()),
            "green": _snapshot_pairs_dict(getattr(snapshot, "green", ())),
            "tracking": _snapshot_pairs_dict(getattr(snapshot, "tracking", ())),
            "fusion": _snapshot_pairs_dict(getattr(snapshot, "fusion", ())),
            "shot": _snapshot_pairs_dict(getattr(snapshot, "shot", ())),
            "tip_registration": _snapshot_pairs_dict(
                getattr(snapshot, "tip_registration", ())),
        }
    # Backward-compatible path for an orchestrator predating the atomic snapshot.
    frame_wh = getattr(orch, "_last_processed_frame_wh", (0, 0)) or (0, 0)
    meter_present = bool(getattr(orch, "_last_meter_present", False))
    raw_bbox = getattr(orch, "_last_meter_bbox", None)
    raw_det_bbox = getattr(orch, "_last_meter_det_bbox", None)
    raw_bbox_wh = getattr(orch, "_last_meter_bbox_wh", None)
    raw_green = getattr(orch, "_last_green_window", None)
    legacy_seq = int(getattr(orch, "_last_processed_seq", 0) or 0)
    raw_tip_registration = getattr(orch, "_last_tip_reg", None)
    legacy_tip_registration = {}
    if (isinstance(raw_tip_registration, dict)
            and bool(getattr(orch, "_capture_integrity_healthy", False))):
        try:
            if int(raw_tip_registration.get("seq", 0) or 0) == legacy_seq:
                legacy_tip_registration = dict(raw_tip_registration)
        except (TypeError, ValueError, OverflowError):
            legacy_tip_registration = {}
    remap = getattr(orch, "_remap_engine", None)
    shot = getattr(remap, "_shot", None) if remap is not None else None
    return {
        "seq": legacy_seq,
        "frame_number": int(getattr(orch, "_last_processed_frame_number", 0) or 0),
        "frame_ts": float(getattr(orch, "_last_processed_frame_ts", 0.0) or 0.0),
        "epoch_ms": float(getattr(orch, "_last_processed_epoch_ms", 0.0) or 0.0),
        # A legacy orchestrator did not publish a canonical measurement epoch.
        # Keep that absence explicit so native strict timing fails closed instead
        # of silently substituting the raw frame-identity clock.
        "measurement_epoch_ms": float(getattr(
            orch, "_last_processed_measurement_epoch_ms", 0.0) or 0.0),
        "pts": int(getattr(orch, "_last_processed_pts", 0) or 0),
        "frame_wh": (int(frame_wh[0]), int(frame_wh[1])),
        "integrity_healthy": bool(getattr(orch, "_capture_integrity_healthy", False)),
        "reject_reason": str(getattr(orch, "_last_frame_reject_reason", "") or ""),
        "reject_counts": dict(getattr(orch, "_frame_integrity_counts", {}) or {}),
        "backend_frozen": bool(getattr(orch, "_last_feed_frozen", False)),
        "revision": int(getattr(orch, "_telemetry_revision", 0) or 0),
        "meter_present": meter_present,
        "raw_fed": bool(meter_present and getattr(orch, "_last_raw_fed", False)),
        "gameplay_structure_verified": bool(
            meter_present
            and getattr(orch, "_last_raw_fed", False)
            and getattr(orch, "_last_gameplay_structure_verified", False)),
        "gameplay_structure_epoch": _safe_uint64(
            getattr(orch, "_last_gameplay_structure_epoch", 0)),
        "stage": str(getattr(orch, "_last_meter_stage", "") or ""),
        "bbox": (tuple(int(v) for v in raw_bbox[:4])
                 if raw_bbox is not None and len(raw_bbox) >= 4 else ()),
        "det_bbox": (tuple(int(v) for v in raw_det_bbox[:4])
                     if raw_det_bbox is not None and len(raw_det_bbox) >= 4 else ()),
        "bbox_wh": (tuple(int(v) for v in raw_bbox_wh[:2])
                    if raw_bbox_wh is not None and len(raw_bbox_wh) >= 2 else ()),
        "green": dict(raw_green) if isinstance(raw_green, dict) else {},
        "tracking": _dataclass_payload(getattr(orch, "_last_meter_track", None)) or {},
        "fusion": _dataclass_payload(getattr(orch, "_last_release_fusion", None)) or {},
        "shot": ({
            "fill_pct": (float(getattr(shot, "fill_pct", 0.0) or 0.0)
                         if meter_present else 0.0),
            "confidence": (float(getattr(shot, "confidence", 0.0) or 0.0)
                           if meter_present else 0.0),
            "rtt_offset_ms": float(getattr(shot, "rtt_offset_ms", 0.0) or 0.0),
        } if shot is not None else {}),
        "tip_registration": legacy_tip_registration,
    }


def _raw_transport_age_ms(orch, now: float = None) -> float:
    """Age of the last successfully delivered raw capture frame.

    This clock deliberately ignores whether those pixels were detector-eligible. A dark loading
    screen must disarm automation, but it must not masquerade as a dead capture transport and
    trigger a process teardown. Pure when ``now`` is supplied so the contract is testable.
    """
    try:
        captured_at = float(getattr(orch, "_last_capture_ts", 0.0) or 0.0)
        if captured_at <= 0.0:
            return 0.0
        current = time.perf_counter() if now is None else float(now)
        return max(0.0, (current - captured_at) * 1000.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _capture_publication_age_ms(orch) -> float:
    """Bounded FrameData capture-to-ring delay, never a timing input."""
    try:
        getter = getattr(orch, "capture_publication_age_ms", None)
        value = getter() if callable(getter) else getattr(
            orch, "_last_capture_publication_age_ms", 0.0)
        parsed = float(value or 0.0)
        return parsed if 0.0 <= parsed <= 60_000.0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _preview_wait_for_detector(orch, capture_seq: int, timeout_s: float) -> bool:
    """Bounded preview barrier: detection completion always gets first use of a frame.

    Returns False when detector work is still backlogged after the bound; callers drop
    that preview frame (latest-wins) instead of encoding it and stealing CPU/GIL time.
    """
    capture_seq = int(capture_seq or 0)
    if capture_seq <= 0 or getattr(orch, "_meter_detector", None) is None:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while int(getattr(orch, "_last_processed_seq", 0) or 0) < capture_seq:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(0.001, remaining))
    return True


def _classify_promotion_failure(orch) -> tuple:
    """Return the fail-closed detail for a rejected console-session promotion.

    A live child, local input pipe, or stream window is not a PS5-route proof. The
    client manager returns True only after a fresh launch-scoped Chiaki streaminfo
    marker, so every False is a hard failure.
    """
    mgr = getattr(orch, "_client_manager", None)
    status = getattr(mgr, "status", None) if mgr is not None else None
    message = ""
    try:
        message = str(getattr(status, "message", "") or getattr(orch, "_last_error_msg", "") or "")
    except Exception:
        message = ""
    if mgr is None:
        return True, (message or "Remote Play client manager unavailable")
    return True, (message or "Chiaki console input session did not become ready")


def _handle_start_stream(orch, msg: dict) -> bool:
    """WARM-PREVIEW -> STREAM promotion driver for the `start_stream` stdin command.

    The native side reused this already-running preview sidecar (capture card + detector live)
    instead of killing+rebuilding it, and now wants Chiaki + the input hook brought up IN PLACE.
    Delegate to the orchestrator's ``promote_to_stream`` — the SAME Chiaki/input bring-up the cold
    connect uses — invoked WITHOUT restarting the process and WITHOUT disturbing the live card feed +
    detector (both keep running on their own threads; preview frames never stop, so the panel does
    not blink). Idempotent: the orchestrator skips a duplicate launch when Chiaki is already running.

    FIX 1 (silent dead bot): this used to emit {"event":"started"} *regardless* of the result, so a
    promotion that could not launch Chiaki (device busy, pairing expired, exe missing) still flipped
    the launcher to "Autogreen running - meter detection active". The capture panel stayed live
    (it's the capture card, unaffected by chiaki) so the UI looked perfect while NO INPUT could ever
    reach the console. Now the real result is honoured: success -> {"event":"started"}, genuine
    failure -> {"event":"error"} which drives the native straight to the Error state.

    The {"event":"stream_promote","state":"begin"} ack emitted first tells the native this sidecar
    speaks the result-reporting protocol and should wait for the explicit readiness verdict. An
    older sidecar never sends it and is rejected after the native's short protocol grace.

    Returns True when the promotion succeeded (`started` emitted). Kept module-level so the
    promotion path is unit-testable without the capture stack."""
    ip = str(msg.get("console_ip", "")).strip()
    console_identity = str(msg.get("console_identity", "")).strip().lower()
    # Ack FIRST (before the blocking bring-up) so the native knows a verdict is coming even while
    # promote_to_stream is still inside its client-launch wait -- but OFF-THREAD (2026-08-29):
    # _emit blocks on _emit_lock, which the preview/telemetry writers can hold for >1s (p50 1538ms
    # measured on the packaged build). Emitting inline delayed the Chiaki spawn by that whole
    # wait on EVERY Connect (the warm-path "3.4s to input ready" was mostly this). The ack
    # threads are joined before any verdict emit so `begin`/`waking` always precede
    # started/error on the wire; the native treats both as progress notes, never as verdicts.
    _ack_threads = []
    def _emit_async(payload) -> None:
        t = threading.Thread(target=_emit, args=(payload,), name="promote-ack", daemon=True)
        _ack_threads.append(t)
        t.start()
    def _ack_done() -> None:
        for t in list(_ack_threads):
            try:
                t.join(timeout=5.0)
            except Exception:
                pass
    _emit_async({"event": "stream_promote", "state": "begin", "console_ip": ip})
    # Wake-aware deadline: when the client manager finds the console in rest mode and sends a
    # wakeup, it reports the port-wait budget here; the native extends its promote deadline
    # (sized for an AWAKE console) so a slow boot no longer ends in a forced "Connect again".
    def _on_console_waking(budget_s: float) -> None:
        try:
            _emit_async({"event": "stream_promote", "state": "waking", "console_ip": ip,
                         "budget_ms": int(max(0.0, float(budget_s)) * 1000.0)})
        except Exception:
            pass
    try:
        setattr(orch, "console_waking_callback", _on_console_waking)
    except Exception:
        pass
    # [ORION_CONNECT_LATENCY 2026-08-29] Stamp the whole promotion and surface the
    # client manager's per-stage breakdown in the native log, so the next latency
    # question is answered by reading one line instead of re-deriving it from
    # timer coincidences. _log goes out on the sidecar's own channel (not the
    # WARNING-filtered python-logging relay), so the line always lands.
    _promote_started = time.perf_counter()
    def _log_promote_timing() -> None:
        try:
            total_ms = (time.perf_counter() - _promote_started) * 1000.0
            summary = str(getattr(orch, "last_promotion_stage_summary", "") or "")
            _log(f"start_stream timing: total={total_ms:.0f}ms"
                 + (f" | {summary}" if summary else ""))
        except Exception:
            pass
    fn = getattr(orch, "promote_to_stream", None)
    if not callable(fn):
        _log("start_stream: orchestrator lacks promote_to_stream (old build) - "
             "no Chiaki/input link, the bot cannot reach the console", "error")
        _ack_done()
        _emit({"event": "error",
               "msg": "Stream start failed: this build cannot promote the live preview to a "
                      "stream (orchestrator has no promote_to_stream). Reconnect to retry.",
               "phase": "promote_to_stream",
               "input_ready": False})
        return False
    try:
        ok = bool(fn(ip or None, console_identity or None))
    except TypeError:
        # Old orchestrators cannot safely re-key a warm cache. Compatibility is allowed only when
        # no identity was supplied (old native); a current native identity must fail closed.
        ok = bool(fn(ip or None)) if not console_identity else False
    if ok:
        ready_fn = getattr(orch, "input_link_ready", None)
        input_ready = bool(callable(ready_fn) and ready_fn())
        if not input_ready:
            _log("start_stream: promotion returned success without explicit current-session "
                 "readiness proof", "error")
            _log_promote_timing()
            _ack_done()
            _emit({"event": "error",
                   "msg": "Stream start failed: console input session readiness was not proven.",
                   "phase": "promote_to_stream",
                   "input_ready": False})
            return False
        _log(f"start_stream: Chiaki console input session ready (console_ip={ip or '-'})")
        _log_promote_timing()
        _ack_done()
        _emit({"event": "started", "msg": "stream promoted from warm preview",
               "input_ready": True})
        return True

    _hard, detail = _classify_promotion_failure(orch)
    _log(f"start_stream: Chiaki/input bring-up FAILED - no input will reach the console "
         f"({detail}) (console_ip={ip or '-'})", "error")
    _log_promote_timing()
    _ack_done()
    _emit({"event": "error",
           "msg": f"Stream start failed: {detail}",
           "phase": "promote_to_stream",
           "input_ready": False})
    return False


def _handle_recover_input(orch) -> bool:
    """Recover Chiaki/input without restarting live capture or detection."""
    _emit({"event": "input_recovery", "state": "begin"})
    fn = getattr(orch, "recover_input_link", None)
    if not callable(fn):
        _log("recover_input: orchestrator lacks input-only recovery", "error")
        _emit({"event": "input_recovery", "state": "error",
               "msg": "Input recovery is unavailable in this sidecar build."})
        return False
    try:
        ok = bool(fn())
    except Exception as exc:
        _LOG.exception("recover_input handler raised")
        _emit({"event": "input_recovery", "state": "error", "msg": str(exc)})
        return False
    ready_fn = getattr(orch, "input_link_ready", None)
    input_ready = bool(ok and callable(ready_fn) and ready_fn())
    if input_ready:
        _log("recover_input: fresh Chiaki console session ready; capture/detector preserved")
        _emit({"event": "input_recovery", "state": "ready", "input_ready": True})
        return True
    _log("recover_input: Chiaki input child failed current-session readiness", "error")
    _emit({"event": "input_recovery", "state": "error",
           "msg": "Chiaki input link did not establish a fresh console session.",
           "input_ready": False})
    return False


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def _safe_uint64(value) -> int:
    """Return a positive uint64 epoch or the invalid zero sentinel."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        if value[0] == "0":
            return 0
        try:
            parsed = int(value, 10)
        except (TypeError, ValueError, OverflowError):
            return 0
        if str(parsed) != value:
            return 0
    else:
        return 0
    return parsed if 0 < parsed <= 0xFFFFFFFFFFFFFFFF else 0


# The three YOLO weight files pose_timing loads. The packager's models whitelist is intentionally
# EMPTY, so on a shipped install none of these exist.
_NO_METER_MODELS = (
    "models/orion_pose2k_n_v2.pt",
    "models/orion_bar_park.pt",
    "models/orion_player_detect_v9.pt",
)


def _probe_no_meter_support(root: Path) -> tuple:
    """FIX 2: is "No Meter" (pose/skele) mode actually USABLE on this install?

    Enabling it makes the orchestrator import ``pose_timing`` -> ultralytics/torch and load three
    ``models/*.pt`` weight files. The shipped packager excludes torch and ships no models, and the
    orchestrator's init failure is swallowed by a bare ``except`` — so the customer just gets a bot
    that silently does nothing. Probe up front and report the verdict so the launcher can grey the
    mode out instead of offering a broken switch.

    Deliberately uses ``importlib.util.find_spec`` (module RESOLUTION only) — never an import — so
    this costs microseconds and can never drag a multi-hundred-MB torch import into the sidecar.

    Returns ``(available, reason)``; reason is "" when available."""
    missing = []
    try:
        import importlib.util as _ilu
        for mod in ("pose_timing", "ultralytics", "torch"):
            try:
                if _ilu.find_spec(mod) is None:
                    missing.append(mod)
            except Exception:
                # A parent package that itself fails to resolve counts as missing.
                missing.append(mod)
    except Exception as exc:
        return False, f"dependency probe failed: {exc}"

    # Model paths are relative in the orchestrator, so honour both the NexusVision root and the
    # process CWD (the packaged launcher spawns the sidecar from the app dir).
    roots = []
    for cand in (root, Path.cwd()):
        try:
            rp = Path(cand).resolve()
            if rp not in roots:
                roots.append(rp)
        except Exception:
            pass
    missing_models = []
    for rel in _NO_METER_MODELS:
        found = False
        for base in roots:
            try:
                if (base / rel).is_file():
                    found = True
                    break
            except Exception:
                pass
        if not found:
            missing_models.append(rel.rsplit("/", 1)[-1])

    if not missing and not missing_models:
        return True, ""
    parts = []
    if missing:
        parts.append("missing dependencies: " + ", ".join(missing))
    if missing_models:
        parts.append("missing model files: " + ", ".join(missing_models))
    return False, "; ".join(parts)


def _handle_probe_marker(orch, msg: dict) -> None:
    """[ORION_PROBE] Warmup pump-fake probe PRESS marker (wall-clock epoch ms + the engine's
    configured spawn offset). The estimator closes it on the meter-appear that follows via
    template back-extrapolation. Module-level (like _handle_release_marker) for unit tests."""
    try:
        fn = getattr(orch, "mark_probe", None)
        if callable(fn):
            _ts = msg.get("wall_ms", None)
            _ts = _safe_float(_ts, None) if _ts is not None else None
            _sq = msg.get("seq", None)
            _sq = _safe_int(_sq, None) if _sq is not None else None
            _sp = _safe_float(msg.get("spawn_offset_ms", 0.0), 0.0)
            fn(_ts, _sq, _sp)
    except Exception as exc:
        _log(f"probe_marker failed: {exc}", "warn")


def _handle_release_marker(orch, msg: dict) -> None:
    """Native engine owns the controller in the live path, so it stamps the release COMMAND
    here (wall-clock ms) to feed the frozen-meter latency oracle. Optional `seq` pairs the
    release to a shot. Optional `calibration` identifies a controlled early-release marker;
    it defaults false for older native clients. Best-effort; missing ts -> orchestrator uses
    now. Module-level (like _handle_start_stream) so the native->oracle relay is unit-testable."""
    try:
        fn = getattr(orch, "mark_release", None)
        if callable(fn):
            _ts = msg.get("wall_ms", None)
            _ts = _safe_float(_ts, None) if _ts is not None else None
            _sq = msg.get("seq", None)
            _sq = _safe_int(_sq, None) if _sq is not None else None
            _cal = _safe_bool(msg.get("calibration", False), False)
            _target = msg.get("validation_target_pct", None)
            _target = _safe_float(_target, None) if _target is not None else None
            _tolerance = msg.get("validation_tolerance_pct", None)
            _tolerance = (_safe_float(_tolerance, None)
                          if _tolerance is not None else None)
            identity_supplied = ("physical_epoch" in msg or "shot_attempt" in msg)
            physical_epoch = _safe_uint64(msg.get("physical_epoch"))
            shot_attempt = _safe_uint64(msg.get("shot_attempt"))
            if identity_supplied and (physical_epoch <= 0 or shot_attempt <= 0):
                _log("release_marker rejected: incomplete/malformed ownership identity", "warn")
                return
            if _target is None and _tolerance is None:
                fn(_ts, _sq, calibration=_cal,
                   physical_epoch=physical_epoch, shot_attempt=shot_attempt)
            else:
                fn(_ts, _sq, calibration=_cal,
                   validation_target_pct=_target,
                   validation_tolerance_pct=_tolerance,
                   physical_epoch=physical_epoch, shot_attempt=shot_attempt)
    except Exception as exc:
        _log(f"release_marker failed: {exc}", "warn")


def _handle_latency_route_attestation(orch, msg: dict) -> bool:
    """Apply a neutral native delivery-route proof without creating shot evidence."""
    route = str(msg.get("delivery_route", "") or "").strip().lower()
    if route not in ("pipe", "vigem_ds4", "vigem_xusb"):
        _log("latency route attestation rejected: invalid delivery route", "warn")
        return False
    encoded_generation = msg.get("attestation_generation")
    generation = _safe_uint64(encoded_generation)
    if (generation <= 0 or not isinstance(encoded_generation, str)
            or str(generation) != encoded_generation):
        _log("latency route attestation rejected: invalid generation", "warn")
        return False
    fn = getattr(orch, "attest_controller_latency_route", None)
    if not callable(fn):
        return False
    return bool(fn(route, generation))


def _latency_route_attestation_ack_payload(orch, msg: dict) -> dict:
    """Build the direct receipt for one neutral controller-route proof.

    The receipt deliberately contains no timing sample, shot identity, or raw scope.  Native gets
    only the exact route/generation it sent plus a SHA-256 digest proving the sidecar committed that
    token against one non-empty estimator scope.  Telemetry remains the separate authority gate for
    a usable latency value; this ACK only closes the command-delivery handshake without waiting for
    an unrelated detector frame.
    """
    route = str(msg.get("delivery_route", "") or "").strip().lower()
    encoded_generation = msg.get("attestation_generation")
    generation = _safe_uint64(encoded_generation)
    canonical_generation = (str(generation)
                            if (generation > 0 and isinstance(encoded_generation, str)
                                and str(generation) == encoded_generation)
                            else "")
    accepted = _handle_latency_route_attestation(orch, msg)
    reason = ""
    scope = ""
    scope_epoch = 0
    if accepted:
        receipt_snapshot_fn = getattr(
            orch, "controller_latency_route_attestation_receipt_snapshot", None)
        if callable(receipt_snapshot_fn):
            try:
                receipt = receipt_snapshot_fn(route, generation)
                if isinstance(receipt, (tuple, list)) and len(receipt) == 2:
                    scope = str(receipt[0] or "")
                    scope_epoch = _safe_uint64(receipt[1])
            except Exception as exc:
                _LOG.warning("latency route receipt snapshot failed: %r", exc)
        else:
            # Compatibility path for an intermediate orchestrator: require both
            # individually locked accessors, then let telemetry's double-read
            # epoch fence reject any transition between them.
            receipt_fn = getattr(
                orch, "controller_latency_route_attestation_receipt", None)
            epoch_fn = getattr(orch, "latency_estimator_scope_epoch", None)
            if callable(receipt_fn) and callable(epoch_fn):
                try:
                    scope = str(receipt_fn(route, generation) or "")
                    scope_epoch = _safe_uint64(epoch_fn())
                except Exception as exc:
                    _LOG.warning("latency route receipt fallback failed: %r", exc)
        if not scope or scope_epoch <= 0:
            accepted = False
            reason = "scope_commit_unverified"
    elif route not in ("pipe", "vigem_ds4", "vigem_xusb"):
        reason = "invalid_delivery_route"
    elif not canonical_generation:
        reason = "invalid_generation"
    else:
        reason = "route_scope_rejected"

    return {
        "event": "latency_route_attestation_ack",
        "accepted": bool(accepted),
        "delivery_route": route if route in ("pipe", "vigem_ds4", "vigem_xusb") else "",
        "attestation_generation": canonical_generation,
        "scope_epoch": str(scope_epoch) if accepted else "",
        "scope_digest": (hashlib.sha256(scope.encode("utf-8")).hexdigest()
                         if accepted else ""),
        "reason": reason,
    }


def _safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return default


def _safe_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off", ""):
            return False
        return default
    return default


def _preview_uses_source_cadence(frame_source, environ, shm_active=True) -> bool:
    """Return whether preview presentation must follow producer frame events.

    Capture-card and decoded-frame-pipe sources already arrive on their own
    presentation cadence.  Putting either behind a second independent 60 Hz
    timer aliases the clocks and periodically turns two 16.7 ms intervals into
    one visible 33 ms gap.  JPEG/window fallback still needs its explicit grid.

    Kept as a pure helper so production/source selection cannot silently drift
    away from the smoothness contract.
    """
    if not shm_active:
        return False
    source = str(frame_source or "").strip().lower()
    capture_card = source in ("capture_card", "capturecard", "card") \
        or _safe_bool(environ.get("ORION_CAPTURE_CARD"), False)
    decoded_pipe = source in ("decoder", "pipe", "frame_pipe") \
        or _safe_bool(environ.get("ORION_REQUIRE_FRAME_PIPE"), False) \
        or _safe_bool(environ.get("ORION_FRAME_PIPE"), False) \
        or bool(str(environ.get("CHIAKI_ORION_FRAME_PIPE", "") or "").strip())
    return capture_card or decoded_pipe


def _preview_needs_detector_barrier(source_cadence: bool, shm_active: bool) -> bool:
    """Whether display work must wait for detector completion.

    A source-paced SHM publish is only a bounded copy from an immutable Orion-owned
    array on a below-normal-priority thread.  It neither encodes nor mutates pixels,
    so waiting for detection there only turns a legitimate >12 ms detector pass into
    a visible dropped frame.  JPEG fallback remains CPU/GIL-heavy and keeps the
    detector-first barrier.
    """
    return not (bool(source_cadence) and bool(shm_active))


def _preview_frame_ownership_reason(frame) -> str:
    """Return why a preview frame is unsafe to share, or ``""`` when safe.

    The source-paced SHM path intentionally copies pixels concurrently with detector
    work.  That is safe only for the orchestrator's owned, C-contiguous, read-only
    uint8 BGR snapshot.  Keep this check at the sidecar boundary as a fail-closed
    guard: a future backend must not accidentally publish a mutable driver/decoder
    view merely because it opted into source-paced preview.
    """
    try:
        if frame is None or int(getattr(frame, "size", 0) or 0) <= 0:
            return "empty"
        if int(getattr(frame, "ndim", 0) or 0) != 3:
            return "dimensions"
        shape = tuple(int(v) for v in frame.shape)
        if len(shape) != 3 or shape[2] != 3:
            return "channels"
        dtype = getattr(frame, "dtype", None)
        if str(getattr(dtype, "name", dtype)) != "uint8":
            return "dtype"
        flags = frame.flags
        if not bool(flags["C_CONTIGUOUS"]):
            return "not_contiguous"
        if not bool(flags["OWNDATA"]):
            return "not_owned"
        if bool(flags["WRITEABLE"]):
            return "writeable"
        return ""
    except Exception:
        return "invalid"


def _dataclass_payload(value):
    try:
        if value is not None and is_dataclass(value):
            return asdict(value)
    except Exception as exc:
        # Feeds the telemetry `tracking` / `fusion` blocks: a failure here silently strips
        # diagnostics the native engine reports on. Counted + logged (60Hz path -> _note_drop).
        _note_drop("telemetry_dataclass", exc)
    return None


def _telemetry_wait(frame_ready, stop_evt, timeout: float = 1.0 / 60.0) -> bool:
    """Pace the telemetry / meter-feed loop.

    Event-driven: block until the orchestrator signals a fresh detection via
    ``frame_ready`` (set the instant new meter state is published in
    ``_processing_loop``), so the native engine's ONLY meter feed is emitted the
    INSTANT a detection is ready -- removing up to ~16.7 ms of feed staleness (the
    wait for the next fixed 60 Hz tick) and the 60 fps emit ceiling. The ``timeout``
    fallback (default 1/60 s) still fires when no new frame arrived, so RTT / tick /
    frame-age telemetry keeps flowing at >=60 Hz while the meter is idle.

    Backward/inert-safe: if ``frame_ready`` is None (an orchestrator build that
    predates the event) this degrades to the original fixed ``stop_evt.wait(timeout)``
    pacing, so the loop still ticks at >=60 Hz.

    Returns True if woken by a fresh frame, False on the timeout fallback. Extracted
    to module scope so the event-driven pacing is unit-testable without spinning up
    the whole orchestrator. Does NOT touch the payload -> the JSON schema is unchanged.
    """
    if frame_ready is not None:
        woke = frame_ready.wait(timeout)
        # Edge-consume: coalesce any frames signaled while we were building/emitting the
        # last payload so the next wait blocks for the NEXT frame. Telemetry reads orch
        # state latest-wins on every emit, so a coalesced signal never drops a meter
        # sample -- at worst it costs one extra (identical-schema) emit.
        frame_ready.clear()
        return woke
    stop_evt.wait(timeout)
    return False


def _install_crash_observability(root: Path) -> None:
    """The 07-03 sessions all ended with the sidecar exiting SILENTLY (~1-2 min in): the native's
    stderr relay only forwards ERROR/CRITICAL/WARNING lines, so a bare Python traceback (and the
    exit code) vanished. Make every death path observable: hard faults via faulthandler, unhandled
    exceptions via sys/threading excepthooks (file + per-line ERROR stderr so the native relays it),
    and an atexit marker recording HOW the process exited (clean paths set _exit_reason)."""
    log = logging.getLogger("autogreen")
    logs_dir = root / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        # Bailing here disables the ENTIRE crash-forensics install (faulthandler, excepthooks,
        # the exit marker) — the exact "sidecar died silently" state this function exists to
        # prevent. Say so loudly instead of returning quietly.
        log.error("crash observability NOT installed: cannot create %s: %r", logs_dir, exc)
        return
    try:
        import faulthandler
        # Kept open for the process lifetime on purpose — faulthandler writes to the fd on a crash.
        fault_f = open(logs_dir / "sidecar_fault.log", "a", buffering=1)
        fault_f.write(f"\n=== sidecar start {time.strftime('%Y-%m-%d %H:%M:%S')} pid={_pid()} ===\n")
        faulthandler.enable(file=fault_f, all_threads=True)
    except Exception as exc:
        log.warning("faulthandler NOT enabled (%r): a hard fault (segv/abort) will leave no trace", exc)

    def _record(kind: str, text: str) -> None:
        try:
            with open(logs_dir / "sidecar_crash.log", "a", encoding="utf-8") as f:
                f.write(f"\n=== {kind} {time.strftime('%Y-%m-%d %H:%M:%S')} pid={_pid()} ===\n{text}\n")
        except Exception as exc:
            # The stderr/_emit relays below still run, so this is non-fatal — but a missing
            # crash log must not itself be invisible when someone goes looking for it.
            log.error("crash log NOT written to %s: %r", logs_dir / "sidecar_crash.log", exc)
        # Per-line ERROR so the native stderr relay (ERROR/CRITICAL/WARNING filter) surfaces every line.
        for ln in text.splitlines():
            if ln.strip():
                log.error("CRASH %s", ln)
        _emit({"event": "error", "msg": f"sidecar {kind}: {text.splitlines()[-1] if text.splitlines() else kind}"})

    import traceback as _tb

    def _sys_hook(exc_type, exc, tb):
        _record("unhandled-exception", "".join(_tb.format_exception(exc_type, exc, tb)))

    def _thread_hook(hook_args):
        _record(f"thread-exception thread={getattr(hook_args.thread, 'name', '?')}",
                "".join(_tb.format_exception(hook_args.exc_type, hook_args.exc_value, hook_args.exc_traceback)))

    sys.excepthook = _sys_hook
    try:
        threading.excepthook = _thread_hook
    except Exception as exc:
        # Without this hook a worker thread (capture / detect / preview / telemetry) can die
        # with a traceback that goes nowhere while the process stays "alive" — the classic
        # silent half-dead sidecar.
        log.error("threading.excepthook NOT installed (%r): a worker-thread crash may be silent", exc)

    import atexit

    def _atexit():
        try:
            with open(logs_dir / "sidecar_crash.log", "a", encoding="utf-8") as f:
                f.write(f"=== exit {time.strftime('%Y-%m-%d %H:%M:%S')} pid={_pid()} "
                        f"reason={_EXIT_REASON.get('reason', 'unknown-interpreter-exit')} ===\n")
        except Exception:
            # Deliberately left bare: this runs during interpreter teardown where logging
            # handlers / sys.stderr may already be closed, so logging here can itself raise
            # and mask the real exit. The native side records the exit code + stderr tail.
            pass

    atexit.register(_atexit)


def _pid() -> int:
    try:
        return os.getpid()
    except Exception:
        return -1


# Set by the clean-exit paths so the atexit marker can tell a commanded shutdown from a mystery
# death: "shutdown-cmd" | "signal" | "stdin-eof" | left unset = the interpreter exited some other
# way (unhandled main-thread exception, os._exit from a C extension, etc.).
_EXIT_REASON: dict = {}


def main() -> int:
    # [ORION_STDIO_LINEBUF 2026-08-07] Force line-buffered stdout BEFORE any _emit() runs.
    # Nuitka's SUBSYSTEM:WINDOWS bootstrap (this is a GUI subsystem child so no console
    # window flashes on customer launch) does not reliably honour PYTHONUNBUFFERED=1; the
    # Python stdio for a no-console child can end up block-buffered, causing the parent
    # to see multi-second bursts of telemetry all arriving together instead of streaming.
    # Native side then measures huge "wire latency" that is actually flush buffering.
    # reconfigure() is a no-op if stdout was already line-buffered, so this is safe on
    # every host regardless of what Nuitka's bootstrap decided.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # very old Python or reconfigure disallowed for this stream; not fatal
    parser = argparse.ArgumentParser(description="Orion autogreen sidecar")
    parser.add_argument("--root", help="NexusVision root")
    parser.add_argument("--config-json", help="JSON config blob")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument(
        "--build-identity",
        action="store_true",
        help="print the source digest embedded in this executable and exit",
    )
    identity.add_argument(
        "--build-identity-file",
        help="write the embedded source digest to this file and exit",
    )
    identity.add_argument(
        "--detector-smoke",
        action="store_true",
        help="benchmark the bundled production meter detector/provider and exit",
    )
    identity.add_argument(
        "--detector-smoke-file",
        help="write bundled detector/provider benchmark JSON to this file and exit",
    )
    args = parser.parse_args()

    if args.build_identity or args.build_identity_file:
        digest = ORION_SIDECAR_SOURCE_DIGEST.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            print("sidecar build identity is unavailable", file=sys.stderr, flush=True)
            return 4
        if args.build_identity_file:
            Path(args.build_identity_file).write_text(digest + "\n", encoding="ascii", newline="\n")
        else:
            print(digest, flush=True)
        return 0

    if args.detector_smoke or args.detector_smoke_file:
        root = (
            Path(args.root).resolve()
            if args.root
            else Path(sys.executable).resolve().parent
        )
        _bootstrap(root)
        configured_model = os.environ.get("ORION_METER_MODEL", "").strip()
        model = (
            Path(configured_model).resolve()
            if configured_model
            else root / "models" / "orion_meter_detector.onnx"
        )
        output_path = (
            Path(args.detector_smoke_file).resolve()
            if args.detector_smoke_file
            else None
        )
        return _detector_provider_smoke(model, output_path=output_path)

    if not args.root or args.config_json is None:
        parser.error("--root and --config-json are required for sidecar runtime mode")

    root = Path(args.root).resolve()
    _bootstrap(root)

    # Detection-sidecar CPU priority. Profiling showed the park detector is algorithmically fast (~6ms), but
    # DURING A SHOT the native TIME_CRITICAL precise-fire thread + the HIGHEST input-hook thread + chiaki's
    # takion preempt this Python process, ballooning detect ~8ms->40ms and dropping cv_fps ~60->24 exactly
    # when the rise needs the most samples. A modest ABOVE_NORMAL bump keeps detection scheduled without
    # starving chiaki's decode or the release (both stay above it). Flag ORION_SIDECAR_PRIORITY:
    # above(default)/high/normal/off. Windows-only, best-effort — never fatal.
    try:
        import os as _os, ctypes as _ct
        if _os.name == "nt":
            _prio = _os.environ.get("ORION_SIDECAR_PRIORITY", "above").strip().lower()
            _pcls = {"above": 0x00008000, "high": 0x00000080, "normal": 0x00000020}.get(_prio)
            if _pcls is not None:
                _ct.windll.kernel32.SetPriorityClass(_ct.windll.kernel32.GetCurrentProcess(), _pcls)
                logging.getLogger("autogreen").info("sidecar priority = %s", _prio)
    except Exception as exc:
        # Non-fatal (detection just runs at normal priority) but no longer invisible: a silent
        # failure here is the difference between ~60 and ~24 cv_fps during a shot.
        _LOG.debug("sidecar priority bump skipped: %r", exc)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _install_crash_observability(root)

    try:
        cfg = json.loads(args.config_json)
    except Exception as exc:
        _emit({"event": "error", "msg": f"bad config json: {exc}"})
        return 2

    try:
        from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    except Exception as exc:
        _emit({"event": "error", "msg": f"failed to import orchestrator: {exc}"})
        return 3
    # One stdout lock for the whole process: the orchestrator's own emitters must not
    # interleave with the sidecar's preview/telemetry lines (bughunt #4).
    _adopt_shared_stdout_lock()

    orch_config = OrchestratorConfig(
        console_ip=str(cfg.get("console_ip", "")),
        console_identity=str(cfg.get("console_identity", "")),
        platform=str(cfg.get("platform", "ps5")),
        client_mode=str(cfg.get("client_mode", "chiaki")),
        auto_launch_client=_safe_bool(cfg.get("auto_launch_client", True), True),
        close_client_on_disconnect=_safe_bool(cfg.get("close_client_on_disconnect", True), True),
        chiaki_path=str(cfg.get("chiaki_path", "")),
        chiaki_identity_size=(_safe_uint64(cfg.get("chiaki_identity_size")) or -1),
        chiaki_identity_sha256=str(cfg.get("chiaki_identity_sha256", "")),
        window_title=str(cfg.get("window_title", "Chiaki")),
        resolution=str(cfg.get("resolution", "1920x1080")),
        target_fps=_safe_int(cfg.get("target_fps"), 120),
        show_video=False,  # we don't render video locally in the sidecar
        virtual_controller=_safe_bool(cfg.get("virtual_controller", True), True),
        hidhide=_safe_bool(cfg.get("hidhide", True), True),
        goto_shot=False,
        # Default Red, matching AppConfig.h:66 and meter_bar_colors.FALLBACK. This read used to
        # default to "Purple", which was harmless only because the park pin overwrote it with
        # "Red" before the reader saw it. Now that the pin is correctly scoped away from the
        # shot-gated readers, a missing key here would hand them the one colour that has never
        # been the shipped default. The native app always writes this key
        # (AppConfig.cpp:224), so this is a belt-and-braces default, not a live path.
        meter_color=str(cfg.get("meter_color", "Red")),
        meter_style=str(cfg.get("meter_style", "Arrow2")),
        confidence_gate=_safe_float(cfg.get("confidence_gate"), 0.32),
        green_window_start_pct=_safe_float(cfg.get("green_window_start_pct"), 93.0),
        green_window_end_pct=_safe_float(cfg.get("green_window_end_pct"), 100.0),
        timing_delay_ms=_safe_float(cfg.get("timing_delay_ms"), 0.0),
        latency_compensation_ms=_safe_float(cfg.get("latency_compensation_ms"), 45.0),
        stable_frames_required=_safe_int(cfg.get("stable_frames_required"), 3),
        tempo_flick_hold_ms=_safe_float(cfg.get("tempo_flick_hold_ms"), 50.0),
        # [ORION_INPUT_MODE 2026-08-10] Was hard-pinned to "square_only" here and at the
        # update_remap handler below, so the value RemotePlaySession.cpp computes from
        # remotePlayInputSource (square / stick / both) was serialized, sent over stdin and
        # then thrown away -- the Remote Play input-source setting was dead config and
        # selecting stick or both did nothing. Honour it, keeping square_only as the default.
        input_mode=str(cfg.get("input_mode", "square_only") or "square_only"),
        no_meter_enabled=_safe_bool(cfg.get("no_meter_enabled", False), False),
        no_meter_release_point=str(cfg.get("no_meter_release_point", "push")),
        no_meter_base_offset_ms=_safe_float(cfg.get("no_meter_base_offset_ms"), 83.0),
        decode_latency_ms=_safe_float(cfg.get("decode_latency_ms"), 7.5),
        no_meter_confidence_gate=_safe_float(cfg.get("no_meter_confidence_gate"), 0.70),
        no_meter_push_release_window_ms=_safe_float(cfg.get("no_meter_push_release_window_ms"), 200.0),
        no_meter_handedness=str(cfg.get("no_meter_handedness", "Right") or "Right"),
        shot_trigger_mode=str(cfg.get("shot_trigger_mode", "button") or "button"),
        active_shot_type=str(cfg.get("active_shot_type", "Standstill") or "Standstill"),
        shot_type_offsets=cfg.get("shot_type_offsets") if isinstance(cfg.get("shot_type_offsets"), dict) else {},
        frame_source=str(cfg.get("frame_source", "auto") or "auto"),
        wait_timeout_s=_safe_float(cfg.get("wait_timeout_s"), 40.0),
    )

    # [ORION_DECODER_TIP_MARGINS 2026-09-14] Route-conditional locator margins, set BEFORE the
    # reader/locator is built (remote_play_orchestrator constructs SimpleMeterReader later).
    # Measured offline (tools/quality/reencode_gate_study.py, 283 true-meter + 1,213 adversarial
    # frames re-encoded through the Chiaki rungs): every shape-gate metric keeps 3-10x margin under
    # H.264, but 4:2:0 chroma subsampling washes the green tip's SATURATION, so first-sight locks
    # drop to 83-91 % at 720p. S>=60 (+ tip px >=2) recovers to 93-99 % with 0 false locks and is
    # exactly neutral on capture-card pixels -- which is why the capture-card route never sets it.
    # setdefault: an explicit env (sweeps) always wins.
    _src_for_margins = str(getattr(orch_config, "frame_source", "") or "").strip().lower()
    _cc_route = _src_for_margins in ("capture_card", "capturecard", "card")         or _safe_bool(os.environ.get("ORION_CAPTURE_CARD"), False)
    _decoder_route = (not _cc_route) and (
        _src_for_margins in ("decoder", "pipe", "frame_pipe")
        or _safe_bool(os.environ.get("ORION_REQUIRE_FRAME_PIPE"), False)
        or _safe_bool(os.environ.get("ORION_FRAME_PIPE"), False)
        or bool(str(os.environ.get("CHIAKI_ORION_FRAME_PIPE", "") or "").strip()))
    if _decoder_route:
        os.environ.setdefault("ORION_CV_GREEN_S_MIN", "60")
        os.environ.setdefault("ORION_CV_TIP_PX_MIN", "2")

    _emit({
        "event": "log",
        "level": "info",
        "msg": (
            "sidecar config parsed: "
            f"meter_color={orch_config.meter_color}, "
            f"virtual_controller={orch_config.virtual_controller}, "
            f"hidhide={orch_config.hidhide}, "
            f"target_fps={orch_config.target_fps}"
        ),
    })

    # FIX 2: publish what this install can actually DO before anything tries to use it. The
    # launcher consumes {"event":"capabilities"} to grey out / warn about "No Meter" mode instead
    # of letting the customer switch to a mode whose torch + weights are not packaged (whose init
    # failure the orchestrator swallows, leaving a bot that silently never fires).
    _no_meter_ok, _no_meter_reason = _probe_no_meter_support(root)
    _emit({
        "event": "capabilities",
        "no_meter_available": bool(_no_meter_ok),
        "no_meter_reason": _no_meter_reason,
    })
    if not _no_meter_ok:
        _LOG.warning("no-meter (pose/skele) mode UNAVAILABLE: %s", _no_meter_reason)
        if orch_config.no_meter_enabled:
            # Rare + directly user-relevant: the configured mode cannot run at all, so this one
            # belongs on the launcher log path, not just stderr.
            _log("No-Meter mode is enabled but NOT available on this install "
                 f"({_no_meter_reason}) - shot timing will not run in that mode.", "error")

    orch = RemotePlayOrchestrator(orch_config)
    # Fresh detector telemetry has priority over the much larger preview JSONL line.
    # The orchestrator sets this event after a detection/fail-closed frame decision;
    # telemetry clears it only after that payload has been written.  Preview checks it
    # before encoding/emitting so it cannot sit on the shared stdout lock first.
    _telemetry_priority_evt = threading.Event()
    orch._telemetry_priority_evt = _telemetry_priority_evt
    _telemetry_state = {"sent_revision": 0}

    # Tune the RTT engine for snappy, court-aware compensation:
    #   - Faster ping interval (300 ms) for responsive RTT updates
    #   - Slightly higher EMA alpha for quicker convergence
    #   - Conservative jitter margin so we don't overshoot perfect green
    try:
        rtt_engine = getattr(orch, "_rtt_engine", None)
        if rtt_engine is not None:
            rcfg = rtt_engine._config
            rcfg.ping_interval_ms = 300.0
            rcfg.ping_timeout_ms = 500.0
            rcfg.ema_alpha = 0.25
            rcfg.jitter_safety_margin_ms = 1.5
            rcfg.min_samples = 4
            rcfg.tick_phase_advance_ms = 2.5
            rcfg.decode_latency_comp_ms = _safe_float(cfg.get("decode_latency_ms"), 7.5)
    except Exception as exc:
        # Silently falling back to the engine's default RTT tuning changes live latency
        # compensation (i.e. release timing) — never let that happen unannounced.
        _LOG.warning("RTT engine tuning NOT applied (%r) - using default ping/EMA/jitter config", exc)

    # Provide a JPEG-encoded preview frame periodically for the launcher to
    # display. Live-capture stability fixes:
    #   - Use monotonic time so the pacing window is not affected by system
    #     clock adjustments (was the main cause of frame bunching/flicker).
    #   - Use a balanced JPEG quality with optimization disabled. Per-frame
    #     optimize=True was spiking CPU hard enough to starve Chiaki audio on
    #     some machines, which shows up as crackle/stutter.
    #   - Mild progressive=False so all decoders interpret the byte stream
    #     identically (avoids partial-decode flashes).
    #   - Pre-import cv2/base64 once instead of per-frame so the callback is
    #     a hot path with no module-lookup latency.
    # Default 60fps preview so the launcher display matches the 60fps stream (the game
    # is 60fps; a 30fps preview is the "stutter" the user sees vs their monitor). The
    # encode runs OFF the capture/detect thread (below), so 60fps preview doesn't starve
    # detection.
    # Live A/B tunables (env overrides config; defaults reproduce shipped behavior):
    #   ORION_PREVIEW_FPS          — preview cadence, clamp 5..60 (default 60 = matches the game).
    #   ORION_PREVIEW_GATE_FACTOR  — de-alias gate as a fraction of the interval, clamp 0.10..1.00
    #                                (default 0.85). Only consulted when pacing is OFF (legacy mode).
    #   ORION_PREVIEW_PACE         — default ON. When on, a frame that arrives EARLY vs the
    #                                1/preview_fps grid is HELD until the grid deadline and then
    #                                emitted, instead of being shed by the gate (see _preview_loop).
    #   ORION_PREVIEW_STATS        — default ON. Emits a compact `preview_stats` log line every 5s
    #                                (emit fps / coalesced / duplicate-frame% / inter-emit gap) so a
    #                                live test can tell judder (gap variance) from a frozen echo
    #                                (duplicate frame_number).
    #   ORION_PREVIEW_JPEG_Q       — [D2] JPEG quality, clamp 60..98 (default 80, was 92). This is the
    #                                lever for the ~200-600 KB/frame stdout line whose base64+write
    #                                holds the GIL and stalls the capture loop; halve it again if a
    #                                slow rig still shows uniqueFrameFps < 60 during gameplay.
    #   ORION_PREVIEW_COPY         — [D1] default OFF. 1 restores the old unconditional frame.copy()
    #                                in the capture callback (see the aliasing audit in _video_cb).
    # The preview ENCODE rate is ORION_PREVIEW_FPS above and is fully decoupled from detection:
    # detection consumes EVERY captured frame in the orchestrator's processing loop regardless, so
    # dropping this to 30 halves the preview's stdout bytes + GIL time without costing the bot a
    # single meter sample. Left at 60 by default because a 30 fps preview against a 60 Hz monitor is
    # the visible "stutter" the user reported previously; ORION_PREVIEW_FPS=30 is the escape hatch.
    preview_fps = max(5.0, min(60.0, _safe_float(
        os.environ.get("ORION_PREVIEW_FPS") or cfg.get("preview_fps"), 60.0)))
    # 1280 is full 720p and already exceeds the panel on ordinary 1080p desktops. It cuts
    # preview pixels by 36% versus 1600 and reduces GUI-pipe traffic. Explicit stream presets
    # still select their own width; ORION_PREVIEW_WIDTH is a diagnostic override.
    # Clamp 640..1920.
    preview_width = max(640, min(1920, _safe_int(
        os.environ.get("ORION_PREVIEW_WIDTH") or cfg.get("preview_width"), 1280)))

    try:
        import cv2 as _preview_cv2  # type: ignore
        import base64 as _preview_b64
    except Exception as _prev_imp_exc:  # pragma: no cover - missing optional deps
        _preview_cv2 = None
        _preview_b64 = None
        # This is a TOTAL preview blackout (the frame callback and the encoder thread both bail
        # immediately), i.e. the launcher shows "No stream" forever while the sidecar is healthy.
        # Rare + entirely user-relevant, so it earns a launcher-visible line, not just stderr.
        _LOG.error("preview encoder DISABLED: cv2/base64 import failed: %r", _prev_imp_exc)
        _log(f"Live preview unavailable: image encoder failed to load ({_prev_imp_exc}). "
             f"Detection still runs, but the capture panel will stay blank.", "error")

    # [D2] q80 (was q92). The JSONL frame line measured 200-595 KB during GAMEPLAY at q92, and the
    # base64+write of it holds the GIL for ~2.3 ms per frame — i.e. it stalls the 60 Hz capture/detect
    # thread in this same process for ~14% of a 16.67 ms frame budget, and every capture iteration that
    # overruns 16.67 ms permanently loses a card frame (the frame ring is capacity-2 latest-wins). q80
    # roughly halves both the bytes and the encode time and is visually indistinguishable at the panel's
    # ~1550 px width. Overridable by env ORION_PREVIEW_JPEG_Q, then config `preview_jpeg_quality`
    # (clamped 60..98) — set it back to 92 to reproduce the old output exactly.
    _jpeg_q = max(60, min(98, _safe_int(
        os.environ.get("ORION_PREVIEW_JPEG_Q") or cfg.get("preview_jpeg_quality"), 80)))
    _jpeg_params = (
        [int(_preview_cv2.IMWRITE_JPEG_QUALITY), _jpeg_q, int(_preview_cv2.IMWRITE_JPEG_OPTIMIZE), 0]
        if _preview_cv2 is not None else []
    )

    # Display-only SHM fast path. It is explicitly enabled by a native build
    # that understands the mapping protocol. Negotiated builds also provide a
    # per-session frame-ready event; older builds keep the backwards-compatible
    # `frame_shm` stdout notification instead of receiving a blank panel.
    _shm_writer = None
    _shm_ok_count = 0
    _shm_fail_count = 0
    _shm_fail_streak = 0
    _shm_disable_evt = threading.Event()
    _shm_failover = _PreviewShmFailover(
        os.environ.get("ORION_PREVIEW_SHM_READY_EVENT", ""))
    if (_preview_cv2 is not None and os.name == "nt"
            and _safe_bool(os.environ.get("ORION_PREVIEW_SHM"), False)):
        try:
            from shm_frame_bridge import ShmFrameWriter
            _shm_writer = ShmFrameWriter(max_width=1280, max_height=720)
            if _shm_writer.start():
                _notify_mode = (
                    "named event" if _shm_writer.event_notifications_enabled
                    else "stdout compatibility")
                _log(
                    "Preview transport: shared memory active "
                    f"({_notify_mode}; JPEG fallback armed)")
            else:
                _log(
                    f"Preview transport: SHM unavailable ({_shm_writer.last_error}); using JPEG",
                    "warn",
                )
                _shm_failover.request("shm_start_failed")
                _shm_failover.announce()
                _shm_writer = None
        except Exception as _shm_exc:
            _shm_writer = None
            _shm_failover.request("shm_start_exception")
            _shm_failover.announce()
            _log(f"Preview transport: SHM startup failed ({_shm_exc}); using JPEG", "warn")

    # Off-thread preview encoder. The orchestrator's frame callback is a capture/detect
    # HOT PATH — doing the resize+JPEG+base64 inline (~2-3ms) there both capped the
    # preview at the detect rate and stole time from detection (live cv_fps 44-57 < 60).
    # So the callback now only stashes the LATEST frame (a cheap COW-safe copy) and wakes
    # a dedicated daemon thread that paces the encode at preview_fps and emits. Intermediate
    # frames are dropped (a preview only ever needs the newest), so the encoder never
    # backs up. This is what lets the display run a smooth 60fps independent of detection.
    _preview = {
        "frame": None,
        "coalesced": 0,
        "detector_backpressure": 0,
        # Callback cadence is a release gate, so its clock must resolve a 60 Hz
        # frame interval on Windows.  Python 3.12 implements time.monotonic()
        # with GetTickCount64 (15.625 ms resolution), which quantizes healthy
        # callbacks into false 31/32 ms gaps.  Keep this metric on QPC nanoseconds,
        # matching the capture-card and SHM stage clocks.
        "callback_last_ns": 0,
        "callback_gap_count": 0,
        "callback_gap_sum_ns": 0,
        "callback_gap_max_ns": 0,
    }
    _preview_lock = threading.Lock()
    _preview_wake = threading.Event()
    _preview_stop = threading.Event()

    # Smoothness tunables (see the ORION_PREVIEW_* block above for semantics).
    _gate_factor = max(0.10, min(1.00, _safe_float(
        os.environ.get("ORION_PREVIEW_GATE_FACTOR") or cfg.get("preview_gate_factor"), 0.85)))
    _pace_early = _safe_bool(
        os.environ.get("ORION_PREVIEW_PACE", cfg.get("preview_pace")), True)
    # Capture-card and decoded-pipe delivery are already event-driven by their
    # producers.  With cheap SHM transport, applying a second independent 60 Hz
    # sleep grid aliases two nominally-equal clocks and periodically coalesces a
    # frame into a 32 ms display gap.  Follow source cadence on both production
    # paths; only window/JPEG transports retain the explicit pacing grid.
    _configured_frame_source = getattr(orch_config, "frame_source", "")
    _event_driven_shm_source = _preview_uses_source_cadence(
        _configured_frame_source, os.environ, shm_active=True)
    _preview_stats = _safe_bool(
        os.environ.get("ORION_PREVIEW_STATS", cfg.get("preview_stats")), True)
    # [D1] Kill-switch for the zero-copy stash below (ORION_PREVIEW_COPY=1 restores the old
    # unconditional frame.copy() on the capture callback). Default OFF — see the aliasing audit
    # in _video_cb.
    _preview_copy = _safe_bool(os.environ.get("ORION_PREVIEW_COPY"), False)

    def _video_cb(frame_data):
        if _preview_cv2 is None or frame_data is None:
            return
        try:
            if isinstance(frame_data, tuple) and len(frame_data) >= 4:
                frame, frame_number, capture_seq, capture_timestamp_ns = frame_data[:4]
            elif isinstance(frame_data, tuple) and len(frame_data) >= 3:
                frame, frame_number, capture_seq = frame_data[:3]
                capture_timestamp_ns = 0
            elif isinstance(frame_data, tuple):
                frame, frame_number = frame_data
                capture_seq = 0
                capture_timestamp_ns = 0
            else:
                frame, frame_number, capture_seq = frame_data, 0, 0
                capture_timestamp_ns = 0
            callback_at_ns = time.perf_counter_ns()
            # [D1] STASH A REFERENCE, NOT A COPY. This callback runs INSIDE the orchestrator's
            # capture loop, whose whole budget is 16.67 ms; every iteration that overruns it
            # permanently loses one card frame 1:1 (the frame ring is capacity-2 latest-wins),
            # which is what pulled the live capture-read rate from 60 (menus) down to 38
            # (gameplay). The 1080p BGR copy that used to sit here measured 1.58 ms = ~9.5% of
            # that entire budget, held the GIL throughout, and `preview_stats coalesced=9..23`
            # per 5 s window shows a slice of those copies were thrown away undisplayed.
            #
            # ALIASING AUDIT — why a bare reference is safe. The orchestrator invokes this
            # callback only after its detector boundary has produced a C-contiguous, OWNDATA,
            # read-only 1280x720 frame. Capture-card buffers are copied and post-copy-verified;
            # untrusted decoder/window frames are copied and verified at the boundary. Detector
            # and preview therefore share one immutable Orion-owned snapshot, never a driver/DMA
            # view that can be recycled underneath either consumer. Source-paced SHM may copy that
            # immutable snapshot while detection is still running; JPEG fallback retains the
            # detector-completion barrier. ORION_PREVIEW_COPY=1 remains a diagnostic extra copy.
            ownership_reason = _preview_frame_ownership_reason(frame)
            if ownership_reason:
                _note_drop("preview_frame_contract", ownership_reason)
                return
            if _preview_copy:
                f = frame.copy()
                # Preserve the same concurrency contract even in diagnostic-copy mode.
                f.setflags(write=False)
            else:
                f = frame
            with _preview_lock:
                callback_last_ns = int(_preview["callback_last_ns"] or 0)
                if callback_last_ns > 0:
                    callback_gap_ns = max(0, callback_at_ns - callback_last_ns)
                    _preview["callback_gap_count"] += 1
                    _preview["callback_gap_sum_ns"] += callback_gap_ns
                    _preview["callback_gap_max_ns"] = max(
                        int(_preview["callback_gap_max_ns"]), callback_gap_ns)
                _preview["callback_last_ns"] = callback_at_ns
                if _preview["frame"] is not None:
                    # Latest-wins shed: the previous stash was never encoded. Counted (not
                    # logged here — hot path) so preview_stats can report shed pressure.
                    _preview["coalesced"] += 1
                _preview["frame"] = (
                    f,
                    frame_number,
                    int(capture_seq or 0),
                    int(capture_timestamp_ns or 0),
                )
            _preview_wake.set()
        except Exception as exc:
            # Never let a preview stash failure escape into the capture/detect hot path, but
            # don't swallow it silently either. Standard logging (not the JSON _log emit): a
            # per-frame failure must not spam the launcher's stdout protocol. Counted so a
            # persistent stash fault shows up as a real number in `preview_dropped` telemetry.
            _note_drop("preview_stash", exc)

    # Stats must never consume a preview cadence slot. The former synchronous
    # _emit() ran on the preview worker every five seconds and could itself
    # manufacture the 32 ms gap it was trying to measure.
    _preview_stats_queue = queue.Queue(maxsize=2)
    _preview_stats_stop = threading.Event()

    def _preview_stats_emit_loop():
        while not _preview_stats_stop.is_set():
            try:
                payload = _preview_stats_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if payload is None:
                break
            while (_telemetry_priority_evt.is_set()
                   and not _preview_stats_stop.wait(0.001)):
                pass
            if not _preview_stats_stop.is_set():
                _emit(payload)

    def _queue_preview_stats(payload):
        try:
            _preview_stats_queue.put_nowait(payload)
        except queue.Full:
            # Diagnostics are latest-wins just like the preview; never block the
            # display worker or the detector's stdout authority.
            try:
                _preview_stats_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                _preview_stats_queue.put_nowait(payload)
            except queue.Full:
                _note_drop("preview_stats_emit")

    _preview_stats_thread = threading.Thread(
        target=_preview_stats_emit_loop, name="preview-stats", daemon=True)
    _preview_stats_thread.start()

    def _preview_loop():
        nonlocal _shm_writer, _shm_ok_count, _shm_fail_count, _shm_fail_streak
        if _preview_cv2 is None or _preview_b64 is None:
            # Already surfaced by the import handler above; log the consequence explicitly so the
            # "encoder thread exited immediately" state is never inferred from an absence of lines.
            _LOG.error("preview encoder thread exiting at start: no cv2/base64 - no frames will be sent")
            return
        # Preview is display-only.  Put its native thread below the detector/telemetry
        # threads so CPU pressure can reduce preview cadence, never meter sampling.
        try:
            if os.name == "nt":
                import ctypes as _ct
                _ct.windll.kernel32.SetThreadPriority(
                    _ct.windll.kernel32.GetCurrentThread(), -1)  # BELOW_NORMAL
        except Exception as exc:
            _LOG.debug("preview thread priority adjustment skipped: %r", exc)
        interval = 1.0 / preview_fps
        # De-alias gate (legacy / ORION_PREVIEW_PACE=0 mode): gate at gate_factor (default 85%) of
        # the interval, not 100%. A frame that arrives a hair EARLY (normal 59.9fps jitter against a
        # fixed 60fps gate) was being dropped by the strict `< interval` test, shedding frames
        # unevenly = judder. The tolerance emits it instead of dropping it; long-run rate is still
        # bounded by preview_fps because frames only arrive at the decode rate.
        #
        # PACED mode (default): the gate above still SHEDS an early frame whenever jitter exceeds
        # the tolerance (at 0.85 that is only ~2.5ms at 60fps) — the shed frame is overwritten by
        # the next arrival, so the viewer sees a ~33ms hole on a 60Hz display = the judder hitch.
        # Pacing instead HOLDS an early frame until the 1/preview_fps grid deadline and then emits
        # the newest stashed frame: no shed, and emissions land on an even grid. Cost: early frames
        # (only) are delayed by at most (interval - early_arrival) <= 16.7ms, typically 1-3ms of
        # real jitter — strictly cheaper than the 2-interval gap a shed produces, and it touches
        # the PREVIEW thread only (capture/detect hot path unchanged). Python 3.11+ on Windows uses
        # high-resolution waitable timers, so the sub-frame sleep is accurate on this rig (3.14).
        gate = interval * _gate_factor
        last = 0.0
        # preview_stats window (5s): distinguish producer callback, mapping
        # commit, and consumer-notification cadence from a frozen echo
        # (duplicate frame_number = upstream is re-serving the same pixels).
        st_t0 = time.monotonic()
        st_attempts = 0
        st_transported = 0
        st_aborted = 0
        st_b64_bytes = 0
        st_encode_ms_sum = 0.0
        st_encode_ms_max = 0.0
        st_dups = 0
        st_gap_count = 0
        st_gap_sum = 0.0
        st_gap_max = 0.0
        st_last_transport = 0.0
        st_write_count = 0
        st_write_ms_sum = 0.0
        st_write_ms_max = 0.0
        st_notify_count = 0
        st_notify_ms_sum = 0.0
        st_notify_ms_max = 0.0
        st_notify_gap_count = 0
        st_notify_gap_sum = 0.0
        st_notify_gap_max = 0.0
        st_last_notify = 0.0
        st_notify_mode = "none"
        st_last_fn = None
        # Preview transport identity cannot reuse detector sequence: detector-invalid dark/loading
        # frames are deliberately displayable without advancing detector authority. A sidecar-local
        # monotonic id lets native latest-wins reassembly accept those frames while preserving the
        # detector seq solely as the priority barrier above.
        wire_frame_id = 0
        while not _preview_stop.is_set():
            _preview_wake.wait(timeout=0.5)
            _preview_wake.clear()
            if _preview_stop.is_set():
                break
            now = time.monotonic()
            _source_cadence = _event_driven_shm_source and _shm_writer is not None
            if not _source_cadence:
                if _pace_early:
                    hold = interval - (now - last)
                    if 0.0 < hold <= interval:
                        time.sleep(hold)
                        now = time.monotonic()
                elif now - last < gate:
                    continue
            with _preview_lock:
                frame_data = _preview["frame"]
                _preview["frame"] = None
            if frame_data is None:
                continue
            frame, frame_number, capture_seq, capture_timestamp_ns = frame_data
            frame_data = None
            # Source-paced SHM is a bounded read-only copy on a below-normal thread,
            # so it can run beside detection without changing the detector's pixels.
            # Do not manufacture a visible hitch when detection legitimately needs
            # >12 ms.  CPU-heavy JPEG fallback still waits and sheds display work
            # before it can starve the bot.
            if (_preview_needs_detector_barrier(_source_cadence, _shm_writer is not None)
                    and not _preview_wait_for_detector(
                        orch, capture_seq, min(0.012, interval * 0.75))):
                with _preview_lock:
                    _preview["detector_backpressure"] += 1
                frame = None
                continue
            # [D1] The callback reference is an immutable Orion-owned detector snapshot (see
            # _video_cb's enforced ownership contract). Resize only when display geometry differs;
            # the normal 1280x720 path can be consumed directly because both transports only read
            # pixels. JPEG reaches here after the detector barrier; source-paced SHM is safe to copy
            # concurrently because mutation and aliased driver/decoder buffers fail closed in the
            # callback. This avoids both the old 1080p callback copy and a redundant same-size
            # preview copy without weakening ownership.
            try:
                h, w = frame.shape[:2]
                preview_height = max(360, int(preview_width * h / max(1, w)))
                if w == preview_width and h == preview_height:
                    # The detector boundary already owns an immutable 1280x720
                    # BGR frame on the normal path.  Re-resizing it to the same
                    # geometry made a full-frame copy that was immediately copied
                    # again into SHM, consuming enough of the preview worker's
                    # budget to shed otherwise healthy 60 Hz card frames.
                    small = frame
                else:
                    small = _preview_cv2.resize(
                        frame, (preview_width, preview_height),
                        interpolation=_preview_cv2.INTER_LINEAR,
                    )
            except Exception as exc:
                # Resize fault. Non-fatal (the encoder thread must keep running or the preview
                # never recovers) but counted + logged, never an invisible dropped frame. `last`
                # is deliberately NOT advanced: a fault must not consume a pacing grid slot.
                _note_drop("preview_encode", exc)
                small = None
            frame = None
            if small is None:
                continue
            if _preview_stats:
                st_attempts += 1
                if st_last_fn is not None and frame_number == st_last_fn:
                    st_dups += 1
                st_last_fn = frame_number
                if now - st_t0 >= 5.0:
                    with _preview_lock:
                        _coal = _preview["coalesced"]
                        _preview["coalesced"] = 0
                        _det_bp = _preview["detector_backpressure"]
                        _preview["detector_backpressure"] = 0
                        _callback_gap_count = int(_preview["callback_gap_count"])
                        _callback_gap_sum_ns = int(_preview["callback_gap_sum_ns"])
                        _callback_gap_max_ns = int(_preview["callback_gap_max_ns"])
                        _preview["callback_gap_count"] = 0
                        _preview["callback_gap_sum_ns"] = 0
                        _preview["callback_gap_max_ns"] = 0
                    _win = now - st_t0
                    _queue_preview_stats({
                        "event": "log",
                        "level": "info",
                        "msg": (
                            f"preview_stats: fps={st_transported / _win:.1f} "
                            f"attempt_fps={st_attempts / _win:.1f} aborted={st_aborted} "
                            f"coalesced={_coal} detector_backpressure={_det_bp} dup={st_dups} "
                            # FIX 3: cumulative frames LOST to a fault (stash/resize/imencode)
                            # rather than deliberately shed. Non-zero = a real bug, not pacing.
                            f"dropped={_drop_count('preview_stash', 'preview_encode', 'preview_imencode', 'preview_emit')} "
                            f"avg_kib={(st_b64_bytes / max(1, st_transported)) / 1024.0:.1f} "
                            f"encode_ms mean={st_encode_ms_sum / max(1, st_attempts):.2f} "
                            f"max={st_encode_ms_max:.2f} "
                            f"gap_ms mean={(st_gap_sum / max(1, st_gap_count)) * 1000.0:.1f} "
                            f"max={st_gap_max * 1000.0:.1f} "
                            f"pace={'source' if _source_cadence else ('on' if _pace_early else 'off')} "
                            f"gate={_gate_factor:.2f} "
                            f"transport={'shm' if _shm_writer is not None else 'jpeg'} "
                            f"shm_ok={_shm_ok_count} shm_fail={_shm_fail_count} "
                            f"callback_gap_ms mean={(_callback_gap_sum_ns / max(1, _callback_gap_count)) / 1_000_000.0:.1f} "
                            f"max={_callback_gap_max_ns / 1_000_000.0:.1f} "
                            f"shm_write_ms mean={st_write_ms_sum / max(1, st_write_count):.2f} "
                            f"max={st_write_ms_max:.2f} "
                            f"notify_ms mean={st_notify_ms_sum / max(1, st_notify_count):.3f} "
                            f"max={st_notify_ms_max:.3f} "
                            f"notify_gap_ms mean={(st_notify_gap_sum / max(1, st_notify_gap_count)) * 1000.0:.1f} "
                            f"max={st_notify_gap_max * 1000.0:.1f} "
                            f"notify_mode={st_notify_mode}"
                        ),
                    })
                    st_t0 = now
                    st_attempts = 0
                    st_transported = 0
                    st_aborted = 0
                    st_b64_bytes = 0
                    st_encode_ms_sum = 0.0
                    st_encode_ms_max = 0.0
                    st_dups = 0
                    st_gap_count = 0
                    st_gap_sum = 0.0
                    st_gap_max = 0.0
                    st_write_count = 0
                    st_write_ms_sum = 0.0
                    st_write_ms_max = 0.0
                    st_notify_count = 0
                    st_notify_ms_sum = 0.0
                    st_notify_ms_max = 0.0
                    st_notify_gap_count = 0
                    st_notify_gap_sum = 0.0
                    st_notify_gap_max = 0.0
            last = now
            if _shm_disable_evt.is_set() and _shm_writer is not None:
                _shm_writer.stop()
                _shm_writer = None
                _log("Preview transport: native requested JPEG fallback", "warn")
            # A self-initiated fallback is fail-closed until its session-scoped
            # control line has reached stdout.  Retrying this small record does
            # not buffer source frames; each failed attempt simply sheds this
            # display-only preview while capture/detection continue unchanged.
            if _shm_writer is None and _shm_failover.pending:
                if not _shm_failover.announce():
                    small = None
                    continue
            if _shm_writer is not None:
                try:
                    if _shm_writer.write(
                            small,
                            source_frame_number=frame_number,
                            timestamp_ns=capture_timestamp_ns):
                        _shm_ok_count += 1
                        _shm_fail_streak = 0
                        _shm_failover.note_success()
                        _transported_at = (
                            float(_shm_writer.last_commit_ns) / 1_000_000_000.0)
                        _write_ms = float(_shm_writer.last_write_duration_ms)
                        st_write_count += 1
                        st_write_ms_sum += _write_ms
                        st_write_ms_max = max(st_write_ms_max, _write_ms)
                        if st_last_transport > 0.0:
                            _gap = _transported_at - st_last_transport
                            st_gap_count += 1
                            st_gap_sum += _gap
                            st_gap_max = max(st_gap_max, _gap)
                        st_last_transport = _transported_at
                        st_transported += 1

                        if _shm_writer.event_notifications_enabled:
                            _notify_at = (
                                float(_shm_writer.last_event_notify_ns)
                                / 1_000_000_000.0)
                            _notify_ms = float(_shm_writer.last_event_notify_ms)
                            st_notify_mode = "event"
                        else:
                            _notify_started = time.perf_counter()
                            _emit({"event": "frame_shm",
                                   "frame_number": int(frame_number or 0)})
                            _notify_at = time.monotonic()
                            _notify_ms = (time.perf_counter() - _notify_started) * 1000.0
                            st_notify_mode = "stdout"
                        st_notify_count += 1
                        st_notify_ms_sum += _notify_ms
                        st_notify_ms_max = max(st_notify_ms_max, _notify_ms)
                        if st_last_notify > 0.0:
                            _notify_gap = _notify_at - st_last_notify
                            st_notify_gap_count += 1
                            st_notify_gap_sum += _notify_gap
                            st_notify_gap_max = max(st_notify_gap_max, _notify_gap)
                        st_last_notify = _notify_at
                        small = None
                        continue
                    _shm_fail_count += 1
                    _shm_fail_streak += 1
                    _shm_error = str(getattr(_shm_writer, "last_error", "") or "unknown")
                    _shm_writer, _allow_jpeg, _retired_now = (
                        _retire_preview_shm_after_write_failure(
                            _shm_writer, _shm_failover, frame_number))
                    if _retired_now:
                        _log(
                            "Preview transport: SHM failed 10 consecutive writes "
                            f"({_shm_error}); switching to JPEG",
                            "warn",
                        )
                    if not _allow_jpeg:
                        # Never mix a one-off JPEG into an otherwise active SHM
                        # epoch. The failed display frame is obsolete on the next
                        # source callback and detector input is unaffected.
                        small = None
                        continue
                except Exception as _shm_exc:
                    _shm_fail_count += 1
                    _shm_fail_streak += 1
                    _note_drop("preview_shm", _shm_exc)
                    _shm_writer, _allow_jpeg, _retired_now = (
                        _retire_preview_shm_after_write_failure(
                            _shm_writer, _shm_failover, frame_number))
                    if _retired_now:
                        _log(
                            "Preview transport: SHM raised 10 consecutive write errors; "
                            "switching to JPEG",
                            "warn",
                        )
                    if not _allow_jpeg:
                        small = None
                        continue
            try:
                _encode_t0 = time.perf_counter()
                ok, buf = _preview_cv2.imencode(".jpg", small, _jpeg_params)
                _encode_ms = (time.perf_counter() - _encode_t0) * 1000.0
                st_encode_ms_sum += _encode_ms
                st_encode_ms_max = max(st_encode_ms_max, _encode_ms)
                if ok:
                    # [D2] Zero-copy view over the encoded JPEG: imencode returns a contiguous
                    # (N,1) uint8 array, so reshape(-1).data is a memoryview onto the same bytes
                    # and skips a ~110-450 KB tobytes() copy per frame. Falls back to the copy if
                    # the array is ever non-contiguous.
                    try:
                        _jpeg = buf.reshape(-1).data
                    except Exception as exc:
                        _LOG.debug("preview: jpeg buffer view unavailable (%r) - copying", exc)
                        _jpeg = buf.tobytes()
                    # Bounded frame chunks release the shared JSONL lock every 8 KB,
                    # so a new detector payload can preempt display transport.
                    # Bound the wait; if telemetry cannot drain, shed preview instead of
                    # becoming backpressure on the bot's only meter-feed channel.
                    _prio_deadline = time.monotonic() + 0.006
                    while (int(getattr(orch, "_telemetry_revision", 0) or 0)
                           > int(_telemetry_state["sent_revision"])) \
                            and time.monotonic() < _prio_deadline:
                        time.sleep(0.0005)
                    if (int(getattr(orch, "_telemetry_revision", 0) or 0)
                            > int(_telemetry_state["sent_revision"])):
                        st_aborted += 1
                        with _preview_lock:
                            _preview["detector_backpressure"] += 1
                    else:
                        _encoded_b64 = _preview_b64.b64encode(_jpeg)
                        wire_frame_id += 1
                        if _emit_frame_chunks(
                                _encoded_b64, frame_number,
                                chunk_frame_id=wire_frame_id,
                                priority_event=_telemetry_priority_evt):
                            _transported_at = time.monotonic()
                            if st_last_transport > 0.0:
                                _gap = _transported_at - st_last_transport
                                st_gap_sum += _gap
                                st_gap_max = max(st_gap_max, _gap)
                            st_last_transport = _transported_at
                            st_transported += 1
                            st_b64_bytes += len(_encoded_b64)
                        else:
                            st_aborted += 1
                            with _preview_lock:
                                _preview["detector_backpressure"] += 1
                else:
                    # imencode reports failure by RETURN VALUE, not by raising — the old code
                    # simply skipped the emit, so a JPEG encoder fault was an invisible black
                    # panel with no log line anywhere.
                    _note_drop("preview_imencode")
            except Exception as exc:
                # imencode / base64 fault (the resize has its own handler above). Stays non-fatal
                # (the encoder thread must keep running or the preview never recovers), but it is
                # now counted + logged instead of becoming an invisible dropped frame.
                _note_drop("preview_encode", exc)

    _preview_thread = threading.Thread(
        target=_preview_loop, name="preview-encoder", daemon=True)
    _preview_thread.start()
    orch.set_video_callback(_video_cb)

    started = orch.start()
    if not started:
        err = getattr(orch, "_last_error_msg", "") or "failed to start orchestrator"
        # start() may already own a pre-attached frame reader and a launched
        # OrionStream child even though it never reached _running=True.  Reap the
        # partial generation before reporting the terminal verdict; otherwise the
        # next native reconnect can race a stray window/fixed-pipe owner.
        try:
            orch.stop()
        except Exception as exc:
            _LOG.exception("partial orchestrator start cleanup failed")
            err = f"{err} (partial-start cleanup failed: {exc})"
        _emit({"event": "error", "msg": err, "input_ready": False})
        return 4

    input_ready_fn = getattr(orch, "input_link_ready", None)
    input_ready = bool(callable(input_ready_fn) and input_ready_fn())
    _emit({"event": "started", "msg": "Remote Play detector running",
           "input_ready": input_ready})

    # [ORION_STANDBY 2026-08-30] Warm-preview mode (auto_launch_client=False, i.e.
    # no Remote Play client was launched at start): pre-boot the client NOW in the
    # fork's --standby mode so a later Connect promotes it (pays only the PS5
    # handshake, ~0.6s) instead of paying the client's ~1-3s boot. The standby
    # opens NO console session by construction; the promote path still requires
    # the launch-scoped streaminfo readiness marker. Old orchestrators/clients:
    # getattr keeps this inert, and the pool never spawns --standby at a binary
    # that lacks it (spawn-free marker sniff — a pre-standby client would raise
    # a visible modal parser-error dialog and linger, verified 2026-08-30).
    # Kill switch: ORION_STANDBY_CLIENT=0.
    if not orch_config.auto_launch_client and orch_config.platform.lower() != "xbox":
        try:
            _prewarm = getattr(orch, "prewarm_standby_client", None)
            if callable(_prewarm):
                _prewarm()
        except Exception as _standby_exc:
            _LOG.warning("standby prewarm not started: %s", _standby_exc)

    stop_evt = threading.Event()

    def _telemetry_loop():
        # Event-driven pacing: wake the moment a new detection publishes fresh meter state,
        # with a >=60Hz idle fallback. getattr keeps this inert on an orchestrator build that
        # predates the event (falls back to a fixed-interval wait inside _telemetry_wait).
        frame_ready = getattr(orch, "_frame_ready_evt", None)
        # RC-2b HEARTBEAT: a monotonic PER-EMISSION counter (advances every telemetry payload, even
        # during a capture stall when `frame_count` — the unique-frame dedup key — is frozen). The
        # native engine can use it to (a) know the sidecar is alive and (b) accept a synthesized
        # stall emission (meter_present=false) whose frame_count matches the last real one.
        _hb = 0
        # Stall threshold for the SYNTHESIZED no-meter emission (belt-and-suspenders alongside the
        # orchestrator CV-loop watchdog): if the pixel age (since the last UNIQUE frame) exceeds this,
        # force meter_present/raw_fed false in the payload so a frozen held fill can't be served.
        try:
            _stall_ms = float(os.environ.get("ORION_STALL_WATCHDOG_MS", "250") or "250")
        except Exception:
            _stall_ms = 250.0
        # Next monotonic instant the detector-health line is due (0 = emit on the first pass).
        _health_next = 0.0
        while not stop_evt.is_set():
            try:
                # One immutable reference pairs seq, source identity, all timestamps,
                # geometry, detector/remap state, and integrity authority for this payload.
                _processed = _read_processed_frame_snapshot(orch)
                # Actual processed frame dimensions + the capture tier that produced
                # them, so the UI can show the TRUE resolution being detected on (the
                # decoder pipe delivers the real 1080p decode; GDI delivers the embed).
                _pwh = _processed["frame_wh"]
                try:
                    _cap_w, _cap_h = int(_pwh[0]), int(_pwh[1])
                except Exception:
                    _cap_w, _cap_h = 0, 0
                # One explicit measurement epoch owns both age and timing math. Decoder
                # mode maps PTS onto epoch; capture-card mode publishes the raw epoch
                # unchanged. Raw epoch remains a separate same-frame identity stamp.
                _measurement_epoch_ms = float(_processed["measurement_epoch_ms"])
                _frame_age_ms = (max(
                    0.0, time.time() * 1000.0 - _measurement_epoch_ms)
                    if _measurement_epoch_ms > 0.0 else 0.0)
                # Raw transport liveness is independent from detector authority.
                # `_last_capture_ts` advances for every successfully delivered raw frame,
                # including duplicates and legitimate dark/loading-screen frames. The processed
                # timestamp above does not advance when detector input is rejected, so it must
                # not be used by native code as evidence that the sidecar process died.
                _transport_age_ms = _raw_transport_age_ms(orch)
                _payload_revision = int(_processed["revision"])
                payload = {
                    "event": "telemetry",
                    # Console-route authority is independent of live video. In
                    # preview it is false by design; after promotion it remains
                    # true only while the current Chiaki session marker has not
                    # been superseded by disconnect/session-quit.
                    "input_ready": bool(input_ready_fn and input_ready_fn()),
                    "fps": int(getattr(orch, "_fps", 0) or 0),
                    "requested_fps": int(getattr(orch_config, "target_fps", 60) or 60),
                    "capture_loop_fps": int(getattr(orch, "_capture_fps", 0) or 0),
                    "unique_frame_fps": int(getattr(orch, "_unique_frame_fps", 0) or 0),
                    "capture_width": _cap_w,
                    "capture_height": _cap_h,
                    "capture_tier": str(getattr(orch, "_last_capture_tier", "") or ""),
                    "duplicate_frame_pct": float(getattr(orch, "_duplicate_frame_pct", 0.0) or 0.0),
                    "frame_age_ms": _frame_age_ms,
                    "transport_age_ms": _transport_age_ms,
                    # Capture/source truth remains the authority timestamp above.
                    # This additive diagnostic exposes backend validation/copy/
                    # conversion/queue cost without feeding release timing.
                    "capture_publication_age_ms": _capture_publication_age_ms(orch),
                    # RC-2b PIXEL AGE (ms since the last UNIQUE frame) — the HONEST staleness signal for
                    # the native fresh gate. frame_age_ms above is refreshed by DUPLICATE frames (capture
                    # liveness), so it reads ~0 through a freeze; pixel_age_ms keeps climbing during a stall
                    # and is what distinguishes a live 60Hz feed from a frozen echo. Consume THIS, not
                    # frame_age_ms, when deciding whether a held fill is stale.
                    "pixel_age_ms": (
                        max(0.0, (time.perf_counter() - float(_processed["frame_ts"])) * 1000.0)
                        if float(_processed["frame_ts"]) > 0.0 else 0.0
                    ),
                    "frame_count": int(_processed["seq"]),
                    # [FRAME-ID JOIN] Decoder wire seq of the last decoded frame — the SAME counter the
                    # preview `frame` stream stamps each JPEG with (both are orch._last_decoded_frame_number;
                    # the preview forward and detection run on the same img in one orchestrator loop). The
                    # native overlay joins the bbox to the preview frame on THIS id so the lock box composites
                    # on the exact frame it was detected on. Distinct from frame_count (the unique-frame dedup
                    # key), which is a different counter and cannot be joined against the preview stream.
                    "frame_number": int(_processed["frame_number"]),
                    "goto_active": bool(getattr(orch, "_goto_shot_active", False)),
                    "goto_triggered": bool(getattr(orch, "_goto_shot_triggered", False)),
                    # Top-level meter presence for THIS frame. True when a meter was detected/served,
                    # False on loss. The native engine relaxes its emit gate to accept a false frame
                    # (fill/conf forced to 0 below) and resets its HOLD clock so it stops coasting on a
                    # stale held fill once the meter genuinely vanishes.
                    "meter_present": bool(_processed["meter_present"]),
                    # True only when the last processed frame had a clean RAW detection
                    # (not a meter_memory echo / roi_not_found). The native engine treats
                    # a false value as stale_or_memory so it won't trust a held fill.
                    "raw_fed": bool(_processed["raw_fed"]),
                    # Independent meter-specific proof for this physical shot epoch. This is
                    # false for the monotonic-rise fallback and for legacy readers/sidecars.
                    "gameplay_structure_verified": bool(
                        _processed["gameplay_structure_verified"]),
                    # Keep the uint64 exact across QJson's IEEE-754 number model.
                    # "0" is the explicit untokenized/invalid sentinel and is rejected natively.
                    "gameplay_structure_epoch": str(_safe_uint64(
                        _processed["gameplay_structure_epoch"])),
                    # A0 unified timebase: epoch-ms stamp of the last UNIQUE captured frame —
                    # the same wall clock the release markers use. The native engine bridges
                    # epoch<->steady once and runs all timing fusion on CAPTURE time, so IPC/
                    # processing jitter no longer smears the fill timeline.
                    "capture_ts_ms": float(_processed["epoch_ms"]),
                    "measurement_capture_ts_ms": _measurement_epoch_ms,
                    # Reader stage for the last processed frame (track / track_green / acquire /
                    # coast / no_meter): lets the engine tell a fresh read from a coasted sample
                    # without inferring it from rejection_reason strings.
                    "stage": str(_processed["stage"]),
                    # Phase-1: feed health passthrough — false when the capture card's content-stall
                    # detector has exhausted reopen attempts (feed_frozen). The native engine uses
                    # this to suppress blind fires on a dead feed. True when the feed is live.
                    "feed_healthy": (
                        bool(_processed["integrity_healthy"])
                        and not bool(_processed["backend_frozen"])
                    ),
                    # Dark/invalid detector content disarms the bot through feed_healthy, but is
                    # not itself proof that the backend or process needs to be restarted.
                    "backend_frozen": bool(
                        getattr(orch, "_last_feed_frozen", _processed["backend_frozen"])
                    ),
                    "frame_reject_reason": str(_processed["reject_reason"]),
                    "frame_reject_counts": dict(_processed["reject_counts"]),
                    "cv_frames_skipped": int(getattr(orch, "_cv_frames_skipped", 0) or 0),
                    "frame_revision": _payload_revision,
                }
                green = _processed["green"]
                if green:
                    payload["green"] = {
                        "start": float(green.get("start", 0.0)),
                        "end": float(green.get("end", 0.0)),
                        "center": float(green.get("center", 0.0)),
                        "width": float(green.get("width", 0.0)),
                    }
                # Detector-only release-window diagnostic: attached once and carrying the native
                # release identity captured inside the reader's shot epoch. It is intentionally not
                # named or consumed as a gameplay grade/outcome.
                _pgg = getattr(orch, "pop_green_grade", None)
                if callable(_pgg):
                    try:
                        _gg = _pgg()
                        if _gg:
                            _gg_wire = _release_window_diagnostic_wire(_gg)
                            if _gg_wire:
                                payload["release_window_diagnostic"] = _gg_wire
                    except Exception as exc:
                        # pop_green_grade is POP-ONCE: a swallowed fault here loses that shot's
                        # detector diagnostic permanently, so it must at least be counted.
                        _note_drop("telemetry_release_window_diagnostic", exc)
                _meter_present = bool(_processed["meter_present"])
                shot = _processed["shot"]
                if shot:
                    # On meter LOSS emit fill=0/confidence=0 (per the IPC contract) so the native engine
                    # sees the meter is gone instead of the last held shot state lingering on the overlay.
                    payload["shot"] = {
                        "fill_pct": float(shot.get("fill_pct", 0.0) or 0.0) if _meter_present else 0.0,
                        "confidence": float(shot.get("confidence", 0.0) or 0.0) if _meter_present else 0.0,
                        "rtt_offset_ms": float(shot.get("rtt_offset_ms", 0.0) or 0.0),
                    }
                # Meter bounding box (capture-frame px) of the last real detection. The native
                # post-release meter-settle grader discriminates a SLIDING fade/Go-To meter
                # (player airborne) from a SETTLED frozen marker (player landed) by bbox motion.
                # Without this the engine saw bbox=(0,0,0,0) every frame -> zero motion -> EVERY
                # meter looked "settled" -> moving shots were graded off a mid-flight read and
                # locked onto a false EXCELLENT. _last_meter_bbox updates per-frame while the
                # meter is visible (so a slide registers motion) and holds steady on the static
                # frozen marker; dropout frames are stale_or_memory and excluded by the grader.
                bbox = _processed["bbox"]
                if bbox:
                    try:
                        payload["bbox"] = [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
                        # [ORION_PROOF_DETECTOR_BOX 2026-09-19] The DETECTOR's rectangle for the
                        # same frame, before the reader's display hug. The native ownership proof
                        # judges shape CONTINUITY on it; the drawn `bbox` above is unchanged and
                        # remains what the overlay and meter_x/meter_y use. Sent inside the same
                        # try/except and from the same immutable snapshot, so the two rectangles
                        # are always the same frame's or neither is sent.
                        _dbb = _processed.get("det_bbox") or ()
                        if _dbb:
                            payload["det_bbox"] = [int(_dbb[0]), int(_dbb[1]),
                                                   int(_dbb[2]), int(_dbb[3])]
                        # frame WxH the bbox was computed in -> native maps with the correct scale
                        # (not a stale captureWidth/Height), so the overlay box never jumps on a res flip.
                        _bwh = _processed["bbox_wh"]
                        if _bwh:
                            payload["bbox_wh"] = [int(_bwh[0]), int(_bwh[1])]
                    except Exception as exc:
                        # No bbox -> the native overlay draws nothing AND the post-release
                        # meter-settle grader sees zero motion. Never silent.
                        _note_drop("telemetry_bbox", exc)
                # On-screen TIMING-banner verdict (green=on-target / red=off) for the native clock
                # grader -- the reliable ground truth the post-release meter can't read at the dead-top.
                # Calibration ONLY; the native engine never times off it. (Replaces the retired HUD path.)
                banner = getattr(orch, "_last_banner", None)
                banner_verdict = getattr(banner, "verdict", None) if banner is not None else None
                if banner_verdict and banner_verdict != "UNCLEAR":
                    payload["banner"] = {"verdict": banner_verdict,
                                         "strength": int(getattr(banner, "strength", 0) or 0)}
                track_payload = _processed["tracking"]
                if track_payload:
                    payload["tracking"] = track_payload
                # One snapshot per emitted frame.  get_snapshot() computes prediction/jitter and
                # copies bounded histories, so calling it separately for timing and telemetry
                # doubles avoidable 60-FPS Python/GIL work and can produce internally inconsistent
                # values if a sample lands between the two reads.
                _rtt_engine = getattr(orch, "_rtt_engine", None)
                _rtt_snapshot = None
                if _rtt_engine is not None:
                    try:
                        _rtt_snapshot = _rtt_engine.get_snapshot()
                    except Exception as exc:
                        # Losing the rtt block silently zeroes the engine's network offset.
                        _note_drop("telemetry_rtt", exc)
                # FROZEN-METER ORACLE -> self-measured release-path latency (IPC: measured_latency_ms).
                # The engine historically modeled ~13ms vs ~40-100ms real; this is the live measurement.
                # Absent (native keeps its own model) unless the oracle is enabled + has produced a value.
                _lat_attestation_generation = 0
                _lat_delivery_route = ""
                _lat_scope_epoch = 0
                _lat_scope_epoch_before = _latency_scope_epoch_snapshot(orch)
                _lat_pair_getter = getattr(
                    orch, "latency_estimator_attestation_snapshot", None)
                if callable(_lat_pair_getter):
                    (_lat, _lat_attestation_generation,
                     _lat_delivery_route) = _lat_pair_getter()
                else:
                    _lat_getter = getattr(orch, "latency_estimator_snapshot", None)
                    _lat = (_lat_getter() if callable(_lat_getter)
                            else getattr(orch, "_latency_estimator", None))
                _lat_scope_epoch_after = _latency_scope_epoch_snapshot(orch)
                if _lat_scope_epoch_after > 0:
                    _lat_scope_epoch = _lat_scope_epoch_after
                if (_lat_scope_epoch_before <= 0
                        or _lat_scope_epoch_before != _lat_scope_epoch_after):
                    # A source/mode re-key raced the estimator/token snapshot.
                    # Keep the observational latency values visible, but strip
                    # the route token so the hybrid can never become authority.
                    _lat_attestation_generation = 0
                    _lat_delivery_route = ""
                if _lat_scope_epoch > 0:
                    # Publish independently of estimator availability. A failed
                    # replacement load still changed authority and must revoke a
                    # previously Confirmed native controller proof immediately.
                    payload["measured_latency_scope_epoch"] = str(_lat_scope_epoch)
                if _lat is not None:
                    try:
                        # The CV thread can close an oracle while stdin opens the next native
                        # release. Capture exactly one frozen estimator generation for every
                        # latency/tick/probe field in this payload; never read live properties
                        # independently across that transition.
                        _lat_snapshot_getter = getattr(_lat, "telemetry_snapshot", None)
                        if not callable(_lat_snapshot_getter):
                            raise TypeError("latency estimator lacks telemetry_snapshot")
                        _lat_snapshot = _lat_snapshot_getter()
                        # [ORION_PROBE] raw press->appear of the latest warmup probe (present
                        # even before any label): the offline D_spawn calibration observable.
                        _praw = float(getattr(
                            _lat_snapshot, "last_probe_raw_ms", -1.0) or -1.0)
                        if _praw > 0.0:
                            payload["probe_raw_ms"] = _praw
                        # [ORION_PROBE] the D_spawn constant derived online from this run's raw
                        # probes against the independently measured oracle total. Telemetry only
                        # (the estimator never folds it back into the posterior); it exists so a
                        # single live session yields the shipping value of probe_spawn_offset_ms
                        # instead of requiring an offline pass over the session log.
                        _pspawn = float(getattr(
                            _lat_snapshot, "probe_spawn_estimate_ms", -1.0) or -1.0)
                        if _pspawn > 0.0:
                            payload["probe_spawn_estimate_ms"] = _pspawn
                        # Always publish the oracle state, including the cold value=0
                        # phase. This makes label starvation/rejection visible while
                        # preserving the native fail-closed value gate.
                        payload.update(_latency_oracle_wire(
                            _lat_snapshot,
                            controller_attestation_generation=
                                _lat_attestation_generation,
                            controller_delivery_route=_lat_delivery_route,
                            scope_epoch=_lat_scope_epoch))
                        # [Phase-2 A2(c)] console input-tick phase from the warmup probe run
                        # (sawtooth fit over tick-staggered presses). Shipped only while the
                        # decayed confidence is > 0 — absent fields keep the native earlier-only
                        # snap fully disengaged (byte-identical engine behaviour). NOTE: distinct
                        # from payload["rtt"]["tick_phase_ms"] (the NETWORK packet tick).
                        _tpc = float(getattr(
                            _lat_snapshot, "tick_phase_conf", 0.0) or 0.0)
                        if _tpc > 0.0:
                            payload["tick_phase_ms"] = float(getattr(
                                _lat_snapshot, "tick_phase_ms", -1.0))
                            payload["tick_phase_conf"] = _tpc
                            payload["tick_phase_sd_ms"] = float(getattr(
                                _lat_snapshot, "tick_phase_sd_ms", -1.0))
                    except Exception as exc:
                        # Dropping measured_latency_ms silently sends the engine back to its
                        # MODELLED latency (~13ms vs 40-100ms real) = every shot mistimed.
                        _note_drop("telemetry_latency", exc)
                # FAR-HORIZON TIP REGISTRATION is copied at the same detector-completion
                # boundary as frame_count, capture time, meter state, and bbox.  Never read
                # orch._last_tip_reg here: the detector may already be computing N+1 while
                # this telemetry iteration still owns frame N.
                payload.update(_tip_registration_wire(
                    _processed.get("tip_registration", {})))
                # POST-HOC full-shot refit tip label (once per completed shot): the clock
                # prior's EMA teacher. tip_epoch_ms is on the capture-epoch clock (A0).
                _ph = getattr(orch, "_last_posthoc", None)
                if _ph is not None:
                    try:
                        payload["posthoc_n"] = int(_ph.get("n", 0))
                        payload["posthoc_tip_ms"] = float(_ph.get("tip_epoch_ms", 0.0))
                        payload["posthoc_conf"] = float(_ph.get("conf", 0.0))
                    except Exception as exc:
                        _note_drop("telemetry_posthoc", exc)
                fusion_payload = _processed["fusion"]
                if fusion_payload:
                    payload["fusion"] = fusion_payload
                if _rtt_snapshot is not None:
                    try:
                        snap = _rtt_snapshot
                        payload["rtt"] = {
                            "ready": bool(snap.ready),
                            "target_verified": bool(getattr(snap, "target_verified", False)),
                            "target_generation": int(getattr(snap, "target_generation", 0)),
                            "sample_age_ms": float(getattr(snap, "sample_age_ms", -1.0)),
                            "court_evidence_age_ms": float(getattr(snap, "court_evidence_age_ms", -1.0)),
                            "phase_source_verified": bool(getattr(snap, "phase_source_verified", False)),
                            "raw_ms": float(snap.rtt_raw_ms),
                            "filtered_ms": float(snap.rtt_filtered_ms),
                            "half_ms": float(snap.rtt_half_ms),
                            "jitter_ms": float(snap.jitter_ms),
                            "effective_offset_ms": float(snap.effective_offset_ms),
                            "court_ip": str(snap.court_ip or ""),
                            "court_confidence": float(snap.court_confidence),
                            "tick_hz": float(1000.0 / snap.tick_interval_ms) if snap.tick_interval_ms > 0 else 0.0,
                            "tick_phase_ms": float(getattr(snap, "tick_phase_ms", 0.0)),
                            "phase_locked": bool(getattr(snap, "phase_locked", False)),
                            "phase_confidence": float(getattr(snap, "phase_confidence", 0.0)),
                            "next_tick_eta_ms": float(getattr(snap, "next_tick_eta_ms", 0.0)),
                            "predicted_rtt_ms": float(getattr(snap, "predicted_rtt_ms", 0.0)),
                            "predicted_offset_ms": float(getattr(snap, "predicted_offset_ms", 0.0)),
                            "jitter_prediction_ms": float(getattr(snap, "jitter_prediction_ms", 0.0)),
                            "packet_interval_ms": float(getattr(snap, "packet_interval_ms", 0.0)),
                            "packet_timing_confidence": float(getattr(snap, "packet_timing_confidence", 0.0)),
                            "decode_comp_ms": float(getattr(snap, "decode_comp_ms", 0.0)),
                            "sample_count": int(snap.sample_count),
                            "ping_method": str(snap.ping_method),
                        }
                    except Exception as exc:
                        # Losing the rtt block silently zeroes the engine's network offset.
                        _note_drop("telemetry_rtt", exc)
                # RC-2b: advance the per-emission heartbeat (monotonic even through a stall, when
                # frame_count is frozen) + synthesize a no-meter state when the pixel age says the feed
                # is stalled.
                _hb += 1
                payload["heartbeat"] = _hb
                # FIX 3: cumulative preview frames lost to a FAULT (stash / resize / imencode) —
                # distinct from the deliberate latest-wins `coalesced` shed. A climbing value is a
                # real bug; the launcher can surface it instead of the user seeing "choppy video"
                # with nothing in any log. Monotonic, so a delta over two samples is the rate.
                payload["preview_dropped"] = _drop_count(
                    "preview_stash", "preview_encode", "preview_imencode", "preview_emit")
                _apply_stall_override(payload, payload.get("pixel_age_ms", 0.0), _stall_ms)
                _apply_feed_health_override(payload)
                _emit(payload)
                _telemetry_state["sent_revision"] = max(
                    int(_telemetry_state["sent_revision"]), _payload_revision)
            except Exception as exc:
                # A raise here means the native engine got NO meter sample this frame. Count every
                # one (stderr, bounded), but only push the rare/first ones onto the launcher's
                # stdout JSONL protocol — this loop runs at 60+Hz, so an unconditional _log would
                # flood the same channel the meter feed uses.
                _n = _note_drop("telemetry_payload", exc)
                if _n == 1 or _n % 500 == 0:
                    _log(f"telemetry loop error (n={_n}): {exc}", "warn")
            # This payload (success or failure) no longer has priority over preview.
            # A later detector completion/reject will set the event again.
            if (int(getattr(orch, "_telemetry_revision", 0) or 0)
                    <= int(_telemetry_state["sent_revision"])):
                _telemetry_priority_evt.clear()
            # [METER DETECTION CARD] Reader detector health as its OWN small line every
            # ~2 s (see _DETECTOR_HEALTH_INTERVAL_S). Deliberately outside the payload
            # try-block above: a health failure can never cost the engine a meter sample,
            # and a payload failure never silences the card.
            if time.monotonic() >= _health_next:
                _health_next = time.monotonic() + _DETECTOR_HEALTH_INTERVAL_S
                try:
                    _health = _detector_health_wire(orch)
                    if _health is not None:
                        _emit(_health)
                except Exception as exc:
                    _note_drop("telemetry_detector_health", exc)
            # Event-driven meter feed (was a fixed 60Hz `stop_evt.wait(1/60)`): this loop is
            # the native engine's ONLY meter feed (shot.fill_pct / green / bbox). Waiting on
            # the orchestrator's per-frame `_frame_ready_evt` emits the payload the INSTANT a
            # new detection is ready -- removing up to ~16.7ms of feed staleness (the wait for
            # the next fixed tick) AND the 60fps emit ceiling -- while the 1/60s timeout keeps
            # RTT / tick / frame-age telemetry flowing at >=60Hz when no new frame has arrived.
            # SAME JSON payload/schema; only the emit CADENCE changed.
            _telemetry_wait(frame_ready, stop_evt)

    t = threading.Thread(target=_telemetry_loop, daemon=True)
    t.start()

    # Stdin command loop (so the launcher can update config / trigger actions)
    _court_ip_state = {"ip": None}   # last targeted court IP -> de-dupe the "retargeted" log spam

    # [ORION_DISCONNECT_AUDIT 2026-09-19] F1: the long-blocking session commands move
    # OFF the stdin reader.
    #
    # `start_stream` and `recover_input` call straight into blocking work with their
    # own budgets -- recover_input_link() paces three attempts against a 17 s deadline,
    # start_stream owns the whole promotion. They used to run ON the stdin thread, so
    # while either was in flight NOTHING else could be read from stdin -- including
    # `shutdown`. The native writes `shutdown`, waits kSidecarGracefulShutdownMs (5 s)
    # and then `taskkill /F /T`s us. So pressing Disconnect while input recovery was
    # running -- which is EXACTLY when the owner would press it, because input is dead
    # and the video is still live -- could not be honoured: we were force-killed 5 s
    # into a 17 s handler, chiaki_session_stop() never ran (console: "LAN cable
    # disconnected") and orch.stop() never ran (Elgato handle still held, so the
    # 3 s post-disconnect preview resume lands on a contended device).
    #
    # One worker, one queue: command ORDER between the offloaded commands is preserved
    # exactly as before, and `shutdown` now jumps ahead of them -- which is the whole
    # point. Both commands were already asynchronous from the native's point of view
    # (it waits for {"event": "promote"/"started"/"error"} and input_recovery/ready
    # against its own deadlines), so nothing downstream gains a new race.
    _deferred_cmd_queue: "queue.Queue" = queue.Queue(maxsize=8)

    def _deferred_command_worker():
        while True:
            try:
                item = _deferred_cmd_queue.get(timeout=0.25)
            except queue.Empty:
                if stop_evt.is_set():
                    return
                continue
            if item is None:
                return
            kind, payload = item
            # A teardown began while this sat in the queue: the native has already
            # stopped caring about the verdict and is waiting for us to exit.
            if stop_evt.is_set():
                continue
            try:
                if kind == "start_stream":
                    _run_start_stream(payload)
                elif kind == "recover_input":
                    _handle_recover_input(orch)
            except Exception as exc:
                _LOG.exception("deferred command %s raised", kind)
                _log(f"{kind} failed: {exc}", "error")

    def _run_start_stream(msg):
        try:
            _handle_start_stream(orch, msg)
        except Exception as exc:
            # FIX 1: the handler raising is ALSO a failed promotion. Without the error
            # event the native's belt-and-suspenders timer flips the UI to
            # "Autogreen running" over a stream that never started.
            _LOG.exception("start_stream handler raised")
            _log(f"start_stream failed: {exc}", "error")
            _emit({"event": "error",
                   "msg": f"Stream start failed: {exc}",
                   "phase": "promote_to_stream",
                   "input_ready": False})

    def _defer_command(kind, payload):
        """Hand a long-blocking command to the worker. Never blocks the reader."""
        try:
            _deferred_cmd_queue.put_nowait((kind, payload))
            return True
        except queue.Full:
            # The queue is bounded so a wedged handler cannot grow it without limit.
            # Refusing is honest: the native has a deadline on both commands and will
            # report the failure rather than wait forever on a lost instruction.
            _note_drop(f"deferred_cmd_full_{kind}", RuntimeError("command queue full"))
            _log(f"{kind} refused: a previous session command is still running", "warn")
            return False

    _deferred_cmd_thread = threading.Thread(
        target=_deferred_command_worker, name="orion-session-cmd", daemon=True)
    _deferred_cmd_thread.start()

    def _stdin_loop():
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as exc:
                # A dropped command line = a launcher instruction (start_stream / shutdown /
                # release_marker) silently never executed. Counted so a corrupted stdin channel
                # is visible; stays non-fatal so one bad line can't kill the command loop.
                _note_drop("stdin_bad_json", exc)
                continue
            cmd = str(msg.get("cmd", ""))
            if cmd == "shutdown":
                _EXIT_REASON["reason"] = "shutdown-cmd"
                stop_evt.set()
                break
            elif cmd == "trigger_goto":
                try:
                    orch.trigger_goto_shot()
                except Exception as exc:
                    _log(f"trigger_goto failed: {exc}", "warn")
            elif cmd == "pose_arm":
                # Native shot-begin -> arm the pose zero-cross search. The live virtual_controller=False
                # path has no orchestrator input-router loop, so the SQUARE-press arming comes from the
                # native engine that owns the controller.
                try:
                    orch.arm_pose(msg.get("arm_token"))
                except Exception as exc:
                    _log(f"pose_arm failed: {exc}", "warn")
            elif cmd == "calibrate_meter":
                # Shoot-to-train calibration window. Scaffolding: log the phase + count so the
                # UI flow is wired end-to-end. The detector already auto-picks the meter colour
                # (auto_meter_color, pushed via config); accumulating + persisting the trained
                # green-chevron HSV across the window is the live-tuning follow-up.
                try:
                    phase = str(msg.get("phase", "")).strip()
                    count = _safe_int(msg.get("count"), 0)
                    _log(f"calibrate_meter: phase={phase} count={count}")
                    fn = getattr(orch, "calibrate_meter", None)
                    if callable(fn):
                        fn(phase, count)
                except Exception as exc:
                    _log(f"calibrate_meter failed: {exc}", "warn")
            elif cmd == "release_marker":
                _handle_release_marker(orch, msg)
            elif cmd == "latency_route_attestation":
                _emit(_latency_route_attestation_ack_payload(orch, msg))
            elif cmd == "probe_marker":
                _handle_probe_marker(orch, msg)
            elif cmd == "set_court_ip":
                try:
                    ip = str(msg.get("ip", "")).strip()
                    if ip and getattr(orch, "_rtt_engine", None):
                        # Only (re)target + log when the IP actually CHANGES. The native was sending
                        # set_court_ip on a hot path, so the unconditional "retargeted" line spammed
                        # ~27k valueless lines/session. The numeric RTT signal now lives in the engine's
                        # per-tick "RTT tick:" log instead.
                        if ip != _court_ip_state.get("ip"):
                            orch._rtt_engine.set_target_ip(ip)
                            _court_ip_state["ip"] = ip
                            _log(f"RTT sync retargeted to court IP {ip}")
                except Exception as exc:
                    _log(f"set_court_ip failed: {exc}", "warn")
            elif cmd == "clear_court_target":
                try:
                    if getattr(orch, "_rtt_engine", None):
                        orch._rtt_engine.clear_court_target()
                    _court_ip_state["ip"] = None
                    _log("court RTT target cleared", "info")
                except Exception as exc:
                    _note_drop("clear_court_target", exc)
            elif cmd == "observe_packet":
                try:
                    if getattr(orch, "_rtt_engine", None):
                        orch._rtt_engine.observe_packet(
                            str(msg.get("src_ip", "")),
                            str(msg.get("dst_ip", "")),
                            _safe_int(msg.get("src_port"), 0),
                            _safe_int(msg.get("dst_port"), 0),
                            _safe_int(msg.get("size"), 0),
                            _safe_float(msg.get("ts"), 0.0),
                        )
                except Exception as exc:
                    # High-frequency (per observed packet): counted + DEBUG only. A persistent
                    # fault here silently starves the RTT engine's packet-tick estimate.
                    _note_drop("observe_packet", exc)
            elif cmd == "update_remap":
                try:
                    from controller_remap import RemapConfig
                    cur = orch._remap_engine._config if orch._remap_engine else None
                    if cur is None:
                        continue
                    new_cfg = RemapConfig(
                        enabled=bool(msg.get("enabled", cur.enabled)),
                        # [ORION_INPUT_MODE 2026-08-10] see the note at the startup RemapConfig.
                        # Hot-apply must honour the pushed value too, otherwise changing the
                        # input source in the UI silently reverts on the next update_remap.
                        input_mode=str(msg.get("input_mode", cur.input_mode) or cur.input_mode),
                        tempo_wait_ms=_safe_float(msg.get("tempo_wait_ms", cur.tempo_wait_ms), cur.tempo_wait_ms),
                        tempo_flick_hold_ms=_safe_float(msg.get("tempo_flick_hold_ms", cur.tempo_flick_hold_ms), cur.tempo_flick_hold_ms),
                        tempo_min_stick_hold_ms=_safe_float(msg.get("tempo_min_stick_hold_ms", cur.tempo_min_stick_hold_ms), cur.tempo_min_stick_hold_ms),
                        tempo_fallback_timeout_ms=_safe_float(msg.get("tempo_fallback_timeout_ms", cur.tempo_fallback_timeout_ms), cur.tempo_fallback_timeout_ms),
                        min_hold_ms=_safe_float(msg.get("min_hold_ms", cur.min_hold_ms), cur.min_hold_ms),
                        max_hold_ms=_safe_float(msg.get("max_hold_ms", cur.max_hold_ms), cur.max_hold_ms),
                        fixed_hold_ms=_safe_float(msg.get("fixed_hold_ms", cur.fixed_hold_ms), cur.fixed_hold_ms),
                        hold_release_strategy=str(msg.get("hold_release_strategy", cur.hold_release_strategy)),
                        release_pulse_ms=_safe_float(msg.get("release_pulse_ms", cur.release_pulse_ms), cur.release_pulse_ms),
                        early_late_offset_ms=_safe_float(msg.get("early_late_offset_ms", cur.early_late_offset_ms), cur.early_late_offset_ms),
                        green_window_target=str(msg.get("green_window_target", cur.green_window_target)),
                        green_window_priority=_safe_bool(msg.get("green_window_priority", cur.green_window_priority), cur.green_window_priority),
                        no_green_target_pct=_safe_float(msg.get("no_green_target_pct", cur.no_green_target_pct), cur.no_green_target_pct),
                        output_latency_ms=_safe_float(msg.get("output_latency_ms", cur.output_latency_ms), cur.output_latency_ms),
                        stick_down_threshold=_safe_float(msg.get("stick_down_threshold", cur.stick_down_threshold), cur.stick_down_threshold),
                        stick_up_threshold=_safe_float(msg.get("stick_up_threshold", cur.stick_up_threshold), cur.stick_up_threshold),
                        goto_enabled=False,
                        goto_flick_hold_ms=_safe_float(msg.get("goto_flick_hold_ms", cur.goto_flick_hold_ms), cur.goto_flick_hold_ms),
                        goto_arm_frames=max(1, _safe_int(msg.get("goto_arm_frames", cur.goto_arm_frames), cur.goto_arm_frames)),
                        confidence_gate=_safe_float(msg.get("confidence_gate", cur.confidence_gate), cur.confidence_gate),
                        stable_frames_required=max(1, _safe_int(msg.get("stable_frames_required", cur.stable_frames_required), cur.stable_frames_required)),
                        gpc_flick_chain_ms=_safe_float(msg.get("gpc_flick_chain_ms", cur.gpc_flick_chain_ms), cur.gpc_flick_chain_ms),
                        input_release_grace_ms=_safe_float(msg.get("input_release_grace_ms", cur.input_release_grace_ms), cur.input_release_grace_ms),
                        stick_hysteresis_pct=_safe_float(msg.get("stick_hysteresis_pct", cur.stick_hysteresis_pct), cur.stick_hysteresis_pct),
                        meter_style=str(msg.get("meter_style", cur.meter_style) or cur.meter_style),
                        shot_trigger_mode=str(msg.get("shot_trigger_mode", cur.shot_trigger_mode) or cur.shot_trigger_mode),
                        active_shot_type=str(msg.get("active_shot_type", cur.active_shot_type) or cur.active_shot_type),
                        shot_type_offsets=(msg.get("shot_type_offsets") if isinstance(msg.get("shot_type_offsets"), dict) else dict(cur.shot_type_offsets)),
                    )
                    # Support both method names depending on the remap engine build.
                    if hasattr(orch._remap_engine, "set_config"):
                        orch._remap_engine.set_config(new_cfg)
                    else:
                        orch._remap_engine.update_config(new_cfg)
                    # Live-apply meter style/color to the running CV detector so
                    # the Meter tab's color/style controls take effect without a
                    # reconnect (color drives the HSV detection mask).
                    try:
                        ms = msg.get("meter_style")
                        mc = msg.get("meter_color")
                        if (ms or mc) and hasattr(orch, "update_meter"):
                            orch.update_meter(meter_style=ms, meter_color=mc)
                    except Exception as _mexc:
                        _log(f"update_meter failed: {_mexc}", "warn")
                    _log("remap config updated")
                except Exception as exc:
                    _log(f"update_remap failed: {exc}", "warn")
            elif cmd == "start_stream":
                # WARM-PREVIEW -> STREAM promotion: bring up Chiaki + input hook IN PLACE (no process
                # restart, no capture/detector teardown) and re-emit {"event":"started"} so the native
                # flips to Running. See _handle_start_stream for the full contract.
                # [F1 2026-09-19] Deferred to the session-command worker so a `shutdown`
                # arriving mid-promotion can still be READ. Same handler, same events.
                if not _defer_command("start_stream", msg):
                    _emit({"event": "error",
                           "msg": "Stream start refused: a session command is still running",
                           "phase": "promote_to_stream",
                           "input_ready": False})
            elif cmd == "recover_input":
                # [F1 2026-09-19] Deferred for the same reason, and this is the one that
                # actually bit: a Disconnect pressed during the 17 s input recovery.
                _defer_command("recover_input", msg)
            elif cmd == "shot_gate_arm":
                source = str(msg.get("source", "hw") or "hw")[:24]
                shot_epoch = _safe_uint64(msg.get("shot_epoch"))
                # [ORION_SHOT_GATE_TYPE 2026-09-15] shot_type/rhythm are OPTIONAL: an older
                # native sends neither and the reader keeps the union meter-onset window.
                shot_type = str(msg.get("shot_type", "") or "")[:24].strip()
                rhythm = bool(msg.get("rhythm", 0))
                fn = getattr(orch, "arm_shot_gate", None)
                if callable(fn):
                    try:
                        fn(source, shot_epoch, shot_type, rhythm)
                    except TypeError:
                        # Older in-tree orchestrator: epoch-only arm, no type channel.
                        fn(source, shot_epoch)
                else:
                    # Older in-tree orchestrators expose only the internal helper.
                    legacy = getattr(orch, "_arm_shot_gate", None)
                    if callable(legacy):
                        try:
                            legacy(source, shot_epoch)
                        except TypeError:
                            legacy(source)
            elif cmd == "shot_gate_release":
                # [ORION_SHOT_GATE_RELEASE 2026-09-15] The engine's release edge, on the same
                # channel as the arm. Ends the reader's press window at the instant the shot
                # actually left the hand instead of letting it time out.
                fn = getattr(orch, "release_shot_gate", None)
                if callable(fn):
                    try:
                        fn(_safe_uint64(msg.get("shot_epoch")),
                           float(msg.get("release_ms", 0.0) or 0.0))
                    except (TypeError, ValueError):
                        fn(_safe_uint64(msg.get("shot_epoch")))
            elif cmd == "shot_gate_disarm":
                # The press ended with NO release edge (manual cancel / engine abort).
                fn = getattr(orch, "disarm_shot_gate", None)
                if callable(fn):
                    try:
                        fn(_safe_uint64(msg.get("shot_epoch")),
                           str(msg.get("reason", "disarm") or "disarm")[:32])
                    except TypeError:
                        fn(_safe_uint64(msg.get("shot_epoch")))
            elif cmd == "preview_transport":
                # Native could not open/read the mapping.  Switch in place to
                # the already-tested JPEG path; capture and detection continue.
                if str(msg.get("mode", "")).strip().lower() == "jpeg":
                    _shm_disable_evt.set()
                    _preview_wake.set()

    def _stdin_loop_wrapped():
        _stdin_loop()
        # The for-raw-in-stdin loop ending WITHOUT a shutdown cmd = the native closed our stdin
        # (parent died or channel torn down). We deliberately do NOT stop on EOF (launch-script
        # orphan reaping handles true orphans), but it must be visible in the death forensics.
        if "reason" not in _EXIT_REASON:
            _log("sidecar stdin EOF without shutdown cmd (native gone?) - staying alive", "warn")
            _EXIT_REASON["reason"] = "stdin-eof-then-unknown"

    stdin_thread = threading.Thread(target=_stdin_loop_wrapped, daemon=True)
    stdin_thread.start()

    # Handle SIGTERM/SIGINT for clean shutdown
    def _sig(signum, frame):
        _EXIT_REASON["reason"] = "signal"
        stop_evt.set()

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception as exc:
        # No clean-shutdown path on signal -> the launcher's terminate() escalates to a hard kill,
        # which drops the PS5 session ungracefully ("LAN cable disconnected"). Worth a line.
        _LOG.warning("signal handlers NOT installed (%r): shutdown will rely on the stdin command", exc)

    try:
        while not stop_evt.is_set():
            stop_evt.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        # [ORION_DISCONNECT_AUDIT 2026-09-19] THE PS5 DISCONNECT HANDSHAKE GOES FIRST.
        # The native gives this whole teardown ~5 s (kSidecarGracefulShutdownMs) before
        # `taskkill /F /T`. Everything below is local cleanup we can afford to lose --
        # a dropped preview frame, a truncated diagnostic CSV. The Chiaki close is the
        # ONE step with an external, non-recoverable consequence: skip it and the
        # console never gets chiaki_session_stop(), so it reports the transport
        # vanishing mid-session ("LAN cable was disconnected").
        #
        # It used to run inside orch.stop() BELOW the two preview joins (2.0 s + 1.0 s),
        # so on a slow join the WM_CLOSE went out at t≈3 s and its own 3 s wait ran past
        # the force-kill at t=5 s -- the orchestrator's own "GRACEFUL CHIAKI SHUTDOWN
        # GOES FIRST" reasoning was correct and simply pre-empted one level up. This is
        # a REORDER, not new work: orch.stop()'s call is idempotent and now a no-op.
        _close_client_first = getattr(orch, "close_remote_play_client", None)
        if callable(_close_client_first):
            try:
                _close_client_first()
            except Exception as exc:
                _LOG.exception("graceful Remote Play client close failed")
                _log(f"chiaki close error: {exc}", "warn")
        _preview_stop.set()
        _preview_wake.set()
        _preview_thread.join(timeout=2.0)
        # The diagnostics writer is independent from the preview worker so a
        # slow stdout flush cannot consume a frame slot. Stop it explicitly and
        # boundedly; a daemon thread must not race the final `stopped` event or
        # retain a stale payload into interpreter teardown.
        _preview_stats_stop.set()
        try:
            _preview_stats_queue.put_nowait(None)
        except queue.Full:
            try:
                _preview_stats_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                _preview_stats_queue.put_nowait(None)
            except queue.Full:
                pass
        _preview_stats_thread.join(timeout=1.0)
        if _shm_writer is not None and not _preview_thread.is_alive():
            try:
                _shm_writer.stop()
            except Exception as exc:
                _note_drop("preview_shm_stop", exc)
        try:
            orch.stop()
        except Exception as exc:
            # Teardown of a real resource (capture card handle, chiaki client, controller).
            # A swallowed failure here leaves the device held so the NEXT launch cannot open it,
            # so record the full traceback to stderr as well as the launcher line.
            _LOG.exception("orchestrator stop() failed - capture/client resources may be leaked")
            _log(f"stop error: {exc}", "warn")
        _emit({"event": "stopped"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
