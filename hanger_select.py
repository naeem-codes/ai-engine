"""Prefab hanger selection by GLASS SURFACE AREA.

Client rule (confirmed 2026-07-30): the hanger is chosen from a fixed matrix of
prefabricated part numbers, sized as a PERCENTAGE OF THE GLASS SURFACE AREA — not a
linear fraction of width or height. Client: "it should be a % base — roughly 20% of the
surface area of the glass … we find one in the matrix that is greater than 20-25% of the
total surface area and plug that in. We ideally don't want to create new hangers."

SELECTION RULE: the **LARGEST prefab that physically fits inside the glass and does NOT
exceed `MAX_AREA_FRACTION` (25%)** of the glass area. The 25% ceiling is the binding
constraint; 20% is a soft target used only for flagging, never a gate.

Why this rule and not "smallest that clears 20%" (the first implementation): it is the
only formulation that reproduces every known data point. Verified:

  glass    → chosen              basis
  36 x 36  → #1038  23.15%       matches the hanger actually in the AMBER model
  24 x 48  → #1119  18.55%       matches the hanger actually in the KELLY model
  36 x 48  → #1333  19.79%       matches the user's explicit call (2026-07-30): 19.79%
                                 "would make sense", and #1215 at 27.78% is "the larger
                                 one that won't work"

The earlier "smallest ≥ 20%" rule got AMBER right by luck but picked #1038 for KELLY
(26.04%, vs the #1119 actually built) and #1215 for 36x48 — both rejected. Overshooting
the band is the failure mode to avoid: a hanger slightly under 20% is fine, one well over
25% is not. So a below-target result (KELLY's real 18.55%) is ACCEPTED and merely flagged.

Sizes are in INCHES, W x H, in the legend's own orientation. Rotation is NOT considered:
the AMBER hanger's bounding box is 20.000" wide x 15.000" tall, matching legend #1038
"20" X 15"" in that order, so the legend order is the placed orientation.
"""

from dataclasses import dataclass, field

# The HANGER LEGEND sheet. (part number, width_in, height_in) — see
# memory reference_hanger_prefab_legend for the source photo.
PREFAB_HANGERS: list[tuple[str, float, float]] = [
    ("1417", 12.00, 7.5),
    ("1004", 14.25, 10.0),
    ("1169", 12.00, 13.0),
    ("1119", 14.25, 15.0),
    ("1038", 20.00, 15.0),
    ("1333", 14.25, 24.0),
    ("1215", 12.00, 40.0),
]

# ── Chassis dims that must TRACK the hanger width ─────────────────────────────
# The prefab hangers have their two slots cut a fixed inset in from their side edges, so
# changing the hanger width moves the slots — and the chassis HANGING TAB spacing has to
# follow or the tabs no longer seat in the slots (observed live 2026-07-30 after the hanger
# went 20" -> 14.25"). The relationship is a CONSTANT DIFFERENCE, evidenced across two
# independent products, which pins the inset at 2.125" per edge on every prefab:
#     AMBER: hanger #1038 20.00" wide,  D1@Sketch81 = 15.75"  -> 4.25"
#     KELLY: hanger #1119 14.25" wide,  D1@Sketch81 = 10.00"  -> 4.25"
# Following by the hanger's width DELTA reproduces that exactly: AMBER's 15.75" + (14.25 -
# 20) = 10.00", i.e. the value KELLY really carries for a 14.25" hanger.
#
# The offset itself is never hard-coded — it is whatever the live model already has; only
# the IDENTITY of the follower dim comes from here. `Sketch81` holds the tab spacing on both
# chassis parts seen so far (12204 and KELLY's), which suggests a shared template, but it is
# still a sketch NUMBER and therefore the fragile part of this: verify per product, and see
# `EXPECTED_TAB_INSET_IN` for the sanity check that catches a mis-identification.
HANGER_FOLLOWER_HINTS: tuple[str, ...] = ("SKETCH81",)
EXPECTED_TAB_INSET_IN = 4.25    # cross-checked; a match this far off the hanger width is
EXPECTED_TAB_INSET_TOL_IN = 1.0  # almost certainly the wrong dim, so skip it and log.

