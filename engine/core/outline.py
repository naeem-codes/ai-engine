"""The mirror's OUTLINE, and the one question every fit check needs to ask it.

Every fit check in this engine used to ask "how wide is this part", meaning its widest point.
That is the right question for a rectangle and the wrong one for everything else, because the
hardware that has to fit — the hanging tabs, the mounting slots, the clips — sits HIGH UP, near
the top of the part, where a curved outline has already narrowed.

Two failures on CAPSULE 20x40, both measured live on 2026-09-17, both this one mistake:

  * **20x40 -> 30x40 aborted.** The hanger follower pushed the tab row out to where a 28"-wide
    RECTANGLE has 3" to spare. On the real obround the slot's outer end landed 1.17" off the
    sheet, `Cut-Extrude2` failed with `swSketchErrorExtRefFail`, and the resize rolled back.
  * **20x40 -> 40x50 built, and was wrong.** The clip held its gap from the top edge and its gap
    from the side edge — each correct on its own axis. Under a curved end the limit is DIAGONAL,
    and the diagonal collapsed: 2.20" of clearance became 0.06". The clip ended up inside
    `12376-CHASSIS TOP`, the end cap built on that very arc.

So: one function, `half_width_at`, and three answers behind it.

**The shape is MEASURED, not guessed.** The app divides a part's real area by the area of its
bounding rectangle and sends the verdict. At 30 x 42 the four outlines the client draws sit at
1.000 / 0.847 / 0.785 — eight percent apart, no close calls. Measured live: CAPSULE's glass came
back 714.2 sq in against a textbook obround's 714.16 (0.006%), and MICHELLE's glass and chassis
agreed independently at 0.779 and 0.781 against an ellipse's 0.785.

That matters because edge-reading would NOT have settled it. MICHELLE's side flange flattens to
48.4320", which matches neither an ellipse (47.23") nor an obround (46.57") built on its 30 x 42
glass, so its ends may well be splines drawn to look like arcs. Area cannot be fooled by that.

**A rectangle answers "full width at every height".** That is not a special case bolted on: it is
what `half_width_at` returns for `rect`, and for any shape string this module does not recognise.
So every rectangular product in the catalogue behaves bit-for-bit as it did before, and an older
app that sends no shape at all gets the old behaviour rather than a new opinion.
"""

from __future__ import annotations

import math

RECT = "rect"
ROUND = "round"
OBROUND = "obround"
ELLIPSE = "ellipse"

# The shapes where "how wide is it" and "how wide is it up there" are different questions.
CURVED: frozenset[str] = frozenset({ROUND, OBROUND, ELLIPSE})

# Clear space kept between a part and the shell it sits in before it counts as fouling it.
# A quarter inch — the same margin the round path's rim check uses, and the same one the
# no-overlap floor uses, so the three do not disagree about what "touching" means.
MARGIN_M = 0.25 * 0.0254


def normalise(shape: str | None) -> str:
    """The shape as one of this module's constants, or `RECT` for anything unrecognised.

    Unrecognised means "behave exactly as before", never "guess". A typo, a shape a future app
    learns to measure, a missing field from an older build — all of them land on the rectangle,
    which is the behaviour every product in the catalogue already has.
    """
    s = (shape or "").strip().lower()
    return s if s in (RECT, ROUND, OBROUND, ELLIPSE) else RECT


def is_curved(shape: str | None) -> bool:
    return normalise(shape) in CURVED


def half_width_at(y: float, half_w: float, half_h: float, shape: str | None) -> float:
    """Half the outline's width at height `y`, measured from the centre. Metres in, metres out.

    `y` is signed but only its magnitude matters — every outline here is symmetric about both
    axes, which is true of all four and is what lets one function serve them.

    A point outside the outline's vertical extent returns 0.0 rather than raising: callers are
    fit checks, and "nothing fits there" is the answer they want, not an exception.

    ROUND is folded in with ELLIPSE deliberately. A circle IS an ellipse with equal axes, and
    the app sends the diameter as both extents on a round product, so the same branch is exact
    for it. Keeping a separate case would be a second place to get the same maths wrong.
    """
    if half_w <= 0 or half_h <= 0:
        return 0.0
    kind = normalise(shape)
    if kind == RECT:
        return half_w

    d = abs(y)
    # Strictly OUTSIDE only. At exactly the top each branch below already gives the right
    # answer, and for a landscape obround that answer is not zero: its caps are on the sides,
    # so the very top is the flat edge between them and still `half_w - r` wide.
    if d > half_h:
        return 0.0

    if kind in (ELLIPSE, ROUND):
        return half_w * math.sqrt(max(0.0, 1.0 - (d / half_h) ** 2))

    # OBROUND: a rectangle capped by two semicircles, radius = HALF THE SHORT SIDE. Which pair
    # of ends is capped follows from that, and is not assumed: a tall product (CAPSULE, 20 wide
    # x 40 tall) is capped top and bottom; a landscape one is capped left and right, where every
    # height still has the straight middle section plus whatever the cap contributes.
    if half_w <= half_h:
        r = half_w
        flat = half_h - r                 # half the straight section
        if d <= flat:
            return half_w
        off = d - flat
        return math.sqrt(max(0.0, r * r - off * off))

    r = half_h
    return (half_w - r) + math.sqrt(max(0.0, r * r - d * d))


def fits(x: float, y: float, half_w: float, half_h: float, shape: str | None,
         overhang: float = 0.0, margin: float = MARGIN_M) -> bool:
    """Does a part centred at (`x`, `y`), reaching `overhang` either side of it, sit inside?"""
    return abs(x) + overhang + margin <= half_width_at(y, half_w, half_h, shape) + 1e-12


def max_x_at(y: float, half_w: float, half_h: float, shape: str | None,
             overhang: float = 0.0, margin: float = MARGIN_M) -> float:
    """How far off centre a part at height `y` may sit before it fouls the shell.

    Never negative: a part with nowhere legal to go returns 0.0, and the caller decides whether
    that means "hold it where it is" or "this size does not work".
    """
    return max(0.0, half_width_at(y, half_w, half_h, shape) - overhang - margin)
