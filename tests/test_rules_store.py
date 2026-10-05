"""The storage layer: family keying, coverage-based selection, and bootstrap.

The behaviours pinned here are the ones that caused real losses: rules living inside the
deleted build folder, and a sized variant silently resolving to no rules at all.
"""

import json

import pytest

from engine.rules import rules_store as store


def _doc(master="D1@Sketch1 [MIRROR-1]", deps=(), **extra):
    doc = {
        "model": "X",
        "width": [{"if_changes": master, "also_change": list(deps)}],
        "height": [],
    }
    doc.update(extra)
    return doc


# ── family derivation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("stem, family", [
    ("KELLY-24.00X48.00-LED", "KELLY-LED"),
    ("KELLY-30.00X48.00-LED", "KELLY-LED"),          # a sized variant → SAME family
    ("AMBER-36.00X36.00-LED", "AMBER-LED"),
    ("ISABELL-24.00X36.00-LED-HO", "ISABELL-LED-HO"),
    ("CLARA-36.00X36.00", "CLARA"),
    ("ALPHA-24.00X40.00-WHITE", "ALPHA-WHITE"),
    ("TOMO-24.00X36.00- BRUSHED BRASS", "TOMO-BRUSHED-BRASS"),
    ("SUZI-36.00X36.00_COPY", "SUZI"),               # Work-on-a-Copy suffix stripped
    ("JEN-42.00X42.00-LED-HO", "JEN-LED-HO"),
])
def test_family_of_strips_the_size_token(stem, family):
    assert store.family_of(f"C:\\models\\{stem}.SLDASM") == family


def test_suffixes_are_not_collapsed_into_one_family():
    """ISABELL-LED and ISABELL-LED-HO are different products and must not share rules."""
    assert store.family_of("ISABELL-24.00X36.00-LED.SLDASM") != \
           store.family_of("ISABELL-24.00X36.00-LED-HO.SLDASM")


def test_family_of_handles_no_path():
    assert store.family_of(None) == ""
    assert store.stem_of(None) == ""


# ── fingerprint / coverage ────────────────────────────────────────────────────

def test_dims_of_collects_every_referenced_dim():
    doc = _doc("W@M [MIRROR-1]", ["W@C [CHASSIS-1]"],
               offset=[{"target_dim": "D1@Sketch81 [CHASSIS-1]", "source_dim": "D2@Base [HANGER-1]"}],
               position=[{"position_dim": "D1@Distance2 [LPM-1]", "driver_dim": "W@M [MIRROR-1]"}])
    assert store.dims_of(doc) == {
        "W@M [MIRROR-1]", "W@C [CHASSIS-1]",
        "D1@Sketch81 [CHASSIS-1]", "D2@Base [HANGER-1]", "D1@Distance2 [LPM-1]",
    }


def test_coverage_reports_missing_dims_and_masters():
    doc = _doc("W@M [MIRROR-1]", ["W@C [CHASSIS-1]", "W@X [GONE-1]"])
    cov = store.coverage(doc, ["W@M [MIRROR-1]", "W@C [CHASSIS-1]"])
    assert cov.total == 3 and cov.present == 2
    assert cov.missing == ["W@X [GONE-1]"]
    assert cov.missing_masters == []
    assert cov.usable is True          # a missing DEPENDENT is tolerated


def test_a_set_whose_master_is_absent_is_not_usable():
    cov = store.coverage(_doc("W@M [MIRROR-1]"), ["W@C [CHASSIS-1]"])
    assert cov.missing_masters == ["W@M [MIRROR-1]"]
    assert cov.usable is False


# ── selection ─────────────────────────────────────────────────────────────────

def test_each_version_uses_its_own_rule_set():
    """Rules are per VERSION: two sizes of one product are independently tunable."""
    store.write_key("KELLY-24.00X48.00-LED", _doc("W@V24 [MIRROR-1]"))
    store.write_key("KELLY-30.00X48.00-LED", _doc("W@V30 [MIRROR-1]"))

    a = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", ["W@V24 [MIRROR-1]"])
    b = store.select_for_model("C:\\m\\KELLY-30.00X48.00-LED.SLDASM", ["W@V30 [MIRROR-1]"])
    assert (a.key, a.source) == ("KELLY-24.00X48.00-LED", "version")
    assert (b.key, b.source) == ("KELLY-30.00X48.00-LED", "version")
    assert a.warning == "" and b.warning == ""


