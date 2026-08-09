import json
import logging
import math
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
logger = logging.getLogger('RTTSyncEngine')


def _is_public_ip_addr(ip):
    """The court is always a PUBLIC game server. Exclude PS5/PC/LAN so the local
    console IP can never be mistaken for the court — critical on a shared/ICS
    connection where the PS5's own private IP carries the most game packets."""
    try:
        import ipaddress
        obj = ipaddress.ip_address(str(ip).strip())
        return not (obj.is_private or obj.is_loopback or obj.is_link_local
                    or obj.is_multicast or obj.is_reserved or obj.is_unspecified)
    except Exception:
        return False

@dataclass
class RTTSyncConfig:
    ping_enabled: bool = True
    ping_target: str = ''
    ping_interval_ms: float = 400.0
    # Faster sampling while a shot is in flight (meter visible): the per-shot
    # sample-and-hold latch on the native side freezes the offset at hold-start,
    # so the freshest possible sample right before the hold is what matters.
    ping_interval_hold_ms: float = 150.0
    ping_timeout_ms: float = 500.0
    ema_alpha: float = 0.08
    kalman_process_noise: float = 0.2
    kalman_measurement_noise: float = 1.5
    outlier_sigma: float = 2.5
    min_samples: int = 4
    jitter_safety_margin_ms: float = 0.0
    jitter_window: int = 24
    tick_sync_enabled: bool = True
    tick_fallback_hz: float = 30.0
    tick_phase_advance_ms: float = 2.5
    court_profile_enabled: bool = True
    court_profile_path: str = ''
    udp_port_min: int = 30000
    udp_port_max: int = 30099
    court_lock_min_pps: float = 24.0
    court_lock_min_packets: int = 36
    court_lock_confidence: float = 0.68
    tick_phase_lock: bool = True
    decode_latency_aware: bool = True
    decode_latency_comp_ms: float = 7.5
    adaptive_jitter: bool = True
    court_evidence_ttl_ms: float = 2500.0
    rtt_sample_ttl_ms: float = 2000.0

class KalmanFilter1D:

    def __init__(self, process_noise=0.5, measurement_noise=2.0):
        self._q = process_noise
        self._r = measurement_noise
        self._x = 0.0
        self._p = 100.0
        self._initialized = False

    def update(self, measurement):
        if not self._initialized:
            self._x = measurement
            self._p = self._r
            self._initialized = True
            return self._x
        x_pred = self._x
        p_pred = self._p + self._q
        k = p_pred / (p_pred + self._r)
        self._x = x_pred + k * (measurement - x_pred)
        self._p = (1.0 - k) * p_pred
        return self._x

    @property
    def estimate(self):
        return self._x

    @property
    def uncertainty(self):
        return math.sqrt(max(0.0, self._p))

    def reset(self):
        self._x = 0.0
        self._p = 100.0
        self._initialized = False

class RTTSampler:

    def __init__(self, config):
        self._config = config
        self._target_ip = ''
        self._method = 'none'
        self._lock = threading.Lock()
        self._ping3_available = False
        self._check_ping3()

    def _check_ping3(self):
        try:
            import ping3
            self._ping3_available = True
        except ImportError:
            self._ping3_available = False

    @property
    def target_ip(self):
        with self._lock:
            return self._target_ip

    @target_ip.setter
    def target_ip(self, ip):
        with self._lock:
            self._target_ip = str(ip or '').strip()

    def measure_rtt_ms(self, effective_timeout_ms=None):
        with self._lock:
            ip = self._target_ip
        if not ip:
            return None
        if self._ping3_available:
            rtt = self._ping_icmp(ip, effective_timeout_ms)
            if rtt is not None:
                self._method = 'icmp'
                return rtt
        rtt = self._ping_tcp(ip, effective_timeout_ms=effective_timeout_ms)
        if rtt is not None:
            self._method = 'tcp'
            return rtt
        return None

    def _ping_icmp(self, ip, effective_timeout_ms=None):
        try:
            import ping3
            ping3.EXCEPTIONS = False
            timeout = (effective_timeout_ms or self._config.ping_timeout_ms) / 1000.0
            result = ping3.ping(ip, timeout=timeout, unit='ms')
            if result is not None and result is not False and (result > 0):
                return float(result)
        except Exception:
            pass
        return None

    def _ping_tcp(self, ip, port=80, effective_timeout_ms=None):
        timeout = (effective_timeout_ms or self._config.ping_timeout_ms) / 1000.0
        try:
            t0 = time.perf_counter()
            s = socket.create_connection((ip, port), timeout=timeout)
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            s.close()
            return rtt_ms
        except socket.timeout:
            return None
        except ConnectionRefusedError:
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            return rtt_ms
        except Exception:
            return None

    @property
    def method(self):
        return self._method

