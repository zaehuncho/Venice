"""Quick check: what does BT.709-encoded magenta look like when decoded as BT.601?

The decoder frame pipe ships raw NV12 and chiaki_backend._to_bgr converts with
cv2.COLOR_YUV2BGR_NV12 (BT.601 limited-range). If the PS5 stream is BT.709, every
saturated colour shifts; the question is where the meter magenta (GDI-measured
H~150 S~203) lands in OpenCV hue after the mismatch.
"""
import numpy as np
import cv2


def rgb_to_ycbcr(rgb, kr, kb, limited=True):
    r, g, b = [c / 255.0 for c in rgb]
    kg = 1.0 - kr - kb
    y = kr * r + kg * g + kb * b
    cb = (b - y) / (2.0 * (1.0 - kb))
    cr = (r - y) / (2.0 * (1.0 - kr))
    if limited:
        Y = 16 + 219 * y
        Cb = 128 + 224 * cb
        Cr = 128 + 224 * cr
    else:
        Y = 255 * y
        Cb = 128 + 255 * cb
        Cr = 128 + 255 * cr
    return Y, Cb, Cr


def ycbcr_to_rgb_601_limited(Y, Cb, Cr):
    # what cv2.COLOR_YUV2BGR_NV12 effectively does (BT.601, limited range)
    y = (Y - 16) / 219.0
    cb = (Cb - 128) / 224.0
    cr = (Cr - 128) / 224.0
    kr, kb = 0.299, 0.114
    kg = 1.0 - kr - kb
    r = y + 2 * (1 - kr) * cr
    b = y + 2 * (1 - kb) * cb
    g = (y - kr * r - kb * b) / kg
    return tuple(int(np.clip(round(c * 255), 0, 255)) for c in (r, g, b))


def hue_of(rgb):
    bgr = np.array([[[rgb[2], rgb[1], rgb[0]]]], dtype=np.uint8)
    h, s, v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0]
    return int(h), int(s), int(v)


# Candidate "true" meter colours (BT.709 source): hue 150 in OpenCV = 300deg = magenta.
# Reconstruct plausible meter RGBs around GDI-measured H150 S~203 V high.
test_colors = []
for hh, ss, vv in [(150, 203, 255), (150, 203, 220), (150, 255, 255), (148, 190, 240)]:
    hsv = np.array([[[hh, ss, vv]]], dtype=np.uint8)
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    test_colors.append((f"meter H{hh} S{ss} V{vv}", (int(bgr[2]), int(bgr[1]), int(bgr[0]))))

print(f"{'source colour':28} {'true RGB':>15} {'-> decoded RGB':>15}   true HSV      mis-decoded HSV")
for name, rgb in test_colors:
    # encode as the PS5 would (BT.709 limited), decode as cv2 does (BT.601 limited)
    Y, Cb, Cr = rgb_to_ycbcr(rgb, kr=0.2126, kb=0.0722, limited=True)
    dec = ycbcr_to_rgb_601_limited(Y, Cb, Cr)
    print(f"{name:28} {str(rgb):>15} {str(dec):>15}   H{hue_of(rgb)}  H{hue_of(dec)}")
