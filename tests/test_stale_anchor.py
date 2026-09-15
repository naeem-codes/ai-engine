"""A rule set whose ANCHOR went stale must not be thrown away when its dependants are right.

HALO, live 2026-09-15. After a rename there were two sets on disk:

    HALO-30.00       trigger [HALO-30-MIRROR-1]   deps 12433-*, 12435-*, 12646-*, 1047-*
    HALO-30.00#2     trigger [HALO-60-MIRROR-1]   deps 1111-*

The model in hand was the `1111-*` one, with its mirror now `HALO-70-MIRROR-1`. So `#2` named six
of the model's seven dims and the original named none of them. The selector kept the original:

    [STORE] this version's own rule set 'HALO-30.00' — 0/7 dims present
    [STORE] 'HALO-30.00#2' rejected — master dim(s) absent: D1@Sketch1 [HALO-60-MIRROR-1]
    [STORE] keeping 'HALO-30.00' — no usable sibling in family 'HALO' (1 candidate(s))

Every dependant then evaporated, the mirror shrank alone inside a frame that stayed at Ø70, and
only the crossing check caught it.

The anchor is the recoverable half — the app measures the master off the model every Refresh.
The dependants are the hard part. So a stale trigger no longer vetoes a set, and the trigger is
re-derived; a set with NOTHING present is still refused, because that one really is describing a
different model.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rules_store as store

MODEL = "C:\\m\\HALO-30.00.SLDASM"
LIVE_MASTER = "D1@Sketch1 [HALO-70-MIRROR-1]"
OLD_DEPS = ["D1@Sketch1 [12433-CHASSIS-1]", "D1@Sketch1 [12435-CHASSIS RING-1]",
            "D1@Sketch6 [12435-CHASSIS RING-1]"]
NEW_DEPS = ["D1@Sketch1 [1111-CHASSIS-1]", "D1@Sketch1 [1111-CHASSIS RING-1]",
            "D1@Sketch6 [1111-CHASSIS RING-1]"]
LIVE = [LIVE_MASTER] + NEW_DEPS


def _doc(trigger, deps):
    return {"model": "X", "width": [{"if_changes": trigger, "also_change": deps}], "height": []}


# `tests/conftest.py` already points the store at a temp dir for every test.


def test_the_fork_wins_even_though_its_trigger_is_stale():
    store.write_key("HALO-30.00", _doc("D1@Sketch1 [HALO-30-MIRROR-1]", OLD_DEPS))
    store.write_key("HALO-30.00#2", _doc("D1@Sketch1 [HALO-60-MIRROR-1]", NEW_DEPS))
    sel = store.select_for_model(MODEL, LIVE)
    assert sel.key == "HALO-30.00#2", "the 0/4 set was kept over the 3/4 one again"
    assert sel.source == "sibling"


def test_an_anchorable_set_still_wins_a_tie():
    """Same coverage on both — the one that still names its own master must win, and this
    version's own set wins over a sibling."""
    store.write_key("HALO-30.00", _doc(LIVE_MASTER, NEW_DEPS))          # 4/4, anchorable
    store.write_key("HALO-30.00#2", _doc("D1@Sketch1 [GONE-1]", NEW_DEPS))   # 3/4, not
    sel = store.select_for_model(MODEL, LIVE)
    assert sel.key == "HALO-30.00"


def test_a_set_that_names_nothing_here_is_still_refused():
    """The guard that existed before, and must survive: no anchor AND no dependants present."""
    store.write_key("HALO-30.00#2", _doc("D1@Sketch1 [GONE-1]", ["D9@Nope [ALSO-GONE-1]"]))
    sel = store.select_for_model(MODEL, LIVE)
    assert sel.doc is None and sel.source == "none"


def test_a_stale_anchor_never_beats_better_coverage():
    """It is ranked BELOW an anchorable set of equal coverage, never above one that covers more."""
    store.write_key("HALO-30.00", _doc(LIVE_MASTER, NEW_DEPS))               # 4/4
    store.write_key("HALO-30.00#2", _doc("D1@Sketch1 [GONE-1]", NEW_DEPS + ["D1@X [GONE-2]"]))
    sel = store.select_for_model(MODEL, LIVE)
    assert sel.key == "HALO-30.00"
