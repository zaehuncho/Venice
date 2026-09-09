"""Rung-tolerant fill (_rung_fill_block / _measure_fill_in_box selection).

The 2K27 Pill (park/rec) capsule divides its fill into ladder segments separated by
1-3-row dark divider lines, with a glossy fill core only ~4-6 columns wide inside the
~20-30-column detector box.  The shipped coarse walk (largest contiguous run of rows
with wfrac >= 0.28) can never read it: the row white-fraction tops out at ~0.20, so the
reader emitted fill 0.00 on 190/192 ground-truth frames while the true fill rose
4->95pp.  These tests render the MEASURED Pill geometry (core band with dark flanks,
~7-11-row segments, 1-3-row dividers, 1-row rung ticks on the empty track @1080p that
alias away @720p) and lock in:

  * a rung-divided bar reads the correct, monotone fill at both scales;
  * a solid smooth bar (Arrow2-like) is byte-identical with the rescue on vs off;
  * a white sliver poking into the box EDGE (the off-box honest-zero case) stays 0.0;
  * a floating occluder blob cannot spike the fill while the ladder touches the base;
  * ORION_METER_RUNG_FILL=0 restores the shipped flat-0.0 behaviour byte-for-byte.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

# ---- measured Pill geometry @720p-scale (box ~20x116; core cols 8-11; pitch ~8) ----
BH, BW = 116, 20
CORE0, CORE1 = 8, 12          # core band columns [CORE0, CORE1)
BASE_ROW = 108                # capsule base (fill never extends below)
PITCH = 8                     # rung pitch, rows
DIV_H = 2                     # divider thickness, rows


def _render_pill(fill_edge_row, ticks=False, core0=CORE0, core1=CORE1,
                 bh=BH, bw=BW, base_row=BASE_ROW, occluder=None):
    """BGR Pill-capsule patch: dark shell/track, bright narrow fill core from
    `fill_edge_row` down to base_row with dark dividers every PITCH rows, optional
    1-row bright rung ticks on the empty track (the 1080p look), green dome cap."""
    patch = np.zeros((bh, bw, 3), np.uint8)
    patch[:] = (40, 42, 45)                       # box surround / capsule shell
    # empty track in the core band: dark neutral (V~70, S~36)
    patch[:base_row, core0:core1] = (60, 65, 70)
    e = int(round(fill_edge_row))
    # fill: bright core with dividers at fixed capsule positions (base-anchored)
    patch[e:base_row, core0:core1] = (250, 252, 253)
    for div_top in range(base_row - PITCH - DIV_H, 4, -(PITCH + DIV_H)):
        d0, d1 = div_top, div_top + DIV_H
        if d0 >= e:                                # divider inside the filled part
            patch[d0:d1, core0:core1] = (100, 105, 110)
        elif ticks and d1 < e - 1:                 # rung tick on the empty track
            patch[d0:d0 + 1, core0 - 1:core1 + 1] = (240, 244, 246)
    # green make-window dome at the capsule top
    patch[2:5, core0 - 1:core1 + 1] = (60, 200, 60)
    # base furniture shelf then court
    patch[base_row:base_row + 3, core0:core1] = (168, 170, 171)
    if occluder is not None:                       # bright band across the WHOLE box
        o0, o1 = occluder
        patch[o0:o1, :] = (246, 248, 250)
    return patch


def _frame_with_box(patch, x=600, y=300, pad=40):
    H, W = patch.shape[0] + 2 * pad, patch.shape[1] + 2 * pad
    f = np.full((H + y, W + x, 3), 35, np.uint8)
    f[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return f, (x, y, patch.shape[1], patch.shape[0])


def _measure(r, patch):
    frame, box = _frame_with_box(patch)
    return r._measure_fill_in_box(frame, box)


@pytest.mark.parametrize("ticks", [False, True], ids=["720p_no_ticks", "1080p_ticks"])
def test_pill_ladder_reads_correct_monotone_fill(ticks):
    """The rung-divided bar must read within ~2 rows of the true edge and rise
    monotonically -- at BOTH scales (ticks model the 1080p empty-track rungs)."""
    r = SimpleMeterReader(1280, 720)
    fills = []
    for edge in range(BASE_ROW - 8, 8, -4):
        fill, _, top = _measure(r, _render_pill(edge, ticks=ticks))
        expect = (BH - 1.0 - edge) / (BH - 1.0) * 100.0
        # tick-merge can lead by up to gap_tol+1 rows at 1080p; dividers by ~DIV_H
        tol_rows = 4.3 if ticks else 2.5
        assert abs(fill - expect) <= tol_rows / (BH - 1.0) * 100.0, (edge, fill, expect)
        assert top >= 0
        fills.append(fill)
    if ticks:
        # @1080p the empty-track rung ticks produce a MEASURED bounded sawtooth: a
        # tick within gap_tol of the edge merges (lead <= gap_tol+1 rows), then the
        # divider at the edge under-reads by ~DIV_H rows -- monotone within ~2.5 rows.
        assert all(b > a - 2.5 / (BH - 1.0) * 100.0
                   for a, b in zip(fills, fills[1:])), fills
    else:
        # the live 720p geometry (ticks alias away) must be strictly monotone
        assert all(b > a for a, b in zip(fills, fills[1:])), fills


def test_pill_flag_off_restores_flat_zero(monkeypatch):
    """ORION_METER_RUNG_FILL=0 must restore the shipped walk byte-for-byte -- which
    reads the Pill at a flat 0.0 (the bug this feature fixes)."""
    monkeypatch.setenv("ORION_METER_RUNG_FILL", "0")
    r = SimpleMeterReader(1280, 720)
    for edge in (30, 60, 90):
        fill, _, top = _measure(r, _render_pill(edge))
        assert fill == 0.0 and top == -1, (edge, fill, top)


def _render_smooth(fill_edge_row, bh=107, bw=24, base_row=100, seed=0, noise=0.0):
    """Arrow2-like solid ribbon: white core spanning ~50% of the box columns."""
    rng = np.random.default_rng(seed)
    patch = np.zeros((bh, bw, 3), np.float32)
    patch[:] = (40, 42, 45)
    patch[:base_row, 5:bw - 5] = (30, 70, 140)      # saturated empty backing
    patch[int(fill_edge_row):base_row, 5:bw - 5] = (250, 252, 253)
    patch[base_row:base_row + 3, 5:bw - 5] = (168, 170, 171)
    patch[2:4, 5:bw - 5] = (60, 200, 60)
    if noise:
        patch = patch + rng.normal(0.0, noise, patch.shape).astype(np.float32)
    return np.clip(patch, 0, 255).astype(np.uint8)


def test_smooth_bar_byte_identical_with_rescue_on_and_off(monkeypatch):
    """A solid smooth ribbon (the Arrow2 shape) must measure EXACTLY the same with the
    rescue enabled as with it disabled: the fill SCALE the engine is tuned on must not
    move.  (Live census: 6064 detected Arrow2 framedump frames, 2 changed -- both
    zero-fill dropout frames, neither a frame the walk could read.)"""
    r_on = SimpleMeterReader(1280, 720)
    monkeypatch.setenv("ORION_METER_RUNG_FILL", "0")
    r_off = SimpleMeterReader(1280, 720)
    for k, edge in enumerate(range(20, 96, 5)):
        patch = _render_smooth(edge, seed=k, noise=1.5)
        fill_on, g_on, top_on = _measure(r_on, patch)
        fill_off, g_off, top_off = _measure(r_off, patch)
        assert fill_on == fill_off, (edge, fill_on, fill_off)
        assert top_on == top_off and g_on == g_off


def test_edge_sliver_stays_honest_zero():
    """A white band touching the box EDGE columns (a neighbouring meter poking into a
    mispositioned box -- the measured off-box case) must stay at fill 0.0: the
    flanking-dark gate refuses a core band without dark columns on BOTH sides."""
    r = SimpleMeterReader(1280, 720)
    for cols in ((0, 4), (BW - 4, BW)):
        patch = np.zeros((BH, BW, 3), np.uint8)
        patch[:] = (40, 42, 45)
        patch[40:BH - 8, cols[0]:cols[1]] = (250, 252, 253)
        fill, _, top = _measure(r, patch)
        assert fill == 0.0 and top == -1, (cols, fill, top)


def test_floating_occluder_cannot_spike_the_fill():
    """A bright blob crossing the whole box mid-height (an arm / overlay) passes the
    shipped 0.28 walk and used to emit a huge one-frame fill spike on the Pill
    (measured 66%% at true fill 17%%).  With a >=2-segment ladder touching the base,
    the ladder must win and the fill must stay near the true edge."""
    r = SimpleMeterReader(1280, 720)
    edge = BASE_ROW - 20                            # true fill ~17%
    patch = _render_pill(edge, occluder=(30, 45))
    fill, _, top = _measure(r, patch)
    expect = (BH - 1.0 - edge) / (BH - 1.0) * 100.0
    assert abs(fill - expect) <= 3.0 / (BH - 1.0) * 100.0, (fill, expect)
    spike = (BH - 1.0 - 30) / (BH - 1.0) * 100.0    # what the occluder would read
    assert fill < spike - 30.0


def test_pill_reports_green_dome():
    """The Pill's green make-window dome must still come back through the green tuple
    (the capless false-lock breaker depends on the cap being measurable)."""
    r = SimpleMeterReader(1280, 720)
    _, green, _ = _measure(r, _render_pill(BASE_ROW - 30))
    assert green is not None
    g_start, g_end = green[0], green[1]
    assert g_end > 90.0 and g_start > 85.0, green
