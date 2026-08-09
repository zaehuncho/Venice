"""RTT sync engine: live decode comp, predictive offset, hold-cadence ping."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtt_sync_engine import RTTSyncConfig, RTTSyncEngine, CourtIPDetector


def _lock_engine_court(eng, court_ip="45.79.123.45"):
    ts = 5_000_000.0
    for _ in range(150):
        eng.observe_packet("192.168.137.117", court_ip, 51000, 30005,
                           1200, ts)
        ts += 1000.0 / 30.0
    assert eng._current_court_ip == court_ip
    return court_ip


def _engine_with_samples(rtts):
    cfg = RTTSyncConfig(ping_enabled=False)
    eng = RTTSyncEngine(cfg)
    _lock_engine_court(eng)
    for rtt in rtts:
        eng._process_rtt_sample(rtt)
    return eng


def test_decode_comp_uses_measured_frame_age_ema():
    eng = _engine_with_samples([20.0] * 6)
    # No frame-age samples yet: falls back to the fixed config constant.
    snap = eng.get_snapshot()
    assert snap.decode_comp_ms == eng._config.decode_latency_comp_ms
    # Feed a stable measured staleness; the EMA must replace the constant.
    for _ in range(20):
        eng.observe_frame_age_ms(12.0)
    snap = eng.get_snapshot()
    assert abs(snap.decode_comp_ms - 12.0) < 0.5
    # An explicit caller-measured value still wins.
    snap = eng.get_snapshot(decode_latency_ms=3.0)
    assert snap.decode_comp_ms == 3.0


def test_frame_age_rejects_garbage():
    eng = _engine_with_samples([20.0] * 6)
    eng.observe_frame_age_ms(-5.0)
    eng.observe_frame_age_ms(10000.0)
    eng.observe_frame_age_ms("nan-ish")
    assert eng._frame_age_ema_ms == 0.0


def test_predicted_offset_tracks_rising_rtt():
    # A steadily rising RTT: the forward-looking offset must lead the current
    # effective offset (predicted half-RTT > current half-RTT).
    eng = _engine_with_samples([20.0 + i * 2.0 for i in range(12)])
    snap = eng.get_snapshot()
    assert snap.ready
    assert snap.predicted_offset_ms > 0.0
    assert snap.predicted_rtt_ms > snap.rtt_filtered_ms
    # On a flat RTT the two offsets agree closely.
    flat = _engine_with_samples([20.0] * 12)
    fsnap = flat.get_snapshot()
    assert abs(fsnap.predicted_offset_ms - fsnap.effective_offset_ms) < 1.5


def test_meter_active_tightens_ping_window():
    cfg = RTTSyncConfig(ping_enabled=False, ping_interval_ms=400.0,
                        ping_interval_hold_ms=150.0)
    eng = RTTSyncEngine(cfg)
    assert eng._meter_active_ms == 0.0
    eng.notify_meter_active()
    now_ms = time.monotonic() * 1000.0
    assert now_ms - eng._meter_active_ms < 100.0


def test_snapshot_has_no_qos_fields():
    eng = _engine_with_samples([20.0] * 6)
    snap = eng.get_snapshot()
    assert not hasattr(snap, "qos_status")
    assert not hasattr(snap, "qos_marking_enabled")


def test_local_seed_rtt_is_diagnostic_only():
    eng = RTTSyncEngine(RTTSyncConfig(ping_enabled=False))
    assert eng.set_ping_target("192.168.137.1")
    generation = eng._target_generation
    for _ in range(8):
        assert eng._process_rtt_sample(
            1.0, expected_target="192.168.137.1",
            expected_generation=generation)

    snap = eng.get_snapshot()
    assert snap.rtt_filtered_ms > 0.0
    assert not snap.target_verified
    assert not snap.ready
    assert snap.rtt_half_ms == 0.0
    assert snap.effective_offset_ms == 0.0
    assert snap.predicted_offset_ms == 0.0


def test_court_retarget_resets_seed_and_discards_inflight_old_sample():
    eng = RTTSyncEngine(RTTSyncConfig(ping_enabled=False))
    assert eng.set_ping_target("192.168.137.1")
    old_generation = eng._target_generation
    for _ in range(8):
        eng._process_rtt_sample(1.0)
    assert eng._sample_count == 8

    _lock_engine_court(eng)
    assert eng._sample_count == 0
    assert eng.get_snapshot().rtt_filtered_ms == 0.0
    assert not eng._process_rtt_sample(
        1.0, expected_target="192.168.137.1",
        expected_generation=old_generation)
    assert eng._sample_count == 0

    court_generation = eng._target_generation
    for _ in range(6):
        assert eng._process_rtt_sample(
            40.0, expected_target="45.79.123.45",
            expected_generation=court_generation)
    snap = eng.get_snapshot()
    assert snap.target_verified
    assert snap.ready
    assert snap.rtt_filtered_ms > 30.0


def test_private_address_cannot_be_declared_a_court_target():
    eng = RTTSyncEngine(RTTSyncConfig(ping_enabled=False))
    assert not eng.set_target_ip("192.168.137.1")
    assert eng._current_court_ip == ""


def test_unrelated_public_service_packets_never_create_tick_eta():
    eng = RTTSyncEngine(RTTSyncConfig(ping_enabled=False))
    ts = 1_000_000.0
    for _ in range(200):
        eng.observe_packet("192.168.137.117", "8.8.8.8",
                           51000, 3478, 500, ts)
        ts += 16.0
    snap = eng.get_snapshot()
    assert eng._current_court_ip == ""
    assert snap.packet_interval_ms == 0.0
    assert snap.next_tick_eta_ms < 0.0
    assert not snap.phase_source_verified


def test_public_candidate_without_packet_evidence_is_not_verified():
    eng = RTTSyncEngine(RTTSyncConfig(ping_enabled=False))
    assert eng.set_target_ip("45.79.123.45")
    for _ in range(8):
        eng._process_rtt_sample(40.0)
    snap = eng.get_snapshot()
    assert not snap.target_verified
    assert not snap.ready
    assert snap.effective_offset_ms == 0.0


def test_retarget_cannot_inherit_prior_court_packet_evidence():
    eng = _engine_with_samples([40.0] * 8)
    assert eng.get_snapshot().ready

    assert eng.set_target_ip("45.79.5.5")
    generation = eng._target_generation
    for _ in range(8):
        assert eng._process_rtt_sample(
            25.0, expected_target="45.79.5.5",
            expected_generation=generation)

    snap = eng.get_snapshot()
    assert not snap.target_verified
    assert not snap.ready
    assert snap.court_evidence_age_ms < 0.0

    eng.observe_packet("192.168.137.117", "45.79.5.5",
                       51000, 30005, 1200, 6_000_000.0)
    assert eng.get_snapshot().ready


def test_court_authority_expires_and_clear_allows_fresh_lock():
    cfg = RTTSyncConfig(ping_enabled=False, court_evidence_ttl_ms=1.0)
    eng = RTTSyncEngine(cfg)
    _lock_engine_court(eng, "45.79.123.45")
    for _ in range(6):
        eng._process_rtt_sample(40.0)
    assert eng.get_snapshot().ready
    # Windows monotonic clocks can advance in ~15.6 ms quanta on some hosts.
    time.sleep(0.025)
    assert not eng.get_snapshot().ready

    old_generation = eng._target_generation
    eng.clear_court_target()
    assert eng._target_generation > old_generation
    assert eng._current_court_ip == ""
    assert eng._sample_count == 0
    _lock_engine_court(eng, "45.79.5.5")
    assert eng._current_court_ip == "45.79.5.5"


def test_rejected_outliers_do_not_refresh_rtt_authority():
    cfg = RTTSyncConfig(ping_enabled=False, rtt_sample_ttl_ms=1.0,
                        court_evidence_ttl_ms=2500.0)
    eng = RTTSyncEngine(cfg)
    _lock_engine_court(eng)
    for _ in range(8):
        assert eng._process_rtt_sample(40.0)
    assert eng.get_snapshot().ready

    time.sleep(0.025)
    accepted_sample_ms = eng._last_sample_ms
    assert not eng._process_rtt_sample(999.0)
    assert eng._last_sample_ms == accepted_sample_ms
    assert not eng.get_snapshot().ready


def _feed_court_flow(det, court_ip, pps, secs, *, src_port=51000, dst_port=30005,
                     ts_base=5_000_000.0):
    """Feed a steady one-way public-peer UDP flow at the given rate. ts_base is a
    large value to mimic the nexus_svc capture clock (perf_counter epoch), which
    differs from this process's clock — exercising the cross-process staleness fix."""
    ps5 = "192.168.137.117"  # private console (ICS subnet)
    t = ts_base
    dt = 1000.0 / pps
    for _ in range(int(pps * secs)):
        det.observe_packet(ps5, court_ip, src_port, dst_port, 1200, t)
        t += dt


