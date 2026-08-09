"""OrionFramePipeBackend YUV->BGR: BT.709 matrix correctness + zero-copy Y plane.

The PS5 Remote Play stream is BT.709; decoding it with cv2's BT.601 constants hue-shifts
saturated content (pure meter red drifts toward orange, out of the reader's narrow
inRange). These tests round-trip known colors through the backend's converter.
"""
import numpy as np
import pytest

from chiaki_backend import OrionFramePipeBackend

W, H = 64, 32


def _nv12_payload(y_val, u_val, v_val, w=W, h=H) -> bytes:
    y = np.full((h, w), y_val, np.uint8)
    uv = np.empty((h // 2, w), np.uint8)
    uv[:, 0::2] = u_val
    uv[:, 1::2] = v_val
    return y.tobytes() + uv.tobytes()


def _yuv709_for_bgr(bgr):
    """Invert the backend's affine so tests state expectations in BGR."""
    M = OrionFramePipeBackend._BT709_M
    yuv = np.linalg.solve(M[:, :3], np.asarray(bgr, np.float64) - M[:, 3])
    return [int(round(float(c))) for c in yuv]


def _backend(monkeypatch, bt709: bool):
    monkeypatch.setenv("ORION_PIPE_BT709", "1" if bt709 else "0")
    return OrionFramePipeBackend()


@pytest.mark.parametrize("bgr", [(0, 0, 255), (60, 200, 60), (25, 25, 25), (190, 190, 190)])
def test_bt709_round_trip_recovers_bgr(monkeypatch, bgr):
    be = _backend(monkeypatch, True)
    y, u, v = _yuv709_for_bgr(bgr)
    out = be._to_bgr(_nv12_payload(y, u, v), W, H, be._FMT_NV12)
    assert out is not None and out.shape == (H, W, 3)
    got = out[H // 2, W // 2].astype(int)
    assert np.abs(got - np.asarray(bgr)).max() <= 6, (bgr, got.tolist())


def test_meter_red_survives_inrange_under_709():
    """A BT.709-encoded pure meter red must land inside the reader's red inRange after
    conversion -- with the BT.601 path it drifts out (the live-path hue-shift bug)."""
    import cv2
    import os
    os.environ["ORION_PIPE_BT709"] = "1"
    be = OrionFramePipeBackend()
    y, u, v = _yuv709_for_bgr((0, 0, 255))
    out = be._to_bgr(_nv12_payload(y, u, v), W, H, be._FMT_NV12)
    mask = cv2.inRange(out, np.array((0, 0, 220), np.uint8), np.array((60, 60, 255), np.uint8))
    assert cv2.countNonZero(mask) > 0.9 * W * H
    os.environ.pop("ORION_PIPE_BT709", None)


def test_bt601_fallback_path_still_works(monkeypatch):
    be = _backend(monkeypatch, False)
    # mid-grey is matrix-invariant: Y=126, U=V=128 -> ~ (128,128,128) either way
    out = be._to_bgr(_nv12_payload(126, 128, 128), W, H, be._FMT_NV12)
    assert out is not None
    got = out[H // 2, W // 2].astype(int)
    assert np.abs(got - 128).max() <= 4, got.tolist()


def test_i420_layout(monkeypatch):
    be = _backend(monkeypatch, True)
    y, u, v = _yuv709_for_bgr((0, 0, 255))
    q = W * H // 4
    payload = (np.full((H, W), y, np.uint8).tobytes()
               + np.full(q, u, np.uint8).tobytes()
               + np.full(q, v, np.uint8).tobytes())
    out = be._to_bgr(payload, W, H, be._FMT_I420)
    got = out[H // 2, W // 2].astype(int)
    assert np.abs(got - np.asarray((0, 0, 255))).max() <= 6, got.tolist()


def test_split_planes_y_is_zero_copy_view(monkeypatch):
    be = _backend(monkeypatch, True)
    payload = _nv12_payload(100, 128, 128)
    y, _, _ = be._split_planes(payload, W, H, be._FMT_NV12)
    assert y.shape == (H, W)
    assert y.base is not None                      # a view, not a copy
    assert int(y[0, 0]) == 100