TARGET_AREA_FRACTION = 0.20   # "roughly 20%" — soft target; under it is flagged, not rejected
MAX_AREA_FRACTION = 0.25      # the binding ceiling: never exceed 25% if anything fits under it

# A hanger may satisfy the AREA band and still be absurd on the panel. JEN 50x41.5: #1215
# (12x40) is 23.13% — comfortably in band — and "fits" because 40 <= 41.5, but a 40" hanger
# mounted at an offset from the top of a 41.5" mirror runs out the bottom (reported live
# 2026-08-03). Area is blind to that; a height cap is not.
#
# Real fitted hangers, height as a fraction of the glass height:
#     KELLY  24x48  #1119 15/48 = 31%      ALPHA 30x40 #1038 15/40 = 38%
#     AMBER  36x36  #1038 15/36 = 42%      AMBER 36x48 #1333 24/48 = 50%  (user-approved)
#     JEN    50x41.5 #1215 40/41.5 = 96%   <- the failure
# Everything real is 31-50%, so 0.60 clears the approved cases with margin and rejects the
# failure decisively. Deliberately NOT an orientation test: AMBER's approved answer flips a
# landscape hanger to a portrait one, so orientation is not the discriminator — height is.
MAX_HEIGHT_FRACTION = 0.60

# ── Width span — the constraint AREA alone gets wrong on a wide mirror ────────────────────
# A mirror hangs from a HORIZONTAL span, so the hanger has to grow with the glass WIDTH. Area
# does not capture that: on a 90in glass the area ceiling capped the hanger at 20in (9% of area)
# and the hanging tabs ended up bunched at the centre, which is structurally wrong and is what
# the user reported (2026-08-06).
#
# Measured off the client's own four products — the hanger is consistently 55-67% of the glass
# WIDTH, while its share of AREA is all over the place:
#
#   glass  hanger              width%   area%
#   24     1004   14.25x10     59.4%    16.5%
#   36     1038   20.00x15     55.6%    23.1%
#   48     12239  30.00x15     62.5%    26.0%   <- over the 25% "ceiling"
#   60     3128   40.00x20     66.7%    37.0%   <- way over
#
# So area is NOT the rule the client actually follows for landscape mirrors; two of their four
# hangers breach the ceiling the selector was enforcing. Width is kept as a FLOOR (prefabs must
# span at least MIN_WIDTH_FRACTION) and as the scaling TARGET, while the area ceiling stays on
# to keep tall/portrait mirrors honest — that is the case area does describe well.
MIN_WIDTH_FRACTION = 0.55     # a prefab narrower than this cannot hold the glass
TARGET_WIDTH_FRACTION = 0.65  # what a scaled hanger aims for; reproduces 48->30 and 60->40
                              # within 4%, and picks the client's exact prefab at 24 and 36

# How far BELOW the 20% target a prefab may sit and still count as "matching". Beyond this
# the prefab is rejected and the FITTED hanger is resized instead (client 2026-08-03: "if
# there isn't one that follows the criteria then we should resize the existing hanger").
#
# Without a lower bound, eligibility only required <= 25%, so a hanger far under target still
# won won the selection: on a 50x36 glass #1038 was picked at **16.67%**, which is what
# prompted this. The line has to sit between that and the cases already validated:
#     50x36  #1038 16.67%   <- must be REJECTED (too far under)
#     KELLY  #1119 18.55%   <- must be ACCEPTED (it is what the real model ships)
#     AMBER  #1333 19.79%   <- must be ACCEPTED (user approved it explicitly)
# 2 pp (accept >= 18%) clears both keepers and rejects the failure.
ACCEPT_BELOW_TARGET_MARGIN = 0.02
MIN_ACCEPTABLE_FRACTION = TARGET_AREA_FRACTION - ACCEPT_BELOW_TARGET_MARGIN   # 0.18

# A resized hanger is a part someone has to make, so give it clean dimensions rather than
# raw square-root output (23.243" -> 23.25").
RESIZE_ROUND_TO_IN = 0.25

