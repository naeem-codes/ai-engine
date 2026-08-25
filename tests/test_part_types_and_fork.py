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


# ── finalising: the working set takes the exported size ──────────────────────
# `#2` is deliberate WHILE the variant is provisional. Export is the point it stops being
# provisional — the folder, the assembly, the drawings and the PDFs all carry the new size by
# then, and rules still naming the old one are the last thing out of step.

def _forked(tmp_path, monkeypatch):
    """Seed the original set and fork a working `#2` off it, as a resize+rename would."""
    path, _ = _seed(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "6666-CXP-CUSTOM-CHAS"})
    return path


def test_finalize_renames_the_working_set_to_the_EXPORTED_size(tmp_path, monkeypatch):
    _forked(tmp_path, monkeypatch)
    # SaveSizedVariant has since renamed the assembly 36x36 -> 20x20.
    old, new, _ = store.finalize_working_set("X/CLARA-20.00X20.00.SLDASM")

    assert (old, new) == ("CLARA-36.00X36.00#2", "CLARA-20.00X20.00")
    assert store.read_key("CLARA-36.00X36.00#2") is None      # moved, not copied
    doc = store.read_key("CLARA-20.00X20.00")
    assert doc["model"] == "CLARA-20.00X20.00"
    assert doc["variant"] == "CLARA-20.00X20.00"
    assert "forked_from" not in doc                            # it IS the set now, not a fork of one
    # the renames it was carrying survive the rekey
    assert doc["width"][0]["also_change"] == ["D1@Sketch1 [6666-CXP-CUSTOM-CHAS-2]"]


def test_finalize_leaves_the_ORIGINAL_set_untouched(tmp_path, monkeypatch):
    _forked(tmp_path, monkeypatch)
    store.finalize_working_set("X/CLARA-20.00X20.00.SLDASM")

    orig = store.read_key("CLARA-36.00X36.00")
    assert orig["width"][0]["also_change"] == ["D1@Sketch1 [12393-CHASSIS-2]"]


def test_finalize_upgrades_the_match_from_a_SIBLING_to_the_models_OWN_key(tmp_path, monkeypatch):
    """The point of the rekey, beyond tidiness: coverage scoring stops being involved."""
    _forked(tmp_path, monkeypatch)
    path = "X/CLARA-20.00X20.00.SLDASM"
    assert store.select_for_model(path).key != "CLARA-20.00X20.00"

    store.finalize_working_set(path)
    sel = store.select_for_model(path)
    assert sel.key == "CLARA-20.00X20.00"
    assert sel.source == "version"      # the model's own key, not a scored sibling


def test_finalize_REFUSES_to_overwrite_an_existing_set_for_that_size(tmp_path, monkeypatch):
    """A previous variant already shipped at 20x20 and its rules were tuned. Losing those to be
    tidy is far worse than keeping a `#2` name, so the working set stays where it is."""
    _forked(tmp_path, monkeypatch)
    store.write_key("CLARA-20.00X20.00", {"model": "CLARA-20.00X20.00", "width": [], "height": []})

    old, new, note = store.finalize_working_set("X/CLARA-20.00X20.00.SLDASM")
    assert old == new == "CLARA-36.00X36.00#2"      # signals "nothing moved" to the caller
    assert "already has its own rule set" in note
    assert store.read_key("CLARA-36.00X36.00#2") is not None
    assert store.read_key("CLARA-20.00X20.00")["width"] == []   # the incumbent is intact


def test_finalize_is_a_no_op_when_the_size_never_changed(tmp_path, monkeypatch):
    """Export without a resize: `CLARA-36.00X36.00#2` would rekey onto the ORIGINAL set's key."""
    _forked(tmp_path, monkeypatch)
    assert store.finalize_working_set("X/CLARA-36.00X36.00.SLDASM") is not None  # refusal, not a move
    assert store.read_key("CLARA-36.00X36.00#2") is not None
    assert store.read_key("CLARA-36.00X36.00")["width"][0]["also_change"] == \
        ["D1@Sketch1 [12393-CHASSIS-2]"]


def test_finalize_does_nothing_when_nothing_was_ever_forked(tmp_path, monkeypatch):
    """Export on a model that was never renamed — there is no working set to rename."""
    _seed(tmp_path, monkeypatch)
    assert store.finalize_working_set("X/CLARA-20.00X20.00.SLDASM") is None


