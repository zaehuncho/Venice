"""Bounded, independently verified HuffYUV chunks for diagnostic pixels only.

The caller owns the bounded producer queue. This worker-side object retains hashes
and receipts, not raw arrays. A receipt is committed only after the closed chunk
decodes byte-for-byte and its temporary file is atomically published.
"""
from pathlib import Path
import hashlib
import os
import operator
import uuid

import cv2


class LosslessFrameArchive:
    def __init__(self, root, commit, discard, chunk_frames=60):
        self.root = Path(root)
        self.commit, self.discard = commit, discard
        self.chunk_frames = max(1, min(120, int(chunk_frames)))
        self.writer = None
        self.pending = []
        self.prefix = "raw_" + uuid.uuid4().hex[:16]
        self.chunk = 0
        self.shape = None
        self.temporary = None

    def append(self, item):
        idx, frame, info = item
        if frame is None or frame.dtype.name != 'uint8' or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError('archive requires uint8 BGR pixels')
        shape = tuple(frame.shape)
        if self.writer is not None and shape != self.shape:
            self.flush()
        if self.writer is None:
            self.shape = shape
            self.temporary = self.root / f'{self.prefix}_{self.chunk:06d}.partial.avi'
            if self.temporary.exists() or self.temporary.with_name(f'{self.prefix}_{self.chunk:06d}.avi').exists():
                raise FileExistsError('archive chunk already exists')
            self.writer = cv2.VideoWriter(str(self.temporary), cv2.VideoWriter_fourcc(*'HFYU'),
                                         60.0, (shape[1], shape[0]))
            if not self.writer.isOpened():
                self.writer.release()
                self.writer = None
                raise RuntimeError('lossless HuffYUV writer unavailable')
        # Reserve a receipt before write: errors must account this item exactly once.
        self.pending.append(((idx, None, info), hashlib.sha256(frame.tobytes()).digest()))
        self.writer.write(frame)
        if len(self.pending) >= self.chunk_frames:
            self.flush()

    def flush(self):
        if self.writer is None:
            return
        writer, self.writer = self.writer, None
        receipts, self.pending = self.pending, []
        try:
            writer.release()
            cap = cv2.VideoCapture(str(self.temporary))
            try:
                if not cap.isOpened():
                    raise IOError('closed diagnostic chunk did not reopen')
                for _, expected in receipts:
                    ok, frame = cap.read()
                    if (not ok or tuple(frame.shape) != self.shape
                            or hashlib.sha256(frame.tobytes()).digest() != expected):
                        raise IOError('lossless diagnostic chunk failed pixel verification')
                if cap.read()[0]:
                    raise IOError('diagnostic chunk has unexpected extra frames')
            finally:
                cap.release()
            final = self.root / f'{self.prefix}_{self.chunk:06d}.avi'
            if final.exists():
                raise FileExistsError('refusing to replace an existing diagnostic chunk')
            os.replace(self.temporary, final)
            self.chunk += 1
        except Exception:
            try:
                self._discard_receipts(receipts)
            except Exception:
                pass  # keep the primary codec/verification error
            raise
        error = None
        for ordinal, (item, _) in enumerate(receipts):
            try:
                self.commit(item, final.name, ordinal)
            except Exception as exc:
                # One failed metadata callback must not strand other receipts.
                # The production discard callback is itself exception-contained.
                error = error or exc
                try:
                    self.discard(item)
                except Exception:
                    pass  # attempt every remaining receipt, retain the first error
        if error is not None:
            raise error

    def _discard_receipts(self, receipts):
        error = None
        for item, _ in receipts:
            try:
                self.discard(item)
            except Exception as exc:
                error = error or exc
        if error is not None:
            raise error

    def discard_pending(self):
        writer, self.writer = self.writer, None
        receipts, self.pending = self.pending, []
        try:
            if writer is not None:
                writer.release()
        finally:
            self._discard_receipts(receipts)


def read_frame(path, ordinal):
    """Read one indexed diagnostic frame; never accept a missing frame as pixels."""
    try:
        if isinstance(ordinal, bool):
            raise ValueError
        if isinstance(ordinal, str):
            if not (ordinal.isascii() and ordinal.isdigit() and len(ordinal) <= 3
                    and str(int(ordinal)) == ordinal):
                raise ValueError
            ordinal = int(ordinal)
        else:
            ordinal = operator.index(ordinal)
        if not 0 <= ordinal < 120:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError('invalid chunk frame ordinal') from None
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise IOError('diagnostic chunk unavailable')
        # Sequential decode keeps prediction/reference handling codec-independent.
        for _ in range(ordinal + 1):
            ok, frame = cap.read()
            if not ok:
                raise IOError('indexed diagnostic frame is missing')
        return frame
    finally:
        cap.release()