def test_a_new_size_falls_back_to_a_sibling_version():
    """SaveSizedVariant renames the assembly, so a fresh clone has no set of its own.

    Without this tier it would drop to LLM classification — the original bug.
    """
    store.write_key("KELLY-24.00X48.00-LED", _doc("W@M [MIRROR-1]", ["W@C [CHASSIS-1]"]))
    sel = store.select_for_model("C:\\m\\KELLY-36.00X60.00-LED.SLDASM",
                                 ["W@M [MIRROR-1]", "W@C [CHASSIS-1]"])
    assert sel.key == "KELLY-24.00X48.00-LED" and sel.source == "sibling"
    assert "no rule set of its own" in sel.warning      # and the user is told


def test_this_versions_set_wins_over_a_sibling():
    store.write_key("KELLY-24.00X48.00-LED", _doc("W@MINE [MIRROR-1]"))
    store.write_key("KELLY-30.00X48.00-LED", _doc("W@SIBLING [MIRROR-1]"))
    sel = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", ["W@MINE [MIRROR-1]"])
    assert sel.source == "version"
    assert store.masters_of(sel.doc) == {"W@MINE [MIRROR-1]"}


def test_a_legacy_family_keyed_set_still_serves_as_a_sibling():
    """Rule sets written by the earlier family-keyed scheme must not become unreachable."""
    store.write_key("KELLY-LED", _doc("W@M [MIRROR-1]"))
    sel = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", ["W@M [MIRROR-1]"])
    assert sel.key == "KELLY-LED" and sel.source == "sibling"


def test_siblings_do_not_leak_across_products():
    """ISABELL-LED-HO must never borrow ISABELL-LED's rules — different products."""
    store.write_key("ISABELL-24.00X36.00-LED", _doc("W@M [MIRROR-1]"))
    sel = store.select_for_model("C:\\m\\ISABELL-30.00X36.00-LED-HO.SLDASM", ["W@M [MIRROR-1]"])
    assert sel.doc is None


def test_band_variant_is_chosen_by_dim_coverage():
    """AMBER 36 vs 60 is a different chassis part number, so a second set must win for it."""
    store.write_key("AMBER-LED", _doc("W@M [MIRROR-1]", ["W@C [12204-CHASSIS-1]"]))
    store.write_key("AMBER-LED#2", _doc("W@M [MIRROR-1]", ["W@C [12999-CHASSIS-1]"]))

    small = store.select_for_model("C:\\m\\AMBER-36.00X36.00-LED.SLDASM",
                                   ["W@M [MIRROR-1]", "W@C [12204-CHASSIS-1]"])
    big = store.select_for_model("C:\\m\\AMBER-60.00X36.00-LED.SLDASM",
                                 ["W@M [MIRROR-1]", "W@C [12999-CHASSIS-1]"])
    assert small.key == "AMBER-LED"
    assert big.key == "AMBER-LED#2"


def test_unusable_sets_are_rejected_rather_than_applied():
    store.write_key("KELLY-LED", _doc("W@GONE [MIRROR-1]"))
    sel = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", ["W@M [MIRROR-1]"])
    assert sel.doc is None and sel.source == "none"
    assert sel.considered == 1


def test_partial_coverage_is_reported_in_the_warning():
    store.write_key("KELLY-LED", _doc("W@M [MIRROR-1]", ["W@A [A-1]", "W@B [B-1]"]))
    sel = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", ["W@M [MIRROR-1]"])
    assert sel.doc is not None                     # master present → still applied
    assert "2 dimension(s) not present" in sel.warning


def test_selection_without_live_dims_skips_scoring():
    """/get-rules only displays a set, so an empty dim list must not reject everything."""
    store.write_key("KELLY-LED", _doc("W@M [MIRROR-1]"))
    sel = store.select_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", [])
    assert sel.key == "KELLY-LED"


def test_no_family_match_returns_empty_selection():
    sel = store.select_for_model("C:\\m\\UNKNOWN-10.00X10.00.SLDASM", ["A"])
    assert sel.doc is None and sel.key == ""


# ── saving ────────────────────────────────────────────────────────────────────

