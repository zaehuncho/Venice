from tools.timing.stream_smoothness_report import assess, parse_evidence


def _capture(unique=60, duplicate=0, tier="capture_card", export=0, gap=0,
             raw_fps=None, raw_gap_max=0, raw_late=0, source_skip=0):
    line = (
        "Capture health: tier=%s tiers={} uniqfps=%s dup%%=%s "
        "export_fps=%s gap_fps=%s cv_fps=%s detect_ms=1.0"
        % (tier, unique, duplicate, export, gap, unique)
    )
    if raw_fps is not None:
        line += (
            " raw_fps=%s raw_gap_max_ms=%s raw_late=%s source_skip=%s"
            % (raw_fps, raw_gap_max, raw_late, source_skip)
        )
    return line


def _preview(fps=60, gap_max=18, pace="source", coalesced=0, backpressure=0,
             callback_gap_max=18, write_max=1, notify_max=1,
             notify_gap_max=18, notify_mode="event", metrics=True):
    line = (
        "preview_stats: fps=%s attempt_fps=%s aborted=0 coalesced=%s "
        "detector_backpressure=%s dup=0 dropped=0 avg_kib=0.0 "
        "encode_ms mean=0.00 max=0.00 gap_ms mean=16.7 max=%s "
        "pace=%s gate=0.85 transport=shm shm_ok=300 shm_fail=0"
        % (fps, fps, coalesced, backpressure, gap_max, pace)
    )
    if metrics:
        line += (
            " callback_gap_ms mean=16.7 max=%s "
            "shm_write_ms mean=0.50 max=%s "
            "notify_ms mean=0.010 max=%s "
            "notify_gap_ms mean=16.7 max=%s notify_mode=%s"
            % (callback_gap_max, write_max, notify_max,
               notify_gap_max, notify_mode)
        )
    return line


def _pipeline(fps=60, *, metrics=True, source_fps=60, jitter_drop=0,
              underflow=0, present_gap_max=18, event_wait_failures=0,
              event_notification_losses=0, ready_frame_replaced=0,
              delivery_schedule_failures=0):
    line = (
        "preview_pipeline: transport=shm present_fps=%s total=300 "
        "open_fail_run=0 read_fault_run=0" % fps
    )
    if metrics:
        line += (
            " source_fps=%s source_total=300 jitter_drop=%s underflow=%s "
            "depth=3 max_depth=4 present_gap_max_ms=%s "
            "event_wait_timeouts=0 event_wait_failures=%s "
            "event_generation_probes=0 event_notification_losses=%s "
            "ready_frame_replaced=%s delivery_schedule_failures=%s"
            % (source_fps, jitter_drop, underflow, present_gap_max,
               event_wait_failures, event_notification_losses,
               ready_frame_replaced, delivery_schedule_failures)
        )
    return line


def _qml(set_fps=60, request_fps=60, gap_max=18, asynchronous=1,
         ready_ack=60, snapshot_miss=0, stale_ack=0):
    line = (
        "qml_preview_pipeline: set_fps=%s request_fps=%s "
        "request_gap_max_ms=%s sets=300 requests=300 async=%s"
        % (set_fps, request_fps, gap_max, asynchronous)
    )
    if ready_ack is not None:
        line += (
            " snapshot_miss=%s ready_ack_fps=%s ready_ack_total=900 "
            "stale_ack=%s" % (snapshot_miss, ready_ack, stale_ack)
        )
    return line


def test_healthy_source_cadence_passes():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    result = assess(parse_evidence(lines))
    assert result.verdict == "pass"
    assert result.passed


def test_fully_populated_named_event_windows_pass():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    assert assess(parse_evidence(lines)).verdict == "pass"


def test_old_second_grid_is_orion_jitter_even_with_healthy_card():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(55, 32, "on", coalesced=20),
            _pipeline(55), _qml(55, 55, 32),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("second pacing grid" in reason for reason in result.reasons)
    assert any("healthy 60.0 FPS source" in reason for reason in result.reasons)


def test_detector_priority_drop_is_not_hidden_as_connection_jitter():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(59, 47, backpressure=2),
            _pipeline(59), _qml(59, 59, 47),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("detector backpressure" in reason for reason in result.reasons)


def test_split_cadence_identifies_callback_handoff_hitch():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(gap_max=18, callback_gap_max=33),
            _pipeline(), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("callback handoff" in reason for reason in result.reasons)
    assert not any("SHM commit cadence" in reason for reason in result.reasons)


def test_split_cadence_identifies_notification_hitch_after_clean_commit():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(gap_max=18, notify_gap_max=34),
            _pipeline(), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("notification cadence" in reason for reason in result.reasons)
    assert not any("SHM commit cadence" in reason for reason in result.reasons)


def test_slow_mapping_write_and_stdout_event_fallback_are_release_faults():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(),
            _preview(gap_max=18, write_max=28, notify_mode="stdout"),
            _pipeline(), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("mapping write" in reason for reason in result.reasons)
    assert any("stdout notification" in reason for reason in result.reasons)


