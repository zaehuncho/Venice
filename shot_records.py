"""shot_records.py -- ONE labelled training record per shot the owner takes.

WHY THIS EXISTS.  The animation-anchor model needs (press -> onset -> release ->
outcome) tuples with a real grade attached.  Until now every one of those tuples was
assembled BY HAND from a framedump plus three separate log greps (``PICKUP:``,
``RELEASE ORACLE:``, ``BANNER VERDICT:``), which is why the corpus is a handful of
drill clips instead of every shot of every session.  Every field this module writes
already exists somewhere in the live sidecar; the only thing that was missing was a
per-shot JOIN keyed on the shot-gate physical epoch and a durable place to put it.

WHERE IT RUNS.  The ``note_*`` entry points are O(1) dict writes under one lock and
are safe to call from any thread; in the sidecar they are called from

  * the CONTROL thread (stdin) -- ``note_press`` / ``note_release`` / ``note_disarm``,
    from ``RemotePlayOrchestrator.arm_shot_gate`` / ``_close_shot_gate_press``,
  * the READER's release path -- ``note_oracle`` (the ``release_oracle`` sink),
  * the BANNER worker -- ``note_banner`` (wrapped ``emit_line``),
  * the DETECT thread -- ``note_onset`` / ``note_icon_sample``, which read only fields
    that frame already computed (no new pixels work beyond one 22x22 ROI mean at 10 Hz
    when ``ORION_SHOT_RECORD_ICON=1``, which is OFF by default).

NOTHING on any of those threads touches the disk.  A record is handed to a dedicated
daemon writer thread which appends one JSON line and fsyncs.

WHEN A RECORD CLOSES.  Not at the release: the RELEASE ORACLE lands ~0.5 s after the
command and the game's own banner lands 1.0-1.7 s after it (plus ~115 ms median emit
latency).  A released press therefore stays open for ``grace_ms`` (3.5 s) and closes
EARLY the moment it is complete (release + oracle + banner).  A press that is never
answered closes at ``press_timeout_ms`` with ``outcome="unanswered"`` -- that record is
as valuable as any other, because a press with no release is exactly the stuck-Square
class.  At most ``max_open`` presses are tracked; an older one is force-closed
(``closed_reason="superseded"``) rather than dropped.

TEMPO.  Computed from the SAME thresholds ``BannerLeadTrim::tempoFor`` uses
(native_orion/src/BannerLeadTrim.h): Standstill quick < 500 / normal 500-580 /
slow > 580 ms, Left/Right Fade quick < 775 / normal 775-915 / slow > 915 ms, every
other type ("Other", i.e. Go-To and the Tempo words) always "normal".  Those cut
points are in the ENGINE's frame -- its first accepted vision sample measured from the
physical Square edge. Schema 2 does NOT claim that a fixed lag recovers that value:
onset_engine_ms remains null and tempo is unknown without native-owned onset evidence.
The historical +36 ms approximation survives only in onset_engine_estimate_ms and
tempo_estimate, with the configured lag recorded. Locator PICKUP is raw proposal
evidence only; onset_ms is the first epoch-matched reader detection, not native ownership.
Native physical-edge wall time is sent explicitly. A missing/invalid edge stamp keeps
physical hold/onset unknown rather than substituting delayed arm-receipt time.

KNOBS
    ORION_SHOT_RECORDS              1  master switch
    ORION_SHOT_RECORDS_ROOT            output dir (default D:\\NexusVision\\shot_records)
    ORION_SHOT_RECORDS_SESSION         session name -> <session>.jsonl
    ORION_SHOT_RECORD_GRACE_MS      3500  how long a released press waits for oracle+banner
    ORION_SHOT_RECORD_TIMEOUT_MS    8000  how long an unanswered press stays open
    ORION_SHOT_RECORD_ONSET_LAG_MS    36  sidecar first-sight -> engine accept
    ORION_SHOT_RECORD_ICON             0  sample the nameplate "3" cell at 10 Hz (see below)

THE "3" ICON.  ``tools/diagnostics/hud_3pt_icon.py``'s ``probe()`` is an 8-scale
``matchTemplate`` sweep of the PS-disc and gamertag templates over a 566-row band of
the frame: **157.7 ms per frame measured on this workstation**, i.e. ~9.5 frame times
at 60 fps.  It cannot run live, not even once per release window.  So this module takes
the documented fallback: when ``ORION_SHOT_RECORD_ICON=1`` it records the MEAN
BRIGHTNESS and dark fraction of the nameplate's "3" cell at 10 Hz inside the release
window (0.026 ms per sample, measured), using the plate position ``player_anchor``
already found on that frame -- no new search.  ``icon_off_ms`` stays null and is derived
OFFLINE from ``icon_cell`` (the icon is a near-black disc with a bright glyph; it is
gone when the cell goes bright, ~163 ms after the release per the 09-14 study).
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import sys
import threading
import time

logger = logging.getLogger('ShotRecords')

SCHEMA = 'shot_record/2'

# COCO-17 keypoint order, reserved for `pose_track` (see _blank_record).  The pose model
# is not wired yet, so `pose_track` is null in every record this build writes; the schema
# is pinned NOW so the corpus does not have to be re-collected when it lands.
POSE_TRACK_SCHEMA = 'coco17/[ts_ms,x,y,conf]'
POSE_KEYPOINTS = 17

# --------------------------------------------------------------------------- tempo
# Mirrors native_orion/src/BannerLeadTrim.h (kStandstillQuickOnsetMs etc.).  Keep the
# two in step: a divergence silently files training records in a bucket the engine's
# trim never uses.
STANDSTILL_QUICK_MS = 500.0
STANDSTILL_SLOW_MS = 580.0
FADE_QUICK_MS = 775.0
FADE_SLOW_MS = 915.0
DEFAULT_ONSET_LAG_MS = 36.0

_FADES = ('Left Fade', 'Right Fade')


def bucket_for(shot_type) -> str:
    """The shot-type bucket, exactly as BannerLeadTrim::bucketFor computes it."""
    t = str(shot_type or '').strip()
    if t in _FADES:
        return t
    if not t or t == 'Standstill':
        return 'Standstill'
    return 'Other'


def tempo_for(shot_type, onset_engine_ms) -> str:
    """quick|normal|slow from the ENGINE-frame onset, as BannerLeadTrim::tempoFor.

    A missing/negative onset is NOT a tempo: it is the reference class ("normal"),
    which is also where every legacy persisted key lands.
    """
    try:
        onset = float(onset_engine_ms)
    except (TypeError, ValueError):
        return 'normal'
    if onset != onset or onset < 0.0:          # NaN / no onset
        return 'normal'
    bucket = bucket_for(shot_type)
    if bucket in _FADES:
        if onset < FADE_QUICK_MS:
            return 'quick'
        return 'slow' if onset > FADE_SLOW_MS else 'normal'
    if bucket == 'Standstill':
        if onset < STANDSTILL_QUICK_MS:
            return 'quick'
        return 'slow' if onset > STANDSTILL_SLOW_MS else 'normal'
    return 'normal'


def tempo_key(shot_type, tempo) -> str:
    """"Standstill/quick" -- the composite key the engine's trim persists under."""
    return '%s/%s' % (bucket_for(shot_type), str(tempo or 'normal'))