class CourtIPDetector:
    _KNOWN_RANGES = [('104.255.0.0/16', '2K-US'), ('185.56.0.0/16', '2K-EU')]

    def __init__(self, config):
        self._config = config
        self._peers = {}
        self._court_ip = ''
        self._court_confidence = 0.0
        self._lock = threading.Lock()

    def observe_packet(self, src_ip, dst_ip, src_port, dst_port, size, timestamp_ms):
        with self._lock:
            for ip, port in [(src_ip, src_port), (dst_ip, dst_port)]:
                if self._config.udp_port_min <= port <= self._config.udp_port_max and _is_public_ip_addr(ip):
                    if ip not in self._peers:
                        self._peers[ip] = _PeerStats()
                    peer = self._peers[ip]
                    peer.packet_count += 1
                    peer.last_ts_ms = timestamp_ms
                    peer.total_bytes += size
                    if peer.first_ts_ms <= 0:
                        peer.first_ts_ms = timestamp_ms
            self._evaluate()

    def _evaluate(self):
        best_ip = ''
        best_score = 0.0
        # Staleness reference: use the most-recent OBSERVED packet timestamp, not
        # this process's perf_counter. The packet `ts` is stamped by the elevated
        # nexus_svc capture process (time.perf_counter() there) and relayed via
        # TCP -> native -> sidecar stdin; perf_counter epochs are PER-PROCESS, so
        # comparing a peer's ts against the sidecar's own perf_counter compared
        # two unrelated clocks and made the 5s eviction window meaningless
        # (it could evict every peer instantly or never). All peer timestamps
        # share the capture process's clock, so the newest one is a valid "now".
        now_ms = 0.0
        for peer in self._peers.values():
            if peer.last_ts_ms > now_ms:
                now_ms = peer.last_ts_ms
        for ip, peer in list(self._peers.items()):
            if now_ms - peer.last_ts_ms > 5000:
                continue
            duration_s = max(0.001, (peer.last_ts_ms - peer.first_ts_ms) / 1000.0)
            pps = peer.packet_count / duration_s
            if peer.packet_count < self._config.court_lock_min_packets:
                continue
            if pps < self._config.court_lock_min_pps:
                continue
            score = pps * math.log2(max(1, peer.packet_count))
            try:
                import ipaddress
                ip_obj = ipaddress.ip_address(ip)
                for cidr, label in self._KNOWN_RANGES:
                    if ip_obj in ipaddress.ip_network(cidr):
                        score *= 2.0
                        break
            except Exception:
                pass
            if score > best_score:
                best_score = score
                best_ip = ip
        if best_ip and best_score > 0:
            # Confidence normaliser. The score is pps*log2(packet_count); for a
            # real 2K court flow (~30-60 pps) sustained for a few seconds the raw
            # score lands ~300-600, so the old /1000 normaliser kept conf well
            # under the 0.68 lock gate and the court IP NEVER locked unless the
            # peer happened to fall in the hard-coded _KNOWN_RANGES (which are
            # unverified guesses). Normalise against a score that a genuine,
            # sustained game flow actually reaches: court_lock_min_pps *
            # log2(4 * court_lock_min_packets). With the defaults (24 pps, 36
            # packets) that's 24*log2(144) ~= 172, so a steady flow that clears
            # the pps + packet-count floors crosses the confidence gate within a
            # few seconds instead of never.
            max_possible = max(
                1.0,
                self._config.court_lock_min_pps
                * math.log2(max(2, 4 * self._config.court_lock_min_packets)),
            )
            conf = min(1.0, best_score / max_possible)
            if conf >= self._config.court_lock_confidence:
                self._court_ip = best_ip
                self._court_confidence = conf

    @property
    def court_ip(self):
        with self._lock:
            return self._court_ip

    @property
    def confidence(self):
        with self._lock:
            return self._court_confidence

    def reset(self):
        with self._lock:
            self._peers.clear()
            self._court_ip = ''
            self._court_confidence = 0.0

@dataclass
class _PeerStats:
    packet_count: int = 0
    first_ts_ms: float = 0.0
    last_ts_ms: float = 0.0
    total_bytes: int = 0

@dataclass
class CourtProfile:
    ip: str = ''
    region: str = ''
    avg_rtt_ms: float = 0.0
    min_rtt_ms: float = 999.0
    max_rtt_ms: float = 0.0
    jitter_ms: float = 0.0
    sample_count: int = 0
    last_seen_ts: float = 0.0
    custom_bias_ms: float = 0.0

    def update(self, rtt_ms):
        self.sample_count += 1
        self.min_rtt_ms = min(self.min_rtt_ms, rtt_ms)
        self.max_rtt_ms = max(self.max_rtt_ms, rtt_ms)
        alpha = 1.0 / min(100, self.sample_count)
        self.avg_rtt_ms = self.avg_rtt_ms * (1.0 - alpha) + rtt_ms * alpha
        self.last_seen_ts = time.time()

