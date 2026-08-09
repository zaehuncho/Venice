#!/usr/bin/env python
"""Classify live-stream cadence without blaming the user's connection by default.

The Orion log has four independently measured stages:

* ``Capture health`` -- capture-card or decoded-pipe producer delivery.
* ``preview_stats`` -- sidecar-to-native SHM/JPEG transport.
* ``preview_pipeline`` -- native reads/decodes/GUI handoff.
* ``qml_preview_pipeline`` -- QML image-provider fetch cadence.

A healthy producer followed by a slower or gapped preview is an Orion defect.  A
slow producer is *not* automatically called a bad connection: Remote Play needs
explicit Chiaki loss/corruption evidence before the report attributes it to the
network.  This is a live release gate, not a synthetic promise about USB devices,
Wi-Fi, HDMI sources, or GPU drivers outside Orion's control.

Examples:
  .venv/Scripts/python.exe tools/timing/stream_smoothness_report.py
  .venv/Scripts/python.exe tools/timing/stream_smoothness_report.py \
      --orion-log logs/orion_native.log --chiaki-log path/to/chiaki_session.log
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import statistics
import sys
from typing import Iterable, Sequence


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"

_CAPTURE_RE = re.compile(
    rf"Capture health: tier=(?P<tier>\S+).*?"
    rf"uniqfps=(?P<unique>{_NUMBER})\s+dup%=(?P<duplicate>{_NUMBER})\s+"
    rf"export_fps=(?P<export>{_NUMBER})\s+gap_fps=(?P<gap>{_NUMBER})\s+"
    rf"cv_fps=(?P<cv>{_NUMBER})\s+detect_ms=(?P<detect>{_NUMBER})"
)

# New capture diagnostics expose cadence measured at the backend's verified
# frame ring, before the orchestrator/detector/preview consumers run.  Keep this
# matcher additive so logs from older builds remain usable.  A zero raw_fps is
# emitted during warm-up (and by backends without cadence_stats); it is not, by
# itself, proof of a stopped source.
_CAPTURE_RAW_RE = re.compile(
    rf"raw_fps=(?P<raw_fps>{_NUMBER})\s+"
    rf"raw_gap_max_ms=(?P<raw_gap_max>{_NUMBER})\s+"
    rf"raw_late=(?P<raw_late>\d+)\s+"
    rf"source_skip=(?P<source_skip>\d+)"
)

_PREVIEW_RE = re.compile(
    rf"preview_stats: fps=(?P<fps>{_NUMBER})\s+"
    rf"attempt_fps=(?P<attempt>{_NUMBER})\s+aborted=(?P<aborted>\d+)\s+"
    rf"coalesced=(?P<coalesced>\d+)\s+"
    rf"detector_backpressure=(?P<backpressure>\d+)\s+dup=(?P<duplicate>\d+)\s+"
    rf"dropped=(?P<dropped>\d+).*?gap_ms mean=(?P<gap_mean>{_NUMBER})\s+"
    rf"max=(?P<gap_max>{_NUMBER})\s+pace=(?P<pace>\S+)\s+"
    rf"gate=(?P<gate>{_NUMBER})\s+transport=(?P<transport>\S+)\s+"
    rf"shm_ok=(?P<shm_ok>\d+)\s+shm_fail=(?P<shm_fail>\d+)"
)

# Additive producer/commit/notification timing appended by the named-event SHM
# transport. Keep it separate from the core matcher so older session logs remain
# parseable, while new runs can prove which exact stage manufactured a hitch.
_PREVIEW_CADENCE_RE = re.compile(
    rf"callback_gap_ms mean=(?P<callback_mean>{_NUMBER})\s+"
    rf"max=(?P<callback_max>{_NUMBER})\s+"
    rf"shm_write_ms mean=(?P<write_mean>{_NUMBER})\s+"
    rf"max=(?P<write_max>{_NUMBER})\s+"
    rf"notify_ms mean=(?P<notify_mean>{_NUMBER})\s+"
    rf"max=(?P<notify_max>{_NUMBER})\s+"
    rf"notify_gap_ms mean=(?P<notify_gap_mean>{_NUMBER})\s+"
    rf"max=(?P<notify_gap_max>{_NUMBER})\s+"
    rf"notify_mode=(?P<notify_mode>\S+)"
)

_NAMED_EVENT_SHM_MARKER = "Preview transport: shared memory active (named event;"

_PIPELINE_SHM_RE = re.compile(
    rf"preview_pipeline: transport=shm\s+present_fps=(?P<present>{_NUMBER})\s+"
    rf"total=(?P<total>\d+)\s+open_fail_run=(?P<open_fail>\d+)\s+"
    rf"read_fault_run=(?P<read_fault>\d+)"
)

_PIPELINE_SHM_CADENCE_RE = re.compile(
    rf"source_fps=(?P<source>{_NUMBER})\s+source_total=(?P<source_total>\d+)\s+"
    rf"jitter_drop=(?P<jitter_drop>\d+)\s+underflow=(?P<underflow>\d+)\s+"
    rf"depth=(?P<depth>\d+)\s+max_depth=(?P<max_depth>\d+)\s+"
    rf"present_gap_max_ms=(?P<present_gap_max>{_NUMBER})\s+"
    rf"event_wait_timeouts=(?P<event_wait_timeouts>\d+)\s+"
    rf"event_wait_failures=(?P<event_wait_failures>\d+)\s+"
    rf"event_generation_probes=(?P<event_generation_probes>\d+)\s+"
    rf"event_notification_losses=(?P<event_notification_losses>\d+)\s+"
    rf"ready_frame_replaced=(?P<ready_frame_replaced>\d+)\s+"
    rf"delivery_schedule_failures=(?P<delivery_schedule_failures>\d+)"
)

_SHM_INTEGRITY_RE = re.compile(
    r"preview_shm_integrity:\s+"
    r"event_wait_timeouts=(?P<event_wait_timeouts>\d+)\s+"
    r"event_wait_failures=(?P<event_wait_failures>\d+)\s+"
    r"event_generation_probes=(?P<event_generation_probes>\d+)\s+"
    r"event_notification_losses=(?P<event_notification_losses>\d+)\s+"
    r"ready_frame_replaced=(?P<ready_frame_replaced>\d+)\s+"
    r"delivery_schedule_failures=(?P<delivery_schedule_failures>\d+)\s+"
    r"notification_failure_run=(?P<notification_failure_run>\d+)"
)

_PIPELINE_JPEG_RE = re.compile(
    rf"preview_pipeline: transport=jpeg\s+rx_fps=(?P<rx>{_NUMBER})\s+"
    rf"decode_fps=(?P<decode>{_NUMBER})\s+present_fps=(?P<present>{_NUMBER})\s+"
    rf"decode_mailbox_drop=(?P<decode_drop>\d+)\s+"
    rf"gui_mailbox_drop=(?P<gui_drop>\d+)"
)

_QML_PIPELINE_RE = re.compile(
    rf"qml_preview_pipeline: set_fps=(?P<set>{_NUMBER})\s+"
    rf"request_fps=(?P<request>{_NUMBER})\s+"
    rf"request_gap_max_ms=(?P<gap_max>{_NUMBER})\s+"
    rf"sets=(?P<sets>\d+)\s+requests=(?P<requests>\d+)\s+"
    rf"async=(?P<async>[01])"
    rf"(?:\s+snapshot_miss=(?P<snapshot_miss>\d+)\s+"
    rf"ready_ack_fps=(?P<ready_ack>{_NUMBER})\s+"
    rf"ready_ack_total=(?P<ready_total>\d+)\s+"
    rf"stale_ack=(?P<stale_ack>\d+))?"
)

# A report session is one sidecar/source generation.  Transport negotiation,
# reader-open, and warm-preview promotion happen *inside* that generation and
# therefore must not truncate its protocol marker or early cadence windows.
# ``Sidecar job assigned`` is emitted immediately after each process start for
# both capture-card and decoded-pipe paths.  The capture-opening marker is a
# compatibility fallback which also precedes SHM negotiation.
_SESSION_START_MARKERS = (
    "Sidecar job assigned: orphan cleanup armed.",
    "Live capture preview: opening capture card",
)

# Chiaki logs may be append-only across several Remote Play connections.  Loss
# from an old connection is not evidence about the current producer slowdown.
# Keep the matcher deliberately narrow and fall back to the whole file when a
# caller supplies a per-session log with no lifecycle lines.
_CHIAKI_SESSION_MARKERS = (
    ">> Started session",
    "Starting session request for ",
    "SESSION START THREAD - Starting RUDP session",
)

_NETWORK_LOSS_PATTERNS = (
    re.compile(r"Detected missing or corrupt frame", re.IGNORECASE),
    re.compile(r"video FEC failure", re.IGNORECASE),
    re.compile(r"\bframes_lost=(?:[1-9]\d*)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class CaptureSample:
    tier: str
    unique_fps: float
    duplicate_pct: float
    export_fps: float
    gap_fps: float
    cv_fps: float
    detect_ms: float
    raw_fps: float | None = None
    raw_gap_max_ms: float | None = None
    raw_late: int | None = None
    source_skip: int | None = None


@dataclass(frozen=True)
class PreviewSample:
    fps: float
    attempt_fps: float
    aborted: int
    coalesced: int
    detector_backpressure: int
    duplicate: int
    dropped_total: int
    gap_mean_ms: float
    gap_max_ms: float
    pace: str
    transport: str
    shm_ok: int
    shm_fail: int
    callback_gap_mean_ms: float | None = None
    callback_gap_max_ms: float | None = None
    shm_write_mean_ms: float | None = None
    shm_write_max_ms: float | None = None
    notify_mean_ms: float | None = None
    notify_max_ms: float | None = None
    notify_gap_mean_ms: float | None = None
    notify_gap_max_ms: float | None = None
    notify_mode: str | None = None
    cadence_metrics_complete: bool = False


@dataclass(frozen=True)
class PipelineSample:
    transport: str
    present_fps: float
    receive_fps: float = 0.0
    decode_fps: float = 0.0
    open_fail_run: int = 0
    read_fault_run: int = 0
    decode_drop: int = 0
    gui_drop: int = 0
    source_fps: float | None = None
    source_total: int | None = None
    jitter_drop: int | None = None
    underflow: int | None = None
    depth: int | None = None
    max_depth: int | None = None
    present_gap_max_ms: float | None = None
    event_wait_timeouts: int | None = None
    event_wait_failures: int | None = None
    event_generation_probes: int | None = None
    event_notification_losses: int | None = None
    ready_frame_replaced: int | None = None
    delivery_schedule_failures: int | None = None
    cadence_metrics_complete: bool = False


@dataclass(frozen=True)
class ShmIntegritySample:
    event_wait_timeouts: int
    event_wait_failures: int
    event_generation_probes: int
    event_notification_losses: int
    ready_frame_replaced: int
    delivery_schedule_failures: int
    notification_failure_run: int


@dataclass(frozen=True)
class QmlSample:
    set_fps: float
    request_fps: float
    request_gap_max_ms: float
    frames_set: int
    requests: int
    asynchronous: bool
    snapshot_misses: int | None = None
    ready_ack_fps: float | None = None
    ready_ack_total: int | None = None
    stale_acks: int | None = None


@dataclass
class StreamEvidence:
    captures: list[CaptureSample] = field(default_factory=list)
    previews: list[PreviewSample] = field(default_factory=list)
    pipelines: list[PipelineSample] = field(default_factory=list)
    shm_integrity: list[ShmIntegritySample] = field(default_factory=list)
    qml: list[QmlSample] = field(default_factory=list)
    network_loss_events: int = 0
    named_event_shm: bool = False


@dataclass(frozen=True)
class StreamAssessment:
    verdict: str
    source: str
    reasons: tuple[str, ...]
    source_fps: float | None
    preview_fps: float | None
    present_fps: float | None
    qml_fps: float | None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def _median(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def _counter_advanced(samples: Sequence[object], attribute: str) -> bool:
    """Whether a cumulative counter advanced inside the selected session.

    SharedMemoryFramePump counters live for the native process and can enter a
    later source epoch with a non-zero baseline.  Treating that inherited value
    as a new fault made clean reconnects fail forever.  Every transition after
    the first observed baseline is still evaluated across the whole session.
    """
    values = [
        int(value) for sample in samples
        if (value := getattr(sample, attribute, None)) is not None
    ]
    return any(current > previous for previous, current in zip(values, values[1:]))


def _counter_regressed(samples: Sequence[object], attribute: str) -> bool:
    """Fail closed if supposedly cumulative telemetry resets mid-session."""
    values = [
        int(value) for sample in samples
        if (value := getattr(sample, attribute, None)) is not None
    ]
    return any(current < previous for previous, current in zip(values, values[1:]))


def latest_session_lines(lines: Sequence[str]) -> list[str]:
    """Return only the latest stream session, while keeping a marker-less log usable."""
    start: int | None = None
    for index, line in enumerate(lines):
        if any(marker in line for marker in _SESSION_START_MARKERS):
            start = index
    # A marker-less or manually-trimmed log stays fail-closed: retain all of its
    # evidence instead of guessing that an in-session protocol/readiness line is
    # a lifecycle boundary and potentially deleting the fault or negotiation.
    return list(lines[start if start is not None else 0:])


def latest_chiaki_session_lines(lines: Sequence[str]) -> list[str]:
    """Return only loss evidence belonging to Chiaki's latest connection."""
    start = 0
    found = False
    for index, line in enumerate(lines):
        if any(marker in line for marker in _CHIAKI_SESSION_MARKERS):
            start = index
            found = True
    return list(lines[start:]) if found else list(lines)


