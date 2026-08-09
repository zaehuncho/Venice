"""Tests for tools/diagnostics/gen_meter_reader_synth_y.py (codec-augmented LUMA meter dataset).

Hardware-dependent tests (real ffmpeg round trip + real framedump backgrounds) skipif when either is
absent. Pure label-math / in-memory-render tests always run.

The tool is loaded by EXPLICIT PATH via importlib (registered in sys.modules before exec) so the
retired tools/diagnostics/simple_meter_reader.py shadow is never involved -- this tool imports no
repo-root module.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import random
import shutil
import sys

import numpy as np
import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_TOOL_PATH = os.path.join(_REPO, "tools", "diagnostics", "gen_meter_reader_synth_y.py")


def _load_tool():
    name = "gen_meter_reader_synth_y"
    tdir = os.path.join(_REPO, "tools", "diagnostics")
    if tdir not in sys.path:
        sys.path.insert(0, tdir)
    spec = importlib.util.spec_from_file_location(name, _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_tool()

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_FRAMEDUMP = bool(glob.glob(os.path.join(mod.DEFAULT_FRAMEDUMP_DIR, "f*_raw.png")))
needs_hw = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_FRAMEDUMP),
    reason=f"needs ffmpeg (have={HAVE_FFMPEG}) and framedump session (have={HAVE_FRAMEDUMP})",
)

# Exact npz schema: key -> (dims-with-None-for-N, dtype).
EXPECTED_SCHEMA = {
    "images":   ((None, mod.STORE_H, mod.STORE_W, 1), np.uint8),
    "labels":   ((None, 4), np.float32),
    "m_fill":   ((None,), np.float32),
    "m_green":  ((None,), np.float32),
    "rung":     ((None,), np.int8),
    "split":    ((None,), np.uint8),
    "fill_raw": ((None,), np.float32),
}


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
    """Generate a small codec-augmented dataset ONCE for the hardware tests (seed 7 => negatives
    present and the 1.06 fill overshoot exercised)."""
    if not (HAVE_FFMPEG and HAVE_FRAMEDUMP):
        pytest.skip("no ffmpeg / no framedump session")
    out = str(tmp_path_factory.mktemp("synth_y") / "meter_reader_y_data.npz")
    mod.generate(10, out, seed=7, verbose=False)
    return np.load(out)


# --------------------------------------------------------------------------- hardware tests
@needs_hw
def test_npz_schema_exact(smoke):
    assert set(smoke.files) == set(EXPECTED_SCHEMA), (
        f"npz keys {sorted(smoke.files)} != {sorted(EXPECTED_SCHEMA)}")
    n = smoke["images"].shape[0]
    assert n > 0
    for key, (dims, dtype) in EXPECTED_SCHEMA.items():
        arr = smoke[key]
        assert arr.dtype == dtype, f"{key}: dtype {arr.dtype} != {dtype}"
        exp = tuple(n if d is None else d for d in dims)
        assert arr.shape == exp, f"{key}: shape {arr.shape} != {exp}"


@needs_hw
def test_fill_labels_in_range(smoke):
    fill = smoke["labels"][:, 0]
    assert float(fill.min()) >= 0.0 and float(fill.max()) <= 1.0
    # clamp correctness: wherever the raw fill overshoots 1.0, the stored fill label is clamped to 1.0
    raw = smoke["fill_raw"]
    over = raw > 1.0
    if over.any():
        assert np.allclose(fill[over], 1.0), "overshoot renders must clamp fill label to 1.0"


@needs_hw
def test_positive_green_bounds_ordered(smoke):
    lab = smoke["labels"]
    pos = lab[:, 3] > 0.5
    assert pos.any(), "expected at least one positive crop"
    gtop, gbot = lab[pos, 1], lab[pos, 2]
    assert bool((gtop < gbot).all()), "green_top must be < green_bottom for every positive"
    assert bool(((gtop >= 0.0) & (gbot <= 1.0)).all())


@needs_hw
def test_negatives_present_and_masks(smoke):
    lab = smoke["labels"]
    neg = lab[:, 3] < 0.5
    assert neg.any(), "expected at least one negative crop at seed 7"
    assert bool((lab[neg, 3] == 0.0).all()), "negatives must have present == 0"
    assert bool((smoke["m_fill"][neg] == 0.0).all()), "negatives must have m_fill == 0"
    assert bool((smoke["m_green"][neg] == 0.0).all()), "negatives must have m_green == 0"
    # positives carry both masks set
    pos = ~neg
    assert bool((smoke["m_fill"][pos] == 1.0).all())
    assert bool((smoke["m_green"][pos] == 1.0).all())


@needs_hw
def test_val_split_ratio(smoke):
    split = smoke["split"]
    n = split.shape[0]
    assert set(np.unique(split).tolist()).issubset({0, 1})
    assert int(split.sum()) == int(round(0.10 * n)), "val split must be ~10% (rounded)"


@needs_hw
def test_rung_indices_valid(smoke):
    rung = smoke["rung"]
    assert rung.dtype == np.int8
    assert int(rung.min()) >= 0 and int(rung.max()) < len(mod.RUNG_NAMES)


@needs_hw
def test_encode_degrades_at_least_one_crop(tmp_path):
    """A harvested (decoded) crop must differ from its pristine render -- proves the ffmpeg round
    trip actually degraded the meter region (the whole point of sequence re-encoding)."""
    rng = random.Random(3)
    bg_files = sorted(glob.glob(os.path.join(mod.DEFAULT_FRAMEDUMP_DIR, "f*_raw.png")))
    import cv2  # noqa: local import so collection never fails when cv2 absent
    bg = cv2.imread(bg_files[len(bg_files) // 2])
    frames, _fills, geom = mod.render_meter_sequence(bg, rng)

    work = str(tmp_path)
    mp4, _kbps = mod.encode_sequence(frames, "Balanced", work)
    decoded = mod.decode_frames(mp4)
    assert len(decoded) > mod.SKIP_INTRA, "decode produced too few frames"

    # fixed (un-jittered) padded meter box so pristine vs decoded compare the same pixels
    x0, y0, x1, y1 = mod.pad_track_box(geom["track_box"])
    ix0, iy0, ix1, iy1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    diffs = []
    for fi in range(mod.SKIP_INTRA, min(len(decoded), len(frames))):
        pc = frames[fi][iy0:iy1, ix0:ix1].astype(np.float32)
        dc = decoded[fi][iy0:iy1, ix0:ix1].astype(np.float32)
        diffs.append(float(np.abs(pc - dc).mean()))
    assert diffs and max(diffs) > 0.5, f"encode did not degrade the crop (max mean|diff|={max(diffs) if diffs else 0})"


# --------------------------------------------------------------------------- no-hardware tests
def test_green_fracs_pure_label_math():
    # crop rows [10, 110); green window canvas rows 22..30 -> fractions 0.12 .. 0.20
    gt, gb = mod.green_fracs_for_crop(22, 30, 10, 100)
    assert abs(gt - 0.12) < 1e-9 and abs(gb - 0.20) < 1e-9
    assert gt < gb

    # pad_track_box geometry: x1.6 total width, +12% height top & bottom
    track = (40, 50, 20, 120)  # tx, ty, tw, th
    px0, py0, px1, py1 = mod.pad_track_box(track)
    assert abs((px1 - px0) - 1.6 * 20) < 1e-6
    assert abs((py1 - py0) - 120 * (1.0 + 2 * 0.12)) < 1e-6

    # a thin green window near the padded-box top -> ordered fractions strictly inside (0,1)
    green_top, green_bottom = 50, 50 + max(2, int(round(120 * 0.06)))
    crop_h = py1 - py0
    gt2, gb2 = mod.green_fracs_for_crop(green_top, green_bottom, py0, crop_h)
    assert 0.0 < gt2 < gb2 < 1.0


def test_render_sequence_in_memory():
    """render_meter_sequence rises and stays in-canvas -- no ffmpeg / no framedump needed."""
    rng = random.Random(0)
    bg = np.random.RandomState(0).randint(0, 255, (300, 300, 3), dtype=np.uint8)
    frames, fills, geom = mod.render_meter_sequence(bg, rng)

    assert len(frames) == mod.N_FRAMES == len(fills)
    assert all(f.shape == (mod.CANVAS, mod.CANVAS, 3) and f.dtype == np.uint8 for f in frames)
    assert fills[-1] > fills[0], "fill must ramp upward across the sequence"

    assert geom["green_top"] < geom["green_bottom"]
    x0, y0, x1, y1 = mod.pad_track_box(geom["track_box"])
    # padded crop box (before jitter) sits inside the canvas
    assert x0 >= 0 and y0 >= 0 and x1 <= mod.CANVAS and y1 <= mod.CANVAS


def test_scaled_bitrate_matches_density():
    """Canvas bitrate scales per-pixel density: kbps * (canvas^2)/(rung_w*rung_h)."""
    for rung in mod.RUNG_NAMES:
        r = mod.RUNGS[rung]
        exp = max(1, int(round(r["kbps"] * (mod.CANVAS * mod.CANVAS) / (r["w"] * r["h"]))))
        assert mod.scaled_bitrate_kbps(rung) == exp
        # scaled density is far below the rung's raw kbps (256^2 << 720p)
        assert mod.scaled_bitrate_kbps(rung) < r["kbps"]
