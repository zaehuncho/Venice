"""Decoder geometry / framing coverage for the Chiaki raw-pipe backend.

Guards the 'purple-stained' regression: the raw pipe is headerless and tightly
packed, so if the decoder's frame geometry doesn't match the stream's actual
(preset-dependent) resolution, the reader slices frames on the wrong byte
boundary and the NV12 Y/chroma planes misalign. The backend learns the real
resolution from Chiaki stderr and reconfigures; a mismatched buffer is dropped
rather than mis-reshaped.
"""
import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np

from chiaki_backend import ChiakiBackend, ChiakiConfig, FrameDecoder


def _cfg(w, h, fmt):
    cfg = ChiakiConfig()
    cfg.frame_width = w
    cfg.frame_height = h
    cfg.frame_format = fmt
    return cfg


def test_bgr24_decode_matches_geometry():
    dec = FrameDecoder(_cfg(1280, 720, "bgr24"))
    raw = np.zeros((720, 1280, 3), np.uint8).tobytes()
    fd = dec.decode_frame(raw)
    assert fd is not None
    assert fd.frame.shape == (720, 1280, 3)


def test_reconfigure_updates_frame_size():
    dec = FrameDecoder(_cfg(1920, 1080, "nv12"))
    assert dec.frame_size == 1920 * 1080 * 3 // 2
    assert dec.reconfigure(1280, 720) is True
    assert dec.frame_size == 1280 * 720 * 3 // 2
    # No-op when geometry is unchanged.
    assert dec.reconfigure(1280, 720) is False


def test_mismatched_buffer_is_dropped_not_garbled():
    # 720p-sized NV12 buffer fed to a decoder still expecting 1080p -> drop (None),
    # never a mis-reshaped (tinted) frame.
    dec = FrameDecoder(_cfg(1920, 1080, "nv12"))
    raw = bytes(1280 * 720 * 3 // 2)
    assert dec.decode_frame(raw) is None


def test_nv12_decodes_correctly_after_reconfigure():
    dec = FrameDecoder(_cfg(1920, 1080, "nv12"))
    dec.reconfigure(1280, 720)
    raw = bytes(1280 * 720 * 3 // 2)
    fd = dec.decode_frame(raw)
    assert fd is not None
    assert fd.frame.shape == (720, 1280, 3)


def test_stderr_resolution_regex_matches_known_presets():
    samples = {
        "Negotiated video 1280x720@60": (1280, 720),
        "stream profile: 1920x1080": (1920, 1080),
        "res 2560x1440 codec h265": (2560, 1440),
    }
    for text, expected in samples.items():
        m = ChiakiBackend._RES_RE.search(text)
        assert m is not None, text
        assert (int(m.group(1)), int(m.group(2))) == expected
        assert expected in ChiakiBackend._KNOWN_RESOLUTIONS


def test_stderr_regex_ignores_non_resolution_numbers():
    # Bare counters / bitrates (no WxH form) must not be mistaken for a resolution.
    assert ChiakiBackend._RES_RE.search("frame 12345 bitrate 8000 kbps") is None
