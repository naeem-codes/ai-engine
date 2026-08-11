"""Chassis B-slot length as a function of chassis width.

The mounting-slot row is what stops a chassis shrinking past its size band. On AMBER's 60"
chassis (`12211-CHASSIS`) four slots per row, 7.78" overall, sit between 4.000" end insets; below
about a 39" chassis they no longer fit and `Cut-Extrude4` fails with `swSketchErrorExtRefFail`,
which aborts the whole resize.

The client's own answer is a different part per band (`12204-CHASSIS` at 36" carries 3 slots at
6.28"). Shortening the slots instead keeps the fitted part and is what makes 60" -> 36" build.

MEASURED LIVE on the real AMBER 60x36, not inferred. With the tab spacing already corrected:

    slot 6.75" -> FAILS      slot 6.50" -> clean      (at a 35" chassis)
    stock 7.5" at 39.0" chassis -> clean      at 38.5" -> FAILS

Both boundaries are predicted to within 0.001" by fitting `count` slots plus `count - 1` gaps
between the insets, on a body whose bounding box is the sketch dim plus two gauge thicknesses:

    count * (L + slot_width) + (count - 1) * gap  <=  (W + 2 * gauge) - 2 * inset

Solved for L at gap = `min_gap_in`, which is why the numbers land exactly rather than
approximately. `L` throughout is the CENTRE-TO-CENTRE length (the dim `D2@Sketch113` holds);
overall slot length is `L + slot_width`, which is what the drawings call out.

EVERY parameter is MEASURED off the live model and arrives as `InterpretRequest.slot_rows` — count,
slot width, inset and the driving dim. Nothing here is keyed to a part number.

⚠️ It was keyed to one, briefly, and that is the mistake this file exists to remember. `count`
looked unreadable (all the slot profiles live in ONE sketch, no pattern feature), so the spec was
recorded per chassis part. The result: it worked on `12211-CHASSIS` and did nothing whatsoever on
`12204-CHASSIS` — the 36" band part of the SAME product family — and that resize aborted exactly as
it had before. The count is perfectly readable after all, as the number of closed contours in the
sketch (`SolidWorksService.MeasureSlotRows`).
"""

from __future__ import annotations

from dataclasses import dataclass

IN = 0.0254


@dataclass(frozen=True)
class SlotSpec:
    """Everything needed to size one chassis part's mounting-slot row. Inches."""

    part: str            # chassis part number, matched against the dim's [component] suffix
    length_dim: str      # the dim holding the slot centre-to-centre length
    stock_length_in: float   # as drawn — never exceeded, so growing a mirror changes nothing
    count: int           # slots per row (NOT readable from the model — see module docstring)
    slot_width_in: float     # overall length = centre-to-centre + this
    inset_in: float      # from each end of the chassis to the outermost slot
    gauge_in: float      # sheet thickness; bbox = sketch width + 2 * gauge
    min_gap_in: float    # smallest gap left between slots (0 would sit exactly on the limit)


# ⚠️ NO HARD-CODED PARTS. This used to hold one entry for `12211-CHASSIS`, which meant the whole
# feature silently did nothing on `12204-CHASSIS` — the 36in-band part of the very same product
# family — and the resize aborted exactly as before (reported 2026-08-06). Specs are now MEASURED
# per model and arrive on the request as `slot_rows`; see `spec_from_measurement`.
CHASSIS_SLOTS: tuple[SlotSpec, ...] = ()

MIN_GAP_IN = 0.25   # smallest gap left between slots (0 would sit exactly on the limit)


def spec_from_measurement(row) -> SlotSpec | None:
    """Build a spec from a `SlotRowIn` the app measured off the live model.

    The gauge term is folded into the measurement: the app reports the part's real bounding-box
    width, so no sheet-thickness correction is needed here — that was only ever a way of turning a
    sketch dim into a body width when both were hard-coded.
    """
    if row is None or row.count < 2 or row.length_meters <= 0:
        return None
    return SlotSpec(
        part=row.component or "measured",
        length_dim=row.dim,
        stock_length_in=row.length_meters / IN,
        count=row.count,
        slot_width_in=row.slot_width_meters / IN,
        inset_in=row.inset_meters / IN,
        gauge_in=0.0,                     # already included in the measured part width
        min_gap_in=MIN_GAP_IN,
    )

