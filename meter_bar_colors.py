"""Shot-meter BAR colour rails for the shot-gated reader (simple_meter_reader).

SCOPE: Red and Purple only.  Those are the two colours with live provenance --
Red is what the rig runs, and Purple was the legacy detector's shipped default
(372 confirmed live detections, and the cleanest decor exposure measured in the
same arena: 4 px/frame vs Red's 18,836).  Orange/Yellow/Cyan/White hue rails
exist in ``meter_detector.BGR_COLOR_RANGES`` but were ported from config tables
and have NEVER been validated on a live meter, so they are NOT offered here.
Adding a colour means adding live evidence for it, not copying a row out of that
table.

RED'S NON-REGRESSION EVIDENCE.  The framedump this was originally to be replayed
against has been DELETED, so a 5,000-frame byte-identity replay is no longer
available.  Red's guarantee is therefore structural rather than statistical: a
Red-configured reader reaches the IDENTICAL ``cv2.inRange`` call with the
IDENTICAL constants it has always shipped with, because the reader builds its
Red rows from its own live ``_RED_LO``/``_RED_HI``/``_COURTWIDE_RED``/
``_MICRO_RED`` instance constants rather than from this table (see
``SimpleMeterReader._rebuild_bands``).  Red never enters the HSV arm at all.
``tests/test_simple_meter_reader.py`` pins that property from both directions:
``test_red_band_rows_are_the_live_reader_constants`` and
``test_red_never_enters_the_hsv_arm``.

------------------------------------------------------------------------------
BAND SCHEMA -- a TAGGED UNION, deliberately.

    ("bgr", lo_bgr, hi_bgr)
        A literal ``cv2.inRange(frame_bgr, lo, hi)`` box.  Red uses this form so
        that a Red-configured reader reaches the IDENTICAL inRange call with the
        IDENTICAL constants it ships with today -- Red is byte-identical BY
        CONSTRUCTION, not by measurement.  That property is the whole reason the
        union is tagged rather than everything being normalised to HSV.

    ("hsv", hue_lo, hue_hi, s_floor, v_floor)
        Hue window on the OpenCV H scale (0-179) with SATURATION and VALUE
        FLOORS.  The floors are CONJUNCTS of the hue test, never a fallback
        branch: a desaturated or dark pixel must never reach the hue comparison
        at all, because hue is meaningless (and numerically wild) near the grey
        axis.  ``hue_lo < 0`` means the window wraps through 0 and is tested as
        ``(H <= hue_hi) | (H >= 180 + hue_lo)``.  No shipped row wraps -- Red,
        the only colour that would, is a BGR row -- but the form is supported so
        a wrapping colour cannot be added incorrectly later.

Three tiers per colour, mirroring the reader's existing tier structure:
    nominal    -- the strict in-band read (``_RED_LO``/``_RED_HI``)
    courtwide  -- the relaxed distant/half-court acquire (``_COURTWIDE_RED``)
    micro      -- the sub-quarter-scale full-frame tier (``_MICRO_RED``)
------------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

BAND_NOMINAL = "nominal"
BAND_COURTWIDE = "courtwide"
BAND_MICRO = "micro"

#: The only colours this reader will run an inference mask for.
SUPPORTED: Tuple[str, ...] = ("Red", "Purple", "White")

#: Colour used when an unsupported/unknown name is configured.
FALLBACK = "Red"


# --------------------------------------------------------------------------- #
#  RED -- BGR rows, character-for-character the shipped SimpleMeterReader
#  constants.  DO NOT "tidy" these into HSV.  They are duplicated from
#  simple_meter_reader.SimpleMeterReader._RED_LO/_RED_HI, ._COURTWIDE_RED and
#  ._MICRO_RED, and the byte-identity gate for the Red replay is exactly the
#  claim that these three rows still equal those three constants.
#
#  Note that `_MICRO_RED` is NOT a red band -- its corner (110,110,110) is
#  neutral grey -- which is why the micro tier additionally applies the
#  red-DOMINANCE floor (ORION_READER_MICRO_RED_DOM).  See `micro_needs_dominance`
#  below: that floor is meaningful for BGR rows ONLY.
# --------------------------------------------------------------------------- #
_RED = {
    BAND_NOMINAL:   ("bgr", (0, 0, 220), (60, 60, 255)),
    BAND_COURTWIDE: ("bgr", (0, 0, 170), (70, 70, 255)),
    BAND_MICRO:     ("bgr", (0, 0, 110), (110, 110, 255)),
}


# --------------------------------------------------------------------------- #
#  PURPLE -- HSV rows.  A BGR box cannot separate the purple bar from the arena
#  without also admitting the blue-grey stadium furniture, which is why the
#  legacy detector carried a dedicated HSV band plus a purity gate for exactly
#  this colour.
#
#  EVERY BOUND BELOW IS CLAMPED TO LIVE-PROVEN PURPLE EVIDENCE in
#  meter_detector.py -- it is not a widened guess:
#
#    _HSV_FILL_RANGES["Purple"]  = ([140,150,150], [158,255,255])   (:666)
#        -> hue 140-158, S floor 150, V floor 150
#    purple_purity_hue_max       = 159.0                            (:331)
#    purple_purity_sat_min       = 165.0                            (:332)
#    _METER_HUE_CENTER["Purple"] = 148.0                            (:728)
#        with the comment: "live moving-meter median spans ~137-159; the clean
#        static crop reads ~149-150 ... and excludes the pink floatie (Hue~166)"
#
#  THE HUE CEILING OF 159 IS LOAD BEARING AND MUST NOT BE RAISED.  Hue ~166 is
#  the in-arena PINK FLOATIE, and the shipped purple gate exists specifically to
#  reject it.  A proposed rail set of 138-162 / 136-164 / 134-166 was rejected
#  during implementation: its micro row lands its ceiling exactly ON the floatie
#  hue, and the micro tier is the FULL-FRAME scan -- the single most
#  false-positive-prone tier in the reader.  Widening past 159 re-admits the one
#  false positive purple was already hardened against.
#
#  Tiering therefore relaxes the SATURATION/VALUE floors (which is what a
#  distant, dimmer, more compressed meter actually costs you) and opens the hue
#  window only DOWNWARD toward the measured live low of ~137 -- never upward.
# --------------------------------------------------------------------------- #
_PURPLE = {
    # Strict read: the shipped fill band's hue window, with the live-proven
    # purity saturation floor (165) rather than the looser fill floor (150).
    BAND_NOMINAL:   ("hsv", 140, 158, 165, 150),
    # Distant/half-court: hue opened down to the live span low and up only to
    # the purity ceiling; S/V relaxed for a smaller, dimmer bar.
    BAND_COURTWIDE: ("hsv", 138, 159, 140, 120),
    # Full-frame micro tier: the complete measured live hue span, still ceilinged
    # at 159.  The S floor stays high (130) precisely BECAUSE this tier scans the
    # whole frame -- it is the only thing keeping arena architecture out.
    BAND_MICRO:     ("hsv", 137, 159, 130, 100),
}


# --------------------------------------------------------------------------- #
#  GREEN IS DELIBERATELY ABSENT -- and must stay absent.  This is not an
#  oversight and not a "not yet validated" case like Orange/Yellow/Cyan/White.
#  A Green bar rail is STRUCTURALLY UNSAFE in this reader:
#
#  The reader is colour-invariant about ONE thing -- the GREEN MAKE-WINDOW CAP.
#  It finds the cap to establish `track_top`, and fill% is measured against that
#  cap-to-floor track.  The cap masks are:
#      SimpleMeterReader._G = ((38,90,90), (85,255,255))   -> hue 38-85
#      _NEON_LO/_NEON_HI    = (48,140,140)/(70,255,255)    -> hue 48-70
#  and a green BAR measures hue 45-72 (meter_detector._COLOR_PURITY_GATES
#  ["Green"] = hue_min 45.0, hue_max 72.0; _METER_HUE_CENTER["Green"] = 58.0).
#
#  Those windows OVERLAP almost totally.  A green bar is therefore masked AS the
#  make-window cap: `track_top` collapses onto the fill boundary, the track
#  height goes to ~0, and every frame reads ~100% fill.  The bot would not
#  mis-time the shot -- it would fire INSTANTLY on every single shot, at maximum
#  confidence, with no rejection reason, because from the reader's point of view
#  the meter is genuinely full.
#
#  That is strictly worse than not firing at all: a colour that cannot be
#  detected fails closed and is visible to the user, whereas this fails OPEN and
#  looks like the bot working.  Green cannot be supported until the cap locator
#  is given a colour-independent signal.  Do not add a "Green" row here.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
#  WHITE -- BGR rows.  NBA 2K27 (early access, 2026-08-26) ships a WHITE-ONLY
#  shot meter: there is no colour choice in the game any more, so this is not an
#  optional extra rail -- it is the only one 2K27 can be read through.
#
#  MEASURED, not ported from a config table (the standard this module sets at
#  its top).  Evidence: framedump session_20260826_185753, 1629 live capture-card
#  frames of 2K27 practice shooting, 791 of them with the meter on screen:
#
#    fill interior      : H irrelevant, S=1, V=255 (B255 G254 R255) -- the fill is
#                         opaque pure white.  Across all 791 frames the fill's
#                         saturation p95 was median 1 / worst 30, and its value
#                         p05 was median 240 / worst 176 (edge antialiasing).
#    meter's own frame  : the grey outline + graduation ticks peak at V~199.
#                         THIS is what sets the floors below -- 235/220/210 clear
#                         the meter's own furniture by 36/21/11 points.  Do not
#                         drop a floor to 200 or the ticks join the mask and the
#                         fill edge walks.
#    false positives    : a V>=235 / S<=30 gate found spurious white above the
#                         fill in 1 frame out of 791 (0.1%) over real court,
#                         including white court lines, jerseys and backboard.
#
#  WHY BGR AND NOT HSV.  White is achromatic, and the HSV row form here is
#  FLOORS-only (hue window + S floor + V floor) precisely so the grey axis is
#  unrepresentable -- see _derive_bar_hsv's HSV_SAT_FLOOR_MIN.  White cannot be
#  written in that form at all.  A BGR box can: requiring all three channels
#  >= the floor implicitly bounds saturation to <= (255-floor), i.e. <=20 at the
#  nominal floor, which is exactly the achromatic gate we want and rejects any
#  saturated bright colour (a blue court line has B high but G/R low).
#
#  THE MICRO TIER IS DELIBERATELY REFUSED FOR WHITE -- see micro_supported().
#  A row is still defined here because BAND_MICRO doubles as the reader's
#  widest-band reference for the arm-edge decor veto, which wants recall.
# --------------------------------------------------------------------------- #
_WHITE = {
    BAND_NOMINAL:   ("bgr", (235, 235, 235), (255, 255, 255)),
    BAND_COURTWIDE: ("bgr", (220, 220, 220), (255, 255, 255)),
    BAND_MICRO:     ("bgr", (210, 210, 210), (255, 255, 255)),
}


_TABLE: Dict[str, Dict[str, tuple]] = {
    "Red": _RED,
    "Purple": _PURPLE,
    "White": _WHITE,
}


def normalize(meter_color) -> str:
    """Map a configured colour name onto a SUPPORTED colour, warning on fallback.

    Unknown, unsupported and empty values all fall back to Red rather than
    raising: the reader must keep reading on a mislabeled config, and Red is the
    only colour with a full byte-identical validation behind it.
    """
    name = str(meter_color or "").strip()
    if name in _TABLE:
        return name
    # Accept case-insensitively before giving up -- a config carrying "purple"
    # is a labelling slip, not a request for a different colour.
    for supported in SUPPORTED:
        if name.lower() == supported.lower():
            return supported
    logger.warning(
        "meter_color=%r is not a supported bar colour %s; falling back to %s. "
        "Set the in-game shot meter colour to one of %s.",
        meter_color, list(SUPPORTED), FALLBACK, " or ".join(SUPPORTED),
    )
    return FALLBACK


def band(meter_color, tier: str = BAND_NOMINAL) -> tuple:
    """Return the tagged band row for ``meter_color`` at ``tier``.

    The returned row is one of the two tagged forms documented at module top.
    Callers MUST dispatch on ``row[0]`` and must never index a row positionally
    assuming BGR -- that assumption is what makes a half-working colour.
    """
    colour = normalize(meter_color)
    rows = _TABLE[colour]
    if tier not in rows:
        raise KeyError(
            "unknown band tier %r (expected one of %s)"
            % (tier, (BAND_NOMINAL, BAND_COURTWIDE, BAND_MICRO)))
    return rows[tier]


def is_bgr(row: tuple) -> bool:
    """True when ``row`` is a literal BGR inRange box."""
    return bool(row) and row[0] == "bgr"


def is_hsv(row: tuple) -> bool:
    """True when ``row`` is an HSV hue-window-with-floors row."""
    return bool(row) and row[0] == "hsv"


#: Per-colour acquisition WIDTH FLOOR, in 1080p-reference pixels (the reader
#: scales it by the live frame width). None = keep the reader's own W_MIN.
#:
#: The reader's W_MIN/W_MAX = 20/34 are commented "Arrow2 contour width 23-30",
#: i.e. measured off the OLD red meter of the previous game. NBA 2K27's white meter is a
#: much narrower ribbon: measured across 791 live frames it is 11-12 px wide at
#: 1280x720, which is ~17 px at 1920x1080. Both fall UNDER the scaled floor
#: (13 px at 720p, 20 px at 1080p), so the real meter was rejected at every
#: resolution and the reader latched whatever small decor blob passed instead --
#: observed directly: an 11x8 acquire while the true meter was 102x11.
#:
#: 12 admits the measured ~17 px bar with ~5 px of slack for perspective and
#: compression, without touching Red/Purple, whose floors stay exactly as shipped.
_WIDTH_FLOOR_1080P: Dict[str, int] = {"White": 12}


#: Per-colour GREEN-TIP anchor geometry, in 1080p-reference pixels.
#: {colour: (gw_min, gh_min, g_area_min)}; absent = keep the reader's own constants.
#:
#: The reader's GW_MIN/GH_MIN/G_AREA_MIN (16 / 6 / 200) are commented "Measured on the
#: live Red/Arrow2 meter: w~28-30, h~24-30, area~500-690 @1080p". NBA 2K27's white meter
#: has a far smaller apex: measured median 11w x 5h, area 32 at 1280x720, i.e. roughly
#: 17 x 8 / ~72px at 1080p. Against the red-era floors it fails on height (33% of frames),
#: width (34%) and area (29%) -- so the green-tip anchor NEVER FIRES on 2K27.
#:
#: That anchor is what holds the lock when the white column is too small to scan, which is
#: exactly the low-fill window the shot is timed in. With it dead, 42.6% of true-meter
#: frames fall through to coast, and a coasted frame reports no cap at all -- which is why
#: the cap was reported on 24% of frames while being findable on 98%.
#:
#: Floors are set to admit the measured p05 apex, NOT to the median, so a genuinely small
#: tip still anchors. See the risk note in `green_tip_floors`.
_GREEN_TIP_1080P: Dict[str, tuple] = {"White": (8, 2, 40)}


def green_tip_floors(meter_color, default: tuple) -> tuple:
    """(gw_min, gh_min, g_area_min) at 1080p reference for this colour.

    RISK, stated plainly: G_AREA_MIN is the reader's principal anti-decor guard for green
    anchors -- its comment notes decor "carries NO saturated green in the band -> no false
    hold". Lowering it for White admits small saturated-green blobs (jersey trim, court
    paint, scoreboard) as potential anchors. Three things bound that: this is scoped by
    COLOUR NAME so Red/Purple are untouched, the strict-tip paths keep their compactness
    and centring checks, and a capless false hold is broken within 2.5s by the White
    capless breaker. The alternative -- relaxing the white column's width/height gates
    instead -- was rejected: it rescues fewer frames AND increases white-decor acceptance,
    which is already the dominant wrong-lock mode.
    """
    return _GREEN_TIP_1080P.get(normalize(meter_color), default)


def width_floor(meter_color, default: int) -> int:
    """The 1080p-reference acquisition width floor for this colour."""
    return _WIDTH_FLOOR_1080P.get(normalize(meter_color), default)


def micro_supported(meter_color) -> bool:
    """May the FULL-FRAME micro acquire tier run for this colour?

    Red/Purple: yes.  White: NO, and this is a safety refusal, not a gap.

    The micro tier is the only tier that scans the whole frame, and it is the
    most false-positive-prone surface in the reader.  Two things make it
    specifically unsafe for a white bar:

      * A useful micro row has to relax its floor toward grey, and grey is where
        every bright neutral in an arena lives (court lines, jersey numbers,
        scoreboard, backboard).  Red repairs that hole with the red-DOMINANCE
        floor (r - max(g,b) >= N); white HAS no dominance -- r == g == b is its
        definition -- so the repair is unavailable in principle.
      * `_MICRO_GREEN`'s saturation floor is 45, which OVERLAPS white's legal
        saturation band.  Under a micro lock the reader latches that green band
        for the life of the lock, so tinted-white bar pixels could be masked AS
        the make-window cap -- collapsing track_top onto the fill and reading
        ~100% every frame.  That is the green fail-open pathology, and it fails
        OPEN (a bot that fires instantly at full confidence) rather than closed.

    White does not need the tier: it is measured at S<=30 / V>=235 against real
    court, where the nominal and courtwide tiers already separate it cleanly.
    """
    return normalize(meter_color) != "White"


def micro_needs_dominance(row: tuple) -> bool:
    """Does the micro tier's red-DOMINANCE floor apply to this row?

    BGR rows ONLY.  The dominance floor (``r - max(g, b) >= N``) exists because a
    BGR box cannot express "red" -- its neutral-grey corner admits every dim
    grey/brown/stone pixel in the arena.  An HSV row does not have that hole:
    its SATURATION floor already makes the grey axis unrepresentable, which is
    the exact property dominance was bolted on to recover.  Applying a
    red-dominance test to a purple mask would additionally be WRONG on its own
    terms, since purple's blue channel legitimately exceeds its red channel.

    NOTE: this is a row-FORM test and White is also a BGR row, so it answers True
    for White.  That is why `micro_supported()` refuses White's micro tier
    outright: relying on `r - max(g,b) >= N` to zero a white mask would be
    correct only by accident (white has no dominance, so the mask empties), and
    an accident is not a safety property.  Callers must consult
    `micro_supported()` first.
    """
    return is_bgr(row)


def supports_channel_widening(row: tuple) -> bool:
    """May the colour-tolerance widening path mutate this row in place?

    Only BGR rows.  That path does per-CHANNEL arithmetic (lower the R floor,
    raise the B/G ceilings), which is meaningless applied to (hue, sat, val)
    scalars -- it would silently walk a hue window into a neighbouring colour.
    HSV rows widen via their S/V floors through the tier table instead.
    """
    return is_bgr(row)