def test_finalize_twice_is_harmless(tmp_path, monkeypatch):
    """Re-clicking Export is a plain overwrite everywhere else; it must be here too."""
    _forked(tmp_path, monkeypatch)
    path = "X/CLARA-20.00X20.00.SLDASM"
    store.finalize_working_set(path)
    assert store.finalize_working_set(path) is None
    assert store.read_key("CLARA-20.00X20.00") is not None


# ── the rename must be IDEMPOTENT ────────────────────────────────────────────
#
# Live 2026-08-25. The fork reads the WORKING set so renames chain, and a rename usually
# APPENDS to the name. A plain `[{old}-` -> `[{new}-` replace then matches its own output on
# the next round: `[12393-CHASSIS-KUCHU-PUCHU-2]` still starts with `[12393-CHASSIS-`, so it
# became `[12393-CHASSIS-KUCHU-PUCHU-KUCHU-PUCHU-2]` and stopped naming anything real.
#
# What it cost, in order: the chassis WIDTH dim vanished from the rules, so the chassis never
# resized; the hanger cap reads that same dim and silently switched off; a 20" prefab was
# chosen for a 10" chassis; the tab follower wrote a 15.75" spacing into it; HANGING TAB
# LOCATIONS failed, Sketch82 lost the geometry it was sketched on, and the resize aborted.

SUFFIX_RENAME = {"12393-CHASSIS": "12393-CHASSIS-KUCHU-PUCHU"}


def test_renaming_TWICE_to_the_same_name_does_not_double_the_suffix(tmp_path, monkeypatch):
    path, _ = _seed(tmp_path, monkeypatch)

    key, once = store.fork_with_renamed_components(path, SUFFIX_RENAME)
    assert once["width"][0]["also_change"] == ["D1@Sketch1 [12393-CHASSIS-KUCHU-PUCHU-2]"]

    # Second round — the model files were reverted and renamed the same way again, but the
    # working set persisted in %PROGRAMDATA% and is what the fork reads.
    again = store.fork_with_renamed_components(path, SUFFIX_RENAME)
    assert again is None, "nothing changed, so there was nothing to rewrite"

    assert store.read_key(key)["width"][0]["also_change"] == [
        "D1@Sketch1 [12393-CHASSIS-KUCHU-PUCHU-2]"]


def test_the_doubled_name_is_not_produced_in_label_KEYS_either(tmp_path, monkeypatch):
    """The dict-key branch had the identical flaw via `k.startswith(f"{old}-")`."""
    path, _ = _seed(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, SUFFIX_RENAME)
    labels = store.read_key("CLARA-36.00X36.00#2")["component_labels"]

    assert "12393-CHASSIS-KUCHU-PUCHU-2" in labels
    assert not any("KUCHU-PUCHU-KUCHU-PUCHU" in k for k in labels)


def test_chaining_two_DIFFERENT_renames_still_works(tmp_path, monkeypatch):
    """The guard must not break the case it was built for: renames still compose."""
    path, _ = _seed(tmp_path, monkeypatch)
    store.fork_with_renamed_components(path, {"12393-CHASSIS": "12393-CHASSIS-REV-A"})
    _, twice = store.fork_with_renamed_components(
        path, {"12393-CHASSIS-REV-A": "12393-CHASSIS-REV-B"})

    assert twice["width"][0]["also_change"] == ["D1@Sketch1 [12393-CHASSIS-REV-B-2]"]
    assert "12393-CHASSIS-REV-B-2" in twice["component_labels"]


def test_a_stem_that_is_a_PREFIX_of_another_part_is_left_alone(tmp_path, monkeypatch):
    """`12393-CHASSIS` must not match `12393-CHASSIS-PLATE`, which is a different file."""
    monkeypatch.setenv("LUMI_DATA_DIR", str(tmp_path))
    store.write_key("P", {
        "model": "P",
        "width": [{"if_changes": "D1@Sketch1 [MIRROR-1]", "also_change": [
            "D1@Sketch1 [12393-CHASSIS-2]", "D1@Sketch1 [12393-CHASSIS-PLATE-1]"]}],
        "height": [], "component_labels": {},
    })
    _, forked = store.fork_with_renamed_components("X/P.SLDASM", SUFFIX_RENAME)

    assert forked["width"][0]["also_change"] == [
        "D1@Sketch1 [12393-CHASSIS-KUCHU-PUCHU-2]",
        "D1@Sketch1 [12393-CHASSIS-PLATE-1]",       # untouched
    ]