def test_legacy_preview_stats_remain_parseable():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(metrics=False), _pipeline(metrics=False), _qml(),
        ))
    evidence = parse_evidence(lines)
    assert len(evidence.previews) == 3
    assert evidence.previews[-1].notify_mode is None
    result = assess(evidence)
    assert result.verdict == "insufficient_evidence"
    assert any("legacy/incomplete SHM telemetry" in reason for reason in result.reasons)


def test_named_event_shm_missing_split_metrics_fails_closed():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for _ in range(3):
        lines.extend((
            _capture(), _preview(metrics=False), _pipeline(metrics=False), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("preview telemetry omitted" in reason for reason in result.reasons)
    assert any("native pipeline telemetry omitted" in reason for reason in result.reasons)


def test_native_underflow_and_long_present_gap_cannot_false_pass():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for _ in range(3):
        lines.extend((
            _capture(), _preview(),
            _pipeline(underflow=1, present_gap_max=250), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("underflowed" in reason for reason in result.reasons)
    assert any("native presentation cadence" in reason for reason in result.reasons)


def test_native_jitter_drop_is_a_release_fault_even_when_average_fps_is_healthy():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for _ in range(3):
        lines.extend((
            _capture(), _preview(), _pipeline(jitter_drop=1), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("dropped a frame" in reason for reason in result.reasons)


def test_native_event_and_delivery_fault_counters_cannot_false_pass():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    counters = (0, 1, 1)
    for value in counters:
        lines.extend((
            _capture(), _preview(),
            _pipeline(event_wait_failures=value, event_notification_losses=value,
                      ready_frame_replaced=value,
                      delivery_schedule_failures=value),
            _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("ready-event wait failed" in reason for reason in result.reasons)
    assert any("notification was lost" in reason for reason in result.reasons)
    assert any("replaced an undelivered" in reason for reason in result.reasons)
    assert any("could not schedule" in reason for reason in result.reasons)


def test_inherited_native_fault_counter_baseline_is_not_recounted_each_window():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for _ in range(3):
        lines.extend((
            _capture(), _preview(),
            _pipeline(event_wait_failures=7, event_notification_losses=4,
                      ready_frame_replaced=11,
                      delivery_schedule_failures=2),
            _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "pass"


def test_cumulative_native_counter_regression_fails_closed():
    lines = ["Preview transport: shared memory active (named event; JPEG fallback armed)"]
    for value in (7, 3, 3):
        lines.extend((
            _capture(), _preview(), _pipeline(ready_frame_replaced=value), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("cumulative telemetry regressed" in reason
               for reason in result.reasons)


def test_integrity_transition_line_blocks_a_post_fallback_false_pass():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    lines.append(
        "preview_shm_integrity: event_wait_timeouts=2 event_wait_failures=0 "
        "event_generation_probes=1 event_notification_losses=1 "
        "ready_frame_replaced=0 delivery_schedule_failures=0 "
        "notification_failure_run=1"
    )
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("integrity failure" in reason for reason in result.reasons)


def test_remote_play_network_attribution_requires_explicit_loss_evidence():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(42, 0, tier="decoder", export=42, gap=18),
            _preview(42, 40),
            _pipeline(42),
            _qml(42, 42, 40),
        ))
    no_proof = assess(parse_evidence(lines))
    assert no_proof.verdict == "source_degraded_unattributed"

    with_proof = assess(parse_evidence(
        lines, ["[decode] arrival frames_lost=3 recovered=1 samples=42"]))
    assert with_proof.verdict == "source_limited_network_evidence"


def test_raw_capture_cadence_is_parsed_and_drives_source_attribution():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(raw_fps=60, raw_gap_max=31, raw_late=2),
            _preview(), _pipeline(), _qml(),
        ))
    evidence = parse_evidence(lines)
    assert evidence.captures[-1].raw_fps == 60
    assert evidence.captures[-1].raw_gap_max_ms == 31
    assert evidence.captures[-1].raw_late == 2
    assert evidence.captures[-1].source_skip == 0

    result = assess(evidence)
    assert result.verdict == "source_degraded_unattributed"
    assert result.source_fps == 60
    assert any("raw source delivery" in reason for reason in result.reasons)
    assert any("without proof" in reason for reason in result.reasons)


def test_healthy_raw_source_exposes_orion_capture_ingest_loss():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(unique=42, raw_fps=60, raw_gap_max=18,
                     raw_late=0, source_skip=5),
            _preview(42), _pipeline(42), _qml(42, 42),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert result.source_fps == 60
    assert any("capture ingest skipped" in reason for reason in result.reasons)
    assert any("inside Orion capture ingest" in reason for reason in result.reasons)


def test_source_gap_cannot_hide_a_downstream_orion_sequence_skip():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(raw_fps=57, raw_gap_max=31, raw_late=2, source_skip=3),
            _preview(), _pipeline(), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("capture ingest skipped" in reason for reason in result.reasons)
    assert any("raw source delivery" in reason for reason in result.reasons)


def test_raw_warmup_zero_does_not_make_a_healthy_source_look_stopped():
    lines = []
    for raw_fps in (0, 60, 60):
        lines.extend((
            _capture(raw_fps=raw_fps, raw_gap_max=18),
            _preview(), _pipeline(), _qml(),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "pass"
    assert result.source_fps == 60


def test_stale_chiaki_loss_from_an_old_session_cannot_blame_current_source():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(42, 0, tier="decoder", export=42, gap=18),
            _preview(42, 40),
            _pipeline(42),
            _qml(42, 42, 40),
        ))
    chiaki = [
        ">> Started session",
        "[decode] frames_lost=7 samples=42",
        "Session has quit",
        ">> Started session",
        "[decode] frames_lost=0 samples=60",
    ]
    result = assess(parse_evidence(lines, chiaki))
    assert result.verdict == "source_degraded_unattributed"


def test_orion_fault_is_not_hidden_by_a_degraded_remote_play_source():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(42, 0, tier="decoder", export=42, gap=18),
            _preview(30, 48, coalesced=4),
            _pipeline(30),
            _qml(30, 30, 48),
        ))
    result = assess(parse_evidence(lines, [
        ">> Started session", "[decode] frames_lost=3 samples=42",
    ]))
    assert result.verdict == "orion_jitter"
    assert any("coalesced" in reason for reason in result.reasons)
    assert any("current-session Chiaki" in reason for reason in result.reasons)


def test_sidecar_only_cadence_cannot_pass_without_native_pipeline_windows():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview()))
    result = assess(parse_evidence(lines))
    assert result.verdict == "insufficient_evidence"
    assert result.present_fps is None


def test_pipeline_windows_must_match_the_active_transport():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    # The latest preview transport switched to JPEG, but only stale SHM pipeline
    # evidence exists.  That is not an end-to-end pass.
    lines[-2] = lines[-2].replace("transport=shm", "transport=jpeg")
    result = assess(parse_evidence(lines))
    assert result.verdict == "insufficient_evidence"


def test_latest_session_discards_stale_bad_windows():
    stale = [_capture(), _preview(54, 63, "on", coalesced=30)] * 3
    fresh = ["Sidecar job assigned: orphan cleanup armed."]
    for _ in range(3):
        fresh.extend((_capture(), _preview(), _pipeline(), _qml()))
    result = assess(parse_evidence(stale + fresh))
    assert result.verdict == "pass"


def test_in_session_readiness_markers_do_not_hide_named_event_negotiation():
    lines = [
        "Sidecar job assigned: orphan cleanup armed.",
        "Preview transport: shared memory active (named event; JPEG fallback armed)",
        "SHM preview reader opened.",
        "Remote Play: Running - Autogreen running - meter detection active",
    ]
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    evidence = parse_evidence(lines)
    assert evidence.named_event_shm
    assert assess(evidence).verdict == "pass"


def test_trimmed_log_keeps_protocol_marker_instead_of_guessing_session_start():
    lines = [
        "Preview transport: shared memory active (named event; JPEG fallback armed)",
        "SHM preview reader opened.",
    ]
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml()))
    assert parse_evidence(lines).named_event_shm


def test_short_run_is_never_reported_as_pass():
    result = assess(parse_evidence([_capture(), _preview()]))
    assert result.verdict == "insufficient_evidence"


def test_healthy_upstream_to_slow_qml_is_orion_jitter():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml(60, 53, 35)))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("QML fetched only" in reason for reason in result.reasons)
    assert any("QML image-provider cadence" in reason for reason in result.reasons)


