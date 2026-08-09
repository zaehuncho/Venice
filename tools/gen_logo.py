"""Generate the Venice V mark while preserving Orion runtime asset filenames.

Outputs:
  assets/orion.png  - 1024px transparent customer-facing mark
  assets/orion.ico  - multi-size Windows icon

The filenames are an internal package/update contract and intentionally remain
unchanged during the customer-facing rebrand.
"""
import os

from PIL import Image, ImageDraw, ImageFilter


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
S = 1024


def vertical_gradient(top, bottom):
    gradient = Image.new("RGBA", (S, S))
    draw = ImageDraw.Draw(gradient)
    for y in range(S):
        t = y / (S - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(4))
        draw.line([(0, y), (S, y)], fill=color)
    return gradient


def gradient_polygon(points, top, bottom):
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return Image.composite(
        vertical_gradient(top, bottom),
        Image.new("RGBA", (S, S), (0, 0, 0, 0)),
        mask,
    )


def make():
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Restrained cobalt halo that reads on black/navy without becoming a tile.
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((160, 125, 864, 885), fill=(45, 125, 255, 56))
    canvas = Image.alpha_composite(canvas, halo.filter(ImageFilter.GaussianBlur(94)))

    left = [(176, 188), (348, 188), (512, 642), (512, 850), (432, 760)]
    right = [(676, 188), (848, 188), (592, 760), (512, 850), (512, 642)]

    # Deep offset edge gives the mark depth at small taskbar sizes.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.polygon([(x + 10, y + 14) for x, y in left], fill=(2, 17, 38, 230))
    shadow_draw.polygon([(x + 10, y + 14) for x, y in right], fill=(2, 17, 38, 230))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(8)))

    canvas = Image.alpha_composite(
        canvas,
        gradient_polygon(left, (105, 168, 255, 255), (21, 76, 190, 255)),
    )
    canvas = Image.alpha_composite(
        canvas,
        gradient_polygon(right, (225, 241, 255, 255), (45, 125, 255, 255)),
    )

    # One crisp inner highlight; no particles, orbit, or celestial motifs.
    highlight = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).line(
        [(734, 222), (548, 692), (520, 786)],
        fill=(232, 245, 255, 190),
        width=12,
        joint="curve",
    )
    canvas = Image.alpha_composite(canvas, highlight)
    return canvas


def main():
    os.makedirs(ASSETS, exist_ok=True)
    mark = make()
    mark.save(os.path.join(ASSETS, "orion.png"))
    mark.resize((256, 256), Image.Resampling.LANCZOS).save(
        os.path.join(ASSETS, "orion.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("wrote assets/orion.png + assets/orion.ico (Venice mark)")


if __name__ == "__main__":
    main()
