"""Inclusive camera-scale bounds must not reject their floating-point endpoint.

Real OpenCV scene transforms, NCC, and fill/green measurement. The reused fixture
renders a fixed physical fill fraction while independently moving/scaling the
entire local meter scene. No source timing or detector ownership gate is mocked.
"""
import pytest

from test_simple_reader_camera_scale_ruler_contract import _reader, _measure, _scene


@pytest.mark.parametrize("scale,limit", [(1.35, .35), (1.30, .30), (1.10, .10)])
@pytest.mark.parametrize("refresh", [False, True])
def test_exact_inclusive_zoom_limit_preserves_fill_and_green(monkeypatch, scale, limit, refresh):
    reader, (expected_fill, expected_green) = _reader(monkeypatch)
    reader._scale_step_max = limit
    for index in range(3):
        frame, box = _scene(sx=scale, sy=scale, dx=3, dy=-2)
        fill, green = _measure(reader, frame, box, 5 + index, refresh=refresh)
        assert reader._det_track_score >= reader._det_track_min
        assert fill == pytest.approx(expected_fill, abs=1.5), \
            "floating-point endpoint refusal turned camera zoom into fill progress"
        assert green is not None
        assert green[:2] == pytest.approx(expected_green[:2], abs=1.5)
        assert reader._subpx_camera_scale == pytest.approx(scale)


@pytest.mark.parametrize("limit,height", [(.35, 162), (.30, 156), (.10, 132)])
def test_template_candidates_include_exact_limit_but_not_one_pixel_beyond(monkeypatch, limit, height):
    reader, _ = _reader(monkeypatch)
    reader._scale_step_max = limit
    assert len(reader._det_track_template_candidates(24, height)) == 2
    assert len(reader._det_track_template_candidates(24, height + 1)) == 1


def test_width_above_limit_still_rejects_height_at_limit(monkeypatch):
    reader, _ = _reader(monkeypatch)
    assert len(reader._det_track_template_candidates(33, 162)) == 1


def test_scale_epsilon_does_not_create_unobserved_pixel_authority(monkeypatch):
    reader, (_fill, _green) = _reader(monkeypatch)
    original_scale = reader._subpx_camera_scale
    frame, box = _scene(sx=1.35, sy=1.35, notch=False, cap=False)
    _measure(reader, frame, box, 5)
    assert reader._det_track_score < reader._det_track_min
    assert reader._subpx_camera_scale == original_scale