class CourtProfileDB:

    def __init__(self, path=''):
        if not path:
            base = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Desktop', 'NexusVision')
            path = os.path.join(base, 'court_profiles.json')
        self._path = path
        self._profiles = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            if os.path.isfile(self._path):
                with open(self._path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for ip, vals in data.items():
                    self._profiles[ip] = CourtProfile(ip=ip, region=str(vals.get('region', '')), avg_rtt_ms=float(vals.get('avg_rtt_ms', 0)), min_rtt_ms=float(vals.get('min_rtt_ms', 999)), max_rtt_ms=float(vals.get('max_rtt_ms', 0)), jitter_ms=float(vals.get('jitter_ms', 0)), sample_count=int(vals.get('sample_count', 0)), last_seen_ts=float(vals.get('last_seen_ts', 0)), custom_bias_ms=float(vals.get('custom_bias_ms', 0)))
        except Exception:
            pass

    def save(self):
        with self._lock:
            try:
                # Prune profiles not seen in 30 days to prevent unbounded growth
                cutoff = time.time() - (30 * 86400)
                self._profiles = {
                    ip: p for ip, p in self._profiles.items()
                    if p.last_seen_ts > cutoff
                }
                data = {}
                for ip, prof in self._profiles.items():
                    data[ip] = {'region': prof.region, 'avg_rtt_ms': round(prof.avg_rtt_ms, 2), 'min_rtt_ms': round(prof.min_rtt_ms, 2), 'max_rtt_ms': round(prof.max_rtt_ms, 2), 'jitter_ms': round(prof.jitter_ms, 2), 'sample_count': prof.sample_count, 'last_seen_ts': prof.last_seen_ts, 'custom_bias_ms': prof.custom_bias_ms}
                tmp = self._path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self._path)
            except Exception as e:
                logger.warning('Failed to save court profiles: %s', e)

    def get_profile(self, ip):
        with self._lock:
            return self._profiles.get(ip)

    def update_profile(self, ip, rtt_ms, region=''):
        with self._lock:
            if ip not in self._profiles:
                self._profiles[ip] = CourtProfile(ip=ip, region=region)
            prof = self._profiles[ip]
            prof.update(rtt_ms)
            if region:
                prof.region = region
            return prof

