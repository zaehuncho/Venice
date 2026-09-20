"""Decoder conversion must preserve pixels without per-frame chroma allocations."""
import cv2
import numpy as np
import pytest

from chiaki_backend import OrionFramePipeBackend


def _reference(payload, width, height, fmt):
    buf = np.frombuffer(payload, dtype=np.uint8)
    y = buf[:width * height].reshape(height, width)
    if fmt == OrionFramePipeBackend._FMT_NV12:
        uv = buf[width * height:].reshape(height // 2, width)
        u, v = uv[:, 0::2].copy(), uv[:, 1::2].copy()
    else:
        q = width * height // 4
        u = buf[width * height:width * height + q].reshape(height // 2, width // 2)
        v = buf[width * height + q:].reshape(height // 2, width // 2)
    up = cv2.resize(u, (width, height), interpolation=cv2.INTER_LINEAR)
    vp = cv2.resize(v, (width, height), interpolation=cv2.INTER_LINEAR)
    return cv2.transform(cv2.merge([y, up, vp]), OrionFramePipeBackend._BT709_M)


@pytest.mark.parametrize("fmt", [0, 1])
def test_changing_geometry_keeps_exact_pixels_and_previous_frame_ownership(fmt):
    backend = OrionFramePipeBackend()
    backend._bt709 = True
    rng = np.random.default_rng(20260906)
    previous = []
    for width, height in [(64, 32), (64, 32), (128, 72), (32, 64), (64, 32)]:
        payload = rng.integers(0, 256, width * height * 3 // 2, dtype=np.uint8).tobytes()
        frame = backend._to_bgr(payload, width, height, fmt)
        assert np.array_equal(frame, _reference(payload, width, height, fmt))
        assert frame.flags.owndata and frame.flags.c_contiguous
        assert all(not np.shares_memory(frame, old) for old, _ in previous)
        assert all(np.array_equal(old, snapshot) for old, snapshot in previous)
        previous.append((frame, frame.copy()))


def test_warmed_nv12_converter_does_not_allocate_contiguous_chroma_copies(monkeypatch):
    backend = OrionFramePipeBackend()
    backend._bt709 = True
    payload = bytes(64 * 32 * 3 // 2)
    assert backend._to_bgr(payload, 64, 32, backend._FMT_NV12) is not None
    copies = []
    original = np.ascontiguousarray

    def track_copy(array, *args, **kwargs):
        if array.ndim == 2 and not array.flags.c_contiguous:
            copies.append(array.shape)
        return original(array, *args, **kwargs)

    monkeypatch.setattr(np, "ascontiguousarray", track_copy)
    for _ in range(5):
        assert backend._to_bgr(payload, 64, 32, backend._FMT_NV12) is not None
    assert copies == [], "the hot path reallocated both half-resolution chroma planes per frame"
