"""Generate the Venice installer artwork in the website palette.

Writes into installer/assets/:
  venice-wizard-{100,150,200}.bmp        side image  (164x314 at 100 %)
  venice-wizard-small-{100,150,200}.bmp  header mark (55x55 at 100 %)
  venice-wizard-back-{100,150,200}.png   page background (subtle glow, used with opacity)

Palette and type come from website/public/styles.css: obsidian #06080c canvas, sapphire
rgba(38,112,255,.22) and ice rgba(90,179,251,.11) glows, #f2f5f9 text, Geist. The V mark is
website/public/orion.png. Deterministic (seeded grain) so re-running produces identical bytes.

Usage:  .venv\\Scripts\\python.exe tools\\installer\\make_wizard_art.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "installer" / "assets"
MARK = ROOT / "website" / "public" / "orion.png"
FONT = ROOT / "website" / "public" / "fonts" / "geist-latin-wght-normal-v1.woff2"

CANVAS = (6, 8, 12)            # #06080c
SAPPHIRE = (38, 112, 255)      # glow, alpha .22 on the site
ICE = (90, 179, 251)           # glow, alpha .11 on the site
TEXT = (242, 245, 249)         # #f2f5f9

SCALES = (100, 150, 200)


def _glow(w: int, h: int, cx: float, cy: float, radius: float, color, strength: float) -> np.ndarray:
    """Additive radial glow layer (float RGB), smooth falloff like a CSS radial-gradient."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max(radius, 1.0)
    a = np.clip(1.0 - d, 0.0, 1.0) ** 2 * strength
    return a[..., None] * np.array(color, dtype=np.float32)[None, None, :]


def _canvas(w: int, h: int, glows, seed: int, grain: float = 3.0) -> Image.Image:
    base = np.ones((h, w, 3), dtype=np.float32) * np.array(CANVAS, dtype=np.float32)
    for g in glows:
        base += _glow(w, h, *g)
    rng = np.random.default_rng(seed)
    base += rng.normal(0.0, grain, size=(h, w, 1)).astype(np.float32)
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def _font(px: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), px)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def _mark(size: int) -> Image.Image:
    m = Image.open(MARK).convert("RGBA")
    return m.resize((size, size), Image.LANCZOS)


def _paste_mark_with_halo(img: Image.Image, mark: Image.Image, x: int, y: int, blur: float) -> None:
    """Soft sapphire halo behind the mark, then the mark itself."""
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    tinted = Image.new("RGBA", mark.size, SAPPHIRE + (0,))
    tinted.putalpha(mark.getchannel("A").point(lambda v: int(v * 0.55)))
    halo.paste(tinted, (x, y), tinted)
    halo = halo.filter(ImageFilter.GaussianBlur(blur))
    out = Image.alpha_composite(img.convert("RGBA"), halo)
    out.alpha_composite(mark, (x, y))
    img.paste(out.convert("RGB"))


def _spaced_text(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, font, fill, tracking: float) -> None:
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def side_image(scale: int) -> Image.Image:
    k = scale / 100
    w, h = round(164 * k), round(314 * k)
    # [2026-09-23 owner] Just the mark and the wordmark: no tagline, no footer line.
    cy = h * 0.44
    img = _canvas(w, h, [
        (w * 0.5, cy, w * 1.05, SAPPHIRE, 0.30),
        (w * 0.95, 0.0, w * 0.9, ICE, 0.12),
    ], seed=scale)
    size = round(w * 0.46)
    _paste_mark_with_halo(img, _mark(size), (w - size) // 2, round(cy - size / 2), blur=10 * k)
    d = ImageDraw.Draw(img)
    _spaced_text(d, w // 2, round(cy + size / 2 + 18 * k), "VENICE", _font(round(15 * k), 700),
                 TEXT, tracking=4.2 * k)
    return img


ANIM_FRAMES = 16
ANIM_SCALE = 150


def side_frame(scale: int, phase: float) -> Image.Image:
    """One frame of the breathing glow: the side image with the sapphire glow and the mark's halo
    swelling and settling on a sine (phase 0..1). Same grain seed every frame, so only the glow
    changes and LZMA's solid compression stores the set cheaply."""
    import math
    k = scale / 100
    w, h = round(164 * k), round(314 * k)
    cy = h * 0.44
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * phase)          # 0 -> 1 -> 0
    img = _canvas(w, h, [
        (w * 0.5, cy, w * (0.95 + 0.20 * breath), SAPPHIRE, 0.22 + 0.16 * breath),
        (w * 0.95, 0.0, w * 0.9, ICE, 0.12),
    ], seed=scale)
    size = round(w * 0.46)
    _paste_mark_with_halo(img, _mark(size), (w - size) // 2, round(cy - size / 2),
                          blur=(8 + 6 * breath) * k)
    d = ImageDraw.Draw(img)
    _spaced_text(d, w // 2, round(cy + size / 2 + 18 * k), "VENICE", _font(round(15 * k), 700),
                 TEXT, tracking=4.2 * k)
    return img