# Where to aim when no prefab qualifies and the fitted hanger has to be resized: the middle
# of the band, so the result is robustly inside it rather than on an edge.
RESIZE_TARGET_FRACTION = (TARGET_AREA_FRACTION + MAX_AREA_FRACTION) / 2
REVIEW_BELOW_FRACTION = 0.15  # so far under target it needs a human look. MUST stay below
                              # KELLY's real 18.55%, which is known-acceptable in practice.
FIT_MARGIN_IN = 0.0           # extra clearance required inside the glass, per side

IN_PER_M = 1 / 0.0254


@dataclass
class Candidate:
    part: str
    width_in: float
    height_in: float
    area_sq_in: float
    fraction: float           # area / glass area
    fits: bool                # physically fits inside the glass
    eligible: bool            # fits AND within the area ceiling AND within the height cap
    in_band: bool             # fits AND 20% <= fraction <= 25%
    too_tall: bool = False    # exceeds MAX_HEIGHT_FRACTION of the glass height
    too_narrow: bool = False  # spans less than MIN_WIDTH_FRACTION of the glass width


@dataclass
class HangerChoice:
    part: str | None                      # None only when nothing fits at all
    fraction: float = 0.0
    in_band: bool = False                 # inside the ideal 20-25%
    under_target: bool = False            # below 20% — acceptable, but reported
    over_ceiling: bool = False            # above 25%: nothing fit under the ceiling
    needs_review: bool = False            # no prefab fits, or the best is < 15%
    # The hanger already fitted to the model is correctly sized — leave it completely alone.
    # This is the FIRST thing checked: a bespoke hanger the client purpose-built for the
    # product must not be swapped for a catalogue part when it is already in band.
    keep_fitted: bool = False
    # No prefab qualified, so the FITTED hanger is scaled (keeping its own aspect ratio) to
    # land in the band — the client's "if there isn't a matching one, change the current one".
    resize_fitted: bool = False
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    # Prefabs that FIT but were deliberately passed over for exceeding 25% — the "larger
    # one that won't work". Reported so the decision is auditable.
    rejected_oversize: list[Candidate] = field(default_factory=list)
    # Exact CATALOGUE dimensions of the chosen prefab, for the app to write onto the
    # placed hanger (D2@Base-Flange1 = width, D1@Sketch1 = height). Snapping to these
    # rather than a computed ratio guarantees the result is always a real prefab size.
    # NOTE the resized part reproduces the prefab's OUTLINE, not its internal hole
    # pattern — the part number is the deliverable, the geometry is a stand-in. Do not
    # emit a DXF cut pattern from it.
    target_width_in: float = 0.0
    target_height_in: float = 0.0

    @property
    def target_width_m(self) -> float:
        return self.target_width_in * 0.0254

    @property
    def target_height_m(self) -> float:
        return self.target_height_in * 0.0254

    @property
    def part_name(self) -> str:
        """Filename / `Number` property stem for the chosen prefab, e.g. "1038-HANGER"."""
        return f"{self.part}-HANGER" if self.part else ""

    def log_lines(self) -> list[str]:
        """Human-readable trace of the whole decision, for the app log / rules UI."""
        if self.keep_fitted:
            head = "keep the fitted hanger"
        elif self.resize_fitted:
            head = "resize the fitted hanger"
        else:
            head = f"chose {'#' + self.part if self.part else '(none)'}"
        out = [f"[HANGER] {head} — {self.reason}"]
        for c in self.candidates:
            if c.part == self.part:
                mark = "CHOSEN"
            elif not c.fits:
                mark = "does not fit"
            elif c.too_tall:
                mark = (f"too tall — {c.height_in:g}in exceeds "
                        f"{MAX_HEIGHT_FRACTION * 100:.0f}% of the glass height")
            elif c.too_narrow:
                mark = (f"too narrow — {c.width_in:g}in spans under "
                        f"{MIN_WIDTH_FRACTION * 100:.0f}% of the glass width")
            elif not c.eligible:
                mark = f"over the {MAX_AREA_FRACTION * 100:.0f}% ceiling"
            else:
                mark = "ok (smaller)"
            out.append(f"[HANGER]   #{c.part}  {c.width_in:g}x{c.height_in:g}  "
                       f"{c.area_sq_in:7.2f} sq in  {c.fraction * 100:5.2f}%  {mark}")
        if self.under_target:
            out.append(f"[HANGER]   note: {self.fraction * 100:.2f}% is under the "
                       f"{TARGET_AREA_FRACTION * 100:.0f}% target but is the largest that "
                       f"stays within {MAX_AREA_FRACTION * 100:.0f}%")
        if self.needs_review:
            out.append("[HANGER]   NEEDS REVIEW — no suitable prefab; a custom hanger may be required")
        return out