def parse_evidence(orion_lines: Sequence[str], chiaki_lines: Sequence[str] = ()) -> StreamEvidence:
    evidence = StreamEvidence()
    session_lines = latest_session_lines(orion_lines)
    evidence.named_event_shm = any(
        _NAMED_EVENT_SHM_MARKER in line for line in session_lines)
    for line in session_lines:
        match = _CAPTURE_RE.search(line)
        if match:
            raw = _CAPTURE_RAW_RE.search(line)
            evidence.captures.append(CaptureSample(
                tier=match.group("tier"),
                unique_fps=float(match.group("unique")),
                duplicate_pct=float(match.group("duplicate")),
                export_fps=float(match.group("export")),
                gap_fps=float(match.group("gap")),
                cv_fps=float(match.group("cv")),
                detect_ms=float(match.group("detect")),
                raw_fps=(float(raw.group("raw_fps")) if raw else None),
                raw_gap_max_ms=(float(raw.group("raw_gap_max")) if raw else None),
                raw_late=(int(raw.group("raw_late")) if raw else None),
                source_skip=(int(raw.group("source_skip")) if raw else None),
            ))
            continue
        match = _PREVIEW_RE.search(line)
        if match:
            cadence = _PREVIEW_CADENCE_RE.search(line)
            evidence.previews.append(PreviewSample(
                fps=float(match.group("fps")),
                attempt_fps=float(match.group("attempt")),
                aborted=int(match.group("aborted")),
                coalesced=int(match.group("coalesced")),
                detector_backpressure=int(match.group("backpressure")),
                duplicate=int(match.group("duplicate")),
                dropped_total=int(match.group("dropped")),
                gap_mean_ms=float(match.group("gap_mean")),
                gap_max_ms=float(match.group("gap_max")),
                pace=match.group("pace"),
                transport=match.group("transport"),
                shm_ok=int(match.group("shm_ok")),
                shm_fail=int(match.group("shm_fail")),
                callback_gap_mean_ms=(float(cadence.group("callback_mean"))
                                      if cadence else None),
                callback_gap_max_ms=(float(cadence.group("callback_max"))
                                     if cadence else None),
                shm_write_mean_ms=(float(cadence.group("write_mean"))
                                   if cadence else None),
                shm_write_max_ms=(float(cadence.group("write_max"))
                                  if cadence else None),
                notify_mean_ms=(float(cadence.group("notify_mean"))
                                if cadence else None),
                notify_max_ms=(float(cadence.group("notify_max"))
                               if cadence else None),
                notify_gap_mean_ms=(float(cadence.group("notify_gap_mean"))
                                    if cadence else None),
                notify_gap_max_ms=(float(cadence.group("notify_gap_max"))
                                   if cadence else None),
                notify_mode=(cadence.group("notify_mode") if cadence else None),
                cadence_metrics_complete=cadence is not None,
            ))
            continue
        match = _PIPELINE_SHM_RE.search(line)
        if match:
            cadence = _PIPELINE_SHM_CADENCE_RE.search(line)
            evidence.pipelines.append(PipelineSample(
                transport="shm",
                present_fps=float(match.group("present")),
                open_fail_run=int(match.group("open_fail")),
                read_fault_run=int(match.group("read_fault")),
                source_fps=(float(cadence.group("source")) if cadence else None),
                source_total=(int(cadence.group("source_total")) if cadence else None),
                jitter_drop=(int(cadence.group("jitter_drop")) if cadence else None),
                underflow=(int(cadence.group("underflow")) if cadence else None),
                depth=(int(cadence.group("depth")) if cadence else None),
                max_depth=(int(cadence.group("max_depth")) if cadence else None),
                present_gap_max_ms=(float(cadence.group("present_gap_max"))
                                    if cadence else None),
                event_wait_timeouts=(int(cadence.group("event_wait_timeouts"))
                                     if cadence else None),
                event_wait_failures=(int(cadence.group("event_wait_failures"))
                                     if cadence else None),
                event_generation_probes=(int(cadence.group("event_generation_probes"))
                                         if cadence else None),
                event_notification_losses=(int(cadence.group("event_notification_losses"))
                                           if cadence else None),
                ready_frame_replaced=(int(cadence.group("ready_frame_replaced"))
                                      if cadence else None),
                delivery_schedule_failures=(int(cadence.group("delivery_schedule_failures"))
                                            if cadence else None),
                cadence_metrics_complete=cadence is not None,
            ))
            continue
        match = _SHM_INTEGRITY_RE.search(line)
        if match:
            evidence.shm_integrity.append(ShmIntegritySample(
                event_wait_timeouts=int(match.group("event_wait_timeouts")),
                event_wait_failures=int(match.group("event_wait_failures")),
                event_generation_probes=int(match.group("event_generation_probes")),
                event_notification_losses=int(match.group("event_notification_losses")),
                ready_frame_replaced=int(match.group("ready_frame_replaced")),
                delivery_schedule_failures=int(match.group("delivery_schedule_failures")),
                notification_failure_run=int(match.group("notification_failure_run")),
            ))
            continue
        match = _PIPELINE_JPEG_RE.search(line)
        if match:
            evidence.pipelines.append(PipelineSample(
                transport="jpeg",
                receive_fps=float(match.group("rx")),
                decode_fps=float(match.group("decode")),
                present_fps=float(match.group("present")),
                decode_drop=int(match.group("decode_drop")),
                gui_drop=int(match.group("gui_drop")),
            ))
            continue
        match = _QML_PIPELINE_RE.search(line)
        if match:
            evidence.qml.append(QmlSample(
                set_fps=float(match.group("set")),
                request_fps=float(match.group("request")),
                request_gap_max_ms=float(match.group("gap_max")),
                frames_set=int(match.group("sets")),
                requests=int(match.group("requests")),
                asynchronous=match.group("async") == "1",
                snapshot_misses=(int(match.group("snapshot_miss"))
                                 if match.group("snapshot_miss") is not None else None),
                ready_ack_fps=(float(match.group("ready_ack"))
                               if match.group("ready_ack") is not None else None),
                ready_ack_total=(int(match.group("ready_total"))
                                 if match.group("ready_total") is not None else None),
                stale_acks=(int(match.group("stale_ack"))
                            if match.group("stale_ack") is not None else None),
            ))

    joined_loss_lines = (
        list(latest_session_lines(orion_lines))
        + latest_chiaki_session_lines(chiaki_lines)
    )
    evidence.network_loss_events = sum(
        1 for line in joined_loss_lines
        if any(pattern.search(line) for pattern in _NETWORK_LOSS_PATTERNS)
    )
    return evidence