def test_court_ip_locks_at_realistic_2k_rate():
    # A real 2K court flow runs ~24-60 pps. With the old /1000 confidence
    # normaliser the lock never fired below ~90 pps unless the IP fell in the
    # hard-coded known ranges; this guards the rate-appropriate normaliser.
    cfg = RTTSyncConfig()
    det = CourtIPDetector(cfg)
    # 45.79.x is globally routable and NOT in CourtIPDetector._KNOWN_RANGES,
    # so this passes WITHOUT the 2x known-range bonus.
    _feed_court_flow(det, "45.79.123.45", pps=30, secs=5.0)
    assert det.court_ip == "45.79.123.45"
    assert det.confidence >= cfg.court_lock_confidence


def test_court_ip_ignores_transient_and_private_and_offport():
    cfg = RTTSyncConfig()
    # Brief low-volume public peer: below the packet-count floor -> no lock.
    d1 = CourtIPDetector(cfg)
    _feed_court_flow(d1, "45.79.123.45", pps=10, secs=0.5)
    assert d1.court_ip == ""
    # Public peer but OUTSIDE the court UDP window -> no lock.
    d2 = CourtIPDetector(cfg)
    _feed_court_flow(d2, "45.79.123.45", pps=45, secs=5.0, dst_port=443)
    assert d2.court_ip == ""
    # High-rate PRIVATE LAN peer on a court port -> never the court.
    d3 = CourtIPDetector(cfg)
    _feed_court_flow(d3, "192.168.1.50", pps=45, secs=5.0)
    assert d3.court_ip == ""


def test_court_staleness_uses_packet_clock_not_local():
    # All peer timestamps come from the capture process clock. Lock must work
    # regardless of how far that clock sits from this process's perf_counter.
    cfg = RTTSyncConfig()
    det = CourtIPDetector(cfg)
    _feed_court_flow(det, "45.79.5.5", pps=40, secs=4.0, ts_base=10.0)
    assert det.court_ip == "45.79.5.5"
