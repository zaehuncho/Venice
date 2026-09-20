"""shot_range.py -- is the live press a THREE or a MID-RANGE shot?

WHY THIS EXISTS.  2026-09-16 night: 13 EXCELLENT / 8 LATE / 3 EARLY on FADES, all of them
sharing one (type, tempo) bucket, and several of the earlies "filled right before the green
window".  The owner's own reading is that a mid-range fade and a three-point fade are
different animations with different windows -- "middy fades should perform just as
standstill shots (slightly bigger window); I'm fine with 50-60 % for three-point fades" --
so one trim and one fixed offset cannot serve both.  RANGE is the third trim dimension the
engine keys fades on (``BannerLeadTrim``); this module is the sidecar half that measures it.

WHAT THE SIGNAL IS.  The 2026-09-14 landmark study (docs/HANDOFF_2026-09-14_UI_POLISH.md,
tools/diagnostics/hud_3pt_icon.py) established that NBA 2K27 draws a small black disc with a
bold white "3" as the LEFT cell of the ball handler's nameplate -- immediately left of the
PlayStation-logo disc and the gamertag -- and that the cell means exactly "this player has
the ball AND is behind the arc".  It is present throughout the hold and disappears ~163 ms
AFTER the release.  So a cheap read of that one cell a few frames after the press says
whether this shot is a three, and it says it ~500 ms before the release needs the lead.

WHAT THIS MODULE DOES NOT DO.  It never searches for the plate.  ``hud_3pt_icon.probe()`` is
an 8-scale ``matchTemplate`` sweep over a 566-row band and measures **157.7 ms per frame** on
this workstation -- ten frame times at 60 fps.  This module reads the cell at whatever plate
``player_anchor`` already found on the frame (or at its last sighting, while that is younger
than ``plate_age_ms``), and if there is no plate it reports ``unknown``.  A range it cannot
measure must degrade to today's behaviour, never to a guess.

THREADING. ``note_frame`` runs on the DETECT thread and stores a frame REFERENCE plus a
scalar plate/measurement-clock snapshot. It never reads pixels or searches for a plate.
Every pixel read, the classification and the stdout emit happen on the daemon worker, off
the detect thread entirely (the same shape ``banner_verdict_live`` and the shot recorder use).

THE MESSAGE.  One line per press on the existing sidecar->native stdout JSONL channel, the
same channel ``release_oracle`` and ``banner_verdict`` ride (there is no sidecar->native
shot-gate message: ``arm_shot_gate`` is a native->sidecar call and its ``SHOT-GATE ARM
RECEIPT`` is a sidecar-side log line, not a reply):

    {"event":"shot_range","release_seq":<shot-gate epoch>,"range":"three|mid|unknown",
     "conf":0.0-1.0,"samples":n,"source":"...","t_ms":<wall ms>,
     "dark":f,"bright":f,"mean":f,"evidence":"three|mid|unknown"}

``release_seq``/``range``/``conf`` are the contract; everything else is forensics that the
engine reads past, exactly as it does for the oracle's ``gap_pct``/``settled_fill``.

== CALIBRATION STATUS: NOT ARMED -- now for a MEASURED reason (2026-09-17, second pass) =======
The offline calibration this feature was specified to pass -- classify against
``banner_verdict_live``'s DISTANCE cell and require >= 90 % agreement -- has now been RUN,
end to end, with both of the excuses removed.  It scores **38.5 %**.  The shipped default
therefore still reports ``unknown`` and the engine keeps today's buckets and today's +-8 ms
fade offset.

What the first 2026-09-17 pass could not do, and what changed:

  * the DISTANCE cell was never recorded.  It is now: ``banner_distance.read_distance``
    reads it (``23'5"`` -> 23.417 ft) and ``banner_verdict_live`` forwards ``distance`` /
    ``distance_ft`` into the verdict and the shot record.  Measured 66/66 hand-labelled
    panels, 1842/1845 frames, 0.25 ms (tools/diagnostics/banner_distance_study.py).
  * the press-window dump ended at release+400 ms, before the banner ever landed.  It now
    runs to release+1700 ms (ORION_FRAMEDUMP_PRESS_BANNER).
  * ``player_anchor`` locked the owner's plate on 2 of 360 pickups.  After the
    ORION_ANCHOR_ACQUIRE pass it locks 66-93 % of the presses whose plate is on screen.

So the matrix was built on the 09-12 corpus, where the panel events are already cut and the
press table exists: 57 panels with a readable distance, 26 joined to a press with >= 2 cell
samples (tools/diagnostics/shot_range_confusion.py).  The result is not a near miss:

    label \\ verdict    three   mid   unknown
    three (>= 22 ft)      10     3       0
    mid                   13     0       0        -> 10/26 = 38.5 %

and the two populations do not separate on the statistic at all --
``three`` dark p10/p50/p90 = 0.009/0.466/0.541 against ``mid`` 0.396/0.485/0.521, and
bright 0.214/0.321/0.898 against 0.234/0.334/0.384.  The classifier calls nearly everything
a three because the cell is dark and has a bright glyph on a mid-range shot too: at the
sampled scale that ROI is the plate's own ``[3]``-cell POSITION, not a read of whether the
icon is drawn there.

So the path stays wired, measured and RECORDED -- every press writes its cell samples into
the shot-record JSONL, and every graded shot now writes the game's own distance beside them,
which is what a real calibration needs -- but the VERDICT is ``unknown`` until
``ORION_SHOT_RANGE_CALIBRATED=1`` arms it.  Arming it on 38.5 % would be exactly the "ship a
guess" this refuses.  The obvious next lead is not a better threshold on this ROI: it is that
the banner's OWN distance is an exact label one shot late, which an engine-side trim can key
on directly without any cell read at all.

-- RE-MEASURED 2026-09-19, with the anchor locking, and the answer is the same ----------------
The 09-17 excuse was "the anchor found a plate on 2 of 360 pickups", so the matrix had 26
joined presses.  After the ORION_ANCHOR_ACQUIRE and ORION_ANCHOR_COARSE_PER_STRIP passes the
anchor locks the owner's plate on 90-100 % of the presses whose plate is on screen, and the
live sampler has written ``range_cells`` on **102** presses that also carry the game's own
``banner.distance_ft``.  That is a 4x corpus on the same statistic
(tools/diagnostics/shot_range_confusion.py --live-records):

    label \\ verdict    three   mid   unknown
    three (>= 22 ft)      79     9       0
    mid                   14     0       0        -> 79/102 = 77.5 %

    three  precision 0.849 (79/93)   recall 0.898 (79/88)
    mid    precision 0.000 (0/9)     recall 0.000 (0/14)

    three  dark p10/p50/p90 0.284/0.493/0.561   bright 0.253/0.355/0.446
    mid    dark p10/p50/p90 0.423/0.536/0.568   bright 0.251/0.338/0.412

MID RECALL IS ZERO, and the apparent 77.5 % is only the 86:14 class imbalance of a corpus of
three-point drills.  The two populations still do not separate -- the `mid` rows are, if
anything, DARKER than the `three` rows -- and the 9 "three -> mid" calls are plate misreads
(dark 0.0 with bright 0.0 / 0.816 / 1.0), not an icon that was absent.

The reason is now visible rather than inferred.  The nameplate strips were cut at the plate
the anchor found for the sub-arc presses that still have frames on disk
(D:/NexusVision/anchor_acq_2026-09-19/plates): at **17.2 ft**, at 19.7, at 20.1 and at 21.1 ft
the plate reads ``[3][PS][trimuzis]`` with the "3" cell plainly drawn.  So on the owner's
court the cell is NOT a behind-the-arc flag at the moment of the press -- whatever it marks,
it is drawn on shots the game itself scores at 17 ft.  No threshold on that ROI can produce
`mid`, and no better ROI geometry is the fix either: the icon is there.

The verdict therefore stays ``unknown``, and the next lead is unchanged and now the only one
left standing: the banner's own DISTANCE is an exact label one shot late.
==============================================================================================

KNOBS
    ORION_SHOT_RANGE                1     master switch
    ORION_SHOT_RANGE_CALIBRATED     0     report the measured verdict instead of `unknown`
    ORION_SHOT_RANGE_LO_MS          40    first sample, ms after the physical press
    ORION_SHOT_RANGE_HI_MS          120   last sample
    ORION_SHOT_RANGE_SAMPLES        3     at most this many frames per press
    ORION_SHOT_RANGE_MIN_SAMPLES    2     fewer than this is `unknown`, never a verdict
    ORION_SHOT_RANGE_DARK_MIN       0.28  the icon's near-black disc
    ORION_SHOT_RANGE_BRIGHT_MIN     0.12  ...with a bright glyph inside it
    ORION_SHOT_RANGE_PLATE_AGE_MS   1000  how stale player_anchor's last plate may be
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

logger = logging.getLogger('ShotRange')

EVENT = 'shot_range'

RANGE_THREE = 'three'
RANGE_MID = 'mid'
RANGE_UNKNOWN = 'unknown'
RANGES = (RANGE_THREE, RANGE_MID, RANGE_UNKNOWN)

# tools/diagnostics/hud_3pt_icon.py: the "3" disc sits one disc-pitch LEFT of the PS-logo
# disc that player_anchor tracks, 25 px at scale 1.0.
PITCH_PX = 25.0
# ...and the same file's DARK_T / BRIGHT_T, so the live cell and the offline study agree on
# what "the icon is there" means.
DARK_T = 75
BRIGHT_T = 165

DEFAULT_LO_MS = 40.0
DEFAULT_HI_MS = 120.0
DEFAULT_SAMPLES = 3
DEFAULT_MIN_SAMPLES = 2
DEFAULT_DARK_MIN = 0.28
DEFAULT_BRIGHT_MIN = 0.12
DEFAULT_PLATE_AGE_MS = 1000.0


def _env_float(name, default, env=None):
    env = env if env is not None else os.environ
    try:
        return float(str(env.get(name, '')).strip())
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default, env=None):
    return int(_env_float(name, default, env))


def _env_flag(name, default, env=None):
    env = env if env is not None else os.environ
    raw = str(env.get(name, default) or default).strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


# --------------------------------------------------------------------------- geometry
def cell_box(icon_x, icon_y, scale, width, height):
    """The "3" cell's pixel box for a plate whose PS disc is at (icon_x, icon_y).

    Byte-identical geometry to the shot recorder's own nameplate sampler, so the two
    instruments read the SAME pixels and a record's `icon_cell` series and its `range` can
    never disagree about where the cell was.  Returns None when the box does not fit.
    """
    try:
        scale = float(scale) if scale else 1.0
        half = max(6.0, 11.0 * scale)
        cx = float(icon_x) - PITCH_PX * scale
        x0 = int(max(0, cx - half))
        x1 = int(min(int(width), cx + half))
        y0 = int(max(0, float(icon_y) - half))
        y1 = int(min(int(height), float(icon_y) + half))
    except (TypeError, ValueError):
        return None
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return (x0, y0, x1, y1)


def cell_stats(frame, icon_x, icon_y, scale):
    """-> (mean, dark_frac, bright_frac) for the "3" cell, or None.

    ROI arithmetic only: one 22x22 slice, one mean and two comparisons.  No template match,
    no search, no full-frame conversion.
    """
    if frame is None:
        return None
    try:
        shape = frame.shape
        box = cell_box(icon_x, icon_y, scale, shape[1], shape[0])
        if box is None:
            return None
        x0, y0, x1, y1 = box
        cell = frame[y0:y1, x0:x1]
        if getattr(cell, 'size', 0) == 0:
            return None
        peak = cell.max(axis=2) if cell.ndim == 3 else cell
        return (float(cell.mean()), float((peak < DARK_T).mean()),
                float((peak > BRIGHT_T).mean()))
    except Exception:
        return None


# --------------------------------------------------------------------------- policy
class ShotRangeClassifier:
    """Pure policy: cell samples in, (range, conf) out.  No pixels, no threads, no I/O."""

    def __init__(self, dark_min=DEFAULT_DARK_MIN, bright_min=DEFAULT_BRIGHT_MIN,
                 min_samples=DEFAULT_MIN_SAMPLES, calibrated=False):
        self.dark_min = float(dark_min)
        self.bright_min = float(bright_min)
        self.min_samples = max(1, int(min_samples))
        # See the module docstring: the 2026-09-17 offline pass did not clear the 90 %
        # agreement bar, so the shipped default REPORTS the evidence and VERDICTS unknown.
        self.calibrated = bool(calibrated)

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ
        return cls(dark_min=_env_float('ORION_SHOT_RANGE_DARK_MIN', DEFAULT_DARK_MIN, env),
                   bright_min=_env_float('ORION_SHOT_RANGE_BRIGHT_MIN',
                                         DEFAULT_BRIGHT_MIN, env),
                   min_samples=_env_int('ORION_SHOT_RANGE_MIN_SAMPLES',
                                        DEFAULT_MIN_SAMPLES, env),
                   calibrated=_env_flag('ORION_SHOT_RANGE_CALIBRATED', '0', env))

    def sample_range(self, mean, dark, bright):
        """One cell reading -> three | mid.  The icon is a near-black disc with a bright
        glyph: present (dark AND bright) = the handler is behind the arc."""
        try:
            return (RANGE_THREE
                    if (float(dark) >= self.dark_min and float(bright) >= self.bright_min)
                    else RANGE_MID)
        except (TypeError, ValueError):
            return RANGE_MID

    def classify(self, samples):
        """`samples` = [(t_ms, mean, dark, bright), ...] -> dict.

        A MAJORITY of the window's frames, never one frame: the icon does not flicker, so a
        split vote is a bad read (a plate that drifted, a frame the ball crossed the cell)
        and a bad read must be `unknown`, not a coin flip.
        """
        rows = []
        for s in samples or ():
            try:
                rows.append((float(s[1]), float(s[2]), float(s[3])))
            except (TypeError, ValueError, IndexError):
                continue
        n = len(rows)
        out = {
            'range': RANGE_UNKNOWN,
            'evidence': RANGE_UNKNOWN,
            'conf': 0.0,
            'samples': n,
            'reason': 'no_samples',
            'mean': None,
            'dark': None,
            'bright': None,
        }
        if n == 0:
            return out
        out['mean'] = round(sum(r[0] for r in rows) / n, 2)
        out['dark'] = round(sum(r[1] for r in rows) / n, 3)
        out['bright'] = round(sum(r[2] for r in rows) / n, 3)
        if n < self.min_samples:
            out['reason'] = 'too_few_samples'
            return out
        votes = [self.sample_range(*r) for r in rows]
        three = votes.count(RANGE_THREE)
        mid = n - three
        if three == mid:
            out['reason'] = 'split'
            return out
        out['evidence'] = RANGE_THREE if three > mid else RANGE_MID
        out['conf'] = round(max(three, mid) / float(n), 3)
        if not self.calibrated:
            # The evidence is recorded; the VERDICT is withheld. Arming this without a fresh
            # confusion matrix would ship a guess into the lead.
            out['reason'] = 'uncalibrated'
            out['conf'] = 0.0
            return out
        out['range'] = out['evidence']
        out['reason'] = 'vote'
        return out


# --------------------------------------------------------------------------- live reader
class ShotRangeReader:
    """The live half: a per-press frame window on the detect thread, everything else on a
    daemon worker.

    `plate_fn()` returns (icon_x, icon_y, scale, ts) -- player_anchor's own last
    sighting on the detector measurement clock -- or None. Production supplies that
    epoch clock explicitly to note_frame, separately from the monotonic press clock.
    It is injected so tests never need a capture device or a template.
    """

    def __init__(self, classifier=None, emit=None, plate_fn=None, on_result=None,
                 lo_ms=DEFAULT_LO_MS, hi_ms=DEFAULT_HI_MS, max_samples=DEFAULT_SAMPLES,
                 plate_age_ms=DEFAULT_PLATE_AGE_MS, log=None, start_thread=True):
        self.classifier = classifier or ShotRangeClassifier()
        self._emit = emit
        self._plate_fn = plate_fn
        self._on_result = on_result
        self.lo_ms = float(lo_ms)
        self.hi_ms = max(float(lo_ms), float(hi_ms))
        self.max_samples = max(1, int(max_samples))
        self.plate_age_ms = float(plate_age_ms)
        self._log = log or logger
        self._lock = threading.Lock()
        self._epoch = 0
        self._press_mono_ms = 0.0
        self._frames = []          # [(t_ms, frame, plate, plate_clock)] -- bounded references
        self._last_frame_clock = 0.0
        self._last_measurement_clock = 0.0
        self._sent = 0
        self._q = queue.Queue(maxsize=8)
        self._thread = None
        self.presses = 0
        self.emitted = 0
        self.dropped = 0
        if start_thread:
            self.start()

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, emit=None, plate_fn=None, on_result=None, log=None, env=None,
               start_thread=True):
        """Build the session's reader, or None when ORION_SHOT_RANGE=0.  Never raises."""
        log = log or logger
        env = env if env is not None else os.environ
        try:
            explicit = 'ORION_SHOT_RANGE' in env
            if not _env_flag('ORION_SHOT_RANGE', '1', env):
                log.info('shot range: OFF (ORION_SHOT_RANGE=%s)',
                         env.get('ORION_SHOT_RANGE'))
                return None
            # Same rule the shot recorder uses: a suite that happens to build a real
            # orchestrator must not start a worker thread or write JSONL on the sidecar's
            # stdout IPC channel. The dedicated suites set the switch on purpose.
            if not explicit and (env.get('PYTEST_CURRENT_TEST') or 'pytest' in sys.modules):
                return None
            clf = ShotRangeClassifier.from_env(env)
            rdr = cls(classifier=clf, emit=emit, plate_fn=plate_fn, on_result=on_result,
                      lo_ms=_env_float('ORION_SHOT_RANGE_LO_MS', DEFAULT_LO_MS, env),
                      hi_ms=_env_float('ORION_SHOT_RANGE_HI_MS', DEFAULT_HI_MS, env),
                      max_samples=_env_int('ORION_SHOT_RANGE_SAMPLES', DEFAULT_SAMPLES, env),
                      plate_age_ms=_env_float('ORION_SHOT_RANGE_PLATE_AGE_MS',
                                              DEFAULT_PLATE_AGE_MS, env),
                      log=log, start_thread=start_thread)
            log.warning('SHOT RANGE armed: window=press+%.0f..%.0fms samples<=%d '
                        'dark>=%.2f bright>=%.2f calibrated=%d',
                        rdr.lo_ms, rdr.hi_ms, rdr.max_samples, clf.dark_min,
                        clf.bright_min, int(clf.calibrated))
            return rdr
        except Exception as exc:
            log.warning('shot range DISABLED: %s', exc, exc_info=True)
            return None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker_loop, name='shot-range',
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=0.5):
        thread = self._thread
        self._thread = None
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ------------------------------------------------------------- control thread
    def note_press(self, epoch, press_mono_ms):
        """A physical Square edge.  O(1) under one lock; called from arm_shot_gate."""
        try:
            ep = int(epoch)
        except (TypeError, ValueError):
            return False
        if ep <= 0:
            return False
        with self._lock:
            if ep == self._epoch:
                return False           # the native's type_upgrade re-arm: same press
            self._epoch = ep
            self._press_mono_ms = float(press_mono_ms)
            self._frames = []
            self._last_frame_clock = 0.0
            self._last_measurement_clock = 0.0
            self._sent = 0
            self.presses += 1
        return True

    def note_close(self, epoch):
        """The press ended (release or disarm).  Flushes whatever the window collected --
        a short hold can end before the worker has been handed the window."""
        try:
            ep = int(epoch)
        except (TypeError, ValueError):
            return False
        return self._flush(ep, 'close')

    # -------------------------------------------------------------- detect thread
    def note_frame(self, frame, now_s, *, measurement_epoch_s=None):
        """Queue a frame plus an immutable plate/measurement-clock snapshot.

        The frame is kept by REFERENCE: capture already isolates every frame (the same
        contract the press-window framedump's pre-roll ring relies on), so this costs a
        pointer and never a 1080p copy on the detect thread. ``now_s`` remains the
        monotonic press-window clock. Production supplies the epoch measurement stamp
        used by the locator separately; legacy callers use one clock for both. Missing
        explicit stamps never fall back to wall now or imply a calibrated verdict.
        """
        try:
            now_s = float(now_s)
            measurement_s = (now_s if measurement_epoch_s is None
                             else float(measurement_epoch_s))
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(now_s) or now_s <= 0.0:
            return False
        with self._lock:
            ep = self._epoch
            if ep <= 0 or self._sent:
                return False
            t_ms = float(now_s) * 1000.0 - self._press_mono_ms
            if t_ms < self.lo_ms:
                return False
            if t_ms > self.hi_ms or len(self._frames) >= self.max_samples:
                ready = (ep, float(now_s), self._frames)
                self._sent = 1
            else:
                if (not math.isfinite(measurement_s) or measurement_s <= 0.0
                        or now_s <= self._last_frame_clock
                        or measurement_s <= self._last_measurement_clock):
                    return False
                self._last_frame_clock = now_s
                self._last_measurement_clock = measurement_s
                # Anchor acquisition is asynchronous. Freeze the tuple now, rather
                # than reading a newer pose after this job waited on the worker.
                self._frames.append((t_ms, frame, self._plate_snapshot(), measurement_s))
                if len(self._frames) < self.max_samples:
                    return True
                ready = (ep, float(now_s), self._frames)
                self._sent = 1
        self._offer(ready)
        return True

    # ------------------------------------------------------------------ internals
    def _flush(self, epoch, reason):
        with self._lock:
            if epoch != self._epoch or self._sent:
                return False
            ready = (self._epoch, None, self._frames)
            self._sent = 1
        self._offer(ready)
        return True

    def _offer(self, ready):
        try:
            self._q.put_nowait(ready)
        except queue.Full:
            self.dropped += 1

    def _worker_drain(self):
        """Process everything queued, on THIS thread.  The worker's body, called
        synchronously: the tests use it instead of racing a daemon, and stop() uses it so a
        session teardown does not lose the last press's reading."""
        done = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return done
            if item is None:
                continue
            try:
                self._process(item)
                done += 1
            except Exception as exc:
                self._log.debug('shot range drain failed: %s', exc)

    def _worker_loop(self):
        while True:
            try:
                item = self._q.get()
            except Exception:
                return
            if item is None:
                return
            try:
                self._process(item)
            except Exception as exc:
                self._log.debug('shot range worker failed: %s', exc)

    def _process(self, item):
        epoch, now_s, frames = item
        samples, source = self._sample(frames, now_s)
        result = self.classifier.classify(samples)
        result['source'] = source
        result['cells'] = [[round(s[0], 1), round(s[1], 2), round(s[2], 3), round(s[3], 3)]
                           for s in samples]
        self._publish(epoch, result)

    def _plate_snapshot(self):
        """Copy only scalar metadata; never trigger acquisition or touch pixels."""
        try:
            plate = self._plate_fn() if self._plate_fn is not None else None
            if plate is None:
                return None
            values = tuple(float(plate[i]) for i in range(4))
            return values if all(math.isfinite(v) for v in values) else None
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        except Exception:
            return None

    def _sample(self, frames, now_s=None):
        """Read the cell on each kept frame.  Runs on the worker; never on the detect loop.

        Each frame carries the exact measurement clock given to the locator and
        a copied plate tuple. No worker clock or mutable latest anchor is consulted.
        ``now_s`` is retained for the queue's diagnostic shape, not freshness.
        """
        samples = []
        failure_source = 'no_plate'
        for t_ms, frame, plate, measurement_s in frames:
            if plate is None:
                continue
            icon_x, icon_y, scale, plate_ts = plate
            age_ms = (measurement_s - plate_ts) * 1000.0
            if plate_ts <= 0.0 or not (-16.7 <= age_ms <= self.plate_age_ms):
                failure_source = 'plate_stale'
                continue
            failure_source = 'cell_unreadable'
            st = cell_stats(frame, icon_x, icon_y, scale)
            if st is not None:
                samples.append((t_ms, st[0], st[1], st[2]))
        if not samples:
            return [], failure_source
        return samples, 'anchor'

    def _publish(self, epoch, result):
        payload = {
            'event': EVENT,
            'release_seq': int(epoch),
            'range': str(result.get('range', RANGE_UNKNOWN)),
            'conf': float(result.get('conf', 0.0)),
            'samples': int(result.get('samples', 0)),
            'source': str(result.get('source', '')),
            'reason': str(result.get('reason', '')),
            'evidence': str(result.get('evidence', RANGE_UNKNOWN)),
            'mean': result.get('mean'),
            'dark': result.get('dark'),
            'bright': result.get('bright'),
            't_ms': round(time.time() * 1000.0, 1),
        }
        self.emitted += 1
        if self._on_result is not None:
            try:
                self._on_result(epoch, payload, result.get('cells') or [])
            except Exception as exc:
                self._log.debug('shot range sink failed: %s', exc)
        if self._emit is not None:
            try:
                self._emit(json.dumps(payload, separators=(',', ':')) + '\n')
            except Exception as exc:
                self._log.debug('shot range emit failed: %s', exc)
        # One greppable line per press, at INFO: the native relays the JSON above, so this is
        # for the sidecar's own log only.
        self._log.info('SHOT RANGE: epoch=%d range=%s evidence=%s conf=%.2f samples=%d '
                       'source=%s reason=%s dark=%s bright=%s',
                       int(epoch), payload['range'], payload['evidence'], payload['conf'],
                       payload['samples'], payload['source'], payload['reason'],
                       payload['dark'], payload['bright'])
        return payload


def anchor_plate():
    """player_anchor's last plate sighting as (icon_x, icon_y, scale, ts), or None.

    READ ONLY.  The anchor's own state is owned by the meter locator; this never writes it
    and never triggers a search of its own.
    """
    try:
        import player_anchor as _pa
        last = getattr(_pa.ANCHOR, '_last', None)   # noqa: SLF001 -- same-process read
        if not last:
            return None
        return (float(last[0]), float(last[1]), float(last[2] or 1.0), float(last[3]))
    except Exception:
        return None
