"""A HANGER TEMPLATE is a drilling plate, not a hanger, and must resize with the glass.

Live 2026-09-21, LUCY 48 -> 55. The rule generator blocked both templates out of the rule set:

    BLOCK D1@Sketch1  [LUCY 48-HANGER TEMPLATE-1]    — Hanger is a prefabricated part
    BLOCK D1@Sketch1  [LUCY 48-HANGER TEMPLATE-B-1]    selected by size — never scaled
    BLOCK D1@Sketch14 [both]

so no rule could drive them and they stayed at 48.000" on a 55" mirror. Their `D1@Sketch1` IS
the glass diameter to the thousandth, and the plate is 47.3 x 20 x 0.25in — it spans the glass.
The same word made the app's `CollectHangers` count three hangers on LUCY and refuse the #1215
prefab swap ("refusing to act on one of several").

The prefab freeze itself is right and stays: a hanger is SELECTED from the legend, never
scaled — ratio-scaling one took a 15.000" hanger to 20.000" on 2026-07-30. This only says the
freeze does not apply to a part that merely has the word in its name.

Safe because the two sets do not overlap, measured over every model on this machine:

    45 hanger files, all "<number>-HANGER" (plus HANGER-EXTRA)   none contains TEMPLATE
     7 template files, all containing TEMPLATE                   none is a "<number>-HANGER"

Both halves are asserted below, because an exception to a freeze is the kind of change that
quietly unfreezes the thing the freeze was written for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resize_policy as policy

# Every distinct real hanger shape on this machine: the prefab legend, a client CUSTOM copy,
# and JEN's second hanger.
REAL_HANGERS = [
    "D2@Base-Flange1 [12446-HANGER-1]",           # LUCY
    "D1@Sketch1 [1004-HANGER-1]",                 # ECLIPSE, HALO
    "D1@Sketch1 [1038-HANGER-1]",                 # prefab legend
    "D1@Sketch1 [1119-HANGER-1]",
    "D1@Sketch1 [1215-HANGER-1]",                 # the one LUCY 55 should have been given
    "D1@Sketch1 [12456-HANGER-1]",                # MICHELLE
    "D1@Sketch1 [1111-HANGER-CUSTOM-33_25X23_25-1]",
    "D1@Sketch1 [HANGER-EXTRA-1]",                # JEN's second hanger
]

TEMPLATES = [
    "D1@Sketch1 [LUCY 48-HANGER TEMPLATE-1]",
    "D1@Sketch14 [LUCY 48-HANGER TEMPLATE-1]",
    "D1@Sketch1 [LUCY 48-HANGER TEMPLATE-B-1]",
    "D1@Sketch14 [LUCY 48-HANGER TEMPLATE-B-1]",
    "D1@Sketch1 [HANGER TEMPLATE-ECLIPSE-30.00-LED-2025-1]",
    "D1@Sketch1 [HANGER TEMPLATE-ECLIPSE-48.00-LED-2025-1]",
    "D1@Sketch1 [HANGER TEMPLATE-ECLIPSE-70.00-LED-2025-1]",
]


def test_every_real_hanger_is_still_frozen():
    """The freeze this exception could have broken. It must not have."""
    for dim in REAL_HANGERS:
        assert policy.fixed_size_reason(dim), dim
        assert policy.is_hanger(dim), dim


def test_no_template_is_frozen_or_mistaken_for_the_hanger():
    for dim in TEMPLATES:
        assert policy.fixed_size_reason(dim) is None, dim
        assert not policy.is_hanger(dim), dim
        assert policy.is_template(dim), dim


def test_the_template_is_free_to_follow_the_glass():
    """The fault as a number: the block is what kept it at 48.000" on a 55" mirror.

    `D1@Sketch1` is 48.000in, exactly the master, so it clears the frame-spanning bar and the
    constant offset gives 55.000in. Nothing else was ever in the way.
    """
    import interpret
    IN = 0.0254
    dim = "D1@Sketch1 [LUCY 48-HANGER TEMPLATE-B-1]"
    allowed, blocked = policy.filter_axis_dims([dim], "width", None, {dim: 48.0 * IN})
    assert allowed == [dim] and blocked == []
    assert interpret._dependent_value(48.0 * IN, 48.0 * IN, 55.0 * IN) == 55.0 * IN


def test_a_declaration_still_beats_the_name():
    """PartType is the escape hatch, in both directions. A part that SAYS it is a hanger is one.

    The app's `InferPartType` refuses to stamp HANGER on a template for exactly this reason: a
    declaration would undo the exception permanently, in a file on disk.
    """
    dim = "D1@Sketch1 [LUCY 48-HANGER TEMPLATE-1]"
    types = {"LUCY 48-HANGER TEMPLATE-1": "HANGER"}
    assert not policy.is_template(dim, types)
    assert policy.is_hanger(dim, None, types)
    assert policy.fixed_size_reason(dim, None, types)


def test_the_exception_is_scoped_to_the_hanger_entry():
    """Only the hanger freeze makes room for a template.

    No clip, bracket or power-supply template exists in any model here, so widening it would be
    a guess. A hypothetical one stays frozen until somebody measures a real part.
    """
    for dim, word in [("D1@Sketch1 [1005-CLIP TEMPLATE-1]", "clip"),
                      ("D1@Sketch1 [LPM TEMPLATE-1]", "power supply"),
                      ("D2@Base-Flange1 [2867-BRACKET TEMPLATE-1]", "bracket")]:
        assert policy.fixed_size_reason(dim), "%s (%s) must stay frozen" % (dim, word)


def test_other_fixed_size_hardware_is_untouched():
    for dim in ["D1@Sketch1 [1005-CLIP-2]",
                "D1@WIDTH [LPM-24060A-1]",
                "D2@Base-Flange1 [2867-HANGING-BRACKET-1]",
                "D2@Base-Flange1 [2004-HANGING-BRACKET-CLIP-1]"]:
        assert policy.fixed_size_reason(dim), dim