class TickSynchronizer:

    def __init__(self, tick_hz=30.0):
        self._tick_hz = max(1.0, tick_hz)
        self._tick_interval_ms = 1000.0 / self._tick_hz
        self._phase_offset_ms = 0.0
        self._last_tick_ts_ms = 0.0
        self._tick_samples = deque(maxlen=120)
        self._phase_samples = deque(maxlen=64)
        self._phase_locked = False
        self._phase_lock_confidence = 0.0
        self._lock = threading.Lock()

    def observe_tick(self, timestamp_ms):
        with self._lock:
            self._tick_samples.append(timestamp_ms)
            if len(self._tick_samples) >= 3:
                intervals = []
                samples = list(self._tick_samples)
                for i in range(1, len(samples)):
                    dt = samples[i] - samples[i - 1]
                    if 0.5 < dt < 100.0:
                        intervals.append(dt)
                if intervals:
                    intervals.sort()
                    trim = max(1, len(intervals) // 6)
                    trimmed = intervals[trim:-trim] if len(intervals) > trim * 2 + 2 else intervals
                    avg_interval = sum(trimmed) / len(trimmed)
                    if self._tick_interval_ms * 0.85 <= avg_interval <= self._tick_interval_ms * 1.15:
                        self._tick_interval_ms = self._tick_interval_ms * 0.90 + avg_interval * 0.10
                        self._tick_hz = 1000.0 / self._tick_interval_ms
                    self._last_tick_ts_ms = samples[-1]
                    # Circular mean phase lock for server tick alignment
                    if len(intervals) >= 8:
                        phases = [(ts % self._tick_interval_ms) / self._tick_interval_ms * 2.0 * math.pi for ts in samples[-32:]]
                        sin_sum = sum(math.sin(p) for p in phases)
                        cos_sum = sum(math.cos(p) for p in phases)
                        n = len(phases)
                        mean_phase = math.atan2(sin_sum / n, cos_sum / n)
                        if mean_phase < 0:
                            mean_phase += 2.0 * math.pi
                        self._phase_offset_ms = (mean_phase / (2.0 * math.pi)) * self._tick_interval_ms
                        r = math.sqrt((sin_sum / n) ** 2 + (cos_sum / n) ** 2)
                        self._phase_lock_confidence = min(1.0, r)
                        self._phase_locked = r >= 0.55

    def next_tick_eta_ms(self):
        with self._lock:
            if self._last_tick_ts_ms <= 0 or not self._phase_locked:
                return -1.0
            now = time.perf_counter() * 1000.0
            elapsed = now - self._last_tick_ts_ms
            phase = elapsed % self._tick_interval_ms
            eta = self._tick_interval_ms - phase
            # Apply phase correction if locked
            if self._phase_locked:
                correction = self._phase_offset_ms - phase
                if correction < 0:
                    correction += self._tick_interval_ms
                if correction > self._tick_interval_ms:
                    correction -= self._tick_interval_ms
                eta = correction if correction > 0.5 else correction + self._tick_interval_ms
            return max(0.0, eta)

    def align_release_ms(self, desired_release_ms, phase_advance_ms=2.5):
        with self._lock:
            if self._last_tick_ts_ms <= 0:
                return desired_release_ms
            elapsed = desired_release_ms - self._last_tick_ts_ms
            ticks_ahead = elapsed / self._tick_interval_ms
            next_tick_n = math.ceil(ticks_ahead)
            next_tick_time = self._last_tick_ts_ms + next_tick_n * self._tick_interval_ms
            if self._phase_locked:
                next_tick_time += self._phase_offset_ms - (next_tick_time % self._tick_interval_ms)
                if next_tick_time < desired_release_ms:
                    next_tick_time += self._tick_interval_ms
            aligned = next_tick_time - phase_advance_ms
            return aligned

    @property
    def phase_locked(self):
        with self._lock:
            return self._phase_locked

    @property
    def phase_confidence(self):
        with self._lock:
            return self._phase_lock_confidence

    @property
    def tick_interval_ms(self):
        with self._lock:
            return self._tick_interval_ms

    @property
    def tick_hz(self):
        with self._lock:
            return self._tick_hz

    def reset(self):
        with self._lock:
            self._phase_offset_ms = 0.0
            self._last_tick_ts_ms = 0.0
            self._tick_samples.clear()
            self._phase_samples.clear()
            self._phase_locked = False
            self._phase_lock_confidence = 0.0

@dataclass
class SyncSnapshot:
    ready: bool = False
    reason: str = 'initializing'
    effective_offset_ms: float = 0.0
    rtt_raw_ms: float = 0.0
    rtt_filtered_ms: float = 0.0
    rtt_half_ms: float = 0.0
    jitter_ms: float = 0.0
    jitter_margin_ms: float = 0.0
    court_ip: str = ''
    court_region: str = ''
    court_confidence: float = 0.0
    tick_interval_ms: float = 16.67
    tick_phase_advance_ms: float = 1.0
    next_tick_eta_ms: float = 0.0
    ping_method: str = 'none'
    sample_count: int = 0
    court_bias_ms: float = 0.0
    predicted_rtt_ms: float = 0.0
    predicted_offset_ms: float = 0.0
    jitter_prediction_ms: float = 0.0
    packet_interval_ms: float = 0.0
    packet_timing_confidence: float = 0.0
    tick_phase_ms: float = 0.0
    phase_locked: bool = False
    phase_confidence: float = 0.0
    decode_comp_ms: float = 0.0
    target_verified: bool = False
    target_generation: int = 0
    sample_age_ms: float = 0.0
    court_evidence_age_ms: float = 0.0
    phase_source_verified: bool = False

class RTTSyncEngine:

    def __init__(self, config=None):
        self._config = config or RTTSyncConfig()
        self._sampler = RTTSampler(self._config)
        self._kalman = KalmanFilter1D(self._config.kalman_process_noise, self._config.kalman_measurement_noise)
        self._court_detector = CourtIPDetector(self._config)
        self._court_db = CourtProfileDB(self._config.court_profile_path)
        self._tick_sync = TickSynchronizer(self._config.tick_fallback_hz)
        self._rtt_history = deque(maxlen=200)
        self._jitter_window = deque(maxlen=self._config.jitter_window)
        self._ema_rtt = 0.0
        self._sample_count = 0
        # Monotonic ms of the last accepted RTT sample. Used to detect a long
        # silence (court switch / packet loss) and reset the filter so a new
        # court does not inherit the previous court's latency estimate.
        self._last_sample_ms = 0.0
        self._last_rtt_log_ms = 0.0   # throttle for the per-tick numeric RTT log line
        self._current_court_ip = ''
        self._last_packet_ts_ms = 0.0
        self._packet_intervals = deque(maxlen=160)
        # Monotonic ms of the last live meter detection: while a shot is in
        # flight the ping loop tightens to ping_interval_hold_ms so the offset
        # the native engine latches at hold-start is as fresh as possible.
        self._meter_active_ms = 0.0
        # Measured capture->use frame staleness EMA (live decode compensation,
        # replaces the fixed decode_latency_comp_ms once samples exist).
        self._frame_age_ema_ms = 0.0
        self._target_generation = 0
        self._last_court_evidence_ms = 0.0
        self._stop_evt = threading.Event()
        self._ping_thread = None
        self._lock = threading.Lock()

    def start(self):
        self._stop_evt.clear()
        if self._config.ping_enabled:
            self._ping_thread = threading.Thread(target=self._ping_loop, name='RTTPingLoop', daemon=True)
            self._ping_thread.start()
            logger.info('RTT sync engine started')

    def _reset_rtt_measurements_locked(self):
        """Invalidate every target-specific RTT sample.

        Caller must hold ``self._lock``. A LAN console seed and a public court
        are different paths; carrying the seed across a retarget both fabricates
        readiness and makes the real court samples look like outliers.
        """
        self._kalman.reset()
        self._ema_rtt = 0.0
        self._sample_count = 0
        self._last_sample_ms = 0.0
        self._last_rtt_log_ms = 0.0
        self._rtt_history.clear()
        self._jitter_window.clear()

    def _reset_target_evidence_locked(self):
        """Invalidate packet/tick evidence that belongs to the prior target."""
        self._last_court_evidence_ms = 0.0
        self._last_packet_ts_ms = 0.0
        self._packet_intervals.clear()

    def stop(self):
        self._stop_evt.set()
        if self._ping_thread and self._ping_thread.is_alive():
            self._ping_thread.join(timeout=3.0)
        self._court_db.save()
        logger.info('RTT sync engine stopped')

    def pre_shot_warmup(self):
        """Item 4: Trigger an immediate RTT ping when shot-arming is detected.
        The first shot after idle has a stale RTT estimate (last ping was up to
        400ms ago). This forces a fresh ping so the offset latched at hold-start
        is current. Called by the orchestrator/sidecar when square-down is detected.
        """
        with self._lock:
            ip = self._sampler.target_ip
            generation = self._target_generation
        if not ip:
            return
        rtt = self._sampler.measure_rtt_ms(
            effective_timeout_ms=self._config.ping_interval_hold_ms
        )
        if rtt is not None and rtt > 0:
            accepted = self._process_rtt_sample(
                rtt, expected_target=ip, expected_generation=generation)
            if accepted:
                logger.debug('Pre-shot warmup ping: %.1fms', rtt)

    def observe_packet(self, src_ip, dst_ip, src_port, dst_port, size, timestamp_ms):
        self._court_detector.observe_packet(src_ip, dst_ip, src_port, dst_port, size, timestamp_ms)
        new_court = self._court_detector.court_ip
        target_changed = False
        if new_court:
            with self._lock:
                if new_court != self._current_court_ip:
                    self._reset_rtt_measurements_locked()
                    self._reset_target_evidence_locked()
                    self._target_generation += 1
                    self._current_court_ip = new_court
                    self._sampler.target_ip = new_court
                    target_changed = True
            if target_changed:
                self._tick_sync.reset()
                logger.info('Court IP locked: %s (conf=%.2f)', new_court, self._court_detector.confidence)
        with self._lock:
            current_court = self._current_court_ip
            court_port = (self._config.udp_port_min <= int(src_port) <= self._config.udp_port_max
                          or self._config.udp_port_min <= int(dst_port) <= self._config.udp_port_max)
            is_court_packet = (current_court and court_port
                               and (str(src_ip) == current_court
                                    or str(dst_ip) == current_court))
            local_packet_ms = time.perf_counter() * 1000.0
            if is_court_packet:
                if self._last_packet_ts_ms > 0:
                    dt = local_packet_ms - self._last_packet_ts_ms
                    if 1.0 <= dt <= 80.0:
                        self._packet_intervals.append(dt)
                self._last_packet_ts_ms = local_packet_ms
                self._last_court_evidence_ms = time.monotonic() * 1000.0
        if self._config.tick_sync_enabled and is_court_packet:
            packet_interval, packet_confidence = self._packet_timing()
            if packet_confidence >= 0.25 and 4.0 <= packet_interval <= 40.0:
                self._tick_sync.observe_tick(local_packet_ms)

    def set_ping_target(self, ip):
        """Seed the RTT ping target ONLY (console/gateway) for latency
        measurement, WITHOUT marking it as the detected court IP. The court IP
        stays empty until a real public game-server peer is locked."""
        target = str(ip or '').strip()
        if not target:
            return False
        with self._lock:
            # A startup seed must never retarget an already locked court back to
            # the local console during a duplicate promote/reconnect callback.
            if self._current_court_ip:
                return False
            if self._sampler.target_ip != target:
                self._reset_rtt_measurements_locked()
                self._target_generation += 1
                self._sampler.target_ip = target
        return True

    def set_target_ip(self, ip):
        target = str(ip or '').strip()
        if not _is_public_ip_addr(target):
            logger.warning('Rejected non-public court RTT target: %s', target or '<empty>')
            return False
        target_changed = False
        with self._lock:
            if target != self._current_court_ip or self._sampler.target_ip != target:
                self._reset_rtt_measurements_locked()
                self._reset_target_evidence_locked()
                self._target_generation += 1
                target_changed = True
            self._sampler.target_ip = target
            self._current_court_ip = target
        if target_changed:
            self._court_detector.reset()
            self._tick_sync.reset()
        return True

    def clear_court_target(self):
        """Revoke court, RTT, and tick authority for an ended flow/session."""
        with self._lock:
            self._reset_rtt_measurements_locked()
            self._reset_target_evidence_locked()
            self._target_generation += 1
            self._current_court_ip = ''
            self._sampler.target_ip = ''
        self._court_detector.reset()
        self._tick_sync.reset()

    def notify_meter_active(self):
        """A live meter detection was just fed to the engine (shot in flight).
        Tightens the ping cadence for the next ~2.5s."""
        with self._lock:
            self._meter_active_ms = time.monotonic() * 1000.0

    def observe_frame_age_ms(self, age_ms):
        """Feed the measured capture->use staleness of a CV frame. The EMA
        replaces the fixed decode_latency_comp_ms in the effective offset."""
        try:
            age = float(age_ms)
        except (TypeError, ValueError):
            return
        if not 0.0 <= age <= 200.0:
            return
        with self._lock:
            if self._frame_age_ema_ms <= 0.0:
                self._frame_age_ema_ms = age
            else:
                self._frame_age_ema_ms = self._frame_age_ema_ms * 0.8 + age * 0.2

    def get_snapshot(self, decode_latency_ms=None):
        with self._lock:
            court_ip = self._current_court_ip
            sampler_target = self._sampler.target_ip
            sample_count = self._sample_count
            frame_age_ema = self._frame_age_ema_ms
            target_generation = self._target_generation
            last_sample_ms = self._last_sample_ms
            last_court_evidence_ms = self._last_court_evidence_ms
            # Snapshot the ping-thread-owned filter state atomically. Mixing an
            # old N with a new Kalman value/history can briefly manufacture a
            # ready offset that never existed as one estimator generation.
            kalman_rtt = self._kalman.estimate
            ema_rtt = self._ema_rtt
            rtt_history = list(self._rtt_history)
            jitter_samples = list(self._jitter_window)
        rtt_filtered = kalman_rtt if sample_count >= self._config.min_samples else ema_rtt
        predicted_rtt = self._predict_next_rtt(rtt_filtered, rtt_history)
        rtt_half = rtt_filtered / 2.0
        predicted_half = predicted_rtt / 2.0 if predicted_rtt > 0 else rtt_half
        prediction_delta = max(-5.0, min(5.0, predicted_half - rtt_half))
        jitter = self._jitter_for_samples(jitter_samples)
        # Adaptive jitter margin: tighter on stable, wider on jittery
        if self._config.adaptive_jitter:
            jitter_margin = min(8.0, max(0.5, jitter * 1.5))
        else:
            jitter_margin = min(jitter, self._config.jitter_safety_margin_ms)
        # Decode latency compensation - frame is this many ms stale. Prefer the
        # LIVE measured frame-age EMA over the fixed config constant; an explicit
        # decode_latency_ms argument still wins (caller measured it itself).
        if decode_latency_ms is None:
            decode_latency_ms = frame_age_ema if frame_age_ema > 0.0 else self._config.decode_latency_comp_ms
        decode_comp = max(0.0, min(40.0, float(decode_latency_ms))) if self._config.decode_latency_aware else 0.0
        court_bias = 0.0
        court_region = ''
        if court_ip and self._config.court_profile_enabled:
            prof = self._court_db.get_profile(court_ip)
            if prof:
                court_bias = prof.custom_bias_ms
                court_region = prof.region
        tick_eta = self._tick_sync.next_tick_eta_ms()
        tick_interval = self._tick_sync.tick_interval_ms
        packet_interval, packet_confidence = self._packet_timing()
        tick_phase = max(0.0, tick_interval - tick_eta) if tick_interval > 0 else 0.0
        phase_locked = self._tick_sync.phase_locked if self._config.tick_phase_lock else False
        # Local console/gateway samples are startup diagnostics only. Timing
        # authority begins after a public court target is identified and the
        # post-retarget filter has independently reconverged.
        now_monotonic_ms = time.monotonic() * 1000.0
        sample_age_ms = (max(0.0, now_monotonic_ms - last_sample_ms)
                         if last_sample_ms > 0.0 else float('inf'))
        court_evidence_age_ms = (max(0.0, now_monotonic_ms - last_court_evidence_ms)
                                 if last_court_evidence_ms > 0.0 else float('inf'))
        target_verified = (_is_public_ip_addr(court_ip)
                           and sampler_target == court_ip
                           and court_evidence_age_ms <= self._config.court_evidence_ttl_ms
                           and sample_age_ms <= self._config.rtt_sample_ttl_ms)
        ready = (target_verified
                 and sample_count >= self._config.min_samples
                 and rtt_filtered > 0)
        effective = 0.0
        predicted_offset = 0.0
        reason = 'initializing'
        if ready:
            # Total offset = network_half_rtt + prediction_trend + jitter_safety
            #              + tick_phase_advance + court_bias + decode_staleness
            effective = (rtt_half + prediction_delta + jitter_margin
                        + self._config.tick_phase_advance_ms + court_bias + decode_comp)
            # Forward-looking variant: the UNCLAMPED predicted half-RTT instead
            # of current-half + clamped trend. The native Auto path prefers this
            # (syncAdjustMs) so a rising RTT is compensated before it lands.
            predicted_offset = (predicted_half + jitter_margin
                        + self._config.tick_phase_advance_ms + court_bias + decode_comp)
            if phase_locked:
                reason = 'phase_locked'
            else:
                reason = 'synced'
        elif sample_count > 0:
            # Keep the raw/filtered sample visible for diagnostics, but publish
            # no authority-bearing half-RTT or offsets before a verified court
            # target has fully converged.
            reason = 'court_converging' if target_verified else 'local_diagnostic'
        return SyncSnapshot(
            ready=ready, reason=reason,
            effective_offset_ms=round(effective, 3),
            rtt_raw_ms=round(rtt_history[-1] if rtt_history else 0.0, 2),
            rtt_filtered_ms=round(rtt_filtered, 2),
            rtt_half_ms=round(rtt_half, 2) if ready else 0.0,
            jitter_ms=round(jitter, 2),
            jitter_margin_ms=round(jitter_margin, 2),
            court_ip=court_ip, court_region=court_region,
            court_confidence=round(self._court_detector.confidence, 2),
            tick_interval_ms=round(tick_interval, 2),
            tick_phase_advance_ms=self._config.tick_phase_advance_ms,
            next_tick_eta_ms=round(tick_eta, 2),
            ping_method=self._sampler.method,
            sample_count=sample_count, court_bias_ms=court_bias,
            predicted_rtt_ms=round(predicted_rtt, 2) if ready else 0.0,
            predicted_offset_ms=round(predicted_offset, 3),
            jitter_prediction_ms=round(prediction_delta, 3),
            packet_interval_ms=round(packet_interval, 3),
            packet_timing_confidence=round(packet_confidence, 3),
            tick_phase_ms=round(tick_phase, 3),
            phase_locked=phase_locked,
            phase_confidence=round(self._tick_sync.phase_confidence, 3),
            decode_comp_ms=round(decode_comp, 3),
            target_verified=target_verified,
            target_generation=target_generation,
            sample_age_ms=round(sample_age_ms, 1) if math.isfinite(sample_age_ms) else -1.0,
            court_evidence_age_ms=(round(court_evidence_age_ms, 1)
                                   if math.isfinite(court_evidence_age_ms) else -1.0),
            # Relayed packet arrivals have not yet been calibrated against a
            # console/game tick clock, so phase is diagnostic-only for now.
            phase_source_verified=False)

    def get_effective_offset_ms(self):
        snap = self.get_snapshot()
        return snap.effective_offset_ms if snap.ready else 0.0

    def align_release(self, desired_ms):
        return self._tick_sync.align_release_ms(desired_ms, self._config.tick_phase_advance_ms)

    def _ping_loop(self):
        while not self._stop_evt.is_set():
            with self._lock:
                ip = self._sampler.target_ip
                generation = self._target_generation
            if not ip:
                if self._config.ping_target:
                    self.set_ping_target(self._config.ping_target)
                    with self._lock:
                        ip = self._sampler.target_ip
                        generation = self._target_generation
                    if not ip:
                        self._stop_evt.wait(1.0)
                        continue
                else:
                    self._stop_evt.wait(1.0)
                    continue
            # Tighten the cadence while a shot is in flight (recent live meter
            # detection) so the offset latched at the NEXT hold-start is fresh.
            interval_ms = self._config.ping_interval_ms
            with self._lock:
                meter_active_ms = self._meter_active_ms
            if meter_active_ms > 0.0 and (time.monotonic() * 1000.0 - meter_active_ms) < 2500.0:
                interval_ms = min(interval_ms, self._config.ping_interval_hold_ms)
            # Cap ping timeout to the effective interval so a blocking ping can't
            # stall the tightened cadence during meter-active periods.
            rtt = self._sampler.measure_rtt_ms(
                effective_timeout_ms=interval_ms if meter_active_ms > 0.0 else None
            )
            if rtt is not None and rtt > 0:
                self._process_rtt_sample(rtt, expected_target=ip,
                                         expected_generation=generation)
            self._stop_evt.wait(interval_ms / 1000.0)

    def _process_rtt_sample(self, rtt_ms, expected_target=None,
                            expected_generation=None):
        with self._lock:
            if expected_target is not None:
                if (self._sampler.target_ip != expected_target
                        or (expected_generation is not None
                            and self._target_generation != expected_generation)):
                    logger.debug('Discarded RTT sample from stale target generation')
                    return False
            now_ms = time.monotonic() * 1000.0
            # Staleness reset: if RTT samples stopped for >30s (court switch or
            # connection drop), the prior estimate is no longer valid. Drop the
            # filter/history so this sample re-seeds cleanly instead of being
            # blended against a stale mean and skipped as an "outlier".
            if self._last_sample_ms > 0.0 and (now_ms - self._last_sample_ms) > 30000.0:
                self._kalman.reset()
                self._ema_rtt = 0.0
                self._sample_count = 0
                self._rtt_history.clear()
                self._jitter_window.clear()
                logger.info('RTT staleness reset (%.1fs gap); re-seeding latency', (now_ms - self._last_sample_ms) / 1000.0)
            if self._sample_count > self._config.min_samples:
                mean = self._ema_rtt
                if len(self._jitter_window) >= 3:
                    std = self._compute_jitter()
                    if abs(rtt_ms - mean) > self._config.outlier_sigma * max(1.0, std):
                        return False
            # Freshness belongs to accepted samples only. Advancing this clock
            # before the outlier gate would let a stream of rejected probes keep
            # stale RTT authority alive indefinitely.
            self._last_sample_ms = now_ms
            self._rtt_history.append(rtt_ms)
            self._jitter_window.append(rtt_ms)
            if self._sample_count == 0:
                self._ema_rtt = rtt_ms
            else:
                alpha = self._config.ema_alpha
                self._ema_rtt = alpha * rtt_ms + (1.0 - alpha) * self._ema_rtt
            self._kalman.update(rtt_ms)
            self._sample_count += 1
            court_ip = self._current_court_ip
            if court_ip:
                self._court_db.update_profile(court_ip, rtt_ms)
            # Per-tick NUMERIC RTT telemetry. Previously the only RTT log line was the valueless
            # "retargeted to court IP" spam (~27k/session); this logs the real filtered RTT + jitter
            # so orion_native.log actually carries the timing signal. Throttled to ~1/s (the raw tick
            # rate is 3-10 Hz) so the log stays readable while every sample still updates the filter.
            try:
                if (now_ms - self._last_rtt_log_ms) >= 1000.0:
                    self._last_rtt_log_ms = now_ms
                    _filt = self._kalman.estimate if self._sample_count >= self._config.min_samples else self._ema_rtt
                    _jit = self._compute_jitter() if len(self._jitter_window) >= 3 else 0.0
                    logger.info('RTT tick: raw=%.1fms filtered=%.1fms half=%.1fms jitter=%.1fms n=%d court=%s',
                                rtt_ms, _filt, _filt * 0.5, _jit, self._sample_count, court_ip or '-')
            except Exception:
                pass
            return True

    def _compute_jitter(self):
        return self._jitter_for_samples(list(self._jitter_window))

    @staticmethod
    def _jitter_for_samples(samples):
        if len(samples) < 2:
            return 0.0
        mean = sum(samples) / len(samples)
        variance = sum(((s - mean) ** 2 for s in samples)) / len(samples)
        return math.sqrt(max(0.0, variance))

    def _predict_next_rtt(self, fallback, history=None):
        samples = list(self._rtt_history if history is None else history)[-30:]
        if len(samples) < 4:
            return float(fallback)
        n = len(samples)
        xs = list(range(n))
        x_mean = sum(xs) / n
        y_mean = sum(samples) / n
        den = sum(((x - x_mean) ** 2 for x in xs))
        if den <= 1e-9:
            return float(fallback)
        slope = sum(((x - x_mean) * (y - y_mean) for x, y in zip(xs, samples))) / den
        predicted = samples[-1] + slope
        upper = max(samples[-1] + 8.0, float(fallback) + 8.0)
        return max(0.0, min(upper, predicted))

    def _packet_timing(self):
        with self._lock:
            intervals = list(self._packet_intervals)
        if len(intervals) < 6:
            return (0.0, 0.0)
        candidates = [dt for dt in intervals[-80:] if 4.0 <= dt <= 40.0]
        if len(candidates) < 6:
            return (0.0, 0.0)
        candidates.sort()
        median = candidates[len(candidates) // 2]
        near = [dt for dt in candidates if abs(dt - median) <= 2.5]
        confidence = min(1.0, len(near) / max(8.0, len(candidates) * 0.65))
        return (median, confidence)

def load_rtt_sync_config(settings_path=None):
    from orion_config_io import load_settings_raw
    cfg = RTTSyncConfig()
    raw = load_settings_raw(settings_path)
    if raw is None:
        return cfg
    cfg.ping_enabled = bool(raw.get('network_ping_enabled', cfg.ping_enabled))
    cfg.ping_target = str(raw.get('network_ping_target', cfg.ping_target) or '')
    cfg.ping_interval_ms = max(100, min(10000, float(raw.get('network_ping_interval_ms', cfg.ping_interval_ms) or 400)))
    cfg.ping_interval_hold_ms = max(50, min(2000, float(raw.get('network_ping_interval_hold_ms', cfg.ping_interval_hold_ms) or 150)))
    cfg.ping_timeout_ms = max(100, min(5000, float(raw.get('network_ping_timeout_ms', cfg.ping_timeout_ms) or 500)))
    cfg.tick_sync_enabled = bool(raw.get('network_tick_sync_enabled', cfg.tick_sync_enabled))
    cfg.tick_fallback_hz = max(1, min(240, float(raw.get('network_tick_fallback_hz', cfg.tick_fallback_hz) or 30)))
    cfg.tick_phase_advance_ms = max(0, min(50, float(raw.get('network_tick_phase_advance_ms', cfg.tick_phase_advance_ms) or 2.5)))
    cfg.udp_port_min = int(raw.get('network_udp_port_min', cfg.udp_port_min) or 30000)
    cfg.udp_port_max = int(raw.get('network_udp_port_max', cfg.udp_port_max) or 30099)
    rtt_cfg = raw.get('rtt_sync', {})
    if isinstance(rtt_cfg, dict):
        cfg.ema_alpha = max(0.01, min(1.0, float(rtt_cfg.get('ema_alpha', cfg.ema_alpha) or 0.08)))
        cfg.kalman_process_noise = max(0.01, min(100, float(rtt_cfg.get('kalman_q', cfg.kalman_process_noise) or 0.2)))
        cfg.kalman_measurement_noise = max(0.01, min(100, float(rtt_cfg.get('kalman_r', cfg.kalman_measurement_noise) or 1.5)))
        cfg.outlier_sigma = max(1.0, min(10.0, float(rtt_cfg.get('outlier_sigma', cfg.outlier_sigma) or 2.5)))
        cfg.jitter_safety_margin_ms = max(0, min(20, float(rtt_cfg.get('jitter_margin_ms', cfg.jitter_safety_margin_ms) or 0)))
        cfg.court_profile_enabled = bool(rtt_cfg.get('court_profiles', cfg.court_profile_enabled))
        cfg.tick_phase_lock = bool(rtt_cfg.get('tick_phase_lock', cfg.tick_phase_lock))
        cfg.decode_latency_aware = bool(rtt_cfg.get('decode_latency_aware', cfg.decode_latency_aware))
        cfg.decode_latency_comp_ms = max(0.0, min(40.0, float(rtt_cfg.get('decode_latency_ms', cfg.decode_latency_comp_ms) or cfg.decode_latency_comp_ms)))
        cfg.adaptive_jitter = bool(rtt_cfg.get('adaptive_jitter', cfg.adaptive_jitter))
    return cfg
