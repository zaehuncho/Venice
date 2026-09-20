"""Tiny apex caps require independent white-ribbon support, never box identity."""
from pathlib import Path

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


def masks(area=4):
    white = np.zeros((120, 26), dtype=bool)
    white[95:112, 8:18] = True
    green = np.zeros_like(white)
    shapes = {
        4: ((6, 12), (6, 13), (7, 12), (7, 13)),
        5: ((6, 12), (6, 13), (7, 11), (7, 12), (7, 13)),
        6: ((6, 11), (6, 12), (6, 13), (7, 11), (7, 12), (7, 13)),
        7: ((6, 11), (6, 12), (6, 13), (6, 14), (7, 11), (7, 12), (7, 13)),
    }
    for y, x in shapes[area]:
        green[y, x] = True
    return green, white


@pytest.mark.parametrize("area", [4, 5, 6, 7])
def test_tiny_current_apex_uses_independent_ribbon(area):
    green, white = masks(area)
    assert np.array_equal(SimpleMeterReader._connected_meter_cap(green, white), green)


@pytest.mark.parametrize("bad", [
    "no_white", "short_white", "scattered_white", "split_white", "wide_white",
    "single_pixel", "single_row", "single_column", "offaxis", "below_tip",
    "fragments", "detached_decor",
])
def test_tiny_rejections_preserved(bad):
    green, white = masks()
    if bad == "no_white":
        white[:] = False
    elif bad == "short_white":
        white[:] = False
        white[107:112, 8:18] = True
    elif bad == "scattered_white":
        white[:] = False
        white[96:112:2, 8:18] = True
    elif bad == "split_white":
        white[:, 12:14] = False
    elif bad == "wide_white":
        white[95:112, :] = True
    elif bad == "single_pixel":
        green[:] = False
        green[6, 12] = True
    elif bad == "single_row":
        green[:] = False
        green[6, 11:15] = True
    elif bad == "single_column":
        green[:] = False
        green[6:10, 12] = True
    elif bad == "offaxis":
        green = np.roll(green, 6, axis=1)
    elif bad == "below_tip":
        green = np.roll(green, 25, axis=0)
    elif bad == "fragments":
        green[6:8, 18:20] = True
    elif bad == "detached_decor":
        green[60, 3] = True
    assert not SimpleMeterReader._connected_meter_cap(green, white).any()


def test_ordinary_eight_pixel_cold_cap_unchanged():
    green, white = masks()
    white[:] = False
    green[6:8, 11:15] = True
    assert np.array_equal(SimpleMeterReader._connected_meter_cap(green, white), green)


@pytest.mark.parametrize("idx", [2896, 4293])
def test_captured_prerelease_tiny_component(idx):
    # Portable strict-HSV masks from two recorded, prerelease 720p crops.
    # Provenance and source hashes live beside the fixture; no external D: path
    # or live session is needed. JPEG-derived masks are not raw decoder bytes.
    path = Path(__file__).parent / "fixtures" / "meter" / "tiny_cap_masks.npz"
    with np.load(path, allow_pickle=False) as data:
        green, white = data[f"green_{idx}"], data[f"white_{idx}"]
    assert green.dtype == white.dtype == np.dtype(bool)
    assert green.shape == white.shape
    assert int(green.sum()) == 6
    assert np.array_equal(SimpleMeterReader._connected_meter_cap(green, white), green)