def _rejected_oversize(candidates: list[Candidate]) -> list[Candidate]:
    """Fitting prefabs passed over for being TOO BIG — over the area ceiling or too tall.

    Deliberately excludes ones rejected for being too SMALL (under MIN_ACCEPTABLE_FRACTION):
    this list exists to show "the larger one that won't work", so folding the small ones in
    would make it meaningless.
    """
    return [c for c in candidates
            if c.fits and not c.eligible
            and (c.too_tall or c.fraction > MAX_AREA_FRACTION)]


def _band_distance(fraction: float) -> float:
    """How far a fraction sits outside the 20-25% band (0 when inside)."""
    if fraction < TARGET_AREA_FRACTION:
        return TARGET_AREA_FRACTION - fraction
    if fraction > MAX_AREA_FRACTION:
        return fraction - MAX_AREA_FRACTION
    return 0.0


def select_hanger(glass_w_in: float, glass_h_in: float,
                  max_fraction: float = MAX_AREA_FRACTION,
                  target_fraction: float = TARGET_AREA_FRACTION,
                  fit_margin_in: float = FIT_MARGIN_IN,
                  fitted_w_in: float = 0.0,
                  fitted_h_in: float = 0.0) -> HangerChoice:
    """Choose a hanger for a glass panel of `glass_w_in` x `glass_h_in` inches.

    `fitted_w_in`/`fitted_h_in` describe the hanger ALREADY in the model. Supplying them
    enables the two outcomes the client asked for beyond straight substitution:
      * it is already in the 20-25% band  -> keep it, change nothing (`keep_fitted`);
      * no prefab qualifies              -> scale it, keeping its aspect (`resize_fitted`).
    Omit them and the behaviour is pure prefab selection, as before.
    """
    glass_area = glass_w_in * glass_h_in
    if glass_area <= 0:
        return HangerChoice(part=None, needs_review=True,
                            reason="glass area is zero — cannot select a hanger")

    max_w = glass_w_in - 2 * fit_margin_in
    max_h = glass_h_in - 2 * fit_margin_in
    height_cap = MAX_HEIGHT_FRACTION * glass_h_in

    width_floor = MIN_WIDTH_FRACTION * glass_w_in

    candidates: list[Candidate] = []
    for part, w, h in PREFAB_HANGERS:
        area = w * h
        frac = area / glass_area
        fits = w <= max_w + 1e-9 and h <= max_h + 1e-9
        too_tall = h > height_cap + 1e-9
        too_narrow = w < width_floor - 1e-9
        candidates.append(Candidate(
            part=part, width_in=w, height_in=h, area_sq_in=area, fraction=frac,
            fits=fits, too_tall=too_tall, too_narrow=too_narrow,
            eligible=(fits and not too_tall and not too_narrow
                      and MIN_ACCEPTABLE_FRACTION - 1e-12 <= frac <= max_fraction + 1e-12),
            # "In band" always uses the CLIENT's stated 20-25%, never the tunable
            # arguments, so retuning can never relabel a near-miss as in-band.
            in_band=fits and TARGET_AREA_FRACTION <= frac <= MAX_AREA_FRACTION,
        ))
    candidates.sort(key=lambda c: c.area_sq_in)

    # ── 1. Is the hanger already on the model already right? ──────────────────
    # Checked FIRST and deliberately: most products carry a bespoke hanger the client made
    # for them (JEN 12239, AMY 12214, AMBER-60 12208+3128 — none in the legend), and
    # swapping a correctly-sized bespoke part for a catalogue one is never an improvement.
    if fitted_w_in > 0 and fitted_h_in > 0:
        fitted_frac = (fitted_w_in * fitted_h_in) / glass_area
        fitted_fits = (fitted_w_in <= max_w + 1e-9 and fitted_h_in <= max_h + 1e-9
                       and fitted_h_in <= height_cap + 1e-9)
        # Same acceptance window as prefab eligibility, so a fitted hanger is judged by the
        # exact standard a candidate would be — including KELLY's real 18.55%.
        # The width floor applies here too: a hanger whose AREA is in band can still be far too
        # narrow to span a wide glass, which is exactly how a 90in mirror kept a 20in hanger.
        if (fitted_fits and fitted_w_in >= width_floor - 1e-9
                and MIN_ACCEPTABLE_FRACTION <= fitted_frac <= MAX_AREA_FRACTION):
            return HangerChoice(
                part=None, fraction=fitted_frac, in_band=True, keep_fitted=True,
                target_width_in=fitted_w_in, target_height_in=fitted_h_in,
                reason=f"the fitted {fitted_w_in:g}x{fitted_h_in:g}in hanger is already "
                       f"{fitted_frac * 100:.2f}% of the {glass_area:.0f} sq in glass "
                       f"(inside the {TARGET_AREA_FRACTION * 100:.0f}-"
                       f"{MAX_AREA_FRACTION * 100:.0f}% band) — left unchanged",
                candidates=candidates, rejected_oversize=_rejected_oversize(candidates))

    rejected_oversize = _rejected_oversize(candidates)
    eligible = [c for c in candidates if c.eligible]

    if eligible:
        # LARGEST prefab that stays within the ceiling — gets as close to the 20-25% band
        # as the matrix allows without overshooting it.
        best = eligible[-1]
        under = best.fraction < target_fraction
        if best.in_band:
            note = f"inside the {target_fraction * 100:.0f}-{max_fraction * 100:.0f}% band"
        else:
            note = (f"under the {target_fraction * 100:.0f}% target, but the largest that "
                    f"stays within {max_fraction * 100:.0f}%")
        return HangerChoice(
            part=best.part, fraction=best.fraction, in_band=best.in_band, under_target=under,
            needs_review=best.fraction < REVIEW_BELOW_FRACTION,
            target_width_in=best.width_in, target_height_in=best.height_in,
            reason=f"largest prefab that fits and stays within {max_fraction * 100:.0f}% "
                   f"of the {glass_area:.0f} sq in glass: {best.fraction * 100:.2f}% ({note})",
            candidates=candidates, rejected_oversize=rejected_oversize,
        )

    # ── 3. No prefab qualifies → resize the FITTED hanger, keeping its aspect ─
    # The client's rule: "if there isn't any matching hanger then make changes to the current
    # one." Preserving the fitted aspect ratio is what stops a wide/short hanger being turned
    # into a tall/narrow one, which is how JEN's came to overflow.
    if fitted_w_in > 0 and fitted_h_in > 0:
        # WIDTH sets the span, HEIGHT then brings the AREA back into the client's 20-25% band.
        #
        # Sizing by area alone put a 20in hanger on a 90in glass (tabs bunched at the centre).
        # Sizing by width alone fixed the span but left the hanger 38.8% of the area — a huge
        # slab. Doing both is the client's instruction (2026-08-06): "to match the width it
        # should decrease the height to make it fall in the 20-25% range". On a 90x36 that gives
        # 58.5 x 12.5 = 22.6% instead of 58.5 x 21.5 = 38.8%.
        new_w = min(TARGET_WIDTH_FRACTION * glass_w_in, max_w)
        new_h = (RESIZE_TARGET_FRACTION * glass_area) / new_w if new_w > 0 else 0.0

        # Three ceilings on the height, all one-directional — the width is never reduced to
        # satisfy them, because the span is the whole point.
        #   * the glass, and MAX_HEIGHT_FRACTION of it (a hanger must not run off the bottom)
        #   * the WIDTH itself: a hanger must never come back taller than it is wide. On a tall
        #     narrow glass (24x60) the area target alone would give 15.6w x 20.8h — precisely
        #     the landscape-to-portrait flip that made JEN's overflow.
        cap = min(max_h, height_cap, new_w)
        if new_h > cap:
            new_h = cap

        # Snap to a clean increment — a resized hanger has to be manufactured, so 23.25" beats
        # 23.243". Round to NEAREST, but fall back to flooring when that would breach the limit:
        # an earlier version rejected the rounded pair outright whenever either dim sat exactly
        # on a cap, and silently shipped the raw 23.4" instead.
        def _snap(value: float, limit: float) -> float:
            step = RESIZE_ROUND_TO_IN
            nearest = round(value / step) * step
            if nearest <= limit + 1e-9:
                return nearest
            return int(value / step) * step          # floor — never exceed the limit

        s_w, s_h = _snap(new_w, max_w), _snap(new_h, cap)
        if s_w > 0 and s_h > 0:
            new_w, new_h = s_w, s_h

        if new_w > 0 and new_h > 0 and new_w <= max_w + 1e-9 and new_h <= cap + 1e-9:
            return HangerChoice(
                part=None, fraction=(new_w * new_h) / glass_area, in_band=True,
                resize_fitted=True, target_width_in=new_w, target_height_in=new_h,
                reason=f"no prefab spans {MIN_WIDTH_FRACTION * 100:.0f}% of the "
                       f"{glass_w_in:g}in glass; the fitted hanger is resized from "
                       f"{fitted_w_in:g}x{fitted_h_in:g}in to {new_w:.3f}x{new_h:.3f}in — "
                       f"{new_w / glass_w_in * 100:.1f}% of the width for the span, height set "
                       f"to bring it to {(new_w * new_h) / glass_area * 100:.1f}% of the area",
                candidates=candidates, rejected_oversize=_rejected_oversize(candidates))

    # ── 4. Nothing qualifies, and there is no fitted hanger to resize ─────────
    # Take the fitting prefab whose area sits CLOSEST to the band. One rule covers both
    # failure directions: on a tiny panel every prefab is over the ceiling and the SMALLEST is
    # closest; on a huge panel every prefab is under the target and the LARGEST is closest.
    # (The previous "smallest that fits" was right only for the first case and picked the very
    # worst option for the second — a 10%-of-glass hanger on a 60x80 panel.)
    fitting = [c for c in candidates if c.fits]
    if not fitting:
        return HangerChoice(
            part=None, needs_review=True,
            reason=f"no prefab fits inside a {glass_w_in:g}x{glass_h_in:g}in glass",
            candidates=candidates, rejected_oversize=rejected_oversize)

    smallest = min(fitting, key=lambda c: (_band_distance(c.fraction), c.area_sq_in))
    _above = smallest.fraction > MAX_AREA_FRACTION
    return HangerChoice(
        part=smallest.part, fraction=smallest.fraction,
        over_ceiling=_above, under_target=not _above,
        needs_review=smallest.fraction < REVIEW_BELOW_FRACTION,
        target_width_in=smallest.width_in, target_height_in=smallest.height_in,
        reason=f"no prefab lands in the {TARGET_AREA_FRACTION * 100:.0f}-"
               f"{max_fraction * 100:.0f}% band for the {glass_area:.0f} sq in glass; closest "
               f"that fits is #{smallest.part} at {smallest.fraction * 100:.2f}% "
               f"({'above' if _above else 'below'} the band)",
        candidates=candidates, rejected_oversize=rejected_oversize,
    )


def select_hanger_meters(glass_w_m: float, glass_h_m: float, **kw) -> HangerChoice:
    """Metre-input wrapper — the engine carries dimensions in metres."""
    return select_hanger(glass_w_m * IN_PER_M, glass_h_m * IN_PER_M, **kw)