def assess(evidence: StreamEvidence, minimum_windows: int = 3) -> StreamAssessment:
    captures = evidence.captures
    previews = evidence.previews
    pipelines = evidence.pipelines
    qml = evidence.qml
    source = "unknown"
    if captures:
        tier = captures[-1].tier.lower()
        source = "capture_card" if tier in {"capture_card", "capturecard", "card"} else "remote_play"

    unique_fps = _median(sample.unique_fps for sample in captures)
    raw_capture_samples = [
        sample for sample in captures
        if sample.raw_fps is not None and sample.raw_fps > 0.0
    ]
    raw_source_fps = _median(sample.raw_fps for sample in raw_capture_samples)
    # Prefer the cadence sampled immediately after the backend's verified frame
    # publication. ``unique_fps`` is downstream of Orion's capture consumer and
    # can fall when that consumer stalls even though the physical/decoded source
    # remained healthy -- exactly the distinction this gate must preserve.
    source_fps = raw_source_fps if raw_source_fps is not None else unique_fps
    preview_fps = _median(sample.fps for sample in previews)
    active_transport = previews[-1].transport if previews else ""
    active_pipelines = [
        sample for sample in pipelines if sample.transport == active_transport
    ]
    present_fps = _median(sample.present_fps for sample in active_pipelines)
    ready_ack_values = [
        sample.ready_ack_fps for sample in qml if sample.ready_ack_fps is not None
    ]
    qml_fps = (_median(ready_ack_values) if ready_ack_values
               else _median(sample.request_fps for sample in qml))
    reasons: list[str] = []
    source_degradation_reasons: list[str] = []

    if (len(captures) < minimum_windows
            or len(previews) < minimum_windows
            or len(active_pipelines) < minimum_windows
            or len(qml) < minimum_windows):
        reasons.append(
            f"need at least {minimum_windows} capture, preview, active native-pipeline, "
            f"and QML windows (got {len(captures)}/{len(previews)}/"
            f"{len(active_pipelines)}/{len(qml)})"
        )
        return StreamAssessment(
            "insufficient_evidence", source, tuple(reasons), source_fps, preview_fps,
            present_fps, qml_fps)

    incomplete_shm_evidence = (
        any(sample.transport == "shm" and not sample.cadence_metrics_complete
            for sample in previews)
        or any(sample.transport == "shm" and not sample.cadence_metrics_complete
               for sample in active_pipelines)
    )
    if incomplete_shm_evidence and not evidence.named_event_shm:
        return StreamAssessment(
            "insufficient_evidence", source,
            ("legacy/incomplete SHM telemetry cannot prove the release cadence",),
            source_fps, preview_fps, present_fps, qml_fps)

    if raw_capture_samples:
        if any((sample.raw_fps or 0.0) < 55.0 for sample in raw_capture_samples):
            source_degradation_reasons.append(
                "raw source delivery recorded a window below the 55 FPS release floor")
        if any((sample.raw_gap_max_ms or 0.0) > 25.0
               for sample in raw_capture_samples):
            source_degradation_reasons.append(
                "raw source delivery recorded a missed-refresh gap above 25 ms")
        if any((sample.raw_late or 0) > 0 for sample in raw_capture_samples):
            source_degradation_reasons.append(
                "raw source delivery recorded late frame gaps")
    else:
        if source_fps is None or source_fps < 55.0:
            source_degradation_reasons.append(
                "source cadence was below the 55 FPS release floor")

    if captures and _median(sample.duplicate_pct for sample in captures) > 5.0:
        source_degradation_reasons.append(
            "source frames exceeded the duplicate-frame release ceiling")

    if source == "remote_play":
        export_values = [sample.export_fps for sample in captures if sample.export_fps > 0.0]
        if export_values and statistics.median(export_values) < 55.0:
            source_degradation_reasons.append(
                "Remote Play frame export fell below the 55 FPS release floor")
        if _median(sample.gap_fps for sample in captures) > 1.0:
            source_degradation_reasons.append(
                "Remote Play frame export reported source gaps")
    source_degraded = bool(source_degradation_reasons)

    # ``source_skip`` is observed after the backend's latest-frame ring.  It
    # proves Orion's capture consumer stepped over one or more published source
    # generations, even if raw delivery was also degraded; it is not evidence
    # against the user's cable, card, or network.  The counter starts with each
    # sidecar process, so any positive value in the selected generation matters.
    if (raw_capture_samples
            and any((sample.source_skip or 0) > 0 for sample in captures)):
        reasons.append("Orion capture ingest skipped source sequence frames")
    if (raw_source_fps is not None and unique_fps is not None
            and not source_degraded and unique_fps < raw_source_fps - 2.0):
        reasons.append(
            f"healthy {raw_source_fps:.1f} FPS raw source fell to "
            f"{unique_fps:.1f} FPS inside Orion capture ingest")

    # Production SHM must follow capture/decoder events. A second pace grid is a
    # known two-clock alias that creates 32 ms hitches even with a healthy source.
    if any(sample.transport == "shm" and sample.pace != "source" for sample in previews):
        reasons.append("SHM preview used a second pacing grid instead of source cadence")

    # A negotiated named-event session is a new protocol, not a legacy log. Its
    # split producer/commit/write/notification and native presentation fields are
    # mandatory release evidence. Treating them as optional made a truncated or
    # partially-upgraded log indistinguishable from a healthy run.
    if evidence.named_event_shm:
        if any(sample.transport == "shm" and not sample.cadence_metrics_complete
               for sample in previews):
            reasons.append("named-event SHM preview telemetry omitted required cadence fields")
        if any(sample.transport == "shm" and not sample.cadence_metrics_complete
               for sample in active_pipelines):
            reasons.append("named-event native pipeline telemetry omitted required cadence fields")
    if evidence.shm_integrity:
        reasons.append("SHM ready-event integrity failure forced a preview transport transition")

    if preview_fps is not None and source_fps is not None:
        if source_fps >= 55.0 and preview_fps < source_fps - 2.0:
            reasons.append(
                f"healthy {source_fps:.1f} FPS source fell to {preview_fps:.1f} FPS inside Orion")

    # One 60 Hz frame is 16.7 ms. Above 25 ms means at least one refresh missed;
    # do not average that hitch away. The report intentionally stays strict.
    # A >25 ms preview gap is an Orion miss only when the producer itself is
    # proven healthy.  With a slow producer, the same observed gap may have
    # originated before Orion and this log has no source-gap histogram with
    # which to prove otherwise.
    if not source_degraded and any(sample.gap_max_ms > 25.0 for sample in previews):
        reasons.append("SHM commit cadence recorded a missed-refresh gap above 25 ms")
    if not source_degraded and any(
            sample.callback_gap_max_ms is not None
            and sample.callback_gap_max_ms > 25.0 for sample in previews):
        reasons.append("preview callback handoff recorded a missed-refresh gap above 25 ms")
    if any(sample.shm_write_max_ms is not None
           and sample.shm_write_max_ms > 25.0 for sample in previews):
        reasons.append("SHM mapping write exceeded the 25 ms missed-refresh budget")
    if any(sample.notify_max_ms is not None
           and sample.notify_max_ms > 25.0 for sample in previews):
        reasons.append("preview consumer notification exceeded the 25 ms missed-refresh budget")
    if not source_degraded and any(
            sample.notify_gap_max_ms is not None
            and sample.notify_gap_max_ms > 25.0 for sample in previews):
        reasons.append("preview consumer notification cadence recorded a missed-refresh gap above 25 ms")
    if any(sample.transport == "shm" and sample.notify_mode is not None
           and sample.notify_mode != "event" for sample in previews):
        reasons.append("SHM preview fell back to per-frame stdout notification")
    if any(sample.coalesced > 0 for sample in previews):
        reasons.append("preview latest-wins mailbox coalesced source frames")
    if any(sample.detector_backpressure > 0 for sample in previews):
        reasons.append("preview dropped frames behind detector backpressure")
    if any(sample.aborted > 0 for sample in previews):
        reasons.append("preview transport aborted frame attempts")
    if previews and (previews[-1].dropped_total > 0 or previews[-1].shm_fail > 0):
        reasons.append("preview transport reported a real frame/write fault")

    if active_pipelines:
        active_present = _median(sample.present_fps for sample in active_pipelines)
        if active_present is not None and source_fps is not None and source_fps >= 55.0:
            if active_present < source_fps - 2.0:
                reasons.append(
                    f"native presentation fell to {active_present:.1f} FPS after a healthy source")
        if any(sample.open_fail_run or sample.read_fault_run
               or sample.decode_drop or sample.gui_drop for sample in active_pipelines):
            reasons.append("native preview pipeline reported read/decode/presentation faults")
        if any((sample.jitter_drop or 0) > 0 for sample in active_pipelines):
            reasons.append("native presentation buffer dropped a frame to recover cadence")
        if _counter_advanced(active_pipelines, "event_wait_failures"):
            reasons.append("native SHM ready-event wait failed")
        if _counter_advanced(active_pipelines, "event_notification_losses"):
            reasons.append("native SHM ready-event notification was lost")
        if _counter_advanced(active_pipelines, "ready_frame_replaced"):
            reasons.append("native SHM pump replaced an undelivered preview frame")
        if _counter_advanced(active_pipelines, "delivery_schedule_failures"):
            reasons.append("native SHM pump could not schedule preview delivery")
        if any((sample.underflow or 0) > 0 for sample in active_pipelines):
            reasons.append("native presentation buffer underflowed")
        if any(sample.present_gap_max_ms is not None
               and sample.present_gap_max_ms > 25.0 for sample in active_pipelines):
            reasons.append("native presentation cadence recorded a missed-refresh gap above 25 ms")
        cumulative_fields = (
            "event_wait_failures",
            "event_notification_losses",
            "ready_frame_replaced",
            "delivery_schedule_failures",
        )
        if any(_counter_regressed(active_pipelines, field)
               for field in cumulative_fields):
            reasons.append(
                "native SHM cumulative telemetry regressed inside the selected session")

    qml_set_fps = _median(sample.set_fps for sample in qml)
    qml_request_fps = _median(sample.request_fps for sample in qml)
    if any(not sample.asynchronous for sample in qml):
        reasons.append("QML preview used the synchronous GUI-thread image-provider path")
    if any(sample.requests <= 0 for sample in qml):
        reasons.append("QML preview recorded a window with no image-provider fetches")
    if qml_set_fps is not None and qml_request_fps is not None:
        if qml_set_fps >= 5.0 and qml_request_fps < qml_set_fps - 2.0:
            reasons.append(
                f"QML fetched only {qml_request_fps:.1f} FPS from a "
                f"{qml_set_fps:.1f} FPS native handoff")
    if ready_ack_values and qml_set_fps is not None and qml_fps is not None:
        if qml_set_fps >= 5.0 and qml_fps < qml_set_fps - 2.0:
            reasons.append(
                f"QML visibly presented only {qml_fps:.1f} FPS from a "
                f"{qml_set_fps:.1f} FPS native handoff")
        if any((sample.snapshot_misses or 0) > 0 for sample in qml):
            reasons.append("QML requested an evicted or mismatched immutable frame snapshot")
        if any((sample.stale_acks or 0) > 0 for sample in qml):
            reasons.append("QML completed stale/canceled texture requests")
    # As with sidecar transport gaps, a QML gap can only be assigned to Orion
    # when the producer was healthy. A fetch-rate loss relative to set_fps above
    # is independently attributable even for a slow source.
    if not source_degraded and any(sample.request_gap_max_ms > 25.0 for sample in qml):
        reasons.append("QML image-provider cadence recorded a missed-refresh gap above 25 ms")

    # Definite Orion-stage faults remain release blockers even when the producer
    # is also slow.  Returning the source verdict first used to hide a second
    # pacing grid, mailbox drops, or native presentation loss behind a degraded
    # Remote Play source.
    if reasons:
        if source_degraded:
            reasons.extend(source_degradation_reasons)
            if source == "remote_play" and evidence.network_loss_events > 0:
                reasons.append(
                    "Remote Play source was also degraded with current-session Chiaki loss/corruption evidence")
            else:
                reasons.append(
                    "source delivery was also degraded without proof of its external cause")
        return StreamAssessment(
            "orion_jitter", source, tuple(dict.fromkeys(reasons)),
            source_fps, preview_fps, present_fps, qml_fps)

    if source_degraded:
        if source == "remote_play" and evidence.network_loss_events > 0:
            # This is attribution, not a release pass: the clean-network Orion path
            # still needs a separate healthy run.
            return StreamAssessment(
                "source_limited_network_evidence", source,
                tuple(dict.fromkeys([
                    *source_degradation_reasons,
                    "Remote Play source was degraded and Chiaki recorded loss/corruption",
                ])),
                source_fps, preview_fps, present_fps, qml_fps)
        return StreamAssessment(
            "source_degraded_unattributed", source,
            tuple(dict.fromkeys([
                *source_degradation_reasons,
                "source cadence was degraded without proof that the user's connection caused it",
            ])),
            source_fps, preview_fps, present_fps, qml_fps)

    return StreamAssessment("pass", source, (), source_fps, preview_fps, present_fps, qml_fps)


def _read_lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orion live stream smoothness release gate")
    parser.add_argument("--orion-log", type=Path, default=Path("logs/orion_native.log"))
    parser.add_argument("--chiaki-log", type=Path)
    parser.add_argument("--minimum-windows", type=int, default=3)
    args = parser.parse_args(argv)

    if not args.orion_log.exists():
        print(f"BLOCKED: Orion log not found: {args.orion_log}")
        return 2
    evidence = parse_evidence(_read_lines(args.orion_log), _read_lines(args.chiaki_log))
    result = assess(evidence, max(1, args.minimum_windows))
    active_transport = evidence.previews[-1].transport if evidence.previews else ""
    active_pipeline_windows = sum(
        sample.transport == active_transport for sample in evidence.pipelines)
    print(
        f"{result.verdict.upper()}: source={result.source} "
        f"source_fps={_fmt(result.source_fps)} preview_fps={_fmt(result.preview_fps)} "
        f"present_fps={_fmt(result.present_fps)} qml_fps={_fmt(result.qml_fps)} "
        f"windows={len(evidence.captures)}/{len(evidence.previews)}/"
        f"{active_pipeline_windows}/{len(evidence.qml)}"
    )
    for reason in result.reasons:
        print(f"- {reason}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
