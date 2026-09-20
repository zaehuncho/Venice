"""Decoded-wire assembly tests; no codec, device, process, or pipe is opened."""
import sys
from types import SimpleNamespace

import pytest

from chiaki_backend import OrionFramePipeBackend


def _reader(monkeypatch, chunks):
    backend = OrionFramePipeBackend()
    backend._handle = object()
    iterator = iter(chunks)
    requests = []

    def read(handle, size):
        assert handle is backend._handle
        requests.append(size)
        data = next(iterator)
        if isinstance(data, Exception):
            raise data
        return 0, data

    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(ReadFile=read))
    return backend, requests


@pytest.mark.parametrize("size", [64, 1920 * 1080 * 3 // 2])
def test_complete_immutable_packet_is_returned_without_payload_copy(monkeypatch, size):
    payload = bytes(size)
    backend, requests = _reader(monkeypatch, [payload])
    assert backend._read_exact(size) is payload
    assert requests == [size]


def test_fragmented_read_preserves_exact_order_and_remaining_lengths(monkeypatch):
    backend, requests = _reader(monkeypatch, [b"ab", b"c", b"def"])
    result = backend._read_exact(6)
    assert type(result) is bytes and result == b"abcdef"
    assert requests == [6, 4, 3]


def test_mutable_read_buffer_is_frozen_before_return(monkeypatch):
    payload = bytearray(b"abcdef")
    backend, _ = _reader(monkeypatch, [payload])
    result = backend._read_exact(6)
    payload[:] = b"xxxxxx"
    assert type(result) is bytes and result == b"abcdef"


@pytest.mark.parametrize("tail", [b"", OSError("read failed")])
def test_incomplete_packet_is_never_published(monkeypatch, tail):
    backend, requests = _reader(monkeypatch, [b"ab", tail])
    assert backend._read_exact(6) is None
    assert requests == [6, 4]


def test_stopped_reader_does_not_issue_another_read(monkeypatch):
    backend, requests = _reader(monkeypatch, [])
    backend._stop_evt.set()
    assert backend._read_exact(6) is None
    assert requests == []


def test_zero_length_remains_empty_bytes(monkeypatch):
    backend, requests = _reader(monkeypatch, [])
    assert backend._read_exact(0) == b""
    assert requests == []


def test_invalid_oversized_chunk_does_not_shift_packet_boundaries(monkeypatch):
    backend, requests = _reader(monkeypatch, [b"too long"])
    assert backend._read_exact(3) is None
    assert requests == [3]