# --------------------------------------------------------------------------- env
def _env(name, default=''):
    return str(os.environ.get(name, default) or '').strip()


def _compiled_customer_build() -> bool:
    """[CL2-P5-003] True inside the Nuitka-compiled customer sidecar (Nuitka defines the module
    global ``__compiled__``; PyInstaller-style freezers set ``sys.frozen``)."""
    return '__compiled__' in globals() or bool(getattr(sys, 'frozen', False))


# [CL2-P5 D-fallback 2026-09-23] The owner's workstation keeps outputs on D:; everyone else
# (and a D: that is full, read-only, a card reader or an optical drive) gets the per-user
# app-data folder. Module-level so tests can point the "primary drive" at a temp dir.
_PRIMARY_DRIVE = 'D:\\'


def _root_is_writable(root) -> bool:
    """Prove ``root`` accepts a real file: create the directory, write + fsync a probe file,
    remove it. ``os.path.isdir('D:\\')`` alone passed a full or read-only D: and the recorder
    then failed later. Never raises."""
    probe = None
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, '.write_probe_%d_%d.tmp'
                             % (os.getpid(), threading.get_ident()))
        with open(probe, 'wb') as fh:
            fh.write(b'{"probe":1}\n' * 64)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                os.remove(probe)
            except Exception:
                pass


def _select_records_root(env, log):
    """First writable root, or None. Explicit ORION_SHOT_RECORDS_ROOT first, then D:, then
    LOCALAPPDATA. A root that fails the probe is skipped with a warning (non-blocking)."""
    candidates = []
    explicit = str(env.get('ORION_SHOT_RECORDS_ROOT', '') or '').strip()
    if explicit:
        candidates.append(explicit)
    if os.path.isdir(_PRIMARY_DRIVE):
        candidates.append(os.path.join(_PRIMARY_DRIVE, 'NexusVision', 'shot_records'))
    candidates.append(os.path.join(env.get('LOCALAPPDATA') or os.path.expanduser('~'),
                                   'NexusVision', 'shot_records'))
    for root in candidates:
        if _root_is_writable(root):
            return root
        log.warning('shot records: %s is not writable (absent, read-only or full); '
                    'trying the next location', root)
    return None


def _env_float(name, default):
    try:
        return float(_env(name, '') or default)
    except (TypeError, ValueError):
        return float(default)


def _num(value, default=None):
    """float(value) or `default` -- never raises, never returns NaN."""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