def test_provider_requests_cannot_hide_slow_visible_texture_completion():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(), _pipeline(),
            _qml(60, 60, 18, ready_ack=42, stale_ack=75),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert result.qml_fps == 42
    assert any("visibly presented only" in reason for reason in result.reasons)
    assert any("stale/canceled" in reason for reason in result.reasons)


def test_missing_immutable_snapshot_is_a_release_blocker():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(), _preview(), _pipeline(),
            _qml(snapshot_miss=1),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("immutable frame snapshot" in reason for reason in result.reasons)


def test_synchronous_qml_path_is_not_release_approved():
    lines = []
    for _ in range(3):
        lines.extend((_capture(), _preview(), _pipeline(), _qml(asynchronous=0)))
    result = assess(parse_evidence(lines))
    assert result.verdict == "orion_jitter"
    assert any("synchronous GUI-thread" in reason for reason in result.reasons)


def test_qml_gap_is_not_assigned_to_orion_when_source_is_slow_but_handoff_matches():
    lines = []
    for _ in range(3):
        lines.extend((
            _capture(42, tier="decoder", export=42, gap=18),
            _preview(42, 40), _pipeline(42), _qml(42, 42, 40),
        ))
    result = assess(parse_evidence(lines))
    assert result.verdict == "source_degraded_unattributed"
