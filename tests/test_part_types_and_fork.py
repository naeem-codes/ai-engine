"""Renaming a part must not silently disarm the policy, and must not overwrite its rule set.

Both problems have the same root: identity is inferred from the component id, and renaming
changes it. A chassis renamed `Custom-Chasis-12323` (one S) stops matching "CHASSIS", and the
rules that name `[12393-CHASSIS-2]` stop describing the model.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import resize_policy as policy
import rules_store as store


# ── declared PartType beats the keyword guess ────────────────────────────────

RENAMED = "D1@Sketch1 [Custom-Chasis-12323-2]"


def test_a_renamed_chassis_is_unrecognisable_without_a_declaration():
    """The bug, pinned: "Chasis" has one S, so the keyword match cannot see it."""
    assert policy.is_chassis(RENAMED) is False


def test_declaring_the_type_restores_it():
    assert policy.is_chassis(RENAMED, {"Custom-Chasis-12323-2": "CHASSIS"}) is True


def test_declaration_beats_a_MISLEADING_name():
    """Not just a fallback — the declaration is authoritative. A part named …CHASSIS… that
    declares itself a MIRROR is a mirror, which is what makes the dropdown able to CORRECT a
    part the keyword guess got wrong."""
    d = "D1@Sketch1 [12393-CHASSIS-2]"
    assert policy.is_chassis(d) is True
    assert policy.is_chassis(d, {"12393-CHASSIS-2": "MIRROR"}) is False


def test_an_empty_map_changes_nothing():
    """The whole migration rests on this: an un-stamped model must behave exactly as before."""
    for dim, fn in [
        ("D1@Sketch1 [12393-CHASSIS-2]", policy.is_chassis),
        ("D2@Base-Flange1 [1038-HANGER-1]", lambda d, t=None: policy.is_hanger(d, None, t)),
    ]:
        assert fn(dim) == fn(dim, {}) is True


def test_fixed_size_follows_the_declaration_both_ways():
    hidden_psu = "D1@WIDTH [12500-CUSTOM-PART-1]"
    assert policy.fixed_size_reason(hidden_psu) is None
    assert policy.fixed_size_reason(hidden_psu, None, {"12500-CUSTOM-PART-1": "PSU"})
    # …and a part whose NAME says clip but which declares itself a chassis is resizable.
    clip = "D1@Sketch1 [1005-CLIP-1]"
    assert policy.fixed_size_reason(clip) is not None
    assert policy.fixed_size_reason(clip, None, {"1005-CLIP-1": "CHASSIS"}) is None


# ── forking the rules ────────────────────────────────────────────────────────

def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMI_DATA_DIR", str(tmp_path))
    doc = {
        "model": "CLARA-36.00X36.00",
        "width": [{"if_changes": "D1@Sketch1 [CLARA-MIRROR-1]",
                   "also_change": ["D1@Sketch1 [12393-CHASSIS-2]"]}],
        "height": [{"if_changes": "D2@Sketch1 [CLARA-MIRROR-1]", "also_change": []}],
        "component_labels": {"12393-CHASSIS-2": "Main Chassis", "CLARA-MIRROR-1": "Mirror Glass"},
    }
    store.write_key("CLARA-36.00X36.00", doc)
    return "X/CLARA-36.00X36.00.SLDASM", doc


def test_fork_substitutes_stems_in_dims_and_in_label_KEYS(tmp_path, monkeypatch):
    path, _ = _seed(tmp_path, monkeypatch)
    key, forked = store.fork_with_renamed_components(
        path, {"12393-CHASSIS": "6666-CXP-CUSTOM-CHAS"})

    assert key == "CLARA-36.00X36.00#2"
    assert forked["width"][0]["also_change"] == ["D1@Sketch1 [6666-CXP-CUSTOM-CHAS-2]"]
    # component_labels is keyed by the BARE id — no brackets — so it needs a PREFIX swap, not the
    # bracketed one the dim names use.
    assert "6666-CXP-CUSTOM-CHAS-2" in forked["component_labels"]
    assert "12393-CHASSIS-2" not in forked["component_labels"]


def test_one_renamed_FILE_covers_every_instance_of_it(tmp_path, monkeypatch):
    """Live 2026-08-21: renaming the Zortech file renames all four LED strips, but an id-keyed
    map caught only the one instance whose path matched the spec — leaving -1, -2 and -4 naming
    a component that no longer existed. A stem covers them all."""
    monkeypatch.setenv("LUMI_DATA_DIR", str(tmp_path))
    store.write_key("CLARA-36.00X36.00", {
        "model": "CLARA-36.00X36.00",
        "width": [{"if_changes": "D1@Sketch1 [CLARA-MIRROR-1]", "also_change": [
            "D1@Boss-Extrude1 [Zortech-Low-Profile-1900-Lumens-1]",
            "D1@Boss-Extrude1 [Zortech-Low-Profile-1900-Lumens-3]"]}],
        "height": [], "component_labels": {},
    })
    _, forked = store.fork_with_renamed_components(
        "X/CLARA-36.00X36.00.SLDASM", {"Zortech-Low-Profile-1900-Lumens": "LED-CXP-1"})
    assert forked["width"][0]["also_change"] == [
        "D1@Boss-Extrude1 [LED-CXP-1-1]",
        "D1@Boss-Extrude1 [LED-CXP-1-3]",
    ]


def test_the_fork_never_writes_a_slashed_instance_path(tmp_path, monkeypatch):
    """A nested component's Name2 is "Parent-1/Child-2"; every dim name strips that. Substituting
    on stems means the slash can never reach the rules file."""
    path, _ = _seed(tmp_path, monkeypatch)
    _, forked = store.fork_with_renamed_components(
        path, {"12393-CHASSIS": "6666-CXP-CUSTOM-CHAS"})
    assert "/" not in json.dumps(forked)


def test_the_original_is_never_touched(tmp_path, monkeypatch):
    """It still describes every un-renamed assembly built from this product."""
    path, original = _seed(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "6666-CXP-CUSTOM-CHAS"})
    on_disk = json.loads(store._key_path("CLARA-36.00X36.00").read_text(encoding="utf-8"))
    assert on_disk["width"] == original["width"]
    assert on_disk["component_labels"] == original["component_labels"]


def test_no_fork_when_the_rename_touches_nothing_this_set_references(tmp_path, monkeypatch):
    path, _ = _seed(tmp_path, monkeypatch)
    assert store.fork_with_renamed_components(path, {"SOMETHING-ELSE": "X"}) is None
    assert store.fork_with_renamed_components(path, {}) is None


def test_a_second_rename_UPDATES_the_working_set_and_keeps_the_first(tmp_path, monkeypatch):
    """One working file per model, rewritten each time — never #3, #4, #5.

    And it must build on the PREVIOUS rename, not on the original: forking from the original
    again would apply only the newest substitution and silently drop the earlier ones, leaving a
    set describing neither the old model nor the new one.
    """
    path, _ = _seed(tmp_path, monkeypatch)
    k1, _ = store.fork_with_renamed_components(path, {"12393-CHASSIS": "1111-CHASSIS"})
    k2, doc = store.fork_with_renamed_components(path, {"CLARA-MIRROR": "CLARA-CXP"})

    assert k1 == k2 == "CLARA-36.00X36.00#2"
    assert not store._key_path("CLARA-36.00X36.00#3").exists()

    # BOTH renames survive.
    assert doc["width"][0]["if_changes"] == "D1@Sketch1 [CLARA-CXP-1]"
    assert doc["width"][0]["also_change"] == ["D1@Sketch1 [1111-CHASSIS-2]"]


def test_the_working_set_is_used_after_the_second_rename(tmp_path, monkeypatch):
    path, _ = _seed(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "1111-CHASSIS"})
    store.fork_with_renamed_components(path, {"CLARA-MIRROR": "CLARA-CXP"})
    live = _dims("D1@Sketch1 [CLARA-CXP-1]", "D2@Sketch1 [CLARA-CXP-1]",
                 "D1@Sketch1 [1111-CHASSIS-2]", "D2@Sketch1 [1111-CHASSIS-2]")
    sel = store.select_for_model(path, live)
    assert sel.key == "CLARA-36.00X36.00#2"
    # _seed's height rule has no dependents, so the set names 3 dims, not 4.
    assert sel.cover.present == sel.cover.total == 3


# ── the fork must actually be SELECTED ───────────────────────────────────────
#
# Live 2026-08-21: the fork was written correctly and then ignored. select_for_model returned the
# exact-stem set at 0/8 dims present because that tier short-circuited before coverage was
# considered, so the chassis never resized and the hanger cap switched off for want of a chassis
# width. Checking only `usable` would not have caught it either — renaming a DEPENDENT leaves the
# masters intact, and that is the common case.

def _two_sets(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMI_DATA_DIR", str(tmp_path))
    store.write_key("CLARA-36.00X36.00", {
        "model": "CLARA-36.00X36.00",
        "width": [{"if_changes": "D1@Sketch1 [CLARA-MIRROR-1]",
                   "also_change": ["D1@Sketch1 [12393-CHASSIS-2]"]}],
        "height": [{"if_changes": "D2@Sketch1 [CLARA-MIRROR-1]",
                    "also_change": ["D2@Sketch1 [12393-CHASSIS-2]"]}],
        "component_labels": {},
    })
    return "X/CLARA-36.00X36.00.SLDASM"


def _dims(*names):
    # coverage() takes plain dim NAMES, not objects.
    return list(names)


def test_the_fork_wins_when_a_DEPENDENT_was_renamed(tmp_path, monkeypatch):
    """The case `usable` would have missed: the masters still exist, so the stale set passes
    every test except the one that matters — how much of the live model it describes."""
    path = _two_sets(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "1111-CHASSIS"})

    live = _dims("D1@Sketch1 [CLARA-MIRROR-1]", "D2@Sketch1 [CLARA-MIRROR-1]",
                 "D1@Sketch1 [1111-CHASSIS-2]", "D2@Sketch1 [1111-CHASSIS-2]")
    sel = store.select_for_model(path, live)
    assert sel.key == "CLARA-36.00X36.00#2"
    assert sel.cover.present == sel.cover.total == 4


def test_the_fork_wins_when_the_MASTER_was_renamed(tmp_path, monkeypatch):
    path = _two_sets(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"CLARA-MIRROR": "CLARA-CXP",
                                              "12393-CHASSIS": "1111-CHASSIS"})
    live = _dims("D1@Sketch1 [CLARA-CXP-1]", "D2@Sketch1 [CLARA-CXP-1]",
                 "D1@Sketch1 [1111-CHASSIS-2]", "D2@Sketch1 [1111-CHASSIS-2]")
    assert store.select_for_model(path, live).key == "CLARA-36.00X36.00#2"


def test_nothing_renamed_keeps_the_models_own_set(tmp_path, monkeypatch):
    """The tie goes to the exact stem, so an untouched model behaves exactly as before."""
    path = _two_sets(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "1111-CHASSIS"})
    live = _dims("D1@Sketch1 [CLARA-MIRROR-1]", "D2@Sketch1 [CLARA-MIRROR-1]",
                 "D1@Sketch1 [12393-CHASSIS-2]", "D2@Sketch1 [12393-CHASSIS-2]")
    sel = store.select_for_model(path, live)
    assert sel.key == "CLARA-36.00X36.00"
    assert sel.source == "version"


def test_get_rules_style_call_still_returns_the_models_own_set(tmp_path, monkeypatch):
    """No dims sent means "just show me a rule set" — scoring is skipped, or the rules UI would
    start displaying a sibling."""
    path = _two_sets(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "1111-CHASSIS"})
    assert store.select_for_model(path, None).key == "CLARA-36.00X36.00"