class ShotRecorder:
    """Assembles one record per shot-gate epoch and appends it as JSONL."""

    #: how long a CLOSED (released/disarmed) press waits for its late instruments
    DEFAULT_GRACE_MS = 3500.0
    #: how long a press with no release edge at all stays open
    DEFAULT_TIMEOUT_MS = 8000.0
    #: bound on concurrently-open presses (the owner cannot have 9 shots in the air)
    DEFAULT_MAX_OPEN = 8

    def __init__(self, path, session='', grace_ms=None, timeout_ms=None,
                 onset_lag_ms=None, max_open=None, log=None, start_thread=True,
                 clock=None, queue_size=256, wait_for_framedump=False):
        self.path = str(path)
        self.session = str(session or '')
        # A press-window census arrives after its post-release banner tail. Keep
        # graded records joinable until that metadata arrives, without waiting on
        # a worker or extending the existing grace/session-end escape paths.
        self.wait_for_framedump = bool(wait_for_framedump)
        self.grace_ms = float(self.DEFAULT_GRACE_MS if grace_ms is None else grace_ms)
        self.timeout_ms = float(self.DEFAULT_TIMEOUT_MS if timeout_ms is None else timeout_ms)
        self.onset_lag_ms = float(DEFAULT_ONSET_LAG_MS if onset_lag_ms is None else onset_lag_ms)
        self.max_open = max(1, int(self.DEFAULT_MAX_OPEN if max_open is None else max_open))
        self._log = log or logger
        self._clock = clock if callable(clock) else (lambda: time.time() * 1000.0)
        self._lock = threading.RLock()
        self._open = {}                 # epoch -> record dict, insertion-ordered
        self._q = queue.Queue(maxsize=max(8, int(queue_size)))
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._close_logged = False
        self._drop_notice = None
        self._seq = 0
        # counters (read by snapshot(), logged at stop)
        self.presses = 0
        self.records_written = 0
        self.records_dropped = 0
        self.write_errors = 0
        self.write_ms_total = 0.0
        self.orphan_oracle = 0
        self.orphan_banner = 0
        if start_thread:
            self.start()

    # ------------------------------------------------------------------ factory
    @classmethod
    def create(cls, session='', log=None, env=None, start_thread=True, clock=None):
        """Build the session's recorder, or None when ORION_SHOT_RECORDS=0.

        Never raises: a diagnostic that cannot open its output must cost the session
        nothing at all.
        """
        log = log or logger
        env = env if env is not None else os.environ
        try:
            explicit = 'ORION_SHOT_RECORDS' in env
            flag = str(env.get('ORION_SHOT_RECORDS', '1') or '1').strip().lower()
            if flag not in ('1', 'true', 'yes', 'on'):
                log.info('shot records: OFF (ORION_SHOT_RECORDS=%s)', flag)
                return None
            # The corpus is PRODUCTION DATA the anchor model will be fitted on. A test run
            # that happens to build a real orchestrator must not append synthetic shots to
            # it (or create its directory), so the default is OFF under pytest and the
            # switch has to be set on purpose -- which the dedicated suites do.
            # `PYTEST_CURRENT_TEST` alone is not enough: it is unset while pytest is
            # COLLECTING, and a suite that builds a real orchestrator at module import
            # would slip through.  The module check catches both.
            if not explicit and (env.get('PYTEST_CURRENT_TEST') or 'pytest' in sys.modules):
                return None
            # [CL2-P5-003 2026-09-23] A development instrument, not a customer feature: it fsyncs
            # every shot with no retention and its default root is this workstation's D:. A
            # compiled (Nuitka) customer sidecar therefore records nothing unless the switch is
            # set on purpose; the owner's source runs are unchanged.
            if not explicit and _compiled_customer_build():
                log.info('shot records: OFF (customer build; set ORION_SHOT_RECORDS=1 to enable)')
                return None
            # C: runs at ~7 GB free on this workstation; every new output goes to D:. A machine
            # without a USABLE D: (absent, read-only, full) falls back to the per-user app-data
            # folder -- proven by a real create/write/fsync, not by isdir. [CL2-P5 2026-09-23]
            root = _select_records_root(env, log)
            if root is None:
                log.warning('shot records DISABLED: no writable location')
                return None
            name = str(env.get('ORION_SHOT_RECORDS_SESSION', '') or '').strip()
            if not name:
                name = session or cls.default_session_name(env)
            name = ''.join(c for c in name if c.isalnum() or c in '._-') or 'session'
            os.makedirs(root, exist_ok=True)
            path = os.path.join(root, name + '.jsonl')
            rec = cls(path, session=name,
                      grace_ms=_env_float('ORION_SHOT_RECORD_GRACE_MS', cls.DEFAULT_GRACE_MS),
                      timeout_ms=_env_float('ORION_SHOT_RECORD_TIMEOUT_MS', cls.DEFAULT_TIMEOUT_MS),
                      onset_lag_ms=_env_float('ORION_SHOT_RECORD_ONSET_LAG_MS',
                                              DEFAULT_ONSET_LAG_MS),
                      log=log, start_thread=start_thread, clock=clock,
                      wait_for_framedump=all(
                          str(env.get(key, '0') or '0').strip().lower()
                          in ('1', 'true', 'yes', 'on')
                          for key in ('ORION_FRAMEDUMP', 'ORION_FRAMEDUMP_PRESS_WINDOW')))
            log.warning('SHOT RECORDS armed -> %s (grace=%.0fms timeout=%.0fms '
                        'onset_lag=%.0fms)', path, rec.grace_ms, rec.timeout_ms,
                        rec.onset_lag_ms)
            return rec
        except Exception as exc:
            log.warning('shot records DISABLED: %s', exc, exc_info=True)
            return None

    @staticmethod
    def default_session_name(env=None) -> str:
        """Reuse the framedump's session folder name when there is one, so a record
        file and its frames carry the same label; otherwise stamp the clock."""
        env = env if env is not None else os.environ
        for key in ('ORION_FRAMEDUMP_DIR', 'ORION_FRAMEDUMP_ROOT'):
            raw = str(env.get(key, '') or '').strip().rstrip('\\/')
            base = os.path.basename(raw)
            if base.startswith('session_'):
                return base
        return time.strftime('session_%Y%m%d_%H%M%S')

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Start one writer. A stopped recorder is retired; create a new session."""
        with self._lifecycle_lock:
            if self._closed:
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(target=self._writer_loop,
                                            name='shot-records', daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=2.0):
        """Retire within the join budget; only the writer drains and touches disk.

        False means the original writer is still finishing. Retain its handle so
        neither start() nor another stop() can start a concurrent append writer.
        """
        with self._lifecycle_lock:
            with self._lock:
                if not self._closed:
                    self._closed = True
                    self.close_all('session_end')
                    self._stop.set()
                    try:
                        self._q.put_nowait(None)  # wake an idle writer
                    except queue.Full:
                        pass
            t = self._thread
            if (t is None or not t.is_alive()) and not self._close_logged:
                t = self._thread = threading.Thread(target=self._writer_loop,
                                                    name='shot-records', daemon=True)
                t.start()
        if t is not None and t.is_alive():
            if t is threading.current_thread():
                return False
            t.join(timeout=max(0.0, float(timeout)))
            if t.is_alive():
                return False
        with self._lifecycle_lock:
            if self._thread is t:
                self._thread = None
        return True

    def snapshot(self):
        with self._lock:
            return {'presses': self.presses, 'written': self.records_written,
                    'dropped': self.records_dropped, 'errors': self.write_errors,
                    'open': len(self._open), 'queued': self._q.qsize(),
                    'orphan_oracle': self.orphan_oracle,
                    'orphan_banner': self.orphan_banner,
                    'write_ms_total': round(self.write_ms_total, 3)}

    # ------------------------------------------------------------------ record
    def _blank(self, epoch, now_ms):
        self._seq += 1
        return {
            'schema': SCHEMA,
            'session': self.session,
            'seq': int(self._seq),
            'epoch': int(epoch),
            'press_ts_ms': None,
            'press_mono_ms': None,
            'press_clock': 'unknown',
            'press_receipt_ts_ms': None,
            'arm_delivery_ms': None,
            'source': '',
            'shot_type': '',
            'shot_type_upgraded': None,
            'rhythm': 0,
            'pickup': None,
            'onset_ms': None,
            'onset_source': None,
            'onset_fill': None,
            'onset_evidence': None,
            'onset_frame_seq': None,
            'onset_clock': None,
            'onset_read_ms': None,
            'onset_structure_verified': False,
            'onset_lag_ms': round(self.onset_lag_ms, 1),
            'onset_engine_ms': None,
            'onset_engine_estimate_ms': None,
            'tempo_estimate': 'unknown',
            'tempo': 'normal',
            'tempo_key': 'Standstill/normal',
            'release_ms': None,
            'release_after_press_ms': None,
            'outcome': 'unanswered',
            'disarm_reason': None,
            'oracle': None,
            'banner': None,
            'icon_off_ms': None,
            'icon_source': 'unavailable',
            'icon_cell': None,
            # [ORION_SHOT_RANGE 2026-09-17] THREE or MID, read off the same nameplate "3"
            # cell a few frames after the press (shot_range.py).  `range` is the VERDICT the
            # engine was told (`unknown` until the classifier is calibrated), `range_evidence`
            # is what the vote actually said, and `range_cells` is the raw
            # [t_ms, mean, dark, bright] the vote was taken on -- which is what grows the
            # corpus a real calibration needs.
            'range': 'unknown',
            'range_conf': 0.0,
            'range_source': 'unavailable',
            'range_reason': '',
            'range_evidence': 'unknown',
            'range_cells': None,
            # Reserved: a 60 fps landmark track, list of per-frame
            # [ts_ms, x, y, conf] x 17 (COCO-17 order).  null until the pose model
            # is wired; the key and its schema string ship NOW so records collected
            # today stay joinable with records collected after it lands.
            'pose_track': None,
            'pose_track_schema': POSE_TRACK_SCHEMA,
            'pose_keypoints': POSE_KEYPOINTS,
            'framedump': None,
            'closed_reason': '',
            'closed_ts_ms': None,
            '_deadline_ms': float(now_ms) + self.timeout_ms,
            '_complete': False,
        }

    @staticmethod
    def _epoch(value) -> int:
        # Match the native physical-epoch contract without float rounding or
        # bool/whitespace aliases that could label a different open shot.
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            ep = value
        elif (isinstance(value, str) and 1 <= len(value) <= 20
              and value.isascii() and value.isdigit() and value[0] != '0'):
            ep = int(value, 10)
        else:
            return 0
        return ep if 0 < ep <= 0xFFFFFFFFFFFFFFFF else 0

    def _get(self, epoch):
        """The open record for `epoch`, or None.  Caller holds the lock."""
        return self._open.get(self._epoch(epoch))

    # ------------------------------------------------------------- control thread
    def note_press(self, epoch, ts_ms=None, mono_ms=None, source='', shot_type='',
                   rhythm=False, clock_source='caller', received_ts_ms=None):
        """A physical shot edge (``shot_gate_arm``).  Opens this epoch's record.

        A DUPLICATE epoch is the native's ``source=type_upgrade`` re-arm on the 200 ms
        blind grace: it re-types the press and must NOT reset its identity, its press
        timestamp or anything already collected.
        """
        ep = self._epoch(epoch)
        if ep <= 0:
            return False
        now = self._clock()
        with self._lock:
            if self._closed:
                return False
            rec = self._open.get(ep)
            if rec is not None:
                # type upgrade: keep the original classification for the record and
                # note what it became, because the engine's own buckets moved with it.
                new_type = str(shot_type or '').strip()
                if new_type and new_type != rec['shot_type']:
                    rec['shot_type_upgraded'] = new_type
                    self._recompute_tempo(rec)
                if rhythm:
                    rec['rhythm'] = 1
                return True
            rec = self._blank(ep, now)
            rec['press_clock'] = str(clock_source or 'unknown')[:24]
            rec['press_receipt_ts_ms'] = _num(received_ts_ms, now)
            # A legacy/invalid wire stamp is an unknown physical edge, not the
            # receiver's current time. Keep receipt separately; never mix the two
            # into a plausible-looking but shortened hold duration.
            if clock_source in ('caller', 'native_edge'):
                rec['press_ts_ms'] = _num(ts_ms, now)
                rec['press_mono_ms'] = _num(mono_ms, time.perf_counter() * 1000.0)
            if clock_source == 'native_edge':
                rec['arm_delivery_ms'] = round(rec['press_receipt_ts_ms'] - rec['press_ts_ms'], 2)
            rec['source'] = str(source or '')[:24]
            rec['shot_type'] = str(shot_type or '').strip()[:24]
            rec['rhythm'] = 1 if rhythm else 0
            self._recompute_tempo(rec)
            self._open[ep] = rec
            self.presses += 1
            # Bound the open set from the OLDEST end, so a burst of unanswered presses
            # can never hide the shot that is actually in the air.
            while len(self._open) > self.max_open:
                old_ep = next(iter(self._open))
                self._finish(old_ep, 'superseded', now)
            self._sweep(now)
        return True

    def note_release(self, epoch, release_ms=0.0):
        """The engine's release edge (``shot_gate_release``)."""
        ep = self._epoch(epoch)
        now = self._clock()
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                self._sweep(now)
                return False
            if rec['outcome'] == 'released':
                return True  # retransmission is not a later physical release
            rec['outcome'] = 'released'
            rec['disarm_reason'] = None
            rel = _num(release_ms)
            if rel is not None and rel > 0.0:
                rec['release_ms'] = rel
                press = _num(rec.get('press_ts_ms'))
                # Only a PLAUSIBLE hold becomes a label. A release stamped on a different
                # clock (a legacy 1-arg relay, a harness) would otherwise write a hold of
                # minus thirty million seconds and look like data.
                if press is not None and 0.0 <= (rel - press) <= 30000.0:
                    rec['release_after_press_ms'] = round(rel - press, 1)
            rec['_deadline_ms'] = now + self.grace_ms
            self._maybe_complete(rec, now)
            self._sweep(now)
        return True

    def note_disarm(self, epoch, reason='disarm'):
        """The press ended with NO release edge (cancel / abort / tap)."""
        ep = self._epoch(epoch)
        now = self._clock()
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                self._sweep(now)
                return False
            if rec['outcome'] in ('released', 'disarmed'):
                return False  # cleanup never rewrites the first terminal edge
            rec['outcome'] = 'disarmed'
            rec['disarm_reason'] = str(reason or 'disarm')[:32]
            # A cancelled press has no banner and no oracle coming; close it promptly,
            # but not instantly -- the reader may still flush a PICKUP line for it.
            rec['_deadline_ms'] = now + min(self.grace_ms, 1000.0)
            self._sweep(now)
        return True

    # --------------------------------------------------------------- reader side
    def note_pickup(self, epoch, pickup):
        """The reader's PICKUP record for this press (first sight of the meter)."""
        ep = self._epoch(epoch)
        if not isinstance(pickup, dict):
            return False
        now = self._clock()
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            keep = {}
            for key in ('first_sight_fill', 'first_sight_ms_after_press', 'anchor_conf',
                        'anchor_ms'):
                val = _num(pickup.get(key))
                if val is not None:
                    keep[key] = round(val, 2)
            for key in ('anchor_used', 'refused_outside_patch', 'expect_accept',
                        'patch_hits'):
                val = _num(pickup.get(key))
                if val is not None:
                    keep[key] = int(val)
            rec['pickup'] = keep or None
            # PICKUP is a locator proposal on its own press-window clock. A spent
            # meter can propose fill=97 at t=0 and then be rejected by the reader.
            # Preserve that evidence, but never promote it to an accepted onset.
            self._maybe_complete(rec, now)
        return True

    def note_onset(self, epoch, onset_ms, fill=None, source='detect_loop',
                   frame_seq=None, structure_verified=False, sample_wall_ms=None):
        """First epoch-matched reader detection, separate from raw locator proposals.

        This is reader acceptance, not proof of native shot ownership. Native onset
        remains unknown here; the historical fixed-lag approximation is labelled
        explicitly as an estimate rather than a measured engine timing bucket.
        """
        ep = self._epoch(epoch)
        val = _num(onset_ms)
        f = _num(fill)
        if val is None or not 0.0 <= val <= 30000.0 or (fill is not None and
                (f is None or not 0.0 <= f <= 100.0)):
            return False
        with self._lock:
            rec = self._open.get(ep)
            if (rec is None or rec.get('onset_ms') is not None
                    or rec.get('press_mono_ms') is None or rec['outcome'] != 'unanswered'):
                return False
            sample_ms = _num(sample_wall_ms)
            capture_onset = None
            if sample_wall_ms is not None:
                press_wall = _num(rec.get('press_ts_ms'))
                if sample_ms is None or press_wall is None:
                    return False
                capture_onset = sample_ms - press_wall
                if not 0.0 <= capture_onset <= min(30000.0, val + 2.0):
                    return False
            rec['onset_ms'] = round(capture_onset if capture_onset is not None else val, 2)
            rec['onset_clock'] = 'capture_wall' if capture_onset is not None else 'reader_mono'
            rec['onset_read_ms'] = round(val, 2)
            rec['onset_source'] = str(source or 'detect_loop')[:24]
            if f is not None:
                rec['onset_fill'] = round(f, 2)
            rec['onset_evidence'] = 'reader_accepted_not_native_owned'
            rec['onset_frame_seq'] = self._epoch(frame_seq) or None
            rec['onset_structure_verified'] = bool(structure_verified)
            self._recompute_tempo(rec)
        return True

    def note_oracle(self, release_seq, rec_in):
        """RELEASE ORACLE for this release (gap_px / settled_fill / verdict_proxy)."""
        ep = self._epoch(release_seq)
        if not isinstance(rec_in, dict):
            return False
        now = self._clock()
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                self.orphan_oracle += 1
                return False
            keep = {}
            for key in ('gap_px', 'gap_pct', 'settled_fill', 'green_bottom_pct'):
                val = _num(rec_in.get(key))
                if val is not None:
                    keep[key] = round(val, 2)
            keep['verdict_proxy'] = str(rec_in.get('verdict_proxy', '') or '')[:16]
            for key in ('n', 'end_reason'):
                if key in rec_in:
                    keep[key] = rec_in[key] if key == 'end_reason' else int(
                        _num(rec_in.get(key), 0) or 0)
            rec['oracle'] = keep
            self._maybe_complete(rec, now)
        return True

    def note_banner(self, payload):
        """The game's own attributed shot-feedback verdict (``banner_verdict``)."""
        if not isinstance(payload, dict):
            return False
        if not int(_num(payload.get('attributed'), 1) or 0):
            return False
        ep = self._epoch(payload.get('release_seq'))
        now = self._clock()
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                self.orphan_banner += 1
                return False
            rec['banner'] = {
                'timing': str(payload.get('timing', '') or '')[:24],
                'timing_color': str(payload.get('timing_color', '') or '')[:16],
                'green': 1 if payload.get('green') else 0,
                'coverage': str(payload.get('coverage', '') or '')[:24] or None,
                # [ORION_BANNER_DISTANCE 2026-09-17] The game's own shot distance, read out
                # of the panel's DISTANCE cell (banner_distance.read_distance). This is the
                # free, exact RANGE label every graded shot carries -- the label
                # shot_range.py's calibration is measured against -- so it belongs in the
                # record whether or not the range classifier ever ships a verdict.
                'distance': str(payload.get('distance', '') or '')[:12] or None,
                'distance_ft': (_num(payload.get('distance_ft'))
                                if _num(payload.get('distance_ft'), -1.0) > 0 else None),
                'release_delay_ms': _num(payload.get('release_delay_ms')),
                'onset_ms': _num(payload.get('onset_ms')),
                'emit_latency_ms': _num(payload.get('emit_latency_ms')),
                'ncc': _num(payload.get('ncc')),
                'verdict_seq': int(_num(payload.get('seq'), 0) or 0),
            }
            self._maybe_complete(rec, now)
        return True

    # ------------------------------------------------------------- detect thread
    def note_icon_sample(self, epoch, t_ms, mean, dark_frac):
        """One 10 Hz nameplate-"3"-cell brightness sample (see the module docstring)."""
        ep = self._epoch(epoch)
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            cell = rec.get('icon_cell')
            if cell is None:
                cell = rec['icon_cell'] = []
                rec['icon_source'] = 'nameplate_cell_10hz'
            if len(cell) >= 120:            # 12 s at 10 Hz; a press cannot outlast that
                return False
            cell.append([round(float(t_ms), 1), round(float(mean), 2),
                         round(float(dark_frac), 3)])
        return True

    def note_range(self, epoch, payload, cells=None):
        """[ORION_SHOT_RANGE 2026-09-17] The press's THREE/MID reading (shot_range.py).

        Called from the range reader's own worker thread, ~press+120 ms, so it always lands
        while the record is open.  O(1) under the same lock every other note_* takes.
        """
        ep = self._epoch(epoch)
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            payload = payload or {}
            rec['range'] = str(payload.get('range', 'unknown') or 'unknown')
            rec['range_conf'] = _num(payload.get('conf'), 0.0)
            rec['range_source'] = str(payload.get('source', '') or 'unavailable')
            rec['range_reason'] = str(payload.get('reason', '') or '')
            rec['range_evidence'] = str(payload.get('evidence', 'unknown') or 'unknown')
            if cells:
                rec['range_cells'] = list(cells)[:8]
        return True

    def note_network(self, epoch, **fields):
        """[ORION_ONSET_FF 2026-09-21] The court RTT the sampler held at the PRESS.

        The engine's onset feedforward corrects a meter that shows up later than usual;
        whether that lateness is the network (a jittery court RTT) or the game's own
        sync logic is UNMEASURED because the RTT snapshot only ever reached the status
        panel. Stamped at the press, on the record and on the summary line, so a graded
        session can regress onset on it. Fields are stored as given; the summary line
        prints court_rtt_ms / court_jitter_ms / court_ready.
        """
        ep = self._epoch(epoch)
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            net = rec.get('network')
            if net is None:
                net = rec['network'] = {}
            net.update(fields)
        return True

    def note_frames(self, epoch, **fields):
        """Press-window framedump bookkeeping (dir / first+last index / drops)."""
        ep = self._epoch(epoch)
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            fd = rec.get('framedump')
            if fd is None:
                fd = rec['framedump'] = {}
            # Window close and worker completion can publish concurrently. A
            # late provisional snapshot must not erase a newer completed census.
            if int(fields.get('census_revision', 0)) < int(fd.get('census_revision', 0)):
                return False
            fd.update(fields)
            self._maybe_complete(rec, self._clock())
        return True

    def note_pose_track(self, epoch, track):
        """Reserved.  `track` is a list of [ts_ms, x, y, conf] x 17 per sampled frame."""
        ep = self._epoch(epoch)
        with self._lock:
            rec = self._open.get(ep)
            if rec is None:
                return False
            rec['pose_track'] = track
        return True

    # ------------------------------------------------------------------ internals
    def _recompute_tempo(self, rec):
        shot_type = rec.get('shot_type_upgraded') or rec.get('shot_type') or ''
        onset = _num(rec.get('onset_ms'))
        # No native-owned onset travels in this recorder's input. Do not label
        # the historical +36 ms approximation as a measured native value.
        rec['onset_engine_ms'] = None
        rec['tempo'] = 'unknown'
        if onset is None or onset < 0.0:
            rec['onset_engine_estimate_ms'] = None
            rec['tempo_estimate'] = 'unknown'
        else:
            engine = onset + self.onset_lag_ms
            rec['onset_engine_estimate_ms'] = round(engine, 2)
            rec['tempo_estimate'] = tempo_for(shot_type, engine)
        rec['tempo_key'] = tempo_key(shot_type, rec['tempo'])

    def _maybe_complete(self, rec, now_ms):
        """Finish a graded press once its optional bounded dump census arrives."""
        if rec['_complete']:
            return
        if rec['outcome'] == 'released' and rec['oracle'] is not None \
                and rec['banner'] is not None:
            fd = rec.get('framedump')
            if self.wait_for_framedump and (fd is None or fd.get('census_complete') is False):
                return
            rec['_complete'] = True
            self._finish(rec['epoch'], 'complete', now_ms)

    def _sweep(self, now_ms):
        """Close every record whose deadline has passed.  Caller holds the lock."""
        for ep in [e for e, r in self._open.items()
                   if float(r.get('_deadline_ms', 0.0)) <= now_ms]:
            reason = 'timeout' if self._open[ep]['outcome'] == 'unanswered' else 'grace'
            self._finish(ep, reason, now_ms)

    def tick(self, now_ms=None):
        """Deadline sweep.  Called by the writer thread; tests call it directly."""
        now = self._clock() if now_ms is None else float(now_ms)
        with self._lock:
            self._sweep(now)

    def close_all(self, reason='session_end'):
        now = self._clock()
        with self._lock:
            for ep in list(self._open):
                self._finish(ep, reason, now)

    def _finish(self, epoch, reason, now_ms):
        """Pop the record, stamp it and hand it to the writer.  Caller holds the lock."""
        rec = self._open.pop(self._epoch(epoch), None)
        if rec is None:
            return
        rec['closed_reason'] = str(reason)
        rec['closed_ts_ms'] = round(float(now_ms), 1)
        rec.pop('_deadline_ms', None)
        rec.pop('_complete', None)
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            self.records_dropped += 1
            # Coalesce overflow evidence; logging can block on stderr/disk and
            # must never happen on the shot-control thread or under its lock.
            self._drop_notice = (rec['epoch'], reason, self.records_dropped)

    # ------------------------------------------------------------------ writer
    def _report_drops(self):
        with self._lock:
            notice, self._drop_notice = self._drop_notice, None
        if notice is not None:
            try:
                self._log.error('SHOT RECORD DROPPED: epoch=%d reason=%s (writer queue full, '
                                'dropped=%d)', *notice)
            except Exception:
                pass  # diagnostic sinks never control recording or shutdown

    def _writer_loop(self):
        while True:
            try:
                item = self._q.get_nowait() if self._stop.is_set() else self._q.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    break
                self.tick()
                self._report_drops()
                continue
            if item is not None:
                with self._write_lock:
                    self._write(item)
            self._report_drops()
        self._report_drops()
        self._close_logged = True
        try:
            self._log.warning('SHOT RECORDS closed: presses=%d written=%d dropped=%d '
                              'errors=%d orphan_oracle=%d orphan_banner=%d -> %s',
                              self.presses, self.records_written, self.records_dropped,
                              self.write_errors, self.orphan_oracle, self.orphan_banner,
                              self.path)
        except Exception:
            pass

    def drain(self, block=False):
        """Explicit synchronous test drain, never a second concurrent writer."""
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            return 0
        if not self._write_lock.acquire(blocking=bool(block)):
            return 0
        n = 0
        try:
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                self._write(item)
                n += 1
            self._report_drops()
            return n
        finally:
            self._write_lock.release()

    def _write(self, rec):
        started = time.perf_counter()
        try:
            line = json.dumps(rec, separators=(',', ':'), default=str) + '\n'
        except Exception as exc:
            self.write_errors += 1
            self._log.error('SHOT RECORD serialise failed: %s', exc)
            return False
        try:
            # APPEND, never truncate: a mid-session sidecar restart must extend the
            # session's corpus, not erase it (the framedump index learned this the
            # hard way on 2026-08-26).
            with open(self.path, 'a', encoding='utf-8', newline='') as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:
            self.write_errors += 1
            self._log.error('SHOT RECORD write failed (%s): %s', self.path, exc)
            return False
        self.records_written += 1
        self.write_ms_total += (time.perf_counter() - started) * 1000.0
        self._emit_line(rec)
        return True

    def _emit_line(self, rec):
        """One greppable ERROR line per record.

        ERROR, not WARNING, for the same reason PICKUP: is: RemotePlaySession relays
        sidecar stderr and every sidecar WARNING shares ONE global 1000 ms throttle
        slot, which the orchestrator's own SHOT-GATE receipts already occupy on every
        press.  ERROR/CRITICAL bypass it.
        """
        try:
            oracle = rec.get('oracle') or {}
            banner = rec.get('banner') or {}
            network = rec.get('network') or {}
            self._log.error(
                'SHOT RECORD: epoch=%d type=%s tempo=%s onset_ms=%s release_ms=%s '
                'oracle_gap=%s banner=%s icon_off_ms=%s outcome=%s closed=%s range=%s '
                'dist=%s court_rtt_ms=%s court_jitter_ms=%s court_ready=%s',
                rec.get('epoch', 0),
                (rec.get('shot_type_upgraded') or rec.get('shot_type') or 'unclassified'
                 ).replace(' ', '_'),
                rec.get('tempo', 'normal'),
                _fmt(rec.get('onset_ms')), _fmt(rec.get('release_after_press_ms')),
                _fmt(oracle.get('gap_px')), banner.get('timing') or '-',
                _fmt(rec.get('icon_off_ms')), rec.get('outcome', '?'),
                rec.get('closed_reason', '?'), rec.get('range', 'unknown'),
                banner.get('distance') or '-',
                # [ORION_ONSET_FF 2026-09-21] APPENDED after dist=, so every key=value
                # reader is unaffected; '-' when the sampler had nothing at the press.
                _fmt(network.get('court_rtt_ms')), _fmt(network.get('court_jitter_ms')),
                network.get('court_ready', '-'))
        except Exception:
            pass


def _fmt(value):
    return '-' if value is None else ('%.1f' % float(value))
