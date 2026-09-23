"""A green ball behind the meter must not displace the observed apex cap."""
from pathlib import Path

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


@pytest.mark.parametrize('idx', range(50, 58))
@pytest.mark.parametrize('box', ['raw', 'display'])
def test_recorded_cap_is_not_replaced_by_larger_lower_ball(idx, box):
    fixture = Path(__file__).parent / 'fixtures/meter/cap_ball_masks.npz'
    with np.load(fixture, allow_pickle=False) as d:
        prefix = f'{box}_{idx}'
        selected = SimpleMeterReader._connected_meter_cap(
            d[prefix + '_green'], d[prefix + '_white'])
        assert selected.any()
        assert np.where(selected)[0].min() == int(d[prefix + '_expected_top'])
        assert np.where(selected)[0].max() <= int(d[prefix + '_expected_top']) + 6


def test_lower_component_size_cannot_move_the_cap():
    g = np.zeros((120, 30), dtype=bool)
    white = np.zeros_like(g)
    white[95:116, 8:22] = True
    g[5:8, 12:17] = True
    for size in range(4, 15):
        trial = g.copy()
        trial[18:18+size, 8:22] = True
        assert np.array_equal(SimpleMeterReader._connected_meter_cap(trial, white), g)


def test_higher_offaxis_decor_stays_rejected():
    g = np.zeros((120, 30), dtype=bool)
    white = np.zeros_like(g)
    white[95:116, 12:20] = True
    g[2:6, 0:4] = True
    g[8:11, 13:18] = True
    selected = SimpleMeterReader._connected_meter_cap(g, white)
    assert selected.sum() == 15
    assert np.where(selected)[0].min() == 8


def test_same_height_uses_larger_component_without_union():
    g = np.zeros((120, 30), dtype=bool)
    white = np.zeros_like(g)
    white[95:116, 5:25] = True
    g[5:9, 6:9] = True
    g[5:9, 15:20] = True
    selected = SimpleMeterReader._connected_meter_cap(g, white)
    assert selected.sum() == 20
    assert np.where(selected)[1].min() == 15