# Round down to this increment: someone has to cut the slot, so 6.2495" is not a real answer.
ROUND_TO_IN = 1.0 / 16.0



def max_length_in(chassis_width_in: float, spec: SlotSpec) -> float:
    """Longest centre-to-centre slot that still fits at this chassis width.

    Straight from the module docstring's inequality; can come out <= 0 on an absurdly narrow
    chassis, which callers must treat as "no slot length works here".
    """
    span = (chassis_width_in + 2 * spec.gauge_in) - 2 * spec.inset_in
    gaps = (spec.count - 1) * spec.min_gap_in
    return (span - gaps) / spec.count - spec.slot_width_in


def slot_length_for(chassis_width_in: float, spec: SlotSpec,
                    scale: float = 1.0) -> float | None:
    """Target slot length at this chassis width, or None to leave the slots alone.

    The slots SCALE WITH THE CHASSIS, in both directions: `scale` is the part-width ratio this
    resize applies, and the slot length tracks it like any other dependent dim. `max_length_in`
    is then applied as a CEILING, so a shrink can never leave a row that will not fit and
    `Cut-Extrude4` can never fail with `swSketchErrorExtRefFail`.

    This used to be shorten-only, clamped at the drawn length ("stock still fits — leave it
    exactly as drawn"). Two problems with that, both reported on AMBER 36x36. Growing a mirror
    left the slots at their old length while everything around them got longer. Worse, the
    "drawn" length is actually MEASURED off the live model each request, so a shrink that was
    saved became the new ceiling — shrink-then-grow ratcheted the slots permanently shorter and
    could never restore them (`D2@Sketch113` stuck at 6.000" on a 35.118" part, a width where
    the drawn 7.780" fits comfortably).

    Returns None only when nothing needs writing: no width change, no room at any length, or a
    target that rounds back onto the current value.
    """
    if scale <= 0:
        return None
    limit = max_length_in(chassis_width_in, spec)
    if limit <= 0:
        return None                     # nothing fits at this width; no length can rescue it

    target = spec.stock_length_in * scale        # scales up as readily as down
    if target > limit:
        target = limit                  # the ceiling: only ever cuts a target down, never up

    rounded = int(target / ROUND_TO_IN) * ROUND_TO_IN     # floor, never round up past the limit
    if rounded <= 0:
        return None
    if abs(rounded - spec.stock_length_in) < ROUND_TO_IN / 2:
        return None                     # already there to within a cuttable increment
    return rounded


def log_lines(chassis_width_in: float, spec: SlotSpec, target_in: float | None,
              current_in: float, scale: float = 1.0) -> list[str]:
    """Human-readable trace of the decision, for the engine log.

    Says which of the two numbers won — the scaled target or the fit ceiling — because when a
    grown slot comes out shorter than the ratio asked for, that clamp is the only explanation.
    """
    limit = max_length_in(chassis_width_in, spec)
    scaled = spec.stock_length_in * scale
    head = (f"[SLOTS] {spec.part} at {chassis_width_in:.3f}\" chassis: {spec.count} slots + "
            f"{spec.count - 1} gaps between {spec.inset_in:.3f}\" insets → longest that fits is "
            f"{limit:.3f}\" (from {spec.stock_length_in:.3f}\", scaled x{scale:.4f} "
            f"= {scaled:.3f}\"{' — CLAMPED to the fit limit' if scaled > limit else ''})")
    if target_in is None:
        return [head, "[SLOTS]   no change needed — target rounds onto the current length"]
    gap = ((chassis_width_in + 2 * spec.gauge_in) - 2 * spec.inset_in
           - spec.count * (target_in + spec.slot_width_in)) / (spec.count - 1)
    return [
        head,
        f"[SLOTS]   {spec.length_dim}: {current_in:.3f}\" → {target_in:.3f}\" "
        f"(overall {target_in + spec.slot_width_in:.3f}\", leaving {gap:.3f}\" gaps)",
    ]
