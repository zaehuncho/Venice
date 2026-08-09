"""Generate Orion Discord brand assets (logo + banner). Royal-blue (#2563EB) on near-black.
Run:  python discord_launch/make_assets.py   ->  discord_launch/assets/orion_logo.png + orion_banner.png
"""
import os, numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "assets")
os.makedirs(OUT, exist_ok=True)

BLUE   = (37, 99, 235)     # #2563EB brand accent
BLUE_L = (120, 165, 255)   # lighter rim/glow
BLUE_D = (18, 46, 120)     # deep base
WHITE  = (244, 247, 250)   # textPrimary

def font(names, size):
    for n in names:
        p = "C:/Windows/Fonts/" + n
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()

def radial(w, h, inner, outer, cx=None, cy=None, power=1.0):
    cx = w/2 if cx is None else cx
    cy = h/2 if cy is None else cy
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx-cx)**2 + (yy-cy)**2)
    d = (d / d.max()) ** power
    out = np.zeros((h, w, 3), np.float32)
    for i in range(3):
        out[..., i] = inner[i]*(1-d) + outer[i]*d
    return Image.fromarray(out.astype(np.uint8), "RGB").convert("RGBA")

def with_glow(base, fg, passes):
    for rad, a in passes:
        g = fg.filter(ImageFilter.GaussianBlur(rad))
        if a != 1.0:
            g.putalpha(g.split()[3].point(lambda p: int(p*a)))
        base.alpha_composite(g)
    base.alpha_composite(fg)
    return base

def star(d, x, y, r, color, spike=0, sw=None):
    if spike > 0:
        sw = sw or max(2, r//3)
        d.line([x-spike, y, x+spike, y], fill=color, width=sw)
        d.line([x, y-spike, x, y+spike], fill=color, width=sw)
    d.ellipse([x-r, y-r, x+r, y+r], fill=color)

def spaced(d, text, fnt, x, y, fill, sp):
    for ch in text:
        d.text((x, y), ch, font=fnt, fill=fill)
        x += d.textlength(ch, font=fnt) + sp
    return x

# ---------------------------------------------------------------- LOGO (icon)
S = 1024
bg = radial(S, S, (22, 30, 48), (6, 9, 13), power=1.25)
fg = Image.new("RGBA", (S, S), (0,0,0,0)); d = ImageDraw.Draw(fg)
cx = cy = S//2
R = 300
d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=BLUE_D, width=58)
d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=BLUE,   width=44)
d.ellipse([cx-R+8, cy-R+8, cx+R-8, cy+R-8], outline=BLUE_L, width=5)
for bx, by in [(-118, 88), (0, 0), (118, -88)]:          # Orion's belt, diagonal
    star(d, cx+bx, cy+by, 30, WHITE, spike=66, sw=9)
    star(d, cx+bx, cy+by, 13, BLUE_L)
star(d, cx-165, cy-150, 10, BLUE_L, spike=26, sw=4)
star(d, cx+178, cy+150, 8,  WHITE,  spike=20, sw=3)
logo = with_glow(bg.copy(), fg, [(30, 0.85), (12, 1.0)]).convert("RGB")
logo.save(os.path.join(OUT, "orion_logo.png"))

# ---------------------------------------------------------------- BANNER
W, H = 1920, 1080
bg = radial(W, H, (16, 24, 42), (5, 7, 11), cx=W*0.46, cy=H*0.40, power=1.12)
fg = Image.new("RGBA", (W, H), (0,0,0,0)); d = ImageDraw.Draw(fg)
# Orion constellation, on the right
sn = [("bet",0.30,0.20),("bel",0.62,0.16),("aln",0.40,0.49),("alm",0.50,0.52),
      ("min",0.61,0.56),("sai",0.35,0.82),("rig",0.68,0.86)]
ox, oy, sc = W*0.575, H*0.10, 560
P = {n: (ox+nx*sc, oy+ny*sc) for n, nx, ny in sn}
for a, b in [("bet","bel"),("bet","aln"),("bel","min"),("aln","alm"),("alm","min"),
             ("aln","sai"),("min","rig"),("sai","rig"),("bet","sai"),("bel","rig")]:
    d.line([P[a], P[b]], fill=(37, 99, 235, 110), width=3)
for n, (x, y) in P.items():
    r = 17 if n in ("bet","rig","alm") else 11
    star(d, x, y, r, WHITE, spike=r*2, sw=max(2, r//4))
    star(d, x, y, max(4, r//2), BLUE_L)
# wordmark + tagline
f_word = font(["seguibl.ttf","ariblk.ttf","Bahnschrift.ttf","impact.ttf","segoeui.ttf"], 250)
f_tag  = font(["seguisb.ttf","bahnschrift.ttf","arialbd.ttf","segoeui.ttf"], 56)
wx, wy = 145, int(H*0.33)
spaced(d, "ORION", f_word, wx, wy, WHITE, 22)
d.rectangle([wx+12, wy+292, wx+12+742, wy+300], fill=(37, 99, 235, 255))
spaced(d, "PRECISION SHOT-TIMING", f_tag, wx+14, wy+322, (120, 165, 255, 255), 12)
banner = with_glow(bg.copy(), fg, [(28, 0.65), (10, 1.0)]).convert("RGB")
banner.save(os.path.join(OUT, "orion_banner.png"))
print("wrote", os.listdir(OUT))