HERO_W, HERO_H = 440, 190      # logical size at 100 %; rendered at 200 % and scaled down by Inno


def hero_frame(phase: float, scale: int = 200) -> Image.Image:
    """One-screen installer hero: the V mark and VENICE on a flat #06080c canvas (no grain, so the
    edges melt into WizardBackColor with no visible rectangle), with a breathing sapphire glow."""
    import math
    k = scale / 100
    w, h = round(HERO_W * k), round(HERO_H * k)
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
    cy = h * 0.42
    # Radius stays below the distance to the nearest edge (0.42 h), so every frame fades to the
    # exact canvas colour before the border - no visible rectangle against WizardBackColor.
    img = _canvas(w, h, [(w * 0.5, cy, h * (0.30 + 0.09 * breath), SAPPHIRE, 0.30 + 0.18 * breath)],
                  seed=0, grain=0.0)
    size = round(h * 0.46)
    _paste_mark_with_halo(img, _mark(size), (w - size) // 2, round(cy - size / 2),
                          blur=(8 + 6 * breath) * k)
    d = ImageDraw.Draw(img)
    _spaced_text(d, w // 2, round(cy + size / 2 + 14 * k), "VENICE", _font(round(20 * k), 700),
                 TEXT, tracking=6.0 * k)
    return img


def small_image(scale: int) -> Image.Image:
    k = scale / 100
    s = round(55 * k)
    img = _canvas(s, s, [(s * 0.5, s * 0.5, s * 0.75, SAPPHIRE, 0.22)], seed=1000 + scale, grain=2.0)
    size = round(s * 0.72)
    _paste_mark_with_halo(img, _mark(size), (s - size) // 2, (s - size) // 2, blur=4 * k)
    return img


def back_image(scale: int) -> Image.Image:
    k = scale / 100
    w, h = round(640 * k), round(480 * k)
    return _canvas(w, h, [
        (w * 0.92, h * 0.02, w * 0.62, SAPPHIRE, 0.16),
        (w * 1.02, h * 0.55, w * 0.45, ICE, 0.06),
    ], seed=2000 + scale, grain=2.5)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for sc in SCALES:
        side_image(sc).save(ASSETS / f"venice-wizard-{sc}.bmp")
        small_image(sc).save(ASSETS / f"venice-wizard-small-{sc}.bmp")
        back_image(sc).save(ASSETS / f"venice-wizard-back-{sc}.png", optimize=True)
    # The one-screen look (installer\ui\venice_ui.iss) animates the hero; the sidebar is hidden,
    # so side_frame() is kept only for reference and no sidebar frames are written.
    hero = ASSETS / "hero"
    hero.mkdir(exist_ok=True)
    for i in range(ANIM_FRAMES):
        hero_frame(i / ANIM_FRAMES).save(hero / f"venice-hero-{i:02d}.bmp")
    for p in sorted(ASSETS.glob("venice-wizard-*")):
        print(f"{p.name}: {Image.open(p).size}")


if __name__ == "__main__":
    main()