def test_save_uses_the_version_key_and_stamps_provenance():
    key = store.save_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM",
                               _doc("W@M [MIRROR-1]", ["W@C [CHASSIS-1]"]))
    assert key == "KELLY-24.00X48.00-LED"
    doc = store.read_key(key)
    assert doc["family"] == "KELLY-LED"                 # grouping is still recorded
    assert doc["generated_for"] == "KELLY-24.00X48.00-LED"


def test_each_version_saves_to_its_own_key():
    a = store.save_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", _doc("W@M [MIRROR-1]"))
    b = store.save_for_model("C:\\m\\KELLY-30.00X48.00-LED.SLDASM", _doc("W@M [MIRROR-1]"))
    assert a != b
    assert sorted(store.candidate_keys("KELLY-LED")) == [
        "KELLY-24.00X48.00-LED", "KELLY-30.00X48.00-LED"]


def test_regenerating_one_version_updates_it_in_place():
    store.save_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", _doc("W@OLD [MIRROR-1]"))
    store.save_for_model("C:\\m\\KELLY-24.00X48.00-LED.SLDASM", _doc("W@NEW [MIRROR-1]"))
    assert len(store.candidate_keys("KELLY-LED")) == 1
    assert store.masters_of(store.read_key("KELLY-24.00X48.00-LED")) == {"W@NEW [MIRROR-1]"}


def test_a_first_save_for_a_new_size_inherits_the_siblings_authored_sections():
    """pattern_rules / offset / position are authored once and shouldn't be lost per size."""
    store.write_key("KELLY-24.00X48.00-LED",
                    _doc("W@M [MIRROR-1]", offset=[{"target_dim": "T", "source_dim": "S"}]))
    inherited = store.read_for_key_or_family("C:\\m\\KELLY-30.00X48.00-LED.SLDASM")
    assert inherited is not None and len(inherited["offset"]) == 1


# ── bootstrap: migration + seeding ────────────────────────────────────────────

def test_legacy_files_migrate_to_their_family_key(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "KELLY-24.00X48.00-LED.rules.json").write_text(
        json.dumps(_doc("W@M [MIRROR-1]", ["W@C [CHASSIS-1]"])))
    monkeypatch.setattr(store, "legacy_dir", lambda: legacy)
    monkeypatch.setattr(store, "seed_dir", lambda: tmp_path / "none")

    info = store.bootstrap()
    assert info["migrated"] == ["KELLY-LED"]
    # …and the migrated set immediately serves a DIFFERENT size of that family.
    sel = store.select_for_model("C:\\m\\KELLY-30.00X48.00-LED.SLDASM",
                                 ["W@M [MIRROR-1]", "W@C [CHASSIS-1]"])
    assert sel.key == "KELLY-LED"


def test_a_seed_never_overwrites_an_existing_set(monkeypatch, tmp_path):
    store.write_key("KELLY-LED", _doc("W@TUNED [MIRROR-1]"))
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "KELLY-24.00X48.00-LED.rules.json").write_text(json.dumps(_doc("W@FACTORY [MIRROR-1]")))
    monkeypatch.setattr(store, "legacy_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(store, "seed_dir", lambda: seed)

    store.bootstrap()
    # Different BOM (no component overlap) → stored alongside, and the tuned one is intact.
    assert store.masters_of(store.read_key("KELLY-LED")) == {"W@TUNED [MIRROR-1]"}


def test_bootstrap_is_idempotent(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "KELLY-24.00X48.00-LED.rules.json").write_text(json.dumps(_doc()))
    monkeypatch.setattr(store, "legacy_dir", lambda: legacy)
    monkeypatch.setattr(store, "seed_dir", lambda: tmp_path / "none")

    store.bootstrap()
    second = store.bootstrap()
    assert second["migrated"] == []          # same BOM already present → no duplicate
    assert second["total"] == 1


def test_pre_family_schema_files_are_skipped(monkeypatch, tmp_path):
    """An old trigger/ratio-format file cannot be interpreted and must not be imported."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "OLD-24.00X24.00.rules.json").write_text(
        json.dumps({"model": "OLD", "rules": [{"trigger": "width", "dimensions": []}]}))
    monkeypatch.setattr(store, "legacy_dir", lambda: legacy)
    monkeypatch.setattr(store, "seed_dir", lambda: tmp_path / "none")
    assert store.bootstrap()["migrated"] == []
