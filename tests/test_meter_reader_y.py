"""MeterReaderNet-Y — the compressed-stream 1-channel LUMA reader.

Covers the load-bearing contracts:
  * model forward shapes/ranges (reg in [0,1], log_var clamped to [-6,2])          [needs torch]
  * masked heteroscedastic loss: a fully-masked term contributes 0; the fill term
    scales linearly with w = 1/sigma^2                                              [needs torch]
  * preprocess determinism + BIT-IDENTICAL trainer<->infer pairing                  [numpy only]
  * infer wrapper disabled-path (missing model / no flag) never raises              [numpy only]
  * 30-step overfit sanity on 64 structured synthetic samples                       [needs torch]
"""
import os
import sys

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TRAIN_DIR = os.path.join(_ROOT, "tools", "training")
for _p in (_ROOT, _TRAIN_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import train_meter_reader_y as TY   # noqa: E402
import meter_reader_y_infer as MY   # noqa: E402

try:
    import torch  # noqa: F401
    HAS_TORCH = True
except Exception:
    HAS_TORCH = False

no_torch = pytest.mark.skipif(not HAS_TORCH, reason="torch not installed")


# --------------------------------------------------------------------------- #
#  synthetic helpers
# --------------------------------------------------------------------------- #
def _rand_crop(seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(TY.INPUT_H, TY.INPUT_W, 1), dtype=np.uint8)


def _structured_batch(n=64, seed=0):
    """Meter-like luma crops: fill = bright column height from the bottom on a dark noisy field.
    Gives the overfit test a genuinely learnable fill signal (not pure noise->noise)."""
    rng = np.random.default_rng(seed)
    X = np.zeros((n, 1, TY.INPUT_H, TY.INPUT_W), np.float32)
    labels = np.zeros((n, 4), np.float32)
    for i in range(n):
        fill = float(rng.uniform(0.1, 0.9))
        gtop = float(rng.uniform(0.0, 0.25))
        gbot = float(rng.uniform(gtop, gtop + 0.2))
        img = rng.uniform(0.0, 0.12, size=(TY.INPUT_H, TY.INPUT_W)).astype(np.float32)
        h = int(fill * TY.INPUT_H)
        if h > 0:
            img[TY.INPUT_H - h:, :] = float(rng.uniform(0.6, 0.9))
        X[i, 0] = img
        labels[i] = (fill, gtop, gbot, 1.0)
    return X, labels


# --------------------------------------------------------------------------- #
#  model forward
# --------------------------------------------------------------------------- #
@no_torch
def test_model_forward_shapes_and_ranges():
    net = TY._make_net(torch).eval()
    x = torch.randn(5, 1, TY.INPUT_H, TY.INPUT_W)
    with torch.no_grad():
        reg, plogit, lvar = net(x)
    assert reg.shape == (5, 3)
    assert plogit.shape == (5,)
    assert lvar.shape == (5,)
    assert float(reg.min()) >= 0.0 and float(reg.max()) <= 1.0     # sigmoid heads
    assert float(lvar.min()) >= -6.0 and float(lvar.max()) <= 2.0  # clamp


@no_torch
def test_logvar_clamp_saturates():
    """Drive the log_var head hard both ways; the clamp must pin it to [-6, 2]."""
    net = TY._make_net(torch).eval()
    with torch.no_grad():
        net.head_logvar.bias.fill_(1000.0)
        _, _, lvar = net(torch.zeros(3, 1, TY.INPUT_H, TY.INPUT_W))
        assert float(lvar.max()) == pytest.approx(2.0)
        net.head_logvar.bias.fill_(-1000.0)
        _, _, lvar = net(torch.zeros(3, 1, TY.INPUT_H, TY.INPUT_W))
        assert float(lvar.min()) == pytest.approx(-6.0)


# --------------------------------------------------------------------------- #
#  masked loss math
# --------------------------------------------------------------------------- #
@no_torch
def test_masked_term_contributes_zero_when_fully_masked():
    n = 8
    reg = torch.rand(n, 3)
    plogit = torch.zeros(n)
    lvar = torch.zeros(n)
    labels = torch.rand(n, 4)
    labels[:, 3] = 1.0
    sigma = torch.ones(n)
    m_green = torch.zeros(n)

    # baseline: fill fully masked
    m_fill0 = torch.zeros(n)
    total0, parts0 = TY.meter_y_loss(reg, plogit, lvar, labels, m_fill0, m_green, sigma)
    assert float(parts0["fill"]) == pytest.approx(0.0)
    assert float(parts0["green"]) == pytest.approx(0.0)

    # perturbing the (masked) fill/green predictions must not move the total at all
    reg2 = reg.clone(); reg2[:, 0] += 5.0; reg2[:, 1] -= 3.0
    total0b, _ = TY.meter_y_loss(reg2, plogit, lvar, labels, m_fill0, m_green, sigma)
    assert float(total0b) == pytest.approx(float(total0), abs=1e-6)


@no_torch
def test_fill_term_scales_with_inverse_sigma_squared():
    """w = 1/sigma^2, and the fill term is m_fill * w * bracket -> halving sigma (w x4) must x4
    the fill loss, independent of the green/present terms."""
    n = 6
    torch.manual_seed(0)
    reg = torch.rand(n, 3)
    plogit = torch.zeros(n)
    lvar = torch.full((n,), 0.3)
    labels = torch.rand(n, 4)
    m_fill = torch.ones(n)
    m_green = torch.zeros(n)

    _, pa = TY.meter_y_loss(reg, plogit, lvar, labels, m_fill, m_green, torch.ones(n))
    _, pb = TY.meter_y_loss(reg, plogit, lvar, labels, m_fill, m_green, torch.full((n,), 0.5))
    assert float(pb["fill"]) == pytest.approx(4.0 * float(pa["fill"]), rel=1e-5)


@no_torch
def test_present_term_is_half_bce():
    n = 4
    plogit = torch.zeros(n)          # sigmoid = 0.5
    labels = torch.zeros(n, 4); labels[:, 3] = 1.0
    reg = torch.rand(n, 3); lvar = torch.zeros(n)
    _, parts = TY.meter_y_loss(reg, plogit, lvar, labels,
                               torch.zeros(n), torch.zeros(n), torch.ones(n))
    # BCE(logit=0, target=1) = -log(0.5) = ln2 ; scaled by 0.5
    assert float(parts["present"]) == pytest.approx(0.5 * np.log(2.0), rel=1e-5)


@no_torch
def test_mask_normalization_is_per_term_not_batch():
    """The green term divides by its OWN mask sum: one green-labelled row among many must not be
    diluted by the batch size."""
    n = 10
    reg = torch.zeros(n, 3); labels = torch.zeros(n, 4)
    reg[:, 1] = 0.0; labels[:, 1] = 1.0   # green_top error = 1.0 on the one masked row
    m_green = torch.zeros(n); m_green[0] = 1.0
    _, parts = TY.meter_y_loss(reg, torch.zeros(n), torch.zeros(n), labels,
                               torch.zeros(n), m_green, torch.ones(n))
    # only row 0 contributes: (gtop_err^2 + gbot_err^2) = 1.0 + 0.0, /mask_sum(=1)
    assert float(parts["green"]) == pytest.approx(1.0, rel=1e-5)


# --------------------------------------------------------------------------- #
#  preprocess: determinism + bit-identical trainer<->infer
# --------------------------------------------------------------------------- #
def test_preprocess_deterministic_bytes():
    crop = _rand_crop(1)
    a = TY.preprocess(crop)
    b = TY.preprocess(crop.copy())
    assert a.shape == (1, TY.INPUT_H, TY.INPUT_W)
    assert a.dtype == np.float32
    assert a.tobytes() == b.tobytes()


def test_preprocess_accepts_2d_and_3d_identically():
    crop3 = _rand_crop(2)
    crop2 = crop3[:, :, 0]
    assert TY.preprocess(crop3).tobytes() == TY.preprocess(crop2).tobytes()


def test_preprocess_resize_path_deterministic():
    rng = np.random.default_rng(3)
    odd = rng.integers(0, 256, size=(100, 40), dtype=np.uint8)  # forces cv2 resize
    a = TY.preprocess(odd)
    b = TY.preprocess(odd.copy())
    assert a.shape == (1, TY.INPUT_H, TY.INPUT_W)
    assert a.tobytes() == b.tobytes()


def test_trainer_and_infer_preprocess_bit_identical():
    """The load-bearing pairing: the infer wrapper's _preprocess must produce byte-identical
    tensors to the trainer's preprocess, on both already-sized and resize paths."""
    reader = MY.MeterReaderY(model_path=os.path.join(_ROOT, "models", "__no_such_model__.pt"))
    for crop in (_rand_crop(4),
                 _rand_crop(5)[:, :, 0],
                 np.random.default_rng(6).integers(0, 256, size=(90, 50), dtype=np.uint8)):
        assert TY.preprocess(crop).tobytes() == reader._preprocess(crop).tobytes()


# --------------------------------------------------------------------------- #
#  infer wrapper: disabled path never raises
# --------------------------------------------------------------------------- #
def test_infer_disabled_when_model_missing():
    reader = MY.MeterReaderY(model_path=os.path.join(_ROOT, "models", "__no_such_model__.pt"))
    assert reader.enabled is False
    assert reader.read(_rand_crop(7)) is None       # never raises
    assert reader.verify(_rand_crop(7)) is False


def test_infer_disabled_read_handles_bad_input():
    reader = MY.MeterReaderY(model_path=os.path.join(_ROOT, "models", "__no_such_model__.pt"))
    assert reader.read(None) is None
    assert reader.read(np.zeros((0, 0), np.uint8)) is None
    assert reader.verify(None) is False


def test_try_load_off_by_default(monkeypatch):
    monkeypatch.delenv("ORION_METER_READER_Y", raising=False)
    assert MY.try_load() is None


def test_try_load_none_when_flag_on_but_model_absent(monkeypatch):
    monkeypatch.setenv("ORION_METER_READER_Y", "1")
    if not os.path.exists(MY._MODEL):
        assert MY.try_load() is None


# --------------------------------------------------------------------------- #
#  AUC metric (numpy)
# --------------------------------------------------------------------------- #
def test_auc_rank_perfect_and_constant():
    y = np.array([0, 0, 1, 1])
    assert TY._auc_rank(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert TY._auc_rank(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
    assert TY._auc_rank(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)  # ties -> 0.5
    assert np.isnan(TY._auc_rank(np.array([1, 1, 1]), np.array([0.1, 0.2, 0.3])))


# --------------------------------------------------------------------------- #
#  overfit sanity
# --------------------------------------------------------------------------- #
@no_torch
def test_overfit_30_steps_decreases_loss():
    X, labels = _structured_batch(64, seed=0)
    net = TY._make_net(torch).train()
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    xb = torch.from_numpy(X)
    lb = torch.from_numpy(labels)
    m_fill = torch.ones(64); m_green = torch.ones(64); sigma = torch.ones(64)

    losses = []
    for _ in range(30):
        opt.zero_grad()
        reg, plogit, lvar = net(xb)
        loss, _ = TY.meter_y_loss(reg, plogit, lvar, lb, m_fill, m_green, sigma)
        losses.append(loss.item())
        loss.backward(); opt.step()
    assert losses[-1] < losses[0], (losses[0], losses[-1])
    assert losses[-1] < 0.9 * losses[0], f"loss barely moved: {losses[0]:.4f} -> {losses[-1]:.4f}"
