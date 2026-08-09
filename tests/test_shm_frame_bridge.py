import mmap
import os
import struct
import uuid

import numpy as np
import pytest

import shm_frame_bridge as shm


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named mappings only")


def _header(mapping):
    return struct.unpack(shm.HEADER_FMT, mapping[: shm.HEADER_PACKED_SIZE])


def _transport_names():
    token = uuid.uuid4().hex
    return (
        f"OrionPreviewFrame_{token}",
        f"OrionPreviewMutex_{token}",
        f"OrionPreviewReady_{token}",
    )


def test_writer_publishes_bounded_bgr_frame_with_source_identity():
    mapping_name, mutex_name, _ = _transport_names()
    writer = shm.ShmFrameWriter(
        mapping_name=mapping_name, mutex_name=mutex_name)
    assert writer.start(), writer.last_error
    reader = mmap.mmap(
        -1, shm.MAPPING_SIZE, tagname=mapping_name, access=mmap.ACCESS_READ)
    try:
        frame = np.empty((720, 1280, 3), dtype=np.uint8)
        frame[:, :, 0] = 17
        frame[:, :, 1] = 83
        frame[:, :, 2] = 211
        source_timestamp_ns = 9_876_543_210
        assert writer.write(
            frame,
            source_frame_number=4242,
            timestamp_ns=source_timestamp_ns,
        ), writer.last_error
        magic, version, width, height, channels, frame_no, stamp, write_count = _header(reader)
        assert (magic, version) == (shm.MAGIC, shm.VERSION)
        assert (width, height, channels, frame_no) == (1280, 720, 3, 4242)
        assert stamp == source_timestamp_ns
        assert write_count == 1
        assert bytes(reader[shm.HEADER_SIZE:shm.HEADER_SIZE + 3]) == bytes((17, 83, 211))
        assert writer.last_commit_ns > 0
        assert writer.last_write_duration_ms >= 0.0
        assert not writer.event_notifications_enabled
    finally:
        reader.close()
        writer.stop()


def test_writer_downscales_aspect_preserving_without_axis_stretch():
    mapping_name, mutex_name, _ = _transport_names()
    writer = shm.ShmFrameWriter(
        mapping_name=mapping_name, mutex_name=mutex_name)
    assert writer.start(), writer.last_error
    reader = mmap.mmap(
        -1, shm.MAPPING_SIZE, tagname=mapping_name, access=mmap.ACCESS_READ)
    try:
        frame = np.zeros((800, 1920, 3), dtype=np.uint8)
        assert writer.write(frame, source_frame_number=9), writer.last_error
        _, _, width, height, channels, frame_no, _, _ = _header(reader)
        assert (width, height, channels, frame_no) == (1280, 533, 3, 9)
        assert abs((width / height) - (1920 / 800)) < 0.01
    finally:
        reader.close()
        writer.stop()


def test_writer_rejects_malformed_detector_frame():
    mapping_name, mutex_name, _ = _transport_names()
    writer = shm.ShmFrameWriter(
        mapping_name=mapping_name, mutex_name=mutex_name)
    assert writer.start(), writer.last_error
    try:
        assert not writer.write(np.zeros((720, 1280, 4), dtype=np.uint8))
        assert "HxWx3" in writer.last_error
        assert not writer.write(np.zeros((720, 1280, 3), dtype=np.float32))
        assert "uint8" in writer.last_error
    finally:
        writer.stop()


def test_writer_signals_negotiated_auto_reset_event_after_commit():
    mapping_name, mutex_name, event_name = _transport_names()
    event = shm._CreateEventW(None, False, False, event_name)
    assert event
    writer = shm.ShmFrameWriter(
        mapping_name=mapping_name,
        mutex_name=mutex_name,
        ready_event_name=event_name,
    )
    try:
        assert writer.start(), writer.last_error
        assert writer.event_notifications_enabled
        assert shm._WaitForSingleObject(event, 0) == shm.WAIT_TIMEOUT

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        assert writer.write(frame, source_frame_number=55), writer.last_error
        assert writer.last_commit_ns > 0
        assert writer.last_event_notify_ns >= writer.last_commit_ns
        assert writer.last_write_duration_ms >= 0.0
        assert writer.last_event_notify_ms >= 0.0
        assert writer.event_signal_failures == 0

        assert shm._WaitForSingleObject(event, 1000) == shm.WAIT_OBJECT_0
        # Auto-reset prevents one committed mapping generation from being
        # presented repeatedly when no newer producer signal exists.
        assert shm._WaitForSingleObject(event, 0) == shm.WAIT_TIMEOUT
    finally:
        writer.stop()
        shm._CloseHandle(event)


def test_writer_invalidates_header_before_body_and_commits_header_last():
    mapping_name, mutex_name, _ = _transport_names()
    writer = shm.ShmFrameWriter(
        mapping_name=mapping_name, mutex_name=mutex_name)
    assert writer.start(), writer.last_error
    real_mapping = writer._mmap

    class RecordingMapping:
        def __init__(self, mapping):
            self.mapping = mapping
            self.writes = []

        def __getitem__(self, key):
            return self.mapping[key]

        def __setitem__(self, key, value):
            start = 0 if key.start is None else int(key.start)
            stop = int(key.stop)
            self.writes.append((start, stop, bytes(value[:16])))
            if start == shm.HEADER_SIZE:
                assert bytes(self.mapping[:shm.HEADER_SIZE]) == shm.INVALID_HEADER
            self.mapping[key] = value

        def close(self):
            self.mapping.close()

    try:
        baseline = np.full((720, 1280, 3), 19, dtype=np.uint8)
        assert writer.write(baseline, source_frame_number=1), writer.last_error
        recorder = RecordingMapping(real_mapping)
        writer._mmap = recorder

        replacement = np.full((720, 1280, 3), 207, dtype=np.uint8)
        assert writer.write(replacement, source_frame_number=2), writer.last_error
        assert [(start, stop) for start, stop, _ in recorder.writes] == [
            (0, shm.HEADER_SIZE),
            (shm.HEADER_SIZE, shm.HEADER_SIZE + shm.MAX_BODY_SIZE),
            (0, shm.HEADER_PACKED_SIZE),
            (shm.HEADER_PACKED_SIZE, shm.HEADER_SIZE),
        ]
        magic, version, width, height, channels, frame_no, _, write_count = (
            _header(real_mapping)
        )
        assert (magic, version, width, height, channels) == (
            shm.MAGIC, shm.VERSION, 1280, 720, 3)
        assert (frame_no, write_count) == (2, 2)
        assert bytes(real_mapping[shm.HEADER_SIZE:shm.HEADER_SIZE + 3]) == bytes(
            (207, 207, 207)
        )
    finally:
        writer.stop()
