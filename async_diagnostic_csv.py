"""Bounded best-effort CSV output; never perform filesystem I/O on the producer.

Rows arrive already formatted with their producer/source timestamps.  Saturation
drops NEW rows rather than blocking or manufacturing replacement timestamps.
This module owns diagnostics only; dropped records must not affect live control.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path


class CsvByteLimitReached(Exception):
    """The recorder exhausted its explicit byte budget (not a capture failure)."""


class AsyncDiagnosticCsv:
    """Single-use, bounded FIFO with an independently bounded close operation.

    ``max_bytes`` is a PER-FILE budget, not the end of the recording.  Reaching it used to raise
    CsvByteLimitReached straight into ``_fail()``, which stopped the recorder for the rest of the
    session: on 2026-09-14 ``logs/diagnostics/detframes.csv`` hit the 64 MiB default at 21:46:27 and
    the last seven minutes of a two-hour measurement session -- the minutes that contained the
    aborts being investigated -- were never recorded, with the only notice a throttled WARNING that
    never reached the native log.  The budget now ROTATES: the full file is renamed
    ``<stem>_<ts>_partN<suffix>`` and a fresh one continues from the header.  The session stops only
    when ``max_parts`` is exhausted (an explicit, bounded disk budget) or a single record cannot fit
    in an empty file.
    """

    def __init__(self, path, header, *, keep=24, capacity=256, report=None,
                 opener=None, report_interval=2.0, max_bytes=64 * 1024 * 1024,
                 max_parts=16, flush_interval=2.0, report_critical=None):
        self.path = Path(path)
        self.header = str(header).rstrip("\r\n") + "\n"
        self.keep = max(1, int(keep))
        self.capacity = max(1, min(2048, int(capacity)))
        self.max_bytes = max(1, int(max_bytes))
        self.max_parts = max(1, min(4096, int(max_parts)))
        self._report = report
        # Lifecycle events (rotation / disable / close) go out through this channel so a caller can
        # route them somewhere that is not sampled.  The native parent relays sidecar WARNING lines
        # through ONE shared 1 Hz slot (RemotePlaySession.cpp kSidecarWarnThrottleMs) that DETDIAG
        # saturates at 25 lines/s, so a once-per-session WARNING is lost essentially at random;
        # ERROR bypasses that throttle.  Falls back to `report` when not supplied.
        self._report_critical = report_critical or report
        self._opener = opener or open
        self._report_interval = max(0.0, float(report_interval))
        # Bounded flush cadence.  The file is line-buffered, so rows already reach the OS per row;
        # this is the explicit upper bound on how much a non-line-buffered opener (or a future
        # buffering change) could hold back, and it is what makes "the last minute is never lost"
        # a property of the writer rather than of the buffering mode.
        self._flush_interval = max(0.0, float(flush_interval))
        self._condition = threading.Condition()
        self._pending = deque()
        self._accepting = True
        self._closing = False
        self._accepted = 0
        self._written = 0
        self._dropped_full = 0
        self._dropped_shutdown = 0
        self._dropped_error = 0
        self._dropped_limit = 0
        self._dropped_invalid = 0
        self._dropped_stopped = 0
        self._bytes_written = 0
        self._total_bytes = 0
        self._parts = 1
        self._rotations = 0
        self._error = ""
        self._inflight = 0
        self._thread = threading.Thread(target=self._run, name="detcsv-writer", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._accepting = False
            self._closing = True
            raise

    def write(self, row):
        """Accept one bounded row without waiting for the disk/worker/logger.

        A brief queue lock is the only synchronization.  It is NEVER held by the
        worker during open/rotation/write/flush/close or diagnostic reporting.
        """
        with self._condition:
            if not self._accepting:
                # Counted, so the closing report says how many rows the session lost AFTER the
                # recorder stopped -- the number that was missing when detframes.csv ended at
                # 21:46 and nobody could tell whether 7 minutes or 70 had gone unrecorded.
                self._dropped_stopped += 1
                return False
            # The actual schema is <1 KiB/row.  Bound memory even if a producer
            # accidentally hands this sink a frame/blob or malformed payload.
            if not isinstance(row, str) or len(row) > 16384:
                self._dropped_invalid += 1
                return False
            if len(self._pending) >= self.capacity:
                self._dropped_full += 1
                return False
            self._pending.append(row)
            self._accepted += 1
            self._condition.notify()
            return True

    def snapshot(self):
        with self._condition:
            return {
                "accepted": self._accepted,
                "written": self._written,
                "queued": len(self._pending),
                "inflight": self._inflight,
                "dropped_full": self._dropped_full,
                "dropped_shutdown": self._dropped_shutdown,
                "dropped_error": self._dropped_error,
                "dropped_limit": self._dropped_limit,
                "dropped_invalid": self._dropped_invalid,
                "dropped_stopped": self._dropped_stopped,
                "bytes_written": self._bytes_written,
                "total_bytes": self._total_bytes,
                "max_bytes": self.max_bytes,
                "parts": self._parts,
                "max_parts": self.max_parts,
                "rotations": self._rotations,
                "error": self._error,
                "accepting": self._accepting,
            }

    def is_running(self):
        return self._thread.is_alive()

    def close(self, timeout=0.5):
        """Stop accepting; drain normally, discard queued rows at the time bound.

        A blocked OS write is never force-closed by this thread.  The daemon owns
        its file until that one operation returns, then closes it itself.  A
        False result explicitly means shutdown did not finish inside the bound.
        """
        with self._condition:
            self._accepting = False
            self._closing = True
            self._condition.notify_all()
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(max(0.0, float(timeout)))
        if not self._thread.is_alive():
            return True
        with self._condition:
            self._dropped_shutdown += len(self._pending)
            self._pending.clear()
            self._condition.notify_all()
        return False

    def _emit(self, event, critical=False):
        sink = self._report_critical if critical else self._report
        if sink is None:
            return
        s = self.snapshot()
        # Only fixed event names, numeric counters and exception CLASS names are
        # reported.  No row payload, key, or raw filesystem exception is logged.
        try:
            sink(
                "DETCSV %s accepted=%d written=%d queued=%d inflight=%d "
                "drop_full=%d drop_shutdown=%d drop_error=%d drop_limit=%d "
                "drop_invalid=%d drop_stopped=%d bytes=%d/%d total_bytes=%d "
                "part=%d/%d file=%s error=%s"
                % (event, s["accepted"], s["written"], s["queued"], s["inflight"],
                   s["dropped_full"], s["dropped_shutdown"], s["dropped_error"],
                   s["dropped_limit"], s["dropped_invalid"], s["dropped_stopped"],
                   s["bytes_written"], s["max_bytes"], s["total_bytes"],
                   s["parts"], s["max_parts"], self.path.name, s["error"] or "none"))
        except Exception:
            pass # A diagnostic logger failure cannot disable capture.

    def _archive_name(self, suffix_hint=""):
        """A free sibling path of the form ``<stem>_<ts><hint><suffix>``.

        Two rotations inside the same second must not overwrite one another, so a serial is added
        until the name is free -- the same rule the start-of-session archive already used.
        """
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        base = self.path.stem + "_" + stamp + suffix_hint
        target = self.path.with_name(base + self.path.suffix)
        serial = 0
        while target.exists():
            serial += 1
            target = self.path.with_name(base + "_%03d" % serial + self.path.suffix)
        return target

    def _prune(self):
        old = sorted(self.path.parent.glob(self.path.stem + "_*" + self.path.suffix),
                     key=lambda p: (p.stat().st_mtime_ns, p.name))
        for p in old[:-self.keep]:
            p.unlink()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve per-session history, including two reconnects within one
        # second.  The old unconditional os.replace could overwrite that sibling.
        if self.path.exists() and self.path.stat().st_size:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.path.stat().st_mtime))
            target = self.path.with_name(self.path.stem + "_" + stamp + self.path.suffix)
            serial = 0
            while target.exists():
                serial += 1
                target = self.path.with_name(self.path.stem + "_" + stamp + "_%03d" % serial + self.path.suffix)
            os.replace(self.path, target)
        self._prune()
        return self._opener(self.path, "w", buffering=1, encoding="utf-8", newline="")

    def _rotate(self, fh, nbytes):
        """Retire the full file and continue in a fresh one; return the new handle.

        Raises CsvByteLimitReached only when continuing is impossible: the part budget is spent, or
        the record would not fit in an EMPTY file (which would rotate forever).  Everything else --
        including the whole point of this method -- keeps the recording alive.
        """
        header_bytes = len(self.header.encode("utf-8"))
        if self._parts >= self.max_parts:
            raise CsvByteLimitReached()
        if header_bytes + nbytes > self.max_bytes:
            raise CsvByteLimitReached()
        try:
            fh.flush()
        finally:
            fh.close()
        target = self._archive_name("_part%02d" % self._parts)
        os.replace(self.path, target)
        self._prune()
        new_fh = self._opener(self.path, "w", buffering=1, encoding="utf-8", newline="")
        with self._condition:
            self._bytes_written = 0
            self._parts += 1
            self._rotations += 1
        self._write_bounded(new_fh, self.header)
        self._emit("rotated->" + target.name, critical=True)
        return new_fh

    def _fail(self, exc):
        with self._condition:
            self._error = type(exc).__name__
            self._accepting = False
            self._closing = True
            lost = len(self._pending) + self._inflight
            if isinstance(exc, CsvByteLimitReached):
                self._dropped_limit += lost
            else:
                self._dropped_error += lost
            self._pending.clear()
            self._inflight = 0
            self._condition.notify_all()
        self._emit("disabled", critical=True)

    def _run(self):
        fh = None
        last_report = time.monotonic()
        last_flush = last_report
        try:
            fh = self._open()
            self._write_bounded(fh, self.header)
            self._emit("active_async")
            while True:
                with self._condition:
                    while not self._pending and not self._closing:
                        self._condition.wait(self._report_interval or 0.1)
                        # Break to report even if only rejected rows arrived.
                        if not self._pending:
                            break
                    if self._closing and not self._pending:
                        break
                    row = self._pending.popleft() if self._pending else None
                    self._inflight = 1 if row is not None else 0
                if row is not None:
                    fh = self._write_row(fh, row)
                    with self._condition:
                        self._written += 1
                        self._inflight = 0
                now = time.monotonic()
                if self._flush_interval and now - last_flush >= self._flush_interval:
                    fh.flush()
                    last_flush = now
                if now - last_report >= self._report_interval:
                    self._emit("health")
                    last_report = now
            fh.flush()
        except Exception as exc:
            self._fail(exc)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception as exc:
                    self._fail(exc)
            with self._condition:
                self._accepting = False
                self._closing = True
            self._emit("closed", critical=True)

    def _write_row(self, fh, text):
        """Write one record, rotating first when it would overflow the per-file budget."""
        nbytes = len(text.encode("utf-8"))
        if self._bytes_written + nbytes > self.max_bytes:
            fh = self._rotate(fh, nbytes)
        self._write_bounded(fh, text)
        return fh

    def _write_bounded(self, fh, text):
        # newline='' makes this byte accounting exact on Windows as well.  UTF-8
        # encoding occurs on the worker, never on the producer's capture callback.
        nbytes = len(text.encode("utf-8"))
        if self._bytes_written + nbytes > self.max_bytes:
            raise CsvByteLimitReached()
        result = fh.write(text)
        if result is not None and result != len(text):
            raise OSError("short diagnostic write")
        with self._condition:
            self._bytes_written += nbytes
            self._total_bytes += nbytes
